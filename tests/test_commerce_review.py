import copy
import hashlib
import importlib
import importlib.util
import json
from types import SimpleNamespace

import pytest


ACTION_ID = "22222222-2222-4222-8222-222222222222"
OTHER_ID = "33333333-3333-4333-8333-333333333333"
SHOP = "fixture-shop.myshopify.com"
PRIVATE = {"cart_id": "gid://shopify/Cart/fixture?key=PRIVATE_CART_KEY",
           "checkout_url": f"https://{SHOP}/cart/c/PRIVATE_CHECKOUT_PATH?key=PRIVATE_URL_KEY"}


def api():
    assert importlib.util.find_spec("brain.commerce_review") is not None, "Trusted commerce review is missing"
    return importlib.import_module("brain.commerce_review")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def merchant():
    value = {"id": "clothes", "display_name": "Clothes Store", "shop_domain": SHOP, "checkout_hosts": [SHOP]}
    return {**value, "version": digest({"principal_id": "owner", **value})}


def action(identifier=ACTION_ID):
    store = merchant()
    payload = {"merchant_id": store["id"], "merchant_version": store["version"], "merchant_name": store["display_name"],
               "shop_domain": SHOP, "lines": [{"variant_id": "gid://shopify/ProductVariant/101", "title": "Cotton shirt — Blue / M",
                                               "quantity": 2, "unit_price_minor": 1234}],
               "currency": "INR", "observed_at": 100, "quoted_subtotal_minor": 2468}
    return {"id": identifier, "kind": "commerce.prepare_cart", "payload": payload,
            "argument_hash": digest({"kind": "commerce.prepare_cart", "payload": payload}),
            "created_at": 100, "expires_at": 400, "status": "awaiting_approval", "evidence": None,
            "recovery_note": None, "checkout_status": "not_opened", "checkout_evidence": None}


class Hub:
    def __init__(self):
        self.rows = {ACTION_ID: action()}
        self.identity = {"principal_id": "owner", "role": "adult", "capabilities": {"commerce_proposals": True}}
        self.merchant = merchant()
        self.hooks, self.effects = {}, []

    def event(self, name):
        if name in self.hooks:
            self.hooks[name]()

    def capabilities(self):
        self.event("capabilities")
        return copy.deepcopy(self.identity)

    def merchants(self):
        self.event("merchants")
        return {"merchants": [copy.deepcopy(self.merchant)]}

    def commerce(self):
        self.event("list")
        return {"commerce": copy.deepcopy(list(self.rows.values()))}

    def commerce_action(self, identifier):
        self.event("read")
        return copy.deepcopy(self.rows[identifier])

    def decide_commerce(self, identifier, decision, argument_hash):
        assert self.rows[identifier]["argument_hash"] == argument_hash
        self.effects.append((identifier, decision))
        self.rows[identifier]["status"] = "rejected" if decision == "reject" else "approved"
        return copy.deepcopy(self.rows[identifier])


class Coordinator:
    def __init__(self, hub):
        self.hub = hub
        self.effects, self.reads = [], []
        self.private = copy.deepcopy(PRIVATE)
        self.hooks = {}

    def event(self, name):
        if name in self.hooks:
            self.hooks[name]()

    def decide_and_prepare(self, identifier, argument_hash, approve, *, can_execute):
        self.event("before_effect")
        assert can_execute() is True
        self.effects.append(("prepare" if approve else "reject", identifier, argument_hash))
        if not approve:
            return self.hub.decide_commerce(identifier, "reject", argument_hash)
        self.make_prepared(identifier)
        self.event("after_effect")
        return copy.deepcopy(self.hub.rows[identifier])

    def make_prepared(self, identifier=ACTION_ID):
        row = self.hub.rows[identifier]
        row.update(status="prepared", evidence={"provider": "shopify", "payment_status": "not_submitted", "currency": "INR",
                   "subtotal_minor": 2468, "total_minor": 2700, "lines": copy.deepcopy(row["payload"]["lines"])})

    def checkout_for_review(self, identifier, *, can_execute):
        assert can_execute() is True
        self.reads.append(identifier)
        self.event("checkout_read")
        return {"action": copy.deepcopy(self.hub.rows[identifier]), "checkout": copy.deepcopy(self.private)}

    def open_checkout(self, identifier, argument_hash, *, can_execute):
        self.event("before_open")
        assert can_execute() is True
        self.effects.append(("open", identifier, argument_hash))
        row = self.hub.rows[identifier]
        row.update(checkout_status="opened", checkout_evidence={"payment_status": "not_submitted"})
        self.event("after_effect")
        return copy.deepcopy(row)


