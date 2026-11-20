from copy import deepcopy
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from connectors.gmail_account import GmailAccount, GmailAccountError, connect_account, gmail_from_env


NOW = 1_800_000_000
CLIENT_ID = "123-synthetic.apps.googleusercontent.com"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
SEND = "https://www.googleapis.com/auth/gmail.send"
TOKEN = "synthetic-access-token-private"
REFRESH = "synthetic-refresh-token-private"


class Hub:
    def __init__(self, role="adult", principal="owner"):
        self.identity = {"principal_id": principal, "role": role}
        self.calls = 0

    def capabilities(self):
        self.calls += 1
        return deepcopy(self.identity)


class Flow:
    def __init__(self):
        self.options = None
        self.configuration = None
        self.requested_scopes = None
        self.oauth2session = SimpleNamespace(token={"access_token": TOKEN, "refresh_token": REFRESH,
            "expires_in": 3600, "scope": [SEND, "openid", "email"], "token_type": "Bearer", "id_token": "synthetic-id-token"})

    def factory(self, configuration, scopes):
        self.configuration, self.requested_scopes = deepcopy(configuration), list(scopes)
        return self

    def run_local_server(self, **kwargs):
        self.options = kwargs
        return object()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    import connectors.gmail_account as module
    monkeypatch.setattr(module, "_protect_directory", lambda _: None, raising=False)
    monkeypatch.setattr(module, "_verify_permissions", lambda _: None, raising=False)
    monkeypatch.delenv("EDITH_GMAIL_ACCOUNT_CONFIG", raising=False)
    client = tmp_path / "desktop-client.json"
    client.write_text(json.dumps({"installed": {"client_id": CLIENT_ID, "client_secret": "synthetic-client-secret",
                                               "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                                               "token_uri": TOKEN_ENDPOINT, "redirect_uris": ["http://localhost"]}}), encoding="utf-8")
    flow = Flow()
    claims = {"aud": CLIENT_ID, "iss": "https://accounts.google.com", "exp": NOW + 3600, "iat": NOW,
              "email": "owner@gmail.com", "email_verified": True, "sub": "google-subject-123"}

    def verifier(token, audience):
        assert token == "synthetic-id-token" and audience == CLIENT_ID
        return {"nonce": flow.options["nonce"], **deepcopy(claims)}

    return SimpleNamespace(client=client, directory=tmp_path / "gmail-account", flow=flow, claims=claims,
                           verifier=verifier, hub=Hub(), module=module)


def connect(fixture):
    return connect_account(fixture.hub, str(fixture.client), str(fixture.directory),
                           flow_factory=fixture.flow.factory, id_token_verifier=fixture.verifier, clock=lambda: NOW)


def test_explicit_onboarding_binds_google_verified_identity_and_private_credentials(setup, capsys):
    result = connect(setup)
    assert result["status"] == "configured" and result["verified_email"] == "owner@gmail.com"
    assert result["principal_id"] == "owner" and setup.hub.calls == 2
    metadata = json.loads((setup.directory / "account.json").read_text())
    credentials = json.loads((setup.directory / "credentials.json").read_text())
    assert metadata["subject"] == "google-subject-123" and metadata["principal_id"] == "owner"
    assert credentials["type"] == "authorized_user" and credentials["refresh_token"] == REFRESH
    assert credentials["token_uri"] == TOKEN_ENDPOINT and credentials["account"] == "owner@gmail.com"
    assert set(setup.flow.requested_scopes) == {SEND, "openid", "email"}
    assert setup.flow.options["host"] == "127.0.0.1" and setup.flow.options["port"] == 0
    assert setup.flow.options["open_browser"] is True and setup.flow.options["authorization_prompt_message"] is None
    assert len(setup.flow.options["nonce"]) >= 32 and setup.flow.options["timeout_seconds"] == 300
    assert TOKEN not in str(result) and REFRESH not in str(result) and capsys.readouterr().out == ""


@pytest.mark.parametrize("change", [{"aud": "wrong-client"}, {"iss": "https://attacker.invalid"}, {"exp": NOW},
                                    {"email_verified": False}, {"email_verified": "true"}, {"sub": ""},
                                    {"email": "attacker\r\n@gmail.com"}, {"email": "owner@third-party.test"},
                                    {"nonce": "wrong-nonce"}])
def test_unverified_identity_never_creates_account_store(setup, change):
    setup.claims.update(change)
    with pytest.raises(GmailAccountError):
        connect(setup)
    assert not setup.directory.exists()


@pytest.mark.parametrize("changes", [{"scope": ["openid", "email"]}, {"refresh_token": None}, {"id_token": None},
                                     {"access_token": "bad token"}, {"expires_in": 0}])
def test_missing_scope_refresh_or_valid_grant_fails_before_storage(setup, changes):
    setup.flow.oauth2session.token.update(changes)
    with pytest.raises(GmailAccountError):
        connect(setup)
    assert not setup.directory.exists()


def test_permission_failure_occurs_before_secret_files_are_written(setup, monkeypatch):
    def fail(_):
        raise OSError("synthetic ACL failure")
    monkeypatch.setattr(setup.module, "_protect_directory", fail)
    with pytest.raises(GmailAccountError):
        connect(setup)
    assert not (setup.directory / "credentials.json").exists()


@pytest.mark.parametrize("role", ["child", "guest", "admin"])
def test_non_adult_cannot_start_browser_onboarding(setup, role):
    setup.hub.identity["role"] = role
    with pytest.raises(GmailAccountError):
        connect(setup)
    assert setup.flow.options is None and not setup.directory.exists()


def test_identity_revocation_during_browser_flow_prevents_storage(setup):
    original = setup.flow.run_local_server
    def revoked(**kwargs):
        result = original(**kwargs)
        setup.hub.identity["role"] = "guest"
        return result
    setup.flow.run_local_server = revoked
    with pytest.raises(GmailAccountError):
        connect(setup)
    assert not setup.directory.exists()


def test_existing_or_synced_directory_refused_before_browser(setup):
    setup.directory.mkdir()
    (setup.directory / "keep.txt").write_bytes(b"existing user data")
    with pytest.raises(GmailAccountError):
        connect(setup)
    assert setup.flow.options is None and (setup.directory / "keep.txt").read_bytes() == b"existing user data"
    setup.directory = setup.directory.parent / "OneDrive" / "account"
    setup.directory.parent.mkdir()
    with pytest.raises(GmailAccountError):
        connect(setup)
    assert setup.flow.options is None


def test_client_config_cannot_redirect_token_exchange_to_model_or_other_host(setup):
    config = json.loads(setup.client.read_text())
    config["installed"]["token_uri"] = "https://attacker.invalid/token"
    setup.client.write_text(json.dumps(config))
    with pytest.raises(GmailAccountError):
        connect(setup)
    assert setup.flow.options is None


def test_runtime_requires_exact_principal_and_reports_only_configured_state(setup):
    result = connect(setup)
    account = GmailAccount(result["account_config"], setup.hub.identity, clock=lambda: NOW)
    assert account.status()["status"] == "configured" and account.sender_email == "owner@gmail.com"
    assert account.access_token() == TOKEN
    with pytest.raises(GmailAccountError):
        GmailAccount(result["account_config"], {"principal_id": "other", "role": "adult"}, clock=lambda: NOW)
    with pytest.raises(GmailAccountError):
        GmailAccount(result["account_config"], {"principal_id": "owner", "role": "guest"}, clock=lambda: NOW)


def test_refresh_rotates_tokens_atomically_and_preserves_google_account_identity(setup):
    result = connect(setup)
    calls = []
    def refresh(payload, *, timeout):
        calls.append((deepcopy(payload), timeout))
        return {"access_token": "rotated-access-private", "refresh_token": "rotated-refresh-private",
                "expires_in": 3600, "token_type": "Bearer", "scope": [SEND, "openid", "email"], "id_token": "new-id-token"}
    def verifier(token, audience):
        assert token == "new-id-token" and audience == CLIENT_ID
        return {**setup.claims, "iat": NOW + 3601, "exp": NOW + 7201}
    account = GmailAccount(result["account_config"], setup.hub.identity, refresh_transport=refresh,
                           id_token_verifier=verifier, clock=lambda: NOW + 3601)
    assert account.access_token() == "rotated-access-private"
    assert account.access_token() == "rotated-access-private" and len(calls) == 1
    assert calls[0][0]["refresh_token"] == REFRESH and calls[0][1] == 5
    credentials = json.loads((setup.directory / "credentials.json").read_text())
    assert credentials["refresh_token"] == "rotated-refresh-private"
    assert set(path.name for path in setup.directory.iterdir()) == {"account.json", "credentials.json", ".refresh.lock"}


@pytest.mark.parametrize("change", [{"email": "other@gmail.com"}, {"sub": "other-subject"}, {"email_verified": False}])
def test_refreshed_identity_cannot_change_account_or_sender(setup, change):
    result = connect(setup)
    before = (setup.directory / "credentials.json").read_bytes()
    account = GmailAccount(result["account_config"], setup.hub.identity,
        refresh_transport=lambda *_args, **_kwargs: {"access_token": "new-private-token", "expires_in": 3600,
                                                     "token_type": "Bearer", "id_token": "new-id-token"},
        id_token_verifier=lambda *_: {**setup.claims, "iat": NOW + 3601, "exp": NOW + 7201, **change}, clock=lambda: NOW + 3601)
    with pytest.raises(GmailAccountError):
        account.access_token()
    assert (setup.directory / "credentials.json").read_bytes() == before


def test_insufficient_refresh_scope_fails_closed_without_rotation(setup):
    result = connect(setup)
    before = (setup.directory / "credentials.json").read_bytes()
    account = GmailAccount(result["account_config"], setup.hub.identity,
        refresh_transport=lambda *_args, **_kwargs: {"access_token": "new-private-token", "expires_in": 3600,
                                                     "token_type": "Bearer", "scope": ["openid"]}, clock=lambda: NOW + 3601)
    with pytest.raises(GmailAccountError):
        account.access_token()
    assert (setup.directory / "credentials.json").read_bytes() == before


def test_runtime_refresh_failure_is_safe_and_never_starts_browser(setup):
    result = connect(setup)
    def fail(*_args, **_kwargs):
        raise TimeoutError("PRIVATE_REFRESH_TOKEN " + REFRESH)
    account = GmailAccount(result["account_config"], setup.hub.identity, refresh_transport=fail, clock=lambda: NOW + 3601)
    with pytest.raises(GmailAccountError) as caught:
        account.access_token()
    assert REFRESH not in str(caught.value) and "PRIVATE" not in str(caught.value)


def test_factory_is_optional_and_never_opens_authentication(setup, monkeypatch):
    assert gmail_from_env(setup.hub.identity) is None
    result = connect(setup)
    monkeypatch.setenv("EDITH_GMAIL_ACCOUNT_CONFIG", result["account_config"])
    adapter = gmail_from_env(setup.hub.identity)
    assert adapter._sender_email == "owner@gmail.com"


def test_existing_store_rejects_extra_files_and_invalid_credentials_endpoint(setup):
    result = connect(setup)
    credentials = json.loads((setup.directory / "credentials.json").read_text())
    credentials["token_uri"] = "https://attacker.invalid/token"
    (setup.directory / "credentials.json").write_text(json.dumps(credentials))
    with pytest.raises(GmailAccountError):
        GmailAccount(result["account_config"], setup.hub.identity, clock=lambda: NOW)


def test_default_oauth_flow_uses_pkce_fixed_endpoints_and_no_environment_proxy(setup):
    configuration = json.loads(setup.client.read_text())
    flow = setup.module._make_flow(configuration, [SEND, "openid", "email"])
    url, _ = flow.authorization_url(nonce="synthetic-nonce")
    query = parse_qs(urlsplit(url).query)
    assert urlsplit(url).hostname == "accounts.google.com"
    assert query["code_challenge_method"] == ["S256"] and query["code_challenge"][0]
    assert flow.oauth2session.trust_env is False
    with pytest.raises(GmailAccountError):
        flow.oauth2session.request("POST", "https://attacker.invalid/token", data={"secret": "synthetic"})


def test_default_refresh_transport_is_fixed_bounded_and_refuses_redirects(setup, monkeypatch):
    import requests
    calls = []
    def request(session, method, url, **kwargs):
        calls.append((method, url, kwargs))
        assert session.trust_env is False
        assert session.get_adapter(url).max_retries.total == 0
        response = requests.Response()
        response.status_code = 302
        response._content = b"never follow redirect"
        response._content_consumed = True
        response.headers["Location"] = "https://attacker.invalid/token"
        return response
    monkeypatch.setattr(requests.Session, "request", request)
    with pytest.raises(GmailAccountError):
        setup.module._refresh_tokens({"grant_type": "refresh_token", "refresh_token": REFRESH}, timeout=5)
    assert len(calls) == 1
    assert calls[0][0:2] == ("POST", TOKEN_ENDPOINT)
    assert calls[0][2]["allow_redirects"] is False and calls[0][2]["timeout"] == 5 and calls[0][2]["stream"] is True


def test_separate_account_instances_cannot_refresh_same_store_concurrently(setup):
    result = connect(setup)
    entered, release = threading.Event(), threading.Event()
    calls, outcome = [], []
    def refresh(*_args, **_kwargs):
        calls.append("refresh")
        entered.set()
        assert release.wait(3)
        return {"access_token": "rotated-access-private", "expires_in": 3600, "token_type": "Bearer"}
    first = GmailAccount(result["account_config"], setup.hub.identity, refresh_transport=refresh, clock=lambda: NOW + 3601)
    second = GmailAccount(result["account_config"], setup.hub.identity, refresh_transport=refresh, clock=lambda: NOW + 3601)
    thread = threading.Thread(target=lambda: outcome.append(first.access_token()))
    thread.start()
    try:
        assert entered.wait(2)
        with pytest.raises(GmailAccountError):
            second.access_token()
    finally:
        release.set()
        thread.join(3)
    assert calls == ["refresh"] and outcome == ["rotated-access-private"]


def test_failed_rotation_keeps_original_token_file_and_returns_no_bearer(setup, monkeypatch):
    result = connect(setup)
    original = (setup.directory / "credentials.json").read_bytes()
    account = GmailAccount(result["account_config"], setup.hub.identity,
        refresh_transport=lambda *_args, **_kwargs: {"access_token": "rotated-access-private", "refresh_token": "rotated-refresh-private",
                                                     "expires_in": 3600, "token_type": "Bearer"}, clock=lambda: NOW + 3601)
    def failed_replace(*_args):
        raise OSError("synthetic failure")
    monkeypatch.setattr(setup.module.os, "replace", failed_replace)
    with pytest.raises(GmailAccountError):
        account.access_token()
    assert (setup.directory / "credentials.json").read_bytes() == original
    assert set(path.name for path in setup.directory.iterdir()) == {"account.json", "credentials.json", ".refresh.lock"}


@pytest.mark.skipif(os.name != "nt", reason="Native Windows protected store check")
def test_native_protected_account_store_can_be_reopened_and_rejects_broad_token_acl(setup, monkeypatch):
    from connectors.workspace import _verify_permissions
    from tools.hub_admin import _restrict_directory
    from tests.test_workspace import change_synthetic_acl
    monkeypatch.setattr(setup.module, "_protect_directory", _restrict_directory)
    monkeypatch.setattr(setup.module, "_verify_permissions", _verify_permissions)
    result = connect(setup)
    account = GmailAccount(result["account_config"], setup.hub.identity, clock=lambda: NOW)
    assert account.access_token() == TOKEN
    change_synthetic_acl(setup.directory / "credentials.json")
    with pytest.raises(GmailAccountError):
        GmailAccount(result["account_config"], setup.hub.identity, clock=lambda: NOW)


def test_unexpected_store_file_is_rejected_before_any_credential_read(setup, monkeypatch):
    result = connect(setup)
    (setup.directory / "unexpected.txt").write_bytes(b"unexpected")
    monkeypatch.setattr(setup.module, "_read_json", lambda *_: pytest.fail("Unexpected store reached credential read"))
    with pytest.raises(GmailAccountError):
        GmailAccount(result["account_config"], setup.hub.identity, clock=lambda: NOW)


def test_partial_secret_write_failure_removes_only_attempt_owned_artifact(setup, monkeypatch):
    def fail_fsync(_):
        raise OSError("synthetic fsync failure after secret bytes were written")
    monkeypatch.setattr(setup.module.os, "fsync", fail_fsync)
    with pytest.raises(GmailAccountError):
        connect(setup)
    assert not (setup.directory / "credentials.json").exists()
    assert not setup.directory.exists()


def test_permission_change_during_refresh_prevents_rotation_and_bearer_return(setup, monkeypatch):
    result = connect(setup)
    original = (setup.directory / "credentials.json").read_bytes()
    def no_longer_private(_):
        raise setup.module.WorkspaceError("synthetic widened ACL")
    def refresh(*_args, **_kwargs):
        monkeypatch.setattr(setup.module, "_verify_permissions", no_longer_private)
        return {"access_token": "rotated-access-private", "expires_in": 3600, "token_type": "Bearer"}
    account = GmailAccount(result["account_config"], setup.hub.identity, refresh_transport=refresh, clock=lambda: NOW + 3601)
    with pytest.raises(GmailAccountError):
        account.access_token()
    assert (setup.directory / "credentials.json").read_bytes() == original


def test_account_metadata_change_during_refresh_prevents_rotation(setup):
    result = connect(setup)
    original = (setup.directory / "credentials.json").read_bytes()
    def refresh(*_args, **_kwargs):
        path = setup.directory / "account.json"
        metadata = json.loads(path.read_text())
        metadata["subject"] = "different-subject"
        path.write_text(json.dumps(metadata))
        return {"access_token": "rotated-access-private", "expires_in": 3600, "token_type": "Bearer"}
    account = GmailAccount(result["account_config"], setup.hub.identity, refresh_transport=refresh, clock=lambda: NOW + 3601)
    with pytest.raises(GmailAccountError):
        account.access_token()
    assert (setup.directory / "credentials.json").read_bytes() == original


def test_rotation_permission_change_is_detected_before_returning_bearer(setup, monkeypatch):
    result = connect(setup)
    original_replace = setup.module.os.replace
    def altered_after_replace(source, target):
        original_replace(source, target)
        monkeypatch.setattr(setup.module, "_verify_permissions", lambda _: (_ for _ in ()).throw(setup.module.WorkspaceError("synthetic ACL change")))
    account = GmailAccount(result["account_config"], setup.hub.identity,
        refresh_transport=lambda *_args, **_kwargs: {"access_token": "rotated-access-private", "expires_in": 3600, "token_type": "Bearer"},
        clock=lambda: NOW + 3601)
    monkeypatch.setattr(setup.module.os, "replace", altered_after_replace)
    with pytest.raises(GmailAccountError):
        account.access_token()


def test_default_id_verifier_checks_real_signature_with_only_synthetic_certificate_transport(setup, monkeypatch):
    import base64
    from datetime import datetime, timedelta, timezone
    import requests
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from google.auth import crypt, jwt
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic.test")])
    current = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(private_key.public_key())
                   .serial_number(x509.random_serial_number()).not_valid_before(current - timedelta(days=1))
                   .not_valid_after(current + timedelta(days=1)).sign(private_key, hashes.SHA256()))
    key_pem = private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    cert_pem = certificate.public_bytes(serialization.Encoding.PEM).decode()
    signer = crypt.RSASigner.from_string(key_pem, key_id="synthetic-key")
    claims = {**setup.claims, "iat": int(time.time()), "exp": int(time.time()) + 3600}
    encoded = jwt.encode(signer, claims).decode()
    calls = []
    def certificates(session, method, url, **kwargs):
        calls.append((method, url))
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({"synthetic-key": cert_pem}).encode()
        response._content_consumed = True
        return response
    monkeypatch.setattr(requests.Session, "request", certificates)
    verified = setup.module._verify_id_token(encoded, CLIENT_ID)
    assert verified["sub"] == "google-subject-123" and verified["email"] == "owner@gmail.com"
    header, payload, signature = encoded.split(".")
    altered = bytearray(base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)))
    altered[0] ^= 1
    forged = ".".join([header, payload, base64.urlsafe_b64encode(altered).decode().rstrip("=")])
    with pytest.raises(ValueError):
        setup.module._verify_id_token(forged, CLIENT_ID)
    assert calls == [("GET", "https://www.googleapis.com/oauth2/v1/certs")] * 2


def test_default_id_verifier_rejects_jwk_response_without_secondary_fetch(setup, monkeypatch):
    import requests
    calls = []
    def certificates(session, method, url, **kwargs):
        calls.append(url)
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"keys":[]}'
        response._content_consumed = True
        return response
    monkeypatch.setattr(requests.Session, "request", certificates)
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: pytest.fail("Unexpected secondary certificate request"))
    with pytest.raises(GmailAccountError):
        setup.module._verify_id_token("synthetic-token", CLIENT_ID)
    assert calls == ["https://www.googleapis.com/oauth2/v1/certs"]
