//! Integration proof: OPT-ARC-001 Phase E-B node-patch PostgreSQL write path.
//!
//! Proves that, when `domain_read_source=postgres` and
//! `domain_node_write_source=postgres`, `PATCH /nodes/{id}` writes the patch to
//! `domain_nodes`, updates the in-memory node cache, never touches JSONL, and
//! that `load_nodes_from_postgres` reconstructs the same projection.
//!
//! Phase scope: node patches only. Account writes, edge writes, step-up email
//! persistence and WebAuthn user-id writeback persistence are NOT implemented.
//!
//! Run with:
//!   DATABASE_URL=postgres://welt:gewebe@localhost:5432/weltgewebe \
//!     cargo test --locked -p weltgewebe-api --test db_domain_node_write_path \
//!     -- --include-ignored --test-threads=1
//!
//! Notes:
//! - Tests are ignored by default to keep offline paths green.
//! - DATABASE_URL must point to direct PostgreSQL (not PgBouncer at :6432).
//! - Fixture rows use the `writepath-node-` id prefix and are cleaned before/after.

mod support;

use anyhow::{Context, Result};
use axum::{
    body,
    http::{Request, StatusCode},
    middleware::from_fn_with_state,
    Router,
};
use serial_test::serial;
use sha2::{Digest, Sha256};
use sqlx::{postgres::PgPoolOptions, Executor, PgPool};
use std::{
    ffi::OsString,
    path::PathBuf,
    sync::{atomic::Ordering, Arc},
    time::Duration,
};
use tokio::sync::{oneshot, watch, Barrier, RwLock};
use tower::ServiceExt;
use weltgewebe_api::{
    auth::{
        accounts::AccountStore, rate_limit::AuthRateLimiter, role::Role, session::SessionBackend,
    },
    config::{
        AppConfig, DomainAccountWriteSource, DomainEdgeWriteSource, DomainNodeWriteSource,
        DomainReadSource,
    },
    domain_db::{
        delete_node_with_edges_in_postgres, delete_node_with_edges_in_postgres_audited,
        domain_projection_version, insert_domain_node, load_nodes_from_postgres,
        lock_node_faden_cache_publication, patch_node_in_postgres,
        patch_node_in_postgres_with_projection_precommit, replace_node_in_postgres_audited,
        NodeConversationDeleteEffect, NodeCreateError, NodePatchInput, NodeWriteError,
    },
    governance::delete_guest_account,
    middleware::{
        auth::auth_middleware, csrf::require_csrf,
        domain_projection::ensure_current_domain_projection,
    },
    node_mutation::{actor_subject_hash, node_hash, NodeMutationAudit, NodeMutationOperation},
    routes::{
        accounts::{AccountInternal, AccountPublic, GarnrolleMapState},
        api_router,
        nodes::{Location, Node},
    },
    state::ApiState,
    telemetry::{BuildInfo, Metrics},
};

mod helpers;
use helpers::set_gewebe_in_dir;

fn account_lifecycle_lock_key(account_id: &str) -> i64 {
    let mut hasher = Sha256::new();
    hasher.update(b"weltgewebe:account-lifecycle:v1");
    let bytes = account_id.as_bytes();
    hasher.update((bytes.len() as u64).to_be_bytes());
    hasher.update(bytes);
    let digest = hasher.finalize();
    i64::from_be_bytes(digest[..8].try_into().expect("SHA-256 prefix"))
}

fn node_mutation_lock_key(node_id: &str) -> i64 {
    let mut hasher = Sha256::new();
    hasher.update(b"weltgewebe:node-mutation:v1");
    let bytes = node_id.as_bytes();
    hasher.update((bytes.len() as u64).to_be_bytes());
    hasher.update(bytes);
    let digest = hasher.finalize();
    i64::from_be_bytes(digest[..8].try_into().expect("SHA-256 prefix"))
}

fn direct_database_url() -> String {
    let url = std::env::var("DATABASE_URL")
        .expect("DATABASE_URL must point at a direct PostgreSQL database (port 5432)");
    assert!(
        !url.contains(":6432"),
        "DATABASE_URL must target direct PostgreSQL, not PgBouncer (port 6432)"
    );
    support::postgres_proof::validated_direct_disposable_url(url)
}

struct EnvVarGuard {
    key: &'static str,
    previous: Option<OsString>,
}

impl EnvVarGuard {
    fn set(key: &'static str, value: &str) -> Self {
        let previous = std::env::var_os(key);
        // SAFETY: every test in this direct-PostgreSQL suite is #[serial], so
        // these process-wide environment mutations cannot overlap each other.
        unsafe { std::env::set_var(key, value) };
        Self { key, previous }
    }
}

impl Drop for EnvVarGuard {
    fn drop(&mut self) {
        match self.previous.take() {
            Some(value) => unsafe { std::env::set_var(self.key, value) },
            None => unsafe { std::env::remove_var(self.key) },
        }
    }
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

const NODE_A: &str = "writepath-node-aaaaaaaaa";
const NODE_B: &str = "writepath-node-bbbbbbbbb";
const NODE_404: &str = "writepath-node-not-found";
const NODE_NULL_LOC: &str = "writepath-node-null-location";
const NODE_BAD_PAYLOAD: &str = "writepath-node-bad-payload";
const SEEDED_NODE_ETAG: &str = "\"2026-01-01T00:00:00+00:00\"";
const JSONL_NODE_ETAG: &str = "\"2024-01-01T00:00:00Z\"";

async fn clean(pool: &PgPool) {
    pool.execute("DELETE FROM domain_edges WHERE id LIKE 'writepath-edge-%'")
        .await
        .expect("clean domain_edges fixtures");
    pool.execute("DELETE FROM domain_node_mutation_audit WHERE node_id LIKE 'writepath-node-%'")
        .await
        .expect("clean node mutation audit fixtures");
    pool.execute("DELETE FROM domain_nodes WHERE id LIKE 'writepath-node-%'")
        .await
        .expect("clean domain_nodes fixtures");
    pool.execute("DELETE FROM domain_accounts WHERE id LIKE 'writepath-node-%'")
        .await
        .expect("clean colliding domain_accounts fixtures");
}

/// Node-create tests generate server-owned UUID ids (no `writepath-node-`
/// prefix), so their cleanup must not rely on the prefix-scoped `clean`.
async fn clean_all_nodes(pool: &PgPool) {
    // Derived Fäden can point at generated node ids. Remove them first so a
    // repeated integration run starts from one coherent graph state.
    pool.execute("DELETE FROM domain_edges")
        .await
        .expect("clean all domain_edges rows");
    pool.execute("DELETE FROM domain_nodes")
        .await
        .expect("clean all domain_nodes rows");
}

async fn seed_node(pool: &PgPool, id: &str, info: Option<&str>, steckbrief: Option<&str>) {
    let payload = match (info, steckbrief) {
        (Some(i), Some(s)) => format!(r#"{{"info": "{i}", "steckbrief": "{s}"}}"#),
        (Some(i), None) => format!(r#"{{"info": "{i}"}}"#),
        (None, Some(s)) => format!(r#"{{"steckbrief": "{s}"}}"#),
        (None, None) => "{}".to_string(),
    };
    sqlx::query(
        "INSERT INTO domain_nodes (id, kind, title, lat, lon, created_at, updated_at, payload) \
         VALUES ($1, 'test', 'Test Node', 53.5, 10.0, \
                 '2026-01-01T00:00:00Z'::timestamptz, \
                 '2026-01-01T00:00:00Z'::timestamptz, $2::jsonb)",
    )
    .bind(id)
    .bind(&payload)
    .execute(pool)
    .await
    .expect("seed domain_nodes row");
}

fn test_metrics() -> Metrics {
    Metrics::try_new(BuildInfo {
        version: "test",
        commit: "test",
        build_timestamp: "test",
    })
    .expect("metrics")
}

fn admin_operator(id: &str) -> AccountInternal {
    AccountInternal {
        public: AccountPublic {
            id: id.to_string(),
            kind: "garnrolle".to_string(),
            title: format!("Operator {id}"),
            summary: None,
            public_pos: None,
            map_state: GarnrolleMapState::NotOnMap,
            radius_m: 0,
            disabled: false,
            tags: vec![],
        },
        role: Role::Admin,
        email: None,
        webauthn_user_id: uuid::Uuid::new_v4(),
    }
}

async fn postgres_write_app(pool: PgPool, operator_id: &str) -> Result<(Router, String, ApiState)> {
    postgres_write_app_with_account_source(
        pool,
        operator_id,
        1_000,
        DomainAccountWriteSource::Postgres,
    )
    .await
}

async fn postgres_write_app_with_guest_limit(
    pool: PgPool,
    operator_id: &str,
    max_guest_owned_nodes: usize,
) -> Result<(Router, String, ApiState)> {
    postgres_write_app_with_account_source(
        pool,
        operator_id,
        max_guest_owned_nodes,
        DomainAccountWriteSource::Postgres,
    )
    .await
}

async fn postgres_write_app_with_account_source(
    pool: PgPool,
    operator_id: &str,
    max_guest_owned_nodes: usize,
    domain_account_write_source: DomainAccountWriteSource,
) -> Result<(Router, String, ApiState)> {
    let operator = admin_operator(operator_id);
    sqlx::query(
        "INSERT INTO domain_accounts (id, kind, title, map_state, role, disabled, webauthn_user_id) \
         VALUES ($1, 'garnrolle', $2, 'not_on_map', 'admin', FALSE, $3::uuid) \
         ON CONFLICT (id) DO UPDATE SET \
             kind = EXCLUDED.kind, title = EXCLUDED.title, map_state = EXCLUDED.map_state, \
             role = EXCLUDED.role, disabled = FALSE, webauthn_user_id = EXCLUDED.webauthn_user_id",
    )
    .bind(operator_id)
    .bind(&operator.public.title)
    .bind(operator.webauthn_user_id.to_string())
    .execute(&pool)
    .await
    .context("seed canonical PostgreSQL operator account")?;

    let mut accounts = AccountStore::new();
    accounts.insert(operator);

    let nodes = load_nodes_from_postgres(&pool)
        .await
        .context("load nodes for test")?;

    let config = AppConfig {
        max_guest_owned_nodes,
        domain_read_source: DomainReadSource::Postgres,
        domain_account_write_source,
        domain_node_write_source: DomainNodeWriteSource::Postgres,
        domain_edge_write_source: DomainEdgeWriteSource::Postgres,
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
    };

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
        domain_projection_local_node_patch_handoff: std::sync::Arc::new(
            std::sync::atomic::AtomicI64::new(-1),
        ),
        domain_projection_version: std::sync::Arc::new(std::sync::atomic::AtomicI64::new(0)),
        edges: Arc::new(RwLock::new(weltgewebe_api::state::OrderedCache::new())),
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

    // Handler-focused tests can directly exercise the write path without the
    // projection middleware. The explicit projected fixture below covers the
    // full production request composition.
    let app = Router::new()
        .merge(api_router())
        .layer(from_fn_with_state(state.clone(), auth_middleware))
        .layer(axum::middleware::from_fn(require_csrf))
        .with_state(state.clone());

    Ok((app, cookie, state))
}

// Production-equivalent ordering for HTTP tests of the privacy projection
// guard: authentication and session I/O must finish before the DB row fence.
fn postgres_projected_router(state: &ApiState) -> Router {
    Router::new()
        .merge(api_router())
        .layer(from_fn_with_state(
            state.clone(),
            ensure_current_domain_projection,
        ))
        .route_layer(from_fn_with_state(state.clone(), auth_middleware))
        .layer(axum::middleware::from_fn(require_csrf))
        .with_state(state.clone())
}

fn patch_node_req(cookie: &str, id: &str, json_body: &str, etag: &str) -> Request<body::Body> {
    Request::builder()
        .method("PATCH")
        .uri(format!("/nodes/{id}"))
        .header("Content-Type", "application/json")
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .header("Cookie", cookie)
        .header("If-Match", etag)
        .body(body::Body::from(json_body.to_string()))
        .unwrap()
}

fn replace_node_req(cookie: &str, id: &str, title: &str, etag: &str) -> Request<body::Body> {
    Request::builder()
        .method("PUT")
        .uri(format!("/nodes/{id}"))
        .header("Content-Type", "application/json")
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .header("Cookie", cookie)
        .header("If-Match", etag)
        .body(body::Body::from(
            serde_json::json!({
                "title": title,
                "kind": "Werkstatt",
                "address": "Neue Straße 1",
                "location": {"lat": 53.55, "lon": 10.05},
                "tags": ["parallel"]
            })
            .to_string(),
        ))
        .unwrap()
}

/// A. PostgreSQL patch persists and reload sees the change.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn postgres_node_patch_persists_and_reload_sees_change() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;
    seed_node(&pool, NODE_A, None, None).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, state) =
        postgres_write_app(pool.clone(), "10000000-0000-0000-0000-000000000001").await?;

    // Production mutations enter through the projection middleware, so the
    // handler starts from one complete current cache generation. Establish the
    // same invariant explicitly in this handler-focused test.
    state.refresh_domain_projection_if_stale().await?;

    let res = app
        .clone()
        .oneshot(patch_node_req(
            &cookie,
            NODE_A,
            r#"{"info": "new info"}"#,
            SEEDED_NODE_ETAG,
        ))
        .await?;
    assert_eq!(res.status(), StatusCode::OK);

    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let patched: serde_json::Value = serde_json::from_slice(&bytes)?;
    assert_eq!(patched["id"], NODE_A);
    assert_eq!(patched["info"], "new info");

    // DB row reflects the patch.
    let (payload_text,): (String,) =
        sqlx::query_as("SELECT payload::text FROM domain_nodes WHERE id = $1")
            .bind(NODE_A)
            .fetch_one(&pool)
            .await
            .expect("node row must exist");
    let payload: serde_json::Value = serde_json::from_str(&payload_text)?;
    assert_eq!(
        payload.get("info").and_then(|v| v.as_str()),
        Some("new info")
    );

    // In-memory cache sees the change immediately (read-your-writes).
    {
        let nodes = state.nodes.read().await;
        let node = nodes.get(NODE_A).expect("patched node in cache");
        assert_eq!(node.info.as_deref(), Some("new info"));
    }

    // load_nodes_from_postgres reconstructs the same projection.
    let reloaded = load_nodes_from_postgres(&pool)
        .await
        .context("reload nodes")?;
    let node = reloaded.get(NODE_A).expect("node reloaded from postgres");
    assert_eq!(node.info.as_deref(), Some("new info"));

    clean(&pool).await;
    Ok(())
}

/// A2. When the PostgreSQL trigger advances the generation by exactly one for
/// our own PATCH, the locally updated cache can safely fast-forward the process
/// marker instead of forcing the next write through a full projection reload.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn postgres_node_patch_fast_forwards_exact_local_projection_generation() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;
    seed_node(&pool, NODE_A, None, None).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (_app, cookie, state) =
        postgres_write_app(pool.clone(), "10000000-0000-0000-0000-000000000091").await?;
    let app = postgres_projected_router(&state);

    let preload = app
        .clone()
        .oneshot(
            Request::get("/nodes")
                .header("Host", "localhost")
                .body(body::Body::empty())?,
        )
        .await?;
    assert_eq!(preload.status(), StatusCode::OK);

    let local_before = state.domain_projection_version.load(Ordering::Acquire);
    let db_before = domain_projection_version(&pool).await?;
    assert_eq!(
        local_before, db_before,
        "preload must synchronize projection generation"
    );

    let response = app
        .clone()
        .oneshot(patch_node_req(
            &cookie,
            NODE_A,
            r#"{"info": "fast-forwarded"}"#,
            SEEDED_NODE_ETAG,
        ))
        .await?;
    assert_eq!(response.status(), StatusCode::OK);

    let db_after = domain_projection_version(&pool).await?;
    let local_after = state.domain_projection_version.load(Ordering::Acquire);
    assert_eq!(
        db_after,
        db_before + 1,
        "one node PATCH must advance the trigger generation once"
    );
    assert_eq!(
        local_after, db_after,
        "isolated local PATCH must fast-forward the process projection marker"
    );
    let metrics = String::from_utf8(state.metrics.render()?)?;
    assert!(metrics.contains(&format!(
        "domain_projection_snapshot{{kind=\"version\"}} {db_after}"
    )));

    clean(&pool).await;
    Ok(())
}

/// A2b. A semantic no-op performs no UPDATE trigger and therefore must not
/// manufacture a transaction-proven local projection generation.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn postgres_node_noop_patch_has_no_local_projection_handoff() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;
    seed_node(&pool, NODE_A, Some("unchanged"), None).await;
    let db_before = domain_projection_version(&pool).await?;

    let (_node, precommit_version) = patch_node_in_postgres_with_projection_precommit(
        &pool,
        NODE_A,
        NodePatchInput {
            info: Some(Some("unchanged".to_string())),
            search_visibility: None,
        },
        |version| version,
    )
    .await?;

    assert_eq!(
        precommit_version, None,
        "a no-op patch must not claim a local projection generation"
    );
    assert_eq!(
        domain_projection_version(&pool).await?,
        db_before,
        "a no-op patch must not advance the projection generation"
    );

    clean(&pool).await;
    Ok(())
}

/// A3. A concurrent external domain update makes the observed generation jump
/// by more than one. The PATCH must then leave the process marker stale so the
/// ordinary refresh path reconciles the missing external change.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn postgres_node_patch_does_not_fast_forward_over_external_generation() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;
    seed_node(&pool, NODE_A, None, None).await;
    seed_node(&pool, NODE_B, Some("before external"), None).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (_app, cookie, state) =
        postgres_write_app(pool.clone(), "10000000-0000-0000-0000-000000000092").await?;
    let app = postgres_projected_router(&state);

    let preload = app
        .clone()
        .oneshot(
            Request::get("/nodes")
                .header("Host", "localhost")
                .body(body::Body::empty())?,
        )
        .await?;
    assert_eq!(preload.status(), StatusCode::OK);
    let local_before = state.domain_projection_version.load(Ordering::Acquire);
    assert_eq!(local_before, domain_projection_version(&pool).await?);

    // Hold NODE_A so the API PATCH passes middleware freshness checks and then
    // blocks inside PostgreSQL. While it is blocked, mutate NODE_B externally.
    let mut blocker = pool.begin().await?;
    sqlx::query("SELECT id FROM domain_nodes WHERE id = $1 FOR UPDATE")
        .bind(NODE_A)
        .fetch_one(&mut *blocker)
        .await?;

    let request = patch_node_req(
        &cookie,
        NODE_A,
        r#"{"info": "local after external"}"#,
        SEEDED_NODE_ETAG,
    );
    let patch_app = app.clone();
    let patch_task = tokio::spawn(async move { patch_app.oneshot(request).await });
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert!(
        !patch_task.is_finished(),
        "row lock must hold the API PATCH before the external generation bump"
    );

    sqlx::query(
        "UPDATE domain_nodes SET payload = jsonb_set(payload, '{info}', to_jsonb($2::text), TRUE), updated_at = NOW() WHERE id = $1",
    )
    .bind(NODE_B)
    .bind("external change")
    .execute(&pool)
    .await?;
    assert_eq!(domain_projection_version(&pool).await?, local_before + 1);

    // This is the bug-shaped window: the API PATCH owns `nodes_persist` but is
    // still blocked before its own commit, while an unrelated external writer
    // already produced V+1. An anonymous read must NOT call this a local handoff
    // and return the old snapshot. It must wait for coherent reconciliation.
    let read_app = app.clone();
    let concurrent_read = tokio::spawn(async move {
        read_app
            .oneshot(
                Request::get("/nodes")
                    .header("Host", "localhost")
                    .body(body::Body::empty())
                    .expect("anonymous read request"),
            )
            .await
    });
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert!(
        !concurrent_read.is_finished(),
        "pre-commit nodes_persist ownership must not misclassify external V+1 as a local handoff"
    );

    blocker.commit().await?;
    let response = patch_task.await??;
    assert_eq!(response.status(), StatusCode::OK);

    let db_after = domain_projection_version(&pool).await?;
    let local_after_patch = state.domain_projection_version.load(Ordering::Acquire);
    assert_eq!(db_after, local_before + 2);
    assert_eq!(
        local_after_patch, local_before,
        "PATCH must not fast-forward across an unseen external generation"
    );

    let refresh = concurrent_read.await??;
    assert_eq!(refresh.status(), StatusCode::OK);
    assert_eq!(
        state.domain_projection_version.load(Ordering::Acquire),
        db_after,
        "next safe request must reconcile the externally changed generation"
    );
    assert_eq!(
        state
            .nodes
            .read()
            .await
            .get(NODE_B)
            .and_then(|node| node.info.as_deref()),
        Some("external change")
    );

    clean(&pool).await;
    Ok(())
}

/// A3b. If an unseen external V+1 lands after the process has a current cache but before
/// our PATCH commits V+2, the PATCH must not publish only its own row into the
/// old V cache. The previous cache must remain a complete generation until the
/// ordinary projection reload atomically replaces it.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn postgres_node_patch_preserves_complete_cache_across_external_generation() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;
    seed_node(&pool, NODE_A, None, None).await;
    seed_node(&pool, NODE_B, Some("before external"), None).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, state) =
        postgres_write_app(pool.clone(), "10000000-0000-0000-0000-00000000009a").await?;

    // Establish one complete current cache generation without wrapping the PATCH
    // in projection middleware. This test targets handler cache publication only;
    // the separate A3/A4 tests cover middleware classification and reconciliation.
    state.refresh_domain_projection_if_stale().await?;
    let local_before = state.domain_projection_version.load(Ordering::Acquire);
    assert_eq!(local_before, domain_projection_version(&pool).await?);

    let mut blocker = pool.begin().await?;
    sqlx::query("SELECT id FROM domain_nodes WHERE id = $1 FOR UPDATE")
        .bind(NODE_A)
        .fetch_one(&mut *blocker)
        .await?;

    let request = patch_node_req(
        &cookie,
        NODE_A,
        r#"{"info": "local after external"}"#,
        SEEDED_NODE_ETAG,
    );
    let patch_app = app.clone();
    let patch_task = tokio::spawn(async move { patch_app.oneshot(request).await });
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert!(
        !patch_task.is_finished(),
        "row lock must hold the API PATCH before its PostgreSQL write can complete"
    );

    sqlx::query(
        "UPDATE domain_nodes SET payload = jsonb_set(payload, '{info}', to_jsonb($2::text), TRUE), updated_at = NOW() WHERE id = $1",
    )
    .bind(NODE_B)
    .bind("external change")
    .execute(&pool)
    .await?;
    assert_eq!(domain_projection_version(&pool).await?, local_before + 1);

    blocker.commit().await?;
    let response = tokio::time::timeout(Duration::from_secs(3), patch_task)
        .await
        .context("PATCH did not finish after releasing its row lock")???;
    assert_eq!(response.status(), StatusCode::OK);
    let db_after = domain_projection_version(&pool).await?;
    assert_eq!(db_after, local_before + 2);
    assert_eq!(
        state.domain_projection_version.load(Ordering::Acquire),
        local_before,
        "process projection marker must remain on the previous complete generation"
    );

    {
        let nodes = state.nodes.read().await;
        assert_eq!(
            nodes.get(NODE_A).and_then(|node| node.info.as_deref()),
            None,
            "PATCH must not publish its V+2 row into the previous V cache"
        );
        assert_eq!(
            nodes.get(NODE_B).and_then(|node| node.info.as_deref()),
            Some("before external"),
            "previous cache must remain internally complete until reconciliation"
        );
    }

    state.refresh_domain_projection_if_stale().await?;
    assert_eq!(
        state.domain_projection_version.load(Ordering::Acquire),
        db_after
    );
    let nodes = state.nodes.read().await;
    assert_eq!(
        nodes.get(NODE_A).and_then(|node| node.info.as_deref()),
        Some("local after external")
    );
    assert_eq!(
        nodes.get(NODE_B).and_then(|node| node.info.as_deref()),
        Some("external change")
    );
    drop(nodes);

    clean(&pool).await;
    Ok(())
}

