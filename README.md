# Clinic phone receptionist

An inbound voice receptionist for a physical therapy clinic. It answers the clinic's
Twilio number, identifies the caller (existing patient lookup, or a minimal new-patient
record), and books, reschedules or cancels appointments against the clinic system.
Anything else (insurance, billing, clinical questions) becomes a front-desk callback.

Speech-to-speech over the OpenAI Realtime API, bridged to Twilio Media Streams. Both
legs are G.711 mu-law 8kHz, so audio is never transcoded. See
[ARCHITECTURE.md](ARCHITECTURE.md).

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env          # fill in keys and PUBLIC_HOST
.venv/bin/uvicorn server:build --factory --port 5050
ngrok http 5050               # PUBLIC_HOST is the ngrok host
```

In the Twilio console, set the clinic number's **A call comes in** to Webhook,
`https://<PUBLIC_HOST>/voice`, HTTP POST. Then:

```bash
.venv/bin/python preflight.py   # checks OpenAI session, Twilio webhook, tunnel
.venv/bin/python -m pytest      # no network needed
```

Call the number.

## Layout

| File | What it owns |
|---|---|
| `server.py` | `/voice` webhook (signature check, TwiML), `/media-stream` (token check) |
| `receptionist/bridge.py` | Per-call relay between Twilio and OpenAI, tool loop, watchdog |
| `receptionist/turns.py` | Playback and turn state: barge-in decisions, reply latency. Pure, tested |
| `receptionist/tools.py` | Tool schemas and handlers; all input validation |
| `receptionist/backend.py` | `ClinicBackend` interface and the in-memory demo backend |
| `receptionist/prompt.py` | The receptionist script, rendered per call with today's date and caller ID |
| `receptionist/realtime.py` | Realtime connection and session config |
| `receptionist/recorder.py`, `transcript.py` | Opt-in call audio and transcripts |

## Connecting the clinic app

The server currently runs on `InMemoryBackend`, so **bookings are not saved anywhere**.
To go live, implement `ClinicBackend` (in `receptionist/backend.py`) against the clinic
app's API and pass it to `create_app` in `server.py:build`. The contract:

- Compare phone numbers on digits only; the clinic app stores them as typed.
- `book` and `reschedule` must re-check the slot and raise `SlotUnavailable` if it
  has gone. Double-booking protection belongs in the clinic app, not the bot.
- Methods taking a `patient_id` act only on that patient's data.
- `create_patient` receives a `date`; the clinic API expects an ISO datetime.

## Before real patients call

- **PHI.** Call audio and transcripts are patient data. They are off by default
  (`STORE_CALLS`, `LOG_TRANSCRIPTS`) and write to the git-ignored `var/` when enabled.
  OpenAI and Twilio both process call audio: get BAAs in place first.
- **Hosting.** ngrok is for development. Run behind a stable HTTPS host.
- **Recording notice.** If you enable `STORE_CALLS`, add a recording disclosure to the
  opening line; several US states require all-party consent.

## History

This repo began as an automated *patient caller* used to test another clinic's voice
agent. That code was replaced by the receptionist; its recordings and findings remain
in `calls/` and `BUGS.md`, and the code is in git history (`632a640`).
