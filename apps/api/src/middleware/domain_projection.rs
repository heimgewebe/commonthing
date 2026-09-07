use std::time::Instant;

use axum::{
    extract::{Request, State},
    http::{Method, StatusCode},
    middleware::Next,
    response::{IntoResponse, Response},
    Json,
};

use crate::{config::DomainReadSource, state::ApiState};

/// Keep PostgreSQL-backed requests on one internally consistent process-local
/// projection. Mutating requests require the current committed generation. Safe
/// GET/HEAD requests may use the previous complete generation only while another
/// request is actively rebuilding the next snapshot outside the request gate.
/// The read guard remains held for the full handler so no request can observe a
/// partially replaced accounts/nodes/edges projection.
pub async fn ensure_current_domain_projection(
    State(state): State<ApiState>,
    request: Request,
    next: Next,
) -> Response {
    if state.config.domain_read_source != DomainReadSource::Postgres {
        return next.run(request).await;
    }

    let refresh = if matches!(*request.method(), Method::GET | Method::HEAD) {
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
