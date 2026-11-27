//! Bounded, resumable internal T005 projection backfill.  It has no HTTP role.

use std::env;

use sqlx::postgres::PgPoolOptions;
use weltgewebe_api::{
    search::{GenerationSpec, OllamaEmbeddingProvider, ProcessOutcome, ProjectionWorker},
    telemetry::{BuildInfo, Metrics},
};

fn required(name: &str) -> anyhow::Result<String> {
    env::var(name).map_err(|_| anyhow::anyhow!("{name} is required"))
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let database_url = required("DATABASE_URL")?;
    let generation_id = required("WELTGEWEBE_SEARCH_GENERATION_ID")?;
    let max_jobs: usize = env::var("WELTGEWEBE_SEARCH_BACKFILL_MAX_JOBS")
        .unwrap_or_else(|_| "100".to_string())
        .parse()?;
    if max_jobs == 0 || max_jobs > 10_000 {
        anyhow::bail!("WELTGEWEBE_SEARCH_BACKFILL_MAX_JOBS must be 1..=10000");
    }
    let pool = PgPoolOptions::new()
        .max_connections(2)
        .connect(&database_url)
        .await?;
    let provider = OllamaEmbeddingProvider::new(
        &required("WELTGEWEBE_SEARCH_OLLAMA_URL")?,
        required("WELTGEWEBE_SEARCH_MODEL_ID")?,
        required("WELTGEWEBE_SEARCH_MODEL_REVISION")?,
        required("WELTGEWEBE_SEARCH_RUNTIME_IDENTITY")?,
    )
    .map_err(|error| anyhow::anyhow!("invalid local Ollama provider: {}", error.code()))?;
    let worker = ProjectionWorker::new_with_provider(
        pool,
        format!("backfill:{}", std::process::id()),
        Metrics::try_new(BuildInfo::collect())?,
        std::sync::Arc::new(provider),
    )?;
    let provider_name = required("WELTGEWEBE_SEARCH_PROVIDER")?;
    let model_id = required("WELTGEWEBE_SEARCH_MODEL_ID")?;
    let model_revision = required("WELTGEWEBE_SEARCH_MODEL_REVISION")?;
    let runtime_identity = required("WELTGEWEBE_SEARCH_RUNTIME_IDENTITY")?;
    worker
        .start_generation(GenerationSpec {
            generation_id: &generation_id,
            provider: &provider_name,
            model_id: &model_id,
            model_revision: &model_revision,
            runtime_identity: &runtime_identity,
            dimension: required("WELTGEWEBE_SEARCH_DIMENSION")?.parse()?,
        })
        .await?;
    let mut completed = 0usize;
    while completed < max_jobs {
        match worker.claim_and_process_one(&generation_id).await? {
            ProcessOutcome::Empty => break,
            _ => completed += 1,
        }
    }
    // Deliberately no auto-activation: an operator invokes the atomic database
    // gate only after examining the bounded run's aggregate state.
    println!("search backfill processed {completed} jobs; generation remains unactivated");
    Ok(())
}
