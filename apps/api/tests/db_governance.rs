//! PostgreSQL-Integrationsbeweis für den Gast-zu-Weber-Prozess.
//!
//! Run with a direct PostgreSQL connection:
//! `DATABASE_URL=postgres://... cargo test --locked --test db_governance -- --include-ignored`

mod support;

use std::{path::PathBuf, str::FromStr};

use chrono::{Duration, TimeZone, Utc};
use serial_test::serial;
use sha2::{Digest, Sha256};
use sqlx::postgres::{PgConnectOptions, PgPoolOptions};
use tokio::time::{sleep, timeout, Duration as TokioDuration};
use weltgewebe_api::governance::{
    add_message, add_veto, create_repeal_proposal, create_sach_proposal, create_weber_proposal,
    delete_guest_account, finalize_due_proposals, get_proposal, list_proposals,
    list_proposals_for_viewer, upsert_vote, withdraw_proposal, CreateProposalError, MessageError,
    ProposalStatus, RepealProposalError, VetoError, VoteChoice, VoteError, VoteWriteOutcome,
    WithdrawProposalError,
};

const GUEST_A: &str = "gov-proof-guest-a";
const GUEST_B: &str = "gov-proof-guest-b";
const GUEST_C: &str = "gov-proof-guest-c";
const WEBER_A: &str = "gov-proof-weber-a";
const WEBER_B: &str = "gov-proof-weber-b";
const GUEST_NODE: &str = "gov-proof-guest-node";
const GUEST_EDGE: &str = "gov-proof-guest-edge";
const SACH_NODE: &str = "gov-proof-sach-node";
const DETACHED_PROOF_APPLICANT_TITLE: &str = "gov-proof:Gast C";

fn direct_database_url() -> String {
    let url = std::env::var("DATABASE_URL")
        .expect("DATABASE_URL must point to direct PostgreSQL to run db_governance");
    assert!(
        !url.contains(":6432"),
        "do not use PgBouncer for migration tests"
    );
    support::postgres_proof::validated_direct_disposable_url(url)
}

async fn pool() -> sqlx::PgPool {
    let options = PgConnectOptions::from_str(&direct_database_url()).expect("valid DATABASE_URL");
    let pool = PgPoolOptions::new()
        .max_connections(4)
        .connect_with(options)
        .await
        .expect("connect PostgreSQL");
    let migrations_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("migrations");
    let migrator = sqlx::migrate::Migrator::new(migrations_dir)
        .await
        .expect("load migrations");
    migrator.run(&pool).await.expect("run migrations");
    pool
}

async fn cleanup(pool: &sqlx::PgPool) {
    sqlx::query("DELETE FROM domain_edges WHERE id LIKE 'gov-proof-%'")
        .execute(pool)
        .await
        .expect("clean edges");
    sqlx::query("DELETE FROM domain_nodes WHERE id LIKE 'gov-proof-%'")
        .execute(pool)
        .await
        .expect("clean nodes");
    // Repeal proposals reference their historical target with ON DELETE RESTRICT.
    // Remove those children first; production has deliberately no generic
    // proposal-deletion surface, this ordering exists only for disposable tests.
    sqlx::query(
        "DELETE FROM governance_proposals \
         WHERE repeals_proposal_id IS NOT NULL \
           AND (applicant_account_id LIKE 'gov-proof-%' \
             OR (applicant_account_id IS NULL AND applicant_title LIKE 'gov-proof:%'))",
    )
    .execute(pool)
    .await
    .expect("clean repeal proposals");
    sqlx::query(
        "DELETE FROM governance_proposals \
         WHERE applicant_account_id LIKE 'gov-proof-%' \
            OR (applicant_account_id IS NULL AND applicant_title LIKE 'gov-proof:%')",
    )
    .execute(pool)
    .await
    .expect("clean proposals");
    sqlx::query("DELETE FROM passkey_credentials WHERE account_id LIKE 'gov-proof-%'")
        .execute(pool)
        .await
        .expect("clean passkeys");
    sqlx::query("DELETE FROM sessions WHERE account_id LIKE 'gov-proof-%'")
        .execute(pool)
        .await
        .expect("clean sessions");
    sqlx::query("DELETE FROM domain_accounts WHERE id LIKE 'gov-proof-%'")
        .execute(pool)
        .await
        .expect("clean accounts");
}

fn node_mutation_lock_key(node_id: &str) -> i64 {
    let mut hasher = Sha256::new();
    hasher.update(b"weltgewebe:node-mutation:v1");
    hasher.update((node_id.len() as u64).to_be_bytes());
    hasher.update(node_id.as_bytes());
    let digest = hasher.finalize();
    i64::from_be_bytes(digest[..8].try_into().expect("SHA-256 prefix"))
}

async fn seed_guest_node(pool: &sqlx::PgPool, account_id: &str, node_id: &str) {
    sqlx::query(
        "INSERT INTO domain_nodes \
         (id, kind, title, lat, lon, created_at, updated_at, payload) \
         VALUES ($1, 'place', 'Rennknoten', 53.5, 10.0, NOW(), NOW(), \
                 jsonb_build_object('created_by_account_id', $2::text))",
    )
    .bind(node_id)
    .bind(account_id)
    .execute(pool)
    .await
    .expect("seed guest node");
}

async fn seed_account(pool: &sqlx::PgPool, id: &str, role: &str) {
    sqlx::query(
        "INSERT INTO domain_accounts \
         (id, kind, title, map_state, radius_m, disabled, role, public_payload, private_payload) \
         VALUES ($1, 'garnrolle', $2, 'not_on_map', 0, FALSE, $3, '{}', '{}')",
    )
    .bind(id)
    .bind(format!("Account {id}"))
    .bind(role)
    .execute(pool)
    .await
    .expect("seed account");
}

