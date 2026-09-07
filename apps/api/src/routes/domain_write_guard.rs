use axum::http::StatusCode;

use crate::{
    config::{
        DomainAccountWriteSource, DomainEdgeWriteSource, DomainNodeWriteSource, DomainReadSource,
    },
    state::ApiState,
};

pub(super) const DOMAIN_READ_SOURCE_READ_ONLY: &str = "DOMAIN_READ_SOURCE_READ_ONLY";
pub(super) const DOMAIN_READ_SOURCE_READ_ONLY_MESSAGE: &str =
    "Domain mutation is blocked because WELTGEWEBE_DOMAIN_READ_SOURCE=postgres but the matching write source is not postgres; align the read and write source before retrying.";

pub(super) const INVALID_DOMAIN_WRITE_CONFIG: &str = "INVALID_DOMAIN_WRITE_CONFIG";
const INVALID_DOMAIN_WRITE_CONFIG_MESSAGE: &str =
    "domain_account_write_source=postgres requires domain_read_source=postgres";

const INVALID_NODE_WRITE_CONFIG_MESSAGE: &str =
    "domain_node_write_source=postgres requires domain_read_source=postgres";

const INVALID_EDGE_WRITE_CONFIG_MESSAGE: &str =
    "domain_edge_write_source=postgres requires domain_read_source=postgres";

fn read_only_conflict() -> (StatusCode, String) {
    (
        StatusCode::CONFLICT,
        format!("{DOMAIN_READ_SOURCE_READ_ONLY}: {DOMAIN_READ_SOURCE_READ_ONLY_MESSAGE}"),
    )
}

fn invalid_write_config() -> (StatusCode, String) {
    (
        StatusCode::INTERNAL_SERVER_ERROR,
        format!("{INVALID_DOMAIN_WRITE_CONFIG}: {INVALID_DOMAIN_WRITE_CONFIG_MESSAGE}"),
    )
}

/// Edge-create write gate (OPT-ARC-001 Phase E-C).
///
/// Behaviour matrix:
/// - JSONL read + JSONL edge write: allow (JSONL append path).
/// - Postgres read + Postgres edge write: allow (PostgreSQL insert path).
/// - Postgres read + JSONL edge write: reject — appending to JSONL under a
///   PostgreSQL read source would persist writes that vanish after a restart.
/// - JSONL read + Postgres edge write: reject defensively (config load also
///   forbids this); tests and internal code may construct `ApiState` manually.
pub(super) fn reject_edge_create_unless_writable(
    state: &ApiState,
) -> Result<(), (StatusCode, String)> {
    if state.config.domain_edge_write_source == DomainEdgeWriteSource::Postgres
        && state.config.domain_read_source != DomainReadSource::Postgres
    {
        return Err((
            StatusCode::INTERNAL_SERVER_ERROR,
            format!("{INVALID_DOMAIN_WRITE_CONFIG}: {INVALID_EDGE_WRITE_CONFIG_MESSAGE}"),
        ));
    }

    if state.config.domain_read_source == DomainReadSource::Postgres
        && state.config.domain_edge_write_source != DomainEdgeWriteSource::Postgres
    {
        return Err(read_only_conflict());
    }

    Ok(())
}

/// Node write gate for create and patch operations.
///
/// Behaviour matrix:
/// - JSONL read + JSONL node write: allow (JSONL rewrite path).
/// - Postgres read + Postgres node write: allow (PostgreSQL update path).
/// - Postgres read + JSONL node write: reject — rewriting JSONL under a
///   PostgreSQL read source would persist writes that vanish after a restart.
/// - JSONL read + Postgres node write: reject defensively (config load also
///   forbids this); tests and internal code may construct `ApiState` manually.
pub(super) fn reject_node_patch_unless_writable(
    state: &ApiState,
) -> Result<(), (StatusCode, String)> {
    if state.config.domain_node_write_source == DomainNodeWriteSource::Postgres
        && state.config.domain_read_source != DomainReadSource::Postgres
    {
        return Err((
            StatusCode::INTERNAL_SERVER_ERROR,
            format!("{INVALID_DOMAIN_WRITE_CONFIG}: {INVALID_NODE_WRITE_CONFIG_MESSAGE}"),
        ));
    }

    if state.config.domain_read_source == DomainReadSource::Postgres
        && state.config.domain_node_write_source != DomainNodeWriteSource::Postgres
    {
        return Err((
            StatusCode::CONFLICT,
            format!("{DOMAIN_READ_SOURCE_READ_ONLY}: {DOMAIN_READ_SOURCE_READ_ONLY_MESSAGE}"),
        ));
    }

    Ok(())
}

/// Node-create write gate (`POST /nodes`).
///
/// Shares the deliberately narrow node-write config surface with node-patch:
/// PostgreSQL node mutations are allowed only when both `domain_read_source`
/// and `domain_node_write_source` are PostgreSQL.
pub(super) fn reject_node_create_unless_writable(
    state: &ApiState,
) -> Result<(), (StatusCode, String)> {
    reject_node_patch_unless_writable(state)
}

