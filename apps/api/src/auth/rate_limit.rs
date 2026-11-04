use crate::{auth::role::Role, config::AppConfig, node_mutation::NodeMutationOperation};
use chrono::{TimeZone, Utc};
use governor::{clock::DefaultClock, state::keyed::DefaultKeyedStateStore, Quota, RateLimiter};
use sha2::{Digest, Sha256};
use sqlx::PgPool;
use std::net::{IpAddr, Ipv6Addr};
use std::num::NonZeroU32;
use thiserror::Error;

type IpLimiter = RateLimiter<IpAddr, DefaultKeyedStateStore<IpAddr>, DefaultClock>;
type EmailLimiter = RateLimiter<String, DefaultKeyedStateStore<String>, DefaultClock>;
type AccountLimiter = RateLimiter<String, DefaultKeyedStateStore<String>, DefaultClock>;

/// Maps a client address to the subject that IP rate limits count against.
///
/// One IPv6 subscriber usually owns at least a /64, so counting each /128
/// separately would give a single client 2^64 independent budgets. IPv6 is
/// therefore collapsed to its /64 network, and IPv4-mapped IPv6 addresses
/// count as the IPv4 address they carry.
pub fn rate_limit_ip_subject(ip: IpAddr) -> IpAddr {
    match ip {
        IpAddr::V4(_) => ip,
        IpAddr::V6(v6) => {
            if let Some(v4) = v6.to_ipv4_mapped() {
                return IpAddr::V4(v4);
            }
            let network = u128::from(v6) & !((1u128 << 64) - 1);
            IpAddr::V6(Ipv6Addr::from(network))
        }
    }
}

#[derive(Debug, Error)]
pub enum RateLimitError {
    #[error("Too many requests from IP")]
    IpLimited,
    #[error("Too many requests for this identifier")]
    EmailLimited,
    #[error("Too many collective node mutations for this account")]
    AccountLimited,
    #[error("Shared rate-limit backend unavailable: {0}")]
    BackendUnavailable(String),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum NodeMutationRateDecision {
    Enforced,
    AdminEmergencyBypass,
}

pub struct AuthRateLimiter {
    ip_limiter_min: Option<IpLimiter>,
    ip_limiter_hour: Option<IpLimiter>,
    email_limiter_min: Option<EmailLimiter>,
    email_limiter_hour: Option<EmailLimiter>,
    node_replace_limiter_min: Option<AccountLimiter>,
    node_replace_limiter_hour: Option<AccountLimiter>,
    node_delete_limiter_min: Option<AccountLimiter>,
    node_delete_limiter_hour: Option<AccountLimiter>,
    node_replace_per_min: u32,
    node_replace_per_hour: u32,
    node_delete_per_min: u32,
    node_delete_per_hour: u32,
    node_admin_emergency_bypass: bool,
    db: Option<PgPool>,
    ip_per_min: Option<u32>,
    ip_per_hour: Option<u32>,
    email_per_min: Option<u32>,
    email_per_hour: Option<u32>,
}

impl AuthRateLimiter {
    pub fn new(config: &AppConfig) -> Self {
        let ip_limiter_min = config.auth_rl_ip_per_min.and_then(|limit| {
            NonZeroU32::new(limit).map(|limit| RateLimiter::keyed(Quota::per_minute(limit)))
        });
        let ip_limiter_hour = config.auth_rl_ip_per_hour.and_then(|limit| {
            NonZeroU32::new(limit).map(|limit| RateLimiter::keyed(Quota::per_hour(limit)))
        });
        let email_limiter_min = config.auth_rl_email_per_min.and_then(|limit| {
            NonZeroU32::new(limit).map(|limit| RateLimiter::keyed(Quota::per_minute(limit)))
        });
        let email_limiter_hour = config.auth_rl_email_per_hour.and_then(|limit| {
            NonZeroU32::new(limit).map(|limit| RateLimiter::keyed(Quota::per_hour(limit)))
        });
        let mutation = &config.node_mutation_rate_limits;
        let node_replace_limiter_min = NonZeroU32::new(mutation.replace_per_minute)
            .map(|limit| RateLimiter::keyed(Quota::per_minute(limit)));
        let node_replace_limiter_hour = NonZeroU32::new(mutation.replace_per_hour)
            .map(|limit| RateLimiter::keyed(Quota::per_hour(limit)));
        let node_delete_limiter_min = NonZeroU32::new(mutation.delete_per_minute)
            .map(|limit| RateLimiter::keyed(Quota::per_minute(limit)));
        let node_delete_limiter_hour = NonZeroU32::new(mutation.delete_per_hour)
            .map(|limit| RateLimiter::keyed(Quota::per_hour(limit)));
        Self {
            ip_limiter_min,
            ip_limiter_hour,
            email_limiter_min,
            email_limiter_hour,
            node_replace_limiter_min,
            node_replace_limiter_hour,
            node_delete_limiter_min,
            node_delete_limiter_hour,
            node_replace_per_min: mutation.replace_per_minute,
            node_replace_per_hour: mutation.replace_per_hour,
            node_delete_per_min: mutation.delete_per_minute,
            node_delete_per_hour: mutation.delete_per_hour,
            node_admin_emergency_bypass: mutation.admin_emergency_bypass,
            db: None,
            ip_per_min: config.auth_rl_ip_per_min,
            ip_per_hour: config.auth_rl_ip_per_hour,
            email_per_min: config.auth_rl_email_per_min,
            email_per_hour: config.auth_rl_email_per_hour,
        }
    }

