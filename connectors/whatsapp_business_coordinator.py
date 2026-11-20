"""Trusted bridge between paired approvals and a separately scoped WhatsApp worker.

Only explicit trusted review/confirmation handlers may execute. Models may propose
through the paired journal but cannot sync verified metadata, grant recipient
permission, claim or complete sends. The connection owns protected credentials;
this module never reads or serializes them. No account/provider IO occurs on import
or construction. Unknown external outcomes are never replayed.
"""
from copy import deepcopy
import hashlib
import hmac
import json
import math
import re
import time

from connectors.whatsapp_templates import _prepared, _template


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_LOCAL = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_NUMBER = re.compile(r"[1-9][0-9]{0,31}\Z")
_PHONE = re.compile(r"\+[1-9][0-9]{1,14}\Z")
_ACCOUNT = {"connection_id", "principal_id", "waba_id", "phone_number_id", "display_phone_number", "verified_name", "version", "verified_at"}
_TEMPLATE = {"id", "name", "language", "status", "category", "parameter_format", "body", "content_hash"}
_BINDING = {"id", "version", "account", "template", "updated_at"}
_PERMISSION = {"id", "version", "connection_id", "account_version", "contact_id", "contact_version", "recipient_e164", "status",
               "allowed_categories", "recorded_at", "expires_at", "source", "evidence_reference", "source_note", "updated_at"}
_PAYLOAD = {"binding_id", "binding_version", "contact_id", "contact_version", "recipient_name", "recipient_e164", "permission_version", "prepared"}
_ROW = {"id", "kind", "payload", "argument_hash", "created_at", "expires_at", "status", "evidence", "recovery_note"}
_EVIDENCE = {"provider", "phone_number_id", "waba_id", "account_version", "template_content_hash", "recipient_e164", "provider_message_id", "delivery_status"}
_STATUSES = {"awaiting_approval", "approved", "rejected", "executing", "accepted", "failed", "unknown"}


class BusinessCoordinationError(RuntimeError):
    """Safe validation/authorization failure without message contents or provider errors."""


class BusinessOutcomeUnknown(BusinessCoordinationError):
    status = "unknown"
    def __init__(self, message_id, evidence):
        self.message_id, self.evidence = message_id, deepcopy(evidence)
        super().__init__("The business message outcome could not be durably confirmed. No resend was attempted; reconcile before another attempt.")


def _matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _integer(value):
    return type(value) is int and 0 <= value <= 9_223_372_036_854_775_807


