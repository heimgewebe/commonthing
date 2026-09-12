use super::domain_write_guard::{
    reject_edge_create_unless_writable, write_source_read_only_conflict,
};
use super::health::{ensure_jsonl_size, load_policy_limits};
use super::query::{
    cursor_page, parse_cursor_params, parse_usize_param, validate_cursor_limit, ListResponse,
    MAX_PAGE_SIZE,
};
use crate::auth::role::Role;
use crate::config::{DomainEdgeWriteSource, FIXED_FADEN_FADE_DAYS};
use crate::domain_db::{
    insert_domain_edge, reactivate_domain_edge, CreateOperationKey, CreateWriteOutcome,
    EdgeWriteError,
};
use crate::middleware::auth::AuthContext;
use crate::state::{ApiState, OrderedCache};
use crate::utils::edges_path;
use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    Extension, Json,
};
use chrono::{DateTime, Duration, SecondsFormat, Timelike, Utc};
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::OnceLock;
use tokio::sync::Mutex;
use tokio::{
    fs::{File, OpenOptions},
    io::{AsyncBufReadExt, AsyncReadExt, AsyncSeekExt, AsyncWriteExt, BufReader, SeekFrom},
};
use uuid::Uuid;

/// Process-local lock serializing edge-create persistence (duplicate check +
/// JSONL append + cache insert) so concurrent creates cannot interleave the
/// check and the write. Kept module-local instead of on `ApiState` so the
/// edge-create feature does not ripple through every manual `ApiState` literal
/// (notably the DB-proof harness states). The lock is per process, matching the
/// existing JSONL-API process model; cross-process file locking is out of scope.
static EDGE_CREATE_PERSIST: OnceLock<Mutex<()>> = OnceLock::new();

pub(crate) fn edge_create_persist_lock() -> &'static Mutex<()> {
    EDGE_CREATE_PERSIST.get_or_init(|| Mutex::new(()))
}

fn jsonl_write_status(error: &std::io::Error) -> StatusCode {
    if error.kind() == std::io::ErrorKind::FileTooLarge {
        StatusCode::INSUFFICIENT_STORAGE
    } else {
        StatusCode::INTERNAL_SERVER_ERROR
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum FadenProjectionMode {
    /// Create a missing projection, but leave an existing operation replay unchanged.
    EnsureOnly,
    /// Create a missing projection or restart the lifecycle of its existing row.
    Reactivate,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LifecycleTimestamp {
    raw: String,
    parsed: Option<DateTime<Utc>>,
}

impl LifecycleTimestamp {
    fn from_raw(raw: String) -> Self {
        let parsed = DateTime::parse_from_rfc3339(&raw)
            .ok()
            .map(|value| value.with_timezone(&Utc));
        Self { raw, parsed }
    }

    pub fn from_datetime(value: DateTime<Utc>) -> Self {
        let value = value
            .with_nanosecond(value.timestamp_subsec_micros() * 1_000)
            .expect("microsecond precision is always a valid nanosecond value");
        Self {
            raw: value.to_rfc3339_opts(SecondsFormat::Micros, true),
            parsed: Some(value),
        }
    }

    pub fn as_str(&self) -> &str {
        &self.raw
    }
    pub fn parsed(&self) -> Option<&DateTime<Utc>> {
        self.parsed.as_ref()
    }
}

impl From<String> for LifecycleTimestamp {
    fn from(value: String) -> Self {
        Self::from_raw(value)
    }
}
impl From<&str> for LifecycleTimestamp {
    fn from(value: &str) -> Self {
        Self::from_raw(value.to_owned())
    }
}
impl std::ops::Deref for LifecycleTimestamp {
    type Target = str;
    fn deref(&self) -> &Self::Target {
        self.as_str()
    }
}
impl std::fmt::Display for LifecycleTimestamp {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(self.as_str())
    }
}
impl Serialize for LifecycleTimestamp {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        serializer.serialize_str(self.as_str())
    }
}
impl<'de> Deserialize<'de> for LifecycleTimestamp {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        String::deserialize(deserializer).map(Self::from_raw)
    }
}

/// Deserialize a present field (including an explicit JSON `null`) as `Some`,
/// so pairing this with `#[serde(default)]` distinguishes an omitted key
/// (`None`, via the `default`) from an explicit `null` (`Some(None)`). Used for
/// `Edge::expires_at`, where that distinction is load-bearing: an omitted key
/// means "derive the canonical expiry retroactively", while an explicit `null`
/// paired with a dated `created_at` is a non-canonical state that must fail
/// closed instead of being silently treated as omitted.
fn deserialize_some<'de, D, T>(deserializer: D) -> Result<Option<T>, D::Error>
where
    T: Deserialize<'de>,
    D: Deserializer<'de>,
{
    Deserialize::deserialize(deserializer).map(Some)
}

/// Semantic kind of a derived participation Faden. The machine values stay
/// language-neutral; clients render the canonical German names
/// Gesprächsfaden, Antragsfaden, Knüpffaden and Stimmfaden.
#[derive(Debug, Serialize, Deserialize, Clone, Copy, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum FadenType {
    Conversation,
    Proposal,
    Knotting,
    Vote,
}

impl FadenType {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Conversation => "conversation",
            Self::Proposal => "proposal",
            Self::Knotting => "knotting",
            Self::Vote => "vote",
        }
    }
}

/// An optional typed field may be omitted, but explicit JSON null would blur
/// the contract distinction between a legacy projection and malformed data.
fn deserialize_optional_non_null_faden_type<'de, D>(
    deserializer: D,
) -> Result<Option<FadenType>, D::Error>
where
    D: Deserializer<'de>,
{
    FadenType::deserialize(deserializer).map(Some)
}

fn deserialize_optional_non_null_string_value<'de, D>(
    deserializer: D,
) -> Result<Option<String>, D::Error>
where
    D: Deserializer<'de>,
{
    String::deserialize(deserializer).map(Some)
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct Edge {
    pub id: String,
    pub source_id: String,
    pub source_type: Option<String>,
    pub target_id: String,
    pub target_type: Option<String>,
    #[serde(alias = "kind", alias = "edgeKind")]
    pub edge_kind: String,
    /// Absent only on legacy/imported projections that predate typed Fäden.
    #[serde(
        default,
        deserialize_with = "deserialize_optional_non_null_faden_type",
        skip_serializing_if = "Option::is_none"
    )]
    pub faden_type: Option<FadenType>,
    /// Stable semantic target (for example one proposal or conversation), even
    /// when the drawable endpoint is the enclosing Webgemeindezentrum.
    #[serde(
        default,
        deserialize_with = "deserialize_optional_non_null_string_value",
        skip_serializing_if = "Option::is_none"
    )]
    pub faden_subject_id: Option<String>,
    pub note: Option<String>,
    pub created_at: Option<LifecycleTimestamp>,
    /// `None` means the JSONL/PostgreSQL record omitted the key (legacy dated
    /// Fäden predating the lifecycle feature); `Some(None)` means the record
    /// carried an explicit `null`. Only the omitted state derives a retroactive
    /// expiry — see [`edge_is_active_at`].
    #[serde(
        default,
        deserialize_with = "deserialize_some",
        skip_serializing_if = "Option::is_none"
    )]
    pub expires_at: Option<Option<LifecycleTimestamp>>,
}

/// Public edge projection. Free-text notes remain persisted authoring metadata
/// and are intentionally omitted from unauthenticated read surfaces.
#[derive(Debug, Serialize, Clone, PartialEq)]
pub struct PublicEdge {
    pub id: String,
    pub source_id: String,
    pub source_type: Option<String>,
    pub target_id: String,
    pub target_type: Option<String>,
    pub edge_kind: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub faden_type: Option<FadenType>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub faden_subject_id: Option<String>,
    pub created_at: Option<String>,
    pub expires_at: Option<String>,
}

impl From<&Edge> for PublicEdge {
    fn from(edge: &Edge) -> Self {
        Self {
            id: edge.id.clone(),
            source_id: edge.source_id.clone(),
            source_type: edge.source_type.clone(),
            target_id: edge.target_id.clone(),
            target_type: edge.target_type.clone(),
            edge_kind: edge.edge_kind.clone(),
            faden_type: edge.faden_type,
            faden_subject_id: edge.faden_subject_id.clone(),
            created_at: edge
                .created_at
                .as_ref()
                .map(|timestamp| timestamp.as_str().to_owned()),
            expires_at: projected_faden_expires_at(edge),
        }
    }
}

#[derive(Debug, Serialize, Clone)]
pub struct EdgeParticipantDetails {
    pub id: String,
    pub title: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub r#type: Option<String>,
}

#[derive(Debug, Serialize, Clone)]
pub struct EdgeWithDetails {
    #[serde(flatten)]
    pub edge: PublicEdge,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_details: Option<EdgeParticipantDetails>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub target_details: Option<EdgeParticipantDetails>,
}

pub(crate) const DEFAULT_MAX_EDGES_CACHE: usize = 500_000;
/// Canonical lifetime of a newly derived, unverzwirnter Faden.
///
/// The durable Webungsaktion remains the source of truth; only this active
/// projection expires. Legacy records with a valid `created_at` use the same
/// deterministic boundary even when the record predates the lifecycle feature.
/// Only fully undated legacy records remain visible because their age cannot be
/// reconstructed without guessing.
pub(crate) const FADEN_LIFETIME_HOURS: i64 = FIXED_FADEN_FADE_DAYS as i64 * 24;

fn checked_faden_expires_at(created_at: DateTime<Utc>) -> Option<DateTime<Utc>> {
    created_at.checked_add_signed(Duration::hours(FADEN_LIFETIME_HOURS))
}

fn faden_expires_at(created_at: DateTime<Utc>) -> DateTime<Utc> {
    checked_faden_expires_at(created_at)
        .expect("server-owned creation timestamps always permit a 168-hour lifetime")
}

/// Publicly project the canonical expiry without rewriting persisted legacy
/// records. Explicit values retain their original wire representation; a
/// missing value is deterministically derived only from a valid `created_at`.
fn projected_faden_expires_at(edge: &Edge) -> Option<String> {
    match edge.expires_at.as_ref() {
        Some(Some(expires_at)) => return Some(expires_at.as_str().to_owned()),
        // Explicit `null` paired with a dated `created_at` is non-canonical;
        // `edge_is_active_at` already hides such records from every read
        // surface that reaches this projection, so there is nothing to derive.
        Some(None) => return None,
        None => {}
    }

    let created_at = edge.created_at.as_ref()?.parsed()?;
    let expires_at = checked_faden_expires_at(*created_at)?;
    Some(expires_at.to_rfc3339_opts(SecondsFormat::AutoSi, true))
}

/// The time-bound window during which an edge is active, or [`Unbounded`]
/// for the fully undated legacy state that has no expiry at all.
///
/// [`Unbounded`]: EdgeLifecycleWindow::Unbounded
enum EdgeLifecycleWindow {
    Unbounded,
    Bounded {
        start: DateTime<Utc>,
        end: DateTime<Utc>,
    },
}

/// Resolve an edge's lifecycle window, or `Err` with a short reason when the
/// edge is invalid or non-canonical and therefore can never be active for
/// *any* `now`. Shared by [`edge_is_active_at`] (the per-request read gate)
/// and [`edge_is_permanently_unreachable`] (the PostgreSQL loader's
/// cache-admission gate), so both agree on exactly which persisted rows can
/// never surface through any read endpoint.
fn edge_lifecycle_window(edge: &Edge) -> Result<EdgeLifecycleWindow, &'static str> {
    let Some(created_at) = edge.created_at.as_ref() else {
        return if matches!(edge.expires_at, Some(Some(_))) {
            Err("expiring edge without created_at")
        } else {
            Ok(EdgeLifecycleWindow::Unbounded)
        };
    };

    let Some(created_at_value) = created_at.parsed() else {
        return Err("invalid created_at");
    };

    let expires_at_value = match edge.expires_at.as_ref() {
        Some(Some(expires_at)) => {
            let Some(expires_at_value) = expires_at.parsed() else {
                return Err("invalid lifecycle timestamp");
            };
            if expires_at_value.signed_duration_since(*created_at_value)
                != Duration::hours(FADEN_LIFETIME_HOURS)
            {
                return Err("non-canonical Faden lifetime");
            }
            *expires_at_value
        }
        // An explicit `null` paired with a dated `created_at` is exactly the
        // pairing the domain contract rejects: readers must not be able to
        // tell a dated Faden with an intentionally unbounded lifetime apart
        // from one whose expiry was simply never persisted. Fail closed
        // instead of silently treating it as the omitted/derive case.
        Some(None) => return Err("explicit null expires_at paired with a dated created_at"),
        None => {
            let Some(expires_at_value) = checked_faden_expires_at(*created_at_value) else {
                return Err("derived expiry exceeds the supported timestamp range");
            };
            expires_at_value
        }
    };

    Ok(EdgeLifecycleWindow::Bounded {
        start: *created_at_value,
        end: expires_at_value,
    })
}

