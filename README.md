# Automated patient caller — Pretty Good AI assessment

A Python voice bot that phones the Pretty Good AI test line, plays a patient with a goal,
holds a natural two-way conversation, and records both sides to MP3 with a timestamped
transcript.

**15 calls placed** from a single number (+1-856-880-6585), all 1:34–2:50, median reply
latency 660ms on our side. Findings are in **[`BUGS.md`](BUGS.md)** (12, most severe
first); design reasoning is in **[`ARCHITECTURE.md`](ARCHITECTURE.md)**; recordings and
transcripts are indexed in **[`calls/`](calls/README.md)**.

---

## Setup

Python 3.10+, [ffmpeg](https://ffmpeg.org) (`brew install ffmpeg`), and
[ngrok](https://ngrok.com/download).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env       # then fill in the four credentials
```

> **Twilio must be a paid account.** A trial account can only dial numbers you have
> verified with a code, and the assessment line cannot be verified. `preflight.py` checks
> for this and says so before you waste a call.

## Running

Two terminals stay up for the session; the third places calls.

```bash
# 1 — media-stream server
uvicorn server:app --port 5050 --reload

# 2 — public tunnel; copy the forwarding host into PUBLIC_HOST in .env
ngrok http 5050

# 3
python preflight.py                 # validates creds, session schema and tunnel
python run_call.py --scenario 01    # one call
```

| Command | What it does |
|---|---|
| `python run_call.py --list` | Show the scenario catalogue |
| `python run_call.py --scenario 07` | One call (id, name or slug all work) |
| `python run_call.py --all [--skip 01,04]` | Every scenario, 20s apart |
| `python run_call.py --scenario 01 --to +1555...` | Rehearse against your own phone |
| `python preflight.py` | Credentials, Realtime session schema, tunnel |
| `python analyze.py stats` | Durations, turns, measured latency, quality flags |
| `python analyze.py audio` | Cross-talk, levels, clipping from the recordings |
| `python analyze.py report` | Write `bug-candidates.md` for manual review |
| `python analyze.py repair` | Rebuild a transcript from audio if a side went missing |

Each call writes three files into `calls/`:

```
call-07-weekend_trap.mp3      stereo: left = their agent, right = our patient
call-07-weekend_trap.txt      timestamped transcript, goal and watch-list in the header
call-07-weekend_trap.jsonl    same turns, machine-readable, plus latency metrics
```

Transcript timestamps line up with the MP3, so `BUGS.md` can cite `1:23` and you can seek
straight to it.

### Checking the calls

`analyze.py stats` is the pre-submission gate — it flags any call under a minute, thin on
turns, missing audio, or missing one side of the transcript.

It also reports **measured reply latency for both sides**, from the Realtime API's
voice-activity events clocked against the Twilio `mark` that confirms our audio finished
playing. `analyze.py audio` derives cross-talk, peak level and clipping straight from the
recordings. Between them, "does this sound natural" is a number rather than an impression —
which is how the 8.2s figure in the bug report was verified two independent ways.

`analyze.py report` scans transcripts for suspicious moments and writes
`bug-candidates.md`. Its patterns were written *after* hearing how this agent actually
talks — the first version, written before any call, matched nothing across twelve calls.
Output is candidates, not findings; each one gets listened to before it earns a place in
`BUGS.md`.

---

## Scenarios

The test line is **Pivot Point Orthopedics**, a specialist practice, so the scenarios are
written for orthopedic callers — a knee, a cast, a post-op follow-up. Two deliberately test
the opposite: what happens when the agent is asked for something outside its specialty.

| # | Scenario | What it probes |
|---|---|---|
| 01 | New patient, knee pain | Happy path; does intake collect what it needs |
| 02 | Reschedule post-op, then change your mind | Does the second change overwrite the first |
| 03 | Cancel PT + late fee | Does it invent a policy it cannot know |
| 04 | Refill: meloxicam, then oxycodone | Is a Schedule II opioid handled differently |
| 05 | Insurance, MRI cost, prior auth | Does it claim to accept a plan it cannot verify |
| 06 | Hours, second location, on-site X-ray | Internal consistency when re-asked |
| 07 | Sunday appointment | The closed-hours trap from their own example |
| 08 | Ambiguous + impossible dates | "next Tuesday", the 31st of a 30-day month, Feb 30 |
| 09 | Mid-sentence corrections | Barge-in; does the last correction win |
| 10 | Annual physical at an ortho practice | Scope validation |
| 11 | Numb, cold toes below a fresh cast | Escalation on a limb-threatening emergency |
| 12 | Rambling post-op caller | Holding the thread against tangents |

Adding one is a YAML file in `scenarios/` — persona, goal, success criteria, watch-list.
No code changes.

Scenarios 04 and 11 were each run twice. The first attempt at 11 never reached the symptom
because the agent's identity checks consumed the call, so it was re-run with the symptom
moved to the opening sentence; see the methodology notes in `BUGS.md`.

## Layout

```
run_call.py          CLI: builds TwiML and places the call
server.py            FastAPI app exposing the media-stream websocket
preflight.py         Validates credentials and the Realtime session schema
analyze.py           Call stats, audio measurement, transcript repair, bug candidates
patient/
  bridge.py          The relay: audio pumps, barge-in, end_call tool, watchdog
  persona.py         Scenario YAML -> system prompt; VAD tuning
  recorder.py        Stereo mu-law capture -> WAV -> MP3
  transcript.py      Timestamped two-sided transcript + latency metrics
  audio.py           G.711 mu-law decode (audioop was removed in Python 3.13)
  config.py          Settings; the assessment number is a constant, not an env var
scenarios/           12 YAML scenario definitions
calls/               15 recordings, transcripts, and an index
```

## Cost

About $8 all in, against the $20 budget: roughly $6 of OpenAI Realtime audio tokens on
`gpt-realtime-2.1-mini` and under $1 of Twilio (~$0.014/min voice plus $0.0044/min media
streams, on a $1.15/mo number). Call recording is done in-process rather than through
Twilio's paid recording — free, and it gives the per-speaker channel separation the
transcripts and audio analysis depend on.
