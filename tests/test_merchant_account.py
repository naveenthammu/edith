from copy import deepcopy
import hashlib
import json
import os
import subprocess

import pytest

from connectors.merchant_account import MerchantAccount, MerchantAccountError, configure_merchant_account, merchant_from_env


IDENTITY = {"principal_id": "owner", "role": "adult"}
TOKEN = "synthetic-private-storefront-token"


def merchant():
    entry = {"principal_id": "owner", "id": "clothes", "display_name": "Clothes Store",
             "shop_domain": "fixture-shop.myshopify.com", "checkout_hosts": ["fixture-shop.myshopify.com"]}
    digest = hashlib.sha256(json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    del entry["principal_id"]
    return {**entry, "version": digest}


@pytest.fixture
def local(tmp_path, monkeypatch):
    import connectors.merchant_account as module
    monkeypatch.setattr(module, "_protect_directory", lambda _: None, raising=False)
    monkeypatch.setattr(module, "_verify_permissions", lambda _: None, raising=False)
    monkeypatch.delenv("EDITH_MERCHANT_CONFIG", raising=False)
    return tmp_path / "merchant-account"


def test_explicit_configuration_is_private_principal_and_server_version_bound(local, capsys):
    result = configure_merchant_account(local, IDENTITY, merchant(), TOKEN)
    assert result["status"] == "configured" and result["principal_id"] == "owner"
    path = local / "merchant.json"
    value = json.loads(path.read_text())
    assert value["storefront_token"] == TOKEN and value["merchant_version"] == merchant()["version"]
    account = MerchantAccount(path, IDENTITY, merchant())
    assert account.merchant == merchant() and account.storefront is not None
    snapshot = account.merchant
    snapshot["shop_domain"] = "evil.myshopify.com"
    assert account.merchant["shop_domain"] == "fixture-shop.myshopify.com"
    assert TOKEN not in str(result) + str(account.merchant) + capsys.readouterr().out


def test_no_configuration_means_no_adapter_or_account_creation(monkeypatch):
    monkeypatch.delenv("EDITH_MERCHANT_CONFIG", raising=False)
    assert merchant_from_env(IDENTITY, {"merchants": []}) is None


def test_environment_factory_requires_matching_current_merchant(local, monkeypatch):
    configure_merchant_account(local, IDENTITY, merchant())
    monkeypatch.setenv("EDITH_MERCHANT_CONFIG", str(local / "merchant.json"))
    account = merchant_from_env(IDENTITY, {"merchants": [merchant()]})
    assert account.merchant == merchant()
    with pytest.raises(MerchantAccountError):
        merchant_from_env(IDENTITY, {"merchants": []})


@pytest.mark.parametrize("change", ["principal", "role", "shop", "version", "hosts", "name"])
def test_changed_or_unauthorized_binding_never_loads_account(local, change):
    configure_merchant_account(local, IDENTITY, merchant(), TOKEN)
    identity, selected = deepcopy(IDENTITY), merchant()
    if change == "principal": identity["principal_id"] = "other"
    elif change == "role": identity["role"] = "guest"
    elif change == "shop": selected["shop_domain"] = "other.myshopify.com"
    elif change == "version": selected["version"] = "a" * 64
    elif change == "hosts": selected["checkout_hosts"] = ["evil.invalid"]
    else: selected["display_name"] = "Different name"
    with pytest.raises(MerchantAccountError):
        MerchantAccount(local / "merchant.json", identity, selected)


def test_existing_store_is_never_overwritten(local):
    configure_merchant_account(local, IDENTITY, merchant(), TOKEN)
    before = (local / "merchant.json").read_bytes()
    with pytest.raises(MerchantAccountError):
        configure_merchant_account(local, IDENTITY, merchant(), "different-token")
    assert (local / "merchant.json").read_bytes() == before


def test_token_is_revalidated_before_use_and_changed_store_fails_closed(local):
    configure_merchant_account(local, IDENTITY, merchant(), TOKEN)
    account = MerchantAccount(local / "merchant.json", IDENTITY, merchant())
    value = json.loads((local / "merchant.json").read_text())
    value["storefront_token"] = "different-token"
    (local / "merchant.json").write_text(json.dumps(value))
    with pytest.raises(MerchantAccountError):
        account.revalidate(merchant())
    calls = []
    account.storefront._transport = lambda *_args, **_kwargs: calls.append(1)
    assert account.storefront.create_cart([{"variant_id": "gid://shopify/ProductVariant/1", "quantity": 1}])["status"] == "failed"
    assert calls == []


@pytest.mark.parametrize("change", ["unexpected", "hardlink", "oversize", "duplicate"])
def test_store_rejects_unsafe_artifacts(local, change):
    configure_merchant_account(local, IDENTITY, merchant(), TOKEN)
    path = local / "merchant.json"
    if change == "unexpected": (local / "extra.txt").write_text("fixture")
    elif change == "hardlink": os.link(path, local.parent / "outside-hardlink")
    elif change == "oversize": path.write_text(" " * 16385)
    else: path.write_text('{"version":1,"version":1}')
    with pytest.raises(MerchantAccountError):
        MerchantAccount(path, IDENTITY, merchant())


def test_synchronized_and_relative_paths_are_rejected_without_creation(local):
    for directory in ("relative-store", local.parent / "OneDrive" / "store"):
        with pytest.raises(MerchantAccountError):
            configure_merchant_account(directory, IDENTITY, merchant(), TOKEN)
    assert not (local.parent / "OneDrive").exists()


def test_secret_write_failure_removes_only_attempt_owned_artifacts(local, monkeypatch):
    import connectors.merchant_account as module
    monkeypatch.setattr(module.os, "fsync", lambda _: (_ for _ in ()).throw(OSError("private details")))
    with pytest.raises(MerchantAccountError) as error:
        configure_merchant_account(local, IDENTITY, merchant(), TOKEN)
    assert not local.exists() and "private details" not in str(error.value)


def test_real_native_permissions_are_required_and_rechecked(tmp_path, monkeypatch):
    import connectors.merchant_account as module
    directory = tmp_path / "native-private-merchant"
    configure_merchant_account(directory, IDENTITY, merchant(), TOKEN)
    account = MerchantAccount(directory / "merchant.json", IDENTITY, merchant())
    account.revalidate(merchant())
    monkeypatch.setattr(module, "_verify_permissions", lambda _: (_ for _ in ()).throw(RuntimeError("broad ACL")))
    with pytest.raises(MerchantAccountError):
        account.revalidate(merchant())


def test_failure_cleanup_preserves_unrelated_new_file_and_returns_safe_error(local, monkeypatch):
    import connectors.merchant_account as module
    def failed_sync(_):
        (local / "unrelated.txt").write_text("keep this")
        raise OSError("private raw details")
    monkeypatch.setattr(module.os, "fsync", failed_sync)
    with pytest.raises(MerchantAccountError) as error:
        configure_merchant_account(local, IDENTITY, merchant(), TOKEN)
    assert (local / "unrelated.txt").read_text() == "keep this"
    assert not (local / "merchant.json").exists() and "private raw details" not in str(error.value)


def test_credentials_are_not_created_inside_a_repository(local):
    (local.parent / ".git").mkdir()
    with pytest.raises(MerchantAccountError):
        configure_merchant_account(local, IDENTITY, merchant(), TOKEN)
    assert not local.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction boundary")
def test_dangling_junction_cannot_redirect_new_merchant_configuration(local):
    outside = local.parent / "missing-junction-target"
    outside.mkdir()
    environment = {**os.environ, "EDITH_TEST_LINK": str(local), "EDITH_TEST_TARGET": str(outside)}
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                             "New-Item -ItemType Junction -Path $env:EDITH_TEST_LINK -Target $env:EDITH_TEST_TARGET | Out-Null"],
                            env=environment, capture_output=True, timeout=15)
    assert result.returncode == 0, "Could not create the synthetic junction fixture"
    outside.rmdir()  # This exact test-created empty directory makes the junction dangling.
    try:
        with pytest.raises(MerchantAccountError):
            configure_merchant_account(local, IDENTITY, merchant(), TOKEN)
        assert not outside.exists()
    finally:
        local.rmdir()  # Remove only the test-created junction, never its target tree.


def test_configuration_replacement_during_second_acl_check_is_rejected(local, monkeypatch):
    import connectors.merchant_account as module
    configure_merchant_account(local, IDENTITY, merchant(), TOKEN)
    path = local / "merchant.json"
    replacement = local.parent / "replacement.json"
    value = json.loads(path.read_text())
    value["storefront_token"] = "different-token"
    replacement.write_text(json.dumps(value))
    checks = []
    def check(_):
        checks.append(1)
        if len(checks) == 2: os.replace(replacement, path)
    monkeypatch.setattr(module, "_verify_permissions", check)
    with pytest.raises(MerchantAccountError):
        MerchantAccount(path, IDENTITY, merchant())


def test_factory_accepts_the_full_server_limit_of_128_merchants(local, monkeypatch):
    configure_merchant_account(local, IDENTITY, merchant())
    monkeypatch.setenv("EDITH_MERCHANT_CONFIG", str(local / "merchant.json"))
    rows = [merchant(), *[{**merchant(), "id": f"other-{index}"} for index in range(127)]]
    assert merchant_from_env(IDENTITY, {"merchants": rows}).merchant == merchant()