/// Active-read predicate for Faden projections.
///
/// A missing `expires_at` is derived from a valid `created_at`, so Fäden created
/// before the lifecycle feature age exactly like new projections. Only records
/// with neither timestamp remain visible as undated legacy data. Malformed or
/// non-canonical lifecycle data fails closed. The exact boundary is exclusive,
/// so a Faden is gone at `now == expires_at`.
pub(crate) fn edge_is_active_at(edge: &Edge, now: DateTime<Utc>) -> bool {
    match edge_lifecycle_window(edge) {
        Err(reason) => {
            tracing::debug!(edge_id = %edge.id, reason, "hiding edge from active projection");
            false
        }
        Ok(EdgeLifecycleWindow::Unbounded) => true,
        Ok(EdgeLifecycleWindow::Bounded { start, end }) => now >= start && now < end,
    }
}

/// `true` when an edge can never be active for *any* `now` — invalid or
/// non-canonical lifecycle data (for example a dated `created_at` paired
/// with an explicit `expires_at: null`). Such a row can never be surfaced by
/// any read endpoint, so the PostgreSQL loader excludes it from the
/// fixed-size edge cache instead of letting it occupy a slot that a
/// genuinely reachable edge would need.
pub(crate) fn edge_is_permanently_unreachable(edge: &Edge) -> bool {
    edge_lifecycle_window(edge).is_err()
}

fn edge_has_valid_faden_metadata(edge: &Edge) -> bool {
    match (&edge.faden_type, &edge.faden_subject_id) {
        (None, None) => true,
        (Some(_), Some(subject_id)) => Uuid::parse_str(subject_id).is_ok(),
        _ => false,
    }
}

fn edge_matches_list_at(
    edge: &Edge,
    source_id: Option<&str>,
    target_id: Option<&str>,
    now: DateTime<Utc>,
) -> bool {
    source_id.is_none_or(|source_id| edge.source_id == source_id)
        && target_id.is_none_or(|target_id| edge.target_id == target_id)
        && edge_is_active_at(edge, now)
}

pub(crate) fn max_edges_cache_limit() -> usize {
    match std::env::var("MAX_EDGES_CACHE") {
        Ok(val) => match val.parse::<usize>() {
            Ok(v) => v,
            Err(_) => {
                tracing::warn!(
                    value = %val,
                    "Invalid MAX_EDGES_CACHE, falling back to default 500,000"
                );
                DEFAULT_MAX_EDGES_CACHE
            }
        },
        Err(_) => DEFAULT_MAX_EDGES_CACHE,
    }
}

pub async fn load_edges() -> std::io::Result<OrderedCache<Edge>> {
    let start = std::time::Instant::now();
    let path = edges_path();
    let file = match File::open(&path).await {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(OrderedCache::new());
        }
        Err(error) => return Err(error),
    };
    let mut lines = BufReader::new(file).lines();
    let mut edges = OrderedCache::new();
    let mut records_read = 0;
    let mut duplicates_count = 0;
    let mut line_number = 0usize;
    let mut cache_limit_warned = false;

    let max_edges = max_edges_cache_limit();

    while let Some(line) = lines.next_line().await? {
        line_number += 1;
        let edge: Edge = serde_json::from_str(&line).map_err(|error| {
            std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!(
                    "invalid edge JSONL at {} line {line_number}: {error}",
                    path.display()
                ),
            )
        })?;

        if !edge_has_valid_faden_metadata(&edge) {
            tracing::warn!(
                edge_id = %edge.id,
                "skipping domain edge with malformed typed Faden metadata"
            );
            continue;
        }

        // Checked before the cache-limit gate, mirroring the PostgreSQL
        // loader: a record that can never be active for any `now` must not
        // consume one of the `max_edges` slots that a genuinely reachable
        // edge would need.
        if edge_is_permanently_unreachable(&edge) {
            tracing::warn!(
                edge_id = %edge.id,
                "skipping domain edge that can never be active under any lifecycle rule"
            );
            continue;
        }

        if records_read >= max_edges {
            if !cache_limit_warned {
                tracing::warn!(
                    ?path,
                    max_edges,
                    "Edges cache limit reached; validating remaining records without caching them"
                );
                cache_limit_warned = true;
            }
            continue;
        }
        records_read += 1;

        if edges.insert(edge.id.clone(), edge) {
            duplicates_count += 1;
        }
    }

    let load_ms = start.elapsed().as_millis();
    tracing::info!(
        count = edges.len(),
        duplicates_count,
        load_ms,
        ?path,
        "Loaded edges into memory cache"
    );
    Ok(edges)
}

pub async fn list_edges(
    State(state): State<ApiState>,
    Query(params): Query<HashMap<String, String>>,
) -> Result<Json<ListResponse<PublicEdge>>, StatusCode> {
    let src = params.get("source_id").map(String::as_str);
    let dst = params.get("target_id").map(String::as_str);
    let limit: usize = parse_usize_param(&params, "limit", 250)?.min(MAX_PAGE_SIZE);
    let (cursor_mode, after_id) = parse_cursor_params(&params)?;
    validate_cursor_limit(cursor_mode, limit)?;

    let now = Utc::now();
    let matches = |edge: &&Edge| edge_matches_list_at(edge, src, dst, now);

    let cache = state.edges.read().await;

    if cursor_mode {
        // Cursor mode sorts by stable id ascending (see query::cursor_page),
        // independent of the file/insertion order used by the legacy path.
        let refs: Vec<&Edge> = cache.iter_in_order().filter(matches).collect();
        let page = cursor_page(
            refs,
            limit,
            after_id.as_deref(),
            |edge: &Edge| edge.id.as_str(),
            |edge: &Edge| PublicEdge::from(edge),
        );
        Ok(Json(ListResponse::Cursor(page)))
    } else {
        let offset: usize = parse_usize_param(&params, "offset", 0)?;
        let out: Vec<PublicEdge> = cache
            .iter_in_order()
            .filter(matches)
            .skip(offset)
            .take(limit)
            .map(PublicEdge::from)
            .collect();
        Ok(Json(ListResponse::Legacy(out)))
    }
}

pub async fn get_edge(
    State(state): State<ApiState>,
    Path(id): Path<String>,
) -> Result<Json<EdgeWithDetails>, StatusCode> {
    let cache = state.edges.read().await;
    let edge = cache.get(&id).cloned().ok_or(StatusCode::NOT_FOUND)?;
    if !edge_is_active_at(&edge, Utc::now()) {
        return Err(StatusCode::NOT_FOUND);
    }
    drop(cache);

    let mut source_details = None;
    let mut target_details = None;

    if let Some(src_type) = &edge.source_type {
        if src_type == "account" {
            let accounts = state.accounts.read().await;
            if let Some(account) = accounts.get(&edge.source_id) {
                source_details = Some(EdgeParticipantDetails {
                    id: account.public.id.clone(),
                    title: account.public.title.clone(),
                    r#type: Some(account.public.kind.clone()),
                });
            }
        } else if src_type == "node" {
            let nodes_cache = state.nodes.read().await;
            if let Some(node) = nodes_cache.get(&edge.source_id) {
                source_details = Some(EdgeParticipantDetails {
                    id: node.id.clone(),
                    title: node.title.clone(),
                    r#type: Some(node.kind.clone()),
                });
            }
        }
    }

    if let Some(tgt_type) = &edge.target_type {
        if tgt_type == "account" {
            let accounts = state.accounts.read().await;
            if let Some(account) = accounts.get(&edge.target_id) {
                target_details = Some(EdgeParticipantDetails {
                    id: account.public.id.clone(),
                    title: account.public.title.clone(),
                    r#type: Some(account.public.kind.clone()),
                });
            }
        } else if tgt_type == "node" {
            let nodes_cache = state.nodes.read().await;
            if let Some(node) = nodes_cache.get(&edge.target_id) {
                target_details = Some(EdgeParticipantDetails {
                    id: node.id.clone(),
                    title: node.title.clone(),
                    r#type: Some(node.kind.clone()),
                });
            }
        } else if tgt_type == "webgemeindezentrum" {
            if let Some(pool) = state.db_pool.as_ref() {
                let center: Option<(String, String)> = sqlx::query_as(
                    "SELECT id, name FROM webgemeindezentren \
                     WHERE faden_endpoint_id = $1::uuid",
                )
                .bind(&edge.target_id)
                .fetch_optional(pool)
                .await
                .map_err(|error| {
                    tracing::error!(%error, edge_id = %edge.id, "failed to resolve center edge endpoint");
                    StatusCode::INTERNAL_SERVER_ERROR
                })?;
                if let Some((id, title)) = center {
                    target_details = Some(EdgeParticipantDetails {
                        id,
                        title,
                        r#type: Some("webgemeindezentrum".to_string()),
                    });
                }
            }
        }
    }

    Ok(Json(EdgeWithDetails {
        edge: PublicEdge::from(&edge),
        source_details,
        target_details,
    }))
}

#[derive(Clone, Copy, Debug, Default)]
pub(crate) struct EdgeEndpointCollisionEvidence {
    pub(crate) account_exists: bool,
    pub(crate) role_exists: bool,
}

impl EdgeEndpointCollisionEvidence {
    pub(crate) fn has_collision(self) -> bool {
        self.account_exists || self.role_exists
    }
}

fn invalid_edge_endpoint_reference(
    value: &Value,
    endpoint: &str,
    type_field: &str,
    endpoint_id: &str,
) -> std::io::Error {
    let edge_id = value
        .get("id")
        .and_then(Value::as_str)
        .unwrap_or("<missing>");
    let found_type = value
        .get(type_field)
        .and_then(Value::as_str)
        .unwrap_or("<missing-or-non-string>");
    std::io::Error::new(
        std::io::ErrorKind::InvalidData,
        format!(
            "edge {edge_id} uses endpoint {endpoint} id {endpoint_id} with invalid {type_field} {found_type}"
        ),
    )
}

fn edge_endpoint_references_node_for_delete(
    value: &Value,
    id_field: &str,
    type_field: &str,
    endpoint: &str,
    node_id: &str,
    evidence: EdgeEndpointCollisionEvidence,
) -> std::io::Result<bool> {
    let Some(endpoint_id) = value.get(id_field).and_then(Value::as_str) else {
        return Ok(false);
    };
    if endpoint_id != node_id {
        return Ok(false);
    }

    match value.get(type_field) {
        Some(Value::String(kind)) if kind == "node" => Ok(true),
        Some(Value::String(kind)) if kind == "account" || kind == "role" => Ok(false),
        None if !evidence.has_collision() => Ok(true),
        None => Err(invalid_edge_endpoint_reference(
            value,
            endpoint,
            type_field,
            endpoint_id,
        )),
        Some(_) => Err(invalid_edge_endpoint_reference(
            value,
            endpoint,
            type_field,
            endpoint_id,
        )),
    }
}

pub(crate) fn edge_value_references_node_for_delete(
    value: &Value,
    node_id: &str,
    evidence: EdgeEndpointCollisionEvidence,
) -> std::io::Result<bool> {
    let source_references_node = edge_endpoint_references_node_for_delete(
        value,
        "source_id",
        "source_type",
        "source",
        node_id,
        evidence,
    )?;
    let target_references_node = edge_endpoint_references_node_for_delete(
        value,
        "target_id",
        "target_type",
        "target",
        node_id,
        evidence,
    )?;
    Ok(source_references_node || target_references_node)
}

pub(crate) fn edge_references_node_for_delete(
    edge: &Edge,
    node_id: &str,
    evidence: EdgeEndpointCollisionEvidence,
) -> std::io::Result<bool> {
    let source_references_node = edge_endpoint_option_references_node_for_delete(
        edge.id.as_str(),
        edge.source_id.as_str(),
        edge.source_type.as_deref(),
        "source",
        "source_type",
        node_id,
        evidence,
    )?;
    let target_references_node = edge_endpoint_option_references_node_for_delete(
        edge.id.as_str(),
        edge.target_id.as_str(),
        edge.target_type.as_deref(),
        "target",
        "target_type",
        node_id,
        evidence,
    )?;
    Ok(source_references_node || target_references_node)
}

fn edge_endpoint_option_references_node_for_delete(
    edge_id: &str,
    endpoint_id: &str,
    endpoint_type: Option<&str>,
    endpoint: &str,
    type_field: &str,
    node_id: &str,
    evidence: EdgeEndpointCollisionEvidence,
) -> std::io::Result<bool> {
    if endpoint_id != node_id {
        return Ok(false);
    }

    match endpoint_type {
        Some("node") => Ok(true),
        Some("account" | "role") => Ok(false),
        None if !evidence.has_collision() => Ok(true),
        Some(found_type) => Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            format!(
                "edge {edge_id} uses endpoint {endpoint} id {endpoint_id} with invalid {type_field} {found_type}"
            ),
        )),
        None => Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            format!(
                "edge {edge_id} uses endpoint {endpoint} id {endpoint_id} with invalid {type_field} <missing>"
            ),
        )),
    }
}