/// A4. An exact transaction-proven +1 commit/cache handoff of a serialized local
/// PostgreSQL node PATCH must let a safe read keep the existing snapshot rather
/// than starting a redundant full reload. Authenticated/strict classification is
/// proven separately by the projection middleware policy test.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn safe_projection_read_defers_exact_local_node_generation_handoff() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;
    seed_node(&pool, NODE_B, Some("before handoff"), None).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (_app, _cookie, state) =
        postgres_write_app(pool.clone(), "10000000-0000-0000-0000-000000000093").await?;

    // The fixture builder has already loaded NODE_B into the process cache. Bind
    // its process marker to the current database generation so the next +1 is
    // exactly the handoff window we want to exercise, without paying for an
    // unrelated full projection reload in test setup.
    let local_before = domain_projection_version(&pool).await?;
    state
        .domain_projection_version
        .store(local_before, Ordering::Release);
    assert_eq!(
        state
            .nodes
            .read()
            .await
            .get(NODE_B)
            .and_then(|node| node.info.as_deref()),
        Some("before handoff")
    );

    // Model the observable commit/cache-publication handoff deterministically.
    // The real PATCH installs the same marker before COMMIT only after its trigger
    // has updated and locked the projection-state row in that transaction.
    let handoff_guard = state.nodes_persist.lock().await;
    sqlx::query(
        "UPDATE domain_nodes SET payload = jsonb_set(payload, '{info}', to_jsonb($2::text), TRUE), updated_at = NOW() WHERE id = $1",
    )
    .bind(NODE_B)
    .bind("committed during handoff")
    .execute(&pool)
    .await?;
    let db_after = domain_projection_version(&pool).await?;
    assert_eq!(db_after, local_before + 1);
    let projection_handoff = state
        .begin_local_node_patch_projection_handoff(db_after)
        .expect("install exact local projection handoff");
    assert!(
        state
            .begin_local_node_patch_projection_handoff(db_after + 1)
            .is_none(),
        "a second handoff must not overwrite the active exact generation"
    );
    assert_eq!(
        state
            .domain_projection_local_node_patch_handoff
            .load(Ordering::Acquire),
        db_after,
        "failed overlapping handoff installation must leave the active marker intact"
    );

    tokio::time::timeout(
        Duration::from_secs(2),
        state.refresh_domain_projection_for_read(),
    )
    .await
    .context("safe projection refresh must not block during exact local +1 handoff")??;

    assert_eq!(
        state.domain_projection_version.load(Ordering::Acquire),
        local_before,
        "safe read must defer rather than claim or full-reload the in-flight +1 generation"
    );
    assert_eq!(
        state
            .nodes
            .read()
            .await
            .get(NODE_B)
            .and_then(|node| node.info.as_deref()),
        Some("before handoff"),
        "deferral must leave the previous snapshot untouched"
    );
    let metrics = String::from_utf8(state.metrics.render()?)?;
    assert!(metrics.contains(r#"domain_projection_events_total{event="refresh_deferred"} 1"#));

    drop(projection_handoff);
    drop(handoff_guard);
    clean(&pool).await;
    Ok(())
}

/// A5. A strict request that observes the exact committed PATCH handoff waits
/// for that writer to publish its cache/version instead of starting an O(N)
/// projection reload for work already completed locally.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn strict_projection_refresh_waits_for_exact_local_patch_handoff() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;
    seed_node(&pool, NODE_B, Some("before strict handoff"), None).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (_app, _cookie, state) =
        postgres_write_app(pool.clone(), "10000000-0000-0000-0000-000000000094").await?;
    let local_before = domain_projection_version(&pool).await?;
    state
        .domain_projection_version
        .store(local_before, Ordering::Release);

    let persist_guard = state.nodes_persist.lock().await;
    sqlx::query(
        "UPDATE domain_nodes SET payload = jsonb_set(payload, '{info}', to_jsonb($2::text), TRUE), updated_at = NOW() WHERE id = $1",
    )
    .bind(NODE_B)
    .bind("strict handoff committed")
    .execute(&pool)
    .await?;
    let db_after = domain_projection_version(&pool).await?;
    assert_eq!(db_after, local_before + 1);
    let projection_handoff = state
        .begin_local_node_patch_projection_handoff(db_after)
        .expect("install exact local projection handoff");

    // Queue a second would-be writer *before* the strict refresh. The strict
    // request must wait only for the explicit handoff above, not enter the fair
    // `nodes_persist` queue behind this later writer.
    let queued_writer_state = state.clone();
    let (queued_writer_acquired_tx, queued_writer_acquired_rx) = oneshot::channel();
    let (queued_writer_release_tx, queued_writer_release_rx) = oneshot::channel();
    let queued_writer = tokio::spawn(async move {
        let _guard = queued_writer_state.nodes_persist.lock().await;
        let _ = queued_writer_acquired_tx.send(());
        let _ = queued_writer_release_rx.await;
    });
    tokio::task::yield_now().await;

    let strict_state = state.clone();
    let strict_refresh =
        tokio::spawn(async move { strict_state.refresh_domain_projection_if_stale().await });
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert!(
        !strict_refresh.is_finished(),
        "strict refresh must wait for the committed local handoff"
    );

    let canonical = load_nodes_from_postgres(&pool).await?;
    let canonical_node = canonical
        .get(NODE_B)
        .cloned()
        .context("canonical node after strict handoff")?;
    state
        .nodes
        .write()
        .await
        .insert(NODE_B.to_string(), canonical_node);
    state
        .domain_projection_version
        .store(db_after, Ordering::Release);
    state.metrics.set_domain_projection_version(db_after);
    drop(projection_handoff);
    drop(persist_guard);

    tokio::time::timeout(Duration::from_secs(2), queued_writer_acquired_rx)
        .await
        .context("queued writer did not acquire persist mutex after handoff")?
        .context("queued writer acquisition signal dropped")?;
    tokio::time::timeout(Duration::from_secs(2), strict_refresh)
        .await
        .context("strict refresh followed the next writer instead of the observed handoff")?
        .context("strict refresh task failed")??;
    let _ = queued_writer_release_tx.send(());
    queued_writer.await.context("queued writer task failed")?;
    let metrics = String::from_utf8(state.metrics.render()?)?;
    assert!(
        !metrics.contains(r#"domain_projection_events_total{event="reload_success"}"#),
        "strict exact +1 handoff must not perform a full projection reload"
    );
    assert_eq!(
        state
            .nodes
            .read()
            .await
            .get(NODE_B)
            .and_then(|node| node.info.as_deref()),
        Some("strict handoff committed")
    );

    clean(&pool).await;
    Ok(())
}

/// A5b-privacy. An anonymous GET started AFTER the PostgreSQL commit must not
/// read stale public data on either API replica. Overlapping pre-commit GETs
/// are a different linearization case and are not claimed by this proof.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn anonymous_reads_after_visibility_commit_hide_nodes_across_api_instances() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;
    seed_node(&pool, NODE_B, Some("PRIVATE-AFTER-COMMIT-MARKER"), None).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (_first_router, _, first_state) =
        postgres_write_app(pool.clone(), "10000000-0000-0000-0000-0000000000a1").await?;
    let (_second_router, _, second_state) =
        postgres_write_app(pool.clone(), "10000000-0000-0000-0000-0000000000a2").await?;
    let first = postgres_projected_router(&first_state);
    let second = postgres_projected_router(&second_state);

    for (instance, app) in [("first", first.clone()), ("second", second.clone())] {
        let response = app
            .oneshot(
                Request::get(format!("/nodes/{NODE_B}"))
                    .header("Host", "localhost")
                    .body(body::Body::empty())?,
            )
            .await?;
        assert_eq!(
            response.status(),
            StatusCode::OK,
            "{instance} must preload public node"
        );
    }

    let mut committed_version = domain_projection_version(&pool).await?;
    for visibility in ["private", "hidden", "revoked"] {
        sqlx::query(
            "UPDATE domain_nodes SET search_visibility = $2, updated_at = NOW() WHERE id = $1",
        )
        .bind(NODE_B)
        .bind(visibility)
        .execute(&pool)
        .await?;
        let new_version = domain_projection_version(&pool).await?;
        assert!(
            new_version > committed_version,
            "visibility commit must advance projection"
        );

        for (instance, app) in [("first", first.clone()), ("second", second.clone())] {
            let detail = app
                .clone()
                .oneshot(
                    Request::get(format!("/nodes/{NODE_B}"))
                        .header("Host", "localhost")
                        .body(body::Body::empty())?,
                )
                .await?;
            assert_eq!(
                detail.status(),
                StatusCode::NOT_FOUND,
                "{instance} leaked {visibility} detail after commit"
            );
            let listing = app
                .oneshot(
                    Request::get("/nodes")
                        .header("Host", "localhost")
                        .body(body::Body::empty())?,
                )
                .await?;
            assert_eq!(listing.status(), StatusCode::OK);
            let bytes = body::to_bytes(listing.into_body(), usize::MAX).await?;
            let rendered = String::from_utf8_lossy(&bytes);
            assert!(
                !rendered.contains(NODE_B) && !rendered.contains("PRIVATE-AFTER-COMMIT-MARKER"),
                "{instance} leaked {visibility} in public list"
            );
        }

        sqlx::query(
            "UPDATE domain_nodes SET search_visibility = 'public', updated_at = NOW() WHERE id = $1",
        )
        .bind(NODE_B)
        .execute(&pool)
        .await?;
        committed_version = domain_projection_version(&pool).await?;
        for app in [first.clone(), second.clone()] {
            let response = app
                .oneshot(
                    Request::get(format!("/nodes/{NODE_B}"))
                        .header("Host", "localhost")
                        .body(body::Body::empty())?,
                )
                .await?;
            assert_eq!(
                response.status(),
                StatusCode::OK,
                "public restoration must be readable"
            );
        }
    }
    clean(&pool).await;
    Ok(())
}

/// A5c. A strict request must not wait forever when a transaction-proven local
/// handoff remains visible but its writer never publishes the local cache/version.
/// The refresh fails closed within the bounded handoff budget and does not start
/// a speculative O(N) reload while ownership is still explicitly marked.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn strict_projection_refresh_times_out_stuck_local_patch_handoff() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;
    seed_node(&pool, NODE_B, Some("before stuck handoff"), None).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (_app, _cookie, state) =
        postgres_write_app(pool.clone(), "10000000-0000-0000-0000-00000000009b").await?;
    let local_before = domain_projection_version(&pool).await?;
    state
        .domain_projection_version
        .store(local_before, Ordering::Release);

    let persist_guard = state.nodes_persist.lock().await;
    sqlx::query(
        "UPDATE domain_nodes SET payload = jsonb_set(payload, '{info}', to_jsonb($2::text), TRUE), updated_at = NOW() WHERE id = $1",
    )
    .bind(NODE_B)
    .bind("committed but publication stuck")
    .execute(&pool)
    .await?;
    let db_after = domain_projection_version(&pool).await?;
    assert_eq!(db_after, local_before + 1);
    let projection_handoff = state
        .begin_local_node_patch_projection_handoff(db_after)
        .expect("install exact stuck projection handoff");

    let error = tokio::time::timeout(
        Duration::from_secs(2),
        state.refresh_domain_projection_if_stale(),
    )
    .await
    .context("strict refresh exceeded the outer stuck-handoff safety budget")?
    .expect_err("stuck exact local handoff must fail closed");
    assert!(
        error
            .to_string()
            .contains("timed out waiting for local node projection handoff generation"),
        "unexpected stuck-handoff error: {error:#}"
    );
    assert_eq!(
        state.domain_projection_version.load(Ordering::Acquire),
        local_before,
        "timed-out strict refresh must not claim the stuck generation"
    );
    let metrics = String::from_utf8(state.metrics.render()?)?;
    assert!(
        !metrics.contains(r#"domain_projection_events_total{event="reload_success"}"#),
        "stuck exact local handoff must fail closed without a speculative full reload"
    );

    drop(projection_handoff);
    drop(persist_guard);
    clean(&pool).await;
    Ok(())
}

/// A5d. A stable lower database generation must still reconcile rather than
/// being accepted merely because the process-local generation is numerically
/// ahead. This protects restore/PITR semantics while the local-ahead fast check
/// only suppresses reloads when a second DB read has actually caught up.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn strict_projection_refresh_reconciles_stable_lower_database_generation() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;
    seed_node(&pool, NODE_A, Some("restore baseline"), None).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (_app, _cookie, state) =
        postgres_write_app(pool.clone(), "10000000-0000-0000-0000-00000000009c").await?;
    state.refresh_domain_projection_if_stale().await?;
    let db_version = domain_projection_version(&pool).await?;
    assert_eq!(
        state.domain_projection_version.load(Ordering::Acquire),
        db_version
    );

    let reload_successes = |rendered: &str| -> u64 {
        rendered
            .lines()
            .find(|line| {
                line.starts_with(r#"domain_projection_events_total{event="reload_success"}"#)
            })
            .and_then(|line| line.split_whitespace().last())
            .and_then(|value| value.parse::<u64>().ok())
            .unwrap_or(0)
    };
    let reloads_before = reload_successes(&String::from_utf8(state.metrics.render()?)?);

    state
        .domain_projection_version
        .store(db_version + 1, Ordering::Release);
    state.refresh_domain_projection_if_stale().await?;

    assert_eq!(
        state.domain_projection_version.load(Ordering::Acquire),
        db_version,
        "stable lower database generation must win over a newer local marker"
    );
    let reloads_after = reload_successes(&String::from_utf8(state.metrics.render()?)?);
    assert_eq!(
        reloads_after,
        reloads_before + 1,
        "stable lower generation must use exactly one normal reconciliation reload"
    );

    clean(&pool).await;
    Ok(())
}

#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn postgres_node_patch_rejects_oversized_info_without_side_effects() -> Result<()> {
    const INFO_MAX_LEN: usize = 20_000;
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;
    seed_node(&pool, NODE_A, Some("initial"), None).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, state) =
        postgres_write_app(pool.clone(), "10000000-0000-0000-0000-000000000099").await?;
    let request_body = serde_json::json!({ "info": "a".repeat(INFO_MAX_LEN + 1) }).to_string();
    let response = app
        .oneshot(patch_node_req(
            &cookie,
            NODE_A,
            &request_body,
            SEEDED_NODE_ETAG,
        ))
        .await?;
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    let response_body = body::to_bytes(response.into_body(), usize::MAX).await?;
    assert_eq!(
        String::from_utf8(response_body.to_vec())?,
        "invalid node patch request: info exceeds the maximum length of 20000"
    );

    let persisted_info: Option<String> =
        sqlx::query_scalar("SELECT payload->>'info' FROM domain_nodes WHERE id = $1")
            .bind(NODE_A)
            .fetch_one(&pool)
            .await?;
    assert_eq!(persisted_info.as_deref(), Some("initial"));
    let cached_info = state
        .nodes
        .read()
        .await
        .get(NODE_A)
        .and_then(|node| node.info.clone());
    assert_eq!(cached_info.as_deref(), Some("initial"));

    clean(&pool).await;
    Ok(())
}

/// B. No JSONL side-effect in PostgreSQL mode.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn postgres_node_patch_has_no_jsonl_side_effect() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;
    seed_node(&pool, NODE_B, Some("original"), None).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, _state) =
        postgres_write_app(pool.clone(), "10000000-0000-0000-0000-000000000002").await?;

    let res = app
        .oneshot(patch_node_req(
            &cookie,
            NODE_B,
            r#"{"info": "updated"}"#,
            SEEDED_NODE_ETAG,
        ))
        .await?;
    assert_eq!(res.status(), StatusCode::OK);

    // JSONL nodes file must NOT have been written.
    let nodes_file = in_dir.join("demo.nodes.jsonl");
    assert!(
        !nodes_file.exists(),
        "PostgreSQL write mode must not create or write the JSONL nodes file"
    );

    clean(&pool).await;
    Ok(())
}

/// C. Not-found returns 404.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn postgres_node_patch_not_found_returns_404() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, state) =
        postgres_write_app(pool.clone(), "10000000-0000-0000-0000-000000000003").await?;

    let res = app
        .oneshot(patch_node_req(
            &cookie,
            NODE_404,
            r#"{"info": "ghost"}"#,
            SEEDED_NODE_ETAG,
        ))
        .await?;
    assert_eq!(res.status(), StatusCode::NOT_FOUND);

    // Cache not touched.
    assert!(
        state.nodes.read().await.get(NODE_404).is_none(),
        "not-found patch must not insert into cache"
    );

    clean(&pool).await;
    Ok(())
}

/// D. Postgres read + JSONL node write is blocked with 409.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn postgres_read_jsonl_node_write_is_blocked() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;
    seed_node(&pool, NODE_A, Some("initial"), None).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let nodes = load_nodes_from_postgres(&pool).await?;

    let config = AppConfig {
        max_guest_owned_nodes: 1_000,
        domain_read_source: DomainReadSource::Postgres,
        domain_account_write_source: DomainAccountWriteSource::Postgres,
        domain_node_write_source: DomainNodeWriteSource::Jsonl,
        domain_edge_write_source: DomainEdgeWriteSource::Jsonl,
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
    };

    // Authentication now validates canonical PostgreSQL identity even when
    // the mutation source is deliberately JSONL/read-only. Seed both stores
    // so this test exercises the expected 409 write-source guard, not a 401.
    const OPERATOR_ID: &str = "10000000-0000-0000-0000-000000000004";
    sqlx::query(
        "INSERT INTO domain_accounts (id,kind,title,map_state,role,disabled) \
         VALUES ($1,'garnrolle','Mixed source operator','not_on_map','admin',FALSE) \
         ON CONFLICT (id) DO UPDATE SET role = 'admin', disabled = FALSE",
    )
    .bind(OPERATOR_ID)
    .execute(&pool)
    .await?;
    let mut accounts = AccountStore::new();
    accounts.insert(admin_operator(OPERATOR_ID));
    let rate_limiter = Arc::new(AuthRateLimiter::new(&config));

    let state = ApiState {
        db_pool: Some(pool.clone()),
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
        domain_projection_local_node_patch_handoff: std::sync::Arc::new(
            std::sync::atomic::AtomicI64::new(-1),
        ),
        domain_projection_version: std::sync::Arc::new(std::sync::atomic::AtomicI64::new(0)),
        edges: Arc::new(RwLock::new(weltgewebe_api::state::OrderedCache::new())),
        rate_limiter,
        mailer: None,
        webauthn: None,
        passkey_registrations: Default::default(),
        passkey_registration_grants: Default::default(),
        passkey_authentications: Default::default(),
        passkeys: Default::default(),
        web_push: None,
    };

    let session = state.sessions.create(OPERATOR_ID.to_string(), None).await?;
    let cookie = format!("gewebe_session={}", session.id);

    let app = Router::new()
        .merge(api_router())
        .layer(from_fn_with_state(state.clone(), auth_middleware))
        .layer(axum::middleware::from_fn(require_csrf))
        .with_state(state.clone());

    let res = app
        .oneshot(patch_node_req(
            &cookie,
            NODE_A,
            r#"{"info": "blocked"}"#,
            SEEDED_NODE_ETAG,
        ))
        .await?;
    assert_eq!(res.status(), StatusCode::CONFLICT);

    let body_bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let body_str = String::from_utf8_lossy(&body_bytes);
    assert!(
        body_str.contains("DOMAIN_READ_SOURCE_READ_ONLY"),
        "body must contain DOMAIN_READ_SOURCE_READ_ONLY, got: {body_str}"
    );

    // DB row must be untouched.
    let (payload_text,): (String,) =
        sqlx::query_as("SELECT payload::text FROM domain_nodes WHERE id = $1")
            .bind(NODE_A)
            .fetch_one(&pool)
            .await
            .expect("row still present");
    let payload: serde_json::Value = serde_json::from_str(&payload_text)?;
    assert_eq!(
        payload.get("info").and_then(|v| v.as_str()),
        Some("initial"),
        "blocked patch must not mutate domain_nodes"
    );

    clean(&pool).await;
    sqlx::query("DELETE FROM domain_accounts WHERE id = $1")
        .bind(OPERATOR_ID)
        .execute(&pool)
        .await?;
    Ok(())
}

