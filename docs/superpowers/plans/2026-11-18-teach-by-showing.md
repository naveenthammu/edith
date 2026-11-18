# Teach-by-showing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The user does a task once while EDITH watches. EDITH saves it as a named skill and can replay it by voice later, with new values.

**Architecture:** A new module, `skills/teach.py`, holds four parts:
- a `pynput` recorder,
- pure helpers for key grouping, parsing, slugs and values,
- a compile step that sends the events and crops to Claude through the new `ClaudeRemote.ask_images`,
- a replay engine. Each click is located by exact image match first, then Claude vision, then the recorded window-relative position.

Taught tasks are listed in the agent's system prompt, after memory, not in the tool descriptions. The tool list stays fixed, so the offline model's saved prompt cache, which is keyed by the tool list, stays valid.

**Tech Stack:** Python 3.12, pynput, pyautogui (pyscreeze `locateOnScreen`), Pillow, the Anthropic SDK, pytest.

**Spec:** `docs/superpowers/specs/2026-11-18-teach-by-showing-design.md`

**Deviation from spec:** The spec rewrites the `do_taught` tool description. This plan puts the taught-task list in the system prompt instead, because changing a tool description invalidates the offline prompt cache and costs about 2 minutes to rebuild.

---

## File map

- Create `skills/teach.py`: the recorder, helpers, and the skills `start_teaching`, `cancel_teaching`, `finish_teaching`, `do_taught`, `list_taught`, `forget_taught`, plus `taught_summary()`.
- Create `tests/test_teach.py`.
- Modify `brain/llm.py`: add `ClaudeRemote.ask_images`.
- Modify `brain/agent.py`: append `taught_summary()` in `_messages`.
- Modify `skills/__init__.py`: import `teach` in `load_all`.
- Modify `requirements.txt`: add `pynput>=1.7`.
- Modify `README.md`: add a "Teach EDITH a task" section.

Taught skills live in `data/taught/<slug>/`. `data/` is already in `.gitignore`.

---

### Task 1: `ClaudeRemote.ask_images`

**Files:** Modify `brain/llm.py` (after `describe_image`). Test: `tests/test_llm.py`.

- [ ] **Step 1: Failing test** (append to `tests/test_llm.py`)

```python
def test_ask_images_sends_every_image_then_prompt():
    from types import SimpleNamespace
    from brain.llm import ClaudeRemote

    sent = {}

    class Msgs:
        def create(self, **kw):
            sent.update(kw)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="12,34")])

    remote = ClaudeRemote(SimpleNamespace(messages=Msgs()), "m")
    assert remote.ask_images([b"a", b"b"], "where?") == "12,34"
    blocks = sent["messages"][0]["content"]
    assert [b["type"] for b in blocks] == ["image", "image", "text"]
    assert blocks[-1]["text"] == "where?"
```

- [ ] **Step 2:** Run `venv/Scripts/python -m pytest tests/test_llm.py -k ask_images -q`. Expected: FAIL with `AttributeError: 'ClaudeRemote' object has no attribute 'ask_images'`.

- [ ] **Step 3: Implement** (in `brain/llm.py`, directly after `describe_image`)

```python
    def ask_images(self, images: list[bytes], prompt: str, max_tokens: int = 1024) -> str:
        """Send PNG images then a prompt; return Claude's raw text (no spoken-answer shaping)."""
        blocks = [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                               "data": base64.standard_b64encode(png).decode()}} for png in images]
        resp = self.client.messages.create(
            model=self.model, max_tokens=max_tokens, output_config={"effort": "low"}, timeout=REMOTE_TIMEOUT,
            messages=[{"role": "user", "content": blocks + [{"type": "text", "text": prompt}]}],
        )
        return " ".join(b.text for b in resp.content if b.type == "text").strip()
```

- [ ] **Step 4:** Run the same test. Expected: PASS.
- [ ] **Step 5:** `git add brain/llm.py tests/test_llm.py && git commit -m "Let Claude read several images with a raw prompt"`

---

### Task 2: Pure helpers (slug, key grouping, parsing, values)

**Files:** Create `skills/teach.py`, `tests/test_teach.py`.

- [ ] **Step 1: Failing tests** (`tests/test_teach.py`)

