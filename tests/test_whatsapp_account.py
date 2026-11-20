import importlib
import importlib.util
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from connectors.whatsapp import WhatsAppResponse


TOKEN = "synthetic-secret-business-token"
PHONE = {"id": "12345", "display_phone_number": "+1 650-555-1234", "verified_name": "Example Shop"}


def api():
    assert importlib.util.find_spec("connectors.whatsapp_account") is not None, "Account verifier is missing"
    return importlib.import_module("connectors.whatsapp_account")


class Wire:
    def __init__(self, *responses):
        self.responses = list(responses or [{"data": [PHONE]}])
        self.requests = []

    def __call__(self, request, **kwargs):
        self.requests.append((request, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, WhatsAppResponse):
            return result
        return WhatsAppResponse(200, json.dumps(result).encode())


def account(wire=None, **kwargs):
    return api().BusinessAccount("alice", "23456", "12345", lambda: TOKEN,
                                 connection_id="business-one", transport=wire or Wire(), clock=lambda: 100, **kwargs)


def test_fixed_origin_account_phone_linkage_and_safe_stable_snapshot():
    wire = Wire({"data": [PHONE]}, {"data": [PHONE]})
    obj = account(wire)
    snapshot = obj.snapshot()
    assert snapshot == obj.snapshot()
    assert snapshot["principal_id"] == "alice" and snapshot["waba_id"] == "23456"
    assert snapshot["phone_number_id"] == "12345" and snapshot["display_phone_number"] == "+16505551234"
    assert snapshot["verified_name"] == "Example Shop" and snapshot["verified_at"] == 100
    assert len(snapshot["version"]) == 64 and snapshot["connection_id"] == "business-one"
    assert TOKEN not in repr(snapshot)
    request, options = wire.requests[0]
    url = urlsplit(request.full_url)
    assert (url.scheme, url.netloc, url.path) == ("https", "graph.facebook.com", "/v26.0/23456/phone_numbers")
    assert parse_qs(url.query)["fields"] == ["id,display_phone_number,verified_name"]
    assert request.get_method() == "GET" and request.data is None
    assert request.get_header("Authorization") == "Bearer " + TOKEN
    assert TOKEN not in request.full_url and options == {"timeout": 5, "max_response_bytes": 65536}


@pytest.mark.parametrize("payload", [
    {"data": []}, {"data": [{**PHONE, "id": "99999"}]}, {"data": [PHONE, PHONE]},
    {"data": [{**PHONE, "display_phone_number": "http://bad/"}]},
    {"data": [{**PHONE, "verified_name": "bad\nname"}]}, {"data": [PHONE], "error": {}},
    {"data": [PHONE], "paging": {"next": "https://evil.invalid/"}},
])
def test_unproven_or_ambiguous_account_binding_is_refused(payload):
    with pytest.raises(api().BusinessAccountError):
        account(Wire(payload)).snapshot()


def test_pagination_reconstructs_fixed_origin_and_never_follows_next_url():
    wire = Wire({"data": [{**PHONE, "id": "99999"}], "paging": {"next": "https://evil.invalid/?access_token=" + TOKEN, "cursors": {"after": "cursor_2"}}},
                {"data": [PHONE]})
    assert account(wire).snapshot()["phone_number_id"] == "12345"
    second = wire.requests[1][0].full_url
    assert second.startswith("https://graph.facebook.com/v26.0/23456/phone_numbers?")
    assert parse_qs(urlsplit(second).query)["after"] == ["cursor_2"]
    assert "evil" not in second and "secret" not in second
    assert TOKEN not in second


def test_repeated_pagination_cursor_is_refused_without_looping():
    page = {"data": [PHONE], "paging": {"next": "ignored", "cursors": {"after": "same"}}}
    wire = Wire(page, page)
    with pytest.raises(api().BusinessAccountError):
        account(wire).snapshot()
    assert len(wire.requests) == 2


@pytest.mark.parametrize("result", [WhatsAppResponse(302, b"secret"), WhatsAppResponse(401, b"secret"),
                                    WhatsAppResponse(200, b"x" * 65537), WhatsAppResponse(200, b'{"data":[],"data":[]}'),
                                    TimeoutError(TOKEN), WhatsAppResponse(200, b"not json")])
def test_read_failures_are_bounded_safe_and_not_retried(result):
    wire = Wire(result)
    with pytest.raises(api().BusinessAccountError) as caught:
        account(wire).snapshot()
    assert TOKEN not in str(caught.value) and len(wire.requests) == 1


@pytest.mark.parametrize("arguments", [("guest!", "23456", "12345"), ("alice", "../evil", "12345"),
                                        ("alice", "23456", "+12345"), ("alice", "23456", "12345/anything")])
def test_config_identifiers_never_become_arbitrary_paths(arguments):
    with pytest.raises(api().BusinessAccountError):
        api().BusinessAccount(*arguments, lambda: TOKEN, connection_id="business-one")


def test_credential_switch_changes_account_version_and_invalidates_old_binding():
    credential = [TOKEN]
    obj = api().BusinessAccount("alice", "23456", "12345", lambda: credential[0], connection_id="business-one",
                                transport=Wire({"data": [PHONE]}, {"data": [PHONE]}), clock=lambda: 100)
    before = obj.snapshot()
    credential[0] = "another-synthetic-token"
    after = obj.snapshot()
    assert before["version"] != after["version"]
    with pytest.raises(api().BusinessAccountError):
        obj.authorized_token(before)


def test_protected_token_file_rejects_relative_and_sync_paths_before_reading():
    for path in ("credentials.json", "C:/Users/Example/OneDrive/credentials.json"):
        with pytest.raises(api().BusinessAccountError):
            api().ProtectedTokenFile(path)()


def test_protected_token_file_rejects_unprotected_directory(tmp_path, monkeypatch):
    mod = api()
    selected = tmp_path / "credentials.json"
    selected.write_text(json.dumps({"access_token": TOKEN}), encoding="utf-8")
    def deny(*_):
        raise RuntimeError("unsafe ACL " + TOKEN)
    monkeypatch.setattr(mod, "_verify_permissions", deny)
    with pytest.raises(mod.BusinessAccountError) as caught:
        mod.ProtectedTokenFile(selected)()
    assert TOKEN not in str(caught.value)


def test_protected_token_file_reads_only_exact_bounded_private_schema(tmp_path, monkeypatch):
    mod = api()
    selected = tmp_path / "credentials.json"
    selected.write_text(json.dumps({"access_token": TOKEN}), encoding="utf-8")
    checks = []
    monkeypatch.setattr(mod, "_verify_permissions", lambda paths: checks.append(paths))
    source = mod.ProtectedTokenFile(selected)
    assert source() == TOKEN and checks
    selected.write_text(json.dumps({"access_token": TOKEN, "endpoint": "https://evil.invalid"}), encoding="utf-8")
    with pytest.raises(mod.BusinessAccountError):
        source()


def test_json_escaped_credential_reflection_never_enters_snapshot():
    token = 'secret"with\\escapes'
    obj = api().BusinessAccount("alice", "23456", "12345", lambda: token, connection_id="one",
                                transport=Wire({"data": [{**PHONE, "verified_name": token}]}))
    with pytest.raises(api().BusinessAccountError) as caught:
        obj.snapshot()
    assert token not in str(caught.value)


def test_protected_file_replacement_during_permission_check_is_refused(tmp_path, monkeypatch):
    mod = api()
    selected = tmp_path / "credentials.json"
    selected.write_text(json.dumps({"access_token": TOKEN}), encoding="utf-8")
    checks = []
    def replace(paths):
        checks.append(1)
        if len(checks) == 2:
            replacement = tmp_path / "new.json"
            replacement.write_text(json.dumps({"access_token": "different-token"}), encoding="utf-8")
            replacement.replace(selected)
    monkeypatch.setattr(mod, "_verify_permissions", replace)
    with pytest.raises(mod.BusinessAccountError):
        mod.ProtectedTokenFile(selected)()


def test_actual_native_private_file_can_be_read_and_hardlink_is_refused(tmp_path):
    import os
    from tools.hub_admin import _restrict_directory
    mod = api()
    directory = tmp_path / "business-account"
    directory.mkdir()
    _restrict_directory(directory)
    selected = directory / "credentials.json"
    fd = os.open(selected, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump({"access_token": TOKEN}, stream)
    assert mod.ProtectedTokenFile(selected)() == TOKEN
    os.link(selected, directory / "other.json")
    with pytest.raises(mod.BusinessAccountError):
        mod.ProtectedTokenFile(selected)()


def test_verification_time_does_not_change_stable_account_version():
    clock = [100]
    obj = api().BusinessAccount("alice", "23456", "12345", lambda: TOKEN, connection_id="one",
                                transport=Wire({"data": [PHONE]}, {"data": [PHONE]}), clock=lambda: clock[0])
    old = obj.snapshot()
    clock[0] = 200
    new = obj.snapshot()
    assert old["version"] == new["version"] and old["verified_at"] != new["verified_at"]


