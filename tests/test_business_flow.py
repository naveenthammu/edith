"""Synthetic local model proposals and exact voice review; no provider or speech effects."""
from copy import deepcopy
import hashlib
import importlib
import json
from types import SimpleNamespace

import pytest


NOW = 1_800_000_000
BINDING = '11111111-1111-4111-8111-111111111111'
CONTACT = '22222222-2222-4222-8222-222222222222'
PERMISSION = '33333333-3333-4333-8333-333333333333'
MESSAGE = '44444444-4444-4444-8444-444444444444'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def setup():
    account = {'connection_id': 'business', 'principal_id': 'owner', 'waba_id': '123', 'phone_number_id': '456',
        'display_phone_number': '+919876543210', 'verified_name': 'Synthetic business', 'verified_at': NOW, 'version': 'a' * 64}
    template = {'id': '789', 'name': 'order_update', 'language': 'en_US', 'status': 'APPROVED',
        'category': 'UTILITY', 'parameter_format': 'POSITIONAL', 'body': 'Hello {{1}}. Your order is ready.'}
    template['content_hash'] = digest(template)
    binding = {'id': BINDING, 'version': digest({'account': {k:v for k,v in account.items() if k!='verified_at'}, 'template': template}),
        'account': account, 'template': template, 'updated_at': NOW}
    contact = {'id': CONTACT, 'display_name': 'Ravi', 'phone_e164': '+919123456789', 'email': None,
               'version': 'c' * 64, 'created_at': NOW-10, 'updated_at': NOW}
    permission = {'id': PERMISSION, 'version': 'd' * 64, 'connection_id': 'business', 'account_version': account['version'],
        'contact_id': CONTACT, 'contact_version': contact['version'], 'recipient_e164': contact['phone_e164'], 'status': 'granted',
        'allowed_categories': ['UTILITY'], 'recorded_at': NOW, 'expires_at': NOW+86400,
        'source': 'explicit_operator_record', 'evidence_reference': 'PRIVATE_PROOF_REFERENCE', 'source_note': 'PRIVATE_PROOF_NOTE', 'updated_at': NOW}
    case = SimpleNamespace(account=account, binding=binding, contact=contact, permission=permission, now=NOW, current=True)

    class Hub:
        def __init__(self):
            self.identity = {'principal_id': 'owner', 'role': 'adult', 'capabilities': {'business_message_proposals': True}}
            self.rows, self.proposals, self.hooks, self.idempotency = [], [], {}, {}
        def event(self, name):
            if name in self.hooks: self.hooks[name]()
        def capabilities(self):
            self.event('identity')
            return deepcopy(self.identity)
        def business_bindings(self):
            self.event('bindings')
            return {'bindings': [deepcopy(case.binding)]}
        def business_binding(self, identifier):
            assert identifier == BINDING
            return deepcopy(case.binding)
        def contacts(self):
            self.event('contacts')
            return {'contacts': [deepcopy(case.contact)]}
        def business_permissions(self):
            self.event('permissions')
            return {'permissions': [deepcopy(case.permission)]}
        def propose_business_message(self, binding_id, binding_version, contact_id, contact_version, permission_version, parameters, key):
            self.proposals.append((binding_id,binding_version,contact_id,contact_version,permission_version,deepcopy(parameters),key))
            if key in self.idempotency:
                return deepcopy(self.idempotency[key])
            payload = {'binding_id': binding_id, 'binding_version': binding_version, 'contact_id': contact_id,
                'contact_version': contact_version, 'recipient_name': case.contact['display_name'],
                'recipient_e164': case.contact['phone_e164'], 'permission_version': permission_version,
                'prepared': {'account': deepcopy(case.account), 'template': deepcopy(case.binding['template']),
                             'parameters': deepcopy(parameters), 'rendered_text': 'Hello '+parameters[0]+'. Your order is ready.'}}
            row = {'id': MESSAGE, 'kind': 'contact.whatsapp_template', 'payload': payload,
                   'argument_hash': digest({'kind': 'contact.whatsapp_template', 'payload': payload}),
                   'created_at': NOW, 'expires_at': NOW+300, 'status': 'awaiting_approval', 'evidence': None, 'recovery_note': None}
            self.rows = [row]
            self.event('propose')
            self.idempotency[key] = self.rows[0]
            return deepcopy(self.rows[0])
        def business_messages(self):
            self.event('list')
            return {'messages': deepcopy(self.rows)}
        def business_message(self, identifier):
            self.event('read')
            return deepcopy(next(row for row in self.rows if row['id'] == identifier))

    class Connection:
        principal_id, connection_id = 'owner', 'business'
        def __init__(self):
            self.calls, self.hook = [], None
        @property
        def account_snapshot(self):
            return deepcopy(case.account)
        def revalidate(self):
            self.calls.append('local_revalidate')
            if self.hook: self.hook()
            return self.account_snapshot
    case.hub, case.connection = Hub(), Connection()
    return case