    pub fn new_postgres(config: &AppConfig, pool: PgPool) -> Self {
        Self {
            db: Some(pool),
            ..Self::new(config)
        }
    }

    pub fn is_shared(&self) -> bool {
        self.db.is_some()
    }

    pub fn check(&self, ip: IpAddr, email_hash: &str) -> Result<(), RateLimitError> {
        let ip = rate_limit_ip_subject(ip);
        if let Some(limiter) = &self.ip_limiter_min {
            if limiter.check_key(&ip).is_err() {
                return Err(RateLimitError::IpLimited);
            }
        }
        if let Some(limiter) = &self.ip_limiter_hour {
            if limiter.check_key(&ip).is_err() {
                return Err(RateLimitError::IpLimited);
            }
        }
        let email_hash_owned = email_hash.to_string();
        if let Some(limiter) = &self.email_limiter_min {
            if limiter.check_key(&email_hash_owned).is_err() {
                return Err(RateLimitError::EmailLimited);
            }
        }
        if let Some(limiter) = &self.email_limiter_hour {
            if limiter.check_key(&email_hash_owned).is_err() {
                return Err(RateLimitError::EmailLimited);
            }
        }
        Ok(())
    }

    fn subject_hash(value: &str) -> String {
        let mut hasher = Sha256::new();
        hasher.update(value.as_bytes());
        crate::auth::digest::encode_sha256_digest(hasher.finalize())
    }

    async fn increment_window(
        pool: &PgPool,
        scope: &str,
        subject: &str,
        window_seconds: i64,
        limit: u32,
    ) -> Result<bool, sqlx::Error> {
        if limit == 0 {
            return Ok(true);
        }
        let now = Utc::now();
        let now_epoch = now.timestamp();
        let window_start = now_epoch - now_epoch.rem_euclid(window_seconds);
        let expires_at = Utc
            .timestamp_opt(window_start + window_seconds + 60, 0)
            .single()
            .expect("bounded epoch seconds");
        let count: i64 = sqlx::query_scalar(
            "INSERT INTO auth_rate_limit_counters \
             (scope, subject_hash, window_start, window_seconds, request_count, expires_at) \
             VALUES ($1, $2, $3, $4, 1, $5) \
             ON CONFLICT (scope, subject_hash, window_start, window_seconds) \
             DO UPDATE SET request_count = auth_rate_limit_counters.request_count + 1 \
             RETURNING request_count",
        )
        .bind(scope)
        .bind(Self::subject_hash(subject))
        .bind(window_start)
        .bind(window_seconds as i32)
        .bind(expires_at)
        .fetch_one(pool)
        .await?;
        Ok(count <= i64::from(limit))
    }

    pub async fn check_shared(
        &self,
        ip: IpAddr,
        identifier_hash: &str,
    ) -> Result<(), RateLimitError> {
        let Some(pool) = &self.db else {
            return self.check(ip, identifier_hash);
        };
        let ip = rate_limit_ip_subject(ip).to_string();
        let checks = [
            ("ip-minute", ip.as_str(), 60_i64, self.ip_per_min, true),
            ("ip-hour", ip.as_str(), 3600_i64, self.ip_per_hour, true),
            (
                "identifier-minute",
                identifier_hash,
                60_i64,
                self.email_per_min,
                false,
            ),
            (
                "identifier-hour",
                identifier_hash,
                3600_i64,
                self.email_per_hour,
                false,
            ),
        ];
        for (scope, subject, seconds, limit, ip_scope) in checks {
            let Some(limit) = limit else { continue };
            let allowed = Self::increment_window(pool, scope, subject, seconds, limit)
                .await
                .map_err(|error| RateLimitError::BackendUnavailable(error.to_string()))?;
            if !allowed {
                return Err(if ip_scope {
                    RateLimitError::IpLimited
                } else {
                    RateLimitError::EmailLimited
                });
            }
        }
        Ok(())
    }