def _text(value, limit, *, empty=False):
    if (not isinstance(value, str) or len(value) > limit or (not empty and not value.strip())
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        return False
    value.encode("utf-8")
    return True


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def _account(value):
    if (not isinstance(value, dict) or set(value) != _ACCOUNT or not _matches(_LOCAL, value["principal_id"])
            or not _matches(_LOCAL, value["connection_id"]) or not _matches(_NUMBER, value["waba_id"])
            or not _matches(_NUMBER, value["phone_number_id"]) or not _matches(_PHONE, value["display_phone_number"])
            or not _text(value["verified_name"], 256) or not _matches(_HASH, value["version"]) or not _integer(value["verified_at"])):
        raise ValueError("account")
    return deepcopy(value)


def _same_account(first, second):
    return {k: v for k, v in _account(first).items() if k != "verified_at"} == {k: v for k, v in _account(second).items() if k != "verified_at"}


def _verified_template(value):
    if not isinstance(value, dict) or set(value) != _TEMPLATE:
        raise ValueError("template")
    expected = _template({**value, "components": [{"type": "BODY", "text": value["body"]}]})
    if value != expected:
        raise ValueError("template hash")
    return deepcopy(value)


def _binding_version(account, template):
    return _digest({"account": {k: v for k, v in account.items() if k != "verified_at"}, "template": template})


def validate_binding(value):
    """Validate safe worker-verified metadata; this is not recipient permission."""
    try:
        if (not isinstance(value, dict) or set(value) != _BINDING or not _matches(_UUID, value["id"])
                or not _matches(_HASH, value["version"]) or not _integer(value["updated_at"])):
            raise ValueError("binding")
        account, template = _account(value["account"]), _verified_template(value["template"])
        if not hmac.compare_digest(value["version"], _binding_version(account, template)):
            raise ValueError("binding hash")
        return deepcopy(value)
    except Exception:
        raise BusinessCoordinationError("The verified business binding is malformed or its immutable version does not match.") from None


def validate_permission(value):
    """Validate an operator-recorded permission row. Never expose evidence notes to models."""
    try:
        if (not isinstance(value, dict) or set(value) != _PERMISSION or not _matches(_UUID, value["id"])
                or not _matches(_HASH, value["version"]) or not _matches(_LOCAL, value["connection_id"])
                or not _matches(_HASH, value["account_version"]) or not _matches(_UUID, value["contact_id"])
                or not _matches(_HASH, value["contact_version"]) or not _matches(_PHONE, value["recipient_e164"])
                or value["status"] not in ("granted", "revoked") or value["source"] != "explicit_operator_record"
                or not _text(value["evidence_reference"], 200) or not _text(value["source_note"], 400, empty=True)
                or any(not _integer(value[k]) for k in ("recorded_at", "expires_at", "updated_at"))):
            raise ValueError("permission")
        categories = value["allowed_categories"]
        if (not isinstance(categories, list) or any(v not in ("UTILITY", "MARKETING") for v in categories)
                or categories != [v for v in ("UTILITY", "MARKETING") if v in categories]
                or (value["status"] == "granted" and not categories)):
            raise ValueError("categories")
        return deepcopy(value)
    except Exception:
        raise BusinessCoordinationError("The recipient permission record is malformed; contact presence is not opt-in.") from None


def _base_evidence(payload):
    prepared = payload["prepared"]
    return {"provider": "whatsapp_business", "phone_number_id": prepared["account"]["phone_number_id"],
            "waba_id": prepared["account"]["waba_id"], "account_version": prepared["account"]["version"],
            "template_content_hash": prepared["template"]["content_hash"], "recipient_e164": payload["recipient_e164"],
            "provider_message_id": None, "delivery_status": "unverified"}


def _valid_evidence(value, payload, status):
    if not isinstance(value, dict) or set(value) != _EVIDENCE:
        return False
    expected = _base_evidence(payload)
    if any(value[key] != expected[key] for key in _EVIDENCE - {"provider_message_id"}):
        return False
    identifier = value["provider_message_id"]
    if identifier is not None and (not isinstance(identifier, str) or not 1 <= len(identifier) <= 512
                                   or not all(33 <= ord(c) <= 126 for c in identifier)):
        return False
    return not (status == "accepted" and identifier is None or status == "failed" and identifier is not None)


def validate_business_message(value):
    """Validate the exact public journal row without trusting its displayed hash alone."""
    try:
        if (not isinstance(value, dict) or set(value) != _ROW or not _matches(_UUID, value["id"])
                or value["kind"] != "contact.whatsapp_template" or value["status"] not in _STATUSES
                or not _matches(_HASH, value["argument_hash"]) or not _integer(value["created_at"])
                or not _integer(value["expires_at"]) or value["expires_at"] != value["created_at"] + 300
                or (value["recovery_note"] is not None and not _text(value["recovery_note"], 500))):
            raise ValueError("row")
        p = value["payload"]
        if (not isinstance(p, dict) or set(p) != _PAYLOAD or not _matches(_UUID, p["binding_id"])
                or not _matches(_HASH, p["binding_version"]) or not _matches(_UUID, p["contact_id"])
                or not _matches(_HASH, p["contact_version"]) or not _text(p["recipient_name"], 100)
                or not _matches(_PHONE, p["recipient_e164"]) or not _matches(_HASH, p["permission_version"])):
            raise ValueError("payload")
        prepared = _prepared(p["prepared"])
        account, template = _account(prepared["account"]), _verified_template(prepared["template"])
        if p["binding_version"] != _binding_version(account, template):
            raise ValueError("binding mismatch")
        if not hmac.compare_digest(value["argument_hash"], _digest({"kind": value["kind"], "payload": p})):
            raise ValueError("argument hash")
        status, proof = value["status"], value["evidence"]
        if status in ("accepted", "failed") or status == "unknown" and proof is not None:
            if not _valid_evidence(proof, p, status):
                raise ValueError("evidence")
        elif proof is not None:
            raise ValueError("premature evidence")
        return deepcopy(value)
    except Exception:
        raise BusinessCoordinationError("The business message receipt does not match its exact verified sender, recipient and template content.") from None


def _allowed(gate):
    try:
        return callable(gate) and gate() is True
    except Exception:
        return False


class BusinessMessageCoordinator:
    def __init__(self, paired_hub, connection, *, clock=time.time):
        if (not _matches(_LOCAL, connection.principal_id) or not _matches(_LOCAL, connection.connection_id)
                or not callable(clock)):
            raise BusinessCoordinationError("Configure a trusted paired owner and separate business worker connection.")
        self.hub, self.connection, self.clock = paired_hub, connection, clock
        self.worker = connection.worker

    def _now(self):
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            raise BusinessCoordinationError("The business message clock is unavailable.")
        return now

    def _paired(self, gate):
        if not _allowed(gate):
            raise BusinessCoordinationError("Business message authorization was cancelled.")
        try:
            identity = self.hub.capabilities()
            if (identity.get("principal_id") != self.connection.principal_id or identity.get("role") not in ("adult", "elder")
                    or identity.get("capabilities", {}).get("business_message_proposals") is not True or not _allowed(gate)):
                raise ValueError("identity")
        except Exception:
            raise BusinessCoordinationError("The paired business-message owner or authority changed.") from None

    def _current(self, gate, payload=None):
        self._paired(gate)
        try:
            account = _account(self.connection.revalidate())
            self._paired(gate)
            if (account["principal_id"] != self.connection.principal_id or account["connection_id"] != self.connection.connection_id
                    or (payload is not None and not _same_account(account, payload["prepared"]["account"]))):
                raise ValueError("connection")
            return account
        except Exception:
            raise BusinessCoordinationError("The protected business account or scoped worker configuration changed.") from None

    def _row(self, value, identifier=None, argument_hash=None, statuses=None):
        row = validate_business_message(value)
        account = row["payload"]["prepared"]["account"]
        if ((identifier is not None and row["id"] != identifier) or (argument_hash is not None and row["argument_hash"] != argument_hash)
                or (statuses is not None and row["status"] not in statuses) or account["principal_id"] != self.connection.principal_id
                or account["connection_id"] != self.connection.connection_id):
            raise BusinessCoordinationError("The message belongs to a different action, owner, connection or execution state.")
        return row

    def _fresh(self, row):
        now = self._now()
        if row["expires_at"] <= now or row["created_at"] > now + 30:
            raise BusinessCoordinationError("The exact message proposal expired; prepare and review a new one.")

    def _permission(self, row, gate):
        p = row["payload"]
        self._current(gate, p)
        contacts = self.hub.contacts()
        self._current(gate, p)
        if not isinstance(contacts, dict) or set(contacts) != {"contacts"} or not isinstance(contacts["contacts"], list) or len(contacts["contacts"]) > 1000:
            raise BusinessCoordinationError("The current contact could not be verified.")
        matches = [c for c in contacts["contacts"] if isinstance(c, dict) and c.get("id") == p["contact_id"]]
        if (len(matches) != 1 or matches[0].get("version") != p["contact_version"]
                or matches[0].get("phone_e164") != p["recipient_e164"] or matches[0].get("display_name") != p["recipient_name"]):
            raise BusinessCoordinationError("The recipient contact changed; prepare a new message.")
        result = self.worker.permissions()
        self._current(gate, p)
        if not isinstance(result, dict) or set(result) != {"permissions"} or not isinstance(result["permissions"], list) or len(result["permissions"]) > 100:
            raise BusinessCoordinationError("Current recipient permission could not be verified.")
        records = [validate_permission(item) for item in result["permissions"]]
        records = [r for r in records if r["connection_id"] == self.connection.connection_id and r["contact_id"] == p["contact_id"]]
        now = self._now()
        if len(records) != 1:
            raise BusinessCoordinationError("An unambiguous explicit recipient permission record is required.")
        record = records[0]
        if (record["version"] != p["permission_version"] or record["status"] != "granted" or record["expires_at"] <= now
                or record["recorded_at"] > now + 30 or record["account_version"] != p["prepared"]["account"]["version"]
                or record["contact_version"] != p["contact_version"] or record["recipient_e164"] != p["recipient_e164"]
                or p["prepared"]["template"]["category"] not in record["allowed_categories"]):
            raise BusinessCoordinationError("The exact recipient's recorded permission is missing, changed, expired or outside this template category.")

    def review(self, identifier, *, can_execute=lambda: False):
        """Refresh verified metadata and validate current policy; never approve, claim or send."""
        try:
            self._current(can_execute)
            if not _matches(_UUID, identifier):
                raise ValueError("id")
            row = self._row(self.hub.business_message(identifier), identifier, statuses={"awaiting_approval", "approved"})
            self._current(can_execute, row["payload"])
            self._fresh(row)
            p = row["payload"]
            t = p["prepared"]["template"]
            fresh = self.connection.catalog.get(t["id"], t["name"], t["language"])
            self._current(can_execute, p)
            if not isinstance(fresh, dict) or set(fresh) != {"account", "template"}:
                raise ValueError("metadata")
            fresh_account, fresh_template = _account(fresh["account"]), _verified_template(fresh["template"])
            if (fresh_account["principal_id"] != self.connection.principal_id or fresh_account["connection_id"] != self.connection.connection_id):
                raise ValueError("provider binding")
            bound = validate_binding(self.worker.register_binding(fresh_account, fresh_template))
            self._current(can_execute, p)
            if (bound["id"] != p["binding_id"] or bound["version"] != p["binding_version"]
                    or bound["account"] != fresh_account or bound["template"] != fresh_template
                    or not _same_account(bound["account"], p["prepared"]["account"]) or bound["template"] != t
                    or not -30 <= self._now() - bound["account"]["verified_at"] < 300):
                raise ValueError("stale binding")
            self._permission(row, can_execute)
            latest = self._row(self.hub.business_message(identifier), identifier, row["argument_hash"], {"awaiting_approval", "approved"})
            self._current(can_execute, p)
            self._fresh(latest)
            if latest["payload"] != row["payload"]:
                raise ValueError("changed payload")
            return latest
        except Exception:
            raise BusinessCoordinationError("The business message's current sender, template, contact or recorded permission could not be verified. No send was attempted.") from None

    def _adapter_outcome(self, result, payload):
        empty = _base_evidence(payload)
        if (not isinstance(result, dict) or set(result) - (_EVIDENCE | {"status", "detail"})
                or result.get("status") not in ("accepted", "failed", "unknown")):
            return "unknown", empty
        status = result["status"]
        if status == "failed" and result.get("provider_message_id") is None:
            # A denied pre-send adapter result can omit sender fields. Never use its untrusted fields.
            return "failed", empty
        proof = {key: result.get(key) for key in _EVIDENCE}
        if not _valid_evidence(proof, payload, status):
            return "unknown", empty
        return status, proof

    def decide_and_execute(self, identifier, argument_hash, approve, *, can_execute=lambda: False):
        """Exact human-confirmed path: paired decision, one worker claim/send/result."""
        if not _matches(_UUID, identifier) or not _matches(_HASH, argument_hash) or type(approve) is not bool:
            raise BusinessCoordinationError("Use an exact message ID, approval hash and trusted boolean decision.")
        self._paired(can_execute)
        if not approve:
            try:
                row = self._row(self.hub.business_message(identifier), identifier, argument_hash, {"awaiting_approval", "approved"})
                self._paired(can_execute)
                return self._row(self.hub.decide_business_message(identifier, "reject", argument_hash), identifier, argument_hash, {"rejected"})
            except Exception:
                raise BusinessCoordinationError("Message rejection could not be confirmed. No send was attempted.") from None
        row = self.review(identifier, can_execute=can_execute)
        if row["argument_hash"] != argument_hash:
            raise BusinessCoordinationError("The reviewed message hash changed; review it again.")
        self._current(can_execute, row["payload"])
        if row["status"] == "awaiting_approval":
            try:
                row = self._row(self.hub.decide_business_message(identifier, "approve", argument_hash), identifier, argument_hash, {"approved"})
            except Exception:
                raise BusinessCoordinationError("Approval could not be confirmed. No claim or send was attempted.") from None
        self._current(can_execute, row["payload"])
        self._fresh(row)
        try:
            claimed = self._row(self.worker.claim_message(identifier, argument_hash), identifier, argument_hash, {"executing"})
            if claimed["payload"] != row["payload"]:
                raise ValueError("claim mismatch")
        except Exception:
            raise BusinessCoordinationError("The worker claim could not be confirmed once. No send was attempted; reconcile the journal before another attempt.") from None
        payload = claimed["payload"]

        def user_gate():
            try:
                self._current(can_execute, payload)
                self._fresh(claimed)
                return True
            except Exception:
                return False

        def recipient_policy(binding):
            try:
                if binding != {"recipient_e164": payload["recipient_e164"], "prepared": payload["prepared"]} or not user_gate():
                    return False
                authorization = self.worker.authorize_message(identifier, argument_hash)
                if not isinstance(authorization, dict) or set(authorization) != {"authorized", "message"} or authorization["authorized"] is not True:
                    return False
                current = self._row(authorization["message"], identifier, argument_hash, {"executing"})
                return current["payload"] == payload and user_gate()
            except Exception:
                return False

        if not user_gate():
            status, proof = "failed", _base_evidence(payload)
        else:
            try:
                sender = self.connection.template_sender(recipient_policy)
                result = sender.send_template(payload["recipient_e164"], deepcopy(payload["prepared"]), can_execute=user_gate)
            except Exception:
                result = None
            status, proof = self._adapter_outcome(result, payload)
        # Historical receipt recording is required even after cancellation, expiry or consent revocation.
        try:
            saved = self._row(self.worker.complete_message(identifier, status, proof), identifier, argument_hash, {status})
            if saved["payload"] != payload or saved["evidence"] != proof:
                raise ValueError("result mismatch")
            return saved
        except Exception:
            raise BusinessOutcomeUnknown(identifier, proof) from None
