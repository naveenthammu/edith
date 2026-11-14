from brain.agent import Agent
from brain.llm import LLMUnavailable, Reply
from skills import Skill


def _call(name, args=None, call_id="c1"):
    return Reply(
        None,
        [{"id": call_id, "name": name, "arguments": args or {}}],
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}
        ]},
    )


def _text(text):
    return Reply(text, [], {"role": "assistant", "content": text})


class ScriptedLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def chat(self, messages, tools):
        self.requests.append((list(messages), tools))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _registry(**skills):
    return {name: Skill(func, confirm, {"type": "function", "function": {"name": name}})
            for name, (func, confirm) in skills.items()}


def test_plain_reply_returned():
    agent = Agent(ScriptedLLM([_text("Hello sir.")]), confirm=lambda a: True, registry={})
    assert agent.handle("hi") == "Hello sir."


def test_tool_call_runs_skill_and_sends_result_back():
    llm = ScriptedLLM([_call("battery"), _text("Battery is at 80 percent.")])
    agent = Agent(llm, confirm=lambda a: True, registry=_registry(battery=(lambda: "80 percent", False)))

    assert agent.handle("battery?") == "Battery is at 80 percent."
    last_messages = llm.requests[1][0]
    assert last_messages[-1] == {"role": "tool", "tool_call_id": "c1", "content": "80 percent"}


def test_skill_exception_becomes_error_string():
    def boom():
        raise ValueError("no network")

    llm = ScriptedLLM([_call("weather"), _text("Sorry, no network.")])
    agent = Agent(llm, confirm=lambda a: True, registry=_registry(weather=(boom, False)))

    agent.handle("weather")
    assert llm.requests[1][0][-1]["content"] == "error: no network"


def test_unknown_tool_reported():
    llm = ScriptedLLM([_call("fly"), _text("I can't fly.")])
    agent = Agent(llm, confirm=lambda a: True, registry={})
    agent.handle("fly")
    assert llm.requests[1][0][-1]["content"] == "error: unknown tool fly"


def test_tool_call_cap_forces_text_answer():
    llm = ScriptedLLM([_call("ping"), _call("ping"), _call("ping"), _text("Done.")])
    runs = []
    agent = Agent(llm, confirm=lambda a: True, registry=_registry(ping=(lambda: runs.append(1) or "pong", False)))

    assert agent.handle("ping a lot") == "Done."
    assert len(runs) == 3
    assert llm.requests[-1][1] == []  # final request sent without tools


def test_confirm_skill_runs_only_on_yes():
    runs = []
    registry = _registry(shutdown_pc=(lambda: runs.append(1) or "shutting down", True))

    no = Agent(ScriptedLLM([_call("shutdown_pc"), _text("Cancelled.")]), confirm=lambda a: False, registry=registry)
    no.handle("shut down")
    assert runs == []
    assert no.history[-2]["content"] == "cancelled by user"

    asked = []
    yes = Agent(ScriptedLLM([_call("shutdown_pc"), _text("Bye.")]),
                confirm=lambda a: asked.append(a) or True, registry=registry)
    yes.handle("shut down")
    assert runs == [1]
    assert asked == ["shutdown pc"]


def test_llm_unavailable_message_and_history_rolled_back():
    agent = Agent(ScriptedLLM([LLMUnavailable("down")]), confirm=lambda a: True, registry={})
    assert agent.handle("hi") == "My brain is offline right now, sir."
    assert agent.history == []


def test_history_keeps_last_10_user_turns():
    llm = ScriptedLLM([_text(f"r{i}") for i in range(12)])
    agent = Agent(llm, confirm=lambda a: True, registry={})
    for i in range(12):
        agent.handle(f"u{i}")
    users = [m["content"] for m in agent.history if m["role"] == "user"]
    assert users == [f"u{i}" for i in range(2, 12)]


def test_reset_clears_history():
    agent = Agent(ScriptedLLM([_text("ok")]), confirm=lambda a: True, registry={})
    agent.handle("hi")
    agent.reset()
    assert agent.history == []
