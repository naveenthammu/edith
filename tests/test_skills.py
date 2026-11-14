from skills import web


def test_resolve_app_known_names():
    assert web.resolve_app("Notepad") == "notepad.exe"
    assert web.resolve_app(" vs code ") == "code"
    assert web.resolve_app("photoshop") is None


def test_normalize_url_adds_scheme_and_blocks_others():
    assert web.normalize_url("youtube.com") == "https://youtube.com"
    assert web.normalize_url("http://example.com") == "http://example.com"
    assert web.normalize_url("file:///C:/Windows") is None
    assert web.normalize_url("javascript:alert(1)") is None


def test_open_website_rejects_bad_scheme(monkeypatch):
    opened = []
    monkeypatch.setattr(web.webbrowser, "open", opened.append)
    assert web.open_website("file:///C:/secret.txt").startswith("error")
    assert opened == []
    web.open_website("github.com")
    assert opened == ["https://github.com"]


from skills import media


def test_first_video_id_found():
    html = 'var ytInitialData = {"contents":[{"videoRenderer":{"videoId":"NeXbmEnpSz0","title":"x"}}]}'
    assert media.first_video_id(html) == "NeXbmEnpSz0"


def test_first_video_id_missing():
    assert media.first_video_id("<html>consent page</html>") is None


def test_volume_rejects_unknown_action(monkeypatch):
    pressed = []
    monkeypatch.setattr(media, "_press", lambda key, presses: pressed.append((key, presses)))
    assert media.volume("louder").startswith("error")
    media.volume("up", 3)
    media.volume("mute")
    assert pressed == [("volumeup", 3), ("volumemute", 1)]


from types import SimpleNamespace

from skills import REGISTRY, system


def test_dangerous_system_skills_need_confirmation():
    for name in ("lock_pc", "sleep_pc", "shutdown_pc"):
        assert REGISTRY[name].confirm is True
    for name in ("cancel_shutdown", "screenshot", "battery"):
        assert REGISTRY[name].confirm is False


def test_battery_reports_status(monkeypatch):
    monkeypatch.setattr(system.psutil, "sensors_battery", lambda: SimpleNamespace(percent=81.6, power_plugged=True))
    assert system.battery() == "Battery at 82 percent, charging."
    monkeypatch.setattr(system.psutil, "sensors_battery", lambda: None)
    assert system.battery() == "No battery found on this PC."


import threading

from skills import productivity


def test_notes_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(productivity, "NOTES_FILE", tmp_path / "notes.txt")
    assert productivity.read_notes() == "You have no notes."
    productivity.add_note("buy milk")
    productivity.add_note("call mom")
    notes = productivity.read_notes()
    assert "buy milk" in notes and "call mom" in notes
    assert notes.index("buy milk") < notes.index("call mom")


def test_reminder_announces(monkeypatch):
    fired = threading.Event()
    said = []
    monkeypatch.setattr(productivity, "announce", lambda text: (said.append(text), fired.set()))
    monkeypatch.setattr(productivity, "SECONDS_PER_MINUTE", 0.01)

    assert productivity.set_reminder(1, "stretch") == "Reminder set for 1 minutes."
    assert fired.wait(2)
    assert said == ["Reminder, sir: stretch"]


def test_reminder_rejects_bad_minutes():
    for bad in (0, 2000, float("nan")):
        assert productivity.set_reminder(bad, "x").startswith("error")


import sys


class FakePyAutoGui:
    def __init__(self):
        self.calls = []

    def press(self, key):
        self.calls.append(("press", key))

    def write(self, text, interval=0):
        self.calls.append(("write", text))


def test_open_app_rejects_command_like_names(monkeypatch):
    fake = FakePyAutoGui()
    monkeypatch.setitem(sys.modules, "pyautogui", fake)
    monkeypatch.setattr(web.time, "sleep", lambda s: None)
    for bad in ("shutdown /s /t 0", "cmd /c calc", r"C:\Windows\x.exe"):
        assert web.open_app(bad).startswith("error")
    assert fake.calls == []


def test_open_app_unknown_plain_name_uses_start_menu(monkeypatch):
    fake = FakePyAutoGui()
    monkeypatch.setitem(sys.modules, "pyautogui", fake)
    monkeypatch.setattr(web.time, "sleep", lambda s: None)
    assert not web.open_app("spotify").startswith("error")
    assert ("write", "spotify") in fake.calls


from brain.llm import LLM
from skills import assistant


def test_brain_mode_switch(monkeypatch):
    ready = [False]
    llm = LLM(object(), "m", local_factory=lambda: None, local_ready=lambda: ready[0])
    monkeypatch.setattr(assistant, "llm", llm)

    assert "still downloading" in assistant.set_brain_mode("offline")
    assert llm.force_offline is False

    ready[0] = True
    assert assistant.set_brain_mode("Offline") == "Switched to offline mode."
    assert llm.force_offline is True

    llm._offline_until = 999.0
    assert assistant.set_brain_mode("online") == "Switched to online mode."
    assert llm.force_offline is False and llm._offline_until == 0.0

    assert assistant.set_brain_mode("sideways").startswith("error")
