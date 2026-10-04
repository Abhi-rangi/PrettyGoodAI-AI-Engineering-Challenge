"""Inbound call server.

    uvicorn server:build --factory --port 5050

Point the clinic's Twilio number's "A call comes in" webhook at
https://<PUBLIC_HOST>/voice (HTTP POST). For each call:

  1. Twilio POSTs /voice. We check the request signature, mint a one-time stream token,
     and answer with TwiML that connects the call to /media-stream, passing the token
     and the caller ID as stream parameters.
  2. Twilio opens /media-stream. We read its `start` frame and check the token before
     opening anything upstream, so nobody who finds the URL can run sessions on our
     OpenAI key.
"""
import secrets
import time
import traceback
from xml.sax.saxutils import quoteattr

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import Response

from receptionist.backend import ClinicBackend, InMemoryBackend
from receptionist.bridge import CallBridge, read_stream_start
from receptionist.config import Settings


class StreamTokens:
    """One-time tokens linking a signed /voice request to the media stream it opens."""

    def __init__(self, ttl_s: float = 60):
        self.ttl_s = ttl_s
        self._issued: dict[str, float] = {}

    def issue(self) -> str:
        now = time.monotonic()
        self._issued = {t: exp for t, exp in self._issued.items() if exp > now}
        token = secrets.token_urlsafe(24)
        self._issued[token] = now + self.ttl_s
        return token

    def redeem(self, token: str | None) -> bool:
        expires = self._issued.pop(token or "", None)
        return expires is not None and expires > time.monotonic()


def twiml(stream_url: str, token: str, caller: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response><Connect>"
        f"<Stream url={quoteattr(stream_url)}>"
        f'<Parameter name="token" value={quoteattr(token)} />'
        f'<Parameter name="caller" value={quoteattr(caller)} />'
        "</Stream></Connect></Response>"
    )


def create_app(settings: Settings, backend: ClinicBackend) -> FastAPI:
    app = FastAPI(title="Clinic receptionist")
    tokens = StreamTokens()

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "model": settings.realtime_model}

    @app.post("/voice")
    async def voice(request: Request) -> Response:
        form = dict(await request.form())
        if settings.validate_twilio_signature:
            from twilio.request_validator import RequestValidator
            # Behind a tunnel request.url is the local address; Twilio signed the
            # public one.
            url = f"https://{settings.public_host}/voice"
            signature = request.headers.get("X-Twilio-Signature", "")
            if not RequestValidator(settings.twilio_auth_token).validate(url, form, signature):
                raise HTTPException(status_code=403, detail="invalid signature")
        body = twiml(f"wss://{settings.public_host}/media-stream", tokens.issue(),
                     str(form.get("From", "")))
        return Response(content=body, media_type="application/xml")

    @app.websocket("/media-stream")
    async def media_stream(ws: WebSocket) -> None:
        await ws.accept()
        start = await read_stream_start(ws)
        if start is None or not tokens.redeem(start.params.get("token")):
            print("  ! rejected media stream without a valid token")
            await ws.close(code=1008)
            return

        print(f"\n=== call {start.call_sid}")
        bridge = CallBridge(ws, settings, backend, start)
        try:
            await bridge.run()
        except Exception:
            traceback.print_exc()
        finally:
            await bridge.finish()
            print(f"=== call {start.call_sid} done\n")

    return app


def build() -> FastAPI:
    """App factory for uvicorn, so importing this module has no side effects."""
    settings = Settings.from_env()
    settings.require("openai_api_key", "twilio_auth_token", "public_host")
    # Swap for the clinic app adapter once it exists; see receptionist/backend.py.
    backend = InMemoryBackend.seeded(settings.clinic_timezone)
    print("  using the in-memory demo backend: bookings are not saved anywhere")
    return create_app(settings, backend)

