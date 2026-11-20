from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from connectors.whatsapp import WhatsAppResponse
from connectors.whatsapp_account import BusinessAccount
from connectors.whatsapp_connection import ConnectionError, WhatsAppConnection, configure_connection, whatsapp_connection_from_env, sync_template


NOW = 1_800_000_000
TOKEN = "synthetic-private-meta-token"
WORKER_TOKEN = "synthetic-private-worker-token-12345"
IDENTITY = {"principal_id": "owner", "role": "adult"}
PHONE = {"id": "12345", "display_phone_number": "+1 650-555-1234", "verified_name": "Example Shop"}


class Hub:
    url = "http://127.0.0.1:8788"
    def __init__(self): self.identity, self.calls, self.hook = deepcopy(IDENTITY), [], None
    def capabilities(self):
        self.calls.append("identity")
        if self.hook: self.hook()
        return deepcopy(self.identity)


class Worker:
    url = Hub.url
    def __init__(self):
        self.scope = {"worker_id": "business-worker", "principal_id": "owner", "connection_id": "store-one", "waba_id": "23456", "phone_number_id": "12345",
                      "capabilities": {"binding_write": True, "permission_write": True, "message_claim": True, "message_result": True}}
        self.calls, self.hook = [], None
    def capabilities(self):
        self.calls.append("capabilities")
        if self.hook: self.hook()
        return deepcopy(self.scope)
    def register_binding(self, account, template):
        self.calls.append("register")
        stable = {key: value for key, value in account.items() if key != "verified_at"}
        version = hashlib.sha256(json.dumps({"account": stable, "template": template}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        return {"id": "binding-one", "version": version, "account": account, "template": template, "updated_at": NOW}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    import connectors.whatsapp_connection as module
    monkeypatch.setattr(module, "_verify_permissions", lambda _: None, raising=False)
    monkeypatch.setattr(module, "_protect_directory", lambda _: None, raising=False)
    monkeypatch.delenv("EDITH_WHATSAPP_CONNECTION_CONFIG", raising=False)
    hub, worker, network = Hub(), Worker(), []
    token_file, worker_file = tmp_path / "selected-meta.json", tmp_path / "selected-worker.json"
    token_file.write_text(json.dumps({"access_token": TOKEN}))
    worker_file.write_text(json.dumps({"url": Hub.url, "token": WORKER_TOKEN}))
    def factory(url, token):
        assert url == Hub.url and token == WORKER_TOKEN
        return worker
    def account_factory(*args, **kwargs):
        def wire(request, **_):
            network.append(request)
            return WhatsAppResponse(200, json.dumps({"data": [PHONE]}).encode())
        return BusinessAccount(*args, **kwargs, transport=wire, clock=lambda: NOW)
    return SimpleNamespace(module=module, hub=hub, worker=worker, network=network, token=token_file, worker_file=worker_file,
                           directory=tmp_path / "connection", worker_factory=factory, account_factory=account_factory, monkeypatch=monkeypatch)


def configure(fixture):
    return configure_connection(fixture.hub, fixture.worker_file, fixture.token, fixture.directory,
                                connection_id="store-one", waba_id="23456", phone_number_id="12345",
                                worker_factory=fixture.worker_factory, account_factory=fixture.account_factory)


def load(fixture, identity=IDENTITY):
    return WhatsAppConnection(fixture.directory / "connection.json", fixture.hub, identity,
                              worker_factory=fixture.worker_factory, account_factory=fixture.account_factory)


def test_explicit_setup_verifies_sender_and_copies_only_selected_credentials(setup, capsys):
    result = configure(setup)
    assert result["status"] == "configured" and result["sending_enabled"] is False and result["principal_id"] == "owner"
    assert {p.name for p in setup.directory.iterdir()} == {"connection.json", "meta-token.json", "worker-client.json"}
    assert json.loads((setup.directory / "meta-token.json").read_text()) == {"access_token": TOKEN}
    assert json.loads((setup.directory / "worker-client.json").read_text()) == {"url": Hub.url, "token": WORKER_TOKEN}
    assert len(setup.network) == 1 and setup.network[0].get_method() == "GET"
    assert "register" not in setup.worker.calls
    assert TOKEN not in str(result) + capsys.readouterr().out and WORKER_TOKEN not in str(result)


def test_constructor_and_factory_are_local_only_and_no_config_returns_none(setup):
    assert whatsapp_connection_from_env(setup.hub, IDENTITY) is None
    configure(setup)
    counts = (len(setup.network), len(setup.hub.calls), len(setup.worker.calls))
    connection = load(setup)
    assert connection.principal_id == "owner" and connection.connection_id == "store-one"
    assert counts == (len(setup.network), len(setup.hub.calls), len(setup.worker.calls))
    assert connection.account is not None and connection.catalog is not None and connection.worker is setup.worker


def test_revalidate_checks_current_paired_and_worker_scope_without_meta_read(setup):
    configure(setup)
    connection = load(setup)
    setup.network.clear()
    snapshot = connection.revalidate()
    assert snapshot["display_phone_number"] == "+16505551234" and snapshot["connection_id"] == "store-one"
    assert setup.network == [] and TOKEN not in str(snapshot) and WORKER_TOKEN not in str(snapshot)
    snapshot["phone_number_id"] = "changed"
    assert connection.revalidate()["phone_number_id"] == "12345"


@pytest.mark.parametrize("field,value", [("principal_id", "other"), ("connection_id", "other"), ("waba_id", "999"), ("phone_number_id", "999"), ("capabilities", {"message_claim": True})])
def test_worker_scope_mismatch_blocks_setup_before_provider_or_file_write(setup, field, value):
    setup.worker.scope[field] = value
    with pytest.raises(ConnectionError): configure(setup)
    assert setup.network == [] and not setup.directory.exists()


@pytest.mark.parametrize("identity", [{"principal_id": "owner", "role": "guest"}, {"principal_id": "other", "role": "adult"}])
def test_wrong_principal_or_role_cannot_load_connection(setup, identity):
    configure(setup)
    with pytest.raises(ConnectionError): load(setup, identity)


def test_permission_revocation_during_metadata_lookup_prevents_local_setup(setup):
    original = setup.account_factory
    def factory(*args, **kwargs):
        account = original(*args, **kwargs)
        snapshot = account.snapshot
        def revoked():
            value = snapshot()
            setup.hub.identity["role"] = "guest"
            return value
        account.snapshot = revoked
        return account
    setup.account_factory = factory
    with pytest.raises(ConnectionError): configure(setup)
    assert not setup.directory.exists()


@pytest.mark.parametrize("file", ["meta-token.json", "worker-client.json", "connection.json"])
def test_changed_config_or_credential_requires_explicit_new_store(setup, file):
    configure(setup)
    connection = load(setup)
    path = setup.directory / file
    data = json.loads(path.read_text())
    if file == "meta-token.json": data["access_token"] = "changed-secret"
    elif file == "worker-client.json": data["token"] = "changed-worker-secret-12345678901234"
    else: data["account_snapshot"]["verified_name"] = "Changed Store"
    path.write_text(json.dumps(data))
    with pytest.raises(ConnectionError): connection.revalidate()
    assert len(setup.network) == 1


def test_config_change_during_worker_identity_lookup_is_not_returned_as_valid(setup):
    configure(setup)
    connection = load(setup)
    def change():
        (setup.directory / "meta-token.json").write_text(json.dumps({"access_token": "changed-secret"}))
    setup.worker.hook = change
    with pytest.raises(ConnectionError): connection.revalidate()


def test_existing_store_is_never_overwritten(setup):
    configure(setup)
    before = (setup.directory / "connection.json").read_bytes()
    with pytest.raises(ConnectionError): configure(setup)
    assert (setup.directory / "connection.json").read_bytes() == before


@pytest.mark.parametrize("change", ["extra", "hardlink", "duplicate", "oversize"])
def test_unexpected_or_unsafe_private_store_artifacts_are_refused(setup, change):
    configure(setup)
    path = setup.directory / "meta-token.json"
    if change == "extra": (setup.directory / "unexpected.txt").write_text("fixture")
    elif change == "hardlink": os.link(path, setup.directory.parent / "hardlink")
    elif change == "duplicate": path.write_text('{"access_token":"a","access_token":"b"}')
    else: path.write_text(" " * 16385)
    with pytest.raises(ConnectionError): load(setup)


def test_token_copy_failure_cleans_only_this_attempts_files(setup, monkeypatch):
    monkeypatch.setattr(setup.module.os, "fsync", lambda _: (_ for _ in ()).throw(OSError("private raw failure")), raising=False)
    with pytest.raises(ConnectionError) as error: configure(setup)
    assert not setup.directory.exists() and "private raw" not in str(error.value)


def test_sender_is_default_deny_without_recipient_policy_and_no_provider_send(setup):
    configure(setup)
    sender = load(setup).sender(None)
    result = sender.send_template("+16505550000", {}, can_execute=lambda: True)
    assert result["status"] == "failed" and len(setup.network) == 1


def template_connection(setup, *, phone=None):
    configure(setup)
    connection = load(setup)
    template = {"id": "34567", "name": "order_update", "language": "en_US", "status": "APPROVED", "category": "UTILITY",
                "parameter_format": "POSITIONAL", "components": [{"type": "BODY", "text": "Hello {{1}}, your order is ready."}]}
    def transport(request, **_):
        setup.network.append(request)
        rows = [phone or PHONE] if "/phone_numbers?" in request.full_url else [template]
        return WhatsAppResponse(200, json.dumps({"data": rows}).encode())
    connection.account._transport = transport
    return connection


def test_explicit_template_sync_registers_exact_verified_snapshot_without_send(setup):
    connection = template_connection(setup)
    result = sync_template(connection, "34567", "order_update", "en_US")
    assert result["id"] == "binding-one" and result["account"]["version"] == connection.account_snapshot["version"]
    assert result["template"]["body"] == "Hello {{1}}, your order is ready."
    assert setup.worker.calls.count("register") == 1
    assert all(request.get_method() == "GET" for request in setup.network)
    assert TOKEN not in str(result) and WORKER_TOKEN not in str(result)


def test_template_sync_refuses_provider_account_change_without_registering(setup):
    connection = template_connection(setup, phone={**PHONE, "verified_name": "Different business"})
    with pytest.raises(ConnectionError): sync_template(connection, "34567", "order_update", "en_US")
    assert "register" not in setup.worker.calls


def test_uncertain_registry_write_is_not_retried_or_reported_as_synchronized(setup):
    connection = template_connection(setup)
    calls = []
    def register(*_):
        calls.append(1)
        raise TimeoutError(TOKEN)
    connection.worker.register_binding = register
    with pytest.raises(ConnectionError) as error: sync_template(connection, "34567", "order_update", "en_US")
    assert calls == [1] and TOKEN not in str(error.value)


def test_template_sync_rejects_forged_registry_receipt(setup):
    connection = template_connection(setup)
    original = connection.worker.register_binding
    def register(*args):
        result = original(*args)
        result["version"] = "b" * 64
        return result
    connection.worker.register_binding = register
    with pytest.raises(ConnectionError): sync_template(connection, "34567", "order_update", "en_US")
    assert setup.worker.calls.count("register") == 1


def test_different_actual_worker_hub_is_refused_before_metadata_lookup(setup):
    setup.worker.url = "http://127.0.0.1:9999"
    with pytest.raises(ConnectionError): configure(setup)
    assert setup.network == [] and not setup.directory.exists()


def test_worker_authority_change_at_runtime_is_refused(setup):
    configure(setup)
    connection = load(setup)
    connection.worker.url = "http://127.0.0.1:9999"
    with pytest.raises(ConnectionError): connection.revalidate()


def test_worker_url_normalizes_only_supported_trailing_slash(setup):
    setup.worker_file.write_text(json.dumps({"url": Hub.url + "/", "token": WORKER_TOKEN}))
    assert configure(setup)["status"] == "configured"
    assert json.loads((setup.directory / "worker-client.json").read_text())["url"] == Hub.url


def test_file_replacement_during_final_batch_acl_check_is_rejected(setup, monkeypatch):
    configure(setup)
    calls = []
    def verify(paths):
        calls.append(paths)
        if len(calls) == 2:
            replacement = setup.directory.parent / "replacement.json"
            replacement.write_bytes((setup.directory / "meta-token.json").read_bytes())
            os.replace(replacement, setup.directory / "meta-token.json")
    monkeypatch.setattr(setup.module, "_verify_permissions", verify)
    with pytest.raises(ConnectionError): load(setup)
    assert len(calls) == 2 and all(len(paths) == 4 for paths in calls)


def test_extra_artifact_created_during_final_acl_check_is_rejected(setup, monkeypatch):
    configure(setup)
    calls = []
    def verify(paths):
        calls.append(paths)
        if len(calls) == 2: (setup.directory / "unexpected.txt").write_text("fixture")
    monkeypatch.setattr(setup.module, "_verify_permissions", verify)
    with pytest.raises(ConnectionError): setup.module._store(setup.directory / "connection.json")


def test_worker_authority_change_during_blocking_scope_check_is_rejected(setup):
    configure(setup)
    connection = load(setup)
    setup.worker.hook = lambda: setattr(setup.worker, "url", "http://127.0.0.1:9999")
    with pytest.raises(ConnectionError): connection.revalidate()


def test_selected_source_change_during_last_scope_check_prevents_setup(setup):
    def mutate():
        if setup.worker.calls.count("capabilities") == 2:
            setup.token.write_text(json.dumps({"access_token": "different-private-token"}))
    setup.worker.hook = mutate
    with pytest.raises(ConnectionError): configure(setup)
    assert not setup.directory.exists()


def test_native_acl_setup_reopen_and_broadened_artifact_refusal(tmp_path):
    import subprocess
    import connectors.whatsapp_connection as module
    selected = tmp_path / "private-inputs"
    selected.mkdir()
    module._protect_directory(selected)
    meta, worker_file = selected / "meta.json", selected / "worker.json"
    meta.write_text(json.dumps({"access_token": TOKEN}))
    worker_file.write_text(json.dumps({"url": Hub.url, "token": WORKER_TOKEN}))
    if os.name != "nt":
        meta.chmod(0o600)
        worker_file.chmod(0o600)
    hub, worker, requests = Hub(), Worker(), []
    def account_factory(*args, **kwargs):
        def transport(request, **_):
            requests.append(request)
            return WhatsAppResponse(200, json.dumps({"data": [PHONE]}).encode())
        return BusinessAccount(*args, **kwargs, transport=transport, clock=lambda: NOW)
    fixture = SimpleNamespace(hub=hub, worker_file=worker_file, token=meta, directory=tmp_path / "native-store",
                              worker_factory=lambda *args: worker, account_factory=account_factory)
    configure(fixture)
    connection = load(fixture)
    assert connection.revalidate()["principal_id"] == "owner" and len(requests) == 1
    token_path = fixture.directory / "meta-token.json"
    if os.name == "nt":
        result = subprocess.run(["icacls.exe", str(token_path), "/grant", "*S-1-1-0:(R)"], capture_output=True,
                                timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert result.returncode == 0
    else:
        token_path.chmod(0o644)
    with pytest.raises(ConnectionError): connection.revalidate()
    assert len(requests) == 1
