"""Explicit per-principal Shopify configuration; no discovery, login or network setup.

The trusted caller supplies the current Rust merchant allowlist row. Matching its
version is configuration binding, not proof that a live merchant/token works.
Only configure_merchant_account writes a NEW protected store. Runtime loads never
repair permissions, rotate credentials, or create directories. Keep the config
outside repositories and synchronized folders. No personal customer data is stored.
"""
from copy import deepcopy
import hashlib
import hmac
import json
import os
from pathlib import Path
import re

from connectors.shopify import ShopifyStorefront
from connectors.workspace import _entry, _read_file, _verify_permissions
from tools.hub_admin import _reject_linked_path, _restrict_directory as _protect_directory


_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_FIELDS = {"version", "principal_id", "merchant_id", "merchant_version", "shop_domain", "checkout_hosts", "storefront_token"}


class MerchantAccountError(RuntimeError):
    """Safe account binding/protection failure without tokens or provider details."""


def _identity(identity):
    if (not isinstance(identity, dict) or identity.get("role") not in {"adult", "elder"}
            or not isinstance(identity.get("principal_id"), str) or not _ID.fullmatch(identity["principal_id"])):
        raise MerchantAccountError("Merchant access requires an authenticated adult or elder principal.")
    return identity["principal_id"]


def _merchant(value, principal):
    if (not isinstance(value, dict) or set(value) != {"id", "display_name", "shop_domain", "checkout_hosts", "version"}
            or not isinstance(value["id"], str) or not _ID.fullmatch(value["id"])
            or not isinstance(value["display_name"], str) or not 1 <= len(value["display_name"]) <= 120
            or not value["display_name"].strip() or any(ord(c) < 32 or ord(c) == 127 for c in value["display_name"])
            or not isinstance(value["checkout_hosts"], list) or not 1 <= len(value["checkout_hosts"]) <= 10
            or any(not isinstance(host, str) for host in value["checkout_hosts"])
            or len(set(value["checkout_hosts"])) != len(value["checkout_hosts"])
            or not isinstance(value["version"], str) or not _HASH.fullmatch(value["version"])):
        raise MerchantAccountError("The trusted merchant allowlist row is invalid.")
    ShopifyStorefront(value["shop_domain"], checkout_hosts=set(value["checkout_hosts"]))
    canonical = {"principal_id": principal, **{key: item for key, item in value.items() if key != "version"}}
    canonical["checkout_hosts"] = sorted(canonical["checkout_hosts"])
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                      allow_nan=False).encode("utf-8")).hexdigest()
    if not hmac.compare_digest(digest, value["version"]):
        raise MerchantAccountError("The merchant allowlist version does not match this principal and configuration.")
    return {**deepcopy(value), "checkout_hosts": sorted(value["checkout_hosts"])}


def _directory(path):
    directory = Path(path)
    if not directory.is_absolute() or str(directory).startswith(("\\\\", "//")):
        raise MerchantAccountError("Use an absolute local merchant directory.")
    for component in (directory, *directory.parents):
        try:
            _entry(component)  # lstat also detects dangling junctions before resolve follows them.
        except FileNotFoundError:
            continue
    _reject_linked_path(directory)
    directory = directory.resolve()
    if any((parent / ".git").exists() for parent in (directory, *directory.parents)):
        raise MerchantAccountError("Keep merchant credentials outside source repositories.")
    if any(any(part.casefold() == name or part.casefold().startswith(name + " - ") for name in
               ("onedrive", "dropbox", "google drive", "googledrive", "icloud", "iclouddrive")) for part in directory.parts):
        raise MerchantAccountError("Keep merchant credentials outside synchronized directories.")
    return directory


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MerchantAccountError("Merchant configuration has duplicate fields.")
        result[key] = value
    return result


