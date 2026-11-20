"""Explicit local EDITH hub setup and document administration; no UI or inference.

Run from the repository with ``python -m tools.hub_admin --help``. Store credentials
outside source control and synchronized folders, for example in D:/EdithData.
The shared-room desktop credential is guest-scoped; owner credentials are for deliberate
local administration, not proof of which person is speaking near the microphone.
"""
import argparse
import base64
import json
import os
import re
import secrets
import stat
import subprocess
import sys
from pathlib import Path

from brain.hub import HubClient, HubError
from brain.embeddings import EmbeddingError, OllamaEmbeddings, index_document, search_documents


MAX_SOURCE_BYTES = 1_000_000
HUB_URL = "http://127.0.0.1:8788"

# The target travels as data in a child-process environment variable. It is never
# interpolated into PowerShell source, so quotes/backticks/$() in a path stay literal.
_WINDOWS_ACL_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
try {
    $target = $env:EDITH_SETUP_DATA_DIR
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $systemSid = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    $acl = [System.Security.AccessControl.DirectorySecurity]::new()
    $acl.SetOwner($sid)
    $acl.SetAccessRuleProtection($true, $false)
    $inherit = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'
    $propagate = [System.Security.AccessControl.PropagationFlags]::None
    $allow = [System.Security.AccessControl.AccessControlType]::Allow
    $rights = [System.Security.AccessControl.FileSystemRights]::FullControl
    foreach ($identity in @($sid, $systemSid)) {
        $rule = [System.Security.AccessControl.FileSystemAccessRule]::new($identity, $rights, $inherit, $propagate, $allow)
        $acl.AddAccessRule($rule)
    }
    # Windows PowerShell may inherit incompatible PowerShell 7 module paths.
    # Use its .NET Framework ACL APIs directly, without Security module autoload.
    [System.IO.Directory]::SetAccessControl($target, $acl)
    $actual = [System.IO.Directory]::GetAccessControl($target)
    if (-not $actual.AreAccessRulesProtected) { throw 'ACL inheritance is not protected' }
    $rules = $actual.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])
    $expected = @($sid.Value, $systemSid.Value) | Select-Object -Unique
    foreach ($rule in $rules) {
        if ($rule.IdentityReference.Value -notin $expected -or $rule.AccessControlType -ne $allow -or $rule.IsInherited) {
            throw 'Unexpected directory access rule'
        }
    }
    foreach ($identity in $expected) {
        $matches = @($rules | Where-Object { $_.IdentityReference.Value -eq $identity -and $_.FileSystemRights -eq $rights -and $_.InheritanceFlags -eq $inherit })
        if ($matches.Count -ne 1) { throw 'Required directory access rule is missing' }
    }
    exit 0
} catch {
    [Console]::Error.WriteLine('Could not restrict directory permissions.')
    exit 1
}
"""


class AdminError(RuntimeError):
    """A local setup/import error safe to display without credentials or document bodies."""


def _restrict_directory(path: Path) -> None:
    if os.name == "nt":
        environment = os.environ.copy()
        environment["EDITH_SETUP_DATA_DIR"] = str(path)
        command = base64.b64encode(_WINDOWS_ACL_SCRIPT.encode("utf-16-le")).decode("ascii")
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", command],
                env=environment, capture_output=True, text=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise AdminError("Could not restrict directory permissions; no credentials were written.") from None
        if result.returncode != 0:
            raise AdminError("Could not restrict directory permissions; no credentials were written.")
    else:
        try:
            path.chmod(0o700)
            if stat.S_IMODE(path.stat().st_mode) != 0o700:
                raise OSError("permissions")
        except OSError:
            raise AdminError("Could not restrict directory permissions; no credentials were written.") from None


def _reject_linked_path(path: Path) -> None:
    try:
        for component in (path, *path.parents):
            if not component.exists():
                continue
            metadata = component.lstat()
            if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                raise AdminError("Use a regular local path without links or reparse points.")
    except OSError:
        raise AdminError("Could not verify the selected local path.") from None


def _write_new_json(path: Path, value: dict) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as destination:
            json.dump(value, destination, ensure_ascii=False, indent=2)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
    except OSError:
        # Exclusive creation above proves this operation owns this exact file.
        path.unlink(missing_ok=True)
        raise


def initialize(data_dir: str) -> dict:
    directory = Path(data_dir)
    if not directory.is_absolute():
        raise AdminError("The data directory must be an absolute path.")
    if directory.exists() or directory.is_symlink():
        raise AdminError("The data directory already exists. Choose a new dedicated directory; nothing was overwritten.")
    if not directory.parent.is_dir():
        raise AdminError("The data directory's parent must already exist.")
    _reject_linked_path(directory.parent)
    directory = directory.parent.resolve() / directory.name
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        raise AdminError("The data directory already exists; nothing was overwritten.") from None
    except OSError:
        raise AdminError("Could not create the dedicated data directory.") from None

    created = []
    try:
        _restrict_directory(directory)
        owner_token = secrets.token_urlsafe(32)
        shared_token = secrets.token_urlsafe(32)
        while shared_token == owner_token:
            shared_token = secrets.token_urlsafe(32)
        database_path = directory / "knowledge.sqlite3"
        files = {
            "hub-config.json": {"database_path": str(database_path), "bind": "127.0.0.1:8788", "principals": [
                {"id": "owner", "token": owner_token, "role": "adult", "enabled": True},
                {"id": "shared", "token": shared_token, "role": "guest", "enabled": True},
            ]},
            "owner-client.json": {"url": HUB_URL, "token": owner_token},
            "desktop-client.json": {"url": HUB_URL, "token": shared_token},
        }
        for filename, content in files.items():
            path = directory / filename
            _write_new_json(path, content)
            created.append(path)
    except (OSError, AdminError):
        # Only files created by this operation are removed; no recursive delete or existing data.
        for path in created:
            try:
                path.unlink()
            except OSError:
                pass
        try:
            directory.rmdir()
        except OSError:
            pass
        raise AdminError("Initialization failed; credentials were not fully created. Check directory permissions and disk space.") from None
    return {"status": "initialized", "hub_config": str(directory / "hub-config.json"),
            "owner_client_config": str(directory / "owner-client.json"),
            "desktop_client_config": str(directory / "desktop-client.json"),
            "desktop_role": "guest", "database_path": str(database_path)}


def _load_client(config_path: str) -> tuple[HubClient, str]:
    try:
        with Path(config_path).open("rb") as source:
            raw = source.read(16_385)
        if len(raw) > 16_384:
            raise ValueError("size")
        config = json.loads(raw)
        if not isinstance(config, dict) or set(config) != {"url", "token"}:
            raise ValueError("fields")
        return HubClient(config["url"], config["token"]), config["token"]
    except (OSError, ValueError, TypeError, KeyError):
        raise AdminError("Client configuration is invalid or unreadable.") from None


def _selected_text(filename: str, title: str | None) -> tuple[str, str]:
    source = Path(filename)
    if source.suffix.lower() not in {".txt", ".md"}:
        raise AdminError("Select one UTF-8 .txt or .md file; folders and other formats are not imported.")
    try:
        _reject_linked_path(source.absolute())
        if not source.is_file() or source.stat().st_size > MAX_SOURCE_BYTES:
            raise AdminError("The selected source must be a regular file no larger than 1 MB.")
        with source.open("rb") as handle:
            raw = handle.read(MAX_SOURCE_BYTES + 1)
        if len(raw) > MAX_SOURCE_BYTES:
            raise AdminError("The selected source exceeds 1 MB.")
        content = raw.decode("utf-8-sig")
    except (OSError, UnicodeError):
        raise AdminError("The selected source is unreadable or is not valid UTF-8 text.") from None
    chosen_title = source.stem if title is None else title.strip()
    if not chosen_title.strip() or len(chosen_title) > 200 or "\x00" in chosen_title:
        raise AdminError("Document title must contain 1 to 200 characters.")
    if not content.strip() or "\x00" in content:
        raise AdminError("The selected source must contain nonempty text without NUL characters.")
    return chosen_title, content


def _fields(value: dict, allowed: tuple[str, ...]) -> dict:
    if not isinstance(value, dict):
        raise AdminError("The hub returned an unexpected response.")
    return {key: value[key] for key in allowed if key in value}


def _items(value: dict, key: str) -> list:
    if not isinstance(value, dict) or not isinstance(value.get(key), list):
        raise AdminError("The hub returned an unexpected response.")
    return value[key]


def _emit(value, token: str | None = None) -> None:
    def redact(item):
        if isinstance(item, str):
            return item.replace(token, "[credential redacted]") if token else item
        if isinstance(item, list):
            return [redact(part) for part in item]
        if isinstance(item, dict):
            return {key: redact(part) for key, part in item.items()}
        return item
    print(json.dumps(redact(value), ensure_ascii=False, indent=2))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("initialize", help="Create protected credentials in a new absolute data directory.")
    setup.add_argument("--data-dir", required=True)
    for command in ("import", "list", "search", "delete", "index"):
        subparser = commands.add_parser(command)
        subparser.add_argument("--client-config", required=True)
        if command == "import":
            subparser.add_argument("--file", required=True)
            subparser.add_argument("--title")
            subparser.add_argument("--visibility", choices=("private", "household"), default="private")
            subparser.add_argument("--child-allowed", action="store_true")
        elif command == "search":
            subparser.add_argument("--query", required=True)
            subparser.add_argument("--limit", type=int, choices=range(1, 11), default=5)
            subparser.add_argument('--semantic', action='store_true', help='Use installed local Nomic embeddings if available.')
        elif command in ("delete", "index"):
            subparser.add_argument("--document-id", required=True)
    return parser


def main(argv=None):
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "initialize":
            _emit(initialize(arguments.data_dir))
            return 0
        if arguments.command == "import":
            if arguments.child_allowed and arguments.visibility != "household":
                raise AdminError("--child-allowed requires --visibility household; it also exposes the document to shared guest clients.")
            title, content = _selected_text(arguments.file, arguments.title)
        client, token = _load_client(arguments.client_config)
        if arguments.command == "import":
            result = client.add_document(title, content, arguments.visibility, arguments.child_allowed)
            if (not isinstance(result, dict) or not isinstance(result.get("id"), str) or not result["id"]
                    or not isinstance(result.get("source_version"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", result["source_version"]) is None):
                raise AdminError("The hub did not return a valid document receipt; check its state before importing again.")
            _emit(_fields(result, ("id", "source_version")), token)
        elif arguments.command == "list":
            result = client.documents()
            _emit({"documents": [_fields(item, ("id", "title", "source_version", "visibility", "child_allowed"))
                                 for item in _items(result, "documents")]}, token)
        elif arguments.command == "search":
            if not arguments.query.strip() or len(arguments.query) > 2000:
                raise AdminError("Search query must contain 1 to 2000 characters.")
            result = search_documents(client, arguments.query, OllamaEmbeddings() if arguments.semantic else None, arguments.limit)
            _emit({"retrieval_mode": result.get('retrieval_mode', 'lexical'),
                   'semantic_status': result.get('semantic_status'),
                   "citations": [_fields(item, ("document_id", "chunk_id", "title", "excerpt", "source_version", "score"))
                                 for item in _items(result, "citations")]}, token)
        elif arguments.command == 'index':
            if not arguments.document_id.strip() or len(arguments.document_id) > 128:
                raise AdminError('Specify one document ID.')
            result = index_document(client, OllamaEmbeddings(), arguments.document_id)
            _emit(_fields(result, ('indexed', 'document_id', 'source_version', 'model_id', 'chunks', 'dimensions')), token)
        elif arguments.command == "delete":
            if not arguments.document_id.strip() or len(arguments.document_id) > 128:
                raise AdminError("Specify one document ID.")
            result = client.delete_document(arguments.document_id)
            _emit({"document_id": arguments.document_id, "deleted": result.get("deleted") is True}, token)
        return 0
    except (AdminError, HubError, EmbeddingError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
