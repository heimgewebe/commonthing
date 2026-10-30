//! T005's internal, PostgreSQL-backed search projection worker.
//!
//! It deliberately has no HTTP surface.  Domain rows remain canonical; this
//! module only claims leased work and writes a regenerable projection.

use std::{sync::Arc, time::Duration};

use anyhow::Context;
use async_trait::async_trait;
use serde_json::Value;
use sha2::{Digest, Sha256};
use sqlx::{PgPool, Row};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpStream,
    time::timeout,
};
use unicode_normalization::UnicodeNormalization;

use crate::telemetry::Metrics;

pub const DOCUMENT_REVISION: &str = "node-document-v4-canonical-visibility";
pub const NORMALIZATION_REVISION: &str = "weltgewebe-search-normalization-v1";
pub const RANKING_REVISION: &str = "weltgewebe-hybrid-ranking-v2";
// Five minutes exceeds the provider boundary's bounded worst-case request budget
// while keeping abandoned claims recoverable without a heartbeat.
const CLAIM_SECONDS: i32 = 300;
const MAX_PROVIDER_ATTEMPTS: i32 = 8;
const OLLAMA_RESPONSE_LIMIT: usize = 1_048_576;
const OLLAMA_TIMEOUT: Duration = Duration::from_secs(10);

#[derive(Clone, Debug)]
pub struct GenerationSpec<'a> {
    pub generation_id: &'a str,
    pub provider: &'a str,
    pub model_id: &'a str,
    pub model_revision: &'a str,
    pub runtime_identity: &'a str,
    pub dimension: i32,
}

impl GenerationSpec<'_> {
    pub fn derived_id(&self) -> String {
        // Keep the production Rust identity byte-compatible with the verified
        // T004 Python reference contract: JCS over the same nested field names.
        let payload = serde_json::json!({
            "model": {
                "provider": self.provider,
                "model_id": self.model_id,
                "model_revision": self.model_revision,
                "runtime_id": self.runtime_identity,
                "dimensions": self.dimension,
            },
            "document_revision": DOCUMENT_REVISION,
            "normalization_revision": NORMALIZATION_REVISION,
            "ranking_revision": RANKING_REVISION,
        });
        let canonical = serde_jcs::to_vec(&payload)
            .expect("static search generation identity payload must serialize");
        format!("search-gen-{}", hex::encode(Sha256::digest(canonical)))
    }

    pub fn validate(&self) -> anyhow::Result<()> {
        if self.provider != "local:ollama" {
            anyhow::bail!("unsupported search embedding provider");
        }
        if self.dimension <= 0 || self.dimension > 8192 {
            anyhow::bail!("search generation identity is invalid");
        }
        if self.generation_id != self.derived_id() {
            anyhow::bail!("search generation id does not match its derived identity");
        }
        Ok(())
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ProcessOutcome {
    Processed,
    Stale,
    PendingProvider,
    Failed,
    Deleted,
    LeaseLost,
    Empty,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProjectionStatusSnapshot {
    /// Unique rows currently present in `domain_nodes`; revisions and projection-job history do not inflate this count.
    pub current_nodes: i64,
    /// All projection jobs across generations and revisions.
    pub total_jobs: i64,
    /// Jobs in terminal states (`done`, `stale`, or `failed`).
    pub terminal_jobs: i64,
    pub pending_jobs: i64,
    pub claimed_jobs: i64,
    pub retry_jobs: i64,
    pub done_jobs: i64,
    pub stale_jobs: i64,
    pub failed_jobs: i64,
    /// Generation currently serving semantic search.
    pub active_generation_id: Option<String>,
    pub active_generation_identity: Option<String>,
    pub active_generation_activated_at: Option<chrono::DateTime<chrono::Utc>>,
    /// Newest non-active generation still being built or retained as activation candidate.
    pub rebuild_generation_id: Option<String>,
    pub rebuild_generation_identity: Option<String>,
    pub rebuild_generation_state: Option<String>,
    pub rebuild_total_jobs: Option<i64>,
    pub rebuild_terminal_jobs: Option<i64>,
    /// Separate integrity statement mirroring the database activation gate.
    pub rebuild_activation_ready: Option<bool>,
}

#[derive(Debug, thiserror::Error)]
pub enum EmbeddingProviderError {
    #[error("embedding provider unavailable")]
    Unavailable,
    #[error("embedding provider identity mismatch")]
    IdentityMismatch,
    #[error("embedding provider returned malformed data")]
    MalformedResponse,
    #[error("embedding provider returned wrong dimension")]
    WrongDimension,
    #[error("embedding provider returned invalid vector")]
    InvalidVector,
    #[error("embedding provider is unsupported")]
    Unsupported,
    #[error("embedding provider failed")]
    Failed,
}

impl EmbeddingProviderError {
    pub fn code(&self) -> &'static str {
        match self {
            Self::Unavailable => "provider_unavailable",
            Self::IdentityMismatch => "provider_identity_mismatch",
            Self::MalformedResponse => "provider_malformed_response",
            Self::WrongDimension => "provider_wrong_dimension",
            Self::InvalidVector => "provider_invalid_vector",
            Self::Unsupported => "provider_unsupported",
            Self::Failed => "provider_failed",
        }
    }
}

/// Private boundary for T005's worker. Implementations must not log the
/// document or vector; the worker records only bounded outcome labels.
#[async_trait]
pub trait EmbeddingProvider: Send + Sync {
    async fn embed(
        &self,
        document: &str,
        dimension: usize,
    ) -> Result<Vec<f64>, EmbeddingProviderError>;
}

/// T004-compatible local Ollama boundary.  It accepts only a literal loopback
/// HTTP origin and checks the model/runtime identity on both sides of embedding.
pub struct OllamaEmbeddingProvider {
    base_url: url::Url,
    model_id: String,
    model_revision: String,
    runtime_identity: String,
}

fn normalize_sha256_digest(value: &str) -> Option<&str> {
    let digest = value.strip_prefix("sha256:").unwrap_or(value);
    (digest.len() == 64
        && digest
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)))
    .then_some(digest)
}

fn sha256_digests_match(observed: &str, expected: &str) -> bool {
    matches!(
        (
            normalize_sha256_digest(observed),
            normalize_sha256_digest(expected),
        ),
        (Some(observed), Some(expected)) if observed == expected
    )
}

impl OllamaEmbeddingProvider {
    pub fn new(
        base_url: &str,
        model_id: String,
        model_revision: String,
        runtime_identity: String,
    ) -> Result<Self, EmbeddingProviderError> {
        let base_url =
            url::Url::parse(base_url).map_err(|_| EmbeddingProviderError::Unsupported)?;
        let loopback = matches!(base_url.host_str(), Some("127.0.0.1") | Some("::1"));
        if base_url.scheme() != "http"
            || !loopback
            || base_url.username() != ""
            || base_url.password().is_some()
            || base_url.query().is_some()
            || base_url.fragment().is_some()
            || base_url.path() != "/"
        {
            return Err(EmbeddingProviderError::Unsupported);
        }
        Ok(Self {
            base_url,
            model_id,
            model_revision,
            runtime_identity,
        })
    }

