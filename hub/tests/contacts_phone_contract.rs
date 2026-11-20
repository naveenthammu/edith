use axum::{
    body::{to_bytes, Body},
    http::{Request, StatusCode},
    Router,
};
use edith_central_hub::{app, Config};
use serde_json::{json, Value};
use tempfile::TempDir;
use tower::ServiceExt;
const A: &str = "phone-owner-synthetic-credential-000001";
const B: &str = "phone-elder-synthetic-credential-000002";
const C: &str = "phone-child-synthetic-credential-000003";
fn config(d: &TempDir) -> Config {
    serde_json::from_value(json!({"database_path":d.path().join("hub.db"),"principals":[{"id":"owner","token":A,"role":"adult"},{"id":"other","token":B,"role":"elder"},{"id":"child","token":C,"role":"child"}]})).unwrap()
}
async fn call(
    r: &Router,
    method: &str,
    path: &str,
    token: &str,
    body: Value,
) -> (StatusCode, Value) {
    let req = Request::builder()
        .method(method)
        .uri(path)
        .header("content-type", "application/json")
        .header("authorization", format!("Bearer {token}"));
    let res = r
        .clone()
        .oneshot(req.body(Body::from(body.to_string())).unwrap())
        .await
        .unwrap();
    let status = res.status();
    let bytes = to_bytes(res.into_body(), 1_000_000).await.unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}
async fn phone(r: &Router) -> Value {
    let (s, c) = call(
        r,
        "POST",
        "/v1/contacts",
        A,
        json!({"display_name":"Friend","phone_e164":"+919876543210"}),
    )
    .await;
    assert_eq!(s, StatusCode::CREATED, "{c}");
    assert!(c["email"].is_null());
    c
}
fn path(c: &Value) -> String {
    format!("/v1/contacts/{}", c["id"].as_str().unwrap())
}

#[tokio::test]
async fn contacts_accept_phone_only_both_or_legacy_email_but_validate_every_supplied_channel() {
    let d = TempDir::new().unwrap();
    let r = app(config(&d)).unwrap();
    let c = phone(&r).await;
    assert_eq!(c["phone_e164"], "+919876543210");
    for data in [
        json!({"display_name":"Neither"}),
        json!({"display_name":"Neither","email":null,"phone_e164":null}),
        json!({"display_name":"Bad","phone_e164":"919876543210"}),
        json!({"display_name":"Bad","phone_e164":"+09123"}),
        json!({"display_name":"Bad","phone_e164":"+91 9876543210"}),
        json!({"display_name":"Bad","phone_e164":"+1234567890123456"}),
        json!({"display_name":"Bad","phone_e164":"+１２３"}),
        json!({"display_name":"Bad","phone_e164":"+919876543210","email":"bad"}),
        json!({"display_name":"Bad","email":"valid@example.test","phone_e164":""}),
    ] {
        assert_eq!(
            call(&r, "POST", "/v1/contacts", A, data).await.0,
            StatusCode::BAD_REQUEST
        );
    }
    for data in [
        json!({"display_name":"Email","email":"friend@example.test"}),
        json!({"display_name":"Both","email":"friend@example.test","phone_e164":"+919876543210"}),
    ] {
        assert_eq!(
            call(&r, "POST", "/v1/contacts", A, data).await.0,
            StatusCode::CREATED
        );
    }
}

