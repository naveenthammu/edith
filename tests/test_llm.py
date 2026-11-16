import json

from brain.llm import parse_local_reply, to_local_messages

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the weather.",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        },
    }
]


def test_parse_local_reply_tool_call():
    reply = parse_local_reply('{"tool": "get_weather", "args": {"city": "Pune"}}')
    assert reply.text is None
    assert reply.tool_calls == [{"id": "local-1", "name": "get_weather", "arguments": {"city": "Pune"}}]
    fn = reply.message["tool_calls"][0]["function"]
    assert fn["name"] == "get_weather"
    assert json.loads(fn["arguments"]) == {"city": "Pune"}


def test_parse_local_reply_tool_call_inside_code_fence():
    reply = parse_local_reply('```json\n{"tool": "battery", "args": {}}\n```')
    assert reply.tool_calls[0]["name"] == "battery"
    assert reply.tool_calls[0]["arguments"] == {}


def test_parse_local_reply_plain_text():
    reply = parse_local_reply("Hello sir, how can I help?")
    assert reply.text == "Hello sir, how can I help?"
    assert reply.tool_calls == []
    assert reply.message == {"role": "assistant", "content": "Hello sir, how can I help?"}


def test_parse_local_reply_invalid_json_is_plain_text():
    reply = parse_local_reply('{"tool": "broken"')
    assert reply.tool_calls == []
    assert reply.text == '{"tool": "broken"'


def test_to_local_messages_converts_tool_turns():
    messages = [
        {"role": "system", "content": "You are EDITH."},
        {"role": "user", "content": "weather in Pune"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Pune"}'}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "Pune: 25 degrees"},
    ]

    out = to_local_messages(messages, TOOLS)

    assert out[0]["role"] == "system"
    assert out[0]["content"].startswith("You can use these tools:")  # static prefix stays cached
    assert out[0]["content"].endswith("You are EDITH.")
    assert "get_weather" in out[0]["content"]
    assert out[1] == {"role": "user", "content": "weather in Pune"}
    assert json.loads(out[2]["content"]) == {"tool": "get_weather", "args": {"city": "Pune"}}
    assert out[3] == {"role": "user", "content": "Tool result: Pune: 25 degrees"}


import httpx2
import anthropic
import pytest
from types import SimpleNamespace

from brain.llm import LLM, ClaudeRemote, LLMUnavailable, Reply, to_claude_messages


class FakeRemote:
    """Stands in for ClaudeRemote: chat(messages, tools) -> Reply."""

    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, 0

    def chat(self, messages, tools):
        self.calls += 1
        if self.error:
            raise self.error
        return self.reply


class FakeLocal:
    def __init__(self, text="local answer"):
        self.text, self.seen = text, None

    def complete(self, messages):
        self.seen = messages
        return self.text


def _connection_error():
    return anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))


MSGS = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]


class _Block(SimpleNamespace):
    def model_dump(self, exclude_none=True):
        return {k: v for k, v in vars(self).items() if v is not None}


class FakeClaudeClient:
    """Mimics anthropic.Anthropic().beta.messages.create."""

    def __init__(self, content, stop_reason="end_turn"):
        self.content, self.stop_reason, self.kwargs = content, stop_reason, None
        self.beta = self.messages = self

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(content=[_Block(**b) for b in self.content], stop_reason=self.stop_reason)


def test_claude_tool_call_is_parsed_and_content_kept():
    client = FakeClaudeClient([
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "tool_use", "id": "toolu_1", "name": "open_website", "input": {"url": "https://youtube.com"}},
    ], stop_reason="tool_use")

    reply = ClaudeRemote(client, "claude-opus-5-5").chat(MSGS, TOOLS)

    assert reply.tool_calls == [{"id": "toolu_1", "name": "open_website", "arguments": {"url": "https://youtube.com"}}]
    assert reply.message["claude_content"][0]["signature"] == "sig"  # thinking survives for replay
    assert client.kwargs["system"] == "s"
    assert client.kwargs["tools"][0] == {"name": "get_weather", "description": "Get the weather.",
                                         "input_schema": TOOLS[0]["function"]["parameters"]}


def test_claude_refusal_gives_spoken_answer():
    reply = ClaudeRemote(FakeClaudeClient([], stop_reason="refusal"), "m").chat(MSGS, [])
    assert reply.text and reply.tool_calls == []


def test_to_claude_messages_groups_tool_results_and_replays_content():
    content = [{"type": "tool_use", "id": "t1", "name": "a", "input": {}},
               {"type": "tool_use", "id": "t2", "name": "b", "input": {}}]
    history = MSGS + [
        {"role": "assistant", "content": None, "claude_content": content},
        {"role": "tool", "tool_call_id": "t1", "content": "one"},
        {"role": "tool", "tool_call_id": "t2", "content": "two"},
    ]
    system, msgs = to_claude_messages(history)
    assert system == "s"
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[1]["content"] == content
    assert [b["tool_use_id"] for b in msgs[2]["content"]] == ["t1", "t2"]


