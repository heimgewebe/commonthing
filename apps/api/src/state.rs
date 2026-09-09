use std::{
    collections::HashMap,
    sync::{
        atomic::{AtomicI64, Ordering},
        Arc, OnceLock,
    },
};
use tokio::sync::{Mutex, Notify, RwLock};

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
    /// Exact V+1 generation reserved by a local PostgreSQL node PATCH after
    /// its trigger updated the transaction-locked projection-state row. The
    /// marker is installed before COMMIT and remains through cache publication;
    /// other connections cannot observe that V+1 until COMMIT. -1 means none.
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
const DOMAIN_PROJECTION_CLASSIFICATION_LIMIT: std::time::Duration =
    std::time::Duration::from_secs(1);
const MAX_DOMAIN_PROJECTION_CLASSIFICATION_ATTEMPTS: usize = 64;

fn local_node_patch_handoff_notify() -> &'static Notify {
    static NOTIFY: OnceLock<Notify> = OnceLock::new();
    NOTIFY.get_or_init(Notify::new)
}

#[derive(Clone, Copy, Debug)]
struct DomainProjectionClassificationBudget {
    deadline: tokio::time::Instant,
    attempts_remaining: usize,
}

impl DomainProjectionClassificationBudget {
    fn new() -> Self {
        Self {
            deadline: tokio::time::Instant::now() + DOMAIN_PROJECTION_CLASSIFICATION_LIMIT,
            attempts_remaining: MAX_DOMAIN_PROJECTION_CLASSIFICATION_ATTEMPTS,
        }
    }

    fn try_begin_attempt(&mut self) -> bool {
        if self.attempts_remaining == 0 || tokio::time::Instant::now() >= self.deadline {
            return false;
        }
        self.attempts_remaining -= 1;
        true
    }

    fn deadline(&self) -> tokio::time::Instant {
        self.deadline
    }
}

async fn domain_projection_version_before_deadline(
    pool: &PgPool,
    deadline: tokio::time::Instant,
) -> anyhow::Result<i64> {
    match tokio::time::timeout_at(deadline, crate::domain_db::domain_projection_version(pool)).await
    {
        Ok(result) => result,
        Err(_) => anyhow::bail!("timed out classifying domain projection freshness"),
    }
}

async fn wait_for_local_node_patch_handoff_completion(
    marker: &AtomicI64,
    observed_version: i64,
    deadline: tokio::time::Instant,
) -> anyhow::Result<()> {
    loop {
        if marker.load(Ordering::Acquire) != observed_version {
            return Ok(());
        }

        // `notified()` is not registered until it is first polled/enabled. Pin
        // and enable it before the second marker read so a concurrent clear
        // cannot be lost between checking the marker and starting to wait.
        let notified = local_node_patch_handoff_notify().notified();
        tokio::pin!(notified);
        let _ = notified.as_mut().enable();
        if marker.load(Ordering::Acquire) != observed_version {
            return Ok(());
        }

        // Notify is only the bell. After every wake-up the atomic marker is read
        // again at the top of the loop and remains the sole source of truth.
        if tokio::time::timeout_at(deadline, notified).await.is_err() {
            anyhow::bail!(
                "timed out waiting for local node projection handoff generation {observed_version}"
            );
        }
    }
}

fn is_exact_local_node_patch_handoff(
    local_version: i64,
    observed_version: i64,
    handoff_version: i64,
) -> bool {
    local_version.checked_add(1) == Some(observed_version) && handoff_version == observed_version
}

/// Clears the explicit transaction-proven handoff marker even when a PATCH
/// future is cancelled after PostgreSQL commit but before cache publication.
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
        // A wake-up carries no state. Waiters always re-read the atomic marker,
        // so notifying after a failed CAS is a harmless spurious wake-up.
        local_node_patch_handoff_notify().notify_waiters();
    }
}

impl ApiState {
    /// Mark only a transaction-proven exact V+1 local PostgreSQL node PATCH.
    /// Callers must already own `nodes_persist` and may create this guard only
    /// after the PATCH trigger has updated and locked `domain_projection_state`
    /// in the same transaction. The guard must survive COMMIT and cache publish.
    /// A second concurrent marker is refused rather than overwritten.
    pub fn begin_local_node_patch_projection_handoff(
        &self,
        expected_version: i64,
    ) -> Option<LocalNodeProjectionHandoffGuard> {
        debug_assert!(expected_version >= 0);
        match self
            .domain_projection_local_node_patch_handoff
            .compare_exchange(
                NO_LOCAL_NODE_PATCH_HANDOFF,
                expected_version,
                Ordering::AcqRel,
                Ordering::Acquire,
            ) {
            Ok(_) => Some(LocalNodeProjectionHandoffGuard {
                marker: self.domain_projection_local_node_patch_handoff.clone(),
                expected_version,
            }),
            Err(active_version) => {
                tracing::error!(
                    expected_version,
                    active_version,
                    "Refusing to overwrite an active local node projection handoff"
                );
                None
            }
        }
    }
    pub async fn refresh_domain_projection_if_stale(&self) -> anyhow::Result<()> {
        self.refresh_domain_projection(DomainProjectionFreshness::RequireCurrent)
            .await
    }

