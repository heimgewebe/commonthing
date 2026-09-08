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
use tokio::sync::{oneshot, RwLock};
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

    let app = Router::new()
        .merge(api_router())
        .layer(from_fn_with_state(state.clone(), auth_middleware))
        .layer(axum::middleware::from_fn(require_csrf))
        .with_state(state.clone());

    Ok((app, cookie, state))
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

    let (app, cookie, state) =
        postgres_write_app(pool.clone(), "10000000-0000-0000-0000-000000000091").await?;
    let app = app.layer(from_fn_with_state(
        state.clone(),
        ensure_current_domain_projection,
    ));

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

    let (app, cookie, state) =
        postgres_write_app(pool.clone(), "10000000-0000-0000-0000-000000000092").await?;
    let app = app.layer(from_fn_with_state(
        state.clone(),
        ensure_current_domain_projection,
    ));

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

    let mut accounts = AccountStore::new();
    accounts.insert(admin_operator("10000000-0000-0000-0000-000000000004"));
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

    let session = state
        .sessions
        .create("10000000-0000-0000-0000-000000000004".to_string(), None)
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

    let nodes = weltgewebe_api::routes::nodes::load_nodes().await;

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

    let (app, cookie, state) = postgres_write_app(pool.clone(), ACTOR_ID).await?;
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

    let app = app.layer(from_fn_with_state(
        state.clone(),
        ensure_current_domain_projection,
    ));

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
    assert_eq!(exit_response.status(), StatusCode::FORBIDDEN);
    let exit_body = body::to_bytes(exit_response.into_body(), usize::MAX).await?;
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
    sqlx::query("UPDATE domain_nodes SET search_visibility='private' WHERE id=$1")
        .bind(NODE_A)
        .execute(&pool)
        .await?;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);
    let (app, cookie, _state) =
        postgres_write_app(pool.clone(), "10000000-0000-0000-0000-000000000096").await?;

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


/// Final-review regression: a no-op must not adopt an unrelated committed V+1.
/// The real request passes projection/auth/CSRF middleware before being blocked.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn final_review_noop_patch_must_not_consume_external_generation() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;
    seed_node(&pool, NODE_A, Some("unchanged"), None).await;
    seed_node(&pool, NODE_B, Some("before external"), None).await;
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);
    let (app, cookie, state) =
        postgres_write_app(pool.clone(), "10000000-0000-0000-0000-000000000097").await?;
    let app = app.layer(from_fn_with_state(state.clone(), ensure_current_domain_projection));
    state.refresh_domain_projection_if_stale().await?;
    let local_before = state.domain_projection_version.load(Ordering::Acquire);
    assert_eq!(local_before, domain_projection_version(&pool).await?);
    let mut blocker = pool.begin().await?;
    sqlx::query("SELECT id FROM domain_nodes WHERE id = $1 FOR UPDATE")
        .bind(NODE_A).fetch_one(&mut *blocker).await?;
    let request = patch_node_req(&cookie, NODE_A, r#"{"info":"unchanged"}"#, SEEDED_NODE_ETAG);
    let patch_app = app.clone();
    let patch_task = tokio::spawn(async move { patch_app.oneshot(request).await });
    tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            let blocked: i64 = sqlx::query_scalar(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock' AND query LIKE '%domain_nodes%FOR UPDATE%'"
            ).fetch_one(&pool).await?;
            if blocked > 0 && state.nodes_persist.try_lock().is_err() { break; }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        Ok::<(), anyhow::Error>(())
    }).await.context("PATCH did not reach its pre-commit row-lock wait")??;
    sqlx::query("UPDATE domain_nodes SET payload = jsonb_set(payload, '{info}', to_jsonb($2::text), TRUE), updated_at = NOW() WHERE id = $1")
        .bind(NODE_B).bind("external change").execute(&pool).await?;
    assert_eq!(domain_projection_version(&pool).await?, local_before + 1);
    blocker.commit().await?;
    let response = tokio::time::timeout(Duration::from_secs(3), patch_task).await???;
    assert_eq!(response.status(), StatusCode::OK);
    let local_after_patch = state.domain_projection_version.load(Ordering::Acquire);
    // This is the same strict refresh used before authenticating subsequent requests.
    state.refresh_domain_projection_if_stale().await?;
    let db_after = domain_projection_version(&pool).await?;
    let local_after_strict = state.domain_projection_version.load(Ordering::Acquire);
    let cached_info = state.nodes.read().await.get(NODE_B).and_then(|n| n.info.clone());
    let db_info: String = sqlx::query_scalar("SELECT payload->>'info' FROM domain_nodes WHERE id=$1")
        .bind(NODE_B).fetch_one(&pool).await?;
    println!("FINAL_REVIEW_NOOP before={local_before} db_after={db_after} local_after_patch={local_after_patch} local_after_strict={local_after_strict} cached_info={cached_info:?} db_info={db_info:?}");
    clean(&pool).await;
    assert_eq!(cached_info.as_deref(), Some(db_info.as_str()),
        "strict refresh must not accept a falsely current projection after no-op PATCH");
    Ok(())
}

/// Final-review contract: a second anonymous request must not queue behind a
/// snapshot writer that is itself waiting for a stalled in-flight handler.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn final_review_anonymous_reader_during_pending_snapshot_swap() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;
    seed_node(&pool, NODE_B, Some("before external"), None).await;
    let (app, _cookie, state) =
        postgres_write_app(pool.clone(), "10000000-0000-0000-0000-000000000098").await?;
    let app = app.layer(from_fn_with_state(state.clone(), ensure_current_domain_projection));
    state.refresh_domain_projection_if_stale().await?;
    // Exact middleware lock state of a slow handler: it keeps its read gate.
    let slow_handler = state.domain_projection_gate.read().await;
    sqlx::query("UPDATE domain_nodes SET payload = jsonb_set(payload, '{info}', to_jsonb($2::text), TRUE), updated_at = NOW() WHERE id = $1")
        .bind(NODE_B).bind("external change").execute(&pool).await?;
    let owner_state = state.clone();
    let owner = tokio::spawn(async move { owner_state.refresh_domain_projection_for_read().await });
    tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            if state.domain_projection_gate.try_read().is_err() { break; }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    }).await.context("reload owner never queued for the snapshot write gate")?;
    let request = Request::get("/nodes").header("Host", "localhost").body(body::Body::empty())?;
    let mut second = tokio::spawn(async move { app.oneshot(request).await });
    let early = tokio::time::timeout(Duration::from_millis(150), &mut second).await;
    let blocked = early.is_err();
    println!("FINAL_REVIEW_ANON blocked_behind_pending_swap={blocked}");
    drop(slow_handler);
    tokio::time::timeout(Duration::from_secs(3), owner).await???;
    if blocked {
        assert_eq!(tokio::time::timeout(Duration::from_secs(3), second).await???.status(), StatusCode::OK);
    }
    clean(&pool).await;
    assert!(!blocked, "parallel anonymous reader queued behind snapshot writer despite refresh deferral");
    Ok(())
}
