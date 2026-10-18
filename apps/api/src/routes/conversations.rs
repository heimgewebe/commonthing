//! Public node conversations backed exclusively by PostgreSQL.
//!
//! Every PostgreSQL node owns exactly one conversation through the migration
//! trigger. Active private or hidden nodes restrict conversation reads to
//! their owner; deleted-node archives retain the existing public contract. Creating, editing and tombstoning
//! messages uses the existing Weber/Admin write gate in `routes::api_router`;
//! this module additionally enforces author-or-admin ownership.
//!
//! Message creation is limited by a PostgreSQL-backed rolling minute window.
//! A transaction-scoped advisory lock serializes the count-and-insert decision
//! per conversation and author across API replicas. Idempotent replays are
//! resolved before the limit is evaluated, so a safe retry never consumes a
//! second allowance or becomes spuriously rate-limited.

use axum::{
    extract::{Path, Query, State},
    http::{
        header::{IF_MATCH, RETRY_AFTER},
        HeaderMap, HeaderValue, StatusCode,
    },
    response::{IntoResponse, Response},
    Extension, Json,
};
use chrono::{DateTime, SecondsFormat, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sqlx::{PgPool, Postgres, Transaction};
use uuid::Uuid;

use crate::{
    advisory_lock::node_mutation_lock_key, auth::role::Role, config::DomainReadSource,
    middleware::auth::AuthContext, state::ApiState,
};

use super::{
    nodes::{ensure_node_activity_faden, reader_account_id, repair_node_activity_faden},
    query::{decode_cursor, encode_cursor},
    webgemeindezentren::{
        ensure_webgemeindezentrum_activity_faden, repair_webgemeindezentrum_activity_faden,
    },
};

const DEFAULT_MESSAGE_PAGE_SIZE: usize = 20;
const MAX_MESSAGE_PAGE_SIZE: usize = 50;
const MAX_MESSAGE_LENGTH: usize = 4_000;
const MESSAGE_RATE_LIMIT_PER_MINUTE: i64 = 10;
const DIRECT_MESSAGE_RATE_LIMIT_PER_MINUTE: i64 = 30;
const DIRECT_CONVERSATION_CREATE_LIMIT_PER_HOUR: i64 = 20;

type ConversationRow = (
    String,
    Option<String>,
    Option<String>,
    Option<String>,
    String,
    String,
    DateTime<Utc>,
    DateTime<Utc>,
    Option<DateTime<Utc>>,
    Option<DateTime<Utc>>,
);

type MessageRow = (
    String,
    String,
    Option<String>,
    String,
    Option<String>,
    DateTime<Utc>,
    DateTime<Utc>,
    Option<DateTime<Utc>>,
);

type DirectConversationRow = (
    String,
    Option<String>,
    String,
    DateTime<Utc>,
    DateTime<Utc>,
    i64,
    Option<String>,
    Option<DateTime<Utc>>,
    bool,
    bool,
);

/// Projection of one private conversation as seen by exactly one participant.
///
/// `$1` binds the reading account. The counterpart is exposed as a live account
/// only while that account still exists and is active; a deleted or deactivated
/// counterpart collapses to the immutable title snapshot, which is the same
/// boundary [`require_direct_message_send_allowed`] enforces on the write path.
///
/// `can_send` folds every reason that blocks new messages — counterpart gone and
/// either side's block — into one flag, so the client never has to guess whether
/// the composer is usable. It deliberately does not disclose which side blocked.
const DIRECT_CONVERSATION_PROJECTION: &str = "\
         SELECT conversation.id::text,
                CASE WHEN counterpart_account.id IS NULL THEN NULL ELSE counterpart.account_id END,
                COALESCE(counterpart_account.title, counterpart.account_title_snapshot),
                conversation.created_at,
                conversation.updated_at,
                (
                    SELECT count(*)::bigint
                    FROM domain_messages AS unread
                    WHERE unread.conversation_id = conversation.id
                      AND unread.deleted_at IS NULL
                      AND unread.author_account_id IS DISTINCT FROM $1
                      AND unread.created_at > mine.last_read_at
                ),
                left(latest.content, 160),
                latest.created_at,
                mine.blocked_at IS NOT NULL,
                counterpart_account.id IS NOT NULL
                    AND mine.blocked_at IS NULL
                    AND counterpart.blocked_at IS NULL
         FROM domain_conversations AS conversation
         JOIN domain_direct_conversation_participants AS mine
           ON mine.conversation_id = conversation.id
         JOIN domain_direct_conversation_participants AS counterpart
           ON counterpart.conversation_id = conversation.id
          AND counterpart.slot <> mine.slot
         LEFT JOIN domain_accounts AS counterpart_account
           ON counterpart_account.id = counterpart.account_id
          AND counterpart_account.disabled = FALSE
         LEFT JOIN LATERAL (
             SELECT message.content, message.created_at
             FROM domain_messages AS message
             WHERE message.conversation_id = conversation.id
               AND message.deleted_at IS NULL
             ORDER BY message.created_at DESC, message.id DESC
             LIMIT 1
         ) AS latest ON TRUE
         WHERE conversation.conversation_type = 'direct'
           AND conversation.visibility = 'participants'
           AND conversation.deleted_at IS NULL
           AND mine.account_id = $1";

#[derive(Debug, Clone, Serialize)]
pub struct ConversationView {
    pub id: String,
    pub conversation_type: String,
    pub lifecycle_state: String,
    pub node_id: Option<String>,
    pub node_id_snapshot: Option<String>,
    pub node_title_snapshot: Option<String>,
    pub visibility: String,
    pub created_at: String,
    pub updated_at: String,
    pub archived_at: Option<String>,
    pub deleted_at: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct MessageView {
    pub id: String,
    pub conversation_id: String,
    pub author_account_id: Option<String>,
    pub author_title: String,
    pub content: Option<String>,
    pub created_at: String,
    pub updated_at: String,
    pub deleted_at: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct MessagePage {
    pub items: Vec<MessageView>,
    pub page: MessagePageMeta,
}

#[derive(Debug, Serialize)]
pub struct MessagePageMeta {
    pub limit: usize,
    pub next_cursor: Option<String>,
    pub has_more: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct DirectConversationView {
    pub id: String,
    pub counterpart_account_id: Option<String>,
    pub counterpart_title: String,
    pub created_at: String,
    pub updated_at: String,
    pub unread_count: i64,
    pub last_message_preview: Option<String>,
    pub last_message_at: Option<String>,
    pub blocked_by_me: bool,
    pub can_send: bool,
}

#[derive(Debug, Serialize)]
pub struct DirectConversationList {
    pub items: Vec<DirectConversationView>,
}

#[derive(Debug)]
pub struct ConversationApiError {
    status: StatusCode,
    code: &'static str,
    message: &'static str,
    current: Option<Value>,
    retry_after_seconds: Option<u64>,
}

impl ConversationApiError {
    fn new(status: StatusCode, code: &'static str, message: &'static str) -> Self {
        Self {
            status,
            code,
            message,
            current: None,
            retry_after_seconds: None,
        }
    }

    fn current(mut self, message: &MessageView) -> Self {
        self.current = Some(json!(message));
        self
    }

    fn retry_after(mut self, seconds: u64) -> Self {
        self.retry_after_seconds = Some(seconds);
        self
    }
}

impl IntoResponse for ConversationApiError {
    fn into_response(self) -> Response {
        let retry_after_seconds = self.retry_after_seconds;
        let mut body = json!({
            "code": self.code,
            "message": self.message,
        });
        if let Some(current) = self.current {
            body["current"] = current;
        }
        let mut response = (self.status, Json(body)).into_response();
        if let Some(seconds) = retry_after_seconds {
            if let Ok(value) = HeaderValue::from_str(&seconds.to_string()) {
                response.headers_mut().insert(RETRY_AFTER, value);
            }
        }
        response
    }
}

fn timestamp(value: DateTime<Utc>) -> String {
    value.to_rfc3339_opts(SecondsFormat::AutoSi, true)
}

fn conversation_from_row(row: ConversationRow) -> ConversationView {
    let lifecycle_state = if row.8.is_some() {
        "archived"
    } else {
        "active"
    };
    ConversationView {
        id: row.0,
        conversation_type: row.4,
        lifecycle_state: lifecycle_state.to_string(),
        node_id: row.1,
        node_id_snapshot: row.2,
        node_title_snapshot: row.3,
        visibility: row.5,
        created_at: timestamp(row.6),
        updated_at: timestamp(row.7),
        archived_at: row.8.map(timestamp),
        deleted_at: row.9.map(timestamp),
    }
}

fn message_from_row(row: MessageRow) -> MessageView {
    MessageView {
        id: row.0,
        conversation_id: row.1,
        author_account_id: row.2,
        author_title: row.3,
        content: row.4,
        created_at: timestamp(row.5),
        updated_at: timestamp(row.6),
        deleted_at: row.7.map(timestamp),
    }
}

fn direct_conversation_from_row(row: DirectConversationRow) -> DirectConversationView {
    DirectConversationView {
        id: row.0,
        counterpart_account_id: row.1,
        counterpart_title: row.2,
        created_at: timestamp(row.3),
        updated_at: timestamp(row.4),
        unread_count: row.5,
        last_message_preview: row.6,
        last_message_at: row.7.map(timestamp),
        blocked_by_me: row.8,
        can_send: row.9,
    }
}

fn direct_pair_key(left: &str, right: &str) -> String {
    let (first, second) = if left <= right {
        (left, right)
    } else {
        (right, left)
    };
    format!("{}:{first}{second}", first.len())
}

fn database_error(context: &'static str, error: sqlx::Error) -> ConversationApiError {
    tracing::error!(%error, context, "node conversation database operation failed");
    ConversationApiError::new(
        StatusCode::INTERNAL_SERVER_ERROR,
        "conversation_database_error",
        "the conversation store could not complete the request",
    )
}

fn require_pool(state: &ApiState) -> Result<&PgPool, ConversationApiError> {
    if state.config.domain_read_source != DomainReadSource::Postgres {
        return Err(ConversationApiError::new(
            StatusCode::SERVICE_UNAVAILABLE,
            "conversation_store_unavailable",
            "node conversations require the canonical PostgreSQL domain source",
        ));
    }
    state.db_pool.as_ref().ok_or_else(|| {
        ConversationApiError::new(
            StatusCode::SERVICE_UNAVAILABLE,
            "conversation_store_unavailable",
            "node conversations require an available PostgreSQL connection",
        )
    })
}

fn account_id(auth: &AuthContext) -> Result<&str, ConversationApiError> {
    auth.account_id.as_deref().ok_or_else(|| {
        ConversationApiError::new(
            StatusCode::UNAUTHORIZED,
            "authentication_required",
            "an authenticated account is required",
        )
    })
}

async fn project_message_participation_faden(
    state: &ApiState,
    auth: &AuthContext,
    node_id: Option<&str>,
    webgemeindezentrum_id: Option<&str>,
    conversation_id: &str,
    message_id: &str,
    projection_mode: super::edges::FadenProjectionMode,
) {
    if let Some(node_id) = node_id {
        let projection = match projection_mode {
            super::edges::FadenProjectionMode::Reactivate => {
                ensure_node_activity_faden(
                    state,
                    auth,
                    node_id,
                    super::edges::FadenType::Conversation,
                    conversation_id,
                    message_id,
                )
                .await
            }
            super::edges::FadenProjectionMode::EnsureOnly => {
                repair_node_activity_faden(
                    state,
                    auth,
                    node_id,
                    super::edges::FadenType::Conversation,
                    conversation_id,
                    message_id,
                )
                .await
            }
        };
        if let Err((status, message)) = projection {
            tracing::error!(
                event = "conversation.message_faden_projection.failed",
                node_id,
                message_id,
                %status,
                error = %message,
                "Message remains durable; the derived node Faden is missing"
            );
        }
        return;
    }

    if let Some(center_id) = webgemeindezentrum_id {
        let projection = match projection_mode {
            super::edges::FadenProjectionMode::Reactivate => {
                ensure_webgemeindezentrum_activity_faden(
                    state,
                    auth,
                    center_id,
                    super::edges::FadenType::Conversation,
                    conversation_id,
                )
                .await
            }
            super::edges::FadenProjectionMode::EnsureOnly => {
                repair_webgemeindezentrum_activity_faden(
                    state,
                    auth,
                    center_id,
                    super::edges::FadenType::Conversation,
                    conversation_id,
                )
                .await
            }
        };
        if let Err((status, message)) = projection {
            tracing::error!(
                event = "conversation.center_message_faden_projection.failed",
                center_id,
                message_id,
                %status,
                error = %message,
                "Message remains durable; the derived center Faden is missing"
            );
        }
    }
}

fn validate_content(content: &str) -> Result<String, ConversationApiError> {
    let content = content.trim();
    let length = content.chars().count();
    if length == 0 || length > MAX_MESSAGE_LENGTH {
        return Err(ConversationApiError::new(
            StatusCode::BAD_REQUEST,
            "invalid_message_content",
            "message content must contain between 1 and 4000 characters",
        ));
    }
    Ok(content.to_string())
}

fn validate_author_title(title: &str) -> Result<String, ConversationApiError> {
    let title = title.trim();
    let length = title.chars().count();
    if length == 0 || length > 200 {
        return Err(ConversationApiError::new(
            StatusCode::CONFLICT,
            "author_title_invalid",
            "the canonical account title must contain between 1 and 200 characters",
        ));
    }
    Ok(title.to_string())
}

fn parse_conversation_id(value: &str) -> Result<String, ConversationApiError> {
    Uuid::parse_str(value)
        .map(|id| id.to_string())
        .map_err(|_| {
            ConversationApiError::new(
                StatusCode::NOT_FOUND,
                "conversation_not_found",
                "the node conversation does not exist",
            )
        })
}

fn parse_message_id(value: &str) -> Result<String, ConversationApiError> {
    Uuid::parse_str(value)
        .map(|id| id.to_string())
        .map_err(|_| {
            ConversationApiError::new(
                StatusCode::NOT_FOUND,
                "message_not_found",
                "the message does not exist in this conversation",
            )
        })
}

fn parse_idempotency_key(headers: &HeaderMap) -> Result<String, ConversationApiError> {
    let raw = headers
        .get("Idempotency-Key")
        .and_then(|value| value.to_str().ok())
        .ok_or_else(|| {
            ConversationApiError::new(
                StatusCode::BAD_REQUEST,
                "idempotency_key_required",
                "Idempotency-Key must be a UUID",
            )
        })?;
    Uuid::parse_str(raw)
        .map(|value| value.to_string())
        .map_err(|_| {
            ConversationApiError::new(
                StatusCode::BAD_REQUEST,
                "invalid_idempotency_key",
                "Idempotency-Key must be a UUID",
            )
        })
}

fn check_precondition(
    headers: &HeaderMap,
    current: &MessageView,
) -> Result<(), ConversationApiError> {
    let expected = format!("\"{}\"", current.updated_at);
    match headers.get(IF_MATCH).and_then(|value| value.to_str().ok()) {
        Some(provided) if provided == expected => Ok(()),
        Some(_) => Err(ConversationApiError::new(
            StatusCode::PRECONDITION_FAILED,
            "message_version_conflict",
            "the message was changed by another request",
        )
        .current(current)),
        None => Err(ConversationApiError::new(
            StatusCode::PRECONDITION_REQUIRED,
            "message_version_required",
            "If-Match with the current message updated_at is required",
        )
        .current(current)),
    }
}

// Authorization and response projection MUST observe one PostgreSQL snapshot.
// Otherwise a node can become private and receive a new message between the
// public authorization query and the later message-page query.
async fn begin_consistent_conversation_read(
    pool: &PgPool,
) -> Result<Transaction<'_, Postgres>, ConversationApiError> {
    let mut tx = pool
        .begin()
        .await
        .map_err(|error| database_error("begin conversation read snapshot", error))?;
    sqlx::query("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        .execute(&mut *tx)
        .await
        .map_err(|error| database_error("configure conversation read snapshot", error))?;
    Ok(tx)
}

async fn require_conversation_read_access(
    tx: &mut Transaction<'_, Postgres>,
    conversation_id: &str,
    auth: &AuthContext,
) -> Result<(), ConversationApiError> {
    let target: Option<(String, String, Option<String>, Option<String>)> = sqlx::query_as(
        "SELECT conversation_type, visibility, node_id, archived_node_visibility FROM domain_conversations \
         WHERE id = $1::uuid AND deleted_at IS NULL",
    )
    .bind(conversation_id)
    .fetch_optional(&mut **tx)
    .await
    .map_err(|error| database_error("classify conversation read target", error))?;

    let Some((conversation_type, visibility, node_id, archived_node_visibility)) = target else {
        return Err(ConversationApiError::new(
            StatusCode::NOT_FOUND,
            "conversation_not_found",
            "the conversation does not exist",
        ));
    };

    if visibility == "public" {
        if conversation_type == "node" {
            if let Some(node_id) = node_id.as_deref() {
                let allowed: bool = sqlx::query_scalar(
                    "SELECT EXISTS(SELECT 1 FROM domain_nodes n
                      WHERE n.id = $1 AND
                       (n.search_visibility = 'public' OR
                        (n.search_visibility IN ('private','hidden')
                         AND $2::text IS NOT NULL
                         AND weltgewebe_search_node_owner_account_id(n.payload) = $2::text)))",
                )
                .bind(node_id)
                .bind(reader_account_id(Some(auth)))
                .fetch_one(&mut **tx)
                .await
                .map_err(|error| database_error("authorize node conversation read", error))?;
                if !allowed {
                    return Err(ConversationApiError::new(
                        StatusCode::NOT_FOUND,
                        "conversation_not_found",
                        "the conversation does not exist",
                    ));
                }
            } else if archived_node_visibility.as_deref() != Some("public") {
                // Public conversation metadata does not grant access to
                // history from a private/revoked deleted node.
                // Historical archives without provenance are fail-closed.
                return Err(ConversationApiError::new(
                    StatusCode::NOT_FOUND,
                    "conversation_not_found",
                    "the conversation does not exist",
                ));
            }
        }
        return Ok(());
    }

    if conversation_type == "direct" && visibility == "participants" {
        let Some(account_id) = auth.account_id.as_deref() else {
            return Err(ConversationApiError::new(
                StatusCode::NOT_FOUND,
                "conversation_not_found",
                "the conversation does not exist",
            ));
        };
        let participant: bool = sqlx::query_scalar(
            "SELECT EXISTS(\
                 SELECT 1 FROM domain_direct_conversation_participants \
                 WHERE conversation_id = $1::uuid AND account_id = $2\
             )",
        )
        .bind(conversation_id)
        .bind(account_id)
        .fetch_one(&mut **tx)
        .await
        .map_err(|error| database_error("authorize direct conversation read", error))?;
        if participant {
            return Ok(());
        }
        return Err(ConversationApiError::new(
            StatusCode::NOT_FOUND,
            "conversation_not_found",
            "the conversation does not exist",
        ));
    }

    Err(ConversationApiError::new(
        StatusCode::INTERNAL_SERVER_ERROR,
        "conversation_visibility_invalid",
        "the conversation visibility contract is invalid",
    ))
}

fn conversation_is_writable(conversation_type: &str, governance_source: Option<&str>) -> bool {
    match conversation_type {
        "node" | "direct" | "webgemeindezentrum" => true,
        "governance_proposal" => governance_source == Some("canonical"),
        _ => false,
    }
}

/// A private conversation only carries new messages while both sides are live.
///
/// "Live" means the same thing here as in [`DIRECT_CONVERSATION_PROJECTION`]: the
/// participant row still points at an account row *and* that account is active.
/// Account deletion severs the binding through `ON DELETE SET NULL`, deactivation
/// leaves it in place, and both are a factual exit — so both must reject the send.
/// Otherwise the read projection would hide the composer for a counterpart that
/// the write path still happily accepts.
async fn require_direct_message_send_allowed(
    tx: &mut Transaction<'_, Postgres>,
    conversation_id: &str,
) -> Result<(), ConversationApiError> {
    // Lock participant rows first, then their account rows in canonical id order.
    // The separate account query is deliberate: PostgreSQL cannot lock the nullable
    // side of an outer join, while locking only participants would let a concurrent
    // account deactivation commit after this check but before message delivery.
    let participant_states: Vec<(Option<String>, Option<DateTime<Utc>>)> = sqlx::query_as(
        "SELECT participant.account_id, participant.blocked_at \
         FROM domain_direct_conversation_participants AS participant \
         WHERE participant.conversation_id = $1::uuid \
         ORDER BY participant.slot FOR SHARE OF participant",
    )
    .bind(conversation_id)
    .fetch_all(&mut **tx)
    .await
    .map_err(|error| database_error("lock direct conversation participants", error))?;
    let account_ids: Vec<String> = participant_states
        .iter()
        .filter_map(|(account_id, _)| account_id.clone())
        .collect();
    if participant_states.len() != 2 || account_ids.len() != 2 {
        return Err(ConversationApiError::new(
            StatusCode::CONFLICT,
            "direct_conversation_counterpart_unavailable",
            "the private conversation no longer has two active accounts",
        ));
    }

    let active_account_ids: Vec<String> = sqlx::query_scalar(
        "SELECT id FROM domain_accounts \
         WHERE id IN ($1, $2) AND disabled = FALSE \
         ORDER BY id FOR SHARE",
    )
    .bind(&account_ids[0])
    .bind(&account_ids[1])
    .fetch_all(&mut **tx)
    .await
    .map_err(|error| database_error("lock direct conversation accounts", error))?;
    if active_account_ids.len() != 2 {
        return Err(ConversationApiError::new(
            StatusCode::CONFLICT,
            "direct_conversation_counterpart_unavailable",
            "the private conversation no longer has two active accounts",
        ));
    }
    if participant_states
        .iter()
        .any(|(_, blocked_at)| blocked_at.is_some())
    {
        return Err(ConversationApiError::new(
            StatusCode::FORBIDDEN,
            "direct_conversation_blocked",
            "new messages are disabled for this private conversation",
        ));
    }
    Ok(())
}

fn conversation_write_busy() -> ConversationApiError {
    ConversationApiError::new(
        StatusCode::SERVICE_UNAVAILABLE,
        "conversation_write_busy",
        "the conversation is temporarily busy; retry this request",
    )
    .retry_after(1)
}

// Classify the current *locked* archive after a previously observed active
// node has disappeared. A node can be deleted between the initial conversation
// classification and the read-visibility precheck; only known-public archives
// may expose the existing 409 conflict. Every other target remains 404.
async fn unavailable_node_conversation_write_error(
    tx: &mut Transaction<'_, Postgres>,
    conversation_id: &str,
) -> Result<ConversationApiError, ConversationApiError> {
    let archived_public: bool = sqlx::query_scalar::<_, bool>(
        "SELECT (archived_at IS NOT NULL AND
                 archived_node_visibility = 'public') IS TRUE
         FROM domain_conversations
         WHERE id = $1::uuid AND deleted_at IS NULL
           AND conversation_type = 'node' AND node_id IS NULL
         FOR SHARE",
    )
    .bind(conversation_id)
    .fetch_optional(&mut **tx)
    .await
    .map_err(|error| database_error("verify archived node write contract", error))?
    .unwrap_or(false);
    Ok(if archived_public {
        ConversationApiError::new(
            StatusCode::CONFLICT,
            "conversation_archived",
            "the node conversation is archived and read-only",
        )
    } else {
        ConversationApiError::new(
            StatusCode::NOT_FOUND,
            "conversation_not_found",
            "the conversation does not exist",
        )
    })
}

async fn require_conversation_writable(
    tx: &mut Transaction<'_, Postgres>,
    conversation_id: &str,
    auth: &AuthContext,
) -> Result<(), ConversationApiError> {
    // Fast-path node conversations without touching the global governance cutover row.
    // The first read is only a classification hint; the row is locked and re-read below
    // before the write is authorized, so deletion or an unexpected type change cannot race.
    // Initial classification does not lock the conversation yet: the node
    // mutation advisory lock and node row lock must be acquired FIRST.
    let observed: Option<(String, Option<String>)> = sqlx::query_as(
        "SELECT conversation_type, node_id FROM domain_conversations
         WHERE id = $1::uuid AND deleted_at IS NULL",
    )
    .bind(conversation_id)
    .fetch_optional(&mut **tx)
    .await
    .map_err(|error| database_error("classify conversation write target", error))?;

    let (observed_type, observed_node_id) = observed.ok_or_else(|| {
        ConversationApiError::new(
            StatusCode::NOT_FOUND,
            "conversation_not_found",
            "the conversation does not exist",
        )
    })?;

    if observed_type == "node" {
        // Account exit locks the author first, then the node. Do not occupy
        // PostgreSQL pool connections while waiting for this account lock;
        // a node mutation can need another connection to finish.
        let author_account_id = reader_account_id(Some(auth)).ok_or_else(|| {
            ConversationApiError::new(
                StatusCode::NOT_FOUND,
                "conversation_not_found",
                "the conversation does not exist",
            )
        })?;
        // A deleted node has no active advisory key. Preserve the explicit
        // public-archive 409 contract without consulting or locking the author;
        // private/revoked/unknown archives remain indistinguishable from absent.
        if observed_node_id.is_none() {
            return Err(unavailable_node_conversation_write_error(tx, conversation_id).await?);
        }
        // Only known readable nodes may receive a retryable busy response:
        // private/hidden/revoked nodes must continue to look nonexistent.
        // The definitive authorization below is repeated after the node lock.
        let preliminarily_readable = if let Some(node_id) = observed_node_id.as_deref() {
            sqlx::query_scalar::<_, bool>(
                "SELECT (search_visibility = 'public' OR
                   (search_visibility IN ('private','hidden')
                    AND weltgewebe_search_node_owner_account_id(payload) = $2::text)) IS TRUE
                 FROM domain_nodes WHERE id = $1",
            )
            .bind(node_id)
            .bind(author_account_id)
            .fetch_optional(&mut **tx)
            .await
            .map_err(|error| database_error("precheck node message privacy", error))?
            .unwrap_or(false)
        } else {
            false
        };
        if !preliminarily_readable {
            // Re-read the conversation under a row lock, not the original
            // classification hint. This distinguishes a concurrently deleted
            // public node from an unreadable, still-active protected node.
            return Err(unavailable_node_conversation_write_error(tx, conversation_id).await?);
        }
        let active_author: Option<String> = sqlx::query_scalar(
            "SELECT id FROM domain_accounts
             WHERE id = $1 AND disabled = FALSE FOR SHARE NOWAIT",
        )
        .bind(author_account_id)
        .fetch_optional(&mut **tx)
        .await
        .map_err(|error| {
            if error
                .as_database_error()
                .and_then(|db| db.code())
                .as_deref()
                == Some("55P03")
            {
                conversation_write_busy()
            } else {
                database_error("lock node message author before node", error)
            }
        })?;
        if active_author.is_none() {
            return Err(ConversationApiError::new(
                StatusCode::NOT_FOUND,
                "conversation_not_found",
                "the conversation does not exist",
            ));
        }
        // Canonical node writes acquire the same advisory key. A FOR SHARE
        // row lock also serializes visibility changes by other DB writers.
        // This respects the node -> conversation lock order of node deletion.
        let allowed_node = if let Some(node_id) = observed_node_id.as_deref() {
            let acquired: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1::bigint)")
                .bind(node_mutation_lock_key(node_id))
                .fetch_one(&mut **tx)
                .await
                .map_err(|error| database_error("try lock conversation target node", error))?;
            if !acquired {
                return Err(conversation_write_busy());
            }
            sqlx::query_scalar::<_, bool>(
                "SELECT (search_visibility = 'public' OR
                   (search_visibility IN ('private','hidden')
                    AND $2::text IS NOT NULL
                    AND weltgewebe_search_node_owner_account_id(payload) = $2::text)) IS TRUE
                 FROM domain_nodes WHERE id = $1 FOR SHARE",
            )
            .bind(node_id)
            .bind(reader_account_id(Some(auth)))
            .fetch_optional(&mut **tx)
            .await
            .map_err(|error| database_error("authorize conversation message node", error))?
            .unwrap_or(false)
        } else {
            false
        };
        // Preserve the established tuple row mapping without Clippy's nested
        // generic type-complexity warning or an extra persistence abstraction.
        type LockedNodeTarget = (
            String,
            Option<String>,
            Option<DateTime<Utc>>,
            Option<String>,
        );
        let locked_target: Option<LockedNodeTarget> = sqlx::query_as(
            "SELECT conversation_type, node_id, archived_at, archived_node_visibility
                 FROM domain_conversations
                 WHERE id = $1::uuid AND deleted_at IS NULL FOR SHARE",
        )
        .bind(conversation_id)
        .fetch_optional(&mut **tx)
        .await
        .map_err(|error| database_error("lock node conversation write target", error))?;

        return match locked_target {
            Some((conversation_type, Some(node_id), None, _)) if conversation_type == "node" => {
                if !allowed_node || observed_node_id.as_deref() != Some(node_id.as_str()) {
                    Err(ConversationApiError::new(
                        StatusCode::NOT_FOUND,
                        "conversation_not_found",
                        "the conversation does not exist",
                    ))
                } else {
                    Ok(())
                }
            }
            Some((conversation_type, None, Some(_), archived_visibility))
                if conversation_type == "node" =>
            {
                if archived_visibility.as_deref() != Some("public") {
                    Err(ConversationApiError::new(
                        StatusCode::NOT_FOUND,
                        "conversation_not_found",
                        "the conversation does not exist",
                    ))
                } else {
                    Err(ConversationApiError::new(
                        StatusCode::CONFLICT,
                        "conversation_archived",
                        "the node conversation is archived and read-only",
                    ))
                }
            }
            None => Err(ConversationApiError::new(
                StatusCode::NOT_FOUND,
                "conversation_not_found",
                "the conversation does not exist",
            )),
            Some(_) => Err(ConversationApiError::new(
                StatusCode::CONFLICT,
                "conversation_write_target_changed",
                "the conversation write target changed while the request was in flight",
            )),
        };
    }

    if observed_type == "webgemeindezentrum" {
        let locked_target: Option<(String, Option<String>, Option<DateTime<Utc>>)> =
            sqlx::query_as(
                "SELECT conversation_type, webgemeindezentrum_id, archived_at \
                 FROM domain_conversations \
                 WHERE id = $1::uuid AND deleted_at IS NULL FOR SHARE",
            )
            .bind(conversation_id)
            .fetch_optional(&mut **tx)
            .await
            .map_err(|error| database_error("lock center conversation write target", error))?;

        return match locked_target {
            Some((conversation_type, Some(_), None))
                if conversation_type == "webgemeindezentrum" =>
            {
                Ok(())
            }
            None => Err(ConversationApiError::new(
                StatusCode::NOT_FOUND,
                "conversation_not_found",
                "the conversation does not exist",
            )),
            Some(_) => Err(ConversationApiError::new(
                StatusCode::CONFLICT,
                "conversation_write_target_changed",
                "the conversation write target changed while the request was in flight",
            )),
        };
    }

    if observed_type == "direct" {
        let locked_target: Option<(String, String, Option<DateTime<Utc>>)> = sqlx::query_as(
            "SELECT conversation_type, visibility, archived_at \
                 FROM domain_conversations \
                 WHERE id = $1::uuid AND deleted_at IS NULL FOR SHARE",
        )
        .bind(conversation_id)
        .fetch_optional(&mut **tx)
        .await
        .map_err(|error| database_error("lock direct conversation write target", error))?;

        match locked_target {
            Some((conversation_type, visibility, None))
                if conversation_type == "direct" && visibility == "participants" => {}
            None => {
                return Err(ConversationApiError::new(
                    StatusCode::NOT_FOUND,
                    "conversation_not_found",
                    "the conversation does not exist",
                ));
            }
            Some(_) => {
                return Err(ConversationApiError::new(
                    StatusCode::CONFLICT,
                    "conversation_write_target_changed",
                    "the conversation write target changed while the request was in flight",
                ));
            }
        }

        let Some(account_id) = auth.account_id.as_deref() else {
            return Err(ConversationApiError::new(
                StatusCode::NOT_FOUND,
                "conversation_not_found",
                "the conversation does not exist",
            ));
        };
        let participant: Option<Option<DateTime<Utc>>> = sqlx::query_scalar(
            "SELECT blocked_at FROM domain_direct_conversation_participants \
             WHERE conversation_id = $1::uuid AND account_id = $2 FOR SHARE",
        )
        .bind(conversation_id)
        .bind(account_id)
        .fetch_optional(&mut **tx)
        .await
        .map_err(|error| database_error("authorize direct conversation write", error))?;
        if participant.is_none() {
            return Err(ConversationApiError::new(
                StatusCode::NOT_FOUND,
                "conversation_not_found",
                "the conversation does not exist",
            ));
        }
        return Ok(());
    }

    // Governance writes use one lock order everywhere: cutover singleton first,
    // conversation row second. This keeps the source stable through commit and
    // prevents a rollback to legacy while an authorized canonical write is in flight.
    let governance_source: Option<String> = sqlx::query_scalar(
        "SELECT governance_source FROM domain_conversation_cutover_state WHERE singleton FOR SHARE",
    )
    .fetch_optional(&mut **tx)
    .await
    .map_err(|error| database_error("lock conversation write cutover", error))?;

    let conversation_type: Option<String> = sqlx::query_scalar(
        "SELECT conversation_type FROM domain_conversations WHERE id = $1::uuid AND deleted_at IS NULL FOR SHARE",
    )
    .bind(conversation_id)
    .fetch_optional(&mut **tx)
    .await
    .map_err(|error| database_error("check conversation write target", error))?;

    let conversation_type = conversation_type.ok_or_else(|| {
        ConversationApiError::new(
            StatusCode::NOT_FOUND,
            "conversation_not_found",
            "the conversation does not exist",
        )
    })?;

    if conversation_type != observed_type {
        return Err(ConversationApiError::new(
            StatusCode::CONFLICT,
            "conversation_write_target_changed",
            "the conversation write target changed while the request was in flight",
        ));
    }

    if conversation_is_writable(&conversation_type, governance_source.as_deref()) {
        return Ok(());
    }

    Err(ConversationApiError::new(
        StatusCode::CONFLICT,
        "conversation_write_not_active",
        "governance conversation writes are not active before canonical cutover",
    ))
}

