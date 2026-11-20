import copy
import hashlib
import importlib
import importlib.util
import json

import pytest


MESSAGE_ID = "11111111-1111-4111-8111-111111111111"
BINDING_ID = "22222222-2222-4222-8222-222222222222"
CONTACT_ID = "33333333-3333-4333-8333-333333333333"
PERMISSION_ID = "44444444-4444-4444-8444-444444444444"


def api():
    assert importlib.util.find_spec("connectors.whatsapp_business_coordinator") is not None, "Business coordinator is missing"
    return importlib.import_module("connectors.whatsapp_business_coordinator")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def snapshot():
    return {"connection_id": "business", "principal_id": "owner", "waba_id": "23456", "phone_number_id": "12345",
            "display_phone_number": "+16505551234", "verified_name": "Example Shop", "version": "a" * 64, "verified_at": 100}


def template():
    value = {"id": "34567", "name": "order_update", "language": "en_US", "status": "APPROVED", "category": "UTILITY",
             "parameter_format": "POSITIONAL", "body": "Hello {{1}}, order {{2}} is ready."}
    return {**value, "content_hash": digest(value)}


def binding(account=None, selected=None):
    account, selected = account or snapshot(), selected or template()
    return {"id": BINDING_ID, "version": digest({"account": {k: v for k, v in account.items() if k != "verified_at"}, "template": selected}),
            "account": copy.deepcopy(account), "template": copy.deepcopy(selected), "updated_at": 100}


def permission():
    return {"id": PERMISSION_ID, "version": "b" * 64, "connection_id": "business", "account_version": "a" * 64,
            "contact_id": CONTACT_ID, "contact_version": "c" * 64, "recipient_e164": "+16505550000", "status": "granted",
            "allowed_categories": ["UTILITY"], "recorded_at": 100, "expires_at": 10000,
            "source": "explicit_operator_record", "evidence_reference": "local-record-1", "source_note": "Private consent note", "updated_at": 100}


def action():
    payload = {"binding_id": BINDING_ID, "binding_version": binding()["version"], "contact_id": CONTACT_ID,
               "contact_version": "c" * 64, "recipient_name": "Asha", "recipient_e164": "+16505550000", "permission_version": "b" * 64,
               "prepared": {"account": snapshot(), "template": template(), "parameters": ["Asha", "42"],
                            "rendered_text": "Hello Asha, order 42 is ready."}}
    return {"id": MESSAGE_ID, "kind": "contact.whatsapp_template", "payload": payload,
            "argument_hash": digest({"kind": "contact.whatsapp_template", "payload": payload}),
            "created_at": 100, "expires_at": 400, "status": "awaiting_approval", "evidence": None, "recovery_note": None}


def evidence(row=None, message_id=None):
    payload = (row or action())["payload"]
    prepared = payload["prepared"]
    return {"provider": "whatsapp_business", "phone_number_id": prepared["account"]["phone_number_id"],
            "waba_id": prepared["account"]["waba_id"], "account_version": prepared["account"]["version"],
            "template_content_hash": prepared["template"]["content_hash"], "recipient_e164": payload["recipient_e164"],
            "provider_message_id": message_id, "delivery_status": "unverified"}


class Hub:
    def __init__(self):
        self.row, self.record, self.bound = action(), permission(), binding()
        self.identity = {"principal_id": "owner", "role": "adult", "capabilities": {"business_message_proposals": True}}
        self.contact = {"id": CONTACT_ID, "version": "c" * 64, "display_name": "Asha", "phone_e164": "+16505550000"}
        self.events, self.hooks = [], {}

    def event(self, name):
        self.events.append(name)
        if name in self.hooks:
            self.hooks[name]()

    def capabilities(self):
        self.event("paired-capabilities")
        return copy.deepcopy(self.identity)

    def business_message(self, identifier):
        self.event("paired-read")
        assert identifier == MESSAGE_ID
        return copy.deepcopy(self.row)

    def contacts(self):
        self.event("contacts")
        return {"contacts": [copy.deepcopy(self.contact)]}

    def decide_business_message(self, identifier, decision, argument_hash):
        self.event("decision")
        assert identifier == MESSAGE_ID and argument_hash == self.row["argument_hash"]
        self.row["status"] = "approved" if decision == "approve" else "rejected"
        return copy.deepcopy(self.row)


