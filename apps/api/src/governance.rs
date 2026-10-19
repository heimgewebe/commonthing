//! Governance: allgemeines Antragssystem (vertikaler Schnitt: Weberantrag).
//!
//! PostgreSQL ist die kanonische Wahrheit für Anträge, Vetos, Stimmen und
//! Gesprächsraum-Beiträge (siehe `docs/specs/governance-antraege.md`). Ohne
//! konfigurierten Pool arbeiten die Governance-Endpunkte fail-closed; es gibt
//! keinen JSONL-Fallback für diese Tabellen.
//!
//! Fristen werden serverseitig und idempotent ausgewertet:
//! [`finalize_due_proposals`] läuft sowohl als Hintergrund-Sweeper als auch
//! lazy vor jedem Governance-Read. Jede Finalisierung ist eine einzelne
//! Datenbanktransaktion mit `SELECT ... FOR UPDATE`-Recheck, damit weder ein
//! Neustart noch konkurrierende Auswertungen doppelte oder halbe Übergänge
//! erzeugen können.
//!
//! Die sqlx-Konfiguration dieses Crates hat kein `uuid`-Feature; `UUID`-Spalten
//! werden — wie in `domain_db` und `passkeys_db` — als Text mit expliziten
//! `$n::uuid`-Casts gebunden und als `id::text` gelesen.

use std::sync::Arc;

use chrono::{DateTime, Duration, Utc};
use serde::{Deserialize, Serialize};
use sqlx::{PgPool, Postgres, Row, Transaction};
use tokio::sync::RwLock;
use uuid::Uuid;

use crate::advisory_lock::{account_lifecycle_lock_key, node_mutation_lock_key};
use crate::auth::accounts::AccountStore;
use crate::auth::role::Role;

/// Länge der offenen Konsentphase eines Weberantrags.
pub const CONSENT_PHASE_DAYS: i64 = 7;
/// Länge der Beratungs- und Abstimmungsphase nach mindestens einem Veto.
pub const VOTING_PHASE_DAYS: i64 = 7;

/// Maximale Länge der Antragsbegründung (Spiegel des DB-Checks).
pub const SUMMARY_MAX_CHARS: usize = 2000;
/// Maximale Länge des Titels eines Sachantrags (Spiegel des DB-Checks).
pub const PROPOSAL_TITLE_MAX_CHARS: usize = 200;
/// Maximale Länge einer Veto-Begründung (Spiegel des DB-Checks).
pub const VETO_REASON_MAX_CHARS: usize = 2000;
/// Maximale Länge eines Gesprächsraum-Beitrags (Spiegel des DB-Checks).
pub const MESSAGE_BODY_MAX_CHARS: usize = 4000;

/// Lebenszyklus eines Antrags. Persistiert als Kleinbuchstaben-Text.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ProposalStatus {
    /// Offene Konsentphase bis `consent_until`.
    Consent,
    /// Beratungs- und Abstimmungsphase bis `voting_until` (nach Veto).
    Voting,
    /// Final angenommen; bei Weberanträgen ist die Aufnahme vollzogen.
    Accepted,
    /// Final abgelehnt (auch Gleichstand und 0:0).
    Rejected,
    /// Vom Antragsteller vor dem Ende der laufenden Phase zurückgezogen.
    Withdrawn,
}

impl ProposalStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Consent => "consent",
            Self::Voting => "voting",
            Self::Accepted => "accepted",
            Self::Rejected => "rejected",
            Self::Withdrawn => "withdrawn",
        }
    }

    fn from_db(value: &str) -> Result<Self, sqlx::Error> {
        match value {
            "consent" => Ok(Self::Consent),
            "voting" => Ok(Self::Voting),
            "accepted" => Ok(Self::Accepted),
            "rejected" => Ok(Self::Rejected),
            "withdrawn" => Ok(Self::Withdrawn),
            other => Err(sqlx::Error::Decode(
                format!("unknown governance proposal status: {other}").into(),
            )),
        }
    }
}

/// Genau eine aktuelle, änderbare Stimme je Weber und Antrag.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum VoteChoice {
    Ja,
    Nein,
    Enthaltung,
}

impl VoteChoice {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Ja => "ja",
            Self::Nein => "nein",
            Self::Enthaltung => "enthaltung",
        }
    }
}

/// Ob eine Stimmabgabe tatsächlich neue Beteiligung darstellt.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum VoteWriteOutcome {
    Created,
    Changed,
    /// Identische Wiederholung, etwa nach einer verlorenen HTTP-Antwort.
    Unchanged,
}

/// Antrag inklusive öffentlicher Zählstände (Vetos und Stimmen).
#[derive(Clone, Debug)]
pub struct ProposalWithCounts {
    pub id: String,
    pub kind: String,
    pub webgemeindezentrum_id: String,
    pub title: Option<String>,
    pub target_node_id: Option<String>,
    pub target_node_title: Option<String>,
    /// Nur bei einem Aufhebungsantrag: der historisch angenommene Sachantrag.
    pub repeals_proposal_id: Option<String>,
    /// Offenes Aufhebungsverfahren, das diesen Beschluss adressiert.
    pub pending_repeal_proposal_id: Option<String>,
    /// Angenommener Aufhebungsantrag, durch den dieser Beschluss aufgehoben ist.
    pub repealed_by_proposal_id: Option<String>,
    pub repealed_at: Option<DateTime<Utc>>,
    pub applicant_account_id: Option<String>,
    pub applicant_title: String,
    pub summary: Option<String>,
    pub status: ProposalStatus,
    pub created_at: DateTime<Utc>,
    pub consent_until: DateTime<Utc>,
    pub voting_until: Option<DateTime<Utc>>,
    pub finalized_at: Option<DateTime<Utc>>,
    /// Jüngster kanonischer öffentlicher Aktivitätszeitpunkt des Verfahrens.
    /// Zukünftige Fristen zählen nicht; ein Fristzeitpunkt wird erst nach dem
    /// tatsächlichen Phasenwechsel zum vergangenen Fachereignis.
    pub last_activity_at: DateTime<Utc>,
    pub veto_count: i64,
    pub message_count: i64,
    pub yes_votes: i64,
    pub no_votes: i64,
    pub abstain_votes: i64,
}

/// Ein Listeneintrag mit ausschließlich der Sicht des aktuellen Betrachters.
/// Die persönlichen Felder werden in derselben SQL-Abfrage wie die öffentliche
/// Liste gelesen, damit Attention keinen N+1-Detailpfad benötigt.
#[derive(Clone, Debug)]
pub struct ProposalListEntry {
    pub proposal: ProposalWithCounts,
    pub own_vote: Option<String>,
    pub own_veto: bool,
}

/// Begründetes Veto eines Webers oder Administrators (öffentlich sichtbare Webungsaktion).
#[derive(Clone, Debug, Serialize)]
pub struct Veto {
    pub weber_account_id: String,
    pub weber_title: String,
    pub reason: String,
    pub created_at: DateTime<Utc>,
}

/// Beitrag im Gesprächsraum eines Antrags.
#[derive(Clone, Debug, Serialize)]
pub struct ProposalMessage {
    pub id: String,
    pub author_account_id: Option<String>,
    pub author_title: String,
    pub body: String,
    pub created_at: DateTime<Utc>,
}

/// Ergebnis einer Finalisierung durch [`finalize_due_proposals`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FinalizationOutcome {
    pub proposal_id: String,
    pub applicant_account_id: String,
    pub status: ProposalStatus,
    /// `true` genau dann, wenn diese Transaktion die bestehende Gastidentität
    /// auf die Berechtigungsrolle `weber` angehoben hat.
    pub promoted: bool,
}