#[tokio::test]
#[serial]
#[ignore = "requires direct PostgreSQL"]
async fn sachantraege_require_weber_allow_multiple_and_accept_without_promotion() {
    let pool = pool().await;
    cleanup(&pool).await;
    seed_account(&pool, GUEST_A, "gast").await;
    seed_account(&pool, WEBER_A, "weber").await;
    seed_guest_node(&pool, WEBER_A, SACH_NODE).await;

    let t0 = Utc.with_ymd_and_hms(2026, 8, 8, 12, 0, 0).unwrap();
    let guest_attempt =
        create_sach_proposal(&pool, GUEST_A, "Gast A", "Nicht zulässig", None, None, t0).await;
    assert!(matches!(
        guest_attempt,
        Err(CreateProposalError::NotSachApplicant)
    ));

    // A public proposal must never snapshot the ID/title of a private,
    // hidden or revoked node, including if the applicant is its owner.
    for state in ["private", "hidden", "revoked"] {
        sqlx::query("UPDATE domain_nodes SET search_visibility = $1 WHERE id = $2")
            .bind(state)
            .bind(SACH_NODE)
            .execute(&pool)
            .await
            .expect("test-only visibility change");
        let rejected = create_sach_proposal(
            &pool,
            WEBER_A,
            "Weber A",
            "Attempted sensitive reference",
            None,
            Some(SACH_NODE),
            t0,
        )
        .await;
        assert!(
            matches!(rejected, Err(CreateProposalError::TargetNodeNotFound)),
            "public proposal accepted sensitive target: {state}"
        );
        let leaked: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM governance_proposals WHERE target_node_id = $1",
        )
        .bind(SACH_NODE)
        .fetch_one(&pool)
        .await
        .expect("read synthetic proposal count");
        assert_eq!(leaked, 0, "sensitive target was snapshotted: {state}");
    }
    sqlx::query("UPDATE domain_nodes SET search_visibility = 'public' WHERE id = $1")
        .bind(SACH_NODE)
        .execute(&pool)
        .await
        .expect("restore public test fixture");

    let first = create_sach_proposal(
        &pool,
        WEBER_A,
        "Weber A",
        "Werkstattzeiten beschließen",
        Some("Die Nutzung soll verlässlich werden."),
        Some(SACH_NODE),
        t0,
    )
    .await
    .expect("create first Sachantrag");
    let second = create_sach_proposal(
        &pool,
        WEBER_A,
        "Weber A",
        "Materialbudget beschließen",
        None,
        None,
        t0 + Duration::minutes(1),
    )
    .await
    .expect("multiple open Sachantraege are allowed");
    assert_eq!(first.target_node_id.as_deref(), Some(SACH_NODE));
    assert_eq!(first.target_node_title.as_deref(), Some("Rennknoten"));
    assert_eq!(second.kind, "sachantrag");

    let outcomes = finalize_due_proposals(&pool, t0 + Duration::days(7) + Duration::minutes(1))
        .await
        .expect("finalize Sachantraege");
    assert_eq!(outcomes.len(), 2);
    assert!(outcomes
        .iter()
        .all(|outcome| { outcome.status == ProposalStatus::Accepted && !outcome.promoted }));
    let role: String = sqlx::query_scalar("SELECT role FROM domain_accounts WHERE id = $1")
        .bind(WEBER_A)
        .fetch_one(&pool)
        .await
        .expect("read unchanged role");
    assert_eq!(role, "weber");

    sqlx::query("DELETE FROM domain_nodes WHERE id = $1")
        .bind(SACH_NODE)
        .execute(&pool)
        .await
        .expect("remove target node");
    let after_delete = get_proposal(&pool, &first.id)
        .await
        .expect("read proposal")
        .expect("proposal remains");
    assert_eq!(after_delete.target_node_id, None);
    assert_eq!(after_delete.target_node_title, None);
    // The historical title remains durably stored for authorized recovery or
    // future policy decisions, but no longer escapes via public APIs once its
    // former node's public visibility cannot be proven.
    let archived_title: Option<String> = sqlx::query_scalar(
        "SELECT target_node_title FROM governance_proposals WHERE id = $1::uuid",
    )
    .bind(&first.id)
    .fetch_one(&pool)
    .await
    .expect("historical proposal snapshot");
    assert_eq!(archived_title.as_deref(), Some("Rennknoten"));

    cleanup(&pool).await;
}

#[tokio::test]
#[serial]
#[ignore = "requires direct PostgreSQL"]
async fn own_open_proposal_withdrawal_preserves_history_and_cannot_beat_deadline() {
    let pool = pool().await;
    cleanup(&pool).await;
    seed_account(&pool, GUEST_A, "gast").await;
    seed_account(&pool, WEBER_A, "weber").await;

    let t0 = Utc.with_ymd_and_hms(2026, 8, 16, 8, 0, 0).unwrap();
    let proposal =
        create_weber_proposal(&pool, GUEST_A, "Gast A", Some("Ich möchte mitweben."), t0)
            .await
            .expect("create Weber proposal");
    add_message(
        &pool,
        &proposal.id,
        GUEST_A,
        "Gast A",
        "Die Rücknahme soll die Geschichte nicht löschen.",
        t0 + Duration::hours(1),
    )
    .await
    .expect("write procedural history");

    let foreign = withdraw_proposal(&pool, &proposal.id, WEBER_A, t0 + Duration::hours(2)).await;
    assert!(matches!(foreign, Err(WithdrawProposalError::NotApplicant)));

    let withdrawn_at = t0 + Duration::days(1);
    let withdrawn = withdraw_proposal(&pool, &proposal.id, GUEST_A, withdrawn_at)
        .await
        .expect("withdraw own open proposal");
    assert_eq!(withdrawn.status, ProposalStatus::Withdrawn);
    assert_eq!(withdrawn.finalized_at, Some(withdrawn_at));
    assert_eq!(withdrawn.message_count, 1);
    assert_eq!(withdrawn.applicant_account_id.as_deref(), Some(GUEST_A));

    let closed_message = add_message(
        &pool,
        &proposal.id,
        GUEST_A,
        "Gast A",
        "Zu spät",
        withdrawn_at + Duration::minutes(1),
    )
    .await;
    assert!(matches!(closed_message, Err(MessageError::WrongPhase)));
    assert!(finalize_due_proposals(&pool, t0 + Duration::days(30))
        .await
        .expect("withdrawn proposal stays terminal")
        .is_empty());
    let role: String = sqlx::query_scalar("SELECT role FROM domain_accounts WHERE id = $1")
        .bind(GUEST_A)
        .fetch_one(&pool)
        .await
        .expect("read guest role");
    assert_eq!(role, "gast", "withdrawal must never grant Weber status");

    let second_created = withdrawn_at + Duration::days(1);
    let second = create_weber_proposal(&pool, GUEST_A, "Gast A", None, second_created)
        .await
        .expect("withdrawal releases the one-open-proposal constraint");
    let late_withdraw = withdraw_proposal(&pool, &second.id, GUEST_A, second.consent_until).await;
    assert!(matches!(
        late_withdraw,
        Err(WithdrawProposalError::WrongPhase)
    ));
    let outcomes = finalize_due_proposals(&pool, second.consent_until)
        .await
        .expect("deadline decision remains authoritative");
    assert_eq!(outcomes.len(), 1);
    assert_eq!(outcomes[0].status, ProposalStatus::Accepted);
    assert!(outcomes[0].promoted);

    cleanup(&pool).await;
}

#[tokio::test]
#[serial]
#[ignore = "requires direct PostgreSQL"]
async fn guest_exit_preserves_withdrawn_proposal_and_detaches_applicant() {
    let pool = pool().await;
    cleanup(&pool).await;
    seed_account(&pool, GUEST_A, "gast").await;

    let t0 = Utc.with_ymd_and_hms(2026, 8, 16, 9, 0, 0).unwrap();
    let proposal = create_weber_proposal(
        &pool,
        GUEST_A,
        "gov-proof:Gast A",
        Some("Dieser zurückgezogene Antrag bleibt Verfahrensgeschichte."),
        t0,
    )
    .await
    .expect("create proposal before withdrawal and exit");
    let withdrawn_at = t0 + Duration::hours(2);
    withdraw_proposal(&pool, &proposal.id, GUEST_A, withdrawn_at)
        .await
        .expect("withdraw before account exit");

    delete_guest_account(&pool, GUEST_A)
        .await
        .expect("delete guest after withdrawal");

    let retained = get_proposal(&pool, &proposal.id)
        .await
        .expect("read withdrawn history after account exit")
        .expect("withdrawn proposal must remain");
    assert_eq!(retained.status, ProposalStatus::Withdrawn);
    assert_eq!(retained.applicant_account_id, None);
    assert_eq!(retained.finalized_at, Some(withdrawn_at));
    let account_exists: bool =
        sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM domain_accounts WHERE id = $1)")
            .bind(GUEST_A)
            .fetch_one(&pool)
            .await
            .expect("read deleted account state");
    assert!(!account_exists);

    cleanup(&pool).await;
}

