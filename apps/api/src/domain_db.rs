//! PostgreSQL loaders and mutation helpers for domain data (OPT-ARC-001).
//!
//! JSONL remains the default source. PostgreSQL is used only when the matching
//! read/write source is explicitly selected; route and startup guards prohibit
//! mixed-source writes and silent fallback.

use anyhow::{Context, Result};
use chrono::{DateTime, Timelike, Utc};
use futures_util::TryStreamExt;
use serde_json::{json, Map, Value};
use sqlx::{PgPool, Postgres, Transaction};
use uuid::Uuid;

use crate::auth::accounts::AccountStore;
use crate::auth::role::Role;
use crate::node_mutation::{insert_postgres_audit, node_hash, NodeMutationAudit};
use crate::routes::accounts::{
    ensure_radius_projection_value, map_json_to_public_account, radius_projection_matches_location,
    radius_projection_public_pos, AccountInternal, Location as AccountLocation, MAX_RADIUS_M,
    MIN_RADIUS_M, RADIUS_PROJECTION_KEY,
};
use crate::routes::auth::MAX_EMAIL_LEN;
use crate::routes::edges::{edge_is_permanently_unreachable, Edge, FadenType, LifecycleTimestamp};
use crate::routes::nodes::{normalize_account_id, Location, Node, SearchVisibility};
use crate::state::OrderedCache;

const DEFAULT_TIMESTAMP: &str = "1970-01-01T00:00:00Z";

/// PostgreSQL stores `timestamptz` with microsecond precision. Canonicalising
/// before persistence keeps response/cache versions byte-identical to a later
/// authoritative database read, which is required for stable `If-Match` tags.
fn postgres_timestamp_precision(value: DateTime<Utc>) -> DateTime<Utc> {
    value
        .with_nanosecond(value.timestamp_subsec_micros() * 1_000)
        .expect("microsecond timestamp is always valid")
}

/// One client-generated create operation, scoped to the authenticated account.
/// Resource ids and timestamps remain server-owned; this key only identifies a
/// retry of the same user action.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CreateOperationKey {
    pub actor_id: String,
    pub operation_id: String,
}

/// Durable create result. `Existing` means the operation key was already
/// committed; the route still verifies that the semantic request is identical.
#[derive(Debug, Clone, PartialEq)]
pub enum CreateWriteOutcome<T> {
    Created,
    Existing(T),
}

type NodeRow = (
    String,
    String,
    String,
    Option<f64>,
    Option<f64>,
    Option<DateTime<Utc>>,
    Option<DateTime<Utc>>,
    String,
    String,
);

type EdgeRow = (
    String,
    String,
    String,
    String,
    Option<DateTime<Utc>>,
    String,
);

type AccountRow = (
    String,
    String,
    String,
    String,
    i64,
    bool,
    Option<f64>,
    Option<f64>,
    String,
    Option<String>,
    Option<String>,
    String,
    String,
);

fn parse_payload(text: &str) -> Value {
    serde_json::from_str(text).unwrap_or_else(|_| Value::Object(Map::new()))
}

fn payload_string(payload: &Value, key: &str) -> Option<String> {
    payload
        .get(key)
        .and_then(|v| v.as_str())
        .map(|s| s.to_string())
}

/// Typed Faden metadata is atomic. Unknown types, malformed UUIDs and half
/// pairs are rejected instead of being silently downgraded to a legacy Faden.
fn payload_faden_metadata(payload: &Value) -> Result<(Option<FadenType>, Option<String>), ()> {
    match (payload.get("faden_type"), payload.get("faden_subject_id")) {
        (None, None) => Ok((None, None)),
        (Some(Value::String(kind)), Some(Value::String(subject_id))) => {
            let faden_type = serde_json::from_value(Value::String(kind.clone())).map_err(|_| ())?;
            Uuid::parse_str(subject_id).map_err(|_| ())?;
            Ok((Some(faden_type), Some(subject_id.clone())))
        }
        _ => Err(()),
    }
}

/// Read a lifecycle timestamp field while preserving the distinction between
/// an omitted key (`None`) and an explicit `null` (`Some(None)`), matching
/// `Edge::expires_at`'s tri-state deserialization. A present non-string,
/// non-null value is structurally malformed payload data — returning `Err`
/// lets the caller reject the whole row instead of coercing it into the
/// explicit-null state, which would let a corrupted value pass as the
/// legitimate fully-undated legacy pairing when `created_at` is also absent.
fn payload_lifecycle_field(
    payload: &Value,
    key: &str,
) -> Result<Option<Option<crate::routes::edges::LifecycleTimestamp>>, ()> {
    match payload.get(key) {
        None => Ok(None),
        Some(Value::Null) => Ok(Some(None)),
        Some(Value::String(s)) => Ok(Some(Some(crate::routes::edges::LifecycleTimestamp::from(
            s.as_str(),
        )))),
        Some(_) => Err(()),
    }
}

fn payload_string_array(payload: &Value, key: &str) -> Vec<String> {
    payload
        .get(key)
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|x| x.as_str().map(String::from))
                .collect()
        })
        .unwrap_or_default()
}

fn node_timestamps(
    created: Option<DateTime<Utc>>,
    updated: Option<DateTime<Utc>>,
) -> (String, String) {
    let created_s = created.map(|t| t.to_rfc3339());
    let updated_s = updated.map(|t| t.to_rfc3339());
    let created_at = created_s
        .as_ref()
        .or(updated_s.as_ref())
        .cloned()
        .unwrap_or_else(|| DEFAULT_TIMESTAMP.to_string());
    let updated_at = updated_s
        .or(created_s)
        .unwrap_or_else(|| DEFAULT_TIMESTAMP.to_string());
    (created_at, updated_at)
}

pub async fn load_nodes_from_postgres(pool: &PgPool) -> Result<OrderedCache<Node>> {
    let rows: Vec<NodeRow> = sqlx::query_as(
        "SELECT id, kind, title, lat, lon, created_at, updated_at, payload::text, search_visibility \
         FROM domain_nodes ORDER BY id ASC",
    )
    .fetch_all(pool)
    .await
    .context("failed to load nodes from domain_nodes")?;

    let mut cache = OrderedCache::new();
    let mut skipped = 0usize;
    for row in rows {
        let node_id = row.0.clone();
        match node_from_row(row) {
            Ok(node) => {
                cache.insert(node_id, node);
            }
            Err(err) => {
                tracing::warn!(node_id = %node_id, ?err, "skipping invalid domain node row");
                skipped += 1;
            }
        }
    }

    tracing::info!(count = cache.len(), skipped, "Loaded nodes from PostgreSQL");
    Ok(cache)
}

/// Load one bounded, id-ordered node page directly from PostgreSQL for map BBOX reads.
///
/// The route keeps the historical in-memory/JSONL path for non-PostgreSQL
/// deployments. PostgreSQL-backed viewport requests use this helper so BBOX
/// selection no longer iterates the global in-process node cache. Projection
/// refresh and cache-generation behavior outside this query remain unchanged.
pub async fn load_nodes_bbox_from_postgres(
    pool: &PgPool,
    min_lat: f64,
    max_lat: f64,
    min_lng: f64,
    max_lng: f64,
    limit: usize,
    offset: i64,
) -> Result<Vec<Node>> {
    let limit = i64::try_from(limit).context("node BBOX limit exceeds PostgreSQL integer range")?;
    let rows: Vec<NodeRow> = sqlx::query_as(
        "SELECT id, kind, title, lat, lon, created_at, updated_at, payload::text, search_visibility \
         FROM domain_nodes \
         WHERE lat >= $1 AND lat <= $2 AND lon >= $3 AND lon <= $4 \
         ORDER BY id ASC LIMIT $5 OFFSET $6",
    )
    .bind(min_lat)
    .bind(max_lat)
    .bind(min_lng)
    .bind(max_lng)
    .bind(limit)
    .bind(offset)
    .fetch_all(pool)
    .await
    .context("failed to load BBOX node page from domain_nodes")?;

    // NULL coordinates cannot satisfy the BBOX predicate and
    // search_visibility is database-constrained. Any remaining mapping error is
    // therefore fail-visible rather than silently shrinking a limited page; this
    // deliberately differs from the full projection loader, which may skip bad rows.
    rows.into_iter()
        .map(node_from_row)
        .collect::<Result<Vec<_>>>()
        .context("failed to map BBOX node page from domain_nodes")
}

/// Load one cursor-anchored BBOX page directly from PostgreSQL.
///
/// Keeping this as a separate statement from the initial/legacy form avoids a
/// nullable `OR` predicate whose generic prepared plan could lose the clear
/// `id > cursor` selectivity. PostgreSQL owns the ordering; callers must not
/// re-sort the returned rows under Rust's string ordering.
pub async fn load_nodes_bbox_after_id_from_postgres(
    pool: &PgPool,
    min_lat: f64,
    max_lat: f64,
    min_lng: f64,
    max_lng: f64,
    limit: usize,
    after_id: &str,
) -> Result<Vec<Node>> {
    let limit = i64::try_from(limit).context("node BBOX limit exceeds PostgreSQL integer range")?;
    let rows: Vec<NodeRow> = sqlx::query_as(
        "SELECT id, kind, title, lat, lon, created_at, updated_at, payload::text, search_visibility \
         FROM domain_nodes \
         WHERE lat >= $1 AND lat <= $2 AND lon >= $3 AND lon <= $4 \
           AND id > $5 \
         ORDER BY id ASC LIMIT $6",
    )
    .bind(min_lat)
    .bind(max_lat)
    .bind(min_lng)
    .bind(max_lng)
    .bind(after_id)
    .bind(limit)
    .fetch_all(pool)
    .await
    .context("failed to load cursor-anchored BBOX node page from domain_nodes")?;

    // Rows with NULL lat/lon cannot satisfy the BBOX predicates, while
    // search_visibility is protected by a database CHECK constraint. A
    // remaining mapping failure therefore signals schema/data corruption and
    // stays fail-visible instead of silently changing cursor cardinality. This
    // deliberately differs from the full projection loader's skip-on-bad-row policy.
    rows.into_iter()
        .map(node_from_row)
        .collect::<Result<Vec<_>>>()
        .context("failed to map cursor-anchored BBOX node page from domain_nodes")
}

pub async fn load_edges_from_postgres(pool: &PgPool) -> Result<OrderedCache<Edge>> {
    let max_edges = crate::routes::edges::max_edges_cache_limit();
    // No SQL-side LIMIT: rejected rows must not consume a physical row slot
    // that a later valid row would need. The loop below bounds physical reads
    // itself, breaking as soon as it has examined `max_edges + 1` *accepted*
    // rows (the `+ 1` only confirms truncation; it is never cached).
    let mut rows = sqlx::query_as::<_, EdgeRow>(
        "SELECT id, source_id, target_id, edge_kind, created_at, payload::text \
         FROM domain_edges ORDER BY id ASC",
    )
    .fetch(pool);

    let mut cache = OrderedCache::new();
    let mut seen = 0usize;
    let mut skipped = 0usize;
    let mut truncated = false;
    while let Some((id, source_id, target_id, edge_kind, created_at, payload_text)) = rows
        .try_next()
        .await
        .context("failed to load edges from domain_edges")?
    {
        let payload = parse_payload(&payload_text);
        let Ok(expires_at) = payload_lifecycle_field(&payload, "expires_at") else {
            tracing::warn!(edge_id = %id, "skipping domain edge with malformed expires_at payload");
            skipped += 1;
            continue;
        };

        let Ok((faden_type, faden_subject_id)) = payload_faden_metadata(&payload) else {
            tracing::warn!(edge_id = %id, "skipping domain edge with malformed typed Faden metadata");
            skipped += 1;
            continue;
        };

        let edge = Edge {
            id: id.clone(),
            source_id,
            source_type: payload_string(&payload, "source_type"),
            target_id,
            target_type: payload_string(&payload, "target_type"),
            edge_kind,
            faden_type,
            faden_subject_id,
            note: payload_string(&payload, "note"),
            created_at: created_at.map(LifecycleTimestamp::from_datetime),
            expires_at,
        };
        // Checked before the cache-limit gate for the same reason as the
        // malformed-payload check above: a row that can never be active for
        // any `now` must never consume one of the `max_edges` slots that a
        // genuinely reachable edge would need.
        if edge_is_permanently_unreachable(&edge) {
            tracing::warn!(
                edge_id = %edge.id,
                "skipping domain edge that can never be active under any lifecycle rule"
            );
            skipped += 1;
            continue;
        }

        if seen >= max_edges {
            truncated = true;
            break;
        }

        cache.insert(id, edge);
        seen += 1;
    }
    if truncated {
        tracing::warn!(
            max_edges,
            "Edges cache limit reached, truncating PostgreSQL load"
        );
    }
    tracing::info!(
        count = cache.len(),
        skipped,
        truncated,
        "Loaded edges from PostgreSQL"
    );
    Ok(cache)
}

/// Read-only readiness counters for a future `domain_accounts.webauthn_user_id`
/// backfill (AUTH-PG-003).
///
/// This is deliberately an audit surface, not a migration primitive: it reads
/// `domain_accounts` and `passkey_credentials`, classifies unsafe states, and
/// performs no writes. A later backfill/NOT NULL decision must use these counts
/// as evidence instead of assuming that missing UUIDs can be regenerated safely.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct AuthUserIdBackfillAudit {
    pub accounts_total: i64,
    pub accounts_missing_auth_user_id: i64,
    pub credential_rows_total: i64,
    pub credential_account_ids_distinct: i64,
    pub credential_accounts_missing_auth_user_id: i64,
    pub credential_accounts_without_domain_account: i64,
    pub credential_accounts_with_multiple_auth_user_ids: i64,
    pub credential_accounts_with_account_credential_uuid_mismatch: i64,
}

