import json
import sys
from pathlib import Path

import pytest

from skills import REGISTRY, computer, media, system, web


def test_type_text_focuses_the_named_app_or_refuses(monkeypatch):
    from types import SimpleNamespace

    from skills import screen

    activated, typed = [], []
    note = SimpleNamespace(title="Untitled - Notepad", isMinimized=False, activate=lambda: activated.append(1))
    monkeypatch.setattr(screen, "OS", "Windows")
    monkeypatch.setattr(screen, "_win_windows", lambda: [note])
    monkeypatch.setattr(screen.time, "sleep", lambda s: None)
    monkeypatch.setattr("pyautogui.write", lambda text, interval=0: typed.append(text))
    assert computer.type_text("hi", app="notepad") == "Typed." and activated and typed == ["hi"]
    assert computer.type_text("hi", app="word").startswith("error") and typed == ["hi"]  # nothing typed blind


def test_every_os_has_power_commands():
    for os_name in ("Windows", "Darwin", "Linux"):
        assert os_name in system.SLEEP and os_name in system.SHUTDOWN
        assert os_name in computer.WINDOW_KEYS
    assert set(system.LOCK) == {"Darwin", "Linux"}  # Windows locks through user32


def test_risky_skills_need_confirmation():
    import skills

    skills.load_all()
    for name in ("delete_file", "close_app", "run_script", "send_whatsapp", "wifi"):
        assert REGISTRY[name].confirm, name
    for name in ("find_files", "type_text", "system_info", "create_script", "remember", "refresh_naukri"):
        assert not REGISTRY[name].confirm, name


def test_user_path_resolves_home_folders(monkeypatch, tmp_path):
    (tmp_path / "Desktop").mkdir()
    monkeypatch.setattr(computer, "HOME", tmp_path)
    assert computer.user_path("desktop/a.txt") == tmp_path / "Desktop" / "a.txt"
    assert computer.user_path(str(tmp_path / "x")) == tmp_path / "x"


def test_delete_refuses_outside_home_and_home_itself(monkeypatch, tmp_path):
    monkeypatch.setattr(computer, "HOME", tmp_path / "home")
    (tmp_path / "home").mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    assert REGISTRY["delete_file"].func(str(outside)).startswith("error")
    assert REGISTRY["delete_file"].func(str(tmp_path / "home")).startswith("error")
    assert outside.exists()


def test_find_and_move_files(monkeypatch, tmp_path):
    monkeypatch.setattr(computer, "HOME", tmp_path)
    (tmp_path / "Report-2026.txt").write_text("r")
    assert "Report-2026.txt" in computer.find_files("report")
    computer.create_folder("Archive")
    computer.move_file("Report-2026.txt", "Archive")
    assert (tmp_path / "Archive" / "Report-2026.txt").exists()


def test_scripts_validated_and_run(monkeypatch, tmp_path):
    monkeypatch.setattr(computer, "SCRIPTS_DIR", tmp_path)
    assert computer.create_script("x", "cobol", "").startswith("error")
    assert "hello_world.py" in computer.create_script("hello world", "python", "print(6 * 7)")
    assert "42" in REGISTRY["run_script"].func("hello world")
    assert REGISTRY["run_script"].func("missing").startswith("error")


def test_contacts_and_phone_validation(monkeypatch, tmp_path):
    monkeypatch.setattr(computer, "CONTACTS_FILE", tmp_path / "contacts.json")
    assert computer.save_contact("mom", "12345").startswith("error")
    computer.save_contact("Mom", "+91 98765 43210")
    assert json.loads((tmp_path / "contacts.json").read_text()) == {"mom": "919876543210"}
    assert REGISTRY["send_whatsapp"].func("nobody", "hi").startswith("error")


def test_press_keys_rejects_unknown_keys():
    assert computer.press_keys("ctrl+banana").startswith("error")


def test_radio_rejects_bad_state():
    assert computer._radio("wifi", "maybe").startswith("error")


def test_open_app_on_mac_and_linux(monkeypatch):
    calls = []
    monkeypatch.setattr(web.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or type("R", (), {"returncode": 0})())
    monkeypatch.setattr(web, "OS", "Darwin")
    assert web.open_app("vs code") == "Opened vs code"
    assert calls[-1] == ["open", "-a", "Visual Studio Code"]
    assert web.open_app("rm -rf /").startswith("error")

    monkeypatch.setattr(web, "OS", "Linux")
    monkeypatch.setattr(web.shutil, "which", lambda c: f"/usr/bin/{c}")
    monkeypatch.setattr(web.subprocess, "Popen", lambda cmd, **kw: calls.append(cmd))
    assert web.open_app("calculator") == "Opened calculator"
    assert calls[-1] == ["gnome-calculator"]


def test_volume_commands_on_mac_and_linux(monkeypatch):
    monkeypatch.setattr(media, "OS", "Linux")
    assert media._volume_cmd("volumeup", 10) == ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "+10%"]
    assert media._volume_cmd("volumemute", 2)[1] == "set-sink-mute"
    monkeypatch.setattr(media, "OS", "Darwin")
    assert media._volume_cmd("volumedown", 6)[0] == "osascript"
    assert "- 6" in media._volume_cmd("volumedown", 6)[2]


def test_screen_skills_registered_with_right_confirmation():
    import skills

    skills.load_all()
    assert REGISTRY["close_all_apps"].confirm
    assert not REGISTRY["look_at_screen"].confirm and not REGISTRY["list_open_windows"].confirm
    assert "app" in REGISTRY["screenshot"].schema["function"]["parameters"]["properties"]


def test_look_at_screen_offline_lists_windows(monkeypatch):
    from skills import assistant, screen

    monkeypatch.setattr(assistant, "llm", None)
    monkeypatch.setattr(screen, "window_titles", lambda: ["Notepad", "Chrome", "Notepad"])
    out = screen.look_at_screen()
    assert "Notepad; Chrome" in out and out.count("Notepad") == 1


def test_screenshot_of_missing_app_is_an_error(monkeypatch):
    from skills import screen

    monkeypatch.setattr(screen, "capture_window", lambda app, path: None)
    assert system.screenshot("ghostapp").startswith("error")


def test_close_app_matches_whole_names_only(monkeypatch):
    from types import SimpleNamespace

    from skills import computer

    procs = [SimpleNamespace(info={"name": n}, terminate=lambda: None) for n in ("svchost.exe", "chrome.exe")]
    monkeypatch.setattr(computer.psutil, "process_iter", lambda attrs: procs)
    assert REGISTRY["close_app"].func("host") == "host is not running."
    assert REGISTRY["close_app"].func("chrome") == "Closed 1 chrome process."
