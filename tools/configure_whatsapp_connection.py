"""Explicit selected-account verification and private local configuration.

No secrets are accepted in argv. Setup does not register a template, record consent,
send a message, or prove billing readiness. Each is a separate explicit operation.
"""
import argparse
import json
import sys

from brain.hub import HubClient
from connectors.whatsapp_connection import ConnectionError, WhatsAppConnection, configure_connection, read_private_json
from tools.hub_admin import _load_client


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "Invalid WhatsApp administration arguments. Use --help; never pass secret values on the command line.\n")


def _load_paired(path):
    before = read_private_json(path)
    if set(before) != {"url", "token"}: raise ConnectionError("Select a paired client configuration.")
    hub, token = _load_client(path)
    expected = HubClient(before["url"], before["token"])
    if token != before["token"] or hub.url != expected.url or read_private_json(path) != before:
        raise ConnectionError("Paired client configuration changed.")
    return hub


def load_connection(config_path, paired_config):
    hub = _load_paired(paired_config)
    return WhatsAppConnection(config_path, hub, hub.capabilities()), hub


def main(argv=None, *, connector=None, paired_loader=None):
    parser = _Parser(description="Verify one selected WhatsApp Business sender and create a NEW private connection store; no messages.", allow_abbrev=False)
    parser.add_argument("--paired-client-config", required=True, help="Absolute protected adult/elder HubClient JSON path")
    parser.add_argument("--worker-client-config", required=True, help="Absolute protected JSON for the dedicated sender-bound worker on the same hub")
    parser.add_argument("--token-file", required=True, help="Absolute protected JSON containing access_token; never a token value")
    parser.add_argument("--connection-dir", required=True, help="NEW private directory outside source repositories and synchronized folders")
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--waba-id", required=True)
    parser.add_argument("--phone-number-id", required=True)
    args = parser.parse_args(argv)
    try:
        hub = (_load_paired if paired_loader is None else paired_loader)(args.paired_client_config)
        result = (configure_connection if connector is None else connector)(hub, args.worker_client_config, args.token_file, args.connection_dir,
            connection_id=args.connection_id, waba_id=args.waba_id, phone_number_id=args.phone_number_id)
        # Directory arguments may themselves accidentally contain sensitive text.
        safe = {key: result[key] for key in ("status", "sending_enabled", "principal_id", "connection_id", "display_phone_number")}
        if safe["status"] != "configured" or safe["sending_enabled"] is not False: raise ConnectionError("Setup receipt.")
        print(json.dumps(safe, ensure_ascii=False))
        return 0
    except Exception:
        print("WhatsApp setup could not be confirmed. Check the selected private files, paired adult/elder identity, exact dedicated worker scope, and new directory. No message was sent.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
