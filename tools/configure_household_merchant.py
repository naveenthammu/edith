"""Explicit local own-Shopify-store setup; never contacts the merchant or opens a browser.

Select a paired owner/elder HubClient configuration and a Rust-allowlisted merchant
ID. Choose --tokenless or a selected private plaintext file containing a PUBLIC
Storefront access token (not an Admin API or private Storefront token). The selected
files and their parent directories must already have current-user/SYSTEM private
permissions. No token is accepted as a command-line value. A new protected merchant
directory is created only after fresh identity and allowlist rechecks.
"""
import argparse
import json
from pathlib import Path
import re
import sys

from connectors.merchant_account import MerchantAccountError, _directory, _identity, _merchant, configure_merchant_account
from connectors.workspace import _entry, _read_file, _verify_permissions
from tools.hub_admin import _load_client


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's usual error echoes unrecognized argv, which could contain a token.
        self.exit(2, "Invalid merchant setup arguments. Use --help; never pass a token value on the command line.\n")


def _private_file(path, limit):
    target = Path(path)
    if not target.is_absolute(): raise MerchantAccountError("Select an absolute private file path.")
    directory = _directory(target.parent)
    target = directory / target.name
    _verify_permissions([directory, target])
    raw, before = _read_file(target, limit)
    _verify_permissions([directory, target])
    if _directory(target.parent) != directory: raise MerchantAccountError("Selected directory changed.")
    after = _entry(target)
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_mode", "st_nlink")
    if after.st_nlink != 1 or any(getattr(after, field) != getattr(before, field) for field in fields):
        raise MerchantAccountError("Selected file changed during verification.")
    return raw


def _selected_merchant(hub, principal, identifier):
    rows = hub.merchants()
    if (not isinstance(rows, dict) or set(rows) != {"merchants"} or not isinstance(rows["merchants"], list)
            or len(rows["merchants"]) > 128):
        raise MerchantAccountError("A current trusted merchant allowlist is required.")
    selected = [row for row in rows["merchants"] if isinstance(row, dict) and row.get("id") == identifier]
    if len(selected) != 1: raise MerchantAccountError("The selected merchant is not allowlisted.")
    return _merchant(selected[0], principal)


def _token(raw):
    value = raw.decode("ascii")
    if value.endswith("\n"):
        value = value[:-1]
        if value.endswith("\r"): value = value[:-1]
    if not 1 <= len(value) <= 8192 or not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", value):
        raise MerchantAccountError("The selected public Storefront token file is invalid.")
    return value


def main(argv=None):
    parser = _Parser(description="Configure one allowlisted own Shopify store locally; no merchant network requests or checkout.", allow_abbrev=False)
    parser.add_argument("--hub-client-config", required=True, help="Absolute protected owner/elder HubClient JSON path")
    parser.add_argument("--merchant-id", required=True, help="Exact merchant ID from the trusted Rust allowlist")
    parser.add_argument("--account-dir", required=True, help="NEW private local directory outside repositories and synchronized folders")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--tokenless", action="store_true", help="Explicitly configure Shopify tokenless access where supported")
    mode.add_argument("--storefront-token-file", help="Absolute protected plaintext file containing a public Storefront token; never an Admin/private token")
    arguments = parser.parse_args(argv)
    try:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", arguments.merchant_id): raise MerchantAccountError("Invalid merchant identifier.")
        client_bytes = _private_file(arguments.hub_client_config, 16_384)
        hub, hub_token = _load_client(arguments.hub_client_config)
        if _private_file(arguments.hub_client_config, 16_384) != client_bytes: raise MerchantAccountError("Client configuration changed.")
        identity = hub.capabilities()
        principal = _identity(identity)
        selected = _selected_merchant(hub, principal, arguments.merchant_id)
        token_bytes, storefront_token = None, None
        if arguments.storefront_token_file is not None:
            token_bytes = _private_file(arguments.storefront_token_file, 8194)
            storefront_token = _token(token_bytes)
        if _selected_merchant(hub, principal, arguments.merchant_id) != selected:
            raise MerchantAccountError("Merchant allowlist changed.")
        if _private_file(arguments.hub_client_config, 16_384) != client_bytes:
            raise MerchantAccountError("Client configuration changed.")
        if token_bytes is not None and _private_file(arguments.storefront_token_file, 8194) != token_bytes:
            raise MerchantAccountError("Selected credential changed.")
        current = hub.capabilities()
        if _identity(current) != principal or current["role"] != identity["role"]:
            raise MerchantAccountError("Household identity or permission changed.")
        if _selected_merchant(hub, principal, arguments.merchant_id) != selected:
            raise MerchantAccountError("Merchant allowlist changed during final identity verification.")
        result = configure_merchant_account(arguments.account_dir, current, selected, storefront_token)
        safe = {key: result[key] for key in ("status", "principal_id", "merchant_id", "config_path")}
        if (safe["status"] != "configured" or safe["principal_id"] != principal or safe["merchant_id"] != selected["id"]
                or not isinstance(safe["config_path"], str)):
            raise MerchantAccountError("Setup result could not be confirmed.")
        output = json.dumps(safe, ensure_ascii=False)
        for secret in (hub_token, storefront_token):
            if isinstance(secret, str) and secret: output = output.replace(secret, "[redacted]")
        print(output)
        return 0
    except Exception:
        print("Merchant setup failed. Check the paired adult/elder identity, allowlisted merchant, selected private files and new account directory.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
