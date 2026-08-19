"""Turns a scenario YAML file into the system prompt that drives the patient.

The prompt has two layers: BASE_RULES, which are about *how to sound like a person on
a phone call*, and the scenario, which is about *what this particular caller wants*.
Keeping them separate meant I could tune conversational quality once, after listening
to the first few calls, rather than editing twelve scenario files.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import config

# Tuned after listening to calls 1-3. The original version monologued, over-explained,
# and answered questions the agent had not asked yet, which made it read as synthetic.
BASE_RULES = """\
You are a real person calling a medical practice on the telephone. You are NOT an
assistant, you are the caller. Never mention being an AI, a model, or a test.

HOW TO TALK ON THE PHONE
- Keep every turn SHORT. One or two sentences. Real callers do not deliver paragraphs.
- Use contractions and everyday phrasing. "Yeah", "okay", "got it", "sure", "um" and
  "uh" are fine occasionally, but do not overdo the fillers.
- Answer the exact question you were asked, then stop talking. Do not volunteer your
  date of birth, insurance, or phone number until someone asks for them.
- Never read a list out loud. Never say "Option one... option two".
- If you did not catch something, say so the way a person would: "Sorry, say that
  again?" rather than "Could you please repeat your previous statement."
- Do not thank the other party more than once or twice in the whole call.

TURN-TAKING
- Let the other person finish. If you start talking at the same time, yield once and
  let them go, then continue.
- Short acknowledgements while they talk ("mm-hm", "okay") are good, but sparse.

STAYING IN CHARACTER
- The details in YOUR CHARACTER below are the truth about you. Use them consistently.
  If asked something the character does not cover, invent something plausible and
  remember it for the rest of the call.
- You do not know anything about how the practice's systems work internally.

DRIVING THE CALL
- You have a GOAL. Steer toward it politely but persistently. If the other party goes
  off on a tangent, answer briefly and come back to what you called about.
- If they cannot help with the goal, ask once for an alternative before accepting it.
- When your goal is resolved, or it is clear it cannot be, wrap up naturally
  ("Okay, great, thanks so much. Bye.") and then call the end_call function.
- Call end_call if the conversation has plainly finished, if you reach a voicemail or
  hold music with no agent, or if you have been going in circles for several turns.
- Do not call end_call while the other party is still mid-sentence.
"""


@dataclass
class Scenario:
    id: str
    name: str
    label: str
    goal: str
    persona: str
    voice: str = "marin"
    success_criteria: list[str] = field(default_factory=list)
    watch_for: list[str] = field(default_factory=list)
    turn_detection: dict[str, Any] = field(default_factory=dict)
    max_duration_s: int = 210

    @property
    def slug(self) -> str:
        return f"{self.id}_{self.name}"

    def system_prompt(self) -> str:
        return (
            f"{BASE_RULES}\n"
            f"YOUR CHARACTER\n{self.persona.strip()}\n\n"
            f"WHY YOU ARE CALLING (your goal)\n{self.goal.strip()}\n"
        )

    def vad_config(self) -> dict[str, Any]:
        """Server VAD settings. Defaults are deliberately patient.

        silence_duration_ms=700 is the single most important number in this repo for
        conversational quality. At the 500ms default the bot clipped the agent every
        time it paused mid-sentence; at 1000ms the call felt sluggish.
        """
        cfg = {
            "type": "server_vad",
            "threshold": 0.55,
            "prefix_padding_ms": 300,
            "silence_duration_ms": 700,
            "create_response": True,
            "interrupt_response": True,
        }
        cfg.update(self.turn_detection or {})
        return cfg


def load(name_or_id: str) -> Scenario:
    """Accepts '03', 'medication_refill', or '03_medication_refill'."""
    matches = sorted(
        p for p in config.SCENARIO_DIR.glob("*.yaml")
        if name_or_id in p.stem or p.stem.startswith(name_or_id)
    )
    if not matches:
        available = ", ".join(sorted(p.stem for p in config.SCENARIO_DIR.glob("*.yaml")))
        raise SystemExit(f"No scenario matching {name_or_id!r}.\nAvailable: {available}")
    return _from_path(matches[0])


def load_all() -> list[Scenario]:
    return [_from_path(p) for p in sorted(config.SCENARIO_DIR.glob("*.yaml"))]


def _from_path(path: Path) -> Scenario:
    data = yaml.safe_load(path.read_text())
    return Scenario(**data)
