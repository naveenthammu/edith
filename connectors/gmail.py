"""One-attempt plaintext Gmail sending with injected credentials; never auto-refresh/retry.

The caller must obtain exact-recipient/content approval before calling this adapter.
The trusted credential provider supplies a token with gmail.send scope. Production
setup must supply sender_email from verified account configuration; a bearer string
does not prove its mailbox identity. sender_email=None supports unconfigured/offline
fixtures and has no live-account verification or missing-From normalization guarantee.
This module
does not load account files, discover credentials, refresh tokens, or verify delivery.
API: https://developers.google.com/workspace/gmail/api/guides/sending
"""
import base64
from dataclasses import dataclass
from email.message import EmailMessage
from email.policy import SMTP
import json
import math
import re
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


ENDPOINT = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
MAX_RESPONSE_BYTES = 65_536
_LOCAL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}\Z")
_DOMAIN_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_TOKEN = re.compile(r"[A-Za-z0-9._~+/-]+=*\Z")
_REJECTIONS = frozenset({400, 401, 403, 404, 429})


class GmailError(ValueError):
    """Safe validation error; never contains credentials or provider response details."""


@dataclass(frozen=True)
class GmailResponse:
    status_code: int
    body: bytes


def validate_content(subject, body) -> None:
    if (not isinstance(subject, str) or not subject.strip() or len(subject) > 200
            or any(ord(character) < 32 or ord(character) == 127 for character in subject)):
        raise GmailError("Use a nonempty subject of at most 200 characters without header control characters.")
    if (not isinstance(body, str) or not body.strip() or len(body) > 4000
            or any((ord(character) < 32 and character not in "\r\n\t") or ord(character) == 127 for character in body)):
        raise GmailError("Use nonempty plaintext of at most 4,000 characters without binary control characters.")
    try:
        subject.encode("utf-8")
        body.encode("utf-8")
    except UnicodeError:
        raise GmailError("Message content must be valid Unicode text.") from None


def validate_address(recipient) -> None:
    if not isinstance(recipient, str) or len(recipient) > 254 or not recipient.isascii() or recipient.count("@") != 1:
        raise GmailError("Use one exact ASCII recipient address without display names or extra recipients.")
    local, domain = recipient.split("@")
    labels = domain.split(".")
    if (not _LOCAL.fullmatch(local) or local.startswith(".") or local.endswith(".") or ".." in local
            or len(labels) < 2 or any(not _DOMAIN_LABEL.fullmatch(label) for label in labels)):
        raise GmailError("Use one exact ASCII recipient address without display names or extra recipients.")


def validate_message(recipient, subject, body) -> None:
    validate_address(recipient)
    validate_content(subject, body)


def valid_message_id(value) -> bool:
    # Gmail documents an opaque string, not a guaranteed hexadecimal format.
    return isinstance(value, str) and 1 <= len(value) <= 256 and all(33 <= ord(char) <= 126 for char in value)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_transport(request, *, timeout, max_response_bytes):
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        response = opener.open(request, timeout=timeout)
    except HTTPError as error:
        status = error.code
        error.close()  # Provider error bodies are neither read nor exposed.
        return GmailResponse(status, b"")
    with response:
        if response.status != 200:
            return GmailResponse(response.status, b"")
        length = response.headers.get("Content-Length")
        if length is not None and (not length.isdigit() or int(length) > max_response_bytes):
            return GmailResponse(200, b"")
        return GmailResponse(response.status, response.read(max_response_bytes + 1))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate response field")
        result[key] = value
    return result


def _can_execute(gate):
    try:
        return gate() is True
    except Exception:
        return False


class Gmail:
    def __init__(self, credential_provider, *, sender_email=None, timeout=5, transport=None):
        if not callable(credential_provider) or (transport is not None and not callable(transport)):
            raise GmailError("Configure a trusted credential provider and optional test transport.")
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise GmailError("Timeout must be greater than zero and at most 60 seconds.")
        if sender_email is not None:
            validate_address(sender_email)
        self._sender_email = sender_email
        self._credential_provider = credential_provider
        self._transport = _http_transport if transport is None else transport
        self._timeout = timeout

    @property
    def sender_email(self):
        """Read-only configured sender for trusted approval previews; never a model override."""
        return self._sender_email

    def send(self, recipient, subject, body, *, can_execute=lambda: True):
        """Make at most one POST; accepted means a provider receipt, never confirmed delivery."""
        validate_message(recipient, subject, body)
        evidence = {"provider": "gmail", "recipient": recipient, "provider_message_id": None,
                    "delivery_status": "unverified"}
        cancelled = {**evidence, "status": "failed", "detail": "Message sending was cancelled or is no longer authorized. No send request was made."}
        if not _can_execute(can_execute):
            return cancelled
        try:
            message = EmailMessage(policy=SMTP)
            message["To"] = recipient
            message["Subject"] = subject
            if self._sender_email is not None:
                message["From"] = self._sender_email
            # No message/model-controlled From, Reply-To, Cc, Bcc, HTML or attachments.
            message.set_content(body, subtype="plain", charset="utf-8")
            raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
            token = self._credential_provider()  # Only once; never refresh after a send attempt.
            if not isinstance(token, str) or not 1 <= len(token) <= 8192 or not _TOKEN.fullmatch(token):
                raise ValueError("invalid credential")
            request = Request(ENDPOINT, data=json.dumps({"raw": raw}, separators=(",", ":")).encode("utf-8"),
                              method="POST", headers={"Authorization": "Bearer " + token,
                                                      "Content-Type": "application/json", "Accept": "application/json"})
        except Exception:
            return {**evidence, "status": "failed", "detail": "The message could not be prepared or authorized; no send request was made."}
        unknown = {**evidence, "status": "unknown",
                   "detail": "Message acceptance is unknown. No retry was attempted; reconcile before sending again."}
        try:
            if not _can_execute(can_execute):
                return cancelled
            response = self._transport(request, timeout=self._timeout, max_response_bytes=MAX_RESPONSE_BYTES)
            if not isinstance(response, GmailResponse) or type(response.status_code) is not int:
                return unknown
            if response.status_code in _REJECTIONS:
                return {**evidence, "status": "failed", "detail": "Gmail rejected this request. No refresh or resend was attempted."}
            if response.status_code != 200 or not isinstance(response.body, bytes) or len(response.body) > MAX_RESPONSE_BYTES:
                return unknown
            data = json.loads(response.body, object_pairs_hook=_unique_object)
            if not isinstance(data, dict) or not valid_message_id(data.get("id")) or token in data["id"]:
                return unknown
            return {**evidence, "provider_message_id": data["id"], "status": "accepted",
                    "detail": "Gmail accepted the message and returned its identifier. Delivery remains unverified."}
        except Exception:
            # Transmission may have happened before any timeout, read failure or parse error.
            return unknown
