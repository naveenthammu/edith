from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from tools import configure_household_merchant as cli


HUB_TOKEN = "synthetic-private-hub-token"
SHOP_TOKEN = "synthetic-private-storefront-token"


def merchant():
    entry = {"principal_id": "owner", "id": "clothes", "display_name": "Clothes Store", "shop_domain": "fixture-shop.myshopify.com",
             "checkout_hosts": ["fixture-shop.myshopify.com"]}
    version = hashlib.sha256(json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    del entry["principal_id"]
    return {**entry, "version": version}


class Hub:
    def __init__(self):
        self.identity = {"principal_id": "owner", "role": "adult"}
        self.rows = [merchant()]
        self.calls = []
        self.hook = None
    def capabilities(self):
        self.calls.append("identity")
        if self.hook: self.hook(self)
        return deepcopy(self.identity)
    def merchants(self):
        self.calls.append("merchants")
        if self.hook: self.hook(self)
        return {"merchants": deepcopy(self.rows)}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from connectors import merchant_account
    hub = Hub()
    config = tmp_path / "owner-client.json"
    config.write_text(json.dumps({"url": "http://127.0.0.1:8788", "token": HUB_TOKEN}))
    token = tmp_path / "selected-token.txt"
    token.write_text(SHOP_TOKEN + "\n")
    monkeypatch.setattr(cli, "_load_client", lambda selected: (hub, HUB_TOKEN), raising=False)
    monkeypatch.setattr(cli, "_verify_permissions", lambda _: None, raising=False)
    monkeypatch.setattr(merchant_account, "_protect_directory", lambda _: None)
    monkeypatch.setattr(merchant_account, "_verify_permissions", lambda _: None)
    return {"hub": hub, "config": config, "token": token, "target": tmp_path / "new-merchant", "monkeypatch": monkeypatch}


def arguments(setup, tokenless=True):
    result = ["--hub-client-config", str(setup["config"]), "--merchant-id", "clothes", "--account-dir", str(setup["target"])]
    return result + (["--tokenless"] if tokenless else ["--storefront-token-file", str(setup["token"])])


def test_tokenless_cli_writes_real_bound_configuration_after_fresh_identity_checks(setup, capsys):
    assert cli.main(arguments(setup)) == 0
    value = json.loads((setup["target"] / "merchant.json").read_text())
    assert value["principal_id"] == "owner" and value["merchant_id"] == "clothes" and value["storefront_token"] is None
    output = json.loads(capsys.readouterr().out)
    assert output == {"status": "configured", "principal_id": "owner", "merchant_id": "clothes", "config_path": str(setup["target"] / "merchant.json")}
    assert setup["hub"].calls == ["identity", "merchants", "merchants", "identity", "merchants"]


def test_selected_private_public_storefront_token_file_is_not_echoed(setup, capsys):
    assert cli.main(arguments(setup, tokenless=False)) == 0
    value = json.loads((setup["target"] / "merchant.json").read_text())
    assert value["storefront_token"] == SHOP_TOKEN
    output = capsys.readouterr()
    assert SHOP_TOKEN not in output.out + output.err and HUB_TOKEN not in output.out + output.err


@pytest.mark.parametrize("change", ["guest", "different_principal", "revoked", "merchant_removed", "merchant_changed"])
def test_role_revocation_or_binding_change_before_write_creates_nothing(setup, capsys, change):
    if change == "guest": setup["hub"].identity["role"] = "guest"
    def hook(hub):
        if len(hub.calls) == 4:
            if change == "different_principal": hub.identity["principal_id"] = "other"
            if change == "revoked": hub.identity["role"] = "guest"
        if len(hub.calls) == 3:
            if change == "merchant_removed": hub.rows = []
            if change == "merchant_changed": hub.rows[0]["version"] = "a" * 64
    setup["hub"].hook = hook
    assert cli.main(arguments(setup)) == 1 and not setup["target"].exists()
    assert capsys.readouterr().out == ""


def test_unprotected_selected_token_file_is_refused_before_setup(setup, capsys):
    def verify(paths):
        if Path(paths[-1]) == setup["token"]: raise RuntimeError("broad private token file")
    setup["monkeypatch"].setattr(cli, "_verify_permissions", verify, raising=False)
    assert cli.main(arguments(setup, tokenless=False)) == 1 and not setup["target"].exists()
    output = capsys.readouterr()
    assert SHOP_TOKEN not in output.out + output.err and "broad private" not in output.err


def test_token_file_change_during_blocking_hub_lookup_is_refused(setup):
    def hook(hub):
        if len(hub.calls) == 3: setup["token"].write_text("changed-token")
    setup["hub"].hook = hook
    assert cli.main(arguments(setup, tokenless=False)) == 1 and not setup["target"].exists()


@pytest.mark.parametrize("content", ["", "bad\r\nheader", "x" * 8195, "token with spaces"])
def test_invalid_selected_token_never_creates_configuration(setup, content):
    setup["token"].write_text(content)
    assert cli.main(arguments(setup, tokenless=False)) == 1 and not setup["target"].exists()


def test_existing_configuration_is_not_overwritten(setup, capsys):
    assert cli.main(arguments(setup)) == 0
    before = (setup["target"] / "merchant.json").read_bytes()
    assert cli.main(arguments(setup, tokenless=False)) == 1
    assert (setup["target"] / "merchant.json").read_bytes() == before
    assert SHOP_TOKEN not in capsys.readouterr().err


def test_cli_rejects_secret_argv_without_echoing_the_unrecognized_secret(setup, capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(arguments(setup) + ["--storefront-token", SHOP_TOKEN])
    assert error.value.code == 2
    output = capsys.readouterr()
    assert SHOP_TOKEN not in output.out + output.err


def test_cli_help_documents_tokenless_and_selected_file_without_side_effects(capsys):
    with pytest.raises(SystemExit) as error: cli.main(["--help"])
    assert error.value.code == 0
    text = capsys.readouterr().out
    assert "--tokenless" in text and "--storefront-token-file" in text and "public" in text


def test_merchant_revoked_during_final_identity_lookup_is_rechecked_before_write(setup):
    def hook(hub):
        if len(hub.calls) == 4:
            hub.rows = []
    setup["hub"].hook = hook
    assert cli.main(arguments(setup)) == 1 and not setup["target"].exists()
