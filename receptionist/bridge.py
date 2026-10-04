"""The relay: Twilio Media Streams <-> OpenAI Realtime API, for one inbound call.

Both legs speak G.711 mu-law at 8kHz, so audio frames pass through as base64 with the
envelope swapped. Three tasks run per call: a pump in each direction and a watchdog
that guarantees the call ends.

What lives elsewhere: turn and playback state in `turns.py`, tool validation and the
clinic backend in `tools.py` / `backend.py`, the prompt in `prompt.py`. This file is
transport and wiring only.
"""
import asyncio
import base64
import json
import statistics
import time
from dataclasses import dataclass
from datetime import datetime

from fastapi import WebSocket, WebSocketDisconnect

from . import prompt, realtime
from .backend import ClinicBackend, phone_digits
from .config import Settings
from .recorder import Recorder
from .tools import CallContext, ToolRouter
from .transcript import CALLER, RECEPTIONIST, Transcript
from .turns import TurnTracker

# Once the receptionist has said goodbye, wait this long for Twilio to confirm it
# played before hanging up regardless.
HANGUP_GRACE_S = 8
# Silence on the line (nobody talking, nothing playing, no tool running) before we ask
# "are you still there?", and again before giving up.
IDLE_S = 20
# How long to collect transcripts still in flight when the call ends.
DRAIN_S = 3.0
# Errors the API raises in normal operation that need no attention.
BENIGN_ERRORS = ("already been truncated", "already truncated", "buffer too small",
                 "no active response")


@dataclass(frozen=True)
class StreamStart:
    stream_sid: str
    call_sid: str
    caller_number: str | None
    params: dict


async def read_stream_start(ws: WebSocket, timeout: float = 10) -> StreamStart | None:
    """Wait for Twilio's `start` frame, which carries the call identity and our custom
    parameters. Done before any OpenAI connection is opened, so an unauthenticated
    socket costs nothing."""
    async def _read() -> StreamStart | None:
        async for raw in ws.iter_text():
            msg = json.loads(raw)
            if msg.get("event") == "start":
                start = msg["start"]
                params = start.get("customParameters") or {}
                return StreamStart(start["streamSid"], start["callSid"],
                                   phone_digits(params.get("caller", "")), params)
        return None
    try:
        return await asyncio.wait_for(_read(), timeout)
    except (asyncio.TimeoutError, WebSocketDisconnect, KeyError, ValueError):
        return None


