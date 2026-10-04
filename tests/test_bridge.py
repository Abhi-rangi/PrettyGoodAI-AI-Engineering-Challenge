"""The bridge wired end to end against fake Twilio and OpenAI sockets."""
import asyncio
import base64
import json

from receptionist import bridge as bridge_mod
from receptionist.backend import InMemoryBackend
from receptionist.bridge import CallBridge, StreamStart
from receptionist.config import Settings

AUDIO = base64.b64encode(b"\xff" * 160).decode()


class FakeOpenAI:
    def __init__(self):
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, raw: str):
        self.sent.append(json.loads(raw))

    def push(self, **event):
        self.inbox.put_nowait(json.dumps(event))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.inbox.get()

    async def recv(self):
        return await self.inbox.get()

    def types(self):
        return [e["type"] for e in self.sent]


class FakeTwilio:
    def __init__(self):
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []

    async def send_text(self, raw: str):
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg["event"] == "mark":  # Twilio echoes marks once played
            self.inbox.put_nowait(json.dumps({"event": "mark", "mark": msg["mark"]}))

    async def iter_text(self):
        while True:
            yield await self.inbox.get()


async def wait_for(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not predicate():
        assert loop.time() < end, "timed out"
        await asyncio.sleep(0.01)


def test_greets_runs_tools_and_hangs_up(monkeypatch):
    async def scenario():
        oai, twilio = FakeOpenAI(), FakeTwilio()
        monkeypatch.setattr(bridge_mod.realtime, "connect", lambda settings: oai)
        monkeypatch.setattr(bridge_mod, "DRAIN_S", 0.05)
        backend = InMemoryBackend.seeded()
        b = CallBridge(twilio, Settings(), backend,
                       StreamStart("MZ1", "CA1", "6095550142", {}))
        run = asyncio.create_task(b.run())

        # Session configured, then the receptionist speaks first.
        await wait_for(lambda: oai.types()[:2] == ["session.update", "response.create"])
        assert "609-555-0142" in oai.sent[0]["session"]["instructions"]

        oai.push(type="response.output_audio.delta", response_id="r1", item_id="i1", delta=AUDIO)
        oai.push(type="response.output_audio.done", response_id="r1")
        oai.push(type="response.done", response={"status": "completed"})
        await wait_for(lambda: any(m["event"] == "mark" for m in twilio.sent))
        assert twilio.sent[0] == {"event": "media", "streamSid": "MZ1",
                                  "media": {"payload": AUDIO}}

        # A tool call: result goes back to the model, then it is asked to speak.
        oai.push(type="response.function_call_arguments.done", name="lookup_patient",
                 call_id="c1", arguments=json.dumps({"last_name": "Whitfield",
                                                     "phone": "6095550142"}))
        oai.push(type="response.done", response={"status": "completed"})
        await wait_for(lambda: oai.types()[-1] == "response.create" and len(oai.sent) == 4)
        output = oai.sent[2]["item"]
        assert output["type"] == "function_call_output" and output["call_id"] == "c1"
        assert json.loads(output["output"]) == {"ok": True, "found": True}
        assert b.ctx.patient_id is not None

        # Goodbye, then end_call: hang up once Twilio confirms the goodbye played.
        oai.push(type="response.function_call_arguments.done", name="end_call",
                 call_id="c2", arguments='{"reason": "done"}')
        oai.push(type="response.done", response={"status": "completed"})
        await asyncio.wait_for(run, timeout=2)
        assert {"event": "mark", "streamSid": "MZ1", "mark": {"name": "goodbye"}} in twilio.sent

    asyncio.run(scenario())


def test_barge_in_clears_twilio_and_truncates(monkeypatch):
    async def scenario():
        oai, twilio = FakeOpenAI(), FakeTwilio()
        monkeypatch.setattr(bridge_mod.realtime, "connect", lambda settings: oai)
        monkeypatch.setattr(bridge_mod, "DRAIN_S", 0.05)
        b = CallBridge(twilio, Settings(), InMemoryBackend(),
                       StreamStart("MZ1", "CA1", None, {}))
        run = asyncio.create_task(b.run())
        await wait_for(lambda: len(oai.sent) == 2)

        for _ in range(50):  # 1s of audio generated
            oai.push(type="response.output_audio.delta", response_id="r1",
                     item_id="i1", delta=AUDIO)
        await wait_for(lambda: len(twilio.sent) == 50)
        twilio.inbox.put_nowait(json.dumps(
            {"event": "media", "media": {"timestamp": "300", "payload": AUDIO}}))
        await wait_for(lambda: b.turns.latest_media_ts == 300)
        oai.push(type="input_audio_buffer.speech_started")

        await wait_for(lambda: twilio.sent[-1]["event"] == "clear")
        await wait_for(lambda: oai.types()[-1] == "conversation.item.truncate")
        assert oai.sent[-1]["item_id"] == "i1" and oai.sent[-1]["audio_end_ms"] == 300

        twilio.inbox.put_nowait(json.dumps({"event": "stop"}))
        await asyncio.wait_for(run, timeout=2)

    asyncio.run(scenario())
