import base64
from email import policy
from email.parser import BytesParser
from io import BytesIO
import json
from urllib.error import HTTPError
from urllib.request import OpenerDirector, ProxyHandler

import pytest

from connectors.gmail import Gmail, GmailError, GmailResponse


TOKEN = "synthetic-bearer-token-never-display"
RECIPIENT = "friend@example.test"


class Wire:
    def __init__(self, response=None, error=None):
        self.response = response or GmailResponse(200, b'{"id":"provider-123","threadId":"thread-1"}')
        self.error = error
        self.requests = []

    def __call__(self, request, *, timeout, max_response_bytes):
        self.requests.append((request, timeout, max_response_bytes))
        if self.error:
            raise self.error
        return self.response


def test_send_serializes_exact_single_recipient_plaintext_mime_to_fixed_endpoint():
    wire = Wire()
    adapter = Gmail(lambda: TOKEN, timeout=4, transport=wire)
    result = adapter.send(RECIPIENT, "Meeting — update", "Hello friend.\nSecond line.")
    assert result["status"] == "accepted" and result["provider_message_id"] == "provider-123"
    assert result["recipient"] == RECIPIENT and result["provider"] == "gmail"
    assert result["delivery_status"] == "unverified"
    assert len(wire.requests) == 1
    request, timeout, limit = wire.requests[0]
    assert request.full_url == "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
    assert request.get_method() == "POST" and timeout == 4 and 0 < limit <= 65_536
    assert request.get_header("Authorization") == "Bearer " + TOKEN
    payload = json.loads(request.data)
    assert set(payload) == {"raw"}
    message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(payload["raw"]))
    assert str(message["To"]) == RECIPIENT and str(message["Subject"]) == "Meeting — update"
    assert all(message.get(name) is None for name in ("Cc", "Bcc", "From", "Reply-To"))
    assert not message.is_multipart() and message.get_content_type() == "text/plain"
    assert message.get_content().replace("\r\n", "\n") == "Hello friend.\nSecond line.\n"
    assert TOKEN not in str(result)


@pytest.mark.parametrize("recipient", ["a@example.test,b@example.test", "Name <a@example.test>",
                                        "a@example.test\r\nBcc: victim@example.test", "é@example.test", "a@é.test",
                                        "a..b@example.test", ".a@example.test", "a.@example.test", "a@-bad.test",
                                        "a@localhost", "a" * 65 + "@example.test", None])
def test_bad_recipient_never_loads_credentials_or_reaches_transport(recipient):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid recipient reached credentials or transport")
    with pytest.raises(GmailError):
        Gmail(forbidden, transport=forbidden).send(recipient, "Subject", "Body")


@pytest.mark.parametrize("subject,body", [("x\r\nBcc:other@example.test", "Body"), ("x" * 201, "Body"),
                                           ("Subject", "x" * 4001), (" ", "Body"), ("Subject", " \n"),
                                           ("Subject", "bad\x00body"), ("Subject", "\ud800"), (None, "Body")])
def test_bad_content_fails_before_credentials_or_network(subject, body):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid content reached credentials or transport")
    with pytest.raises(GmailError):
        Gmail(forbidden, transport=forbidden).send(RECIPIENT, subject, body)


@pytest.mark.parametrize("timeout", [True, 0, -1, 61, float("nan"), "5"])
def test_constructor_rejects_unbounded_timeout(timeout):
    with pytest.raises(GmailError):
        Gmail(lambda: TOKEN, timeout=timeout, transport=Wire())


def test_missing_credentials_are_pretransmission_failure_without_details():
    wire = Wire()
    def credentials():
        raise RuntimeError("private OAuth refresh details " + TOKEN)
    result = Gmail(credentials, transport=wire).send(RECIPIENT, "Subject", "Body")
    assert result["status"] == "failed" and result["provider_message_id"] is None
    assert wire.requests == [] and TOKEN not in str(result) and "OAuth" not in str(result)


@pytest.mark.parametrize("token", [None, "", "header\r\ninjection", "token with spaces", "x" * 8193])
def test_invalid_token_never_reaches_transport(token):
    wire = Wire()
    result = Gmail(lambda: token, transport=wire).send(RECIPIENT, "Subject", "Body")
    assert result["status"] == "failed" and wire.requests == []


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429])
def test_documented_rejection_is_failed_without_refresh_or_resend(status):
    calls = []
    def credential():
        calls.append("token")
        return TOKEN
    wire = Wire(GmailResponse(status, TOKEN.encode()))
    result = Gmail(credential, transport=wire).send(RECIPIENT, "Subject", "Body")
    assert result["status"] == "failed" and result["provider_message_id"] is None
    assert calls == ["token"] and len(wire.requests) == 1 and TOKEN not in str(result)


@pytest.mark.parametrize("status", [302, 307, 308, 408, 409, 500, 502, 503, 504])
def test_ambiguous_http_response_stays_unknown_without_retry(status):
    wire = Wire(GmailResponse(status, b'{"id":"misleading-id"}'))
    result = Gmail(lambda: TOKEN, transport=wire).send(RECIPIENT, "Subject", "Body")
    assert result["status"] == "unknown" and result["provider_message_id"] is None
    assert len(wire.requests) == 1


