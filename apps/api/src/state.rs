use std::{
    collections::HashMap,
    sync::{
        atomic::{AtomicI64, Ordering},
        Arc,
    },
};
use tokio::sync::{Mutex, RwLock};

use crate::{
    auth::{
        challenges::ChallengeStore, passkeys::PasskeyAuthenticationStore,
        passkeys::PasskeyRegistrationGrantStore, passkeys::PasskeyRegistrationStore,
        passkeys::PasskeyStore, rate_limit::AuthRateLimiter, session::SessionBackend,
        step_up_tokens::StepUpTokenStore, tokens::TokenStore,
    },
    config::AppConfig,
    mailer::Mailer,
    notifications::WebPushService,
    routes::{edges::Edge, nodes::Node},
    telemetry::Metrics,
};

use async_nats::Client as NatsClient;

/// A cache that provides $O(1)$ lookups by ID while preserving the original
/// load/insertion order for deterministic list responses.
#[derive(Clone, Default)]
pub struct OrderedCache<T> {
    items: HashMap<String, T>,
    order: Vec<String>,
}

impl<T> OrderedCache<T> {
    pub fn new() -> Self {
        Self {
            items: HashMap::new(),
            order: Vec::new(),
        }
    }

    pub fn insert(&mut self, id: String, item: T) -> bool {
        let is_replaced = self.items.insert(id.clone(), item).is_some();
        if !is_replaced {
            self.order.push(id);
        }
        is_replaced
    }

    pub fn iter_in_order(&self) -> impl Iterator<Item = &T> {
        self.order.iter().filter_map(move |id| self.items.get(id))
    }

    pub fn get(&self, id: &str) -> Option<&T> {
        self.items.get(id)
    }

    pub fn remove(&mut self, id: &str) -> Option<T> {
        let removed = self.items.remove(id)?;
        if let Some(position) = self.order.iter().position(|existing_id| existing_id == id) {
            self.order.remove(position);
        }
        Some(removed)
    }

    pub fn len(&self) -> usize {
        self.items.len()
    }

    pub fn is_empty(&self) -> bool {
        self.items.is_empty()
    }
}

use sqlx::PgPool;
use webauthn_rs::prelude::Webauthn;

#[derive(Clone)]
pub struct ApiState {
    pub db_pool: Option<PgPool>,
    pub db_pool_configured: bool,
    pub nats_client: Option<NatsClient>,
    pub nats_configured: bool,
    pub config: AppConfig,
    pub metrics: Metrics,
    pub sessions: SessionBackend,
    pub challenges: ChallengeStore,
    pub tokens: TokenStore,
    pub step_up_tokens: StepUpTokenStore,
    pub accounts: Arc<RwLock<crate::auth::accounts::AccountStore>>,
    pub nodes: Arc<RwLock<OrderedCache<Node>>>,
    pub nodes_persist: Arc<Mutex<()>>,
    /// Serializes account-create persistence (append to JSONL) so concurrent
    /// creates cannot interleave the duplicate-check and the write.
    pub accounts_persist: Arc<Mutex<()>>,
    /// Blocks projection replacement while PostgreSQL-backed requests are
    /// reading the process-local projection. Requests share a read guard;
    /// refreshes take the write guard.
    pub domain_projection_gate: Arc<RwLock<()>>,
    pub domain_projection_version: Arc<AtomicI64>,
    pub edges: Arc<RwLock<OrderedCache<Edge>>>,
    pub rate_limiter: Arc<AuthRateLimiter>,
    pub mailer: Option<Arc<Mailer>>,
    /// WebAuthn instance, present only when passkey support is configured.
    pub webauthn: Option<Arc<Webauthn>>,
    pub passkey_registrations: PasskeyRegistrationStore,
    pub passkey_registration_grants: PasskeyRegistrationGrantStore,
    /// In-progress passkey authentication ceremonies. PostgreSQL-backed
    /// deployments share this TTL-bounded, single-use state across processes.
    pub passkey_authentications: PasskeyAuthenticationStore,
    pub passkeys: PasskeyStore,
    /// Optional VAPID signer and allow-listed outbound Web Push client.
    pub web_push: Option<Arc<WebPushService>>,
}