/// E. steckbrief cleanup in PostgreSQL mode.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn postgres_node_patch_removes_steckbrief() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;
    seed_node(&pool, NODE_A, Some("kept"), Some("legacy")).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, _state) =
        postgres_write_app(pool.clone(), "10000000-0000-0000-0000-000000000005").await?;

    // Patch with no info change (no-op for info) — only steckbrief cleanup.
    let res = app
        .oneshot(patch_node_req(&cookie, NODE_A, r#"{}"#, SEEDED_NODE_ETAG))
        .await?;
    assert_eq!(res.status(), StatusCode::OK);

    let (payload_text,): (String,) =
        sqlx::query_as("SELECT payload::text FROM domain_nodes WHERE id = $1")
            .bind(NODE_A)
            .fetch_one(&pool)
            .await
            .expect("node row present");
    let payload: serde_json::Value = serde_json::from_str(&payload_text)?;
    assert!(
        payload.get("steckbrief").is_none(),
        "steckbrief must be removed by patch"
    );
    assert_eq!(
        payload.get("info").and_then(|v| v.as_str()),
        Some("kept"),
        "info must be preserved when not in patch"
    );

    clean(&pool).await;
    Ok(())
}

/// F. JSONL default mode continues to work (compile-only guard).
/// The full JSONL behaviour is covered by existing api_nodes tests.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn jsonl_default_node_patch_compiles_and_routes_correctly() -> Result<()> {
    // This test only verifies that the compile path for JSONL mode is reachable.
    // Actual JSONL semantics are covered by the offline api_nodes test suite.
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    // Write a minimal nodes JSONL so the JSONL handler can open it.
    let nodes_file = in_dir.join("demo.nodes.jsonl");
    std::fs::write(
        &nodes_file,
        r#"{"id":"writepath-node-jsonl-1","kind":"test","title":"T","location":{"lat":53.5,"lon":10.0},"created_at":"2024-01-01T00:00:00Z","updated_at":"2024-01-01T00:00:00Z"}"#,
    )?;

    let config = AppConfig {
        max_guest_owned_nodes: 1_000,
        domain_read_source: DomainReadSource::Jsonl,
        domain_account_write_source: DomainAccountWriteSource::Jsonl,
        domain_node_write_source: DomainNodeWriteSource::Jsonl,
        domain_edge_write_source: DomainEdgeWriteSource::Jsonl,
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
    };

    let mut accounts = AccountStore::new();
    accounts.insert(admin_operator("writepath-node-admin-6"));
    let rate_limiter = Arc::new(AuthRateLimiter::new(&config));

    let nodes = weltgewebe_api::routes::nodes::load_nodes().await?;

    let state = ApiState {
        db_pool: None,
        db_pool_configured: false,
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
        domain_projection_local_node_patch_handoff: std::sync::Arc::new(
            std::sync::atomic::AtomicI64::new(-1),
        ),
        domain_projection_version: std::sync::Arc::new(std::sync::atomic::AtomicI64::new(0)),
        edges: Arc::new(RwLock::new(weltgewebe_api::state::OrderedCache::new())),
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
        .create("writepath-node-admin-6".to_string(), None)
        .await?;
    let cookie = format!("gewebe_session={}", session.id);

    let app = Router::new()
        .merge(api_router())
        .layer(from_fn_with_state(state.clone(), auth_middleware))
        .layer(axum::middleware::from_fn(require_csrf))
        .with_state(state.clone());

    let res = app
        .oneshot(patch_node_req(
            &cookie,
            "writepath-node-jsonl-1",
            r#"{"info": "via jsonl"}"#,
            JSONL_NODE_ETAG,
        ))
        .await?;
    assert_eq!(res.status(), StatusCode::OK);
    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let patched: serde_json::Value = serde_json::from_slice(&bytes)?;
    assert_eq!(patched["info"], "via jsonl");

    clean(&pool).await;
    Ok(())
}

/// G. Mapping failure (NULL lat/lon) does not commit — payload stays unchanged.
///
/// Inserts a row with NULL lat and lon (schema allows it). `patch_node_in_postgres`
/// must fail with `NodeWriteError::Mapping` before committing, leaving the payload
/// untouched.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn postgres_node_patch_mapping_failure_does_not_commit() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;

    // Seed a row with NULL lat/lon and a known payload.
    sqlx::query(
        "INSERT INTO domain_nodes (id, kind, title, lat, lon, payload) \
         VALUES ($1, 'test', 'Null Loc', NULL, NULL, '{\"info\":\"original\"}'::jsonb)",
    )
    .bind(NODE_NULL_LOC)
    .execute(&pool)
    .await
    .expect("seed null-location node");

    let result = patch_node_in_postgres(
        &pool,
        NODE_NULL_LOC,
        NodePatchInput {
            info: Some(Some("changed".to_string())),
            search_visibility: None,
        },
    )
    .await;

    // Must fail with a Mapping error — NULL location cannot be projected.
    let err = match result {
        Err(e) => e,
        Ok(_) => panic!("patch must fail for a node with NULL location, but returned Ok"),
    };
    let err_str = err.to_string();
    assert!(
        err_str.contains("failed to map"),
        "expected Mapping error, got: {err_str}"
    );

    // DB payload must be unchanged — no commit should have occurred.
    let (payload_text,): (String,) =
        sqlx::query_as("SELECT payload::text FROM domain_nodes WHERE id = $1")
            .bind(NODE_NULL_LOC)
            .fetch_one(&pool)
            .await
            .expect("row still present");
    let payload: serde_json::Value = serde_json::from_str(&payload_text)?;
    assert_eq!(
        payload.get("info").and_then(|v| v.as_str()),
        Some("original"),
        "payload must not have been modified by a failed patch"
    );

    clean(&pool).await;
    Ok(())
}

/// H. Non-object payload is rejected without committing.
///
/// Inserts a row with an array payload `[]` (valid JSONB, but not an object).
/// `patch_node_in_postgres` must return a Mapping error before any mutation.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn postgres_node_patch_non_object_payload_is_rejected_without_commit() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;

    // Seed a row with an array payload (non-object JSONB — data corruption scenario).
    sqlx::query(
        "INSERT INTO domain_nodes (id, kind, title, lat, lon, payload) \
         VALUES ($1, 'test', 'Bad Payload', 53.5, 10.0, '[]'::jsonb)",
    )
    .bind(NODE_BAD_PAYLOAD)
    .execute(&pool)
    .await
    .expect("seed non-object payload node");

    let result = patch_node_in_postgres(
        &pool,
        NODE_BAD_PAYLOAD,
        NodePatchInput {
            info: Some(Some("inject".to_string())),
            search_visibility: None,
        },
    )
    .await;

    let err = match result {
        Err(e) => e,
        Ok(_) => panic!("patch must fail for a node with non-object payload, but returned Ok"),
    };
    let err_str = err.to_string();
    assert!(
        err_str.contains("failed to map"),
        "expected Mapping error, got: {err_str}"
    );

    // DB payload must be unchanged.
    let (payload_text,): (String,) =
        sqlx::query_as("SELECT payload::text FROM domain_nodes WHERE id = $1")
            .bind(NODE_BAD_PAYLOAD)
            .fetch_one(&pool)
            .await
            .expect("row still present");
    assert_eq!(
        payload_text.trim(),
        "[]",
        "non-object payload must be untouched"
    );

    clean(&pool).await;
    Ok(())
}

// ── POST /nodes PostgreSQL write path ───────────────────────────────────────

fn post_node_req(cookie: &str, json_body: &str) -> Request<body::Body> {
    let mut payload: serde_json::Value =
        serde_json::from_str(json_body).expect("node test request must be JSON");
    if let Some(object) = payload.as_object_mut() {
        object
            .entry("operation_id")
            .or_insert_with(|| serde_json::Value::String(uuid::Uuid::new_v4().to_string()));
    }
    Request::post("/nodes")
        .header("Content-Type", "application/json")
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .header("Cookie", cookie)
        .body(body::Body::from(payload.to_string()))
        .unwrap()
}

/// I. `POST /nodes` persists a transactional insert, updates the cache after
/// persistence, never appends JSONL, and `load_nodes_from_postgres`
/// reconstructs the same node (including the new `address` field) after a
/// simulated restart.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn postgres_node_create_persists_and_reload_sees_it() -> Result<()> {
    const ACTOR_ID: &str = "10000000-0000-0000-0000-000000000003";

    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean_all_nodes(&pool).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, state) = postgres_write_app(pool.clone(), ACTOR_ID).await?;

    let body = r#"{"title":"New Node","kind":"Werkstatt","address":"Musterstraße 1, 12345 Musterstadt","location":{"lat":53.55,"lon":9.99},"summary":"Short summary","tags":["a","b"]}"#;
    let res = app.clone().oneshot(post_node_req(&cookie, body)).await?;
    assert_eq!(res.status(), StatusCode::CREATED);

    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let created: serde_json::Value = serde_json::from_slice(&bytes)?;
    let id = created["id"].as_str().context("id present")?.to_string();
    assert!(
        uuid::Uuid::parse_str(&id).is_ok(),
        "server must generate a UUID id"
    );
    assert_eq!(created["title"], "New Node");
    assert_eq!(created["kind"], "Werkstatt");
    assert_eq!(created["address"], "Musterstraße 1, 12345 Musterstadt");
    assert_eq!(created["location"]["lat"], 53.55);
    assert_eq!(created["location"]["lon"], 9.99);
    assert!(created["created_at"].as_str().is_some());
    assert_eq!(created["created_at"], created["updated_at"]);

    // DB row: explicit columns plus JSONB payload (summary, tags, address).
    let (kind, title, lat, lon): (String, String, Option<f64>, Option<f64>) =
        sqlx::query_as("SELECT kind, title, lat, lon FROM domain_nodes WHERE id = $1")
            .bind(&id)
            .fetch_one(&pool)
            .await
            .expect("node row must exist after create");
    assert_eq!(kind, "Werkstatt");
    assert_eq!(title, "New Node");
    assert!((lat.unwrap() - 53.55).abs() < 1e-9);
    assert!((lon.unwrap() - 9.99).abs() < 1e-9);

    let (payload_text,): (String,) =
        sqlx::query_as("SELECT payload::text FROM domain_nodes WHERE id = $1")
            .bind(&id)
            .fetch_one(&pool)
            .await
            .expect("payload readable");
    let payload: serde_json::Value = serde_json::from_str(&payload_text)?;
    assert_eq!(
        payload.get("address").and_then(|v| v.as_str()),
        Some("Musterstraße 1, 12345 Musterstadt")
    );
    assert_eq!(
        payload.get("summary").and_then(|v| v.as_str()),
        Some("Short summary")
    );
    assert_eq!(
        payload.get("tags").and_then(|v| v.as_array()).map(Vec::len),
        Some(2)
    );

    // The same Webungsaktion must also create exactly one durable derived
    // Faden in the configured PostgreSQL edge store.
    let (edge_source, edge_target, edge_kind): (String, String, String) = sqlx::query_as(
        "SELECT source_id, target_id, edge_kind FROM domain_edges WHERE target_id = $1",
    )
    .bind(&id)
    .fetch_one(&pool)
    .await
    .expect("derived Faden must exist after node create");
    assert_eq!(edge_source, ACTOR_ID);
    assert_eq!(edge_target, id);
    assert_eq!(edge_kind, "reference");

    // JSONL must NOT be appended in PostgreSQL write mode.
    let nodes_file = in_dir.join("demo.nodes.jsonl");
    assert!(
        !nodes_file.exists(),
        "PostgreSQL write mode must not create or write the JSONL nodes file"
    );

    // In-memory cache contains the node immediately (read-your-writes).
    {
        let nodes = state.nodes.read().await;
        let node = nodes.get(&id).expect("created node present in cache");
        assert_eq!(node.title, "New Node");
        assert_eq!(
            node.address.as_deref(),
            Some("Musterstraße 1, 12345 Musterstadt")
        );
    }

    // Simulated restart: reload from PostgreSQL alone reconstructs the node.
    let reloaded = load_nodes_from_postgres(&pool)
        .await
        .expect("reload nodes from postgres");
    let node = reloaded.get(&id).expect("node reloaded from postgres");
    assert_eq!(node.title, "New Node");
    assert_eq!(node.kind, "Werkstatt");
    assert_eq!(
        node.address.as_deref(),
        Some("Musterstraße 1, 12345 Musterstadt")
    );
    assert_eq!(node.summary.as_deref(), Some("Short summary"));
    assert_eq!(node.tags, vec!["a".to_string(), "b".to_string()]);
    assert!((node.location.lat - 53.55).abs() < 1e-9);
    assert!((node.location.lon - 9.99).abs() < 1e-9);

    clean_all_nodes(&pool).await;
    Ok(())
}

/// I2. A staged migration may keep account writes on JSONL/read-only while
/// PostgreSQL remains the canonical read source and owns node/edge writes.
/// Node creation must still commit the node and origin Faden atomically.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn postgres_node_create_allows_jsonl_account_write_source() -> Result<()> {
    const ACTOR_ID: &str = "10000000-0000-0000-0000-000000000023";

    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean_all_nodes(&pool).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, state) = postgres_write_app_with_account_source(
        pool.clone(),
        ACTOR_ID,
        1_000,
        DomainAccountWriteSource::Jsonl,
    )
    .await?;
    assert_eq!(
        state.config.domain_account_write_source,
        DomainAccountWriteSource::Jsonl
    );

    let response = app
        .oneshot(post_node_req(
            &cookie,
            r#"{"title":"Staged Migration Node","kind":"Werkstatt","address":"Migrationsweg 1","location":{"lat":53.55,"lon":9.99},"operation_id":"30000000-0000-4000-8000-000000000123"}"#,
        ))
        .await?;
    assert_eq!(response.status(), StatusCode::CREATED);
    let response_body = body::to_bytes(response.into_body(), usize::MAX).await?;
    let created: serde_json::Value = serde_json::from_slice(&response_body)?;
    let node_id = created["id"].as_str().context("staged migration node id")?;

    let node_count: i64 = sqlx::query_scalar("SELECT count(*) FROM domain_nodes WHERE id = $1")
        .bind(node_id)
        .fetch_one(&pool)
        .await?;
    let edge_count: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM domain_edges WHERE source_id = $1 AND target_id = $2 \
         AND payload ->> 'source_type' = 'account' AND payload ->> 'target_type' = 'node'",
    )
    .bind(ACTOR_ID)
    .bind(node_id)
    .fetch_one(&pool)
    .await?;
    assert_eq!(node_count, 1);
    assert_eq!(edge_count, 1);
    assert!(
        !in_dir.join("demo.nodes.jsonl").exists(),
        "PostgreSQL node writes must not append JSONL in the staged configuration"
    );

    clean_all_nodes(&pool).await;
    Ok(())
}

/// J. Node creation and later edits share one stable Knüpffaden.
/// Real patches and full replacements restart its seven-day projection
/// lifecycle without replacing or multiplying the account→node relation.
/// Semantic no-ops keep both the node version and the Faden lifecycle unchanged.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn postgres_node_edits_reuse_stable_expiring_faden_without_noop_inflation() -> Result<()> {
    const ACTOR_ID: &str = "10000000-0000-4000-8000-000000000013";

    async fn faden_lifecycle(
        pool: &PgPool,
        actor_id: &str,
        node_id: &str,
    ) -> Result<(chrono::DateTime<chrono::Utc>, chrono::DateTime<chrono::Utc>)> {
        Ok(sqlx::query_as(
            "SELECT created_at, (payload->>'expires_at')::timestamptz
             FROM domain_edges WHERE source_id = $1 AND target_id = $2",
        )
        .bind(actor_id)
        .bind(node_id)
        .fetch_one(pool)
        .await?)
    }

    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean_all_nodes(&pool).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, _state) = postgres_write_app(pool.clone(), ACTOR_ID).await?;
    let created_response = app
        .clone()
        .oneshot(post_node_req(
            &cookie,
            r#"{"title":"Aktiver Knoten","kind":"Werkstatt","address":"Musterstraße 1","location":{"lat":53.55,"lon":9.99}}"#,
        ))
        .await?;
    assert_eq!(created_response.status(), StatusCode::CREATED);
    let created: serde_json::Value =
        serde_json::from_slice(&body::to_bytes(created_response.into_body(), usize::MAX).await?)?;
    let node_id = created["id"].as_str().context("created node id")?;
    let created_version = created["updated_at"]
        .as_str()
        .context("created node updated_at")?;
    let persisted_create_version: chrono::DateTime<chrono::Utc> =
        sqlx::query_scalar("SELECT updated_at FROM domain_nodes WHERE id = $1")
            .bind(node_id)
            .fetch_one(&pool)
            .await?;
    assert_eq!(created_version, persisted_create_version.to_rfc3339());
    let created_etag = format!("\"{created_version}\"");

    let initial_edge_count: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM domain_edges WHERE source_id = $1 AND target_id = $2",
    )
    .bind(ACTOR_ID)
    .bind(node_id)
    .fetch_one(&pool)
    .await?;
    assert_eq!(initial_edge_count, 1, "node creation projects one Faden");
    let initial_edge_id: String =
        sqlx::query_scalar("SELECT id FROM domain_edges WHERE source_id = $1 AND target_id = $2")
            .bind(ACTOR_ID)
            .bind(node_id)
            .fetch_one(&pool)
            .await?;
    let initial_faden_lifecycle = faden_lifecycle(&pool, ACTOR_ID, node_id).await?;
    assert_eq!(
        initial_faden_lifecycle.1 - initial_faden_lifecycle.0,
        chrono::Duration::days(7),
    );

    tokio::time::sleep(Duration::from_millis(2)).await;
    let patched_response = app
        .clone()
        .oneshot(patch_node_req(
            &cookie,
            node_id,
            r#"{"info":"Gemeinsam bearbeitet"}"#,
            &created_etag,
        ))
        .await?;
    assert_eq!(patched_response.status(), StatusCode::OK);
    let patched: serde_json::Value =
        serde_json::from_slice(&body::to_bytes(patched_response.into_body(), usize::MAX).await?)?;
    let patched_etag = format!(
        "\"{}\"",
        patched["updated_at"]
            .as_str()
            .context("patched node updated_at")?
    );
    assert_ne!(patched["updated_at"], created["updated_at"]);

    let after_patch_count: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM domain_edges WHERE source_id = $1 AND target_id = $2",
    )
    .bind(ACTOR_ID)
    .bind(node_id)
    .fetch_one(&pool)
    .await?;
    assert_eq!(
        after_patch_count, 1,
        "a real patch must reuse the stable Knüpffaden",
    );
    let after_patch_edge_id: String =
        sqlx::query_scalar("SELECT id FROM domain_edges WHERE source_id = $1 AND target_id = $2")
            .bind(ACTOR_ID)
            .bind(node_id)
            .fetch_one(&pool)
            .await?;
    assert_eq!(after_patch_edge_id, initial_edge_id);
    let after_patch_lifecycle = faden_lifecycle(&pool, ACTOR_ID, node_id).await?;
    assert!(
        after_patch_lifecycle.0 > initial_faden_lifecycle.0,
        "a real patch must restart the existing Knüpffaden lifecycle",
    );
    assert_eq!(
        after_patch_lifecycle.1 - after_patch_lifecycle.0,
        chrono::Duration::days(7),
    );

    let lifetimes: Vec<f64> = sqlx::query_scalar(
        "SELECT EXTRACT(EPOCH FROM ((payload->>'expires_at')::timestamptz - created_at))::double precision
         FROM domain_edges WHERE source_id = $1 AND target_id = $2",
    )
    .bind(ACTOR_ID)
    .bind(node_id)
    .fetch_all(&pool)
    .await?;
    assert_eq!(lifetimes.len(), 1);
    assert!(
        lifetimes
            .iter()
            .all(|seconds| (*seconds - 604_800.0).abs() < 0.001),
        "every projected Faden must expire exactly 168 hours after creation: {lifetimes:?}",
    );

    let noop_response = app
        .clone()
        .oneshot(patch_node_req(&cookie, node_id, r#"{}"#, &patched_etag))
        .await?;
    assert_eq!(noop_response.status(), StatusCode::OK);
    let noop: serde_json::Value =
        serde_json::from_slice(&body::to_bytes(noop_response.into_body(), usize::MAX).await?)?;
    assert_eq!(noop["updated_at"], patched["updated_at"]);
    let after_noop_count: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM domain_edges WHERE source_id = $1 AND target_id = $2",
    )
    .bind(ACTOR_ID)
    .bind(node_id)
    .fetch_one(&pool)
    .await?;
    assert_eq!(
        after_noop_count, 1,
        "an empty patch must not inflate the stable relation",
    );
    let after_noop_lifecycle = faden_lifecycle(&pool, ACTOR_ID, node_id).await?;
    assert_eq!(
        after_noop_lifecycle, after_patch_lifecycle,
        "an empty patch must not extend the Faden lifecycle",
    );

    let identical_info_response = app
        .clone()
        .oneshot(patch_node_req(
            &cookie,
            node_id,
            r#"{"info":"Gemeinsam bearbeitet"}"#,
            &patched_etag,
        ))
        .await?;
    assert_eq!(identical_info_response.status(), StatusCode::OK);
    let identical_info: serde_json::Value = serde_json::from_slice(
        &body::to_bytes(identical_info_response.into_body(), usize::MAX).await?,
    )?;
    assert_eq!(identical_info["updated_at"], patched["updated_at"]);

    let identical_visibility_response = app
        .clone()
        .oneshot(patch_node_req(
            &cookie,
            node_id,
            r#"{"search_visibility":"public"}"#,
            &patched_etag,
        ))
        .await?;
    assert_eq!(identical_visibility_response.status(), StatusCode::OK);
    let identical_visibility: serde_json::Value = serde_json::from_slice(
        &body::to_bytes(identical_visibility_response.into_body(), usize::MAX).await?,
    )?;
    assert_eq!(identical_visibility["updated_at"], patched["updated_at"]);
    let after_semantic_noops: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM domain_edges WHERE source_id = $1 AND target_id = $2",
    )
    .bind(ACTOR_ID)
    .bind(node_id)
    .fetch_one(&pool)
    .await?;
    assert_eq!(
        after_semantic_noops, 1,
        "identical info and visibility patches must not inflate the stable relation",
    );
    let after_semantic_noops_lifecycle = faden_lifecycle(&pool, ACTOR_ID, node_id).await?;
    assert_eq!(
        after_semantic_noops_lifecycle, after_patch_lifecycle,
        "identical patches must not extend the Faden lifecycle",
    );

    tokio::time::sleep(Duration::from_millis(2)).await;
    let replaced_response = app
        .clone()
        .oneshot(replace_node_req(
            &cookie,
            node_id,
            "Aktiver Knoten – überarbeitet",
            &patched_etag,
        ))
        .await?;
    assert_eq!(replaced_response.status(), StatusCode::OK);
    let replaced: serde_json::Value =
        serde_json::from_slice(&body::to_bytes(replaced_response.into_body(), usize::MAX).await?)?;
    assert_ne!(replaced["updated_at"], patched["updated_at"]);

    let after_replace_count: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM domain_edges WHERE source_id = $1 AND target_id = $2",
    )
    .bind(ACTOR_ID)
    .bind(node_id)
    .fetch_one(&pool)
    .await?;
    assert_eq!(
        after_replace_count, 1,
        "a full replacement must reuse the stable Knüpffaden",
    );
    let after_replace_edge_id: String =
        sqlx::query_scalar("SELECT id FROM domain_edges WHERE source_id = $1 AND target_id = $2")
            .bind(ACTOR_ID)
            .bind(node_id)
            .fetch_one(&pool)
            .await?;
    assert_eq!(after_replace_edge_id, initial_edge_id);
    let after_replace_lifecycle = faden_lifecycle(&pool, ACTOR_ID, node_id).await?;
    assert!(
        after_replace_lifecycle.0 > after_patch_lifecycle.0,
        "a real replacement must restart the existing Knüpffaden lifecycle",
    );
    assert_eq!(
        after_replace_lifecycle.1 - after_replace_lifecycle.0,
        chrono::Duration::days(7),
    );

    let replaced_etag = format!(
        "\"{}\"",
        replaced["updated_at"]
            .as_str()
            .context("replaced node updated_at")?
    );
    let identical_replace_response = app
        .clone()
        .oneshot(replace_node_req(
            &cookie,
            node_id,
            "Aktiver Knoten – überarbeitet",
            &replaced_etag,
        ))
        .await?;
    assert_eq!(identical_replace_response.status(), StatusCode::OK);
    let identical_replace: serde_json::Value = serde_json::from_slice(
        &body::to_bytes(identical_replace_response.into_body(), usize::MAX).await?,
    )?;
    assert_eq!(identical_replace["updated_at"], replaced["updated_at"]);
    let after_identical_replace: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM domain_edges WHERE source_id = $1 AND target_id = $2",
    )
    .bind(ACTOR_ID)
    .bind(node_id)
    .fetch_one(&pool)
    .await?;
    assert_eq!(
        after_identical_replace, 1,
        "an identical full replacement must not inflate the stable relation",
    );
    let after_identical_replace_lifecycle = faden_lifecycle(&pool, ACTOR_ID, node_id).await?;
    assert_eq!(
        after_identical_replace_lifecycle, after_replace_lifecycle,
        "an identical replacement must not extend the Faden lifecycle",
    );

    clean_all_nodes(&pool).await;
    Ok(())
}

