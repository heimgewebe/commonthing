//! HTTP-Oberfläche des Antragssystems (`docs/specs/governance-antraege.md`).
//!
//! Leserechte sind öffentlich. Angemeldete Gäste dürfen den eigenen
//! Weberantrag stellen, in offenen Gesprächsräumen mitreden, eigene offene
//! Anträge zurückziehen und den eigenen Account auflösen. Formale Vetos, Stimmen
//! und Aufhebungsanträge bleiben Webern und Administratoren vorbehalten.
//!
//! PostgreSQL ist kanonisch: ohne konfigurierten Pool antworten alle
//! Governance-Endpunkte fail-closed mit 503 — es gibt keinen JSONL- oder
//! In-Memory-Fallback. Vor jedem Read werden fällige Fristen serverseitig
//! und idempotent finalisiert (zusätzlich läuft der Sweeper in `lib.rs`).

use axum::{
    extract::{Path, State},
    http::{header, StatusCode},
    response::{IntoResponse, Response},
    Extension, Json,
};
use chrono::{DateTime, Utc};
#[cfg(feature = "integration-testing")]
use serde::Deserialize;
use serde::Serialize;
use serde_json::Value;
use sqlx::{PgPool, Postgres, Transaction};
use std::{env, sync::OnceLock};
use tokio::{
    sync::SemaphorePermit,
    time::{timeout, Duration},
};

use super::webgemeindezentren::{
    ensure_webgemeindezentrum_activity_faden, repair_webgemeindezentrum_activity_faden,
};
use crate::auth::{challenges::ChallengeIntent, role::Role};
use crate::config::{
    DomainAccountWriteSource, DomainEdgeWriteSource, DomainNodeWriteSource, DomainReadSource,
};
use crate::governance::{
    self, CreateProposalError, GuestExitError, MessageError, ProposalListEntry, ProposalMessage,
    ProposalStatus, ProposalWithCounts, Veto, VetoError, VoteChoice, VoteError, VoteWriteOutcome,
    MESSAGE_BODY_MAX_CHARS, PROPOSAL_TITLE_MAX_CHARS, SUMMARY_MAX_CHARS, VETO_REASON_MAX_CHARS,
};
use crate::middleware::auth::AuthContext;
use crate::state::{reserve_postgres_domain_operation, ApiState};

type ApiError = (StatusCode, String);

const GUEST_EXIT_STEP_UP_ENABLED_ENV: &str = "AUTH_GUEST_EXIT_STEP_UP_ENABLED";
static GUEST_EXIT_STEP_UP_ENABLED: OnceLock<bool> = OnceLock::new();

fn parse_guest_exit_step_up_enabled(raw: Option<&str>) -> bool {
    raw.is_some_and(|value| {
        let value = value.trim();
        value == "1" || value.eq_ignore_ascii_case("true")
    })
}

fn guest_exit_step_up_enabled() -> bool {
    *GUEST_EXIT_STEP_UP_ENABLED.get_or_init(|| {
        let raw = env::var(GUEST_EXIT_STEP_UP_ENABLED_ENV).ok();
        let enabled = parse_guest_exit_step_up_enabled(raw.as_deref());
        if raw.as_deref().is_some_and(|value| {
            let value = value.trim();
            !matches!(value, "0" | "1")
                && !value.eq_ignore_ascii_case("true")
                && !value.eq_ignore_ascii_case("false")
        }) {
            tracing::warn!(
                env_var = GUEST_EXIT_STEP_UP_ENABLED_ENV,
                value = raw.as_deref().unwrap_or_default(),
                "invalid guest-exit step-up rollout flag; failing closed"
            );
        }
        enabled
    })
}

fn private_no_store_json<T: Serialize>(value: T) -> Response {
    ([(header::CACHE_CONTROL, "private, no-store")], Json(value)).into_response()
}

fn record_guest_exit_session_cleanup(
    account_id: &str,
    cleanup: crate::auth::session::SessionResult<()>,
) {
    if let Err(error) = cleanup {
        tracing::warn!(
            error = %error,
            account_id,
            "guest exit committed; secondary session backend cleanup failed"
        );
    }
}

/// Fail-closed-Torwächter: Governance existiert nur mit PostgreSQL.
fn require_pool(state: &ApiState) -> Result<&PgPool, ApiError> {
    if state.config.domain_read_source != DomainReadSource::Postgres
        || state.config.domain_account_write_source != DomainAccountWriteSource::Postgres
    {
        return Err((
            StatusCode::SERVICE_UNAVAILABLE,
            "governance requires PostgreSQL as the canonical account read and write source"
                .to_string(),
        ));
    }
    state.db_pool.as_ref().ok_or((
        StatusCode::SERVICE_UNAVAILABLE,
        "governance requires a configured PostgreSQL database".to_string(),
    ))
}

fn require_guest_exit_pool(state: &ApiState) -> Result<&PgPool, ApiError> {
    if state.config.domain_read_source != DomainReadSource::Postgres
        || state.config.domain_account_write_source != DomainAccountWriteSource::Postgres
        || state.config.domain_node_write_source != DomainNodeWriteSource::Postgres
        || state.config.domain_edge_write_source != DomainEdgeWriteSource::Postgres
    {
        return Err((
            StatusCode::SERVICE_UNAVAILABLE,
            "guest exit requires PostgreSQL as the canonical account, node and edge source"
                .to_string(),
        ));
    }
    state.db_pool.as_ref().ok_or((
        StatusCode::SERVICE_UNAVAILABLE,
        "guest exit requires a configured PostgreSQL database".to_string(),
    ))
}

fn internal_error(context: &'static str) -> impl Fn(sqlx::Error) -> ApiError {
    move |error| {
        tracing::error!(error = %error, context, "governance database operation failed");
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            "governance operation failed".to_string(),
        )
    }
}

