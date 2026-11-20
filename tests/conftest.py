import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import os
import socket
import subprocess
import time

import pytest
from brain.hub import HubClient, HubError


@pytest.fixture
def running_hub(tmp_path):
    executable = os.getenv("EDITH_TEST_HUB_EXE")
    if not executable:
        pytest.skip("Set EDITH_TEST_HUB_EXE to the built Rust binary for cross-language verification")
    assert Path(executable).is_file()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    tokens = {"owner": "owner-" + "x" * 40, "other": "other-" + "y" * 40, "child": "child-" + "z" * 40}
    configuration = tmp_path / "hub.json"
    configuration.write_text(json.dumps({"database_path": str(tmp_path / "knowledge.sqlite3"),
        "bind": f"127.0.0.1:{port}", "home_entities": ["light.study"],
        "commerce_merchants": [{"principal_id": "owner", "id": "test-shop", "display_name": "Synthetic shop",
            "shop_domain": "synthetic.myshopify.com", "checkout_hosts": ["synthetic.myshopify.com"]}],
        "principals": [{"id": name, "token": token, "role": "child" if name == "child" else "adult"}
                       for name, token in tokens.items()]}))
    environment = {**os.environ, "EDITH_HUB_CONFIG": str(configuration)}
    processes = []

    def start():
        process = subprocess.Popen([executable], env=environment, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        processes.append(process)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail("Test hub failed to start: " + process.stderr.read().decode(errors="replace"))
            try:
                HubClient(url, tokens["owner"]).capabilities()
                return process
            except HubError:
                time.sleep(0.05)
        pytest.fail("Test hub did not become ready")

    def stop(process):
        process.terminate()
        process.wait(timeout=10)
        process.stderr.close()

    try:
        process = start()
        clients = {name: HubClient(url, token) for name, token in tokens.items()}
        yield clients, lambda: (stop(processes[-1]), start())
    finally:
        for process in processes:
            if process.poll() is None:
                stop(process)