#[derive(Debug, thiserror::Error)]
pub enum CreateProposalError {
    /// Der Account hat bereits einen offenen Antrag dieser Art (Doppelantrag).
    #[error("an open proposal of this kind already exists for this account")]
    AlreadyOpen,
    /// Nur eine aktive Gastidentität darf den Weberstatus beantragen.
    #[error("only an active guest account may create a Weber proposal")]
    NotGuest,
    #[error("only an active Weber or administrator may create a Sachantrag")]
    NotSachApplicant,
    #[error("no unique active Webgemeindezentrum is available for this proposal")]
    CenterUnavailable,
    #[error("the target node does not exist")]
    TargetNodeNotFound,
    #[error("failed to persist proposal: {0}")]
    Database(#[source] sqlx::Error),
}

#[derive(Debug, thiserror::Error)]
pub enum WithdrawProposalError {
    #[error("proposal not found")]
    NotFound,
    #[error("only the applicant may withdraw this proposal")]
    NotApplicant,
    #[error("proposal is no longer open for withdrawal")]
    WrongPhase,
    #[error("proposal applicant account is missing or disabled")]
    ActorUnavailable,
    #[error("failed to withdraw proposal: {0}")]
    Database(#[source] sqlx::Error),
}

#[derive(Debug, thiserror::Error)]
pub enum RepealProposalError {
    #[error("proposal not found")]
    NotFound,
    #[error("only accepted Sachantraege may be repealed")]
    TargetNotSachProposal,
    #[error("only an accepted decision may be repealed")]
    TargetNotAccepted,
    #[error("a repeal proposal cannot itself be repealed")]
    TargetIsRepeal,
    #[error("an open or accepted repeal proposal already exists")]
    AlreadyHasRepeal,
    #[error("repeal proposals require Weber or administrator status")]
    ActorNotEligible,
    #[error("repeal applicant account is missing or disabled")]
    ActorUnavailable,
    #[error("failed to persist repeal proposal: {0}")]
    Database(#[source] sqlx::Error),
}

#[derive(Debug, thiserror::Error)]
pub enum VetoError {
    #[error("proposal not found")]
    NotFound,
    /// Veto ist nur während der offenen Konsentphase zulässig.
    #[error("proposal is not in an open consent phase")]
    WrongPhase,
    /// Je Account höchstens ein Veto pro Antrag.
    #[error("this account already vetoed the proposal")]
    AlreadyVetoed,
    #[error("the applicant cannot veto the own proposal")]
    ApplicantCannotDecide,
    #[error("veto actor account is not a Weber or administrator")]
    ActorNotEligible,
    #[error("veto actor account is missing or disabled")]
    ActorUnavailable,
    #[error("failed to persist veto: {0}")]
    Database(#[source] sqlx::Error),
}

#[derive(Debug, thiserror::Error)]
pub enum VoteError {
    #[error("proposal not found")]
    NotFound,
    /// Stimmen sind nur während der Beratungs- und Abstimmungsphase zulässig.
    #[error("proposal is not in an open voting phase")]
    WrongPhase,
    #[error("the applicant cannot vote on the own proposal")]
    ApplicantCannotDecide,
    #[error("vote actor account is not a Weber or administrator")]
    ActorNotEligible,
    #[error("vote actor account is missing or disabled")]
    ActorUnavailable,
    #[error("failed to persist vote: {0}")]
    Database(#[source] sqlx::Error),
}

#[derive(Debug, thiserror::Error)]
pub enum GuestExitError {
    #[error("account is no longer an active guest")]
    NotEligible,
    #[error("guest exit cannot classify an untyped legacy edge endpoint because a node uses the same id")]
    AmbiguousLegacyEndpoint,
    #[error("failed to delete guest account: {0}")]
    Database(#[from] sqlx::Error),
}

#[derive(Debug, thiserror::Error)]
pub enum MessageError {
    #[error("proposal not found")]
    NotFound,
    /// Beiträge sind nur zulässig, solange der Antrag offen ist.
    #[error("proposal is no longer open for conversation")]
    WrongPhase,
    #[error("failed to persist message: {0}")]
    Database(#[source] sqlx::Error),
}

// ---------------------------------------------------------------------------
// Reine Entscheidungslogik (ohne Datenbank testbar)
// ---------------------------------------------------------------------------

/// Ergebnis der Konsentphase: ohne Veto wird nach Ablauf angenommen, mit
/// mindestens einem Veto beginnt die Beratungs- und Abstimmungsphase.
pub fn consent_phase_outcome(veto_count: i64) -> ProposalStatus {
    if veto_count == 0 {
        ProposalStatus::Accepted
    } else {
        ProposalStatus::Voting
    }
}

/// Ergebnis der Abstimmungsphase: kein Quorum; angenommen genau dann, wenn
/// Ja-Stimmen größer als Nein-Stimmen sind. Gleichstand und 0:0 sind
/// abgelehnt. Enthaltungen gehen nicht in die Zählung ein.
pub fn voting_phase_outcome(yes_votes: i64, no_votes: i64) -> ProposalStatus {
    if yes_votes > no_votes {
        ProposalStatus::Accepted
    } else {
        ProposalStatus::Rejected
    }
}

// ---------------------------------------------------------------------------
// Schreibpfade
// ---------------------------------------------------------------------------

const ONE_OPEN_PROPOSAL_INDEX: &str = "governance_proposals_one_open_per_applicant";

fn is_unique_violation(error: &sqlx::Error, constraint: &str) -> bool {
    matches!(
        error.as_database_error().and_then(|db| db.constraint()),
        Some(name) if name == constraint
    )
}

/// Resolve and lock the active governance center used by every proposal kind.
/// Without an explicit center, exactly one active center must exist; multiple
/// active centers fail closed until nodes have a canonical center assignment.
async fn resolve_active_center(
    tx: &mut Transaction<'_, Postgres>,
    requested_center_id: Option<&str>,
) -> Result<Option<String>, sqlx::Error> {
    if let Some(requested_center_id) = requested_center_id {
        return sqlx::query_scalar(
            "SELECT c.id \
             FROM ortswebereien o \
             JOIN gewebezellen g ON g.id = o.gewebezelle_id \
             JOIN webgemeindezentren c \
               ON c.id = o.active_webgemeindezentrum_id \
              AND c.ortsweberei_id = o.id \
             WHERE o.lifecycle_state = 'active' \
               AND g.lifecycle_state = 'active' \
               AND c.id = $1 \
             FOR SHARE OF o, g, c",
        )
        .bind(requested_center_id)
        .fetch_optional(&mut **tx)
        .await;
    }

    let center_ids: Vec<String> = sqlx::query_scalar(
        "SELECT c.id \
         FROM ortswebereien o \
         JOIN gewebezellen g ON g.id = o.gewebezelle_id \
         JOIN webgemeindezentren c \
           ON c.id = o.active_webgemeindezentrum_id \
          AND c.ortsweberei_id = o.id \
         WHERE o.lifecycle_state = 'active' AND g.lifecycle_state = 'active' \
         ORDER BY c.id FOR SHARE OF o, g, c",
    )
    .fetch_all(&mut **tx)
    .await?;
    Ok((center_ids.len() == 1).then(|| center_ids[0].clone()))
}

/// Lege einen neuen Weberantrag an. Die Konsentphase beginnt mit `now` und
/// endet nach [`CONSENT_PHASE_DAYS`]. Ein zweiter offener Antrag desselben
/// Accounts wird über den partiellen Unique-Index abgewiesen.
pub async fn create_weber_proposal(
    pool: &PgPool,
    applicant_account_id: &str,
    applicant_title: &str,
    summary: Option<&str>,
    now: DateTime<Utc>,
) -> Result<ProposalWithCounts, CreateProposalError> {
    create_weber_proposal_at_center(
        pool,
        applicant_account_id,
        applicant_title,
        summary,
        None,
        now,
    )
    .await
}

/// Variante für eine Governance-Oberfläche, die bereits an ein konkretes
/// Webgemeindezentrum gebunden ist. Der angegebene Mittelpunkt muss der aktive
/// Mittelpunkt einer aktiven Ortsweberei und Gewebezelle sein.
pub async fn create_weber_proposal_at_center(
    pool: &PgPool,
    applicant_account_id: &str,
    applicant_title: &str,
    summary: Option<&str>,
    requested_center_id: Option<&str>,
    now: DateTime<Utc>,
) -> Result<ProposalWithCounts, CreateProposalError> {
    let id = Uuid::new_v4().to_string();
    let consent_until = now + Duration::days(CONSENT_PHASE_DAYS);

    let mut tx = pool.begin().await.map_err(CreateProposalError::Database)?;
    let applicant_role: Option<String> = sqlx::query_scalar(
        "SELECT role FROM domain_accounts \
         WHERE id = $1 AND disabled = FALSE FOR UPDATE",
    )
    .bind(applicant_account_id)
    .fetch_optional(&mut *tx)
    .await
    .map_err(CreateProposalError::Database)?;
    if applicant_role.as_deref() != Some("gast") {
        return Err(CreateProposalError::NotGuest);
    }

    let webgemeindezentrum_id = resolve_active_center(&mut tx, requested_center_id)
        .await
        .map_err(CreateProposalError::Database)?
        .ok_or(CreateProposalError::CenterUnavailable)?;

    sqlx::query(
        "INSERT INTO governance_proposals \
             (id, kind, webgemeindezentrum_id, applicant_account_id, applicant_title, summary, status, \
              created_at, consent_until) \
         VALUES ($1::uuid, 'weberantrag', $2, $3, $4, $5, 'consent', $6, $7)",
    )
    .bind(&id)
    .bind(&webgemeindezentrum_id)
    .bind(applicant_account_id)
    .bind(applicant_title)
    .bind(summary)
    .bind(now)
    .bind(consent_until)
    .execute(&mut *tx)
    .await
    .map_err(|error| {
        if is_unique_violation(&error, ONE_OPEN_PROPOSAL_INDEX) {
            CreateProposalError::AlreadyOpen
        } else {
            CreateProposalError::Database(error)
        }
    })?;
    tx.commit().await.map_err(CreateProposalError::Database)?;

    Ok(ProposalWithCounts {
        id,
        kind: "weberantrag".to_string(),
        webgemeindezentrum_id,
        title: None,
        target_node_id: None,
        target_node_title: None,
        repeals_proposal_id: None,
        pending_repeal_proposal_id: None,
        repealed_by_proposal_id: None,
        repealed_at: None,
        applicant_account_id: Some(applicant_account_id.to_string()),
        applicant_title: applicant_title.to_string(),
        summary: summary.map(str::to_string),
        status: ProposalStatus::Consent,
        created_at: now,
        consent_until,
        voting_until: None,
        finalized_at: None,
        last_activity_at: now,
        veto_count: 0,
        message_count: 0,
        yes_votes: 0,
        no_votes: 0,
        abstain_votes: 0,
    })
}

/// Lege einen Sachantrag im selben Governance-Verfahren an. Der Antragsteller
/// und ein optional referenzierter Knoten werden innerhalb derselben
/// Transaktion geprüft und gesperrt. Der Knotentitel bleibt als Snapshot
/// erhalten, wenn der Knoten später regulär aus dem Gewebe entfernt wird.
pub async fn create_sach_proposal(
    pool: &PgPool,
    applicant_account_id: &str,
    applicant_title: &str,
    title: &str,
    summary: Option<&str>,
    target_node_id: Option<&str>,
    now: DateTime<Utc>,
) -> Result<ProposalWithCounts, CreateProposalError> {
    create_sach_proposal_at_center(
        pool,
        applicant_account_id,
        applicant_title,
        title,
        summary,
        None,
        target_node_id,
        now,
    )
    .await
}

/// Center-gebundene Variante eines Sachantrags. Ein fehlender Center wird nur
/// bei genau einem aktiven Center aufgelöst; Mehrdeutigkeit bleibt fail-closed.
#[allow(clippy::too_many_arguments)]
pub async fn create_sach_proposal_at_center(
    pool: &PgPool,
    applicant_account_id: &str,
    applicant_title: &str,
    title: &str,
    summary: Option<&str>,
    requested_center_id: Option<&str>,
    target_node_id: Option<&str>,
    now: DateTime<Utc>,
) -> Result<ProposalWithCounts, CreateProposalError> {
    let id = Uuid::new_v4().to_string();
    let consent_until = now + Duration::days(CONSENT_PHASE_DAYS);
    let mut tx = pool.begin().await.map_err(CreateProposalError::Database)?;

    let applicant_role: Option<String> = sqlx::query_scalar(
        "SELECT role FROM domain_accounts \
         WHERE id = $1 AND disabled = FALSE FOR UPDATE",
    )
    .bind(applicant_account_id)
    .fetch_optional(&mut *tx)
    .await
    .map_err(CreateProposalError::Database)?;
    if !matches!(applicant_role.as_deref(), Some("weber" | "admin")) {
        return Err(CreateProposalError::NotSachApplicant);
    }

    let webgemeindezentrum_id = resolve_active_center(&mut tx, requested_center_id)
        .await
        .map_err(CreateProposalError::Database)?
        .ok_or(CreateProposalError::CenterUnavailable)?;

    let target_node_title = if let Some(target_node_id) = target_node_id {
        sqlx::query("SELECT pg_advisory_xact_lock($1::bigint)")
            .bind(node_mutation_lock_key(target_node_id))
            .execute(&mut *tx)
            .await
            .map_err(CreateProposalError::Database)?;
        Some(
            // This is a PUBLIC Sachantrag: even a private-node owner may
            // not copy its title/id into the world-readable proposal snapshot.
            // FOR SHARE keeps public visibility stable until the proposal commits.
            sqlx::query_scalar(
                "SELECT title FROM domain_nodes WHERE id = $1 AND search_visibility = 'public' FOR SHARE",
            )
                .bind(target_node_id)
                .fetch_optional(&mut *tx)
                .await
                .map_err(CreateProposalError::Database)?
                .ok_or(CreateProposalError::TargetNodeNotFound)?,
        )
    } else {
        None
    };

    sqlx::query(
        "INSERT INTO governance_proposals \
             (id, kind, webgemeindezentrum_id, title, target_node_id, target_node_title, \
              applicant_account_id, applicant_title, summary, status, created_at, consent_until) \
         VALUES ($1::uuid, 'sachantrag', $2, $3, $4, $5, $6, $7, $8, 'consent', $9, $10)",
    )
    .bind(&id)
    .bind(&webgemeindezentrum_id)
    .bind(title)
    .bind(target_node_id)
    .bind(&target_node_title)
    .bind(applicant_account_id)
    .bind(applicant_title)
    .bind(summary)
    .bind(now)
    .bind(consent_until)
    .execute(&mut *tx)
    .await
    .map_err(CreateProposalError::Database)?;
    tx.commit().await.map_err(CreateProposalError::Database)?;

    Ok(ProposalWithCounts {
        id,
        kind: "sachantrag".to_string(),
        webgemeindezentrum_id,
        title: Some(title.to_string()),
        target_node_id: target_node_id.map(str::to_string),
        target_node_title,
        repeals_proposal_id: None,
        pending_repeal_proposal_id: None,
        repealed_by_proposal_id: None,
        repealed_at: None,
        applicant_account_id: Some(applicant_account_id.to_string()),
        applicant_title: applicant_title.to_string(),
        summary: summary.map(str::to_string),
        status: ProposalStatus::Consent,
        created_at: now,
        consent_until,
        voting_until: None,
        finalized_at: None,
        last_activity_at: now,
        veto_count: 0,
        message_count: 0,
        yes_votes: 0,
        no_votes: 0,
        abstain_votes: 0,
    })
}

fn repeal_title(target_title: Option<&str>) -> String {
    let prefix = "Aufhebung: ";
    let fallback = "Sachbeschluss";
    let source = target_title.unwrap_or(fallback).trim();
    let available = PROPOSAL_TITLE_MAX_CHARS.saturating_sub(prefix.chars().count());
    let shortened: String = source.chars().take(available).collect();
    format!("{prefix}{shortened}")
}

/// Ziehe den eigenen noch offenen Antrag zurück, ohne seine Verfahrensspur zu
/// löschen. Die laufende Phase muss auch zeitlich noch offen sein: eine bereits
/// abgelaufene Entscheidung kann nicht durch einen verspäteten Rücknahmeklick
/// überholt werden, nur weil der Sweeper sie noch nicht finalisiert hat.
pub async fn withdraw_proposal(
    pool: &PgPool,
    proposal_id: &str,
    applicant_account_id: &str,
    now: DateTime<Utc>,
) -> Result<ProposalWithCounts, WithdrawProposalError> {
    let mut tx = pool
        .begin()
        .await
        .map_err(WithdrawProposalError::Database)?;

    let active_actor: Option<String> = sqlx::query_scalar(
        "SELECT id FROM domain_accounts WHERE id = $1 AND disabled = FALSE FOR UPDATE",
    )
    .bind(applicant_account_id)
    .fetch_optional(&mut *tx)
    .await
    .map_err(WithdrawProposalError::Database)?;
    if active_actor.is_none() {
        return Err(WithdrawProposalError::ActorUnavailable);
    }

    let (status, locked_applicant, consent_until, voting_until) =
        lock_proposal_phase(&mut tx, proposal_id)
            .await
            .map_err(WithdrawProposalError::Database)?
            .ok_or(WithdrawProposalError::NotFound)?;
    if locked_applicant.as_deref() != Some(applicant_account_id) {
        return Err(WithdrawProposalError::NotApplicant);
    }
    let still_open = match status {
        ProposalStatus::Consent => now < consent_until,
        ProposalStatus::Voting => voting_until.is_some_and(|until| now < until),
        ProposalStatus::Accepted | ProposalStatus::Rejected | ProposalStatus::Withdrawn => false,
    };
    if !still_open {
        return Err(WithdrawProposalError::WrongPhase);
    }

    sqlx::query(
        "UPDATE governance_proposals SET status = 'withdrawn', finalized_at = $2 \
         WHERE id = $1::uuid",
    )
    .bind(proposal_id)
    .bind(now)
    .execute(&mut *tx)
    .await
    .map_err(WithdrawProposalError::Database)?;
    tx.commit().await.map_err(WithdrawProposalError::Database)?;

    get_proposal(pool, proposal_id)
        .await
        .map_err(WithdrawProposalError::Database)?
        .ok_or_else(|| WithdrawProposalError::Database(sqlx::Error::RowNotFound))
}

/// Eröffne ein reguläres Sachantragsverfahren zur Aufhebung eines bereits
/// angenommenen Sachbeschlusses. Der alte Beschluss bleibt unverändert; erst
/// die Annahme dieses neuen Antrags macht die Aufhebung in der Projektion
/// wirksam. Aufhebungsanträge werden nicht rekursiv aufgehoben — eine spätere
/// inhaltliche Kehrtwende ist wieder ein normaler Sachantrag.
pub async fn create_repeal_proposal(
    pool: &PgPool,
    target_proposal_id: &str,
    applicant_account_id: &str,
    applicant_title: &str,
    summary: Option<&str>,
    now: DateTime<Utc>,
) -> Result<ProposalWithCounts, RepealProposalError> {
    let mut tx = pool.begin().await.map_err(RepealProposalError::Database)?;

    // Gleiche Lock-Reihenfolge wie Veto, Stimme, Nachricht und Finalisierung:
    // zuerst Account, dann Antrag. Das hält Account-Lifecycle und Governance
    // auch unter Parallelität deadlock-frei.
    let actor_role: Option<String> = sqlx::query_scalar(
        "SELECT role FROM domain_accounts WHERE id = $1 AND disabled = FALSE FOR UPDATE",
    )
    .bind(applicant_account_id)
    .fetch_optional(&mut *tx)
    .await
    .map_err(RepealProposalError::Database)?;
    match actor_role.as_deref() {
        None => return Err(RepealProposalError::ActorUnavailable),
        Some("weber" | "admin") => {}
        Some(_) => return Err(RepealProposalError::ActorNotEligible),
    }

    let row = sqlx::query(
        "SELECT kind, status, webgemeindezentrum_id, title, target_node_id, \
                target_node_title, repeals_proposal_id::text AS repeals_proposal_id \
         FROM governance_proposals WHERE id = $1::uuid FOR UPDATE",
    )
    .bind(target_proposal_id)
    .fetch_optional(&mut *tx)
    .await
    .map_err(RepealProposalError::Database)?
    .ok_or(RepealProposalError::NotFound)?;

    let kind: String = row.try_get("kind").map_err(RepealProposalError::Database)?;
    if kind != "sachantrag" {
        return Err(RepealProposalError::TargetNotSachProposal);
    }
    let status = ProposalStatus::from_db(
        row.try_get::<String, _>("status")
            .map_err(RepealProposalError::Database)?
            .as_str(),
    )
    .map_err(RepealProposalError::Database)?;
    if status != ProposalStatus::Accepted {
        return Err(RepealProposalError::TargetNotAccepted);
    }
    let target_repeals: Option<String> = row
        .try_get("repeals_proposal_id")
        .map_err(RepealProposalError::Database)?;
    if target_repeals.is_some() {
        return Err(RepealProposalError::TargetIsRepeal);
    }

    let existing: Option<String> = sqlx::query_scalar(
        "SELECT id::text FROM governance_proposals \
         WHERE repeals_proposal_id = $1::uuid \
           AND status IN ('consent', 'voting', 'accepted') LIMIT 1",
    )
    .bind(target_proposal_id)
    .fetch_optional(&mut *tx)
    .await
    .map_err(RepealProposalError::Database)?;
    if existing.is_some() {
        return Err(RepealProposalError::AlreadyHasRepeal);
    }

    let webgemeindezentrum_id: String = row
        .try_get("webgemeindezentrum_id")
        .map_err(RepealProposalError::Database)?;
    let target_title: Option<String> = row
        .try_get("title")
        .map_err(RepealProposalError::Database)?;
    let target_node_id: Option<String> = row
        .try_get("target_node_id")
        .map_err(RepealProposalError::Database)?;
    let target_node_title: Option<String> = row
        .try_get("target_node_title")
        .map_err(RepealProposalError::Database)?;
    let title = repeal_title(target_title.as_deref());
    let id = Uuid::new_v4().to_string();
    let consent_until = now + Duration::days(CONSENT_PHASE_DAYS);

    sqlx::query(
        "INSERT INTO governance_proposals \
             (id, kind, webgemeindezentrum_id, title, target_node_id, target_node_title, \
              applicant_account_id, applicant_title, summary, status, created_at, consent_until, \
              repeals_proposal_id) \
         VALUES ($1::uuid, 'sachantrag', $2, $3, $4, $5, $6, $7, $8, 'consent', $9, $10, $11::uuid)",
    )
    .bind(&id)
    .bind(&webgemeindezentrum_id)
    .bind(&title)
    .bind(&target_node_id)
    .bind(&target_node_title)
    .bind(applicant_account_id)
    .bind(applicant_title)
    .bind(summary)
    .bind(now)
    .bind(consent_until)
    .bind(target_proposal_id)
    .execute(&mut *tx)
    .await
    .map_err(|error| {
        if is_unique_violation(&error, "governance_proposals_one_active_repeal") {
            RepealProposalError::AlreadyHasRepeal
        } else {
            RepealProposalError::Database(error)
        }
    })?;
    tx.commit().await.map_err(RepealProposalError::Database)?;

    get_proposal(pool, &id)
        .await
        .map_err(RepealProposalError::Database)?
        .ok_or_else(|| RepealProposalError::Database(sqlx::Error::RowNotFound))
}

/// Sperre den Antrag und liefere Phase, Antragsteller und Fristen.
async fn lock_proposal_phase(
    tx: &mut Transaction<'_, Postgres>,
    proposal_id: &str,
) -> Result<
    Option<(
        ProposalStatus,
        Option<String>,
        DateTime<Utc>,
        Option<DateTime<Utc>>,
    )>,
    sqlx::Error,
> {
    let row = sqlx::query(
        "SELECT status, applicant_account_id, consent_until, voting_until \
         FROM governance_proposals WHERE id = $1::uuid FOR UPDATE",
    )
    .bind(proposal_id)
    .fetch_optional(&mut **tx)
    .await?;

    row.map(|row| {
        let status = ProposalStatus::from_db(row.try_get::<String, _>("status")?.as_str())?;
        Ok((
            status,
            row.try_get("applicant_account_id")?,
            row.try_get("consent_until")?,
            row.try_get("voting_until")?,
        ))
    })
    .transpose()
}

/// Lege ein begründetes Veto ein. Zulässig nur während der offenen
/// Konsentphase (`status = 'consent'` und `now < consent_until`).
pub async fn add_veto(
    pool: &PgPool,
    proposal_id: &str,
    weber_account_id: &str,
    weber_title: &str,
    reason: &str,
    now: DateTime<Utc>,
) -> Result<Veto, VetoError> {
    let mut tx = pool.begin().await.map_err(VetoError::Database)?;

    // Guest exit uses account -> proposal lock ordering. Formal actions use the
    // same order so an exit cannot leave a fresh live actor binding behind.
    let actor_role: Option<String> = sqlx::query_scalar(
        "SELECT role FROM domain_accounts WHERE id = $1 AND disabled = FALSE FOR UPDATE",
    )
    .bind(weber_account_id)
    .fetch_optional(&mut *tx)
    .await
    .map_err(VetoError::Database)?;
    match actor_role.as_deref() {
        None => return Err(VetoError::ActorUnavailable),
        Some("weber" | "admin") => {}
        Some(_) => return Err(VetoError::ActorNotEligible),
    }

    let (status, applicant_account_id, consent_until, _) =
        lock_proposal_phase(&mut tx, proposal_id)
            .await
            .map_err(VetoError::Database)?
            .ok_or(VetoError::NotFound)?;
    if applicant_account_id.as_deref() == Some(weber_account_id) {
        return Err(VetoError::ApplicantCannotDecide);
    }
    if status != ProposalStatus::Consent || now >= consent_until {
        return Err(VetoError::WrongPhase);
    }

    sqlx::query(
        "INSERT INTO governance_vetoes \
             (proposal_id, weber_account_id, weber_title, reason, created_at) \
         VALUES ($1::uuid, $2, $3, $4, $5)",
    )
    .bind(proposal_id)
    .bind(weber_account_id)
    .bind(weber_title)
    .bind(reason)
    .bind(now)
    .execute(&mut *tx)
    .await
    .map_err(|error| {
        if is_unique_violation(&error, "governance_vetoes_pkey") {
            VetoError::AlreadyVetoed
        } else {
            VetoError::Database(error)
        }
    })?;

    tx.commit().await.map_err(VetoError::Database)?;

    Ok(Veto {
        weber_account_id: weber_account_id.to_string(),
        weber_title: weber_title.to_string(),
        reason: reason.to_string(),
        created_at: now,
    })
}

/// Setze oder ändere die aktuelle Stimme eines Webers. Zulässig nur während
/// der Beratungs- und Abstimmungsphase (`status = 'voting'`, vor Fristablauf).
pub async fn upsert_vote(
    pool: &PgPool,
    proposal_id: &str,
    voter_account_id: &str,
    choice: VoteChoice,
    now: DateTime<Utc>,
) -> Result<VoteWriteOutcome, VoteError> {
    let mut tx = pool.begin().await.map_err(VoteError::Database)?;

    let actor_role: Option<String> = sqlx::query_scalar(
        "SELECT role FROM domain_accounts WHERE id = $1 AND disabled = FALSE FOR UPDATE",
    )
    .bind(voter_account_id)
    .fetch_optional(&mut *tx)
    .await
    .map_err(VoteError::Database)?;
    match actor_role.as_deref() {
        None => return Err(VoteError::ActorUnavailable),
        Some("weber" | "admin") => {}
        Some(_) => return Err(VoteError::ActorNotEligible),
    }

    let (status, applicant_account_id, _, voting_until) = lock_proposal_phase(&mut tx, proposal_id)
        .await
        .map_err(VoteError::Database)?
        .ok_or(VoteError::NotFound)?;
    if applicant_account_id.as_deref() == Some(voter_account_id) {
        return Err(VoteError::ApplicantCannotDecide);
    }
    let open_voting = status == ProposalStatus::Voting
        && voting_until.is_some_and(|voting_until| now < voting_until);
    if !open_voting {
        return Err(VoteError::WrongPhase);
    }

    // The active account row is already locked above. Calls for the same
    // voter therefore serialize even while no governance_votes row exists yet:
    // exactly one call can insert the first vote, and the next observes it.
    let existing_choice: Option<String> = sqlx::query_scalar(
        "SELECT choice FROM governance_votes \
         WHERE proposal_id = $1::uuid AND voter_account_id = $2 FOR UPDATE",
    )
    .bind(proposal_id)
    .bind(voter_account_id)
    .fetch_optional(&mut *tx)
    .await
    .map_err(VoteError::Database)?;

    let outcome = match existing_choice.as_deref() {
        Some(existing) if existing == choice.as_str() => VoteWriteOutcome::Unchanged,
        Some(_) => {
            sqlx::query(
                "UPDATE governance_votes SET choice = $3, updated_at = $4 \
                 WHERE proposal_id = $1::uuid AND voter_account_id = $2",
            )
            .bind(proposal_id)
            .bind(voter_account_id)
            .bind(choice.as_str())
            .bind(now)
            .execute(&mut *tx)
            .await
            .map_err(VoteError::Database)?;
            VoteWriteOutcome::Changed
        }
        None => {
            sqlx::query(
                "INSERT INTO governance_votes \
                     (proposal_id, voter_account_id, choice, updated_at) \
                 VALUES ($1::uuid, $2, $3, $4)",
            )
            .bind(proposal_id)
            .bind(voter_account_id)
            .bind(choice.as_str())
            .bind(now)
            .execute(&mut *tx)
            .await
            .map_err(VoteError::Database)?;
            VoteWriteOutcome::Created
        }
    };

    tx.commit().await.map_err(VoteError::Database)?;
    Ok(outcome)
}

/// Ergänze einen Gesprächsraum-Beitrag. Zulässig, solange der Antrag offen ist.
pub async fn add_message(
    pool: &PgPool,
    proposal_id: &str,
    author_account_id: &str,
    author_title: &str,
    body: &str,
    now: DateTime<Utc>,
) -> Result<ProposalMessage, MessageError> {
    let mut tx = pool.begin().await.map_err(MessageError::Database)?;

    // Guest exit and proposal finalization use account -> proposal ordering.
    // Lock the message author first as well so the FK insert cannot create the
    // inverse proposal -> account dependency and deadlock with guest exit.
    let active_author: Option<String> = sqlx::query_scalar(
        "SELECT id FROM domain_accounts WHERE id = $1 AND disabled = FALSE FOR UPDATE",
    )
    .bind(author_account_id)
    .fetch_optional(&mut *tx)
    .await
    .map_err(MessageError::Database)?;
    if active_author.is_none() {
        return Err(MessageError::Database(sqlx::Error::RowNotFound));
    }

    let (status, _, consent_until, voting_until) = lock_proposal_phase(&mut tx, proposal_id)
        .await
        .map_err(MessageError::Database)?
        .ok_or(MessageError::NotFound)?;
    let conversation_open = match status {
        ProposalStatus::Consent => now < consent_until,
        ProposalStatus::Voting => voting_until.is_some_and(|until| now < until),
        ProposalStatus::Accepted | ProposalStatus::Rejected | ProposalStatus::Withdrawn => false,
    };
    if !conversation_open {
        return Err(MessageError::WrongPhase);
    }

    let id = Uuid::new_v4().to_string();
    sqlx::query(
        "INSERT INTO governance_messages \
             (id, proposal_id, author_account_id, author_title, body, created_at) \
         VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6)",
    )
    .bind(&id)
    .bind(proposal_id)
    .bind(author_account_id)
    .bind(author_title)
    .bind(body)
    .bind(now)
    .execute(&mut *tx)
    .await
    .map_err(MessageError::Database)?;

    tx.commit().await.map_err(MessageError::Database)?;

    Ok(ProposalMessage {
        id,
        author_account_id: Some(author_account_id.to_string()),
        author_title: author_title.to_string(),
        body: body.to_string(),
        created_at: now,
    })
}

/// Lösche ein Gastkonto aus der kanonischen PostgreSQL-Wahrheit, ohne bereits
/// gemeinschaftlich sichtbare Beiträge oder Knoten zu vernichten.
///
/// - Eigene Weberanträge und ihre abhängigen Verfahrensdaten werden entfernt.
/// - Knoten bleiben bestehen, verlieren aber die aktive Urheberbindung.
/// - Von der gelöschten Garnrolle ausgehende oder zu ihr führende Fäden werden
///   entfernt, weil ihr Account-Endpunkt nicht mehr existiert.
/// - Beiträge in fremden Anträgen behalten Text und Anzeigenamen, verlieren
///   aber die live Account-ID.
/// - Leere eigene Anträge verschwinden; eigene Anträge mit Verfahrensgeschichte
///   bleiben als abgeschlossener Snapshot ohne aktive Accountbindung erhalten.
/// - Passkeys, Sessions und die Gastidentität werden atomar entfernt.
///
/// Die Rollenbedingung schützt gegen versehentliches Löschen eines Webers oder
/// Administrators.
pub async fn delete_guest_account(pool: &PgPool, account_id: &str) -> Result<(), GuestExitError> {
    // Reserve the same shared admission as the GET generation fence BEFORE
    // obtaining any connection or account/node lifecycle lock. Four parallel
    // exits otherwise occupy the remaining pool slots while their triggers
    // wait for that fence, starving the reader's response query.
    let _pool_budget = crate::state::reserve_postgres_domain_operation().await?;
    let mut tx = pool.begin().await?;

    // Serialize the complete account lifecycle before taking row or per-node
    // locks. Node creation uses the same account-scoped advisory lock from
    // before the durable node INSERT until its derived Faden is durable (or
    // compensated), so guest exit can never enter the historical commit gap.
    sqlx::query("SELECT pg_advisory_xact_lock($1::bigint)")
        .bind(account_lifecycle_lock_key(account_id))
        .execute(&mut *tx)
        .await?;

    // Lock and verify the exact active guest before touching durable traces.
    let role: Option<String> = sqlx::query_scalar(
        "SELECT role FROM domain_accounts WHERE id = $1 AND disabled = FALSE FOR UPDATE",
    )
    .bind(account_id)
    .fetch_optional(&mut *tx)
    .await?;
    if role.as_deref() != Some("gast") {
        return Err(GuestExitError::NotEligible);
    }

    // Existing node mutations acquire this same advisory lock before reading
    // or changing ownership. Lock every guest-owned node in deterministic id
    // order so account exit cannot race an already authorized edit or delete.
    let owned_node_ids: Vec<String> = sqlx::query_scalar(
        "SELECT id FROM domain_nodes \
         WHERE payload ? 'created_by_account_id' \
           AND payload ->> 'created_by_account_id' = $1 ORDER BY id",
    )
    .bind(account_id)
    .fetch_all(&mut *tx)
    .await?;
    let mut node_lock_keys: Vec<i64> = owned_node_ids
        .iter()
        .map(|node_id| node_mutation_lock_key(node_id))
        .collect();
    node_lock_keys.sort_unstable();
    node_lock_keys.dedup();
    if !node_lock_keys.is_empty() {
        // One roundtrip acquires every lock in numeric order. The ORDER BY is
        // part of the deadlock contract; do not replace this with an unordered
        // array scan.
        sqlx::query(
            "SELECT pg_advisory_xact_lock(lock_key) \
             FROM unnest($1::bigint[]) AS locks(lock_key) ORDER BY lock_key",
        )
        .bind(&node_lock_keys)
        .fetch_all(&mut *tx)
        .await?;
    }

    sqlx::query(
        "UPDATE domain_nodes \
         SET payload = payload - 'created_by_account_id', updated_at = NOW() \
         WHERE payload ? 'created_by_account_id' \
           AND payload ->> 'created_by_account_id' = $1",
    )
    .bind(account_id)
    .execute(&mut *tx)
    .await?;

    let ambiguous_legacy_endpoint: bool = sqlx::query_scalar(
        "SELECT EXISTS (\
             SELECT 1 FROM domain_edges e \
             WHERE EXISTS (SELECT 1 FROM domain_nodes n WHERE n.id = $1) \
               AND (\
                    (e.source_id = $1 AND NULLIF(e.payload ->> 'source_type', '') IS NULL) \
                 OR (e.target_id = $1 AND NULLIF(e.payload ->> 'target_type', '') IS NULL)\
               )\
         )",
    )
    .bind(account_id)
    .fetch_one(&mut *tx)
    .await?;
    if ambiguous_legacy_endpoint {
        return Err(GuestExitError::AmbiguousLegacyEndpoint);
    }

    sqlx::query(
        "DELETE FROM domain_edges \
         WHERE (source_id = $1 AND (payload ->> 'source_type' = 'account' \
                 OR NULLIF(payload ->> 'source_type', '') IS NULL)) \
            OR (target_id = $1 AND (payload ->> 'target_type' = 'account' \
                 OR NULLIF(payload ->> 'target_type', '') IS NULL))",
    )
    .bind(account_id)
    .execute(&mut *tx)
    .await?;

    sqlx::query("DELETE FROM passkey_credentials WHERE account_id = $1")
        .bind(account_id)
        .execute(&mut *tx)
        .await?;
    sqlx::query("DELETE FROM sessions WHERE account_id = $1")
        .bind(account_id)
        .execute(&mut *tx)
        .await?;
    let deleted = sqlx::query("DELETE FROM domain_accounts WHERE id = $1 AND role = 'gast'")
        .bind(account_id)
        .execute(&mut *tx)
        .await?;
    if deleted.rows_affected() != 1 {
        return Err(GuestExitError::Database(sqlx::Error::RowNotFound));
    }
    tx.commit().await?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Lesepfade
// ---------------------------------------------------------------------------

const PROPOSAL_WITH_COUNTS_SELECT: &str = "SELECT p.id::text AS id, p.kind, \
        p.webgemeindezentrum_id, p.title, \
        CASE WHEN public_target.id IS NOT NULL THEN p.target_node_id END AS target_node_id, \
        CASE WHEN public_target.id IS NOT NULL THEN p.target_node_title END AS target_node_title, \
        p.repeals_proposal_id::text AS repeals_proposal_id, \
        (SELECT r.id::text FROM governance_proposals r \
            WHERE r.repeals_proposal_id = p.id AND r.status IN ('consent', 'voting') \
            ORDER BY r.created_at DESC, r.id DESC LIMIT 1) AS pending_repeal_proposal_id, \
        (SELECT r.id::text FROM governance_proposals r \
            WHERE r.repeals_proposal_id = p.id AND r.status = 'accepted' \
            ORDER BY r.created_at DESC, r.id DESC LIMIT 1) AS repealed_by_proposal_id, \
        (SELECT r.finalized_at FROM governance_proposals r \
            WHERE r.repeals_proposal_id = p.id AND r.status = 'accepted' \
            ORDER BY r.created_at DESC, r.id DESC LIMIT 1) AS repealed_at, \
        p.applicant_account_id, p.applicant_title, p.summary, p.status, \
        p.created_at, p.consent_until, p.voting_until, p.finalized_at, \
        GREATEST( \
            p.created_at, \
            CASE \
                WHEN p.status = 'voting' THEN p.consent_until \
                WHEN p.status IN ('accepted', 'rejected', 'withdrawn') \
                    THEN COALESCE(p.finalized_at, p.created_at) \
                ELSE p.created_at \
            END, \
            COALESCE((SELECT max(v.created_at) FROM governance_vetoes v \
                WHERE v.proposal_id = p.id), p.created_at), \
            COALESCE((SELECT max(gm.created_at) FROM governance_messages gm \
                WHERE gm.proposal_id = p.id), p.created_at), \
            COALESCE((SELECT max(gv.updated_at) FROM governance_votes gv \
                WHERE gv.proposal_id = p.id), p.created_at) \
        ) AS last_activity_at, \
        (SELECT count(*) FROM governance_vetoes v \
            WHERE v.proposal_id = p.id) AS veto_count, \
        (SELECT count(*) FROM governance_messages gm \
            WHERE gm.proposal_id = p.id) AS message_count, \
        (SELECT count(*) FROM governance_votes gv \
            WHERE gv.proposal_id = p.id AND gv.choice = 'ja') AS yes_votes, \
        (SELECT count(*) FROM governance_votes gv \
            WHERE gv.proposal_id = p.id AND gv.choice = 'nein') AS no_votes, \
        (SELECT count(*) FROM governance_votes gv \
            WHERE gv.proposal_id = p.id AND gv.choice = 'enthaltung') AS abstain_votes \
     FROM governance_proposals p \
     LEFT JOIN domain_nodes public_target \
       ON public_target.id = p.target_node_id \
      AND public_target.search_visibility = 'public'";

fn proposal_from_row(row: &sqlx::postgres::PgRow) -> Result<ProposalWithCounts, sqlx::Error> {
    Ok(ProposalWithCounts {
        id: row.try_get("id")?,
        kind: row.try_get("kind")?,
        webgemeindezentrum_id: row.try_get("webgemeindezentrum_id")?,
        title: row.try_get("title")?,
        target_node_id: row.try_get("target_node_id")?,
        target_node_title: row.try_get("target_node_title")?,
        repeals_proposal_id: row.try_get("repeals_proposal_id")?,
        pending_repeal_proposal_id: row.try_get("pending_repeal_proposal_id")?,
        repealed_by_proposal_id: row.try_get("repealed_by_proposal_id")?,
        repealed_at: row.try_get("repealed_at")?,
        applicant_account_id: row.try_get("applicant_account_id")?,
        applicant_title: row.try_get("applicant_title")?,
        summary: row.try_get("summary")?,
        status: ProposalStatus::from_db(row.try_get::<String, _>("status")?.as_str())?,
        created_at: row.try_get("created_at")?,
        consent_until: row.try_get("consent_until")?,
        voting_until: row.try_get("voting_until")?,
        finalized_at: row.try_get("finalized_at")?,
        last_activity_at: row.try_get("last_activity_at")?,
        veto_count: row.try_get("veto_count")?,
        message_count: row.try_get("message_count")?,
        yes_votes: row.try_get("yes_votes")?,
        no_votes: row.try_get("no_votes")?,
        abstain_votes: row.try_get("abstain_votes")?,
    })
}

/// Alle Anträge, neueste zuerst.
pub async fn list_proposals(pool: &PgPool) -> Result<Vec<ProposalWithCounts>, sqlx::Error> {
    let query = format!("{PROPOSAL_WITH_COUNTS_SELECT} ORDER BY p.created_at DESC, p.id");
    let rows = sqlx::query(&query).fetch_all(pool).await?;
    rows.iter().map(proposal_from_row).collect()
}

/// Alle Anträge mit der persönlichen Beteiligung des optionalen Betrachters.
/// Ohne Account bleiben `own_vote` leer und `own_veto` falsch. Der Pfad ist
/// bewusst eine einzelne Listenabfrage statt eines Detailrequests je Antrag.
pub async fn list_proposals_for_viewer(
    pool: &PgPool,
    viewer_account_id: Option<&str>,
) -> Result<Vec<ProposalListEntry>, sqlx::Error> {
    let query = format!(
        "SELECT base.*, \
            (SELECT gv.choice FROM governance_votes gv \
                WHERE gv.proposal_id = base.id::uuid AND gv.voter_account_id = $1) AS own_vote, \
            EXISTS(SELECT 1 FROM governance_vetoes vv \
                WHERE vv.proposal_id = base.id::uuid AND vv.weber_account_id = $1) AS own_veto \
         FROM ({PROPOSAL_WITH_COUNTS_SELECT}) base \
         ORDER BY base.created_at DESC, base.id"
    );
    let rows = sqlx::query(&query)
        .bind(viewer_account_id)
        .fetch_all(pool)
        .await?;
    rows.iter()
        .map(|row| {
            Ok(ProposalListEntry {
                proposal: proposal_from_row(row)?,
                own_vote: row.try_get("own_vote")?,
                own_veto: row.try_get("own_veto")?,
            })
        })
        .collect()
}

/// Ein Antrag mit Zählständen; `None`, wenn er nicht existiert.
pub async fn get_proposal(
    pool: &PgPool,
    proposal_id: &str,
) -> Result<Option<ProposalWithCounts>, sqlx::Error> {
    let query = format!("{PROPOSAL_WITH_COUNTS_SELECT} WHERE p.id = $1::uuid");
    let row = sqlx::query(&query)
        .bind(proposal_id)
        .fetch_optional(pool)
        .await?;
    row.as_ref().map(proposal_from_row).transpose()
}

/// Alle Vetos eines Antrags, älteste zuerst.
pub async fn list_vetoes(pool: &PgPool, proposal_id: &str) -> Result<Vec<Veto>, sqlx::Error> {
    let rows = sqlx::query(
        "SELECT weber_account_id, weber_title, reason, created_at \
         FROM governance_vetoes WHERE proposal_id = $1::uuid \
         ORDER BY created_at, weber_account_id",
    )
    .bind(proposal_id)
    .fetch_all(pool)
    .await?;

    rows.iter()
        .map(|row| {
            Ok(Veto {
                weber_account_id: row.try_get("weber_account_id")?,
                weber_title: row.try_get("weber_title")?,
                reason: row.try_get("reason")?,
                created_at: row.try_get("created_at")?,
            })
        })
        .collect()
}

/// Aktuelle Stimme eines Accounts für einen Antrag, falls vorhanden.
pub async fn get_own_vote(
    pool: &PgPool,
    proposal_id: &str,
    voter_account_id: &str,
) -> Result<Option<String>, sqlx::Error> {
    let row = sqlx::query(
        "SELECT choice FROM governance_votes \
         WHERE proposal_id = $1::uuid AND voter_account_id = $2",
    )
    .bind(proposal_id)
    .bind(voter_account_id)
    .fetch_optional(pool)
    .await?;
    row.map(|row| row.try_get("choice")).transpose()
}

/// Alle Beiträge des Gesprächsraums, älteste zuerst.
pub async fn list_messages(
    pool: &PgPool,
    proposal_id: &str,
) -> Result<Vec<ProposalMessage>, sqlx::Error> {
    let rows = sqlx::query(
        "SELECT id::text AS id, author_account_id, author_title, body, created_at \
         FROM governance_messages WHERE proposal_id = $1::uuid \
         ORDER BY created_at, id",
    )
    .bind(proposal_id)
    .fetch_all(pool)
    .await?;

    rows.iter()
        .map(|row| {
            Ok(ProposalMessage {
                id: row.try_get("id")?,
                author_account_id: row.try_get("author_account_id")?,
                author_title: row.try_get("author_title")?,
                body: row.try_get("body")?,
                created_at: row.try_get("created_at")?,
            })
        })
        .collect()
}

// ---------------------------------------------------------------------------
// Fristauswertung und Aufnahme
// ---------------------------------------------------------------------------

/// Finalisiere alle fälligen Anträge. Serverseitig, idempotent und
/// restart-stabil: jeder Antrag wird in einer eigenen Transaktion unter
/// `FOR UPDATE` erneut geprüft, bevor ein Übergang geschrieben wird. Eine
/// konkurrierende Auswertung sieht nach dem Lock den bereits geschriebenen
/// Zustand und überspringt den Antrag.
pub async fn finalize_due_proposals(
    pool: &PgPool,
    now: DateTime<Utc>,
) -> Result<Vec<FinalizationOutcome>, sqlx::Error> {
    let mut outcomes = Vec::new();
    loop {
        let due_ids: Vec<String> = sqlx::query(
            "SELECT id::text AS id FROM governance_proposals \
             WHERE (status = 'consent' AND consent_until <= $1) \
                OR (status = 'voting' AND voting_until <= $1) \
             ORDER BY created_at, id",
        )
        .bind(now)
        .fetch_all(pool)
        .await?
        .iter()
        .map(|row| row.try_get("id"))
        .collect::<Result<_, _>>()?;
        if due_ids.is_empty() {
            break;
        }
        let mut progressed = false;
        for proposal_id in due_ids {
            if let Some(outcome) = finalize_one(pool, &proposal_id, now).await? {
                progressed = true;
                outcomes.push(outcome);
            }
        }
        if !progressed {
            break;
        }
    }
    Ok(outcomes)
}

#[cfg(feature = "integration-testing")]
/// Finalisiere ausschließlich den ausgewählten Antrag für den isolierten
/// PostgreSQL-/Browser-Beweis. Anders als der produktive Sweeper berührt dieser
/// Testpfad keine weiteren fälligen Verfahren in derselben Datenbank.
pub(crate) async fn finalize_testing_proposal(
    pool: &PgPool,
    proposal_id: &str,
    now: DateTime<Utc>,
) -> Result<Vec<FinalizationOutcome>, sqlx::Error> {
    Ok(finalize_one(pool, proposal_id, now)
        .await?
        .into_iter()
        .collect())
}

/// Finalisiere genau einen Antrag, falls er (noch) fällig ist.
async fn finalize_one(
    pool: &PgPool,
    proposal_id: &str,
    now: DateTime<Utc>,
) -> Result<Option<FinalizationOutcome>, sqlx::Error> {
    // Accepting a Weberantrag writes the canonical account generation.
    // This path is also invoked by the background sweeper, outside HTTP.
    let _pool_budget = crate::state::reserve_postgres_domain_operation().await?;
    let mut tx = pool.begin().await?;

    // Account exit locks account -> proposal. Finalization must use the same
    // order, otherwise the two transactions can deadlock. The first read only
    // discovers the immutable applicant id; all decisions use the row re-read
    // below after both locks are held.
    let applicant_account_id = match sqlx::query_scalar::<_, Option<String>>(
        "SELECT applicant_account_id FROM governance_proposals WHERE id = $1::uuid",
    )
    .bind(proposal_id)
    .fetch_optional(&mut *tx)
    .await?
    {
        Some(Some(account_id)) => account_id,
        Some(None) | None => return Ok(None),
    };

    let account_lock = sqlx::query("SELECT id FROM domain_accounts WHERE id = $1 FOR UPDATE")
        .bind(&applicant_account_id)
        .execute(&mut *tx)
        .await?;
    if account_lock.rows_affected() == 0 {
        return Ok(None);
    }

    let Some(row) = sqlx::query(
        "SELECT kind, status, applicant_account_id, consent_until, voting_until \
         FROM governance_proposals WHERE id = $1::uuid FOR UPDATE",
    )
    .bind(proposal_id)
    .fetch_optional(&mut *tx)
    .await?
    else {
        return Ok(None);
    };

    let kind: String = row.try_get("kind")?;
    if !matches!(kind.as_str(), "weberantrag" | "sachantrag") {
        return Err(sqlx::Error::Decode(
            format!("unknown governance proposal kind: {kind}").into(),
        ));
    }
    let status = ProposalStatus::from_db(row.try_get::<String, _>("status")?.as_str())?;
    let locked_applicant_account_id: Option<String> = row.try_get("applicant_account_id")?;
    if locked_applicant_account_id.as_deref() != Some(applicant_account_id.as_str()) {
        return Ok(None);
    }
    let consent_until: DateTime<Utc> = row.try_get("consent_until")?;
    let voting_until: Option<DateTime<Utc>> = row.try_get("voting_until")?;

    let outcome = match status {
        ProposalStatus::Consent if consent_until <= now => {
            let veto_count: i64 = sqlx::query(
                "SELECT count(*) AS n FROM governance_vetoes WHERE proposal_id = $1::uuid",
            )
            .bind(proposal_id)
            .fetch_one(&mut *tx)
            .await?
            .try_get("n")?;

            match consent_phase_outcome(veto_count) {
                ProposalStatus::Accepted => {
                    let promoted =
                        accept_proposal(&mut tx, proposal_id, &kind, &applicant_account_id, now)
                            .await?;
                    Some(FinalizationOutcome {
                        proposal_id: proposal_id.to_string(),
                        applicant_account_id,
                        status: ProposalStatus::Accepted,
                        promoted,
                    })
                }
                _ => {
                    // Mit Veto beginnt die Beratungs- und Abstimmungsphase im
                    // Anschluss an die volle Konsentphase.
                    let voting_until = consent_until + Duration::days(VOTING_PHASE_DAYS);
                    sqlx::query(
                        "UPDATE governance_proposals \
                         SET status = 'voting', voting_until = $2 WHERE id = $1::uuid",
                    )
                    .bind(proposal_id)
                    .bind(voting_until)
                    .execute(&mut *tx)
                    .await?;
                    Some(FinalizationOutcome {
                        proposal_id: proposal_id.to_string(),
                        applicant_account_id,
                        status: ProposalStatus::Voting,
                        promoted: false,
                    })
                }
            }
        }
        ProposalStatus::Voting if voting_until.is_some_and(|voting_until| voting_until <= now) => {
            let tally = sqlx::query(
                "SELECT \
                     count(*) FILTER (WHERE choice = 'ja') AS yes_votes, \
                     count(*) FILTER (WHERE choice = 'nein') AS no_votes \
                 FROM governance_votes WHERE proposal_id = $1::uuid",
            )
            .bind(proposal_id)
            .fetch_one(&mut *tx)
            .await?;
            let yes_votes: i64 = tally.try_get("yes_votes")?;
            let no_votes: i64 = tally.try_get("no_votes")?;

            match voting_phase_outcome(yes_votes, no_votes) {
                ProposalStatus::Accepted => {
                    let promoted =
                        accept_proposal(&mut tx, proposal_id, &kind, &applicant_account_id, now)
                            .await?;
                    Some(FinalizationOutcome {
                        proposal_id: proposal_id.to_string(),
                        applicant_account_id,
                        status: ProposalStatus::Accepted,
                        promoted,
                    })
                }
                _ => {
                    sqlx::query(
                        "UPDATE governance_proposals \
                         SET status = 'rejected', finalized_at = $2 WHERE id = $1::uuid",
                    )
                    .bind(proposal_id)
                    .bind(now)
                    .execute(&mut *tx)
                    .await?;
                    Some(FinalizationOutcome {
                        proposal_id: proposal_id.to_string(),
                        applicant_account_id,
                        status: ProposalStatus::Rejected,
                        promoted: false,
                    })
                }
            }
        }
        // Bereits finalisiert oder noch nicht fällig: idempotenter No-op.
        _ => None,
    };

    tx.commit().await?;

    if let Some(outcome) = &outcome {
        tracing::info!(
            event = "governance.proposal.finalized",
            proposal_id = %outcome.proposal_id,
            status = outcome.status.as_str(),
            promoted = outcome.promoted,
            "Governance proposal transitioned"
        );
    }

    Ok(outcome)
}

/// Vollziehe die Aufnahme in derselben Transaktion wie den Statuswechsel:
/// Antrag wird `accepted`, und die bereits kanonisch gespeicherte Garnrolle
/// erhält `role = 'weber'`. Profil, Verortung und Identität bleiben unverändert.
/// Fehlt die Gastidentität, bricht die Transaktion fail-closed ab. Replays sind
/// No-ops (Rolle bereits `weber`), Admin-Rollen werden nie herabgestuft.
async fn accept_proposal(
    tx: &mut Transaction<'_, Postgres>,
    proposal_id: &str,
    kind: &str,
    applicant_account_id: &str,
    now: DateTime<Utc>,
) -> Result<bool, sqlx::Error> {
    if kind == "sachantrag" {
        sqlx::query(
            "UPDATE governance_proposals \
             SET status = 'accepted', finalized_at = $2 WHERE id = $1::uuid",
        )
        .bind(proposal_id)
        .bind(now)
        .execute(&mut **tx)
        .await?;
        return Ok(false);
    }

    let result = sqlx::query(
        "UPDATE domain_accounts \
         SET role = 'weber', disabled = FALSE, updated_at = $2 \
         WHERE id = $1 AND role = 'gast' AND disabled = FALSE",
    )
    .bind(applicant_account_id)
    .bind(now)
    .execute(&mut **tx)
    .await?;

    if result.rows_affected() == 0 {
        let role: Option<String> = sqlx::query_scalar(
            "SELECT role FROM domain_accounts WHERE id = $1 AND disabled = FALSE",
        )
        .bind(applicant_account_id)
        .fetch_optional(&mut **tx)
        .await?;
        if !matches!(role.as_deref(), Some("weber") | Some("admin")) {
            return Err(sqlx::Error::RowNotFound);
        }
    }

    sqlx::query(
        "UPDATE governance_proposals \
         SET status = 'accepted', finalized_at = $2 WHERE id = $1::uuid",
    )
    .bind(proposal_id)
    .bind(now)
    .execute(&mut **tx)
    .await?;

    Ok(result.rows_affected() > 0)
}

/// Spiegle Beförderungen in den laufenden Account-Store, damit bestehende
/// Sessions die neue Rolle ohne Neuanmeldung erhalten. Der Store ist eine
/// Laufzeitprojektion; die kanonische Wahrheit liegt bereits in PostgreSQL.
pub async fn apply_promotions_to_store(
    accounts: &Arc<RwLock<AccountStore>>,
    outcomes: &[FinalizationOutcome],
) {
    let promoted: Vec<&FinalizationOutcome> =
        outcomes.iter().filter(|outcome| outcome.promoted).collect();
    if promoted.is_empty() {
        return;
    }

    let mut store = accounts.write().await;
    for outcome in promoted {
        if let Some(existing) = store.get(&outcome.applicant_account_id) {
            if existing.role == Role::Gast {
                let mut updated = existing.clone();
                updated.role = Role::Weber;
                store.insert(updated);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn consent_without_veto_is_accepted() {
        assert_eq!(consent_phase_outcome(0), ProposalStatus::Accepted);
    }

    #[test]
    fn consent_with_veto_moves_to_voting() {
        assert_eq!(consent_phase_outcome(1), ProposalStatus::Voting);
        assert_eq!(consent_phase_outcome(5), ProposalStatus::Voting);
    }

    #[test]
    fn voting_yes_majority_is_accepted() {
        assert_eq!(voting_phase_outcome(3, 2), ProposalStatus::Accepted);
        assert_eq!(voting_phase_outcome(1, 0), ProposalStatus::Accepted);
    }

    #[test]
    fn voting_tie_is_rejected() {
        assert_eq!(voting_phase_outcome(2, 2), ProposalStatus::Rejected);
    }

    #[test]
    fn voting_zero_zero_is_rejected() {
        assert_eq!(voting_phase_outcome(0, 0), ProposalStatus::Rejected);
    }

    #[test]
    fn voting_no_majority_is_rejected() {
        assert_eq!(voting_phase_outcome(1, 4), ProposalStatus::Rejected);
    }

    #[test]
    fn proposal_status_round_trips_through_db_strings() {
        for status in [
            ProposalStatus::Consent,
            ProposalStatus::Voting,
            ProposalStatus::Accepted,
            ProposalStatus::Rejected,
            ProposalStatus::Withdrawn,
        ] {
            assert_eq!(
                ProposalStatus::from_db(status.as_str()).expect("known status must parse"),
                status
            );
        }
        assert!(ProposalStatus::from_db("garbage").is_err());
    }

    #[test]
    fn proposal_list_projects_canonical_message_counts() {
        assert!(PROPOSAL_WITH_COUNTS_SELECT.contains("FROM governance_messages gm"));
        assert!(PROPOSAL_WITH_COUNTS_SELECT.contains("AS message_count"));
    }

    #[test]
    fn proposal_list_projects_canonical_last_activity() {
        assert!(PROPOSAL_WITH_COUNTS_SELECT.contains("AS last_activity_at"));
        assert!(PROPOSAL_WITH_COUNTS_SELECT.contains("max(v.created_at)"));
        assert!(PROPOSAL_WITH_COUNTS_SELECT.contains("max(gm.created_at)"));
        assert!(PROPOSAL_WITH_COUNTS_SELECT.contains("max(gv.updated_at)"));
        assert!(
            PROPOSAL_WITH_COUNTS_SELECT.contains("WHEN p.status = 'voting' THEN p.consent_until")
        );
    }

    #[test]
    fn vote_choice_serializes_lowercase() {
        assert_eq!(VoteChoice::Ja.as_str(), "ja");
        assert_eq!(VoteChoice::Nein.as_str(), "nein");
        assert_eq!(VoteChoice::Enthaltung.as_str(), "enthaltung");
        let parsed: VoteChoice = serde_json::from_str("\"enthaltung\"").expect("parse");
        assert_eq!(parsed, VoteChoice::Enthaltung);
    }
}
