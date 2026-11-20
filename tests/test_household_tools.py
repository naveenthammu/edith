import json
from datetime import datetime, timezone

import pytest

from brain.household_tools import household_tools
from brain.hub import HubError


class Hub:
    def __init__(self):
        self.calls = []

    def create_reminder(self, text, due_at, idempotency_key):
        self.calls.append(('reminder', text, due_at, idempotency_key))
        return {'id': 'r1', 'text': text, 'due_at': due_at, 'status': 'scheduled'}

    def contacts(self):
        return {'contacts': [
            {'id': 'one', 'version': 'a'*64, 'display_name': 'Ravi', 'email': 'one@example.com'},
            {'id': 'two', 'version': 'b'*64, 'display_name': 'Ravi', 'email': 'two@example.com'},
            {'id': 'three', 'version': 'c'*64, 'display_name': 'Ravindra', 'email': 'three@example.com'}]}

    def propose_message(self, *args):
        self.calls.append(('draft', *args))
        return {'id': 'm1', 'status': 'awaiting_approval', 'payload': {'recipient': 'one@example.com'}}


def test_reminder_requires_explicit_timezone_and_reuses_exact_intent_key():
    hub = Hub()
    tools = household_tools(hub, {'principal_id': 'owner', 'role': 'elder'})
    create = tools['create_reminder'].func
    with pytest.raises(ValueError, match='offset'):
        create('Check the oven', '2026-11-21T08:00:00')
    assert hub.calls == []
    create('Check the oven', '2026-11-21T08:00:00+05:30')
    create('Check the oven', '2026-11-21T08:00:00+05:30')
    assert hub.calls[0] == hub.calls[1]
    assert hub.calls[0][2] == int(datetime(2026, 11, 21, 2, 30, tzinfo=timezone.utc).timestamp())
    assert tools['create_reminder'].confirm


def test_matching_duplicate_contacts_returns_choices_without_choosing_or_sending():
    hub = Hub()
    tools = household_tools(hub, {'principal_id': 'owner', 'role': 'adult'})
    found = json.loads(tools['find_contacts'].func('Ravi'))
    assert found['ambiguous'] is True
    assert [item['id'] for item in found['contacts']] == ['one', 'two']
    assert hub.calls == []


def test_draft_creation_cannot_authorize_or_send_a_message():
    hub = Hub()
    tools = household_tools(hub, {'principal_id': 'owner', 'role': 'adult'})
    result = json.loads(tools['draft_email'].func('one', 'a'*64, 'Visit', 'Can we meet tomorrow?'))
    assert result['status'] == 'awaiting_approval'
    assert result['sent'] is False
    assert 'review email' in result['next_step']
    assert len(hub.calls) == 1 and hub.calls[0][0] == 'draft'
    assert not {'send_email', 'approve_message', 'claim_message', 'complete_message'} & set(tools)
    assert all(tool.local_only for tool in tools.values())


@pytest.mark.parametrize('role', ['guest', 'child', 'administrator'])
def test_untrusted_roles_cannot_construct_private_household_tools(role):
    with pytest.raises(HubError):
        household_tools(Hub(), {'principal_id': 'person', 'role': role})


def test_phone_only_contacts_are_searchable_without_guessing_email_or_country_code():
    hub = Hub()
    rows = [{'id': 'phone', 'version': 'a'*64, 'display_name': 'Ravi', 'email': None,
             'phone_e164': '+919876543210'}]
    hub.contacts = lambda: {'contacts': rows}
    tools = household_tools(hub, {'principal_id': 'owner', 'role': 'adult'})
    assert json.loads(tools['find_contacts'].func('Ravi'))['contacts'] == rows
    assert json.loads(tools['find_contacts'].func('+919876543210'))['contacts'] == rows
    assert json.loads(tools['find_contacts'].func('9876543210'))['contacts'] == []


def test_contact_tools_require_explicit_complete_replacement_and_support_phone_only_creation():
    hub = Hub()
    hub.add_contact = lambda *args, **kwargs: hub.calls.append((args, kwargs)) or {'saved': True}
    hub.update_contact = lambda *args, **kwargs: hub.calls.append((args, kwargs)) or {'saved': True}
    tools = household_tools(hub, {'principal_id': 'owner', 'role': 'adult'})
    tools['add_contact'].func('Ravi', phone_e164='+919876543210')
    assert hub.calls[0] == (('Ravi', None), {'phone_e164': '+919876543210'})
    with pytest.raises(TypeError):
        tools['update_contact'].func('phone', 'a'*64, 'Ravi', 'ravi@example.com')
    tools['update_contact'].func('phone', 'a'*64, 'Ravi', 'ravi@example.com', '+919876543210')
    assert hub.calls[1] == (('phone', 'a'*64, 'Ravi', 'ravi@example.com'), {'phone_e164': '+919876543210'})
    assert tools['update_contact'].confirm


