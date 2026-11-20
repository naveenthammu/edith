from copy import deepcopy
import hashlib
import json

import pytest

from connectors.message_coordinator import MessageCoordinator, MessageCoordinationError, MessageOutcomeUnknown


PAYLOAD = {"contact_id": "contact-1", "contact_version": "a" * 64, "recipient": "friend@example.test",
           "subject": "Meeting — update", "body": "Hello friend.\nSecond line."}
ARGUMENT_HASH = "d7dc3093e6ded6d5ca5431077b7b491d60b17036772999f4a0a8c8ea7c92ed1a"


def row(status="awaiting_approval"):
    return {"id": "message-1", "kind": "contact.email", "payload": deepcopy(PAYLOAD), "argument_hash": ARGUMENT_HASH,
            "created_at": 1000, "expires_at": 1300, "status": status, "evidence": None, "recovery_note": None}


def evidence(identifier=None):
    return {"provider": "gmail", "recipient": "friend@example.test", "provider_message_id": identifier,
            "delivery_status": "unverified"}


class Journal:
    def __init__(self):
        self.record = row()
        self.events = []
        self.corrupt_decision = None
        self.corrupt_claim = None
        self.corrupt_result = None
        self.fail_result = None
        self.claim_error = False

    def propose_message(self, contact_id, contact_version, subject, body, idempotency_key):
        self.events.append(("propose", contact_id, contact_version, subject, body, idempotency_key))
        return deepcopy(self.record)

    def decide_message(self, identifier, decision, argument_hash):
        self.events.append(("decide", identifier, decision, argument_hash))
        if self.record["status"] != "awaiting_approval":
            raise RuntimeError("already decided or completed")
        self.record["status"] = "approved" if decision == "approve" else "rejected"
        result = deepcopy(self.record)
        if self.corrupt_decision:
            self.corrupt_decision(result)
        return result

    def claim_message(self, identifier):
        self.events.append(("claim", identifier))
        if self.claim_error or self.record["status"] != "approved":
            raise RuntimeError("duplicate claim, changed contact, or uncertain claim response")
        self.record["status"] = "executing"
        result = deepcopy(self.record)
        if self.corrupt_claim:
            self.corrupt_claim(result)
        return result

    def complete_message(self, identifier, status, proof):
        self.events.append(("complete", identifier, status, deepcopy(proof)))
        if self.fail_result == "before":
            raise RuntimeError("private database error")
        self.record["status"], self.record["evidence"] = status, deepcopy(proof)
        if self.fail_result == "after":
            raise TimeoutError("response lost after journal persistence")
        result = deepcopy(self.record)
        if self.corrupt_result:
            self.corrupt_result(result)
        return result


class Mailer:
    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = {"status": "accepted", **evidence("provider-123")} if result is None else result
        self.error = error

    def send(self, recipient, subject, body, *, can_execute=lambda: True):
        self.calls.append((recipient, subject, body))
        if self.error:
            raise self.error
        return deepcopy(self.result)


def test_prepare_proposes_only_and_preserves_exact_unicode_payload_hash():
    hub, mailer = Journal(), Mailer()
    result = MessageCoordinator(hub, mailer).prepare("contact-1", "a" * 64, PAYLOAD["subject"], PAYLOAD["body"], "key-1")
    assert result["argument_hash"] == ARGUMENT_HASH and result["payload"] == PAYLOAD
    assert hub.events == [("propose", "contact-1", "a" * 64, PAYLOAD["subject"], PAYLOAD["body"], "key-1")]
    assert mailer.calls == []


@pytest.mark.parametrize("changes", [{"contact_id": "../bad"}, {"contact_version": "bad"},
                                     {"subject": "header\r\ninjection"}, {"body": " "},
                                     {"idempotency_key": "bad key"}])
def test_invalid_proposal_never_reaches_journal_or_mailer(changes):
    hub, mailer = Journal(), Mailer()
    arguments = {"contact_id": "contact-1", "contact_version": "a" * 64,
                 "subject": "Subject", "body": "Body", "idempotency_key": "key-1", **changes}
    with pytest.raises(MessageCoordinationError):
        MessageCoordinator(hub, mailer).prepare(**arguments)
    assert hub.events == [] and mailer.calls == []


