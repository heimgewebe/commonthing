//! Integration proof: OPT-ARC-001 Phase E-C edge-create PostgreSQL write path.
//!
//! Proves that, when `domain_read_source=postgres` and
//! `domain_edge_write_source=postgres`, the internal derived-Faden projector inserts exactly one row
//! into `domain_edges` (plain INSERT, duplicate id -> 409), updates the
//! in-memory edge cache only after the successful insert, never touches JSONL,
//! and that `load_edges_from_postgres` reconstructs the stored edge including
//! `source_type`, `target_type`, `note` and `created_at`.
//!
//! Phase scope: edge creates only. Step-up email persistence and WebAuthn
//! user-id writeback persistence are NOT implemented; no cutover, no dual-write.
//!
//! Run with:
//!   DATABASE_URL=postgres://welt:gewebe@localhost:5432/weltgewebe \
//!     cargo test --locked -p weltgewebe-api --test db_domain_edge_write_path \
//!     -- --include-ignored --test-threads=1
//!
//! Notes:
//! - Tests are ignored by default to keep offline paths green.
//! - DATABASE_URL must point to direct PostgreSQL (not PgBouncer at :6432).
//! - Fixture rows use the `edec0000-` id prefix (valid UUID hex) and are
//!   cleaned before/after.

mod support;

use anyhow::{Context, Result};
use axum::{
    body,
    http::{Request, StatusCode},
    middleware::{from_fn, from_fn_with_state},
    routing::post,
    Router,
};
use serial_test::serial;
use sqlx::PgPool;
use std::{path::PathBuf, sync::Arc};
use tokio::sync::RwLock;
use tower::ServiceExt;
use weltgewebe_api::{
    auth::{rate_limit::AuthRateLimiter, role::Role, session::SessionBackend},
    config::{
        AppConfig, DomainAccountWriteSource, DomainEdgeWriteSource, DomainNodeWriteSource,
        DomainReadSource,
    },
    domain_db::{
        insert_domain_node, load_accounts_from_postgres, load_edges_from_postgres,
        load_nodes_from_postgres,
    },
    middleware::{auth::auth_middleware, authz::require_write, csrf::require_csrf},
    routes::{api_router, edges::create_edge},
    state::ApiState,
    telemetry::{BuildInfo, Metrics},
};

mod helpers;
use helpers::{read_account_details, set_gewebe_in_dir, test_node};

fn direct_database_url() -> String {
    let url = std::env::var("DATABASE_URL")
        .expect("DATABASE_URL must point at a direct PostgreSQL database (port 5432)");
    assert!(
        !url.contains(":6432"),
        "DATABASE_URL must target direct PostgreSQL, not PgBouncer (port 6432)"
    );
    support::postgres_proof::validated_direct_disposable_url(url)
}

async fn connect_pool() -> PgPool {
    PgPool::connect(&direct_database_url())
        .await
        .expect("connect to direct PostgreSQL")
}

async fn run_migrations(pool: &PgPool) {
    let migrations_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("migrations");
    let migrator = sqlx::migrate::Migrator::new(migrations_dir)
        .await
        .expect("failed to load migrations");
    migrator.run(pool).await.expect("failed to run migrations");
}

// Fixture ids are UUID-formatted (the create contract validates them) and
// share the `edec0000-` hex prefix so cleanup can target them via LIKE.
const EDGE_ID_A: &str = "edec0000-0000-4000-8000-0000000000a1";
const EDGE_ID_DUP: &str = "edec0000-0000-4000-8000-0000000000d1";
const NODE_ID: &str = "edec0000-0000-4000-8000-00000000005a";
const ACCOUNT_ID: &str = "edec0000-0000-4000-8000-00000000005b";

async fn clean(pool: &PgPool) {
    sqlx::query(
        "DELETE FROM domain_edges \
         WHERE id LIKE 'edec0000-%' \
            OR source_id IN ($1, $2) \
            OR target_id IN ($1, $2)",
    )
    .bind(NODE_ID)
    .bind(ACCOUNT_ID)
    .execute(pool)
    .await
    .expect("clean domain_edges fixtures");
    sqlx::query("DELETE FROM domain_nodes WHERE id = $1")
        .bind(NODE_ID)
        .execute(pool)
        .await
        .expect("clean domain_nodes fixture");
    sqlx::query(
        "DELETE FROM domain_accounts \
         WHERE id = $1 OR id LIKE 'writepath-edge-%'",
    )
    .bind(ACCOUNT_ID)
    .execute(pool)
    .await
    .expect("clean domain_accounts fixtures");
}

async fn ensure_operator_account(pool: &PgPool, id: &str, role: &Role) -> Result<()> {
    let role_name = match role {
        Role::Gast => "gast",
        Role::Weber => "weber",
        Role::Admin => "admin",
    };
    sqlx::query(
        "INSERT INTO domain_accounts \
            (id, kind, title, map_state, radius_m, disabled, role, webauthn_user_id, \
             public_payload, private_payload) \
         VALUES ($1, 'garnrolle', $2, 'not_on_map', 0, false, $3, $4::uuid, '{}'::jsonb, '{}'::jsonb) \
         ON CONFLICT (id) DO UPDATE SET role = EXCLUDED.role, title = EXCLUDED.title",
    )
    .bind(id)
    .bind(format!("Writer {id}"))
    .bind(role_name)
    .bind(uuid::Uuid::new_v4().to_string())
    .execute(pool)
    .await?;
    Ok(())
}