impl AuthUserIdBackfillAudit {
    /// `true` when a later backfill would have any account rows to touch.
    pub fn has_backfill_scope(&self) -> bool {
        self.accounts_missing_auth_user_id > 0
    }

    /// `true` when current credential/account data is not safe for cutover or
    /// a mechanical NOT NULL migration without human review.
    pub fn has_cutover_blockers(&self) -> bool {
        self.credential_accounts_missing_auth_user_id > 0
            || self.credential_accounts_without_domain_account > 0
            || self.credential_accounts_with_multiple_auth_user_ids > 0
            || self.credential_accounts_with_account_credential_uuid_mismatch > 0
    }
}

async fn count_scalar(pool: &PgPool, sql: &str) -> Result<i64> {
    sqlx::query_scalar::<_, i64>(sql)
        .fetch_one(pool)
        .await
        .with_context(|| format!("failed to run AUTH-PG-003 audit query: {sql}"))
}

/// Audit `webauthn_user_id` backfill readiness without mutating the database.
///
/// The counters intentionally separate three cases that would otherwise be
/// easy to flatten incorrectly:
/// - accounts missing `webauthn_user_id` but carrying no credential yet
///   (backfill scope, not automatically a cutover blocker),
/// - credential-bearing accounts whose domain-account row has NULL
///   `webauthn_user_id` (identity repair required before cutover),
/// - credential rows that cannot be reconciled to exactly one account UUID
///   (orphan, multi-UUID, or mismatch blockers).
pub async fn audit_auth_user_id_backfill_readiness(
    pool: &PgPool,
) -> Result<AuthUserIdBackfillAudit> {
    let accounts_total = count_scalar(pool, "SELECT COUNT(*) FROM domain_accounts").await?;
    let accounts_missing_auth_user_id = count_scalar(
        pool,
        "SELECT COUNT(*) FROM domain_accounts WHERE webauthn_user_id IS NULL",
    )
    .await?;
    let credential_rows_total =
        count_scalar(pool, "SELECT COUNT(*) FROM passkey_credentials").await?;
    let credential_account_ids_distinct = count_scalar(
        pool,
        "SELECT COUNT(DISTINCT account_id) FROM passkey_credentials",
    )
    .await?;
    let credential_accounts_missing_auth_user_id = count_scalar(
        pool,
        "SELECT COUNT(*) FROM (\
             SELECT p.account_id \
             FROM passkey_credentials p \
             JOIN domain_accounts a ON a.id = p.account_id \
             WHERE a.webauthn_user_id IS NULL \
             GROUP BY p.account_id\
         ) credential_accounts_with_null_domain_uuid",
    )
    .await?;
    let credential_accounts_without_domain_account = count_scalar(
        pool,
        "SELECT COUNT(*) FROM (\
             SELECT p.account_id \
             FROM passkey_credentials p \
             LEFT JOIN domain_accounts a ON a.id = p.account_id \
             WHERE a.id IS NULL \
             GROUP BY p.account_id\
         ) orphan_credential_accounts",
    )
    .await?;
    let credential_accounts_with_multiple_auth_user_ids = count_scalar(
        pool,
        "SELECT COUNT(*) FROM (\
             SELECT account_id \
             FROM passkey_credentials \
             GROUP BY account_id \
             HAVING COUNT(DISTINCT webauthn_user_id) > 1\
         ) multi_webauthn_user_id_accounts",
    )
    .await?;
    let credential_accounts_with_account_credential_uuid_mismatch = count_scalar(
        pool,
        "SELECT COUNT(*) FROM (\
             SELECT p.account_id \
             FROM passkey_credentials p \
             JOIN domain_accounts a ON a.id = p.account_id \
             WHERE a.webauthn_user_id IS NOT NULL \
               AND p.webauthn_user_id <> a.webauthn_user_id \
             GROUP BY p.account_id\
         ) credential_account_uuid_mismatches",
    )
    .await?;

    Ok(AuthUserIdBackfillAudit {
        accounts_total,
        accounts_missing_auth_user_id,
        credential_rows_total,
        credential_account_ids_distinct,
        credential_accounts_missing_auth_user_id,
        credential_accounts_without_domain_account,
        credential_accounts_with_multiple_auth_user_ids,
        credential_accounts_with_account_credential_uuid_mismatch,
    })
}

/// Project one canonical `domain_accounts` row into an `AccountInternal`.
/// Returns `None` (with a warning logged) when the row fails
/// projection — the caller decides what "no account" means in its context.
fn account_row_to_internal(row: AccountRow) -> Option<AccountInternal> {
    let (
        id,
        kind,
        title,
        db_map_state,
        radius_m,
        disabled,
        location_lat,
        location_lon,
        role,
        email,
        webauthn_text,
        public_text,
        private_text,
    ) = row;

    if kind != "garnrolle" {
        tracing::warn!(account_id = %id, account_type = %kind, "rejecting non-canonical database account");
        return None;
    }
    if !matches!(db_map_state.as_str(), "not_on_map" | "exact" | "radius") {
        tracing::warn!(account_id = %id, map_state = %db_map_state, "rejecting database account with invalid map_state");
        return None;
    }

    let public_payload = parse_payload(&public_text);
    let private_payload = parse_payload(&private_text);
    for forbidden in ["visibility", "suppress_public_pos", "ron_flag"] {
        if private_payload.get(forbidden).is_some() {
            tracing::warn!(account_id = %id, field = forbidden, "rejecting removed private account field");
            return None;
        }
    }
    let effective_map_state = if db_map_state != "not_on_map"
        && (location_lat.is_none() || location_lon.is_none())
    {
        tracing::warn!(account_id = %id, "suppressing mapped database account without an internal location");
        "not_on_map"
    } else {
        db_map_state.as_str()
    };
    let effective_radius_m = if effective_map_state == "radius" {
        radius_m
    } else {
        0
    };

    let mut record = Map::new();
    record.insert("id".to_string(), json!(id));
    record.insert("type".to_string(), json!("garnrolle"));
    record.insert("title".to_string(), json!(title));
    record.insert("map_state".to_string(), json!(effective_map_state));
    if let Some(summary) = public_payload.get("summary").and_then(|v| v.as_str()) {
        record.insert("summary".to_string(), json!(summary));
    }
    if let Some(tags) = public_payload.get("tags") {
        if tags.is_array() {
            record.insert("tags".to_string(), tags.clone());
        }
    }
    if let (Some(lat), Some(lon)) = (location_lat, location_lon) {
        record.insert("location".to_string(), json!({ "lat": lat, "lon": lon }));
    }
    if let Some(projection) = private_payload.get(RADIUS_PROJECTION_KEY) {
        record.insert(RADIUS_PROJECTION_KEY.to_string(), projection.clone());
    }
    record.insert(
        "radius_m".to_string(),
        json!(effective_radius_m.max(0) as u64),
    );
    record.insert("disabled".to_string(), json!(disabled));

    let value = Value::Object(record);
    let public = match map_json_to_public_account(&value) {
        Some(public) => public,
        None => {
            tracing::warn!(account_id = %id, "skipping domain account that failed projection");
            return None;
        }
    };
    let role = Role::from_str_lossy(&role);
    let webauthn_user_id = webauthn_text
        .as_deref()
        .and_then(|s| Uuid::parse_str(s).ok())
        .unwrap_or_else(Uuid::new_v4);
    Some(AccountInternal {
        public,
        role,
        email,
        webauthn_user_id,
    })
}

pub async fn load_accounts_from_postgres(pool: &PgPool) -> Result<AccountStore> {
    let rows: Vec<AccountRow> = sqlx::query_as(
        "SELECT id, kind, title, map_state, radius_m, disabled, \
         location_lat, location_lon, role, email, \
         webauthn_user_id::text, public_payload::text, private_payload::text \
         FROM domain_accounts ORDER BY id ASC",
    )
    .fetch_all(pool)
    .await
    .context("failed to load accounts from domain_accounts")?;

    let mut store = AccountStore::new();
    let mut skipped = 0usize;
    let total = rows.len();
    for row in rows {
        match account_row_to_internal(row) {
            Some(account) => store.insert_unindexed(account),
            None => skipped += 1,
        }
    }
    debug_assert!(skipped <= total);
    store.rebuild_email_index();
    tracing::info!(
        count = store.len(),
        skipped,
        "Loaded accounts from PostgreSQL"
    );
    Ok(store)
}

/// Monotonic version advanced by the same triggers that append domain outbox
/// events. A process compares this value with its local projection generation
/// before serving PostgreSQL-backed domain reads.
pub async fn domain_projection_version(pool: &PgPool) -> Result<i64> {
    sqlx::query_scalar("SELECT version FROM domain_projection_state WHERE singleton = TRUE")
        .fetch_one(pool)
        .await
        .context("failed to read domain projection version")
}

/// Load all process-local domain projections while the committed database
/// generation is stable. Reads use independent pooled connections, but the
/// before/after version fence forces a retry whenever a committed domain
/// mutation overlaps the load. Therefore a returned projection cannot combine
/// rows from two committed generations.
pub async fn load_stable_domain_projection_from_postgres(
    pool: &PgPool,
) -> Result<(AccountStore, OrderedCache<Node>, OrderedCache<Edge>, i64)> {
    const MAX_ATTEMPTS: usize = 5;
    for attempt in 1..=MAX_ATTEMPTS {
        let before = domain_projection_version(pool).await?;
        let (accounts, nodes, edges) = tokio::try_join!(
            load_accounts_from_postgres(pool),
            load_nodes_from_postgres(pool),
            load_edges_from_postgres(pool),
        )?;
        let after = domain_projection_version(pool).await?;
        if before == after {
            return Ok((accounts, nodes, edges, after));
        }
        tracing::info!(
            attempt,
            before,
            after,
            "Domain projection changed during reload; retrying stable snapshot"
        );
    }
    anyhow::bail!("domain projection did not stabilize after {MAX_ATTEMPTS} reload attempts")
}

/// Look up exactly one account by its normalized, non-empty email
/// (`lower(btrim(email))`), mirroring the partial unique index
/// [`ACCOUNT_EMAIL_UNIQUE_CONSTRAINT`].
///
/// Used by the auth auto-provisioning race path: when a concurrent insert (a
/// different process, or a prior run) wins the unique-email race, this
/// resolves the account that actually persisted instead of fabricating a
/// second, divergent one.
pub async fn find_account_by_normalized_email(
    pool: &PgPool,
    email_norm: &str,
) -> Result<Option<AccountInternal>> {
    let row: Option<AccountRow> = sqlx::query_as(
        "SELECT id, kind, title, map_state, radius_m, disabled, \
         location_lat, location_lon, role, email, \
         webauthn_user_id::text, public_payload::text, private_payload::text \
         FROM domain_accounts WHERE lower(btrim(email)) = $1",
    )
    .bind(email_norm)
    .fetch_optional(pool)
    .await
    .context("failed to look up account by normalized email")?;

    Ok(row.and_then(account_row_to_internal))
}

// ── OPT-ARC-001 Phase E-B: node-patch write path ────────────────────────────
//
// Narrow PostgreSQL write helper for `PATCH /nodes` only. Applies the current
// JSONL patch semantics (info set/null/no-op, steckbrief cleanup) using a
// SELECT FOR UPDATE + conditional UPDATE transaction. Timestamp semantics are
// intentionally JSONL-parity: supplied `info` patches bump `updated_at` even
// when the projected value is unchanged; `steckbrief` cleanup also bumps it. It
// does NOT touch in-memory caches and does NOT write JSONL — the caller owns
// cache updates.
//
// This helper owns only node patches; account, node-create and edge writes
// use their dedicated helpers below.

/// Subset of node payload fields modified by `PATCH /nodes`.
#[derive(Debug, Clone, PartialEq)]
pub struct NodePatchInput {
    /// `None` = no-op; `Some(Some(s))` = set; `Some(None)` = clear.
    pub info: Option<Option<String>>,
    pub search_visibility: Option<SearchVisibility>,
}

