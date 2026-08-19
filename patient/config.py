"""Environment-backed settings, loaded once at import."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1-mini")
# Used both for live input transcription and for post-hoc transcript repair.
TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "whisper-1")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "")
TARGET_NUMBER = os.getenv("TARGET_NUMBER", "+18054398008")

PUBLIC_HOST = os.getenv("PUBLIC_HOST", "").replace("https://", "").replace("http://", "").rstrip("/")
PORT = int(os.getenv("PORT", "5050"))

SCENARIO_DIR = ROOT / "scenarios"
CALL_DIR = ROOT / "calls"
CALL_DIR.mkdir(exist_ok=True)


def require(*names: str) -> None:
    """Fail fast with a readable message instead of a confusing 401 later."""
    missing = [n for n in names if not globals().get(n)]
    if missing:
        raise SystemExit(
            "Missing required settings: "
            + ", ".join(missing)
            + "\nCopy .env.example to .env and fill them in."
        )