async fn fixture_row_count(pool: &PgPool) -> i64 {
    let (count,): (i64,) =
        sqlx::query_as("SELECT COUNT(*) FROM domain_edges WHERE id LIKE 'edec0000-%'")
            .fetch_one(pool)
            .await
            .expect("count domain_edges fixtures");
    count
}

fn test_metrics() -> Metrics {
    Metrics::try_new(BuildInfo {
        version: "test",
        commit: "test",
        build_timestamp: "test",
    })
    .expect("metrics")
}

fn write_path_config(
    domain_read_source: DomainReadSource,
    domain_edge_write_source: DomainEdgeWriteSource,
) -> AppConfig {
    AppConfig {
        max_guest_owned_nodes: 1_000,
        domain_read_source,
        domain_account_write_source: DomainAccountWriteSource::Postgres,
        domain_node_write_source: DomainNodeWriteSource::Postgres,
        domain_edge_write_source,
        passkey_credential_source: weltgewebe_api::config::PasskeyCredentialSource::InMemory,
        auth_public_login: false,
        auth_cookie_secure: weltgewebe_api::config::auth_cookie_secure_env_override()
            .unwrap_or(true),
        app_base_url: None,
        auth_trusted_proxies: None,
        auth_allow_emails: None,
        auth_allow_email_domains: None,
        auth_auto_provision: false,
        auth_auto_provision_role: weltgewebe_api::config::AutoProvisionRole::Gast,
        auth_rl_ip_per_min: None,
        auth_rl_ip_per_hour: None,
        auth_rl_email_per_min: None,
        auth_rl_email_per_hour: None,
        node_mutation_rate_limits: Default::default(),
        smtp_host: None,
        smtp_port: None,
        smtp_user: None,
        smtp_pass: None,
        smtp_from: None,
        auth_log_magic_token: false,
        webauthn_rp_id: None,
        webauthn_rp_origin: None,
        webauthn_rp_name: None,
    }
}

/// App with a live pool, a writer session, and the given read/edge-write
/// sources. The edge cache is loaded from PostgreSQL, mirroring startup.
async fn edge_write_app(
    pool: PgPool,
    operator_id: &str,
    operator_role: Role,
    domain_read_source: DomainReadSource,
    domain_edge_write_source: DomainEdgeWriteSource,
) -> Result<(Router, String, ApiState)> {
    ensure_operator_account(&pool, operator_id, &operator_role).await?;
    let accounts = load_accounts_from_postgres(&pool)
        .await
        .context("load accounts for test")?;
    let nodes = load_nodes_from_postgres(&pool)
        .await
        .context("load nodes for test")?;
    let edges = load_edges_from_postgres(&pool)
        .await
        .context("load edges for test")?;

    let config = write_path_config(domain_read_source, domain_edge_write_source);
    let rate_limiter = Arc::new(AuthRateLimiter::new(&config));

    let state = ApiState {
        db_pool: Some(pool),
        db_pool_configured: true,
        nats_client: None,
        nats_configured: false,
        config,
        metrics: test_metrics(),
        sessions: SessionBackend::new_in_memory(),
        challenges: Default::default(),
        tokens: weltgewebe_api::auth::tokens::TokenStore::new(),
        step_up_tokens: weltgewebe_api::auth::step_up_tokens::StepUpTokenStore::new(),
        accounts: Arc::new(RwLock::new(accounts)),
        nodes: Arc::new(RwLock::new(nodes)),
        nodes_persist: Arc::new(tokio::sync::Mutex::new(())),
        accounts_persist: Arc::new(tokio::sync::Mutex::new(())),
        domain_projection_gate: std::sync::Arc::new(tokio::sync::RwLock::new(())),
        domain_projection_reload: std::sync::Arc::new(tokio::sync::Mutex::new(())),
        domain_projection_version: std::sync::Arc::new(std::sync::atomic::AtomicI64::new(0)),
        edges: Arc::new(RwLock::new(edges)),
        rate_limiter,
        mailer: None,
        webauthn: None,
        passkey_registrations: Default::default(),
        passkey_registration_grants: Default::default(),
        passkey_authentications: Default::default(),
        passkeys: Default::default(),
        web_push: None,
    };

    let session = state
        .sessions
        .create(operator_id.to_string(), None)
        .await
        .expect("session create");
    let cookie = format!("gewebe_session={}", session.id);

    let app = Router::new()
        .merge(api_router())
        .route(
            "/__test/derived-edges",
            post(create_edge).route_layer(from_fn(require_write)),
        )
        .layer(from_fn_with_state(state.clone(), auth_middleware))
        .layer(axum::middleware::from_fn(require_csrf))
        .with_state(state.clone());

    Ok((app, cookie, state))
}

fn post_edges_req(cookie: &str, json_body: &str) -> Request<body::Body> {
    Request::builder()
        .method("POST")
        .uri("/__test/derived-edges")
        .header("Content-Type", "application/json")
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .header("Cookie", cookie)
        .body(body::Body::from(json_body.to_string()))
        .unwrap()
}

async fn read_json_body(res: axum::response::Response) -> Result<serde_json::Value> {
    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    Ok(serde_json::from_slice(&bytes)?)
}

async fn read_text_body(res: axum::response::Response) -> Result<String> {
    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    Ok(String::from_utf8_lossy(&bytes).into_owned())
}

