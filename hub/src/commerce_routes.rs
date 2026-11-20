use super::*;
use commerce::*;
use serde::Deserialize;

pub fn routes() -> Router<HubState> {
    Router::new()
        .route("/v1/merchants", get(merchants))
        .route("/v1/commerce", get(carts).post(propose))
        .route("/v1/commerce/{id}", get(cart))
        .route("/v1/commerce/{id}/decide", post(decide))
        .route("/v1/commerce/{id}/claim", post(claim))
        .route("/v1/commerce/{id}/result", post(finish))
        .route("/v1/commerce/{id}/checkout", get(checkout))
        .route("/v1/commerce/{id}/checkout/claim", post(open_claim))
        .route("/v1/commerce/{id}/checkout/result", post(open_finish))
}
fn adult(actor: &Actor) -> Result<(), Response> {
    if actor.role.is_adult() {
        Ok(())
    } else {
        Err(error(
            StatusCode::FORBIDDEN,
            "This principal cannot access commerce records",
        ))
    }
}
fn failure(e: actions::Error) -> Response {
    match e {
        actions::Error::NotFound => error(StatusCode::NOT_FOUND, "Commerce record not found"),
        other => journal_error(other),
    }
}
fn response(result: Result<Value, actions::Error>) -> Response {
    match result {
        Ok(v) => Json(v).into_response(),
        Err(e) => failure(e),
    }
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Empty {}
fn change<T>(
    state: &HubState,
    actor: &Actor,
    input: Result<Json<T>, JsonRejection>,
    f: impl FnOnce(&mut Knowledge, T) -> Result<(Value, StatusCode), actions::Error>,
) -> Response {
    if let Err(e) = adult(actor) {
        return e;
    }
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
async fn merchants(State(state): State<HubState>, Extension(actor): Extension<Actor>) -> Response {
    if let Err(e) = adult(&actor) {
        return e;
    }
    Json(json!({"merchants":state.merchants.iter().filter(|m|m.principal_id==actor.id).collect::<Vec<_>>()})).into_response()
}
async fn carts(State(state): State<HubState>, Extension(actor): Extension<Actor>) -> Response {
    if let Err(e) = adult(&actor) {
        return e;
    }
    let Ok(store) = state.knowledge.lock() else {
        return storage_error();
    };
    response(
        store
            .carts(&actor.id)
            .map(|items| json!({"commerce":items.iter().map(Cart::view).collect::<Vec<_>>()})),
    )
}
async fn cart(
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
    response(store.cart(&actor.id, &id).map(|v| v.view()))
}
async fn checkout(
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
    response(
        store
            .checkout(&actor.id, &id, &state.merchants, personal::now())
            .map(|v| v.checkout_view()),
    )
}
async fn propose(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
    input: Result<Json<Proposal>, JsonRejection>,
) -> Response {
    change(&state, &actor, input, |s, p| {
        s.propose_cart(&actor.id, p, &state.merchants, personal::now())
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
    change(&state, &actor, input, |s, p| {
        s.decide_cart(&actor.id, &id, p, &state.merchants, personal::now())
            .map(|v| (v.view(), StatusCode::OK))
    })
}
async fn claim(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
    Path(id): Path<String>,
    input: Result<Json<Empty>, JsonRejection>,
) -> Response {
    change(&state, &actor, input, |s, _| {
        s.claim_cart(&actor.id, &id, &state.merchants, personal::now())
            .map(|v| (v.view(), StatusCode::OK))
    })
}
async fn finish(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
    Path(id): Path<String>,
    input: Result<Json<CartResult>, JsonRejection>,
) -> Response {
    change(&state, &actor, input, |s, p| {
        s.finish_cart(&actor.id, &id, p, &state.merchants)
            .map(|v| (v.view(), StatusCode::OK))
    })
}
async fn open_claim(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
    Path(id): Path<String>,
    input: Result<Json<OpenClaim>, JsonRejection>,
) -> Response {
    change(&state, &actor, input, |s, p| {
        s.claim_checkout(&actor.id, &id, p, &state.merchants, personal::now())
            .map(|v| (v.checkout_view(), StatusCode::OK))
    })
}
async fn open_finish(
    State(state): State<HubState>,
    Extension(actor): Extension<Actor>,
    Path(id): Path<String>,
    input: Result<Json<OpenResult>, JsonRejection>,
) -> Response {
    change(&state, &actor, input, |s, p| {
        s.finish_checkout(&actor.id, &id, p)
            .map(|v| (v.view(), StatusCode::OK))
    })
}
