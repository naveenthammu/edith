import pytest

from brain.trusted_commands import TrustedCommands, configured_trusted_commands


class Commands:
    def __init__(self, command, reply):
        self.command, self.reply = command, reply
        self.calls, self.cleared = [], 0

    def handle(self, text, *, confirm, still_current):
        self.calls.append(text)
        return self.reply if text == self.command else None

    def clear(self):
        self.cleared += 1


def test_routes_exact_commands_and_clears_other_review_contexts():
    email, shopping = Commands('review email', 'email review'), Commands('review cart', 'cart review')
    group = TrustedCommands([email, shopping])
    assert group.handle('review cart', confirm=lambda _: True, still_current=lambda: True) == 'cart review'
    assert email.cleared == 1 and shopping.cleared == 0
    assert group.handle('ordinary request', confirm=lambda _: True, still_current=lambda: True) is None
    group.clear()
    assert email.cleared == 2 and shopping.cleared == 1


@pytest.mark.parametrize('role', ['guest', 'child'])
def test_factory_never_loads_accounts_for_shared_roles(monkeypatch, role):
    def unexpected(*_):
        pytest.fail('Shared session must not load private accounts')
    monkeypatch.setattr('brain.message_review.configured_message_review', unexpected)
    monkeypatch.setattr('brain.commerce_review.configured_commerce_review', unexpected)
    monkeypatch.setattr('brain.business_review.configured_business_review', unexpected)
    assert configured_trusted_commands(object(), {'role': role, 'principal_id': role}) is None


def test_factory_combines_only_configured_controllers(monkeypatch):
    controller = Commands('review cart', 'cart review')
    monkeypatch.setattr('brain.message_review.configured_message_review', lambda *_: None)
    monkeypatch.setattr('brain.commerce_review.configured_commerce_review', lambda *_: controller)
    monkeypatch.setattr('brain.business_review.configured_business_review', lambda *_: None)
    group = configured_trusted_commands(object(), {'role': 'adult', 'principal_id': 'owner'})
    assert group.handle('review cart', confirm=lambda _: True, still_current=lambda: True) == 'cart review'
    monkeypatch.setattr('brain.commerce_review.configured_commerce_review', lambda *_: None)
    assert configured_trusted_commands(object(), {'role': 'elder', 'principal_id': 'owner'}) is None


def test_business_review_is_routed_by_configured_existing_voice_composition(monkeypatch):
    controller = Commands('review business message', 'business review')
    monkeypatch.setattr('brain.message_review.configured_message_review', lambda *_: None)
    monkeypatch.setattr('brain.commerce_review.configured_commerce_review', lambda *_: None)
    monkeypatch.setattr('brain.business_review.configured_business_review', lambda *_: controller)
    group = configured_trusted_commands(object(), {'role': 'adult', 'principal_id': 'owner'})
    assert group.handle('review business message', confirm=lambda _: True, still_current=lambda: True) == 'business review'
