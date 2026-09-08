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
    routes::{api_router, auth::GENERIC_LOGIN_MSG},
    state::ApiState,
    telemetry::{BuildInfo, Metrics},
};

mod helpers;
use helpers::set_gewebe_in_dir;

fn test_state_open_reg() -> Result<ApiState> {
    let metrics = Metrics::try_new(BuildInfo {
        version: "test",
        commit: "test",
        build_timestamp: "test",
    })?;

    // Option C: Open Registration Configuration
    // - Public Login: true
    // - Auto Provision: true
    // - Allowlist (Email): None
    // - Allowlist (Domain): None
    // - Rate Limits: Set (> 0)
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

        auth_allow_emails: None,        // Essential for Option C
        auth_allow_email_domains: None, // Essential for Option C
        auth_auto_provision: true,      // Essential for Option C
        auth_auto_provision_role: weltgewebe_api::config::AutoProvisionRole::Gast,

        // Mandatory Rate Limits for Option C
        auth_rl_ip_per_min: Some(100),
        auth_rl_ip_per_hour: Some(1000),
        auth_rl_email_per_min: Some(100),
        auth_rl_email_per_hour: Some(1000),
        node_mutation_rate_limits: Default::default(),

        smtp_host: None,
        smtp_port: None,
        smtp_user: None,
        smtp_pass: None,
        smtp_from: None,
        // Enable token logging to satisfy delivery requirement for tests without SMTP
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
        domain_projection_local_node_patch_handoff: std::sync::Arc::new(
            std::sync::atomic::AtomicI64::new(-1),
        ),
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
    Router::new()
        .merge(api_router())
        .layer(MockConnectInfo(SocketAddr::from(([127, 0, 0, 1], 8080))))
        .with_state(state)
}

#[tokio::test]
#[serial]
async fn test_open_registration_flow_auto_provisions_unknown_email() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let _env = set_gewebe_in_dir(tmp.path());

    let state = test_state_open_reg()?;
    let app = app(state.clone());

    let email = "unknown_user@example.com";
    let req = Request::post("/auth/magic-link/request")
        .header("Content-Type", "application/json")
        .body(body::Body::from(format!(r#"{{"email":"{}"}}"#, email)))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);

    // Check response body
    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let body_val: serde_json::Value = serde_json::from_slice(&body)?;
    assert_eq!(body_val["ok"], true);
    assert_eq!(body_val["message"], GENERIC_LOGIN_MSG);

    // CRITICAL ASSERTION:
    {
        let accounts = state.accounts.read().await;
        let found = accounts
            .values()
            .any(|acc| acc.email.as_deref() == Some(email));

        assert!(
            found,
            "Account should be auto-provisioned for unknown email in Open Registration mode"
        );
    }

    Ok(())
}

#[tokio::test]
#[serial]
async fn test_open_registration_rejects_invalid_email_shapes_without_side_effects() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let _env = set_gewebe_in_dir(tmp.path());

    let state = test_state_open_reg()?;
    let app = app(state.clone());
    let invalid_emails = [
        "a@b@c",
        "@example.org",
        "user@",
        "user name@example.org",
        " user@example.org",
        "user@example.org ",
        "user@example.org\n",
        "foo..bar@example.org",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa@example.org",
    ];

    for email in invalid_emails {
        let req = Request::post("/auth/magic-link/request")
            .header("Content-Type", "application/json")
            .body(body::Body::from(
                serde_json::json!({"email": email}).to_string(),
            ))?;

        let res = app.clone().oneshot(req).await?;
        assert_eq!(res.status(), StatusCode::OK, "email={email:?}");

        let response_body = body::to_bytes(res.into_body(), usize::MAX).await?;
        let body_val: serde_json::Value = serde_json::from_slice(&response_body)?;
        assert_eq!(body_val["ok"], true, "email={email:?}");
        assert_eq!(body_val["message"], GENERIC_LOGIN_MSG, "email={email:?}");

        let accounts = state.accounts.read().await;
        assert!(accounts.values().next().is_none(), "email={email:?}");
        drop(accounts);
        #[cfg(feature = "integration-testing")]
        {
            assert!(
                state.tokens.latest_raw_for_email(email).is_none(),
                "invalid email must not create a token: {email:?}"
            );
            let legacy_normalized = email.trim().to_ascii_lowercase();
            assert!(
                state
                    .tokens
                    .latest_raw_for_email(&legacy_normalized)
                    .is_none(),
                "invalid email must not create a normalized token: {email:?}"
            );
        }
    }

    Ok(())
}
