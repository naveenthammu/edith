use axum::{
    body::{to_bytes, Body},
    http::{Request, StatusCode},
    Router,
};
use edith_central_hub::{app, Config};
use serde_json::{json, Value};
use tempfile::TempDir;
use tower::ServiceExt;

const ALICE: &str = "alice-action-test-credential-000000000001";
const BOB: &str = "bob-action-test-credential-0000000000002";
const CHILD: &str = "child-action-test-credential-00000000003";
const GUEST: &str = "guest-action-test-credential-00000000004";
fn config(d: &TempDir) -> Value {
    json!({"database_path":d.path().join("hub.db"),"home_entities":["light.study","fan.bedroom","switch.table"],"principals":[
        {"id":"alice","token":ALICE,"role":"adult"},{"id":"bob","token":BOB,"role":"elder"},
        {"id":"child","token":CHILD,"role":"child"},{"id":"guest","token":GUEST,"role":"guest"}
    ]})
}
fn router(v: Value) -> Router {
    app(serde_json::from_value::<Config>(v).unwrap()).unwrap()
}
async fn call(
    app: &Router,
    method: &str,
    path: &str,
    token: Option<&str>,
    data: Value,
) -> (StatusCode, Value) {
    let mut r = Request::builder()
        .method(method)
        .uri(path)
        .header("content-type", "application/json");
    if let Some(token) = token {
        r = r.header("authorization", format!("Bearer {token}"));
    }
    let r = app
        .clone()
        .oneshot(
            r.body(Body::from(if data.is_null() {
                String::new()
            } else {
                data.to_string()
            }))
            .unwrap(),
        )
        .await
        .unwrap();
    let status = r.status();
    let bytes = to_bytes(r.into_body(), 100_000).await.unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}
fn proposal(key: &str, on: bool) -> Value {
    json!({"kind":"home.power","payload":{"entity_id":"light.study","on":on},"idempotency_key":key})
}
async fn prepare(app: &Router, key: &str) -> Value {
    let (s, v) = call(app, "POST", "/v1/actions", Some(ALICE), proposal(key, true)).await;
    assert_eq!(s, StatusCode::CREATED, "{v}");
    v
}
fn path(action: &Value, suffix: &str) -> String {
    format!("/v1/actions/{}/{}", action["id"].as_str().unwrap(), suffix)
}
async fn approve(app: &Router, action: &Value) -> Value {
    let (s, v) = call(
        app,
        "POST",
        &path(action, "decide"),
        Some(ALICE),
        json!({"decision":"approve","argument_hash":action["argument_hash"]}),
    )
    .await;
    assert_eq!(s, StatusCode::OK, "{v}");
    assert_eq!(v["status"], "approved");
    v
}
fn result(status: &str, observed: &str) -> Value {
    json!({"status":status,"evidence":{"entity_id":"light.study","requested_state":"on","observed_state":observed,"command_status":"accepted"}})
}

#[tokio::test]
async fn actions_require_owner_adult_and_explicit_server_entity_allowlist() {
    let d = TempDir::new().unwrap();
    let app = router(config(&d));
    assert_eq!(
        call(&app, "POST", "/v1/actions", None, proposal("first", true))
            .await
            .0,
        StatusCode::UNAUTHORIZED
    );
    for token in [CHILD, GUEST] {
        assert_eq!(
            call(
                &app,
                "POST",
                "/v1/actions",
                Some(token),
                proposal("first", true)
            )
            .await
            .0,
            StatusCode::FORBIDDEN
        );
        assert_eq!(
            call(&app, "GET", "/v1/actions", Some(token), Value::Null)
                .await
                .0,
            StatusCode::FORBIDDEN
        );
    }
    let a = prepare(&app, "first").await;
    assert_eq!(a["status"], "awaiting_approval");
    assert!(a["evidence"].is_null());
    assert_eq!(
        a["expires_at"].as_i64().unwrap() - a["created_at"].as_i64().unwrap(),
        300
    );
    assert_eq!(
        call(&app, "GET", "/v1/actions", Some(BOB), Value::Null)
            .await
            .1["actions"],
        json!([])
    );
    for token in [BOB, CHILD, GUEST] {
        for suffix in ["decide", "claim", "result"] {
            let (s, _) = call(
                &app,
                "POST",
                &path(&a, suffix),
                Some(token),
                json!({"decision":"approve","argument_hash":a["argument_hash"]}),
            )
            .await;
            assert!(
                s == StatusCode::NOT_FOUND || s == StatusCode::FORBIDDEN,
                "{token} {suffix}: {s}"
            );
        }
    }
    for body in [
        json!({"kind":"home.power","payload":{"entity_id":"light.other","on":true},"idempotency_key":"other"}),
        json!({"kind":"home.power","payload":{"entity_id":"lock.door","on":true},"idempotency_key":"other"}),
    ] {
        assert_eq!(
            call(&app, "POST", "/v1/actions", Some(ALICE), body).await.0,
            StatusCode::FORBIDDEN
        );
    }
    let mut cfg = config(&d);
    cfg["home_entities"] = json!([]);
    let restricted = router(cfg);
    assert_eq!(
        call(
            &restricted,
            "POST",
            "/v1/actions",
            Some(ALICE),
            proposal("empty", true)
        )
        .await
        .0,
        StatusCode::FORBIDDEN
    );
}

