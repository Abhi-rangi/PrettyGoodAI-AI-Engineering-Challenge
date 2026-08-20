"""The relay: Twilio Media Streams <-> OpenAI Realtime API.

Both legs speak G.711 mu-law at 8kHz, which is the whole reason this file is short.
Twilio's phone audio is already mu-law, and the Realtime API accepts and emits
`audio/pcmu` natively, so there is no resampling or transcoding anywhere in the hot
path. Audio frames are passed through as base64 strings with the envelope swapped.

Three concurrent tasks run per call: one pump in each direction, plus a watchdog that
guarantees the call cannot hang or run past its budget.
"""
import asyncio
import base64
from collections import deque
import json
import os
import time
from pathlib import Path

from fastapi import WebSocket, WebSocketDisconnect

from . import config
from .persona import Scenario
from .recorder import Recorder
from .transcript import Transcript

try:  # websockets >= 14
    from websockets.asyncio.client import connect as ws_connect
    _HEADER_KW = "additional_headers"
except ImportError:  # older releases
    from websockets.client import connect as ws_connect  # type: ignore
    _HEADER_KW = "extra_headers"

REALTIME_URL = f"wss://api.openai.com/v1/realtime?model={config.REALTIME_MODEL}"

# Dump every server event type to stdout. Invaluable while tuning; noisy otherwise.
DEBUG = os.getenv("REALTIME_DEBUG", "").lower() in {"1", "true", "yes"}

# If the far end never picks up or sits silent, say something rather than hang there.
DEAD_AIR_S = 12
# Once the bot has decided to hang up, wait this long for Twilio to confirm it played
# the goodbye before tearing the call down regardless.
HANGUP_GRACE_S = 8

END_CALL_TOOL = {
    "type": "function",
    "name": "end_call",
    "description": (
        "Hang up the phone. Call this once your reason for calling has been resolved "
        "or clearly cannot be, immediately after you have said goodbye out loud."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "One short sentence on how the call ended.",
            }
        },
        "required": ["reason"],
    },
}


def build_session_config(scenario: Scenario) -> dict:
    """GA Realtime session shape.

    Note this is the *nested* GA schema (session.audio.input.format as an object with
    an `audio/pcmu` type). Most Twilio+OpenAI examples online still use the beta flat
    shape (`input_audio_format: "g711_ulaw"`), which the GA endpoint rejects with
    "Unknown parameter". Kept at module level so preflight.py can validate the exact
    payload the bridge will send without opening a phone call.

    noise_reduction is `near_field`: the input here is a single voice arriving over a
    narrowband phone codec, which is what near_field is tuned for. far_field is for
    laptop and conference-room mics and audibly over-processes telephony.
    """
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "instructions": scenario.system_prompt(),
            "output_modalities": ["audio"],
            "tools": [END_CALL_TOOL],
            "tool_choice": "auto",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},
                    "turn_detection": scenario.vad_config(),
                    "transcription": {"model": config.TRANSCRIBE_MODEL},
                    "noise_reduction": {"type": "near_field"},
                },
                "output": {
                    "format": {"type": "audio/pcmu"},
                    "voice": scenario.voice,
                    "speed": 1.0,
                },
            },
        },
    }