@pytest.mark.parametrize("error", [TimeoutError("private transport details"), OSError(TOKEN)])
def test_transmission_exception_remains_unknown_without_retry_or_secret_details(error):
    wire = Wire(error=error)
    result = Gmail(lambda: TOKEN, transport=wire).send(RECIPIENT, "Subject", "Body")
    assert result["status"] == "unknown" and len(wire.requests) == 1
    assert TOKEN not in str(result) and "private transport" not in str(result)


@pytest.mark.parametrize("body", [b"not JSON", b"[]", b"{}", b'{"id":null}', b'{"id":""}',
                                  b'{"id":"a","id":"b"}', b'{"id":"bad\\nheader"}',
                                  b'{"id":"' + TOKEN.encode() + b'"}', b" " * 65_537])
def test_success_without_unambiguous_bounded_provider_id_remains_unknown(body):
    wire = Wire(GmailResponse(200, body))
    result = Gmail(lambda: TOKEN, transport=wire).send(RECIPIENT, "Subject", "Body")
    assert result["status"] == "unknown" and result["provider_message_id"] is None
    assert len(wire.requests) == 1 and TOKEN not in str(result)


def test_real_default_transport_disables_environment_proxy_and_refuses_redirect(monkeypatch):
    calls = []
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    def open_without_network(opener, request, timeout):
        calls.append(request.full_url)
        assert all(not isinstance(handler, ProxyHandler) or handler.proxies == {} for handler in opener.handlers)
        redirect = next(handler for handler in opener.handlers if hasattr(handler, "redirect_request"))
        assert redirect.redirect_request(request, None, 302, "redirect", {}, "https://attacker.invalid/") is None
        raise HTTPError(request.full_url, 302, "redirect", {}, BytesIO(b"private response"))
    monkeypatch.setattr(OpenerDirector, "open", open_without_network)
    result = Gmail(lambda: TOKEN).send(RECIPIENT, "Subject", "Body")
    assert result["status"] == "unknown" and len(calls) == 1


def test_real_default_transport_bounds_read_and_accepts_valid_response(monkeypatch):
    reads = []
    class Response(BytesIO):
        status = 200
        headers = {"Content-Length": "21"}
        def read(self, size=-1):
            reads.append(size)
            return super().read(size)
    monkeypatch.setattr(OpenerDirector, "open", lambda *_args, **_kwargs: Response(b'{"id":"provider-123"}'))
    result = Gmail(lambda: TOKEN).send(RECIPIENT, "Subject", "Body")
    assert result["status"] == "accepted" and reads == [65_537]


def test_configured_sender_is_emitted_only_from_trusted_constructor():
    wire = Wire()
    adapter = Gmail(lambda: TOKEN, sender_email="owner@example.test", transport=wire)
    assert adapter.sender_email == "owner@example.test"
    with pytest.raises(AttributeError):
        adapter.sender_email = "changed@example.test"
    result = adapter.send(RECIPIENT, "Subject", "Body")
    raw = json.loads(wire.requests[0][0].data)["raw"]
    message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw))
    assert str(message["From"]) == "owner@example.test"
    assert str(message["To"]) == RECIPIENT and result["status"] == "accepted"


@pytest.mark.parametrize("sender", ["owner@example.test\r\nBcc:other@example.test", "Display <owner@example.test>",
                                     "owner@example.test,other@example.test", "é@example.test", ""])
def test_sender_configuration_rejects_header_injection_and_ambiguous_identity(sender):
    with pytest.raises(GmailError):
        Gmail(lambda: TOKEN, sender_email=sender, transport=Wire())


def test_send_caller_cannot_override_configured_sender():
    wire = Wire()
    adapter = Gmail(lambda: TOKEN, sender_email="owner@example.test", transport=wire)
    with pytest.raises(TypeError):
        adapter.send(RECIPIENT, "Subject", "Body", sender_email="impersonated@example.test")
    assert wire.requests == []


def test_cancelled_send_gate_never_acquires_credentials_or_posts():
    wire = Wire()
    def forbidden():
        pytest.fail("Cancelled send acquired credentials")
    result = Gmail(forbidden, transport=wire).send(RECIPIENT, "Subject", "Body", can_execute=lambda: False)
    assert result["status"] == "failed" and result["provider_message_id"] is None and wire.requests == []


def test_cancellation_during_credential_refresh_never_posts():
    wire = Wire()
    active = {"value": True}
    def credential():
        active["value"] = False
        return TOKEN
    result = Gmail(credential, transport=wire).send(RECIPIENT, "Subject", "Body", can_execute=lambda: active["value"])
    assert result["status"] == "failed" and result["provider_message_id"] is None and wire.requests == []


def test_gate_failure_after_credentials_is_failed_before_transmission():
    wire = Wire()
    calls = []
    def gate():
        calls.append("gate")
        if len(calls) > 1:
            raise RuntimeError("PRIVATE_CANCELLATION_DETAILS")
        return True
    result = Gmail(lambda: TOKEN, transport=wire).send(RECIPIENT, "Subject", "Body", can_execute=gate)
    assert result["status"] == "failed" and wire.requests == []
    assert "PRIVATE_CANCELLATION_DETAILS" not in str(result)
