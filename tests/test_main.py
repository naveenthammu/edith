from main import converse, is_goodbye, is_yes


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
