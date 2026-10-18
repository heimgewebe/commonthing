use std::{future::Future, sync::OnceLock, time::Instant};

use axum::{
    extract::{Path, State},
    http::{header::IF_MATCH, HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    Extension, Json,
};
use serde_json::Value;
use sqlx::{Postgres, Transaction};
use tokio::sync::{Mutex, MutexGuard, Semaphore, SemaphorePermit};

use crate::{
    advisory_lock::node_mutation_lock_key,
    auth::{
        rate_limit::{NodeMutationRateDecision, RateLimitError},
        role::Role,
    },
    config::{DomainNodeWriteSource, DomainReadSource},
    domain_db,
    middleware::auth::AuthContext,
    node_mutation::NodeMutationOperation,
    state::ApiState,
    telemetry::NodeMutationOutcome,
};

use super::{domain_write_guard::reject_node_patch_unless_writable, nodes};

// PostgreSQL node mutations are serialized per node id across API instances.
// A bounded process-local stripe queue prevents same-node waiters from occupying
// the connection pool while the first request owns the database advisory lock.
// Different node ids can proceed concurrently; rare stripe collisions only
// reduce local parallelism and never weaken correctness.
const LOCAL_NODE_LOCK_STRIPES: usize = 64;
// Each PostgreSQL mutation temporarily needs two pool connections: one holds
// the advisory-lock transaction and one performs the existing mutation helper.
// Leave one connection available for unrelated requests and cap concurrent
// mutations to the remaining connection pairs.
const LOCAL_POSTGRES_NODE_MUTATION_SLOTS: usize =
    (crate::DATABASE_POOL_MAX_CONNECTIONS as usize - 1) / 2;
static LOCAL_NODE_LOCKS: OnceLock<Vec<Mutex<()>>> = OnceLock::new();
static POSTGRES_NODE_MUTATION_SLOTS: OnceLock<Semaphore> = OnceLock::new();

enum NodeMutationGuard {
    Local {
        _guard: MutexGuard<'static, ()>,
    },
    Postgres {
        _guard: MutexGuard<'static, ()>,
        _slot: SemaphorePermit<'static>,
        transaction: Transaction<'static, Postgres>,
    },
}

fn local_node_locks() -> &'static [Mutex<()>] {
    LOCAL_NODE_LOCKS
        .get_or_init(|| {
            (0..LOCAL_NODE_LOCK_STRIPES)
                .map(|_| Mutex::new(()))
                .collect()
        })
        .as_slice()
}

fn local_node_lock_index(node_id: &str) -> usize {
    (node_mutation_lock_key(node_id) as u64 as usize) % LOCAL_NODE_LOCK_STRIPES
}

fn local_node_lock(node_id: &str) -> &'static Mutex<()> {
    &local_node_locks()[local_node_lock_index(node_id)]
}

fn postgres_node_mutation_slots() -> &'static Semaphore {
    POSTGRES_NODE_MUTATION_SLOTS.get_or_init(|| Semaphore::new(LOCAL_POSTGRES_NODE_MUTATION_SLOTS))
}

fn uses_postgres_node_persistence(state: &ApiState) -> bool {
    state.config.domain_read_source == DomainReadSource::Postgres
        && state.config.domain_node_write_source == DomainNodeWriteSource::Postgres
}

