use std::{
    future::Future,
    pin::Pin,
    sync::Arc,
    task::{Context, Poll},
    time::Duration,
};

pub mod health;

use axum::{
    extract::{MatchedPath, State},
    http::{header, HeaderValue, Request, StatusCode},
    response::{IntoResponse, Response},
};
use prometheus::{
    Encoder, Histogram, HistogramOpts, HistogramVec, IntCounter, IntCounterVec, IntGauge,
    IntGaugeVec, Opts, Registry, TextEncoder,
};
use tower::{Layer, Service};

use crate::state::ApiState;

const UNMATCHED_ROUTE_LABEL: &str = "<unmatched>";

/// Fixed set of search service request outcomes. The label set is bounded by
/// the type system rather than by a runtime string match. Successful search
/// modes are mapped exhaustively through the typed `SearchMode -> SearchRequestOutcome`
/// conversion; error outcomes remain exhaustively matched at the request boundary.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SearchRequestOutcome {
    Hybrid,
    LexicalFallback,
    ProviderContractError,
    Unavailable,
    InvalidRequest,
    Internal,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NodeMutationOutcome {
    Success,
    Conflict,
    RateLimited,
    Forbidden,
    Invalid,
    NotFound,
    Unavailable,
    Internal,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NodeMutationJsonlRecoveryOutcome {
    Committed,
    Aborted,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DomainEventWorker {
    Relay,
    ReceiptConsumer,
}

impl DomainEventWorker {
    fn as_label(self) -> &'static str {
        match self {
            Self::Relay => "relay",
            Self::ReceiptConsumer => "receipt_consumer",
        }
    }
}

impl NodeMutationJsonlRecoveryOutcome {
    fn as_label(self) -> &'static str {
        match self {
            Self::Committed => "committed",
            Self::Aborted => "aborted",
        }
    }
}

impl NodeMutationOutcome {
    fn as_label(self) -> &'static str {
        match self {
            Self::Success => "success",
            Self::Conflict => "conflict",
            Self::RateLimited => "rate_limited",
            Self::Forbidden => "forbidden",
            Self::Invalid => "invalid",
            Self::NotFound => "not_found",
            Self::Unavailable => "unavailable",
            Self::Internal => "internal",
        }
    }

    pub fn from_status(status: StatusCode) -> Self {
        match status {
            StatusCode::OK | StatusCode::NO_CONTENT => Self::Success,
            StatusCode::CONFLICT
            | StatusCode::PRECONDITION_FAILED
            | StatusCode::PRECONDITION_REQUIRED => Self::Conflict,
            StatusCode::TOO_MANY_REQUESTS => Self::RateLimited,
            StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN => Self::Forbidden,
            StatusCode::BAD_REQUEST | StatusCode::UNPROCESSABLE_ENTITY => Self::Invalid,
            StatusCode::NOT_FOUND => Self::NotFound,
            StatusCode::SERVICE_UNAVAILABLE => Self::Unavailable,
            _ => Self::Internal,
        }
    }
}

impl SearchRequestOutcome {
    fn as_label(self) -> &'static str {
        match self {
            Self::Hybrid => "hybrid",
            Self::LexicalFallback => "lexical_fallback",
            Self::ProviderContractError => "provider_contract_error",
            Self::Unavailable => "unavailable",
            Self::InvalidRequest => "invalid_request",
            Self::Internal => "internal",
        }
    }
}

fn metrics_path_label<B>(request: &Request<B>) -> String {
    request
        .extensions()
        .get::<MatchedPath>()
        .map(|path| path.as_str().to_owned())
        .unwrap_or_else(|| UNMATCHED_ROUTE_LABEL.to_owned())
}

#[cfg(debug_assertions)]
const GIT_COMMIT_SHA: &str = match option_env!("GIT_COMMIT_SHA") {
    Some(value) => value,
    None => "unknown",
};
#[cfg(not(debug_assertions))]
const GIT_COMMIT_SHA: &str = env!(
    "GIT_COMMIT_SHA",
    "release builds require GIT_COMMIT_SHA to bind the API binary to its source commit"
);

#[cfg(debug_assertions)]
const BUILD_TIMESTAMP: &str = match option_env!("BUILD_TIMESTAMP") {
    Some(value) => value,
    None => "unknown",
};
#[cfg(not(debug_assertions))]
const BUILD_TIMESTAMP: &str = env!(
    "BUILD_TIMESTAMP",
    "release builds require BUILD_TIMESTAMP to bind the API binary to a deterministic Git timestamp"
);

#[derive(Clone, Debug)]
pub struct BuildInfo {
    pub version: &'static str,
    pub commit: &'static str,
    pub build_timestamp: &'static str,
}

