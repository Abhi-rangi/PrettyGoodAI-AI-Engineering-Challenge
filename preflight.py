#!/usr/bin/env python3
"""Check the setup before calling the clinic number.

    python preflight.py

Checks, in order: the OpenAI key works and the Realtime session config (prompt and
tools included) is accepted, the Twilio credentials work and a number on the account
points its voice webhook at this server, and the public tunnel reaches /health.
"""
import asyncio
import json
import sys
import urllib.request

from receptionist import prompt, realtime
from receptionist.config import Settings

PASS, FAIL, WARN = "  \033[32mok\033[0m  ", "  \033[31mFAIL\033[0m", "  \033[33mwarn\033[0m"


async def check_openai(settings: Settings) -> bool:
    """Open a real Realtime session and push our exact session.update through it."""
    if not settings.openai_api_key:
        print(f"{FAIL} OPENAI_API_KEY is not set")
        return False
    instructions = prompt.render(settings.clinic_name, settings.clinic_timezone, "6095550100")
    try:
        async with realtime.connect(settings) as ws:
            await ws.send(json.dumps(realtime.session_update(settings, instructions)))
            for _ in range(12):
                event = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                if event["type"] == "session.updated":
                    tools = len(event["session"].get("tools", []))
                    print(f"{PASS} Realtime accepted the session "
                          f"(model={settings.realtime_model}, {tools} tools)")
                    return True
                if event["type"] == "error":
                    print(f"{FAIL} Realtime rejected the session config: "
                          f"{event['error'].get('message')}")
                    return False
            print(f"{WARN} no session.updated came back; check manually")
            return False
    except Exception as exc:
        print(f"{FAIL} could not open a Realtime session: {exc}")
        return False


def check_twilio(settings: Settings) -> bool:
    if not (settings.twilio_account_sid and settings.twilio_auth_token):
        print(f"{FAIL} Twilio credentials are not set")
        return False
    try:
        from twilio.rest import Client
        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        numbers = client.incoming_phone_numbers.list()
    except Exception as exc:
        print(f"{FAIL} Twilio check failed: {exc}")
        return False
    if not numbers:
        print(f"{FAIL} this Twilio account owns no phone numbers")
        return False
    want = f"https://{settings.public_host}/voice"
    if settings.twilio_number:
        numbers = [n for n in numbers if n.phone_number == settings.twilio_number]
        if not numbers:
            print(f"{FAIL} TWILIO_NUMBER {settings.twilio_number} is not on this account")
            return False
    wired = [n.phone_number for n in numbers if (n.voice_url or "").rstrip("/") == want]
    if wired:
        print(f"{PASS} {', '.join(wired)} sends incoming calls to {want}")
        return True
    print(f"{FAIL} voice webhook should be {want}")
    for n in numbers:
        print(f"       {n.phone_number} -> {n.voice_url or '(none)'}")
    print("       Console -> Phone Numbers -> your number -> 'A call comes in' -> Webhook, POST")
    return False


def check_tunnel(settings: Settings) -> bool:
    if not settings.public_host:
        print(f"{FAIL} PUBLIC_HOST is not set")
        return False
    try:
        with urllib.request.urlopen(f"https://{settings.public_host}/health", timeout=8) as r:
            body = json.loads(r.read())
        print(f"{PASS} server reachable at {settings.public_host} (model={body.get('model')})")
        return True
    except Exception as exc:
        print(f"{FAIL} could not reach https://{settings.public_host}/health "
              f"- is uvicorn running and the tunnel pointed at it? ({exc})")
        return False


async def main() -> None:
    settings = Settings.from_env()
    print("\nPreflight\n")
    results = [await check_openai(settings), check_twilio(settings), check_tunnel(settings)]
    if not settings.validate_twilio_signature:
        print(f"{WARN} VALIDATE_TWILIO_SIGNATURE is off: anyone can POST /voice")
    if settings.store_calls:
        print(f"{WARN} STORE_CALLS is on: call audio and transcripts (PHI) are written "
              f"to {settings.call_dir}")
    print()
    if all(results):
        print("All good. Call the clinic number to test.\n")
    else:
        print("Fix the failures above first.\n")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
