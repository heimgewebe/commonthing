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
    /// Blocks only the atomic projection replacement while PostgreSQL-backed
    /// requests read the process-local projection. Full database reloads happen
    /// outside this gate so established readers are not frozen by O(N) I/O.
    pub domain_projection_gate: Arc<RwLock<()>>,
    /// Single-flight guard for PostgreSQL projection reloads. Safe read requests
    /// may keep using the previous complete snapshot while one reload owns this.
    pub domain_projection_reload: Arc<Mutex<()>>,
    /// Exact V+1 generation currently being published by a committed local
    /// PostgreSQL node PATCH. -1 means no post-commit handoff is active.
    /// This is deliberately separate from `nodes_persist`: that mutex is also
    /// held before commit and therefore cannot prove that observed drift is ours.
    pub domain_projection_local_node_patch_handoff: Arc<AtomicI64>,
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

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum DomainProjectionFreshness {
    RequireCurrent,
    AllowStaleWhileRefreshing,
}

const NO_LOCAL_NODE_PATCH_HANDOFF: i64 = -1;

fn is_exact_local_node_patch_handoff(
    local_version: i64,
    observed_version: i64,
    handoff_version: i64,
) -> bool {
    local_version.checked_add(1) == Some(observed_version) && handoff_version == observed_version
}

/// Clears the explicit post-commit marker even when a PATCH future is cancelled
/// after PostgreSQL commit but before cache publication has completed.
pub struct LocalNodeProjectionHandoffGuard {
    marker: Arc<AtomicI64>,
    expected_version: i64,
}

impl Drop for LocalNodeProjectionHandoffGuard {
    fn drop(&mut self) {
        let _ = self.marker.compare_exchange(
            self.expected_version,
            NO_LOCAL_NODE_PATCH_HANDOFF,
            Ordering::AcqRel,
            Ordering::Acquire,
        );
    }
}

impl ApiState {
    /// Mark only the post-commit/cache-publication phase of an isolated local
    /// PostgreSQL node PATCH. Callers must already own `nodes_persist` and must
    /// create this guard only after the database mutation has committed.
    pub fn begin_local_node_patch_projection_handoff(
        &self,
        expected_version: i64,
    ) -> LocalNodeProjectionHandoffGuard {
        debug_assert!(expected_version >= 0);
        self.domain_projection_local_node_patch_handoff
            .store(expected_version, Ordering::Release);
        LocalNodeProjectionHandoffGuard {
            marker: self.domain_projection_local_node_patch_handoff.clone(),
            expected_version,
        }
    }
    pub async fn refresh_domain_projection_if_stale(&self) -> anyhow::Result<()> {
        self.refresh_domain_projection(DomainProjectionFreshness::RequireCurrent)
            .await
    }

    /// Refresh for a safe read request. If another request already owns the
    /// reload, or an exact +1 local node generation is in its post-commit cache
    /// handoff, this request may keep using the previous *complete* projection.
    pub async fn refresh_domain_projection_for_read(&self) -> anyhow::Result<()> {
        self.refresh_domain_projection(DomainProjectionFreshness::AllowStaleWhileRefreshing)
            .await
    }

