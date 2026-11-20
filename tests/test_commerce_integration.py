"""Real local Rust + protected merchant account, with synthetic provider/voice/browser effects."""
import copy
import json
import time

import pytest

from brain.hub import HubError
from brain.commerce_tools import commerce_tools
from brain.commerce_review import CommerceReview
from brain.trusted_commands import TrustedCommands
from connectors.commerce_coordinator import CommerceCoordinator
from connectors.merchant_account import MerchantAccount, configure_merchant_account
from connectors.shopify import ShopifyResponse


SHOP = 'synthetic.myshopify.com'
VARIANT = 'gid://shopify/ProductVariant/101'
PRODUCT = 'gid://shopify/Product/10'
SELECTION = [{'variant_id': VARIANT, 'quantity': 2}]
PRIVATE = {'cart_id': 'gid://shopify/Cart/synthetic?key=privateCartKey',
           'checkout_url': f'https://{SHOP}/cart/c/synthetic?key=privateCheckoutKey'}


def money(amount):
    return {'amount': amount, 'currencyCode': 'INR'}


def connection(nodes):
    return {'nodes': nodes, 'pageInfo': {'hasNextPage': False}}


class Merchant:
    def __init__(self):
        self.calls, self.price = [], '1000.00'

    def __call__(self, request, **_):
        assert request.full_url == f'https://{SHOP}/api/2026-10/graphql.json'
        body = json.loads(request.data)
        query = body['query']
        variant = {'__typename': 'ProductVariant', 'id': VARIANT, 'title': 'Blue / M', 'availableForSale': True,
                   'price': money(self.price), 'product': {'id': PRODUCT, 'title': 'Cotton shirt'}}
        if 'EdithCatalog' in query:
            self.calls.append('catalog')
            result = {'products': connection([{'id': PRODUCT, 'title': 'Cotton shirt',
                'onlineStoreUrl': f'https://{SHOP}/products/shirt', 'variants': connection([variant])}])}
        elif 'EdithExactVariants' in query:
            self.calls.append('quote')
            assert body['variables'] == {'ids': [VARIANT]}
            result = {'nodes': [variant]}
        else:
            cart = {'id': PRIVATE['cart_id'], 'checkoutUrl': PRIVATE['checkout_url'], 'totalQuantity': 2,
                'cost': {'subtotalAmount': money('2000.00'), 'totalAmount': money('2150.00'),
                         'subtotalAmountEstimated': False, 'totalAmountEstimated': True},
                'lines': connection([{'__typename': 'CartLine', 'id': 'gid://shopify/CartLine/line-1',
                    'quantity': 2, 'merchandise': variant, 'cost': {'amountPerQuantity': money('1000.00'),
                        'subtotalAmount': money('2000.00'), 'totalAmount': money('2000.00')}}])}
            if 'mutation' in query:
                self.calls.append('create')
                assert body['variables'] == {'input': {'lines': [{'merchandiseId': VARIANT, 'quantity': 2}]}}
                result = {'cartCreate': {'userErrors': [], 'warnings': [], 'cart': cart}}
            else:
                self.calls.append('read')
                assert body['variables'] == {'id': PRIVATE['cart_id']}
                result = {'cart': cart}
        return ShopifyResponse(200, json.dumps({'data': result}).encode())


@pytest.fixture
def shop(running_hub, tmp_path, monkeypatch):
    from types import SimpleNamespace
    clients, restart = running_hub
    hub = clients['owner']
    merchant = hub.merchants()['merchants'][0]
    identity = hub.capabilities()
    provider = Merchant()
    monkeypatch.setattr('connectors.shopify._http_transport', provider)
    setup = configure_merchant_account(tmp_path / 'merchant', identity, merchant)
    account = MerchantAccount(setup['config_path'], identity, merchant)
    opened = []
    coordinator = CommerceCoordinator(hub, account, opener=lambda url: opened.append(url) or True)
    review = CommerceReview(hub, expected_principal='owner', merchant=merchant, coordinator=coordinator)
    tools = commerce_tools(hub, account, identity, can_execute=lambda: True)
    return SimpleNamespace(hub=hub, clients=clients, restart=restart, provider=provider, opened=opened,
                           review=review, tools=tools, coordinator=coordinator, merchant=merchant)


def propose(shop):
    shop.tools['search_store_catalog'].func('cotton shirt')
    return json.loads(shop.tools['propose_cart'].func(copy.deepcopy(SELECTION)))