fn create_body(id: &str, note: Option<&str>) -> String {
    match note {
        Some(note) => format!(
            r#"{{"id":"{id}","source_id":"{NODE_ID}","source_type":"node","target_id":"{ACCOUNT_ID}","target_type":"account","edge_kind":"reference","note":"{note}"}}"#
        ),
        None => format!(
            r#"{{"id":"{id}","source_id":"{NODE_ID}","source_type":"node","target_id":"{ACCOUNT_ID}","target_type":"account","edge_kind":"reference"}}"#
        ),
    }
}

/// Account endpoint authorization is enforced before PostgreSQL persistence.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn postgres_weber_cannot_create_node_to_account_edge() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, state) = edge_write_app(
        pool.clone(),
        ACCOUNT_ID,
        Role::Weber,
        DomainReadSource::Postgres,
        DomainEdgeWriteSource::Postgres,
    )
    .await?;
    let disguised_account_body = format!(
        r#"{{"id":"{EDGE_ID_A}","source_id":"{ACCOUNT_ID}","source_type":"node","target_id":"{NODE_ID}","target_type":"node","edge_kind":"reference","note":"must not persist"}}"#
    );
    let response = app
        .oneshot(post_edges_req(&cookie, &disguised_account_body))
        .await?;
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    assert_eq!(fixture_row_count(&pool).await, 0);
    assert!(state.edges.read().await.get(EDGE_ID_A).is_none());
    assert!(!in_dir.join("demo.edges.jsonl").exists());

    clean(&pool).await;
    Ok(())
}

/// A. PostgreSQL create persists one row, updates the cache, serves GET, and
/// the loader reconstructs the stored edge (three persistence levels: direct
/// row, cache/GET in-process, `load_edges_from_postgres` roundtrip).
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn postgres_edge_create_persists_row_cache_and_loader_roundtrip() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    insert_domain_node(
        &pool,
        &test_node(
            NODE_ID,
            "PostgreSQL Vorwärtsnachweis",
            Some("Persistente Garnrollen-Knotenbeziehung"),
        ),
        None,
    )
    .await
    .context("persist relation node fixture")?;

    let (app, cookie, state) = edge_write_app(
        pool.clone(),
        ACCOUNT_ID,
        Role::Admin,
        DomainReadSource::Postgres,
        DomainEdgeWriteSource::Postgres,
    )
    .await?;
    let res = app
        .clone()
        .oneshot(post_edges_req(
            &cookie,
            &create_body(EDGE_ID_A, Some("edge via postgres")),
        ))
        .await?;
    assert_eq!(res.status(), StatusCode::CREATED);
    let v = read_json_body(res).await?;
    assert_eq!(v["id"], EDGE_ID_A);
    assert_eq!(v["note"], "edge via postgres");
    let response_created_at = v["created_at"]
        .as_str()
        .context("created_at must be a string")?
        .to_string();
    let response_created = chrono::DateTime::parse_from_rfc3339(&response_created_at)
        .context("created_at must be RFC3339")?;

    // Level 1: exactly one direct domain_edges row with the expected mapping.
    assert_eq!(fixture_row_count(&pool).await, 1);
    let (source_id, target_id, edge_kind, db_created_at, payload_text): (
        String,
        String,
        String,
        Option<chrono::DateTime<chrono::Utc>>,
        String,
    ) = sqlx::query_as(
        "SELECT source_id, target_id, edge_kind, created_at, payload::text \
         FROM domain_edges WHERE id = $1",
    )
    .bind(EDGE_ID_A)
    .fetch_one(&pool)
    .await
    .expect("created edge row must exist");
    assert_eq!(source_id, NODE_ID);
    assert_eq!(target_id, ACCOUNT_ID);
    assert_eq!(edge_kind, "reference");
    // created_at must not silently vanish; PostgreSQL stores microseconds, the
    // response carries nanoseconds, so compare at microsecond precision.
    let db_created_at = db_created_at.context("created_at column must not be NULL")?;
    assert_eq!(
        db_created_at.timestamp_micros(),
        response_created.timestamp_micros(),
        "domain_edges.created_at must carry the server-owned create timestamp"
    );
    let payload: serde_json::Value = serde_json::from_str(&payload_text)?;
    assert_eq!(
        payload.get("source_type").and_then(|v| v.as_str()),
        Some("node")
    );
    assert_eq!(
        payload.get("target_type").and_then(|v| v.as_str()),
        Some("account")
    );
    assert_eq!(
        payload.get("note").and_then(|v| v.as_str()),
        Some("edge via postgres")
    );

    // No JSONL side effect in PostgreSQL mode.
    let edges_file = in_dir.join("demo.edges.jsonl");
    assert!(
        !edges_file.exists(),
        "PostgreSQL write mode must not create or write the JSONL edges file"
    );

    // Level 2: cache holds the edge (read-your-writes) and GET serves it in
    // the same process with the exact response values.
    {
        let cache = state.edges.read().await;
        let cached = cache.get(EDGE_ID_A).context("edge must be in cache")?;
        assert_eq!(
            cached.created_at.as_deref(),
            Some(response_created_at.as_str())
        );
        assert_eq!(cached.note.as_deref(), Some("edge via postgres"));
    }
    let uri = format!("/edges/{EDGE_ID_A}");
    let res = app
        .clone()
        .oneshot(
            Request::get(uri.as_str())
                .header("Host", "localhost")
                .body(body::Body::empty())?,
        )
        .await?;
    assert_eq!(res.status(), StatusCode::OK);
    let fetched = read_json_body(res).await?;
    assert_eq!(fetched["id"], EDGE_ID_A);
    assert_eq!(fetched["created_at"], response_created_at.as_str());
    assert!(
        fetched.get("note").is_none(),
        "public PostgreSQL edge detail must omit free-text notes"
    );

    let immediate_details = read_account_details(&app, ACCOUNT_ID).await?;
    assert_eq!(
        immediate_details["nodes"].as_array().map(Vec::len),
        Some(1),
        "the active Faden remains visible in the current node relation projection"
    );
    assert_eq!(immediate_details["nodes"][0]["node_id"], NODE_ID);
    assert_eq!(
        immediate_details["activity"].as_array().map(Vec::len),
        Some(0),
        "a raw incoming Faden must not invent account activity"
    );

    // Level 3: load_edges_from_postgres reconstructs the stored edge.
    let reloaded = load_edges_from_postgres(&pool)
        .await
        .context("reload edges")?;
    let edge = reloaded
        .get(EDGE_ID_A)
        .context("edge reloaded from postgres")?;
    assert_eq!(edge.source_id, NODE_ID);
    assert_eq!(edge.source_type.as_deref(), Some("node"));
    assert_eq!(edge.target_id, ACCOUNT_ID);
    assert_eq!(edge.target_type.as_deref(), Some("account"));
    assert_eq!(edge.edge_kind, "reference");
    assert_eq!(edge.note.as_deref(), Some("edge via postgres"));
    let reloaded_created = chrono::DateTime::parse_from_rfc3339(
        edge.created_at
            .as_deref()
            .context("reloaded edge must carry created_at")?,
    )?;
    assert_eq!(
        reloaded_created.timestamp_micros(),
        response_created.timestamp_micros(),
        "loader must reconstruct the stored create timestamp"
    );

    let (restarted_app, _restarted_cookie, restarted_state) = edge_write_app(
        pool.clone(),
        ACCOUNT_ID,
        Role::Admin,
        DomainReadSource::Postgres,
        DomainEdgeWriteSource::Postgres,
    )
    .await?;
    assert!(restarted_state
        .accounts
        .read()
        .await
        .get(ACCOUNT_ID)
        .is_some());
    assert!(restarted_state.nodes.read().await.get(NODE_ID).is_some());
    assert!(restarted_state.edges.read().await.get(EDGE_ID_A).is_some());
    let restarted_details = read_account_details(&restarted_app, ACCOUNT_ID).await?;
    assert_eq!(
        restarted_details["nodes"].as_array().map(Vec::len),
        Some(1),
        "the current relation survives a full PostgreSQL reload"
    );
    assert_eq!(restarted_details["nodes"][0]["node_id"], NODE_ID);
    assert_eq!(
        restarted_details["activity"].as_array().map(Vec::len),
        Some(0),
        "a reloaded raw Faden still must not become account history"
    );

    clean(&pool).await;
    Ok(())
}