    async fn refresh_domain_projection(
        &self,
        freshness: DomainProjectionFreshness,
    ) -> anyhow::Result<()> {
        if self.config.domain_read_source != crate::config::DomainReadSource::Postgres {
            return Ok(());
        }
        self.metrics.domain_projection_refresh_check();
        let pool = self
            .db_pool
            .as_ref()
            .ok_or_else(|| anyhow::anyhow!("PostgreSQL domain source has no database pool"))?;

        // Coalesce both the cheap generation check and the expensive reload.
        // During a full reload, safe readers must not keep hammering PostgreSQL
        // with one version query per request: that contention made the O(N)
        // snapshot rebuild several times slower at 100k/500k scale. Strict
        // requests wait; safe reads immediately use the previous complete
        // projection while one check/reload owner is active.
        let _reload_guard = match freshness {
            DomainProjectionFreshness::RequireCurrent => self.domain_projection_reload.lock().await,
            DomainProjectionFreshness::AllowStaleWhileRefreshing => {
                match self.domain_projection_reload.try_lock() {
                    Ok(guard) => guard,
                    Err(_) => {
                        self.metrics.domain_projection_refresh_deferred();
                        return Ok(());
                    }
                }
            }
        };

        let observed = crate::domain_db::domain_projection_version(pool).await?;
        let local_version = self.domain_projection_version.load(Ordering::Acquire);
        if observed == local_version {
            return Ok(());
        }

        // `nodes_persist` alone cannot identify this handoff because PostgreSQL
        // mutations also own that mutex before commit. Only the explicit marker,
        // installed after a local PATCH has committed, may classify exact V+1 as
        // our own cache-publication window. This prevents an external V+1 from
        // being hidden merely because an unrelated local write is still blocked
        // before commit.
        let handoff_version = self
            .domain_projection_local_node_patch_handoff
            .load(Ordering::Acquire);
        if is_exact_local_node_patch_handoff(local_version, observed, handoff_version) {
            match freshness {
                DomainProjectionFreshness::AllowStaleWhileRefreshing => {
                    self.metrics.domain_projection_refresh_deferred();
                    tracing::debug!(
                        local_version,
                        observed,
                        "Deferring anonymous projection refresh during committed local node PATCH handoff"
                    );
                    return Ok(());
                }
                DomainProjectionFreshness::RequireCurrent => {
                    // A strict request must not launch an O(N) reload for a
                    // generation the local writer is already publishing. Wait
                    // for the existing serialization guard, then re-check. The
                    // writer clears the explicit handoff marker before releasing
                    // `nodes_persist`.
                    let persist_guard = self.nodes_persist.lock().await;
                    drop(persist_guard);
                    let observed_after_handoff =
                        crate::domain_db::domain_projection_version(pool).await?;
                    let local_after_handoff =
                        self.domain_projection_version.load(Ordering::Acquire);
                    if observed_after_handoff == local_after_handoff {
                        return Ok(());
                    }
                }
            }
        }

        // A same-process domain write can finish after the stable database load
        // but before the atomic cache swap. The request read gate excludes such
        // writes once we acquire the write gate, so recheck the version there and
        // discard/reload rather than overwriting a newer local cache state.
        const MAX_SWAP_ATTEMPTS: usize = 5;
        for attempt in 1..=MAX_SWAP_ATTEMPTS {
            let reload_started = std::time::Instant::now();
            let projection =
                crate::domain_db::load_stable_domain_projection_from_postgres(pool).await;
            let reload_duration = reload_started.elapsed();
            let (accounts, nodes, edges, stable_version) = match projection {
                Ok(projection) => projection,
                Err(error) => {
                    self.metrics
                        .observe_domain_projection_reload_failure(reload_duration);
                    return Err(error);
                }
            };

            let write_gate_wait_started = std::time::Instant::now();
            let _projection_write = self.domain_projection_gate.write().await;
            self.metrics
                .observe_domain_projection_write_gate_wait(write_gate_wait_started.elapsed());
            let write_gate_hold_started = std::time::Instant::now();

            let latest_version = match crate::domain_db::domain_projection_version(pool).await {
                Ok(version) => version,
                Err(error) => {
                    self.metrics.observe_domain_projection_write_gate_hold(
                        write_gate_hold_started.elapsed(),
                    );
                    return Err(error);
                }
            };
            if latest_version != stable_version {
                self.metrics
                    .observe_domain_projection_write_gate_hold(write_gate_hold_started.elapsed());
                tracing::debug!(
                    attempt,
                    stable_version,
                    latest_version,
                    "Domain projection advanced before cache swap; reloading outside request gate"
                );
                if attempt == MAX_SWAP_ATTEMPTS {
                    anyhow::bail!(
                        "domain projection advanced before cache swap after {MAX_SWAP_ATTEMPTS} attempts"
                    );
                }
                continue;
            }

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
            return Ok(());
        }

        unreachable!("bounded projection reload loop always returns")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn local_node_generation_handoff_deferral_is_narrow() {
        assert!(is_exact_local_node_patch_handoff(41, 42, 42));
        assert!(!is_exact_local_node_patch_handoff(41, 42, -1));
        assert!(!is_exact_local_node_patch_handoff(41, 43, 42));
        assert!(!is_exact_local_node_patch_handoff(41, 42, 43));
        assert!(!is_exact_local_node_patch_handoff(
            i64::MAX,
            i64::MIN,
            i64::MIN,
        ));
    }

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