def test_prepare_rejects_validly_hashed_receipt_for_different_requested_content():
    hub, mailer = Journal(), Mailer()
    hub.record["payload"]["body"] = "different content"
    hub.record["argument_hash"] = hashlib.sha256(json.dumps({"kind": "contact.email", "payload": hub.record["payload"]},
                                                          sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    with pytest.raises(MessageCoordinationError):
        MessageCoordinator(hub, mailer).prepare("contact-1", "a" * 64, PAYLOAD["subject"], PAYLOAD["body"], "key-1")
    assert mailer.calls == []


def test_rejection_never_claims_or_sends():
    hub, mailer = Journal(), Mailer()
    result = MessageCoordinator(hub, mailer).decide_and_execute("message-1", ARGUMENT_HASH, False)
    assert result["status"] == "rejected"
    assert hub.events == [("decide", "message-1", "reject", ARGUMENT_HASH)] and mailer.calls == []


@pytest.mark.parametrize("identifier,digest,approve", [("../bad", ARGUMENT_HASH, True),
                                                       ("message-1", "bad", True), ("message-1", ARGUMENT_HASH, 1)])
def test_invalid_approval_arguments_never_reach_journal(identifier, digest, approve):
    hub, mailer = Journal(), Mailer()
    with pytest.raises(MessageCoordinationError):
        MessageCoordinator(hub, mailer).decide_and_execute(identifier, digest, approve)
    assert hub.events == [] and mailer.calls == []


@pytest.mark.parametrize("stage", ["decision", "claim"])
@pytest.mark.parametrize("field,value", [("id", "different-message"), ("kind", "home.power"),
                                         ("argument_hash", "0" * 64), ("status", "accepted"),
                                         ("unexpected", "field"), ("created_at", True)])
def test_untrusted_decision_or_claim_receipt_cannot_send(stage, field, value):
    hub, mailer = Journal(), Mailer()
    setattr(hub, "corrupt_" + stage, lambda result: result.__setitem__(field, value))
    with pytest.raises(MessageCoordinationError):
        MessageCoordinator(hub, mailer).decide_and_execute("message-1", ARGUMENT_HASH, True)
    assert mailer.calls == []
    assert all(event[0] != "complete" for event in hub.events)


def test_claim_payload_changed_after_approval_cannot_send():
    hub, mailer = Journal(), Mailer()
    hub.corrupt_claim = lambda result: result["payload"].__setitem__("recipient", "someone-else@example.test")
    with pytest.raises(MessageCoordinationError):
        MessageCoordinator(hub, mailer).decide_and_execute("message-1", ARGUMENT_HASH, True)
    assert mailer.calls == []


def test_duplicate_or_uncertain_claim_never_sends():
    hub, mailer = Journal(), Mailer()
    hub.claim_error = True
    with pytest.raises(MessageCoordinationError):
        MessageCoordinator(hub, mailer).decide_and_execute("message-1", ARGUMENT_HASH, True)
    assert mailer.calls == [] and [event[0] for event in hub.events] == ["decide", "claim"]


def test_approved_claim_sends_exactly_once_and_records_acceptance_not_delivery():
    hub, mailer = Journal(), Mailer()
    coordinator = MessageCoordinator(hub, mailer)
    result = coordinator.decide_and_execute("message-1", ARGUMENT_HASH, True)
    assert result["status"] == "accepted" and result["evidence"] == evidence("provider-123")
    assert mailer.calls == [(PAYLOAD["recipient"], PAYLOAD["subject"], PAYLOAD["body"])]
    assert [event[0] for event in hub.events] == ["decide", "claim", "complete"]
    with pytest.raises(MessageCoordinationError):
        coordinator.decide_and_execute("message-1", ARGUMENT_HASH, True)
    assert len(mailer.calls) == 1


@pytest.mark.parametrize("status", ["failed", "unknown"])
def test_nonaccepted_adapter_outcome_is_preserved_and_never_retried(status):
    hub, mailer = Journal(), Mailer({"status": status, **evidence()})
    result = MessageCoordinator(hub, mailer).decide_and_execute("message-1", ARGUMENT_HASH, True)
    assert result["status"] == status and result["evidence"] == evidence()
    assert len(mailer.calls) == 1


def test_unexpected_adapter_exception_records_unknown_without_private_error_text():
    hub, mailer = Journal(), Mailer(error=RuntimeError("PRIVATE_TOKEN after send"))
    result = MessageCoordinator(hub, mailer).decide_and_execute("message-1", ARGUMENT_HASH, True)
    assert result["status"] == "unknown" and result["evidence"] == evidence()
    assert "PRIVATE_TOKEN" not in str(result) and len(mailer.calls) == 1


@pytest.mark.parametrize("change", [{"provider": "another"}, {"recipient": "other@example.test"},
                                    {"provider_message_id": None}, {"delivery_status": "delivered"},
                                    {"status": "delivered"}, {"unexpected": "private detail"}])
def test_malformed_adapter_receipt_cannot_claim_acceptance(change):
    result = {"status": "accepted", **evidence("provider-123"), **change}
    hub, mailer = Journal(), Mailer(result)
    final = MessageCoordinator(hub, mailer).decide_and_execute("message-1", ARGUMENT_HASH, True)
    assert final["status"] == "unknown" and final["evidence"] == evidence()
    assert len(mailer.calls) == 1


@pytest.mark.parametrize("failure", ["before", "after"])
def test_uncertain_result_persistence_raises_unknown_and_cannot_replay(failure):
    hub, mailer = Journal(), Mailer()
    hub.fail_result = failure
    coordinator = MessageCoordinator(hub, mailer)
    with pytest.raises(MessageOutcomeUnknown) as caught:
        coordinator.decide_and_execute("message-1", ARGUMENT_HASH, True)
    assert caught.value.status == "unknown" and caught.value.message_id == "message-1"
    assert caught.value.evidence == evidence("provider-123")
    assert "private database" not in str(caught.value)
    with pytest.raises(MessageCoordinationError):
        coordinator.decide_and_execute("message-1", ARGUMENT_HASH, True)
    assert len(mailer.calls) == 1


def test_mismatched_final_journal_evidence_is_unknown_even_after_provider_acceptance():
    hub, mailer = Journal(), Mailer()
    hub.corrupt_result = lambda result: result["evidence"].__setitem__("provider_message_id", "different-id")
    with pytest.raises(MessageOutcomeUnknown):
        MessageCoordinator(hub, mailer).decide_and_execute("message-1", ARGUMENT_HASH, True)
    assert len(mailer.calls) == 1


def test_public_review_validator_returns_independent_exact_validated_payload():
    from connectors.message_coordinator import validate_message
    original = row()
    reviewed = validate_message(original)
    assert reviewed["payload"] == PAYLOAD and reviewed["argument_hash"] == ARGUMENT_HASH
    original["payload"]["body"] = "changed after review"
    assert reviewed["payload"]["body"] == PAYLOAD["body"]


@pytest.mark.parametrize("change", [{"argument_hash": "0" * 64}, {"expires_at": True},
                                    {"expires_at": 1000}, {"expires_at": 2000},
                                    {"status": "delivered"}, {"unexpected": "field"}])
def test_public_review_validator_rejects_forged_hash_status_or_expiry(change):
    from connectors.message_coordinator import validate_message
    with pytest.raises(MessageCoordinationError):
        validate_message({**row(), **change})


def test_cancelled_trusted_gate_before_decision_has_no_journal_or_provider_effect():
    hub, mailer = Journal(), Mailer()
    with pytest.raises(MessageCoordinationError):
        MessageCoordinator(hub, mailer).decide_and_execute("message-1", ARGUMENT_HASH, True, can_execute=lambda: False)
    assert hub.events == [] and mailer.calls == []


def test_cancellation_during_decision_prevents_claim_and_send():
    hub, mailer = Journal(), Mailer()
    active = {"value": True}
    hub.corrupt_decision = lambda _: active.update(value=False)
    with pytest.raises(MessageCoordinationError):
        MessageCoordinator(hub, mailer).decide_and_execute("message-1", ARGUMENT_HASH, True, can_execute=lambda: active["value"])
    assert [event[0] for event in hub.events] == ["decide"]
    assert hub.record["status"] == "approved" and mailer.calls == []


def test_cancellation_during_claim_records_failed_without_provider_attempt():
    hub, mailer = Journal(), Mailer()
    active = {"value": True}
    hub.corrupt_claim = lambda _: active.update(value=False)
    final = MessageCoordinator(hub, mailer).decide_and_execute("message-1", ARGUMENT_HASH, True, can_execute=lambda: active["value"])
    assert final["status"] == "failed" and final["evidence"] == evidence()
    assert [event[0] for event in hub.events] == ["decide", "claim", "complete"] and mailer.calls == []


def test_cancellation_after_claim_with_failed_persistence_is_unknown_without_send():
    hub, mailer = Journal(), Mailer()
    active = {"value": True}
    hub.corrupt_claim = lambda _: active.update(value=False)
    hub.fail_result = "after"
    with pytest.raises(MessageOutcomeUnknown):
        MessageCoordinator(hub, mailer).decide_and_execute("message-1", ARGUMENT_HASH, True, can_execute=lambda: active["value"])
    assert mailer.calls == []


def test_trusted_gate_exception_is_safe_cancellation_before_any_effect():
    hub, mailer = Journal(), Mailer()
    def cancelled():
        raise RuntimeError("PRIVATE_IDENTITY_DETAILS")
    with pytest.raises(MessageCoordinationError) as caught:
        MessageCoordinator(hub, mailer).decide_and_execute("message-1", ARGUMENT_HASH, True, can_execute=cancelled)
    assert "PRIVATE_IDENTITY_DETAILS" not in str(caught.value)
    assert hub.events == [] and mailer.calls == []
