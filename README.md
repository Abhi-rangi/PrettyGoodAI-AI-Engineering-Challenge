# Automated patient caller — Pretty Good AI assessment

A Python voice bot that phones the Pretty Good AI test line, plays a realistic patient
with a goal, holds a natural two-way conversation, and records both sides to MP3 with a
timestamped transcript.

Twelve scenarios ship with the repo, covering scheduling, rescheduling, cancellation,
refills, insurance, hours, and six deliberate edge cases. Findings are in
[`BUGS.md`](BUGS.md); design reasoning is in [`ARCHITECTURE.md`](ARCHITECTURE.md).

Calls and transcripts are in [`calls/`](calls/).

---

## Setup

Requires Python 3.10+, [ffmpeg](https://ffmpeg.org) (`brew install ffmpeg`), and
[ngrok](https://ngrok.com/download).

```bash
git clone https://github.com/<you>/pgai-patient-caller.git
cd pgai-patient-caller
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env       # then fill it in
```

`.env` needs an OpenAI API key with Realtime access, Twilio credentials, and the one
phone number you call from. See [`.env.example`](.env.example) for the full list.

> **Twilio must be a paid account.** A trial account can only dial numbers you have
> verified with a code, and the assessment line cannot be verified. `preflight.py`
> checks for this and tells you before you waste a call.

---

## Running

Three terminals. The first two stay up for the whole session.

```bash
# 1 — the media-stream server
uvicorn server:app --port 5050

# 2 — the public tunnel; copy the forwarding host into PUBLIC_HOST in .env
ngrok http 5050

# 3 — check everything, then call
python preflight.py
python run_call.py --scenario 01
```

That is the single command per call: `python run_call.py --scenario 01`.

| Command | What it does |
|---|---|
| `python run_call.py --list` | Show the scenario catalogue |
| `python run_call.py --scenario 07` | One call (id, name, or slug all work) |
| `python run_call.py --all` | Every scenario, 20s apart |
| `python preflight.py` | Validate credentials, session schema, and tunnel |
| `python analyze.py stats` | Durations, turn counts, measured latency, quality flags |
| `python analyze.py report` | Write `bug-candidates.md` for manual review |
| `python analyze.py repair` | Rebuild a transcript from audio if a side went missing |

Each call writes three files into `calls/`:

```
call-07-weekend_trap.mp3      stereo: left = PGAI agent, right = our patient
call-07-weekend_trap.txt      timestamped transcript with goal and watch-list
call-07-weekend_trap.jsonl    the same turns, machine-readable
```

Transcript timestamps line up with the MP3, so a bug report can cite `1:23` and you can
seek straight to it.

### Checking the calls

`analyze.py stats` is the pre-submission check — it flags any call that is under a
minute, thin on turns, missing audio, or missing one side of the transcript, and tells
you how many clear the bar.

It also reports **measured reply latency for both sides**. The Realtime API's VAD events
give the exact moment each side stopped and started talking, so "how long did the agent
take to answer" is a number rather than an impression. Our own latency is in there too,
which is how I verified the bot met the brief's pacing requirement instead of assuming it.

`analyze.py report` scans every transcript for suspicious moments — a booking confirmed
for a weekend, a price quoted twice with different numbers, refill language in a
controlled-substance call, an emergency symptom with no escalation anywhere in the call,
the agent repeating itself verbatim — and writes them to `bug-candidates.md` with
timestamps. These are candidates, not findings; each one gets listened to before it earns
a place in `BUGS.md`.

If live transcription ever drops a side, `analyze.py repair` re-transcribes from the
stereo recording. Because the two speakers are on separate channels, each can be
transcribed in isolation, which is more accurate than transcribing a mixed call.

---

## Scenarios

The target turned out to be **Pivot Point Orthopedics**, a specialist practice, so the
scenarios are written for orthopedic care rather than generic primary care — a real
caller to this number has a knee, a cast or a post-op follow-up, not a head cold. Two
scenarios deliberately test the opposite: what the agent does when asked for something
outside its specialty.

| # | Scenario | What it probes |
|---|---|---|
| 01 | New patient, knee pain | Happy path; does intake collect what it needs |
| 02 | Reschedule post-op, then change your mind | Does the second change overwrite the first |
| 03 | Cancel PT + late fee | Does it invent a policy it cannot know |
| 04 | Post-op refill, meloxicam then oxycodone | Is a Schedule II opioid handled differently |
| 05 | Insurance, MRI cost, prior auth | Does it claim to accept a plan it cannot verify |
| 06 | Hours, second location, on-site X-ray | Internal consistency when re-asked |
| 07 | Sunday appointment | The closed-hours trap from their own example |
| 08 | Ambiguous + impossible dates | "next Tuesday", the 31st of a 30-day month, Feb 30 |
| 09 | Mid-sentence corrections | Barge-in; does the last correction win |
| 10 | Annual physical at an ortho practice | Scope validation — does it book what it cannot do |
| 11 | Numb, cold toes below a fresh cast | Escalation on a limb-threatening emergency |
| 12 | Rambling post-op caller | Holding the thread against tangents |

Adding one is a YAML file in `scenarios/` — a persona, a goal, success criteria, and a
watch-list. No code changes.

---

## Layout

```
run_call.py          CLI: builds TwiML and places the call
server.py            FastAPI app exposing /media-stream
preflight.py         Validates credentials and the Realtime session schema
patient/
  bridge.py          The relay. Audio pumps, barge-in, end_call tool
  persona.py         Scenario YAML -> system prompt; VAD tuning
  recorder.py        Stereo mu-law capture -> WAV -> MP3
  transcript.py      Timestamped two-sided transcript
  audio.py           G.711 mu-law decode (audioop is gone in 3.13)
  config.py          Environment settings
analyze.py           Call quality stats, transcript repair, bug candidate scan
scenarios/           Twelve YAML scenario definitions
calls/               Recordings and transcripts
```

## Cost

Roughly $0.20 per minute of call, almost all of it OpenAI Realtime audio tokens. The
full twelve-call suite plus development came to well under the $20 budget. Twilio is
about two cents a minute including Media Streams; call recording is done in-process
rather than through Twilio's paid recording, which is both free and gives channel
separation.
