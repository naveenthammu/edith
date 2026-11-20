from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace

import pytest

from connectors.commerce_coordinator import CommerceCoordinator, CommerceError, CommerceOutcomeUnknown, validate_commerce, validate_checkout_receipt
from connectors.shopify import ShopifyStorefront


NOW = 1_800_000_000
VARIANT = "gid://shopify/ProductVariant/101"
SHOP = "fixture-shop.myshopify.com"
SELECTED = [{"variant_id": VARIANT, "quantity": 2}]
QUOTE_LINES = [{"variant_id": VARIANT, "title": "Cotton shirt — Blue / M", "quantity": 2, "unit_price_minor": 1234}]
MERCHANT = {"id": "clothes", "version": "a" * 64, "display_name": "Clothes Store", "shop_domain": SHOP, "checkout_hosts": [SHOP]}
CHECKOUT = {"cart_id": "gid://shopify/Cart/fixture-cart?key=private-cart-key", "checkout_url": f"https://{SHOP}/cart/c/checkout-token?key=private-checkout-key"}


def hash_payload(payload):
    return hashlib.sha256(json.dumps({"kind": "commerce.prepare_cart", "payload": payload}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def action():
    payload = {"merchant_id": "clothes", "merchant_version": "a" * 64, "merchant_name": "Clothes Store",
               "shop_domain": SHOP, "lines": deepcopy(QUOTE_LINES), "currency": "INR", "observed_at": NOW,
               "quoted_subtotal_minor": 2468}
    return {"id": "action-1", "kind": "commerce.prepare_cart", "payload": payload, "argument_hash": hash_payload(payload),
            "created_at": NOW, "expires_at": NOW + 300, "status": "awaiting_approval", "evidence": None,
            "recovery_note": None, "checkout_status": "not_opened", "checkout_evidence": None}


def evidence():
    return {"provider": "shopify", "payment_status": "not_submitted", "currency": "INR", "subtotal_minor": 2468,
            "total_minor": 2700, "lines": deepcopy(QUOTE_LINES)}


def cart_result():
    return {"status": "prepared", "provider": "shopify", "payment_status": "not_submitted", **CHECKOUT,
            "estimated_subtotal_minor": 2468, "estimated_total_minor": 2700, "currency": "INR",
            "lines": [{"variant_id": VARIANT, "quantity": 2, "title": "Cotton shirt", "variant_title": "Blue / M",
                       "available_for_sale": True, "unit_price_minor": 1234, "subtotal_minor": 2468,
                       "total_minor": 2468, "currency": "INR"}]}


class Hub:
    def __init__(self):
        self.row = action()
        self.events = []
        self.hooks = {}
        self.checkout = deepcopy(CHECKOUT)
    def event(self, name):
        self.events.append(name)
        if name in self.hooks: self.hooks[name]()
    def merchants(self):
        self.event("merchants")
        return {"merchants": [deepcopy(MERCHANT)]}
    def propose_commerce(self, merchant_id, merchant_version, lines, currency, observed_at, idempotency_key):
        self.event("propose")
        assert (merchant_id, merchant_version, lines, currency, observed_at, idempotency_key) == ("clothes", "a" * 64, QUOTE_LINES, "INR", NOW, "request-1")
        return deepcopy(self.row)
    def commerce_action(self, identifier):
        self.event("read")
        assert identifier == "action-1"
        return deepcopy(self.row)
    def decide_commerce(self, identifier, decision, digest):
        self.event("decide")
        assert identifier == "action-1" and digest == self.row["argument_hash"]
        self.row["status"] = "approved" if decision == "approve" else "rejected"
        return deepcopy(self.row)
    def claim_commerce(self, identifier):
        self.event("claim")
        if self.row["status"] != "approved": raise RuntimeError("already claimed")
        self.row["status"] = "executing"
        return deepcopy(self.row)
    def complete_commerce(self, identifier, status, observed, checkout):
        self.event("complete")
        self.row.update(status=status, evidence=deepcopy(observed))
        self.checkout = deepcopy(checkout)
        return deepcopy(self.row)
    def commerce_checkout(self, identifier):
        self.event("checkout-read")
        return {"action": deepcopy(self.row), "checkout": deepcopy(self.checkout)}
    def claim_commerce_checkout(self, identifier, digest):
        self.event("checkout-claim")
        if self.row["checkout_status"] != "not_opened": raise RuntimeError("already opened")
        self.row["checkout_status"] = "executing"
        return {"action": deepcopy(self.row), "checkout": deepcopy(self.checkout)}
    def complete_commerce_checkout(self, identifier, status, observed):
        self.event("checkout-complete")
        self.row.update(checkout_status=status, checkout_evidence=deepcopy(observed))
        return deepcopy(self.row)


class Account:
    def __init__(self, events):
        self.events = events
        self.merchant = deepcopy(MERCHANT)
        self.result = cart_result()
        self.quote_result = {"provider": "shopify", "shop_domain": SHOP, "lines": deepcopy(QUOTE_LINES), "currency": "INR", "quoted_subtotal_minor": 2468,
                             "observed_at": datetime.fromtimestamp(NOW, timezone.utc).isoformat().replace("+00:00", "Z")}
        self.storefront = self
        self.hooks = {}
    def revalidate(self, selected):
        assert selected == MERCHANT
        self.events.append("account")
        return deepcopy(selected)
    def quote(self, lines, *, can_execute):
        assert lines == SELECTED
        assert can_execute() is True
        self.events.append("quote")
        if "quote" in self.hooks: self.hooks["quote"]()
        return deepcopy(self.quote_result)
    def create_cart(self, lines, *, can_execute):
        assert lines == SELECTED
        assert can_execute() is True
        self.events.append("cart-create")
        if "create" in self.hooks: self.hooks["create"]()
        return deepcopy(self.result)
    def read_cart(self, identifier, lines, *, can_execute):
        assert identifier == CHECKOUT["cart_id"] and lines == SELECTED and can_execute() is True
        self.events.append("cart-read")
        if "read" in self.hooks: self.hooks["read"]()
        return deepcopy(self.result)
    def validate_checkout_url(self, value):
        return ShopifyStorefront(SHOP).validate_checkout_url(value)


@pytest.fixture
def setup():
    hub = Hub()
    account = Account(hub.events)
    opened = []
    coordinator = CommerceCoordinator(hub, account, opener=lambda url: opened.append(url) or True, clock=lambda: NOW)
    return SimpleNamespace(hub=hub, account=account, opened=opened, coordinator=coordinator)


def decide(setup, approve=True, gate=lambda: True):
    return setup.coordinator.decide_and_prepare("action-1", setup.hub.row["argument_hash"], approve, can_execute=gate)


def prepared(setup):
    setup.hub.row.update(status="prepared", evidence=evidence())


def test_prepare_uses_fresh_exact_variant_quote_and_returns_public_row_only(setup):
    result = setup.coordinator.prepare(SELECTED, "request-1")
    assert result == action() and setup.hub.events.index("quote") < setup.hub.events.index("propose")
    assert "private-cart-key" not in str(result) and "cart-create" not in setup.hub.events


def test_reject_makes_no_merchant_quote_cart_or_browser_call(setup):
    result = decide(setup, False)
    assert result["status"] == "rejected"
    assert not any(event in setup.hub.events for event in ("quote", "cart-create", "claim", "merchants"))
    assert setup.opened == []


def test_approval_requotes_then_claims_once_and_persists_separate_private_checkout(setup):
    result = decide(setup)
    assert result["status"] == "prepared" and result["evidence"] == evidence()
    assert setup.hub.checkout == CHECKOUT and setup.opened == []
    assert setup.hub.events.index("quote") < setup.hub.events.index("decide") < setup.hub.events.index("claim") < setup.hub.events.index("cart-create")
    assert setup.hub.events.count("cart-create") == 1 and "private-cart-key" not in str(result)


@pytest.mark.parametrize("change", ["price", "currency", "title", "variant"])
def test_changed_quote_requires_new_review_before_decision_or_cart(setup, change):
    if change == "currency": setup.account.quote_result["currency"] = "USD"
    elif change == "price": setup.account.quote_result["lines"][0]["unit_price_minor"] = 1300
    elif change == "title": setup.account.quote_result["lines"][0]["title"] = "Changed item"
    else: setup.account.quote_result["lines"][0]["variant_id"] = "gid://shopify/ProductVariant/999"
    with pytest.raises(CommerceError): decide(setup)
    assert "decide" not in setup.hub.events and "cart-create" not in setup.hub.events


@pytest.mark.parametrize("where", ["quote", "decide", "claim"])
def test_cancellation_after_blocking_calls_never_creates_cart(setup, where):
    current = True
    def cancel():
        nonlocal current
        current = False
    (setup.account.hooks if where == "quote" else setup.hub.hooks)[where] = cancel
    if where == "claim":
        assert decide(setup, gate=lambda: current)["status"] == "failed"
    else:
        with pytest.raises(CommerceError): decide(setup, gate=lambda: current)
    assert "cart-create" not in setup.hub.events and setup.opened == []


def test_unknown_cart_is_never_retried_and_never_exposes_checkout(setup):
    setup.account.result = {"status": "unknown", "provider": "shopify", "payment_status": "not_submitted"}
    result = decide(setup)
    assert result["status"] == "unknown" and result["evidence"]["lines"] == []
    assert setup.hub.checkout is None and setup.hub.events.count("cart-create") == 1


@pytest.mark.parametrize("change", ["price", "currency", "quantity", "url", "unavailable"])
def test_changed_or_unsafe_cart_receipt_is_unknown_not_checkout_ready(setup, change):
    if change == "price": setup.account.result["lines"][0]["unit_price_minor"] = 1300
    elif change == "currency": setup.account.result["currency"] = "USD"
    elif change == "quantity": setup.account.result["lines"][0]["quantity"] = 1
    elif change == "url": setup.account.result["checkout_url"] = "https://evil.invalid/cart/c/x"
    else: setup.account.result["lines"][0]["available_for_sale"] = False
    result = decide(setup)
    assert result["status"] == "unknown" and setup.hub.checkout is None and setup.opened == []


def test_result_persistence_failure_after_cart_is_explicit_unknown_without_replay(setup):
    setup.hub.hooks["complete"] = lambda: (_ for _ in ()).throw(RuntimeError("private-cart-key"))
    with pytest.raises(CommerceOutcomeUnknown) as error: decide(setup)
    assert error.value.status == "unknown" and "private-cart-key" not in str(error.value)
    assert setup.hub.events.count("cart-create") == 1


@pytest.mark.parametrize("field,value", [("argument_hash", "b" * 64), ("kind", "home.power"), ("id", "different")])
def test_forged_claim_cannot_reach_shopify(setup, field, value):
    original = setup.hub.claim_commerce
    def forged(identifier):
        result = original(identifier)
        result[field] = value
        return result
    setup.hub.claim_commerce = forged
    with pytest.raises(CommerceError): decide(setup)
    assert "cart-create" not in setup.hub.events


def test_duplicate_claim_failure_never_creates_cart(setup):
    setup.hub.hooks["claim"] = lambda: (_ for _ in ()).throw(RuntimeError("claimed"))
    with pytest.raises(CommerceError): decide(setup)
    assert "cart-create" not in setup.hub.events


def test_checkout_review_freshly_reads_exact_cart_without_opening(setup):
    prepared(setup)
    wrapper = setup.coordinator.checkout_for_review("action-1", can_execute=lambda: True)
    assert wrapper["checkout"] == CHECKOUT and setup.hub.events.count("cart-read") == 1
    assert setup.opened == [] and "checkout-claim" not in setup.hub.events


def test_checkout_open_rechecks_receipt_then_claims_once_and_never_submits_payment(setup):
    prepared(setup)
    result = setup.coordinator.open_checkout("action-1", setup.hub.row["argument_hash"], can_execute=lambda: True)
    assert result["checkout_status"] == "opened" and result["checkout_evidence"] == {"payment_status": "not_submitted"}
    assert setup.opened == [CHECKOUT["checkout_url"]] and setup.hub.events.index("cart-read") < setup.hub.events.index("checkout-claim")
    assert "private-checkout-key" not in str(result)


def test_changed_total_before_open_requires_new_review_and_never_claims_or_opens(setup):
    prepared(setup)
    setup.account.result["estimated_total_minor"] = 2800
    with pytest.raises(CommerceError):
        setup.coordinator.open_checkout("action-1", setup.hub.row["argument_hash"], can_execute=lambda: True)
    assert setup.opened == [] and "checkout-claim" not in setup.hub.events


def test_cancel_during_checkout_claim_records_no_browser_attempt(setup):
    prepared(setup)
    current = True
    def cancel():
        nonlocal current
        current = False
    setup.hub.hooks["checkout-claim"] = cancel
    result = setup.coordinator.open_checkout("action-1", setup.hub.row["argument_hash"], can_execute=lambda: current)
    assert result["checkout_status"] == "failed" and setup.opened == []


@pytest.mark.parametrize("mode", ["false", "exception", "persistence"])
def test_ambiguous_browser_or_journal_result_never_reopens(mode, setup):
    prepared(setup)
    calls = []
    def opener(url):
        calls.append(url)
        if mode == "exception": raise RuntimeError("private-checkout-key")
        return mode == "persistence"
    setup.coordinator._opener = opener
    if mode == "persistence":
        setup.hub.hooks["checkout-complete"] = lambda: (_ for _ in ()).throw(RuntimeError("secret"))
        with pytest.raises(CommerceOutcomeUnknown):
            setup.coordinator.open_checkout("action-1", setup.hub.row["argument_hash"], can_execute=lambda: True)
    else:
        result = setup.coordinator.open_checkout("action-1", setup.hub.row["argument_hash"], can_execute=lambda: True)
        assert result["checkout_status"] == "unknown"
    assert len(calls) == 1


def test_default_deny_and_nonboolean_approval_have_no_side_effects(setup):
    with pytest.raises(CommerceError):
        setup.coordinator.decide_and_prepare("action-1", setup.hub.row["argument_hash"], True)
    with pytest.raises(CommerceError): decide(setup, 1)
    assert setup.hub.events == []


def test_public_validation_rejects_secret_fields_and_changed_hash():
    value = action()
    assert validate_commerce(value) == value
    value["checkout_url"] = CHECKOUT["checkout_url"]
    with pytest.raises(CommerceError): validate_commerce(value)
    value = action()
    value["payload"]["lines"][0]["unit_price_minor"] += 1
    with pytest.raises(CommerceError): validate_commerce(value)


def test_private_wrapper_validation_requires_prepared_row():
    with pytest.raises(CommerceError):
        validate_checkout_receipt({"action": action(), "checkout": CHECKOUT})


def test_prepared_evidence_boolean_quantity_cannot_equal_integer_one():
    value = action()
    value["payload"]["lines"][0]["quantity"] = 1
    value["payload"]["quoted_subtotal_minor"] = 1234
    value["argument_hash"] = hash_payload(value["payload"])
    value.update(status="prepared", evidence=evidence())
    value["evidence"]["subtotal_minor"] = 1234
    value["evidence"]["lines"][0]["quantity"] = True
    with pytest.raises(CommerceError): validate_commerce(value)


def test_actual_shopify_adapter_runs_quote_cart_and_fresh_checkout_read_without_network(setup):
    from connectors.shopify import ShopifyResponse
    calls = []
    cart = {"id": CHECKOUT["cart_id"], "checkoutUrl": CHECKOUT["checkout_url"], "totalQuantity": 2,
            "cost": {"subtotalAmount": {"amount": "24.68", "currencyCode": "INR"}, "totalAmount": {"amount": "27.00", "currencyCode": "INR"},
                     "subtotalAmountEstimated": False, "totalAmountEstimated": True},
            "lines": {"pageInfo": {"hasNextPage": False}, "nodes": [{"__typename": "CartLine", "id": "gid://shopify/CartLine/line-1", "quantity": 2,
                "merchandise": {"__typename": "ProductVariant", "id": VARIANT, "title": "Blue / M", "availableForSale": True,
                                "product": {"id": "gid://shopify/Product/10", "title": "Cotton shirt"}},
                "cost": {"amountPerQuantity": {"amount": "12.34", "currencyCode": "INR"}, "subtotalAmount": {"amount": "24.68", "currencyCode": "INR"},
                         "totalAmount": {"amount": "24.68", "currencyCode": "INR"}}}]}}
    def transport(request, **_):
        query = json.loads(request.data)["query"]
        if "EdithExactVariants" in query:
            calls.append("quote")
            value = {"nodes": [{"__typename": "ProductVariant", "id": VARIANT, "title": "Blue / M", "availableForSale": True,
                      "price": {"amount": "12.34", "currencyCode": "INR"}, "product": {"id": "gid://shopify/Product/10", "title": "Cotton shirt"}}]}
        elif "mutation" in query:
            calls.append("create")
            value = {"cartCreate": {"userErrors": [], "warnings": [], "cart": cart}}
        else:
            calls.append("read")
            value = {"cart": cart}
        return ShopifyResponse(200, json.dumps({"data": value}).encode())
    setup.account.storefront = ShopifyStorefront(SHOP, transport=transport, clock=lambda: NOW)
    assert decide(setup)["status"] == "prepared"
    result = setup.coordinator.open_checkout("action-1", setup.hub.row["argument_hash"], can_execute=lambda: True)
    assert result["checkout_status"] == "opened" and calls == ["quote", "create", "read"]
    assert len(setup.opened) == 1 and "private-cart-key" not in str(result)


def test_freshly_confirmed_approved_unclaimed_action_requotes_and_claims_without_redeciding(setup):
    setup.hub.row["status"] = "approved"
    result = decide(setup)
    assert result["status"] == "prepared" and "decide" not in setup.hub.events
    assert setup.hub.events.index("quote") < setup.hub.events.index("claim") < setup.hub.events.index("cart-create")
    with pytest.raises(CommerceError): decide(setup)
    assert setup.hub.events.count("cart-create") == 1


@pytest.mark.parametrize("key,accepted", [("base64+key==", True), ("key.with-dash_and.period", True), ("tilde~key", False)])
def test_private_checkout_key_grammar_matches_the_rust_journal(key, accepted):
    row = action()
    row.update(status="prepared", evidence=evidence())
    wrapper = {"action": row, "checkout": {**CHECKOUT, "checkout_url": f"https://{SHOP}/cart/c/checkout-token?key={key}"}}
    if accepted:
        assert validate_checkout_receipt(wrapper) == wrapper
    else:
        with pytest.raises(CommerceError): validate_checkout_receipt(wrapper)


def test_coordinator_accepts_the_full_server_limit_of_128_merchants(setup):
    setup.hub.merchants = lambda: {"merchants": [deepcopy(MERCHANT), *[{**MERCHANT, "id": f"other-{index}"} for index in range(127)]]}
    assert decide(setup)["status"] == "prepared"
