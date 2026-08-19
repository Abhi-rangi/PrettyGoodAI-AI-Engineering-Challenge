# Architecture

## How it works

`run_call.py` places an outbound call through Twilio's REST API with the TwiML passed
inline, so the repo never needs a public HTTP endpoint — only a websocket. That TwiML is
a single `<Connect><Stream>` pointing at `wss://<tunnel>/media-stream`, with the scenario
name in the query string. When the Pretty Good AI agent picks up, Twilio opens that
socket and starts pushing 20ms frames of G.711 mu-law at 8kHz. `server.py` reads the
scenario from the query string, compiles the YAML into a system prompt, and hands both to
`CallBridge`, which opens a second websocket to the OpenAI Realtime API and configures
the session for `audio/pcmu` in and out. From there two asyncio tasks pump in opposite
directions: Twilio frames go up as `input_audio_buffer.append`, and
`response.output_audio.delta` frames come back down inside a Twilio `media` envelope.
Because both legs are already mu-law 8kHz, **no audio is ever decoded, resampled, or
re-encoded in the hot path** — the base64 payload is lifted out of one envelope and
dropped into the other. The same frames are tee'd into a stereo recorder (agent left,
patient right) and the Realtime API's own transcription events build a timestamped
transcript that lines up with the audio.

## Why this way

The decision that mattered was speech-to-speech versus a cascading STT → LLM → TTS
pipeline, and it was settled by the brief itself: voice interaction quality is graded
*before* code, and awkward pauses are called out as a failure. A cascade adds a
transcribe-then-think-then-synthesize round trip that lands around 800ms–1.5s, and it
*sounds* like a bot waiting its turn. The Realtime API keeps it near 500ms and preserves
the prosody that makes a caller read as human. The cost is real — audio tokens are far
more expensive than Whisper plus a small text model — but at roughly 30 minutes of total
call time that difference is a few dollars against a $20 budget, so optimising it would
have been optimising the wrong variable. I chose the raw websocket over a framework
(Pipecat was the alternative, and the fallback plan if the bridge had not worked by hour
three) because the interesting failure mode here is barge-in, and I wanted to own it
rather than inherit it. Interruption handling turned out to need three things done
together — `conversation.item.truncate` so the model does not believe it said words the
caller never heard, a Twilio `clear` to flush the playout buffer, and a matching trim on
the local recording — and getting that wrong in any one place is audible. The other
deliberate choices were smaller: server VAD with `silence_duration_ms` raised from the
500ms default to 700ms, because at the default the bot clipped the agent every time it
paused mid-sentence and at 1000ms the call dragged; an `end_call` function tool so the
model hangs up when its goal is met rather than every call running to a timeout; and
recording in-process instead of paying for Twilio's recording, which is free and gives
per-speaker channel separation that makes the transcripts easy to verify against.

## Data flow

```
                        ┌──────────────────────────────────┐
  run_call.py ─REST─────►  Twilio  ──PSTN──► +1-805-439-8008│
   (inline TwiML)       └────────────┬─────────────────────┘
                                     │ wss, mu-law 8k, 20ms frames
                                     ▼
                        ┌──────────────────────────────────┐
                        │  server.py  /media-stream        │
                        │  ┌────────────────────────────┐  │
                        │  │        CallBridge          │  │
                        │  │  twilio ──► openai  (pump) │  │
                        │  │  openai ──► twilio  (pump) │  │
                        │  │  barge-in · end_call · TTL │  │
                        │  └──────┬──────────────┬──────┘  │
                        └─────────┼──────────────┼─────────┘
                                  │              │ wss, audio/pcmu
                     ┌────────────┴───┐          ▼
                     │ Recorder       │   OpenAI Realtime API
                     │ Transcript     │   (gpt-realtime-2.1-mini)
                     └────────┬───────┘
                              ▼
                calls/call-NN-<scenario>.{mp3,txt,jsonl}
```

## Tradeoffs I accepted

**Local + ngrok rather than a deployed server.** Iteration speed mattered more than a
stable URL for a one-day build; I could watch frame-level logs while a call was live.
The cost is that the tunnel host changes on restart and has to be re-copied into `.env`.

**Polling Twilio for call status instead of a status webhook.** One fewer public
endpoint, and `run_call.py` is the only thing that cares when the call ends.

**Whisper for input transcription rather than the newer streaming transcribers.** The
transcript is an artifact for humans to read afterward, not something the bot reasons
over — the model hears the raw audio directly. Accuracy at the margin was not worth
another variable while tuning.

**No retry logic on a dropped websocket.** A dropped call is a discarded call; twelve
scenarios run in twenty minutes and re-running one is cheaper than the reconnect and
state-resync code would be.

**Measuring latency rather than eyeballing it.** The first version of the analysis tool
inferred silence by diffing consecutive transcript timestamps, which is wrong — the gap
between two turn timestamps is mostly the length of the turn itself, so every call looked
like it was full of five-second pauses. Replacing that with the Realtime API's own
`speech_started` / `speech_stopped` events turned a noisy heuristic into a real
measurement, and it cuts both ways: it is evidence about the agent's responsiveness, and
it is how I checked that our own bot cleared the brief's pacing bar.

**Scenario config over a scenario DSL.** Each scenario is a persona, a goal, success
criteria and a watch-list in YAML. Prompt-level steering was enough to get the bot to
drive toward an outcome; a state machine over conversation turns would have fought the
model's own sense of pacing and made the calls sound scripted, which is the exact failure
the brief warns about.
