"""Explicit, read-only WhatsApp business sender verification; never account creation.

The trusted constructor binds the local principal to one configured WABA and phone.
Provider linkage is verified through /{WABA-ID}/phone_numbers, not a model assertion.
This proves accessible account metadata, not human identity, recipient permission,
messaging-token scope, account quality, billing readiness or successful delivery.
Use a single messaging-account token in this initial v26.0 scope.

https://www.postman.com/meta/whatsapp-business-platform/request/e9ady51/get-phone-numbers
https://developers.facebook.com/documentation/business-messaging/whatsapp/permissions/
"""
import hashlib
import hmac
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import urlencode
from urllib.request import Request

from connectors.whatsapp import GRAPH_API_VERSION, MAX_RESPONSE_BYTES, WhatsAppResponse, _http_transport, _unique_object
from connectors.workspace import _entry, _read_file, _verify_permissions
from tools.hub_admin import _reject_linked_path


_ID = re.compile(r"[1-9][0-9]{0,31}\Z")
_LOCAL_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_E164 = re.compile(r"\+[1-9][0-9]{1,14}\Z")
_NAME = re.compile(r"[a-z0-9_]{1,512}\Z")
_CURSOR = re.compile(r"[A-Za-z0-9_+/=-]{1,1024}\Z")
_ACCOUNT_FIELDS = {"connection_id", "principal_id", "waba_id", "phone_number_id", "display_phone_number", "verified_name", "version", "verified_at"}


class BusinessAccountError(RuntimeError):
    """Safe account failure; provider bodies, credentials and underlying errors omitted."""


def _secret(value):
    return isinstance(value, str) and 1 <= len(value) <= 8192 and all(33 <= ord(c) <= 126 for c in value)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _contains_secret(value, token):
    if isinstance(value, str):
        return token in value
    if isinstance(value, dict):
        return any(_contains_secret(k, token) or _contains_secret(v, token) for k, v in value.items())
    return isinstance(value, list) and any(_contains_secret(v, token) for v in value)


class ProtectedTokenFile:
    """Read an explicitly selected private JSON file containing only access_token.

    Does not create or repair permissions, search for credentials, or write tokens.
    ACLs and non-linked bounded file identity are checked on every read. Credentials
    are plaintext protected by OS permissions, not encryption against the same user.
    """
    def __init__(self, path):
        self._path = Path(path)

    def __call__(self):
        try:
            path = self._path
            if not path.is_absolute() or any(
                part.casefold() == name or part.casefold().startswith(name + " - ")
                for part in path.parts for name in ("onedrive", "dropbox", "google drive", "googledrive", "icloud", "iclouddrive")
            ):
                raise ValueError("unsafe path")
            _reject_linked_path(path)
            _verify_permissions([path.parent, path])
            raw, read_stat = _read_file(path, 16384)
            value = json.loads(raw, object_pairs_hook=_unique_object)
            _reject_linked_path(path)
            _verify_permissions([path.parent, path])
            now = _entry(path)
            if ((now.st_dev, now.st_ino, now.st_size, now.st_mtime_ns, now.st_nlink)
                    != (read_stat.st_dev, read_stat.st_ino, read_stat.st_size, read_stat.st_mtime_ns, 1)):
                raise ValueError("credential file changed")
            if not isinstance(value, dict) or set(value) != {"access_token"} or not _secret(value["access_token"]):
                raise ValueError("invalid credential")
            return value["access_token"]
        except Exception:
            raise BusinessAccountError("Select an existing bounded private token JSON file outside synced folders; its ACLs and exact access_token schema must be valid.") from None