// Governance GET handlers finalize due proposals (which may write canonical
// accounts) BEFORE entering the visibility fence. The general middleware
// cannot fence these GETs without holding FOR SHARE across its own writers.
// Reserve two shared SQLx budget units before taking a separate transaction
// that holds the global generation row FOR SHARE until response serialization.
// This also prevents another API process from publishing an old public node
// target after its public -> private update has committed.
fn proposal_privacy_unavailable() -> ApiError {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        "proposal privacy snapshot unavailable".to_string(),
    )
}

async fn begin_proposal_privacy_fence(
    pool: &PgPool,
) -> Result<(SemaphorePermit<'static>, Transaction<'static, Postgres>), ApiError> {
    let slot = match timeout(Duration::from_secs(3), reserve_postgres_domain_operation()).await {
        Ok(Ok(slot)) => slot,
        _ => return Err(proposal_privacy_unavailable()),
    };
    let (tx, _generation) = match timeout(
        Duration::from_secs(3),
        crate::middleware::domain_projection::begin_fenced_generation(pool),
    )
    .await
    {
        Ok(Ok(fence)) => fence,
        Ok(Err(error)) => {
            tracing::warn!(%error, "cannot acquire proposal privacy snapshot");
            return Err(proposal_privacy_unavailable());
        }
        Err(_) => return Err(proposal_privacy_unavailable()),
    };
    Ok((slot, tx))
}

async fn finish_proposal_privacy_fence(
    tx: Transaction<'static, Postgres>,
    response: Response,
) -> Result<Response, ApiError> {
    // If rollback fails the already-built response must NOT leave this
    // function: a private target could otherwise escape after losing the
    // cross-process lock. Any error path drops the response with no payload.
    if let Err(error) = tx.rollback().await {
        tracing::warn!(%error, "cannot release proposal privacy snapshot");
        return Err(proposal_privacy_unavailable());
    }
    Ok(response)
}

fn require_account_id(auth: &AuthContext) -> Result<String, ApiError> {
    if !auth.authenticated {
        return Err((
            StatusCode::UNAUTHORIZED,
            "authentication required".to_string(),
        ));
    }
    auth.account_id.clone().ok_or((
        StatusCode::UNAUTHORIZED,
        "authenticated account context missing".to_string(),
    ))
}

fn require_formal_governance_actor(auth: &AuthContext) -> Result<String, ApiError> {
    let account_id = require_account_id(auth)?;
    if !matches!(auth.role, Role::Weber | Role::Admin) {
        return Err((
            StatusCode::FORBIDDEN,
            "formal governance actions require Weber or administrator status".to_string(),
        ));
    }
    Ok(account_id)
}

/// Anzeigename aus dem laufenden Account-Store; Fallback, falls die Projektion
/// den Account (noch) nicht kennt.
async fn account_title(state: &ApiState, account_id: &str) -> String {
    let accounts = state.accounts.read().await;
    accounts
        .get(account_id)
        .map(|account| account.public.title.clone())
        .unwrap_or_else(|| "Unbenannter Account".to_string())
}

/// Fällige Fristen serverseitig und idempotent auswerten, Beförderungen in
/// den laufenden Store spiegeln. Läuft vor jedem Governance-Read.
async fn finalize_due(state: &ApiState, pool: &PgPool) -> Result<(), ApiError> {
    let outcomes = governance::finalize_due_proposals(pool, Utc::now())
        .await
        .map_err(internal_error("finalize_due_proposals"))?;
    governance::apply_promotions_to_store(&state.accounts, &outcomes).await;
    Ok(())
}

/// Öffentliche Antragsprojektion inklusive verbleibender Frist in Sekunden.
#[derive(Debug, Serialize)]
pub struct ProposalView {
    pub id: String,
    pub kind: String,
    pub webgemeindezentrum_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub title: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub target_node_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub target_node_title: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub repeals_proposal_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pending_repeal_proposal_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub repealed_by_proposal_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub repealed_at: Option<DateTime<Utc>>,
    pub applicant_account_id: Option<String>,
    pub applicant_title: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub summary: Option<String>,
    pub status: ProposalStatus,
    pub created_at: DateTime<Utc>,
    pub consent_until: DateTime<Utc>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub voting_until: Option<DateTime<Utc>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub finalized_at: Option<DateTime<Utc>>,
    /// Jüngste kanonische öffentliche Fachaktivität des Antrags.
    pub last_activity_at: DateTime<Utc>,
    pub veto_count: i64,
    pub message_count: i64,
    pub yes_votes: i64,
    pub no_votes: i64,
    pub abstain_votes: i64,
    /// Verbleibende Sekunden der laufenden Phase; `None` nach Finalisierung.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub remaining_seconds: Option<i64>,
}

fn proposal_view(proposal: ProposalWithCounts, now: DateTime<Utc>) -> ProposalView {
    let remaining_seconds = match proposal.status {
        ProposalStatus::Consent => Some((proposal.consent_until - now).num_seconds().max(0)),
        ProposalStatus::Voting => proposal
            .voting_until
            .map(|until| (until - now).num_seconds().max(0)),
        ProposalStatus::Accepted | ProposalStatus::Rejected | ProposalStatus::Withdrawn => None,
    };

    ProposalView {
        id: proposal.id,
        kind: proposal.kind,
        webgemeindezentrum_id: proposal.webgemeindezentrum_id,
        title: proposal.title,
        target_node_id: proposal.target_node_id,
        target_node_title: proposal.target_node_title,
        repeals_proposal_id: proposal.repeals_proposal_id,
        pending_repeal_proposal_id: proposal.pending_repeal_proposal_id,
        repealed_by_proposal_id: proposal.repealed_by_proposal_id,
        repealed_at: proposal.repealed_at,
        applicant_account_id: proposal.applicant_account_id,
        applicant_title: proposal.applicant_title,
        summary: proposal.summary,
        status: proposal.status,
        created_at: proposal.created_at,
        consent_until: proposal.consent_until,
        voting_until: proposal.voting_until,
        finalized_at: proposal.finalized_at,
        last_activity_at: proposal.last_activity_at,
        veto_count: proposal.veto_count,
        message_count: proposal.message_count,
        yes_votes: proposal.yes_votes,
        no_votes: proposal.no_votes,
        abstain_votes: proposal.abstain_votes,
        remaining_seconds,
    }
}

