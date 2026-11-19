"""Teach-by-showing: the user does a task once while EDITH watches, then EDITH replays it by voice.

Recording uses pynput; Claude turns the raw events into readable steps saved in data/taught/<slug>/skill.json.
Replay finds each click by exact picture match, then Claude vision, then the recorded spot in the window.
"""
import io
import json
import logging
import re
import shutil
import threading
import time
from pathlib import Path

from skills import skill
from skills.computer import OS, press_keys, type_text
from skills.screen import VISION_MAX_SIDE, focus_window

log = logging.getLogger(__name__)
TAUGHT = Path(__file__).resolve().parent.parent / "data" / "taught"
STEP_KINDS = {"focus", "click", "type", "keys", "wait"}
CHORD_MODS = {"ctrl", "alt", "win", "command"}  # shift alone just changes the letter typed
CROP = 120  # px square saved around each click
PRIVATE = ("password", "sign in", "log in", "login")
OWN_WINDOW = "EDITH"  # the overlay's title
STEP_PAUSE = 0.4  # seconds between replayed steps, so apps keep up
MAX_WAIT = 10
MOD_NAMES = {"ctrl_l": "ctrl", "ctrl_r": "ctrl", "ctrl": "ctrl", "alt_l": "alt", "alt_r": "alt", "alt_gr": "alt",
             "alt": "alt", "shift": "shift", "shift_l": "shift", "shift_r": "shift",
             "cmd": "win", "cmd_l": "win", "cmd_r": "win"}
COMPILE_PROMPT = (
    "The user showed a desktop assistant how to do a task. Below are the recorded events, in order. "
    "The images are the 120x120 crops around each click, in the same order as the click events. "
    "Turn this into a short replayable recipe. Reply with JSON only, shaped as "
    '{"description": "<what the task does, under 10 words>", "steps": [...]} where each step is exactly one of: '
    '{"focus": "<app or window name>"}, {"click": "<what is clicked, e.g. Save button>", "crop": "<crop file>", '
    '"rel": [<x fraction of window>, <y fraction of window>]}, {"type": "<text>"}, {"keys": "<e.g. ctrl+s>"}, '
    '{"wait": <seconds>}. Drop accidental or repeated actions. Add a wait after anything that loads. '
    "If typed text is clearly a value that changes each time (a date, month, name, amount), "
    "replace it with a {variable_name} placeholder.\n\nEvents:\n")


# ---------- pure helpers ----------

def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def group_keys(events: list[dict]) -> list[dict]:
    """Merge single key events into typed text and shortcut chords; other events pass through."""
    out, text = [], ""

    def flush():
        nonlocal text
        if text:
            out.append({"kind": "type", "text": text})
            text = ""

    for e in events:
        if e["kind"] != "key":
            flush()
            out.append(e)
            continue
        key, mods = e["key"], [m for m in e["mods"] if m in CHORD_MODS]
        if not mods and (len(key) == 1 or key == "space"):
            text += " " if key == "space" else key
        elif not mods and key == "backspace" and text:
            text = text[:-1]
        else:
            flush()
            out.append({"kind": "keys", "keys": "+".join(mods + [key])})
    flush()
    return out


def parse_compiled(text: str) -> dict:
    """Check Claude's compiled steps; raise ValueError if they aren't usable."""
    start, end = text.find("{"), text.rfind("}")
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        raise ValueError("the answer wasn't JSON") from None
    steps = data.get("steps") if isinstance(data, dict) else None
    if not isinstance(steps, list) or not steps:
        raise ValueError("no steps")
    for s in steps:
        if not isinstance(s, dict) or len(STEP_KINDS & s.keys()) != 1:
            raise ValueError(f"bad step {s}")
    variables = sorted({v for s in steps if "type" in s for v in re.findall(r"\{(\w+)\}", str(s["type"]))})
    return {"description": str(data.get("description", "")), "variables": variables, "steps": steps}


def parse_values(values: str, variables: list[str]) -> dict:
    """'month=March; year=2026', or a bare value when there is one variable. ValueError names what's missing."""
    values = values.strip()
    if values and "=" not in values and len(variables) == 1:
        pairs = {variables[0]: values}
    else:
        pairs = {k.strip(): v.strip() for k, _, v in (p.partition("=") for p in values.split(";")) if k.strip()}
    missing = [v for v in variables if v not in pairs]
    if missing:
        raise ValueError(", ".join(missing))
    return pairs


def fill(text: str, pairs: dict) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: pairs.get(m[1], m[0]), text)


# ---------- recording ----------

def _active() -> tuple[str, list | None]:
    """Title and [left, top, width, height] of the focused window."""
    if OS != "Windows":
        return "", None  # shortcut: window tracking is Windows-only; macOS/Linux replay relies on pictures and vision
    import pygetwindow

    w = pygetwindow.getActiveWindow()
    return (w.title, [w.left, w.top, w.width, w.height]) if w else ("", None)


