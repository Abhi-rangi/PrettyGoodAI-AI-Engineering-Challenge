"""Settings, read from the environment once and passed around explicitly.

Nothing here runs at import time beyond reading `.env`, so tests can build a
`Settings` by hand without touching the filesystem or the network.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    openai_api_key: str = ""
    realtime_model: str = "gpt-realtime-2.1-mini"
    transcribe_model: str = "whisper-1"
    voice: str = "marin"

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    # The clinic number patients call. Only used by preflight to check its webhook.
    twilio_number: str = ""
    # Host only, no scheme: Twilio reaches us at https://<host>/voice and wss://<host>/media-stream.
    public_host: str = ""
    validate_twilio_signature: bool = True

    clinic_name: str = "RD Physical Therapy & Wellness"
    clinic_timezone: str = "America/New_York"

    # Intake plus booking runs longer than a scripted test call. This is a backstop,
    # not a target.
    max_call_s: int = 600
    # Server VAD. Callers pause between letters and digit groups when spelling a name
    # or reading a number, and 900ms ended their turn mid-spelling.
    vad_silence_ms: int = 1100
    vad_threshold: float = 0.55

    # Call audio and transcripts are PHI. Both are off unless explicitly enabled, and
    # when enabled they go to a directory outside version control.
    store_calls: bool = False
    log_transcripts: bool = False
    call_dir: Path = field(default_factory=lambda: ROOT / "var" / "calls")
    debug_events: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(ROOT / ".env")
        host = os.getenv("PUBLIC_HOST", "")
        host = host.replace("https://", "").replace("http://", "").rstrip("/")
        return cls(
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            realtime_model=os.getenv("OPENAI_REALTIME_MODEL", cls.realtime_model),
            transcribe_model=os.getenv("OPENAI_TRANSCRIBE_MODEL", cls.transcribe_model),
            voice=os.getenv("VOICE", cls.voice),
            twilio_account_sid=os.getenv("TWILIO_ACCOUNT_SID", ""),
            twilio_auth_token=os.getenv("TWILIO_AUTH_TOKEN", ""),
            twilio_number=os.getenv("TWILIO_NUMBER", "").strip(),
            public_host=host,
            validate_twilio_signature=_flag("VALIDATE_TWILIO_SIGNATURE", True),
            clinic_name=os.getenv("CLINIC_NAME", cls.clinic_name),
            clinic_timezone=os.getenv("CLINIC_TIMEZONE", cls.clinic_timezone),
            max_call_s=int(os.getenv("MAX_CALL_S", cls.max_call_s)),
            vad_silence_ms=int(os.getenv("VAD_SILENCE_MS", cls.vad_silence_ms)),
            vad_threshold=float(os.getenv("VAD_THRESHOLD", cls.vad_threshold)),
            store_calls=_flag("STORE_CALLS", False),
            log_transcripts=_flag("LOG_TRANSCRIPTS", False),
            call_dir=Path(os.getenv("CALL_DIR", str(ROOT / "var" / "calls"))),
            debug_events=_flag("REALTIME_DEBUG", False),
        )

    def require(self, *names: str) -> None:
        """Fail fast with a readable message instead of a confusing 401 later."""
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            raise SystemExit(
                "Missing required settings: " + ", ".join(n.upper() for n in missing)
                + "\nCopy .env.example to .env and fill them in."
            )