```python
import pytest

from skills import teach


def k(key, *mods):
    return {"kind": "key", "key": key, "mods": list(mods)}


def test_slug():
    assert teach.slug("  Monday Report! ") == "monday-report"


def test_group_keys_merges_text_and_chords():
    events = [k("h"), k("i"), k("space"), k("X", "shift"), k("backspace"), k("s", "ctrl"), k("enter"),
              {"kind": "click", "x": 1, "y": 2}]
    assert teach.group_keys(events) == [
        {"kind": "type", "text": "hi "}, {"kind": "keys", "keys": "ctrl+s"},
        {"kind": "keys", "keys": "enter"}, {"kind": "click", "x": 1, "y": 2}]


def test_group_keys_backspace_with_no_pending_text_is_a_key():
    assert teach.group_keys([k("backspace")]) == [{"kind": "keys", "keys": "backspace"}]


def test_parse_compiled_reads_fenced_json_and_finds_variables():
    text = '```json\n{"description": "Report", "steps": [{"focus": "Excel"}, {"type": "{month} sales"}, {"keys": "ctrl+s"}]}\n```'
    d = teach.parse_compiled(text)
    assert d["description"] == "Report"
    assert d["variables"] == ["month"]
    assert len(d["steps"]) == 3


@pytest.mark.parametrize("text", ["not json", '{"steps": []}', '{"steps": [{"explode": 1}]}'])
def test_parse_compiled_rejects_bad_output(text):
    with pytest.raises(ValueError):
        teach.parse_compiled(text)


def test_parse_values():
    assert teach.parse_values("month=March; year = 2026", ["month", "year"]) == {"month": "March", "year": "2026"}
    assert teach.parse_values("March", ["month"]) == {"month": "March"}
    with pytest.raises(ValueError, match="month"):
        teach.parse_values("", ["month"])


def test_fill_leaves_unknown_braces():
    assert teach.fill("{month} {x}", {"month": "May"}) == "May {x}"
```

- [ ] **Step 2:** Run `venv/Scripts/python -m pytest tests/test_teach.py -q`. Expected: FAIL with `ImportError` (no `skills.teach`).

- [ ] **Step 3: Implement** (`skills/teach.py`, first part)

```python
"""Teach-by-showing: the user does a task once while EDITH watches, then EDITH replays it by voice.

Recording uses pynput; Claude turns the raw events into readable steps saved in data/taught/<slug>/skill.json.
"""
import json
import re

STEP_KINDS = {"focus", "click", "type", "keys", "wait"}
CHORD_MODS = {"ctrl", "alt", "win", "command"}  # shift alone just changes the letter typed


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
```

- [ ] **Step 4:** Run `venv/Scripts/python -m pytest tests/test_teach.py -q`. Expected: all PASS.
- [ ] **Step 5:** `git add skills/teach.py tests/test_teach.py && git commit -m "Teach: key grouping, step parsing and values"`

---

### Task 3: Recorder and the teaching skills (start, cancel, finish)

**Files:** Modify `skills/teach.py`, `tests/test_teach.py`, `skills/__init__.py`, `requirements.txt`.

- [ ] **Step 1: Failing tests** (append to `tests/test_teach.py`, and add `import json` at the top)

```python
class FakeRecorder:
    def __init__(self, folder):
        self.dir, self.events, self.stopped = folder, [], False

    def start(self):
        self.dir.mkdir(parents=True, exist_ok=True)

    def stop(self):
        self.stopped = True


class FakeSeer:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def ask_images(self, images, prompt, max_tokens=1024):
        self.calls.append((images, prompt))
        return self.reply


@pytest.fixture
def taught(tmp_path, monkeypatch):
    monkeypatch.setattr(teach, "TAUGHT", tmp_path)
    monkeypatch.setattr(teach, "Recorder", FakeRecorder)
    monkeypatch.setattr(teach, "_rec", None)
    return tmp_path


def test_finish_saves_compiled_skill(taught, monkeypatch):
    seer = FakeSeer('{"description": "Save report", "steps": [{"click": "Save", "crop": "0.png", "rel": [0.5, 0.5]}]}')
    monkeypatch.setattr(teach, "_seer", lambda: seer)
    teach.start_teaching()
    teach._rec.events = [{"kind": "click", "x": 5, "y": 5, "crop": "0.png", "title": "Excel", "rect": [0, 0, 10, 10]}]
    (teach._rec.dir / "0.png").write_bytes(b"png")
    assert teach.finish_teaching("Monday Report").startswith("Learned")
    saved = json.loads((taught / "monday-report" / "skill.json").read_text())
    assert saved["name"] == "Monday Report" and saved["steps"][0]["click"] == "Save"
    assert (taught / "monday-report" / "0.png").exists()
    assert seer.calls[0][0] == [b"png"]
    assert teach._rec is None


def test_finish_offline_keeps_recording(taught, monkeypatch):
    monkeypatch.setattr(teach, "_seer", lambda: None)
    teach.start_teaching()
    teach._rec.events = [{"kind": "key", "key": "a", "mods": []}]
    assert "internet" in teach.finish_teaching("x")
    assert teach._rec is not None


def test_finish_without_start(taught):
    assert teach.finish_teaching("x").startswith("error")


def test_cancel_discards(taught):
    teach.start_teaching()
    assert teach.cancel_teaching() == "Okay, I stopped watching."
    assert teach._rec is None
```

