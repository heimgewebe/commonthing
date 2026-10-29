//! Integration proof: domain schema migrations (nodes, edges, accounts).
//!
//! Verifies that Phase B migrations for OPT-ARC-001 create domain tables and
//! indexes correctly, and that basic structural inserts and constraints hold.
//! In particular, the accounts schema intentionally keeps email non-unique in
//! Phase B to remain compatible with current JSONL/runtime tolerance until
//! Phase C duplicate-audit/quarantine policy is implemented.
//! Does NOT verify down-migration / DROP behaviour — that requires a
//! disposable database and is deferred to a dedicated revert-proof gate.
//! No runtime cutover is tested here — JSONL remains the active data source
//! until Phase D/E.
//!
//! Run with:
//!   DATABASE_URL=postgres://welt:gewebe@localhost:5432/weltgewebe \
//!     cargo test --locked -p weltgewebe-api --test db_domain_schema_migrations -- --include-ignored
//!
//! Notes:
//! - Tests are ignored by default to keep offline paths green.
//! - DATABASE_URL must point to direct PostgreSQL (not PgBouncer at :6432).

mod support;

use std::{path::PathBuf, str::FromStr};

use sqlx::postgres::{PgConnectOptions, PgPoolOptions};

fn direct_database_url() -> String {
    let url = std::env::var("DATABASE_URL").expect(
        "DATABASE_URL must be set to run db_domain_schema_migrations tests; \
         point it to direct PostgreSQL (port 5432)",
    );

    assert!(
        !url.contains(":6432"),
        "DATABASE_URL must target direct PostgreSQL, not PgBouncer (port 6432)"
    );

    support::postgres_proof::validated_direct_disposable_url(url)
}

async fn connect_pool() -> sqlx::PgPool {
    let connect_opts = PgConnectOptions::from_str(&direct_database_url())
        .expect("DATABASE_URL must be a valid postgres connection string");

    PgPoolOptions::new()
        .max_connections(2)
        .connect_with(connect_opts)
        .await
        .expect("failed to connect to direct PostgreSQL")
}

async fn run_migrations(pool: &sqlx::PgPool) {
    let migrations_dir: PathBuf = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("migrations");

    let migrator = sqlx::migrate::Migrator::new(migrations_dir)
        .await
        .expect("failed to load migrations");

    migrator.run(pool).await.expect("failed to run migrations");
}

async fn table_exists(pool: &sqlx::PgPool, table_name: &str) -> bool {
    let row: (bool,) = sqlx::query_as(
        "SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public'
            AND table_name = $1
        )",
    )
    .bind(table_name)
    .fetch_one(pool)
    .await
    .expect("failed to query information_schema.tables");

    row.0
}

async fn index_exists(pool: &sqlx::PgPool, index_name: &str) -> bool {
    let row: (bool,) = sqlx::query_as(
        "SELECT EXISTS (
            SELECT 1 FROM pg_indexes
            WHERE schemaname = 'public'
            AND indexname = $1
        )",
    )
    .bind(index_name)
    .fetch_one(pool)
    .await
    .expect("failed to query pg_indexes");

    row.0
}

async fn column_exists(pool: &sqlx::PgPool, table_name: &str, column_name: &str) -> bool {
    let row: (bool,) = sqlx::query_as(
        r#"
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = $1
              AND column_name = $2
        )
        "#,
    )
    .bind(table_name)
    .bind(column_name)
    .fetch_one(pool)
    .await
    .expect("failed to query information_schema.columns");

    row.0
}

async fn constraint_exists(pool: &sqlx::PgPool, constraint_name: &str) -> bool {
    let row: (bool,) = sqlx::query_as(
        r#"
        SELECT EXISTS (
            SELECT 1
            FROM pg_constraint
            WHERE connamespace = 'public'::regnamespace
              AND conname = $1
        )
        "#,
    )
    .bind(constraint_name)
    .fetch_one(pool)
    .await
    .expect("failed to query pg_constraint");

    row.0
}