/// Append a single edge record as a JSONL line. Durability via fsync.
/// Callers MUST hold the `edge_create_persist_lock` to serialize writes.
///
/// If the existing file does not end with a newline (e.g. a hand-written or
/// truncated fixture), a separator newline is written first so the previous
/// record and the new record are never glued into one unparseable line.
async fn append_edge_line(record: &Value) -> std::io::Result<()> {
    let maximum_bytes = load_policy_limits()
        .await
        .map_err(|errors| {
            std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!("JSONL limits policy unavailable: {}", errors.join("; ")),
            )
        })?
        .max_edges_jsonl_bytes();
    let path = edges_path();
    if let Some(parent) = path.parent() {
        tokio::fs::create_dir_all(parent).await?;
    }
    let line = serde_json::to_string(record)
        .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;

    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .read(true)
        .open(&path)
        .await?;

    // Preserve the JSONL record boundary: only the last byte is read for the
    // check (no full-file scan, no rewrite). With O_APPEND the seek moves the
    // read position only; writes still always land at the end of the file.
    let len = file.metadata().await?.len();
    let mut separator_bytes = 0_u64;
    if len > 0 {
        file.seek(SeekFrom::Start(len - 1)).await?;
        let mut last = [0_u8; 1];
        file.read_exact(&mut last).await?;
        if last[0] != b'\n' {
            separator_bytes = 1;
        }
    }

    let line_bytes = u64::try_from(line.len())
        .map_err(|_| std::io::Error::other("edge JSONL record length exceeds u64"))?;
    let resulting_bytes = len
        .checked_add(separator_bytes)
        .and_then(|bytes| bytes.checked_add(line_bytes))
        .and_then(|bytes| bytes.checked_add(1))
        .ok_or_else(|| std::io::Error::other("edge JSONL resulting size overflow"))?;
    ensure_jsonl_size("edges", resulting_bytes, maximum_bytes)?;

    if separator_bytes == 1 {
        file.write_all(b"\n").await?;
    }

    file.write_all(line.as_bytes()).await?;
    file.write_all(b"\n").await?;
    file.flush().await?;
    file.sync_all().await?;
    Ok(())
}

fn replace_edge_operation_record(
    input: &[u8],
    record: &Value,
    operation: &CreateOperationKey,
) -> std::io::Result<Vec<u8>> {
    let replacement = serde_json::to_vec(record)
        .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))?;
    let mut output = Vec::with_capacity(input.len().max(replacement.len() + 1));
    let mut matches = 0usize;

    for segment in input.split_inclusive(|byte| *byte == b'\n') {
        let (body, delimiter): (&[u8], &[u8]) = if let Some(body) = segment.strip_suffix(b"\r\n") {
            (body, b"\r\n")
        } else if let Some(body) = segment.strip_suffix(b"\n") {
            (body, b"\n")
        } else {
            (segment, b"")
        };
        let operation_matches = std::str::from_utf8(body)
            .ok()
            .and_then(|line| serde_json::from_str::<Value>(line).ok())
            .is_some_and(|value| {
                value.get(CREATE_ACTOR_KEY).and_then(Value::as_str)
                    == Some(operation.actor_id.as_str())
                    && value.get(CREATE_OPERATION_KEY).and_then(Value::as_str)
                        == Some(operation.operation_id.as_str())
            });
        if operation_matches {
            matches += 1;
            if matches > 1 {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::InvalidData,
                    "duplicate edge create operation metadata",
                ));
            }
            output.extend_from_slice(&replacement);
            output.extend_from_slice(delimiter);
        } else {
            output.extend_from_slice(segment);
        }
    }
    if matches != 1 {
        return Err(std::io::Error::new(
            std::io::ErrorKind::NotFound,
            "edge create operation record not found",
        ));
    }
    Ok(output)
}

/// Atomically replace exactly one operation-bound JSONL record.
///
/// Callers hold `edge_create_persist_lock`, so no create or reactivation can
/// race the scan-and-rename sequence in this process. Unrelated bytes, including
/// malformed legacy lines and their line endings, are preserved verbatim.
async fn replace_edge_operation_line(
    record: &Value,
    operation: &CreateOperationKey,
) -> std::io::Result<()> {
    let maximum_bytes = load_policy_limits()
        .await
        .map_err(|errors| {
            std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!("JSONL limits policy unavailable: {}", errors.join("; ")),
            )
        })?
        .max_edges_jsonl_bytes();
    let path = edges_path();
    let metadata = tokio::fs::metadata(&path).await?;
    let input = tokio::fs::read(&path).await?;
    let output = replace_edge_operation_record(&input, record, operation)?;
    let output_bytes = u64::try_from(output.len())
        .map_err(|_| std::io::Error::other("edge JSONL rewrite length exceeds u64"))?;
    ensure_jsonl_size("edges", output_bytes, maximum_bytes)?;

    let parent = path.parent().ok_or_else(|| {
        std::io::Error::new(std::io::ErrorKind::InvalidInput, "edges path has no parent")
    })?;
    tokio::fs::create_dir_all(parent).await?;
    let file_name = path
        .file_name()
        .and_then(|value| value.to_str())
        .ok_or_else(|| {
            std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "edges path has no UTF-8 filename",
            )
        })?;
    let temporary = path.with_file_name(format!(".{file_name}.reactivate-{}.tmp", Uuid::new_v4()));
    let result = async {
        let mut file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&temporary)
            .await?;
        tokio::fs::set_permissions(&temporary, metadata.permissions()).await?;
        file.write_all(&output).await?;
        file.flush().await?;
        file.sync_all().await?;
        tokio::fs::rename(&temporary, &path).await?;
        File::open(parent).await?.sync_all().await?;
        Ok::<(), std::io::Error>(())
    }
    .await;
    if result.is_err() {
        tokio::fs::remove_file(&temporary).await.ok();
    }
    result
}

const CREATE_ACTOR_KEY: &str = "_create_actor_id";
const CREATE_OPERATION_KEY: &str = "_create_operation_id";

fn add_create_operation_metadata(record: &mut Value, operation: Option<&CreateOperationKey>) {
    let Some(operation) = operation else {
        return;
    };
    let object = record
        .as_object_mut()
        .expect("canonical edge create record must be an object");
    object.insert(
        CREATE_ACTOR_KEY.to_string(),
        Value::String(operation.actor_id.clone()),
    );
    object.insert(
        CREATE_OPERATION_KEY.to_string(),
        Value::String(operation.operation_id.clone()),
    );
}

fn edge_matches_create(edge: &Edge, expected: &edge_create::ValidatedCreateEdge) -> bool {
    expected
        .id
        .as_ref()
        .is_none_or(|expected_id| edge.id == *expected_id)
        && edge.source_id == expected.source_id
        && edge.source_type.as_deref() == Some(expected.source_type.as_str())
        && edge.target_id == expected.target_id
        && edge.target_type.as_deref() == Some(expected.target_type.as_str())
        && edge.edge_kind == expected.edge_kind
        && ((edge.faden_type.is_none() && edge.faden_subject_id.is_none())
            || (edge.faden_type == expected.faden_type
                && edge.faden_subject_id == expected.faden_subject_id))
        && edge.note == expected.note
}

/// Outcome of inspecting the persisted edges file before a create.
#[derive(Debug, Clone)]
struct EdgePersistenceStatus {
    /// The file already holds at least `max_edges_cache_limit()` cache-admissible
    /// edges, so another appended edge would not be materialized after restart.
    cache_limit_reached: bool,
    /// The id already exists somewhere in the persistence source, even when
    /// it is not in the in-memory cache (e.g. in a suffix the loader
    /// truncated away).
    duplicate_id: bool,
    /// Durable result of the same account-scoped operation, when present.
    existing_operation: Option<Edge>,
}

/// Scan the persisted edges file once before an append. Cache-limit accounting
/// follows [`load_edges`] for parseable edges that could ever be active (matching
/// [`edge_is_permanently_unreachable`]). The scan is strict about JSON and `Edge`
/// projection for the same reason as startup: a create must never append to a
/// canonical file that the next restart would reject. The scan remains read-only
/// and preserves every existing byte. A final unterminated line is still read.
/// The whole file is scanned — also beyond the limit — so duplicate ids and an earlier operation result in an
/// unmaterialized suffix remain detectable regardless of lifecycle validity:
/// an id must never be reusable just because the record that first claimed
/// it later became lifecycle-invalid or fell out of cache capacity. A
/// missing file means an empty persistence source. Callers MUST hold the
/// `edge_create_persist_lock`.
async fn inspect_edge_persistence_for_create(
    id: &str,
    operation: Option<&CreateOperationKey>,
) -> std::io::Result<EdgePersistenceStatus> {
    let path = edges_path();
    let max_edges = max_edges_cache_limit();
    let file = match File::open(&path).await {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(EdgePersistenceStatus {
                cache_limit_reached: max_edges == 0,
                duplicate_id: false,
                existing_operation: None,
            });
        }
        Err(error) => return Err(error),
    };

    let mut lines = BufReader::new(file).lines();
    let mut lines_read = 0usize;
    let mut duplicate_id = false;
    let mut existing_operation = None;
    let mut line_number = 0usize;

    while let Some(line) = lines.next_line().await? {
        line_number += 1;
        let value: Value = serde_json::from_str(&line).map_err(|error| {
            std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!(
                    "invalid edge JSONL at {} line {line_number}: {error}",
                    path.display()
                ),
            )
        })?;

        let operation_matches = operation.is_some_and(|operation| {
            value.get(CREATE_ACTOR_KEY).and_then(Value::as_str) == Some(operation.actor_id.as_str())
                && value.get(CREATE_OPERATION_KEY).and_then(Value::as_str)
                    == Some(operation.operation_id.as_str())
        });
        let edge: Edge = serde_json::from_value(value).map_err(|error| {
            std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!(
                    "invalid edge JSONL at {} line {line_number}: {error}",
                    path.display()
                ),
            )
        })?;

        // Duplicate-id and operation-replay detection must see every
        // parseable line regardless of lifecycle validity: an id must stay
        // permanently claimed even if the record that claimed it can never
        // be active. Only the cache-limit accounting below mirrors the
        // loader's admission rule.
        if edge.id == id {
            duplicate_id = true;
        }
        if operation_matches {
            if existing_operation.is_some() {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::InvalidData,
                    "duplicate edge create operation metadata",
                ));
            }
            existing_operation = Some(edge.clone());
        }

        if !edge_is_permanently_unreachable(&edge) {
            lines_read += 1;
        }
    }

    Ok(EdgePersistenceStatus {
        cache_limit_reached: lines_read >= max_edges,
        duplicate_id,
        existing_operation,
    })
}

/// Build the in-memory `Edge` and its canonical JSONL record from a validated
/// create request plus the server-owned `id` and `created_at`.
///
/// The returned `Edge` carries exactly the values that land in the cache and
/// the JSONL line. `note` is only written to the record when present (absent
/// means omitted, never `null`). `created_at` and `expires_at` are server-owned;
/// client `expires_at`, `payload`, and `metadata` remain rejected.
fn build_edge_record(
    validated: edge_create::ValidatedCreateEdge,
    id: String,
    created_at: DateTime<Utc>,
) -> (Edge, Value) {
    let expires_at = faden_expires_at(created_at);
    let edge = Edge {
        id,
        source_id: validated.source_id,
        source_type: Some(validated.source_type),
        target_id: validated.target_id,
        target_type: Some(validated.target_type),
        edge_kind: validated.edge_kind,
        faden_type: validated.faden_type,
        faden_subject_id: validated.faden_subject_id,
        note: validated.note,
        created_at: Some(LifecycleTimestamp::from_datetime(created_at)),
        expires_at: Some(Some(LifecycleTimestamp::from_datetime(expires_at))),
    };

    let mut record = serde_json::Map::new();
    record.insert("id".into(), json!(edge.id));
    record.insert("source_id".into(), json!(edge.source_id));
    record.insert("source_type".into(), json!(edge.source_type));
    record.insert("target_id".into(), json!(edge.target_id));
    record.insert("target_type".into(), json!(edge.target_type));
    record.insert("edge_kind".into(), json!(edge.edge_kind));
    if let Some(faden_type) = edge.faden_type {
        record.insert("faden_type".into(), json!(faden_type));
    }
    if let Some(faden_subject_id) = &edge.faden_subject_id {
        record.insert("faden_subject_id".into(), json!(faden_subject_id));
    }
    record.insert("created_at".into(), json!(edge.created_at));
    // `json!` serializes the bare `Option<Option<_>>` value directly rather
    // than through `Edge`'s field-level `skip_serializing_if`, so both the
    // omitted and explicit-null states would otherwise collapse to a literal
    // `null` here. A freshly created edge always carries a concrete server-
    // derived expiry, so flatten to the inner timestamp before serializing.
    record.insert(
        "expires_at".into(),
        json!(edge.expires_at.clone().flatten()),
    );
    if let Some(note) = &edge.note {
        record.insert("note".into(), json!(note));
    }

    (edge, Value::Object(record))
}

