import hashlib
import json
from pathlib import Path

import pytest

from brain.coding import CodingError, coding_tools, workspace_from_env
from connectors.workspace import Workspace, WorkspaceError
from skills import REGISTRY


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr("connectors.workspace._protect_directory", lambda _: None)
    monkeypatch.delenv("EDITH_WORKSPACE_CONFIG", raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_bytes(b"first line\nneedle on line two\nlast line\n")
    return Workspace(root, tmp_path / "stage", "owner", "project-a"), root


def test_coding_registry_contains_only_isolated_local_read_search_and_proposal_tools(project):
    service, _ = project
    before = dict(REGISTRY)
    registry = coding_tools(service)
    assert set(registry) == {"read_project_file", "search_project", "propose_project_patch"}
    assert REGISTRY == before
    assert all(tool.local_only and not tool.confirm for tool in registry.values())
    assert registry["read_project_file"].schema["function"]["parameters"]["properties"]["start_line"] == {"type": "integer"}


def test_project_read_returns_requested_lines_and_full_source_hash(project):
    service, _ = project
    result = json.loads(coding_tools(service)["read_project_file"].func("main.py", 2, 1))
    assert result["content"] == "needle on line two\n"
    assert result["sha256"] == hashlib.sha256(b"first line\nneedle on line two\nlast line\n").hexdigest()
    assert result["start_line"] == 2 and result["total_lines"] == 3
    assert result["truncated"] is True
    assert result["principal_id"] == "owner" and result["project_id"] == "project-a"


def test_read_bounds_json_output_even_for_long_escape_heavy_lines(project):
    service, root = project
    original = ('"\\\t' * 40_000).encode()
    (root / "long.txt").write_bytes(original)
    output = coding_tools(service)["read_project_file"].func("long.txt")
    result = json.loads(output)
    assert len(output) <= 12_000
    assert result["truncated"] is True and result["content_truncated"] is True
    assert original.decode().startswith(result["content"])
    assert result["sha256"] == hashlib.sha256(original).hexdigest()


@pytest.mark.parametrize("start,count", [(0, 1), (-1, 1), (True, 1), (1, False), (1, 0), (1, 201), ("1", 10)])
def test_read_rejects_invalid_line_ranges_without_source_access(project, start, count, monkeypatch):
    service, _ = project
    monkeypatch.setattr(service, "source_snapshot", lambda *_: pytest.fail("Invalid range reached source access"))
    with pytest.raises(CodingError):
        coding_tools(service)["read_project_file"].func("main.py", start, count)


def test_search_uses_scoped_sources_and_real_line_references(project):
    service, root = project
    (root / ".env").write_bytes(b"needle private")
    result = json.loads(coding_tools(service)["search_project"].func("needle"))
    assert result["matches"] == [{"path": "main.py", "line": 2, "text": "needle on line two"}]
    assert "private" not in str(result)


def test_search_bounds_total_output_with_explicit_truncation(project):
    service, root = project
    (root / "many.txt").write_bytes((("needle " + "x" * 490 + "\n") * 50).encode())
    output = coding_tools(service)["search_project"].func("needle")
    result = json.loads(output)
    assert len(output) <= 12_000 and result["truncated"] is True
    assert 0 < len(result["matches"]) < 50


def test_patch_tool_stages_exact_reviewable_receipt_without_applying(project):
    service, root = project
    original = (root / "main.py").read_bytes()
    baseline = hashlib.sha256(original).hexdigest()
    output = coding_tools(service)["propose_project_patch"].func("main.py", baseline, "print('review me')\n")
    result = json.loads(output)
    assert result["status"] == "awaiting_review" and result["base_sha256"] == baseline
    assert "+print('review me')" in result["diff"]
    assert (root / "main.py").read_bytes() == original
    assert (service.staging_dir / result["id"] / "proposal.json").is_file()
    assert len(result["proposal_hash"]) == 64 and result["truncated"] is False


def test_patch_tool_bounds_diff_and_retains_full_replacement_hash(project):
    service, root = project
    baseline = hashlib.sha256((root / "main.py").read_bytes()).hexdigest()
    replacement = "reviewed line\n" * 10_000
    output = coding_tools(service)["propose_project_patch"].func("main.py", baseline, replacement)
    result = json.loads(output)
    assert len(output) <= 12_000 and result["truncated"] is True
    assert result["new_sha256"] == hashlib.sha256(replacement.encode()).hexdigest()
    assert result["diff_truncated"] is True


def test_model_tool_cannot_bypass_workspace_path_policy(project):
    service, root = project
    (root / "credentials.json").write_bytes(b"synthetic credential")
    with pytest.raises((CodingError, WorkspaceError)):
        coding_tools(service)["read_project_file"].func("credentials.json")


def config_file(tmp_path, monkeypatch, *, principal="owner", role="adult", **changes):
    root = tmp_path / "configured-repo"
    root.mkdir(exist_ok=True)
    values = {"root": str(root), "staging_dir": str(tmp_path / "configured-stage"),
              "principal_id": principal, "project_id": "project-a", **changes}
    config = tmp_path / "workspace.json"
    config.write_text(json.dumps(values), encoding="utf-8")
    monkeypatch.setenv("EDITH_WORKSPACE_CONFIG", str(config))
    return config, values, {"principal_id": "owner", "role": role, "capabilities": {}}


def test_optional_workspace_is_disabled_without_configuration(monkeypatch):
    monkeypatch.delenv("EDITH_WORKSPACE_CONFIG", raising=False)
    assert workspace_from_env({"principal_id": "owner", "role": "adult"}) is None


@pytest.mark.parametrize("role", ["adult", "elder"])
def test_configured_workspace_binds_exact_trusted_identity_and_project(project, tmp_path, monkeypatch, role):
    _, values, identity = config_file(tmp_path, monkeypatch, role=role)
    service = workspace_from_env(identity)
    assert isinstance(service, Workspace)
    assert service.root == Path(values["root"])
    assert service.principal_id == "owner" and service.project_id == "project-a"


@pytest.mark.parametrize("role", ["guest", "child", "admin", None])
def test_non_adult_identity_cannot_load_or_create_workspace(project, tmp_path, monkeypatch, role):
    _, values, identity = config_file(tmp_path, monkeypatch, role=role)
    with pytest.raises(CodingError):
        workspace_from_env(identity)
    assert not Path(values["staging_dir"]).exists()


def test_principal_mismatch_fails_before_staging_creation(project, tmp_path, monkeypatch):
    _, values, identity = config_file(tmp_path, monkeypatch, principal="someone-else")
    with pytest.raises(CodingError):
        workspace_from_env(identity)
    assert not Path(values["staging_dir"]).exists()


@pytest.mark.parametrize("change", [{"root": "relative/repo"}, {"staging_dir": "relative/stage"},
                                     {"project_id": "../bad"}, {"unexpected": "field"}, {"root": None}])
def test_invalid_config_is_rejected_without_partial_workspace_creation(project, tmp_path, monkeypatch, change):
    _, values, identity = config_file(tmp_path, monkeypatch, **change)
    with pytest.raises(CodingError):
        workspace_from_env(identity)
    assert not (tmp_path / "configured-stage").exists()


def test_config_path_must_be_absolute_and_json_bounded(project, tmp_path, monkeypatch):
    monkeypatch.setenv("EDITH_WORKSPACE_CONFIG", "relative.json")
    with pytest.raises(CodingError):
        workspace_from_env({"principal_id": "owner", "role": "adult"})
    config, values, identity = config_file(tmp_path, monkeypatch)
    config.write_bytes(b" " * 16_385)
    with pytest.raises(CodingError):
        workspace_from_env(identity)
    assert not Path(values["staging_dir"]).exists()


def test_duplicate_config_keys_are_rejected(project, tmp_path, monkeypatch):
    config, values, identity = config_file(tmp_path, monkeypatch)
    config.write_text(json.dumps(values)[:-1] + ',"principal_id":"owner"}', encoding="utf-8")
    with pytest.raises(CodingError):
        workspace_from_env(identity)
    assert not Path(values["staging_dir"]).exists()
