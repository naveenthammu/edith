"""Exercise the real speech worker; only the physical audio engine is replaced."""
import queue
import threading

import pytest

from voice.tts import Speaker


class SilentEngine:
    def __init__(self):
        self.started = queue.Queue()
        self.finished = threading.Event()
        self.cancelled = threading.Event()
        self.closed = threading.Event()
        self.rate_changed = queue.Queue()
        self.calls = []
        self.fail_start = False
        self.fail_poll = False
        self.auto_finish = False

    def record(self, name):
        self.calls.append((name, threading.get_ident()))

    def start(self, text):
        self.record("start")
        if self.fail_start:
            raise RuntimeError("audio driver unavailable")
        self.finished.clear()
        self.started.put(text)
        if self.auto_finish:
            self.finished.set()

    def wait_until_done(self, timeout_ms):
        self.record("poll")
        if self.fail_poll:
            raise RuntimeError("audio driver disconnected")
        return self.finished.wait(timeout_ms / 1000)

    def stop(self):
        self.record("stop")
        self.cancelled.set()
        self.finished.set()

    def set_rate(self, rate):
        self.record("rate")
        self.rate_changed.put(rate)

    def close(self):
        self.record("close")
        self.closed.set()


@pytest.fixture
def speakers():
    instances = []

    def create(engine=None, **kwargs):
        engine = engine or SilentEngine()
        instance = Speaker(engine_factory=lambda: engine, **kwargs)
        instances.append(instance)
        return instance, engine

    yield create
    for instance in instances:
        assert instance.close(timeout=2), "speech worker did not close"


def call_async(function):
    done = threading.Event()
    errors = []

    def invoke():
        try:
            function()
        except Exception as exc:
            errors.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=invoke, daemon=True)
    thread.start()
    return done, errors


def test_waiting_say_returns_only_after_audio_finishes(speakers):
    speaker, engine = speakers()
    done, errors = call_async(lambda: speaker.say("first"))
    assert engine.started.get(timeout=1) == "first"
    assert speaker.speaking.is_set()
    assert speaker.utterances == 1
    assert not done.is_set()
    engine.finished.set()
    assert done.wait(1)
    assert not errors
    assert not speaker.speaking.is_set()


def test_stop_releases_current_and_queued_waiters_without_playing_stale_text(speakers):
    speaker, engine = speakers()
    current, errors = call_async(lambda: speaker.say("current"))
    assert engine.started.get(timeout=1) == "current"
    speaker.say("old queued one", wait=False)
    speaker.say("old queued two", wait=False)
    speaker.stop()
    assert current.wait(1)
    assert not errors
    assert engine.cancelled.wait(1)
    speaker.say("new generation", wait=False)
    assert engine.started.get(timeout=1) == "new generation"
    assert engine.started.empty()
    assert speaker.utterances == 2


def test_stop_before_engine_load_discards_pending_audio():
    entered = threading.Event()
    load = threading.Event()
    engine = SilentEngine()

    def factory():
        entered.set()
        assert load.wait(2)
        return engine

    speaker = Speaker(engine_factory=factory)
    try:
        assert entered.wait(1)
        speaker.say("stale before loading", wait=False)
        speaker.stop()
        load.set()
        speaker.say("fresh", wait=False)
        assert engine.started.get(timeout=1) == "fresh"
        assert engine.started.empty()
        assert speaker.utterances == 1
    finally:
        load.set()
        assert speaker.close(timeout=2)


def test_stop_between_dequeue_and_start_prevents_stale_audio(speakers, monkeypatch):
    speaker, engine = speakers()
    selected = threading.Event()
    release = threading.Event()
    real_play = speaker._play

    def play(adapter, item):
        if item.text == "selected before stop":
            selected.set()
            assert release.wait(2)
        return real_play(adapter, item)

    monkeypatch.setattr(speaker, "_play", play)
    try:
        speaker.say("selected before stop", wait=False)
        assert selected.wait(1)
        speaker.stop()
        speaker.say("accepted after stop", wait=False)
        release.set()
        assert engine.started.get(timeout=1) == "accepted after stop"
        assert engine.started.empty()
        assert speaker.utterances == 1
    finally:
        release.set()


