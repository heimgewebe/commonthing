use super::{
    domain_write_guard::{
        reject_node_create_unless_writable, reject_node_patch_unless_writable,
        DOMAIN_READ_SOURCE_READ_ONLY, DOMAIN_READ_SOURCE_READ_ONLY_MESSAGE,
    },
    edges::{
        edge_create_persist_lock, edge_references_node_for_delete,
        edge_value_references_node_for_delete, EdgeEndpointCollisionEvidence,
    },
    health::{ensure_jsonl_size, load_policy_limits, PolicyLimits},
    query::{
        cursor_page, encode_cursor, parse_cursor_params, parse_usize_param, validate_cursor_limit,
        CursorPage, ListResponse, PageMeta, MAX_PAGE_SIZE,
    },
};
use crate::auth::role::Role;
use crate::config::{
    DomainAccountWriteSource, DomainEdgeWriteSource, DomainNodeWriteSource, DomainReadSource,
};
use crate::domain_db::{
    delete_node_with_edges_in_postgres_audited, domain_projection_version,
    insert_domain_node_and_faden_with_creator_limit, load_nodes_bbox_after_id_from_postgres,
    load_nodes_bbox_from_postgres, lock_node_faden_cache_publication,
    patch_node_in_postgres_with_projection_precommit, replace_node_in_postgres_audited,
    CreateOperationKey, NodeConversationDeleteEffect, NodeCreateError, NodeFadenCreateError,
    NodePatchInput, NodeWriteError,
};
use crate::middleware::auth::AuthContext;
use crate::node_mutation::{
    abort_jsonl_audit, commit_jsonl_audit, persist_postgres_audit, prepare_jsonl_audit,
    NodeMutationAudit, NodeMutationOperation,
};
use crate::state::{ApiState, OrderedCache};
use crate::utils::{edges_path, nodes_path};
use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    Extension, Json,
};
use serde::{Deserialize, Deserializer, Serialize};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::path::{Path as FsPath, PathBuf};
use std::sync::atomic::Ordering;
use tokio::{
    fs::{File, OpenOptions},
    io::{
        AsyncBufReadExt, AsyncReadExt, AsyncSeekExt, AsyncWriteExt, BufReader, BufWriter, SeekFrom,
    },
};
use uuid::Uuid;

fn policy_io_error(errors: Vec<String>) -> std::io::Error {
    std::io::Error::new(
        std::io::ErrorKind::InvalidData,
        format!("JSONL limits policy unavailable: {}", errors.join("; ")),
    )
}

async fn jsonl_policy_limits() -> std::io::Result<PolicyLimits> {
    load_policy_limits().await.map_err(policy_io_error)
}

fn jsonl_write_status(error: &std::io::Error) -> StatusCode {
    if error.kind() == std::io::ErrorKind::FileTooLarge {
        StatusCode::INSUFFICIENT_STORAGE
    } else {
        StatusCode::INTERNAL_SERVER_ERROR
    }
}

pub enum NodeMutationError {
    Status(StatusCode),
    DomainReadSourceReadOnly,
    Message(StatusCode, String),
    Problem {
        status: StatusCode,
        code: &'static str,
        message: &'static str,
    },
}

#[derive(Debug, Serialize)]
pub struct NodeDeleteResponse {
    pub node_id: String,
    pub node_state: &'static str,
    pub removed_edge_ids: Vec<String>,
    pub conversation: NodeDeleteConversationResponse,
}

#[derive(Debug, Serialize)]
#[serde(tag = "effect", rename_all = "snake_case")]
pub enum NodeDeleteConversationResponse {
    NotApplicable,
    DeletedEmpty,
    Archived {
        archive_id: String,
        archive_url: String,
    },
}

impl From<NodeConversationDeleteEffect> for NodeDeleteConversationResponse {
    fn from(effect: NodeConversationDeleteEffect) -> Self {
        match effect {
            NodeConversationDeleteEffect::DeletedEmpty => Self::DeletedEmpty,
            NodeConversationDeleteEffect::Archived { archive_id } => Self::Archived {
                archive_url: format!("/api/conversations/{archive_id}"),
                archive_id,
            },
        }
    }
}

impl IntoResponse for NodeMutationError {
    fn into_response(self) -> Response {
        match self {
            NodeMutationError::Status(status) => status.into_response(),
            NodeMutationError::DomainReadSourceReadOnly => {
                let body = format!(
                    "{DOMAIN_READ_SOURCE_READ_ONLY}: {DOMAIN_READ_SOURCE_READ_ONLY_MESSAGE}"
                );
                (StatusCode::CONFLICT, body).into_response()
            }
            NodeMutationError::Message(status, body) => (status, body).into_response(),
            NodeMutationError::Problem {
                status,
                code,
                message,
            } => (status, Json(json!({ "code": code, "message": message }))).into_response(),
        }
    }
}

#[derive(Clone, Copy, Debug)]
struct BBox {
    min_lng: f64,
    min_lat: f64,
    max_lng: f64,
    max_lat: f64,
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct Location {
    pub lat: f64,
    pub lon: f64,
}

#[derive(Serialize, Deserialize, Clone, Copy, Debug, PartialEq, Eq, Default)]
#[serde(rename_all = "snake_case")]
pub enum SearchVisibility {
    #[default]
    Public,
    Private,
    Hidden,
    Revoked,
}

impl SearchVisibility {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Public => "public",
            Self::Private => "private",
            Self::Hidden => "hidden",
            Self::Revoked => "revoked",
        }
    }
}

impl std::fmt::Display for SearchVisibility {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.as_str())
    }
}

impl std::str::FromStr for SearchVisibility {
    type Err = String;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s {
            "public" => Ok(Self::Public),
            "private" => Ok(Self::Private),
            "hidden" => Ok(Self::Hidden),
            "revoked" => Ok(Self::Revoked),
            other => Err(format!("invalid search_visibility: {}", other)),
        }
    }
}

impl sqlx::Type<sqlx::Postgres> for SearchVisibility {
    fn type_info() -> sqlx::postgres::PgTypeInfo {
        <&str as sqlx::Type<sqlx::Postgres>>::type_info()
    }
}

impl<'q> sqlx::Encode<'q, sqlx::Postgres> for SearchVisibility {
    fn encode_by_ref(
        &self,
        buf: &mut sqlx::postgres::PgArgumentBuffer,
    ) -> Result<sqlx::encode::IsNull, Box<dyn std::error::Error + Send + Sync>> {
        <&str as sqlx::Encode<sqlx::Postgres>>::encode_by_ref(&self.as_str(), buf)
    }
}

impl<'r> sqlx::Decode<'r, sqlx::Postgres> for SearchVisibility {
    fn decode(
        value: sqlx::postgres::PgValueRef<'r>,
    ) -> Result<Self, Box<dyn std::error::Error + Send + Sync>> {
        let s = <&str as sqlx::Decode<sqlx::Postgres>>::decode(value)?;
        s.parse::<SearchVisibility>().map_err(|e| e.into())
    }
}

pub fn normalize_account_id(s: Option<&str>) -> Option<String> {
    s.map(|v| v.trim())
        .filter(|v| !v.is_empty())
        .map(|v| v.to_string())
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct Node {
    pub id: String,
    pub kind: String,
    pub title: String,
    pub created_at: String,
    pub updated_at: String,
    /// True only when the durable source carried a valid original creation
    /// timestamp. This is runtime provenance and is never exposed or persisted.
    #[serde(skip)]
    pub has_authoritative_created_at: bool,
    /// Server-owned, immutable creator binding. Legacy records remain `None`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub created_by_account_id: Option<String>,
    #[serde(default)]
    pub search_visibility: SearchVisibility,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub summary: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub info: Option<String>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub tags: Vec<String>,
    /// Human-readable address. Optional for backward compatibility with
    /// records persisted before this field existed; `POST /nodes` requires it.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub address: Option<String>,
    pub location: Location,
}

#[derive(Serialize, Clone, Copy, Debug, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum NodeHistoryKind {
    Created,
    Updated,
}

#[derive(Serialize, Clone, Debug, PartialEq, Eq)]
pub struct NodeHistoryEvent {
    pub date: String,
    pub event: &'static str,
    pub kind: NodeHistoryKind,
}

#[derive(Serialize)]
pub struct NodeDetails {
    #[serde(flatten)]
    pub node: Node,
    /// Current public Garnrollen title for the immutable creator binding.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub created_by_account_current_title: Option<String>,
    /// Newest-first, typed timeline derived from the authoritative node dates.
    pub history: Vec<NodeHistoryEvent>,
}

fn node_history(node: &Node) -> Vec<NodeHistoryEvent> {
    let mut history = Vec::with_capacity(2);
    if node.updated_at != node.created_at {
        history.push(NodeHistoryEvent {
            date: node.updated_at.clone(),
            event: "Knoten aktualisiert.",
            kind: NodeHistoryKind::Updated,
        });
    }
    history.push(NodeHistoryEvent {
        date: node.created_at.clone(),
        event: "Knoten wurde im Gewebe verankert.",
        kind: NodeHistoryKind::Created,
    });
    history
}

fn deserialize_some<'de, T, D>(deserializer: D) -> Result<Option<T>, D::Error>
where
    T: Deserialize<'de>,
    D: Deserializer<'de>,
{
    Deserialize::deserialize(deserializer).map(Some)
}

/// Mirrors `contracts/domain/node.schema.json` (`info.maxLength`) for every
/// node write path, including partial updates.
const NODE_INFO_MAX_LEN: usize = 20_000;

#[derive(Deserialize)]
pub struct UpdateNode {
    #[serde(default, deserialize_with = "deserialize_some")]
    pub info: Option<Option<String>>,
    #[serde(default, deserialize_with = "deserialize_some")]
    pub search_visibility: Option<Option<SearchVisibility>>,
}

#[derive(Debug, PartialEq)]
enum NodePatchValidationError {
    InfoTooLong { max: usize },
}

impl UpdateNode {
    fn validate(&self) -> Result<(), NodePatchValidationError> {
        if let Some(Some(info)) = &self.info {
            if info.chars().count() > NODE_INFO_MAX_LEN {
                return Err(NodePatchValidationError::InfoTooLong {
                    max: NODE_INFO_MAX_LEN,
                });
            }
        }
        Ok(())
    }
}

fn node_patch_error_message(error: &NodePatchValidationError) -> String {
    match error {
        NodePatchValidationError::InfoTooLong { max } => {
            format!("info exceeds the maximum length of {max}")
        }
    }
}

#[cfg(test)]
mod node_patch_validation_tests {
    use super::{NodePatchValidationError, SearchVisibility, UpdateNode, NODE_INFO_MAX_LEN};

    #[test]
    fn accepts_absent_null_and_exact_limit_info() {
        for info in [None, Some(None), Some(Some("a".repeat(NODE_INFO_MAX_LEN)))] {
            let request = UpdateNode {
                info,
                search_visibility: Some(Some(SearchVisibility::Public)),
            };
            assert_eq!(request.validate(), Ok(()));
        }
    }

    #[test]
    fn rejects_info_over_limit() {
        let request = UpdateNode {
            info: Some(Some("a".repeat(NODE_INFO_MAX_LEN + 1))),
            search_visibility: None,
        };
        assert_eq!(
            request.validate(),
            Err(NodePatchValidationError::InfoTooLong {
                max: NODE_INFO_MAX_LEN,
            })
        );
    }
}

/// Lightweight struct for fast-path ID checking during node rewrites.
///
/// Used to check if a line matches the target node ID without fully parsing
/// the entire JSON `Value`. This avoids full deserialization for non-matching
/// lines during PATCH, PUT and DELETE preparation and keeps memory usage O(1).
#[derive(Deserialize)]
struct IdOnly {
    id: Option<String>,
}

fn jsonl_line_has_id(line: &str, target_id: &str) -> bool {
    serde_json::from_str::<IdOnly>(line)
        .ok()
        .and_then(|record| record.id)
        .as_deref()
        == Some(target_id)
}

#[derive(Deserialize)]
struct LocationDto {
    #[serde(deserialize_with = "deserialize_f64_or_string")]
    lat: f64,
    #[serde(deserialize_with = "deserialize_f64_or_string")]
    lon: f64,
}

const DEFAULT_KIND: &str = "Unknown";
const DEFAULT_TITLE: &str = "Untitled";

#[derive(Deserialize)]
struct NodeDto {
    id: String,
    #[serde(default, deserialize_with = "deserialize_opt_string_loose")]
    kind: Option<String>,
    #[serde(default, deserialize_with = "deserialize_opt_string_loose")]
    title: Option<String>,
    #[serde(default, deserialize_with = "deserialize_opt_string_loose")]
    created_at: Option<String>,
    #[serde(default, deserialize_with = "deserialize_opt_string_loose")]
    updated_at: Option<String>,
    #[serde(default, deserialize_with = "deserialize_opt_string_loose")]
    created_by_account_id: Option<String>,
    #[serde(default)]
    search_visibility: Option<SearchVisibility>,
    #[serde(default, deserialize_with = "deserialize_opt_string_loose")]
    summary: Option<String>,
    #[serde(default, deserialize_with = "deserialize_opt_string_loose")]
    info: Option<String>,
    #[serde(default, deserialize_with = "deserialize_tags_loose")]
    tags: Vec<String>,
    #[serde(default, deserialize_with = "deserialize_opt_string_loose")]
    address: Option<String>,
    location: LocationDto,
}

fn deserialize_opt_string_loose<'de, D>(deserializer: D) -> Result<Option<String>, D::Error>
where
    D: Deserializer<'de>,
{
    let v = Option::<Value>::deserialize(deserializer)?;
    Ok(v.and_then(|x| x.as_str().map(|s| s.to_string())))
}

fn deserialize_tags_loose<'de, D>(deserializer: D) -> Result<Vec<String>, D::Error>
where
    D: Deserializer<'de>,
{
    let v = Option::<Value>::deserialize(deserializer)?;
    match v {
        Some(Value::Array(arr)) => Ok(arr
            .into_iter()
            .filter_map(|x| x.as_str().map(|s| s.to_string()))
            .collect()),
        _ => Ok(Vec::new()),
    }
}

fn deserialize_f64_or_string<'de, D>(deserializer: D) -> Result<f64, D::Error>
where
    D: Deserializer<'de>,
{
    struct F64OrStringVisitor;

    impl<'de> serde::de::Visitor<'de> for F64OrStringVisitor {
        type Value = f64;

        fn expecting(&self, formatter: &mut std::fmt::Formatter) -> std::fmt::Result {
            formatter.write_str("a float or a string containing a float")
        }

        fn visit_f64<E>(self, v: f64) -> Result<Self::Value, E>
        where
            E: serde::de::Error,
        {
            Ok(v)
        }

        fn visit_i64<E>(self, v: i64) -> Result<Self::Value, E>
        where
            E: serde::de::Error,
        {
            Ok(v as f64)
        }

        fn visit_u64<E>(self, v: u64) -> Result<Self::Value, E>
        where
            E: serde::de::Error,
        {
            Ok(v as f64)
        }

        fn visit_str<E>(self, v: &str) -> Result<Self::Value, E>
        where
            E: serde::de::Error,
        {
            v.parse::<f64>().map_err(serde::de::Error::custom)
        }
    }

    deserializer.deserialize_any(F64OrStringVisitor)
}

impl From<NodeDto> for Node {
    fn from(dto: NodeDto) -> Self {
        let default_timestamp = "1970-01-01T00:00:00Z";
        let has_authoritative_created_at = dto
            .created_at
            .as_deref()
            .is_some_and(|timestamp| chrono::DateTime::parse_from_rfc3339(timestamp).is_ok());

        let created_at = dto
            .created_at
            .as_deref()
            .or(dto.updated_at.as_deref())
            .unwrap_or(default_timestamp)
            .to_string();
        let updated_at = dto
            .updated_at
            .as_deref()
            .or(dto.created_at.as_deref())
            .unwrap_or(default_timestamp)
            .to_string();

        Node {
            id: dto.id,
            kind: dto.kind.unwrap_or_else(|| DEFAULT_KIND.to_string()),
            title: dto.title.unwrap_or_else(|| DEFAULT_TITLE.to_string()),
            created_at,
            updated_at,
            has_authoritative_created_at,
            created_by_account_id: normalize_account_id(dto.created_by_account_id.as_deref()),
            search_visibility: dto.search_visibility.unwrap_or_default(),
            summary: dto.summary,
            info: dto.info,
            tags: dto.tags,
            address: dto.address,
            location: Location {
                lat: dto.location.lat,
                lon: dto.location.lon,
            },
        }
    }
}

fn parse_bbox(s: &str) -> Option<BBox> {
    let parts: Vec<_> = s.split(',').collect();
    let (lng1, lat1, lng2, lat2) = match parts.as_slice() {
        [lng1, lat1, lng2, lat2] => (
            lng1.trim().parse::<f64>().ok()?,
            lat1.trim().parse::<f64>().ok()?,
            lng2.trim().parse::<f64>().ok()?,
            lat2.trim().parse::<f64>().ok()?,
        ),
        _ => return None,
    };

    Some(BBox {
        min_lng: lng1.min(lng2),
        min_lat: lat1.min(lat2),
        max_lng: lng1.max(lng2),
        max_lat: lat1.max(lat2),
    })
}

fn point_in_bbox(lng: f64, lat: f64, bb: &BBox) -> bool {
    lng >= bb.min_lng && lng <= bb.max_lng && lat >= bb.min_lat && lat <= bb.max_lat
}