    /// Refresh for a safe read request. If another request already owns the
    /// reload, or an exact +1 local node generation is in its commit/cache
    /// publication handoff, this request may keep using the previous *complete* projection.
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

        // Reclassify after every completed exact local handoff. The whole cheap
        // classification phase gets one cumulative deadline plus an explicit
        // iteration cap: DB re-reads, repeated generation changes and marker waits
        // cannot hold the single-flight reload coordinator indefinitely.
        let mut classification_budget = DomainProjectionClassificationBudget::new();
        loop {
            if !classification_budget.try_begin_attempt() {
                tracing::warn!(
                    classification_limit_ms = DOMAIN_PROJECTION_CLASSIFICATION_LIMIT.as_millis(),
                    max_attempts = MAX_DOMAIN_PROJECTION_CLASSIFICATION_ATTEMPTS,
                    "Domain projection freshness classification budget exhausted"
                );
                anyhow::bail!("domain projection freshness classification budget exhausted");
            }

            let observed =
                domain_projection_version_before_deadline(pool, classification_budget.deadline())
                    .await?;

            // The writer publishes the local projection version before its RAII
            // guard clears the handoff marker. Read the marker first: if this
            // acquire observes that clear, the following version load also sees
            // the preceding publication; if it still sees the active marker, the
            // exact handoff remains classifiable. Reading these in the opposite
            // order permits a completed handoff to look like foreign drift.
            let handoff_version = self
                .domain_projection_local_node_patch_handoff
                .load(Ordering::Acquire);
            let local_version = self.domain_projection_version.load(Ordering::Acquire);
            if observed == local_version {
                return Ok(());
            }

            // The DB read happens before the local atomics. A writer can publish
            // a complete V+1 cache after that query, making the first observation
            // V while `local_version` is already V+1. Do not mistake that harmless
            // stale DB read for foreign drift and launch O(N) reconciliation.
            // Re-read only in this local-ahead case. Equality proves catch-up; a
            // stable lower DB generation still falls through to reconciliation,
            // preserving restore/PITR semantics instead of trusting newer cache
            // state across a genuine database rollback.
            if observed < local_version {
                let confirmed_observed = domain_projection_version_before_deadline(
                    pool,
                    classification_budget.deadline(),
                )
                .await?;
                if confirmed_observed == local_version {
                    return Ok(());
                }
                if confirmed_observed != observed {
                    continue;
                }
            }

            // `nodes_persist` alone cannot identify this handoff because PostgreSQL
            // mutations also own that mutex before commit. Only the explicit marker,
            // installed only after the PATCH transaction has itself updated and
            // locked the projection-state row, may classify exact V+1 as our own
            // commit/cache-publication window. This prevents an external V+1 from
            // being hidden merely because an unrelated local write is still blocked
            // before commit.
            if !is_exact_local_node_patch_handoff(local_version, observed, handoff_version) {
                break;
            }

            match freshness {
                DomainProjectionFreshness::AllowStaleWhileRefreshing => {
                    self.metrics.domain_projection_refresh_deferred();
                    tracing::debug!(
                        local_version,
                        observed,
                        "Deferring anonymous projection refresh during transaction-proven local node PATCH handoff"
                    );
                    return Ok(());
                }
                DomainProjectionFreshness::RequireCurrent => {
                    // A strict request must not launch an O(N) reload for a
                    // generation the local writer is already publishing. Notify is
                    // only the wake-up signal; the explicit marker remains the truth.
                    // This wait shares the same deadline as every DB re-read above.
                    if let Err(error) = wait_for_local_node_patch_handoff_completion(
                        &self.domain_projection_local_node_patch_handoff,
                        observed,
                        classification_budget.deadline(),
                    )
                    .await
                    {
                        tracing::warn!(
                            observed_version = observed,
                            classification_limit_ms =
                                DOMAIN_PROJECTION_CLASSIFICATION_LIMIT.as_millis(),
                            error = %error,
                            "Timed out waiting for transaction-proven local node PATCH handoff"
                        );
                        return Err(error);
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
    fn classification_budget_bounds_repeated_generation_changes() {
        let mut budget = DomainProjectionClassificationBudget::new();
        let generations =
            (0..=(MAX_DOMAIN_PROJECTION_CLASSIFICATION_ATTEMPTS as i64 + 1)).collect::<Vec<_>>();
        let mut attempts = 0usize;

        for pair in generations.windows(2) {
            if !budget.try_begin_attempt() {
                break;
            }
            assert_ne!(pair[0], pair[1]);
            attempts += 1;
        }

        assert_eq!(attempts, MAX_DOMAIN_PROJECTION_CLASSIFICATION_ATTEMPTS);
        assert!(!budget.try_begin_attempt());
    }

    #[tokio::test(start_paused = true)]
    async fn classification_budget_counts_elapsed_time_cumulatively() {
        let mut budget = DomainProjectionClassificationBudget::new();
        assert!(budget.try_begin_attempt());

        tokio::time::advance(DOMAIN_PROJECTION_CLASSIFICATION_LIMIT).await;

        assert!(!budget.try_begin_attempt());
    }

    #[tokio::test(start_paused = true)]
    async fn handoff_guard_drop_wakes_registered_waiter_without_polling() {
        let marker = Arc::new(AtomicI64::new(42));
        let guard = LocalNodeProjectionHandoffGuard {
            marker: marker.clone(),
            expected_version: 42,
        };
        let waiter_marker = marker.clone();
        let waiter = tokio::spawn(async move {
            wait_for_local_node_patch_handoff_completion(
                &waiter_marker,
                42,
                tokio::time::Instant::now() + DOMAIN_PROJECTION_CLASSIFICATION_LIMIT,
            )
            .await
        });
        tokio::task::yield_now().await;
        assert!(!waiter.is_finished());

        drop(guard);
        tokio::task::yield_now().await;

        waiter.await.expect("waiter task").expect("handoff wake");
        assert_eq!(marker.load(Ordering::Acquire), NO_LOCAL_NODE_PATCH_HANDOFF);
    }

    #[tokio::test(start_paused = true)]
    async fn handoff_clear_before_waiter_registration_is_not_lost() {
        let marker = Arc::new(AtomicI64::new(42));
        let guard = LocalNodeProjectionHandoffGuard {
            marker: marker.clone(),
            expected_version: 42,
        };
        let waiter_marker = marker.clone();
        let waiter = tokio::spawn(async move {
            wait_for_local_node_patch_handoff_completion(
                &waiter_marker,
                42,
                tokio::time::Instant::now() + DOMAIN_PROJECTION_CLASSIFICATION_LIMIT,
            )
            .await
        });

        // `spawn` does not synchronously poll the future. Clear + notify before
        // yielding so the waiter starts only after the wake-up already happened.
        drop(guard);
        tokio::task::yield_now().await;

        waiter
            .await
            .expect("waiter task")
            .expect("marker recheck after early wake");
    }

    #[tokio::test(start_paused = true)]
    async fn spurious_notify_does_not_complete_an_active_handoff() {
        let marker = Arc::new(AtomicI64::new(42));
        let waiter_marker = marker.clone();
        let waiter = tokio::spawn(async move {
            wait_for_local_node_patch_handoff_completion(
                &waiter_marker,
                42,
                tokio::time::Instant::now() + DOMAIN_PROJECTION_CLASSIFICATION_LIMIT,
            )
            .await
        });
        tokio::task::yield_now().await;

        local_node_patch_handoff_notify().notify_waiters();
        tokio::task::yield_now().await;
        assert!(!waiter.is_finished());

        marker.store(NO_LOCAL_NODE_PATCH_HANDOFF, Ordering::Release);
        local_node_patch_handoff_notify().notify_waiters();
        tokio::task::yield_now().await;
        waiter
            .await
            .expect("waiter task")
            .expect("real marker clear");
    }

    #[tokio::test(start_paused = true)]
    async fn handoff_wait_timeout_remains_fail_closed() {
        let marker = Arc::new(AtomicI64::new(42));
        let waiter_marker = marker.clone();
        let waiter = tokio::spawn(async move {
            wait_for_local_node_patch_handoff_completion(
                &waiter_marker,
                42,
                tokio::time::Instant::now() + DOMAIN_PROJECTION_CLASSIFICATION_LIMIT,
            )
            .await
        });
        tokio::task::yield_now().await;

        tokio::time::advance(DOMAIN_PROJECTION_CLASSIFICATION_LIMIT).await;
        tokio::task::yield_now().await;

        let error = waiter
            .await
            .expect("waiter task")
            .expect_err("stuck marker must fail closed");
        assert!(error
            .to_string()
            .contains("timed out waiting for local node projection handoff generation 42"));
    }

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
