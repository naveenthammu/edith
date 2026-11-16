"""Computer control that works on Windows, macOS and Linux.

Each OS-specific action picks its command from a dict keyed by platform.system(), so a missing
entry is an honest "not supported here" instead of a crash.
"""
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote

import psutil

from skills import skill

OS = platform.system()  # "Windows", "Darwin" or "Linux"
HOME = Path.home()
SCRIPTS_DIR = HOME / "Documents" / "Edith" / "scripts"
CONTACTS_FILE = Path(__file__).resolve().parent.parent / "data" / "contacts.json"
MAX_RESULTS = 10
MAX_DIRS_SCANNED = 20000  # keeps find_files to a few seconds on a big home folder
SKIP_DIRS = {"AppData", "node_modules", "venv", ".git", "Library", "__pycache__", "site-packages"}


def run(cmd: list[str], timeout: float = 15) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def powershell(script: str, timeout: float = 20) -> subprocess.CompletedProcess:
    # PowerShell writes the console code page by default, which turns non-English text into '?'.
    return run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + script], timeout)


def open_target(target: str) -> None:
    """Open a file, folder or URL with the OS default handler."""
    if OS == "Windows":
        os.startfile(target)  # type: ignore[attr-defined]  # Windows-only API
    else:
        subprocess.Popen(["open" if OS == "Darwin" else "xdg-open", target])


def user_path(path: str) -> Path:
    """Resolve what the user said ('Desktop/report.txt', '~/x', an absolute path) to a real path."""
    p = Path(os.path.expandvars(path.strip().strip('"'))).expanduser()
    if not p.is_absolute():
        # "desktop", "downloads"... are folders under home; match case-insensitively (Linux is case-sensitive).
        first, *rest = p.parts or ("",)
        match = next((c for c in HOME.iterdir() if c.name.lower() == first.lower()), None) if first else None
        p = (match or HOME / first).joinpath(*rest)
    return p


def inside_home(p: Path) -> bool:
    try:
        p.resolve().relative_to(HOME.resolve())
        return True
    except ValueError:
        return False


# ---------- files and folders ----------

@skill()
def find_files(name: str, folder: str = "") -> str:
    """Find files or folders whose name contains some text, searching the user's home folder (or a given folder)."""
    root = user_path(folder) if folder else HOME
    needle, hits, scanned = name.strip().lower(), [], 0
    if not needle:
        return "error: say part of the file name"
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS]
        scanned += 1
        hits += [os.path.join(dirpath, n) for n in dirnames + filenames if needle in n.lower()]
        if len(hits) >= MAX_RESULTS or scanned >= MAX_DIRS_SCANNED:
            break
    return "\n".join(hits[:MAX_RESULTS]) or f"No files matching {name} found."


@skill()
def open_path(path: str) -> str:
    """Open a file or folder with its default app, e.g. 'Downloads' or 'Documents/resume.pdf'."""
    p = user_path(path)
    if not p.exists():
        return f"error: {p} does not exist"
    open_target(str(p))
    return f"Opened {p}"


@skill()
def create_folder(path: str) -> str:
    """Create a folder (and any missing parent folders), e.g. 'Desktop/Projects/edith'."""
    p = user_path(path)
    p.mkdir(parents=True, exist_ok=True)
    return f"Folder ready at {p}"


@skill()
def move_file(source: str, destination: str) -> str:
    """Move or rename a file or folder. destination can be a folder or a new name."""
    src, dst = user_path(source), user_path(destination)
    if not src.exists():
        return f"error: {src} does not exist"
    if dst.exists() and not dst.is_dir():
        return f"error: {dst} already exists"
    return f"Moved to {shutil.move(str(src), str(dst))}"


@skill(confirm=True)
def delete_file(path: str) -> str:
    """Move a file or folder to the Recycle Bin / Trash. Only works inside the user's home folder."""
    from send2trash import send2trash

    p = user_path(path)
    if not p.exists():
        return f"error: {p} does not exist"
    if not inside_home(p) or p.resolve() == HOME.resolve():
        return "error: I only delete things inside your home folder"
    send2trash(str(p))  # recoverable, unlike os.remove
    return f"Moved {p.name} to the trash."


# ---------- apps and windows ----------

