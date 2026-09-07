//! T006 server-side hybrid search service.
use std::{env, sync::Arc, time::Instant};

use axum::{
    http::StatusCode,
    response::{IntoResponse, Response},
};
use serde::{Deserialize, Serialize};

use crate::{
    config::DomainReadSource,
    middleware::auth::AuthContext,
    routes::nodes::Node,
    search::{
        ranking::{
            rank_hybrid, SearchQuery, DEFAULT_SEMANTIC_MINIMUM_COSINE,
            DEFAULT_SEMANTIC_MINIMUM_MARGIN,
        },
        repository::{
            fetch_active_search_generation, fetch_postgres_candidates_for_generation,
            ActiveSearchGeneration, SearchFilters, SearchRepositoryError,
        },
        worker::{
            EmbeddingProvider, EmbeddingProviderError, GenerationSpec, OllamaEmbeddingProvider,
            DOCUMENT_REVISION, NORMALIZATION_REVISION, RANKING_REVISION,
        },
    },
    state::ApiState,
    telemetry::SearchRequestOutcome,
};

const DEFAULT_LIMIT: usize = 10;
const MAX_LIMIT: usize = 10;
const MAX_OFFSET: usize = 0;
const MAX_QUERY_CHARS: usize = 512;

#[derive(Debug, Clone, Deserialize, Default)]
pub struct SearchQueryParams {
    pub q: Option<String>,
    pub kind: Option<String>,
    pub kinds: Option<String>,
    pub tag: Option<String>,
    pub tags: Option<String>,
    pub language: Option<String>,
    pub languages: Option<String>,
    /// Bounded T003/T004 result size; T006 v1 accepts values from 1 through 10.
    pub limit: Option<usize>,
    /// Reserved for a future measured pagination contract; T006 v1 accepts only 0.
    pub offset: Option<usize>,
}

/// Bounded search result mode. The serialized HTTP/JSON wire format keeps the
/// pre-existing snake_case strings, so this does not change the wire contract.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum SearchMode {
    Hybrid,
    LexicalFallback,
}

impl From<SearchMode> for SearchRequestOutcome {
    fn from(mode: SearchMode) -> Self {
        match mode {
            SearchMode::Hybrid => Self::Hybrid,
            SearchMode::LexicalFallback => Self::LexicalFallback,
        }
    }
}

#[derive(Debug, Serialize, Deserialize, PartialEq)]
pub struct SearchResponse {
    pub items: Vec<Node>,
    pub mode: SearchMode,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub fallback_reason: Option<String>,
    pub generation_id: String,
    pub offset: usize,
}

