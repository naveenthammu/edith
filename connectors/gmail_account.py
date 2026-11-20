"""Explicit per-principal Gmail OAuth setup and protected token refresh.

Only connect_account(), called by the explicit setup CLI, can open an OAuth browser.
Runtime loading/refresh never authenticates interactively and never accesses the legacy
global Google store. Account files contain plaintext credentials protected by OS ACLs;
they must be kept in a dedicated local directory outside backups/sync selected by users.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import threading
import time
from types import SimpleNamespace
import uuid

import requests

from connectors.gmail import Gmail, GmailError, validate_address
from connectors.workspace import WorkspaceError, _entry, _read_file, _verify_permissions
from tools.hub_admin import AdminError, _reject_linked_path, _restrict_directory as _protect_directory


TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
CERTS_ENDPOINT = "https://www.googleapis.com/oauth2/v1/certs"
SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
SCOPES = (SEND_SCOPE, "openid", "email")
TIMEOUT = 5
MAX_JSON = 65_536
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_CLIENT = re.compile(r"[A-Za-z0-9._-]{1,256}\.apps\.googleusercontent\.com\Z")
_TOKEN = re.compile(r"[A-Za-z0-9._~+/-]+=*\Z")
_FILES = {"account.json", "credentials.json", ".refresh.lock"}
_METADATA = {"version", "principal_id", "verified_email", "subject", "client_id", "scopes", "verified_at", "credentials_file"}
_CREDENTIALS = {"type", "client_id", "client_secret", "refresh_token", "token_uri", "token", "expiry", "scopes", "account"}


class GmailAccountError(RuntimeError):
    """Display-safe account error; excludes tokens, auth codes, and underlying exceptions."""


def _identity(value):
    if (not isinstance(value, dict) or not isinstance(value.get("principal_id"), str)
            or not _ID.fullmatch(value["principal_id"]) or value.get("role") not in ("adult", "elder")):
        raise GmailAccountError("Gmail setup requires an authenticated adult or elder principal.")
    return {"principal_id": value["principal_id"], "role": value["role"]}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise GmailAccountError("Account configuration contains duplicate fields.")
        result[key] = value
    return result


def _read_json(path):
    path = Path(path)
    if not path.is_absolute():
        raise GmailAccountError("Use an absolute selected local configuration path.")
    _reject_linked_path(path)
    raw, _ = _read_file(path, MAX_JSON)
    value = json.loads(raw, object_pairs_hook=_unique)
    if not isinstance(value, dict):
        raise GmailAccountError("Configuration must be a bounded JSON object.")
    return value


def _private_directory(path):
    directory = Path(path)
    if not directory.is_absolute():
        raise GmailAccountError("Use a dedicated absolute local account directory.")
    _reject_linked_path(directory)
    directory = directory.resolve()
    for part in directory.parts:
        lowered = part.casefold()
        if any(lowered == name or lowered.startswith(name + " - ") for name in
               ("onedrive", "dropbox", "google drive", "googledrive", "icloud", "iclouddrive")):
            raise GmailAccountError("Keep account credentials outside synchronized directories.")
    return directory


def _store(config_path):
    config = Path(config_path)
    if config.name != "account.json" or not config.is_absolute():
        raise GmailAccountError("Select the account.json file from a dedicated Gmail account store.")
    directory = _private_directory(config.parent)
    if not directory.is_dir():
        raise GmailAccountError("The configured account directory is unavailable.")
    files = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if len(files) >= 3 or entry.name not in _FILES:
                raise GmailAccountError("Account directory contains unexpected files.")
            path = directory / entry.name
            metadata = _entry(path)
            if not path.is_file() or metadata.st_nlink != 1:
                raise GmailAccountError("Account artifacts must be regular private files without links.")
            files.append(path)
    if {path.name for path in files} != _FILES:
        raise GmailAccountError("The Gmail account store is incomplete.")
    _verify_permissions([directory, *files])
    return directory


def _secret(value, *, bearer=False):
    return (isinstance(value, str) and 1 <= len(value) <= 8192 and all(33 <= ord(char) <= 126 for char in value)
            and (not bearer or _TOKEN.fullmatch(value) is not None))


def _scopes(value):
    if isinstance(value, str):
        if len(value) > 8192:
            raise GmailAccountError("Granted scopes are invalid.")
        value = value.split()
    if (not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 64
            or any(not isinstance(scope, str) or not 1 <= len(scope) <= 256 or any(char.isspace() for char in scope) for scope in value)):
        raise GmailAccountError("Granted scopes are invalid.")
    normalized = ["email" if scope == "https://www.googleapis.com/auth/userinfo.email" else scope for scope in value]
    if SEND_SCOPE not in normalized:
        raise GmailAccountError("Gmail send permission was not granted. Reconnect explicitly with the required scope.")
    return sorted(set(normalized))


def _client_config(path):
    value = _read_json(path)
    if set(value) != {"installed"} or not isinstance(value["installed"], dict):
        raise GmailAccountError("Choose a Google Desktop OAuth client JSON, not a web or service-account credential.")
    client = value["installed"]
    if (not isinstance(client.get("client_id"), str) or not _CLIENT.fullmatch(client["client_id"])
            or not _secret(client.get("client_secret")) or client.get("token_uri") != TOKEN_ENDPOINT
            or client.get("auth_uri") not in (AUTH_ENDPOINT, "https://accounts.google.com/o/oauth2/auth")):
        raise GmailAccountError("The Desktop OAuth client must use the official fixed Google endpoints.")
    return {"installed": {"client_id": client["client_id"], "client_secret": client["client_secret"],
                           "auth_uri": AUTH_ENDPOINT, "token_uri": TOKEN_ENDPOINT, "redirect_uris": ["http://127.0.0.1"]}}


def _session():
    session = requests.Session()
    session.trust_env = False
    session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
    return session


def _bounded_request(requester, method, url, *args, **kwargs):
    if (method.upper(), url) not in {("POST", TOKEN_ENDPOINT), ("GET", CERTS_ENDPOINT)}:
        raise GmailAccountError("OAuth transport refused an unexpected endpoint.")
    kwargs.update(timeout=TIMEOUT, allow_redirects=False, stream=True)
    response = requester(method, url, *args, **kwargs)
    try:
        if response.status_code != 200:
            raise GmailAccountError("Google did not accept the account request. Explicit reconnect may be required.")
        declared = response.headers.get("Content-Length")
        if declared is not None and (not declared.isdigit() or int(declared) > MAX_JSON):
            raise GmailAccountError("Google account response exceeded its size limit.")
        data = bytearray()
        for chunk in response.iter_content(chunk_size=8192):
            if len(data) + len(chunk) > MAX_JSON:
                raise GmailAccountError("Google account response exceeded its size limit.")
            data.extend(chunk)
        response._content = bytes(data)
        response._content_consumed = True
        return response
    finally:
        response.close()


def _refresh_tokens(payload, *, timeout=TIMEOUT):
    with _session() as session:
        response = _bounded_request(session.request, "POST", TOKEN_ENDPOINT, data=payload)
        return json.loads(response.content, object_pairs_hook=_unique)


def _make_flow(configuration, scopes):
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_config(configuration, scopes=list(scopes), autogenerate_code_verifier=True)
    flow.oauth2session.trust_env = False
    flow.oauth2session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
    original_request = flow.oauth2session.request
    flow.oauth2session.request = lambda method, url, *args, **kwargs: _bounded_request(original_request, method, url, *args, **kwargs)
    return flow


def _verify_id_token(token, audience):
    from google.oauth2 import id_token
    def certificate_request(url, method="GET", body=None, headers=None, **_kwargs):
        if url != CERTS_ENDPOINT or method != "GET" or body is not None:
            raise GmailAccountError("ID verification refused an unexpected certificate request.")
        with _session() as session:
            response = _bounded_request(session.request, "GET", CERTS_ENDPOINT)
            certificates = json.loads(response.content, object_pairs_hook=_unique)
        # Google v1 uses PEMs; reject JWK-shaped data so the verifier never creates a separate unbounded client.
        if (not isinstance(certificates, dict) or not 1 <= len(certificates) <= 20 or "keys" in certificates
                or any(not isinstance(value, str) or not value.startswith("-----BEGIN CERTIFICATE-----") for value in certificates.values())):
            raise GmailAccountError("Google signing certificates were invalid.")
        return SimpleNamespace(status=200, data=response.content, headers=response.headers)
    return id_token.verify_oauth2_token(token, certificate_request, audience=audience)


def _claims(token, client_id, verifier, now, *, nonce=None, expected=None):
    if not _secret(token):
        raise GmailAccountError("Google did not return a usable identity token.")
    claims = verifier(token, client_id)
    if (not isinstance(claims, dict) or claims.get("aud") != client_id
            or claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com")
            or type(claims.get("exp")) is not int or claims["exp"] <= now
            or type(claims.get("iat")) is not int or claims["iat"] > now + 30
            or claims.get("email_verified") is not True or not _secret(claims.get("sub")) or len(claims["sub"]) > 255
            or (nonce is not None and claims.get("nonce") != nonce)):
        raise GmailAccountError("Google identity verification did not match this account setup.")
    validate_address(claims.get("email"))
    domain = claims["email"].rsplit("@", 1)[1].casefold()
    hosted_domain = claims.get("hd")
    if domain not in {"gmail.com", "googlemail.com"} and (not isinstance(hosted_domain, str) or not hosted_domain.strip()):
        raise GmailAccountError("Use a verified Gmail or Google Workspace mailbox for this send-only connection.")
    if expected is not None and (claims["sub"] != expected["subject"] or claims["email"] != expected["verified_email"]):
        raise GmailAccountError("Refreshed Google identity differs from the configured sender. Explicit reconnect is required.")
    return claims


def _grant(value, now, previous=None):
    if (not isinstance(value, dict) or not _secret(value.get("access_token"), bearer=True)
            or value.get("token_type", "").casefold() != "bearer"
            or type(value.get("expires_in")) is not int or not 0 < value["expires_in"] <= 86_400):
        raise GmailAccountError("Google returned an invalid or expired access grant.")
    refresh = value.get("refresh_token", None if previous is None else previous["refresh_token"])
    if not _secret(refresh):
        raise GmailAccountError("Offline refresh permission was not granted. Reconnect explicitly.")
    scopes = _scopes(value.get("scope", None if previous is None else previous["scopes"]))
    expiry = datetime.fromtimestamp(now + value["expires_in"], timezone.utc).isoformat().replace("+00:00", "Z")
    return {"token": value["access_token"], "refresh_token": refresh, "expiry": expiry, "scopes": scopes}


def _write_new(path, value):
    raw = value if isinstance(value, bytes) else (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
    except BaseException:
        # Exclusive creation above proves ownership even if write/flush/fsync failed.
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _atomic_credentials(directory, credentials):
    temporary = directory / (".credentials-" + uuid.uuid4().hex + ".tmp")
    try:
        _write_new(temporary, credentials)
        os.replace(temporary, directory / "credentials.json")
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _refresh_lock(directory):
    path = directory / ".refresh.lock"
    before = _entry(path)
    with path.open("r+b", buffering=0) as handle:
        opened = os.fstat(handle.fileno())
        if (before.st_ino, before.st_dev) != (opened.st_ino, opened.st_dev):
            raise GmailAccountError("Account lock changed before refresh.")
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise GmailAccountError("Another account refresh is in progress. No send was authorized.") from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def connect_account(hub, oauth_client_config, account_dir, *, flow_factory=None, id_token_verifier=None, clock=None):
    """Explicit interactive setup only. Never call from an audio/model action or import hook."""
    now = time.time if clock is None else clock
    created = []
    directory = None
    owns_directory = False
    try:
        identity = _identity(hub.capabilities())
        directory = _private_directory(account_dir)
        if directory.exists() or not directory.parent.is_dir():
            raise GmailAccountError("Choose a new account directory with an existing parent; existing accounts are never overwritten.")
        configuration = _client_config(oauth_client_config)
        client = configuration["installed"]
        flow = (_make_flow if flow_factory is None else flow_factory)(configuration, SCOPES)
        nonce = secrets.token_urlsafe(32)
        # OAuth libraries may log callback URLs/auth codes at INFO/DEBUG. Setup is an
        # explicit standalone operation, so suppress library logging for this interval.
        previous_logging = logging.root.manager.disable
        try:
            logging.disable(logging.CRITICAL)
            flow.run_local_server(host="127.0.0.1", port=0, open_browser=True, authorization_prompt_message=None,
                                  success_message="Gmail authorization returned. You may close this window.",
                                  timeout_seconds=300, access_type="offline", prompt="consent", nonce=nonce)
        finally:
            logging.disable(previous_logging)
        response = flow.oauth2session.token
        grant = _grant(response, now())
        claims = _claims(response.get("id_token"), client["client_id"], _verify_id_token if id_token_verifier is None else id_token_verifier,
                         now(), nonce=nonce)
        if _identity(hub.capabilities()) != identity:
            raise GmailAccountError("The household identity changed during setup. No account was stored.")
        metadata = {"version": 1, "principal_id": identity["principal_id"], "verified_email": claims["email"],
                    "subject": claims["sub"], "client_id": client["client_id"], "scopes": grant["scopes"],
                    "verified_at": int(now()), "credentials_file": "credentials.json"}
        credentials = {"type": "authorized_user", "client_id": client["client_id"], "client_secret": client["client_secret"],
                       "token_uri": TOKEN_ENDPOINT, "account": claims["email"], **grant}
        _reject_linked_path(directory.parent)
        directory.mkdir(mode=0o700)
        owns_directory = True
        _protect_directory(directory)
        for filename, value in [("credentials.json", credentials), (".refresh.lock", b"0"), ("account.json", metadata)]:
            _write_new(directory / filename, value)
            created.append(directory / filename)
        _store(directory / "account.json")
        return {"status": "configured", "principal_id": identity["principal_id"], "verified_email": claims["email"],
                "account_config": str(directory / "account.json")}
    except Exception:
        # Remove only files successfully created by this attempt; never recurse or alter an existing store.
        for path in reversed(created):
            try:
                path.unlink()
            except OSError:
                pass
        if owns_directory:
            try:
                directory.rmdir()
            except OSError:
                pass
        raise GmailAccountError("Gmail setup was not completed. Check the selected Desktop client, consent, identity and private account directory.") from None


class GmailAccount:
    def __init__(self, config_path, identity, *, refresh_transport=None, id_token_verifier=None, clock=None):
        self._clock = time.time if clock is None else clock
        self._refresh = _refresh_tokens if refresh_transport is None else refresh_transport
        self._verifier = _verify_id_token if id_token_verifier is None else id_token_verifier
        self._lock = threading.Lock()
        try:
            self._identity = _identity(identity)
            self._config = Path(config_path)
            self._directory = _store(self._config)
            self._metadata, _ = self._load()
            self.sender_email = self._metadata["verified_email"]
        except Exception:
            raise GmailAccountError("The Gmail account configuration is invalid, unprotected, or belongs to another principal.") from None

    def _load(self):
        metadata = _read_json(self._directory / "account.json")
        credentials = _read_json(self._directory / "credentials.json")
        if (set(metadata) != _METADATA or type(metadata["version"]) is not int or metadata["version"] != 1
                or metadata["principal_id"] != self._identity["principal_id"] or metadata["credentials_file"] != "credentials.json"
                or not _secret(metadata["subject"]) or len(metadata["subject"]) > 255
                or not isinstance(metadata["client_id"], str) or not _CLIENT.fullmatch(metadata["client_id"])
                or type(metadata["verified_at"]) is not int or metadata["verified_at"] < 0):
            raise GmailAccountError("Account metadata does not match this principal.")
        validate_address(metadata["verified_email"])
        _scopes(metadata["scopes"])
        if (set(credentials) != _CREDENTIALS or credentials["type"] != "authorized_user"
                or credentials["client_id"] != metadata["client_id"] or credentials["account"] != metadata["verified_email"]
                or credentials["token_uri"] != TOKEN_ENDPOINT or not _secret(credentials["client_secret"])
                or not _secret(credentials["refresh_token"]) or not _secret(credentials["token"], bearer=True)):
            raise GmailAccountError("Authorized-user credentials do not match this configured account.")
        _scopes(credentials["scopes"])
        self._expiry(credentials)
        return metadata, credentials

    @staticmethod
    def _expiry(credentials):
        value = credentials["expiry"]
        if not isinstance(value, str) or len(value) > 40 or not value.endswith("Z"):
            raise GmailAccountError("Credential expiry is invalid.")
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()

    def status(self):
        return {"status": "configured", "principal_id": self._identity["principal_id"], "verified_email": self.sender_email,
                "detail": "Account configuration is present; live Gmail access and delivery are not implied."}

    def access_token(self):
        if not self._lock.acquire(blocking=False):
            raise GmailAccountError("Another account refresh is in progress.")
        try:
            _store(self._config)
            with _refresh_lock(self._directory):
                metadata, credentials = self._load()
                if metadata != self._metadata:
                    raise GmailAccountError("The configured account changed; reload the trusted connection.")
                if self._expiry(credentials) > self._clock() + 60:
                    return credentials["token"]
                payload = {"grant_type": "refresh_token", "refresh_token": credentials["refresh_token"],
                           "client_id": credentials["client_id"], "client_secret": credentials["client_secret"]}
                response = self._refresh(payload, timeout=TIMEOUT)
                updated = _grant(response, self._clock(), credentials)
                if "id_token" in response:
                    _claims(response["id_token"], metadata["client_id"], self._verifier, self._clock(), expected=metadata)
                _store(self._config)
                current_metadata, current_credentials = self._load()
                if current_metadata != metadata or current_credentials != credentials:
                    raise GmailAccountError("Account configuration changed while refresh was in progress.")
                rotated = {**credentials, **updated}
                _atomic_credentials(self._directory, rotated)
                _store(self._config)
                saved_metadata, saved_credentials = self._load()
                if saved_metadata != metadata or saved_credentials != rotated:
                    raise GmailAccountError("Rotated credentials could not be confirmed in the private account store.")
                return updated["token"]
        except Exception:
            raise GmailAccountError("Gmail credentials could not be verified or refreshed. No message was sent; explicit reconnect may be required.") from None
        finally:
            self._lock.release()


def gmail_from_env(identity):
    """Optional configured adapter only; never launches OAuth or loads the global Google store."""
    path = os.getenv("EDITH_GMAIL_ACCOUNT_CONFIG")
    if not path:
        return None
    account = GmailAccount(path, identity)
    return Gmail(account.access_token, sender_email=account.sender_email)
