use axum::{
    body::{to_bytes, Body},
    http::{Request, StatusCode},
    Router,
};
use edith_central_hub::{app, Config};
use serde_json::{json, Value};
use tempfile::TempDir;
use tower::ServiceExt;

const ALICE: &str = "alice-test-token-not-real-000000000000001";
const BOB: &str = "bob-test-token-not-real-00000000000000002";
const CHILD: &str = "child-test-token-not-real-000000000000003";
const GUEST: &str = "guest-test-token-not-real-000000000000004";
const ELDER: &str = "elder-test-token-not-real-000000000000005";
const DISABLED: &str = "disabled-test-token-not-real-000000000006";

fn config(dir: &TempDir) -> Value {
    json!({"database_path":dir.path().join("knowledge.db"), "bind":"127.0.0.1:8788", "principals":[
        {"id":"alice","token":ALICE,"role":"adult"},
        {"id":"bob","token":BOB,"role":"adult"},
        {"id":"child","token":CHILD,"role":"child"},
        {"id":"guest","token":GUEST,"role":"guest"},
        {"id":"elder","token":ELDER,"role":"elder"},
        {"id":"disabled","token":DISABLED,"role":"adult","enabled":false}
    ]})
}
fn router(value: Value) -> Router {
    app(serde_json::from_value::<Config>(value).unwrap()).unwrap()
}

