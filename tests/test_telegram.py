from skills import telegram


def test_only_owner_messages_are_returned(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    updates = [{"update_id": 5, "message": {"chat": {"id": 99}, "text": "stranger"}},
               {"update_id": 6, "message": {"chat": {"id": 42}, "text": "hi edith"}}]
    monkeypatch.setattr(telegram, "_api", lambda method, **kw: updates)
    assert telegram._next_text(5) == "hi edith"
    assert telegram._offset == 7


def test_send_needs_config(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert telegram.send_telegram("x").startswith("error")
