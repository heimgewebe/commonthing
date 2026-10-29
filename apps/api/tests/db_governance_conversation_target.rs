//! Direct PostgreSQL proof for the additive T018 governance conversation target.
//!
//! Run with a direct PostgreSQL connection:
//! `DATABASE_URL=postgres://... cargo test --locked --test db_governance_conversation_target -- --include-ignored`

use std::{path::PathBuf, str::FromStr};

use serial_test::serial;
use sqlx::{
    postgres::{PgConnectOptions, PgPoolOptions},
    Executor,
};

const ACCOUNT_ID: &str = "t018-governance-target-account";
const SECOND_ACCOUNT_ID: &str = "t018-governance-target-account-2";
const NODE_ID: &str = "t018-governance-target-node";
const PROPOSAL_ID: &str = "82000000-0000-4000-8000-000000000001";
const SECOND_PROPOSAL_ID: &str = "82000000-0000-4000-8000-000000000002";
const CENTER_HUB_UP_MIGRATION: &str =
    include_str!("../migrations/20260802000002_webgemeindezentrum_governance_hub.up.sql");
const CENTER_HUB_DOWN_MIGRATION: &str =
    include_str!("../migrations/20260802000002_webgemeindezentrum_governance_hub.down.sql");
const DIRECT_DOWN_MIGRATION: &str =
    include_str!("../migrations/20260730000001_private_direct_conversations.down.sql");
const DOWN_MIGRATION: &str =
    include_str!("../migrations/20260722000005_governance_conversation_target.down.sql");

fn direct_database_url() -> String {
    let url = std::env::var("DATABASE_URL")
        .expect("DATABASE_URL must point to direct PostgreSQL for this test");
    assert!(!url.contains(":6432"), "do not run through PgBouncer");
    url
}

async fn pool() -> sqlx::PgPool {
    let options = PgConnectOptions::from_str(&direct_database_url()).expect("valid DATABASE_URL");
    let pool = PgPoolOptions::new()
        .max_connections(5)
        .connect_with(options)
        .await
        .expect("connect PostgreSQL");
    let migrations = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("migrations");
    sqlx::migrate::Migrator::new(migrations)
        .await
        .expect("load migrations")
        .run(&pool)
        .await
        .expect("run migrations");
    pool
}

async fn cleanup(pool: &sqlx::PgPool) {
    sqlx::query(
        "DELETE FROM governance_proposals
         WHERE id IN ($1::uuid, $2::uuid)",
    )
    .bind(PROPOSAL_ID)
    .bind(SECOND_PROPOSAL_ID)
    .execute(pool)
    .await
    .expect("clean proposals");
    sqlx::query("DELETE FROM domain_nodes WHERE id = $1")
        .bind(NODE_ID)
        .execute(pool)
        .await
        .expect("clean node");
    sqlx::query("DELETE FROM domain_accounts WHERE id IN ($1, $2)")
        .bind(ACCOUNT_ID)
        .bind(SECOND_ACCOUNT_ID)
        .execute(pool)
        .await
        .expect("clean account");
    sqlx::query(
        "DELETE FROM domain_outbox
         WHERE aggregate_id IN (
             $1,
             $2,
             weltgewebe_governance_proposal_conversation_id($1::uuid)::text,
             weltgewebe_governance_proposal_conversation_id($2::uuid)::text,
             $3,
             weltgewebe_node_conversation_id($3)::text
         )",
    )
    .bind(PROPOSAL_ID)
    .bind(SECOND_PROPOSAL_ID)
    .bind(NODE_ID)
    .execute(pool)
    .await
    .expect("clean outbox");
}

async fn seed_account(pool: &sqlx::PgPool, account_id: &str, title: &str) {
    sqlx::query(
        "INSERT INTO domain_accounts
         (id, kind, title, map_state, radius_m, disabled, role, public_payload, private_payload)
         VALUES ($1, 'garnrolle', $2, 'not_on_map', 0, FALSE,
                 'gast', '{}', '{}')",
    )
    .bind(account_id)
    .bind(title)
    .execute(pool)
    .await
    .expect("seed account");
}