/// Build the canonical account→node Faden for a successful node creation.
/// PostgreSQL node creation persists this value in the same transaction as the
/// node; JSONL callers continue through the regular projection writer.
pub(crate) fn build_node_origin_faden(account_id: &str, node_id: &str) -> Edge {
    let validated = edge_create::ValidatedCreateEdge {
        id: None,
        source_id: account_id.to_string(),
        target_id: node_id.to_string(),
        edge_kind: "reference".to_string(),
        source_type: "account".to_string(),
        target_type: "node".to_string(),
        faden_type: Some(FadenType::Knotting),
        faden_subject_id: Some(node_id.to_string()),
        note: None,
        operation_id: None,
    };
    build_edge_record(validated, Uuid::new_v4().to_string(), Utc::now()).0
}

/// Map an `EdgeCreateValidationError` onto a stable message for the 400 body.
fn edge_create_error_message(err: &edge_create::EdgeCreateValidationError) -> String {
    use edge_create::EdgeCreateValidationError as E;
    match err {
        E::MissingOrEmptyField(field) => format!("missing or empty field: {field}"),
        E::InvalidEnumValue { field, value } => {
            format!("invalid enum value for {field}: {value}")
        }
        E::InvalidUuid { field, value } => format!("invalid UUID for {field}: {value}"),
        E::IncompleteFadenMetadata => {
            "faden_type and faden_subject_id must be supplied together".to_string()
        }
        E::NoteTooLong => "note exceeds the maximum length of 1000 characters".to_string(),
    }
}

/// Internal projection primitive for a Faden derived from a Webungsaktion.
///
/// This function is deliberately not registered as an HTTP route. Domain
/// actions call it after their own durable state transition and reuse a stable
/// operation id so a retry repairs a missing projection without duplication.
///
/// Write path: write gate ([`reject_edge_create_unless_writable`]) -> contract
/// validation (PR-1 semantics) -> server-generated `id` / `created_at` ->
/// persistence via the configured edge-create write source -> cache insert. A
/// new durable write returns 201; an identical operation replay returns the
/// existing edge with 200. Failed writes never leave a phantom cache entry.
///
/// Runtime default is read-only: JSONL may still be loaded as fallback data,
/// but derived Fäden are not persisted until PostgreSQL is selected explicitly.
/// The JSONL append/rewrite arm remains temporarily for direct legacy
/// persistence tests; successful runtime configuration cannot activate it.
/// PostgreSQL (`WELTGEWEBE_DOMAIN_EDGE_WRITE_SOURCE=postgres`, requiring the
/// PostgreSQL read source) is the only configurable durable writer. No
/// dual-write or fallback exists.
pub async fn create_edge(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
    Json(payload): Json<Value>,
) -> Result<(StatusCode, Json<Edge>), (StatusCode, String)> {
    project_edge(state, auth, payload, FadenProjectionMode::EnsureOnly).await
}

pub(crate) async fn reactivate_edge(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
    Json(payload): Json<Value>,
) -> Result<(StatusCode, Json<Edge>), (StatusCode, String)> {
    project_edge(state, auth, payload, FadenProjectionMode::Reactivate).await
}

async fn project_edge(
    state: ApiState,
    auth: AuthContext,
    payload: Value,
    projection_mode: FadenProjectionMode,
) -> Result<(StatusCode, Json<Edge>), (StatusCode, String)> {
    reject_edge_create_unless_writable(&state)?;

    // Manual deserialization keeps unknown fields, missing required fields and
    // explicit nulls on one deterministic 400 contract.
    let request: edge_create::CreateEdgeRequest = serde_json::from_value(payload).map_err(|e| {
        (
            StatusCode::BAD_REQUEST,
            format!("invalid edge create request: {e}"),
        )
    })?;

    let validated = request.validate().map_err(|e| {
        (
            StatusCode::BAD_REQUEST,
            format!(
                "invalid edge create request: {}",
                edge_create_error_message(&e)
            ),
        )
    })?;
    let semantic_request = validated.clone();

    // Endpoint type labels are security-relevant: account detail projections
    // must never be reachable by disguising a known account id as a node. Keep
    // the check independent of role so administrative imports cannot persist a
    // type-confused edge either. Unknown ids remain importable; their declared
    // type is still enforced when a matching entity becomes visible on reads.
    let (source_is_known_account, target_is_known_account) = {
        let accounts = state.accounts.read().await;
        (
            accounts.get(&validated.source_id).is_some(),
            accounts.get(&validated.target_id).is_some(),
        )
    };
    if (source_is_known_account && validated.source_type != "account")
        || (target_is_known_account && validated.target_type != "account")
    {
        return Err((
            StatusCode::BAD_REQUEST,
            "edge endpoint type does not match the referenced account".to_string(),
        ));
    }

    // A non-admin may involve an account only through an outgoing action of
    // the authenticated Garnrolle. This rejects both source impersonation and
    // crafted node -> foreign-account edges that would otherwise make another
    // Garnrolle appear to have acted. Admins retain the explicit repair/import
    // path; incoming administrative edges are attributed neutrally on reads.
    if auth.role != Role::Admin
        && (validated.source_type == "account" || validated.target_type == "account")
    {
        let own_account_id = auth.account_id.as_deref().ok_or_else(|| {
            (
                StatusCode::UNAUTHORIZED,
                "authenticated account context missing".to_string(),
            )
        })?;
        if validated.source_type != "account" || validated.source_id != own_account_id {
            return Err((
                StatusCode::FORBIDDEN,
                "account relationships must originate from the authenticated Garnrolle".to_string(),
            ));
        }
    }

    let operation = match validated.operation_id.as_ref() {
        Some(operation_id) => {
            let actor_id = auth.account_id.clone().ok_or_else(|| {
                (
                    StatusCode::UNAUTHORIZED,
                    "authenticated account context missing".to_string(),
                )
            })?;
            Some(CreateOperationKey {
                actor_id,
                operation_id: operation_id.clone(),
            })
        }
        None => None,
    };

    // Server-owned values: generate `id` when the client omitted it and stamp
    // `created_at`. The operation id identifies only a retry and never becomes
    // the resource id.
    let id = validated
        .id
        .clone()
        .unwrap_or_else(|| Uuid::new_v4().to_string());
    let created_at = Utc::now();
    let (edge, mut record) = build_edge_record(validated, id, created_at);
    add_create_operation_metadata(&mut record, operation.as_ref());

    // Exactly one configured persistence source is used; there is no
    // JSONL/PostgreSQL dual-write or fallback.
    match state.config.domain_edge_write_source {
        DomainEdgeWriteSource::ReadOnly => {
            return Err(write_source_read_only_conflict());
        }
        DomainEdgeWriteSource::Jsonl => {
            // One lock covers operation lookup, duplicate/limit inspection and
            // append, so concurrent retries cannot both write.
            let _persist_guard = edge_create_persist_lock().lock().await;

            // The existing full-file safety scan now also finds an earlier
            // operation result, avoiding a second pass over large JSONL files.
            let persistence = inspect_edge_persistence_for_create(&edge.id, operation.as_ref())
                .await
                .map_err(|e| {
                    tracing::error!(error = %e, "failed to inspect edges JSONL before create");
                    (
                        StatusCode::INTERNAL_SERVER_ERROR,
                        "failed to persist edge".to_string(),
                    )
                })?;
            if let Some(existing) = persistence.existing_operation {
                if !edge_matches_create(&existing, &semantic_request) {
                    return Err((
                        StatusCode::CONFLICT,
                        "edge operation id was already used for different data".to_string(),
                    ));
                }
                if projection_mode == FadenProjectionMode::Reactivate {
                    let operation = operation
                        .as_ref()
                        .expect("an existing operation result always has an operation key");
                    let (reactivated, mut reactivated_record) = build_edge_record(
                        semantic_request.clone(),
                        existing.id.clone(),
                        Utc::now(),
                    );
                    add_create_operation_metadata(&mut reactivated_record, Some(operation));
                    replace_edge_operation_line(&reactivated_record, operation)
                        .await
                        .map_err(|error| {
                            let status = jsonl_write_status(&error);
                            tracing::error!(%error, %status, edge_id = %existing.id, "failed to reactivate edge in JSONL");
                            (
                                status,
                                if status == StatusCode::INSUFFICIENT_STORAGE {
                                    "edge JSONL storage limit reached".to_string()
                                } else {
                                    "failed to reactivate edge".to_string()
                                },
                            )
                        })?;
                    let mut edges = state.edges.write().await;
                    edges.insert(reactivated.id.clone(), reactivated.clone());
                    tracing::info!(
                        event = "edge.reactivated",
                        edge_id = %reactivated.id,
                        write_source = ?state.config.domain_edge_write_source,
                        "Existing Faden lifecycle restarted"
                    );
                    return Ok((StatusCode::OK, Json(reactivated)));
                }
                let mut edges = state.edges.write().await;
                edges.insert(existing.id.clone(), existing.clone());
                return Ok((StatusCode::OK, Json(existing)));
            }
            if persistence.duplicate_id {
                return Err((StatusCode::CONFLICT, "edge id already exists".to_string()));
            }
            if persistence.cache_limit_reached {
                return Err((StatusCode::CONFLICT, "edge cache limit reached".to_string()));
            }

            {
                let edges = state.edges.read().await;
                if edges.get(&edge.id).is_some() {
                    return Err((StatusCode::CONFLICT, "edge id already exists".to_string()));
                }
            }

            // Cache mutation follows durable append; failed writes never
            // leave a phantom Faden in memory.
            if let Err(error) = append_edge_line(&record).await {
                let status = jsonl_write_status(&error);
                tracing::error!(%error, %status, "failed to append edge to JSONL");
                return Err((
                    status,
                    if status == StatusCode::INSUFFICIENT_STORAGE {
                        "edge JSONL storage limit reached".to_string()
                    } else {
                        "failed to persist edge".to_string()
                    },
                ));
            }

            let mut edges = state.edges.write().await;
            edges.insert(edge.id.clone(), edge.clone());
        }
        DomainEdgeWriteSource::Postgres => {
            // PostgreSQL mode never inspects or appends JSONL. The existing
            // serialized transaction also owns the idempotency lookup.
            {
                let edges = state.edges.read().await;
                if edges.get(&edge.id).is_some() {
                    return Err((StatusCode::CONFLICT, "edge id already exists".to_string()));
                }
            }

            // Missing pool state is an internal error, never permission to
            // degrade silently to the JSONL write path.
            let pool = state.db_pool.as_ref().ok_or_else(|| {
                (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "PostgreSQL pool unavailable for edge write".to_string(),
                )
            })?;
            match insert_domain_edge(pool, &edge, operation.as_ref()).await {
                Ok(CreateWriteOutcome::Created) => {}
                Ok(CreateWriteOutcome::Existing(existing)) => {
                    if !edge_matches_create(&existing, &semantic_request) {
                        return Err((
                            StatusCode::CONFLICT,
                            "edge operation id was already used for different data".to_string(),
                        ));
                    }
                    if projection_mode == FadenProjectionMode::Reactivate {
                        let operation = operation
                            .as_ref()
                            .expect("an existing operation result always has an operation key");
                        let (reactivated, _) = build_edge_record(
                            semantic_request.clone(),
                            existing.id.clone(),
                            Utc::now(),
                        );
                        let reactivated = reactivate_domain_edge(pool, &reactivated, operation)
                            .await
                            .map_err(|error| match error {
                                EdgeWriteError::OperationConflict => (
                                    StatusCode::CONFLICT,
                                    "edge operation id was already used for different data".to_string(),
                                ),
                                other => {
                                    tracing::error!(error = %other, edge_id = %existing.id, "failed to reactivate edge in domain_edges");
                                    (
                                        StatusCode::INTERNAL_SERVER_ERROR,
                                        "failed to reactivate edge".to_string(),
                                    )
                                }
                            })?;
                        let mut edges = state.edges.write().await;
                        edges.insert(reactivated.id.clone(), reactivated.clone());
                        tracing::info!(
                            event = "edge.reactivated",
                            edge_id = %reactivated.id,
                            write_source = ?state.config.domain_edge_write_source,
                            "Existing Faden lifecycle restarted"
                        );
                        return Ok((StatusCode::OK, Json(reactivated)));
                    }
                    let mut edges = state.edges.write().await;
                    edges.insert(existing.id.clone(), existing.clone());
                    return Ok((StatusCode::OK, Json(existing)));
                }
                Err(EdgeWriteError::DuplicateId) => {
                    return Err((StatusCode::CONFLICT, "edge id already exists".to_string()));
                }
                Err(EdgeWriteError::CacheLimitReached) => {
                    return Err((StatusCode::CONFLICT, "edge cache limit reached".to_string()));
                }
                Err(e) => {
                    tracing::error!(error = %e, "failed to insert edge into domain_edges");
                    return Err((
                        StatusCode::INTERNAL_SERVER_ERROR,
                        "failed to persist edge".to_string(),
                    ));
                }
            }

            let mut edges = state.edges.write().await;
            edges.insert(edge.id.clone(), edge.clone());
        }
    }

    tracing::info!(
        event = "edge.created",
        edge_id = %edge.id,
        write_source = ?state.config.domain_edge_write_source,
        operation_bound = operation.is_some(),
        "Edge created"
    );

    Ok((StatusCode::CREATED, Json(edge)))
}