pub(crate) fn map_json_to_node(v: &Value) -> Option<Node> {
    let id = v
        .get("id")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string())?;

    // Parse location object with explicit error handling
    let location = v.get("location")?;
    let lon = location
        .get("lon")
        .and_then(|val| val.as_f64().or_else(|| val.as_str()?.parse().ok()))?;
    let lat = location
        .get("lat")
        .and_then(|val| val.as_f64().or_else(|| val.as_str()?.parse().ok()))?;

    let title = v
        .get("title")
        .and_then(|v| v.as_str())
        .unwrap_or(DEFAULT_TITLE)
        .to_string();
    let kind = v
        .get("kind")
        .and_then(|v| v.as_str())
        .unwrap_or(DEFAULT_KIND)
        .to_string();
    let created_at_raw = v.get("created_at").and_then(|v| v.as_str());
    let updated_at_raw = v.get("updated_at").and_then(|v| v.as_str());
    let default_timestamp = "1970-01-01T00:00:00Z";
    let has_authoritative_created_at = created_at_raw
        .is_some_and(|timestamp| chrono::DateTime::parse_from_rfc3339(timestamp).is_ok());

    let created_at = created_at_raw
        .or(updated_at_raw)
        .unwrap_or(default_timestamp)
        .to_string();
    let updated_at = updated_at_raw
        .or(created_at_raw)
        .unwrap_or(default_timestamp)
        .to_string();

    let created_by_account_id =
        normalize_account_id(v.get("created_by_account_id").and_then(|v| v.as_str()));

    let search_visibility = match v.get("search_visibility") {
        None => SearchVisibility::Public,
        Some(value) => serde_json::from_value::<SearchVisibility>(value.clone())
            .unwrap_or(SearchVisibility::Hidden),
    };

    let summary = v
        .get("summary")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string());

    let info = v
        .get("info")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string());

    let tags = v
        .get("tags")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|x| x.as_str().map(String::from))
                .collect()
        })
        .unwrap_or_default();

    let address = v
        .get("address")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string());

    Some(Node {
        id,
        kind,
        title,
        created_at,
        updated_at,
        has_authoritative_created_at,
        created_by_account_id,
        search_visibility,
        summary,
        info,
        tags,
        address,
        location: Location { lat, lon },
    })
}

/// Loads nodes from the JSONL file into memory.
///
/// **Architecture Note:**
/// The in-memory cache populated by this function is considered the "Source of Truth"
/// for read operations during the API process lifetime.
/// - The file is strictly used for persistence.
/// - `patch_node` updates both the file (for durability) and this cache (for consistency).
/// - External modifications to the nodes file (e.g. via deployment or manual edit)
///   will NOT be detected until the API process is restarted.
pub async fn load_nodes() -> std::io::Result<OrderedCache<Node>> {
    let start = std::time::Instant::now();
    let path = nodes_path();
    let file = match File::open(&path).await {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(OrderedCache::new());
        }
        Err(error) => return Err(error),
    };
    let mut lines = BufReader::new(file).lines();
    let mut nodes = OrderedCache::new();
    let mut duplicates_count = 0;
    let mut line_number = 0usize;

    while let Some(line) = lines.next_line().await? {
        line_number += 1;
        let dto: NodeDto = serde_json::from_str(&line).map_err(|error| {
            std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!(
                    "invalid node JSONL at {} line {line_number}: {error}",
                    path.display()
                ),
            )
        })?;
        let node: Node = dto.into();
        if nodes.insert(node.id.clone(), node) {
            // Last-write-wins: Overwrite existing node
            duplicates_count += 1;
        }
    }

    let load_ms = start.elapsed().as_millis();
    let file_size_bytes = tokio::fs::metadata(&path)
        .await
        .map(|m| m.len())
        .unwrap_or(0);

    tracing::info!(
        count = nodes.len(),
        duplicates_count,
        load_ms,
        file_size_bytes,
        ?path,
        "Loaded nodes into memory cache"
    );
    Ok(nodes)
}

pub async fn get_node(
    State(state): State<ApiState>,
    Path(id): Path<String>,
) -> Result<Json<NodeDetails>, StatusCode> {
    // Clone before reading accounts so this endpoint never holds several state
    // locks at once. The projection middleware keeps both caches on the same
    // generation; BBOX-specific PostgreSQL reads must not bypass that contract.
    let node = state
        .nodes
        .read()
        .await
        .get(&id)
        .cloned()
        .ok_or(StatusCode::NOT_FOUND)?;
    let created_by_account_current_title = match node.created_by_account_id.as_deref() {
        Some(account_id) => state
            .accounts
            .read()
            .await
            .public_display_title(account_id)
            .map(str::to_owned),
        None => None,
    };

    let history = node_history(&node);

    Ok(Json(NodeDetails {
        node,
        created_by_account_current_title,
        history,
    }))
}

/// Append a single node record as a JSONL line. Durability via fsync.
/// Callers MUST hold `state.nodes_persist` to serialize writes.
///
/// If the existing file does not end with a newline (e.g. a hand-written or
/// truncated fixture), a separator newline is written first so the previous
/// record and the new record are never glued into one unparseable line.
async fn append_node_line(record: &Value) -> std::io::Result<()> {
    let maximum_bytes = jsonl_policy_limits().await?.max_nodes_jsonl_bytes();
    let path = nodes_path();
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
        .map_err(|_| std::io::Error::other("node JSONL record length exceeds u64"))?;
    let resulting_bytes = len
        .checked_add(separator_bytes)
        .and_then(|bytes| bytes.checked_add(line_bytes))
        .and_then(|bytes| bytes.checked_add(1))
        .ok_or_else(|| std::io::Error::other("node JSONL resulting size overflow"))?;
    ensure_jsonl_size("nodes", resulting_bytes, maximum_bytes)?;

    if separator_bytes == 1 {
        file.write_all(b"\n").await?;
    }

    file.write_all(line.as_bytes()).await?;
    file.write_all(b"\n").await?;
    file.flush().await?;
    file.sync_all().await?;
    Ok(())
}

const CREATE_ACTOR_KEY: &str = "_create_actor_id";
const CREATE_OPERATION_KEY: &str = "_create_operation_id";

fn add_create_operation_metadata(record: &mut Value, operation: Option<&CreateOperationKey>) {
    let Some(operation) = operation else {
        return;
    };
    let object = record
        .as_object_mut()
        .expect("canonical node create record must be an object");
    object.insert(
        CREATE_ACTOR_KEY.to_string(),
        Value::String(operation.actor_id.clone()),
    );
    object.insert(
        CREATE_OPERATION_KEY.to_string(),
        Value::String(operation.operation_id.clone()),
    );
}

#[derive(Deserialize)]
struct NodeOperationRecord {
    #[serde(flatten)]
    node: NodeDto,
    #[serde(default, rename = "_create_actor_id")]
    actor_id: Option<Value>,
    #[serde(default, rename = "_create_operation_id")]
    operation_id: Option<Value>,
}

/// Find an earlier durable JSONL result for one account-scoped operation.
/// Every record is validated through the same `NodeDto` contract as startup,
/// so a create cannot extend a canonical file that the next restart rejects.
/// Operation metadata remains invisible to the public `Node` projection.
async fn find_node_by_operation(operation: &CreateOperationKey) -> std::io::Result<Option<Node>> {
    let path = nodes_path();
    let file = match File::open(&path).await {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    let mut lines = BufReader::new(file).lines();
    let mut found = None;
    let mut line_number = 0usize;
    while let Some(line) = lines.next_line().await? {
        line_number += 1;
        let record: NodeOperationRecord = serde_json::from_str(&line).map_err(|error| {
            std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!(
                    "invalid node JSONL at {} line {line_number}: {error}",
                    path.display()
                ),
            )
        })?;
        let actor_matches =
            record.actor_id.as_ref().and_then(Value::as_str) == Some(operation.actor_id.as_str());
        let operation_matches = record.operation_id.as_ref().and_then(Value::as_str)
            == Some(operation.operation_id.as_str());
        if !actor_matches || !operation_matches {
            continue;
        }
        if found.is_some() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "duplicate node create operation metadata",
            ));
        }
        found = Some(record.node.into());
    }
    Ok(found)
}

fn node_matches_create(node: &Node, expected: &node_create::ValidatedCreateNode) -> bool {
    node.title == expected.title
        && node.kind == expected.kind
        && node.search_visibility == expected.search_visibility.unwrap_or_default()
        && node.address.as_deref() == Some(expected.address.as_str())
        && node.location.lat == expected.lat
        && node.location.lon == expected.lon
        && node.summary == expected.summary
        && node.info == expected.info
        && node.tags == expected.tags
}

/// Build the in-memory `Node` and its canonical JSONL record from a validated
/// create request plus the server-owned `id` and timestamp.
fn build_node_record(
    validated: node_create::ValidatedCreateNode,
    id: String,
    now: String,
    created_by_account_id: String,
) -> (Node, Value) {
    let node = Node {
        id,
        kind: validated.kind,
        title: validated.title,
        created_at: now.clone(),
        updated_at: now,
        has_authoritative_created_at: true,
        created_by_account_id: Some(created_by_account_id),
        search_visibility: validated.search_visibility.unwrap_or_default(),
        summary: validated.summary,
        info: validated.info,
        tags: validated.tags,
        address: Some(validated.address),
        location: Location {
            lat: validated.lat,
            lon: validated.lon,
        },
    };

    let mut record = serde_json::Map::new();
    record.insert("id".into(), json!(node.id));
    record.insert("kind".into(), json!(node.kind));
    record.insert("title".into(), json!(node.title));
    record.insert("created_at".into(), json!(node.created_at));
    record.insert("updated_at".into(), json!(node.updated_at));
    if let Some(created_by_account_id) = &node.created_by_account_id {
        record.insert("created_by_account_id".into(), json!(created_by_account_id));
    }
    record.insert(
        "search_visibility".into(),
        json!(node.search_visibility.as_str()),
    );
    if let Some(summary) = &node.summary {
        record.insert("summary".into(), json!(summary));
    }
    if let Some(info) = &node.info {
        record.insert("info".into(), json!(info));
    }
    if !node.tags.is_empty() {
        record.insert("tags".into(), json!(node.tags));
    }
    if let Some(address) = &node.address {
        record.insert("address".into(), json!(address));
    }
    record.insert(
        "location".into(),
        json!({ "lat": node.location.lat, "lon": node.location.lon }),
    );

    (node, Value::Object(record))
}

/// Map a `NodeCreateValidationError` onto a stable message for the 400 body.
fn node_create_error_message(err: &node_create::NodeCreateValidationError) -> String {
    use node_create::NodeCreateValidationError as E;
    match err {
        E::MissingOrEmptyField(field) => format!("missing or empty field: {field}"),
        E::FieldTooLong { field, max } => format!("{field} exceeds the maximum length of {max}"),
        E::InvalidCoordinate { field } => {
            format!("{field} must be a finite number within world bounds")
        }
        E::InvalidUuid { field, value } => format!("invalid UUID for {field}: {value}"),
        E::TooManyTags { max } => format!("tags exceeds the maximum count of {max}"),
    }
}

fn node_activity_faden_operation_id(
    faden_type: super::edges::FadenType,
    account_id: &str,
    node_id: &str,
    subject_id: &str,
) -> String {
    fn component(value: &str) -> String {
        format!("{}:{value}", value.len())
    }

    let name = format!(
        "weltgewebe:node-participation-faden:v2|{}|{}|{}|{}",
        component(faden_type.as_str()),
        component(account_id),
        component(node_id),
        component(subject_id),
    );
    Uuid::new_v5(&Uuid::NAMESPACE_URL, name.as_bytes()).to_string()
}

/// Project recent participation as an expiring account-to-node Faden.
/// Each account gets at most one active Faden of a semantic type per target
/// object. Later messages or edits replay that relation instead of drawing
/// parallel lines; `activity_id` remains diagnostic evidence only.
///
/// The strict edge contract only admits UUID endpoints. Older imported nodes
/// and accounts can still carry historical text ids; participation on those
/// entities must remain available, so the derived projection is skipped with a
/// structured warning instead of turning a successful domain write into a 500.
pub(crate) async fn ensure_node_activity_faden(
    state: &ApiState,
    auth: &AuthContext,
    node_id: &str,
    faden_type: super::edges::FadenType,
    subject_id: &str,
    activity_id: &str,
) -> Result<(), (StatusCode, String)> {
    project_node_activity_faden(
        state,
        auth,
        node_id,
        faden_type,
        subject_id,
        activity_id,
        super::edges::FadenProjectionMode::Reactivate,
    )
    .await
}

/// Repair an absent projection for an exact durable-action replay without
/// pretending that the replay itself was new participation.
pub(crate) async fn repair_node_activity_faden(
    state: &ApiState,
    auth: &AuthContext,
    node_id: &str,
    faden_type: super::edges::FadenType,
    subject_id: &str,
    activity_id: &str,
) -> Result<(), (StatusCode, String)> {
    project_node_activity_faden(
        state,
        auth,
        node_id,
        faden_type,
        subject_id,
        activity_id,
        super::edges::FadenProjectionMode::EnsureOnly,
    )
    .await
}

async fn project_node_activity_faden(
    state: &ApiState,
    auth: &AuthContext,
    node_id: &str,
    faden_type: super::edges::FadenType,
    subject_id: &str,
    activity_id: &str,
    projection_mode: super::edges::FadenProjectionMode,
) -> Result<(), (StatusCode, String)> {
    super::collective_write_guard::run_node_activity_projection(
        state,
        node_id,
        ensure_node_activity_faden_guarded_with_mode(
            state,
            auth,
            node_id,
            faden_type,
            subject_id,
            activity_id,
            projection_mode,
        ),
    )
    .await
}

pub(crate) async fn ensure_node_activity_faden_guarded(
    state: &ApiState,
    auth: &AuthContext,
    node_id: &str,
    faden_type: super::edges::FadenType,
    subject_id: &str,
    activity_id: &str,
) -> Result<(), (StatusCode, String)> {
    ensure_node_activity_faden_guarded_with_mode(
        state,
        auth,
        node_id,
        faden_type,
        subject_id,
        activity_id,
        super::edges::FadenProjectionMode::Reactivate,
    )
    .await
}

async fn ensure_node_activity_faden_guarded_with_mode(
    state: &ApiState,
    auth: &AuthContext,
    node_id: &str,
    faden_type: super::edges::FadenType,
    subject_id: &str,
    activity_id: &str,
    projection_mode: super::edges::FadenProjectionMode,
) -> Result<(), (StatusCode, String)> {
    let account_id = auth.account_id.as_deref().ok_or_else(|| {
        (
            StatusCode::UNAUTHORIZED,
            "authenticated account context missing".to_string(),
        )
    })?;
    if Uuid::parse_str(account_id).is_err()
        || Uuid::parse_str(node_id).is_err()
        || Uuid::parse_str(subject_id).is_err()
    {
        tracing::warn!(
            event = "node.faden_projection.skipped_legacy_identifier",
            node_id,
            account_id,
            faden_type = faden_type.as_str(),
            subject_id,
            activity_id,
            "Recent participation cannot be represented by the UUID-only Faden contract"
        );
        return Ok(());
    }

    ensure_node_faden_with_operation_id(
        state,
        auth,
        node_id,
        false,
        NodeFadenProjection {
            faden_type,
            subject_id,
            mode: projection_mode,
        },
    )
    .await
}

/// Ensure the Faden that visualizes one successful Webungsaktion.
///
/// The edge writer remains an internal projection primitive. Its HTTP route is
/// intentionally absent. The deterministic relation id — account, Fadenart,
/// drawable node and semantic subject — keeps retries idempotent and prevents
/// create/edit from drawing parallel Knüpffäden. The projection mode controls
/// whether an existing lifecycle remains unchanged or is restarted after
/// genuine new participation.
struct NodeFadenProjection<'a> {
    faden_type: super::edges::FadenType,
    subject_id: &'a str,
    mode: super::edges::FadenProjectionMode,
}

async fn ensure_node_faden_with_operation_id(
    state: &ApiState,
    auth: &AuthContext,
    node_id: &str,
    account_lifecycle_guarded: bool,
    projection: NodeFadenProjection<'_>,
) -> Result<(), (StatusCode, String)> {
    let account_id = auth.account_id.as_deref().ok_or_else(|| {
        (
            StatusCode::UNAUTHORIZED,
            "authenticated account context missing".to_string(),
        )
    })?;
    // When the caller owns the account-lifecycle advisory lock, guest exit is
    // already excluded across the complete Node -> Faden operation. Legacy
    // callers without that guard retain the account-row protection, but only
    // when accounts and edges are canonically PostgreSQL-backed.
    let needs_postgres_account_guard = !account_lifecycle_guarded
        && state.config.domain_read_source == DomainReadSource::Postgres
        && state.config.domain_account_write_source == DomainAccountWriteSource::Postgres
        && state.config.domain_edge_write_source == DomainEdgeWriteSource::Postgres;

    // Legacy callers that do not own the wider account-lifecycle advisory lock
    // still keep the canonical account row locked until the derived Faden is
    // durable. The regular PostgreSQL node-create path uses the wider guard and
    // therefore does not rely on this narrower fallback for exit serialization.
    let mut account_guard = if needs_postgres_account_guard {
        let pool = state.db_pool.as_ref().ok_or_else(|| {
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                "PostgreSQL pool unavailable for node Faden account guard".to_string(),
            )
        })?;
        let mut tx = pool.begin().await.map_err(|error| {
            tracing::error!(%error, account_id = %account_id, node_id, faden_type = projection.faden_type.as_str(), "failed to begin node Faden account guard");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                "failed to guard creator account for derived Faden".to_string(),
            )
        })?;
        let active_account: Option<String> = sqlx::query_scalar(
            "SELECT id FROM domain_accounts WHERE id = $1 AND disabled = FALSE FOR UPDATE",
        )
        .bind(account_id)
        .fetch_optional(&mut *tx)
        .await
        .map_err(|error| {
            tracing::error!(%error, account_id = %account_id, node_id, faden_type = projection.faden_type.as_str(), "failed to lock creator account for node Faden");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                "failed to guard creator account for derived Faden".to_string(),
            )
        })?;
        if active_account.is_none() {
            return Err((
                StatusCode::UNAUTHORIZED,
                "creator account is no longer active".to_string(),
            ));
        }
        Some(tx)
    } else {
        None
    };

    let projection_operation_id = node_activity_faden_operation_id(
        projection.faden_type,
        account_id,
        node_id,
        projection.subject_id,
    );
    let payload = json!({
        "source_id": account_id,
        "source_type": "account",
        "target_id": node_id,
        "target_type": "node",
        "edge_kind": "reference",
        "faden_type": projection.faden_type,
        "faden_subject_id": projection.subject_id,
        "operation_id": projection_operation_id,
    });

    match projection.mode {
        super::edges::FadenProjectionMode::EnsureOnly => {
            super::edges::create_edge(State(state.clone()), Extension(auth.clone()), Json(payload))
                .await
        }
        super::edges::FadenProjectionMode::Reactivate => {
            super::edges::reactivate_edge(
                State(state.clone()),
                Extension(auth.clone()),
                Json(payload),
            )
            .await
        }
    }
    .map(|_| ())
    .map_err(|(status, message)| {
        tracing::error!(
            event = "node.faden_projection.failed",
            node_id,
            account_id = %account_id,
            faden_type = projection.faden_type.as_str(),
            faden_subject_id = projection.subject_id,
            %status,
            error = %message,
            "Node is durable but its derived Faden projection failed"
        );
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            "derived Faden could not be projected; retry the same node operation".to_string(),
        )
    })?;

    if let Some(tx) = account_guard.take() {
        tx.commit().await.map_err(|error| {
            tracing::error!(%error, account_id = %account_id, node_id, faden_type = projection.faden_type.as_str(), "failed to commit node Faden account guard");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                "failed to finalize derived Faden account guard".to_string(),
            )
        })?;
    }
    Ok(())
}