async fn require_conversation_tombstone_allowed(
    tx: &mut Transaction<'_, Postgres>,
    conversation_id: &str,
    auth: &AuthContext,
) -> Result<(), ConversationApiError> {
    let observed: Option<(String, Option<String>)> = sqlx::query_as(
        "SELECT conversation_type, node_id FROM domain_conversations
         WHERE id = $1::uuid AND deleted_at IS NULL",
    )
    .bind(conversation_id)
    .fetch_optional(&mut **tx)
    .await
    .map_err(|error| database_error("classify conversation tombstone target", error))?;
    let (conversation_type, node_id) = observed.ok_or_else(|| {
        ConversationApiError::new(
            StatusCode::NOT_FOUND,
            "conversation_not_found",
            "the conversation does not exist",
        )
    })?;

    if conversation_type == "node" {
        if node_id.is_some() {
            // Reuse the same transaction-scoped node advisory/row lock and
            // visibility check as POST/PATCH. This must run BEFORE loading
            // the message or returning an If-Match precondition body.
            return require_conversation_writable(tx, conversation_id, auth).await;
        }

        // Archived node histories deliberately allow their original public
        // message authors to tombstone; private/unknown historical archives
        // have no such ordinary public capability.
        let archived_visibility: Option<Option<String>> = sqlx::query_scalar(
            "SELECT archived_node_visibility FROM domain_conversations
             WHERE id = $1::uuid AND conversation_type = 'node'
               AND node_id IS NULL AND archived_at IS NOT NULL
               AND deleted_at IS NULL FOR SHARE",
        )
        .bind(conversation_id)
        .fetch_optional(&mut **tx)
        .await
        .map_err(|error| database_error("authorize archived node message tombstone", error))?;
        if archived_visibility.flatten().as_deref() == Some("public") {
            return Ok(());
        }
        return Err(ConversationApiError::new(
            StatusCode::NOT_FOUND,
            "conversation_not_found",
            "the conversation does not exist",
        ));
    }

    require_conversation_writable(tx, conversation_id, auth).await
}