/// Explizite, rein betrachterbezogene Beteiligungsfakten. `vote_choice = null`
/// bedeutet belastbar „noch keine Stimme“, nicht „Feld fehlt“.
#[derive(Debug, Serialize)]
pub struct ProposalViewerParticipation {
    pub vote_choice: Option<String>,
    pub has_veto: bool,
    pub may_vote: bool,
    pub may_veto: bool,
}

/// Listenprojektion. Die Fachwahrheit bleibt Governance; Attention leitet
/// daraus nur Bedeutung ab. Anonyme Leser erhalten `viewer_participation: null`.
#[derive(Debug, Serialize)]
pub struct ProposalListView {
    #[serde(flatten)]
    pub proposal: ProposalView,
    pub viewer_participation: Option<ProposalViewerParticipation>,
    // Transitional wire compatibility for browser tabs loaded before the
    // viewer_participation cutover. New consumers must use the nested contract.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub own_vote: Option<String>,
    pub own_veto: bool,
    pub can_vote: bool,
    pub can_veto: bool,
}

fn proposal_list_view(
    entry: ProposalListEntry,
    now: DateTime<Utc>,
    auth: &AuthContext,
) -> ProposalListView {
    let ProposalListEntry {
        proposal,
        own_vote,
        own_veto,
    } = entry;
    let formal_actor = auth.authenticated && matches!(auth.role, Role::Weber | Role::Admin);
    let own_proposal = auth.account_id.as_deref() == proposal.applicant_account_id.as_deref();
    let may_vote = formal_actor
        && !own_proposal
        && proposal.status == ProposalStatus::Voting
        && proposal.voting_until.is_some_and(|until| now < until);
    let may_veto = formal_actor
        && !own_proposal
        && proposal.status == ProposalStatus::Consent
        && now < proposal.consent_until
        && !own_veto;
    let viewer_participation = if auth.authenticated && auth.account_id.is_some() {
        Some(ProposalViewerParticipation {
            vote_choice: own_vote.clone(),
            has_veto: own_veto,
            may_vote,
            may_veto,
        })
    } else {
        None
    };

    ProposalListView {
        proposal: proposal_view(proposal, now),
        viewer_participation,
        own_vote,
        own_veto,
        can_vote: may_vote,
        can_veto: may_veto,
    }
}

#[derive(Debug, Serialize)]
pub struct ProposalDetailView {
    #[serde(flatten)]
    pub proposal: ProposalView,
    pub vetoes: Vec<Veto>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub own_vote: Option<String>,
}

#[cfg(feature = "integration-testing")]
#[derive(Debug, Deserialize)]
pub struct GovernanceTestingAdvancePayload {
    pub now: DateTime<Utc>,
}

#[cfg(feature = "integration-testing")]
/// Advance one proof proposal through the genuine production finalizer at an
/// explicit test timestamp. The route exists only in integration-testing builds
/// and refuses non-proof applicants.
pub async fn governance_testing_advance_proposal(
    State(state): State<ApiState>,
    Path(id): Path<String>,
    Json(payload): Json<GovernanceTestingAdvancePayload>,
) -> Result<Json<ProposalView>, ApiError> {
    let pool = require_pool(&state)?;
    let before = governance::get_proposal(pool, &id)
        .await
        .map_err(internal_error("get_proposal"))?
        .ok_or((StatusCode::NOT_FOUND, "proposal not found".to_string()))?;
    if !before
        .applicant_account_id
        .as_deref()
        .is_some_and(|account_id| account_id.starts_with("proof-governance-"))
    {
        return Err((
            StatusCode::FORBIDDEN,
            "testing advance is restricted to governance proof accounts".to_string(),
        ));
    }

    let outcomes = governance::finalize_testing_proposal(pool, &id, payload.now)
        .await
        .map_err(internal_error("finalize_testing_proposal"))?;
    governance::apply_promotions_to_store(&state.accounts, &outcomes).await;
    let after = governance::get_proposal(pool, &id)
        .await
        .map_err(internal_error("get_proposal"))?
        .ok_or((StatusCode::NOT_FOUND, "proposal not found".to_string()))?;
    Ok(Json(proposal_view(after, payload.now)))
}

/// GET /proposals — öffentliche Liste, neueste zuerst.
pub async fn list_proposals(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
) -> Result<Response, ApiError> {
    let pool = require_pool(&state)?;
    finalize_due(&state, pool).await?;
    let (_slot, visibility_tx) = begin_proposal_privacy_fence(pool).await?;

    let now = Utc::now();
    let viewer_account_id = if auth.authenticated {
        auth.account_id.as_deref()
    } else {
        None
    };
    let proposals = governance::list_proposals_for_viewer(pool, viewer_account_id)
        .await
        .map_err(internal_error("list_proposals_for_viewer"))?;
    let response = private_no_store_json(
        proposals
            .into_iter()
            .map(|proposal| proposal_list_view(proposal, now, &auth))
            .collect::<Vec<_>>(),
    );
    finish_proposal_privacy_fence(visibility_tx, response).await
}

/// GET /proposals/{id} — öffentliche Informationsseite inkl. Vetos; für
/// angemeldete Accounts zusätzlich die eigene aktuelle Stimme.
pub async fn get_proposal(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
    Path(id): Path<String>,
) -> Result<Response, ApiError> {
    let pool = require_pool(&state)?;
    finalize_due(&state, pool).await?;
    let (_slot, visibility_tx) = begin_proposal_privacy_fence(pool).await?;

    let proposal = governance::get_proposal(pool, &id)
        .await
        .map_err(internal_error("get_proposal"))?
        .ok_or((StatusCode::NOT_FOUND, "proposal not found".to_string()))?;

    let vetoes = governance::list_vetoes(pool, &id)
        .await
        .map_err(internal_error("list_vetoes"))?;

    let own_vote = match (&auth.authenticated, &auth.account_id) {
        (true, Some(account_id)) => governance::get_own_vote(pool, &id, account_id)
            .await
            .map_err(internal_error("get_own_vote"))?,
        _ => None,
    };

    let response = private_no_store_json(ProposalDetailView {
        proposal: proposal_view(proposal, Utc::now()),
        vetoes,
        own_vote,
    });
    finish_proposal_privacy_fence(visibility_tx, response).await
}

