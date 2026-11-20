from io import BytesIO
import json
from urllib.error import HTTPError
from urllib.request import OpenerDirector, ProxyHandler

import pytest

from connectors.whatsapp import WhatsAppBusiness, WhatsAppError, WhatsAppResponse


TOKEN = "synthetic-meta-token-never-display"
PHONE_ID = "106540352242922"
RECIPIENT = "+16505551234"
SUCCESS = b'{"messaging_product":"whatsapp","contacts":[{"input":"+16505551234","wa_id":"16505551234"}],"messages":[{"id":"wamid.synthetic-123"}]}'


class Wire:
    def __init__(self, response=None, error=None):
        self.response = WhatsAppResponse(200, SUCCESS) if response is None else response
        self.error = error
        self.requests = []

    def __call__(self, request, *, timeout, max_response_bytes):
        self.requests.append((request, timeout, max_response_bytes))
        if self.error is not None:
            raise self.error
        return self.response


def allowed(_sender, _recipient):
    # A synthetic trusted policy only; production must consult consent/window state.
    return True


def send(adapter, recipient=RECIPIENT, text="Hello — update\nSecond line."):
    return adapter.send_text(recipient, text, can_execute=lambda: True)


def test_text_send_preserves_exact_content_and_fixed_sender_without_delivery_claim():
    wire = Wire()
    policy_calls = []

    def policy(sender, recipient):
        policy_calls.append((sender, recipient))
        return True

    result = send(WhatsAppBusiness(PHONE_ID, lambda: TOKEN, recipient_policy=policy, timeout=4, transport=wire))
    assert result["status"] == "accepted"
    assert result["provider_message_id"] == "wamid.synthetic-123"
    assert result["recipient_e164"] == RECIPIENT and result["phone_number_id"] == PHONE_ID
    assert result["provider"] == "whatsapp_business" and result["delivery_status"] == "unverified"
    assert len(wire.requests) == 1
    request, timeout, limit = wire.requests[0]
    assert request.full_url == "https://graph.facebook.com/v26.0/106540352242922/messages"
    assert request.get_method() == "POST" and timeout == 4 and limit == 65_536
    assert request.get_header("Authorization") == "Bearer " + TOKEN
    assert json.loads(request.data) == {
        "messaging_product": "whatsapp", "recipient_type": "individual", "to": RECIPIENT,
        "type": "text", "text": {"body": "Hello — update\nSecond line.", "preview_url": False},
    }
    assert policy_calls == [(PHONE_ID, RECIPIENT), (PHONE_ID, RECIPIENT)]
    assert TOKEN not in str(result) and "Second line" not in str(result)


@pytest.mark.parametrize("policy,gate", [(None, lambda: True), (allowed, None), (None, None)])
def test_default_policy_and_execution_gate_deny_before_credentials(policy, gate):
    effects = []
    wire = Wire()
    adapter = WhatsAppBusiness(PHONE_ID, lambda: effects.append("credential") or TOKEN,
                               recipient_policy=policy, transport=wire)
    result = adapter.send_text(RECIPIENT, "Body", can_execute=gate)
    assert result["status"] == "failed" and result["provider_message_id"] is None
    assert effects == [] and wire.requests == []


@pytest.mark.parametrize("decision", [False, None, 1, "true", {"allowed": True}])
def test_only_literal_true_policy_can_authorize_recipient(decision):
    wire = Wire()
    credentials = []
    adapter = WhatsAppBusiness(PHONE_ID, lambda: credentials.append("called") or TOKEN,
                               recipient_policy=lambda *_: decision, transport=wire)
    result = send(adapter)
    assert result["status"] == "failed" and wire.requests == [] and credentials == []


def test_policy_exception_never_reaches_credentials_or_leaks_details():
    wire = Wire()
    def policy(*_):
        raise RuntimeError("private consent record " + TOKEN)
    result = send(WhatsAppBusiness(PHONE_ID, lambda: TOKEN, recipient_policy=policy, transport=wire))
    assert result["status"] == "failed" and wire.requests == []
    assert TOKEN not in str(result) and "private consent" not in str(result)


def test_consent_or_window_change_during_credentials_blocks_post():
    eligible = True
    wire = Wire()
    def credential():
        nonlocal eligible
        eligible = False
        return TOKEN
    result = send(WhatsAppBusiness(PHONE_ID, credential, recipient_policy=lambda *_: eligible, transport=wire))
    assert result["status"] == "failed" and wire.requests == []


@pytest.mark.parametrize("cancel_during", ["credential", "policy"])
def test_cancellation_during_blocking_preparation_is_checked_immediately_before_post(cancel_during):
    current = True
    credentials = []
    wire = Wire()
    policy_calls = []
    def credential():
        nonlocal current
        credentials.append(1)
        if cancel_during == "credential":
            current = False
        return TOKEN
    def policy(*_):
        nonlocal current
        policy_calls.append(1)
        if cancel_during == "policy" and len(policy_calls) == 2:
            current = False
        return True
    adapter = WhatsAppBusiness(PHONE_ID, credential, recipient_policy=policy, transport=wire)
    result = adapter.send_text(RECIPIENT, "Body", can_execute=lambda: current)
    assert result["status"] == "failed" and wire.requests == [] and credentials == [1]


