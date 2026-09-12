use super::query::{
    cursor_page, parse_cursor_params, parse_usize_param, validate_cursor_limit, ListResponse,
    MAX_PAGE_SIZE,
};
use crate::{middleware::auth::AuthContext, routes::nodes::Location, state::ApiState};
use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    Json,
};
use chrono::{DateTime, SecondsFormat, Utc};
use serde::Serialize;
use serde_json::json;
use sqlx::{PgPool, Row};
use std::{collections::HashMap, sync::OnceLock};
use tokio::sync::Semaphore;
use uuid::Uuid;

// A center activity projection temporarily owns one connection for the
// account-lifecycle advisory lock and a second one in the edge writer. Bound
// concurrent projections so waiters can never occupy the complete pool while
// each waits for its second connection.
const fn center_activity_slot_count(pool_max_connections: usize) -> usize {
    pool_max_connections.saturating_sub(1) / 2
}

const LOCAL_CENTER_ACTIVITY_SLOTS: usize =
    center_activity_slot_count(crate::DATABASE_POOL_MAX_CONNECTIONS as usize);
static CENTER_ACTIVITY_SLOTS: OnceLock<Semaphore> = OnceLock::new();

fn center_activity_slots() -> &'static Semaphore {
    CENTER_ACTIVITY_SLOTS.get_or_init(|| Semaphore::new(LOCAL_CENTER_ACTIVITY_SLOTS))
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum WebgemeindezentrumLocationState {
    Desired,
    Provisional,
    Confirmed,
    Unavailable,
    RelocationProposed,
}

