"""Real Rust/ACL/client integration; all Meta, microphone and speech effects are synthetic."""
import json
from pathlib import Path
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from brain.hub import BusinessWorkerClient, HubClient, HubError
from brain.business_review import BusinessMessageReview
from brain.business_tools import business_tools
from brain.trusted_commands import TrustedCommands
from connectors.whatsapp import WhatsAppResponse
from connectors.whatsapp_connection import WhatsAppConnection, configure_connection
from connectors.whatsapp_business_coordinator import BusinessMessageCoordinator
from tools.hub_admin import _restrict_directory


@pytest.fixture
def business_hub(running_hub, tmp_path):
    clients, restart = running_hub
    path = tmp_path / 'hub.json'
    config = json.loads(path.read_text())
    token = 'synthetic-worker-' + 'x' * 40
    other_token = 'other-worker-' + 'y' * 40
    config['whatsapp_workers'] = [
        {'id': 'worker-owner', 'principal_id': 'owner', 'connection_id': 'business', 'waba_id': '123', 'phone_number_id': '456', 'token': token},
        {'id': 'worker-other', 'principal_id': 'other', 'connection_id': 'business', 'waba_id': '789', 'phone_number_id': '987', 'token': other_token},
    ]
    path.write_text(json.dumps(config))
    restart()
    return SimpleNamespace(hub=clients['owner'], clients=clients, restart=restart,
        worker=BusinessWorkerClient(clients['owner'].url, token), token=token,
        other_worker=BusinessWorkerClient(clients['other'].url, other_token))


class Meta:
    def __init__(self):
        self.posts, self.reads, self.hook = [], [], None
        self.lose_receipt = False
    def __call__(self, request, **_):
        parsed = urlsplit(request.full_url)
        assert parsed.scheme == 'https' and parsed.netloc == 'graph.facebook.com'
        assert request.get_header('Authorization') == 'Bearer synthetic-meta-credential-123456789'
        if request.method == 'GET':
            self.reads.append(parsed.path)
            if self.hook: self.hook(parsed.path)
            if parsed.path == '/v26.0/123/phone_numbers':
                result = {'data': [{'id': '456', 'display_phone_number': '+91 98765 43210', 'verified_name': 'Synthetic business'}]}
            elif parsed.path == '/v26.0/123/message_templates':
                result = {'data': [{'id': '345', 'name': 'order_update', 'language': 'en_US', 'status': 'APPROVED',
                    'category': 'UTILITY', 'parameter_format': 'POSITIONAL',
                    'components': [{'type': 'BODY', 'text': 'Hello {{1}}, your order is ready.'}]}]}
            else:
                pytest.fail('Unexpected synthetic Meta endpoint')
        else:
            assert request.method == 'POST' and parsed.path == '/v26.0/456/messages'
            payload = json.loads(request.data)
            self.posts.append(payload)
            assert payload == {'messaging_product': 'whatsapp', 'recipient_type': 'individual', 'to': '+919123456789',
                'type': 'template', 'template': {'name': 'order_update', 'language': {'code': 'en_US'},
                    'components': [{'type': 'body', 'parameters': [{'type': 'text', 'text': 'Ravi'}]}]}}
            if self.lose_receipt:
                raise TimeoutError('synthetic receipt loss after provider attempt')
            result = {'messaging_product': 'whatsapp', 'contacts': [{'input': '+919123456789', 'wa_id': '919123456789'}],
                'messages': [{'id': 'wamid.synthetic-integration', 'message_status': 'accepted'}]}
        return WhatsAppResponse(200, json.dumps(result).encode())


@pytest.fixture
def connected(business_hub, tmp_path, monkeypatch):
    case = business_hub
    case.meta = Meta()
    monkeypatch.setattr('connectors.whatsapp_account._http_transport', case.meta)
    monkeypatch.setattr('connectors.whatsapp_templates._http_transport', case.meta)
    inputs = tmp_path / 'private-inputs'
    inputs.mkdir()
    _restrict_directory(inputs)
    worker_file, token_file = inputs / 'worker.json', inputs / 'meta.json'
    worker_file.write_text(json.dumps({'url': case.hub.url, 'token': case.token}))
    token_file.write_text(json.dumps({'access_token': 'synthetic-meta-credential-123456789'}))
    result = configure_connection(case.hub, worker_file, token_file, tmp_path / 'whatsapp',
        connection_id='business', waba_id='123', phone_number_id='456')
    case.connection = WhatsAppConnection(result['config_path'], case.hub, case.hub.capabilities())
    snapshot = case.connection.catalog.get('345', 'order_update', 'en_US')
    case.binding = case.worker.register_binding(snapshot['account'], snapshot['template'])
    case.contact = case.hub.add_contact('Ravi', phone_e164='+919123456789')
    stamp = int(time.time())
    case.permission_input = {'connection_id': 'business', 'account_version': snapshot['account']['version'],
        'contact_id': case.contact['id'], 'contact_version': case.contact['version'], 'expected_version': None,
        'status': 'granted', 'allowed_categories': ['UTILITY'], 'recorded_at': stamp, 'expires_at': stamp+86400,
        'source': 'explicit_operator_record', 'evidence_reference': 'PRIVATE_SYNTHETIC_PROOF',
        'source_note': 'Synthetic fixture only; no real consent was asserted.'}
    case.permission = case.worker.set_permission(case.permission_input)
    case.tools = business_tools(case.hub, case.connection, case.hub.capabilities(), can_execute=lambda: True)
    case.coordinator = BusinessMessageCoordinator(case.hub, case.connection)
    case.review = BusinessMessageReview(case.hub, case.connection, coordinator=case.coordinator)
    return case