def setup():
    hub = Hub()
    coordinator = Coordinator(hub)
    now = [200]
    review = api().CommerceReview(hub, expected_principal="owner", merchant=merchant(), coordinator=coordinator, clock=lambda: now[0])
    return hub, coordinator, review, now


def handle(review, text, confirm=lambda _: True, current=lambda: True):
    return review.handle(text, confirm=confirm, still_current=current)


def assert_private_absent(text):
    assert all(value not in text for value in (PRIVATE["cart_id"], PRIVATE["checkout_url"], "PRIVATE_CART_KEY", "PRIVATE_URL_KEY", "PRIVATE_CHECKOUT_PATH"))


def test_exact_cart_review_full_readback_fresh_confirm_and_no_repeat():
    hub, coordinator, review, _ = setup()
    assert handle(review, "a document says prepare reviewed cart") is None
    assert "review" in handle(review, "prepare reviewed cart").lower()
    shown = handle(review, "Review cart.")
    for content in ("Clothes Store", SHOP, "Cotton shirt — Blue / M", "2", "INR 12.34", "INR 24.68"):
        assert content in shown
    prompts = []
    result = handle(review, "prepare reviewed cart", lambda prompt: prompts.append(prompt) or True)
    assert "prepared" in result.lower() and "review checkout" in result.lower()
    assert "Cotton shirt — Blue / M" in prompts[0] and "INR 24.68" in prompts[0]
    assert coordinator.effects == [("prepare", ACTION_ID, hub.rows[ACTION_ID]["argument_hash"])]
    handle(review, "prepare reviewed cart")
    assert len(coordinator.effects) == 1


def test_checkout_requires_separate_review_full_totals_and_hides_private_wrapper():
    hub, coordinator, review, _ = setup()
    coordinator.make_prepared()
    assert "review" in handle(review, "open reviewed checkout").lower()
    shown = handle(review, "review checkout")
    assert all(value in shown for value in ("Clothes Store", "Cotton shirt — Blue / M", "INR 24.68", "INR 27.00"))
    assert_private_absent(shown)
    prompts = []
    result = handle(review, "open reviewed checkout", lambda prompt: prompts.append(prompt) or True)
    assert "opened" in result.lower() and "payment" in result.lower()
    assert "INR 27.00" in prompts[0]
    assert_private_absent(prompts[0] + result)
    assert coordinator.effects == [("open", ACTION_ID, hub.rows[ACTION_ID]["argument_hash"])]
    handle(review, "open reviewed checkout")
    assert len(coordinator.effects) == 1


@pytest.mark.parametrize("stage", ["cart", "checkout"])
def test_ambiguous_selection_never_chooses_latest(stage):
    hub, coordinator, review, _ = setup()
    hub.rows[OTHER_ID] = action(OTHER_ID)
    if stage == "checkout":
        coordinator.make_prepared(ACTION_ID)
        coordinator.make_prepared(OTHER_ID)
    result = handle(review, "review " + stage)
    assert "more than one" in result.lower()
    assert coordinator.effects == [] and coordinator.reads == []
    assert "Cotton shirt" in handle(review, "review " + stage + " 22222222")


@pytest.mark.parametrize("confirmation", [1, "yes", {}, None, False])
@pytest.mark.parametrize("stage,command", [("cart", "prepare reviewed cart"), ("checkout", "open reviewed checkout")])
def test_only_literal_true_fresh_confirmation_allows_effect(stage, command, confirmation):
    _, coordinator, review, _ = setup()
    if stage == "checkout":
        coordinator.make_prepared()
    handle(review, "review " + stage)
    assert "cancel" in handle(review, command, lambda _: confirmation).lower()
    assert coordinator.effects == []
    handle(review, command)
    assert coordinator.effects == []