    async fn object(
        &self,
        path: &str,
        body: Option<Value>,
    ) -> Result<Value, EmbeddingProviderError> {
        let host = self
            .base_url
            .host_str()
            .ok_or(EmbeddingProviderError::Unsupported)?;
        let port = self
            .base_url
            .port_or_known_default()
            .ok_or(EmbeddingProviderError::Unsupported)?;
        let payload = body
            .map(|body| serde_json::to_vec(&body).map_err(|_| EmbeddingProviderError::Failed))
            .transpose()?;
        let host_header = http_host_header(host, port);
        let request = match &payload {
            Some(payload) => format!("POST /{path} HTTP/1.1\r\nHost: {host_header}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", payload.len()).into_bytes(),
            None => format!("GET /{path} HTTP/1.1\r\nHost: {host_header}\r\nConnection: close\r\n\r\n").into_bytes(),
        };
        let mut stream = timeout(OLLAMA_TIMEOUT, TcpStream::connect((host, port)))
            .await
            .map_err(|_| EmbeddingProviderError::Unavailable)?
            .map_err(|_| EmbeddingProviderError::Unavailable)?;
        timeout(OLLAMA_TIMEOUT, async {
            stream.write_all(&request).await?;
            if let Some(payload) = &payload {
                stream.write_all(payload).await?;
            }
            stream.flush().await
        })
        .await
        .map_err(|_| EmbeddingProviderError::Unavailable)?
        .map_err(|_| EmbeddingProviderError::Unavailable)?;
        let mut bytes = Vec::new();
        timeout(OLLAMA_TIMEOUT, async {
            let mut part = [0_u8; 8192];
            loop {
                let read = stream
                    .read(&mut part)
                    .await
                    .map_err(|_| EmbeddingProviderError::Unavailable)?;
                if read == 0 {
                    match http_response_complete(&bytes)? {
                        true => break,
                        false if http_response_is_close_delimited(&bytes)? => break,
                        false => return Err(EmbeddingProviderError::Unavailable),
                    }
                }
                if bytes.len() + read > OLLAMA_RESPONSE_LIMIT + 16_384 {
                    return Err(EmbeddingProviderError::MalformedResponse);
                }
                bytes.extend_from_slice(&part[..read]);
                if http_response_complete(&bytes)? {
                    break;
                }
            }
            Ok::<(), EmbeddingProviderError>(())
        })
        .await
        .map_err(|_| EmbeddingProviderError::Unavailable)??;
        let split = bytes
            .windows(4)
            .position(|window| window == b"\r\n\r\n")
            .ok_or(EmbeddingProviderError::MalformedResponse)?;
        let header = std::str::from_utf8(&bytes[..split])
            .map_err(|_| EmbeddingProviderError::MalformedResponse)?;
        let status = header
            .split_whitespace()
            .nth(1)
            .ok_or(EmbeddingProviderError::MalformedResponse)?
            .parse::<u16>()
            .map_err(|_| EmbeddingProviderError::MalformedResponse)?;
        if status == 408 || status == 429 || (500..600).contains(&status) {
            return Err(EmbeddingProviderError::Unavailable);
        }
        if !(200..300).contains(&status) {
            return Err(EmbeddingProviderError::Failed);
        }
        let body = &bytes[split + 4..];
        let header_lower = header.to_ascii_lowercase();
        let decoded;
        let body = if header_lower.lines().any(|line| {
            line.strip_prefix("transfer-encoding:")
                .is_some_and(|value| value.split(',').any(|coding| coding.trim() == "chunked"))
        }) {
            decoded = decode_chunked_body(body)?;
            decoded.as_slice()
        } else {
            body
        };
        if body.len() > OLLAMA_RESPONSE_LIMIT {
            return Err(EmbeddingProviderError::MalformedResponse);
        }
        serde_json::from_slice(body).map_err(|_| EmbeddingProviderError::MalformedResponse)
    }

    async fn verify_identity(&self) -> Result<(), EmbeddingProviderError> {
        let tags = self.object("api/tags", None).await?;
        let digest = tags
            .get("models")
            .and_then(Value::as_array)
            .and_then(|models| {
                models
                    .iter()
                    .find(|m| m.get("name").and_then(Value::as_str) == Some(self.model_id.as_str()))
            })
            .and_then(|model| model.get("digest"))
            .and_then(Value::as_str)
            .ok_or(EmbeddingProviderError::IdentityMismatch)?;
        if !sha256_digests_match(digest, &self.model_revision) {
            return Err(EmbeddingProviderError::IdentityMismatch);
        }
        let version_response = self.object("api/version", None).await?;
        let version = version_response
            .get("version")
            .and_then(Value::as_str)
            .filter(|v| !v.is_empty())
            .ok_or(EmbeddingProviderError::IdentityMismatch)?;
        let observed = format!(
            "ollama:{version}@{}",
            self.base_url.as_str().trim_end_matches('/')
        );
        if observed != self.runtime_identity {
            return Err(EmbeddingProviderError::IdentityMismatch);
        }
        Ok(())
    }
}

#[async_trait]
impl EmbeddingProvider for OllamaEmbeddingProvider {
    async fn embed(
        &self,
        document: &str,
        dimension: usize,
    ) -> Result<Vec<f64>, EmbeddingProviderError> {
        self.verify_identity().await?;
        let response = self
            .object(
                "api/embed",
                Some(serde_json::json!({"model": self.model_id, "input": document})),
            )
            .await?;
        let vectors = response
            .get("embeddings")
            .and_then(Value::as_array)
            .filter(|v| v.len() == 1)
            .ok_or(EmbeddingProviderError::MalformedResponse)?;
        let vector = vectors[0]
            .as_array()
            .ok_or(EmbeddingProviderError::MalformedResponse)?;
        if vector.len() != dimension {
            return Err(EmbeddingProviderError::WrongDimension);
        }
        let vector = vector
            .iter()
            .map(|v| v.as_f64().ok_or(EmbeddingProviderError::InvalidVector))
            .collect::<Result<Vec<_>, _>>()?;
        let norm = vector.iter().map(|v| v * v).sum::<f64>();
        if !norm.is_finite() || norm <= 0.0 || vector.iter().any(|v| !v.is_finite()) {
            return Err(EmbeddingProviderError::InvalidVector);
        }
        self.verify_identity().await?;
        Ok(vector)
    }
}

fn http_host_header(host: &str, port: u16) -> String {
    if host.contains(':') {
        format!("[{host}]:{port}")
    } else {
        format!("{host}:{port}")
    }
}

fn http_framing(header_lower: &str) -> Result<(bool, Option<usize>), EmbeddingProviderError> {
    let chunked = header_lower.lines().any(|line| {
        line.strip_prefix("transfer-encoding:")
            .is_some_and(|value| value.split(',').any(|coding| coding.trim() == "chunked"))
    });
    let mut content_length = None;
    for line in header_lower.lines() {
        let Some(value) = line.strip_prefix("content-length:") else {
            continue;
        };
        let parsed = value
            .trim()
            .parse::<usize>()
            .map_err(|_| EmbeddingProviderError::MalformedResponse)?;
        if parsed > OLLAMA_RESPONSE_LIMIT {
            return Err(EmbeddingProviderError::MalformedResponse);
        }
        if content_length.is_some_and(|previous| previous != parsed) {
            return Err(EmbeddingProviderError::MalformedResponse);
        }
        content_length = Some(parsed);
    }
    if chunked && content_length.is_some() {
        return Err(EmbeddingProviderError::MalformedResponse);
    }
    Ok((chunked, content_length))
}

fn http_response_is_close_delimited(bytes: &[u8]) -> Result<bool, EmbeddingProviderError> {
    let Some(split) = bytes.windows(4).position(|window| window == b"\r\n\r\n") else {
        return Ok(false);
    };
    let header = std::str::from_utf8(&bytes[..split])
        .map_err(|_| EmbeddingProviderError::MalformedResponse)?
        .to_ascii_lowercase();
    let (chunked, content_length) = http_framing(&header)?;
    Ok(!chunked && content_length.is_none())
}

fn http_response_complete(bytes: &[u8]) -> Result<bool, EmbeddingProviderError> {
    let Some(split) = bytes.windows(4).position(|window| window == b"\r\n\r\n") else {
        return Ok(false);
    };
    let header = std::str::from_utf8(&bytes[..split])
        .map_err(|_| EmbeddingProviderError::MalformedResponse)?;
    let header_lower = header.to_ascii_lowercase();
    let body = &bytes[split + 4..];
    let (chunked, content_length) = http_framing(&header_lower)?;
    if chunked {
        return chunked_body_complete(body);
    }
    if let Some(length) = content_length {
        return Ok(body.len() >= length);
    }
    Ok(false)
}

fn chunked_body_complete(mut input: &[u8]) -> Result<bool, EmbeddingProviderError> {
    loop {
        let Some(line_end) = input.windows(2).position(|window| window == b"\r\n") else {
            return Ok(false);
        };
        let size_line = std::str::from_utf8(&input[..line_end])
            .map_err(|_| EmbeddingProviderError::MalformedResponse)?;
        let size_token = size_line.split(';').next().unwrap_or_default().trim();
        let size = usize::from_str_radix(size_token, 16)
            .map_err(|_| EmbeddingProviderError::MalformedResponse)?;
        input = &input[line_end + 2..];
        if size == 0 {
            return Ok(
                input.starts_with(b"\r\n") || input.windows(4).any(|window| window == b"\r\n\r\n")
            );
        }
        if size > OLLAMA_RESPONSE_LIMIT {
            return Err(EmbeddingProviderError::MalformedResponse);
        }
        if input.len() < size + 2 {
            return Ok(false);
        }
        if &input[size..size + 2] != b"\r\n" {
            return Err(EmbeddingProviderError::MalformedResponse);
        }
        input = &input[size + 2..];
    }
}