/// Edge-create request contract — OPT-ARC-001 Phase E-C, PR-1 (Semantik-Lock).
///
/// This module locks the *accepted* shape and validation rules of a
/// internal derived-Faden projection request without taking on routing, persistence, or
/// status-code concerns. The [`create_edge`] handler (PR-2, JSONL edge create)
/// consumes `CreateEdgeRequest` / `CreateEdgeRequest::validate` and owns the
/// HTTP mapping.
///
/// Locked semantics (see
/// `docs/reports/domain-edge-create-semantics-preflight.md`):
/// - `created_at` is not accepted from clients — the server owns the timestamp.
/// - client `expires_at` is not accepted — the server derives it from `created_at`,
///   so supplied values are rejected via `deny_unknown_fields` rather than ignored.
/// - `payload` / `metadata` are not accepted (the edge contract forbids them).
/// - any other unknown field is rejected instead of silently ignored.
/// - `source_type` / `target_type` are required and enum-checked.
/// - `edge_kind` is required and enum-checked.
/// - `source_id` / `target_id` are required, non-blank, and UUID-formatted.
/// - `id` is optional but, when present, must be non-blank and UUID-formatted.
/// - `note` is optional but, when present, must be non-blank and ≤ 1000 chars.
/// - `operation_id` is optional but, when present, must be a non-null UUID and
///   identifies only one account-scoped create action.
///
/// `deny_unknown_fields` is applied **only** to `CreateEdgeRequest`, never to the
/// read-side `Edge` model, so existing JSONL/read semantics stay untouched.
mod edge_create {
    use serde::de::{self, Deserialize, Deserializer, Visitor};
    use std::fmt;
    use uuid::Uuid;

    /// Allowed `edge_kind` values, mirroring `contracts/domain/edge.schema.json`.
    const EDGE_KIND_VALUES: [&str; 4] = ["delegation", "membership", "ownership", "reference"];

    /// Allowed `source_type` / `target_type` values, mirroring the edge contract.
    const EDGE_PARTICIPANT_TYPE_VALUES: [&str; 4] =
        ["role", "node", "account", "webgemeindezentrum"];

    /// Maximum `note` length in characters, mirroring the edge contract
    /// (`maxLength: 1000`). JSON Schema counts characters, not bytes.
    const EDGE_NOTE_MAX_LEN: usize = 1000;