async fn load_message_for_update(
    tx: &mut Transaction<'_, Postgres>,
    conversation_id: &str,
    message_id: &str,
) -> Result<MessageView, ConversationApiError> {
    let row: Option<MessageRow> = sqlx::query_as(
        "SELECT id::text, conversation_id::text, author_account_id, author_title, content, \
                created_at, updated_at, deleted_at \
         FROM domain_messages \
         WHERE id = $1::uuid AND conversation_id = $2::uuid \
         FOR UPDATE",
    )
    .bind(message_id)
    .bind(conversation_id)
    .fetch_optional(&mut **tx)
    .await
    .map_err(|error| database_error("load message for update", error))?;

    row.map(message_from_row).ok_or_else(|| {
        ConversationApiError::new(
            StatusCode::NOT_FOUND,
            "message_not_found",
            "the message does not exist in this conversation",
        )
    })
}

fn require_author(auth: &AuthContext, message: &MessageView) -> Result<(), ConversationApiError> {
    if auth
        .account_id
        .as_deref()
        .is_some_and(|account_id| message.author_account_id.as_deref() == Some(account_id))
    {
        return Ok(());
    }
    Err(ConversationApiError::new(
        StatusCode::FORBIDDEN,
        "message_author_required",
        "only the message author may edit this message",
    ))
}

