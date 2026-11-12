//! Account-owned system messages are canonical domain_messages with a typed
//! private routing envelope, not synthetic human 1:1 conversations.
use axum::{
    extract::{Path, Query, State},
    http::{header, StatusCode},
    response::{IntoResponse, Response},
    Extension, Json,
};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use sqlx::{PgPool, Row};
use uuid::Uuid;

use crate::{config::DomainReadSource, middleware::auth::AuthContext, state::ApiState};

type ApiError = (StatusCode, String);
const PAGE_SIZE: usize = 50;

fn require_actor(auth: &AuthContext) -> Result<&str, ApiError> {
    auth.account_id
        .as_deref()
        .filter(|_| auth.authenticated)
        .ok_or((
            StatusCode::UNAUTHORIZED,
            "authentication required".to_owned(),
        ))
}

fn require_pool(state: &ApiState) -> Result<&PgPool, ApiError> {
    if state.config.domain_read_source != DomainReadSource::Postgres {
        return Err((
            StatusCode::SERVICE_UNAVAILABLE,
            "personal messages require canonical PostgreSQL".to_owned(),
        ));
    }
    state.db_pool.as_ref().ok_or((
        StatusCode::SERVICE_UNAVAILABLE,
        "personal message store unavailable".to_owned(),
    ))
}

