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


@pytest.mark.parametrize("step", [
    {"type": 42}, {"focus": ""}, {"keys": ["ctrl", "s"]},
    {"wait": -1}, {"wait": True}, {"wait": 11}, {"wait": float("nan")},
    {"click": "Save", "crop": "../../private.png"},
    {"click": "Save", "crop": "C:/private.png"},
    {"click": "Save", "crop": "secret.txt"},
    {"click": "Save", "crop": "0.png", "rel": [1.5, 0.5]},
    {"click": "Save", "crop": "0.png", "rel": [True, 0.5]},
    {"click": "Save", "crop": "0.png", "rel": [0.5]},
    {"type": "hello", "unexpected": "ignored commands are dangerous"},
])
def test_recipe_rejects_invalid_types_bounds_and_paths(step):
    with pytest.raises(ValueError):
        teach.parse_compiled(json.dumps({"steps": [step]}))


def test_recipe_limits_step_count_before_replay():
    with pytest.raises(ValueError):
        teach.parse_compiled(json.dumps({"steps": [{"keys": "enter"}] * 101}))


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
    KEYBOARD_KEYS = ["ctrl", "shift", "s", "enter"]
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
    (tmp_path / "0.png").write_bytes(b"x")
    step = {"click": "Save", "crop": "0.png", "rel": [0.5, 0.25]}
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
    save(taught / "report", "report", ["month"], [{"type": "{month}"}])
    assert teach.do_taught("report") == "error: tell me the month for report"


def test_do_taught_unknown(taught):
    assert teach.do_taught("nope").startswith("error: I don't know")


# ---------- managing ----------

def test_list_and_summary(taught):
    assert teach.taught_summary() == ""
    save(taught / "monday-report", "Monday report", ["month"], [{"type": "{month}"}])
    (taught / "_recording").mkdir()
    assert teach.list_taught() == "You've taught me: Monday report (Save report, needs month)."
    assert "Monday report" in teach.taught_summary() and "do_taught" in teach.taught_summary()


def test_forget(taught):
    save(taught / "monday-report", "Monday report")
    assert teach.forget_taught("monday report") == "Forgot Monday report."
    assert not (taught / "monday-report").exists()
    assert teach.forget_taught("monday report").startswith("error")


def test_finish_refuses_name_without_latin_letters(taught, monkeypatch):
    (taught / "old").mkdir()
    (taught / "old" / "skill.json").write_text('{"name": "old", "steps": []}')
    monkeypatch.setattr(teach, "_seer", lambda: FakeSeer('{"steps": [{"keys": "enter"}]}'))
    teach.start_teaching()
    teach._rec.events = [k("a")]
    assert teach.finish_teaching("रिपोर्ट").startswith("error")
    assert (taught / "old" / "skill.json").exists() and teach._rec is not None
    assert teach._rec.stopped is True


def test_broken_task_file_is_skipped(taught):
    (taught / "bad").mkdir()
    (taught / "bad" / "skill.json").write_text("{not json")
    assert teach.taught_summary() == ""


@pytest.mark.parametrize("crop", ["../../outside.png", "C:/outside.png", "missing.png"])
def test_invalid_saved_recipe_is_rejected_before_any_input_or_upload(taught, monkeypatch, crop):
    save(taught / "report", "report", steps=[
        {"focus": "Excel"}, {"click": "Save", "crop": crop}])
    monkeypatch.setattr(teach, "focus_window", lambda _: pytest.fail("Invalid task focused a window"))
    monkeypatch.setattr(teach, "_seer", lambda: pytest.fail("Invalid task requested a vision provider"))
    assert teach.do_taught("report").startswith("error:")


def test_crop_symlink_is_rejected_before_capture_or_read(tmp_path, monkeypatch):
    folder = tmp_path / "task"
    folder.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"synthetic private bytes")
    try:
        (folder / "0.png").symlink_to(outside)
    except OSError:
        pytest.skip("This Windows account cannot create test symlinks")
    monkeypatch.setitem(sys.modules, "pyautogui", SimpleNamespace(
        locateOnScreen=lambda _: pytest.fail("Linked file reached screen matching")))
    with pytest.raises(ValueError):
        teach.find_click({"click": "Save", "crop": "0.png"}, folder, None, None)


