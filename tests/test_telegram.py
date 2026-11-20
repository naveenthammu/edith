from skills import telegram


def test_only_owner_messages_are_returned(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    updates = [{"update_id": 5, "message": {"chat": {"id": 99, "type": "private"}, "from": {"id": 99}, "text": "stranger"}},
               {"update_id": 6, "message": {"chat": {"id": 42, "type": "private"}, "from": {"id": 42}, "text": "hi edith"}}]
    monkeypatch.setattr(telegram, "_api", lambda method, **kw: updates)
    assert telegram._next_text(5) == "hi edith"
    assert telegram._offset == 7


def test_group_and_forged_sender_are_ignored_before_genuine_private_owner(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    updates = [
        {"update_id": 1, "message": {"chat": {"id": 42, "type": "group"}, "from": {"id": 42}, "text": "group command"}},
        {"update_id": 2, "message": {"chat": {"id": 42, "type": "private"}, "from": {"id": 99}, "text": "forged command"}},
        {"update_id": 3, "message": {"chat": {"id": 42, "type": "private"}, "from": {"id": 42}, "text": "owner command"}},
    ]
    monkeypatch.setattr(telegram, "_api", lambda *args, **kwargs: updates)
    assert telegram._next_text(1) == "owner command"


def test_group_chat_configuration_never_polls_for_commands(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-10042")
    monkeypatch.setattr(telegram, "_api", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("group should not poll")))
    assert telegram._next_text(1) is None


def test_send_needs_config(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert telegram.send_telegram("x").startswith("error")


def test_backlog_is_skipped_on_start(monkeypatch):
    monkeypatch.setattr(telegram, "_offset", 0)
    monkeypatch.setattr(telegram, "_api", lambda method, **kw: [{"update_id": 41, "message": {"text": "shutdown"}}])
    telegram._skip_backlog()
    assert telegram._offset == 42


def test_errors_never_carry_the_token(monkeypatch):
    import pytest
    import requests

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRET")

    def boom(url, **kw):
        raise requests.ConnectionError(f"failed for {url}")

    monkeypatch.setattr(telegram.requests, "post", boom)
    with pytest.raises(requests.RequestException) as e:
        telegram._api("getUpdates")
    assert "SECRET" not in str(e.value) and e.value.__cause__ is None
