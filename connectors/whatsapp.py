"""Standalone, default-deny WhatsApp Business Cloud API text adapter.

No runtime registration, account loading, consent store, webhook receiver, journal,
templates, or delivery verification is implemented here. A trusted caller must bind
the configured business sender to the authenticated principal and obtain approval
of the exact recipient and content before calling send_text. The constructor's
recipient_policy(phone_number_id, recipient_e164) must consult current trusted
opt-in and active 24-hour service-window evidence; contact existence and model
claims are insufficient. It is rechecked after credential acquisition. can_execute
must check that the exact-message approval remains valid. Both default to deny.

This narrow configuration supports a token associated with one messaging account
for the configured phone number. Multi-account attribution and approved templates
require separate account configuration and approval work; there is no fallback.

Meta documents POST /{version}/{phone-number-id}/messages and text <=4096 chars:
https://developers.facebook.com/documentation/business-messaging/whatsapp/messages/text-messages
Consent and service window:
https://developers.facebook.com/documentation/business-messaging/whatsapp/messages/send-messages
v26.0 is a supported, deliberately pinned version (not a claim to be the latest):
https://developers.facebook.com/docs/graph-api/changelog/versions/
"""
from dataclasses import dataclass
import json
import math
import re
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


GRAPH_API_VERSION = "v26.0"
MAX_RESPONSE_BYTES = 65_536
_PHONE_ID = re.compile(r"[1-9][0-9]{0,31}\Z")
_E164 = re.compile(r"\+[1-9][0-9]{1,14}\Z")
_REJECTIONS = frozenset({400, 401, 403, 404, 429})


class WhatsAppError(ValueError):
    """Safe local validation error without account or provider response details."""


@dataclass(frozen=True)
class WhatsAppResponse:
    status_code: int
    body: bytes


def _validate_content(recipient, text):
    # Syntactic E.164 validation is not proof of registration, ownership or consent.
    if not isinstance(recipient, str) or not _E164.fullmatch(recipient):
        raise WhatsAppError("Use one exact E.164 recipient beginning with + and its country code.")
    if (not isinstance(text, str) or not text.strip() or len(text) > 4096
            or any((ord(char) < 32 and char not in "\r\n\t") or ord(char) == 127 for char in text)):
        raise WhatsAppError("Use nonempty text of at most 4,096 characters without binary controls.")
    try:
        text.encode("utf-8")
    except UnicodeError:
        raise WhatsAppError("Message content must be valid Unicode text.") from None


def _permits(callback, *args):
    try:
        return callable(callback) and callback(*args) is True
    except Exception:
        return False


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_transport(request, *, timeout, max_response_bytes):
    # An empty ProxyHandler disables environment and Windows ambient proxy lookup.
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        response = opener.open(request, timeout=timeout)
    except HTTPError as error:
        status = error.code
        error.close()  # Never read or expose provider error bodies.
        return WhatsAppResponse(status, b"")
    with response:
        if response.status != 200:
            return WhatsAppResponse(response.status, b"")
        length = response.headers.get("Content-Length")
        if length is not None and (not length.isascii() or not length.isdigit() or int(length) > max_response_bytes):
            return WhatsAppResponse(200, b"")
        return WhatsAppResponse(200, response.read(max_response_bytes + 1))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate response field")
        result[key] = value
    return result


def _receipt(body, recipient, token):
    data = json.loads(body, object_pairs_hook=_unique_object)
    if not isinstance(data, dict) or data.get("messaging_product") != "whatsapp" or "error" in data:
        return None
    contacts, messages = data.get("contacts"), data.get("messages")
    if not isinstance(contacts, list) or len(contacts) != 1 or not isinstance(messages, list) or len(messages) != 1:
        return None
    contact, message = contacts[0], messages[0]
    if (not isinstance(contact, dict) or not isinstance(message, dict)
            or contact.get("input") not in (recipient, recipient[1:]) or contact.get("wa_id") != recipient[1:]):
        return None
    identifier = message.get("id")
    if (not isinstance(identifier, str) or not 1 <= len(identifier) <= 512
            or not all(33 <= ord(char) <= 126 for char in identifier) or token in identifier):
        return None
    status = message.get("message_status", "accepted")
    if status not in {"accepted", "held_for_quality_assessment", "paused"}:
        return None
    return identifier, status


