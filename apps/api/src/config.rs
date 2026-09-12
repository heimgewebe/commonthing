use std::{env, fs, net::IpAddr, path::Path};

use anyhow::{Context, Result};
use ipnet::IpNet;
use serde::Deserialize;

macro_rules! apply_env_override {
    ($self:ident, $field:ident, $env_var:literal) => {
        if let Ok(value) = env::var($env_var) {
            match value.trim().parse() {
                Ok(parsed) => {
                    $self.$field = parsed;
                }
                Err(e) => {
                    tracing::warn!(
                        env_var = $env_var,
                        value = %value,
                        error = %e,
                        "failed to parse environment override; keeping configured value"
                    );
                }
            }
        }
    };
}

fn default_auth_cookie_secure() -> bool {
    true
}

fn default_max_guest_owned_nodes() -> usize {
    1_000
}

pub(crate) const FIXED_FADEN_FADE_DAYS: u32 = 7;

/// Read the optional startup override for the browser-cookie security scope.
/// Callers should capture this once while constructing configuration/state;
/// request handlers must use the stored configuration value.
pub fn auth_cookie_secure_env_override() -> Option<bool> {
    env::var("AUTH_COOKIE_SECURE").ok().map(|value| {
        let value = value.trim();
        value != "0" && !value.eq_ignore_ascii_case("false")
    })
}

macro_rules! apply_env_override_option {
    ($self:ident, $field:ident, $type:ty, $env_var:literal) => {
        if let Ok(value) = env::var($env_var) {
            if !value.trim().is_empty() {
                match value.trim().parse::<$type>() {
                    Ok(parsed) => {
                        $self.$field = Some(parsed);
                    }
                    Err(e) => {
                        tracing::warn!(
                            env_var = $env_var,
                            value = %value,
                            error = %e,
                            "failed to parse environment override; keeping configured value"
                        );
                    }
                }
            }
        }
    };
}

/// Proxy allowlist applied when `auth_trusted_proxies` is unset or empty.
///
/// Loopback only. A reverse proxy that reaches the API over a container bridge
/// network never presents a loopback peer address, so this default deliberately
/// trusts no real proxy.
pub const DEFAULT_TRUSTED_PROXIES: &str = "127.0.0.1,::1";

/// Explicit `auth_trusted_proxies` value declaring that no proxy is trusted.
///
/// Required (instead of simply leaving the value unset) for a directly exposed
/// API that runs public login, so "no proxy in front" is an operator decision
/// on the record rather than an accident of an absent variable.
pub const TRUSTED_PROXIES_NONE: &str = "none";

fn validate_trusted_proxy_declaration(value: &str) -> Result<()> {
    let value = value.trim();
    if value.eq_ignore_ascii_case(TRUSTED_PROXIES_NONE) {
        return Ok(());
    }

    let entries: Vec<_> = value
        .split(',')
        .map(str::trim)
        .filter(|entry| !entry.is_empty())
        .collect();
    if entries.is_empty() {
        anyhow::bail!(
            "AUTH_TRUSTED_PROXIES must contain IP addresses/CIDRs or the explicit value `{TRUSTED_PROXIES_NONE}`"
        );
    }

    for entry in entries {
        if entry.parse::<IpNet>().is_err() && entry.parse::<IpAddr>().is_err() {
            anyhow::bail!(
                "AUTH_TRUSTED_PROXIES contains invalid entry `{entry}`; expected an IP address, CIDR, or `{TRUSTED_PROXIES_NONE}`"
            );
        }
    }
    Ok(())
}

/// Selects where domain data is read from at startup.
///
/// JSONL remains the default read source and write truth. PostgreSQL is
/// explicitly opt-in via `WELTGEWEBE_DOMAIN_READ_SOURCE` or the
/// `domain_read_source` config-file key.
#[derive(Debug, Clone, Copy, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "lowercase")]
pub enum DomainReadSource {
    #[default]
    #[serde(alias = "file", alias = "files")]
    Jsonl,
    #[serde(alias = "pg", alias = "db")]
    Postgres,
}

impl DomainReadSource {
    fn parse_env_value(value: &str) -> Result<Self> {
        match value.trim().to_ascii_lowercase().as_str() {
            "jsonl" | "file" | "files" => Ok(Self::Jsonl),
            "postgres" | "pg" | "db" => Ok(Self::Postgres),
            other => anyhow::bail!(
                "invalid WELTGEWEBE_DOMAIN_READ_SOURCE value '{other}'; expected one of: jsonl, file, files, postgres, pg, db"
            ),
        }
    }
}

/// Selects where `PATCH /nodes` writes to.
///
/// OPT-ARC-001 Phase E-B: this is a deliberately narrow gate. It governs the
/// node-patch write path **only** — account writes, edge writes, step-up email
/// persistence and WebAuthn user-id writeback are out of scope and remain
/// unchanged. JSONL remains the default. PostgreSQL is opt-in via
/// `WELTGEWEBE_DOMAIN_NODE_WRITE_SOURCE` or the `domain_node_write_source`
/// config-file key, and additionally requires `domain_read_source=postgres`
/// (validated at config load) plus a live pool (validated at startup).
#[derive(Debug, Clone, Copy, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "lowercase")]
pub enum DomainNodeWriteSource {
    #[default]
    #[serde(alias = "file", alias = "files")]
    Jsonl,
    #[serde(alias = "pg", alias = "db")]
    Postgres,
}

impl DomainNodeWriteSource {
    fn parse_env_value(value: &str) -> Result<Self> {
        match value.trim().to_ascii_lowercase().as_str() {
            "jsonl" | "file" | "files" => Ok(Self::Jsonl),
            "postgres" | "pg" | "db" => Ok(Self::Postgres),
            other => anyhow::bail!(
                "invalid WELTGEWEBE_DOMAIN_NODE_WRITE_SOURCE value '{other}'; expected one of: jsonl, file, files, postgres, pg, db"
            ),
        }
    }
}

/// Selects where narrow account writes persist to.
///
/// OPT-ARC-001 Phase E-A: this is a deliberately narrow gate. It governs the
/// account-create and step-up email update write paths **only** — node writes,
/// edge writes and WebAuthn user-id writeback are out of scope and remain
/// unchanged. JSONL remains the default. PostgreSQL is opt-in via
/// `WELTGEWEBE_DOMAIN_ACCOUNT_WRITE_SOURCE` or the `domain_account_write_source`
/// config-file key, and additionally requires `domain_read_source=postgres`
/// (validated at config load) plus a live pool (validated at startup).
#[derive(Debug, Clone, Copy, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "lowercase")]
pub enum DomainAccountWriteSource {
    #[default]
    #[serde(alias = "file", alias = "files")]
    Jsonl,
    #[serde(alias = "pg", alias = "db")]
    Postgres,
}

impl DomainAccountWriteSource {
    fn parse_env_value(value: &str) -> Result<Self> {
        match value.trim().to_ascii_lowercase().as_str() {
            "jsonl" | "file" | "files" => Ok(Self::Jsonl),
            "postgres" | "pg" | "db" => Ok(Self::Postgres),
            other => anyhow::bail!(
                "invalid WELTGEWEBE_DOMAIN_ACCOUNT_WRITE_SOURCE value '{other}'; expected one of: jsonl, file, files, postgres, pg, db"
            ),
        }
    }
}

/// Selects how internal derived-Faden projections may persist.
///
/// After the PostgreSQL cutover, JSONL is a read-only fallback for runtime
/// configuration. `ReadOnly` is therefore the default; PostgreSQL is the only
/// configurable durable edge-write source. `Jsonl` remains temporarily as an
/// internal legacy/test value so the old persistence helpers stay
/// characterizable until their own removal slice. Successful runtime config
/// validation rejects that legacy value.
#[derive(Debug, Clone, Copy, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "snake_case")]
pub enum DomainEdgeWriteSource {
    #[default]
    #[serde(alias = "readonly", alias = "read-only", alias = "none")]
    ReadOnly,
    #[serde(alias = "file", alias = "files")]
    Jsonl,
    #[serde(alias = "pg", alias = "db")]
    Postgres,
}

impl DomainEdgeWriteSource {
    fn parse_env_value(value: &str) -> Result<Self> {
        match value.trim().to_ascii_lowercase().as_str() {
            "read_only" | "readonly" | "read-only" | "none" => Ok(Self::ReadOnly),
            "jsonl" | "file" | "files" => Ok(Self::Jsonl),
            "postgres" | "pg" | "db" => Ok(Self::Postgres),
            other => anyhow::bail!(
                "invalid WELTGEWEBE_DOMAIN_EDGE_WRITE_SOURCE value '{other}'; expected one of: read_only, readonly, read-only, none, postgres, pg, db"
            ),
        }
    }
}

/// Selects where registered WebAuthn/passkey credentials are stored.
///
/// AUTH-PG-002 Slice B: only the long-lived credential records are switched.
/// Registration/authentication ceremonies remain in-memory. PostgreSQL is
/// explicit opt-in via `WELTGEWEBE_PASSKEY_CREDENTIAL_SOURCE` or the
/// `passkey_credential_source` config-file key, and additionally requires
/// `domain_read_source=postgres` (validated at config load) plus a live pool
/// (validated at startup). No silent fallback is allowed.
#[derive(Debug, Clone, Copy, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "snake_case")]
pub enum PasskeyCredentialSource {
    #[default]
    #[serde(alias = "memory", alias = "mem", alias = "inmemory")]
    InMemory,
    #[serde(alias = "pg", alias = "db")]
    Postgres,
}

impl PasskeyCredentialSource {
    fn parse_env_value(value: &str) -> Result<Self> {
        match value.trim().to_ascii_lowercase().as_str() {
            "in_memory" | "memory" | "mem" => Ok(Self::InMemory),
            "postgres" | "pg" | "db" => Ok(Self::Postgres),
            other => anyhow::bail!(
                "invalid WELTGEWEBE_PASSKEY_CREDENTIAL_SOURCE value '{other}'; expected one of: in_memory, memory, mem, postgres, pg, db"
            ),
        }
    }
}

/// Role assigned to accounts created by auth auto-provisioning
/// (`AUTH_AUTO_PROVISION`). Deliberately narrow: only `gast` and `weber` are
/// valid targets. `admin` (and any other value) is refused at config load —
/// auto-provisioning must never be able to mint a privileged account. Default
/// is `gast`; `weber` requires a concrete email or domain allowlist (see
/// `AppConfig::validate`), so open registration can never provision `weber`.
#[derive(Debug, Clone, Copy, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "lowercase")]
pub enum AutoProvisionRole {
    #[default]
    Gast,
    Weber,
}

impl AutoProvisionRole {
    fn parse_env_value(value: &str) -> Result<Self> {
        match value.trim().to_ascii_lowercase().as_str() {
            "gast" => Ok(Self::Gast),
            "weber" => Ok(Self::Weber),
            other => anyhow::bail!(
                "invalid AUTH_AUTO_PROVISION_ROLE value '{other}'; expected one of: gast, weber \
                 (admin is never allowed for auto-provisioning)"
            ),
        }
    }

    pub fn as_role_str(self) -> &'static str {
        match self {
            Self::Gast => "gast",
            Self::Weber => "weber",
        }
    }
}

#[derive(Debug, Clone, Deserialize, PartialEq)]
pub struct NodeMutationRateLimitConfig {
    #[serde(default = "default_node_mutation_replace_per_minute")]
    pub replace_per_minute: u32,
    #[serde(default = "default_node_mutation_replace_per_hour")]
    pub replace_per_hour: u32,
    #[serde(default = "default_node_mutation_delete_per_minute")]
    pub delete_per_minute: u32,
    #[serde(default = "default_node_mutation_delete_per_hour")]
    pub delete_per_hour: u32,
    #[serde(default)]
    pub admin_emergency_bypass: bool,
}

impl Default for NodeMutationRateLimitConfig {
    fn default() -> Self {
        Self {
            replace_per_minute: default_node_mutation_replace_per_minute(),
            replace_per_hour: default_node_mutation_replace_per_hour(),
            delete_per_minute: default_node_mutation_delete_per_minute(),
            delete_per_hour: default_node_mutation_delete_per_hour(),
            admin_emergency_bypass: false,
        }
    }
}

const fn default_node_mutation_replace_per_minute() -> u32 {
    30
}
const fn default_node_mutation_replace_per_hour() -> u32 {
    300
}
const fn default_node_mutation_delete_per_minute() -> u32 {
    10
}
const fn default_node_mutation_delete_per_hour() -> u32 {
    50
}

#[derive(Debug, Clone, Deserialize, PartialEq)]
pub struct AppConfig {
    /// Maximum number of nodes a guest account may own at once. Loaded once
    /// at startup so request handlers never re-read process environment state.
    #[serde(default = "default_max_guest_owned_nodes")]
    pub max_guest_owned_nodes: usize,

    /// Domain data read source (JSONL default, PostgreSQL opt-in).
    #[serde(default)]
    pub domain_read_source: DomainReadSource,