fn decode_chunked_body(mut input: &[u8]) -> Result<Vec<u8>, EmbeddingProviderError> {
    let mut output = Vec::new();
    loop {
        let line_end = input
            .windows(2)
            .position(|window| window == b"\r\n")
            .ok_or(EmbeddingProviderError::MalformedResponse)?;
        let size_line = std::str::from_utf8(&input[..line_end])
            .map_err(|_| EmbeddingProviderError::MalformedResponse)?;
        let size_token = size_line.split(';').next().unwrap_or_default().trim();
        let size = usize::from_str_radix(size_token, 16)
            .map_err(|_| EmbeddingProviderError::MalformedResponse)?;
        input = &input[line_end + 2..];
        if size == 0 {
            return Ok(output);
        }
        if size > OLLAMA_RESPONSE_LIMIT.saturating_sub(output.len())
            || input.len() < size + 2
            || &input[size..size + 2] != b"\r\n"
        {
            return Err(EmbeddingProviderError::MalformedResponse);
        }
        output.extend_from_slice(&input[..size]);
        input = &input[size + 2..];
    }
}

pub struct UnavailableEmbeddingProvider;

#[async_trait]
impl EmbeddingProvider for UnavailableEmbeddingProvider {
    async fn embed(
        &self,
        _document: &str,
        _dimension: usize,
    ) -> Result<Vec<f64>, EmbeddingProviderError> {
        Err(EmbeddingProviderError::Unavailable)
    }
}

#[derive(Clone)]
pub struct ProjectionWorker {
    pool: PgPool,
    worker_id: String,
    metrics: Metrics,
    provider: Arc<dyn EmbeddingProvider>,
}

impl ProjectionWorker {
    pub fn new(pool: PgPool, worker_id: String, metrics: Metrics) -> anyhow::Result<Self> {
        Self::new_with_provider(
            pool,
            worker_id,
            metrics,
            Arc::new(UnavailableEmbeddingProvider),
        )
    }

    pub fn new_with_provider(
        pool: PgPool,
        worker_id: String,
        metrics: Metrics,
        provider: Arc<dyn EmbeddingProvider>,
    ) -> anyhow::Result<Self> {
        if worker_id.trim().is_empty() {
            anyhow::bail!("search worker id must not be empty");
        }
        Ok(Self {
            pool,
            worker_id,
            metrics,
            provider,
        })
    }

