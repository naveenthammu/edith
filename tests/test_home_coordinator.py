"""Real coordination with doubles only at the journal/device network boundaries."""
from copy import deepcopy
import time

import pytest

from brain.hub import HubError
from connectors.home_coordinator import HomeCoordinator, CoordinationError, OutcomeUnknown


ACTION_ID = "419bcc36-c5f1-4c11-b4c6-ea81f87c23d3"
# Independently fixed SHA256 of compact sorted JSON for home.power / light.study / true.
ARGUMENT_HASH = "720e25ab30f83d7362d6617863495795eea7ce7edd5f45e466757ff3b7971b50"
PAYLOAD = {"entity_id": "light.study", "on": True}


def record(status):
    return {"id": ACTION_ID, "kind": "home.power", "payload": deepcopy(PAYLOAD),
            "argument_hash": ARGUMENT_HASH, "summary": "Turn light.study on",
            "created_at": int(time.time()), "expires_at": int(time.time()) + 300,
            "status": status, "evidence": None, "recovery_note": None}


class Hub:
    def __init__(self, events):
        self.events = events
        self.decision_override = self.claim_override = self.completion_override = None
        self.fail_decision = self.fail_claim = self.fail_completion = False
        self.claimed = False

    def propose_action(self, kind, payload, key):
        self.events.append(("propose", kind, deepcopy(payload), key))
        return record("awaiting_approval")

    def decide_action(self, action_id, decision, argument_hash):
        self.events.append(("decide", action_id, decision, argument_hash))
        if self.fail_decision:
            raise HubError("synthetic decision failure")
        return deepcopy(self.decision_override or record("approved" if decision == "approve" else "rejected"))

    def claim_action(self, action_id):
        self.events.append(("claim", action_id))
        if self.claimed or self.fail_claim:
            raise HubError("already claimed or unavailable")
        self.claimed = True
        return deepcopy(self.claim_override or record("executing"))

    def complete_action(self, action_id, status, evidence):
        self.events.append(("complete", action_id, status, deepcopy(evidence)))
        if self.fail_completion:
            raise HubError("synthetic-private-token must not be echoed")
        result = deepcopy(self.completion_override or record(status))
        result["evidence"] = deepcopy(evidence)
        return result


class Home:
    def __init__(self, events):
        self.events = events
        self.error = None
        self.result = {"entity_id": "light.study", "requested_state": "on", "observed_state": "on",
                       "status": "verified", "command_status": "accepted", "detail": "observed"}

    def set_power(self, entity_id, on):
        self.events.append(("home", entity_id, on))
        if self.error:
            raise self.error
        return deepcopy(self.result)


@pytest.fixture
def setup():
    events = []
    hub, home = Hub(events), Home(events)
    return HomeCoordinator(hub, home), hub, home, events


def test_prepare_only_proposes_exact_payload_and_never_executes(setup):
    coordinator, _, _, events = setup
    result = coordinator.prepare("light.study", True, "request-001")
    assert result["status"] == "awaiting_approval" and result["argument_hash"] == ARGUMENT_HASH
    assert events == [("propose", "home.power", PAYLOAD, "request-001")]


@pytest.mark.parametrize("entity,on,key", [("lock.front_door", True, "x"), ("light.*", True, "x"),
                                          ("light.study", "yes", "x"), ("light.study", 1, "x"),
                                          ("light.study", True, ""), ("light.study", True, "../x")])
def test_invalid_proposal_inputs_do_not_reach_network(setup, entity, on, key):
    coordinator, _, _, events = setup
    with pytest.raises(CoordinationError):
        coordinator.prepare(entity, on, key)
    assert events == []


def test_rejection_never_claims_or_calls_home(setup):
    coordinator, _, _, events = setup
    result = coordinator.decide_and_execute(ACTION_ID, ARGUMENT_HASH, False)
    assert result["status"] == "rejected"
    assert events == [("decide", ACTION_ID, "reject", ARGUMENT_HASH)]


def test_verified_execution_is_claimed_once_then_persisted_with_bound_evidence(setup):
    coordinator, _, _, events = setup
    result = coordinator.decide_and_execute(ACTION_ID, ARGUMENT_HASH, True)
    evidence = {"entity_id": "light.study", "requested_state": "on", "observed_state": "on", "command_status": "accepted"}
    assert events == [("decide", ACTION_ID, "approve", ARGUMENT_HASH), ("claim", ACTION_ID),
                      ("home", "light.study", True), ("complete", ACTION_ID, "verified", evidence)]
    assert result["status"] == "verified" and result["evidence"] == evidence