#[tokio::test]
#[serial]
#[ignore = "requires direct PostgreSQL"]
async fn repeal_is_a_new_sachantrag_and_never_rewrites_the_old_decision() {
    let pool = pool().await;
    cleanup(&pool).await;
    seed_account(&pool, WEBER_A, "weber").await;
    seed_account(&pool, WEBER_B, "weber").await;

    let t0 = Utc.with_ymd_and_hms(2026, 8, 16, 8, 0, 0).unwrap();
    let target = create_sach_proposal(
        &pool,
        WEBER_A,
        "Weber A",
        "Werkstatt sonntags öffnen",
        Some("Erster gemeinschaftlicher Beschluss."),
        None,
        t0,
    )
    .await
    .expect("create target Sachantrag");
    finalize_due_proposals(&pool, target.consent_until)
        .await
        .expect("accept target");
    assert_eq!(
        get_proposal(&pool, &target.id)
            .await
            .expect("read target")
            .expect("target")
            .status,
        ProposalStatus::Accepted
    );

    let first = create_repeal_proposal(
        &pool,
        &target.id,
        WEBER_B,
        "Weber B",
        Some("Der Beschluss hat sich nicht bewährt."),
        t0 + Duration::days(8),
    )
    .await
    .expect("create repeal proposal");
    assert_eq!(first.kind, "sachantrag");
    assert_eq!(
        first.repeals_proposal_id.as_deref(),
        Some(target.id.as_str())
    );
    assert_eq!(first.status, ProposalStatus::Consent);
    assert!(first
        .title
        .as_deref()
        .is_some_and(|title| title.starts_with("Aufhebung: ")));

    let target_with_pending = get_proposal(&pool, &target.id)
        .await
        .expect("read target with pending repeal")
        .expect("target");
    assert_eq!(target_with_pending.status, ProposalStatus::Accepted);
    assert_eq!(
        target_with_pending.pending_repeal_proposal_id.as_deref(),
        Some(first.id.as_str())
    );
    assert_eq!(target_with_pending.repealed_by_proposal_id, None);

    let duplicate = create_repeal_proposal(
        &pool,
        &target.id,
        WEBER_A,
        "Weber A",
        None,
        t0 + Duration::days(8) + Duration::minutes(1),
    )
    .await;
    assert!(matches!(
        duplicate,
        Err(RepealProposalError::AlreadyHasRepeal)
    ));

    let withdrawn = withdraw_proposal(&pool, &first.id, WEBER_B, t0 + Duration::days(9))
        .await
        .expect("withdraw first repeal attempt");
    assert_eq!(withdrawn.status, ProposalStatus::Withdrawn);
    assert_eq!(
        withdrawn.repeals_proposal_id.as_deref(),
        Some(target.id.as_str())
    );
    let target_after_withdrawal = get_proposal(&pool, &target.id)
        .await
        .expect("read target after withdrawn repeal")
        .expect("target");
    assert_eq!(target_after_withdrawal.status, ProposalStatus::Accepted);
    assert_eq!(target_after_withdrawal.pending_repeal_proposal_id, None);
    assert_eq!(target_after_withdrawal.repealed_by_proposal_id, None);

    let second = create_repeal_proposal(
        &pool,
        &target.id,
        WEBER_A,
        "Weber A",
        Some("Erneuter Aufhebungsantrag, diesmal mit Abstimmung."),
        t0 + Duration::days(10),
    )
    .await
    .expect("withdrawn repeal permits a later new procedure");
    add_veto(
        &pool,
        &second.id,
        WEBER_B,
        "Weber B",
        "Die Aufhebung soll ausdrücklich abgestimmt werden.",
        t0 + Duration::days(11),
    )
    .await
    .expect("open voting on second repeal attempt");
    let second_phase = finalize_due_proposals(&pool, second.consent_until)
        .await
        .expect("move second repeal into voting");
    assert_eq!(second_phase.len(), 1);
    assert_eq!(second_phase[0].status, ProposalStatus::Voting);
    let second_voting = get_proposal(&pool, &second.id)
        .await
        .expect("read voting repeal")
        .expect("second repeal remains");
    let second_voting_until = second_voting
        .voting_until
        .expect("voting repeal has a second deadline");
    let rejected_outcomes = finalize_due_proposals(&pool, second_voting_until)
        .await
        .expect("reject zero-to-zero repeal vote");
    assert_eq!(rejected_outcomes.len(), 1);
    assert_eq!(rejected_outcomes[0].proposal_id, second.id);
    assert_eq!(rejected_outcomes[0].status, ProposalStatus::Rejected);

    let target_after_rejection = get_proposal(&pool, &target.id)
        .await
        .expect("read target after rejected repeal")
        .expect("target");
    assert_eq!(target_after_rejection.status, ProposalStatus::Accepted);
    assert_eq!(target_after_rejection.pending_repeal_proposal_id, None);
    assert_eq!(target_after_rejection.repealed_by_proposal_id, None);

    let third = create_repeal_proposal(
        &pool,
        &target.id,
        WEBER_B,
        "Weber B",
        Some("Neuer Aufhebungsantrag nach der Ablehnung."),
        second_voting_until + Duration::minutes(1),
    )
    .await
    .expect("rejected repeal permits a later new procedure");
    let third_outcomes = finalize_due_proposals(&pool, third.consent_until)
        .await
        .expect("accept third repeal proposal");
    assert_eq!(third_outcomes.len(), 1);
    assert_eq!(third_outcomes[0].proposal_id, third.id);
    assert_eq!(third_outcomes[0].status, ProposalStatus::Accepted);
    assert!(!third_outcomes[0].promoted);

    let original = get_proposal(&pool, &target.id)
        .await
        .expect("read repealed original")
        .expect("target remains");
    assert_eq!(
        original.status,
        ProposalStatus::Accepted,
        "historical decision itself must remain accepted"
    );
    assert_eq!(original.pending_repeal_proposal_id, None);
    assert_eq!(
        original.repealed_by_proposal_id.as_deref(),
        Some(third.id.as_str())
    );
    let accepted_repeal = get_proposal(&pool, &third.id)
        .await
        .expect("read accepted repeal")
        .expect("accepted repeal remains");
    assert_eq!(accepted_repeal.status, ProposalStatus::Accepted);
    assert_eq!(
        accepted_repeal.repeals_proposal_id.as_deref(),
        Some(target.id.as_str())
    );
    assert_eq!(original.repealed_at, accepted_repeal.finalized_at);

    let duplicate_after_accept = create_repeal_proposal(
        &pool,
        &target.id,
        WEBER_A,
        "Weber A",
        None,
        third.consent_until + Duration::minutes(1),
    )
    .await;
    assert!(matches!(
        duplicate_after_accept,
        Err(RepealProposalError::AlreadyHasRepeal)
    ));

    let recursive = create_repeal_proposal(
        &pool,
        &third.id,
        WEBER_A,
        "Weber A",
        None,
        third.consent_until + Duration::minutes(1),
    )
    .await;
    assert!(matches!(
        recursive,
        Err(RepealProposalError::TargetIsRepeal)
    ));

    cleanup(&pool).await;
}

#[tokio::test]
#[serial]
#[ignore = "requires direct PostgreSQL"]
async fn concurrent_repeal_creation_allows_exactly_one_active_child() {
    let pool = pool().await;
    cleanup(&pool).await;
    seed_account(&pool, WEBER_A, "weber").await;
    seed_account(&pool, WEBER_B, "weber").await;

    let t0 = Utc.with_ymd_and_hms(2026, 8, 16, 10, 0, 0).unwrap();
    let target = create_sach_proposal(
        &pool,
        WEBER_A,
        "Weber A",
        "Parallelität beim Aufhebungsverfahren prüfen",
        None,
        None,
        t0,
    )
    .await
    .expect("create concurrent repeal target");
    finalize_due_proposals(&pool, target.consent_until)
        .await
        .expect("accept concurrent repeal target");

    let first_pool = pool.clone();
    let second_pool = pool.clone();
    let first_target = target.id.clone();
    let second_target = target.id.clone();
    let first = create_repeal_proposal(
        &first_pool,
        &first_target,
        WEBER_A,
        "Weber A",
        Some("Erster paralleler Versuch."),
        t0 + Duration::days(8),
    );
    let second = create_repeal_proposal(
        &second_pool,
        &second_target,
        WEBER_B,
        "Weber B",
        Some("Zweiter paralleler Versuch."),
        t0 + Duration::days(8),
    );
    let (first, second) = tokio::join!(first, second);
    let created = match (first, second) {
        (Ok(created), Err(RepealProposalError::AlreadyHasRepeal))
        | (Err(RepealProposalError::AlreadyHasRepeal), Ok(created)) => created,
        _ => panic!("exactly one concurrent repeal creation must win"),
    };
    assert_eq!(
        created.repeals_proposal_id.as_deref(),
        Some(target.id.as_str())
    );

    let child_count: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM governance_proposals WHERE repeals_proposal_id = $1::uuid",
    )
    .bind(&target.id)
    .fetch_one(&pool)
    .await
    .expect("count concurrent repeal children");
    assert_eq!(child_count, 1);

    cleanup(&pool).await;
}

