#[cfg(test)]
mod tests {
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
            accounts::AccountStore, rate_limit::AuthRateLimiter, role::Role,
            session::SessionBackend,
        },
        config::AppConfig,
        routes::{
            accounts::{AccountInternal, AccountPublic},
            api_router,
        },
        state::ApiState,
        telemetry::{BuildInfo, Metrics},
    };

    fn test_state_undeliverable() -> Result<ApiState> {
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
            auth_public_login: true, // Enabled
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
            smtp_host: None, // No SMTP
            smtp_port: None,
            smtp_user: None,
            smtp_pass: None,
            smtp_from: None,
            auth_log_magic_token: false, // No Dev Logging
            webauthn_rp_id: None,
            webauthn_rp_origin: None,
            webauthn_rp_name: None,
        };

        let rate_limiter = Arc::new(AuthRateLimiter::new(&config));

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
            accounts: Arc::new(RwLock::new(account_map)),
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
            mailer: None, // No Mailer instance
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
    async fn request_login_fails_503_if_undeliverable() -> Result<()> {
        let state = test_state_undeliverable()?;
        let app = app(state);

        let req = Request::post("/auth/magic-link/request")
            .header("Content-Type", "application/json")
            .body(body::Body::from(r#"{"email":"u1@example.com"}"#))?;

        let res = app.oneshot(req).await?;
        assert_eq!(res.status(), StatusCode::SERVICE_UNAVAILABLE);

        Ok(())
    }
}