fn require_author_or_admin(
    auth: &AuthContext,
    message: &MessageView,
) -> Result<(), ConversationApiError> {
    if auth.role == Role::Admin
        || auth
            .account_id
            .as_deref()
            .is_some_and(|account_id| message.author_account_id.as_deref() == Some(account_id))
    {
        return Ok(());
    }
    Err(ConversationApiError::new(
        StatusCode::FORBIDDEN,
        "message_owner_required",
        "only the message author or an administrator may remove this message",
    ))
}

pub async fn get_node_conversation(
    State(state): State<ApiState>,
    Path(node_id): Path<String>,
    Extension(auth): Extension<AuthContext>,
) -> Result<Json<ConversationView>, ConversationApiError> {
    let pool = require_pool(&state)?;
    let mut tx = begin_consistent_conversation_read(pool).await?;
    let row: Option<ConversationRow> = sqlx::query_as(
        "SELECT id::text, node_id, node_id_snapshot, node_title_snapshot, \
                conversation_type, visibility, created_at, updated_at, archived_at, deleted_at \
         FROM domain_conversations WHERE node_id = $1 AND deleted_at IS NULL",
    )
    .bind(node_id)
    .fetch_optional(&mut *tx)
    .await
    .map_err(|error| database_error("get node conversation", error))?;

    let row = row.ok_or_else(|| {
        ConversationApiError::new(
            StatusCode::NOT_FOUND,
            "conversation_not_found",
            "the node conversation does not exist",
        )
    })?;
    require_conversation_read_access(&mut tx, &row.0, &auth).await?;
    tx.commit()
        .await
        .map_err(|error| database_error("commit node conversation read snapshot", error))?;
    Ok(Json(conversation_from_row(row)))
}

