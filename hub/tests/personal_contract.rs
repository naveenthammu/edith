use axum::{
    body::{to_bytes, Body},
    http::{Request, StatusCode},
    Router,
};
use edith_central_hub::{app, Config};
use serde_json::{json, Value};
use tempfile::TempDir;
use tower::ServiceExt;
const A: &str = "owner-personal-contract-token-000000001";
const B: &str = "elder-personal-contract-token-000000002";
const C: &str = "child-personal-contract-token-000000003";
const G: &str = "guest-personal-contract-token-000000004";
fn config(d: &TempDir) -> Config {
    serde_json::from_value(json!({"database_path":d.path().join("hub.db"),"principals":[{"id":"owner","token":A,"role":"adult"},{"id":"elder","token":B,"role":"elder"},{"id":"child","token":C,"role":"child"},{"id":"guest","token":G,"role":"guest"}]})).unwrap()
}
async fn call(r: &Router, m: &str, p: &str, t: &str, b: Value) -> (StatusCode, Value) {
    let mut q = Request::builder()
        .method(m)
        .uri(p)
        .header("content-type", "application/json");
    if !t.is_empty() {
        q = q.header("authorization", format!("Bearer {t}"));
    }
    let res = r
        .clone()
        .oneshot(
            q.body(Body::from(if b.is_null() {
                String::new()
            } else {
                b.to_string()
            }))
            .unwrap(),
        )
        .await
        .unwrap();
    let status = res.status();
    let bytes = to_bytes(res.into_body(), 2_000_000).await.unwrap();
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
async fn create_reminder(r: &Router, key: &str) -> Value {
    let (s, v) = call(
        r,
        "POST",
        "/v1/reminders",
        A,
        json!({"text":"Check the garden","due_at":now(),"idempotency_key":key}),
    )
    .await;
    assert_eq!(s, StatusCode::CREATED, "{v}");
    v
}
async fn contact(r: &Router) -> Value {
    let (s, v) = call(
        r,
        "POST",
        "/v1/contacts",
        A,
        json!({"display_name":"Friend","email":"friend@example.test"}),
    )
    .await;
    assert_eq!(s, StatusCode::CREATED, "{v}");
    v
}
fn proposal(c: &Value, key: &str) -> Value {
    json!({"contact_id":c["id"],"contact_version":c["version"],"subject":"Hello","body":"A synthetic message.","idempotency_key":key})
}
async fn message(r: &Router, c: &Value, key: &str) -> Value {
    let (s, v) = call(r, "POST", "/v1/messages", A, proposal(c, key)).await;
    assert_eq!(s, StatusCode::CREATED, "{v}");
    v
}
fn route(base: &str, v: &Value, suffix: &str) -> String {
    format!("/v1/{base}/{}{}", v["id"].as_str().unwrap(), suffix)
}
async fn approve(r: &Router, m: &Value) -> (StatusCode, Value) {
    call(
        r,
        "POST",
        &route("messages", m, "/decide"),
        A,
        json!({"decision":"approve","argument_hash":m["argument_hash"]}),
    )
    .await
}

#[tokio::test]
async fn personal_apis_require_authenticated_adults_and_never_cross_owner() {
    let d = TempDir::new().unwrap();
    let r = app(config(&d)).unwrap();
    for base in ["reminders", "contacts", "messages"] {
        let p = format!("/v1/{base}");
        assert_eq!(
            call(&r, "GET", &p, "", Value::Null).await.0,
            StatusCode::UNAUTHORIZED
        );
        for t in [C, G] {
            assert_eq!(
                call(&r, "GET", &p, t, Value::Null).await.0,
                StatusCode::FORBIDDEN
            );
            assert_eq!(
                call(&r, "POST", &p, t, json!({})).await.0,
                StatusCode::FORBIDDEN
            );
        }
    }
    let rem = create_reminder(&r, "one").await;
    let c = contact(&r).await;
    let m = message(&r, &c, "one").await;
    for (base, v, suffix, method) in [
        ("reminders", &rem, "/cancel", "POST"),
        ("reminders", &rem, "/acknowledge", "POST"),
        ("contacts", &c, "", "DELETE"),
        ("messages", &m, "", "GET"),
        ("messages", &m, "/claim", "POST"),
    ] {
        assert_eq!(
            call(&r, method, &route(base, v, suffix), B, json!({}))
                .await
                .0,
            StatusCode::NOT_FOUND
        );
    }
    for base in ["reminders", "contacts", "messages"] {
        assert!(call(&r, "GET", &format!("/v1/{base}"), B, Value::Null)
            .await
            .1[base]
            .as_array()
            .unwrap()
            .is_empty());
    }
}

#[tokio::test]
async fn reminders_idempotency_validation_leases_and_terminal_invalidation() {
    let d = TempDir::new().unwrap();
    let r = app(config(&d)).unwrap();
    let t = now();
    let p = json!({"text":"Hydrate","due_at":t,"idempotency_key":"water"});
    let (s, rem) = call(&r, "POST", "/v1/reminders", A, p.clone()).await;
    assert_eq!(s, StatusCode::CREATED);
    assert_eq!(
        call(&r, "POST", "/v1/reminders", A, p.clone()).await.1["id"],
        rem["id"]
    );
    let mut changed = p.clone();
    changed["text"] = json!("Different");
    assert_eq!(
        call(&r, "POST", "/v1/reminders", A, changed).await.0,
        StatusCode::CONFLICT
    );
    for p in [
        json!({"text":" ","due_at":t,"idempotency_key":"bad"}),
        json!({"text":"x".repeat(401),"due_at":t,"idempotency_key":"bad"}),
        json!({"text":"x","due_at":i64::MAX,"idempotency_key":"bad"}),
        json!({"text":"x","due_at":t-61,"idempotency_key":"bad"}),
        json!({"text":"x","due_at":t,"idempotency_key":"bad","extra":true}),
    ] {
        assert_eq!(
            call(&r, "POST", "/v1/reminders", A, p).await.0,
            StatusCode::BAD_REQUEST
        );
    }
    let (first, second) = tokio::join!(
        call(&r, "POST", "/v1/reminders/claim-due", A, json!({})),
        call(&r, "POST", "/v1/reminders/claim-due", A, json!({}))
    );
    let claims = [first.1, second.1];
    assert_eq!(
        claims.iter().filter(|v| !v["reminder"].is_null()).count(),
        1
    );
    let claim = &claims.iter().find(|v| !v["reminder"].is_null()).unwrap()["reminder"];
    assert_eq!(
        call(
            &r,
            "POST",
            &route("reminders", &rem, "/cancel"),
            A,
            json!({})
        )
        .await
        .1["status"],
        "cancelled"
    );
    assert_eq!(
        call(
            &r,
            "POST",
            &route("reminders", &rem, "/delivery"),
            A,
            json!({"delivery_id":claim["delivery_id"],"status":"announced"})
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
    assert_eq!(
        call(
            &r,
            "POST",
            &route("reminders", &rem, "/cancel"),
            A,
            json!({})
        )
        .await
        .0,
        StatusCode::OK
    );
    assert_eq!(
        call(
            &r,
            "POST",
            &route("reminders", &rem, "/acknowledge"),
            A,
            json!({})
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
}

#[tokio::test]
async fn reminder_delivery_retry_ack_and_restart_are_durable() {
    let d = TempDir::new().unwrap();
    let r = app(config(&d)).unwrap();
    let rem = create_reminder(&r, "delivery").await;
    let claim = call(&r, "POST", "/v1/reminders/claim-due", A, json!({}))
        .await
        .1["reminder"]
        .clone();
    let path = route("reminders", &rem, "/delivery");
    let retry = json!({"delivery_id":claim["delivery_id"],"status":"retry"});
    assert_eq!(
        call(&r, "POST", &path, A, retry.clone()).await.1["status"],
        "scheduled"
    );
    assert_eq!(call(&r, "POST", &path, A, retry).await.0, StatusCode::OK);
    let next = call(&r, "POST", "/v1/reminders/claim-due", A, json!({}))
        .await
        .1["reminder"]
        .clone();
    assert_ne!(claim["delivery_id"], next["delivery_id"]);
    let delivered = json!({"delivery_id":next["delivery_id"],"status":"announced"});
    assert_eq!(
        call(&r, "POST", &path, A, delivered.clone()).await.1["status"],
        "announced"
    );
    assert_eq!(
        call(&r, "POST", &path, A, delivered).await.0,
        StatusCode::OK
    );
    drop(r);
    let r = app(config(&d)).unwrap();
    assert!(call(&r, "POST", "/v1/reminders/claim-due", A, json!({}))
        .await
        .1["reminder"]
        .is_null());
    assert_eq!(
        call(
            &r,
            "POST",
            &route("reminders", &rem, "/acknowledge"),
            A,
            json!({})
        )
        .await
        .1["status"],
        "acknowledged"
    );
}

#[tokio::test]
async fn contacts_versions_validation_and_message_contact_binding() {
    let d = TempDir::new().unwrap();
    let r = app(config(&d)).unwrap();
    for email in [
        "a@b\r\nBcc:other@test",
        "a@b,c@d",
        "a@",
        " a@b.test",
        "ü@example.test",
    ] {
        assert_eq!(
            call(
                &r,
                "POST",
                "/v1/contacts",
                A,
                json!({"display_name":"Friend","email":email})
            )
            .await
            .0,
            StatusCode::BAD_REQUEST
        );
    }
    let c = contact(&r).await;
    let m = message(&r, &c, "contact-version").await;
    assert_eq!(m["payload"]["recipient"], c["email"]);
    let update = json!({"expected_version":c["version"],"display_name":"Renamed","email":"new@example.test"});
    let changed = call(&r, "PUT", &route("contacts", &c, ""), A, update.clone()).await;
    assert_eq!(changed.0, StatusCode::OK);
    assert_ne!(changed.1["version"], c["version"]);
    assert_eq!(
        call(&r, "PUT", &route("contacts", &c, ""), A, update)
            .await
            .0,
        StatusCode::CONFLICT
    );
    assert_eq!(approve(&r, &m).await.0, StatusCode::CONFLICT);
    assert_eq!(
        call(&r, "POST", "/v1/messages", A, proposal(&c, "stale"))
            .await
            .0,
        StatusCode::CONFLICT
    );
    assert_eq!(
        call(
            &r,
            "POST",
            "/v1/messages",
            B,
            proposal(&changed.1, "foreign")
        )
        .await
        .0,
        StatusCode::NOT_FOUND
    );
}

#[tokio::test]
async fn messages_immutable_hash_decisions_claim_and_provider_receipts() {
    let d = TempDir::new().unwrap();
    let r = app(config(&d)).unwrap();
    let c = contact(&r).await;
    let m = message(&r, &c, "workflow").await;
    assert_eq!(
        call(&r, "POST", "/v1/messages", A, proposal(&c, "workflow"))
            .await
            .0,
        StatusCode::OK
    );
    let mut changed = proposal(&c, "workflow");
    changed["body"] = json!("different");
    assert_eq!(
        call(&r, "POST", "/v1/messages", A, changed).await.0,
        StatusCode::CONFLICT
    );
    assert_eq!(
        call(
            &r,
            "POST",
            &route("messages", &m, "/decide"),
            A,
            json!({"decision":"approve","argument_hash":"0".repeat(64)})
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
    assert_eq!(approve(&r, &m).await.1["status"], "approved");
    let claim_path = route("messages", &m, "/claim");
    let (a, b) = tokio::join!(
        call(&r, "POST", &claim_path, A, json!({})),
        call(&r, "POST", &claim_path, A, json!({}))
    );
    assert_eq!(
        [a.0, b.0].iter().filter(|s| **s == StatusCode::OK).count(),
        1
    );
    let path = route("messages", &m, "/result");
    let good = json!({"status":"accepted","evidence":{"provider":"gmail","recipient":c["email"],"provider_message_id":"synthetic123","delivery_status":"unverified"}});
    let mut impossible_failure = good.clone();
    impossible_failure["status"] = json!("failed");
    assert_eq!(
        call(&r, "POST", &path, A, impossible_failure).await.0,
        StatusCode::BAD_REQUEST
    );
    let mut bad = good.clone();
    bad["evidence"]["recipient"] = json!("other@example.test");
    assert_eq!(
        call(&r, "POST", &path, A, bad).await.0,
        StatusCode::BAD_REQUEST
    );
    let mut bad = good.clone();
    bad["evidence"]["provider_message_id"] = Value::Null;
    assert_eq!(
        call(&r, "POST", &path, A, bad).await.0,
        StatusCode::BAD_REQUEST
    );
    assert_eq!(
        call(&r, "POST", &path, A, good.clone()).await.1["status"],
        "accepted"
    );
    assert_eq!(call(&r, "POST", &path, A, good).await.0, StatusCode::OK);
    assert_eq!(
        call(&r, "DELETE", &route("contacts", &c, ""), A, Value::Null)
            .await
            .0,
        StatusCode::OK
    );
    assert_eq!(
        call(&r, "GET", &route("messages", &m, ""), A, Value::Null)
            .await
            .1["payload"]["recipient"],
        "friend@example.test"
    );
}

#[tokio::test]
async fn rejected_deleted_expired_and_interrupted_messages_cannot_execute() {
    let d = TempDir::new().unwrap();
    let r = app(config(&d)).unwrap();
    let c = contact(&r).await;
    let rejected = message(&r, &c, "reject").await;
    assert_eq!(
        call(
            &r,
            "POST",
            &route("messages", &rejected, "/decide"),
            A,
            json!({"decision":"reject","argument_hash":rejected["argument_hash"]})
        )
        .await
        .1["status"],
        "rejected"
    );
    assert_eq!(
        call(
            &r,
            "POST",
            &route("messages", &rejected, "/claim"),
            A,
            json!({})
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
    let interrupted = message(&r, &c, "interrupted").await;
    assert_eq!(approve(&r, &interrupted).await.0, StatusCode::OK);
    assert_eq!(
        call(
            &r,
            "POST",
            &route("messages", &interrupted, "/claim"),
            A,
            json!({})
        )
        .await
        .0,
        StatusCode::OK
    );
    let deleted = message(&r, &c, "deleted").await;
    assert_eq!(approve(&r, &deleted).await.0, StatusCode::OK);
    call(&r, "DELETE", &route("contacts", &c, ""), A, Value::Null).await;
    assert_eq!(
        call(
            &r,
            "POST",
            &route("messages", &deleted, "/claim"),
            A,
            json!({})
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
    drop(r);
    let r = app(config(&d)).unwrap();
    assert_eq!(
        call(
            &r,
            "GET",
            &route("messages", &interrupted, ""),
            A,
            Value::Null
        )
        .await
        .1["status"],
        "unknown"
    );
    assert_eq!(
        call(
            &r,
            "POST",
            &route("messages", &interrupted, "/claim"),
            A,
            json!({})
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
}

#[tokio::test]
async fn every_mutation_is_owner_scoped_and_evidence_rejects_unbound_fields() {
    let d = TempDir::new().unwrap();
    let r = app(config(&d)).unwrap();
    let rem = create_reminder(&r, "scope").await;
    let c = contact(&r).await;
    let m = message(&r, &c, "scope").await;
    let endpoints = vec![
        ("POST", route("reminders", &rem, "/cancel"), json!({})),
        ("POST", route("reminders", &rem, "/acknowledge"), json!({})),
        (
            "POST",
            route("reminders", &rem, "/delivery"),
            json!({"delivery_id":"fake","status":"announced"}),
        ),
        (
            "PUT",
            route("contacts", &c, ""),
            json!({"expected_version":c["version"],"display_name":"Name","email":"same@example.test"}),
        ),
        ("DELETE", route("contacts", &c, ""), Value::Null),
        (
            "POST",
            route("messages", &m, "/decide"),
            json!({"decision":"approve","argument_hash":m["argument_hash"]}),
        ),
        ("POST", route("messages", &m, "/claim"), json!({})),
        (
            "POST",
            route("messages", &m, "/result"),
            json!({"status":"unknown","evidence":{"provider":"gmail","recipient":c["email"],"provider_message_id":null,"delivery_status":"unverified"}}),
        ),
    ];
    for (method, path, payload) in endpoints {
        for token in [C, G, B] {
            assert_eq!(
                call(&r, method, &path, token, payload.clone()).await.0,
                if token == B {
                    StatusCode::NOT_FOUND
                } else {
                    StatusCode::FORBIDDEN
                },
                "{method} {path}"
            );
        }
    }
    let mut p = proposal(&c, "injected");
    p["recipient"] = json!("other@example.test");
    assert_eq!(
        call(&r, "POST", "/v1/messages", A, p).await.0,
        StatusCode::BAD_REQUEST
    );
    for (field, bad) in [
        ("subject", json!("header\r\nBcc:elsewhere")),
        ("subject", json!("x".repeat(201))),
        ("body", json!("x".repeat(4001))),
        ("body", json!("nul\u{0000}")),
    ] {
        let mut p = proposal(&c, "invalid");
        p[field] = bad;
        assert_eq!(
            call(&r, "POST", "/v1/messages", A, p).await.0,
            StatusCode::BAD_REQUEST
        );
    }
    assert_eq!(
        call(
            &r,
            "POST",
            "/v1/reminders/claim-due",
            A,
            json!({"owner_id":"elder"})
        )
        .await
        .0,
        StatusCode::BAD_REQUEST
    );
    let completion = json!({"status":"unknown","evidence":{"provider":"gmail","recipient":c["email"],"provider_message_id":null,"delivery_status":"unverified"}});
    assert_eq!(
        call(
            &r,
            "POST",
            &route("messages", &m, "/result"),
            A,
            completion.clone()
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
    assert_eq!(approve(&r, &m).await.0, StatusCode::OK);
    assert_eq!(
        call(&r, "POST", &route("messages", &m, "/claim"), A, json!({}))
            .await
            .0,
        StatusCode::OK
    );
    let mut extra = completion.clone();
    extra["evidence"]["raw_response"] = json!("untrusted provider text");
    assert_eq!(
        call(&r, "POST", &route("messages", &m, "/result"), A, extra)
            .await
            .0,
        StatusCode::BAD_REQUEST
    );
    assert_eq!(
        call(
            &r,
            "POST",
            &route("messages", &m, "/result"),
            A,
            completion.clone()
        )
        .await
        .0,
        StatusCode::OK
    );
    assert_eq!(
        call(
            &r,
            "POST",
            &route("messages", &m, "/result"),
            A,
            completion.clone()
        )
        .await
        .0,
        StatusCode::OK
    );
    let mut changed = completion;
    changed["status"] = json!("failed");
    assert_eq!(
        call(&r, "POST", &route("messages", &m, "/result"), A, changed)
            .await
            .0,
        StatusCode::CONFLICT
    );
    let caps = call(&r, "GET", "/v1/capabilities", A, Value::Null).await.1;
    assert_eq!(caps["capabilities"]["gmail_connected"], false);
    assert_eq!(caps["capabilities"]["email_proposals"], true);
}