#[tokio::test]
#[serial]
#[ignore = "requires direct PostgreSQL"]
async fn sachantrag_uses_shared_veto_voting_majority_and_self_decision_guard() {
    let pool = pool().await;
    cleanup(&pool).await;
    seed_account(&pool, WEBER_A, "weber").await;
    seed_account(&pool, WEBER_B, "weber").await;

    let t0 = Utc.with_ymd_and_hms(2026, 8, 8, 12, 0, 0).unwrap();
    let proposal = create_sach_proposal(
        &pool,
        WEBER_A,
        "Weber A",
        "Gemeinschaftsraum öffnen",
        None,
        None,
        t0,
    )
    .await
    .expect("create Sachantrag");

    let own_veto = add_veto(
        &pool,
        &proposal.id,
        WEBER_A,
        "Weber A",
        "Eigener Einwand",
        t0 + Duration::days(1),
    )
    .await;
    assert!(matches!(own_veto, Err(VetoError::ApplicantCannotDecide)));
    add_veto(
        &pool,
        &proposal.id,
        WEBER_B,
        "Weber B",
        "Bitte ausdrücklich abstimmen",
        t0 + Duration::days(1),
    )
    .await
    .expect("foreign veto");

    let consent_entries = list_proposals_for_viewer(&pool, Some(WEBER_B))
        .await
        .expect("list consent participation for viewer");
    let consent_entry = consent_entries
        .iter()
        .find(|entry| entry.proposal.id == proposal.id)
        .expect("proposal in viewer list");
    assert!(consent_entry.own_veto);
    assert_eq!(consent_entry.own_vote, None);
    assert_eq!(
        consent_entry.proposal.last_activity_at,
        t0 + Duration::days(1),
        "latest veto is the canonical activity during consent",
    );

    let anonymous_entries = list_proposals_for_viewer(&pool, None)
        .await
        .expect("list anonymous proposal projection");
    let anonymous_entry = anonymous_entries
        .iter()
        .find(|entry| entry.proposal.id == proposal.id)
        .expect("proposal in anonymous list");
    assert!(!anonymous_entry.own_veto);
    assert_eq!(anonymous_entry.own_vote, None);
    assert_eq!(
        anonymous_entry.proposal.last_activity_at,
        t0 + Duration::days(1),
    );

    finalize_due_proposals(&pool, t0 + Duration::days(7))
        .await
        .expect("open voting");

    let voting_before_vote = list_proposals_for_viewer(&pool, Some(WEBER_B))
        .await
        .expect("list proposal after voting phase transition")
        .into_iter()
        .find(|entry| entry.proposal.id == proposal.id)
        .expect("proposal after voting phase transition");
    assert_eq!(
        voting_before_vote.proposal.last_activity_at,
        t0 + Duration::days(7),
        "completed phase transition becomes activity, not its future deadline",
    );

    let own_vote = upsert_vote(
        &pool,
        &proposal.id,
        WEBER_A,
        VoteChoice::Ja,
        t0 + Duration::days(8),
    )
    .await;
    assert!(matches!(own_vote, Err(VoteError::ApplicantCannotDecide)));
    upsert_vote(
        &pool,
        &proposal.id,
        WEBER_B,
        VoteChoice::Ja,
        t0 + Duration::days(8),
    )
    .await
    .expect("foreign yes vote");

    let voting_entries = list_proposals_for_viewer(&pool, Some(WEBER_B))
        .await
        .expect("list voting participation for viewer");
    let voting_entry = voting_entries
        .iter()
        .find(|entry| entry.proposal.id == proposal.id)
        .expect("proposal in voting viewer list");
    assert_eq!(voting_entry.own_vote.as_deref(), Some("ja"));
    assert!(voting_entry.own_veto);
    assert_eq!(
        voting_entry.proposal.last_activity_at,
        t0 + Duration::days(8),
        "latest vote update becomes the canonical activity",
    );

    let outcomes = finalize_due_proposals(&pool, t0 + Duration::days(14))
        .await
        .expect("finalize voting");
    assert_eq!(outcomes.len(), 1);
    assert_eq!(outcomes[0].status, ProposalStatus::Accepted);
    assert!(!outcomes[0].promoted);

    cleanup(&pool).await;
}

#[tokio::test]
#[serial]
#[ignore = "requires direct PostgreSQL"]
async fn no_veto_promotes_guest_atomically_and_idempotently() {
    let pool = pool().await;
    cleanup(&pool).await;
    seed_account(&pool, GUEST_A, "gast").await;

    let t0 = Utc.with_ymd_and_hms(2026, 7, 1, 12, 0, 0).unwrap();
    let proposal = create_weber_proposal(&pool, GUEST_A, "Gast A", Some("Hallo"), t0)
        .await
        .expect("create proposal");

    let outcomes = finalize_due_proposals(&pool, t0 + Duration::days(7))
        .await
        .expect("finalize");
    assert_eq!(outcomes.len(), 1);
    assert_eq!(outcomes[0].status, ProposalStatus::Accepted);
    assert!(outcomes[0].promoted);

    let role: String = sqlx::query_scalar("SELECT role FROM domain_accounts WHERE id = $1")
        .bind(GUEST_A)
        .fetch_one(&pool)
        .await
        .expect("read role");
    assert_eq!(role, "weber");
    assert_eq!(
        get_proposal(&pool, &proposal.id)
            .await
            .expect("read proposal")
            .expect("proposal")
            .status,
        ProposalStatus::Accepted
    );

    assert!(finalize_due_proposals(&pool, t0 + Duration::days(30))
        .await
        .expect("repeat finalize")
        .is_empty());
    let rows: i64 = sqlx::query_scalar("SELECT count(*) FROM domain_accounts WHERE id = $1")
        .bind(GUEST_A)
        .fetch_one(&pool)
        .await
        .expect("count account");
    assert_eq!(rows, 1, "promotion must never create a duplicate Garnrolle");

    cleanup(&pool).await;
}