def test_real_cart_voice_confirmation_checkout_once_and_restart(shop):
    from main import converse
    proposal = propose(shop)
    assert proposal['status'] == 'awaiting_approval'
    for role in ('other', 'child'):
        with pytest.raises(HubError):
            shop.clients[role].commerce_action(proposal['id'])
        with pytest.raises(HubError):
            shop.clients[role].commerce_checkout(proposal['id'])

    class Listener:
        texts = iter(['review cart', 'prepare reviewed cart', 'yes', 'review checkout',
                      'open reviewed checkout', 'yes', 'open reviewed checkout', None])

        def listen(self, **_):
            return next(self.texts)

    class Speaker:
        def __init__(self):
            self.said, self.confirmations = [], []

        def say(self, text, **_):
            self.said.append(text)

        def say_tracked(self, text, **_):
            self.confirmations.append(text)
            return 'completed'

    class NoModel:
        def handle(self, text):
            pytest.fail('Explicit shopping approval reached the language model')

    started = time.monotonic()
    speaker = Speaker()
    converse(NoModel(), Listener(), speaker, trusted_commands=TrustedCommands([shop.review]))
    saved = shop.hub.commerce_action(proposal['id'])
    assert saved['status'] == 'prepared' and saved['checkout_status'] == 'opened'
    assert saved['evidence']['subtotal_minor'] == 200000 and saved['evidence']['total_minor'] == 215000
    assert saved['checkout_evidence'] == {'payment_status': 'not_submitted'}
    assert shop.opened == [PRIVATE['checkout_url']]
    assert shop.provider.calls == ['catalog', 'quote', 'create', 'read', 'read', 'read', 'read']
    assert len(speaker.confirmations) == 2
    assert all('Cotton shirt' in prompt and 'Quantity: 2' in prompt and 'INR 1000.00' in prompt
               for prompt in speaker.confirmations)
    assert 'INR 2150.00' in speaker.confirmations[1]
    public = json.dumps([saved, shop.hub.commerce(), shop.tools['list_carts'].func(), speaker.said, speaker.confirmations])
    assert all(secret not in public for secret in ('privateCartKey', 'privateCheckoutKey', 'checkout_url', 'cart_id'))
    shop.restart()
    assert shop.hub.commerce_action(proposal['id']) == saved
    with pytest.raises(HubError):
        shop.hub.claim_commerce_checkout(proposal['id'], saved['argument_hash'])
    assert len(shop.opened) == 1
    print(json.dumps({'acceptance': 'real-rust-shopify-reviewed-checkout', 'elapsed_seconds': round(time.monotonic()-started, 2),
        'provider': 'synthetic HTTP transport', 'native_private_account': True, 'actual_browser_opened': False,
        'cart_attempts': 1, 'synthetic_checkout_opens': 1, 'payment_submitted': False, 'restart_persisted': True}))


@pytest.mark.parametrize('change', ['price', 'merchant'])
def test_real_stale_selection_or_merchant_change_prevents_cart(shop, tmp_path, change):
    proposal = propose(shop)
    handle = lambda text: shop.review.handle(text, confirm=lambda _: True, still_current=lambda: True)
    assert 'Cotton shirt' in handle('review cart')
    if change == 'price':
        shop.provider.price = '1100.00'
    else:
        path = tmp_path / 'hub.json'
        config = json.loads(path.read_text())
        config['commerce_merchants'][0]['display_name'] = 'Reconfigured shop'
        path.write_text(json.dumps(config))
        shop.restart()
    assert 'could not be confirmed' in handle('prepare reviewed cart')
    assert 'create' not in shop.provider.calls and shop.opened == []
    assert shop.hub.commerce_action(proposal['id'])['status'] == 'awaiting_approval'


def test_real_interrupted_cart_claim_recovers_unknown_without_replay(running_hub):
    clients, restart = running_hub
    hub = clients['owner']
    merchant = hub.merchants()['merchants'][0]
    row = hub.propose_commerce(merchant['id'], merchant['version'],
        [{'variant_id': VARIANT, 'title': 'Synthetic shirt', 'quantity': 1, 'unit_price_minor': 100000}],
        'INR', int(time.time()), 'interrupted-cart')
    hub.decide_commerce(row['id'], 'approve', row['argument_hash'])
    assert hub.claim_commerce(row['id'])['status'] == 'executing'
    restart()
    assert hub.commerce_action(row['id'])['status'] == 'unknown'
    with pytest.raises(HubError):
        hub.claim_commerce(row['id'])
