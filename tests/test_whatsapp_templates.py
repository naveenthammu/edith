import copy
import importlib
import importlib.util
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from connectors.whatsapp import WhatsAppResponse


TOKEN = "synthetic-private-template-token"
PHONE = {"id": "12345", "display_phone_number": "+16505551234", "verified_name": "Example Shop"}
TEMPLATE = {"id": "34567", "name": "order_update", "language": "en_US", "status": "APPROVED", "category": "UTILITY",
            "parameter_format": "POSITIONAL", "components": [{"type": "BODY", "text": "Hello {{1}}, order {{2}} is ready."}]}
RECIPIENT = "+16505550000"


def api():
    assert importlib.util.find_spec("connectors.whatsapp_templates") is not None, "Template verifier is missing"
    return importlib.import_module("connectors.whatsapp_templates")


class Provider:
    def __init__(self):
        self.phone, self.template = copy.deepcopy(PHONE), copy.deepcopy(TEMPLATE)
        self.requests = []
        self.response = WhatsAppResponse(200, json.dumps({"messaging_product": "whatsapp", "contacts": [{"input": RECIPIENT, "wa_id": RECIPIENT[1:]}], "messages": [{"id": "wamid.example"}]}).encode())

    def __call__(self, request, **_kwargs):
        self.requests.append(request)
        if request.get_method() == "POST":
            if isinstance(self.response, Exception):
                raise self.response
            return self.response
        rows = [self.phone] if urlsplit(request.full_url).path.endswith("/phone_numbers") else [self.template]
        return WhatsAppResponse(200, json.dumps({"data": rows}).encode())


def setup(provider=None, policy=None):
    mod = api()
    account_mod = importlib.import_module("connectors.whatsapp_account")
    provider = provider or Provider()
    account = account_mod.BusinessAccount("alice", "23456", "12345", lambda: TOKEN, connection_id="one",
                                          transport=provider, clock=lambda: 100)
    catalog = mod.TemplateCatalog(account)
    sender = mod.TemplateSender(catalog, recipient_policy=policy, transport=provider)
    return provider, catalog, sender


def prepare(catalog):
    return catalog.prepare("34567", "order_update", "en_US", ["Asha", "42"])


def test_template_snapshot_proves_account_membership_and_exact_rendering():
    provider, catalog, _ = setup()
    result = prepare(catalog)
    assert result["rendered_text"] == "Hello Asha, order 42 is ready."
    assert result["parameters"] == ["Asha", "42"]
    assert result["template"]["body"] == TEMPLATE["components"][0]["text"]
    assert result["template"]["status"] == "APPROVED"
    assert len(result["template"]["content_hash"]) == 64
    assert result["account"]["waba_id"] == "23456" and TOKEN not in repr(result)
    urls = [urlsplit(r.full_url) for r in provider.requests]
    assert urls[-1].path == "/v26.0/23456/message_templates"
    assert parse_qs(urls[-1].query)["name"] == ["order_update"]


def test_catalog_get_verifies_metadata_without_inventing_message_parameters():
    _, catalog, _ = setup()
    assert hasattr(catalog, "get"), "Read-only template metadata operation is missing"
    result = catalog.get("34567", "order_update", "en_US")
    assert set(result) == {"account", "template"}
    assert result["template"]["body"] == "Hello {{1}}, order {{2}} is ready."
    assert result["account"]["waba_id"] == "23456"


@pytest.mark.parametrize("change", [
    {"status": "PAUSED"}, {"status": "PENDING"}, {"id": "11111"}, {"language": "hi"}, {"name": "other"},
    {"parameter_format": "NAMED"}, {"category": "AUTHENTICATION"},
    {"components": [{"type": "BODY", "text": "Hello {{1}}"}, {"type": "FOOTER", "text": "Extra content"}]},
    {"components": [{"type": "HEADER", "text": "Hello"}]},
    {"components": [{"type": "BODY", "text": "Hi {{name}}"}]},
    {"components": [{"type": "BODY", "text": "Hi {{2}}"}]},
    {"components": [{"type": "BODY", "text": "Hi {{1}}", "buttons": []}]},
])
def test_unsupported_or_unapproved_provider_template_fails_closed(change):
    provider, catalog, _ = setup()
    provider.template.update(change)
    with pytest.raises(api().TemplateError):
        prepare(catalog)


@pytest.mark.parametrize("parameters", [[], ["Asha"], ["Asha", "42", "extra"], [1, "42"], ["", "42"],
                                        ["bad\nvalue", "42"], ["{{2}}", "42"], ["x" * 1025, "42"]])
def test_parameters_are_exact_bounded_text_not_nested_template_instructions(parameters):
    _, catalog, _ = setup()
    with pytest.raises(api().TemplateError):
        catalog.prepare("34567", "order_update", "en_US", parameters)


def test_missing_policies_deny_without_new_reads_or_send():
    provider, catalog, sender = setup()
    prepared = prepare(catalog)
    provider.requests.clear()
    assert sender.send_template(RECIPIENT, prepared, can_execute=lambda: True)["status"] == "failed"
    assert provider.requests == []


