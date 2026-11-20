"""Explicit read-only business sender/template verification; never sends a message.

Run python -m tools.verify_whatsapp_business --help from the repository. Select an
existing private token JSON with only access_token, outside synced/source folders.
The token file and parent directory must have private OS permissions. This command
does not alter ACLs, create accounts, collect tokens from arguments or enable runtime
sending. The principal argument is a trusted local binding, not human authentication.
Metadata access requires whatsapp_business_management; sending permission and
account/billing readiness are not established by these read-only checks.
"""
import argparse
import json
import sys

from connectors.whatsapp_account import BusinessAccount, ProtectedTokenFile
from connectors.whatsapp_templates import TemplateCatalog


def main(argv=None, *, account_factory=None, catalog_factory=None):
    parser = argparse.ArgumentParser(description="Verify configured WhatsApp Business metadata without sending, connecting a runtime, or creating an account.")
    parser.add_argument("--principal-id", required=True, help="Trusted local principal binding; not proof of speaker identity")
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--waba-id", required=True)
    parser.add_argument("--phone-number-id", required=True)
    parser.add_argument("--token-file", required=True, help="Absolute existing private JSON path; never place the token in command-line arguments")
    parser.add_argument("--template-id")
    parser.add_argument("--template-name")
    parser.add_argument("--language")
    parser.add_argument("--parameter", action="append", default=[], help="Exact positional text value; command-line values can enter shell history")
    args = parser.parse_args(argv)
    template_options = (args.template_id, args.template_name, args.language)
    if (any(template_options) and not all(template_options)) or (args.parameter and not all(template_options)):
        parser.error("Template verification requires --template-id, --template-name and --language together.")
    try:
        account = (BusinessAccount if account_factory is None else account_factory)(
            args.principal_id, args.waba_id, args.phone_number_id, ProtectedTokenFile(args.token_file), connection_id=args.connection_id)
        if all(template_options):
            catalog = (TemplateCatalog if catalog_factory is None else catalog_factory)(account)
            output = {"status": "template_metadata_verified", "sending_enabled": False,
                      "prepared": catalog.prepare(*template_options, args.parameter)}
        else:
            output = {"status": "metadata_verified", "sending_enabled": False, "account": account.snapshot()}
        print(json.dumps(output, ensure_ascii=False, allow_nan=False))
        return 0
    except Exception:
        print("Business metadata verification failed. Check the exact account, selected private token file, permissions and approved template configuration. No message was sent.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
