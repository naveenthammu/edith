from voice.wake import heard
import pytest


def test_heard_matches_whole_word():
    assert heard("edith", "edith")
    assert heard("hey edith", "edith")
    assert not heard("[unk]", "edith")
    assert not heard("", "edith")
    assert not heard("editha", "edith")


def test_wake_ignores_speaker_audio_and_tail_without_losing_later_user_wake(monkeypatch):
    import json
    import sys
    import threading
    from types import SimpleNamespace
    import numpy as np
    from voice.wake import WakeWord

    accepted, resets = [], []

    class Recognizer:
        def __init__(self, *args):
            pass
        def Reset(self):
            resets.append(True)
        def AcceptWaveform(self, data):
            accepted.append(data)
            return True
        def Result(self):
            return json.dumps({'text': 'edith'})

    monkeypatch.setitem(sys.modules, 'vosk', SimpleNamespace(KaldiRecognizer=Recognizer))
    clock = [0.0]
    speaker = SimpleNamespace(utterances=0, speaking=threading.Event())

    class Mic:
        capture_revision = 0
        muted = threading.Event()
        def drop(self):
            pass
        def chunks(self):
            speaker.utterances += 1
            speaker.speaking.set()
            yield np.ones(4, dtype=np.float32)
            clock[0] = 0.05
            speaker.speaking.clear()
            yield np.ones(4, dtype=np.float32)
            clock[0] = 0.4
            yield np.zeros(4, dtype=np.float32)

    wake = WakeWord.__new__(WakeWord)
    wake.model, wake.word = object(), 'edith'
    wake.wait(Mic(), speaker=speaker, clock=lambda: clock[0])
    assert accepted == [bytes(8)] and resets


@pytest.mark.parametrize('change', ['mute_cycle', 'speech', 'cancel'])
def test_wake_discards_detection_if_capture_changes_inside_recognizer(monkeypatch, change):
    import json
    import sys
    import threading
    from types import SimpleNamespace
    import numpy as np
    from voice.wake import WakeWord

    clock, generation, accepted = [0.0], [0], []
    speaker = SimpleNamespace(utterances=0, generation=0, speaking=threading.Event())

    class Mic:
        capture_revision = 0
        muted = threading.Event()
        def drop(self):
            pass
        def chunks(self):
            yield np.ones(4, dtype=np.float32)
            clock[0] = 1.0
            yield np.zeros(4, dtype=np.float32)

    mic = Mic()
    class Recognizer:
        def __init__(self, *args):
            pass
        def Reset(self):
            pass
        def AcceptWaveform(self, data):
            accepted.append(data)
            if len(accepted) == 1:
                if change == 'mute_cycle':
                    mic.capture_revision += 2
                    speaker.generation += 1
                    generation[0] += 1
                elif change == 'speech':
                    speaker.utterances += 1
                else:
                    generation[0] += 1
            return True
        def Result(self):
            return json.dumps({'text': 'edith'})

    monkeypatch.setitem(sys.modules, 'vosk', SimpleNamespace(KaldiRecognizer=Recognizer))
    wake = WakeWord.__new__(WakeWord)
    wake.model, wake.word = object(), 'edith'
    snapshot = wake.wait(mic, speaker=speaker, clock=lambda: clock[0], current_generation=lambda: generation[0])
    assert len(accepted) == 2  # The first recognised word became stale before returning.
    assert snapshot.token == (mic.capture_revision, speaker.generation, generation[0])
    assert snapshot.speech_count == speaker.utterances
    # Returning a frozen receipt must not adopt later runtime changes.
    before = snapshot.token
    mic.capture_revision += 1
    assert snapshot.token == before
