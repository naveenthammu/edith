"""Trusted approval bridge from the message journal to one Gmail send attempt.

prepare() creates an immutable preview only. decide_and_execute() is exclusively for
an authenticated approval handler which has shown/read the exact recipient and content.
Never register its approve boolean as an LLM tool or infer consent from generated text.
The journal authenticates its caller, not the person speaking near a microphone.
No account configuration or credential discovery occurs in this module.
"""
from copy import deepcopy
import hashlib
import hmac
import json
import re

from connectors.gmail import GmailError, valid_message_id, validate_content, validate_message as validate_email


_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_STATUSES = {"awaiting_approval", "approved", "rejected", "executing", "accepted", "failed", "unknown"}
_ROW_FIELDS = {"id", "kind", "payload", "argument_hash", "created_at", "expires_at", "status", "evidence", "recovery_note"}
_PAYLOAD_FIELDS = {"contact_id", "contact_version", "recipient", "subject", "body"}
_EVIDENCE_FIELDS = {"provider", "recipient", "provider_message_id", "delivery_status"}


class MessageCoordinationError(RuntimeError):
    """The journal/approval could not be trusted; no additional send is permitted."""


class MessageOutcomeUnknown(MessageCoordinationError):
    """A send may have occurred, but durable result confirmation failed; never replay."""

    def __init__(self, message_id, evidence):
        super().__init__("The message outcome is unknown because its journal result could not be confirmed. "
                         "No resend was attempted. Reconcile this message before preparing another.")
        self.message_id = message_id
        self.status = "unknown"
        self.evidence = deepcopy(evidence)


def _identifier(value):
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _hash(value):
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def _payload(value):
    if (not isinstance(value, dict) or set(value) != _PAYLOAD_FIELDS
            or not _identifier(value["contact_id"]) or not _hash(value["contact_version"])):
        raise MessageCoordinationError("The message requires an exact stored contact and immutable version.")
    try:
        validate_email(value["recipient"], value["subject"], value["body"])
    except GmailError:
        raise MessageCoordinationError("The message recipient or content is invalid.") from None
    return deepcopy(value)


