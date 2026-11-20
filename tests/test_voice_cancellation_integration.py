"""Real cancellation wiring with synthetic transcripts and silent native edges."""
import queue
import sys
import threading
from types import SimpleNamespace

import pytest

from brain.agent import Agent
from brain.llm import Reply
from main import converse
from skills import Skill
from voice.stt import Mic
from voice.tts import Speaker


class SilentAudio:
    def __init__(self):
        self.started = queue.Queue()

    def start(self, text):
        self.started.put(text)

    def wait_until_done(self, timeout_ms):
        return True

    def stop(self):
        pass

    def set_rate(self, rate):
        pass

    def close(self):
        pass


@pytest.fixture
def audio(monkeypatch):
    class NoDeviceStream:
        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(InputStream=NoDeviceStream))
    mic = Mic()
    native = SilentAudio()
    speaker = Speaker(engine_factory=lambda: native)
    yield mic, speaker, native
    assert speaker.close(timeout=2)


class ScriptedListener:
    def __init__(self, mic, results=(), before_result=None):
        self.mic = mic
        self.results = iter(results)
        self.before_result = before_result

    def listen(self, **kwargs):
        item = next(self.results, None)
        if self.before_result:
            self.before_result()
        if isinstance(item, Exception):
            raise item
        return item


def text_reply(text):
    return Reply(text, [], {"role": "assistant", "content": text})


class DelayedModel:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def chat_local(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            assert self.release.wait(2)
            call = {"id": "c1", "name": "record_effect", "arguments": {}}
            return Reply(None, [call], {"role": "assistant", "content": None})
        return text_reply("Fresh response after unmute.")

    def chat(self, messages, tools):
        raise AssertionError("This paired voice test must remain local")


def wire_mute(mic, speaker, agent):
    def interrupt():
        agent.cancel()
        speaker.stop()
    mic.on_mute = interrupt


def run_background(function):
    done = threading.Event()
    errors = []

    def invoke():
        try:
            function()
        except Exception as error:
            errors.append(error)
        finally:
            done.set()

    threading.Thread(target=invoke, daemon=True).start()
    return done, errors


@pytest.mark.parametrize("unmute_during_model", [False, True])
def test_mute_during_model_discards_tool_calls_and_fresh_unmuted_turn_works(audio, unmute_during_model):
    mic, speaker, native = audio
    model = DelayedModel()
    effects = []
    agent = Agent(model, lambda _: True, registry={
        "record_effect": Skill(lambda: effects.append(True) or "done", False,
                               {"function": {"name": "record_effect"}})
    }, local_session=True, include_taught_tasks=False)
    wire_mute(mic, speaker, agent)
    done, errors = run_background(lambda: converse(agent, ScriptedListener(mic), speaker,
                                                   first="Do the old request"))
    try:
        assert model.entered.wait(1)
        mic.muted.set()
        if unmute_during_model:
            mic.muted.clear()
        model.release.set()
        assert done.wait(1)
        assert not errors
        assert effects == []
        assert native.started.empty()
        assert agent.history == []
        if mic.muted.is_set():
            mic.muted.clear()
        converse(agent, ScriptedListener(mic), speaker, first="A fresh request")
        assert native.started.get(timeout=1) == "Fresh response after unmute."
        assert model.calls == 2
        assert "old request" not in str(agent.history)
    finally:
        model.release.set()
        assert done.wait(2)


def test_mute_between_reply_and_speech_admission_rejects_stale_generation(audio):
    mic, speaker, native = audio

    class Model:
        def chat_local(self, messages, tools):
            return text_reply("Do not speak this stale reply")

    agent = Agent(Model(), lambda _: True, registry={}, local_session=True, include_taught_tasks=False)
    wire_mute(mic, speaker, agent)

    def on_turn(who, text):
        if who == "edith":
            mic.muted.set()
            mic.muted.clear()

    converse(agent, ScriptedListener(mic), speaker, on_turn=on_turn, first="Please answer")
    assert native.started.empty()
    assert speaker.utterances == 0


@pytest.mark.parametrize("result", ["", OSError("simulated microphone failure"), "bye"])
def test_mute_during_capture_prevents_late_status_and_goodbye_speech(audio, result):
    mic, speaker, native = audio

    class UnusedAgent:
        def handle(self, text):
            raise AssertionError("Muted captured input must never reach an agent")

    mic.on_mute = speaker.stop
    listener = ScriptedListener(mic, [result], before_result=mic.muted.set)
    converse(UnusedAgent(), listener, speaker)
    assert native.started.empty()
    assert speaker.utterances == 0


def test_mute_unmute_after_transcription_cannot_reclassify_old_input_as_fresh(audio):
    mic, speaker, native = audio
    handled = []

    class RecordingAgent:
        def handle(self, text):
            handled.append(text)
            return "This stale request was executed"

    mic.on_mute = speaker.stop

    def on_turn(who, text):
        if who == "you":
            mic.muted.set()
            mic.muted.clear()

    converse(RecordingAgent(), ScriptedListener(mic), speaker, on_turn=on_turn,
             first="Captured before mute")
    assert handled == []
    assert native.started.empty()
