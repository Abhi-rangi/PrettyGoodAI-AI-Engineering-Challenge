from receptionist.turns import TurnTracker

FRAME = 160  # 20ms of mu-law


def speak(t: TurnTracker, response_id: str, frames: int, now: float = 0.0):
    result = None
    for _ in range(frames):
        result = t.on_audio_delta(response_id, "item-" + response_id, FRAME, now)
    return result


def test_barge_in_while_generating_flushes_and_drops_the_rest():
    t = TurnTracker(latest_media_ts=1000)
    speak(t, "r1", 50)  # 1s generated
    t.latest_media_ts = 1400  # 400ms has played
    _, barge = t.on_caller_speech_started(now=2.0)
    assert barge is not None and barge.item_id == "item-r1" and barge.played_ms == 400
    # Late frames of the cancelled response must not reach Twilio after the clear.
    assert t.on_audio_delta("r1", "item-r1", FRAME, 2.1).send is False
    assert t.on_audio_done("r1") is None
    assert not t.playing


def test_barge_in_during_buffered_tail_still_flushes():
    """The old bug: generation finished, Twilio still playing, interruption ignored."""
    t = TurnTracker(latest_media_ts=0)
    speak(t, "r1", 150)  # 3s generated almost instantly
    assert t.on_audio_done("r1") == "play-1"
    assert t.playing  # mark not echoed yet
    t.latest_media_ts = 2000
    _, barge = t.on_caller_speech_started(now=2.0)
    assert barge is not None and barge.played_ms == 2000


def test_played_ms_never_exceeds_generated_audio():
    t = TurnTracker(latest_media_ts=0)
    speak(t, "r1", 25)  # 500ms
    t.on_audio_done("r1")
    t.latest_media_ts = 5000
    _, barge = t.on_caller_speech_started(now=5.0)
    assert barge.played_ms == 500


def test_no_barge_in_after_playback_confirmed():
    t = TurnTracker()
    speak(t, "r1", 10)
    mark = t.on_audio_done("r1")
    assert t.on_mark(mark, now=3.0) is True
    latency, barge = t.on_caller_speech_started(now=3.8)
    assert barge is None
    assert latency == 800  # caller's reply clocked from end of *playback*


def test_stale_mark_after_barge_in_is_ignored():
    t = TurnTracker()
    speak(t, "r1", 10)
    mark = t.on_audio_done("r1")
    t.on_caller_speech_started(now=1.0)
    assert t.on_mark(mark, now=1.1) is False


def test_no_caller_latency_when_they_were_already_talking():
    t = TurnTracker()
    speak(t, "r1", 10)
    mark = t.on_audio_done("r1")
    t.caller_speaking = True
    t.on_mark(mark, now=2.0)
    t.caller_speaking = False
    latency, _ = t.on_caller_speech_started(now=9.0)
    assert latency is None


def test_receptionist_latency_from_caller_stop_to_first_audio():
    t = TurnTracker()
    t.on_caller_speech_started(now=1.0)
    t.on_caller_speech_stopped(now=2.0)
    first = t.on_audio_delta("r1", "i1", FRAME, now=2.65)
    assert first.new_response and first.reply_latency_ms == 650
    assert t.on_audio_delta("r1", "i1", FRAME, now=2.7).reply_latency_ms is None


def test_new_response_after_barge_in_plays_normally():
    t = TurnTracker()
    speak(t, "r1", 10)
    t.on_caller_speech_started(now=1.0)
    t.on_caller_speech_stopped(now=2.0)
    result = t.on_audio_delta("r2", "i2", FRAME, now=2.5)
    assert result.send and result.new_response
    assert t.on_audio_done("r2") is not None
