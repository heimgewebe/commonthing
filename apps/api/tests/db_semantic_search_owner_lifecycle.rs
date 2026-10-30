//! Direct disposable-PostgreSQL proof for T012's canonical private-owner lifecycle.

mod support;

use std::{path::PathBuf, sync::Arc};

use async_trait::async_trait;
use sqlx::{postgres::PgPoolOptions, PgPool};
use weltgewebe_api::{
    auth::role::Role,
    middleware::auth::AuthContext,
    search::{
        fetch_postgres_candidates, EmbeddingProvider, EmbeddingProviderError, GenerationSpec,
        ProcessOutcome, ProjectionWorker, SearchFilters,
    },
    telemetry::{BuildInfo, Metrics},
};

const NODE_ID: &str = "t012-owner-lifecycle-node";
const OWNER_ID: &str = "t012-owner-lifecycle-account";
const PRIVATE_TITLE: &str = "Vertrauliche Fahrradwerkstatt";
const REDACTED: &str = "[nicht öffentlich]";

fn database_url() -> String {
    let url = std::env::var("T005_DATABASE_URL")
        .expect("T005_DATABASE_URL must point to a direct disposable PostgreSQL database");
    support::postgres_proof::assert_direct_disposable_database_url(&url);
    url
}

async fn pool() -> PgPool {
    PgPoolOptions::new()
        .max_connections(5)
        .connect(&database_url())
        .await
        .expect("connect T012 owner-lifecycle PostgreSQL")
}

async fn migrate(pool: &PgPool) {
    let migrations = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("migrations");
    sqlx::migrate::Migrator::new(migrations)
        .await
        .expect("load migrations")
        .run(pool)
        .await
        .expect("run migrations");
}

async fn reset(pool: &PgPool) {
    sqlx::query("DELETE FROM domain_nodes WHERE id=$1")
        .bind(NODE_ID)
        .execute(pool)
        .await
        .expect("remove owner-lifecycle node");
    sqlx::query("DELETE FROM domain_accounts WHERE id=$1")
        .bind(OWNER_ID)
        .execute(pool)
        .await
        .expect("remove owner-lifecycle account");
    sqlx::query(
        "TRUNCATE search_projection_jobs, search_node_projections, search_node_versions, search_index_generations RESTART IDENTITY CASCADE",
    )
    .execute(pool)
    .await
    .expect("reset owner-lifecycle projection state");
}

struct ForbiddenProvider;

#[async_trait]
impl EmbeddingProvider for ForbiddenProvider {
    async fn embed(
        &self,
        _document: &str,
        _dimension: usize,
    ) -> Result<Vec<f64>, EmbeddingProviderError> {
        panic!("private owner lifecycle must never call the embedding provider")
    }
}

fn exact_generation_spec() -> GenerationSpec<'static> {
    let draft = GenerationSpec {
        generation_id: "",
        provider: "local:ollama",
        model_id: "t012-owner-model",
        model_revision: "t012-owner-revision",
        runtime_identity: "ollama:t012-owner@http://127.0.0.1:11434",
        dimension: 3,
    };
    let generation_id: &'static str = Box::leak(draft.derived_id().into_boxed_str());
    GenerationSpec {
        generation_id,
        ..draft
    }
}

fn worker(pool: PgPool) -> ProjectionWorker {
    ProjectionWorker::new_with_provider(
        pool,
        "t012-owner-lifecycle-worker".to_owned(),
        Metrics::try_new(BuildInfo::collect()).expect("metrics"),
        Arc::new(ForbiddenProvider),
    )
    .expect("owner-lifecycle worker")
}

#[derive(sqlx::FromRow)]
struct ProjectionRow {
    source_version: i64,
    title: String,
    tags: Vec<String>,
    searchable_text: String,
    language: String,
    kind: String,
    status: String,
    visibility_scopes: Vec<String>,
    semantic_state: String,
    embedding: Option<Vec<f64>>,
    search_vector: String,
    search_vector_simple: String,
}

async fn projection(pool: &PgPool, generation: &str) -> ProjectionRow {
    sqlx::query_as(
        "SELECT source_version,title,tags,searchable_text,language,kind,status,visibility_scopes,semantic_state,embedding, \
                search_vector::text AS search_vector, search_vector_simple::text AS search_vector_simple \
         FROM search_node_projections WHERE generation_id=$1 AND node_id=$2",
    )
    .bind(generation)
    .bind(NODE_ID)
    .fetch_one(pool)
    .await
    .expect("read owner-lifecycle projection")
}

