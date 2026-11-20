from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from tools import configure_whatsapp_connection as configure_cli, sync_whatsapp_template as sync_cli, record_whatsapp_permission as permission_cli


NOW = 1_800_000_000
CONTACT_VERSION = "c" * 64
ACCOUNT_VERSION = "a" * 64
PERMISSION_VERSION = "d" * 64
SNAPSHOT = {"principal_id": "owner", "connection_id": "store-one", "waba_id": "23456", "phone_number_id": "12345",
            "display_phone_number": "+16505551234", "verified_name": "Example Shop", "version": ACCOUNT_VERSION, "verified_at": NOW}


class Worker:
    def __init__(self): self.requests, self.error = [], None
    def set_permission(self, payload):
        self.requests.append(deepcopy(payload))
        if self.error: raise self.error
        return {**{key: value for key, value in payload.items() if key != "expected_version"}, "id": "permission-one",
                "version": "e" * 64 if payload["expected_version"] == PERMISSION_VERSION else PERMISSION_VERSION,
                "recipient_e164": "+16505550000", "updated_at": NOW}


class Connection:
    principal_id, connection_id = "owner", "store-one"
    def __init__(self): self.worker, self.calls, self.error = Worker(), [], None
    def revalidate(self):
        self.calls.append("revalidate")
        if self.error: raise self.error
        return deepcopy(SNAPSHOT)


class Hub:
    def __init__(self): self.phone, self.version, self.calls = "+16505550000", CONTACT_VERSION, []
    def contacts(self):
        self.calls.append("contacts")
        return {"contacts": [{"id": "contact-one", "version": self.version, "display_name": "Selected Person", "phone_e164": self.phone, "email": None}]}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from connectors import whatsapp_connection
    monkeypatch.setattr(whatsapp_connection, "_verify_permissions", lambda _: None)
    file = tmp_path / "permission-evidence.json"
    file.write_text(json.dumps({"expected_version": None, "status": "granted", "allowed_categories": ["UTILITY"],
                               "expires_at": NOW + 86400, "evidence_reference": "form-record-12", "source_note": "private evidence description"}))
    connection, hub, loads = Connection(), Hub(), []
    def loader(config, paired):
        loads.append((config, paired))
        return connection, hub
    return SimpleNamespace(file=file, connection=connection, hub=hub, loader=loader, loads=loads)


def permission_args(setup, confirm=False):
    args = ["--connection-config", "D:/Private/connection.json", "--paired-client-config", "D:/Private/paired.json",
            "--contact-id", "contact-one", "--contact-version", CONTACT_VERSION, "--evidence-file", str(setup.file)]
    return args + (["--confirm-record"] if confirm else [])


def test_setup_cli_uses_paired_identity_and_selected_files_never_token_arguments(capsys):
    calls = []
    hub = object()
    def configure(*args, **kwargs):
        calls.append((args, kwargs))
        return {"status": "configured", "sending_enabled": False, "principal_id": "owner", "connection_id": "store-one",
                "display_phone_number": "+16505551234", "config_path": "D:/Private/connection/connection.json"}
    args = ["--paired-client-config", "D:/Private/paired.json", "--worker-client-config", "D:/Private/worker.json",
            "--token-file", "D:/Private/meta-token.json", "--connection-dir", "D:/Private/connection",
            "--connection-id", "store-one", "--waba-id", "23456", "--phone-number-id", "12345"]
    assert configure_cli.main(args, connector=configure, paired_loader=lambda path: hub) == 0
    assert calls == [((hub, "D:/Private/worker.json", "D:/Private/meta-token.json", "D:/Private/connection"),
                      {"connection_id": "store-one", "waba_id": "23456", "phone_number_id": "12345"})]
    assert json.loads(capsys.readouterr().out)["sending_enabled"] is False


def test_setup_failures_are_static_and_do_not_echo_provider_secret(capsys):
    args = ["--paired-client-config", "D:/p", "--worker-client-config", "D:/w", "--token-file", "D:/t", "--connection-dir", "D:/new",
            "--connection-id", "one", "--waba-id", "1", "--phone-number-id", "2"]
    def fail(*args, **kwargs): raise RuntimeError("secret-meta-token")
    assert configure_cli.main(args, connector=fail, paired_loader=lambda _: object()) == 1
    output = capsys.readouterr()
    assert output.out == "" and "secret-meta-token" not in output.err


