use std::time::Instant;

use axum::{
    extract::{Request, State},
    http::StatusCode,
    middleware::Next,
    response::{IntoResponse, Response},
    Json,
};

use crate::{config::DomainReadSource, state::ApiState};

/// Ensure every PostgreSQL-backed request observes a process-local projection
/// from the current committed database generation. The read guard remains held
/// for the full request so another task cannot replace only part of the
/// projection while a handler is reading multiple aggregate types.
pub async fn ensure_current_domain_projection(
    State(state): State<ApiState>,
    request: Request,
    next: Next,
) -> Response {
    if state.config.domain_read_source != DomainReadSource::Postgres {
        return next.run(request).await;
    }

    if let Err(error) = state.refresh_domain_projection_if_stale().await {
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
