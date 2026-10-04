"""OpenAI Realtime API: connection and session configuration.

Kept apart from the bridge so preflight.py can validate the exact session payload the
bridge sends without a phone call.
"""
from .config import Settings
from .tools import TOOL_SCHEMAS

try:  # websockets >= 14
    from websockets.asyncio.client import connect as _connect
    _HEADER_KW = "additional_headers"
except ImportError:  # older releases
    from websockets.client import connect as _connect  # type: ignore
    _HEADER_KW = "extra_headers"


def realtime_url(settings: Settings) -> str:
    return f"wss://api.openai.com/v1/realtime?model={settings.realtime_model}"


def connect(settings: Settings):
    headers = {"Authorization": f"Bearer {settings.openai_api_key}"}
    return _connect(realtime_url(settings), **{_HEADER_KW: headers})


def session_update(settings: Settings, instructions: str) -> dict:
    """GA Realtime session shape (nested `audio.input.format`, not the beta flat
    `input_audio_format`, which the GA endpoint rejects).

    Both legs are G.711 mu-law at 8kHz, the same as Twilio, so audio passes through
    without transcoding. noise_reduction is near_field: one voice over a narrowband
    phone codec is what it is tuned for.
    """
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "instructions": instructions,
            "output_modalities": ["audio"],
            "tools": TOOL_SCHEMAS,
            "tool_choice": "auto",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": settings.vad_threshold,
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": settings.vad_silence_ms,
                        "create_response": True,
                        "interrupt_response": True,
                    },
                    "transcription": {"model": settings.transcribe_model},
                    "noise_reduction": {"type": "near_field"},
                },
                "output": {
                    "format": {"type": "audio/pcmu"},
                    "voice": settings.voice,
                },
            },
        },
    }
