"""Reviewable isolated-worker plans, never host execution.

The caller must supply an authorized Workspace plus trusted image/command configuration.
An argv plan is not proof of a working sandbox. Docker daemon mode, image availability,
resource limits, mount translation and runtime isolation have NOT been verified here.
This module deliberately has no launch function and no fallback to host Python or WSL.
"""
import argparse
import json
import re
import shutil

from connectors.workspace import Workspace, WorkspaceError


PINNED_IMAGE = re.compile(r"[a-z0-9][a-z0-9._:/-]{0,254}@sha256:[0-9a-f]{64}\Z")


class WorkerError(RuntimeError):
    """Invalid worker configuration or changed snapshot."""


def execution_status():
    detected = [name for name in ("docker", "wsl", "WindowsSandbox") if shutil.which(name)]
    return {"status": "unavailable", "available": False, "runtime_verified": False,
            "host_fallback": False, "detected_tools": detected,
            "reason": "No verified isolated execution backend is configured. Tool presence alone is insufficient."}


def plan_docker_invocation(workspace, snapshot_id, snapshot_hash, image, command):
    """Build a review artifact only; do not execute this without a separately verified worker."""
    if not isinstance(workspace, Workspace):
        raise WorkerError("Use a trusted, configured Workspace.")
    if not isinstance(image, str) or not PINNED_IMAGE.fullmatch(image):
        raise WorkerError("Configure an approved image pinned by its complete SHA-256 digest.")
    if (not isinstance(command, list) or not 1 <= len(command) <= 32
            or any(not isinstance(arg, str) or not arg or len(arg) > 4096 or "\x00" in arg for arg in command)
            or sum(len(arg) for arg in command) > 8192):
        raise WorkerError("Configure a bounded argument vector, not a shell command string.")
    try:
        snapshot = workspace.verify_worker_snapshot(snapshot_id, snapshot_hash)
    except WorkspaceError:
        raise WorkerError("Snapshot integrity or scope validation failed; no execution was attempted.") from None
    directory = snapshot["directory"]
    if any(char in directory for char in ',\r\n"'):
        raise WorkerError("Snapshot path cannot be represented safely in Docker mount syntax.")
    argv = ["docker", "run", "--rm", "--pull", "never", "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "64",
            "--memory", "512m", "--cpus", "1", "--user", "65534:65534",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", "--workdir", "/workspace",
            "--mount", f"type=bind,source={directory},target=/workspace,readonly", image, *command]
    return {"status": "planned_not_executed", "execution_available": False, "argv": argv,
            "snapshot_id": snapshot_id, "snapshot_hash": snapshot_hash,
            "principal_id": snapshot["principal_id"], "project_id": snapshot["project_id"],
            "reason": "This is a reviewable invocation plan; no sandbox or execution has been verified."}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Report isolated code-worker availability; never execute host code.")
    parser.add_argument("--status", action="store_true", help="Report availability without starting any runtime")
    parser.parse_args(argv)
    print(json.dumps(execution_status(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