/// J2. The post-commit edit projection retains the per-node advisory guard.
/// A concurrent deletion cannot pass the edit between its durable node write
/// and the derived Faden; after the edit completes, deletion removes both the
/// node and every activity edge without leaving an orphan.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn postgres_node_edit_projection_serializes_with_delete() -> Result<()> {
    const ACTOR_ID: &str = "10000000-0000-4000-8000-000000000014";

    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean_all_nodes(&pool).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, _state) = postgres_write_app(pool.clone(), ACTOR_ID).await?;
    let created_response = app
        .clone()
        .oneshot(post_node_req(
            &cookie,
            r#"{"title":"Race node","kind":"Werkstatt","address":"Musterstraße 2","location":{"lat":53.56,"lon":9.98}}"#,
        ))
        .await?;
    assert_eq!(created_response.status(), StatusCode::CREATED);
    let created: serde_json::Value =
        serde_json::from_slice(&body::to_bytes(created_response.into_body(), usize::MAX).await?)?;
    let node_id = created["id"]
        .as_str()
        .context("created race node id")?
        .to_string();
    let created_version = created["updated_at"]
        .as_str()
        .context("created race node updated_at")?
        .to_string();
    let created_etag = format!("\"{created_version}\"");

    // Hold the edge table after node creation. The patch can commit its node
    // row, but its derived edge must wait while the outer node guard remains held.
    let mut edge_blocker = pool.begin().await.context("begin edge blocker")?;
    sqlx::query("LOCK TABLE domain_edges IN ACCESS EXCLUSIVE MODE")
        .execute(&mut *edge_blocker)
        .await
        .context("block activity edge projection")?;

    let patch_app = app.clone();
    let patch_cookie = cookie.clone();
    let patch_node_id = node_id.clone();
    let mut patch_task = tokio::spawn(async move {
        patch_app
            .oneshot(patch_node_req(
                &patch_cookie,
                &patch_node_id,
                r#"{"info":"race update"}"#,
                &created_etag,
            ))
            .await
    });

    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    loop {
        let current: chrono::DateTime<chrono::Utc> =
            sqlx::query_scalar("SELECT updated_at FROM domain_nodes WHERE id = $1")
                .bind(&node_id)
                .fetch_one(&pool)
                .await?;
        if current.to_rfc3339() != created_version {
            break;
        }
        if tokio::time::Instant::now() >= deadline {
            anyhow::bail!("patch did not commit its node update before projection timeout");
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    assert!(
        tokio::time::timeout(Duration::from_millis(100), &mut patch_task)
            .await
            .is_err(),
        "patch must still be waiting for the blocked Faden projection",
    );

    let delete_pool = pool.clone();
    let delete_node_id = node_id.clone();
    let mut delete_task = tokio::spawn(async move {
        let mut guard = delete_pool.begin().await?;
        sqlx::query("SELECT pg_advisory_xact_lock($1::bigint)")
            .bind(node_mutation_lock_key(&delete_node_id))
            .execute(&mut *guard)
            .await?;
        let outcome = delete_node_with_edges_in_postgres(&delete_pool, &delete_node_id).await?;
        guard.commit().await?;
        Ok::<_, anyhow::Error>(outcome)
    });
    assert!(
        tokio::time::timeout(Duration::from_millis(150), &mut delete_task)
            .await
            .is_err(),
        "delete must wait while edit projection owns the per-node guard",
    );

    edge_blocker
        .commit()
        .await
        .context("release edge blocker")?;
    let patch_response = patch_task.await.context("join guarded patch")??;
    assert_eq!(patch_response.status(), StatusCode::OK);

    let delete_outcome = delete_task.await.context("join serialized delete")??;
    assert!(
        !delete_outcome.removed_edge_ids.is_empty(),
        "serialized delete must remove the projected activity edges",
    );
    let node_exists: bool =
        sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM domain_nodes WHERE id = $1)")
            .bind(&node_id)
            .fetch_one(&pool)
            .await?;
    let edge_count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM domain_edges WHERE target_id = $1")
            .bind(&node_id)
            .fetch_one(&pool)
            .await?;
    assert!(!node_exists, "serialized delete must remove the node");
    assert_eq!(
        edge_count, 0,
        "serialized delete must leave no orphan Faden"
    );

    clean_all_nodes(&pool).await;
    Ok(())
}

/// K. An account-scoped operation survives a simulated API restart. The first
/// request returns 201; the same semantic request from a newly built app returns
/// the existing node with 200; changed data under the same key returns 409.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn postgres_node_create_operation_replays_after_restart() -> Result<()> {
    const ACTOR_ID: &str = "20000000-0000-0000-0000-000000000001";
    const OPERATION_ID: &str = "30000000-0000-0000-0000-000000000001";

    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean_all_nodes(&pool).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);
    let request_body = format!(
        r#"{{"title":"Postgres Retry Node","kind":"Werkstatt","address":"Musterstraße 1","location":{{"lat":53.55,"lon":9.99}},"summary":"durable","operation_id":"{OPERATION_ID}"}}"#
    );

    let (first_app, first_cookie, _) = postgres_write_app(pool.clone(), ACTOR_ID).await?;
    let first_attempt = first_app
        .clone()
        .oneshot(post_node_req(&first_cookie, &request_body));
    let second_attempt = first_app
        .clone()
        .oneshot(post_node_req(&first_cookie, &request_body));
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
    let first_bytes = body::to_bytes(first.into_body(), usize::MAX).await?;
    let second_bytes = body::to_bytes(second.into_body(), usize::MAX).await?;
    let first_node: serde_json::Value = serde_json::from_slice(&first_bytes)?;
    let second_node: serde_json::Value = serde_json::from_slice(&second_bytes)?;
    assert_eq!(first_node["id"], second_node["id"]);
    let first_id = first_node["id"].as_str().context("created node id")?;

    // New app state and newly loaded PostgreSQL cache simulate an API restart.
    let (restarted_app, restarted_cookie, restarted_state) =
        postgres_write_app(pool.clone(), ACTOR_ID).await?;
    let replay = restarted_app
        .clone()
        .oneshot(post_node_req(&restarted_cookie, &request_body))
        .await?;
    assert_eq!(replay.status(), StatusCode::OK);
    let replay_bytes = body::to_bytes(replay.into_body(), usize::MAX).await?;
    let replay_node: serde_json::Value = serde_json::from_slice(&replay_bytes)?;
    assert_eq!(replay_node["id"], first_id);
    assert_eq!(restarted_state.nodes.read().await.len(), 1);

    let changed_body = format!(
        r#"{{"title":"Changed Retry Node","kind":"Werkstatt","address":"Musterstraße 1","location":{{"lat":53.55,"lon":9.99}},"summary":"durable","operation_id":"{OPERATION_ID}"}}"#
    );
    let conflict = restarted_app
        .oneshot(post_node_req(&restarted_cookie, &changed_body))
        .await?;
    assert_eq!(conflict.status(), StatusCode::CONFLICT);

    let (count, actor, operation): (i64, Option<String>, Option<String>) = sqlx::query_as(
        "SELECT count(*), min(create_actor_id), min(create_operation_id) FROM domain_nodes",
    )
    .fetch_one(&pool)
    .await?;
    assert_eq!(count, 1);
    assert_eq!(actor.as_deref(), Some(ACTOR_ID));
    assert_eq!(operation.as_deref(), Some(OPERATION_ID));

    let (edge_count, edge_source, edge_target): (i64, Option<String>, Option<String>) =
        sqlx::query_as("SELECT count(*), min(source_id), min(target_id) FROM domain_edges")
            .fetch_one(&pool)
            .await?;
    assert_eq!(edge_count, 1, "retries must project exactly one Faden");
    assert_eq!(edge_source.as_deref(), Some(ACTOR_ID));
    assert_eq!(edge_target.as_deref(), Some(first_id));

    clean_all_nodes(&pool).await;
    Ok(())
}

/// K. Invalid create payloads (missing required fields, out-of-bounds
/// coordinates) return a stable 400 and never touch the database.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn postgres_node_create_rejects_invalid_payload_without_side_effects() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean_all_nodes(&pool).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, state) =
        postgres_write_app(pool.clone(), "10000000-0000-0000-0000-000000000004").await?;

    // Missing required `address`.
    let res = app
        .clone()
        .oneshot(post_node_req(
            &cookie,
            r#"{"title":"No Address","kind":"Werkstatt","location":{"lat":53.5,"lon":10.0}}"#,
        ))
        .await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);

    // Out-of-bounds latitude.
    let res = app
        .clone()
        .oneshot(post_node_req(
            &cookie,
            r#"{"title":"Bad Coords","kind":"Werkstatt","address":"Somewhere","location":{"lat":999.0,"lon":10.0}}"#,
        ))
        .await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);

    let (count,): (i64,) = sqlx::query_as("SELECT count(*) FROM domain_nodes")
        .fetch_one(&pool)
        .await
        .expect("count domain_nodes rows");
    assert_eq!(count, 0, "invalid create requests must not persist a row");
    assert!(
        state.nodes.read().await.is_empty(),
        "invalid create requests must not populate the cache"
    );

    clean_all_nodes(&pool).await;
    Ok(())
}

/// K2. A PostgreSQL node create cannot race past guest-account deletion.
/// The create must wait for the account row lock and then fail once the exit
/// transaction has removed the canonical identity.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn postgres_node_create_rejects_creator_deleted_by_inflight_exit() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;

    let creator_id = "writepath-node-creator-race";
    let node_id = "writepath-node-race-create";
    sqlx::query(
        "INSERT INTO domain_accounts \
         (id, kind, title, map_state, radius_m, disabled, role, public_payload, private_payload) \
         VALUES ($1, 'garnrolle', 'Race Guest', 'not_on_map', 0, FALSE, 'gast', '{}', '{}')",
    )
    .bind(creator_id)
    .execute(&pool)
    .await
    .context("seed race creator account")?;

    let node = Node {
        id: node_id.to_string(),
        kind: "Werkstatt".to_string(),
        title: "Race-konsistenter Gastknoten".to_string(),
        created_at: "2026-07-20T15:00:00+00:00".to_string(),
        updated_at: "2026-07-20T15:00:00+00:00".to_string(),
        has_authoritative_created_at: true,
        created_by_account_id: Some(creator_id.to_string()),
        search_visibility: Default::default(),
        summary: None,
        info: None,
        tags: vec![],
        address: Some("Somewhere 2".to_string()),
        location: Location {
            lat: 53.5,
            lon: 10.0,
        },
    };

    let mut exit_tx = pool.begin().await.context("begin simulated guest exit")?;
    let locked_creator: String = sqlx::query_scalar(
        "SELECT id FROM domain_accounts WHERE id = $1 AND disabled = FALSE FOR UPDATE",
    )
    .bind(creator_id)
    .fetch_one(&mut *exit_tx)
    .await
    .context("lock creator as guest exit does")?;
    assert_eq!(locked_creator, creator_id);

    let create_pool = pool.clone();
    let mut create_task =
        tokio::spawn(async move { insert_domain_node(&create_pool, &node, None).await });
    assert!(
        tokio::time::timeout(Duration::from_millis(150), &mut create_task)
            .await
            .is_err(),
        "node create must wait while guest exit owns the account row lock"
    );

    sqlx::query("DELETE FROM domain_accounts WHERE id = $1")
        .bind(creator_id)
        .execute(&mut *exit_tx)
        .await
        .context("delete creator inside guest exit transaction")?;
    exit_tx
        .commit()
        .await
        .context("commit simulated guest exit")?;

    let create_result = create_task.await.context("join blocked node create")?;
    match create_result {
        Err(NodeCreateError::CreatorAccountUnavailable) => {}
        Err(error) => panic!("unexpected stale node create error: {error}"),
        Ok(_) => panic!("stale node create must not succeed after guest exit"),
    }
    let node_exists: bool =
        sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM domain_nodes WHERE id = $1)")
            .bind(node_id)
            .fetch_one(&pool)
            .await
            .context("check race node absence")?;
    assert!(
        !node_exists,
        "guest exit race must not leave an orphaned node"
    );

    clean(&pool).await;
    Ok(())
}

/// K2b. The production projection middleware holds a read guard for the whole
/// request. Guest exit must finish without attempting a read-to-write lock
/// upgrade; the following request performs the lazy projection refresh.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn postgres_guest_exit_completes_under_projection_middleware() -> Result<()> {
    const ACTOR_ID: &str = "20000000-0000-0000-0000-000000000098";
    const NODE_ID: &str = "writepath-guest-exit-cache-node";
    const EDGE_ID: &str = "writepath-guest-exit-cache-edge";

    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean_all_nodes(&pool).await;
    sqlx::query("DELETE FROM domain_accounts WHERE id = $1")
        .bind(ACTOR_ID)
        .execute(&pool)
        .await?;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (_app, cookie, state) = postgres_write_app(pool.clone(), ACTOR_ID).await?;
    let app = postgres_projected_router(&state);
    sqlx::query("UPDATE domain_accounts SET role = 'gast' WHERE id = $1")
        .bind(ACTOR_ID)
        .execute(&pool)
        .await?;
    let mut guest = admin_operator(ACTOR_ID);
    guest.role = Role::Gast;
    state.accounts.write().await.insert(guest);

    sqlx::query(
        "INSERT INTO domain_nodes \
         (id, kind, title, lat, lon, created_at, updated_at, payload) \
         VALUES ($1, 'Ort', 'Exit cache node', 53.5, 10.0, NOW(), NOW(), \
                 jsonb_build_object('created_by_account_id', $2::text))",
    )
    .bind(NODE_ID)
    .bind(ACTOR_ID)
    .execute(&pool)
    .await?;
    sqlx::query(
        "INSERT INTO domain_edges \
         (id, source_id, target_id, edge_kind, created_at, payload) \
         VALUES ($1, $2, $3, 'reference', NOW(), \
                 jsonb_build_object('source_type', 'account', 'target_type', 'node'))",
    )
    .bind(EDGE_ID)
    .bind(ACTOR_ID)
    .bind(NODE_ID)
    .execute(&pool)
    .await?;

    let preload_response = app
        .clone()
        .oneshot(
            Request::get("/nodes")
                .header("Host", "localhost")
                .body(body::Body::empty())?,
        )
        .await?;
    assert_eq!(preload_response.status(), StatusCode::OK);
    let cached_creator = state
        .nodes
        .read()
        .await
        .get(NODE_ID)
        .and_then(|node| node.created_by_account_id.clone());
    assert_eq!(cached_creator.as_deref(), Some(ACTOR_ID));
    assert!(
        state.edges.read().await.get(EDGE_ID).is_some(),
        "precondition: account-bound Faden must be present in runtime cache before exit"
    );

    let exit_request = Request::post("/accounts/me/exit")
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .header("Cookie", &cookie)
        .body(body::Body::empty())?;
    let exit_response =
        tokio::time::timeout(Duration::from_secs(5), app.clone().oneshot(exit_request))
            .await
            .context("guest exit challenge must not deadlock under projection middleware")??;
    let exit_status = exit_response.status();
    let exit_body = body::to_bytes(exit_response.into_body(), usize::MAX).await?;
    assert_eq!(
        exit_status,
        StatusCode::FORBIDDEN,
        "guest exit challenge body: {}",
        String::from_utf8_lossy(&exit_body)
    );
    let exit_payload: serde_json::Value = serde_json::from_slice(&exit_body)?;
    assert_eq!(exit_payload["error"], "STEP_UP_REQUIRED");
    let challenge_id = exit_payload["challenge_id"]
        .as_str()
        .context("guest exit must return challenge_id")?;

    let still_exists: bool =
        sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM domain_accounts WHERE id = $1)")
            .bind(ACTOR_ID)
            .fetch_one(&pool)
            .await?;
    assert!(
        still_exists,
        "a normal authenticated session must not delete the guest account"
    );

    let challenge = state
        .challenges
        .get(challenge_id)
        .context("guest exit challenge must exist before confirmation")?;
    assert_eq!(challenge.account_id, ACTOR_ID);
    assert_eq!(
        challenge.intent,
        weltgewebe_api::auth::challenges::ChallengeIntent::ExitGuestAccount
    );
    let step_up_token = state.step_up_tokens.create(
        challenge.id.clone(),
        ACTOR_ID.to_string(),
        challenge.device_id.clone(),
    );

    let confirm_request = Request::post("/auth/step-up/magic-link/consume")
        .header("Content-Type", "application/json")
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .header("Cookie", &cookie)
        .body(body::Body::from(
            serde_json::json!({
                "token": step_up_token,
                "challenge_id": challenge.id,
            })
            .to_string(),
        ))?;
    let confirm_response =
        tokio::time::timeout(Duration::from_secs(5), app.clone().oneshot(confirm_request))
            .await
            .context("confirmed guest exit must not deadlock under projection middleware")??;
    assert_eq!(confirm_response.status(), StatusCode::NO_CONTENT);

    let exists: bool =
        sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM domain_accounts WHERE id = $1)")
            .bind(ACTOR_ID)
            .fetch_one(&pool)
            .await?;
    assert!(!exists, "canonical guest account must be deleted");

    let refresh_request = Request::get("/nodes")
        .header("Host", "localhost")
        .body(body::Body::empty())?;
    let refresh_response =
        tokio::time::timeout(Duration::from_secs(5), app.oneshot(refresh_request))
            .await
            .context("next request must refresh projection after exit")??;
    assert_eq!(refresh_response.status(), StatusCode::OK);
    assert!(
        state.accounts.read().await.get(ACTOR_ID).is_none(),
        "lazy projection refresh must remove exited guest from runtime cache"
    );
    let refreshed_creator = state
        .nodes
        .read()
        .await
        .get(NODE_ID)
        .and_then(|node| node.created_by_account_id.clone());
    assert_eq!(
        refreshed_creator, None,
        "lazy projection refresh must retain the node without ghost guest ownership"
    );
    assert!(
        state.edges.read().await.get(EDGE_ID).is_none(),
        "lazy projection refresh must evict account-bound Fäden deleted by guest exit"
    );

    clean_all_nodes(&pool).await;
    Ok(())
}

/// K2c. Five concurrent node creates on the real five-connection runtime pool
/// must retain enough headroom for the nested edge transaction. A separate
/// blocker pool holds the edge table so it does not consume runtime capacity.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn concurrent_node_creates_preserve_runtime_pool_headroom() -> Result<()> {
    const ACTOR_ID: &str = "20000000-0000-0000-0000-000000000097";

    let database_url = direct_database_url();
    let runtime_pool = PgPoolOptions::new()
        .max_connections(5)
        .connect(&database_url)
        .await
        .context("connect five-slot runtime pool")?;
    let blocker_pool = PgPoolOptions::new()
        .max_connections(1)
        .connect(&database_url)
        .await
        .context("connect independent edge blocker pool")?;
    run_migrations(&runtime_pool).await;
    clean_all_nodes(&runtime_pool).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);
    let (app, cookie, _state) = postgres_write_app(runtime_pool.clone(), ACTOR_ID).await?;

    let mut edge_blocker = blocker_pool
        .begin()
        .await
        .context("begin external edge blocker")?;
    sqlx::query("LOCK TABLE domain_edges IN ACCESS EXCLUSIVE MODE")
        .execute(&mut *edge_blocker)
        .await
        .context("block derived edge writes")?;

    let mut tasks = Vec::new();
    for index in 0..5 {
        let app = app.clone();
        let cookie = cookie.clone();
        tasks.push(tokio::spawn(async move {
            let body = serde_json::json!({
                "title": format!("Pool Headroom {index}"),
                "kind": "Werkstatt",
                "address": format!("Poolweg {index}"),
                "location": {"lat": 53.5 + f64::from(index) / 1000.0, "lon": 10.0},
                "operation_id": format!("30000000-0000-4000-8000-{index:012}")
            })
            .to_string();
            app.oneshot(post_node_req(&cookie, &body)).await
        }));
    }

    tokio::time::sleep(Duration::from_millis(200)).await;
    assert!(
        tasks.iter().all(|task| !task.is_finished()),
        "all creates should be waiting behind the external edge-table blocker"
    );

    edge_blocker
        .commit()
        .await
        .context("release external edge blocker")?;
    tokio::time::timeout(Duration::from_secs(10), async {
        for task in tasks {
            let response = task
                .await
                .expect("join concurrent node create")
                .expect("node create response");
            assert_eq!(response.status(), StatusCode::CREATED);
        }
    })
    .await
    .context("five concurrent creates must complete without pool exhaustion")?;

    let node_count: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM domain_nodes WHERE payload ->> 'created_by_account_id' = $1",
    )
    .bind(ACTOR_ID)
    .fetch_one(&runtime_pool)
    .await?;
    let edge_count: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM domain_edges WHERE source_id = $1 AND payload ->> 'source_type' = 'account'",
    )
    .bind(ACTOR_ID)
    .fetch_one(&runtime_pool)
    .await?;
    assert_eq!(node_count, 5);
    assert_eq!(edge_count, 5);

    clean_all_nodes(&runtime_pool).await;
    Ok(())
}

/// K2d. A permanent derived-Faden capacity failure must roll back the complete
/// PostgreSQL Webungsaktion: node, trigger-created conversation, domain outbox
/// records and cache state remain unchanged.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn node_create_rolls_back_when_derived_faden_is_rejected() -> Result<()> {
    const ACTOR_ID: &str = "20000000-0000-0000-0000-000000000096";
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean_all_nodes(&pool).await;
    sqlx::query(
        "INSERT INTO domain_edges (id, source_id, target_id, edge_kind, created_at, payload) \
         VALUES ('writepath-edge-capacity-sentinel', 'role:a', 'role:b', 'reference', NOW(), \
                 jsonb_build_object('source_type','role','target_type','role'))",
    )
    .execute(&pool)
    .await?;
    let _edge_limit = EnvVarGuard::set("MAX_EDGES_CACHE", "1");
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);
    let (app, cookie, state) = postgres_write_app(pool.clone(), ACTOR_ID).await?;
    let conversations_before: i64 = sqlx::query_scalar("SELECT count(*) FROM domain_conversations")
        .fetch_one(&pool)
        .await?;
    let outbox_before: i64 = sqlx::query_scalar("SELECT count(*) FROM domain_outbox")
        .fetch_one(&pool)
        .await?;

    let body = serde_json::json!({
        "title": "Rollback Node",
        "kind": "Werkstatt",
        "address": "Rollbackweg 1",
        "location": {"lat": 53.5, "lon": 10.0},
        "operation_id": "30000000-0000-4000-8000-000000000096"
    })
    .to_string();
    let response = app.oneshot(post_node_req(&cookie, &body)).await?;
    assert_eq!(response.status(), StatusCode::INTERNAL_SERVER_ERROR);

    let durable_nodes: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM domain_nodes WHERE payload ->> 'created_by_account_id' = $1",
    )
    .bind(ACTOR_ID)
    .fetch_one(&pool)
    .await?;
    assert_eq!(
        durable_nodes, 0,
        "failed Faden projection must roll back node insert"
    );
    let conversations_after: i64 = sqlx::query_scalar("SELECT count(*) FROM domain_conversations")
        .fetch_one(&pool)
        .await?;
    let outbox_after: i64 = sqlx::query_scalar("SELECT count(*) FROM domain_outbox")
        .fetch_one(&pool)
        .await?;
    assert_eq!(
        conversations_after, conversations_before,
        "node conversation trigger effects must roll back with the failed Faden"
    );
    assert_eq!(
        outbox_after, outbox_before,
        "node and conversation outbox effects must roll back with the failed Faden"
    );
    assert!(
        state
            .nodes
            .read()
            .await
            .iter_in_order()
            .all(|node| node.created_by_account_id.as_deref() != Some(ACTOR_ID)),
        "failed atomic create must not publish the node to cache"
    );

    sqlx::query("DELETE FROM domain_edges WHERE id = 'writepath-edge-capacity-sentinel'")
        .execute(&pool)
        .await?;
    Ok(())
}

