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
    assert out[0]["content"].startswith("You are EDITH.")
    assert "get_weather" in out[0]["content"]
    assert out[1] == {"role": "user", "content": "weather in Pune"}
    assert json.loads(out[2]["content"]) == {"tool": "get_weather", "args": {"city": "Pune"}}
    assert out[3] == {"role": "user", "content": "Tool result: Pune: 25 degrees"}


import httpx2
import openai
import pytest
from openai.types.chat import ChatCompletion

from brain.llm import LLM, LLMUnavailable


def _completion(message: dict) -> ChatCompletion:
    return ChatCompletion.model_validate(
        {
            "id": "x",
            "object": "chat.completion",
            "created": 0,
            "model": "auto",
            "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
        }
    )


class FakeRemote:
    """Mimics openai.OpenAI().chat.completions.create."""

    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, 0
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


class FakeLocal:
    def __init__(self, text="local answer"):
        self.text, self.seen = text, None

    def complete(self, messages):
        self.seen = messages
        return self.text


def _connection_error():
    return openai.APIConnectionError(request=httpx2.Request("POST", "http://127.0.0.1:31415/v1/chat/completions"))


MSGS = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]


def test_remote_tool_call_is_parsed_and_message_kept():
    remote = FakeRemote(
        _completion(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "open_website", "arguments": '{"url": "https://youtube.com"}'},
                        "thought_signature": "abc",
                    }
                ],
            }
        )
    )
    llm = LLM(remote, "auto", local_factory=FakeLocal)

    reply = llm.chat(MSGS, TOOLS)

    assert reply.tool_calls == [{"id": "call_1", "name": "open_website", "arguments": {"url": "https://youtube.com"}}]
    # Extra provider fields must survive so the next request is accepted.
    assert reply.message["tool_calls"][0]["thought_signature"] == "abc"


def test_remote_failure_falls_back_to_local_and_announces_once():
    announced = []
    local = FakeLocal("offline hello")
    remote = FakeRemote(error=_connection_error())
    llm = LLM(remote, "auto", local_factory=lambda: local, on_fallback=lambda: announced.append(1), clock=lambda: 0.0)

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
    llm = LLM(remote, "auto", local_factory=FakeLocal, clock=lambda: now[0])

    llm.chat(MSGS, TOOLS)
    now[0] = 61.0
    llm.chat(MSGS, TOOLS)

    assert remote.calls == 2


def test_no_remote_uses_local_only():
    llm = LLM(None, "auto", local_factory=lambda: FakeLocal('{"tool": "battery", "args": {}}'))
    assert llm.chat(MSGS, TOOLS).tool_calls[0]["name"] == "battery"


def test_both_backends_down_raises_unavailable():
    def broken_local():
        raise RuntimeError("model file missing")

    llm = LLM(FakeRemote(error=_connection_error()), "auto", local_factory=broken_local)
    with pytest.raises(LLMUnavailable):
        llm.chat(MSGS, TOOLS)


def test_any_api_error_falls_back_to_local():
    err = openai.BadRequestError(
        "bad", response=httpx2.Response(400, request=httpx2.Request("POST", "http://x")), body=None
    )
    llm = LLM(FakeRemote(error=err), "auto", local_factory=lambda: FakeLocal("local text"))
    assert llm.chat(MSGS, TOOLS).text == "local text"


def test_remote_message_drops_unknown_top_level_fields():
    remote = FakeRemote(
        _completion(
            {
                "role": "assistant",
                "content": None,
                "reasoning": "secret thoughts",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "battery", "arguments": "{}"},
                        "thought_signature": "abc",
                    }
                ],
            }
        )
    )
    reply = LLM(remote, "auto", local_factory=FakeLocal).chat(MSGS, TOOLS)
    assert "reasoning" not in reply.message
    assert reply.message["tool_calls"][0]["thought_signature"] == "abc"
    assert "content" in reply.message and reply.message["content"] is None


def test_broken_local_factory_not_retried_during_cooldown():
    calls = []

    def broken():
        calls.append(1)
        raise RuntimeError("no model")

    llm = LLM(None, "auto", local_factory=broken, clock=lambda: 0.0)
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

    llm = LLM(FakeRemote(error=_connection_error()), "auto", local_factory=lambda: FakeLocal("still here"),
              on_fallback=boom)
    assert llm.chat(MSGS, TOOLS).text == "still here"


def test_force_offline_skips_remote():
    remote = FakeRemote(error=_connection_error())
    llm = LLM(remote, "auto", local_factory=lambda: FakeLocal("local only"))
    llm.force_offline = True
    assert llm.chat(MSGS, TOOLS).text == "local only"
    assert remote.calls == 0
