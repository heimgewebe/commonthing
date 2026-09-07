//! Integration proof: auth auto-provisioning is DB-before-cache, never
//! cache-only.
//!
//! Covers the JSONL write source (fast, always runs). The PostgreSQL write
//! source is covered separately in `db_auto_provision_write_path.rs` (ignored
//! by default, requires DATABASE_URL).
//!
//! Proves:
//! - A successful auto-provision durably appends the JSONL accounts file
//!   *before* the in-memory cache is updated, and a fresh `load_all_accounts`
//!   (simulating a restart) reconstructs the same account.
//! - The configured `AUTH_AUTO_PROVISION_ROLE` is applied to the persisted
//!   record, not just the in-memory copy.
//! - A concurrent double-request for the same email persists exactly one
//!   account (race handled), not two.
//! - A persistence failure (unwritable target directory) never leaves a
//!   cache-only phantom account and never proceeds to send a magic link.

use anyhow::Result;
use axum::{
    body,
    extract::connect_info::MockConnectInfo,
    http::{Request, StatusCode},
    Router,
};
use serial_test::serial;
use std::{net::SocketAddr, sync::Arc};
use tokio::sync::RwLock;
use tower::ServiceExt;
use weltgewebe_api::{
    auth::{
        accounts::AccountStore, rate_limit::AuthRateLimiter, role::Role, session::SessionBackend,
    },
    config::{AppConfig, AutoProvisionRole},
    routes::{accounts::load_all_accounts, api_router},
    state::ApiState,
    telemetry::{BuildInfo, Metrics},
};

mod helpers;
use helpers::set_gewebe_in_dir;

fn test_metrics() -> Metrics {
    Metrics::try_new(BuildInfo {
        version: "test",
        commit: "test",
        build_timestamp: "test",
    })
    .expect("metrics")
}

/// Build a minimal state with public login + auto-provisioning enabled via an
/// explicit email allowlist (never open registration, so `weber` role stays
/// valid per `AppConfig::validate`).
fn provisioning_state(role: AutoProvisionRole, allow_emails: Vec<String>) -> Result<ApiState> {
    let config = AppConfig {
        max_guest_owned_nodes: 1_000,
        domain_read_source: weltgewebe_api::config::DomainReadSource::Jsonl,
        domain_account_write_source: weltgewebe_api::config::DomainAccountWriteSource::Jsonl,
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
        auth_auto_provision_role: role,
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
        // Dev delivery fallback so `auth_public_login` validation is satisfied
        // without a real SMTP mailer.
        auth_log_magic_token: true,
        webauthn_rp_id: None,
        webauthn_rp_origin: None,
        webauthn_rp_name: None,
    };

    let rate_limiter = Arc::new(AuthRateLimiter::new(&config));

    Ok(ApiState {
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
        accounts: Arc::new(RwLock::new(AccountStore::new())),
        nodes: Arc::new(RwLock::new(weltgewebe_api::state::OrderedCache::new())),
        nodes_persist: Arc::new(tokio::sync::Mutex::new(())),
        accounts_persist: Arc::new(tokio::sync::Mutex::new(())),
        domain_projection_gate: std::sync::Arc::new(tokio::sync::RwLock::new(())),
        domain_projection_reload: std::sync::Arc::new(tokio::sync::Mutex::new(())),
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
    })
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
#[serial]
async fn auto_provision_persists_before_cache_and_survives_reload() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let email = "gast-provision@example.com";
    let state = provisioning_state(AutoProvisionRole::Gast, vec![email.to_string()])?;
    let app = app(state.clone());

    let res = app.oneshot(magic_link_request(email)).await?;
    assert_eq!(res.status(), StatusCode::OK);

    // Durable JSONL append happened.
    let accounts_file = in_dir.join("demo.accounts.jsonl");
    assert!(
        accounts_file.exists(),
        "auto-provisioning must durably append the JSONL accounts file"
    );
    let contents = std::fs::read_to_string(&accounts_file)?;
    assert_eq!(
        contents.lines().count(),
        1,
        "exactly one record must be appended"
    );
    let record: serde_json::Value = serde_json::from_str(contents.lines().next().unwrap())?;
    assert_eq!(record["email"], email);
    assert_eq!(record["role"], "gast");
    assert_eq!(record["map_state"], "not_on_map");

    // In-memory cache reflects it (read-your-writes).
    let account_id = {
        let accounts = state.accounts.read().await;
        let internal = accounts
            .values()
            .find(|a| a.email.as_deref() == Some(email))
            .expect("provisioned account present in cache");
        assert_eq!(internal.role, Role::Gast);
        internal.public.id.clone()
    };

    // Simulated restart: a fresh load from the JSONL file alone must
    // reconstruct the exact same account — no reliance on the in-memory cache.
    let reloaded = load_all_accounts().await?;
    let reloaded_account = reloaded
        .get(&account_id)
        .expect("account must be loadable from PostgreSQL/JSONL after a restart");
    assert_eq!(reloaded_account.email.as_deref(), Some(email));
    assert_eq!(reloaded_account.role, Role::Gast);

    Ok(())
}

