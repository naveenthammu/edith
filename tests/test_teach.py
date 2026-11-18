import json
import sys
from types import SimpleNamespace

import pytest
from PIL import Image

from skills import teach


def k(key, *mods):
    return {"kind": "key", "key": key, "mods": list(mods)}


# ---------- pure helpers ----------

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
    text = ('```json\n{"description": "Report", "steps": [{"focus": "Excel"}, {"type": "{month} sales"}, '
            '{"keys": "ctrl+s"}]}\n```')
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


# ---------- teaching ----------

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
    teach._rec.events = [k("a")]
    assert "internet" in teach.finish_teaching("x")
    assert teach._rec is not None


def test_finish_without_start(taught):
    assert teach.finish_teaching("x").startswith("error")


def test_cancel_discards(taught):
    teach.start_teaching()
    assert teach.cancel_teaching() == "Okay, I stopped watching."
    assert teach._rec is None


# ---------- replay ----------

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


def save(folder, name, variables=(), steps=({"wait": 0},)):
    folder.mkdir()
    (folder / "skill.json").write_text(json.dumps({"name": name, "description": "Save report",
                                                   "variables": list(variables), "steps": list(steps)}))


def test_do_taught_runs_steps_with_values(taught, monkeypatch):
    save(taught / "report", "report", ["month"],
         [{"focus": "Excel"}, {"type": "{month} sales"}, {"keys": "ctrl+s"}, {"wait": 0}])
    done = []
    monkeypatch.setitem(sys.modules, "pyautogui", FakeGui())
    monkeypatch.setattr(teach, "_seer", lambda: None)
    monkeypatch.setattr(teach, "_active", lambda: ("Excel", [0, 0, 10, 10]))
    monkeypatch.setattr(teach, "STEP_PAUSE", 0)
    monkeypatch.setattr(teach, "focus_window", lambda app: done.append(("focus", app)) or True)
    monkeypatch.setattr(teach, "type_text", lambda t: done.append(("type", t)) or "Typed.")
    monkeypatch.setattr(teach, "press_keys", lambda keys: done.append(("keys", keys)) or "Pressed.")
    assert teach.do_taught("Report", "March") == "Done: report."
    assert done == [("focus", "Excel"), ("type", "March sales"), ("keys", "ctrl+s")]


def test_do_taught_asks_for_missing_value(taught):
    save(taught / "report", "report", ["month"])
    assert teach.do_taught("report") == "error: tell me the month for report"


def test_do_taught_unknown(taught):
    assert teach.do_taught("nope").startswith("error: I don't know")


# ---------- managing ----------

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
