"""Timestamped two-sided transcript of a call.

Always kept in memory (the bridge uses it for its end-of-call summary); written to disk
only when STORE_CALLS is on, and echoed to stdout only when LOG_TRANSCRIPTS is on, since
both are PHI.

Timestamps are seconds since the media stream opened, so a line points at the same
moment in the recording.
"""
import json
import time
from collections import deque
from pathlib import Path

CALLER, RECEPTIONIST = "CALLER", "RECEPTIONIST"


def mmss(seconds: float) -> str:
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


class Transcript:
    def __init__(self, echo: bool = False):
        self.echo = echo
        self.t0 = time.monotonic()
        self.lines: list[dict] = []
        # When each side began talking. Queues, not single slots: transcription lands
        # well after the audio, so two or three turns can be outstanding at once and a
        # single variable would stamp them all with the most recent start time.
        self._starts: dict[str, deque[float]] = {CALLER: deque(), RECEPTIONIST: deque()}

    def now(self) -> float:
        return round(time.monotonic() - self.t0, 2)

    def turn_started(self, speaker: str) -> None:
        self._starts[speaker].append(self.now())

    def add(self, speaker: str, text: str) -> None:
        """Stamped with when the turn was *spoken*, not when its transcript arrived."""
        queue = self._starts[speaker]
        t = queue.popleft() if queue else self.now()
        text = (text or "").strip()
        if not text:
            return
        self.lines.append({"t": t, "speaker": speaker, "text": text})
        if self.echo:
            print(f"  [{mmss(t)}] {speaker}: {text}")

    def event(self, text: str) -> None:
        """Non-speech moments: barge-in, tool calls, hangup reason. No PHI in these."""
        self.lines.append({"t": self.now(), "speaker": "EVENT", "text": text})
        print(f"  [{mmss(self.now())}] -- {text}")

    def metric(self, kind: str, **fields) -> None:
        """Machine-only measurement. Lands in the .jsonl, stays out of the .txt."""
        self.lines.append({"t": self.now(), "speaker": "METRIC", "kind": kind, **fields})

    def turns(self, speaker: str | None = None) -> list[dict]:
        return [l for l in self.lines if l["speaker"] in (CALLER, RECEPTIONIST)
                and (speaker is None or l["speaker"] == speaker)]

    def save(self, path_stem: Path, header: list[str]) -> Path:
        path_stem.parent.mkdir(parents=True, exist_ok=True)
        self.lines.sort(key=lambda l: l["t"])
        body = []
        for line in self.lines:
            if line["speaker"] == "METRIC":
                continue
            if line["speaker"] == "EVENT":
                body.append(f"[{mmss(line['t'])}] -- {line['text']} --")
            else:
                body.append(f"[{mmss(line['t'])}] {line['speaker']}: {line['text']}")
        txt = path_stem.with_suffix(".txt")
        txt.write_text("\n".join(header + ["-" * 72, ""] + body) + "\n")
        path_stem.with_suffix(".jsonl").write_text(
            "\n".join(json.dumps(l) for l in self.lines) + "\n")
        return txt