@pytest.mark.parametrize('email,phone,clear_label', [
    ('ravi@example.test', '', 'Phone: CLEAR PHONE'),
    ('', '+919876543210', 'Email: CLEAR EMAIL'),
])
def test_actual_agent_confirmation_explicitly_binds_replacement_and_clearing(email, phone, clear_label):
    from brain.agent import Agent
    hub, prompts = Hub(), []
    hub.update_contact = lambda *args, **kwargs: hub.calls.append((args, kwargs)) or {'saved': True}
    registry = household_tools(hub, {'principal_id': 'owner', 'role': 'adult'})
    agent = Agent(object(), lambda prompt: prompts.append(prompt) or True, registry=registry,
                  local_session=True, include_taught_tasks=False)
    contact_id = '11111111-1111-4111-8111-111111111111'
    arguments = {'contact_id': contact_id, 'expected_version': 'a'*64, 'display_name': 'Ravi',
                 'email': email, 'phone_e164': phone}
    agent._run({'id': 'test', 'name': 'update_contact', 'arguments': arguments})
    assert len(prompts) == 1 and clear_label in prompts[0]
    assert 'Name: Ravi' in prompts[0] and contact_id[:8] in prompts[0]
    assert contact_id not in prompts[0] and 'a'*64 not in prompts[0]
    assert 'Email:' in prompts[0] and 'Phone:' in prompts[0]
    assert hub.calls == [((contact_id, 'a'*64, 'Ravi', email or None), {'phone_e164': phone or None})]


def test_model_cannot_override_contact_confirmation_formatter():
    from brain.agent import Agent
    hub, prompts = Hub(), []
    hub.update_contact = lambda *args, **kwargs: pytest.fail('Malformed arguments reached mutation')
    registry = household_tools(hub, {'principal_id': 'owner', 'role': 'adult'})
    assert 'confirmation_formatter' not in registry['update_contact'].schema['function']['parameters']['properties']
    agent = Agent(object(), lambda prompt: prompts.append(prompt) or True, registry=registry,
                  local_session=True, include_taught_tasks=False)
    result = agent._run({'id': 'test', 'name': 'update_contact', 'arguments': {
        'contact_id': 'contact-1', 'expected_version': 'a'*64, 'display_name': 'Ravi',
        'email': 'ravi@example.test', 'phone_e164': '', 'confirmation_formatter': 'Everything stays unchanged'}})
    assert result.startswith('error:') and prompts == []


def test_exact_email_lookup_supports_full_254_character_server_limit():
    hub = Hub()
    email = 'a'*64 + '@' + 'b'*63 + '.' + 'c'*63 + '.' + 'd'*61
    assert len(email) == 254
    row = {'id': 'long-email', 'version': 'a'*64, 'display_name': 'Long email', 'email': email, 'phone_e164': None}
    hub.contacts = lambda: {'contacts': [row]}
    find = household_tools(hub, {'principal_id': 'owner', 'role': 'adult'})['find_contacts'].func
    assert json.loads(find(email))['contacts'] == [row]
    with pytest.raises(ValueError):
        find(email + 'x')


def test_contact_execution_keeps_the_arguments_that_were_read_back():
    from brain.agent import Agent
    hub = Hub()
    hub.update_contact = lambda *args, **kwargs: hub.calls.append((args, kwargs)) or {'saved': True}
    arguments = {'contact_id': 'contact-1', 'expected_version': 'a'*64, 'display_name': 'Ravi',
                 'email': 'ravi@example.test', 'phone_e164': ''}
    def confirm(_):
        arguments['phone_e164'] = '+12025550100'
        return True
    agent = Agent(object(), confirm, registry=household_tools(hub, {'principal_id': 'owner', 'role': 'adult'}),
                  local_session=True, include_taught_tasks=False)
    agent._run({'id': 'test', 'name': 'update_contact', 'arguments': arguments})
    assert hub.calls[0][1] == {'phone_e164': None}