    /// Starts a bounded rebuild generation.  No activation occurs here.
    pub async fn start_generation(&self, spec: GenerationSpec<'_>) -> anyhow::Result<i64> {
        spec.validate()?;
        let mut tx = self.pool.begin().await?;
        // Match the trigger's shared transaction lock. This makes the initial
        // generation snapshot linearizable with relevant domain mutations: a
        // mutation is ordered wholly before this seed or, after commit, sees
        // the new generation and enqueues its own current revision.
        sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended('weltgewebe.search.generation.activation', 0))")
            .execute(&mut *tx)
            .await?;
        // A migration starts tracking future mutations.  This idempotent seed
        // makes legacy rows resumable without treating a missing revision as a
        // public/visible value.
        sqlx::query(
            "INSERT INTO search_node_versions (node_id,source_version,source_revision) \
            SELECT id,1,'node-1' FROM domain_nodes ON CONFLICT (node_id) DO NOTHING",
        )
        .execute(&mut *tx)
        .await?;
        let identity = sqlx::query("INSERT INTO search_index_generations (generation_id, provider, model_id, model_revision, runtime_identity, dimension, document_revision, normalization_revision, ranking_revision, state, expected_nodes) \
                     VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'building',(SELECT count(*) FROM domain_nodes)) \
                     ON CONFLICT (generation_id) DO UPDATE SET generation_id=EXCLUDED.generation_id \
                     WHERE search_index_generations.provider=EXCLUDED.provider \
                       AND search_index_generations.model_id=EXCLUDED.model_id \
                       AND search_index_generations.model_revision=EXCLUDED.model_revision \
                       AND search_index_generations.runtime_identity=EXCLUDED.runtime_identity \
                       AND search_index_generations.dimension=EXCLUDED.dimension \
                       AND search_index_generations.document_revision=EXCLUDED.document_revision \
                       AND search_index_generations.normalization_revision=EXCLUDED.normalization_revision \
                       AND search_index_generations.ranking_revision=EXCLUDED.ranking_revision \
                     RETURNING generation_id")
            .bind(spec.generation_id).bind(spec.provider).bind(spec.model_id).bind(spec.model_revision).bind(spec.runtime_identity)
            .bind(spec.dimension).bind(DOCUMENT_REVISION).bind(NORMALIZATION_REVISION).bind(RANKING_REVISION)
            .fetch_optional(&mut *tx).await?;
        if identity.is_none() {
            anyhow::bail!("search generation identity mismatch");
        }
        // A previous bounded outage may have exhausted retries, or a corrected
        // provider identity check may make an exact pinned generation valid.
        // Re-running that same generation is the explicit catch-up action;
        // unrelated permanent contract failures stay failed.
        sqlx::query("UPDATE search_projection_jobs SET state='pending', attempt_count=0, available_at=NOW(), claimed_by=NULL, claim_until=NULL, completed_at=NULL, last_error_code=NULL WHERE generation_id=$1 AND state='failed' AND last_error_code IN ('provider_unavailable_exhausted','provider_identity_mismatch')")
            .bind(spec.generation_id).execute(&mut *tx).await?;
        let inserted = sqlx::query("INSERT INTO search_projection_jobs (generation_id,node_id,source_version,source_revision,operation) \
            SELECT $1, v.node_id,v.source_version,v.source_revision,'upsert' FROM search_node_versions v JOIN domain_nodes n ON n.id=v.node_id \
            ON CONFLICT DO NOTHING")
            .bind(spec.generation_id).execute(&mut *tx).await?.rows_affected() as i64;
        tx.commit().await?;
        // Defense-in-depth reconciliation after releasing the exclusive
        // generation lock. The lock establishes the race-free ordering; this
        // idempotent pass repairs legacy or externally seeded version rows.
        let reconciled = sqlx::query("INSERT INTO search_projection_jobs (generation_id,node_id,source_version,source_revision,operation) \
            SELECT $1, v.node_id,v.source_version,v.source_revision,'upsert' FROM search_node_versions v JOIN domain_nodes n ON n.id=v.node_id \
            ON CONFLICT DO NOTHING")
            .bind(spec.generation_id)
            .execute(&self.pool)
            .await?
            .rows_affected() as i64;
        Ok(inserted + reconciled)
    }

    pub async fn claim_and_process_one(&self) -> anyhow::Result<ProcessOutcome> {
        let job = sqlx::query("WITH picked AS (SELECT id FROM search_projection_jobs \
            WHERE (state IN ('pending','retry') AND available_at <= NOW()) OR (state='claimed' AND claim_until <= NOW()) \
            ORDER BY available_at,id FOR UPDATE SKIP LOCKED LIMIT 1) \
            UPDATE search_projection_jobs j SET state='claimed', claimed_by=$1, claim_until=NOW()+make_interval(secs=>$2), attempt_count=j.attempt_count+1 \
            FROM picked WHERE j.id=picked.id RETURNING j.id,j.generation_id,j.node_id,j.source_version,j.source_revision,j.operation,j.attempt_count")
            .bind(&self.worker_id).bind(CLAIM_SECONDS).fetch_optional(&self.pool).await?;
        let Some(job) = job else {
            return Ok(ProcessOutcome::Empty);
        };
        let id: i64 = job.get("id");
        let generation: String = job.get("generation_id");
        let node_id: String = job.get("node_id");
        let version: i64 = job.get("source_version");
        let revision: String = job.get("source_revision");
        let operation: String = job.get("operation");
        let attempts: i32 = job.get("attempt_count");
        let result = if operation == "delete" {
            self.delete_if_current(id, attempts, &generation, &node_id, version, &revision)
                .await?
        } else {
            self.upsert_if_current(id, attempts, &generation, &node_id, version, &revision)
                .await?
        };
        let outcome = match result {
            Ok(outcome) => outcome,
            Err(code) => {
                if !self.finish(id, attempts, "failed", Some(code)).await? {
                    self.metrics.search_projection_outcome("lease_lost");
                    return Ok(ProcessOutcome::LeaseLost);
                }
                self.metrics.search_projection_outcome("failed");
                return Ok(ProcessOutcome::Failed);
            }
        };
        let final_outcome = match outcome {
            ProcessOutcome::LeaseLost => ProcessOutcome::LeaseLost,
            ProcessOutcome::PendingProvider if attempts >= MAX_PROVIDER_ATTEMPTS => {
                if self
                    .finish(
                        id,
                        attempts,
                        "failed",
                        Some("provider_unavailable_exhausted"),
                    )
                    .await?
                {
                    ProcessOutcome::Failed
                } else {
                    ProcessOutcome::LeaseLost
                }
            }
            ProcessOutcome::PendingProvider => {
                if self.retry(id, attempts, "provider_unavailable").await? {
                    ProcessOutcome::PendingProvider
                } else {
                    ProcessOutcome::LeaseLost
                }
            }
            ProcessOutcome::Stale => {
                if self
                    .finish(id, attempts, "stale", Some("stale_revision"))
                    .await?
                {
                    ProcessOutcome::Stale
                } else {
                    ProcessOutcome::LeaseLost
                }
            }
            other => {
                if self.finish(id, attempts, "done", None).await? {
                    other
                } else {
                    ProcessOutcome::LeaseLost
                }
            }
        };
        self.metrics.search_projection_outcome(match final_outcome {
            ProcessOutcome::Processed => "processed",
            ProcessOutcome::Stale => "stale",
            ProcessOutcome::PendingProvider => "pending",
            ProcessOutcome::Deleted => "deleted",
            ProcessOutcome::Failed => "failed",
            ProcessOutcome::LeaseLost => "lease_lost",
            ProcessOutcome::Empty => "empty",
        });
        Ok(final_outcome)
    }

    /// Read-only operational view: no document text, tags, or vectors escape.
    /// All fields come from one PostgreSQL statement, so operators never see a
    /// synthetic snapshot assembled from different committed moments.
    pub async fn status_snapshot(&self) -> anyhow::Result<ProjectionStatusSnapshot> {
        let row = sqlx::query(
            "WITH job_counts AS ( \
                SELECT \
                    (SELECT count(*) FROM domain_nodes) AS current_nodes, \
                    count(*) AS total_jobs, \
                    count(*) FILTER (WHERE state IN ('done','stale','failed')) AS terminal_jobs, \
                    count(*) FILTER (WHERE state='pending') AS pending, \
                    count(*) FILTER (WHERE state='claimed') AS claimed, \
                    count(*) FILTER (WHERE state='retry') AS retry, \
                    count(*) FILTER (WHERE state='done') AS done, \
                    count(*) FILTER (WHERE state='stale') AS stale, \
                    count(*) FILTER (WHERE state='failed') AS failed \
                FROM search_projection_jobs \
             ), active AS ( \
                SELECT generation_id,provider,model_id,model_revision,runtime_identity,dimension,document_revision,normalization_revision,ranking_revision,activated_at \
                FROM search_index_generations WHERE state='active' \
             ), rebuild AS ( \
                SELECT g.generation_id,g.provider,g.model_id,g.model_revision,g.runtime_identity,g.dimension,g.document_revision,g.normalization_revision,g.ranking_revision,g.state, \
                    (SELECT count(*) FROM search_projection_jobs j WHERE j.generation_id=g.generation_id) AS total_jobs, \
                    (SELECT count(*) FROM search_projection_jobs j WHERE j.generation_id=g.generation_id AND j.state IN ('done','stale','failed')) AS terminal_jobs, \
                    weltgewebe_search_generation_activation_ready(g.generation_id) AS activation_ready \
                FROM search_index_generations g \
                WHERE g.state IN ('building','ready') \
                ORDER BY CASE WHEN g.state='ready' THEN 0 ELSE 1 END, g.created_at DESC, g.generation_id DESC \
                LIMIT 1 \
             ) \
             SELECT jc.*, \
                a.generation_id AS active_generation_id, \
                CASE WHEN a.generation_id IS NULL THEN NULL ELSE concat_ws(':',a.provider,a.model_id,a.model_revision,a.runtime_identity,a.dimension::text,a.document_revision,a.normalization_revision,a.ranking_revision) END AS active_generation_identity, \
                a.activated_at AS active_generation_activated_at, \
                r.generation_id AS rebuild_generation_id, \
                CASE WHEN r.generation_id IS NULL THEN NULL ELSE concat_ws(':',r.provider,r.model_id,r.model_revision,r.runtime_identity,r.dimension::text,r.document_revision,r.normalization_revision,r.ranking_revision) END AS rebuild_generation_identity, \
                r.state AS rebuild_generation_state, \
                r.total_jobs AS rebuild_total_jobs, \
                r.terminal_jobs AS rebuild_terminal_jobs, \
                r.activation_ready AS rebuild_activation_ready \
             FROM job_counts jc \
             LEFT JOIN active a ON TRUE \
             LEFT JOIN rebuild r ON TRUE",
        )
        .fetch_one(&self.pool)
        .await?;
        Ok(ProjectionStatusSnapshot {
            current_nodes: row.get::<i64, _>("current_nodes"),
            total_jobs: row.get::<i64, _>("total_jobs"),
            terminal_jobs: row.get::<i64, _>("terminal_jobs"),
            pending_jobs: row.get::<i64, _>("pending"),
            claimed_jobs: row.get::<i64, _>("claimed"),
            retry_jobs: row.get::<i64, _>("retry"),
            done_jobs: row.get::<i64, _>("done"),
            stale_jobs: row.get::<i64, _>("stale"),
            failed_jobs: row.get::<i64, _>("failed"),
            active_generation_id: row.get("active_generation_id"),
            active_generation_identity: row.get("active_generation_identity"),
            active_generation_activated_at: row.get("active_generation_activated_at"),
            rebuild_generation_id: row.get("rebuild_generation_id"),
            rebuild_generation_identity: row.get("rebuild_generation_identity"),
            rebuild_generation_state: row.get("rebuild_generation_state"),
            rebuild_total_jobs: row.get("rebuild_total_jobs"),
            rebuild_terminal_jobs: row.get("rebuild_terminal_jobs"),
            rebuild_activation_ready: row.get("rebuild_activation_ready"),
        })
    }

    /// Deterministic digest over projection identity and metadata, deliberately
    /// excluding `searchable_text`, tags, raw embeddings, timestamps, and the
    /// generation id itself. Empty rebuilds still bind the full identity.
    pub async fn integrity_digest(&self, generation_id: &str) -> anyhow::Result<String> {
        let generation = sqlx::query("SELECT provider,model_id,model_revision,runtime_identity,dimension,document_revision,normalization_revision,ranking_revision FROM search_index_generations WHERE generation_id=$1")
            .bind(generation_id)
            .fetch_optional(&self.pool)
            .await?
            .context("unknown search generation")?;
        let rows = sqlx::query("SELECT node_id,source_version,source_revision,content_sha256,language,kind,status,visibility_scopes,semantic_state FROM search_node_projections WHERE generation_id=$1 ORDER BY node_id")
            .bind(generation_id).fetch_all(&self.pool).await?;
        let mut digest = Sha256::new();
        for value in [
            generation.get::<String, _>("provider"),
            generation.get::<String, _>("model_id"),
            generation.get::<String, _>("model_revision"),
            generation.get::<String, _>("runtime_identity"),
            generation.get::<i32, _>("dimension").to_string(),
            generation.get::<String, _>("document_revision"),
            generation.get::<String, _>("normalization_revision"),
            generation.get::<String, _>("ranking_revision"),
        ] {
            digest.update(value.as_bytes());
            digest.update([0]);
        }
        for row in rows {
            for value in [
                row.get::<String, _>("node_id"),
                row.get::<i64, _>("source_version").to_string(),
                row.get::<String, _>("source_revision"),
                row.get::<String, _>("content_sha256"),
                row.get::<String, _>("language"),
                row.get::<String, _>("kind"),
                row.get::<String, _>("status"),
                row.get::<Vec<String>, _>("visibility_scopes")
                    .join("\u{1f}"),
                row.get::<String, _>("semantic_state"),
            ] {
                digest.update(value.as_bytes());
                digest.update([0]);
            }
        }
        Ok(hex::encode(digest.finalize()))
    }

    async fn claim_is_current(&self, id: i64, attempts: i32) -> anyhow::Result<bool> {
        Ok(sqlx::query_scalar(
            "SELECT EXISTS (SELECT 1 FROM search_projection_jobs WHERE id=$1 AND state='claimed' AND claimed_by=$2 AND attempt_count=$3 AND claim_until > NOW())",
        )
        .bind(id)
        .bind(&self.worker_id)
        .bind(attempts)
        .fetch_one(&self.pool)
        .await?)
    }

    async fn delete_if_current(
        &self,
        job_id: i64,
        attempts: i32,
        generation: &str,
        node: &str,
        version: i64,
        revision: &str,
    ) -> anyhow::Result<Result<ProcessOutcome, &'static str>> {
        // The claim row and canonical tombstone are locked and checked inside
        // the mutating statement. attempt_count is the fencing token, so a
        // reclaimed lease cannot be reused even when a worker id is recycled.
        let row = sqlx::query(
            "WITH lease AS (SELECT 1 FROM search_projection_jobs WHERE id=$5 AND generation_id=$4 AND node_id=$1 AND source_version=$2 AND source_revision=$3 AND operation='delete' AND state='claimed' AND claimed_by=$6 AND attempt_count=$7 AND claim_until > NOW() FOR UPDATE), current AS (SELECT 1 FROM search_node_versions WHERE node_id=$1 AND source_version=$2 AND source_revision=$3 AND deleted_at IS NOT NULL FOR UPDATE), deleted AS (DELETE FROM search_node_projections p USING current, lease, search_index_generations g WHERE p.node_id=$1 AND p.generation_id=$4 AND g.generation_id=p.generation_id AND g.state IN ('building','ready','active') RETURNING 1) SELECT EXISTS (SELECT 1 FROM lease) AS lease_current, EXISTS (SELECT 1 FROM current) AS tombstone_current",
        )
        .bind(node)
        .bind(version)
        .bind(revision)
        .bind(generation)
        .bind(job_id)
        .bind(&self.worker_id)
        .bind(attempts)
        .fetch_one(&self.pool)
        .await?;
        let lease_current: bool = row.get("lease_current");
        let tombstone_current: bool = row.get("tombstone_current");
        Ok(Ok(if !lease_current {
            ProcessOutcome::LeaseLost
        } else if tombstone_current {
            ProcessOutcome::Deleted
        } else {
            ProcessOutcome::Stale
        }))
    }

    async fn upsert_if_current(
        &self,
        job_id: i64,
        attempts: i32,
        generation: &str,
        node: &str,
        version: i64,
        revision: &str,
    ) -> anyhow::Result<Result<ProcessOutcome, &'static str>> {
        let row = sqlx::query("SELECT n.kind,n.title,n.payload::text,n.search_visibility,g.dimension,weltgewebe_search_node_owner_account_id(n.payload) AS owner_account_id FROM domain_nodes n JOIN search_node_versions v ON v.node_id=n.id JOIN search_index_generations g ON g.generation_id=$2 JOIN search_projection_jobs j ON j.id=$5 WHERE n.id=$1 AND v.source_version=$3 AND v.source_revision=$4 AND v.deleted_at IS NULL AND j.generation_id=$2 AND j.node_id=$1 AND j.source_version=$3 AND j.source_revision=$4 AND j.operation='upsert' AND j.state='claimed' AND j.claimed_by=$6 AND j.attempt_count=$7 AND j.claim_until > NOW()")
            .bind(node).bind(generation).bind(version).bind(revision).bind(job_id).bind(&self.worker_id).bind(attempts).fetch_optional(&self.pool).await?;
        let Some(row) = row else {
            return Ok(Ok(if self.claim_is_current(job_id, attempts).await? {
                ProcessOutcome::Stale
            } else {
                ProcessOutcome::LeaseLost
            }));
        };
        let search_visibility: String = row.get("search_visibility");
        let owner_account_id: Option<String> = row.get("owner_account_id");
        let dimension: i32 = row.get("dimension");

        let is_sentinel = search_visibility == "hidden"
            || search_visibility == "revoked"
            || (search_visibility == "private" && owner_account_id.is_none());

        if is_sentinel {
            // Canonically hidden, revoked, deleted, unknown, or ownerless private
            // rows are represented only by a content-free sentinel. They never
            // parse content, never cross the embedding-provider boundary and cannot
            // leave recoverable text or vectors in the projection.
            let redacted = "[nicht öffentlich]";
            let content_sha256 = hex::encode(Sha256::digest(
                format!("redacted-search-projection-v2\n{node}\n{version}\n{revision}").as_bytes(),
            ));
            let written = sqlx::query("INSERT INTO search_node_projections (generation_id,node_id,source_version,source_revision,content_sha256,title,tags,searchable_text,language,kind,status,visibility_scopes,semantic_state,embedding) \
                SELECT $1,$2,$3,$4,$5,$6,'{}'::TEXT[],$6,'und',$6,'hidden','{}'::TEXT[],'unavailable',NULL \
                FROM (SELECT v.node_id FROM search_node_versions v JOIN domain_nodes n ON n.id=v.node_id JOIN search_index_generations g ON g.generation_id=$1 JOIN search_projection_jobs j ON j.id=$7 WHERE n.id=$2 AND (n.search_visibility IN ('hidden','revoked') OR (n.search_visibility='private' AND weltgewebe_search_node_owner_account_id(n.payload) IS NULL)) AND v.source_version=$3 AND v.source_revision=$4 AND v.deleted_at IS NULL AND g.state IN ('building','ready','active') AND j.generation_id=$1 AND j.node_id=$2 AND j.source_version=$3 AND j.source_revision=$4 AND j.operation='upsert' AND j.state='claimed' AND j.claimed_by=$8 AND j.attempt_count=$9 AND j.claim_until > NOW() FOR UPDATE OF v,j) current \
                ON CONFLICT (generation_id,node_id) DO UPDATE SET source_version=EXCLUDED.source_version,source_revision=EXCLUDED.source_revision,content_sha256=EXCLUDED.content_sha256,title=EXCLUDED.title,tags=EXCLUDED.tags,searchable_text=EXCLUDED.searchable_text,language=EXCLUDED.language,kind=EXCLUDED.kind,status=EXCLUDED.status,visibility_scopes=EXCLUDED.visibility_scopes,semantic_state=EXCLUDED.semantic_state,embedding=NULL,indexed_at=clock_timestamp() \
                WHERE search_node_projections.source_version <= EXCLUDED.source_version")
                .bind(generation)
                .bind(node)
                .bind(version)
                .bind(revision)
                .bind(content_sha256)
                .bind(redacted)
                .bind(job_id)
                .bind(&self.worker_id)
                .bind(attempts)
                .execute(&self.pool)
                .await?
                .rows_affected();
            return Ok(Ok(if written == 1 {
                ProcessOutcome::Processed
            } else if self.claim_is_current(job_id, attempts).await? {
                ProcessOutcome::Stale
            } else {
                ProcessOutcome::LeaseLost
            }));
        }

        let document = match SearchDocument::from_row(
            node,
            &row.get::<String, _>("kind"),
            &row.get::<String, _>("title"),
            &row.get::<String, _>("payload"),
            &search_visibility,
            owner_account_id.as_deref(),
        ) {
            Ok(document) => document,
            Err(_) => return Ok(Err("document_invalid")),
        };
        if document.scopes.as_slice() == ["owner"] {
            // Private content remains searchable only as an authorized lexical
            // projection inside PostgreSQL. It is never sent to the embedding
            // provider and never receives a vector.
            // Account lifecycle updates lock the account row before they
            // redact projections and bump node versions. Lock the same row first
            // here, before the existing version/job fence, so both paths have one
            // global lock order and cannot deadlock each other.
            let active_owner_account_id = owner_account_id
                .as_deref()
                .context("private projection missing active owner")?;
            let mut tx = self.pool.begin().await?;
            let owner_locked = sqlx::query_scalar::<_, String>(
                "SELECT id FROM domain_accounts WHERE id=$1 AND disabled=FALSE FOR SHARE",
            )
            .bind(active_owner_account_id)
            .fetch_optional(&mut *tx)
            .await?
            .is_some();
            if !owner_locked {
                tx.rollback().await?;
                return Ok(Ok(if self.claim_is_current(job_id, attempts).await? {
                    ProcessOutcome::Stale
                } else {
                    ProcessOutcome::LeaseLost
                }));
            }
            let written = sqlx::query("INSERT INTO search_node_projections (generation_id,node_id,source_version,source_revision,content_sha256,title,tags,searchable_text,language,kind,status,visibility_scopes,semantic_state,embedding) \
                SELECT $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'active',ARRAY['owner']::TEXT[],'unavailable',NULL \
                FROM (SELECT v.node_id FROM search_node_versions v JOIN domain_nodes n ON n.id=v.node_id JOIN search_index_generations g ON g.generation_id=$1 JOIN search_projection_jobs j ON j.id=$11 WHERE n.id=$2 AND n.search_visibility = 'private' AND weltgewebe_search_node_owner_account_id(n.payload) IS NOT NULL AND v.source_version=$3 AND v.source_revision=$4 AND v.deleted_at IS NULL AND g.state IN ('building','ready','active') AND j.generation_id=$1 AND j.node_id=$2 AND j.source_version=$3 AND j.source_revision=$4 AND j.operation='upsert' AND j.state='claimed' AND j.claimed_by=$12 AND j.attempt_count=$13 AND j.claim_until > NOW() FOR UPDATE OF v,j) current \
                ON CONFLICT (generation_id,node_id) DO UPDATE SET source_version=EXCLUDED.source_version,source_revision=EXCLUDED.source_revision,content_sha256=EXCLUDED.content_sha256,title=EXCLUDED.title,tags=EXCLUDED.tags,searchable_text=EXCLUDED.searchable_text,language=EXCLUDED.language,kind=EXCLUDED.kind,status=EXCLUDED.status,visibility_scopes=EXCLUDED.visibility_scopes,semantic_state=EXCLUDED.semantic_state,embedding=NULL,indexed_at=clock_timestamp() \
                WHERE search_node_projections.source_version <= EXCLUDED.source_version")
                .bind(generation)
                .bind(node)
                .bind(version)
                .bind(revision)
                .bind(document.hash())
                .bind(&document.title)
                .bind(&document.tags)
                .bind(&document.text)
                .bind(&document.language)
                .bind(&document.kind)
                .bind(job_id)
                .bind(&self.worker_id)
                .bind(attempts)
                .execute(&mut *tx)
                .await?
                .rows_affected();
            tx.commit().await?;
            return Ok(Ok(if written == 1 {
                ProcessOutcome::Processed
            } else if self.claim_is_current(job_id, attempts).await? {
                ProcessOutcome::Stale
            } else {
                ProcessOutcome::LeaseLost
            }));
        }
        let embedding = match self
            .provider
            .embed(&document.embedding_text(), dimension as usize)
            .await
        {
            Ok(vector)
                if vector.len() == dimension as usize
                    && vector.iter().all(|v| v.is_finite())
                    && vector.iter().map(|v| v * v).sum::<f64>() > 0.0 =>
            {
                vector
            }
            Ok(vector) if vector.len() != dimension as usize => {
                return Ok(Err("provider_wrong_dimension"))
            }
            Ok(_) => return Ok(Err("provider_invalid_vector")),
            Err(EmbeddingProviderError::Unavailable) => {
                return Ok(Ok(ProcessOutcome::PendingProvider))
            }
            Err(error) => return Ok(Err(error.code())),
        };
        // This INSERT is itself revision fenced. Its affected-row semantics
        // classify a concurrent canonical mutation as Stale, closing TOCTOU.
        let written = sqlx::query("INSERT INTO search_node_projections (generation_id,node_id,source_version,source_revision,content_sha256,title,tags,searchable_text,language,kind,status,visibility_scopes,semantic_state,embedding) \
            SELECT $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,'ready',$13 \
            FROM (SELECT v.node_id FROM search_node_versions v JOIN domain_nodes n ON n.id=v.node_id JOIN search_index_generations g ON g.generation_id=$1 JOIN search_projection_jobs j ON j.id=$14 WHERE n.id=$2 AND n.search_visibility='public' AND v.source_version=$3 AND v.source_revision=$4 AND v.deleted_at IS NULL AND g.state IN ('building','ready','active') AND j.generation_id=$1 AND j.node_id=$2 AND j.source_version=$3 AND j.source_revision=$4 AND j.operation='upsert' AND j.state='claimed' AND j.claimed_by=$15 AND j.attempt_count=$16 AND j.claim_until > NOW() FOR UPDATE OF v,j) current \
            ON CONFLICT (generation_id,node_id) DO UPDATE SET source_version=EXCLUDED.source_version,source_revision=EXCLUDED.source_revision,content_sha256=EXCLUDED.content_sha256,title=EXCLUDED.title,tags=EXCLUDED.tags,searchable_text=EXCLUDED.searchable_text,language=EXCLUDED.language,kind=EXCLUDED.kind,status=EXCLUDED.status,visibility_scopes=EXCLUDED.visibility_scopes,semantic_state='ready',embedding=EXCLUDED.embedding,indexed_at=clock_timestamp() \
            WHERE search_node_projections.source_version <= EXCLUDED.source_version")
            .bind(generation).bind(node).bind(version).bind(revision).bind(document.hash()).bind(&document.title).bind(&document.tags).bind(&document.text).bind(&document.language).bind(&document.kind).bind(&document.status).bind(&document.scopes).bind(embedding).bind(job_id).bind(&self.worker_id).bind(attempts).execute(&self.pool).await?.rows_affected();
        Ok(Ok(if written == 1 {
            ProcessOutcome::Processed
        } else if self.claim_is_current(job_id, attempts).await? {
            ProcessOutcome::Stale
        } else {
            ProcessOutcome::LeaseLost
        }))
    }

    async fn finish(
        &self,
        id: i64,
        attempts: i32,
        state: &str,
        code: Option<&str>,
    ) -> anyhow::Result<bool> {
        let mut tx = self.pool.begin().await?;
        // Generation completion is a state transition, so serialize it with
        // generation start/activation and relevant domain mutations. Keeping
        // the fenced job finish and generation reconciliation in one
        // transaction also prevents a ready-index conflict from committing the
        // job while leaving its generation counters stale.
        sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended('weltgewebe.search.generation.activation', 0))")
            .execute(&mut *tx)
            .await?;
        let finished = sqlx::query("UPDATE search_projection_jobs SET state=$2,claimed_by=NULL,claim_until=NULL,completed_at=NOW(),last_error_code=$3 WHERE id=$1 AND state='claimed' AND claimed_by=$4 AND attempt_count=$5 AND claim_until > NOW()")
            .bind(id).bind(state).bind(code).bind(&self.worker_id).bind(attempts).execute(&mut *tx).await?.rows_affected() == 1;
        if !finished {
            tx.rollback().await?;
            return Ok(false);
        }
        sqlx::query("UPDATE search_index_generations g SET expected_nodes=(SELECT count(*) FROM search_projection_jobs j WHERE j.generation_id=g.generation_id), completed_nodes=(SELECT count(*) FROM search_projection_jobs j WHERE j.generation_id=g.generation_id AND j.state IN ('done','stale')) WHERE g.generation_id=(SELECT generation_id FROM search_projection_jobs WHERE id=$1)")
            .bind(id).execute(&mut *tx).await?;
        sqlx::query("UPDATE search_index_generations g SET state='ready' WHERE g.generation_id=(SELECT generation_id FROM search_projection_jobs WHERE id=$1) AND g.state='building' AND NOT EXISTS (SELECT 1 FROM search_index_generations r WHERE r.state='ready' AND r.generation_id<>g.generation_id) AND weltgewebe_search_generation_activation_ready(g.generation_id)")
            .bind(id).execute(&mut *tx).await?;
        tx.commit().await?;
        Ok(true)
    }

    async fn retry(&self, id: i64, attempts: i32, code: &str) -> anyhow::Result<bool> {
        let secs = (2_i32.pow(attempts.clamp(1, 7) as u32)).min(300);
        Ok(sqlx::query("UPDATE search_projection_jobs SET state='retry',claimed_by=NULL,claim_until=NULL,available_at=NOW()+make_interval(secs=>$2),last_error_code=$3 WHERE id=$1 AND state='claimed' AND claimed_by=$4 AND attempt_count=$5 AND claim_until > NOW()")
            .bind(id).bind(secs).bind(code).bind(&self.worker_id).bind(attempts).execute(&self.pool).await?.rows_affected() == 1)
    }
}