async fn rollback_newly_created_node_after_faden_failure(
    state: &ApiState,
    node_id: &str,
) -> Result<(), String> {
    // Only JSONL reaches compensation: PostgreSQL commits the node and its
    // mandatory origin Faden in one transaction and returns before this path.
    // Match normal JSONL create/delete lock order: node persistence before edge
    // persistence. The journal-backed helper restores both files together after
    // crashes during compensation.
    let _node_guard = state.nodes_persist.lock().await;
    let _edge_guard = edge_create_persist_lock().lock().await;
    let outcome = delete_node_and_edges_jsonl(node_id, EdgeEndpointCollisionEvidence::default())
        .await
        .map_err(|error| format!("failed to compensate JSONL node create: {error}"))?;

    let mut edges = state.edges.write().await;
    for edge_id in &outcome.removed_edge_ids {
        edges.remove(edge_id);
    }
    state.metrics.set_edges_cache_count(edges.len() as i64);
    drop(edges);

    let mut nodes = state.nodes.write().await;
    nodes.remove(node_id);
    state.metrics.set_nodes_cache_count(nodes.len() as i64);
    Ok(())
}

const NODE_CREATE_PUBLICATION_DELETED_MESSAGE: &str =
    "node was deleted before the create response could be published";

fn published_node_or_conflict(canonical_node: Option<Node>) -> Result<Node, (StatusCode, String)> {
    canonical_node.ok_or_else(|| {
        (
            StatusCode::CONFLICT,
            NODE_CREATE_PUBLICATION_DELETED_MESSAGE.to_string(),
        )
    })
}

#[cfg(test)]
mod node_create_publication_tests {
    use super::{published_node_or_conflict, NODE_CREATE_PUBLICATION_DELETED_MESSAGE};
    use axum::http::StatusCode;

    #[test]
    fn deleted_post_commit_node_is_not_reported_as_created() {
        let (status, message) = published_node_or_conflict(None)
            .expect_err("deleted canonical node must not fall back to stale create output");

        assert_eq!(status, StatusCode::CONFLICT);
        assert_eq!(message, NODE_CREATE_PUBLICATION_DELETED_MESSAGE);
    }
}

/// Create a node.
///
/// Write path: write gate ([`reject_node_create_unless_writable`]) -> contract
/// validation -> server-generated `id` / `created_at` / `updated_at` ->
/// persistence via the configured node write source -> cache insert. A new
/// durable write returns 201; an identical durable operation replay returns the
/// existing node with 200. Failed writes never leave a phantom cache entry.
///
/// JSONL (default): durable JSONL append (fsync), serialized against
/// concurrent node persistence via `state.nodes_persist`.
/// PostgreSQL (opt-in via `WELTGEWEBE_DOMAIN_NODE_WRITE_SOURCE=postgres`,
/// requires PostgreSQL reads plus PostgreSQL node/edge writes): one transaction
/// commits the node, its conversation trigger effects, idempotency binding and
/// mandatory account→node origin Faden. No dual-write: JSONL mode never touches
/// PostgreSQL, PostgreSQL mode never appends JSONL.
pub async fn create_node(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
    Json(payload): Json<Value>,
) -> Result<(StatusCode, Json<Node>), (StatusCode, String)> {
    reject_node_create_unless_writable(&state)?;
    let creator_account_id = auth
        .account_id
        .clone()
        .filter(|_| auth.authenticated)
        .ok_or_else(|| {
            (
                StatusCode::UNAUTHORIZED,
                "authenticated account context missing".to_string(),
            )
        })?;

    // Deserialize manually (instead of extracting Json<CreateNodeRequest>) so
    // contract violations (unknown fields, missing required fields and explicit
    // nulls) map to a deterministic 400 rather than an extractor-shaped 422.
    let request: node_create::CreateNodeRequest = serde_json::from_value(payload).map_err(|e| {
        (
            StatusCode::BAD_REQUEST,
            format!("invalid node create request: {e}"),
        )
    })?;

    let validated = request.validate().map_err(|e| {
        (
            StatusCode::BAD_REQUEST,
            format!(
                "invalid node create request: {}",
                node_create_error_message(&e)
            ),
        )
    })?;
    let semantic_request = validated.clone();
    let operation_id = validated
        .require_operation_id_for_create()
        .map_err(|error| {
            (
                StatusCode::BAD_REQUEST,
                format!(
                    "invalid node create request: {}",
                    node_create_error_message(&error)
                ),
            )
        })?
        .to_string();
    let actor_id = auth.account_id.clone().ok_or_else(|| {
        (
            StatusCode::UNAUTHORIZED,
            "authenticated account context missing".to_string(),
        )
    })?;
    let operation = Some(CreateOperationKey {
        actor_id,
        operation_id,
    });

    let id = Uuid::new_v4().to_string();
    // PostgreSQL `timestamptz` stores microseconds. Emit the same precision in
    // the create response so its `updated_at` is immediately reusable as the
    // exact `If-Match` value for the first edit.
    let now = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, false);
    let (node, mut record) =
        build_node_record(validated, id.clone(), now, creator_account_id.clone());
    add_create_operation_metadata(&mut record, operation.as_ref());

    // Account writes may deliberately remain JSONL/read-only during a staged
    // migration. The creator account is nevertheless canonical PostgreSQL truth
    // whenever domain reads are PostgreSQL, so atomic node creation depends only
    // on the read, node-write and edge-write sources.
    let postgres_atomic_create = state.config.domain_read_source == DomainReadSource::Postgres
        && state.config.domain_node_write_source == DomainNodeWriteSource::Postgres
        && state.config.domain_edge_write_source == DomainEdgeWriteSource::Postgres;

    // The canonical PostgreSQL configuration writes the node and its mandatory
    // account→node origin Faden in one transaction. Cache state is published
    // only after commit. An operation replay can repair an older partial node,
    // but cannot attach a Faden when the original request semantics differ.
    if postgres_atomic_create {
        let pool = state.db_pool.as_ref().ok_or_else(|| {
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                "PostgreSQL pool unavailable for atomic node creation".to_string(),
            )
        })?;
        let creator_node_limit =
            (auth.role == Role::Gast).then_some(state.config.max_guest_owned_nodes);
        let operation_key = operation
            .as_ref()
            .expect("validated node create always carries an operation id");
        let outcome = insert_domain_node_and_faden_with_creator_limit(
            pool,
            &node,
            operation_key,
            creator_node_limit,
            |existing| node_matches_create(existing, &semantic_request),
            |actual_node_id| {
                super::edges::build_node_origin_faden(&creator_account_id, actual_node_id)
            },
            |actual_node_id| CreateOperationKey {
                actor_id: creator_account_id.clone(),
                operation_id: node_activity_faden_operation_id(
                    super::edges::FadenType::Knotting,
                    &creator_account_id,
                    actual_node_id,
                    actual_node_id,
                ),
            },
        )
        .await;

        let outcome = match outcome {
            Ok(outcome) => outcome,
            Err(NodeFadenCreateError::OperationConflict) => {
                return Err((
                    StatusCode::CONFLICT,
                    "node operation id was already used for different data".to_string(),
                ));
            }
            Err(NodeFadenCreateError::EdgeOperationConflict) => {
                return Err((
                    StatusCode::CONFLICT,
                    "node operation id has an inconsistent origin Faden".to_string(),
                ));
            }
            Err(NodeFadenCreateError::ReplayedNodeDeleted) => {
                return Err((
                    StatusCode::CONFLICT,
                    "node operation result was deleted before replay completed".to_string(),
                ));
            }
            Err(NodeFadenCreateError::Node(NodeCreateError::DuplicateId)) => {
                return Err((StatusCode::CONFLICT, "node id already exists".to_string()));
            }
            Err(NodeFadenCreateError::Node(NodeCreateError::CreatorAccountUnavailable)) => {
                return Err((
                    StatusCode::UNAUTHORIZED,
                    "creator account is no longer active".to_string(),
                ));
            }
            Err(NodeFadenCreateError::Node(NodeCreateError::CreatorNodeLimitReached { max })) => {
                return Err((
                    StatusCode::TOO_MANY_REQUESTS,
                    format!("guest node limit reached ({max})"),
                ));
            }
            Err(error) => {
                tracing::error!(%error, node_id = %node.id, "atomic node and origin Faden create failed");
                return Err((
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "failed to persist node and its origin Faden atomically".to_string(),
                ));
            }
        };

        // Commit releases the transaction-scoped creation locks. Reacquire the
        // canonical node-mutation lock before touching caches and read the
        // post-commit truth under that lock. If delete or guest exit won the
        // narrow gap, their state is preserved instead of being overwritten by
        // the stale create outcome. The guard remains held through both cache
        // writes, matching edit/delete serialization across API instances.
        let publication =
            lock_node_faden_cache_publication(pool, &outcome.node.id, &outcome.edge)
        .await
        .map_err(|error| {
            tracing::error!(%error, node_id = %outcome.node.id, "failed to lock canonical node/Faden cache publication");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                "node and origin Faden committed, but cache publication could not be reconciled; retry the same operation"
                    .to_string(),
            )
        })?;
        let canonical_node = publication.node.clone();
        let canonical_edge = publication.edge.clone();

        // Acquire both cache writers before mutating either projection. Match
        // `ApiState::refresh_domain_projection_if_stale` lock order (nodes,
        // then edges) so refresh and create cannot deadlock. Once both guards
        // are held, readers observe either the old pair or the new pair, never
        // a Faden whose target node is not yet available from the node cache.
        {
            let mut nodes = state.nodes.write().await;
            let mut edges = state.edges.write().await;

            edges.remove(&outcome.edge.id);
            if let Some(edge) = canonical_edge.as_ref() {
                edges.insert(edge.id.clone(), edge.clone());
            }
            nodes.remove(&outcome.node.id);
            if let Some(node) = canonical_node.as_ref() {
                nodes.insert(node.id.clone(), node.clone());
            }

            state.metrics.set_edges_cache_count(edges.len() as i64);
            state.metrics.set_nodes_cache_count(nodes.len() as i64);
        }
        if let Err(error) = publication.release().await {
            tracing::error!(%error, node_id = %outcome.node.id, "failed to release node/Faden cache publication lock");
        }

        let status = if outcome.created {
            StatusCode::CREATED
        } else {
            StatusCode::OK
        };
        let response_node = match published_node_or_conflict(canonical_node) {
            Ok(node) => node,
            Err(error) => {
                tracing::warn!(
                    node_id = %outcome.node.id,
                    replay = !outcome.created,
                    "Node was deleted before its create response could be published"
                );
                return Err(error);
            }
        };
        tracing::info!(
            event = "node.created",
            node_id = %outcome.node.id,
            write_source = ?state.config.domain_node_write_source,
            operation_bound = true,
            atomic_origin_faden = true,
            replay = !outcome.created,
            "Node and origin Faden committed atomically"
        );
        return Ok((status, Json(response_node)));
    }

    match state.config.domain_node_write_source {
        DomainNodeWriteSource::Jsonl => {
            // Serialize operation lookup and append so two retries cannot both
            // decide that the durable result is absent.
            let _persist_guard = state.nodes_persist.lock().await;

            if let Some(operation) = operation.as_ref() {
                let existing = find_node_by_operation(operation).await.map_err(|error| {
                    tracing::error!(%error, "failed to inspect node create operation");
                    (
                        StatusCode::INTERNAL_SERVER_ERROR,
                        "failed to inspect node create operation".to_string(),
                    )
                })?;
                if let Some(existing) = existing {
                    if !node_matches_create(&existing, &semantic_request) {
                        return Err((
                            StatusCode::CONFLICT,
                            "node operation id was already used for different data".to_string(),
                        ));
                    }
                    {
                        let mut nodes = state.nodes.write().await;
                        nodes.insert(existing.id.clone(), existing.clone());
                        state.metrics.set_nodes_cache_count(nodes.len() as i64);
                    }
                    ensure_node_faden_with_operation_id(
                        &state,
                        &auth,
                        &existing.id,
                        false,
                        NodeFadenProjection {
                            faden_type: super::edges::FadenType::Knotting,
                            subject_id: &existing.id,
                            mode: super::edges::FadenProjectionMode::EnsureOnly,
                        },
                    )
                    .await?;
                    return Ok((StatusCode::OK, Json(existing)));
                }
            }

            if auth.role == Role::Gast {
                let max = state.config.max_guest_owned_nodes;
                let owned_count = state
                    .nodes
                    .read()
                    .await
                    .iter_in_order()
                    .filter(|existing| {
                        existing.created_by_account_id.as_deref()
                            == Some(creator_account_id.as_str())
                    })
                    .count();
                if owned_count >= max {
                    return Err((
                        StatusCode::TOO_MANY_REQUESTS,
                        format!("guest node limit reached ({max})"),
                    ));
                }
            }

            // The id is server-generated, but keep the cache collision check
            // as a fail-closed invariant and for directly seeded test states.
            {
                let nodes = state.nodes.read().await;
                if nodes.get(&id).is_some() {
                    return Err((StatusCode::CONFLICT, "node id already exists".to_string()));
                }
            }

            // Only a successful durable append may be reflected in cache.
            if let Err(error) = append_node_line(&record).await {
                let status = jsonl_write_status(&error);
                tracing::error!(%error, %status, "failed to append node to JSONL");
                return Err((
                    status,
                    if status == StatusCode::INSUFFICIENT_STORAGE {
                        "node JSONL storage limit reached".to_string()
                    } else {
                        "failed to persist node".to_string()
                    },
                ));
            }

            let mut nodes = state.nodes.write().await;
            nodes.insert(id.clone(), node.clone());
            state.metrics.set_nodes_cache_count(nodes.len() as i64);
        }
        DomainNodeWriteSource::Postgres => {
            // Any supported PostgreSQL configuration returned from the atomic
            // branch above. Reaching this arm means the source contract is
            // internally inconsistent; never fall back to a partial write.
            return Err((
                StatusCode::INTERNAL_SERVER_ERROR,
                "PostgreSQL node create requires PostgreSQL reads plus PostgreSQL node and edge writes"
                    .to_string(),
            ));
        }
    }

    if let Err(projection_error) = ensure_node_faden_with_operation_id(
        &state,
        &auth,
        &node.id,
        false,
        NodeFadenProjection {
            faden_type: super::edges::FadenType::Knotting,
            subject_id: &node.id,
            mode: super::edges::FadenProjectionMode::EnsureOnly,
        },
    )
    .await
    {
        if let Err(rollback_error) =
            rollback_newly_created_node_after_faden_failure(&state, &node.id).await
        {
            tracing::error!(
                node_id = %node.id,
                %rollback_error,
                "Derived Faden failed and compensating node rollback also failed"
            );
            return Err((
                StatusCode::INTERNAL_SERVER_ERROR,
                "derived Faden failed and node rollback could not be completed".to_string(),
            ));
        }
        return Err(projection_error);
    }

    tracing::info!(
        event = "node.created",
        node_id = %node.id,
        write_source = ?state.config.domain_node_write_source,
        operation_bound = operation.is_some(),
        "Node created"
    );

    Ok((StatusCode::CREATED, Json(node)))
}

/// Node-create request contract.
///
/// Locks the accepted shape and validation rules of a `POST /nodes` create
/// request. `id`, `created_at`, `updated_at` are server-owned and therefore
/// absent from the request type; combined with `deny_unknown_fields` a client
/// that supplies them gets a deterministic 400 instead of a silently dropped
/// field. `operation_id` is required and must be a non-null UUID so a client
/// can safely replay the same account-scoped create action when the derived
/// Faden projection fails after a JSONL node itself became durable.
/// PostgreSQL commits both records atomically and never enters this state.
mod node_create {
    use super::{SearchVisibility, NODE_INFO_MAX_LEN};
    use serde::{de, Deserialize, Deserializer};
    use uuid::Uuid;