/// K2e. Guest ownership is bounded durably. Replaying the first operation stays
/// allowed at the cap, while a different operation is rejected.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn guest_node_limit_allows_replay_but_rejects_new_operation() -> Result<()> {
    const ACTOR_ID: &str = "20000000-0000-0000-0000-000000000095";
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean_all_nodes(&pool).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);
    let (app, cookie, state) =
        postgres_write_app_with_guest_limit(pool.clone(), ACTOR_ID, 1).await?;
    sqlx::query("UPDATE domain_accounts SET role = 'gast' WHERE id = $1")
        .bind(ACTOR_ID)
        .execute(&pool)
        .await?;
    let mut guest = admin_operator(ACTOR_ID);
    guest.role = Role::Gast;
    state.accounts.write().await.insert(guest);

    let first = serde_json::json!({
        "title": "Limit One",
        "kind": "Werkstatt",
        "address": "Limitweg 1",
        "location": {"lat": 53.5, "lon": 10.0},
        "operation_id": "30000000-0000-4000-8000-000000000095"
    })
    .to_string();
    let created = app.clone().oneshot(post_node_req(&cookie, &first)).await?;
    assert_eq!(created.status(), StatusCode::CREATED);
    let replay = app.clone().oneshot(post_node_req(&cookie, &first)).await?;
    assert_eq!(replay.status(), StatusCode::OK);

    let second = serde_json::json!({
        "title": "Limit Two",
        "kind": "Werkstatt",
        "address": "Limitweg 2",
        "location": {"lat": 53.6, "lon": 10.0},
        "operation_id": "30000000-0000-4000-8000-000000000094"
    })
    .to_string();
    let rejected = app.oneshot(post_node_req(&cookie, &second)).await?;
    assert_eq!(rejected.status(), StatusCode::TOO_MANY_REQUESTS);

    let owned_nodes: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM domain_nodes WHERE payload ->> 'created_by_account_id' = $1",
    )
    .bind(ACTOR_ID)
    .fetch_one(&pool)
    .await?;
    assert_eq!(owned_nodes, 1);

    clean_all_nodes(&pool).await;
    Ok(())
}

/// K2f. Two distinct guest creates racing at a one-node limit are serialized
/// by the account lifecycle/account-row lock contract. Exactly one may commit;
/// the loser must observe the committed count and fail with 429.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn concurrent_guest_node_creates_cannot_overshoot_limit() -> Result<()> {
    const ACTOR_ID: &str = "20000000-0000-0000-0000-000000000092";
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean_all_nodes(&pool).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);
    let (app, cookie, state) =
        postgres_write_app_with_guest_limit(pool.clone(), ACTOR_ID, 1).await?;
    sqlx::query("UPDATE domain_accounts SET role = 'gast' WHERE id = $1")
        .bind(ACTOR_ID)
        .execute(&pool)
        .await?;
    let mut guest = admin_operator(ACTOR_ID);
    guest.role = Role::Gast;
    state.accounts.write().await.insert(guest);

    let first = serde_json::json!({
        "title": "Concurrent Limit A",
        "kind": "Werkstatt",
        "address": "Limitweg A",
        "location": {"lat": 53.5, "lon": 10.0},
        "operation_id": "30000000-0000-4000-8000-000000000091"
    })
    .to_string();
    let second = serde_json::json!({
        "title": "Concurrent Limit B",
        "kind": "Werkstatt",
        "address": "Limitweg B",
        "location": {"lat": 53.6, "lon": 10.0},
        "operation_id": "30000000-0000-4000-8000-000000000092"
    })
    .to_string();

    let first_app = app.clone();
    let first_cookie = cookie.clone();
    let first_task = tokio::spawn(async move {
        first_app
            .oneshot(post_node_req(&first_cookie, &first))
            .await
    });
    let second_task =
        tokio::spawn(async move { app.oneshot(post_node_req(&cookie, &second)).await });

    let first_status = first_task
        .await
        .context("join first concurrent create")??
        .status();
    let second_status = second_task
        .await
        .context("join second concurrent create")??
        .status();
    let mut statuses = [first_status, second_status];
    statuses.sort_by_key(|status| status.as_u16());
    assert_eq!(
        statuses,
        [StatusCode::CREATED, StatusCode::TOO_MANY_REQUESTS]
    );

    let owned_nodes: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM domain_nodes WHERE payload ->> 'created_by_account_id' = $1",
    )
    .bind(ACTOR_ID)
    .fetch_one(&pool)
    .await?;
    assert_eq!(
        owned_nodes, 1,
        "concurrent guest creates must not overshoot the cap"
    );

    clean_all_nodes(&pool).await;
    Ok(())
}

/// K2g. An idempotent replay must lock the real existing node before repairing
/// its derived Faden. Otherwise a concurrent delete could remove the node while
/// the replay recreates an orphaned edge to the already deleted id.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn node_create_replay_locks_existing_node_before_faden_repair() -> Result<()> {
    const ACTOR_ID: &str = "20000000-0000-0000-0000-000000000094";
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean_all_nodes(&pool).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);
    let (app, cookie, state) = postgres_write_app(pool.clone(), ACTOR_ID).await?;

    let request_body = serde_json::json!({
        "title": "Replay Lock",
        "kind": "Werkstatt",
        "address": "Replayweg 1",
        "location": {"lat": 53.5, "lon": 10.0},
        "operation_id": "30000000-0000-4000-8000-000000000093"
    })
    .to_string();
    let created_response = app
        .clone()
        .oneshot(post_node_req(&cookie, &request_body))
        .await?;
    assert_eq!(created_response.status(), StatusCode::CREATED);
    let created_body = body::to_bytes(created_response.into_body(), usize::MAX).await?;
    let created: serde_json::Value = serde_json::from_slice(&created_body)?;
    let node_id = created["id"]
        .as_str()
        .context("created replay-lock node id")?
        .to_string();

    {
        let mut nodes = state.nodes.write().await;
        nodes.remove(&node_id);
    }

    sqlx::query(
        "DELETE FROM domain_edges WHERE source_id = $1 AND target_id = $2 \
         AND payload ->> 'source_type' = 'account' AND payload ->> 'target_type' = 'node'",
    )
    .bind(ACTOR_ID)
    .bind(&node_id)
    .execute(&pool)
    .await?;

    let mut mutation_blocker = pool.begin().await.context("begin replay node blocker")?;
    sqlx::query("SELECT pg_advisory_xact_lock($1::bigint)")
        .bind(node_mutation_lock_key(&node_id))
        .execute(&mut *mutation_blocker)
        .await
        .context("lock existing replay node")?;

    let replay_app = app.clone();
    let replay_cookie = cookie.clone();
    let replay_body = request_body.clone();
    let mut replay_task = tokio::spawn(async move {
        replay_app
            .oneshot(post_node_req(&replay_cookie, &replay_body))
            .await
    });
    assert!(
        tokio::time::timeout(Duration::from_millis(150), &mut replay_task)
            .await
            .is_err(),
        "replay must wait while the existing node mutation lock is held"
    );
    assert!(
        state.nodes.read().await.get(&node_id).is_none(),
        "blocked replay must not repopulate cache before owning the existing-node lock"
    );

    sqlx::query("DELETE FROM domain_nodes WHERE id = $1")
        .bind(&node_id)
        .execute(&mut *mutation_blocker)
        .await
        .context("delete replay node while holding mutation lock")?;
    mutation_blocker
        .commit()
        .await
        .context("commit concurrent replay-node deletion")?;
    let replay_response = tokio::time::timeout(Duration::from_secs(5), replay_task)
        .await
        .context("replay completes after existing-node lock release")?
        .context("join replay request")??;
    assert_eq!(replay_response.status(), StatusCode::CONFLICT);

    let repaired_edges: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM domain_edges WHERE source_id = $1 AND target_id = $2 \
         AND payload ->> 'source_type' = 'account' AND payload ->> 'target_type' = 'node'",
    )
    .bind(ACTOR_ID)
    .bind(&node_id)
    .fetch_one(&pool)
    .await?;
    assert_eq!(
        repaired_edges, 0,
        "replay must not recreate a Faden after the existing node was deleted"
    );
    assert!(
        state.nodes.read().await.get(&node_id).is_none(),
        "conflicting replay must not leave a stale node in cache"
    );

    clean_all_nodes(&pool).await;
    Ok(())
}

/// K2h. Post-commit cache publication re-reads canonical state under the same
/// node lock as edit/delete. A deletion that wins the commit→cache gap is
/// observed, while a later deletion waits until publication releases the lock.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn node_faden_cache_publication_is_delete_race_safe() -> Result<()> {
    const ACTOR_ID: &str = "20000000-0000-0000-0000-000000000095";
    const OPERATION_ONE: &str = "30000000-0000-4000-8000-000000000095";
    const OPERATION_TWO: &str = "30000000-0000-4000-8000-000000000096";

    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean_all_nodes(&pool).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);
    let (app, cookie, state) = postgres_write_app(pool.clone(), ACTOR_ID).await?;

    let create = |title: &str, operation_id: &str| {
        serde_json::json!({
            "title": title,
            "kind": "Werkstatt",
            "address": "Publikationsweg 1",
            "location": {"lat": 53.5, "lon": 10.0},
            "operation_id": operation_id
        })
        .to_string()
    };

    let first_response = app
        .clone()
        .oneshot(post_node_req(
            &cookie,
            &create("Delete wins", OPERATION_ONE),
        ))
        .await?;
    assert_eq!(first_response.status(), StatusCode::CREATED);
    let first_body = body::to_bytes(first_response.into_body(), usize::MAX).await?;
    let first: serde_json::Value = serde_json::from_slice(&first_body)?;
    let first_node_id = first["id"].as_str().context("first node id")?.to_string();
    let first_edge = state
        .edges
        .read()
        .await
        .iter_in_order()
        .find(|edge| edge.source_id == ACTOR_ID && edge.target_id == first_node_id)
        .cloned()
        .context("first origin Faden")?;
    delete_node_with_edges_in_postgres(&pool, &first_node_id)
        .await
        .context("delete before cache publication readback")?;
    let deleted_snapshot = lock_node_faden_cache_publication(&pool, &first_node_id, &first_edge)
        .await
        .context("lock deleted publication snapshot")?;
    assert!(deleted_snapshot.node.is_none());
    assert!(deleted_snapshot.edge.is_none());
    deleted_snapshot.release().await?;

    let second_response = app
        .clone()
        .oneshot(post_node_req(
            &cookie,
            &create("Delete waits", OPERATION_TWO),
        ))
        .await?;
    assert_eq!(second_response.status(), StatusCode::CREATED);
    let second_body = body::to_bytes(second_response.into_body(), usize::MAX).await?;
    let second: serde_json::Value = serde_json::from_slice(&second_body)?;
    let second_node_id = second["id"].as_str().context("second node id")?.to_string();
    let second_edge = state
        .edges
        .read()
        .await
        .iter_in_order()
        .find(|edge| edge.source_id == ACTOR_ID && edge.target_id == second_node_id)
        .cloned()
        .context("second origin Faden")?;
    let publication = lock_node_faden_cache_publication(&pool, &second_node_id, &second_edge)
        .await
        .context("lock live publication snapshot")?;
    assert!(publication.node.is_some());
    assert!(publication.edge.is_some());

    let delete_pool = pool.clone();
    let delete_id = second_node_id.clone();
    let mut delete_task = tokio::spawn(async move {
        let mut transaction = delete_pool.begin().await?;
        sqlx::query("SELECT pg_advisory_xact_lock($1::bigint)")
            .bind(node_mutation_lock_key(&delete_id))
            .execute(&mut *transaction)
            .await?;
        sqlx::query("DELETE FROM domain_nodes WHERE id = $1")
            .bind(&delete_id)
            .execute(&mut *transaction)
            .await?;
        transaction.commit().await
    });
    assert!(
        tokio::time::timeout(Duration::from_millis(150), &mut delete_task)
            .await
            .is_err(),
        "node deletion must wait while cache publication owns the node lock"
    );
    publication.release().await?;
    tokio::time::timeout(Duration::from_secs(5), delete_task)
        .await
        .context("delete completes after publication lock release")?
        .context("join publication-race delete")??;

    let second_exists: bool =
        sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM domain_nodes WHERE id = $1)")
            .bind(&second_node_id)
            .fetch_one(&pool)
            .await?;
    assert!(!second_exists);

    clean_all_nodes(&pool).await;
    Ok(())
}

/// K2i. Cache publication acquires both write guards before changing either
/// projection. Holding a node-cache reader must therefore prevent the origin
/// Faden from becoming visible on its own.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn node_and_faden_caches_publish_in_one_critical_section() -> Result<()> {
    const ACTOR_ID: &str = "20000000-0000-0000-0000-000000000096";

    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean_all_nodes(&pool).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);
    let (app, cookie, state) = postgres_write_app(pool.clone(), ACTOR_ID).await?;

    let edges_before = state.edges.read().await.len();
    let nodes_before = state.nodes.read().await.len();
    let node_reader = state.nodes.read().await;

    let request = post_node_req(
        &cookie,
        r#"{"title":"Atomic Cache Pair","kind":"Werkstatt","address":"Cacheweg 1","location":{"lat":53.5,"lon":10.0},"operation_id":"30000000-0000-4000-8000-000000000097"}"#,
    );
    let create_app = app.clone();
    let mut create_task = tokio::spawn(async move { create_app.oneshot(request).await });

    tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            let durable: bool = sqlx::query_scalar(
                "SELECT EXISTS (SELECT 1 FROM domain_nodes WHERE payload ->> 'created_by_account_id' = $1)",
            )
            .bind(ACTOR_ID)
            .fetch_one(&pool)
            .await
            .expect("probe durable node");
            if durable {
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .context("node becomes durable before blocked cache publication")?;

    assert!(
        tokio::time::timeout(Duration::from_millis(150), &mut create_task)
            .await
            .is_err(),
        "create response must wait while the node cache writer is blocked"
    );
    assert_eq!(
        state.edges.read().await.len(),
        edges_before,
        "origin Faden must not publish before the node cache writer is acquired"
    );
    assert_eq!(node_reader.len(), nodes_before);
    drop(node_reader);

    let response = tokio::time::timeout(Duration::from_secs(5), create_task)
        .await
        .context("create completes after node cache reader release")?
        .context("join cache-pair create")??;
    assert_eq!(response.status(), StatusCode::CREATED);
    let response_body = body::to_bytes(response.into_body(), usize::MAX).await?;
    let created: serde_json::Value = serde_json::from_slice(&response_body)?;
    let node_id = created["id"].as_str().context("cache-pair node id")?;

    assert!(state.nodes.read().await.get(node_id).is_some());
    assert!(
        state
            .edges
            .read()
            .await
            .iter_in_order()
            .any(|edge| edge.source_id == ACTOR_ID && edge.target_id == node_id),
        "node and origin Faden must both be present after publication"
    );

    clean_all_nodes(&pool).await;
    Ok(())
}

/// K3. The real PostgreSQL HTTP path keeps node and origin Faden in one
/// transaction while holding the account-lifecycle lock. A concurrent reader
/// cannot observe the node before the blocked Faden insert, and guest exit must
/// wait until the complete transaction commits.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn postgres_node_create_faden_projection_serializes_with_guest_exit() -> Result<()> {
    const ACTOR_ID: &str = "20000000-0000-0000-0000-000000000099";

    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean_all_nodes(&pool).await;
    sqlx::query("DELETE FROM domain_accounts WHERE id = $1")
        .bind(ACTOR_ID)
        .execute(&pool)
        .await?;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, state) = postgres_write_app(pool.clone(), ACTOR_ID).await?;
    sqlx::query("UPDATE domain_accounts SET role = 'gast' WHERE id = $1")
        .bind(ACTOR_ID)
        .execute(&pool)
        .await?;
    let mut guest = admin_operator(ACTOR_ID);
    guest.role = Role::Gast;
    state.accounts.write().await.insert(guest);

    // Stop the edge INSERT. Because node and Faden now share the same
    // transaction, the node must remain invisible to every other connection.
    let mut edge_blocker = pool.begin().await.context("begin edge blocker")?;
    sqlx::query("LOCK TABLE domain_edges IN ACCESS EXCLUSIVE MODE")
        .execute(&mut *edge_blocker)
        .await
        .context("lock edge table")?;

    let request = post_node_req(
        &cookie,
        r#"{"title":"Exit Race Node","kind":"Werkstatt","address":"Race 1","location":{"lat":53.55,"lon":9.99}}"#,
    );
    let create_app = app.clone();
    let mut create_task = tokio::spawn(async move { create_app.oneshot(request).await });
    assert!(
        tokio::time::timeout(Duration::from_millis(150), &mut create_task)
            .await
            .is_err(),
        "node create must wait while the edge table is blocked"
    );
    let visible_before_faden: bool = sqlx::query_scalar(
        "SELECT EXISTS (SELECT 1 FROM domain_nodes WHERE payload ->> 'created_by_account_id' = $1)",
    )
    .bind(ACTOR_ID)
    .fetch_one(&pool)
    .await?;
    assert!(
        !visible_before_faden,
        "atomic create must not expose the node before its origin Faden"
    );

    // The same transaction already owns the account lifecycle lock, so guest
    // exit cannot overtake the blocked atomic create.
    let lifecycle_key = account_lifecycle_lock_key(ACTOR_ID);
    tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            let mut probe = pool.begin().await.expect("begin lifecycle lock probe");
            let acquired: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1::bigint)")
                .bind(lifecycle_key)
                .fetch_one(&mut *probe)
                .await
                .expect("probe account lifecycle lock");
            probe
                .rollback()
                .await
                .expect("rollback lifecycle lock probe");
            if !acquired {
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .context("node create must hold account lifecycle lock across the durable-node/Faden gap")?;

    let exit_pool = pool.clone();
    let mut exit_task =
        tokio::spawn(async move { delete_guest_account(&exit_pool, ACTOR_ID).await });
    assert!(
        tokio::time::timeout(Duration::from_millis(150), &mut exit_task)
            .await
            .is_err(),
        "guest exit must wait for the account lifecycle lock until the derived Faden is durable"
    );

    edge_blocker
        .commit()
        .await
        .context("release edge blocker")?;
    let create_response = tokio::time::timeout(Duration::from_secs(5), create_task)
        .await
        .context("node request completes")?
        .context("join node request")??;
    let create_status = create_response.status();
    let create_body = body::to_bytes(create_response.into_body(), usize::MAX).await?;
    assert_eq!(
        create_status,
        StatusCode::CREATED,
        "node create body: {}",
        String::from_utf8_lossy(&create_body)
    );
    tokio::time::timeout(Duration::from_secs(5), exit_task)
        .await
        .context("guest exit completes after Faden projection")??
        .context("delete guest account")?;

    let account_exists: bool =
        sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM domain_accounts WHERE id = $1)")
            .bind(ACTOR_ID)
            .fetch_one(&pool)
            .await?;
    let orphan_edges: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM domain_edges WHERE source_id = $1 OR target_id = $1",
    )
    .bind(ACTOR_ID)
    .fetch_one(&pool)
    .await?;
    let owned_nodes: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM domain_nodes WHERE payload ->> 'created_by_account_id' = $1",
    )
    .bind(ACTOR_ID)
    .fetch_one(&pool)
    .await?;
    assert!(!account_exists);
    assert_eq!(
        orphan_edges, 0,
        "exit must remove every account-bound Faden"
    );
    assert_eq!(owned_nodes, 0, "retained node must be anonymized");

    clean_all_nodes(&pool).await;
    Ok(())
}

/// L. Direct write-path proof: a primary-key collision at the database level
/// is classified as `DuplicateId` and leaves the existing row untouched.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn insert_domain_node_classifies_duplicate_id() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean_all_nodes(&pool).await;

    let id = "writepath-node-dup-id";
    let make_node = |title: &str| Node {
        id: id.to_string(),
        kind: "Werkstatt".to_string(),
        title: title.to_string(),
        created_at: "2026-06-12T10:00:00+00:00".to_string(),
        updated_at: "2026-06-12T10:00:00+00:00".to_string(),
        has_authoritative_created_at: true,
        created_by_account_id: None,
        search_visibility: Default::default(),
        summary: None,
        info: None,
        tags: vec![],
        address: Some("Somewhere 1".to_string()),
        location: Location {
            lat: 53.5,
            lon: 10.0,
        },
    };

    insert_domain_node(&pool, &make_node("First"), None)
        .await
        .expect("first insert must succeed");

    let err = match insert_domain_node(&pool, &make_node("Second"), None).await {
        Err(error) => error,
        Ok(_) => panic!("duplicate id insert must fail"),
    };
    assert!(
        matches!(err, NodeCreateError::DuplicateId),
        "expected DuplicateId, got {err:?}"
    );

    let (title,): (String,) = sqlx::query_as("SELECT title FROM domain_nodes WHERE id = $1")
        .bind(id)
        .fetch_one(&pool)
        .await
        .expect("existing row still present");
    assert_eq!(
        title, "First",
        "duplicate insert must not overwrite the existing row"
    );

    clean_all_nodes(&pool).await;
    Ok(())
}