@skill(confirm=True)
def close_app(name: str) -> str:
    """Close a running app by name, e.g. chrome or notepad. Unsaved work in it may be lost."""
    needle = name.strip().lower().removesuffix(".exe")
    if len(needle) < 3:
        return "error: app name too short"
    procs = [p for p in psutil.process_iter(["name"]) if needle in (p.info["name"] or "").lower()]
    for p in procs:
        try:
            p.terminate()
        except psutil.Error:
            pass
    return f"Closed {len(procs)} {name} process{'es' if len(procs) != 1 else ''}." if procs else f"{name} is not running."


WINDOW_KEYS = {
    "Windows": {"minimize": "win+down", "maximize": "win+up", "switch": "alt+tab", "desktop": "win+d", "close": "alt+f4"},
    "Darwin": {"minimize": "command+m", "maximize": "ctrl+command+f", "switch": "command+tab",
               "desktop": "fn+f11", "close": "command+w"},
    "Linux": {"minimize": "win+h", "maximize": "win+up", "switch": "alt+tab", "desktop": "win+d", "close": "alt+f4"},
}


@skill()
def window(action: str) -> str:
    """Control the active window. action: minimize, maximize, switch (to the previous app), desktop (show desktop) or close."""
    keys = WINDOW_KEYS.get(OS, {}).get(action.strip().lower())
    if keys is None:
        return "error: action must be minimize, maximize, switch, desktop or close"
    import pyautogui

    pyautogui.hotkey(*keys.split("+"))
    return f"Window {action} done."


# ---------- keyboard, mouse, clipboard ----------

@skill()
def type_text(text: str) -> str:
    """Type text into the focused app, as if typed on the keyboard."""
    import pyautogui

    if text.isascii():
        pyautogui.write(text, interval=0.01)
    else:  # pyautogui can't type non-English letters; paste them instead
        set_clipboard(text)
        pyautogui.hotkey("command" if OS == "Darwin" else "ctrl", "v")
    return "Typed."


@skill()
def press_keys(keys: str) -> str:
    """Press a key or shortcut, e.g. 'enter', 'ctrl+s', 'alt+tab', 'ctrl+shift+esc'."""
    import pyautogui

    parts = [k.strip().lower() for k in keys.replace(" ", "+").split("+") if k.strip()]
    bad = [k for k in parts if k not in pyautogui.KEYBOARD_KEYS]
    if not parts or bad:
        return f"error: unknown key {', '.join(bad) or keys}"
    pyautogui.hotkey(*parts)
    return f"Pressed {keys}."


@skill()
def mouse(action: str, amount: int = 5) -> str:
    """Use the mouse at its current position. action: click, double_click, right_click, scroll_up or scroll_down."""
    import pyautogui

    act = action.strip().lower()
    if act in ("click", "double_click", "right_click"):
        {"click": pyautogui.click, "double_click": pyautogui.doubleClick, "right_click": pyautogui.rightClick}[act]()
    elif act in ("scroll_up", "scroll_down"):
        pyautogui.scroll((1 if act == "scroll_up" else -1) * max(1, min(amount, 50)) * 100)
    else:
        return "error: action must be click, double_click, right_click, scroll_up or scroll_down"
    return f"Mouse {act.replace('_', ' ')} done."


CLIPBOARD_GET = {"Darwin": ["pbpaste"], "Linux": ["xclip", "-selection", "clipboard", "-o"]}
CLIPBOARD_SET = {"Windows": ["clip"], "Darwin": ["pbcopy"], "Linux": ["xclip", "-selection", "clipboard"]}


@skill()
def read_clipboard() -> str:
    """Read the text currently on the clipboard."""
    out = (powershell("Get-Clipboard -Raw") if OS == "Windows" else run(CLIPBOARD_GET[OS])).stdout.strip()
    return out[:1000] or "The clipboard is empty."


@skill()
def set_clipboard(text: str) -> str:
    """Copy text to the clipboard."""
    # clip.exe reads the console code page; UTF-16 is what it understands for non-English text.
    data = text.encode("utf-16-le") if OS == "Windows" else text.encode()
    subprocess.run(CLIPBOARD_SET[OS], input=data, check=True, timeout=10)
    return "Copied to clipboard."