def _skip(title: str) -> bool:
    t = title.lower()
    return title == OWN_WINDOW or any(p in t for p in PRIVATE)


def _key_name(key) -> str:
    name = getattr(key, "name", None)
    if name:
        return MOD_NAMES.get(name, name.replace("_", ""))  # page_down -> pagedown, as pyautogui spells it
    ch = getattr(key, "char", None)
    if ch and ord(ch) < 32 and getattr(key, "vk", None):  # ctrl+letter arrives as a control character
        return chr(key.vk).lower()
    return ch or ""


class Recorder:
    """Collects clicks (with a crop of what was clicked), key presses and window changes."""

    def __init__(self, folder: Path):
        self.dir, self.events, self.mods = folder, [], set()
        self._stop = threading.Event()

    def start(self) -> None:
        from pynput import keyboard, mouse

        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True)
        self._k = keyboard.Listener(on_press=self._press, on_release=self._release)
        self._m = mouse.Listener(on_click=self._click)
        self._k.start()
        self._m.start()
        threading.Thread(target=self._watch_focus, daemon=True, name="teach-focus").start()

    def stop(self) -> None:
        self._stop.set()
        self._k.stop()
        self._m.stop()

    def _add(self, event: dict) -> None:
        self.events.append({**event, "t": round(time.monotonic(), 2)})

    def _click(self, x, y, button, pressed):
        if not pressed:
            return
        title, rect = _active()
        if _skip(title):
            return
        import pyautogui

        name = f"{len(self.events)}.png"
        pyautogui.screenshot(region=(max(x - CROP // 2, 0), max(y - CROP // 2, 0), CROP, CROP)).save(self.dir / name)
        self._add({"kind": "click", "x": x, "y": y, "button": button.name, "title": title, "rect": rect, "crop": name})

    def _press(self, key):
        name = _key_name(key)
        if name in ("ctrl", "alt", "shift", "win"):
            self.mods.add(name)
        elif name and not _skip(_active()[0]):
            self._add({"kind": "key", "key": name, "mods": sorted(self.mods)})

    def _release(self, key):
        self.mods.discard(_key_name(key))

    def _watch_focus(self):
        last = None
        while not self._stop.wait(0.3):
            title, rect = _active()
            if title != last and title and not _skip(title):
                self._add({"kind": "focus", "title": title, "rect": rect})
            last = title


_rec: Recorder | None = None


def _seer():
    """The online model that can see images, or None when offline."""
    from skills import assistant

    llm = assistant.llm
    return llm.remote.vision() if llm and llm.remote and not llm.force_offline else None


@skill()
def start_teaching() -> str:
    """Start watching the user do a task on the computer, so it can be learned and repeated later.
    Use for 'watch me', 'learn this', 'let me show you'."""
    global _rec
    if _rec is not None:
        return "I'm already watching, sir. Say done when you're finished."
    _rec = Recorder(TAUGHT / "_recording")
    _rec.start()
    return "Watching. Do the task, then say 'Edith, done' and give it a name."


@skill()
def cancel_teaching() -> str:
    """Stop watching without learning anything."""
    global _rec
    if _rec is None:
        return "I wasn't watching anything, sir."
    _rec.stop()
    shutil.rmtree(_rec.dir, ignore_errors=True)
    _rec = None
    return "Okay, I stopped watching."


@skill()
def finish_teaching(name: str) -> str:
    """Stop watching and learn what the user just did as a task with this name, e.g. 'Monday report'."""
    global _rec
    from brain.llm import REMOTE_ERRORS

    if _rec is None:
        return "error: I'm not watching anything. Say 'watch me' first."
    if not slug(name):  # an empty slug would point at the whole taught folder and delete every task
        return "error: give the task a name with English letters or numbers, sir."
    _rec.stop()
    events = group_keys(_rec.events)
    if not events:
        return cancel_teaching() + " I didn't see you do anything."
    seer = _seer()
    offline = "I need internet to learn this, sir. Say done again when we're online, or cancel."
    if seer is None:
        return offline
    crops = [(_rec.dir / e["crop"]).read_bytes() for e in events if e["kind"] == "click"]
    try:
        recipe = parse_compiled(seer.ask_images(crops, COMPILE_PROMPT + json.dumps(events), 4096))
    except REMOTE_ERRORS:
        return offline
    except ValueError as e:
        return f"error: I couldn't turn that into steps ({e}). Say done to try again, or cancel."
    dest = TAUGHT / slug(name)
    shutil.rmtree(dest, ignore_errors=True)
    shutil.move(str(_rec.dir), str(dest))
    (dest / "skill.json").write_text(json.dumps({"name": name, **recipe}, indent=2))
    _rec = None
    needs = f" It needs: {', '.join(recipe['variables'])}." if recipe["variables"] else ""
    return f"Learned {name}: {recipe['description']}.{needs} Say 'do {name}' any time."


# ---------- replay ----------

def find_click(step: dict, folder: Path, seer, rect: list | None) -> tuple[int, int] | None:
    """Where to click now: exact picture match, then Claude vision, then the recorded spot in the window."""
    import pyautogui

    crop = folder / step.get("crop", "")
    if step.get("crop") and crop.exists():
        try:
            box = pyautogui.locateOnScreen(str(crop))
        except Exception:  # pyscreeze raises ImageNotFoundException instead of returning None in newer versions
            box = None
        if box:
            return box.left + box.width // 2, box.top + box.height // 2
        if seer is not None:
            from brain.llm import REMOTE_ERRORS

            shot = pyautogui.screenshot()
            full_w = shot.width
            shot.thumbnail((VISION_MAX_SIDE, VISION_MAX_SIDE))
            buf = io.BytesIO()
            shot.save(buf, "PNG")
            try:
                reply = seer.ask_images([buf.getvalue(), crop.read_bytes()], (
                    f"The second image shows a UI element: {step['click']}. Find it in the first image, "
                    f"a {shot.width}x{shot.height} screenshot. Reply only with the pixel x,y of its centre, or none."))
            except REMOTE_ERRORS:
                reply = ""
            found = re.search(r"(\d+)\s*,\s*(\d+)", reply)
            if found:
                scale = full_w / shot.width
                return round(int(found[1]) * scale), round(int(found[2]) * scale)
    if rect and step.get("rel"):
        left, top, w, h = rect
        return round(left + step["rel"][0] * w), round(top + step["rel"][1] * h)
    return None


@skill(confirm=True)
def do_taught(name: str, values: str = "") -> str:
    """Do a task the user taught by showing (see the taught tasks list). values fills its blanks,
    e.g. 'month=March' or just 'March' when it has one."""
    import pyautogui

    folder = TAUGHT / slug(name)
    if not (folder / "skill.json").exists():
        return f"error: I don't know a task called {name}. {list_taught()}"
    task = json.loads((folder / "skill.json").read_text())
    try:
        pairs = parse_values(values, task.get("variables", []))
    except ValueError as e:
        return f"error: tell me the {e} for {task['name']}"
    seer, rect, steps = _seer(), None, task["steps"]
    for i, step in enumerate(steps, 1):
        try:
            if "focus" in step:
                if not focus_window(step["focus"]):
                    return f"error: step {i}: no open window for {step['focus']}"
                rect = _active()[1]
            elif "click" in step:
                pos = find_click(step, folder, seer, rect)
                if pos is None:
                    return f"error: step {i}: I couldn't find {step['click']}"
                pyautogui.click(*pos)
            elif "type" in step:
                type_text(fill(str(step["type"]), pairs))
            elif "keys" in step:
                result = press_keys(str(step["keys"]))
                if result.startswith("error"):
                    return f"error: step {i}: {result}"
            elif "wait" in step:
                time.sleep(min(float(step["wait"]), MAX_WAIT))
        except pyautogui.FailSafeException:
            return f"Stopped at step {i} of {len(steps)}, sir."
        time.sleep(STEP_PAUSE)
    return f"Done: {task['name']}."


# ---------- managing ----------

def _tasks() -> list[dict]:
    if not TAUGHT.exists():
        return []
    out = []
    for f in sorted(TAUGHT.glob("*/skill.json")):
        if f.parent.name == "_recording":
            continue
        try:  # every prompt reads these: one broken file must not break EDITH
            out.append(json.loads(f.read_text()))
        except ValueError:
            log.warning("skipping unreadable taught task %s", f)
    return out


def _line(t: dict) -> str:
    needs = f", needs {', '.join(t['variables'])}" if t.get("variables") else ""
    return f"{t['name']} ({t.get('description', '')}{needs})"


def taught_summary() -> str:
    """Extra system-prompt text naming the taught tasks; kept out of tool descriptions so the offline cache holds."""
    tasks = _tasks()
    if not tasks:
        return ""
    return ("\n\nTasks the user taught you by showing; run them with do_taught: "
            + "; ".join(_line(t) for t in tasks) + ".")


@skill()
def list_taught() -> str:
    """List the tasks the user has taught by showing."""
    tasks = _tasks()
    if not tasks:
        return "You haven't taught me any tasks yet. Say 'watch me' to start."
    return "You've taught me: " + "; ".join(_line(t) for t in tasks) + "."


@skill(confirm=True)
def forget_taught(name: str) -> str:
    """Delete a task the user taught by showing."""
    folder = TAUGHT / slug(name)
    if not (folder / "skill.json").exists():
        return f"error: I don't know a task called {name}."
    real = json.loads((folder / "skill.json").read_text())["name"]
    shutil.rmtree(folder)
    return f"Forgot {real}."