pub async fn get_conversation(
    State(state): State<ApiState>,
    Path(id): Path<String>,
    Extension(auth): Extension<AuthContext>,
) -> Result<Json<ConversationView>, ConversationApiError> {
    let pool = require_pool(&state)?;
    let id = parse_conversation_id(&id)?;
    let mut tx = begin_consistent_conversation_read(pool).await?;
    require_conversation_read_access(&mut tx, &id, &auth).await?;
    let row: Option<ConversationRow> = sqlx::query_as(
        "SELECT id::text, node_id, node_id_snapshot, node_title_snapshot, \
                conversation_type, visibility, created_at, updated_at, archived_at, deleted_at \
         FROM domain_conversations WHERE id = $1::uuid AND deleted_at IS NULL",
    )
    .bind(id)
    .fetch_optional(&mut *tx)
    .await
    .map_err(|error| database_error("get conversation", error))?;

    let view = row.map(conversation_from_row).ok_or_else(|| {
        ConversationApiError::new(
            StatusCode::NOT_FOUND,
            "conversation_not_found",
            "the node conversation does not exist",
        )
    })?;
    tx.commit()
        .await
        .map_err(|error| database_error("commit conversation read snapshot", error))?;
    Ok(Json(view))
}

#[derive(Debug, Deserialize)]
pub struct MessageListQuery {
    limit: Option<usize>,
    cursor: Option<String>,
}

