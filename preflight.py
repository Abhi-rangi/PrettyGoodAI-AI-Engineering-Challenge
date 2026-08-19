#!/usr/bin/env python3
"""Verify everything works before spending money on a phone call.

    python preflight.py

Checks, in order: the scenario files parse, the OpenAI key is valid and the Realtime
session config is accepted, the Twilio credentials work and the from-number is real,
and the public tunnel is reachable. Every one of these has a failure mode that would
otherwise show up as a silent dead call, which is an expensive way to find a typo.
"""
import asyncio
import json
import sys

import websockets

from patient import config, persona
from patient.bridge import REALTIME_URL, build_session_config

PASS, FAIL, WARN = "  \033[32mok\033[0m  ", "  \033[31mFAIL\033[0m", "  \033[33mwarn\033[0m"


def check_scenarios() -> bool:
    try:
        scenarios = persona.load_all()
        for s in scenarios:
            s.system_prompt()
        print(f"{PASS} {len(scenarios)} scenarios parse and build prompts")
        return True
    except Exception as exc:
        print(f"{FAIL} scenario problem: {exc}")
        return False


async def check_openai() -> bool:
    """Open a real Realtime session and push our exact session.update through it."""
    if not config.OPENAI_API_KEY:
        print(f"{FAIL} OPENAI_API_KEY is not set")
        return False

    payload = build_session_config(persona.load_all()[0])
    try:
        async with websockets.connect(
            REALTIME_URL, additional_headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"}
        ) as ws:
            await ws.send(json.dumps(payload))
            for _ in range(12):
                event = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                if event["type"] == "session.updated":
                    audio = event["session"].get("audio", {})
                    fmt = audio.get("input", {}).get("format")
                    print(f"{PASS} OpenAI Realtime accepted the session "
                          f"(model={config.REALTIME_MODEL}, input format={fmt})")
                    return True
                if event["type"] == "error":
                    print(f"{FAIL} OpenAI rejected the session config:")
                    print(f"       {event['error'].get('message')}")
                    return False
            print(f"{WARN} no session.updated came back; check manually")
            return False
    except Exception as exc:
        print(f"{FAIL} could not open a Realtime session: {exc}")
        return False


def check_twilio() -> bool:
    if not (config.TWILIO_ACCOUNT_SID and config.TWILIO_AUTH_TOKEN):
        print(f"{FAIL} Twilio credentials are not set")
        return False
    try:
        from twilio.rest import Client
        client = Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
        account = client.api.accounts(config.TWILIO_ACCOUNT_SID).fetch()

        numbers = [n.phone_number for n in client.incoming_phone_numbers.list()]
        if config.TWILIO_FROM_NUMBER not in numbers:
            print(f"{FAIL} {config.TWILIO_FROM_NUMBER} is not on this account. Owned: {numbers}")
            return False

        print(f"{PASS} Twilio account '{account.friendly_name}' [{account.type}], "
              f"calling from {config.TWILIO_FROM_NUMBER}")
        if account.type == "Trial":
            print(f"{FAIL} This is a TRIAL account. It can only dial verified numbers, "
                  f"and {config.TARGET_NUMBER} cannot be verified. Upgrade before calling.")
            return False
        return True
    except Exception as exc:
        print(f"{FAIL} Twilio check failed: {exc}")
        return False


def check_tunnel() -> bool:
    if not config.PUBLIC_HOST:
        print(f"{FAIL} PUBLIC_HOST is not set")
        return False
    try:
        import urllib.request
        with urllib.request.urlopen(f"https://{config.PUBLIC_HOST}/health", timeout=8) as resp:
            body = json.loads(resp.read())
        print(f"{PASS} tunnel reachable at {config.PUBLIC_HOST} (server model={body.get('model')})")
        return True
    except Exception as exc:
        print(f"{FAIL} could not reach https://{config.PUBLIC_HOST}/health — "
              f"is uvicorn running and ngrok pointed at it? ({exc})")
        return False


async def main() -> None:
    print("\nPreflight\n")
    results = [
        check_scenarios(),
        await check_openai(),
        check_twilio(),
        check_tunnel(),
    ]
    print()
    if all(results):
        print("All good. `python run_call.py --scenario 01` to place the first call.\n")
    else:
        print("Fix the failures above before calling.\n")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