def test_exact_fresh_template_send_is_one_post_and_acceptance_only():
    bindings = []
    provider, catalog, sender = setup(policy=lambda binding: bindings.append(copy.deepcopy(binding)) or True)
    prepared = prepare(catalog)
    result = sender.send_template(RECIPIENT, prepared, can_execute=lambda: True)
    posts = [r for r in provider.requests if r.get_method() == "POST"]
    assert len(posts) == 1 and result["status"] == "accepted" and result["delivery_status"] == "unverified"
    assert result["template_content_hash"] == prepared["template"]["content_hash"]
    assert result["account_version"] == prepared["account"]["version"]
    assert json.loads(posts[0].data) == {"messaging_product": "whatsapp", "recipient_type": "individual", "to": RECIPIENT,
        "type": "template", "template": {"name": "order_update", "language": {"code": "en_US"},
        "components": [{"type": "body", "parameters": [{"type": "text", "text": "Asha"}, {"type": "text", "text": "42"}]}]}}
    assert len(bindings) >= 2 and bindings[-1]["recipient_e164"] == RECIPIENT
    assert bindings[-1]["prepared"] == prepared
    assert TOKEN not in repr(result) and "Hello Asha" not in repr(result)


@pytest.mark.parametrize("change", ["body", "paused", "sender", "parameters", "readback", "hash"])
def test_changed_account_template_or_reviewed_payload_prevents_send(change):
    provider, catalog, sender = setup(policy=lambda _: True)
    prepared = prepare(catalog)
    if change == "body":
        provider.template["components"][0]["text"] = "Changed {{1}} and {{2}}"
    elif change == "paused":
        provider.template["status"] = "PAUSED"
    elif change == "sender":
        provider.phone["display_phone_number"] = "+16505558888"
    elif change == "parameters":
        prepared["parameters"][0] = "Changed"
    elif change == "readback":
        prepared["rendered_text"] = "Not the actual template"
    else:
        prepared["template"]["content_hash"] = "0" * 64
    result = sender.send_template(RECIPIENT, prepared, can_execute=lambda: True)
    assert result["status"] == "failed"
    assert not any(r.get_method() == "POST" for r in provider.requests)


def test_policy_revocation_during_fresh_read_prevents_post():
    permitted = [True]
    provider, catalog, sender = setup(policy=lambda _: permitted[0])
    prepared = prepare(catalog)
    original = catalog.account._transport
    def revoke(request, **kwargs):
        permitted[0] = False
        return original(request, **kwargs)
    catalog.account._transport = revoke
    result = sender.send_template(RECIPIENT, prepared, can_execute=lambda: True)
    assert result["status"] == "failed" and not any(r.get_method() == "POST" for r in provider.requests)


def test_timeout_after_post_is_unknown_and_never_retried():
    provider, catalog, sender = setup(policy=lambda _: True)
    prepared = prepare(catalog)
    provider.response = TimeoutError(TOKEN)
    result = sender.send_template(RECIPIENT, prepared, can_execute=lambda: True)
    assert result["status"] == "unknown" and TOKEN not in repr(result)
    assert len([r for r in provider.requests if r.get_method() == "POST"]) == 1


def test_no_parameter_template_omits_empty_components():
    provider, catalog, sender = setup(policy=lambda _: True)
    provider.template["components"][0]["text"] = "Your order is ready."
    prepared = catalog.prepare("34567", "order_update", "en_US", [])
    result = sender.send_template(RECIPIENT, prepared, can_execute=lambda: True)
    assert result["status"] == "accepted"
    assert "components" not in json.loads(provider.requests[-1].data)["template"]


@pytest.mark.parametrize("decision", [1, "yes", {}, None, False])
def test_nonboolean_recipient_permission_never_sends(decision):
    provider, catalog, sender = setup(policy=lambda _: decision)
    prepared = prepare(catalog)
    provider.requests.clear()
    assert sender.send_template(RECIPIENT, prepared, can_execute=lambda: True)["status"] == "failed"
    assert provider.requests == []


def test_final_blocking_policy_cancellation_stops_post():
    live, calls = [True], []
    def policy(_):
        calls.append(1)
        if len(calls) == 2:
            live[0] = False
        return True
    provider, catalog, sender = setup(policy=policy)
    result = sender.send_template(RECIPIENT, prepare(catalog), can_execute=lambda: live[0])
    assert result["status"] == "failed" and not any(r.get_method() == "POST" for r in provider.requests)


def test_unapproved_or_substituted_receipt_never_claims_delivery():
    provider, catalog, sender = setup(policy=lambda _: True)
    prepared = prepare(catalog)
    payload = json.loads(provider.response.body)
    payload["contacts"][0]["input"] = "+16505559999"
    provider.response = WhatsAppResponse(200, json.dumps(payload).encode())
    assert sender.send_template(RECIPIENT, prepared, can_execute=lambda: True)["status"] == "unknown"

