import re
import webbrowser
from urllib.parse import quote_plus

import requests

from skills import skill

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
    _press(key, 1 if key == "volumemute" else max(1, min(steps, 50)))
    return f"Volume {action}"
