use std::future::Future;
use std::sync::atomic::{AtomicI64, Ordering};
use std::time::{Duration, Instant};

use sqlx::{PgPool, Postgres, Transaction};
use tokio::sync::{RwLockReadGuard, Semaphore};

use axum::{
    extract::{Request, State},
    http::{Method, StatusCode},
    middleware::Next,
    response::{IntoResponse, Response},
    Json,
};
use axum_extra::extract::cookie::CookieJar;

use crate::{config::DomainReadSource, routes::auth::SESSION_COOKIE_NAME, state::ApiState};

static PRIVACY_READ_SLOTS: Semaphore =
    Semaphore::const_new((crate::DATABASE_POOL_MAX_CONNECTIONS as usize - 1) / 2);

// Only fixed-size domain JSON reads get the cross-process database fence.
// The search endpoint already authorizes against domain_nodes in its SQL query.
// Mutations MUST NOT hold a shared fence while their own transactions update
// domain_projection_state, or they would deadlock themselves.
fn privacy_sensitive_domain_path(path: &str) -> bool {
    let path = path.strip_prefix("/api").unwrap_or(path);
    [
        "/nodes",
        "/edges",
        "/accounts",
        "/conversations",
        "/ortswebereien",
        "/webgemeindezentren",
    ]
    .iter()
    .any(|prefix| path == *prefix || path.starts_with(&format!("{prefix}/")))
}

fn requires_database_privacy_fence(method: &Method, path: &str) -> bool {
    if !matches!(*method, Method::GET | Method::HEAD) {
        return false;
    }
    let path = path.strip_prefix("/api").unwrap_or(path);
    // GET /accounts/me/profile takes accounts_persist before its DB read,
    // just like PATCH /accounts/me/profile does before a domain_accounts UPDATE.
    // A PG shared generation row lock acquired here before accounts_persist
    // would invert that order and deadlock with a simultaneous profile PATCH.
    if path == "/accounts/me/profile" || path == "/nodes/search" {
        return false;
    }
    // Other unlisted domain views read canonical PostgreSQL directly.
    // Only cache-derived node/account/edge and conversation GETs need a
    // cross-process fence; otherwise reserve pooled connections for writers.
    ["/nodes", "/edges", "/accounts", "/conversations"]
        .iter()
        .any(|prefix| path == *prefix || path.starts_with(&format!("{prefix}/")))
}

/// Keep PostgreSQL-backed requests on one internally consistent process-local
/// projection. Mutating requests and authenticated requests require the current
/// committed generation. Anonymous GET/HEAD requests may use the previous complete
/// generation while another request rebuilds the next snapshot, or during an exact
/// +1 transaction-proven commit/cache handoff of a serialized local node PATCH.
/// Keeping requests with the canonical session cookie strict is security-sensitive
/// because auth middleware reads account disabled/role
/// state from this projection after this middleware runs. Canonical request
/// authentication is currently cookie-only; adding any other inbound auth scheme
/// requires updating this classifier before that scheme can be enabled. The read
/// guard remains held for the full handler so no request can observe a partially replaced
/// accounts/nodes/edges projection.
fn may_use_previous_projection(method: &Method, has_session_cookie: bool, path: &str) -> bool {
    if !matches!(*method, Method::GET | Method::HEAD) || has_session_cookie {
        return false;
    }
    // Anonymous projections are allowed to defer only where stale public
    // content cannot expose node visibility changes. The old fast-path
    // must not publish public->private/revoked transitions via graph routes.
    !privacy_sensitive_domain_path(path)
}

