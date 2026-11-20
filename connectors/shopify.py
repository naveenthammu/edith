"""Bounded Shopify catalog observations and manual merchant-checkout preparation.

No order/payment submission, browser opening, customer identity or address input.
The caller must obtain appropriate household/action approval before creating a
cart. Shop domains, credentials and checkout hosts are trusted configuration,
never model-selected URLs. Catalog strings remain untrusted merchant content.

Schema: https://shopify.dev/docs/api/storefront/2026-10/mutations/cartCreate
Costs: https://shopify.dev/docs/api/storefront/2026-10/objects/CartCost
"""
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from urllib.error import HTTPError
from urllib.parse import parse_qsl, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

API_VERSION = "2026-10"
MAX_RESPONSE_BYTES = 262_144
_SHOP = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.myshopify\.com\Z")
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\Z")
_TOKEN = re.compile(r"[A-Za-z0-9._~+/-]+=*\Z")
_AMOUNT = re.compile(r"[0-9]{1,12}(?:\.[0-9]{1,2})?\Z")
_CURRENCIES = frozenset({"INR", "USD", "EUR", "GBP"})
_REDIRECT_KEYS = frozenset({"return_to", "return_url", "returnurl", "redirect", "redirect_uri", "redirect_url", "next", "url"})
_CHECKOUT_PATH = re.compile(r"/(?:cart/c/|(?:[0-9]+/)?checkouts/)[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*/?\Z")

CATALOG_QUERY = """query EdithCatalog($search: String!, $limit: Int!) {
  products(first: $limit, query: $search) {
    pageInfo { hasNextPage }
    nodes { id title onlineStoreUrl
      variants(first: 5) { pageInfo { hasNextPage }
        nodes { id title availableForSale price { amount currencyCode } }
      }
    }
  }
}"""
QUOTE_QUERY = """query EdithExactVariants($ids: [ID!]!) {
  nodes(ids: $ids) { __typename ... on ProductVariant {
    id title availableForSale price { amount currencyCode } product { id title }
  } }
}"""
CART_MUTATION = """mutation EdithCart($input: CartInput!) {
  cartCreate(input: $input) {
    userErrors { field message code }
    warnings { code message }
    cart { id checkoutUrl totalQuantity
      cost { subtotalAmount { amount currencyCode } totalAmount { amount currencyCode }
             subtotalAmountEstimated totalAmountEstimated }
      lines(first: 10) { pageInfo { hasNextPage }
        nodes { __typename id quantity
          merchandise { __typename ... on ProductVariant { id title availableForSale product { id title } } }
          cost { amountPerQuantity { amount currencyCode } subtotalAmount { amount currencyCode }
                 totalAmount { amount currencyCode } }
        }
      }
    }
  }
}"""
CART_QUERY = """query EdithReadCart($id: ID!) {
  cart(id: $id) { id checkoutUrl totalQuantity
    cost { subtotalAmount { amount currencyCode } totalAmount { amount currencyCode }
           subtotalAmountEstimated totalAmountEstimated }
    lines(first: 10) { pageInfo { hasNextPage }
      nodes { __typename id quantity
        merchandise { __typename ... on ProductVariant { id title availableForSale product { id title } } }
        cost { amountPerQuantity { amount currencyCode } subtotalAmount { amount currencyCode }
               totalAmount { amount currencyCode } }
      }
    }
  }
}"""


class ShopifyError(ValueError):
    """Sanitized configuration/input/catalog error; no provider body or secrets."""


@dataclass(frozen=True)
class ShopifyResponse:
    status_code: int
    body: bytes


class _Failure(Exception):
    def __init__(self, status):
        self.status = status


def _allowed(gate):
    try:
        return callable(gate) and gate() is True
    except Exception:
        return False


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_transport(request, *, timeout, max_response_bytes):
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        response = opener.open(request, timeout=timeout)
    except HTTPError as error:
        status = error.code
        error.close()
        return ShopifyResponse(status, b"")
    with response:
        if response.status != 200:
            return ShopifyResponse(response.status, b"")
        size = response.headers.get("Content-Length")
        if size is not None and (not size.isdigit() or int(size) > max_response_bytes):
            return ShopifyResponse(200, b"")
        return ShopifyResponse(200, response.read(max_response_bytes + 1))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _contains_secret(value, secret):
    """Check decoded JSON values and keys too; wire escaping must not bypass redaction."""
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, str) and secret in current:
            return True
        if isinstance(current, dict):
            pending.extend(current.keys())
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return False


