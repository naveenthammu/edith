"""Focus tests replace OS boundaries; they never activate a real window."""
import sys
from types import SimpleNamespace

import pytest

from skills import screen


class Window:
    title = "Report - Notepad"
    _hWnd = 42
    isMinimized = False

    def __init__(self, error=None):
        self.error = error

    def activate(self):
        if self.error:
            raise self.error


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(screen, "OS", "Windows")
    monkeypatch.setattr(screen.time, "sleep", lambda _: None)
    monkeypatch.setattr(screen, "_win_windows", lambda: [Window()])


def test_matching_title_is_not_success_when_foreground_remains_elsewhere(windows, monkeypatch):
    monkeypatch.setitem(sys.modules, "pygetwindow", SimpleNamespace(
        getActiveWindow=lambda: SimpleNamespace(_hWnd=99)))
    assert screen.focus_window("Notepad", wait=0) is False


def test_activation_failure_does_not_allow_caller_to_type(windows, monkeypatch):
    monkeypatch.setattr(screen, "_win_windows", lambda: [Window(RuntimeError("focus refused"))])
    monkeypatch.setitem(sys.modules, "pygetwindow", SimpleNamespace(
        getActiveWindow=lambda: SimpleNamespace(_hWnd=42)))
    assert screen.focus_window("Notepad", wait=0) is False


def test_focus_succeeds_only_when_matching_window_is_foreground(windows, monkeypatch):
    monkeypatch.setitem(sys.modules, "pygetwindow", SimpleNamespace(
        getActiveWindow=lambda: SimpleNamespace(_hWnd=42)))
    assert screen.focus_window("Notepad", wait=0) is True


def test_empty_target_never_selects_an_arbitrary_window(windows, monkeypatch):
    monkeypatch.setitem(sys.modules, "pygetwindow", SimpleNamespace(
        getActiveWindow=lambda: SimpleNamespace(_hWnd=42)))
    assert screen.focus_window(" ", wait=0) is False


@pytest.mark.parametrize("os_name", ["Darwin", "Linux"])
def test_other_platform_activation_requires_observed_foreground(os_name, monkeypatch):
    monkeypatch.setattr(screen, "OS", os_name)
    monkeypatch.setattr(screen.time, "sleep", lambda _: None)
    monkeypatch.setattr(screen, "_mac_apps", lambda: ["Notes"])
    monkeypatch.setattr(screen, "_linux_windows", lambda: [("0x0000002a", "Notes")])
    monkeypatch.setattr(screen, "run", lambda _: SimpleNamespace(returncode=0, stdout="Other app"))
    assert screen.focus_window("Notes", wait=0) is False


@pytest.mark.parametrize("os_name, observed", [
    ("Darwin", "Notes\n"), ("Linux", "_NET_ACTIVE_WINDOW(WINDOW): window id # 0x2a\n"),
])
def test_other_platform_success_checks_actual_foreground(os_name, observed, monkeypatch):
    monkeypatch.setattr(screen, "OS", os_name)
    monkeypatch.setattr(screen.time, "sleep", lambda _: None)
    monkeypatch.setattr(screen, "_mac_apps", lambda: ["Notes"])
    monkeypatch.setattr(screen, "_linux_windows", lambda: [("0x0000002a", "Notes")])
    monkeypatch.setattr(screen, "run", lambda _: SimpleNamespace(returncode=0, stdout=observed))
    assert screen.focus_window("Notes", wait=0) is True
