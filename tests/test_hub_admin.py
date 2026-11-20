import json
import base64
import os
import subprocess
from pathlib import Path

import pytest

from tools import hub_admin


@pytest.mark.skipif(os.name != "nt", reason="Exercises the real Windows NTFS ACL boundary")
def test_real_windows_acl_restriction_preserves_only_owner_and_system(tmp_path):
    # Windows PowerShell launched from a PowerShell 7 host can inherit module
    # paths that make Security module autoload fail. Exercise the actual child
    # process and read the resulting ACL independently, rather than mocking it.
    target = tmp_path / "ACL [literal] ' $` test"
    target.mkdir()
    hub_admin._restrict_directory(target)
    script = r"""
$ErrorActionPreference = 'Stop'
$target = $env:EDITH_ACL_TEST_TARGET
$current = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$acl = [System.IO.Directory]::GetAccessControl($target)
$rules = $acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])
$expected = @($current.Value, 'S-1-5-18')
$valid = $rules.Count -eq 2
foreach ($rule in $rules) {
    $valid = $valid -and ($rule.IdentityReference.Value -in $expected) -and (-not $rule.IsInherited)
    $valid = $valid -and ($rule.AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Allow)
    $valid = $valid -and ($rule.FileSystemRights -eq [System.Security.AccessControl.FileSystemRights]::FullControl)
    $valid = $valid -and ($rule.InheritanceFlags -eq [System.Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit')
    $valid = $valid -and ($rule.PropagationFlags -eq [System.Security.AccessControl.PropagationFlags]::None)
}
$owner = $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
@{protected=$acl.AreAccessRulesProtected; owner_matches=($owner -eq $current.Value); exact_rules=$valid; rule_count=$rules.Count} | ConvertTo-Json -Compress
"""
    environment = os.environ.copy()
    environment["EDITH_ACL_TEST_TARGET"] = str(target)
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
         base64.b64encode(script.encode("utf-16-le")).decode("ascii")],
        env=environment, capture_output=True, text=True, timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "protected": True, "owner_matches": True, "exact_rules": True, "rule_count": 2,
    }


@pytest.fixture
def secured(monkeypatch):
    inspected = []

    def restrict(path):
        # No credentials may exist when the directory boundary is established.
        inspected.append((path, list(path.iterdir())))

    monkeypatch.setattr(hub_admin, "_restrict_directory", restrict)
    return inspected


def test_initialize_creates_distinct_owner_and_shared_credentials_without_printing(tmp_path, secured, capsys):
    directory = tmp_path / "new-edith-data"
    assert hub_admin.main(["initialize", "--data-dir", str(directory)]) == 0
    config = json.loads((directory / "hub-config.json").read_text())
    owner = json.loads((directory / "owner-client.json").read_text())
    desktop = json.loads((directory / "desktop-client.json").read_text())
    principals = {principal["id"]: principal for principal in config["principals"]}
    assert principals["owner"]["role"] == "adult"
    assert principals["shared"]["role"] == "guest"
    assert principals["owner"]["enabled"] is True and principals["shared"]["enabled"] is True
    assert owner["token"] == principals["owner"]["token"]
    assert desktop["token"] == principals["shared"]["token"]
    assert owner["token"] != desktop["token"]
    assert len(owner["token"]) >= 43 and len(desktop["token"]) >= 43
    assert owner["url"] == desktop["url"] == "http://127.0.0.1:8788"
    assert config["bind"] == "127.0.0.1:8788"
    assert Path(config["database_path"]) == directory / "knowledge.sqlite3"
    assert not (directory / "knowledge.sqlite3").exists()
    assert secured == [(directory, [])]
    output = capsys.readouterr()
    assert "desktop-client.json" in output.out
    assert owner["token"] not in output.out + output.err
    assert desktop["token"] not in output.out + output.err


