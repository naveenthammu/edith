"""Trusted cart preparation and separate manual-checkout opening.

Only a dispatcher that authenticates the person and freshly confirms the full
selection/costs may call mutation/open methods. A boolean/hash is not proof of
consent; these are never model tools. Catalog/proposal tools must not expose the
private checkout wrapper. There is no order, payment, card, OTP, or checkout-form
automation here. Uncertain claims/effects are never replayed.
"""
from copy import deepcopy
from datetime import datetime
import hashlib
import hmac
import json
import re
import time
from urllib.parse import urlsplit

from connectors.shopify import ShopifyStorefront, _gid, _SHOP, _CHECKOUT_PATH


_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_TOTAL_LIMIT = 4_999_999_999_999_950
_STATUSES = {"awaiting_approval", "approved", "rejected", "executing", "prepared", "failed", "unknown"}
_CHECKOUT_STATUSES = {"not_opened", "executing", "opened", "failed", "unknown"}
_ROW_FIELDS = {"id", "kind", "payload", "argument_hash", "created_at", "expires_at", "status", "evidence",
               "recovery_note", "checkout_status", "checkout_evidence"}
_PAYLOAD_FIELDS = {"merchant_id", "merchant_version", "merchant_name", "shop_domain", "lines", "currency", "observed_at", "quoted_subtotal_minor"}
_EVIDENCE_FIELDS = {"provider", "payment_status", "currency", "subtotal_minor", "total_minor", "lines"}


class CommerceError(RuntimeError):
    """Safe validation/authorization failure, never containing cart secrets."""


class CommerceOutcomeUnknown(CommerceError):
    status = "unknown"

    def __init__(self, action_id, stage):
        self.action_id, self.stage = action_id, stage
        super().__init__("The operation outcome could not be durably confirmed. No retry was attempted; reconcile before trying again.")


def _label(value, limit):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise CommerceError("Invalid commerce text.")
    value.encode("utf-8")
    return value


def _integer(value, maximum):
    return type(value) is int and 0 <= value <= maximum