async fn acquire_node_mutation_guard(
    state: &ApiState,
    node_id: &str,
) -> Result<NodeMutationGuard, Response> {
    // Every existing-node mutation takes the same process-local stripe. For
    // JSONL this makes the version check atomic with the existing handler's
    // file/cache mutation. PostgreSQL additionally takes a cross-instance
    // advisory lock below.
    let guard = local_node_lock(node_id).lock().await;
    if !uses_postgres_node_persistence(state) {
        return Ok(NodeMutationGuard::Local { _guard: guard });
    }

    // Same-node waiters do not consume the global pool budget. The slot then
    // guarantees enough free connections for the authoritative version read,
    // the mutation helper, and unrelated database traffic.
    let slot = postgres_node_mutation_slots()
        .acquire()
        .await
        .map_err(|error| {
            tracing::error!(%error, node_id, "PostgreSQL node mutation limiter closed");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                "failed to acquire collective node write guard",
            )
                .into_response()
        })?;
    let pool = state.db_pool.as_ref().ok_or_else(|| {
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            "PostgreSQL pool unavailable for collective node write",
        )
            .into_response()
    })?;
    let mut transaction = pool.begin().await.map_err(|error| {
        tracing::error!(%error, node_id, "failed to begin collective node guard transaction");
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            "failed to acquire collective node write guard",
        )
            .into_response()
    })?;
    // Hash in application code and call only PostgreSQL's documented bigint
    // advisory-lock API. This avoids depending on backend hash support
    // functions whose availability may differ across PostgreSQL-compatible
    // managed services.
    sqlx::query("SELECT pg_advisory_xact_lock($1::bigint)")
        .bind(node_mutation_lock_key(node_id))
        .execute(&mut *transaction)
        .await
        .map_err(|error| {
            tracing::error!(%error, node_id, "failed to acquire per-node advisory lock");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                "failed to acquire collective node write guard",
            )
                .into_response()
        })?;

    Ok(NodeMutationGuard::Postgres {
        _guard: guard,
        _slot: slot,
        transaction,
    })
}

async fn release_node_mutation_guard(guard: NodeMutationGuard) {
    match guard {
        NodeMutationGuard::Local { _guard } => {}
        NodeMutationGuard::Postgres {
            _guard,
            _slot,
            transaction,
        } => {
            if let Err(error) = transaction.commit().await {
                tracing::error!(
                    %error,
                    "failed to release per-node advisory lock after mutation response was produced"
                );
            }
        }
    }
}

async fn run_node_mutation<F, T>(
    state: &ApiState,
    node_id: &str,
    mutation: F,
) -> Result<T, Response>
where
    F: Future<Output = Result<T, Response>>,
{
    // Applicability and storage-mode errors take precedence over HTTP
    // preconditions. This preserves the existing fail-closed write contract
    // and avoids reading or locking a node for a mutation that cannot run.
    if let Err((status, message)) = reject_node_patch_unless_writable(state) {
        return Err((status, message).into_response());
    }

    let guard = match acquire_node_mutation_guard(state, node_id).await {
        Ok(guard) => guard,
        Err(response) => return Err(response),
    };
    let outcome = mutation.await;
    release_node_mutation_guard(guard).await;
    outcome
}

/// Serialize a post-commit activity projection with node deletion. The node is
/// re-read after the guard is acquired: a projection that lost the race to a
/// completed deletion becomes a no-op, while a later deletion waits until the
/// new edge is durable and then removes it in the normal cascade.
pub(crate) async fn run_node_activity_projection<F>(
    state: &ApiState,
    node_id: &str,
    projection: F,
) -> Result<(), (StatusCode, String)>
where
    F: Future<Output = Result<(), (StatusCode, String)>>,
{
    let guard = acquire_node_mutation_guard(state, node_id)
        .await
        .map_err(|response| {
            (
                response.status(),
                "failed to acquire node activity projection guard".to_string(),
            )
        })?;
    let node_exists = node_exists_for_activity_projection(state, node_id).await;
    let outcome = match node_exists {
        Ok(true) => projection.await,
        Ok(false) => Ok(()),
        Err(response) => Err((
            response.status(),
            "failed to verify node before activity projection".to_string(),
        )),
    };
    release_node_mutation_guard(guard).await;
    outcome
}

pub async fn create_node_serialized(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
    Json(payload): Json<Value>,
) -> Response {
    nodes::create_node(State(state), Extension(auth), Json(payload))
        .await
        .into_response()
}

