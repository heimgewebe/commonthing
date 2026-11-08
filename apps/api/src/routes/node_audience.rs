//! Explicit owner-controlled audience grants for existing nodes.
//! A grant only makes a private node readable by active, consenting members.
//! No grant conveys node-writing rights or changes map coordinates.
use std::collections::HashSet;

use axum::{
    extract::{Path, State},
    http::{header, StatusCode},
    response::{IntoResponse, Response},
    Extension, Json,
};
use serde::{Deserialize, Serialize};
use sqlx::{PgPool, Row};
use uuid::Uuid;

use crate::{
    config::{DomainAccountWriteSource, DomainNodeWriteSource, DomainReadSource},
    middleware::auth::AuthContext,
    state::ApiState,
};

type ApiError = (StatusCode, String);

const MAX_NODE_CIRCLES: usize = 8;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SetNodeAudience {
    scope: String,
    #[serde(default)]
    circle_ids: Vec<Uuid>,
    // Exact representation that the owner last read. Independent sessions
    // may not silently overwrite someone else's later privacy decision.
    expected_scope: String,
    #[serde(default)]
    expected_circle_ids: Vec<Uuid>,
}

impl SetNodeAudience {
    fn validate(&self) -> Result<(Vec<Uuid>, Vec<Uuid>), ApiError> {
        fn check(scope: &str, ids: &[Uuid]) -> Result<Vec<Uuid>, ApiError> {
            if !matches!(scope, "public" | "private" | "circles") {
                return Err(bad_request("scope must be public, private or circles"));
            }
            if ids.len() > MAX_NODE_CIRCLES {
                return Err(bad_request(
                    "at most 8 explicit audience circles are allowed",
                ));
            }
            if scope == "circles" && ids.is_empty() {
                return Err(bad_request("circles scope requires an explicit circle"));
            }
            if scope != "circles" && !ids.is_empty() {
                return Err(bad_request(
                    "public and private scopes must have no circle grants",
                ));
            }
            let mut sorted = ids.to_vec();
            sorted.sort_unstable();
            sorted.dedup();
            if sorted.len() != ids.len() {
                return Err(bad_request("duplicate circle grants are not allowed"));
            }
            Ok(sorted)
        }
        Ok((
            check(&self.scope, &self.circle_ids)?,
            check(&self.expected_scope, &self.expected_circle_ids)?,
        ))
    }
}

#[derive(Debug, Serialize)]
pub struct NodeAudienceView {
    node_id: String,
    scope: &'static str,
    circle_ids: Vec<Uuid>,
}

fn bad_request(message: &str) -> ApiError {
    (StatusCode::BAD_REQUEST, message.to_owned())
}

fn database_error(context: &'static str) -> impl Fn(sqlx::Error) -> ApiError {
    move |err| {
        tracing::error!(%err, context, "node audience PostgreSQL operation failed");
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            "node audience operation unavailable".to_owned(),
        )
    }
}

fn require_actor(auth: &AuthContext) -> Result<&str, ApiError> {
    auth.account_id
        .as_deref()
        .filter(|_| auth.authenticated)
        .ok_or((
            StatusCode::UNAUTHORIZED,
            "authentication required".to_owned(),
        ))
}

fn require_pool(state: &ApiState, write: bool) -> Result<&PgPool, ApiError> {
    if state.config.domain_read_source != DomainReadSource::Postgres
        || state.config.domain_account_write_source != DomainAccountWriteSource::Postgres
        || (write && state.config.domain_node_write_source != DomainNodeWriteSource::Postgres)
    {
        return Err((
            StatusCode::SERVICE_UNAVAILABLE,
            "audience grants require canonical PostgreSQL nodes and accounts".to_owned(),
        ));
    }
    state.db_pool.as_ref().ok_or((
        StatusCode::SERVICE_UNAVAILABLE,
        "node audience requires PostgreSQL".to_owned(),
    ))
}

fn no_store<T: Serialize>(body: T) -> Response {
    ([(header::CACHE_CONTROL, "private, no-store")], Json(body)).into_response()
}

fn audience_view(id: String, visibility: &str, ids: Vec<Uuid>) -> NodeAudienceView {
    let scope = match visibility {
        "public" => "public",
        "private" if !ids.is_empty() => "circles",
        "private" => "private",
        _ => "unavailable",
    };
    NodeAudienceView {
        node_id: id,
        scope,
        circle_ids: ids,
    }
}

