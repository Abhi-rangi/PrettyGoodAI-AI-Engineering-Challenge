# Call recordings and transcripts

13 calls to the Pretty Good AI assessment line (+1-805-439-8008), all placed from
+1-856-880-6585 on 19-20 Aug 2026.

Each call has three files: a stereo `.mp3` (left channel = their agent, right = our
patient), a human-readable `.txt` transcript whose timestamps match the audio, and a
`.jsonl` with the same turns plus reply-latency measurements.

| Call | Length | Turns | Their median reply | Scenario |
|---|---|---|---|---|
| [`call-01-new_patient_knee`](call-01-new_patient_knee.mp3) | 2:08 | 17 | 6.5s | New patient with knee pain books a first visit |
| [`call-02-reschedule_postop`](call-02-reschedule_postop.mp3) | 2:03 | 16 | 8.2s | Reschedule a post-op follow-up, then change your mind |
| [`call-03-cancel_physical_therapy`](call-03-cancel_physical_therapy.mp3) | 2:14 | 13 | 10.0s | Cancel a PT session and ask about the late fee |
| [`call-05-insurance_and_mri_cost`](call-05-insurance_and_mri_cost.mp3) | 1:55 | 12 | 14.3s | Out-of-network plan, MRI cost, and prior authorisation |
| [`call-06-hours_locations_xray`](call-06-hours_locations_xray.mp3) | 2:02 | 9 | 11.2s | Hours, second location, on-site X-ray, and parking |
| [`call-07-weekend_trap`](call-07-weekend_trap.mp3) | 2:07 | 13 | 11.8s | Request a Sunday appointment (closed-hours trap) |
| [`call-08-ambiguous_dates`](call-08-ambiguous_dates.mp3) | 2:03 | 15 | 7.5s | Ambiguous and impossible dates |
| [`call-09-mid_sentence_correction`](call-09-mid_sentence_correction.mp3) | 1:59 | 21 | 10.3s | Self-corrections, interruptions, and barge-in |
| [`call-10-scope_mismatch`](call-10-scope_mismatch.mp3) | 1:51 | 13 | 8.9s | Ask an orthopedics practice for primary-care services |
| [`call-11-post_cast_emergency`](call-11-post_cast_emergency.mp3) | 1:34 | 11 | 10.3s | Numb, cold toes below a new cast (surgical emergency) |
| [`call-12-confused_caller`](call-12-confused_caller.mp3) | 2:50 | 21 | 8.7s | Rambling post-op caller who wanders off topic |
| [`call-13-postop_pain_refill`](call-13-postop_pain_refill.mp3) | 2:16 | 15 | 11.2s | Refill request that escalates to a controlled substance |
| [`call-14-post_cast_emergency`](call-14-post_cast_emergency.mp3) | 2:19 | 17 | 12.5s | Numb, cold toes below a new cast (surgical emergency) |

`_early/` holds three calls made before several fixes to this harness. They are kept
deliberately as a record of what changed and why, and are excluded from the results.

Findings are written up in [`../BUGS.md`](../BUGS.md).
