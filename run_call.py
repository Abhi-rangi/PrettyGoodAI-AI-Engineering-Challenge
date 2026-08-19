#!/usr/bin/env python3
"""Place outbound test calls to the Pretty Good AI assessment line.

    python run_call.py --list                 # show the scenario catalogue
    python run_call.py --scenario 01          # one call
    python run_call.py --all                  # the full suite, back to back

The TwiML is passed inline to Twilio's REST API, so this repo does not need a public
HTTP endpoint — only the websocket that <Connect><Stream> dials into.
"""
import argparse
import sys
import time
from xml.sax.saxutils import escape

from twilio.rest import Client

from patient import config, persona


def next_index() -> int:
    existing = [p for p in config.CALL_DIR.glob("call-*.txt")]
    return len(existing) + 1


def twiml_for(scenario: persona.Scenario, label: str) -> str:
    url = f"wss://{config.PUBLIC_HOST}/media-stream?scenario={scenario.slug}&label={label}"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response><Connect>"
        f'<Stream url="{escape(url)}" />'
        "</Connect></Response>"
    )


def place(client: Client, scenario: persona.Scenario, index: int) -> None:
    label = f"call-{index:02d}-{scenario.name}"
    print(f"\n▶ {label}: {scenario.label}")
    print(f"  dialing {config.TARGET_NUMBER} from {config.TWILIO_FROM_NUMBER}")

    call = client.calls.create(
        to=config.TARGET_NUMBER,
        from_=config.TWILIO_FROM_NUMBER,
        twiml=twiml_for(scenario, label),
    )

    # Poll rather than webhook: one less public endpoint, and this script is the only
    # thing waiting on the result anyway.
    deadline = time.time() + scenario.max_duration_s + 60
    status = call.status
    while status not in {"completed", "failed", "busy", "no-answer", "canceled"}:
        if time.time() > deadline:
            print("  ! timed out waiting for the call to end")
            break
        time.sleep(3)
        status = client.calls(call.sid).fetch().status

    print(f"  call {status}")
    if status in {"failed", "busy", "no-answer"}:
        print("  (nothing recorded — check the Twilio console for the error code)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", help="scenario id or name, e.g. 01 or medication_refill")
    ap.add_argument("--all", action="store_true", help="run every scenario in order")
    ap.add_argument("--list", action="store_true", help="list scenarios and exit")
    ap.add_argument("--gap", type=int, default=20, help="seconds to wait between calls in --all")
    args = ap.parse_args()

    if args.list:
        for s in persona.load_all():
            print(f"  {s.id}  {s.name:<26} {s.label}")
        return

    config.require("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER",
                   "OPENAI_API_KEY", "PUBLIC_HOST")

    client = Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
    index = next_index()

    if args.all:
        scenarios = persona.load_all()
    elif args.scenario:
        scenarios = [persona.load(args.scenario)]
    else:
        ap.error("pass --scenario NAME, --all, or --list")
        return

    print(f"Server must already be running and reachable at wss://{config.PUBLIC_HOST}")
    for i, scenario in enumerate(scenarios):
        place(client, scenario, index + i)
        if i < len(scenarios) - 1:
            print(f"  waiting {args.gap}s before the next call…")
            time.sleep(args.gap)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