    fn deserialize_optional_non_null_string<'de, D>(
        deserializer: D,
    ) -> Result<Option<String>, D::Error>
    where
        D: Deserializer<'de>,
    {
        Option::<String>::deserialize(deserializer)?
            .map(Some)
            .ok_or_else(|| de::Error::custom("field must not be null"))
    }

    /// Mirrors `contracts/domain/node.schema.json` (`title.maxLength`).
    const NODE_TITLE_MAX_LEN: usize = 200;
    /// Mirrors `contracts/domain/node.schema.json` (`kind.maxLength`).
    const NODE_KIND_MAX_LEN: usize = 100;
    /// Address is not yet part of the JSON Schema's `required` set (older
    /// records may lack it), but `POST /nodes` requires it; 500 chars is a
    /// generous bound for a real-world postal address.
    const NODE_ADDRESS_MAX_LEN: usize = 500;
    /// Mirrors `contracts/domain/node.schema.json` (`summary.maxLength`).
    const NODE_SUMMARY_MAX_LEN: usize = 500;
    /// Mirrors `contracts/domain/node.schema.json` (`tags.items.maxLength`).
    const NODE_TAG_MAX_LEN: usize = 64;
    /// The domain contract does not cap the number of tags; this bound exists
    /// only to keep a single create request bounded in size (well above any
    /// realistic UI usage).
    const NODE_TAGS_MAX_COUNT: usize = 32;

    #[derive(Debug, Deserialize)]
    #[serde(deny_unknown_fields)]
    pub(super) struct CreateLocation {
        lat: f64,
        lon: f64,
    }

    #[derive(Debug, Deserialize)]
    #[serde(deny_unknown_fields)]
    pub(super) struct CreateNodeRequest {
        title: String,
        kind: String,
        address: String,
        location: CreateLocation,
        #[serde(default)]
        search_visibility: Option<SearchVisibility>,
        #[serde(default)]
        summary: Option<String>,
        #[serde(default)]
        info: Option<String>,
        #[serde(default)]
        tags: Option<Vec<String>>,
        #[serde(default, deserialize_with = "deserialize_optional_non_null_string")]
        operation_id: Option<String>,
    }

    #[derive(Debug, Clone, PartialEq)]
    pub(super) struct ValidatedCreateNode {
        pub(super) title: String,
        pub(super) kind: String,
        pub(super) address: String,
        /// `None` means the client omitted the field. Creation resolves that
        /// to `public`; replacement preserves the existing canonical value.
        pub(super) search_visibility: Option<SearchVisibility>,
        pub(super) lat: f64,
        pub(super) lon: f64,
        pub(super) summary: Option<String>,
        pub(super) info: Option<String>,
        pub(super) tags: Vec<String>,
        pub(super) operation_id: Option<String>,
    }

    #[derive(Debug, Clone, PartialEq)]
    pub(super) enum NodeCreateValidationError {
        MissingOrEmptyField(&'static str),
        FieldTooLong { field: &'static str, max: usize },
        InvalidCoordinate { field: &'static str },
        InvalidUuid { field: &'static str, value: String },
        TooManyTags { max: usize },
    }

    fn require_non_blank(
        field: &'static str,
        value: &str,
    ) -> Result<(), NodeCreateValidationError> {
        if value.trim().is_empty() {
            return Err(NodeCreateValidationError::MissingOrEmptyField(field));
        }
        Ok(())
    }

    fn require_max_len(
        field: &'static str,
        value: &str,
        max: usize,
    ) -> Result<(), NodeCreateValidationError> {
        if value.chars().count() > max {
            return Err(NodeCreateValidationError::FieldTooLong { field, max });
        }
        Ok(())
    }

    fn require_world_coordinate(
        field: &'static str,
        value: f64,
        min: f64,
        max: f64,
    ) -> Result<(), NodeCreateValidationError> {
        if !value.is_finite() || value < min || value > max {
            return Err(NodeCreateValidationError::InvalidCoordinate { field });
        }
        Ok(())
    }

    impl ValidatedCreateNode {
        pub(super) fn require_operation_id_for_create(
            &self,
        ) -> Result<&str, NodeCreateValidationError> {
            self.operation_id
                .as_deref()
                .ok_or(NodeCreateValidationError::MissingOrEmptyField(
                    "operation_id",
                ))
        }
    }

    impl CreateNodeRequest {
        /// Validate into a `ValidatedCreateNode` without mutating values
        /// beyond trimming required string fields. Order: (1) required fields
        /// non-blank, (2) required field max lengths, (3) coordinates finite
        /// and within world bounds, (4) optional fields (summary/info/tags)
        /// when present.
        pub(super) fn validate(self) -> Result<ValidatedCreateNode, NodeCreateValidationError> {
            require_non_blank("title", &self.title)?;
            require_non_blank("kind", &self.kind)?;
            require_non_blank("address", &self.address)?;

            let title = self.title.trim().to_string();
            let kind = self.kind.trim().to_string();
            let address = self.address.trim().to_string();

            require_max_len("title", &title, NODE_TITLE_MAX_LEN)?;
            require_max_len("kind", &kind, NODE_KIND_MAX_LEN)?;
            require_max_len("address", &address, NODE_ADDRESS_MAX_LEN)?;

            require_world_coordinate("location.lat", self.location.lat, -90.0, 90.0)?;
            require_world_coordinate("location.lon", self.location.lon, -180.0, 180.0)?;

            let summary = match self.summary {
                Some(s) => {
                    require_non_blank("summary", &s)?;
                    let s = s.trim().to_string();
                    require_max_len("summary", &s, NODE_SUMMARY_MAX_LEN)?;
                    Some(s)
                }
                None => None,
            };

            let info = match self.info {
                Some(s) => {
                    require_max_len("info", &s, NODE_INFO_MAX_LEN)?;
                    Some(s)
                }
                None => None,
            };

            let tags = self.tags.unwrap_or_default();
            if tags.len() > NODE_TAGS_MAX_COUNT {
                return Err(NodeCreateValidationError::TooManyTags {
                    max: NODE_TAGS_MAX_COUNT,
                });
            }
            let mut validated_tags = Vec::with_capacity(tags.len());
            for tag in tags {
                require_non_blank("tags[]", &tag)?;
                let tag = tag.trim().to_string();
                require_max_len("tags[]", &tag, NODE_TAG_MAX_LEN)?;
                validated_tags.push(tag);
            }

            let operation_id = match self.operation_id {
                Some(value) => {
                    require_non_blank("operation_id", &value)?;
                    let parsed = Uuid::parse_str(&value).map_err(|_| {
                        NodeCreateValidationError::InvalidUuid {
                            field: "operation_id",
                            value: value.clone(),
                        }
                    })?;
                    Some(parsed.to_string())
                }
                None => None,
            };

            Ok(ValidatedCreateNode {
                title,
                kind,
                address,
                search_visibility: self.search_visibility,
                lat: self.location.lat,
                lon: self.location.lon,
                summary,
                info,
                tags: validated_tags,
                operation_id,
            })
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        fn valid_request() -> CreateNodeRequest {
            CreateNodeRequest {
                title: "A Node".to_string(),
                kind: "Werkstatt".to_string(),
                address: "Musterstraße 1, 12345 Musterstadt".to_string(),
                location: CreateLocation {
                    lat: 53.5,
                    lon: 10.0,
                },
                search_visibility: None,
                summary: None,
                info: None,
                tags: None,
                operation_id: Some("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa".to_string()),
            }
        }

        #[test]
        fn accepts_minimal_payload() {
            let json = r#"{"title":"A Node","kind":"Werkstatt","address":"Musterstraße 1","location":{"lat":53.5,"lon":10.0},"operation_id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}"#;
            let req = serde_json::from_str::<CreateNodeRequest>(json)
                .expect("minimal request must deserialize");
            let validated = req.validate().expect("minimal request must validate");
            assert_eq!(validated.title, "A Node");
            assert_eq!(validated.kind, "Werkstatt");
            assert_eq!(validated.address, "Musterstraße 1");
            assert_eq!(validated.lat, 53.5);
            assert_eq!(validated.lon, 10.0);
            assert_eq!(validated.summary, None);
            assert_eq!(validated.info, None);
            assert!(validated.tags.is_empty());
        }

        #[test]
        fn accepts_full_payload() {
            let json = r#"{"title":"A Node","kind":"Werkstatt","address":"Musterstraße 1","location":{"lat":53.5,"lon":10.0},"summary":"Short","info":"Long text","tags":["a","b"],"operation_id":"bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"}"#;
            let req = serde_json::from_str::<CreateNodeRequest>(json)
                .expect("full request must deserialize");
            let validated = req.validate().expect("full request must validate");
            assert_eq!(validated.summary.as_deref(), Some("Short"));
            assert_eq!(validated.info.as_deref(), Some("Long text"));
            assert_eq!(validated.tags, vec!["a".to_string(), "b".to_string()]);
        }

        #[test]
        fn rejects_missing_operation_id() {
            let json = r#"{"title":"A Node","kind":"Werkstatt","address":"Musterstraße 1","location":{"lat":53.5,"lon":10.0}}"#;
            let req = serde_json::from_str::<CreateNodeRequest>(json)
                .expect("request shape without operation id still deserializes deterministically");
            let validated = req
                .validate()
                .expect("shared create/replace payload validation must succeed");
            assert_eq!(
                validated.require_operation_id_for_create(),
                Err(NodeCreateValidationError::MissingOrEmptyField(
                    "operation_id"
                ))
            );
        }

        #[test]
        fn trims_required_string_fields() {
            let mut req = valid_request();
            req.title = "  A Node  ".to_string();
            req.kind = "  Werkstatt  ".to_string();
            req.address = "  Musterstraße 1  ".to_string();
            let validated = req.validate().expect("must validate");
            assert_eq!(validated.title, "A Node");
            assert_eq!(validated.kind, "Werkstatt");
            assert_eq!(validated.address, "Musterstraße 1");
        }

        #[test]
        fn rejects_missing_title() {
            let json = r#"{"kind":"Werkstatt","address":"Musterstraße 1","location":{"lat":53.5,"lon":10.0}}"#;
            assert!(serde_json::from_str::<CreateNodeRequest>(json).is_err());
        }

        #[test]
        fn rejects_missing_kind() {
            let json = r#"{"title":"A Node","address":"Musterstraße 1","location":{"lat":53.5,"lon":10.0}}"#;
            assert!(serde_json::from_str::<CreateNodeRequest>(json).is_err());
        }

        #[test]
        fn rejects_missing_address() {
            let json =
                r#"{"title":"A Node","kind":"Werkstatt","location":{"lat":53.5,"lon":10.0}}"#;
            assert!(serde_json::from_str::<CreateNodeRequest>(json).is_err());
        }

        #[test]
        fn rejects_missing_location() {
            let json = r#"{"title":"A Node","kind":"Werkstatt","address":"Musterstraße 1"}"#;
            assert!(serde_json::from_str::<CreateNodeRequest>(json).is_err());
        }

        #[test]
        fn rejects_blank_title() {
            let mut req = valid_request();
            req.title = "   ".to_string();
            assert_eq!(
                req.validate(),
                Err(NodeCreateValidationError::MissingOrEmptyField("title"))
            );
        }

        #[test]
        fn rejects_blank_kind() {
            let mut req = valid_request();
            req.kind = "".to_string();
            assert_eq!(
                req.validate(),
                Err(NodeCreateValidationError::MissingOrEmptyField("kind"))
            );
        }

        #[test]
        fn rejects_blank_address() {
            let mut req = valid_request();
            req.address = "   ".to_string();
            assert_eq!(
                req.validate(),
                Err(NodeCreateValidationError::MissingOrEmptyField("address"))
            );
        }

        #[test]
        fn rejects_title_over_max_length() {
            let mut req = valid_request();
            req.title = "a".repeat(NODE_TITLE_MAX_LEN + 1);
            assert_eq!(
                req.validate(),
                Err(NodeCreateValidationError::FieldTooLong {
                    field: "title",
                    max: NODE_TITLE_MAX_LEN
                })
            );
        }

        #[test]
        fn accepts_title_at_max_length() {
            let mut req = valid_request();
            req.title = "a".repeat(NODE_TITLE_MAX_LEN);
            assert!(req.validate().is_ok());
        }

        #[test]
        fn rejects_address_over_max_length() {
            let mut req = valid_request();
            req.address = "a".repeat(NODE_ADDRESS_MAX_LEN + 1);
            assert_eq!(
                req.validate(),
                Err(NodeCreateValidationError::FieldTooLong {
                    field: "address",
                    max: NODE_ADDRESS_MAX_LEN
                })
            );
        }

        #[test]
        fn accepts_address_at_max_length() {
            let mut req = valid_request();
            req.address = "a".repeat(NODE_ADDRESS_MAX_LEN);
            assert!(req.validate().is_ok());
        }

        #[test]
        fn rejects_non_finite_and_out_of_bounds_coordinates() {
            for (lat, lon) in [
                (f64::NAN, 10.0),
                (f64::INFINITY, 10.0),
                (f64::NEG_INFINITY, 10.0),
                (91.0, 10.0),
                (-91.0, 10.0),
            ] {
                let mut req = valid_request();
                req.location = CreateLocation { lat, lon };
                assert_eq!(
                    req.validate(),
                    Err(NodeCreateValidationError::InvalidCoordinate {
                        field: "location.lat"
                    })
                );
            }

            for (lat, lon) in [
                (10.0, f64::NAN),
                (10.0, f64::INFINITY),
                (10.0, f64::NEG_INFINITY),
                (10.0, 181.0),
                (10.0, -181.0),
            ] {
                let mut req = valid_request();
                req.location = CreateLocation { lat, lon };
                assert_eq!(
                    req.validate(),
                    Err(NodeCreateValidationError::InvalidCoordinate {
                        field: "location.lon"
                    })
                );
            }
        }

        #[test]
        fn accepts_world_bound_edges() {
            for (lat, lon) in [(90.0, 180.0), (-90.0, -180.0), (0.0, 0.0)] {
                let mut req = valid_request();
                req.location = CreateLocation { lat, lon };
                assert!(req.validate().is_ok());
            }
        }

        #[test]
        fn rejects_blank_summary_when_present() {
            let mut req = valid_request();
            req.summary = Some("   ".to_string());
            assert_eq!(
                req.validate(),
                Err(NodeCreateValidationError::MissingOrEmptyField("summary"))
            );
        }

        #[test]
        fn rejects_summary_over_max_length() {
            let mut req = valid_request();
            req.summary = Some("a".repeat(NODE_SUMMARY_MAX_LEN + 1));
            assert_eq!(
                req.validate(),
                Err(NodeCreateValidationError::FieldTooLong {
                    field: "summary",
                    max: NODE_SUMMARY_MAX_LEN
                })
            );
        }

        #[test]
        fn rejects_info_over_max_length() {
            let mut req = valid_request();
            req.info = Some("a".repeat(NODE_INFO_MAX_LEN + 1));
            assert_eq!(
                req.validate(),
                Err(NodeCreateValidationError::FieldTooLong {
                    field: "info",
                    max: NODE_INFO_MAX_LEN
                })
            );
        }

        #[test]
        fn rejects_blank_tag() {
            let mut req = valid_request();
            req.tags = Some(vec!["ok".to_string(), "   ".to_string()]);
            assert_eq!(
                req.validate(),
                Err(NodeCreateValidationError::MissingOrEmptyField("tags[]"))
            );
        }

        #[test]
        fn rejects_tag_over_max_length() {
            let mut req = valid_request();
            req.tags = Some(vec!["a".repeat(NODE_TAG_MAX_LEN + 1)]);
            assert_eq!(
                req.validate(),
                Err(NodeCreateValidationError::FieldTooLong {
                    field: "tags[]",
                    max: NODE_TAG_MAX_LEN
                })
            );
        }

        #[test]
        fn rejects_too_many_tags() {
            let mut req = valid_request();
            req.tags = Some(
                (0..NODE_TAGS_MAX_COUNT + 1)
                    .map(|i| i.to_string())
                    .collect(),
            );
            assert_eq!(
                req.validate(),
                Err(NodeCreateValidationError::TooManyTags {
                    max: NODE_TAGS_MAX_COUNT
                })
            );
        }

        #[test]
        fn accepts_max_tag_count() {
            let mut req = valid_request();
            req.tags = Some((0..NODE_TAGS_MAX_COUNT).map(|i| i.to_string()).collect());
            assert!(req.validate().is_ok());
        }

        #[test]
        fn rejects_unknown_fields() {
            let json = r#"{"title":"A Node","kind":"Werkstatt","address":"Musterstraße 1","location":{"lat":53.5,"lon":10.0},"wat":true}"#;
            assert!(serde_json::from_str::<CreateNodeRequest>(json).is_err());
        }

        #[test]
        fn rejects_id_field_because_server_owns_it() {
            let json = r#"{"id":"00000000-0000-0000-0000-000000000001","title":"A Node","kind":"Werkstatt","address":"Musterstraße 1","location":{"lat":53.5,"lon":10.0}}"#;
            assert!(serde_json::from_str::<CreateNodeRequest>(json).is_err());
        }

        #[test]
        fn rejects_created_at_because_server_owns_timestamps() {
            let json = r#"{"title":"A Node","kind":"Werkstatt","address":"Musterstraße 1","location":{"lat":53.5,"lon":10.0},"created_at":"2026-01-01T00:00:00Z"}"#;
            assert!(serde_json::from_str::<CreateNodeRequest>(json).is_err());
        }

        #[test]
        fn rejects_unknown_location_fields() {
            let json = r#"{"title":"A Node","kind":"Werkstatt","address":"Musterstraße 1","location":{"lat":53.5,"lon":10.0,"alt":5.0}}"#;
            assert!(serde_json::from_str::<CreateNodeRequest>(json).is_err());
        }
    }
}

fn set_node_record_fields(record: &mut Value, node: &Node) -> std::io::Result<()> {
    let object = record.as_object_mut().ok_or_else(|| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "node record is not an object",
        )
    })?;
    object.insert("id".to_string(), json!(node.id));
    object.insert("kind".to_string(), json!(node.kind));
    object.insert("title".to_string(), json!(node.title));
    if node.has_authoritative_created_at {
        object.insert("created_at".to_string(), json!(node.created_at));
    }
    object.insert("updated_at".to_string(), json!(node.updated_at));
    if let Some(created_by_account_id) = &node.created_by_account_id {
        object.insert(
            "created_by_account_id".to_string(),
            json!(created_by_account_id),
        );
    } else {
        object.remove("created_by_account_id");
    }
    object.insert(
        "location".to_string(),
        json!({ "lat": node.location.lat, "lon": node.location.lon }),
    );
    for (key, value) in [
        ("summary", node.summary.as_ref().map(|value| json!(value))),
        ("info", node.info.as_ref().map(|value| json!(value))),
        ("address", node.address.as_ref().map(|value| json!(value))),
    ] {
        if let Some(value) = value {
            object.insert(key.to_string(), value);
        } else {
            object.remove(key);
        }
    }
    if node.tags.is_empty() {
        object.remove("tags");
    } else {
        object.insert("tags".to_string(), json!(node.tags));
    }
    object.remove("steckbrief");
    Ok(())
}

#[cfg(test)]
mod node_record_tests {
    use super::*;
    use crate::test_helpers::EnvGuard;
    use serial_test::serial;
    use std::{fs, sync::Arc};

    #[test]
    fn jsonl_replace_does_not_promote_fallback_time_to_creation_time() {
        let mut record = json!({
            "id": "legacy-undated",
            "kind": "resource",
            "title": "Vorher",
            "updated_at": "2026-07-13T05:00:00Z",
            "location": { "lat": 53.5, "lon": 10.0 }
        });
        let mut node = map_json_to_node(&record).expect("legacy node must load");
        assert!(!node.has_authoritative_created_at);
        node.title = "Nachher".to_string();

        set_node_record_fields(&mut record, &node).expect("legacy node record must update");

        assert!(record.get("created_at").is_none());
        assert_eq!(record["updated_at"], "2026-07-13T05:00:00Z");
        let reloaded = map_json_to_node(&record).expect("updated legacy node must reload");
        assert!(!reloaded.has_authoritative_created_at);
    }

    #[tokio::test]
    #[serial]
    async fn jsonl_node_append_boundary_remains_safe_under_serialized_concurrency() {
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

        let record = json!({"id": "boundary-node"});
        let appended_bytes = serde_json::to_vec(&record).expect("record").len() + 1;
        let maximum_bytes = 1024 * 1024;
        let existing_bytes = maximum_bytes - appended_bytes;
        let mut existing = vec![b'x'; existing_bytes];
        *existing.last_mut().expect("non-empty fixture") = b'\n';
        fs::write(nodes_path(), &existing).expect("write near-limit nodes");

        let serialization = Arc::new(tokio::sync::Mutex::new(()));
        let first_lock = serialization.clone();
        let first_record = record.clone();
        let first = tokio::spawn(async move {
            let _guard = first_lock.lock().await;
            append_node_line(&first_record).await
        });
        let second_lock = serialization;
        let second = tokio::spawn(async move {
            let _guard = second_lock.lock().await;
            append_node_line(&record).await
        });
        let results = [
            first.await.expect("first task"),
            second.await.expect("second task"),
        ];

        assert_eq!(results.iter().filter(|result| result.is_ok()).count(), 1);
        assert_eq!(results.iter().filter(|result| result.is_err()).count(), 1);
        assert_eq!(
            fs::metadata(nodes_path()).expect("nodes metadata").len(),
            1024 * 1024
        );
    }

    #[tokio::test]
    #[serial]
    async fn jsonl_node_rewrite_rejects_growth_before_canonical_rename() {
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

        let record = json!({
            "id": "rewrite-node",
            "kind": "Ort",
            "title": "A",
            "location": {"lat": 53.5, "lon": 10.0}
        });
        let first_line = serde_json::to_string(&record).expect("node record");
        let maximum_bytes = 1024 * 1024;
        let filler_len = maximum_bytes - first_line.len() - 2;
        let original = format!("{first_line}\n{}\n", "x".repeat(filler_len));
        fs::write(nodes_path(), original.as_bytes()).expect("write boundary nodes");
        assert_eq!(original.len(), maximum_bytes);
        let mut node = map_json_to_node(&record).expect("node projection");
        node.title = "A".repeat(100);

        let error = replace_node_jsonl(&node)
            .await
            .expect_err("growing rewrite must exceed cap");
        assert_eq!(error.kind(), std::io::ErrorKind::FileTooLarge);
        assert_eq!(
            fs::read(nodes_path()).expect("canonical nodes remain readable"),
            original.as_bytes()
        );
    }

    #[test]
    fn oversized_jsonl_can_recover_by_shrinking_delete_rewrite() {
        let over_limit = 2 * 1024 * 1024;
        assert!(ensure_delete_rewrite_not_growing("nodes", over_limit, over_limit - 1).is_ok());
        assert!(ensure_delete_rewrite_not_growing("edges", over_limit, over_limit).is_ok());
        assert!(ensure_delete_rewrite_not_growing("nodes", over_limit, over_limit + 1).is_err());
    }

    #[test]
    fn jsonl_capacity_breach_maps_to_insufficient_storage() {
        let error = std::io::Error::new(std::io::ErrorKind::FileTooLarge, "cap");
        assert_eq!(jsonl_write_status(&error), StatusCode::INSUFFICIENT_STORAGE);
    }
}

async fn replace_node_jsonl(node: &Node) -> std::io::Result<bool> {
    let maximum_bytes = jsonl_policy_limits().await?.max_nodes_jsonl_bytes();
    let path = nodes_path();
    let file = File::open(&path).await?;
    let mut lines = BufReader::new(file).lines();
    let mut tmp_path = path.clone();
    let filename = tmp_path.file_name().ok_or_else(|| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "nodes path has no filename",
        )
    })?;
    let mut tmp_filename = filename.to_os_string();
    tmp_filename.push(format!(".tmp.{}", Uuid::new_v4()));
    tmp_path.set_file_name(tmp_filename);

    let result = async {
        let tmp_file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&tmp_path)
            .await?;
        let mut writer = BufWriter::new(tmp_file);
        let mut replaced = false;
        while let Some(line) = lines.next_line().await? {
            let mut output = line.clone();
            if jsonl_line_has_id(&line, &node.id) {
                let mut value = serde_json::from_str::<Value>(&line)
                    .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))?;
                set_node_record_fields(&mut value, node)?;
                output = serde_json::to_string(&value)
                    .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))?;
                replaced = true;
            }
            writer.write_all(output.as_bytes()).await?;
            writer.write_all(b"\n").await?;
        }
        writer.flush().await?;
        let file = writer.into_inner();
        ensure_jsonl_size("nodes", file.metadata().await?.len(), maximum_bytes)?;
        file.sync_all().await?;
        Ok::<bool, std::io::Error>(replaced)
    }
    .await;

    match result {
        Ok(replaced) => {
            tokio::fs::rename(&tmp_path, &path).await?;
            if let Some(parent) = path.parent() {
                sync_directory(parent)?;
            }
            Ok(replaced)
        }
        Err(error) => {
            let _ = tokio::fs::remove_file(&tmp_path).await;
            Err(error)
        }
    }
}

