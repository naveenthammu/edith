"""Trusted cart preparation and separate checkout review commands outside the LLM.

Configured adult/elder device access is not speaker biometrics. The application
must supply its trusted private-session confirmation and cancellation callbacks.
Private cart IDs/keys/URLs stay inside coordinator receipts and an internal digest;
readback contains only the merchant, exact item selection and monetary estimates.
"""
from copy import deepcopy
import hashlib
import hmac
import json
import logging
import math
import re
import time
from urllib.parse import parse_qs, urlsplit

from connectors.commerce_coordinator import validate_commerce, validate_checkout_receipt


_ACTION_ID = re.compile(r"[a-f0-9-]{8,36}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")


def configured_commerce_review(hub, identity):
    """Load an optional configured merchant; never contact its provider or open a browser."""
    if (not isinstance(identity, dict) or identity.get("role") not in {"adult", "elder"}
            or not isinstance(identity.get("principal_id"), str) or not identity["principal_id"]
            or not isinstance(identity.get("capabilities"), dict)
            or identity.get("capabilities", {}).get("commerce_proposals") is not True):
        return None
    from brain.commerce_tools import configured_merchant_account
    from connectors.commerce_coordinator import CommerceCoordinator
    try:
        account = configured_merchant_account(hub, identity)
        if account is None:
            return None
        return CommerceReview(hub, expected_principal=identity["principal_id"], merchant=account.merchant,
                              coordinator=CommerceCoordinator(hub, account))
    except Exception:
        logging.getLogger(__name__).warning("Shopping review disabled: the member's merchant configuration could not be verified.")
        return None


def _money(currency, minor):
    return f"{currency} {minor // 100}.{minor % 100:02d}"


