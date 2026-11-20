import base64
import ctypes
import hashlib
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from connectors.workspace import Workspace, WorkspaceError


ORIGINAL = "print('original')\n"
REPLACEMENT = "print('reviewed change')\n"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr("connectors.workspace._protect_directory", lambda _: None)
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_bytes(ORIGINAL.encode())
    (root / "README.md").write_bytes(b"Read this project.\n")
    (root / ".env").write_bytes(b"needle-secret-do-not-read")
    (root / "data").mkdir()
    (root / "data" / "private.txt").write_bytes(b"needle-secret-do-not-read")
    (root / "src" / "token.json").write_bytes(b"needle-secret-do-not-read")
    return Workspace(root, tmp_path / "stage", "owner", "project-a"), root


def test_snapshot_reads_real_source_with_exact_byte_hash(workspace):
    service, _ = workspace
    snapshot = service.source_snapshot("src/main.py")
    assert snapshot["content"] == ORIGINAL
    assert snapshot["sha256"] == hashlib.sha256(ORIGINAL.encode()).hexdigest()
    assert snapshot["principal_id"] == "owner" and snapshot["project_id"] == "project-a"


@pytest.mark.parametrize("path", [".env", "data/private.txt", "src/token.json", "../README.md", "/README.md",
                                  "C:/Windows/test.txt", "src\\main.py", "src/main.py:secret", "src/../README.md",
                                  "src//main.py", "src/CON.py", "src/LPT1.txt", "src/main.py.",
                                  "node_modules/code.js", ".git/config", "build/file.py", "artifact/file.py"])
def test_private_and_escaping_paths_are_rejected(workspace, path):
    service, _ = workspace
    with pytest.raises(WorkspaceError):
        service.source_snapshot(path)


def test_snapshot_rejects_oversized_and_non_utf8_files(workspace):
    service, root = workspace
    (root / "large.txt").write_bytes(b"x" * 256_001)
    (root / "binary.txt").write_bytes(b"\xff")
    for path in ("large.txt", "binary.txt"):
        with pytest.raises(WorkspaceError):
            service.source_snapshot(path)


def test_search_reports_real_line_references_without_reading_private_files(workspace, monkeypatch):
    service, root = workspace
    (root / "src" / "search.py").write_bytes(b"first line\nneedle public\nlast line\n")
    original_open = service._read_regular

    def guarded(path):
        assert path.name not in {".env", "private.txt", "token.json"}
        return original_open(path)

    monkeypatch.setattr(service, "_read_regular", guarded)
    result = service.search("needle")
    assert result["matches"] == [{"path": "src/search.py", "line": 2, "text": "needle public"}]
    assert "needle-secret" not in str(result)


def test_prepare_stages_review_diff_without_modifying_source(workspace):
    service, root = workspace
    before = service.source_snapshot("src/main.py")
    proposal = service.prepare_patch("src/main.py", before["sha256"], REPLACEMENT)
    assert (root / "src" / "main.py").read_text() == ORIGINAL
    assert "-print('original')" in proposal["diff"] and "+print('reviewed change')" in proposal["diff"]
    assert proposal["new_sha256"] == hashlib.sha256(REPLACEMENT.encode()).hexdigest()
    assert proposal["principal_id"] == "owner" and proposal["project_id"] == "project-a"
    assert len(proposal["proposal_hash"]) == 64


def test_prepare_rejects_changed_source(workspace):
    service, root = workspace
    before = service.source_snapshot("src/main.py")
    (root / "src" / "main.py").write_bytes(b"concurrent edit\n")
    with pytest.raises(WorkspaceError):
        service.prepare_patch("src/main.py", before["sha256"], REPLACEMENT)
    assert (root / "src" / "main.py").read_bytes() == b"concurrent edit\n"