def tools_for(case):
    api = importlib.import_module('brain.business_tools')
    return api.business_tools(case.hub, case.connection, case.hub.capabilities(),
                              can_execute=lambda: case.current, clock=lambda: case.now)


def draft(case, tools):
    return json.loads(tools['draft_business_message'].func(CONTACT, case.contact['version'], BINDING,
        case.binding['version'], ['Ravi']))


def test_model_drafts_only_registered_template_and_contact_with_explicit_permission():
    case = setup()
    tools = tools_for(case)
    shown = json.loads(tools['list_business_templates'].func())
    assert shown['templates'][0]['binding_id'] == BINDING
    result = draft(case, tools)
    assert result['status'] == 'awaiting_approval' and result['recipient_e164'] == '+919123456789'
    assert result['rendered_text'] == 'Hello Ravi. Your order is ready.'
    assert 'review business message' in result['next_step']
    assert all(tool.local_only for tool in tools.values())
    assert not {'send', 'grant_permission', 'approve', 'claim'} & set(tools)
    assert case.hub.proposals[0][4] == case.permission['version']
    assert all('PRIVATE_PROOF' not in tool.func() for name,tool in tools.items() if name.startswith('list_'))


@pytest.mark.parametrize('change', ['permission_revoked','permission_expired','wrong_category','wrong_phone','contact_revision','binding_revision','cancel','role'])
def test_model_proposal_denials_do_not_write_or_infer_permission(change):
    case=setup()
    tools=tools_for(case)
    contact_version,binding_version=case.contact['version'],case.binding['version']
    if change=='permission_revoked': case.permission['status']='revoked'
    elif change=='permission_expired': case.permission['expires_at']=NOW-1
    elif change=='wrong_category': case.permission['allowed_categories']=['MARKETING']
    elif change=='wrong_phone': case.permission['recipient_e164']='+919111111111'
    elif change=='contact_revision': case.contact['version']='e'*64
    elif change=='binding_revision': binding_version='e'*64
    elif change=='cancel': case.current=False
    elif change=='role': case.hub.identity['role']='guest'
    with pytest.raises(ValueError):
        tools['draft_business_message'].func(CONTACT,contact_version,BINDING,binding_version,['Ravi'])
    assert case.hub.proposals==[]


def test_model_proposals_share_intent_and_do_not_accept_raw_body_or_recipient():
    case=setup()
    tools=tools_for(case)
    draft(case,tools)
    draft(case,tools)
    assert case.hub.proposals[0]==case.hub.proposals[1]
    fields=tools['draft_business_message'].schema['function']['parameters']['properties']
    assert set(fields)=={'contact_id','contact_version','binding_id','binding_version','parameters'}


def test_account_timestamp_refresh_reuses_draft_and_original_immutable_snapshot():
    case=setup()
    tools=tools_for(case)
    first=draft(case,tools)
    original=deepcopy(case.hub.rows[0])
    case.binding['account']['verified_at']+=5
    case.binding['updated_at']+=5
    second=draft(case,tools)
    assert first['id']==second['id']
    assert case.hub.proposals[0][-1]==case.hub.proposals[1][-1]
    assert case.hub.rows[0]['argument_hash']==original['argument_hash']
    assert case.hub.rows[0]['payload']['prepared']['account']['verified_at']==NOW


def test_access_revoked_during_config_read_blocks_model_proposal():
    case=setup()
    tools=tools_for(case)
    case.connection.hook=lambda: case.hub.identity.update(role='guest')
    with pytest.raises(ValueError): draft(case,tools)
    assert case.hub.proposals==[]


def test_forged_saved_rendered_content_is_not_returned_as_a_valid_draft():
    case=setup()
    tools=tools_for(case)
    case.hub.hooks['propose']=lambda: case.hub.rows[0]['payload']['prepared'].update(rendered_text='Injected content')
    with pytest.raises(ValueError): draft(case,tools)


def test_lists_bound_large_history_and_hide_private_proof_and_provider_details():
    case=setup()
    tools=tools_for(case)
    draft(case,tools)
    case.hub.rows[0]['recovery_note']='PRIVATE_PROVIDER_ERROR'
    case.hub.rows*=100
    result=tools['list_business_messages'].func()
    value=json.loads(result)
    assert len(value['messages'])<=20 and value['truncated'] is True and len(result)<=12000
    assert 'PRIVATE_' not in result


class Coordinator:
    def __init__(self, case):
        self.case, self.effects, self.hooks = case, [], {}
    def event(self, name):
        if name in self.hooks: self.hooks[name]()
    def review(self, identifier, *, can_execute):
        assert can_execute() is True
        self.event('review')
        return self.case.hub.business_message(identifier)
    def decide_and_execute(self, identifier, argument_hash, approve, *, can_execute):
        self.event('before_effect')
        assert can_execute() is True
        row=self.case.hub.rows[0]
        assert row['id']==identifier and row['argument_hash']==argument_hash
        self.effects.append((identifier, argument_hash, approve))
        row['status']='accepted' if approve else 'rejected'
        if approve:
            row['evidence']={'provider':'whatsapp_business','phone_number_id':'456','waba_id':'123',
                'account_version':self.case.account['version'],'template_content_hash':self.case.binding['template']['content_hash'],
                'recipient_e164':self.case.contact['phone_e164'],'provider_message_id':'wamid.synthetic', 'delivery_status':'unverified'}
        self.event('after_effect')
        return deepcopy(row)