/// One canonical query for all cache-based node, edge, and account surfaces.
/// The caller's PostgreSQL domain-generation response fence prevents a
/// concurrent membership/grant mutation from invalidating the read result.
pub(crate) async fn readable_circle_node_ids(
    state: &ApiState,
    actor: Option<&str>,
) -> Result<HashSet<String>, StatusCode> {
    let Some(actor) = actor else {
        return Ok(HashSet::new());
    };
    if state.config.domain_read_source != DomainReadSource::Postgres {
        return Ok(HashSet::new());
    }
    let Some(pool) = state.db_pool.as_ref() else {
        return Err(StatusCode::SERVICE_UNAVAILABLE);
    };
    let ids: Vec<String> = sqlx::query_scalar(
        "SELECT DISTINCT g.node_id
         FROM domain_node_circle_grants g
         JOIN domain_nodes n ON n.id = g.node_id
         JOIN domain_accounts owner
           ON owner.id = weltgewebe_search_node_owner_account_id(n.payload)
          AND owner.disabled = FALSE
         WHERE n.search_visibility = 'private'
           AND weltgewebe_active_circle_member(g.circle_id, $1)",
    )
    .bind(actor)
    .fetch_all(pool)
    .await
    .map_err(|error| {
        tracing::error!(%error, "node circle read authorization failed");
        StatusCode::SERVICE_UNAVAILABLE
    })?;
    Ok(ids.into_iter().collect())
}

/// GET /nodes/{id}/audience - only the original owner may inspect grants.
pub async fn get_node_audience(
    State(state): State<ApiState>,
    Path(id): Path<String>,
    Extension(auth): Extension<AuthContext>,
) -> Result<Response, ApiError> {
    let actor = require_actor(&auth)?;
    let pool = require_pool(&state, false)?;
    let row = sqlx::query(
        "SELECT search_visibility,
                weltgewebe_search_node_owner_account_id(payload) AS owner_id
         FROM domain_nodes WHERE id=$1",
    )
    .bind(&id)
    .fetch_optional(pool)
    .await
    .map_err(database_error("get_node_audience"))?
    .ok_or((StatusCode::NOT_FOUND, "node not found".to_owned()))?;
    let owner: Option<String> = row
        .try_get("owner_id")
        .map_err(database_error("get_owner"))?;
    if owner.as_deref() != Some(actor) {
        return Err((StatusCode::NOT_FOUND, "node not found".to_owned()));
    }
    let visibility: String = row
        .try_get("search_visibility")
        .map_err(database_error("get_visibility"))?;
    let ids: Vec<Uuid> = sqlx::query_scalar(
        "SELECT circle_id FROM domain_node_circle_grants WHERE node_id=$1 ORDER BY circle_id",
    )
    .bind(&id)
    .fetch_all(pool)
    .await
    .map_err(database_error("get_node_grants"))?;
    Ok(no_store(audience_view(id, &visibility, ids)))
}

