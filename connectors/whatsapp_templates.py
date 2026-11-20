"""Approved body-only positional WhatsApp templates and a default-deny sender.

No runtime registration, consent inference, account creation or send on import.
The trusted caller must own a durable claim and exact human approval. Revalidation
immediately before POST reduces stale-template risk; Meta does not offer an atomic
content-hash condition on send, so remote edits after the last read remain a limit.
https://developers.facebook.com/docs/graph-api/reference/whats-app-business-account/message_templates/
https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/overview
"""
from copy import deepcopy
import json
import re
from urllib.request import Request

from connectors.whatsapp import (GRAPH_API_VERSION, MAX_RESPONSE_BYTES, WhatsAppResponse, _http_transport,
                                 _permits, _receipt, _REJECTIONS, _validate_content)
from connectors.whatsapp_account import BusinessAccount, BusinessAccountError, _digest


_ID = re.compile(r"[1-9][0-9]{0,31}\Z")
_NAME = re.compile(r"[a-z0-9_]{1,512}\Z")
_LANGUAGE = re.compile(r"[a-z]{2,3}(?:_[A-Z]{2})?\Z")
_PARAM = re.compile(r"\{\{([1-9][0-9]?)\}\}")
_TEMPLATE_FIELDS = {"id", "name", "language", "status", "category", "parameter_format", "body", "content_hash"}


class TemplateError(RuntimeError):
    """Safe verification/preparation failure; never contains provider bodies or tokens."""


def _valid_text(value, limit, *, multiline=False):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        return False
    if any(ord(c) < 32 and not (multiline and c in "\r\n") or ord(c) == 127 for c in value):
        return False
    try:
        value.encode("utf-8")
        return True
    except UnicodeError:
        return False


def _template(row):
    if (not isinstance(row, dict) or not isinstance(row.get("id"), str) or not _ID.fullmatch(row["id"])
            or not isinstance(row.get("name"), str) or not _NAME.fullmatch(row["name"])
            or not isinstance(row.get("language"), str) or not _LANGUAGE.fullmatch(row["language"])
            or row.get("status") != "APPROVED" or row.get("category") not in ("UTILITY", "MARKETING")
            or row.get("parameter_format", "POSITIONAL") != "POSITIONAL"):
        raise TemplateError("Use an exact current approved utility or marketing positional template.")
    components = row.get("components")
    if (not isinstance(components, list) or len(components) != 1 or not isinstance(components[0], dict)
            or components[0].get("type") != "BODY" or set(components[0]) - {"type", "text", "example"}
            or not _valid_text(components[0].get("text"), 1024, multiline=True)):
        raise TemplateError("Only one bounded BODY text component is supported; headers, footers, buttons and media are unavailable.")
    body = components[0]["text"]
    remaining = _PARAM.sub("", body)
    indices = {int(match) for match in _PARAM.findall(body)}
    if "{{" in remaining or "}}" in remaining or indices != set(range(1, len(indices) + 1)) or len(indices) > 10:
        raise TemplateError("Use at most ten contiguous positional body parameters.")
    result = {"id": row["id"], "name": row["name"], "language": row["language"], "status": "APPROVED",
              "category": row["category"], "parameter_format": "POSITIONAL", "body": body}
    result["content_hash"] = _digest(result)
    return result


def _render(template, parameters):
    count = len(set(_PARAM.findall(template["body"])))
    if (not isinstance(parameters, list) or len(parameters) != count
            or any(not _valid_text(p, 1024) or "{{" in p or "}}" in p for p in parameters)):
        raise TemplateError("Supply exactly the bounded plain-text parameters required by this template.")
    rendered = _PARAM.sub(lambda match: parameters[int(match.group(1)) - 1], template["body"])
    if not _valid_text(rendered, 4096, multiline=True):
        raise TemplateError("The rendered template exceeds the supported message limit.")
    return rendered


def _prepared(value):
    if (not isinstance(value, dict) or set(value) != {"account", "template", "parameters", "rendered_text"}
            or not isinstance(value["account"], dict) or not isinstance(value["template"], dict)
            or set(value["template"]) != _TEMPLATE_FIELDS):
        raise TemplateError("An exact verified template snapshot is required.")
    template = value["template"]
    verified = _template({**template, "components": [{"type": "BODY", "text": template["body"]}]})
    if (template != verified or value["rendered_text"] != _render(verified, value["parameters"])):
        raise TemplateError("Template content or rendered approval text changed.")
    return deepcopy(value)


