use anyhow::{Context, Result};
use axum::{
    body,
    extract::connect_info::MockConnectInfo,
    http::{header::SET_COOKIE, HeaderMap, HeaderValue, Request, StatusCode},
    response::IntoResponse,
    Router,
};
use axum_extra::extract::cookie::{Cookie, CookieJar, SameSite};
use serial_test::serial;
use sha2::{Digest, Sha256};
use std::{net::SocketAddr, sync::Arc};
use tokio::sync::RwLock;
use tower::ServiceExt;
use weltgewebe_api::{
    auth::{
        accounts::AccountStore, rate_limit::AuthRateLimiter, role::Role, session::SessionBackend,
    },
    config::AppConfig,
    routes::{
        accounts::{AccountInternal, AccountPublic},
        api_router,
        auth::{GENERIC_LOGIN_MSG, NONCE_COOKIE_NAME, SESSION_COOKIE_NAME},
    },
    state::ApiState,
    telemetry::{BuildInfo, Metrics},
};

async fn create_session(
    state: &ApiState,
    account_id: &str,
    existing_device_id: Option<&str>,
) -> weltgewebe_api::auth::session::Session {
    state
        .sessions
        .create(
            account_id.to_string(),
            existing_device_id.map(std::string::ToString::to_string),
        )
        .await
        .expect("in-memory session backend must create session")
}

async fn get_session(
    state: &ApiState,
    session_id: &str,
) -> Option<weltgewebe_api::auth::session::Session> {
    state
        .sessions
        .get(session_id)
        .await
        .expect("in-memory session backend must fetch session")
}

async fn delete_session(state: &ApiState, session_id: &str) {
    state
        .sessions
        .delete(session_id)
        .await
        .expect("in-memory session backend must delete session");
}

fn test_state() -> Result<ApiState> {
    let metrics = Metrics::try_new(BuildInfo {
        version: "test",
        commit: "test",
        build_timestamp: "test",
    })?;

    let config = AppConfig {
        max_guest_owned_nodes: 1_000,
        domain_read_source: weltgewebe_api::config::DomainReadSource::Jsonl,
        domain_account_write_source: weltgewebe_api::config::DomainAccountWriteSource::Jsonl,
        domain_node_write_source: weltgewebe_api::config::DomainNodeWriteSource::Jsonl,
        domain_edge_write_source: weltgewebe_api::config::DomainEdgeWriteSource::Jsonl,
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
        // Enable token logging to satisfy "delivery mechanism required" policy for tests
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

fn test_state_with_accounts() -> Result<ApiState> {
    let mut state = test_state()?;
    let mut account_map = AccountStore::new();

    let account = AccountInternal {
        public: AccountPublic {
            id: "u1".to_string(),
            kind: "garnrolle".to_string(),
            title: "User One".to_string(),
            summary: Some("Summary 1".to_string()),
            public_pos: None,
            map_state: weltgewebe_api::routes::accounts::GarnrolleMapState::NotOnMap,
            radius_m: 0,

            disabled: false,
            tags: vec![],
        },
        role: Role::Gast,
        email: Some("u1@example.com".to_string()),
        webauthn_user_id: uuid::Uuid::new_v4(),
    };
    account_map.insert(account);

    let account = AccountInternal {
        public: AccountPublic {
            id: "a1".to_string(),
            kind: "garnrolle".to_string(),
            title: "Admin One".to_string(),
            summary: None,
            public_pos: None,
            map_state: weltgewebe_api::routes::accounts::GarnrolleMapState::NotOnMap,
            radius_m: 0,

            disabled: false,
            tags: vec![],
        },
        role: Role::Admin,
        email: Some("a1@example.com".to_string()),
        webauthn_user_id: uuid::Uuid::new_v4(),
    };
    account_map.insert(account);

    state.accounts = Arc::new(RwLock::new(account_map));
    Ok(state)
}

fn app(state: ApiState) -> Router {
    app_with_addr(state, "127.0.0.1:8080".parse().unwrap())
}

fn app_with_addr(state: ApiState, addr: SocketAddr) -> Router {
    Router::new()
        .merge(api_router())
        .layer(MockConnectInfo(addr))
        .with_state(state)
}

fn app_with_security(state: ApiState) -> Router {
    Router::new()
        .merge(
            api_router()
                .route_layer(axum::middleware::from_fn_with_state(
                    state.clone(),
                    weltgewebe_api::middleware::auth::auth_middleware,
                ))
                .layer(axum::middleware::from_fn(
                    weltgewebe_api::middleware::csrf::require_csrf,
                )),
        )
        .layer(MockConnectInfo(SocketAddr::from(([127, 0, 0, 1], 8080))))
        .with_state(state)
}

struct DeferEnvRemove(&'static str);
impl Drop for DeferEnvRemove {
    fn drop(&mut self) {
        unsafe {
            std::env::remove_var(self.0);
        }
    }
}
fn defer_env_remove(key: &'static str) -> DeferEnvRemove {
    DeferEnvRemove(key)
}

#[tokio::test]
#[serial]
async fn auth_login_fails_when_dev_login_disabled() -> Result<()> {
    unsafe {
        std::env::remove_var("AUTH_DEV_LOGIN");
    }
    let state = test_state()?;
    let app = app(state);

    let req = Request::post("/auth/dev/login")
        .header("Content-Type", "application/json")
        .body(body::Body::from(r#"{"account_id":"any"}"#))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::NOT_FOUND);
    Ok(())
}

#[tokio::test]
#[serial]
async fn request_login_denied_if_account_disabled() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());

    // Disable the account
    {
        let mut accounts = state.accounts.write().await;
        if let Some(acc) = accounts.get("u1").cloned() {
            let mut acc = acc;
            acc.public.disabled = true;
            accounts.insert(acc);
        }
    }

    let app = app(state.clone());

    let req = Request::post("/auth/magic-link/request")
        .header("Content-Type", "application/json")
        .body(body::Body::from(r#"{"email":"u1@example.com"}"#))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);

    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let body_val: serde_json::Value = serde_json::from_slice(&body)?;

    // Should return generic success
    assert_eq!(body_val["ok"], true);
    assert_eq!(body_val["message"], GENERIC_LOGIN_MSG);

    Ok(())
}

#[tokio::test]
#[serial]
async fn consume_login_fails_if_account_disabled() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());

    // Disable the account
    {
        let mut accounts = state.accounts.write().await;
        if let Some(acc) = accounts.get("u1").cloned() {
            let mut acc = acc;
            acc.public.disabled = true;
            accounts.insert(acc);
        }
    }

    // Create a valid token manually
    let token = state.tokens.create("u1@example.com".to_string());
    let app = app(state);

    // 1. GET (Confirm Page) - This might still work because it only validates token existence, not account status?
    // `consume_login_get` only calls `state.tokens.peek`. It doesn't look up the account.
    // So GET will show the form. This is acceptable (leaks nothing sensitive).

    let nonce_val = {
        // Helper to get nonce without full request flow
        // Or just do the GET request
        let uri = format!("/auth/magic-link/consume?token={}", token);
        let req_get = Request::get(&uri).body(body::Body::empty())?;
        let res_get = app.clone().oneshot(req_get).await?;
        assert_eq!(res_get.status(), StatusCode::OK);
        extract_cookie_value(res_get.headers(), NONCE_COOKIE_NAME).context("missing nonce")?
    };

    // Extract nonce from cookie (hash.nonce)
    let (_, nonce) = nonce_val.split_once('.').context("invalid nonce format")?;
    let nonce = nonce.to_string();

    // 2. POST (Consume)
    let body_str = format!("token={}&nonce={}", token, nonce);
    let req_post = Request::post("/auth/magic-link/consume")
        .header("Content-Type", "application/x-www-form-urlencoded")
        .header("Cookie", format!("{}={}", NONCE_COOKIE_NAME, nonce_val))
        .body(body::Body::from(body_str))?;

    let res_post = app.clone().oneshot(req_post).await?;

    // Should fail and redirect to login error
    assert_eq!(res_post.status(), StatusCode::SEE_OTHER);
    let location = res_post
        .headers()
        .get("location")
        .unwrap()
        .to_str()
        .unwrap();
    assert_eq!(location, "/login?error=account_disabled");

    Ok(())
}

#[tokio::test]
#[serial]
async fn auth_login_succeeds_with_flag_and_account() -> Result<()> {
    unsafe {
        std::env::set_var("AUTH_DEV_LOGIN", "1");
    }
    let _defer = defer_env_remove("AUTH_DEV_LOGIN");
    let _guard_cookie = weltgewebe_api::test_helpers::EnvGuard::set("AUTH_COOKIE_SECURE", "1");

    let mut account_map = AccountStore::new();
    let account = AccountInternal {
        public: AccountPublic {
            id: "u1".to_string(),
            kind: "garnrolle".to_string(),
            title: "User".to_string(),
            summary: None,
            public_pos: None,
            map_state: weltgewebe_api::routes::accounts::GarnrolleMapState::NotOnMap,
            radius_m: 0,

            disabled: false,
            tags: vec![],
        },
        role: Role::Gast,
        email: Some("u1@example.com".to_string()),
        webauthn_user_id: uuid::Uuid::new_v4(),
    };
    account_map.insert(account);

    let mut state = test_state()?;
    state.accounts = Arc::new(RwLock::new(account_map));

    let app = app(state);

    let req = Request::post("/auth/dev/login")
        .header("Content-Type", "application/json")
        .body(body::Body::from(r#"{"account_id":"u1"}"#))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);

    let cookie = res
        .headers()
        .get("set-cookie")
        .context("missing set-cookie")?
        .to_str()?;
    assert!(cookie.contains(SESSION_COOKIE_NAME));
    assert!(cookie.contains("Secure"));
    assert!(cookie.contains("HttpOnly"));
    assert!(cookie.contains("SameSite=Lax"));

    Ok(())
}

#[tokio::test]
#[serial]
async fn list_dev_accounts_succeeds_ipv6_localhost() -> Result<()> {
    unsafe {
        std::env::set_var("AUTH_DEV_LOGIN", "1");
    }
    let _defer = defer_env_remove("AUTH_DEV_LOGIN");

    let state = test_state_with_accounts()?;
    // Use IPv6 loopback address
    let app = app_with_addr(state, "[::1]:8080".parse()?);

    let req = Request::get("/auth/dev/accounts").body(body::Body::empty())?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);

    Ok(())
}

#[tokio::test]
#[serial]
async fn list_dev_accounts_fails_when_dev_login_disabled() -> Result<()> {
    unsafe {
        std::env::remove_var("AUTH_DEV_LOGIN");
    }
    let state = test_state_with_accounts()?;
    let app = app(state);

    let req = Request::get("/auth/dev/accounts").body(body::Body::empty())?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::NOT_FOUND);
    Ok(())
}

#[tokio::test]
#[serial]
async fn list_dev_accounts_succeeds_localhost() -> Result<()> {
    unsafe {
        std::env::set_var("AUTH_DEV_LOGIN", "1");
    }
    let _defer = defer_env_remove("AUTH_DEV_LOGIN");

    let state = test_state_with_accounts()?;
    let app = app(state);

    let req = Request::get("/auth/dev/accounts").body(body::Body::empty())?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);

    let body_bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let accounts: Vec<serde_json::Value> = serde_json::from_slice(&body_bytes)?;
    assert_eq!(accounts.len(), 2);
    // Sort order check: a1 should be before u1
    assert_eq!(accounts[0]["id"], "a1");
    assert_eq!(accounts[1]["id"], "u1");
    assert_eq!(accounts[0]["role"], "admin");
    assert_eq!(accounts[1]["role"], "gast");

    Ok(())
}

#[tokio::test]
#[serial]
async fn list_dev_accounts_fails_remote() -> Result<()> {
    unsafe {
        std::env::set_var("AUTH_DEV_LOGIN", "1");
        std::env::remove_var("AUTH_DEV_LOGIN_ALLOW_REMOTE");
    }
    let _defer = defer_env_remove("AUTH_DEV_LOGIN");

    let state = test_state_with_accounts()?;
    // Use a non-localhost IP to simulate remote access
    let app = app_with_addr(state, "192.168.1.100:8080".parse()?);

    let req = Request::get("/auth/dev/accounts").body(body::Body::empty())?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::FORBIDDEN);
    Ok(())
}

#[tokio::test]
#[serial]
async fn list_dev_accounts_succeeds_remote_allowed() -> Result<()> {
    unsafe {
        std::env::set_var("AUTH_DEV_LOGIN", "1");
        std::env::set_var("AUTH_DEV_LOGIN_ALLOW_REMOTE", "1");
    }
    let _defer1 = defer_env_remove("AUTH_DEV_LOGIN");
    let _defer2 = defer_env_remove("AUTH_DEV_LOGIN_ALLOW_REMOTE");

    let state = test_state_with_accounts()?;
    // Use a non-localhost IP to simulate remote access
    let app = app_with_addr(state, "192.168.1.100:8080".parse()?);

    let req = Request::get("/auth/dev/accounts").body(body::Body::empty())?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);
    Ok(())
}

#[tokio::test]
#[serial]
async fn list_dev_accounts_rejects_spoofed_host_header() -> Result<()> {
    unsafe {
        std::env::set_var("AUTH_DEV_LOGIN", "1");
        std::env::remove_var("AUTH_DEV_LOGIN_ALLOW_REMOTE");
    }
    let _defer = defer_env_remove("AUTH_DEV_LOGIN");

    let state = test_state_with_accounts()?;
    // Use a non-localhost IP (actual client address)
    let app = app_with_addr(state, "203.0.113.42:12345".parse()?);

    // Try to bypass the guard by spoofing the Host header
    let req = Request::get("/auth/dev/accounts")
        .header("Host", "localhost:8080")
        .body(body::Body::empty())?;

    let res = app.oneshot(req).await?;
    // Should still be forbidden because actual socket address is not localhost
    assert_eq!(res.status(), StatusCode::FORBIDDEN);
    Ok(())
}

#[tokio::test]
#[serial]
async fn auth_login_fails_from_remote_without_allow_flag() -> Result<()> {
    unsafe {
        std::env::set_var("AUTH_DEV_LOGIN", "1");
        std::env::remove_var("AUTH_DEV_LOGIN_ALLOW_REMOTE");
    }
    let _defer = defer_env_remove("AUTH_DEV_LOGIN");

    let state = test_state_with_accounts()?;
    // Use a non-localhost IP to simulate remote access
    let app = app_with_addr(state, "192.168.1.100:8080".parse()?);

    let req = Request::post("/auth/dev/login")
        .header("Content-Type", "application/json")
        .body(body::Body::from(r#"{"account_id":"u1"}"#))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::FORBIDDEN);
    Ok(())
}

#[tokio::test]
#[serial]
async fn auth_login_succeeds_from_remote_with_allow_flag() -> Result<()> {
    unsafe {
        std::env::set_var("AUTH_DEV_LOGIN", "1");
        std::env::set_var("AUTH_DEV_LOGIN_ALLOW_REMOTE", "1");
    }
    let _defer1 = defer_env_remove("AUTH_DEV_LOGIN");
    let _defer2 = defer_env_remove("AUTH_DEV_LOGIN_ALLOW_REMOTE");

    let state = test_state_with_accounts()?;
    // Use a non-localhost IP to simulate remote access
    let app = app_with_addr(state, "192.168.1.100:8080".parse()?);

    let req = Request::post("/auth/dev/login")
        .header("Content-Type", "application/json")
        .body(body::Body::from(r#"{"account_id":"u1"}"#))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);

    let cookie = res
        .headers()
        .get("set-cookie")
        .context("missing set-cookie")?
        .to_str()?;
    assert!(cookie.contains(SESSION_COOKIE_NAME));
    Ok(())
}

#[tokio::test]
#[serial]
async fn request_login_fails_when_public_login_disabled() -> Result<()> {
    let mut state = test_state()?;
    state.config.auth_public_login = false;
    let app = app(state);

    let req = Request::post("/auth/magic-link/request")
        .header("Content-Type", "application/json")
        .body(body::Body::from(r#"{"email":"u1@example.com"}"#))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::NOT_FOUND);
    Ok(())
}

#[tokio::test]
#[serial]
async fn request_login_succeeds_when_public_login_enabled() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());

    let app = app(state);

    let req = Request::post("/auth/magic-link/request")
        .header("Content-Type", "application/json")
        .body(body::Body::from(r#"{"email":"u1@example.com"}"#))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);

    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let body_val: serde_json::Value = serde_json::from_slice(&body)?;

    // Check JSON contract
    assert_eq!(body_val["ok"], true);
    assert_eq!(body_val["message"], GENERIC_LOGIN_MSG);

    // Security check: no token leak in the entire JSON string representation
    let body_str = body_val.to_string();
    assert!(!body_str.contains("token="));
    // Security check: ensure email is not echoed back
    assert!(!body_str.contains("u1@example.com"));

    Ok(())
}

#[tokio::test]
#[serial]
async fn request_login_unknown_user_returns_identical_response() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());

    let app = app(state);

    let req = Request::post("/auth/magic-link/request")
        .header("Content-Type", "application/json")
        .body(body::Body::from(r#"{"email":"unknown@example.com"}"#))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);

    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let body_val: serde_json::Value = serde_json::from_slice(&body)?;

    assert_eq!(body_val["ok"], true);
    assert_eq!(body_val["message"], GENERIC_LOGIN_MSG);
    assert!(!body_val.to_string().contains("unknown@example.com"));

    Ok(())
}

#[tokio::test]
#[serial]
async fn request_login_rejects_overlong_email_with_generic_response() -> Result<()> {
    // Guards against unbounded work (hashing, rate-limit lookups, mailer dispatch) on
    // arbitrary client input. Anti-Enumeration parity must hold: the response shape
    // matches the known-/unknown-user baseline above and emits no Set-Cookie.
    let mut state = test_state()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());

    let app = app(state);

    // Create an overlong email (260 bytes)
    let local_part = "overlong".repeat(30); // ~240 bytes
    let email_long = format!("{}@example.com", local_part);
    let email_long = if email_long.len() < 260 {
        let padding = "x".repeat(260 - email_long.len());
        format!("{}{}@example.com", local_part, padding)
    } else {
        email_long[..260].to_string()
    };

    let body_str = format!(r#"{{"email":"{}"}}"#, email_long);

    let req = Request::post("/auth/magic-link/request")
        .header("Content-Type", "application/json")
        .body(body::Body::from(body_str))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);

    let headers = res.headers().clone();
    assert!(
        headers.get_all("set-cookie").iter().next().is_none(),
        "overlong-email rejection must not emit any Set-Cookie header"
    );

    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let body_val: serde_json::Value = serde_json::from_slice(&body)?;

    assert_eq!(body_val["ok"], true);
    assert_eq!(body_val["message"], GENERIC_LOGIN_MSG);
    assert!(!body_val.to_string().contains(&local_part));

    Ok(())
}

#[tokio::test]
#[serial]
#[cfg(feature = "integration-testing")]
async fn request_login_overlong_known_email_skips_token_creation() -> Result<()> {
    // Prove that NO token is created for overlong emails when sent to a known account.
    // This is the critical proof that the MAX_EMAIL_LEN guard prevents downstream work
    // (token generation) for overlong inputs, even for known accounts.
    // Without the guard, a token WOULD be created for this known account.
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());

    // Create an account with a 260-byte email
    let local_part = "overlong".repeat(30); // ~240 bytes
    let email_long = format!("{}@example.com", local_part);
    let email_long = if email_long.len() < 260 {
        let padding = "x".repeat(260 - email_long.len());
        format!("{}{}@example.com", local_part, padding)
    } else {
        email_long[..260].to_string()
    };

    let account = AccountInternal {
        public: AccountPublic {
            id: "u_long".to_string(),
            kind: "garnrolle".to_string(),
            title: "Long Email User".to_string(),
            summary: None,
            public_pos: None,
            map_state: weltgewebe_api::routes::accounts::GarnrolleMapState::NotOnMap,
            radius_m: 0,
            disabled: false,
            tags: vec![],
        },
        role: Role::Gast,
        email: Some(email_long.clone()),
        webauthn_user_id: uuid::Uuid::new_v4(),
    };
    {
        let mut accounts = state.accounts.write().await;
        accounts.insert(account);
    }

    let app = app(state.clone());

    let body_str = format!(r#"{{"email":"{}"}}"#, email_long);

    let req = Request::post("/auth/magic-link/request")
        .header("Content-Type", "application/json")
        .body(body::Body::from(body_str))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);

    // Prove that NO token was created for the overlong email (guards against token generation).
    // Without the MAX_EMAIL_LEN check, a token WOULD be created for this known account.
    // This assertion would fail, proving the guard is working.
    assert_eq!(
        state.tokens.latest_raw_for_email(&email_long),
        None,
        "overlong email for known account must not trigger token creation (proves guard works)"
    );

    Ok(())
}

/// Generates an ASCII email-shaped address with valid local/domain label structure
/// and the exact target byte length.
///
/// For 254 bytes this is within the RFC 5321 mailbox limit. For 255 bytes this
/// intentionally exceeds that limit while keeping DNS label structure valid.
///
/// Local-part is limited to 64 octets (RFC 5321), domain labels to 63 octets each (RFC 1035).
/// Structure: "aaaa...aaaa" (64) + "@" (1) + "aaaa...aaaa.aaaa...aaaa.co" (remaining)
#[cfg(feature = "integration-testing")]
fn generate_rfc_email_with_length(target_len: usize) -> String {
    const LOCAL_PART_LEN: usize = 64;
    const AT_LEN: usize = 1;
    const MAX_LABEL_LEN: usize = 63; // RFC 1035 DNS label limit

    assert!(
        target_len > LOCAL_PART_LEN + AT_LEN,
        "target_len must be greater than {} bytes (local + @)",
        LOCAL_PART_LEN + AT_LEN
    );

    let local_part = "a".repeat(LOCAL_PART_LEN);
    let domain_bytes_available = target_len.saturating_sub(LOCAL_PART_LEN + AT_LEN);

    let mut domain = String::new();
    let mut remaining = domain_bytes_available;

    // Build labels separated by dots, with final label having no trailing dot
    while remaining > MAX_LABEL_LEN {
        // Add a full 63-byte label + dot
        domain.push_str(&"a".repeat(MAX_LABEL_LEN));
        domain.push('.');
        remaining -= MAX_LABEL_LEN + 1; // 63 bytes label + 1 byte dot
    }

    // Add final label with all remaining bytes (no trailing dot)
    if remaining > 0 {
        domain.push_str(&"a".repeat(remaining));
    }

    // Verify RFC compliance invariants
    assert!(!domain.starts_with('.'), "domain must not start with a dot");
    assert!(!domain.ends_with('.'), "domain must not end with a dot");
    assert!(
        domain
            .split('.')
            .all(|label| !label.is_empty() && label.len() <= MAX_LABEL_LEN),
        "domain labels must be non-empty and <= 63 bytes"
    );

    let email = format!("{}@{}", local_part, domain);
    assert_eq!(
        email.len(),
        target_len,
        "generated email must be exactly {} bytes",
        target_len
    );
    email
}

#[tokio::test]
#[serial]
#[cfg(feature = "integration-testing")]
async fn request_login_accepts_boundary_254_byte_email_for_known_account() -> Result<()> {
    // Verify that the 254-byte limit accepts exactly 254 bytes for known accounts.
    // This tests the boundary: 254 bytes PASSES, 255 bytes FAILS.
    // Email of exactly 254 bytes should create a token for a known account.
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());

    // Create an account with an email of exactly 254 bytes using RFC-valid structure
    // (local_part 64 bytes + '@' 1 byte + domain 189 bytes = 254 bytes total)
    let email = generate_rfc_email_with_length(254);
    assert_eq!(email.len(), 254, "email must be exactly 254 bytes");

    let account = AccountInternal {
        public: AccountPublic {
            id: "u_boundary_254".to_string(),
            kind: "garnrolle".to_string(),
            title: "Boundary 254 User".to_string(),
            summary: None,
            public_pos: None,
            map_state: weltgewebe_api::routes::accounts::GarnrolleMapState::NotOnMap,
            radius_m: 0,
            disabled: false,
            tags: vec![],
        },
        role: Role::Gast,
        email: Some(email.clone()),
        webauthn_user_id: uuid::Uuid::new_v4(),
    };
    {
        let mut accounts = state.accounts.write().await;
        accounts.insert(account);
    }

    let app = app(state.clone());

    let body_str = format!(r#"{{"email":"{}"}}"#, email);

    let req = Request::post("/auth/magic-link/request")
        .header("Content-Type", "application/json")
        .body(body::Body::from(body_str))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);

    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let body_val: serde_json::Value = serde_json::from_slice(&body)?;

    assert_eq!(body_val["ok"], true);
    assert_eq!(body_val["message"], GENERIC_LOGIN_MSG);

    // Verify token WAS created for 254-byte email (proves the boundary is correct and inclusive)
    assert!(
        state.tokens.latest_raw_for_email(&email).is_some(),
        "254-byte email for known account must create a token (boundary inclusive)"
    );

    Ok(())
}

#[tokio::test]
#[serial]
#[cfg(feature = "integration-testing")]
async fn request_login_rejects_boundary_255_byte_email_for_known_account() -> Result<()> {
    // Verify that the 254-byte limit rejects 255 bytes for known accounts.
    // Email of 255 bytes should NOT create a token for a known account.
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());

    // Create an account with an email of exactly 255 bytes using RFC-valid structure
    // (local_part 64 bytes + '@' 1 byte + domain 190 bytes = 255 bytes total)
    let email = generate_rfc_email_with_length(255);
    assert_eq!(email.len(), 255, "email must be exactly 255 bytes");

    let account = AccountInternal {
        public: AccountPublic {
            id: "u_boundary_255".to_string(),
            kind: "garnrolle".to_string(),
            title: "Boundary 255 User".to_string(),
            summary: None,
            public_pos: None,
            map_state: weltgewebe_api::routes::accounts::GarnrolleMapState::NotOnMap,
            radius_m: 0,
            disabled: false,
            tags: vec![],
        },
        role: Role::Gast,
        email: Some(email.clone()),
        webauthn_user_id: uuid::Uuid::new_v4(),
    };
    {
        let mut accounts = state.accounts.write().await;
        accounts.insert(account);
    }

    let app = app(state.clone());

    let body_str = format!(r#"{{"email":"{}"}}"#, email);

    let req = Request::post("/auth/magic-link/request")
        .header("Content-Type", "application/json")
        .body(body::Body::from(body_str))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);

    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let body_val: serde_json::Value = serde_json::from_slice(&body)?;

    assert_eq!(body_val["ok"], true);
    assert_eq!(body_val["message"], GENERIC_LOGIN_MSG);

    // Verify NO token was created for 255-byte email (proves rejection)
    assert_eq!(
        state.tokens.latest_raw_for_email(&email),
        None,
        "255-byte email for known account must be rejected and not create a token"
    );

    Ok(())
}

fn hash_token(token: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(token.as_bytes());
    hex::encode(hasher.finalize())
}

fn extract_cookie_value(headers: &HeaderMap, name: &str) -> Option<String> {
    headers.get_all("set-cookie").iter().find_map(|val| {
        let s = val.to_str().ok()?;
        let (cookie_part, _) = s.split_once(';').unwrap_or((s, ""));
        let (key, value) = cookie_part.split_once('=')?;
        if key.trim() == name {
            let v = value.trim();
            let v = v
                .strip_prefix('"')
                .and_then(|s| s.strip_suffix('"'))
                .unwrap_or(v);
            Some(v.to_string())
        } else {
            None
        }
    })
}

/// Extract full Set-Cookie header for inspection (includes attributes).
fn extract_set_cookie_header(headers: &HeaderMap, name: &str) -> Option<String> {
    headers.get_all("set-cookie").iter().find_map(|val| {
        let s = val.to_str().ok()?;
        let cookie_part = s.split_once(';').map(|(part, _)| part).unwrap_or(s);
        let (key, _) = cookie_part.split_once('=')?;
        if key.trim() == name {
            Some(s.to_string())
        } else {
            None
        }
    })
}

fn has_cookie_flag_attribute(cookie_header: &str, attr_name: &str) -> bool {
    cookie_header
        .split(';')
        .skip(1)
        .map(str::trim)
        .any(|attr| attr.eq_ignore_ascii_case(attr_name))
}

fn has_cookie_samesite_lax(cookie_header: &str) -> bool {
    cookie_header
        .split(';')
        .skip(1)
        .map(str::trim)
        .filter_map(|attr| attr.split_once('='))
        .any(|(key, value)| {
            key.trim().eq_ignore_ascii_case("samesite") && value.trim().eq_ignore_ascii_case("lax")
        })
}

fn cookie_attribute_value<'a>(cookie_header: &'a str, name: &str) -> Option<&'a str> {
    cookie_header
        .split(';')
        .skip(1)
        .map(str::trim)
        .filter_map(|attribute| attribute.split_once('='))
        .find_map(|(key, value)| {
            key.trim()
                .eq_ignore_ascii_case(name)
                .then_some(value.trim())
        })
}