class CallBridge:
    def __init__(self, twilio_ws: WebSocket, settings: Settings, backend: ClinicBackend,
                 start: StreamStart):
        self.twilio_ws = twilio_ws
        self.settings = settings
        self.stream_sid, self.call_sid = start.stream_sid, start.call_sid
        self.label = f"{datetime.now():%Y%m%d-%H%M%S}-{start.call_sid[-8:]}"

        self.ctx = CallContext(caller_number=start.caller_number,
                               timezone=settings.clinic_timezone)
        self.tools = ToolRouter(backend, self.ctx)
        self.turns = TurnTracker()
        self.transcript = Transcript(echo=settings.log_transcripts)
        self.recorder = Recorder(settings.call_dir / self.label) if settings.store_calls else None

        self.finished = asyncio.Event()
        self.pending_hangup = False
        self.errors: list[str] = []
        self.started_at = time.monotonic()
        self._last_activity = time.monotonic()
        self._tool_tasks: list[asyncio.Task] = []
        self._tools_running = 0
        self._background: set[asyncio.Task] = set()

    # ------------------------------------------------------------------- run

    async def run(self) -> None:
        async with realtime.connect(self.settings) as oai:
            instructions = prompt.render(self.settings.clinic_name,
                                         self.settings.clinic_timezone,
                                         self.ctx.caller_number)
            await oai.send(json.dumps(realtime.session_update(self.settings, instructions)))
            # The receptionist speaks first. Nothing else would make it: server VAD
            # only creates a response after the caller has said something.
            opening = prompt.opening_line(self.settings.clinic_name)
            await oai.send(json.dumps({"type": "response.create", "response": {
                "instructions": f'Greet the caller by saying exactly: "{opening}"'}}))

            tasks = [
                asyncio.create_task(self._pump_twilio_to_openai(oai)),
                asyncio.create_task(self._pump_openai_to_twilio(oai)),
                asyncio.create_task(self._watchdog(oai)),
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            # Let the cancellations land before reading the socket again: a pump still
            # parked in recv() would make the drain's recv() fail and lose transcripts.
            await asyncio.gather(*pending, *self._background, return_exceptions=True)
            await self._drain_transcripts(oai)
            for task in done:
                if not task.cancelled() and task.exception():
                    raise task.exception()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    # ------------------------------------------------------- twilio -> openai

    async def _pump_twilio_to_openai(self, oai) -> None:
        try:
            async for raw in self.twilio_ws.iter_text():
                msg = json.loads(raw)
                event = msg.get("event")

                if event == "media":
                    self.turns.latest_media_ts = int(msg["media"]["timestamp"])
                    payload = msg["media"]["payload"]
                    if self.recorder:
                        self.recorder.add_inbound(base64.b64decode(payload))
                    await oai.send(json.dumps({"type": "input_audio_buffer.append",
                                               "audio": payload}))

                elif event == "mark":
                    name = msg.get("mark", {}).get("name", "")
                    if name == "goodbye":
                        # Everything queued before it has played: the caller heard goodbye.
                        self.finished.set()
                        return
                    if self.turns.on_mark(name, self.transcript.now()):
                        self._last_activity = time.monotonic()

                elif event == "stop":
                    print("  caller hung up")
                    self.finished.set()
                    return
        except WebSocketDisconnect:
            print("  Twilio disconnected")
            self.finished.set()

    # ------------------------------------------------------- openai -> twilio

    async def _pump_openai_to_twilio(self, oai) -> None:
        async for raw in oai:
            event = json.loads(raw)
            etype = event.get("type", "")
            if self.settings.debug_events and not etype.endswith(".delta"):
                print(f"    · {etype}")

            if self._handle_transcript_event(etype, event):
                continue
            if etype == "error":
                self._log_error(event)
            elif etype == "response.output_audio.delta":
                await self._on_audio_delta(event)
            elif etype == "response.output_audio.done":
                mark = self.turns.on_audio_done(event.get("response_id", ""))
                if mark:
                    await self._send_mark(mark)
            elif etype == "input_audio_buffer.speech_started":
                await self._on_caller_speech_started(oai)
            elif etype == "input_audio_buffer.speech_stopped":
                self.turns.on_caller_speech_stopped(self.transcript.now())
                self._last_activity = time.monotonic()
            elif etype == "response.function_call_arguments.done":
                self._on_function_call(oai, event)
            elif etype == "response.done":
                await self._on_response_done(oai, event)

    def _handle_transcript_event(self, etype: str, event: dict) -> bool:
        if etype == "conversation.item.input_audio_transcription.completed":
            self.transcript.add(CALLER, event.get("transcript", ""))
        elif etype == "response.output_audio_transcript.done":
            self.transcript.add(RECEPTIONIST, event.get("transcript", ""))
        else:
            return False
        return True

    async def _on_audio_delta(self, event: dict) -> None:
        b64 = event["delta"]
        audio = base64.b64decode(b64)
        result = self.turns.on_audio_delta(event.get("response_id", ""), event.get("item_id"),
                                           len(audio), self.transcript.now())
        if result.new_response:
            self.transcript.turn_started(RECEPTIONIST)
        if result.reply_latency_ms is not None:
            self.transcript.metric("latency", who="receptionist", ms=result.reply_latency_ms)
        if not result.send:
            return
        if self.recorder:
            self.recorder.add_outbound(audio, result.new_response)
        await self.twilio_ws.send_text(json.dumps({
            "event": "media", "streamSid": self.stream_sid, "media": {"payload": b64}}))

    async def _on_caller_speech_started(self, oai) -> None:
        """The caller started talking. If we were still audible, stop at once.

        Three things, all audible if skipped: flush Twilio's playout buffer, truncate the
        model's item so it does not believe the caller heard words they did not, and
        trim the recording to match. The flush applies whenever playback is still
        running, including the tail Twilio is playing after generation has finished,
        which is the most common moment to be interrupted.
        """
        self._last_activity = time.monotonic()
        was_speaking = self.turns.caller_speaking
        latency, barge = self.turns.on_caller_speech_started(self.transcript.now())
        if not was_speaking:
            self.transcript.turn_started(CALLER)
        if latency is not None:
            self.transcript.metric("latency", who="caller", ms=latency)
        if barge is None:
            return

        await self.twilio_ws.send_text(json.dumps({"event": "clear",
                                                   "streamSid": self.stream_sid}))
        if barge.item_id and barge.played_ms > 0:
            await oai.send(json.dumps({"type": "conversation.item.truncate",
                                       "item_id": barge.item_id, "content_index": 0,
                                       "audio_end_ms": barge.played_ms}))
        if self.recorder:
            self.recorder.truncate_current_response(barge.played_ms)
        self.transcript.event(f"barge-in after {barge.played_ms}ms")

    # ----------------------------------------------------------------- tools

    def _on_function_call(self, oai, event: dict) -> None:
        name = event.get("name", "")
        if name == "end_call":
            self.pending_hangup = True
            self.transcript.event("receptionist ended the call")
            return
        self._tool_tasks.append(
            self._spawn(self._run_tool(oai, event.get("call_id", ""), name,
                                       event.get("arguments", "{}"))))

    async def _run_tool(self, oai, call_id: str, name: str, arguments: str) -> None:
        self._tools_running += 1
        try:
            result = await self.tools.call(name, arguments)
            # Log the outcome, not the arguments: those are the caller's details.
            status = "ok" if result.get("ok") else result.get("error", "error")
            self.transcript.event(f"tool {name}: {status}")
            await oai.send(json.dumps({"type": "conversation.item.create", "item": {
                "type": "function_call_output", "call_id": call_id,
                "output": json.dumps(result)}}))
        finally:
            self._tools_running -= 1
            self._last_activity = time.monotonic()

    async def _on_response_done(self, oai, event: dict) -> None:
        tool_tasks, self._tool_tasks = self._tool_tasks, []
        cancelled = (event.get("response") or {}).get("status") == "cancelled"
        if tool_tasks:
            self._spawn(self._continue_after_tools(oai, tool_tasks, cancelled))
        elif self.pending_hangup:
            # Queues behind all audio already sent; Twilio echoes it once played.
            await self._send_mark("goodbye")

    async def _continue_after_tools(self, oai, tasks: list[asyncio.Task],
                                    cancelled: bool) -> None:
        """Results are in the conversation; ask the model to speak about them.

        Not done when the response was cancelled by the caller talking: server VAD has
        already started a new response, which will see the results, and a second
        response.create would be rejected as a concurrent response.
        """
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.pending_hangup:
            await self._send_mark("goodbye")
        elif not cancelled:
            await oai.send(json.dumps({"type": "response.create"}))

    # ----------------------------------------------------------------- utils

    async def _send_mark(self, name: str) -> None:
        await self.twilio_ws.send_text(json.dumps({
            "event": "mark", "streamSid": self.stream_sid, "mark": {"name": name}}))

    def _log_error(self, event: dict) -> None:
        message = (event.get("error") or {}).get("message", str(event))
        if any(s in message.lower() for s in BENIGN_ERRORS):
            return
        self.errors.append(message)
        print(f"  ! openai error: {message}")

    async def _drain_transcripts(self, oai) -> None:
        """Collect transcripts still in flight when the call ended.

        Transcription finishes well after the audio it describes, so closing the socket
        at once silently dropped the last few turns.
        """
        try:
            if self.turns.caller_speaking:
                # VAD never saw the caller stop, so nothing was committed to transcribe.
                await oai.send(json.dumps({"type": "input_audio_buffer.commit"}))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + DRAIN_S
            while (remaining := deadline - loop.time()) > 0:
                event = json.loads(await asyncio.wait_for(oai.recv(), timeout=remaining))
                self._handle_transcript_event(event.get("type", ""), event)
        except Exception:
            pass  # socket closed or deadline hit; what we have is what we keep

    # -------------------------------------------------------------- watchdog

    async def _watchdog(self, oai) -> None:
        """Guarantees the call ends: on goodbye, on prolonged silence, or on the clock."""
        hangup_at: float | None = None
        nudged = False

        while not self.finished.is_set():
            await asyncio.sleep(0.25)
            now = time.monotonic()

            if self.pending_hangup:
                hangup_at = hangup_at or now + HANGUP_GRACE_S
                if now > hangup_at:
                    self.transcript.event("goodbye mark never echoed, hanging up")
                    return

            if now - self.started_at > self.settings.max_call_s:
                self.transcript.event("max call duration reached, hanging up")
                return

            idle = (not self.turns.playing and not self.turns.caller_speaking
                    and self._tools_running == 0 and not self.pending_hangup)
            if not idle:
                continue
            if now - self._last_activity > IDLE_S:
                if nudged:
                    self.transcript.event("caller silent, hanging up")
                    return
                nudged = True
                self._last_activity = now
                await oai.send(json.dumps({"type": "response.create", "response": {
                    "instructions": "Briefly ask whether the caller is still there."}}))

        await asyncio.sleep(0.3)  # let the line settle

    # --------------------------------------------------------------- teardown

    async def finish(self) -> None:
        """Hang up, persist if enabled, and print a summary with no caller details."""
        await asyncio.to_thread(self._hang_up)
        duration = time.monotonic() - self.started_at
        if self.recorder:
            audio = await asyncio.to_thread(self.recorder.save)
            txt = self.transcript.save(self.settings.call_dir / self.label, [
                f"Call:      {self.label}", f"Twilio:    {self.call_sid}",
                f"Audio:     {audio.name}", f"Duration:  {int(duration)}s", ""])
            print(f"  saved {txt.parent / self.label}.*")

        latencies = [l["ms"] for l in self.transcript.lines
                     if l.get("kind") == "latency" and l.get("who") == "receptionist"]
        median = f"{statistics.median(latencies):.0f}ms" if latencies else "n/a"
        print(f"  {int(duration)}s, {len(self.transcript.turns())} turns, "
              f"reply latency median {median}, "
              f"patient identified: {'yes' if self.ctx.patient_id else 'no'}")
        if self.errors:
            print(f"  ! {len(self.errors)} API error(s) during this call")

    def _hang_up(self) -> None:
        """End the call at Twilio in case the caller is still holding the line."""
        if not (self.settings.twilio_account_sid and self.settings.twilio_auth_token):
            return
        try:
            from twilio.rest import Client
            Client(self.settings.twilio_account_sid, self.settings.twilio_auth_token) \
                .calls(self.call_sid).update(status="completed")
        except Exception as exc:  # usually already over
            print(f"  (hangup no-op: {type(exc).__name__})")
