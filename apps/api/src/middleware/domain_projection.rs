use std::future::Future;
use std::sync::atomic::{AtomicI64, Ordering};
use std::time::{Duration, Instant};

use sqlx::{PgPool, Postgres, Transaction};
use tokio::sync::{RwLock, RwLockReadGuard, Semaphore, SemaphorePermit};

use axum::{
    extract::{Request, State},
    http::{Method, StatusCode},
    middleware::Next,
    response::{IntoResponse, Response},
    Json,
};
use axum_extra::extract::cookie::CookieJar;

use crate::{
    config::DomainReadSource,
    middleware::auth::AuthContext,
    routes::{
        auth::SESSION_COOKIE_NAME, postgres_domain_connection_budget,
        POSTGRES_DOMAIN_OPERATION_UNITS,
    },
    state::ApiState,
};

// Privacy-sensitive JSON reads need the cross-process fence through response
// construction. Search SQL checks visibility but not subsequent ranking/JSON.
// Mutations MUST NOT hold a shared fence while their own transactions update
// domain_projection_state, or they would deadlock themselves.
fn privacy_sensitive_domain_path(path: &str) -> bool {
    let path = path.strip_prefix("/api").unwrap_or(path);
    [
        "/nodes",
        "/search",
        "/edges",
        "/accounts",
        "/conversations",
        "/ortswebereien",
        "/webgemeindezentren",
    ]
    .iter()
    .any(|prefix| path == *prefix || path.starts_with(&format!("{prefix}/")))
}

// Search performs potentially slow external embedding before any privacy data
// is read. Only its handler may acquire the response-lifetime PostgreSQL fence,
// AFTER that preparation, not this outer middleware.
fn is_deferred_search_privacy_path(method: &Method, path: &str) -> bool {
    if !matches!(*method, Method::GET | Method::HEAD) {
        return false;
    }
    let path = path.strip_prefix("/api").unwrap_or(path);
    matches!(path, "/search" | "/nodes/search")
}

fn requires_database_privacy_fence(method: &Method, path: &str) -> bool {
    if !matches!(*method, Method::GET | Method::HEAD) {
        return false;
    }
    if is_deferred_search_privacy_path(method, path) {
        return false; // response fence is acquired by the route after embedding
    }
    let path = path.strip_prefix("/api").unwrap_or(path);
    // GET /accounts/me/profile takes accounts_persist before its DB read,
    // just like PATCH /accounts/me/profile does before a domain_accounts UPDATE.
    // A PG shared generation row lock acquired here before accounts_persist
    // would invert that order and deadlock with a simultaneous profile PATCH.
    if path == "/accounts/me/profile" {
        return false;
    }
    // Other unlisted domain views read canonical PostgreSQL directly.
    // Governance GETs explicitly hold a response-lifetime fence inside their
    // handlers AFTER due-proposal finalization; finalization itself can write.
    // Search is deliberately handled after embedding inside its route, not
    // under an outer lock held across HTTP calls to its provider.
    // Otherwise reserve pooled connections for writers.
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
pub(crate) async fn begin_fenced_generation(
    pool: &PgPool,
) -> anyhow::Result<(Transaction<'static, Postgres>, i64)> {
    let mut tx = pool.begin().await?;
    // PostgreSQL's FOR SHARE can admit a late compatible reader ahead of a
    // writer already waiting to UPDATE the singleton row. Without a
    // process-independent fair queue, a stream of GETs across API replicas
    // can starve public->private commits indefinitely. The paired BEFORE
    // STATEMENT node/edge/account writer triggers take the same transaction
    // advisory lock exclusively, BEFORE they modify domain row tuples.
    // Heavyweight advisory wait queues then admit the writer before newly
    // arriving shared readers. Both locks release on transaction rollback.
    sqlx::query(
        "SELECT pg_advisory_xact_lock_shared(\
         hashtextextended('weltgewebe.domain.generation.fair_queue.v1', 0))",
    )
    .execute(&mut *tx)
    .await?;
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

// Every protected operation that needs two pooled connections must acquire
// the process-local projection read gate BEFORE reserving connection capacity.
// Mutations already enter their handlers under that read gate. Reserving GET
// permits first creates a cycle when a refresh has queued its write gate and
// an in-flight mutation holds a read gate while waiting for the same permits.
async fn acquire_fenced_read_admission<'a>(
    gate: &'a RwLock<()>,
    budget: &'a Semaphore,
) -> Result<(RwLockReadGuard<'a, ()>, SemaphorePermit<'a>, Duration), &'static str> {
    let started = Instant::now();
    let projection_guard = gate.read().await;
    let gate_wait = started.elapsed();
    let slot = match tokio::time::timeout(
        Duration::from_secs(3),
        budget.acquire_many(POSTGRES_DOMAIN_OPERATION_UNITS),
    )
    .await
    {
        Ok(Ok(slot)) => slot,
        Ok(Err(_)) => return Err("privacy read slot closed"),
        Err(_) => return Err("privacy read slot timed out"),
    };
    Ok((projection_guard, slot, gate_wait))
}