const SESSION_COOKIE_EXPIRY_SAFETY_SECONDS: i64 = 5 * 60;

fn assert_persistent_session_cookie(cookie_header: &str, expect_secure: bool) {
    assert_session_cookie_secure(cookie_header, expect_secure);
    assert_eq!(
        cookie_attribute_value(cookie_header, "path"),
        Some("/"),
        "Session cookie must use profile-wide Path=/; got: {}",
        cookie_header
    );
    let max_age = cookie_attribute_value(cookie_header, "max-age")
        .and_then(|value| value.parse::<i64>().ok())
        .expect("persistent session cookie must contain an integer Max-Age");
    assert!(
        max_age > 0,
        "persistent session cookie Max-Age must be positive; got: {}",
        cookie_header
    );
    assert!(
        cookie_attribute_value(cookie_header, "expires").is_some(),
        "persistent session cookie must contain Expires; got: {}",
        cookie_header
    );
}

fn assert_removal_session_cookie(cookie_header: &str, expect_secure: bool) {
    assert_session_cookie_secure(cookie_header, expect_secure);
    let cookie =
        Cookie::parse(cookie_header.to_string()).expect("removal Set-Cookie header must parse");
    assert_eq!(cookie.name(), SESSION_COOKIE_NAME);
    assert_eq!(cookie.value(), "");
    assert_eq!(cookie.path(), Some("/"));
    assert!(
        cookie.max_age().is_some_and(|age| age.whole_seconds() <= 0),
        "removal session cookie must have Max-Age=0; got: {cookie_header}"
    );
    assert!(
        cookie
            .expires_datetime()
            .is_some_and(|expires| expires <= time::OffsetDateTime::UNIX_EPOCH),
        "removal session cookie must be expired; got: {cookie_header}"
    );
}

fn assert_persistent_cookie_matches_stored_session(
    cookie_header: &str,
    expect_secure: bool,
    session: &weltgewebe_api::auth::session::Session,
    response_started: chrono::DateTime<chrono::Utc>,
    response_finished: chrono::DateTime<chrono::Utc>,
) {
    assert_persistent_session_cookie(cookie_header, expect_secure);
    let cookie =
        Cookie::parse(cookie_header.to_string()).expect("persistent Set-Cookie header must parse");
    let cookie_expires = cookie
        .expires_datetime()
        .expect("persistent session cookie must have Expires")
        .unix_timestamp();
    assert_eq!(
        cookie_expires,
        session.expires_at.timestamp() - SESSION_COOKIE_EXPIRY_SAFETY_SECONDS,
        "cookie Expires must be derived from the stored server-session expiry"
    );

    let max_age = cookie
        .max_age()
        .expect("persistent session cookie must have Max-Age")
        .whole_seconds();
    let lower_bound = cookie_expires - response_finished.timestamp() - 1;
    let upper_bound = cookie_expires - response_started.timestamp() + 1;
    assert!(
        max_age >= lower_bound && max_age <= upper_bound,
        "cookie Max-Age must describe the same conservative expiry window;          max_age={max_age}, expected_range={lower_bound}..={upper_bound}"
    );
}

/// Verify that Session Cookie attributes match security requirements.
fn assert_session_cookie_secure(cookie_header: &str, expect_secure: bool) {
    // Must have httponly
    assert!(
        has_cookie_flag_attribute(cookie_header, "httponly"),
        "Session cookie must have HttpOnly attribute; got: {}",
        cookie_header
    );

    // Must have SameSite=Lax
    assert!(
        has_cookie_samesite_lax(cookie_header),
        "Session cookie must have SameSite=Lax; got: {}",
        cookie_header
    );

    // Secure should match config
    let has_secure = has_cookie_flag_attribute(cookie_header, "secure");
    assert_eq!(
        has_secure, expect_secure,
        "Session cookie Secure flag mismatch: expected={}, got={}; header: {}",
        expect_secure, has_secure, cookie_header
    );
}

#[tokio::test]
#[serial]
async fn consume_legacy_alias_returns_404() -> Result<()> {
    let state = test_state_with_accounts()?;
    let app = app(state);

    // 1. GET (Legacy Alias)
    let req_get = Request::get("/auth/login/consume?token=any").body(body::Body::empty())?;
    let res_get = app.clone().oneshot(req_get).await?;
    assert_eq!(res_get.status(), StatusCode::NOT_FOUND);

    // 2. POST (Legacy Alias)
    let req_post = Request::post("/auth/login/consume")
        .header("Content-Type", "application/x-www-form-urlencoded")
        .body(body::Body::from("token=any&nonce=any"))?;
    let res_post = app.clone().oneshot(req_post).await?;
    assert_eq!(res_post.status(), StatusCode::NOT_FOUND);

    Ok(())
}

#[tokio::test]
#[serial]
async fn magic_link_consume_origin_null_succeeds_through_security_middleware() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());

    let stale_session = create_session(&state, "u1", Some("stale-device")).await;
    let token = state.tokens.create("u1@example.com".to_string());
    let app = app_with_security(state.clone());

    let uri = format!("/auth/magic-link/consume?token={}", token);
    let req_get = Request::get(&uri)
        .header("Host", "localhost")
        .body(body::Body::empty())?;
    let res_get = app.clone().oneshot(req_get).await?;
    assert_eq!(res_get.status(), StatusCode::OK);

    let nonce_val = extract_cookie_value(res_get.headers(), NONCE_COOKIE_NAME)
        .context("GET consume must set nonce cookie")?;
    let (stored_hash, nonce) = nonce_val.split_once('.').context("invalid nonce format")?;
    assert_eq!(stored_hash, hash_token(&token));

    let body_str = format!("token={}&nonce={}", token, nonce);
    let req_post = Request::post("/auth/magic-link/consume")
        .header("Content-Type", "application/x-www-form-urlencoded")
        .header("Host", "localhost")
        .header("Origin", "null")
        .header(
            "Cookie",
            format!(
                "{}={}; {}={}",
                SESSION_COOKIE_NAME, stale_session.id, NONCE_COOKIE_NAME, nonce_val
            ),
        )
        .body(body::Body::from(body_str))?;
    let res_post = app.oneshot(req_post).await?;

    assert_eq!(res_post.status(), StatusCode::SEE_OTHER);
    assert_eq!(res_post.headers().get("location").unwrap(), "/");

    let session_cookie = extract_set_cookie_header(res_post.headers(), SESSION_COOKIE_NAME)
        .context("valid magic-link consume must create a session cookie")?;
    assert_persistent_session_cookie(&session_cookie, true);

    let nonce_cleanup = extract_set_cookie_header(res_post.headers(), NONCE_COOKIE_NAME)
        .context("session-cookie normalization must preserve nonce cleanup")?;
    assert_eq!(
        cookie_attribute_value(&nonce_cleanup, "max-age"),
        Some("0"),
        "nonce cleanup must remain an immediate deletion cookie"
    );

    let session_id = extract_cookie_value(res_post.headers(), SESSION_COOKIE_NAME)
        .context("valid magic-link consume must create a session cookie")?;
    assert!(
        get_session(&state, &session_id).await.is_some(),
        "created session id must exist in the session backend"
    );

    Ok(())
}

#[tokio::test]
#[serial]
async fn magic_link_consume_origin_null_invalid_token_does_not_create_session() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());

    let stale_session = create_session(&state, "u1", Some("stale-device")).await;
    let invalid_token = "invalid-token-for-origin-null-proof";
    let nonce = "valid-nonce-for-invalid-token-proof";
    let nonce_val = format!("{}.{}", hash_token(invalid_token), nonce);
    let app = app_with_security(state);

    let body_str = format!("token={}&nonce={}", invalid_token, nonce);
    let req_post = Request::post("/auth/magic-link/consume")
        .header("Content-Type", "application/x-www-form-urlencoded")
        .header("Host", "localhost")
        .header("Origin", "null")
        .header(
            "Cookie",
            format!(
                "{}={}; {}={}",
                SESSION_COOKIE_NAME, stale_session.id, NONCE_COOKIE_NAME, nonce_val
            ),
        )
        .body(body::Body::from(body_str))?;
    let res_post = app.oneshot(req_post).await?;

    assert_eq!(res_post.status(), StatusCode::SEE_OTHER);
    assert_eq!(
        res_post.headers().get("location").unwrap(),
        "/login?error=invalid_token"
    );
    assert!(
        extract_cookie_value(res_post.headers(), SESSION_COOKIE_NAME).is_none(),
        "invalid token must not create a session cookie"
    );

    Ok(())
}

#[tokio::test]
#[serial]
async fn magic_link_consume_origin_null_invalid_nonce_does_not_create_session() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());

    let stale_session = create_session(&state, "u1", Some("stale-device")).await;
    let token = state.tokens.create("u1@example.com".to_string());
    let nonce_val = format!("{}.{}", hash_token(&token), "stored-nonce");
    let app = app_with_security(state.clone());

    let body_str = format!("token={}&nonce={}", token, "submitted-different-nonce");
    let req_post = Request::post("/auth/magic-link/consume")
        .header("Content-Type", "application/x-www-form-urlencoded")
        .header("Host", "localhost")
        .header("Origin", "null")
        .header(
            "Cookie",
            format!(
                "{}={}; {}={}",
                SESSION_COOKIE_NAME, stale_session.id, NONCE_COOKIE_NAME, nonce_val
            ),
        )
        .body(body::Body::from(body_str))?;
    let res_post = app.oneshot(req_post).await?;

    assert_eq!(res_post.status(), StatusCode::SEE_OTHER);
    assert_eq!(
        res_post.headers().get("location").unwrap(),
        "/login?error=invalid_token"
    );
    assert!(
        extract_cookie_value(res_post.headers(), SESSION_COOKIE_NAME).is_none(),
        "invalid nonce must not create a session cookie"
    );
    assert!(
        state.tokens.peek(&token).is_some(),
        "invalid nonce must not consume the one-time token"
    );

    Ok(())
}

#[tokio::test]
#[serial]
async fn consume_login_flow_succeeds() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());

    // Create a valid token
    let token = state.tokens.create("u1@example.com".to_string());
    let app = app(state);

    // 1. GET (Confirm Page)
    let uri = format!("/auth/magic-link/consume?token={}", token);
    let req_get = Request::get(&uri).body(body::Body::empty())?;
    let res_get = app.clone().oneshot(req_get).await?;

    assert_eq!(res_get.status(), StatusCode::OK);
    // Should NOT set session cookie yet
    let set_cookies = res_get.headers().get_all("set-cookie");
    for c in set_cookies.iter() {
        assert!(!c.to_str()?.contains(SESSION_COOKIE_NAME));
    }
    // Should set nonce cookie
    let nonce_val = extract_cookie_value(res_get.headers(), NONCE_COOKIE_NAME)
        .context("missing nonce cookie")?;

    // Extract nonce from cookie (hash.nonce)
    let (_, nonce) = nonce_val.split_once('.').context("invalid nonce format")?;
    let nonce = nonce.to_string(); // Keep only nonce part for form

    // 2. POST (Consume)
    let body_str = format!("token={}&nonce={}", token, nonce);
    let req_post = Request::post("/auth/magic-link/consume")
        .header("Content-Type", "application/x-www-form-urlencoded")
        .header("Cookie", format!("{}={}", NONCE_COOKIE_NAME, nonce_val))
        .body(body::Body::from(body_str))?;

    let res_post = app.oneshot(req_post).await?;

    assert_eq!(res_post.status(), StatusCode::SEE_OTHER);
    assert_eq!(res_post.headers().get("location").unwrap(), "/");

    let set_cookies = res_post.headers().get_all("set-cookie");
    let session_cookie_present = set_cookies
        .iter()
        .any(|c| c.to_str().unwrap_or("").contains(SESSION_COOKIE_NAME));
    assert!(
        session_cookie_present,
        "Session cookie not found in response"
    );

    // Ensure nonce cookie is cleared (Max-Age=0 or Expires)
    let nonce_cleared = set_cookies.iter().any(|c| {
        let val = c.to_str().unwrap_or("");
        val.contains(NONCE_COOKIE_NAME) && (val.contains("Max-Age=0") || val.contains("Expires="))
    });
    assert!(nonce_cleared, "Nonce cookie should be cleared in response");

    Ok(())
}

#[tokio::test]
#[serial]
async fn consume_login_fails_invalid_token() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());

    let app = app(state);

    let req = Request::get("/auth/magic-link/consume?token=invalid_token_123")
        .body(body::Body::empty())?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::SEE_OTHER);
    assert_eq!(
        res.headers().get("location").unwrap(),
        "/login?error=invalid_token"
    );

    Ok(())
}

#[tokio::test]
#[serial]
async fn consume_login_fails_reuse() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());

    let token = state.tokens.create("u1@example.com".to_string());
    let app = app(state);

    // 1. GET (Confirm Page)
    let uri = format!("/auth/magic-link/consume?token={}", token);
    let req_get = Request::get(&uri).body(body::Body::empty())?;
    let res_get = app.clone().oneshot(req_get).await?;

    assert_eq!(res_get.status(), StatusCode::OK);

    let nonce_val = extract_cookie_value(res_get.headers(), NONCE_COOKIE_NAME)
        .context("missing nonce cookie")?;

    // Extract nonce from cookie (hash.nonce)
    let (_, nonce) = nonce_val.split_once('.').context("invalid nonce format")?;
    let nonce = nonce.to_string(); // Keep only nonce part for form

    // 2. POST (Consume)
    let body_str = format!("token={}&nonce={}", token, nonce);
    let req_post = Request::post("/auth/magic-link/consume")
        .header("Content-Type", "application/x-www-form-urlencoded")
        .header("Cookie", format!("{}={}", NONCE_COOKIE_NAME, nonce_val))
        .body(body::Body::from(body_str))?;

    let res_post = app.clone().oneshot(req_post).await?;

    assert_eq!(res_post.status(), StatusCode::SEE_OTHER);
    assert_eq!(res_post.headers().get("location").unwrap(), "/");

    // 3. GET Reuse (Should fail)
    let req_reuse = Request::get(&uri).body(body::Body::empty())?;
    let res_reuse = app.oneshot(req_reuse).await?;

    assert_eq!(res_reuse.status(), StatusCode::SEE_OTHER);
    assert_eq!(
        res_reuse.headers().get("location").unwrap(),
        "/login?error=invalid_token"
    );

    Ok(())
}

#[tokio::test]
#[serial]
async fn consume_login_fails_expired_token() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());

    // Create an expired token (expired 1 second ago)
    let token = state
        .tokens
        .create_with_expiry("u1@example.com".to_string(), chrono::Duration::seconds(-1));

    let app = app(state);

    // GET should fail
    let uri = format!("/auth/magic-link/consume?token={}", token);
    let req = Request::get(uri).body(body::Body::empty())?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::SEE_OTHER);
    assert_eq!(
        res.headers().get("location").unwrap(),
        "/login?error=invalid_token"
    );

    Ok(())
}

#[tokio::test]
#[serial]
async fn consume_login_fails_bad_nonce() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());

    let token = state.tokens.create("u1@example.com".to_string());
    let app = app(state.clone());

    // Mismatch nonce
    let nonce_cookie = "nonce1";
    let nonce_form = "nonce2";
    let token_hash = hash_token(&token);
    let cookie_val = format!("{}.{}", token_hash, nonce_cookie);
    let body_str = format!("token={}&nonce={}", token, nonce_form);

    let req = Request::post("/auth/magic-link/consume")
        .header("Content-Type", "application/x-www-form-urlencoded")
        .header("Cookie", format!("{}={}", NONCE_COOKIE_NAME, cookie_val))
        .body(body::Body::from(body_str))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::SEE_OTHER);
    assert_eq!(
        res.headers().get("location").unwrap(),
        "/login?error=invalid_token"
    );
    assert!(
        res.headers()
            .get("content-type")
            .and_then(|value| value.to_str().ok())
            .is_none_or(|value| !value.starts_with("text/html")),
        "invalid-nonce POST must not render an HTML document"
    );
    // Token should NOT be consumed
    assert!(state.tokens.peek(&token).is_some());

    Ok(())
}

#[tokio::test]
#[serial]
async fn consume_login_post_expired_token_is_non_document_redirect() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());

    let token = state
        .tokens
        .create_with_expiry("u1@example.com".to_string(), chrono::Duration::seconds(-1));
    let nonce = "expired-token-post-nonce";
    let cookie_val = format!("{}.{}", hash_token(&token), nonce);
    let body_str = format!("token={}&nonce={}", token, nonce);
    let app = app(state);

    let req = Request::post("/auth/magic-link/consume")
        .header("Content-Type", "application/x-www-form-urlencoded")
        .header("Cookie", format!("{}={}", NONCE_COOKIE_NAME, cookie_val))
        .body(body::Body::from(body_str))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::SEE_OTHER);
    assert_eq!(
        res.headers().get("location").unwrap(),
        "/login?error=invalid_token"
    );
    assert!(
        res.headers()
            .get("content-type")
            .and_then(|value| value.to_str().ok())
            .is_none_or(|value| !value.starts_with("text/html")),
        "expired-token POST must not render an HTML document"
    );

    Ok(())
}

#[tokio::test]
#[serial]
async fn consume_login_fails_bad_token_binding() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());

    let token = state.tokens.create("u1@example.com".to_string());
    let app = app(state.clone());

    let nonce = "nonce1";
    // A fabricated token is sufficient to prove the cookie/form binding mismatch.
    // Creating a second real token for the same address would intentionally
    // invalidate `token`, because the newest sign-in request is authoritative.
    let token_hash = hash_token("different-token");
    let cookie_val = format!("{}.{}", token_hash, nonce);
    let body_str = format!("token={}&nonce={}", token, nonce); // Correct token in form

    let req = Request::post("/auth/magic-link/consume")
        .header("Content-Type", "application/x-www-form-urlencoded")
        .header("Cookie", format!("{}={}", NONCE_COOKIE_NAME, cookie_val))
        .body(body::Body::from(body_str))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::SEE_OTHER);
    assert_eq!(
        res.headers().get("location").unwrap(),
        "/login?error=invalid_token"
    );
    // Token should NOT be consumed
    assert!(state.tokens.peek(&token).is_some());

    Ok(())
}