    /// Account-domain write source (JSONL default, PostgreSQL opt-in).
    /// Governs `POST /accounts`, auth auto-provisioning and step-up
    /// `UpdateEmail` persistence.
    #[serde(default)]
    pub domain_account_write_source: DomainAccountWriteSource,

    /// Node write source (JSONL default, PostgreSQL opt-in).
    /// Governs `POST /nodes` and `PATCH /nodes/{id}`.
    #[serde(default)]
    pub domain_node_write_source: DomainNodeWriteSource,

    /// Edge-create write source (read-only default, PostgreSQL opt-in).
    /// Governs `POST /edges` and internal derived-Faden lifecycle projections.
    /// The legacy `Jsonl` value is retained only for internal persistence tests
    /// and is rejected by normal runtime configuration validation.
    #[serde(default)]
    pub domain_edge_write_source: DomainEdgeWriteSource,

    /// Registered passkey credential source (in-memory default, PostgreSQL opt-in).
    /// AUTH-PG-002 Slice B: governs long-lived WebAuthn credential records only.
    #[serde(default)]
    pub passkey_credential_source: PasskeyCredentialSource,

    // Public Login Configuration
    #[serde(default)]
    pub auth_public_login: bool,
    /// Whether auth cookies carry the Secure attribute. Captured once at
    /// configuration load so all auth routes and middleware share one scope.
    #[serde(default = "default_auth_cookie_secure")]
    pub auth_cookie_secure: bool,
    #[serde(default)]
    pub app_base_url: Option<String>,
    #[serde(default)]
    pub auth_trusted_proxies: Option<String>,

    // Entry Policy Configuration
    #[serde(default)]
    pub auth_allow_emails: Option<Vec<String>>,
    #[serde(default)]
    pub auth_allow_email_domains: Option<Vec<String>>,
    #[serde(default)]
    pub auth_auto_provision: bool,
    /// Role assigned to accounts created by auto-provisioning. Default `gast`;
    /// `weber` requires a concrete allowlist (see `validate`).
    #[serde(default)]
    pub auth_auto_provision_role: AutoProvisionRole,

    // Rate Limiting Configuration
    #[serde(default)]
    pub auth_rl_ip_per_min: Option<u32>,
    #[serde(default)]
    pub auth_rl_ip_per_hour: Option<u32>,
    #[serde(default)]
    pub auth_rl_email_per_min: Option<u32>,
    #[serde(default)]
    pub auth_rl_email_per_hour: Option<u32>,
    /// Separate, account-scoped limits for collective node mutations. These
    /// buckets never reuse login email or IP counters.
    #[serde(default)]
    pub node_mutation_rate_limits: NodeMutationRateLimitConfig,

    // SMTP Configuration
    #[serde(default)]
    pub smtp_host: Option<String>,
    #[serde(default)]
    pub smtp_port: Option<u16>,
    #[serde(default)]
    pub smtp_user: Option<String>,
    #[serde(default)]
    pub smtp_pass: Option<String>,
    #[serde(default)]
    pub smtp_from: Option<String>,

    // WebAuthn / Passkey Configuration
    #[serde(default)]
    pub webauthn_rp_id: Option<String>,
    #[serde(default)]
    pub webauthn_rp_origin: Option<String>,
    #[serde(default)]
    pub webauthn_rp_name: Option<String>,

    // Dev/Ops Configuration
    #[serde(default)]
    pub auth_log_magic_token: bool,
}

impl AppConfig {
    const DEFAULT_CONFIG: &'static str = include_str!("../../../configs/app.defaults.yml");

    fn parse_yaml(raw: &str) -> Result<Self> {
        let value: serde_yaml::Value =
            serde_yaml::from_str(raw).context("failed to parse YAML configuration")?;
        for removed_key in [
            "fade_days",
            "ron_days",
            "delegation_expire_days",
            "anonymize_opt_in",
        ] {
            let key = serde_yaml::Value::String(removed_key.to_string());
            if value
                .as_mapping()
                .is_some_and(|mapping| mapping.contains_key(&key))
            {
                match removed_key {
                    "fade_days" => anyhow::bail!(
                        "fade_days has been removed from runtime configuration; the lifetime of newly \
                         derived, unverzwirnte Fäden is the fixed constitutional value of \
                         {FIXED_FADEN_FADE_DAYS} days"
                    ),
                    "ron_days" => anyhow::bail!(concat!(
                        "ron_days has been removed from runtime configuration; ",
                        "no runtime RON retention consumer exists, so the setting must not be ",
                        "presented as operator-tunable"
                    )),
                    "delegation_expire_days" => anyhow::bail!(concat!(
                        "delegation_expire_days has been removed from runtime configuration; ",
                        "no runtime delegation-expiry consumer exists, so the setting must not be ",
                        "presented as operator-tunable"
                    )),
                    "anonymize_opt_in" => anyhow::bail!(concat!(
                        "anonymize_opt_in has been removed from runtime configuration; ",
                        "no runtime anonymization consumer exists, so the setting must not be ",
                        "presented as operator-tunable. The privacy commitment is declared in ",
                        "policies/retention.yml"
                    )),
                    _ => unreachable!(),
                }
            }
        }
        serde_yaml::from_value(value).context("failed to decode YAML configuration")
    }

    pub fn load() -> Result<Self> {
        let config = match env::var("APP_CONFIG_PATH") {
            Ok(path) => Self::load_from_path(&path).with_context(|| {
                format!(
                    "APP_CONFIG_PATH is set to '{}', but the configuration could not be loaded",
                    path
                )
            })?,
            Err(env::VarError::NotPresent) => Self::parse_yaml(Self::DEFAULT_CONFIG)
                .context("failed to parse embedded default configuration")?,
            Err(env::VarError::NotUnicode(value)) => {
                anyhow::bail!(
                    "APP_CONFIG_PATH is set but is not valid Unicode: {:?}",
                    value
                );
            }
        };

        config.apply_env_overrides()
    }

    pub fn load_from_path(path: impl AsRef<Path>) -> Result<Self> {
        let path = path.as_ref();
        let raw = fs::read_to_string(path)
            .with_context(|| format!("failed to read configuration file at {}", path.display()))?;
        let config = Self::parse_yaml(&raw)
            .with_context(|| format!("failed to parse configuration file at {}", path.display()))?;
        config.apply_env_overrides()
    }

    /// Parse configuration from a YAML string (primarily for tests).
    pub fn load_from_str(yaml: &str) -> Result<Self> {
        let config = Self::parse_yaml(yaml).context("failed to parse YAML configuration string")?;
        config.apply_env_overrides()
    }

    fn apply_env_overrides(mut self) -> Result<Self> {
        if env::var_os("HA_FADE_DAYS").is_some() {
            anyhow::bail!(
                "HA_FADE_DAYS has been removed; the lifetime of newly derived, unverzwirnte \
                 Fäden is the fixed constitutional value of {FIXED_FADEN_FADE_DAYS} days"
            );
        }
        if env::var_os("HA_RON_DAYS").is_some() {
            anyhow::bail!(concat!(
                "HA_RON_DAYS has been removed; ",
                "no runtime RON retention consumer exists for this setting"
            ));
        }
        if env::var_os("HA_DELEGATION_EXPIRE_DAYS").is_some() {
            anyhow::bail!(concat!(
                "HA_DELEGATION_EXPIRE_DAYS has been removed; ",
                "no runtime delegation-expiry consumer exists for this setting"
            ));
        }
        if env::var_os("HA_ANONYMIZE_OPT_IN").is_some() {
            anyhow::bail!(concat!(
                "HA_ANONYMIZE_OPT_IN has been removed; ",
                "no runtime anonymization consumer exists for this setting. ",
                "The privacy commitment is declared in policies/retention.yml, ",
                "not as an operator-tunable runtime switch"
            ));
        }
        apply_env_override!(self, max_guest_owned_nodes, "MAX_GUEST_OWNED_NODES");

        if let Ok(val) = env::var("WELTGEWEBE_DOMAIN_READ_SOURCE") {
            if !val.trim().is_empty() {
                self.domain_read_source = DomainReadSource::parse_env_value(&val)?;
            }
        }

        if let Ok(val) = env::var("WELTGEWEBE_DOMAIN_ACCOUNT_WRITE_SOURCE") {
            if !val.trim().is_empty() {
                self.domain_account_write_source = DomainAccountWriteSource::parse_env_value(&val)?;
            }
        }

        if let Ok(val) = env::var("WELTGEWEBE_DOMAIN_NODE_WRITE_SOURCE") {
            if !val.trim().is_empty() {
                self.domain_node_write_source = DomainNodeWriteSource::parse_env_value(&val)?;
            }
        }

        if let Ok(val) = env::var("WELTGEWEBE_DOMAIN_EDGE_WRITE_SOURCE") {
            if !val.trim().is_empty() {
                self.domain_edge_write_source = DomainEdgeWriteSource::parse_env_value(&val)?;
            }
        }

        if let Ok(val) = env::var("WELTGEWEBE_PASSKEY_CREDENTIAL_SOURCE") {
            if !val.trim().is_empty() {
                self.passkey_credential_source = PasskeyCredentialSource::parse_env_value(&val)?;
            }
        }

        // Auth Overrides
        if let Ok(val) = env::var("AUTH_PUBLIC_LOGIN") {
            let val = val.trim();
            self.auth_public_login = val == "1" || val.eq_ignore_ascii_case("true");
        }
        if let Some(value) = auth_cookie_secure_env_override() {
            self.auth_cookie_secure = value;
        }
        if let Ok(val) = env::var("APP_BASE_URL") {
            let val = val.trim();
            if !val.is_empty() {
                self.app_base_url = Some(val.to_string());
            }
        }
        if let Ok(val) = env::var("AUTH_TRUSTED_PROXIES") {
            let val = val.trim();
            if !val.is_empty() {
                self.auth_trusted_proxies = Some(val.to_string());
            }
        }

        // Entry Policy Overrides
        if let Ok(val) = env::var("AUTH_ALLOW_EMAILS") {
            let entries: Vec<String> = val
                .split(',')
                .map(|s| s.trim().to_ascii_lowercase())
                .filter(|s| !s.is_empty())
                .collect();
            if !entries.is_empty() {
                self.auth_allow_emails = Some(entries);
            }
        }
        if let Ok(val) = env::var("AUTH_ALLOW_EMAIL_DOMAINS") {
            let entries: Vec<String> = val
                .split(',')
                .map(|s| s.trim().to_ascii_lowercase())
                .filter(|s| !s.is_empty())
                .collect();
            if !entries.is_empty() {
                self.auth_allow_email_domains = Some(entries);
            }
        }
        if let Ok(val) = env::var("AUTH_AUTO_PROVISION") {
            let val = val.trim();
            self.auth_auto_provision = val == "1" || val.eq_ignore_ascii_case("true");
        }
        if let Ok(val) = env::var("AUTH_AUTO_PROVISION_ROLE") {
            if !val.trim().is_empty() {
                self.auth_auto_provision_role = AutoProvisionRole::parse_env_value(&val)?;
            }
        }

        // Rate Limit Overrides
        apply_env_override_option!(self, auth_rl_ip_per_min, u32, "AUTH_RL_IP_PER_MIN");
        apply_env_override_option!(self, auth_rl_ip_per_hour, u32, "AUTH_RL_IP_PER_HOUR");
        apply_env_override_option!(self, auth_rl_email_per_min, u32, "AUTH_RL_EMAIL_PER_MIN");
        apply_env_override_option!(self, auth_rl_email_per_hour, u32, "AUTH_RL_EMAIL_PER_HOUR");
        for (name, target) in [
            (
                "NODE_MUTATION_REPLACE_PER_MINUTE",
                &mut self.node_mutation_rate_limits.replace_per_minute,
            ),
            (
                "NODE_MUTATION_REPLACE_PER_HOUR",
                &mut self.node_mutation_rate_limits.replace_per_hour,
            ),
            (
                "NODE_MUTATION_DELETE_PER_MINUTE",
                &mut self.node_mutation_rate_limits.delete_per_minute,
            ),
            (
                "NODE_MUTATION_DELETE_PER_HOUR",
                &mut self.node_mutation_rate_limits.delete_per_hour,
            ),
        ] {
            if let Ok(raw) = env::var(name) {
                *target = raw
                    .trim()
                    .parse::<u32>()
                    .with_context(|| format!("invalid {name}: expected a non-negative integer"))?;
            }
        }
        if let Ok(raw) = env::var("NODE_MUTATION_ADMIN_EMERGENCY_BYPASS") {
            self.node_mutation_rate_limits.admin_emergency_bypass =
                match raw.trim().to_ascii_lowercase().as_str() {
                    "1" | "true" => true,
                    "0" | "false" => false,
                    other => anyhow::bail!(
                        "invalid NODE_MUTATION_ADMIN_EMERGENCY_BYPASS value '{other}'; expected true/false or 1/0"
                    ),
                };
        }

        // SMTP Overrides
        if let Ok(val) = env::var("SMTP_HOST") {
            if !val.trim().is_empty() {
                self.smtp_host = Some(val.trim().to_string());
            }
        }
        apply_env_override_option!(self, smtp_port, u16, "SMTP_PORT");
        if let Ok(val) = env::var("SMTP_USER") {
            if !val.trim().is_empty() {
                self.smtp_user = Some(val.trim().to_string());
            }
        }
        if let Ok(val) = env::var("SMTP_PASS") {
            if !val.trim().is_empty() {
                self.smtp_pass = Some(val.trim().to_string());
            }
        }
        if let Ok(val) = env::var("SMTP_FROM") {
            if !val.trim().is_empty() {
                self.smtp_from = Some(val.trim().to_string());
            }
        }

        // Dev/Ops Overrides
        if let Ok(val) = env::var("AUTH_LOG_MAGIC_TOKEN") {
            let val = val.trim();
            self.auth_log_magic_token = val == "1" || val.eq_ignore_ascii_case("true");
        }

        // WebAuthn Overrides
        if let Ok(val) = env::var("WEBAUTHN_RP_ID") {
            let val = val.trim();
            if !val.is_empty() {
                self.webauthn_rp_id = Some(val.to_string());
            }
        }
        if let Ok(val) = env::var("WEBAUTHN_RP_ORIGIN") {
            let val = val.trim();
            if !val.is_empty() {
                self.webauthn_rp_origin = Some(val.to_string());
            }
        }
        if let Ok(val) = env::var("WEBAUTHN_RP_NAME") {
            let val = val.trim();
            if !val.is_empty() {
                self.webauthn_rp_name = Some(val.to_string());
            }
        }

        self.normalize().validate()
    }