def test_approved_patch_applies_atomically_and_returns_exact_receipt(workspace):
    service, root = workspace
    before = service.source_snapshot("src/main.py")
    proposal = service.prepare_patch("src/main.py", before["sha256"], REPLACEMENT)
    result = service.apply_patch(proposal["id"], proposal["proposal_hash"], before["sha256"])
    assert (root / "src" / "main.py").read_bytes() == REPLACEMENT.encode()
    assert result["status"] == "applied" and result["new_sha256"] == proposal["new_sha256"]
    assert result["proposal_hash"] == proposal["proposal_hash"]
    with pytest.raises(WorkspaceError):
        service.apply_patch(proposal["id"], proposal["proposal_hash"], before["sha256"])


def test_apply_refuses_stale_baseline_without_overwriting(workspace):
    service, root = workspace
    before = service.source_snapshot("src/main.py")
    proposal = service.prepare_patch("src/main.py", before["sha256"], REPLACEMENT)
    (root / "src" / "main.py").write_bytes(b"concurrent edit\n")
    with pytest.raises(WorkspaceError):
        service.apply_patch(proposal["id"], proposal["proposal_hash"], before["sha256"])
    assert (root / "src" / "main.py").read_bytes() == b"concurrent edit\n"


def test_changed_proposal_hash_cannot_authorize_an_apply(workspace):
    service, root = workspace
    before = service.source_snapshot("src/main.py")
    proposal = service.prepare_patch("src/main.py", before["sha256"], REPLACEMENT)
    with pytest.raises(WorkspaceError):
        service.apply_patch(proposal["id"], "0" * 64, before["sha256"])
    assert (root / "src" / "main.py").read_bytes() == ORIGINAL.encode()


def test_staged_content_tampering_is_rejected(workspace):
    service, root = workspace
    before = service.source_snapshot("src/main.py")
    proposal = service.prepare_patch("src/main.py", before["sha256"], REPLACEMENT)
    (service.staging_dir / proposal["id"] / "replacement.utf8").write_bytes(b"tampered")
    with pytest.raises(WorkspaceError):
        service.apply_patch(proposal["id"], proposal["proposal_hash"], before["sha256"])
    assert (root / "src" / "main.py").read_bytes() == ORIGINAL.encode()


def test_staging_directory_cannot_be_inside_source_or_rebound_to_other_identity(workspace):
    service, root = workspace
    with pytest.raises(WorkspaceError):
        Workspace(root, root / "staging", "owner", "project-a")
    with pytest.raises(WorkspaceError):
        Workspace(root, service.staging_dir, "someone-else", "project-a")


def test_source_symlink_cannot_escape_root(workspace, tmp_path):
    service, root = workspace
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"private content")
    try:
        (root / "linked.py").symlink_to(outside)
    except OSError:
        pytest.skip("This Windows account cannot create test symlinks")
    with pytest.raises(WorkspaceError):
        service.source_snapshot("linked.py")


def test_windows_reparse_source_is_rejected_before_read(workspace, monkeypatch):
    service, root = workspace
    target = root / "src" / "main.py"
    original = Path.lstat

    def lstat(path, *args, **kwargs):
        metadata = original(path, *args, **kwargs)
        if path == target:
            return SimpleNamespace(st_mode=metadata.st_mode, st_file_attributes=0x400)
        return metadata

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(WorkspaceError):
        service.source_snapshot("src/main.py")


def test_worker_snapshot_copies_only_explicit_allowed_sources(workspace):
    service, _ = workspace
    snapshot = service.create_worker_snapshot(["src/main.py", "README.md"])
    copied = Path(snapshot["directory"])
    assert (copied / "src" / "main.py").read_bytes() == ORIGINAL.encode()
    assert sorted(item["path"] for item in snapshot["files"]) == ["README.md", "src/main.py"]
    assert not (copied / ".env").exists() and not (copied / "data").exists()
    assert snapshot["total_bytes"] == len(ORIGINAL.encode()) + len(b"Read this project.\n")


def test_worker_snapshot_rejects_private_sources_and_duplicates(workspace):
    service, _ = workspace
    for paths in [[".env"], ["src/main.py", "src/main.py"]]:
        with pytest.raises(WorkspaceError):
            service.create_worker_snapshot(paths)