pub async fn list_messages(
    State(state): State<ApiState>,
    Path(conversation_id): Path<String>,
    Query(query): Query<MessageListQuery>,
    Extension(auth): Extension<AuthContext>,
) -> Result<Json<MessagePage>, ConversationApiError> {
    let pool = require_pool(&state)?;
    let conversation_id = parse_conversation_id(&conversation_id)?;
    let mut tx = begin_consistent_conversation_read(pool).await?;
    // This already checks existence/deletion in the SAME snapshot.
    require_conversation_read_access(&mut tx, &conversation_id, &auth).await?;

    let limit = query.limit.unwrap_or(DEFAULT_MESSAGE_PAGE_SIZE);
    if limit == 0 || limit > MAX_MESSAGE_PAGE_SIZE {
        return Err(ConversationApiError::new(
            StatusCode::BAD_REQUEST,
            "invalid_page_size",
            "limit must be between 1 and 50",
        ));
    }

    let cursor_id = match query.cursor.as_deref() {
        Some(token) if !token.is_empty() => {
            let decoded = decode_cursor(token).map_err(|_| {
                ConversationApiError::new(
                    StatusCode::BAD_REQUEST,
                    "invalid_cursor",
                    "cursor is malformed or does not belong to this conversation",
                )
            })?;
            let canonical = Uuid::parse_str(&decoded).map_err(|_| {
                ConversationApiError::new(
                    StatusCode::BAD_REQUEST,
                    "invalid_cursor",
                    "cursor is malformed or does not belong to this conversation",
                )
            })?;
            Some(canonical.to_string())
        }
        _ => None,
    };

    let rows: Vec<MessageRow> = if let Some(cursor_id) = cursor_id {
        let cursor_position: Option<(DateTime<Utc>, String)> = sqlx::query_as(
            "SELECT created_at, id::text FROM domain_messages \
             WHERE id = $1::uuid AND conversation_id = $2::uuid",
        )
        .bind(cursor_id)
        .bind(&conversation_id)
        .fetch_optional(&mut *tx)
        .await
        .map_err(|error| database_error("resolve message cursor", error))?;
        let (cursor_created_at, cursor_id) = cursor_position.ok_or_else(|| {
            ConversationApiError::new(
                StatusCode::BAD_REQUEST,
                "invalid_cursor",
                "cursor is malformed or does not belong to this conversation",
            )
        })?;
        sqlx::query_as(
            "SELECT id::text, conversation_id::text, author_account_id, author_title, content, \
                    created_at, updated_at, deleted_at \
             FROM domain_messages \
             WHERE conversation_id = $1::uuid \
               AND (created_at, id) < ($2, $3::uuid) \
             ORDER BY created_at DESC, id DESC LIMIT $4",
        )
        .bind(&conversation_id)
        .bind(cursor_created_at)
        .bind(cursor_id)
        .bind((limit + 1) as i64)
        .fetch_all(&mut *tx)
        .await
        .map_err(|error| database_error("list messages after cursor", error))?
    } else {
        sqlx::query_as(
            "SELECT id::text, conversation_id::text, author_account_id, author_title, content, \
                    created_at, updated_at, deleted_at \
             FROM domain_messages WHERE conversation_id = $1::uuid \
             ORDER BY created_at DESC, id DESC LIMIT $2",
        )
        .bind(&conversation_id)
        .bind((limit + 1) as i64)
        .fetch_all(&mut *tx)
        .await
        .map_err(|error| database_error("list messages", error))?
    };

    tx.commit()
        .await
        .map_err(|error| database_error("commit message page read snapshot", error))?;
    let has_more = rows.len() > limit;
    let mut items: Vec<MessageView> = rows.into_iter().take(limit).map(message_from_row).collect();
    items.reverse();
    // The page is returned oldest-first for the UI, so the first item is the
    // oldest row in this page and therefore the boundary for the next older page.
    let next_cursor = if has_more {
        items.first().map(|message| encode_cursor(&message.id))
    } else {
        None
    };

    Ok(Json(MessagePage {
        items,
        page: MessagePageMeta {
            limit,
            next_cursor,
            has_more,
        },
    }))
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CreateMessageRequest {
    content: String,
}

pub async fn create_message(
    State(state): State<ApiState>,
    Path(conversation_id): Path<String>,
    Extension(auth): Extension<AuthContext>,
    headers: HeaderMap,
    Json(request): Json<CreateMessageRequest>,
) -> Result<(StatusCode, Json<MessageView>), ConversationApiError> {
    let pool = require_pool(&state)?;
    let author_account_id = account_id(&auth)?.to_string();
    let idempotency_key = parse_idempotency_key(&headers)?;
    let content = validate_content(&request.content)?;
    let conversation_id = parse_conversation_id(&conversation_id)?;

    let mut tx = pool
        .begin()
        .await
        .map_err(|error| database_error("begin create message", error))?;
    require_conversation_writable(&mut tx, &conversation_id, &auth).await?;
    let (node_id, conversation_type, webgemeindezentrum_id): (
        Option<String>,
        String,
        Option<String>,
    ) = sqlx::query_as(
        "SELECT node_id, conversation_type, webgemeindezentrum_id \
         FROM domain_conversations WHERE id = $1::uuid",
    )
    .bind(&conversation_id)
    .fetch_one(&mut *tx)
    .await
    .map_err(|error| database_error("load conversation type for message create", error))?;

    let author_title: Option<String> =
        sqlx::query_scalar("SELECT title FROM domain_accounts WHERE id = $1")
            .bind(&author_account_id)
            .fetch_optional(&mut *tx)
            .await
            .map_err(|error| database_error("load message author", error))?;
    let author_title = author_title.ok_or_else(|| {
        ConversationApiError::new(
            StatusCode::CONFLICT,
            "author_account_not_persisted",
            "the authenticated account is not present in the canonical domain store",
        )
    })?;
    let author_title = validate_author_title(&author_title)?;

    let rate_limit_key = format!("conversation-message-rate:{conversation_id}:{author_account_id}");
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))")
        .bind(&rate_limit_key)
        .fetch_one(&mut *tx)
        .await
        .map_err(|error| database_error("lock message rate window", error))?;

    let existing: Option<MessageRow> = sqlx::query_as(
        "SELECT id::text, conversation_id::text, author_account_id, author_title, content,
                created_at, updated_at, deleted_at
         FROM domain_messages
         WHERE conversation_id = $1::uuid
           AND author_account_id = $2
           AND idempotency_key = $3::uuid",
    )
    .bind(&conversation_id)
    .bind(&author_account_id)
    .bind(&idempotency_key)
    .fetch_optional(&mut *tx)
    .await
    .map_err(|error| database_error("load idempotent message", error))?;

    if let Some(existing) = existing {
        let existing = message_from_row(existing);
        if existing.content.as_deref() != Some(content.as_str()) || existing.deleted_at.is_some() {
            return Err(ConversationApiError::new(
                StatusCode::CONFLICT,
                "idempotency_key_conflict",
                "the Idempotency-Key was already used for a different message",
            ));
        }
        let message_id = existing.id.clone();
        tx.commit()
            .await
            .map_err(|error| database_error("commit idempotent message replay", error))?;
        project_message_participation_faden(
            &state,
            &auth,
            node_id.as_deref(),
            webgemeindezentrum_id.as_deref(),
            &conversation_id,
            &message_id,
            super::edges::FadenProjectionMode::EnsureOnly,
        )
        .await;
        return Ok((StatusCode::OK, Json(existing)));
    }

    if conversation_type == "direct" {
        let delivery_lock_key = format!("direct-conversation-delivery:{conversation_id}");
        sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))")
            .bind(&delivery_lock_key)
            .fetch_one(&mut *tx)
            .await
            .map_err(|error| database_error("lock private conversation delivery", error))?;
        require_direct_message_send_allowed(&mut tx, &conversation_id).await?;
        let global_rate_key = format!("direct-message-global-rate:{author_account_id}");
        sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))")
            .bind(&global_rate_key)
            .fetch_one(&mut *tx)
            .await
            .map_err(|error| database_error("lock global private message rate window", error))?;
        let global_recent_count: i64 = sqlx::query_scalar(
            "SELECT count(*)::bigint
             FROM domain_messages AS message
             JOIN domain_conversations AS conversation
               ON conversation.id = message.conversation_id
             WHERE conversation.conversation_type = 'direct'
               AND message.author_account_id = $1
               AND message.created_at > NOW() - INTERVAL '1 minute'",
        )
        .bind(&author_account_id)
        .fetch_one(&mut *tx)
        .await
        .map_err(|error| database_error("count global private message rate", error))?;
        if global_recent_count >= DIRECT_MESSAGE_RATE_LIMIT_PER_MINUTE {
            return Err(ConversationApiError::new(
                StatusCode::TOO_MANY_REQUESTS,
                "direct_message_rate_limited",
                "at most 30 private messages per minute are allowed across conversations",
            )
            .retry_after(60));
        }
    }

    let recent_count: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM domain_messages
         WHERE conversation_id = $1::uuid
           AND author_account_id = $2
           AND created_at > NOW() - INTERVAL '1 minute'",
    )
    .bind(&conversation_id)
    .bind(&author_account_id)
    .fetch_one(&mut *tx)
    .await
    .map_err(|error| database_error("count recent messages", error))?;
    if recent_count >= MESSAGE_RATE_LIMIT_PER_MINUTE {
        return Err(ConversationApiError::new(
            StatusCode::TOO_MANY_REQUESTS,
            "message_rate_limited",
            "at most 10 new messages per minute are allowed in one conversation",
        )
        .retry_after(60));
    }

    let message_id = Uuid::new_v4().to_string();
    let row: MessageRow = sqlx::query_as(
        "WITH message_stamp AS (
             SELECT CASE WHEN $7 THEN clock_timestamp() ELSE NOW() END AS value
         )
         INSERT INTO domain_messages (
             id, conversation_id, author_account_id, author_title, content, idempotency_key,
             created_at, updated_at
         )
         SELECT $1::uuid, $2::uuid, $3, $4, $5, $6::uuid,
                message_stamp.value, message_stamp.value
         FROM message_stamp
         RETURNING id::text, conversation_id::text, author_account_id, author_title, content,
                   created_at, updated_at, deleted_at",
    )
    .bind(message_id)
    .bind(&conversation_id)
    .bind(&author_account_id)
    .bind(&author_title)
    .bind(&content)
    .bind(&idempotency_key)
    .bind(conversation_type == "direct")
    .fetch_one(&mut *tx)
    .await
    .map_err(|error| database_error("insert message", error))?;
    let message = message_from_row(row);

    tx.commit()
        .await
        .map_err(|error| database_error("commit message create", error))?;
    project_message_participation_faden(
        &state,
        &auth,
        node_id.as_deref(),
        webgemeindezentrum_id.as_deref(),
        &conversation_id,
        &message.id,
        super::edges::FadenProjectionMode::Reactivate,
    )
    .await;
    Ok((StatusCode::CREATED, Json(message)))
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UpdateMessageRequest {
    content: String,
}

pub async fn update_message(
    State(state): State<ApiState>,
    Path((conversation_id, message_id)): Path<(String, String)>,
    Extension(auth): Extension<AuthContext>,
    headers: HeaderMap,
    Json(request): Json<UpdateMessageRequest>,
) -> Result<Json<MessageView>, ConversationApiError> {
    let pool = require_pool(&state)?;
    let content = validate_content(&request.content)?;
    let conversation_id = parse_conversation_id(&conversation_id)?;
    let message_id = parse_message_id(&message_id)?;
    let mut tx = pool
        .begin()
        .await
        .map_err(|error| database_error("begin message update", error))?;
    require_conversation_writable(&mut tx, &conversation_id, &auth).await?;
    let current = load_message_for_update(&mut tx, &conversation_id, &message_id).await?;
    require_author(&auth, &current)?;
    check_precondition(&headers, &current)?;
    if current.deleted_at.is_some() {
        return Err(ConversationApiError::new(
            StatusCode::CONFLICT,
            "message_deleted",
            "a tombstoned message cannot be edited",
        ));
    }

    let row: MessageRow = sqlx::query_as(
        "UPDATE domain_messages
         SET content = $1,
             updated_at = GREATEST(NOW(), updated_at + INTERVAL '1 microsecond')
         WHERE id = $2::uuid
         RETURNING id::text, conversation_id::text, author_account_id, author_title, content,
                   created_at, updated_at, deleted_at",
    )
    .bind(content)
    .bind(message_id)
    .fetch_one(&mut *tx)
    .await
    .map_err(|error| database_error("update message", error))?;
    tx.commit()
        .await
        .map_err(|error| database_error("commit message update", error))?;
    Ok(Json(message_from_row(row)))
}

pub async fn delete_message(
    State(state): State<ApiState>,
    Path((conversation_id, message_id)): Path<(String, String)>,
    Extension(auth): Extension<AuthContext>,
    headers: HeaderMap,
) -> Result<Json<MessageView>, ConversationApiError> {
    let pool = require_pool(&state)?;
    let conversation_id = parse_conversation_id(&conversation_id)?;
    let message_id = parse_message_id(&message_id)?;
    let mut tx = pool
        .begin()
        .await
        .map_err(|error| database_error("begin message tombstone", error))?;
    require_conversation_tombstone_allowed(&mut tx, &conversation_id, &auth).await?;
    let conversation_type: String = sqlx::query_scalar(
        "SELECT conversation_type FROM domain_conversations WHERE id = $1::uuid",
    )
    .bind(&conversation_id)
    .fetch_one(&mut *tx)
    .await
    .map_err(|error| database_error("load conversation type for message tombstone", error))?;
    let current = load_message_for_update(&mut tx, &conversation_id, &message_id).await?;
    if conversation_type == "direct" {
        require_author(&auth, &current)?;
    } else {
        require_author_or_admin(&auth, &current)?;
    }
    check_precondition(&headers, &current)?;
    if current.deleted_at.is_some() {
        tx.commit()
            .await
            .map_err(|error| database_error("commit existing tombstone", error))?;
        return Ok(Json(current));
    }

    let row: MessageRow = sqlx::query_as(
        "UPDATE domain_messages
         SET content = NULL,
             deleted_at = GREATEST(NOW(), updated_at + INTERVAL '1 microsecond'),
             updated_at = GREATEST(NOW(), updated_at + INTERVAL '1 microsecond')
         WHERE id = $1::uuid
         RETURNING id::text, conversation_id::text, author_account_id, author_title, content,
                   created_at, updated_at, deleted_at",
    )
    .bind(message_id)
    .fetch_one(&mut *tx)
    .await
    .map_err(|error| database_error("tombstone message", error))?;
    tx.commit()
        .await
        .map_err(|error| database_error("commit message tombstone", error))?;
    Ok(Json(message_from_row(row)))
}