def proposal(case):
    return json.loads(case.tools['draft_business_message'].func(case.contact['id'], case.contact['version'],
        case.binding['id'], case.binding['version'], ['Ravi']))


def test_real_business_exact_voice_approval_separate_worker_one_post_and_restart(connected):
    from main import converse
    case = connected
    row = proposal(case)
    for client in (case.clients['other'], case.clients['child']):
        with pytest.raises(HubError): client.business_message(row['id'])
    with pytest.raises(HubError): case.other_worker.message(row['id'])
    with pytest.raises(HubError):
        BusinessWorkerClient(case.hub.url, 'owner-'+'x'*40).claim_message(row['id'], 'a'*64)
    with pytest.raises(HubError): HubClient(case.hub.url, case.token).capabilities()
    with pytest.raises(HubError): case.worker._request('POST', 'messages/'+row['id']+'/decide', {'decision':'approve','argument_hash':'a'*64})

    class Listener:
        items = iter(['review business message', 'send reviewed business message', 'yes', 'send reviewed business message', None])
        def listen(self, **_): return next(self.items)
    class Speaker:
        def __init__(self): self.said, self.prompts = [], []
        def say(self, text, **_): self.said.append(text)
        def say_tracked(self, text, **_):
            self.prompts.append(text)
            return 'completed'
    class NoModel:
        def handle(self, text): pytest.fail('A trusted send command reached the language model: '+text)
    speaker = Speaker()
    converse(NoModel(), Listener(), speaker, trusted_commands=TrustedCommands([case.review]))
    final = case.hub.business_message(row['id'])
    assert final['status'] == 'accepted' and final['evidence']['delivery_status'] == 'unverified'
    assert final['evidence']['provider_message_id'] == 'wamid.synthetic-integration'
    assert len(case.meta.posts) == len(speaker.prompts) == 1
    assert all(value in speaker.prompts[0] for value in ('Synthetic business','+919876543210','Ravi','+919123456789','Hello Ravi, your order is ready.'))
    shown = json.dumps(speaker.said+speaker.prompts+[case.tools['list_business_messages'].func()])
    assert 'PRIVATE_SYNTHETIC_PROOF' not in shown and 'synthetic-meta-credential' not in shown and case.token not in shown
    case.restart()
    assert case.hub.business_message(row['id']) == final
    with pytest.raises(HubError): case.worker.claim_message(row['id'], final['argument_hash'])
    print(json.dumps({'acceptance':'real-rust-whatsapp-business-template','provider':'synthetic Meta HTTP',
        'native_private_connection':True,'paired_worker_scopes_distinct':True,'exact_confirmations':1,
        'provider_attempts':1,'accepted_not_delivered':True,'restart_persisted':True,'actual_message_sent':False}))


def test_real_permission_revocation_after_claim_before_post_is_rechecked(connected):
    case = connected
    row = proposal(case)
    revoked = []
    def revoke_during_provider_read(_):
        if not revoked and case.hub.business_message(row['id'])['status'] == 'executing':
            revoked.append(True)
            payload = {**case.permission_input, 'expected_version': case.permission['version'], 'status': 'revoked',
                       'allowed_categories': [], 'recorded_at': int(time.time()), 'expires_at': 0}
            case.worker.set_permission(payload)
    case.meta.hook = revoke_during_provider_read
    message = case.hub.business_message(row['id'])
    result = case.coordinator.decide_and_execute(row['id'], message['argument_hash'], True, can_execute=lambda:True)
    assert revoked == [True] and case.meta.posts == [] and result['status'] == 'failed'
    assert result['evidence']['provider_message_id'] is None


def test_real_lost_provider_receipt_becomes_unknown_and_is_not_resent(connected):
    case = connected
    row = proposal(case)
    case.meta.lose_receipt = True
    message = case.hub.business_message(row['id'])
    result = case.coordinator.decide_and_execute(row['id'], message['argument_hash'], True, can_execute=lambda:True)
    assert result['status']=='unknown' and len(case.meta.posts)==1
    case.restart()
    assert case.hub.business_message(row['id'])['status']=='unknown'
    with pytest.raises(HubError): case.worker.claim_message(row['id'], message['argument_hash'])
    assert len(case.meta.posts)==1
