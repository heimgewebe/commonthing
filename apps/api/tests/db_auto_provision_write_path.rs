//! Integration proof: auth auto-provisioning PostgreSQL write path.
//!
//! Proves that, when `domain_read_source=postgres` and
//! `domain_account_write_source=postgres`, a magic-link login request for an
//! allow-listed, unknown email writes one row into `domain_accounts`, updates
//! the in-memory `AccountStore` only after that write succeeds, never appends
//! JSONL, and that the Phase D loader reconstructs the same account after a
//! simulated restart. Also proves the cross-process duplicate-email race is
//! resolved to the winning row instead of fabricating a phantom second
//! account.
//!
//! Run with:
//!   DATABASE_URL=postgres://welt:gewebe@localhost:5432/weltgewebe \
//!     cargo test --locked -p weltgewebe-api --test db_auto_provision_write_path \
//!     -- --include-ignored --test-threads=1

mod support;

use anyhow::Result;
use axum::{
    body,
    extract::connect_info::MockConnectInfo,
    http::{Request, StatusCode},
    Router,
};
use serial_test::serial;
use sqlx::{Executor, PgPool};
use std::{net::SocketAddr, path::PathBuf, sync::Arc};
use tokio::sync::RwLock;
use tower::ServiceExt;
use weltgewebe_api::{
    auth::{
        accounts::AccountStore, rate_limit::AuthRateLimiter, role::Role, session::SessionBackend,
    },
    config::{AppConfig, AutoProvisionRole, DomainAccountWriteSource, DomainReadSource},
    domain_db::load_accounts_from_postgres,
    mailer::Mailer,
    routes::api_router,
    state::ApiState,
    telemetry::{BuildInfo, Metrics},
};

mod helpers;
use helpers::set_gewebe_in_dir;

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

async fn clean(pool: &PgPool) {
    pool.execute("DELETE FROM domain_accounts WHERE email LIKE '%auto-provision-proof%'")
        .await
        .expect("clean domain_accounts fixtures");
}

fn test_metrics() -> Metrics {
    Metrics::try_new(BuildInfo {
        version: "test",
        commit: "test",
        build_timestamp: "test",
    })
    .expect("metrics")
}

fn provisioning_state_postgres(pool: PgPool, allow_emails: Vec<String>) -> ApiState {
    let config = AppConfig {
        max_guest_owned_nodes: 1_000,
        domain_read_source: DomainReadSource::Postgres,
        domain_account_write_source: DomainAccountWriteSource::Postgres,
        domain_node_write_source: weltgewebe_api::config::DomainNodeWriteSource::Jsonl,
        domain_edge_write_source: weltgewebe_api::config::DomainEdgeWriteSource::Jsonl,
        passkey_credential_source: weltgewebe_api::config::PasskeyCredentialSource::InMemory,
        auth_public_login: true,
        auth_cookie_secure: weltgewebe_api::config::auth_cookie_secure_env_override()
            .unwrap_or(true),
        app_base_url: Some("http://localhost".to_string()),
        auth_trusted_proxies: None,
        auth_allow_emails: Some(allow_emails),
        auth_allow_email_domains: None,
        auth_auto_provision: true,
        auth_auto_provision_role: AutoProvisionRole::Gast,
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
        auth_log_magic_token: true,
        webauthn_rp_id: None,
        webauthn_rp_origin: None,
        webauthn_rp_name: None,
    };

    let rate_limiter = Arc::new(AuthRateLimiter::new(&config));

    ApiState {
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
        accounts: Arc::new(RwLock::new(AccountStore::new())),
        nodes: Arc::new(RwLock::new(weltgewebe_api::state::OrderedCache::new())),
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
    }
}

fn app(state: ApiState) -> Router {
    Router::new()
        .merge(api_router())
        .layer(MockConnectInfo(SocketAddr::from(([127, 0, 0, 1], 8080))))
        .with_state(state)
}