async fn insert_proposal(pool: &sqlx::PgPool, proposal_id: &str, account_id: &str) {
    sqlx::query(
        "INSERT INTO governance_proposals (
             id, kind, webgemeindezentrum_id, applicant_account_id, applicant_title, summary, status,
             created_at, consent_until
         ) VALUES (
             $1::uuid, 'weberantrag', 'webgemeindezentrum-hammer-park', $2, 'T018 Antragsteller', 'Schema-Beweis',
             'consent', TIMESTAMPTZ '2026-07-20 12:00:00+00',
             TIMESTAMPTZ '2026-07-27 12:00:00+00'
         )",
    )
    .bind(proposal_id)
    .bind(account_id)
    .execute(pool)
    .await
    .expect("insert proposal");
}

#[tokio::test]
#[serial]
#[ignore = "requires direct PostgreSQL"]
async fn center_hub_migration_accepts_multiple_active_centers_without_legacy_proposals() {
    let pool = pool().await;
    let mut tx = pool
        .begin()
        .await
        .expect("begin multi-center migration proof");

    let legacy_proposals: i64 = sqlx::query_scalar("SELECT count(*) FROM governance_proposals")
        .fetch_one(&mut *tx)
        .await
        .expect("count legacy proposals");
    assert_eq!(
        legacy_proposals, 0,
        "multi-center migration proof requires an empty legacy proposal table"
    );

    tx.execute(CENTER_HUB_DOWN_MIGRATION)
        .await
        .expect("remove center hub before multi-center proof");
    let baseline_centers: i64 =
        sqlx::query_scalar("SELECT count(*)::bigint FROM webgemeindezentren")
            .fetch_one(&mut *tx)
            .await
            .expect("count centers before multi-center proof");
    assert!(
        baseline_centers >= 1,
        "the canonical first center must exist before adding a second one"
    );

    let suffix = uuid::Uuid::new_v4().simple().to_string();
    let cell_id = format!("proof-{suffix}.weltgewebe.net");
    let weaving_id = format!("ortsweberei-proof-{suffix}");
    let center_id = format!("webgemeindezentrum-proof-{suffix}");
    let slug = format!("proof-{suffix}");

    sqlx::query(
        "INSERT INTO gewebezellen (id, lifecycle_state, created_at, updated_at)
         VALUES ($1, 'active', NOW(), NOW())",
    )
    .bind(&cell_id)
    .execute(&mut *tx)
    .await
    .expect("insert second Gewebezelle");
    sqlx::query(
        "INSERT INTO ortswebereien (
             id, slug, name, description, gewebezelle_id, lifecycle_state,
             active_webgemeindezentrum_id, created_at, updated_at
         ) VALUES (
             $1, $2, 'Ortsweberei Proof',
             'Zweite aktive Ortsweberei für den Migrationsbeweis.',
             $3, 'active', $4, NOW(), NOW()
         )",
    )
    .bind(&weaving_id)
    .bind(&slug)
    .bind(&cell_id)
    .bind(&center_id)
    .execute(&mut *tx)
    .await
    .expect("insert second Ortsweberei");
    sqlx::query(
        "INSERT INTO webgemeindezentren (
             id, ortsweberei_id, name, location_state, lat, lon,
             location_label, meeting_note, access_note, created_at, updated_at
         ) VALUES (
             $1, $2, 'Webgemeindezentrum Proof', 'desired', 53.55, 10.05,
             'Migrationsbeweis', 'Gemeinsamer Treffpunkt für den Migrationsbeweis.',
             'Nur Testdaten.', NOW(), NOW()
         )",
    )
    .bind(&center_id)
    .bind(&weaving_id)
    .execute(&mut *tx)
    .await
    .expect("insert second Webgemeindezentrum");

    tx.execute(CENTER_HUB_UP_MIGRATION).await.expect(
        "center hub migration must accept multiple active centers without legacy proposals",
    );
    let center_contract: (i64, i64) = sqlx::query_as(
        "SELECT count(*)::bigint, count(DISTINCT faden_endpoint_id)::bigint
         FROM webgemeindezentren",
    )
    .fetch_one(&mut *tx)
    .await
    .expect("inspect multi-center Faden endpoints");
    assert_eq!(
        center_contract,
        (baseline_centers + 1, baseline_centers + 1)
    );
    let center_conversations: i64 = sqlx::query_scalar(
        "SELECT count(*)::bigint FROM domain_conversations
         WHERE conversation_type = 'webgemeindezentrum'",
    )
    .fetch_one(&mut *tx)
    .await
    .expect("inspect multi-center conversations");
    assert_eq!(center_conversations, baseline_centers + 1);

    tx.rollback().await.expect("rollback multi-center proof");
    pool.close().await;
}