@pytest.mark.parametrize("existing_name", ["hub-config.json", "knowledge.sqlite3", "unrelated.txt"])
def test_initialize_refuses_existing_directory_without_altering_files(tmp_path, secured, capsys, existing_name):
    existing = tmp_path / "existing"
    existing.mkdir()
    original = existing / existing_name
    original.write_bytes(b"preserve exactly")
    assert hub_admin.main(["initialize", "--data-dir", str(existing)]) == 2
    assert original.read_bytes() == b"preserve exactly"
    assert list(existing.iterdir()) == [original]
    assert secured == []
    assert "already exists" in capsys.readouterr().err


def test_initialize_requires_absolute_target(secured, capsys):
    assert hub_admin.main(["initialize", "--data-dir", "relative-data"]) == 2
    assert "absolute" in capsys.readouterr().err
    assert secured == []


def test_acl_failure_prevents_credentials_being_written(tmp_path, monkeypatch, capsys):
    target = tmp_path / "denied"

    def deny(_):
        raise hub_admin.AdminError("Could not restrict directory permissions.")

    monkeypatch.setattr(hub_admin, "_restrict_directory", deny)
    assert hub_admin.main(["initialize", "--data-dir", str(target)]) == 2
    assert not (target / "hub-config.json").exists()
    assert not (target / "owner-client.json").exists()
    assert not (target / "desktop-client.json").exists()
    assert "permissions" in capsys.readouterr().err


@pytest.fixture
def hub(tmp_path, monkeypatch):
    config = tmp_path / "owner-client.json"
    config.write_text(json.dumps({"url": "http://127.0.0.1:8788", "token": "synthetic-client-credential-" + "x" * 32}))
    calls = []

    class Client:
        def __init__(self, url, token):
            assert url == "http://127.0.0.1:8788"
            self.token = token

        def add_document(self, title, content, visibility="private", child_allowed=False):
            calls.append(("add", title, content, visibility, child_allowed))
            return {"id": "document-123", "source_version": "a" * 64, "content": content, "token": self.token}

        def documents(self):
            return {"documents": [{"id": "document-123", "title": "Fixture", "source_version": "a" * 64,
                                   "visibility": "private", "content": "do not print raw document"}]}

        def search(self, query, limit=5):
            calls.append(("search", query, limit))
            return {"citations": [{"document_id": "document-123", "chunk_id": "document-123:0",
                                   "title": "Fixture", "source_version": "a" * 64,
                                   "excerpt": "Selected matching passage"}]}

        def delete_document(self, document_id):
            calls.append(("delete", document_id))
            return {"deleted": True}

    monkeypatch.setattr(hub_admin, "HubClient", Client)
    return config, calls


def test_import_reads_only_selected_text_and_defaults_to_private(tmp_path, hub, capsys):
    config, calls = hub
    selected = tmp_path / "House manual.md"
    selected.write_bytes("వెలుగు schedule\nWater plants on Sunday.".encode("utf-8"))
    (tmp_path / "ignored.txt").write_text("not selected")
    assert hub_admin.main(["import", "--client-config", str(config), "--file", str(selected)]) == 0
    assert calls == [("add", "House manual", "వెలుగు schedule\nWater plants on Sunday.", "private", False)]
    output = capsys.readouterr().out
    assert "document-123" in output and "a" * 64 in output
    assert "Water plants" not in output and "synthetic-client-credential" not in output


def test_explicit_household_child_sharing_is_forwarded(tmp_path, hub):
    config, calls = hub
    selected = tmp_path / "fixture.txt"
    selected.write_text("shared material")
    assert hub_admin.main(["import", "--client-config", str(config), "--file", str(selected),
                           "--title", "Child-approved fixture", "--visibility", "household", "--child-allowed"]) == 0
    assert calls == [("add", "Child-approved fixture", "shared material", "household", True)]


