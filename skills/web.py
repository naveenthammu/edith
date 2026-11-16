import re
import shutil
import subprocess
import time
import webbrowser
from urllib.parse import quote_plus, urlparse

from skills import skill
from skills.computer import OS

APPS = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "calc": "calc.exe",
    "paint": "mspaint.exe",
    "file explorer": "explorer.exe",
    "explorer": "explorer.exe",
    "command prompt": "cmd.exe",
    "cmd": "cmd.exe",
    "task manager": "taskmgr.exe",
    "settings": "ms-settings:",
    "vs code": "code",
    "vscode": "code",
    "visual studio code": "code",
    "chrome": "chrome",
    "edge": "msedge",
    "whatsapp": "whatsapp:",
    "terminal": "wt",
}


# macOS app bundle names for `open -a`, and Linux launch commands, for the same spoken names.
MAC_APPS = {"notepad": "TextEdit", "calculator": "Calculator", "calc": "Calculator", "chrome": "Google Chrome",
            "vs code": "Visual Studio Code", "vscode": "Visual Studio Code", "visual studio code": "Visual Studio Code",
            "settings": "System Settings", "file explorer": "Finder", "explorer": "Finder", "terminal": "Terminal",
            "edge": "Microsoft Edge", "whatsapp": "WhatsApp"}
LINUX_APPS = {"notepad": "gnome-text-editor", "calculator": "gnome-calculator", "calc": "gnome-calculator",
              "chrome": "google-chrome", "vs code": "code", "vscode": "code", "visual studio code": "code",
              "settings": "gnome-control-center", "file explorer": "nautilus", "explorer": "nautilus",
              "terminal": "gnome-terminal", "edge": "microsoft-edge"}
PLAIN_NAME = re.compile(r"[A-Za-z0-9 .+-]{1,40}")  # app names only: never paths, switches or shell syntax


def resolve_app(name: str) -> str | None:
    return APPS.get(name.strip().lower())


def normalize_url(url: str) -> str | None:
    url = url.strip()
    if "://" not in url and ":" not in url.split("/")[0]:
        url = "https://" + url
    return url if urlparse(url).scheme in ("http", "https") else None


@skill()
def open_website(url: str) -> str:
    """Open a website in the default browser, for example youtube.com, web.whatsapp.com, instagram.com or mail.google.com."""
    target = normalize_url(url)
    if target is None:
        return "error: only http and https websites can be opened"
    webbrowser.open(target)
    return f"Opened {target}"


@skill()
def search_google(query: str) -> str:
    """Search Google for a query in the browser."""
    webbrowser.open("https://www.google.com/search?q=" + quote_plus(query))
    return f"Searched Google for {query}"


@skill()
def open_app(name: str) -> str:
    """Open an application by name, for example notepad, calculator, vs code, chrome, whatsapp or settings."""
    if OS != "Windows":
        return _open_app_unix(name)
    target = resolve_app(name)
    if target:
        # Safe: target comes from the fixed APPS map, never from user text.
        subprocess.Popen(f'start "" {target}', shell=True)
        return f"Opened {name}"
    # Windows Search runs command lines, so only plain app names may be typed.
    if not PLAIN_NAME.fullmatch(name.strip()):
        return "error: unknown app name"
    import pyautogui

    pyautogui.press("win")
    time.sleep(0.6)
    pyautogui.write(name, interval=0.03)
    time.sleep(0.6)
    pyautogui.press("enter")
    return f"Searched the Start menu for {name}"


def _open_app_unix(name: str) -> str:
    key = name.strip().lower()
    if not PLAIN_NAME.fullmatch(name.strip()):
        return "error: unknown app name"
    if OS == "Darwin":  # list args, no shell: the name can't run anything but an app
        result = subprocess.run(["open", "-a", MAC_APPS.get(key, name.strip())], capture_output=True, text=True)
        return f"Opened {name}" if result.returncode == 0 else f"error: no app called {name}"
    cmd = LINUX_APPS.get(key) or shutil.which(key.replace(" ", "-")) or shutil.which(key.replace(" ", ""))
    if not cmd or not shutil.which(cmd):
        return f"error: no app called {name}"
    subprocess.Popen([cmd], start_new_session=True)
    return f"Opened {name}"
