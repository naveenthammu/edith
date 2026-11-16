import pytest

from skills import google_apps
from skills.notifications import format_notifications


class FakeApi:
    """Chainable stand-in for a googleapiclient service: records calls, returns canned results by call path."""

    def __init__(self, results, calls, path=()):
        self.results, self.calls, self.path = results, calls, path

    def __getattr__(self, name):
        def call(**kw):
            self.calls.append((".".join(self.path + (name,)), kw))
            return FakeApi(self.results, self.calls, self.path + (name,))
        return call

    def execute(self):
        return self.results.get(".".join(self.path), {})


def fake_google(monkeypatch, results):
    calls = []
    monkeypatch.setattr(google_apps, "_service", lambda name, version: FakeApi(results, calls))
    return calls


def test_notifications_newest_first_and_filtered():
    toasts = [("WhatsApp", ["Mom", "Call me"]), ("Gmail", ["Amazon", "Shipped"]), ("WhatsApp", ["Ravi", "Hi"])]
    assert format_notifications(toasts).splitlines()[1] == "WhatsApp: Ravi - Hi"
    only = format_notifications(toasts, "whatsapp")
    assert only.startswith("2 notification(s)") and "Gmail" not in only
    assert format_notifications(toasts, "teams") == "No teams notifications."


def test_read_email_lists_sender_subject_preview(monkeypatch):
    fake_google(monkeypatch, {
        "users.messages.list": {"messages": [{"id": "1"}]},
        "users.messages.get": {"payload": {"headers": [{"name": "From", "value": "Amazon"},
                                                       {"name": "Subject", "value": "Order shipped"}]},
                               "snippet": "Your order is on the way"},
    })
    assert google_apps.read_email() == "From Amazon: Order shipped - Your order is on the way"


def test_add_task_creates_missing_list(monkeypatch):
    calls = fake_google(monkeypatch, {
        "tasklists.list": {"items": [{"id": "a", "title": "My Tasks"}]},
        "tasklists.insert": {"id": "b", "title": "Bucket List"},
    })
    assert google_apps.add_task("See the northern lights", "bucket list") == "Added See the northern lights to bucket list."
    assert ("tasks.insert", {"tasklist": "b", "body": {"title": "See the northern lights"}}) in calls


def test_event_line_timed_and_all_day():
    assert google_apps.event_line({"start": {"dateTime": "2026-11-16T15:00:00+05:30"}, "summary": "Standup"}) \
        == "Mon 16 Nov 03:00 PM: Standup"
    assert google_apps.event_line({"start": {"date": "2026-11-17"}}) == "Tue 17 Nov (all day): (no title)"


def test_bad_input_is_rejected_before_calling_google(monkeypatch):
    calls = fake_google(monkeypatch, {})
    assert google_apps.add_calendar_event("Gym", "tomorrow evening").startswith("error")
    assert google_apps.send_email("ravi", "hi", "hello").startswith("error")
    assert calls == []


def test_not_connected_messages(monkeypatch, tmp_path):
    monkeypatch.setattr(google_apps, "CREDENTIALS", tmp_path / "credentials.json")
    monkeypatch.setattr(google_apps, "TOKEN", tmp_path / "token.json")
    assert "credentials.json" in google_apps.connect_google()
    with pytest.raises(RuntimeError, match="connect Google"):
        google_apps._service("gmail", "v1")