// Acquire the request's actual snapshot guard before checking the canonical
// DB generation. A pre-gate check can be invalidated while waiting for the
// read guard, especially by another request publishing a new cache generation.
async fn current_generation_under_read_gate<'a, F, Fut>(
    gate: &'a tokio::sync::RwLock<()>,
    local_version: &AtomicI64,
    db_version: F,
) -> anyhow::Result<(Option<RwLockReadGuard<'a, ()>>, Duration)>
where
    F: FnOnce() -> Fut,
    Fut: Future<Output = anyhow::Result<i64>>,
{
    let wait_started = Instant::now();
    let guard = gate.read().await;
    let waited = wait_started.elapsed();
    let observed = db_version().await?;
    if observed == local_version.load(Ordering::Acquire) {
        Ok((Some(guard), waited))
    } else {
        Ok((None, waited))
    }
}

// Only the transaction-held PostgreSQL row lock can exclude visibility commits
// by OTHER API processes. A process-local RwLock or a second generation query
// after the handler cannot close the check-to-response race.
async fn begin_fenced_generation(
    pool: &PgPool,
) -> anyhow::Result<(Transaction<'static, Postgres>, i64)> {
    let mut tx = pool.begin().await?;
    // Every domain_nodes/accounts/edges writer updates this singleton row in
    // the SAME transaction via weltgewebe_enqueue_domain_event(). A FOR SHARE
    // lock prevents any of those writes from committing until we finish the
    // complete JSON handler response (including cache-based authorization).
    let observed: i64 = sqlx::query_scalar(
        "SELECT version FROM domain_projection_state WHERE singleton = TRUE FOR SHARE",
    )
    .fetch_one(&mut *tx)
    .await?;
    Ok((tx, observed))
}

async fn fenced_domain_read(state: &ApiState, request: Request, next: Next) -> Response {
    let Some(pool) = state.db_pool.as_ref() else {
        return projection_unavailable(state, "PostgreSQL domain pool is missing");
    };
    // The API's PostgreSQL pool has five connections. Without this bound, five
    // readers could each hold a connection while their handlers wait for a
    // sixth one, starving both handlers and privacy writers.
    let _slot =
        match tokio::time::timeout(Duration::from_secs(3), PRIVACY_READ_SLOTS.acquire()).await {
            Ok(Ok(slot)) => slot,
            Ok(Err(error)) => return projection_unavailable(state, error),
            Err(_) => return projection_unavailable(state, "privacy read slot timed out"),
        };
    const MAX_FENCED_GENERATION_ATTEMPTS: usize = 4;
    for _ in 0..MAX_FENCED_GENERATION_ATTEMPTS {
        let wait_started = Instant::now();
        let projection_guard = state.domain_projection_gate.read().await;
        state
            .metrics
            .observe_domain_projection_read_gate_wait(wait_started.elapsed());

        let (tx, observed) =
            match tokio::time::timeout(Duration::from_secs(3), begin_fenced_generation(pool)).await
            {
                Ok(Ok(checked)) => checked,
                Ok(Err(error)) => return projection_unavailable(state, error),
                Err(_) => {
                    return projection_unavailable(state, "PostgreSQL privacy fence timed out")
                }
            };
        if observed == state.domain_projection_version.load(Ordering::Acquire) {
            let response = next.run(request).await;
            // Retain BOTH the process-local cache read guard and the database
            // row lock through response creation. A rollback failure must not
            // release a potentially stale private payload to the caller.
            return match tx.rollback().await {
                Ok(()) => response,
                Err(error) => projection_unavailable(state, error),
            };
        }
        // Release the database fence AND the local gate before reloading.
        if let Err(error) = tx.rollback().await {
            return projection_unavailable(state, error);
        }
        drop(projection_guard);
        if let Err(error) = state.refresh_domain_projection_if_stale().await {
            return projection_unavailable(state, error);
        }
    }
    projection_unavailable(
        state,
        "domain generation changed during fenced privacy read",
    )
}

fn projection_unavailable(state: &ApiState, error: impl std::fmt::Display) -> Response {
    state.metrics.domain_projection_refresh_failed();
    tracing::error!(
        event = "domain.projection_refresh_failed",
        error = %error,
        "PostgreSQL domain projection is not current for this request"
    );
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(serde_json::json!({
            "error": "DOMAIN_PROJECTION_UNAVAILABLE",
        })),
    )
        .into_response()
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
    if may_use_previous_projection(request.method(), has_session_cookie, request.uri().path()) {
        if let Err(error) = state.refresh_domain_projection_for_read().await {
            return projection_unavailable(&state, error);
        }
        let read_gate_wait_started = Instant::now();
        let _projection_read = state.domain_projection_gate.read().await;
        state
            .metrics
            .observe_domain_projection_read_gate_wait(read_gate_wait_started.elapsed());
        return next.run(request).await;
    }

    if requires_database_privacy_fence(request.method(), request.uri().path()) {
        return fenced_domain_read(&state, request, next).await;
    }

    // Non-cache-sensitive routes retain their existing strict admission logic.
    // Cache-derived privacy JSON gets a PostgreSQL cross-process fence above.
    const MAX_CURRENT_GENERATION_ATTEMPTS: usize = 4;
    let Some(pool) = state.db_pool.as_ref() else {
        return projection_unavailable(&state, "PostgreSQL domain pool is missing");
    };
    for _ in 0..MAX_CURRENT_GENERATION_ATTEMPTS {
        let checked = current_generation_under_read_gate(
            &state.domain_projection_gate,
            &state.domain_projection_version,
            || async {
                tokio::time::timeout(
                    Duration::from_secs(3),
                    crate::domain_db::domain_projection_version(pool),
                )
                .await
                .map_err(|_| anyhow::anyhow!("domain generation read timed out"))?
            },
        )
        .await;
        match checked {
            Ok((Some(_projection_read), wait_duration)) => {
                state
                    .metrics
                    .observe_domain_projection_read_gate_wait(wait_duration);
                return next.run(request).await;
            }
            Ok((None, wait_duration)) => {
                state
                    .metrics
                    .observe_domain_projection_read_gate_wait(wait_duration);
            }
            Err(error) => return projection_unavailable(&state, error),
        }
        // Release the read guard before refresh, which may need the write guard.
        if let Err(error) = state.refresh_domain_projection_if_stale().await {
            return projection_unavailable(&state, error);
        }
    }
    projection_unavailable(
        &state,
        "domain generation changed during bounded read admission",
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn generation_is_checked_after_acquiring_the_snapshot_guard() {
        let gate = std::sync::Arc::new(tokio::sync::RwLock::new(()));
        let local = std::sync::Arc::new(AtomicI64::new(1));
        let writer = gate.write().await;
        let waiting_gate = std::sync::Arc::clone(&gate);
        let waiting_local = std::sync::Arc::clone(&local);
        let reader = tokio::spawn(async move {
            let (read, _) =
                current_generation_under_read_gate(&waiting_gate, &waiting_local, || async {
                    Ok(2)
                })
                .await
                .expect("canonical generation");
            assert!(read.is_some(), "post-writer read must see new generation");
        });
        tokio::task::yield_now().await;
        assert!(!reader.is_finished(), "read must wait for writer guard");
        local.store(2, Ordering::Release);
        drop(writer);
        reader.await.expect("read completed");
    }

    #[tokio::test]
    async fn generation_drift_and_errors_release_the_read_guard() {
        let gate = tokio::sync::RwLock::new(());
        let local = AtomicI64::new(1);
        let (stale, _) = current_generation_under_read_gate(&gate, &local, || async { Ok(2) })
            .await
            .expect("canonical generation");
        assert!(stale.is_none(), "DB advance requires fresh projection");
        assert!(gate.try_write().is_ok(), "stale gate must be released");

        let (current, _) = current_generation_under_read_gate(&gate, &local, || async { Ok(1) })
            .await
            .expect("canonical generation");
        assert!(current.is_some());
        assert!(gate.try_write().is_err(), "handler must retain guard");
        drop(current);
        assert!(gate.try_write().is_ok());

        let missing = current_generation_under_read_gate(&gate, &local, || async {
            Err(anyhow::anyhow!("no authoritative version"))
        })
        .await;
        assert!(missing.is_err());
        assert!(
            gate.try_write().is_ok(),
            "failed DB read must not strand gate"
        );
    }

    #[test]
    fn database_fence_covers_sensitive_json_reads_not_writes() {
        for path in [
            "/nodes",
            "/api/nodes/id",
            "/edges/id",
            "/api/accounts/account",
            "/api/conversations/id/messages",
        ] {
            assert!(
                requires_database_privacy_fence(&Method::GET, path),
                "{path}"
            );
            assert!(
                requires_database_privacy_fence(&Method::HEAD, path),
                "{path}"
            );
            assert!(
                !requires_database_privacy_fence(&Method::POST, path),
                "{path}"
            );
            assert!(
                !requires_database_privacy_fence(&Method::PATCH, path),
                "{path}"
            );
        }
        // Governance GETs finalize due proposals (writes). GET own profile
        // takes a mutex also held by the profile PATCH writer: both stay
        // strict-generation but cannot hold the shared DB row fence.
        for path in [
            "/health/ready",
            "/api/version",
            "/api/search",
            "/auth/me",
            "/api/proposals/id",
            "/accounts/me/profile",
            "/api/accounts/me/profile",
            "/nodes/search",
            "/api/nodes/search",
            "/ortswebereien",
            "/api/webgemeindezentren/id",
        ] {
            assert!(
                !requires_database_privacy_fence(&Method::GET, path),
                "{path}"
            );
        }
    }

    #[test]
    fn intentionally_unfenced_domain_gets_remain_strict_generation() {
        for path in [
            "/accounts/me/profile",
            "/api/accounts/me/profile",
            "/api/nodes/search",
            "/api/ortswebereien",
            "/api/webgemeindezentren/id",
        ] {
            assert!(
                !may_use_previous_projection(&Method::GET, false, path),
                "unfenced domain GET must not fall back to previous cache: {path}"
            );
        }
    }

    #[test]
    fn only_non_domain_anonymous_reads_may_use_previous_projection() {
        assert!(may_use_previous_projection(
            &Method::GET,
            false,
            "/api/health/ready"
        ));
        assert!(may_use_previous_projection(
            &Method::HEAD,
            false,
            "/api/version"
        ));
        for path in [
            "/nodes",
            "/nodes/private",
            "/api/nodes",
            "/api/nodes/private",
            "/edges",
            "/api/edges/edge-id",
            "/accounts/account-id",
            "/api/accounts/account-id",
            "/conversations/conv-id",
            "/api/conversations/conv-id/messages",
            "/api/ortswebereien",
            "/api/webgemeindezentren/center",
        ] {
            assert!(
                !may_use_previous_projection(&Method::GET, false, path),
                "{path}"
            );
        }
        assert!(!may_use_previous_projection(
            &Method::GET,
            true,
            "/api/health/ready"
        ));
        assert!(!may_use_previous_projection(
            &Method::HEAD,
            true,
            "/api/version"
        ));
        assert!(!may_use_previous_projection(
            &Method::POST,
            false,
            "/api/health/ready"
        ));
        assert!(!may_use_previous_projection(
            &Method::PATCH,
            false,
            "/api/nodes/private"
        ));
        assert!(!may_use_previous_projection(
            &Method::DELETE,
            false,
            "/api/nodes/private"
        ));
    }
}