/// Verifies that running all migrations creates the domain tables and
/// expected indexes for nodes, edges, and accounts.
///
/// This is a schema-presence proof: it does not insert or query domain data.
/// No runtime read/write paths are tested here.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
async fn domain_schema_tables_exist_after_migration() {
    let pool = connect_pool().await;
    run_migrations(&pool).await;

    // --- domain_nodes ---
    assert!(
        table_exists(&pool, "domain_nodes").await,
        "domain_nodes table must exist after migration"
    );
    assert!(
        index_exists(&pool, "domain_nodes_kind").await,
        "domain_nodes_kind index must exist"
    );
    assert!(
        index_exists(&pool, "domain_nodes_lat_lon").await,
        "domain_nodes_lat_lon index must exist"
    );
    assert!(
        column_exists(&pool, "domain_nodes", "create_actor_id").await,
        "domain_nodes.create_actor_id must exist"
    );
    assert!(
        column_exists(&pool, "domain_nodes", "create_operation_id").await,
        "domain_nodes.create_operation_id must exist"
    );
    assert!(
        index_exists(&pool, "domain_nodes_create_operation_unique").await,
        "domain_nodes create-operation unique index must exist"
    );
    assert!(
        constraint_exists(&pool, "domain_nodes_create_operation_pair").await,
        "domain_nodes create-operation pair check must exist"
    );

    // --- domain_edges ---
    assert!(
        table_exists(&pool, "domain_edges").await,
        "domain_edges table must exist after migration"
    );
    assert!(
        index_exists(&pool, "domain_edges_source_id").await,
        "domain_edges_source_id index must exist"
    );
    assert!(
        index_exists(&pool, "domain_edges_target_id").await,
        "domain_edges_target_id index must exist"
    );
    assert!(
        index_exists(&pool, "domain_edges_source_target").await,
        "domain_edges_source_target index must exist"
    );
    assert!(
        column_exists(&pool, "domain_edges", "create_actor_id").await,
        "domain_edges.create_actor_id must exist"
    );
    assert!(
        column_exists(&pool, "domain_edges", "create_operation_id").await,
        "domain_edges.create_operation_id must exist"
    );
    assert!(
        index_exists(&pool, "domain_edges_create_operation_unique").await,
        "domain_edges create-operation unique index must exist"
    );
    assert!(
        constraint_exists(&pool, "domain_edges_create_operation_pair").await,
        "domain_edges create-operation pair check must exist"
    );

    // --- domain_accounts ---
    assert!(
        table_exists(&pool, "domain_accounts").await,
        "domain_accounts table must exist after migration"
    );
    assert!(
        index_exists(&pool, "domain_accounts_email_lookup").await,
        "domain_accounts_email_lookup index must exist"
    );
    assert!(
        !column_exists(&pool, "domain_accounts", "mode").await,
        "removed domain_accounts.mode column must not exist"
    );

    // --- Webgemeindezentrum governance hub ---
    assert!(
        column_exists(&pool, "webgemeindezentren", "faden_endpoint_id").await,
        "Webgemeindezentrum must expose a strict UUID Faden endpoint"
    );
    assert!(
        column_exists(&pool, "governance_proposals", "webgemeindezentrum_id").await,
        "every governance proposal must be anchored to a Webgemeindezentrum"
    );
    assert!(
        column_exists(&pool, "governance_proposals", "title").await,
        "Sachantraege must persist their decision title"
    );
    assert!(
        column_exists(&pool, "governance_proposals", "target_node_id").await,
        "Sachantraege must support an optional live node reference"
    );
    assert!(
        column_exists(&pool, "governance_proposals", "target_node_title").await,
        "Sachantraege must preserve the node title snapshot"
    );
    assert!(
        index_exists(&pool, "governance_proposals_center_kind_status").await,
        "center governance lists need a kind/status index"
    );
    assert!(
        index_exists(&pool, "governance_proposals_target_node").await,
        "node panels need an indexed proposal lookup"
    );
    assert!(
        constraint_exists(&pool, "governance_proposals_kind_fields_check").await,
        "proposal kinds must enforce their valid field combinations"
    );
    assert!(
        column_exists(&pool, "domain_conversations", "webgemeindezentrum_id").await,
        "center conversations must have an explicit subject"
    );
    assert!(
        index_exists(&pool, "domain_conversations_one_per_webgemeindezentrum").await,
        "each Webgemeindezentrum must have one canonical conversation"
    );
    let center_hub: (bool, bool, i64) = sqlx::query_as(
        "SELECT \
             c.faden_endpoint_id IS NOT NULL, \
             center_conversation.id IS NOT NULL, \
             count(proposal.id)::bigint \
         FROM webgemeindezentren c \
         LEFT JOIN domain_conversations center_conversation \
           ON center_conversation.webgemeindezentrum_id = c.id \
          AND center_conversation.conversation_type = 'webgemeindezentrum' \
         LEFT JOIN governance_proposals proposal \
           ON proposal.webgemeindezentrum_id = c.id \
         WHERE c.id = 'webgemeindezentrum-hammer-park' \
         GROUP BY c.faden_endpoint_id, center_conversation.id",
    )
    .fetch_one(&pool)
    .await
    .expect("failed to verify the first Webgemeindezentrum governance hub");
    assert!(center_hub.0, "center Faden endpoint must be populated");
    assert!(center_hub.1, "center conversation must be populated");
    assert_eq!(
        center_hub.2, 0,
        "fresh schema must not invent governance proposals"
    );

    // --- private collective node mutation audit ---
    assert!(
        table_exists(&pool, "domain_node_mutation_audit").await,
        "domain_node_mutation_audit table must exist"
    );
    for column in [
        "operation_id",
        "node_id",
        "actor_subject_hash",
        "operation",
        "before_hash",
        "after_hash",
        "occurred_at",
    ] {
        assert!(
            column_exists(&pool, "domain_node_mutation_audit", column).await,
            "domain_node_mutation_audit.{column} must exist"
        );
    }
    assert!(
        index_exists(&pool, "domain_node_mutation_audit_node_time").await,
        "node mutation audit node/time index must exist"
    );

    pool.close().await;
}