/// PUT /nodes/{id}/audience - atomic owner-only visibility + circle grants.
/// The existing node row is locked; legacy hidden/revoked cannot be re-opened
/// through this endpoint. Ordinary node edits remain separately authorized.
pub async fn set_node_audience(
    State(state): State<ApiState>,
    Path(id): Path<String>,
    Extension(auth): Extension<AuthContext>,
    Json(input): Json<SetNodeAudience>,
) -> Result<Response, ApiError> {
    let actor = require_actor(&auth)?;
    let (selected, expected_circle_ids) = input.validate()?;
    // Ordinary node PUT/PATCH takes this stripe before its If-Match check;
    // the audience edit must not interleave until that write has completed.
    let _node_mutation_stripe =
        super::collective_write_guard::acquire_node_mutation_stripe(&id).await;
    let pool = require_pool(&state, true)?;
    // The generation-fenced reader also needs two pool connections. Reserve
    // the existing shared weighted budget BEFORE opening our transaction.
    let _pool_budget = crate::state::reserve_postgres_domain_operation()
        .await
        .map_err(database_error("reserve_node_audience_pool_budget"))?;
    let mut tx = pool
        .begin()
        .await
        .map_err(database_error("begin_node_audience"))?;
    // Guest account exit takes account-lifecycle before all owned-node locks.
    // Join the existing order to prevent a cycle when an owner leaves while
    // granting private nodes. Keep the lifecycle lock until transaction end.
    sqlx::query("SELECT pg_advisory_xact_lock($1::bigint)")
        .bind(crate::advisory_lock::account_lifecycle_lock_key(actor))
        .execute(&mut *tx)
        .await
        .map_err(database_error("lock_node_audience_owner_lifecycle"))?;
    // Cross-instance node writes use this same advisory lock, protecting the
    // ordinary node precondition/read/replace sequence against privacy changes.
    sqlx::query("SELECT pg_advisory_xact_lock($1::bigint)")
        .bind(crate::advisory_lock::node_mutation_lock_key(&id))
        .execute(&mut *tx)
        .await
        .map_err(database_error("lock_node_audience_mutation"))?;
    let row = sqlx::query(
        "SELECT search_visibility,
                weltgewebe_search_node_owner_account_id(payload) AS owner_id
         FROM domain_nodes WHERE id=$1 FOR UPDATE",
    )
    .bind(&id)
    .fetch_optional(&mut *tx)
    .await
    .map_err(database_error("lock_node_audience"))?
    .ok_or((StatusCode::NOT_FOUND, "node not found".to_owned()))?;
    let owner: Option<String> = row.try_get("owner_id").map_err(database_error("owner"))?;
    if owner.as_deref() != Some(actor) {
        return Err((StatusCode::NOT_FOUND, "node not found".to_owned()));
    }
    let previous_visibility: String = row
        .try_get("search_visibility")
        .map_err(database_error("visibility"))?;
    if !matches!(previous_visibility.as_str(), "public" | "private") {
        return Err((
            StatusCode::CONFLICT,
            "hidden or revoked nodes cannot be granted to a circle".to_owned(),
        ));
    }

    if !selected.is_empty() {
        // Lock consent records BEFORE grant triggers acquire the global domain
        // generation writer lock. Otherwise a concurrent circle departure could
        // hold the circle row while waiting for generation, deadlocking this
        // transaction during its later INSERT foreign-key check.
        let valid: Vec<Uuid> = sqlx::query_scalar(
            "SELECT c.id FROM domain_audience_circles c
             JOIN domain_audience_circle_members m ON m.circle_id=c.id
             JOIN domain_accounts a ON a.id=m.account_id
             WHERE c.id=ANY($1::uuid[]) AND m.account_id=$2
               AND c.state='active' AND m.status='active' AND a.disabled=FALSE
             ORDER BY c.id
             FOR SHARE OF c, m, a",
        )
        .bind(&selected)
        .bind(actor)
        .fetch_all(&mut *tx)
        .await
        .map_err(database_error("validate_audience_circle_memberships"))?;
        if valid != selected {
            return Err((
                StatusCode::NOT_FOUND,
                "one or more audience circles are not active memberships".to_owned(),
            ));
        }
    }

    let old_ids: Vec<Uuid> = sqlx::query_scalar(
        "SELECT circle_id FROM domain_node_circle_grants WHERE node_id=$1 ORDER BY circle_id",
    )
    .bind(&id)
    .fetch_all(&mut *tx)
    .await
    .map_err(database_error("existing_node_grants"))?;
    let target_visibility = if input.scope == "public" {
        "public"
    } else {
        "private"
    };
    if previous_visibility == target_visibility && old_ids == selected {
        // A retried PUT after an accepted write remains idempotent.
        return Ok(no_store(audience_view(id, target_visibility, selected)));
    }
    let current_scope = audience_view(id.clone(), &previous_visibility, old_ids.clone()).scope;
    if current_scope != input.expected_scope || old_ids != expected_circle_ids {
        return Err((
            StatusCode::PRECONDITION_FAILED,
            "node audience changed since it was read; reload before saving".to_owned(),
        ));
    }

    // A public/private node change already triggers canonical search and
    // outbox invalidation. Grant INSERT/DELETE triggers bump the *same* domain
    // projection generation without inventing a node history event.
    sqlx::query("DELETE FROM domain_node_circle_grants WHERE node_id=$1")
        .bind(&id)
        .execute(&mut *tx)
        .await
        .map_err(database_error("revoke_old_node_grants"))?;
    // Audience edits also change the canonical node ETag used by ordinary
    // PUT/PATCH. Otherwise a stale editor can republish a newly private node.
    // The no-op return above avoids advancing the version on retries.
    sqlx::query(
        "UPDATE domain_nodes
         SET search_visibility=$2,
             updated_at=GREATEST(NOW(), updated_at + INTERVAL '1 microsecond')
         WHERE id=$1",
    )
    .bind(&id)
    .bind(target_visibility)
    .execute(&mut *tx)
    .await
    .map_err(database_error("set_node_visibility"))?;
    if !selected.is_empty() {
        sqlx::query(
            "INSERT INTO domain_node_circle_grants (node_id,circle_id)
             SELECT $1, unnest($2::uuid[])",
        )
        .bind(&id)
        .bind(&selected)
        .execute(&mut *tx)
        .await
        .map_err(database_error("store_node_circle_grants"))?;
    }
    tx.commit()
        .await
        .map_err(database_error("commit_node_audience"))?;
    Ok(no_store(audience_view(id, target_visibility, selected)))
}

#[cfg(test)]
mod tests {
    use super::SetNodeAudience;

    #[test]
    fn rejects_unspecified_and_duplicate_circle_grants() {
        let empty: SetNodeAudience =
            serde_json::from_str(r#"{"scope":"circles","expected_scope":"public"}"#).unwrap();
        assert!(empty.validate().is_err());
        let duplicate: SetNodeAudience = serde_json::from_str(
            r#"{"scope":"circles","circle_ids":["00000000-0000-4000-8000-000000000001","00000000-0000-4000-8000-000000000001"],"expected_scope":"public"}"#,
        ).unwrap();
        assert!(duplicate.validate().is_err());
        let public_grant: SetNodeAudience = serde_json::from_str(
            r#"{"scope":"public","circle_ids":["00000000-0000-4000-8000-000000000001"],"expected_scope":"public"}"#,
        )
        .unwrap();
        assert!(public_grant.validate().is_err());
    }

    #[test]
    fn hidden_and_revoked_cannot_be_request_scopes() {
        for value in ["hidden", "revoked", "restricted", "household"] {
            let input: SetNodeAudience = serde_json::from_str(&format!(
                r#"{{"scope":"{value}","expected_scope":"public"}}"#
            ))
            .unwrap();
            assert!(input.validate().is_err());
        }
    }
}
