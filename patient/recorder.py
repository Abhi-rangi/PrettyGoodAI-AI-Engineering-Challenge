"""Records both sides of the call to a stereo file: left = agent, right = our bot.

We already have both audio directions passing through this process, so paying Twilio
for call recording would be redundant. Separating the two speakers onto their own
channels also makes the bug report much easier to write, because you can solo the
agent and hear exactly what it said without our patient talking over it.

Alignment approach: the inbound stream from Twilio is a continuous 8kHz feed and is
used as the master clock. Before appending outbound audio we pad the outbound channel
with silence up to the inbound length, so the two channels stay in sync without
needing timestamps on every frame.
"""
import subprocess
import wave
from pathlib import Path

from .audio import SILENCE, ulaw_to_pcm16

SAMPLE_RATE = 8000


class Recorder:
    def __init__(self, path_stem: Path):
        self.path_stem = path_stem
        self.inbound = bytearray()   # what the PGAI agent said
        self.outbound = bytearray()  # what our patient said
        self._response_start: int | None = None

    def add_inbound(self, payload: bytes) -> None:
        self.inbound.extend(payload)

    def add_outbound(self, payload: bytes) -> None:
        if len(self.outbound) < len(self.inbound):
            self.outbound.extend(bytes([SILENCE]) * (len(self.inbound) - len(self.outbound)))
        if self._response_start is None:
            self._response_start = len(self.outbound)
        self.outbound.extend(payload)

    def mark_response_end(self) -> None:
        self._response_start = None

    def truncate_current_response(self, played_ms: int) -> None:
        """Barge-in: drop the tail we queued but Twilio never actually played."""
        if self._response_start is None:
            return
        keep = self._response_start + int(played_ms * SAMPLE_RATE / 1000)
        if keep < len(self.outbound):
            del self.outbound[keep:]
        self._response_start = None

    def save(self) -> tuple[Path, Path | None]:
        """Write the stereo WAV, then transcode to MP3 if ffmpeg is available."""
        length = max(len(self.inbound), len(self.outbound))
        left = bytes(self.inbound).ljust(length, bytes([SILENCE]))
        right = bytes(self.outbound).ljust(length, bytes([SILENCE]))

        left_pcm = ulaw_to_pcm16(left)
        right_pcm = ulaw_to_pcm16(right)

        interleaved = bytearray(len(left_pcm) * 2)
        for i in range(0, len(left_pcm), 2):
            j = i * 2
            interleaved[j:j + 2] = left_pcm[i:i + 2]
            interleaved[j + 2:j + 4] = right_pcm[i:i + 2]

        wav_path = self.path_stem.with_suffix(".wav")
        with wave.open(str(wav_path), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(bytes(interleaved))

        mp3_path = self.path_stem.with_suffix(".mp3")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav_path),
                 "-codec:a", "libmp3lame", "-b:a", "64k", str(mp3_path)],
                check=True,
            )
            wav_path.unlink()
            return mp3_path, mp3_path
        except (FileNotFoundError, subprocess.CalledProcessError):
            print("  ! ffmpeg not found or failed; keeping WAV. `brew install ffmpeg`")
            return wav_path, None

    @property
    def duration_s(self) -> float:
        return max(len(self.inbound), len(self.outbound)) / SAMPLE_RATE