# ---------- system settings and info ----------

def _local_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("8.8.8.8", 80))  # UDP connect sends nothing; it only picks the outgoing interface
            return s.getsockname()[0]
        except OSError:
            return "offline"


@skill()
def system_info() -> str:
    """Get CPU and memory use, free disk space, IP address and uptime."""
    disk = psutil.disk_usage(str(HOME))
    up = int(time.time() - psutil.boot_time()) // 60
    return (f"CPU {psutil.cpu_percent(interval=0.5):.0f} percent, memory {psutil.virtual_memory().percent:.0f} percent used, "
            f"{disk.free // 2**30} GB free disk, IP {_local_ip()}, up {up // 60} hours {up % 60} minutes.")


# Windows radios (Wi-Fi/Bluetooth) via WinRT: works without admin rights, unlike netsh.
_WIN_RADIO = r"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$m = ([System.WindowsRuntimeSystemExtensions].GetMethods() | ? { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
Function Await($t, $type) { $n = $m.MakeGenericMethod($type).Invoke($null, @($t)); $n.Wait(-1) | Out-Null; $n.Result }
[Windows.Devices.Radios.Radio,Windows.System.Devices,ContentType=WindowsRuntime] | Out-Null
[Windows.Devices.Radios.RadioAccessStatus,Windows.System.Devices,ContentType=WindowsRuntime] | Out-Null
Await ([Windows.Devices.Radios.Radio]::RequestAccessAsync()) ([Windows.Devices.Radios.RadioAccessStatus]) | Out-Null
$radios = Await ([Windows.Devices.Radios.Radio]::GetRadiosAsync()) ([System.Collections.Generic.IReadOnlyList[Windows.Devices.Radios.Radio]])
$r = $radios | ? { $_.Kind -eq '__KIND__' } | Select-Object -First 1
if (-not $r) { Write-Output 'none'; exit }
[Windows.Devices.Radios.RadioState,Windows.System.Devices,ContentType=WindowsRuntime] | Out-Null
Await ($r.SetStateAsync('__STATE__')) ([Windows.Devices.Radios.RadioAccessStatus])
"""

RADIO_CMDS = {
    ("Darwin", "wifi"): lambda on: ["networksetup", "-setairportpower", "en0", "on" if on else "off"],
    ("Darwin", "bluetooth"): lambda on: ["blueutil", "--power", "1" if on else "0"],  # brew install blueutil
    ("Linux", "wifi"): lambda on: ["nmcli", "radio", "wifi", "on" if on else "off"],
    ("Linux", "bluetooth"): lambda on: ["rfkill", "unblock" if on else "block", "bluetooth"],
}


def _radio(kind: str, state: str) -> str:
    on = state.strip().lower() in ("on", "enable", "true", "1")
    if state.strip().lower() not in ("on", "off", "enable", "disable", "true", "false", "1", "0"):
        return "error: state must be on or off"
    label = "Wi-Fi" if kind == "wifi" else "Bluetooth"
    if OS == "Windows":
        script = _WIN_RADIO.replace("__KIND__", "WiFi" if kind == "wifi" else "Bluetooth").replace("__STATE__", "On" if on else "Off")
        out = powershell(script).stdout.strip()
        if out == "none":
            return f"This computer has no {label} radio."
        if "Allowed" not in out:
            return f"error: Windows did not allow changing {label} ({out or 'no answer'})"
    else:
        cmd = RADIO_CMDS.get((OS, kind))
        if cmd is None:
            return f"error: {label} control is not supported on {OS}"
        result = run(cmd(on))
        if result.returncode:
            return f"error: {result.stderr.strip() or 'command failed'}"
    return f"{label} turned {'on' if on else 'off'}."


@skill(confirm=True)
def wifi(state: str) -> str:
    """Turn Wi-Fi on or off. state is 'on' or 'off'. Turning it off disconnects the internet."""
    return _radio("wifi", state)


@skill()
def bluetooth(state: str) -> str:
    """Turn Bluetooth on or off. state is 'on' or 'off'."""
    return _radio("bluetooth", state)


@skill()
def brightness(level: int) -> str:
    """Set the screen brightness from 0 to 100 percent (built-in laptop screens)."""
    level = max(0, min(int(level), 100))
    if OS == "Windows":
        result = powershell("Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods | "
                            "Invoke-CimMethod -MethodName WmiSetBrightness "
                            f"-Arguments @{{Timeout=[uint32]1;Brightness=[byte]{level}}}")
    elif OS == "Darwin":
        result = run(["brightness", str(level / 100)])  # brew install brightness
    else:
        result = run(["brightnessctl", "set", f"{level}%"])
    if result.returncode:
        return "error: could not change brightness here (external monitors usually can't be controlled)."
    return f"Brightness set to {level} percent."


# ---------- scripts ----------

SCRIPT_TYPES = {"python": ".py", "powershell": ".ps1", "bash": ".sh", "batch": ".bat"}


def _interpreter(path: Path) -> list[str]:
    return {".py": [sys.executable], ".ps1": ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"],
            ".sh": ["bash"], ".bat": ["cmd", "/c"]}[path.suffix]


@skill()
def create_script(name: str, language: str, code: str) -> str:
    """Write a script file in Documents/Edith/scripts. language: python, powershell, bash or batch. code is the full script."""
    ext = SCRIPT_TYPES.get(language.strip().lower())
    if ext is None:
        return "error: language must be python, powershell, bash or batch"
    safe = "".join(c for c in name if c.isalnum() or c in "-_ ").strip().replace(" ", "_") or "script"
    path = SCRIPTS_DIR / (safe + ext)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(code, encoding="utf-8")
    return f"Saved {path}"


@skill(confirm=True)
def run_script(name: str) -> str:
    """Run a script saved with create_script and report its output. Runs with your user's full permissions."""
    matches = [p for p in SCRIPTS_DIR.glob("*") if p.stem.lower() == name.strip().lower().replace(" ", "_")] \
        if SCRIPTS_DIR.exists() else []
    if not matches:
        return f"error: no script called {name}"
    try:
        result = run(_interpreter(matches[0]) + [str(matches[0])], timeout=120)
    except subprocess.TimeoutExpired:
        return "error: the script ran longer than 2 minutes and was stopped"
    out = (result.stdout + result.stderr).strip()
    return f"Exit code {result.returncode}. {out[-800:] or 'No output.'}"