impl BuildInfo {
    pub fn collect() -> Self {
        Self {
            version: env!("CARGO_PKG_VERSION"),
            commit: GIT_COMMIT_SHA,
            build_timestamp: BUILD_TIMESTAMP,
        }
    }
}

#[derive(Clone)]
pub struct Metrics {
    inner: Arc<MetricsInner>,
}

struct MetricsInner {
    registry: Registry,
    pub http_requests_total: IntCounterVec,
    pub nodes_cache_count: IntGauge,
    pub edges_cache_count: IntGauge,
    pub search_projection_jobs_total: IntCounterVec,
    pub search_requests_total: IntCounterVec,
    pub search_candidate_set_overflow_total: IntCounter,
    pub search_request_duration_seconds: Histogram,
    pub search_repository_duration_seconds: Histogram,
    pub search_provider_duration_seconds: Histogram,
    pub search_authorized_candidates: Histogram,
    pub search_lexical_candidates: Histogram,
    pub node_mutations_total: IntCounterVec,
    pub node_mutation_conflicts_total: IntCounterVec,
    pub node_mutation_admin_bypass_total: IntCounterVec,
    pub node_mutation_jsonl_recovery_total: IntCounterVec,
    pub node_mutation_duration_seconds: HistogramVec,
    pub domain_projection_events_total: IntCounterVec,
    pub domain_projection_duration_seconds: HistogramVec,
    pub domain_projection_rows_loaded_total: IntCounterVec,
    pub domain_projection_snapshot: IntGaugeVec,
    pub domain_event_worker_up: IntGaugeVec,
    pub domain_event_chain_snapshot_up: IntGauge,
    pub domain_outbox_actionable_pending: IntGauge,
    pub domain_outbox_quarantine_present: IntGauge,
    pub domain_outbox_oldest_actionable_age_seconds: IntGauge,
    pub domain_event_receipt_probe_missing: IntGauge,
    pub domain_event_receipt_probe_age_seconds: IntGauge,
}

