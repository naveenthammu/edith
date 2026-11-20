use axum::{body::{to_bytes, Body}, http::{Request, StatusCode}, Router};
use edith_hub::app::{router, AppState};
use serde_json::{json, Value};
use std::{fs, path::PathBuf};
use tower::ServiceExt;
use uuid::Uuid;

struct Fixture { root: PathBuf, app: Router }
impl Fixture {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!("edith-hub-test-{}", Uuid::new_v4()));
        fs::create_dir_all(root.join("repo/prototype/web/src")).unwrap();
        fs::write(root.join("repo/main.py"), "print('safe source fixture')\n").unwrap();
        fs::write(root.join("repo/.env"), "SECRET=never-return-this").unwrap();
        fs::write(root.join("repo/prototype/web/src/App.tsx"), "export const app = 'safe';").unwrap();
        let app = router(AppState::new(root.join("data"), root.join("repo")).unwrap());
        Self { root, app }
    }
    fn reload(&self) -> Router {
        router(AppState::new(self.root.join("data"), self.root.join("repo")).unwrap())
    }
}
impl Drop for Fixture { fn drop(&mut self) { let _ = fs::remove_dir_all(&self.root); } }

async fn call(app: &Router, method: &str, path: &str, token: Option<&str>, body: Value) -> (StatusCode, Value) {
    let mut request = Request::builder().method(method).uri(path)
        .header("host", "127.0.0.1:8787").header("origin", "http://127.0.0.1:8787")
        .header("content-type", "application/json");
    if let Some(token) = token { request = request.header("x-edith-session", token); }
    let response = app.clone().oneshot(request.body(Body::from(if body.is_null() { String::new() } else { body.to_string() })).unwrap()).await.unwrap();
    let status = response.status();
    let bytes = to_bytes(response.into_body(), 2_000_000).await.unwrap();
    (status, serde_json::from_slice(&bytes).unwrap_or(Value::Null))
}
async fn bootstrap(app: &Router) -> String {
    let (status, body) = call(app, "GET", "/api/bootstrap", None, Value::Null).await;
    assert_eq!(status, StatusCode::OK, "bootstrap: {body}");
    body["session_token"].as_str().unwrap().to_owned()
}
async fn profile(app: &Router, token: &str, value: &str) -> Value {
    let (status, body) = call(app, "POST", "/api/profile", Some(token), json!({"profile":value})).await;
    assert_eq!(status, StatusCode::OK, "profile: {body}"); body
}

// Removing either token or local-origin validation must expose a failing test.
#[tokio::test]
async fn private_reads_require_token_and_cross_origin_bootstrap_is_denied() {
    let f = Fixture::new();
    assert_eq!(call(&f.app, "GET", "/api/state", None, Value::Null).await.0, StatusCode::UNAUTHORIZED);
    let response = f.app.clone().oneshot(Request::builder().uri("/api/bootstrap")
        .header("host", "127.0.0.1:8787").header("origin", "https://untrusted.example")
        .body(Body::empty()).unwrap()).await.unwrap();
    assert_eq!(response.status(), StatusCode::FORBIDDEN);
    let response = f.app.clone().oneshot(Request::builder().uri("/api/bootstrap")
        .header("host", "attacker.example:8787").body(Body::empty()).unwrap()).await.unwrap();
    assert_eq!(response.status(), StatusCode::FORBIDDEN);
}

