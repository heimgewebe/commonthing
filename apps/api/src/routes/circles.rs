//! Explicitly confirmed, location-independent audience-circle membership.
//!
//! A circle is an access-control scope, not a map object or a second group app.
//! No member of a 'forming' circle gains content rights. An invitation alone
//! never grants membership. PostgreSQL owns the canonical state.
use axum::{
    extract::{Path, Query, State},
    http::{header, StatusCode},
    response::{IntoResponse, Response},
    Extension, Json,
};
use serde::{Deserialize, Serialize};
use sqlx::{PgPool, Row};
use uuid::Uuid;

use crate::{
    config::{DomainAccountWriteSource, DomainReadSource},
    middleware::auth::AuthContext,
    state::ApiState,
};

type ApiError = (StatusCode, String);

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CreateCircleInput {
    kind: String,
    name: Option<String>,
    invitee_account_id: String,
}

impl CreateCircleInput {
    fn validate(&self, creator: &str) -> Result<(&str, String, &str), ApiError> {
        if !matches!(self.kind.as_str(), "household" | "community") {
            return Err(bad_request("unsupported circle kind"));
        }
        let invited = self.invitee_account_id.trim();
        if invited.is_empty() || invited.len() > 200 || invited == creator {
            return Err(bad_request("invitee must be a different account"));
        }
        let default_name = if self.kind == "household" {
            "Hausgemeinschaft"
        } else {
            "Gemeinschaft"
        };
        let name = self.name.as_deref().unwrap_or(default_name).trim();
        if name.is_empty() || name.chars().count() > 80 || name.chars().any(char::is_control) {
            return Err(bad_request(
                "circle name must have 1 to 80 printable characters",
            ));
        }
        Ok((self.kind.as_str(), name.to_owned(), invited))
    }
}

#[derive(Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ListCirclesQuery {
    after: Option<Uuid>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InviteAccountInput {
    account_id: String,
}

#[derive(Debug, Serialize)]
pub struct CircleMemberView {
    account_id: String,
    title: String,
}

#[derive(Debug, Serialize)]
pub struct CircleView {
    id: Uuid,
    kind: String,
    name: String,
    state: String,
    membership_status: String,
}

fn bad_request(message: &str) -> ApiError {
    (StatusCode::BAD_REQUEST, message.to_owned())
}