async fn fenced_domain_read(state: &ApiState, request: Request, next: Next) -> Response {
    let Some(pool) = state.db_pool.as_ref() else {
        return projection_unavailable(state, "PostgreSQL domain pool is missing");
    };
    const MAX_FENCED_GENERATION_ATTEMPTS: usize = 4;
    for _ in 0..MAX_FENCED_GENERATION_ATTEMPTS {
        // The slot belongs to THIS snapshot attempt only. In particular a
        // stale GET must not hoard permits while a cache refresh obtains the
        // exclusive write gate and uses the database itself.
        let (projection_guard, slot, gate_wait) = match acquire_fenced_read_admission(
            &state.domain_projection_gate,
            postgres_domain_connection_budget(),
        )
        .await
        {
            Ok(admission) => admission,
            Err(error) => return projection_unavailable(state, error),
        };
        state
            .metrics
            .observe_domain_projection_read_gate_wait(gate_wait);

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
            if !auth_matches_guarded_projection(
                state,
                request.extensions().get::<AuthContext>().cloned(),
            )
            .await
            {
                if let Err(error) = tx.rollback().await {
                    return projection_unavailable(state, error);
                }
                return StatusCode::UNAUTHORIZED.into_response();
            }
            let response = next.run(request).await;
            // Retain BOTH the process-local cache read guard and the database
            // row lock through response creation. A rollback failure must not
            // release a potentially stale private payload to the caller.
            return match tx.rollback().await {
                Ok(()) => response,
                Err(error) => projection_unavailable(state, error),
            };
        }
        // Release the database fence, pool reservation AND read gate before
        // refreshing; otherwise refresh may queue behind a waiter that is
        // holding permits it no longer needs.
        if let Err(error) = tx.rollback().await {
            return projection_unavailable(state, error);
        }
        drop(slot);
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

// Session authentication happens outside the held PostgreSQL generation-row
// lock, otherwise its SQL touch can deadlock against guest exit. Re-check the
// authenticated account against the SAME guarded and current local domain
// generation before the handler uses the role/owner from AuthContext.
async fn auth_matches_guarded_projection(state: &ApiState, auth: Option<AuthContext>) -> bool {
    let Some(auth) = auth else {
        // Unauthenticated tests may invoke this middleware directly; the
        // production composition always inserts AuthContext first.
        return true;
    };
    if !auth.authenticated {
        return true;
    }
    let Some(account_id) = auth.account_id.as_deref() else {
        return false;
    };
    let accounts = state.accounts.read().await;
    accounts
        .get(account_id)
        .is_some_and(|account| !account.public.disabled && account.role == auth.role)
}

/// A response-lifetime PostgreSQL search fence, acquired only after the slow
/// embedding provider has finished. The local read guard and existing two-unit
/// connection reservation are held together with the row lock.
pub struct SearchPrivacyFence<'a> {
    _projection_guard: RwLockReadGuard<'a, ()>,
    _slot: SemaphorePermit<'a>,
    tx: Transaction<'static, Postgres>,
}