async fn load_direct_conversation_for_account(
    pool: &PgPool,
    conversation_id: &str,
    account_id: &str,
) -> Result<DirectConversationView, ConversationApiError> {
    let row: Option<DirectConversationRow> = sqlx::query_as(&format!(
        "{DIRECT_CONVERSATION_PROJECTION}
           AND conversation.id = $2::uuid"
    ))
    .bind(account_id)
    .bind(conversation_id)
    .fetch_optional(pool)
    .await
    .map_err(|error| database_error("load private conversation", error))?;

    row.map(direct_conversation_from_row).ok_or_else(|| {
        ConversationApiError::new(
            StatusCode::NOT_FOUND,
            "conversation_not_found",
            "the conversation does not exist",
        )
    })
}

pub async fn list_direct_conversations(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
) -> Result<Json<DirectConversationList>, ConversationApiError> {
    let pool = require_pool(&state)?;
    let account_id = account_id(&auth)?;
    let rows: Vec<DirectConversationRow> = sqlx::query_as(&format!(
        "{DIRECT_CONVERSATION_PROJECTION}
         ORDER BY conversation.updated_at DESC, conversation.id DESC"
    ))
    .bind(account_id)
    .fetch_all(pool)
    .await
    .map_err(|error| database_error("list private conversations", error))?;

    Ok(Json(DirectConversationList {
        items: rows.into_iter().map(direct_conversation_from_row).collect(),
    }))
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CreateDirectConversationRequest {
    recipient_account_id: String,
}

pub async fn create_direct_conversation(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
    Json(request): Json<CreateDirectConversationRequest>,
) -> Result<(StatusCode, Json<DirectConversationView>), ConversationApiError> {
    let pool = require_pool(&state)?;
    let sender_id = account_id(&auth)?.to_string();
    let recipient_id = request.recipient_account_id.trim().to_string();
    if recipient_id.is_empty() {
        return Err(ConversationApiError::new(
            StatusCode::BAD_REQUEST,
            "recipient_required",
            "recipient_account_id is required",
        ));
    }
    if recipient_id == sender_id {
        return Err(ConversationApiError::new(
            StatusCode::BAD_REQUEST,
            "direct_message_self_recipient",
            "a private conversation requires another account",
        ));
    }

    let pair_key = direct_pair_key(&sender_id, &recipient_id);
    let mut tx = pool
        .begin()
        .await
        .map_err(|error| database_error("begin private conversation create", error))?;

    // Lock both accounts in canonical id order. Concurrent A→B and B→A requests
    // therefore cannot deadlock while creating the same pair.
    let account_rows: Vec<(String, String)> = sqlx::query_as(
        "SELECT id, title FROM domain_accounts
         WHERE id IN ($1, $2) AND disabled = FALSE
         ORDER BY id
         FOR SHARE",
    )
    .bind(&sender_id)
    .bind(&recipient_id)
    .fetch_all(&mut *tx)
    .await
    .map_err(|error| database_error("lock private conversation accounts", error))?;

    let sender_title = account_rows
        .iter()
        .find(|(id, _)| id == &sender_id)
        .map(|(_, title)| title.clone())
        .ok_or_else(|| {
            ConversationApiError::new(
                StatusCode::CONFLICT,
                "author_account_not_persisted",
                "the authenticated account is not present in the canonical domain store",
            )
        })?;
    let recipient_title = account_rows
        .iter()
        .find(|(id, _)| id == &recipient_id)
        .map(|(_, title)| title.clone())
        .ok_or_else(|| {
            ConversationApiError::new(
                StatusCode::NOT_FOUND,
                "recipient_not_found",
                "the recipient account does not exist",
            )
        })?;
    let sender_title = validate_author_title(&sender_title)?;
    let recipient_title = validate_author_title(&recipient_title)?;

    let pair_lock_key = format!("direct-conversation-pair:{pair_key}");
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))")
        .bind(&pair_lock_key)
        .fetch_one(&mut *tx)
        .await
        .map_err(|error| database_error("lock private conversation pair", error))?;

    let existing_id: Option<String> = sqlx::query_scalar(
        "SELECT id::text FROM domain_conversations
         WHERE conversation_type = 'direct'
           AND direct_pair_key = $1
           AND deleted_at IS NULL
         FOR SHARE",
    )
    .bind(&pair_key)
    .fetch_optional(&mut *tx)
    .await
    .map_err(|error| database_error("load existing private conversation", error))?;

    if let Some(existing_id) = existing_id {
        let active_participants: i64 = sqlx::query_scalar(
            "SELECT count(*)::bigint
             FROM domain_direct_conversation_participants
             WHERE conversation_id = $1::uuid
               AND account_id IN ($2, $3)",
        )
        .bind(&existing_id)
        .bind(&sender_id)
        .bind(&recipient_id)
        .fetch_one(&mut *tx)
        .await
        .map_err(|error| database_error("verify existing private participants", error))?;
        if active_participants != 2 {
            return Err(ConversationApiError::new(
                StatusCode::CONFLICT,
                "direct_conversation_pair_retired",
                "this account pair cannot inherit a retired private conversation",
            ));
        }
        tx.commit()
            .await
            .map_err(|error| database_error("commit private conversation lookup", error))?;
        let view = load_direct_conversation_for_account(pool, &existing_id, &sender_id).await?;
        return Ok((StatusCode::OK, Json(view)));
    }

    let creator_rate_key = format!("direct-conversation-create-rate:{sender_id}");
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))")
        .bind(&creator_rate_key)
        .fetch_one(&mut *tx)
        .await
        .map_err(|error| database_error("lock private conversation create rate", error))?;
    let recent_created_count: i64 = sqlx::query_scalar(
        "SELECT count(*)::bigint FROM domain_conversations
         WHERE conversation_type = 'direct'
           AND direct_created_by_account_id = $1
           AND created_at > NOW() - INTERVAL '1 hour'",
    )
    .bind(&sender_id)
    .fetch_one(&mut *tx)
    .await
    .map_err(|error| database_error("count private conversations created recently", error))?;
    if recent_created_count >= DIRECT_CONVERSATION_CREATE_LIMIT_PER_HOUR {
        return Err(ConversationApiError::new(
            StatusCode::TOO_MANY_REQUESTS,
            "direct_conversation_create_rate_limited",
            "at most 20 new private conversations per hour are allowed",
        )
        .retry_after(3600));
    }

    let conversation_id = Uuid::new_v4().to_string();
    sqlx::query(
        "INSERT INTO domain_conversations (
             id, conversation_type, visibility, direct_pair_key, direct_created_by_account_id
         ) VALUES ($1::uuid, 'direct', 'participants', $2, $3)",
    )
    .bind(&conversation_id)
    .bind(&pair_key)
    .bind(&sender_id)
    .execute(&mut *tx)
    .await
    .map_err(|error| database_error("insert private conversation", error))?;

    let (slot_one_id, slot_one_title, slot_two_id, slot_two_title) = if sender_id <= recipient_id {
        (&sender_id, &sender_title, &recipient_id, &recipient_title)
    } else {
        (&recipient_id, &recipient_title, &sender_id, &sender_title)
    };
    sqlx::query(
        "INSERT INTO domain_direct_conversation_participants (
             conversation_id, slot, account_id, account_title_snapshot
         ) VALUES
             ($1::uuid, 1, $2, $3),
             ($1::uuid, 2, $4, $5)",
    )
    .bind(&conversation_id)
    .bind(slot_one_id)
    .bind(slot_one_title)
    .bind(slot_two_id)
    .bind(slot_two_title)
    .execute(&mut *tx)
    .await
    .map_err(|error| database_error("insert private conversation participants", error))?;

    tx.commit()
        .await
        .map_err(|error| database_error("commit private conversation create", error))?;
    let view = load_direct_conversation_for_account(pool, &conversation_id, &sender_id).await?;
    Ok((StatusCode::CREATED, Json(view)))
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MarkDirectConversationReadRequest {
    through_message_id: String,
}

