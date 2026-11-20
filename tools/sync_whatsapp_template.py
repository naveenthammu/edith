"""Verify one exact approved template with Meta and register its immutable snapshot."""
import json
import sys

from connectors.whatsapp_connection import sync_template
from tools.configure_whatsapp_connection import _Parser, load_connection


def main(argv=None, *, connection_loader=None, synchronizer=None):
    parser = _Parser(description="Verify and register one exact approved BODY-only WhatsApp template; no permission record or message.", allow_abbrev=False)
    parser.add_argument("--connection-config", required=True)
    parser.add_argument("--paired-client-config", required=True)
    parser.add_argument("--template-id", required=True)
    parser.add_argument("--template-name", required=True)
    parser.add_argument("--language", required=True)
    args = parser.parse_args(argv)
    try:
        connection, hub = (load_connection if connection_loader is None else connection_loader)(args.connection_config, args.paired_client_config)
        result = (sync_template if synchronizer is None else synchronizer)(connection, args.template_id, args.template_name, args.language)
        print(json.dumps({"status": "template_registered", "binding_id": result["id"], "binding_version": result["version"],
                          "connection_id": connection.connection_id, "template_id": result["template"]["id"]}))
        return 0
    except Exception:
        print("Template registration could not be confirmed. A registry entry may already exist; inspect it before retrying. No message was sent.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