class BusinessAccount:
    def __init__(self, principal_id, waba_id, phone_number_id, credential_provider, *, connection_id,
                 transport=None, timeout=5, clock=time.time):
        if (not isinstance(principal_id, str) or not _LOCAL_ID.fullmatch(principal_id)
                or not isinstance(connection_id, str) or not _LOCAL_ID.fullmatch(connection_id)
                or any(not isinstance(v, str) or not _ID.fullmatch(v) for v in (waba_id, phone_number_id))
                or not callable(credential_provider) or (transport is not None and not callable(transport))
                or not callable(clock) or type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 60):
            raise BusinessAccountError("Configure a trusted principal, connection, numeric account/phone IDs and bounded account reader.")
        self._principal_id, self._waba_id, self._phone_id = principal_id, waba_id, phone_number_id
        self._connection_id, self._credentials = connection_id, credential_provider
        self._transport = _http_transport if transport is None else transport
        self._timeout, self._clock = timeout, clock

    def _token(self):
        try:
            value = self._credentials()
            if not _secret(value):
                raise ValueError("invalid credential")
            return value
        except Exception:
            raise BusinessAccountError("A protected business credential is unavailable; explicit configuration is required.") from None

    def _edge(self, edge, fields, token, **parameters):
        # All callers choose edge/fields here in code. Pagination never follows provider URLs.
        if edge not in ("phone_numbers", "message_templates"):
            raise BusinessAccountError("Unsupported account read.")
        rows, cursors = [], set()
        query = {"fields": fields, "limit": 100, **parameters}
        try:
            for _ in range(5):
                url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{self._waba_id}/{edge}?" + urlencode(query)
                request = Request(url, method="GET", headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
                response = self._transport(request, timeout=self._timeout, max_response_bytes=MAX_RESPONSE_BYTES)
                if (not isinstance(response, WhatsAppResponse) or type(response.status_code) is not int or response.status_code != 200
                        or not isinstance(response.body, bytes) or len(response.body) > MAX_RESPONSE_BYTES):
                    raise ValueError("read failed")
                value = json.loads(response.body, object_pairs_hook=_unique_object)
                if (not isinstance(value, dict) or "error" in value or not isinstance(value.get("data"), list)
                        or len(value["data"]) > 100 or any(not isinstance(row, dict) for row in value["data"])):
                    raise ValueError("invalid page")
                # Graph may embed the bearer in paging.next: discard that URL, never follow/log it.
                # Returned account/template fields must not reflect it into safe snapshots.
                if _contains_secret(value["data"], token):
                    raise ValueError("credential reflection")
                rows.extend(value["data"])
                paging = value.get("paging", {})
                if not isinstance(paging, dict):
                    raise ValueError("invalid paging")
                if not paging.get("next"):
                    return rows
                cursor = paging.get("cursors", {}).get("after")
                if not isinstance(cursor, str) or not _CURSOR.fullmatch(cursor) or cursor in cursors or token in cursor:
                    raise ValueError("invalid cursor")
                cursors.add(cursor)
                query["after"] = cursor
            raise ValueError("page limit")
        except Exception:
            raise BusinessAccountError("Business metadata could not be verified within bounded trusted reads. No send was attempted.") from None

    def _version(self, snapshot, token):
        body = {k: v for k, v in snapshot.items() if k not in ("version", "verified_at")}
        return _digest({"account": body, "credential_revision": hashlib.sha256(token.encode()).hexdigest()})

    def _snapshot(self, token):
        rows = self._edge("phone_numbers", "id,display_phone_number,verified_name", token)
        matches = [row for row in rows if row.get("id") == self._phone_id]
        if len(matches) != 1:
            raise BusinessAccountError("The configured phone was not uniquely verified under the configured business account.")
        row = matches[0]
        display, name = row.get("display_phone_number"), row.get("verified_name")
        if (not isinstance(display, str) or len(display) > 64 or not re.fullmatch(r"\+?[0-9 ()-]+", display)
                or not isinstance(name, str) or not name.strip() or len(name) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in name)):
            raise BusinessAccountError("The provider returned invalid business display details.")
        display = "+" + re.sub(r"[^0-9]", "", display)
        if not _E164.fullmatch(display):
            raise BusinessAccountError("The provider did not return a usable business phone number.")
        stamp = self._clock()
        if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp < 0:
            raise BusinessAccountError("The account verification clock is invalid.")
        result = {"connection_id": self._connection_id, "principal_id": self._principal_id,
                  "waba_id": self._waba_id, "phone_number_id": self._phone_id,
                  "display_phone_number": display, "verified_name": name, "verified_at": int(stamp)}
        result["version"] = self._version(result, token)
        return result

    def snapshot(self):
        """Perform an actual bounded provider read; construction does no network IO."""
        token = self._token()
        result = self._snapshot(token)
        self.authorized_token(result)  # Configuration must remain stable during the read.
        return result

    def template_rows(self, name):
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise BusinessAccountError("Use an exact supported template name.")
        token = self._token()
        snapshot = self._snapshot(token)
        rows = self._edge("message_templates", "id,name,language,status,category,parameter_format,components", token, name=name)
        self.authorized_token(snapshot)
        return snapshot, rows

    def authorized_token(self, snapshot):
        """Worker-private credential access, only for an unchanged verified binding.

        This does not itself authorize sending or prove current recipient consent.
        """
        token = self._token()
        if (not isinstance(snapshot, dict) or set(snapshot) != _ACCOUNT_FIELDS
                or snapshot.get("connection_id") != self._connection_id or snapshot.get("principal_id") != self._principal_id
                or snapshot.get("waba_id") != self._waba_id or snapshot.get("phone_number_id") != self._phone_id
                or not isinstance(snapshot.get("version"), str)
                or not hmac.compare_digest(snapshot["version"], self._version(snapshot, token))):
            raise BusinessAccountError("The verified account or credential binding changed; verify and review it again.")
        return token