/// Account-create write gate (OPT-ARC-001 Phase E-A).
///
/// Behaviour matrix:
/// - JSONL read + JSONL account write: allow (JSONL append path).
/// - Postgres read + Postgres account write: allow (PostgreSQL insert path).
/// - Postgres read + JSONL account write: reject — appending to JSONL under a
///   PostgreSQL read source would persist writes that vanish after a restart.
/// - JSONL read + Postgres account write: reject defensively (config load also
///   forbids this); tests and internal code may construct `ApiState` manually.
pub(super) fn reject_account_create_unless_writable(
    state: &ApiState,
) -> Result<(), (StatusCode, String)> {
    if state.config.domain_account_write_source == DomainAccountWriteSource::Postgres
        && state.config.domain_read_source != DomainReadSource::Postgres
    {
        return Err(invalid_write_config());
    }

    if state.config.domain_read_source == DomainReadSource::Postgres
        && state.config.domain_account_write_source != DomainAccountWriteSource::Postgres
    {
        return Err(read_only_conflict());
    }

    Ok(())
}

/// Account email-update write gate (AUTH-PG-001).
///
/// Shares the deliberately narrow account-write config surface with
/// account-create: PostgreSQL account mutations are allowed only when both
/// `domain_read_source` and `domain_account_write_source` are PostgreSQL.
pub(super) fn reject_account_email_update_unless_writable(
    state: &ApiState,
) -> Result<(), (StatusCode, String)> {
    reject_account_create_unless_writable(state)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        auth::{
            accounts::AccountStore, rate_limit::AuthRateLimiter, session::SessionBackend,
            tokens::TokenStore,
        },
        config::{
            AppConfig, DomainAccountWriteSource, DomainEdgeWriteSource, DomainNodeWriteSource,
            DomainReadSource,
        },
        state::ApiState,
        telemetry::{BuildInfo, Metrics},
    };
    use std::sync::Arc;
    use tokio::sync::{Mutex, RwLock};

    fn test_state(
        domain_read_source: DomainReadSource,
        domain_account_write_source: DomainAccountWriteSource,
    ) -> ApiState {
        test_state_with_node_write(
            domain_read_source,
            domain_account_write_source,
            DomainNodeWriteSource::Jsonl,
        )
    }

    fn test_state_with_node_write(
        domain_read_source: DomainReadSource,
        domain_account_write_source: DomainAccountWriteSource,
        domain_node_write_source: DomainNodeWriteSource,
    ) -> ApiState {
        test_state_with_edge_write(
            domain_read_source,
            domain_account_write_source,
            domain_node_write_source,
            DomainEdgeWriteSource::Jsonl,
        )
    }

    fn test_state_with_edge_write(
        domain_read_source: DomainReadSource,
        domain_account_write_source: DomainAccountWriteSource,
        domain_node_write_source: DomainNodeWriteSource,
        domain_edge_write_source: DomainEdgeWriteSource,
    ) -> ApiState {
        let metrics = Metrics::try_new(BuildInfo {
            version: "test",
            commit: "test",
            build_timestamp: "test",
        })
        .expect("metrics");

        let config = AppConfig {
            max_guest_owned_nodes: 1_000,
            domain_read_source,
            domain_account_write_source,
            domain_node_write_source,
            domain_edge_write_source,
            passkey_credential_source: crate::config::PasskeyCredentialSource::InMemory,
            auth_public_login: false,
            auth_cookie_secure: crate::config::auth_cookie_secure_env_override().unwrap_or(true),
            app_base_url: None,
            auth_trusted_proxies: None,
            auth_allow_emails: None,
            auth_allow_email_domains: None,
            auth_auto_provision: false,
            auth_auto_provision_role: crate::config::AutoProvisionRole::Gast,
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

        ApiState {
            db_pool: None,
            db_pool_configured: false,
            nats_client: None,
            nats_configured: false,
            config,
            metrics,
            sessions: SessionBackend::new_in_memory(),
            challenges: Default::default(),
            tokens: TokenStore::new(),
            step_up_tokens: crate::auth::step_up_tokens::StepUpTokenStore::new(),
            accounts: Arc::new(RwLock::new(AccountStore::new())),
            nodes: Arc::new(RwLock::new(crate::state::OrderedCache::new())),
            nodes_persist: Arc::new(Mutex::new(())),
            accounts_persist: Arc::new(Mutex::new(())),
            domain_projection_gate: std::sync::Arc::new(tokio::sync::RwLock::new(())),
            domain_projection_reload: std::sync::Arc::new(tokio::sync::Mutex::new(())),
            domain_projection_version: std::sync::Arc::new(std::sync::atomic::AtomicI64::new(0)),
            edges: Arc::new(RwLock::new(crate::state::OrderedCache::new())),
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

    #[test]
    fn jsonl_read_jsonl_write_allows_account_create() {
        let state = test_state(DomainReadSource::Jsonl, DomainAccountWriteSource::Jsonl);
        assert!(reject_account_create_unless_writable(&state).is_ok());
    }

    #[test]
    fn postgres_read_postgres_write_allows_account_create() {
        let state = test_state(
            DomainReadSource::Postgres,
            DomainAccountWriteSource::Postgres,
        );
        assert!(reject_account_create_unless_writable(&state).is_ok());
    }

    #[test]
    fn postgres_read_jsonl_write_rejects_read_only() {
        let state = test_state(DomainReadSource::Postgres, DomainAccountWriteSource::Jsonl);
        let err = reject_account_create_unless_writable(&state).unwrap_err();
        assert_eq!(err.0, StatusCode::CONFLICT);
        assert!(err.1.contains(DOMAIN_READ_SOURCE_READ_ONLY));
    }

    #[test]
    fn jsonl_read_postgres_write_rejects_invalid_config() {
        let state = test_state(DomainReadSource::Jsonl, DomainAccountWriteSource::Postgres);
        let err = reject_account_create_unless_writable(&state).unwrap_err();
        assert_eq!(err.0, StatusCode::INTERNAL_SERVER_ERROR);
        assert!(err.1.contains(INVALID_DOMAIN_WRITE_CONFIG));
        assert!(err.1.contains(INVALID_DOMAIN_WRITE_CONFIG_MESSAGE));
    }

    #[test]
    fn jsonl_read_jsonl_node_write_allows_patch() {
        let state = test_state_with_node_write(
            DomainReadSource::Jsonl,
            DomainAccountWriteSource::Jsonl,
            DomainNodeWriteSource::Jsonl,
        );
        assert!(reject_node_patch_unless_writable(&state).is_ok());
    }

    #[test]
    fn postgres_read_postgres_node_write_allows_patch() {
        let state = test_state_with_node_write(
            DomainReadSource::Postgres,
            DomainAccountWriteSource::Postgres,
            DomainNodeWriteSource::Postgres,
        );
        assert!(reject_node_patch_unless_writable(&state).is_ok());
    }

    #[test]
    fn postgres_read_jsonl_node_write_rejects_read_only() {
        let state = test_state_with_node_write(
            DomainReadSource::Postgres,
            DomainAccountWriteSource::Postgres,
            DomainNodeWriteSource::Jsonl,
        );
        let err = reject_node_patch_unless_writable(&state).unwrap_err();
        assert_eq!(err.0, StatusCode::CONFLICT);
        assert!(err.1.contains(DOMAIN_READ_SOURCE_READ_ONLY));
    }

    #[test]
    fn jsonl_read_postgres_node_write_rejects_invalid_config() {
        let state = test_state_with_node_write(
            DomainReadSource::Jsonl,
            DomainAccountWriteSource::Jsonl,
            DomainNodeWriteSource::Postgres,
        );
        let err = reject_node_patch_unless_writable(&state).unwrap_err();
        assert_eq!(err.0, StatusCode::INTERNAL_SERVER_ERROR);
        assert!(err.1.contains(INVALID_DOMAIN_WRITE_CONFIG));
        assert!(err.1.contains(INVALID_NODE_WRITE_CONFIG_MESSAGE));
    }

    #[test]
    fn jsonl_read_jsonl_edge_write_allows_create() {
        let state = test_state_with_edge_write(
            DomainReadSource::Jsonl,
            DomainAccountWriteSource::Jsonl,
            DomainNodeWriteSource::Jsonl,
            DomainEdgeWriteSource::Jsonl,
        );
        assert!(reject_edge_create_unless_writable(&state).is_ok());
    }

    #[test]
    fn postgres_read_postgres_edge_write_allows_create() {
        let state = test_state_with_edge_write(
            DomainReadSource::Postgres,
            DomainAccountWriteSource::Postgres,
            DomainNodeWriteSource::Postgres,
            DomainEdgeWriteSource::Postgres,
        );
        assert!(reject_edge_create_unless_writable(&state).is_ok());
    }

    #[test]
    fn postgres_read_jsonl_edge_write_rejects_read_only() {
        let state = test_state_with_edge_write(
            DomainReadSource::Postgres,
            DomainAccountWriteSource::Postgres,
            DomainNodeWriteSource::Postgres,
            DomainEdgeWriteSource::Jsonl,
        );
        let err = reject_edge_create_unless_writable(&state).unwrap_err();
        assert_eq!(err.0, StatusCode::CONFLICT);
        assert!(err.1.contains(DOMAIN_READ_SOURCE_READ_ONLY));
    }

    #[test]
    fn jsonl_read_postgres_edge_write_rejects_invalid_config() {
        let state = test_state_with_edge_write(
            DomainReadSource::Jsonl,
            DomainAccountWriteSource::Jsonl,
            DomainNodeWriteSource::Jsonl,
            DomainEdgeWriteSource::Postgres,
        );
        let err = reject_edge_create_unless_writable(&state).unwrap_err();
        assert_eq!(err.0, StatusCode::INTERNAL_SERVER_ERROR);
        assert!(err.1.contains(INVALID_DOMAIN_WRITE_CONFIG));
        assert!(err.1.contains(INVALID_EDGE_WRITE_CONFIG_MESSAGE));
    }
}