#[tokio::test]
async fn idempotency_key_replays_exact_arguments_and_rejects_changed_payloads() {
    let d = TempDir::new().unwrap();
    let app = router(config(&d));
    let a = prepare(&app, "same-key").await;
    let (s, b) = call(
        &app,
        "POST",
        "/v1/actions",
        Some(ALICE),
        proposal("same-key", true),
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(a, b);
    assert_eq!(
        call(
            &app,
            "POST",
            "/v1/actions",
            Some(ALICE),
            proposal("same-key", false)
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
    let (s, b) = call(
        &app,
        "POST",
        "/v1/actions",
        Some(BOB),
        proposal("same-key", true),
    )
    .await;
    assert_eq!(s, StatusCode::CREATED);
    assert_ne!(a["id"], b["id"]);
    for extra in [
        json!({"kind":"home.power","payload":{"entity_id":"light.study","on":"true"},"idempotency_key":"invalid"}),
        json!({"kind":"shell.run","payload":{"entity_id":"light.study","on":true},"idempotency_key":"invalid"}),
        json!({"kind":"home.power","payload":{"entity_id":"light.study","on":true,"script":"bad"},"idempotency_key":"invalid"}),
        json!({"kind":"home.power","payload":{"entity_id":"light.study","on":true},"idempotency_key":"invalid","principal":"bob"}),
    ] {
        assert_eq!(
            call(&app, "POST", "/v1/actions", Some(ALICE), extra)
                .await
                .0,
            StatusCode::BAD_REQUEST
        );
    }
}

#[tokio::test]
async fn fresh_exact_approval_is_required_and_rejection_cannot_be_claimed() {
    let d = TempDir::new().unwrap();
    let app = router(config(&d));
    let a = prepare(&app, "approval").await;
    assert_eq!(
        call(&app, "POST", &path(&a, "claim"), Some(ALICE), Value::Null)
            .await
            .0,
        StatusCode::CONFLICT
    );
    assert_eq!(
        call(
            &app,
            "POST",
            &path(&a, "decide"),
            Some(ALICE),
            json!({"decision":"approve","argument_hash":"different"})
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
    let approved = approve(&app, &a).await;
    assert_eq!(
        call(
            &app,
            "POST",
            &path(&a, "decide"),
            Some(ALICE),
            json!({"decision":"approve","argument_hash":a["argument_hash"]})
        )
        .await
        .1,
        approved
    );
    assert_eq!(
        call(
            &app,
            "POST",
            &path(&a, "decide"),
            Some(ALICE),
            json!({"decision":"reject","argument_hash":a["argument_hash"]})
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
    let rejected = prepare(&app, "rejected").await;
    let body = json!({"decision":"reject","argument_hash":rejected["argument_hash"]});
    let (s, first) = call(
        &app,
        "POST",
        &path(&rejected, "decide"),
        Some(ALICE),
        body.clone(),
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(first["status"], "rejected");
    assert_eq!(
        call(&app, "POST", &path(&rejected, "decide"), Some(ALICE), body)
            .await
            .1,
        first
    );
    assert_eq!(
        call(
            &app,
            "POST",
            &path(&rejected, "claim"),
            Some(ALICE),
            Value::Null
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
    let expired = prepare(&app, "expired").await;
    let db = rusqlite::Connection::open(d.path().join("hub.db")).unwrap();
    db.execute(
        "UPDATE actions SET expires_at=1 WHERE id=?1",
        [expired["id"].as_str().unwrap()],
    )
    .unwrap();
    assert_eq!(
        call(
            &app,
            "POST",
            &path(&expired, "decide"),
            Some(ALICE),
            json!({"decision":"approve","argument_hash":expired["argument_hash"]})
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
    db.execute(
        "UPDATE actions SET expires_at=1 WHERE id=?1",
        [a["id"].as_str().unwrap()],
    )
    .unwrap();
    assert_eq!(
        call(&app, "POST", &path(&a, "claim"), Some(ALICE), Value::Null)
            .await
            .0,
        StatusCode::CONFLICT
    );
}

#[tokio::test]
async fn concurrent_claims_have_one_winner_and_restart_marks_execution_unknown() {
    let d = TempDir::new().unwrap();
    let cfg = config(&d);
    let app = router(cfg.clone());
    let a = prepare(&app, "once").await;
    approve(&app, &a).await;
    let p = path(&a, "claim");
    let (first, second) = tokio::join!(
        call(&app, "POST", &p, Some(ALICE), Value::Null),
        call(&app, "POST", &p, Some(ALICE), Value::Null)
    );
    assert_eq!(
        [first.0, second.0]
            .iter()
            .filter(|&&s| s == StatusCode::OK)
            .count(),
        1
    );
    assert_eq!(
        [first.0, second.0]
            .iter()
            .filter(|&&s| s == StatusCode::CONFLICT)
            .count(),
        1
    );
    drop(app);
    let app = router(cfg);
    let (_, rows) = call(&app, "GET", "/v1/actions", Some(ALICE), Value::Null).await;
    assert_eq!(rows["actions"][0]["status"], "unknown");
    assert!(rows["actions"][0]["recovery_note"]
        .as_str()
        .unwrap()
        .contains("interrupted"));
    assert_eq!(
        call(&app, "POST", &p, Some(ALICE), Value::Null).await.0,
        StatusCode::CONFLICT
    );
}

#[tokio::test]
async fn results_need_execution_matching_state_and_immutable_typed_evidence() {
    let d = TempDir::new().unwrap();
    let cfg = config(&d);
    let app = router(cfg.clone());
    let a = prepare(&app, "result").await;
    approve(&app, &a).await;
    assert_eq!(
        call(
            &app,
            "POST",
            &path(&a, "result"),
            Some(ALICE),
            result("verified", "on")
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
    assert_eq!(
        call(&app, "POST", &path(&a, "claim"), Some(ALICE), Value::Null)
            .await
            .0,
        StatusCode::OK
    );
    for invalid in [
        result("verified", "off"),
        json!({"status":"verified","evidence":{"entity_id":"light.other","requested_state":"on","observed_state":"on","command_status":"accepted"}}),
        json!({"status":"verified","evidence":{"entity_id":"light.study","requested_state":"off","observed_state":"on","command_status":"accepted"}}),
        json!({"status":"verified","evidence":{"entity_id":"light.study","requested_state":"on","observed_state":"on","command_status":"rejected"}}),
        json!({"status":"verified","evidence":{"entity_id":"light.study","requested_state":"on","observed_state":"on","command_status":"accepted","token":"secret"}}),
    ] {
        assert_eq!(
            call(&app, "POST", &path(&a, "result"), Some(ALICE), invalid)
                .await
                .0,
            StatusCode::BAD_REQUEST
        );
    }
    let input = result("verified", "on");
    let (s, done) = call(
        &app,
        "POST",
        &path(&a, "result"),
        Some(ALICE),
        input.clone(),
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(done["status"], "verified");
    assert_eq!(
        call(&app, "POST", &path(&a, "result"), Some(ALICE), input)
            .await
            .1,
        done
    );
    assert_eq!(
        call(
            &app,
            "POST",
            &path(&a, "result"),
            Some(ALICE),
            result("failed", "off")
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
    drop(app);
    let app = router(cfg);
    assert_eq!(
        call(&app, "GET", "/v1/actions", Some(ALICE), Value::Null)
            .await
            .1["actions"][0],
        done
    );
}

#[test]
fn entity_configuration_accepts_only_exact_low_risk_entity_ids() {
    let d = TempDir::new().unwrap();
    for entity in [
        "light.*",
        "lock.door",
        "light.study;evil",
        "switch.",
        "fan.UPPER",
        "light.room.other",
    ] {
        let mut cfg = config(&d);
        cfg["home_entities"] = json!([entity]);
        assert!(
            app(serde_json::from_value(cfg).unwrap()).is_err(),
            "accepted {entity}"
        );
    }
}