#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
async fn node_mutation_audit_schema_accepts_only_privacy_reviewed_shapes() {
    let pool = connect_pool().await;
    run_migrations(&pool).await;

    sqlx::query("DELETE FROM domain_node_mutation_audit WHERE node_id LIKE 'schema-audit-%'")
        .execute(&pool)
        .await
        .expect("pre-test audit cleanup failed");

    let hash = "a".repeat(64);
    sqlx::query(
        "INSERT INTO domain_node_mutation_audit (             operation_id, node_id, actor_subject_hash, operation, before_hash, after_hash, occurred_at         ) VALUES ($1::uuid, 'schema-audit-replace', $2, 'replace', $2, $2, NOW()),                   ($3::uuid, 'schema-audit-delete', $2, 'delete', $2, NULL, NOW())",
    )
    .bind(uuid::Uuid::new_v4().to_string())
    .bind(&hash)
    .bind(uuid::Uuid::new_v4().to_string())
    .execute(&pool)
    .await
    .expect("valid replace/delete audit rows must be accepted");

    let invalid_operation = sqlx::query(
        "INSERT INTO domain_node_mutation_audit (             operation_id, node_id, actor_subject_hash, operation, before_hash, after_hash, occurred_at         ) VALUES ($1::uuid, 'schema-audit-invalid-operation', $2, 'patch', $2, $2, NOW())",
    )
    .bind(uuid::Uuid::new_v4().to_string())
    .bind(&hash)
    .execute(&pool)
    .await;
    assert!(
        invalid_operation.is_err(),
        "patch is not a persisted audit operation"
    );

    let invalid_delete_after = sqlx::query(
        "INSERT INTO domain_node_mutation_audit (             operation_id, node_id, actor_subject_hash, operation, before_hash, after_hash, occurred_at         ) VALUES ($1::uuid, 'schema-audit-invalid-delete', $2, 'delete', $2, $2, NOW())",
    )
    .bind(uuid::Uuid::new_v4().to_string())
    .bind(&hash)
    .execute(&pool)
    .await;
    assert!(
        invalid_delete_after.is_err(),
        "delete audit must not retain an after hash"
    );

    let invalid_actor_hash = sqlx::query(
        "INSERT INTO domain_node_mutation_audit (             operation_id, node_id, actor_subject_hash, operation, before_hash, after_hash, occurred_at         ) VALUES ($1::uuid, 'schema-audit-invalid-actor', 'raw-account-id', 'delete', $2, NULL, NOW())",
    )
    .bind(uuid::Uuid::new_v4().to_string())
    .bind(&hash)
    .execute(&pool)
    .await;
    assert!(
        invalid_actor_hash.is_err(),
        "raw account identifiers must not fit the actor hash column"
    );

    sqlx::query("DELETE FROM domain_node_mutation_audit WHERE node_id LIKE 'schema-audit-%'")
        .execute(&pool)
        .await
        .expect("post-test audit cleanup failed");
    pool.close().await;
}

