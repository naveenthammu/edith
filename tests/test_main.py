import pytest

import main
from main import converse, ensure_proxy, is_goodbye, is_yes


def test_ensure_proxy_starts_app_when_port_closed(monkeypatch, tmp_path):
    app = tmp_path / "FreeLLMAPI.exe"
    app.write_text("")
    started, state = [], {"open": False}
    monkeypatch.setattr(main, "port_open", lambda url: state["open"])
    monkeypatch.setenv("ELECTRON_RUN_AS_NODE", "1")
    monkeypatch.setattr("subprocess.Popen", lambda cmd, env: started.append((cmd, env)) or state.update(open=True))
    assert ensure_proxy("http://127.0.0.1:31415", str(app), sleep=lambda s: None) is True
    assert started[0][0] == [str(app)] and "ELECTRON_RUN_AS_NODE" not in started[0][1]


def test_ensure_proxy_skips_when_running_or_unset(monkeypatch):
    monkeypatch.setattr("subprocess.Popen", lambda cmd, env: pytest.fail("should not start"))
    monkeypatch.setattr(main, "port_open", lambda url: True)
    assert ensure_proxy("http://127.0.0.1:31415", "x.exe") is True
    monkeypatch.setattr(main, "port_open", lambda url: False)
    assert ensure_proxy("http://127.0.0.1:31415", None) is False
    assert ensure_proxy(None, "x.exe") is False


def test_is_goodbye():
    assert is_goodbye("bye")
    assert is_goodbye("Goodbye Edith.")
    assert is_goodbye("ok stop")
    assert not is_goodbye("stop the music on youtube please")
    assert not is_goodbye("open notepad")


class FakeListener:
    def __init__(self, texts):
        self.texts = list(texts)

    def listen(self, **kw):
        item = self.texts.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeSpeaker:
    def __init__(self):
        self.said = []

    def say(self, text, wait=True):
        self.said.append(text)


class EchoAgent:
    def handle(self, text):
        return f"you said {text}"


def test_converse_until_goodbye():
    speaker = FakeSpeaker()
    converse(EchoAgent(), FakeListener(["hello", "", "bye"]), speaker)
    assert speaker.said == ["you said hello", "I didn't catch that, sir.", "Goodbye Naveen."]


def test_converse_answers_request_heard_with_wake_word_first():
    speaker = FakeSpeaker()
    converse(EchoAgent(), FakeListener(["bye"]), speaker, first="what's the time")
    assert speaker.said == ["you said what's the time", "Goodbye Naveen."]


def test_converse_returns_on_silence():
    speaker = FakeSpeaker()
    converse(EchoAgent(), FakeListener([None]), speaker)
    assert speaker.said == []


def test_converse_returns_on_microphone_error():
    speaker = FakeSpeaker()
    converse(EchoAgent(), FakeListener([OSError("no mic")]), speaker)
    assert speaker.said == ["I can't hear you, sir. Please check the microphone."]


def test_is_yes_whole_word_only():
    assert is_yes("yes")
    assert is_yes("Yes, do it.")
    assert not is_yes("yesterday")
    assert not is_yes("eyes")
    assert not is_yes("")


def test_converse_leaves_after_three_empty_transcripts():
    speaker = FakeSpeaker()
    converse(EchoAgent(), FakeListener(["", "", ""]), speaker)
    assert speaker.said == ["I didn't catch that, sir."] * 3


def test_converse_reports_each_turn_for_history():
    turns = []
    converse(EchoAgent(), FakeListener(["hello", "", "bye"]), FakeSpeaker(),
             lambda who, text: turns.append((who, text)))
    assert turns == [("you", "hello"), ("edith", "you said hello"), ("you", "bye"), ("edith", "Goodbye Naveen.")]


def test_voice_rate_choice_is_local_and_does_not_require_model():
    class RateSpeaker(FakeSpeaker):
        def set_rate(self, rate):
            self.rate = rate

    class NoModel:
        def handle(self, text):
            pytest.fail('A simple speech preference should not need model inference')

    speaker = RateSpeaker()
    converse(NoModel(), FakeListener(['speak slower', None]), speaker)
    assert speaker.rate == 140
    assert speaker.said == ["I'll speak more slowly."]


