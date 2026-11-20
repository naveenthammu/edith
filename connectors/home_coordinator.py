"""Trusted coordination between EDITH's Rust action journal and Home Assistant.

``prepare`` may be used to produce an approval preview. ``decide_and_execute`` is
ONLY for a trusted dispatcher that has authenticated the approving person and shown
the exact prepared action. Do not register it as an LLM tool or treat the bool argument
as evidence of human consent. The journal authenticates credentials, not the speaker.

Home Assistant remains unconfigured until the installer supplies a connection and
explicit entity allowlist. This module neither discovers devices nor reads credentials.
"""
from copy import deepcopy
import hashlib
import hmac
import json
import re

from connectors.home_assistant import ENTITY


_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_STATUSES = {"awaiting_approval", "approved", "rejected", "executing", "verified", "failed", "unknown"}
_RECEIPT_FIELDS = {"id", "kind", "payload", "argument_hash", "summary", "created_at", "expires_at",
                   "status", "evidence", "recovery_note"}


class CoordinationError(RuntimeError):
    """The journal/approval could not be trusted; no additional device call is allowed."""


class OutcomeUnknown(CoordinationError):
    """A device call may have occurred, but its durable result was not confirmed."""

    def __init__(self, action_id: str, evidence: dict):
        super().__init__("Action outcome is unknown because the journal result could not be confirmed. "
                         "The device command was not retried. Reconcile this action before preparing another.")
        self.action_id = action_id
        self.status = "unknown"
        self.evidence = deepcopy(evidence)


def _payload(value) -> dict:
    if (not isinstance(value, dict) or set(value) != {"entity_id", "on"}
            or not isinstance(value["entity_id"], str) or not ENTITY.fullmatch(value["entity_id"])
            or type(value["on"]) is not bool):
        raise CoordinationError("Power actions require one exact allowed-domain entity and a boolean state.")
    return {"entity_id": value["entity_id"], "on": value["on"]}


def _argument_hash(payload: dict) -> str:
    # Matches Rust serde_json's sorted-map canonical encoding for this fixed ASCII schema.
    canonical = json.dumps({"kind": "home.power", "payload": payload}, sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _receipt(value, action_id: str | None, argument_hash: str, statuses: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) - _RECEIPT_FIELDS:
        raise CoordinationError("The action journal returned an invalid receipt.")
    identifier = value.get("id")
    if not isinstance(identifier, str) or not _IDENTIFIER.fullmatch(identifier) or (action_id is not None and identifier != action_id):
        raise CoordinationError("The action receipt does not match the requested action.")
    if value.get("kind") != "home.power" or not isinstance(value.get("status"), str) or value["status"] not in statuses:
        raise CoordinationError("The action receipt is not in the required state.")
    payload = _payload(value.get("payload"))
    digest = value.get("argument_hash")
    if (not isinstance(digest, str) or not _HASH.fullmatch(digest)
            or not hmac.compare_digest(digest, argument_hash)
            or not hmac.compare_digest(digest, _argument_hash(payload))):
        raise CoordinationError("The action receipt does not match the approved arguments.")
    return deepcopy(value)


def _unknown_evidence(payload: dict) -> dict:
    return {"entity_id": payload["entity_id"], "requested_state": "on" if payload["on"] else "off",
            "observed_state": "unknown", "command_status": "unknown"}


def _result(payload: dict, result) -> tuple[str, dict]:
    unknown = _unknown_evidence(payload)
    if (not isinstance(result, dict) or result.get("entity_id") != payload["entity_id"]
            or result.get("requested_state") != unknown["requested_state"]
            or not isinstance(result.get("status"), str) or result["status"] not in {"verified", "unknown"}
            or not isinstance(result.get("command_status"), str)
            or result["command_status"] not in {"accepted", "rejected", "unknown"}):
        return "unknown", unknown
    observed = result.get("observed_state")
    if observed is None:
        observed = "unknown"
    if not isinstance(observed, str) or observed not in {"on", "off", "unknown", "unavailable"}:
        return "unknown", unknown
    evidence = {**unknown, "observed_state": observed, "command_status": result["command_status"]}
    verified = (result["status"] == "verified" and result["command_status"] == "accepted"
                and observed == unknown["requested_state"])
    return "verified" if verified else "unknown", evidence


class HomeCoordinator:
    def __init__(self, hub, home):
        self.hub, self.home = hub, home

    def prepare(self, entity_id, on, idempotency_key):
        """Prepare a journaled action only; no approval, execution or device access occurs."""
        payload = _payload({"entity_id": entity_id, "on": on})
        if not isinstance(idempotency_key, str) or not _IDENTIFIER.fullmatch(idempotency_key):
            raise CoordinationError("Use one explicit idempotency key with 1 to 128 letters, digits, hyphens or underscores.")
        try:
            result = self.hub.propose_action("home.power", payload, idempotency_key)
        except Exception:
            raise CoordinationError("The action proposal could not be confirmed. No device command was issued.") from None
        return _receipt(result, None, _argument_hash(payload), _STATUSES)

    def decide_and_execute(self, action_id, argument_hash, approve):
        """Consume trusted exact-action consent, claim once, execute once, persist evidence.

        Caller authentication and approval presentation are required outside this method.
        Uncertain claim/result requests must be reconciled, never replayed by this method.
        """
        if (not isinstance(action_id, str) or not _IDENTIFIER.fullmatch(action_id)
                or not isinstance(argument_hash, str) or not _HASH.fullmatch(argument_hash)
                or type(approve) is not bool):
            raise CoordinationError("A valid action ID, exact argument hash and trusted boolean decision are required.")
        try:
            decision = self.hub.decide_action(action_id, "approve" if approve else "reject", argument_hash)
        except Exception:
            raise CoordinationError("The approval decision could not be confirmed. No device command was issued.") from None
        approved = _receipt(decision, action_id, argument_hash, {"approved" if approve else "rejected"})
        if not approve:
            return approved
        try:
            claim = self.hub.claim_action(action_id)
        except Exception:
            raise CoordinationError("The action could not be claimed once. No device command was issued; reconcile its journal state.") from None
        claimed = _receipt(claim, action_id, argument_hash, {"executing"})
        if claimed["payload"] != approved["payload"]:
            raise CoordinationError("Claimed arguments changed after approval. No device command was issued.")
        payload = claimed["payload"]
        try:
            adapter_result = self.home.set_power(payload["entity_id"], payload["on"])
        except Exception:
            # The exception may occur after the remote service acted. Never infer failure,
            # expose exception details, or perform a second Home Assistant request here.
            adapter_result = None
        status, evidence = _result(payload, adapter_result)
        try:
            result = self.hub.complete_action(action_id, status, evidence)
            final = _receipt(result, action_id, argument_hash, {status})
            if final.get("evidence") != evidence:
                raise CoordinationError("Recorded evidence differs from the submitted outcome.")
        except Exception:
            raise OutcomeUnknown(action_id, evidence) from None
        return final