pub async fn mark_direct_conversation_read(
    State(state): State<ApiState>,
    Path(conversation_id): Path<String>,
    Extension(auth): Extension<AuthContext>,
    Json(request): Json<MarkDirectConversationReadRequest>,
) -> Result<StatusCode, ConversationApiError> {
    let pool = require_pool(&state)?;
    let account_id = account_id(&auth)?;
    let conversation_id = parse_conversation_id(&conversation_id)?;
    let through_message_id = parse_message_id(&request.through_message_id)?;
    let mut tx = pool
        .begin()
        .await
        .map_err(|error| database_error("begin private conversation read marker", error))?;

    let delivery_lock_key = format!("direct-conversation-delivery:{conversation_id}");
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))")
        .bind(&delivery_lock_key)
        .fetch_one(&mut *tx)
        .await
        .map_err(|error| database_error("lock private conversation read marker", error))?;

    let through_created_at: Option<DateTime<Utc>> = sqlx::query_scalar(
        "SELECT message.created_at
         FROM domain_messages AS message
         JOIN domain_conversations AS conversation
           ON conversation.id = message.conversation_id
         JOIN domain_direct_conversation_participants AS participant
           ON participant.conversation_id = conversation.id
         WHERE conversation.id = $1::uuid
           AND conversation.conversation_type = 'direct'
           AND conversation.visibility = 'participants'
           AND conversation.deleted_at IS NULL
           AND participant.account_id = $2
           AND message.id = $3::uuid
         FOR SHARE OF conversation, participant, message",
    )
    .bind(&conversation_id)
    .bind(account_id)
    .bind(&through_message_id)
    .fetch_optional(&mut *tx)
    .await
    .map_err(|error| database_error("load private conversation read boundary", error))?;
    let through_created_at = through_created_at.ok_or_else(|| {
        ConversationApiError::new(
            StatusCode::NOT_FOUND,
            "conversation_not_found",
            "the conversation or read boundary does not exist",
        )
    })?;

    let result = sqlx::query(
        "UPDATE domain_direct_conversation_participants
         SET last_read_at = GREATEST(last_read_at, $3)
         WHERE conversation_id = $1::uuid
           AND account_id = $2",
    )
    .bind(&conversation_id)
    .bind(account_id)
    .bind(through_created_at)
    .execute(&mut *tx)
    .await
    .map_err(|error| database_error("mark private conversation read", error))?;
    if result.rows_affected() == 0 {
        return Err(ConversationApiError::new(
            StatusCode::NOT_FOUND,
            "conversation_not_found",
            "the conversation does not exist",
        ));
    }
    tx.commit()
        .await
        .map_err(|error| database_error("commit private conversation read marker", error))?;
    Ok(StatusCode::NO_CONTENT)
}

/// Toggle the caller's own block flag and answer with the refreshed projection.
///
/// The full view is returned instead of a bare `blocked` flag because releasing
/// one's own block does not necessarily restore `can_send`: the counterpart may
/// still block, or may have left. The client would otherwise re-enable its
/// composer on a state the server rejects.
async fn set_direct_conversation_block(
    state: &ApiState,
    auth: &AuthContext,
    conversation_id: &str,
    blocked: bool,
) -> Result<Json<DirectConversationView>, ConversationApiError> {
    let pool = require_pool(state)?;
    let account_id = account_id(auth)?;
    let conversation_id = parse_conversation_id(conversation_id)?;
    let result = if blocked {
        sqlx::query(
            "UPDATE domain_direct_conversation_participants AS participant
             SET blocked_at = COALESCE(participant.blocked_at, clock_timestamp())
             FROM domain_conversations AS conversation
             WHERE participant.conversation_id = conversation.id
               AND conversation.id = $1::uuid
               AND conversation.conversation_type = 'direct'
               AND conversation.deleted_at IS NULL
               AND participant.account_id = $2",
        )
        .bind(&conversation_id)
        .bind(account_id)
        .execute(pool)
        .await
    } else {
        sqlx::query(
            "UPDATE domain_direct_conversation_participants AS participant
             SET blocked_at = NULL
             FROM domain_conversations AS conversation
             WHERE participant.conversation_id = conversation.id
               AND conversation.id = $1::uuid
               AND conversation.conversation_type = 'direct'
               AND conversation.deleted_at IS NULL
               AND participant.account_id = $2",
        )
        .bind(&conversation_id)
        .bind(account_id)
        .execute(pool)
        .await
    }
    .map_err(|error| database_error("update private conversation block state", error))?;

    if result.rows_affected() == 0 {
        return Err(ConversationApiError::new(
            StatusCode::NOT_FOUND,
            "conversation_not_found",
            "the conversation does not exist",
        ));
    }
    load_direct_conversation_for_account(pool, &conversation_id, account_id)
        .await
        .map(Json)
}

pub async fn block_direct_conversation(
    State(state): State<ApiState>,
    Path(conversation_id): Path<String>,
    Extension(auth): Extension<AuthContext>,
) -> Result<Json<DirectConversationView>, ConversationApiError> {
    set_direct_conversation_block(&state, &auth, &conversation_id, true).await
}

pub async fn unblock_direct_conversation(
    State(state): State<ApiState>,
    Path(conversation_id): Path<String>,
    Extension(auth): Extension<AuthContext>,
) -> Result<Json<DirectConversationView>, ConversationApiError> {
    set_direct_conversation_block(&state, &auth, &conversation_id, false).await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn content_validation_trims_and_bounds_plain_text() {
        assert_eq!(validate_content("  Hallo  ").unwrap(), "Hallo");
        assert_eq!(
            validate_content("   ").unwrap_err().code,
            "invalid_message_content"
        );
        assert_eq!(
            validate_content(&"x".repeat(MAX_MESSAGE_LENGTH + 1))
                .unwrap_err()
                .code,
            "invalid_message_content"
        );
    }

    #[test]
    fn conversation_write_cutover_keeps_governance_on_legacy_until_canonical() {
        assert!(conversation_is_writable("node", Some("legacy")));
        assert!(conversation_is_writable("direct", None));
        assert!(!conversation_is_writable(
            "governance_proposal",
            Some("legacy")
        ));
        assert!(conversation_is_writable(
            "governance_proposal",
            Some("canonical")
        ));
        assert!(!conversation_is_writable("governance_proposal", None));
        assert!(!conversation_is_writable("unknown", Some("canonical")));
    }

    #[test]
    fn direct_pair_key_is_symmetric_and_unambiguous() {
        assert_eq!(
            direct_pair_key("account-a", "account-b"),
            direct_pair_key("account-b", "account-a")
        );
        assert_ne!(direct_pair_key("ab", "c"), direct_pair_key("a", "bc"));
    }

    #[test]
    fn conversation_view_exposes_active_node_lifecycle() {
        let created_at = DateTime::parse_from_rfc3339("2026-07-27T08:00:00Z")
            .unwrap()
            .with_timezone(&Utc);
        let view = conversation_from_row((
            Uuid::new_v4().to_string(),
            Some("active-node".to_string()),
            None,
            None,
            "node".to_string(),
            "public".to_string(),
            created_at,
            created_at,
            None,
            None,
        ));

        assert_eq!(view.lifecycle_state, "active");
        assert_eq!(view.node_id.as_deref(), Some("active-node"));
        assert_eq!(view.node_id_snapshot, None);
        assert_eq!(view.archived_at, None);
    }

    #[test]
    fn conversation_view_accepts_governance_target_without_node() {
        let created_at = DateTime::parse_from_rfc3339("2026-07-22T12:00:00Z")
            .unwrap()
            .with_timezone(&Utc);
        let updated_at = DateTime::parse_from_rfc3339("2026-07-22T12:00:01Z")
            .unwrap()
            .with_timezone(&Utc);
        let view = conversation_from_row((
            Uuid::new_v4().to_string(),
            None,
            None,
            None,
            "governance_proposal".to_string(),
            "public".to_string(),
            created_at,
            updated_at,
            None,
            None,
        ));

        assert_eq!(view.conversation_type, "governance_proposal");
        assert_eq!(view.lifecycle_state, "active");
        assert_eq!(view.node_id, None);
        assert_eq!(view.node_id_snapshot, None);
        assert_eq!(view.archived_at, None);
    }

    #[test]
    fn conversation_view_exposes_archived_node_context() {
        let created_at = DateTime::parse_from_rfc3339("2026-07-27T09:00:00Z")
            .unwrap()
            .with_timezone(&Utc);
        let archived_at = DateTime::parse_from_rfc3339("2026-07-27T09:05:00Z")
            .unwrap()
            .with_timezone(&Utc);
        let view = conversation_from_row((
            Uuid::new_v4().to_string(),
            None,
            Some("deleted-node".to_string()),
            Some("Gelöschter Knoten".to_string()),
            "node".to_string(),
            "public".to_string(),
            created_at,
            archived_at,
            Some(archived_at),
            None,
        ));

        assert_eq!(view.lifecycle_state, "archived");
        assert_eq!(view.node_id, None);
        assert_eq!(view.node_id_snapshot.as_deref(), Some("deleted-node"));
        assert_eq!(
            view.node_title_snapshot.as_deref(),
            Some("Gelöschter Knoten")
        );
        assert_eq!(view.archived_at.as_deref(), Some("2026-07-27T09:05:00Z"));
    }

    #[test]
    fn precondition_uses_the_public_updated_at_value() {
        let message = MessageView {
            id: Uuid::new_v4().to_string(),
            conversation_id: Uuid::new_v4().to_string(),
            author_account_id: Some("account-a".to_string()),
            author_title: "A".to_string(),
            content: Some("Hallo".to_string()),
            created_at: "2026-07-19T12:00:00Z".to_string(),
            updated_at: "2026-07-19T12:00:00.123456Z".to_string(),
            deleted_at: None,
        };
        let mut headers = HeaderMap::new();
        headers.insert(IF_MATCH, "\"2026-07-19T12:00:00.123456Z\"".parse().unwrap());
        assert!(check_precondition(&headers, &message).is_ok());
        headers.insert(IF_MATCH, "\"stale\"".parse().unwrap());
        assert_eq!(
            check_precondition(&headers, &message).unwrap_err().code,
            "message_version_conflict"
        );
    }
}
