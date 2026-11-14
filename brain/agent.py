import logging
from datetime import datetime
from typing import Callable

from brain.llm import LLMUnavailable
from skills import REGISTRY, Skill

log = logging.getLogger(__name__)

_PY_TYPES = {"integer": int, "number": float, "string": str}


def _coerce(args: dict, schema: dict) -> dict:
    """The local model often sends numbers as strings; convert using the skill's schema."""
    props = schema.get("function", {}).get("parameters", {}).get("properties", {})
    out = {}
    for key, value in args.items():
        cast = _PY_TYPES.get(props.get(key, {}).get("type"))
        out[key] = cast(value) if cast and not isinstance(value, bool) else value
    return out

SYSTEM_PROMPT = (
    "You are EDITH, a voice assistant running on the user's Windows PC. Address the user as sir. "
    "Your replies are spoken aloud: answer in one to three short sentences, with no markdown, lists or emojis. "
    "Use the tools to act on the PC or fetch information. Current date and time: {now}."
)


class Agent:
    def __init__(self, llm, confirm: Callable[[str], bool], registry: dict[str, Skill] = REGISTRY,
                 max_tool_calls: int = 3, max_turns: int = 10):
        self.llm = llm
        self.confirm = confirm  # asks the user out loud, returns True only on "yes"
        self.registry = registry
        self.max_tool_calls = max_tool_calls
        self.max_turns = max_turns
        self.history: list[dict] = []

    def reset(self) -> None:
        self.history = []

    def handle(self, text: str) -> str:
        start = len(self.history)
        self.history.append({"role": "user", "content": text})
        try:
            answer = self._loop()
        except LLMUnavailable:
            del self.history[start:]
            return "My brain is offline right now, sir."
        except Exception:
            log.exception("agent failed")
            del self.history[start:]
            return "Sorry sir, something went wrong."
        self._trim()
        return answer

    def _loop(self) -> str:
        tools = [s.schema for s in self.registry.values()]
        calls = 0
        while calls < self.max_tool_calls:
            reply = self.llm.chat(self._messages(), tools)
            self.history.append(reply.message)
            if not reply.tool_calls:
                return (reply.text or "").strip() or "Done, sir."
            for call in reply.tool_calls:
                calls += 1
                # Every tool call needs a tool message, even past the cap.
                result = self._run(call) if calls <= self.max_tool_calls else "error: tool call limit reached"
                self.history.append({"role": "tool", "tool_call_id": call["id"], "content": result})
        reply = self.llm.chat(self._messages(), [])  # no tools: force a spoken answer
        text = (reply.text or "").strip() or "Done, sir."
        # Store plain text only: a stray tool_calls message here would have no tool results and break later requests.
        self.history.append({"role": "assistant", "content": text})
        return text

    def _run(self, call: dict) -> str:
        entry = self.registry.get(call["name"])
        if entry is None:
            return f"error: unknown tool {call['name']}"
        if entry.confirm and not self.confirm(call["name"].replace("_", " ")):
            return "cancelled by user"
        try:
            return str(entry.func(**_coerce(call["arguments"], entry.schema)))
        except Exception as e:
            log.exception("skill %s failed", call["name"])
            return f"error: {e}"

    def _messages(self) -> list[dict]:
        system = SYSTEM_PROMPT.format(now=datetime.now().strftime("%A %d %B %Y, %I:%M %p"))
        return [{"role": "system", "content": system}] + self.history

    def _trim(self) -> None:
        user_idx = [i for i, m in enumerate(self.history) if m["role"] == "user"]
        if len(user_idx) > self.max_turns:
            # Cut at a user message so tool calls and their results stay paired.
            self.history = self.history[user_idx[-self.max_turns]:]