#[tokio::test]
#[serial]
#[ignore = "requires direct PostgreSQL"]
async fn veto_opens_exact_second_phase_and_yes_must_exceed_no() {
    let pool = pool().await;
    cleanup(&pool).await;
    seed_account(&pool, GUEST_A, "gast").await;
    seed_account(&pool, GUEST_B, "gast").await;
    seed_account(&pool, WEBER_A, "weber").await;
    seed_account(&pool, WEBER_B, "weber").await;

    let t0 = Utc.with_ymd_and_hms(2026, 7, 1, 12, 0, 0).unwrap();

    let forbidden = create_weber_proposal(&pool, WEBER_A, "Weber A", None, t0).await;
    assert!(matches!(forbidden, Err(CreateProposalError::NotGuest)));

    let accepted = create_weber_proposal(&pool, GUEST_A, "Gast A", None, t0)
        .await
        .expect("create accepted candidate");
    let own_veto = add_veto(
        &pool,
        &accepted.id,
        GUEST_A,
        "Gast A",
        "Eigene Aufnahme blockieren",
        t0 + Duration::days(1),
    )
    .await;
    assert!(matches!(own_veto, Err(VetoError::ActorNotEligible)));

    let guest_veto = add_veto(
        &pool,
        &accepted.id,
        GUEST_B,
        "Gast B",
        "Bitte zuerst beraten",
        t0 + Duration::days(1),
    )
    .await;
    assert!(matches!(guest_veto, Err(VetoError::ActorNotEligible)));

    add_veto(
        &pool,
        &accepted.id,
        WEBER_A,
        "Weber A",
        "Bitte zuerst beraten",
        t0 + Duration::days(1),
    )
    .await
    .expect("Weber veto on guest application");

    let duplicate_veto = add_veto(
        &pool,
        &accepted.id,
        WEBER_A,
        "Weber A",
        "Zweites Veto darf nicht als Datenbankfehler erscheinen",
        t0 + Duration::days(2),
    )
    .await;
    assert!(matches!(duplicate_veto, Err(VetoError::AlreadyVetoed)));

    let first_phase = finalize_due_proposals(&pool, t0 + Duration::days(7))
        .await
        .expect("open voting");
    assert_eq!(first_phase[0].status, ProposalStatus::Voting);
    let voting_proposal = get_proposal(&pool, &accepted.id)
        .await
        .expect("read voting proposal")
        .expect("voting proposal");
    assert_eq!(voting_proposal.voting_until, Some(t0 + Duration::days(14)));

    let own_vote = upsert_vote(
        &pool,
        &accepted.id,
        GUEST_A,
        VoteChoice::Ja,
        t0 + Duration::days(8),
    )
    .await;
    assert!(matches!(own_vote, Err(VoteError::ActorNotEligible)));

    let guest_vote = upsert_vote(
        &pool,
        &accepted.id,
        GUEST_B,
        VoteChoice::Nein,
        t0 + Duration::days(8),
    )
    .await;
    assert!(matches!(guest_vote, Err(VoteError::ActorNotEligible)));

    let initial_vote = upsert_vote(
        &pool,
        &accepted.id,
        WEBER_B,
        VoteChoice::Nein,
        t0 + Duration::days(8),
    )
    .await
    .expect("initial Weber vote");
    assert_eq!(initial_vote, VoteWriteOutcome::Created);

    let repeated_vote = upsert_vote(
        &pool,
        &accepted.id,
        WEBER_B,
        VoteChoice::Nein,
        t0 + Duration::days(9),
    )
    .await
    .expect("identical Weber vote replay");
    assert_eq!(repeated_vote, VoteWriteOutcome::Unchanged);
    let unchanged_updated_at: chrono::DateTime<Utc> = sqlx::query_scalar(
        "SELECT updated_at FROM governance_votes \
         WHERE proposal_id = $1::uuid AND voter_account_id = $2",
    )
    .bind(&accepted.id)
    .bind(WEBER_B)
    .fetch_one(&pool)
    .await
    .expect("read unchanged vote timestamp");
    assert_eq!(unchanged_updated_at, t0 + Duration::days(8));

    let changed_vote = upsert_vote(
        &pool,
        &accepted.id,
        WEBER_B,
        VoteChoice::Ja,
        t0 + Duration::days(10),
    )
    .await
    .expect("changed Weber vote");
    assert_eq!(changed_vote, VoteWriteOutcome::Changed);
    let result = finalize_due_proposals(&pool, t0 + Duration::days(14))
        .await
        .expect("final vote");
    assert_eq!(result[0].status, ProposalStatus::Accepted);

    let tied = create_weber_proposal(&pool, GUEST_B, "Gast B", None, t0)
        .await
        .expect("create tied candidate");
    add_veto(
        &pool,
        &tied.id,
        WEBER_A,
        "Weber A",
        "Abstimmung nötig",
        t0 + Duration::days(1),
    )
    .await
    .expect("veto tied");
    finalize_due_proposals(&pool, t0 + Duration::days(7))
        .await
        .expect("open tied voting");
    upsert_vote(
        &pool,
        &tied.id,
        WEBER_A,
        VoteChoice::Ja,
        t0 + Duration::days(8),
    )
    .await
    .expect("yes");
    upsert_vote(
        &pool,
        &tied.id,
        WEBER_B,
        VoteChoice::Nein,
        t0 + Duration::days(8),
    )
    .await
    .expect("no");
    let tied_result = finalize_due_proposals(&pool, t0 + Duration::days(14))
        .await
        .expect("finalize tied");
    assert_eq!(tied_result[0].status, ProposalStatus::Rejected);

    cleanup(&pool).await;
}

#[tokio::test]
#[serial]
#[ignore = "requires direct PostgreSQL"]
async fn concurrent_first_vote_for_same_account_is_serialized() {
    let pool = pool().await;
    cleanup(&pool).await;
    seed_account(&pool, GUEST_A, "gast").await;
    seed_account(&pool, WEBER_A, "weber").await;
    seed_account(&pool, WEBER_B, "weber").await;

    let t0 = Utc.with_ymd_and_hms(2026, 7, 1, 12, 0, 0).unwrap();
    let proposal = create_weber_proposal(&pool, GUEST_A, "Gast A", None, t0)
        .await
        .expect("create candidate");
    add_veto(
        &pool,
        &proposal.id,
        WEBER_A,
        "Weber A",
        "Abstimmung nötig",
        t0 + Duration::days(1),
    )
    .await
    .expect("open second phase");
    finalize_due_proposals(&pool, t0 + Duration::days(7))
        .await
        .expect("open voting");

    let first_pool = pool.clone();
    let second_pool = pool.clone();
    let first_proposal_id = proposal.id.clone();
    let second_proposal_id = proposal.id.clone();
    let first = upsert_vote(
        &first_pool,
        &first_proposal_id,
        WEBER_B,
        VoteChoice::Nein,
        t0 + Duration::days(8),
    );
    let second = upsert_vote(
        &second_pool,
        &second_proposal_id,
        WEBER_B,
        VoteChoice::Nein,
        t0 + Duration::days(9),
    );
    let (first, second) = tokio::join!(first, second);
    let first = first.expect("first concurrent vote");
    let second = second.expect("second concurrent vote");
    assert!(matches!(
        (first, second),
        (VoteWriteOutcome::Created, VoteWriteOutcome::Unchanged)
            | (VoteWriteOutcome::Unchanged, VoteWriteOutcome::Created)
    ));

    let row_count: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM governance_votes \
         WHERE proposal_id = $1::uuid AND voter_account_id = $2",
    )
    .bind(&proposal.id)
    .bind(WEBER_B)
    .fetch_one(&pool)
    .await
    .expect("count concurrent vote rows");
    assert_eq!(row_count, 1);

    let stored_choice: String = sqlx::query_scalar(
        "SELECT choice FROM governance_votes \
         WHERE proposal_id = $1::uuid AND voter_account_id = $2",
    )
    .bind(&proposal.id)
    .bind(WEBER_B)
    .fetch_one(&pool)
    .await
    .expect("read concurrent vote");
    assert_eq!(stored_choice, "nein");

    cleanup(&pool).await;
}

