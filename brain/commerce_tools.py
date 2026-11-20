"""Catalog reads and local proposals; model tools never create carts or open checkout."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import logging
import os
import re
import time

from skills import Skill, _schema
from brain.hub import HubError
from connectors.commerce_coordinator import CommerceError, validate_commerce


class CommerceToolError(ValueError):
    """A bounded, safe configuration or selection error."""


def configured_merchant_account(hub, identity):
    """Load a selected protected store for an allowed member, without provider I/O."""
    if (identity is None or identity.get('role') not in {'adult', 'elder'}
            or identity.get('capabilities', {}).get('commerce_proposals') is not True
            or not os.getenv('EDITH_MERCHANT_CONFIG')):
        return None
    from connectors.merchant_account import MerchantAccountError, merchant_from_env
    try:
        return merchant_from_env(identity, hub.merchants())
    except (MerchantAccountError, HubError):
        logging.getLogger(__name__).warning('Shopping disabled: member store configuration could not be verified.')
        return None


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _summary(row):
    try:
        row = validate_commerce(row)
    except CommerceError:
        raise CommerceToolError('The hub returned an invalid cart proposal.') from None
    payload = row['payload']
    # Deliberate allowlist: private checkout URLs/cart keys and raw provider notes
    # can never be copied into model context, even if a response contains them.
    result = {key: deepcopy(payload[key]) for key in ('merchant_name', 'lines', 'currency', 'quoted_subtotal_minor')}
    result.update(id=row['id'], status=row['status'], expires_at=row['expires_at'],
                  checkout_status=row.get('checkout_status', 'not_opened'), payment_status='not_submitted')
    return result


def commerce_tools(hub, account, identity, *, can_execute, clock=time.time):
    if (identity.get('role') not in {'adult', 'elder'} or not identity.get('principal_id')
            or not callable(can_execute)):
        raise CommerceToolError('A current paired adult or elder session is required.')
    identity = deepcopy(identity)
    merchant = deepcopy(account.merchant)
    cache = {}

    def permitted():
        try:
            if can_execute() is not True:
                return False
            current = hub.capabilities()
            if (current.get('principal_id') != identity['principal_id'] or current.get('role') != identity['role']
                    or current.get('capabilities', {}).get('commerce_proposals') is not True):
                return False
            matches = [row for row in hub.merchants()['merchants'] if row.get('id') == merchant['id']]
            if len(matches) != 1 or matches[0] != merchant:
                return False
            account.revalidate(matches[0])
            current = hub.capabilities()
            return (current.get('principal_id') == identity['principal_id'] and current.get('role') == identity['role']
                    and current.get('capabilities', {}).get('commerce_proposals') is True and can_execute() is True)
        except Exception:
            return False

    def require_current():
        if not permitted():
            cache.clear()
            raise CommerceToolError('Store access changed or this request was cancelled. Reconnect the selected store before continuing.')

    def search_store_catalog(query: str, max_products: int = 5) -> str:
        """Search the configured Shopify store. Returns current variants/prices, not reserved stock. Use only the user's shopping query; never send private notes to a merchant."""
        require_current()
        result = deepcopy(account.storefront.catalog(query, limit=max_products, can_execute=permitted))
        require_current()
        try:
            observed = datetime.fromisoformat(result['observed_at'].replace('Z', '+00:00'))
            if observed.tzinfo is None or observed.utcoffset() is None:
                raise ValueError()
            observed_at = int(observed.timestamp())
            if not clock() - 300 <= observed_at <= clock() + 30 or result['shop_domain'] != merchant['shop_domain']:
                raise ValueError()
            products = result['products']
            if not isinstance(products, list) or len(products) > 10:
                raise ValueError()
            pending = {}
            for product in products:
                if not isinstance(product['variants'], list) or len(product['variants']) > 5:
                    raise ValueError()
                for variant in product['variants']:
                    title = product['title'] + ' — ' + variant['title']
                    identifier = variant['variant_id']
                    if (not isinstance(identifier, str) or re.fullmatch(r'gid://shopify/ProductVariant/[1-9][0-9]{0,19}', identifier) is None
                            or len(title) > 300 or not title.isprintable()
                            or type(variant['available_for_sale']) is not bool
                            or type(variant['price_minor']) is not int or not 0 <= variant['price_minor'] <= 99_999_999_999_999
                            or variant['currency'] not in {'INR', 'USD', 'EUR', 'GBP'} or identifier in pending):
                        raise ValueError()
                    pending[identifier] = {'variant_id': identifier, 'title': title,
                                          'unit_price_minor': variant['price_minor'], 'currency': variant['currency'],
                                          'available': variant['available_for_sale'], 'observed_at': observed_at}
        except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
            raise CommerceToolError('The catalog could not be bound to a recent exact store observation.') from None
        for identifier in list(cache):
            if cache[identifier]['observed_at'] <= clock() - 300:
                del cache[identifier]
        cache.update(pending)
        while len(cache) > 200:
            del cache[next(iter(cache))]
        result['truncated'] = False
        while len(_json(result)) > 12_000 and result['products']:
            result['products'].pop()
            result['truncated'] = True
        if len(_json(result)) > 12_000:
            raise CommerceToolError('The catalog metadata exceeds the display limit.')
        return _json(result)

    def propose_cart(items: list) -> str:
        """Save a local cart proposal from recently observed variant IDs and quantities. Never creates a merchant cart or pays. Search again for stale/missing variants; user reviews and confirms through trusted commands."""
        require_current()
        if not isinstance(items, list) or not 1 <= len(items) <= 10:
            raise CommerceToolError('Select one to ten exact variants from the recent catalog.')
        lines, seen, currencies, observations = [], set(), set(), []
        for item in items:
            if not isinstance(item, dict) or set(item) != {'variant_id', 'quantity'}:
                raise CommerceToolError('Choose only an observed variant ID and quantity; prices come from the merchant.')
            identifier, quantity = item['variant_id'], item['quantity']
            if (not isinstance(identifier, str) or identifier in seen or type(quantity) is not int
                    or not 1 <= quantity <= 10):
                raise CommerceToolError('Use distinct variants with quantities from one to ten.')
            source = cache.get(identifier)
            if source is None or not source['available'] or source['observed_at'] <= clock() - 300:
                raise CommerceToolError('This variant is missing, unavailable or stale. Search the catalog again.')
            seen.add(identifier)
            lines.append({'variant_id': identifier, 'title': source['title'], 'quantity': quantity,
                          'unit_price_minor': source['unit_price_minor']})
            currencies.add(source['currency'])
            observations.append(source['observed_at'])
        if len(currencies) != 1 or sum(line['quantity'] for line in lines) > 50:
            raise CommerceToolError('Use one currency and at most fifty total items.')
        currency, observed_at = next(iter(currencies)), min(observations)
        payload = {'merchant_id': merchant['id'], 'merchant_version': merchant['version'],
                   'merchant_name': merchant['display_name'], 'shop_domain': merchant['shop_domain'],
                   'lines': lines, 'currency': currency, 'observed_at': observed_at,
                   'quoted_subtotal_minor': sum(line['quantity'] * line['unit_price_minor'] for line in lines)}
        key = hashlib.sha256(_json(payload).encode('utf-8')).hexdigest()
        require_current()
        row = hub.propose_commerce(merchant['id'], merchant['version'], lines, currency, observed_at, key)
        require_current()
        if row.get('payload') != payload:
            raise CommerceToolError('The saved proposal did not match the selected items and estimate.')
        result = _summary(row)
        result['next_step'] = ('Say review checkout to inspect this prepared cart.' if row['status'] == 'prepared' else
                               'Say review cart, then prepare reviewed cart for a fresh exact-selection confirmation.')
        result['notice'] = 'This is a local proposal, not an order. Shipping, taxes and final cost are confirmed at merchant checkout.'
        return _json(result)

    def list_carts(status: str = 'active') -> str:
        """List owned cart proposal summaries. prepared/opened is not a paid order; reconcile unknown attempts instead of retrying."""
        if status not in {'active', 'all'}:
            raise CommerceToolError('Choose active or all cart proposals.')
        require_current()
        rows = hub.commerce()['commerce']
        require_current()
        if not isinstance(rows, list) or len(rows) > 100:
            raise CommerceToolError('The cart list exceeded its bound.')
        selected = [_summary(row) for row in rows
                    if status == 'all' or row.get('status') in {'awaiting_approval', 'approved', 'executing', 'prepared', 'unknown'}]
        result = {'carts': selected[:20], 'matches': len(selected), 'truncated': len(selected) > 20}
        while len(_json(result)) > 12_000 and result['carts']:
            result['carts'].pop()
            result['truncated'] = True
        return _json(result)

    proposal_schema = {'type': 'function', 'function': {'name': 'propose_cart', 'description': propose_cart.__doc__,
        'parameters': {'type': 'object', 'additionalProperties': False, 'required': ['items'],
            'properties': {'items': {'type': 'array', 'minItems': 1, 'maxItems': 10,
                'items': {'type': 'object', 'additionalProperties': False, 'required': ['variant_id', 'quantity'],
                          'properties': {'variant_id': {'type': 'string'},
                                         'quantity': {'type': 'integer', 'minimum': 1, 'maximum': 10}}}}}}}}
    return {'search_store_catalog': Skill(search_store_catalog, False, _schema(search_store_catalog), local_only=False),
            'propose_cart': Skill(propose_cart, False, proposal_schema, local_only=True),
            'list_carts': Skill(list_carts, False, _schema(list_carts), local_only=True)}