/// An expired legacy origin Faden remains useful only as bounded attribution
/// evidence for the durable Knoten creation. It is not restored to the active
/// Knoten projection, and explicit node actor metadata supersedes the fallback.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn postgres_account_activity_reconstructs_expired_legacy_origin_faden() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;

    let node = test_node(NODE_ID, "Historischer Knoten", None);
    insert_domain_node(&pool, &node, None)
        .await
        .context("persist legacy activity node fixture")?;
    ensure_operator_account(&pool, ACCOUNT_ID, &Role::Admin).await?;

    let origin_created_at = chrono::DateTime::parse_from_rfc3339(&node.created_at)?
        .with_timezone(&chrono::Utc)
        + chrono::Duration::milliseconds(25);
    sqlx::query(
        "INSERT INTO domain_edges \
            (id, source_id, target_id, edge_kind, created_at, payload) \
         VALUES ($1, $2, $3, 'reference', $4, \
                 jsonb_build_object( \
                     'source_type', 'account', \
                     'target_type', 'node' \
                 ))",
    )
    .bind(EDGE_ID_A)
    .bind(ACCOUNT_ID)
    .bind(NODE_ID)
    .bind(origin_created_at)
    .execute(&pool)
    .await
    .context("persist expired legacy origin Faden")?;

    let (app, _cookie, _state) = edge_write_app(
        pool.clone(),
        ACCOUNT_ID,
        Role::Admin,
        DomainReadSource::Postgres,
        DomainEdgeWriteSource::Postgres,
    )
    .await?;
    let legacy_details = read_account_details(&app, ACCOUNT_ID).await?;
    assert_eq!(
        legacy_details["nodes"].as_array().map(Vec::len),
        Some(0),
        "the expired Faden must stay absent from the current Knoten projection"
    );
    assert_eq!(legacy_details["activity"].as_array().map(Vec::len), Some(1));
    assert_eq!(
        legacy_details["activity"][0]["event"],
        "Hat den Knoten \"Historischer Knoten\" geknüpft."
    );
    let projected_at = chrono::DateTime::parse_from_rfc3339(
        legacy_details["activity"][0]["date"]
            .as_str()
            .context("legacy creation activity date")?,
    )?;
    let node_created_at = chrono::DateTime::parse_from_rfc3339(&node.created_at)?;
    assert_eq!(projected_at, node_created_at);

    sqlx::query("UPDATE domain_edges SET payload = '{}'::jsonb WHERE id = $1")
        .bind(EDGE_ID_A)
        .execute(&pool)
        .await
        .context("remove endpoint types from legacy edge")?;
    let untyped_details = read_account_details(&app, ACCOUNT_ID).await?;
    assert_eq!(
        untyped_details["activity"].as_array().map(Vec::len),
        Some(0),
        "an untyped legacy relation must not be guessed into creator activity"
    );
    sqlx::query(
        "UPDATE domain_edges \
         SET payload = jsonb_build_object( \
             'source_type', 'account', \
             'target_type', 'node' \
         ) \
         WHERE id = $1",
    )
    .bind(EDGE_ID_A)
    .execute(&pool)
    .await
    .context("restore typed legacy origin edge")?;

    sqlx::query(
        "UPDATE domain_nodes \
         SET create_actor_id = $2, \
             create_operation_id = 'edec0000-0000-4000-8000-0000000000c1' \
         WHERE id = $1",
    )
    .bind(NODE_ID)
    .bind(ACCOUNT_ID)
    .execute(&pool)
    .await
    .context("bind explicit create-operation metadata")?;

    let explicit_with_origin = read_account_details(&app, ACCOUNT_ID).await?;
    assert_eq!(
        explicit_with_origin["activity"].as_array().map(Vec::len),
        Some(1),
        "create-operation metadata may corroborate the still-linked origin Faden"
    );

    sqlx::query("DELETE FROM domain_edges WHERE id = $1")
        .bind(EDGE_ID_A)
        .execute(&pool)
        .await
        .context("simulate identity unlinking by removing the origin Faden")?;
    let unlinked_details = read_account_details(&app, ACCOUNT_ID).await?;
    assert_eq!(
        unlinked_details["activity"].as_array().map(Vec::len),
        Some(0),
        "create_actor_id alone must not revive history after identity unlinking"
    );

    sqlx::query(
        "UPDATE domain_nodes \
         SET created_at = NULL, \
             payload = payload || jsonb_build_object('created_by_account_id', $2::text) \
         WHERE id = $1",
    )
    .bind(NODE_ID)
    .bind(ACCOUNT_ID)
    .execute(&pool)
    .await
    .context("make owned legacy node lack its creation timestamp")?;
    let half_dated_details = read_account_details(&app, ACCOUNT_ID).await?;
    assert_eq!(
        half_dated_details["activity"].as_array().map(Vec::len),
        Some(0),
        "updated_at must not be substituted for a missing creation timestamp"
    );

    sqlx::query("UPDATE domain_nodes SET updated_at = NULL WHERE id = $1")
        .bind(NODE_ID)
        .execute(&pool)
        .await
        .context("make owned legacy node fully undated")?;
    let undated_details = read_account_details(&app, ACCOUNT_ID).await?;
    assert_eq!(
        undated_details["activity"].as_array().map(Vec::len),
        Some(0),
        "a fully undated owned legacy node is skipped instead of failing account details"
    );

    clean(&pool).await;
    Ok(())
}

