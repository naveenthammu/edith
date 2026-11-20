mod actions;
mod business_messages;
mod business_routes;
mod commerce;
mod commerce_routes;
mod config;
mod knowledge;
mod personal;
mod personal_routes;
mod semantic;

use axum::{
    extract::{rejection::JsonRejection, DefaultBodyLimit, Path, Request, State},
    http::StatusCode,
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{delete, get, post},
    Extension, Json, Router,
};
pub use commerce::MerchantConfig;
pub use config::{Config, Principal, Role, WhatsAppWorker};
use knowledge::{CreateDocument, Knowledge, Search};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::sync::{Arc, Mutex};
use subtle::ConstantTimeEq;

#[derive(Clone)]
struct Actor {
    id: String,
    role: Role,
}
struct Credential {
    actor: Actor,
    digest: [u8; 32],
    enabled: bool,
}
struct WorkerCredential {
    scope: business_messages::WorkerScope,
    digest: [u8; 32],
    enabled: bool,
}
#[derive(Clone)]
struct HubState {
    credentials: Arc<Vec<Credential>>,
    knowledge: Arc<Mutex<Knowledge>>,
    home_entities: Arc<HashSet<String>>,
    merchants: Arc<Vec<commerce::Merchant>>,
    worker_credentials: Arc<Vec<WorkerCredential>>,
    worker_scopes: Arc<Vec<business_messages::WorkerScope>>,
}

pub fn app(config: Config) -> Result<Router, String> {
    config.validate()?;
    let knowledge = Knowledge::open(&config.database_path)?;
    let worker_credentials: Vec<WorkerCredential> = config
        .whatsapp_workers
        .into_iter()
        .map(|w| WorkerCredential {
            scope: business_messages::WorkerScope {
                worker_id: w.id,
                principal_id: w.principal_id,
                connection_id: w.connection_id,
                waba_id: w.waba_id,
                phone_number_id: w.phone_number_id,
            },
            digest: Sha256::digest(w.token.as_bytes()).into(),
            enabled: w.enabled,
        })
        .collect();
    let worker_scopes = worker_credentials
        .iter()
        .filter(|w| w.enabled)
        .map(|w| w.scope.clone())
        .collect();
    let credentials = config
        .principals
        .into_iter()
        .map(|p| Credential {
            actor: Actor {
                id: p.id,
                role: p.role,
            },
            digest: Sha256::digest(p.token.as_bytes()).into(),
            enabled: p.enabled,
        })
        .collect();
    let state = HubState {
        credentials: Arc::new(credentials),
        knowledge: Arc::new(Mutex::new(knowledge)),
        home_entities: Arc::new(config.home_entities.into_iter().collect()),
        merchants: Arc::new(
            config
                .commerce_merchants
                .iter()
                .map(|m| m.record())
                .collect(),
        ),
        worker_credentials: Arc::new(worker_credentials),
        worker_scopes: Arc::new(worker_scopes),
    };
    let protected = Router::new()
        .merge(personal_routes::routes())
        .merge(commerce_routes::routes())
        .merge(business_routes::paired_routes())
        .route("/v1/capabilities", get(capabilities))
        .route("/v1/documents", get(list_documents).post(create_document))
        .route(
            "/v1/documents/{id}",
            delete(delete_document).put(update_document),
        )
        .route("/v1/documents/{id}/chunks", get(document_chunks))
        .route(
            "/v1/documents/{id}/embeddings",
            post(index_embeddings).layer(DefaultBodyLimit::max(32 * 1024 * 1024)),
        )
        .route("/v1/sources/validate", post(validate_sources))
        .route("/v1/search", post(search))
        .route("/v1/actions", get(list_actions).post(propose_action))
        .route("/v1/actions/{id}/decide", post(decide_action))
        .route("/v1/actions/{id}/claim", post(claim_action))
        .route("/v1/actions/{id}/result", post(finish_action))
        .route_layer(middleware::from_fn_with_state(state.clone(), authenticate));
    Ok(Router::new().route("/health",get(||async {Json(json!({"service":"edith-central-hub","version":env!("CARGO_PKG_VERSION"),"status":"ready"}))}))
        .merge(protected).merge(business_routes::worker_routes().route_layer(middleware::from_fn_with_state(state.clone(),authenticate_worker))).layer(DefaultBodyLimit::max(2_100_000)).with_state(state))
}

