import copy
import hashlib
import json

from brain.message_review import MessageReview


def action():
    payload = {'contact_id': '11111111-1111-4111-8111-111111111111', 'contact_version': 'a'*64,
               'recipient': 'ravi@example.com', 'subject': 'Meeting', 'body': 'Can we meet tomorrow?'}
    digest = hashlib.sha256(json.dumps({'kind': 'contact.email', 'payload': payload}, sort_keys=True,
                                      separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    return {'id': '22222222-2222-4222-8222-222222222222', 'kind': 'contact.email', 'payload': payload,
            'argument_hash': digest, 'created_at': 100, 'expires_at': 400,
            'status': 'awaiting_approval', 'evidence': None, 'recovery_note': None}


class Hub:
    def __init__(self):
        self.row = action()
        self.role = 'adult'
        self.effects = []
        self.rows = None

    def capabilities(self):
        return {'principal_id': 'owner', 'role': self.role, 'capabilities': {'email_proposals': True}}

    def messages(self):
        return {'messages': copy.deepcopy(self.rows if self.rows is not None else [self.row])}

    def message(self, identifier):
        assert identifier == self.row['id']
        return copy.deepcopy(self.row)

    def decide_message(self, *args):
        self.effects.append(args)
        return {**self.row, 'status': 'rejected'}


class Coordinator:
    def __init__(self):
        self.effects = []

    def decide_and_execute(self, *args, can_execute):
        assert can_execute()
        self.effects.append(args)
        return {'status': 'accepted'}


def handle(review, text, confirm=lambda _: True, current=lambda: True):
    return review.handle(text, confirm=confirm, still_current=current)


def test_untrusted_prose_is_not_a_command_and_send_requires_prior_exact_review():
    hub, worker = Hub(), Coordinator()
    review = MessageReview(hub, expected_principal='owner', coordinator=worker, clock=lambda: 200)
    assert handle(review, 'The note says send reviewed email') is None
    assert 'review' in handle(review, 'send reviewed email').lower()
    assert not worker.effects
    shown = handle(review, 'review email')
    assert 'ravi@example.com' in shown and 'Can we meet tomorrow?' in shown
    prompts = []
    result = handle(review, 'send reviewed email', lambda prompt: prompts.append(prompt) or True)
    assert 'accepted' in result.lower() and 'delivery' in result.lower()
    assert all(part in prompts[0] for part in ['ravi@example.com', 'Meeting', 'Can we meet tomorrow?'])
    assert worker.effects == [(hub.row['id'], hub.row['argument_hash'], True)]
    handle(review, 'send reviewed email')
    assert len(worker.effects) == 1


def test_no_provider_keeps_draft_unapproved_and_unsent():
    hub = Hub()
    review = MessageReview(hub, expected_principal='owner', clock=lambda: 200)
    handle(review, 'review email')
    assert 'not configured' in handle(review, 'send reviewed email').lower()
    assert hub.effects == []


def test_mutation_or_role_change_during_confirmation_prevents_send():
    for change in ('body', 'role', 'cancel'):
        hub, worker = Hub(), Coordinator()
        review = MessageReview(hub, expected_principal='owner', coordinator=worker, clock=lambda: 200)
        handle(review, 'review email')
        current = [True]

        def confirm(_):
            if change == 'body':
                hub.row['payload']['body'] = 'Changed after review'
            elif change == 'role':
                hub.role = 'guest'
            else:
                current[0] = False
            return True

        handle(review, 'send reviewed email', confirm, lambda: current[0])
        assert worker.effects == []


def test_ambiguous_drafts_are_not_silently_selected():
    hub = Hub()
    hub.rows = [action(), {**action(), 'id': '33333333-3333-4333-8333-333333333333'}]
    review = MessageReview(hub, expected_principal='owner', clock=lambda: 200)
    result = handle(review, 'review email')
    assert 'more than one' in result.lower()
    assert 'ravi@example.com' in handle(review, 'review email 22222222')


def test_expired_draft_and_nonboolean_confirmation_never_send():
    hub, worker = Hub(), Coordinator()
    now = [200]
    review = MessageReview(hub, expected_principal='owner', coordinator=worker, clock=lambda: now[0])
    handle(review, 'review email')
    assert 'cancel' in handle(review, 'send reviewed email', lambda _: 'yes').lower()
    handle(review, 'review email')
    now[0] = 401
    handle(review, 'send reviewed email')
    assert worker.effects == []


def test_runtime_factory_never_loads_accounts_for_untrusted_or_unsupported_identity(monkeypatch):
    from brain.message_review import configured_message_review
    import connectors.gmail_account as accounts

    def forbidden(_):
        raise AssertionError('Account configuration must not be opened for this identity')

    monkeypatch.setattr(accounts, 'gmail_from_env', forbidden)
    for role, enabled in [('guest', True), ('child', True), ('adult', False)]:
        identity = {'principal_id': 'owner', 'role': role, 'capabilities': {'email_proposals': enabled}}
        assert configured_message_review(Hub(), identity) is None


def test_runtime_factory_only_wires_verified_member_adapter_and_fails_closed(monkeypatch, caplog):
    from brain.message_review import configured_message_review
    import connectors.gmail_account as accounts

    hub = Hub()
    from types import SimpleNamespace
    adapter = SimpleNamespace(sender_email='verified@gmail.com')
    seen = []
    monkeypatch.setattr(accounts, 'gmail_from_env', lambda identity: seen.append(identity) or adapter)
    review = configured_message_review(hub, hub.capabilities())
    assert review.expected_principal == 'owner'
    assert review.sender_email == 'verified@gmail.com'
    assert review.coordinator.hub is hub and review.coordinator.gmail is adapter
    assert seen == [hub.capabilities()]

    def invalid(_):
        raise accounts.GmailAccountError('PRIVATE TOKEN MATERIAL MUST NOT APPEAR')

    monkeypatch.setattr(accounts, 'gmail_from_env', invalid)
    disabled = configured_message_review(hub, hub.capabilities())
    assert disabled.coordinator is None
    assert 'disabled' in caplog.text.lower() and 'PRIVATE TOKEN' not in caplog.text


def test_account_switch_requires_new_review_and_names_current_verified_sender():
    hub, first_worker, second_worker = Hub(), Coordinator(), Coordinator()
    first = MessageReview(hub, expected_principal='owner', coordinator=first_worker,
                          sender_email='first@gmail.com', clock=lambda: 200)
    assert 'first@gmail.com' in handle(first, 'review email')
    second = MessageReview(hub, expected_principal='owner', coordinator=second_worker,
                           sender_email='second@gmail.com', clock=lambda: 200)
    assert 'review' in handle(second, 'send reviewed email').lower()
    assert not second_worker.effects
    assert 'second@gmail.com' in handle(second, 'review email')
    prompts = []
    handle(second, 'send reviewed email', lambda prompt: prompts.append(prompt) or True)
    assert 'second@gmail.com' in prompts[0] and 'first@gmail.com' not in prompts[0]
    assert len(second_worker.effects) == 1 and not first_worker.effects