#[tokio::test]
async fn phone_only_contact_rejects_email_proposal_without_creating_a_message() {
    let d = TempDir::new().unwrap();
    let r = app(config(&d)).unwrap();
    let c = phone(&r).await;
    let proposal = json!({"contact_id":c["id"],"contact_version":c["version"],"subject":"Hello","body":"Synthetic text","idempotency_key":"no-email"});
    assert_eq!(
        call(&r, "POST", "/v1/messages", A, proposal).await.0,
        StatusCode::BAD_REQUEST
    );
    assert!(
        call(&r, "GET", "/v1/messages", A, Value::Null).await.1["messages"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    let caps = call(&r, "GET", "/v1/capabilities", A, Value::Null).await.1;
    assert_eq!(caps["capabilities"]["whatsapp_business"], "not_configured");
    assert_eq!(caps["capabilities"]["whatsapp_api_connected"], false);
}

#[tokio::test]
async fn full_contact_replacement_advances_version_and_invalidates_old_email_approval() {
    let d = TempDir::new().unwrap();
    let r = app(config(&d)).unwrap();
    let (_, c) = call(
        &r,
        "POST",
        "/v1/contacts",
        A,
        json!({"display_name":"Both","email":"friend@example.test","phone_e164":"+919876543210"}),
    )
    .await;
    assert!(c["id"].is_string());
    let(s,m)=call(&r,"POST","/v1/messages",A,json!({"contact_id":c["id"],"contact_version":c["version"],"subject":"Hello","body":"Synthetic text","idempotency_key":"old-version"})).await;
    assert_eq!(s, StatusCode::CREATED);
    let update = json!({"expected_version":c["version"],"display_name":"Both","email":"friend@example.test","phone_e164":"+12025550100"});
    let (s, changed) = call(&r, "PUT", &path(&c), A, update.clone()).await;
    assert_eq!(s, StatusCode::OK);
    assert_ne!(changed["version"], c["version"]);
    assert_eq!(
        call(&r, "PUT", &path(&c), A, update).await.0,
        StatusCode::CONFLICT
    );
    assert_eq!(
        call(
            &r,
            "POST",
            &format!("/v1/messages/{}/decide", m["id"].as_str().unwrap()),
            A,
            json!({"decision":"approve","argument_hash":m["argument_hash"]})
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
    let(s,phone_only)=call(&r,"PUT",&path(&c),A,json!({"expected_version":changed["version"],"display_name":"Phone","phone_e164":"+12025550100"})).await;
    assert_eq!(s, StatusCode::OK);
    assert!(phone_only["email"].is_null());
    assert_eq!(phone_only["phone_e164"], "+12025550100");
    drop(r);
    let r = app(config(&d)).unwrap();
    assert_eq!(
        call(&r, "GET", "/v1/contacts", A, Value::Null).await.1["contacts"][0],
        phone_only
    );
}

#[tokio::test]
async fn legacy_schema_five_contacts_migrate_without_changing_revision_or_email() {
    let d = TempDir::new().unwrap();
    let file = d.path().join("hub.db");
    let legacy = json!({"id":"11111111-1111-4111-8111-111111111111","display_name":"Legacy","email":"legacy@example.test","version":"a".repeat(64),"created_at":1,"updated_at":1});
    {
        let db = rusqlite::Connection::open(&file).unwrap();
        db.execute_batch("CREATE TABLE contacts(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,data TEXT NOT NULL CHECK(json_valid(data)));PRAGMA user_version=5;").unwrap();
        db.execute(
            "INSERT INTO contacts VALUES(?1,'owner',?2)",
            rusqlite::params![legacy["id"].as_str().unwrap(), legacy.to_string()],
        )
        .unwrap();
    }
    let r = app(config(&d)).unwrap();
    let c = call(&r, "GET", "/v1/contacts", A, Value::Null).await.1["contacts"][0].clone();
    assert_eq!(c["version"], legacy["version"]);
    assert_eq!(c["email"], legacy["email"]);
    assert!(c["phone_e164"].is_null());
    assert_eq!(call(&r,"POST","/v1/messages",A,json!({"contact_id":c["id"],"contact_version":c["version"],"subject":"Hello","body":"Still valid","idempotency_key":"migrated"})).await.0,StatusCode::CREATED);
    drop(r);
    let db = rusqlite::Connection::open(file).unwrap();
    assert_eq!(
        db.pragma_query_value(None, "user_version", |r| r.get::<_, i64>(0))
            .unwrap(),
        8
    );
}

#[tokio::test]
async fn phone_fields_do_not_weaken_owner_or_child_isolation() {
    let d = TempDir::new().unwrap();
    let r = app(config(&d)).unwrap();
    let c = phone(&r).await;
    assert!(
        call(&r, "GET", "/v1/contacts", B, Value::Null).await.1["contacts"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    let update =
        json!({"expected_version":c["version"],"display_name":"Other","phone_e164":"+12025550100"});
    assert_eq!(
        call(&r, "PUT", &path(&c), B, update.clone()).await.0,
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        call(&r, "PUT", &path(&c), C, update).await.0,
        StatusCode::FORBIDDEN
    );
    assert_eq!(
        call(
            &r,
            "POST",
            "/v1/contacts",
            C,
            json!({"display_name":"Phone","phone_e164":"+12025550100"})
        )
        .await
        .0,
        StatusCode::FORBIDDEN
    );
}