/// M. Full replacement preserves technical identity, and deletion removes only
/// node-typed edge projections in the same PostgreSQL transaction.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn replace_and_delete_node_cascade_is_transactional_in_postgres() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;

    seed_node(&pool, NODE_A, Some("old info"), None).await;
    sqlx::query(
        "INSERT INTO domain_edges \
         (id, source_id, target_id, edge_kind, payload) \
         VALUES \
         ('writepath-edge-node', $1, 'writepath-node-other', 'reference', \
          '{\"source_type\":\"node\",\"target_type\":\"node\"}'::jsonb), \
         ('writepath-edge-account-collision', $1, 'writepath-node-other', 'reference', \
          '{\"source_type\":\"account\",\"target_type\":\"node\"}'::jsonb), \
         ('writepath-edge-role-collision', 'writepath-node-other', $1, 'reference', \
          '{\"source_type\":\"node\",\"target_type\":\"role\"}'::jsonb)",
    )
    .bind(NODE_A)
    .execute(&pool)
    .await
    .context("seed node and account-typed edge fixtures")?;

    let before = load_nodes_from_postgres(&pool)
        .await
        .context("load node before audited replacement")?
        .get(NODE_A)
        .cloned()
        .context("seeded node must exist")?;
    let replacement = Node {
        id: NODE_A.to_string(),
        kind: "Werkstatt".to_string(),
        title: "Gemeinsam gepflegt".to_string(),
        created_at: "2026-01-01T00:00:00Z".to_string(),
        updated_at: "2026-07-15T04:00:00Z".to_string(),
        has_authoritative_created_at: true,
        created_by_account_id: None,
        search_visibility: Default::default(),
        summary: Some("Neue Zusammenfassung".to_string()),
        info: Some("Neue Information".to_string()),
        tags: vec!["commons".to_string()],
        address: Some("Neue Straße 1".to_string()),
        location: Location {
            lat: 53.55,
            lon: 10.05,
        },
    };
    let replace_audit = NodeMutationAudit::new(
        NodeMutationOperation::Replace,
        NODE_A,
        "postgres-actor",
        &before,
        Some(&replacement),
    )?;
    replace_node_in_postgres_audited(&pool, &replacement, &replace_audit)
        .await
        .context("replace node with audit")?;

    let row: (String, String, f64, f64, serde_json::Value) = sqlx::query_as(
        "SELECT kind, title, lat, lon, payload \
         FROM domain_nodes WHERE id = $1",
    )
    .bind(NODE_A)
    .fetch_one(&pool)
    .await
    .context("read replaced node")?;
    assert_eq!(row.0, "Werkstatt");
    assert_eq!(row.1, "Gemeinsam gepflegt");
    assert_eq!(row.2, 53.55);
    assert_eq!(row.3, 10.05);
    assert_eq!(row.4["summary"], "Neue Zusammenfassung");
    assert_eq!(row.4["info"], "Neue Information");
    assert_eq!(row.4["address"], "Neue Straße 1");
    assert_eq!(row.4["tags"], serde_json::json!(["commons"]));

    let persisted_replacement = load_nodes_from_postgres(&pool)
        .await
        .context("load node before audited delete")?
        .get(NODE_A)
        .cloned()
        .context("replaced node must exist")?;
    let delete_audit = NodeMutationAudit::new(
        NodeMutationOperation::Delete,
        NODE_A,
        "postgres-actor",
        &persisted_replacement,
        None,
    )?;
    let mut outcome = delete_node_with_edges_in_postgres_audited(&pool, NODE_A, &delete_audit)
        .await
        .context("delete node with projections and audit")?;
    outcome.removed_edge_ids.sort();
    assert_eq!(
        outcome.removed_edge_ids,
        vec!["writepath-edge-node".to_string()]
    );
    assert_eq!(
        outcome.conversation,
        NodeConversationDeleteEffect::DeletedEmpty
    );

    let node_exists: bool =
        sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM domain_nodes WHERE id = $1)")
            .bind(NODE_A)
            .fetch_one(&pool)
            .await?;
    assert!(!node_exists);

    let audits: Vec<(String, String, String, String, Option<String>)> = sqlx::query_as(
        "SELECT operation, actor_subject_hash, node_id, before_hash, after_hash          FROM domain_node_mutation_audit WHERE node_id = $1 ORDER BY occurred_at",
    )
    .bind(NODE_A)
    .fetch_all(&pool)
    .await
    .context("read node mutation audit rows")?;
    assert_eq!(audits.len(), 2);
    assert_eq!(audits[0].0, "replace");
    assert_eq!(audits[1].0, "delete");
    assert_eq!(audits[0].1, actor_subject_hash("postgres-actor"));
    assert_eq!(audits[1].1, actor_subject_hash("postgres-actor"));
    assert_eq!(audits[0].2, NODE_A);
    assert_eq!(audits[1].2, NODE_A);
    assert!(audits[0].4.is_some());
    assert!(audits[1].4.is_none());
    assert_ne!(audits[0].3, audits[0].4.as_deref().unwrap());
    assert_eq!(
        audits[0].4.as_deref(),
        Some(node_hash(&persisted_replacement)?.as_str()),
        "after hash must bind the microsecond-precision PostgreSQL truth"
    );

    let node_edge_exists: bool = sqlx::query_scalar(
        "SELECT EXISTS (SELECT 1 FROM domain_edges WHERE id = 'writepath-edge-node')",
    )
    .fetch_one(&pool)
    .await?;
    assert!(!node_edge_exists);

    let account_edge_exists: bool = sqlx::query_scalar(
        "SELECT EXISTS (SELECT 1 FROM domain_edges \
         WHERE id = 'writepath-edge-account-collision')",
    )
    .fetch_one(&pool)
    .await?;
    assert!(account_edge_exists);

    let role_edge_exists: bool = sqlx::query_scalar(
        "SELECT EXISTS (SELECT 1 FROM domain_edges \
         WHERE id = 'writepath-edge-role-collision')",
    )
    .fetch_one(&pool)
    .await?;
    assert!(role_edge_exists);

    clean(&pool).await;
    Ok(())
}

/// N. Unique untyped legacy edges are classified as node projections and
/// deleted in the same PostgreSQL transaction.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn delete_node_removes_unique_untyped_legacy_postgres_edge() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;

    seed_node(&pool, NODE_A, Some("kept"), None).await;
    sqlx::query(
        "INSERT INTO domain_edges \
         (id, source_id, target_id, edge_kind, payload) \
         VALUES \
         ('writepath-edge-node', $1, 'writepath-node-other', 'reference', \
          '{\"source_type\":\"node\",\"target_type\":\"node\"}'::jsonb), \
         ('writepath-edge-untyped-legacy', $1, 'writepath-node-other', 'reference', \
          '{\"target_type\":\"node\"}'::jsonb), \
         ('writepath-edge-kept', 'writepath-node-other', 'writepath-node-third', 'reference', \
          '{\"source_type\":\"node\",\"target_type\":\"node\"}'::jsonb)",
    )
    .bind(NODE_A)
    .execute(&pool)
    .await
    .context("seed unique untyped edge fixtures")?;

    let mut outcome = delete_node_with_edges_in_postgres(&pool, NODE_A)
        .await
        .context("delete node with legacy edge")?;
    outcome.removed_edge_ids.sort();
    assert_eq!(
        outcome.removed_edge_ids,
        vec![
            "writepath-edge-node".to_string(),
            "writepath-edge-untyped-legacy".to_string()
        ]
    );
    assert_eq!(
        outcome.conversation,
        NodeConversationDeleteEffect::DeletedEmpty
    );

    let node_exists: bool =
        sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM domain_nodes WHERE id = $1)")
            .bind(NODE_A)
            .fetch_one(&pool)
            .await?;
    assert!(!node_exists);

    for edge_id in ["writepath-edge-node", "writepath-edge-untyped-legacy"] {
        let edge_exists: bool =
            sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM domain_edges WHERE id = $1)")
                .bind(edge_id)
                .fetch_one(&pool)
                .await?;
        assert!(!edge_exists, "{edge_id} must be removed");
    }
    let kept_exists: bool =
        sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM domain_edges WHERE id = $1)")
            .bind("writepath-edge-kept")
            .fetch_one(&pool)
            .await?;
    assert!(kept_exists);

    clean(&pool).await;
    Ok(())
}

/// O. Account collisions make untyped endpoints ambiguous and abort
/// PostgreSQL node deletion before any node or edge mutation.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn delete_node_rejects_untyped_postgres_account_collision_without_partial_mutation(
) -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;

    seed_node(&pool, NODE_A, Some("kept"), None).await;
    sqlx::query(
        "INSERT INTO domain_accounts \
         (id, kind, title, map_state, role, disabled, webauthn_user_id) \
         VALUES ($1, 'garnrolle', 'Colliding Account', 'not_on_map', 'weber', FALSE, $2::uuid)",
    )
    .bind(NODE_A)
    .bind(uuid::Uuid::new_v4().to_string())
    .execute(&pool)
    .await
    .context("seed colliding account fixture")?;
    sqlx::query(
        "INSERT INTO domain_edges \
         (id, source_id, target_id, edge_kind, payload) \
         VALUES \
         ('writepath-edge-node', $1, 'writepath-node-other', 'reference', \
          '{\"source_type\":\"node\",\"target_type\":\"node\"}'::jsonb), \
         ('writepath-edge-untyped-account-collision', $1, 'writepath-node-other', 'reference', \
          '{\"target_type\":\"node\"}'::jsonb)",
    )
    .bind(NODE_A)
    .execute(&pool)
    .await
    .context("seed ambiguous collision edge fixtures")?;

    let err = match delete_node_with_edges_in_postgres(&pool, NODE_A).await {
        Err(error) => error,
        Ok(_) => panic!("delete must fail for ambiguous edge endpoint collisions"),
    };
    assert!(
        matches!(err, NodeWriteError::InvalidEdgeReference(_)),
        "expected InvalidEdgeReference, got {err:?}"
    );

    let node_exists: bool =
        sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM domain_nodes WHERE id = $1)")
            .bind(NODE_A)
            .fetch_one(&pool)
            .await?;
    assert!(node_exists);

    for edge_id in [
        "writepath-edge-node",
        "writepath-edge-untyped-account-collision",
    ] {
        let edge_exists: bool =
            sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM domain_edges WHERE id = $1)")
                .bind(edge_id)
                .fetch_one(&pool)
                .await?;
        assert!(edge_exists, "{edge_id} must remain after failed delete");
    }

    clean(&pool).await;
    Ok(())
}

/// P. A role-typed collision evidence makes untyped endpoints ambiguous and
/// aborts before any node or edge mutation.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn delete_node_rejects_untyped_postgres_role_collision_without_partial_mutation() -> Result<()>
{
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;

    seed_node(&pool, NODE_A, Some("kept"), None).await;
    sqlx::query(
        "INSERT INTO domain_edges \
         (id, source_id, target_id, edge_kind, payload) \
         VALUES \
         ('writepath-edge-node', $1, 'writepath-node-other', 'reference', \
          '{\"source_type\":\"node\",\"target_type\":\"node\"}'::jsonb), \
         ('writepath-edge-role-evidence', 'writepath-node-other', $1, 'reference', \
          '{\"source_type\":\"node\",\"target_type\":\"role\"}'::jsonb), \
         ('writepath-edge-untyped-role-collision', $1, 'writepath-node-other', 'reference', \
          '{\"target_type\":\"node\"}'::jsonb)",
    )
    .bind(NODE_A)
    .execute(&pool)
    .await
    .context("seed role collision edge fixtures")?;

    let err = match delete_node_with_edges_in_postgres(&pool, NODE_A).await {
        Err(error) => error,
        Ok(_) => panic!("delete must fail for ambiguous role endpoint collision"),
    };
    assert!(
        matches!(err, NodeWriteError::InvalidEdgeReference(_)),
        "expected InvalidEdgeReference, got {err:?}"
    );

    let node_exists: bool =
        sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM domain_nodes WHERE id = $1)")
            .bind(NODE_A)
            .fetch_one(&pool)
            .await?;
    assert!(node_exists);

    for edge_id in [
        "writepath-edge-node",
        "writepath-edge-role-evidence",
        "writepath-edge-untyped-role-collision",
    ] {
        let edge_exists: bool =
            sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM domain_edges WHERE id = $1)")
                .bind(edge_id)
                .fetch_one(&pool)
                .await?;
        assert!(edge_exists, "{edge_id} must remain after failed delete");
    }

    clean(&pool).await;
    Ok(())
}

/// Q. Invalid typed endpoints still fail closed before mutation.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn delete_node_rejects_invalid_postgres_edge_type_without_partial_mutation() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;

    seed_node(&pool, NODE_A, Some("kept"), None).await;
    sqlx::query(
        "INSERT INTO domain_edges \
         (id, source_id, target_id, edge_kind, payload) \
         VALUES \
         ('writepath-edge-node', $1, 'writepath-node-other', 'reference', \
          '{\"source_type\":\"node\",\"target_type\":\"node\"}'::jsonb), \
         ('writepath-edge-invalid-collision', 'writepath-node-other', $1, 'reference', \
          '{\"source_type\":\"node\",\"target_type\":\"group\"}'::jsonb)",
    )
    .bind(NODE_A)
    .execute(&pool)
    .await
    .context("seed invalid edge fixtures")?;

    let err = match delete_node_with_edges_in_postgres(&pool, NODE_A).await {
        Err(error) => error,
        Ok(_) => panic!("delete must fail for invalid edge endpoint type"),
    };
    assert!(
        matches!(err, NodeWriteError::InvalidEdgeReference(_)),
        "expected InvalidEdgeReference, got {err:?}"
    );

    let node_exists: bool =
        sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM domain_nodes WHERE id = $1)")
            .bind(NODE_A)
            .fetch_one(&pool)
            .await?;
    assert!(node_exists);

    for edge_id in ["writepath-edge-node", "writepath-edge-invalid-collision"] {
        let edge_exists: bool =
            sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM domain_edges WHERE id = $1)")
                .bind(edge_id)
                .fetch_one(&pool)
                .await?;
        assert!(edge_exists, "{edge_id} must remain after failed delete");
    }

    clean(&pool).await;
    Ok(())
}

/// R. The ETag returned by a PostgreSQL PUT must match the persisted
/// microsecond-precision timestamp and be reusable immediately.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn postgres_replace_response_etag_is_reusable() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;
    seed_node(&pool, NODE_A, Some("before"), None).await;
    // This is an ETag precision test for a PRIVATE node. Seed an actual
    // canonical creator binding so the authenticated actor is authorized to
    // read and reuse its version; a foreign Admin must remain denied.
    let owner_id = "10000000-0000-0000-0000-000000000096";
    sqlx::query(
        "UPDATE domain_nodes
         SET search_visibility='private',
             payload = jsonb_set(payload, '{created_by_account_id}', to_jsonb($2::text), true)
         WHERE id=$1",
    )
    .bind(NODE_A)
    .bind(owner_id)
    .execute(&pool)
    .await?;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);
    let (app, cookie, _state) = postgres_write_app(pool.clone(), owner_id).await?;

    let first_response = app
        .clone()
        .oneshot(replace_node_req(
            &cookie,
            NODE_A,
            "First replace",
            SEEDED_NODE_ETAG,
        ))
        .await?;
    assert_eq!(first_response.status(), StatusCode::OK);
    let first_body = body::to_bytes(first_response.into_body(), usize::MAX).await?;
    let first_node: serde_json::Value = serde_json::from_slice(&first_body)?;
    assert_eq!(first_node["search_visibility"], "private");
    let reusable_etag = format!(
        "\"{}\"",
        first_node["updated_at"]
            .as_str()
            .expect("replace response has updated_at"),
    );

    let second_response = app
        .oneshot(replace_node_req(
            &cookie,
            NODE_A,
            "Second replace",
            &reusable_etag,
        ))
        .await?;
    assert_eq!(second_response.status(), StatusCode::OK);
    let second_body = body::to_bytes(second_response.into_body(), usize::MAX).await?;
    let second_node: serde_json::Value = serde_json::from_slice(&second_body)?;
    assert_eq!(second_node["title"], "Second replace");
    assert_eq!(second_node["search_visibility"], "private");

    let persisted_visibility: String =
        sqlx::query_scalar("SELECT search_visibility FROM domain_nodes WHERE id = $1")
            .bind(NODE_A)
            .fetch_one(&pool)
            .await?;
    assert_eq!(persisted_visibility, "private");

    let persisted_updated_at: chrono::DateTime<chrono::Utc> =
        sqlx::query_scalar("SELECT updated_at FROM domain_nodes WHERE id = $1")
            .bind(NODE_A)
            .fetch_one(&pool)
            .await?;
    assert_eq!(second_node["updated_at"], persisted_updated_at.to_rfc3339(),);

    clean(&pool).await;
    Ok(())
}

/// S. A second API instance must compare `If-Match` against PostgreSQL,
/// not against its stale process-local cache.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn stale_second_instance_cache_cannot_overwrite_newer_postgres_node() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;
    seed_node(&pool, NODE_A, Some("before"), None).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (first_app, first_cookie, _first_state) =
        postgres_write_app(pool.clone(), "10000000-0000-0000-0000-000000000097").await?;
    let (second_app, second_cookie, second_state) =
        postgres_write_app(pool.clone(), "10000000-0000-0000-0000-000000000098").await?;

    assert_eq!(
        second_state
            .nodes
            .read()
            .await
            .get(NODE_A)
            .map(|node| node.updated_at.as_str()),
        Some("2026-01-01T00:00:00+00:00"),
    );

    let first_response = first_app
        .oneshot(patch_node_req(
            &first_cookie,
            NODE_A,
            r#"{"info": "first instance wins"}"#,
            SEEDED_NODE_ETAG,
        ))
        .await?;
    assert_eq!(first_response.status(), StatusCode::OK);
    let first_body = body::to_bytes(first_response.into_body(), usize::MAX).await?;
    let first_node: serde_json::Value = serde_json::from_slice(&first_body)?;
    assert_eq!(first_node["info"], "first instance wins");
    assert_ne!(first_node["updated_at"], "2026-01-01T00:00:00+00:00");

    let stale_response = second_app
        .oneshot(patch_node_req(
            &second_cookie,
            NODE_A,
            r#"{"info": "stale overwrite"}"#,
            SEEDED_NODE_ETAG,
        ))
        .await?;
    assert_eq!(stale_response.status(), StatusCode::PRECONDITION_FAILED);
    let stale_body = body::to_bytes(stale_response.into_body(), usize::MAX).await?;
    let current_node: serde_json::Value = serde_json::from_slice(&stale_body)?;
    assert_eq!(current_node["info"], "first instance wins");
    assert_eq!(current_node["updated_at"], first_node["updated_at"]);

    let persisted_info: Option<String> =
        sqlx::query_scalar("SELECT payload->>'info' FROM domain_nodes WHERE id = $1")
            .bind(NODE_A)
            .fetch_one(&pool)
            .await?;
    assert_eq!(persisted_info.as_deref(), Some("first instance wins"));

    clean(&pool).await;
    Ok(())
}

/// T. Distinct-node mutations cannot consume the whole five-connection pool
/// with advisory-lock transactions while every request waits for a second
/// connection to perform its update.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn concurrent_distinct_node_replaces_preserve_pool_headroom() -> Result<()> {
    let pool = PgPoolOptions::new()
        .max_connections(5)
        .connect(&direct_database_url())
        .await
        .context("connect five-connection production-sized pool")?;
    run_migrations(&pool).await;
    clean(&pool).await;

    let ids = [
        "writepath-node-pool-1",
        "writepath-node-pool-2",
        "writepath-node-pool-3",
        "writepath-node-pool-4",
        "writepath-node-pool-5",
    ];
    for id in ids {
        seed_node(&pool, id, Some("before"), None).await;
    }

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);
    let (app, cookie, _state) =
        postgres_write_app(pool.clone(), "10000000-0000-0000-0000-000000000099").await?;

    let requests = async {
        tokio::join!(
            app.clone().oneshot(replace_node_req(
                &cookie,
                ids[0],
                "Parallel 1",
                SEEDED_NODE_ETAG
            )),
            app.clone().oneshot(replace_node_req(
                &cookie,
                ids[1],
                "Parallel 2",
                SEEDED_NODE_ETAG
            )),
            app.clone().oneshot(replace_node_req(
                &cookie,
                ids[2],
                "Parallel 3",
                SEEDED_NODE_ETAG
            )),
            app.clone().oneshot(replace_node_req(
                &cookie,
                ids[3],
                "Parallel 4",
                SEEDED_NODE_ETAG
            )),
            app.clone().oneshot(replace_node_req(
                &cookie,
                ids[4],
                "Parallel 5",
                SEEDED_NODE_ETAG
            )),
        )
    };
    let responses = tokio::time::timeout(Duration::from_secs(10), requests)
        .await
        .context("five distinct node replacements must not deadlock the pool")?;
    for response in [
        responses.0?,
        responses.1?,
        responses.2?,
        responses.3?,
        responses.4?,
    ] {
        assert_eq!(response.status(), StatusCode::OK);
    }

    let updated_count: i64 = sqlx::query_scalar(
        "SELECT COUNT(*) FROM domain_nodes          WHERE id = ANY($1) AND title LIKE 'Parallel %'",
    )
    .bind(ids.as_slice())
    .fetch_one(&pool)
    .await?;
    assert_eq!(updated_count, ids.len() as i64);

    clean(&pool).await;
    Ok(())
}

/// A2c. A semantic no-op cannot claim an unrelated external V+1 as its own
/// completed projection generation. Otherwise the process marker could become
/// current while another node remains stale in the in-memory snapshot.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn postgres_node_noop_patch_does_not_fast_forward_external_generation() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;
    seed_node(&pool, NODE_A, Some("unchanged"), None).await;
    seed_node(&pool, NODE_B, Some("before external"), None).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (_app, cookie, state) =
        postgres_write_app(pool.clone(), "10000000-0000-0000-0000-000000000097").await?;
    let app = postgres_projected_router(&state);
    state.refresh_domain_projection_if_stale().await?;
    let local_before = state.domain_projection_version.load(Ordering::Acquire);
    assert_eq!(local_before, domain_projection_version(&pool).await?);

    // Let middleware establish a current snapshot, then hold the target row so
    // the no-op request owns nodes_persist while still waiting before its UPDATE.
    let mut blocker = pool.begin().await?;
    sqlx::query("SELECT id FROM domain_nodes WHERE id = $1 FOR UPDATE")
        .bind(NODE_A)
        .fetch_one(&mut *blocker)
        .await?;
    let request = patch_node_req(&cookie, NODE_A, r#"{"info":"unchanged"}"#, SEEDED_NODE_ETAG);
    let patch_app = app.clone();
    let patch_task = tokio::spawn(async move { patch_app.oneshot(request).await });
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert!(
        !patch_task.is_finished(),
        "row lock must keep the no-op request inside its PostgreSQL write path"
    );
    assert!(
        state.nodes_persist.try_lock().is_err(),
        "blocked PATCH must already own nodes_persist"
    );

    sqlx::query(
        "UPDATE domain_nodes SET payload = jsonb_set(payload, '{info}', to_jsonb($2::text), TRUE), updated_at = NOW() WHERE id = $1",
    )
    .bind(NODE_B)
    .bind("external change")
    .execute(&pool)
    .await?;
    assert_eq!(domain_projection_version(&pool).await?, local_before + 1);

    blocker.commit().await?;
    let response = tokio::time::timeout(Duration::from_secs(3), patch_task)
        .await
        .context("no-op PATCH did not finish after releasing its row lock")???;
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(
        state.domain_projection_version.load(Ordering::Acquire),
        local_before,
        "no-op PATCH must not consume a generation it did not create"
    );

    state.refresh_domain_projection_if_stale().await?;
    let db_after = domain_projection_version(&pool).await?;
    assert_eq!(
        state.domain_projection_version.load(Ordering::Acquire),
        db_after,
        "strict refresh must reconcile the external generation"
    );
    assert_eq!(
        state
            .nodes
            .read()
            .await
            .get(NODE_B)
            .and_then(|node| node.info.as_deref()),
        Some("external change"),
        "external node change must be present after strict reconciliation"
    );

    clean(&pool).await;
    Ok(())
}

