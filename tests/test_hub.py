import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from brain.hub import HubClient, HubError


@pytest.fixture
def endpoint():
    seen = []
    reply = {"status": 200, "body": {"citations": []}}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            seen.append((self.path, self.headers.get("Authorization"), body))
            self.send_response(reply["status"])
            if reply["status"] == 302:
                self.send_header("Location", "/credential-leak")
            self.end_headers()
            payload = reply["body"]
            self.wfile.write(payload if isinstance(payload, bytes) else json.dumps(payload).encode())

        def log_message(self, *args):
            pass

        do_PUT = do_POST

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield f"http://127.0.0.1:{server.server_port}", seen, reply
    server.shutdown()
    server.server_close()
    worker.join()


def test_search_uses_authenticated_loopback_and_ignores_ambient_proxy(endpoint, monkeypatch):
    url, seen, reply = endpoint
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    reply["body"] = {"citations": [{"document_id": "d1", "excerpt": "Water orchid on Monday."}]}
    result = HubClient(url, "x" * 40).search('orchid "schedule"', 3)
    assert result["citations"][0]["excerpt"] == "Water orchid on Monday."
    assert seen == [("/v1/search", "Bearer " + "x" * 40, {"query": 'orchid "schedule"', "limit": 3})]


@pytest.mark.parametrize("url", ["https://example.com", "http://localhost:8788", "http://127.0.0.1.evil:8788", "http://user:pass@127.0.0.1:8788", "http://127.0.0.1:8788/other", "http://0.0.0.0:8788"])
def test_client_refuses_nonliteral_loopback_or_ambiguous_urls(url):
    with pytest.raises(HubError):
        HubClient(url, "x" * 40)


def test_redirect_does_not_forward_credentials(endpoint):
    url, seen, reply = endpoint
    reply.update(status=302, body={})
    with pytest.raises(HubError):
        HubClient(url, "x" * 40).search("orchid")
    assert len(seen) == 1


@pytest.mark.parametrize("body", [b"not-json", b"x" * 1_100_000])
def test_malformed_or_oversized_response_is_rejected(endpoint, body):
    url, _, reply = endpoint
    reply["body"] = body
    with pytest.raises(HubError):
        HubClient(url, "x" * 40).search("orchid")


def test_service_error_does_not_echo_tokens_or_private_data(endpoint):
    url, _, reply = endpoint
    reply.update(status=500, body={"error": "private marker " + "x" * 40})
    with pytest.raises(HubError) as caught:
        HubClient(url, "x" * 40).search("orchid")
    assert "private marker" not in str(caught.value) and "x" * 40 not in str(caught.value)


def test_client_config_missing_is_optional_but_malformed_config_fails_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("EDITH_HUB_CLIENT_CONFIG", raising=False)
    assert HubClient.from_env() is None
    path = tmp_path / "client.json"
    path.write_text('{"url":"http://127.0.0.1:8788","token":"too-short"}')
    monkeypatch.setenv("EDITH_HUB_CLIENT_CONFIG", str(path))
    with pytest.raises(HubError):
        HubClient.from_env()


def test_full_document_content_limit_has_room_for_json_envelope(endpoint):
    url, seen, reply = endpoint
    client = HubClient(url, 'x' * 40)
    client.add_document('Boundary', 'a' * 2_000_000)
    assert len(seen[0][2]['content']) == 2_000_000
    client.update_document('d1', 'v1', 'Boundary', 'b' * 2_000_000)
    assert len(seen[1][2]['content']) == 2_000_000
