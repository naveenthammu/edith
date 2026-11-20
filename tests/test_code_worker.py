from pathlib import Path

import pytest

from connectors.workspace import Workspace
from tools.code_worker import WorkerError, execution_status, plan_docker_invocation


IMAGE = "python@sha256:" + "a" * 64


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.setattr("connectors.workspace._protect_directory", lambda _: None)
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_bytes(b"print('synthetic source')\n")
    service = Workspace(root, tmp_path / "stage", "owner", "project-a")
    snapshot = service.create_worker_snapshot(["main.py"])
    return service, snapshot


def test_status_never_promises_execution_from_cli_presence(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda command: "C:/synthetic/" + command)
    status = execution_status()
    assert status["available"] is False
    assert status["status"] == "unavailable"
    assert status["runtime_verified"] is False
    assert status["host_fallback"] is False


def test_status_reports_missing_tools_without_starting_them(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    status = execution_status()
    assert status["available"] is False
    assert status["detected_tools"] == []


def test_plan_contains_only_verified_selected_snapshot_mount_and_bounded_isolation(prepared):
    service, snapshot = prepared
    plan = plan_docker_invocation(service, snapshot["id"], snapshot["snapshot_hash"], IMAGE, ["python", "main.py"])
    argv = plan["argv"]
    assert plan["status"] == "planned_not_executed" and plan["execution_available"] is False
    assert argv[:3] == ["docker", "run", "--rm"]
    for flag, value in [("--network", "none"), ("--cap-drop", "ALL"),
                        ("--security-opt", "no-new-privileges"), ("--pull", "never"),
                        ("--pids-limit", "64"), ("--memory", "512m"), ("--cpus", "1"),
                        ("--user", "65534:65534"), ("--workdir", "/workspace")]:
        assert argv[argv.index(flag) + 1] == value
    assert "--read-only" in argv
    assert argv.count("--mount") == 1
    assert argv[argv.index("--mount") + 1] == f"type=bind,source={snapshot['directory']},target=/workspace,readonly"
    assert argv[-3:] == [IMAGE, "python", "main.py"]
    assert str(service.root) not in " ".join(argv)
    assert not (Path(snapshot["directory"]) / "output.txt").exists()


@pytest.mark.parametrize("image", ["python:latest", "python", "-v@sha256:" + "a" * 64, IMAGE + " --privileged", None])
def test_plan_requires_pinned_digest_and_rejects_image_argument_injection(prepared, image):
    service, snapshot = prepared
    with pytest.raises(WorkerError):
        plan_docker_invocation(service, snapshot["id"], snapshot["snapshot_hash"], image, ["python", "main.py"])


@pytest.mark.parametrize("command", [[], "python main.py", ["python", None], ["python", "x\x00y"], ["x"] * 33])
def test_plan_requires_bounded_argument_vector(prepared, command):
    service, snapshot = prepared
    with pytest.raises(WorkerError):
        plan_docker_invocation(service, snapshot["id"], snapshot["snapshot_hash"], IMAGE, command)


def test_modified_snapshot_cannot_be_planned(prepared):
    service, snapshot = prepared
    (Path(snapshot["directory"]) / "main.py").write_bytes(b"changed after preparation")
    with pytest.raises(WorkerError):
        plan_docker_invocation(service, snapshot["id"], snapshot["snapshot_hash"], IMAGE, ["python", "main.py"])


def test_unselected_file_cannot_be_smuggled_into_snapshot_mount(prepared):
    service, snapshot = prepared
    (Path(snapshot["directory"]) / ".env").write_bytes(b"synthetic private data")
    with pytest.raises(WorkerError):
        plan_docker_invocation(service, snapshot["id"], snapshot["snapshot_hash"], IMAGE, ["python", "main.py"])


def test_forged_snapshot_hash_and_traversal_cannot_be_planned(prepared):
    service, snapshot = prepared
    for identifier, digest in [(snapshot["id"], "0" * 64), ("../../outside", snapshot["snapshot_hash"])]:
        with pytest.raises(WorkerError):
            plan_docker_invocation(service, identifier, digest, IMAGE, ["python", "main.py"])


def test_cli_status_is_explicit_and_does_not_offer_host_execution(monkeypatch, capsys):
    from tools.code_worker import main
    monkeypatch.setattr("shutil.which", lambda _: None)
    assert main(["--status"]) == 0
    output = capsys.readouterr().out
    assert '"available": false' in output and '"host_fallback": false' in output