impl WebgemeindezentrumLocationState {
    fn parse(value: &str) -> Result<Self, sqlx::Error> {
        match value {
            "desired" => Ok(Self::Desired),
            "provisional" => Ok(Self::Provisional),
            "confirmed" => Ok(Self::Confirmed),
            "unavailable" => Ok(Self::Unavailable),
            "relocation_proposed" => Ok(Self::RelocationProposed),
            other => Err(sqlx::Error::Decode(
                format!("unsupported Webgemeindezentrum location_state {other:?}").into(),
            )),
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Self::Desired => "Gewünschter Treffort",
            Self::Provisional => "Vorläufiger Treffort",
            Self::Confirmed => "Bestätigter Treffort",
            Self::Unavailable => "Derzeit nicht verfügbar",
            Self::RelocationProposed => "Neuer Treffort vorgeschlagen",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct OrtswebereiReference {
    pub id: String,
    pub slug: String,
    pub name: String,
    pub gewebezelle_id: String,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Webgemeindezentrum {
    #[serde(rename = "type")]
    pub entity_type: &'static str,
    pub id: String,
    pub title: String,
    pub ortsweberei: OrtswebereiReference,
    pub location_state: WebgemeindezentrumLocationState,
    pub location_state_label: &'static str,
    pub faden_endpoint_id: String,
    pub conversation_id: String,
    pub location: Location,
    pub location_label: String,
    pub meeting_note: String,
    pub access_note: String,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Ortsweberei {
    pub id: String,
    pub slug: String,
    pub name: String,
    pub description: String,
    pub gewebezelle_id: String,
    pub lifecycle_state: String,
    pub created_at: String,
    pub updated_at: String,
    pub webgemeindezentrum: Webgemeindezentrum,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct WebgemeindezentrumLocationHistoryEvent {
    pub event_id: i64,
    pub event_type: String,
    pub location_state: WebgemeindezentrumLocationState,
    pub location_state_label: &'static str,
    pub location: Location,
    pub location_label: String,
    pub reason: String,
    pub decided_at: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct WebgemeindezentrumGovernance {
    pub proposal_count: i64,
    pub open_proposal_count: i64,
    pub voting_proposal_count: i64,
    pub conversation_message_count: i64,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct WebgemeindezentrumDetails {
    #[serde(flatten)]
    pub center: Webgemeindezentrum,
    pub governance: WebgemeindezentrumGovernance,
    pub location_history: Vec<WebgemeindezentrumLocationHistoryEvent>,
}

fn timestamp(value: DateTime<Utc>) -> String {
    value.to_rfc3339_opts(SecondsFormat::Millis, true)
}

fn database_pool(state: &ApiState) -> Result<&PgPool, StatusCode> {
    state
        .db_pool
        .as_ref()
        .ok_or(StatusCode::SERVICE_UNAVAILABLE)
}

async fn load_active_ortswebereien(pool: &PgPool) -> Result<Vec<Ortsweberei>, sqlx::Error> {
    let rows = sqlx::query(
        "SELECT \
             o.id AS ortsweberei_id, o.slug, o.name AS ortsweberei_name, \
             o.description, o.gewebezelle_id, o.lifecycle_state, \
             o.created_at AS ortsweberei_created_at, \
             o.updated_at AS ortsweberei_updated_at, \
             c.id AS center_id, c.name AS center_name, c.location_state, \
             c.faden_endpoint_id::text AS faden_endpoint_id, \
             center_conversation.id::text AS center_conversation_id, \
             c.lat, c.lon, c.location_label, c.meeting_note, c.access_note, \
             c.created_at AS center_created_at, c.updated_at AS center_updated_at \
         FROM ortswebereien o \
         JOIN gewebezellen g ON g.id = o.gewebezelle_id \
         JOIN webgemeindezentren c \
           ON c.id = o.active_webgemeindezentrum_id \
          AND c.ortsweberei_id = o.id \
         JOIN domain_conversations center_conversation \
           ON center_conversation.webgemeindezentrum_id = c.id \
          AND center_conversation.conversation_type = 'webgemeindezentrum' \
          AND center_conversation.deleted_at IS NULL \
         WHERE o.lifecycle_state = 'active' \
           AND g.lifecycle_state = 'active' \
         ORDER BY o.id",
    )
    .fetch_all(pool)
    .await?;

    rows.into_iter()
        .map(|row| {
            let location_state_raw: String = row.try_get("location_state")?;
            let location_state = WebgemeindezentrumLocationState::parse(&location_state_raw)?;
            let reference = OrtswebereiReference {
                id: row.try_get("ortsweberei_id")?,
                slug: row.try_get("slug")?,
                name: row.try_get("ortsweberei_name")?,
                gewebezelle_id: row.try_get("gewebezelle_id")?,
            };
            let center = Webgemeindezentrum {
                entity_type: "webgemeindezentrum",
                id: row.try_get("center_id")?,
                title: row.try_get("center_name")?,
                ortsweberei: reference.clone(),
                location_state,
                location_state_label: location_state.label(),
                faden_endpoint_id: row.try_get("faden_endpoint_id")?,
                conversation_id: row.try_get("center_conversation_id")?,
                location: Location {
                    lat: row.try_get("lat")?,
                    lon: row.try_get("lon")?,
                },
                location_label: row.try_get("location_label")?,
                meeting_note: row.try_get("meeting_note")?,
                access_note: row.try_get("access_note")?,
                created_at: timestamp(row.try_get("center_created_at")?),
                updated_at: timestamp(row.try_get("center_updated_at")?),
            };

            Ok(Ortsweberei {
                id: reference.id,
                slug: reference.slug,
                name: reference.name,
                description: row.try_get("description")?,
                gewebezelle_id: reference.gewebezelle_id,
                lifecycle_state: row.try_get("lifecycle_state")?,
                created_at: timestamp(row.try_get("ortsweberei_created_at")?),
                updated_at: timestamp(row.try_get("ortsweberei_updated_at")?),
                webgemeindezentrum: center,
            })
        })
        .collect()
}

fn internal_error(error: sqlx::Error, operation: &'static str) -> StatusCode {
    tracing::error!(%error, operation, "failed to read Ortsweberei structure");
    StatusCode::INTERNAL_SERVER_ERROR
}

fn paginate<T: Clone>(
    items: Vec<T>,
    params: &HashMap<String, String>,
    id_of: impl Fn(&T) -> &str + Copy,
) -> Result<ListResponse<T>, StatusCode> {
    let limit = parse_usize_param(params, "limit", 100)?.min(MAX_PAGE_SIZE);
    let (cursor_mode, after_id) = parse_cursor_params(params)?;
    validate_cursor_limit(cursor_mode, limit)?;

    if cursor_mode {
        let refs: Vec<&T> = items.iter().collect();
        Ok(ListResponse::Cursor(cursor_page(
            refs,
            limit,
            after_id.as_deref(),
            |item| id_of(item),
            |item| item.clone(),
        )))
    } else {
        let offset = parse_usize_param(params, "offset", 0)?;
        Ok(ListResponse::Legacy(
            items.into_iter().skip(offset).take(limit).collect(),
        ))
    }
}

pub async fn list_webgemeindezentren(
    State(state): State<ApiState>,
    Query(params): Query<HashMap<String, String>>,
) -> Result<Json<ListResponse<Webgemeindezentrum>>, StatusCode> {
    let pool = database_pool(&state)?;
    let mut centers: Vec<Webgemeindezentrum> = load_active_ortswebereien(pool)
        .await
        .map_err(|error| internal_error(error, "list_webgemeindezentren"))?
        .into_iter()
        .map(|ortsweberei| ortsweberei.webgemeindezentrum)
        .collect();
    centers.sort_by(|left, right| left.id.cmp(&right.id));
    paginate(centers, &params, |center| center.id.as_str()).map(Json)
}

pub async fn get_webgemeindezentrum(
    State(state): State<ApiState>,
    Path(id): Path<String>,
) -> Result<Json<WebgemeindezentrumDetails>, StatusCode> {
    let pool = database_pool(&state)?;
    let center = load_active_ortswebereien(pool)
        .await
        .map_err(|error| internal_error(error, "get_webgemeindezentrum"))?
        .into_iter()
        .map(|ortsweberei| ortsweberei.webgemeindezentrum)
        .find(|center| center.id == id)
        .ok_or(StatusCode::NOT_FOUND)?;

    let governance_row: (i64, i64, i64, i64) = sqlx::query_as(
        "SELECT \
             (SELECT count(*)::bigint FROM governance_proposals WHERE webgemeindezentrum_id = $1), \
             (SELECT count(*)::bigint FROM governance_proposals WHERE webgemeindezentrum_id = $1 AND status IN ('consent', 'voting')), \
             (SELECT count(*)::bigint FROM governance_proposals WHERE webgemeindezentrum_id = $1 AND status = 'voting'), \
             (SELECT count(*)::bigint FROM domain_messages WHERE conversation_id = $2::uuid AND deleted_at IS NULL)",
    )
    .bind(&id)
    .bind(&center.conversation_id)
    .fetch_one(pool)
    .await
    .map_err(|error| internal_error(error, "get_webgemeindezentrum_governance"))?;
    let governance = WebgemeindezentrumGovernance {
        proposal_count: governance_row.0,
        open_proposal_count: governance_row.1,
        voting_proposal_count: governance_row.2,
        conversation_message_count: governance_row.3,
    };

    let rows = sqlx::query(
        "SELECT event_id, event_type, location_state, lat, lon, \
                location_label, reason, decided_at \
         FROM webgemeindezentrum_location_history \
         WHERE webgemeindezentrum_id = $1 \
         ORDER BY decided_at DESC, event_id DESC",
    )
    .bind(&id)
    .fetch_all(pool)
    .await
    .map_err(|error| internal_error(error, "get_webgemeindezentrum_history"))?;

    let location_history = rows
        .into_iter()
        .map(|row| {
            let raw_state: String = row.try_get("location_state")?;
            let state = WebgemeindezentrumLocationState::parse(&raw_state)?;
            Ok(WebgemeindezentrumLocationHistoryEvent {
                event_id: row.try_get("event_id")?,
                event_type: row.try_get("event_type")?,
                location_state: state,
                location_state_label: state.label(),
                location: Location {
                    lat: row.try_get("lat")?,
                    lon: row.try_get("lon")?,
                },
                location_label: row.try_get("location_label")?,
                reason: row.try_get("reason")?,
                decided_at: timestamp(row.try_get("decided_at")?),
            })
        })
        .collect::<Result<Vec<_>, sqlx::Error>>()
        .map_err(|error| internal_error(error, "decode_webgemeindezentrum_history"))?;

    Ok(Json(WebgemeindezentrumDetails {
        center,
        governance,
        location_history,
    }))
}

pub async fn list_ortswebereien(
    State(state): State<ApiState>,
    Query(params): Query<HashMap<String, String>>,
) -> Result<Json<ListResponse<Ortsweberei>>, StatusCode> {
    let pool = database_pool(&state)?;
    let ortswebereien = load_active_ortswebereien(pool)
        .await
        .map_err(|error| internal_error(error, "list_ortswebereien"))?;
    paginate(ortswebereien, &params, |ortsweberei| {
        ortsweberei.id.as_str()
    })
    .map(Json)
}

pub async fn get_ortsweberei(
    State(state): State<ApiState>,
    Path(id): Path<String>,
) -> Result<Json<Ortsweberei>, StatusCode> {
    let pool = database_pool(&state)?;
    load_active_ortswebereien(pool)
        .await
        .map_err(|error| internal_error(error, "get_ortsweberei"))?
        .into_iter()
        .find(|ortsweberei| ortsweberei.id == id)
        .map(Json)
        .ok_or(StatusCode::NOT_FOUND)
}

fn center_activity_operation_id(
    faden_type: super::edges::FadenType,
    account_id: &str,
    center_id: &str,
    subject_id: &str,
) -> String {
    fn component(value: &str) -> String {
        format!("{}:{value}", value.len())
    }
    let name = format!(
        "weltgewebe:webgemeindezentrum-participation-faden:v2|{}|{}|{}|{}",
        component(faden_type.as_str()),
        component(account_id),
        component(center_id),
        component(subject_id),
    );
    Uuid::new_v5(&Uuid::NAMESPACE_URL, name.as_bytes()).to_string()
}

/// Project one explicit governance or conversation action as a temporary
/// account-to-center Faden. The readable center id remains the public URL id;
/// the edge uses its deterministic UUID alias to preserve the strict endpoint
/// contract used by every other Faden.
pub(crate) async fn ensure_webgemeindezentrum_activity_faden(
    state: &ApiState,
    auth: &AuthContext,
    center_id: &str,
    faden_type: super::edges::FadenType,
    subject_id: &str,
) -> Result<(), (StatusCode, String)> {
    project_webgemeindezentrum_activity_faden(
        state,
        auth,
        center_id,
        faden_type,
        subject_id,
        super::edges::FadenProjectionMode::Reactivate,
    )
    .await
}

/// Repair an absent center projection for an exact durable-action replay
/// without extending the already-recorded participation lifecycle.
pub(crate) async fn repair_webgemeindezentrum_activity_faden(
    state: &ApiState,
    auth: &AuthContext,
    center_id: &str,
    faden_type: super::edges::FadenType,
    subject_id: &str,
) -> Result<(), (StatusCode, String)> {
    project_webgemeindezentrum_activity_faden(
        state,
        auth,
        center_id,
        faden_type,
        subject_id,
        super::edges::FadenProjectionMode::EnsureOnly,
    )
    .await
}

async fn project_webgemeindezentrum_activity_faden(
    state: &ApiState,
    auth: &AuthContext,
    center_id: &str,
    faden_type: super::edges::FadenType,
    subject_id: &str,
    projection_mode: super::edges::FadenProjectionMode,
) -> Result<(), (StatusCode, String)> {
    let account_id = auth.account_id.as_deref().ok_or_else(|| {
        (
            StatusCode::UNAUTHORIZED,
            "authenticated account context missing".to_string(),
        )
    })?;
    if Uuid::parse_str(account_id).is_err() {
        tracing::warn!(
            event = "webgemeindezentrum.faden_projection.skipped_legacy_identifier",
            center_id,
            account_id,
            faden_type = faden_type.as_str(),
            subject_id,
            "Center participation cannot be represented by the UUID-only Faden contract"
        );
        return Ok(());
    }

    let _slot = center_activity_slots().acquire().await.map_err(|error| {
        tracing::error!(%error, center_id, "center activity projection limiter closed");
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            "failed to reserve Webgemeindezentrum Faden capacity".to_string(),
        )
    })?;

    let pool = database_pool(state).map_err(|status| {
        (
            status,
            "Webgemeindezentrum store unavailable for Faden projection".to_string(),
        )
    })?;
    let mut account_guard = pool.begin().await.map_err(|error| {
        tracing::error!(%error, account_id, "failed to begin center Faden lifecycle guard");
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            "failed to guard Webgemeindezentrum Faden projection".to_string(),
        )
    })?;
    sqlx::query("SELECT pg_advisory_xact_lock($1::bigint)")
        .bind(crate::advisory_lock::account_lifecycle_lock_key(account_id))
        .execute(&mut *account_guard)
        .await
        .map_err(|error| {
            tracing::error!(%error, account_id, "failed to lock account lifecycle for center Faden");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                "failed to guard Webgemeindezentrum Faden projection".to_string(),
            )
        })?;
    let account_is_active: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM domain_accounts WHERE id = $1 AND disabled = FALSE)",
    )
    .bind(account_id)
    .fetch_one(&mut *account_guard)
    .await
    .map_err(|error| {
        tracing::error!(%error, account_id, "failed to verify center Faden actor");
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            "failed to verify Webgemeindezentrum Faden actor".to_string(),
        )
    })?;
    if !account_is_active {
        return Ok(());
    }

    let endpoint_id: Option<String> = sqlx::query_scalar(
        "SELECT faden_endpoint_id::text FROM webgemeindezentren WHERE id = $1",
    )
    .bind(center_id)
    .fetch_optional(&mut *account_guard)
    .await
    .map_err(|error| {
        tracing::error!(%error, center_id, faden_type = faden_type.as_str(), subject_id, "failed to resolve center Faden endpoint");
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            "failed to resolve Webgemeindezentrum Faden endpoint".to_string(),
        )
    })?;
    let endpoint_id = endpoint_id.ok_or_else(|| {
        (
            StatusCode::NOT_FOUND,
            "Webgemeindezentrum Faden endpoint not found".to_string(),
        )
    })?;
    let operation_id = center_activity_operation_id(faden_type, account_id, center_id, subject_id);
    let payload = json!({
        "source_id": account_id,
        "source_type": "account",
        "target_id": endpoint_id,
        "target_type": "webgemeindezentrum",
        "edge_kind": "reference",
        "faden_type": faden_type,
        "faden_subject_id": subject_id,
        "operation_id": operation_id,
    });

    let projection = match projection_mode {
        super::edges::FadenProjectionMode::EnsureOnly => {
            super::edges::ensure_derived_faden(state.clone(), auth.clone(), payload).await
        }
        super::edges::FadenProjectionMode::Reactivate => {
            super::edges::reactivate_derived_faden(state.clone(), auth.clone(), payload).await
        }
    }
    .map(|_| ())
    .map_err(|(status, message)| {
        tracing::error!(
            event = "webgemeindezentrum.faden_projection.failed",
            center_id,
            account_id,
            faden_type = faden_type.as_str(),
            subject_id,
            %status,
            error = %message,
            "Durable action exists but its derived center Faden is missing"
        );
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            "derived Webgemeindezentrum Faden could not be projected".to_string(),
        )
    });
    account_guard.commit().await.map_err(|error| {
        tracing::error!(%error, account_id, "failed to release center Faden lifecycle guard");
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            "failed to release Webgemeindezentrum Faden guard".to_string(),
        )
    })?;
    projection
}