class Worker:
    def __init__(self, hub):
        self.hub = hub

    def register_binding(self, account, selected):
        self.hub.event("sync")
        self.hub.bound = binding(account, selected)
        self.hub.bound["updated_at"] = 200
        return copy.deepcopy(self.hub.bound)

    def permissions(self):
        self.hub.event("permissions")
        return {"permissions": [copy.deepcopy(self.hub.record)]}

    def claim_message(self, identifier, argument_hash):
        self.hub.event("claim")
        assert identifier == MESSAGE_ID and argument_hash == self.hub.row["argument_hash"]
        assert self.hub.row["status"] == "approved"
        self.hub.row["status"] = "executing"
        return copy.deepcopy(self.hub.row)

    def authorize_message(self, identifier, argument_hash):
        self.hub.event("authorize")
        assert identifier == MESSAGE_ID and argument_hash == self.hub.row["argument_hash"]
        if self.hub.record["status"] != "granted" or self.hub.row["status"] != "executing":
            raise RuntimeError("current permission denied")
        return {"authorized": True, "message": copy.deepcopy(self.hub.row)}

    def complete_message(self, identifier, status, proof):
        self.hub.event("result")
        self.hub.row.update(status=status, evidence=copy.deepcopy(proof))
        return copy.deepcopy(self.hub.row)


class Connection:
    principal_id, connection_id = "owner", "business"
    def __init__(self, hub):
        self.hub, self.worker, self.catalog = hub, Worker(hub), self
        self.account_snapshot, self.template_snapshot = snapshot(), template()
        self.result = {**evidence(message_id="wamid.synthetic"), "status": "accepted", "detail": "provider acceptance only"}

    def revalidate(self):
        self.hub.event("connection")
        return copy.deepcopy(self.account_snapshot)

    def get(self, template_id, name, language):
        self.hub.event("provider-read")
        assert (template_id, name, language) == ("34567", "order_update", "en_US")
        return {"account": {**self.account_snapshot, "verified_at": 200}, "template": copy.deepcopy(self.template_snapshot)}

    def template_sender(self, policy):
        connection = self
        class Sender:
            def send_template(self, recipient, prepared, *, can_execute):
                assert recipient == "+16505550000" and prepared == connection.hub.row["payload"]["prepared"]
                assert can_execute() is True
                connection.hub.event("sender-verification")
                if policy({"recipient_e164": recipient, "prepared": copy.deepcopy(prepared)}) is not True or can_execute() is not True:
                    return {"provider": "whatsapp_business", "recipient_e164": recipient, "provider_message_id": None,
                            "delivery_status": "unverified", "status": "failed"}
                connection.hub.event("post")
                if isinstance(connection.result, Exception):
                    raise connection.result
                return copy.deepcopy(connection.result)
        return Sender()


def setup():
    hub = Hub()
    connection = Connection(hub)
    obj = api().BusinessMessageCoordinator(hub, connection, clock=lambda: 200)
    return hub, connection, obj


def execute(obj, approve=True, gate=lambda: True):
    return obj.decide_and_execute(MESSAGE_ID, action()["argument_hash"], approve, can_execute=gate)


def test_fresh_review_syncs_metadata_without_changing_immutable_action_or_claiming():
    hub, _, obj = setup()
    reviewed = obj.review(MESSAGE_ID, can_execute=lambda: True)
    assert reviewed == action() and hub.bound["account"]["verified_at"] == 200
    assert hub.bound["version"] == binding()["version"]
    assert "provider-read" in hub.events and "sync" in hub.events
    assert not {"decision", "claim", "post", "result"} & set(hub.events)


def test_exact_approved_claim_sends_once_then_persists_bound_acceptance():
    hub, _, obj = setup()
    final = execute(obj)
    assert final["status"] == "accepted" and final["evidence"] == evidence(message_id="wamid.synthetic")
    assert hub.events.count("post") == hub.events.count("claim") == hub.events.count("result") == 1
    assert hub.events.index("sender-verification") < hub.events.index("authorize") < hub.events.index("post")
    with pytest.raises(api().BusinessCoordinationError): execute(obj)
    assert hub.events.count("post") == 1


def test_reject_is_paired_only_without_worker_provider_or_credentials():
    hub, _, obj = setup()
    final = execute(obj, False)
    assert final["status"] == "rejected"
    assert not {"connection", "provider-read", "sync", "permissions", "claim", "post", "result"} & set(hub.events)


def test_approved_unclaimed_message_requires_fresh_review_but_not_second_decision():
    hub, _, obj = setup()
    hub.row["status"] = "approved"
    assert execute(obj)["status"] == "accepted"
    assert "decision" not in hub.events and "provider-read" in hub.events and hub.events.count("claim") == 1