def test_to_claude_messages_converts_local_model_tool_calls():
    history = [{"role": "user", "content": "battery?"},
               {"role": "assistant", "content": None, "tool_calls": [
                   {"id": "local-1", "type": "function", "function": {"name": "battery", "arguments": "{}"}}]},
               {"role": "tool", "tool_call_id": "local-1", "content": "90 percent"}]
    _, msgs = to_claude_messages(history)
    assert msgs[1]["content"] == [{"type": "tool_use", "id": "local-1", "name": "battery", "input": {}}]
    assert msgs[2]["content"][0]["type"] == "tool_result"


def test_remote_failure_falls_back_to_local_and_announces_once():
    announced = []
    local = FakeLocal("offline hello")
    remote = FakeRemote(error=_connection_error())
    llm = LLM(remote, local_factory=lambda: local, on_fallback=lambda: announced.append(1), clock=lambda: 0.0)

    first = llm.chat(MSGS, TOOLS)
    second = llm.chat(MSGS, TOOLS)

    assert first.text == "offline hello"
    assert second.text == "offline hello"
    assert announced == [1]
    assert remote.calls == 1  # second request skipped remote: still inside the 60s offline window
    assert "get_weather" in local.seen[0]["content"]


def test_remote_retried_after_offline_window():
    now = [0.0]
    remote = FakeRemote(error=_connection_error())
    llm = LLM(remote, local_factory=FakeLocal, clock=lambda: now[0])

    llm.chat(MSGS, TOOLS)
    now[0] = 61.0
    llm.chat(MSGS, TOOLS)

    assert remote.calls == 2


def test_remote_success_is_returned():
    reply = Reply("hello", [], {"role": "assistant", "content": "hello"})
    assert LLM(FakeRemote(reply), local_factory=FakeLocal).chat(MSGS, TOOLS) is reply


def test_no_remote_uses_local_only():
    llm = LLM(None, local_factory=lambda: FakeLocal('{"tool": "battery", "args": {}}'))
    assert llm.chat(MSGS, TOOLS).tool_calls[0]["name"] == "battery"


def test_both_backends_down_raises_unavailable():
    def broken_local():
        raise RuntimeError("model file missing")

    llm = LLM(FakeRemote(error=_connection_error()), local_factory=broken_local)
    with pytest.raises(LLMUnavailable):
        llm.chat(MSGS, TOOLS)


def test_any_api_error_falls_back_to_local():
    err = anthropic.BadRequestError(
        "bad", response=httpx2.Response(400, request=httpx2.Request("POST", "http://x")), body=None
    )
    llm = LLM(FakeRemote(error=err), local_factory=lambda: FakeLocal("local text"))
    assert llm.chat(MSGS, TOOLS).text == "local text"


def test_broken_local_factory_not_retried_during_cooldown():
    calls = []

    def broken():
        calls.append(1)
        raise RuntimeError("no model")

    llm = LLM(None, local_factory=broken, clock=lambda: 0.0)
    for _ in range(2):
        with pytest.raises(LLMUnavailable):
            llm.chat(MSGS, TOOLS)
    assert len(calls) == 1


def test_to_local_messages_truncates_tool_results():
    out = to_local_messages([{"role": "tool", "tool_call_id": "c", "content": "x" * 2000}], [])
    assert out[0]["content"] == "Tool result: " + "x" * 500


def test_fallback_announcement_failure_does_not_break_chat():
    def boom():
        raise RuntimeError("tts down")

    llm = LLM(FakeRemote(error=_connection_error()), local_factory=lambda: FakeLocal("still here"), on_fallback=boom)
    assert llm.chat(MSGS, TOOLS).text == "still here"


def test_force_offline_skips_remote():
    remote = FakeRemote(error=_connection_error())
    llm = LLM(remote, local_factory=lambda: FakeLocal("local only"))
    llm.force_offline = True
    assert llm.chat(MSGS, TOOLS).text == "local only"
    assert remote.calls == 0


from brain.llm import OpenAIRemote, RemoteChain


def test_chain_falls_through_to_next_online_model():
    good = Reply("free answer", [], {"role": "assistant", "content": "free answer"})
    first, second = FakeRemote(error=_connection_error()), FakeRemote(good)
    assert RemoteChain([first, second]).chat(MSGS, TOOLS) is good
    assert (first.calls, second.calls) == (1, 1)


def test_chain_raises_when_all_online_models_fail_so_local_takes_over():
    chain = RemoteChain([FakeRemote(error=_connection_error()), FakeRemote(error=_connection_error())])
    llm = LLM(chain, local_factory=lambda: FakeLocal("local"), clock=lambda: 0.0)
    assert llm.chat(MSGS, TOOLS).text == "local"


def test_chain_vision_picks_claude_only():
    claude = ClaudeRemote(FakeClaudeClient([]), "m")
    assert RemoteChain([object.__new__(OpenAIRemote), claude]).vision() is claude
    assert RemoteChain([object.__new__(OpenAIRemote)]).vision() is None


def test_openai_remote_strips_claude_content():
    seen = {}

    class Client:
        def __init__(self):
            self.chat = self.completions = self

        def create(self, **kw):
            seen.update(kw)
            msg = SimpleNamespace(content="hi", tool_calls=None, model_dump=lambda exclude_none: {})
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    history = MSGS + [{"role": "assistant", "content": "x", "claude_content": [{"type": "text", "text": "x"}]}]
    assert OpenAIRemote(Client(), "auto").chat(history, []).text == "hi"
    assert all("claude_content" not in m for m in seen["messages"])
