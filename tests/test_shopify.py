"""Only injected HTTP responses: no real merchant catalog, cart, or checkout."""
import copy
import json

import pytest

from connectors.shopify import ShopifyError, ShopifyResponse, ShopifyStorefront


SHOP = "fixture-shop.myshopify.com"
VARIANT = "gid://shopify/ProductVariant/101"
PRODUCT = "gid://shopify/Product/10"
LINES = [{"variant_id": VARIANT, "quantity": 2}]


def money(amount="12.34", currency="INR"):
    return {"amount": amount, "currencyCode": currency}


def connection(nodes, more=False):
    return {"nodes": nodes, "pageInfo": {"hasNextPage": more}}


def catalog():
    return {"data": {"products": connection([{
        "id": PRODUCT, "title": "Cotton shirt", "onlineStoreUrl": f"https://{SHOP}/products/shirt",
        "variants": connection([{"id": VARIANT, "title": "Blue / M", "availableForSale": True,
                                  "price": money()}]),
    }])}}


def cart():
    return {"data": {"cartCreate": {"userErrors": [], "warnings": [], "cart": {
        "id": "gid://shopify/Cart/fixture-cart?key=privatecartkey",
        "checkoutUrl": f"https://{SHOP}/cart/c/fixture-checkout?key=checkoutkey",
        "totalQuantity": 2,
        "cost": {"subtotalAmount": money("24.68"), "totalAmount": money("27.00"),
                 "subtotalAmountEstimated": False, "totalAmountEstimated": True},
        "lines": connection([{
            "__typename": "CartLine", "id": "gid://shopify/CartLine/line-1", "quantity": 2,
            "merchandise": {"__typename": "ProductVariant", "id": VARIANT, "title": "Blue / M",
                            "availableForSale": True, "product": {"id": PRODUCT, "title": "Cotton shirt"}},
            "cost": {"amountPerQuantity": money(), "subtotalAmount": money("24.68"),
                     "totalAmount": money("24.68")},
        }]),
    }}}}


class Transport:
    def __init__(self, data=None, *, status=200, raw=None, error=None):
        self.body = json.dumps(data).encode() if raw is None else raw
        self.status = status
        self.error = error
        self.calls = []

    def __call__(self, request, **kwargs):
        self.calls.append((request, kwargs))
        if self.error:
            raise self.error
        return ShopifyResponse(self.status, self.body)


def client(transport, **kwargs):
    return ShopifyStorefront(SHOP, transport=transport, clock=lambda: 0, **kwargs)


def test_catalog_uses_fixed_endpoint_and_variables_and_returns_observed_minor_units():
    transport = Transport(catalog())
    store = client(transport, credential_provider=lambda: "synthetic_storefront_token_123")
    query = 'shirt" }) mutation { cartCreate'
    result = store.catalog(query)
    request, bounds = transport.calls[0]
    payload = json.loads(request.data)
    assert request.full_url == f"https://{SHOP}/api/2026-10/graphql.json"
    assert request.method == "POST"
    assert payload["variables"] == {"search": query, "limit": 5}
    assert query not in payload["query"]
    assert request.get_header("X-shopify-storefront-access-token") == "synthetic_storefront_token_123"
    assert bounds["timeout"] <= 60 and bounds["max_response_bytes"] <= 1_048_576
    variant = result["products"][0]["variants"][0]
    assert variant == {"variant_id": VARIANT, "title": "Blue / M", "available_for_sale": True,
                       "price_minor": 1234, "currency": "INR"}
    assert result["products"][0]["source_url"] == f"https://{SHOP}/products/shirt"
    assert result["observed_at"] == "1970-01-01T00:00:00Z"


def test_tokenless_catalog_reports_unavailable_and_truncation_without_inventing_stock():
    data = catalog()
    product = data["data"]["products"]["nodes"][0]
    product["variants"]["nodes"][0]["availableForSale"] = False
    product["variants"]["pageInfo"]["hasNextPage"] = True
    product["onlineStoreUrl"] = None
    transport = Transport(data)
    result = client(transport).catalog("shirt")
    assert transport.calls[0][0].get_header("X-shopify-storefront-access-token") is None
    assert result["products"][0]["variants"][0]["available_for_sale"] is False
    assert result["products"][0]["has_more_variants"] is True
    assert result["products"][0]["source_url"] == f"https://{SHOP}/api/2026-10/graphql.json"