#[tokio::test]
#[serial]
#[ignore = "requires direct PostgreSQL"]
async fn zero_to_zero_is_rejected_and_guest_exit_removes_identity() {
    let pool = pool().await;
    cleanup(&pool).await;
    seed_account(&pool, GUEST_C, "gast").await;
    seed_account(&pool, WEBER_A, "weber").await;

    let t0 = Utc.with_ymd_and_hms(2026, 7, 1, 12, 0, 0).unwrap();
    let proposal = create_weber_proposal(&pool, GUEST_C, DETACHED_PROOF_APPLICANT_TITLE, None, t0)
        .await
        .expect("create proposal");
    add_veto(
        &pool,
        &proposal.id,
        WEBER_A,
        "Weber A",
        "Abstimmung ohne Beteiligung",
        t0 + Duration::days(1),
    )
    .await
    .expect("veto");
    let late_message = add_message(
        &pool,
        &proposal.id,
        WEBER_A,
        "Weber A",
        "Zu spät",
        t0 + Duration::days(7),
    )
    .await;
    assert!(matches!(late_message, Err(MessageError::WrongPhase)));

    let outcomes = finalize_due_proposals(&pool, t0 + Duration::days(14))
        .await
        .expect("late sweep covers both phases");
    assert_eq!(
        outcomes.last().expect("final outcome").status,
        ProposalStatus::Rejected
    );

    let second = create_weber_proposal(
        &pool,
        GUEST_C,
        "Gast C",
        Some("späterer Antrag"),
        t0 + Duration::days(15),
    )
    .await
    .expect("new proposal after rejection");

    seed_account(&pool, GUEST_B, "gast").await;
    let foreign_proposal = create_weber_proposal(
        &pool,
        GUEST_B,
        "Gast B",
        Some("fremder Antrag"),
        t0 + Duration::days(15),
    )
    .await
    .expect("create foreign proposal");
    let retained_message = add_message(
        &pool,
        &foreign_proposal.id,
        GUEST_C,
        "Gast C",
        "Dieser Beitrag bleibt erhalten.",
        t0 + Duration::days(16),
    )
    .await
    .expect("guest contribution to foreign proposal");

    let listed = list_proposals(&pool)
        .await
        .expect("list proposal projections");
    let foreign_projection = listed
        .iter()
        .find(|candidate| candidate.id == foreign_proposal.id)
        .expect("foreign proposal in list");
    assert_eq!(foreign_projection.message_count, 1);
    let empty_projection = listed
        .iter()
        .find(|candidate| candidate.id == second.id)
        .expect("empty proposal in list");
    assert_eq!(empty_projection.message_count, 0);

    sqlx::query(
        "INSERT INTO domain_nodes \
         (id, kind, title, lat, lon, created_at, updated_at, payload) \
         VALUES ($1, 'place', 'Gastknoten', 53.5, 10.0, $2, $2, \
                 jsonb_build_object('created_by_account_id', $3::text))",
    )
    .bind(GUEST_NODE)
    .bind(t0)
    .bind(GUEST_C)
    .execute(&pool)
    .await
    .expect("seed guest node");
    sqlx::query(
        "INSERT INTO domain_edges \
         (id, source_id, target_id, edge_kind, created_at, payload) \
         VALUES ($1, $2, $3, 'reference', $4, \
                 jsonb_build_object('source_type', 'account', 'target_type', 'node'))",
    )
    .bind(GUEST_EDGE)
    .bind(GUEST_C)
    .bind(GUEST_NODE)
    .bind(t0)
    .execute(&pool)
    .await
    .expect("seed guest edge");

    delete_guest_account(&pool, GUEST_C)
        .await
        .expect("delete guest");

    let listed_after_exit = list_proposals(&pool)
        .await
        .expect("list proposal projections after guest exit");
    let retained_projection = listed_after_exit
        .iter()
        .find(|candidate| candidate.id == foreign_proposal.id)
        .expect("foreign proposal remains in list after guest exit");
    assert_eq!(retained_projection.message_count, 1);

    let account_exists: bool =
        sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM domain_accounts WHERE id = $1)")
            .bind(GUEST_C)
            .fetch_one(&pool)
            .await
            .expect("account existence");
    let proposal_exists: bool =
        sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM governance_proposals WHERE id = $1::uuid)")
            .bind(second.id)
            .fetch_one(&pool)
            .await
            .expect("proposal existence");
    let retained_procedure: (Option<String>, String, String, bool) = sqlx::query_as(
        "SELECT applicant_account_id, applicant_title, status, finalized_at IS NOT NULL
         FROM governance_proposals WHERE id = $1::uuid",
    )
    .bind(&proposal.id)
    .fetch_one(&pool)
    .await
    .expect("retained procedural history");
    let node_creator: Option<String> = sqlx::query_scalar(
        "SELECT payload ->> 'created_by_account_id' FROM domain_nodes WHERE id = $1",
    )
    .bind(GUEST_NODE)
    .fetch_one(&pool)
    .await
    .expect("retained node creator");
    let edge_exists: bool =
        sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM domain_edges WHERE id = $1)")
            .bind(GUEST_EDGE)
            .fetch_one(&pool)
            .await
            .expect("edge existence");
    let retained_author: Option<String> =
        sqlx::query_scalar("SELECT author_account_id FROM governance_messages WHERE id = $1::uuid")
            .bind(&retained_message.id)
            .fetch_one(&pool)
            .await
            .expect("retained message author");
    let retained_body: String =
        sqlx::query_scalar("SELECT body FROM governance_messages WHERE id = $1::uuid")
            .bind(&retained_message.id)
            .fetch_one(&pool)
            .await
            .expect("retained message body");
    assert!(!account_exists);
    assert!(!proposal_exists, "empty own proposal still disappears");
    assert_eq!(
        retained_procedure.0, None,
        "procedural history loses only the live applicant binding"
    );
    assert_eq!(
        retained_procedure.1, DETACHED_PROOF_APPLICANT_TITLE,
        "applicant title snapshot survives"
    );
    assert_eq!(
        retained_procedure.2, "rejected",
        "an open applicant-less procedure cannot continue"
    );
    assert!(retained_procedure.3, "detached procedure is final");
    assert_eq!(node_creator, None, "retained node must be anonymized");
    assert!(!edge_exists, "account-bound Faden must be removed");
    assert_eq!(
        retained_author, None,
        "foreign contribution must be anonymized"
    );
    assert_eq!(retained_body, "Dieser Beitrag bleibt erhalten.");

    cleanup(&pool).await;
}

#[tokio::test]
#[serial]
#[ignore = "requires direct PostgreSQL"]
async fn guest_exit_bulk_locks_and_anonymizes_many_owned_nodes() {
    const ACCOUNT: &str = "gov-proof-bulk-exit";

    let pool = pool().await;
    cleanup(&pool).await;
    seed_account(&pool, ACCOUNT, "gast").await;
    sqlx::query(
        "INSERT INTO domain_nodes \
         (id, kind, title, lat, lon, created_at, updated_at, payload) \
         SELECT 'gov-proof-bulk-node-' || series::text, 'place', 'Bulk-Knoten', \
                53.5, 10.0, NOW(), NOW(), \
                jsonb_build_object('created_by_account_id', $1::text) \
         FROM generate_series(1, 64) AS series",
    )
    .bind(ACCOUNT)
    .execute(&pool)
    .await
    .expect("seed many guest nodes");

    delete_guest_account(&pool, ACCOUNT)
        .await
        .expect("bulk guest exit");

    let retained: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM domain_nodes WHERE id LIKE 'gov-proof-bulk-node-%'",
    )
    .fetch_one(&pool)
    .await
    .expect("count retained bulk nodes");
    let still_owned: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM domain_nodes \
         WHERE id LIKE 'gov-proof-bulk-node-%' \
           AND payload ->> 'created_by_account_id' = $1",
    )
    .bind(ACCOUNT)
    .fetch_one(&pool)
    .await
    .expect("count residual bulk ownership");
    assert_eq!(retained, 64, "exit retains community-visible nodes");
    assert_eq!(still_owned, 0, "every retained node must be anonymized");

    cleanup(&pool).await;
}

