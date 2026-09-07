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
    auth::{accounts::AccountStore, rate_limit::AuthRateLimiter, session::SessionBackend},
    config::AppConfig,
    routes::api_router,
    state::ApiState,
    telemetry::{BuildInfo, Metrics},
    test_helpers::EnvGuard,
};

fn test_state(config: AppConfig) -> Result<ApiState> {
    let metrics = Metrics::try_new(BuildInfo {
        version: "test",
        commit: "test",
        build_timestamp: "test",
    })?;

    let rate_limiter = Arc::new(AuthRateLimiter::new(&config));

    Ok(ApiState {
        db_pool: None,
        db_pool_configured: false,
        nats_client: None,
        nats_configured: false,
        config,
        metrics,
        sessions: SessionBackend::new_in_memory(),
        challenges: Default::default(),
        tokens: weltgewebe_api::auth::tokens::TokenStore::new(),
        step_up_tokens: weltgewebe_api::auth::step_up_tokens::StepUpTokenStore::new(),
        accounts: Arc::new(RwLock::new(AccountStore::new())),
        nodes: Arc::new(tokio::sync::RwLock::new(
            weltgewebe_api::state::OrderedCache::new(),
        )),
        nodes_persist: Arc::new(tokio::sync::Mutex::new(())),
        accounts_persist: Arc::new(tokio::sync::Mutex::new(())),
        domain_projection_gate: std::sync::Arc::new(tokio::sync::RwLock::new(())),
        domain_projection_reload: std::sync::Arc::new(tokio::sync::Mutex::new(())),
        domain_projection_version: std::sync::Arc::new(std::sync::atomic::AtomicI64::new(0)),
        edges: Arc::new(tokio::sync::RwLock::new(
            weltgewebe_api::state::OrderedCache::new(),
        )),
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
    // Mock the peer address as the "Proxy" IP (e.g. Caddy)
    Router::new()
        .merge(api_router())
        .layer(MockConnectInfo(SocketAddr::from(([127, 0, 0, 1], 8080))))
        .with_state(state)
}

fn default_config() -> AppConfig {
    AppConfig {
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
        auth_log_magic_token: true,
        webauthn_rp_id: None,
        webauthn_rp_origin: None,
        webauthn_rp_name: None,
    }
}

#[tokio::test]
#[serial]
async fn rate_limit_ignores_forwarded_header_when_untrusted() -> Result<()> {
    // 1. Setup: Trust NOTHING (or non-matching IP). Peer is 127.0.0.1.
    // We explicitly set trusted proxies to something else to ensure 127.0.0.1 is untrusted.
    let _guard = EnvGuard::set("AUTH_TRUSTED_PROXIES", "10.0.0.1");

    let mut config = default_config();
    // Logic reads env via effective_client_ip -> get_trusted_proxies
    config.auth_rl_ip_per_min = Some(2);

    let state = test_state(config)?;
    let app = app(state);

    // 2. Client A claims to be 1.2.3.4
    let req_client_a = || {
        Request::post("/auth/magic-link/request")
            .header("Content-Type", "application/json")
            .header("X-Forwarded-For", "1.2.3.4")
            .body(body::Body::from(r#"{"email":"u1@example.com"}"#))
    };

    // 3. Client B claims to be 5.6.7.8
    let req_client_b = || {
        Request::post("/auth/magic-link/request")
            .header("Content-Type", "application/json")
            .header("X-Forwarded-For", "5.6.7.8")
            .body(body::Body::from(r#"{"email":"u2@example.com"}"#))
    };

    // Since proxy (127.0.0.1) is UNTRUSTED, both are treated as coming from 127.0.0.1
    // Limit is 2 per IP.

    // Req 1 (A) -> OK (127.0.0.1 count: 1)
    let res = app.clone().oneshot(req_client_a()?).await?;
    assert_eq!(res.status(), StatusCode::OK);

    // Req 2 (B) -> OK (127.0.0.1 count: 2)
    let res = app.clone().oneshot(req_client_b()?).await?;
    assert_eq!(res.status(), StatusCode::OK);

    // Req 3 (A) -> 429 (127.0.0.1 count: 3 > 2)
    // If it trusted the header, A would be at 2, and B at 1, so this would succeed.
    // Because it ignores the header, it sees 127.0.0.1 again and blocks.
    let res = app.clone().oneshot(req_client_a()?).await?;
    assert_eq!(res.status(), StatusCode::TOO_MANY_REQUESTS);

    Ok(())
}