/// Error from the node-patch write path.
#[derive(Debug, thiserror::Error)]
pub enum NodeWriteError {
    #[error("node not found")]
    NotFound,
    #[error("invalid edge reference blocks node deletion: {0}")]
    InvalidEdgeReference(String),
    #[error("node deletion is blocked because its public conversation has messages")]
    ConversationNotEmpty,
    #[error("node conversation lifecycle is inconsistent: {0}")]
    ConversationLifecycle(String),
    #[error("failed to map node row: {0}")]
    Mapping(#[source] anyhow::Error),
    #[error("failed to serialize node payload: {0}")]
    Serialization(#[source] serde_json::Error),
    #[error("failed to persist node to domain_nodes: {0}")]
    Database(#[source] sqlx::Error),
}

fn node_from_row(row: NodeRow) -> Result<Node, anyhow::Error> {
    let (id, kind, title, lat, lon, created_at, updated_at, payload_text, search_visibility_str) =
        row;
    let (lat, lon) = match (lat, lon) {
        (Some(lat), Some(lon)) => (lat, lon),
        _ => anyhow::bail!("domain node {} has NULL location", id),
    };
    let payload = parse_payload(&payload_text);
    let has_authoritative_created_at = created_at.is_some();
    let (created_at, updated_at) = node_timestamps(created_at, updated_at);
    let search_visibility = search_visibility_str
        .parse::<SearchVisibility>()
        .map_err(|error| {
            anyhow::anyhow!(
                "domain node {} has invalid search_visibility {:?}: {}",
                id,
                search_visibility_str,
                error
            )
        })?;
    Ok(Node {
        id,
        kind,
        title,
        created_at,
        updated_at,
        has_authoritative_created_at,
        created_by_account_id: normalize_account_id(
            payload_string(&payload, "created_by_account_id").as_deref(),
        ),
        search_visibility,
        summary: payload_string(&payload, "summary"),
        info: payload_string(&payload, "info"),
        tags: payload_string_array(&payload, "tags"),
        address: payload_string(&payload, "address"),
        location: Location { lat, lon },
    })
}

fn edge_from_row(row: EdgeRow) -> Result<Edge, anyhow::Error> {
    let (id, source_id, target_id, edge_kind, created_at, payload_text) = row;
    let payload = parse_payload(&payload_text);
    let expires_at = payload_lifecycle_field(&payload, "expires_at")
        .map_err(|()| anyhow::anyhow!("edge {id} has malformed expires_at payload value"))?;
    let (faden_type, faden_subject_id) = payload_faden_metadata(&payload)
        .map_err(|()| anyhow::anyhow!("edge {id} has malformed typed Faden metadata"))?;
    Ok(Edge {
        id,
        source_id,
        source_type: payload_string(&payload, "source_type"),
        target_id,
        target_type: payload_string(&payload, "target_type"),
        edge_kind,
        faden_type,
        faden_subject_id,
        note: payload_string(&payload, "note"),
        created_at: created_at.map(LifecycleTimestamp::from_datetime),
        expires_at,
    })
}

fn serialize_node_payload(node: &Node) -> Result<String, serde_json::Error> {
    let mut payload_map = Map::new();
    if let Some(created_by_account_id) = &node.created_by_account_id {
        payload_map.insert(
            "created_by_account_id".to_string(),
            json!(created_by_account_id),
        );
    }
    if let Some(summary) = &node.summary {
        payload_map.insert("summary".to_string(), json!(summary));
    }
    if let Some(info) = &node.info {
        payload_map.insert("info".to_string(), json!(info));
    }
    if !node.tags.is_empty() {
        payload_map.insert("tags".to_string(), json!(node.tags));
    }
    if let Some(address) = &node.address {
        payload_map.insert("address".to_string(), json!(address));
    }
    serde_json::to_string(&Value::Object(payload_map))
}

/// Load one node from PostgreSQL for an authoritative mutation precondition check.
///
/// Callers must hold the per-node mutation guard while comparing the returned
/// `updated_at` value and performing the subsequent mutation.
pub async fn load_node_from_postgres(
    pool: &PgPool,
    id: &str,
) -> Result<Option<Node>, NodeWriteError> {
    let row: Option<NodeRow> = sqlx::query_as(
        "SELECT id, kind, title, lat, lon, created_at, updated_at, payload::text, search_visibility \
         FROM domain_nodes WHERE id = $1",
    )
    .bind(id)
    .fetch_optional(pool)
    .await
    .map_err(NodeWriteError::Database)?;

    row.map(node_from_row)
        .transpose()
        .map_err(NodeWriteError::Mapping)
}

/// Apply a patch to one `domain_nodes` row inside a transaction.
pub async fn patch_node_in_postgres(
    pool: &PgPool,
    id: &str,
    patch: NodePatchInput,
) -> Result<Node, NodeWriteError> {
    let (node, ()) =
        patch_node_in_postgres_with_projection_precommit(pool, id, patch, |_| ()).await?;
    Ok(node)
}

/// Apply one node patch and expose the transaction-local projection generation
/// after the row trigger has run but before COMMIT makes that generation visible
/// to other connections.
///
/// `weltgewebe_enqueue_domain_event` increments the singleton
/// `domain_projection_state` row in the same transaction as the node UPDATE.
/// Once that UPDATE has completed, this transaction owns the projection-state
/// row lock until commit. Therefore the callback receives `Some(version)` only
/// for a real triggered UPDATE and can safely classify an exact local V+1 without
/// the pre-commit ambiguity of the broader `nodes_persist` mutex or a post-commit
/// marker gap. A no-op patch receives `None` and proves no local generation.
pub async fn patch_node_in_postgres_with_projection_precommit<T, F>(
    pool: &PgPool,
    id: &str,
    patch: NodePatchInput,
    on_projection_precommit: F,
) -> Result<(Node, T), NodeWriteError>
where
    F: FnOnce(Option<i64>) -> T,
{
    let mut tx = pool.begin().await.map_err(NodeWriteError::Database)?;

    let row: Option<NodeRow> = sqlx::query_as(
        "SELECT id, kind, title, lat, lon, created_at, updated_at, payload::text, search_visibility \
         FROM domain_nodes WHERE id = $1 FOR UPDATE",
    )
    .bind(id)
    .fetch_optional(&mut *tx)
    .await
    .map_err(NodeWriteError::Database)?;

    let (
        row_id,
        kind,
        title,
        lat,
        lon,
        created_at,
        updated_at,
        payload_text,
        current_search_visibility_str,
    ) = match row {
        Some(r) => r,
        None => {
            tx.rollback().await.ok();
            return Err(NodeWriteError::NotFound);
        }
    };

    let mut payload: serde_json::Value = parse_payload(&payload_text);
    let mut has_changes = false;

    {
        let obj = payload.as_object_mut().ok_or_else(|| {
            NodeWriteError::Mapping(anyhow::anyhow!("domain node {} has non-object payload", id))
        })?;

        match &patch.info {
            Some(Some(s)) => {
                if obj.get("info").and_then(serde_json::Value::as_str) != Some(s.as_str()) {
                    obj.insert("info".to_string(), serde_json::Value::String(s.clone()));
                    has_changes = true;
                }
            }
            Some(None) => {
                if obj.get("info").is_some_and(|value| !value.is_null()) {
                    obj.remove("info");
                    has_changes = true;
                }
            }
            None => {}
        }
        if obj.remove("steckbrief").is_some() {
            has_changes = true;
        }
    }

    let new_search_visibility = match patch.search_visibility {
        Some(vis) => {
            if vis.as_str() != current_search_visibility_str {
                has_changes = true;
            }
            vis
        }
        None => current_search_visibility_str
            .parse::<SearchVisibility>()
            .map_err(|error| {
                NodeWriteError::Mapping(anyhow::anyhow!(
                    "domain node {} has invalid search_visibility {:?}: {}",
                    id,
                    current_search_visibility_str,
                    error
                ))
            })?,
    };

    let final_payload_text =
        serde_json::to_string(&payload).map_err(NodeWriteError::Serialization)?;

    let new_updated_at = if has_changes {
        let now = postgres_timestamp_precision(chrono::Utc::now());
        sqlx::query(
            "UPDATE domain_nodes \
             SET payload = $2::jsonb, updated_at = $3, search_visibility = $4 \
             WHERE id = $1",
        )
        .bind(id)
        .bind(&final_payload_text)
        .bind(now)
        .bind(new_search_visibility.as_str())
        .execute(&mut *tx)
        .await
        .map_err(NodeWriteError::Database)?;
        Some(now)
    } else {
        updated_at
    };

    let final_node = node_from_row((
        row_id,
        kind,
        title,
        lat,
        lon,
        created_at,
        new_updated_at,
        final_payload_text,
        new_search_visibility.as_str().to_string(),
    ))
    .map_err(NodeWriteError::Mapping)?;

    let projection_version_before_commit = if has_changes {
        Some(
            sqlx::query_scalar(
                "SELECT version FROM domain_projection_state WHERE singleton = TRUE",
            )
            .fetch_one(&mut *tx)
            .await
            .map_err(NodeWriteError::Database)?,
        )
    } else {
        None
    };
    let precommit = on_projection_precommit(projection_version_before_commit);

    tx.commit().await.map_err(NodeWriteError::Database)?;

    Ok((final_node, precommit))
}

pub async fn replace_node_in_postgres(
    pool: &PgPool,
    node: &Node,
) -> Result<DateTime<Utc>, NodeWriteError> {
    replace_node_in_postgres_inner(pool, node, None).await
}

pub async fn replace_node_in_postgres_audited(
    pool: &PgPool,
    node: &Node,
    audit: &NodeMutationAudit,
) -> Result<DateTime<Utc>, NodeWriteError> {
    replace_node_in_postgres_inner(pool, node, Some(audit)).await
}

async fn replace_node_in_postgres_inner(
    pool: &PgPool,
    node: &Node,
    audit: Option<&NodeMutationAudit>,
) -> Result<DateTime<Utc>, NodeWriteError> {
    let payload = serialize_node_payload(node).map_err(NodeWriteError::Serialization)?;
    let updated_at = DateTime::parse_from_rfc3339(&node.updated_at)
        .map(|value| postgres_timestamp_precision(value.with_timezone(&Utc)))
        .map_err(|error| NodeWriteError::Mapping(anyhow::Error::new(error)))?;
    let mut tx = pool.begin().await.map_err(NodeWriteError::Database)?;
    let persisted_row: Option<NodeRow> = sqlx::query_as(
        "UPDATE domain_nodes \
         SET kind = $2, title = $3, lat = $4, lon = $5, updated_at = $6, payload = $7::jsonb, search_visibility = $8 \
         WHERE id = $1 \
         RETURNING id, kind, title, lat, lon, created_at, updated_at, payload::text, search_visibility",
    )
    .bind(&node.id)
    .bind(&node.kind)
    .bind(&node.title)
    .bind(node.location.lat)
    .bind(node.location.lon)
    .bind(updated_at)
    .bind(payload)
    .bind(node.search_visibility.as_str())
    .fetch_optional(&mut *tx)
    .await
    .map_err(NodeWriteError::Database)?;
    let Some(persisted_row) = persisted_row else {
        tx.rollback().await.ok();
        return Err(NodeWriteError::NotFound);
    };
    let persisted_node = node_from_row(persisted_row).map_err(NodeWriteError::Mapping)?;
    if let Some(audit) = audit {
        let mut persisted_audit = audit.clone();
        persisted_audit.after_hash =
            Some(node_hash(&persisted_node).map_err(NodeWriteError::Serialization)?);
        insert_postgres_audit(&mut tx, &persisted_audit)
            .await
            .map_err(NodeWriteError::Database)?;
    }
    tx.commit().await.map_err(NodeWriteError::Database)?;
    Ok(updated_at)
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum NodeConversationDeleteEffect {
    DeletedEmpty,
    Archived { archive_id: String },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct NodeDeleteOutcome {
    pub removed_edge_ids: Vec<String>,
    pub conversation: NodeConversationDeleteEffect,
}

pub async fn delete_node_with_edges_in_postgres(
    pool: &PgPool,
    id: &str,
) -> Result<NodeDeleteOutcome, NodeWriteError> {
    delete_node_with_edges_in_postgres_inner(pool, id, None).await
}

pub async fn delete_node_with_edges_in_postgres_audited(
    pool: &PgPool,
    id: &str,
    audit: &NodeMutationAudit,
) -> Result<NodeDeleteOutcome, NodeWriteError> {
    delete_node_with_edges_in_postgres_inner(pool, id, Some(audit)).await
}

async fn delete_node_with_edges_in_postgres_inner(
    pool: &PgPool,
    id: &str,
    audit: Option<&NodeMutationAudit>,
) -> Result<NodeDeleteOutcome, NodeWriteError> {
    let mut tx = pool.begin().await.map_err(NodeWriteError::Database)?;
    // Edge creation uses the same table lock. Holding it through the node and
    // projection deletes prevents an edge insert from interleaving with the
    // cascade in another process.
    sqlx::query("LOCK TABLE domain_edges IN EXCLUSIVE MODE")
        .execute(&mut *tx)
        .await
        .map_err(NodeWriteError::Database)?;
    let node_id: Option<String> =
        sqlx::query_scalar("SELECT id FROM domain_nodes WHERE id = $1 FOR UPDATE")
            .bind(id)
            .fetch_optional(&mut *tx)
            .await
            .map_err(NodeWriteError::Database)?;
    if node_id.is_none() {
        tx.rollback().await.ok();
        return Err(NodeWriteError::NotFound);
    }

    let conversation: Option<(String, bool)> = sqlx::query_as(
        "SELECT conversation.id::text, EXISTS (\
             SELECT 1 FROM domain_messages AS message \
             WHERE message.conversation_id = conversation.id\
         ) AS has_messages \
         FROM domain_conversations AS conversation \
         WHERE conversation.node_id = $1 FOR UPDATE",
    )
    .bind(id)
    .fetch_optional(&mut *tx)
    .await
    .map_err(NodeWriteError::Database)?;
    let Some((conversation_id, conversation_has_messages)) = conversation else {
        tx.rollback().await.ok();
        return Err(NodeWriteError::ConversationLifecycle(
            "node has no generated conversation".to_string(),
        ));
    };

    // The database BEFORE DELETE trigger owns the history decision. Empty
    // generated conversations cascade away; non-empty conversations are detached
    // and archived atomically before this transaction removes the node.

    let account_collision_exists: bool =
        sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM domain_accounts WHERE id = $1)")
            .bind(id)
            .fetch_one(&mut *tx)
            .await
            .map_err(NodeWriteError::Database)?;
    let role_collision_exists: bool = sqlx::query_scalar(
        "SELECT EXISTS (\
             SELECT 1 FROM domain_edges \
             WHERE (source_id = $1 AND payload->>'source_type' = 'role') \
                OR (target_id = $1 AND payload->>'target_type' = 'role')\
         )",
    )
    .bind(id)
    .fetch_one(&mut *tx)
    .await
    .map_err(NodeWriteError::Database)?;
    let untyped_endpoint_is_ambiguous = account_collision_exists || role_collision_exists;

    let invalid_edge: Option<(String, Option<String>, Option<String>)> = sqlx::query_as(
        "SELECT id, \
                CASE WHEN source_id = $1 THEN payload->>'source_type' ELSE NULL END AS source_type, \
                CASE WHEN target_id = $1 THEN payload->>'target_type' ELSE NULL END AS target_type \
         FROM domain_edges \
         WHERE (source_id = $1 AND (\
                    jsonb_typeof(payload) <> 'object' \
                 OR (payload ? 'source_type' AND (\
                        jsonb_typeof(payload->'source_type') <> 'string' \
                        OR payload->>'source_type' NOT IN ('node', 'account', 'role')\
                    )) \
                 OR (NOT (payload ? 'source_type') AND $2)\
               )) \
            OR (target_id = $1 AND (\
                    jsonb_typeof(payload) <> 'object' \
                 OR (payload ? 'target_type' AND (\
                        jsonb_typeof(payload->'target_type') <> 'string' \
                        OR payload->>'target_type' NOT IN ('node', 'account', 'role')\
                    )) \
                 OR (NOT (payload ? 'target_type') AND $2)\
               )) \
         ORDER BY id \
         LIMIT 1",
    )
    .bind(id)
    .bind(untyped_endpoint_is_ambiguous)
    .fetch_optional(&mut *tx)
    .await
    .map_err(NodeWriteError::Database)?;
    if let Some((edge_id, source_type, target_type)) = invalid_edge {
        tx.rollback().await.ok();
        return Err(NodeWriteError::InvalidEdgeReference(format!(
            "edge {edge_id} has source_type={source_type:?} target_type={target_type:?} for endpoint id {id}"
        )));
    }

    let mut edge_ids: Vec<String> = sqlx::query_scalar(
        "DELETE FROM domain_edges \
         WHERE (source_id = $1 AND (\
                    payload->>'source_type' = 'node' \
                 OR (NOT (payload ? 'source_type') AND NOT $2)\
               )) \
            OR (target_id = $1 AND (\
                    payload->>'target_type' = 'node' \
                 OR (NOT (payload ? 'target_type') AND NOT $2)\
               )) \
         RETURNING id",
    )
    .bind(id)
    .bind(untyped_endpoint_is_ambiguous)
    .fetch_all(&mut *tx)
    .await
    .map_err(NodeWriteError::Database)?;
    edge_ids.sort();
    sqlx::query("DELETE FROM domain_nodes WHERE id = $1")
        .bind(id)
        .execute(&mut *tx)
        .await
        .map_err(NodeWriteError::Database)?;

    let conversation = if conversation_has_messages {
        let archived: bool = sqlx::query_scalar(
            "SELECT EXISTS (\
                 SELECT 1 FROM domain_conversations \
                 WHERE id = $1::uuid \
                   AND node_id IS NULL \
                   AND archived_at IS NOT NULL\
             )",
        )
        .bind(&conversation_id)
        .fetch_one(&mut *tx)
        .await
        .map_err(NodeWriteError::Database)?;
        if !archived {
            tx.rollback().await.ok();
            return Err(NodeWriteError::ConversationLifecycle(
                "non-empty conversation was not archived by node deletion".to_string(),
            ));
        }
        NodeConversationDeleteEffect::Archived {
            archive_id: conversation_id,
        }
    } else {
        let still_exists: bool = sqlx::query_scalar(
            "SELECT EXISTS (SELECT 1 FROM domain_conversations WHERE id = $1::uuid)",
        )
        .bind(&conversation_id)
        .fetch_one(&mut *tx)
        .await
        .map_err(NodeWriteError::Database)?;
        if still_exists {
            tx.rollback().await.ok();
            return Err(NodeWriteError::ConversationLifecycle(
                "empty conversation did not cascade with node deletion".to_string(),
            ));
        }
        NodeConversationDeleteEffect::DeletedEmpty
    };

    if let Some(audit) = audit {
        insert_postgres_audit(&mut tx, audit)
            .await
            .map_err(NodeWriteError::Database)?;
    }
    tx.commit().await.map_err(NodeWriteError::Database)?;
    Ok(NodeDeleteOutcome {
        removed_edge_ids: edge_ids,
        conversation,
    })
}

// ── node-create write path ──────────────────────────────────────────────────
//
// Narrow PostgreSQL write helper for `POST /nodes` only. Maps the validated
// `Node` the create route already builds (server-owned id/timestamps) into a
// `domain_nodes` row inside a transaction. Payload keys mirror
// `load_nodes_from_postgres` (summary, info, tags, address) — no new key
// names. It does NOT touch in-memory caches and does NOT write JSONL — the
// caller owns cache updates.

/// Error from the node-create write path.
#[derive(Debug, thiserror::Error)]
pub enum NodeCreateError {
    #[error("node id already exists")]
    DuplicateId,
    #[error("node creator account is missing or disabled")]
    CreatorAccountUnavailable,
    #[error("node creator reached the maximum of {max} owned nodes")]
    CreatorNodeLimitReached { max: usize },
    #[error("failed to map existing node row: {0}")]
    Mapping(#[source] anyhow::Error),
    #[error("failed to serialize node payload: {0}")]
    Serialization(#[source] serde_json::Error),
    #[error("invalid server-generated node timestamp: {0}")]
    InvalidTimestamp(#[source] chrono::ParseError),
    #[error("failed to persist node to domain_nodes: {0}")]
    Database(#[source] sqlx::Error),
}

/// Insert exactly one node row into `domain_nodes` (`POST /nodes`).
///
/// Runs inside a transaction and relies on the primary-key constraint for
/// race-safe duplicate detection. Node ids are server-generated UUIDv4s, so a
/// table-wide lock and a separate precheck would only serialize unrelated
/// writers without adding correctness.
///
/// This function performs no in-memory mutation and writes no JSONL.
pub async fn insert_domain_node(
    pool: &PgPool,
    node: &Node,
    operation: Option<&CreateOperationKey>,
) -> Result<CreateWriteOutcome<Node>, NodeCreateError> {
    insert_domain_node_with_creator_limit(pool, node, operation, None).await
}

/// Variant used by guest node creation. When a limit is supplied, the creator
/// account row is locked `FOR UPDATE`, serializing the count-and-insert decision
/// across all API instances. Operation replays are resolved before the limit so
/// a successful prior create remains safely retryable even at the cap.
async fn insert_domain_node_in_transaction(
    tx: &mut Transaction<'_, Postgres>,
    node: &Node,
    operation: Option<&CreateOperationKey>,
    creator_node_limit: Option<usize>,
) -> Result<CreateWriteOutcome<Node>, NodeCreateError> {
    let payload = serialize_node_payload(node).map_err(NodeCreateError::Serialization)?;

    // These values are generated by the server. A parse failure therefore
    // signals an internal contract violation and must fail before any write.
    let created_at = DateTime::parse_from_rfc3339(&node.created_at)
        .map(|dt| dt.with_timezone(&Utc))
        .map_err(NodeCreateError::InvalidTimestamp)?;
    let updated_at = DateTime::parse_from_rfc3339(&node.updated_at)
        .map(|dt| dt.with_timezone(&Utc))
        .map_err(NodeCreateError::InvalidTimestamp)?;

    // Bind creator identity to a live canonical account for the full create
    // transaction. `FOR KEY SHARE` is compatible with unrelated account
    // updates, but conflicts with the `FOR UPDATE` lock taken by guest exit.
    if let Some(creator_account_id) = node.created_by_account_id.as_deref() {
        let active_creator: Option<String> = if creator_node_limit.is_some() {
            sqlx::query_scalar(
                "SELECT id FROM domain_accounts \
                 WHERE id = $1 AND disabled = FALSE FOR UPDATE",
            )
            .bind(creator_account_id)
            .fetch_optional(&mut **tx)
            .await
            .map_err(NodeCreateError::Database)?
        } else {
            sqlx::query_scalar(
                "SELECT id FROM domain_accounts \
                 WHERE id = $1 AND disabled = FALSE FOR KEY SHARE",
            )
            .bind(creator_account_id)
            .fetch_optional(&mut **tx)
            .await
            .map_err(NodeCreateError::Database)?
        };
        if active_creator.is_none() {
            return Err(NodeCreateError::CreatorAccountUnavailable);
        }
    }

    if let Some(operation) = operation {
        let lock_key = crate::advisory_lock::stable_advisory_lock_key(
            "weltgewebe:create-operation:v1",
            &[&operation.actor_id, &operation.operation_id],
        );
        sqlx::query("SELECT pg_advisory_xact_lock($1::bigint)")
            .bind(lock_key)
            .fetch_one(&mut **tx)
            .await
            .map_err(NodeCreateError::Database)?;

        let existing: Option<NodeRow> = sqlx::query_as(
            "SELECT id, kind, title, lat, lon, created_at, updated_at, payload::text, search_visibility \
             FROM domain_nodes \
             WHERE create_actor_id = $1 AND create_operation_id = $2",
        )
        .bind(&operation.actor_id)
        .bind(&operation.operation_id)
        .fetch_optional(&mut **tx)
        .await
        .map_err(NodeCreateError::Database)?;

        if let Some(row) = existing {
            let existing = node_from_row(row).map_err(NodeCreateError::Mapping)?;
            return Ok(CreateWriteOutcome::Existing(existing));
        }
    }

    if let (Some(creator_account_id), Some(max)) =
        (node.created_by_account_id.as_deref(), creator_node_limit)
    {
        let max_i64 = i64::try_from(max).unwrap_or(i64::MAX);
        let owned_count: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM domain_nodes \
             WHERE payload ? 'created_by_account_id' \
               AND weltgewebe_search_node_owner_account_id(payload) = trim($1)",
        )
        .bind(creator_account_id)
        .fetch_one(&mut **tx)
        .await
        .map_err(NodeCreateError::Database)?;
        if owned_count >= max_i64 {
            return Err(NodeCreateError::CreatorNodeLimitReached { max });
        }
    }

    let result = sqlx::query(
        "INSERT INTO domain_nodes \
            (id, kind, title, lat, lon, created_at, updated_at, payload, \
             create_actor_id, create_operation_id, search_visibility) \
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11)",
    )
    .bind(&node.id)
    .bind(&node.kind)
    .bind(&node.title)
    .bind(node.location.lat)
    .bind(node.location.lon)
    .bind(created_at)
    .bind(updated_at)
    .bind(&payload)
    .bind(operation.map(|value| value.actor_id.as_str()))
    .bind(operation.map(|value| value.operation_id.as_str()))
    .bind(node.search_visibility.as_str())
    .execute(&mut **tx)
    .await;

    match result {
        Ok(_) => Ok(CreateWriteOutcome::Created),
        Err(sqlx::Error::Database(db_err)) if db_err.is_unique_violation() => {
            Err(NodeCreateError::DuplicateId)
        }
        Err(error) => Err(NodeCreateError::Database(error)),
    }
}

pub async fn insert_domain_node_with_creator_limit(
    pool: &PgPool,
    node: &Node,
    operation: Option<&CreateOperationKey>,
    creator_node_limit: Option<usize>,
) -> Result<CreateWriteOutcome<Node>, NodeCreateError> {
    let mut tx = pool.begin().await.map_err(NodeCreateError::Database)?;
    let outcome =
        insert_domain_node_in_transaction(&mut tx, node, operation, creator_node_limit).await;
    match outcome {
        Ok(outcome) => {
            tx.commit().await.map_err(NodeCreateError::Database)?;
            Ok(outcome)
        }
        Err(error) => {
            tx.rollback().await.ok();
            Err(error)
        }
    }
}

// ── OPT-ARC-001 Phase E-A: account-create write path ────────────────────────
//
// Narrow PostgreSQL write helper for `POST /accounts` only. It maps the same
// validated, JSONL-shaped account record that `create_account` would otherwise
// append to JSONL, using the same semantic mapping as the Phase C backfill
// (`tests/db_domain_backfill.rs::import_accounts`) so that a row written here is
// indistinguishable from "JSONL create + Phase C backfill". It does NOT touch
// in-memory caches and does NOT write JSONL — the caller owns cache updates.
//
// This helper owns account insertion only; node and edge writes use their
// dedicated helpers.

fn json_f64(v: &Value) -> Option<f64> {
    v.as_f64()
        .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
}

fn parse_ts(v: &Value) -> Option<DateTime<Utc>> {
    v.as_str().and_then(|s| s.parse().ok())
}

/// Serialise the listed keys of `source` into a compact JSON object string,
/// skipping absent or null keys (mirrors the Phase C backfill payload helper).
fn payload_from_keys(keys: &[&str], source: &Value) -> String {
    let mut m = Map::new();
    for &k in keys {
        if let Some(val) = source.get(k) {
            if !val.is_null() {
                m.insert(k.to_string(), val.clone());
            }
        }
    }
    serde_json::to_string(&Value::Object(m)).unwrap_or_else(|_| "{}".to_string())
}

/// A single row destined for `domain_accounts`, built from a validated,
/// JSONL-shaped account record. UUID and JSON columns are carried as text so
/// they can be bound with explicit `::uuid` / `::jsonb` casts (the sqlx build
/// has no `uuid` feature), exactly as the Phase C backfill binds them.
#[derive(Debug, Clone, PartialEq)]
pub struct NewDomainAccountRow {
    pub id: String,
    pub kind: String,
    pub title: String,
    pub map_state: String,
    pub radius_m: i64,
    pub disabled: bool,
    pub location_lat: Option<f64>,
    pub location_lon: Option<f64>,
    pub role: String,
    pub email: Option<String>,
    /// UUID as text (validated), bound with `$n::uuid`; `None` stores NULL.
    pub webauthn_user_id: Option<String>,
    pub created_at: Option<DateTime<Utc>>,
    pub updated_at: Option<DateTime<Utc>>,
    pub public_payload: String,
    pub private_payload: String,
}

impl NewDomainAccountRow {
    /// Map a validated, JSONL-shaped account record to a `domain_accounts` row.
    ///
    /// This mirrors the Phase C backfill mapping exactly so the Phase D loader
    /// reconstructs the same public projection. In particular:
    /// - `type` must be the canonical `garnrolle`
    /// - `map_state` must be `not_on_map`, `exact`, or `radius`
    /// - removed identity and visibility fields are rejected rather than repaired
    /// - `radius_m` is accepted only from 50 through 5,000 metres for a
    ///   radius projection; records without a valid private binding fail closed
    ///   as `not_on_map`
    /// - `private_payload` carries the typed private `radius_projection` binding.
    ///   The binding is never
    ///   copied into `AccountPublic` or an outbox payload.
    /// - `created_at` / `updated_at` are taken from the record if present, else
    ///   NULL. The current create path never sets them, so account-create rows
    ///   store NULL — identical to JSONL-create followed by Phase C backfill.
    ///   These columns are not part of the public projection and are not read by
    ///   the loader, so the choice is non-observable via the API.
    pub fn from_jsonl_record(v: &Value) -> Result<Self> {
        let id = v
            .get("id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .map(|s| s.to_string())
            .context("account record is missing a non-empty id")?;

        let kind = match v.get("type").and_then(|value| value.as_str()) {
            Some("garnrolle") => "garnrolle".to_string(),
            Some(other) => anyhow::bail!("invalid account type: {other}"),
            None => anyhow::bail!("account record is missing type=garnrolle"),
        };
        for forbidden in ["mode", "ron_flag", "visibility", "suppress_public_pos"] {
            if v.get(forbidden).is_some() {
                anyhow::bail!("removed account field is not accepted: {forbidden}");
            }
        }
        let title = v
            .get("title")
            .and_then(|v| v.as_str())
            .unwrap_or("Untitled")
            .to_string();

        let disabled = v.get("disabled").and_then(|v| v.as_bool()).unwrap_or(false);

        let location_lat = v
            .get("location")
            .and_then(|l| l.get("lat"))
            .and_then(json_f64);
        let location_lon = v
            .get("location")
            .and_then(|l| l.get("lon"))
            .and_then(json_f64);

        let role = v
            .get("role")
            .and_then(|v| v.as_str())
            .unwrap_or("gast")
            .to_string();

        // Normalize like the API create path: trim, then treat an after-trim
        // empty value as "no email" (None). This keeps the persisted value
        // consistent with the normalized unique index `lower(btrim(email))` and
        // the `domain_accounts_email_not_empty_after_trim` check constraint.
        let email = v
            .get("email")
            .and_then(|v| v.as_str())
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(|s| s.to_string());

        // Only persist a UUID we can actually parse; otherwise store NULL.
        let webauthn_user_id = v
            .get("webauthn_user_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .filter(|s| Uuid::parse_str(s).is_ok())
            .map(|s| s.to_string());

        let created_at = v.get("created_at").and_then(parse_ts);
        let updated_at = v.get("updated_at").and_then(parse_ts);

        let radius_raw = v.get("radius_m").and_then(|v| v.as_u64()).unwrap_or(0);
        let mut radius_m = i64::try_from(radius_raw).unwrap_or(0);
        let invalid_radius = radius_raw > 0
            && u32::try_from(radius_raw)
                .ok()
                .is_none_or(|radius_m| !(MIN_RADIUS_M..=MAX_RADIUS_M).contains(&radius_m));
        let explicit_map_state = match v.get("map_state").and_then(|value| value.as_str()) {
            Some(state @ ("not_on_map" | "exact" | "radius")) => state,
            Some(state) => anyhow::bail!("invalid account map_state: {state}"),
            None => anyhow::bail!("account record is missing map_state"),
        };
        let radius_requested = invalid_radius || explicit_map_state == "radius" || radius_m > 0;
        if radius_requested && radius_m == 0 && !invalid_radius {
            radius_m = 250;
        }
        let private_location = match (location_lat, location_lon) {
            (Some(lat), Some(lon)) => Some(AccountLocation { lat, lon }),
            _ => None,
        };
        let valid_radius_projection = if radius_requested {
            u32::try_from(radius_m)
                .ok()
                .filter(|radius_m| (MIN_RADIUS_M..=MAX_RADIUS_M).contains(radius_m))
                .and_then(|radius_m| {
                    private_location.as_ref().and_then(|location| {
                        radius_projection_public_pos(
                            v.get(RADIUS_PROJECTION_KEY),
                            location,
                            radius_m,
                        )
                    })
                })
        } else {
            None
        };
        let map_state = if explicit_map_state == "not_on_map" || private_location.is_none() {
            radius_m = 0;
            "not_on_map".to_string()
        } else if radius_requested {
            if valid_radius_projection.is_some() {
                "radius".to_string()
            } else {
                radius_m = 0;
                "not_on_map".to_string()
            }
        } else {
            radius_m = 0;
            "exact".to_string()
        };

        let public_payload = payload_from_keys(&["summary", "tags"], v);

        let mut priv_map = Map::new();
        if let Some(address) = v
            .get("address")
            .and_then(|value| value.as_str())
            .map(str::trim)
            .filter(|value| !value.is_empty())
        {
            priv_map.insert("address".to_string(), Value::String(address.to_string()));
        }
        if let Some(projection) = v.get(RADIUS_PROJECTION_KEY) {
            priv_map.insert(RADIUS_PROJECTION_KEY.to_string(), projection.clone());
        }
        let private_payload = if priv_map.is_empty() {
            "{}".to_string()
        } else {
            serde_json::to_string(&Value::Object(priv_map))
                .context("failed to serialise account private_payload")?
        };

        Ok(Self {
            id,
            kind,
            title,
            map_state,
            radius_m,
            disabled,
            location_lat,
            location_lon,
            role,
            email,
            webauthn_user_id,
            created_at,
            updated_at,
            public_payload,
            private_payload,
        })
    }
}

/// Name of the partial unique index that enforces normalized account-email
/// uniqueness (`lower(btrim(email))` where the trimmed email is non-empty).
/// Kept in sync with the migration
/// `20260613000001_domain_accounts_email_normalized_unique`.
pub const ACCOUNT_EMAIL_UNIQUE_CONSTRAINT: &str = "domain_accounts_email_normalized_unique";

/// Name of the `domain_accounts` primary-key constraint (PostgreSQL default
/// `<table>_pkey`). Kept in sync with the table definition in
/// `20260531000003_create_domain_accounts`.
pub const ACCOUNT_ID_PRIMARY_KEY_CONSTRAINT: &str = "domain_accounts_pkey";

/// Error from the account-create write path.
#[derive(Debug, thiserror::Error)]
pub enum AccountWriteError {
    /// The account `id` (primary key) already exists in `domain_accounts`.
    #[error("account id already exists")]
    DuplicateId,
    /// Another account already persists the same normalized, non-empty email
    /// (`lower(btrim(email))`), rejected by the partial unique index
    /// [`ACCOUNT_EMAIL_UNIQUE_CONSTRAINT`].
    #[error("account email already exists")]
    DuplicateEmail,
    /// The JSONL-shaped record could not be mapped to a row.
    #[error("failed to map account record: {0}")]
    Mapping(#[source] anyhow::Error),
    /// Any other database failure.
    #[error("failed to persist account to domain_accounts: {0}")]
    Database(#[source] sqlx::Error),
}

/// Insert exactly one account row into `domain_accounts` (Phase E-A).
///
/// A plain `INSERT` (no `ON CONFLICT`) is used on purpose: account creation must
/// never silently overwrite an existing account. The database unique constraints
/// are the race-safety boundary; a violation is classified by constraint name so
/// the route can return a precise `409 CONFLICT` cause: a primary-key collision
/// surfaces as [`AccountWriteError::DuplicateId`], and a normalized-email
/// collision (the partial unique index [`ACCOUNT_EMAIL_UNIQUE_CONSTRAINT`]) as
/// [`AccountWriteError::DuplicateEmail`]. This function performs no in-memory
/// mutation and writes no JSONL.
pub async fn insert_account_from_jsonl_record(
    pool: &PgPool,
    record: &Value,
) -> Result<(), AccountWriteError> {
    let row = NewDomainAccountRow::from_jsonl_record(record).map_err(AccountWriteError::Mapping)?;

    let result = sqlx::query(
        "INSERT INTO domain_accounts \
            (id, kind, title, map_state, radius_m, disabled, \
             location_lat, location_lon, \
             role, email, webauthn_user_id, \
             created_at, updated_at, \
             public_payload, private_payload) \
         VALUES \
            ($1, $2, $3, $4, $5, $6, \
             $7, $8, \
             $9, $10, $11::uuid, \
             $12, $13, \
             $14::jsonb, $15::jsonb)",
    )
    .bind(&row.id)
    .bind(&row.kind)
    .bind(&row.title)
    .bind(&row.map_state)
    .bind(row.radius_m)
    .bind(row.disabled)
    .bind(row.location_lat)
    .bind(row.location_lon)
    .bind(&row.role)
    .bind(row.email.as_deref())
    .bind(row.webauthn_user_id.as_deref())
    .bind(row.created_at)
    .bind(row.updated_at)
    .bind(&row.public_payload)
    .bind(&row.private_payload)
    .execute(pool)
    .await;

    match result {
        Ok(_) => Ok(()),
        Err(sqlx::Error::Database(db_err)) if db_err.is_unique_violation() => {
            // Classify ONLY the two constraints this code owns; both map to a
            // precise 409 cause with distinct error bodies. Any other unique
            // violation (e.g. a future constraint) stays a generic database
            // error instead of being mislabelled as a duplicate id. Each
            // `constraint()` borrow ends at the comparison, so `db_err` can be
            // moved back into the generic error in the final branch.
            if db_err.constraint() == Some(ACCOUNT_EMAIL_UNIQUE_CONSTRAINT) {
                Err(AccountWriteError::DuplicateEmail)
            } else if db_err.constraint() == Some(ACCOUNT_ID_PRIMARY_KEY_CONSTRAINT) {
                Err(AccountWriteError::DuplicateId)
            } else {
                Err(AccountWriteError::Database(sqlx::Error::Database(db_err)))
            }
        }
        Err(e) => Err(AccountWriteError::Database(e)),
    }
}

/// Name of the check constraint that rejects after-trim empty persisted emails.

#[derive(Debug, Clone, PartialEq)]
pub struct AccountProfileUpdate {
    pub title: String,
    pub summary: Option<String>,
    pub tags: Vec<String>,
    pub address: Option<String>,
    pub clear_address: bool,
    pub map_state: String,
    pub radius_m: i64,
    pub location: Option<AccountLocation>,
    pub clear_location: bool,
}

#[derive(Debug, Clone)]
pub struct StoredAccountProfile {
    pub account: AccountInternal,
    pub address: Option<String>,
    pub location: Option<AccountLocation>,
}

#[derive(Debug, thiserror::Error)]
pub enum AccountProfileUpdateError {
    #[error("account not found")]
    NotFound,
    #[error("a map location is required for this visibility")]
    MissingLocation,
    #[error("failed to serialise account profile: {0}")]
    Serialization(#[source] serde_json::Error),
    #[error("failed to map updated account")]
    Mapping,
    #[error("failed to update account profile in domain_accounts: {0}")]
    Database(#[source] sqlx::Error),
}

pub async fn load_account_profile_from_postgres(
    pool: &PgPool,
    account_id: &str,
) -> Result<Option<(Option<String>, Option<AccountLocation>)>, sqlx::Error> {
    let row: Option<(Option<String>, Option<f64>, Option<f64>)> = sqlx::query_as(
        "SELECT private_payload->>'address', location_lat, location_lon \
         FROM domain_accounts WHERE id = $1",
    )
    .bind(account_id)
    .fetch_optional(pool)
    .await?;

    Ok(row.map(|(address, lat, lon)| {
        let location = match (lat, lon) {
            (Some(lat), Some(lon)) => Some(AccountLocation { lat, lon }),
            _ => None,
        };
        (address, location)
    }))
}

pub async fn update_account_profile_in_postgres(
    pool: &PgPool,
    account_id: &str,
    update: &AccountProfileUpdate,
) -> Result<StoredAccountProfile, AccountProfileUpdateError> {
    let mut tx = pool
        .begin()
        .await
        .map_err(AccountProfileUpdateError::Database)?;
    let existing: Option<(Option<f64>, Option<f64>, String)> = sqlx::query_as(
        "SELECT location_lat, location_lon, private_payload::text \
         FROM domain_accounts WHERE id = $1 FOR UPDATE",
    )
    .bind(account_id)
    .fetch_optional(&mut *tx)
    .await
    .map_err(AccountProfileUpdateError::Database)?;
    let Some((existing_lat, existing_lon, existing_private_text)) = existing else {
        return Err(AccountProfileUpdateError::NotFound);
    };
    let existing_private_payload = parse_payload(&existing_private_text);
    let existing_address = existing_private_payload
        .get("address")
        .and_then(Value::as_str)
        .map(str::to_string);
    let effective_address = if update.clear_address {
        None
    } else {
        update.address.clone().or(existing_address)
    };
    let existing_location = match (existing_lat, existing_lon) {
        (Some(lat), Some(lon)) => Some(AccountLocation { lat, lon }),
        _ => None,
    };
    let effective_location = if update.clear_location {
        None
    } else {
        update.location.clone().or(existing_location)
    };
    if update.map_state != "not_on_map" && effective_location.is_none() {
        return Err(AccountProfileUpdateError::MissingLocation);
    }

    let mut public_map = Map::new();
    if let Some(summary) = &update.summary {
        public_map.insert("summary".to_string(), Value::String(summary.clone()));
    }
    if !update.tags.is_empty() {
        public_map.insert("tags".to_string(), json!(update.tags));
    }
    let public_payload = serde_json::to_string(&Value::Object(public_map))
        .map_err(AccountProfileUpdateError::Serialization)?;

    let mut private_map = Map::new();
    if let Some(address) = &effective_address {
        private_map.insert("address".to_string(), Value::String(address.clone()));
    }
    if let (Some(existing_projection), Some(location)) = (
        existing_private_payload.get(RADIUS_PROJECTION_KEY),
        effective_location.as_ref(),
    ) {
        // Preserve the old binding while hidden only when it still belongs to
        // the current private location. A private move discards the stale anchor.
        if radius_projection_matches_location(Some(existing_projection), location) {
            private_map.insert(
                RADIUS_PROJECTION_KEY.to_string(),
                existing_projection.clone(),
            );
        }
    }
    if update.map_state == "radius" {
        let location = effective_location
            .as_ref()
            .ok_or(AccountProfileUpdateError::MissingLocation)?;
        let radius_m =
            u32::try_from(update.radius_m).map_err(|_| AccountProfileUpdateError::Mapping)?;
        let projection = ensure_radius_projection_value(
            existing_private_payload.get(RADIUS_PROJECTION_KEY),
            location,
            radius_m,
        );
        private_map.insert(RADIUS_PROJECTION_KEY.to_string(), projection);
    }
    let private_payload = serde_json::to_string(&Value::Object(private_map))
        .map_err(AccountProfileUpdateError::Serialization)?;

    let (new_lat, new_lon) = match &effective_location {
        Some(location) => (Some(location.lat), Some(location.lon)),
        None => (None, None),
    };

    let row: AccountRow = sqlx::query_as(
        "UPDATE domain_accounts SET \
           title = $2, \
           map_state = $3, \
           radius_m = $4, \
           location_lat = $5, \
           location_lon = $6, \
           public_payload = (public_payload - 'summary' - 'tags') || $7::jsonb, \
           private_payload = (private_payload - 'address' - 'radius_projection') || $8::jsonb, \
           updated_at = now() \
         WHERE id = $1 \
         RETURNING id, kind, title, map_state, radius_m, disabled, \
           location_lat, location_lon, role, email, webauthn_user_id::text, \
           public_payload::text, private_payload::text",
    )
    .bind(account_id)
    .bind(&update.title)
    .bind(&update.map_state)
    .bind(update.radius_m)
    .bind(new_lat)
    .bind(new_lon)
    .bind(&public_payload)
    .bind(&private_payload)
    .fetch_one(&mut *tx)
    .await
    .map_err(AccountProfileUpdateError::Database)?;

    tx.commit()
        .await
        .map_err(AccountProfileUpdateError::Database)?;
    let account = account_row_to_internal(row).ok_or(AccountProfileUpdateError::Mapping)?;
    Ok(StoredAccountProfile {
        account,
        address: effective_address,
        location: effective_location,
    })
}

pub const ACCOUNT_EMAIL_NOT_EMPTY_CONSTRAINT: &str = "domain_accounts_email_not_empty_after_trim";

/// Error from the account email update write path.
#[derive(Debug, thiserror::Error)]
pub enum AccountEmailUpdateError {
    /// The account id does not exist in `domain_accounts`.
    #[error("account not found")]
    NotFound,
    /// Another account already persists the same normalized, non-empty email.
    #[error("account email already exists")]
    DuplicateEmail,
    /// The supplied email cannot be persisted under the DB email constraints.
    #[error("invalid account email")]
    InvalidEmail,
    /// Any other database failure.
    #[error("failed to update account email in domain_accounts: {0}")]
    Database(#[source] sqlx::Error),
}

/// Update one account email in `domain_accounts` (AUTH-PG-001).
///
/// This helper performs no in-memory mutation and writes no JSONL. Callers must
/// update the `AccountStore` only after this function returns `Ok(())`, so the
/// PostgreSQL path remains DB-before-cache and restart-stable.
pub async fn update_account_email_in_postgres(
    pool: &PgPool,
    account_id: &str,
    new_email: &str,
) -> Result<DateTime<Utc>, AccountEmailUpdateError> {
    let new_email = new_email.trim().to_ascii_lowercase();
    if new_email.is_empty() || new_email.len() > MAX_EMAIL_LEN {
        return Err(AccountEmailUpdateError::InvalidEmail);
    }

    let result = sqlx::query_scalar::<_, DateTime<Utc>>(
        "UPDATE domain_accounts \
         SET email = $2, updated_at = now() \
         WHERE id = $1 \
         RETURNING updated_at",
    )
    .bind(account_id)
    .bind(&new_email)
    .fetch_optional(pool)
    .await;

    match result {
        Ok(Some(updated_at)) => Ok(updated_at),
        Ok(None) => Err(AccountEmailUpdateError::NotFound),
        Err(sqlx::Error::Database(db_err)) => {
            if db_err.constraint() == Some(ACCOUNT_EMAIL_UNIQUE_CONSTRAINT) {
                Err(AccountEmailUpdateError::DuplicateEmail)
            } else if db_err.constraint() == Some(ACCOUNT_EMAIL_NOT_EMPTY_CONSTRAINT) {
                Err(AccountEmailUpdateError::InvalidEmail)
            } else {
                Err(AccountEmailUpdateError::Database(sqlx::Error::Database(
                    db_err,
                )))
            }
        }
        Err(e) => Err(AccountEmailUpdateError::Database(e)),
    }
}

// ── OPT-ARC-001 Phase E-C: edge-create write path ───────────────────────────
//
// Narrow PostgreSQL helper for internal derived-Faden projection. It maps the same
// validated `Edge` value that `create_edge` puts into the cache and the
// response (built via the canonical `build_edge_record` semantics), so the
// PostgreSQL branch accepts exactly the same create semantics as JSONL. The
// payload keys mirror `load_edges_from_postgres` (source_type, target_type,
// note) — no new key names. It does NOT touch in-memory caches and does NOT
// write JSONL — the caller owns cache updates.
//
// This helper owns edge insertion only; account and node writes use their
// dedicated helpers.

/// A single row destined for `domain_edges`, built from the validated `Edge`
/// the create route already uses for cache and response. `payload` is carried
/// as text and bound with an explicit `::jsonb` cast, exactly as the account
/// write path binds its payloads.
#[derive(Debug, Clone, PartialEq)]
pub struct NewDomainEdgeRow {
    pub id: String,
    pub source_id: String,
    pub target_id: String,
    pub edge_kind: String,
    /// Server-owned create timestamp. Required for new creates: the column is
    /// nullable for legacy/import contexts, but a create must never drop it.
    pub created_at: DateTime<Utc>,
    pub payload: String,
}

impl NewDomainEdgeRow {
    /// Map a validated `Edge` (cache/response value of `create_edge`) to a
    /// `domain_edges` row.
    ///
    /// - `created_at` must be present and RFC3339-parseable; otherwise mapping
    ///   fails and the route returns 500 without any DB or cache mutation.
    /// - `payload` carries `source_type` / `target_type` (when present),
    ///   `note` only when `Some`, and the server-owned `expires_at` when present —
    ///   matching what `load_edges_from_postgres` reads back.
    /// - client `expires_at`, `payload`, and `metadata` create fields do not exist
    ///   in `CreateEdgeRequest`; `deny_unknown_fields` rejects them.
    pub fn from_edge(edge: &crate::routes::edges::Edge) -> Result<Self> {
        let created_at_timestamp = edge
            .created_at
            .as_ref()
            .context("edge record is missing created_at")?;
        let created_at = created_at_timestamp.parsed().cloned().with_context(|| {
            format!(
                "edge created_at is not RFC3339: {}",
                created_at_timestamp.as_str()
            )
        })?;

        let mut payload_map = Map::new();
        if let Some(source_type) = &edge.source_type {
            payload_map.insert(
                "source_type".to_string(),
                Value::String(source_type.clone()),
            );
        }
        if let Some(target_type) = &edge.target_type {
            payload_map.insert(
                "target_type".to_string(),
                Value::String(target_type.clone()),
            );
        }
        if let Some(faden_type) = edge.faden_type {
            payload_map.insert("faden_type".to_string(), json!(faden_type));
        }
        if let Some(faden_subject_id) = &edge.faden_subject_id {
            payload_map.insert(
                "faden_subject_id".to_string(),
                Value::String(faden_subject_id.clone()),
            );
        }
        if let Some(note) = &edge.note {
            payload_map.insert("note".to_string(), Value::String(note.clone()));
        }
        if let Some(Some(expires_at)) = &edge.expires_at {
            payload_map.insert(
                "expires_at".to_string(),
                Value::String(expires_at.as_str().to_owned()),
            );
        }
        let payload = serde_json::to_string(&Value::Object(payload_map))
            .context("failed to serialise edge payload")?;

        Ok(Self {
            id: edge.id.clone(),
            source_id: edge.source_id.clone(),
            target_id: edge.target_id.clone(),
            edge_kind: edge.edge_kind.clone(),
            created_at,
            payload,
        })
    }
}

/// Error from the edge-create write path.
#[derive(Debug, thiserror::Error)]
pub enum EdgeWriteError {
    /// The edge `id` (primary key) already exists in `domain_edges`.
    #[error("edge id already exists")]
    DuplicateId,
    /// The edge cache limit has been reached, preventing insert.
    #[error("edge cache limit reached")]
    CacheLimitReached,
    /// The validated edge could not be mapped to a row.
    #[error("failed to map edge record: {0}")]
    Mapping(#[source] anyhow::Error),
    /// A reactivation operation no longer has a durable row to refresh.
    #[error("edge operation result not found")]
    OperationNotFound,
    /// A stable operation key points at a semantically different edge.
    #[error("edge operation id was already used for different data")]
    OperationConflict,
    /// Any other database failure.
    #[error("failed to persist edge to domain_edges: {0}")]
    Database(#[source] sqlx::Error),
}

/// Insert exactly one edge row into `domain_edges` (Phase E-C).
///
/// The PostgreSQL create path is serialized in a transaction:
/// table-level lock, duplicate precheck, cache-limit count check, then final
/// INSERT. Duplicate ids are checked before the cache-limit condition so the
/// client receives the more precise 409 cause. A unique violation can still
/// surface as a defensive fallback.
///
/// This function performs no in-memory mutation and writes no JSONL.
async fn insert_domain_edge_in_transaction(
    tx: &mut Transaction<'_, Postgres>,
    edge: &crate::routes::edges::Edge,
    operation: Option<&CreateOperationKey>,
) -> Result<CreateWriteOutcome<Edge>, EdgeWriteError> {
    let row = NewDomainEdgeRow::from_edge(edge).map_err(EdgeWriteError::Mapping)?;

    // One transaction owns operation replay, duplicate detection, capacity and
    // insertion. This helper deliberately does not commit so node creation can
    // include its mandatory origin Faden in the same transaction.
    sqlx::query("LOCK TABLE domain_edges IN EXCLUSIVE MODE")
        .execute(&mut **tx)
        .await
        .map_err(EdgeWriteError::Database)?;

    if let Some(operation) = operation {
        let existing: Option<EdgeRow> = sqlx::query_as(
            "SELECT id, source_id, target_id, edge_kind, created_at, payload::text \
             FROM domain_edges \
             WHERE create_actor_id = $1 AND create_operation_id = $2",
        )
        .bind(&operation.actor_id)
        .bind(&operation.operation_id)
        .fetch_optional(&mut **tx)
        .await
        .map_err(EdgeWriteError::Database)?;
        if let Some(row) = existing {
            let existing = edge_from_row(row).map_err(EdgeWriteError::Mapping)?;
            return Ok(CreateWriteOutcome::Existing(existing));
        }
    }

    let (exists,): (bool,) =
        sqlx::query_as("SELECT EXISTS (SELECT 1 FROM domain_edges WHERE id = $1)")
            .bind(&row.id)
            .fetch_one(&mut **tx)
            .await
            .map_err(EdgeWriteError::Database)?;
    if exists {
        return Err(EdgeWriteError::DuplicateId);
    }

    let max_edges = crate::routes::edges::max_edges_cache_limit();
    let max_edges_i64 = i64::try_from(max_edges).unwrap_or(i64::MAX);
    // Mirrors `edge_is_permanently_unreachable` for every case that is safe
    // to check without parsing the payload's `expires_at` string as a
    // timestamp (which `jsonb_typeof` never needs to do, so this can never
    // throw regardless of what a corrupted row contains):
    //   - a present `expires_at` that is neither `null` nor a string is
    //     malformed and never becomes a valid `Edge` at all;
    //   - a dated `created_at` paired with an explicit `expires_at: null`;
    //   - an absent `created_at` paired with a concrete `expires_at` string.
    // `payload ? 'expires_at'` is a definite boolean (never SQL NULL), so a
    // missing key short-circuits every branch to a definite `false` instead
    // of the `->` operator's SQL NULL silently propagating through equality
    // checks (as an earlier version of this condition did).
    //
    // The whole exclusion is additionally guarded on the payload actually
    // being a JSON object. `?` matches array *elements*, not just object
    // keys, so a non-object payload such as `["expires_at"]` would otherwise
    // make `payload ? 'expires_at'` true while `payload -> 'expires_at'`
    // returns SQL NULL — `jsonb_typeof(NULL)` is itself NULL, propagating
    // through the same three-valued-logic trap the `?` operator was meant to
    // avoid. `payload_string`/`payload_lifecycle_field` only ever look up
    // keys via `Value::get`, which returns `None` for any non-object value
    // regardless of its contents, so the loader treats a non-object payload
    // exactly like `{}` (a reachable, omitted-expiry edge) — the capacity
    // count must agree instead of quietly excluding it.
    //
    // Deliberately not covered: a dated `created_at` with a present string
    // `expires_at` that is unparseable or not exactly the canonical 168-hour
    // duration. Validating that would require casting the payload string to
    // a timestamp, which raises a hard error for any non-timestamp string
    // anywhere in the table instead of evaluating to a boolean — turning a
    // conservative over-count into a query failure that blocks every create.
    // Closing that gap needs a safe-cast SQL helper (a schema migration),
    // not a WHERE clause; the loader already fails such rows closed at read
    // time, so at worst a persisted row that could never occur through this
    // API's own write path (which always emits a canonical duration) counts
    // toward capacity a little too eagerly.
    let (limit_reached,): (bool,) = sqlx::query_as(
        "SELECT COUNT(*) >= $1 FROM domain_edges \
         WHERE NOT (\
             jsonb_typeof(payload) = 'object' \
             AND payload ? 'expires_at' \
             AND (\
                 jsonb_typeof(payload -> 'expires_at') NOT IN ('null', 'string') \
                 OR (created_at IS NOT NULL AND jsonb_typeof(payload -> 'expires_at') = 'null') \
                 OR (created_at IS NULL AND jsonb_typeof(payload -> 'expires_at') = 'string')\
             )\
         )",
    )
    .bind(max_edges_i64)
    .fetch_one(&mut **tx)
    .await
    .map_err(EdgeWriteError::Database)?;
    if limit_reached {
        return Err(EdgeWriteError::CacheLimitReached);
    }

    let result = sqlx::query(
        "INSERT INTO domain_edges \
            (id, source_id, target_id, edge_kind, created_at, payload, \
             create_actor_id, create_operation_id) \
         VALUES \
            ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)",
    )
    .bind(&row.id)
    .bind(&row.source_id)
    .bind(&row.target_id)
    .bind(&row.edge_kind)
    .bind(row.created_at)
    .bind(&row.payload)
    .bind(operation.map(|value| value.actor_id.as_str()))
    .bind(operation.map(|value| value.operation_id.as_str()))
    .execute(&mut **tx)
    .await;

    match result {
        Ok(_) => Ok(CreateWriteOutcome::Created),
        Err(sqlx::Error::Database(db_err)) if db_err.is_unique_violation() => {
            Err(EdgeWriteError::DuplicateId)
        }
        Err(error) => Err(EdgeWriteError::Database(error)),
    }
}

pub async fn insert_domain_edge(
    pool: &PgPool,
    edge: &crate::routes::edges::Edge,
    operation: Option<&CreateOperationKey>,
) -> Result<CreateWriteOutcome<Edge>, EdgeWriteError> {
    let mut tx = pool.begin().await.map_err(EdgeWriteError::Database)?;
    let outcome = insert_domain_edge_in_transaction(&mut tx, edge, operation).await;
    match outcome {
        Ok(outcome) => {
            tx.commit().await.map_err(EdgeWriteError::Database)?;
            Ok(outcome)
        }
        Err(error) => {
            tx.rollback().await.ok();
            Err(error)
        }
    }
}

/// Refresh one existing operation-bound Faden without changing its identity.
///
/// A row lock makes lookup, semantic validation and lifecycle replacement one
/// atomic database action without blocking unrelated Fäden. The durable domain
/// action remains the historical source of truth; this row is only its current
/// map projection.
pub async fn reactivate_domain_edge(
    pool: &PgPool,
    expected: &Edge,
    operation: &CreateOperationKey,
) -> Result<Edge, EdgeWriteError> {
    let row = NewDomainEdgeRow::from_edge(expected).map_err(EdgeWriteError::Mapping)?;
    let mut tx = pool.begin().await.map_err(EdgeWriteError::Database)?;

    let existing: Option<EdgeRow> = sqlx::query_as(
        "SELECT id, source_id, target_id, edge_kind, created_at, payload::text \
         FROM domain_edges \
         WHERE create_actor_id = $1 AND create_operation_id = $2 \
         FOR UPDATE",
    )
    .bind(&operation.actor_id)
    .bind(&operation.operation_id)
    .fetch_optional(&mut *tx)
    .await
    .map_err(EdgeWriteError::Database)?;
    let existing = existing.ok_or(EdgeWriteError::OperationNotFound)?;
    let existing = edge_from_row(existing).map_err(EdgeWriteError::Mapping)?;
    if existing.id != expected.id || !edge_matches_projection(&existing, expected) {
        return Err(EdgeWriteError::OperationConflict);
    }

    let update = sqlx::query(
        "UPDATE domain_edges SET created_at = $1, payload = $2::jsonb \
         WHERE id = $3 AND create_actor_id = $4 AND create_operation_id = $5",
    )
    .bind(row.created_at)
    .bind(&row.payload)
    .bind(&row.id)
    .bind(&operation.actor_id)
    .bind(&operation.operation_id)
    .execute(&mut *tx)
    .await
    .map_err(EdgeWriteError::Database)?;
    if update.rows_affected() != 1 {
        return Err(EdgeWriteError::OperationNotFound);
    }

    tx.commit().await.map_err(EdgeWriteError::Database)?;
    Ok(expected.clone())
}

#[derive(Debug, Clone)]
pub struct NodeFadenCreateOutcome {
    pub node: Node,
    pub edge: Edge,
    pub created: bool,
}

#[derive(Debug, thiserror::Error)]
pub enum NodeFadenCreateError {
    #[error(transparent)]
    Node(#[from] NodeCreateError),
    #[error(transparent)]
    Edge(#[from] EdgeWriteError),
    #[error("node operation id was already used for different data")]
    OperationConflict,
    #[error("edge operation id was already used for different data")]
    EdgeOperationConflict,
    #[error("replayed node was deleted before its origin Faden could be repaired")]
    ReplayedNodeDeleted,
}

fn edge_matches_projection(existing: &Edge, expected: &Edge) -> bool {
    existing.source_id == expected.source_id
        && existing.source_type == expected.source_type
        && existing.target_id == expected.target_id
        && existing.target_type == expected.target_type
        && existing.edge_kind == expected.edge_kind
        && ((existing.faden_type.is_none() && existing.faden_subject_id.is_none())
            || (existing.faden_type == expected.faden_type
                && existing.faden_subject_id == expected.faden_subject_id))
        && existing.note == expected.note
}

/// Atomically persist a PostgreSQL node and its mandatory account→node origin
/// Faden. Operation replays repair an older missing Faden in the same guarded
/// transaction after validating the original node semantics.
pub async fn insert_domain_node_and_faden_with_creator_limit<F, G, H>(
    pool: &PgPool,
    node: &Node,
    operation: &CreateOperationKey,
    creator_node_limit: Option<usize>,
    existing_matches: F,
    edge_for_node: G,
    edge_operation_for_node: H,
) -> Result<NodeFadenCreateOutcome, NodeFadenCreateError>
where
    F: Fn(&Node) -> bool,
    G: Fn(&str) -> Edge,
    H: Fn(&str) -> CreateOperationKey,
{
    let mut tx = pool
        .begin()
        .await
        .map_err(NodeCreateError::Database)
        .map_err(NodeFadenCreateError::Node)?;

    let result: Result<NodeFadenCreateOutcome, NodeFadenCreateError> = async {
        if let Some(account_id) = node.created_by_account_id.as_deref() {
            sqlx::query("SELECT pg_advisory_xact_lock($1::bigint)")
                .bind(crate::advisory_lock::account_lifecycle_lock_key(account_id))
                .execute(&mut *tx)
                .await
                .map_err(NodeCreateError::Database)?;
        }

        let node_outcome =
            insert_domain_node_in_transaction(&mut tx, node, Some(operation), creator_node_limit)
                .await?;
        let (actual_node, created) = match node_outcome {
            CreateWriteOutcome::Created => (node.clone(), true),
            CreateWriteOutcome::Existing(existing) => {
                if !existing_matches(&existing) {
                    return Err(NodeFadenCreateError::OperationConflict);
                }
                (existing, false)
            }
        };

        sqlx::query("SELECT pg_advisory_xact_lock($1::bigint)")
            .bind(crate::advisory_lock::node_mutation_lock_key(
                &actual_node.id,
            ))
            .execute(&mut *tx)
            .await
            .map_err(NodeCreateError::Database)?;
        if !created {
            let node_still_exists: bool =
                sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM domain_nodes WHERE id = $1)")
                    .bind(&actual_node.id)
                    .fetch_one(&mut *tx)
                    .await
                    .map_err(NodeCreateError::Database)?;
            if !node_still_exists {
                return Err(NodeFadenCreateError::ReplayedNodeDeleted);
            }
        }

        let expected_edge = edge_for_node(&actual_node.id);
        let edge_operation = edge_operation_for_node(&actual_node.id);
        let edge_outcome =
            insert_domain_edge_in_transaction(&mut tx, &expected_edge, Some(&edge_operation))
                .await?;
        let actual_edge = match edge_outcome {
            CreateWriteOutcome::Created => expected_edge,
            CreateWriteOutcome::Existing(existing) => {
                if !edge_matches_projection(&existing, &expected_edge) {
                    return Err(NodeFadenCreateError::EdgeOperationConflict);
                }
                existing
            }
        };

        Ok(NodeFadenCreateOutcome {
            node: actual_node,
            edge: actual_edge,
            created,
        })
    }
    .await;

    match result {
        Ok(outcome) => {
            tx.commit()
                .await
                .map_err(NodeCreateError::Database)
                .map_err(NodeFadenCreateError::Node)?;
            Ok(outcome)
        }
        Err(error) => {
            tx.rollback().await.ok();
            Err(error)
        }
    }
}

/// Holds the same cross-instance node-mutation lock used by edit and delete
/// routes while the create handler reconciles and publishes its cache state.
/// The snapshots are read only after the lock is acquired, so a deletion or
/// guest exit that wins the post-commit gap is observed instead of overwritten
/// by the stale create response.
pub struct NodeFadenCachePublicationGuard {
    transaction: Transaction<'static, Postgres>,
    pub node: Option<Node>,
    pub edge: Option<Edge>,
}

impl NodeFadenCachePublicationGuard {
    pub async fn release(self) -> Result<(), sqlx::Error> {
        self.transaction.commit().await
    }
}

pub async fn lock_node_faden_cache_publication(
    pool: &PgPool,
    node_id: &str,
    expected_edge: &Edge,
) -> Result<NodeFadenCachePublicationGuard, NodeFadenCreateError> {
    let mut transaction = pool
        .begin()
        .await
        .map_err(NodeCreateError::Database)
        .map_err(NodeFadenCreateError::Node)?;

    sqlx::query("SELECT pg_advisory_xact_lock($1::bigint)")
        .bind(crate::advisory_lock::node_mutation_lock_key(node_id))
        .execute(&mut *transaction)
        .await
        .map_err(NodeCreateError::Database)?;

    let node = sqlx::query_as::<_, NodeRow>(
        "SELECT id, kind, title, lat, lon, created_at, updated_at, payload::text, search_visibility \
         FROM domain_nodes WHERE id = $1",
    )
    .bind(node_id)
    .fetch_optional(&mut *transaction)
    .await
    .map_err(NodeCreateError::Database)?
    .map(node_from_row)
    .transpose()
    .map_err(NodeCreateError::Mapping)?;

    let edge = if node.is_some() {
        let edge = sqlx::query_as::<_, EdgeRow>(
            "SELECT id, source_id, target_id, edge_kind, created_at, payload::text \
             FROM domain_edges WHERE id = $1",
        )
        .bind(&expected_edge.id)
        .fetch_optional(&mut *transaction)
        .await
        .map_err(EdgeWriteError::Database)?
        .map(edge_from_row)
        .transpose()
        .map_err(EdgeWriteError::Mapping)?;
        if let Some(existing) = edge.as_ref() {
            if !edge_matches_projection(existing, expected_edge) {
                return Err(NodeFadenCreateError::EdgeOperationConflict);
            }
        }
        edge
    } else {
        None
    };

    Ok(NodeFadenCachePublicationGuard {
        transaction,
        node,
        edge,
    })
}

#[cfg(test)]
mod faden_metadata_payload_tests {
    use super::*;

    #[test]
    fn accepts_absent_or_complete_typed_faden_metadata() {
        assert_eq!(payload_faden_metadata(&json!({})), Ok((None, None)));
        assert_eq!(
            payload_faden_metadata(&json!({
                "faden_type": "conversation",
                "faden_subject_id": "11111111-1111-5111-8111-111111111111"
            })),
            Ok((
                Some(FadenType::Conversation),
                Some("11111111-1111-5111-8111-111111111111".to_string())
            ))
        );
    }

    #[test]
    fn rejects_unknown_half_or_malformed_typed_faden_metadata() {
        assert!(payload_faden_metadata(&json!({"faden_type": "vote"})).is_err());
        assert!(payload_faden_metadata(&json!({
            "faden_type": "unknown",
            "faden_subject_id": "11111111-1111-5111-8111-111111111111"
        }))
        .is_err());
        assert!(payload_faden_metadata(&json!({
            "faden_type": "vote",
            "faden_subject_id": "not-a-uuid"
        }))
        .is_err());
    }
}

#[cfg(test)]
mod edge_write_path_tests {
    use super::*;
    use crate::routes::edges::Edge;

    fn create_edge_value() -> Edge {
        Edge {
            id: "00000000-0000-0000-0000-0000000000e1".to_string(),
            source_id: "00000000-0000-0000-0000-0000000000a1".to_string(),
            source_type: Some("node".to_string()),
            target_id: "00000000-0000-0000-0000-0000000000b1".to_string(),
            target_type: Some("account".to_string()),
            edge_kind: "reference".to_string(),
            faden_type: None,
            faden_subject_id: None,
            note: None,
            created_at: Some("2026-06-12T10:00:00+00:00".into()),
            expires_at: Some(Some("2026-06-19T10:00:00+00:00".into())),
        }
    }

    #[test]
    fn maps_create_edge_with_loader_compatible_payload_keys() {
        let mut edge = create_edge_value();
        edge.note = Some("a note".to_string());
        let row = NewDomainEdgeRow::from_edge(&edge).expect("map");
        assert_eq!(row.id, edge.id);
        assert_eq!(row.source_id, edge.source_id);
        assert_eq!(row.target_id, edge.target_id);
        assert_eq!(row.edge_kind, "reference");
        assert_eq!(row.created_at.to_rfc3339(), "2026-06-12T10:00:00+00:00");
        // Payload keys must match what load_edges_from_postgres reads back.
        let payload: Value = serde_json::from_str(&row.payload).expect("payload json");
        assert_eq!(
            payload.get("source_type").and_then(|v| v.as_str()),
            Some("node")
        );
        assert_eq!(
            payload.get("target_type").and_then(|v| v.as_str()),
            Some("account")
        );
        assert_eq!(payload.get("note").and_then(|v| v.as_str()), Some("a note"));
        assert_eq!(
            payload.get("expires_at").and_then(|v| v.as_str()),
            Some("2026-06-19T10:00:00+00:00")
        );
    }

    #[test]
    fn reactivation_matching_ignores_lifecycle_but_rejects_semantic_drift() {
        let mut existing = create_edge_value();
        existing.faden_type = Some(FadenType::Conversation);
        existing.faden_subject_id = Some("11111111-1111-5111-8111-111111111111".to_string());
        let mut refreshed = existing.clone();
        refreshed.created_at = Some("2026-06-15T12:00:00+00:00".into());
        refreshed.expires_at = Some(Some("2026-06-22T12:00:00+00:00".into()));

        assert!(edge_matches_projection(&existing, &refreshed));

        let mut changed_target = refreshed.clone();
        changed_target.target_id = "00000000-0000-0000-0000-0000000000c1".to_string();
        assert!(!edge_matches_projection(&existing, &changed_target));

        let mut changed_type = refreshed.clone();
        changed_type.faden_type = Some(FadenType::Vote);
        assert!(!edge_matches_projection(&existing, &changed_type));
    }

    #[test]
    fn reactivation_can_upgrade_legacy_untyped_projection_without_new_identity() {
        let legacy = create_edge_value();
        let mut typed = legacy.clone();
        typed.faden_type = Some(FadenType::Knotting);
        typed.faden_subject_id = Some(typed.target_id.clone());
        typed.created_at = Some("2026-06-15T12:00:00+00:00".into());
        typed.expires_at = Some(Some("2026-06-22T12:00:00+00:00".into()));

        assert!(edge_matches_projection(&legacy, &typed));
        assert_eq!(legacy.id, typed.id);
    }

    #[test]
    fn omits_note_key_when_note_is_absent() {
        let row = NewDomainEdgeRow::from_edge(&create_edge_value()).expect("map");
        let payload: Value = serde_json::from_str(&row.payload).expect("payload json");
        assert!(
            payload.get("note").is_none(),
            "absent note must be omitted, never null"
        );
    }

    #[test]
    fn rejects_missing_created_at() {
        let mut edge = create_edge_value();
        edge.created_at = None;
        let err = NewDomainEdgeRow::from_edge(&edge).unwrap_err();
        assert!(err.to_string().contains("missing created_at"));
    }

    #[test]
    fn rejects_unparseable_created_at() {
        let mut edge = create_edge_value();
        edge.created_at = Some("yesterday".into());
        let err = NewDomainEdgeRow::from_edge(&edge).unwrap_err();
        assert!(err.to_string().contains("not RFC3339"));
    }

    #[test]
    fn payload_lifecycle_field_distinguishes_omitted_null_and_value() {
        let omitted = serde_json::json!({});
        assert_eq!(payload_lifecycle_field(&omitted, "expires_at"), Ok(None));

        let explicit_null = serde_json::json!({ "expires_at": null });
        assert_eq!(
            payload_lifecycle_field(&explicit_null, "expires_at"),
            Ok(Some(None))
        );

        let dated = serde_json::json!({ "expires_at": "2026-06-19T10:00:00+00:00" });
        assert_eq!(
            payload_lifecycle_field(&dated, "expires_at"),
            Ok(Some(Some("2026-06-19T10:00:00+00:00".into())))
        );
    }

    /// A structurally malformed `expires_at` (neither absent, `null`, nor a
    /// string) must not be silently coerced into the explicit-null state.
    /// Doing so would let it pass as the legitimate fully-undated legacy
    /// pairing whenever `created_at` is also absent, defeating fail-closed
    /// handling of corrupted payload data.
    #[test]
    fn payload_lifecycle_field_rejects_structurally_malformed_value() {
        for malformed in [
            serde_json::json!({ "expires_at": 12345 }),
            serde_json::json!({ "expires_at": true }),
            serde_json::json!({ "expires_at": {} }),
            serde_json::json!({ "expires_at": [] }),
        ] {
            assert_eq!(payload_lifecycle_field(&malformed, "expires_at"), Err(()));
        }
    }

    #[test]
    fn edge_from_row_rejects_row_with_malformed_expires_at_payload() {
        let row: EdgeRow = (
            "edge-malformed".to_string(),
            "source".to_string(),
            "target".to_string(),
            "reference".to_string(),
            None,
            r#"{"expires_at": 12345}"#.to_string(),
        );
        let err = edge_from_row(row).unwrap_err();
        assert!(err.to_string().contains("malformed expires_at"));
    }
}

#[cfg(test)]
mod write_path_tests {
    use super::*;
    use serde_json::json;

    // The tests below exercise the canonical JSONL-to-PostgreSQL mapper.
    // Removed identity fields are rejected rather than normalized.

    /// The create route builds exactly this record shape before persistence.
    fn create_record() -> Value {
        let location = AccountLocation {
            lat: 53.5,
            lon: 10.0,
        };
        let mut record = json!({
            "id": "writepath-unit-1",
            "type": "garnrolle",
            "map_state": "radius",
            "title": "Unit",
            "summary": "A summary",
            "tags": ["x", "y"],
            "role": "weber",
            "location": { "lat": location.lat, "lon": location.lon },
            "radius_m": 250,
            "email": "unit@example.test"
        });
        record[RADIUS_PROJECTION_KEY] = ensure_radius_projection_value(None, &location, 250);
        record
    }

    #[test]
    fn maps_create_record_like_backfill() {
        let row = NewDomainAccountRow::from_jsonl_record(&create_record()).expect("map");
        assert_eq!(row.id, "writepath-unit-1");
        assert_eq!(row.kind, "garnrolle");
        assert_eq!(row.title, "Unit");
        assert_eq!(row.map_state, "radius");
        assert_eq!(row.radius_m, 250);
        assert!(!row.disabled);
        assert_eq!(row.location_lat, Some(53.5));
        assert_eq!(row.location_lon, Some(10.0));
        assert_eq!(row.role, "weber");
        assert_eq!(row.email.as_deref(), Some("unit@example.test"));
        // create records carry no webauthn_user_id / timestamps
        assert_eq!(row.webauthn_user_id, None);
        assert_eq!(row.created_at, None);
        assert_eq!(row.updated_at, None);
        // public_payload carries summary + tags
        let public: Value = serde_json::from_str(&row.public_payload).expect("public json");
        assert_eq!(
            public.get("summary").and_then(|v| v.as_str()),
            Some("A summary")
        );
        assert!(public.get("tags").and_then(|v| v.as_array()).is_some());
        // New writes do not persist the legacy identity mode.
        let private: Value = serde_json::from_str(&row.private_payload).expect("private json");
        assert!(private.get("mode").is_none());
        assert!(private.get("visibility").is_none());
        assert!(private.get("ron_flag").is_none());
        assert!(private.get(RADIUS_PROJECTION_KEY).is_some());
    }

    #[test]
    fn removed_visibility_field_is_rejected() {
        let record = json!({
            "id": "writepath-unit-private",
            "type": "garnrolle",
            "title": "Private",
            "map_state": "not_on_map",
            "visibility": "private"
        });
        let error = NewDomainAccountRow::from_jsonl_record(&record)
            .expect_err("removed visibility field must be rejected");
        assert!(error.to_string().contains("removed account field"));
    }

    #[test]
    fn invalid_explicit_map_state_is_rejected() {
        let record = json!({
            "id": "writepath-unit-invalid-state",
            "type": "garnrolle",
            "title": "Invalid state",
            "map_state": "public-ish",
            "location": { "lat": 53.5, "lon": 10.0 }
        });
        let error = NewDomainAccountRow::from_jsonl_record(&record)
            .expect_err("unknown map_state must not be repaired implicitly");
        assert!(error.to_string().contains("invalid account map_state"));
    }

    #[test]
    fn explicit_not_on_map_keeps_internal_location_private() {
        let record = json!({
            "id": "writepath-unit-hidden-location",
            "type": "garnrolle",
            "title": "Hidden location",
            "map_state": "not_on_map",
            "location": { "lat": 53.5, "lon": 10.0 },
            "radius_m": 500
        });
        let row = NewDomainAccountRow::from_jsonl_record(&record).expect("map");
        assert_eq!(row.map_state, "not_on_map");
        assert_eq!(row.radius_m, 0);
        assert_eq!(row.location_lat, Some(53.5));
        assert_eq!(row.location_lon, Some(10.0));
    }

    #[test]
    fn missing_map_state_is_rejected() {
        let record = json!({
            "id": "writepath-unit-missing-state",
            "type": "garnrolle",
            "title": "Missing state"
        });
        let error = NewDomainAccountRow::from_jsonl_record(&record)
            .expect_err("canonical records require map_state");
        assert!(error.to_string().contains("missing map_state"));
    }

    #[test]
    fn oversized_radius_fails_closed_without_integer_wraparound() {
        let record = json!({
            "id": "writepath-unit-oversized-radius",
            "type": "garnrolle",
            "title": "Oversized radius",
            "map_state": "radius",
            "location": { "lat": 53.5, "lon": 10.0 },
            "radius_m": u64::MAX
        });
        let row = NewDomainAccountRow::from_jsonl_record(&record).expect("map");
        assert_eq!(row.map_state, "not_on_map");
        assert_eq!(row.radius_m, 0);
        assert_eq!(row.location_lat, Some(53.5));
        assert_eq!(row.location_lon, Some(10.0));
    }

    #[test]
    fn payload_from_keys_preserves_selected_non_null_fields() {
        let source = serde_json::json!({
            "summary": "hello",
            "tags": ["a", "b"],
            "private_note": "secret",
            "empty": null
        });

        let payload = payload_from_keys(&["summary", "tags", "empty", "missing"], &source);
        let parsed: serde_json::Value = serde_json::from_str(&payload).unwrap();

        assert_eq!(
            parsed,
            serde_json::json!({
                "summary": "hello",
                "tags": ["a", "b"]
            })
        );
    }

    #[test]
    fn payload_from_keys_returns_empty_object_for_missing_or_null_fields() {
        let source = serde_json::json!({
            "summary": null,
            "private_note": "secret"
        });

        let payload = payload_from_keys(&["summary", "tags"], &source);

        assert_eq!(payload, "{}");
    }

    #[test]
    fn removed_ron_flag_is_rejected() {
        let record = json!({
            "id": "writepath-unit-removed-field",
            "type": "garnrolle",
            "title": "Removed field",
            "map_state": "not_on_map",
            "ron_flag": true
        });
        let error = NewDomainAccountRow::from_jsonl_record(&record)
            .expect_err("removed ron_flag must be rejected");
        assert!(error.to_string().contains("removed account field"));
    }

    #[test]
    fn email_is_trimmed_and_after_trim_empty_becomes_none() {
        let with_email = |email: Value| {
            json!({
                "id": "wp-email",
                "type": "garnrolle",
                "title": "E",
                "map_state": "not_on_map",
                "email": email
            })
        };
        let no_email = json!({
            "id": "wp-email",
            "type": "garnrolle",
            "title": "E",
            "map_state": "not_on_map"
        });

        let map = |v: &Value| {
            NewDomainAccountRow::from_jsonl_record(v)
                .expect("map")
                .email
        };

        assert_eq!(map(&no_email), None, "missing email => None");
        assert_eq!(map(&with_email(Value::Null)), None, "null email => None");
        assert_eq!(map(&with_email(json!(""))), None, "empty email => None");
        assert_eq!(
            map(&with_email(json!("   "))),
            None,
            "after-trim-empty email => None"
        );
        assert_eq!(
            map(&with_email(json!(" alpha@example.invalid "))).as_deref(),
            Some("alpha@example.invalid"),
            "surrounding whitespace is trimmed"
        );
    }
}