class WhatsAppBusiness:
    def __init__(self, phone_number_id, credential_provider, *, recipient_policy=None, timeout=5, transport=None):
        if not isinstance(phone_number_id, str) or not _PHONE_ID.fullmatch(phone_number_id):
            raise WhatsAppError("Configure a numeric business phone-number identifier.")
        if (not callable(credential_provider) or (recipient_policy is not None and not callable(recipient_policy))
                or (transport is not None and not callable(transport))):
            raise WhatsAppError("Configure trusted credential and recipient-policy callbacks and an optional test transport.")
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise WhatsAppError("Timeout must be greater than zero and at most 60 seconds.")
        self._phone_number_id = phone_number_id
        self._endpoint = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{phone_number_id}/messages"
        self._credential_provider = credential_provider
        self._recipient_policy = recipient_policy
        self._timeout = timeout
        self._transport = _http_transport if transport is None else transport

    def _authorized(self, recipient, gate):
        # The policy may block; recheck cancellation after it returns.
        return (_permits(gate) and _permits(self._recipient_policy, self._phone_number_id, recipient)
                and _permits(gate))

    def send_text(self, recipient_e164, text, *, can_execute=None):
        """At most one POST per call; accepted is a provider receipt, never delivery.

        Trusted handler only. This has no journal/idempotency store, so callers must
        claim a durable approved action once and never replay an uncertain attempt.
        The optional transport is a test boundary, not a model-configurable endpoint.
        """
        _validate_content(recipient_e164, text)
        evidence = {"provider": "whatsapp_business", "phone_number_id": self._phone_number_id,
                    "recipient_e164": recipient_e164, "provider_message_id": None, "delivery_status": "unverified"}
        denied = {**evidence, "status": "failed",
                  "detail": "Current approval, recipient consent or service-window eligibility was not confirmed. No send request was made."}
        if not self._authorized(recipient_e164, can_execute):
            return denied
        try:
            payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": recipient_e164,
                       "type": "text", "text": {"body": text, "preview_url": False}}
            token = self._credential_provider()  # Once only; never refresh in response to a POST outcome.
            # Opaque bearer, not decoded. These are bounded header-safety limits.
            if (not isinstance(token, str) or not 1 <= len(token) <= 8192
                    or not all(33 <= ord(char) <= 126 for char in token)):
                raise ValueError("invalid credential")
            request = Request(self._endpoint, data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                              method="POST", headers={"Authorization": "Bearer " + token,
                                                      "Content-Type": "application/json", "Accept": "application/json"})
        except Exception:
            return {**evidence, "status": "failed",
                    "detail": "The message could not be prepared or authorized. No send request was made."}
        unknown = {**evidence, "status": "unknown",
                   "detail": "Message acceptance is unknown. No retry was attempted; reconcile before sending again."}
        try:
            if not self._authorized(recipient_e164, can_execute):
                return denied
            response = self._transport(request, timeout=self._timeout, max_response_bytes=MAX_RESPONSE_BYTES)
            if not isinstance(response, WhatsAppResponse) or type(response.status_code) is not int:
                return unknown
            if response.status_code in _REJECTIONS:
                return {**evidence, "status": "failed", "detail": "WhatsApp rejected this request. No refresh or resend was attempted."}
            if response.status_code != 200 or not isinstance(response.body, bytes) or len(response.body) > MAX_RESPONSE_BYTES:
                return unknown
            receipt = _receipt(response.body, recipient_e164, token)
            if receipt is None:
                return unknown
            identifier, status = receipt
            if status != "accepted":
                return {**unknown, "provider_message_id": identifier}
            return {**evidence, "status": "accepted", "provider_message_id": identifier,
                    "detail": "WhatsApp accepted the message and returned its identifier. Delivery remains unverified."}
        except Exception:
            # A timeout/read/parse failure may happen after the provider received it.
            return unknown