fn assert_redacted(row: &ProjectionRow) {
    assert_eq!(row.title, REDACTED);
    assert!(row.tags.is_empty());
    assert_eq!(row.searchable_text, REDACTED);
    assert_eq!(row.language, "und");
    assert_eq!(row.kind, REDACTED);
    assert_eq!(row.status, "hidden");
    assert!(row.visibility_scopes.is_empty());
    assert_eq!(row.semantic_state, "unavailable");
    assert!(row.embedding.is_none());
    let german_vector = row.search_vector.to_lowercase();
    let simple_vector = row.search_vector_simple.to_lowercase();
    for sensitive_lexeme in ["vertraulich", "fahrradwerkstatt", "eigentuemer"] {
        assert!(
            !german_vector.contains(sensitive_lexeme),
            "redacted German vector retained sensitive lexeme {sensitive_lexeme}: {german_vector}"
        );
        assert!(
            !simple_vector.contains(sensitive_lexeme),
            "redacted simple vector retained sensitive lexeme {sensitive_lexeme}: {simple_vector}"
        );
    }
}

fn assert_private(row: &ProjectionRow) {
    assert_eq!(row.title, PRIVATE_TITLE);
    assert!(row.searchable_text.contains(PRIVATE_TITLE));
    assert!(row.searchable_text.contains("Nur für den Eigentümer"));
    assert!(!row.searchable_text.contains("Geheimer Weg 9"));
    assert_eq!(row.status, "active");
    assert_eq!(row.visibility_scopes, ["owner"]);
    assert_eq!(row.semantic_state, "unavailable");
    assert!(row.embedding.is_none());
}

fn forged_owner_auth() -> AuthContext {
    AuthContext {
        authenticated: true,
        account_id: Some(OWNER_ID.to_owned()),
        device_id: Some("t012-owner-device".to_owned()),
        role: Role::Gast,
        expires_at: None,
    }
}

async fn owner_candidate_count(pool: &PgPool) -> usize {
    fetch_postgres_candidates(
        pool,
        PRIVATE_TITLE,
        &SearchFilters::default(),
        &forged_owner_auth(),
    )
    .await
    .expect("query private candidates")
    .expect("active generation")
    .candidates
    .len()
}

async fn process_one(projection_worker: &ProjectionWorker) {
    assert_eq!(
        projection_worker
            .claim_and_process_one()
            .await
            .expect("process owner-lifecycle job"),
        ProcessOutcome::Processed
    );
}