impl Metrics {
    pub fn try_new(build_info: BuildInfo) -> Result<Self, prometheus::Error> {
        let http_opts = Opts::new("http_requests_total", "Total number of HTTP requests");
        let http_requests_total = IntCounterVec::new(http_opts, &["method", "path", "status"])?;

        let build_opts = Opts::new("build_info", "Build information for the API");
        let build_info_metric =
            IntGaugeVec::new(build_opts, &["version", "commit", "build_timestamp"])?;

        let nodes_count_opts = Opts::new("nodes_cache_count", "Number of nodes in memory cache");
        let nodes_cache_count = IntGauge::with_opts(nodes_count_opts)?;

        let edges_count_opts = Opts::new("edges_cache_count", "Number of edges in memory cache");
        let edges_cache_count = IntGauge::with_opts(edges_count_opts)?;

        let search_projection_jobs_total = IntCounterVec::new(
            Opts::new(
                "search_projection_jobs_total",
                "Projection worker outcomes without node, text, query, or vector labels",
            ),
            &["outcome"],
        )?;

        let search_requests_total = IntCounterVec::new(
            Opts::new(
                "search_requests_total",
                "Search service outcomes using only fixed, non-user-controlled labels",
            ),
            &["outcome"],
        )?;

        let search_candidate_set_overflow_total = IntCounter::new(
            "search_candidate_set_overflow_total",
            "Search requests rejected because the authorized candidate transfer exceeded its hard bound",
        )?;

        let duration_buckets = vec![
            0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
        ];
        // MAX_AUTHORIZED_CANDIDATES (search/repository.rs) hard-bounds successful
        // observed values at 1000. Keep the historical 1001 bucket as a legacy
        // compatibility series for existing Prometheus queries and dashboards.
        // It is intentionally redundant with 1000 and is not an overflow signal;
        // overflows are reported via search_candidate_set_overflow_total.
        let candidate_buckets = vec![
            1.0, 5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 750.0, 1000.0, 1001.0,
        ];
        let lexical_candidate_buckets = vec![0.0, 1.0, 2.0, 5.0, 10.0];
        let search_request_duration_seconds = Histogram::with_opts(
            HistogramOpts::new(
                "search_request_duration_seconds",
                "End-to-end search service duration without query or identity labels",
            )
            .buckets(duration_buckets.clone()),
        )?;
        let search_repository_duration_seconds = Histogram::with_opts(
            HistogramOpts::new(
                "search_repository_duration_seconds",
                "PostgreSQL search candidate retrieval duration without query or identity labels",
            )
            .buckets(duration_buckets.clone()),
        )?;
        let search_provider_duration_seconds = Histogram::with_opts(
            HistogramOpts::new(
                "search_provider_duration_seconds",
                "Search query embedding provider duration without query, model, or vector labels",
            )
            .buckets(duration_buckets),
        )?;
        let search_authorized_candidates = Histogram::with_opts(
            HistogramOpts::new(
                "search_authorized_candidates",
                "Authorized search candidates transferred to the API per request",
            )
            .buckets(candidate_buckets.clone()),
        )?;
        let search_lexical_candidates = Histogram::with_opts(
            HistogramOpts::new(
                "search_lexical_candidates",
                "Lexical candidates in the authoritative top-ten prefix per search request",
            )
            .buckets(lexical_candidate_buckets),
        )?;
        let node_mutations_total = IntCounterVec::new(
            Opts::new(
                "node_mutations_total",
                "Collective node mutation outcomes with fixed operation and outcome labels",
            ),
            &["operation", "outcome"],
        )?;
        let node_mutation_conflicts_total = IntCounterVec::new(
            Opts::new(
                "node_mutation_conflicts_total",
                "Collective node write conflicts without node or account labels",
            ),
            &["operation"],
        )?;
        let node_mutation_admin_bypass_total = IntCounterVec::new(
            Opts::new(
                "node_mutation_admin_bypass_total",
                "Explicit administrator emergency rate-limit bypasses",
            ),
            &["operation"],
        )?;
        let node_mutation_jsonl_recovery_total = IntCounterVec::new(
            Opts::new(
                "node_mutation_jsonl_recovery_total",
                "JSONL mutation audit recovery outcomes without identifiers",
            ),
            &["outcome"],
        )?;
        let node_mutation_duration_seconds = HistogramVec::new(
            HistogramOpts::new(
                "node_mutation_duration_seconds",
                "Collective node mutation latency without node or account labels",
            )
            .buckets(vec![
                0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0,
            ]),
            &["operation"],
        )?;
        let domain_projection_events_total = IntCounterVec::new(
            Opts::new(
                "domain_projection_events_total",
                "Domain projection refresh events with a fixed bounded event label",
            ),
            &["event"],
        )?;
        let projection_duration_buckets = vec![
            0.0001, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0,
        ];
        let domain_projection_duration_seconds = HistogramVec::new(
            HistogramOpts::new(
                "domain_projection_duration_seconds",
                "Domain projection gate waits and full reload duration with a fixed bounded phase label",
            )
            .buckets(projection_duration_buckets),
            &["phase"],
        )?;
        let domain_projection_rows_loaded_total = IntCounterVec::new(
            Opts::new(
                "domain_projection_rows_loaded_total",
                "Rows loaded by completed domain projection reloads with a fixed aggregate-kind label",
            ),
            &["kind"],
        )?;
        let domain_projection_snapshot = IntGaugeVec::new(
            Opts::new(
                "domain_projection_snapshot",
                "Current domain projection aggregate row counts and committed version",
            ),
            &["kind"],
        )?;
        let domain_event_worker_up = IntGaugeVec::new(
            Opts::new(
                "domain_event_worker_up",
                "Liveness of the essential transactional outbox workers",
            ),
            &["worker"],
        )?;
        let domain_event_chain_snapshot_up = IntGauge::new(
            "domain_event_chain_snapshot_up",
            "Whether the bounded database health snapshot for the domain event chain succeeded",
        )?;
        let domain_outbox_actionable_pending = IntGauge::new(
            "domain_outbox_actionable_pending",
            "Whether at least one non-quarantined domain event is currently eligible for relay",
        )?;
        let domain_outbox_quarantine_present = IntGauge::new(
            "domain_outbox_quarantine_present",
            "Whether at least one unpublished domain event is intentionally quarantined",
        )?;
        let domain_outbox_oldest_actionable_age_seconds = IntGauge::new(
            "domain_outbox_oldest_actionable_age_seconds",
            "Age in seconds since the oldest currently actionable domain event became eligible",
        )?;
        let domain_event_receipt_probe_missing = IntGauge::new(
            "domain_event_receipt_probe_missing",
            "Whether the bounded current-health receipt probe found a missing durable receipt",
        )?;
        let domain_event_receipt_probe_age_seconds = IntGauge::new(
            "domain_event_receipt_probe_age_seconds",
            "Age in seconds of the event selected by the bounded missing-receipt health probe",
        )?;

        let registry = Registry::new();
        registry.register(Box::new(http_requests_total.clone()))?;
        registry.register(Box::new(build_info_metric.clone()))?;
        registry.register(Box::new(nodes_cache_count.clone()))?;
        registry.register(Box::new(edges_cache_count.clone()))?;
        registry.register(Box::new(search_projection_jobs_total.clone()))?;
        registry.register(Box::new(search_requests_total.clone()))?;
        registry.register(Box::new(search_candidate_set_overflow_total.clone()))?;
        registry.register(Box::new(search_request_duration_seconds.clone()))?;
        registry.register(Box::new(search_repository_duration_seconds.clone()))?;
        registry.register(Box::new(search_provider_duration_seconds.clone()))?;
        registry.register(Box::new(search_authorized_candidates.clone()))?;
        registry.register(Box::new(search_lexical_candidates.clone()))?;
        registry.register(Box::new(node_mutations_total.clone()))?;
        registry.register(Box::new(node_mutation_conflicts_total.clone()))?;
        registry.register(Box::new(node_mutation_admin_bypass_total.clone()))?;
        registry.register(Box::new(node_mutation_jsonl_recovery_total.clone()))?;
        registry.register(Box::new(node_mutation_duration_seconds.clone()))?;
        registry.register(Box::new(domain_projection_events_total.clone()))?;
        registry.register(Box::new(domain_projection_duration_seconds.clone()))?;
        registry.register(Box::new(domain_projection_rows_loaded_total.clone()))?;
        registry.register(Box::new(domain_projection_snapshot.clone()))?;
        registry.register(Box::new(domain_event_worker_up.clone()))?;
        registry.register(Box::new(domain_event_chain_snapshot_up.clone()))?;
        registry.register(Box::new(domain_outbox_actionable_pending.clone()))?;
        registry.register(Box::new(domain_outbox_quarantine_present.clone()))?;
        registry.register(Box::new(
            domain_outbox_oldest_actionable_age_seconds.clone(),
        ))?;
        registry.register(Box::new(domain_event_receipt_probe_missing.clone()))?;
        registry.register(Box::new(domain_event_receipt_probe_age_seconds.clone()))?;

        build_info_metric
            .with_label_values(&[
                build_info.version,
                build_info.commit,
                build_info.build_timestamp,
            ])
            .set(1);

        Ok(Self {
            inner: Arc::new(MetricsInner {
                registry,
                http_requests_total,
                nodes_cache_count,
                edges_cache_count,
                search_projection_jobs_total,
                search_requests_total,
                search_candidate_set_overflow_total,
                search_request_duration_seconds,
                search_repository_duration_seconds,
                search_provider_duration_seconds,
                search_authorized_candidates,
                search_lexical_candidates,
                node_mutations_total,
                node_mutation_conflicts_total,
                node_mutation_admin_bypass_total,
                node_mutation_jsonl_recovery_total,
                node_mutation_duration_seconds,
                domain_projection_events_total,
                domain_projection_duration_seconds,
                domain_projection_rows_loaded_total,
                domain_projection_snapshot,
                domain_event_worker_up,
                domain_event_chain_snapshot_up,
                domain_outbox_actionable_pending,
                domain_outbox_quarantine_present,
                domain_outbox_oldest_actionable_age_seconds,
                domain_event_receipt_probe_missing,
                domain_event_receipt_probe_age_seconds,
            }),
        })
    }

