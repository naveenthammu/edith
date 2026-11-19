"""Instant commands: clear, common requests run their tool straight away, without asking a model.

A model call takes 5-10 s online and minutes offline on a laptop CPU; these take milliseconds.
Each pattern must match the whole (cleaned-up) sentence, so anything longer or vaguer
("open notepad and type hi", "what's the weather like") still goes to the model.
"""
import re

LEAD = ("hey ", "hi ", "ok ", "okay ", "edith ", "please ", "can you ", "could you ", "would you ", "will you ")
TRAIL = (" please", " now", " for me", " edith")


def normalize(text: str) -> str:
    t = " ".join(re.sub(r"[^a-z0-9%' ]+", " ", text.lower()).split())
    changed = True
    while changed:
        changed = False
        for w in LEAD:
            if t.startswith(w):
                t, changed = t[len(w):], True
        for w in TRAIL:
            if t.endswith(w):
                t, changed = t[:-len(w)], True
    return t


def _steps(by: str | None) -> int:
    return max(1, int(by) // 2) if by else 5  # one volume key press is 2 percent


def _open(name: str):
    from skills.computer import HOME, user_path
    from skills.web import resolve_app

    if resolve_app(name):
        return "open_app", {"name": name}
    p = user_path(name)
    if " " not in name and p.parent == HOME and p.is_dir():  # "open downloads": a folder right under home
        return "open_path", {"path": name}
    return None  # "open my resume": let the model work out what is meant


PCT = r"(\d{1,3})(?: ?%| percent)?"
RADIO = {"wifi": "wifi", "wi fi": "wifi", "bluetooth": "bluetooth"}
WINDOW = {"minimise": "minimize", "minimize": "minimize", "maximise": "maximize", "maximize": "maximize",
          "close": "close"}
RULES = [
    (r"what time is it|(?:what(?:'s| is) the )?time|what(?:'s| is) the (?:date|day)(?: today)?|what day is it(?: today)?"
     r"|(?:what(?:'s| is) )?today's date", lambda m: ("get_datetime", {})),
    (r"(?:what(?:'s| is) )?(?:my |the )?battery(?: level| status| percentage)?|how much battery(?: is left| do i have)?",
     lambda m: ("battery", {})),
    (r"how(?:'s| is) my (?:system|pc|computer|laptop)(?: doing)?|system (?:info|status)", lambda m: ("system_info", {})),
    (r"(?:turn )?(?:the )?volume (up|down)(?: by (\d+)(?: ?%| percent| steps?)?)?",
     lambda m: ("volume", {"action": m[1], "steps": _steps(m[2])})),
    (r"(increase|raise|decrease|lower|reduce) (?:the )?volume(?: by (\d+)(?: ?%| percent)?)?",
     lambda m: ("volume", {"action": "up" if m[1] in ("increase", "raise") else "down", "steps": _steps(m[2])})),
    (r"(?:set |change |put |turn )?(?:the )?volume (?:to |at )?" + PCT, lambda m: ("set_volume", {"percent": int(m[1])})),
    (r"(?:un)?mute(?: the)?(?: volume| sound| audio)?", lambda m: ("volume", {"action": "mute"})),
    (r"(?:set |change |turn )?(?:the )?(?:screen )?brightness (?:to |at )?" + PCT,
     lambda m: ("brightness", {"level": int(m[1])})),
    (r"turn (on|off) (?:the )?(wifi|wi fi|bluetooth)", lambda m: (RADIO[m[2]], {"state": m[1]})),
    (r"turn (?:the )?(wifi|wi fi|bluetooth) (on|off)", lambda m: (RADIO[m[1]], {"state": m[2]})),
    (r"(minimise|minimize|maximise|maximize|close) (?:this |the )?window", lambda m: ("window", {"action": WINDOW[m[1]]})),
    (r"show (?:me )?(?:the )?desktop", lambda m: ("window", {"action": "desktop"})),
    (r"close ([a-z0-9]{3,30})", lambda m: None if m[1] in ("all", "everything") else ("close_app", {"name": m[1]})),
    (r"open (?:the |my )?([a-z0-9 .+-]{2,40}?)(?: app)?", lambda m: _open(m[1])),
    (r"(?:take (?:a )?)?screen ?shot", lambda m: ("screenshot", {})),
    (r"lock (?:the |my )?(?:pc|computer|laptop|screen)", lambda m: ("lock_pc", {})),
    (r"cancel (?:the )?shutdown", lambda m: ("cancel_shutdown", {})),
    (r"(?:take|add|make|write) (?:a )?note(?: that)? (.+)|note that (.+)", lambda m: ("add_note", {"text": m[1] or m[2]})),
    (r"(?:read|show) (?:me )?(?:my |the )?notes|what are my notes", lambda m: ("read_notes", {})),
    (r"remind me in (\d+) (minutes?|mins?|hours?) to (.+)",
     lambda m: ("set_reminder", {"minutes": int(m[1]) * (60 if m[2].startswith("hour") else 1), "message": m[3]})),
    (r"remind me to (.+) in (\d+) (minutes?|mins?|hours?)",
     lambda m: ("set_reminder", {"minutes": int(m[2]) * (60 if m[3].startswith("hour") else 1), "message": m[1]})),
    (r"(?:what(?:'s| is) the |how(?:'s| is) the )?weather (?:in|at|for) ([a-z .]{2,40}?)(?: today)?",
     lambda m: ("get_weather", {"city": m[1]})),
    (r"(?:switch|go|change|turn) (?:to |back to )?(online|offline)(?: mode)?", lambda m: ("set_brain_mode", {"mode": m[1]})),
    (r"play (.+?) on youtube", lambda m: ("play_youtube", {"query": m[1]})),
]
RULES = [(re.compile(p), build) for p, build in RULES]


def match(text: str) -> tuple[str, dict] | None:
    """(tool name, arguments) for a clear, common command, or None to ask the model."""
    t = normalize(text)
    for pattern, build in RULES:
        m = pattern.fullmatch(t)
        if m:
            return build(m)
    return None