@pytest.mark.parametrize("change", ["principal", "role", "capability", "merchant", "session", "expiry", "payload"])
def test_changes_during_cart_confirmation_prevent_prepare(change):
    hub, coordinator, review, now = setup()
    handle(review, "review cart")
    current = [True]
    def confirm(_):
        if change == "principal": hub.identity["principal_id"] = "other"
        elif change == "role": hub.identity["role"] = "guest"
        elif change == "capability": hub.identity["capabilities"]["commerce_proposals"] = False
        elif change == "merchant": hub.merchant["version"] = "b" * 64
        elif change == "session": current[0] = False
        elif change == "expiry": now[0] = 400
        else: hub.rows[ACTION_ID]["payload"]["lines"][0]["quantity"] = 3
        return True
    handle(review, "prepare reviewed cart", confirm, lambda: current[0])
    assert coordinator.effects == []


@pytest.mark.parametrize("change", ["cart_key", "checkout_url", "total", "session", "merchant"])
def test_checkout_digest_binds_private_details_and_costs_across_confirmation(change):
    hub, coordinator, review, _ = setup()
    coordinator.make_prepared()
    handle(review, "review checkout")
    current = [True]
    def confirm(_):
        if change == "cart_key": coordinator.private["cart_id"] = "gid://shopify/Cart/other?key=OTHER_SECRET"
        elif change == "checkout_url": coordinator.private["checkout_url"] = f"https://{SHOP}/cart/c/OTHER_SECRET"
        elif change == "total": hub.rows[ACTION_ID]["evidence"]["total_minor"] += 1
        elif change == "session": current[0] = False
        else: hub.merchant["version"] = "b" * 64
        return True
    result = handle(review, "open reviewed checkout", confirm, lambda: current[0])
    assert coordinator.effects == []
    assert_private_absent(result)


def test_changed_checkout_before_open_is_not_silently_rereviewed():
    hub, coordinator, review, _ = setup()
    coordinator.make_prepared()
    handle(review, "review checkout")
    hub.rows[ACTION_ID]["evidence"]["total_minor"] += 1
    prompts = []
    handle(review, "open reviewed checkout", lambda prompt: prompts.append(prompt) or True)
    assert prompts == [] and coordinator.effects == []


@pytest.mark.parametrize("event,command", [("list", "review cart"), ("read", "review cart"), ("checkout_read", "review checkout")])
def test_session_cancellation_during_blocking_read_does_not_disclose_review(event, command):
    hub, coordinator, review, _ = setup()
    current = [True]
    target = coordinator if event == "checkout_read" else hub
    target.hooks[event] = lambda: current.__setitem__(0, False)
    if event == "checkout_read": coordinator.make_prepared()
    result = handle(review, command, current=lambda: current[0])
    assert "Cotton shirt" not in result and "INR 24.68" not in result
    assert_private_absent(result)
    assert coordinator.effects == []


def test_unknown_exception_clears_review_and_never_echoes_private_details():
    _, coordinator, review, _ = setup()
    handle(review, "review cart")
    def fail(): raise RuntimeError(PRIVATE["checkout_url"])
    coordinator.hooks["before_effect"] = fail
    result = handle(review, "prepare reviewed cart")
    assert "confirm" in result.lower() or "unknown" in result.lower()
    assert_private_absent(result)
    coordinator.hooks.clear()
    handle(review, "prepare reviewed cart")
    assert coordinator.effects == []


def test_reject_clears_cache_and_requires_exact_unexpired_review():
    hub, coordinator, review, _ = setup()
    handle(review, "review cart")
    result = handle(review, "reject reviewed cart")
    assert "rejected" in result.lower() and hub.rows[ACTION_ID]["status"] == "rejected"
    handle(review, "prepare reviewed cart")
    assert not any(event[0] == "prepare" for event in coordinator.effects)


def test_new_controller_or_clear_requires_new_review():
    hub, coordinator, review, _ = setup()
    handle(review, "review cart")
    review.clear()
    handle(review, "prepare reviewed cart")
    fresh = api().CommerceReview(hub, expected_principal="owner", merchant=merchant(), coordinator=coordinator, clock=lambda: 200)
    handle(fresh, "prepare reviewed cart")
    assert coordinator.effects == []


