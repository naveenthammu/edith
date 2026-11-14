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
