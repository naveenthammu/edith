use axum::{
    body::{to_bytes, Body},
    http::{Request, StatusCode},
    Router,
};
use edith_central_hub::{app, Config};
use serde_json::{json, Value};
use tempfile::TempDir;
use tower::ServiceExt;
const A: &str = "commerce-adult-synthetic-credential-00001";
const B: &str = "commerce-elder-synthetic-credential-00002";
const C: &str = "commerce-child-synthetic-credential-00003";
fn config(d: &TempDir, merchants: bool) -> Config {
    let mut v = json!({"database_path":d.path().join("hub.db"),"principals":[{"id":"owner","token":A,"role":"adult"},{"id":"other","token":B,"role":"elder"},{"id":"child","token":C,"role":"child"}]});
    if merchants {
        v["commerce_merchants"] = json!([{"principal_id":"owner","id":"shop","display_name":"Synthetic Store","shop_domain":"synthetic.myshopify.com","checkout_hosts":["synthetic.myshopify.com"]}]);
    }
    serde_json::from_value(v).expect("Trusted merchant configuration should be supported")
}
async fn call(r: &Router, m: &str, p: &str, t: &str, b: Value) -> (StatusCode, Value) {
    let res = r
        .clone()
        .oneshot(
            Request::builder()
                .method(m)
                .uri(p)
                .header("content-type", "application/json")
                .header("authorization", format!("Bearer {t}"))
                .body(Body::from(b.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = res.status();
    let bytes = to_bytes(res.into_body(), 1_000_000).await.unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}
fn now() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs() as i64
}
fn line() -> Value {
    json!({"variant_id":"gid://shopify/ProductVariant/123","title":"Cotton shirt — blue","quantity":2,"unit_price_minor":120000})
}
async fn proposal(r: &Router, key: &str) -> Value {
    let (s, m) = call(r, "GET", "/v1/merchants", A, json!({})).await;
    assert_eq!(s, StatusCode::OK);
    json!({"merchant_id":"shop","merchant_version":m["merchants"][0]["version"],"lines":[line()],"currency":"INR","observed_at":now(),"idempotency_key":key})
}
async fn create(r: &Router, key: &str) -> Value {
    let (s, v) = call(r, "POST", "/v1/commerce", A, proposal(r, key).await).await;
    assert_eq!(s, StatusCode::CREATED, "{v}");
    v
}
fn path(v: &Value, suffix: &str) -> String {
    format!("/v1/commerce/{}{}", v["id"].as_str().unwrap(), suffix)
}
async fn approved(r: &Router, key: &str) -> Value {
    let v = create(r, key).await;
    let (s, v) = call(
        r,
        "POST",
        &path(&v, "/decide"),
        A,
        json!({"decision":"approve","argument_hash":v["argument_hash"]}),
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    v
}
fn completion() -> Value {
    json!({"status":"prepared","evidence":{"provider":"shopify","payment_status":"not_submitted","currency":"INR","subtotal_minor":240000,"total_minor":250000,"lines":[line()]},"checkout":{"cart_id":"gid://shopify/Cart/synthetic?key=secretCartKey","checkout_url":"https://synthetic.myshopify.com/cart/c/synthetic?key=secretCheckout"}})
}
async fn prepared(r: &Router, key: &str) -> Value {
    let v = approved(r, key).await;
    assert_eq!(
        call(r, "POST", &path(&v, "/claim"), A, json!({})).await.0,
        StatusCode::OK
    );
    let (s, v) = call(r, "POST", &path(&v, "/result"), A, completion()).await;
    assert_eq!(s, StatusCode::OK, "{v}");
    v
}

#[tokio::test]
async fn unconfigured_commerce_is_empty_and_requires_an_authorized_adult() {
    let d = TempDir::new().unwrap();
    let r = app(config(&d, false)).unwrap();
    assert_eq!(
        call(&r, "GET", "/v1/merchants", A, json!({})).await,
        (StatusCode::OK, json!({"merchants":[]}))
    );
    assert_eq!(
        call(&r, "GET", "/v1/commerce", C, json!({})).await.0,
        StatusCode::FORBIDDEN
    );
    assert_eq!(
        call(&r, "GET", "/v1/commerce", "incorrect", json!({}))
            .await
            .0,
        StatusCode::UNAUTHORIZED
    );
}
#[tokio::test]
async fn proposals_are_owner_scoped_idempotent_and_hash_bound() {
    let d = TempDir::new().unwrap();
    let r = app(config(&d, true)).unwrap();
    let input = proposal(&r, "one").await;
    let (s, v) = call(&r, "POST", "/v1/commerce", A, input.clone()).await;
    assert_eq!(s, StatusCode::CREATED);
    assert_eq!(v["payload"]["quoted_subtotal_minor"], 240000);
    assert_eq!(v["payload"]["merchant_name"], "Synthetic Store");
    assert_eq!(
        call(&r, "POST", "/v1/commerce", A, input.clone()).await,
        (StatusCode::OK, v.clone())
    );
    let mut changed = input.clone();
    changed["lines"][0]["quantity"] = json!(1);
    assert_eq!(
        call(&r, "POST", "/v1/commerce", A, changed).await.0,
        StatusCode::CONFLICT
    );
    for suffix in ["", "/checkout"] {
        assert_eq!(
            call(&r, "GET", &path(&v, suffix), B, json!({})).await.0,
            StatusCode::NOT_FOUND
        );
    }
    assert_eq!(
        call(&r, "GET", "/v1/merchants", B, json!({})).await.1,
        json!({"merchants":[]})
    );
    assert_eq!(
        call(&r, "POST", "/v1/commerce", B, input).await.0,
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        call(
            &r,
            "POST",
            &path(&v, "/decide"),
            A,
            json!({"decision":"approve","argument_hash":"wrong"})
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
    assert_eq!(
        call(
            &r,
            "POST",
            &path(&v, "/decide"),
            C,
            json!({"decision":"approve","argument_hash":v["argument_hash"]})
        )
        .await
        .0,
        StatusCode::FORBIDDEN
    );
    assert_eq!(
        call(&r, "POST", &path(&v, "/claim"), A, json!({})).await.0,
        StatusCode::CONFLICT
    );
    assert_eq!(
        call(
            &r,
            "POST",
            &path(&v, "/decide"),
            A,
            json!({"decision":"reject","argument_hash":v["argument_hash"]})
        )
        .await
        .0,
        StatusCode::OK
    );
    assert_eq!(
        call(&r, "POST", &path(&v, "/claim"), A, json!({})).await.0,
        StatusCode::CONFLICT
    );
}
#[tokio::test]
async fn quote_validation_rejects_staleness_substitutions_overflows_and_unknown_fields() {
    let d = TempDir::new().unwrap();
    let r = app(config(&d, true)).unwrap();
    let base = proposal(&r, "bad").await;
    let variants = [
        ("observed_at", json!(now() - 301)),
        ("observed_at", json!(now() + 31)),
        ("currency", json!("JPY")),
        ("lines", json!([])),
        ("quoted_subtotal_minor", json!(1)),
        ("shop_domain", json!("evil.example")),
    ];
    for (k, value) in variants {
        let mut bad = base.clone();
        bad[k] = value;
        assert_eq!(
            call(&r, "POST", "/v1/commerce", A, bad).await.0,
            StatusCode::BAD_REQUEST,
            "{k}"
        );
    }
    for (k, value) in [
        ("quantity", json!(0)),
        ("quantity", json!(11)),
        ("unit_price_minor", json!(-1)),
        ("unit_price_minor", json!(i64::MAX)),
        ("variant_id", json!("https://evil.example/item")),
        ("title", json!("bad\ntext")),
    ] {
        let mut bad = base.clone();
        bad["lines"][0][k] = value;
        assert_eq!(
            call(&r, "POST", "/v1/commerce", A, bad).await.0,
            StatusCode::BAD_REQUEST,
            "{k}"
        );
    }
    let mut bad = base.clone();
    bad["lines"] = json!([line(), line()]);
    assert_eq!(
        call(&r, "POST", "/v1/commerce", A, bad).await.0,
        StatusCode::BAD_REQUEST
    );
}
#[tokio::test]
async fn only_one_cart_and_one_checkout_claim_wins_and_private_values_never_enter_public_rows() {
    let d = TempDir::new().unwrap();
    let r = app(config(&d, true)).unwrap();
    let v = approved(&r, "race").await;
    let claim = path(&v, "/claim");
    let (a, b) = tokio::join!(
        call(&r, "POST", &claim, A, json!({})),
        call(&r, "POST", &claim, A, json!({}))
    );
    let mut codes = vec![a.0.as_u16(), b.0.as_u16()];
    codes.sort();
    assert_eq!(codes, vec![200, 409]);
    let result = completion();
    let (s, v) = call(&r, "POST", &path(&v, "/result"), A, result.clone()).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(
        call(&r, "POST", &path(&v, "/result"), A, result).await.0,
        StatusCode::OK
    );
    for p in ["/v1/commerce".to_owned(), path(&v, "")] {
        let (s, public) = call(&r, "GET", &p, A, json!({})).await;
        assert_eq!(s, StatusCode::OK);
        for secret in ["secretCartKey", "secretCheckout", "checkout_url", "cart_id"] {
            assert!(!public.to_string().contains(secret));
        }
    }
    let (s, private) = call(&r, "GET", &path(&v, "/checkout"), A, json!({})).await;
    assert_eq!(s, StatusCode::OK);
    assert!(private["checkout"]["checkout_url"]
        .as_str()
        .unwrap()
        .contains("secretCheckout"));
    let claim = path(&v, "/checkout/claim");
    let hash = json!({"argument_hash":v["argument_hash"]});
    let (a, b) = tokio::join!(
        call(&r, "POST", &claim, A, hash.clone()),
        call(&r, "POST", &claim, A, hash)
    );
    let mut codes = vec![a.0.as_u16(), b.0.as_u16()];
    codes.sort();
    assert_eq!(codes, vec![200, 409]);
    let done = json!({"status":"opened","evidence":{"payment_status":"not_submitted"}});
    let (s, v) = call(&r, "POST", &path(&v, "/checkout/result"), A, done.clone()).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["checkout_status"], "opened");
    assert_eq!(
        call(&r, "POST", &path(&v, "/checkout/result"), A, done)
            .await
            .0,
        StatusCode::OK
    );
    assert_eq!(
        call(
            &r,
            "POST",
            &claim,
            A,
            json!({"argument_hash":v["argument_hash"]})
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
}
#[tokio::test]
async fn prepared_receipts_reject_unsafe_origins_changed_quotes_and_terminal_rewrites() {
    let d = TempDir::new().unwrap();
    let r = app(config(&d, true)).unwrap();
    let v = approved(&r, "receipt").await;
    assert_eq!(
        call(&r, "POST", &path(&v, "/claim"), A, json!({})).await.0,
        StatusCode::OK
    );
    for url in [
        "https://evil.example/cart/c/x",
        "https://synthetic.myshopify.com@evil.example/cart/c/x",
        "http://synthetic.myshopify.com/cart/c/x",
        "https://synthetic.myshopify.com/cart/c/x?redirect=https://evil.example",
        "https://synthetic.myshopify.com/cart/c/x#secret",
        "https://synthetic.myshopify.com/cart/c/x?key=%0d%0a",
    ] {
        let mut bad = completion();
        bad["checkout"]["checkout_url"] = json!(url);
        assert_eq!(
            call(&r, "POST", &path(&v, "/result"), A, bad).await.0,
            StatusCode::BAD_REQUEST,
            "{url}"
        );
    }
    for pointer in [
        "/evidence/currency",
        "/evidence/subtotal_minor",
        "/evidence/lines/0/quantity",
        "/evidence/lines/0/title",
    ] {
        let mut bad = completion();
        *bad.pointer_mut(pointer).unwrap() =
            if pointer.ends_with("currency") || pointer.ends_with("title") {
                json!("changed")
            } else {
                json!(1)
            };
        assert_eq!(
            call(&r, "POST", &path(&v, "/result"), A, bad).await.0,
            StatusCode::BAD_REQUEST
        );
    }
    let unknown = json!({"status":"unknown","evidence":{"provider":"shopify","payment_status":"not_submitted","currency":null,"subtotal_minor":null,"total_minor":null,"lines":[]},"checkout":null});
    assert_eq!(
        call(&r, "POST", &path(&v, "/result"), A, unknown).await.0,
        StatusCode::OK
    );
    assert_eq!(
        call(&r, "POST", &path(&v, "/result"), A, completion())
            .await
            .0,
        StatusCode::CONFLICT
    );
}
#[tokio::test]
async fn restart_preserves_records_but_never_replays_inflight_cart_or_opening() {
    let d = TempDir::new().unwrap();
    let r = app(config(&d, true)).unwrap();
    let cart = approved(&r, "inflight").await;
    call(&r, "POST", &path(&cart, "/claim"), A, json!({})).await;
    let open = prepared(&r, "opening").await;
    call(
        &r,
        "POST",
        &path(&open, "/checkout/claim"),
        A,
        json!({"argument_hash":open["argument_hash"]}),
    )
    .await;
    drop(r);
    let r = app(config(&d, true)).unwrap();
    let (_, cart2) = call(&r, "GET", &path(&cart, ""), A, json!({})).await;
    assert_eq!(cart2["status"], "unknown");
    let (_, open2) = call(&r, "GET", &path(&open, ""), A, json!({})).await;
    assert_eq!(open2["status"], "prepared");
    assert_eq!(open2["checkout_status"], "unknown");
    assert_eq!(
        call(&r, "POST", &path(&cart, "/claim"), A, json!({}))
            .await
            .0,
        StatusCode::CONFLICT
    );
    assert_eq!(
        call(
            &r,
            "POST",
            &path(&open, "/checkout/claim"),
            A,
            json!({"argument_hash":open["argument_hash"]})
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
}

#[tokio::test]
async fn merchant_revision_changes_revoke_pending_approval_cart_execution_and_checkout_opening() {
    let d = TempDir::new().unwrap();
    let r = app(config(&d, true)).unwrap();
    let waiting = create(&r, "waiting").await;
    let approved = approved(&r, "approved").await;
    let prepared = prepared(&r, "prepared").await;
    drop(r);
    let mut cfg = config(&d, true);
    cfg.commerce_merchants[0]
        .checkout_hosts
        .push("checkout.synthetic.test".into());
    let r = app(cfg).unwrap();
    assert_eq!(
        call(
            &r,
            "POST",
            &path(&waiting, "/decide"),
            A,
            json!({"decision":"approve","argument_hash":waiting["argument_hash"]})
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
    assert_eq!(
        call(&r, "POST", &path(&approved, "/claim"), A, json!({}))
            .await
            .0,
        StatusCode::CONFLICT
    );
    assert_eq!(
        call(&r, "GET", &path(&prepared, "/checkout"), A, json!({}))
            .await
            .0,
        StatusCode::CONFLICT
    );
    assert_eq!(
        call(
            &r,
            "POST",
            &path(&prepared, "/checkout/claim"),
            A,
            json!({"argument_hash":prepared["argument_hash"]})
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
    assert_eq!(
        call(&r, "GET", &path(&prepared, ""), A, json!({})).await.1["status"],
        "prepared"
    );
}

#[test]
fn merchant_configuration_rejects_wildcards_foreign_principals_duplicates_and_malformed_hosts() {
    let d = TempDir::new().unwrap();
    for field in ["shop_domain", "checkout_hosts"] {
        for bad in [
            "https://synthetic.myshopify.com",
            "*.myshopify.com",
            "127.0.0.1",
            "SYNTHETIC.myshopify.com",
            "synthetic.myshopify.com:443",
            "synthetic.myshopify.com.",
        ] {
            let mut cfg = config(&d, true);
            if field == "shop_domain" {
                cfg.commerce_merchants[0].shop_domain = bad.into();
            } else {
                cfg.commerce_merchants[0].checkout_hosts = vec![bad.into()];
            }
            assert!(cfg.validate().is_err(), "{bad}");
        }
    }
    for owner in ["missing", "child"] {
        let mut cfg = config(&d, true);
        cfg.commerce_merchants[0].principal_id = owner.into();
        assert!(cfg.validate().is_err());
    }
    let mut cfg = config(&d, true);
    cfg.commerce_merchants
        .push(cfg.commerce_merchants[0].clone());
    assert!(cfg.validate().is_err());
}

#[tokio::test]
async fn every_mutation_remains_owner_scoped_and_no_preclaim_result_is_accepted() {
    let d = TempDir::new().unwrap();
    let r = app(config(&d, true)).unwrap();
    let v = create(&r, "isolation").await;
    for (suffix, body) in [
        (
            "/decide",
            json!({"decision":"approve","argument_hash":v["argument_hash"]}),
        ),
        ("/claim", json!({})),
        ("/result", completion()),
        (
            "/checkout/claim",
            json!({"argument_hash":v["argument_hash"]}),
        ),
        (
            "/checkout/result",
            json!({"status":"opened","evidence":{"payment_status":"not_submitted"}}),
        ),
    ] {
        assert_eq!(
            call(&r, "POST", &path(&v, suffix), B, body.clone()).await.0,
            StatusCode::NOT_FOUND
        );
        assert_eq!(
            call(&r, "POST", &path(&v, suffix), C, body).await.0,
            StatusCode::FORBIDDEN
        );
    }
    assert_eq!(
        call(&r, "POST", &path(&v, "/result"), A, completion())
            .await
            .0,
        StatusCode::CONFLICT
    );
    assert_eq!(
        call(
            &r,
            "POST",
            &path(&v, "/checkout/result"),
            A,
            json!({"status":"opened","evidence":{"payment_status":"not_submitted"}})
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
    let caps = call(&r, "GET", "/v1/capabilities", A, json!({})).await.1;
    assert_eq!(caps["capabilities"]["commerce_proposals"], true);
    assert_eq!(caps["capabilities"]["merchant_checkout_connected"], false);
}
