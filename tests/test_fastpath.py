import pytest

from brain import fastpath
from brain.agent import Agent
from skills import Skill


@pytest.mark.parametrize("text, expected", [
    ("What time is it?", ("get_datetime", {})),
    ("Edith, what's the time now?", ("get_datetime", {})),
    ("What's the date today?", ("get_datetime", {})),
    ("Battery level.", ("battery", {})),
    ("How's my system doing?", ("system_info", {})),
    ("Edith, increase the volume.", ("volume", {"action": "up", "steps": 5})),
    ("Volume down by 10 percent", ("volume", {"action": "down", "steps": 5})),
    ("Volume to 50%", ("set_volume", {"percent": 50})),
    ("Set the volume to 30 percent.", ("set_volume", {"percent": 30})),
    ("Mute.", ("volume", {"action": "mute"})),
    ("Set brightness to 40.", ("brightness", {"level": 40})),
    ("Turn off Wi-Fi.", ("wifi", {"state": "off"})),
    ("Turn bluetooth on please", ("bluetooth", {"state": "on"})),
    ("Minimise this window.", ("window", {"action": "minimize"})),
    ("Close Chrome.", ("close_app", {"name": "chrome"})),
    ("Open Notepad.", ("open_app", {"name": "notepad"})),
    ("Open VS Code", ("open_app", {"name": "vs code"})),
    ("Take a screenshot.", ("screenshot", {})),
    ("Lock the PC.", ("lock_pc", {})),
    ("Take a note that the meeting is at 5", ("add_note", {"text": "the meeting is at 5"})),
    ("Remind me in 10 minutes to call mom.", ("set_reminder", {"minutes": 10, "message": "call mom"})),
    ("Remind me to stretch in 1 hour", ("set_reminder", {"minutes": 60, "message": "stretch"})),
    ("What's the weather in Bengaluru?", ("get_weather", {"city": "bengaluru"})),
    ("Switch to online mode.", ("set_brain_mode", {"mode": "online"})),
    ("Play Believer on YouTube", ("play_youtube", {"query": "believer"})),
])
def test_matches(text, expected):
    assert fastpath.match(text) == expected


@pytest.mark.parametrize("text", [
    "Open notepad and type hi",           # several steps: the model plans them
    "What's the weather like?",           # no city: memory may know it
    "Open my resume",                     # not an app or a home folder
    "Stop the music on YouTube",
    "If you want to see more videos like this, please subscribe to my channel.",
    "Close all apps",
    "What is the capital of France?",
])
def test_leaves_the_rest_to_the_model(text):
    assert fastpath.match(text) is None


def test_open_home_folder(tmp_path, monkeypatch):
    from skills import computer

    (tmp_path / "Downloads").mkdir()
    monkeypatch.setattr(computer, "HOME", tmp_path)
    assert fastpath.match("open downloads") == ("open_path", {"path": "downloads"})


class NoLLM:
    def chat(self, messages, tools):
        raise AssertionError("instant commands must not call the model")


def _skill(func, confirm=False):
    return Skill(func, confirm, {"type": "function", "function": {"name": "x", "parameters": {"properties": {}}}})


def test_agent_runs_instant_command_without_model():
    agent = Agent(NoLLM(), confirm=lambda a: True, registry={"battery": _skill(lambda: "Battery at 80 percent.")})
    assert agent.handle("battery level") == "Battery at 80 percent."
    assert [m["role"] for m in agent.history] == ["user", "assistant"]


def test_instant_command_still_asks_to_confirm():
    asked = []
    agent = Agent(NoLLM(), confirm=lambda a: asked.append(a) or False,
                  registry={"lock_pc": _skill(lambda: "Computer locked.", confirm=True)})
    assert agent.handle("lock the pc") == "Okay, cancelled, sir."
    assert asked == ["lock pc"]