// Filtering only the UI or filtering after retrieval/model prompting fails this boundary.
#[tokio::test]
async fn child_and_guest_cannot_retrieve_adult_sources_or_use_adult_capabilities() {
    let f = Fixture::new(); let token = bootstrap(&f.app).await;
    let (_, private) = call(&f.app, "POST", "/api/documents", Some(&token), json!({"title":"Private fixture","content":"violetbudgetsecret costs 8700 INR","scope":"adult"})).await;
    for role in ["child", "guest"] {
        let state = profile(&f.app, &token, role).await;
        assert!(!state.to_string().contains("Private fixture"));
        let (status, found) = call(&f.app, "POST", "/api/search", Some(&token), json!({"query":"violetbudgetsecret"})).await;
        assert_eq!(status, StatusCode::OK); assert_eq!(found["citations"], json!([]));
        for (method, path, body) in [
            ("GET", "/api/developer/files".to_owned(), Value::Null),
            ("POST", "/api/actions".to_owned(), json!({"kind":"message","details":{"recipient":"Alex (demo)","text":"hello"}})),
            ("POST", "/api/documents".to_owned(), json!({"title":"No","content":"No","scope":"household"})),
            ("DELETE", format!("/api/documents/{}", private["id"].as_str().unwrap()), Value::Null),
        ] { assert_eq!(call(&f.app, method, &path, Some(&token), body).await.0, StatusCode::FORBIDDEN, "{role}: {path}"); }
    }
    profile(&f.app, &token, "child").await;
    assert_eq!(call(&f.app, "POST", "/api/devices/study-light/toggle", Some(&token), json!({"on":true})).await.0, StatusCode::OK);
    assert_eq!(call(&f.app, "POST", "/api/devices/bedroom-fan/toggle", Some(&token), json!({"on":true})).await.0, StatusCode::FORBIDDEN);
    profile(&f.app, &token, "guest").await;
    assert_eq!(call(&f.app, "POST", "/api/devices/study-light/toggle", Some(&token), json!({"on":false})).await.0, StatusCode::FORBIDDEN);
}

// A deletion that only changes listings, or a write that never reaches disk, fails here.
#[tokio::test]
async fn note_retrieval_and_deletion_survive_restart_without_leaking_contents_into_activity() {
    let f = Fixture::new(); let token = bootstrap(&f.app).await;
    let (status, doc) = call(&f.app, "POST", "/api/documents", Some(&token), json!({"title":"Orchid schedule","content":"orchidtestmarker Water on Monday at 9am.","scope":"household","share_confirmed":true})).await;
    assert_eq!(status, StatusCode::OK);
    let restarted = f.reload();
    let (_, hits) = call(&restarted, "POST", "/api/search", Some(&token), json!({"query":"orchidtestmarker"})).await;
    assert_eq!(hits["citations"][0]["document_id"], doc["id"]);
    assert!(hits["citations"][0]["excerpt"].as_str().unwrap().contains("Monday"));
    assert_eq!(call(&restarted, "DELETE", &format!("/api/documents/{}", doc["id"].as_str().unwrap()), Some(&token), Value::Null).await.0, StatusCode::OK);
    let restarted = f.reload();
    assert_eq!(call(&restarted, "POST", "/api/search", Some(&token), json!({"query":"orchidtestmarker"})).await.1["citations"], json!([]));
    let (_, state) = call(&restarted, "GET", "/api/state", Some(&token), Value::Null).await;
    assert!(!state["activity"].to_string().contains("orchidtestmarker"));
}

// Trusting client totals, permitting another owner/profile, or changing completed decisions fails here.
#[tokio::test]
async fn approval_totals_ownership_profile_and_idempotency_are_server_enforced() {
    let f = Fixture::new(); let token = bootstrap(&f.app).await;
    let (status, action) = call(&f.app, "POST", "/api/actions", Some(&token), json!({"kind":"purchase","details":{"items":[{"name":"Rice (demo)","quantity":2,"unit_price":12.50}],"merchant":"Demo grocery"}})).await;
    assert_eq!(status, StatusCode::OK, "{action}");
    assert_eq!(action["details"]["total"], json!(25.0));
    let path = format!("/api/actions/{}/decide", action["id"].as_str().unwrap());
    let other = bootstrap(&f.app).await;
    assert_eq!(call(&f.app, "POST", &path, Some(&other), json!({"decision":"approve"})).await.0, StatusCode::FORBIDDEN);
    assert_eq!(call(&f.app, "GET", "/api/state", Some(&other), Value::Null).await.1["actions"], json!([]));
    profile(&f.app, &token, "elder").await;
    assert_eq!(call(&f.app, "POST", &path, Some(&token), json!({"decision":"approve"})).await.0, StatusCode::FORBIDDEN);
    profile(&f.app, &token, "adult").await;
    let (status, approved) = call(&f.app, "POST", &path, Some(&token), json!({"decision":"approve"})).await;
    assert_eq!(status, StatusCode::OK); assert_eq!(approved["status"], "approved_preview"); assert_eq!(approved["demo"], true);
    assert_eq!(call(&f.app, "POST", &path, Some(&token), json!({"decision":"approve"})).await.1, approved);
    assert_eq!(call(&f.app, "POST", &path, Some(&token), json!({"decision":"reject"})).await.0, StatusCode::CONFLICT);
}

