use axum::{
    body::{to_bytes, Body},
    http::{Request, StatusCode},
    Router,
};
use edith_central_hub::{app, Config};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use tempfile::TempDir;
use tower::ServiceExt;
const A: &str = "business-owner-synthetic-credential-001";
const B: &str = "business-other-synthetic-credential-002";
const C: &str = "business-child-synthetic-credential-003";
const W: &str = "business-worker-synthetic-credential-001";
const W2: &str = "business-second-worker-credential-002";
fn config(d: &TempDir, workers: bool) -> Config {
    let mut v = json!({"database_path":d.path().join("hub.db"),"principals":[{"id":"owner","token":A,"role":"adult"},{"id":"other","token":B,"role":"elder"},{"id":"child","token":C,"role":"child"}]});
    if workers {
        v["whatsapp_workers"] = json!([{"id":"worker","principal_id":"owner","connection_id":"business","waba_id":"111","phone_number_id":"222","token":W},{"id":"second-worker","principal_id":"owner","connection_id":"other-business","waba_id":"333","phone_number_id":"444","token":W2}]);
    }
    serde_json::from_value(v).expect("Worker configuration should be supported")
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
fn snapshot() -> Value {
    let mut template = json!({"id":"555","name":"pickup_update","language":"en_US","status":"APPROVED","category":"UTILITY","parameter_format":"POSITIONAL","body":"Hello {{1}}, order {{2}} is ready."});
    let hash = format!("{:x}", Sha256::digest(template.to_string().as_bytes()));
    template["content_hash"] = json!(hash);
    json!({"account":{"connection_id":"business","principal_id":"owner","waba_id":"111","phone_number_id":"222","display_phone_number":"+16505551234","verified_name":"Synthetic Shop","version":"a".repeat(64),"verified_at":now()},"template":template})
}
async fn binding(r: &Router) -> Value {
    let (s, v) = call(r, "POST", "/v1/whatsapp-worker/bindings", W, snapshot()).await;
    assert!(s == StatusCode::CREATED || s == StatusCode::OK, "{s} {v}");
    v
}
async fn contact(r: &Router) -> Value {
    let (s, v) = call(
        r,
        "POST",
        "/v1/contacts",
        A,
        json!({"display_name":"Friend","phone_e164":"+16505550000"}),
    )
    .await;
    assert_eq!(s, StatusCode::CREATED);
    v
}
fn permission(c: &Value) -> Value {
    json!({"connection_id":"business","account_version":"a".repeat(64),"contact_id":c["id"],"contact_version":c["version"],"expected_version":null,"status":"granted","allowed_categories":["UTILITY"],"recorded_at":now(),"expires_at":now()+3600,"source":"explicit_operator_record","evidence_reference":"synthetic-explicit-opt-in","source_note":"Synthetic test evidence only"})
}
async fn granted(r: &Router, c: &Value) -> Value {
    let (s, v) = call(
        r,
        "POST",
        "/v1/whatsapp-worker/permissions",
        W,
        permission(c),
    )
    .await;
    assert!(s == StatusCode::CREATED || s == StatusCode::OK, "{s} {v}");
    v
}
fn proposal(b: &Value, c: &Value, p: &Value, key: &str) -> Value {
    json!({"binding_id":b["id"],"binding_version":b["version"],"contact_id":c["id"],"contact_version":c["version"],"permission_version":p["version"],"parameters":["Asha","42"],"idempotency_key":key})
}
fn path(v: &Value, suffix: &str, worker: bool) -> String {
    format!(
        "/v1/{}/{}{}",
        if worker {
            "whatsapp-worker/messages"
        } else {
            "business-messages"
        },
        v["id"].as_str().unwrap(),
        suffix
    )
}
async fn proposed(r: &Router, key: &str) -> (Value, Value, Value, Value) {
    let b = binding(r).await;
    let c = contact(r).await;
    let p = granted(r, &c).await;
    let (s, v) = call(
        r,
        "POST",
        "/v1/business-messages",
        A,
        proposal(&b, &c, &p, key),
    )
    .await;
    assert_eq!(s, StatusCode::CREATED, "{v}");
    (v, b, c, p)
}
async fn claim(r: &Router, v: &Value) -> Value {
    let (s, _) = call(
        r,
        "POST",
        &path(v, "/decide", false),
        A,
        json!({"decision":"approve","argument_hash":v["argument_hash"]}),
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    let (s, v) = call(
        r,
        "POST",
        &path(v, "/claim", true),
        W,
        json!({"argument_hash":v["argument_hash"]}),
    )
    .await;
    assert_eq!(s, StatusCode::OK, "{v}");
    v
}
fn result() -> Value {
    json!({"status":"accepted","evidence":{"provider":"whatsapp_business","phone_number_id":"222","waba_id":"111","account_version":"a".repeat(64),"template_content_hash":snapshot()["template"]["content_hash"],"recipient_e164":"+16505550000","provider_message_id":"wamid.synthetic","delivery_status":"unverified"}})
}

#[tokio::test]
async fn worker_authority_is_a_distinct_credential_domain() {
    let d = TempDir::new().unwrap();
    let r = app(config(&d, false)).unwrap();
    assert_eq!(
        call(&r, "GET", "/v1/whatsapp-worker/capabilities", A, json!({}))
            .await
            .0,
        StatusCode::UNAUTHORIZED
    );
    let d = TempDir::new().unwrap();
    let r = app(config(&d, true)).unwrap();
    let (s, caps) = call(&r, "GET", "/v1/whatsapp-worker/capabilities", W, json!({})).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(caps["principal_id"], "owner");
    assert_eq!(caps["connection_id"], "business");
    for p in [
        "/v1/capabilities",
        "/v1/contacts",
        "/v1/documents",
        "/v1/actions",
        "/v1/commerce",
        "/v1/business-messages",
    ] {
        assert_eq!(
            call(&r, "GET", p, W, json!({})).await.0,
            StatusCode::UNAUTHORIZED,
            "{p}"
        );
    }
    assert_eq!(
        call(&r, "POST", "/v1/whatsapp-worker/bindings", A, snapshot())
            .await
            .0,
        StatusCode::UNAUTHORIZED
    );
    assert_eq!(
        call(&r, "GET", "/v1/whatsapp-bindings", C, json!({}))
            .await
            .0,
        StatusCode::FORBIDDEN
    );
}

#[tokio::test]
async fn worker_binding_is_exact_scoped_and_template_content_is_recomputed() {
    let d = TempDir::new().unwrap();
    let r = app(config(&d, true)).unwrap();
    let b = binding(&r).await;
    assert_eq!(
        call(&r, "POST", "/v1/whatsapp-worker/bindings", W, snapshot())
            .await
            .1["version"],
        b["version"]
    );
    assert_eq!(
        call(&r, "GET", "/v1/whatsapp-bindings", B, json!({}))
            .await
            .1,
        json!({"bindings":[]})
    );
    assert_eq!(
        call(
            &r,
            "GET",
            &format!("/v1/whatsapp-bindings/{}", b["id"].as_str().unwrap()),
            B,
            json!({})
        )
        .await
        .0,
        StatusCode::NOT_FOUND
    );
    for (ptr, val) in [
        ("/account/principal_id", json!("other")),
        ("/account/phone_number_id", json!("333")),
        ("/account/waba_id", json!("444")),
        ("/account/connection_id", json!("different")),
        ("/template/content_hash", json!("b".repeat(64))),
        ("/template/status", json!("PAUSED")),
        ("/template/body", json!("Changed {{1}} {{2}}")),
    ] {
        let mut bad = snapshot();
        *bad.pointer_mut(ptr).unwrap() = val;
        assert!(
            call(&r, "POST", "/v1/whatsapp-worker/bindings", W, bad)
                .await
                .0
                .is_client_error(),
            "{ptr}"
        );
    }
    assert_eq!(
        call(&r, "POST", "/v1/whatsapp-worker/bindings", W2, snapshot())
            .await
            .0,
        StatusCode::FORBIDDEN
    );
}

#[tokio::test]
async fn permission_requires_current_phone_contact_explicit_evidence_and_compare_and_swap() {
    let d = TempDir::new().unwrap();
    let r = app(config(&d, true)).unwrap();
    let b = binding(&r).await;
    let c = contact(&r).await;
    assert_eq!(
        call(
            &r,
            "POST",
            "/v1/business-messages",
            A,
            proposal(&b, &c, &json!({"version":"a".repeat(64)}), "no-permission")
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
    let p = granted(&r, &c).await;
    assert_eq!(p["recipient_e164"], "+16505550000");
    assert_eq!(
        call(
            &r,
            "POST",
            "/v1/whatsapp-worker/permissions",
            W,
            permission(&c)
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
    for (k, val) in [
        ("source", json!("contact_exists")),
        ("recorded_at", json!(now() - 301)),
        ("expires_at", json!(now() + 367 * 86400)),
        ("allowed_categories", json!(["UTILITY", "UTILITY"])),
        ("evidence_reference", json!("")),
    ] {
        let mut bad = permission(&c);
        bad["expected_version"] = p["version"].clone();
        bad[k] = val;
        assert_eq!(
            call(&r, "POST", "/v1/whatsapp-worker/permissions", W, bad)
                .await
                .0,
            StatusCode::BAD_REQUEST,
            "{k}"
        );
    }
    let mut revoke = permission(&c);
    revoke["expected_version"] = p["version"].clone();
    revoke["status"] = json!("revoked");
    let (s, new) = call(
        &r,
        "POST",
        "/v1/whatsapp-worker/permissions",
        W,
        revoke.clone(),
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    assert_ne!(new["version"], p["version"]);
    assert_eq!(
        call(&r, "POST", "/v1/whatsapp-worker/permissions", W, revoke)
            .await
            .0,
        StatusCode::CONFLICT
    );
    assert_eq!(
        call(&r, "GET", "/v1/whatsapp-permissions", B, json!({}))
            .await
            .1,
        json!({"permissions":[]})
    );
}

#[tokio::test]
async fn immutable_proposals_render_only_server_template_and_bind_exact_revisions() {
    let d = TempDir::new().unwrap();
    let r = app(config(&d, true)).unwrap();
    let (v, b, c, p) = proposed(&r, "exact").await;
    assert_eq!(
        v["payload"]["prepared"]["rendered_text"],
        "Hello Asha, order 42 is ready."
    );
    assert_eq!(v["payload"]["recipient_name"], "Friend");
    assert_eq!(v["kind"], "contact.whatsapp_template");
    let request = proposal(&b, &c, &p, "exact");
    assert_eq!(
        call(&r, "POST", "/v1/business-messages", A, request.clone()).await,
        (StatusCode::OK, v.clone())
    );
    let mut changed = request.clone();
    changed["parameters"] = json!(["Different", "42"]);
    assert_eq!(
        call(&r, "POST", "/v1/business-messages", A, changed)
            .await
            .0,
        StatusCode::CONFLICT
    );
    for key in ["recipient_e164", "rendered_text", "prepared", "account"] {
        let mut bad = request.clone();
        bad[key] = json!("invented");
        assert_eq!(
            call(&r, "POST", "/v1/business-messages", A, bad).await.0,
            StatusCode::BAD_REQUEST
        );
    }
    for params in [
        json!([]),
        json!(["Asha"]),
        json!(["Asha", "{{1}}"]),
        json!(["Asha", "bad\ntext"]),
    ] {
        let mut bad = request.clone();
        bad["idempotency_key"] = json!("bad");
        bad["parameters"] = params;
        assert_eq!(
            call(&r, "POST", "/v1/business-messages", A, bad).await.0,
            StatusCode::BAD_REQUEST
        );
    }
    assert_eq!(
        call(&r, "GET", &path(&v, "", false), B, json!({})).await.0,
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        call(
            &r,
            "POST",
            &path(&v, "/decide", false),
            A,
            json!({"decision":"approve","argument_hash":"bad"})
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
    assert_eq!(
        call(
            &r,
            "POST",
            &path(&v, "/claim", true),
            A,
            json!({"argument_hash":v["argument_hash"]})
        )
        .await
        .0,
        StatusCode::UNAUTHORIZED
    );
}

#[tokio::test]
async fn single_claim_current_authorization_revocation_and_historical_receipt_are_distinct() {
    let d = TempDir::new().unwrap();
    let r = app(config(&d, true)).unwrap();
    let (v, _, c, p) = proposed(&r, "claim").await;
    assert_eq!(
        call(&r, "POST", &path(&v, "/result", true), W, result())
            .await
            .0,
        StatusCode::CONFLICT
    );
    assert_eq!(
        call(
            &r,
            "POST",
            &path(&v, "/decide", false),
            A,
            json!({"decision":"approve","argument_hash":v["argument_hash"]})
        )
        .await
        .0,
        StatusCode::OK
    );
    let claimpath = path(&v, "/claim", true);
    let arg = json!({"argument_hash":v["argument_hash"]});
    let (a, b) = tokio::join!(
        call(&r, "POST", &claimpath, W, arg.clone()),
        call(&r, "POST", &claimpath, W, arg.clone())
    );
    let mut codes = vec![a.0.as_u16(), b.0.as_u16()];
    codes.sort();
    assert_eq!(codes, vec![200, 409]);
    let (s, authorized) = call(&r, "POST", &path(&v, "/authorize", true), W, arg.clone()).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(authorized["authorized"], true);
    assert_eq!(authorized["message"]["status"], "executing");
    assert_eq!(
        call(&r, "POST", &path(&v, "/authorize", true), W2, arg.clone())
            .await
            .0,
        StatusCode::NOT_FOUND
    );
    let mut revoke = permission(&c);
    revoke["expected_version"] = p["version"].clone();
    revoke["status"] = json!("revoked");
    assert_eq!(
        call(&r, "POST", "/v1/whatsapp-worker/permissions", W, revoke)
            .await
            .0,
        StatusCode::OK
    );
    assert_eq!(
        call(&r, "POST", &path(&v, "/authorize", true), W, arg)
            .await
            .0,
        StatusCode::CONFLICT
    );
    assert_eq!(
        call(&r, "POST", &path(&v, "/result", true), W2, result())
            .await
            .0,
        StatusCode::NOT_FOUND
    );
    let (s, done) = call(&r, "POST", &path(&v, "/result", true), W, result()).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(done["status"], "accepted");
    assert_eq!(done["evidence"]["delivery_status"], "unverified");
    assert_eq!(
        call(&r, "POST", &path(&v, "/result", true), W, result())
            .await
            .0,
        StatusCode::OK
    );
    let mut changed = result();
    changed["evidence"]["provider_message_id"] = json!("wamid.changed");
    assert_eq!(
        call(&r, "POST", &path(&v, "/result", true), W, changed)
            .await
            .0,
        StatusCode::CONFLICT
    );
}

#[tokio::test]
async fn interrupted_send_becomes_unknown_and_never_reclaims_after_restart() {
    let d = TempDir::new().unwrap();
    let r = app(config(&d, true)).unwrap();
    let (v, _, _, _) = proposed(&r, "restart").await;
    claim(&r, &v).await;
    drop(r);
    let r = app(config(&d, true)).unwrap();
    let (s, recovered) = call(&r, "GET", &path(&v, "", false), A, json!({})).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(recovered["status"], "unknown");
    assert!(recovered["evidence"].is_null());
    for suffix in ["/claim", "/authorize"] {
        assert_eq!(
            call(
                &r,
                "POST",
                &path(&v, suffix, true),
                W,
                json!({"argument_hash":v["argument_hash"]})
            )
            .await
            .0,
            StatusCode::CONFLICT
        );
    }
}

#[test]
fn worker_configuration_rejects_token_overlap_duplicate_scope_and_unauthorized_principals() {
    let d = TempDir::new().unwrap();
    for change in 0..6 {
        let mut cfg = config(&d, true);
        match change {
            0 => cfg.whatsapp_workers[0].token = A.into(),
            1 => cfg.whatsapp_workers[0].principal_id = "child".into(),
            2 => cfg.whatsapp_workers[0].waba_id = "../111".into(),
            3 => cfg.whatsapp_workers[0].connection_id = "arbitrary/path".into(),
            4 => cfg.principals[0].enabled = false,
            _ => {
                cfg.whatsapp_workers[1].connection_id = "business".into();
            }
        }
        assert!(cfg.validate().is_err(), "case {change}");
    }
}

#[tokio::test]
async fn disabled_workers_revoke_old_binding_approval_without_exposing_other_api_authority() {
    let d = TempDir::new().unwrap();
    let r = app(config(&d, true)).unwrap();
    let (v, b, c, p) = proposed(&r, "disabled").await;
    drop(r);
    let mut cfg = config(&d, true);
    for w in &mut cfg.whatsapp_workers {
        w.enabled = false;
    }
    let r = app(cfg).unwrap();
    assert_eq!(
        call(&r, "GET", "/v1/whatsapp-worker/capabilities", W, json!({}))
            .await
            .0,
        StatusCode::UNAUTHORIZED
    );
    assert_eq!(
        call(&r, "GET", "/v1/capabilities", A, json!({})).await.1["capabilities"]
            ["business_message_proposals"],
        false
    );
    assert_eq!(
        call(
            &r,
            "POST",
            &path(&v, "/decide", false),
            A,
            json!({"decision":"approve","argument_hash":v["argument_hash"]})
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
    assert_eq!(
        call(
            &r,
            "POST",
            "/v1/business-messages",
            A,
            proposal(&b, &c, &p, "new-after-disable")
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
}

#[tokio::test]
async fn contact_and_template_changes_invalidate_authorization_while_receipts_stay_bound() {
    let d = TempDir::new().unwrap();
    let r = app(config(&d, true)).unwrap();
    let (v, _, c, _) = proposed(&r, "contact-change").await;
    claim(&r, &v).await;
    let update = json!({"expected_version":c["version"],"display_name":"Changed contact","phone_e164":"+16505559999"});
    assert_eq!(
        call(
            &r,
            "PUT",
            &format!("/v1/contacts/{}", c["id"].as_str().unwrap()),
            A,
            update
        )
        .await
        .0,
        StatusCode::OK
    );
    assert_eq!(
        call(
            &r,
            "POST",
            &path(&v, "/authorize", true),
            W,
            json!({"argument_hash":v["argument_hash"]})
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
    for (pointer, value) in [
        ("/evidence/phone_number_id", json!("444")),
        ("/evidence/waba_id", json!("333")),
        ("/evidence/account_version", json!("b".repeat(64))),
        ("/evidence/recipient_e164", json!("+16505559999")),
        ("/evidence/template_content_hash", json!("b".repeat(64))),
        ("/evidence/provider_message_id", Value::Null),
    ] {
        let mut bad = result();
        *bad.pointer_mut(pointer).unwrap() = value;
        assert_eq!(
            call(&r, "POST", &path(&v, "/result", true), W, bad).await.0,
            StatusCode::BAD_REQUEST,
            "{pointer}"
        );
    }
    let mut failed = result();
    failed["status"] = json!("failed");
    assert_eq!(
        call(&r, "POST", &path(&v, "/result", true), W, failed)
            .await
            .0,
        StatusCode::BAD_REQUEST
    );
    assert_eq!(
        call(&r, "POST", &path(&v, "/result", true), A, result())
            .await
            .0,
        StatusCode::UNAUTHORIZED
    );
    assert_eq!(
        call(&r, "POST", &path(&v, "/result", true), W, result())
            .await
            .0,
        StatusCode::OK
    );
    let (v, _, _, _) = proposed(&r, "template-change").await;
    let mut changed = snapshot();
    changed["template"]["body"] = json!("Changed content {{1}} {{2}}.");
    changed["template"]
        .as_object_mut()
        .unwrap()
        .remove("content_hash");
    changed["template"]["content_hash"] = json!(format!(
        "{:x}",
        Sha256::digest(changed["template"].to_string().as_bytes())
    ));
    assert_eq!(
        call(&r, "POST", "/v1/whatsapp-worker/bindings", W, changed)
            .await
            .0,
        StatusCode::OK
    );
    assert_eq!(
        call(
            &r,
            "POST",
            &path(&v, "/decide", false),
            A,
            json!({"decision":"approve","argument_hash":v["argument_hash"]})
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
}

#[tokio::test]
async fn permission_requires_explicit_nullable_cas_field_and_category_scope() {
    let d = TempDir::new().unwrap();
    let r = app(config(&d, true)).unwrap();
    let b = binding(&r).await;
    let c = contact(&r).await;
    let mut missing = permission(&c);
    missing.as_object_mut().unwrap().remove("expected_version");
    assert_eq!(
        call(&r, "POST", "/v1/whatsapp-worker/permissions", W, missing)
            .await
            .0,
        StatusCode::BAD_REQUEST
    );
    let mut marketing = permission(&c);
    marketing["allowed_categories"] = json!(["MARKETING"]);
    let (s, p) = call(&r, "POST", "/v1/whatsapp-worker/permissions", W, marketing).await;
    assert_eq!(s, StatusCode::CREATED);
    assert_eq!(
        call(
            &r,
            "POST",
            "/v1/business-messages",
            A,
            proposal(&b, &c, &p, "category-mismatch")
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
}

#[tokio::test]
async fn retargeted_worker_cannot_read_old_sender_bindings_messages_or_permission_proof() {
    let d = TempDir::new().unwrap();
    let r = app(config(&d, true)).unwrap();
    let (message, binding, contact, permission_record) = proposed(&r, "old-sender").await;
    drop(r);
    let mut cfg = config(&d, true);
    let new_token = "retargeted-worker-synthetic-credential-003";
    cfg.whatsapp_workers[0].waba_id = "999".into();
    cfg.whatsapp_workers[0].phone_number_id = "888".into();
    cfg.whatsapp_workers[0].token = new_token.into();
    let r = app(cfg).unwrap();
    for (route, key) in [
        ("bindings", "bindings"),
        ("messages", "messages"),
        ("permissions", "permissions"),
    ] {
        let (status, value) = call(
            &r,
            "GET",
            &format!("/v1/whatsapp-worker/{route}"),
            new_token,
            json!({}),
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(value[key], json!([]), "old sender leaked through {route}");
    }
    assert_eq!(
        call(
            &r,
            "GET",
            &format!(
                "/v1/whatsapp-worker/bindings/{}",
                binding["id"].as_str().unwrap()
            ),
            new_token,
            json!({})
        )
        .await
        .0,
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        call(&r, "GET", &path(&message, "", true), new_token, json!({}))
            .await
            .0,
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        call(&r, "GET", &path(&message, "", false), A, json!({}))
            .await
            .0,
        StatusCode::OK
    );
    let mut changed = snapshot();
    changed["account"]["waba_id"] = json!("999");
    changed["account"]["phone_number_id"] = json!("888");
    changed["account"]["version"] = json!("b".repeat(64));
    changed["account"]["verified_at"] = json!(now() + 1);
    assert_eq!(
        call(
            &r,
            "POST",
            "/v1/whatsapp-worker/bindings",
            new_token,
            changed
        )
        .await
        .0,
        StatusCode::OK
    );
    assert_eq!(
        call(
            &r,
            "GET",
            "/v1/whatsapp-worker/permissions",
            new_token,
            json!({})
        )
        .await
        .1,
        json!({"permissions":[]})
    );
    assert_eq!(
        call(
            &r,
            "GET",
            "/v1/whatsapp-worker/messages",
            new_token,
            json!({})
        )
        .await
        .1,
        json!({"messages":[]})
    );
    let mut new_permission = permission(&contact);
    new_permission["account_version"] = json!("b".repeat(64));
    new_permission["expected_version"] = permission_record["version"].clone();
    new_permission["evidence_reference"] = json!("new-sender-explicit-evidence");
    let (status, current) = call(
        &r,
        "POST",
        "/v1/whatsapp-worker/permissions",
        new_token,
        new_permission,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        call(
            &r,
            "GET",
            "/v1/whatsapp-worker/permissions",
            new_token,
            json!({})
        )
        .await
        .1,
        json!({"permissions":[current]})
    );
}

#[tokio::test]
async fn approved_unclaimed_message_can_be_rejected_once_and_never_reapproved() {
    let d = TempDir::new().unwrap();
    let r = app(config(&d, true)).unwrap();
    let (v, _, _, _) = proposed(&r, "reject-approved").await;
    let approve = json!({"decision":"approve","argument_hash":v["argument_hash"]});
    let reject = json!({"decision":"reject","argument_hash":v["argument_hash"]});
    assert_eq!(
        call(&r, "POST", &path(&v, "/decide", false), A, approve.clone())
            .await
            .0,
        StatusCode::OK
    );
    let (status, rejected) = call(&r, "POST", &path(&v, "/decide", false), A, reject.clone()).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(rejected["status"], "rejected");
    assert_eq!(
        call(&r, "POST", &path(&v, "/decide", false), A, reject).await,
        (StatusCode::OK, rejected)
    );
    assert_eq!(
        call(&r, "POST", &path(&v, "/decide", false), A, approve)
            .await
            .0,
        StatusCode::CONFLICT
    );
    assert_eq!(
        call(
            &r,
            "POST",
            &path(&v, "/claim", true),
            W,
            json!({"argument_hash":v["argument_hash"]})
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
}

#[tokio::test]
async fn approval_rejection_racing_execution_claim_has_exactly_one_winner() {
    let d = TempDir::new().unwrap();
    let r = app(config(&d, true)).unwrap();
    let (v, _, _, _) = proposed(&r, "cancel-race").await;
    call(
        &r,
        "POST",
        &path(&v, "/decide", false),
        A,
        json!({"decision":"approve","argument_hash":v["argument_hash"]}),
    )
    .await;
    let decision_path = path(&v, "/decide", false);
    let claim_path = path(&v, "/claim", true);
    let (reject, claimed) = tokio::join!(
        call(
            &r,
            "POST",
            &decision_path,
            A,
            json!({"decision":"reject","argument_hash":v["argument_hash"]})
        ),
        call(
            &r,
            "POST",
            &claim_path,
            W,
            json!({"argument_hash":v["argument_hash"]})
        )
    );
    assert_eq!(
        [reject.0, claimed.0]
            .into_iter()
            .filter(|s| *s == StatusCode::OK)
            .count(),
        1
    );
    assert_eq!(
        [reject.0, claimed.0]
            .into_iter()
            .filter(|s| *s == StatusCode::CONFLICT)
            .count(),
        1
    );
    let (_, current) = call(&r, "GET", &path(&v, "", false), A, json!({})).await;
    assert_eq!(
        current["status"],
        if reject.0 == StatusCode::OK {
            "rejected"
        } else {
            "executing"
        }
    );
    let (other, _, _, _) = proposed(&r, "cannot-cancel-executing").await;
    claim(&r, &other).await;
    assert_eq!(
        call(
            &r,
            "POST",
            &path(&other, "/decide", false),
            A,
            json!({"decision":"reject","argument_hash":other["argument_hash"]})
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
}