impl ApiState {
    pub async fn refresh_domain_projection_if_stale(&self) -> anyhow::Result<()> {
        if self.config.domain_read_source != crate::config::DomainReadSource::Postgres {
            return Ok(());
        }
        self.metrics.domain_projection_refresh_check();
        let pool = self
            .db_pool
            .as_ref()
            .ok_or_else(|| anyhow::anyhow!("PostgreSQL domain source has no database pool"))?;
        let observed = crate::domain_db::domain_projection_version(pool).await?;
        if observed == self.domain_projection_version.load(Ordering::Acquire) {
            return Ok(());
        }

        let write_gate_wait_started = std::time::Instant::now();
        let _projection_write = self.domain_projection_gate.write().await;
        self.metrics
            .observe_domain_projection_write_gate_wait(write_gate_wait_started.elapsed());
        let write_gate_hold_started = std::time::Instant::now();

        let observed = match crate::domain_db::domain_projection_version(pool).await {
            Ok(version) => version,
            Err(error) => {
                self.metrics
                    .observe_domain_projection_write_gate_hold(write_gate_hold_started.elapsed());
                return Err(error);
            }
        };
        if observed == self.domain_projection_version.load(Ordering::Acquire) {
            self.metrics
                .observe_domain_projection_write_gate_hold(write_gate_hold_started.elapsed());
            return Ok(());
        }

        let reload_started = std::time::Instant::now();
        let projection = crate::domain_db::load_stable_domain_projection_from_postgres(pool).await;
        let reload_duration = reload_started.elapsed();
        let (accounts, nodes, edges, stable_version) = match projection {
            Ok(projection) => projection,
            Err(error) => {
                self.metrics
                    .observe_domain_projection_reload_failure(reload_duration);
                self.metrics
                    .observe_domain_projection_write_gate_hold(write_gate_hold_started.elapsed());
                return Err(error);
            }
        };
        let account_count = accounts.len();
        let node_count = nodes.len();
        let edge_count = edges.len();

        let mut accounts_guard = self.accounts.write().await;
        let mut nodes_guard = self.nodes.write().await;
        let mut edges_guard = self.edges.write().await;
        *accounts_guard = accounts;
        *nodes_guard = nodes;
        *edges_guard = edges;
        self.metrics.set_nodes_cache_count(nodes_guard.len() as i64);
        self.metrics.set_edges_cache_count(edges_guard.len() as i64);
        self.domain_projection_version
            .store(stable_version, Ordering::Release);
        self.metrics.observe_domain_projection_reload_success(
            reload_duration,
            account_count,
            node_count,
            edge_count,
            stable_version,
        );
        self.metrics
            .observe_domain_projection_write_gate_hold(write_gate_hold_started.elapsed());
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_ordered_cache_id_lookup() {
        let mut cache = OrderedCache::<String>::new();
        cache.insert("id1".to_string(), "item1".to_string());
        cache.insert("id2".to_string(), "item2".to_string());

        assert_eq!(cache.get("id1"), Some(&"item1".to_string()));
        assert_eq!(cache.get("id2"), Some(&"item2".to_string()));
        assert_eq!(cache.get("id3"), None);
    }

    #[test]
    fn test_ordered_cache_deterministic_order() {
        let mut cache = OrderedCache::<String>::new();
        cache.insert("z".to_string(), "item_z".to_string());
        cache.insert("a".to_string(), "item_a".to_string());
        cache.insert("m".to_string(), "item_m".to_string());

        let order: Vec<_> = cache.iter_in_order().collect();
        assert_eq!(
            order,
            vec![
                &"item_z".to_string(),
                &"item_a".to_string(),
                &"item_m".to_string()
            ]
        );
    }

    #[test]
    fn test_ordered_cache_duplicate_last_write_wins_and_stable_order() {
        let mut cache = OrderedCache::<String>::new();
        cache.insert("id1".to_string(), "first".to_string());
        cache.insert("id2".to_string(), "item2".to_string());
        cache.insert("id1".to_string(), "second".to_string());

        assert_eq!(cache.get("id1"), Some(&"second".to_string()));
        assert_eq!(cache.len(), 2);
        // Order must match original insertion of the unique ID
        let order: Vec<_> = cache.iter_in_order().collect();
        assert_eq!(order, vec![&"second".to_string(), &"item2".to_string()]);
    }

    #[test]
    fn test_ordered_cache_remove_updates_lookup_length_and_order() {
        let mut cache = OrderedCache::<String>::new();
        cache.insert("id1".to_string(), "first".to_string());
        cache.insert("id2".to_string(), "second".to_string());

        assert_eq!(cache.remove("id1"), Some("first".to_string()));
        assert_eq!(cache.get("id1"), None);
        assert_eq!(cache.len(), 1);
        assert_eq!(
            cache.iter_in_order().collect::<Vec<_>>(),
            vec![&"second".to_string()]
        );
        assert_eq!(cache.remove("missing"), None);
    }
}
