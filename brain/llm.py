import json
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

LOCAL_TOOL_PROMPT = """You can use these tools:
{tools}
To use a tool, reply with only a JSON object like {{"tool": "tool_name", "args": {{"name": "value"}}}} and nothing else.
After you get the tool result, answer the user in plain words."""


@dataclass
class Reply:
    text: str | None
    tool_calls: list[dict] = field(default_factory=list)  # [{"id", "name", "arguments": dict}]
    message: dict = field(default_factory=dict)  # assistant message to append to history as-is


def _loads_args(raw: str | None) -> dict:
    try:
        args = json.loads(raw or "{}")
    except ValueError:
        return {}
    return args if isinstance(args, dict) else {}


def parse_local_reply(text: str) -> Reply:
    plain = Reply(text, [], {"role": "assistant", "content": text})
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        return plain
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return plain
    if not isinstance(data, dict) or not isinstance(data.get("tool"), str):
        return plain
    args = data.get("args") if isinstance(data.get("args"), dict) else {}
    call = {"id": "local-1", "name": data["tool"], "arguments": args}
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "local-1", "type": "function", "function": {"name": data["tool"], "arguments": json.dumps(args)}}
        ],
    }
    return Reply(None, [call], message)


def to_local_messages(messages: list[dict], tools: list[dict]) -> list[dict]:
    """Rewrite OpenAI tool turns as plain chat turns the local model understands."""
    tool_lines = "\n".join(
        f"- {t['function']['name']}: {t['function']['description']} "
        f"args: {json.dumps(t['function']['parameters']['properties'])}"
        for t in tools
    )
    out = []
    for m in messages:
        role = m["role"]
        if role == "system":
            extra = "\n\n" + LOCAL_TOOL_PROMPT.format(tools=tool_lines) if tools else ""
            out.append({"role": "system", "content": m["content"] + extra})
        elif role == "tool":
            out.append({"role": "user", "content": f"Tool result: {m['content']}"})
        elif role == "assistant" and m.get("tool_calls"):
            fn = m["tool_calls"][0]["function"]
            out.append(
                {"role": "assistant", "content": json.dumps({"tool": fn["name"], "args": _loads_args(fn["arguments"])})}
            )
        else:
            out.append({"role": role, "content": m.get("content") or ""})
    return out
