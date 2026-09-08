use std::time::Instant;

use axum::{
    extract::{Request, State},
    http::{Method, StatusCode},
    middleware::Next,
    response::{IntoResponse, Response},
    Json,
};
use axum_extra::extract::cookie::CookieJar;

use crate::{config::DomainReadSource, routes::auth::SESSION_COOKIE_NAME, state::ApiState};

/// Keep PostgreSQL-backed requests on one internally consistent process-local
/// projection. Mutating requests and authenticated requests require the current
/// committed generation. Anonymous GET/HEAD requests may use the previous complete
/// generation while another request rebuilds the next snapshot, or during the exact
/// +1 post-commit handoff of a serialized local node write. Keeping requests with
/// the canonical session cookie
/// strict is security-sensitive because auth middleware reads account disabled/role
/// state from this projection after this middleware runs. Canonical request
/// authentication is currently cookie-only; adding any other inbound auth scheme
/// requires updating this classifier before that scheme can be enabled. The read guard remains
/// held for the full handler so no request can observe a partially replaced
/// accounts/nodes/edges projection.
fn may_use_previous_projection(method: &Method, has_session_cookie: bool) -> bool {
    matches!(*method, Method::GET | Method::HEAD) && !has_session_cookie
}

pub async fn ensure_current_domain_projection(
    State(state): State<ApiState>,
    jar: CookieJar,
    request: Request,
    next: Next,
) -> Response {
    if state.config.domain_read_source != DomainReadSource::Postgres {
        return next.run(request).await;
    }

    let has_session_cookie = jar.get(SESSION_COOKIE_NAME).is_some();
    let refresh = if may_use_previous_projection(request.method(), has_session_cookie) {
        state.refresh_domain_projection_for_read().await
    } else {
        state.refresh_domain_projection_if_stale().await
    };
    if let Err(error) = refresh {
        state.metrics.domain_projection_refresh_failed();
        tracing::error!(
            event = "domain.projection_refresh_failed",
            error = %error,
            "PostgreSQL domain projection could not be refreshed"
        );
        let body = serde_json::json!({
            "error": "DOMAIN_PROJECTION_UNAVAILABLE",
        });
        return (StatusCode::SERVICE_UNAVAILABLE, Json(body)).into_response();
    }

    let read_gate_wait_started = Instant::now();
    let _projection_read = state.domain_projection_gate.read().await;
    state
        .metrics
        .observe_domain_projection_read_gate_wait(read_gate_wait_started.elapsed());
    next.run(request).await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_anonymous_safe_reads_may_use_previous_projection() {
        assert!(may_use_previous_projection(&Method::GET, false));
        assert!(may_use_previous_projection(&Method::HEAD, false));

        assert!(!may_use_previous_projection(&Method::GET, true));
        assert!(!may_use_previous_projection(&Method::HEAD, true));
        assert!(!may_use_previous_projection(&Method::POST, false));
        assert!(!may_use_previous_projection(&Method::PATCH, false));
        assert!(!may_use_previous_projection(&Method::DELETE, false));
    }
}