def _private_digest(wrapper):
    return hashlib.sha256(json.dumps(wrapper, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


class CommerceReview:
    def __init__(self, hub, *, expected_principal, merchant, coordinator=None, clock=time.time):
        if (not isinstance(expected_principal, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", expected_principal)
                or not isinstance(merchant, dict) or not isinstance(merchant.get("id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", merchant["id"])
                or not isinstance(merchant.get("version"), str) or not _HASH.fullmatch(merchant["version"])
                or not callable(clock)):
            raise ValueError("Configure the exact commerce owner and merchant version.")
        self.hub, self.expected_principal = hub, expected_principal
        self.merchant, self.coordinator, self.clock = deepcopy(merchant), coordinator, clock
        self.clear()

    def clear(self):
        self._cart_review = None
        self._checkout_review = None

    def _identity(self, value):
        return (isinstance(value, dict) and value.get("principal_id") == self.expected_principal
                and value.get("role") in {"adult", "elder"}
                and isinstance(value.get("capabilities"), dict)
                and value["capabilities"].get("commerce_proposals") is True)

    def _allowed(self, still_current):
        try:
            if still_current() is not True or not self._identity(self.hub.capabilities()) or still_current() is not True:
                return False
            result = self.hub.merchants()
            if (still_current() is not True or not isinstance(result, dict) or set(result) != {"merchants"}
                    or not isinstance(result["merchants"], list) or len(result["merchants"]) > 128):
                return False
            matches = [row for row in result["merchants"] if isinstance(row, dict) and row.get("id") == self.merchant["id"]]
            return (matches == [self.merchant] and self._identity(self.hub.capabilities()) and still_current() is True)
        except Exception:
            return False

    def _require(self, still_current):
        if not self._allowed(still_current):
            raise ValueError("The commerce session or merchant binding changed.")

    def _bound(self, row):
        payload = row["payload"]
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            raise ValueError("The commerce clock is invalid.")
        if (payload["merchant_id"] != self.merchant["id"] or payload["merchant_version"] != self.merchant["version"]
                or payload["merchant_name"] != self.merchant["display_name"] or payload["shop_domain"] != self.merchant["shop_domain"]
                or not _ACTION_ID.fullmatch(row["id"]) or row["expires_at"] <= now
                or payload["observed_at"] > now + 30 or row["created_at"] > now + 30):
            raise ValueError("This merchant quote changed or expired.")
        return row

    def _row(self, identifier, still_current, *, statuses):
        self._require(still_current)
        row = validate_commerce(self.hub.commerce_action(identifier))
        self._require(still_current)
        if row["id"] != identifier or row["status"] not in statuses:
            raise ValueError("The cart proposal identity or status changed.")
        return self._bound(row)

    def _checkout(self, identifier, still_current):
        if self.coordinator is None:
            raise ValueError("Checkout is not configured.")
        self._require(still_current)
        wrapper = validate_checkout_receipt(self.coordinator.checkout_for_review(identifier, can_execute=lambda: self._allowed(still_current)))
        self._require(still_current)
        row = self._bound(wrapper["action"])
        if row["id"] != identifier or row["status"] != "prepared" or row["checkout_status"] != "not_opened":
            raise ValueError("Checkout is not available for opening.")
        # Refuse even an unexpected reflection of private checkout material into
        # public merchant/item labels; do not silently alter the exact readback.
        private = wrapper["checkout"]
        secrets = set(private.values())
        for value in private.values():
            secrets.update(parse_qs(urlsplit(value).query).get("key", []))
        summary = self._summary(row, checkout=True)
        if any(secret and secret in summary for secret in secrets):
            raise ValueError("Private checkout material appeared in public labels.")
        return wrapper

    def _summary(self, row, *, checkout=False):
        payload = row["payload"]
        currency = payload["currency"]
        parts = [f"Merchant: {payload['merchant_name']} ({payload['shop_domain']})."]
        for line in payload["lines"]:
            parts.append(f"Item: {line['title']}. Quantity: {line['quantity']}. Unit price: {_money(currency, line['unit_price_minor'])}. "
                         f"Line subtotal: {_money(currency, line['quantity'] * line['unit_price_minor'])}.")
        parts.append(f"Catalog subtotal: {_money(currency, payload['quoted_subtotal_minor'])}.")
        if checkout:
            evidence = row["evidence"]
            parts.append(f"Stored cart subtotal: {_money(currency, evidence['subtotal_minor'])}. Estimated total: {_money(currency, evidence['total_minor'])}.")
        parts.append("Shipping, taxes and final cost must be verified at the merchant checkout. Payment has not been submitted.")
        return " ".join(parts)

    def _review(self, stage, prefix, still_current):
        self.clear()
        self._require(still_current)
        result = self.hub.commerce()
        self._require(still_current)
        rows = result.get("commerce") if isinstance(result, dict) and set(result) == {"commerce"} else None
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError("Invalid cart list.")
        candidates = []
        statuses = {"prepared"} if stage == "checkout" else {"awaiting_approval", "approved"}
        for row in rows:
            if (not isinstance(row, dict) or row.get("status") not in statuses or row.get("checkout_status") != "not_opened"
                    or not isinstance(row.get("id"), str) or not _ACTION_ID.fullmatch(row["id"])
                    or (prefix and not row["id"].startswith(prefix)) or type(row.get("expires_at")) is not int
                    or row["expires_at"] <= self.clock() or not isinstance(row.get("payload"), dict)
                    or row["payload"].get("merchant_id") != self.merchant["id"]):
                continue
            candidates.append(self._bound(validate_commerce(row)))
        if not candidates:
            return f"No matching unexpired {stage} is ready for review. Prepare a fresh proposal first."
        if len(candidates) != 1:
            return (f"More than one {stage} matches. Say review {stage} followed by its eight-character ID: "
                    + ", ".join(row["id"][:8] for row in candidates[:10]))
        identifier = candidates[0]["id"]
        if stage == "cart":
            row = self._row(identifier, still_current, statuses=statuses)
            self._cart_review = (identifier, row["argument_hash"])
            return (f"Cart proposal {identifier}. " + self._summary(row)
                    + " Say prepare reviewed cart for a fresh confirmation, or reject reviewed cart.")
        if self.coordinator is None:
            return "Merchant checkout is not configured for this member."
        wrapper = self._checkout(identifier, still_current)
        row = wrapper["action"]
        self._checkout_review = (identifier, row["argument_hash"], _private_digest(wrapper))
        return (f"Checkout for proposal {identifier}. " + self._summary(row, checkout=True)
                + " Say open reviewed checkout for a fresh confirmation. You will review and pay directly with the merchant.")

    def _cart_effect(self, reject, confirm, still_current):
        if self._cart_review is None:
            return "Review a cart proposal first by saying review cart."
        identifier, argument_hash = self._cart_review
        row = self._row(identifier, still_current, statuses={"awaiting_approval", "approved"})
        if row["argument_hash"] != argument_hash:
            raise ValueError("The reviewed cart changed.")
        if not reject:
            if self.coordinator is None:
                return "Merchant cart preparation is not configured for this member."
            prompt = self._summary(row) + " Prepare this exact cart with the merchant? Say yes to prepare it."
            if confirm(prompt) is not True:
                self.clear()
                return "Cart preparation cancelled."
            self._require(still_current)
            latest = self._row(identifier, still_current, statuses={"awaiting_approval", "approved"})
            if latest["argument_hash"] != argument_hash:
                raise ValueError("The confirmed quote changed.")
        self._require(still_current)
        self.clear()
        if self.coordinator is None:
            result = self.hub.decide_commerce(identifier, "reject", argument_hash)
        else:
            result = self.coordinator.decide_and_prepare(identifier, argument_hash, not reject,
                                                         can_execute=lambda: self._allowed(still_current))
        self._require(still_current)
        result = validate_commerce(result)
        if result["id"] != identifier or result["argument_hash"] != argument_hash:
            raise ValueError("Mismatched cart receipt.")
        if reject and result["status"] == "rejected":
            return "The cart proposal was rejected."
        if not reject and result["status"] == "prepared":
            return "The merchant cart is prepared. Say review checkout to inspect its estimated total. No payment has been submitted."
        if not reject and result["status"] == "failed":
            return "Cart preparation failed. Inspect the recorded result before preparing another proposal."
        return "The cart outcome is unknown. Reconcile the recorded proposal before another attempt."

    def _open(self, confirm, still_current):
        if self._checkout_review is None:
            return "Review the prepared checkout first by saying review checkout."
        identifier, argument_hash, reviewed_digest = self._checkout_review
        wrapper = self._checkout(identifier, still_current)
        if not hmac.compare_digest(_private_digest(wrapper), reviewed_digest):
            raise ValueError("The reviewed checkout changed.")
        prompt = self._summary(wrapper["action"], checkout=True) + " Open this exact merchant checkout? You must verify delivery details and complete any payment yourself. Say yes to open it."
        if confirm(prompt) is not True:
            self.clear()
            return "Checkout opening cancelled."
        self._require(still_current)
        latest = self._checkout(identifier, still_current)
        if not hmac.compare_digest(_private_digest(latest), reviewed_digest):
            raise ValueError("The confirmed checkout changed.")
        self._require(still_current)
        self.clear()
        result = self.coordinator.open_checkout(identifier, argument_hash, can_execute=lambda: self._allowed(still_current))
        self._require(still_current)
        result = validate_commerce(result)
        if result["id"] != identifier or result["argument_hash"] != argument_hash:
            raise ValueError("Mismatched checkout receipt.")
        if result["checkout_status"] == "opened":
            return "The merchant checkout was opened. Review the final total and delivery details there. Payment and order completion are not verified."
        if result["checkout_status"] == "failed":
            return "Checkout opening failed. Inspect the recorded result before another attempt."
        return "Checkout opening is unconfirmed. Check the browser and recorded cart before trying again; payment is unverified."

    def handle(self, text, *, confirm, still_current):
        if not isinstance(text, str) or len(text) > 100:
            return None
        command = " ".join(text.strip().lower().rstrip(".!?").split())
        review = re.fullmatch(r"review (cart|checkout)(?: ([a-f0-9-]{8,36}))?", command)
        if review is None and command not in {"prepare reviewed cart", "reject reviewed cart", "open reviewed checkout"}:
            return None
        try:
            self._require(still_current)
            if review is not None:
                result = self._review(review.group(1), review.group(2), still_current)
            elif command == "open reviewed checkout":
                result = self._open(confirm, still_current)
            else:
                result = self._cart_effect(command == "reject reviewed cart", confirm, still_current)
            self._require(still_current)
            return result
        except Exception:
            self.clear()
            # A blocked or interrupted operation can already have a provider/browser effect.
            # Never expose exception text, private checkout fields or a false no-effect claim.
            return "The commerce operation could not be confirmed because its state, access or session changed. Inspect the recorded cart before another attempt."