@pytest.mark.parametrize("name", ["id_rsa.txt", "id_ed25519.json", "privatekey.json"])
def test_private_key_names_are_rejected_even_with_allowed_extensions(workspace, name):
    service, root = workspace
    (root / name).write_bytes(b"synthetic private key material")
    with pytest.raises(WorkspaceError):
        service.source_snapshot(name)


def test_review_diff_marks_missing_final_newlines(workspace):
    service, root = workspace
    (root / "src" / "main.py").write_bytes(b"original")
    before = service.source_snapshot("src/main.py")
    proposal = service.prepare_patch("src/main.py", before["sha256"], "replacement")
    assert "-original\n\\ No newline at end of file\n+replacement\n" in proposal["diff"]


def test_large_short_line_diff_can_still_be_reviewed_and_applied(workspace):
    service, root = workspace
    original = "a\n" * 128_000
    replacement = "b\n" * 128_000
    (root / "src" / "main.py").write_bytes(original.encode())
    before = service.source_snapshot("src/main.py")
    proposal = service.prepare_patch("src/main.py", before["sha256"], replacement)
    result = service.apply_patch(proposal["id"], proposal["proposal_hash"], before["sha256"])
    assert result["status"] == "applied"
    assert (root / "src" / "main.py").read_bytes() == replacement.encode()


def test_worker_snapshot_enforces_combined_content_limit_before_staging(workspace, monkeypatch):
    service, root = workspace
    (root / "a.txt").write_bytes(b"a" * 10)
    (root / "b.txt").write_bytes(b"b" * 11)
    monkeypatch.setattr("connectors.workspace.MAX_SNAPSHOT_BYTES", 20)
    before = set(service.staging_dir.iterdir())
    with pytest.raises(WorkspaceError):
        service.create_worker_snapshot(["a.txt", "b.txt"])
    assert set(service.staging_dir.iterdir()) == before


def test_search_caps_matches_and_files_inspected(workspace, monkeypatch):
    service, root = workspace
    (root / "a.txt").write_bytes(b"needle\nneedle\nneedle\n")
    monkeypatch.setattr("connectors.workspace.MAX_MATCHES", 2)
    result = service.search("needle")
    assert [item["line"] for item in result["matches"]] == [1, 2]
    assert result["truncated"] is True
    monkeypatch.setattr("connectors.workspace.MAX_FILES", 1)
    result = service.search("not present")
    assert result["files_scanned"] == 1 and result["truncated"] is True


def test_failed_receipt_after_replace_is_explicitly_unknown_and_single_use(workspace, monkeypatch):
    import connectors.workspace as module
    service, root = workspace
    before = service.source_snapshot("src/main.py")
    proposal = service.prepare_patch("src/main.py", before["sha256"], REPLACEMENT)
    original_write = module._write_new

    def disk_failure(path, data):
        if path.name == "result.json":
            raise OSError("synthetic disk failure")
        original_write(path, data)

    monkeypatch.setattr(module, "_write_new", disk_failure)
    with pytest.raises(WorkspaceError) as failure:
        service.apply_patch(proposal["id"], proposal["proposal_hash"], before["sha256"])
    assert getattr(failure.value, "status", None) == "unknown"
    assert (root / "src" / "main.py").read_bytes() == REPLACEMENT.encode()
    (root / "src" / "main.py").write_bytes(ORIGINAL.encode())
    with pytest.raises(WorkspaceError):
        service.apply_patch(proposal["id"], proposal["proposal_hash"], before["sha256"])
    assert (root / "src" / "main.py").read_bytes() == ORIGINAL.encode()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction boundary")
def test_real_windows_junction_is_not_read_or_searched(workspace, tmp_path):
    service, root = workspace
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_bytes(b"private sentinel needle")
    junction = root / "linked"
    environment = {**os.environ, "EDITH_TEST_LINK": str(junction), "EDITH_TEST_TARGET": str(outside)}
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                             "New-Item -ItemType Junction -Path $env:EDITH_TEST_LINK -Target $env:EDITH_TEST_TARGET | Out-Null"],
                            env=environment, capture_output=True, timeout=15)
    assert result.returncode == 0, "Could not create synthetic junction fixture"
    try:
        with pytest.raises(WorkspaceError):
            service.source_snapshot("linked/private.txt")
        assert service.search("private sentinel")["matches"] == []
    finally:
        junction.rmdir()
    assert (outside / "private.txt").read_bytes() == b"private sentinel needle"