#[tokio::test]
#[serial]
#[ignore = "requires direct PostgreSQL"]
async fn governance_cutover_flip_waits_for_inflight_writer_gate() {
    let pool = pool().await;
    sqlx::query(
        "UPDATE domain_conversation_cutover_state SET governance_source = 'canonical', updated_at = NOW() WHERE singleton",
    )
    .execute(&pool)
    .await
    .expect("enable canonical source for lock proof");

    let mut writer_tx = pool.begin().await.expect("begin writer gate proof");
    let source: String = sqlx::query_scalar(
        "SELECT governance_source FROM domain_conversation_cutover_state WHERE singleton FOR SHARE",
    )
    .fetch_one(&mut *writer_tx)
    .await
    .expect("lock canonical cutover source like the runtime write gate");
    assert_eq!(source, "canonical");

    let mut flip_tx = pool.begin().await.expect("begin concurrent cutover flip");
    sqlx::query("SET LOCAL lock_timeout = '100ms'")
        .execute(&mut *flip_tx)
        .await
        .expect("set bounded lock timeout");
    let blocked_flip = sqlx::query(
        "UPDATE domain_conversation_cutover_state SET governance_source = 'legacy', updated_at = NOW() WHERE singleton",
    )
    .execute(&mut *flip_tx)
    .await;
    assert!(
        blocked_flip.is_err(),
        "legacy cutover must wait while an authorized writer holds the shared gate lock"
    );
    flip_tx
        .rollback()
        .await
        .expect("rollback blocked cutover flip");
    writer_tx
        .rollback()
        .await
        .expect("release writer gate lock");

    sqlx::query(
        "UPDATE domain_conversation_cutover_state SET governance_source = 'legacy', updated_at = NOW() WHERE singleton",
    )
    .execute(&pool)
    .await
    .expect("restore legacy cutover source");
    pool.close().await;
}

#[tokio::test]
#[serial]
#[ignore = "requires direct PostgreSQL"]
async fn governance_down_guard_blocks_concurrent_cutover_flip() {
    let pool = pool().await;
    sqlx::query(
        "UPDATE domain_conversation_cutover_state SET governance_source = 'legacy', updated_at = NOW() WHERE singleton",
    )
    .execute(&pool)
    .await
    .expect("restore legacy source before rollback guard proof");

    let guard_end = DOWN_MIGRATION
        .find("$$;\n\nDROP TRIGGER")
        .expect("down migration starts with a standalone guard block")
        + 3;
    let down_guard = &DOWN_MIGRATION[..guard_end];

    let mut down_tx = pool.begin().await.expect("begin rollback guard proof");
    down_tx
        .execute(down_guard)
        .await
        .expect("execute exact rollback guard from down migration");

    let mut flip_tx = pool
        .begin()
        .await
        .expect("begin concurrent cutover flip proof");
    sqlx::query("SET LOCAL lock_timeout = '100ms'")
        .execute(&mut *flip_tx)
        .await
        .expect("set bounded cutover lock timeout");
    let blocked_flip = sqlx::query(
        "UPDATE domain_conversation_cutover_state SET governance_source = 'canonical', updated_at = NOW() WHERE singleton",
    )
    .execute(&mut *flip_tx)
    .await;
    assert!(
        blocked_flip.is_err(),
        "cutover flip must wait while the exact down-migration guard holds the singleton row lock"
    );
    flip_tx
        .rollback()
        .await
        .expect("rollback blocked concurrent cutover flip");
    down_tx
        .rollback()
        .await
        .expect("release rollback guard row lock");
    pool.close().await;
}