@pytest.mark.parametrize("recipient", [None, True, "16505551234", "+012345", "+1", "+" + "1" * 16,
                                        "+16505551234\n", "+16505551234,+16505551235", "+１２３", "https://evil.invalid/"])
def test_invalid_recipient_is_rejected_before_policy_or_credentials(recipient):
    def forbidden(*_, **__):
        pytest.fail("Invalid recipient reached an external dependency")
    with pytest.raises(WhatsAppError):
        send(WhatsAppBusiness(PHONE_ID, forbidden, recipient_policy=forbidden, transport=forbidden), recipient)


@pytest.mark.parametrize("body", [None, True, "", " \n", "x" * 4097, "bad\x00text", "bad\x7ftext", "\ud800"])
def test_invalid_text_is_rejected_before_policy_or_credentials(body):
    def forbidden(*_, **__):
        pytest.fail("Invalid text reached an external dependency")
    with pytest.raises(WhatsAppError):
        send(WhatsAppBusiness(PHONE_ID, forbidden, recipient_policy=forbidden, transport=forbidden), text=body)


def test_unicode_character_limit_is_not_an_ascii_byte_limit():
    wire = Wire()
    result = send(WhatsAppBusiness(PHONE_ID, lambda: TOKEN, recipient_policy=allowed, transport=wire), text="😀" * 4096)
    assert result["status"] == "accepted"
    assert json.loads(wire.requests[0][0].data)["text"]["body"] == "😀" * 4096


@pytest.mark.parametrize("phone_id", [None, True, 123, "", "00123", "1/../../messages", "https://evil.invalid", "１２３", "1" * 33])
def test_constructor_rejects_unsafe_sender_identifier(phone_id):
    with pytest.raises(WhatsAppError):
        WhatsAppBusiness(phone_id, lambda: TOKEN)


@pytest.mark.parametrize("timeout", [True, 0, -1, 61, float("inf"), float("nan"), "5"])
def test_constructor_rejects_unbounded_timeout(timeout):
    with pytest.raises(WhatsAppError):
        WhatsAppBusiness(PHONE_ID, lambda: TOKEN, timeout=timeout)


@pytest.mark.parametrize("kwargs", [{"credential_provider": "token"}, {"recipient_policy": True}, {"transport": True}])
def test_constructor_requires_trusted_callable_dependencies(kwargs):
    arguments = {"credential_provider": lambda: TOKEN, **kwargs}
    with pytest.raises(WhatsAppError):
        WhatsAppBusiness(PHONE_ID, **arguments)


@pytest.mark.parametrize("token", [None, "", "token\r\nheader", "token with spaces", "x" * 8193, "é"])
def test_invalid_bearer_is_a_pretransmission_failure(token):
    wire = Wire()
    result = send(WhatsAppBusiness(PHONE_ID, lambda: token, recipient_policy=allowed, transport=wire))
    assert result["status"] == "failed" and wire.requests == []


def test_credential_error_is_safe_and_never_retried():
    wire = Wire()
    calls = []
    def credentials():
        calls.append(1)
        raise RuntimeError(TOKEN + " private refresh details")
    result = send(WhatsAppBusiness(PHONE_ID, credentials, recipient_policy=allowed, transport=wire))
    assert result["status"] == "failed" and wire.requests == [] and calls == [1]
    assert TOKEN not in str(result) and "refresh details" not in str(result)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429])
def test_http_rejection_has_no_refresh_or_resend(status):
    credentials = []
    wire = Wire(WhatsAppResponse(status, TOKEN.encode()))
    result = send(WhatsAppBusiness(PHONE_ID, lambda: credentials.append(1) or TOKEN, recipient_policy=allowed, transport=wire))
    assert result["status"] == "failed" and result["provider_message_id"] is None
    assert credentials == [1] and len(wire.requests) == 1 and TOKEN not in str(result)


@pytest.mark.parametrize("status", [201, 302, 307, 308, 408, 409, 500, 502, 503, 504])
def test_ambiguous_http_result_is_unknown_never_retried(status):
    wire = Wire(WhatsAppResponse(status, SUCCESS))
    result = send(WhatsAppBusiness(PHONE_ID, lambda: TOKEN, recipient_policy=allowed, transport=wire))
    assert result["status"] == "unknown" and result["provider_message_id"] is None
    assert len(wire.requests) == 1


@pytest.mark.parametrize("error", [TimeoutError("private timeout"), OSError(TOKEN)])
def test_transport_exception_is_unknown_never_retried_or_echoed(error):
    wire = Wire(error=error)
    result = send(WhatsAppBusiness(PHONE_ID, lambda: TOKEN, recipient_policy=allowed, transport=wire))
    assert result["status"] == "unknown" and len(wire.requests) == 1
    assert TOKEN not in str(result) and "private timeout" not in str(result)