#[cfg(test)]
mod tests {
    use super::{
        http_host_header, http_response_complete, normalize_sha256_digest, sha256_digests_match,
        EmbeddingProvider, EmbeddingProviderError, GenerationSpec, OllamaEmbeddingProvider,
        SearchDocument,
    };
    use tokio::{
        io::{AsyncReadExt, AsyncWriteExt},
        net::TcpListener,
    };

    #[test]
    fn t004_selected_generation_identity_is_byte_compatible() {
        let generation = GenerationSpec {
            generation_id: "unused-for-derivation",
            provider: "local:ollama",
            model_id: "qwen3-embedding:4b",
            model_revision:
                "sha256:df5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff907",
            runtime_identity: "ollama:0.12.6@http://127.0.0.1:11434",
            dimension: 2560,
        };
        assert_eq!(
            generation.derived_id(),
            "search-gen-2e8358273aa6d41e6a59025985a99738614aba725b8f369b3a54f390f8752e5c"
        );
    }

    #[test]
    fn generation_identity_rejects_mismatched_configured_id() {
        let generation = GenerationSpec {
            generation_id: "search-gen-wrong",
            provider: "local:ollama",
            model_id: "qwen3-embedding:4b",
            model_revision:
                "sha256:df5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff907",
            runtime_identity: "ollama:0.12.6@http://127.0.0.1:11434",
            dimension: 2560,
        };
        assert!(generation.validate().is_err());
    }

