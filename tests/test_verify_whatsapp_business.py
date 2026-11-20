import importlib
import importlib.util
import json


def api():
    assert importlib.util.find_spec("tools.verify_whatsapp_business") is not None, "Explicit verification CLI is missing"
    return importlib.import_module("tools.verify_whatsapp_business")


ARGS = ["--principal-id", "alice", "--connection-id", "one", "--waba-id", "23456", "--phone-number-id", "12345", "--token-file", "D:/Private/account-token.json"]


def test_readonly_cli_verifies_metadata_without_send_or_account_creation(capsys):
    calls = []
    class Account:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))
        def snapshot(self):
            return {"principal_id": "alice", "connection_id": "one", "waba_id": "23456", "phone_number_id": "12345",
                    "display_phone_number": "+16505551234", "verified_name": "Example", "version": "a" * 64, "verified_at": 100}
    assert api().main(ARGS, account_factory=Account) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "metadata_verified" and output["sending_enabled"] is False
    assert calls[0][0][:3] == ("alice", "23456", "12345")
    assert callable(calls[0][0][3])


def test_cli_failure_does_not_print_token_or_provider_details(capsys):
    def fail(*_, **__):
        raise RuntimeError("secret-bearer-token provider response")
    assert api().main(ARGS, account_factory=fail) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "secret-bearer" not in captured.err


def test_cli_reads_exact_optional_template_and_parameters(capsys):
    class Account:
        def __init__(self, *args, **kwargs):
            pass
    calls = []
    class Catalog:
        def __init__(self, account):
            pass
        def prepare(self, *args):
            calls.append(args)
            return {"account": {"principal_id": "alice"}, "template": {"id": "34567"}, "parameters": ["Asha"], "rendered_text": "Hello Asha"}
    result = api().main(ARGS + ["--template-id", "34567", "--template-name", "hello", "--language", "en_US", "--parameter", "Asha"],
                        account_factory=Account, catalog_factory=Catalog)
    assert result == 0 and calls == [("34567", "hello", "en_US", ["Asha"])]
    assert json.loads(capsys.readouterr().out)["status"] == "template_metadata_verified"