    fn normalize(mut self) -> Self {
        // Ensure empty vectors are treated as None to prevent empty allowlists
        // from being interpreted as "allowlist present but empty" (which is unsafe).
        if let Some(emails) = self.auth_allow_emails {
            let normalized: Vec<String> = emails
                .into_iter()
                .map(|s| s.trim().to_ascii_lowercase())
                .filter(|s| !s.is_empty())
                .collect();
            self.auth_allow_emails = if normalized.is_empty() {
                None
            } else {
                Some(normalized)
            };
        }

        if let Some(domains) = self.auth_allow_email_domains {
            let normalized: Vec<String> = domains
                .into_iter()
                .map(|s| s.trim().to_ascii_lowercase())
                .filter(|s| !s.is_empty())
                .collect();
            self.auth_allow_email_domains = if normalized.is_empty() {
                None
            } else {
                Some(normalized)
            };
        }

        // A blank `auth_trusted_proxies` is "unset", not "an empty allowlist".
        // Collapsing it here means `validate()` sees the same `None` whether the
        // value was omitted or written as an empty string in a config file.
        if let Some(proxies) = self.auth_trusted_proxies {
            let trimmed = proxies.trim().to_string();
            self.auth_trusted_proxies = if trimmed.is_empty() {
                None
            } else {
                Some(trimmed)
            };
        }

        self
    }

    /// True when at least one per-IP rate limit is configured and effective.
    ///
    /// A limit of `0` is treated as absent: `AuthRateLimiter::new` maps it to
    /// `NonZeroU32::new(0) == None` and installs no limiter for that window.
    fn has_ip_rate_limits(&self) -> bool {
        self.auth_rl_ip_per_min.unwrap_or(0) > 0 || self.auth_rl_ip_per_hour.unwrap_or(0) > 0
    }

    pub fn is_open_registration(&self) -> bool {
        self.auth_public_login
            && self.auth_auto_provision
            && self.auth_allow_emails.is_none()
            && self.auth_allow_email_domains.is_none()
            && self.auth_rl_ip_per_min.unwrap_or(0) > 0
            && self.auth_rl_ip_per_hour.unwrap_or(0) > 0
            && self.auth_rl_email_per_min.unwrap_or(0) > 0
            && self.auth_rl_email_per_hour.unwrap_or(0) > 0
    }

    fn validate(self) -> Result<Self> {
        if self.max_guest_owned_nodes == 0 {
            anyhow::bail!("MAX_GUEST_OWNED_NODES must be greater than zero");
        }
        for (name, value) in [
            (
                "NODE_MUTATION_REPLACE_PER_MINUTE",
                self.node_mutation_rate_limits.replace_per_minute,
            ),
            (
                "NODE_MUTATION_REPLACE_PER_HOUR",
                self.node_mutation_rate_limits.replace_per_hour,
            ),
            (
                "NODE_MUTATION_DELETE_PER_MINUTE",
                self.node_mutation_rate_limits.delete_per_minute,
            ),
            (
                "NODE_MUTATION_DELETE_PER_HOUR",
                self.node_mutation_rate_limits.delete_per_hour,
            ),
        ] {
            if value == 0 {
                anyhow::bail!(
                    "{name} must be greater than zero; use the explicit administrator emergency bypass instead of disabling collective mutation limits"
                );
            }
        }

        // OPT-ARC-001 Phase E-A: PostgreSQL account-create writes require the
        // PostgreSQL read source. Writing account creates to PostgreSQL while
        // reading accounts from JSONL would persist writes that are invisible
        // after a restart (the JSONL loader would never see them). This is a
        // hard config error, not a silent fallback.
        if self.domain_account_write_source == DomainAccountWriteSource::Postgres
            && self.domain_read_source != DomainReadSource::Postgres
        {
            anyhow::bail!(
                "domain_account_write_source=postgres requires domain_read_source=postgres \
                 (set WELTGEWEBE_DOMAIN_READ_SOURCE=postgres). Writing account creates to \
                 PostgreSQL while reading from JSONL would create restart-invisible writes."
            );
        }

        // OPT-ARC-001 Phase E-B: PostgreSQL node-patch writes require the
        // PostgreSQL read source. Writing node patches to PostgreSQL while
        // reading nodes from JSONL would persist writes that are invisible
        // after a restart. This is a hard config error, not a silent fallback.
        if self.domain_node_write_source == DomainNodeWriteSource::Postgres
            && self.domain_read_source != DomainReadSource::Postgres
        {
            anyhow::bail!(
                "domain_node_write_source=postgres requires domain_read_source=postgres \
                 (set WELTGEWEBE_DOMAIN_READ_SOURCE=postgres). Writing node patches to \
                 PostgreSQL while reading from JSONL would create restart-invisible writes."
            );
        }

        // Post-cutover runtime contract: JSONL edge writes are retired. Keep
        // the enum value temporarily for direct legacy-persistence tests, but
        // never allow a successfully loaded application configuration to
        // activate it.
        if self.domain_edge_write_source == DomainEdgeWriteSource::Jsonl {
            anyhow::bail!(
                "domain_edge_write_source=jsonl is retired after the PostgreSQL cutover; \
                 use domain_edge_write_source=read_only for JSONL fallback reads or postgres \
                 together with domain_read_source=postgres for durable edge writes"
            );
        }

        // OPT-ARC-001 Phase E-C: PostgreSQL edge-create writes require the
        // PostgreSQL read source. Writing edge creates to PostgreSQL while
        // reading edges from JSONL would persist writes that are invisible
        // after a restart. This is a hard config error, not a silent fallback.
        if self.domain_edge_write_source == DomainEdgeWriteSource::Postgres
            && self.domain_read_source != DomainReadSource::Postgres
        {
            anyhow::bail!(
                "domain_edge_write_source=postgres requires domain_read_source=postgres \
                 (set WELTGEWEBE_DOMAIN_READ_SOURCE=postgres). Writing edge creates to \
                 PostgreSQL while reading from JSONL would create restart-invisible writes."
            );
        }

        if self.passkey_credential_source == PasskeyCredentialSource::Postgres
            && self.domain_read_source != DomainReadSource::Postgres
        {
            anyhow::bail!(
                "passkey_credential_source=postgres requires domain_read_source=postgres \
                 (set WELTGEWEBE_DOMAIN_READ_SOURCE=postgres). Storing passkey credentials in \
                 PostgreSQL while reading accounts from JSONL would create restart-invisible \
                 credential bindings."
            );
        }

        if self.auth_public_login && self.app_base_url.is_none() {
            anyhow::bail!("AUTH_PUBLIC_LOGIN is enabled but APP_BASE_URL is not set. Please set APP_BASE_URL (e.g. https://mein-weltgewebe.de)");
        }

        // Any configured per-IP limiter needs an explicit client-IP trust
        // decision, regardless of whether registration is public. Otherwise a
        // containerised reverse proxy collapses every client into its own peer
        // address and turns the limiter into a shared outage switch.
        if self.has_ip_rate_limits() && self.auth_trusted_proxies.is_none() {
            anyhow::bail!(
                "Per-IP rate limits (AUTH_RL_IP_PER_MIN / AUTH_RL_IP_PER_HOUR) are enabled but \
                 AUTH_TRUSTED_PROXIES is not set. Without it the allowlist defaults to \
                 `{DEFAULT_TRUSTED_PROXIES}`, so a reverse proxy may be untrusted and every \
                 client may share one rate-limit bucket. Set AUTH_TRUSTED_PROXIES to the \
                 proxy's IP or CIDR (e.g. `127.0.0.1,::1,172.16.0.0/12` for Docker bridge \
                 networks), or to `{TRUSTED_PROXIES_NONE}` if the API is exposed directly."
            );
        }
        if let Some(proxies) = self.auth_trusted_proxies.as_deref() {
            validate_trusted_proxy_declaration(proxies)?;
        }

        if self.auth_auto_provision {
            let has_email_allowlist = self
                .auth_allow_emails
                .as_ref()
                .map(|v| !v.is_empty())
                .unwrap_or(false);
            let has_domain_allowlist = self
                .auth_allow_email_domains
                .as_ref()
                .map(|v| !v.is_empty())
                .unwrap_or(false);

            if !has_email_allowlist && !has_domain_allowlist {
                // Open Registration Mode (Option C)
                // Require all rate limits to be set and > 0
                let rl_ok = self.auth_rl_ip_per_min.unwrap_or(0) > 0
                    && self.auth_rl_ip_per_hour.unwrap_or(0) > 0
                    && self.auth_rl_email_per_min.unwrap_or(0) > 0
                    && self.auth_rl_email_per_hour.unwrap_or(0) > 0;

                if !rl_ok {
                    anyhow::bail!("AUTH_AUTO_PROVISION is enabled without allowlist, but strict rate limits are missing. Configure AUTH_RL_* variables > 0 to proceed.");
                } else {
                    tracing::info!("Starting with Open Registration (No Allowlist) and Strict Rate Limits active.");
                }

                // Open registration must never auto-provision a privileged role:
                // anyone reachable by rate limits alone would otherwise become weber.
                if self.auth_auto_provision_role == AutoProvisionRole::Weber {
                    anyhow::bail!(
                        "AUTH_AUTO_PROVISION_ROLE=weber requires a concrete AUTH_ALLOW_EMAILS or \
                         AUTH_ALLOW_EMAIL_DOMAINS allowlist. Open registration (no allowlist) must \
                         never auto-provision weber accounts."
                    );
                }
            } else {
                tracing::info!("Starting with Allowlist-restricted Registration.");
            }
        }

        if self.auth_public_login {
            // Check for valid SMTP configuration
            // Note: smtp_user/pass might be missing if SMTP_AUTH=off or auto (with no creds)
            // Mailer::new handles strict checks if SMTP_AUTH=on.
            // Here we check for delivery mechanism presence (HOST+FROM).
            let smtp_valid = self.smtp_host.is_some() && self.smtp_from.is_some();

            // Check if dev logging fallback is enabled
            let dev_logging = self.auth_log_magic_token;

            if dev_logging {
                tracing::warn!(
                    "SECURITY WARNING: AUTH_LOG_MAGIC_TOKEN is enabled. Do not use in production."
                );
            }

            if !smtp_valid && !dev_logging {
                anyhow::bail!("AUTH_PUBLIC_LOGIN is enabled but no delivery mechanism is configured. Configure SMTP_* or set AUTH_LOG_MAGIC_TOKEN=1 for dev.");
            }
        } else if self.smtp_host.is_some() && self.smtp_from.is_none() {
            // Only check this consistency if we didn't already bail above,
            // though the above check implies strictness when public login is on.
            // This clause catches "SMTP partially configured but Public Login OFF" edge cases.
            anyhow::bail!(
                "SMTP_HOST is set but SMTP_FROM is missing. Please set SMTP_FROM (e.g. noreply@example.com)."
            );
        }

        // WebAuthn Configuration Validation
        // rp_id and rp_origin must both be set (or both unset).
        // When set, rp_origin must be a valid URL whose host matches rp_id.
        let has_rp_id = self.webauthn_rp_id.is_some();
        let has_rp_origin = self.webauthn_rp_origin.is_some();
        if has_rp_id != has_rp_origin {
            anyhow::bail!(
                "WEBAUTHN_RP_ID and WEBAUTHN_RP_ORIGIN must both be set or both be unset."
            );
        }
        if let (Some(rp_id), Some(rp_origin)) = (&self.webauthn_rp_id, &self.webauthn_rp_origin) {
            let origin_url = url::Url::parse(rp_origin).map_err(|e| {
                anyhow::anyhow!("WEBAUTHN_RP_ORIGIN is not a valid URL: {rp_origin} ({e})")
            })?;

            // WEBAUTHN_RP_ORIGIN must be a strict Origin value: scheme + host [+ port].
            // Paths, queries, fragments and credentials are not valid Origin components.
            let scheme = origin_url.scheme();
            if scheme != "http" && scheme != "https" {
                anyhow::bail!(
                    "WEBAUTHN_RP_ORIGIN scheme must be 'http' or 'https', got '{scheme}'"
                );
            }
            if !origin_url.username().is_empty() || origin_url.password().is_some() {
                anyhow::bail!("WEBAUTHN_RP_ORIGIN must not contain credentials (userinfo)");
            }
            if origin_url.query().is_some() {
                anyhow::bail!("WEBAUTHN_RP_ORIGIN must not contain a query string");
            }
            if origin_url.fragment().is_some() {
                anyhow::bail!("WEBAUTHN_RP_ORIGIN must not contain a fragment");
            }
            let path = origin_url.path();
            if path != "/" && !path.is_empty() {
                anyhow::bail!("WEBAUTHN_RP_ORIGIN must not contain a path component, got '{path}'");
            }

            let origin_host = origin_url.host_str().unwrap_or("");
            // rp_id must equal the origin host, or the origin host must be a proper
            // subdomain of rp_id (i.e. host ends with ".<rp_id>").
            // A bare ends_with check is intentionally NOT used here because it would
            // accept "evil-example.com" when rp_id is "example.com".
            let host_matches =
                origin_host == rp_id.as_str() || origin_host.ends_with(&format!(".{rp_id}"));
            if !host_matches {
                anyhow::bail!(
                    "WEBAUTHN_RP_ORIGIN host '{origin_host}' does not match WEBAUTHN_RP_ID '{rp_id}'. \
                     The origin host must equal the RP ID or be a subdomain of it."
                );
            }
        }

        Ok(self)
    }
}