/// B. An account-scoped operation survives a simulated API restart and returns
/// the same server-owned edge id. Reusing the key for different semantic data
/// is rejected without a second row.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn postgres_edge_create_operation_replays_after_restart() -> Result<()> {
    const ACTOR_ID: &str = "writepath-edge-idempotency-actor";
    const OPERATION_ID: &str = "40000000-0000-0000-0000-000000000001";

    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);
    let request_body = format!(
        r#"{{"source_id":"{NODE_ID}","source_type":"node","target_id":"{ACCOUNT_ID}","target_type":"account","edge_kind":"reference","note":"durable","operation_id":"{OPERATION_ID}"}}"#
    );

    let (first_app, first_cookie, _) = edge_write_app(
        pool.clone(),
        ACTOR_ID,
        Role::Admin,
        DomainReadSource::Postgres,
        DomainEdgeWriteSource::Postgres,
    )
    .await?;
    let first_attempt = first_app
        .clone()
        .oneshot(post_edges_req(&first_cookie, &request_body));
    let second_attempt = first_app
        .clone()
        .oneshot(post_edges_req(&first_cookie, &request_body));
    let (first, second) = tokio::join!(first_attempt, second_attempt);
    let first = first?;
    let second = second?;
    let first_status = first.status();
    let second_status = second.status();
    assert!(
        (first_status == StatusCode::CREATED && second_status == StatusCode::OK)
            || (first_status == StatusCode::OK && second_status == StatusCode::CREATED),
        "parallel PostgreSQL retries must produce one 201 and one 200, got {first_status} and {second_status}"
    );
    let first_edge = read_json_body(first).await?;
    let second_edge = read_json_body(second).await?;
    assert_eq!(first_edge["id"], second_edge["id"]);
    let first_id = first_edge["id"].as_str().context("created edge id")?;

    let (restarted_app, restarted_cookie, restarted_state) = edge_write_app(
        pool.clone(),
        ACTOR_ID,
        Role::Admin,
        DomainReadSource::Postgres,
        DomainEdgeWriteSource::Postgres,
    )
    .await?;
    let replay = restarted_app
        .clone()
        .oneshot(post_edges_req(&restarted_cookie, &request_body))
        .await?;
    assert_eq!(replay.status(), StatusCode::OK);
    let replay_edge = read_json_body(replay).await?;
    assert_eq!(replay_edge["id"], first_id);
    assert_eq!(restarted_state.edges.read().await.len(), 1);

    let changed_body = format!(
        r#"{{"source_id":"{NODE_ID}","source_type":"node","target_id":"{ACCOUNT_ID}","target_type":"account","edge_kind":"reference","note":"changed","operation_id":"{OPERATION_ID}"}}"#
    );
    let conflict = restarted_app
        .oneshot(post_edges_req(&restarted_cookie, &changed_body))
        .await?;
    assert_eq!(conflict.status(), StatusCode::CONFLICT);

    let (count, actor, operation): (i64, Option<String>, Option<String>) = sqlx::query_as(
        "SELECT count(*), min(create_actor_id), min(create_operation_id) FROM domain_edges",
    )
    .fetch_one(&pool)
    .await?;
    assert_eq!(count, 1);
    assert_eq!(actor.as_deref(), Some(ACTOR_ID));
    assert_eq!(operation.as_deref(), Some(OPERATION_ID));

    clean(&pool).await;
    Ok(())
}