#[tokio::test]
#[serial]
#[ignore = "requires direct PostgreSQL"]
async fn governance_conversation_target_is_additive_deterministic_and_reversible() {
    let pool = pool().await;
    cleanup(&pool).await;
    seed_account(&pool, ACCOUNT_ID, "T018 Antragsteller").await;
    seed_account(&pool, SECOND_ACCOUNT_ID, "T018 Zweitantragsteller").await;

    let cutover_source: String = sqlx::query_scalar(
        "SELECT governance_source
         FROM domain_conversation_cutover_state
         WHERE singleton",
    )
    .fetch_one(&pool)
    .await
    .expect("read cutover source");
    assert_eq!(cutover_source, "legacy");

    let invalid_cutover = sqlx::query(
        "UPDATE domain_conversation_cutover_state
         SET governance_source = 'unknown', updated_at = NOW()
         WHERE singleton",
    )
    .execute(&pool)
    .await;
    assert!(
        invalid_cutover.is_err(),
        "unknown cutover sources must be rejected"
    );

    let (proposal_index_unique, proposal_index_predicate): (bool, Option<String>) = sqlx::query_as(
        "SELECT index_state.indisunique, pg_get_expr(index_state.indpred, index_state.indrelid)
             FROM pg_index AS index_state
             JOIN pg_class AS index_class ON index_class.oid = index_state.indexrelid
             WHERE index_class.relname = 'domain_conversations_one_per_governance_proposal'",
    )
    .fetch_one(&pool)
    .await
    .expect("inspect governance proposal conversation index");
    assert!(proposal_index_unique);
    assert!(
        proposal_index_predicate.is_none(),
        "proposal_id must have a non-partial unique index so foreign-key cascades can use it"
    );

    let projection_before: i64 =
        sqlx::query_scalar("SELECT version FROM domain_projection_state WHERE singleton")
            .fetch_one(&pool)
            .await
            .expect("projection before proposal");

    insert_proposal(&pool, PROPOSAL_ID, ACCOUNT_ID).await;

    let (conversation_id, deterministic_id, node_id, proposal_id, kind, count): (
        String,
        String,
        Option<String>,
        Option<String>,
        String,
        i64,
    ) = sqlx::query_as(
        "SELECT min(id::text),
                weltgewebe_governance_proposal_conversation_id($1::uuid)::text,
                min(node_id), min(proposal_id::text), min(conversation_type), count(*)
         FROM domain_conversations
         WHERE proposal_id = $1::uuid",
    )
    .bind(PROPOSAL_ID)
    .fetch_one(&pool)
    .await
    .expect("read generated governance conversation");
    assert_eq!(conversation_id, deterministic_id);
    let deterministic_uuid = uuid::Uuid::parse_str(&deterministic_id).expect("deterministic UUID");
    assert_eq!(
        deterministic_uuid.get_version_num(),
        3,
        "MD5-backed deterministic conversation ids must advertise RFC UUID version 3"
    );
    assert!(node_id.is_none());
    assert_eq!(proposal_id.as_deref(), Some(PROPOSAL_ID));
    assert_eq!(kind, "governance_proposal");
    assert_eq!(count, 1);

    let projection_after: i64 =
        sqlx::query_scalar("SELECT version FROM domain_projection_state WHERE singleton")
            .fetch_one(&pool)
            .await
            .expect("projection after proposal");
    assert_eq!(
        projection_after, projection_before,
        "proposal conversation creation must not invalidate the map projection"
    );

    let conversation_events: i64 = sqlx::query_scalar(
        "SELECT count(*)
         FROM domain_outbox
         WHERE aggregate_type = 'conversation'
           AND aggregate_id = $1
           AND event_type = 'domain.conversation.created'",
    )
    .bind(&conversation_id)
    .fetch_one(&pool)
    .await
    .expect("count conversation events");
    assert_eq!(conversation_events, 1);

    let duplicate_subject = sqlx::query(
        "INSERT INTO domain_conversations (
             id, proposal_id, conversation_type, visibility
         ) VALUES (
             '82000000-0000-4000-8000-000000000099'::uuid,
             $1::uuid, 'governance_proposal', 'public'
         )",
    )
    .bind(PROPOSAL_ID)
    .execute(&pool)
    .await;
    assert!(
        duplicate_subject.is_err(),
        "each governance proposal must own exactly one conversation"
    );

    sqlx::query(
        "INSERT INTO domain_nodes (id, kind, title, created_at, updated_at, payload)
         VALUES ($1, 'place', 'T018 Knotenkontrolle', NOW(), NOW(), '{}')",
    )
    .bind(NODE_ID)
    .execute(&pool)
    .await
    .expect("insert control node");

    let (node_conversation_id, node_deterministic_id, node_proposal, node_kind): (
        String,
        String,
        Option<String>,
        String,
    ) = sqlx::query_as(
        "SELECT id::text, weltgewebe_node_conversation_id($1)::text,
                proposal_id::text, conversation_type
         FROM domain_conversations
         WHERE node_id = $1",
    )
    .bind(NODE_ID)
    .fetch_one(&pool)
    .await
    .expect("read node control conversation");
    assert_eq!(node_conversation_id, node_deterministic_id);
    assert!(node_proposal.is_none());
    assert_eq!(node_kind, "node");

    let invalid_no_subject = sqlx::query(
        "INSERT INTO domain_conversations (
             id, node_id, proposal_id, conversation_type, visibility
         ) VALUES (
             '82000000-0000-4000-8000-000000000098'::uuid,
             NULL, NULL, 'governance_proposal', 'public'
         )",
    )
    .execute(&pool)
    .await;
    assert!(
        invalid_no_subject.is_err(),
        "a conversation must have one subject"
    );

    let invalid_two_subjects = sqlx::query(
        "INSERT INTO domain_conversations (
             id, node_id, proposal_id, conversation_type, visibility
         ) VALUES (
             '82000000-0000-4000-8000-000000000097'::uuid,
             $1, $2::uuid, 'node', 'public'
         )",
    )
    .bind(NODE_ID)
    .bind(PROPOSAL_ID)
    .execute(&pool)
    .await;
    assert!(
        invalid_two_subjects.is_err(),
        "a conversation must never bind node and proposal simultaneously"
    );

    // Simulate a proposal that existed before the additive target migration:
    // remove its empty target, clear its target events, and reconstruct it from
    // governance_proposals with the exact migration backfill statement.
    sqlx::query("DELETE FROM domain_conversations WHERE id = $1::uuid")
        .bind(&conversation_id)
        .execute(&pool)
        .await
        .expect("remove empty target before backfill proof");
    sqlx::query(
        "DELETE FROM domain_outbox
         WHERE aggregate_type = 'conversation' AND aggregate_id = $1",
    )
    .bind(&conversation_id)
    .execute(&pool)
    .await
    .expect("clear target events before backfill proof");

    let backfill = "INSERT INTO domain_conversations (
             id, node_id, proposal_id, conversation_type, visibility, created_at, updated_at
         )
         SELECT weltgewebe_governance_proposal_conversation_id(id), NULL, id,
                'governance_proposal', 'public', created_at, created_at
         FROM governance_proposals
         WHERE id = $1::uuid
         ON CONFLICT (proposal_id)
         DO NOTHING";
    sqlx::query(backfill)
        .bind(PROPOSAL_ID)
        .execute(&pool)
        .await
        .expect("reconstruct deterministic proposal conversation");

    let count_after_backfill: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM domain_conversations WHERE proposal_id = $1::uuid",
    )
    .bind(PROPOSAL_ID)
    .fetch_one(&pool)
    .await
    .expect("count conversations after backfill");
    assert_eq!(count_after_backfill, 1);
    let events_after_backfill: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM domain_outbox
         WHERE aggregate_type = 'conversation' AND aggregate_id = $1",
    )
    .bind(&conversation_id)
    .fetch_one(&pool)
    .await
    .expect("count events after backfill");
    assert_eq!(events_after_backfill, 1);

    // A second run is idempotent: no duplicate row and no duplicate event.
    sqlx::query(backfill)
        .bind(PROPOSAL_ID)
        .execute(&pool)
        .await
        .expect("repeat deterministic backfill");
    let count_after_replay: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM domain_conversations WHERE proposal_id = $1::uuid",
    )
    .bind(PROPOSAL_ID)
    .fetch_one(&pool)
    .await
    .expect("count conversations after replay");
    assert_eq!(count_after_replay, 1);
    let events_after_replay: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM domain_outbox
         WHERE aggregate_type = 'conversation' AND aggregate_id = $1",
    )
    .bind(&conversation_id)
    .fetch_one(&pool)
    .await
    .expect("count events after replay");
    assert_eq!(events_after_replay, 1);

    insert_proposal(&pool, SECOND_PROPOSAL_ID, SECOND_ACCOUNT_ID).await;
    let second_count: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM domain_conversations WHERE proposal_id = $1::uuid",
    )
    .bind(SECOND_PROPOSAL_ID)
    .fetch_one(&pool)
    .await
    .expect("count second proposal conversation");
    assert_eq!(second_count, 1);

    // Empty targets may follow proposal deletion. The transaction is rolled
    // back so the remaining rollback proofs keep their complete fixture set.
    let mut empty_delete_tx = pool.begin().await.expect("begin empty delete proof");
    sqlx::query("DELETE FROM governance_proposals WHERE id = $1::uuid")
        .bind(SECOND_PROPOSAL_ID)
        .execute(&mut *empty_delete_tx)
        .await
        .expect("delete proposal with empty target");
    let empty_target_remains: bool = sqlx::query_scalar(
        "SELECT EXISTS (
             SELECT 1 FROM domain_conversations WHERE proposal_id = $1::uuid
         )",
    )
    .bind(SECOND_PROPOSAL_ID)
    .fetch_one(&mut *empty_delete_tx)
    .await
    .expect("inspect empty target cascade");
    assert!(!empty_target_remains);
    empty_delete_tx
        .rollback()
        .await
        .expect("restore empty proposal fixture");

    // Once a canonical contribution exists, proposal deletion must not cascade
    // away public history.
    let mut protected_delete_tx = pool.begin().await.expect("begin protected delete proof");
    sqlx::query(
        "INSERT INTO domain_messages (
             id, conversation_id, author_account_id, author_title, content, idempotency_key
         ) VALUES (
             '82000000-0000-4000-8000-000000000092'::uuid,
             $1::uuid, $2, 'T018 Antragsteller', 'Öffentliche Geschichte bewahren',
             '82000000-0000-4000-8000-000000000093'::uuid
         )",
    )
    .bind(&conversation_id)
    .bind(ACCOUNT_ID)
    .execute(&mut *protected_delete_tx)
    .await
    .expect("insert protected governance message");
    let protected_delete = sqlx::query("DELETE FROM governance_proposals WHERE id = $1::uuid")
        .bind(PROPOSAL_ID)
        .execute(&mut *protected_delete_tx)
        .await;
    assert!(
        protected_delete.is_err(),
        "proposal deletion must not erase canonical conversation history"
    );
    protected_delete_tx
        .rollback()
        .await
        .expect("rollback protected delete proof");

    // The down migration refuses a completed cutover.
    let mut canonical_tx = pool.begin().await.expect("begin canonical rollback proof");
    sqlx::query(
        "UPDATE domain_conversation_cutover_state
         SET governance_source = 'canonical', updated_at = NOW()
         WHERE singleton",
    )
    .execute(&mut *canonical_tx)
    .await
    .expect("set canonical marker in transaction");
    canonical_tx
        .execute(CENTER_HUB_DOWN_MIGRATION)
        .await
        .expect("remove the later center hub before release-A rollback proof");
    canonical_tx
        .execute(DIRECT_DOWN_MIGRATION)
        .await
        .expect("remove the later direct-conversation migration before release-A rollback proof");
    let canonical_down = canonical_tx.execute(DOWN_MIGRATION).await;
    assert!(
        canonical_down.is_err(),
        "down migration must reject a canonical cutover marker"
    );
    canonical_tx
        .rollback()
        .await
        .expect("rollback canonical proof");

    // The down migration also refuses any canonical governance message.
    let mut message_tx = pool.begin().await.expect("begin message rollback proof");
    sqlx::query(
        "INSERT INTO domain_messages (
             id, conversation_id, author_account_id, author_title, content, idempotency_key
         ) VALUES (
             '82000000-0000-4000-8000-000000000090'::uuid,
             $1::uuid, $2, 'T018 Antragsteller', 'Noch nicht umschalten',
             '82000000-0000-4000-8000-000000000091'::uuid
         )",
    )
    .bind(&conversation_id)
    .bind(ACCOUNT_ID)
    .execute(&mut *message_tx)
    .await
    .expect("insert canonical message in transaction");
    message_tx
        .execute(CENTER_HUB_DOWN_MIGRATION)
        .await
        .expect("remove the later center hub before release-A message proof");
    message_tx
        .execute(DIRECT_DOWN_MIGRATION)
        .await
        .expect("remove the later direct-conversation migration before release-A message proof");
    let message_down = message_tx.execute(DOWN_MIGRATION).await;
    assert!(
        message_down.is_err(),
        "down migration must reject canonical governance messages"
    );
    message_tx.rollback().await.expect("rollback message proof");

    // Before cutover and without canonical messages, the down migration restores
    // the node-only schema. Roll the proof transaction back afterwards so the
    // shared test database remains on the latest migration.
    let mut down_tx = pool.begin().await.expect("begin successful down proof");
    down_tx
        .execute(CENTER_HUB_DOWN_MIGRATION)
        .await
        .expect("remove the later center hub before release-A rollback");
    down_tx
        .execute(DIRECT_DOWN_MIGRATION)
        .await
        .expect("remove the later direct-conversation migration before release-A rollback");
    down_tx
        .execute(DOWN_MIGRATION)
        .await
        .expect("release-A down migration before cutover");
    let proposal_column_exists: bool = sqlx::query_scalar(
        "SELECT EXISTS (
             SELECT 1 FROM information_schema.columns
             WHERE table_schema = current_schema()
               AND table_name = 'domain_conversations'
               AND column_name = 'proposal_id'
         )",
    )
    .fetch_one(&mut *down_tx)
    .await
    .expect("inspect proposal column after down");
    assert!(!proposal_column_exists);
    let node_nullable: String = sqlx::query_scalar(
        "SELECT is_nullable FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name = 'domain_conversations'
           AND column_name = 'node_id'",
    )
    .fetch_one(&mut *down_tx)
    .await
    .expect("inspect node nullability after down");
    assert_eq!(node_nullable, "NO");
    let compensating_delete_events: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM domain_outbox
         WHERE aggregate_type = 'conversation'
           AND aggregate_id = $1
           AND event_type = 'domain.conversation.deleted'",
    )
    .bind(&conversation_id)
    .fetch_one(&mut *down_tx)
    .await
    .expect("count compensating conversation delete events after down");
    assert_eq!(
        compensating_delete_events, 1,
        "rollback must retain a compensating delete event for downstream consumers"
    );
    let created_events_still_present: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM domain_outbox
         WHERE aggregate_type = 'conversation'
           AND aggregate_id = $1
           AND event_type = 'domain.conversation.created'",
    )
    .bind(&conversation_id)
    .fetch_one(&mut *down_tx)
    .await
    .expect("count retained conversation create events after down");
    assert!(
        created_events_still_present >= 1,
        "rollback must not erase event history that may already have been published"
    );
    down_tx.rollback().await.expect("restore latest schema");

    cleanup(&pool).await;
    pool.close().await;
}