    pub fn set_nodes_cache_count(&self, count: i64) {
        self.inner.nodes_cache_count.set(count);
    }

    pub fn set_edges_cache_count(&self, count: i64) {
        self.inner.edges_cache_count.set(count);
    }

    pub fn http_requests_total(&self) -> &IntCounterVec {
        &self.inner.http_requests_total
    }

    /// Outcome labels are from a fixed enum; callers must never attach node
    /// identifiers, source text, queries, provider responses, or vectors.
    pub fn search_projection_outcome(&self, outcome: &str) {
        self.inner
            .search_projection_jobs_total
            .with_label_values(&[outcome])
            .inc();
    }

    /// Queries, identities, node ids, and provider text must never become
    /// labels; `SearchRequestOutcome` bounds the label set at compile time.
    pub fn search_request_outcome(&self, outcome: SearchRequestOutcome) {
        self.inner
            .search_requests_total
            .with_label_values(&[outcome.as_label()])
            .inc();
    }

    pub fn search_candidate_set_overflow(&self) {
        self.inner.search_candidate_set_overflow_total.inc();
    }

    pub fn observe_search_request_duration(&self, duration: Duration) {
        self.inner
            .search_request_duration_seconds
            .observe(duration.as_secs_f64());
    }

    pub fn observe_search_repository_duration(&self, duration: Duration) {
        self.inner
            .search_repository_duration_seconds
            .observe(duration.as_secs_f64());
    }

    pub fn observe_search_provider_duration(&self, duration: Duration) {
        self.inner
            .search_provider_duration_seconds
            .observe(duration.as_secs_f64());
    }

    pub fn observe_search_candidate_counts(&self, authorized: usize, lexical: usize) {
        self.inner
            .search_authorized_candidates
            .observe(authorized as f64);
        self.inner.search_lexical_candidates.observe(lexical as f64);
    }

