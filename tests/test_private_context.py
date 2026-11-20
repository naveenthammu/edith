from brain.agent import Agent
from brain.llm import LLM, LLMUnavailable, Reply
from skills import Skill


def answer(text):
    return Reply(text, [], {"role": "assistant", "content": text})


def call(name):
    return Reply(None, [{"id": "c1", "name": name, "arguments": {}}], {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": name, "arguments": "{}"}}]})


class RoutedModel:
    def __init__(self):
        self.remote = []
        self.local = []

    def chat(self, messages, tools):
        self.remote.append(str(messages))
        assert "privateorchid" not in str(messages)
        return call("search_notes")

    def chat_local(self, messages, tools):
        self.local.append((messages, tools))
        return answer("According to your note, water the orchid Monday.")


def test_private_retrieval_and_followup_never_go_to_remote_model_or_learning(caplog):
    model = RoutedModel()
    skill = Skill(lambda: "privateorchid: Water on Monday [source d1:c1]", False,
                  {"function": {"name": "search_notes"}}, local_only=True)
    agent = Agent(model, lambda _: True, registry={"search_notes": skill})
    assert "Monday" in agent.handle("Look in my notes for orchid care")
    assert "Monday" in agent.handle("Repeat that please")
    assert len(model.remote) == 1 and len(model.local) == 2
    assert agent.learn() == []
    assert "privateorchid" not in caplog.text
    agent.reset()
    assert "privateorchid" not in str(agent.history)


def test_private_context_blocks_unscoped_external_tool_even_if_model_requests_it():
    sent = []

    class Model(RoutedModel):
        def chat_local(self, messages, tools):
            self.local.append((messages, tools))
            return call("send_email") if len(self.local) == 1 else answer("I cannot send that in this private session.")

    model = Model()
    registry = {
        "search_notes": Skill(lambda: "privateorchid", False, {"function": {"name": "search_notes"}}, local_only=True),
        "send_email": Skill(lambda: sent.append(True) or "sent", False, {"function": {"name": "send_email"}}),
    }
    Agent(model, lambda _: True, registry=registry).handle("Find the orchid note and explain it")
    assert sent == []
    assert all(s["function"]["name"] != "send_email" for s in model.local[0][1])


def test_llm_explicit_local_path_never_attempts_remote():
    class Remote:
        def chat(self, *args):
            raise AssertionError("private content attempted remote inference")

    class Local:
        def complete(self, messages):
            return "Monday from your source"

    llm = LLM(Remote(), local_factory=Local)
    result = llm.chat_local([{"role": "user", "content": "privateorchid"}], [])
    assert result.text == "Monday from your source" and result.local


def test_local_session_keeps_initial_private_input_and_reset_turns_off_remote():
    model = RoutedModel()
    agent = Agent(model, lambda _: True, registry={}, local_session=True)
    assert "Monday" in agent.handle("Save this private document: privateorchid")
    agent.reset()
    assert "Monday" in agent.handle("My next private question includes privateorchid")
    assert model.remote == []
    assert len(model.local) == 2
    assert "privateorchid" in str(model.local[0][0])


def test_local_session_skips_learning_without_a_cloud_or_local_memory_request(tmp_path):
    from brain.memory import Memory

    model = RoutedModel()
    memory = Memory(tmp_path / "memory.txt")
    agent = Agent(model, lambda _: True, registry={}, memory=memory, local_session=True)
    agent.handle("privateorchid is personal")
    assert agent.learn() == []
    assert model.remote == [] and len(model.local) == 1
    assert memory.facts() == []


def test_local_session_allows_desktop_tools_before_private_document_retrieval():
    performed = []

    class Model(RoutedModel):
        def chat_local(self, messages, tools):
            self.local.append((messages, tools))
            return call("desktop_action") if len(self.local) == 1 else answer("Desktop action done.")

    model = Model()
    registry = {"desktop_action": Skill(lambda: performed.append("done") or "done", False,
                                        {"function": {"name": "desktop_action"}})}
    agent = Agent(model, lambda _: True, registry=registry, local_session=True)
    assert agent.handle("Perform the desktop action") == "Desktop action done."
    assert performed == ["done"] and model.remote == []


def test_local_session_without_local_runtime_reports_no_cloud_fallback():
    class RemoteOnly:
        def chat(self, *args):
            raise AssertionError("local-session input must not use the remote path")

    agent = Agent(RemoteOnly(), lambda _: True, registry={}, local_session=True)
    result = agent.handle("privateorchid should remain on this PC")
    assert "local model" in result.lower() and "no cloud fallback" in result.lower()
    assert agent.history == []


def test_local_session_generation_failure_keeps_private_text_out_of_remote():
    class Unavailable(RoutedModel):
        def chat_local(self, messages, tools):
            raise LLMUnavailable("local runtime is unavailable")

    model = Unavailable()
    agent = Agent(model, lambda _: True, registry={}, local_session=True)
    assert "no cloud fallback" in agent.handle("privateorchid").lower()
    assert model.remote == []


def test_taught_task_metadata_can_be_excluded_from_untrusted_entry_points(monkeypatch):
    from skills import teach

    def private_metadata():
        raise AssertionError("restricted entry point attempted to inspect taught task metadata")

    class PublicModel:
        def chat(self, messages, tools):
            return answer("Public answer")

    monkeypatch.setattr(teach, "taught_summary", private_metadata)
    agent = Agent(PublicModel(), lambda _: False, registry={}, include_taught_tasks=False)
    assert agent.handle("Hello") == "Public answer"