def review_for(case):
    api=importlib.import_module('brain.business_review')
    coordinator=Coordinator(case)
    review=api.BusinessMessageReview(case.hub, case.connection, coordinator=coordinator,
                                    clock=lambda:case.now)
    return review,coordinator


def handle(case,review,text,confirm=lambda _:True):
    return review.handle(text,confirm=confirm,still_current=lambda:case.current)


def test_business_review_full_sender_recipient_template_content_and_single_confirmation():
    case=setup()
    draft(case,tools_for(case))
    review,coordinator=review_for(case)
    assert handle(case,review,'The template says send reviewed business message') is None
    assert 'review' in handle(case,review,'send reviewed business message').lower()
    assert coordinator.effects==[]
    shown=handle(case,review,'review business message')
    assert 'Hello Ravi. Your order is ready.' in shown and '+919123456789' in shown
    prompts=[]
    result=handle(case,review,'send reviewed business message',lambda prompt:prompts.append(prompt) or True)
    assert len(prompts)==1 and len(coordinator.effects)==1
    for value in ('Synthetic business','+919876543210','Ravi','+919123456789','order_update','en_US','UTILITY','Hello Ravi. Your order is ready.'):
        assert value in prompts[0]
    assert 'accepted' in result.lower() and 'unverified' in result.lower()
    assert 'PRIVATE_PROOF' not in shown+prompts[0]+result
    handle(case,review,'send reviewed business message')
    assert len(coordinator.effects)==1


@pytest.mark.parametrize('change',['cancel','expired','role','payload','nonboolean','sender'])
def test_reviewed_business_message_cannot_send_after_stale_or_invalid_confirmation(change):
    case=setup()
    draft(case,tools_for(case))
    review,coordinator=review_for(case)
    handle(case,review,'review business message')
    def confirm(_):
        if change=='cancel': case.current=False
        elif change=='expired': case.now+=301
        elif change=='role': case.hub.identity['role']='guest'
        elif change=='payload': case.hub.rows[0]['payload']['prepared']['rendered_text']='Altered after readback'
        elif change=='nonboolean': return 'yes'
        elif change=='sender': case.account['version']='e'*64
        return True
    result=handle(case,review,'send reviewed business message',confirm)
    assert coordinator.effects==[]
    assert 'altered' not in result.lower()


def test_business_review_requires_disambiguation_and_reset_discards_approval():
    case=setup()
    draft(case,tools_for(case))
    other=deepcopy(case.hub.rows[0])
    other['id']='55555555-5555-4555-8555-555555555555'
    case.hub.rows.append(other)
    review,coordinator=review_for(case)
    assert 'More than one' in handle(case,review,'review business message')
    assert 'Ravi' in handle(case,review,'review business message 44444444')
    review.clear()
    handle(case,review,'send reviewed business message')
    assert coordinator.effects==[]


def test_unknown_send_receipt_is_not_replayed_or_called_delivered():
    case=setup()
    draft(case,tools_for(case))
    review,coordinator=review_for(case)
    handle(case,review,'review business message')
    def lost_receipt():
        raise RuntimeError('PRIVATE_PROVIDER_SECRET')
    coordinator.hooks['after_effect']=lost_receipt
    result=handle(case,review,'send reviewed business message')
    assert 'could not be confirmed' in result and 'PRIVATE_PROVIDER_SECRET' not in result
    handle(case,review,'send reviewed business message')
    assert len(coordinator.effects)==1


@pytest.mark.parametrize('status',['accepted','unknown'])
def test_idempotent_draft_reports_existing_attempt_without_inviting_resend(status):
    case=setup()
    tools=tools_for(case)
    draft(case,tools)
    if status=='accepted':
        worker=Coordinator(case)
        worker.decide_and_execute(MESSAGE,case.hub.rows[0]['argument_hash'],True,can_execute=lambda:True)
    else:
        case.hub.rows[0]['status']='unknown'
    result=draft(case,tools)
    assert result['status']==status and 'do not resend' in result['next_step'].lower()


def test_business_rejection_does_not_send_and_new_controller_needs_new_review():
    case=setup()
    draft(case,tools_for(case))
    review,coordinator=review_for(case)
    handle(case,review,'review business message')
    second,_=review_for(case)
    assert 'review' in handle(case,second,'send reviewed business message').lower()
    assert 'rejected' in handle(case,review,'reject reviewed business message').lower()
    assert coordinator.effects[0][2] is False