fn bad_request(message: &str) -> ApiError {
    (StatusCode::BAD_REQUEST, message.to_string())
}

fn optional_trimmed_text(
    value: Option<&Value>,
    field: &'static str,
    max_chars: usize,
) -> Result<Option<String>, ApiError> {
    match value {
        None => Ok(None),
        Some(Value::String(text)) => {
            let trimmed = text.trim();
            if trimmed.is_empty() {
                return Err(bad_request(&format!(
                    "{field} must not be blank when present"
                )));
            }
            if trimmed.chars().count() > max_chars {
                return Err(bad_request(&format!(
                    "{field} exceeds the maximum length of {max_chars} characters"
                )));
            }
            Ok(Some(trimmed.to_string()))
        }
        Some(_) => Err(bad_request(&format!("{field} must be a string"))),
    }
}

fn required_trimmed_text(
    value: Option<&Value>,
    field: &'static str,
    max_chars: usize,
) -> Result<String, ApiError> {
    optional_trimmed_text(value, field, max_chars)?
        .ok_or_else(|| bad_request(&format!("missing or empty field: {field}")))
}

/// POST /proposals — Weberantrag für Gäste oder Sachantrag für Weber/Admin.
/// Erlaubte Felder werden je `kind` strikt getrennt geprüft.
pub async fn create_proposal(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
    Json(payload): Json<Value>,
) -> Result<Response, ApiError> {
    let account_id = require_account_id(&auth)?;

    let pool = require_pool(&state)?;

    let object = payload
        .as_object()
        .ok_or_else(|| bad_request("proposal request must be a JSON object"))?;
    let kind = object
        .get("kind")
        .and_then(Value::as_str)
        .ok_or_else(|| bad_request("kind must be one of: weberantrag, sachantrag"))?;
    let allowed_fields: &[&str] = match kind {
        "weberantrag" => &["kind", "summary", "webgemeindezentrum_id"],
        "sachantrag" => &[
            "kind",
            "title",
            "summary",
            "webgemeindezentrum_id",
            "target_node_id",
        ],
        _ => return Err(bad_request("kind must be one of: weberantrag, sachantrag")),
    };
    for key in object.keys() {
        if !allowed_fields.contains(&key.as_str()) {
            return Err(bad_request(&format!("unknown field for {kind}: {key}")));
        }
    }
    let summary = optional_trimmed_text(object.get("summary"), "summary", SUMMARY_MAX_CHARS)?;
    let webgemeindezentrum_id = optional_trimmed_text(
        object.get("webgemeindezentrum_id"),
        "webgemeindezentrum_id",
        128,
    )?;

    let applicant_title = account_title(&state, &account_id).await;
    let proposal_result = match kind {
        "weberantrag" => {
            if auth.role != Role::Gast {
                return Err((
                    StatusCode::CONFLICT,
                    "account already holds Weber status".to_string(),
                ));
            }
            governance::create_weber_proposal_at_center(
                pool,
                &account_id,
                &applicant_title,
                summary.as_deref(),
                webgemeindezentrum_id.as_deref(),
                Utc::now(),
            )
            .await
        }
        "sachantrag" => {
            if !matches!(auth.role, Role::Weber | Role::Admin) {
                return Err((
                    StatusCode::FORBIDDEN,
                    "Sachantraege require Weber or administrator status".to_string(),
                ));
            }
            let title =
                required_trimmed_text(object.get("title"), "title", PROPOSAL_TITLE_MAX_CHARS)?;
            let target_node_id =
                optional_trimmed_text(object.get("target_node_id"), "target_node_id", 200)?;
            governance::create_sach_proposal_at_center(
                pool,
                &account_id,
                &applicant_title,
                &title,
                summary.as_deref(),
                webgemeindezentrum_id.as_deref(),
                target_node_id.as_deref(),
                Utc::now(),
            )
            .await
        }
        _ => unreachable!("kind was validated above"),
    };
    let proposal = proposal_result.map_err(|error| match error {
        CreateProposalError::AlreadyOpen => (
            StatusCode::CONFLICT,
            "an open proposal of this kind already exists for this account".to_string(),
        ),
        CreateProposalError::NotGuest => (
            StatusCode::CONFLICT,
            "only an active guest account may apply for Weber status".to_string(),
        ),
        CreateProposalError::NotSachApplicant => (
            StatusCode::FORBIDDEN,
            "only an active Weber or administrator may create a Sachantrag".to_string(),
        ),
        CreateProposalError::CenterUnavailable => (
            StatusCode::SERVICE_UNAVAILABLE,
            "the requested Webgemeindezentrum is not an active governance center".to_string(),
        ),
        CreateProposalError::TargetNodeNotFound => (
            StatusCode::NOT_FOUND,
            "the target node does not exist".to_string(),
        ),
        CreateProposalError::Database(error) => internal_error("create_proposal")(error),
    })?;

    if let Err((status, error)) = ensure_webgemeindezentrum_activity_faden(
        &state,
        &auth,
        &proposal.webgemeindezentrum_id,
        super::edges::FadenType::Proposal,
        &proposal.id,
    )
    .await
    {
        tracing::error!(
            event = "governance.proposal_faden_projection.failed",
            proposal_id = %proposal.id,
            center_id = %proposal.webgemeindezentrum_id,
            %status,
            %error,
            "Proposal remains durable; its derived center Faden is missing"
        );
    }

    tracing::info!(
        event = "governance.proposal.created",
        proposal_id = %proposal.id,
        kind = %proposal.kind,
        center_id = %proposal.webgemeindezentrum_id,
        "Governance proposal created"
    );

    let now = Utc::now();
    // The proposal has already committed, and its Faden may have performed
    // additional writes. Acquire the existing DB generation-row privacy fence
    // only NOW, never while our own transaction or Faden writer is active.
    // Creation's original target snapshot may no longer be publicly visible:
    // re-read the canonical, visibility-masked proposal under the same fence
    // kept through completed HTTP JSON construction.
    if proposal.target_node_id.is_some() {
        let (_slot, visibility_tx) = begin_proposal_privacy_fence(pool).await?;
        let current = governance::get_proposal(pool, &proposal.id)
            .await
            .map_err(internal_error("get_proposal_after_create"))?
            .ok_or_else(proposal_privacy_unavailable)?;
        let response = (
            StatusCode::CREATED,
            private_no_store_json(proposal_view(current, now)),
        )
            .into_response();
        return finish_proposal_privacy_fence(visibility_tx, response).await;
    }
    Ok((StatusCode::CREATED, Json(proposal_view(proposal, now))).into_response())
}

