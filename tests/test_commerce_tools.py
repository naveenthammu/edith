import copy
from datetime import datetime, timezone
import hashlib
import json

import pytest

from brain.commerce_tools import commerce_tools


NOW = 1_800_000_000
VARIANT = 'gid://shopify/ProductVariant/21'
MERCHANT = {'id': 'test-shop', 'display_name': 'Synthetic shop', 'shop_domain': 'synthetic.myshopify.com',
            'checkout_hosts': ['synthetic.myshopify.com'], 'version': 'a' * 64}
IDENTITY = {'principal_id': 'owner', 'role': 'adult'}


class Store:
    def __init__(self):
        self.calls = []
        self.price = 100000

    def catalog(self, query, limit=5, *, can_execute):
        assert can_execute()
        self.calls.append((query, limit))
        return {'provider': 'shopify', 'shop_domain': MERCHANT['shop_domain'],
                'source_url': 'https://synthetic.myshopify.com/api/2026-10/graphql.json',
                'observed_at': datetime.fromtimestamp(NOW, timezone.utc).isoformat(),
                'has_more_products': False,
                'products': [{'product_id': 'gid://shopify/Product/11', 'title': 'Shirt',
                    'source_url': 'https://synthetic.myshopify.com/products/shirt', 'has_more_variants': False,
                    'variants': [{'variant_id': VARIANT, 'title': 'Blue / M', 'available_for_sale': True,
                                  'price_minor': self.price, 'currency': 'INR'}]}]}


class Account:
    def __init__(self):
        self.merchant = copy.deepcopy(MERCHANT)
        self.storefront = Store()

    def revalidate(self, merchant):
        assert merchant == self.merchant