def _argument_hash(payload):
    raw = json.dumps({"kind": "contact.email", "payload": payload}, sort_keys=True,
                     separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _evidence(value, payload, status):
    if (not isinstance(value, dict) or set(value) != _EVIDENCE_FIELDS
            or value["provider"] != "gmail" or value["recipient"] != payload["recipient"]
            or value["delivery_status"] != "unverified"):
        raise MessageCoordinationError("Message evidence does not match this exact recipient and provider.")
    identifier = value["provider_message_id"]
    if identifier is not None and not valid_message_id(identifier):
        raise MessageCoordinationError("The provider message identifier is invalid.")
    if status == "accepted" and identifier is None:
        raise MessageCoordinationError("Provider acceptance requires an actual message identifier.")
    if status == "failed" and identifier is not None:
        raise MessageCoordinationError("Rejected messages cannot claim a provider acceptance identifier.")
    return deepcopy(value)


def validate_message(action):
    """Validate an immutable preview/receipt; caller must authenticate the person and check current state."""
    if not isinstance(action, dict) or set(action) != _ROW_FIELDS or not _identifier(action["id"]):
        raise MessageCoordinationError("The message journal returned an invalid receipt.")
    if (action["kind"] != "contact.email" or not isinstance(action["status"], str)
            or action["status"] not in _STATUSES):
        raise MessageCoordinationError("The message journal returned an invalid kind or state.")
    payload = _payload(action["payload"])
    if not _hash(action["argument_hash"]) or not hmac.compare_digest(action["argument_hash"], _argument_hash(payload)):
        raise MessageCoordinationError("The message receipt does not match its immutable arguments.")
    if (type(action["created_at"]) is not int or type(action["expires_at"]) is not int
            or action["created_at"] < 0 or not 0 < action["expires_at"] - action["created_at"] <= 300):
        raise MessageCoordinationError("The message receipt has invalid approval expiry metadata.")
    if action["recovery_note"] is not None and (not isinstance(action["recovery_note"], str) or len(action["recovery_note"]) > 1000):
        raise MessageCoordinationError("The message recovery metadata is invalid.")
    status = action["status"]
    if action["evidence"] is not None:
        if status not in {"accepted", "failed", "unknown"}:
            raise MessageCoordinationError("An unexecuted message cannot contain send evidence.")
        _evidence(action["evidence"], payload, status)
    elif status == "accepted":
        raise MessageCoordinationError("An accepted message is missing provider evidence.")
    return deepcopy(action)


def _receipt(value, message_id=None, argument_hash=None, statuses=None):
    result = validate_message(value)
    if message_id is not None and result["id"] != message_id:
        raise MessageCoordinationError("The receipt belongs to a different message.")
    if argument_hash is not None and not hmac.compare_digest(result["argument_hash"], argument_hash):
        raise MessageCoordinationError("The message receipt differs from the approved arguments.")
    if statuses is not None and result["status"] not in statuses:
        raise MessageCoordinationError("The message is not in the required journal state.")
    return result


def _unknown_evidence(payload):
    return {"provider": "gmail", "recipient": payload["recipient"], "provider_message_id": None,
            "delivery_status": "unverified"}


def _adapter_result(payload, result):
    unknown = _unknown_evidence(payload)
    if (not isinstance(result, dict) or set(result) - (_EVIDENCE_FIELDS | {"status", "detail"})
            or not isinstance(result.get("status"), str) or result["status"] not in {"accepted", "failed", "unknown"}):
        return "unknown", unknown
    try:
        proof = _evidence({key: result[key] for key in _EVIDENCE_FIELDS}, payload, result["status"])
    except (KeyError, MessageCoordinationError):
        return "unknown", unknown
    return result["status"], proof


def _can_execute(gate):
    try:
        return gate() is True
    except Exception:
        return False


class MessageCoordinator:
    def __init__(self, hub, gmail):
        self.hub, self.gmail = hub, gmail

    def prepare(self, contact_id, contact_version, subject, body, idempotency_key):
        """Propose a preview for a stored contact only; no approval or send occurs."""
        if not _identifier(contact_id) or not _hash(contact_version) or not _identifier(idempotency_key):
            raise MessageCoordinationError("Use an exact stored contact, version hash and bounded idempotency key.")
        try:
            validate_content(subject, body)
        except GmailError:
            raise MessageCoordinationError("The message content is invalid.") from None
        try:
            result = self.hub.propose_message(contact_id, contact_version, subject, body, idempotency_key)
        except Exception:
            raise MessageCoordinationError("The proposal could not be confirmed. No send request was made.") from None
        prepared = _receipt(result)
        expected = {"contact_id": contact_id, "contact_version": contact_version, "subject": subject, "body": body}
        if any(prepared["payload"][key] != value for key, value in expected.items()):
            raise MessageCoordinationError("The proposal does not match the requested contact and content.")
        return prepared

    def decide_and_execute(self, message_id, argument_hash, approve, *, can_execute=lambda: True):
        """Trusted exact-preview approval only: decide, claim once, send once, persist evidence."""
        if not _identifier(message_id) or not _hash(argument_hash) or type(approve) is not bool:
            raise MessageCoordinationError("A valid message ID, exact approval hash and trusted boolean decision are required.")
        if not _can_execute(can_execute):
            raise MessageCoordinationError("Message operation was cancelled or is no longer authorized. No send was attempted.")
        try:
            decision = self.hub.decide_message(message_id, "approve" if approve else "reject", argument_hash)
        except Exception:
            raise MessageCoordinationError("The approval decision could not be confirmed. No send request was made.") from None
        approved = _receipt(decision, message_id, argument_hash, {"approved" if approve else "rejected"})
        if not approve:
            return approved
        if not _can_execute(can_execute):
            raise MessageCoordinationError("Message approval was interrupted. No claim or send was attempted.")
        try:
            claim = self.hub.claim_message(message_id)
        except Exception:
            raise MessageCoordinationError("The message could not be claimed once. No send was attempted; reconcile its journal state.") from None
        claimed = _receipt(claim, message_id, argument_hash, {"executing"})
        if claimed["payload"] != approved["payload"]:
            raise MessageCoordinationError("Claimed content changed after approval. No send request was made.")
        payload = claimed["payload"]
        if not _can_execute(can_execute):
            status, proof = "failed", _unknown_evidence(payload)  # Cancellation before any provider/credential call.
        else:
            try:
                result = self.gmail.send(payload["recipient"], payload["subject"], payload["body"], can_execute=can_execute)
            except Exception:
                result = None  # May have happened after transmission; never infer safe failure or resend.
            status, proof = _adapter_result(payload, result)
        try:
            saved = self.hub.complete_message(message_id, status, proof)
            final = _receipt(saved, message_id, argument_hash, {status})
            if final["evidence"] != proof:
                raise MessageCoordinationError("Persisted message evidence differs from the provider outcome.")
        except Exception:
            raise MessageOutcomeUnknown(message_id, proof) from None
        return final