/// A5b. When a second serialized local PATCH commits immediately after the
/// first handoff, a strict refresh reclassifies V+2 as another exact local
/// handoff instead of starting a redundant full snapshot reload.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn strict_projection_refresh_reclassifies_following_local_patch_handoff() -> Result<()> {
    let pool = PgPoolOptions::new()
        .max_connections(2)
        .connect(&direct_database_url())
        .await
        .context("connect bounded direct PostgreSQL pool")?;
    run_migrations(&pool).await;
    clean(&pool).await;
    seed_node(&pool, NODE_B, Some("before handoffs"), None).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (_app, _cookie, state) =
        postgres_write_app(pool.clone(), "10000000-0000-0000-0000-000000000099").await?;
    let local_before = domain_projection_version(&pool).await?;
    state
        .domain_projection_version
        .store(local_before, Ordering::Release);

    // Handoff A is already committed in PostgreSQL but has not yet published
    // its cache/version locally.
    let persist_a = state.nodes_persist.lock().await;
    sqlx::query(
        "UPDATE domain_nodes SET payload = jsonb_set(payload, '{info}', to_jsonb($2::text), TRUE), updated_at = NOW() WHERE id = $1",
    )
    .bind(NODE_B)
    .bind("handoff A")
    .execute(&pool)
    .await?;
    let version_a = domain_projection_version(&pool).await?;
    assert_eq!(version_a, local_before + 1);
    let marker_a = state
        .begin_local_node_patch_projection_handoff(version_a)
        .expect("install handoff A marker");

    // Writer B reserves one connection but performs no database mutation until
    // it has acquired nodes_persist after A. This lets the test later exhaust
    // the other pool slot and force the strict reader to observe B only after
    // B's pre-COMMIT marker is installed and its V+2 commit is visible.
    let b_pool = pool.clone();
    let b_state = state.clone();
    let (reserved_tx, reserved_rx) = oneshot::channel();
    let (committed_tx, committed_rx) = oneshot::channel();
    let (publish_tx, publish_rx) = oneshot::channel();
    let writer_b = tokio::spawn(async move {
        let mut connection = b_pool
            .acquire()
            .await
            .context("reserve writer B connection")?;
        let _ = reserved_tx.send(());
        let _persist_b = b_state.nodes_persist.lock().await;

        sqlx::query("BEGIN").execute(&mut *connection).await?;
        sqlx::query(
            "UPDATE domain_nodes SET payload = jsonb_set(payload, '{info}', to_jsonb($2::text), TRUE), updated_at = NOW() WHERE id = $1",
        )
        .bind(NODE_B)
        .bind("handoff B")
        .execute(&mut *connection)
        .await?;
        let version_b: i64 = sqlx::query_scalar(
            "SELECT version FROM domain_projection_state WHERE singleton = TRUE",
        )
        .fetch_one(&mut *connection)
        .await?;
        let marker_b = b_state
            .begin_local_node_patch_projection_handoff(version_b)
            .context("install handoff B marker")?;
        sqlx::query("COMMIT").execute(&mut *connection).await?;
        drop(connection);
        let _ = committed_tx.send(version_b);

        let _ = publish_rx.await;
        let mut node = b_state
            .nodes
            .read()
            .await
            .get(NODE_B)
            .cloned()
            .context("cached node for handoff B publication")?;
        node.info = Some("handoff B".to_string());
        b_state.nodes.write().await.insert(NODE_B.to_string(), node);
        b_state
            .domain_projection_version
            .store(version_b, Ordering::Release);
        b_state.metrics.set_domain_projection_version(version_b);
        drop(marker_b);
        Ok::<(), anyhow::Error>(())
    });
    tokio::time::timeout(Duration::from_secs(2), reserved_rx)
        .await
        .context("writer B did not reserve its connection")?
        .context("writer B reservation signal dropped")?;

    let strict_state = state.clone();
    let strict_refresh =
        tokio::spawn(async move { strict_state.refresh_domain_projection_if_stale().await });
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert!(
        !strict_refresh.is_finished(),
        "strict refresh must wait for handoff A"
    );

    // Writer B owns one pool slot while waiting on nodes_persist. Holding the
    // second slot means the strict reader cannot perform its post-A version read
    // until B commits and returns its reserved connection to the pool.
    let blocker_connection = tokio::time::timeout(Duration::from_secs(2), pool.acquire())
        .await
        .context("could not reserve strict-reader blocker connection")??;

    let mut node_a = state
        .nodes
        .read()
        .await
        .get(NODE_B)
        .cloned()
        .context("cached node for handoff A publication")?;
    node_a.info = Some("handoff A".to_string());
    state.nodes.write().await.insert(NODE_B.to_string(), node_a);
    state
        .domain_projection_version
        .store(version_a, Ordering::Release);
    state.metrics.set_domain_projection_version(version_a);
    drop(marker_a);
    drop(persist_a);

    let version_b = tokio::time::timeout(Duration::from_secs(3), committed_rx)
        .await
        .context("writer B did not commit")?
        .context("writer B commit signal dropped")?;
    assert_eq!(version_b, version_a + 1);

    // Give the strict task enough time to observe committed V+2. Correct code
    // remains in the exact B handoff; the old one-shot classification enters a
    // full reload here.
    tokio::time::sleep(Duration::from_millis(100)).await;
    let _ = publish_tx.send(());
    tokio::time::timeout(Duration::from_secs(3), writer_b)
        .await
        .context("writer B did not publish")?
        .context("writer B task failed")??;
    tokio::time::timeout(Duration::from_secs(3), strict_refresh)
        .await
        .context("strict refresh did not finish after handoff B")?
        .context("strict refresh task failed")??;
    drop(blocker_connection);

    let metrics = String::from_utf8(state.metrics.render()?)?;
    assert!(
        !metrics.contains(r#"domain_projection_events_total{event="reload_success"}"#),
        "following exact local handoff must be reclassified without a full reload"
    );
    assert_eq!(
        state.domain_projection_version.load(Ordering::Acquire),
        version_b
    );
    assert_eq!(
        state
            .nodes
            .read()
            .await
            .get(NODE_B)
            .and_then(|node| node.info.as_deref()),
        Some("handoff B")
    );

    clean(&pool).await;
    Ok(())
}

// A five-connection runtime must still have a usable connection while a
// cross-process GET holds the canonical visibility row and PATCH waits to
// advance it. Two independent reader/writer admission limits previously let
// the waiting requests consume the whole SQLx pool (PoolTimedOut).
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn fenced_get_and_node_patch_share_five_connection_budget() -> Result<()> {
    const ACTOR_ID: &str = "10000000-0000-0000-0000-0000000000b4";
    let pool = PgPoolOptions::new()
        .max_connections(5)
        .connect(&direct_database_url())
        .await
        .context("connect five-connection SQLx runtime pool")?;
    run_migrations(&pool).await;
    clean(&pool).await;
    seed_node(&pool, NODE_A, None, None).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);
    let (_handler_app, cookie, state) = postgres_write_app(pool.clone(), ACTOR_ID).await?;
    state.refresh_domain_projection_if_stale().await?;

    let entered = Arc::new(Barrier::new(2));
    let (release_tx, release_rx) = watch::channel(false);
    let probe_entered = Arc::clone(&entered);
    // This handler runs INSIDE the real GET PostgreSQL FOR SHARE fence.
    let app = Router::new()
        .merge(api_router())
        .route(
            "/nodes/pool-fence-probe",
            axum::routing::get(move || {
                let probe_entered = Arc::clone(&probe_entered);
                let mut release_rx = release_rx.clone();
                async move {
                    probe_entered.wait().await;
                    if !*release_rx.borrow() {
                        let _ = release_rx.changed().await;
                    }
                    StatusCode::OK
                }
            }),
        )
        .layer(from_fn_with_state(
            state.clone(),
            ensure_current_domain_projection,
        ))
        .route_layer(from_fn_with_state(state.clone(), auth_middleware))
        .layer(axum::middleware::from_fn(require_csrf))
        .with_state(state.clone());

    let first_app = app.clone();
    let held_get = tokio::spawn(async move {
        first_app
            .oneshot(
                Request::get("/nodes/pool-fence-probe")
                    .header("Host", "localhost")
                    .body(body::Body::empty())
                    .expect("GET fence probe"),
            )
            .await
    });
    tokio::time::timeout(Duration::from_secs(5), entered.wait())
        .await
        .context("real PostgreSQL-fenced GET did not reach its handler")?;

    let writer_app = app.clone();
    let writer = tokio::spawn(async move {
        writer_app
            .oneshot(patch_node_req(
                &cookie,
                NODE_A,
                r#"{"info":"fenced mixed load"}"#,
                SEEDED_NODE_ETAG,
            ))
            .await
    });

    // Confirm the writer is actually blocked by a PostgreSQL lock rather than
    // merely queued or finished before the test probes the pool.
    let mut observed_lock_wait = false;
    for _ in 0..40 {
        let waiting: i64 = sqlx::query_scalar(
            "SELECT COUNT(*) FROM pg_stat_activity              WHERE datname = current_database()                AND wait_event_type = 'Lock' AND state = 'active'",
        )
        .fetch_one(&pool)
        .await?;
        if waiting > 0 {
            observed_lock_wait = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
    assert!(
        observed_lock_wait,
        "writer never reached a blocked PostgreSQL lock"
    );
    assert!(
        !writer.is_finished(),
        "writer bypassed the held visibility fence"
    );

    // A third protected request must not exhaust all five SQLx connections.
    let second_app = app.clone();
    let later_get = tokio::spawn(async move {
        second_app
            .oneshot(
                Request::get("/nodes")
                    .header("Host", "localhost")
                    .body(body::Body::empty())
                    .expect("parallel node list"),
            )
            .await
    });
    tokio::time::sleep(Duration::from_millis(75)).await;
    let headroom: i64 = tokio::time::timeout(
        Duration::from_secs(2),
        sqlx::query_scalar("SELECT 1::bigint").fetch_one(&pool),
    )
    .await
    .context("mixed GET/PATCH blocked all five SQLx connections")??;
    assert_eq!(headroom, 1, "at least one connection must remain usable");

    release_tx.send(true).context("release held GET")?;
    let first = tokio::time::timeout(Duration::from_secs(7), held_get)
        .await
        .context("held GET failed to finish after release")??;
    assert_eq!(first?.status(), StatusCode::OK);
    let patched = tokio::time::timeout(Duration::from_secs(7), writer)
        .await
        .context("waiting writer did not finish after GET released fence")??;
    assert_eq!(patched?.status(), StatusCode::OK);
    let second = tokio::time::timeout(Duration::from_secs(7), later_get)
        .await
        .context("later GET starved after writer finished")??;
    assert_eq!(second?.status(), StatusCode::OK);

    let persisted: Option<String> =
        sqlx::query_scalar("SELECT payload ->> 'info' FROM domain_nodes WHERE id = $1")
            .bind(NODE_A)
            .fetch_one(&pool)
            .await?;
    assert_eq!(persisted.as_deref(), Some("fenced mixed load"));
    clean(&pool).await;
    Ok(())
}

// An ordinary node CREATE uses one PostgreSQL transaction, which holds a
// connection while its INSERT trigger waits for the GET's generation FOR SHARE.
// Four unbudgeted CREATEs once exhausted the remaining four connections,
// preventing the GET handler's second query from ever completing.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn fenced_get_survives_four_concurrent_node_creations() -> Result<()> {
    const ACTOR_ID: &str = "10000000-0000-0000-0000-0000000000c4";
    let pool = PgPoolOptions::new()
        .max_connections(5)
        .connect(&direct_database_url())
        .await
        .context("five-connection production-sized runtime pool")?;
    let monitor = PgPoolOptions::new()
        .max_connections(1)
        .connect(&direct_database_url())
        .await
        .context("separate read-only PostgreSQL lock monitor")?;
    run_migrations(&pool).await;
    clean_all_nodes(&pool).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);
    let (_handler_app, cookie, state) = postgres_write_app(pool.clone(), ACTOR_ID).await?;
    state.refresh_domain_projection_if_stale().await?;

    let entered = Arc::new(Barrier::new(2));
    let (release_tx, release_rx) = watch::channel(false);
    let get_pool = pool.clone();
    let entered_handler = Arc::clone(&entered);
    let app = Router::new()
        .merge(api_router())
        .route(
            "/nodes/create-fence-probe",
            axum::routing::get(move || {
                let entered_handler = Arc::clone(&entered_handler);
                let mut release_rx = release_rx.clone();
                let get_pool = get_pool.clone();
                async move {
                    entered_handler.wait().await;
                    if !*release_rx.borrow() {
                        let _ = release_rx.changed().await;
                    }
                    // This actual query must have a connection while
                    // the PostgreSQL generation row is still FOR SHARE.
                    match tokio::time::timeout(
                        Duration::from_secs(2),
                        sqlx::query_scalar::<_, i64>("SELECT 1::bigint").fetch_one(&get_pool),
                    )
                    .await
                    {
                        Ok(Ok(1)) => StatusCode::OK,
                        _ => StatusCode::GATEWAY_TIMEOUT,
                    }
                }
            }),
        )
        .layer(from_fn_with_state(
            state.clone(),
            ensure_current_domain_projection,
        ))
        .route_layer(from_fn_with_state(state.clone(), auth_middleware))
        .layer(axum::middleware::from_fn(require_csrf))
        .with_state(state);

    let first_app = app.clone();
    let fenced_get = tokio::spawn(async move {
        first_app
            .oneshot(
                Request::get("/nodes/create-fence-probe")
                    .header("Host", "localhost")
                    .body(body::Body::empty())
                    .expect("fenced get"),
            )
            .await
    });
    tokio::time::timeout(Duration::from_secs(5), entered.wait())
        .await
        .context("fenced GET handler did not start")?;

    let mut creates = Vec::new();
    for index in 0..4 {
        let app = app.clone();
        let cookie = cookie.clone();
        creates.push(tokio::spawn(async move {
            let payload = serde_json::json!({
                "title": format!("Concurrent Create {index}"),
                "kind": "Werkstatt",
                "address": "Poolkreuzung 2",
                "location": {"lat": 53.5, "lon": 10.0},
                "operation_id": format!("30000000-0000-4000-8000-{index:012}")
            })
            .to_string();
            app.oneshot(post_node_req(&cookie, &payload)).await
        }));
    }
    let mut lock_wait_observed = false;
    for _ in 0..80 {
        let waiting: i64 = sqlx::query_scalar(
            "SELECT COUNT(*)::bigint FROM pg_stat_activity              WHERE datname = current_database()                AND wait_event_type = 'Lock' AND state = 'active'",
        )
        .fetch_one(&monitor)
        .await?;
        if waiting > 0 {
            lock_wait_observed = true;
        }
        // Give all four launched CREATEs the chance to occupy the SQLx pool
        // on the old, unbudgeted path. The fixed path intentionally admits
        // fewer, so do not assert a fixed number of PostgreSQL lock waiters.
        if lock_wait_observed && waiting >= 4 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
    assert!(
        lock_wait_observed,
        "at least one node writer must wait behind GET fence"
    );
    assert!(creates.iter().all(|task| !task.is_finished()));

    release_tx
        .send(true)
        .context("let fenced GET query the still-blocked pool")?;
    let first = tokio::time::timeout(Duration::from_secs(6), fenced_get)
        .await
        .context("fenced GET failed to complete under concurrent CREATEs")??;
    assert_eq!(first?.status(), StatusCode::OK);
    for task in creates {
        let response = tokio::time::timeout(Duration::from_secs(10), task)
            .await
            .context("node creation did not complete after fence release")??;
        assert_eq!(response?.status(), StatusCode::CREATED);
    }
    let count: i64 = sqlx::query_scalar(
        "SELECT COUNT(*)::bigint FROM domain_nodes          WHERE payload ->> 'created_by_account_id' = $1",
    )
    .bind(ACTOR_ID)
    .fetch_one(&pool)
    .await?;
    assert_eq!(count, 4);
    clean_all_nodes(&pool).await;
    Ok(())
}

// A guest exit updates nodes/edges/accounts under a single PostgreSQL
// transaction. Four simultaneous exits can each occupy a pooled connection
// waiting for the held GET generation row, starving that GET's next DB query.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn fenced_get_survives_four_concurrent_guest_exits() -> Result<()> {
    const ACTOR_ID: &str = "10000000-0000-0000-0000-0000000000d4";
    let pool = PgPoolOptions::new()
        .max_connections(5)
        .connect(&direct_database_url())
        .await?;
    let monitor = PgPoolOptions::new()
        .max_connections(1)
        .connect(&direct_database_url())
        .await?;
    run_migrations(&pool).await;
    clean_all_nodes(&pool).await;
    for index in 0..4 {
        let guest_id = format!("writepath-guest-budget-{index}");
        sqlx::query(
            "INSERT INTO domain_accounts              (id,kind,title,map_state,radius_m,disabled,role,public_payload,private_payload)              VALUES ($1,'garnrolle','Guest budget','not_on_map',0,FALSE,'gast','{}','{}')",
        )
        .bind(&guest_id)
        .execute(&pool)
        .await?;
    }

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);
    let (_handler_app, _cookie, state) = postgres_write_app(pool.clone(), ACTOR_ID).await?;
    state.refresh_domain_projection_if_stale().await?;

    let entered = Arc::new(Barrier::new(2));
    let (release_tx, release_rx) = watch::channel(false);
    let entered_handler = Arc::clone(&entered);
    let get_pool = pool.clone();
    let app = Router::new()
        .route(
            "/nodes/guest-exit-budget-probe",
            axum::routing::get(move || {
                let entered_handler = Arc::clone(&entered_handler);
                let mut release_rx = release_rx.clone();
                let get_pool = get_pool.clone();
                async move {
                    entered_handler.wait().await;
                    if !*release_rx.borrow() {
                        let _ = release_rx.changed().await;
                    }
                    match tokio::time::timeout(
                        Duration::from_secs(2),
                        sqlx::query_scalar::<_, i64>("SELECT 1::bigint").fetch_one(&get_pool),
                    )
                    .await
                    {
                        Ok(Ok(1)) => StatusCode::OK,
                        _ => StatusCode::GATEWAY_TIMEOUT,
                    }
                }
            }),
        )
        .layer(from_fn_with_state(
            state.clone(),
            ensure_current_domain_projection,
        ))
        .with_state(state);

    let first_app = app.clone();
    let fenced_get = tokio::spawn(async move {
        first_app
            .oneshot(
                Request::get("/nodes/guest-exit-budget-probe")
                    .body(body::Body::empty())
                    .expect("GET budget probe"),
            )
            .await
    });
    tokio::time::timeout(Duration::from_secs(5), entered.wait())
        .await
        .context("privacy-fenced guest-exit GET did not start")?;

    let mut exits = Vec::new();
    for index in 0..4 {
        let pool = pool.clone();
        exits.push(tokio::spawn(async move {
            let guest_id = format!("writepath-guest-budget-{index}");
            delete_guest_account(&pool, &guest_id).await
        }));
    }

    let mut saw_lock_wait = false;
    for _ in 0..80 {
        let waiting: i64 = sqlx::query_scalar(
            "SELECT COUNT(*)::bigint FROM pg_stat_activity              WHERE datname = current_database()                AND wait_event_type = 'Lock' AND state = 'active'",
        )
        .fetch_one(&monitor)
        .await?;
        if waiting > 0 {
            saw_lock_wait = true;
        }
        if waiting >= 4 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
    assert!(
        saw_lock_wait,
        "guest exit must actually wait on a canonical PG lock"
    );
    assert!(exits.iter().all(|task| !task.is_finished()));
    release_tx
        .send(true)
        .context("allow fenced GET's second SQLx connection")?;

    let first = tokio::time::timeout(Duration::from_secs(6), fenced_get)
        .await
        .context("fenced GET stuck under four guest exits")??;
    assert_eq!(first?.status(), StatusCode::OK);
    for task in exits {
        tokio::time::timeout(Duration::from_secs(8), task)
            .await
            .context("guest exit starved after releasing GET fence")???
    }
    let remaining: i64 = sqlx::query_scalar(
        "SELECT COUNT(*)::bigint FROM domain_accounts WHERE id LIKE 'writepath-guest-budget-%'",
    )
    .fetch_one(&pool)
    .await?;
    assert_eq!(remaining, 0, "all four guests must be deleted");
    clean_all_nodes(&pool).await;
    Ok(())
}