    pub fn observe_node_mutation(
        &self,
        operation: crate::node_mutation::NodeMutationOperation,
        outcome: NodeMutationOutcome,
        duration: Duration,
        admin_bypass: bool,
    ) {
        let operation = operation.as_str();
        self.inner
            .node_mutations_total
            .with_label_values(&[operation, outcome.as_label()])
            .inc();
        self.inner
            .node_mutation_duration_seconds
            .with_label_values(&[operation])
            .observe(duration.as_secs_f64());
        if outcome == NodeMutationOutcome::Conflict {
            self.inner
                .node_mutation_conflicts_total
                .with_label_values(&[operation])
                .inc();
        }
        if admin_bypass {
            self.inner
                .node_mutation_admin_bypass_total
                .with_label_values(&[operation])
                .inc();
        }
    }

    pub fn node_mutation_jsonl_recovery(
        &self,
        outcome: NodeMutationJsonlRecoveryOutcome,
        count: u64,
    ) {
        self.inner
            .node_mutation_jsonl_recovery_total
            .with_label_values(&[outcome.as_label()])
            .inc_by(count);
    }

    pub fn domain_projection_refresh_check(&self) {
        self.inner
            .domain_projection_events_total
            .with_label_values(&["refresh_check"])
            .inc();
    }
    pub fn domain_projection_refresh_failed(&self) {
        self.inner
            .domain_projection_events_total
            .with_label_values(&["refresh_failure"])
            .inc();
    }
    pub fn domain_projection_refresh_deferred(&self) {
        self.inner
            .domain_projection_events_total
            .with_label_values(&["refresh_deferred"])
            .inc();
    }
    pub fn observe_domain_projection_write_gate_wait(&self, duration: Duration) {
        self.inner
            .domain_projection_duration_seconds
            .with_label_values(&["write_gate_wait"])
            .observe(duration.as_secs_f64());
    }
    pub fn observe_domain_projection_write_gate_hold(&self, duration: Duration) {
        self.inner
            .domain_projection_duration_seconds
            .with_label_values(&["write_gate_hold"])
            .observe(duration.as_secs_f64());
    }
    pub fn observe_domain_projection_read_gate_wait(&self, duration: Duration) {
        self.inner
            .domain_projection_duration_seconds
            .with_label_values(&["read_gate_wait"])
            .observe(duration.as_secs_f64());
    }
    pub fn observe_domain_projection_reload_success(
        &self,
        duration: Duration,
        accounts: usize,
        nodes: usize,
        edges: usize,
        version: i64,
    ) {
        self.inner
            .domain_projection_events_total
            .with_label_values(&["reload_success"])
            .inc();
        self.inner
            .domain_projection_duration_seconds
            .with_label_values(&["reload"])
            .observe(duration.as_secs_f64());
        for (kind, count) in [("accounts", accounts), ("nodes", nodes), ("edges", edges)] {
            self.inner
                .domain_projection_rows_loaded_total
                .with_label_values(&[kind])
                .inc_by(count as u64);
            self.inner
                .domain_projection_snapshot
                .with_label_values(&[kind])
                .set(count as i64);
        }
        self.inner
            .domain_projection_snapshot
            .with_label_values(&["version"])
            .set(version);
    }
    pub fn set_domain_projection_version(&self, version: i64) {
        self.inner
            .domain_projection_snapshot
            .with_label_values(&["version"])
            .set(version);
    }
    pub fn observe_domain_projection_reload_failure(&self, duration: Duration) {
        self.inner
            .domain_projection_events_total
            .with_label_values(&["reload_failure"])
            .inc();
        self.inner
            .domain_projection_duration_seconds
            .with_label_values(&["reload"])
            .observe(duration.as_secs_f64());
    }
    pub fn set_domain_event_worker_up(&self, worker: DomainEventWorker, up: bool) {
        self.inner
            .domain_event_worker_up
            .with_label_values(&[worker.as_label()])
            .set(i64::from(up));
    }

    pub fn domain_event_worker_is_up(&self, worker: DomainEventWorker) -> bool {
        self.inner
            .domain_event_worker_up
            .with_label_values(&[worker.as_label()])
            .get()
            == 1
    }

    pub fn set_domain_event_chain_snapshot(
        &self,
        actionable_pending: bool,
        quarantine_present: bool,
        oldest_actionable_age_seconds: i64,
        receipt_probe_missing: bool,
        receipt_probe_age_seconds: i64,
    ) {
        self.inner.domain_event_chain_snapshot_up.set(1);
        self.inner
            .domain_outbox_actionable_pending
            .set(i64::from(actionable_pending));
        self.inner
            .domain_outbox_quarantine_present
            .set(i64::from(quarantine_present));
        self.inner
            .domain_outbox_oldest_actionable_age_seconds
            .set(oldest_actionable_age_seconds);
        self.inner
            .domain_event_receipt_probe_missing
            .set(i64::from(receipt_probe_missing));
        self.inner
            .domain_event_receipt_probe_age_seconds
            .set(receipt_probe_age_seconds);
    }