@pytest.mark.parametrize("change", ["permission", "category", "expired", "contact", "sender", "template", "role", "principal", "capability"])
def test_stale_or_unauthorized_binding_cannot_reach_claim(change):
    hub, connection, obj = setup()
    if change == "permission": hub.record["status"] = "revoked"
    elif change == "category": hub.record["allowed_categories"] = ["MARKETING"]
    elif change == "expired": hub.record["expires_at"] = 200
    elif change == "contact": hub.contact["version"] = "d" * 64
    elif change == "sender": connection.account_snapshot["version"] = "d" * 64
    elif change == "template":
        connection.template_snapshot["body"] = "Changed {{1}} {{2}}"
        connection.template_snapshot["content_hash"] = digest({k: v for k, v in connection.template_snapshot.items() if k != "content_hash"})
    elif change == "role": hub.identity["role"] = "guest"
    elif change == "principal": hub.identity["principal_id"] = "other"
    else: hub.identity["capabilities"]["business_message_proposals"] = False
    with pytest.raises(api().BusinessCoordinationError): execute(obj)
    assert not {"claim", "post"} & set(hub.events)


def test_revocation_during_sender_provider_reads_is_rechecked_atomically_before_post():
    hub, _, obj = setup()
    hub.hooks["sender-verification"] = lambda: hub.record.update(status="revoked")
    final = execute(obj)
    assert final["status"] == "failed" and final["evidence"] == evidence()
    assert "authorize" in hub.events and "post" not in hub.events and hub.events.count("result") == 1


def test_current_permission_authorization_must_echo_same_executing_action():
    hub, connection, obj = setup()
    def forged(*_):
        return {"authorized": True, "message": {**hub.row, "id": BINDING_ID}}
    connection.worker.authorize_message = forged
    assert execute(obj)["status"] == "failed" and "post" not in hub.events


def test_historical_receipt_is_saved_even_if_session_is_cancelled_after_post():
    hub, _, obj = setup()
    current = [True]
    hub.hooks["post"] = lambda: current.__setitem__(0, False)
    final = execute(obj, gate=lambda: current[0])
    assert final["status"] == "accepted" and hub.events.count("result") == 1


@pytest.mark.parametrize("status,message_id", [("unknown", None), ("unknown", "wamid.held"), ("failed", None)])
def test_adapter_outcomes_are_recorded_without_delivery_or_retry_claims(status, message_id):
    hub, connection, obj = setup()
    connection.result = {**evidence(message_id=message_id), "status": status}
    result = execute(obj)
    assert result["status"] == status and result["evidence"]["provider_message_id"] == message_id
    assert result["evidence"]["delivery_status"] == "unverified" and hub.events.count("post") == 1


@pytest.mark.parametrize("change", ["recipient", "account", "hash", "missing_id", "extra_field"])
def test_substituted_or_incomplete_acceptance_is_recorded_unknown(change):
    hub, connection, obj = setup()
    if change == "recipient": connection.result["recipient_e164"] = "+16505559999"
    elif change == "account": connection.result["account_version"] = "d" * 64
    elif change == "hash": connection.result["template_content_hash"] = "d" * 64
    elif change == "missing_id": connection.result["provider_message_id"] = None
    else: connection.result["raw_provider_body"] = "PRIVATE_BODY"
    final = execute(obj)
    assert final["status"] == "unknown" and final["evidence"] == evidence()
    assert "PRIVATE_BODY" not in repr(final)


def test_timeout_or_result_receipt_loss_never_retries_or_leaks_details():
    hub, connection, obj = setup()
    connection.result = TimeoutError("PRIVATE_PROVIDER_BODY")
    assert execute(obj)["status"] == "unknown"
    assert hub.events.count("post") == 1
    hub, connection, obj = setup()
    def lost(*_):
        hub.event("result")
        raise TimeoutError("PRIVATE_TOKEN")
    connection.worker.complete_message = lost
    with pytest.raises(api().BusinessOutcomeUnknown) as caught:
        execute(obj)
    assert "PRIVATE" not in str(caught.value) and hub.events.count("result") == 1 and hub.events.count("post") == 1


def test_unknown_claim_never_calls_sender_or_reclaims():
    hub, connection, obj = setup()
    def lost(*_):
        hub.event("claim")
        raise TimeoutError("private claim response")
    connection.worker.claim_message = lost
    with pytest.raises(api().BusinessCoordinationError): execute(obj)
    assert hub.events.count("claim") == 1 and "post" not in hub.events and "result" not in hub.events


def test_missing_gate_defaults_to_deny_before_provider_or_mutation():
    hub, _, obj = setup()
    with pytest.raises(api().BusinessCoordinationError): obj.review(MESSAGE_ID)
    with pytest.raises(api().BusinessCoordinationError): obj.decide_and_execute(MESSAGE_ID, action()["argument_hash"], True)
    assert hub.events == []