/// Final identity cutover proof: obsolete `mode` contents are discarded, but
/// semantically meaningful legacy identity markers still block the column drop.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
async fn final_identity_migration_discards_mode_but_blocks_legacy_markers() {
    let pool = connect_pool().await;
    run_migrations(&pool).await;

    const ACCOUNT_ID: &str = "final-identity-migration-probe";
    sqlx::query("DELETE FROM domain_accounts WHERE id = $1")
        .bind(ACCOUNT_ID)
        .execute(&pool)
        .await
        .expect("pre-test cleanup failed");

    sqlx::raw_sql(include_str!(
        "../migrations/20260724000001_remove_ron_legacy.down.sql"
    ))
    .execute(&pool)
    .await
    .expect("restore nullable compatibility column");

    sqlx::query(
        "INSERT INTO domain_accounts (
             id, kind, mode, title, map_state, radius_m, role,
             public_payload, private_payload
         ) VALUES ($1, 'garnrolle', 'ron', 'Canonical Garnrolle',
                   'not_on_map', 0, 'gast', '{}'::jsonb, '{}'::jsonb)",
    )
    .bind(ACCOUNT_ID)
    .execute(&pool)
    .await
    .expect("insert canonical row with obsolete mode value");

    sqlx::raw_sql(include_str!(
        "../migrations/20260724000001_remove_ron_legacy.up.sql"
    ))
    .execute(&pool)
    .await
    .expect("obsolete mode value must not block the final column drop");
    assert!(
        !column_exists(&pool, "domain_accounts", "mode").await,
        "mode column must be absent after successful cutover"
    );

    sqlx::raw_sql(include_str!(
        "../migrations/20260724000001_remove_ron_legacy.down.sql"
    ))
    .execute(&pool)
    .await
    .expect("restore compatibility column for fail-closed case");
    sqlx::query(
        r#"UPDATE domain_accounts
         SET private_payload = '{"visibility":"private"}'::jsonb
         WHERE id = $1"#,
    )
    .bind(ACCOUNT_ID)
    .execute(&pool)
    .await
    .expect("add semantic legacy marker");

    let error = sqlx::raw_sql(include_str!(
        "../migrations/20260724000001_remove_ron_legacy.up.sql"
    ))
    .execute(&pool)
    .await
    .expect_err("semantic legacy marker must block the cutover");
    assert!(
        error.to_string().contains("legacy rows remain"),
        "unexpected migration error: {error}"
    );
    assert!(
        column_exists(&pool, "domain_accounts", "mode").await,
        "failed migration must leave the schema unchanged"
    );

    sqlx::query(
        "UPDATE domain_accounts
         SET private_payload = private_payload - 'visibility'
         WHERE id = $1",
    )
    .bind(ACCOUNT_ID)
    .execute(&pool)
    .await
    .expect("remove semantic legacy marker");
    sqlx::raw_sql(include_str!(
        "../migrations/20260724000001_remove_ron_legacy.up.sql"
    ))
    .execute(&pool)
    .await
    .expect("cutover must succeed after remediation");

    sqlx::query("DELETE FROM domain_accounts WHERE id = $1")
        .bind(ACCOUNT_ID)
        .execute(&pool)
        .await
        .expect("post-test cleanup failed");
    pool.close().await;
}

/// Security cutover proof: an historical radius row is never re-exposed by
/// the new migration or its intentionally irreversible down migration.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
async fn radius_privacy_migration_hides_legacy_rows_irreversibly() {
    let pool = connect_pool().await;
    run_migrations(&pool).await;

    sqlx::query("DELETE FROM domain_accounts WHERE id IN ('radius-migration-legacy', 'radius-migration-exact')")
        .execute(&pool)
        .await
        .expect("pre-test cleanup failed");
    sqlx::query(
        "INSERT INTO domain_accounts (
             id, kind, title, map_state, radius_m, role,
             location_lat, location_lon, public_payload, private_payload
         ) VALUES
         ('radius-migration-legacy', 'garnrolle', 'Legacy radius', 'radius', 500, 'weber',
          53.5, 10.0, '{}'::jsonb,
          '{\"visibility\":\"approximate\",\"radius_projection\":{\"legacy\":true},\"keep\":\"yes\"}'::jsonb),
         ('radius-migration-exact', 'garnrolle', 'Exact', 'exact', 0, 'weber',
          53.6, 10.1, '{}'::jsonb, '{\"keep\":\"exact\"}'::jsonb)",
    )
    .execute(&pool)
    .await
    .expect("insert migration fixtures");

    sqlx::raw_sql(include_str!(
        "../migrations/20260718000001_radius_projection_privacy.up.sql"
    ))
    .execute(&pool)
    .await
    .expect("apply radius privacy migration");

    let (map_state, radius_m, lat, lon, private_text): (
        String,
        i64,
        Option<f64>,
        Option<f64>,
        String,
    ) = sqlx::query_as(
        "SELECT map_state, radius_m, location_lat, location_lon, private_payload::text
         FROM domain_accounts WHERE id = 'radius-migration-legacy'",
    )
    .fetch_one(&pool)
    .await
    .expect("read migrated radius row");
    assert_eq!(map_state, "not_on_map");
    assert_eq!(radius_m, 0);
    assert_eq!(lat, Some(53.5));
    assert_eq!(lon, Some(10.0));
    let private_payload: serde_json::Value =
        serde_json::from_str(&private_text).expect("private payload json");
    assert_eq!(private_payload["keep"], "yes");
    assert!(private_payload.get("visibility").is_none());
    assert!(private_payload.get("radius_projection").is_none());

    let (exact_state, exact_radius, exact_private): (String, i64, String) = sqlx::query_as(
        "SELECT map_state, radius_m, private_payload::text
         FROM domain_accounts WHERE id = 'radius-migration-exact'",
    )
    .fetch_one(&pool)
    .await
    .expect("read exact control row");
    assert_eq!(exact_state, "exact");
    assert_eq!(exact_radius, 0);
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(&exact_private).unwrap()["keep"],
        "exact"
    );

    // Simulate a safe radius binding created after the forward migration. A
    // rollback must hide this row as well, because old application code would
    // otherwise ignore the private binding and expose the reversible legacy
    // projection again.
    sqlx::query(
        r#"UPDATE domain_accounts
         SET map_state = 'radius', radius_m = 250,
             private_payload = private_payload ||
               '{"radius_projection":{"version":1,"anchor":{"lat":53.6,"lon":10.1},"radius_m":250,"public_pos":{"lat":53.6005,"lon":10.1}}}'::jsonb
         WHERE id = 'radius-migration-exact'"#,
    )
    .execute(&pool)
    .await
    .expect("create post-cutover safe radius row");

    sqlx::raw_sql(include_str!(
        "../migrations/20260718000001_radius_projection_privacy.down.sql"
    ))
    .execute(&pool)
    .await
    .expect("apply irreversible down migration");
    for id in ["radius-migration-legacy", "radius-migration-exact"] {
        let (state_after_down, radius_after_down, lat_after_down, private_after_down): (
            String,
            i64,
            Option<f64>,
            String,
        ) = sqlx::query_as(
            "SELECT map_state, radius_m, location_lat, private_payload::text
             FROM domain_accounts WHERE id = $1",
        )
        .bind(id)
        .fetch_one(&pool)
        .await
        .expect("read row after down migration");
        assert_eq!(state_after_down, "not_on_map");
        assert_eq!(radius_after_down, 0);
        assert!(lat_after_down.is_some(), "private location must remain");
        let private_after_down: serde_json::Value =
            serde_json::from_str(&private_after_down).expect("private payload after down");
        assert!(private_after_down.get("radius_projection").is_none());
    }

    sqlx::query("DELETE FROM domain_accounts WHERE id IN ('radius-migration-legacy', 'radius-migration-exact')")
        .execute(&pool)
        .await
        .expect("post-test cleanup failed");
    pool.close().await;
}