#[derive(Debug, thiserror::Error)]
pub enum SearchError {
    #[error("provider contract error")]
    ProviderContractError(#[from] EmbeddingProviderError),
    #[error("search service unavailable")]
    Unavailable,
    #[error("invalid search request")]
    InvalidRequest,
    #[error("internal search error")]
    Internal,
}

impl IntoResponse for SearchError {
    fn into_response(self) -> Response {
        let (status, code) = match &self {
            SearchError::ProviderContractError(EmbeddingProviderError::Unavailable) => {
                (StatusCode::SERVICE_UNAVAILABLE, "provider_unavailable")
            }
            SearchError::ProviderContractError(error) => {
                (StatusCode::INTERNAL_SERVER_ERROR, error.code())
            }
            SearchError::Unavailable => (StatusCode::SERVICE_UNAVAILABLE, "search_unavailable"),
            SearchError::InvalidRequest => (StatusCode::BAD_REQUEST, "invalid_search_request"),
            SearchError::Internal => (StatusCode::INTERNAL_SERVER_ERROR, "search_internal_error"),
        };
        tracing::warn!(event = "search.request.failed", error_code = code, %status);
        (status, code).into_response()
    }
}

fn parse_list(single: &Option<String>, multiple: &Option<String>) -> Vec<String> {
    let mut values = Vec::new();
    for source in [single.as_deref(), multiple.as_deref()]
        .into_iter()
        .flatten()
    {
        values.extend(
            source
                .split(',')
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .map(str::to_owned),
        );
    }
    values.sort();
    values.dedup();
    values
}

fn validated_query(params: &SearchQueryParams) -> Result<SearchQuery, SearchError> {
    let raw_q = params.q.as_deref().unwrap_or("").trim();
    if raw_q.is_empty() || raw_q.chars().count() > MAX_QUERY_CHARS {
        return Err(SearchError::InvalidRequest);
    }
    let query = SearchQuery::new(raw_q);
    if query.normalized.is_empty() {
        return Err(SearchError::InvalidRequest);
    }
    Ok(query)
}

fn pagination(params: &SearchQueryParams) -> Result<(usize, usize), SearchError> {
    let limit = params.limit.unwrap_or(DEFAULT_LIMIT);
    let offset = params.offset.unwrap_or(0);
    if !(1..=MAX_LIMIT).contains(&limit) || offset > MAX_OFFSET {
        return Err(SearchError::InvalidRequest);
    }
    Ok((limit, offset))
}

fn validate_generation(generation: &ActiveSearchGeneration) -> Result<(), SearchError> {
    if generation.dimension <= 0
        || generation.document_revision != DOCUMENT_REVISION
        || generation.normalization_revision != NORMALIZATION_REVISION
        || generation.ranking_revision != RANKING_REVISION
    {
        return Err(SearchError::Unavailable);
    }
    let spec = GenerationSpec {
        generation_id: &generation.generation_id,
        provider: &generation.provider,
        model_id: &generation.model_id,
        model_revision: &generation.model_revision,
        runtime_identity: &generation.runtime_identity,
        dimension: generation.dimension,
    };
    if spec.derived_id() != generation.generation_id {
        return Err(SearchError::Unavailable);
    }
    Ok(())
}

fn runtime_provider(
    generation: &ActiveSearchGeneration,
) -> Result<Arc<dyn EmbeddingProvider>, SearchError> {
    if generation.provider != "local:ollama" {
        return Err(SearchError::ProviderContractError(
            EmbeddingProviderError::Unsupported,
        ));
    }
    let base_url =
        env::var("WELTGEWEBE_SEARCH_OLLAMA_URL").map_err(|_| SearchError::Unavailable)?;
    let provider = OllamaEmbeddingProvider::new(
        &base_url,
        generation.model_id.clone(),
        generation.model_revision.clone(),
        generation.runtime_identity.clone(),
    )?;
    Ok(Arc::new(provider))
}

pub async fn execute_search(
    state: &ApiState,
    auth: &AuthContext,
    params: SearchQueryParams,
    provider_override: Option<Arc<dyn EmbeddingProvider>>,
) -> Result<SearchResponse, SearchError> {
    let request_started = Instant::now();
    let result = execute_search_inner(state, auth, params, provider_override).await;
    let outcome = match &result {
        Ok(response) => response.mode.into(),
        Err(SearchError::ProviderContractError(_)) => SearchRequestOutcome::ProviderContractError,
        Err(SearchError::Unavailable) => SearchRequestOutcome::Unavailable,
        Err(SearchError::InvalidRequest) => SearchRequestOutcome::InvalidRequest,
        Err(SearchError::Internal) => SearchRequestOutcome::Internal,
    };
    state.metrics.search_request_outcome(outcome);
    state
        .metrics
        .observe_search_request_duration(request_started.elapsed());
    result
}

async fn execute_search_inner(
    state: &ApiState,
    auth: &AuthContext,
    params: SearchQueryParams,
    provider_override: Option<Arc<dyn EmbeddingProvider>>,
) -> Result<SearchResponse, SearchError> {
    if state.config.domain_read_source != DomainReadSource::Postgres {
        return Err(SearchError::Unavailable);
    }
    let pool = state.db_pool.as_ref().ok_or(SearchError::Unavailable)?;

    let query = validated_query(&params)?;
    let (limit, offset) = pagination(&params)?;
    let filters = SearchFilters {
        kinds: parse_list(&params.kind, &params.kinds),
        tags: parse_list(&params.tag, &params.tags),
        languages: parse_list(&params.language, &params.languages),
    };

    // Resolve and validate the tiny generation contract before asking the local
    // provider whether semantic vectors are needed. This keeps lexical fallback
    // from transferring megabytes of embeddings that it never reads.
    let generation_started = Instant::now();
    let generation_result = fetch_active_search_generation(pool).await;
    let generation_elapsed = generation_started.elapsed();
    let generation = match generation_result {
        Ok(Some(generation)) => generation,
        Ok(None) => {
            state
                .metrics
                .observe_search_repository_duration(generation_elapsed);
            return Err(SearchError::Unavailable);
        }
        Err(_) => {
            state
                .metrics
                .observe_search_repository_duration(generation_elapsed);
            return Err(SearchError::Internal);
        }
    };
    if let Err(error) = validate_generation(&generation) {
        state
            .metrics
            .observe_search_repository_duration(generation_elapsed);
        return Err(error);
    }

    let provider = match provider_override {
        Some(provider) => provider,
        None => match runtime_provider(&generation) {
            Ok(provider) => provider,
            Err(error) => {
                state
                    .metrics
                    .observe_search_repository_duration(generation_elapsed);
                return Err(error);
            }
        },
    };

    let query_embedding_text = query.embedding_text();
    let provider_started = Instant::now();
    let provider_result = provider
        .embed(&query_embedding_text, generation.dimension as usize)
        .await;
    state
        .metrics
        .observe_search_provider_duration(provider_started.elapsed());

    let candidate_started = Instant::now();
    let candidate_result = fetch_postgres_candidates_for_generation(
        pool,
        &generation,
        &query.raw,
        &filters,
        auth,
        provider_result.is_ok(),
    )
    .await;
    state
        .metrics
        .observe_search_repository_duration(generation_elapsed + candidate_started.elapsed());
    let candidate_set = match candidate_result {
        Ok(candidate_set) => candidate_set,
        Err(SearchRepositoryError::CandidateSetTooLarge) => {
            state.metrics.search_candidate_set_overflow();
            return Err(SearchError::Unavailable);
        }
        Err(SearchRepositoryError::Database(_))
        | Err(SearchRepositoryError::Json(_))
        | Err(SearchRepositoryError::InvalidVisibility(_)) => {
            return Err(SearchError::Internal);
        }
    };

    let lexical_ranked = candidate_set
        .candidates
        .iter()
        .filter(|candidate| candidate.rank_class != u8::MAX)
        .cloned()
        .collect::<Vec<_>>();

    state
        .metrics
        .observe_search_candidate_counts(candidate_set.candidates.len(), lexical_ranked.len());

    let (ranked, mode, fallback_reason) = match provider_result {
        Ok(query_vector) => (
            rank_hybrid(
                Some(&query_vector),
                lexical_ranked,
                &candidate_set.candidates,
                DEFAULT_SEMANTIC_MINIMUM_COSINE,
                DEFAULT_SEMANTIC_MINIMUM_MARGIN,
            ),
            SearchMode::Hybrid,
            None,
        ),
        Err(EmbeddingProviderError::Unavailable) => (
            lexical_ranked,
            SearchMode::LexicalFallback,
            Some("provider_unavailable".to_string()),
        ),
        Err(error) => return Err(SearchError::ProviderContractError(error)),
    };

    let items = ranked
        .into_iter()
        .skip(offset)
        .take(limit)
        .map(|candidate| candidate.node)
        .collect();

    Ok(SearchResponse {
        items,
        mode,
        fallback_reason,
        generation_id: candidate_set.generation.generation_id,
        offset,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pagination_is_a_bounded_top_ten_contract_without_offset_pages() {
        assert_eq!(pagination(&SearchQueryParams::default()).unwrap(), (10, 0));

        let exact = SearchQueryParams {
            limit: Some(MAX_LIMIT),
            offset: Some(0),
            ..Default::default()
        };
        assert_eq!(pagination(&exact).unwrap(), (MAX_LIMIT, 0));

        for invalid in [
            SearchQueryParams {
                limit: Some(0),
                ..Default::default()
            },
            SearchQueryParams {
                limit: Some(MAX_LIMIT + 1),
                ..Default::default()
            },
            SearchQueryParams {
                offset: Some(1),
                ..Default::default()
            },
        ] {
            assert!(matches!(
                pagination(&invalid),
                Err(SearchError::InvalidRequest)
            ));
        }
    }

    #[test]
    fn query_validation_rejects_empty_and_overlong_input() {
        for q in [None, Some("".to_string()), Some("   ".to_string())] {
            let params = SearchQueryParams {
                q,
                ..Default::default()
            };
            assert!(matches!(
                validated_query(&params),
                Err(SearchError::InvalidRequest)
            ));
        }

        let overlong = SearchQueryParams {
            q: Some("x".repeat(MAX_QUERY_CHARS + 1)),
            ..Default::default()
        };
        assert!(matches!(
            validated_query(&overlong),
            Err(SearchError::InvalidRequest)
        ));

        let valid = SearchQueryParams {
            q: Some("  Fahrrad Hilfe  ".to_string()),
            ..Default::default()
        };
        assert_eq!(validated_query(&valid).unwrap().raw, "Fahrrad Hilfe");
    }

    #[test]
    fn list_filters_merge_deduplicate_and_sort_single_and_plural_forms() {
        assert_eq!(
            parse_list(
                &Some("Werkstatt, Treffpunkt".to_string()),
                &Some("Treffpunkt,Bibliothek".to_string())
            ),
            vec![
                "Bibliothek".to_string(),
                "Treffpunkt".to_string(),
                "Werkstatt".to_string()
            ]
        );
    }

    /// Non-Postgres `ApiState` for the early-unavailable path, which returns
    /// before ever touching a database pool. Mirrors the state builder in
    /// `routes/health.rs`'s tests, the existing non-DB `ApiState` harness.
    fn state_without_postgres() -> ApiState {
        use crate::{
            auth::{rate_limit::AuthRateLimiter, session::SessionBackend},
            config::{AppConfig, AutoProvisionRole, DomainReadSource, PasskeyCredentialSource},
            state::OrderedCache,
            telemetry::{BuildInfo, Metrics},
        };
        use std::sync::atomic::AtomicI64;
        use tokio::sync::{Mutex, RwLock};

        let metrics = Metrics::try_new(BuildInfo {
            version: "test",
            commit: "test",
            build_timestamp: "test",
        })
        .expect("metrics");

        let config = AppConfig {
            max_guest_owned_nodes: 1_000,
            domain_read_source: DomainReadSource::Jsonl,
            domain_account_write_source: crate::config::DomainAccountWriteSource::Jsonl,
            domain_node_write_source: crate::config::DomainNodeWriteSource::Jsonl,
            domain_edge_write_source: crate::config::DomainEdgeWriteSource::Jsonl,
            passkey_credential_source: PasskeyCredentialSource::InMemory,
            auth_public_login: false,
            auth_cookie_secure: true,
            app_base_url: None,
            auth_trusted_proxies: None,
            auth_allow_emails: None,
            auth_allow_email_domains: None,
            auth_auto_provision: false,
            auth_auto_provision_role: AutoProvisionRole::Gast,
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
            tokens: crate::auth::tokens::TokenStore::new(),
            step_up_tokens: crate::auth::step_up_tokens::StepUpTokenStore::new(),
            accounts: Arc::new(RwLock::new(crate::auth::accounts::AccountStore::new())),
            nodes: Arc::new(RwLock::new(OrderedCache::new())),
            nodes_persist: Arc::new(Mutex::new(())),
            accounts_persist: Arc::new(Mutex::new(())),
            domain_projection_gate: Arc::new(RwLock::new(())),
            domain_projection_reload: std::sync::Arc::new(tokio::sync::Mutex::new(())),
            domain_projection_version: Arc::new(AtomicI64::new(0)),
            edges: Arc::new(RwLock::new(OrderedCache::new())),
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

    #[tokio::test]
    async fn early_unavailable_path_records_exactly_one_bounded_outcome_and_no_downstream_metrics()
    {
        use crate::auth::role::Role;

        let state = state_without_postgres();
        let auth = AuthContext {
            authenticated: false,
            account_id: None,
            device_id: None,
            role: Role::Gast,
            expires_at: None,
        };

        let result = execute_search(
            &state,
            &auth,
            SearchQueryParams {
                q: Some("Fahrrad".to_string()),
                ..Default::default()
            },
            None,
        )
        .await;

        assert!(matches!(result, Err(SearchError::Unavailable)));

        let rendered =
            String::from_utf8(state.metrics.render().expect("render metrics")).expect("utf8");
        assert!(rendered.contains("search_requests_total{outcome=\"unavailable\"} 1"));
        assert!(rendered.contains("search_request_duration_seconds_count 1"));
        assert!(
            rendered.contains("search_repository_duration_seconds_count 0"),
            "the early unavailable path must return before the repository is ever queried"
        );
        assert!(
            rendered.contains("search_provider_duration_seconds_count 0"),
            "the early unavailable path must return before an embedding provider is ever called"
        );
        assert!(rendered.contains("search_authorized_candidates_count 0"));
        assert!(rendered.contains("search_candidate_set_overflow_total 0"));
    }
}