def test_saved_recipe_unknown_field_is_rejected_before_focus(taught, monkeypatch):
    save(taught / "report", "report", steps=[{"focus": "Excel"}, {"type": "ok", "wait": 0}])
    monkeypatch.setattr(teach, "focus_window", lambda _: pytest.fail("Invalid task focused a window"))
    assert teach.do_taught("report").startswith("error:")


def test_vision_out_of_screen_coordinates_never_become_a_click(tmp_path, monkeypatch):
    (tmp_path / "0.png").write_bytes(b"x")
    monkeypatch.setitem(sys.modules, "pyautogui", FakeGui())
    assert teach.find_click({"click": "Save", "crop": "0.png"}, tmp_path,
                            FakeSeer("999999, 999999"), None) is None


def test_failed_type_step_stops_remaining_replay(taught, monkeypatch):
    save(taught / "report", "report", steps=[{"type": "hello"}, {"keys": "enter"}])
    monkeypatch.setitem(sys.modules, "pyautogui", FakeGui())
    monkeypatch.setattr(teach, "_seer", lambda: None)
    monkeypatch.setattr(teach, "type_text", lambda _: "error: target disappeared")
    monkeypatch.setattr(teach, "press_keys", lambda _: pytest.fail("Continued after failed typing"))
    assert teach.do_taught("report").startswith("error:")


def test_unknown_key_in_later_step_is_rejected_before_first_focus(taught, monkeypatch):
    save(taught / "report", "report", steps=[{"focus": "Excel"}, {"keys": "ctrl+imaginarykey"}])
    monkeypatch.setitem(sys.modules, "pyautogui", FakeGui())
    monkeypatch.setattr(teach, "focus_window", lambda _: pytest.fail("Invalid keyboard step reached focus"))
    monkeypatch.setattr(teach, "_seer", lambda: pytest.fail("Invalid keyboard step requested vision"))
    assert teach.do_taught("report").startswith("error:")


def test_stopped_recorder_ignores_late_callbacks(tmp_path, monkeypatch):
    # Use the real recorder without starting global hooks.
    recorder = teach.Recorder(tmp_path)
    recorder._stop.set()
    monkeypatch.setattr(teach, "_active", lambda: pytest.fail("Stopped capture inspected the desktop"))
    recorder._click(1, 1, SimpleNamespace(name="left"), True)
    recorder._press(SimpleNamespace(char="a", name=None))
    assert recorder.events == []


def test_non_dictionary_saved_task_is_skipped_in_prompt_summary(taught):
    (taught / "bad").mkdir()
    (taught / "bad" / "skill.json").write_text("[]")
    assert teach.taught_summary() == ""


def test_windows_reparse_crop_is_rejected_without_screen_matching(tmp_path, monkeypatch):
    crop = tmp_path / "0.png"
    crop.write_bytes(b"synthetic image")
    original = type(crop).lstat

    def lstat(path, *args, **kwargs):
        metadata = original(path, *args, **kwargs)
        if path == crop:
            return SimpleNamespace(st_mode=metadata.st_mode, st_size=metadata.st_size,
                                   st_file_attributes=0x400)
        return metadata

    monkeypatch.setattr(type(crop), "lstat", lstat)
    monkeypatch.setitem(sys.modules, "pyautogui", SimpleNamespace(
        locateOnScreen=lambda _: pytest.fail("Reparse asset reached screen matching")))
    with pytest.raises(ValueError):
        teach.find_click({"click": "Save", "crop": "0.png"}, tmp_path, None, None)


def test_compile_rejects_unrecorded_asset_before_saving(taught, monkeypatch):
    monkeypatch.setattr(teach, "_seer", lambda: FakeSeer('{"steps":[{"click":"Save","crop":"missing.png"}]}'))
    teach.start_teaching()
    teach._rec.events = [k("a")]
    assert teach.finish_teaching("report").startswith("error:")
    assert not (taught / "report").exists()
    assert teach._rec.stopped is True
