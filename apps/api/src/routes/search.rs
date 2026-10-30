//! T006 server-side search route.
use axum::{
    extract::{Query, State},
    response::Response,
    Extension,
};

use crate::{
    middleware::auth::AuthContext,
    search::{execute_search_http, SearchError, SearchQueryParams},
    state::ApiState,
};

/// GET /search
///
/// Authorization comes from the request's canonical AuthContext and is applied
/// against PostgreSQL domain state before retrieval. T006 v1 returns a bounded
/// top-10 ranking and deliberately does not claim offset pagination.
pub async fn search_nodes(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
    Query(params): Query<SearchQueryParams>,
) -> Result<Response, SearchError> {
    execute_search_http(&state, &auth, params, None).await
}