@pytest.mark.parametrize("amount,currency", [("1.001", "INR"), ("1.000", "USD"), ("1", "JPY"),
                                             ("NaN", "EUR"), ("1e2", "GBP"), ("-1.00", "INR"),
                                             (1.2, "INR"), ("999999999999999999999999", "USD")])
def test_catalog_rejects_unsupported_or_inexact_money(amount, currency):
    data = catalog()
    data["data"]["products"]["nodes"][0]["variants"]["nodes"][0]["price"] = money(amount, currency)
    with pytest.raises(ShopifyError):
        client(Transport(data)).catalog("shirt")


@pytest.mark.parametrize("currency", ["INR", "USD", "EUR", "GBP"])
def test_supported_currency_conversion_is_exact(currency):
    data = catalog()
    data["data"]["products"]["nodes"][0]["variants"]["nodes"][0]["price"] = money("0.29", currency)
    assert client(Transport(data)).catalog("shirt")["products"][0]["variants"][0]["price_minor"] == 29


@pytest.mark.parametrize("domain", ["https://fixture-shop.myshopify.com", "myshopify.com", "evil.test",
                                   "shop.myshopify.com.evil.test", "a.b.myshopify.com", "shop.myshopify.com.",
                                   "shop.myshopify.com:443", "shop.myshopify.com@evil.test", "127.0.0.1"])
def test_untrusted_shop_endpoints_are_rejected_before_transport(domain):
    transport = Transport(catalog())
    with pytest.raises(ShopifyError):
        ShopifyStorefront(domain, transport=transport)
    assert transport.calls == []


def test_cart_prepares_only_exact_lines_and_manual_checkout_with_estimate_disclosure():
    transport = Transport(cart())
    result = client(transport).create_cart(LINES)
    payload = json.loads(transport.calls[0][0].data)
    assert payload["variables"] == {"input": {"lines": [{"merchandiseId": VARIANT, "quantity": 2}]}}
    assert len(transport.calls) == 1
    assert result["status"] == "prepared"
    assert result["cart_id"] == "gid://shopify/Cart/fixture-cart?key=privatecartkey"
    assert result["checkout_url"] == f"https://{SHOP}/cart/c/fixture-checkout?key=checkoutkey"
    assert result["estimated_subtotal_minor"] == 2468
    assert result["estimated_total_minor"] == 2700
    assert result["currency"] == "INR" and result["total_estimated"] is True
    assert result["lines"][0]["variant_id"] == VARIANT
    assert result["lines"][0]["quantity"] == 2
    assert "change" in result["detail"] and "merchant checkout" in result["detail"]
    assert result["payment_status"] == "not_submitted"


def test_final_checkout_disclosure_is_present_even_when_estimate_flag_is_false():
    data = cart()
    data["data"]["cartCreate"]["cart"]["cost"]["totalAmountEstimated"] = False
    result = client(Transport(data)).create_cart(LINES)
    assert result["total_estimated"] is False
    assert "change" in result["detail"]


@pytest.mark.parametrize("lines", [[], [{"variant_id": VARIANT, "quantity": True}],
                                  [{"variant_id": VARIANT, "quantity": 0}],
                                  [{"variant_id": VARIANT, "quantity": 11}], LINES * 2,
                                  [{"variant_id": "https://evil.test", "quantity": 1}],
                                  [{"variant_id": VARIANT, "quantity": 1, "email": "private@example.test"}],
                                  [{"variant_id": VARIANT, "quantity": 1, "address": "private"}]])
def test_invalid_or_customer_data_inputs_never_create_a_cart(lines):
    transport = Transport(cart())
    with pytest.raises(ShopifyError):
        client(transport).create_cart(lines)
    assert transport.calls == []