    /// Deserialize an optional string field with hardened null-semantics: the
    /// field may be **absent** (`#[serde(default)]` yields `None`) but an
    /// explicit JSON `null` is **rejected** instead of being coerced to `None`.
    /// A present value must be a string; numbers, objects, and arrays fail.
    ///
    /// This keeps "optional" meaning "may be omitted" rather than "may be
    /// nulled", so a client cannot erase an edge field by sending `null` — no
    /// silent meaning-loss in JSON costume.
    fn deserialize_optional_non_null_string<'de, D>(
        deserializer: D,
    ) -> Result<Option<String>, D::Error>
    where
        D: Deserializer<'de>,
    {
        struct OptionalNonNullStringVisitor;

        impl<'de> Visitor<'de> for OptionalNonNullStringVisitor {
            type Value = Option<String>;

            fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str("a string or an absent field, not null")
            }

            fn visit_some<D>(self, deserializer: D) -> Result<Self::Value, D::Error>
            where
                D: Deserializer<'de>,
            {
                String::deserialize(deserializer).map(Some)
            }

            fn visit_none<E>(self) -> Result<Self::Value, E>
            where
                E: de::Error,
            {
                Err(E::custom("null is not allowed; omit the field instead"))
            }

            fn visit_unit<E>(self) -> Result<Self::Value, E>
            where
                E: de::Error,
            {
                Err(E::custom("null is not allowed; omit the field instead"))
            }
        }

        deserializer.deserialize_option(OptionalNonNullStringVisitor)
    }

    /// Accepted shape of a future internal derived-Faden projection request.
    ///
    /// `created_at`, client `expires_at`, `payload`, and `metadata` are intentionally
    /// absent; together with `deny_unknown_fields` they are rejected rather than
    /// silently dropped. `source_type` and `target_type` are **required**,
    /// matching the domain contract. The remaining optional fields `id` and
    /// `note` may be omitted but reject an explicit `null`, so "optional" never
    /// quietly collapses into "nullable".
    ///
    /// `pub(super)` exposes the type to the parent `edges` module so the upcoming
    /// internal projection writer can consume it without a later visibility rework.
    #[derive(Debug, serde::Deserialize)]
    #[serde(deny_unknown_fields)]
    pub(super) struct CreateEdgeRequest {
        #[serde(default, deserialize_with = "deserialize_optional_non_null_string")]
        id: Option<String>,
        source_id: String,
        target_id: String,
        edge_kind: String,
        source_type: String,
        target_type: String,
        #[serde(
            default,
            deserialize_with = "super::deserialize_optional_non_null_faden_type"
        )]
        faden_type: Option<super::FadenType>,
        #[serde(default, deserialize_with = "deserialize_optional_non_null_string")]
        faden_subject_id: Option<String>,
        #[serde(default, deserialize_with = "deserialize_optional_non_null_string")]
        note: Option<String>,
        #[serde(default, deserialize_with = "deserialize_optional_non_null_string")]
        operation_id: Option<String>,
    }

    /// Validated form of a `CreateEdgeRequest`.
    ///
    /// Values are preserved verbatim — no lowercasing, no trimming into storage.
    /// Whitespace is only inspected to reject blank required/optional fields.
    /// Fields are `pub(super)` so the internal projection writer in the parent
    /// `edges` module can read the validated values directly.
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub(super) struct ValidatedCreateEdge {
        pub(super) id: Option<String>,
        pub(super) source_id: String,
        pub(super) target_id: String,
        pub(super) edge_kind: String,
        pub(super) source_type: String,
        pub(super) target_type: String,
        pub(super) faden_type: Option<super::FadenType>,
        pub(super) faden_subject_id: Option<String>,
        pub(super) note: Option<String>,
        pub(super) operation_id: Option<String>,
    }

    /// Why a `CreateEdgeRequest` failed validation.
    ///
    /// Intentionally HTTP-agnostic: this PR fixes the create contract and its
    /// validation only. Status-code mapping belongs to the internal projection boundary.
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub(super) enum EdgeCreateValidationError {
        /// A required field was missing/blank, or an optional field that, when
        /// present, must be non-blank (`id`, `note`) was blank.
        MissingOrEmptyField(&'static str),
        /// An enum-constrained field held a value outside its allowed set.
        InvalidEnumValue { field: &'static str, value: String },
        /// An id-like field (`id`, `source_id`, `target_id`) was not a valid UUID.
        InvalidUuid { field: &'static str, value: String },
        /// Typed participation metadata must contain both the semantic type
        /// and its stable target id, or neither for a legacy projection.
        IncompleteFadenMetadata,
        /// `note` exceeded the contract maximum of 1000 characters.
        NoteTooLong,
    }

    /// Reject a field that is empty or whitespace-only.
    fn require_non_blank(
        field: &'static str,
        value: &str,
    ) -> Result<(), EdgeCreateValidationError> {
        if value.trim().is_empty() {
            return Err(EdgeCreateValidationError::MissingOrEmptyField(field));
        }
        Ok(())
    }

    /// Reject a value outside its allowed enum set. Matching is exact: wrong
    /// casing fails (no case-folding, no normalization).
    fn require_enum(
        field: &'static str,
        value: &str,
        allowed: &[&str],
    ) -> Result<(), EdgeCreateValidationError> {
        if allowed.contains(&value) {
            Ok(())
        } else {
            Err(EdgeCreateValidationError::InvalidEnumValue {
                field,
                value: value.to_string(),
            })
        }
    }

    /// Reject a value that is not a valid UUID (any form `uuid::Uuid` accepts).
    fn require_uuid(field: &'static str, value: &str) -> Result<(), EdgeCreateValidationError> {
        Uuid::parse_str(value)
            .map(|_| ())
            .map_err(|_| EdgeCreateValidationError::InvalidUuid {
                field,
                value: value.to_string(),
            })
    }

    impl CreateEdgeRequest {
        /// Validate into a `ValidatedCreateEdge` without mutating values.
        ///
        /// Pure: no persistence, no UUID generation, no timestamping. Order:
        /// (1) required fields non-blank, (2) `id`/`source_id`/`target_id` UUID
        /// format, (3) `edge_kind`/`source_type`/`target_type` exact enum
        /// members, (4) `note` non-blank and ≤ 1000 characters when present.
        /// `id` stays optional so the server can generate it; when present it
        /// must be a UUID.
        pub(super) fn validate(self) -> Result<ValidatedCreateEdge, EdgeCreateValidationError> {
            // (1) non-blank required fields.
            require_non_blank("source_id", &self.source_id)?;
            require_non_blank("target_id", &self.target_id)?;
            require_non_blank("edge_kind", &self.edge_kind)?;
            require_non_blank("source_type", &self.source_type)?;
            require_non_blank("target_type", &self.target_type)?;
            if let Some(id) = &self.id {
                require_non_blank("id", id)?;
            }

            // (2) UUID format for id-like fields.
            require_uuid("source_id", &self.source_id)?;
            require_uuid("target_id", &self.target_id)?;
            if let Some(id) = &self.id {
                require_uuid("id", id)?;
            }
            if let Some(subject_id) = &self.faden_subject_id {
                require_uuid("faden_subject_id", subject_id)?;
            }
            if self.faden_type.is_some() != self.faden_subject_id.is_some() {
                return Err(EdgeCreateValidationError::IncompleteFadenMetadata);
            }

            // (3) enum-constrained fields (exact match, no case-folding).
            require_enum("edge_kind", &self.edge_kind, &EDGE_KIND_VALUES)?;
            require_enum(
                "source_type",
                &self.source_type,
                &EDGE_PARTICIPANT_TYPE_VALUES,
            )?;
            require_enum(
                "target_type",
                &self.target_type,
                &EDGE_PARTICIPANT_TYPE_VALUES,
            )?;

            // (4) note: optional, non-blank and within length when present.
            if let Some(note) = &self.note {
                require_non_blank("note", note)?;
                if note.chars().count() > EDGE_NOTE_MAX_LEN {
                    return Err(EdgeCreateValidationError::NoteTooLong);
                }
            }

            let operation_id = match self.operation_id {
                Some(value) => {
                    require_non_blank("operation_id", &value)?;
                    let parsed = Uuid::parse_str(&value).map_err(|_| {
                        EdgeCreateValidationError::InvalidUuid {
                            field: "operation_id",
                            value: value.clone(),
                        }
                    })?;
                    Some(parsed.to_string())
                }
                None => None,
            };

            Ok(ValidatedCreateEdge {
                id: self.id,
                source_id: self.source_id,
                target_id: self.target_id,
                edge_kind: self.edge_kind,
                source_type: self.source_type,
                target_type: self.target_type,
                faden_type: self.faden_type,
                faden_subject_id: self.faden_subject_id,
                note: self.note,
                operation_id,
            })
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        // Valid UUIDs for contract-near fixtures: `id`, `source_id`, and
        // `target_id` are UUID-formatted per the domain contract.
        const EDGE_ID: &str = "00000000-0000-0000-0000-000000000001";
        const SOURCE_ID: &str = "00000000-0000-0000-0000-000000000002";
        const TARGET_ID: &str = "00000000-0000-0000-0000-000000000003";

        fn valid_request() -> CreateEdgeRequest {
            CreateEdgeRequest {
                id: None,
                source_id: SOURCE_ID.to_string(),
                target_id: TARGET_ID.to_string(),
                edge_kind: "reference".to_string(),
                source_type: "node".to_string(),
                target_type: "account".to_string(),
                faden_type: None,
                faden_subject_id: None,
                note: None,
                operation_id: None,
            }
        }

        // ---- positive: accepted shapes ----

        #[test]
        fn edge_create_request_accepts_minimal_payload() {
            // "Minimal" now carries the required participant types.
            let json = format!(
                r#"{{"source_id":"{SOURCE_ID}","source_type":"node","target_id":"{TARGET_ID}","target_type":"account","edge_kind":"reference"}}"#
            );
            let req = serde_json::from_str::<CreateEdgeRequest>(&json)
                .expect("minimal request must deserialize");
            assert_eq!(
                req.validate(),
                Ok(ValidatedCreateEdge {
                    id: None,
                    source_id: SOURCE_ID.to_string(),
                    target_id: TARGET_ID.to_string(),
                    edge_kind: "reference".to_string(),
                    source_type: "node".to_string(),
                    target_type: "account".to_string(),
                    faden_type: None,
                    faden_subject_id: None,
                    note: None,
                    operation_id: None,
                })
            );
        }

        #[test]
        fn edge_create_request_accepts_typed_faden_pair() {
            let mut req = valid_request();
            req.faden_type = Some(super::super::FadenType::Conversation);
            req.faden_subject_id = Some(TARGET_ID.to_string());

            let validated = req.validate().expect("typed Faden pair must validate");
            assert_eq!(
                validated.faden_type,
                Some(super::super::FadenType::Conversation)
            );
            assert_eq!(validated.faden_subject_id.as_deref(), Some(TARGET_ID));
        }

        #[test]
        fn edge_create_request_rejects_null_faden_type() {
            let json = format!(
                r#"{{"source_id":"{SOURCE_ID}","source_type":"node","target_id":"{TARGET_ID}","target_type":"account","edge_kind":"reference","faden_type":null}}"#
            );
            assert!(serde_json::from_str::<CreateEdgeRequest>(&json).is_err());
        }

        #[test]
        fn edge_create_request_rejects_incomplete_typed_faden_pair() {
            let mut missing_subject = valid_request();
            missing_subject.faden_type = Some(super::super::FadenType::Vote);
            assert_eq!(
                missing_subject.validate(),
                Err(EdgeCreateValidationError::IncompleteFadenMetadata)
            );

            let mut missing_type = valid_request();
            missing_type.faden_subject_id = Some(TARGET_ID.to_string());
            assert_eq!(
                missing_type.validate(),
                Err(EdgeCreateValidationError::IncompleteFadenMetadata)
            );
        }

        #[test]
        fn edge_create_request_accepts_full_payload() {
            let json = format!(
                r#"{{"id":"{EDGE_ID}","source_id":"{SOURCE_ID}","target_id":"{TARGET_ID}","edge_kind":"ownership","source_type":"node","target_type":"account","note":"a note"}}"#
            );
            let req = serde_json::from_str::<CreateEdgeRequest>(&json)
                .expect("full request must deserialize");
            assert_eq!(
                req.validate(),
                Ok(ValidatedCreateEdge {
                    id: Some(EDGE_ID.to_string()),
                    source_id: SOURCE_ID.to_string(),
                    target_id: TARGET_ID.to_string(),
                    edge_kind: "ownership".to_string(),
                    source_type: "node".to_string(),
                    target_type: "account".to_string(),
                    faden_type: None,
                    faden_subject_id: None,
                    note: Some("a note".to_string()),
                    operation_id: None,
                })
            );
        }

        #[test]
        fn edge_create_request_validates_edge_kind_enum() {
            for kind in EDGE_KIND_VALUES {
                let mut req = valid_request();
                req.edge_kind = kind.to_string();
                assert!(
                    req.validate().is_ok(),
                    "edge_kind `{kind}` must be accepted"
                );
            }

            let mut req = valid_request();
            req.edge_kind = "frobnicate".to_string();
            assert_eq!(
                req.validate(),
                Err(EdgeCreateValidationError::InvalidEnumValue {
                    field: "edge_kind",
                    value: "frobnicate".to_string(),
                })
            );
        }

        // ---- source_type / target_type: required + enum (contract parity) ----

        #[test]
        fn edge_create_request_validates_source_and_target_type_enum() {
            for ty in EDGE_PARTICIPANT_TYPE_VALUES {
                let mut req = valid_request();
                req.source_type = ty.to_string();
                req.target_type = ty.to_string();
                let validated = req.validate().expect("participant type must be accepted");
                assert_eq!(validated.source_type, ty);
                assert_eq!(validated.target_type, ty);
            }
        }

        #[test]
        fn edge_create_request_rejects_missing_source_type() {
            let json = format!(
                r#"{{"source_id":"{SOURCE_ID}","target_id":"{TARGET_ID}","target_type":"account","edge_kind":"reference"}}"#
            );
            assert!(
                serde_json::from_str::<CreateEdgeRequest>(&json).is_err(),
                "source_type is required and must not be omitted"
            );
        }

        #[test]
        fn edge_create_request_rejects_missing_target_type() {
            let json = format!(
                r#"{{"source_id":"{SOURCE_ID}","source_type":"node","target_id":"{TARGET_ID}","edge_kind":"reference"}}"#
            );
            assert!(
                serde_json::from_str::<CreateEdgeRequest>(&json).is_err(),
                "target_type is required and must not be omitted"
            );
        }

        #[test]
        fn edge_create_request_rejects_null_source_type() {
            let json = format!(
                r#"{{"source_id":"{SOURCE_ID}","source_type":null,"target_id":"{TARGET_ID}","target_type":"account","edge_kind":"reference"}}"#
            );
            assert!(
                serde_json::from_str::<CreateEdgeRequest>(&json).is_err(),
                "source_type=null must be rejected"
            );
        }

        #[test]
        fn edge_create_request_rejects_null_target_type() {
            let json = format!(
                r#"{{"source_id":"{SOURCE_ID}","source_type":"node","target_id":"{TARGET_ID}","target_type":null,"edge_kind":"reference"}}"#
            );
            assert!(
                serde_json::from_str::<CreateEdgeRequest>(&json).is_err(),
                "target_type=null must be rejected"
            );
        }

        #[test]
        fn edge_create_request_rejects_blank_source_type() {
            let mut req = valid_request();
            req.source_type = "  ".to_string();
            assert_eq!(
                req.validate(),
                Err(EdgeCreateValidationError::MissingOrEmptyField(
                    "source_type"
                ))
            );
        }

        #[test]
        fn edge_create_request_rejects_blank_target_type() {
            let mut req = valid_request();
            req.target_type = "   ".to_string();
            assert_eq!(
                req.validate(),
                Err(EdgeCreateValidationError::MissingOrEmptyField(
                    "target_type"
                ))
            );
        }

        #[test]
        fn edge_create_request_rejects_invalid_source_type() {
            let mut req = valid_request();
            req.source_type = "group".to_string();
            assert_eq!(
                req.validate(),
                Err(EdgeCreateValidationError::InvalidEnumValue {
                    field: "source_type",
                    value: "group".to_string(),
                })
            );
        }

        #[test]
        fn edge_create_request_rejects_invalid_target_type() {
            let mut req = valid_request();
            req.target_type = "group".to_string();
            assert_eq!(
                req.validate(),
                Err(EdgeCreateValidationError::InvalidEnumValue {
                    field: "target_type",
                    value: "group".to_string(),
                })
            );
        }

        // ---- id / source_id / target_id: UUID format ----

        #[test]
        fn edge_create_request_rejects_invalid_source_id_uuid() {
            let mut req = valid_request();
            req.source_id = "not-a-uuid".to_string();
            assert_eq!(
                req.validate(),
                Err(EdgeCreateValidationError::InvalidUuid {
                    field: "source_id",
                    value: "not-a-uuid".to_string(),
                })
            );
        }

        #[test]
        fn edge_create_request_rejects_invalid_target_id_uuid() {
            let mut req = valid_request();
            req.target_id = "not-a-uuid".to_string();
            assert_eq!(
                req.validate(),
                Err(EdgeCreateValidationError::InvalidUuid {
                    field: "target_id",
                    value: "not-a-uuid".to_string(),
                })
            );
        }

        #[test]
        fn edge_create_request_rejects_invalid_id_uuid_when_present() {
            let mut req = valid_request();
            req.id = Some("not-a-uuid".to_string());
            assert_eq!(
                req.validate(),
                Err(EdgeCreateValidationError::InvalidUuid {
                    field: "id",
                    value: "not-a-uuid".to_string(),
                })
            );
        }

        #[test]
        fn edge_create_request_allows_absent_id_for_server_generation() {
            let req = valid_request();
            assert_eq!(req.id, None);
            let validated = req.validate().expect("absent id must be accepted");
            assert_eq!(validated.id, None);
        }

        #[test]
        fn edge_create_request_accepts_valid_uuid_id_when_present() {
            let mut req = valid_request();
            req.id = Some(EDGE_ID.to_string());
            let validated = req.validate().expect("valid uuid id must be accepted");
            assert_eq!(validated.id.as_deref(), Some(EDGE_ID));
        }

        // ---- negative: deny_unknown_fields / silent-drop protection ----

        #[test]
        fn edge_create_request_rejects_expires_at_to_prevent_silent_drop() {
            let json = format!(
                r#"{{"source_id":"{SOURCE_ID}","source_type":"node","target_id":"{TARGET_ID}","target_type":"account","edge_kind":"reference","expires_at":"2026-01-01T00:00:00Z"}}"#
            );
            assert!(
                serde_json::from_str::<CreateEdgeRequest>(&json).is_err(),
                "expires_at must be rejected, never silently dropped"
            );
        }

        #[test]
        fn edge_create_request_rejects_created_at_because_server_owns_timestamp() {
            let json = format!(
                r#"{{"source_id":"{SOURCE_ID}","source_type":"node","target_id":"{TARGET_ID}","target_type":"account","edge_kind":"reference","created_at":"2026-01-01T00:00:00Z"}}"#
            );
            assert!(
                serde_json::from_str::<CreateEdgeRequest>(&json).is_err(),
                "created_at must be rejected; the server owns the create timestamp"
            );
        }

        #[test]
        fn edge_create_request_rejects_payload_and_metadata() {
            let payload = format!(
                r#"{{"source_id":"{SOURCE_ID}","source_type":"node","target_id":"{TARGET_ID}","target_type":"account","edge_kind":"reference","payload":{{}}}}"#
            );
            assert!(
                serde_json::from_str::<CreateEdgeRequest>(&payload).is_err(),
                "payload must be rejected"
            );
            let metadata = format!(
                r#"{{"source_id":"{SOURCE_ID}","source_type":"node","target_id":"{TARGET_ID}","target_type":"account","edge_kind":"reference","metadata":{{}}}}"#
            );
            assert!(
                serde_json::from_str::<CreateEdgeRequest>(&metadata).is_err(),
                "metadata must be rejected"
            );
        }

        #[test]
        fn edge_create_request_rejects_unknown_fields() {
            let json = format!(
                r#"{{"source_id":"{SOURCE_ID}","source_type":"node","target_id":"{TARGET_ID}","target_type":"account","edge_kind":"reference","wat":true}}"#
            );
            assert!(
                serde_json::from_str::<CreateEdgeRequest>(&json).is_err(),
                "unknown fields must be rejected, never silently ignored"
            );
        }

        // ---- negative: explicit null / non-string on optional fields ----
        //
        // `id` and `note` stay optional: absent is valid (proven by
        // edge_create_request_accepts_minimal_payload), but an explicit null or
        // a non-string value must fail rather than collapse to None.

        #[test]
        fn edge_create_request_rejects_null_id() {
            let json = format!(
                r#"{{"id":null,"source_id":"{SOURCE_ID}","source_type":"node","target_id":"{TARGET_ID}","target_type":"account","edge_kind":"reference"}}"#
            );
            assert!(
                serde_json::from_str::<CreateEdgeRequest>(&json).is_err(),
                "id=null must be rejected; omit id instead"
            );
        }

        #[test]
        fn edge_create_request_rejects_null_note() {
            let json = format!(
                r#"{{"source_id":"{SOURCE_ID}","source_type":"node","target_id":"{TARGET_ID}","target_type":"account","edge_kind":"reference","note":null}}"#
            );
            assert!(
                serde_json::from_str::<CreateEdgeRequest>(&json).is_err(),
                "note=null must be rejected; omit note instead"
            );
        }

        #[test]
        fn edge_create_request_rejects_non_string_optional_fields() {
            // Present optional fields must be strings; other JSON types fail.
            let numeric_id = format!(
                r#"{{"id":123,"source_id":"{SOURCE_ID}","source_type":"node","target_id":"{TARGET_ID}","target_type":"account","edge_kind":"reference"}}"#
            );
            assert!(
                serde_json::from_str::<CreateEdgeRequest>(&numeric_id).is_err(),
                "numeric id must be rejected"
            );
            let object_note = format!(
                r#"{{"source_id":"{SOURCE_ID}","source_type":"node","target_id":"{TARGET_ID}","target_type":"account","edge_kind":"reference","note":{{}}}}"#
            );
            assert!(
                serde_json::from_str::<CreateEdgeRequest>(&object_note).is_err(),
                "object note must be rejected"
            );
        }

        // ---- negative: required fields missing (serde level) ----

        #[test]
        fn edge_create_request_rejects_missing_source_id() {
            let json = format!(
                r#"{{"source_type":"node","target_id":"{TARGET_ID}","target_type":"account","edge_kind":"reference"}}"#
            );
            assert!(serde_json::from_str::<CreateEdgeRequest>(&json).is_err());
        }

        #[test]
        fn edge_create_request_rejects_missing_target_id() {
            let json = format!(
                r#"{{"source_id":"{SOURCE_ID}","source_type":"node","target_type":"account","edge_kind":"reference"}}"#
            );
            assert!(serde_json::from_str::<CreateEdgeRequest>(&json).is_err());
        }

        #[test]
        fn edge_create_request_rejects_missing_edge_kind() {
            let json = format!(
                r#"{{"source_id":"{SOURCE_ID}","source_type":"node","target_id":"{TARGET_ID}","target_type":"account"}}"#
            );
            assert!(serde_json::from_str::<CreateEdgeRequest>(&json).is_err());
        }

        // ---- negative: blank required / optional fields (validate level) ----

        #[test]
        fn edge_create_request_rejects_blank_source_id() {
            let mut req = valid_request();
            req.source_id = "   ".to_string();
            assert_eq!(
                req.validate(),
                Err(EdgeCreateValidationError::MissingOrEmptyField("source_id"))
            );
        }

        #[test]
        fn edge_create_request_rejects_blank_target_id() {
            let mut req = valid_request();
            req.target_id = String::new();
            assert_eq!(
                req.validate(),
                Err(EdgeCreateValidationError::MissingOrEmptyField("target_id"))
            );
        }

        #[test]
        fn edge_create_request_rejects_blank_edge_kind() {
            let mut req = valid_request();
            req.edge_kind = "  ".to_string();
            assert_eq!(
                req.validate(),
                Err(EdgeCreateValidationError::MissingOrEmptyField("edge_kind"))
            );
        }

        #[test]
        fn edge_create_request_rejects_blank_id_when_present() {
            let mut req = valid_request();
            req.id = Some("   ".to_string());
            assert_eq!(
                req.validate(),
                Err(EdgeCreateValidationError::MissingOrEmptyField("id"))
            );
        }

        #[test]
        fn edge_create_request_rejects_blank_note_when_present() {
            let mut req = valid_request();
            req.note = Some("   ".to_string());
            assert_eq!(
                req.validate(),
                Err(EdgeCreateValidationError::MissingOrEmptyField("note"))
            );
        }

        // ---- negative: wrong casing on enums (validate level) ----

        #[test]
        fn edge_create_request_rejects_uppercase_enum_values() {
            // Wrong casing must fail — no automatic lowercasing / normalization.
            let mut req = valid_request();
            req.edge_kind = "Reference".to_string();
            assert_eq!(
                req.validate(),
                Err(EdgeCreateValidationError::InvalidEnumValue {
                    field: "edge_kind",
                    value: "Reference".to_string(),
                })
            );

            let mut req = valid_request();
            req.source_type = "Node".to_string();
            assert_eq!(
                req.validate(),
                Err(EdgeCreateValidationError::InvalidEnumValue {
                    field: "source_type",
                    value: "Node".to_string(),
                })
            );

            let mut req = valid_request();
            req.target_type = "Account".to_string();
            assert_eq!(
                req.validate(),
                Err(EdgeCreateValidationError::InvalidEnumValue {
                    field: "target_type",
                    value: "Account".to_string(),
                })
            );
        }

        // ---- negative / boundary: note length ----

        #[test]
        fn edge_create_request_accepts_note_at_max_length() {
            let mut req = valid_request();
            req.note = Some("a".repeat(EDGE_NOTE_MAX_LEN));
            let validated = req
                .validate()
                .expect("note of exactly the max length must be accepted");
            assert_eq!(
                validated.note.map(|n| n.chars().count()),
                Some(EDGE_NOTE_MAX_LEN)
            );
        }

        #[test]
        fn edge_create_request_rejects_note_over_max_length() {
            let mut req = valid_request();
            req.note = Some("a".repeat(EDGE_NOTE_MAX_LEN + 1));
            assert_eq!(req.validate(), Err(EdgeCreateValidationError::NoteTooLong));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{
        append_edge_line, build_edge_record, edge_create::ValidatedCreateEdge,
        edge_has_valid_faden_metadata, edge_is_active_at, edge_is_permanently_unreachable,
        edge_matches_list_at, faden_expires_at, max_edges_cache_limit, projected_faden_expires_at,
        replace_edge_operation_line, replace_edge_operation_record, Edge, FadenType,
        LifecycleTimestamp, PublicEdge, DEFAULT_MAX_EDGES_CACHE, FADEN_LIFETIME_HOURS,
    };
    use crate::{domain_db::CreateOperationKey, test_helpers::EnvGuard};
    use chrono::{DateTime, Duration, TimeZone, Utc};
    use serde_json::json;
    use serial_test::serial;
    use std::{fs, sync::Arc};

    /// `expires_at: None` models an omitted key (legacy dated Fäden); use
    /// [`lifecycle_edge_with_null_expiry`] to model an explicit `null`.
    fn lifecycle_edge(created_at: Option<&str>, expires_at: Option<&str>) -> Edge {
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
            created_at: created_at.map(LifecycleTimestamp::from),
            expires_at: expires_at.map(|value| Some(LifecycleTimestamp::from(value))),
        }
    }

    /// Models a persisted record carrying an explicit `expires_at: null`,
    /// distinct from the omitted key that [`lifecycle_edge`] produces.
    fn lifecycle_edge_with_null_expiry(created_at: Option<&str>) -> Edge {
        let mut edge = lifecycle_edge(created_at, None);
        edge.expires_at = Some(None);
        edge
    }

    #[test]
    fn lifecycle_timestamp_preserves_wire_strings_and_invalid_legacy_values() {
        let valid: LifecycleTimestamp = serde_json::from_str("\"2026-07-17T10:00:00Z\"").unwrap();
        assert!(valid.parsed().is_some());
        assert_eq!(valid.as_str(), "2026-07-17T10:00:00Z");
        assert_eq!(
            serde_json::to_string(&valid).unwrap(),
            "\"2026-07-17T10:00:00Z\""
        );

        let invalid: LifecycleTimestamp = serde_json::from_str("\"yesterday\"").unwrap();
        assert!(invalid.parsed().is_none());
        assert_eq!(invalid.as_str(), "yesterday");
        assert_eq!(serde_json::to_string(&invalid).unwrap(), "\"yesterday\"");
    }

    #[test]
    fn new_faden_expires_exactly_after_168_hours_and_persists_both_timestamps() {
        let created_at = DateTime::parse_from_rfc3339("2026-07-17T10:00:00.123456789Z")
            .unwrap()
            .with_timezone(&Utc);
        assert_eq!(
            faden_expires_at(created_at),
            created_at + Duration::hours(FADEN_LIFETIME_HOURS)
        );

        let validated = ValidatedCreateEdge {
            id: None,
            source_id: "00000000-0000-0000-0000-0000000000a1".to_string(),
            target_id: "00000000-0000-0000-0000-0000000000b1".to_string(),
            edge_kind: "reference".to_string(),
            source_type: "node".to_string(),
            target_type: "account".to_string(),
            faden_type: None,
            faden_subject_id: None,
            note: None,
            operation_id: None,
        };
        let (edge, record) = build_edge_record(
            validated,
            "00000000-0000-0000-0000-0000000000e1".to_string(),
            created_at,
        );
        assert_eq!(
            edge.created_at.as_deref(),
            Some("2026-07-17T10:00:00.123456Z")
        );
        assert_eq!(
            edge.expires_at.clone().flatten().as_deref(),
            Some("2026-07-24T10:00:00.123456Z")
        );
        assert_eq!(
            record.get("created_at"),
            Some(&serde_json::json!(edge.created_at))
        );
        assert_eq!(
            record.get("expires_at"),
            Some(&serde_json::json!(edge.expires_at.clone().flatten()))
        );
        let round_trip: Edge = serde_json::from_value(record).expect("JSONL round trip");
        assert_eq!(round_trip.expires_at, edge.expires_at);
    }

    #[test]
    fn reactivation_keeps_identity_and_semantics_but_restarts_exact_lifecycle() {
        let validated = ValidatedCreateEdge {
            id: None,
            source_id: "00000000-0000-0000-0000-0000000000a1".to_string(),
            target_id: "00000000-0000-0000-0000-0000000000b1".to_string(),
            edge_kind: "reference".to_string(),
            source_type: "account".to_string(),
            target_type: "node".to_string(),
            faden_type: Some(FadenType::Conversation),
            faden_subject_id: Some("11111111-1111-5111-8111-111111111111".to_string()),
            note: None,
            operation_id: None,
        };
        let id = "00000000-0000-0000-0000-0000000000e1".to_string();
        let first_at = Utc.with_ymd_and_hms(2026, 7, 17, 10, 0, 0).unwrap();
        let second_at = Utc.with_ymd_and_hms(2026, 7, 20, 12, 30, 0).unwrap();
        let (first, _) = build_edge_record(validated.clone(), id.clone(), first_at);
        let (second, _) = build_edge_record(validated, id, second_at);

        assert_eq!(second.id, first.id);
        assert_eq!(second.source_id, first.source_id);
        assert_eq!(second.target_id, first.target_id);
        assert_eq!(second.faden_type, first.faden_type);
        assert_eq!(second.faden_subject_id, first.faden_subject_id);
        assert_ne!(second.created_at, first.created_at);
        assert_eq!(
            second.created_at.as_deref(),
            Some("2026-07-20T12:30:00.000000Z")
        );
        assert_eq!(
            second.expires_at.flatten().as_deref(),
            Some("2026-07-27T12:30:00.000000Z")
        );
    }

    fn operation_key() -> CreateOperationKey {
        CreateOperationKey {
            actor_id: "00000000-0000-0000-0000-0000000000a1".to_string(),
            operation_id: "22222222-2222-4222-8222-222222222222".to_string(),
        }
    }

    fn operation_record(id: &str, created_at: &str) -> serde_json::Value {
        let operation = operation_key();
        json!({
            "id": id,
            "source_id": operation.actor_id,
            "source_type": "account",
            "target_id": "00000000-0000-0000-0000-0000000000b1",
            "target_type": "node",
            "edge_kind": "reference",
            "faden_type": "conversation",
            "faden_subject_id": "11111111-1111-5111-8111-111111111111",
            "created_at": created_at,
            "expires_at": "2026-07-27T12:30:00Z",
            "_create_actor_id": operation.actor_id,
            "_create_operation_id": operation.operation_id,
        })
    }

    #[test]
    fn jsonl_reactivation_replaces_one_record_and_preserves_unrelated_bytes() {
        let old = serde_json::to_vec(&operation_record(
            "00000000-0000-0000-0000-0000000000e1",
            "2026-07-17T10:00:00Z",
        ))
        .unwrap();
        let replacement = operation_record(
            "00000000-0000-0000-0000-0000000000e1",
            "2026-07-20T12:30:00Z",
        );
        let mut input = b"{malformed legacy line}\n".to_vec();
        input.extend_from_slice(&old);
        input.extend_from_slice(b"\r\n{\"id\":\"untouched\"}");

        let output = replace_edge_operation_record(&input, &replacement, &operation_key())
            .expect("replace one operation record");
        let encoded_replacement = serde_json::to_vec(&replacement).unwrap();
        let mut expected = b"{malformed legacy line}\n".to_vec();
        expected.extend_from_slice(&encoded_replacement);
        expected.extend_from_slice(b"\r\n{\"id\":\"untouched\"}");

        assert_eq!(output, expected);
    }

    #[test]
    fn jsonl_reactivation_fails_closed_on_missing_or_duplicate_operation() {
        let replacement = operation_record(
            "00000000-0000-0000-0000-0000000000e1",
            "2026-07-20T12:30:00Z",
        );
        let missing = replace_edge_operation_record(
            b"{\"id\":\"unrelated\"}\n",
            &replacement,
            &operation_key(),
        )
        .unwrap_err();
        assert_eq!(missing.kind(), std::io::ErrorKind::NotFound);

        let existing = serde_json::to_vec(&replacement).unwrap();
        let mut duplicate = existing.clone();
        duplicate.push(b'\n');
        duplicate.extend_from_slice(&existing);
        duplicate.push(b'\n');
        let duplicate_error =
            replace_edge_operation_record(&duplicate, &replacement, &operation_key()).unwrap_err();
        assert_eq!(duplicate_error.kind(), std::io::ErrorKind::InvalidData);
    }

    #[tokio::test]
    #[serial]
    async fn jsonl_edge_append_boundary_remains_safe_under_serialized_concurrency() {
        let tmp = tempfile::tempdir().expect("tmpdir");
        let in_dir = tmp.path().join("in");
        fs::create_dir_all(&in_dir).expect("create input directory");
        let policy = tmp.path().join("limits.yaml");
        fs::write(&policy, "max_nodes_jsonl_mb: 1\nmax_edges_jsonl_mb: 1\n")
            .expect("write limits policy");
        let _policy = EnvGuard::set(
            "POLICY_LIMITS_PATH",
            policy.to_str().expect("UTF-8 policy path"),
        );
        let _data = EnvGuard::set("GEWEBE_IN_DIR", in_dir.to_str().expect("UTF-8 input path"));

        let record = json!({"id": "boundary-edge"});
        let appended_bytes = serde_json::to_vec(&record).expect("record").len() + 1;
        let maximum_bytes = 1024 * 1024;
        let existing_bytes = maximum_bytes - appended_bytes;
        let mut existing = vec![b'x'; existing_bytes];
        *existing.last_mut().expect("non-empty fixture") = b'\n';
        fs::write(crate::utils::edges_path(), &existing).expect("write near-limit edges");

        let serialization = Arc::new(tokio::sync::Mutex::new(()));
        let first_lock = serialization.clone();
        let first_record = record.clone();
        let first = tokio::spawn(async move {
            let _guard = first_lock.lock().await;
            append_edge_line(&first_record).await
        });
        let second_lock = serialization;
        let second = tokio::spawn(async move {
            let _guard = second_lock.lock().await;
            append_edge_line(&record).await
        });
        let results = [
            first.await.expect("first task"),
            second.await.expect("second task"),
        ];

        assert_eq!(results.iter().filter(|result| result.is_ok()).count(), 1);
        assert_eq!(results.iter().filter(|result| result.is_err()).count(), 1);
        assert_eq!(
            fs::metadata(crate::utils::edges_path())
                .expect("edges metadata")
                .len(),
            1024 * 1024
        );
    }

    #[tokio::test]
    #[serial]
    async fn jsonl_edge_rewrite_rejects_growth_before_canonical_rename() {
        let tmp = tempfile::tempdir().expect("tmpdir");
        let in_dir = tmp.path().join("in");
        fs::create_dir_all(&in_dir).expect("create input directory");
        let policy = tmp.path().join("limits.yaml");
        fs::write(&policy, "max_nodes_jsonl_mb: 1\nmax_edges_jsonl_mb: 1\n")
            .expect("write limits policy");
        let _policy = EnvGuard::set(
            "POLICY_LIMITS_PATH",
            policy.to_str().expect("UTF-8 policy path"),
        );
        let _data = EnvGuard::set("GEWEBE_IN_DIR", in_dir.to_str().expect("UTF-8 input path"));

        let existing_record = operation_record(
            "00000000-0000-0000-0000-0000000000e1",
            "2026-07-17T10:00:00Z",
        );
        let first_line = serde_json::to_string(&existing_record).expect("edge record");
        let maximum_bytes = 1024 * 1024;
        let filler_len = maximum_bytes - first_line.len() - 2;
        let original = format!("{first_line}\n{}\n", "x".repeat(filler_len));
        fs::write(crate::utils::edges_path(), original.as_bytes()).expect("write boundary edges");
        let mut replacement = existing_record;
        replacement["note"] = json!("x".repeat(1_000));

        let error = replace_edge_operation_line(&replacement, &operation_key())
            .await
            .expect_err("growing rewrite must exceed cap");
        assert_eq!(error.kind(), std::io::ErrorKind::FileTooLarge);
        assert_eq!(
            fs::read(crate::utils::edges_path()).expect("canonical edges remain readable"),
            original.as_bytes()
        );
    }

    #[test]
    fn public_projection_derives_missing_legacy_expiry_without_mutating_storage() {
        let edge = lifecycle_edge(Some("2026-07-17T10:00:00Z"), None);
        let projected = PublicEdge::from(&edge);

        assert_eq!(
            projected.expires_at.as_deref(),
            Some("2026-07-24T10:00:00Z")
        );
        assert!(edge.expires_at.is_none());
    }

    #[test]
    fn legacy_projection_preserves_submicrosecond_timestamp_precision() {
        let precise = lifecycle_edge(Some("2026-07-17T10:00:00.123456789Z"), None);

        assert_eq!(
            projected_faden_expires_at(&precise).as_deref(),
            Some("2026-07-24T10:00:00.123456789Z")
        );
    }

    #[test]
    fn typed_faden_metadata_is_atomic_and_uuid_bound() {
        let mut edge = lifecycle_edge(Some("2026-07-01T00:00:00Z"), Some("2026-07-08T00:00:00Z"));
        assert!(edge_has_valid_faden_metadata(&edge));

        edge.faden_type = Some(FadenType::Conversation);
        assert!(!edge_has_valid_faden_metadata(&edge));

        edge.faden_subject_id = Some("not-a-uuid".to_string());
        assert!(!edge_has_valid_faden_metadata(&edge));

        edge.faden_subject_id = Some("11111111-1111-5111-8111-111111111111".to_string());
        assert!(edge_has_valid_faden_metadata(&edge));
    }

    #[test]
    fn active_projection_uses_exact_boundary_and_retroactive_legacy_expiry() {
        let edge = lifecycle_edge(Some("2026-07-17T10:00:00Z"), Some("2026-07-24T10:00:00Z"));
        let legacy_with_created_at = lifecycle_edge(Some("2026-07-17T10:00:00Z"), None);
        let before_creation = Utc.with_ymd_and_hms(2026, 7, 17, 9, 59, 59).unwrap();
        let created = Utc.with_ymd_and_hms(2026, 7, 17, 10, 0, 0).unwrap();
        let before_expiry = Utc.with_ymd_and_hms(2026, 7, 24, 9, 59, 59).unwrap();
        let at_expiry = Utc.with_ymd_and_hms(2026, 7, 24, 10, 0, 0).unwrap();
        assert!(!edge_is_active_at(&edge, before_creation));
        assert!(edge_is_active_at(&edge, created));
        assert!(edge_is_active_at(&edge, before_expiry));
        assert!(!edge_is_active_at(&edge, at_expiry));
        assert!(edge_is_active_at(&legacy_with_created_at, created));
        assert!(edge_is_active_at(&legacy_with_created_at, before_expiry));
        assert!(!edge_is_active_at(&legacy_with_created_at, at_expiry));
        assert!(edge_is_active_at(&lifecycle_edge(None, None), at_expiry));
    }

    #[test]
    fn malformed_or_noncanonical_lifecycle_fails_closed() {
        let now = Utc.with_ymd_and_hms(2026, 7, 18, 10, 0, 0).unwrap();
        assert!(!edge_is_active_at(
            &lifecycle_edge(Some("invalid"), Some("2026-07-24T10:00:00Z")),
            now
        ));
        assert!(!edge_is_active_at(
            &lifecycle_edge(Some("invalid"), None),
            now
        ));
        assert!(!edge_is_active_at(
            &lifecycle_edge(None, Some("2026-07-24T10:00:00Z")),
            now
        ));
        assert!(!edge_is_active_at(
            &lifecycle_edge(Some("2026-07-17T10:00:00Z"), Some("2026-07-25T10:00:00Z"),),
            now
        ));
    }

    /// A JSONL/PostgreSQL record whose stored `expires_at` is an explicit
    /// `null` — as opposed to the field simply being omitted — must not be
    /// treated as "no stored expiry, derive one". That noncanonical pairing
    /// is exactly what the domain schema rejects, so it has to fail closed
    /// instead of silently reusing the omitted-key derivation path.
    #[test]
    fn dated_edge_with_explicit_null_expiry_fails_closed() {
        let now = Utc.with_ymd_and_hms(2026, 7, 18, 10, 0, 0).unwrap();
        let dated_explicit_null = lifecycle_edge_with_null_expiry(Some("2026-07-17T10:00:00Z"));
        assert_eq!(dated_explicit_null.expires_at, Some(None));
        assert!(!edge_is_active_at(&dated_explicit_null, now));
        assert_eq!(projected_faden_expires_at(&dated_explicit_null), None);

        // Paired with an equally undated `created_at`, an explicit null
        // expiry remains the accepted fully-undated legacy state.
        assert!(edge_is_active_at(
            &lifecycle_edge_with_null_expiry(None),
            now
        ));
    }

    /// `edge_is_permanently_unreachable` must agree with `edge_is_active_at`
    /// on exactly which edges can never be active for any `now`. A loader
    /// that used a different rule could admit a permanently-invalid row into
    /// a fixed-size cache, wasting a slot a genuinely reachable edge needs.
    #[test]
    fn permanently_unreachable_matches_every_always_inactive_shape() {
        let now = Utc.with_ymd_and_hms(2026, 7, 18, 10, 0, 0).unwrap();

        let always_unreachable = [
            lifecycle_edge_with_null_expiry(Some("2026-07-17T10:00:00Z")),
            lifecycle_edge(Some("invalid"), Some("2026-07-24T10:00:00Z")),
            lifecycle_edge(Some("invalid"), None),
            lifecycle_edge(None, Some("2026-07-24T10:00:00Z")),
            lifecycle_edge(Some("2026-07-17T10:00:00Z"), Some("2026-07-25T10:00:00Z")),
        ];
        for edge in always_unreachable {
            assert!(edge_is_permanently_unreachable(&edge));
            assert!(!edge_is_active_at(&edge, now));
        }

        let reachable = [
            lifecycle_edge(None, None),
            lifecycle_edge_with_null_expiry(None),
            lifecycle_edge(Some("2026-07-17T10:00:00Z"), None),
            lifecycle_edge(Some("2026-07-17T10:00:00Z"), Some("2026-07-24T10:00:00Z")),
        ];
        for edge in reachable {
            assert!(!edge_is_permanently_unreachable(&edge));
        }
    }

    /// Undated legacy edges must project as an explicit JSON null/null pair
    /// (keys present, values null), not omit the fields. Clients and AJV
    /// couple the two nulls; omitted keys would look like "derive expires_at".
    #[test]
    fn public_undated_legacy_serializes_explicit_null_lifecycle_pair() {
        let edge = lifecycle_edge_with_null_expiry(None);
        let projected = PublicEdge::from(&edge);
        assert_eq!(projected.created_at, None);
        assert_eq!(projected.expires_at, None);

        let value = serde_json::to_value(&projected).expect("serialize public edge");
        assert_eq!(value.get("created_at"), Some(&serde_json::Value::Null));
        assert_eq!(value.get("expires_at"), Some(&serde_json::Value::Null));
        // Keys must be present; absence would violate the schema coupling.
        assert!(value
            .as_object()
            .expect("object")
            .contains_key("created_at"));
        assert!(value
            .as_object()
            .expect("object")
            .contains_key("expires_at"));
    }

    #[test]
    fn expiry_filter_precedes_pagination() {
        let now = Utc.with_ymd_and_hms(2026, 7, 24, 10, 0, 0).unwrap();
        let expired = lifecycle_edge(Some("2026-07-17T10:00:00Z"), Some("2026-07-24T10:00:00Z"));
        let mut active_one =
            lifecycle_edge(Some("2026-07-18T10:00:00Z"), Some("2026-07-25T10:00:00Z"));
        active_one.id = "active-one".to_string();
        let mut active_two =
            lifecycle_edge(Some("2026-07-19T10:00:00Z"), Some("2026-07-26T10:00:00Z"));
        active_two.id = "active-two".to_string();
        let edges = [expired, active_one, active_two];
        let page: Vec<_> = edges
            .iter()
            .filter(|edge| edge_matches_list_at(edge, None, None, now))
            .skip(1)
            .take(1)
            .map(|edge| edge.id.as_str())
            .collect();
        assert_eq!(page, vec!["active-two"]);
    }

    #[test]
    #[serial]
    fn max_edges_cache_invalid_falls_back_to_default() {
        let _env = EnvGuard::set("MAX_EDGES_CACHE", "not-a-number");

        assert_eq!(max_edges_cache_limit(), DEFAULT_MAX_EDGES_CACHE);
    }

    #[test]
    #[serial]
    fn max_edges_cache_absent_returns_default() {
        let _env = EnvGuard::unset("MAX_EDGES_CACHE");

        assert_eq!(max_edges_cache_limit(), DEFAULT_MAX_EDGES_CACHE);
    }
}
