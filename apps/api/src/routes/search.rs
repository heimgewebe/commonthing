//! T006 server-side search route.
use std::net::SocketAddr;

use axum::{
    extract::{ConnectInfo, FromRequestParts, Query, State},
    http::{request::Parts, HeaderMap},
    response::Response,
    Extension,
};

use crate::{
    auth::rate_limit::RateLimitError,
    middleware::auth::AuthContext,
    routes::auth::client_ip_or_peer,
    search::{execute_search_http, SearchError, SearchQueryParams},
    state::ApiState,
    telemetry::SearchRequestOutcome,
};

/// Transport peer if the server was started with connect info.
///
/// The production listener always provides it (`into_make_service_with_connect_info`).
/// Router-level tests that do not install `MockConnectInfo` get `None`, and the
/// per-IP search limit is then skipped instead of failing the request.
pub struct OptionalPeer(pub Option<SocketAddr>);

impl<S: Send + Sync> FromRequestParts<S> for OptionalPeer {
    type Rejection = std::convert::Infallible;

    async fn from_request_parts(parts: &mut Parts, state: &S) -> Result<Self, Self::Rejection> {
        Ok(Self(
            ConnectInfo::<SocketAddr>::from_request_parts(parts, state)
                .await
                .ok()
                .map(|ConnectInfo(addr)| addr),
        ))
    }
}

/// GET /search
///
/// Authorization comes from the request's canonical AuthContext and is applied
/// against PostgreSQL domain state before retrieval. T006 v1 returns a bounded
/// top-10 ranking and deliberately does not claim offset pagination.
///
/// The route is reachable anonymously and every request reads candidate rows
/// from PostgreSQL, so it is capped per client IP (`SEARCH_RL_IP_PER_MIN`,
/// `SEARCH_RL_IP_PER_HOUR`) before any search work starts.
pub async fn search_nodes(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
    OptionalPeer(peer): OptionalPeer,
    headers: HeaderMap,
    Query(params): Query<SearchQueryParams>,
) -> Result<Response, SearchError> {
    if let Some(peer) = peer {
        let client_ip = client_ip_or_peer(peer, &headers, "search");
        if let Err(error) = state.rate_limiter.check_search(client_ip) {
            state
                .metrics
                .search_request_outcome(SearchRequestOutcome::RateLimited);
            return Err(match error {
                RateLimitError::SearchIpLimited { retry_after } => SearchError::RateLimited {
                    retry_after_secs: retry_after.as_secs().saturating_add(1),
                },
                _ => SearchError::Internal,
            });
        }
    }
    execute_search_http(&state, &auth, params, None).await
}
