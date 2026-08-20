#!/usr/bin/env python3
"""Place outbound test calls to the Pretty Good AI assessment line.

    python run_call.py --list                 # show the scenario catalogue
    python run_call.py --scenario 01          # one call
    python run_call.py --all                  # the full suite, back to back

The TwiML is passed inline to Twilio's REST API, so this repo does not need a public
HTTP endpoint — only the websocket that <Connect><Stream> dials into.
"""
import argparse
import re
import sys
import time
from urllib.parse import quote
from xml.sax.saxutils import escape

from twilio.rest import Client

from patient import config, persona


def next_index(prefix: str) -> int:
    """Highest existing number plus one.

    Self-test calls are numbered separately so they can never be mistaken for the real
    submission set. This counted files rather than reading their numbers, so deleting a
    bad call made the next one reuse a number that was already taken - two different
    scenarios ended up as call-14.
    """
    used = [int(m.group(1))
            for f in config.CALL_DIR.glob(f"{prefix}-*.txt")
            if (m := re.match(rf"{prefix}-(\d+)-", f.name))]
    return max(used, default=0) + 1


def twiml_for(scenario: persona.Scenario, label: str, to: str) -> str:
    """Call parameters ride in the URL *path*, not a query string.

    A query string means ampersands, and an ampersand inside a TwiML attribute has to
    be written `&amp;`. In practice the first parameter arrived and everything after
    the first separator did not — the scenario loaded but the label came through empty,
    so recordings were written as `call-unlabelled`. Path segments have no separator to
    escape, so there is nothing to get wrong.
    """
    url = (f"wss://{config.PUBLIC_HOST}/media-stream"
           f"/{quote(scenario.slug)}/{quote(label)}/{quote(to)}")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response><Connect>"
        f'<Stream url="{escape(url)}" />'
        "</Connect></Response>"
    )


def place(client: Client, scenario: persona.Scenario, index: int, to: str) -> None:
    prefix = "call" if to == config.TARGET_NUMBER else "selftest"
    label = f"{prefix}-{index:02d}-{scenario.name}"
    print(f"\n▶ {label}: {scenario.label}")
    print(f"  dialing {to} from {config.TWILIO_FROM_NUMBER}")
    if prefix == "selftest":
        print("  ** self-test: answer your phone and talk to the bot. "
              "This call does NOT count toward the submission. **")

    call = client.calls.create(
        to=to,
        from_=config.TWILIO_FROM_NUMBER,
        twiml=twiml_for(scenario, label, to),
    )

    # Poll rather than webhook: one less public endpoint, and this script is the only
    # thing waiting on the result anyway. Transient TCP resets against the Twilio API
    # are tolerated - the call itself is already in flight and is being recorded by the
    # media-stream server, so a failed status poll is a reporting problem, not a lost
    # call. Without this, one reset ended a twelve-call run at call four.
    deadline = time.time() + scenario.max_duration_s + 60
    status, consecutive_errors = call.status, 0
    while status not in {"completed", "failed", "busy", "no-answer", "canceled"}:
        if time.time() > deadline:
            print("  ! timed out waiting for the call to end")
            break
        time.sleep(3)
        try:
            status = client.calls(call.sid).fetch().status
            consecutive_errors = 0
        except Exception as exc:
            consecutive_errors += 1
            if consecutive_errors >= 5:
                print(f"  ! lost contact with the Twilio API ({exc}); "
                      f"moving on, check calls/ for the recording")
                break
            time.sleep(3 * consecutive_errors)

    print(f"  call {status}")
    if status in {"failed", "busy", "no-answer"}:
        print("  (nothing recorded — check the Twilio console for the error code)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", help="scenario id or name, e.g. 01 or medication_refill")
    ap.add_argument("--all", action="store_true", help="run every scenario in order")
    ap.add_argument("--skip", default="", metavar="IDS",
                    help="with --all, skip these scenario ids, e.g. --skip 01,07")
    ap.add_argument("--list", action="store_true", help="list scenarios and exit")
    ap.add_argument("--gap", type=int, default=20, help="seconds to wait between calls in --all")
    ap.add_argument("--to", metavar="E164",
                    help="dial this number instead of the assessment line. Use your own "
                         "mobile to rehearse the whole pipeline on a Twilio trial account, "
                         "where only verified numbers are reachable. Output is written as "
                         "selftest-NN-* and excluded from the submission set.")
    args = ap.parse_args()

    if args.list:
        for s in persona.load_all():
            print(f"  {s.id}  {s.name:<26} {s.label}")
        return

    config.require("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER",
                   "OPENAI_API_KEY", "PUBLIC_HOST")

    client = Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
    to = args.to or config.TARGET_NUMBER
    if not to.startswith("+"):
        ap.error(f"--to must be E.164, e.g. +16095550142 (got {to!r})")
    index = next_index("call" if to == config.TARGET_NUMBER else "selftest")

    if args.all:
        skip = {x.strip() for x in args.skip.split(",") if x.strip()}
        scenarios = [s for s in persona.load_all() if s.id not in skip]
        if skip:
            print(f"skipping scenario(s): {', '.join(sorted(skip))}")
    elif args.scenario:
        scenarios = [persona.load(args.scenario)]
    else:
        ap.error("pass --scenario NAME, --all, or --list")
        return

    print(f"Server must already be running and reachable at wss://{config.PUBLIC_HOST}")
    if to != config.TARGET_NUMBER:
        print(f"SELF-TEST MODE — dialing {to}, not the assessment line.")
    failed = []
    for i, scenario in enumerate(scenarios):
        # One bad call must not end the run. Re-running a single scenario afterwards is
        # cheap; losing the eight calls queued behind it is not.
        try:
            place(client, scenario, index + i, to)
        except Exception as exc:
            failed.append(scenario.id)
            print(f"  ! scenario {scenario.id} failed: {type(exc).__name__}: {exc}")
        if i < len(scenarios) - 1:
            print(f"  waiting {args.gap}s before the next call…")
            time.sleep(args.gap)

    if failed:
        print(f"\nFailed scenarios: {', '.join(failed)}")
        print(f"Re-run with: python run_call.py --scenario {failed[0]}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