async fn node_exists_for_activity_projection(state: &ApiState, id: &str) -> Result<bool, Response> {
    if uses_postgres_node_persistence(state) {
        let pool = state.db_pool.as_ref().ok_or_else(|| {
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                "PostgreSQL pool unavailable for node activity projection",
            )
                .into_response()
        })?;
        return sqlx::query_scalar::<_, bool>(
            "SELECT EXISTS (SELECT 1 FROM domain_nodes WHERE id = $1)",
        )
        .bind(id)
        .fetch_one(pool)
        .await
        .map_err(|error| {
            tracing::error!(%error, node_id = %id, "failed to verify node existence for activity projection");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                "failed to verify node before activity projection",
            )
                .into_response()
        });
    }

    let cache = state.nodes.read().await;
    Ok(cache.get(id).is_some())
}

async fn current_node_for_precondition(
    state: &ApiState,
    id: &str,
) -> Result<Option<nodes::Node>, Response> {
    if uses_postgres_node_persistence(state) {
        let pool = state.db_pool.as_ref().ok_or_else(|| {
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                "PostgreSQL pool unavailable for collective node precondition",
            )
                .into_response()
        })?;
        return domain_db::load_node_from_postgres(pool, id)
            .await
            .map_err(|error| {
                tracing::error!(%error, node_id = %id, "failed to load authoritative node version");
                (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "failed to verify collective node write precondition",
                )
                    .into_response()
            });
    }

    let cache = state.nodes.read().await;
    Ok(cache.get(id).cloned())
}

fn authorize_node_mutation(
    auth: &AuthContext,
    node: &nodes::Node,
) -> Result<(), (StatusCode, &'static str)> {
    // A mutation precondition includes the entire current node in its 412/428
    // response. A mutation role alone is not a grant to read private nodes,
    // and revoked nodes have no ordinary API readers, including their creator.
    // A separately authorized moderation route is needed for exceptional recovery.
    if !nodes::node_readable_to(node, Some(auth)) {
        return Err((StatusCode::NOT_FOUND, "node not found"));
    }
    if matches!(auth.role, Role::Weber | Role::Admin) {
        return Ok(());
    }
    let account_id = auth.account_id.as_deref().ok_or((
        StatusCode::UNAUTHORIZED,
        "authenticated account context missing",
    ))?;
    if node.created_by_account_id.as_deref() == Some(account_id) {
        return Ok(());
    }
    Err((
        StatusCode::FORBIDDEN,
        "guests may only modify nodes they created",
    ))
}

fn check_node_precondition(
    node: Option<nodes::Node>,
    id: &str,
    headers: &HeaderMap,
) -> Result<(), Box<Response>> {
    let Some(node) = node else {
        return Ok(()); // Let the underlying handler return 404
    };

    let if_match = headers.get(IF_MATCH).and_then(|h| h.to_str().ok());
    let expected_etag = format!("\"{}\"", node.updated_at);

    match if_match {
        Some(etag) if etag == expected_etag => Ok(()),
        Some(_) => {
            tracing::info!(node_id = %id, provided = ?if_match, expected = %expected_etag, "Precondition failed: concurrent modification detected");
            Err(Box::new(
                (StatusCode::PRECONDITION_FAILED, Json(node)).into_response(),
            ))
        }
        None => {
            tracing::info!(node_id = %id, "Precondition required: missing If-Match header");
            Err(Box::new(
                (StatusCode::PRECONDITION_REQUIRED, Json(node)).into_response(),
            ))
        }
    }
}

async fn mutation_rate_context(
    state: &ApiState,
    auth: &AuthContext,
    operation: NodeMutationOperation,
) -> Result<(String, bool), Response> {
    let account_id = auth.account_id.clone().ok_or_else(|| {
        (
            StatusCode::UNAUTHORIZED,
            "authenticated account context missing",
        )
            .into_response()
    })?;
    match state
        .rate_limiter
        .check_node_mutation(&account_id, auth.role.clone(), operation)
        .await
    {
        Ok(NodeMutationRateDecision::Enforced) => Ok((account_id, false)),
        Ok(NodeMutationRateDecision::AdminEmergencyBypass) => Ok((account_id, true)),
        Err(RateLimitError::AccountLimited) => Err((
            StatusCode::TOO_MANY_REQUESTS,
            "collective node mutation rate limit exceeded",
        )
            .into_response()),
        Err(RateLimitError::BackendUnavailable(error)) => {
            tracing::error!(%error, operation = operation.as_str(), "node mutation rate-limit backend unavailable");
            Err((
                StatusCode::SERVICE_UNAVAILABLE,
                "collective node mutation rate limit unavailable",
            )
                .into_response())
        }
        Err(error) => {
            tracing::error!(%error, operation = operation.as_str(), "unexpected node mutation rate-limit error");
            Err(StatusCode::INTERNAL_SERVER_ERROR.into_response())
        }
    }
}