def synthetic_alias(monkeypatch, root, alias, canonical):
    alias_path, canonical_path = root / alias, root / canonical
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    alias_path.write_bytes(b"synthetic alternate spelling")
    canonical_path.write_bytes(b"synthetic canonical contents")
    original = Path.resolve

    def resolve(path, *args, **kwargs):
        return canonical_path if path == alias_path else original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    return alias, canonical


@pytest.mark.parametrize("alias,canonical", [("CREDEN~1.JSON", "credentials-record.json"),
                                             ("CODE~1.PY", "data/code.py"), ("SOURCE~1.PY", "not-source.exe")])
def test_short_name_cannot_bypass_canonical_private_filename_policy(workspace, monkeypatch, alias, canonical):
    service, root = workspace
    alias, _ = synthetic_alias(monkeypatch, root, alias, canonical)
    monkeypatch.setattr(service, "_read_regular", lambda *_: pytest.fail("Forbidden canonical source reached a content read"))
    with pytest.raises(WorkspaceError):
        service.source_snapshot(alias)


def test_allowed_alias_reads_and_reports_the_validated_canonical_source(workspace, monkeypatch):
    service, root = workspace
    alias, canonical = synthetic_alias(monkeypatch, root, "LONGFI~1.PY", "longfilename.py")
    result = service.source_snapshot(alias)
    assert result["path"] == canonical
    assert result["content"] == "synthetic canonical contents"
    proposal = service.prepare_patch(alias, result["sha256"], "synthetic change")
    assert proposal["path"] == canonical and "a/longfilename.py" in proposal["diff"]


def test_worker_snapshot_rejects_alias_spelling_before_any_source_read(workspace, monkeypatch):
    service, root = workspace
    alias, _ = synthetic_alias(monkeypatch, root, "LONGFI~1.PY", "longfilename.py")
    monkeypatch.setattr(service, "_read_regular", lambda *_: pytest.fail("Ambiguous snapshot source was read"))
    with pytest.raises(WorkspaceError):
        service.create_worker_snapshot([alias])


@pytest.mark.skipif(os.name != "nt", reason="Real NTFS short-name boundary")
@pytest.mark.parametrize("filename", ["credentials-record.json", "longfilename.py"])
def test_real_ntfs_alias_obeys_canonical_source_policy(workspace, filename):
    service, root = workspace
    source = root / filename
    source.write_bytes(b"synthetic file only")
    buffer = ctypes.create_unicode_buffer(32768)
    get_short_path = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
    get_short_path.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    get_short_path.restype = ctypes.c_uint32
    count = get_short_path(str(source), buffer, len(buffer))
    assert 0 < count < len(buffer), "Could not query the synthetic file's short path"
    alias = Path(buffer.value).name
    if alias.casefold() == filename.casefold():
        pytest.skip("This temporary volume does not assign NTFS 8.3 aliases")
    if filename.startswith("credentials"):
        with pytest.raises(WorkspaceError):
            service.source_snapshot(alias)
    else:
        result = service.source_snapshot(alias)
        assert result["path"] == filename and result["content"] == "synthetic file only"
        with pytest.raises(WorkspaceError):
            service.create_worker_snapshot([alias])


def change_synthetic_acl(path, *, inherit=False):
    script = r"""
$ErrorActionPreference = 'Stop'
$target = $env:EDITH_TEST_ACL_PATH
$directory = [System.IO.Directory]::Exists($target)
if ($directory) { $acl = [System.IO.Directory]::GetAccessControl($target) }
else { $acl = [System.IO.File]::GetAccessControl($target) }
if ($env:EDITH_TEST_ENABLE_INHERITANCE -eq '1') {
    $acl.SetAccessRuleProtection($false, $true)
} else {
    $everyone = [System.Security.Principal.SecurityIdentifier]::new('S-1-1-0')
    $rule = [System.Security.AccessControl.FileSystemAccessRule]::new($everyone, 'Read', 'Allow')
    $acl.AddAccessRule($rule)
}
if ($directory) { [System.IO.Directory]::SetAccessControl($target, $acl) }
else { [System.IO.File]::SetAccessControl($target, $acl) }
"""
    environment = {**os.environ, "EDITH_TEST_ACL_PATH": str(path), "EDITH_TEST_ENABLE_INHERITANCE": "1" if inherit else "0"}
    command = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", command],
                            env=environment, capture_output=True, timeout=20)
    assert result.returncode == 0, "Could not modify the synthetic staging ACL fixture"


