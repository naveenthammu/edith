"""Configured family wording and finite, curated games with no model or I/O.

Roles describe the paired device configuration, not recognized voice identity.
Prompts are communication guidance, never authorization or a content filter.
The caller must apply tool permissions separately and use child_fallback for
unsupported child conversation instead of sending it to an open-ended model.
Create one OfflineGames instance per conversation; reset it on role/session
changes. Only question position, turn count and score are held in memory.
"""
import re
from dataclasses import dataclass

_ROLES = frozenset({"adult", "elder", "child", "guest"})

_CHILD_PROMPT = (
    "This device is configured for child mode; that does not verify who is speaking. "
    "Use kind, age-appropriate, plain language and at most one short question at a time. "
    "Do not collect names, contact details, locations, photos, passwords, or other personal information. "
    "Never ask a child to keep secrets, hide a conversation, or depend on you instead of trusted people. "
    "Games use only the separately implemented curated offline questions. Do not generate new game "
    "content, browse for content, or invent stories in this mode. Unsupported conversation uses the "
    "fixed child fallback. For concerns beyond a game, direct the child to a parent or trusted adult. "
    "Do not diagnose, prescribe, or make medical claims."
)
_ELDER_PROMPT = (
    "This device is configured for elder mode; that does not verify who is speaking. "
    "Use respectful, plain, patient language without talking down to the person or assuming ability. "
    "Give one short step at a time and ask at most one question before continuing. "
    "Offer to repeat an explanation when useful, and accept stop or cancel immediately. "
    "Never claim an action completed without a confirmed result. Use the existing exact-action "
    "approval process for consequential actions; configured age is not consent or identity. "
    "Do not diagnose, prescribe, claim to monitor health, or make medical claims."
)


def _validate_role(role):
    if not isinstance(role, str) or role not in _ROLES:
        raise ValueError("Use a configured adult, elder, child, or guest role")


def role_prompt(role: str) -> str:
    """Return communication guidance for an already authorized configured role."""
    _validate_role(role)
    return {"child": _CHILD_PROMPT, "elder": _ELDER_PROMPT}.get(role, "")


def child_fallback() -> str:
    """Fixed local response for child requests outside the curated games."""
    return (
        "We can play a short word game or maths game. Say start word game or start math game. "
        "For anything else, please ask a parent or trusted adult."
    )


@dataclass(frozen=True)
class _Question:
    prompt: str
    answer: str
    accepted: frozenset[str]


_WORD_QUESTIONS = (
    _Question("Which animal says meow?", "cat", frozenset({"cat", "a cat", "the cat"})),
    _Question("What is water called when it freezes into a cube?", "ice", frozenset({"ice", "an ice cube", "ice cube"})),
    _Question("What is the opposite of hot?", "cold", frozenset({"cold"})),
    _Question("Which long yellow fruit do you peel before eating?", "banana", frozenset({"banana", "a banana", "bananas"})),
    _Question("Which shape has three sides?", "triangle", frozenset({"triangle", "a triangle", "the triangle"})),
)
_MATH_QUESTIONS = (
    _Question("What is 2 plus 3?", "5", frozenset({"5", "five"})),
    _Question("What is 7 minus 3?", "4", frozenset({"4", "four"})),
    _Question("What is 4 plus 4?", "8", frozenset({"8", "eight"})),
    _Question("What is 9 minus 3?", "6", frozenset({"6", "six"})),
    _Question("What is 10 plus 2?", "12", frozenset({"12", "twelve"})),
)
_STARTS = {
    "start word game": _WORD_QUESTIONS,
    "play word game": _WORD_QUESTIONS,
    "start math game": _MATH_QUESTIONS,
    "play math game": _MATH_QUESTIONS,
    "start maths game": _MATH_QUESTIONS,
    "play maths game": _MATH_QUESTIONS,
}
_STOPS = frozenset({"stop", "stop game", "cancel", "cancel game", "end game", "quit", "exit", "i'm done"})
_REPEATS = frozenset({"repeat", "repeat question", "say again"})
_MAX_TURNS = 12


def _normalize(text):
    if not isinstance(text, str) or len(text) > 160:
        return None
    normalized = " ".join(text.lower().strip(" .!?,\t\r\n").split())
    return normalized if re.fullmatch(r"[a-z0-9 ']+", normalized) else None


class OfflineGames:
    """Five curated questions, at most twelve inputs, and no retained answers.

    handle returns None only when idle and the input is not a game command.
    While active, every input is consumed locally; callers must not fall back
    to a model on an incorrect/unsupported answer. Repeating or trying to start
    another game consumes the turn allowance and cannot restart that allowance.
    """

    def __init__(self, role: str = "child"):
        _validate_role(role)
        self.role = role
        self.reset()

    def reset(self) -> None:
        self._questions = ()
        self._index = 0
        self._score = 0
        self._turns = 0

    @property
    def active(self) -> bool:
        return bool(self._questions)

    def _question(self) -> str:
        return f"Question {self._index + 1} of {len(self._questions)}. {self._questions[self._index].prompt}"

    def handle(self, text: str) -> str | None:
        command = _normalize(text)
        if not self.active:
            questions = _STARTS.get(command)
            if questions is not None:
                self.reset()
                self._questions = questions
                return self._question()
            if command in {"start game", "play game", "play a game"}:
                return "Say start word game or start math game."
            return None

        if command in _STOPS:
            self.reset()
            return "Game stopped."
        self._turns += 1
        if self._turns >= _MAX_TURNS:
            self.reset()
            return "This game has reached its turn limit. We will stop here."
        if command in _STARTS:
            return "A game is already running. Say stop to finish it. " + self._question()
        if command in _REPEATS:
            return self._question()
        if command == "help":
            return "Say one answer, repeat, or stop. " + self._question()
        if command is None or len(command) > 64:
            return "Please say one short answer, repeat, or stop."

        question = self._questions[self._index]
        correct = command in question.accepted
        self._score += int(correct)
        self._index += 1
        feedback = "Correct. " if correct else f"The answer is {question.answer}. "
        if self._index == len(self._questions):
            summary = f"Game finished. You answered {self._score} out of {len(self._questions)} correctly. Thanks for playing."
            self.reset()
            return summary if correct else feedback + summary
        return feedback + self._question()
