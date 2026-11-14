import threading
from datetime import datetime
from pathlib import Path

from skills import skill

NOTES_FILE = Path(__file__).resolve().parent.parent / "data" / "notes.txt"
SECONDS_PER_MINUTE = 60

# main.py replaces this with the speaker so reminders are spoken aloud.
announce = print


@skill()
def set_reminder(minutes: float, message: str) -> str:
    """Remind the user with a spoken message after a number of minutes."""
    if minutes <= 0:
        return "error: minutes must be more than zero"
    # shortcut: reminders live in memory and are lost on restart; persist to a file if that matters.
    timer = threading.Timer(minutes * SECONDS_PER_MINUTE, lambda: announce(f"Reminder, sir: {message}"))
    timer.daemon = True
    timer.start()
    return f"Reminder set for {minutes:g} minutes."


@skill()
def add_note(text: str) -> str:
    """Save a note for the user."""
    NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with NOTES_FILE.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M} {text}\n")
    return "Note saved."


@skill()
def read_notes() -> str:
    """Read back the user's saved notes."""
    if not NOTES_FILE.exists() or not NOTES_FILE.read_text(encoding="utf-8").strip():
        return "You have no notes."
    return NOTES_FILE.read_text(encoding="utf-8").strip()
