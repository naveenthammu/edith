import copy
import threading
from collections import deque

import pytest

from brain.reminders import ReminderDispatcher


def reminder(**changes):
    row = {"id": "r1", "text": "Private fixture reminder", "due_at": 900,
           "status": "scheduled", "delivery_id": "delivery1", "lease_expires_at": 1120}
    row.update(changes)
    return row


class Clock:
    now = 1000

    def __call__(self):
        return self.now


class FakeHub:
    def __init__(self, rows=()):
        self.identity = {"principal_id": "owner1", "role": "adult", "capabilities": {"reminders": True}}
        self.rows = deque(rows)
        self.capability_calls = 0
        self.claims = 0
        self.deliveries = []
        self.after_claim = lambda: None
        self.claim_error = None
        self.delivery_error = None

    def capabilities(self):
        self.capability_calls += 1
        return copy.deepcopy(self.identity)

    def claim_due_reminder(self):
        self.claims += 1
        if self.claim_error:
            raise self.claim_error
        result = self.rows.popleft() if self.rows else None
        self.after_claim()
        return {"reminder": result}

    def complete_reminder_delivery(self, reminder_id, delivery_id, status):
        self.deliveries.append((reminder_id, delivery_id, status))
        if self.delivery_error:
            raise self.delivery_error
        return {"id": reminder_id, "status": "announced" if status == "announced" else "scheduled"}


class FakeSpeaker:
    def __init__(self):
        self.generation = 0
        self.outcome = "completed"
        self.calls = []
        self.after_speech = lambda: None

    def say_tracked(self, text, *, expected_generation=None):
        self.calls.append((text, expected_generation))
        if expected_generation != self.generation:
            return "cancelled"
        self.after_speech()
        return self.outcome

    def stop(self):
        self.generation += 1


def setup(rows=(), **kwargs):
    hub = FakeHub(rows)
    speaker = FakeSpeaker()
    gate = {"allowed": True}
    lock = threading.Lock()
    clock = Clock()
    dispatcher = ReminderDispatcher(hub, speaker, lambda: gate["allowed"], lock,
                                    expected_principal="owner1", clock=clock, **kwargs)
    return dispatcher, hub, speaker, gate, lock, clock


def test_idle_or_muted_dispatcher_never_claims_or_starts_itself():
    dispatcher, hub, speaker, gate, _, _ = setup([reminder()])
    assert hub.claims == 0
    gate["allowed"] = False
    assert dispatcher.poll_once() == "idle"
    assert hub.capability_calls == 0
    assert hub.claims == 0
    assert speaker.calls == []


def test_active_conversation_lock_prevents_claim_without_waiting():
    dispatcher, hub, _, _, lock, _ = setup([reminder()])
    with lock:
        assert dispatcher.poll_once() == "idle"
    assert hub.claims == 0


@pytest.mark.parametrize("change", [
    {"role": "child"}, {"role": "guest"}, {"principal_id": "someone_else"},
    {"capabilities": {"reminders": False}}, {"capabilities": {}},
])
def test_only_configured_owner_adult_or_elder_capability_can_claim(change):
    dispatcher, hub, speaker, _, _, _ = setup([reminder()])
    hub.identity.update(change)
    assert dispatcher.poll_once() == "idle"
    assert hub.claims == 0
    assert speaker.calls == []


def test_completed_current_owner_speech_marks_one_reminder_announced():
    dispatcher, hub, speaker, _, lock, _ = setup([reminder(), reminder(id="r2")])
    hub.identity["role"] = "elder"
    assert dispatcher.poll_once() == "announced"
    assert hub.claims == 1
    assert len(hub.rows) == 1
    assert speaker.calls == [("Private fixture reminder", 0)]
    assert hub.deliveries == [("r1", "delivery1", "announced")]
    assert hub.capability_calls >= 3  # before claim, before speech, before receipt
    assert lock.acquire(blocking=False)
    lock.release()


@pytest.mark.parametrize("change", ["mute", "generation", "identity"])
def test_changed_gate_generation_or_identity_after_claim_retries_without_speech(change):
    dispatcher, hub, speaker, gate, _, _ = setup([reminder()])

    def after_claim():
        if change == "mute":
            gate["allowed"] = False
        elif change == "generation":
            speaker.stop()
        else:
            hub.identity["role"] = "child"

    hub.after_claim = after_claim
    assert dispatcher.poll_once() == "retry"
    assert speaker.calls == []
    assert hub.deliveries == [("r1", "delivery1", "retry")]


@pytest.mark.parametrize("change", ["mute", "generation", "identity"])
def test_changed_gate_or_identity_after_speech_never_marks_announced(change):
    dispatcher, hub, speaker, gate, _, _ = setup([reminder()])

    def after_speech():
        if change == "mute":
            gate["allowed"] = False
        elif change == "generation":
            speaker.stop()
        else:
            hub.identity["principal_id"] = "different_owner"

    speaker.after_speech = after_speech
    assert dispatcher.poll_once() == "retry"
    assert hub.deliveries == [("r1", "delivery1", "retry")]