def test_bounded_queue_applies_backpressure_and_stop_cancels_blocked_producer(speakers, monkeypatch):
    speaker, engine = speakers(max_pending=1)
    speaker.say("active", wait=False)
    assert engine.started.get(timeout=1) == "active"
    speaker.say("queued", wait=False)
    with pytest.raises(queue.Full):
        speaker.say("overload", wait=False)

    # Observe the producer entering the real capacity wait, not scheduling timing.
    waiting = threading.Event()
    real_wait = speaker._condition.wait

    def wait(timeout=None):
        waiting.set()
        return real_wait(timeout)

    monkeypatch.setattr(speaker._condition, "wait", wait)
    generation = speaker.generation
    blocked, errors = call_async(lambda: speaker.say("blocked old generation", expected_generation=generation))
    assert waiting.wait(1)
    assert not blocked.is_set()
    speaker.stop()
    assert blocked.wait(1)
    assert not errors
    assert engine.cancelled.wait(1)
    speaker.say("next", wait=False)
    assert engine.started.get(timeout=1) == "next"
    assert engine.started.empty()


def test_stop_releases_queued_waiter_before_engine_finishes_loading():
    release_factory = threading.Event()
    queued = threading.Event()
    engine = SilentEngine()

    def factory():
        assert release_factory.wait(2)
        return engine

    speaker = Speaker(engine_factory=factory)
    try:
        # Observe acceptance so stop cannot accidentally precede this call.
        original = speaker._enqueue_locked

        def enqueue(text, wait, expected_generation=None):
            item = original(text, wait, expected_generation)
            queued.set()
            return item

        speaker._enqueue_locked = enqueue
        done, errors = call_async(lambda: speaker.say("waiting before load"))
        assert queued.wait(1)
        speaker.stop()
        assert done.wait(1)
        assert not errors
        release_factory.set()
        engine.auto_finish = True
        speaker.say("only fresh text")
        assert engine.started.get(timeout=1) == "only fresh text"
        assert engine.started.empty()
    finally:
        release_factory.set()
        assert speaker.close(timeout=2)


def test_unavailable_engine_releases_waiters_and_future_calls_return():
    entered = threading.Event()
    fail = threading.Event()

    def factory():
        entered.set()
        assert fail.wait(2)
        raise RuntimeError("not installed")

    speaker = Speaker(engine_factory=factory)
    try:
        assert entered.wait(1)
        done, errors = call_async(lambda: speaker.say("cannot speak"))
        fail.set()
        assert done.wait(1)
        assert not errors
        speaker.say("still cannot speak")
        assert not speaker.speaking.is_set()
        assert speaker.repeat_last() is False
    finally:
        fail.set()
        assert speaker.close(timeout=2)


@pytest.mark.parametrize("failure", ["fail_start", "fail_poll"])
def test_driver_failure_does_not_hang_waiters_or_repeat_failed_text(speakers, failure):
    engine = SilentEngine()
    setattr(engine, failure, True)
    speaker, _ = speakers(engine)
    done, errors = call_async(lambda: speaker.say("failed result"))
    assert done.wait(1)
    assert not errors
    assert not speaker.speaking.is_set()
    assert speaker.repeat_last() is False
    assert engine.closed.wait(1)


def test_repeat_uses_only_completed_result_never_cancelled_text(speakers):
    speaker, engine = speakers()
    assert speaker.repeat_last() is False
    done, _ = call_async(lambda: speaker.say("completed result"))
    assert engine.started.get(timeout=1) == "completed result"
    engine.finished.set()
    assert done.wait(1)
    speaker.say("cancelled secret", wait=False)
    assert engine.started.get(timeout=1) == "cancelled secret"
    speaker.stop()
    assert engine.cancelled.wait(1)
    assert speaker.repeat_last(wait=False) is True
    assert engine.started.get(timeout=1) == "completed result"


@pytest.mark.parametrize("invalid", [True, 79, 261, 130.5, "180", None])
def test_rate_rejects_invalid_values_before_driver_receives_them(speakers, invalid):
    speaker, engine = speakers()
    with pytest.raises(ValueError):
        speaker.set_rate(invalid)
    assert engine.rate_changed.empty()