#[tokio::test]
#[ignore = "requires T005_DATABASE_URL pointing to direct disposable PostgreSQL"]
async fn private_owner_lifecycle_is_account_bound_redacted_and_recoverable() {
    let pool = pool().await;
    migrate(&pool).await;
    reset(&pool).await;

    sqlx::query(
        "INSERT INTO domain_nodes \
         (id,kind,title,lat,lon,payload,search_visibility,created_at,updated_at) \
         VALUES ($1,'place',$2,54.9,8.3,$3::jsonb,'private',clock_timestamp(),clock_timestamp())",
    )
    .bind(NODE_ID)
    .bind(PRIVATE_TITLE)
    .bind(format!(
        r#"{{"summary":"Nur für den Eigentümer","address":"Geheimer Weg 9","created_by_account_id":"{OWNER_ID}"}}"#
    ))
    .execute(&pool)
    .await
    .expect("insert private node without account");

    let generation_spec = exact_generation_spec();
    let generation = generation_spec.generation_id.to_owned();
    let projection_worker = worker(pool.clone());
    projection_worker
        .start_generation(generation_spec)
        .await
        .expect("start owner-lifecycle generation");
    process_one(&projection_worker).await;

    let missing_owner = projection(&pool, &generation).await;
    assert_eq!(missing_owner.source_version, 1);
    assert_redacted(&missing_owner);
    let ready_without_owner: bool =
        sqlx::query_scalar("SELECT weltgewebe_search_generation_activation_ready($1)")
            .bind(&generation)
            .fetch_one(&pool)
            .await
            .expect("read missing-owner readiness");
    assert!(ready_without_owner);
    sqlx::query("SELECT weltgewebe_activate_search_generation($1)")
        .bind(&generation)
        .execute(&pool)
        .await
        .expect("activate owner-lifecycle generation");
    assert_eq!(owner_candidate_count(&pool).await, 0);

    sqlx::query("INSERT INTO domain_accounts (id,title,disabled) VALUES ($1,'Eigentümer',FALSE)")
        .bind(OWNER_ID)
        .execute(&pool)
        .await
        .expect("create active owner account");
    assert_eq!(owner_candidate_count(&pool).await, 0);
    process_one(&projection_worker).await;
    let active_owner = projection(&pool, &generation).await;
    assert_eq!(active_owner.source_version, 2);
    assert_private(&active_owner);
    assert_eq!(owner_candidate_count(&pool).await, 1);

    // Prove the account-row lock against the dangerous race: a new private
    // projection is inserted but not committed yet while another connection
    // disables the owner. The disable must wait; after the insert commits, the
    // lifecycle trigger must observe and synchronously redact that row.
    sqlx::query("DELETE FROM search_node_projections WHERE generation_id=$1 AND node_id=$2")
        .bind(&generation)
        .bind(NODE_ID)
        .execute(&pool)
        .await
        .expect("remove committed private projection before race proof");

    let mut projection_tx = pool
        .begin()
        .await
        .expect("begin concurrent projection write");
    sqlx::query(
        "INSERT INTO search_node_projections \
         (generation_id,node_id,source_version,source_revision,content_sha256,title,tags,searchable_text,language,kind,status,visibility_scopes,semantic_state,embedding) \
         SELECT $1,v.node_id,v.source_version,v.source_revision,$3,$4,'{}'::TEXT[],$5,'de','place','active',ARRAY['owner']::TEXT[],'unavailable',NULL \
         FROM search_node_versions v WHERE v.node_id=$2",
    )
    .bind(&generation)
    .bind(NODE_ID)
    .bind(format!("{:064x}", 99))
    .bind(PRIVATE_TITLE)
    .bind(format!("{PRIVATE_TITLE} Nur für den Eigentümer"))
    .execute(&mut *projection_tx)
    .await
    .expect("insert uncommitted private projection");

    let mut disable_connection = pool
        .acquire()
        .await
        .expect("acquire concurrent account connection");
    let (disable_started_tx, disable_started_rx) = tokio::sync::oneshot::channel();
    let mut disable_task = tokio::spawn(async move {
        let _ = disable_started_tx.send(());
        sqlx::query("UPDATE domain_accounts SET disabled=TRUE WHERE id=$1")
            .bind(OWNER_ID)
            .execute(&mut *disable_connection)
            .await
    });
    disable_started_rx
        .await
        .expect("concurrent disable task started");
    assert!(
        tokio::time::timeout(std::time::Duration::from_millis(250), &mut disable_task)
            .await
            .is_err(),
        "owner disable completed before the uncommitted private projection released its account lock"
    );
    projection_tx
        .commit()
        .await
        .expect("commit concurrent private projection");
    let disabled_result = disable_task
        .await
        .expect("join concurrent owner disable")
        .expect("disable owner account after projection commit");
    assert_eq!(disabled_result.rows_affected(), 1);

    let synchronously_disabled = projection(&pool, &generation).await;
    assert_redacted(&synchronously_disabled);
    assert_eq!(owner_candidate_count(&pool).await, 0);

    sqlx::query(
        "UPDATE search_node_projections \
         SET title='LEAK', searchable_text='LEAK', status='active', \
             visibility_scopes=ARRAY['owner']::TEXT[], semantic_state='unavailable' \
         WHERE generation_id=$1 AND node_id=$2",
    )
    .bind(&generation)
    .bind(NODE_ID)
    .execute(&pool)
    .await
    .expect("attempt direct plaintext restoration");
    assert_redacted(&projection(&pool, &generation).await);

    process_one(&projection_worker).await;
    let disabled_owner = projection(&pool, &generation).await;
    assert_eq!(disabled_owner.source_version, 3);
    assert_redacted(&disabled_owner);

    sqlx::query("UPDATE domain_accounts SET disabled=FALSE WHERE id=$1")
        .bind(OWNER_ID)
        .execute(&pool)
        .await
        .expect("reactivate owner account");
    assert_eq!(owner_candidate_count(&pool).await, 0);
    process_one(&projection_worker).await;
    let reactivated_owner = projection(&pool, &generation).await;
    assert_eq!(reactivated_owner.source_version, 4);
    assert_private(&reactivated_owner);
    assert_eq!(owner_candidate_count(&pool).await, 1);

    sqlx::query("DELETE FROM domain_accounts WHERE id=$1")
        .bind(OWNER_ID)
        .execute(&pool)
        .await
        .expect("delete owner account");
    let synchronously_deleted = projection(&pool, &generation).await;
    assert_redacted(&synchronously_deleted);
    assert_eq!(owner_candidate_count(&pool).await, 0);
    process_one(&projection_worker).await;
    let deleted_owner = projection(&pool, &generation).await;
    assert_eq!(deleted_owner.source_version, 5);
    assert_redacted(&deleted_owner);

    let final_ready: bool =
        sqlx::query_scalar("SELECT weltgewebe_search_generation_activation_ready($1)")
            .bind(&generation)
            .fetch_one(&pool)
            .await
            .expect("read final owner-lifecycle readiness");
    assert!(final_ready);
}

