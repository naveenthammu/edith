"""Review and explicitly record owner-attested evidence; never infer consent.

Evidence holds expected_version, status, allowed_categories, expires_at,
evidence_reference, and optional source_note. The selected file must be private.
Default is review only; --confirm-record records the operator's explicit assertion.
This does not independently prove the evidence or grant blanket send permission.
"""
from copy import deepcopy
import json
import re
import sys
import time

from connectors.whatsapp_connection import ConnectionError, read_private_json
from tools.configure_whatsapp_connection import _Parser, load_connection


_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_PHONE = re.compile(r"\+[1-9][0-9]{1,14}\Z")
_FIELDS = {"expected_version", "status", "allowed_categories", "expires_at", "evidence_reference"}


def _text(value, minimum, maximum):
    return (isinstance(value, str) and minimum <= len(value) <= maximum
            and (minimum == 0 or bool(value.strip())) and all(ord(c) >= 32 and ord(c) != 127 for c in value)
            and not any(0xd800 <= ord(c) <= 0xdfff for c in value))


def _evidence(value, now):
    if (not isinstance(value, dict) or not _FIELDS <= set(value) or set(value) - _FIELDS - {"source_note"}
            or value["expected_version"] is not None and (not isinstance(value["expected_version"], str) or not _HASH.fullmatch(value["expected_version"]))
            or value["status"] not in ("granted", "revoked") or type(value["expires_at"]) is not int
            or not 0 <= value["expires_at"] <= now + 366 * 86400
            or not isinstance(value["allowed_categories"], list) or len(value["allowed_categories"]) > 2
            or any(item not in ("UTILITY", "MARKETING") for item in value["allowed_categories"])
            or len(set(value["allowed_categories"])) != len(value["allowed_categories"])
            or value["status"] == "granted" and (not value["allowed_categories"] or value["expires_at"] <= now)
            or not _text(value["evidence_reference"], 1, 200) or not _text(value.get("source_note", ""), 0, 400)):
        raise ConnectionError("Invalid explicit evidence selection.")
    return {**deepcopy(value), "source_note": value.get("source_note", ""),
            "allowed_categories": [category for category in ("UTILITY", "MARKETING") if category in value["allowed_categories"]]}


def _contact(hub, identifier, version):
    if not isinstance(identifier, str) or not _ID.fullmatch(identifier) or not isinstance(version, str) or not _HASH.fullmatch(version):
        raise ConnectionError("Exact contact identifier and version are required.")
    rows = hub.contacts()
    if not isinstance(rows, dict) or set(rows) != {"contacts"} or not isinstance(rows["contacts"], list) or len(rows["contacts"]) > 1000:
        raise ConnectionError("Invalid current contact collection.")
    selected = [row for row in rows["contacts"] if isinstance(row, dict) and row.get("id") == identifier]
    if (len(selected) != 1 or selected[0].get("version") != version or not isinstance(selected[0].get("phone_e164"), str)
            or not _PHONE.fullmatch(selected[0]["phone_e164"])):
        raise ConnectionError("The selected contact or phone changed.")
    return {"id": identifier, "version": version, "recipient_e164": selected[0]["phone_e164"]}


def _receipt(result, payload, recipient):
    expected = {key: value for key, value in payload.items() if key != "expected_version"}
    fields = set(expected) | {"id", "version", "recipient_e164", "updated_at"}
    if (not isinstance(result, dict) or set(result) != fields or any(result[key] != value for key, value in expected.items())
            or not isinstance(result["id"], str) or not _ID.fullmatch(result["id"])
            or not isinstance(result["version"], str) or not _HASH.fullmatch(result["version"])
            or result["version"] == payload["expected_version"] or result["recipient_e164"] != recipient
            or type(result["updated_at"]) is not int or result["updated_at"] < 0
            or type(result["recorded_at"]) is not int or type(result["expires_at"]) is not int):
        raise ConnectionError("The permission receipt did not match the explicit record.")
    return result


def main(argv=None, *, connection_loader=None, clock=None):
    parser = _Parser(description="Review or record selected consent evidence for one exact contact; no Meta request or message.", allow_abbrev=False)
    parser.add_argument("--connection-config", required=True)
    parser.add_argument("--paired-client-config", required=True)
    parser.add_argument("--contact-id", required=True)
    parser.add_argument("--contact-version", required=True)
    parser.add_argument("--evidence-file", required=True, help="Absolute protected JSON file; never evidence text or tokens on argv")
    parser.add_argument("--confirm-record", action="store_true", help="Explicit operator assertion to write this reviewed evidence; otherwise review only")
    args = parser.parse_args(argv)
    attempted = False
    try:
        get_time = time.time if clock is None else clock
        connection, hub = (load_connection if connection_loader is None else connection_loader)(args.connection_config, args.paired_client_config)
        account = connection.revalidate()
        selected = read_private_json(args.evidence_file)
        evidence = _evidence(selected, int(get_time()))
        contact = _contact(hub, args.contact_id, args.contact_version)
        review = {"principal_id": connection.principal_id, "connection_id": connection.connection_id,
                  "business_sender": account["display_phone_number"], "contact_id": contact["id"], "contact_version": contact["version"],
                  "recipient_e164": contact["recipient_e164"], "permission_status": evidence["status"],
                  "allowed_categories": evidence["allowed_categories"], "expires_at": evidence["expires_at"],
                  "source": "explicit_operator_record"}
        print(json.dumps({"status": "review_required", "review": review}, ensure_ascii=False))
        if not args.confirm_record: return 0
        if connection.revalidate() != account: raise ConnectionError("Connection changed during review.")
        if _contact(hub, args.contact_id, args.contact_version) != contact: raise ConnectionError("Contact changed during review.")
        if read_private_json(args.evidence_file) != selected: raise ConnectionError("Selected evidence changed during review.")
        now = int(get_time())
        evidence = _evidence(selected, now)
        payload = {"connection_id": connection.connection_id, "account_version": account["version"],
                   "contact_id": contact["id"], "contact_version": contact["version"], **evidence,
                   "recorded_at": now, "source": "explicit_operator_record"}
        attempted = True
        result = _receipt(connection.worker.set_permission(payload), payload, contact["recipient_e164"])
        connection.revalidate()
        print(json.dumps({"status": "permission_recorded", "permission_id": result["id"], "permission_version": result["version"],
                          "connection_id": connection.connection_id, "contact_id": contact["id"], "permission_status": result["status"]}))
        return 0
    except Exception:
        detail = "The record may already exist; inspect current permissions before retrying." if attempted else "No permission write was attempted."
        print("Permission recording could not be confirmed. " + detail + " No message was sent.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