// A single guest exit holds the guest-account row FOR UPDATE and eventually
// waits for the GET's canonical generation fence. Three concurrent real HTTP
// POST /direct-conversations requests FOR SHARE that same guest row. Without
// shared SQLx admission, they occupy the last three pool connections and the
// GET cannot obtain the second connection needed to complete its JSON handler.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn fenced_get_survives_guest_exit_and_three_direct_conversation_writers() -> Result<()> {
    const ACTOR_ID: &str = "10000000-0000-0000-0000-0000000000e4";
    const GUEST_ID: &str = "writepath-direct-guest-budget";
    let pool = PgPoolOptions::new()
        .max_connections(5)
        .connect(&direct_database_url())
        .await?;
    let monitor = PgPoolOptions::new()
        .max_connections(1)
        .connect(&direct_database_url())
        .await?;
    run_migrations(&pool).await;
    clean_all_nodes(&pool).await;

    sqlx::query(
        "INSERT INTO domain_accounts          (id,kind,title,map_state,radius_m,disabled,role,public_payload,private_payload)          VALUES ($1,'garnrolle','Guest inbox budget','not_on_map',0,FALSE,'gast','{}','{}')",
    )
    .bind(GUEST_ID)
    .execute(&pool)
    .await?;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);
    let (_app, cookie, state) = postgres_write_app(pool.clone(), ACTOR_ID).await?;
    state.refresh_domain_projection_if_stale().await?;

    let entered = Arc::new(Barrier::new(2));
    let (release_tx, release_rx) = watch::channel(false);
    let entered_handler = Arc::clone(&entered);
    let get_pool = pool.clone();
    let app = Router::new()
        .merge(api_router())
        .route(
            "/nodes/direct-conversation-budget-probe",
            axum::routing::get(move || {
                let entered_handler = Arc::clone(&entered_handler);
                let mut release_rx = release_rx.clone();
                let get_pool = get_pool.clone();
                async move {
                    entered_handler.wait().await;
                    if !*release_rx.borrow() {
                        let _ = release_rx.changed().await;
                    }
                    match tokio::time::timeout(
                        Duration::from_secs(2),
                        sqlx::query_scalar::<_, i64>("SELECT 1::bigint").fetch_one(&get_pool),
                    )
                    .await
                    {
                        Ok(Ok(1)) => StatusCode::OK,
                        _ => StatusCode::GATEWAY_TIMEOUT,
                    }
                }
            }),
        )
        .layer(from_fn_with_state(
            state.clone(),
            ensure_current_domain_projection,
        ))
        .route_layer(from_fn_with_state(state.clone(), auth_middleware))
        .layer(axum::middleware::from_fn(require_csrf))
        .with_state(state);

    let first_app = app.clone();
    let fenced_get = tokio::spawn(async move {
        first_app
            .oneshot(
                Request::get("/nodes/direct-conversation-budget-probe")
                    .header("Host", "localhost")
                    .body(body::Body::empty())
                    .expect("privacy-fenced GET"),
            )
            .await
    });
    tokio::time::timeout(Duration::from_secs(5), entered.wait())
        .await
        .context("fenced GET did not enter handler")?;

    let exit_pool = pool.clone();
    let exit = tokio::spawn(async move { delete_guest_account(&exit_pool, GUEST_ID).await });

    let mut row_is_locked = false;
    for _ in 0..80 {
        // A short NOWAIT read identifies the real FOR UPDATE lock held by the
        // active guest exit. Do not infer the ordering from arbitrary sleeps.
        match sqlx::query_scalar::<_, String>(
            "SELECT id FROM domain_accounts WHERE id=$1 FOR SHARE NOWAIT",
        )
        .bind(GUEST_ID)
        .fetch_one(&monitor)
        .await
        {
            Ok(_) => {}
            Err(sqlx::Error::Database(error)) if error.code().as_deref() == Some("55P03") => {
                row_is_locked = true;
                break;
            }
            Err(error) => return Err(error.into()),
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
    assert!(
        row_is_locked,
        "guest exit must hold the guest account FOR UPDATE"
    );

    let mut conversations = Vec::new();
    for _ in 0..3 {
        let app = app.clone();
        let cookie = cookie.clone();
        conversations.push(tokio::spawn(async move {
            app.oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/direct-conversations")
                    .header("Host", "localhost")
                    .header("Origin", "http://localhost")
                    .header("Content-Type", "application/json")
                    .header("Cookie", cookie)
                    .body(body::Body::from(
                        serde_json::json!({"recipient_account_id": GUEST_ID}).to_string(),
                    ))
                    .expect("direct conversation request"),
            )
            .await
        }));
    }

    // The guest exit must reach the PostgreSQL visibility lock before
    // releasing GET. On the broken path all three POSTs can then occupy
    // connections while waiting on the guest. The fixed path deliberately
    // admits fewer: requiring multiple DB waiters would reject the fix.
    let mut lock_wait_observed = false;
    for _ in 0..80 {
        let waiting: i64 = sqlx::query_scalar(
            "SELECT COUNT(*)::bigint FROM pg_stat_activity              WHERE datname = current_database() AND wait_event_type = 'Lock' AND state = 'active'"
        )
        .fetch_one(&monitor)
        .await?;
        if waiting >= 1 {
            lock_wait_observed = true;
        }
        if waiting >= 4 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
    assert!(
        lock_wait_observed,
        "guest exit must be waiting on the canonical visibility fence"
    );
    assert!(
        !exit.is_finished(),
        "guest exit bypassed held generation fence"
    );

    release_tx
        .send(true)
        .context("allow GET to acquire second SQLx connection")?;
    let get_response = tokio::time::timeout(Duration::from_secs(7), fenced_get)
        .await
        .context("GET starved by direct-conversation/guest-exit DB locks")??;
    assert_eq!(
        get_response?.status(),
        StatusCode::OK,
        "GET must not starve"
    );

    tokio::time::timeout(Duration::from_secs(10), exit)
        .await
        .context("guest exit failed to finish")???;
    for writer in conversations {
        let response = tokio::time::timeout(Duration::from_secs(9), writer)
            .await
            .context("direct-conversation POST did not finish")??;
        let status = response?.status();
        assert!(
            !status.is_server_error(),
            "conversation writer must finish without 5xx: {status}"
        );
    }
    let remaining: i64 =
        sqlx::query_scalar("SELECT COUNT(*)::bigint FROM domain_accounts WHERE id=$1")
            .bind(GUEST_ID)
            .fetch_one(&pool)
            .await?;
    assert_eq!(remaining, 0);
    clean_all_nodes(&pool).await;
    Ok(())
}

// A protected GET, guest exit, two proposal-message writes and an expired
// session touch share only five SQLx connections. Each of the latter operations
// can wait on the exit's domain-account/session locks. Every one must be
// admitted through the SAME pool budget instead of occupying the GET's second
// handler connection while its generation FOR SHARE lock is still held.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn fenced_get_survives_guest_exit_governance_and_session_touch() -> Result<()> {
    use weltgewebe_api::{
        auth::{session::SessionOps, session_db::DbSessionStore},
        governance::{add_message, create_weber_proposal, MessageError},
    };

    const ACTOR_ID: &str = "10000000-0000-0000-0000-0000000000f4";
    const GUEST_ID: &str = "writepath-gov-session-budget";
    let pool = PgPoolOptions::new()
        .max_connections(5)
        .connect(&direct_database_url())
        .await?;
    let monitor = PgPoolOptions::new()
        .max_connections(1)
        .connect(&direct_database_url())
        .await?;
    run_migrations(&pool).await;
    clean_all_nodes(&pool).await;

    sqlx::query(
        "INSERT INTO domain_accounts          (id,kind,title,map_state,radius_m,disabled,role,public_payload,private_payload)          VALUES ($1,'garnrolle','Guest governance budget','not_on_map',0,FALSE,'gast','{}','{}')",
    )
    .bind(GUEST_ID)
    .execute(&pool)
    .await?;
    let proposal = create_weber_proposal(
        &pool,
        GUEST_ID,
        "Guest governance budget",
        None,
        chrono::Utc::now(),
    )
    .await?;
    let session_id = uuid::Uuid::new_v4().to_string();
    sqlx::query(
        "INSERT INTO sessions (id, account_id, device_id, created_at, last_active, expires_at)          VALUES ($1, $2, $3, NOW(), NOW() - INTERVAL '10 minutes', NOW() + INTERVAL '1 day')",
    )
    .bind(&session_id)
    .bind(GUEST_ID)
    .bind(uuid::Uuid::new_v4().to_string())
    .execute(&pool)
    .await?;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);
    let (_app, _cookie, state) = postgres_write_app(pool.clone(), ACTOR_ID).await?;
    state.refresh_domain_projection_if_stale().await?;

    let entered = Arc::new(Barrier::new(2));
    let (release_tx, release_rx) = watch::channel(false);
    let entered_handler = Arc::clone(&entered);
    let get_pool = pool.clone();
    let app = Router::new()
        .route(
            "/nodes/governance-session-budget-probe",
            axum::routing::get(move || {
                let entered_handler = Arc::clone(&entered_handler);
                let mut release_rx = release_rx.clone();
                let get_pool = get_pool.clone();
                async move {
                    entered_handler.wait().await;
                    if !*release_rx.borrow() {
                        let _ = release_rx.changed().await;
                    }
                    match tokio::time::timeout(
                        Duration::from_secs(2),
                        sqlx::query_scalar::<_, i64>("SELECT 1::bigint").fetch_one(&get_pool),
                    )
                    .await
                    {
                        Ok(Ok(1)) => StatusCode::OK,
                        _ => StatusCode::GATEWAY_TIMEOUT,
                    }
                }
            }),
        )
        .layer(from_fn_with_state(
            state.clone(),
            ensure_current_domain_projection,
        ))
        .with_state(state);
    let fenced_get = tokio::spawn(async move {
        app.oneshot(
            Request::get("/nodes/governance-session-budget-probe")
                .header("Host", "localhost")
                .body(body::Body::empty())
                .expect("fenced GET"),
        )
        .await
    });
    tokio::time::timeout(Duration::from_secs(5), entered.wait())
        .await
        .context("fenced GET must hold canonical generation")?;

    let exit_pool = pool.clone();
    let exit = tokio::spawn(async move { delete_guest_account(&exit_pool, GUEST_ID).await });
    let mut exit_row_locked = false;
    for _ in 0..80 {
        match sqlx::query_scalar::<_, String>(
            "SELECT id FROM domain_accounts WHERE id = $1 FOR SHARE NOWAIT",
        )
        .bind(GUEST_ID)
        .fetch_one(&monitor)
        .await
        {
            Ok(_) => {}
            Err(sqlx::Error::Database(error)) if error.code().as_deref() == Some("55P03") => {
                exit_row_locked = true;
                break;
            }
            Err(error) => return Err(error.into()),
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
    assert!(exit_row_locked, "guest exit must hold account FOR UPDATE");

    // Wait until the exit is blocked by the GET generation row. Guest exit
    // deletes the sessions first, so their TOUCH would otherwise retain
    // pooled connections while waiting on the uncommitted session deletes.
    let mut exit_generation_wait = false;
    for _ in 0..80 {
        let waiting: i64 = sqlx::query_scalar(
            "SELECT COUNT(*)::bigint FROM pg_stat_activity              WHERE datname = current_database() AND wait_event_type = 'Lock' AND state = 'active'",
        )
        .fetch_one(&monitor)
        .await?;
        if waiting >= 1 {
            exit_generation_wait = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
    assert!(
        exit_generation_wait,
        "guest exit must wait behind held GET generation"
    );

    let mut governance_writers = Vec::new();
    for index in 0..2 {
        let pool = pool.clone();
        let proposal_id = proposal.id.clone();
        governance_writers.push(tokio::spawn(async move {
            add_message(
                &pool,
                &proposal_id,
                GUEST_ID,
                "Guest governance budget",
                &format!("blocked proposal-message {index}"),
                chrono::Utc::now(),
            )
            .await
        }));
    }
    let session_store = DbSessionStore::new(pool.clone());
    let touch = tokio::spawn(async move { session_store.touch(&session_id).await });

    // The previous unbudgeted path consumes three pool connections here:
    // two account-lock waiters and one session-row UPDATE waiter. The fixed
    // path queues them before SQLx admission. Exercise both for long enough.
    for _ in 0..60 {
        let waiting: i64 = sqlx::query_scalar(
            "SELECT COUNT(*)::bigint FROM pg_stat_activity              WHERE datname = current_database() AND wait_event_type = 'Lock' AND state = 'active'",
        )
        .fetch_one(&monitor)
        .await?;
        if waiting >= 4 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
    assert!(
        !exit.is_finished(),
        "guest exit bypassed canonical visibility fence"
    );
    release_tx
        .send(true)
        .context("allow protected GET's second SQLx read")?;
    let response = tokio::time::timeout(Duration::from_secs(7), fenced_get)
        .await
        .context("GET stalled behind pool-blocked governance/session writers")??;
    assert_eq!(response?.status(), StatusCode::OK);

    tokio::time::timeout(Duration::from_secs(10), exit)
        .await
        .context("guest exit did not complete")???;
    for task in governance_writers {
        let outcome = tokio::time::timeout(Duration::from_secs(10), task)
            .await
            .context("governance writer did not complete")??;
        assert!(
            matches!(
                outcome,
                Ok(_) | Err(MessageError::Database(sqlx::Error::RowNotFound))
            ),
            "governance writer may only complete or observe the deleted guest, never time out"
        );
    }
    tokio::time::timeout(Duration::from_secs(10), touch)
        .await
        .context("SQLx session touch did not complete")??
        .expect("session touch must be a completed no-op after guest exit");
    let remaining: i64 =
        sqlx::query_scalar("SELECT COUNT(*)::bigint FROM domain_accounts WHERE id = $1")
            .bind(GUEST_ID)
            .fetch_one(&pool)
            .await?;
    assert_eq!(remaining, 0);
    clean_all_nodes(&pool).await;
    Ok(())
}

// Regression: a magic-link login must release its account-cache read guard
// before its PostgreSQL session creation queues behind two fenced readers.
// Otherwise a waiting account-cache writer can deadlock the second reader.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct disposable PostgreSQL"]
#[serial]
async fn magic_link_login_releases_cache_lock_before_pg_admission() -> Result<()> {
    use axum::extract::connect_info::MockConnectInfo;
    use weltgewebe_api::auth::session_db::DbSessionStore;
    use weltgewebe_api::routes::auth::NONCE_COOKIE_NAME;

    const ACTOR_ID: &str = "10000000-0000-0000-0000-0000000000f5";
    const EMAIL: &str = "privacy-admission-login@example.test";
    let pool = PgPoolOptions::new()
        .max_connections(5)
        .connect(&direct_database_url())
        .await?;
    run_migrations(&pool).await;
    clean_all_nodes(&pool).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);
    let (_app, _cookie, mut state) = postgres_write_app(pool.clone(), ACTOR_ID).await?;
    state.sessions = SessionBackend::new(DbSessionStore::new(pool.clone()));
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());
    state.refresh_domain_projection_if_stale().await?;
    // Refresh reloads the canonical account projection; install the test-only
    // login address AFTER it so the magic link reaches PostgreSQL session IO.
    {
        let mut accounts = state.accounts.write().await;
        let mut account = accounts
            .get(ACTOR_ID)
            .context("seed login account")?
            .clone();
        account.email = Some(EMAIL.to_string());
        accounts.insert(account);
    }
    assert!(
        state.accounts.read().await.get_by_email(EMAIL).is_some(),
        "fixture must resolve the login email after projection refresh"
    );

    // Both readers enter their handler while holding the shared canonical
    // projection generation and both weighted admission reservations.
    let entered = Arc::new(Barrier::new(3));
    let (release_tx, release_rx) = watch::channel(false);
    let entered_handler = Arc::clone(&entered);
    let read_pool = pool.clone();
    let get_app = Router::new()
        .route(
            "/nodes/login-lock-probe",
            axum::routing::get(move || {
                let entered_handler = Arc::clone(&entered_handler);
                let mut release_rx = release_rx.clone();
                let read_pool = read_pool.clone();
                async move {
                    entered_handler.wait().await;
                    if !*release_rx.borrow() {
                        let _ = release_rx.changed().await;
                    }
                    match sqlx::query_scalar::<_, i64>("SELECT 1::bigint")
                        .fetch_one(&read_pool)
                        .await
                    {
                        Ok(1) => StatusCode::OK,
                        _ => StatusCode::GATEWAY_TIMEOUT,
                    }
                }
            }),
        )
        .layer(from_fn_with_state(
            state.clone(),
            ensure_current_domain_projection,
        ))
        .with_state(state.clone());
    let mut readers = Vec::new();
    for _ in 0..2 {
        let app = get_app.clone();
        readers.push(tokio::spawn(async move {
            app.oneshot(
                Request::get("/nodes/login-lock-probe")
                    .body(body::Body::empty())
                    .expect("protected GET"),
            )
            .await
        }));
    }
    tokio::time::timeout(Duration::from_secs(5), entered.wait())
        .await
        .context("both protected readers must reserve their PG admission")?;

    let token = state.tokens.create(EMAIL.to_string());
    let nonce = "privacy-cache-lock-nonce";
    let cookie = format!(
        "{}={}.{}",
        NONCE_COOKIE_NAME,
        hex::encode(Sha256::digest(token.as_bytes())),
        nonce
    );
    let login_app = Router::new()
        .merge(api_router())
        .layer(MockConnectInfo(
            "127.0.0.1:8080".parse::<std::net::SocketAddr>()?,
        ))
        .with_state(state.clone());
    let login_token = token.clone();
    let login = tokio::spawn(async move {
        login_app
            .oneshot(
                Request::post("/auth/magic-link/consume")
                    .header("Host", "localhost")
                    .header("Content-Type", "application/x-www-form-urlencoded")
                    .header("Cookie", cookie)
                    .body(body::Body::from(format!(
                        "token={login_token}&nonce={nonce}"
                    )))
                    .expect("login request"),
            )
            .await
    });

    let mut consumed = false;
    for _ in 0..100 {
        if state.tokens.peek(&token).is_none() {
            consumed = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    assert!(
        consumed,
        "login must consume token before testing lock order"
    );
    assert!(
        !login.is_finished(),
        "login must wait on occupied PG admission"
    );
    // Allow the login task to reach session admission after consuming its
    // token. The writer must not depend on release of the two PG readers.
    tokio::task::yield_now().await;
    let accounts = Arc::clone(&state.accounts);
    let mut cache_writer = tokio::spawn(async move {
        let _guard = accounts.write().await;
    });
    let writer_before_release =
        tokio::time::timeout(Duration::from_secs(2), &mut cache_writer).await;
    // Always unwind the held readers, even when asserting the old deadlock.
    release_tx.send(true).context("release protected readers")?;
    for reader in readers {
        let response = tokio::time::timeout(Duration::from_secs(5), reader)
            .await
            .context("protected reader completion")??;
        assert_eq!(response?.status(), StatusCode::OK);
    }
    let response = tokio::time::timeout(Duration::from_secs(5), login)
        .await
        .context("login completes once admission is released")??;
    assert_eq!(response?.status(), StatusCode::SEE_OTHER);
    if writer_before_release.is_err() {
        tokio::time::timeout(Duration::from_secs(5), cache_writer)
            .await
            .context("cache writer completes after release")??;
    }
    assert!(
        writer_before_release.is_ok(),
        "login retained the account-cache read lock across PostgreSQL admission"
    );
    sqlx::query("DELETE FROM sessions WHERE account_id = $1")
        .bind(ACTOR_ID)
        .execute(&pool)
        .await?;
    sqlx::query("DELETE FROM domain_accounts WHERE id = $1")
        .bind(ACTOR_ID)
        .execute(&pool)
        .await?;
    clean_all_nodes(&pool).await;
    Ok(())
}

// Regression: three real notification preference PUTs must not fill the
// five-connection SQLx pool while the guest-exit writer holds their FK row
// and waits on the generation fence of a protected GET.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct disposable PostgreSQL"]
#[serial]
async fn notification_preferences_share_guest_exit_privacy_pool_budget() -> Result<()> {
    const ACTOR_ID: &str = "10000000-0000-0000-0000-0000000000f6";
    const GUEST_ID: &str = "writepath-notification-budget-guest";
    let pool = PgPoolOptions::new()
        .max_connections(5)
        .connect(&direct_database_url())
        .await?;
    let monitor = PgPoolOptions::new()
        .max_connections(1)
        .connect(&direct_database_url())
        .await?;
    run_migrations(&pool).await;
    clean_all_nodes(&pool).await;
    sqlx::query(
        "INSERT INTO domain_accounts
         (id,kind,title,map_state,radius_m,disabled,role,public_payload,private_payload)
         VALUES ($1,'garnrolle','Notification guest budget','not_on_map',0,FALSE,'gast','{}','{}')",
    )
    .bind(GUEST_ID)
    .execute(&pool)
    .await?;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);
    let (_app, _cookie, state) = postgres_write_app(pool.clone(), ACTOR_ID).await?;
    {
        let mut guest = admin_operator(GUEST_ID);
        guest.role = Role::Gast;
        state.accounts.write().await.insert(guest);
    }
    let session = state.sessions.create(GUEST_ID.to_string(), None).await?;
    let cookie = format!("gewebe_session={}", session.id);
    state.refresh_domain_projection_if_stale().await?;

    let entered = Arc::new(Barrier::new(2));
    let (release_tx, release_rx) = watch::channel(false);
    let entered_handler = Arc::clone(&entered);
    let read_pool = pool.clone();
    let get_app = Router::new()
        .route(
            "/nodes/notification-budget-probe",
            axum::routing::get(move || {
                let entered_handler = Arc::clone(&entered_handler);
                let mut release_rx = release_rx.clone();
                let read_pool = read_pool.clone();
                async move {
                    entered_handler.wait().await;
                    if !*release_rx.borrow() {
                        let _ = release_rx.changed().await;
                    }
                    match tokio::time::timeout(
                        Duration::from_secs(2),
                        sqlx::query_scalar::<_, i64>("SELECT 1::bigint").fetch_one(&read_pool),
                    )
                    .await
                    {
                        Ok(Ok(1)) => StatusCode::OK,
                        _ => StatusCode::GATEWAY_TIMEOUT,
                    }
                }
            }),
        )
        .layer(from_fn_with_state(
            state.clone(),
            ensure_current_domain_projection,
        ))
        .with_state(state.clone());
    let protected_get = tokio::spawn(async move {
        get_app
            .oneshot(
                Request::get("/nodes/notification-budget-probe")
                    .body(body::Body::empty())
                    .expect("protected GET"),
            )
            .await
    });
    tokio::time::timeout(Duration::from_secs(5), entered.wait())
        .await
        .context("protected GET must hold PG generation fence")?;

    let exit_pool = pool.clone();
    let exit = tokio::spawn(async move { delete_guest_account(&exit_pool, GUEST_ID).await });
    let mut row_locked = false;
    for _ in 0..80 {
        match sqlx::query_scalar::<_, String>(
            "SELECT id FROM domain_accounts WHERE id = $1 FOR SHARE NOWAIT",
        )
        .bind(GUEST_ID)
        .fetch_one(&monitor)
        .await
        {
            Ok(_) => {}
            Err(sqlx::Error::Database(error)) if error.code().as_deref() == Some("55P03") => {
                row_locked = true;
                break;
            }
            Err(error) => return Err(error.into()),
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
    assert!(
        row_locked,
        "guest exit must hold canonical account FOR UPDATE"
    );

    // Observe the actual HTTP handler boundary after canonical authentication:
    // all three real PUTs must reach the notification writer before the held
    // GET is released. This distinguishes queued SQLx admission from requests
    // that never reached the competing operation.
    let arrived = Arc::new(Barrier::new(4));
    let arrived_handler = Arc::clone(&arrived);
    let preferences_app = Router::new()
        .route(
            "/notifications/preferences",
            axum::routing::put(
                move |state: axum::extract::State<ApiState>,
                      auth: axum::Extension<weltgewebe_api::middleware::auth::AuthContext>,
                      request: axum::Json<
                    weltgewebe_api::notifications::UpdateNotificationPreferencesRequest,
                >| {
                    let arrived_handler = Arc::clone(&arrived_handler);
                    async move {
                        arrived_handler.wait().await;
                        weltgewebe_api::notifications::update_notification_preferences(
                            state, auth, request,
                        )
                        .await
                    }
                },
            ),
        )
        .layer(from_fn_with_state(state.clone(), auth_middleware))
        .with_state(state);
    let mut notifications = Vec::new();
    for _ in 0..3 {
        let app = preferences_app.clone();
        let cookie = cookie.clone();
        notifications.push(tokio::spawn(async move {
            app.oneshot(
                Request::put("/notifications/preferences")
                    .header("Host", "localhost")
                    .header("Content-Type", "application/json")
                    .header("Cookie", cookie)
                    .body(body::Body::from(r#"{"direct_messages_push":true}"#))
                    .expect("notification PUT"),
            )
            .await
        }));
    }
    tokio::time::timeout(Duration::from_secs(5), arrived.wait())
        .await
        .context("all three notification PUTs must pass authentication")?;
    // The guest exit must be the only PG lock waiter: with the shared budget,
    // all three PUTs queue BEFORE SQLx. The unfixed code instead checks out
    // three connections, waits on the guest's FK row, and exhausts the pool.
    let mut observed_exit_wait = false;
    for _ in 0..40 {
        let waiting: i64 = sqlx::query_scalar(
            "SELECT COUNT(*)::bigint FROM pg_stat_activity
             WHERE datname = current_database() AND wait_event_type = 'Lock' AND state = 'active'",
        )
        .fetch_one(&monitor)
        .await?;
        assert!(
            waiting <= 1,
            "notification PUTs must not occupy SQLx connections waiting on guest FK locks: {waiting}"
        );
        if waiting == 1 {
            observed_exit_wait = true;
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
    assert!(
        observed_exit_wait,
        "guest exit must actually wait for generation fence"
    );
    assert!(
        notifications.iter().all(|task| !task.is_finished()),
        "notification PUTs must queue while the protected GET and exit hold admission"
    );
    assert!(
        !exit.is_finished(),
        "guest exit must still wait on GET's fence"
    );
    release_tx
        .send(true)
        .context("release GET's second SQLx read")?;
    let response = tokio::time::timeout(Duration::from_secs(7), protected_get)
        .await
        .context("protected GET starved by notification pool waiters")??;
    assert_eq!(response?.status(), StatusCode::OK);
    tokio::time::timeout(Duration::from_secs(10), exit)
        .await
        .context("guest exit must complete")???;
    for request in notifications {
        let response = tokio::time::timeout(Duration::from_secs(10), request)
            .await
            .context("notification PUT must terminate after guest exit")??;
        // FK failure after guest deletion is valid; indefinite DB pool wait is not.
        assert!(
            matches!(
                response?.status(),
                StatusCode::OK | StatusCode::INTERNAL_SERVER_ERROR
            ),
            "notification must reach SQLx instead of being rejected by auth"
        );
    }
    let remaining: i64 =
        sqlx::query_scalar("SELECT COUNT(*)::bigint FROM domain_accounts WHERE id = $1")
            .bind(GUEST_ID)
            .fetch_one(&pool)
            .await?;
    assert_eq!(remaining, 0, "guest deletion must be durable");
    clean_all_nodes(&pool).await;
    Ok(())
}