@pytest.mark.parametrize("outcome", ["failed", "cancelled", None, "heard"])
def test_only_explicit_completed_speech_can_be_recorded_as_announced(outcome):
    dispatcher, hub, speaker, _, _, _ = setup([reminder()])
    speaker.outcome = outcome
    assert dispatcher.poll_once() == "retry"
    assert hub.deliveries[-1][-1] == "retry"


def test_unknown_claim_is_not_blindly_retried_until_lease_window_passes(caplog):
    dispatcher, hub, speaker, _, _, clock = setup([reminder()])
    hub.claim_error = TimeoutError("Private fixture reminder")
    assert dispatcher.poll_once() == "unknown"
    assert dispatcher.poll_once() == "idle"
    clock.now = 1119
    assert dispatcher.poll_once() == "idle"
    assert hub.claims == 1
    assert hub.deliveries == []
    assert speaker.calls == []
    assert "Private fixture reminder" not in caplog.text
    clock.now = 1120
    hub.claim_error = None
    hub.rows = deque([reminder(delivery_id="fresh_lease", lease_expires_at=1240)])
    assert dispatcher.poll_once() == "announced"


def test_unknown_receipt_leaves_lease_without_repeating_mutation_or_speech(caplog):
    dispatcher, hub, speaker, _, _, clock = setup([reminder(), reminder(id="r2", lease_expires_at=1240)])
    hub.delivery_error = TimeoutError("Private fixture reminder")
    assert dispatcher.poll_once() == "unknown"
    assert hub.deliveries == [("r1", "delivery1", "announced")]
    assert dispatcher.poll_once() == "idle"
    assert hub.claims == 1
    assert len(speaker.calls) == 1
    assert "Private fixture reminder" not in caplog.text
    clock.now = 1120
    hub.delivery_error = None
    assert dispatcher.poll_once() == "announced"
    assert hub.claims == 2


@pytest.mark.parametrize("changes", [
    {"text": "x" * 401}, {"text": ""}, {"due_at": 2000}, {"delivery_id": ""},
    {"lease_expires_at": 999}, {"status": "cancelled"}, {"due_at": True},
])
def test_malformed_future_or_expired_claim_never_speaks_or_records_success(changes):
    dispatcher, hub, speaker, _, _, _ = setup([reminder(**changes)])
    assert dispatcher.poll_once() == "unknown"
    assert speaker.calls == []
    assert hub.deliveries == []


def test_lease_expiring_during_speech_is_left_unconfirmed():
    dispatcher, hub, speaker, _, _, clock = setup([reminder()])
    speaker.after_speech = lambda: setattr(clock, "now", 1120)
    assert dispatcher.poll_once() == "unknown"
    assert hub.deliveries == []


def test_lease_expiring_during_access_recheck_does_not_start_speech():
    dispatcher, hub, speaker, _, _, clock = setup([reminder()])
    original = hub.capabilities

    def capabilities():
        result = original()
        if hub.capability_calls == 2:
            clock.now = 1120
        return result

    hub.capabilities = capabilities
    assert dispatcher.poll_once() == "unknown"
    assert speaker.calls == []
    assert hub.deliveries == []


def test_malformed_completion_receipt_is_not_reported_or_retried_as_success():
    dispatcher, hub, _, _, _, _ = setup([reminder()])
    original = hub.complete_reminder_delivery

    def wrong_receipt(*args):
        original(*args)
        return {"id": "another-reminder", "status": "announced"}

    hub.complete_reminder_delivery = wrong_receipt
    assert dispatcher.poll_once() == "unknown"
    assert dispatcher.poll_once() == "idle"
    assert len(hub.deliveries) == 1


def test_speech_exception_retries_without_logging_reminder_contents(caplog):
    dispatcher, hub, speaker, _, _, _ = setup([reminder()])

    def fail():
        raise RuntimeError("Private fixture reminder")

    speaker.after_speech = fail
    assert dispatcher.poll_once() == "retry"
    assert hub.deliveries[-1][-1] == "retry"
    assert "Private fixture reminder" not in caplog.text


def test_stop_interrupts_only_active_dispatch_speech_and_releases_turn_lock():
    dispatcher, hub, _, _, lock, _ = setup([reminder()])
    entered = threading.Event()
    cancelled = threading.Event()

    class BlockingSpeaker(FakeSpeaker):
        def say_tracked(self, text, *, expected_generation=None):
            entered.set()
            assert cancelled.wait(2)
            return "cancelled"

        def stop(self):
            super().stop()
            cancelled.set()

    dispatcher.speaker = BlockingSpeaker()
    assert dispatcher.start() is True
    assert entered.wait(1)
    assert not lock.acquire(blocking=False)
    assert dispatcher.start() is False
    assert dispatcher.stop(timeout=2) is True
    assert cancelled.is_set()
    assert hub.deliveries == [("r1", "delivery1", "retry")]
    assert lock.acquire(blocking=False)
    lock.release()
    assert dispatcher.poll_once() == "idle"


def test_injected_stop_event_prevents_start_and_claim():
    event = threading.Event()
    event.set()
    dispatcher, hub, speaker, _, _, _ = setup([reminder()], stop_event=event)
    assert dispatcher.start() is False
    assert dispatcher.poll_once() == "idle"
    assert dispatcher.stop() is True
    assert hub.claims == 0
    assert speaker.calls == []