def _load(path):
    config = Path(path)
    if not config.is_absolute() or config.name != "merchant.json":
        raise MerchantAccountError("Select the absolute merchant.json path in a dedicated private directory.")
    directory = _directory(config.parent)
    with os.scandir(directory) as entries:
        names = []
        for entry in entries:
            names.append(entry.name)
            if names != ["merchant.json"]:
                raise MerchantAccountError("Merchant directory contains unexpected artifacts.")
    if names != ["merchant.json"]:
        raise MerchantAccountError("The merchant configuration is missing.")
    target = directory / "merchant.json"
    if _entry(target).st_nlink != 1:
        raise MerchantAccountError("Merchant configuration cannot be linked.")
    _verify_permissions([directory, target])
    raw, read_stat = _read_file(target, 16_384)
    value = json.loads(raw, object_pairs_hook=_unique)
    if (not isinstance(value, dict) or set(value) != _FIELDS or type(value["version"]) is not int or value["version"] != 1):
        raise MerchantAccountError("The merchant configuration schema is invalid.")
    token = value["storefront_token"]
    if token is not None and (not isinstance(token, str) or not 1 <= len(token) <= 8192
                              or not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token)):
        raise MerchantAccountError("The configured Storefront credential is invalid.")
    _verify_permissions([directory, target])
    if _directory(config.parent) != directory:
        raise MerchantAccountError("Merchant directory changed during verification.")
    after = _entry(target)
    # Windows fstat()/lstat() expose different legacy ctime semantics; compare
    # stable file identity, size, modification time, type/mode and link count.
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_mode", "st_nlink")
    if after.st_nlink != 1 or any(getattr(after, key) != getattr(read_stat, key) for key in fields):
        raise MerchantAccountError("Merchant configuration changed during permission verification.")
    return value


def _configuration(principal, selected, token):
    return {"version": 1, "principal_id": principal, "merchant_id": selected["id"],
            "merchant_version": selected["version"], "shop_domain": selected["shop_domain"],
            "checkout_hosts": selected["checkout_hosts"], "storefront_token": token}


class MerchantAccount:
    def __init__(self, config_path, identity, merchant):
        try:
            self._principal = _identity(identity)
            self._merchant = _merchant(merchant, self._principal)
            self._path = Path(config_path)
            self._configuration = _load(self._path)
            if self._configuration != _configuration(self._principal, self._merchant, self._configuration["storefront_token"]):
                raise MerchantAccountError("Merchant configuration does not match the trusted principal or allowlist.")
            self.storefront = ShopifyStorefront(self._merchant["shop_domain"], self._credential,
                                                checkout_hosts=set(self._merchant["checkout_hosts"]))
        except Exception:
            raise MerchantAccountError("Merchant configuration or its private permissions could not be verified.") from None

    @property
    def merchant(self):
        """Safe copied configuration metadata, never credentials or cart keys."""
        return deepcopy(self._merchant)

    def revalidate(self, merchant):
        try:
            if _merchant(merchant, self._principal) != self._merchant or _load(self._path) != self._configuration:
                raise MerchantAccountError("Merchant account changed.")
        except Exception:
            raise MerchantAccountError("Merchant configuration changed or lost its private protection; reconnect and review again.") from None
        return self.merchant

    def _credential(self):
        self.revalidate(self._merchant)
        return self._configuration["storefront_token"]


def configure_merchant_account(account_dir, identity, merchant, storefront_token=None):
    directory, owned_file, owned_directory = None, False, False
    try:
        principal = _identity(identity)
        selected = _merchant(merchant, principal)
        directory = _directory(account_dir)
        if directory.exists() or not directory.parent.is_dir():
            raise MerchantAccountError("Choose a new dedicated merchant directory under an existing local parent.")
        directory.mkdir(mode=0o700)
        owned_directory = True
        _protect_directory(directory)
        _verify_permissions([directory])
        target = directory / "merchant.json"
        raw = json.dumps(_configuration(principal, selected, storefront_token), ensure_ascii=False, indent=2).encode("utf-8")
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        owned_file = True
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        MerchantAccount(target, identity, selected)
        return {"status": "configured", "principal_id": principal, "merchant_id": selected["id"], "config_path": str(target)}
    except BaseException as error:
        if owned_file:
            try:
                (directory / "merchant.json").unlink(missing_ok=True)
            except OSError:
                pass
        if owned_directory:
            try:
                directory.rmdir()  # Only this attempt's empty directory; preserve unrelated files.
            except OSError:
                pass
        if not isinstance(error, Exception):
            raise
        raise MerchantAccountError("Merchant setup failed; existing account data was not overwritten.") from None


def merchant_from_env(identity, merchants):
    path = os.environ.get("EDITH_MERCHANT_CONFIG")
    if not path:
        return None
    try:
        _identity(identity)
        configured = _load(path)
        if (not isinstance(merchants, dict) or set(merchants) != {"merchants"}
                or not isinstance(merchants["merchants"], list) or len(merchants["merchants"]) > 128):
            raise MerchantAccountError("A current trusted merchant allowlist is required.")
        selected = [row for row in merchants["merchants"] if isinstance(row, dict) and row.get("id") == configured["merchant_id"]]
        if len(selected) != 1:
            raise MerchantAccountError("The configured merchant is unavailable in the current allowlist.")
        return MerchantAccount(path, identity, selected[0])
    except Exception:
        raise MerchantAccountError("The selected merchant account could not be verified for this principal.") from None