const NODE_DELETE_JOURNAL_FILE: &str = ".node-delete-cascade.journal.json";
const NODE_DELETE_JOURNAL_VERSION: u8 = 1;
const NODE_DELETE_JOURNAL_KIND: &str = "node_delete_cascade";

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
enum NodeDeleteJournalPhase {
    Prepared,
    BackupsCreated,
    EdgeSwapped,
    NodeSwapped,
}

#[derive(Debug, Deserialize, Serialize)]
struct NodeDeleteJournal {
    version: u8,
    kind: String,
    tx_id: String,
    phase: NodeDeleteJournalPhase,
    node_tmp: String,
    edge_tmp: String,
    node_backup: String,
    edge_backup: String,
    edge_original_existed: bool,
}

#[derive(Debug)]
struct NodeDeletePaths {
    dir: PathBuf,
    nodes: PathBuf,
    edges: PathBuf,
    journal: PathBuf,
}

#[derive(Debug)]
struct JsonlNodeDeleteOutcome {
    node_deleted: bool,
    removed_edge_ids: Vec<String>,
}

#[derive(Clone, Copy, Debug, Default)]
struct JsonlNodeDeleteFailureInjection {
    fail_after_temps_prepared: bool,
    fail_before_node_swap: bool,
    crash_after_edge_swap: bool,
    defer_cleanup_after_node_swap: bool,
}

enum JsonlNodeDeleteTxError {
    Io(std::io::Error),
    SimulatedCrash(std::io::Error),
}

impl From<std::io::Error> for JsonlNodeDeleteTxError {
    fn from(error: std::io::Error) -> Self {
        Self::Io(error)
    }
}

fn io_invalid_data(message: impl Into<String>) -> std::io::Error {
    std::io::Error::new(std::io::ErrorKind::InvalidData, message.into())
}

fn io_invalid_input(message: impl Into<String>) -> std::io::Error {
    std::io::Error::new(std::io::ErrorKind::InvalidInput, message.into())
}

fn node_delete_paths() -> std::io::Result<NodeDeletePaths> {
    let nodes = nodes_path();
    let edges = edges_path();
    let node_dir = nodes
        .parent()
        .ok_or_else(|| io_invalid_input("nodes path has no parent directory"))?;
    let edge_dir = edges
        .parent()
        .ok_or_else(|| io_invalid_input("edges path has no parent directory"))?;
    if node_dir != edge_dir {
        return Err(io_invalid_input(
            "nodes and edges JSONL paths must share the same data directory",
        ));
    }
    let dir = node_dir.to_path_buf();
    Ok(NodeDeletePaths {
        journal: dir.join(NODE_DELETE_JOURNAL_FILE),
        dir,
        nodes,
        edges,
    })
}

fn file_name_for(path: &FsPath, label: &str) -> std::io::Result<String> {
    path.file_name()
        .and_then(|value| value.to_str())
        .map(str::to_string)
        .ok_or_else(|| io_invalid_input(format!("{label} path has no UTF-8 filename")))
}

fn journal_child_path(dir: &FsPath, filename: &str) -> std::io::Result<PathBuf> {
    if filename.is_empty() {
        return Err(io_invalid_data("journal path filename is empty"));
    }
    let path = FsPath::new(filename);
    if path.components().count() != 1
        || path.file_name().and_then(|value| value.to_str()) != Some(filename)
    {
        return Err(io_invalid_data(format!(
            "journal path {filename} is not a single data-directory filename"
        )));
    }
    Ok(dir.join(filename))
}

async fn ensure_regular_file_or_missing(path: &FsPath, label: &str) -> std::io::Result<bool> {
    match tokio::fs::symlink_metadata(path).await {
        Ok(metadata) if metadata.file_type().is_file() => Ok(true),
        Ok(_) => Err(io_invalid_input(format!(
            "{label} path {} is not a regular file",
            path.display()
        ))),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(error),
    }
}

async fn ensure_regular_file(path: &FsPath, label: &str) -> std::io::Result<()> {
    if ensure_regular_file_or_missing(path, label).await? {
        Ok(())
    } else {
        Err(std::io::Error::new(
            std::io::ErrorKind::NotFound,
            format!("{label} path {} does not exist", path.display()),
        ))
    }
}

fn sync_directory(path: &FsPath) -> std::io::Result<()> {
    std::fs::File::open(path)?.sync_all()
}

async fn write_node_delete_journal(
    paths: &NodeDeletePaths,
    journal: &NodeDeleteJournal,
) -> std::io::Result<()> {
    ensure_regular_file_or_missing(&paths.journal, "node-delete journal").await?;
    let bytes = serde_json::to_vec(journal)
        .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))?;
    let journal_tmp = paths
        .dir
        .join(format!("{NODE_DELETE_JOURNAL_FILE}.{}.tmp", Uuid::new_v4()));
    let write_result = async {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&journal_tmp)
            .await?;
        file.write_all(&bytes).await?;
        file.write_all(b"\n").await?;
        file.flush().await?;
        file.sync_all().await?;
        tokio::fs::rename(&journal_tmp, &paths.journal).await?;
        sync_directory(&paths.dir)?;
        Ok::<(), std::io::Error>(())
    }
    .await;
    if write_result.is_err() {
        let _ = remove_file_if_exists(&journal_tmp).await;
    }
    write_result
}

async fn read_node_delete_journal(
    paths: &NodeDeletePaths,
) -> std::io::Result<Option<NodeDeleteJournal>> {
    match ensure_regular_file_or_missing(&paths.journal, "node-delete journal").await {
        Ok(true) => {}
        Ok(false) => return Ok(None),
        Err(error) => return Err(error),
    }
    let bytes = tokio::fs::read(&paths.journal).await?;
    let journal: NodeDeleteJournal = serde_json::from_slice(&bytes)
        .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))?;
    if journal.version != NODE_DELETE_JOURNAL_VERSION || journal.kind != NODE_DELETE_JOURNAL_KIND {
        return Err(io_invalid_data("unknown node-delete journal format"));
    }
    Ok(Some(journal))
}

fn new_node_delete_journal(paths: &NodeDeletePaths) -> std::io::Result<NodeDeleteJournal> {
    let tx_id = Uuid::new_v4().to_string();
    let node_name = file_name_for(&paths.nodes, "nodes")?;
    let edge_name = file_name_for(&paths.edges, "edges")?;
    Ok(NodeDeleteJournal {
        version: NODE_DELETE_JOURNAL_VERSION,
        kind: NODE_DELETE_JOURNAL_KIND.to_string(),
        tx_id: tx_id.clone(),
        phase: NodeDeleteJournalPhase::Prepared,
        node_tmp: format!("{node_name}.delete.{tx_id}.tmp"),
        edge_tmp: format!("{edge_name}.delete.{tx_id}.tmp"),
        node_backup: format!("{node_name}.delete.{tx_id}.bak"),
        edge_backup: format!("{edge_name}.delete.{tx_id}.bak"),
        edge_original_existed: false,
    })
}

async fn remove_file_if_exists(path: &FsPath) -> std::io::Result<()> {
    match tokio::fs::remove_file(path).await {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error),
    }
}

async fn restore_backup(
    target: &FsPath,
    backup: &FsPath,
    original_existed: bool,
) -> std::io::Result<()> {
    if original_existed {
        if ensure_regular_file_or_missing(backup, "node-delete backup").await? {
            remove_file_if_exists(target).await?;
            tokio::fs::rename(backup, target).await?;
        } else {
            // Before backups are created the original target is still valid.
            // After that point a missing target and missing backup is corruption
            // and must keep the journal in place for operator recovery.
            ensure_regular_file(target, "node-delete rollback target").await?;
        }
    } else {
        remove_file_if_exists(target).await?;
        remove_file_if_exists(backup).await?;
    }
    Ok(())
}

async fn rollback_node_delete_journal(
    paths: &NodeDeletePaths,
    journal: &NodeDeleteJournal,
) -> std::io::Result<()> {
    let node_tmp = journal_child_path(&paths.dir, &journal.node_tmp)?;
    let edge_tmp = journal_child_path(&paths.dir, &journal.edge_tmp)?;
    let node_backup = journal_child_path(&paths.dir, &journal.node_backup)?;
    let edge_backup = journal_child_path(&paths.dir, &journal.edge_backup)?;

    remove_file_if_exists(&node_tmp).await?;
    remove_file_if_exists(&edge_tmp).await?;
    restore_backup(&paths.nodes, &node_backup, true).await?;
    restore_backup(&paths.edges, &edge_backup, journal.edge_original_existed).await?;
    remove_file_if_exists(&paths.journal).await?;
    sync_directory(&paths.dir)?;
    Ok(())
}

async fn complete_node_delete_commit(
    paths: &NodeDeletePaths,
    journal: &NodeDeleteJournal,
) -> std::io::Result<()> {
    if journal.phase != NodeDeleteJournalPhase::NodeSwapped {
        return Err(io_invalid_data(
            "node-delete commit cleanup requires the durable node_swapped marker",
        ));
    }

    let node_tmp = journal_child_path(&paths.dir, &journal.node_tmp)?;
    let edge_tmp = journal_child_path(&paths.dir, &journal.edge_tmp)?;
    let node_backup = journal_child_path(&paths.dir, &journal.node_backup)?;
    let edge_backup = journal_child_path(&paths.dir, &journal.edge_backup)?;

    remove_file_if_exists(&node_tmp).await?;
    remove_file_if_exists(&edge_tmp).await?;

    // Never discard the rollback material until both committed projections are
    // demonstrably present as regular files.
    ensure_regular_file(&paths.nodes, "committed nodes JSONL").await?;
    ensure_regular_file(&paths.edges, "committed edges JSONL").await?;
    sync_directory(&paths.dir)?;
    remove_file_if_exists(&node_backup).await?;
    remove_file_if_exists(&edge_backup).await?;
    remove_file_if_exists(&node_tmp).await?;
    remove_file_if_exists(&edge_tmp).await?;
    remove_file_if_exists(&paths.journal).await?;
    sync_directory(&paths.dir)?;
    Ok(())
}

pub async fn recover_node_delete_jsonl_journal() -> std::io::Result<()> {
    let paths = node_delete_paths()?;
    let Some(journal) = read_node_delete_journal(&paths).await? else {
        return Ok(());
    };
    match journal.phase {
        NodeDeleteJournalPhase::Prepared
        | NodeDeleteJournalPhase::BackupsCreated
        | NodeDeleteJournalPhase::EdgeSwapped => {
            rollback_node_delete_journal(&paths, &journal).await
        }
        NodeDeleteJournalPhase::NodeSwapped => complete_node_delete_commit(&paths, &journal).await,
    }
}

fn edge_endpoint_is_role_collision(
    value: &Value,
    id_field: &str,
    type_field: &str,
    node_id: &str,
) -> bool {
    value.get(id_field).and_then(Value::as_str) == Some(node_id)
        && value.get(type_field).and_then(Value::as_str) == Some("role")
}

async fn edge_role_collision_jsonl(edge_path: &FsPath, node_id: &str) -> std::io::Result<bool> {
    let file = match File::open(edge_path).await {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(false),
        Err(error) => return Err(error),
    };
    let mut lines = BufReader::new(file).lines();
    while let Some(line) = lines.next_line().await? {
        let Ok(value) = serde_json::from_str::<Value>(&line) else {
            continue;
        };
        if edge_endpoint_is_role_collision(&value, "source_id", "source_type", node_id)
            || edge_endpoint_is_role_collision(&value, "target_id", "target_type", node_id)
        {
            return Ok(true);
        }
    }
    Ok(false)
}

fn ensure_delete_rewrite_not_growing(
    label: &str,
    original_bytes: u64,
    resulting_bytes: u64,
) -> std::io::Result<()> {
    if resulting_bytes > original_bytes {
        return Err(std::io::Error::other(format!(
            "{label} JSONL delete rewrite grew from {original_bytes} to {resulting_bytes} bytes"
        )));
    }
    Ok(())
}

