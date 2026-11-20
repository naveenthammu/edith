use super::*;
use personal::*;
use serde::Deserialize;

pub fn routes() -> Router<HubState> {
    Router::new()
        .route("/v1/reminders", get(reminders).post(create_reminder))
        .route("/v1/reminders/claim-due", post(claim_due))
        .route("/v1/reminders/{id}/cancel", post(cancel_reminder))
        .route("/v1/reminders/{id}/acknowledge", post(ack_reminder))
        .route("/v1/reminders/{id}/delivery", post(deliver_reminder))
        .route("/v1/contacts", get(contacts).post(create_contact))
        .route(
            "/v1/contacts/{id}",
            delete(delete_contact).put(update_contact),
        )
        .route("/v1/messages", get(messages).post(propose_message))
        .route("/v1/messages/{id}", get(message))
        .route("/v1/messages/{id}/decide", post(decide_message))
        .route("/v1/messages/{id}/claim", post(claim_message))
        .route("/v1/messages/{id}/result", post(finish_message))
}
fn failure(e: actions::Error) -> Response {
    match e {
        actions::Error::NotFound => error(StatusCode::NOT_FOUND, "Personal record not found"),
        other => journal_error(other),
    }
}
fn adult(actor: &Actor) -> Result<(), Response> {
    if actor.role.is_adult() {
        Ok(())
    } else {
        Err(error(
            StatusCode::FORBIDDEN,
            "This principal cannot access personal records",
        ))
    }
}
fn response(result: Result<Value, actions::Error>) -> Response {
    match result {
        Ok(value) => Json(value).into_response(),
        Err(e) => failure(e),
    }
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Empty {}

async fn reminders(State(state): State<HubState>, Extension(actor): Extension<Actor>) -> Response {
    if let Err(e) = adult(&actor) {
        return e;
    }
    let Ok(store) = state.knowledge.lock() else {
        return storage_error();
    };
    response(
        store.reminders(&actor.id).map(
            |items| json!({"reminders":items.iter().map(|v|v.view(false)).collect::<Vec<_>>()}),
        ),
    )
}
async fn create_reminder(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
    input: Result<Json<NewReminder>, JsonRejection>,
) -> Response {
    if let Err(e) = adult(&actor) {
        return e;
    }
    let input = match body(input) {
        Ok(v) => v,
        Err(e) => return e,
    };
    let Ok(mut store) = state.knowledge.lock() else {
        return storage_error();
    };
    match store.create_reminder(&actor.id, input, now()) {
        Ok((value, created)) => (
            if created {
                StatusCode::CREATED
            } else {
                StatusCode::OK
            },
            Json(value.view(false)),
        )
            .into_response(),
        Err(e) => failure(e),
    }
}
async fn claim_due(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
    input: Result<Json<Empty>, JsonRejection>,
) -> Response {
    if let Err(e) = adult(&actor) {
        return e;
    }
    if let Err(e) = body(input) {
        return e;
    }
    let Ok(mut store) = state.knowledge.lock() else {
        return storage_error();
    };
    response(
        store
            .claim_due(&actor.id, now())
            .map(|value| json!({"reminder":value.map(|v|v.view(true))})),
    )
}
fn close(
    state: HubState,
    actor: Actor,
    id: String,
    input: Result<Json<Empty>, JsonRejection>,
    ack: bool,
) -> Response {
    if let Err(e) = adult(&actor) {
        return e;
    }
    if let Err(e) = body(input) {
        return e;
    }
    let Ok(mut store) = state.knowledge.lock() else {
        return storage_error();
    };
    response(
        store
            .close_reminder(&actor.id, &id, ack, now())
            .map(|v| v.view(false)),
    )
}
async fn cancel_reminder(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
    Path(id): Path<String>,
    input: Result<Json<Empty>, JsonRejection>,
) -> Response {
    close(state, actor, id, input, false)
}
async fn ack_reminder(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
    Path(id): Path<String>,
    input: Result<Json<Empty>, JsonRejection>,
) -> Response {
    close(state, actor, id, input, true)
}
async fn deliver_reminder(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
    Path(id): Path<String>,
    input: Result<Json<Delivery>, JsonRejection>,
) -> Response {
    if let Err(e) = adult(&actor) {
        return e;
    }
    let input = match body(input) {
        Ok(v) => v,
        Err(e) => return e,
    };
    let Ok(mut store) = state.knowledge.lock() else {
        return storage_error();
    };
    response(
        store
            .reminder_delivery(&actor.id, &id, input, now())
            .map(|v| v.view(false)),
    )
}
async fn contacts(State(state): State<HubState>, Extension(actor): Extension<Actor>) -> Response {
    if let Err(e) = adult(&actor) {
        return e;
    }
    let Ok(store) = state.knowledge.lock() else {
        return storage_error();
    };
    response(
        store
            .contacts(&actor.id)
            .map(|items| json!({"contacts":items})),
    )
}
async fn create_contact(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
    input: Result<Json<NewContact>, JsonRejection>,
) -> Response {
    if let Err(e) = adult(&actor) {
        return e;
    }
    let input = match body(input) {
        Ok(v) => v,
        Err(e) => return e,
    };
    let Ok(mut store) = state.knowledge.lock() else {
        return storage_error();
    };
    match store.create_contact(&actor.id, input, now()) {
        Ok(value) => (StatusCode::CREATED, Json(value)).into_response(),
        Err(e) => failure(e),
    }
}
async fn update_contact(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
    Path(id): Path<String>,
    input: Result<Json<UpdateContact>, JsonRejection>,
) -> Response {
    if let Err(e) = adult(&actor) {
        return e;
    }
    let input = match body(input) {
        Ok(v) => v,
        Err(e) => return e,
    };
    let Ok(mut store) = state.knowledge.lock() else {
        return storage_error();
    };
    response(
        store
            .update_contact(&actor.id, &id, input, now())
            .map(|v| json!(v)),
    )
}
async fn delete_contact(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
    Path(id): Path<String>,
) -> Response {
    if let Err(e) = adult(&actor) {
        return e;
    }
    let Ok(mut store) = state.knowledge.lock() else {
        return storage_error();
    };
    response(
        store
            .delete_contact(&actor.id, &id)
            .map(|_| json!({"deleted":true})),
    )
}
async fn messages(State(state): State<HubState>, Extension(actor): Extension<Actor>) -> Response {
    if let Err(e) = adult(&actor) {
        return e;
    }
    let Ok(store) = state.knowledge.lock() else {
        return storage_error();
    };
    response(
        store
            .messages(&actor.id)
            .map(|items| json!({"messages":items.iter().map(Message::view).collect::<Vec<_>>()})),
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
    let Ok(store) = state.knowledge.lock() else {
        return storage_error();
    };
    response(store.message(&actor.id, &id).map(|v| v.view()))
}
async fn propose_message(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
    input: Result<Json<NewMessage>, JsonRejection>,
) -> Response {
    if let Err(e) = adult(&actor) {
        return e;
    }
    let input = match body(input) {
        Ok(v) => v,
        Err(e) => return e,
    };
    let Ok(mut store) = state.knowledge.lock() else {
        return storage_error();
    };
    match store.propose_message(&actor.id, input, now()) {
        Ok((value, created)) => (
            if created {
                StatusCode::CREATED
            } else {
                StatusCode::OK
            },
            Json(value.view()),
        )
            .into_response(),
        Err(e) => failure(e),
    }
}
async fn decide_message(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
    Path(id): Path<String>,
    input: Result<Json<actions::Decide>, JsonRejection>,
) -> Response {
    if let Err(e) = adult(&actor) {
        return e;
    }
    let input = match body(input) {
        Ok(v) => v,
        Err(e) => return e,
    };
    let Ok(mut store) = state.knowledge.lock() else {
        return storage_error();
    };
    response(
        store
            .decide_message(&actor.id, &id, input, now())
            .map(|v| v.view()),
    )
}
async fn claim_message(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
    Path(id): Path<String>,
    input: Result<Json<Empty>, JsonRejection>,
) -> Response {
    if let Err(e) = adult(&actor) {
        return e;
    }
    if let Err(e) = body(input) {
        return e;
    }
    let Ok(mut store) = state.knowledge.lock() else {
        return storage_error();
    };
    response(store.claim_message(&actor.id, &id, now()).map(|v| v.view()))
}
async fn finish_message(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
    Path(id): Path<String>,
    input: Result<Json<MessageResult>, JsonRejection>,
) -> Response {
    if let Err(e) = adult(&actor) {
        return e;
    }
    let input = match body(input) {
        Ok(v) => v,
        Err(e) => return e,
    };
    let Ok(mut store) = state.knowledge.lock() else {
        return storage_error();
    };
    response(
        store
            .finish_message(&actor.id, &id, input)
            .map(|v| v.view()),
    )
}