- [ ] **Step 2:** Run `venv/Scripts/python -m pytest tests/test_teach.py -q`. Expected: the new tests FAIL with `AttributeError` (`TAUGHT` / `start_teaching`).

- [ ] **Step 3: Implement** (add to `skills/teach.py`, with the imports merged at the top)

```python
import shutil
import threading
import time
from pathlib import Path

from skills import skill
from skills.computer import OS

TAUGHT = Path(__file__).resolve().parent.parent / "data" / "taught"
CROP = 120  # px square saved around each click
PRIVATE = ("password", "sign in", "log in", "login")
OWN_WINDOW = "EDITH"  # the overlay's title
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


def _active() -> tuple[str, list | None]:
    """Title and [left, top, width, height] of the focused window."""
    if OS != "Windows":
        return "", None  # shortcut: window tracking is Windows-only; macOS/Linux replay falls back to vision
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
```

`requirements.txt`: add `pynput>=1.7` after `pyautogui>=0.9.54`. `skills/__init__.py` `load_all`: add `teach` to the import list.

- [ ] **Step 4:** Run `venv/Scripts/python -m pip install "pynput>=1.7"`, then `venv/Scripts/python -m pytest tests/test_teach.py -q`. Expected: all PASS.
- [ ] **Step 5:** `git add skills/teach.py skills/__init__.py requirements.txt tests/test_teach.py && git commit -m "Teach: record a demo and compile it into steps with Claude"`

---

### Task 4: Replay (`do_taught`, click finding)

**Files:** Modify `skills/teach.py`, `tests/test_teach.py`.

- [ ] **Step 1: Failing tests** (append)

```python
import sys
from types import SimpleNamespace

from PIL import Image


class FakeGui:
    class FailSafeException(Exception):
        pass

    def __init__(self, box=None):
        self.box, self.clicks = box, []

    def locateOnScreen(self, path):
        return self.box

    def screenshot(self):
        return Image.new("RGB", (3136, 1000))

    def click(self, x, y):
        self.clicks.append((x, y))


def test_find_click_prefers_exact_match(tmp_path, monkeypatch):
    (tmp_path / "0.png").write_bytes(b"x")
    monkeypatch.setitem(sys.modules, "pyautogui", FakeGui(SimpleNamespace(left=10, top=20, width=120, height=120)))
    step = {"click": "Save", "crop": "0.png", "rel": [0.5, 0.5]}
    assert teach.find_click(step, tmp_path, None, [0, 0, 100, 100]) == (70, 80)


def test_find_click_uses_vision_scaled_back(tmp_path, monkeypatch):
    (tmp_path / "0.png").write_bytes(b"x")
    monkeypatch.setitem(sys.modules, "pyautogui", FakeGui())
    step = {"click": "Save", "crop": "0.png"}
    assert teach.find_click(step, tmp_path, FakeSeer("500, 300"), None) == (1000, 600)


def test_find_click_falls_back_to_window_position(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pyautogui", FakeGui())
    step = {"click": "Save", "crop": "missing.png", "rel": [0.5, 0.25]}
    assert teach.find_click(step, tmp_path, FakeSeer("none"), [100, 100, 200, 400]) == (200, 200)


def test_do_taught_runs_steps_with_values(taught, monkeypatch):
    folder = taught / "report"
    folder.mkdir()
    (folder / "skill.json").write_text(json.dumps({"name": "report", "description": "d", "variables": ["month"],
        "steps": [{"focus": "Excel"}, {"type": "{month} sales"}, {"keys": "ctrl+s"}, {"wait": 0}]}))
    done = []
    monkeypatch.setitem(sys.modules, "pyautogui", FakeGui())
    monkeypatch.setattr(teach, "_seer", lambda: None)
    monkeypatch.setattr(teach, "STEP_PAUSE", 0)
    monkeypatch.setattr(teach, "focus_window", lambda app: done.append(("focus", app)) or True)
    monkeypatch.setattr(teach, "type_text", lambda t: done.append(("type", t)) or "Typed.")
    monkeypatch.setattr(teach, "press_keys", lambda k: done.append(("keys", k)) or "Pressed.")
    assert teach.do_taught("Report", "March") == "Done: report."
    assert done == [("focus", "Excel"), ("type", "March sales"), ("keys", "ctrl+s")]


def test_do_taught_asks_for_missing_value(taught):
    folder = taught / "report"
    folder.mkdir()
    (folder / "skill.json").write_text(json.dumps({"name": "report", "variables": ["month"], "steps": [{"wait": 0}]}))
    assert teach.do_taught("report") == "error: tell me the month for report"


def test_do_taught_unknown(taught):
    assert teach.do_taught("nope").startswith("error: I don't know")
```