@pytest.mark.parametrize("change", ["variant", "quantity", "extra_line", "truncated", "currency", "malformed_money"])
def test_response_substitutions_and_incomplete_cart_never_expose_checkout(change):
    data = cart()
    value = data["data"]["cartCreate"]["cart"]
    line = value["lines"]["nodes"][0]
    if change == "variant":
        line["merchandise"]["id"] = "gid://shopify/ProductVariant/999"
    elif change == "quantity":
        line["quantity"] = 1
    elif change == "extra_line":
        value["lines"]["nodes"].append(copy.deepcopy(line))
    elif change == "truncated":
        value["lines"]["pageInfo"]["hasNextPage"] = True
    elif change == "currency":
        line["cost"]["totalAmount"]["currencyCode"] = "USD"
    else:
        value["cost"]["totalAmount"]["amount"] = "27.001"
    result = client(Transport(data)).create_cart(LINES)
    assert result["status"] == "unknown"
    assert result["checkout_url"] is None


@pytest.mark.parametrize("url", ["http://fixture-shop.myshopify.com/cart/c/x", "https://evil.test/cart/c/x",
                                "https://fixture-shop.myshopify.com.evil.test/cart/c/x",
                                "https://user@fixture-shop.myshopify.com/cart/c/x", "javascript:alert(1)",
                                "https://fixture-shop.myshopify.com:444/cart/c/x",
                                "https://fixture-shop.myshopify.com/admin",
                                "https://fixture-shop.myshopify.com/cart/c/x?return_to=https://evil.test",
                                "https://fixture-shop.myshopify.com/cart/c/x%0a",
                                "https://fixture-shop.myshopify.com\\@evil.test/cart/c/x"])
def test_unsafe_checkout_urls_are_never_returned(url):
    data = cart()
    data["data"]["cartCreate"]["cart"]["checkoutUrl"] = url
    assert client(Transport(data)).create_cart(LINES)["checkout_url"] is None


def test_custom_checkout_host_requires_explicit_exact_configuration():
    data = cart()
    data["data"]["cartCreate"]["cart"]["checkoutUrl"] = "https://checkout.fixture.example/checkouts/cn/token"
    assert client(Transport(data)).create_cart(LINES)["status"] == "unknown"
    result = client(Transport(data), checkout_hosts={"checkout.fixture.example"}).create_cart(LINES)
    assert result["status"] == "prepared"


def test_custom_checkout_configuration_does_not_remove_shop_catalog_sources():
    result = client(Transport(catalog()), checkout_hosts={"checkout.fixture.example"}).catalog("shirt")
    assert result["products"][0]["source_url"] == f"https://{SHOP}/products/shirt"


def test_catalog_malformed_currency_type_is_a_sanitized_validation_error():
    data = catalog()
    data["data"]["products"]["nodes"][0]["variants"]["nodes"][0]["price"]["currencyCode"] = ["INR"]
    with pytest.raises(ShopifyError):
        client(Transport(data)).catalog("shirt")


def test_encoded_checkout_redirect_parameter_is_not_a_safe_handoff():
    data = cart()
    data["data"]["cartCreate"]["cart"]["checkoutUrl"] = f"https://{SHOP}/cart/c/x?%2572eturn_to=https://evil.test"
    assert client(Transport(data)).create_cart(LINES)["checkout_url"] is None


def test_credential_reflected_in_provider_data_is_never_returned():
    data = catalog()
    secret = "synthetic_storefront_credential_123"
    data["data"]["products"]["nodes"][0]["title"] = secret
    with pytest.raises(ShopifyError) as error:
        client(Transport(data), credential_provider=lambda: secret).catalog("shirt")
    assert secret not in str(error.value)


@pytest.mark.parametrize("change", ["too_many_products", "too_many_variants", "missing_availability", "duplicate_product"])
def test_incomplete_or_oversized_catalog_observations_are_rejected(change):
    data = catalog()
    products = data["data"]["products"]["nodes"]
    if change == "too_many_products":
        products.extend(copy.deepcopy(products[0]) for _ in range(5))
    elif change == "too_many_variants":
        variants = products[0]["variants"]["nodes"]
        variants.extend(copy.deepcopy(variants[0]) for _ in range(5))
    elif change == "missing_availability":
        del products[0]["variants"]["nodes"][0]["availableForSale"]
    else:
        products.append(copy.deepcopy(products[0]))
    with pytest.raises(ShopifyError):
        client(Transport(data)).catalog("shirt")