async fn prepare_node_delete_tmp(
    source_path: &FsPath,
    tmp_path: &FsPath,
    node_id: &str,
) -> std::io::Result<bool> {
    let original_bytes = tokio::fs::metadata(source_path).await?.len();
    let file = File::open(source_path).await?;
    let mut lines = BufReader::new(file).lines();
    let tmp_file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(tmp_path)
        .await?;
    let mut writer = BufWriter::new(tmp_file);
    let mut deleted = false;

    while let Some(line) = lines.next_line().await? {
        let matches = jsonl_line_has_id(&line, node_id);
        if matches {
            deleted = true;
            continue;
        }
        writer.write_all(line.as_bytes()).await?;
        writer.write_all(b"\n").await?;
    }
    writer.flush().await?;
    let file = writer.into_inner();
    let resulting_bytes = file.metadata().await?.len();
    ensure_delete_rewrite_not_growing("nodes", original_bytes, resulting_bytes)?;
    file.sync_all().await?;
    Ok(deleted)
}

async fn prepare_edge_delete_tmp(
    source_path: &FsPath,
    tmp_path: &FsPath,
    node_id: &str,
    evidence: EdgeEndpointCollisionEvidence,
) -> std::io::Result<Vec<String>> {
    let original_bytes = match tokio::fs::metadata(source_path).await {
        Ok(metadata) => metadata.len(),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => 0,
        Err(error) => return Err(error),
    };
    let maybe_file = match File::open(source_path).await {
        Ok(file) => Some(file),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
        Err(error) => return Err(error),
    };
    let tmp_file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(tmp_path)
        .await?;
    let mut writer = BufWriter::new(tmp_file);
    let mut removed = Vec::new();

    if let Some(file) = maybe_file {
        let mut lines = BufReader::new(file).lines();
        let mut line_number = 0usize;
        while let Some(line) = lines.next_line().await? {
            line_number += 1;
            let value = serde_json::from_str::<Value>(&line).map_err(|error| {
                io_invalid_data(format!("invalid edge JSONL at line {line_number}: {error}"))
            })?;
            if edge_value_references_node_for_delete(&value, node_id, evidence)? {
                let edge_id = value.get("id").and_then(Value::as_str).ok_or_else(|| {
                    std::io::Error::new(
                        std::io::ErrorKind::InvalidData,
                        "edge referencing node must have a string id",
                    )
                })?;
                removed.push(edge_id.to_string());
                continue;
            }
            writer.write_all(line.as_bytes()).await?;
            writer.write_all(b"\n").await?;
        }
    }

    writer.flush().await?;
    let file = writer.into_inner();
    let resulting_bytes = file.metadata().await?.len();
    ensure_delete_rewrite_not_growing("edges", original_bytes, resulting_bytes)?;
    file.sync_all().await?;
    Ok(removed)
}

async fn rename_target_to_backup(
    target: &FsPath,
    backup: &FsPath,
    original_existed: bool,
) -> std::io::Result<()> {
    if original_existed {
        ensure_regular_file(target, "JSONL target").await?;
        if ensure_regular_file_or_missing(backup, "JSONL backup").await? {
            return Err(std::io::Error::new(
                std::io::ErrorKind::AlreadyExists,
                format!("backup {} already exists", backup.display()),
            ));
        }
        tokio::fs::rename(target, backup).await?;
    }
    Ok(())
}

async fn cleanup_node_delete_transaction_files(
    paths: &NodeDeletePaths,
    journal: &NodeDeleteJournal,
) -> std::io::Result<()> {
    let node_backup = journal_child_path(&paths.dir, &journal.node_backup)?;
    let edge_backup = journal_child_path(&paths.dir, &journal.edge_backup)?;
    let node_tmp = journal_child_path(&paths.dir, &journal.node_tmp)?;
    let edge_tmp = journal_child_path(&paths.dir, &journal.edge_tmp)?;
    remove_file_if_exists(&node_backup).await?;
    remove_file_if_exists(&edge_backup).await?;
    remove_file_if_exists(&node_tmp).await?;
    remove_file_if_exists(&edge_tmp).await?;
    remove_file_if_exists(&paths.journal).await?;
    sync_directory(&paths.dir)?;
    Ok(())
}

async fn delete_node_and_edges_jsonl(
    node_id: &str,
    evidence: EdgeEndpointCollisionEvidence,
) -> std::io::Result<JsonlNodeDeleteOutcome> {
    delete_node_and_edges_jsonl_with_injection(
        node_id,
        evidence,
        JsonlNodeDeleteFailureInjection::default(),
    )
    .await
}

async fn delete_node_and_edges_jsonl_with_injection(
    node_id: &str,
    mut evidence: EdgeEndpointCollisionEvidence,
    injection: JsonlNodeDeleteFailureInjection,
) -> std::io::Result<JsonlNodeDeleteOutcome> {
    recover_node_delete_jsonl_journal().await?;

    let paths = node_delete_paths()?;
    tokio::fs::create_dir_all(&paths.dir).await?;
    ensure_regular_file(&paths.nodes, "nodes JSONL").await?;
    let edge_original_existed = ensure_regular_file_or_missing(&paths.edges, "edges JSONL").await?;
    ensure_regular_file_or_missing(&paths.journal, "node-delete journal").await?;

    evidence.role_exists |= edge_role_collision_jsonl(&paths.edges, node_id).await?;

    let mut journal = new_node_delete_journal(&paths)?;
    journal.edge_original_existed = edge_original_existed;
    let node_tmp = journal_child_path(&paths.dir, &journal.node_tmp)?;
    let edge_tmp = journal_child_path(&paths.dir, &journal.edge_tmp)?;
    let node_backup = journal_child_path(&paths.dir, &journal.node_backup)?;
    let edge_backup = journal_child_path(&paths.dir, &journal.edge_backup)?;

    let result = async {
        let removed_edge_ids =
            prepare_edge_delete_tmp(&paths.edges, &edge_tmp, node_id, evidence).await?;
        let node_deleted = prepare_node_delete_tmp(&paths.nodes, &node_tmp, node_id).await?;
        if !node_deleted {
            return Err(JsonlNodeDeleteTxError::Io(std::io::Error::new(
                std::io::ErrorKind::NotFound,
                "node was not found in JSONL persistence during delete",
            )));
        }
        sync_directory(&paths.dir)?;

        if injection.fail_after_temps_prepared {
            return Err(JsonlNodeDeleteTxError::Io(std::io::Error::other(
                "injected node-delete failure after temp preparation",
            )));
        }

        write_node_delete_journal(&paths, &journal).await?;

        rename_target_to_backup(&paths.edges, &edge_backup, edge_original_existed).await?;
        rename_target_to_backup(&paths.nodes, &node_backup, true).await?;
        sync_directory(&paths.dir)?;
        journal.phase = NodeDeleteJournalPhase::BackupsCreated;
        write_node_delete_journal(&paths, &journal).await?;

        tokio::fs::rename(&edge_tmp, &paths.edges).await?;
        sync_directory(&paths.dir)?;
        journal.phase = NodeDeleteJournalPhase::EdgeSwapped;
        write_node_delete_journal(&paths, &journal).await?;

        if injection.crash_after_edge_swap {
            return Err(JsonlNodeDeleteTxError::SimulatedCrash(std::io::Error::new(
                std::io::ErrorKind::Interrupted,
                "injected node-delete crash after edge swap",
            )));
        }

        if injection.fail_before_node_swap {
            return Err(JsonlNodeDeleteTxError::Io(std::io::Error::other(
                "injected node-delete failure before node swap",
            )));
        }

        tokio::fs::rename(&node_tmp, &paths.nodes).await?;
        sync_directory(&paths.dir)?;
        journal.phase = NodeDeleteJournalPhase::NodeSwapped;
        write_node_delete_journal(&paths, &journal).await?;

        if injection.defer_cleanup_after_node_swap {
            tracing::warn!(
                node_id,
                "node delete committed; transaction cleanup deliberately deferred"
            );
        } else if let Err(error) = cleanup_node_delete_transaction_files(&paths, &journal).await {
            tracing::warn!(
                %error,
                node_id,
                "node delete committed; transaction cleanup deferred to startup recovery"
            );
        }
        Ok::<JsonlNodeDeleteOutcome, JsonlNodeDeleteTxError>(JsonlNodeDeleteOutcome {
            node_deleted,
            removed_edge_ids,
        })
    }
    .await;

    match result {
        Ok(outcome) => Ok(outcome),
        Err(JsonlNodeDeleteTxError::SimulatedCrash(error)) => Err(error),
        Err(JsonlNodeDeleteTxError::Io(error)) => {
            rollback_node_delete_journal(&paths, &journal).await?;
            Err(error)
        }
    }
}

pub async fn replace_node(
    state: ApiState,
    id: String,
    payload: Value,
    actor_account_id: String,
) -> Result<Json<Node>, NodeMutationError> {
    reject_node_patch_unless_writable(&state)
        .map_err(|(status, body)| NodeMutationError::Message(status, body))?;
    let request: node_create::CreateNodeRequest =
        serde_json::from_value(payload).map_err(|error| {
            NodeMutationError::Message(
                StatusCode::BAD_REQUEST,
                format!("invalid node replacement request: {error}"),
            )
        })?;
    let validated = request.validate().map_err(|error| {
        NodeMutationError::Message(
            StatusCode::BAD_REQUEST,
            format!(
                "invalid node replacement request: {}",
                node_create_error_message(&error)
            ),
        )
    })?;
    if validated.operation_id.is_some() {
        return Err(NodeMutationError::Message(
            StatusCode::BAD_REQUEST,
            "operation_id is only valid when creating a node".to_string(),
        ));
    }

    let _persist_guard = state.nodes_persist.lock().await;
    let existing = state
        .nodes
        .read()
        .await
        .get(&id)
        .cloned()
        .ok_or(NodeMutationError::Status(StatusCode::NOT_FOUND))?;
    let mut node = Node {
        id: existing.id.clone(),
        kind: validated.kind,
        title: validated.title,
        created_at: existing.created_at.clone(),
        updated_at: existing.updated_at.clone(),
        has_authoritative_created_at: existing.has_authoritative_created_at,
        created_by_account_id: existing.created_by_account_id.clone(),
        search_visibility: validated
            .search_visibility
            .unwrap_or(existing.search_visibility),
        summary: validated.summary,
        info: validated.info,
        tags: validated.tags,
        address: Some(validated.address),
        location: Location {
            lat: validated.lat,
            lon: validated.lon,
        },
    };
    if node == existing {
        let audit = NodeMutationAudit::new(
            NodeMutationOperation::Replace,
            &id,
            &actor_account_id,
            &existing,
            Some(&existing),
        )
        .map_err(|error| {
            tracing::error!(%error, node_id = %id, "failed to prepare no-op node replacement audit");
            NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR)
        })?;
        match state.config.domain_node_write_source {
            DomainNodeWriteSource::Jsonl => {
                prepare_jsonl_audit(&audit).await.map_err(|error| {
                    tracing::error!(%error, node_id = %id, "failed to durably prepare no-op node replacement audit");
                    NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR)
                })?;
                if let Err(error) = commit_jsonl_audit(&audit).await {
                    tracing::warn!(%error, node_id = %id, operation_id = %audit.operation_id, "no-op node replacement accepted; audit finalization deferred to startup recovery");
                }
            }
            DomainNodeWriteSource::Postgres => {
                let pool = state
                    .db_pool
                    .as_ref()
                    .ok_or(NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR))?;
                persist_postgres_audit(pool, &audit).await.map_err(|error| {
                    tracing::error!(%error, node_id = %id, "failed to persist no-op node replacement audit");
                    NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR)
                })?;
            }
        }
        tracing::info!(event = "node.replaced.collective.noop", node_id = %id, "Node replacement matched current state and was audited");
        return Ok(Json(existing));
    }
    node.updated_at = chrono::Utc::now().to_rfc3339();
    let audit = NodeMutationAudit::new(
        NodeMutationOperation::Replace,
        &id,
        &actor_account_id,
        &existing,
        Some(&node),
    )
    .map_err(|error| {
        tracing::error!(%error, node_id = %id, "failed to prepare node replacement audit");
        NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR)
    })?;

    match state.config.domain_node_write_source {
        DomainNodeWriteSource::Jsonl => {
            prepare_jsonl_audit(&audit).await.map_err(|error| {
                tracing::error!(%error, node_id = %id, "failed to durably prepare node replacement audit");
                NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR)
            })?;
            match replace_node_jsonl(&node).await {
                Ok(true) => {
                    if let Err(error) = commit_jsonl_audit(&audit).await {
                        tracing::warn!(%error, node_id = %id, operation_id = %audit.operation_id, "node replacement committed; audit finalization deferred to startup recovery");
                    }
                }
                Ok(false) => {
                    let _ = abort_jsonl_audit(&audit).await;
                    return Err(NodeMutationError::Status(StatusCode::NOT_FOUND));
                }
                Err(error) => {
                    let _ = abort_jsonl_audit(&audit).await;
                    let status = jsonl_write_status(&error);
                    tracing::error!(%error, %status, node_id = %id, "failed to replace node JSONL record");
                    return Err(NodeMutationError::Status(status));
                }
            }
        }
        DomainNodeWriteSource::Postgres => {
            let pool = state
                .db_pool
                .as_ref()
                .ok_or(NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR))?;
            let persisted_updated_at = replace_node_in_postgres_audited(pool, &node, &audit)
                .await
                .map_err(|error| match error {
                    NodeWriteError::NotFound => NodeMutationError::Status(StatusCode::NOT_FOUND),
                    other => {
                        tracing::error!(%other, node_id = %id, "failed to replace node in PostgreSQL");
                        NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR)
                    }
                })?;
            node.updated_at = persisted_updated_at.to_rfc3339();
        }
    }

    let mut nodes = state.nodes.write().await;
    nodes.insert(id.clone(), node.clone());
    state.metrics.set_nodes_cache_count(nodes.len() as i64);
    tracing::info!(event = "node.updated.collective", node_id = %id, "Node collectively updated");
    Ok(Json(node))
}

fn map_postgres_node_delete_error(error: NodeWriteError, id: &str) -> NodeMutationError {
    match error {
        NodeWriteError::NotFound => NodeMutationError::Status(StatusCode::NOT_FOUND),
        NodeWriteError::InvalidEdgeReference(_) => NodeMutationError::Message(
            StatusCode::CONFLICT,
            "node deletion blocked by ambiguous edge endpoint".to_string(),
        ),
        NodeWriteError::ConversationNotEmpty => NodeMutationError::Problem {
            status: StatusCode::CONFLICT,
            code: "node_conversation_not_empty",
            message:
                "node deletion is blocked because its public conversation contains contributions",
        },
        other => {
            tracing::error!(%other, node_id = %id, "failed to delete node in PostgreSQL");
            NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR)
        }
    }
}

pub async fn delete_node(
    state: ApiState,
    id: String,
    actor_account_id: String,
) -> Result<(StatusCode, Json<NodeDeleteResponse>), NodeMutationError> {
    reject_node_patch_unless_writable(&state)
        .map_err(|(status, body)| NodeMutationError::Message(status, body))?;
    let persistence_is_coherent = matches!(
        (
            state.config.domain_node_write_source,
            state.config.domain_edge_write_source,
        ),
        (DomainNodeWriteSource::Jsonl, DomainEdgeWriteSource::Jsonl,)
            | (
                DomainNodeWriteSource::Postgres,
                DomainEdgeWriteSource::Postgres,
            )
    );
    if !persistence_is_coherent {
        return Err(NodeMutationError::Message(
            StatusCode::CONFLICT,
            "node deletion requires matching node and edge write sources".to_string(),
        ));
    }
    let _persist_guard = state.nodes_persist.lock().await;
    let existing = state
        .nodes
        .read()
        .await
        .get(&id)
        .cloned()
        .ok_or(NodeMutationError::Status(StatusCode::NOT_FOUND))?;
    let audit = NodeMutationAudit::new(
        NodeMutationOperation::Delete,
        &id,
        &actor_account_id,
        &existing,
        None,
    )
    .map_err(|error| {
        tracing::error!(%error, node_id = %id, "failed to prepare node delete audit");
        NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR)
    })?;

    let account_collision_exists = {
        let accounts = state.accounts.read().await;
        accounts.get(&id).is_some()
    };
    let cached_edge_ids = {
        let edges = state.edges.read().await;
        let role_collision_exists = edges.iter_in_order().any(|edge| {
            (edge.source_id == id && edge.source_type.as_deref() == Some("role"))
                || (edge.target_id == id && edge.target_type.as_deref() == Some("role"))
        });
        let evidence = EdgeEndpointCollisionEvidence {
            account_exists: account_collision_exists,
            role_exists: role_collision_exists,
        };
        let mut edge_ids = Vec::new();
        for edge in edges.iter_in_order() {
            if edge_references_node_for_delete(edge, &id, evidence).map_err(|error| {
                tracing::error!(%error, node_id = %id, "node deletion blocked by ambiguous cached edge endpoint");
                NodeMutationError::Message(
                    StatusCode::CONFLICT,
                    "node deletion blocked by ambiguous edge endpoint".to_string(),
                )
            })? {
                edge_ids.push(edge.id.clone());
            }
        }
        edge_ids
    };

    let (persisted_edge_ids, conversation) = match state.config.domain_node_write_source {
        DomainNodeWriteSource::Jsonl => {
            prepare_jsonl_audit(&audit).await.map_err(|error| {
                tracing::error!(%error, node_id = %id, "failed to durably prepare node delete audit");
                NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR)
            })?;
            // Keep the edge lock until the node file has also been replaced. Otherwise a
            // concurrent edge create could attach a new edge between cascade cleanup and
            // node deletion, leaving an orphan behind.
            let _edge_persist_guard = edge_create_persist_lock().lock().await;
            let outcome = match delete_node_and_edges_jsonl(
                &id,
                EdgeEndpointCollisionEvidence {
                    account_exists: account_collision_exists,
                    role_exists: false,
                },
            )
            .await
            {
                Ok(outcome) => outcome,
                Err(error) => {
                    let _ = abort_jsonl_audit(&audit).await;
                    tracing::error!(%error, node_id = %id, "failed to delete node and edges from JSONL");
                    return Err(if error.kind() == std::io::ErrorKind::InvalidData {
                        NodeMutationError::Message(
                            StatusCode::CONFLICT,
                            "node deletion blocked by ambiguous edge endpoint".to_string(),
                        )
                    } else {
                        NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR)
                    });
                }
            };
            if !outcome.node_deleted {
                let _ = abort_jsonl_audit(&audit).await;
                return Err(NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR));
            }
            if let Err(error) = commit_jsonl_audit(&audit).await {
                tracing::warn!(%error, node_id = %id, operation_id = %audit.operation_id, "node delete committed; audit finalization deferred to startup recovery");
            }
            (
                outcome.removed_edge_ids,
                NodeDeleteConversationResponse::NotApplicable,
            )
        }
        DomainNodeWriteSource::Postgres => {
            let pool = state
                .db_pool
                .as_ref()
                .ok_or(NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR))?;
            let outcome = delete_node_with_edges_in_postgres_audited(pool, &id, &audit)
                .await
                .map_err(|error| map_postgres_node_delete_error(error, &id))?;
            (outcome.removed_edge_ids, outcome.conversation.into())
        }
    };

    let mut edges = state.edges.write().await;
    for edge_id in cached_edge_ids.iter().chain(persisted_edge_ids.iter()) {
        edges.remove(edge_id);
    }
    let edge_count = edges.len();
    drop(edges);
    state.metrics.set_edges_cache_count(edge_count as i64);
    let mut nodes = state.nodes.write().await;
    nodes.remove(&id);
    state.metrics.set_nodes_cache_count(nodes.len() as i64);
    tracing::info!(event = "node.deleted.collective", node_id = %id, removed_edges = persisted_edge_ids.len(), "Node collectively deleted");
    let response = NodeDeleteResponse {
        node_id: id,
        node_state: "removed",
        removed_edge_ids: persisted_edge_ids,
        conversation,
    };
    Ok((StatusCode::OK, Json(response)))
}