/// POST /proposals/{id}/withdraw — der Antragsteller beendet das eigene offene
/// Verfahren, ohne es oder seine bisherigen Beiträge zu löschen.
pub async fn withdraw_proposal(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
    Path(id): Path<String>,
) -> Result<Json<ProposalView>, ApiError> {
    let account_id = require_account_id(&auth)?;
    let pool = require_pool(&state)?;
    let proposal = governance::withdraw_proposal(pool, &id, &account_id, Utc::now())
        .await
        .map_err(|error| match error {
            governance::WithdrawProposalError::NotFound => {
                (StatusCode::NOT_FOUND, "proposal not found".to_string())
            }
            governance::WithdrawProposalError::NotApplicant => (
                StatusCode::FORBIDDEN,
                "only the applicant may withdraw this proposal".to_string(),
            ),
            governance::WithdrawProposalError::WrongPhase => (
                StatusCode::CONFLICT,
                "only a currently open proposal may be withdrawn".to_string(),
            ),
            governance::WithdrawProposalError::ActorUnavailable => (
                StatusCode::UNAUTHORIZED,
                "proposal applicant account is no longer active".to_string(),
            ),
            governance::WithdrawProposalError::Database(error) => {
                internal_error("withdraw_proposal")(error)
            }
        })?;

    tracing::info!(
        event = "governance.proposal.withdrawn",
        proposal_id = %id,
        applicant_account_id = %account_id,
        "Governance proposal withdrawn"
    );
    Ok(Json(proposal_view(proposal, Utc::now())))
}

/// POST /proposals/{id}/repeal — eröffnet einen neuen Sachantrag, der einen
/// angenommenen Sachbeschluss adressiert. Der Zielbeschluss wird nicht mutiert.
pub async fn request_repeal(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
    Path(id): Path<String>,
    Json(payload): Json<Value>,
) -> Result<(StatusCode, Json<ProposalView>), ApiError> {
    let account_id = require_formal_governance_actor(&auth)?;
    let pool = require_pool(&state)?;
    let object = payload
        .as_object()
        .ok_or_else(|| bad_request("repeal request must be a JSON object"))?;
    for key in object.keys() {
        if key != "summary" {
            return Err(bad_request(&format!("unknown field: {key}")));
        }
    }
    let summary = optional_trimmed_text(object.get("summary"), "summary", SUMMARY_MAX_CHARS)?;
    let applicant_title = account_title(&state, &account_id).await;
    let proposal = governance::create_repeal_proposal(
        pool,
        &id,
        &account_id,
        &applicant_title,
        summary.as_deref(),
        Utc::now(),
    )
    .await
    .map_err(|error| match error {
        governance::RepealProposalError::NotFound => {
            (StatusCode::NOT_FOUND, "proposal not found".to_string())
        }
        governance::RepealProposalError::TargetNotSachProposal => (
            StatusCode::CONFLICT,
            "only a Sachantrag decision can be repealed".to_string(),
        ),
        governance::RepealProposalError::TargetNotAccepted => (
            StatusCode::CONFLICT,
            "only an accepted Sachantrag can be repealed".to_string(),
        ),
        governance::RepealProposalError::TargetIsRepeal => (
            StatusCode::CONFLICT,
            "a repeal proposal cannot itself be repealed".to_string(),
        ),
        governance::RepealProposalError::AlreadyHasRepeal => (
            StatusCode::CONFLICT,
            "an open or accepted repeal proposal already exists for this decision".to_string(),
        ),
        governance::RepealProposalError::ActorNotEligible => (
            StatusCode::FORBIDDEN,
            "repeal proposals require Weber or administrator status".to_string(),
        ),
        governance::RepealProposalError::ActorUnavailable => (
            StatusCode::UNAUTHORIZED,
            "repeal applicant account is no longer active".to_string(),
        ),
        governance::RepealProposalError::Database(error) => {
            internal_error("create_repeal_proposal")(error)
        }
    })?;

    if let Err((status, error)) = ensure_webgemeindezentrum_activity_faden(
        &state,
        &auth,
        &proposal.webgemeindezentrum_id,
        super::edges::FadenType::Proposal,
        &proposal.id,
    )
    .await
    {
        tracing::error!(
            event = "governance.repeal_proposal_faden_projection.failed",
            proposal_id = %proposal.id,
            target_proposal_id = %id,
            center_id = %proposal.webgemeindezentrum_id,
            %status,
            %error,
            "Repeal proposal remains durable; its derived center Faden is missing"
        );
    }

    tracing::info!(
        event = "governance.repeal_proposal.created",
        proposal_id = %proposal.id,
        target_proposal_id = %id,
        applicant_account_id = %account_id,
        "Governance repeal proposal created"
    );
    Ok((
        StatusCode::CREATED,
        Json(proposal_view(proposal, Utc::now())),
    ))
}

