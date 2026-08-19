"""Collects a timestamped, two-sided transcript and writes it next to the audio.

Timestamps are seconds since the media stream opened, so a line in the transcript
points at the same moment in the MP3. The bug report cites those positions directly.
"""
import json
import time
from pathlib import Path


class Transcript:
    def __init__(self, path_stem: Path, scenario):
        self.path_stem = path_stem
        self.scenario = scenario
        self.t0 = time.monotonic()
        self.lines: list[dict] = []

    def _stamp(self) -> float:
        return round(time.monotonic() - self.t0, 2)

    def add(self, speaker: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        entry = {"t": self._stamp(), "speaker": speaker, "text": text}
        self.lines.append(entry)
        mmss = f"{int(entry['t']) // 60}:{int(entry['t']) % 60:02d}"
        print(f"  [{mmss}] {speaker}: {text}")

    def add_event(self, kind: str, detail: str = "") -> None:
        """Non-speech moments worth seeing in the transcript, e.g. barge-in."""
        self.lines.append({"t": self._stamp(), "speaker": "EVENT", "text": f"{kind} {detail}".strip()})

    def add_metric(self, kind: str, **fields) -> None:
        """Machine-only measurement. Lands in the .jsonl, stays out of the .txt.

        Response latency is measured for both sides: ours proves the bot cleared the
        brief's pacing bar, theirs is evidence for the bug report.
        """
        self.lines.append({"t": self._stamp(), "speaker": "METRIC", "kind": kind, **fields})

    def now(self) -> float:
        return self._stamp()

    def save(self, audio_name: str, duration_s: float) -> Path:
        txt_path = self.path_stem.with_suffix(".txt")
        header = [
            f"Call:        {self.path_stem.name}",
            f"Scenario:    {self.scenario.id} — {self.scenario.label}",
            f"Target:      Pretty Good AI test line (+1-805-439-8008)",
            f"Audio:       {audio_name}",
            f"Duration:    {int(duration_s) // 60}:{int(duration_s) % 60:02d}",
            "",
            "Goal:",
            "  " + self.scenario.goal.strip().replace("\n", "\n  "),
            "",
            "What to watch for:",
            *[f"  - {w}" for w in self.scenario.watch_for],
            "",
            "-" * 72,
            "",
        ]
        body = []
        for line in (l for l in self.lines if l["speaker"] != "METRIC"):
            mmss = f"{int(line['t']) // 60}:{int(line['t']) % 60:02d}"
            if line["speaker"] == "EVENT":
                body.append(f"[{mmss}] -- {line['text']} --")
            else:
                body.append(f"[{mmss}] {line['speaker']}: {line['text']}")

        txt_path.write_text("\n".join(header + body) + "\n")
        self.path_stem.with_suffix(".jsonl").write_text(
            "\n".join(json.dumps(line) for line in self.lines) + "\n"
        )
        return txt_path
