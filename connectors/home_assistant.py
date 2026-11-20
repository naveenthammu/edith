"""Home Assistant power control behind EDITH's household action authority.

The caller must authorize the member, exact entity and requested action before invoking
this adapter. Its installation allowlist is an additional boundary, not member identity.
An observed HA state is not proof that a particular request caused a physical change.

API contract: https://contributors.home-assistant.io/core/internals/rest-api/
"""
import json
import math
import re
import threading
from urllib.parse import urlsplit

import requests


MAX_RESPONSE_BYTES = 512_000
MAX_ALLOWED_ENTITIES = 200
ENTITY = re.compile(r"(?:light|switch|fan)\.[a-z0-9_]{1,120}\Z")


class HomeAssistantError(RuntimeError):
    """A connection failed without exposing its credentials or response body."""

    def __init__(self, detail: str, code: str = "unavailable"):
        super().__init__(detail)
        self.code = code


class HomeAssistant:
    def __init__(self, base_url: str, token: str, allowed_entities: set[str], timeout: float = 5):
        if not isinstance(base_url, str) or any(c.isspace() or ord(c) < 32 for c in base_url) or "\\" in base_url:
            raise ValueError("Home Assistant needs a fixed HTTP or HTTPS endpoint.")
        try:
            endpoint = urlsplit(base_url)
            port = endpoint.port
        except ValueError:
            raise ValueError("Home Assistant endpoint is invalid.") from None
        if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname
                or endpoint.username is not None or endpoint.password is not None
                or endpoint.query or endpoint.fragment or (port is not None and not 1 <= port <= 65535)):
            raise ValueError("Home Assistant endpoint must not contain credentials, a query or a fragment.")
        if (not isinstance(token, str) or not token or len(token) > 8192
                or any(ord(c) < 33 or ord(c) > 126 for c in token)):
            raise ValueError("Home Assistant access token is missing or invalid.")
        if (not isinstance(allowed_entities, (set, frozenset)) or len(allowed_entities) > MAX_ALLOWED_ENTITIES
                or any(not isinstance(entity, str) or not ENTITY.fullmatch(entity) for entity in allowed_entities)):
            raise ValueError("Allow only exact light, switch or fan entity IDs in this connection.")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError("Home Assistant timeout must be greater than zero and at most 60 seconds.")
        self._base_url = base_url.rstrip("/")
        self._allowed = frozenset(allowed_entities)
        self._timeout = timeout
        self._lock = threading.RLock()
        self._session = requests.Session()
        self._session.trust_env = False  # no proxy, netrc or environment CA override
        self._session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
        self._session.mount("http://", requests.adapters.HTTPAdapter(max_retries=0))
        self._session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))

    def close(self) -> None:
        """Release the connection pool when the configured adapter is retired."""
        with self._lock:
            self._session.close()

    def _entity(self, entity_id: str) -> str:
        if not isinstance(entity_id, str) or not ENTITY.fullmatch(entity_id):
            raise ValueError("A valid light, switch or fan entity ID is required.")
        if entity_id not in self._allowed:
            raise PermissionError("This entity is not permitted by the configured Home Assistant connection.")
        return entity_id.split(".", 1)[0]

    def _request(self, method: str, path: str, payload: dict | None = None):
        # Paths come only from internal constants and strictly validated entity IDs.
        with self._lock:
            try:
                with self._session.request(method, self._base_url + path, json=payload,
                                           timeout=self._timeout, allow_redirects=False, stream=True) as response:
                    if 300 <= response.status_code < 400:
                        raise HomeAssistantError("Home Assistant redirects are refused.", "redirect_refused")
                    if response.status_code in {401, 403}:
                        raise HomeAssistantError("Home Assistant authentication or permission was refused.", "authentication_failed")
                    if response.status_code == 404:
                        raise HomeAssistantError("The configured Home Assistant resource was not found.", "not_found")
                    if response.status_code != 200:
                        raise HomeAssistantError("Home Assistant did not accept this request.", "http_error")
                    declared = response.headers.get("Content-Length")
                    if declared is not None and (not declared.isdigit() or int(declared) > MAX_RESPONSE_BYTES):
                        raise HomeAssistantError("Home Assistant response exceeded the permitted size.", "invalid_response")
                    body = bytearray()
                    for chunk in response.iter_content(chunk_size=8192):
                        if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise HomeAssistantError("Home Assistant response exceeded the permitted size.", "invalid_response")
                        body.extend(chunk)
                    try:
                        return json.loads(body)
                    except (ValueError, UnicodeError):
                        raise HomeAssistantError("Home Assistant returned invalid JSON.", "invalid_response") from None
            except requests.Timeout:
                raise HomeAssistantError("Home Assistant request timed out; its outcome may be unknown.", "timeout") from None
            except requests.RequestException:
                raise HomeAssistantError("Home Assistant connection failed; its outcome may be unknown.", "connection_failed") from None

    @staticmethod
    def _observation(entity_id: str, data) -> dict:
        if (not isinstance(data, dict) or data.get("entity_id") != entity_id
                or not isinstance(data.get("state"), str) or data["state"] not in {"on", "off"}):
            return {"entity_id": entity_id, "observed_state": None, "status": "unknown",
                    "detail": "Home Assistant did not return an available on/off state for this exact entity."}
        return {"entity_id": entity_id, "observed_state": data["state"], "status": "observed",
                "detail": "Current Home Assistant state observed; this is not an independent physical-device measurement."}

    def list_devices(self) -> list[dict]:
        """Read only the configured entities from HA's returned states; omit unrelated attributes.

        A transport/schema error raises HomeAssistantError rather than pretending no devices exist.
        Missing configured entities remain visible as unknown observations.
        """
        if not self._allowed:
            return []
        data = self._request("GET", "/api/states")
        if not isinstance(data, list):
            raise HomeAssistantError("Home Assistant returned an invalid state list.", "invalid_response")
        selected = {}
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("entity_id"), str) and item["entity_id"] in self._allowed:
                entity = item["entity_id"]
                if entity in selected:
                    raise HomeAssistantError("Home Assistant returned duplicate entity states.", "invalid_response")
                selected[entity] = item
        return [self._observation(entity, selected.get(entity)) for entity in sorted(self._allowed)]

    def state(self, entity_id: str) -> dict:
        """Observe an allowed entity, with unknown explicitly distinguished from off."""
        self._entity(entity_id)
        try:
            return self._observation(entity_id, self._request("GET", f"/api/states/{entity_id}"))
        except HomeAssistantError as error:
            return {"entity_id": entity_id, "observed_state": None, "status": "unknown", "detail": str(error)}

    def set_power(self, entity_id: str, on: bool) -> dict:
        """Submit one power command, then read back state. Never retry an uncertain POST.

        'verified' requires an accepted service call AND matching state afterward. Even if a
        read after timeout matches, status remains unknown: observation is not a transaction receipt.
        The action journal must retain that distinction and decide any later reconciliation.
        """
        domain = self._entity(entity_id)
        if type(on) is not bool:
            raise ValueError("Power must be a true or false boolean.")
        requested = "on" if on else "off"
        command_status = "accepted"
        detail = "The service request was accepted and its requested Home Assistant state was observed."
        with self._lock:
            try:
                result = self._request("POST", f"/api/services/{domain}/turn_{requested}", {"entity_id": entity_id})
                if not isinstance(result, list):
                    raise HomeAssistantError("Home Assistant returned an invalid service response.", "invalid_response")
            except HomeAssistantError as error:
                command_status = "rejected" if error.code in {"authentication_failed", "not_found"} else "unknown"
                detail = str(error) + " The command was not retried; any readback describes current state, not proof of this command's effect."
            observed = self.state(entity_id)
        verified = command_status == "accepted" and observed["observed_state"] == requested
        if command_status == "accepted" and not verified:
            detail = "The service request was accepted, but its requested state was not verified. The command was not retried."
        return {"entity_id": entity_id, "requested_state": requested,
                "observed_state": observed["observed_state"], "status": "verified" if verified else "unknown",
                "command_status": command_status, "detail": detail}