def test_template_sync_cli_prints_registry_identity_not_template_body(setup, capsys):
    calls = []
    def synchronize(connection, template_id, name, language):
        calls.append((connection, template_id, name, language))
        return {"id": "binding-one", "version": "b" * 64, "account": deepcopy(SNAPSHOT),
                "template": {"id": "34567", "name": "order_update", "language": "en_US", "body": "private template body"}, "updated_at": NOW}
    args = ["--connection-config", "D:/Private/connection.json", "--paired-client-config", "D:/Private/paired.json",
            "--template-id", "34567", "--template-name", "order_update", "--language", "en_US"]
    assert sync_cli.main(args, connection_loader=setup.loader, synchronizer=synchronize) == 0
    assert calls == [(setup.connection, "34567", "order_update", "en_US")]
    output = capsys.readouterr().out
    assert json.loads(output)["binding_id"] == "binding-one" and "private template body" not in output


def test_permission_command_defaults_to_review_without_grant_or_provider_call(setup, capsys):
    assert permission_cli.main(permission_args(setup), connection_loader=setup.loader, clock=lambda: NOW) == 0
    assert setup.connection.worker.requests == []
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "review_required" and output["review"]["recipient_e164"] == "+16505550000"
    assert output["review"]["allowed_categories"] == ["UTILITY"] and "private evidence" not in str(output)


def test_explicit_confirmation_records_exact_contact_version_and_owner_attested_evidence(setup, capsys):
    assert permission_cli.main(permission_args(setup, True), connection_loader=setup.loader, clock=lambda: NOW) == 0
    assert setup.connection.worker.requests == [{"connection_id": "store-one", "account_version": ACCOUNT_VERSION,
        "contact_id": "contact-one", "contact_version": CONTACT_VERSION, "expected_version": None,
        "status": "granted", "allowed_categories": ["UTILITY"], "recorded_at": NOW, "expires_at": NOW + 86400,
        "source": "explicit_operator_record", "evidence_reference": "form-record-12", "source_note": "private evidence description"}]
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2 and json.loads(lines[-1])["permission_version"] == PERMISSION_VERSION
    assert "private evidence description" not in "".join(lines)


def test_revocation_supports_empty_categories_and_zero_expiry(setup):
    data = json.loads(setup.file.read_text())
    data.update(expected_version=PERMISSION_VERSION, status="revoked", allowed_categories=[], expires_at=0)
    setup.file.write_text(json.dumps(data))
    assert permission_cli.main(permission_args(setup, True), connection_loader=setup.loader, clock=lambda: NOW) == 0
    assert setup.connection.worker.requests[0]["status"] == "revoked"


@pytest.mark.parametrize("change", ["contact_version", "missing_phone", "role_revoked", "expired", "duplicate_categories", "empty_categories", "fake_source", "extra_field"])
def test_permission_invalid_or_stale_selection_never_writes(setup, change):
    data = json.loads(setup.file.read_text())
    if change == "contact_version": setup.hub.version = "f" * 64
    elif change == "missing_phone": setup.hub.phone = None
    elif change == "role_revoked": setup.connection.error = RuntimeError("revoked")
    elif change == "expired": data["expires_at"] = NOW
    elif change == "duplicate_categories": data["allowed_categories"] = ["UTILITY", "UTILITY"]
    elif change == "empty_categories": data["allowed_categories"] = []
    elif change == "fake_source": data["source"] = "provider_verified"
    else: data["recipient_e164"] = "+16505559999"
    setup.file.write_text(json.dumps(data))
    assert permission_cli.main(permission_args(setup, True), connection_loader=setup.loader, clock=lambda: NOW) == 1
    assert setup.connection.worker.requests == []


def test_evidence_file_change_during_final_identity_check_prevents_write(setup):
    original = setup.connection.revalidate
    def changing():
        value = original()
        if len(setup.connection.calls) == 2:
            data = json.loads(setup.file.read_text())
            data["allowed_categories"] = ["MARKETING"]
            setup.file.write_text(json.dumps(data))
        return value
    setup.connection.revalidate = changing
    assert permission_cli.main(permission_args(setup, True), connection_loader=setup.loader, clock=lambda: NOW) == 1
    assert setup.connection.worker.requests == []