/// Verifies that a row can be inserted into and read back from domain_nodes,
/// domain_edges, and domain_accounts using the schema defined in Phase B.
///
/// This is a structural smoke test only. It does not test any runtime API
/// logic, JSONL backfill, or cursor pagination behaviour.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
async fn domain_schema_basic_insert_and_read() {
    let pool = connect_pool().await;
    run_migrations(&pool).await;

    // Pre-test cleanup to avoid stale rows from a previously interrupted run.
    sqlx::query("DELETE FROM domain_nodes WHERE id = 'test-node-schema-probe'")
        .execute(&pool)
        .await
        .expect("pre-test cleanup of domain_nodes failed");

    // --- domain_nodes: insert and read back ---
    sqlx::query(
        "INSERT INTO domain_nodes (id, kind, title, lat, lon, payload)
         VALUES ('test-node-schema-probe', 'place', 'Test Node', 53.55, 10.00, '{\"info\":\"probe\"}'::jsonb)",
    )
    .execute(&pool)
    .await
    .expect("failed to insert test row into domain_nodes");

    let (kind, title): (String, String) =
        sqlx::query_as("SELECT kind, title FROM domain_nodes WHERE id = $1")
            .bind("test-node-schema-probe")
            .fetch_one(&pool)
            .await
            .expect("failed to read back test row from domain_nodes");

    assert_eq!(kind, "place");
    assert_eq!(title, "Test Node");

    sqlx::query("DELETE FROM domain_nodes WHERE id = 'test-node-schema-probe'")
        .execute(&pool)
        .await
        .expect("failed to clean up domain_nodes probe row");

    // --- domain_edges: insert and read back ---
    // Pre-test cleanup
    sqlx::query("DELETE FROM domain_edges WHERE id = 'test-edge-schema-probe'")
        .execute(&pool)
        .await
        .expect("pre-test cleanup of domain_edges failed");

    sqlx::query(
        "INSERT INTO domain_edges (id, source_id, target_id, edge_kind, payload)
         VALUES ('test-edge-schema-probe', 'src-001', 'tgt-001', 'TestKind', '{}'::jsonb)",
    )
    .execute(&pool)
    .await
    .expect("failed to insert test row into domain_edges");

    let (source_id, target_id): (String, String) =
        sqlx::query_as("SELECT source_id, target_id FROM domain_edges WHERE id = $1")
            .bind("test-edge-schema-probe")
            .fetch_one(&pool)
            .await
            .expect("failed to read back test row from domain_edges");

    assert_eq!(source_id, "src-001");
    assert_eq!(target_id, "tgt-001");

    sqlx::query("DELETE FROM domain_edges WHERE id = 'test-edge-schema-probe'")
        .execute(&pool)
        .await
        .expect("failed to clean up domain_edges probe row");

    // --- domain_accounts: insert and read back ---
    // Pre-test cleanup
    sqlx::query("DELETE FROM domain_accounts WHERE id = 'test-account-schema-probe'")
        .execute(&pool)
        .await
        .expect("pre-test cleanup of domain_accounts failed");

    sqlx::query(
        "INSERT INTO domain_accounts (id, kind, title, map_state, radius_m, role, public_payload, private_payload)
            VALUES ('test-account-schema-probe', 'garnrolle', 'Test Account', 'not_on_map', 0, 'gast', '{}'::jsonb, '{}'::jsonb)",
    )
    .execute(&pool)
    .await
    .expect("failed to insert test row into domain_accounts");

    let (kind, map_state): (String, String) =
        sqlx::query_as("SELECT kind, map_state FROM domain_accounts WHERE id = $1")
            .bind("test-account-schema-probe")
            .fetch_one(&pool)
            .await
            .expect("failed to read back test row from domain_accounts");

    assert_eq!(kind, "garnrolle");
    assert_eq!(map_state, "not_on_map");

    sqlx::query("DELETE FROM domain_accounts WHERE id = 'test-account-schema-probe'")
        .execute(&pool)
        .await
        .expect("failed to clean up domain_accounts probe row");

    pool.close().await;
}

