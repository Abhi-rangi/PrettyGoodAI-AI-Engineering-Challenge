"""Who is talking, what is still playing, and how long each side took to reply.

Pure state, no I/O: the bridge feeds events in and acts on what comes back. Kept
separate because this logic is where the bugs were, and in this form it can be tested
with plain event sequences instead of live phone calls.

The key distinction is *generated* versus *played*. The Realtime API produces audio
faster than real time, so "the model finished" arrives seconds before the caller has
heard the end of it. The previous bridge treated the first as the second, which broke
three things at once: a barge-in near the end of a turn did not flush Twilio's buffer,
the recording kept audio the caller never heard, and the caller's reply latency was
clocked from the end of generation instead of the end of playback.

Playback is now tracked with Twilio marks. After each response's audio we send a mark;
Twilio echoes it once everything queued before it has played. Until that echo, the
receptionist is still audibly speaking.
"""
from dataclasses import dataclass, field

SAMPLE_RATE = 8000  # G.711 mu-law: one byte per sample


@dataclass(frozen=True)
class BargeIn:
    """The caller started talking over us. The bridge must flush Twilio, and truncate
    the model's item (if `item_id`) and the recording to `played_ms`."""
    item_id: str | None
    played_ms: int


@dataclass(frozen=True)
class DeltaResult:
    send: bool                 # forward this audio to Twilio
    new_response: bool         # first audio of a new response
    reply_latency_ms: int | None = None  # ours: caller stopped -> our first audio


@dataclass
class TurnTracker:
    latest_media_ts: int = 0   # ms, Twilio's inbound frame clock

    # Our side.
    _response_id: str | None = None
    _item_id: str | None = None
    _start_media_ts: int | None = None
    _generated_bytes: int = 0
    _generating: bool = False
    _outstanding_marks: set[str] = field(default_factory=set)
    _dropped_responses: set[str] = field(default_factory=set)
    _mark_seq: int = 0

    # Their side.
    caller_speaking: bool = False
    _caller_stopped_at: float | None = None
    _playback_finished_at: float | None = None

    @property
    def playing(self) -> bool:
        """True from our first audio frame until Twilio confirms the last one played."""
        return self._generating or bool(self._outstanding_marks)

    # ---------------------------------------------------------------- our audio

    def on_audio_delta(self, response_id: str, item_id: str | None, nbytes: int,
                       now: float) -> DeltaResult:
        if response_id in self._dropped_responses:
            # A cancelled response can still emit a frame or two after the caller cut it
            # off. Sending them would undo the Twilio clear.
            return DeltaResult(send=False, new_response=False)
        new = response_id != self._response_id
        latency = None
        if new:
            self._response_id, self._item_id = response_id, item_id
            self._start_media_ts = self.latest_media_ts
            self._generated_bytes = 0
            if self._caller_stopped_at is not None:
                latency = round((now - self._caller_stopped_at) * 1000)
                self._caller_stopped_at = None
        self._generating = True
        self._generated_bytes += nbytes
        return DeltaResult(send=True, new_response=new, reply_latency_ms=latency)

    def on_audio_done(self, response_id: str) -> str | None:
        """Generation for this response ended. Returns the mark name to send to Twilio,
        or None if the caller already cut this response off."""
        if response_id in self._dropped_responses or response_id != self._response_id:
            return None
        self._generating = False
        self._mark_seq += 1
        name = f"play-{self._mark_seq}"
        self._outstanding_marks.add(name)
        return name

    def on_mark(self, name: str, now: float) -> bool:
        """Twilio played everything up to `name`. Returns True if we are now silent."""
        if name not in self._outstanding_marks:
            return False  # echoes of marks we already gave up on after a barge-in
        self._outstanding_marks.discard(name)
        if self.playing:
            return False
        if not self.caller_speaking:
            # Only clock their reply if the line actually went quiet. If they were
            # already talking when we finished, there is no reply delay to measure.
            self._playback_finished_at = now
        return True

    # ------------------------------------------------------------- their speech

    def on_caller_speech_started(self, now: float) -> tuple[int | None, BargeIn | None]:
        """Returns (their reply latency in ms, barge-in to perform)."""
        latency = None
        if self._playback_finished_at is not None:
            latency = round((now - self._playback_finished_at) * 1000)
            self._playback_finished_at = None
        self.caller_speaking = True
        if not self.playing:
            return latency, None

        played_ms = 0
        if self._start_media_ts is not None:
            generated_ms = self._generated_bytes * 1000 // SAMPLE_RATE
            played_ms = min(max(0, self.latest_media_ts - self._start_media_ts), generated_ms)
        barge = BargeIn(item_id=self._item_id, played_ms=played_ms)

        if self._generating and self._response_id:
            self._dropped_responses.add(self._response_id)
        self._generating = False
        self._outstanding_marks.clear()
        self._item_id = None
        self._start_media_ts = None
        return latency, barge

    def on_caller_speech_stopped(self, now: float) -> None:
        self.caller_speaking = False
        self._caller_stopped_at = now