/// C. Absent note stays absent: the payload carries no `note` key (never
/// `note: null`), and the loader reconstructs `note = None`.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn postgres_edge_create_omits_note_key_when_absent() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, _state) = edge_write_app(
        pool.clone(),
        "writepath-edge-writer-2",
        Role::Admin,
        DomainReadSource::Postgres,
        DomainEdgeWriteSource::Postgres,
    )
    .await?;

    let res = app
        .oneshot(post_edges_req(&cookie, &create_body(EDGE_ID_A, None)))
        .await?;
    assert_eq!(res.status(), StatusCode::CREATED);

    let (payload_text,): (String,) =
        sqlx::query_as("SELECT payload::text FROM domain_edges WHERE id = $1")
            .bind(EDGE_ID_A)
            .fetch_one(&pool)
            .await
            .expect("created edge row must exist");
    let payload: serde_json::Value = serde_json::from_str(&payload_text)?;
    assert!(
        payload.get("note").is_none(),
        "absent note must be omitted from the payload, never null: {payload_text}"
    );
    assert_eq!(
        payload.get("source_type").and_then(|v| v.as_str()),
        Some("node")
    );
    assert_eq!(
        payload.get("target_type").and_then(|v| v.as_str()),
        Some("account")
    );

    let reloaded = load_edges_from_postgres(&pool).await?;
    let edge = reloaded
        .get(EDGE_ID_A)
        .context("edge reloaded from postgres")?;
    assert_eq!(edge.note, None);

    clean(&pool).await;
    Ok(())
}

/// D. Duplicate id from the database surfaces as 409 (plain INSERT, no
/// ON CONFLICT): the second create leaves exactly one row and no extra cache
/// entry.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn postgres_edge_create_duplicate_id_returns_409() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;

    // Seed the duplicate target directly in the DB so the in-memory cache of a
    // *fresh* app instance knows it too — then bypass the cache by seeding only
    // the DB row after app construction for the genuinely DB-level conflict.
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, state) = edge_write_app(
        pool.clone(),
        "writepath-edge-writer-3",
        Role::Admin,
        DomainReadSource::Postgres,
        DomainEdgeWriteSource::Postgres,
    )
    .await?;

    // Insert the conflicting row AFTER the cache was loaded, so the route's
    // cache-level duplicate check cannot see it and the 409 must come from the
    // database unique violation.
    sqlx::query(
        "INSERT INTO domain_edges (id, source_id, target_id, edge_kind, created_at, payload) \
         VALUES ($1, $2, $3, 'reference', NOW(), '{}'::jsonb)",
    )
    .bind(EDGE_ID_DUP)
    .bind(NODE_ID)
    .bind(ACCOUNT_ID)
    .execute(&pool)
    .await
    .expect("seed conflicting edge row");

    let res = app
        .oneshot(post_edges_req(&cookie, &create_body(EDGE_ID_DUP, None)))
        .await?;
    assert_eq!(res.status(), StatusCode::CONFLICT);
    let text = read_text_body(res).await?;
    assert!(text.contains("edge id already exists"), "body: {text}");

    // Still exactly one row; the failed create left no phantom cache entry.
    let (count,): (i64,) = sqlx::query_as("SELECT COUNT(*) FROM domain_edges WHERE id = $1")
        .bind(EDGE_ID_DUP)
        .fetch_one(&pool)
        .await?;
    assert_eq!(count, 1);
    assert!(
        state.edges.read().await.get(EDGE_ID_DUP).is_none(),
        "rejected duplicate create must not insert into the cache"
    );

    clean(&pool).await;
    Ok(())
}

/// E. Postgres read + JSONL edge write is blocked with 409 and writes nothing.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn postgres_read_jsonl_edge_write_is_blocked() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, state) = edge_write_app(
        pool.clone(),
        "writepath-edge-writer-4",
        Role::Admin,
        DomainReadSource::Postgres,
        DomainEdgeWriteSource::Jsonl,
    )
    .await?;

    let res = app
        .oneshot(post_edges_req(&cookie, &create_body(EDGE_ID_A, None)))
        .await?;
    assert_eq!(res.status(), StatusCode::CONFLICT);
    let text = read_text_body(res).await?;
    assert!(
        text.contains("DOMAIN_READ_SOURCE_READ_ONLY"),
        "body: {text}"
    );

    // No DB row, no JSONL write, no cache entry.
    assert_eq!(fixture_row_count(&pool).await, 0);
    assert!(
        !in_dir.join("demo.edges.jsonl").exists(),
        "blocked create must not write JSONL"
    );
    assert!(state.edges.read().await.get(EDGE_ID_A).is_none());

    clean(&pool).await;
    Ok(())
}