#[tokio::test]
#[serial]
async fn request_login_provisioning_disabled_for_unknown() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());
    state.config.auth_auto_provision = false;

    let app = app(state.clone());

    let email = "newuser@example.com";
    let req = Request::post("/auth/magic-link/request")
        .header("Content-Type", "application/json")
        .body(body::Body::from(format!(r#"{{"email":"{}"}}"#, email)))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);

    // Should NOT have created an account
    let accounts = state.accounts.read().await;
    let found = accounts
        .values()
        .any(|acc| acc.email.as_deref() == Some(email));
    assert!(!found);

    Ok(())
}

#[tokio::test]
#[serial]
async fn request_login_provisioning_enabled_success() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let _env = weltgewebe_api::test_helpers::EnvGuard::set(
        "GEWEBE_IN_DIR",
        tmp.path().to_str().expect("tempdir path is valid utf-8"),
    );

    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());
    state.config.auth_auto_provision = true;
    state.config.auth_allow_emails = Some(vec!["allowed@example.com".to_string()]);

    let app = app(state.clone());

    let email = "allowed@example.com";
    let req = Request::post("/auth/magic-link/request")
        .header("Content-Type", "application/json")
        .body(body::Body::from(format!(r#"{{"email":"{}"}}"#, email)))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);

    // Should have created an account
    {
        let accounts = state.accounts.read().await;
        let found = accounts
            .values()
            .find(|acc| acc.email.as_deref() == Some(email));
        assert!(found.is_some());
        let acc = found.unwrap();
        assert_eq!(acc.role, Role::Gast);
        // Verify auto-provisioning privacy invariants
        assert_eq!(acc.public.title, "Garnrolle");
        assert_eq!(acc.public.kind, "garnrolle");
        assert_eq!(
            acc.public.map_state,
            weltgewebe_api::routes::accounts::GarnrolleMapState::NotOnMap
        );
        assert!(acc.public.public_pos.is_none());
    }

    // Should have created a token
    // We can't easily peek by email with current TokenStore public API in tests (peek takes token string)
    // But we can rely on the fact that if account was created, token generation follows.
    // However, let's verify if we can check token count or something?
    // TokenStore::create returns the token string. We don't have it here.
    // But we know it succeeded if we see the account.

    Ok(())
}

#[tokio::test]
#[serial]
async fn request_login_provisioning_enabled_denied() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());
    state.config.auth_auto_provision = true;
    state.config.auth_allow_emails = Some(vec!["allowed@example.com".to_string()]);

    let app = app(state.clone());

    let email = "denied@example.com";
    let req = Request::post("/auth/magic-link/request")
        .header("Content-Type", "application/json")
        .body(body::Body::from(format!(r#"{{"email":"{}"}}"#, email)))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);

    // Should NOT have created an account
    {
        let accounts = state.accounts.read().await;
        let found = accounts
            .values()
            .any(|acc| acc.email.as_deref() == Some(email));
        assert!(!found);
    }

    Ok(())
}

#[tokio::test]
#[serial]
async fn request_login_provisioning_enabled_domain_allowlist() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let _env = weltgewebe_api::test_helpers::EnvGuard::set(
        "GEWEBE_IN_DIR",
        tmp.path().to_str().expect("tempdir path is valid utf-8"),
    );

    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());
    state.config.auth_auto_provision = true;
    state.config.auth_allow_email_domains = Some(vec!["allowed.com".to_string()]);

    let app = app(state.clone());

    let email = "user@allowed.com";
    let req = Request::post("/auth/magic-link/request")
        .header("Content-Type", "application/json")
        .body(body::Body::from(format!(r#"{{"email":"{}"}}"#, email)))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);

    // Should have created an account
    {
        let accounts = state.accounts.read().await;
        let found = accounts
            .values()
            .any(|acc| acc.email.as_deref() == Some(email));
        assert!(found);
    }

    Ok(())
}

#[tokio::test]
#[serial]
async fn request_login_provisioning_domain_allowlist_rejects_multi_at_attack() -> Result<()> {
    // Attack vector: attacker@allowed.com@evil.com
    // Should NOT match "allowed.com"
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());
    state.config.auth_auto_provision = true;
    state.config.auth_allow_email_domains = Some(vec!["allowed.com".to_string()]);

    let app = app(state.clone());

    let email = "attacker@allowed.com@evil.com";
    let req = Request::post("/auth/magic-link/request")
        .header("Content-Type", "application/json")
        .body(body::Body::from(format!(r#"{{"email":"{}"}}"#, email)))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);

    // Verify account NOT created
    // We check against the normalized email because if it were created, it would be normalized.
    let email_norm = email.trim().to_ascii_lowercase();
    {
        let accounts = state.accounts.read().await;
        let found = accounts
            .values()
            .any(|acc| acc.email.as_deref() == Some(email_norm.as_str()));
        assert!(
            !found,
            "Account should not be created for multi-@ attack email"
        );
    }

    Ok(())
}

#[tokio::test]
#[serial]
async fn request_login_provisioning_empty_domain_rejected() -> Result<()> {
    // Edge case: "user@" (empty domain)
    // Config: "allowed.com" (and potentially empty strings filtered out)
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());
    state.config.auth_auto_provision = true;
    state.config.auth_allow_email_domains = Some(vec!["allowed.com".to_string()]);

    let app = app(state.clone());

    let email = "user@";
    let req = Request::post("/auth/magic-link/request")
        .header("Content-Type", "application/json")
        .body(body::Body::from(format!(r#"{{"email":"{}"}}"#, email)))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);

    // Verify account NOT created
    let email_norm = email.trim().to_ascii_lowercase();
    {
        let accounts = state.accounts.read().await;
        let found = accounts
            .values()
            .any(|acc| acc.email.as_deref() == Some(email_norm.as_str()));
        assert!(!found, "Account should not be created for empty domain");
    }

    Ok(())
}

#[tokio::test]
#[serial]
async fn request_login_provisioning_email_normalization_works() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let _env = weltgewebe_api::test_helpers::EnvGuard::set(
        "GEWEBE_IN_DIR",
        tmp.path().to_str().expect("tempdir path is valid utf-8"),
    );

    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());
    state.config.auth_auto_provision = true;
    // Config uses lowercase
    state.config.auth_allow_emails = Some(vec!["allowed@example.com".to_string()]);

    let app = app(state.clone());

    // Input has mixed case; semantic normalization remains lowercase.
    let input_email = "Allowed@EXAMPLE.com";
    let normalized_email = "allowed@example.com";

    let req = Request::post("/auth/magic-link/request")
        .header("Content-Type", "application/json")
        .body(body::Body::from(format!(
            r#"{{"email":"{}"}}"#,
            input_email
        )))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);

    // Verify account created with normalized email
    {
        let accounts = state.accounts.read().await;
        let found = accounts
            .values()
            .find(|acc| acc.email.as_deref() == Some(normalized_email));
        assert!(
            found.is_some(),
            "Account should be created with normalized email"
        );
    }

    Ok(())
}

#[tokio::test]
#[serial]
async fn request_login_mixed_case_stored_email_no_duplicate() -> Result<()> {
    // Regression test: eq_ignore_ascii_case in request_login's account lookup ensures that a
    // pre-existing account whose stored email has Mixed-Case (e.g. legacy data) is found when a
    // login request arrives with the normalised lowercase form.
    // Without the fix the lookup would miss the stored account and provision_account would create a
    // duplicate entry, because that path's own collision check was also case-sensitive before.
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());
    state.config.auth_auto_provision = true;
    // Allow the normalized email so the provisioning path would be entered if the lookup fails.
    state.config.auth_allow_emails = Some(vec!["user@mixedcase.example".to_string()]);

    // Insert a legacy account with a Mixed-Case stored email.
    let mixed_id = "legacy-mixed-case".to_string();
    {
        let mut accounts = state.accounts.write().await;
        accounts.insert(AccountInternal {
            public: AccountPublic {
                id: mixed_id.clone(),
                kind: "garnrolle".to_string(),
                title: "Mixed Case Legacy".to_string(),
                summary: None,
                public_pos: None,
                map_state: weltgewebe_api::routes::accounts::GarnrolleMapState::NotOnMap,
                radius_m: 0,
                disabled: false,
                tags: vec![],
            },
            role: Role::Gast,
            email: Some("User@MixedCase.Example".to_string()), // Mixed-Case stored
            webauthn_user_id: uuid::Uuid::new_v4(),
        });
    }

    let account_count_before = state.accounts.read().await.len();

    let app = app(state.clone());
    let req = Request::post("/auth/magic-link/request")
        .header("Content-Type", "application/json")
        .body(body::Body::from(r#"{"email":"user@mixedcase.example"}"#))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);

    let accounts = state.accounts.read().await;

    // No duplicate must have been created: the case-insensitive lookup finds the existing
    // account, so provision_account is never called.
    assert_eq!(
        accounts.len(),
        account_count_before,
        "No new account should be created when the stored Mixed-Case email matches case-insensitively"
    );

    // The original account must be the sole case-insensitive match for the request email.
    let matches: Vec<_> = accounts
        .values()
        .filter(|acc| {
            acc.email
                .as_deref()
                .map(|e| e.eq_ignore_ascii_case("user@mixedcase.example"))
                .unwrap_or(false)
        })
        .collect();
    assert_eq!(
        matches.len(),
        1,
        "Exactly one account should match the email case-insensitively"
    );
    assert_eq!(
        matches[0].public.id, mixed_id,
        "The matched account must be the original Mixed-Case account, not a new one"
    );

    Ok(())
}

#[tokio::test]
async fn session_endpoint_unauthenticated() -> Result<()> {
    let state = test_state_with_accounts()?;
    let app = Router::new()
        .merge(weltgewebe_api::routes::api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .with_state(state);

    let req = Request::get("/auth/session")
        .header("Host", "localhost")
        .body(body::Body::empty())?;

    let res = app.oneshot(req).await?;

    let status = res.status();
    let body_bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    if status != StatusCode::OK {
        println!("FAIL BODY: {:?}", String::from_utf8_lossy(&body_bytes));
    }
    assert_eq!(status, StatusCode::OK);

    let body_json: serde_json::Value = serde_json::from_slice(&body_bytes)?;

    assert_eq!(body_json["authenticated"], false);
    assert!(
        body_json.get("expires_at").is_none(),
        "expires_at must be completely omitted"
    );
    assert!(body_json.get("device_id").is_none());

    Ok(())
}

#[tokio::test]
async fn session_endpoint_authenticated() -> Result<()> {
    let state = test_state_with_accounts()?;
    // Mock a valid session for user u1
    let session = create_session(&state, "u1", None).await;
    let session_id = session.id;

    let app = Router::new()
        .merge(weltgewebe_api::routes::api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .with_state(state);

    let req = Request::get("/auth/session")
        .header("Host", "localhost")
        .header(
            "Cookie",
            format!(
                "{}={}",
                weltgewebe_api::routes::auth::SESSION_COOKIE_NAME,
                session_id
            ),
        )
        .body(body::Body::empty())?;

    let res = app.oneshot(req).await?;

    let status = res.status();
    let body_bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    if status != StatusCode::OK {
        println!("FAIL BODY: {:?}", String::from_utf8_lossy(&body_bytes));
    }
    assert_eq!(status, StatusCode::OK);

    let body_json: serde_json::Value = serde_json::from_slice(&body_bytes)?;

    assert_eq!(body_json["authenticated"], true);
    assert!(body_json.get("expires_at").is_some());
    assert_eq!(body_json["device_id"].as_str().unwrap(), session.device_id);

    Ok(())
}

#[tokio::test]
#[serial]
async fn test_session_refresh_success() -> Result<()> {
    let _guard = weltgewebe_api::test_helpers::EnvGuard::set("AUTH_DEV_LOGIN", "1");
    let _guard_cookie = weltgewebe_api::test_helpers::EnvGuard::set("AUTH_COOKIE_SECURE", "1");
    let mut account_map = AccountStore::new();
    let account = AccountInternal {
        public: AccountPublic {
            id: "u-admin".to_string(),
            kind: "garnrolle".to_string(),
            title: "User".to_string(),
            summary: None,
            public_pos: None,
            map_state: weltgewebe_api::routes::accounts::GarnrolleMapState::NotOnMap,
            radius_m: 0,
            disabled: false,
            tags: vec![],
        },
        role: Role::Admin,
        email: Some("u1@example.com".to_string()),
        webauthn_user_id: uuid::Uuid::new_v4(),
    };
    account_map.insert(account);

    let mut state = test_state()?;
    state.accounts = Arc::new(RwLock::new(account_map));
    let app = Router::new()
        .merge(api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .layer(MockConnectInfo(
            "127.0.0.1:8080".parse::<SocketAddr>().unwrap(),
        ))
        .layer(axum::middleware::from_fn(
            weltgewebe_api::middleware::csrf::require_csrf,
        ))
        .with_state(state.clone());

    // 1. Login to get a session cookie
    let req = Request::post("/auth/dev/login")
        .header("Content-Type", "application/json")
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .body(body::Body::from(r#"{"account_id": "u-admin"}"#))?;

    let res = app.clone().oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);

    let set_cookie = res.headers().get("Set-Cookie").unwrap().to_str().unwrap();
    let session_cookie = set_cookie.split(';').next().unwrap();
    assert!(set_cookie.contains("Secure"));
    assert!(set_cookie.contains("HttpOnly"));
    assert!(set_cookie.contains("SameSite=Lax"));

    // 2. Refresh session
    let req_refresh = Request::post("/auth/session/refresh")
        .header("Cookie", session_cookie)
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .body(body::Body::empty())?;

    let res_refresh = app.clone().oneshot(req_refresh).await?;
    assert_eq!(res_refresh.status(), StatusCode::OK);

    // Verify new cookie is set
    let refresh_set_cookie = res_refresh
        .headers()
        .get("Set-Cookie")
        .unwrap()
        .to_str()
        .unwrap();
    let new_session_cookie = refresh_set_cookie.split(';').next().unwrap().to_string();
    assert!(refresh_set_cookie.contains("Secure"));
    assert!(refresh_set_cookie.contains("HttpOnly"));
    assert!(refresh_set_cookie.contains("SameSite=Lax"));
    assert_ne!(
        session_cookie, new_session_cookie,
        "Session cookie should be rotated"
    );

    let body_bytes = body::to_bytes(res_refresh.into_body(), usize::MAX).await?;
    let body: serde_json::Value = serde_json::from_slice(&body_bytes).unwrap();
    assert_eq!(body["authenticated"], true);
    assert!(body["expires_at"].is_string());
    assert!(
        body["device_id"].is_string(),
        "session refresh response must include device_id"
    );

    // 3. New cookie should be valid
    let req_new = Request::get("/auth/session")
        .header("Cookie", &new_session_cookie)
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .body(body::Body::empty())?;

    let res_new = app.clone().oneshot(req_new).await?;
    let new_body_bytes = body::to_bytes(res_new.into_body(), usize::MAX).await?;
    let new_body: serde_json::Value = serde_json::from_slice(&new_body_bytes).unwrap();
    assert_eq!(new_body["authenticated"], true);
    assert!(new_body["expires_at"].is_string());

    // 4. Old cookie should now be invalid

    let req_old = Request::get("/auth/session")
        .header("Cookie", session_cookie)
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .body(body::Body::empty())?;

    let res_old = app.clone().oneshot(req_old).await?;
    let old_body_bytes = body::to_bytes(res_old.into_body(), usize::MAX).await?;
    let old_body: serde_json::Value = serde_json::from_slice(&old_body_bytes).unwrap();
    assert_eq!(old_body["authenticated"], false);

    Ok(())
}

#[tokio::test]
#[serial]
async fn test_session_refresh_invalid_token() -> Result<()> {
    let state = test_state()?;
    let app = Router::new()
        .merge(api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .layer(MockConnectInfo(
            "127.0.0.1:8080".parse::<SocketAddr>().unwrap(),
        ))
        .layer(axum::middleware::from_fn(
            weltgewebe_api::middleware::csrf::require_csrf,
        ))
        .with_state(state.clone());

    // Refresh with no cookie
    let req = Request::post("/auth/session/refresh")
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .body(body::Body::empty())?;

    let res = app.clone().oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::UNAUTHORIZED);

    let body_bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let body: serde_json::Value = serde_json::from_slice(&body_bytes).unwrap();
    assert_eq!(body["error"], "SESSION_EXPIRED");

    // Refresh with invalid cookie
    let req2 = Request::post("/auth/session/refresh")
        .header("Cookie", "weltgewebe_session=invalid-session-id")
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .body(body::Body::empty())?;

    let res2 = app.clone().oneshot(req2).await?;
    assert_eq!(res2.status(), StatusCode::UNAUTHORIZED);

    Ok(())
}

#[tokio::test]
#[serial]
async fn test_session_refresh_csrf_rejected() -> Result<()> {
    let _guard = weltgewebe_api::test_helpers::EnvGuard::set("AUTH_DEV_LOGIN", "1");
    let mut account_map = AccountStore::new();
    let account = AccountInternal {
        public: AccountPublic {
            id: "u-admin".to_string(),
            kind: "garnrolle".to_string(),
            title: "User".to_string(),
            summary: None,
            public_pos: None,
            map_state: weltgewebe_api::routes::accounts::GarnrolleMapState::NotOnMap,
            radius_m: 0,
            disabled: false,
            tags: vec![],
        },
        role: Role::Admin,
        email: Some("u1@example.com".to_string()),
        webauthn_user_id: uuid::Uuid::new_v4(),
    };
    account_map.insert(account);

    let mut state = test_state()?;
    state.accounts = Arc::new(RwLock::new(account_map));
    let app = Router::new()
        .merge(api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .layer(axum::middleware::from_fn(
            weltgewebe_api::middleware::csrf::require_csrf,
        ))
        .layer(MockConnectInfo(
            "127.0.0.1:8080".parse::<SocketAddr>().unwrap(),
        ))
        .with_state(state.clone());

    // 1. Login to get a session cookie
    let req = Request::post("/auth/dev/login")
        .header("Content-Type", "application/json")
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .body(body::Body::from(r#"{"account_id": "u-admin"}"#))?;

    let res = app.clone().oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);

    let set_cookie = res.headers().get("Set-Cookie").unwrap().to_str().unwrap();
    let session_cookie = set_cookie.split(';').next().unwrap();

    // 2. Refresh session without Origin/Referer (CSRF failure)
    let req_refresh = Request::post("/auth/session/refresh")
        .header("Cookie", session_cookie)
        .header("Host", "localhost")
        .body(body::Body::empty())?;

    let res_refresh = app.clone().oneshot(req_refresh).await?;
    assert_eq!(res_refresh.status(), StatusCode::FORBIDDEN); // CSRF blocks it

    Ok(())
}

#[tokio::test]
#[serial]
async fn test_session_refresh_account_disabled() -> Result<()> {
    let _guard = weltgewebe_api::test_helpers::EnvGuard::set("AUTH_DEV_LOGIN", "1");
    let mut account_map = AccountStore::new();
    let mut account = AccountInternal {
        public: AccountPublic {
            id: "u-admin".to_string(),
            kind: "garnrolle".to_string(),
            title: "User".to_string(),
            summary: None,
            public_pos: None,
            map_state: weltgewebe_api::routes::accounts::GarnrolleMapState::NotOnMap,
            radius_m: 0,
            disabled: false,
            tags: vec![],
        },
        role: Role::Admin,
        email: Some("u1@example.com".to_string()),
        webauthn_user_id: uuid::Uuid::new_v4(),
    };
    account_map.insert(account.clone());

    let mut state = test_state()?;
    state.accounts = Arc::new(RwLock::new(account_map.clone()));
    let app = Router::new()
        .merge(api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .layer(MockConnectInfo(
            "127.0.0.1:8080".parse::<SocketAddr>().unwrap(),
        ))
        .with_state(state.clone());

    // 1. Login to get a session cookie
    let req = Request::post("/auth/dev/login")
        .header("Content-Type", "application/json")
        .body(body::Body::from(r#"{"account_id": "u-admin"}"#))?;

    let res = app.clone().oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);

    let set_cookie = res.headers().get("Set-Cookie").unwrap().to_str().unwrap();
    let session_cookie = set_cookie.split(';').next().unwrap().to_string();

    // 2. Disable account
    account.public.disabled = true;
    state.accounts.write().await.insert(account);

    // 3. Refresh session (should fail)
    let req_refresh = Request::post("/auth/session/refresh")
        .header("Cookie", &session_cookie)
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .body(body::Body::empty())?;

    let res_refresh = app.clone().oneshot(req_refresh).await?;
    assert_eq!(res_refresh.status(), StatusCode::UNAUTHORIZED);

    let refresh_set_cookie = res_refresh
        .headers()
        .get("Set-Cookie")
        .unwrap()
        .to_str()
        .unwrap();
    assert!(
        refresh_set_cookie.contains("Max-Age=0"),
        "Cookie should be deleted"
    );

    Ok(())
}

#[tokio::test]
#[serial]
async fn test_logout() -> Result<()> {
    let _guard = weltgewebe_api::test_helpers::EnvGuard::set("AUTH_DEV_LOGIN", "1");
    let mut account_map = AccountStore::new();
    let account = AccountInternal {
        public: AccountPublic {
            id: "u-admin".to_string(),
            kind: "garnrolle".to_string(),
            title: "User".to_string(),
            summary: None,
            public_pos: None,
            map_state: weltgewebe_api::routes::accounts::GarnrolleMapState::NotOnMap,
            radius_m: 0,
            disabled: false,
            tags: vec![],
        },
        role: Role::Admin,
        email: Some("u1@example.com".to_string()),
        webauthn_user_id: uuid::Uuid::new_v4(),
    };
    account_map.insert(account);

    let mut state = test_state()?;
    state.accounts = Arc::new(RwLock::new(account_map));
    let app = Router::new()
        .merge(api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .layer(MockConnectInfo(
            "127.0.0.1:8080".parse::<SocketAddr>().unwrap(),
        ))
        .layer(axum::middleware::from_fn(
            weltgewebe_api::middleware::csrf::require_csrf,
        ))
        .with_state(state.clone());

    // 1. Login to get a session cookie
    let req = Request::post("/auth/dev/login")
        .header("Content-Type", "application/json")
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .body(body::Body::from(r#"{"account_id": "u-admin"}"#))?;

    let res = app.clone().oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);

    let set_cookie = res.headers().get("Set-Cookie").unwrap().to_str().unwrap();
    let session_cookie = set_cookie.split(';').next().unwrap();

    // 2. Logout
    let req_logout = Request::post("/auth/logout")
        .header("Cookie", session_cookie)
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .body(body::Body::empty())?;

    let res_logout = app.clone().oneshot(req_logout).await?;
    assert_eq!(res_logout.status(), StatusCode::OK);

    let logout_set_cookie = res_logout
        .headers()
        .get("Set-Cookie")
        .unwrap()
        .to_str()
        .unwrap();
    assert!(
        logout_set_cookie.contains("Max-Age=0"),
        "Cookie should be deleted"
    );

    // 3. Old cookie should now be invalid
    let req_old = Request::get("/auth/session")
        .header("Cookie", session_cookie)
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .body(body::Body::empty())?;

    let res_old = app.clone().oneshot(req_old).await?;
    let old_body_bytes = body::to_bytes(res_old.into_body(), usize::MAX).await?;
    let old_body: serde_json::Value = serde_json::from_slice(&old_body_bytes).unwrap();
    assert_eq!(old_body["authenticated"], false);

    Ok(())
}

#[tokio::test]
#[serial]
async fn test_logout_all_requires_step_up_and_preserves_sessions() -> Result<()> {
    let _guard = weltgewebe_api::test_helpers::EnvGuard::set("AUTH_DEV_LOGIN", "1");
    let mut account_map = AccountStore::new();
    let account = AccountInternal {
        public: AccountPublic {
            id: "u-admin".to_string(),
            kind: "garnrolle".to_string(),
            title: "User".to_string(),
            summary: None,
            public_pos: None,
            map_state: weltgewebe_api::routes::accounts::GarnrolleMapState::NotOnMap,
            radius_m: 0,
            disabled: false,
            tags: vec![],
        },
        role: Role::Admin,
        email: Some("u1@example.com".to_string()),
        webauthn_user_id: uuid::Uuid::new_v4(),
    };
    account_map.insert(account);

    let mut state = test_state()?;
    state.accounts = Arc::new(RwLock::new(account_map));
    let app = Router::new()
        .merge(api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .layer(MockConnectInfo(
            "127.0.0.1:8080".parse::<SocketAddr>().unwrap(),
        ))
        .layer(axum::middleware::from_fn(
            weltgewebe_api::middleware::csrf::require_csrf,
        ))
        .with_state(state.clone());

    // 1. Login once to get session 1
    let req1 = Request::post("/auth/dev/login")
        .header("Content-Type", "application/json")
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .body(body::Body::from(r#"{"account_id": "u-admin"}"#))?;

    let res1 = app.clone().oneshot(req1).await?;
    assert_eq!(res1.status(), StatusCode::OK);
    let set_cookie1 = res1.headers().get("Set-Cookie").unwrap().to_str().unwrap();
    let session_cookie1 = set_cookie1.split(';').next().unwrap().to_string();

    // 2. Login again to get session 2
    let req2 = Request::post("/auth/dev/login")
        .header("Content-Type", "application/json")
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .body(body::Body::from(r#"{"account_id": "u-admin"}"#))?;

    let res2 = app.clone().oneshot(req2).await?;
    assert_eq!(res2.status(), StatusCode::OK);
    let set_cookie2 = res2.headers().get("Set-Cookie").unwrap().to_str().unwrap();
    let session_cookie2 = set_cookie2.split(';').next().unwrap().to_string();

    assert_ne!(session_cookie1, session_cookie2);

    // 3. Logout All using session 1
    let req_logout_all = Request::post("/auth/logout-all")
        .header("Cookie", &session_cookie1)
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .body(body::Body::empty())?;

    let res_logout_all = app.clone().oneshot(req_logout_all).await?;
    // It should now return 403 Forbidden as Step-Up Auth is required
    assert_eq!(res_logout_all.status(), StatusCode::FORBIDDEN);

    let body_bytes_logout_all = body::to_bytes(res_logout_all.into_body(), usize::MAX).await?;
    let body_logout_all: serde_json::Value =
        serde_json::from_slice(&body_bytes_logout_all).unwrap();
    assert_eq!(body_logout_all["error"], "STEP_UP_REQUIRED");
    assert!(body_logout_all["challenge_id"].is_string());

    let req_check_device_1 = Request::get("/auth/session")
        .header("Cookie", &session_cookie1)
        .header("Host", "localhost")
        .body(body::Body::empty())
        .unwrap();
    let res_check_device_1 = app.clone().oneshot(req_check_device_1).await.unwrap();
    let body_bytes_dev_1 = axum::body::to_bytes(res_check_device_1.into_body(), usize::MAX)
        .await
        .unwrap();
    let body_dev_1: serde_json::Value = serde_json::from_slice(&body_bytes_dev_1).unwrap();
    let expected_device_id_1 = body_dev_1["device_id"].as_str().unwrap().to_string();

    let challenge_id = body_logout_all["challenge_id"].as_str().unwrap();
    let challenge = state
        .challenges
        .get(challenge_id)
        .expect("Challenge not found in store");
    assert_eq!(challenge.account_id, "u-admin");
    assert_eq!(challenge.device_id, expected_device_id_1);
    assert_eq!(
        challenge.intent,
        weltgewebe_api::auth::challenges::ChallengeIntent::LogoutAll
    );

    // 4. Verify session 1 is STILL valid (no deletion without Step-Up)
    let req_check1 = Request::get("/auth/session")
        .header("Cookie", &session_cookie1)
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .body(body::Body::empty())?;

    let res_check1 = app.clone().oneshot(req_check1).await?;
    let body_bytes1 = body::to_bytes(res_check1.into_body(), usize::MAX).await?;
    let body1: serde_json::Value = serde_json::from_slice(&body_bytes1).unwrap();
    assert_eq!(body1["authenticated"], true);

    // 5. Verify session 2 is ALSO STILL valid
    let req_check2 = Request::get("/auth/session")
        .header("Cookie", &session_cookie2)
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .body(body::Body::empty())?;

    let res_check2 = app.clone().oneshot(req_check2).await?;
    let body_bytes2 = body::to_bytes(res_check2.into_body(), usize::MAX).await?;
    let body2: serde_json::Value = serde_json::from_slice(&body_bytes2).unwrap();
    assert_eq!(body2["authenticated"], true);

    Ok(())
}

#[tokio::test]
#[serial]
async fn test_logout_all_unauthenticated_rejected() -> Result<()> {
    let state = test_state()?;
    let app = Router::new()
        .merge(api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .layer(MockConnectInfo(
            "127.0.0.1:8080".parse::<SocketAddr>().unwrap(),
        ))
        .layer(axum::middleware::from_fn(
            weltgewebe_api::middleware::csrf::require_csrf,
        ))
        .with_state(state.clone());

    let req = Request::post("/auth/logout-all")
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .body(body::Body::empty())?;

    let res = app.clone().oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::UNAUTHORIZED);

    let body_bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let body_json: serde_json::Value = serde_json::from_slice(&body_bytes).unwrap();
    assert_eq!(body_json["error"], "UNAUTHORIZED");

    Ok(())
}

#[tokio::test]
#[serial]
async fn test_device_management() -> Result<()> {
    let _guard = weltgewebe_api::test_helpers::EnvGuard::set("AUTH_DEV_LOGIN", "1");
    let mut account_map = AccountStore::new();
    let account = weltgewebe_api::routes::accounts::AccountInternal {
        public: weltgewebe_api::routes::accounts::AccountPublic {
            id: "u-admin".to_string(),
            kind: "garnrolle".to_string(),
            title: "User".to_string(),
            summary: None,
            public_pos: None,
            map_state: weltgewebe_api::routes::accounts::GarnrolleMapState::NotOnMap,
            radius_m: 0,
            disabled: false,
            tags: vec![],
        },
        role: Role::Admin,
        email: Some("u1@example.com".to_string()),
        webauthn_user_id: uuid::Uuid::new_v4(),
    };
    account_map.insert(account);

    let state = test_state()?;
    state
        .accounts
        .write()
        .await
        .insert(account_map.get("u-admin").unwrap().clone());

    let app = Router::new()
        .merge(api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .layer(MockConnectInfo(
            "127.0.0.1:8080".parse::<std::net::SocketAddr>().unwrap(),
        ))
        .layer(axum::middleware::from_fn(
            weltgewebe_api::middleware::csrf::require_csrf,
        ))
        .with_state(state.clone());

    // 1. Login to get a session (Device A)
    let req1 = Request::post("/auth/dev/login")
        .header("Content-Type", "application/json")
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .body(body::Body::from(r#"{"account_id": "u-admin"}"#))?;

    let res1 = app.clone().oneshot(req1).await?;
    assert_eq!(res1.status(), StatusCode::OK);
    let set_cookie1 = res1.headers().get("Set-Cookie").unwrap().to_str().unwrap();
    let session_cookie1 = set_cookie1.split(';').next().unwrap().to_string();

    // 2. Refresh session 1 (should preserve device A)
    let req_refresh = Request::post("/auth/session/refresh")
        .header("Cookie", &session_cookie1)
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .body(body::Body::empty())?;

    let res_refresh = app.clone().oneshot(req_refresh).await?;
    assert_eq!(res_refresh.status(), StatusCode::OK);
    let refresh_set_cookie = res_refresh
        .headers()
        .get("Set-Cookie")
        .unwrap()
        .to_str()
        .unwrap();
    let refresh_cookie = refresh_set_cookie.split(';').next().unwrap().to_string();

    // Verify session 1 gives device A
    let req_check_session1 = Request::get("/auth/session")
        .header("Cookie", &refresh_cookie)
        .header("Host", "localhost")
        .body(body::Body::empty())?;

    let res_check_session1 = app.clone().oneshot(req_check_session1).await?;
    let body_bytes1 = body::to_bytes(res_check_session1.into_body(), usize::MAX).await?;
    let body1: serde_json::Value = serde_json::from_slice(&body_bytes1).unwrap();
    let device_a_id = body1["device_id"].as_str().unwrap().to_string();

    // 3. Login again (Device B)
    let req2 = Request::post("/auth/dev/login")
        .header("Content-Type", "application/json")
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .body(body::Body::from(r#"{"account_id": "u-admin"}"#))?;

    let res2 = app.clone().oneshot(req2).await?;
    let set_cookie2 = res2.headers().get("Set-Cookie").unwrap().to_str().unwrap();
    let session_cookie2 = set_cookie2.split(';').next().unwrap().to_string();

    let req_check_session2 = Request::get("/auth/session")
        .header("Cookie", &session_cookie2)
        .header("Host", "localhost")
        .body(body::Body::empty())?;

    let res_check_session2 = app.clone().oneshot(req_check_session2).await?;
    let body_bytes2 = body::to_bytes(res_check_session2.into_body(), usize::MAX).await?;
    let body2: serde_json::Value = serde_json::from_slice(&body_bytes2).unwrap();
    let device_b_id = body2["device_id"].as_str().unwrap().to_string();

    assert_ne!(
        device_a_id, device_b_id,
        "Different logins should generate different device IDs"
    );

    // 4. GET /auth/devices using Device A
    let req_devices = Request::get("/auth/devices")
        .header("Cookie", &refresh_cookie)
        .header("Host", "localhost")
        .body(body::Body::empty())?;

    let res_devices = app.clone().oneshot(req_devices).await?;
    assert_eq!(res_devices.status(), StatusCode::OK);
    let body_bytes_dev = body::to_bytes(res_devices.into_body(), usize::MAX).await?;
    let devices: Vec<serde_json::Value> = serde_json::from_slice(&body_bytes_dev).unwrap();

    // There should be exactly 2 devices (the original session 1 was rotated out and replaced, so still 1 device for A)
    assert_eq!(devices.len(), 2);

    let current_dev = devices
        .iter()
        .find(|d| d["current"].as_bool().unwrap())
        .unwrap();
    assert_eq!(current_dev["device_id"].as_str().unwrap(), device_a_id);

    // 5. DELETE /auth/devices/:device_b_id using Device A (should return 403 Step-up required)
    let req_del_foreign = Request::delete(format!("/auth/devices/{}", device_b_id))
        .header("Cookie", &refresh_cookie)
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .body(body::Body::empty())?;

    let res_del_foreign = app.clone().oneshot(req_del_foreign).await?;
    assert_eq!(res_del_foreign.status(), StatusCode::FORBIDDEN);

    let body_bytes_del_foreign = body::to_bytes(res_del_foreign.into_body(), usize::MAX).await?;
    let body_del_foreign: serde_json::Value =
        serde_json::from_slice(&body_bytes_del_foreign).unwrap();
    assert_eq!(body_del_foreign["error"], "STEP_UP_REQUIRED");
    assert!(body_del_foreign["challenge_id"].is_string());

    let challenge_id = body_del_foreign["challenge_id"].as_str().unwrap();
    let challenge = state
        .challenges
        .get(challenge_id)
        .expect("Challenge not found in store");
    assert_eq!(challenge.account_id, "u-admin");
    assert_eq!(challenge.device_id, device_a_id);
    if let weltgewebe_api::auth::challenges::ChallengeIntent::RemoveDevice { target_device_id } =
        challenge.intent
    {
        assert_eq!(target_device_id, device_b_id);
    } else {
        panic!("Incorrect challenge intent");
    }

    // Attempt to delete a non-existent foreign device (should return 404 NOT_FOUND)
    let req_del_fake = Request::delete(format!("/auth/devices/{}", "fake-device-id"))
        .header("Cookie", &refresh_cookie)
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .body(body::Body::empty())
        .unwrap();

    let res_del_fake = app.clone().oneshot(req_del_fake).await.unwrap();
    assert_eq!(res_del_fake.status(), StatusCode::NOT_FOUND);

    let body_bytes_del_fake = axum::body::to_bytes(res_del_fake.into_body(), usize::MAX)
        .await
        .unwrap();
    let body_del_fake: serde_json::Value = serde_json::from_slice(&body_bytes_del_fake).unwrap();
    assert_eq!(body_del_fake["error"], "NOT_FOUND");
    assert!(body_del_fake.get("challenge_id").is_none());

    // Explicitly verify that the foreign device (Device B) is STILL valid
    let req_check_foreign = Request::get("/auth/session")
        .header("Cookie", &session_cookie2)
        .header("Host", "localhost")
        .body(body::Body::empty())?;

    let res_check_foreign = app.clone().oneshot(req_check_foreign).await?;
    let body_bytes_foreign = body::to_bytes(res_check_foreign.into_body(), usize::MAX).await?;
    let body_foreign: serde_json::Value = serde_json::from_slice(&body_bytes_foreign).unwrap();
    assert_eq!(
        body_foreign["authenticated"], true,
        "Foreign device should remain authenticated after 403 deletion attempt"
    );
    assert_eq!(body_foreign["device_id"].as_str().unwrap(), device_b_id);

    // 6. DELETE /auth/devices/:device_a_id using Device A (should delete current device)
    let req_del_self = Request::delete(format!("/auth/devices/{}", device_a_id))
        .header("Cookie", &refresh_cookie)
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .body(body::Body::empty())?;

    let res_del_self = app.clone().oneshot(req_del_self).await?;
    assert_eq!(res_del_self.status(), StatusCode::NO_CONTENT);

    let logout_set_cookie = res_del_self
        .headers()
        .get("Set-Cookie")
        .unwrap()
        .to_str()
        .unwrap();
    assert!(
        logout_set_cookie.contains("Max-Age=0"),
        "Cookie should be deleted on self device removal"
    );

    // Verify Device A is gone
    let req_check_deleted = Request::get("/auth/session")
        .header("Cookie", &refresh_cookie)
        .header("Host", "localhost")
        .body(body::Body::empty())?;

    let res_check_deleted = app.clone().oneshot(req_check_deleted).await?;
    let body_bytes_deleted = body::to_bytes(res_check_deleted.into_body(), usize::MAX).await?;
    let body_deleted: serde_json::Value = serde_json::from_slice(&body_bytes_deleted).unwrap();
    assert_eq!(body_deleted["authenticated"], false);

    // Verify Device B is now the ONLY device left by querying /auth/devices using Device B's session
    let req_devices_b = Request::get("/auth/devices")
        .header("Cookie", &session_cookie2)
        .header("Host", "localhost")
        .body(body::Body::empty())?;

    let res_devices_b = app.clone().oneshot(req_devices_b).await?;
    assert_eq!(res_devices_b.status(), StatusCode::OK);
    let body_bytes_dev_b = body::to_bytes(res_devices_b.into_body(), usize::MAX).await?;
    let devices_b: Vec<serde_json::Value> = serde_json::from_slice(&body_bytes_dev_b).unwrap();

    assert_eq!(
        devices_b.len(),
        1,
        "Only Device B should remain after Device A was deleted"
    );
    assert_eq!(devices_b[0]["device_id"].as_str().unwrap(), device_b_id);
    assert!(
        devices_b[0]["current"].as_bool().unwrap(),
        "Device B should be current"
    );

    Ok(())
}

#[tokio::test]
#[serial]
async fn test_step_up_magic_link_request_missing_mailer() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());

    let session = create_session(&state, "u1", None).await;
    let session_id = session.id.clone();
    let device_id = session.device_id.clone();

    let challenge = state.challenges.create(
        "u1".to_string(),
        device_id,
        weltgewebe_api::auth::challenges::ChallengeIntent::LogoutAll,
    );

    let app = Router::new()
        .merge(weltgewebe_api::routes::api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .with_state(state.clone());

    let req = Request::post("/auth/step-up/magic-link/request")
        .header("Content-Type", "application/json")
        .header("Host", "localhost")
        .header(
            "Cookie",
            format!(
                "{}={}",
                weltgewebe_api::routes::auth::SESSION_COOKIE_NAME,
                session_id
            ),
        )
        .body(body::Body::from(format!(
            r#"{{"challenge_id":"{}"}}"#,
            challenge.id
        )))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::SERVICE_UNAVAILABLE);

    Ok(())
}

#[tokio::test]
#[serial]
async fn test_step_up_magic_link_request_unauthenticated() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());

    let app = Router::new()
        .merge(weltgewebe_api::routes::api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .with_state(state.clone());

    let req = Request::post("/auth/step-up/magic-link/request")
        .header("Content-Type", "application/json")
        .body(body::Body::from(r#"{"challenge_id":"any-id"}"#))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::UNAUTHORIZED);
    Ok(())
}

#[tokio::test]
#[serial]
async fn test_step_up_magic_link_request_invalid_challenge() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());

    let session = create_session(&state, "u1", None).await;
    let session_id = session.id;

    let app = Router::new()
        .merge(weltgewebe_api::routes::api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .with_state(state.clone());

    let req = Request::post("/auth/step-up/magic-link/request")
        .header("Content-Type", "application/json")
        .header("Host", "localhost")
        .header(
            "Cookie",
            format!(
                "{}={}",
                weltgewebe_api::routes::auth::SESSION_COOKIE_NAME,
                session_id
            ),
        )
        .body(body::Body::from(r#"{"challenge_id":"invalid-id"}"#))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);

    let body_bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let body_str = String::from_utf8_lossy(&body_bytes);
    assert!(body_str.contains("CHALLENGE_INVALID"));

    Ok(())
}

#[tokio::test]
#[serial]
async fn test_step_up_magic_link_request_binding_mismatch() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());

    {
        let mut accounts = state.accounts.write().await;
        let account = weltgewebe_api::routes::accounts::AccountInternal {
            public: weltgewebe_api::routes::accounts::AccountPublic {
                id: "u2".to_string(),
                kind: "garnrolle".to_string(),
                title: "u2".to_string(),
                summary: None,
                public_pos: None,
                map_state: weltgewebe_api::routes::accounts::GarnrolleMapState::NotOnMap,
                radius_m: 0,
                disabled: false,
                tags: vec![],
            },
            role: weltgewebe_api::auth::role::Role::Gast,
            email: Some("u2@example.com".to_string()),
            webauthn_user_id: uuid::Uuid::new_v4(),
        };
        accounts.insert(account);
    }
    let session2 = create_session(&state, "u2", None).await;
    let session_id2 = session2.id;

    let session1 = create_session(&state, "u1", None).await;
    let device_id1 = session1.device_id;
    let challenge1 = state.challenges.create(
        "u1".to_string(),
        device_id1,
        weltgewebe_api::auth::challenges::ChallengeIntent::LogoutAll,
    );

    let app = Router::new()
        .merge(weltgewebe_api::routes::api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .with_state(state.clone());

    let req = Request::post("/auth/step-up/magic-link/request")
        .header("Content-Type", "application/json")
        .header("Host", "localhost")
        .header(
            "Cookie",
            format!(
                "{}={}",
                weltgewebe_api::routes::auth::SESSION_COOKIE_NAME,
                session_id2
            ),
        )
        .body(body::Body::from(format!(
            r#"{{"challenge_id":"{}"}}"#,
            challenge1.id
        )))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);

    let body_bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let body_str = String::from_utf8_lossy(&body_bytes);
    assert!(body_str.contains("CHALLENGE_INVALID"));

    Ok(())
}

#[tokio::test]
#[serial]
async fn test_step_up_magic_link_request_account_invalid() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());

    // Create u2 but without email
    {
        let mut accounts = state.accounts.write().await;
        let account = weltgewebe_api::routes::accounts::AccountInternal {
            public: weltgewebe_api::routes::accounts::AccountPublic {
                id: "u2".to_string(),
                kind: "garnrolle".to_string(),
                title: "u2".to_string(),
                summary: None,
                public_pos: None,
                map_state: weltgewebe_api::routes::accounts::GarnrolleMapState::NotOnMap,
                radius_m: 0,
                disabled: false,
                tags: vec![],
            },
            role: weltgewebe_api::auth::role::Role::Gast,
            email: None, // Missing email
            webauthn_user_id: uuid::Uuid::new_v4(),
        };
        accounts.insert(account);
    }

    let session = create_session(&state, "u2", None).await;
    let session_id = session.id.clone();
    let device_id = session.device_id.clone();

    let challenge = state.challenges.create(
        "u2".to_string(),
        device_id,
        weltgewebe_api::auth::challenges::ChallengeIntent::LogoutAll,
    );

    let app = Router::new()
        .merge(weltgewebe_api::routes::api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .with_state(state.clone());

    let req = Request::post("/auth/step-up/magic-link/request")
        .header("Content-Type", "application/json")
        .header("Host", "localhost")
        .header(
            "Cookie",
            format!(
                "{}={}",
                weltgewebe_api::routes::auth::SESSION_COOKIE_NAME,
                session_id
            ),
        )
        .body(body::Body::from(format!(
            r#"{{"challenge_id":"{}"}}"#,
            challenge.id
        )))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);

    let body_bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let body_str = String::from_utf8_lossy(&body_bytes);
    assert!(body_str.contains("ACCOUNT_INVALID"));

    Ok(())
}

#[tokio::test]
#[serial]
async fn test_step_up_magic_link_request_missing_base_url() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = None; // explicitly missing

    let session = create_session(&state, "u1", None).await;
    let session_id = session.id.clone();
    let device_id = session.device_id.clone();

    let challenge = state.challenges.create(
        "u1".to_string(),
        device_id,
        weltgewebe_api::auth::challenges::ChallengeIntent::LogoutAll,
    );

    let app = Router::new()
        .merge(weltgewebe_api::routes::api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .with_state(state.clone());

    let req = Request::post("/auth/step-up/magic-link/request")
        .header("Content-Type", "application/json")
        .header("Host", "localhost")
        .header(
            "Cookie",
            format!(
                "{}={}",
                weltgewebe_api::routes::auth::SESSION_COOKIE_NAME,
                session_id
            ),
        )
        .body(body::Body::from(
            r#"{"challenge_id":"any"}"#.replace("any", &challenge.id),
        ))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::INTERNAL_SERVER_ERROR);
    Ok(())
}

#[tokio::test]
#[serial]
async fn test_step_up_consume_unauthenticated() -> Result<()> {
    let state = test_state_with_accounts()?;
    let app = Router::new()
        .merge(weltgewebe_api::routes::api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .with_state(state);

    let req = Request::post("/auth/step-up/magic-link/consume")
        .header("Content-Type", "application/json")
        .body(body::Body::from(
            r#"{"token":"any-token","challenge_id":"any-id"}"#,
        ))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::UNAUTHORIZED);
    Ok(())
}

#[tokio::test]
#[serial]
async fn test_step_up_consume_invalid_token() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;

    let session = create_session(&state, "u1", None).await;
    let session_id = session.id.clone();

    let app = Router::new()
        .merge(weltgewebe_api::routes::api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .with_state(state);

    let req = Request::post("/auth/step-up/magic-link/consume")
        .header("Content-Type", "application/json")
        .header(
            "Cookie",
            format!(
                "{}={}",
                weltgewebe_api::routes::auth::SESSION_COOKIE_NAME,
                session_id
            ),
        )
        .body(body::Body::from(
            r#"{"token":"nonexistent-token","challenge_id":"any-id"}"#,
        ))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::UNAUTHORIZED);
    let body_bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let body: serde_json::Value = serde_json::from_slice(&body_bytes)?;
    assert_eq!(body["error"], "TOKEN_INVALID");
    Ok(())
}

#[tokio::test]
#[serial]
async fn test_step_up_consume_challenge_id_mismatch() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;

    let session = create_session(&state, "u1", None).await;
    let session_id = session.id.clone();
    let device_id = session.device_id.clone();

    let challenge = state.challenges.create(
        "u1".to_string(),
        device_id.clone(),
        weltgewebe_api::auth::challenges::ChallengeIntent::LogoutAll,
    );
    // Token references challenge.id, but request claims a different challenge_id
    let token = state
        .step_up_tokens
        .create(challenge.id.clone(), "u1".to_string(), device_id);

    let app = Router::new()
        .merge(weltgewebe_api::routes::api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .with_state(state);

    let req = Request::post("/auth/step-up/magic-link/consume")
        .header("Content-Type", "application/json")
        .header(
            "Cookie",
            format!(
                "{}={}",
                weltgewebe_api::routes::auth::SESSION_COOKIE_NAME,
                session_id
            ),
        )
        .body(body::Body::from(format!(
            r#"{{"token":"{}","challenge_id":"wrong-challenge-id"}}"#,
            token
        )))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::UNAUTHORIZED);
    let body_bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let body: serde_json::Value = serde_json::from_slice(&body_bytes)?;
    assert_eq!(body["error"], "TOKEN_INVALID");
    Ok(())
}

#[tokio::test]
#[serial]
// Documents the deliberate asymmetry: once consume_if_matches succeeds (token removed),
// a missing/expired challenge causes a 401 and the token cannot be reused — client must
// request a new step-up link.
async fn test_step_up_consume_challenge_missing_token_gone() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;

    let session = create_session(&state, "u1", None).await;
    let session_id = session.id.clone();
    let device_id = session.device_id.clone();

    let challenge = state.challenges.create(
        "u1".to_string(),
        device_id.clone(),
        weltgewebe_api::auth::challenges::ChallengeIntent::LogoutAll,
    );
    let challenge_id = challenge.id.clone();
    let token = state
        .step_up_tokens
        .create(challenge_id.clone(), "u1".to_string(), device_id);

    // Pre-consume the challenge so it is gone when the HTTP call arrives
    state.challenges.consume(&challenge_id);

    let app = Router::new()
        .merge(weltgewebe_api::routes::api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .with_state(state.clone());

    // Bindings match → token is removed; challenge is absent → 401
    let req = Request::post("/auth/step-up/magic-link/consume")
        .header("Content-Type", "application/json")
        .header(
            "Cookie",
            format!(
                "{}={}",
                weltgewebe_api::routes::auth::SESSION_COOKIE_NAME,
                session_id
            ),
        )
        .body(body::Body::from(format!(
            r#"{{"token":"{}","challenge_id":"{}"}}"#,
            token, challenge_id
        )))?;

    let res = app.clone().oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::UNAUTHORIZED);
    let body_bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let body: serde_json::Value = serde_json::from_slice(&body_bytes)?;
    assert_eq!(body["error"], "TOKEN_INVALID");

    // Token is gone — retry with the exact same session, challenge_id, and token also fails.
    // This isolates the cause: the token was consumed during the first call (binding-match
    // succeeded) and is now NotFound in the store, regardless of challenge state.
    let req_retry = Request::post("/auth/step-up/magic-link/consume")
        .header("Content-Type", "application/json")
        .header(
            "Cookie",
            format!(
                "{}={}",
                weltgewebe_api::routes::auth::SESSION_COOKIE_NAME,
                session_id
            ),
        )
        .body(body::Body::from(format!(
            r#"{{"token":"{}","challenge_id":"{}"}}"#,
            token, challenge_id
        )))?;

    let res_retry = app.oneshot(req_retry).await?;
    assert_eq!(res_retry.status(), StatusCode::UNAUTHORIZED);
    let body_bytes = body::to_bytes(res_retry.into_body(), usize::MAX).await?;
    let body: serde_json::Value = serde_json::from_slice(&body_bytes)?;
    assert_eq!(body["error"], "TOKEN_INVALID");

    // Confirm at store level: the token is gone (NotFound, not BindingMismatch)
    use weltgewebe_api::auth::step_up_tokens::ConsumeMatchResult;
    let device_id = get_session(&state, &session_id).await.unwrap().device_id;
    assert!(matches!(
        state
            .step_up_tokens
            .consume_if_matches(&token, &challenge_id, "u1", &device_id),
        ConsumeMatchResult::NotFound
    ));
    Ok(())
}

#[tokio::test]
#[serial]
// Verifies the 401/TOKEN_INVALID error response when a token is presented from the wrong session.
// See also: test_step_up_consume_session_mismatch_token_survives, which proves the token is preserved.
async fn test_step_up_consume_session_mismatch() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;

    // session1 belongs to u1/device1 — token is bound to this
    let session1 = create_session(&state, "u1", None).await;
    let device_id1 = session1.device_id.clone();
    let challenge = state.challenges.create(
        "u1".to_string(),
        device_id1.clone(),
        weltgewebe_api::auth::challenges::ChallengeIntent::LogoutAll,
    );
    let token = state
        .step_up_tokens
        .create(challenge.id.clone(), "u1".to_string(), device_id1);

    // session2 belongs to u1 but a different device — we present this session
    let session2 = create_session(&state, "u1", None).await;
    let session_id2 = session2.id.clone();

    let app = Router::new()
        .merge(weltgewebe_api::routes::api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .with_state(state);

    let req = Request::post("/auth/step-up/magic-link/consume")
        .header("Content-Type", "application/json")
        .header(
            "Cookie",
            format!(
                "{}={}",
                weltgewebe_api::routes::auth::SESSION_COOKIE_NAME,
                session_id2
            ),
        )
        .body(body::Body::from(format!(
            r#"{{"token":"{}","challenge_id":"{}"}}"#,
            token, challenge.id
        )))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::UNAUTHORIZED);
    let body_bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let body: serde_json::Value = serde_json::from_slice(&body_bytes)?;
    assert_eq!(body["error"], "TOKEN_INVALID");
    Ok(())
}

#[tokio::test]
#[serial]
// Verifies the token-survival invariant: a wrong-session attempt returns 401 but does NOT burn
// the token, so the legitimate caller can still succeed on a subsequent attempt.
// See also: test_step_up_consume_session_mismatch, which focuses only on the error response.
async fn test_step_up_consume_session_mismatch_token_survives() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;

    // session1 belongs to u1/device1 — token and challenge are bound to this session
    let session1 = create_session(&state, "u1", None).await;
    let session_id1 = session1.id.clone();
    let device_id1 = session1.device_id.clone();
    let challenge = state.challenges.create(
        "u1".to_string(),
        device_id1.clone(),
        weltgewebe_api::auth::challenges::ChallengeIntent::LogoutAll,
    );
    let token = state
        .step_up_tokens
        .create(challenge.id.clone(), "u1".to_string(), device_id1);

    // session2 belongs to u1 but a different device — wrong session
    let session2 = create_session(&state, "u1", None).await;
    let session_id2 = session2.id.clone();

    let app = Router::new()
        .merge(weltgewebe_api::routes::api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .with_state(state.clone());

    // First attempt: wrong session — must return 401 and must NOT burn the token
    let req_mismatch = Request::post("/auth/step-up/magic-link/consume")
        .header("Content-Type", "application/json")
        .header(
            "Cookie",
            format!(
                "{}={}",
                weltgewebe_api::routes::auth::SESSION_COOKIE_NAME,
                session_id2
            ),
        )
        .body(body::Body::from(format!(
            r#"{{"token":"{}","challenge_id":"{}"}}"#,
            token, challenge.id
        )))?;

    let res_mismatch = app.clone().oneshot(req_mismatch).await?;
    assert_eq!(res_mismatch.status(), StatusCode::UNAUTHORIZED);
    let body_bytes = body::to_bytes(res_mismatch.into_body(), usize::MAX).await?;
    let body: serde_json::Value = serde_json::from_slice(&body_bytes)?;
    assert_eq!(body["error"], "TOKEN_INVALID");

    // Second attempt: correct session — must succeed, proving the token was not burned
    let req_correct = Request::post("/auth/step-up/magic-link/consume")
        .header("Content-Type", "application/json")
        .header(
            "Cookie",
            format!(
                "{}={}",
                weltgewebe_api::routes::auth::SESSION_COOKIE_NAME,
                session_id1
            ),
        )
        .body(body::Body::from(format!(
            r#"{{"token":"{}","challenge_id":"{}"}}"#,
            token, challenge.id
        )))?;

    let res_correct = app.oneshot(req_correct).await?;
    assert_eq!(res_correct.status(), StatusCode::NO_CONTENT);
    Ok(())
}

#[tokio::test]
#[serial]
async fn test_step_up_consume_token_reuse_rejected() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;

    let session = create_session(&state, "u1", None).await;
    let session_id = session.id.clone();
    let device_id = session.device_id.clone();

    let challenge = state.challenges.create(
        "u1".to_string(),
        device_id.clone(),
        weltgewebe_api::auth::challenges::ChallengeIntent::LogoutAll,
    );
    let token = state
        .step_up_tokens
        .create(challenge.id.clone(), "u1".to_string(), device_id);

    let app = Router::new()
        .merge(weltgewebe_api::routes::api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .with_state(state.clone());

    // First call — should succeed
    let req1 = Request::post("/auth/step-up/magic-link/consume")
        .header("Content-Type", "application/json")
        .header(
            "Cookie",
            format!(
                "{}={}",
                weltgewebe_api::routes::auth::SESSION_COOKIE_NAME,
                session_id
            ),
        )
        .body(body::Body::from(format!(
            r#"{{"token":"{}","challenge_id":"{}"}}"#,
            token, challenge.id
        )))?;

    let res1 = app.clone().oneshot(req1).await?;
    assert_eq!(res1.status(), StatusCode::NO_CONTENT);

    // Recreate a session since all were deleted by LogoutAll
    let new_session = create_session(&state, "u1", None).await;
    let new_session_id = new_session.id.clone();

    // Second call with the same token — must be rejected
    let req2 = Request::post("/auth/step-up/magic-link/consume")
        .header("Content-Type", "application/json")
        .header(
            "Cookie",
            format!(
                "{}={}",
                weltgewebe_api::routes::auth::SESSION_COOKIE_NAME,
                new_session_id
            ),
        )
        .body(body::Body::from(format!(
            r#"{{"token":"{}","challenge_id":"{}"}}"#,
            token, challenge.id
        )))?;

    let res2 = app.oneshot(req2).await?;
    assert_eq!(res2.status(), StatusCode::UNAUTHORIZED);
    let body_bytes = body::to_bytes(res2.into_body(), usize::MAX).await?;
    let body: serde_json::Value = serde_json::from_slice(&body_bytes)?;
    assert_eq!(body["error"], "TOKEN_INVALID");
    Ok(())
}

#[tokio::test]
#[serial]
async fn test_step_up_consume_logout_all_success() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;

    // Create two sessions for the same account
    let session1 = create_session(&state, "u1", None).await;
    let session_id1 = session1.id.clone();
    let device_id1 = session1.device_id.clone();
    let session2 = create_session(&state, "u1", None).await;
    let session_id2 = session2.id.clone();

    let challenge = state.challenges.create(
        "u1".to_string(),
        device_id1.clone(),
        weltgewebe_api::auth::challenges::ChallengeIntent::LogoutAll,
    );
    let token = state
        .step_up_tokens
        .create(challenge.id.clone(), "u1".to_string(), device_id1);

    let app = Router::new()
        .merge(weltgewebe_api::routes::api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .with_state(state.clone());

    let req = Request::post("/auth/step-up/magic-link/consume")
        .header("Content-Type", "application/json")
        .header(
            "Cookie",
            format!(
                "{}={}",
                weltgewebe_api::routes::auth::SESSION_COOKIE_NAME,
                session_id1
            ),
        )
        .body(body::Body::from(format!(
            r#"{{"token":"{}","challenge_id":"{}"}}"#,
            token, challenge.id
        )))?;

    let res = app.clone().oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::NO_CONTENT);

    // Session 1 must be gone
    assert!(get_session(&state, &session_id1).await.is_none());
    // Session 2 must also be gone (LogoutAll)
    assert!(get_session(&state, &session_id2).await.is_none());

    // The challenge must be consumed (single-use)
    assert!(state.challenges.get(&challenge.id).is_none());
    Ok(())
}

#[tokio::test]
#[serial]
async fn test_step_up_consume_remove_device_success() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;

    // session1 is the requesting session (device1)
    let session1 = create_session(&state, "u1", None).await;
    let session_id1 = session1.id.clone();
    let device_id1 = session1.device_id.clone();

    // session2 is the target device to remove (device2)
    let session2 = create_session(&state, "u1", Some("target-device")).await;
    let session_id2 = session2.id.clone();

    let challenge = state.challenges.create(
        "u1".to_string(),
        device_id1.clone(),
        weltgewebe_api::auth::challenges::ChallengeIntent::RemoveDevice {
            target_device_id: "target-device".to_string(),
        },
    );
    let token = state
        .step_up_tokens
        .create(challenge.id.clone(), "u1".to_string(), device_id1);

    let app = Router::new()
        .merge(weltgewebe_api::routes::api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .with_state(state.clone());

    let req = Request::post("/auth/step-up/magic-link/consume")
        .header("Content-Type", "application/json")
        .header(
            "Cookie",
            format!(
                "{}={}",
                weltgewebe_api::routes::auth::SESSION_COOKIE_NAME,
                session_id1
            ),
        )
        .body(body::Body::from(format!(
            r#"{{"token":"{}","challenge_id":"{}"}}"#,
            token, challenge.id
        )))?;

    let res = app.clone().oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::NO_CONTENT);

    // Target device (session2) must be removed
    assert!(get_session(&state, &session_id2).await.is_none());
    // Requesting session (session1) must still be valid
    assert!(get_session(&state, &session_id1).await.is_some());

    // The challenge must be consumed (single-use)
    assert!(state.challenges.get(&challenge.id).is_none());
    Ok(())
}

#[tokio::test]
#[serial]
async fn test_update_email_no_op_returns_204() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;

    let session = create_session(&state, "u1", Some("dev1")).await;
    let cookie = format!("{}={}", SESSION_COOKIE_NAME, session.id);

    let app = Router::new()
        .merge(weltgewebe_api::routes::api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .with_state(state.clone());

    let req = Request::builder()
        .method("PUT")
        .uri("/auth/me/email")
        .header("Cookie", cookie.clone())
        .header("Origin", "http://localhost")
        .header("Content-Type", "application/json")
        .header("X-Forwarded-For", "127.0.0.1")
        .body(body::Body::from(r#"{"new_email": "u1@example.com"}"#))?;

    let res = app.oneshot(req).await?;
    // Should be a no-op since the email is already u1@example.com
    assert_eq!(res.status(), StatusCode::NO_CONTENT);

    Ok(())
}

#[tokio::test]
#[serial]
async fn test_update_email_full_e2e_flow() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let _env = weltgewebe_api::test_helpers::EnvGuard::set(
        "GEWEBE_IN_DIR",
        tmp.path().to_str().expect("tempdir path is valid utf-8"),
    );
    let initial_record = serde_json::json!({
        "id": "u1",
        "type": "garnrolle",
        "title": "User One",
        "summary": "Summary 1",
        "map_state": "not_on_map",
        "radius_m": 0,
        "disabled": false,
        "tags": [],
        "role": "gast",
        "email": "u1@example.com",
        "webauthn_user_id": uuid::Uuid::new_v4().to_string(),
    });
    tokio::fs::write(
        tmp.path().join("demo.accounts.jsonl"),
        format!("{}\n", serde_json::to_string(&initial_record)?),
    )
    .await?;

    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());

    // Inject the mock mailer with a test sink
    let sink = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    state.config.smtp_host = Some("localhost".to_string());
    state.config.smtp_from = Some("noreply@example.com".to_string());
    let mailer = weltgewebe_api::mailer::Mailer::new(&state.config)
        .unwrap()
        .with_test_sink(sink.clone());
    state.mailer = Some(std::sync::Arc::new(mailer));

    let session = create_session(&state, "u1", Some("dev1")).await;
    let cookie = format!("{}={}", SESSION_COOKIE_NAME, session.id);

    let app = Router::new()
        .merge(weltgewebe_api::routes::api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .with_state(state.clone());

    // 1. PUT /auth/me/email
    let req1 = Request::builder()
        .method("PUT")
        .uri("/auth/me/email")
        .header("Cookie", cookie.clone())
        .header("Origin", "http://localhost")
        .header("Content-Type", "application/json")
        .header("X-Forwarded-For", "127.0.0.1")
        .body(body::Body::from(r#"{"new_email": "e2e@example.com"}"#))?;

    let res1 = app.clone().oneshot(req1).await?;
    assert_eq!(res1.status(), StatusCode::FORBIDDEN);

    let body_bytes = axum::body::to_bytes(res1.into_body(), usize::MAX).await?;
    let body_json: serde_json::Value = serde_json::from_slice(&body_bytes)?;
    let challenge_id = body_json["challenge_id"].as_str().unwrap().to_string();

    // 2. POST /auth/step-up/magic-link/request
    let req2 = Request::builder()
        .method("POST")
        .uri("/auth/step-up/magic-link/request")
        .header("Cookie", cookie.clone())
        .header("Content-Type", "application/json")
        .header("Origin", "http://localhost")
        .header("X-Forwarded-For", "127.0.0.1")
        .body(body::Body::from(
            serde_json::json!({
                "challenge_id": challenge_id
            })
            .to_string(),
        ))?;

    let res2 = app.clone().oneshot(req2).await?;
    assert_eq!(res2.status(), StatusCode::NO_CONTENT);

    // 3. Extract token and challenge_id from mail sink and verify the contract
    let (token, link_challenge_id) = {
        let messages = sink.lock().unwrap();
        assert_eq!(messages.len(), 1);
        assert_eq!(messages[0].0, "e2e@example.com"); // Mail went to new address

        let link = &messages[0].1;
        // The link format must be exactly "{base_url}/auth/step-up/consume?token={token}&challenge_id={id}"
        assert!(link.contains("?token="), "Link must contain ?token=");
        assert!(
            link.contains("&challenge_id="),
            "Link must contain &challenge_id="
        );

        let parts: Vec<&str> = link.split("?token=").collect();
        let token_and_challenge: Vec<&str> = parts[1].split("&challenge_id=").collect();

        let extracted_token = token_and_challenge[0].to_string();
        let extracted_challenge = token_and_challenge[1].to_string();

        // Explicitly assert the challenge_id in the link matches the challenge we created
        assert_eq!(
            extracted_challenge, challenge_id,
            "Challenge ID in link must match the requested challenge"
        );

        (extracted_token, extracted_challenge)
    };

    // 4. POST /auth/step-up/magic-link/consume
    let req3 = Request::builder()
        .method("POST")
        .uri("/auth/step-up/magic-link/consume")
        .header("Cookie", cookie)
        .header("Content-Type", "application/json")
        .header("Origin", "http://localhost")
        .header("X-Forwarded-For", "127.0.0.1")
        .body(body::Body::from(
            serde_json::json!({
                "token": token,
                "challenge_id": link_challenge_id
            })
            .to_string(),
        ))?;

    let res3 = app.oneshot(req3).await?;
    assert_eq!(res3.status(), StatusCode::NO_CONTENT);

    // 5. Assert both the live cache and a fresh JSONL reload see the new email.
    {
        let accounts = state.accounts.read().await;
        let acc = accounts.get("u1").unwrap();
        assert_eq!(acc.email.as_deref(), Some("e2e@example.com"));
    }
    let reloaded = weltgewebe_api::routes::accounts::load_all_accounts().await?;
    assert_eq!(
        reloaded
            .get("u1")
            .and_then(|account| account.email.as_deref()),
        Some("e2e@example.com")
    );

    // Ensure challenge is consumed and gone
    assert!(state.challenges.get(&challenge_id).is_none());

    Ok(())
}

#[tokio::test]
#[serial]
async fn test_update_email_invalid_format() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;

    let session = create_session(&state, "u1", Some("dev1")).await;
    let cookie = format!("{}={}", SESSION_COOKIE_NAME, session.id);

    let app = Router::new()
        .merge(weltgewebe_api::routes::api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .with_state(state.clone());

    let req1 = Request::builder()
        .method("PUT")
        .uri("/auth/me/email")
        .header("Cookie", cookie.clone())
        .header("Origin", "http://localhost")
        .header("Content-Type", "application/json")
        .header("X-Forwarded-For", "127.0.0.1")
        .body(body::Body::from(r#"{"new_email": "invalidemail"}"#))?;

    let res1 = app.clone().oneshot(req1).await?;
    assert_eq!(res1.status(), StatusCode::BAD_REQUEST);

    let req2 = Request::builder()
        .method("PUT")
        .uri("/auth/me/email")
        .header("Cookie", cookie.clone())
        .header("Origin", "http://localhost")
        .header("Content-Type", "application/json")
        .header("X-Forwarded-For", "127.0.0.1")
        .body(body::Body::from(
            r#"{"new_email": "invalid email@example.com"}"#, // space
        ))?;

    let res2 = app.clone().oneshot(req2).await?;
    assert_eq!(res2.status(), StatusCode::BAD_REQUEST);

    let req3 = Request::builder()
        .method("PUT")
        .uri("/auth/me/email")
        .header("Cookie", cookie.clone())
        .header("Origin", "http://localhost")
        .header("Content-Type", "application/json")
        .header("X-Forwarded-For", "127.0.0.1")
        .body(body::Body::from(
            r#"{"new_email": "nodomain@a"}"#, // missing dot
        ))?;

    let res3 = app.oneshot(req3).await?;
    assert_eq!(res3.status(), StatusCode::BAD_REQUEST);

    Ok(())
}

#[tokio::test]
#[serial]
async fn test_step_up_consume_begin_passkey_registration_issues_grant() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;

    let session = create_session(&state, "u1", Some("dev-passkey")).await;
    let challenge = state.challenges.create(
        "u1".to_string(),
        "dev-passkey".to_string(),
        weltgewebe_api::auth::challenges::ChallengeIntent::BeginPasskeyRegistration,
    );
    let token = state.step_up_tokens.create(
        challenge.id.clone(),
        "u1".to_string(),
        "dev-passkey".to_string(),
    );

    let app = Router::new()
        .merge(weltgewebe_api::routes::api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .with_state(state.clone());

    let req = Request::post("/auth/step-up/magic-link/consume")
        .header("Content-Type", "application/json")
        .header("Cookie", format!("{}={}", SESSION_COOKIE_NAME, session.id))
        .body(body::Body::from(
            serde_json::json!({
                "token": token,
                "challenge_id": challenge.id
            })
            .to_string(),
        ))?;

    let res = app.clone().oneshot(req).await?;
    assert_eq!(
        res.status(),
        StatusCode::OK,
        "BeginPasskeyRegistration step-up consume must return 200 with registration_grant_id"
    );

    let body_bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let body_json: serde_json::Value = serde_json::from_slice(&body_bytes)?;
    assert!(
        body_json.get("registration_grant_id").is_some(),
        "response must contain registration_grant_id"
    );
    let grant_id = body_json["registration_grant_id"]
        .as_str()
        .context("registration_grant_id must be a string")?;
    assert!(
        !grant_id.is_empty(),
        "registration_grant_id must not be empty"
    );

    assert!(
        get_session(&state, &session.id).await.is_some(),
        "begin-passkey-registration step-up must not alter sessions"
    );
    assert!(
        state.challenges.get(&challenge.id).is_none(),
        "challenge must be consumed exactly once"
    );

    Ok(())
}

#[tokio::test]
#[serial]
async fn test_update_email_requires_step_up() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;

    let session = create_session(&state, "u1", Some("dev1")).await;
    let cookie = format!("{}={}", SESSION_COOKIE_NAME, session.id);

    let app = Router::new()
        .merge(weltgewebe_api::routes::api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .with_state(state.clone());

    let req = Request::builder()
        .method("PUT")
        .uri("/auth/me/email")
        .header("Cookie", cookie.clone())
        .header("Origin", "http://localhost")
        .header("Content-Type", "application/json")
        .header("X-Forwarded-For", "127.0.0.1")
        .body(body::Body::from(r#"{"new_email": "new@example.com"}"#))?;

    let res = app.oneshot(req).await?;

    let status = res.status();
    let body_bytes = axum::body::to_bytes(res.into_body(), usize::MAX).await?;
    let body_str = String::from_utf8_lossy(&body_bytes);

    assert_eq!(status, StatusCode::FORBIDDEN);

    let body_json: serde_json::Value = serde_json::from_str(&body_str)?;
    assert_eq!(body_json["error"], "STEP_UP_REQUIRED");
    assert!(body_json.get("challenge_id").is_some());
    Ok(())
}

#[tokio::test]
#[serial]
async fn test_update_email_conflict_with_existing_account() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;

    let session = create_session(&state, "u1", Some("dev1")).await;
    let cookie = format!("{}={}", SESSION_COOKIE_NAME, session.id);

    let app = Router::new()
        .merge(weltgewebe_api::routes::api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .with_state(state.clone());

    // We must add u2 to state.accounts so there is a conflict
    {
        let mut accounts = state.accounts.write().await;
        accounts.insert(weltgewebe_api::routes::accounts::AccountInternal {
            public: weltgewebe_api::routes::accounts::AccountPublic {
                id: "u2".to_string(),
                kind: "garnrolle".to_string(),
                title: "User Two".to_string(),
                summary: None,
                public_pos: None,
                map_state: weltgewebe_api::routes::accounts::GarnrolleMapState::NotOnMap,
                radius_m: 0,
                disabled: false,
                tags: vec![],
            },
            role: weltgewebe_api::auth::role::Role::Gast,
            email: Some("u2@example.com".to_string()),
            webauthn_user_id: uuid::Uuid::new_v4(),
        });
    }

    // Try to update to an email that is already used by u2
    let req = Request::builder()
        .method("PUT")
        .uri("/auth/me/email")
        .header("Cookie", cookie.clone())
        .header("Origin", "http://localhost")
        .header("Content-Type", "application/json")
        .header("X-Forwarded-For", "127.0.0.1")
        .body(body::Body::from(r#"{"new_email": "u2@example.com"}"#))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::CONFLICT);
    Ok(())
}

#[tokio::test]
#[serial]
async fn test_update_email_consume_wrong_session() -> Result<()> {
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;

    // Session 1 is the one that initiated the update
    let session1 = create_session(&state, "u1", Some("dev1")).await;

    // Session 2 is an attacker or just another session
    let session2 = create_session(&state, "u2", Some("dev2")).await;
    let wrong_cookie = format!("{}={}", SESSION_COOKIE_NAME, session2.id);

    use weltgewebe_api::auth::challenges::ChallengeIntent;

    let challenge = state.challenges.create(
        session1.account_id.clone(),
        session1.device_id.clone(),
        ChallengeIntent::UpdateEmail {
            new_email: "hacked@example.com".to_string(),
        },
    );

    let token = state.step_up_tokens.create(
        challenge.id.clone(),
        session1.account_id.clone(),
        session1.device_id.clone(),
    );

    let app = Router::new()
        .merge(weltgewebe_api::routes::api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .with_state(state.clone());

    let req = Request::builder()
        .method("POST")
        .uri("/auth/step-up/magic-link/consume")
        .header("Cookie", wrong_cookie) // Attack attempt
        .header("Content-Type", "application/json")
        .header("Origin", "http://localhost")
        .header("X-Forwarded-For", "127.0.0.1")
        .body(body::Body::from(
            serde_json::json!({
                "token": token,
                "challenge_id": challenge.id
            })
            .to_string(),
        ))?;

    let res = app.oneshot(req).await?;
    // The binding mismatch rejects it
    assert_eq!(res.status(), StatusCode::UNAUTHORIZED);

    // Ensure the email wasn't updated
    let accounts = state.accounts.read().await;
    let acc = accounts.get("u1").unwrap();
    assert_ne!(acc.email, Some("hacked@example.com".to_string()));

    Ok(())
}

#[tokio::test]
#[serial]
async fn test_update_email_consume_session_rotation() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let _env = weltgewebe_api::test_helpers::EnvGuard::set(
        "GEWEBE_IN_DIR",
        tmp.path().to_str().expect("tempdir path is valid utf-8"),
    );
    let initial_record = serde_json::json!({
        "id": "u1",
        "type": "garnrolle",
        "title": "User One",
        "summary": "Summary 1",
        "map_state": "not_on_map",
        "radius_m": 0,
        "disabled": false,
        "tags": [],
        "role": "gast",
        "email": "u1@example.com",
        "webauthn_user_id": uuid::Uuid::new_v4().to_string(),
    });
    tokio::fs::write(
        tmp.path().join("demo.accounts.jsonl"),
        format!("{}\n", serde_json::to_string(&initial_record)?),
    )
    .await?;

    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;

    // Session 1 is the one that initiated the update (same device)
    let session1 = create_session(&state, "u1", Some("dev1")).await;

    use weltgewebe_api::auth::challenges::ChallengeIntent;

    let challenge = state.challenges.create(
        session1.account_id.clone(),
        session1.device_id.clone(),
        ChallengeIntent::UpdateEmail {
            new_email: "rotated@example.com".to_string(),
        },
    );

    let token = state.step_up_tokens.create(
        challenge.id.clone(),
        session1.account_id.clone(),
        session1.device_id.clone(),
    );

    // Simulate session rotation: Session 1 expires, new Session 2 is created for the *same* device
    delete_session(&state, &session1.id).await;
    let session2 = create_session(&state, "u1", Some("dev1")).await;

    let new_cookie = format!("{}={}", SESSION_COOKIE_NAME, session2.id);

    let app = Router::new()
        .merge(weltgewebe_api::routes::api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .with_state(state.clone());

    let req = Request::builder()
        .method("POST")
        .uri("/auth/step-up/magic-link/consume")
        .header("Cookie", new_cookie) // Consume with rotated session
        .header("Content-Type", "application/json")
        .header("Origin", "http://localhost")
        .header("X-Forwarded-For", "127.0.0.1")
        .body(body::Body::from(
            serde_json::json!({
                "token": token,
                "challenge_id": challenge.id
            })
            .to_string(),
        ))?;

    let res = app.oneshot(req).await?;

    // The binding matches because account_id and device_id match, despite the different session_id
    assert_eq!(res.status(), StatusCode::NO_CONTENT);

    // Ensure both the cache and a fresh JSONL reload observe the rotated email.
    {
        let accounts = state.accounts.read().await;
        let acc = accounts.get("u1").unwrap();
        assert_eq!(acc.email.as_deref(), Some("rotated@example.com"));
    }
    let reloaded = weltgewebe_api::routes::accounts::load_all_accounts().await?;
    assert_eq!(
        reloaded
            .get("u1")
            .and_then(|account| account.email.as_deref()),
        Some("rotated@example.com")
    );

    Ok(())
}

#[tokio::test]
#[serial]
async fn test_update_email_jsonl_invalid_source_leaves_cache_unchanged() -> Result<()> {
    for corrupt_tail in ["{not-json", r#"{"id":"u1"}"#] {
        let tmp = tempfile::tempdir()?;
        let _env = weltgewebe_api::test_helpers::EnvGuard::set(
            "GEWEBE_IN_DIR",
            tmp.path().to_str().expect("tempdir path is valid utf-8"),
        );
        let initial_record = serde_json::json!({
            "id": "u1",
            "type": "garnrolle",
            "title": "User One",
            "summary": "Summary 1",
            "map_state": "not_on_map",
            "radius_m": 0,
            "disabled": false,
            "tags": [],
            "role": "gast",
            "email": "u1@example.com",
            "webauthn_user_id": uuid::Uuid::new_v4().to_string(),
        });
        tokio::fs::write(
            tmp.path().join("demo.accounts.jsonl"),
            format!(
                "{}\n{}\n",
                serde_json::to_string(&initial_record)?,
                corrupt_tail
            ),
        )
        .await?;

        let mut state = test_state_with_accounts()?;
        state.config.auth_public_login = true;

        let session = create_session(&state, "u1", Some("dev1")).await;
        let cookie = format!("{}={}", SESSION_COOKIE_NAME, session.id);

        use weltgewebe_api::auth::challenges::ChallengeIntent;
        let challenge = state.challenges.create(
            session.account_id.clone(),
            session.device_id.clone(),
            ChallengeIntent::UpdateEmail {
                new_email: "must-not-stick@example.com".to_string(),
            },
        );
        let token = state.step_up_tokens.create(
            challenge.id.clone(),
            session.account_id.clone(),
            session.device_id.clone(),
        );

        let app = Router::new()
            .merge(weltgewebe_api::routes::api_router())
            .route_layer(axum::middleware::from_fn_with_state(
                state.clone(),
                weltgewebe_api::middleware::auth::auth_middleware,
            ))
            .with_state(state.clone());

        let req = Request::builder()
            .method("POST")
            .uri("/auth/step-up/magic-link/consume")
            .header("Cookie", cookie)
            .header("Content-Type", "application/json")
            .header("Origin", "http://localhost")
            .header("X-Forwarded-For", "127.0.0.1")
            .body(body::Body::from(
                serde_json::json!({
                    "token": token,
                    "challenge_id": challenge.id
                })
                .to_string(),
            ))?;

        let res = app.oneshot(req).await?;
        assert_eq!(
            res.status(),
            StatusCode::INTERNAL_SERVER_ERROR,
            "invalid JSONL tail must fail closed: {corrupt_tail}"
        );

        let accounts = state.accounts.read().await;
        assert_eq!(
            accounts
                .get("u1")
                .and_then(|account| account.email.as_deref()),
            Some("u1@example.com"),
            "an invalid JSONL source must not leave a cache-only email update"
        );
    }

    Ok(())
}

#[tokio::test]
#[serial]
async fn test_update_email_jsonl_read_failure_leaves_cache_unchanged() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let invalid_in_dir = tmp.path().join("not-a-directory");
    tokio::fs::write(&invalid_in_dir, b"not a directory").await?;
    let _env = weltgewebe_api::test_helpers::EnvGuard::set(
        "GEWEBE_IN_DIR",
        invalid_in_dir
            .to_str()
            .expect("temporary path is valid utf-8"),
    );

    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;

    let session = create_session(&state, "u1", Some("dev1")).await;
    let cookie = format!("{}={}", SESSION_COOKIE_NAME, session.id);

    use weltgewebe_api::auth::challenges::ChallengeIntent;
    let challenge = state.challenges.create(
        session.account_id.clone(),
        session.device_id.clone(),
        ChallengeIntent::UpdateEmail {
            new_email: "must-not-stick@example.com".to_string(),
        },
    );
    let token = state.step_up_tokens.create(
        challenge.id.clone(),
        session.account_id.clone(),
        session.device_id.clone(),
    );

    let app = Router::new()
        .merge(weltgewebe_api::routes::api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .with_state(state.clone());

    let req = Request::builder()
        .method("POST")
        .uri("/auth/step-up/magic-link/consume")
        .header("Cookie", cookie)
        .header("Content-Type", "application/json")
        .header("Origin", "http://localhost")
        .header("X-Forwarded-For", "127.0.0.1")
        .body(body::Body::from(
            serde_json::json!({
                "token": token,
                "challenge_id": challenge.id
            })
            .to_string(),
        ))?;

    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::INTERNAL_SERVER_ERROR);

    let accounts = state.accounts.read().await;
    assert_eq!(
        accounts
            .get("u1")
            .and_then(|account| account.email.as_deref()),
        Some("u1@example.com"),
        "a failed durable JSONL read must not leave a cache-only email update"
    );

    Ok(())
}

// ─── Passkey / WebAuthn Integration Tests ────────────────────────────────────

/// Helper: build a test state with WebAuthn configured (rp_id=localhost, origin=http://localhost:3000).
fn test_state_with_webauthn() -> Result<ApiState> {
    let mut state = test_state_with_accounts()?;
    state.config.webauthn_rp_id = Some("localhost".to_string());
    state.config.webauthn_rp_origin = Some("http://localhost:3000".to_string());
    let webauthn = weltgewebe_api::auth::passkeys::build_webauthn(&state.config)
        .context("failed to build Webauthn")?
        .context("Webauthn should be Some when rp_id/rp_origin set")?;
    state.webauthn = Some(webauthn);
    Ok(state)
}

fn app_with_auth(state: ApiState) -> Router {
    Router::new()
        .merge(weltgewebe_api::routes::api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        // Provide a peer address so handlers that extract `ConnectInfo`
        // (e.g. the rate-limited `auth/options`) resolve a client IP.
        .layer(MockConnectInfo(SocketAddr::from(([127, 0, 0, 1], 8080))))
        .with_state(state)
}

#[tokio::test]
async fn passkey_register_options_requires_authentication() -> Result<()> {
    let state = test_state_with_webauthn()?;
    let app = app_with_auth(state);

    let req = Request::post("/auth/passkeys/register/options")
        .header("Host", "localhost")
        .header("Origin", "http://localhost:3000")
        .body(body::Body::empty())?;

    let res = app.oneshot(req).await?;
    assert_eq!(
        res.status(),
        StatusCode::UNAUTHORIZED,
        "unauthenticated request must be rejected"
    );

    Ok(())
}

#[tokio::test]
async fn passkey_register_options_returns_503_when_not_configured() -> Result<()> {
    let state = test_state_with_accounts()?;
    // webauthn is None in default test_state
    let session = create_session(&state, "u1", None).await;

    let app = app_with_auth(state);

    let req = Request::post("/auth/passkeys/register/options")
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .header("Cookie", format!("{}={}", SESSION_COOKIE_NAME, session.id))
        .body(body::Body::empty())?;

    let res = app.oneshot(req).await?;
    assert_eq!(
        res.status(),
        StatusCode::SERVICE_UNAVAILABLE,
        "should return 503 when WebAuthn not configured"
    );

    let body_bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let body_json: serde_json::Value = serde_json::from_slice(&body_bytes)?;
    assert_eq!(body_json["error"], "PASSKEYS_NOT_CONFIGURED");

    Ok(())
}

#[tokio::test]
async fn passkey_register_options_requires_step_up_challenge() -> Result<()> {
    let state = test_state_with_webauthn()?;
    let session = create_session(&state, "u1", Some("dev-passkey")).await;
    let expected_device_id = session.device_id.clone();

    let app = app_with_auth(state.clone());

    let req = Request::post("/auth/passkeys/register/options")
        .header("Host", "localhost")
        .header("Origin", "http://localhost:3000")
        .header("Cookie", format!("{}={}", SESSION_COOKIE_NAME, session.id))
        .body(body::Body::empty())?;

    let res = app.oneshot(req).await?;
    let status = res.status();
    let body_bytes = body::to_bytes(res.into_body(), usize::MAX).await?;

    assert_eq!(status, StatusCode::FORBIDDEN);

    let body_json: serde_json::Value = serde_json::from_slice(&body_bytes)?;
    assert_eq!(body_json["error"], "STEP_UP_REQUIRED");
    let challenge_id = body_json["challenge_id"]
        .as_str()
        .context("response must include challenge_id")?;

    let challenge = state
        .challenges
        .get(challenge_id)
        .context("challenge must be stored for step-up flow")?;
    assert_eq!(challenge.account_id, "u1");
    assert_eq!(challenge.device_id, expected_device_id);
    assert!(matches!(
        challenge.intent,
        weltgewebe_api::auth::challenges::ChallengeIntent::BeginPasskeyRegistration
    ));

    Ok(())
}

#[tokio::test]
async fn passkey_register_options_reuses_active_step_up_challenge() -> Result<()> {
    let state = test_state_with_webauthn()?;
    let session = create_session(&state, "u1", Some("dev-passkey")).await;

    let app = app_with_auth(state.clone());

    // First call
    let req = Request::post("/auth/passkeys/register/options")
        .header("Host", "localhost")
        .header("Origin", "http://localhost:3000")
        .header("Cookie", format!("{}={}", SESSION_COOKIE_NAME, session.id))
        .body(body::Body::empty())?;
    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::FORBIDDEN);
    let body1: serde_json::Value =
        serde_json::from_slice(&body::to_bytes(res.into_body(), usize::MAX).await?)?;

    // Second call (rebuild the app since oneshot consumes it)
    let app2 = app_with_auth(state.clone());
    let req2 = Request::post("/auth/passkeys/register/options")
        .header("Host", "localhost")
        .header("Origin", "http://localhost:3000")
        .header("Cookie", format!("{}={}", SESSION_COOKIE_NAME, session.id))
        .body(body::Body::empty())?;
    let res2 = app2.oneshot(req2).await?;
    assert_eq!(res2.status(), StatusCode::FORBIDDEN);
    let body2: serde_json::Value =
        serde_json::from_slice(&body::to_bytes(res2.into_body(), usize::MAX).await?)?;

    let challenge_id_1 = body1["challenge_id"]
        .as_str()
        .context("first response must include challenge_id")?;
    let challenge_id_2 = body2["challenge_id"]
        .as_str()
        .context("second response must include challenge_id")?;
    assert_eq!(
        challenge_id_1, challenge_id_2,
        "same account/device/intent must reuse the active challenge"
    );

    let challenge = state
        .challenges
        .get(challenge_id_1)
        .context("reused challenge must remain available")?;
    assert!(matches!(
        challenge.intent,
        weltgewebe_api::auth::challenges::ChallengeIntent::BeginPasskeyRegistration
    ));

    Ok(())
}

async fn request_passkey_registration_challenge(
    state: &ApiState,
    session_cookie: &str,
) -> Result<String> {
    let app = app_with_auth(state.clone());
    let req = Request::post("/auth/passkeys/register/options")
        .header("Host", "localhost")
        .header("Origin", "http://localhost:3000")
        .header("Cookie", session_cookie.to_string())
        .body(body::Body::empty())?;

    let res = app.oneshot(req).await?;
    assert_eq!(
        res.status(),
        StatusCode::FORBIDDEN,
        "register/options without grant must return STEP_UP_REQUIRED"
    );
    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let body: serde_json::Value = serde_json::from_slice(&bytes)?;
    assert_eq!(body["error"], "STEP_UP_REQUIRED");
    let challenge_id = body["challenge_id"]
        .as_str()
        .context("response must contain challenge_id")?
        .to_string();

    Ok(challenge_id)
}

async fn consume_begin_passkey_registration_step_up(
    state: &ApiState,
    session_cookie: &str,
    challenge_id: &str,
    account_id: &str,
    device_id: &str,
) -> Result<String> {
    let token = state.step_up_tokens.create(
        challenge_id.to_string(),
        account_id.to_string(),
        device_id.to_string(),
    );

    let app = app_with_auth(state.clone());
    let req = Request::post("/auth/step-up/magic-link/consume")
        .header("Content-Type", "application/json")
        .header("Cookie", session_cookie.to_string())
        .body(body::Body::from(
            serde_json::json!({"token": token, "challenge_id": challenge_id}).to_string(),
        ))?;

    let res = app.oneshot(req).await?;
    assert_eq!(
        res.status(),
        StatusCode::OK,
        "step-up consume must return 200 OK for BeginPasskeyRegistration"
    );
    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let body: serde_json::Value = serde_json::from_slice(&bytes)?;
    let grant_id = body["registration_grant_id"]
        .as_str()
        .context("step-up consume must return registration_grant_id")?
        .to_string();
    Ok(grant_id)
}

#[tokio::test]
async fn passkey_register_options_with_valid_grant_returns_200() -> Result<()> {
    let state = test_state_with_webauthn()?;
    let session = create_session(&state, "u1", Some("dev-passkey")).await;
    let cookie = format!("{}={}", SESSION_COOKIE_NAME, session.id);

    let challenge_id = request_passkey_registration_challenge(&state, &cookie).await?;
    let challenge = state
        .challenges
        .get(&challenge_id)
        .context("challenge from register/options must be stored")?;
    assert_eq!(challenge.account_id, "u1");
    assert_eq!(challenge.device_id, session.device_id);
    assert!(matches!(
        challenge.intent,
        weltgewebe_api::auth::challenges::ChallengeIntent::BeginPasskeyRegistration
    ));

    let grant_id = consume_begin_passkey_registration_step_up(
        &state,
        &cookie,
        &challenge_id,
        &session.account_id,
        &session.device_id,
    )
    .await?;

    let app = app_with_auth(state.clone());
    let req = Request::post("/auth/passkeys/register/options")
        .header("Host", "localhost")
        .header("Origin", "http://localhost:3000")
        .header("Content-Type", "application/json")
        .header("Cookie", &cookie)
        .body(body::Body::from(
            serde_json::json!({"registration_grant_id": grant_id}).to_string(),
        ))?;

    let res = app.oneshot(req).await?;
    assert_eq!(
        res.status(),
        StatusCode::OK,
        "valid grant must allow the WebAuthn ceremony to start"
    );

    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let body: serde_json::Value = serde_json::from_slice(&bytes)?;
    let registration_id = body["registration_id"]
        .as_str()
        .context("response must contain registration_id")?;
    assert!(
        !registration_id.is_empty(),
        "registration_id must not be empty"
    );
    assert!(
        body.get("options").is_some(),
        "response must contain WebAuthn options"
    );

    // The registration must be stored in the PasskeyRegistrationStore.
    let stored = state
        .passkey_registrations
        .consume(registration_id, "u1")
        .await;
    assert!(
        stored.is_some(),
        "registration_id must be stored in PasskeyRegistrationStore"
    );

    Ok(())
}

#[tokio::test]
async fn passkey_register_options_grant_is_single_use() -> Result<()> {
    let state = test_state_with_webauthn()?;
    let session = create_session(&state, "u1", Some("dev-passkey")).await;
    let cookie = format!("{}={}", SESSION_COOKIE_NAME, session.id);

    let challenge_id = request_passkey_registration_challenge(&state, &cookie).await?;
    let grant_id = consume_begin_passkey_registration_step_up(
        &state,
        &cookie,
        &challenge_id,
        &session.account_id,
        &session.device_id,
    )
    .await?;

    // First use: must succeed.
    let app1 = app_with_auth(state.clone());
    let req1 = Request::post("/auth/passkeys/register/options")
        .header("Host", "localhost")
        .header("Origin", "http://localhost:3000")
        .header("Content-Type", "application/json")
        .header("Cookie", &cookie)
        .body(body::Body::from(
            serde_json::json!({"registration_grant_id": grant_id}).to_string(),
        ))?;
    let res1 = app1.oneshot(req1).await?;
    assert_eq!(res1.status(), StatusCode::OK, "first use must succeed");

    // Second use: must be rejected (single-use).
    let app2 = app_with_auth(state.clone());
    let req2 = Request::post("/auth/passkeys/register/options")
        .header("Host", "localhost")
        .header("Origin", "http://localhost:3000")
        .header("Content-Type", "application/json")
        .header("Cookie", &cookie)
        .body(body::Body::from(
            serde_json::json!({"registration_grant_id": grant_id}).to_string(),
        ))?;
    let res2 = app2.oneshot(req2).await?;
    assert_eq!(
        res2.status(),
        StatusCode::FORBIDDEN,
        "second use of the same grant must be rejected"
    );
    let bytes = body::to_bytes(res2.into_body(), usize::MAX).await?;
    let body: serde_json::Value = serde_json::from_slice(&bytes)?;
    assert_eq!(body["error"], "GRANT_INVALID");

    Ok(())
}

#[tokio::test]
async fn passkey_register_options_grant_wrong_account_rejected() -> Result<()> {
    let state = test_state_with_webauthn()?;

    // Session for u1 with device "dev-passkey".
    let session_u1 = create_session(&state, "u1", Some("dev-passkey")).await;
    // Session for a1 (different account, different device).
    let session_a1 = create_session(&state, "a1", Some("dev-a1")).await;
    let cookie_u1 = format!("{}={}", SESSION_COOKIE_NAME, session_u1.id);
    let cookie_a1 = format!("{}={}", SESSION_COOKIE_NAME, session_a1.id);

    let challenge_id = request_passkey_registration_challenge(&state, &cookie_u1).await?;
    let grant_id = consume_begin_passkey_registration_step_up(
        &state,
        &cookie_u1,
        &challenge_id,
        &session_u1.account_id,
        &session_u1.device_id,
    )
    .await?;

    // a1 tries to use u1's grant — must be rejected.
    let app = app_with_auth(state.clone());
    let req = Request::post("/auth/passkeys/register/options")
        .header("Host", "localhost")
        .header("Origin", "http://localhost:3000")
        .header("Content-Type", "application/json")
        .header("Cookie", &cookie_a1)
        .body(body::Body::from(
            serde_json::json!({"registration_grant_id": grant_id}).to_string(),
        ))?;
    let res = app.oneshot(req).await?;
    assert_eq!(
        res.status(),
        StatusCode::FORBIDDEN,
        "grant belonging to a different account must be rejected"
    );
    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let body: serde_json::Value = serde_json::from_slice(&bytes)?;
    assert_eq!(body["error"], "GRANT_INVALID");

    // Grant must still be valid for the correct account/device after the rejected attempt.
    let app2 = app_with_auth(state.clone());
    let req2 = Request::post("/auth/passkeys/register/options")
        .header("Host", "localhost")
        .header("Origin", "http://localhost:3000")
        .header("Content-Type", "application/json")
        .header("Cookie", &cookie_u1)
        .body(body::Body::from(
            serde_json::json!({"registration_grant_id": grant_id}).to_string(),
        ))?;
    let res2 = app2.oneshot(req2).await?;
    assert_eq!(
        res2.status(),
        StatusCode::OK,
        "grant must remain valid for the correct account after a wrong-account attempt"
    );

    Ok(())
}

#[tokio::test]
async fn passkey_register_options_grant_wrong_device_rejected() -> Result<()> {
    let state = test_state_with_webauthn()?;

    let session_u1_device_a = create_session(&state, "u1", Some("dev-passkey")).await;
    let session_u1_device_b = create_session(&state, "u1", Some("dev-other")).await;
    let cookie_device_a = format!("{}={}", SESSION_COOKIE_NAME, session_u1_device_a.id);
    let cookie_device_b = format!("{}={}", SESSION_COOKIE_NAME, session_u1_device_b.id);

    let challenge_id = request_passkey_registration_challenge(&state, &cookie_device_a).await?;
    let grant_id = consume_begin_passkey_registration_step_up(
        &state,
        &cookie_device_a,
        &challenge_id,
        &session_u1_device_a.account_id,
        &session_u1_device_a.device_id,
    )
    .await?;

    let app = app_with_auth(state.clone());
    let req = Request::post("/auth/passkeys/register/options")
        .header("Host", "localhost")
        .header("Origin", "http://localhost:3000")
        .header("Content-Type", "application/json")
        .header("Cookie", &cookie_device_b)
        .body(body::Body::from(
            serde_json::json!({"registration_grant_id": grant_id}).to_string(),
        ))?;
    let res = app.oneshot(req).await?;
    assert_eq!(
        res.status(),
        StatusCode::FORBIDDEN,
        "grant belonging to a different device must be rejected"
    );
    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let body: serde_json::Value = serde_json::from_slice(&bytes)?;
    assert_eq!(body["error"], "GRANT_INVALID");

    let app2 = app_with_auth(state.clone());
    let req2 = Request::post("/auth/passkeys/register/options")
        .header("Host", "localhost")
        .header("Origin", "http://localhost:3000")
        .header("Content-Type", "application/json")
        .header("Cookie", &cookie_device_a)
        .body(body::Body::from(
            serde_json::json!({"registration_grant_id": grant_id}).to_string(),
        ))?;
    let res2 = app2.oneshot(req2).await?;
    assert_eq!(
        res2.status(),
        StatusCode::OK,
        "grant must remain valid for the correct device after a wrong-device attempt"
    );

    Ok(())
}

#[tokio::test]
async fn passkey_register_options_without_grant_still_returns_step_up_required() -> Result<()> {
    let state = test_state_with_webauthn()?;
    let session = create_session(&state, "u1", Some("dev-passkey")).await;

    let app = app_with_auth(state.clone());
    let req = Request::post("/auth/passkeys/register/options")
        .header("Host", "localhost")
        .header("Origin", "http://localhost:3000")
        .header("Cookie", format!("{}={}", SESSION_COOKIE_NAME, session.id))
        .body(body::Body::empty())?;

    let res = app.oneshot(req).await?;
    assert_eq!(
        res.status(),
        StatusCode::FORBIDDEN,
        "no grant must still yield STEP_UP_REQUIRED"
    );
    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let body: serde_json::Value = serde_json::from_slice(&bytes)?;
    assert_eq!(body["error"], "STEP_UP_REQUIRED");
    assert!(body.get("challenge_id").is_some());

    Ok(())
}

#[tokio::test]
async fn passkey_register_options_expired_grant_rejected() -> Result<()> {
    let state = test_state_with_webauthn()?;
    let session = create_session(&state, "u1", Some("dev-passkey")).await;
    let cookie = format!("{}={}", SESSION_COOKIE_NAME, session.id);

    // Insert an already-expired grant directly.
    let grant_id = state.passkey_registration_grants.insert_with_ttl(
        "u1".to_string(),
        session.device_id.clone(),
        chrono::Duration::milliseconds(1),
    );
    std::thread::sleep(std::time::Duration::from_millis(50));

    let app = app_with_auth(state.clone());
    let req = Request::post("/auth/passkeys/register/options")
        .header("Host", "localhost")
        .header("Origin", "http://localhost:3000")
        .header("Content-Type", "application/json")
        .header("Cookie", &cookie)
        .body(body::Body::from(
            serde_json::json!({"registration_grant_id": grant_id}).to_string(),
        ))?;
    let res = app.oneshot(req).await?;
    assert_eq!(
        res.status(),
        StatusCode::FORBIDDEN,
        "expired grant must be rejected"
    );
    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let body: serde_json::Value = serde_json::from_slice(&bytes)?;
    assert_eq!(body["error"], "GRANT_INVALID");

    Ok(())
}

// ── Passkey Register-Verify ───────────────────────────────────────────────
//
// These tests cover the negative paths and structural guarantees of
// `POST /auth/passkeys/register/verify`. A reproducible positive proof
// (success path with a real Passkey from `navigator.credentials.create()`)
// requires either a browser E2E test or a soft-authenticator crate
// (e.g. `webauthn-authenticator-rs`); neither is part of this repository's
// current dependency set. See `docs/reports/passkey-register-verify-prep.md`.
//
// The structurally-valid but cryptographically-bogus credential used below
// matches the `RegisterPublicKeyCredential` shape defined in
// `webauthn-rs-proto::attest::RegisterPublicKeyCredential` so that the JSON
// deserialiser accepts it; `webauthn.finish_passkey_registration` then
// rejects it during the real cryptographic checks.

fn bogus_register_credential() -> serde_json::Value {
    serde_json::json!({
        "id": "AAAA",
        "rawId": "AAAA",
        "response": {
            "attestationObject": "AAAA",
            "clientDataJSON": "AAAA"
        },
        "type": "public-key"
    })
}

async fn obtain_registration_id_via_api(
    state: &ApiState,
    session_cookie: &str,
    account_id: &str,
    device_id: &str,
) -> Result<String> {
    let challenge_id = request_passkey_registration_challenge(state, session_cookie).await?;
    let grant_id = consume_begin_passkey_registration_step_up(
        state,
        session_cookie,
        &challenge_id,
        account_id,
        device_id,
    )
    .await?;

    let app = app_with_auth(state.clone());
    let req = Request::post("/auth/passkeys/register/options")
        .header("Host", "localhost")
        .header("Origin", "http://localhost:3000")
        .header("Content-Type", "application/json")
        .header("Cookie", session_cookie.to_string())
        .body(body::Body::from(
            serde_json::json!({"registration_grant_id": grant_id}).to_string(),
        ))?;
    let res = app.oneshot(req).await?;
    assert_eq!(
        res.status(),
        StatusCode::OK,
        "register/options must succeed with valid grant"
    );
    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let body: serde_json::Value = serde_json::from_slice(&bytes)?;
    let registration_id = body["registration_id"]
        .as_str()
        .context("response must contain registration_id")?
        .to_string();
    Ok(registration_id)
}

#[tokio::test]
async fn passkey_register_verify_requires_authentication() -> Result<()> {
    let state = test_state_with_webauthn()?;
    let app = app_with_auth(state);

    let body_payload = serde_json::json!({
        "registration_id": "any-id",
        "credential": bogus_register_credential(),
    });
    let req = Request::post("/auth/passkeys/register/verify")
        .header("Host", "localhost")
        .header("Origin", "http://localhost:3000")
        .header("Content-Type", "application/json")
        .body(body::Body::from(body_payload.to_string()))?;

    let res = app.oneshot(req).await?;
    assert_eq!(
        res.status(),
        StatusCode::UNAUTHORIZED,
        "unauthenticated register-verify must be rejected"
    );

    Ok(())
}

#[tokio::test]
async fn passkey_register_verify_returns_503_when_not_configured() -> Result<()> {
    let state = test_state_with_accounts()?;
    let session = create_session(&state, "u1", None).await;
    let app = app_with_auth(state);

    let body_payload = serde_json::json!({
        "registration_id": "any-id",
        "credential": bogus_register_credential(),
    });
    let req = Request::post("/auth/passkeys/register/verify")
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .header("Content-Type", "application/json")
        .header("Cookie", format!("{}={}", SESSION_COOKIE_NAME, session.id))
        .body(body::Body::from(body_payload.to_string()))?;

    let res = app.oneshot(req).await?;
    assert_eq!(
        res.status(),
        StatusCode::SERVICE_UNAVAILABLE,
        "register-verify must fail closed when WebAuthn is not configured"
    );
    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let body: serde_json::Value = serde_json::from_slice(&bytes)?;
    assert_eq!(body["error"], "PASSKEYS_NOT_CONFIGURED");

    Ok(())
}

#[tokio::test]
async fn passkey_register_verify_unknown_registration_id_returns_400() -> Result<()> {
    let state = test_state_with_webauthn()?;
    let session = create_session(&state, "u1", Some("dev-passkey")).await;
    let cookie = format!("{}={}", SESSION_COOKIE_NAME, session.id);

    let app = app_with_auth(state);
    let body_payload = serde_json::json!({
        "registration_id": "not-a-real-registration",
        "credential": bogus_register_credential(),
    });
    let req = Request::post("/auth/passkeys/register/verify")
        .header("Host", "localhost")
        .header("Origin", "http://localhost:3000")
        .header("Content-Type", "application/json")
        .header("Cookie", &cookie)
        .body(body::Body::from(body_payload.to_string()))?;

    let res = app.oneshot(req).await?;
    assert_eq!(
        res.status(),
        StatusCode::BAD_REQUEST,
        "unknown registration_id must yield 400"
    );
    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let body: serde_json::Value = serde_json::from_slice(&bytes)?;
    assert_eq!(body["error"], "REGISTRATION_INVALID");

    Ok(())
}

#[tokio::test]
async fn passkey_register_verify_wrong_account_rejected_and_non_destructive() -> Result<()> {
    let state = test_state_with_webauthn()?;
    let session_u1 = create_session(&state, "u1", Some("dev-passkey")).await;
    let session_a1 = create_session(&state, "a1", Some("dev-a1")).await;
    let cookie_u1 = format!("{}={}", SESSION_COOKIE_NAME, session_u1.id);
    let cookie_a1 = format!("{}={}", SESSION_COOKIE_NAME, session_a1.id);

    let registration_id = obtain_registration_id_via_api(
        &state,
        &cookie_u1,
        &session_u1.account_id,
        &session_u1.device_id,
    )
    .await?;

    // a1 tries to verify u1's registration — must fail with 400.
    let app = app_with_auth(state.clone());
    let body_payload = serde_json::json!({
        "registration_id": registration_id.as_str(),
        "credential": bogus_register_credential(),
    });
    let req = Request::post("/auth/passkeys/register/verify")
        .header("Host", "localhost")
        .header("Origin", "http://localhost:3000")
        .header("Content-Type", "application/json")
        .header("Cookie", &cookie_a1)
        .body(body::Body::from(body_payload.to_string()))?;
    let res = app.oneshot(req).await?;
    assert_eq!(
        res.status(),
        StatusCode::BAD_REQUEST,
        "verify with mismatching account must be rejected"
    );
    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let body: serde_json::Value = serde_json::from_slice(&bytes)?;
    assert_eq!(body["error"], "REGISTRATION_INVALID");

    // The registration must still be consumable by the legitimate owner u1
    // (PasskeyRegistrationStore::consume is non-destructive on wrong account).
    let still_there = state
        .passkey_registrations
        .consume(&registration_id, &session_u1.account_id)
        .await;
    assert!(
        still_there.is_some(),
        "wrong-account verify must not burn the registration for the legitimate owner"
    );

    Ok(())
}

#[tokio::test]
async fn passkey_register_verify_invalid_credential_returns_400_and_consumes_registration(
) -> Result<()> {
    let state = test_state_with_webauthn()?;
    let session = create_session(&state, "u1", Some("dev-passkey")).await;
    let cookie = format!("{}={}", SESSION_COOKIE_NAME, session.id);

    let registration_id =
        obtain_registration_id_via_api(&state, &cookie, &session.account_id, &session.device_id)
            .await?;

    // First attempt: structurally valid but cryptographically bogus credential.
    let app = app_with_auth(state.clone());
    let body_payload = serde_json::json!({
        "registration_id": registration_id.as_str(),
        "credential": bogus_register_credential(),
    });
    let req = Request::post("/auth/passkeys/register/verify")
        .header("Host", "localhost")
        .header("Origin", "http://localhost:3000")
        .header("Content-Type", "application/json")
        .header("Cookie", &cookie)
        .body(body::Body::from(body_payload.to_string()))?;
    let res = app.oneshot(req).await?;
    assert_eq!(
        res.status(),
        StatusCode::BAD_REQUEST,
        "invalid credential must be rejected by WebAuthn verification"
    );
    // Snapshot Set-Cookie headers before consuming the body — register/verify
    // never establishes a session, even on the happy path, and must not set a
    // session cookie on the failure path either.
    let res_cookies_str: Vec<String> = res
        .headers()
        .get_all("set-cookie")
        .iter()
        .filter_map(|v| v.to_str().ok().map(str::to_string))
        .collect();
    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let body: serde_json::Value = serde_json::from_slice(&bytes)?;
    // The error code differentiates from REGISTRATION_INVALID so that the
    // caller can tell "your registration_id is gone" from "your credential
    // failed verification".
    assert_eq!(body["error"], "CREDENTIAL_INVALID");

    assert!(
        res_cookies_str
            .iter()
            .all(|c| !c.starts_with(&format!("{}=", SESSION_COOKIE_NAME))),
        "register/verify must not set a session cookie; got: {:?}",
        res_cookies_str
    );

    // Second attempt with the same registration_id must now fail with
    // REGISTRATION_INVALID — the registration_id is single-use and was
    // consumed before WebAuthn verification (consume-first semantics).
    let app2 = app_with_auth(state.clone());
    let body_payload2 = serde_json::json!({
        "registration_id": registration_id.as_str(),
        "credential": bogus_register_credential(),
    });
    let req2 = Request::post("/auth/passkeys/register/verify")
        .header("Host", "localhost")
        .header("Origin", "http://localhost:3000")
        .header("Content-Type", "application/json")
        .header("Cookie", &cookie)
        .body(body::Body::from(body_payload2.to_string()))?;
    let res2 = app2.oneshot(req2).await?;
    assert_eq!(
        res2.status(),
        StatusCode::BAD_REQUEST,
        "second verify with the same registration_id must be rejected"
    );
    let bytes2 = body::to_bytes(res2.into_body(), usize::MAX).await?;
    let body2: serde_json::Value = serde_json::from_slice(&bytes2)?;
    assert_eq!(body2["error"], "REGISTRATION_INVALID");

    Ok(())
}

#[tokio::test]
#[serial]
async fn passkey_register_verify_deleted_account_returns_401_and_does_not_consume_registration(
) -> Result<()> {
    // Defense-in-depth layering: `auth_middleware` checks account existence and
    // only populates `AuthContext.account_id` when the account is in state. If
    // the account is deleted after session creation, the middleware returns 401
    // before the handler executes — so the handler's inner ACCOUNT_INVALID guard
    // never fires in practice. This test proves the middleware layer holds and
    // that no registration_id is consumed.
    let state = test_state_with_webauthn()?;
    let session = create_session(&state, "u1", Some("dev-passkey")).await;
    let cookie = format!("{}={}", SESSION_COOKIE_NAME, session.id);
    let account_id = session.account_id.clone();

    let registration_id =
        obtain_registration_id_via_api(&state, &cookie, &account_id, &session.device_id).await?;

    // Remove the account from the store to simulate a deleted/ghost account.
    {
        let mut accounts = state.accounts.write().await;
        let removed = accounts.remove(&account_id);
        assert!(removed, "account must be present before removal");
    }

    let app = app_with_auth(state.clone());
    let body_payload = serde_json::json!({
        "registration_id": registration_id.as_str(),
        "credential": bogus_register_credential(),
    });
    let req = Request::post("/auth/passkeys/register/verify")
        .header("Host", "localhost")
        .header("Origin", "http://localhost:3000")
        .header("Content-Type", "application/json")
        .header("Cookie", &cookie)
        .body(body::Body::from(body_payload.to_string()))?;
    let res = app.oneshot(req).await?;
    // `auth_middleware` fires first: no account in state → ctx.account_id = None → 401.
    assert_eq!(
        res.status(),
        StatusCode::UNAUTHORIZED,
        "deleted account: middleware must return 401 before handler executes"
    );

    // The registration_id must NOT have been consumed — the request was rejected
    // before the consume step.
    let reg_state = state
        .passkey_registrations
        .consume(&registration_id, &account_id)
        .await;
    assert!(
        reg_state.is_some(),
        "registration_id must not be consumed when middleware rejects the request"
    );

    Ok(())
}

#[tokio::test]
#[serial]
async fn session_middleware_removes_unknown_cookie() -> Result<()> {
    let _guard = weltgewebe_api::test_helpers::EnvGuard::set("AUTH_COOKIE_SECURE", "0");
    let state = test_state_with_accounts()?;
    let app = app_with_auth(state);

    let req = Request::get("/auth/session")
        .header("Cookie", format!("{}=unknown-session", SESSION_COOKIE_NAME))
        .body(body::Body::empty())?;
    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);
    let removal = extract_set_cookie_header(res.headers(), SESSION_COOKIE_NAME)
        .context("stale session cookie must be removed")?;
    assert_removal_session_cookie(&removal, false);

    let body_bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let body_json: serde_json::Value = serde_json::from_slice(&body_bytes)?;
    assert_eq!(body_json["authenticated"], false);
    Ok(())
}

#[tokio::test]
#[serial]
async fn session_middleware_deletes_disabled_account_session() -> Result<()> {
    let _guard = weltgewebe_api::test_helpers::EnvGuard::set("AUTH_COOKIE_SECURE", "0");
    let state = test_state_with_accounts()?;
    let session = create_session(&state, "u1", None).await;
    {
        let mut accounts = state.accounts.write().await;
        let mut account = accounts.get("u1").cloned().context("u1 missing")?;
        account.public.disabled = true;
        accounts.insert(account);
    }

    let req = Request::get("/auth/session")
        .header("Cookie", format!("{}={}", SESSION_COOKIE_NAME, session.id))
        .body(body::Body::empty())?;
    let res = app_with_auth(state.clone()).oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);
    let removal = extract_set_cookie_header(res.headers(), SESSION_COOKIE_NAME)
        .context("disabled-account session cookie must be removed")?;
    assert_removal_session_cookie(&removal, false);
    assert!(
        get_session(&state, &session.id).await.is_none(),
        "disabled-account session must be deleted server-side"
    );
    Ok(())
}

#[tokio::test]
#[serial]
async fn session_middleware_deletes_session_for_missing_account() -> Result<()> {
    let _guard = weltgewebe_api::test_helpers::EnvGuard::set("AUTH_COOKIE_SECURE", "0");
    let state = test_state_with_accounts()?;
    let session = create_session(&state, "missing-account", None).await;

    let req = Request::get("/auth/session")
        .header("Cookie", format!("{}={}", SESSION_COOKIE_NAME, session.id))
        .body(body::Body::empty())?;
    let res = app_with_auth(state.clone()).oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);
    let removal = extract_set_cookie_header(res.headers(), SESSION_COOKIE_NAME)
        .context("missing-account session cookie must be removed")?;
    assert_removal_session_cookie(&removal, false);
    assert!(
        get_session(&state, &session.id).await.is_none(),
        "missing-account session must be deleted server-side"
    );
    Ok(())
}

#[tokio::test]
#[serial]
async fn session_cookie_normalization_failure_preserves_other_cookies() -> Result<()> {
    async fn invalid_cookie_handler(jar: CookieJar) -> axum::response::Response {
        let session_cookie = Cookie::build((SESSION_COOKIE_NAME, "missing-session"))
            .path("/")
            .http_only(true)
            .same_site(SameSite::Lax)
            .secure(false)
            .build();
        let auxiliary_cookie = Cookie::build(("proof_aux", "kept"))
            .path("/")
            .http_only(true)
            .build();
        (
            jar.add(session_cookie).add(auxiliary_cookie),
            (StatusCode::OK, "must be cleared"),
        )
            .into_response()
    }

    let _guard = weltgewebe_api::test_helpers::EnvGuard::set("AUTH_COOKIE_SECURE", "0");
    let state = test_state_with_accounts()?;
    let app = Router::new()
        .route(
            "/test/invalid-session-cookie",
            axum::routing::get(invalid_cookie_handler),
        )
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .with_state(state);

    let req = Request::get("/test/invalid-session-cookie").body(body::Body::empty())?;
    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::INTERNAL_SERVER_ERROR);
    let removal = extract_set_cookie_header(res.headers(), SESSION_COOKIE_NAME)
        .context("invalid response session cookie must be removed")?;
    assert_removal_session_cookie(&removal, false);
    assert_eq!(
        extract_cookie_value(res.headers(), "proof_aux").as_deref(),
        Some("kept"),
        "non-session Set-Cookie headers must survive normalization failure"
    );
    let body_bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    assert!(
        body_bytes.is_empty(),
        "failed response body must be cleared"
    );
    Ok(())
}

#[tokio::test]
#[serial]
async fn duplicate_response_session_cookies_fail_closed_to_one_removal() -> Result<()> {
    async fn duplicate_cookie_handler() -> axum::response::Response {
        let mut response = (StatusCode::OK, "must be cleared").into_response();
        let removal = HeaderValue::from_static(
            "gewebe_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0;              Expires=Thu, 01 Jan 1970 00:00:00 GMT",
        );
        response.headers_mut().append(SET_COOKIE, removal.clone());
        response.headers_mut().append(SET_COOKIE, removal);
        response.headers_mut().append(
            SET_COOKIE,
            HeaderValue::from_static("proof_aux=kept; HttpOnly; Path=/"),
        );
        response
    }

    let _guard = weltgewebe_api::test_helpers::EnvGuard::set("AUTH_COOKIE_SECURE", "0");
    let state = test_state_with_accounts()?;
    let app = Router::new()
        .route(
            "/test/duplicate-session-cookies",
            axum::routing::get(duplicate_cookie_handler),
        )
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .with_state(state);

    let res = app
        .oneshot(Request::get("/test/duplicate-session-cookies").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::INTERNAL_SERVER_ERROR);
    let session_headers = res
        .headers()
        .get_all(SET_COOKIE)
        .iter()
        .filter(|header| header.as_bytes().starts_with(b"gewebe_session="))
        .collect::<Vec<_>>();
    assert_eq!(
        session_headers.len(),
        1,
        "fail-closed normalization must emit exactly one session removal cookie"
    );
    assert_removal_session_cookie(session_headers[0].to_str()?, false);
    assert_eq!(
        extract_cookie_value(res.headers(), "proof_aux").as_deref(),
        Some("kept"),
        "unrelated cookies must survive duplicate-session rejection"
    );
    Ok(())
}

#[tokio::test]
#[serial]
async fn malformed_response_session_cookie_is_replaced_with_removal() -> Result<()> {
    async fn malformed_cookie_handler() -> axum::response::Response {
        let mut response = (StatusCode::OK, "must be cleared").into_response();
        response.headers_mut().append(
            SET_COOKIE,
            HeaderValue::from_bytes(b"gewebe_session=\xff; Path=/")
                .expect("obs-text header value must be constructible"),
        );
        response
    }

    let _guard = weltgewebe_api::test_helpers::EnvGuard::set("AUTH_COOKIE_SECURE", "0");
    let state = test_state_with_accounts()?;
    let app = Router::new()
        .route(
            "/test/malformed-session-cookie",
            axum::routing::get(malformed_cookie_handler),
        )
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .with_state(state);

    let res = app
        .oneshot(Request::get("/test/malformed-session-cookie").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::INTERNAL_SERVER_ERROR);
    let session_headers = res
        .headers()
        .get_all(SET_COOKIE)
        .iter()
        .filter(|header| header.as_bytes().starts_with(b"gewebe_session="))
        .collect::<Vec<_>>();
    assert_eq!(session_headers.len(), 1);
    assert_removal_session_cookie(session_headers[0].to_str()?, false);
    Ok(())
}

// Phase 6 API-level Proof: Session Cookie Security Attributes
//
// This test proves that:
// 1. Session cookies are set with correct security attributes (HttpOnly, SameSite=Lax, Secure).
// 2. Magic-Link consume with seeded token produces a valid authenticated API session.
// 3. Offline mode remains covered (no DATABASE_URL required).

#[tokio::test]
#[serial]
async fn session_cookie_has_secure_attributes_on_magic_link_consume() -> Result<()> {
    // Simulate production-like mode where secure cookies are explicitly enabled.
    let _guard = weltgewebe_api::test_helpers::EnvGuard::set("AUTH_COOKIE_SECURE", "1");

    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());

    let app = app_with_auth(state.clone());

    // Step 1: Seed a valid token directly (consume-path API proof, no mailer/browser E2E)
    let token = state.tokens.create("u1@example.com".to_string());

    // Step 2: GET consume page to extract nonce
    let uri = format!("/auth/magic-link/consume?token={}", token);
    let req_get = Request::get(&uri).body(body::Body::empty())?;
    let res_get = app.clone().oneshot(req_get).await?;
    assert_eq!(res_get.status(), StatusCode::OK);

    let nonce_val = extract_cookie_value(res_get.headers(), NONCE_COOKIE_NAME)
        .context("nonce cookie not set")?;
    let (_, nonce) = nonce_val.split_once('.').context("invalid nonce format")?;
    let nonce = nonce.to_string();

    // Step 3: POST consume with nonce - this sets the SESSION_COOKIE_NAME
    let body_str = format!("token={}&nonce={}", token, nonce);
    let req_post = Request::post("/auth/magic-link/consume")
        .header("Content-Type", "application/x-www-form-urlencoded")
        .header("Cookie", format!("{}={}", NONCE_COOKIE_NAME, nonce_val))
        .body(body::Body::from(body_str))?;

    let response_started = chrono::Utc::now();
    let res_post = app.clone().oneshot(req_post).await?;
    let response_finished = chrono::Utc::now();
    assert_eq!(res_post.status(), StatusCode::SEE_OTHER);

    // Step 4: Verify Session Cookie Attributes (PROOF)
    let session_cookie_header = extract_set_cookie_header(res_post.headers(), SESSION_COOKIE_NAME)
        .context("Session cookie not set in response")?;

    // Step 5: Verify the emitted Cookie attributes are derived from the
    // actually stored session, not only from a pure helper function.
    let session_id = extract_cookie_value(res_post.headers(), SESSION_COOKIE_NAME)
        .context("session id not in set-cookie")?;
    let stored_session = get_session(&state, &session_id)
        .await
        .context("server session must exist after magic-link consume")?;
    assert_persistent_cookie_matches_stored_session(
        &session_cookie_header,
        true,
        &stored_session,
        response_started,
        response_finished,
    );

    // Step 6: Verify cookie authenticates a real API request (/auth/session)
    let req_session = Request::get("/auth/session")
        .header("Host", "localhost")
        .header("Cookie", format!("{}={}", SESSION_COOKIE_NAME, session_id))
        .body(body::Body::empty())?;
    let res_session = app.oneshot(req_session).await?;
    assert_eq!(res_session.status(), StatusCode::OK);

    let body_bytes = body::to_bytes(res_session.into_body(), usize::MAX).await?;
    let body_json: serde_json::Value = serde_json::from_slice(&body_bytes)?;
    assert_eq!(body_json["authenticated"], true);
    assert!(body_json.get("expires_at").is_some());
    assert!(
        body_json["device_id"].as_str().is_some(),
        "device_id must be present for authenticated session"
    );

    Ok(())
}

#[tokio::test]
#[serial]
async fn session_cookie_insecure_when_auth_cookie_secure_disabled() -> Result<()> {
    // Simulate dev/local mode where AUTH_COOKIE_SECURE=0
    let _guard = weltgewebe_api::test_helpers::EnvGuard::set("AUTH_COOKIE_SECURE", "0");

    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());
    assert!(!state.config.auth_cookie_secure);

    // Runtime environment changes after state construction must not change the
    // cookie scope mid-flight. All auth paths use the captured config value.
    let _runtime_flip = weltgewebe_api::test_helpers::EnvGuard::set("AUTH_COOKIE_SECURE", "1");
    let app = app(state.clone());

    // Seed token directly for consume-path proof.
    let token = state.tokens.create("u1@example.com".to_string());

    // GET consume page
    let uri = format!("/auth/magic-link/consume?token={}", token);
    let req_get = Request::get(&uri).body(body::Body::empty())?;
    let res_get = app.clone().oneshot(req_get).await?;

    let nonce_header = extract_set_cookie_header(res_get.headers(), NONCE_COOKIE_NAME)
        .context("nonce cookie not set")?;
    assert_session_cookie_secure(&nonce_header, false);
    let nonce_val = extract_cookie_value(res_get.headers(), NONCE_COOKIE_NAME)
        .context("nonce cookie value not set")?;
    let (_, nonce) = nonce_val.split_once('.').context("invalid nonce format")?;

    // POST consume
    let body_str = format!("token={}&nonce={}", token, nonce);
    let req_post = Request::post("/auth/magic-link/consume")
        .header("Content-Type", "application/x-www-form-urlencoded")
        .header("Cookie", format!("{}={}", NONCE_COOKIE_NAME, nonce_val))
        .body(body::Body::from(body_str))?;

    let res_post = app.oneshot(req_post).await?;
    assert_eq!(res_post.status(), StatusCode::SEE_OTHER);

    // Verify Session Cookie has NO Secure flag
    let session_cookie_header = extract_set_cookie_header(res_post.headers(), SESSION_COOKIE_NAME)
        .context("Session cookie not set")?;

    assert_session_cookie_secure(&session_cookie_header, false);

    Ok(())
}

#[cfg(feature = "integration-testing")]
#[tokio::test]
#[serial]
async fn magic_link_full_round_trip_request_to_session() -> Result<()> {
    // Prove the full Magic-Link flow:
    //   POST /auth/magic-link/request  (generates token inside handler)
    //   → GET  /auth/magic-link/consume (nonce exchange)
    //   → POST /auth/magic-link/consume (session creation)
    //   → GET  /auth/session            (authenticated)
    //
    // Unlike the Phase-6 seeded-token tests, this proof calls the request endpoint
    // first and retrieves the token the handler generated — closing the round-trip gap.
    let mut state = test_state_with_accounts()?;
    state.config.auth_public_login = true;
    state.config.app_base_url = Some("http://localhost".to_string());

    let app = app(state.clone());

    // Step 1: POST /auth/magic-link/request — token is generated inside the handler
    let req = Request::post("/auth/magic-link/request")
        .header("Content-Type", "application/json")
        .body(body::Body::from(r#"{"email":"u1@example.com"}"#))?;
    let res = app.clone().oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);
    let body_bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let body_json: serde_json::Value = serde_json::from_slice(&body_bytes)?;
    assert_eq!(body_json["ok"], true);

    // Step 2: Retrieve the token generated by the request handler
    let token = state
        .tokens
        .latest_raw_for_email("u1@example.com")
        .context("token must be present in store after POST /auth/magic-link/request")?;

    // Step 3: GET /auth/magic-link/consume — confirm page, sets nonce cookie
    let uri = format!("/auth/magic-link/consume?token={}", token);
    let req_get = Request::get(&uri).body(body::Body::empty())?;
    let res_get = app.clone().oneshot(req_get).await?;
    assert_eq!(res_get.status(), StatusCode::OK);

    let nonce_val = extract_cookie_value(res_get.headers(), NONCE_COOKIE_NAME)
        .context("nonce cookie not set by GET consume")?;
    let (_, nonce) = nonce_val.split_once('.').context("invalid nonce format")?;
    let nonce = nonce.to_string();

    // Step 4: POST /auth/magic-link/consume — creates session, sets session cookie
    let body_str = format!("token={}&nonce={}", token, nonce);
    let req_post = Request::post("/auth/magic-link/consume")
        .header("Content-Type", "application/x-www-form-urlencoded")
        .header("Cookie", format!("{}={}", NONCE_COOKIE_NAME, nonce_val))
        .body(body::Body::from(body_str))?;
    let res_post = app.oneshot(req_post).await?;
    assert_eq!(res_post.status(), StatusCode::SEE_OTHER);
    assert_eq!(res_post.headers().get("location").unwrap(), "/");

    let session_id = extract_cookie_value(res_post.headers(), SESSION_COOKIE_NAME)
        .context("session cookie must be set after POST consume")?;

    // Step 5: GET /auth/session — session cookie must authenticate
    let req_session = Request::get("/auth/session")
        .header("Host", "localhost")
        .header("Cookie", format!("{}={}", SESSION_COOKIE_NAME, session_id))
        .body(body::Body::empty())?;
    let res_session = app_with_auth(state).oneshot(req_session).await?;
    assert_eq!(res_session.status(), StatusCode::OK);

    let body_bytes = body::to_bytes(res_session.into_body(), usize::MAX).await?;
    let body_json: serde_json::Value = serde_json::from_slice(&body_bytes)?;
    assert_eq!(body_json["authenticated"], true);
    assert!(
        body_json["expires_at"].as_str().is_some(),
        "expires_at must be present in authenticated session response"
    );
    assert!(
        body_json["device_id"].as_str().is_some(),
        "device_id must be present in authenticated session response"
    );

    Ok(())
}

fn mock_passkey_with_credential_id(
    credential_id_b64: &str,
) -> Result<webauthn_rs::prelude::Passkey> {
    let passkey: webauthn_rs::prelude::Passkey = serde_json::from_value(serde_json::json!({
        "cred": {
            "cred_id": credential_id_b64,
            "cred": {
                "type_": "ES256",
                "key": {
                    "EC_EC2": {
                        "curve": "SECP256R1",
                        "x": vec![1_u8; 32],
                        "y": vec![2_u8; 32]
                    }
                }
            },
            "counter": 0,
            "transports": null,
            "user_verified": false,
            "backup_eligible": false,
            "backup_state": false,
            "registration_policy": "preferred",
            "extensions": {
                "cred_protect": "NotRequested",
                "hmac_create_secret": "NotRequested"
            },
            "attestation": {
                "data": "None",
                "metadata": "None"
            },
            "attestation_format": "None"
        }
    }))?;
    Ok(passkey)
}

#[tokio::test]
async fn passkey_register_options_excludes_existing_credentials() -> Result<()> {
    use base64::engine::general_purpose::URL_SAFE_NO_PAD;
    use base64::Engine;

    let state = test_state_with_webauthn()?;

    let credential_id = vec![42_u8; 32];
    let credential_id_b64 = URL_SAFE_NO_PAD.encode(&credential_id);
    let passkey = mock_passkey_with_credential_id(&credential_id_b64)?;

    let expected_cred_id = passkey.cred_id().clone();
    state
        .passkeys
        .insert("u1".to_string(), passkey)
        .context("mock passkey insert should succeed")?;

    let session = create_session(&state, "u1", Some("dev-passkey")).await;
    let cookie = format!("{}={}", SESSION_COOKIE_NAME, session.id);

    let challenge_id = request_passkey_registration_challenge(&state, &cookie).await?;

    let grant_id = consume_begin_passkey_registration_step_up(
        &state,
        &cookie,
        &challenge_id,
        &session.account_id,
        &session.device_id,
    )
    .await?;

    let app = app_with_auth(state.clone());
    let req = Request::post("/auth/passkeys/register/options")
        .header("Host", "localhost")
        .header("Origin", "http://localhost:3000")
        .header("Content-Type", "application/json")
        .header("Cookie", cookie)
        .body(body::Body::from(
            serde_json::json!({"registration_grant_id": grant_id}).to_string(),
        ))?;
    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);

    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let body: serde_json::Value = serde_json::from_slice(&bytes)?;

    assert!(body.get("registration_id").is_some());
    assert!(body.get("options").is_some());

    let exclude_credentials = body["options"]["publicKey"]["excludeCredentials"]
        .as_array()
        .context("excludeCredentials must be an array")?;
    assert_eq!(
        exclude_credentials.len(),
        1,
        "must exclude exactly one credential"
    );

    let id_b64 = exclude_credentials[0]["id"]
        .as_str()
        .context("id must be string")?;
    let decoded_id = URL_SAFE_NO_PAD.decode(id_b64)?;
    assert_eq!(decoded_id, expected_cred_id);

    Ok(())
}

// ── Passkey Login (Authentication) backend — PR 1 ─────────────────────────
//
// Backend-only slice: the *positive* (real-assertion) path is intentionally
// left to the browser / virtual-authenticator proof (PR 2). These tests cover
// the option ceremony, the fail-closed error paths, single-use state, and the
// hard cookie invariant: a session cookie is set ONLY on a successful verify.

/// Start a real login ceremony via `auth/options` and return its
/// `authentication_id`. The account must already own a passkey.
async fn start_auth_ceremony(app: &Router, email: &str) -> Result<String> {
    let req = Request::post("/auth/passkeys/auth/options")
        .header("content-type", "application/json")
        .body(body::Body::from(format!(r#"{{"email":"{email}"}}"#)))?;
    let res = app.clone().oneshot(req).await?;
    assert_eq!(
        res.status(),
        StatusCode::OK,
        "auth/options must succeed to start a ceremony"
    );
    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let json: serde_json::Value = serde_json::from_slice(&bytes)?;
    Ok(json["authentication_id"]
        .as_str()
        .context("authentication_id must be present")?
        .to_string())
}

fn seed_passkey(state: &ApiState, account_id: &str, seed: u8) -> Result<()> {
    use base64::engine::general_purpose::URL_SAFE_NO_PAD;
    use base64::Engine;
    let cred_id_b64 = URL_SAFE_NO_PAD.encode([seed; 32]);
    state
        .passkeys
        .insert(
            account_id.to_string(),
            mock_passkey_with_credential_id(&cred_id_b64)?,
        )
        .expect("seed passkey must insert");
    Ok(())
}

/// `auth/options` returns options + an authentication_id and never sets a cookie.
#[tokio::test]
async fn passkey_auth_options_returns_options_without_cookie() -> Result<()> {
    let state = test_state_with_webauthn()?;
    seed_passkey(&state, "u1", 7)?;
    let app = app_with_auth(state);

    let req = Request::post("/auth/passkeys/auth/options")
        .header("content-type", "application/json")
        .body(body::Body::from(r#"{"email":"u1@example.com"}"#))?;
    let res = app.oneshot(req).await?;

    assert_eq!(res.status(), StatusCode::OK);
    assert!(
        !res.headers().contains_key(axum::http::header::SET_COOKIE),
        "auth/options must never set a session cookie"
    );
    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let json: serde_json::Value = serde_json::from_slice(&bytes)?;
    assert!(
        json.get("authentication_id")
            .and_then(|v| v.as_str())
            .is_some_and(|s| !s.is_empty()),
        "must return a non-empty authentication_id"
    );
    assert!(
        json.get("options").is_some(),
        "must return WebAuthn options"
    );
    Ok(())
}

/// `auth/options` fail-closes with 503 when WebAuthn is unconfigured (no cookie).
#[tokio::test]
async fn passkey_auth_options_requires_webauthn_config() -> Result<()> {
    let state = test_state_with_accounts()?; // webauthn is None
    let app = app_with_auth(state);

    let req = Request::post("/auth/passkeys/auth/options")
        .header("content-type", "application/json")
        .body(body::Body::from(r#"{"email":"u1@example.com"}"#))?;
    let res = app.oneshot(req).await?;

    assert_eq!(res.status(), StatusCode::SERVICE_UNAVAILABLE);
    assert!(!res.headers().contains_key(axum::http::header::SET_COOKIE));
    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let json: serde_json::Value = serde_json::from_slice(&bytes)?;
    assert_eq!(json["error"], "PASSKEYS_NOT_CONFIGURED");
    Ok(())
}

/// `auth/options` fail-closes uniformly for an unknown identifier *and* for a
/// known account without a passkey — same status, no cookie either way.
#[tokio::test]
async fn passkey_auth_options_no_credentials_fails_closed_without_cookie() -> Result<()> {
    let state = test_state_with_webauthn()?; // u1 exists but has NO passkey seeded
    let app = app_with_auth(state);

    // Known account, but without a registered passkey.
    let known_no_passkey = Request::post("/auth/passkeys/auth/options")
        .header("content-type", "application/json")
        .body(body::Body::from(r#"{"email":"u1@example.com"}"#))?;
    let res = app.clone().oneshot(known_no_passkey).await?;
    assert_eq!(res.status(), StatusCode::NOT_FOUND);
    assert!(!res.headers().contains_key(axum::http::header::SET_COOKIE));

    // Entirely unknown identifier — indistinguishable response.
    let unknown = Request::post("/auth/passkeys/auth/options")
        .header("content-type", "application/json")
        .body(body::Body::from(r#"{"email":"nobody@example.com"}"#))?;
    let res2 = app.oneshot(unknown).await?;
    assert_eq!(res2.status(), StatusCode::NOT_FOUND);
    assert!(!res2.headers().contains_key(axum::http::header::SET_COOKIE));
    Ok(())
}

/// `auth/options` rejects a malformed/empty request without a cookie.
#[tokio::test]
async fn passkey_auth_options_rejects_malformed_request_without_cookie() -> Result<()> {
    let state = test_state_with_webauthn()?;
    let app = app_with_auth(state);

    let req = Request::post("/auth/passkeys/auth/options")
        .header("content-type", "application/json")
        .body(body::Body::from(r#"{"email":""}"#))?;
    let res = app.oneshot(req).await?;

    assert_eq!(res.status(), StatusCode::BAD_REQUEST);
    assert!(!res.headers().contains_key(axum::http::header::SET_COOKIE));
    Ok(())
}

/// `auth/verify` fail-closes with 503 when WebAuthn is unconfigured (no cookie).
#[tokio::test]
async fn passkey_auth_verify_requires_webauthn_config() -> Result<()> {
    let state = test_state_with_accounts()?; // webauthn is None
    let app = app_with_auth(state);

    let req = Request::post("/auth/passkeys/auth/verify")
        .header("content-type", "application/json")
        .body(body::Body::from(
            r#"{"authentication_id":"x","credential":{}}"#,
        ))?;
    let res = app.oneshot(req).await?;

    assert_eq!(res.status(), StatusCode::SERVICE_UNAVAILABLE);
    assert!(!res.headers().contains_key(axum::http::header::SET_COOKIE));
    Ok(())
}

/// `auth/verify` with an unknown authentication_id is rejected without a cookie.
#[tokio::test]
async fn passkey_auth_verify_unknown_state_rejected_without_cookie() -> Result<()> {
    let state = test_state_with_webauthn()?;
    let app = app_with_auth(state);

    let req = Request::post("/auth/passkeys/auth/verify")
        .header("content-type", "application/json")
        .body(body::Body::from(
            r#"{"authentication_id":"does-not-exist","credential":{}}"#,
        ))?;
    let res = app.oneshot(req).await?;

    assert_eq!(res.status(), StatusCode::BAD_REQUEST);
    assert!(!res.headers().contains_key(axum::http::header::SET_COOKIE));
    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let json: serde_json::Value = serde_json::from_slice(&bytes)?;
    assert_eq!(json["error"], "AUTHENTICATION_INVALID");
    Ok(())
}

/// `auth/verify` with a malformed assertion over a real, freshly issued state is
/// rejected as CREDENTIAL_INVALID without a cookie (and consumes the state).
#[tokio::test]
async fn passkey_auth_verify_invalid_assertion_rejected_without_cookie() -> Result<()> {
    let state = test_state_with_webauthn()?;
    seed_passkey(&state, "u1", 9)?;
    let app = app_with_auth(state);

    let auth_id = start_auth_ceremony(&app, "u1@example.com").await?;

    let req = Request::post("/auth/passkeys/auth/verify")
        .header("content-type", "application/json")
        .body(body::Body::from(format!(
            r#"{{"authentication_id":"{auth_id}","credential":{{"not":"a-real-assertion"}}}}"#
        )))?;
    let res = app.oneshot(req).await?;

    assert_eq!(res.status(), StatusCode::BAD_REQUEST);
    assert!(!res.headers().contains_key(axum::http::header::SET_COOKIE));
    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let json: serde_json::Value = serde_json::from_slice(&bytes)?;
    assert_eq!(json["error"], "CREDENTIAL_INVALID");
    Ok(())
}

/// `auth/verify` consumes the authentication state single-use: a second attempt
/// with the same authentication_id is rejected as unknown, without a cookie.
#[tokio::test]
async fn passkey_auth_verify_reused_state_rejected_without_cookie() -> Result<()> {
    let state = test_state_with_webauthn()?;
    seed_passkey(&state, "u1", 11)?;
    let app = app_with_auth(state);

    let auth_id = start_auth_ceremony(&app, "u1@example.com").await?;

    // First verify consumes the state (and fails the bogus assertion).
    let first = Request::post("/auth/passkeys/auth/verify")
        .header("content-type", "application/json")
        .body(body::Body::from(format!(
            r#"{{"authentication_id":"{auth_id}","credential":{{}}}}"#
        )))?;
    let res1 = app.clone().oneshot(first).await?;
    assert_eq!(res1.status(), StatusCode::BAD_REQUEST);
    assert!(!res1.headers().contains_key(axum::http::header::SET_COOKIE));
    let b1 = body::to_bytes(res1.into_body(), usize::MAX).await?;
    let j1: serde_json::Value = serde_json::from_slice(&b1)?;
    assert_eq!(j1["error"], "CREDENTIAL_INVALID");

    // Second verify with the same id is rejected as unknown (single-use).
    let second = Request::post("/auth/passkeys/auth/verify")
        .header("content-type", "application/json")
        .body(body::Body::from(format!(
            r#"{{"authentication_id":"{auth_id}","credential":{{}}}}"#
        )))?;
    let res2 = app.oneshot(second).await?;
    assert_eq!(res2.status(), StatusCode::BAD_REQUEST);
    assert!(!res2.headers().contains_key(axum::http::header::SET_COOKIE));
    let b2 = body::to_bytes(res2.into_body(), usize::MAX).await?;
    let j2: serde_json::Value = serde_json::from_slice(&b2)?;
    assert_eq!(j2["error"], "AUTHENTICATION_INVALID");
    Ok(())
}

fn disabled_account(id: &str, email: &str) -> AccountInternal {
    AccountInternal {
        public: AccountPublic {
            id: id.to_string(),
            kind: "garnrolle".to_string(),
            title: "Disabled User".to_string(),
            summary: None,
            public_pos: None,
            map_state: weltgewebe_api::routes::accounts::GarnrolleMapState::NotOnMap,
            radius_m: 0,
            disabled: true,
            tags: vec![],
        },
        role: Role::Gast,
        email: Some(email.to_string()),
        webauthn_user_id: uuid::Uuid::new_v4(),
    }
}

/// A disabled account that *owns a passkey* must still fail-close at
/// `auth/options` — folded into the uniform no-credentials 404, no cookie — so
/// a disabled account can neither start a ceremony nor be distinguished.
#[tokio::test]
async fn passkey_auth_options_rejects_disabled_account_without_cookie() -> Result<()> {
    let state = test_state_with_webauthn()?;
    seed_passkey(&state, "u1", 13)?;
    // Disable u1 (insert replaces by id); the passkey in PasskeyStore remains.
    state
        .accounts
        .write()
        .await
        .insert(disabled_account("u1", "u1@example.com"));
    let app = app_with_auth(state);

    let req = Request::post("/auth/passkeys/auth/options")
        .header("content-type", "application/json")
        .body(body::Body::from(r#"{"email":"u1@example.com"}"#))?;
    let res = app.oneshot(req).await?;

    assert_eq!(
        res.status(),
        StatusCode::NOT_FOUND,
        "disabled account must fold into the uniform no-credentials response"
    );
    assert!(!res.headers().contains_key(axum::http::header::SET_COOKIE));
    Ok(())
}

/// `auth/options` is rate-limited before it creates any WebAuthn state, and the
/// throttled response carries no cookie.
#[tokio::test]
async fn passkey_auth_options_is_rate_limited_without_cookie() -> Result<()> {
    let mut state = test_state_with_webauthn()?;
    state.config.auth_rl_ip_per_min = Some(1);
    state.rate_limiter = Arc::new(AuthRateLimiter::new(&state.config));
    seed_passkey(&state, "u1", 15)?;
    let app = app_with_auth(state);

    let req = || {
        Request::post("/auth/passkeys/auth/options")
            .header("content-type", "application/json")
            .body(body::Body::from(r#"{"email":"u1@example.com"}"#))
    };

    // First request within the per-minute IP budget succeeds.
    let res1 = app.clone().oneshot(req()?).await?;
    assert_eq!(res1.status(), StatusCode::OK);

    // Second request from the same IP is throttled — and sets no cookie.
    let res2 = app.oneshot(req()?).await?;
    assert_eq!(res2.status(), StatusCode::TOO_MANY_REQUESTS);
    assert!(!res2.headers().contains_key(axum::http::header::SET_COOKIE));
    Ok(())
}

/// `auth/verify` is rate-limited *before* it consumes the single-use state or
/// runs any WebAuthn crypto: a throttled verify returns `429`, sets no cookie,
/// and leaves the authentication state intact.
#[tokio::test]
async fn passkey_auth_verify_is_rate_limited_without_consuming_state() -> Result<()> {
    let mut state = test_state_with_webauthn()?;
    state.config.auth_rl_ip_per_min = Some(1);
    state.rate_limiter = Arc::new(AuthRateLimiter::new(&state.config));
    seed_passkey(&state, "u1", 17)?;
    let auth_store = state.passkey_authentications.clone();
    let app = app_with_auth(state);

    // The single per-minute IP token is spent by auth/options, which yields a
    // real authentication_id.
    let auth_id = start_auth_ceremony(&app, "u1@example.com").await?;

    // auth/verify is now over the IP budget: throttled, no cookie.
    let req = Request::post("/auth/passkeys/auth/verify")
        .header("content-type", "application/json")
        .body(body::Body::from(format!(
            r#"{{"authentication_id":"{auth_id}","credential":{{}}}}"#
        )))?;
    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::TOO_MANY_REQUESTS);
    assert!(!res.headers().contains_key(axum::http::header::SET_COOKIE));

    // Crucially, the rate-limited verify must NOT have consumed the state.
    assert!(
        auth_store.consume(&auth_id).await.is_some(),
        "a rate-limited verify must not consume the single-use authentication state"
    );
    Ok(())
}

/// Syntactically invalid JSON to `auth/options` yields the uniform JSON
/// `400 INVALID_REQUEST` (not a plaintext axum rejection) and no cookie.
#[tokio::test]
async fn passkey_auth_options_rejects_malformed_json_with_json_error_without_cookie() -> Result<()>
{
    let state = test_state_with_webauthn()?;
    let app = app_with_auth(state);

    let req = Request::post("/auth/passkeys/auth/options")
        .header("content-type", "application/json")
        .body(body::Body::from("{ this is not valid json"))?;
    let res = app.oneshot(req).await?;

    assert_eq!(res.status(), StatusCode::BAD_REQUEST);
    assert!(!res.headers().contains_key(axum::http::header::SET_COOKIE));
    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let json: serde_json::Value = serde_json::from_slice(&bytes)?;
    assert_eq!(
        json["error"], "INVALID_REQUEST",
        "malformed JSON must yield a structured JSON error"
    );
    Ok(())
}

/// Syntactically invalid JSON to `auth/verify` yields the uniform JSON
/// `400 INVALID_REQUEST` and no cookie.
#[tokio::test]
async fn passkey_auth_verify_rejects_malformed_json_with_json_error_without_cookie() -> Result<()> {
    let state = test_state_with_webauthn()?;
    let app = app_with_auth(state);

    let req = Request::post("/auth/passkeys/auth/verify")
        .header("content-type", "application/json")
        .body(body::Body::from("not json at all"))?;
    let res = app.oneshot(req).await?;

    assert_eq!(res.status(), StatusCode::BAD_REQUEST);
    assert!(!res.headers().contains_key(axum::http::header::SET_COOKIE));
    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let json: serde_json::Value = serde_json::from_slice(&bytes)?;
    assert_eq!(json["error"], "INVALID_REQUEST");
    Ok(())
}

/// A request with no body / no JSON content-type also yields the uniform JSON
/// `400 INVALID_REQUEST` (via `JsonRejection`), not a plaintext rejection, and
/// no cookie.
#[tokio::test]
async fn passkey_auth_options_rejects_missing_body_with_json_error_without_cookie() -> Result<()> {
    let state = test_state_with_webauthn()?;
    let app = app_with_auth(state);

    let req = Request::post("/auth/passkeys/auth/options").body(body::Body::empty())?;
    let res = app.oneshot(req).await?;

    assert_eq!(res.status(), StatusCode::BAD_REQUEST);
    assert!(!res.headers().contains_key(axum::http::header::SET_COOKIE));
    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let json: serde_json::Value = serde_json::from_slice(&bytes)?;
    assert_eq!(json["error"], "INVALID_REQUEST");
    Ok(())
}