fn observe_mutation_response(
    state: &ApiState,
    operation: NodeMutationOperation,
    started: Instant,
    admin_bypass: bool,
    response: &Response,
) {
    state.metrics.observe_node_mutation(
        operation,
        NodeMutationOutcome::from_status(response.status()),
        started.elapsed(),
        admin_bypass,
    );
}

pub async fn patch_node_serialized(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
    Path(id): Path<String>,
    headers: HeaderMap,
    Json(payload): Json<nodes::UpdateNode>,
) -> Response {
    let operation = NodeMutationOperation::Patch;
    let started = Instant::now();
    let (_actor_account_id, admin_bypass) =
        match mutation_rate_context(&state, &auth, operation).await {
            Ok(context) => context,
            Err(response) => {
                observe_mutation_response(&state, operation, started, false, &response);
                return response;
            }
        };
    let projection_auth = auth.clone();
    let mutation_auth = auth;
    let mutation_state = state.clone();
    let mutation_id = id.clone();
    let outcome = run_node_mutation(&state, &id, async move {
        let node = match current_node_for_precondition(&mutation_state, &mutation_id).await {
            Ok(node) => node,
            Err(response) => return Err(response),
        };
        let Some(current) = node.as_ref() else {
            return Err(StatusCode::NOT_FOUND.into_response());
        };
        if let Err((status, message)) = authorize_node_mutation(&mutation_auth, current) {
            return Err((status, message).into_response());
        }
        let previous_updated_at = current.updated_at.clone();
        if let Err(response) = check_node_precondition(node, &mutation_id, &headers) {
            return Err(*response);
        }
        let Json(node) = nodes::patch_node(
            State(mutation_state.clone()),
            Path(mutation_id),
            Json(payload),
        )
        .await
        .map_err(|error| error.into_response())?;
        if node.updated_at != previous_updated_at {
            if let Err((status, message)) = nodes::ensure_node_activity_faden_guarded(
                &mutation_state,
                &projection_auth,
                &node.id,
                super::edges::FadenType::Knotting,
                &node.id,
                &node.updated_at,
            )
            .await
            {
                tracing::error!(
                    event = "node.edit_faden_projection.failed",
                    node_id = %node.id,
                    %status,
                    error = %message,
                    "Node edit remains successful because it is already durable; the derived Faden is missing"
                );
            }
        }
        Ok(node)
    })
    .await;

    let response = match outcome {
        Ok(node) => Json(node).into_response(),
        Err(response) => response,
    };
    observe_mutation_response(&state, operation, started, admin_bypass, &response);
    response
}