/// F. Defensive invalid-config state (JSONL read + Postgres edge write, which
/// config load forbids) is rejected with 500 and writes nothing.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn jsonl_read_postgres_edge_write_invalid_config_returns_500() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, state) = edge_write_app(
        pool.clone(),
        "writepath-edge-writer-5",
        Role::Admin,
        DomainReadSource::Jsonl,
        DomainEdgeWriteSource::Postgres,
    )
    .await?;

    let res = app
        .oneshot(post_edges_req(&cookie, &create_body(EDGE_ID_A, None)))
        .await?;
    assert_eq!(res.status(), StatusCode::INTERNAL_SERVER_ERROR);
    let text = read_text_body(res).await?;
    assert!(text.contains("INVALID_DOMAIN_WRITE_CONFIG"), "body: {text}");

    // No DB row, no JSONL write, no cache entry.
    assert_eq!(fixture_row_count(&pool).await, 0);
    assert!(
        !in_dir.join("demo.edges.jsonl").exists(),
        "invalid-config create must not write JSONL"
    );
    assert!(state.edges.read().await.get(EDGE_ID_A).is_none());

    clean(&pool).await;
    Ok(())
}

struct EnvVarGuard {
    key: &'static str,
    old: Option<String>,
}

impl EnvVarGuard {
    fn set(key: &'static str, value: String) -> Self {
        let old = std::env::var(key).ok();
        std::env::set_var(key, value);
        Self { key, old }
    }
}

impl Drop for EnvVarGuard {
    fn drop(&mut self) {
        if let Some(old) = &self.old {
            std::env::set_var(self.key, old);
        } else {
            std::env::remove_var(self.key);
        }
    }
}

/// G. PostgreSQL create rejects when domain_edges is already at MAX_EDGES_CACHE.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn postgres_edge_create_rejects_when_cache_limit_reached() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;

    let (base_count,): (i64,) = sqlx::query_as("SELECT COUNT(*) FROM domain_edges")
        .fetch_one(&pool)
        .await?;

    // Set MAX_EDGES_CACHE to exactly one more than the base count.
    let target_limit = base_count + 1;
    let _env_guard = EnvVarGuard::set("MAX_EDGES_CACHE", target_limit.to_string());

    // Insert one fixture to exactly hit the limit.
    sqlx::query(
        "INSERT INTO domain_edges (id, source_id, target_id, edge_kind, created_at, payload) \
         VALUES ($1, $2, $3, 'reference', NOW(), '{}'::jsonb)",
    )
    .bind(EDGE_ID_DUP)
    .bind(NODE_ID)
    .bind(ACCOUNT_ID)
    .execute(&pool)
    .await
    .expect("seed limit-filling edge row");

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, state) = edge_write_app(
        pool.clone(),
        "writepath-edge-writer-limit",
        Role::Admin,
        DomainReadSource::Postgres,
        DomainEdgeWriteSource::Postgres,
    )
    .await?;

    let res = app
        .oneshot(post_edges_req(&cookie, &create_body(EDGE_ID_A, None)))
        .await?;
    assert_eq!(res.status(), StatusCode::CONFLICT);
    let text = read_text_body(res).await?;
    assert!(text.contains("edge cache limit reached"), "body: {text}");

    // No new DB row, no JSONL write, no cache entry.
    let (total_count,): (i64,) = sqlx::query_as("SELECT COUNT(*) FROM domain_edges")
        .fetch_one(&pool)
        .await?;
    assert_eq!(total_count, target_limit);
    assert!(
        !in_dir.join("demo.edges.jsonl").exists(),
        "blocked create must not write JSONL"
    );
    assert!(state.edges.read().await.get(EDGE_ID_A).is_none());

    clean(&pool).await;
    Ok(())
}

/// A dated row with an explicit `expires_at: null` payload is permanently
/// unreachable per `edge_is_active_at` and the read loader excludes it from
/// cache capacity; the write-capacity check must not still count it, or a
/// row declared not to consume cache capacity would permanently block
/// otherwise-valid creates.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn postgres_edge_create_admits_when_limit_filled_by_permanently_unreachable_row() -> Result<()>
{
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;

    let (base_count,): (i64,) = sqlx::query_as("SELECT COUNT(*) FROM domain_edges")
        .fetch_one(&pool)
        .await?;

    let target_limit = base_count + 1;
    let _env_guard = EnvVarGuard::set("MAX_EDGES_CACHE", target_limit.to_string());

    // Fill the "limit" with a permanently unreachable row instead of a
    // genuinely reachable one.
    sqlx::query(
        "INSERT INTO domain_edges (id, source_id, target_id, edge_kind, created_at, payload) \
         VALUES ($1, $2, $3, 'reference', '2026-06-01T00:00:00Z', '{\"expires_at\": null}'::jsonb)",
    )
    .bind(EDGE_ID_DUP)
    .bind(NODE_ID)
    .bind(ACCOUNT_ID)
    .execute(&pool)
    .await
    .expect("seed permanently unreachable edge row");

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, state) = edge_write_app(
        pool.clone(),
        "writepath-edge-writer-limit-unreachable",
        Role::Admin,
        DomainReadSource::Postgres,
        DomainEdgeWriteSource::Postgres,
    )
    .await?;
    assert!(state.edges.read().await.get(EDGE_ID_DUP).is_none());

    let res = app
        .oneshot(post_edges_req(&cookie, &create_body(EDGE_ID_A, None)))
        .await?;
    assert_eq!(res.status(), StatusCode::CREATED);
    assert!(state.edges.read().await.get(EDGE_ID_A).is_some());

    clean(&pool).await;
    Ok(())
}