#[tokio::test]
#[ignore = "requires T005_DATABASE_URL pointing to direct disposable PostgreSQL"]
async fn private_worker_locks_owner_before_projection_state() {
    let pool = pool().await;
    migrate(&pool).await;
    reset(&pool).await;

    sqlx::query("INSERT INTO domain_accounts (id,title,disabled) VALUES ($1,'Eigentümer',FALSE)")
        .bind(OWNER_ID)
        .execute(&pool)
        .await
        .expect("create active owner for worker race proof");
    sqlx::query(
        "INSERT INTO domain_nodes \
         (id,kind,title,lat,lon,payload,search_visibility,created_at,updated_at) \
         VALUES ($1,'place',$2,54.9,8.3,$3::jsonb,'private',clock_timestamp(),clock_timestamp())",
    )
    .bind(NODE_ID)
    .bind(PRIVATE_TITLE)
    .bind(format!(
        r#"{{"summary":"Nur für den Eigentümer","created_by_account_id":"{OWNER_ID}"}}"#
    ))
    .execute(&pool)
    .await
    .expect("insert private node for worker race proof");

    let generation_spec = exact_generation_spec();
    let generation = generation_spec.generation_id.to_owned();
    let projection_worker = worker(pool.clone());
    projection_worker
        .start_generation(generation_spec)
        .await
        .expect("start generation for worker race proof");

    let mut version_blocker = pool.begin().await.expect("begin version blocker");
    sqlx::query("SELECT node_id FROM search_node_versions WHERE node_id=$1 FOR UPDATE")
        .bind(NODE_ID)
        .fetch_one(&mut *version_blocker)
        .await
        .expect("lock node version before worker");

    let worker_task = tokio::spawn(async move { projection_worker.claim_and_process_one().await });

    tokio::time::timeout(std::time::Duration::from_secs(10), async {
        loop {
            let waiting: bool = sqlx::query_scalar(
                "SELECT EXISTS (\
                   SELECT 1 FROM pg_stat_activity \
                   WHERE pid <> pg_backend_pid() \
                     AND datname = current_database() \
                     AND state = 'active' \
                     AND wait_event_type = 'Lock' \
                     AND query ILIKE '%INSERT INTO search_node_projections%'\
                 )",
            )
            .fetch_one(&pool)
            .await
            .expect("observe worker lock wait");
            if waiting {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(25)).await;
        }
    })
    .await
    .expect("worker did not reach version fence while holding owner lock");

    let mut disable_connection = pool
        .acquire()
        .await
        .expect("acquire account-disable connection");
    let mut disable_task = tokio::spawn(async move {
        sqlx::query("UPDATE domain_accounts SET disabled=TRUE WHERE id=$1")
            .bind(OWNER_ID)
            .execute(&mut *disable_connection)
            .await
    });
    assert!(
        tokio::time::timeout(std::time::Duration::from_millis(250), &mut disable_task,)
            .await
            .is_err(),
        "owner disable bypassed the real worker's account lock",
    );

    version_blocker
        .commit()
        .await
        .expect("release node version for worker");
    let worker_outcome = tokio::time::timeout(std::time::Duration::from_secs(10), worker_task)
        .await
        .expect("worker remained blocked after version release")
        .expect("join real worker race proof")
        .expect("process private projection in race proof");
    assert_eq!(worker_outcome, ProcessOutcome::Processed);

    let disabled_result = disable_task
        .await
        .expect("join concurrent owner disable")
        .expect("disable owner after worker commit");
    assert_eq!(disabled_result.rows_affected(), 1);
    assert_redacted(&projection(&pool, &generation).await);
}