# ---------- WhatsApp ----------

def _contacts() -> dict[str, str]:
    try:
        return json.loads(CONTACTS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _phone(text: str) -> str | None:
    digits = "".join(c for c in text if c.isdigit())
    return digits if 10 <= len(digits) <= 15 else None


@skill()
def save_contact(name: str, phone: str) -> str:
    """Save a WhatsApp contact's phone number, with country code, e.g. name 'mom', phone '+91 98765 43210'."""
    number = _phone(phone)
    if number is None:
        return "error: phone number must have 10 to 15 digits including the country code"
    contacts = _contacts()
    contacts[name.strip().lower()] = number
    CONTACTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONTACTS_FILE.write_text(json.dumps(contacts, indent=2), encoding="utf-8")
    return f"Saved {name}."


WHATSAPP_LOAD_SECONDS = 12  # time for WhatsApp to open the chat before Enter is pressed


def _has_whatsapp_app() -> bool:
    if OS == "Windows":
        import winreg

        try:
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "whatsapp"))  # app registers whatsapp:// links
            return True
        except OSError:
            return False
    return OS == "Darwin" and Path("/Applications/WhatsApp.app").exists()


@skill(confirm=True)
def send_whatsapp(contact: str, message: str) -> str:
    """Send a WhatsApp message to a saved contact name or a phone number with country code."""
    number = _contacts().get(contact.strip().lower()) or _phone(contact)
    if number is None:
        return f"error: I don't have a number for {contact}. Ask me to save the contact first."
    # Desktop app where it exists; WhatsApp Web otherwise. Both open the chat with the text already typed.
    app = _has_whatsapp_app()
    url = (f"whatsapp://send?phone={number}&text={quote(message)}" if app
           else f"https://web.whatsapp.com/send?phone={number}&text={quote(message)}")
    webbrowser.open(url)
    import pyautogui

    time.sleep(WHATSAPP_LOAD_SECONDS if not app else 5)
    pyautogui.press("enter")
    return f"Message sent to {contact}."