pub async fn replace_node_serialized(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
    Path(id): Path<String>,
    headers: HeaderMap,
    Json(payload): Json<Value>,
) -> Response {
    let operation = NodeMutationOperation::Replace;
    let started = Instant::now();
    let (actor_account_id, admin_bypass) =
        match mutation_rate_context(&state, &auth, operation).await {
            Ok(context) => context,
            Err(response) => {
                observe_mutation_response(&state, operation, started, false, &response);
                return response;
            }
        };
    let projection_auth = auth.clone();
    let mutation_auth = auth;
    let mutation_state = state.clone();
    let mutation_id = id.clone();
    let outcome = run_node_mutation(&state, &id, async move {
        let node = match current_node_for_precondition(&mutation_state, &mutation_id).await {
            Ok(node) => node,
            Err(response) => return Err(response),
        };
        let Some(current) = node.as_ref() else {
            return Err(StatusCode::NOT_FOUND.into_response());
        };
        if let Err((status, message)) = authorize_node_mutation(&mutation_auth, current) {
            return Err((status, message).into_response());
        }
        let previous_updated_at = current.updated_at.clone();
        if let Err(response) = check_node_precondition(node, &mutation_id, &headers) {
            return Err(*response);
        }
        let Json(node) = nodes::replace_node(
            mutation_state.clone(),
            mutation_id,
            payload,
            actor_account_id,
        )
        .await
        .map_err(|error| error.into_response())?;
        if node.updated_at != previous_updated_at {
            if let Err((status, message)) = nodes::ensure_node_activity_faden_guarded(
                &mutation_state,
                &projection_auth,
                &node.id,
                super::edges::FadenType::Knotting,
                &node.id,
                &node.updated_at,
            )
            .await
            {
                tracing::error!(
                    event = "node.edit_faden_projection.failed",
                    node_id = %node.id,
                    %status,
                    error = %message,
                    "Node replacement remains successful because it is already durable; the derived Faden is missing"
                );
            }
        }
        Ok(node)
    })
    .await;

    let response = match outcome {
        Ok(node) => Json(node).into_response(),
        Err(response) => response,
    };
    observe_mutation_response(&state, operation, started, admin_bypass, &response);
    response
}

pub async fn delete_node_serialized(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
    Path(id): Path<String>,
    headers: HeaderMap,
) -> Response {
    let operation = NodeMutationOperation::Delete;
    let started = Instant::now();
    let (actor_account_id, admin_bypass) =
        match mutation_rate_context(&state, &auth, operation).await {
            Ok(context) => context,
            Err(response) => {
                observe_mutation_response(&state, operation, started, false, &response);
                return response;
            }
        };
    let mutation_state = state.clone();
    let mutation_id = id.clone();
    let outcome = run_node_mutation(&state, &id, async move {
        let node = match current_node_for_precondition(&mutation_state, &mutation_id).await {
            Ok(node) => node,
            Err(response) => return Err(response),
        };
        let Some(current) = node.as_ref() else {
            return Err(StatusCode::NOT_FOUND.into_response());
        };
        if let Err((status, message)) = authorize_node_mutation(&auth, current) {
            return Err((status, message).into_response());
        }
        if let Err(response) = check_node_precondition(node, &mutation_id, &headers) {
            return Err(*response);
        }
        let response = nodes::delete_node(mutation_state, mutation_id, actor_account_id)
            .await
            .into_response();
        Ok(response)
    })
    .await;
    let response = match outcome {
        Ok(response) | Err(response) => response,
    };
    observe_mutation_response(&state, operation, started, admin_bypass, &response);
    response
}

#[cfg(test)]
mod tests {
    use std::collections::HashSet;

    use super::*;

    #[test]
    fn protected_node_preconditions_deny_foreign_weber_admin_and_revoked_owner() {
        let mut node = nodes::Node {
            id: "confidential-fixture".into(),
            kind: "test".into(),
            title: "Confidential title".into(),
            created_at: "2026".into(),
            updated_at: "2026-10-18T12:00:00Z".into(),
            has_authoritative_created_at: true,
            created_by_account_id: Some("owner".into()),
            search_visibility: nodes::SearchVisibility::Private,
            summary: Some("confidential summary".into()),
            info: None,
            tags: vec![],
            address: None,
            location: nodes::Location { lat: 0.0, lon: 0.0 },
        };
        let viewer = |id: &str, role| AuthContext {
            authenticated: true,
            account_id: Some(id.to_string()),
            device_id: None,
            role,
            expires_at: None,
        };
        let owner = viewer("owner", Role::Weber);
        let foreign_weber = viewer("unrelated", Role::Weber);
        let foreign_admin = viewer("unrelated-admin", Role::Admin);

        for visibility in [
            nodes::SearchVisibility::Private,
            nodes::SearchVisibility::Hidden,
        ] {
            node.search_visibility = visibility;
            for actor in [&foreign_weber, &foreign_admin] {
                assert_eq!(
                    authorize_node_mutation(actor, &node).unwrap_err().0,
                    StatusCode::NOT_FOUND,
                    "a foreign editor must never obtain the protected node precondition body"
                );
            }
            assert!(authorize_node_mutation(&owner, &node).is_ok());
        }
        node.search_visibility = nodes::SearchVisibility::Revoked;
        assert_eq!(
            authorize_node_mutation(&owner, &node).unwrap_err().0,
            StatusCode::NOT_FOUND,
            "revocation prevents its owner from reading precondition content too"
        );
    }