    pub async fn check_node_mutation(
        &self,
        account_id: &str,
        role: Role,
        operation: NodeMutationOperation,
    ) -> Result<NodeMutationRateDecision, RateLimitError> {
        if matches!(role, Role::Admin) && self.node_admin_emergency_bypass {
            return Ok(NodeMutationRateDecision::AdminEmergencyBypass);
        }

        let subject = Self::subject_hash(account_id);
        let (local_minute, local_hour, per_minute, per_hour, minute_scope, hour_scope) =
            match operation {
                NodeMutationOperation::Patch | NodeMutationOperation::Replace => (
                    &self.node_replace_limiter_min,
                    &self.node_replace_limiter_hour,
                    self.node_replace_per_min,
                    self.node_replace_per_hour,
                    "node-mutation-replace-minute",
                    "node-mutation-replace-hour",
                ),
                NodeMutationOperation::Delete => (
                    &self.node_delete_limiter_min,
                    &self.node_delete_limiter_hour,
                    self.node_delete_per_min,
                    self.node_delete_per_hour,
                    "node-mutation-delete-minute",
                    "node-mutation-delete-hour",
                ),
            };

        if let Some(pool) = &self.db {
            for (scope, seconds, limit) in [
                (minute_scope, 60_i64, per_minute),
                (hour_scope, 3600_i64, per_hour),
            ] {
                let allowed = Self::increment_window(pool, scope, account_id, seconds, limit)
                    .await
                    .map_err(|error| RateLimitError::BackendUnavailable(error.to_string()))?;
                if !allowed {
                    return Err(RateLimitError::AccountLimited);
                }
            }
        } else {
            if let Some(limiter) = local_minute {
                if limiter.check_key(&subject).is_err() {
                    return Err(RateLimitError::AccountLimited);
                }
            }
            if let Some(limiter) = local_hour {
                if limiter.check_key(&subject).is_err() {
                    return Err(RateLimitError::AccountLimited);
                }
            }
        }
        Ok(NodeMutationRateDecision::Enforced)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::Ipv4Addr;

    #[test]
    fn ipv4_subject_is_unchanged() {
        let ip = IpAddr::V4(Ipv4Addr::new(192, 0, 2, 7));
        assert_eq!(rate_limit_ip_subject(ip), ip);
    }

    #[test]
    fn ipv6_addresses_in_one_slash_64_share_a_subject() {
        let a: IpAddr = "2001:db8:1:2:aaaa:bbbb:cccc:dddd".parse().unwrap();
        let b: IpAddr = "2001:db8:1:2::1".parse().unwrap();
        let other: IpAddr = "2001:db8:1:3::1".parse().unwrap();
        let expected: IpAddr = "2001:db8:1:2::".parse().unwrap();
        assert_eq!(rate_limit_ip_subject(a), expected);
        assert_eq!(rate_limit_ip_subject(b), expected);
        assert_ne!(rate_limit_ip_subject(other), expected);
    }

    #[test]
    fn ipv4_mapped_ipv6_counts_as_ipv4() {
        let mapped: IpAddr = "::ffff:192.0.2.7".parse().unwrap();
        assert_eq!(
            rate_limit_ip_subject(mapped),
            IpAddr::V4(Ipv4Addr::new(192, 0, 2, 7))
        );
    }

    #[test]
    fn in_memory_ip_limit_is_shared_across_one_slash_64() {
        let mut config = AppConfig::load_from_str("{}\n").expect("default config");
        config.auth_rl_ip_per_min = Some(1);
        config.auth_rl_ip_per_hour = None;
        config.auth_rl_email_per_min = None;
        config.auth_rl_email_per_hour = None;
        let limiter = AuthRateLimiter::new(&config);
        let first: IpAddr = "2001:db8::1".parse().unwrap();
        let rotated: IpAddr = "2001:db8::ffff:2".parse().unwrap();
        let elsewhere: IpAddr = "2001:db8:0:1::1".parse().unwrap();
        assert!(limiter.check(first, "a").is_ok());
        assert!(matches!(
            limiter.check(rotated, "b"),
            Err(RateLimitError::IpLimited)
        ));
        assert!(limiter.check(elsewhere, "c").is_ok());
    }
}
