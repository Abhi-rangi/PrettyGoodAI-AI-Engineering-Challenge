import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from twilio.request_validator import RequestValidator

from receptionist import prompt
from receptionist.backend import InMemoryBackend
from receptionist.config import Settings
from receptionist.recorder import Recorder
from server import create_app

SETTINGS = Settings(openai_api_key="sk-test", twilio_auth_token="secret",
                    public_host="clinic.example.com")
FORM = {"From": "+16095550142", "CallSid": "CA123"}


def client(settings=SETTINGS):
    return TestClient(create_app(settings, InMemoryBackend()))


def test_voice_rejects_unsigned_requests():
    assert client().post("/voice", data=FORM).status_code == 403


def test_voice_returns_stream_twiml_for_signed_requests():
    sig = RequestValidator("secret").compute_signature("https://clinic.example.com/voice", FORM)
    resp = client().post("/voice", data=FORM, headers={"X-Twilio-Signature": sig})
    assert resp.status_code == 200
    assert 'url="wss://clinic.example.com/media-stream"' in resp.text
    assert '<Parameter name="caller" value="+16095550142" />' in resp.text
    assert '<Parameter name="token" value="' in resp.text


def test_media_stream_rejects_missing_token():
    with client().websocket_connect("/media-stream") as ws:
        ws.send_text(json.dumps({"event": "connected"}))
        ws.send_text(json.dumps({"event": "start", "start": {
            "streamSid": "MZ1", "callSid": "CA1", "customParameters": {"token": "forged"}}}))
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
        assert exc.value.code == 1008


def test_prompt_carries_date_and_caller_id():
    now = datetime(2026, 10, 5, 9, 0, tzinfo=ZoneInfo("America/New_York"))
    text = prompt.render("RD PT", "America/New_York", "6095550142", now=now)
    assert "Monday, October 5, 2026 (2026-10-05)" in text
    assert "609-555-0142" in text
    assert "Caller ID is not available" in prompt.render("RD PT", "America/New_York", None, now)


def test_recorder_trims_after_generation_finished(tmp_path):
    rec = Recorder(tmp_path / "call")
    rec.add_inbound(b"\x7f" * 8000)
    rec.add_outbound(b"\x10" * 16000, new_response=True)  # 2s queued at once
    rec.truncate_current_response(played_ms=500)
    assert len(rec.outbound) == 8000 + 4000
    assert rec.save().exists()