/// POST /proposals/{id}/veto — begründetes Veto eines angemeldeten Accounts.
/// Der eigene Weberantrag bleibt von formaler Selbstentscheidung ausgeschlossen.
pub async fn veto_proposal(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
    Path(id): Path<String>,
    Json(payload): Json<Value>,
) -> Result<(StatusCode, Json<Veto>), ApiError> {
    let account_id = require_formal_governance_actor(&auth)?;
    let pool = require_pool(&state)?;

    let object = payload
        .as_object()
        .ok_or_else(|| bad_request("veto request must be a JSON object"))?;
    for key in object.keys() {
        if key != "reason" {
            return Err(bad_request(&format!("unknown field: {key}")));
        }
    }
    let reason = required_trimmed_text(object.get("reason"), "reason", VETO_REASON_MAX_CHARS)?;

    let proposal = governance::get_proposal(pool, &id)
        .await
        .map_err(internal_error("get_proposal_for_veto"))?
        .ok_or((StatusCode::NOT_FOUND, "proposal not found".to_string()))?;
    let title = account_title(&state, &account_id).await;
    let veto = governance::add_veto(pool, &id, &account_id, &title, &reason, Utc::now())
        .await
        .map_err(|error| match error {
            VetoError::NotFound => (StatusCode::NOT_FOUND, "proposal not found".to_string()),
            VetoError::WrongPhase => (
                StatusCode::CONFLICT,
                "veto is only possible during the open consent phase".to_string(),
            ),
            VetoError::AlreadyVetoed => (
                StatusCode::CONFLICT,
                "this account already vetoed the proposal".to_string(),
            ),
            VetoError::ApplicantCannotDecide => (
                StatusCode::FORBIDDEN,
                "the applicant cannot veto the own proposal".to_string(),
            ),
            VetoError::ActorNotEligible => (
                StatusCode::FORBIDDEN,
                "formal vetoes require Weber status".to_string(),
            ),
            VetoError::ActorUnavailable => (
                StatusCode::UNAUTHORIZED,
                "veto actor account is no longer active".to_string(),
            ),
            VetoError::Database(error) => internal_error("add_veto")(error),
        })?;

    if let Err((status, error)) = ensure_webgemeindezentrum_activity_faden(
        &state,
        &auth,
        &proposal.webgemeindezentrum_id,
        super::edges::FadenType::Vote,
        &id,
    )
    .await
    {
        tracing::error!(
            event = "governance.veto_faden_projection.failed",
            proposal_id = %id,
            center_id = %proposal.webgemeindezentrum_id,
            %status,
            %error,
            "Veto remains durable; its derived center Faden is missing"
        );
    }

    tracing::info!(
        event = "governance.veto.recorded",
        proposal_id = %id,
        center_id = %proposal.webgemeindezentrum_id,
        "Veto recorded"
    );

    Ok((StatusCode::CREATED, Json(veto)))
}

/// PUT /proposals/{id}/vote — genau eine aktuelle, änderbare Stimme je
/// angemeldetem Account; der Antragsteller stimmt nicht über die eigene Aufnahme ab.
pub async fn vote_proposal(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
    Path(id): Path<String>,
    Json(payload): Json<Value>,
) -> Result<Json<Value>, ApiError> {
    let account_id = require_formal_governance_actor(&auth)?;
    let pool = require_pool(&state)?;

    let object = payload
        .as_object()
        .ok_or_else(|| bad_request("vote request must be a JSON object"))?;
    for key in object.keys() {
        if key != "choice" {
            return Err(bad_request(&format!("unknown field: {key}")));
        }
    }
    let choice: VoteChoice = object
        .get("choice")
        .cloned()
        .and_then(|value| serde_json::from_value(value).ok())
        .ok_or_else(|| bad_request("choice must be one of: ja, nein, enthaltung"))?;

    let proposal = governance::get_proposal(pool, &id)
        .await
        .map_err(internal_error("get_proposal_for_vote"))?
        .ok_or((StatusCode::NOT_FOUND, "proposal not found".to_string()))?;

    let vote_outcome = governance::upsert_vote(pool, &id, &account_id, choice, Utc::now())
        .await
        .map_err(|error| match error {
            VoteError::NotFound => (StatusCode::NOT_FOUND, "proposal not found".to_string()),
            VoteError::WrongPhase => (
                StatusCode::CONFLICT,
                "voting is only possible during the open voting phase".to_string(),
            ),
            VoteError::ApplicantCannotDecide => (
                StatusCode::FORBIDDEN,
                "the applicant cannot vote on the own proposal".to_string(),
            ),
            VoteError::ActorNotEligible => (
                StatusCode::FORBIDDEN,
                "formal votes require Weber status".to_string(),
            ),
            VoteError::ActorUnavailable => (
                StatusCode::UNAUTHORIZED,
                "vote actor account is no longer active".to_string(),
            ),
            VoteError::Database(error) => internal_error("upsert_vote")(error),
        })?;

    let projection = match vote_outcome {
        VoteWriteOutcome::Created | VoteWriteOutcome::Changed => {
            ensure_webgemeindezentrum_activity_faden(
                &state,
                &auth,
                &proposal.webgemeindezentrum_id,
                super::edges::FadenType::Vote,
                &id,
            )
            .await
        }
        VoteWriteOutcome::Unchanged => {
            repair_webgemeindezentrum_activity_faden(
                &state,
                &auth,
                &proposal.webgemeindezentrum_id,
                super::edges::FadenType::Vote,
                &id,
            )
            .await
        }
    };
    if let Err((status, error)) = projection {
        tracing::error!(
            event = "governance.vote_faden_projection.failed",
            proposal_id = %id,
            center_id = %proposal.webgemeindezentrum_id,
            %status,
            %error,
            "Vote remains durable; its derived center Faden is missing"
        );
    }

    Ok(Json(serde_json::json!({ "choice": choice.as_str() })))
}

/// GET /proposals/{id}/messages — öffentlicher Gesprächsraum (lesend).
pub async fn list_proposal_messages(
    State(state): State<ApiState>,
    Path(id): Path<String>,
) -> Result<Json<Vec<ProposalMessage>>, ApiError> {
    let pool = require_pool(&state)?;
    finalize_due(&state, pool).await?;

    // 404 für unbekannte Anträge statt einer leeren Liste.
    governance::get_proposal(pool, &id)
        .await
        .map_err(internal_error("get_proposal"))?
        .ok_or((StatusCode::NOT_FOUND, "proposal not found".to_string()))?;

    let messages = governance::list_messages(pool, &id)
        .await
        .map_err(internal_error("list_messages"))?;
    Ok(Json(messages))
}