    #[test]
    fn document_excludes_address_and_fails_closed_visibility() {
        let unknown = SearchDocument::from_row(
            "node-1",
            "place",
            "Fahrradhilfe",
            r#"{"summary":"Reparatur","address":"secret lane 9","tags":["Rad"]}"#,
            "hidden",
            None,
        )
        .expect("document");
        assert_eq!(unknown.status, "hidden");
        assert!(unknown.scopes.is_empty());
        assert!(!unknown.text.contains("secret lane 9"));

        let private = SearchDocument::from_row(
            "node-1",
            "place",
            "Fahrradhilfe",
            r#"{"created_by_account_id":"owner-a","summary":"Reparatur","address":"secret lane 9","tags":["Rad"]}"#,
            "private",
            Some("owner-a"),
        )
        .expect("private document");
        assert_eq!(private.status, "active");
        assert_eq!(private.scopes, ["owner"]);
        assert!(!private.text.contains("secret lane 9"));

        let sql_canonical_private = SearchDocument::from_row(
            "node-1",
            "place",
            "Fahrradhilfe",
            r#"{"created_by_account_id":"\t","summary":"Reparatur"}"#,
            "private",
            Some("\t"),
        )
        .expect("SQL-canonical private document");
        assert_eq!(sql_canonical_private.status, "active");
        assert_eq!(sql_canonical_private.scopes, ["owner"]);

        let public = SearchDocument::from_row(
            "node-1",
            "place",
            "Fahrradhilfe",
            r#"{"summary":"Reparatur","tags":["Rad"]}"#,
            "public",
            None,
        )
        .expect("document");
        assert_eq!(public.status, "active");
        assert_eq!(public.scopes, ["public"]);
        assert_eq!(
            public.embedding_text(),
            "Titel: Fahrradhilfe\nKurzbeschreibung: Reparatur\nInformation: \nTags: Rad\nArt: place\nSprache: und"
        );
    }