@pytest.mark.parametrize("kind", ["message", "binding", "permission"])
def test_public_validators_reject_unknown_fields_and_canonical_hash_tampering(kind):
    mod = api()
    value, validate = {"message": (action(), mod.validate_business_message), "binding": (binding(), mod.validate_binding),
                       "permission": (permission(), mod.validate_permission)}[kind]
    assert validate(value) == value
    value["access_token"] = "PRIVATE_TOKEN"
    with pytest.raises(mod.BusinessCoordinationError) as caught:
        validate(value)
    assert "PRIVATE_TOKEN" not in str(caught.value)


def test_rendered_text_and_template_hash_are_independently_checked_from_argument_hash():
    value = action()
    value["payload"]["prepared"]["rendered_text"] = "Injected message"
    value["argument_hash"] = digest({"kind": value["kind"], "payload": value["payload"]})
    with pytest.raises(api().BusinessCoordinationError): api().validate_business_message(value)


def test_permission_validator_accepts_rust_category_order_and_historical_revocation():
    value = permission()
    value["allowed_categories"] = ["UTILITY", "MARKETING"]
    assert api().validate_permission(value) == value
    value.update(status="revoked", allowed_categories=[], expires_at=0)
    assert api().validate_permission(value) == value


def test_permission_recorded_time_does_not_define_its_server_expiry_limit():
    value = permission()
    value.update(recorded_at=100, updated_at=200, expires_at=200 + 366 * 86400)
    assert api().validate_permission(value) == value


@pytest.mark.parametrize("revoke_during_final_read", [False, True])
def test_real_template_sender_reauthorizes_after_provider_reads_before_single_post(revoke_during_final_read):
    from urllib.parse import urlsplit
    from connectors.whatsapp import WhatsAppResponse
    from connectors.whatsapp_account import BusinessAccount
    from connectors.whatsapp_templates import TemplateCatalog, TemplateSender

    hub, connection, obj = setup()
    requests = []

    def provider(request, **_kwargs):
        method, path = request.get_method(), urlsplit(request.full_url).path
        requests.append((method, path))
        hub.event("provider-" + method)
        if method == "POST":
            return WhatsAppResponse(200, json.dumps({"messaging_product": "whatsapp", "contacts": [
                {"input": "+16505550000", "wa_id": "16505550000"}], "messages": [{"id": "wamid.real-adapter"}]}).encode())
        if path.endswith("/phone_numbers"):
            rows = [{"id": "12345", "display_phone_number": "+16505551234", "verified_name": "Example Shop"}]
        else:
            if revoke_during_final_read and hub.row["status"] == "executing":
                hub.record["status"] = "revoked"
            selected = template()
            rows = [{k: v for k, v in selected.items() if k not in ("body", "content_hash")}]
            rows[0]["components"] = [{"type": "BODY", "text": selected["body"]}]
        return WhatsAppResponse(200, json.dumps({"data": rows}).encode())

    account = BusinessAccount("owner", "23456", "12345", lambda: "synthetic-private-token", connection_id="business",
                              transport=provider, clock=lambda: 200)
    catalog = TemplateCatalog(account)
    prepared = catalog.prepare("34567", "order_update", "en_US", ["Asha", "42"])
    connection.account_snapshot = copy.deepcopy(prepared["account"])
    connection.catalog = catalog
    connection.template_sender = lambda policy: TemplateSender(catalog, recipient_policy=policy, transport=provider)
    hub.bound = binding(prepared["account"], prepared["template"])
    hub.record["account_version"] = prepared["account"]["version"]
    hub.row["payload"].update(prepared=prepared, binding_version=hub.bound["version"])
    hub.row["argument_hash"] = digest({"kind": hub.row["kind"], "payload": hub.row["payload"]})
    requests.clear()
    hub.events.clear()
    final = obj.decide_and_execute(MESSAGE_ID, hub.row["argument_hash"], True, can_execute=lambda: True)

    assert hub.events.count("claim") == hub.events.count("result") == 1
    assert hub.events.count("authorize") == 2
    last_read = max(i for i, event in enumerate(hub.events) if event == "provider-GET")
    final_auth = max(i for i, event in enumerate(hub.events) if event == "authorize")
    assert last_read < final_auth
    posts = [path for method, path in requests if method == "POST"]
    if revoke_during_final_read:
        assert final["status"] == "failed" and posts == []
    else:
        assert final["status"] == "accepted" and posts == ["/v26.0/12345/messages"]
        assert final["evidence"]["provider_message_id"] == "wamid.real-adapter"
        assert final_auth < hub.events.index("provider-POST")