fn storage_error(context: &'static str) -> impl Fn(sqlx::Error) -> ApiError {
    move |error| {
        tracing::error!(%error, context, "personal-event storage failed");
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            "personal messages could not be loaded".to_owned(),
        )
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PersonalEventPageQuery {
    before: Option<Uuid>,
}

#[derive(Debug, Serialize)]
pub struct PersonalEventView {
    id: Uuid,
    event_kind: String,
    content: String,
    created_at: DateTime<Utc>,
    read_at: Option<DateTime<Utc>>,
    circle_id: Option<Uuid>,
    circle_name: Option<String>,
    target_account_id: Option<String>,
    invitation_created_at: Option<DateTime<Utc>>,
    actionable: bool,
}

#[derive(Debug, Serialize)]
pub struct PersonalEventPage {
    items: Vec<PersonalEventView>,
    unread_count: i64,
    has_more: bool,
}

/// GET /personal-events — account-scoped, paginated personal system messages.
/// A former member keeps a neutral historic notification but not the circle ID,
/// name or a live action that could open a newly restricted node.
pub async fn list_personal_events(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
    Query(query): Query<PersonalEventPageQuery>,
) -> Result<Response, ApiError> {
    let actor = require_actor(&auth)?;
    let pool = require_pool(&state)?;
    let mut tx = pool
        .begin()
        .await
        .map_err(storage_error("begin_personal_event_read"))?;

    // Read under the same generation fence as node visibility; departing a
    // circle cannot race this response past the membership authorization.
    sqlx::query("SELECT version FROM domain_projection_state WHERE singleton=TRUE FOR SHARE")
        .fetch_one(&mut *tx)
        .await
        .map_err(storage_error("personal_event_visibility_fence"))?;

    let rows = sqlx::query(
        "SELECT m.id, e.event_kind, m.content, m.created_at, e.read_at, e.invitation_created_at, \
                CASE WHEN member.account_id IS NOT NULL AND c.state <> 'inactive' \
                     THEN c.id ELSE NULL END AS visible_circle_id, \
                CASE WHEN member.account_id IS NOT NULL AND c.state <> 'inactive' \
                     THEN c.name ELSE NULL END AS visible_circle_name, \
                CASE WHEN e.event_kind='circle_invitation_sent' AND member.status='active' \
                       AND pending.status='invited' AND pending.invited_by_account_id=$1 \
                       AND pending.created_at=e.invitation_created_at \
                       AND c.state<>'inactive' THEN pending.account_id ELSE NULL END \
                    AS target_account_id, \
                COALESCE(((e.event_kind='circle_invited' AND member.status='invited' \
                        AND member.created_at=e.invitation_created_at) \
                 OR (e.event_kind='circle_invitation_sent' AND member.status='active' \
                     AND pending.status='invited' AND pending.invited_by_account_id=$1 \
                     AND pending.created_at=e.invitation_created_at)) \
                 AND c.state<>'inactive', FALSE) AS actionable \
         FROM domain_conversations inbox \
         JOIN domain_messages m ON m.conversation_id=inbox.id \
         JOIN domain_personal_event_messages e ON e.message_id=m.id \
         LEFT JOIN domain_audience_circles c ON c.id=e.circle_id \
         LEFT JOIN domain_audience_circle_members member \
             ON member.circle_id=c.id AND member.account_id=$1 \
         LEFT JOIN domain_audience_circle_members pending \
             ON pending.circle_id=c.id AND pending.account_id=e.subject_account_id \
                AND pending.status='invited' \
         WHERE inbox.conversation_type='account_event' \
           AND inbox.account_event_owner_id=$1 \
           AND m.deleted_at IS NULL \
           AND ($2::uuid IS NULL OR (m.created_at, m.id) < ( \
               SELECT anchor.created_at, anchor.id \
               FROM domain_messages anchor \
               JOIN domain_conversations anchor_inbox \
                 ON anchor_inbox.id=anchor.conversation_id \
               WHERE anchor.id=$2 AND anchor_inbox.conversation_type='account_event' \
                 AND anchor_inbox.account_event_owner_id=$1 \
           )) \
         ORDER BY m.created_at DESC, m.id DESC LIMIT $3",
    )
    .bind(actor)
    .bind(query.before)
    .bind((PAGE_SIZE + 1) as i64)
    .fetch_all(&mut *tx)
    .await
    .map_err(storage_error("list_personal_events"))?;

    let unread_count: i64 = sqlx::query_scalar(
        "SELECT COUNT(*)::bigint FROM domain_personal_event_messages e \
         JOIN domain_messages m ON m.id=e.message_id \
         JOIN domain_conversations inbox ON inbox.id=m.conversation_id \
         WHERE inbox.conversation_type='account_event' \
           AND inbox.account_event_owner_id=$1 \
           AND e.read_at IS NULL AND m.deleted_at IS NULL",
    )
    .bind(actor)
    .fetch_one(&mut *tx)
    .await
    .map_err(storage_error("count_personal_event_unread"))?;

    let has_more = rows.len() > PAGE_SIZE;
    let items = rows
        .iter()
        .take(PAGE_SIZE)
        .map(|row| {
            Ok(PersonalEventView {
                id: row.try_get("id").map_err(storage_error("event_id"))?,
                event_kind: row
                    .try_get("event_kind")
                    .map_err(storage_error("event_kind"))?,
                content: row
                    .try_get("content")
                    .map_err(storage_error("event_content"))?,
                created_at: row
                    .try_get("created_at")
                    .map_err(storage_error("event_created_at"))?,
                read_at: row
                    .try_get("read_at")
                    .map_err(storage_error("event_read_at"))?,
                circle_id: row
                    .try_get("visible_circle_id")
                    .map_err(storage_error("event_circle_id"))?,
                circle_name: row
                    .try_get("visible_circle_name")
                    .map_err(storage_error("event_circle_name"))?,
                target_account_id: row
                    .try_get("target_account_id")
                    .map_err(storage_error("event_target_account_id"))?,
                invitation_created_at: row
                    .try_get("invitation_created_at")
                    .map_err(storage_error("event_invitation_created_at"))?,
                actionable: row
                    .try_get("actionable")
                    .map_err(storage_error("event_actionable"))?,
            })
        })
        .collect::<Result<Vec<_>, ApiError>>()?;

    tx.commit()
        .await
        .map_err(storage_error("commit_personal_event_read"))?;
    Ok((
        [(header::CACHE_CONTROL, "private, no-store")],
        Json(PersonalEventPage {
            items,
            unread_count,
            has_more,
        }),
    )
        .into_response())
}

/// POST /personal-events/{id}/read — exact per-message acknowledgment.
/// A refresh or unrelated newer event never becomes marked read implicitly.
pub async fn mark_personal_event_read(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
    Path(id): Path<Uuid>,
) -> Result<Response, ApiError> {
    let actor = require_actor(&auth)?;
    let pool = require_pool(&state)?;
    let _pool_budget = crate::state::reserve_postgres_domain_operation()
        .await
        .map_err(|error| {
            tracing::error!(%error, "reserve personal-event write capacity");
            (
                StatusCode::SERVICE_UNAVAILABLE,
                "personal messages are busy".to_owned(),
            )
        })?;
    let mut tx = pool
        .begin()
        .await
        .map_err(storage_error("begin_personal_event_read_receipt"))?;
    let allowed: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM domain_personal_event_messages e \
            JOIN domain_messages m ON m.id=e.message_id \
            JOIN domain_conversations inbox ON inbox.id=m.conversation_id \
            WHERE e.message_id=$1 AND inbox.conversation_type='account_event' \
              AND inbox.account_event_owner_id=$2)",
    )
    .bind(id)
    .bind(actor)
    .fetch_one(&mut *tx)
    .await
    .map_err(storage_error("authorize_personal_event_read_receipt"))?;
    if !allowed {
        return Err((
            StatusCode::NOT_FOUND,
            "personal message not found".to_owned(),
        ));
    }
    sqlx::query(
        "UPDATE domain_personal_event_messages SET read_at=NOW() \
         WHERE message_id=$1 AND read_at IS NULL",
    )
    .bind(id)
    .execute(&mut *tx)
    .await
    .map_err(storage_error("mark_personal_event_read"))?;
    tx.commit()
        .await
        .map_err(storage_error("commit_personal_event_read_receipt"))?;
    Ok((
        StatusCode::NO_CONTENT,
        [(header::CACHE_CONTROL, "private, no-store")],
    )
        .into_response())
}
