"""Private per-owner WhatsApp worker connection; construction never contacts providers.

Explicit setup verifies Meta metadata but creates no remote accounts, permission
records or messages. Sync and permission administration are separate trusted CLI
operations. Runtime configuration is not proof of billing readiness or live access.
Tokens are plaintext protected by OS ACLs, not encrypted against the same OS user.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re

from brain.hub import BusinessWorkerClient, HubClient
from connectors.merchant_account import _directory as _private_directory
from connectors.whatsapp import GRAPH_API_VERSION
from connectors.whatsapp_account import BusinessAccount, _ACCOUNT_FIELDS
from connectors.whatsapp_templates import TemplateCatalog, TemplateSender, _template
from connectors.workspace import _entry, _read_file, _verify_permissions
from tools.hub_admin import _restrict_directory as _protect_directory


_LOCAL_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_REMOTE_ID = re.compile(r"[1-9][0-9]{0,31}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_FILES = {"connection.json", "meta-token.json", "worker-client.json"}
_CONFIG_FIELDS = {"schema_version", "principal_id", "connection_id", "graph_api_version", "account_snapshot", "token_file", "worker_client_file"}
_WORKER_CAPABILITIES = {"binding_write": True, "permission_write": True, "message_claim": True, "message_result": True}


class ConnectionError(RuntimeError):
    """Safe connection failure; never includes tokens, provider bodies or raw errors."""


def _identity(value):
    if (not isinstance(value, dict) or value.get("role") not in ("adult", "elder")
            or not isinstance(value.get("principal_id"), str) or not _LOCAL_ID.fullmatch(value["principal_id"])):
        raise ConnectionError("A currently paired adult or elder identity is required.")
    return {"principal_id": value["principal_id"], "role": value["role"]}


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value: raise ConnectionError("Duplicate private configuration fields.")
        value[key] = item
    return value


def _same_file(before, after):
    # Legacy ctime differs between Windows fstat and lstat; use stable identity/mtime.
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_mode", "st_nlink")
    return after.st_nlink == 1 and all(getattr(before, field) == getattr(after, field) for field in fields)


def read_private_json(path, limit=16_384):
    """Read one explicitly selected bounded private file; no discovery or ACL repair."""
    try:
        selected = Path(path)
        if not selected.is_absolute(): raise ValueError("path")
        directory = _private_directory(selected.parent)
        selected = directory / selected.name
        _verify_permissions([directory, selected])
        raw, before = _read_file(selected, limit)
        _verify_permissions([directory, selected])
        if _private_directory(selected.parent) != directory or not _same_file(before, _entry(selected)):
            raise ValueError("changed file")
        value = json.loads(raw, object_pairs_hook=_unique)
        if not isinstance(value, dict): raise ValueError("object")
        return value
    except Exception:
        raise ConnectionError("Select a bounded private JSON file outside synchronized folders and source repositories.") from None


def _token(value):
    if (not isinstance(value, dict) or set(value) != {"access_token"} or not isinstance(value["access_token"], str)
            or not 1 <= len(value["access_token"]) <= 8192 or not all(33 <= ord(c) <= 126 for c in value["access_token"])):
        raise ConnectionError("The selected Meta token JSON is invalid.")
    return value["access_token"]


def _worker_config(value, paired_hub):
    if (not isinstance(value, dict) or set(value) != {"url", "token"}
            or not isinstance(value.get("token"), str) or not 32 <= len(value["token"]) <= 256
            or not all(33 <= ord(c) <= 126 for c in value["token"])):
        raise ConnectionError("The selected worker credential must belong to this same local hub.")
    normalized = HubClient(value["url"], value["token"]).url  # Validation/construction only; no network.
    if normalized != paired_hub.url:
        raise ConnectionError("The paired and worker clients must use the same exact local hub.")
    return {"url": normalized, "token": value["token"]}


def _same_hub(worker, paired_hub, expected_url):
    if worker.url != expected_url or paired_hub.url != expected_url:
        raise ConnectionError("The worker and paired hub authority changed.")


def _scope(worker, identity, connection_id, waba_id, phone_id):
    value = worker.capabilities()
    if (not isinstance(value, dict) or set(value) != {"worker_id", "principal_id", "connection_id", "waba_id", "phone_number_id", "capabilities"}
            or not isinstance(value["worker_id"], str) or not _LOCAL_ID.fullmatch(value["worker_id"])
            or value["principal_id"] != identity["principal_id"] or value["connection_id"] != connection_id
            or value["waba_id"] != waba_id or value["phone_number_id"] != phone_id
            or not isinstance(value["capabilities"], dict) or set(value["capabilities"]) != set(_WORKER_CAPABILITIES)
            or any(value["capabilities"][key] is not True for key in _WORKER_CAPABILITIES)):
        raise ConnectionError("A dedicated worker credential for this exact owner and business sender is required.")
    return value


def _snapshot(value, principal, connection_id):
    if (not isinstance(value, dict) or set(value) != _ACCOUNT_FIELDS or value["principal_id"] != principal
            or value["connection_id"] != connection_id or any(not isinstance(value[key], str) or not _REMOTE_ID.fullmatch(value[key]) for key in ("waba_id", "phone_number_id"))
            or not isinstance(value["version"], str) or not _HASH.fullmatch(value["version"])
            or type(value["verified_at"]) is not int or value["verified_at"] < 0
            or not isinstance(value["display_phone_number"], str) or not re.fullmatch(r"\+[1-9][0-9]{1,14}", value["display_phone_number"])
            or not isinstance(value["verified_name"], str) or not value["verified_name"].strip() or len(value["verified_name"]) > 256
            or any(ord(c) < 32 or ord(c) == 127 for c in value["verified_name"])):
        raise ConnectionError("The local verified account snapshot is invalid.")
    value["verified_name"].encode("utf-8")
    return deepcopy(value)


def _inventory(directory):
    paths = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if len(paths) >= 3 or entry.name not in _FILES: raise ConnectionError("Unexpected connection-store artifact.")
            paths.append(directory / entry.name)
    if {p.name for p in paths} != _FILES: raise ConnectionError("The connection store is incomplete.")
    return paths


def _store(config_path):
    path = Path(config_path)
    if not path.is_absolute() or path.name != "connection.json": raise ConnectionError("Select the absolute connection.json path.")
    directory = _private_directory(path.parent)
    paths = _inventory(directory)
    _verify_permissions([directory, *paths])
    loaded, metadata = {}, {}
    for selected in paths:
        raw, metadata[selected] = _read_file(selected, 16_384)
        loaded[selected.name] = json.loads(raw, object_pairs_hook=_unique)
    _verify_permissions([directory, *paths])
    if (_private_directory(path.parent) != directory or set(_inventory(directory)) != set(paths)
            or any(not _same_file(before, _entry(selected)) for selected, before in metadata.items())):
        raise ConnectionError("Connection files changed during verification.")
    return loaded


class WhatsAppConnection:
    def __init__(self, config_path, paired_hub, identity, *, worker_factory=None, account_factory=None):
        try:
            self._identity = _identity(identity)
            self._paired_hub, self._path = paired_hub, Path(config_path)
            self._expected = _store(self._path)
            config = self._expected["connection.json"]
            if (not isinstance(config, dict) or set(config) != _CONFIG_FIELDS or type(config["schema_version"]) is not int or config["schema_version"] != 1
                    or config["principal_id"] != self._identity["principal_id"] or not isinstance(config["connection_id"], str)
                    or not _LOCAL_ID.fullmatch(config["connection_id"]) or config["graph_api_version"] != GRAPH_API_VERSION
                    or config["token_file"] != "meta-token.json" or config["worker_client_file"] != "worker-client.json"):
                raise ValueError("configuration")
            self._account_snapshot = _snapshot(config["account_snapshot"], config["principal_id"], config["connection_id"])
            credentials = _worker_config(self._expected["worker-client.json"], paired_hub)
            _token(self._expected["meta-token.json"])
            self.worker = (BusinessWorkerClient if worker_factory is None else worker_factory)(credentials["url"], credentials["token"])
            _same_hub(self.worker, paired_hub, credentials["url"])
            self.account = (BusinessAccount if account_factory is None else account_factory)(self.principal_id, self._account_snapshot["waba_id"],
                self._account_snapshot["phone_number_id"], self._credential, connection_id=self.connection_id)
            self.account.authorized_token(self._account_snapshot)  # Local cryptographic credential binding, no Meta request.
            self.catalog = TemplateCatalog(self.account)
        except Exception:
            raise ConnectionError("The selected private WhatsApp connection could not be verified for this principal.") from None

    @property
    def principal_id(self): return self._identity["principal_id"]

    @property
    def connection_id(self): return self._account_snapshot["connection_id"]

    @property
    def account_snapshot(self): return deepcopy(self._account_snapshot)

    def _credential(self):
        try:
            if _store(self._path) != self._expected: raise ValueError("changed store")
            return _token(self._expected["meta-token.json"])
        except Exception:
            raise ConnectionError("The connection or credential changed; configure and review it again explicitly.") from None

    def revalidate(self):
        """Check local integrity and fresh paired/worker authority; no Meta requests."""
        try:
            self.account.authorized_token(self._account_snapshot)
            _same_hub(self.worker, self._paired_hub, _worker_config(self._expected["worker-client.json"], self._paired_hub)["url"])
            if _identity(self._paired_hub.capabilities()) != self._identity: raise ValueError("identity changed")
            _scope(self.worker, self._identity, self.connection_id, self._account_snapshot["waba_id"], self._account_snapshot["phone_number_id"])
            self.account.authorized_token(self._account_snapshot)  # Changes during blocking hub reads must fail closed.
            _same_hub(self.worker, self._paired_hub, _worker_config(self._expected["worker-client.json"], self._paired_hub)["url"])
            return self.account_snapshot
        except Exception:
            raise ConnectionError("The connection, paired identity or dedicated worker authority changed or could not be verified.") from None

    def template_sender(self, recipient_policy):
        return TemplateSender(self.catalog, recipient_policy=recipient_policy)

    def sender(self, recipient_policy):
        return self.template_sender(recipient_policy)


def configure_connection(paired_hub, worker_client_config, token_file, connection_dir, *, connection_id,
                         waba_id, phone_number_id, worker_factory=None, account_factory=None):
    directory, created, owned = None, [], False
    try:
        if (not isinstance(connection_id, str) or not _LOCAL_ID.fullmatch(connection_id)
                or any(not isinstance(value, str) or not _REMOTE_ID.fullmatch(value) for value in (waba_id, phone_number_id))):
            raise ValueError("identifiers")
        directory = _private_directory(connection_dir)
        if directory.exists() or not directory.parent.is_dir(): raise ValueError("new directory required")
        identity = _identity(paired_hub.capabilities())
        worker_config = _worker_config(read_private_json(worker_client_config), paired_hub)
        token_config = read_private_json(token_file)
        _token(token_config)
        worker = (BusinessWorkerClient if worker_factory is None else worker_factory)(worker_config["url"], worker_config["token"])
        _same_hub(worker, paired_hub, worker_config["url"])
        _scope(worker, identity, connection_id, waba_id, phone_number_id)
        def credential():
            if read_private_json(token_file) != token_config: raise ConnectionError("Selected Meta credential changed.")
            return _token(token_config)
        account = (BusinessAccount if account_factory is None else account_factory)(identity["principal_id"], waba_id, phone_number_id,
                                                                                    credential, connection_id=connection_id)
        snapshot = _snapshot(account.snapshot(), identity["principal_id"], connection_id)
        if snapshot["waba_id"] != waba_id or snapshot["phone_number_id"] != phone_number_id: raise ValueError("sender")
        if _identity(paired_hub.capabilities()) != identity: raise ValueError("identity changed")
        _scope(worker, identity, connection_id, waba_id, phone_number_id)
        if _worker_config(read_private_json(worker_client_config), paired_hub) != worker_config or read_private_json(token_file) != token_config: raise ValueError("source changed")
        _same_hub(worker, paired_hub, worker_config["url"])
        config = {"schema_version": 1, "principal_id": identity["principal_id"], "connection_id": connection_id,
                  "graph_api_version": GRAPH_API_VERSION, "account_snapshot": snapshot,
                  "token_file": "meta-token.json", "worker_client_file": "worker-client.json"}
        directory.mkdir(mode=0o700)
        owned = True
        _protect_directory(directory)
        _verify_permissions([directory])
        for name, value in (("connection.json", config), ("meta-token.json", token_config), ("worker-client.json", worker_config)):
            path = directory / name
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            created.append(path)
            with os.fdopen(descriptor, "wb") as target:
                target.write(json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))
                target.flush()
                os.fsync(target.fileno())
        WhatsAppConnection(directory / "connection.json", paired_hub, identity, worker_factory=worker_factory, account_factory=account_factory)
        return {"status": "configured", "sending_enabled": False, "principal_id": identity["principal_id"],
                "connection_id": connection_id, "display_phone_number": snapshot["display_phone_number"], "config_path": str(directory / "connection.json")}
    except BaseException as error:
        for path in reversed(created):
            try: path.unlink(missing_ok=True)
            except OSError: pass
        if owned:
            try: directory.rmdir()
            except OSError: pass
        if not isinstance(error, Exception): raise
        raise ConnectionError("WhatsApp connection setup failed. No account, consent record or message was created; existing stores were not overwritten.") from None


def whatsapp_connection_from_env(paired_hub, identity):
    path = os.environ.get("EDITH_WHATSAPP_CONNECTION_CONFIG")
    return None if not path else WhatsAppConnection(path, paired_hub, identity)


def sync_template(connection, template_id, name, language):
    """Explicit provider verification and one worker registry write, never a send."""
    try:
        baseline = connection.revalidate()
        verified = connection.catalog.get(template_id, name, language)
        if not isinstance(verified, dict) or set(verified) != {"account", "template"}: raise ValueError("snapshot")
        account = _snapshot(verified["account"], connection.principal_id, connection.connection_id)
        stable = lambda value: {key: item for key, item in value.items() if key != "verified_at"}
        if stable(account) != stable(baseline): raise ValueError("account changed")
        template = verified["template"]
        if (not isinstance(template, dict) or template != _template({**template, "components": [{"type": "BODY", "text": template.get("body")}]})):
            raise ValueError("template changed")
        if connection.revalidate() != baseline: raise ValueError("connection changed")
        digest = hashlib.sha256(json.dumps({"account": stable(account), "template": template}, sort_keys=True,
                                          separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    except Exception:
        raise ConnectionError("The exact configured sender and approved template could not be verified. No registry write or message was attempted.") from None
    try:
        result = connection.worker.register_binding(account, template)
        if (not isinstance(result, dict) or set(result) != {"id", "version", "account", "template", "updated_at"}
                or not isinstance(result["id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", result["id"])
                or result["version"] != digest or result["account"] != account or result["template"] != template
                or type(result["updated_at"]) is not int or result["updated_at"] < 0):
            raise ValueError("registry receipt")
        connection.revalidate()
        return deepcopy(result)
    except Exception:
        raise ConnectionError("Template registration could not be confirmed. It may already exist; inspect the worker registry before retrying. No message was sent.") from None
