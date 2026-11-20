"""Authenticated, bounded transport to the local Rust authority. No model calls."""
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class HubError(RuntimeError):
    """Public error messages never contain credentials, response bodies or documents."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HubClient:
    MAX_RESPONSE = 1_048_576

    def __init__(self, url: str, token: str, timeout: float = 10):
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
                     and parsed.port is not None and 0 < parsed.port < 65536
                     and not parsed.username and not parsed.password
                     and parsed.path in ("", "/") and not parsed.query and not parsed.fragment)
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise HubError("Hub URL must be an explicit http://127.0.0.1:port address.")
        if not isinstance(token, str) or not 32 <= len(token) <= 256 or not all(33 <= ord(c) <= 126 for c in token):
            raise HubError("Hub credential is missing or invalid. Re-pair this client.")
        self.url = url.rstrip("/")
        self._token = token
        self.timeout = timeout
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    @classmethod
    def from_env(cls):
        config_path = os.getenv("EDITH_HUB_CLIENT_CONFIG")
        if not config_path:
            return None
        try:
            with Path(config_path).open("rb") as source:
                raw = source.read(16_385)
            if len(raw) > 16_384:
                raise ValueError("size")
            value = json.loads(raw)
            return cls(value["url"], value["token"])
        except (OSError, ValueError, TypeError, KeyError):
            raise HubError("Hub client configuration is invalid or unreadable. Re-pair this client.") from None

    def _request(self, method, path, payload=None, *, max_request=2_100_000, max_response=None):
        response_limit = self.MAX_RESPONSE if max_response is None else max_response
        try:
            data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode() if payload is not None else None
        except (TypeError, ValueError):
            raise HubError("Hub request is not valid JSON.") from None
        if data is not None and len(data) > max_request:
            raise HubError("Document/request exceeds the local hub size limit.")
        request = Request(self.url + path, data=data, method=method, headers={
            "Authorization": "Bearer " + self._token,
            "Content-Type": "application/json", "Accept": "application/json",
        })
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(response_limit + 1)
        except HTTPError as error:
            status = error.code
            error.close()
            messages = {401: "Hub authentication failed. Re-pair this client.",
                        403: "This household identity does not have permission for that operation.",
                        404: "The requested record or hub endpoint was not found.",
                        400: "The hub rejected the request. Check the supplied fields.",
                        409: "The record or action changed. Refresh its current state before trying again.",
                        413: "The hub rejected the request size."}
            raise HubError(messages.get(status, "The hub could not complete this operation.")) from None
        except (URLError, OSError, TimeoutError):
            raise HubError("The local EDITH hub is unavailable. Start it and retry; nothing was confirmed.") from None
        if len(raw) > response_limit:
            raise HubError("Hub response exceeded the size limit.")
        try:
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError("object required")
            return result
        except (ValueError, UnicodeError):
            raise HubError("The hub returned an invalid response.") from None

    def capabilities(self):
        return self._request("GET", "/v1/capabilities")

    def documents(self):
        return self._request("GET", "/v1/documents")

    def add_document(self, title, content, visibility="private", child_allowed=False):
        return self._request("POST", "/v1/documents", {"title": title, "content": content,
                             "visibility": visibility, "child_allowed": child_allowed})

    def search(self, query, limit=5, *, query_vector=None):
        payload = {"query": query, "limit": limit}
        if query_vector is not None:
            payload['query_vector'] = query_vector
        return self._request("POST", "/v1/search", payload)

    def document_chunks(self, document_id):
        return self._request('GET', '/v1/documents/' + quote(document_id, safe='') + '/chunks',
                             max_response=16_000_000)

    def store_embeddings(self, document_id, source_version, model_id, embeddings):
        return self._request('POST', '/v1/documents/' + quote(document_id, safe='') + '/embeddings',
                             {'source_version': source_version, 'model_id': model_id, 'embeddings': embeddings},
                             max_request=32_000_000)

    def update_document(self, document_id, expected_source_version, title, content,
                        visibility='private', child_allowed=False):
        return self._request('PUT', '/v1/documents/' + quote(document_id, safe=''),
                             {'expected_source_version': expected_source_version, 'title': title,
                              'content': content, 'visibility': visibility, 'child_allowed': child_allowed})

    def validate_sources(self, sources):
        return self._request('POST', '/v1/sources/validate', {'sources': sources})

    def delete_document(self, document_id):
        return self._request("DELETE", "/v1/documents/" + quote(document_id, safe=""))

    def actions(self):
        return self._request("GET", "/v1/actions")

    def propose_action(self, kind, payload, idempotency_key):
        return self._request("POST", "/v1/actions", {"kind": kind, "payload": payload,
                             "idempotency_key": idempotency_key})

    def decide_action(self, action_id, decision, argument_hash):
        return self._request("POST", "/v1/actions/" + quote(action_id, safe="") + "/decide",
                             {"decision": decision, "argument_hash": argument_hash})

    def claim_action(self, action_id):
        return self._request("POST", "/v1/actions/" + quote(action_id, safe="") + "/claim", {})

    def complete_action(self, action_id, status, evidence):
        return self._request("POST", "/v1/actions/" + quote(action_id, safe="") + "/result",
                             {"status": status, "evidence": evidence})

    def reminders(self):
        return self._request('GET', '/v1/reminders', max_response=3_000_000)

    def create_reminder(self, text, due_at, idempotency_key):
        return self._request('POST', '/v1/reminders', {'text': text, 'due_at': due_at,
                                                     'idempotency_key': idempotency_key})

    def cancel_reminder(self, reminder_id):
        return self._request('POST', '/v1/reminders/' + quote(reminder_id, safe='') + '/cancel', {})

    def acknowledge_reminder(self, reminder_id):
        return self._request('POST', '/v1/reminders/' + quote(reminder_id, safe='') + '/acknowledge', {})

    def claim_due_reminder(self):
        return self._request('POST', '/v1/reminders/claim-due', {})

    def complete_reminder_delivery(self, reminder_id, delivery_id, status):
        return self._request('POST', '/v1/reminders/' + quote(reminder_id, safe='') + '/delivery',
                             {'delivery_id': delivery_id, 'status': status})

    def contacts(self):
        return self._request('GET', '/v1/contacts', max_response=2_000_000)

    def add_contact(self, display_name, email=None, *, phone_e164=None):
        return self._request('POST', '/v1/contacts', {'display_name': display_name, 'email': email,
                                                    'phone_e164': phone_e164})

    def update_contact(self, contact_id, expected_version, display_name, email=None, *, phone_e164=None):
        return self._request('PUT', '/v1/contacts/' + quote(contact_id, safe=''),
                             {'expected_version': expected_version, 'display_name': display_name, 'email': email,
                              'phone_e164': phone_e164})

    def delete_contact(self, contact_id):
        return self._request('DELETE', '/v1/contacts/' + quote(contact_id, safe=''))

    def propose_message(self, contact_id, contact_version, subject, body, idempotency_key):
        return self._request('POST', '/v1/messages', {'contact_id': contact_id, 'contact_version': contact_version,
                             'subject': subject, 'body': body, 'idempotency_key': idempotency_key})

    def messages(self):
        return self._request('GET', '/v1/messages', max_response=24_000_000)

    def message(self, message_id):
        return self._request('GET', '/v1/messages/' + quote(message_id, safe=''))

    def decide_message(self, message_id, decision, argument_hash):
        return self._request('POST', '/v1/messages/' + quote(message_id, safe='') + '/decide',
                             {'decision': decision, 'argument_hash': argument_hash})

    def claim_message(self, message_id):
        return self._request('POST', '/v1/messages/' + quote(message_id, safe='') + '/claim', {})

    def complete_message(self, message_id, status, evidence):
        return self._request('POST', '/v1/messages/' + quote(message_id, safe='') + '/result',
                             {'status': status, 'evidence': evidence})

    def merchants(self):
        return self._request('GET', '/v1/merchants')

    def propose_commerce(self, merchant_id, merchant_version, lines, currency, observed_at, idempotency_key):
        return self._request('POST', '/v1/commerce', {'merchant_id': merchant_id,
                             'merchant_version': merchant_version, 'lines': lines, 'currency': currency,
                             'observed_at': observed_at, 'idempotency_key': idempotency_key})

    def commerce(self):
        return self._request('GET', '/v1/commerce', max_response=4_000_000)

    def commerce_action(self, action_id):
        return self._request('GET', '/v1/commerce/' + quote(action_id, safe=''))

    def decide_commerce(self, action_id, decision, argument_hash):
        return self._request('POST', '/v1/commerce/' + quote(action_id, safe='') + '/decide',
                             {'decision': decision, 'argument_hash': argument_hash})

    def claim_commerce(self, action_id):
        return self._request('POST', '/v1/commerce/' + quote(action_id, safe='') + '/claim', {})

    def complete_commerce(self, action_id, status, evidence, checkout=None):
        return self._request('POST', '/v1/commerce/' + quote(action_id, safe='') + '/result',
                             {'status': status, 'evidence': evidence, 'checkout': checkout})

    def commerce_checkout(self, action_id):
        # Trusted review/coordinator only. Cart keys and checkout URLs are not model tools.
        return self._request('GET', '/v1/commerce/' + quote(action_id, safe='') + '/checkout')

    def claim_commerce_checkout(self, action_id, argument_hash):
        return self._request('POST', '/v1/commerce/' + quote(action_id, safe='') + '/checkout/claim',
                             {'argument_hash': argument_hash})

    def complete_commerce_checkout(self, action_id, status, evidence):
        return self._request('POST', '/v1/commerce/' + quote(action_id, safe='') + '/checkout/result',
                             {'status': status, 'evidence': evidence})

    def business_bindings(self):
        return self._request('GET', '/v1/whatsapp-bindings', max_response=4_000_000)

    def business_binding(self, binding_id):
        return self._request('GET', '/v1/whatsapp-bindings/' + quote(binding_id, safe=''))

    def business_permissions(self):
        return self._request('GET', '/v1/whatsapp-permissions', max_response=2_000_000)

    def propose_business_message(self, binding_id, binding_version, contact_id, contact_version,
                                 permission_version, parameters, idempotency_key):
        return self._request('POST', '/v1/business-messages', {'binding_id': binding_id,
            'binding_version': binding_version, 'contact_id': contact_id, 'contact_version': contact_version,
            'permission_version': permission_version, 'parameters': parameters, 'idempotency_key': idempotency_key})

    def business_messages(self):
        return self._request('GET', '/v1/business-messages', max_response=8_000_000)

    def business_message(self, message_id):
        return self._request('GET', '/v1/business-messages/' + quote(message_id, safe=''))

    def decide_business_message(self, message_id, decision, argument_hash):
        return self._request('POST', '/v1/business-messages/' + quote(message_id, safe='') + '/decide',
                             {'decision': decision, 'argument_hash': argument_hash})


class BusinessWorkerClient:
    """Dedicated WhatsApp worker API surface; it cannot approve a paired user's draft.

    Rust independently enforces the separate credential purpose and exact connection.
    This narrow Python surface is an additional guard, not the authorization boundary.
    """
    def __init__(self, url, token, timeout=10):
        self._transport = HubClient(url, token, timeout)
        self.url = self._transport.url

    def _request(self, method, path, payload=None, **limits):
        return self._transport._request(method, '/v1/whatsapp-worker/' + path, payload, **limits)

    def capabilities(self):
        return self._request('GET', 'capabilities')

    def bindings(self):
        return self._request('GET', 'bindings', max_response=4_000_000)

    def binding(self, binding_id):
        return self._request('GET', 'bindings/' + quote(binding_id, safe=''))

    def register_binding(self, account, template):
        return self._request('POST', 'bindings', {'account': account, 'template': template})

    def permissions(self):
        return self._request('GET', 'permissions', max_response=2_000_000)

    def set_permission(self, payload):
        return self._request('POST', 'permissions', payload)

    def messages(self):
        return self._request('GET', 'messages', max_response=8_000_000)

    def message(self, message_id):
        return self._request('GET', 'messages/' + quote(message_id, safe=''))

    def claim_message(self, message_id, argument_hash):
        return self._request('POST', 'messages/' + quote(message_id, safe='') + '/claim',
                             {'argument_hash': argument_hash})

    def authorize_message(self, message_id, argument_hash):
        return self._request('POST', 'messages/' + quote(message_id, safe='') + '/authorize',
                             {'argument_hash': argument_hash})

    def complete_message(self, message_id, status, evidence):
        return self._request('POST', 'messages/' + quote(message_id, safe='') + '/result',
                             {'status': status, 'evidence': evidence})