def test_rate_updates_are_applied_on_worker_even_during_speech(speakers):
    speaker, engine = speakers()
    speaker.set_rate(80)
    speaker.say("slow", wait=False)
    assert engine.started.get(timeout=1) == "slow"
    assert engine.rate_changed.get(timeout=1) == 80
    speaker.set_rate(260)
    assert engine.rate_changed.get(timeout=1) == 260
    speaker.stop()
    assert engine.cancelled.wait(1)
    assert speaker.close(timeout=2)
    thread_ids = {thread for _, thread in engine.calls}
    assert len(thread_ids) == 1
    assert threading.get_ident() not in thread_ids


def test_closed_speaker_never_plays_new_or_repeated_audio(speakers):
    engine = SilentEngine()
    engine.auto_finish = True
    speaker, _ = speakers(engine)
    speaker.say("previous")
    assert engine.started.get(timeout=1) == "previous"
    assert speaker.close(timeout=2)
    speaker.say("after close")
    assert speaker.repeat_last() is False
    assert engine.started.empty()


def test_admission_generation_rejects_result_computed_before_stop(speakers):
    speaker, engine = speakers()
    generation = speaker.generation
    speaker.stop()
    speaker.say("late result from cancelled request", expected_generation=generation)
    assert engine.started.empty()
    assert speaker.utterances == 0
    engine.auto_finish = True
    speaker.say("new result", expected_generation=speaker.generation)
    assert engine.started.get(timeout=1) == "new result"
    assert engine.started.empty()


def test_say_without_admission_generation_still_accepts_after_stop(speakers):
    speaker, engine = speakers()
    speaker.stop()
    engine.auto_finish = True
    speaker.say("ordinary call")
    assert engine.started.get(timeout=1) == "ordinary call"


def test_tracked_speech_reports_completed_only_after_native_completion(speakers, capsys):
    speaker, engine = speakers()
    outcomes = []
    done, errors = call_async(lambda: outcomes.append(speaker.say_tracked("private reminder")))
    assert engine.started.get(timeout=1) == "private reminder"
    assert outcomes == []
    engine.finished.set()
    assert done.wait(1)
    assert not errors
    assert outcomes == ["completed"]
    assert "private reminder" not in capsys.readouterr().out


def test_tracked_speech_reports_inflight_cancellation(speakers):
    speaker, engine = speakers()
    outcomes = []
    done, errors = call_async(lambda: outcomes.append(speaker.say_tracked("cancel this")))
    assert engine.started.get(timeout=1) == "cancel this"
    speaker.stop()
    assert done.wait(1)
    assert not errors
    assert outcomes == ["cancelled"]


def test_tracked_speech_reports_cancelled_for_stale_admission_or_closed_speaker(speakers):
    speaker, engine = speakers()
    generation = speaker.generation
    speaker.stop()
    assert speaker.say_tracked("stale", expected_generation=generation) == "cancelled"
    assert speaker.close(timeout=2)
    assert speaker.say_tracked("closed") == "cancelled"
    assert engine.started.empty()


def test_tracked_queued_speech_cancellation_never_reports_completed(speakers, monkeypatch):
    speaker, engine = speakers()
    speaker.say("active", wait=False)
    assert engine.started.get(timeout=1) == "active"
    queued = threading.Event()
    original = speaker._enqueue_locked

    def enqueue(*args, **kwargs):
        item = original(*args, **kwargs)
        queued.set()
        return item

    monkeypatch.setattr(speaker, "_enqueue_locked", enqueue)
    outcomes = []
    done, errors = call_async(lambda: outcomes.append(speaker.say_tracked("pending")))
    assert queued.wait(1)
    speaker.stop()
    assert done.wait(1)
    assert not errors
    assert outcomes == ["cancelled"]
    assert engine.started.empty()