@pytest.mark.parametrize("field,value", [("id", "other-action"), ("argument_hash", "f" * 64),
                                       ("kind", "message.send"), ("payload", {"entity_id": "light.other", "on": True}),
                                       ("payload", {"entity_id": "light.study", "on": 1}),
                                       ("payload", {"entity_id": "light.study", "on": True, "all": True}),
                                       ("status", "awaiting_approval")])
def test_mismatched_approval_receipt_prevents_claim_and_effect(setup, field, value):
    coordinator, hub, _, events = setup
    hub.decision_override = record("approved")
    hub.decision_override[field] = value
    with pytest.raises(CoordinationError):
        coordinator.decide_and_execute(ACTION_ID, ARGUMENT_HASH, True)
    assert len(events) == 1 and events[0][0] == "decide"


def test_claimed_payload_must_match_approved_payload_before_effect(setup):
    coordinator, hub, _, events = setup
    hub.claim_override = record("executing")
    hub.claim_override["payload"]["on"] = False
    with pytest.raises(CoordinationError):
        coordinator.decide_and_execute(ACTION_ID, ARGUMENT_HASH, True)
    assert [event[0] for event in events] == ["decide", "claim"]


def test_duplicate_claim_never_replays_the_effect(setup):
    coordinator, _, _, events = setup
    coordinator.decide_and_execute(ACTION_ID, ARGUMENT_HASH, True)
    with pytest.raises(CoordinationError):
        coordinator.decide_and_execute(ACTION_ID, ARGUMENT_HASH, True)
    assert [event for event in events if event[0] == "home"] == [("home", "light.study", True)]


@pytest.mark.parametrize("failure", ["fail_decision", "fail_claim"])
def test_network_error_before_execution_never_calls_home(setup, failure):
    coordinator, hub, _, events = setup
    setattr(hub, failure, True)
    with pytest.raises(CoordinationError):
        coordinator.decide_and_execute(ACTION_ID, ARGUMENT_HASH, True)
    assert not any(event[0] == "home" for event in events)


def test_uncertain_adapter_result_stays_unknown_without_retry(setup):
    coordinator, _, home, events = setup
    home.result.update(status="unknown", command_status="unknown", observed_state="on")
    result = coordinator.decide_and_execute(ACTION_ID, ARGUMENT_HASH, True)
    assert result["status"] == "unknown"
    assert result["evidence"]["observed_state"] == "on"
    assert len([event for event in events if event[0] == "home"]) == 1


def test_adapter_exception_is_recorded_conservatively_without_exception_text(setup):
    coordinator, _, home, _ = setup
    home.error = RuntimeError("synthetic-private-token")
    result = coordinator.decide_and_execute(ACTION_ID, ARGUMENT_HASH, True)
    assert result["status"] == "unknown"
    assert result["evidence"] == {"entity_id": "light.study", "requested_state": "on",
                                  "observed_state": "unknown", "command_status": "unknown"}
    assert "synthetic-private-token" not in str(result)


def test_unmatched_adapter_evidence_cannot_mark_journal_verified(setup):
    coordinator, _, home, _ = setup
    home.result["entity_id"] = "light.other"
    result = coordinator.decide_and_execute(ACTION_ID, ARGUMENT_HASH, True)
    assert result["status"] == "unknown"
    assert result["evidence"]["entity_id"] == "light.study"
    assert result["evidence"]["observed_state"] == "unknown"


def test_failed_result_persistence_is_explicit_unknown_and_cannot_trigger_replay(setup):
    coordinator, hub, _, events = setup
    hub.fail_completion = True
    with pytest.raises(OutcomeUnknown) as error:
        coordinator.decide_and_execute(ACTION_ID, ARGUMENT_HASH, True)
    assert error.value.status == "unknown" and error.value.action_id == ACTION_ID
    assert "synthetic-private-token" not in str(error.value)
    assert "not retried" in str(error.value).lower()
    with pytest.raises(CoordinationError):
        coordinator.decide_and_execute(ACTION_ID, ARGUMENT_HASH, True)
    assert len([event for event in events if event[0] == "home"]) == 1


def test_mismatched_completion_receipt_cannot_be_reported_as_success(setup):
    coordinator, hub, _, events = setup
    hub.completion_override = record("verified")
    hub.completion_override["id"] = "other-action"
    with pytest.raises(OutcomeUnknown):
        coordinator.decide_and_execute(ACTION_ID, ARGUMENT_HASH, True)
    assert len([event for event in events if event[0] == "home"]) == 1


@pytest.mark.parametrize("approve", ["yes", 1, None])
def test_approval_parameter_requires_trusted_boolean_not_text(setup, approve):
    coordinator, _, _, events = setup
    with pytest.raises(CoordinationError):
        coordinator.decide_and_execute(ACTION_ID, ARGUMENT_HASH, approve)
    assert events == []