/// POST /proposals/{id}/messages — öffentlicher Beitrag eines angemeldeten
/// Accounts während einer offenen Phase.
pub async fn post_proposal_message(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
    Path(id): Path<String>,
    Json(payload): Json<Value>,
) -> Result<(StatusCode, Json<ProposalMessage>), ApiError> {
    let pool = require_pool(&state)?;
    let account_id = require_account_id(&auth)?;

    let object = payload
        .as_object()
        .ok_or_else(|| bad_request("message request must be a JSON object"))?;
    for key in object.keys() {
        if key != "body" {
            return Err(bad_request(&format!("unknown field: {key}")));
        }
    }
    let body = required_trimmed_text(object.get("body"), "body", MESSAGE_BODY_MAX_CHARS)?;

    let proposal = governance::get_proposal(pool, &id)
        .await
        .map_err(internal_error("get_proposal_for_message"))?
        .ok_or((StatusCode::NOT_FOUND, "proposal not found".to_string()))?;
    let title = account_title(&state, &account_id).await;
    let message = governance::add_message(pool, &id, &account_id, &title, &body, Utc::now())
        .await
        .map_err(|error| match error {
            MessageError::NotFound => (StatusCode::NOT_FOUND, "proposal not found".to_string()),
            MessageError::WrongPhase => (
                StatusCode::CONFLICT,
                "the conversation is closed for finalized proposals".to_string(),
            ),
            MessageError::Database(error) => internal_error("add_message")(error),
        })?;

    if let Err((status, error)) = ensure_webgemeindezentrum_activity_faden(
        &state,
        &auth,
        &proposal.webgemeindezentrum_id,
        super::edges::FadenType::Conversation,
        &id,
    )
    .await
    {
        tracing::error!(
            event = "governance.message_faden_projection.failed",
            proposal_id = %id,
            message_id = %message.id,
            center_id = %proposal.webgemeindezentrum_id,
            %status,
            %error,
            "Proposal message remains durable; its derived center Faden is missing"
        );
    }

    Ok((StatusCode::CREATED, Json(message)))
}

/// Führt den bereits frisch bestätigten Gast-Austritt aus. Der Aufrufer muss
/// die Step-up-Autorisierung vorher konsumiert haben. Die kanonische Löschung
/// bleibt transaktional; ein nachgelagerter Session-Cleanup-Fehler macht eine
/// bereits committete Löschung nicht rückwirkend zu einem Fehler.
pub(crate) async fn execute_guest_exit(state: &ApiState, account_id: &str) -> Result<(), ApiError> {
    let pool = require_guest_exit_pool(state)?;

    governance::delete_guest_account(pool, account_id)
        .await
        .map_err(|error| match error {
            GuestExitError::NotEligible => (
                StatusCode::CONFLICT,
                "account is no longer an active guest".to_string(),
            ),
            GuestExitError::AmbiguousLegacyEndpoint => (
                StatusCode::CONFLICT,
                "guest exit is blocked by an ambiguous legacy relationship".to_string(),
            ),
            GuestExitError::Database(error) => internal_error("delete_guest_account")(error),
        })?;

    let session_cleanup = state.sessions.delete_all_by_account(account_id).await;
    record_guest_exit_session_cleanup(account_id, session_cleanup);
    // In PostgreSQL mode the projection middleware holds a read guard for the
    // whole request. Refreshing here would try to upgrade that guard to a write
    // lock and deadlock against ourselves. The next domain request observes the
    // incremented database generation and refreshes before taking its read guard.

    tracing::info!(
        event = "governance.guest.exited",
        account_id,
        "Guest account deleted after step-up confirmation"
    );

    Ok(())
}

/// POST /accounts/me/exit — startet den irreversiblen Austritt eines Gasts.
/// Eine normale Sitzung darf die Löschung nicht mehr selbst ausführen: Sie
/// erzeugt nur einen kurzlebigen, an Account + Gerät + Lösch-Intent gebundenen
/// Step-up-Challenge. Erst dessen einmaliger Verbrauch führt die Löschung aus.
pub async fn exit_own_account(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
) -> Result<Response, ApiError> {
    let account_id = require_account_id(&auth)?;
    if auth.role != Role::Gast {
        return Err((
            StatusCode::CONFLICT,
            "only guest accounts can use the guest exit path".to_string(),
        ));
    }

    // Preserve fail-closed governance semantics before issuing any security token.
    require_guest_exit_pool(&state)?;

    // `ExitGuestAccount` is persisted in the shared step-up challenge payload.
    // During the first rollout, older API replicas cannot deserialize that new
    // enum variant. Keep challenge creation disabled until every live API replica
    // runs a compatibility-aware binary; activation is a separate rollout step.
    if !guest_exit_step_up_enabled() {
        tracing::warn!(
            event = "governance.guest.exit_step_up_rollout_gated",
            account_id = %account_id,
            env_var = GUEST_EXIT_STEP_UP_ENABLED_ENV,
            "Guest exit is temporarily unavailable until the step-up rollout gate is activated"
        );
        return Err((
            StatusCode::SERVICE_UNAVAILABLE,
            "guest exit is temporarily unavailable while step-up confirmation is being activated"
                .to_string(),
        ));
    }

    let device_id = auth.device_id.clone().ok_or((
        StatusCode::INTERNAL_SERVER_ERROR,
        "authenticated context missing device_id".to_string(),
    ))?;

    let challenge = match super::auth::create_shared_challenge(
        &state,
        account_id.clone(),
        device_id,
        ChallengeIntent::ExitGuestAccount,
    )
    .await
    {
        Ok(challenge) => challenge,
        Err(response) => return Ok(response),
    };

    tracing::info!(
        event = "governance.guest.exit_step_up_required",
        account_id = %account_id,
        challenge_id = %challenge.id,
        "Guest account exit requires fresh step-up confirmation"
    );

    Ok((
        StatusCode::FORBIDDEN,
        Json(serde_json::json!({
            "error": "STEP_UP_REQUIRED",
            "challenge_id": challenge.id
        })),
    )
        .into_response())
}

