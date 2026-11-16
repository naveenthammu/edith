"""What's on screen: open windows, closing them, app screenshots, and (online) looking at the screen."""
import io
import time
from pathlib import Path

import psutil

from skills import skill
from skills.computer import OS, run

VISION_MAX_SIDE = 1568  # larger images cost more and aren't read any better


def _own_pids() -> set[int]:
    """EDITH and the terminal running it: never close these, or EDITH closes herself."""
    me = psutil.Process()
    return {me.pid} | {p.pid for p in me.parents()}


# ---------- per-OS window access ----------

def _win_windows() -> list:
    import ctypes

    import pygetwindow  # installed with pyautogui on Windows

    own, console = _own_pids(), ctypes.windll.kernel32.GetConsoleWindow()
    out = []
    for w in pygetwindow.getAllWindows():
        if not (w.visible and w.title.strip()) or w.title == "Program Manager" or w._hWnd == console:
            continue
        pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(w._hWnd, ctypes.byref(pid))
        if pid.value not in own:
            out.append(w)
    return out


def _mac_apps() -> list[str]:
    script = 'tell application "System Events" to get name of every application process whose background only is false'
    names = [n.strip() for n in run(["osascript", "-e", script]).stdout.split(",") if n.strip()]
    own = {psutil.Process(pid).name() for pid in _own_pids()}
    return [n for n in names if n != "Finder" and n not in own]


def _linux_windows() -> list[tuple[str, str]]:
    """(window id, title) pairs from wmctrl, minus EDITH's own windows."""
    own, out = _own_pids(), []
    for line in run(["wmctrl", "-lp"]).stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) == 5 and parts[2].isdigit() and int(parts[2]) not in own:
            out.append((parts[0], parts[4]))
    return out


def window_titles() -> list[str]:
    if OS == "Windows":
        return [w.title for w in _win_windows()]
    if OS == "Darwin":
        return _mac_apps()
    return [title for _, title in _linux_windows()]


@skill()
def list_open_windows() -> str:
    """List the apps and windows open right now."""
    titles = list(dict.fromkeys(window_titles()))
    return "Open: " + "; ".join(titles) if titles else "No app windows are open."


@skill(confirm=True)
def close_all_apps() -> str:
    """Close every open app window (not EDITH). Apps with unsaved work get the chance to ask to save."""
    if OS == "Windows":
        windows = _win_windows()
        for w in windows:
            try:
                w.close()  # WM_CLOSE: the same as clicking X, so apps can prompt to save
            except Exception:
                pass
        count = len(windows)
    elif OS == "Darwin":
        apps = _mac_apps()
        for name in apps:
            run(["osascript", "-e", f'quit app "{name}"'])  # names come from System Events, not from the user
        count = len(apps)
    else:
        windows = _linux_windows()
        for wid, _ in windows:
            run(["wmctrl", "-ic", wid])
        count = len(windows)
    return f"Asked {count} window{'s' if count != 1 else ''} to close." if count else "Nothing was open."


def capture_window(app: str, path: Path) -> str | None:
    """Save a screenshot of the first window whose title or app name contains `app`; None if not found."""
    needle = app.strip().lower()
    if OS == "Windows":
        import pyautogui

        w = next((w for w in _win_windows() if needle in w.title.lower()), None)
        if w is None:
            return None
        try:
            if w.isMinimized:
                w.restore()
            w.activate()
        except Exception:
            pass  # Windows sometimes refuses focus changes; the region grab still works if it's visible
        time.sleep(0.5)
        pyautogui.screenshot(str(path), region=(max(w.left, 0), max(w.top, 0), w.width, w.height))
    elif OS == "Darwin":
        name = next((n for n in _mac_apps() if needle in n.lower()), None)
        if name is None:
            return None
        bounds = run(["osascript", "-e",
                      f'tell application "System Events" to tell process "{name}"\n'
                      f'set frontmost to true\nset {{x, y}} to position of window 1\n'
                      f'set {{w, h}} to size of window 1\nreturn (x as text) & "," & y & "," & w & "," & h\nend tell'
                      ]).stdout.strip()
        time.sleep(0.5)
        run(["screencapture", "-x", "-R", bounds, str(path)])
    else:
        wid = next((wid for wid, title in _linux_windows() if needle in title.lower()), None)
        if wid is None:
            return None
        run(["wmctrl", "-ia", wid])
        time.sleep(0.5)
        run(["gnome-screenshot", "-w", "-f", str(path)])
    return str(path) if path.exists() else None


# ---------- looking at the screen ----------

@skill()
def look_at_screen(question: str = "What is on my screen?") -> str:
    """Look at the screen and answer a question about it, e.g. 'what's on my screen' or 'read this error'."""
    from brain.llm import REMOTE_ERRORS
    from skills import assistant

    llm = assistant.llm
    seer = llm.remote.vision() if llm and llm.remote and not llm.force_offline else None
    if seer is not None:
        import pyautogui

        img = pyautogui.screenshot()
        img.thumbnail((VISION_MAX_SIDE, VISION_MAX_SIDE))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        try:
            return seer.describe_image(buf.getvalue(), question)
        except REMOTE_ERRORS:
            pass  # fall through to what can be known offline
    # shortcut: offline EDITH can't see pixels, only window titles; a local vision model (e.g. Moondream)
    # could fill this in at ~1-2 GB extra download and ~30 s per look on this CPU.
    titles = list(dict.fromkeys(window_titles()))
    return ("I can't see the screen without an online model, sir. "
            + ("Open windows are: " + "; ".join(titles) if titles else "No app windows are open."))
