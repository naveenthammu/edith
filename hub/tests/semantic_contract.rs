use axum::{
    body::{to_bytes, Body},
    http::{Request, StatusCode},
    Router,
};
use edith_central_hub::{app, Config};
use serde_json::{json, Value};
use tempfile::TempDir;
use tower::ServiceExt;

const ALICE: &str = "semantic-alice-credential-000000000000001";
const BOB: &str = "semantic-bob-credential-00000000000000002";
const CHILD: &str = "semantic-child-credential-00000000000003";
const MODEL: &str = "synthetic-vectors@sha256:model-a";
fn config(d: &TempDir) -> Value {
    json!({"database_path":d.path().join("hub.db"),"principals":[
        {"id":"alice","token":ALICE,"role":"adult"},{"id":"bob","token":BOB,"role":"elder"},{"id":"child","token":CHILD,"role":"child"}
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
    data: Value,
) -> (StatusCode, Value) {
    let mut req = Request::builder()
        .method(method)
        .uri(path)
        .header("content-type", "application/json");
    if let Some(token) = token {
        req = req.header("authorization", format!("Bearer {token}"));
    }
    let response = app
        .clone()
        .oneshot(
            req.body(Body::from(if data.is_null() {
                String::new()
            } else {
                data.to_string()
            }))
            .unwrap(),
        )
        .await
        .unwrap();
    let status = response.status();
    let bytes = to_bytes(response.into_body(), 40_000_000).await.unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}
async fn create(
    app: &Router,
    owner: &str,
    title: &str,
    content: &str,
    visibility: &str,
    child: bool,
) -> Value {
    let (s, v) = call(
        app,
        "POST",
        "/v1/documents",
        Some(owner),
        json!({"title":title,"content":content,"visibility":visibility,"child_allowed":child}),
    )
    .await;
    assert_eq!(s, StatusCode::CREATED, "{v}");
    v
}
fn endpoint(doc: &Value, suffix: &str) -> String {
    format!("/v1/documents/{}/{}", doc["id"].as_str().unwrap(), suffix)
}
async fn chunks(app: &Router, owner: &str, doc: &Value) -> Value {
    let (s, v) = call(
        app,
        "GET",
        &endpoint(doc, "chunks"),
        Some(owner),
        Value::Null,
    )
    .await;
    assert_eq!(s, StatusCode::OK, "{v}");
    v
}
fn vector(axis: usize) -> Vec<f32> {
    let mut value = vec![0.0; 16];
    value[axis] = 1.0;
    value
}
async fn upload(app: &Router, owner: &str, doc: &Value, model: &str, value: Vec<f32>) -> Value {
    let source = chunks(app, owner, doc).await;
    let embeddings: Vec<_> = source["chunks"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| json!({"chunk_id":c["chunk_id"],"vector":value}))
        .collect();
    let (s, v) = call(
        app,
        "POST",
        &endpoint(doc, "embeddings"),
        Some(owner),
        json!({"source_version":doc["source_version"],"model_id":model,"embeddings":embeddings}),
    )
    .await;
    assert_eq!(s, StatusCode::OK, "{v}");
    v
}
async fn search(app: &Router, owner: &str, query: &str, model: &str, value: Vec<f32>) -> Value {
    let (s, v) = call(
        app,
        "POST",
        "/v1/search",
        Some(owner),
        json!({"query":query,"query_vector":{"model_id":model,"vector":value}}),
    )
    .await;
    assert_eq!(s, StatusCode::OK, "{v}");
    v
}

#[tokio::test]
async fn semantic_synonyms_retrieve_without_word_overlap_and_never_cross_private_owners() {
    let d = TempDir::new().unwrap();
    let cfg = config(&d);
    let app = router(cfg.clone());
    let recipe = create(
        &app,
        ALICE,
        "Supper guide",
        "Prepare chickpea stew for the evening meal.",
        "private",
        false,
    )
    .await;
    let unrelated = create(
        &app,
        ALICE,
        "Garden",
        "Water the roses each morning.",
        "private",
        false,
    )
    .await;
    upload(&app, ALICE, &recipe, MODEL, vector(0)).await;
    upload(&app, ALICE, &unrelated, MODEL, vector(1)).await;
    let (s, lexical) = call(
        &app,
        "POST",
        "/v1/search",
        Some(ALICE),
        json!({"query":"dinner instructions"}),
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(lexical["citations"], json!([]));
    assert_eq!(lexical["retrieval_mode"], "lexical");
    let found = search(&app, ALICE, "dinner instructions", MODEL, vector(0)).await;
    assert_eq!(found["retrieval_mode"], "hybrid");
    assert_eq!(found["semantic_available"], true);
    assert_eq!(found["citations"].as_array().unwrap().len(), 1);
    assert_eq!(found["citations"][0]["document_id"], recipe["id"]);
    assert!(found["citations"][0]["excerpt"]
        .as_str()
        .unwrap()
        .contains("chickpea"));
    for token in [BOB, CHILD] {
        let hidden = search(&app, token, "dinner instructions", MODEL, vector(0)).await;
        assert_eq!(hidden["citations"], json!([]));
        assert_eq!(hidden["semantic_available"], false);
        assert_eq!(hidden["retrieval_mode"], "lexical");
        assert_eq!(
            call(
                &app,
                "GET",
                &endpoint(&recipe, "chunks"),
                Some(token),
                Value::Null
            )
            .await
            .0,
            StatusCode::NOT_FOUND
        );
        assert_eq!(
            call(
                &app,
                "POST",
                &endpoint(&recipe, "embeddings"),
                Some(token),
                json!({})
            )
            .await
            .0,
            StatusCode::NOT_FOUND
        );
    }
    drop(app);
    let app = router(cfg);
    assert_eq!(
        search(&app, ALICE, "dinner", MODEL, vector(0)).await["citations"][0]["document_id"],
        recipe["id"]
    );
}

#[tokio::test]
async fn fusion_uses_both_rankings_and_only_explicit_child_sources() {
    let d = TempDir::new().unwrap();
    let app = router(config(&d));
    let lexical = create(
        &app,
        ALICE,
        "trousers trousers trousers",
        "trousers trousers trousers",
        "household",
        false,
    )
    .await;
    let both = create(
        &app,
        ALICE,
        "Clothes",
        "trousers fit comfortably",
        "household",
        true,
    )
    .await;
    let semantic = create(
        &app,
        ALICE,
        "Pants",
        "Tailored slacks fit well",
        "household",
        false,
    )
    .await;
    upload(&app, ALICE, &lexical, MODEL, vector(1)).await;
    let mut mixed = vector(0);
    mixed[1] = 0.5;
    upload(&app, ALICE, &both, MODEL, mixed).await;
    upload(&app, ALICE, &semantic, MODEL, vector(0)).await;
    let found = search(&app, BOB, "trousers", MODEL, vector(0)).await;
    assert_eq!(
        found["citations"][0]["document_id"], both["id"],
        "a source present in both rankings should outrank one-channel-only sources"
    );
    let child = search(&app, CHILD, "pants", MODEL, vector(0)).await;
    assert_eq!(child["citations"].as_array().unwrap().len(), 1);
    assert_eq!(child["citations"][0]["document_id"], both["id"]);
    let (s,filtered)=call(&app,"POST","/v1/search",Some(BOB),json!({"query":"pants","query_vector":{"model_id":MODEL,"vector":vector(0)},"min_similarity":0.99})).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(filtered["citations"][0]["document_id"], semantic["id"]);
}

#[tokio::test]
async fn embedding_uploads_are_complete_atomic_versioned_and_dimension_bound() {
    let d = TempDir::new().unwrap();
    let app = router(config(&d));
    let doc = create(
        &app,
        ALICE,
        "Long",
        "original ".repeat(500).as_str(),
        "private",
        false,
    )
    .await;
    let source = chunks(&app, ALICE, &doc).await;
    let list = source["chunks"].as_array().unwrap();
    assert!(list.len() > 1);
    upload(&app, ALICE, &doc, MODEL, vector(0)).await;
    let baseline = search(&app, ALICE, "synonym", MODEL, vector(0)).await;
    let valid: Vec<_> = list
        .iter()
        .map(|c| json!({"chunk_id":c["chunk_id"],"vector":vector(1)}))
        .collect();
    let mut duplicate = valid.clone();
    duplicate[1] = duplicate[0].clone();
    let mut foreign = valid.clone();
    foreign[0]["chunk_id"] = json!("another-document:0");
    let mut dimensions = valid.clone();
    dimensions[0]["vector"] = json!(vec![1.0; 17]);
    let mut zero = valid.clone();
    zero[0]["vector"] = json!(vec![0.0; 16]);
    for invalid in [valid[..1].to_vec(), duplicate, foreign, dimensions, zero] {
        let (status, _) = call(
            &app,
            "POST",
            &endpoint(&doc, "embeddings"),
            Some(ALICE),
            json!({"source_version":doc["source_version"],"model_id":MODEL,"embeddings":invalid}),
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert_eq!(
            search(&app, ALICE, "synonym", MODEL, vector(0)).await,
            baseline,
            "failed replacement must retain prior vectors"
        );
    }
    assert_eq!(
        call(
            &app,
            "POST",
            &endpoint(&doc, "embeddings"),
            Some(ALICE),
            json!({"source_version":"stale","model_id":MODEL,"embeddings":valid})
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
    let all17: Vec<_> = list
        .iter()
        .map(|c| json!({"chunk_id":c["chunk_id"],"vector":vec![1.0;17]}))
        .collect();
    assert_eq!(
        call(
            &app,
            "POST",
            &endpoint(&doc, "embeddings"),
            Some(ALICE),
            json!({"source_version":doc["source_version"],"model_id":MODEL,"embeddings":all17})
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
    upload(&app, ALICE, &doc, "different-engine@sha256:b", vector(1)).await;
    assert!(
        !search(&app, ALICE, "synonym", MODEL, vector(0)).await["citations"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    assert_eq!(
        search(
            &app,
            ALICE,
            "synonym",
            "different-engine@sha256:b",
            vector(0)
        )
        .await["citations"],
        json!([])
    );
}

#[tokio::test]
async fn malformed_vectors_and_thresholds_are_rejected_without_poisoning_search() {
    let d = TempDir::new().unwrap();
    let app = router(config(&d));
    for value in [
        json!(vec![0.0; 16]),
        json!(vec![1.0; 15]),
        json!(vec![1.0; 4097]),
        json!([null]),
        json!(vec![1e300; 16]),
    ] {
        assert_eq!(
            call(
                &app,
                "POST",
                "/v1/search",
                Some(ALICE),
                json!({"query":"x","query_vector":{"model_id":MODEL,"vector":value}})
            )
            .await
            .0,
            StatusCode::BAD_REQUEST
        );
    }
    for min in [-1.1, 1.1] {
        assert_eq!(
            call(
                &app,
                "POST",
                "/v1/search",
                Some(ALICE),
                json!({"query":"x","min_similarity":min})
            )
            .await
            .0,
            StatusCode::BAD_REQUEST
        );
    }
    let found = search(&app, ALICE, "x", "unknown-engine", vector(0)).await;
    assert_eq!(found["retrieval_mode"], "lexical");
    assert_eq!(found["semantic_available"], false);
}

#[tokio::test]
async fn guarded_updates_and_deletion_invalidate_vectors_and_citations_immediately() {
    let d = TempDir::new().unwrap();
    let app = router(config(&d));
    let original = create(
        &app,
        ALICE,
        "Old title",
        "The school opens Monday.",
        "household",
        true,
    )
    .await;
    upload(&app, ALICE, &original, MODEL, vector(0)).await;
    let check = json!({"sources":[{"document_id":original["id"],"source_version":original["source_version"]},{"document_id":"missing","source_version":"anything"}]});
    let before = call(
        &app,
        "POST",
        "/v1/sources/validate",
        Some(CHILD),
        check.clone(),
    )
    .await;
    assert_eq!(before.0, StatusCode::OK);
    assert_eq!(before.1["sources"][0]["valid"], true);
    assert_eq!(before.1["sources"][1]["valid"], false);
    let replacement = json!({"expected_source_version":original["source_version"],"title":"New title","content":"The school opens Tuesday.","visibility":"private","child_allowed":false});
    let p = format!("/v1/documents/{}", original["id"].as_str().unwrap());
    assert_eq!(
        call(&app, "PUT", &p, Some(BOB), replacement.clone())
            .await
            .0,
        StatusCode::NOT_FOUND
    );
    let (s, updated) = call(&app, "PUT", &p, Some(ALICE), replacement.clone()).await;
    assert_eq!(s, StatusCode::OK);
    assert_ne!(original["source_version"], updated["source_version"]);
    assert_eq!(
        call(&app, "PUT", &p, Some(ALICE), replacement).await.0,
        StatusCode::CONFLICT
    );
    assert_eq!(
        search(&app, ALICE, "opening time", MODEL, vector(0)).await["semantic_available"],
        false
    );
    let after = call(&app, "POST", "/v1/sources/validate", Some(CHILD), check)
        .await
        .1;
    assert_eq!(after["sources"][0]["valid"], false);
    assert!(!after.to_string().contains("New title"));
    upload(&app, ALICE, &updated, MODEL, vector(0)).await;
    assert_eq!(
        call(&app, "DELETE", &p, Some(ALICE), Value::Null).await.0,
        StatusCode::OK
    );
    assert_eq!(
        search(&app, ALICE, "opening time", MODEL, vector(0)).await["semantic_available"],
        false
    );
    let db = rusqlite::Connection::open(d.path().join("hub.db")).unwrap();
    assert_eq!(
        db.query_row("SELECT COUNT(*) FROM document_embeddings", [], |r| r
            .get::<_, i64>(0))
            .unwrap(),
        0
    );
}

#[tokio::test]
async fn metadata_only_changes_advance_revision_and_revoke_previous_source_receipts() {
    let d = TempDir::new().unwrap();
    let app = router(config(&d));
    let original = create(
        &app,
        ALICE,
        "Original label",
        "Unchanged body",
        "household",
        true,
    )
    .await;
    upload(&app, ALICE, &original, MODEL, vector(0)).await;
    let p = format!("/v1/documents/{}", original["id"].as_str().unwrap());
    let body = json!({"expected_source_version":original["source_version"],"title":"Corrected label","content":"Unchanged body","visibility":"household","child_allowed":true});
    let (status, renamed) = call(&app, "PUT", &p, Some(ALICE), body.clone()).await;
    assert_eq!(status, StatusCode::OK);
    assert_ne!(renamed["source_version"], original["source_version"]);
    assert_eq!(
        call(&app, "PUT", &p, Some(ALICE), body).await.0,
        StatusCode::CONFLICT
    );
    let checked = call(
        &app,
        "POST",
        "/v1/sources/validate",
        Some(CHILD),
        json!({"sources":[
            {"document_id":original["id"],"source_version":original["source_version"]},
            {"document_id":renamed["id"],"source_version":renamed["source_version"]}
        ]}),
    )
    .await
    .1;
    assert_eq!(checked["sources"][0]["valid"], false);
    assert_eq!(checked["sources"][1]["valid"], true);
    let (s,private)=call(&app,"PUT",&p,Some(ALICE),json!({"expected_source_version":renamed["source_version"],"title":"Corrected label","content":"Unchanged body","visibility":"private","child_allowed":false})).await;
    assert_eq!(s, StatusCode::OK);
    assert_ne!(private["source_version"], renamed["source_version"]);
    assert_eq!(
        search(&app, ALICE, "synonym", MODEL, vector(0)).await["semantic_available"],
        false
    );
    let denied=call(&app,"POST","/v1/sources/validate",Some(CHILD),json!({"sources":[{"document_id":private["id"],"source_version":private["source_version"]}]})).await.1;
    assert_eq!(denied["sources"][0]["valid"], false);
    let invalid = json!({"expected_source_version":private["source_version"],"title":" ","content":"Must not replace","visibility":"private"});
    assert_eq!(
        call(&app, "PUT", &p, Some(ALICE), invalid).await.0,
        StatusCode::BAD_REQUEST
    );
    assert_eq!(
        chunks(&app, ALICE, &private).await["chunks"][0]["content"],
        "Unchanged body"
    );
}

#[tokio::test]
async fn dimensions_of_private_models_are_not_disclosed_to_other_principals() {
    let d = TempDir::new().unwrap();
    let app = router(config(&d));
    let doc = create(&app, ALICE, "Private", "Semantic fixture", "private", false).await;
    upload(&app, ALICE, &doc, MODEL, vector(0)).await;
    let different = vec![1.0; 32];
    let hidden = search(&app, BOB, "fixture", MODEL, different.clone()).await;
    assert_eq!(hidden["semantic_available"], false);
    assert_eq!(hidden["citations"], json!([]));
    assert_eq!(
        call(
            &app,
            "POST",
            "/v1/search",
            Some(ALICE),
            json!({"query":"fixture","query_vector":{"model_id":MODEL,"vector":different}})
        )
        .await
        .0,
        StatusCode::BAD_REQUEST
    );
}

#[tokio::test]
async fn embedding_route_accepts_atomic_payloads_above_the_document_body_limit() {
    let d = TempDir::new().unwrap();
    let app = router(config(&d));
    let doc = create(
        &app,
        ALICE,
        "Bulk index",
        &"large ".repeat(16_000),
        "private",
        false,
    )
    .await;
    let source = chunks(&app, ALICE, &doc).await;
    let value = vec![0.123456789_f32; 4096];
    let rows: Vec<_> = source["chunks"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| json!({"chunk_id":c["chunk_id"],"vector":value}))
        .collect();
    let body = json!({"source_version":doc["source_version"],"model_id":"bulk-model@sha256:4096","embeddings":rows});
    assert!(body.to_string().len() > 2_100_000);
    assert!(body.to_string().len() < 32 * 1024 * 1024);
    let (s, receipt) = call(
        &app,
        "POST",
        &endpoint(&doc, "embeddings"),
        Some(ALICE),
        body,
    )
    .await;
    assert_eq!(s, StatusCode::OK, "{receipt}");
    assert_eq!(receipt["dimensions"], 4096);
    let found = search(
        &app,
        ALICE,
        "unrelated synonym",
        "bulk-model@sha256:4096",
        value,
    )
    .await;
    assert_eq!(found["retrieval_mode"], "hybrid");
    assert_eq!(found["citations"][0]["document_id"], doc["id"]);
}

#[tokio::test]
async fn oversized_embedding_upload_reports_size_limit_and_leaves_the_index_unchanged() {
    let d = TempDir::new().unwrap();
    let app = router(config(&d));
    let doc = create(
        &app,
        ALICE,
        "Size guard",
        "Existing semantic source",
        "private",
        false,
    )
    .await;
    upload(&app, ALICE, &doc, MODEL, vector(0)).await;
    let before = search(&app, ALICE, "synonym", MODEL, vector(0)).await;
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(endpoint(&doc, "embeddings"))
                .header("authorization", format!("Bearer {ALICE}"))
                .header("content-type", "application/json")
                .body(Body::from(vec![b' '; 32 * 1024 * 1024 + 1]))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::PAYLOAD_TOO_LARGE);
    assert_eq!(
        search(&app, ALICE, "synonym", MODEL, vector(0)).await,
        before
    );
}