@pytest.mark.parametrize("operation", ["catalog", "create_cart"])
def test_graphql_errors_reject_partial_data_without_echoing_provider_details(operation):
    data = catalog() if operation == "catalog" else cart()
    data["errors"] = [{"message": "private-provider-secret"}]
    transport = Transport(data)
    if operation == "catalog":
        with pytest.raises(ShopifyError) as caught:
            client(transport).catalog("shirt")
        assert "private-provider-secret" not in str(caught.value)
    else:
        result = client(transport).create_cart(LINES)
        assert result["status"] == "unknown" and result["checkout_url"] is None
        assert "private-provider-secret" not in str(result)
    assert len(transport.calls) == 1


def test_cart_user_errors_are_explicit_rejection_and_automatic_changes_are_not_accepted():
    rejected = {"data": {"cartCreate": {"cart": None, "userErrors": [{"message": "out of stock"}], "warnings": []}}}
    assert client(Transport(rejected)).create_cart(LINES)["status"] == "failed"
    changed = cart()
    changed["data"]["cartCreate"]["warnings"] = [{"code": "MERCHANDISE_NOT_ENOUGH_STOCK"}]
    assert client(Transport(changed)).create_cart(LINES)["status"] == "unknown"


@pytest.mark.parametrize("transport", [Transport(error=TimeoutError("private-token")),
                                       Transport(raw=b"{broken"), Transport(status=302),
                                       Transport(raw=b"x" * 1_048_577), Transport(status=503)])
def test_ambiguous_cart_creation_is_not_retried_or_reported_as_failure(transport):
    result = client(transport).create_cart(LINES)
    assert result["status"] == "unknown"
    assert result["checkout_url"] is None
    assert result["payment_status"] == "not_submitted"
    assert "private-token" not in str(result)
    assert len(transport.calls) == 1


def test_invalid_credentials_do_not_attempt_transport_or_leak_token():
    transport = Transport(cart())
    result = client(transport, credential_provider=lambda: "bad\r\nprivate-token").create_cart(LINES)
    assert result["status"] == "failed"
    assert "private-token" not in str(result)
    assert transport.calls == []


def test_default_transport_disables_proxies_redirects_and_bounds_read(monkeypatch):
    import connectors.shopify as module
    seen = {}

    class Response:
        status = 200
        headers = {}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, limit):
            seen["limit"] = limit
            return json.dumps(catalog()).encode()

    class Opener:
        def open(self, request, timeout):
            seen["request"] = request
            return Response()

    def build(*handlers):
        seen["handlers"] = handlers
        return Opener()

    monkeypatch.setattr(module, "build_opener", build)
    ShopifyStorefront(SHOP).catalog("shirt")
    assert seen["handlers"][0].proxies == {}
    redirect = seen["handlers"][1]
    assert redirect.redirect_request(seen["request"], None, 302, "redirect", {}, "https://evil.test") is None
    assert seen["limit"] <= 1_048_577


def quote_data():
    return {"data": {"nodes": [{"__typename": "ProductVariant", "id": VARIANT, "title": "Blue / M",
                                "availableForSale": True, "price": money(),
                                "product": {"id": PRODUCT, "title": "Cotton shirt"}}]}}


def test_exact_variant_quote_queries_selected_ids_not_catalog_search():
    transport = Transport(quote_data())
    result = client(transport).quote(LINES)
    request = json.loads(transport.calls[0][0].data)
    assert request["variables"] == {"ids": [VARIANT]}
    assert "nodes(ids:" in request["query"] and "mutation" not in request["query"]
    assert result["lines"] == [{"variant_id": VARIANT, "title": "Cotton shirt — Blue / M", "quantity": 2, "unit_price_minor": 1234}]
    assert result["currency"] == "INR" and result["quoted_subtotal_minor"] == 2468
    assert result["observed_at"] == "1970-01-01T00:00:00Z"


