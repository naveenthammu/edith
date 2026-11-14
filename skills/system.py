import ctypes
import subprocess
from datetime import datetime
from pathlib import Path

import psutil

from skills import skill


@skill(confirm=True)
def lock_pc() -> str:
    """Lock the Windows PC."""
    ctypes.windll.user32.LockWorkStation()
    return "PC locked."


@skill(confirm=True)
def sleep_pc() -> str:
    """Put the PC to sleep."""
    # Note: if hibernation is enabled on this PC, SetSuspendState hibernates instead of sleeping.
    subprocess.run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], check=False)
    return "Going to sleep."


@skill(confirm=True)
def shutdown_pc() -> str:
    """Shut down the PC in 30 seconds. It can be cancelled with cancel_shutdown."""
    subprocess.run(["shutdown", "/s", "/t", "30"], check=True)
    return "Shutting down in 30 seconds."


@skill()
def cancel_shutdown() -> str:
    """Cancel a pending shutdown."""
    result = subprocess.run(["shutdown", "/a"], capture_output=True, text=True)
    return "Shutdown cancelled." if result.returncode == 0 else "There was no shutdown to cancel."


@skill()
def screenshot() -> str:
    """Take a screenshot and save it to Pictures/Edith."""
    import pyautogui

    folder = Path.home() / "Pictures" / "Edith"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"screenshot-{datetime.now():%Y%m%d-%H%M%S}.png"
    pyautogui.screenshot(str(path))
    return f"Screenshot saved to {path}"


@skill()
def battery() -> str:
    """Get the battery level and charging state."""
    info = psutil.sensors_battery()
    if info is None:
        return "No battery found on this PC."
    state = "charging" if info.power_plugged else "on battery"
    return f"Battery at {info.percent:.0f} percent, {state}."