#[cfg(test)]
mod tests {
    use super::{
        AppConfig as RuntimeAppConfig, AutoProvisionRole, DomainAccountWriteSource,
        DomainEdgeWriteSource, DomainNodeWriteSource, DomainReadSource, PasskeyCredentialSource,
    };
    use crate::test_helpers::{DirGuard, EnvGuard};
    use anyhow::Result;
    use serial_test::serial;
    use std::path::Path;
    use tempfile::{tempdir, NamedTempFile};

    const YAML: &str = r#"
max_guest_owned_nodes: 1000
"#;

    // Config tests must not inherit a removed operator switch from the developer
    // or CI host. The explicit rejection test below bypasses this facade so it
    // still exercises the production fail-closed path.
    struct AppConfig;

    impl AppConfig {
        const DEFAULT_CONFIG: &'static str = RuntimeAppConfig::DEFAULT_CONFIG;

        fn load() -> Result<RuntimeAppConfig> {
            let _anonymize = EnvGuard::unset("HA_ANONYMIZE_OPT_IN");
            RuntimeAppConfig::load()
        }

        fn load_from_path(path: impl AsRef<Path>) -> Result<RuntimeAppConfig> {
            let _anonymize = EnvGuard::unset("HA_ANONYMIZE_OPT_IN");
            RuntimeAppConfig::load_from_path(path)
        }

        fn load_from_str(yaml: &str) -> Result<RuntimeAppConfig> {
            let _anonymize = EnvGuard::unset("HA_ANONYMIZE_OPT_IN");
            RuntimeAppConfig::load_from_str(yaml)
        }
    }

    #[test]
    #[serial]
    fn anonymize_opt_in_yaml_key_is_rejected_as_removed_setting() {
        let _anonymize = EnvGuard::unset("HA_ANONYMIZE_OPT_IN");
        let yaml = format!("anonymize_opt_in: true\n{YAML}");

        let error = AppConfig::load_from_str(&yaml)
            .expect_err("the removed anonymize_opt_in YAML key must fail closed");
        let error = format!("{error:#}");

        assert!(error.contains("anonymize_opt_in has been removed"));
        assert!(error.contains("no runtime anonymization consumer exists"));
        assert!(error.contains("policies/retention.yml"));
    }

    #[test]
    #[serial]
    fn anonymize_opt_in_env_override_is_rejected_as_removed_setting() {
        let _anonymize = EnvGuard::set("HA_ANONYMIZE_OPT_IN", "false");

        let error = RuntimeAppConfig::load_from_str(YAML)
            .expect_err("the removed HA_ANONYMIZE_OPT_IN override must fail closed");
        let error = format!("{error:#}");

        assert!(error.contains("HA_ANONYMIZE_OPT_IN has been removed"));
        assert!(error.contains("no runtime anonymization consumer exists"));
    }

    #[test]
    fn embedded_defaults_do_not_publish_anonymize_opt_in() {
        assert!(
            !AppConfig::DEFAULT_CONFIG.contains("anonymize_opt_in"),
            "the embedded defaults must not reintroduce the removed switch"
        );
    }

    #[test]
    #[serial]
    fn ron_days_yaml_key_is_rejected_as_removed_setting() {
        let _ron = EnvGuard::unset("HA_RON_DAYS");
        let yaml = format!("ron_days: 84\n{YAML}");

        let error = AppConfig::load_from_str(&yaml)
            .expect_err("the removed ron_days YAML key must fail closed");
        let error = format!("{error:#}");

        assert!(error.contains("ron_days has been removed"));
        assert!(error.contains("no runtime RON retention consumer exists"));
    }

    #[test]
    #[serial]
    fn ron_days_env_override_is_rejected_as_removed_setting() {
        let _ron = EnvGuard::set("HA_RON_DAYS", "84");

        let error = AppConfig::load_from_str(YAML)
            .expect_err("the removed HA_RON_DAYS override must fail closed");
        let error = format!("{error:#}");

        assert!(error.contains("HA_RON_DAYS has been removed"));
        assert!(error.contains("no runtime RON retention consumer exists"));
    }

    #[test]
    #[serial]
    fn delegation_expire_days_yaml_key_is_rejected_as_removed_setting() {
        let _delegation = EnvGuard::unset("HA_DELEGATION_EXPIRE_DAYS");
        let yaml = format!("delegation_expire_days: 28\n{YAML}");

        let error = AppConfig::load_from_str(&yaml)
            .expect_err("the removed delegation_expire_days YAML key must fail closed");
        let error = format!("{error:#}");

        assert!(error.contains("delegation_expire_days has been removed"));
        assert!(error.contains("no runtime delegation-expiry consumer exists"));
    }

    #[test]
    #[serial]
    fn delegation_expire_days_env_override_is_rejected_as_removed_setting() {
        let _delegation = EnvGuard::set("HA_DELEGATION_EXPIRE_DAYS", "28");

        let error = AppConfig::load_from_str(YAML)
            .expect_err("the removed HA_DELEGATION_EXPIRE_DAYS override must fail closed");
        let error = format!("{error:#}");

        assert!(error.contains("HA_DELEGATION_EXPIRE_DAYS has been removed"));
        assert!(error.contains("no runtime delegation-expiry consumer exists"));
    }

    #[test]
    #[serial]
    fn load_from_path_reads_defaults() -> Result<()> {
        let _fade = EnvGuard::unset("HA_FADE_DAYS");
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        let _config_path = EnvGuard::unset("APP_CONFIG_PATH");
        let _ron = EnvGuard::unset("HA_RON_DAYS");
        let _guest_nodes = EnvGuard::unset("MAX_GUEST_OWNED_NODES");
        let _cookie_secure = EnvGuard::unset("AUTH_COOKIE_SECURE");
        let _mutation_replace_min = EnvGuard::unset("NODE_MUTATION_REPLACE_PER_MINUTE");
        let _mutation_replace_hour = EnvGuard::unset("NODE_MUTATION_REPLACE_PER_HOUR");
        let _mutation_delete_min = EnvGuard::unset("NODE_MUTATION_DELETE_PER_MINUTE");
        let _mutation_delete_hour = EnvGuard::unset("NODE_MUTATION_DELETE_PER_HOUR");
        let _mutation_bypass = EnvGuard::unset("NODE_MUTATION_ADMIN_EMERGENCY_BYPASS");

        let cfg = AppConfig::load_from_path(file.path())?;
        assert_eq!(cfg.max_guest_owned_nodes, 1_000);
        assert!(cfg.auth_cookie_secure);
        assert_eq!(cfg.node_mutation_rate_limits.replace_per_minute, 30);
        assert_eq!(cfg.node_mutation_rate_limits.replace_per_hour, 300);
        assert_eq!(cfg.node_mutation_rate_limits.delete_per_minute, 10);
        assert_eq!(cfg.node_mutation_rate_limits.delete_per_hour, 50);
        assert!(!cfg.node_mutation_rate_limits.admin_emergency_bypass);

        Ok(())
    }

    #[test]
    #[serial]
    fn fade_days_yaml_key_is_rejected_as_removed_setting() {
        let _fade = EnvGuard::unset("HA_FADE_DAYS");
        let yaml = format!(
            "fade_days: 7
{YAML}"
        );

        let error = AppConfig::load_from_str(&yaml)
            .expect_err("the removed fade_days YAML key must fail closed");

        let error = format!("{error:#}");
        assert!(error.contains("fade_days has been removed"));
        assert!(error.contains("fixed constitutional value of 7 days"));
    }

    #[test]
    #[serial]
    fn fade_days_env_override_is_rejected_as_removed_setting() {
        let _fade = EnvGuard::set("HA_FADE_DAYS", "7");

        let error = AppConfig::load_from_str(YAML)
            .expect_err("the removed HA_FADE_DAYS override must fail closed");

        let error = format!("{error:#}");
        assert!(error.contains("HA_FADE_DAYS has been removed"));
        assert!(error.contains("fixed constitutional value of 7 days"));
    }

    #[test]
    #[serial]
    fn guest_node_limit_must_be_positive() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;
        let _limit = EnvGuard::set("MAX_GUEST_OWNED_NODES", "0");