@pytest.mark.parametrize("failure", ["fail_start", "fail_poll"])
def test_tracked_driver_failure_is_failed_not_completed(speakers, failure):
    engine = SilentEngine()
    setattr(engine, failure, True)
    speaker, _ = speakers(engine)
    assert speaker.say_tracked("failed result") == "failed"
    assert engine.closed.wait(1)
    assert speaker.say_tracked("unavailable") == "failed"


def test_tracked_engine_load_failure_releases_pending_call_with_failed():
    load = threading.Event()
    entered = threading.Event()

    def factory():
        entered.set()
        assert load.wait(2)
        raise RuntimeError("engine unavailable")

    speaker = Speaker(engine_factory=factory)
    try:
        assert entered.wait(1)
        outcomes = []
        done, errors = call_async(lambda: outcomes.append(speaker.say_tracked("unavailable")))
        load.set()
        assert done.wait(1)
        assert not errors
        assert outcomes == ["failed"]
    finally:
        load.set()
        assert speaker.close(timeout=2)


def test_tracked_native_error_cannot_copy_private_speech_into_logs(speakers, caplog):
    class ErrorEchoingDriver(SilentEngine):
        def start(self, text):
            raise RuntimeError(text)

    speaker, engine = speakers(ErrorEchoingDriver())
    assert speaker.say_tracked("private-reminder-fixture") == "failed"
    assert engine.closed.wait(1)
    assert "private-reminder-fixture" not in caplog.text


def test_windows_adapter_uses_async_plain_text_and_worker_owned_purge(monkeypatch):
    import sys
    import types
    from voice import tts

    calls = []
    finished = threading.Event()
    started = threading.Event()

    class Voice:
        Rate = 0

        def Speak(self, text, flags):
            calls.append((text, flags, threading.get_ident()))
            if text:
                started.set()
            else:
                finished.set()

        def WaitUntilDone(self, timeout):
            return finished.wait(timeout / 1000)

    owner = []
    voice = Voice()
    client = types.SimpleNamespace(CreateObject=lambda name: voice)
    com = types.SimpleNamespace(client=client, CoInitialize=lambda: owner.append(threading.get_ident()),
                                CoUninitialize=lambda: owner.append(threading.get_ident()))
    monkeypatch.setitem(sys.modules, "comtypes", com)
    monkeypatch.setitem(sys.modules, "comtypes.client", client)
    monkeypatch.setattr(tts.platform, "system", lambda: "Windows")
    speaker = Speaker()
    try:
        speaker.say("<audio src='private-file'/> is literal text", wait=False)
        assert started.wait(1)
        speaker.stop()
        assert finished.wait(1)
    finally:
        assert speaker.close(timeout=2)
    assert calls[0][1] & 1  # asynchronous: required for worker polling/cancellation
    assert calls[0][1] & 16  # never interpret model text as SAPI XML or file audio
    assert any(not text and flags & 2 for text, flags, _ in calls)
    assert len(owner) == 2
    assert len({*owner, *(thread for _, _, thread in calls)}) == 1
    assert owner[0] != threading.get_ident()


@pytest.mark.parametrize("wpm, relative", [(80, -7), (180, 0), (260, 3)])
def test_sapi_rate_uses_documented_threefold_span_with_approximate_baseline(wpm, relative):
    from types import SimpleNamespace
    from voice.tts import _SapiEngine

    voice = SimpleNamespace(Rate=0)
    engine = _SapiEngine(voice, lambda: None)
    engine.set_rate(wpm)
    assert voice.Rate == relative


def test_speaking_flag_remains_until_native_engine_acknowledges_purge(speakers):
    class DelayedPurge(SilentEngine):
        def stop(self):
            self.record("stop")
            self.cancelled.set()

    engine = DelayedPurge()
    speaker, _ = speakers(engine)
    done, errors = call_async(lambda: speaker.say("current audio"))
    assert engine.started.get(timeout=1) == "current audio"
    speaker.stop()
    assert done.wait(1)  # caller is released before hardware acknowledgement
    assert engine.cancelled.wait(1)
    assert not errors
    assert speaker.speaking.is_set()
    engine.finished.set()
    assert speaker.close(timeout=2)
    assert not speaker.speaking.is_set()