fn internal_error(context: &'static str) -> impl Fn(sqlx::Error) -> ApiError {
    move |error| {
        tracing::error!(%error, context, "audience-circle PostgreSQL operation failed");
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            "circle operation failed".to_owned(),
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

fn require_pool(state: &ApiState) -> Result<&PgPool, ApiError> {
    if state.config.domain_read_source != DomainReadSource::Postgres
        || state.config.domain_account_write_source != DomainAccountWriteSource::Postgres
    {
        return Err((
            StatusCode::SERVICE_UNAVAILABLE,
            "audience circles require canonical PostgreSQL accounts".to_owned(),
        ));
    }
    state.db_pool.as_ref().ok_or((
        StatusCode::SERVICE_UNAVAILABLE,
        "audience circles require PostgreSQL".to_owned(),
    ))
}

fn no_store<T: Serialize>(body: T) -> Response {
    ([(header::CACHE_CONTROL, "private, no-store")], Json(body)).into_response()
}

async fn account_exists(pool: &PgPool, account_id: &str) -> Result<bool, ApiError> {
    sqlx::query_scalar::<_, bool>(
        "SELECT EXISTS(SELECT 1 FROM domain_accounts WHERE id=$1 AND disabled=FALSE)",
    )
    .bind(account_id)
    .fetch_one(pool)
    .await
    .map_err(internal_error("account_exists"))
}

async fn own_circle(pool: &PgPool, circle_id: Uuid, actor: &str) -> Result<CircleView, ApiError> {
    let row = sqlx::query(
        "SELECT c.id, c.kind, c.name, c.state, m.status AS membership_status
         FROM domain_audience_circles c
         JOIN domain_audience_circle_members m ON m.circle_id=c.id
         WHERE c.id=$1 AND m.account_id=$2",
    )
    .bind(circle_id)
    .bind(actor)
    .fetch_optional(pool)
    .await
    .map_err(internal_error("own_circle"))?
    .ok_or((StatusCode::NOT_FOUND, "circle not found".to_owned()))?;
    row_to_view(&row)
}

fn row_to_view(row: &sqlx::postgres::PgRow) -> Result<CircleView, ApiError> {
    let get = |column| {
        row.try_get::<String, _>(column)
            .map_err(internal_error("circle_row"))
    };
    Ok(CircleView {
        id: row.try_get("id").map_err(internal_error("circle_id"))?,
        kind: get("kind")?,
        name: get("name")?,
        state: get("state")?,
        membership_status: get("membership_status")?,
    })
}

/// GET /circles/me — only own invitations and memberships, never public.
pub async fn list_my_circles(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
    Query(query): Query<ListCirclesQuery>,
) -> Result<Response, ApiError> {
    let actor = require_actor(&auth)?;
    let pool = require_pool(&state)?;
    // Keyset pagination is stable when invitations arrive or memberships
    // disappear between HTTP requests; OFFSET would shift later pages.
    let rows = sqlx::query(
        "SELECT c.id, c.kind, c.name, c.state, m.status AS membership_status
         FROM domain_audience_circles c
         JOIN domain_audience_circle_members m ON m.circle_id=c.id
         WHERE m.account_id=$1 AND ($2::uuid IS NULL OR EXISTS (
             SELECT 1 FROM domain_audience_circles anchor
             WHERE anchor.id=$2
               AND (c.created_at, c.id) < (anchor.created_at, anchor.id)
         ))
         ORDER BY c.created_at DESC, c.id DESC LIMIT 100",
    )
    .bind(actor)
    .bind(query.after)
    .fetch_all(pool)
    .await
    .map_err(internal_error("list_my_circles"))?;
    let views = rows
        .iter()
        .map(row_to_view)
        .collect::<Result<Vec<_>, _>>()?;
    Ok(no_store(views))
}

/// GET /circles/{id}/members — member names are private to active members.
/// Invited accounts, unconfirmed circles and strangers learn nothing.
pub async fn list_circle_members(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
    Path(id): Path<Uuid>,
) -> Result<Response, ApiError> {
    let actor = require_actor(&auth)?;
    let pool = require_pool(&state)?;
    // Hold a shared circle lock until after member rows have been read.
    // Departure, invitation and acceptance need an exclusive circle lock.
    let mut tx = pool
        .begin()
        .await
        .map_err(internal_error("begin_member_list"))?;
    let allowed = sqlx::query_scalar::<_, Uuid>(
        "SELECT c.id FROM domain_audience_circles c
         JOIN domain_audience_circle_members actor_m ON actor_m.circle_id=c.id
         JOIN domain_accounts actor_a ON actor_a.id=actor_m.account_id
         WHERE c.id=$1 AND c.state='active'
           AND actor_m.account_id=$2 AND actor_m.status='active'
           AND actor_a.disabled=FALSE
         FOR SHARE OF c, actor_m, actor_a",
    )
    .bind(id)
    .bind(actor)
    .fetch_optional(&mut *tx)
    .await
    .map_err(internal_error("check_member_list_access"))?;
    if allowed.is_none() {
        return Err((StatusCode::NOT_FOUND, "circle not found".to_owned()));
    }
    let rows = sqlx::query(
        "SELECT a.id AS account_id, a.title FROM domain_audience_circle_members m
         JOIN domain_accounts a ON a.id=m.account_id
         WHERE m.circle_id=$1 AND m.status='active' AND a.disabled=FALSE
         ORDER BY a.title, a.id LIMIT 100",
    )
    .bind(id)
    .fetch_all(&mut *tx)
    .await
    .map_err(internal_error("list_circle_members"))?;
    tx.commit()
        .await
        .map_err(internal_error("commit_member_list"))?;
    let members = rows
        .iter()
        .map(|row| {
            Ok(CircleMemberView {
                account_id: row
                    .try_get("account_id")
                    .map_err(internal_error("member_id"))?,
                title: row
                    .try_get("title")
                    .map_err(internal_error("member_title"))?,
            })
        })
        .collect::<Result<Vec<_>, ApiError>>()?;
    Ok(no_store(members))
}

/// POST /circles — the founder consents, the second member must confirm.
/// Only then does the circle change from forming to active.
pub async fn create_circle(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
    Json(input): Json<CreateCircleInput>,
) -> Result<Response, ApiError> {
    let actor = require_actor(&auth)?;
    let (kind, name, invited) = input.validate(actor)?;
    let pool = require_pool(&state)?;
    if !account_exists(pool, invited).await? {
        return Err((
            StatusCode::NOT_FOUND,
            "invitee account not found".to_owned(),
        ));
    }
    // Keep two pool units available to a privacy-fenced GET while circle
    // membership triggers wait for its generation lock.
    let _pool_budget = crate::state::reserve_postgres_domain_operation()
        .await
        .map_err(internal_error("reserve_create_circle_pool_budget"))?;
    let mut tx = pool
        .begin()
        .await
        .map_err(internal_error("begin_create_circle"))?;
    let circle_id = Uuid::new_v4();
    sqlx::query(
        "INSERT INTO domain_audience_circles
            (id, kind, name, state, created_by_account_id)
         VALUES ($1, $2, $3, 'forming', $4)",
    )
    .bind(circle_id)
    .bind(kind)
    .bind(name)
    .bind(actor)
    .execute(&mut *tx)
    .await
    .map_err(internal_error("insert_circle"))?;
    sqlx::query(
        "INSERT INTO domain_audience_circle_members
            (circle_id, account_id, status, invited_by_account_id, joined_at)
         VALUES ($1, $2, 'active', $2, NOW()), ($1, $3, 'invited', $2, NULL)",
    )
    .bind(circle_id)
    .bind(actor)
    .bind(invited)
    .execute(&mut *tx)
    .await
    .map_err(internal_error("insert_circle_founders"))?;
    tx.commit().await.map_err(internal_error("commit_circle"))?;
    let view = own_circle(pool, circle_id, actor).await?;
    Ok((StatusCode::CREATED, no_store(view)).into_response())
}

/// POST /circles/{id}/accept — only the specifically invited account.
pub async fn accept_invitation(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
    Path(id): Path<Uuid>,
) -> Result<Response, ApiError> {
    let actor = require_actor(&auth)?;
    let pool = require_pool(&state)?;
    // Keep two pool units available to a privacy-fenced GET while circle
    // membership triggers wait for its generation lock.
    let _pool_budget = crate::state::reserve_postgres_domain_operation()
        .await
        .map_err(internal_error("reserve_accept_circle_pool_budget"))?;
    let mut tx = pool.begin().await.map_err(internal_error("begin_accept"))?;
    let row = sqlx::query(
        "SELECT c.state, m.status FROM domain_audience_circles c
         JOIN domain_audience_circle_members m ON m.circle_id=c.id
         WHERE c.id=$1 AND m.account_id=$2
         FOR UPDATE OF c, m",
    )
    .bind(id)
    .bind(actor)
    .fetch_optional(&mut *tx)
    .await
    .map_err(internal_error("find_invitation"))?
    .ok_or((StatusCode::NOT_FOUND, "invitation not found".to_owned()))?;
    let state: String = row
        .try_get("state")
        .map_err(internal_error("circle_state"))?;
    let status: String = row
        .try_get("status")
        .map_err(internal_error("member_status"))?;
    if state == "inactive" || status != "invited" {
        return Err((
            StatusCode::CONFLICT,
            "invitation cannot be accepted".to_owned(),
        ));
    }
    sqlx::query(
        "UPDATE domain_audience_circle_members
         SET status='active', joined_at=NOW() WHERE circle_id=$1 AND account_id=$2",
    )
    .bind(id)
    .bind(actor)
    .execute(&mut *tx)
    .await
    .map_err(internal_error("accept_member"))?;
    sqlx::query(
        "UPDATE domain_audience_circles SET state='active'
         WHERE id=$1 AND state='forming'
           AND (SELECT COUNT(*) FROM domain_audience_circle_members m
                JOIN domain_accounts a ON a.id=m.account_id
                WHERE m.circle_id=$1 AND m.status='active' AND a.disabled=FALSE) >= 2",
    )
    .bind(id)
    .execute(&mut *tx)
    .await
    .map_err(internal_error("activate_circle"))?;
    tx.commit().await.map_err(internal_error("commit_accept"))?;
    Ok(no_store(own_circle(pool, id, actor).await?))
}

/// POST /circles/{id}/invitations — only a confirmed active member.
pub async fn invite_account(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
    Path(id): Path<Uuid>,
    Json(input): Json<InviteAccountInput>,
) -> Result<Response, ApiError> {
    let actor = require_actor(&auth)?;
    let invited = input.account_id.trim();
    if invited.is_empty() || invited.len() > 200 || invited == actor {
        return Err(bad_request("invitee must be a different account"));
    }
    let pool = require_pool(&state)?;
    if !account_exists(pool, invited).await? {
        return Err((
            StatusCode::NOT_FOUND,
            "invitee account not found".to_owned(),
        ));
    }
    // Keep two pool units available to a privacy-fenced GET while circle
    // membership triggers wait for its generation lock.
    let _pool_budget = crate::state::reserve_postgres_domain_operation()
        .await
        .map_err(internal_error("reserve_invite_circle_pool_budget"))?;
    let mut tx = pool.begin().await.map_err(internal_error("begin_invite"))?;
    let row = sqlx::query(
        "SELECT c.state FROM domain_audience_circles c
         JOIN domain_audience_circle_members m ON m.circle_id=c.id
         WHERE c.id=$1 AND m.account_id=$2 AND m.status='active'
         FOR UPDATE OF c, m",
    )
    .bind(id)
    .bind(actor)
    .fetch_optional(&mut *tx)
    .await
    .map_err(internal_error("find_inviter"))?
    .ok_or((StatusCode::NOT_FOUND, "circle not found".to_owned()))?;
    let circle_state: String = row
        .try_get("state")
        .map_err(internal_error("invite_state"))?;
    if circle_state != "active" {
        return Err((StatusCode::CONFLICT, "circle is not yet active".to_owned()));
    }
    sqlx::query(
        "INSERT INTO domain_audience_circle_members
             (circle_id, account_id, status, invited_by_account_id)
         VALUES ($1, $2, 'invited', $3)
         ON CONFLICT (circle_id, account_id) DO NOTHING",
    )
    .bind(id)
    .bind(invited)
    .bind(actor)
    .execute(&mut *tx)
    .await
    .map_err(internal_error("insert_invitation"))?;
    tx.commit().await.map_err(internal_error("commit_invite"))?;
    Ok((
        StatusCode::NO_CONTENT,
        [(header::CACHE_CONTROL, "private, no-store")],
    )
        .into_response())
}

/// DELETE /circles/{id}/invitations/{account_id} — the active inviter
/// may withdraw only their own still-pending invitation. The membership
/// trigger emits one personal event to the former invitee in this transaction.
pub async fn withdraw_invitation(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
    Path((id, invitee)): Path<(Uuid, String)>,
) -> Result<Response, ApiError> {
    let actor = require_actor(&auth)?;
    let pool = require_pool(&state)?;
    let _pool_budget = crate::state::reserve_postgres_domain_operation()
        .await
        .map_err(internal_error("reserve_withdraw_invitation_pool_budget"))?;
    let mut tx = pool
        .begin()
        .await
        .map_err(internal_error("begin_withdraw_invitation"))?;
    let allowed = sqlx::query_scalar::<_, Uuid>(
        "SELECT c.id FROM domain_audience_circles c
         JOIN domain_audience_circle_members inviter
           ON inviter.circle_id=c.id AND inviter.account_id=$2
         JOIN domain_audience_circle_members invited
           ON invited.circle_id=c.id AND invited.account_id=$3
         WHERE c.id=$1 AND c.state<>'inactive'
           AND inviter.status='active' AND invited.status='invited'
           AND invited.invited_by_account_id=$2
         FOR UPDATE OF c, inviter, invited",
    )
    .bind(id)
    .bind(actor)
    .bind(&invitee)
    .fetch_optional(&mut *tx)
    .await
    .map_err(internal_error("authorize_invitation_withdrawal"))?;
    if allowed.is_none() {
        return Err((StatusCode::NOT_FOUND, "invitation not found".to_owned()));
    }

    // Transaction-local marker only: the DELETE trigger distinguishes an
    // inviter's withdrawal from the invitee's existing leave/decline path.
    sqlx::query("SELECT set_config('weltgewebe.circle_invitation_withdrawal','yes',true)")
        .fetch_one(&mut *tx)
        .await
        .map_err(internal_error("mark_invitation_withdrawal"))?;
    sqlx::query(
        "DELETE FROM domain_audience_circle_members
         WHERE circle_id=$1 AND account_id=$2 AND status='invited'",
    )
    .bind(id)
    .bind(&invitee)
    .execute(&mut *tx)
    .await
    .map_err(internal_error("delete_withdrawn_invitation"))?;

    // A founding proposal with no one left to accept must not stay waiting.
    sqlx::query(
        "UPDATE domain_audience_circles c SET state='inactive'
         WHERE c.id=$1 AND c.state='forming'
         AND NOT EXISTS (SELECT 1 FROM domain_audience_circle_members m
                         WHERE m.circle_id=c.id AND m.status='invited')",
    )
    .bind(id)
    .execute(&mut *tx)
    .await
    .map_err(internal_error("close_unanswered_foundation"))?;
    tx.commit()
        .await
        .map_err(internal_error("commit_withdraw_invitation"))?;
    Ok((
        StatusCode::NO_CONTENT,
        [(header::CACHE_CONTROL, "private, no-store")],
    )
        .into_response())
}

/// DELETE /circles/{id}/membership — departure immediately revokes own rights.
/// Historical circle identity is retained, even when the last member leaves.
pub async fn leave_circle(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
    Path(id): Path<Uuid>,
) -> Result<Response, ApiError> {
    let actor = require_actor(&auth)?;
    let pool = require_pool(&state)?;
    // Keep two pool units available to a privacy-fenced GET while circle
    // membership triggers wait for its generation lock.
    let _pool_budget = crate::state::reserve_postgres_domain_operation()
        .await
        .map_err(internal_error("reserve_leave_circle_pool_budget"))?;
    let mut tx = pool.begin().await.map_err(internal_error("begin_leave"))?;
    // Serialize with invitations and acceptance on this circle.
    sqlx::query("SELECT id FROM domain_audience_circles WHERE id=$1 FOR UPDATE")
        .bind(id)
        .fetch_optional(&mut *tx)
        .await
        .map_err(internal_error("lock_circle"))?
        .ok_or((StatusCode::NOT_FOUND, "circle not found".to_owned()))?;
    let removed = sqlx::query(
        "DELETE FROM domain_audience_circle_members
         WHERE circle_id=$1 AND account_id=$2 RETURNING account_id",
    )
    .bind(id)
    .bind(actor)
    .fetch_optional(&mut *tx)
    .await
    .map_err(internal_error("remove_member"))?;
    if removed.is_none() {
        return Err((StatusCode::NOT_FOUND, "circle not found".to_owned()));
    }
    // A rejected final founding invitation must not leave a permanently
    // unanswerable "forming" proposal behind.
    sqlx::query(
        "UPDATE domain_audience_circles c SET state='inactive'
         WHERE c.id=$1 AND c.state='forming'
         AND NOT EXISTS (
           SELECT 1 FROM domain_audience_circle_members m
           WHERE m.circle_id=c.id AND m.status='invited'
         )",
    )
    .bind(id)
    .execute(&mut *tx)
    .await
    .map_err(internal_error("close_declined_foundation"))?;
    sqlx::query(
        "UPDATE domain_audience_circles c SET state='inactive'
         WHERE c.id=$1 AND NOT EXISTS (
           SELECT 1 FROM domain_audience_circle_members m
           WHERE m.circle_id=c.id AND m.status='active'
         )",
    )
    .bind(id)
    .execute(&mut *tx)
    .await
    .map_err(internal_error("deactivate_empty_circle"))?;
    tx.commit().await.map_err(internal_error("commit_leave"))?;
    Ok((
        StatusCode::NO_CONTENT,
        [(header::CACHE_CONTROL, "private, no-store")],
    )
        .into_response())
}

#[cfg(test)]
mod tests {
    use super::{CreateCircleInput, ListCirclesQuery};

    #[test]
    fn consent_requires_distinct_accounts() {
        let base = CreateCircleInput {
            kind: "household".into(),
            name: None,
            invitee_account_id: "account-a".into(),
        };
        assert!(base.validate("account-a").is_err());
        assert!(base.validate("account-b").is_ok());
    }

    #[test]
    fn list_cursor_is_uuid_and_legacy_offset_is_rejected() {
        let value: ListCirclesQuery =
            serde_json::from_str(r#"{"after":"00000000-0000-4000-8000-000000000001"}"#).unwrap();
        assert!(value.after.is_some());
        assert!(serde_json::from_str::<ListCirclesQuery>(r#"{"offset":100}"#).is_err());
        assert!(serde_json::from_str::<ListCirclesQuery>(r#"{"after":"not-a-uuid"}"#).is_err());
    }

    #[test]
    fn rejects_unrecognized_circle_types_and_bad_names() {
        let mut input = CreateCircleInput {
            kind: "household".into(),
            name: Some(" ".into()),
            invitee_account_id: "account-b".into(),
        };
        assert!(input.validate("account-a").is_err());
        input.name = Some("Valid".into());
        input.kind = "coordinate_circle".into();
        assert!(input.validate("account-a").is_err());
        input.kind = "community".into();
        assert!(input.validate("account-a").is_ok());
    }
}
