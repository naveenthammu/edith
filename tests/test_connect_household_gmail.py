import json

from tools.connect_household_gmail import main


def test_cli_uses_explicit_selected_configs_and_prints_only_safe_account_metadata(tmp_path, capsys):
    token = "synthetic-hub-token-" + "x" * 32
    client = tmp_path / "hub-client.json"
    client.write_text(json.dumps({"url": "http://127.0.0.1:8788", "token": token}))
    oauth = tmp_path / "desktop-oauth-client.json"
    target = tmp_path / "new-gmail-account"
    calls = []
    hub = object()
    def factory(url, supplied_token):
        assert url == "http://127.0.0.1:8788" and supplied_token == token
        return hub
    def connect(supplied_hub, oauth_path, account_dir):
        calls.append((supplied_hub, oauth_path, account_dir))
        return {"status": "configured", "principal_id": "owner", "verified_email": "owner@gmail.com",
                "account_config": str(target / "account.json"), "access_token": "PRIVATE_TOKEN"}
    result = main(["--hub-client-config", str(client), "--oauth-client-config", str(oauth), "--account-dir", str(target)],
                  hub_factory=factory, connector=connect)
    output = capsys.readouterr()
    assert result == 0 and calls == [(hub, str(oauth), str(target))]
    value = json.loads(output.out)
    assert value["status"] == "configured" and value["verified_email"] == "owner@gmail.com"
    assert "PRIVATE_TOKEN" not in output.out and token not in output.out and output.err == ""


def test_cli_failure_does_not_print_raw_exception_or_secret(tmp_path, capsys):
    client = tmp_path / "hub-client.json"
    client.write_text(json.dumps({"url": "http://127.0.0.1:8788", "token": "x" * 40}))
    def fail(*_args):
        raise RuntimeError("PRIVATE_AUTHORIZATION_CODE")
    result = main(["--hub-client-config", str(client), "--oauth-client-config", str(tmp_path / "oauth.json"),
                   "--account-dir", str(tmp_path / "new-account")], hub_factory=lambda *_: object(), connector=fail)
    output = capsys.readouterr()
    assert result == 1 and output.out == "" and "PRIVATE_AUTHORIZATION_CODE" not in output.err
    assert "Gmail setup failed" in output.err


def test_cli_rejects_invalid_client_json_without_starting_authorization(tmp_path, capsys):
    client = tmp_path / "hub-client.json"
    client.write_text('{"url":"http://127.0.0.1:8788","token":"x","unexpected":true}')
    calls = []
    result = main(["--hub-client-config", str(client), "--oauth-client-config", str(tmp_path / "oauth.json"),
                   "--account-dir", str(tmp_path / "new-account")],
                  hub_factory=lambda *_: calls.append("hub"), connector=lambda *_: calls.append("oauth"))
    assert result == 1 and calls == []
    assert capsys.readouterr().out == ""