@pytest.mark.parametrize("change", ["missing", "duplicate", "different", "unavailable", "not_variant", "currency"])
def test_exact_quote_rejects_unavailable_substituted_or_ambiguous_variants(change):
    data = quote_data()
    node = data["data"]["nodes"][0]
    if change == "missing":
        data["data"]["nodes"] = [None]
    elif change == "duplicate":
        data["data"]["nodes"].append(copy.deepcopy(node))
    elif change == "different":
        node["id"] = "gid://shopify/ProductVariant/999"
    elif change == "unavailable":
        node["availableForSale"] = False
    elif change == "not_variant":
        node["__typename"] = "Product"
    else:
        node["price"]["currencyCode"] = "JPY"
    with pytest.raises(ShopifyError):
        client(Transport(data)).quote(LINES)


@pytest.mark.parametrize("operation", ["quote", "create_cart", "read_cart"])
def test_cancellation_during_credentials_prevents_any_shopify_request(operation):
    current = True
    transport = Transport(cart())
    def credentials():
        nonlocal current
        current = False
        return "synthetic_storefront_token"
    store = client(transport, credential_provider=credentials)
    if operation == "quote":
        with pytest.raises(ShopifyError):
            store.quote(LINES, can_execute=lambda: current)
    else:
        args = (LINES,) if operation == "create_cart" else ("gid://shopify/Cart/fixture-cart?key=privatecartkey", LINES)
        result = getattr(store, operation)(*args, can_execute=lambda: current)
        assert result["status"] == "failed" and result["checkout_url"] is None
    assert transport.calls == []


def test_fresh_cart_read_uses_exact_secret_id_and_never_mutates_cart():
    data = {"data": {"cart": cart()["data"]["cartCreate"]["cart"]}}
    transport = Transport(data)
    result = client(transport).read_cart("gid://shopify/Cart/fixture-cart?key=privatecartkey", LINES)
    request = json.loads(transport.calls[0][0].data)
    assert request["variables"] == {"id": "gid://shopify/Cart/fixture-cart?key=privatecartkey"}
    assert "mutation" not in request["query"] and "cart(id:" in request["query"]
    assert result["status"] == "prepared" and result["estimated_total_minor"] == 2700


def test_fresh_cart_read_rejects_different_cart_identity():
    data = {"data": {"cart": cart()["data"]["cartCreate"]["cart"]}}
    data["data"]["cart"]["id"] = "gid://shopify/Cart/different?key=other"
    result = client(Transport(data)).read_cart("gid://shopify/Cart/fixture-cart?key=privatecartkey", LINES)
    assert result["status"] == "unknown" and result["checkout_url"] is None


@pytest.mark.parametrize("operation", ["catalog", "quote", "read_cart"])
def test_json_escaped_credential_reflection_is_never_returned(operation):
    secret = "synthetic_storefront_credential_123"
    if operation == "catalog":
        data = catalog()
        data["data"]["products"]["nodes"][0]["title"] = secret
    elif operation == "quote":
        data = quote_data()
        data["data"]["nodes"][0]["product"]["title"] = secret
    else:
        data = {"data": {"cart": cart()["data"]["cartCreate"]["cart"]}}
        data["data"]["cart"]["lines"]["nodes"][0]["merchandise"]["product"]["title"] = secret
    escaped = "".join("\\u%04x" % ord(char) for char in secret)
    raw = json.dumps(data).replace(secret, escaped).encode()
    assert secret.encode() not in raw
    store = client(Transport(raw=raw), credential_provider=lambda: secret)
    if operation == "read_cart":
        result = store.read_cart("gid://shopify/Cart/fixture-cart?key=privatecartkey", LINES)
        assert result["status"] == "unknown" and secret not in str(result)
    else:
        with pytest.raises(ShopifyError) as error:
            getattr(store, operation)("shirt" if operation == "catalog" else LINES)
        assert secret not in str(error.value)