async fn call(
    app: &Router,
    method: &str,
    path: &str,
    token: Option<&str>,
    value: Value,
) -> (StatusCode, Value) {
    let mut request = Request::builder()
        .method(method)
        .uri(path)
        .header("content-type", "application/json");
    if let Some(token) = token {
        request = request.header("authorization", format!("Bearer {token}"));
    }
    let response = app
        .clone()
        .oneshot(
            request
                .body(Body::from(if value.is_null() {
                    String::new()
                } else {
                    value.to_string()
                }))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = response.status();
    let bytes = to_bytes(response.into_body(), 3_000_000).await.unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}
async fn create(
    app: &Router,
    token: &str,
    title: &str,
    content: &str,
    visibility: &str,
    child_allowed: bool,
) -> Value {
    let (status,result)=call(app,"POST","/v1/documents",Some(token),json!({"title":title,"content":content,"visibility":visibility,"child_allowed":child_allowed})).await;
    assert_eq!(status, StatusCode::CREATED, "{result}");
    result
}
async fn search(app: &Router, token: &str, query: &str) -> Value {
    let (status, result) = call(
        app,
        "POST",
        "/v1/search",
        Some(token),
        json!({"query":query}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{result}");
    result["citations"].clone()
}

#[tokio::test]
async fn authenticated_principals_cannot_be_forged_or_disabled() {
    let d = TempDir::new().unwrap();
    let app = router(config(&d));
    assert_eq!(
        call(&app, "GET", "/health", None, Value::Null).await.0,
        StatusCode::OK
    );
    for token in [None, Some("invalid"), Some(DISABLED)] {
        assert_eq!(
            call(&app, "GET", "/v1/capabilities", token, Value::Null)
                .await
                .0,
            StatusCode::UNAUTHORIZED
        );
        assert_eq!(
            call(
                &app,
                "POST",
                "/v1/search",
                token,
                json!({"query":"private"})
            )
            .await
            .0,
            StatusCode::UNAUTHORIZED
        );
    }
    let (_, caps) = call(&app, "GET", "/v1/capabilities", Some(CHILD), Value::Null).await;
    assert_eq!(caps["principal_id"], "child");
    assert_eq!(caps["role"], "child");
    assert_eq!(call(&app,"POST","/v1/documents",Some(CHILD),json!({"title":"x","content":"secret","visibility":"household","principal_id":"alice","role":"adult"})).await.0,StatusCode::FORBIDDEN);
}

#[tokio::test]
async fn private_sources_and_child_permissions_are_enforced_before_search_and_listing() {
    let d = TempDir::new().unwrap();
    let app = router(config(&d));
    let private = create(
        &app,
        ALICE,
        "Alice private",
        "orchidmarker private budget is 8700 INR",
        "private",
        false,
    )
    .await;
    create(
        &app,
        ALICE,
        "Adult shared",
        "orchidmarker adult household information",
        "household",
        false,
    )
    .await;
    let child = create(
        &app,
        ALICE,
        "Approved plants",
        "orchidmarker water plants on Sunday",
        "household",
        true,
    )
    .await;
    assert_eq!(
        search(&app, ALICE, "orchidmarker")
            .await
            .as_array()
            .unwrap()
            .len(),
        3
    );
    assert_eq!(
        search(&app, BOB, "orchidmarker")
            .await
            .as_array()
            .unwrap()
            .len(),
        2
    );
    for token in [CHILD, GUEST] {
        let hits = search(&app, token, "orchidmarker").await;
        assert_eq!(hits.as_array().unwrap().len(), 1);
        assert_eq!(hits[0]["document_id"], child["id"]);
        let (_, list) = call(&app, "GET", "/v1/documents", Some(token), Value::Null).await;
        assert!(!list.to_string().contains("private"));
        assert!(!list.to_string().contains("Adult shared"));
    }
    let (_, list) = call(&app, "GET", "/v1/documents", Some(BOB), Value::Null).await;
    assert!(!list.to_string().contains(private["id"].as_str().unwrap()));
    for token in [BOB, CHILD, GUEST] {
        assert_eq!(
            call(
                &app,
                "DELETE",
                &format!("/v1/documents/{}", private["id"].as_str().unwrap()),
                Some(token),
                Value::Null
            )
            .await
            .0,
            StatusCode::NOT_FOUND
        );
    }
}

#[tokio::test]
async fn ingestion_citations_and_deletion_survive_database_reopening() {
    let d = TempDir::new().unwrap();
    let cfg = config(&d);
    let app = router(cfg.clone());
    let doc = create(
        &app,
        ELDER,
        "Family recipe",
        "recipeproof Add lentils and simmer for twenty minutes.",
        "private",
        false,
    )
    .await;
    drop(app);
    let app = router(cfg.clone());
    let hits = search(&app, ELDER, "recipeproof").await;
    assert_eq!(hits[0]["document_id"], doc["id"]);
    assert!(hits[0]["excerpt"]
        .as_str()
        .unwrap()
        .contains("twenty minutes"));
    assert_eq!(hits[0]["source_version"].as_str().unwrap().len(), 64);
    assert!(hits[0]["chunk_id"]
        .as_str()
        .unwrap()
        .contains(doc["id"].as_str().unwrap()));
    assert!(search(&app, ALICE, "recipeproof")
        .await
        .as_array()
        .unwrap()
        .is_empty());
    assert_eq!(
        call(
            &app,
            "DELETE",
            &format!("/v1/documents/{}", doc["id"].as_str().unwrap()),
            Some(ELDER),
            Value::Null
        )
        .await
        .0,
        StatusCode::OK
    );
    drop(app);
    let app = router(cfg);
    assert!(search(&app, ELDER, "recipeproof")
        .await
        .as_array()
        .unwrap()
        .is_empty());
    assert_eq!(
        call(&app, "GET", "/v1/documents", Some(ELDER), Value::Null)
            .await
            .1["documents"],
        json!([])
    );
}

#[tokio::test]
async fn punctuation_fts_operators_unicode_and_limits_are_data_not_query_syntax() {
    let d = TempDir::new().unwrap();
    let app = router(config(&d));
    create(
        &app,
        ALICE,
        "తెలుగు గమనిక",
        "ఆదివారం మొక్కలకు నీరు పెట్టండి. café résumé quotedmarker.",
        "private",
        false,
    )
    .await;
    assert!(!search(&app, ALICE, "ఆదివారం")
        .await
        .as_array()
        .unwrap()
        .is_empty());
    assert!(!search(&app, ALICE, "café")
        .await
        .as_array()
        .unwrap()
        .is_empty());
    for query in [
        "\"quotedmarker\"",
        "quotedmarker OR \"",
        "quotedmarker'); DROP TABLE documents; --",
    ] {
        assert!(!search(&app, ALICE, query)
            .await
            .as_array()
            .unwrap()
            .is_empty());
    }
    assert_eq!(search(&app, ALICE, "\"()*:-'").await, json!([]));
    for limit in [0, 11, -1] {
        assert_eq!(
            call(
                &app,
                "POST",
                "/v1/search",
                Some(ALICE),
                json!({"query":"café","limit":limit})
            )
            .await
            .0,
            StatusCode::BAD_REQUEST
        );
    }
    assert_eq!(
        call(
            &app,
            "POST",
            "/v1/search",
            Some(ALICE),
            json!({"query":"x".repeat(2049)})
        )
        .await
        .0,
        StatusCode::BAD_REQUEST
    );
}

#[tokio::test]
async fn invalid_and_overlarge_inputs_do_not_create_documents() {
    let d = TempDir::new().unwrap();
    let app = router(config(&d));
    for body in [
        json!({"title":" ","content":"x","visibility":"private"}),
        json!({"title":"x","content":" ","visibility":"private"}),
        json!({"title":"x","content":"text","visibility":"unknown"}),
        json!({"title":"x","content":"text","visibility":"private","child_allowed":true}),
        json!({"title":"x","content":"text","visibility":"private","owner_id":"bob"}),
        json!({"title":"x".repeat(201),"content":"text","visibility":"private"}),
    ] {
        assert_eq!(
            call(&app, "POST", "/v1/documents", Some(ALICE), body)
                .await
                .0,
            StatusCode::BAD_REQUEST
        );
    }
    let status = call(
        &app,
        "POST",
        "/v1/documents",
        Some(ALICE),
        json!({"title":"Large","content":"x".repeat(2_000_001),"visibility":"private"}),
    )
    .await
    .0;
    assert!(status == StatusCode::BAD_REQUEST || status == StatusCode::PAYLOAD_TOO_LARGE);
    assert_eq!(
        call(&app, "GET", "/v1/documents", Some(ALICE), Value::Null)
            .await
            .1["documents"],
        json!([])
    );
}

#[test]
fn unsafe_bind_and_ambiguous_or_short_credentials_fail_closed_without_disclosure() {
    let d = TempDir::new().unwrap();
    let mut values = vec![];
    for bind in ["0.0.0.0:8788", "192.168.1.10:8788", "example.com:8788"] {
        let mut v = config(&d);
        v["bind"] = json!(bind);
        values.push(v);
    }
    let mut v = config(&d);
    v["principals"][1]["id"] = json!("alice");
    values.push(v);
    let mut v = config(&d);
    v["principals"][1]["token"] = json!(ALICE);
    values.push(v);
    let mut v = config(&d);
    v["principals"][0]["token"] = json!("short-secret");
    values.push(v);
    for value in values {
        let result = app(serde_json::from_value(value).unwrap());
        let error = result.err().expect("unsafe configuration accepted");
        assert!(!error.contains(ALICE));
        assert!(!error.contains("short-secret"));
    }
}

#[tokio::test]
async fn titles_and_late_chunks_are_searchable_and_results_are_bounded() {
    let d = TempDir::new().unwrap();
    let app = router(config(&d));
    let content = format!(
        "{} finalsectionmarker: turn off the garden pump.",
        "ordinary text. ".repeat(500)
    );
    let doc = create(&app, ALICE, "Pumpmanualtitle", &content, "private", false).await;
    assert_eq!(
        search(&app, ALICE, "pumpmanualtitle").await[0]["document_id"],
        doc["id"]
    );
    let hits = search(&app, ALICE, "finalsectionmarker").await;
    assert!(hits[0]["excerpt"].as_str().unwrap().contains("garden pump"));
    assert!(hits[0]["excerpt"].as_str().unwrap().chars().count() <= 1600);
    let (_, found) = call(
        &app,
        "POST",
        "/v1/search",
        Some(ALICE),
        json!({"query":"ordinary","limit":1}),
    )
    .await;
    assert_eq!(found["citations"].as_array().unwrap().len(), 1);
    assert!(search(&app, BOB, "pumpmanualtitle finalsectionmarker")
        .await
        .as_array()
        .unwrap()
        .is_empty());
}

#[tokio::test]
async fn initial_schema_is_migrated_without_discarding_private_documents() {
    let d = TempDir::new().unwrap();
    let cfg = config(&d);
    let db = rusqlite::Connection::open(d.path().join("knowledge.db")).unwrap();
    db.execute_batch("CREATE TABLE documents(id TEXT PRIMARY KEY,owner_id TEXT,title TEXT,visibility TEXT,child_allowed INTEGER,source_version TEXT,created_at INTEGER);
        CREATE VIRTUAL TABLE document_chunks USING fts5(chunk_id UNINDEXED,document_id UNINDEXED,content,tokenize='unicode61');
        INSERT INTO documents VALUES('legacy','alice','Migratedtitle','private',0,'legacy-version',1);
        INSERT INTO document_chunks VALUES('legacy:0','legacy','preserved content'); PRAGMA user_version=1;").unwrap();
    drop(db);
    let app = router(cfg);
    assert_eq!(
        search(&app, ALICE, "Migratedtitle").await[0]["document_id"],
        "legacy"
    );
    assert_eq!(
        search(&app, ALICE, "preserved").await[0]["excerpt"],
        "preserved content"
    );
    assert!(search(&app, BOB, "Migratedtitle preserved")
        .await
        .as_array()
        .unwrap()
        .is_empty());
}

#[tokio::test]
async fn relevance_includes_matches_beyond_the_first_thousand_authorized_chunks() {
    let d = TempDir::new().unwrap();
    let app = router(config(&d));
    // More than 1,000 chunks match the title. The unique content marker is in
    // chunks near 999, beyond a lexicographic chunk-ID LIMIT 1000 cutoff.
    let content = format!(
        "{} ultrarelevanttail turn off the garden pump. {}",
        "ordinary ".repeat(155_450),
        "ordinary ".repeat(60_000)
    );
    let document = create(&app, ALICE, "commoncatalog", &content, "private", false).await;
    let (status, result) = call(
        &app,
        "POST",
        "/v1/search",
        Some(ALICE),
        json!({"query":"commoncatalog ultrarelevanttail","limit":1}),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(result["citations"].as_array().unwrap().len(), 1);
    assert_eq!(result["citations"][0]["document_id"], document["id"]);
    assert!(result["citations"][0]["excerpt"]
        .as_str()
        .unwrap()
        .contains("ultrarelevanttail"));
}
