use super::*;
use business_messages::*;

pub fn paired_routes() -> Router<HubState> {
    Router::new()
        .route("/v1/whatsapp-bindings", get(bindings))
        .route("/v1/whatsapp-bindings/{id}", get(binding))
        .route("/v1/whatsapp-permissions", get(permissions))
        .route("/v1/business-messages", get(messages).post(propose))
        .route("/v1/business-messages/{id}", get(message))
        .route("/v1/business-messages/{id}/decide", post(decide))
}
pub fn worker_routes() -> Router<HubState> {
    Router::new()
        .route("/v1/whatsapp-worker/capabilities", get(worker_capabilities))
        .route(
            "/v1/whatsapp-worker/bindings",
            get(worker_bindings).post(sync_binding),
        )
        .route("/v1/whatsapp-worker/bindings/{id}", get(worker_binding))
        .route(
            "/v1/whatsapp-worker/permissions",
            get(worker_permissions).post(set_permission),
        )
        .route("/v1/whatsapp-worker/messages", get(worker_messages))
        .route("/v1/whatsapp-worker/messages/{id}", get(worker_message))
        .route("/v1/whatsapp-worker/messages/{id}/claim", post(claim))
        .route(
            "/v1/whatsapp-worker/messages/{id}/authorize",
            post(authorize),
        )
        .route("/v1/whatsapp-worker/messages/{id}/result", post(complete))
}
fn adult(actor: &Actor) -> Result<(), Response> {
    if actor.role.is_adult() {
        Ok(())
    } else {
        Err(error(
            StatusCode::FORBIDDEN,
            "This principal cannot access business-message records",
        ))
    }
}
fn failure(e: actions::Error) -> Response {
    match e {
        actions::Error::NotFound => error(StatusCode::NOT_FOUND, "Business record not found"),
        actions::Error::Forbidden => error(
            StatusCode::FORBIDDEN,
            "This worker cannot access that connection",
        ),
        other => journal_error(other),
    }
}
fn response(result: Result<Value, actions::Error>) -> Response {
    match result {
        Ok(v) => Json(v).into_response(),
        Err(e) => failure(e),
    }
}
fn change<T>(
    state: &HubState,
    input: Result<Json<T>, JsonRejection>,
    f: impl FnOnce(&mut Knowledge, T) -> Result<(Value, StatusCode), actions::Error>,
) -> Response {
    let input = match body(input) {
        Ok(v) => v,
        Err(e) => return e,
    };
    let Ok(mut store) = state.knowledge.lock() else {
        return storage_error();
    };
    match f(&mut store, input) {
        Ok((v, s)) => (s, Json(v)).into_response(),
        Err(e) => failure(e),
    }
}
async fn worker_capabilities(Extension(w): Extension<WorkerScope>) -> Json<Value> {
    Json(
        json!({"worker_id":w.worker_id,"principal_id":w.principal_id,"connection_id":w.connection_id,"waba_id":w.waba_id,"phone_number_id":w.phone_number_id,"capabilities":{"binding_write":true,"permission_write":true,"message_claim":true,"message_result":true}}),
    )
}
async fn bindings(State(state): State<HubState>, Extension(actor): Extension<Actor>) -> Response {
    if let Err(e) = adult(&actor) {
        return e;
    }
    let Ok(s) = state.knowledge.lock() else {
        return storage_error();
    };
    response(
        s.business_bindings(&actor.id, None)
            .map(|v| json!({"bindings":v})),
    )
}
async fn binding(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
    Path(id): Path<String>,
) -> Response {
    if let Err(e) = adult(&actor) {
        return e;
    }
    let Ok(s) = state.knowledge.lock() else {
        return storage_error();
    };
    response(s.business_binding(&actor.id, &id, None).map(|v| json!(v)))
}
async fn worker_bindings(
    State(state): State<HubState>,
    Extension(w): Extension<WorkerScope>,
) -> Response {
    let Ok(s) = state.knowledge.lock() else {
        return storage_error();
    };
    response(
        s.worker_business_bindings(&w)
            .map(|v| json!({"bindings":v})),
    )
}
async fn worker_binding(
    State(state): State<HubState>,
    Extension(w): Extension<WorkerScope>,
    Path(id): Path<String>,
) -> Response {
    let Ok(s) = state.knowledge.lock() else {
        return storage_error();
    };
    response(s.worker_business_binding(&w, &id).map(|v| json!(v)))
}
async fn sync_binding(
    State(state): State<HubState>,
    Extension(w): Extension<WorkerScope>,
    input: Result<Json<BindingInput>, JsonRejection>,
) -> Response {
    change(&state, input, |s, p| {
        s.sync_business_binding(&w, p, personal::now())
            .map(|(v, new)| {
                (
                    json!(v),
                    if new {
                        StatusCode::CREATED
                    } else {
                        StatusCode::OK
                    },
                )
            })
    })
}
async fn permissions(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
) -> Response {
    if let Err(e) = adult(&actor) {
        return e;
    }
    let Ok(s) = state.knowledge.lock() else {
        return storage_error();
    };
    response(
        s.business_permissions(&actor.id, None)
            .map(|v| json!({"permissions":v})),
    )
}
async fn worker_permissions(
    State(state): State<HubState>,
    Extension(w): Extension<WorkerScope>,
) -> Response {
    let Ok(s) = state.knowledge.lock() else {
        return storage_error();
    };
    response(
        s.worker_business_permissions(&w)
            .map(|v| json!({"permissions":v})),
    )
}
async fn set_permission(
    State(state): State<HubState>,
    Extension(w): Extension<WorkerScope>,
    input: Result<Json<PermissionInput>, JsonRejection>,
) -> Response {
    change(&state, input, |s, p| {
        s.set_business_permission(&w, p, personal::now())
            .map(|(v, new)| {
                (
                    json!(v),
                    if new {
                        StatusCode::CREATED
                    } else {
                        StatusCode::OK
                    },
                )
            })
    })
}
async fn messages(State(state): State<HubState>, Extension(actor): Extension<Actor>) -> Response {
    if let Err(e) = adult(&actor) {
        return e;
    }
    let Ok(s) = state.knowledge.lock() else {
        return storage_error();
    };
    response(
        s.business_messages(&actor.id, None)
            .map(|v| json!({"messages":v.iter().map(Message::view).collect::<Vec<_>>()})),
    )
}
async fn worker_messages(
    State(state): State<HubState>,
    Extension(w): Extension<WorkerScope>,
) -> Response {
    let Ok(s) = state.knowledge.lock() else {
        return storage_error();
    };
    response(
        s.worker_business_messages(&w)
            .map(|v| json!({"messages":v.iter().map(Message::view).collect::<Vec<_>>()})),
    )
}
async fn message(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
    Path(id): Path<String>,
) -> Response {
    if let Err(e) = adult(&actor) {
        return e;
    }
    let Ok(s) = state.knowledge.lock() else {
        return storage_error();
    };
    response(s.business_message(&actor.id, &id).map(|v| v.view()))
}
async fn worker_message(
    State(state): State<HubState>,
    Extension(w): Extension<WorkerScope>,
    Path(id): Path<String>,
) -> Response {
    let Ok(s) = state.knowledge.lock() else {
        return storage_error();
    };
    response(s.worker_business_message(&w, &id).map(|v| v.view()))
}
async fn propose(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
    input: Result<Json<Proposal>, JsonRejection>,
) -> Response {
    if let Err(e) = adult(&actor) {
        return e;
    }
    change(&state, input, |s, p| {
        s.propose_business_message(&actor.id, p, &state.worker_scopes, personal::now())
            .map(|(v, new)| {
                (
                    v.view(),
                    if new {
                        StatusCode::CREATED
                    } else {
                        StatusCode::OK
                    },
                )
            })
    })
}
async fn decide(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
    Path(id): Path<String>,
    input: Result<Json<actions::Decide>, JsonRejection>,
) -> Response {
    if let Err(e) = adult(&actor) {
        return e;
    }
    change(&state, input, |s, p| {
        s.decide_business_message(&actor.id, &id, p, &state.worker_scopes, personal::now())
            .map(|v| (v.view(), StatusCode::OK))
    })
}
async fn claim(
    State(state): State<HubState>,
    Extension(w): Extension<WorkerScope>,
    Path(id): Path<String>,
    input: Result<Json<Claim>, JsonRejection>,
) -> Response {
    change(&state, input, |s, p| {
        s.claim_business_message(&w, &id, p, &state.worker_scopes, personal::now())
            .map(|v| (v.view(), StatusCode::OK))
    })
}
async fn authorize(
    State(state): State<HubState>,
    Extension(w): Extension<WorkerScope>,
    Path(id): Path<String>,
    input: Result<Json<Claim>, JsonRejection>,
) -> Response {
    change(&state, input, |s, p| {
        s.authorize_business_message(&w, &id, p, &state.worker_scopes, personal::now())
            .map(|v| {
                (
                    json!({"authorized":true,"message":v.view()}),
                    StatusCode::OK,
                )
            })
    })
}
async fn complete(
    State(state): State<HubState>,
    Extension(w): Extension<WorkerScope>,
    Path(id): Path<String>,
    input: Result<Json<Completion>, JsonRejection>,
) -> Response {
    change(&state, input, |s, p| {
        s.complete_business_message(&w, &id, p)
            .map(|v| (v.view(), StatusCode::OK))
    })
}
