"""Telegram: EDITH sends messages to your phone, and answers what you text the bot.

Needs TELEGRAM_BOT_TOKEN (from @BotFather) and TELEGRAM_CHAT_ID (your own chat) in .env.
Only the configured person's private-chat messages are accepted. Household/desktop
capabilities require separately paired access; main supplies a public-information registry.
"""
import logging
import os
import threading
import time

import requests

from skills import skill

log = logging.getLogger("edith.telegram")
POLL_SECONDS = 30  # Telegram long-poll: the request waits up to this long for a new message
CONFIRM_SECONDS = 60

_offset = 0  # next update id to fetch; only the bot thread touches it


def _api(method: str, http_timeout: float = 15, **params) -> dict:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=params, timeout=http_timeout)
        r.raise_for_status()
    except requests.RequestException as e:
        # requests puts the URL, and so the bot token, in its messages: never let it reach the log
        code = getattr(e.response, "status_code", None) or type(e).__name__
        raise requests.RequestException(f"Telegram {method} failed ({code})") from None
    return r.json()["result"]


def _owner() -> str:
    value = os.getenv("TELEGRAM_CHAT_ID") or ""
    return value if value.isascii() and value.isdigit() and int(value) > 0 else ""


def _send(text: str) -> None:
    for i in range(0, len(text), 4000):  # Telegram's limit is 4096 chars per message
        _api("sendMessage", chat_id=_owner(), text=text[i:i + 4000])


def _next_text(wait: float) -> str | None:
    """Next text message from the owner within wait seconds, or None."""
    global _offset
    owner = _owner()
    if not owner:
        return None
    deadline = time.monotonic() + wait
    while (left := deadline - time.monotonic()) > 0:
        poll = int(min(POLL_SECONDS, max(left, 1)))
        for u in _api("getUpdates", http_timeout=poll + 10, offset=_offset, timeout=poll):
            _offset = u["update_id"] + 1
            msg = u.get("message") or {}
            chat, sender = msg.get("chat") or {}, msg.get("from") or {}
            if (chat.get("type") == "private" and str(chat.get("id")) == owner
                    and str(sender.get("id")) == owner and not sender.get("is_bot", False)
                    and isinstance(msg.get("text"), str) and msg["text"]):
                return msg["text"]
    return None


def _skip_backlog() -> None:
    """Start after the newest waiting message: Telegram keeps unread ones for 24 h, and old commands must not run."""
    global _offset
    old = _api("getUpdates", offset=-1)
    if old:
        _offset = old[-1]["update_id"] + 1


@skill(confirm=True)
def send_telegram(text: str) -> str:
    """Send a Telegram message to the user's phone, e.g. a link, a list or a note to read later."""
    if not os.getenv("TELEGRAM_BOT_TOKEN") or not _owner():
        return "error: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env"
    try:
        _send(text)
    except requests.RequestException as e:
        return f"error: Telegram send failed ({e})"
    return "Sent on Telegram."


def start_bot(make_agent) -> None:
    """Answer the owner's Telegram messages in a background thread.

    make_agent(confirm) builds an Agent; confirm-gated skills ask "yes?" in the chat.
    """
    if not os.getenv("TELEGRAM_BOT_TOKEN") or not _owner():
        return

    def confirm(action: str) -> bool:
        _send(f"Are you sure you want to {action}? Reply yes.")
        from brain.approval import is_affirmative
        return is_affirmative(_next_text(CONFIRM_SECONDS) or "")

    def loop():
        agent = make_agent(confirm)
        fresh = False
        while True:
            try:
                if not fresh:
                    _skip_backlog()
                    fresh = True
                text = _next_text(POLL_SECONDS)
                if text is None:
                    continue
                if text.strip().lower() in ("/start", "/reset"):
                    agent.reset()
                    _send("Edith here. Ask me anything.")
                    continue
                log.info("telegram utterance received (%d characters)", len(text))
                _send(agent.handle(text))
            except Exception:
                log.exception("telegram bot failed")
                time.sleep(10)  # network down: back off instead of spinning

    threading.Thread(target=loop, daemon=True, name="telegram").start()