fn magic_link_request(email: &str) -> Request<body::Body> {
    Request::post("/auth/magic-link/request")
        .header("Content-Type", "application/json")
        .body(body::Body::from(format!(r#"{{"email":"{email}"}}"#)))
        .unwrap()
}

#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn auto_provision_persists_to_postgres_before_cache_and_reloads() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let email = "auto-provision-proof-pg@example.invalid";
    let state = provisioning_state_postgres(pool.clone(), vec![email.to_string()]);
    let app = app(state.clone());

    let res = app.oneshot(magic_link_request(email)).await?;
    assert_eq!(res.status(), StatusCode::OK);

    let (db_email, db_role): (Option<String>, String) =
        sqlx::query_as("SELECT email, role FROM domain_accounts WHERE email = $1")
            .bind(email)
            .fetch_one(&pool)
            .await
            .expect("account row must exist after auto-provisioning");
    assert_eq!(db_email.as_deref(), Some(email));
    assert_eq!(db_role, "gast");

    // PostgreSQL write mode must never append JSONL.
    assert!(
        !in_dir.join("demo.accounts.jsonl").exists(),
        "PostgreSQL write mode must not append the JSONL accounts file"
    );

    // Cache updated (read-your-writes).
    let account_id = {
        let accounts = state.accounts.read().await;
        let internal = accounts
            .values()
            .find(|a| a.email.as_deref() == Some(email))
            .expect("provisioned account present in cache");
        assert_eq!(internal.role, Role::Gast);
        internal.public.id.clone()
    };

    // Simulated restart: reload from PostgreSQL alone reconstructs the account.
    let reloaded = load_accounts_from_postgres(&pool)
        .await
        .expect("reload accounts from postgres");
    let account = reloaded
        .get(&account_id)
        .expect("account must reload from PostgreSQL after a restart");
    assert_eq!(account.email.as_deref(), Some(email));
    assert_eq!(account.role, Role::Gast);

    clean(&pool).await;
    Ok(())
}

/// Race proof: another writer (a different process, or a prior request) has
/// already persisted this exact email directly in PostgreSQL, but the local
/// in-memory cache does not know about it yet. The DB unique-email
/// constraint must reject the second insert, and the provisioning path must
/// resolve to the existing row instead of leaving no account or a phantom
/// duplicate.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn auto_provision_resolves_cross_process_duplicate_email_race() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let email = "auto-provision-proof-race@example.invalid";
    let existing_id = "aaaaaaaa-aaaa-4aaa-8aaa-000000000099";

    // Seed a winning row directly (simulating a concurrent writer), deliberately
    // NOT present in the in-memory cache.
    sqlx::query(
        "INSERT INTO domain_accounts \
            (id, kind, title, map_state, radius_m, role, email, public_payload, private_payload) \
         VALUES ($1, 'garnrolle', 'Garnrolle', 'not_on_map', 0, 'gast', $2, '{}'::jsonb, '{}'::jsonb)",
    )
    .bind(existing_id)
    .bind(email)
    .execute(&pool)
    .await
    .expect("seed racing domain_accounts row");

    let state = provisioning_state_postgres(pool.clone(), vec![email.to_string()]);
    let app = app(state.clone());

    let res = app.oneshot(magic_link_request(email)).await?;
    assert_eq!(res.status(), StatusCode::OK);

    // Exactly one row for this email — no phantom duplicate was inserted.
    let (count,): (i64,) = sqlx::query_as("SELECT count(*) FROM domain_accounts WHERE email = $1")
        .bind(email)
        .fetch_one(&pool)
        .await
        .expect("count rows for email");
    assert_eq!(
        count, 1,
        "duplicate-email race must resolve to the single winning row"
    );

    // The cache must reflect the row that actually won the race, not a
    // fabricated new id.
    let accounts = state.accounts.read().await;
    let cached = accounts
        .values()
        .find(|a| a.email.as_deref() == Some(email))
        .expect("race-resolved account present in cache");
    assert_eq!(
        cached.public.id, existing_id,
        "cache must resolve to the account that actually persisted, not a phantom"
    );

    clean(&pool).await;
    Ok(())
}

/// A cross-process race may resolve to an account that was disabled before
/// this instance observed it. That row must never be cached as login-ready and
/// must not receive a magic-link token.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
#[serial]
async fn auto_provision_refuses_disabled_cross_process_winner() -> Result<()> {
    let pool = connect_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let email = "auto-provision-proof-disabled@example.invalid";
    sqlx::query(
        "INSERT INTO domain_accounts \
            (id, kind, title, map_state, radius_m, disabled, role, email, public_payload, private_payload) \
         VALUES ('aaaaaaaa-aaaa-4aaa-8aaa-000000000100', 'garnrolle', 'Garnrolle', \
                 'not_on_map', 0, true, 'gast', $1, '{}'::jsonb, '{}'::jsonb)",
    )
    .bind(email)
    .execute(&pool)
    .await?;

    let mut state = provisioning_state_postgres(pool.clone(), vec![email.to_string()]);
    // Use the mailer's in-memory sink as the externally observable delivery
    // boundary. The disabled race winner must never reach it.
    let _smtp_auth = weltgewebe_api::test_helpers::EnvGuard::set("SMTP_AUTH", "off");
    state.config.smtp_host = Some("localhost".to_string());
    state.config.smtp_port = Some(2525);
    state.config.smtp_from = Some("weltgewebe@example.invalid".to_string());
    state.config.auth_log_magic_token = false;
    let mail_sink = Arc::new(std::sync::Mutex::new(Vec::new()));
    state.mailer = Some(Arc::new(
        Mailer::new(&state.config)?.with_test_sink(mail_sink.clone()),
    ));

    let app = app(state.clone());
    let res = app.oneshot(magic_link_request(email)).await?;
    assert_eq!(res.status(), StatusCode::OK);

    assert!(
        state.accounts.read().await.get_by_email(email).is_none(),
        "disabled race winner must not become a login-ready cached account"
    );
    assert!(
        mail_sink.lock().expect("mail sink lock").is_empty(),
        "disabled race winner must not receive a magic-link email"
    );

    clean(&pool).await;
    Ok(())
}
