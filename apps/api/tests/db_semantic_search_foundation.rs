//! Integration proof for Semantic Search v1 T003 PostgreSQL foundations.
//!
//! This test is ignored by default and requires a direct disposable PostgreSQL
//! database. It verifies the migration surface and hard candidate boundaries;
//! it does not start an API, worker, backfill or production search runtime.

mod support;

use std::path::PathBuf;

use serial_test::serial;
use sqlx::{
    postgres::{PgConnectOptions, PgPoolOptions, PgSslMode},
    Connection, PgConnection,
};

const GENERATION: &str = "rust-t003-generation";
const ACTIVE_NODE: &str = "rust-t003-active";
const HIDDEN_NODE: &str = "rust-t003-hidden";
const DELETED_NODE: &str = "rust-t003-deleted";

fn required_env(name: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| {
        panic!("{name} must identify the direct disposable PostgreSQL test instance")
    })
}

async fn connect_pool() -> sqlx::PgPool {
    let host = required_env("T003_PG_HOST");
    let port: u16 = required_env("T003_PG_PORT")
        .parse()
        .expect("T003_PG_PORT must be a valid TCP port");
    assert_ne!(
        port, 6432,
        "T003 must target direct PostgreSQL, not PgBouncer"
    );
    let database = required_env("T003_PG_DATABASE");
    support::postgres_proof::assert_disposable_database_name(&database);
    let options = PgConnectOptions::new()
        .host(&host)
        .port(port)
        .username(&required_env("T003_PG_USER"))
        .password(&required_env("T003_PG_PASSWORD"))
        .database(&database)
        .ssl_mode(PgSslMode::Disable);
    let probe = PgConnection::connect_with(&options)
        .await
        .expect("connect direct PostgreSQL probe");
    probe.close().await.expect("close direct PostgreSQL probe");
    PgPoolOptions::new()
        .max_connections(4)
        .connect_with(options)
        .await
        .expect("connect direct PostgreSQL pool")
}

async fn run_migrations(pool: &sqlx::PgPool) {
    let migrations: PathBuf = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("migrations");
    sqlx::migrate::Migrator::new(migrations)
        .await
        .expect("load migrations")
        .run(pool)
        .await
        .expect("run migrations");
}

async fn apply_t003_contract(pool: &sqlx::PgPool) {
    // This target proves the historical T003 foundation in isolation. The
    // production migrator now also contains the later T005 projection schema,
    // so unwind only that Search migration in this disposable database before
    // applying the deliberately non-production T003 proof contract.
    sqlx::raw_sql(include_str!(
        "../migrations/20260721000001_semantic_search_projection_worker.down.sql"
    ))
    .execute(pool)
    .await
    .expect("unwind T005 search migration for isolated T003 proof");
    sqlx::raw_sql(include_str!(
        "../../../contracts/search/postgres-foundation.up.sql"
    ))
    .execute(pool)
    .await
    .expect("apply T003 proof contract");
}

async fn cleanup(pool: &sqlx::PgPool) {
    sqlx::query("DELETE FROM domain_nodes WHERE id = ANY($1::text[])")
        .bind(vec![ACTIVE_NODE, HIDDEN_NODE, DELETED_NODE])
        .execute(pool)
        .await
        .expect("clean T003 probe nodes");
    sqlx::query("DELETE FROM search_index_generations WHERE generation_id = $1")
        .bind(GENERATION)
        .execute(pool)
        .await
        .expect("clean T003 probe generation");
}

async fn put_projection(
    pool: &sqlx::PgPool,
    node_id: &str,
    source_version: i64,
    status: &str,
    scopes: Vec<&str>,
    embedding: Option<Vec<f64>>,
) -> Result<String, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT weltgewebe_put_search_projection(
             $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13
         )",
    )
    .bind(GENERATION)
    .bind(node_id)
    .bind(source_version)
    .bind(format!("revision-{source_version}"))
    .bind("a".repeat(64))
    .bind("Fahrrad Reparatur")
    .bind(vec!["fahrrad", "reparatur"])
    .bind("Fahrradwerkstatt und gemeinschaftliche Reparaturhilfe")
    .bind("de")
    .bind("place")
    .bind(status)
    .bind(scopes)
    .bind(embedding)
    .fetch_one(pool)
    .await
}