#[cfg(test)]
mod tests {
    use super::{
        center_activity_operation_id, center_activity_slot_count, WebgemeindezentrumLocationState,
    };

    #[test]
    fn location_state_labels_do_not_claim_confirmation_for_intentions() {
        assert_eq!(
            WebgemeindezentrumLocationState::Desired.label(),
            "Gewünschter Treffort"
        );
        assert_eq!(
            WebgemeindezentrumLocationState::Provisional.label(),
            "Vorläufiger Treffort"
        );
        assert_eq!(
            WebgemeindezentrumLocationState::Confirmed.label(),
            "Bestätigter Treffort"
        );
    }

    #[test]
    fn center_activity_projection_reserves_pool_capacity() {
        for pool_max_connections in [3, 4, 5, 32] {
            let slots = center_activity_slot_count(pool_max_connections);
            assert!(slots > 0);
            assert!(slots * 2 < pool_max_connections);
        }
    }

    #[test]
    fn center_activity_faden_operation_is_stable_and_unambiguous() {
        let first = center_activity_operation_id(
            super::super::edges::FadenType::Proposal,
            "account-a",
            "webgemeindezentrum-hammer-park",
            "proposal-a",
        );
        let replay = center_activity_operation_id(
            super::super::edges::FadenType::Proposal,
            "account-a",
            "webgemeindezentrum-hammer-park",
            "proposal-a",
        );
        let different_subject = center_activity_operation_id(
            super::super::edges::FadenType::Proposal,
            "account-a",
            "webgemeindezentrum-hammer-park",
            "proposal-b",
        );

        assert_eq!(first, replay);
        assert_ne!(first, different_subject);
        assert!(uuid::Uuid::parse_str(&first).is_ok());
    }

    #[test]
    fn rejects_unknown_location_states_from_the_database() {
        assert!(WebgemeindezentrumLocationState::parse("reserved").is_err());
    }
}