pub async fn patch_node(
    State(state): State<ApiState>,
    Path(id): Path<String>,
    Json(payload): Json<UpdateNode>,
) -> Result<Json<Node>, NodeMutationError> {
    reject_node_patch_unless_writable(&state)
        .map_err(|(status, body)| NodeMutationError::Message(status, body))?;
    payload.validate().map_err(|error| {
        NodeMutationError::Message(
            StatusCode::BAD_REQUEST,
            format!(
                "invalid node patch request: {}",
                node_patch_error_message(&error)
            ),
        )
    })?;

    if state.config.domain_node_write_source == DomainNodeWriteSource::Postgres {
        return patch_node_postgres(&state, &id, payload).await;
    }

    patch_node_jsonl(state, id, payload).await
}

fn node_patch_owns_next_projection_generation(
    transaction_projection_version: Option<i64>,
    expected_projection_version: Option<i64>,
    handoff_guard_acquired: bool,
) -> bool {
    handoff_guard_acquired
        && matches!(
            (transaction_projection_version, expected_projection_version),
            (Some(transaction_version), Some(expected_version))
                if transaction_version == expected_version
        )
}

async fn patch_node_postgres(
    state: &ApiState,
    id: &str,
    payload: UpdateNode,
) -> Result<Json<Node>, NodeMutationError> {
    let pool = state
        .db_pool
        .as_ref()
        .ok_or(NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR))?;

    let patch = NodePatchInput {
        info: payload.info.clone(),
        search_visibility: payload.search_visibility.flatten(),
    };

    // Serialize DB patch + cache update in-process so a later committed patch cannot
    // be overwritten in the cache by an earlier request that resumes late after commit.
    // This is an in-process coherence guard, not a multi-instance cache invalidation mechanism.
    let _persist_guard = state.nodes_persist.lock().await;
    let projection_version_before = state.domain_projection_version.load(Ordering::Acquire);

    let expected_projection_version = projection_version_before.checked_add(1);
    let (node, (transaction_projection_version, projection_handoff_guard)) =
        patch_node_in_postgres_with_projection_precommit(pool, id, patch, |transaction_version| {
            let projection_handoff_guard = transaction_version.and_then(|transaction_version| {
                expected_projection_version
                    .filter(|expected| *expected == transaction_version)
                    .and_then(|version| state.begin_local_node_patch_projection_handoff(version))
            });
            (transaction_version, projection_handoff_guard)
        })
        .await
        .map_err(|e| match e {
            NodeWriteError::NotFound => NodeMutationError::Status(StatusCode::NOT_FOUND),
            NodeWriteError::InvalidEdgeReference(err) => {
                tracing::error!(%err, node_id = %id, "node patch blocked by edge reference integrity error");
                NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR)
            }
            NodeWriteError::ConversationNotEmpty => {
                tracing::error!(node_id = %id, "unexpected conversation guard during node patch");
                NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR)
            }
            NodeWriteError::ConversationLifecycle(err) => {
                tracing::error!(%err, node_id = %id, "unexpected conversation lifecycle error during node patch");
                NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR)
            }
            NodeWriteError::Mapping(err) => {
                tracing::error!(?err, node_id = %id, "node mapping failed after postgres patch");
                NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR)
            }
            NodeWriteError::Serialization(err) => {
                tracing::error!(?err, node_id = %id, "payload serialization failed during node patch");
                NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR)
            }
            NodeWriteError::Database(err) => {
                tracing::error!(?err, node_id = %id, "database error during node patch");
                NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR)
            }
        })?;

    // Publish the single-node cache change only when this transaction itself
    // proved it created exactly the next local generation. If an external
    // mutation already advanced PostgreSQL, inserting this node would mix the
    // new row with older cached rows and destroy the complete-snapshot invariant
    // that anonymous reads rely on while reconciliation is in flight.
    let owns_next_projection_generation = node_patch_owns_next_projection_generation(
        transaction_projection_version,
        expected_projection_version,
        projection_handoff_guard.is_some(),
    );
    if owns_next_projection_generation {
        let mut cache_guard = state.nodes.write().await;
        cache_guard.insert(id.to_string(), node.clone());
        state
            .metrics
            .set_nodes_cache_count(cache_guard.len() as i64);
        drop(cache_guard);
    } else {
        tracing::debug!(
            node_id = %id,
            projection_version_before,
            transaction_projection_version,
            "Preserved previous complete node cache because the PATCH did not own the next projection generation"
        );
    }

    // The domain_nodes outbox trigger increments the global projection version
    // exactly once for this row update. If PostgreSQL is now precisely V+1, no
    // other domain mutation raced with this request and the local cache update
    // above already represents the complete new generation. Fast-forward the
    // local version marker so the next mutation does not pay for an O(N) reload.
    // Any larger/unexpected jump is left stale on purpose; the normal refresh
    // path then reconciles external or concurrent writes.
    match domain_projection_version(pool).await {
        Ok(observed_version) => {
            if owns_next_projection_generation
                && transaction_projection_version == Some(observed_version)
            {
                match state.domain_projection_version.compare_exchange(
                    projection_version_before,
                    observed_version,
                    Ordering::AcqRel,
                    Ordering::Acquire,
                ) {
                    Ok(_) => {
                        state
                            .metrics
                            .set_domain_projection_version(observed_version);
                        tracing::debug!(
                            node_id = %id,
                            projection_version_before,
                            projection_version_after = observed_version,
                            "Fast-forwarded projection version after isolated PostgreSQL node patch"
                        );
                    }
                    Err(current_version) => {
                        tracing::debug!(
                            node_id = %id,
                            projection_version_before,
                            observed_version,
                            current_version,
                            "Skipped projection fast-forward because local generation advanced concurrently"
                        );
                    }
                }
            } else {
                tracing::debug!(
                    node_id = %id,
                    projection_version_before,
                    observed_version,
                    transaction_projection_version,
                    "Skipped projection fast-forward because the PATCH did not prove ownership of the observed generation"
                );
            }
        }
        Err(error) => {
            // The PostgreSQL patch is already committed and the local node cache
            // is updated. Do not turn that successful write into a false 500 just
            // because the optional fast-forward read failed; leaving the marker
            // stale makes the next request reconcile through the normal path.
            tracing::warn!(
                node_id = %id,
                error = %error,
                "Could not verify projection version after committed node patch; leaving projection stale"
            );
        }
    }

    drop(projection_handoff_guard);

    tracing::info!(node_id = %id, write_source = "postgres", "Node patch finished");

    Ok(Json(node))
}

async fn patch_node_jsonl(
    state: ApiState,
    id: String,
    payload: UpdateNode,
) -> Result<Json<Node>, NodeMutationError> {
    // Serialize PATCH persistence (per-process): allow concurrent node reads during file I/O;
    // only the brief in-memory cache write-lock blocks readers to guarantee read-your-writes in this instance.
    let start_persist_wait = std::time::Instant::now();
    let _persist_guard = state.nodes_persist.lock().await;
    let persist_lock_wait_ms = start_persist_wait.elapsed().as_millis();
    let maximum_bytes = jsonl_policy_limits()
        .await
        .map_err(|_| NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR))?
        .max_nodes_jsonl_bytes();

    let path = nodes_path();
    // Open source file for reading
    let file = File::open(&path)
        .await
        .map_err(|_| NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR))?;
    let mut lines = BufReader::new(file).lines();

    // Use a unique temporary file + rename for atomic writes to prevent data corruption and race conditions
    let mut tmp_path = path.clone();
    if let Some(filename) = tmp_path.file_name() {
        let mut new_filename = filename.to_os_string();
        new_filename.push(format!(".tmp.{}", Uuid::new_v4()));
        tmp_path.set_file_name(new_filename);
    } else {
        return Err(NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR));
    }

    let start_persist = std::time::Instant::now();

    // Inner function to handle processing and writing logic so we can catch errors and cleanup
    let process_result = async {
        let tmp_file = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .open(&tmp_path)
            .await
            .map_err(|_| NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR))?;

        let mut writer = BufWriter::new(tmp_file);
        let mut found_node: Option<Node> = None;
        let mut updated = false;

        while let Some(line) = lines
            .next_line()
            .await
            .map_err(|_| NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR))?
        {
            // Optimization: check ID without parsing full Value
            let should_update = match serde_json::from_str::<IdOnly>(&line) {
                Ok(obj) => obj.id.as_deref() == Some(id.as_str()),
                Err(_) => return Err(NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR)),
            };

            if should_update {
                let mut v: Value =
                    serde_json::from_str(&line).map_err(|_| NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR))?;
                let current_projection = map_json_to_node(&v)
                    .ok_or(NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR))?;

                // Compare the public node projection, not just raw JSON shape:
                // legacy rows without an explicit visibility already mean
                // `public` and therefore must not advance the version when a
                // client sends that same effective value.
                let mut has_changes = false;
                match &payload.info {
                    Some(Some(s)) => {
                        if current_projection.info.as_deref() != Some(s.as_str()) {
                            v["info"] = Value::String(s.clone());
                            has_changes = true;
                        }
                    }
                    Some(None) => {
                        if current_projection.info.is_some() {
                            v["info"] = Value::Null;
                            has_changes = true;
                        }
                    }
                    None => {} // No-op
                }

                if let Some(Some(v_enum)) = payload.search_visibility {
                    if current_projection.search_visibility != v_enum {
                        v["search_visibility"] = Value::String(v_enum.as_str().to_string());
                        has_changes = true;
                    }
                }

                // Clean up old "steckbrief" field if it exists (migration logic)
                if let Some(obj) = v.as_object_mut() {
                    if obj.remove("steckbrief").is_some() {
                        has_changes = true;
                    }
                }

                // Update updated_at only if we actually changed something
                if has_changes {
                    let now = chrono::Utc::now().to_rfc3339();
                    v["updated_at"] = Value::String(now);
                }

                // Map to Node and fail hard if mapping fails.
                // Ensures we never persist changes without a valid Node response.
                let node = map_json_to_node(&v).ok_or(NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR))?;

                // Security/Consistency: Ensure the ID hasn't been changed via the update.
                if node.id != id {
                    tracing::error!(path_id = %id, payload_id = %node.id, "Node ID mismatch in PATCH");
                    return Err(NodeMutationError::Status(StatusCode::BAD_REQUEST));
                }
                found_node = Some(node);

                let s = serde_json::to_string(&v).map_err(|_| NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR))?;
                writer
                    .write_all(s.as_bytes())
                    .await
                    .map_err(|_| NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR))?;
                updated = true;
            } else {
                writer
                    .write_all(line.as_bytes())
                    .await
                    .map_err(|_| NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR))?;
            }

            writer
                .write_all(b"\n")
                .await
                .map_err(|_| NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR))?;
        }

        if !updated {
            return Err(NodeMutationError::Status(StatusCode::NOT_FOUND));
        }

        writer
            .flush()
            .await
            .map_err(|_| NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR))?;

        // Ensure durability
        let file = writer.into_inner();
        ensure_jsonl_size(
            "nodes",
            file.metadata()
                .await
                .map_err(|_| NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR))?
                .len(),
            maximum_bytes,
        )
        .map_err(|error| NodeMutationError::Status(jsonl_write_status(&error)))?;
        file.sync_all()
            .await
            .map_err(|_| NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR))?;

        Ok(found_node)
    }
    .await;

    let found_node = match process_result {
        Ok(n) => n,
        Err(e) => {
            // Cleanup temp file on failure
            let _ = tokio::fs::remove_file(&tmp_path).await;
            return Err(e);
        }
    };

    if let Err(_e) = tokio::fs::rename(&tmp_path, &path).await {
        // Cleanup temp file if rename fails
        let _ = tokio::fs::remove_file(&tmp_path).await;
        return Err(NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR));
    }
    if let Some(parent) = path.parent() {
        sync_directory(parent).map_err(|error| {
            tracing::error!(%error, node_id = %id, "failed to sync node JSONL directory after patch");
            NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR)
        })?;
    }

    let persist_ms = start_persist.elapsed().as_millis();

    // Update in-memory cache
    let start_mem_wait = std::time::Instant::now();
    let mut cache_guard = state.nodes.write().await;
    let mem_lock_wait_ms = start_mem_wait.elapsed().as_millis();
    let start_mem_hold = std::time::Instant::now();

    if let Some(ref updated_node) = found_node {
        cache_guard.insert(id.clone(), updated_node.clone());
    }

    // Update metrics
    state
        .metrics
        .set_nodes_cache_count(cache_guard.len() as i64);

    let mem_lock_hold_ms = start_mem_hold.elapsed().as_millis();
    // Explicitly drop lock before logging to avoid holding it during tracing
    drop(cache_guard);

    tracing::info!(
        persist_ms,
        persist_lock_wait_ms,
        mem_lock_wait_ms,
        mem_lock_hold_ms,
        node_id = %id,
        node_found = found_node.is_some(),
        "Node patch finished"
    );

    found_node
        .map(Json)
        .ok_or(NodeMutationError::Status(StatusCode::INTERNAL_SERVER_ERROR))
}

/// Build a cursor envelope from rows already ordered by PostgreSQL.
///
/// `cursor_page` sorts in Rust. PostgreSQL text collation can differ from Rust
/// string ordering, so SQL-backed pagination must preserve the database order
/// used by both `ORDER BY id` and the next page's `id > cursor` predicate.
fn postgres_bbox_cursor_page(mut rows: Vec<Node>, limit: usize) -> CursorPage<Node> {
    let has_more = rows.len() > limit;
    if has_more {
        rows.truncate(limit);
    }
    let next_cursor = if has_more {
        rows.last().map(|node| encode_cursor(&node.id))
    } else {
        None
    };
    CursorPage {
        items: rows,
        page: PageMeta {
            limit,
            next_cursor,
            has_more,
        },
    }
}

pub async fn list_nodes(
    State(state): State<ApiState>,
    Query(params): Query<HashMap<String, String>>,
) -> Result<Json<ListResponse<Node>>, StatusCode> {
    let bbox = match params.get("bbox") {
        Some(raw_bbox) => Some(parse_bbox(raw_bbox).ok_or(StatusCode::BAD_REQUEST)?),
        None => None,
    };
    let limit: usize = parse_usize_param(&params, "limit", 100)?.min(MAX_PAGE_SIZE);
    let (cursor_mode, after_id) = parse_cursor_params(&params)?;
    validate_cursor_limit(cursor_mode, limit)?;

    if let Some(bb) = bbox
        .as_ref()
        .filter(|_| state.config.domain_read_source == DomainReadSource::Postgres)
    {
        let pool = state
            .db_pool
            .as_ref()
            .ok_or(StatusCode::INTERNAL_SERVER_ERROR)?;
        if cursor_mode {
            // Fetch one extra row so the existing wire contract can prove
            // has_more without counting or materialising the global dataset.
            // Initial and anchored pages use separate SQL statements so the
            // planner never has to optimise a nullable cursor `OR`.
            let rows = match after_id.as_deref() {
                Some(after_id) => {
                    load_nodes_bbox_after_id_from_postgres(
                        pool,
                        bb.min_lat,
                        bb.max_lat,
                        bb.min_lng,
                        bb.max_lng,
                        limit.saturating_add(1),
                        after_id,
                    )
                    .await
                }
                None => {
                    load_nodes_bbox_from_postgres(
                        pool,
                        bb.min_lat,
                        bb.max_lat,
                        bb.min_lng,
                        bb.max_lng,
                        limit.saturating_add(1),
                        0,
                    )
                    .await
                }
            }
            .map_err(|error| {
                tracing::error!(?error, "PostgreSQL node BBOX read failed");
                StatusCode::INTERNAL_SERVER_ERROR
            })?;
            let page = postgres_bbox_cursor_page(rows, limit);
            return Ok(Json(ListResponse::Cursor(page)));
        }

        let offset: usize = parse_usize_param(&params, "offset", 0)?;
        // PostgreSQL OFFSET is a signed 64-bit value. Treat a client-supplied
        // value outside that wire range as bad input instead of surfacing it as
        // an internal database/read failure.
        let offset = i64::try_from(offset).map_err(|_| StatusCode::BAD_REQUEST)?;
        let rows = load_nodes_bbox_from_postgres(
            pool, bb.min_lat, bb.max_lat, bb.min_lng, bb.max_lng, limit, offset,
        )
        .await
        .map_err(|error| {
            tracing::error!(?error, "PostgreSQL legacy node BBOX read failed");
            StatusCode::INTERNAL_SERVER_ERROR
        })?;
        return Ok(Json(ListResponse::Legacy(rows)));
    }

    let cache = state.nodes.read().await;

    if cursor_mode {
        // Cursor mode sorts by stable id ascending (see query::cursor_page),
        // independent of the file/insertion order used by the legacy path.
        let refs: Vec<&Node> = cache
            .iter_in_order()
            .filter(|node| match &bbox {
                Some(bb) => point_in_bbox(node.location.lon, node.location.lat, bb),
                None => true,
            })
            .collect();
        let page = cursor_page(
            refs,
            limit,
            after_id.as_deref(),
            |node: &Node| node.id.as_str(),
            |node: &Node| node.clone(),
        );
        Ok(Json(ListResponse::Cursor(page)))
    } else {
        let offset: usize = parse_usize_param(&params, "offset", 0)?;
        let out: Vec<Node> = cache
            .iter_in_order()
            .filter(|node| match &bbox {
                Some(bb) => point_in_bbox(node.location.lon, node.location.lat, bb),
                None => true,
            })
            .skip(offset)
            .take(limit)
            .cloned()
            .collect();
        Ok(Json(ListResponse::Legacy(out)))
    }
}