@pytest.mark.parametrize("mutation", [
    {"messaging_product": "other"}, {"contacts": []}, {"messages": []},
    {"contacts": [{"input": "+16505559999", "wa_id": "16505551234"}]},
    {"contacts": [{"input": "+16505551234", "wa_id": "16505559999"}]},
    {"contacts": [{"input": "+16505551234", "wa_id": "16505551234"}] * 2},
    {"messages": [{"id": "wamid.a"}, {"id": "wamid.b"}]},
    {"messages": [{"id": None}]}, {"messages": [{"id": ""}]},
    {"messages": [{"id": "bad\nreceipt"}]}, {"messages": [{"id": "x" * 513}]},
    {"messages": [{"id": TOKEN}]}, {"messages": [{"id": "wamid.a", "message_status": "delivered"}]},
    {"error": {"code": 100}},
])
def test_success_requires_unambiguous_exact_recipient_and_provider_receipt(mutation):
    payload = {**json.loads(SUCCESS), **mutation}
    wire = Wire(WhatsAppResponse(200, json.dumps(payload).encode()))
    result = send(WhatsAppBusiness(PHONE_ID, lambda: TOKEN, recipient_policy=allowed, transport=wire))
    assert result["status"] == "unknown" and result["provider_message_id"] is None
    assert len(wire.requests) == 1 and TOKEN not in str(result)


@pytest.mark.parametrize("status", ["held_for_quality_assessment", "paused"])
def test_held_or_paused_receipt_retains_reconciliation_id_without_acceptance_or_delivery_claim(status):
    payload = json.loads(SUCCESS)
    payload["messages"][0]["message_status"] = status
    wire = Wire(WhatsAppResponse(200, json.dumps(payload).encode()))
    result = send(WhatsAppBusiness(PHONE_ID, lambda: TOKEN, recipient_policy=allowed, transport=wire))
    assert result["status"] == "unknown" and result["provider_message_id"] == "wamid.synthetic-123"
    assert result["delivery_status"] == "unverified" and len(wire.requests) == 1


@pytest.mark.parametrize("body", [b"not JSON", b"[]", b"{}", b" " * 65_537,
                                  SUCCESS.replace(b'"messaging_product":"whatsapp"', b'"messaging_product":"whatsapp","messaging_product":"whatsapp"')])
def test_unreadable_or_ambiguous_bounded_response_is_unknown(body):
    wire = Wire(WhatsAppResponse(200, body))
    result = send(WhatsAppBusiness(PHONE_ID, lambda: TOKEN, recipient_policy=allowed, transport=wire))
    assert result["status"] == "unknown" and result["provider_message_id"] is None


def test_digits_only_contact_echo_and_explicit_accepted_state_are_supported():
    payload = json.loads(SUCCESS)
    payload["contacts"][0]["input"] = "16505551234"
    payload["messages"][0]["message_status"] = "accepted"
    wire = Wire(WhatsAppResponse(200, json.dumps(payload).encode()))
    result = send(WhatsAppBusiness(PHONE_ID, lambda: TOKEN, recipient_policy=allowed, transport=wire))
    assert result["status"] == "accepted" and result["recipient_e164"] == "+16505551234"


def test_default_transport_disables_environment_proxy_and_refuses_redirect(monkeypatch):
    calls = []
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    def open_without_network(opener, request, timeout):
        calls.append(request.full_url)
        assert all(not isinstance(handler, ProxyHandler) or handler.proxies == {} for handler in opener.handlers)
        redirect = next(handler for handler in opener.handlers if hasattr(handler, "redirect_request"))
        assert redirect.redirect_request(request, None, 302, "redirect", {}, "https://attacker.invalid/") is None
        raise HTTPError(request.full_url, 302, "redirect", {}, BytesIO(b"private response"))
    monkeypatch.setattr(OpenerDirector, "open", open_without_network)
    result = send(WhatsAppBusiness(PHONE_ID, lambda: TOKEN, recipient_policy=allowed))
    assert result["status"] == "unknown" and len(calls) == 1


@pytest.mark.parametrize("length", [None, str(len(SUCCESS)), "65537", "invalid"])
def test_default_transport_bounds_body_read_and_rejects_oversize_declared_body(monkeypatch, length):
    reads = []
    class Response(BytesIO):
        status = 200
        headers = {} if length is None else {"Content-Length": length}
        def read(self, size=-1):
            reads.append(size)
            return super().read(size)
    monkeypatch.setattr(OpenerDirector, "open", lambda *_args, **_kwargs: Response(SUCCESS))
    result = send(WhatsAppBusiness(PHONE_ID, lambda: TOKEN, recipient_policy=allowed))
    if length in {"65537", "invalid"}:
        assert result["status"] == "unknown" and reads == []
    else:
        assert result["status"] == "accepted" and reads == [65_537]