#[tokio::test]
#[serial]
async fn jsonl_loader_rejects_legacy_account_instead_of_hiding_it() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    std::fs::write(
        in_dir.join("demo.accounts.jsonl"),
        concat!(
            r#"{"id":"legacy-disabled","type":"garnrolle","title":"Legacy disabled","map_state":"not_on_map","email":"blocked@example.com","disabled":true,"visibility":"private"}"#,
            "\n"
        ),
    )?;

    let error = match load_all_accounts().await {
        Ok(_) => panic!("legacy JSONL account must stop startup instead of disappearing"),
        Err(error) => error,
    };
    let message = error.to_string();
    assert!(message.contains("line 1"), "unexpected error: {message}");
    assert!(
        message.contains("canonical Garnrolle contract"),
        "unexpected error: {message}"
    );

    Ok(())
}

#[tokio::test]
#[serial]
async fn auto_provision_applies_configured_weber_role_to_persisted_record() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let email = "weber-provision@example.com";
    let state = provisioning_state(AutoProvisionRole::Weber, vec![email.to_string()])?;
    let app = app(state.clone());

    let res = app.oneshot(magic_link_request(email)).await?;
    assert_eq!(res.status(), StatusCode::OK);

    let accounts_file = in_dir.join("demo.accounts.jsonl");
    let contents = std::fs::read_to_string(&accounts_file)?;
    let record: serde_json::Value = serde_json::from_str(contents.lines().next().unwrap())?;
    assert_eq!(record["role"], "weber");

    let accounts = state.accounts.read().await;
    let internal = accounts
        .values()
        .find(|a| a.email.as_deref() == Some(email))
        .expect("provisioned account present in cache");
    assert_eq!(internal.role, Role::Weber);

    Ok(())
}

#[tokio::test]
#[serial]
async fn auto_provision_concurrent_requests_persist_exactly_one_account() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let email = "race-provision@example.com";
    let state = provisioning_state(AutoProvisionRole::Gast, vec![email.to_string()])?;

    let app_a = app(state.clone());
    let app_b = app(state.clone());

    let (res_a, res_b) = tokio::join!(
        app_a.oneshot(magic_link_request(email)),
        app_b.oneshot(magic_link_request(email)),
    );
    assert_eq!(res_a?.status(), StatusCode::OK);
    assert_eq!(res_b?.status(), StatusCode::OK);

    // Exactly one record for this email, both in the file and in the cache —
    // the double-checked lock must collapse the race to a single winner.
    let accounts_file = in_dir.join("demo.accounts.jsonl");
    let contents = std::fs::read_to_string(&accounts_file)?;
    let matching_lines = contents.lines().filter(|line| line.contains(email)).count();
    assert_eq!(
        matching_lines, 1,
        "a concurrent double-request must not append two JSONL records"
    );

    let accounts = state.accounts.read().await;
    let matching_accounts = accounts
        .values()
        .filter(|a| a.email.as_deref() == Some(email))
        .count();
    assert_eq!(
        matching_accounts, 1,
        "a concurrent double-request must not create two cached accounts"
    );

    Ok(())
}

#[tokio::test]
#[serial]
async fn auto_provision_persistence_failure_leaves_no_phantom_account() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    // Point GEWEBE_IN_DIR at a path that can never become a writable
    // directory: a regular file sits where the JSONL loader/writer expects a
    // directory, so `create_dir_all` in `append_account_line` fails.
    let blocked_dir = tmp.path().join("blocked");
    std::fs::write(&blocked_dir, b"not a directory")?;
    let _env = set_gewebe_in_dir(&blocked_dir);

    let email = "fails-to-persist@example.com";
    let state = provisioning_state(AutoProvisionRole::Gast, vec![email.to_string()])?;
    let app = app(state.clone());

    // Anti-enumeration: the HTTP response must stay the generic 200 even
    // though persistence failed internally.
    let res = app.oneshot(magic_link_request(email)).await?;
    assert_eq!(res.status(), StatusCode::OK);

    // No phantom cache-only account.
    let accounts = state.accounts.read().await;
    assert!(
        !accounts.values().any(|a| a.email.as_deref() == Some(email)),
        "a persistence failure must never leave a cache-only phantom account"
    );

    Ok(())
}
