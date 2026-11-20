"""Shared, conservative parsing for an explicit confirmation response.

This answers only whether the response is affirmative. The caller still owns
authentication, the exact proposed action, expiry and authorization.
"""

import re


_AFFIRMATIVES = frozenset({
    "yes", "yes please", "yes proceed", "yes do it", "confirm", "confirmed", "approve", "i confirm",
})


def is_affirmative(text: str) -> bool:
    """Accept a short explicit affirmative, never a yes embedded in instructions."""
    if not isinstance(text, str) or len(text) > 80:
        return False
    normalized = text.strip().casefold()
    if not re.fullmatch(r"[a-z\s,.!?]+", normalized):
        return False
    normalized = " ".join(re.sub(r"[,.!?]", " ", normalized).split())
    return normalized in _AFFIRMATIVES