    #[test]
    fn document_rejects_blank_projection_required_fields() {
        assert!(SearchDocument::from_row("node", "Kind", "   ", "{}", "public", None).is_err());
        assert!(SearchDocument::from_row("node", "   ", "Titel", "{}", "public", None).is_err());
    }

    #[test]
    fn document_hash_changes_for_indexed_content_not_unindexed_payload() {
        let first = SearchDocument::from_row(
            "node",
            "Kind",
            "Titel",
            r#"{"address":"a"}"#,
            "public",
            None,
        )
        .expect("document");
        let same = SearchDocument::from_row(
            "node",
            "Kind",
            "Titel",
            r#"{"address":"b"}"#,
            "public",
            None,
        )
        .expect("document");
        let changed = SearchDocument::from_row(
            "node",
            "Kind",
            "Titel",
            r#"{"summary":"neu"}"#,
            "public",
            None,
        )
        .expect("document");
        assert_eq!(first.hash(), same.hash());
        assert_ne!(first.hash(), changed.hash());
    }

    #[test]
    fn ollama_ipv6_loopback_uses_bracketed_host_header() {
        assert_eq!(http_host_header("::1", 11434), "[::1]:11434");
        assert_eq!(http_host_header("127.0.0.1", 11434), "127.0.0.1:11434");
    }

    #[test]
    fn ollama_invalid_or_conflicting_content_length_is_permanent_format_error() {
        for response in [
            b"HTTP/1.1 200 OK\r\nContent-Length: abc\r\n\r\n{}".as_slice(),
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 3\r\n\r\n{}".as_slice(),
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Length: 2\r\n\r\n0\r\n\r\n"
                .as_slice(),
        ] {
            assert!(matches!(
                http_response_complete(response),
                Err(EmbeddingProviderError::MalformedResponse)
            ));
        }
    }