#[tokio::test]
#[serial]
#[ignore = "requires DATABASE_URL pointing to direct disposable PostgreSQL"]
async fn postgres_foundation_enforces_visibility_generation_and_revision_order() {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    apply_t003_contract(&pool).await;
    cleanup(&pool).await;

    let extensions: Vec<String> = sqlx::query_scalar(
        "SELECT extname FROM pg_extension
         WHERE extname IN ('pgcrypto', 'pg_trgm') ORDER BY extname",
    )
    .fetch_all(&pool)
    .await
    .expect("read installed extensions");
    assert_eq!(extensions, vec!["pg_trgm"]);

    let vector_available: bool = sqlx::query_scalar(
        "SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector')",
    )
    .fetch_one(&pool)
    .await
    .expect("read pgvector capability");
    assert!(
        !vector_available,
        "canonical pinned PostgreSQL image unexpectedly changed; remeasure T003 before enabling embeddings"
    );

    sqlx::query(
        "INSERT INTO search_index_generations (
             generation_id, provider, model_id, model_revision, dimension,
             document_revision, normalization_revision, state
         ) VALUES ($1, 'local-reference', 'qwen3-embedding:8b', 'sha256:test', 4,
                   'node-document-v1', 'normalization-v1', 'ready')",
    )
    .bind(GENERATION)
    .execute(&pool)
    .await
    .expect("insert generation");

    for (id, title) in [
        (ACTIVE_NODE, "Aktive Fahrradwerkstatt"),
        (HIDDEN_NODE, "Verborgene Fahrradwerkstatt"),
        (DELETED_NODE, "Gelöschte Fahrradwerkstatt"),
    ] {
        sqlx::query(
            "INSERT INTO domain_nodes (id, kind, title, payload)
             VALUES ($1, 'place', $2, '{}'::jsonb)",
        )
        .bind(id)
        .bind(title)
        .execute(&pool)
        .await
        .expect("insert domain node");
    }

    assert_eq!(
        put_projection(&pool, ACTIVE_NODE, 1, "active", vec!["public"], None)
            .await
            .expect("insert active projection"),
        "inserted"
    );
    put_projection(&pool, HIDDEN_NODE, 1, "hidden", vec!["public"], None)
        .await
        .expect("insert hidden projection");
    put_projection(&pool, DELETED_NODE, 1, "deleted", vec!["public"], None)
        .await
        .expect("insert deleted projection");

    sqlx::query("SELECT weltgewebe_activate_search_generation($1)")
        .bind(GENERATION)
        .execute(&pool)
        .await
        .expect("activate generation");

    let candidates: Vec<String> = sqlx::query_scalar(
        "SELECT node_id FROM weltgewebe_search_lexical_candidates(
             $1, 'public', $2, 'Fahrrad Reparatur', '{}', '{}', ARRAY['de'], 10
         )",
    )
    .bind(GENERATION)
    .bind(vec![ACTIVE_NODE, HIDDEN_NODE, DELETED_NODE])
    .fetch_all(&pool)
    .await
    .expect("query candidates");
    assert_eq!(candidates, vec![ACTIVE_NODE]);

    let unauthorized: Vec<String> = sqlx::query_scalar(
        "SELECT node_id FROM weltgewebe_search_lexical_candidates(
             $1, 'public', '{}', 'Fahrrad Reparatur', '{}', '{}', ARRAY['de'], 10
         )",
    )
    .bind(GENERATION)
    .fetch_all(&pool)
    .await
    .expect("query unauthorized candidates");
    assert!(unauthorized.is_empty());

    assert_eq!(
        put_projection(&pool, ACTIVE_NODE, 1, "active", vec!["public"], None)
            .await
            .expect("repeat projection"),
        "idempotent"
    );
    let visibility_conflict = put_projection(&pool, ACTIVE_NODE, 1, "hidden", vec!["public"], None)
        .await
        .expect_err("equal-version visibility conflict must fail");
    assert_eq!(
        visibility_conflict
            .as_database_error()
            .and_then(|error| error.code())
            .as_deref(),
        Some("40001")
    );

    assert_eq!(
        put_projection(&pool, ACTIVE_NODE, 0, "active", vec!["public"], None)
            .await
            .expect("stale projection"),
        "stale"
    );

    let wrong_dimension = put_projection(
        &pool,
        ACTIVE_NODE,
        2,
        "active",
        vec!["public"],
        Some(vec![1.0]),
    )
    .await
    .expect_err("wrong embedding dimension must fail");
    assert_eq!(
        wrong_dimension
            .as_database_error()
            .and_then(|error| error.code())
            .as_deref(),
        Some("23514")
    );

    cleanup(&pool).await;
    pool.close().await;
}
