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
    assert speaker.said == ["you said hello", "I didn't catch that, sir.", "Goodbye sir."]


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
    assert turns == [("you", "hello"), ("edith", "you said hello"), ("you", "bye"), ("edith", "Goodbye sir.")]