    pub fn mark_domain_event_chain_snapshot_failed(&self) {
        self.inner.domain_event_chain_snapshot_up.set(0);
    }

    pub fn render(&self) -> Result<Vec<u8>, prometheus::Error> {
        let metric_families = self.inner.registry.gather();
        let encoder = TextEncoder::new();
        let mut buffer = Vec::new();
        encoder.encode(&metric_families, &mut buffer)?;
        Ok(buffer)
    }
}

pub async fn metrics_handler(State(state): State<ApiState>) -> impl IntoResponse {
    let content_type = HeaderValue::from_static("text/plain; version=0.0.4; charset=utf-8");
    match state.metrics.render() {
        Ok(body) => (StatusCode::OK, [(header::CONTENT_TYPE, content_type)], body).into_response(),
        Err(error) => {
            tracing::error!(error = %error, "failed to encode metrics");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}

#[derive(Clone)]
pub struct MetricsLayer {
    metrics: Metrics,
}

impl MetricsLayer {
    pub fn new(metrics: Metrics) -> Self {
        Self { metrics }
    }
}

impl<S> Layer<S> for MetricsLayer {
    type Service = MetricsService<S>;

    fn layer(&self, inner: S) -> Self::Service {
        MetricsService {
            inner,
            metrics: self.metrics.clone(),
        }
    }
}

#[derive(Clone)]
pub struct MetricsService<S> {
    inner: S,
    metrics: Metrics,
}

impl<S, B> Service<Request<B>> for MetricsService<S>
where
    S: Service<Request<B>>,
    S::Future: Send + 'static,
    S::Response: IntoResponse,
    B: Send + 'static,
{
    type Response = Response;
    type Error = S::Error;
    type Future = Pin<Box<dyn Future<Output = Result<Self::Response, Self::Error>> + Send>>;

    fn poll_ready(&mut self, cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        self.inner.poll_ready(cx)
    }

    fn call(&mut self, request: Request<B>) -> Self::Future {
        let method = request.method().as_str().to_owned();
        // Never fall back to the raw URI path here. Unmatched paths are attacker-controlled;
        // using them as Prometheus label values creates one time series per unique 404 path
        // and allows unbounded label-cardinality growth. Known routes still use Axum's
        // normalized route template from `MatchedPath`.
        let path = metrics_path_label(&request);
        let metrics = self.metrics.clone();
        let future = self.inner.call(request);

        Box::pin(async move {
            match future.await {
                Ok(response) => {
                    let response: Response = response.into_response();
                    let status = response.status().as_u16().to_string();
                    metrics
                        .http_requests_total()
                        .with_label_values(&[method.as_str(), path.as_str(), status.as_str()])
                        .inc();
                    Ok(response)
                }
                Err(error) => Err(error),
            }
        })
    }
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use super::{
        BuildInfo, DomainEventWorker, Metrics, MetricsLayer, NodeMutationJsonlRecoveryOutcome,
        NodeMutationOutcome, SearchRequestOutcome, UNMATCHED_ROUTE_LABEL,
    };
    use crate::node_mutation::NodeMutationOperation;
    use axum::{body::Body, http::Request, routing::get, Router};
    use tower::ServiceExt;

    fn test_metrics() -> Metrics {
        Metrics::try_new(BuildInfo {
            version: "test",
            commit: "test",
            build_timestamp: "test",
        })
        .expect("metrics")
    }

    #[test]
    fn search_request_outcome_records_exactly_one_observation_per_bounded_label() {
        let metrics = test_metrics();
        let outcomes = [
            (SearchRequestOutcome::Hybrid, "hybrid"),
            (SearchRequestOutcome::LexicalFallback, "lexical_fallback"),
            (
                SearchRequestOutcome::ProviderContractError,
                "provider_contract_error",
            ),
            (SearchRequestOutcome::Unavailable, "unavailable"),
            (SearchRequestOutcome::InvalidRequest, "invalid_request"),
            (SearchRequestOutcome::Internal, "internal"),
        ];
        for (outcome, _) in outcomes {
            metrics.search_request_outcome(outcome);
        }

        let rendered = String::from_utf8(metrics.render().expect("render metrics")).expect("utf8");
        for (_, label) in outcomes {
            assert!(
                rendered.contains(&format!("search_requests_total{{outcome=\"{label}\"}} 1")),
                "expected exactly one recorded observation for outcome {label}"
            );
        }
    }

    #[test]
    fn search_cost_metrics_use_bounded_non_user_controlled_labels_and_record_observations() {
        let metrics = test_metrics();
        metrics.search_request_outcome(SearchRequestOutcome::Hybrid);
        metrics.search_candidate_set_overflow();
        metrics.observe_search_request_duration(Duration::from_millis(25));
        metrics.observe_search_repository_duration(Duration::from_millis(10));
        metrics.observe_search_provider_duration(Duration::from_millis(5));
        metrics.observe_search_candidate_counts(37, 10);

        let rendered = String::from_utf8(metrics.render().expect("render metrics")).expect("utf8");
        assert!(rendered.contains("search_requests_total{outcome=\"hybrid\"} 1"));
        assert!(rendered.contains("search_candidate_set_overflow_total 1"));
        assert!(rendered.contains("search_request_duration_seconds_count 1"));
        assert!(rendered.contains("search_repository_duration_seconds_count 1"));
        assert!(rendered.contains("search_provider_duration_seconds_count 1"));
        assert!(rendered.contains("search_authorized_candidates_sum 37"));
        assert!(rendered.contains("search_lexical_candidates_sum 10"));
        assert!(!rendered.contains("query="));
        assert!(!rendered.contains("node_id="));
    }

    #[test]
    fn node_mutation_metrics_use_only_fixed_privacy_safe_labels() {
        let metrics = test_metrics();
        metrics.observe_node_mutation(
            NodeMutationOperation::Replace,
            NodeMutationOutcome::Conflict,
            Duration::from_millis(12),
            true,
        );
        metrics.observe_node_mutation(
            NodeMutationOperation::Delete,
            NodeMutationOutcome::RateLimited,
            Duration::from_millis(3),
            false,
        );
        metrics.node_mutation_jsonl_recovery(NodeMutationJsonlRecoveryOutcome::Committed, 2);
        metrics.node_mutation_jsonl_recovery(NodeMutationJsonlRecoveryOutcome::Aborted, 1);

        let rendered = String::from_utf8(metrics.render().expect("render metrics")).expect("utf8");
        assert!(
            rendered.contains(r#"node_mutations_total{operation="replace",outcome="conflict"} 1"#)
        );
        assert!(rendered
            .contains(r#"node_mutations_total{operation="delete",outcome="rate_limited"} 1"#));
        assert!(rendered.contains(r#"node_mutation_conflicts_total{operation="replace"} 1"#));
        assert!(rendered.contains(r#"node_mutation_admin_bypass_total{operation="replace"} 1"#));
        assert!(rendered.contains(r#"node_mutation_duration_seconds_count{operation="replace"} 1"#));
        assert!(rendered.contains(r#"node_mutation_jsonl_recovery_total{outcome="committed"} 2"#));
        assert!(rendered.contains(r#"node_mutation_jsonl_recovery_total{outcome="aborted"} 1"#));
        for forbidden in ["node_id=", "account_id=", "title=", "content="] {
            assert!(!rendered.contains(forbidden));
        }
    }

    #[test]
    fn domain_projection_metrics_are_bounded_and_identifier_free() {
        let metrics = test_metrics();
        metrics.domain_projection_refresh_check();
        metrics.domain_projection_refresh_failed();
        metrics.domain_projection_refresh_deferred();
        metrics.observe_domain_projection_write_gate_wait(Duration::from_micros(200));
        metrics.observe_domain_projection_write_gate_hold(Duration::from_millis(20));
        metrics.observe_domain_projection_read_gate_wait(Duration::from_micros(300));
        metrics.observe_domain_projection_reload_success(
            Duration::from_millis(15),
            2,
            1_000,
            5_000,
            7,
        );
        metrics.observe_domain_projection_reload_failure(Duration::from_millis(25));
        metrics.set_domain_projection_version(8);
        let rendered = String::from_utf8(metrics.render().expect("render metrics")).expect("utf8");
        for expected in [
            r#"domain_projection_events_total{event="refresh_check"} 1"#,
            r#"domain_projection_events_total{event="refresh_failure"} 1"#,
            r#"domain_projection_events_total{event="refresh_deferred"} 1"#,
            r#"domain_projection_events_total{event="reload_success"} 1"#,
            r#"domain_projection_events_total{event="reload_failure"} 1"#,
            r#"domain_projection_duration_seconds_count{phase="write_gate_wait"} 1"#,
            r#"domain_projection_duration_seconds_count{phase="write_gate_hold"} 1"#,
            r#"domain_projection_duration_seconds_count{phase="read_gate_wait"} 1"#,
            r#"domain_projection_duration_seconds_count{phase="reload"} 2"#,
            r#"domain_projection_rows_loaded_total{kind="accounts"} 2"#,
            r#"domain_projection_rows_loaded_total{kind="nodes"} 1000"#,
            r#"domain_projection_rows_loaded_total{kind="edges"} 5000"#,
            r#"domain_projection_snapshot{kind="version"} 8"#,
        ] {
            assert!(rendered.contains(expected), "missing metric: {expected}");
        }
        for forbidden in ["account_id=", "node_id=", "edge_id=", "title=", "content="] {
            assert!(!rendered.contains(forbidden));
        }
    }
    #[test]
    fn domain_event_health_metrics_are_bounded_and_identifier_free() {
        let metrics = test_metrics();
        metrics.set_domain_event_worker_up(DomainEventWorker::Relay, true);
        metrics.set_domain_event_worker_up(DomainEventWorker::ReceiptConsumer, false);
        metrics.set_domain_event_chain_snapshot(true, true, 61, true, 62);

        assert!(metrics.domain_event_worker_is_up(DomainEventWorker::Relay));
        assert!(!metrics.domain_event_worker_is_up(DomainEventWorker::ReceiptConsumer));
        let rendered = String::from_utf8(metrics.render().expect("render metrics")).expect("utf8");
        assert!(rendered.contains(r#"domain_event_worker_up{worker="relay"} 1"#));
        assert!(rendered.contains(r#"domain_event_worker_up{worker="receipt_consumer"} 0"#));
        assert!(rendered.contains("domain_event_chain_snapshot_up 1"));
        assert!(rendered.contains("domain_outbox_actionable_pending 1"));
        assert!(rendered.contains("domain_outbox_quarantine_present 1"));
        assert!(rendered.contains("domain_outbox_oldest_actionable_age_seconds 61"));
        assert!(rendered.contains("domain_event_receipt_probe_missing 1"));
        assert!(rendered.contains("domain_event_receipt_probe_age_seconds 62"));
        metrics.mark_domain_event_chain_snapshot_failed();
        let failed = String::from_utf8(metrics.render().expect("render metrics")).expect("utf8");
        assert!(failed.contains("domain_event_chain_snapshot_up 0"));
        for forbidden in ["event_id=", "aggregate_id=", "last_error="] {
            assert!(!rendered.contains(forbidden));
        }
    }

    #[test]
    fn search_authorized_candidates_histogram_keeps_legacy_1001_bucket_for_compatibility() {
        let metrics = test_metrics();
        metrics.observe_search_candidate_counts(1000, 10);

        let rendered = String::from_utf8(metrics.render().expect("render metrics")).expect("utf8");
        for le in ["1000", "1001", "+Inf"] {
            assert!(
                rendered.contains(&format!(
                    "search_authorized_candidates_bucket{{le=\"{le}\"}} 1"
                )),
                "expected the successful hard-bound observation in bucket le={le}"
            );
        }
        assert!(
            rendered.contains("search_candidate_set_overflow_total 0"),
            "the legacy 1001 bucket is not an overflow signal"
        );
    }

    #[tokio::test]
    async fn known_dynamic_routes_keep_one_normalized_matched_path_label() {
        let metrics = test_metrics();
        let app = Router::new()
            .route("/known/{id}", get(|| async { "ok" }))
            .layer(MetricsLayer::new(metrics.clone()));

        for uri in ["/known/1", "/known/2"] {
            let response = app
                .clone()
                .oneshot(
                    Request::builder()
                        .uri(uri)
                        .body(Body::empty())
                        .expect("request"),
                )
                .await
                .expect("response");
            assert!(response.status().is_success());
        }

        let rendered = String::from_utf8(metrics.render().expect("render metrics")).expect("utf8");
        assert!(rendered.contains("path=\"/known/{id}\""));
        assert!(!rendered.contains("path=\"/known/1\""));
        assert!(!rendered.contains("path=\"/known/2\""));
    }

    #[tokio::test]
    async fn real_unmatched_requests_collapse_to_one_metrics_label() {
        let metrics = test_metrics();
        let app = Router::new()
            .route("/known/{id}", get(|| async { "ok" }))
            .layer(MetricsLayer::new(metrics.clone()));

        for uri in ["/attacker-controlled/a", "/attacker-controlled/b"] {
            let response = app
                .clone()
                .oneshot(
                    Request::builder()
                        .uri(uri)
                        .body(Body::empty())
                        .expect("request"),
                )
                .await
                .expect("response");
            assert_eq!(response.status(), axum::http::StatusCode::NOT_FOUND);
        }

        let rendered = String::from_utf8(metrics.render().expect("render metrics")).expect("utf8");
        assert!(rendered.contains(&format!("path=\"{UNMATCHED_ROUTE_LABEL}\"")));
        assert!(!rendered.contains("path=\"/attacker-controlled/a\""));
        assert!(!rendered.contains("path=\"/attacker-controlled/b\""));
    }
}
