import re
import subprocess
import webbrowser
from urllib.parse import quote_plus

import requests

from skills import skill
from skills.computer import OS

VOLUME_KEYS = {"up": "volumeup", "down": "volumedown", "mute": "volumemute"}


def first_video_id(html: str) -> str | None:
    match = re.search(r'"videoId":"([\w-]{11})"', html)
    return match.group(1) if match else None


def _press(key: str, presses: int) -> None:
    import pyautogui

    pyautogui.press(key, presses=presses)


@skill()
def play_youtube(query: str) -> str:
    """Play a song or video on YouTube by name."""
    search_url = "https://www.youtube.com/results?search_query=" + quote_plus(query)
    try:
        html = requests.get(search_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8).text
        video_id = first_video_id(html)
    except requests.RequestException:
        video_id = None
    webbrowser.open(f"https://www.youtube.com/watch?v={video_id}" if video_id else search_url)
    return f"Playing {query} on YouTube" if video_id else f"Opened YouTube search for {query}"


@skill()
def volume(action: str, steps: int = 5) -> str:
    """Change the system volume. action is 'up', 'down' or 'mute'. Each step is 2 percent."""
    key = VOLUME_KEYS.get(action.strip().lower())
    if key is None:
        return "error: action must be up, down or mute"
    steps = 1 if key == "volumemute" else max(1, min(steps, 50))
    if OS == "Windows":
        _press(key, steps)
    else:
        subprocess.run(_volume_cmd(key, steps * 2), check=False)
    return f"Volume {action}"


@skill()
def set_volume(percent: int) -> str:
    """Set the system volume to a level from 0 to 100 percent, e.g. 'volume to 50'."""
    percent = max(0, min(int(percent), 100))
    if OS == "Windows":
        # shortcut: no volume API without a new dependency; each key press is 2 %, so go to 0 then climb
        _press("volumedown", 50)
        _press("volumeup", percent // 2)
    elif OS == "Darwin":
        subprocess.run(["osascript", "-e", f"set volume output volume {percent}"], check=False)
    else:
        subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{percent}%"], check=False)
    return f"Volume set to {percent} percent."


def _volume_cmd(key: str, percent: int) -> list[str]:
    if OS == "Darwin":
        script = ("set volume output muted (not output muted of (get volume settings))" if key == "volumemute" else
                  f"set volume output volume ((output volume of (get volume settings)) "
                  f"{'+' if key == 'volumeup' else '-'} {percent})")
        return ["osascript", "-e", script]
    if key == "volumemute":
        return ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"]
    return ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{'+' if key == 'volumeup' else '-'}{percent}%"]