#[tokio::test]
async fn expired_approvals_and_malformed_action_inputs_are_rejected() {
    let f = Fixture::new(); let token = bootstrap(&f.app).await;
    for details in [
        json!({"items":[{"name":"Bad","quantity":-1,"unit_price":20}],"merchant":"Demo"}),
        json!({"items":[{"name":"Bad","quantity":1,"unit_price":1000001}],"merchant":"Demo"}),
        json!({"items":[],"merchant":"Demo"}),
        json!({"items":[{"name":"Bad","quantity":1,"unit_price":20}],"merchant":"Demo","total":1}),
    ] { assert_eq!(call(&f.app, "POST", "/api/actions", Some(&token), json!({"kind":"purchase","details":details})).await.0, StatusCode::BAD_REQUEST); }
    assert_eq!(call(&f.app, "POST", "/api/actions", Some(&token), json!({"kind":"message","details":{"recipient":"person@example.com","text":"hello"}})).await.0, StatusCode::BAD_REQUEST);
    let (_, action) = call(&f.app, "POST", "/api/actions", Some(&token), json!({"kind":"message","details":{"recipient":"Alex (demo)","text":"hello"}})).await;
    let state_path = f.root.join("data/state.json");
    let mut persisted: Value = serde_json::from_slice(&fs::read(&state_path).unwrap()).unwrap();
    persisted["actions"][0]["expires_unix"] = json!(1);
    fs::write(&state_path, serde_json::to_vec(&persisted).unwrap()).unwrap();
    let restarted = f.reload();
    assert_eq!(call(&restarted, "POST", &format!("/api/actions/{}/decide", action["id"].as_str().unwrap()), Some(&token), json!({"decision":"approve"})).await.0, StatusCode::GONE);
}

// Removing normalization/canonical containment or secret-name rejection fails these requests.
#[tokio::test]
async fn developer_can_read_allowlisted_source_but_not_paths_or_secret_files() {
    let f = Fixture::new(); let token = bootstrap(&f.app).await;
    let (status, file) = call(&f.app, "GET", "/api/developer/file?path=main.py", Some(&token), Value::Null).await;
    assert_eq!(status, StatusCode::OK); assert!(file["content"].as_str().unwrap().contains("safe source fixture"));
    for path in ["../.env", "..%2F.env", ".env", "C:%5CWindows%5Cwin.ini", "/etc/passwd", "prototype%5C..%5C..%5C.env", "prototype/web/src/credentials.ts"] {
        assert_ne!(call(&f.app, "GET", &format!("/api/developer/file?path={path}"), Some(&token), Value::Null).await.0, StatusCode::OK, "{path}");
    }
    let (_, files) = call(&f.app, "GET", "/api/developer/files", Some(&token), Value::Null).await;
    assert!(!files.to_string().contains(".env"));
}