class TemplateCatalog:
    def __init__(self, account):
        if not isinstance(account, BusinessAccount):
            raise TemplateError("Configure a trusted verified business account reader.")
        self.account = account

    def get(self, template_id, name, language):
        """Verify current sender/template metadata without requiring personal parameter values."""
        if (not isinstance(template_id, str) or not _ID.fullmatch(template_id)
                or not isinstance(name, str) or not _NAME.fullmatch(name)
                or not isinstance(language, str) or not _LANGUAGE.fullmatch(language)):
            raise TemplateError("Select an exact numeric template ID, name and language.")
        try:
            account, rows = self.account.template_rows(name)
            matches = [row for row in rows if row.get("id") == template_id]
            if len(matches) != 1 or matches[0].get("name") != name or matches[0].get("language") != language:
                raise TemplateError("The selected template is not uniquely verified under this business account.")
            template = _template(matches[0])
            return {"account": account, "template": template}
        except BusinessAccountError:
            raise TemplateError("Business account and template verification could not be completed. No send was attempted.") from None

    def prepare(self, template_id, name, language, parameters):
        verified = self.get(template_id, name, language)
        return {**verified, "parameters": deepcopy(parameters), "rendered_text": _render(verified["template"], parameters)}

    def revalidate(self, prepared):
        original = _prepared(prepared)
        t = original["template"]
        current = self.prepare(t["id"], t["name"], t["language"], original["parameters"])
        before, after = deepcopy(original), deepcopy(current)
        before["account"].pop("verified_at", None)
        after["account"].pop("verified_at", None)
        if before != after:
            raise TemplateError("The verified sender or template changed; prepare and review a new proposal.")
        return current


class TemplateSender:
    def __init__(self, catalog, *, recipient_policy=None, transport=None, timeout=5):
        if (not isinstance(catalog, TemplateCatalog) or (recipient_policy is not None and not callable(recipient_policy))
                or (transport is not None and not callable(transport)) or type(timeout) not in (int, float) or not 0 < timeout <= 60):
            raise TemplateError("Configure a trusted template catalog, recipient policy and bounded transport.")
        self.catalog, self._policy, self._timeout = catalog, recipient_policy, timeout
        self._transport = _http_transport if transport is None else transport

    def _authorized(self, binding, gate):
        return _permits(gate) and _permits(self._policy, deepcopy(binding)) and _permits(gate)

    def send_template(self, recipient_e164, prepared, *, can_execute=None):
        """Trusted claimed-action path only: fresh verification, at most one POST.

        recipient_policy receives {recipient_e164, prepared}; it must consult current
        trusted opt-in evidence for this sender/contact. An approved template does
        not require a service window but never substitutes for contact permission.
        """
        evidence = {"provider": "whatsapp_business", "recipient_e164": recipient_e164,
                    "provider_message_id": None, "delivery_status": "unverified"}
        failed = {**evidence, "status": "failed", "detail": "The exact account, template, recipient permission or approval could not be verified. No send was attempted."}
        try:
            _validate_content(recipient_e164, "Validation")
            snapshot = _prepared(prepared)
            binding = {"recipient_e164": recipient_e164, "prepared": snapshot}
            if not self._authorized(binding, can_execute):
                return failed
            current = self.catalog.revalidate(snapshot)
            if not _permits(can_execute):
                return failed
            token = self.catalog.account.authorized_token(current["account"])
            template = snapshot["template"]
            payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": recipient_e164,
                       "type": "template", "template": {"name": template["name"], "language": {"code": template["language"]}}}
            if snapshot["parameters"]:
                payload["template"]["components"] = [{"type": "body", "parameters": [{"type": "text", "text": p} for p in snapshot["parameters"]]}]
            account = current["account"]
            evidence.update(phone_number_id=account["phone_number_id"], waba_id=account["waba_id"], account_version=account["version"],
                            template_content_hash=template["content_hash"])
            request = Request(f"https://graph.facebook.com/{GRAPH_API_VERSION}/{account['phone_number_id']}/messages",
                              method="POST", data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                              headers={"Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "application/json"})
            if not self._authorized(binding, can_execute):
                return failed
        except Exception:
            return failed
        unknown = {**evidence, "status": "unknown", "detail": "Message acceptance is unknown. No retry was attempted; reconcile before sending again."}
        try:
            response = self._transport(request, timeout=self._timeout, max_response_bytes=MAX_RESPONSE_BYTES)
            if not isinstance(response, WhatsAppResponse) or type(response.status_code) is not int:
                return unknown
            if response.status_code in _REJECTIONS:
                return {**evidence, "status": "failed", "detail": "The provider rejected the template request. No retry was attempted."}
            if response.status_code != 200 or not isinstance(response.body, bytes) or len(response.body) > MAX_RESPONSE_BYTES:
                return unknown
            receipt = _receipt(response.body, recipient_e164, token)
            if receipt is None:
                return unknown
            identifier, status = receipt
            if status != "accepted":
                return {**unknown, "provider_message_id": identifier}
            return {**evidence, "status": "accepted", "provider_message_id": identifier,
                    "detail": "The provider accepted the template request. Delivery remains unverified."}
        except Exception:
            return unknown