    #[tokio::test]
    async fn ollama_truncated_framed_response_is_temporary_unavailability() {
        let listener = match TcpListener::bind("127.0.0.1:0").await {
            Ok(listener) => listener,
            Err(error) if error.kind() == std::io::ErrorKind::PermissionDenied => return,
            Err(error) => panic!("bind loopback: {error}"),
        };
        let port = listener.local_addr().expect("address").port();
        let server = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.expect("accept");
            let mut request = [0_u8; 4096];
            let _ = stream.read(&mut request).await.expect("read request");
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 64\r\nConnection: close\r\n\r\n{\"models\":[")
                .await
                .expect("partial response");
        });
        let provider = OllamaEmbeddingProvider::new(
            &format!("http://127.0.0.1:{port}/"),
            "test-model".into(),
            "sha256:exact".into(),
            format!("ollama:test-runtime@http://127.0.0.1:{port}"),
        )
        .expect("provider");
        assert!(matches!(
            provider.object("api/tags", None).await,
            Err(EmbeddingProviderError::Unavailable)
        ));
        server.await.expect("server");
    }

    #[tokio::test]
    async fn ollama_408_and_429_are_temporary_unavailability() {
        for status in [408_u16, 429_u16] {
            let listener = match TcpListener::bind("127.0.0.1:0").await {
                Ok(listener) => listener,
                Err(error) if error.kind() == std::io::ErrorKind::PermissionDenied => return,
                Err(error) => panic!("bind loopback: {error}"),
            };
            let port = listener.local_addr().expect("address").port();
            let server = tokio::spawn(async move {
                let (mut stream, _) = listener.accept().await.expect("accept");
                let mut request = [0_u8; 4096];
                let _ = stream.read(&mut request).await.expect("read request");
                stream
                    .write_all(
                        format!(
                            "HTTP/1.1 {status} Temporary\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                        )
                        .as_bytes(),
                    )
                    .await
                    .expect("temporary response");
            });
            let provider = OllamaEmbeddingProvider::new(
                &format!("http://127.0.0.1:{port}/"),
                "test-model".into(),
                "sha256:exact".into(),
                format!("ollama:test-runtime@http://127.0.0.1:{port}"),
            )
            .expect("provider");
            assert!(matches!(
                provider.object("api/tags", None).await,
                Err(EmbeddingProviderError::Unavailable)
            ));
            server.await.expect("server");
        }
    }

    #[test]
    fn sha256_digest_normalization_accepts_only_optional_lowercase_prefix() {
        const DIGEST: &str = "df5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff907";
        assert_eq!(normalize_sha256_digest(DIGEST), Some(DIGEST));
        let prefixed = format!("sha256:{DIGEST}");
        assert_eq!(normalize_sha256_digest(&prefixed), Some(DIGEST));
        assert!(sha256_digests_match(DIGEST, &prefixed));
        assert!(sha256_digests_match(&prefixed, DIGEST));
        assert!(!sha256_digests_match(
            "ef5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff907",
            &prefixed,
        ));

        for malformed in [
            "sha256:",
            "sha256:ABCDEF",
            "SHA256:df5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff907",
            "sha256:df5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff90g",
            "sha256:sha256:df5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff907",
        ] {
            assert_eq!(normalize_sha256_digest(malformed), None);
            assert!(!sha256_digests_match(malformed, &prefixed));
        }
    }

    #[tokio::test]
    #[ignore = "requires explicit T005 live Ollama identity environment"]
    async fn ollama_boundary_embeds_against_explicit_live_loopback() {
        let url = std::env::var("T005_LIVE_OLLAMA_URL").expect("T005_LIVE_OLLAMA_URL");
        let model_id = std::env::var("T005_LIVE_OLLAMA_MODEL_ID").expect("model id");
        let model_revision =
            std::env::var("T005_LIVE_OLLAMA_MODEL_REVISION").expect("model revision");
        let runtime_identity =
            std::env::var("T005_LIVE_OLLAMA_RUNTIME_IDENTITY").expect("runtime identity");
        let dimension: usize = std::env::var("T005_LIVE_OLLAMA_DIMENSION")
            .expect("dimension")
            .parse()
            .expect("numeric dimension");
        let provider =
            OllamaEmbeddingProvider::new(&url, model_id, model_revision, runtime_identity)
                .expect("live local provider");
        provider
            .object("api/tags", None)
            .await
            .expect("live local tags");
        provider
            .object("api/version", None)
            .await
            .expect("live local version");
        provider
            .verify_identity()
            .await
            .expect("live local identity");
        let vector = provider
            .embed("T005 local provider contract proof", dimension)
            .await
            .expect("live local embedding");
        assert_eq!(vector.len(), dimension);
        assert!(vector.iter().all(|value| value.is_finite()));
    }

    #[tokio::test]
    async fn ollama_boundary_uses_only_mocked_loopback_and_rechecks_identity() {
        let listener = match TcpListener::bind("127.0.0.1:0").await {
            Ok(listener) => listener,
            Err(error) if error.kind() == std::io::ErrorKind::PermissionDenied => return,
            Err(error) => panic!("bind loopback: {error}"),
        };
        let port = listener.local_addr().expect("address").port();
        let server = tokio::spawn(async move {
            for _ in 0..5 {
                let (mut stream, _) = listener.accept().await.expect("accept");
                let mut request = [0_u8; 4096];
                let count = stream.read(&mut request).await.expect("read request");
                let request = std::str::from_utf8(&request[..count]).expect("request utf8");
                let (body, chunked) = if request.starts_with("GET /api/tags ") {
                    (
                        r#"{"models":[{"name":"test-model","digest":"df5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff907"}]}"#,
                        false,
                    )
                } else if request.starts_with("GET /api/version ") {
                    (r#"{"version":"test-runtime"}"#, false)
                } else if request.starts_with("POST /api/embed ") {
                    (r#"{"embeddings":[[0.25,0.5]]}"#, true)
                } else {
                    panic!("unexpected request: {request}");
                };
                let response = if chunked {
                    format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n{:X}\r\n{}\r\n0\r\n\r\n",
                        body.len(), body
                    )
                } else {
                    format!("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}", body.len(), body)
                };
                stream
                    .write_all(response.as_bytes())
                    .await
                    .expect("response");
            }
        });
        let origin = format!("http://127.0.0.1:{port}/");
        let provider = OllamaEmbeddingProvider::new(
            &origin,
            "test-model".into(),
            "sha256:df5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff907".into(),
            format!("ollama:test-runtime@http://127.0.0.1:{port}"),
        )
        .expect("provider");
        assert_eq!(
            provider.embed("only a test", 2).await.expect("embedding"),
            vec![0.25, 0.5]
        );
        server.await.expect("server");
        assert!(matches!(
            OllamaEmbeddingProvider::new(
                "https://127.0.0.1:11434/",
                "m".into(),
                "r".into(),
                "x".into()
            ),
            Err(EmbeddingProviderError::Unsupported)
        ));
        assert!(matches!(
            OllamaEmbeddingProvider::new(
                "http://example.invalid/",
                "m".into(),
                "r".into(),
                "x".into()
            ),
            Err(EmbeddingProviderError::Unsupported)
        ));
    }
}

#[derive(Debug)]
pub struct SearchDocument {
    pub title: String,
    pub tags: Vec<String>,
    pub summary: String,
    pub info: String,
    pub text: String,
    pub language: String,
    pub kind: String,
    pub status: String,
    pub scopes: Vec<String>,
}
impl SearchDocument {
    pub fn from_row(
        node_id: &str,
        kind: &str,
        title: &str,
        payload: &str,
        search_visibility: &str,
        owner_account_id: Option<&str>,
    ) -> anyhow::Result<Self> {
        let payload: Value =
            serde_json::from_str(payload).context("domain node payload is not JSON")?;
        let tags = payload
            .get("tags")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .map(normalize)
            .filter(|s| !s.is_empty())
            .collect::<Vec<_>>();
        let mut tags = tags;
        tags.sort();
        tags.dedup();
        let summary = normalize(payload.get("summary").and_then(Value::as_str).unwrap_or(""));
        let info = normalize(payload.get("info").and_then(Value::as_str).unwrap_or(""));
        let language = normalize(
            payload
                .get("language")
                .and_then(Value::as_str)
                .filter(|v| !v.trim().is_empty())
                .unwrap_or("und"),
        );
        // Visibility and owner eligibility are server-owned canonical domain
        // state. PostgreSQL computes the owner once; Rust must not reinterpret
        // whitespace differently and accidentally route private text to the
        // embedding provider.
        let public = search_visibility == "public";
        let private = search_visibility == "private" && owner_account_id.is_some();
        let title = normalize(title);
        let kind = normalize(kind);
        // Projection columns enforce non-blank title/kind individually. Reject
        // invalid legacy domain rows before embedding or SQL mutation so the
        // leased job can terminate fail-closed as document_invalid.
        if title.is_empty() || kind.is_empty() {
            anyhow::bail!("search document for {node_id} has blank required fields");
        }
        let tag_text = tags.join(" ");
        let text = [
            title.as_str(),
            kind.as_str(),
            summary.as_str(),
            info.as_str(),
            tag_text.as_str(),
        ]
        .join("\n");
        if text.trim().is_empty() {
            anyhow::bail!("search document for {node_id} is empty");
        }
        Ok(Self {
            title,
            tags,
            summary,
            info,
            text,
            language,
            kind,
            status: if public || private {
                "active"
            } else {
                "hidden"
            }
            .to_owned(),
            scopes: if public {
                vec!["public".to_owned()]
            } else if private {
                vec!["owner".to_owned()]
            } else {
                vec![]
            },
        })
    }
    pub fn embedding_text(&self) -> String {
        format!(
            "Titel: {}\nKurzbeschreibung: {}\nInformation: {}\nTags: {}\nArt: {}\nSprache: {}",
            self.title,
            self.summary,
            self.info,
            self.tags.join(", "),
            self.kind,
            self.language
        )
    }

    pub fn hash(&self) -> String {
        let mut h = Sha256::new();
        let tags = self.tags.join("\u{1f}");
        for (label, value) in [
            ("document_revision", DOCUMENT_REVISION),
            ("normalization_revision", NORMALIZATION_REVISION),
            ("title", self.title.as_str()),
            ("summary", self.summary.as_str()),
            ("info", self.info.as_str()),
            ("tags", tags.as_str()),
            ("language", self.language.as_str()),
            ("kind", self.kind.as_str()),
        ] {
            h.update(label.as_bytes());
            h.update([0]);
            h.update(value.as_bytes());
            h.update([0]);
        }
        hex::encode(h.finalize())
    }
}

pub fn normalize(value: &str) -> String {
    value.nfkc().collect::<String>().trim().to_owned()
}