def _text(value, limit=300):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ShopifyError("Shopify returned invalid text.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ShopifyError("Shopify returned invalid text.") from None
    return value


def _gid(value, kind):
    if kind in {"Product", "ProductVariant"}:
        valid = isinstance(value, str) and re.fullmatch(r"gid://shopify/" + kind + r"/[1-9][0-9]{0,19}", value)
    else:
        valid = (isinstance(value, str) and len(value) <= 2048
                 and re.fullmatch(r"gid://shopify/" + kind + r"/[A-Za-z0-9_+=.-]+(?:\?key=[A-Za-z0-9_+=.-]+)?", value))
    if not valid:
        raise ShopifyError("A valid Shopify resource identifier is required.")
    return value


def _money(value):
    if (not isinstance(value, dict) or not isinstance(value.get("currencyCode"), str)
            or value["currencyCode"] not in _CURRENCIES
            or not isinstance(value.get("amount"), str) or not _AMOUNT.fullmatch(value["amount"])):
        raise ShopifyError("Shopify returned an unsupported or inexact price.")
    return int(Decimal(value["amount"]) * 100), value["currencyCode"]


def _connection(value, limit):
    if (not isinstance(value, dict) or not isinstance(value.get("nodes"), list)
            or len(value["nodes"]) > limit or not isinstance(value.get("pageInfo"), dict)
            or type(value["pageInfo"].get("hasNextPage")) is not bool
            or any(not isinstance(node, dict) for node in value["nodes"])):
        raise ShopifyError("Shopify returned an incomplete or oversized connection.")
    return value["nodes"], value["pageInfo"]["hasNextPage"]


class ShopifyStorefront:
    def __init__(self, shop_domain, credential_provider=None, *, checkout_hosts=None,
                 timeout=5, transport=None, clock=time.time):
        if not isinstance(shop_domain, str) or not _SHOP.fullmatch(shop_domain):
            raise ShopifyError("Configure one exact lowercase shop.myshopify.com domain.")
        if credential_provider is not None and not callable(credential_provider):
            raise ShopifyError("Configure a trusted optional Storefront credential provider.")
        if transport is not None and not callable(transport):
            raise ShopifyError("Configure a trusted transport.")
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ShopifyError("Timeout must be positive and at most 60 seconds.")
        hosts = {shop_domain} if checkout_hosts is None else checkout_hosts
        if (not isinstance(hosts, (set, frozenset)) or not 1 <= len(hosts) <= 10
                or any(not isinstance(host, str) or len(host) > 253 or not _HOST.fullmatch(host)
                       or host.replace(".", "").isdigit() for host in hosts)):
            raise ShopifyError("Configure exact checkout hostnames without URLs or wildcards.")
        self._shop = shop_domain
        self._endpoint = f"https://{shop_domain}/api/{API_VERSION}/graphql.json"
        self._hosts = frozenset(hosts)
        self._credentials = credential_provider
        self._transport = _http_transport if transport is None else transport
        self._timeout = timeout
        self._clock = clock

    def _url(self, value, *, checkout=False):
        if (not isinstance(value, str) or not 1 <= len(value) <= 2048 or not value.isascii()
                or any(ord(c) <= 32 or ord(c) == 127 for c in value) or "\\" in value):
            raise ShopifyError("Shopify returned an unsafe merchant URL.")
        try:
            parsed = urlsplit(value)
            allowed_hosts = self._hosts if checkout else self._hosts | {self._shop}
            valid = (parsed.scheme == "https" and parsed.hostname in allowed_hosts
                     and parsed.port in {None, 443} and parsed.username is None and parsed.password is None
                     and not parsed.fragment and parsed.path.startswith("/") and not parsed.path.startswith("//"))
            decoded = value
            for _ in range(3):
                decoded = unquote(decoded, errors="strict")
            valid = valid and not any(ord(c) < 32 or ord(c) == 127 or c == "\\" for c in decoded)
            if checkout:
                valid = (valid and bool(_CHECKOUT_PATH.fullmatch(parsed.path))
                         and not any(key.lower() in _REDIRECT_KEYS for key, _ in parse_qsl(urlsplit(decoded).query)))
            if not valid:
                raise ValueError("unsafe")
        except (ValueError, UnicodeError):
            raise ShopifyError("Shopify returned an unsafe merchant URL.") from None
        return value

    def _request(self, query, variables, *, can_execute=lambda: True):
        if not _allowed(can_execute):
            raise _Failure("failed")
        try:
            token = self._credentials() if self._credentials is not None else None
            if token is not None and (not isinstance(token, str) or not 1 <= len(token) <= 8192 or not _TOKEN.fullmatch(token)):
                raise ValueError("credential")
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            if token is not None:
                headers["X-Shopify-Storefront-Access-Token"] = token
            body = json.dumps({"query": query, "variables": variables}, allow_nan=False).encode("utf-8")
            if len(body) > 32_768:
                raise ValueError("request size")
            request = Request(self._endpoint, data=body, method="POST", headers=headers)
        except Exception:
            raise _Failure("failed") from None
        if not _allowed(can_execute):
            raise _Failure("failed")
        try:
            response = self._transport(request, timeout=self._timeout, max_response_bytes=MAX_RESPONSE_BYTES)
        except Exception:
            raise _Failure("unknown") from None
        if not isinstance(response, ShopifyResponse) or type(response.status_code) is not int:
            raise _Failure("unknown")
        if response.status_code in {400, 401, 403, 404, 429}:
            raise _Failure("failed")
        if (response.status_code != 200 or not isinstance(response.body, bytes)
                or len(response.body) > MAX_RESPONSE_BYTES or (token is not None and token.encode() in response.body)):
            raise _Failure("unknown")
        try:
            result = json.loads(response.body, object_pairs_hook=_unique_object)
            if ((token is not None and _contains_secret(result, token))
                    or not isinstance(result, dict) or ("errors" in result and result["errors"] != [])
                    or not isinstance(result.get("data"), dict)):
                raise ValueError("partial response")
            return result["data"]
        except (ValueError, UnicodeError, RecursionError):
            raise _Failure("unknown") from None

    def _observed(self):
        return datetime.fromtimestamp(self._clock(), timezone.utc).isoformat().replace("+00:00", "Z")

    def catalog(self, search, limit=5, *, can_execute=lambda: True):
        """Observe up to ten products and five variants each; no stock reservation."""
        if (not isinstance(search, str) or not search.strip() or len(search) > 200
                or any(ord(char) < 32 or ord(char) == 127 for char in search)
                or type(limit) is not int or not 1 <= limit <= 10):
            raise ShopifyError("Use a short catalog search and a result limit from one to ten.")
        try:
            data = self._request(CATALOG_QUERY, {"search": search, "limit": limit}, can_execute=can_execute)
            nodes, more = _connection(data.get("products"), limit)
            products, ids, variant_ids = [], set(), set()
            for product in nodes:
                product_id = _gid(product.get("id"), "Product")
                if product_id in ids or "onlineStoreUrl" not in product:
                    raise ShopifyError("Shopify returned duplicate or incomplete products.")
                ids.add(product_id)
                variants, variant_more = _connection(product.get("variants"), 5)
                observed_variants = []
                for variant in variants:
                    variant_id = _gid(variant.get("id"), "ProductVariant")
                    if variant_id in variant_ids or type(variant.get("availableForSale")) is not bool:
                        raise ShopifyError("Shopify returned invalid variant availability.")
                    variant_ids.add(variant_id)
                    amount, currency = _money(variant.get("price"))
                    observed_variants.append({"variant_id": variant_id, "title": _text(variant.get("title")),
                                              "available_for_sale": variant["availableForSale"],
                                              "price_minor": amount, "currency": currency})
                source = product["onlineStoreUrl"]
                products.append({"product_id": product_id, "title": _text(product.get("title")),
                                 "source_url": self._endpoint if source is None else self._url(source),
                                 "variants": observed_variants, "has_more_variants": variant_more})
            return {"provider": "shopify", "shop_domain": self._shop, "source_url": self._endpoint,
                    "observed_at": self._observed(), "products": products, "has_more_products": more,
                    "detail": "Prices and available-for-sale flags were observed at the stated time; they do not reserve inventory or guarantee delivery."}
        except _Failure:
            raise ShopifyError("The Shopify catalog response could not be verified; no retry was attempted.") from None

    @staticmethod
    def _lines(lines):
        if not isinstance(lines, list) or not 1 <= len(lines) <= 10:
            raise ShopifyError("Choose one to ten exact product variants.")
        wanted = {}
        for row in lines:
            if not isinstance(row, dict) or set(row) != {"variant_id", "quantity"}:
                raise ShopifyError("Cart lines accept only variant_id and quantity.")
            variant = _gid(row["variant_id"], "ProductVariant")
            quantity = row["quantity"]
            if type(quantity) is not int or not 1 <= quantity <= 10 or variant in wanted:
                raise ShopifyError("Use distinct variants with integer quantities from one to ten.")
            wanted[variant] = quantity
        if sum(wanted.values()) > 50:
            raise ShopifyError("Prepare at most fifty items in a cart.")
        return wanted

    def quote(self, lines, *, can_execute=lambda: True):
        """Read selected exact variants afresh; no cart, stock reservation or checkout."""
        wanted = self._lines(lines)
        try:
            data = self._request(QUOTE_QUERY, {"ids": list(wanted)}, can_execute=can_execute)
            nodes = data.get("nodes")
            if not isinstance(nodes, list) or len(nodes) != len(wanted):
                raise ShopifyError("The exact variant quote is incomplete.")
            observed, currencies = {}, set()
            for node in nodes:
                if (not isinstance(node, dict) or node.get("__typename") != "ProductVariant"
                        or node.get("availableForSale") is not True or not isinstance(node.get("product"), dict)):
                    raise ShopifyError("A selected variant is unavailable or could not be verified.")
                variant = _gid(node.get("id"), "ProductVariant")
                if variant not in wanted or variant in observed:
                    raise ShopifyError("The quote returned different or duplicate variants.")
                _gid(node["product"].get("id"), "Product")
                title = _text(_text(node["product"].get("title")) + " — " + _text(node.get("title")))
                amount, currency = _money(node.get("price"))
                currencies.add(currency)
                observed[variant] = {"variant_id": variant, "title": title,
                                     "quantity": wanted[variant], "unit_price_minor": amount}
            if len(currencies) != 1 or set(observed) != set(wanted):
                raise ShopifyError("The quote has mixed currencies or missing variants.")
            selected = [observed[variant] for variant in wanted]
            return {"provider": "shopify", "shop_domain": self._shop, "source_url": self._endpoint,
                    "observed_at": self._observed(), "lines": selected, "currency": currencies.pop(),
                    "quoted_subtotal_minor": sum(line["quantity"] * line["unit_price_minor"] for line in selected)}
        except _Failure:
            raise ShopifyError("The exact variant quote could not be verified; no retry was attempted.") from None

    def create_cart(self, lines, *, can_execute=lambda: True):
        """One cartCreate attempt; never open checkout, submit payment, or retry."""
        return self._cart_request(lines, can_execute=can_execute)

    def read_cart(self, cart_id, lines, *, can_execute=lambda: True):
        """Read one exact private cart; caller must not expose its key to the model."""
        _gid(cart_id, "Cart")
        return self._cart_request(lines, cart_id=cart_id, can_execute=can_execute)

    def validate_checkout_url(self, url):
        """Validate a trusted journal receipt against this configured merchant's hosts."""
        return self._url(url, checkout=True)

    def _cart_request(self, lines, *, cart_id=None, can_execute):
        wanted = self._lines(lines)
        empty = {"provider": "shopify", "cart_id": None, "checkout_url": None, "payment_status": "not_submitted"}
        try:
            if cart_id is None:
                data = self._request(CART_MUTATION, {"input": {"lines": [
                    {"merchandiseId": variant, "quantity": quantity} for variant, quantity in wanted.items()]}}, can_execute=can_execute)
                payload = data.get("cartCreate")
            else:
                data = self._request(CART_QUERY, {"id": cart_id}, can_execute=can_execute)
                payload = {"cart": data.get("cart"), "userErrors": [], "warnings": []}
            if not isinstance(payload, dict) or not isinstance(payload.get("userErrors"), list) or not isinstance(payload.get("warnings"), list):
                raise ShopifyError("Incomplete cart result")
            if payload["userErrors"]:
                raise _Failure("failed" if payload.get("cart") is None else "unknown")
            if payload["warnings"]:
                raise ShopifyError("Cart was automatically changed")
            cart = payload.get("cart")
            if not isinstance(cart, dict):
                raise ShopifyError("No verifiable cart")
            returned_cart_id = _gid(cart.get("id"), "Cart")
            if cart_id is not None and returned_cart_id != cart_id:
                raise ShopifyError("The returned cart identity changed.")
            cart_id = returned_cart_id
            checkout = self._url(cart.get("checkoutUrl"), checkout=True)
            nodes, more = _connection(cart.get("lines"), 10)
            if more or len(nodes) != len(wanted) or type(cart.get("totalQuantity")) is not int or cart["totalQuantity"] != sum(wanted.values()):
                raise ShopifyError("Incomplete cart quantities")
            cost = cart.get("cost")
            if not isinstance(cost, dict) or type(cost.get("totalAmountEstimated")) is not bool or type(cost.get("subtotalAmountEstimated")) is not bool:
                raise ShopifyError("Incomplete cart costs")
            subtotal, currency = _money(cost.get("subtotalAmount"))
            total, total_currency = _money(cost.get("totalAmount"))
            if currency != total_currency:
                raise ShopifyError("Cart currencies differ")
            observed, returned, line_ids = [], {}, set()
            for line in nodes:
                line_id = _gid(line.get("id"), "CartLine")
                merchandise, line_cost = line.get("merchandise"), line.get("cost")
                if (line.get("__typename") != "CartLine" or not isinstance(merchandise, dict)
                        or merchandise.get("__typename") != "ProductVariant" or not isinstance(line_cost, dict)
                        or line_id in line_ids or type(line.get("quantity")) is not int
                        or type(merchandise.get("availableForSale")) is not bool):
                    raise ShopifyError("Invalid cart line")
                line_ids.add(line_id)
                variant = _gid(merchandise.get("id"), "ProductVariant")
                if variant in returned or wanted.get(variant) != line["quantity"]:
                    raise ShopifyError("Cart variant or quantity changed")
                returned[variant] = line["quantity"]
                unit, unit_currency = _money(line_cost.get("amountPerQuantity"))
                line_subtotal, subtotal_currency = _money(line_cost.get("subtotalAmount"))
                line_total, line_currency = _money(line_cost.get("totalAmount"))
                if {unit_currency, subtotal_currency, line_currency} != {currency}:
                    raise ShopifyError("Cart line currency changed")
                product = merchandise.get("product")
                if not isinstance(product, dict):
                    raise ShopifyError("Incomplete cart product")
                observed.append({"line_id": line_id, "product_id": _gid(product.get("id"), "Product"),
                                 "title": _text(product.get("title")), "variant_id": variant,
                                 "variant_title": _text(merchandise.get("title")), "quantity": line["quantity"],
                                 "available_for_sale": merchandise["availableForSale"], "unit_price_minor": unit,
                                 "subtotal_minor": line_subtotal, "total_minor": line_total, "currency": currency})
            if returned != wanted:
                raise ShopifyError("Cart lines changed")
            return {**empty, "status": "prepared", "cart_id": cart_id, "checkout_url": checkout,
                    "lines": observed, "estimated_subtotal_minor": subtotal, "estimated_total_minor": total,
                    "currency": currency, "total_estimated": cost["totalAmountEstimated"],
                    "subtotal_estimated": cost["subtotalAmountEstimated"], "observed_at": self._observed(),
                    "source_url": self._endpoint,
                    "detail": "Cart prepared for manual merchant checkout. Costs are estimates and can change at final merchant checkout. No payment or order was submitted."}
        except _Failure as error:
            status = error.status
        except (ShopifyError, ValueError, TypeError, KeyError, OverflowError):
            status = "unknown"
        return {**empty, "status": status, "detail": (
            "Cart preparation was rejected or could not be authorized. No retry was attempted."
            if status == "failed" else
            "Cart creation or its returned contents could not be confirmed. A cart may exist; no retry was attempted. Reconcile before creating another cart.")}