def test_child_sharing_rejected_for_private_document(tmp_path, hub):
    config, calls = hub
    selected = tmp_path / "fixture.txt"
    selected.write_text("private")
    assert hub_admin.main(["import", "--client-config", str(config), "--file", str(selected), "--child-allowed"]) == 2
    assert calls == []


@pytest.mark.parametrize("name, content", [("bad.pdf", b"pdf"), ("bad.txt", b"\xff"), ("empty.txt", b" "),
                                           ("large.md", b"x" * 1_000_001)])
def test_unsupported_empty_invalid_or_large_source_never_reaches_hub(tmp_path, hub, name, content):
    config, calls = hub
    selected = tmp_path / name
    selected.write_bytes(content)
    assert hub_admin.main(["import", "--client-config", str(config), "--file", str(selected)]) == 2
    assert calls == []


def test_directory_cannot_be_imported_as_a_file(tmp_path, hub):
    config, calls = hub
    assert hub_admin.main(["import", "--client-config", str(config), "--file", str(tmp_path)]) == 2
    assert calls == []


def test_list_search_and_delete_use_explicit_requests(hub, capsys):
    config, calls = hub
    assert hub_admin.main(["list", "--client-config", str(config)]) == 0
    assert "do not print raw document" not in capsys.readouterr().out
    assert hub_admin.main(["search", "--client-config", str(config), "--query", "plant schedule", "--limit", "3"]) == 0
    assert "Selected matching passage" in capsys.readouterr().out
    assert hub_admin.main(["delete", "--client-config", str(config), "--document-id", "document-123"]) == 0
    assert calls == [("search", "plant schedule", 3), ("delete", "document-123")]


def test_initialization_write_failure_removes_partial_credentials(tmp_path, secured, monkeypatch):
    target = tmp_path / "write-failed"

    def disk_full(_):
        raise OSError("synthetic full disk")

    monkeypatch.setattr(hub_admin.os, "fsync", disk_full)
    assert hub_admin.main(["initialize", "--data-dir", str(target)]) == 2
    assert not target.exists()


def test_import_requires_document_receipt_not_empty_success(tmp_path, hub, monkeypatch):
    config, _ = hub
    source = tmp_path / "selected.txt"
    source.write_text("selected text")
    monkeypatch.setattr(hub_admin.HubClient, "add_document", lambda *args: {})
    assert hub_admin.main(["import", "--client-config", str(config), "--file", str(source)]) == 2


def test_missing_list_shape_is_not_reported_as_empty_library(hub, monkeypatch):
    config, _ = hub
    monkeypatch.setattr(hub_admin.HubClient, "documents", lambda _: {})
    assert hub_admin.main(["list", "--client-config", str(config)]) == 2


def test_credentials_are_redacted_even_if_reflected_in_returned_metadata(hub, monkeypatch, capsys):
    config, _ = hub
    token = json.loads(config.read_text())["token"]
    monkeypatch.setattr(hub_admin.HubClient, "documents", lambda _: {"documents": [
        {"id": "document-123", "title": "reflected " + token, "source_version": "a" * 64}]})
    assert hub_admin.main(["list", "--client-config", str(config)]) == 0
    output = capsys.readouterr().out
    assert token not in output and "credential redacted" in output


def test_index_selects_one_existing_document_without_reimport(hub, monkeypatch, capsys):
    config, calls = hub
    indexed = []
    monkeypatch.setattr(hub_admin, 'OllamaEmbeddings', lambda: object(), raising=False)

    def index(client, model, document_id):
        indexed.append(document_id)
        return {'indexed': True, 'document_id': document_id, 'chunks': 2, 'model_id': 'local-digest'}

    monkeypatch.setattr(hub_admin, 'index_document', index, raising=False)
    assert hub_admin.main(['index', '--client-config', str(config), '--document-id', 'document-123']) == 0
    assert calls == [] and indexed == ['document-123']
    assert json.loads(capsys.readouterr().out)['indexed'] is True