async fn authenticate_worker(
    State(state): State<HubState>,
    mut request: Request,
    next: Next,
) -> Response {
    let token = request
        .headers()
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .filter(|t| t.len() <= 256);
    let Some(token) = token else {
        return error(
            StatusCode::UNAUTHORIZED,
            "A configured business-worker credential is required",
        );
    };
    let digest: [u8; 32] = Sha256::digest(token.as_bytes()).into();
    let mut scope = None;
    for worker in state.worker_credentials.iter() {
        if bool::from(digest.ct_eq(&worker.digest)) && worker.enabled {
            scope = Some(worker.scope.clone());
        }
    }
    let Some(scope) = scope else {
        return error(
            StatusCode::UNAUTHORIZED,
            "A configured business-worker credential is required",
        );
    };
    request.extensions_mut().insert(scope);
    next.run(request).await
}

async fn authenticate(State(state): State<HubState>, mut request: Request, next: Next) -> Response {
    let token = request
        .headers()
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "));
    let Some(token) = token.filter(|t| t.len() <= 256) else {
        return error(
            StatusCode::UNAUTHORIZED,
            "A configured principal credential is required",
        );
    };
    let digest: [u8; 32] = Sha256::digest(token.as_bytes()).into();
    let mut actor = None;
    for c in state.credentials.iter() {
        if bool::from(digest.ct_eq(&c.digest)) && c.enabled {
            actor = Some(c.actor.clone());
        }
    }
    let Some(actor) = actor else {
        return error(
            StatusCode::UNAUTHORIZED,
            "A configured principal credential is required",
        );
    };
    request.extensions_mut().insert(actor);
    next.run(request).await
}

fn error(status: StatusCode, message: &str) -> Response {
    (status, Json(json!({"error":message}))).into_response()
}
fn storage_error() -> Response {
    error(
        StatusCode::INTERNAL_SERVER_ERROR,
        "Hub storage operation failed",
    )
}
fn body<T>(value: Result<Json<T>, JsonRejection>) -> Result<T, Response> {
    value.map(|Json(v)| v).map_err(|rejection| {
        if rejection.status() == StatusCode::PAYLOAD_TOO_LARGE {
            error(
                StatusCode::PAYLOAD_TOO_LARGE,
                "Request exceeds this endpoint's size limit",
            )
        } else {
            error(
                StatusCode::BAD_REQUEST,
                "Invalid JSON fields or field types",
            )
        }
    })
}

async fn capabilities(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
) -> Json<Value> {
    Json(
        json!({"principal_id":actor.id,"role":actor.role,"capabilities":{
            "knowledge_search":true,"knowledge_write":actor.role.is_adult(),"knowledge_mode":"scoped_hybrid_fts5_cosine",
            "semantic_embeddings":true,"home_devices":"not_configured","commerce":"not_configured","messages":"not_configured",
            "action_journal":true,"home_power_proposals":actor.role.is_adult()&&!state.home_entities.is_empty()
            ,"reminders":actor.role.is_adult(),"contacts":actor.role.is_adult(),"email_proposals":actor.role.is_adult(),"gmail_connected":false
            ,"whatsapp_business":"not_configured","whatsapp_api_connected":false
            ,"commerce_journal":true,"commerce_proposals":actor.role.is_adult()&&state.merchants.iter().any(|m|m.principal_id==actor.id),"merchant_checkout_connected":false
            ,"business_message_proposals":actor.role.is_adult()&&state.worker_scopes.iter().any(|s|s.principal_id==actor.id),"whatsapp_business_journal":true,"whatsapp_business_connected":false
        }}),
    )
}