/// Same as above, but the permanently unreachable row carries a malformed
/// (non-string, non-null) `expires_at` payload value instead of an explicit
/// `null` — `edge_is_permanently_unreachable` rejects this shape via the
/// same `jsonb_typeof` check regardless of `created_at`.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn postgres_edge_create_admits_when_limit_filled_by_malformed_expiry_row() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;

    let (base_count,): (i64,) = sqlx::query_as("SELECT COUNT(*) FROM domain_edges")
        .fetch_one(&pool)
        .await?;

    let target_limit = base_count + 1;
    let _env_guard = EnvVarGuard::set("MAX_EDGES_CACHE", target_limit.to_string());

    sqlx::query(
        "INSERT INTO domain_edges (id, source_id, target_id, edge_kind, created_at, payload) \
         VALUES ($1, $2, $3, 'reference', '2026-06-01T00:00:00Z', '{\"expires_at\": 12345}'::jsonb)",
    )
    .bind(EDGE_ID_DUP)
    .bind(NODE_ID)
    .bind(ACCOUNT_ID)
    .execute(&pool)
    .await
    .expect("seed malformed edge row");

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, state) = edge_write_app(
        pool.clone(),
        "writepath-edge-writer-limit-malformed",
        Role::Admin,
        DomainReadSource::Postgres,
        DomainEdgeWriteSource::Postgres,
    )
    .await?;
    assert!(state.edges.read().await.get(EDGE_ID_DUP).is_none());

    let res = app
        .oneshot(post_edges_req(&cookie, &create_body(EDGE_ID_A, None)))
        .await?;
    assert_eq!(res.status(), StatusCode::CREATED);
    assert!(state.edges.read().await.get(EDGE_ID_A).is_some());

    clean(&pool).await;
    Ok(())
}

/// Same as above, but the permanently unreachable row is undated
/// (`created_at IS NULL`) with a concrete `expires_at` string — the
/// "expiring edge without created_at" shape `edge_is_permanently_unreachable`
/// also rejects.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn postgres_edge_create_admits_when_limit_filled_by_undated_row_with_expiry() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;

    let (base_count,): (i64,) = sqlx::query_as("SELECT COUNT(*) FROM domain_edges")
        .fetch_one(&pool)
        .await?;

    let target_limit = base_count + 1;
    let _env_guard = EnvVarGuard::set("MAX_EDGES_CACHE", target_limit.to_string());

    sqlx::query(
        "INSERT INTO domain_edges (id, source_id, target_id, edge_kind, created_at, payload) \
         VALUES ($1, $2, $3, 'reference', NULL, \
         '{\"expires_at\": \"2026-06-08T00:00:00Z\"}'::jsonb)",
    )
    .bind(EDGE_ID_DUP)
    .bind(NODE_ID)
    .bind(ACCOUNT_ID)
    .execute(&pool)
    .await
    .expect("seed undated edge row with a concrete expiry");

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, state) = edge_write_app(
        pool.clone(),
        "writepath-edge-writer-limit-undated",
        Role::Admin,
        DomainReadSource::Postgres,
        DomainEdgeWriteSource::Postgres,
    )
    .await?;
    assert!(state.edges.read().await.get(EDGE_ID_DUP).is_none());

    let res = app
        .oneshot(post_edges_req(&cookie, &create_body(EDGE_ID_A, None)))
        .await?;
    assert_eq!(res.status(), StatusCode::CREATED);
    assert!(state.edges.read().await.get(EDGE_ID_A).is_some());

    clean(&pool).await;
    Ok(())
}

/// A dated row whose payload is a JSON *array* rather than an object (e.g.
/// `["expires_at"]`) is not permanently unreachable: `payload_lifecycle_field`
/// looks up keys via `Value::get`, which returns `None` for any non-object
/// value regardless of content, so the loader treats it exactly like an
/// empty object payload — a reachable dated legacy edge with an omitted
/// expiry. The write-capacity check must count it too (unlike the
/// permanently-unreachable rows above), so a create at capacity must still
/// be rejected.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn postgres_edge_create_rejects_when_limit_filled_by_non_object_payload_row() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;

    let (base_count,): (i64,) = sqlx::query_as("SELECT COUNT(*) FROM domain_edges")
        .fetch_one(&pool)
        .await?;

    let target_limit = base_count + 1;
    let _env_guard = EnvVarGuard::set("MAX_EDGES_CACHE", target_limit.to_string());

    // A non-object payload happening to contain the string "expires_at" as
    // an array element: PostgreSQL's `?` operator matches array elements as
    // well as object keys, which is exactly the trap a naive exclusion falls
    // into.
    sqlx::query(
        "INSERT INTO domain_edges (id, source_id, target_id, edge_kind, created_at, payload) \
         VALUES ($1, $2, $3, 'reference', '2026-06-01T00:00:00Z', '[\"expires_at\"]'::jsonb)",
    )
    .bind(EDGE_ID_DUP)
    .bind(NODE_ID)
    .bind(ACCOUNT_ID)
    .execute(&pool)
    .await
    .expect("seed non-object-payload edge row");

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, state) = edge_write_app(
        pool.clone(),
        "writepath-edge-writer-limit-nonobject",
        Role::Admin,
        DomainReadSource::Postgres,
        DomainEdgeWriteSource::Postgres,
    )
    .await?;
    // The loader admits this row as a reachable dated legacy edge, occupying
    // the single available slot.
    assert!(state.edges.read().await.get(EDGE_ID_DUP).is_some());

    let res = app
        .oneshot(post_edges_req(&cookie, &create_body(EDGE_ID_A, None)))
        .await?;
    assert_eq!(res.status(), StatusCode::CONFLICT);
    let text = read_text_body(res).await?;
    assert!(text.contains("edge cache limit reached"), "body: {text}");
    assert!(state.edges.read().await.get(EDGE_ID_A).is_none());

    clean(&pool).await;
    Ok(())
}