- [ ] **Step 2:** Run `venv/Scripts/python -m pytest tests/test_teach.py -q`. Expected: the new tests FAIL with `AttributeError` (`find_click` / `do_taught`).

- [ ] **Step 3: Implement** (append to `skills/teach.py`. Add `import io` at the top, and change the top imports to `from skills.computer import OS, press_keys, type_text` and `from skills.screen import VISION_MAX_SIDE, focus_window`.)

```python
STEP_PAUSE = 0.4  # seconds between steps, so apps keep up
MAX_WAIT = 10


def find_click(step: dict, folder: Path, seer, rect: list | None) -> tuple[int, int] | None:
    """Where to click now: exact image match, then Claude vision, then the recorded spot in the window."""
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
```

- [ ] **Step 4:** Run `venv/Scripts/python -m pytest tests/test_teach.py -q`. Expected: all PASS.
- [ ] **Step 5:** `git add skills/teach.py tests/test_teach.py && git commit -m "Teach: replay taught tasks with image, vision and position fallbacks"`

---

### Task 5: List, forget, and the taught list in the system prompt

**Files:** Modify `skills/teach.py`, `brain/agent.py` (`_messages`), `tests/test_teach.py`, `README.md`.

- [ ] **Step 1: Failing tests** (append)

```python
def save(folder, name, variables=()):
    folder.mkdir()
    (folder / "skill.json").write_text(json.dumps({"name": name, "description": "Save report",
                                                   "variables": list(variables), "steps": [{"wait": 0}]}))


def test_list_and_summary(taught):
    assert teach.taught_summary() == ""
    save(taught / "monday-report", "Monday report", ["month"])
    (taught / "_recording").mkdir()
    assert teach.list_taught() == "You've taught me: Monday report (Save report, needs month)."
    assert "Monday report" in teach.taught_summary() and "do_taught" in teach.taught_summary()


def test_forget(taught):
    save(taught / "monday-report", "Monday report")
    assert teach.forget_taught("monday report") == "Forgot Monday report."
    assert not (taught / "monday-report").exists()
    assert teach.forget_taught("monday report").startswith("error")
```

- [ ] **Step 2:** Run `venv/Scripts/python -m pytest tests/test_teach.py -q`. Expected: the new tests FAIL with `AttributeError` (`taught_summary`).

- [ ] **Step 3: Implement** (append to `skills/teach.py`)

```python
def _tasks() -> list[dict]:
    if not TAUGHT.exists():
        return []
    return [json.loads(f.read_text()) for f in sorted(TAUGHT.glob("*/skill.json")) if f.parent.name != "_recording"]


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
```

`brain/agent.py` `_messages`: directly before `return [{"role": "system", ...`, add:

```python
        from skills.teach import taught_summary  # here, not at the top: skills import brain modules
        system += taught_summary()
```

`README.md`: after the "Things to try" table, add:

```markdown
### Teach EDITH a task

Say "Edith, watch me", then do the task with your mouse and keyboard. When you finish, say "Edith, done, call it Monday report". EDITH sends what you did to Claude, which turns it into named steps. The steps are saved in `data/taught/monday-report/skill.json`, a plain file you can edit. Later, say "Do Monday report", or "Do Monday report for March" if the task has a value that changes. EDITH asks "Are you sure?" and then repeats the task. If a button has moved, she finds it again by its picture.

- Learning needs internet. Replaying mostly doesn't: Claude vision is used only when a button can't be found by its picture.
- Move the mouse into a screen corner to stop a replay.
- EDITH doesn't record typing in windows whose title mentions a password or login. While she watches, keystrokes in every other window are recorded, so say "done" or "cancel" as soon as you finish.
- "What have I taught you?" lists tasks. "Forget Monday report" deletes one.
- Window tracking works on Windows. On macOS and Linux, replay relies on matching pictures and vision.
```

- [ ] **Step 4:** Run `venv/Scripts/python -m pytest -q` (the whole suite). Expected: all PASS.
- [ ] **Step 5:** `git add skills/teach.py brain/agent.py tests/test_teach.py README.md && git commit -m "Teach: list and forget taught tasks; name them in the system prompt"`

---

### Task 6: Manual check (real hardware)

- [ ] Run `venv/Scripts/python main.py`.
- [ ] Say "Edith, watch me". Open Notepad, type `hello March`, press `ctrl+s`, then cancel the dialog with `esc`. Say "Edith, done, call it notepad test".
- [ ] Open `data/taught/notepad-test/skill.json` and check that the steps read sensibly.
- [ ] Close Notepad. Say "Do notepad test for April", answer "yes", and check that Notepad gets `hello April`.