def _lines(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 10:
        raise CommerceError("Choose one to ten exact variants.")
    selected = []
    for line in value:
        if not isinstance(line, dict) or set(line) != {"variant_id", "title", "quantity", "unit_price_minor"}:
            raise CommerceError("The quoted lines are invalid.")
        _label(line["title"], 300)
        if not _integer(line["unit_price_minor"], 99_999_999_999_999):
            raise CommerceError("A quoted price is invalid.")
        selected.append({"variant_id": line["variant_id"], "quantity": line["quantity"]})
    ShopifyStorefront._lines(selected)
    return deepcopy(value)


def _digest(payload):
    return hashlib.sha256(json.dumps({"kind": "commerce.prepare_cart", "payload": payload}, sort_keys=True,
                                      separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def _empty_evidence():
    return {"provider": "shopify", "payment_status": "not_submitted", "currency": None,
            "subtotal_minor": None, "total_minor": None, "lines": []}


def _evidence(value, payload, prepared):
    if not prepared:
        return value == _empty_evidence()
    if isinstance(value, dict):
        _lines(value.get("lines"))
    return (isinstance(value, dict) and set(value) == _EVIDENCE_FIELDS and value.get("provider") == "shopify"
            and value.get("payment_status") == "not_submitted" and value.get("currency") == payload["currency"]
            and value.get("lines") == payload["lines"] and type(value.get("subtotal_minor")) is int
            and value["subtotal_minor"] == payload["quoted_subtotal_minor"] and _integer(value.get("total_minor"), _TOTAL_LIMIT))


def validate_commerce(value):
    """Validate the public canonical Rust row; no private checkout fields accepted."""
    try:
        if (not isinstance(value, dict) or set(value) != _ROW_FIELDS or value["kind"] != "commerce.prepare_cart"
                or not isinstance(value["id"], str) or not _ID.fullmatch(value["id"])
                or not isinstance(value["status"], str) or value["status"] not in _STATUSES
                or not isinstance(value["checkout_status"], str) or value["checkout_status"] not in _CHECKOUT_STATUSES):
            raise ValueError("row")
        payload = value["payload"]
        if (not isinstance(payload, dict) or set(payload) != _PAYLOAD_FIELDS
                or not isinstance(payload["merchant_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", payload["merchant_id"])
                or not isinstance(payload["merchant_version"], str) or not _HASH.fullmatch(payload["merchant_version"])
                or not isinstance(payload["shop_domain"], str) or not _SHOP.fullmatch(payload["shop_domain"])
                or not isinstance(payload["currency"], str) or payload["currency"] not in {"INR", "USD", "EUR", "GBP"}
                or not _integer(payload["observed_at"], 9_223_372_036_854_775_807)):
            raise ValueError("payload")
        _label(payload["merchant_name"], 120)
        lines = _lines(payload["lines"])
        if (not _integer(payload["quoted_subtotal_minor"], _TOTAL_LIMIT)
                or payload["quoted_subtotal_minor"] != sum(line["quantity"] * line["unit_price_minor"] for line in lines)
                or not _integer(value["created_at"], 9_223_372_036_854_775_807)
                or not _integer(value["expires_at"], 9_223_372_036_854_775_807)
                or not value["created_at"] < value["expires_at"] <= min(value["created_at"] + 300, payload["observed_at"] + 300)
                or not isinstance(value["argument_hash"], str) or not _HASH.fullmatch(value["argument_hash"])
                or not hmac.compare_digest(value["argument_hash"], _digest(payload))):
            raise ValueError("hash or time")
        status, evidence = value["status"], value["evidence"]
        if status == "prepared":
            if not _evidence(evidence, payload, True): raise ValueError("evidence")
        elif status in {"failed", "unknown"}:
            if not (status == "unknown" and evidence is None) and not _evidence(evidence, payload, False):
                raise ValueError("empty evidence")
        elif evidence is not None:
            raise ValueError("premature evidence")
        if value["recovery_note"] is not None: _label(value["recovery_note"], 500)
        if value["checkout_status"] != "not_opened" and status != "prepared": raise ValueError("checkout status")
        if value["checkout_status"] in {"opened", "failed"}:
            if value["checkout_evidence"] != {"payment_status": "not_submitted"}: raise ValueError("checkout evidence")
        elif value["checkout_evidence"] is not None and not (value["checkout_status"] == "unknown" and value["checkout_evidence"] == {"payment_status": "not_submitted"}):
            raise ValueError("checkout evidence")
        return deepcopy(value)
    except Exception:
        raise CommerceError("The commerce journal receipt is invalid or does not match its immutable quote.") from None


def _checkout(value):
    if not isinstance(value, dict) or set(value) != {"cart_id", "checkout_url"}:
        raise CommerceError("The private checkout receipt is invalid.")
    _gid(value["cart_id"], "Cart")
    url = value["checkout_url"]
    if not isinstance(url, str) or not 1 <= len(url) <= 2048 or not url.isascii() or any(ord(c) <= 32 or ord(c) == 127 or c == "\\" for c in url):
        raise CommerceError("The checkout URL is invalid.")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.port not in {None, 443} or parsed.fragment or not _CHECKOUT_PATH.fullmatch(parsed.path)
            or (parsed.query and not re.fullmatch(r"key=[A-Za-z0-9_+=.-]+", parsed.query))):
        raise CommerceError("The checkout URL is invalid.")
    return deepcopy(value)


def validate_checkout_receipt(value):
    """Trusted-only wrapper; never put its cart ID/URL into model context or summaries."""
    try:
        if not isinstance(value, dict) or set(value) != {"action", "checkout"}: raise ValueError("wrapper")
        row = validate_commerce(value["action"])
        if row["status"] != "prepared": raise ValueError("not prepared")
        return {"action": row, "checkout": _checkout(value["checkout"])}
    except Exception:
        raise CommerceError("The private checkout receipt could not be verified.") from None


def _allowed(gate):
    try:
        return callable(gate) and gate() is True
    except Exception:
        return False


def _require(gate):
    if not _allowed(gate):
        raise CommerceError("Commerce authorization expired or the operation was cancelled.")


def _selected(payload):
    return [{"variant_id": line["variant_id"], "quantity": line["quantity"]} for line in payload["lines"]]


class CommerceCoordinator:
    def __init__(self, hub, merchant_account, *, opener=None, clock=None):
        self.hub, self.account = hub, merchant_account
        self._clock = time.time if clock is None else clock
        if opener is None:
            import webbrowser
            opener = webbrowser.open_new_tab
        if not callable(opener) or not callable(self._clock): raise CommerceError("Configure trusted local dependencies.")
        self._opener = opener

    def _row(self, value, action_id=None, digest=None, statuses=None):
        row = validate_commerce(value)
        if ((action_id is not None and row["id"] != action_id)
                or (digest is not None and row["argument_hash"] != digest)
                or (statuses is not None and row["status"] not in statuses)):
            raise CommerceError("The journal receipt differs from the reviewed action.")
        return row

    def _fresh(self, row):
        now = int(self._clock())
        if row["expires_at"] <= now or row["payload"]["observed_at"] > now + 30:
            raise CommerceError("This quote expired. Prepare and review a new proposal.")

    def _current(self, gate, payload=None):
        _require(gate)
        try:
            rows = self.hub.merchants()
            _require(gate)
            if not isinstance(rows, dict) or set(rows) != {"merchants"} or not isinstance(rows["merchants"], list) or len(rows["merchants"]) > 128:
                raise CommerceError("Invalid allowlist")
            matches = [row for row in rows["merchants"] if isinstance(row, dict) and row.get("id") == self.account.merchant["id"]]
            if len(matches) != 1: raise CommerceError("Merchant unavailable")
            selected = self.account.revalidate(matches[0])
            _require(gate)
            if payload is not None and any(payload[field] != selected[key] for field, key in
                                          (("merchant_id", "id"), ("merchant_version", "version"), ("merchant_name", "display_name"), ("shop_domain", "shop_domain"))):
                raise CommerceError("Merchant changed")
            return selected
        except Exception:
            raise CommerceError("The current merchant binding or authorization could not be verified.") from None

    def _quote(self, lines, selected, gate):
        try:
            quote = self.account.storefront.quote(lines, can_execute=gate)
            _require(gate)
            if not isinstance(quote, dict) or quote.get("provider") != "shopify" or quote.get("shop_domain") != selected["shop_domain"]:
                raise ValueError("source")
            quoted = _lines(quote.get("lines"))
            if _selected({"lines": quoted}) != lines or quote.get("currency") not in {"INR", "USD", "EUR", "GBP"}:
                raise ValueError("selection")
            observed = datetime.fromisoformat(quote["observed_at"].replace("Z", "+00:00"))
            if observed.utcoffset() is None: raise ValueError("time zone")
            timestamp = int(observed.timestamp())
            now = int(self._clock())
            if not now - 300 < timestamp <= now + 30: raise ValueError("stale quote")
            subtotal = sum(row["quantity"] * row["unit_price_minor"] for row in quoted)
            if quote.get("quoted_subtotal_minor") != subtotal or not _integer(subtotal, _TOTAL_LIMIT): raise ValueError("subtotal")
            return {"lines": quoted, "currency": quote["currency"], "observed_at": timestamp, "quoted_subtotal_minor": subtotal}
        except Exception:
            raise CommerceError("The fresh exact-variant quote could not be verified. No cart was requested.") from None

    def prepare(self, lines, idempotency_key, *, can_execute=lambda: True):
        """Trusted fresh-quote helper. Model proposals must use the separate bounded catalog cache."""
        try:
            ShopifyStorefront._lines(lines)
            if not isinstance(idempotency_key, str) or not _ID.fullmatch(idempotency_key): raise ValueError("key")
        except Exception:
            raise CommerceError("Use bounded exact variant selections and an explicit request key.") from None
        selected = self._current(can_execute)
        quote = self._quote(lines, selected, can_execute)
        self._current(can_execute)
        payload = {"merchant_id": selected["id"], "merchant_version": selected["version"], "merchant_name": selected["display_name"],
                   "shop_domain": selected["shop_domain"], **quote}
        try:
            result = self.hub.propose_commerce(selected["id"], selected["version"], quote["lines"], quote["currency"], quote["observed_at"], idempotency_key)
            return self._row(result, digest=_digest(payload))
        except Exception:
            raise CommerceError("The proposal could not be confirmed. No cart or checkout was requested.") from None

    def _cart_outcome(self, payload, result):
        empty = _empty_evidence()
        try:
            if not isinstance(result, dict) or result.get("provider") != "shopify" or result.get("payment_status") != "not_submitted":
                return "unknown", empty, None
            if result.get("status") == "failed": return "failed", empty, None
            if result.get("status") != "prepared": return "unknown", empty, None
            private = _checkout({"cart_id": result.get("cart_id"), "checkout_url": result.get("checkout_url")})
            self.account.storefront.validate_checkout_url(private["checkout_url"])
            returned = result.get("lines")
            if not isinstance(returned, list) or len(returned) != len(payload["lines"]): raise ValueError("lines")
            indexed = {}
            for line in returned:
                if not isinstance(line, dict) or line.get("variant_id") in indexed: raise ValueError("line")
                if line.get("available_for_sale") is not True or line.get("currency") != payload["currency"]: raise ValueError("availability")
                if (type(line.get("quantity")) is not int or type(line.get("unit_price_minor")) is not int
                        or type(line.get("subtotal_minor")) is not int or line["subtotal_minor"] != line["quantity"] * line["unit_price_minor"]):
                    raise ValueError("cost")
                indexed[line["variant_id"]] = {"variant_id": line["variant_id"], "title": _label(line.get("title"), 300) + " — " + _label(line.get("variant_title"), 300),
                                                 "quantity": line["quantity"], "unit_price_minor": line["unit_price_minor"]}
            quoted_lines = [indexed[line["variant_id"]] for line in payload["lines"]]
            observed = {"provider": "shopify", "payment_status": "not_submitted", "currency": result.get("currency"),
                        "subtotal_minor": result.get("estimated_subtotal_minor"), "total_minor": result.get("estimated_total_minor"), "lines": quoted_lines}
            if not _evidence(observed, payload, True): raise ValueError("changed quote")
            return "prepared", observed, private
        except Exception:
            return "unknown", empty, None

    def _complete_cart(self, row, status, evidence, private):
        try:
            result = self.hub.complete_commerce(row["id"], status, evidence, private)
            final = self._row(result, row["id"], row["argument_hash"], {status})
            if final["evidence"] != evidence: raise CommerceError("Evidence mismatch")
            return final
        except Exception:
            raise CommerceOutcomeUnknown(row["id"], "cart") from None

    def decide_and_prepare(self, action_id, argument_hash, approve, *, can_execute=lambda: False):
        if (not isinstance(action_id, str) or not _ID.fullmatch(action_id) or not isinstance(argument_hash, str)
                or not _HASH.fullmatch(argument_hash) or type(approve) is not bool):
            raise CommerceError("A valid action ID, exact hash and trusted boolean decision are required.")
        _require(can_execute)
        if approve:
            try: row = self._row(self.hub.commerce_action(action_id), action_id, argument_hash, {"awaiting_approval", "approved"})
            except Exception: raise CommerceError("The reviewed proposal could not be verified.") from None
            _require(can_execute)
            self._fresh(row)
            selected = self._current(can_execute, row["payload"])
            quote = self._quote(_selected(row["payload"]), selected, can_execute)
            if any(quote[key] != row["payload"][key] for key in ("lines", "currency", "quoted_subtotal_minor")):
                raise CommerceError("The selection or price changed. Prepare and review a new proposal; no cart was requested.")
        _require(can_execute)
        try:
            if approve and row["status"] == "approved":
                decision = row  # Fresh consent/quote above; the durable single claim still controls execution.
            else:
                decision = self._row(self.hub.decide_commerce(action_id, "approve" if approve else "reject", argument_hash), action_id, argument_hash, {"approved" if approve else "rejected"})
        except Exception:
            raise CommerceError("The decision could not be confirmed. No cart was requested.") from None
        if not approve: return decision
        _require(can_execute)
        self._fresh(decision)
        try:
            claimed = self._row(self.hub.claim_commerce(action_id), action_id, argument_hash, {"executing"})
            if claimed["payload"] != decision["payload"]: raise CommerceError("Claim mismatch")
        except Exception:
            raise CommerceError("The cart action could not be claimed once. No cart was requested; reconcile the journal.") from None
        try:
            self._fresh(claimed)
            self._current(can_execute, claimed["payload"])
        except Exception:
            return self._complete_cart(claimed, "failed", _empty_evidence(), None)
        try:
            result = self.account.storefront.create_cart(_selected(claimed["payload"]), can_execute=can_execute)
        except Exception:
            result = None
        status, evidence, private = self._cart_outcome(claimed["payload"], result)
        return self._complete_cart(claimed, status, evidence, private)

    def checkout_for_review(self, action_id, *, can_execute=lambda: False):
        _require(can_execute)
        if not isinstance(action_id, str) or not _ID.fullmatch(action_id): raise CommerceError("Invalid checkout action ID.")
        try:
            wrapper = validate_checkout_receipt(self.hub.commerce_checkout(action_id))
            row = self._row(wrapper["action"], action_id, statuses={"prepared"})
            if row["checkout_status"] != "not_opened": raise CommerceError("Checkout already claimed")
            self._fresh(row)
            self._current(can_execute, row["payload"])
            self.account.storefront.validate_checkout_url(wrapper["checkout"]["checkout_url"])
            result = self.account.storefront.read_cart(wrapper["checkout"]["cart_id"], _selected(row["payload"]), can_execute=can_execute)
            _require(can_execute)
            status, evidence, private = self._cart_outcome(row["payload"], result)
            if status != "prepared" or evidence != row["evidence"] or private != wrapper["checkout"]:
                raise CommerceError("The cart changed")
            self._fresh(row)
            return wrapper
        except Exception:
            raise CommerceError("The current checkout selection, costs or authorization changed or could not be verified. Review a new proposal.") from None

    def open_checkout(self, action_id, argument_hash, *, can_execute=lambda: False):
        if not isinstance(argument_hash, str) or not _HASH.fullmatch(argument_hash): raise CommerceError("A valid exact-action hash is required.")
        wrapper = self.checkout_for_review(action_id, can_execute=can_execute)
        self._row(wrapper["action"], action_id, argument_hash, {"prepared"})
        _require(can_execute)
        try:
            claimed = validate_checkout_receipt(self.hub.claim_commerce_checkout(action_id, argument_hash))
            row = self._row(claimed["action"], action_id, argument_hash, {"prepared"})
            if (row["checkout_status"] != "executing" or row["payload"] != wrapper["action"]["payload"]
                    or row["evidence"] != wrapper["action"]["evidence"] or claimed["checkout"] != wrapper["checkout"]):
                raise CommerceError("Checkout claim mismatch")
        except Exception:
            raise CommerceError("Checkout could not be claimed once. No browser was requested; reconcile the journal.") from None
        status = "failed"
        try:
            self._fresh(row)
            self._current(can_execute, row["payload"])
            self.account.storefront.validate_checkout_url(claimed["checkout"]["checkout_url"])
            _require(can_execute)
        except Exception:
            pass
        else:
            try:
                status = "opened" if self._opener(claimed["checkout"]["checkout_url"]) is True else "unknown"
            except Exception:
                status = "unknown"
        observed = {"payment_status": "not_submitted"}
        try:
            final = self._row(self.hub.complete_commerce_checkout(action_id, status, observed), action_id, argument_hash, {"prepared"})
            if final["checkout_status"] != status or final["checkout_evidence"] != observed: raise CommerceError("Checkout outcome mismatch")
            return final
        except Exception:
            raise CommerceOutcomeUnknown(action_id, "checkout") from None