def test_confirmation_requires_completed_readback_before_listening():
    class TrackedSpeaker(FakeSpeaker):
        def say_tracked(self, text, **kwargs):
            return 'cancelled'

    listener = FakeListener(['yes'])
    speaker = TrackedSpeaker()
    assert main.confirm_current('Send exact message?', listener, speaker) is False
    assert listener.texts == ['yes']


def test_trusted_review_command_bypasses_model_and_uses_fresh_confirmation():
    class TrackedSpeaker(FakeSpeaker):
        def say_tracked(self, text, **kwargs):
            self.said.append(text)
            return 'completed'

    class Commands:
        def handle(self, text, *, confirm, still_current):
            assert text == 'send reviewed email' and still_current()
            assert confirm('Exact recipient and full body?') is True
            return 'Provider accepted; delivery unverified.'

    class NoModel:
        def handle(self, text):
            pytest.fail('Trusted approval command must not go through the model')

    speaker = TrackedSpeaker()
    converse(NoModel(), FakeListener(['send reviewed email', 'yes', None]), speaker, trusted_commands=Commands())
    assert speaker.said == ['Exact recipient and full body?', 'Provider accepted; delivery unverified.']


def test_reminder_between_wake_detection_and_lock_acquisition_drops_buffer():
    import threading
    from types import SimpleNamespace
    drops, captures = [], []
    mic = SimpleNamespace(capture_revision=0, muted=threading.Event(), drop=lambda: drops.append(True))
    speaker = SimpleNamespace(generation=0, utterances=1, speaking=threading.Event())
    agent = SimpleNamespace(generation=0)
    def listen(**kwargs):
        captures.append(kwargs)
        return 'buffered reminder words'
    listener = SimpleNamespace(mic=mic, listen=listen)
    # This is the state at accepted wake detection, BEFORE the completed reminder.
    snapshot = SimpleNamespace(token=(0, 0, 0), speech_count=0)
    assert main.capture_wake_request(snapshot, listener, speaker, agent) == ((0, 0, 0), None)
    assert drops == [True] and captures == []


def test_mute_then_unmute_before_wake_lock_does_not_admit_fresh_generation():
    import threading
    from types import SimpleNamespace
    drops = []
    mic = SimpleNamespace(capture_revision=2, muted=threading.Event(), drop=lambda: drops.append(True))
    speaker = SimpleNamespace(generation=1, utterances=0, speaking=threading.Event())
    listener = SimpleNamespace(mic=mic, listen=lambda **_: pytest.fail('Stale wake reached capture'))
    snapshot = SimpleNamespace(token=(0, 0, 0), speech_count=0)
    assert main.capture_wake_request(snapshot, listener, speaker, SimpleNamespace(generation=1)) is None
    assert drops == [True]


def test_unchanged_wake_captures_rest_of_sentence_with_original_token():
    import threading
    from types import SimpleNamespace
    calls = []
    mic = SimpleNamespace(capture_revision=0, muted=threading.Event(), drop=lambda: None)
    speaker = SimpleNamespace(generation=0, utterances=0, speaking=threading.Event())
    listener = SimpleNamespace(mic=mic, listen=lambda **kwargs: calls.append(kwargs) or "what's the time")
    snapshot = SimpleNamespace(token=(0, 0, 0), speech_count=0)
    assert main.capture_wake_request(snapshot, listener, speaker, SimpleNamespace(generation=0)) == ((0, 0, 0), "what's the time")
    assert calls == [{'start_timeout': 0.6, 'keep_buffer': True}]


def test_microphone_close_is_attempted_when_stop_raises(caplog):
    from types import SimpleNamespace
    calls = []
    def stop():
        calls.append('stop')
        raise RuntimeError('synthetic driver failure')
    main.close_microphone(SimpleNamespace(stream=SimpleNamespace(stop=stop, close=lambda: calls.append('close'))))
    assert calls == ['stop', 'close']
    assert 'synthetic driver failure' not in caplog.text