def test_ambiguous_permission_write_is_not_retried_or_reported_recorded(setup, capsys):
    setup.connection.worker.error = TimeoutError("secret-worker-token")
    assert permission_cli.main(permission_args(setup, True), connection_loader=setup.loader, clock=lambda: NOW) == 1
    assert len(setup.connection.worker.requests) == 1
    output = capsys.readouterr()
    assert "secret-worker-token" not in output.out + output.err and "permission_recorded" not in output.out


def test_forged_permission_receipt_is_not_reported_recorded(setup, capsys):
    original = setup.connection.worker.set_permission
    def forged(payload):
        result = original(payload)
        result["recipient_e164"] = "+16505559999"
        return result
    setup.connection.worker.set_permission = forged
    assert permission_cli.main(permission_args(setup, True), connection_loader=setup.loader, clock=lambda: NOW) == 1
    assert "permission_recorded" not in capsys.readouterr().out


@pytest.mark.parametrize("module", [configure_cli, sync_cli, permission_cli])
def test_cli_parser_does_not_echo_accidental_secret_argv(module, capsys):
    with pytest.raises(SystemExit) as error: module.main(["--unexpected-token", "secret-bearer-value"])
    assert error.value.code == 2 and "secret-bearer-value" not in capsys.readouterr().err


def test_categories_follow_server_canonical_order_before_review_and_write(setup, capsys):
    data = json.loads(setup.file.read_text())
    data["allowed_categories"] = ["MARKETING", "UTILITY"]
    setup.file.write_text(json.dumps(data))
    original = setup.connection.worker.set_permission
    def canonical(payload):
        result = original(payload)
        result["allowed_categories"] = ["UTILITY", "MARKETING"]
        return result
    setup.connection.worker.set_permission = canonical
    assert permission_cli.main(permission_args(setup, True), connection_loader=setup.loader, clock=lambda: NOW) == 0
    assert setup.connection.worker.requests[0]["allowed_categories"] == ["UTILITY", "MARKETING"]
    assert json.loads(capsys.readouterr().out.splitlines()[0])["review"]["allowed_categories"] == ["UTILITY", "MARKETING"]


def test_evidence_change_during_final_contact_lookup_prevents_write(setup):
    original = setup.hub.contacts
    def changed():
        result = original()
        if len(setup.hub.calls) == 2:
            data = json.loads(setup.file.read_text())
            data["source_note"] = "changed private evidence"
            setup.file.write_text(json.dumps(data))
        return result
    setup.hub.contacts = changed
    assert permission_cli.main(permission_args(setup, True), connection_loader=setup.loader, clock=lambda: NOW) == 1
    assert setup.connection.worker.requests == []


def test_paired_loader_binds_constructed_client_to_exact_verified_credentials(setup, monkeypatch):
    path = setup.file.parent / "paired.json"
    path.write_text(json.dumps({"url": "http://127.0.0.1:8788", "token": "verified-owner-credential-1234567890"}))
    changed_token = "transient-unverified-credential-123456"
    monkeypatch.setattr(configure_cli, "_load_client", lambda _: (SimpleNamespace(url="http://127.0.0.1:8788"), changed_token))
    from connectors.whatsapp_connection import ConnectionError
    with pytest.raises(ConnectionError): configure_cli._load_paired(path)


def test_setup_cli_omits_caller_path_that_may_contain_sensitive_text(capsys):
    args = ["--paired-client-config", "D:/p", "--worker-client-config", "D:/w", "--token-file", "D:/t", "--connection-dir", "D:/new",
            "--connection-id", "one", "--waba-id", "1", "--phone-number-id", "2"]
    def connector(*args, **kwargs):
        return {"status": "configured", "sending_enabled": False, "principal_id": "owner", "connection_id": "one",
                "display_phone_number": "+16505550000", "config_path": "D:/private/accidental-secret-text/connection.json"}
    assert configure_cli.main(args, connector=connector, paired_loader=lambda _: object()) == 0
    assert "accidental-secret-text" not in capsys.readouterr().out
