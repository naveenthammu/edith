import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from brain.hub import HubClient, BusinessWorkerClient


@pytest.fixture
def endpoint():
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def handle_request(self):
            raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            calls.append((self.command, self.path, self.headers.get('Authorization'), json.loads(raw) if raw else None))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"synthetic":true}')
        do_GET = do_POST = handle_request
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}', calls
    server.shutdown()
    server.server_close()
    thread.join()


def test_paired_client_drafts_only_exact_registered_bindings_and_versions(endpoint):
    url, calls = endpoint
    client = HubClient(url, 'p' * 40)
    client.business_bindings()
    client.business_binding('binding-1')
    client.business_permissions()
    client.propose_business_message('binding-1', 'b' * 64, 'contact-1', 'cv', 'pv', ['Ravi'], 'request-1')
    client.business_messages()
    client.business_message('message-1')
    client.decide_business_message('message-1', 'approve', 'a' * 64)
    assert [(c[0], c[1]) for c in calls] == [
        ('GET', '/v1/whatsapp-bindings'), ('GET', '/v1/whatsapp-bindings/binding-1'),
        ('GET', '/v1/whatsapp-permissions'), ('POST', '/v1/business-messages'),
        ('GET', '/v1/business-messages'), ('GET', '/v1/business-messages/message-1'),
        ('POST', '/v1/business-messages/message-1/decide')]
    assert calls[3][3] == {'binding_id': 'binding-1', 'binding_version': 'b' * 64, 'contact_id': 'contact-1',
        'contact_version': 'cv', 'permission_version': 'pv', 'parameters': ['Ravi'], 'idempotency_key': 'request-1'}
    assert calls[-1][3] == {'decision': 'approve', 'argument_hash': 'a' * 64}
    assert all(c[2] == 'Bearer ' + 'p' * 40 for c in calls)
    assert not hasattr(client, 'claim_business_message')


def test_worker_client_uses_only_separate_routes_and_credential(endpoint):
    url, calls = endpoint
    client = BusinessWorkerClient(url, 'w' * 40)
    client.capabilities()
    client.bindings()
    client.binding('binding-1')
    client.register_binding({'snapshot': 'account'}, {'snapshot': 'template'})
    client.permissions()
    client.set_permission({'status': 'revoked', 'expected_version': 'v1'})
    client.messages()
    client.message('message-1')
    client.claim_message('message-1', 'a' * 64)
    client.authorize_message('message-1', 'a' * 64)
    client.complete_message('message-1', 'unknown', {'provider_message_id': None})
    assert all(c[1].startswith('/v1/whatsapp-worker/') and c[2] == 'Bearer ' + 'w' * 40 for c in calls)
    assert [c[1].removeprefix('/v1/whatsapp-worker/') for c in calls] == [
        'capabilities', 'bindings', 'bindings/binding-1', 'bindings', 'permissions', 'permissions',
        'messages', 'messages/message-1', 'messages/message-1/claim', 'messages/message-1/authorize', 'messages/message-1/result']
    assert calls[3][3] == {'account': {'snapshot': 'account'}, 'template': {'snapshot': 'template'}}
    assert calls[8][3] == calls[9][3] == {'argument_hash': 'a' * 64}
    assert calls[-1][3] == {'status': 'unknown', 'evidence': {'provider_message_id': None}}
    assert not hasattr(client, 'decide_message') and not hasattr(client, 'search')


def test_both_clients_encode_path_identifiers_without_endpoint_injection(endpoint):
    url, calls = endpoint
    HubClient(url, 'p' * 40).business_message('one/two?key=x')
    BusinessWorkerClient(url, 'w' * 40).authorize_message('one/two?key=x', 'a' * 64)
    assert calls[0][1] == '/v1/business-messages/one%2Ftwo%3Fkey%3Dx'
    assert calls[1][1] == '/v1/whatsapp-worker/messages/one%2Ftwo%3Fkey%3Dx/authorize'
