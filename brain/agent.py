import logging
import platform
import re
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
    "You are EDITH, a voice assistant running on the user's {os} computer. Address the user as sir. "
    "Your replies are spoken aloud: answer in one to three short sentences, with no markdown, lists or emojis. "
    "Think like a thoughtful human assistant: work out what the user actually means, even from casual or "
    "half-finished phrasing; for anything with several steps, plan it and do the steps in order with the tools; "
    "when a request is genuinely unclear, ask one short question instead of guessing. "
    "Use the tools to act on the computer or fetch information, and never say you did something a tool didn't confirm. "
    "Current date and time: {now}."
)
MEMORY_PROMPT = "\n\nWhat you have learned about the user (use it, don't recite it):\n{facts}"
LEARN_PROMPT = (
    "Before we finish: list any new lasting facts worth remembering about me from this conversation - "
    "my name, people in my life, preferences, habits, or corrections to how you should behave. "
    "One short third-person line each, e.g. 'User prefers Celsius.'. Skip temporary things and anything "
    "you were already told above. If there is nothing new, reply NONE."
)
OS_NAMES = {"Windows": "Windows", "Darwin": "macOS", "Linux": "Linux"}


# "open notepad and type hi": offline, these still go back to the model for the next step.
MULTI_STEP = re.compile(r"\b(and|then|after that|also)\b", re.IGNORECASE)


def spoken(result: str) -> str:
    """A tool result read out as EDITH's answer; offline mode skips the model's second, slow pass."""
    if result == "cancelled by user":
        return "Okay, cancelled, sir."
    if result.startswith("error: "):
        result = "Sorry sir, " + result[len("error: "):]
    lines = [line.strip() for line in result.splitlines() if line.strip()]
    return " ".join(line if line[-1] in ".!?:" else line + "." for line in lines) or "Done, sir."


class Agent:
    def __init__(self, llm, confirm: Callable[[str], bool], registry: dict[str, Skill] = REGISTRY,
                 max_tool_calls: int = 6, max_turns: int = 10, memory=None):
        self.llm = llm
        self.confirm = confirm  # asks the user out loud, returns True only on "yes"
        self.registry = registry
        self.memory = memory  # brain.memory.Memory, or None
        self.max_tool_calls = max_tool_calls
        self.max_turns = max_turns
        self.history: list[dict] = []

    def reset(self) -> None:
        self.history = []

    def learn(self) -> list[str]:
        """Ask the brain what this conversation taught it about the user and store it: EDITH's day-to-day learning."""
        if self.memory is None or not any(m["role"] == "user" for m in self.history):
            return []
        reply = self.llm.chat(self._messages() + [{"role": "user", "content": LEARN_PROMPT}], [])
        lines = [line.strip(" -*•\t") for line in (reply.text or "").splitlines()]
        learned = [f for f in lines if f and f.upper().rstrip(".") != "NONE" and self.memory.add(f)]
        if learned:
            log.info("learned: %s", learned)
        return learned

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
                log.info("reply: %s", reply.text)
                return (reply.text or "").strip() or "Done, sir."
            results = []
            for call in reply.tool_calls:
                calls += 1
                log.info("tool: %s %s", call["name"], call["arguments"])
                # Every tool call needs a tool message, even past the cap.
                result = self._run(call) if calls <= self.max_tool_calls else "error: tool call limit reached"
                self.history.append({"role": "tool", "tool_call_id": call["id"], "content": result})
                results.append(result)
            offline = getattr(self.llm, "offline", None)
            asked = next((m["content"] for m in reversed(self.history) if m["role"] == "user"), "")
            if offline and offline() and not MULTI_STEP.search(asked):
                # The local model needs 5-20 s a call on a laptop CPU: speak the result instead of asking it again.
                text = " ".join(spoken(r) for r in results)
                log.info("reply: %s", text)
                self.history.append({"role": "assistant", "content": text})
                return text
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
        system = SYSTEM_PROMPT.format(os=OS_NAMES.get(platform.system(), platform.system()),
                                      now=datetime.now().strftime("%A %d %B %Y, %I:%M %p"))
        facts = self.memory.facts() if self.memory else []
        if facts:
            system += MEMORY_PROMPT.format(facts="\n".join(f"- {f}" for f in facts))
        return [{"role": "system", "content": system}] + self.history

    def _trim(self) -> None:
        user_idx = [i for i, m in enumerate(self.history) if m["role"] == "user"]
        if len(user_idx) > self.max_turns:
            # Cut at a user message so tool calls and their results stay paired.
            self.history = self.history[user_idx[-self.max_turns]:]