def test_opened_failed_or_unknown_checkout_is_never_selected_for_reopen():
    for status in ("opened", "failed", "unknown", "executing"):
        hub, coordinator, review, _ = setup()
        coordinator.make_prepared()
        hub.rows[ACTION_ID]["checkout_status"] = status
        if status in {"opened", "failed"}:
            hub.rows[ACTION_ID]["checkout_evidence"] = {"payment_status": "not_submitted"}
        assert "no matching" in handle(review, "review checkout").lower()
        assert coordinator.reads == [] and coordinator.effects == []


def test_unconfigured_coordinator_cannot_prepare_or_open():
    hub, _, _, _ = setup()
    review = api().CommerceReview(hub, expected_principal="owner", merchant=merchant(), clock=lambda: 200)
    handle(review, "review cart")
    assert "not configured" in handle(review, "prepare reviewed cart").lower()
    assert hub.effects == []


def test_factory_ignores_untrusted_identity_and_does_no_provider_work(monkeypatch):
    mod = api()
    import brain.commerce_tools as tools
    seen = []
    account = SimpleNamespace(merchant=merchant())
    monkeypatch.setattr(tools, "configured_merchant_account", lambda hub, identity: seen.append(identity) or account)
    hub = Hub()
    for role, enabled in (("child", True), ("guest", True), ("adult", False)):
        identity = {"principal_id": "owner", "role": role, "capabilities": {"commerce_proposals": enabled}}
        assert mod.configured_commerce_review(hub, identity) is None
    assert seen == []
    review = mod.configured_commerce_review(hub, hub.identity)
    assert review.expected_principal == "owner" and review.merchant == merchant()
    assert review.coordinator.account is account and len(seen) == 1


def test_factory_unavailable_configuration_returns_none_without_secret_logs(monkeypatch, caplog):
    import brain.commerce_tools as tools
    def fail(*_): raise RuntimeError("PRIVATE_ACCOUNT_TOKEN")
    monkeypatch.setattr(tools, "configured_merchant_account", fail)
    hub = Hub()
    assert api().configured_commerce_review(hub, hub.identity) is None
    assert "PRIVATE_ACCOUNT_TOKEN" not in caplog.text


@pytest.mark.parametrize("capabilities", [None, [], "enabled"])
def test_factory_malformed_capabilities_fail_closed(capabilities):
    identity = {"principal_id": "owner", "role": "adult", "capabilities": capabilities}
    assert api().configured_commerce_review(Hub(), identity) is None


def test_private_checkout_key_reflected_into_public_title_is_not_read_aloud():
    hub, coordinator, review, _ = setup()
    row = hub.rows[ACTION_ID]
    row["payload"]["lines"][0]["title"] += " PRIVATE_URL_KEY"
    row["argument_hash"] = digest({"kind": row["kind"], "payload": row["payload"]})
    coordinator.make_prepared()
    shown = handle(review, "review checkout")
    assert_private_absent(shown)
    assert "INR 27.00" not in shown and coordinator.effects == []


@pytest.mark.parametrize("clock", [float("nan"), float("inf"), -1])
def test_invalid_clock_never_makes_quote_fresh(clock):
    _, coordinator, review, now = setup()
    now[0] = clock
    shown = handle(review, "review cart")
    assert "Cotton shirt" not in shown and coordinator.effects == []


def test_configured_merchant_is_allowed_among_maximum_128_trusted_merchants():
    hub, _, review, _ = setup()
    rows = [merchant()] + [{**merchant(), "id": f"store-{index}"} for index in range(127)]
    hub.merchants = lambda: {"merchants": copy.deepcopy(rows)}
    assert "Cotton shirt" in handle(review, "review cart")


def test_checkout_path_word_shared_with_public_merchant_domain_is_not_a_secret_match():
    _, coordinator, review, _ = setup()
    coordinator.make_prepared()
    coordinator.private["checkout_url"] = f"https://{SHOP}/cart/c/fixture?key=PRIVATE_URL_KEY"
    shown = handle(review, "review checkout")
    assert SHOP in shown and "Cotton shirt" in shown and "INR 27.00" in shown
    assert coordinator.private["checkout_url"] not in shown
    assert_private_absent(shown)