    #[test]
    fn postgres_mutation_slots_preserve_pool_headroom() {
        assert_eq!(crate::DATABASE_POOL_MAX_CONNECTIONS, 5);
        assert_eq!(LOCAL_POSTGRES_NODE_MUTATION_SLOTS, 2);
        assert!(
            LOCAL_POSTGRES_NODE_MUTATION_SLOTS * 2 < crate::DATABASE_POOL_MAX_CONNECTIONS as usize,
            "lock and mutation connection pairs must leave one pool connection free"
        );
    }

    #[test]
    fn node_lock_keys_and_stripes_are_stable_per_id_and_not_global() {
        assert_eq!(
            node_mutation_lock_key("node-stable"),
            2_204_785_427_200_031_019
        );
        assert_eq!(
            local_node_lock_index("node-stable"),
            local_node_lock_index("node-stable")
        );

        let stripes: HashSet<_> = (0..128)
            .map(|index| local_node_lock_index(&format!("node-{index}")))
            .collect();
        assert!(
            stripes.len() > 1,
            "different node ids must not all share one process-local lock"
        );
    }

    #[test]
    fn precondition_missing_yields_428() {
        let headers = HeaderMap::new();
        let node = nodes::Node {
            id: "1".into(),
            kind: "test".into(),
            title: "Test".into(),
            created_at: "2026".into(),
            updated_at: "2026-07-18T12:00:00Z".into(),
            has_authoritative_created_at: true,
            created_by_account_id: None,
            search_visibility: Default::default(),
            summary: None,
            info: None,
            tags: vec![],
            address: None,
            location: nodes::Location { lat: 0.0, lon: 0.0 },
        };

        let err = check_node_precondition(Some(node), "1", &headers).unwrap_err();
        assert_eq!(err.status(), StatusCode::PRECONDITION_REQUIRED);
    }

    #[test]
    fn precondition_mismatch_yields_412() {
        let mut headers = HeaderMap::new();
        headers.insert(IF_MATCH, "\"old-time\"".parse().unwrap());
        let node = nodes::Node {
            id: "1".into(),
            kind: "test".into(),
            title: "Test".into(),
            created_at: "2026".into(),
            updated_at: "2026-07-18T12:00:00Z".into(),
            has_authoritative_created_at: true,
            created_by_account_id: None,
            search_visibility: Default::default(),
            summary: None,
            info: None,
            tags: vec![],
            address: None,
            location: nodes::Location { lat: 0.0, lon: 0.0 },
        };

        let err = check_node_precondition(Some(node), "1", &headers).unwrap_err();
        assert_eq!(err.status(), StatusCode::PRECONDITION_FAILED);
    }

    #[test]
    fn precondition_match_yields_ok() {
        let mut headers = HeaderMap::new();
        headers.insert(IF_MATCH, "\"2026-07-18T12:00:00Z\"".parse().unwrap());
        let node = nodes::Node {
            id: "1".into(),
            kind: "test".into(),
            title: "Test".into(),
            created_at: "2026".into(),
            updated_at: "2026-07-18T12:00:00Z".into(),
            has_authoritative_created_at: true,
            created_by_account_id: None,
            search_visibility: Default::default(),
            summary: None,
            info: None,
            tags: vec![],
            address: None,
            location: nodes::Location { lat: 0.0, lon: 0.0 },
        };

        assert!(check_node_precondition(Some(node), "1", &headers).is_ok());
    }
}