#[cfg(test)]
mod tests {
    use super::{
        parse_guest_exit_step_up_enabled, private_no_store_json, proposal_list_view,
        record_guest_exit_session_cleanup,
    };
    use crate::{
        auth::{role::Role, session::SessionBackendError},
        governance::{ProposalListEntry, ProposalStatus, ProposalWithCounts},
        middleware::auth::AuthContext,
    };
    use axum::http::header;
    use chrono::{Duration, Utc};

    fn proposal_entry(
        status: ProposalStatus,
        applicant_account_id: &str,
        own_vote: Option<&str>,
        own_veto: bool,
    ) -> ProposalListEntry {
        let now = Utc::now();
        ProposalListEntry {
            proposal: ProposalWithCounts {
                id: "proposal-1".to_string(),
                kind: "sachantrag".to_string(),
                webgemeindezentrum_id: "wgz-1".to_string(),
                title: Some("Testantrag".to_string()),
                target_node_id: None,
                target_node_title: None,
                repeals_proposal_id: None,
                pending_repeal_proposal_id: None,
                repealed_by_proposal_id: None,
                repealed_at: None,
                applicant_account_id: Some(applicant_account_id.to_string()),
                applicant_title: "Antragsteller".to_string(),
                summary: None,
                status,
                created_at: now - Duration::hours(1),
                consent_until: now + Duration::hours(1),
                voting_until: Some(now + Duration::hours(1)),
                finalized_at: None,
                last_activity_at: now - Duration::minutes(15),
                veto_count: 0,
                message_count: 0,
                yes_votes: 0,
                no_votes: 0,
                abstain_votes: 0,
            },
            own_vote: own_vote.map(str::to_string),
            own_veto,
        }
    }

    fn auth(account_id: &str, role: Role) -> AuthContext {
        AuthContext {
            authenticated: true,
            account_id: Some(account_id.to_string()),
            device_id: None,
            role,
            expires_at: None,
        }
    }

    #[test]
    fn proposal_list_actionability_is_viewer_and_phase_bound() {
        let now = Utc::now();
        let weber = auth("viewer", Role::Weber);

        let consent = proposal_list_view(
            proposal_entry(ProposalStatus::Consent, "other", None, false),
            now,
            &weber,
        );
        let consent_viewer = consent.viewer_participation.expect("authenticated viewer");
        assert!(consent_viewer.may_veto);
        assert!(!consent_viewer.may_vote);
        assert_eq!(consent_viewer.vote_choice, None);
        assert!(!consent_viewer.has_veto);
        assert!(consent.can_veto);
        assert!(!consent.can_vote);

        let already_vetoed = proposal_list_view(
            proposal_entry(ProposalStatus::Consent, "other", None, true),
            now,
            &weber,
        );
        let veto_viewer = already_vetoed
            .viewer_participation
            .expect("authenticated viewer");
        assert!(!veto_viewer.may_veto);
        assert!(veto_viewer.has_veto);

        let voting = proposal_list_view(
            proposal_entry(ProposalStatus::Voting, "other", Some("ja"), false),
            now,
            &weber,
        );
        let voting_viewer = voting.viewer_participation.expect("authenticated viewer");
        assert!(
            voting_viewer.may_vote,
            "a cast vote remains changeable while voting is open"
        );
        assert_eq!(voting_viewer.vote_choice.as_deref(), Some("ja"));
        assert_eq!(voting.own_vote.as_deref(), Some("ja"));
        assert!(voting.can_vote);

        let own = proposal_list_view(
            proposal_entry(ProposalStatus::Voting, "viewer", None, false),
            now,
            &weber,
        );
        let own_viewer = own.viewer_participation.expect("authenticated viewer");
        assert!(!own_viewer.may_vote);
        assert!(!own_viewer.may_veto);

        let guest = auth("guest", Role::Gast);
        let foreign_for_guest = proposal_list_view(
            proposal_entry(ProposalStatus::Consent, "other", None, false),
            now,
            &guest,
        );
        let guest_viewer = foreign_for_guest
            .viewer_participation
            .expect("authenticated guest");
        assert!(!guest_viewer.may_vote);
        assert!(!guest_viewer.may_veto);

        let anonymous = AuthContext {
            authenticated: false,
            account_id: None,
            device_id: None,
            role: Role::Gast,
            expires_at: None,
        };
        let public_view = proposal_list_view(
            proposal_entry(ProposalStatus::Consent, "other", None, false),
            now,
            &anonymous,
        );
        assert!(public_view.viewer_participation.is_none());
        assert!(public_view.own_vote.is_none());
        assert!(!public_view.own_veto);
        assert!(!public_view.can_vote);
        assert!(!public_view.can_veto);
    }

    #[test]
    fn personalized_governance_json_is_never_cacheable() {
        let response = private_no_store_json(serde_json::json!({ "ok": true }));
        assert_eq!(
            response
                .headers()
                .get(header::CACHE_CONTROL)
                .and_then(|value| value.to_str().ok()),
            Some("private, no-store")
        );
    }

    #[test]
    fn guest_exit_step_up_rollout_flag_is_fail_closed() {
        assert!(!parse_guest_exit_step_up_enabled(None));
        assert!(!parse_guest_exit_step_up_enabled(Some("")));
        assert!(!parse_guest_exit_step_up_enabled(Some("0")));
        assert!(!parse_guest_exit_step_up_enabled(Some("false")));
        assert!(!parse_guest_exit_step_up_enabled(Some("unexpected")));
        assert!(parse_guest_exit_step_up_enabled(Some("1")));
        assert!(parse_guest_exit_step_up_enabled(Some(" TRUE ")));
    }

    #[test]
    fn failed_secondary_session_cleanup_does_not_fail_committed_guest_exit() {
        record_guest_exit_session_cleanup(
            "guest-already-deleted",
            Err(SessionBackendError::Unavailable),
        );
    }
}