        let error = AppConfig::load_from_path(file.path()).expect_err("zero guest node limit");
        assert!(error
            .to_string()
            .contains("MAX_GUEST_OWNED_NODES must be greater than zero"));
        Ok(())
    }

    #[test]
    #[serial]
    fn allowlist_is_ignored_if_empty_string() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        // Case 1: Empty string -> Should fail without rate limits
        let _auto = EnvGuard::set("AUTH_AUTO_PROVISION", "1");
        let _emails = EnvGuard::set("AUTH_ALLOW_EMAILS", "");
        let _domains = EnvGuard::unset("AUTH_ALLOW_EMAIL_DOMAINS");
        let _rl_ip_min = EnvGuard::unset("AUTH_RL_IP_PER_MIN");

        let res = AppConfig::load_from_path(file.path());
        assert!(res.is_err());
        assert!(res
            .unwrap_err()
            .to_string()
            .contains("strict rate limits are missing"));

        // Case 2: Comma only -> Should fail without rate limits
        let _emails_comma = EnvGuard::set("AUTH_ALLOW_EMAILS", ",,");
        let res2 = AppConfig::load_from_path(file.path());
        assert!(res2.is_err());

        Ok(())
    }

    #[test]
    #[serial]
    fn load_from_path_applies_env_overrides() -> Result<()> {
        let _fade = EnvGuard::unset("HA_FADE_DAYS");
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        let _config_path = EnvGuard::unset("APP_CONFIG_PATH");
        let _ron = EnvGuard::unset("HA_RON_DAYS");
        let _guest_nodes = EnvGuard::set("MAX_GUEST_OWNED_NODES", "321");
        let _cookie_secure = EnvGuard::set("AUTH_COOKIE_SECURE", "false");

        let cfg = AppConfig::load_from_path(file.path())?;
        assert_eq!(cfg.max_guest_owned_nodes, 321);
        assert!(!cfg.auth_cookie_secure);

        Ok(())
    }

    #[test]
    #[serial]
    fn node_mutation_rate_limit_env_overrides_are_separate() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;
        let _replace_min = EnvGuard::set("NODE_MUTATION_REPLACE_PER_MINUTE", "7");
        let _replace_hour = EnvGuard::set("NODE_MUTATION_REPLACE_PER_HOUR", "70");
        let _delete_min = EnvGuard::set("NODE_MUTATION_DELETE_PER_MINUTE", "3");
        let _delete_hour = EnvGuard::set("NODE_MUTATION_DELETE_PER_HOUR", "12");
        let _bypass = EnvGuard::set("NODE_MUTATION_ADMIN_EMERGENCY_BYPASS", "true");

        let cfg = AppConfig::load_from_path(file.path())?;
        assert_eq!(cfg.node_mutation_rate_limits.replace_per_minute, 7);
        assert_eq!(cfg.node_mutation_rate_limits.replace_per_hour, 70);
        assert_eq!(cfg.node_mutation_rate_limits.delete_per_minute, 3);
        assert_eq!(cfg.node_mutation_rate_limits.delete_per_hour, 12);
        assert!(cfg.node_mutation_rate_limits.admin_emergency_bypass);
        assert_eq!(cfg.auth_rl_email_per_min, None);
        assert_eq!(cfg.auth_rl_email_per_hour, None);
        Ok(())
    }

    #[test]
    #[serial]
    fn zero_node_mutation_limit_is_rejected() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;
        let _replace_min = EnvGuard::set("NODE_MUTATION_REPLACE_PER_MINUTE", "0");

        let error = AppConfig::load_from_path(file.path())
            .expect_err("zero collective mutation limit must fail closed");
        assert!(error
            .to_string()
            .contains("NODE_MUTATION_REPLACE_PER_MINUTE must be greater than zero"));
        Ok(())
    }

    #[test]
    #[serial]
    fn invalid_node_mutation_admin_bypass_fails_closed() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;
        let _bypass = EnvGuard::set("NODE_MUTATION_ADMIN_EMERGENCY_BYPASS", "enable-ish");

        let error = AppConfig::load_from_path(file.path())
            .expect_err("ambiguous admin bypass value must be rejected");
        assert!(error
            .to_string()
            .contains("invalid NODE_MUTATION_ADMIN_EMERGENCY_BYPASS"));
        Ok(())
    }

    #[test]
    #[serial]
    fn load_uses_defaults_when_app_config_path_is_unset() -> Result<()> {
        let _fade = EnvGuard::unset("HA_FADE_DAYS");
        let temp_dir = tempdir()?;
        let _dir = DirGuard::change_to(temp_dir.path())?;

        let _config_path = EnvGuard::unset("APP_CONFIG_PATH");
        let _ron = EnvGuard::unset("HA_RON_DAYS");
        let _read = EnvGuard::unset("WELTGEWEBE_DOMAIN_READ_SOURCE");
        let _account_write = EnvGuard::unset("WELTGEWEBE_DOMAIN_ACCOUNT_WRITE_SOURCE");
        let _node_write = EnvGuard::unset("WELTGEWEBE_DOMAIN_NODE_WRITE_SOURCE");
        let _edge_write = EnvGuard::unset("WELTGEWEBE_DOMAIN_EDGE_WRITE_SOURCE");

        let cfg = AppConfig::load()?;
        assert_eq!(cfg.domain_read_source, DomainReadSource::Jsonl);

        Ok(())
    }

    #[test]
    #[serial]
    fn load_errors_when_app_config_path_is_missing() -> Result<()> {
        let temp_dir = tempdir()?;
        let invalid_path = temp_dir.path().join("does-not-exist.yml");

        let _config_path = EnvGuard::set(
            "APP_CONFIG_PATH",
            invalid_path.to_str().expect("path is valid utf-8"),
        );
        let _read = EnvGuard::unset("WELTGEWEBE_DOMAIN_READ_SOURCE");
        let _account_write = EnvGuard::unset("WELTGEWEBE_DOMAIN_ACCOUNT_WRITE_SOURCE");
        let _node_write = EnvGuard::unset("WELTGEWEBE_DOMAIN_NODE_WRITE_SOURCE");
        let _edge_write = EnvGuard::unset("WELTGEWEBE_DOMAIN_EDGE_WRITE_SOURCE");

        let res = AppConfig::load();
        assert!(res.is_err());
        let err_msg = res.unwrap_err().to_string();
        assert!(err_msg.contains("APP_CONFIG_PATH is set to"));
        assert!(err_msg.contains("does-not-exist.yml"));

        Ok(())
    }

    #[test]
    #[serial]
    fn load_errors_when_app_config_path_is_not_a_file() -> Result<()> {
        let temp_dir = tempdir()?;

        // temp_dir.path() is a directory, not a file
        let _config_path = EnvGuard::set(
            "APP_CONFIG_PATH",
            temp_dir.path().to_str().expect("path is valid utf-8"),
        );
        let _read = EnvGuard::unset("WELTGEWEBE_DOMAIN_READ_SOURCE");
        let _account_write = EnvGuard::unset("WELTGEWEBE_DOMAIN_ACCOUNT_WRITE_SOURCE");
        let _node_write = EnvGuard::unset("WELTGEWEBE_DOMAIN_NODE_WRITE_SOURCE");
        let _edge_write = EnvGuard::unset("WELTGEWEBE_DOMAIN_EDGE_WRITE_SOURCE");

        let res = AppConfig::load();
        assert!(res.is_err());
        let err_msg = res.unwrap_err().to_string();
        // A set APP_CONFIG_PATH is operator intent; falling back to defaults would hide a deployment/configuration error.
        assert!(err_msg.contains("APP_CONFIG_PATH is set to"));

        Ok(())
    }

    #[test]
    #[serial]
    fn load_errors_when_app_config_path_yaml_is_invalid() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), "ron_days: [")?;

        let _config_path = EnvGuard::set(
            "APP_CONFIG_PATH",
            file.path().to_str().expect("path is valid utf-8"),
        );
        let _read = EnvGuard::unset("WELTGEWEBE_DOMAIN_READ_SOURCE");
        let _account_write = EnvGuard::unset("WELTGEWEBE_DOMAIN_ACCOUNT_WRITE_SOURCE");
        let _node_write = EnvGuard::unset("WELTGEWEBE_DOMAIN_NODE_WRITE_SOURCE");
        let _edge_write = EnvGuard::unset("WELTGEWEBE_DOMAIN_EDGE_WRITE_SOURCE");

        let res = AppConfig::load();
        assert!(res.is_err());
        let err_msg = res.unwrap_err().to_string();
        assert!(err_msg.contains("APP_CONFIG_PATH is set to"));

        Ok(())
    }

    #[test]
    #[serial]
    fn load_errors_when_explicit_config_fails_domain_write_validation() -> Result<()> {
        let file = NamedTempFile::new()?;
        let invalid_yaml = r#"
domain_read_source: jsonl
domain_account_write_source: postgres
"#;
        std::fs::write(file.path(), invalid_yaml)?;

        let _config_path = EnvGuard::set(
            "APP_CONFIG_PATH",
            file.path().to_str().expect("path is valid utf-8"),
        );
        let _read = EnvGuard::unset("WELTGEWEBE_DOMAIN_READ_SOURCE");
        let _account_write = EnvGuard::unset("WELTGEWEBE_DOMAIN_ACCOUNT_WRITE_SOURCE");
        let _node_write = EnvGuard::unset("WELTGEWEBE_DOMAIN_NODE_WRITE_SOURCE");
        let _edge_write = EnvGuard::unset("WELTGEWEBE_DOMAIN_EDGE_WRITE_SOURCE");

        let res = AppConfig::load();
        assert!(res.is_err());
        let err_msg = format!("{:?}", res.unwrap_err());
        assert!(err_msg
            .contains("domain_account_write_source=postgres requires domain_read_source=postgres"));

        Ok(())
    }

    #[test]
    #[serial]
    fn load_uses_explicit_app_config_path_when_valid() -> Result<()> {
        let _fade = EnvGuard::unset("HA_FADE_DAYS");
        let file = NamedTempFile::new()?;
        let valid_yaml = r#"
max_guest_owned_nodes: 1000
"#;
        std::fs::write(file.path(), valid_yaml)?;

        let _config_path = EnvGuard::set(
            "APP_CONFIG_PATH",
            file.path().to_str().expect("path is valid utf-8"),
        );
        let _ron = EnvGuard::unset("HA_RON_DAYS");
        let _read = EnvGuard::unset("WELTGEWEBE_DOMAIN_READ_SOURCE");
        let _account_write = EnvGuard::unset("WELTGEWEBE_DOMAIN_ACCOUNT_WRITE_SOURCE");
        let _node_write = EnvGuard::unset("WELTGEWEBE_DOMAIN_NODE_WRITE_SOURCE");
        let _edge_write = EnvGuard::unset("WELTGEWEBE_DOMAIN_EDGE_WRITE_SOURCE");

        let cfg = AppConfig::load()?;
        assert_eq!(cfg.domain_read_source, DomainReadSource::Jsonl);
        assert_eq!(
            cfg.domain_account_write_source,
            DomainAccountWriteSource::Jsonl
        );
        assert_eq!(cfg.domain_node_write_source, DomainNodeWriteSource::Jsonl);
        assert_eq!(
            cfg.domain_edge_write_source,
            DomainEdgeWriteSource::ReadOnly
        );

        Ok(())
    }

    #[test]
    #[serial]
    fn domain_edge_write_source_jsonl_yaml_is_rejected_as_retired() -> Result<()> {
        let _write = EnvGuard::unset("WELTGEWEBE_DOMAIN_EDGE_WRITE_SOURCE");
        let error = AppConfig::load_from_str(
            "max_guest_owned_nodes: 1000\ndomain_edge_write_source: jsonl\n",
        )
        .expect_err("runtime config file must not reactivate legacy JSONL edge writes");

        assert!(error
            .to_string()
            .contains("domain_edge_write_source=jsonl is retired"));
        Ok(())
    }

    #[test]
    #[serial]
    fn validation_fails_if_public_login_enabled_without_base_url() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        let _public = EnvGuard::set("AUTH_PUBLIC_LOGIN", "1");
        let _url = EnvGuard::unset("APP_BASE_URL");

        let res = AppConfig::load_from_path(file.path());
        assert!(res.is_err());
        assert!(res
            .unwrap_err()
            .to_string()
            .contains("APP_BASE_URL is not set"));

        Ok(())
    }

    #[test]
    #[serial]
    fn validation_succeeds_with_public_login_and_base_url() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        let _public = EnvGuard::set("AUTH_PUBLIC_LOGIN", "1");
        let _url = EnvGuard::set("APP_BASE_URL", "https://example.com");
        // Enable token logging to satisfy delivery requirement for this test
        let _log = EnvGuard::set("AUTH_LOG_MAGIC_TOKEN", "1");

        let cfg = AppConfig::load_from_path(file.path())?;
        assert!(cfg.auth_public_login);
        assert_eq!(cfg.app_base_url.unwrap(), "https://example.com");

        Ok(())
    }

    #[test]
    #[serial]
    fn auth_fields_default_correctly() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        // Ensure envs are unset
        let _public = EnvGuard::unset("AUTH_PUBLIC_LOGIN");
        let _url = EnvGuard::unset("APP_BASE_URL");
        let _proxies = EnvGuard::unset("AUTH_TRUSTED_PROXIES");
        let _emails = EnvGuard::unset("AUTH_ALLOW_EMAILS");
        let _domains = EnvGuard::unset("AUTH_ALLOW_EMAIL_DOMAINS");
        let _auto = EnvGuard::unset("AUTH_AUTO_PROVISION");

        let cfg = AppConfig::load_from_path(file.path())?;
        assert!(!cfg.auth_public_login);
        assert!(cfg.app_base_url.is_none());
        assert!(cfg.auth_trusted_proxies.is_none());
        assert!(cfg.auth_allow_emails.is_none());
        assert!(cfg.auth_allow_email_domains.is_none());
        assert!(!cfg.auth_auto_provision);

        Ok(())
    }

    #[test]
    #[serial]
    fn validation_fails_if_auto_provision_enabled_without_allowlist_and_no_limits() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        let _auto = EnvGuard::set("AUTH_AUTO_PROVISION", "1");
        let _emails = EnvGuard::unset("AUTH_ALLOW_EMAILS");
        let _domains = EnvGuard::unset("AUTH_ALLOW_EMAIL_DOMAINS");
        // Ensure rate limits are missing or zero
        let _rl_ip_min = EnvGuard::unset("AUTH_RL_IP_PER_MIN");

        let res = AppConfig::load_from_path(file.path());
        assert!(res.is_err());
        assert!(res
            .unwrap_err()
            .to_string()
            .contains("strict rate limits are missing"));

        Ok(())
    }

    #[test]
    #[serial]
    fn validation_succeeds_if_auto_provision_without_allowlist_and_strict_limits() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        let _auto = EnvGuard::set("AUTH_AUTO_PROVISION", "1");
        let _emails = EnvGuard::unset("AUTH_ALLOW_EMAILS");
        let _domains = EnvGuard::unset("AUTH_ALLOW_EMAIL_DOMAINS");

        // Set mandatory rate limits
        let _rl_ip_min = EnvGuard::set("AUTH_RL_IP_PER_MIN", "5");
        let _rl_ip_hour = EnvGuard::set("AUTH_RL_IP_PER_HOUR", "30");
        let _proxies = EnvGuard::set("AUTH_TRUSTED_PROXIES", "none");
        let _rl_email_min = EnvGuard::set("AUTH_RL_EMAIL_PER_MIN", "2");
        let _rl_email_hour = EnvGuard::set("AUTH_RL_EMAIL_PER_HOUR", "10");

        let cfg = AppConfig::load_from_path(file.path())?;
        assert!(cfg.auth_auto_provision);
        assert!(cfg.auth_allow_emails.is_none());
        assert!(cfg.auth_allow_email_domains.is_none());

        Ok(())
    }

    #[test]
    #[serial]
    fn validation_succeeds_with_auto_provision_and_email_allowlist() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        let _auto = EnvGuard::set("AUTH_AUTO_PROVISION", "1");
        let _emails = EnvGuard::set("AUTH_ALLOW_EMAILS", "test@example.com, foo@bar.com");
        let _domains = EnvGuard::unset("AUTH_ALLOW_EMAIL_DOMAINS");

        let cfg = AppConfig::load_from_path(file.path())?;
        assert!(cfg.auth_auto_provision);
        assert_eq!(cfg.auth_allow_emails.unwrap().len(), 2);

        Ok(())
    }

    #[test]
    #[serial]
    fn validation_succeeds_with_auto_provision_and_domain_allowlist() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        let _auto = EnvGuard::set("AUTH_AUTO_PROVISION", "1");
        let _emails = EnvGuard::unset("AUTH_ALLOW_EMAILS");
        let _domains = EnvGuard::set("AUTH_ALLOW_EMAIL_DOMAINS", "example.com");

        let cfg = AppConfig::load_from_path(file.path())?;
        assert!(cfg.auth_auto_provision);
        assert_eq!(cfg.auth_allow_email_domains.unwrap().len(), 1);

        Ok(())
    }

    #[test]
    #[serial]
    fn auth_auto_provision_role_defaults_to_gast() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        let _role = EnvGuard::unset("AUTH_AUTO_PROVISION_ROLE");
        let _auto = EnvGuard::unset("AUTH_AUTO_PROVISION");

        let cfg = AppConfig::load_from_path(file.path())?;
        assert_eq!(cfg.auth_auto_provision_role, AutoProvisionRole::Gast);

        Ok(())
    }

    #[test]
    #[serial]
    fn auth_auto_provision_role_rejects_admin() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        let _role = EnvGuard::set("AUTH_AUTO_PROVISION_ROLE", "admin");

        let res = AppConfig::load_from_path(file.path());
        assert!(res.is_err());
        let msg = res.unwrap_err().to_string();
        assert!(msg.contains("invalid AUTH_AUTO_PROVISION_ROLE value"));
        assert!(msg.contains("admin is never allowed"));

        Ok(())
    }

    #[test]
    #[serial]
    fn auth_auto_provision_role_rejects_unknown_value() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        let _role = EnvGuard::set("AUTH_AUTO_PROVISION_ROLE", "superuser");

        let res = AppConfig::load_from_path(file.path());
        assert!(res.is_err());
        assert!(res
            .unwrap_err()
            .to_string()
            .contains("invalid AUTH_AUTO_PROVISION_ROLE value"));

        Ok(())
    }

    #[test]
    #[serial]
    fn auth_auto_provision_role_weber_rejected_in_open_registration() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        let _role = EnvGuard::set("AUTH_AUTO_PROVISION_ROLE", "weber");
        let _auto = EnvGuard::set("AUTH_AUTO_PROVISION", "1");
        let _emails = EnvGuard::unset("AUTH_ALLOW_EMAILS");
        let _domains = EnvGuard::unset("AUTH_ALLOW_EMAIL_DOMAINS");
        let _rl_ip_min = EnvGuard::set("AUTH_RL_IP_PER_MIN", "5");
        let _rl_ip_hour = EnvGuard::set("AUTH_RL_IP_PER_HOUR", "30");
        let _proxies = EnvGuard::set("AUTH_TRUSTED_PROXIES", "none");
        let _rl_email_min = EnvGuard::set("AUTH_RL_EMAIL_PER_MIN", "2");
        let _rl_email_hour = EnvGuard::set("AUTH_RL_EMAIL_PER_HOUR", "10");

        let res = AppConfig::load_from_path(file.path());
        assert!(res.is_err());
        assert!(res
            .unwrap_err()
            .to_string()
            .contains("Open registration (no allowlist) must never auto-provision weber"));

        Ok(())
    }

    #[test]
    #[serial]
    fn auth_auto_provision_role_weber_accepted_with_email_allowlist() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        let _role = EnvGuard::set("AUTH_AUTO_PROVISION_ROLE", "weber");
        let _auto = EnvGuard::set("AUTH_AUTO_PROVISION", "1");
        let _emails = EnvGuard::set("AUTH_ALLOW_EMAILS", "weber@example.com");
        let _domains = EnvGuard::unset("AUTH_ALLOW_EMAIL_DOMAINS");

        let cfg = AppConfig::load_from_path(file.path())?;
        assert_eq!(cfg.auth_auto_provision_role, AutoProvisionRole::Weber);

        Ok(())
    }

    #[test]
    #[serial]
    fn auth_auto_provision_role_weber_accepted_with_domain_allowlist() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        let _role = EnvGuard::set("AUTH_AUTO_PROVISION_ROLE", "weber");
        let _auto = EnvGuard::set("AUTH_AUTO_PROVISION", "1");
        let _emails = EnvGuard::unset("AUTH_ALLOW_EMAILS");
        let _domains = EnvGuard::set("AUTH_ALLOW_EMAIL_DOMAINS", "example.com");

        let cfg = AppConfig::load_from_path(file.path())?;
        assert_eq!(cfg.auth_auto_provision_role, AutoProvisionRole::Weber);

        Ok(())
    }

    #[test]
    #[serial]
    fn validation_normalizes_mixed_case_inputs() -> Result<()> {
        let file = NamedTempFile::new()?;
        // Simulate YAML input with mixed case
        let yaml_content = format!(
            "{}\nauth_allow_email_domains: [\"Example.COM\", \"  Space.net \"]\n",
            YAML
        );
        std::fs::write(file.path(), yaml_content)?;

        // Ensure env doesn't override
        let _domains = EnvGuard::unset("AUTH_ALLOW_EMAIL_DOMAINS");

        let cfg = AppConfig::load_from_path(file.path())?;
        let domains = cfg.auth_allow_email_domains.expect("domains set");

        assert_eq!(domains.len(), 2);
        assert!(domains.contains(&"example.com".to_string()));
        assert!(domains.contains(&"space.net".to_string()));

        Ok(())
    }

    #[test]
    #[serial]
    fn validation_fails_if_public_login_without_delivery() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        let _public = EnvGuard::set("AUTH_PUBLIC_LOGIN", "1");
        let _url = EnvGuard::set("APP_BASE_URL", "http://localhost");
        // Ensure no SMTP and no dev logging
        let _smtp_host = EnvGuard::unset("SMTP_HOST");
        let _smtp_user = EnvGuard::unset("SMTP_USER");
        let _smtp_pass = EnvGuard::unset("SMTP_PASS");
        let _smtp_from = EnvGuard::unset("SMTP_FROM");
        let _smtp_port = EnvGuard::unset("SMTP_PORT");
        let _log_token = EnvGuard::unset("AUTH_LOG_MAGIC_TOKEN");

        let res = AppConfig::load_from_path(file.path());
        assert!(res.is_err());
        assert!(res
            .unwrap_err()
            .to_string()
            .contains("no delivery mechanism is configured"));

        Ok(())
    }

    /// Set up a config that is valid apart from the proxy-trust declaration:
    /// public login on, a delivery path, and one per-IP rate limit.
    fn public_login_with_ip_rate_limit_guards() -> Vec<EnvGuard> {
        vec![
            EnvGuard::set("AUTH_PUBLIC_LOGIN", "1"),
            EnvGuard::set("APP_BASE_URL", "https://example.com"),
            EnvGuard::set("AUTH_LOG_MAGIC_TOKEN", "1"),
            EnvGuard::set("AUTH_RL_IP_PER_MIN", "5"),
            EnvGuard::unset("AUTH_RL_IP_PER_HOUR"),
            EnvGuard::unset("AUTH_AUTO_PROVISION"),
        ]
    }

    #[test]
    #[serial]
    fn validation_rejects_public_login_with_ip_rate_limits_and_undeclared_proxy_trust() -> Result<()>
    {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        let _guards = public_login_with_ip_rate_limit_guards();
        let _proxies = EnvGuard::unset("AUTH_TRUSTED_PROXIES");

        let error = AppConfig::load_from_path(file.path())
            .expect_err("undeclared proxy trust must abort startup under per-IP rate limits");
        let message = error.to_string();

        assert!(
            message.contains("AUTH_TRUSTED_PROXIES is not set"),
            "error must name the missing variable, got: {message}"
        );
        assert!(
            message.contains("rate-limit bucket"),
            "error must explain the collapse into a single bucket, got: {message}"
        );

        Ok(())
    }

    #[test]
    #[serial]
    fn validation_rejects_ip_rate_limits_without_proxy_decision_when_login_is_private() -> Result<()>
    {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        let _public = EnvGuard::set("AUTH_PUBLIC_LOGIN", "0");
        let _rl_ip_min = EnvGuard::set("AUTH_RL_IP_PER_MIN", "5");
        let _rl_ip_hour = EnvGuard::unset("AUTH_RL_IP_PER_HOUR");
        let _proxies = EnvGuard::unset("AUTH_TRUSTED_PROXIES");

        let error = AppConfig::load_from_path(file.path())
            .expect_err("every active per-IP limiter needs an explicit proxy decision");
        assert!(error
            .to_string()
            .contains("AUTH_TRUSTED_PROXIES is not set"));
        Ok(())
    }

    #[test]
    #[serial]
    fn validation_rejects_invalid_proxy_entries_instead_of_silently_dropping_them() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        let _guards = public_login_with_ip_rate_limit_guards();
        let _proxies = EnvGuard::set("AUTH_TRUSTED_PROXIES", "127.0.0.1,not-an-ip,172.16.0.0/12");

        let error = AppConfig::load_from_path(file.path())
            .expect_err("a malformed allowlist entry must abort startup");
        let message = error.to_string();
        assert!(
            message.contains("invalid entry `not-an-ip`"),
            "got: {message}"
        );
        Ok(())
    }

    #[test]
    #[serial]
    fn validation_accepts_explicit_none_proxy_trust_for_direct_exposure() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        let _guards = public_login_with_ip_rate_limit_guards();
        let _proxies = EnvGuard::set("AUTH_TRUSTED_PROXIES", "none");

        let cfg = AppConfig::load_from_path(file.path())?;
        assert_eq!(cfg.auth_trusted_proxies.as_deref(), Some("none"));

        Ok(())
    }

    #[test]
    #[serial]
    fn validation_accepts_declared_proxy_cidr() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        let _guards = public_login_with_ip_rate_limit_guards();
        let _proxies = EnvGuard::set("AUTH_TRUSTED_PROXIES", "127.0.0.1,::1,172.16.0.0/12");

        let cfg = AppConfig::load_from_path(file.path())?;
        assert_eq!(
            cfg.auth_trusted_proxies.as_deref(),
            Some("127.0.0.1,::1,172.16.0.0/12")
        );

        Ok(())
    }

    #[test]
    #[serial]
    fn validation_treats_blank_proxy_trust_as_undeclared() -> Result<()> {
        let file = NamedTempFile::new()?;
        // A config file may spell the value as an empty string. That is "unset",
        // not "an allowlist that happens to be empty".
        let yaml = format!("{YAML}auth_trusted_proxies: \"   \"\n");
        std::fs::write(file.path(), yaml)?;

        let _guards = public_login_with_ip_rate_limit_guards();
        let _proxies = EnvGuard::unset("AUTH_TRUSTED_PROXIES");

        let error = AppConfig::load_from_path(file.path())
            .expect_err("a blank declaration must be treated as undeclared");
        assert!(error
            .to_string()
            .contains("AUTH_TRUSTED_PROXIES is not set"));

        Ok(())
    }

    #[test]
    #[serial]
    fn validation_allows_undeclared_proxy_trust_without_ip_rate_limits() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        // No per-IP limiter is installed, so the client-IP resolution cannot
        // silently degrade a defence that does not exist. Local dev stays easy.
        let _public = EnvGuard::set("AUTH_PUBLIC_LOGIN", "1");
        let _url = EnvGuard::set("APP_BASE_URL", "http://localhost");
        let _log = EnvGuard::set("AUTH_LOG_MAGIC_TOKEN", "1");
        let _rl_ip_min = EnvGuard::unset("AUTH_RL_IP_PER_MIN");
        let _rl_ip_hour = EnvGuard::unset("AUTH_RL_IP_PER_HOUR");
        let _auto = EnvGuard::unset("AUTH_AUTO_PROVISION");
        let _proxies = EnvGuard::unset("AUTH_TRUSTED_PROXIES");

        let cfg = AppConfig::load_from_path(file.path())?;
        assert!(cfg.auth_trusted_proxies.is_none());

        Ok(())
    }

    #[test]
    #[serial]
    fn validation_ignores_zero_ip_rate_limits_when_checking_proxy_trust() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        // `AuthRateLimiter::new` maps a limit of 0 to "no limiter", so 0 must not
        // trip the proxy-trust requirement either.
        let _public = EnvGuard::set("AUTH_PUBLIC_LOGIN", "1");
        let _url = EnvGuard::set("APP_BASE_URL", "http://localhost");
        let _log = EnvGuard::set("AUTH_LOG_MAGIC_TOKEN", "1");
        let _rl_ip_min = EnvGuard::set("AUTH_RL_IP_PER_MIN", "0");
        let _rl_ip_hour = EnvGuard::set("AUTH_RL_IP_PER_HOUR", "0");
        let _auto = EnvGuard::unset("AUTH_AUTO_PROVISION");
        let _proxies = EnvGuard::unset("AUTH_TRUSTED_PROXIES");

        let cfg = AppConfig::load_from_path(file.path())?;
        assert!(cfg.auth_trusted_proxies.is_none());

        Ok(())
    }

    #[test]
    #[serial]
    fn validation_succeeds_if_public_login_with_auth_log_magic_token() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        let _public = EnvGuard::set("AUTH_PUBLIC_LOGIN", "1");
        let _url = EnvGuard::set("APP_BASE_URL", "http://localhost");
        // No SMTP but enable dev logging
        let _smtp_host = EnvGuard::unset("SMTP_HOST");
        let _smtp_user = EnvGuard::unset("SMTP_USER");
        let _smtp_pass = EnvGuard::unset("SMTP_PASS");
        let _smtp_from = EnvGuard::unset("SMTP_FROM");
        let _smtp_port = EnvGuard::unset("SMTP_PORT");
        let _log_token = EnvGuard::set("AUTH_LOG_MAGIC_TOKEN", "1");

        let cfg = AppConfig::load_from_path(file.path())?;
        assert!(cfg.auth_public_login);
        assert!(cfg.auth_log_magic_token);

        Ok(())
    }

    #[test]
    #[serial]
    fn is_open_registration_logic_is_correct() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        // Default: False
        let cfg = AppConfig::load_from_path(file.path())?;
        assert!(!cfg.is_open_registration());

        // Enable all requirements
        let _public = EnvGuard::set("AUTH_PUBLIC_LOGIN", "1");
        let _base = EnvGuard::set("APP_BASE_URL", "http://localhost");
        let _auto = EnvGuard::set("AUTH_AUTO_PROVISION", "1");
        let _rl_ip_min = EnvGuard::set("AUTH_RL_IP_PER_MIN", "5");
        let _rl_ip_hour = EnvGuard::set("AUTH_RL_IP_PER_HOUR", "30");
        let _rl_email_min = EnvGuard::set("AUTH_RL_EMAIL_PER_MIN", "2");
        let _rl_email_hour = EnvGuard::set("AUTH_RL_EMAIL_PER_HOUR", "10");
        let _log = EnvGuard::set("AUTH_LOG_MAGIC_TOKEN", "1");
        // Per-IP limits plus public login now require an explicit proxy-trust
        // declaration (see `trusted_proxies_*` tests below).
        let _proxies = EnvGuard::set("AUTH_TRUSTED_PROXIES", "none");

        let cfg = AppConfig::load_from_path(file.path())?;
        assert!(cfg.is_open_registration());

        // Add Allowlist -> False
        let _emails = EnvGuard::set("AUTH_ALLOW_EMAILS", "foo@bar.com");
        let cfg = AppConfig::load_from_path(file.path())?;
        assert!(!cfg.is_open_registration());
        let _emails_unset = EnvGuard::unset("AUTH_ALLOW_EMAILS");

        // Missing Rate Limit -> False (and actually validation would fail, but let's check method behavior on constructed struct if possible or via validation error)
        let _rl_missing = EnvGuard::unset("AUTH_RL_IP_PER_MIN");
        // Validation fails so we can't get a cfg object via load_from_path
        let res = AppConfig::load_from_path(file.path());
        assert!(res.is_err());

        Ok(())
    }

    #[test]
    #[serial]
    fn webauthn_env_overrides_are_applied() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        let _rp_id = EnvGuard::set("WEBAUTHN_RP_ID", "example.com");
        let _rp_origin = EnvGuard::set("WEBAUTHN_RP_ORIGIN", "https://example.com");
        let _rp_name = EnvGuard::set("WEBAUTHN_RP_NAME", "My App");

        let cfg = AppConfig::load_from_path(file.path())?;
        assert_eq!(cfg.webauthn_rp_id.as_deref(), Some("example.com"));
        assert_eq!(
            cfg.webauthn_rp_origin.as_deref(),
            Some("https://example.com")
        );
        assert_eq!(cfg.webauthn_rp_name.as_deref(), Some("My App"));

        Ok(())
    }

    #[test]
    #[serial]
    fn webauthn_validation_rejects_rp_id_without_origin() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        let _rp_id = EnvGuard::set("WEBAUTHN_RP_ID", "example.com");
        let _rp_origin = EnvGuard::unset("WEBAUTHN_RP_ORIGIN");

        let res = AppConfig::load_from_path(file.path());
        assert!(
            res.is_err(),
            "rp_id without rp_origin must be rejected at config load"
        );
        Ok(())
    }

    #[test]
    #[serial]
    fn webauthn_validation_rejects_mismatched_origin() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        // rp_id is "example.com" but origin host is "other.org" — must be rejected.
        let _rp_id = EnvGuard::set("WEBAUTHN_RP_ID", "example.com");
        let _rp_origin = EnvGuard::set("WEBAUTHN_RP_ORIGIN", "https://other.org");

        let res = AppConfig::load_from_path(file.path());
        assert!(
            res.is_err(),
            "origin that does not end with rp_id must be rejected"
        );
        Ok(())
    }

    #[test]
    #[serial]
    fn webauthn_validation_allows_exact_host_match() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        let _rp_id = EnvGuard::set("WEBAUTHN_RP_ID", "example.com");
        let _rp_origin = EnvGuard::set("WEBAUTHN_RP_ORIGIN", "https://example.com");

        let res = AppConfig::load_from_path(file.path());
        assert!(
            res.is_ok(),
            "exact host match (origin host == rp_id) must be accepted"
        );
        Ok(())
    }

    #[test]
    #[serial]
    fn webauthn_validation_allows_true_subdomain() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        // sub.example.com ends_with ".example.com" — valid subdomain
        let _rp_id = EnvGuard::set("WEBAUTHN_RP_ID", "example.com");
        let _rp_origin = EnvGuard::set("WEBAUTHN_RP_ORIGIN", "https://app.example.com");

        let res = AppConfig::load_from_path(file.path());
        assert!(
            res.is_ok(),
            "true subdomain (app.example.com for rp_id=example.com) must be accepted"
        );
        Ok(())
    }

    #[test]
    #[serial]
    fn webauthn_validation_rejects_suffix_attack() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        // evil-example.com ends_with "example.com" as a bare string but is NOT a subdomain
        let _rp_id = EnvGuard::set("WEBAUTHN_RP_ID", "example.com");
        let _rp_origin = EnvGuard::set("WEBAUTHN_RP_ORIGIN", "https://evil-example.com");

        let res = AppConfig::load_from_path(file.path());
        assert!(
            res.is_err(),
            "suffix attack (evil-example.com) must be rejected for rp_id=example.com"
        );
        Ok(())
    }

    #[test]
    #[serial]
    fn webauthn_validation_rejects_origin_with_path() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        let _rp_id = EnvGuard::set("WEBAUTHN_RP_ID", "example.com");
        let _rp_origin = EnvGuard::set("WEBAUTHN_RP_ORIGIN", "https://example.com/app");

        let res = AppConfig::load_from_path(file.path());
        assert!(
            res.is_err(),
            "origin with a path component must be rejected"
        );
        Ok(())
    }

    #[test]
    #[serial]
    fn webauthn_validation_rejects_origin_with_query() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        let _rp_id = EnvGuard::set("WEBAUTHN_RP_ID", "example.com");
        let _rp_origin = EnvGuard::set("WEBAUTHN_RP_ORIGIN", "https://example.com?foo=bar");

        let res = AppConfig::load_from_path(file.path());
        assert!(res.is_err(), "origin with a query string must be rejected");
        Ok(())
    }

    #[test]
    #[serial]
    fn webauthn_validation_rejects_non_http_scheme() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        let _rp_id = EnvGuard::set("WEBAUTHN_RP_ID", "example.com");
        let _rp_origin = EnvGuard::set("WEBAUTHN_RP_ORIGIN", "ftp://example.com");

        let res = AppConfig::load_from_path(file.path());
        assert!(res.is_err(), "non-http/https scheme must be rejected");
        Ok(())
    }

    #[test]
    #[serial]
    fn webauthn_validation_rejects_origin_with_credentials() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        let _rp_id = EnvGuard::set("WEBAUTHN_RP_ID", "example.com");
        let _rp_origin = EnvGuard::set("WEBAUTHN_RP_ORIGIN", "https://user@example.com");

        let res = AppConfig::load_from_path(file.path());
        assert!(
            res.is_err(),
            "origin with userinfo credentials must be rejected"
        );
        Ok(())
    }

    #[test]
    #[serial]
    fn webauthn_validation_rejects_origin_with_fragment() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;

        let _rp_id = EnvGuard::set("WEBAUTHN_RP_ID", "example.com");
        let _rp_origin = EnvGuard::set("WEBAUTHN_RP_ORIGIN", "https://example.com#frag");

        let res = AppConfig::load_from_path(file.path());
        assert!(res.is_err(), "origin with a fragment must be rejected");
        Ok(())
    }
    #[test]
    #[serial]
    fn domain_read_source_defaults_to_jsonl() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;
        let _source = EnvGuard::unset("WELTGEWEBE_DOMAIN_READ_SOURCE");

        let cfg = AppConfig::load_from_path(file.path())?;

        assert_eq!(cfg.domain_read_source, DomainReadSource::Jsonl);
        Ok(())
    }

    #[test]
    #[serial]
    fn domain_read_source_env_accepts_aliases() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;
        let _source = EnvGuard::set("WELTGEWEBE_DOMAIN_READ_SOURCE", "pg");

        let cfg = AppConfig::load_from_path(file.path())?;

        assert_eq!(cfg.domain_read_source, DomainReadSource::Postgres);
        Ok(())
    }

    #[test]
    #[serial]
    fn domain_read_source_invalid_env_is_hard_error() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;
        let _source = EnvGuard::set("WELTGEWEBE_DOMAIN_READ_SOURCE", "sqlite");

        let err = AppConfig::load_from_path(file.path()).unwrap_err();

        assert!(err
            .to_string()
            .contains("invalid WELTGEWEBE_DOMAIN_READ_SOURCE"));
        Ok(())
    }

    #[test]
    #[serial]
    fn domain_account_write_source_defaults_to_jsonl() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;
        let _read = EnvGuard::unset("WELTGEWEBE_DOMAIN_READ_SOURCE");
        let _write = EnvGuard::unset("WELTGEWEBE_DOMAIN_ACCOUNT_WRITE_SOURCE");

        let cfg = AppConfig::load_from_path(file.path())?;

        assert_eq!(
            cfg.domain_account_write_source,
            DomainAccountWriteSource::Jsonl
        );
        Ok(())
    }

    #[test]
    #[serial]
    fn domain_account_write_source_empty_env_keeps_default() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;
        let _read = EnvGuard::unset("WELTGEWEBE_DOMAIN_READ_SOURCE");
        let _write = EnvGuard::set("WELTGEWEBE_DOMAIN_ACCOUNT_WRITE_SOURCE", "   ");

        let cfg = AppConfig::load_from_path(file.path())?;

        assert_eq!(
            cfg.domain_account_write_source,
            DomainAccountWriteSource::Jsonl
        );
        Ok(())
    }

    #[test]
    #[serial]
    fn domain_account_write_source_env_accepts_aliases() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;
        // postgres account-write requires postgres read source
        let _read = EnvGuard::set("WELTGEWEBE_DOMAIN_READ_SOURCE", "postgres");
        let _write = EnvGuard::set("WELTGEWEBE_DOMAIN_ACCOUNT_WRITE_SOURCE", "db");

        let cfg = AppConfig::load_from_path(file.path())?;

        assert_eq!(
            cfg.domain_account_write_source,
            DomainAccountWriteSource::Postgres
        );
        Ok(())
    }

    #[test]
    #[serial]
    fn domain_account_write_source_invalid_env_is_hard_error() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;
        let _read = EnvGuard::unset("WELTGEWEBE_DOMAIN_READ_SOURCE");
        let _write = EnvGuard::set("WELTGEWEBE_DOMAIN_ACCOUNT_WRITE_SOURCE", "sqlite");

        let err = AppConfig::load_from_path(file.path()).unwrap_err();

        assert!(err
            .to_string()
            .contains("invalid WELTGEWEBE_DOMAIN_ACCOUNT_WRITE_SOURCE"));
        Ok(())
    }

    #[test]
    #[serial]
    fn domain_account_write_postgres_with_jsonl_read_is_rejected() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;
        let _read = EnvGuard::set("WELTGEWEBE_DOMAIN_READ_SOURCE", "jsonl");
        let _write = EnvGuard::set("WELTGEWEBE_DOMAIN_ACCOUNT_WRITE_SOURCE", "postgres");

        let err = AppConfig::load_from_path(file.path()).unwrap_err();

        assert!(err
            .to_string()
            .contains("domain_account_write_source=postgres requires domain_read_source=postgres"));
        Ok(())
    }

    #[test]
    #[serial]
    fn domain_account_write_postgres_with_postgres_read_is_accepted() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;
        let _read = EnvGuard::set("WELTGEWEBE_DOMAIN_READ_SOURCE", "postgres");
        let _write = EnvGuard::set("WELTGEWEBE_DOMAIN_ACCOUNT_WRITE_SOURCE", "postgres");

        let cfg = AppConfig::load_from_path(file.path())?;

        assert_eq!(cfg.domain_read_source, DomainReadSource::Postgres);
        assert_eq!(
            cfg.domain_account_write_source,
            DomainAccountWriteSource::Postgres
        );
        Ok(())
    }

    #[test]
    #[serial]
    fn domain_node_write_source_defaults_to_jsonl() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;
        let _read = EnvGuard::unset("WELTGEWEBE_DOMAIN_READ_SOURCE");
        let _write = EnvGuard::unset("WELTGEWEBE_DOMAIN_NODE_WRITE_SOURCE");

        let cfg = AppConfig::load_from_path(file.path())?;

        assert_eq!(cfg.domain_node_write_source, DomainNodeWriteSource::Jsonl);
        Ok(())
    }

    #[test]
    #[serial]
    fn domain_node_write_source_empty_env_keeps_default() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;
        let _read = EnvGuard::unset("WELTGEWEBE_DOMAIN_READ_SOURCE");
        let _write = EnvGuard::set("WELTGEWEBE_DOMAIN_NODE_WRITE_SOURCE", "   ");

        let cfg = AppConfig::load_from_path(file.path())?;

        assert_eq!(cfg.domain_node_write_source, DomainNodeWriteSource::Jsonl);
        Ok(())
    }

    #[test]
    #[serial]
    fn domain_node_write_source_env_accepts_aliases() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;
        let _read = EnvGuard::set("WELTGEWEBE_DOMAIN_READ_SOURCE", "postgres");
        let _write = EnvGuard::set("WELTGEWEBE_DOMAIN_NODE_WRITE_SOURCE", "db");

        let cfg = AppConfig::load_from_path(file.path())?;

        assert_eq!(
            cfg.domain_node_write_source,
            DomainNodeWriteSource::Postgres
        );
        Ok(())
    }

    #[test]
    #[serial]
    fn domain_node_write_source_invalid_env_is_hard_error() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;
        let _read = EnvGuard::unset("WELTGEWEBE_DOMAIN_READ_SOURCE");
        let _write = EnvGuard::set("WELTGEWEBE_DOMAIN_NODE_WRITE_SOURCE", "sqlite");

        let err = AppConfig::load_from_path(file.path()).unwrap_err();

        assert!(err
            .to_string()
            .contains("invalid WELTGEWEBE_DOMAIN_NODE_WRITE_SOURCE"));
        Ok(())
    }

    #[test]
    #[serial]
    fn domain_node_write_postgres_with_jsonl_read_is_rejected() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;
        let _read = EnvGuard::set("WELTGEWEBE_DOMAIN_READ_SOURCE", "jsonl");
        let _write = EnvGuard::set("WELTGEWEBE_DOMAIN_NODE_WRITE_SOURCE", "postgres");

        let err = AppConfig::load_from_path(file.path()).unwrap_err();

        assert!(err
            .to_string()
            .contains("domain_node_write_source=postgres requires domain_read_source=postgres"));
        Ok(())
    }

    #[test]
    #[serial]
    fn domain_node_write_postgres_with_postgres_read_is_accepted() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;
        let _read = EnvGuard::set("WELTGEWEBE_DOMAIN_READ_SOURCE", "postgres");
        let _write = EnvGuard::set("WELTGEWEBE_DOMAIN_NODE_WRITE_SOURCE", "postgres");

        let cfg = AppConfig::load_from_path(file.path())?;

        assert_eq!(cfg.domain_read_source, DomainReadSource::Postgres);
        assert_eq!(
            cfg.domain_node_write_source,
            DomainNodeWriteSource::Postgres
        );
        Ok(())
    }

    #[test]
    #[serial]
    fn domain_edge_write_source_defaults_to_read_only() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;
        let _read = EnvGuard::unset("WELTGEWEBE_DOMAIN_READ_SOURCE");
        let _write = EnvGuard::unset("WELTGEWEBE_DOMAIN_EDGE_WRITE_SOURCE");

        let cfg = AppConfig::load_from_path(file.path())?;

        assert_eq!(
            cfg.domain_edge_write_source,
            DomainEdgeWriteSource::ReadOnly
        );
        Ok(())
    }

    #[test]
    #[serial]
    fn domain_edge_write_source_empty_env_keeps_default() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;
        let _read = EnvGuard::unset("WELTGEWEBE_DOMAIN_READ_SOURCE");
        let _write = EnvGuard::set("WELTGEWEBE_DOMAIN_EDGE_WRITE_SOURCE", "   ");

        let cfg = AppConfig::load_from_path(file.path())?;

        assert_eq!(
            cfg.domain_edge_write_source,
            DomainEdgeWriteSource::ReadOnly
        );
        Ok(())
    }

    #[test]
    #[serial]
    fn domain_edge_write_source_read_only_env_is_accepted() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;
        let _read = EnvGuard::unset("WELTGEWEBE_DOMAIN_READ_SOURCE");
        let _write = EnvGuard::set("WELTGEWEBE_DOMAIN_EDGE_WRITE_SOURCE", "read_only");

        let cfg = AppConfig::load_from_path(file.path())?;

        assert_eq!(
            cfg.domain_edge_write_source,
            DomainEdgeWriteSource::ReadOnly
        );
        Ok(())
    }

    #[test]
    #[serial]
    fn domain_edge_write_source_jsonl_env_is_rejected_as_retired() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;
        let _read = EnvGuard::unset("WELTGEWEBE_DOMAIN_READ_SOURCE");
        let _write = EnvGuard::set("WELTGEWEBE_DOMAIN_EDGE_WRITE_SOURCE", "jsonl");

        let error = AppConfig::load_from_path(file.path())
            .expect_err("runtime config must not reactivate legacy JSONL edge writes");

        assert!(error
            .to_string()
            .contains("domain_edge_write_source=jsonl is retired"));
        Ok(())
    }

    #[test]
    #[serial]
    fn domain_edge_write_source_env_accepts_aliases() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;
        let _read = EnvGuard::set("WELTGEWEBE_DOMAIN_READ_SOURCE", "postgres");
        let _write = EnvGuard::set("WELTGEWEBE_DOMAIN_EDGE_WRITE_SOURCE", "db");

        let cfg = AppConfig::load_from_path(file.path())?;

        assert_eq!(
            cfg.domain_edge_write_source,
            DomainEdgeWriteSource::Postgres
        );
        Ok(())
    }

    #[test]
    #[serial]
    fn domain_edge_write_source_invalid_env_is_hard_error() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;
        let _read = EnvGuard::unset("WELTGEWEBE_DOMAIN_READ_SOURCE");
        let _write = EnvGuard::set("WELTGEWEBE_DOMAIN_EDGE_WRITE_SOURCE", "sqlite");

        let err = AppConfig::load_from_path(file.path()).unwrap_err();

        assert!(err
            .to_string()
            .contains("invalid WELTGEWEBE_DOMAIN_EDGE_WRITE_SOURCE"));
        Ok(())
    }

    #[test]
    #[serial]
    fn domain_edge_write_postgres_with_jsonl_read_is_rejected() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;
        let _read = EnvGuard::set("WELTGEWEBE_DOMAIN_READ_SOURCE", "jsonl");
        let _write = EnvGuard::set("WELTGEWEBE_DOMAIN_EDGE_WRITE_SOURCE", "postgres");

        let err = AppConfig::load_from_path(file.path()).unwrap_err();

        assert!(err
            .to_string()
            .contains("domain_edge_write_source=postgres requires domain_read_source=postgres"));
        Ok(())
    }

    #[test]
    #[serial]
    fn domain_edge_write_read_only_with_postgres_read_is_accepted() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;
        let _read = EnvGuard::set("WELTGEWEBE_DOMAIN_READ_SOURCE", "postgres");
        let _write = EnvGuard::set("WELTGEWEBE_DOMAIN_EDGE_WRITE_SOURCE", "read_only");

        let cfg = AppConfig::load_from_path(file.path())?;

        assert_eq!(cfg.domain_read_source, DomainReadSource::Postgres);
        assert_eq!(
            cfg.domain_edge_write_source,
            DomainEdgeWriteSource::ReadOnly
        );
        Ok(())
    }

    #[test]
    #[serial]
    fn domain_edge_write_postgres_with_postgres_read_is_accepted() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;
        let _read = EnvGuard::set("WELTGEWEBE_DOMAIN_READ_SOURCE", "postgres");
        let _write = EnvGuard::set("WELTGEWEBE_DOMAIN_EDGE_WRITE_SOURCE", "postgres");

        let cfg = AppConfig::load_from_path(file.path())?;

        assert_eq!(cfg.domain_read_source, DomainReadSource::Postgres);
        assert_eq!(
            cfg.domain_edge_write_source,
            DomainEdgeWriteSource::Postgres
        );
        Ok(())
    }

    #[test]
    #[serial]
    fn passkey_credential_source_defaults_to_in_memory() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;
        let _read = EnvGuard::unset("WELTGEWEBE_DOMAIN_READ_SOURCE");
        let _source = EnvGuard::unset("WELTGEWEBE_PASSKEY_CREDENTIAL_SOURCE");

        let cfg = AppConfig::load_from_path(file.path())?;

        assert_eq!(
            cfg.passkey_credential_source,
            PasskeyCredentialSource::InMemory
        );
        Ok(())
    }

    #[test]
    #[serial]
    fn passkey_credential_source_env_accepts_aliases() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;
        let _read = EnvGuard::set("WELTGEWEBE_DOMAIN_READ_SOURCE", "postgres");
        let _source = EnvGuard::set("WELTGEWEBE_PASSKEY_CREDENTIAL_SOURCE", "db");

        let cfg = AppConfig::load_from_path(file.path())?;

        assert_eq!(cfg.domain_read_source, DomainReadSource::Postgres);
        assert_eq!(
            cfg.passkey_credential_source,
            PasskeyCredentialSource::Postgres
        );
        Ok(())
    }

    #[test]
    #[serial]
    fn passkey_credential_source_yaml_accepts_in_memory() -> Result<()> {
        let yaml = format!(
            "{YAML}passkey_credential_source: in_memory
"
        );
        let cfg = AppConfig::load_from_str(&yaml)?;

        assert_eq!(
            cfg.passkey_credential_source,
            PasskeyCredentialSource::InMemory
        );
        Ok(())
    }

    #[test]
    #[serial]
    fn passkey_credential_source_yaml_accepts_postgres() -> Result<()> {
        let yaml = format!(
            "{YAML}domain_read_source: postgres
passkey_credential_source: postgres
"
        );
        let cfg = AppConfig::load_from_str(&yaml)?;

        assert_eq!(cfg.domain_read_source, DomainReadSource::Postgres);
        assert_eq!(
            cfg.passkey_credential_source,
            PasskeyCredentialSource::Postgres
        );
        Ok(())
    }

    #[test]
    #[serial]
    fn passkey_credential_source_invalid_env_is_hard_error() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;
        let _source = EnvGuard::set("WELTGEWEBE_PASSKEY_CREDENTIAL_SOURCE", "sqlite");

        let err = AppConfig::load_from_path(file.path()).unwrap_err();

        assert!(err
            .to_string()
            .contains("invalid WELTGEWEBE_PASSKEY_CREDENTIAL_SOURCE"));
        Ok(())
    }

    #[test]
    #[serial]
    fn passkey_credential_postgres_with_jsonl_read_is_rejected() -> Result<()> {
        let file = NamedTempFile::new()?;
        std::fs::write(file.path(), YAML)?;
        let _read = EnvGuard::set("WELTGEWEBE_DOMAIN_READ_SOURCE", "jsonl");
        let _source = EnvGuard::set("WELTGEWEBE_PASSKEY_CREDENTIAL_SOURCE", "postgres");

        let err = AppConfig::load_from_path(file.path()).unwrap_err();

        assert!(err
            .to_string()
            .contains("passkey_credential_source=postgres requires domain_read_source=postgres"));
        Ok(())
    }
}