class Hub:
    def __init__(self):
        self.identity = dict(IDENTITY)
        self.merchant = copy.deepcopy(MERCHANT)
        self.calls = []
        self.rows = []

    def capabilities(self):
        return {**self.identity, 'capabilities': {'commerce_proposals': True}}

    def merchants(self):
        return {'merchants': [copy.deepcopy(self.merchant)]}

    def propose_commerce(self, merchant_id, merchant_version, lines, currency, observed_at, key):
        self.calls.append(copy.deepcopy((merchant_id, merchant_version, lines, currency, observed_at, key)))
        row = {'id': '11111111-1111-4111-8111-111111111111', 'status': 'awaiting_approval',
               'kind': 'commerce.prepare_cart', 'created_at': NOW, 'evidence': None,
               'recovery_note': None, 'checkout_evidence': None,
               'expires_at': NOW + 300, 'checkout_status': 'not_opened', 'payload': {
                   'merchant_id': merchant_id, 'merchant_version': merchant_version,
                   'merchant_name': self.merchant['display_name'], 'shop_domain': self.merchant['shop_domain'],
                   'lines': copy.deepcopy(lines), 'currency': currency, 'observed_at': observed_at,
                   'quoted_subtotal_minor': sum(line['quantity'] * line['unit_price_minor'] for line in lines)}}
        row['argument_hash'] = hashlib.sha256(json.dumps({'kind': row['kind'], 'payload': row['payload']},
            sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        self.rows = [row]
        return copy.deepcopy(row)

    def commerce(self):
        return {'commerce': copy.deepcopy(self.rows)}


def setup_tools(*, now=None, permitted=lambda: True):
    hub, account = Hub(), Account()
    return hub, account, commerce_tools(hub, account, IDENTITY, can_execute=permitted,
                                       clock=(lambda: NOW) if now is None else now)


def test_model_can_only_propose_recent_observed_variants_with_provider_prices():
    hub, account, tools = setup_tools()
    with pytest.raises(ValueError):
        tools['propose_cart'].func([{'variant_id': VARIANT, 'quantity': 2}])
    assert hub.calls == []
    tools['search_store_catalog'].func('blue shirt')
    result = json.loads(tools['propose_cart'].func([{'variant_id': VARIANT, 'quantity': 2}]))
    assert hub.calls[0][2] == [{'variant_id': VARIANT, 'title': 'Shirt — Blue / M',
                              'quantity': 2, 'unit_price_minor': 100000}]
    assert result['quoted_subtotal_minor'] == 200000
    assert result['status'] == 'awaiting_approval' and 'review cart' in result['next_step']
    assert account.storefront.calls == [('blue shirt', 5)]
    assert tools['propose_cart'].local_only and not tools['search_store_catalog'].local_only
    assert not {'create_cart', 'open_checkout', 'approve_cart', 'pay'} & set(tools)


@pytest.mark.parametrize('items', [
    [{'variant_id': VARIANT, 'quantity': 1, 'unit_price_minor': 1}],
    [{'variant_id': 'gid://shopify/ProductVariant/999', 'quantity': 1}],
    [{'variant_id': VARIANT, 'quantity': True}],
    [{'variant_id': VARIANT, 'quantity': 11}],
    [{'variant_id': VARIANT, 'quantity': 1}] * 2,
])
def test_model_cannot_supply_prices_or_unobserved_or_ambiguous_lines(items):
    hub, _, tools = setup_tools()
    tools['search_store_catalog'].func('shirt')
    with pytest.raises(ValueError):
        tools['propose_cart'].func(items)
    assert hub.calls == []


def test_stale_catalog_identity_revocation_and_cancellation_prevent_proposals():
    for change in ('stale', 'identity', 'cancel'):
        now, allowed = [NOW], [True]
        hub, _, tools = setup_tools(now=lambda: now[0], permitted=lambda: allowed[0])
        tools['search_store_catalog'].func('shirt')
        if change == 'stale':
            now[0] += 301
        elif change == 'identity':
            hub.identity['principal_id'] = 'other'
        else:
            allowed[0] = False
        with pytest.raises(ValueError):
            tools['propose_cart'].func([{'variant_id': VARIANT, 'quantity': 1}])
        assert hub.calls == []


def test_repeated_exact_selection_reuses_intent_without_another_provider_call():
    hub, account, tools = setup_tools()
    tools['search_store_catalog'].func('shirt')
    for _ in range(2):
        tools['propose_cart'].func([{'variant_id': VARIANT, 'quantity': 1}])
    assert hub.calls[0] == hub.calls[1] and len(account.storefront.calls) == 1


def test_model_summaries_never_include_checkout_keys_or_provider_private_fields():
    hub, _, tools = setup_tools()
    tools['search_store_catalog'].func('shirt')
    tools['propose_cart'].func([{'variant_id': VARIANT, 'quantity': 1}])
    hub.rows[0]['recovery_note'] = 'private-provider-body'
    result = tools['list_carts'].func()
    assert json.loads(result)['carts'][0]['id'] == hub.rows[0]['id']
    assert all(secret not in result for secret in ('private-key', 'secret-cart-key', 'private-provider-body'))


@pytest.mark.parametrize('nested', [False, True])
def test_malformed_public_receipts_do_not_leak_private_fields(nested):
    hub, _, tools = setup_tools()
    tools['search_store_catalog'].func('shirt')
    tools['propose_cart'].func([{'variant_id': VARIANT, 'quantity': 1}])
    target = hub.rows[0]['payload']['lines'][0] if nested else hub.rows[0]
    target['checkout_url'] = 'https://synthetic.myshopify.com/cart/c/private-key'
    with pytest.raises(ValueError) as caught:
        tools['list_carts'].func()
    assert 'private-key' not in str(caught.value)


def test_catalog_rejects_price_outside_hub_bound():
    _, account, tools = setup_tools()
    account.storefront.price = 100_000_000_000_000
    with pytest.raises(ValueError):
        tools['search_store_catalog'].func('shirt')


def test_identity_revoked_during_blocking_config_read_never_sends_catalog_query():
    hub, account, tools = setup_tools()
    reads = []
    def revoke_during_provider_gate(merchant):
        reads.append(merchant)
        if len(reads) == 2:
            hub.identity['role'] = 'guest'
    account.revalidate = revoke_during_provider_gate
    with pytest.raises((ValueError, AssertionError)):
        tools['search_store_catalog'].func('private shopping query')
    assert account.storefront.calls == []