/// Verifies the create-operation schema contract directly: legacy NULL/NULL
/// rows remain valid, one account cannot reuse one operation id for a second
/// row, another account may use the same UUID independently, and half-populated
/// operation metadata is rejected by the pair check.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
async fn domain_create_operation_constraints_are_enforced() {
    let pool = connect_pool().await;
    run_migrations(&pool).await;

    sqlx::query("DELETE FROM domain_edges WHERE id LIKE 'test-operation-edge-%'")
        .execute(&pool)
        .await
        .expect("failed to clean edge create-operation probe rows");
    sqlx::query("DELETE FROM domain_nodes WHERE id LIKE 'test-operation-node-%'")
        .execute(&pool)
        .await
        .expect("failed to clean node create-operation probe rows");

    sqlx::query(
        "INSERT INTO domain_nodes \
            (id, kind, title, lat, lon, payload, create_actor_id, create_operation_id) \
         VALUES \
            ('test-operation-node-a', 'test', 'A', 1, 1, '{}', 'actor-a', \
             '50000000-0000-0000-0000-000000000001')",
    )
    .execute(&pool)
    .await
    .expect("first node operation row must insert");

    let node_duplicate = sqlx::query(
        "INSERT INTO domain_nodes \
            (id, kind, title, lat, lon, payload, create_actor_id, create_operation_id) \
         VALUES \
            ('test-operation-node-b', 'test', 'B', 1, 1, '{}', 'actor-a', \
             '50000000-0000-0000-0000-000000000001')",
    )
    .execute(&pool)
    .await
    .expect_err("same actor and node operation must be unique");
    assert!(node_duplicate
        .as_database_error()
        .is_some_and(|error| error.is_unique_violation()));

    sqlx::query(
        "INSERT INTO domain_nodes \
            (id, kind, title, lat, lon, payload, create_actor_id, create_operation_id) \
         VALUES \
            ('test-operation-node-c', 'test', 'C', 1, 1, '{}', 'actor-b', \
             '50000000-0000-0000-0000-000000000001')",
    )
    .execute(&pool)
    .await
    .expect("same node operation UUID must remain independent across accounts");

    let node_half_pair = sqlx::query(
        "INSERT INTO domain_nodes \
            (id, kind, title, lat, lon, payload, create_actor_id) \
         VALUES ('test-operation-node-half', 'test', 'half', 1, 1, '{}', 'actor-a')",
    )
    .execute(&pool)
    .await
    .expect_err("half-populated node operation metadata must fail");
    assert_eq!(
        node_half_pair
            .as_database_error()
            .and_then(|error| error.code())
            .as_deref(),
        Some("23514")
    );

    for (id, actor, operation) in [
        (
            "test-operation-node-blank-actor",
            "   ",
            "50000000-0000-0000-0000-000000000002",
        ),
        ("test-operation-node-invalid-uuid", "actor-a", "NOT-A-UUID"),
    ] {
        let error = sqlx::query(
            "INSERT INTO domain_nodes \
                (id, kind, title, lat, lon, payload, create_actor_id, create_operation_id) \
             VALUES ($1, 'test', 'invalid metadata', 1, 1, '{}', $2, $3)",
        )
        .bind(id)
        .bind(actor)
        .bind(operation)
        .execute(&pool)
        .await
        .expect_err("invalid node operation metadata must fail");
        assert_eq!(
            error
                .as_database_error()
                .and_then(|error| error.code())
                .as_deref(),
            Some("23514")
        );
    }

    sqlx::query(
        "INSERT INTO domain_edges \
            (id, source_id, target_id, edge_kind, payload, create_actor_id, create_operation_id) \
         VALUES \
            ('test-operation-edge-a', 'src', 'dst', 'reference', '{}', 'actor-a', \
             '60000000-0000-0000-0000-000000000001')",
    )
    .execute(&pool)
    .await
    .expect("first edge operation row must insert");

    let edge_duplicate = sqlx::query(
        "INSERT INTO domain_edges \
            (id, source_id, target_id, edge_kind, payload, create_actor_id, create_operation_id) \
         VALUES \
            ('test-operation-edge-b', 'src', 'dst', 'reference', '{}', 'actor-a', \
             '60000000-0000-0000-0000-000000000001')",
    )
    .execute(&pool)
    .await
    .expect_err("same actor and edge operation must be unique");
    assert!(edge_duplicate
        .as_database_error()
        .is_some_and(|error| error.is_unique_violation()));

    sqlx::query(
        "INSERT INTO domain_edges \
            (id, source_id, target_id, edge_kind, payload, create_actor_id, create_operation_id) \
         VALUES \
            ('test-operation-edge-c', 'src', 'dst', 'reference', '{}', 'actor-b', \
             '60000000-0000-0000-0000-000000000001')",
    )
    .execute(&pool)
    .await
    .expect("same edge operation UUID must remain independent across accounts");

    let edge_half_pair = sqlx::query(
        "INSERT INTO domain_edges \
            (id, source_id, target_id, edge_kind, payload, create_operation_id) \
         VALUES \
            ('test-operation-edge-half', 'src', 'dst', 'reference', '{}', \
             '60000000-0000-0000-0000-000000000002')",
    )
    .execute(&pool)
    .await
    .expect_err("half-populated edge operation metadata must fail");
    assert_eq!(
        edge_half_pair
            .as_database_error()
            .and_then(|error| error.code())
            .as_deref(),
        Some("23514")
    );

    for (id, actor, operation) in [
        (
            "test-operation-edge-blank-actor",
            "",
            "60000000-0000-0000-0000-000000000003",
        ),
        (
            "test-operation-edge-invalid-uuid",
            "actor-a",
            "60000000-INVALID",
        ),
    ] {
        let error = sqlx::query(
            "INSERT INTO domain_edges \
                (id, source_id, target_id, edge_kind, payload, \
                 create_actor_id, create_operation_id) \
             VALUES ($1, 'src', 'dst', 'reference', '{}', $2, $3)",
        )
        .bind(id)
        .bind(actor)
        .bind(operation)
        .execute(&pool)
        .await
        .expect_err("invalid edge operation metadata must fail");
        assert_eq!(
            error
                .as_database_error()
                .and_then(|error| error.code())
                .as_deref(),
            Some("23514")
        );
    }

    sqlx::query("DELETE FROM domain_edges WHERE id LIKE 'test-operation-edge-%'")
        .execute(&pool)
        .await
        .expect("failed to clean edge create-operation probe rows");
    sqlx::query("DELETE FROM domain_nodes WHERE id LIKE 'test-operation-node-%'")
        .execute(&pool)
        .await
        .expect("failed to clean node create-operation probe rows");

    pool.close().await;
}

