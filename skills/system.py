import subprocess
from datetime import datetime
from pathlib import Path

import psutil

from skills import skill
from skills.computer import OS

# Commands per OS. macOS/Linux shutdown use the session's own power controls, so no sudo is needed.
LOCK = {"Darwin": ["pmset", "displaysleepnow"], "Linux": ["loginctl", "lock-session"]}
SLEEP = {"Windows": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],  # hibernates if hibernation is on
         "Darwin": ["pmset", "sleepnow"], "Linux": ["systemctl", "suspend"]}
SHUTDOWN = {"Windows": ["shutdown", "/s", "/t", "30"],
            "Darwin": ["osascript", "-e", 'tell application "System Events" to shut down'],
            "Linux": ["shutdown", "-h", "+1"]}
CANCEL = {"Windows": ["shutdown", "/a"], "Linux": ["shutdown", "-c"]}


@skill(confirm=True)
def lock_pc() -> str:
    """Lock the computer."""
    if OS == "Windows":
        import ctypes

        ctypes.windll.user32.LockWorkStation()
    else:
        subprocess.run(LOCK[OS], check=False)
    return "Computer locked."


@skill(confirm=True)
def sleep_pc() -> str:
    """Put the computer to sleep."""
    subprocess.run(SLEEP[OS], check=False)
    return "Going to sleep."


@skill(confirm=True)
def shutdown_pc() -> str:
    """Shut down the computer (after 30 seconds on Windows, 1 minute on Linux, immediately on macOS)."""
    subprocess.run(SHUTDOWN[OS], check=True)
    return "Shutting down." if OS == "Darwin" else "Shutting down shortly. Say cancel shutdown to stop it."


@skill()
def cancel_shutdown() -> str:
    """Cancel a pending shutdown."""
    if OS not in CANCEL:
        return "A shutdown on this system can't be cancelled once started."
    result = subprocess.run(CANCEL[OS], capture_output=True, text=True)
    return "Shutdown cancelled." if result.returncode == 0 else "There was no shutdown to cancel."


@skill()
def screenshot(app: str = "") -> str:
    """Take a screenshot and save it to Pictures/Edith. Give app (e.g. 'chrome', 'whatsapp') to capture just that
    app's window; leave it empty for the whole screen."""
    import pyautogui  # on Linux this needs gnome-screenshot or scrot installed

    from skills.screen import capture_window

    folder = Path.home() / "Pictures" / "Edith"
    folder.mkdir(parents=True, exist_ok=True)
    label = "".join(c for c in app.lower() if c.isalnum())
    path = folder / f"screenshot-{label + '-' if label else ''}{datetime.now():%Y%m%d-%H%M%S}.png"
    if app.strip():
        if capture_window(app, path) is None:
            return f"error: no open window matches {app}"
    else:
        pyautogui.screenshot(str(path))
    return f"Screenshot saved to {path}"


@skill()
def battery() -> str:
    """Get the battery level and charging state."""
    info = psutil.sensors_battery()
    if info is None:
        return "No battery found on this computer."
    state = "charging" if info.power_plugged else "on battery"
    return f"Battery at {info.percent:.0f} percent, {state}."