impl SearchPrivacyFence<'_> {
    /// Serialize the JSON body before calling this method. If rollback fails,
    /// the already-built response must never be delivered to the client.
    pub async fn finish(self, response: Response) -> anyhow::Result<Response> {
        self.tx.rollback().await?;
        Ok(response)
    }
}

/// Reuse the normal PostgreSQL admission and version checks for search, but
/// perform them *after* provider I/O. Ok(None) means the authenticated account
/// was revoked or changed role while embedding; errors fail closed as 503.
pub async fn acquire_search_privacy_fence<'a>(
    state: &'a ApiState,
    auth: Option<AuthContext>,
) -> anyhow::Result<Option<SearchPrivacyFence<'a>>> {
    let pool = state
        .db_pool
        .as_ref()
        .ok_or_else(|| anyhow::anyhow!("PostgreSQL domain pool is missing"))?;
    for _ in 0..4 {
        let (projection_guard, slot, gate_wait) = acquire_fenced_read_admission(
            &state.domain_projection_gate,
            postgres_domain_connection_budget(),
        )
        .await
        .map_err(|reason| anyhow::anyhow!(reason))?;
        state
            .metrics
            .observe_domain_projection_read_gate_wait(gate_wait);

        let (tx, observed) =
            tokio::time::timeout(Duration::from_secs(3), begin_fenced_generation(pool))
                .await
                .map_err(|_| anyhow::anyhow!("PostgreSQL search privacy fence timed out"))??;
        if observed == state.domain_projection_version.load(Ordering::Acquire) {
            if !auth_matches_guarded_projection(state, auth.clone()).await {
                tx.rollback().await?;
                return Ok(None);
            }
            return Ok(Some(SearchPrivacyFence {
                _projection_guard: projection_guard,
                _slot: slot,
                tx,
            }));
        }
        // No stale cache is ever admitted. Release all read resources before
        // a refresh, which may need the projection writer gate and pool slots.
        tx.rollback().await?;
        drop(slot);
        drop(projection_guard);
        state.refresh_domain_projection_if_stale().await?;
    }
    anyhow::bail!("domain generation changed during bounded search admission")
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
    if has_session_cookie && request.extensions().get::<AuthContext>().is_none() {
        return projection_unavailable(
            &state,
            "session authentication must precede domain projection",
        );
    }
    if is_deferred_search_privacy_path(request.method(), request.uri().path()) {
        // The search service owns the canonical read fence after embedding and
        // revalidates generation + account authorization before reading nodes.
        return next.run(request).await;
    }
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
                if !auth_matches_guarded_projection(
                    &state,
                    request.extensions().get::<AuthContext>().cloned(),
                )
                .await
                {
                    return StatusCode::UNAUTHORIZED.into_response();
                }
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
    async fn queued_projection_writer_is_not_blocked_by_fenced_gets_budget_admission() {
        let gate = std::sync::Arc::new(RwLock::new(()));
        let budget = std::sync::Arc::new(Semaphore::new(4));
        let writer = gate.write().await;
        let reader_gate = gate.clone();
        let reader_budget = budget.clone();
        let mut queued = tokio::spawn(async move {
            let (_read, _slot, _) = acquire_fenced_read_admission(&reader_gate, &reader_budget)
                .await
                .expect("queued read should be admitted");
        });
        assert!(
            tokio::time::timeout(Duration::from_millis(150), &mut queued)
                .await
                .is_err(),
            "GET must await a queued projection writer"
        );
        assert_eq!(
            budget.available_permits(),
            4,
            "blocked GET must not reserve a pool connection slot before the read gate"
        );
        drop(writer);
        tokio::time::timeout(Duration::from_secs(3), queued)
            .await
            .expect("read must finish after projection writer")
            .expect("GET task");
        assert_eq!(budget.available_permits(), 4);
    }

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
            "/search",
            "/api/search",
            "/nodes/search",
            "/api/nodes/search",
            "/auth/me",
            "/api/proposals/id",
            "/accounts/me/profile",
            "/api/accounts/me/profile",
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
            "/search",
            "/api/search",
            "/nodes/search",
            "/api/nodes/search",
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
