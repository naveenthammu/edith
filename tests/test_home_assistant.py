"""Exercise the real HTTP adapter against an in-process loopback server only."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from connectors.home_assistant import HomeAssistant, HomeAssistantError


@pytest.fixture
def ha_server():
    scenario = SimpleNamespace(
        states={"light.study": "off", "switch.socket": "on", "lock.front_door": "locked"},
        calls=[], post_status=200, post_delay=0, apply_change=True,
        get_status=200, wrong_entity=False, invalid_state=False, redirect=False, raw=None,
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, data):
            payload = scenario.raw if scenario.raw is not None else json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            if status == 302:
                self.send_header("Location", "/credential-leak-target")
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def do_GET(self):
            scenario.calls.append({"method": "GET", "path": self.path})
            if scenario.redirect:
                self.reply(302, {})
            elif scenario.get_status != 200:
                self.reply(scenario.get_status, {"error": "synthetic-secret-token"})
            elif self.path == "/api/states":
                self.reply(200, [{"entity_id": entity, "state": value,
                                  "attributes": {"friendly_name": entity, "private_field": "never return"}}
                                 for entity, value in scenario.states.items()])
            elif self.path.startswith("/api/states/"):
                entity = self.path.removeprefix("/api/states/")
                if entity not in scenario.states:
                    self.reply(404, {})
                else:
                    self.reply(200, {"entity_id": "light.someone_else" if scenario.wrong_entity else entity,
                                     "state": "unavailable" if scenario.invalid_state else scenario.states[entity],
                                     "attributes": {"friendly_name": "Study light", "private_field": "never return"}})
            else:
                self.reply(404, {})

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            scenario.calls.append({"method": "POST", "path": self.path, "json": data,
                                   "authorization": self.headers.get("Authorization")})
            if scenario.apply_change:
                scenario.states[data["entity_id"]] = "on" if self.path.endswith("/turn_on") else "off"
            if scenario.post_delay:
                time.sleep(scenario.post_delay)
            self.reply(scenario.post_status, [] if scenario.post_status == 200 else {"error": "synthetic-secret-token"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    scenario.url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield scenario
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def client(server, **kwargs):
    return HomeAssistant(server.url, "synthetic-secret-token", {"light.study", "switch.socket"}, **kwargs)


def test_set_power_calls_service_and_reads_state_before_verified(ha_server):
    result = client(ha_server).set_power("light.study", True)
    assert result["status"] == "verified"
    assert result["entity_id"] == "light.study"
    assert result["requested_state"] == result["observed_state"] == "on"
    assert result["command_status"] == "accepted"
    assert ha_server.calls == [
        {"method": "POST", "path": "/api/services/light/turn_on", "json": {"entity_id": "light.study"},
         "authorization": "Bearer synthetic-secret-token"},
        {"method": "GET", "path": "/api/states/light.study"},
    ]


def test_turn_off_uses_domain_specific_service(ha_server):
    assert client(ha_server).set_power("switch.socket", False)["observed_state"] == "off"
    assert ha_server.calls[0]["path"] == "/api/services/switch/turn_off"


def test_http_success_without_matching_state_is_unknown(ha_server):
    ha_server.apply_change = False
    result = client(ha_server).set_power("light.study", True)
    assert result["status"] == "unknown" and result["observed_state"] == "off"


def test_post_timeout_never_retries_even_when_readback_matches(ha_server):
    ha_server.post_delay = 0.15
    result = client(ha_server, timeout=0.03).set_power("light.study", True)
    assert result["status"] == "unknown"
    assert result["command_status"] == "unknown"
    assert result["observed_state"] == "on"
    assert len([c for c in ha_server.calls if c["method"] == "POST"]) == 1


@pytest.mark.parametrize("status", [401, 403, 500, 503])
def test_post_errors_do_not_claim_success_or_disclose_token(ha_server, status):
    ha_server.post_status = status
    result = client(ha_server).set_power("light.study", True)
    assert result["status"] == "unknown"
    assert "synthetic-secret-token" not in json.dumps(result)
    assert len([c for c in ha_server.calls if c["method"] == "POST"]) == 1


@pytest.mark.parametrize("entity", ["lock.front_door", "light.other", "light.study/../../lock.front_door", "light.Study", "light.study?x=1"])
def test_denied_or_malformed_entity_never_reaches_network(ha_server, entity):
    adapter = client(ha_server)
    with pytest.raises((ValueError, PermissionError)):
        adapter.set_power(entity, True)
    with pytest.raises((ValueError, PermissionError)):
        adapter.state(entity)
    assert ha_server.calls == []


@pytest.mark.parametrize("value", [1, 0, "true", None, [], {}])
def test_power_requires_actual_boolean_before_any_request(ha_server, value):
    with pytest.raises(ValueError):
        client(ha_server).set_power("light.study", value)
    assert ha_server.calls == []


def test_list_devices_returns_only_allowlisted_states_and_no_private_attributes(ha_server):
    devices = client(ha_server).list_devices()
    assert {d["entity_id"] for d in devices} == {"light.study", "switch.socket"}
    assert "lock.front_door" not in json.dumps(devices)
    assert "private_field" not in json.dumps(devices)


def test_redirect_is_not_followed(ha_server):
    ha_server.redirect = True
    result = client(ha_server).state("light.study")
    assert result["status"] == "unknown"
    assert ha_server.calls == [{"method": "GET", "path": "/api/states/light.study"}]


@pytest.mark.parametrize("wrong, invalid, missing", [(True, False, False), (False, True, False), (False, False, True)])
def test_state_requires_exact_entity_and_available_on_off_value(ha_server, wrong, invalid, missing):
    ha_server.wrong_entity, ha_server.invalid_state = wrong, invalid
    if missing:
        ha_server.states.pop("light.study")
    result = client(ha_server).state("light.study")
    assert result["status"] == "unknown" and result["observed_state"] is None


def test_ambient_proxy_configuration_is_ignored(ha_server, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "")
    assert client(ha_server).state("light.study")["observed_state"] == "off"


@pytest.mark.parametrize("raw", [b"not JSON", b"x" * 600_000])
def test_invalid_or_oversized_responses_are_unknown(ha_server, raw):
    ha_server.raw = raw
    assert client(ha_server).state("light.study")["status"] == "unknown"
    with pytest.raises(HomeAssistantError):
        client(ha_server).list_devices()


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://user:password@localhost:8123", "http://localhost:8123/?token=x", "http://localhost:8123/#x", "http://localhost:bad", "http://localhost\n:8123"])
def test_invalid_fixed_endpoint_configuration_is_rejected_without_echo(url):
    with pytest.raises(ValueError) as error:
        HomeAssistant(url, "synthetic-secret-token", {"light.study"})
    assert "password" not in str(error.value) and "synthetic-secret-token" not in str(error.value)


def test_safety_domains_cannot_be_configured(ha_server):
    with pytest.raises(ValueError):
        HomeAssistant(ha_server.url, "synthetic-secret-token", {"lock.front_door"})
    assert ha_server.calls == []


@pytest.mark.parametrize("value", [[], {}, None, 1, True])
def test_malformed_state_value_is_unknown_instead_of_crashing(ha_server, value):
    ha_server.states["light.study"] = value
    assert client(ha_server).state("light.study")["status"] == "unknown"
    item = next(d for d in client(ha_server).list_devices() if d["entity_id"] == "light.study")
    assert item["observed_state"] is None


def test_post_redirect_is_not_followed_or_misreported_as_verified(ha_server):
    ha_server.post_status = 302
    result = client(ha_server).set_power("light.study", True)
    assert result["status"] == "unknown"
    assert len(ha_server.calls) == 2
    assert all(call["path"] != "/credential-leak-target" for call in ha_server.calls)


def test_missing_configured_device_is_reported_unknown_in_listing(ha_server):
    ha_server.states.pop("switch.socket")
    item = next(d for d in client(ha_server).list_devices() if d["entity_id"] == "switch.socket")
    assert item["status"] == "unknown" and item["observed_state"] is None


def test_empty_allowlist_does_not_fetch_any_household_data(ha_server):
    assert HomeAssistant(ha_server.url, "synthetic-secret-token", set()).list_devices() == []
    assert ha_server.calls == []