@pytest.mark.skipif(os.name != "nt", reason="Real Windows staging ACLs")
@pytest.mark.parametrize("target", ["root_inheritance", "proposal_file", "snapshot_file"])
def test_reopen_rejects_permissive_existing_directory_or_leaf_acl(tmp_path, target):
    root = tmp_path / "acl-repo"
    root.mkdir()
    (root / "main.py").write_bytes(b"synthetic source\n")
    service = Workspace(root, tmp_path / "acl-stage", "owner", "project-a")
    baseline = service.source_snapshot("main.py")
    proposal = service.prepare_patch("main.py", baseline["sha256"], "synthetic replacement\n")
    snapshot = service.create_worker_snapshot(["main.py"])
    # A legitimate protected stage and inherited protected artifacts can be reopened.
    reopened = Workspace(root, service.staging_dir, "owner", "project-a")
    assert reopened.source_snapshot("main.py")["sha256"] == baseline["sha256"]
    if target == "root_inheritance":
        change_synthetic_acl(service.staging_dir, inherit=True)
    elif target == "proposal_file":
        change_synthetic_acl(service.staging_dir / proposal["id"] / "replacement.utf8")
    else:
        change_synthetic_acl(Path(snapshot["directory"]) / "main.py")
    with pytest.raises(WorkspaceError):
        Workspace(root, service.staging_dir, "owner", "project-a")


@pytest.mark.parametrize("artifact", ["unexpected.txt", "a" * 32 + "/unexpected.bin"])
def test_reopened_stage_rejects_unknown_artifact_before_acl_or_content_read(workspace, monkeypatch, artifact):
    service, root = workspace
    target = service.staging_dir / artifact
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"synthetic unexpected artifact")
    monkeypatch.setattr("connectors.workspace._verify_permissions", lambda *_: pytest.fail("Unexpected inventory reached ACL verification"), raising=False)
    with pytest.raises(WorkspaceError):
        Workspace(root, service.staging_dir, "owner", "project-a")


def test_reopened_stage_inventory_is_bounded(workspace, monkeypatch):
    service, root = workspace
    baseline = service.source_snapshot("src/main.py")
    service.prepare_patch("src/main.py", baseline["sha256"], REPLACEMENT)
    monkeypatch.setattr("connectors.workspace.MAX_STAGE_ENTRIES", 3, raising=False)
    monkeypatch.setattr("connectors.workspace._verify_permissions", lambda *_: pytest.fail("Oversized inventory reached ACL verification"), raising=False)
    with pytest.raises(WorkspaceError):
        Workspace(root, service.staging_dir, "owner", "project-a")


def test_reopened_stage_rejects_linked_artifact_before_acl_or_content_read(workspace, monkeypatch):
    service, root = workspace
    baseline = service.source_snapshot("src/main.py")
    proposal = service.prepare_patch("src/main.py", baseline["sha256"], REPLACEMENT)
    target = service.staging_dir / proposal["id"] / "replacement.utf8"
    original = Path.lstat

    def lstat(path, *args, **kwargs):
        metadata = original(path, *args, **kwargs)
        if path == target:
            return SimpleNamespace(st_mode=metadata.st_mode, st_file_attributes=0x400)
        return metadata

    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr("connectors.workspace._verify_permissions", lambda *_: pytest.fail("Linked inventory reached ACL verification"), raising=False)
    with pytest.raises(WorkspaceError):
        Workspace(root, service.staging_dir, "owner", "project-a")