#[cfg(test)]
mod postgres_node_patch_projection_ownership_tests {
    use super::node_patch_owns_next_projection_generation;

    #[test]
    fn exact_v_plus_one_requires_the_acquired_handoff_guard() {
        assert!(!node_patch_owns_next_projection_generation(
            Some(42),
            Some(42),
            false,
        ));
        assert!(node_patch_owns_next_projection_generation(
            Some(42),
            Some(42),
            true,
        ));
        assert!(!node_patch_owns_next_projection_generation(
            Some(43),
            Some(42),
            true,
        ));
    }
}

#[cfg(test)]
mod postgres_bbox_cursor_page_tests {
    use super::*;

    fn node(id: &str) -> Node {
        map_json_to_node(&json!({
            "id": id,
            "kind": "place",
            "title": id,
            "location": {"lat": 53.5, "lon": 9.9}
        }))
        .expect("test node")
    }

    #[test]
    fn preserves_database_order_for_collation_sensitive_text_ids() {
        // Treat this vector as a database result from a locale-aware collation.
        // Rust byte/string ordering would put `rp-a-c` before `rp-ab`.
        let page = postgres_bbox_cursor_page(vec![node("rp-ab"), node("rp-a-c"), node("rp-z")], 2);

        assert_eq!(
            page.items
                .iter()
                .map(|node| node.id.as_str())
                .collect::<Vec<_>>(),
            vec!["rp-ab", "rp-a-c"]
        );
        assert!(page.page.has_more);
        assert_eq!(page.page.next_cursor, Some(encode_cursor("rp-a-c")));
    }
}

#[cfg(test)]
mod node_mutation_error_response_tests {
    use super::*;
    use axum::body;

    #[tokio::test]
    async fn conversation_history_guard_has_a_stable_json_problem_code() {
        let response = map_postgres_node_delete_error(NodeWriteError::ConversationNotEmpty, "n1")
            .into_response();

        assert_eq!(response.status(), StatusCode::CONFLICT);
        assert_eq!(
            response
                .headers()
                .get(axum::http::header::CONTENT_TYPE)
                .and_then(|value| value.to_str().ok()),
            Some("application/json")
        );

        let body = body::to_bytes(response.into_body(), 4096)
            .await
            .expect("problem body");
        let payload: Value = serde_json::from_slice(&body).expect("problem JSON");
        assert_eq!(payload["code"], "node_conversation_not_empty");
        assert_eq!(
            payload["message"],
            "node deletion is blocked because its public conversation contains contributions"
        );
    }
}

#[cfg(test)]
mod search_visibility_mapping_tests {
    use super::*;

    fn node_fixture() -> Value {
        json!({
            "id": "visibility-fixture",
            "kind": "Werkstatt",
            "title": "Sichtbarkeit",
            "location": {"lat": 54.0, "lon": 8.0}
        })
    }

    #[test]
    fn jsonl_visibility_defaults_only_when_absent_and_malformed_values_fail_closed() {
        let missing = map_json_to_node(&node_fixture()).expect("missing visibility fixture");
        assert_eq!(missing.search_visibility, SearchVisibility::Public);

        let mut malformed = node_fixture();
        malformed["search_visibility"] = json!("unexpected");
        let malformed = map_json_to_node(&malformed).expect("malformed visibility fixture");
        assert_eq!(malformed.search_visibility, SearchVisibility::Hidden);

        let mut wrong_type = node_fixture();
        wrong_type["search_visibility"] = json!({"scope": "public"});
        let wrong_type = map_json_to_node(&wrong_type).expect("wrong-type visibility fixture");
        assert_eq!(wrong_type.search_visibility, SearchVisibility::Hidden);
    }
}

#[cfg(test)]
mod node_activity_faden_tests {
    use super::*;

    #[test]
    fn operation_ids_are_deterministic_and_relation_scoped() {
        let account = "10000000-0000-4000-8000-000000000001";
        let node = "20000000-0000-4000-8000-000000000001";
        let first = node_activity_faden_operation_id(
            super::super::edges::FadenType::Knotting,
            account,
            node,
            node,
        );
        let replay = node_activity_faden_operation_id(
            super::super::edges::FadenType::Knotting,
            account,
            node,
            node,
        );
        let conversation = node_activity_faden_operation_id(
            super::super::edges::FadenType::Conversation,
            account,
            node,
            "30000000-0000-4000-8000-000000000001",
        );

        assert_eq!(first, replay);
        assert_ne!(first, conversation);
        assert!(Uuid::parse_str(&first).is_ok());
    }

    #[test]
    fn length_prefixes_keep_ambiguous_components_distinct() {
        let left = node_activity_faden_operation_id(
            super::super::edges::FadenType::Knotting,
            "ab",
            "c",
            "d",
        );
        let right = node_activity_faden_operation_id(
            super::super::edges::FadenType::Knotting,
            "a",
            "bc",
            "d",
        );
        assert_ne!(left, right);
    }
}

#[cfg(test)]
mod node_delete_journal_tests {
    use super::*;
    use crate::test_helpers::EnvGuard;
    use serial_test::serial;
    use std::fs;

    fn set_gewebe_in_dir(path: &FsPath) -> EnvGuard {
        EnvGuard::set(
            "GEWEBE_IN_DIR",
            path.to_str()
                .expect("test GEWEBE_IN_DIR path must be valid UTF-8"),
        )
    }

    fn write_fixture(path: &FsPath, content: &str) {
        fs::create_dir_all(path.parent().expect("fixture parent")).expect("create fixture dir");
        fs::write(path, content).expect("write fixture");
    }

    fn assert_no_transaction_files(dir: &FsPath) {
        let entries = fs::read_dir(dir)
            .expect("read data dir")
            .map(|entry| entry.expect("dir entry").file_name())
            .map(|name| name.to_string_lossy().to_string())
            .collect::<Vec<_>>();
        assert!(
            entries.iter().all(|name| {
                !name.contains(".delete.")
                    && name != NODE_DELETE_JOURNAL_FILE
                    && !name.ends_with(".tmp")
                    && !name.ends_with(".bak")
            }),
            "unexpected transaction files: {entries:?}"
        );
    }

    #[tokio::test]
    #[serial]
    async fn jsonl_node_delete_rolls_back_when_node_swap_fails_after_edge_temp() {
        let tmp = tempfile::tempdir().expect("tmpdir");
        let in_dir = tmp.path().join("in");
        let nodes_path = in_dir.join("demo.nodes.jsonl");
        let edges_path = in_dir.join("demo.edges.jsonl");
        let nodes_original = concat!(
            r#"{"id":"n1","kind":"Werkstatt","title":"Alt","location":{"lat":53.5,"lon":10.0}}"#,
            "\n",
            r#"{"id":"n2","kind":"Ort","title":"Ziel","location":{"lat":53.6,"lon":10.1}}"#,
            "\n",
        );
        let edges_original = concat!(
            r#"{"id":"e1","source_id":"n1","source_type":"node","target_id":"n2","target_type":"node","edge_kind":"reference"}"#,
            "\n",
        );
        write_fixture(&nodes_path, nodes_original);
        write_fixture(&edges_path, edges_original);
        let _env = set_gewebe_in_dir(&in_dir);

        let error = delete_node_and_edges_jsonl_with_injection(
            "n1",
            EdgeEndpointCollisionEvidence::default(),
            JsonlNodeDeleteFailureInjection {
                fail_before_node_swap: true,
                ..JsonlNodeDeleteFailureInjection::default()
            },
        )
        .await
        .expect_err("injected node swap failure must fail");

        assert_eq!(error.kind(), std::io::ErrorKind::Other);
        assert_eq!(
            fs::read_to_string(&nodes_path).expect("nodes"),
            nodes_original
        );
        assert_eq!(
            fs::read_to_string(&edges_path).expect("edges"),
            edges_original
        );
        assert_no_transaction_files(&in_dir);
    }

    #[tokio::test]
    #[serial]
    async fn jsonl_node_delete_recovery_rolls_back_crash_between_swaps() {
        let tmp = tempfile::tempdir().expect("tmpdir");
        let in_dir = tmp.path().join("in");
        let nodes_path = in_dir.join("demo.nodes.jsonl");
        let edges_path = in_dir.join("demo.edges.jsonl");
        let nodes_original = concat!(
            r#"{"id":"n1","kind":"Werkstatt","title":"Alt","location":{"lat":53.5,"lon":10.0}}"#,
            "\n",
            r#"{"id":"n2","kind":"Ort","title":"Ziel","location":{"lat":53.6,"lon":10.1}}"#,
            "\n",
        );
        let edges_original = concat!(
            r#"{"id":"e1","source_id":"n1","source_type":"node","target_id":"n2","target_type":"node","edge_kind":"reference"}"#,
            "\n",
            r#"{"id":"e2","source_id":"n2","source_type":"node","target_id":"n3","target_type":"node","edge_kind":"reference"}"#,
            "\n",
        );
        write_fixture(&nodes_path, nodes_original);
        write_fixture(&edges_path, edges_original);
        let _env = set_gewebe_in_dir(&in_dir);

        let error = delete_node_and_edges_jsonl_with_injection(
            "n1",
            EdgeEndpointCollisionEvidence::default(),
            JsonlNodeDeleteFailureInjection {
                crash_after_edge_swap: true,
                ..JsonlNodeDeleteFailureInjection::default()
            },
        )
        .await
        .expect_err("injected crash must leave recovery work");

        assert_eq!(error.kind(), std::io::ErrorKind::Interrupted);
        assert!(
            in_dir.join(NODE_DELETE_JOURNAL_FILE).exists(),
            "journal must survive the simulated crash"
        );
        assert!(
            !nodes_path.exists(),
            "simulated crash point is between edge and node swaps"
        );

        recover_node_delete_jsonl_journal()
            .await
            .expect("recovery must roll back before the durable node swap marker");

        assert_eq!(
            fs::read_to_string(&nodes_path).expect("nodes after recovery"),
            nodes_original
        );
        assert_eq!(
            fs::read_to_string(&edges_path).expect("edges after recovery"),
            edges_original
        );
        assert_no_transaction_files(&in_dir);
    }

    #[tokio::test]
    #[serial]
    async fn jsonl_node_delete_returns_success_when_committed_cleanup_is_deferred() {
        let tmp = tempfile::tempdir().expect("tmpdir");
        let in_dir = tmp.path().join("in");
        let nodes_path = in_dir.join("demo.nodes.jsonl");
        let edges_path = in_dir.join("demo.edges.jsonl");
        write_fixture(
            &nodes_path,
            concat!(
                r#"{"id":"n1","kind":"Werkstatt","title":"Alt","location":{"lat":53.5,"lon":10.0}}"#,
                "\n",
                r#"{"id":"n2","kind":"Ort","title":"Ziel","location":{"lat":53.6,"lon":10.1}}"#,
                "\n",
            ),
        );
        write_fixture(
            &edges_path,
            concat!(
                r#"{"id":"e1","source_id":"n1","source_type":"node","target_id":"n2","target_type":"node","edge_kind":"reference"}"#,
                "\n",
                r#"{"id":"e2","source_id":"n2","source_type":"node","target_id":"n3","target_type":"node","edge_kind":"reference"}"#,
                "\n",
            ),
        );
        let _env = set_gewebe_in_dir(&in_dir);

        let outcome = delete_node_and_edges_jsonl_with_injection(
            "n1",
            EdgeEndpointCollisionEvidence::default(),
            JsonlNodeDeleteFailureInjection {
                defer_cleanup_after_node_swap: true,
                ..JsonlNodeDeleteFailureInjection::default()
            },
        )
        .await
        .expect("durably committed delete must remain successful");

        assert!(outcome.node_deleted);
        assert_eq!(outcome.removed_edge_ids, vec!["e1"]);
        assert!(in_dir.join(NODE_DELETE_JOURNAL_FILE).exists());
        let committed_nodes = fs::read_to_string(&nodes_path).expect("committed nodes");
        let committed_edges = fs::read_to_string(&edges_path).expect("committed edges");
        assert!(!committed_nodes.contains(r#""id":"n1""#));
        assert!(committed_nodes.contains(r#""id":"n2""#));
        assert!(!committed_edges.contains(r#""id":"e1""#));
        assert!(committed_edges.contains(r#""id":"e2""#));

        recover_node_delete_jsonl_journal()
            .await
            .expect("startup recovery must finish committed cleanup");

        assert_eq!(
            fs::read_to_string(&nodes_path).expect("nodes after cleanup"),
            committed_nodes
        );
        assert_eq!(
            fs::read_to_string(&edges_path).expect("edges after cleanup"),
            committed_edges
        );
        assert_no_transaction_files(&in_dir);
    }

    #[tokio::test]
    #[serial]
    async fn jsonl_node_delete_rejects_malformed_edge_without_partial_mutation() {
        let tmp = tempfile::tempdir().expect("tmpdir");
        let in_dir = tmp.path().join("in");
        let nodes_path = in_dir.join("demo.nodes.jsonl");
        let edges_path = in_dir.join("demo.edges.jsonl");
        let nodes_original = concat!(
            r#"{"id":"n1","kind":"Werkstatt","title":"Alt","location":{"lat":53.5,"lon":10.0}}"#,
            "\n",
        );
        let edges_original = concat!(
            r#"{"id":"e1","source_id":"n1","source_type":"node","target_id":"n2","target_type":"node","edge_kind":"reference"}"#,
            "\n",
            r#"{"id":"broken""#,
            "\n",
        );
        write_fixture(&nodes_path, nodes_original);
        write_fixture(&edges_path, edges_original);
        let _env = set_gewebe_in_dir(&in_dir);

        let error = delete_node_and_edges_jsonl("n1", EdgeEndpointCollisionEvidence::default())
            .await
            .expect_err("malformed edge persistence must block destructive delete");

        assert_eq!(error.kind(), std::io::ErrorKind::InvalidData);
        assert!(error.to_string().contains("invalid edge JSONL at line 2"));
        assert_eq!(
            fs::read_to_string(&nodes_path).expect("nodes"),
            nodes_original
        );
        assert_eq!(
            fs::read_to_string(&edges_path).expect("edges"),
            edges_original
        );
        assert_no_transaction_files(&in_dir);
    }

    #[tokio::test]
    #[serial]
    async fn jsonl_node_delete_streams_large_edge_file_and_preserves_untouched_lines() {
        let tmp = tempfile::tempdir().expect("tmpdir");
        let in_dir = tmp.path().join("in");
        let nodes_path = in_dir.join("demo.nodes.jsonl");
        let edges_path = in_dir.join("demo.edges.jsonl");
        let surviving_node =
            r#"{"id":"n2","kind":"Ort","title":"Ziel","location":{"lat":53.6,"lon":10.1}}"#;
        let nodes_original = format!(
            "{}\n{}\n",
            r#"{"id":"n1","kind":"Werkstatt","title":"Alt","location":{"lat":53.5,"lon":10.0}}"#,
            surviving_node,
        );
        let mut edges_original = String::new();
        let mut expected_edges = String::new();
        for index in 0..5_000usize {
            let source_id = if index == 2_500 { "n1" } else { "n2" };
            let line = format!(
                r#"{{ "id": "e-{index:04}", "source_id": "{source_id}", "source_type": "node", "target_id": "n3", "target_type": "node", "edge_kind": "reference" }}"#,
            );
            edges_original.push_str(&line);
            edges_original.push('\n');
            if index != 2_500 {
                expected_edges.push_str(&line);
                expected_edges.push('\n');
            }
        }
        write_fixture(&nodes_path, &nodes_original);
        write_fixture(&edges_path, &edges_original);
        let _env = set_gewebe_in_dir(&in_dir);

        let outcome = delete_node_and_edges_jsonl("n1", EdgeEndpointCollisionEvidence::default())
            .await
            .expect("large streaming delete must succeed");

        assert!(outcome.node_deleted);
        assert_eq!(outcome.removed_edge_ids, vec!["e-2500"]);
        assert_eq!(
            fs::read_to_string(&nodes_path).expect("nodes after delete"),
            format!("{surviving_node}\n")
        );
        assert_eq!(
            fs::read_to_string(&edges_path).expect("edges after delete"),
            expected_edges,
            "untouched edge lines must remain byte-for-byte stable"
        );
        assert_no_transaction_files(&in_dir);
    }
}