/// Verifies that normalized non-empty account emails are unique
/// (case-insensitive) via the `domain_accounts_email_normalized_unique` partial
/// index and that after-trim-empty emails are rejected by the
/// `domain_accounts_email_not_empty_after_trim` check constraint, while NULL
/// emails remain allowed and the case-insensitive lookup index stays in place.
/// TODO 2A supersedes the former Phase-B duplicate-email tolerance for this
/// narrow invariant only.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
async fn domain_accounts_normalized_email_uniqueness_is_enforced() {
    let pool = connect_pool().await;
    run_migrations(&pool).await;

    // Clean up any prior probe rows
    sqlx::query(
        "DELETE FROM domain_accounts WHERE id IN (
            'test-email-dup-a', 'test-email-dup-b', 'test-email-null-a', 'test-email-null-b'
        )",
    )
    .execute(&pool)
    .await
    .expect("failed to clean up probe rows");

    // Insert first account with email
    sqlx::query(
        "INSERT INTO domain_accounts (id, kind, title, map_state, radius_m, role, email, public_payload, private_payload)
         VALUES ('test-email-dup-a', 'garnrolle', 'dup-a', 'not_on_map', 0, 'gast', 'alpha@example.invalid', '{}', '{}')",
    )
    .execute(&pool)
    .await
    .expect("first insert with email must succeed");

    // Second account with the same normalized email (different case) must now be
    // rejected specifically by the normalized unique index (TODO 2A), identified
    // by constraint name so any unrelated database error fails the test.
    let dup_result = sqlx::query(
        "INSERT INTO domain_accounts (id, kind, title, map_state, radius_m, role, email, public_payload, private_payload)
         VALUES ('test-email-dup-b', 'garnrolle', 'dup-b', 'not_on_map', 0, 'gast', 'ALPHA@example.invalid', '{}', '{}')",
    )
    .execute(&pool)
    .await;
    let dup_err = dup_result.expect_err("duplicate normalized email must be rejected");
    let dup_db_err = dup_err
        .as_database_error()
        .expect("duplicate email rejection must be a database error");
    assert_eq!(
        dup_db_err.constraint(),
        Some(weltgewebe_api::domain_db::ACCOUNT_EMAIL_UNIQUE_CONSTRAINT),
        "must be rejected by the normalized email unique index, not another constraint"
    );

    // Two accounts without email (NULL) must both succeed
    sqlx::query(
        "INSERT INTO domain_accounts (id, kind, title, map_state, radius_m, role, public_payload, private_payload)
         VALUES ('test-email-null-a', 'garnrolle', 'null-a', 'not_on_map', 0, 'gast', '{}', '{}')",
    )
    .execute(&pool)
    .await
    .expect("first NULL-email insert must succeed");

    sqlx::query(
        "INSERT INTO domain_accounts (id, kind, title, map_state, radius_m, role, public_payload, private_payload)
         VALUES ('test-email-null-b', 'garnrolle', 'null-b', 'not_on_map', 0, 'gast', '{}', '{}')",
    )
    .execute(&pool)
    .await
    .expect("second NULL-email insert must succeed");

    // After-trim-empty emails are rejected by the check constraint
    // domain_accounts_email_not_empty_after_trim. NULL stays allowed (above);
    // "" and whitespace-only must fail with that specific constraint.
    for (probe_id, probe_email) in [("test-email-empty", ""), ("test-email-ws", "   ")] {
        let res = sqlx::query(
            "INSERT INTO domain_accounts (id, kind, title, map_state, radius_m, role, email, public_payload, private_payload)
             VALUES ($1, 'garnrolle', 'empty-email', 'not_on_map', 0, 'gast', $2, '{}', '{}')",
        )
        .bind(probe_id)
        .bind(probe_email)
        .execute(&pool)
        .await;
        let err = res.expect_err("after-trim-empty email must be rejected");
        let db_err = err
            .as_database_error()
            .expect("check-constraint rejection must be a database error");
        assert_eq!(
            db_err.constraint(),
            Some("domain_accounts_email_not_empty_after_trim"),
            "after-trim-empty email must violate the not-empty-after-trim check"
        );
    }

    // Radius supports full u32 range via BIGINT + CHECK constraint.
    sqlx::query("DELETE FROM domain_accounts WHERE id = 'test-radius-u32-max'")
        .execute(&pool)
        .await
        .expect("pre-test cleanup of test-radius-u32-max failed");

    sqlx::query(
        "INSERT INTO domain_accounts (id, kind, title, map_state, radius_m, location_lat, location_lon, role, public_payload, private_payload)
         VALUES ('test-radius-u32-max', 'garnrolle', 'radius-max', 'radius', 4294967295, 53.5, 10.0, 'gast', '{}', '{}')",
    )
    .execute(&pool)
    .await
    .expect("u32::MAX radius_m must be accepted");

    let overflow_result = sqlx::query(
        "INSERT INTO domain_accounts (id, kind, title, map_state, radius_m, location_lat, location_lon, role, public_payload, private_payload)
         VALUES ('test-radius-overflow', 'garnrolle', 'radius-overflow', 'radius', 4294967296, 53.5, 10.0, 'gast', '{}', '{}')",
    )
    .execute(&pool)
    .await;

    assert!(
        overflow_result.is_err(),
        "radius_m above u32::MAX must violate check constraint"
    );

    // Clean up
    sqlx::query(
        "DELETE FROM domain_accounts WHERE id IN (
            'test-email-dup-a', 'test-email-dup-b', 'test-email-null-a', 'test-email-null-b',
            'test-radius-u32-max', 'test-radius-overflow'
        )",
    )
    .execute(&pool)
    .await
    .expect("failed to clean up probe rows");

    pool.close().await;
}