class SilentPyttsx:
    """The native pyttsx3 boundary, including its asynchronous callbacks."""
    def __init__(self):
        self.callbacks = {}
        self.started = threading.Event()
        self.closed = threading.Event()
        self.cancelled = threading.Event()
        self.actions = queue.Queue()
        self.busy = False
        self.calls = []
        self.unsupported_loop = False

    def connect(self, topic, callback):
        self.callbacks[topic] = callback
        return topic

    def disconnect(self, token):
        self.callbacks.pop(token, None)

    def startLoop(self, use_driver_loop):
        assert use_driver_loop is False

    def iterate(self):
        if self.unsupported_loop:
            raise TypeError("external iterator unavailable")
        try:
            action = self.actions.get_nowait()
        except queue.Empty:
            return
        self.busy = False
        if action == "error":
            callback = self.callbacks.get("error", lambda **kwargs: None)
            callback(name=None, exception=RuntimeError("driver failure"))
        else:
            callback = self.callbacks.get("finished-utterance", lambda **kwargs: None)
            callback(name=None, completed=action == "complete")
            if action == "cancel":
                self.cancelled.set()

    def say(self, text):
        self.calls.append((text, threading.get_ident()))
        self.busy = True
        self.started.set()

    def isBusy(self):
        return self.busy

    def stop(self):
        if self.busy:
            self.actions.put("cancel")

    def setProperty(self, name, value):
        self.calls.append(((name, value), threading.get_ident()))

    def endLoop(self):
        self.closed.set()


@pytest.mark.parametrize("result", ["error", "cancel"])
def test_pyttsx_driver_error_or_unsolicited_cancel_cannot_be_repeated(result):
    from voice.tts import _Pyttsx3Engine

    native = SilentPyttsx()
    speaker = Speaker(engine_factory=lambda: _Pyttsx3Engine(native))
    try:
        done, errors = call_async(lambda: speaker.say("never completed"))
        assert native.started.wait(1)
        native.actions.put(result)
        assert done.wait(1)
        assert not errors
        assert speaker.repeat_last(wait=False) is False
        assert native.closed.wait(1)
    finally:
        assert speaker.close(timeout=2)


def test_unsupported_pyttsx_external_loop_is_closed_and_waiters_released():
    from voice.tts import _Pyttsx3Engine

    native = SilentPyttsx()
    native.unsupported_loop = True
    speaker = Speaker(engine_factory=lambda: _Pyttsx3Engine(native))
    try:
        done, errors = call_async(lambda: speaker.say("unsupported"))
        assert done.wait(1)
        assert not errors
        assert native.closed.wait(1)
        assert not native.started.is_set()
    finally:
        assert speaker.close(timeout=2)


def test_pyttsx_external_loop_pumps_stop_and_releases_native_resources():
    from voice.tts import _Pyttsx3Engine

    native = SilentPyttsx()
    speaker = Speaker(engine_factory=lambda: _Pyttsx3Engine(native))
    try:
        speaker.set_rate(120)
        speaker.say("active", wait=False)
        assert native.started.wait(1)
        speaker.stop()
        assert native.cancelled.wait(1)
    finally:
        assert speaker.close(timeout=2)
    assert native.closed.is_set()
    assert native.callbacks == {}
    assert native.calls[0][0] == ("rate", 120)
    assert len({thread for _, thread in native.calls}) == 1
    assert native.calls[0][1] != threading.get_ident()


def test_pyttsx_only_successful_finished_callback_becomes_repeatable():
    from voice.tts import _Pyttsx3Engine

    native = SilentPyttsx()
    speaker = Speaker(engine_factory=lambda: _Pyttsx3Engine(native))
    try:
        done, errors = call_async(lambda: speaker.say("completed by driver"))
        assert native.started.wait(1)
        native.actions.put("complete")
        assert done.wait(1)
        assert not errors
        native.started.clear()
        assert speaker.repeat_last(wait=False) is True
        assert native.started.wait(1)
        assert [text for text, _ in native.calls if isinstance(text, str)] == [
            "completed by driver", "completed by driver"
        ]
    finally:
        assert speaker.close(timeout=2)