fn journal_error(error_value: actions::Error) -> Response {
    match error_value {
        actions::Error::BadRequest(message) => error(StatusCode::BAD_REQUEST, message),
        actions::Error::Forbidden => {
            error(StatusCode::FORBIDDEN, "This home entity is not authorized")
        }
        actions::Error::NotFound => error(StatusCode::NOT_FOUND, "Action not found"),
        actions::Error::Conflict(message) => error(StatusCode::CONFLICT, message),
        actions::Error::Storage => storage_error(),
    }
}
fn journal_response(result: Result<actions::Action, actions::Error>) -> Response {
    match result {
        Ok(action) => Json(action).into_response(),
        Err(e) => journal_error(e),
    }
}
fn action_role(actor: &Actor) -> Result<(), Response> {
    if actor.role.is_adult() {
        Ok(())
    } else {
        Err(error(
            StatusCode::FORBIDDEN,
            "This principal cannot access home actions",
        ))
    }
}
async fn list_actions(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
) -> Response {
    if let Err(e) = action_role(&actor) {
        return e;
    }
    let Ok(store) = state.knowledge.lock() else {
        return storage_error();
    };
    match store.list_actions(&actor.id) {
        Ok(actions) => Json(json!({"actions":actions})).into_response(),
        Err(e) => journal_error(e),
    }
}
async fn propose_action(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
    payload: Result<Json<actions::Proposal>, JsonRejection>,
) -> Response {
    if let Err(e) = action_role(&actor) {
        return e;
    }
    let input = match body(payload) {
        Ok(v) => v,
        Err(e) => return e,
    };
    let Ok(mut store) = state.knowledge.lock() else {
        return storage_error();
    };
    match store.propose(&actor.id, input, &state.home_entities) {
        Ok((action, created)) => (
            if created {
                StatusCode::CREATED
            } else {
                StatusCode::OK
            },
            Json(action),
        )
            .into_response(),
        Err(e) => journal_error(e),
    }
}
async fn decide_action(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
    Path(id): Path<String>,
    payload: Result<Json<actions::Decide>, JsonRejection>,
) -> Response {
    if let Err(e) = action_role(&actor) {
        return e;
    }
    let Ok(mut store) = state.knowledge.lock() else {
        return storage_error();
    };
    if let Err(e) = store.action(&actor.id, &id) {
        return journal_error(e);
    }
    let input = match body(payload) {
        Ok(v) => v,
        Err(e) => return e,
    };
    journal_response(store.decide(&actor.id, &id, input, &state.home_entities))
}
async fn claim_action(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
    Path(id): Path<String>,
) -> Response {
    if let Err(e) = action_role(&actor) {
        return e;
    }
    let Ok(mut store) = state.knowledge.lock() else {
        return storage_error();
    };
    journal_response(store.claim(&actor.id, &id, &state.home_entities))
}
async fn finish_action(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
    Path(id): Path<String>,
    payload: Result<Json<actions::Completion>, JsonRejection>,
) -> Response {
    if let Err(e) = action_role(&actor) {
        return e;
    }
    let Ok(mut store) = state.knowledge.lock() else {
        return storage_error();
    };
    if let Err(e) = store.action(&actor.id, &id) {
        return journal_error(e);
    }
    let input = match body(payload) {
        Ok(v) => v,
        Err(e) => return e,
    };
    journal_response(store.finish(&actor.id, &id, input))
}
async fn create_document(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
    payload: Result<Json<CreateDocument>, JsonRejection>,
) -> Response {
    if !actor.role.is_adult() {
        return error(
            StatusCode::FORBIDDEN,
            "This principal cannot create documents",
        );
    }
    let input = match body(payload) {
        Ok(v) => v,
        Err(e) => return e,
    };
    if let Err(message) = input.validate() {
        return error(StatusCode::BAD_REQUEST, message);
    }
    let Ok(mut store) = state.knowledge.lock() else {
        return storage_error();
    };
    match store.create(&actor.id, input) {
        Ok(doc) => (StatusCode::CREATED, Json(doc)).into_response(),
        Err(_) => storage_error(),
    }
}
async fn list_documents(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
) -> Response {
    let Ok(store) = state.knowledge.lock() else {
        return storage_error();
    };
    match store.list(&actor.id, actor.role) {
        Ok(documents) => Json(json!({"documents":documents})).into_response(),
        Err(_) => storage_error(),
    }
}
async fn delete_document(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
    Path(id): Path<String>,
) -> Response {
    if !actor.role.is_adult() {
        return error(StatusCode::NOT_FOUND, "Document not found");
    }
    let Ok(mut store) = state.knowledge.lock() else {
        return storage_error();
    };
    match store.delete(&actor.id, &id) {
        Ok(true) => Json(json!({"deleted":true})).into_response(),
        Ok(false) => error(StatusCode::NOT_FOUND, "Document not found"),
        Err(_) => storage_error(),
    }
}
async fn search(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
    payload: Result<Json<Search>, JsonRejection>,
) -> Response {
    let input = match body(payload) {
        Ok(v) => v,
        Err(e) => return e,
    };
    if let Err(message) = input.validate() {
        return error(StatusCode::BAD_REQUEST, message);
    }
    let Ok(store) = state.knowledge.lock() else {
        return storage_error();
    };
    match store.search(&actor.id, actor.role, &input) {
        Ok(result) => Json(result).into_response(),
        Err(e) => knowledge_error(e),
    }
}

