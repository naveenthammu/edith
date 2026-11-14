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