#[tokio::test]
#[serial]
#[ignore = "requires direct PostgreSQL"]
async fn guest_exit_waits_for_an_inflight_owned_node_mutation() {
    const ACCOUNT: &str = "gov-proof-race-mutation-first";
    const NODE: &str = "gov-proof-race-node-mutation-first";

    let pool = pool().await;
    cleanup(&pool).await;
    seed_account(&pool, ACCOUNT, "gast").await;
    seed_guest_node(&pool, ACCOUNT, NODE).await;

    let mut mutation = pool.begin().await.expect("begin mutation");
    sqlx::query("SELECT pg_advisory_xact_lock($1::bigint)")
        .bind(node_mutation_lock_key(NODE))
        .execute(&mut *mutation)
        .await
        .expect("lock node mutation");

    let exit_pool = pool.clone();
    let exit = tokio::spawn(async move { delete_guest_account(&exit_pool, ACCOUNT).await });
    sleep(TokioDuration::from_millis(150)).await;
    assert!(
        !exit.is_finished(),
        "exit must wait for the active node mutation"
    );

    sqlx::query("UPDATE domain_nodes SET title = 'Bearbeitung gewinnt' WHERE id = $1")
        .bind(NODE)
        .execute(&mut *mutation)
        .await
        .expect("finish node mutation");
    mutation.commit().await.expect("commit node mutation");

    timeout(TokioDuration::from_secs(5), exit)
        .await
        .expect("exit completes after mutation lock release")
        .expect("exit task")
        .expect("delete guest");

    let row: (String, Option<String>) = sqlx::query_as(
        "SELECT title, payload ->> 'created_by_account_id' FROM domain_nodes WHERE id = $1",
    )
    .bind(NODE)
    .fetch_one(&pool)
    .await
    .expect("read retained node");
    assert_eq!(row.0, "Bearbeitung gewinnt");
    assert_eq!(row.1, None, "exit anonymizes after the mutation commits");

    cleanup(&pool).await;
}

#[tokio::test]
#[serial]
#[ignore = "requires direct PostgreSQL"]
async fn owned_node_mutation_waits_for_guest_exit_and_then_loses_ownership() {
    const ACCOUNT: &str = "gov-proof-race-exit-first";
    const NODE: &str = "gov-proof-race-node-exit-first";
    const EDGE: &str = "gov-proof-race-edge-exit-first";

    let pool = pool().await;
    cleanup(&pool).await;
    seed_account(&pool, ACCOUNT, "gast").await;
    seed_guest_node(&pool, ACCOUNT, NODE).await;
    sqlx::query(
        "INSERT INTO domain_edges (id, source_id, target_id, edge_kind, created_at, payload) \
         VALUES ($1, $2, $3, 'reference', NOW(), \
                 jsonb_build_object('source_type', 'account', 'target_type', 'node'))",
    )
    .bind(EDGE)
    .bind(ACCOUNT)
    .bind(NODE)
    .execute(&pool)
    .await
    .expect("seed blocking edge");

    let mut edge_blocker = pool.begin().await.expect("begin edge blocker");
    sqlx::query("SELECT id FROM domain_edges WHERE id = $1 FOR UPDATE")
        .bind(EDGE)
        .execute(&mut *edge_blocker)
        .await
        .expect("lock edge so exit remains open");

    let exit_pool = pool.clone();
    let exit = tokio::spawn(async move { delete_guest_account(&exit_pool, ACCOUNT).await });

    let key = node_mutation_lock_key(NODE);
    timeout(TokioDuration::from_secs(5), async {
        loop {
            let mut probe = pool.begin().await.expect("begin advisory probe");
            let acquired: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1::bigint)")
                .bind(key)
                .fetch_one(&mut *probe)
                .await
                .expect("probe node lock");
            probe.rollback().await.expect("rollback advisory probe");
            if !acquired {
                break;
            }
            sleep(TokioDuration::from_millis(25)).await;
        }
    })
    .await
    .expect("exit acquires node lock before edge deletion");

    let mutation_pool = pool.clone();
    let mutation = tokio::spawn(async move {
        let mut tx = mutation_pool.begin().await.expect("begin late mutation");
        sqlx::query("SELECT pg_advisory_xact_lock($1::bigint)")
            .bind(key)
            .execute(&mut *tx)
            .await
            .expect("wait for node lock");
        let affected = sqlx::query(
            "UPDATE domain_nodes SET title = 'Darf nicht gewinnen' \
             WHERE id = $1 AND payload ->> 'created_by_account_id' = $2",
        )
        .bind(NODE)
        .bind(ACCOUNT)
        .execute(&mut *tx)
        .await
        .expect("attempt ownership-bound mutation")
        .rows_affected();
        tx.commit().await.expect("commit late mutation");
        affected
    });
    sleep(TokioDuration::from_millis(150)).await;
    assert!(
        !mutation.is_finished(),
        "late mutation must wait for guest exit"
    );

    edge_blocker.commit().await.expect("release exit blocker");
    timeout(TokioDuration::from_secs(5), exit)
        .await
        .expect("exit completes")
        .expect("exit task")
        .expect("delete guest");
    let affected = timeout(TokioDuration::from_secs(5), mutation)
        .await
        .expect("late mutation completes")
        .expect("mutation task");
    assert_eq!(
        affected, 0,
        "post-exit ownership check must reject mutation"
    );

    let row: (String, Option<String>) = sqlx::query_as(
        "SELECT title, payload ->> 'created_by_account_id' FROM domain_nodes WHERE id = $1",
    )
    .bind(NODE)
    .fetch_one(&pool)
    .await
    .expect("read retained node");
    assert_eq!(row.0, "Rennknoten");
    assert_eq!(row.1, None);

    cleanup(&pool).await;
}

#[tokio::test]
#[serial]
#[ignore = "requires direct PostgreSQL"]
async fn finalization_locks_account_before_proposal() {
    const ACCOUNT: &str = "gov-proof-lock-order";

    let pool = pool().await;
    cleanup(&pool).await;
    seed_account(&pool, ACCOUNT, "gast").await;
    let t0 = Utc.with_ymd_and_hms(2026, 7, 1, 12, 0, 0).unwrap();
    let proposal = create_weber_proposal(&pool, ACCOUNT, "Lock Order", None, t0)
        .await
        .expect("create proposal");

    let mut account_blocker = pool.begin().await.expect("begin account blocker");
    sqlx::query("SELECT id FROM domain_accounts WHERE id = $1 FOR UPDATE")
        .bind(ACCOUNT)
        .execute(&mut *account_blocker)
        .await
        .expect("lock account");

    let finalizer_pool = pool.clone();
    let finalizer = tokio::spawn(async move {
        finalize_due_proposals(&finalizer_pool, t0 + Duration::days(7)).await
    });
    sleep(TokioDuration::from_millis(150)).await;
    assert!(
        !finalizer.is_finished(),
        "finalizer must wait for account lock"
    );

    let mut proposal_probe = pool.begin().await.expect("begin proposal probe");
    sqlx::query("SELECT id FROM governance_proposals WHERE id = $1::uuid FOR UPDATE NOWAIT")
        .bind(&proposal.id)
        .execute(&mut *proposal_probe)
        .await
        .expect("proposal must remain unlocked while account is blocked");
    proposal_probe
        .rollback()
        .await
        .expect("rollback proposal probe");

    account_blocker
        .commit()
        .await
        .expect("release account lock");
    let outcomes = timeout(TokioDuration::from_secs(5), finalizer)
        .await
        .expect("finalizer completes")
        .expect("finalizer task")
        .expect("finalize proposal");
    assert_eq!(outcomes.len(), 1);
    assert_eq!(outcomes[0].status, ProposalStatus::Accepted);

    cleanup(&pool).await;
}

