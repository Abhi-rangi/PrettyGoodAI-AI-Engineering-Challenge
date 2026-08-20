# Early calls (not part of the submission set)

Kept deliberately as a record of what changed and why. `analyze.py` only looks at the
top level of `calls/`, so these are excluded from the results.

- **call-01** — the bot answered the recorded IVR disclaimer ("I'd prefer to continue
  in English") and replied twice in a row. Led to the silence-through-recordings rule
  and raising server VAD `silence_duration_ms` from 700ms to 900ms.
  Also the first real finding: at 0:49 the agent asserts a date of birth of
  July 4 2000 that the caller never gave.
- **call-02** — cut short at 1:16; superseded.
- **call-03** — clean conversation, and the call that showed the agent booking a
  *routine annual physical* at an orthopedics practice without flagging the mismatch.
  Also exposed our own bug: 30 seconds of agent speech at the end of the call was in
  the recording but missing from the transcript, because we closed the OpenAI socket
  before the in-flight transcriptions arrived.
