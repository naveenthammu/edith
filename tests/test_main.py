from main import converse, is_goodbye


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
