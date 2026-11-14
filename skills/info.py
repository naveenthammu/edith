from datetime import datetime
from urllib.parse import quote

import requests

from skills import skill

HEADERS = {"User-Agent": "edith-assistant/3.0"}
TIMEOUT = 8


@skill()
def get_datetime() -> str:
    """Get the current local date and time."""
    return datetime.now().strftime("%A %d %B %Y, %I:%M %p")


@skill()
def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    geo = requests.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": city, "count": 1},
        headers=HEADERS,
        timeout=TIMEOUT,
    ).json()
    if not geo.get("results"):
        return f"Could not find a place called {city}."
    place = geo["results"][0]
    current = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code",
        },
        headers=HEADERS,
        timeout=TIMEOUT,
    ).json()["current"]
    return (
        f"{place['name']}: {current['temperature_2m']} degrees Celsius, "
        f"humidity {current['relative_humidity_2m']} percent, wind {current['wind_speed_10m']} km/h, "
        f"WMO weather code {current['weather_code']}."
    )


@skill()
def wikipedia(topic: str) -> str:
    """Get a short Wikipedia summary of a topic."""
    url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + quote(topic.strip().replace(" ", "_"))
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    if resp.status_code == 404:
        return f"No Wikipedia article found for {topic}."
    resp.raise_for_status()
    return resp.json().get("extract", "")[:600]
