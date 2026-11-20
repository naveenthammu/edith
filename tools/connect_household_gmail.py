"""Explicit Google Desktop OAuth onboarding for one authenticated household principal."""
import argparse
import json
import sys

from brain.hub import HubClient
from connectors.gmail_account import GmailAccountError, _read_json, connect_account


def main(argv=None, *, hub_factory=None, connector=None):
    parser = argparse.ArgumentParser(description="Connect a verified Gmail mailbox for one adult/elder household principal.")
    parser.add_argument("--hub-client-config", required=True, help="Absolute path to the authenticated owner client JSON")
    parser.add_argument("--oauth-client-config", required=True, help="Absolute path to a Google Desktop OAuth client JSON")
    parser.add_argument("--account-dir", required=True, help="New private local directory outside synchronized folders")
    arguments = parser.parse_args(argv)
    try:
        config = _read_json(arguments.hub_client_config)
        if set(config) != {"url", "token"} or not isinstance(config["url"], str) or not isinstance(config["token"], str):
            raise GmailAccountError("Invalid household client configuration.")
        hub = (HubClient if hub_factory is None else hub_factory)(config["url"], config["token"])
        result = (connect_account if connector is None else connector)(hub, arguments.oauth_client_config, arguments.account_dir)
        safe = {key: result[key] for key in ("status", "principal_id", "verified_email", "account_config")}
        if safe["status"] != "configured" or any(not isinstance(value, str) for value in safe.values()):
            raise GmailAccountError("Account setup did not return a valid result.")
        output = json.dumps(safe, ensure_ascii=False)
        if config["token"]:
            output = output.replace(config["token"], "[redacted]")
        print(output)
        return 0
    except Exception:
        print("Gmail setup failed. Check the selected configurations, consent and private account-directory permissions.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
