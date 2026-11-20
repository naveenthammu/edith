from brain.remote import remote_agent
from brain.llm import Reply
from skills import Skill


def test_remote_agent_cannot_invoke_desktop_or_private_knowledge(monkeypatch):
    effects = []

    class Model:
        def chat(self, messages, tools):
            assert "private_task_marker" not in str(messages)
            assert all(s["function"]["name"] == "get_datetime" for s in tools)
            if not any(m["role"] == "tool" for m in messages):
                return Reply(None, [{"id": "c", "name": "run_script", "arguments": {}}], {"role": "assistant", "content": ""})
            return Reply("That action is unavailable remotely.", [], {"role": "assistant", "content": "That action is unavailable remotely."})

    monkeypatch.setattr("skills.teach.taught_summary", lambda: "private_task_marker")
    registry = {name: Skill(lambda: effects.append(name) or "done", False, {"function": {"name": name}})
                for name in ("get_datetime", "run_script", "search_knowledge", "type_text", "read_emails")}
    agent = remote_agent(Model(), lambda _: True, registry)
    assert "unavailable" in agent.handle("run a script")
    assert effects == [] and agent.memory is None
