"""Records both sides of the call to a stereo file: left = caller, right = receptionist.

Only used when STORE_CALLS is on. Call audio is PHI; see the README before enabling it.

Alignment: the inbound stream from Twilio is a continuous 8kHz feed and is the master
clock. Before appending our audio, the outbound channel is padded with silence up to the
inbound length, so the two channels stay in sync without per-frame timestamps.
"""
import subprocess
import wave
from pathlib import Path

from .audio import SILENCE, interleave_stereo, ulaw_to_pcm16
from .turns import SAMPLE_RATE


class Recorder:
    def __init__(self, path_stem: Path):
        self.path_stem = path_stem
        self.inbound = bytearray()   # the caller
        self.outbound = bytearray()  # the receptionist
        self._response_start: int | None = None

    def add_inbound(self, payload: bytes) -> None:
        self.inbound.extend(payload)

    def add_outbound(self, payload: bytes, new_response: bool) -> None:
        if len(self.outbound) < len(self.inbound):
            self.outbound.extend(bytes([SILENCE]) * (len(self.inbound) - len(self.outbound)))
        if new_response or self._response_start is None:
            self._response_start = len(self.outbound)
        self.outbound.extend(payload)

    def truncate_current_response(self, played_ms: int) -> None:
        """Barge-in: drop the tail we queued but Twilio never played.

        The start offset is kept until the next response begins, not cleared when
        generation ends, so an interruption during the buffered tail still trims.
        """
        if self._response_start is None:
            return
        keep = self._response_start + played_ms * SAMPLE_RATE // 1000
        del self.outbound[keep:]
        self._response_start = None

    @property
    def duration_s(self) -> float:
        return max(len(self.inbound), len(self.outbound)) / SAMPLE_RATE

    def save(self) -> Path:
        """Write the stereo WAV, then transcode to MP3 if ffmpeg is available. Blocking."""
        self.path_stem.parent.mkdir(parents=True, exist_ok=True)
        length = max(len(self.inbound), len(self.outbound))
        left = ulaw_to_pcm16(bytes(self.inbound).ljust(length, bytes([SILENCE])))
        right = ulaw_to_pcm16(bytes(self.outbound).ljust(length, bytes([SILENCE])))

        wav_path = self.path_stem.with_suffix(".wav")
        with wave.open(str(wav_path), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(interleave_stereo(left, right))

        mp3_path = self.path_stem.with_suffix(".mp3")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav_path),
                 "-codec:a", "libmp3lame", "-b:a", "64k", str(mp3_path)],
                check=True,
            )
            wav_path.unlink()
            return mp3_path
        except (FileNotFoundError, subprocess.CalledProcessError):
            print("  ! ffmpeg not found or failed; keeping WAV")
            return wav_path
