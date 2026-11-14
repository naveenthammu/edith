import subprocess
import time
import webbrowser
from urllib.parse import quote_plus, urlparse

from skills import skill

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
}


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
    """Open a Windows application by name, for example notepad, calculator, vs code, chrome or settings."""
    target = resolve_app(name)
    if target:
        # Safe: target comes from the fixed APPS map, never from user text.
        subprocess.Popen(f'start "" {target}', shell=True)
        return f"Opened {name}"
    import pyautogui

    pyautogui.press("win")
    time.sleep(0.6)
    pyautogui.write(name, interval=0.03)
    time.sleep(0.6)
    pyautogui.press("enter")
    return f"Searched the Start menu for {name}"