#[tokio::test]
async fn limits_and_curated_play_do_not_expose_answers() {
    let f = Fixture::new(); let token = bootstrap(&f.app).await;
    assert_eq!(call(&f.app, "POST", "/api/documents", Some(&token), json!({"title":"too large","content":"x".repeat(50_001),"scope":"household"})).await.0, StatusCode::BAD_REQUEST);
    assert_eq!(call(&f.app, "POST", "/api/chat", Some(&token), json!({"message":"x".repeat(4_001)})).await.0, StatusCode::BAD_REQUEST);
    profile(&f.app, &token, "child").await;
    let (_, play) = call(&f.app, "GET", "/api/play", Some(&token), Value::Null).await;
    assert!(play["questions"].as_array().unwrap().len() >= 3);
    for q in play["questions"].as_array().unwrap() { assert!(q.get("answer").is_none()); assert!(q.get("correct").is_none()); }
    let (_, answer) = call(&f.app, "POST", "/api/play/answer", Some(&token), json!({"question_id":"planet","answer":"Earth"})).await;
    assert_eq!(answer["correct"], true);
    let (_, chat) = call(&f.app, "POST", "/api/chat", Some(&token), json!({"message":"Tell me a story"})).await;
    assert_eq!(chat["mode"], "curated");
}

#[tokio::test]
async fn safe_home_commands_change_demo_state_and_restricted_commands_do_not() {
    let f = Fixture::new(); let token = bootstrap(&f.app).await;
    let (status, chat) = call(&f.app, "POST", "/api/chat", Some(&token), json!({"message":"Turn on the study light"})).await;
    assert_eq!(status, StatusCode::OK); assert_eq!(chat["mode"], "local-tool");
    let (_, state) = call(&f.app, "GET", "/api/state", Some(&token), Value::Null).await;
    assert_eq!(state["devices"].as_array().unwrap().iter().find(|d| d["id"] == "study-light").unwrap()["on"], true);
    profile(&f.app, &token, "child").await;
    assert_eq!(call(&f.app, "POST", "/api/chat", Some(&token), json!({"message":"Turn on the bedroom fan"})).await.0, StatusCode::FORBIDDEN);
    let (_, chat) = call(&f.app, "POST", "/api/chat", Some(&token), json!({"message":"start a game"})).await;
    assert_eq!(chat["navigate"], "play");
}

#[tokio::test]
async fn persistence_failure_does_not_report_success_or_change_memory() {
    let f = Fixture::new(); let token = bootstrap(&f.app).await;
    let state_path = f.root.join("data/state.json");
    fs::remove_file(&state_path).unwrap();
    fs::create_dir(&state_path).unwrap();
    let (status, body) = call(&f.app, "POST", "/api/documents", Some(&token), json!({"title":"Unsaved note","content":"notdurablefixture","scope":"household","share_confirmed":true})).await;
    assert_eq!(status, StatusCode::INTERNAL_SERVER_ERROR, "{body}");
    let (_, state) = call(&f.app, "GET", "/api/state", Some(&token), Value::Null).await;
    assert!(!state["documents"].to_string().contains("Unsaved note"));
}

#[tokio::test]
async fn invalid_json_returns_json_error_without_crashing() {
    let f = Fixture::new(); let token = bootstrap(&f.app).await;
    let response = f.app.clone().oneshot(Request::builder().method("POST").uri("/api/chat")
        .header("host", "localhost:8787").header("x-edith-session", token)
        .header("content-type", "application/json").body(Body::from("{invalid")).unwrap()).await.unwrap();
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    let body: Value = serde_json::from_slice(&to_bytes(response.into_body(), 4096).await.unwrap()).unwrap();
    assert!(body["error"].is_string());
}

#[tokio::test]
async fn shared_notes_require_explicit_confirmation_but_adult_notes_do_not() {
    let f = Fixture::new(); let token = bootstrap(&f.app).await;
    assert_eq!(call(&f.app, "POST", "/api/documents", Some(&token), json!({"title":"Shared","content":"Visible to children and guests","scope":"household"})).await.0, StatusCode::BAD_REQUEST);
    assert_eq!(call(&f.app, "POST", "/api/documents", Some(&token), json!({"title":"Private","content":"Adults only","scope":"adult"})).await.0, StatusCode::OK);
    assert_eq!(call(&f.app, "POST", "/api/documents", Some(&token), json!({"title":"Shared","content":"Visible to children and guests","scope":"household","share_confirmed":true})).await.0, StatusCode::OK);
}