#[tokio::test]
#[serial]
#[ignore = "requires direct PostgreSQL"]
async fn guest_exit_removes_untyped_legacy_account_edges_when_unambiguous() {
    const ACCOUNT: &str = "gov-proof-legacy-untyped-edge";
    const EDGE: &str = "gov-proof-legacy-untyped-edge-id";

    let pool = pool().await;
    cleanup(&pool).await;
    seed_account(&pool, ACCOUNT, "gast").await;
    sqlx::query(
        "INSERT INTO domain_edges (id, source_id, target_id, edge_kind, created_at, payload) \
         VALUES ($1, $2, 'legacy-target', 'reference', NOW(), '{}'::jsonb)",
    )
    .bind(EDGE)
    .bind(ACCOUNT)
    .execute(&pool)
    .await
    .expect("seed untyped legacy edge");

    delete_guest_account(&pool, ACCOUNT)
        .await
        .expect("unambiguous legacy account edge must be removable");
    let edge_exists: bool =
        sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM domain_edges WHERE id = $1)")
            .bind(EDGE)
            .fetch_one(&pool)
            .await
            .expect("check legacy edge");
    assert!(!edge_exists);

    cleanup(&pool).await;
}

#[tokio::test]
#[serial]
#[ignore = "requires direct PostgreSQL"]
async fn guest_exit_fails_closed_for_ambiguous_untyped_legacy_endpoint() {
    const ACCOUNT: &str = "gov-proof-legacy-ambiguous";
    const EDGE: &str = "gov-proof-legacy-ambiguous-edge";

    let pool = pool().await;
    cleanup(&pool).await;
    seed_account(&pool, ACCOUNT, "gast").await;
    seed_guest_node(&pool, "different-creator", ACCOUNT).await;
    sqlx::query(
        "INSERT INTO domain_edges (id, source_id, target_id, edge_kind, created_at, payload) \
         VALUES ($1, $2, 'legacy-target', 'reference', NOW(), '{}'::jsonb)",
    )
    .bind(EDGE)
    .bind(ACCOUNT)
    .execute(&pool)
    .await
    .expect("seed ambiguous untyped edge");

    let error = delete_guest_account(&pool, ACCOUNT)
        .await
        .expect_err("ambiguous endpoint must fail closed");
    assert!(error
        .to_string()
        .contains("cannot classify an untyped legacy edge endpoint"));

    let account_exists: bool =
        sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM domain_accounts WHERE id = $1)")
            .bind(ACCOUNT)
            .fetch_one(&pool)
            .await
            .expect("check rollback account");
    let edge_exists: bool =
        sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM domain_edges WHERE id = $1)")
            .bind(EDGE)
            .fetch_one(&pool)
            .await
            .expect("check rollback edge");
    assert!(
        account_exists,
        "failed exit must roll back account deletion"
    );
    assert!(
        edge_exists,
        "failed exit must leave ambiguous edge untouched"
    );

    cleanup(&pool).await;
}

#[tokio::test]
#[serial]
#[ignore = "requires direct PostgreSQL"]
async fn proposal_message_locks_author_account_before_proposal() {
    const AUTHOR: &str = "gov-proof-message-lock-author";
    const APPLICANT: &str = "gov-proof-message-lock-applicant";

    let pool = pool().await;
    cleanup(&pool).await;
    seed_account(&pool, AUTHOR, "gast").await;
    seed_account(&pool, APPLICANT, "gast").await;
    let t0 = Utc.with_ymd_and_hms(2026, 7, 1, 12, 0, 0).unwrap();
    let proposal = create_weber_proposal(&pool, APPLICANT, "Applicant", None, t0)
        .await
        .expect("create proposal");

    let mut account_blocker = pool.begin().await.expect("begin author blocker");
    sqlx::query("SELECT id FROM domain_accounts WHERE id = $1 FOR UPDATE")
        .bind(AUTHOR)
        .execute(&mut *account_blocker)
        .await
        .expect("lock author account");

    let message_pool = pool.clone();
    let proposal_id = proposal.id.clone();
    let writer = tokio::spawn(async move {
        add_message(
            &message_pool,
            &proposal_id,
            AUTHOR,
            "Author",
            "Lock-order proof",
            t0 + Duration::days(1),
        )
        .await
    });
    sleep(TokioDuration::from_millis(150)).await;
    assert!(
        !writer.is_finished(),
        "message writer must wait for author account lock"
    );

    let mut proposal_probe = pool.begin().await.expect("begin proposal probe");
    sqlx::query("SELECT id FROM governance_proposals WHERE id = $1::uuid FOR UPDATE NOWAIT")
        .bind(&proposal.id)
        .execute(&mut *proposal_probe)
        .await
        .expect("proposal must remain unlocked while author account is blocked");
    proposal_probe
        .rollback()
        .await
        .expect("rollback proposal probe");

    account_blocker
        .commit()
        .await
        .expect("release author account");
    timeout(TokioDuration::from_secs(5), writer)
        .await
        .expect("message writer completes")
        .expect("message task")
        .expect("message write");

    cleanup(&pool).await;
}

#[tokio::test]
#[serial]
#[ignore = "requires direct PostgreSQL"]
async fn public_proposals_redact_target_snapshots_after_node_visibility_changes() {
    let pool = pool().await;
    cleanup(&pool).await;
    seed_account(&pool, WEBER_A, "weber").await;
    seed_guest_node(&pool, WEBER_A, SACH_NODE).await;
    let proposal = create_sach_proposal(
        &pool,
        WEBER_A,
        "Weber A",
        "Öffentliche Abstimmung",
        None,
        Some(SACH_NODE),
        Utc.with_ymd_and_hms(2026, 10, 19, 12, 0, 0).unwrap(),
    )
    .await
    .expect("public Sachantrag for initially public node");
    assert_eq!(proposal.target_node_id.as_deref(), Some(SACH_NODE));
    assert_eq!(proposal.target_node_title.as_deref(), Some("Rennknoten"));

    for visibility in ["private", "hidden", "revoked"] {
        sqlx::query("UPDATE domain_nodes SET search_visibility = $1 WHERE id = $2")
            .bind(visibility)
            .bind(SACH_NODE)
            .execute(&pool)
            .await
            .expect("change visibility after public proposal creation");
        let detail = get_proposal(&pool, &proposal.id)
            .await
            .expect("public proposal detail")
            .expect("public proposal still exists");
        assert!(
            detail.target_node_id.is_none() && detail.target_node_title.is_none(),
            "public proposal detail exposes previously-public now-{visibility} node ID/title"
        );
        let list = list_proposals_for_viewer(&pool, None)
            .await
            .expect("anonymous proposal list");
        let entry = list
            .iter()
            .find(|entry| entry.proposal.id == proposal.id)
            .expect("public proposal stays listed");
        assert!(
            entry.proposal.target_node_id.is_none() && entry.proposal.target_node_title.is_none(),
            "anonymous proposal list exposes now-{visibility} node ID/title"
        );
    }

    // Output redaction never destroys the historical governance source.
    let stored_title: Option<String> = sqlx::query_scalar(
        "SELECT target_node_title FROM governance_proposals WHERE id = $1::uuid",
    )
    .bind(&proposal.id)
    .fetch_one(&pool)
    .await
    .expect("durable proposal snapshot");
    assert_eq!(stored_title.as_deref(), Some("Rennknoten"));

    sqlx::query("UPDATE domain_nodes SET search_visibility = 'public' WHERE id = $1")
        .bind(SACH_NODE)
        .execute(&pool)
        .await
        .expect("make node public again");
    let reopened = get_proposal(&pool, &proposal.id)
        .await
        .expect("public proposal detail")
        .expect("proposal exists");
    assert_eq!(reopened.target_node_id.as_deref(), Some(SACH_NODE));
    assert_eq!(reopened.target_node_title.as_deref(), Some("Rennknoten"));

    sqlx::query("DELETE FROM domain_nodes WHERE id = $1")
        .bind(SACH_NODE)
        .execute(&pool)
        .await
        .expect("delete formerly public node");
    let deleted = get_proposal(&pool, &proposal.id)
        .await
        .expect("public proposal detail")
        .expect("proposal remains");
    assert_eq!(deleted.target_node_id, None);
    assert_eq!(
        deleted.target_node_title, None,
        "deleted node no longer has verified public visibility"
    );
    cleanup(&pool).await;
}