fn knowledge_error(error_value: semantic::Error) -> Response {
    match error_value {
        semantic::Error::BadRequest(message) => error(StatusCode::BAD_REQUEST, message),
        semantic::Error::NotFound => error(StatusCode::NOT_FOUND, "Document not found"),
        semantic::Error::Conflict(message) => error(StatusCode::CONFLICT, message),
        semantic::Error::Storage => storage_error(),
    }
}
async fn document_chunks(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
    Path(id): Path<String>,
) -> Response {
    if !actor.role.is_adult() {
        return error(StatusCode::NOT_FOUND, "Document not found");
    }
    let Ok(store) = state.knowledge.lock() else {
        return storage_error();
    };
    match store.chunks(&actor.id, &id) {
        Ok(result) => Json(result).into_response(),
        Err(e) => knowledge_error(e),
    }
}
async fn index_embeddings(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
    Path(id): Path<String>,
    payload: Result<Json<semantic::Upload>, JsonRejection>,
) -> Response {
    if !actor.role.is_adult() {
        return error(StatusCode::NOT_FOUND, "Document not found");
    }
    let Ok(mut store) = state.knowledge.lock() else {
        return storage_error();
    };
    if let Err(e) = store.owned_document(&actor.id, &id) {
        return knowledge_error(e);
    }
    let input = match body(payload) {
        Ok(v) => v,
        Err(e) => return e,
    };
    match store.index_embeddings(&actor.id, &id, input) {
        Ok(result) => Json(result).into_response(),
        Err(e) => knowledge_error(e),
    }
}
async fn update_document(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
    Path(id): Path<String>,
    payload: Result<Json<semantic::UpdateDocument>, JsonRejection>,
) -> Response {
    if !actor.role.is_adult() {
        return error(StatusCode::NOT_FOUND, "Document not found");
    }
    let Ok(mut store) = state.knowledge.lock() else {
        return storage_error();
    };
    if let Err(e) = store.owned_document(&actor.id, &id) {
        return knowledge_error(e);
    }
    let input = match body(payload) {
        Ok(v) => v,
        Err(e) => return e,
    };
    match store.update_document(&actor.id, &id, input) {
        Ok(result) => Json(result).into_response(),
        Err(e) => knowledge_error(e),
    }
}
async fn validate_sources(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
    payload: Result<Json<semantic::ValidateSources>, JsonRejection>,
) -> Response {
    let input = match body(payload) {
        Ok(v) => v,
        Err(e) => return e,
    };
    let Ok(store) = state.knowledge.lock() else {
        return storage_error();
    };
    match store.validate_sources(&actor.id, actor.role, input) {
        Ok(sources) => Json(json!({"sources":sources})).into_response(),
        Err(e) => knowledge_error(e),
    }
}