class CallBridge:
    def __init__(self, twilio_ws: WebSocket, scenario: Scenario, label: str,
                 dialed: str = ""):
        self.twilio_ws = twilio_ws
        self.scenario = scenario
        self.stem: Path = config.CALL_DIR / label
        self.recorder = Recorder(self.stem)
        self.transcript = Transcript(self.stem, scenario, dialed)

        self.stream_sid: str | None = None
        self.call_sid: str | None = None

        # Playback bookkeeping, used to truncate cleanly on barge-in.
        self.latest_media_ts = 0          # ms, from Twilio's own frame clock
        self.response_start_ts: int | None = None
        self.last_assistant_item: str | None = None
        self.bot_is_speaking = False

        self.pending_hangup = False
        self.hangup_reason = ""
        self.finished = asyncio.Event()
        self.heard_anything = False
        self.errors: list[str] = []

        # Reply-latency instrumentation. Ours = how long after the agent stopped
        # talking before our first audio frame goes out. Theirs = how long after our
        # turn ends before they start. Both are quality evidence.
        self.agent_stopped_at: float | None = None
        self.bot_finished_at: float | None = None

        # When each side began talking. Queues, not single slots: transcription lands
        # well after the audio, so two or three turns can be outstanding at once and a
        # single variable would stamp them all with the most recent start time.
        self.agent_turn_starts: deque[float] = deque()
        self.patient_turn_starts: deque[float] = deque()
        self.agent_speaking = False
        self.turn_seq = 0
        self.current_response_id: str | None = None
        self.started_at = time.monotonic()

    # ------------------------------------------------------------------- run

    async def run(self) -> None:
        headers = {"Authorization": f"Bearer {config.OPENAI_API_KEY}"}
        async with ws_connect(REALTIME_URL, **{_HEADER_KW: headers}) as openai_ws:
            await openai_ws.send(json.dumps(build_session_config(self.scenario)))

            tasks = [
                asyncio.create_task(self._pump_twilio_to_openai(openai_ws)),
                asyncio.create_task(self._pump_openai_to_twilio(openai_ws)),
                asyncio.create_task(self._watchdog(openai_ws)),
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await self._drain_transcripts(openai_ws)
            for task in done:  # surface exceptions rather than swallowing them
                if task.exception():
                    raise task.exception()

    # ------------------------------------------------------- twilio -> openai

    async def _pump_twilio_to_openai(self, openai_ws) -> None:
        try:
            async for raw in self.twilio_ws.iter_text():
                msg = json.loads(raw)
                event = msg.get("event")

                if event == "media":
                    self.latest_media_ts = int(msg["media"]["timestamp"])
                    payload = msg["media"]["payload"]
                    self.recorder.add_inbound(base64.b64decode(payload))
                    await openai_ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": payload,
                    }))

                elif event == "start":
                    start = msg["start"]
                    self.stream_sid = start["streamSid"]
                    self.call_sid = start["callSid"]
                    print(f"  stream open  call={self.call_sid}")

                elif event == "mark" and msg.get("mark", {}).get("name", "").startswith("turn-"):
                    # Our audio has finished leaving the speaker. Start the clock on how
                    # long the agent takes to reply.
                    self.bot_finished_at = None if self.agent_speaking else self.transcript.now()

                elif event == "mark" and msg.get("mark", {}).get("name") == "goodbye":
                    # Twilio finished playing everything queued before this mark, so
                    # the goodbye actually reached the far end. Safe to hang up.
                    self.finished.set()
                    return

                elif event == "stop":
                    print("  stream closed by Twilio")
                    self.finished.set()
                    return
        except WebSocketDisconnect:
            print("  Twilio disconnected")
            self.finished.set()

    # ------------------------------------------------------- openai -> twilio

    async def _pump_openai_to_twilio(self, openai_ws) -> None:
        async for raw in openai_ws:
            event = json.loads(raw)
            etype = event.get("type", "")
            if DEBUG and not etype.endswith(".delta"):
                print(f"    · {etype}")

            if etype == "error":
                self._log_error(event)

            elif etype == "response.output_audio.delta":
                # Key off the response id, not a speaking flag: barge-in clears the
                # flag mid-response, which would queue a second start time for a turn
                # that only produces one transcript and shift every later timestamp.
                if event.get("response_id") != self.current_response_id:
                    self.current_response_id = event.get("response_id")
                    self.patient_turn_starts.append(self.transcript.now())
                if self.agent_stopped_at is not None:
                    self.transcript.add_metric(
                        "latency", who="patient",
                        ms=round((self.transcript.now() - self.agent_stopped_at) * 1000))
                    self.agent_stopped_at = None
                self.bot_is_speaking = True
                if item_id := event.get("item_id"):
                    self.last_assistant_item = item_id
                await self._send_audio(event["delta"])

            elif etype == "response.output_audio.done":
                # Our side has finished *generating*. Twilio is still playing out the
                # buffered tail, so bot_is_speaking stays true until the response ends.
                self.recorder.mark_response_end()

            elif etype == "input_audio_buffer.speech_started":
                self.heard_anything = True
                if not self.agent_speaking:
                    self.agent_turn_starts.append(self.transcript.now())
                self.agent_speaking = True
                if self.bot_finished_at is not None:
                    self.transcript.add_metric(
                        "latency", who="agent",
                        ms=round((self.transcript.now() - self.bot_finished_at) * 1000))
                    self.bot_finished_at = None
                await self._handle_barge_in(openai_ws)

            elif etype == "input_audio_buffer.speech_stopped":
                self.agent_stopped_at = self.transcript.now()
                self.agent_speaking = False

            elif etype == "conversation.item.input_audio_transcription.completed":
                self._add_turn("AGENT", event.get("transcript", ""))

            elif etype == "response.output_audio_transcript.done":
                self._add_turn("PATIENT", event.get("transcript", ""))

            elif etype == "response.function_call_arguments.done":
                if event.get("name") == "end_call":
                    try:
                        self.hangup_reason = json.loads(event["arguments"]).get("reason", "")
                    except (json.JSONDecodeError, KeyError, TypeError):
                        self.hangup_reason = "unspecified"
                    self.pending_hangup = True
                    self.transcript.add_event("bot chose to hang up:", self.hangup_reason)

            elif etype == "response.done":
                # Only start the clock if the line actually went quiet. If the agent is
                # already mid-utterance when our turn ends, the next speech_started is
                # just the far side pausing and resuming — timing to it measures their
                # sentence length, not their response delay. An early version of this
                # reported an 18-second "silence" on a call whose audio contained no
                # gap longer than 2.4 seconds.
                self.bot_finished_at = None if self.agent_speaking else self.transcript.now()
                self.patient_turn_started = None
                self.bot_is_speaking = False
                self.response_start_ts = None
                if self.pending_hangup:
                    # Queues behind all audio already sent; Twilio echoes it back once
                    # the goodbye has actually played out of the far end's speaker.
                    await self._send_mark("goodbye")

    def _add_turn(self, speaker: str, text: str) -> None:
        queue = self.agent_turn_starts if speaker == "AGENT" else self.patient_turn_starts
        started = queue.popleft() if queue else None
        self.transcript.add(speaker, text, t=started)

    async def _drain_transcripts(self, openai_ws, seconds: float = 3.0) -> None:
        """Collect transcripts that were still in flight when the call ended.

        Whisper finishes well after the audio it describes, so the last few turns of a
        call are still being transcribed when we hang up. Closing the socket
        immediately silently dropped them: one call had thirty seconds of agent speech
        in the recording and no matching lines in the transcript. Both sides of every
        call are a hard requirement, so we wait briefly for the stragglers.
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + seconds
        recovered = 0
        while (remaining := deadline - loop.time()) > 0:
            try:
                raw = await asyncio.wait_for(openai_ws.recv(), timeout=remaining)
            except Exception:
                break
            event = json.loads(raw)
            etype = event.get("type", "")
            if etype == "conversation.item.input_audio_transcription.completed":
                self._add_turn("AGENT", event.get("transcript", ""))
                recovered += 1
            elif etype == "response.output_audio_transcript.done":
                self._add_turn("PATIENT", event.get("transcript", ""))
                recovered += 1
        if recovered:
            print(f"  recovered {recovered} late transcript line(s)")

    def _log_error(self, event: dict) -> None:
        err = event.get("error", {}) or {}
        message = err.get("message", str(event))
        # Truncating an item the server already truncated itself (because
        # turn_detection.interrupt_response is on) is expected and harmless.
        if "truncate" in message.lower() and "already" in message.lower():
            return
        self.errors.append(message)
        print(f"  ! openai error: {message}")
        self.transcript.add_event("api error:", message)

    async def _send_audio(self, b64_payload: str) -> None:
        if not self.stream_sid:
            return
        if self.response_start_ts is None:
            self.response_start_ts = self.latest_media_ts
        self.recorder.add_outbound(base64.b64decode(b64_payload))
        await self.twilio_ws.send_text(json.dumps({
            "event": "media",
            "streamSid": self.stream_sid,
            "media": {"payload": b64_payload},
        }))

    async def _send_mark(self, name: str) -> None:
        if self.stream_sid:
            await self.twilio_ws.send_text(json.dumps({
                "event": "mark",
                "streamSid": self.stream_sid,
                "mark": {"name": name},
            }))

    async def _handle_barge_in(self, openai_ws) -> None:
        """The agent started talking while our patient was mid-sentence.

        Three things have to happen, and skipping any one of them is audible: flush
        Twilio's playout buffer or our voice keeps coming out of the far end's speaker
        for seconds after we "stopped"; tell OpenAI to forget the audio it generated
        but nobody heard, or it will believe it said words it did not; and trim our own
        recording so the MP3 matches what was actually on the line.

        The Twilio `clear` is unconditional. Generation finishes well before playback
        does, so a barge-in arriving after response.output_audio.done still has real
        audio queued at Twilio even though there is no longer anything to truncate.
        Making the clear conditional on having a truncatable item was a bug: the most
        common interruption, one that lands right at the end of our turn, went
        unflushed and the two sides talked over each other.
        """
        if not self.bot_is_speaking:
            return

        await self.twilio_ws.send_text(json.dumps({
            "event": "clear", "streamSid": self.stream_sid,
        }))

        played_ms = 0
        if self.last_assistant_item is not None and self.response_start_ts is not None:
            played_ms = max(0, self.latest_media_ts - self.response_start_ts)
            if played_ms > 0:
                await openai_ws.send(json.dumps({
                    "type": "conversation.item.truncate",
                    "item_id": self.last_assistant_item,
                    "content_index": 0,
                    "audio_end_ms": played_ms,
                }))
                self.recorder.truncate_current_response(played_ms)

        self.transcript.add_event("barge-in", f"(patient cut off after {played_ms}ms)")
        self.last_assistant_item = None
        self.response_start_ts = None
        self.bot_is_speaking = False

    # -------------------------------------------------------------- watchdog

    async def _watchdog(self, openai_ws) -> None:
        """Guarantees the call ends: on goodbye, on silence, or on the clock."""
        nudged = False
        hangup_at: float | None = None

        while not self.finished.is_set():
            await asyncio.sleep(0.25)
            elapsed = time.monotonic() - self.started_at

            # Dead air usually means an IVR menu, hold music, or a slow pickup. Rather
            # than sit there burning the clock, prompt our side to say hello.
            if not self.heard_anything and not nudged and elapsed > DEAD_AIR_S:
                nudged = True
                self.transcript.add_event(f"no speech detected in {DEAD_AIR_S}s, prompting our side")
                await openai_ws.send(json.dumps({
                    "type": "response.create",
                    "response": {"instructions": "Say a short, natural 'Hello? Hi, can you hear me?'"},
                }))

            # The bot said goodbye. Give Twilio a bounded window to confirm playback.
            if self.pending_hangup:
                if hangup_at is None:
                    hangup_at = time.monotonic() + HANGUP_GRACE_S
                elif time.monotonic() > hangup_at:
                    self.transcript.add_event("goodbye mark never echoed, hanging up anyway")
                    return

            if elapsed > self.scenario.max_duration_s:
                self.transcript.add_event("max call duration reached, hanging up")
                return

        # finished was set by a goodbye mark or a Twilio stop. Let the line settle.
        await asyncio.sleep(0.3)

    # --------------------------------------------------------------- teardown

    def save(self) -> None:
        audio_path, _ = self.recorder.save()
        txt = self.transcript.save(audio_path.name, self.recorder.duration_s)
        mins, secs = divmod(int(self.recorder.duration_s), 60)
        turns = sum(1 for line in self.transcript.lines
                    if line["speaker"] in ("AGENT", "PATIENT"))
        agent_turns = sum(1 for line in self.transcript.lines if line["speaker"] == "AGENT")
        print(f"  saved {audio_path.name} + {txt.name}  ({mins}:{secs:02d}, {turns} turns)")
        if agent_turns == 0 and self.recorder.duration_s > 5:
            print("  ! no agent speech was transcribed — run: python analyze.py repair")
        if self.errors:
            print(f"  ! {len(self.errors)} API error(s) during this call")

    def hang_up(self) -> None:
        """Ask Twilio to end the call, in case the far end is still holding the line."""
        if not self.call_sid:
            return
        try:
            from twilio.rest import Client
            Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN) \
                .calls(self.call_sid).update(status="completed")
        except Exception as exc:  # the call may already be over; not worth failing on
            print(f"  (hangup no-op: {exc})")
