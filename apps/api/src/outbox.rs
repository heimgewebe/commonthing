use std::{future::Future, time::Duration};

use anyhow::Context;
use async_nats::{
    jetstream::{
        self,
        consumer::{self, FromConsumer, PullConsumer},
        stream,
    },
    Client, HeaderMap,
};
use chrono::{DateTime, Utc};
use futures_util::StreamExt;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sqlx::PgPool;

use crate::{
    config::{
        AppConfig, DomainAccountWriteSource, DomainEdgeWriteSource, DomainNodeWriteSource,
        DomainReadSource,
    },
    telemetry::{DomainEventWorker, Metrics},
};

pub(crate) const STREAM_NAME: &str = "WELTGEWEBE_DOMAIN";
pub(crate) const STREAM_SUBJECT: &str = "weltgewebe.domain.>";
pub(crate) const CONSUMER_NAME: &str = "weltgewebe-api-domain-receipts-v1";
pub(crate) const CONSUMER_LEDGER_NAME: &str = "weltgewebe-api-domain-receipts-v1";
const DOMAIN_EVENT_REPLICAS_ENV: &str = "WELTGEWEBE_DOMAIN_JETSTREAM_REPLICAS";
const DEFAULT_DOMAIN_EVENT_REPLICAS: usize = 1;
const CLAIM_BATCH_SIZE: i64 = 32;
const CLAIM_LEASE_SECONDS: i32 = 30;
const MAX_ATTEMPTS: i32 = 10;

#[derive(Debug, Clone, sqlx::FromRow)]
pub struct OutboxEvent {
    pub id: i64,
    pub aggregate_type: String,
    pub aggregate_id: String,
    pub event_type: String,
    pub payload: Value,
    pub created_at: DateTime<Utc>,
    pub attempt_count: i32,
}

#[derive(Debug, Clone, Copy, sqlx::FromRow)]
pub struct EventChainDbSnapshot {
    pub actionable_pending: bool,
    pub oldest_actionable_age_seconds: i64,
    pub quarantine_present: bool,
    pub receipt_probe_missing: bool,
    pub receipt_probe_age_seconds: i64,
}

#[derive(Debug, Serialize, Deserialize)]
struct PublishedDomainEvent {
    schema_version: u8,
    event_id: i64,
    aggregate_type: String,
    aggregate_id: String,
    event_type: String,
    payload: Value,
    created_at: DateTime<Utc>,
}

fn expected_stream_config(replicas: usize) -> stream::Config {
    stream::Config {
        name: STREAM_NAME.to_string(),
        description: Some(
            "Transactional commonThing domain events emitted from PostgreSQL".to_string(),
        ),
        subjects: vec![STREAM_SUBJECT.to_string()],
        retention: stream::RetentionPolicy::Limits,
        max_age: Duration::from_secs(30 * 24 * 60 * 60),
        storage: stream::StorageType::File,
        duplicate_window: Duration::from_secs(2 * 60),
        num_replicas: replicas,
        ..Default::default()
    }
}

fn expected_consumer_config(replicas: usize) -> consumer::pull::Config {
    consumer::pull::Config {
        durable_name: Some(CONSUMER_NAME.to_string()),
        filter_subject: STREAM_SUBJECT.to_string(),
        ack_policy: consumer::AckPolicy::Explicit,
        num_replicas: replicas,
        ..Default::default()
    }
}

fn parse_domain_event_replicas(raw: Option<&str>) -> anyhow::Result<usize> {
    match raw {
        None => Ok(DEFAULT_DOMAIN_EVENT_REPLICAS),
        Some("1") => Ok(1),
        Some("3") => Ok(3),
        Some(observed) => {
            anyhow::bail!("{DOMAIN_EVENT_REPLICAS_ENV} must be exactly 1 or 3, got {observed:?}")
        }
    }
}

fn domain_event_replicas() -> anyhow::Result<usize> {
    match std::env::var(DOMAIN_EVENT_REPLICAS_ENV) {
        Ok(value) => parse_domain_event_replicas(Some(&value)),
        Err(std::env::VarError::NotPresent) => parse_domain_event_replicas(None),
        Err(std::env::VarError::NotUnicode(_)) => {
            anyhow::bail!("{DOMAIN_EVENT_REPLICAS_ENV} is not valid Unicode")
        }
    }
}

fn validate_stream_shape_contract(config: &stream::Config) -> anyhow::Result<()> {
    anyhow::ensure!(
        config.name == STREAM_NAME,
        "domain JetStream has name {:?}, expected {STREAM_NAME:?}",
        config.name
    );
    anyhow::ensure!(
        config.subjects == [STREAM_SUBJECT],
        "domain JetStream subjects are {:?}, expected only {STREAM_SUBJECT:?}",
        config.subjects
    );
    anyhow::ensure!(
        config.retention == stream::RetentionPolicy::Limits,
        "domain JetStream retention is {:?}, expected limits",
        config.retention
    );
    anyhow::ensure!(
        config.storage == stream::StorageType::File,
        "domain JetStream storage is {:?}, expected file",
        config.storage
    );
    anyhow::ensure!(
        config.max_age == Duration::from_secs(30 * 24 * 60 * 60),
        "domain JetStream max_age is {:?}, expected 30 days",
        config.max_age
    );
    anyhow::ensure!(
        config.duplicate_window == Duration::from_secs(2 * 60),
        "domain JetStream duplicate_window is {:?}, expected 120 seconds",
        config.duplicate_window
    );
    Ok(())
}

fn stream_replica_upgrade_needed(
    config: &stream::Config,
    expected_replicas: usize,
) -> anyhow::Result<bool> {
    validate_stream_shape_contract(config)?;
    match (expected_replicas, config.num_replicas) {
        // Base/local mode may inherit a stream that was already HA-replicated.
        // Preserve 3 replicas rather than implicitly downgrading durable state.
        (1, 1 | 3) | (3, 3) => Ok(false),
        (3, 1) => Ok(true),
        (1 | 3, observed) => anyhow::bail!(
            "domain JetStream has {observed} replicas, expected {expected_replicas}; refusing implicit downgrade or unsupported replica drift"
        ),
        _ => anyhow::bail!("unsupported expected domain JetStream replica count: {expected_replicas}"),
    }
}

pub(crate) fn validate_stream_contract(
    config: &stream::Config,
    expected_replicas: usize,
) -> anyhow::Result<()> {
    validate_stream_shape_contract(config)?;
    let matches_replica_contract = match expected_replicas {
        1 => matches!(config.num_replicas, 1 | 3),
        3 => config.num_replicas == 3,
        _ => false,
    };
    anyhow::ensure!(
        matches_replica_contract,
        "domain JetStream has {} replicas, incompatible with deployment replica contract {expected_replicas}",
        config.num_replicas
    );
    Ok(())
}

fn validate_consumer_shape_config(
    stream_name: &str,
    consumer_name: &str,
    config: &consumer::Config,
) -> anyhow::Result<()> {
    anyhow::ensure!(
        stream_name == STREAM_NAME,
        "domain receipt consumer is attached to {:?}, expected {STREAM_NAME:?}",
        stream_name
    );
    anyhow::ensure!(
        consumer_name == CONSUMER_NAME,
        "domain receipt consumer has name {:?}, expected {CONSUMER_NAME:?}",
        consumer_name
    );
    anyhow::ensure!(
        config.durable_name.as_deref() == Some(CONSUMER_NAME),
        "domain receipt consumer is not the expected durable consumer"
    );
    anyhow::ensure!(
        config.deliver_subject.is_none(),
        "domain receipt consumer must remain pull-based"
    );
    anyhow::ensure!(
        config.filter_subject == STREAM_SUBJECT,
        "domain receipt consumer filter is {:?}, expected {STREAM_SUBJECT:?}",
        config.filter_subject
    );
    anyhow::ensure!(
        config.ack_policy == consumer::AckPolicy::Explicit,
        "domain receipt consumer must use explicit acknowledgements"
    );
    Ok(())
}

fn consumer_replica_upgrade_needed(
    stream_name: &str,
    consumer_name: &str,
    config: &consumer::Config,
    expected_replicas: usize,
) -> anyhow::Result<bool> {
    validate_consumer_shape_config(stream_name, consumer_name, config)?;
    match (expected_replicas, config.num_replicas) {
        // As with the stream, local/base mode never forces an HA consumer down to 1.
        (1, 0 | 1 | 3) | (3, 3) => Ok(false),
        (3, 0 | 1) => Ok(true),
        (1 | 3, observed) => anyhow::bail!(
            "domain receipt consumer has {observed} replicas, expected {expected_replicas}; refusing implicit downgrade or unsupported replica drift"
        ),
        _ => anyhow::bail!(
            "unsupported expected domain receipt consumer replica count: {expected_replicas}"
        ),
    }
}

fn validate_consumer_config(
    stream_name: &str,
    consumer_name: &str,
    config: &consumer::Config,
    expected_replicas: usize,
) -> anyhow::Result<()> {
    validate_consumer_shape_config(stream_name, consumer_name, config)?;
    let matches_replica_contract = match expected_replicas {
        1 => matches!(config.num_replicas, 0 | 1 | 3),
        3 => config.num_replicas == 3,
        _ => false,
    };
    anyhow::ensure!(
        matches_replica_contract,
        "domain receipt consumer has {} replicas, expected {expected_replicas}",
        config.num_replicas
    );
    Ok(())
}

pub(crate) fn validate_consumer_contract(
    info: &consumer::Info,
    expected_replicas: usize,
) -> anyhow::Result<()> {
    validate_consumer_config(
        &info.stream_name,
        &info.name,
        &info.config,
        expected_replicas,
    )
}

pub fn event_chain_required(config: &AppConfig) -> bool {
    config.domain_read_source == DomainReadSource::Postgres
        || config.domain_account_write_source == DomainAccountWriteSource::Postgres
        || config.domain_node_write_source == DomainNodeWriteSource::Postgres
        || config.domain_edge_write_source == DomainEdgeWriteSource::Postgres
}

async fn ensure_stream_with_replicas(
    client: Client,
    expected_replicas: usize,
) -> anyhow::Result<(jetstream::Context, stream::Stream)> {
    let context = jetstream::new(client);
    let mut stream = context
        .get_or_create_stream(expected_stream_config(expected_replicas))
        .await
        .context("failed to get or create commonThing domain JetStream")?;
    let info = stream
        .info()
        .await
        .context("failed to inspect commonThing domain JetStream")?;
    if stream_replica_upgrade_needed(&info.config, expected_replicas)? {
        let mut upgraded = info.config.clone();
        upgraded.num_replicas = expected_replicas;
        context
            .update_stream(&upgraded)
            .await
            .context("failed to upgrade commonThing domain JetStream replication")?;
        stream = context
            .get_stream(STREAM_NAME)
            .await
            .context("failed to re-open upgraded commonThing domain JetStream")?;
    }
    let info = stream
        .info()
        .await
        .context("failed to verify commonThing domain JetStream replication")?;
    validate_stream_contract(&info.config, expected_replicas)?;
    Ok((context, stream))
}

pub async fn ensure_stream(client: Client) -> anyhow::Result<(jetstream::Context, stream::Stream)> {
    let expected_replicas = domain_event_replicas()?;
    ensure_stream_with_replicas(client, expected_replicas).await
}

pub async fn verify_jetstream_contract(client: &Client) -> anyhow::Result<()> {
    let expected_replicas = domain_event_replicas()?;
    let context = jetstream::new(client.clone());
    let mut stream = context
        .get_stream(STREAM_NAME)
        .await
        .context("expected commonThing domain JetStream is missing")?;
    let stream_info = stream
        .info()
        .await
        .context("failed to inspect commonThing domain JetStream")?;
    validate_stream_contract(&stream_info.config, expected_replicas)?;
    let consumer_info = stream
        .consumer_info(CONSUMER_NAME)
        .await
        .context("expected durable domain receipt consumer is missing")?;
    validate_consumer_contract(&consumer_info, expected_replicas)
}

pub async fn load_event_chain_db_snapshot(
    pool: &PgPool,
    receipt_grace_seconds: i64,
    receipt_window_seconds: i64,
) -> anyhow::Result<EventChainDbSnapshot> {
    anyhow::ensure!(receipt_grace_seconds > 0, "receipt grace must be positive");
    anyhow::ensure!(
        receipt_window_seconds > receipt_grace_seconds,
        "receipt health window must exceed receipt grace"
    );
    sqlx::query_as::<_, EventChainDbSnapshot>(
        "WITH oldest_actionable AS MATERIALIZED (\
             SELECT available_at FROM domain_outbox \
              WHERE published_at IS NULL \
                AND quarantined_at IS NULL \
                AND available_at <= NOW() \
              ORDER BY available_at ASC, id ASC \
              LIMIT 1\
         ), missing_receipt AS MATERIALIZED (\
             SELECT event.published_at FROM domain_outbox event \
              WHERE event.published_at IS NOT NULL \
                AND event.published_at <= NOW() - make_interval(secs => $2) \
                AND event.published_at >= NOW() - make_interval(secs => $3) \
                AND NOT EXISTS (\
                    SELECT 1 FROM domain_event_consumptions receipt \
                     WHERE receipt.consumer_name = $1 \
                       AND receipt.event_id = event.id\
                ) \
              ORDER BY event.published_at ASC, event.id ASC \
              LIMIT 1\
         ) \
         SELECT \
             EXISTS (SELECT 1 FROM oldest_actionable) AS actionable_pending, \
             COALESCE((\
                 SELECT FLOOR(EXTRACT(EPOCH FROM (NOW() - available_at)))::BIGINT \
                   FROM oldest_actionable\
             ), 0)::BIGINT AS oldest_actionable_age_seconds, \
             EXISTS (\
                 SELECT 1 FROM domain_outbox \
                  WHERE published_at IS NULL AND quarantined_at IS NOT NULL \
                  LIMIT 1\
             ) AS quarantine_present, \
             EXISTS (SELECT 1 FROM missing_receipt) AS receipt_probe_missing, \
             COALESCE((\
                 SELECT FLOOR(EXTRACT(EPOCH FROM (NOW() - published_at)))::BIGINT \
                   FROM missing_receipt\
             ), 0)::BIGINT AS receipt_probe_age_seconds",
    )
    .bind(CONSUMER_LEDGER_NAME)
    .bind(receipt_grace_seconds)
    .bind(receipt_window_seconds)
    .fetch_one(pool)
    .await
    .context("failed to inspect time-bounded transactional domain event-chain health")
}

pub async fn claim_pending(pool: &PgPool, limit: i64) -> anyhow::Result<Vec<OutboxEvent>> {
    sqlx::query_as::<_, OutboxEvent>(
        "WITH picked AS (\
             SELECT id FROM domain_outbox \
             WHERE published_at IS NULL \
               AND quarantined_at IS NULL \
               AND available_at <= NOW() \
             ORDER BY available_at ASC, id ASC \
             FOR UPDATE SKIP LOCKED \
             LIMIT $1\
         ) \
         UPDATE domain_outbox AS event \
            SET attempt_count = event.attempt_count + 1, \
                available_at = NOW() + make_interval(secs => $2) \
           FROM picked \
          WHERE event.id = picked.id \
         RETURNING event.id, event.aggregate_type, event.aggregate_id, \
                   event.event_type, event.payload, event.created_at, event.attempt_count",
    )
    .bind(limit)
    .bind(CLAIM_LEASE_SECONDS)
    .fetch_all(pool)
    .await
    .context("failed to claim domain outbox events")
}

pub async fn mark_published(pool: &PgPool, event_id: i64) -> anyhow::Result<()> {
    sqlx::query(
        "UPDATE domain_outbox \
            SET published_at = NOW(), last_error = NULL \
          WHERE id = $1 AND published_at IS NULL AND quarantined_at IS NULL",
    )
    .bind(event_id)
    .execute(pool)
    .await
    .context("failed to mark domain outbox event published")?;
    Ok(())
}

pub async fn mark_failed(
    pool: &PgPool,
    event_id: i64,
    attempt_count: i32,
    error: &str,
) -> anyhow::Result<bool> {
    let error: String = error.chars().take(2_000).collect();
    if attempt_count >= MAX_ATTEMPTS {
        sqlx::query(
            "UPDATE domain_outbox \
                SET quarantined_at = NOW(), last_error = $2 \
              WHERE id = $1 AND published_at IS NULL",
        )
        .bind(event_id)
        .bind(&error)
        .execute(pool)
        .await
        .context("failed to quarantine domain outbox event")?;
        return Ok(true);
    }

    let exponent = u32::try_from(attempt_count.clamp(1, 8)).unwrap_or(8);
    let base_delay_seconds = (2_i32.pow(exponent) * 2).min(300);
    // Deterministic per-event jitter prevents many failed events from becoming
    // eligible in the same second while keeping retry timing testable.
    let jitter_seconds = i32::try_from(event_id.rem_euclid(11)).unwrap_or(0);
    let delay_seconds = (base_delay_seconds + jitter_seconds).min(300);
    sqlx::query(
        "UPDATE domain_outbox \
            SET available_at = NOW() + make_interval(secs => $2), last_error = $3 \
          WHERE id = $1 AND published_at IS NULL AND quarantined_at IS NULL",
    )
    .bind(event_id)
    .bind(delay_seconds)
    .bind(&error)
    .execute(pool)
    .await
    .context("failed to reschedule domain outbox event")?;
    Ok(false)
}

/// Requeue one explicitly quarantined event for a controlled operator retry.
/// Published events are never reopened, and non-quarantined events are left unchanged.
pub async fn requeue_quarantined(pool: &PgPool, event_id: i64) -> anyhow::Result<bool> {
    let result = sqlx::query(
        "UPDATE domain_outbox \
            SET quarantined_at = NULL, attempt_count = 0, available_at = NOW(), last_error = NULL \
          WHERE id = $1 AND published_at IS NULL AND quarantined_at IS NOT NULL",
    )
    .bind(event_id)
    .execute(pool)
    .await
    .context("failed to requeue quarantined domain outbox event")?;
    Ok(result.rows_affected() == 1)
}

pub async fn record_consumed_once(
    pool: &PgPool,
    consumer_name: &str,
    event_id: i64,
) -> anyhow::Result<bool> {
    let result = sqlx::query(
        "INSERT INTO domain_event_consumptions (consumer_name, event_id) \
         VALUES ($1, $2) ON CONFLICT DO NOTHING",
    )
    .bind(consumer_name)
    .bind(event_id)
    .execute(pool)
    .await
    .context("failed to record domain event consumption")?;
    Ok(result.rows_affected() == 1)
}

async fn publish_event(context: &jetstream::Context, event: &OutboxEvent) -> anyhow::Result<()> {
    let envelope = PublishedDomainEvent {
        schema_version: 1,
        event_id: event.id,
        aggregate_type: event.aggregate_type.clone(),
        aggregate_id: event.aggregate_id.clone(),
        event_type: event.event_type.clone(),
        payload: event.payload.clone(),
        created_at: event.created_at,
    };
    let subject = format!("weltgewebe.{}", event.event_type);
    let mut headers = HeaderMap::new();
    headers.insert("Nats-Msg-Id", format!("domain-outbox-{}", event.id));
    context
        .publish_with_headers(subject, headers, serde_json::to_vec(&envelope)?.into())
        .await
        .context("failed to send domain outbox event to JetStream")?
        .await
        .context("JetStream did not acknowledge domain outbox event")?;
    Ok(())
}

async fn relay_loop(pool: PgPool, context: jetstream::Context) {
    loop {
        match claim_pending(&pool, CLAIM_BATCH_SIZE).await {
            Ok(events) if events.is_empty() => {
                tokio::time::sleep(Duration::from_millis(250)).await;
            }
            Ok(events) => {
                for event in events {
                    match publish_event(&context, &event).await {
                        Ok(()) => {
                            if let Err(error) = mark_published(&pool, event.id).await {
                                tracing::error!(
                                    event_id = event.id,
                                    error = %error,
                                    "Published domain event could not be marked complete"
                                );
                            }
                        }
                        Err(error) => {
                            match mark_failed(
                                &pool,
                                event.id,
                                event.attempt_count,
                                &error.to_string(),
                            )
                            .await
                            {
                                Ok(true) => tracing::error!(
                                    event_id = event.id,
                                    attempts = event.attempt_count,
                                    error = %error,
                                    "Domain outbox event quarantined"
                                ),
                                Ok(false) => tracing::warn!(
                                    event_id = event.id,
                                    attempts = event.attempt_count,
                                    error = %error,
                                    "Domain outbox publish failed; retry scheduled"
                                ),
                                Err(mark_error) => tracing::error!(
                                    event_id = event.id,
                                    error = %error,
                                    mark_error = %mark_error,
                                    "Domain outbox publish and failure recording both failed"
                                ),
                            }
                        }
                    }
                }
            }
            Err(error) => {
                tracing::error!(error = %error, "Domain outbox claim failed");
                tokio::time::sleep(Duration::from_secs(1)).await;
            }
        }
    }
}

async fn receipt_consumer_loop(pool: PgPool, consumer: PullConsumer) {
    loop {
        let mut messages = match consumer.messages().await {
            Ok(messages) => messages,
            Err(error) => {
                tracing::error!(error = %error, "Domain receipt consumer could not fetch messages");
                tokio::time::sleep(Duration::from_secs(1)).await;
                continue;
            }
        };
        while let Some(message) = messages.next().await {
            let message = match message {
                Ok(message) => message,
                Err(error) => {
                    tracing::warn!(error = %error, "Domain receipt consumer message error");
                    break;
                }
            };
            let envelope: PublishedDomainEvent = match serde_json::from_slice(&message.payload) {
                Ok(envelope) => envelope,
                Err(error) => {
                    tracing::error!(
                        error = %error,
                        "Malformed internal domain event left unacknowledged for redelivery"
                    );
                    continue;
                }
            };
            match record_consumed_once(&pool, CONSUMER_LEDGER_NAME, envelope.event_id).await {
                Ok(first_delivery) => {
                    tracing::debug!(
                        event_id = envelope.event_id,
                        first_delivery,
                        "Domain event consumption receipt recorded"
                    );
                    if let Err(error) = message.ack().await {
                        tracing::warn!(event_id = envelope.event_id, error = %error, "Domain event ack failed");
                    }
                }
                Err(error) => {
                    tracing::error!(
                        event_id = envelope.event_id,
                        error = %error,
                        "Domain event consumption receipt failed; leaving message unacked"
                    );
                }
            }
        }
    }
}

fn spawn_essential_worker<F, Fut>(
    metrics: Metrics,
    worker: DomainEventWorker,
    factory: F,
) -> tokio::task::JoinHandle<()>
where
    F: Fn() -> Fut + Send + Sync + 'static,
    Fut: Future<Output = ()> + Send + 'static,
{
    metrics.set_domain_event_worker_up(worker, true);
    tokio::spawn(async move {
        loop {
            metrics.set_domain_event_worker_up(worker, true);
            let outcome = tokio::spawn(factory()).await;
            metrics.set_domain_event_worker_up(worker, false);
            match outcome {
                Ok(()) => tracing::error!(?worker, "Essential domain event worker exited"),
                Err(error) => tracing::error!(
                    ?worker,
                    %error,
                    "Essential domain event worker stopped unexpectedly"
                ),
            }
            tokio::time::sleep(Duration::from_secs(1)).await;
        }
    })
}

pub async fn start(pool: PgPool, client: Client, metrics: Metrics) -> anyhow::Result<()> {
    let expected_replicas = domain_event_replicas()?;
    let (context, stream) = ensure_stream_with_replicas(client, expected_replicas).await?;
    let mut consumer: PullConsumer = stream
        .get_or_create_consumer(CONSUMER_NAME, expected_consumer_config(expected_replicas))
        .await
        .context("failed to get or create domain receipt consumer")?;
    let consumer_info = stream
        .consumer_info(CONSUMER_NAME)
        .await
        .context("failed to inspect domain receipt consumer")?;
    if consumer_replica_upgrade_needed(
        &consumer_info.stream_name,
        &consumer_info.name,
        &consumer_info.config,
        expected_replicas,
    )? {
        let mut upgraded =
            consumer::pull::Config::try_from_consumer_config(consumer_info.config.clone())
                .map_err(|error| {
                    anyhow::anyhow!(
                        "existing domain receipt consumer is not pull-compatible: {error}"
                    )
                })?;
        upgraded.num_replicas = expected_replicas;
        consumer = stream
            .update_consumer(upgraded)
            .await
            .context("failed to upgrade domain receipt consumer replication")?;
    }
    let consumer_info = stream
        .consumer_info(CONSUMER_NAME)
        .await
        .context("failed to verify domain receipt consumer replication")?;
    validate_consumer_contract(&consumer_info, expected_replicas)?;

    let relay_pool = pool.clone();
    let relay_context = context.clone();
    spawn_essential_worker(metrics.clone(), DomainEventWorker::Relay, move || {
        relay_loop(relay_pool.clone(), relay_context.clone())
    });

    let receipt_pool = pool;
    let receipt_consumer = consumer;
    spawn_essential_worker(metrics, DomainEventWorker::ReceiptConsumer, move || {
        receipt_consumer_loop(receipt_pool.clone(), receipt_consumer.clone())
    });
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::{
        future::pending,
        sync::{
            atomic::{AtomicUsize, Ordering},
            Arc,
        },
        time::Duration,
    };

    use super::*;
    use crate::telemetry::BuildInfo;

    fn metrics() -> Metrics {
        Metrics::try_new(BuildInfo {
            version: "outbox-test",
            commit: "outbox-test",
            build_timestamp: "outbox-test",
        })
        .expect("metrics")
    }

    #[test]
    fn jetstream_contract_rejects_subject_and_durable_consumer_drift() {
        assert_eq!(
            parse_domain_event_replicas(None).expect("default replicas"),
            1
        );
        assert_eq!(
            parse_domain_event_replicas(Some("1")).expect("local replicas"),
            1
        );
        assert_eq!(
            parse_domain_event_replicas(Some("3")).expect("HA replicas"),
            3
        );
        for invalid in ["", "0", "2", "4", " 3 "] {
            assert!(parse_domain_event_replicas(Some(invalid)).is_err());
        }

        let local_stream = expected_stream_config(1);
        validate_stream_contract(&local_stream, 1).expect("local stream contract");
        assert!(!stream_replica_upgrade_needed(&local_stream, 1).expect("local stream"));
        assert!(stream_replica_upgrade_needed(&local_stream, 3).expect("legacy HA stream"));

        let ha_stream = expected_stream_config(3);
        validate_stream_contract(&ha_stream, 3).expect("HA stream contract");
        assert!(!stream_replica_upgrade_needed(&ha_stream, 3).expect("current HA stream"));
        validate_stream_contract(&ha_stream, 1).expect("local mode preserves HA stream");
        assert!(
            !stream_replica_upgrade_needed(&ha_stream, 1).expect("local mode preserves HA stream")
        );

        let mut invalid_replica_stream = ha_stream.clone();
        invalid_replica_stream.num_replicas = 2;
        assert!(stream_replica_upgrade_needed(&invalid_replica_stream, 3).is_err());
        let mut wrong_stream = ha_stream.clone();
        wrong_stream.subjects = vec!["unrelated.>".to_string()];
        assert!(validate_stream_contract(&wrong_stream, 3).is_err());
        let mut extra_subject = ha_stream.clone();
        extra_subject.subjects.push("unrelated.>".to_string());
        assert!(validate_stream_contract(&extra_subject, 3).is_err());
        let mut wrong_retention_policy = ha_stream.clone();
        wrong_retention_policy.retention = stream::RetentionPolicy::WorkQueue;
        assert!(validate_stream_contract(&wrong_retention_policy, 3).is_err());
        let mut wrong_storage = ha_stream.clone();
        wrong_storage.storage = stream::StorageType::Memory;
        assert!(validate_stream_contract(&wrong_storage, 3).is_err());
        let mut wrong_retention = ha_stream.clone();
        wrong_retention.max_age = Duration::from_secs(60);
        assert!(validate_stream_contract(&wrong_retention, 3).is_err());
        let mut wrong_deduplication = ha_stream.clone();
        wrong_deduplication.duplicate_window = Duration::from_secs(30);
        assert!(validate_stream_contract(&wrong_deduplication, 3).is_err());

        let consumer = |num_replicas| consumer::Config {
            durable_name: Some(CONSUMER_NAME.to_string()),
            filter_subject: STREAM_SUBJECT.to_string(),
            ack_policy: consumer::AckPolicy::Explicit,
            num_replicas,
            ..Default::default()
        };
        for local_replicas in [0, 1] {
            let local = consumer(local_replicas);
            validate_consumer_config(STREAM_NAME, CONSUMER_NAME, &local, 1)
                .expect("local durable consumer contract");
            assert!(
                !consumer_replica_upgrade_needed(STREAM_NAME, CONSUMER_NAME, &local, 1,)
                    .expect("local consumer")
            );
            assert!(
                consumer_replica_upgrade_needed(STREAM_NAME, CONSUMER_NAME, &local, 3,)
                    .expect("legacy HA consumer")
            );
        }
        let ha_consumer = consumer(3);
        validate_consumer_config(STREAM_NAME, CONSUMER_NAME, &ha_consumer, 3)
            .expect("HA durable consumer contract");
        assert!(
            !consumer_replica_upgrade_needed(STREAM_NAME, CONSUMER_NAME, &ha_consumer, 3,)
                .expect("HA consumer")
        );
        validate_consumer_config(STREAM_NAME, CONSUMER_NAME, &ha_consumer, 1)
            .expect("local mode preserves HA consumer");
        assert!(
            !consumer_replica_upgrade_needed(STREAM_NAME, CONSUMER_NAME, &ha_consumer, 1,)
                .expect("local mode preserves HA consumer")
        );

        let invalid_replica_consumer = consumer(2);
        assert!(consumer_replica_upgrade_needed(
            STREAM_NAME,
            CONSUMER_NAME,
            &invalid_replica_consumer,
            3,
        )
        .is_err());
        let mut ephemeral = ha_consumer;
        ephemeral.durable_name = None;
        assert!(validate_consumer_config(STREAM_NAME, CONSUMER_NAME, &ephemeral, 3).is_err());
    }

    #[tokio::test]
    async fn essential_worker_restarts_after_unexpected_exit() {
        let metrics = metrics();
        let attempts = Arc::new(AtomicUsize::new(0));
        let factory_attempts = attempts.clone();
        let supervisor =
            spawn_essential_worker(metrics.clone(), DomainEventWorker::Relay, move || {
                let factory_attempts = factory_attempts.clone();
                async move {
                    let attempt = factory_attempts.fetch_add(1, Ordering::SeqCst) + 1;
                    if attempt > 1 {
                        pending::<()>().await;
                    }
                }
            });

        tokio::time::timeout(Duration::from_secs(3), async {
            loop {
                if attempts.load(Ordering::SeqCst) >= 2
                    && metrics.domain_event_worker_is_up(DomainEventWorker::Relay)
                {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("essential worker supervisor must restart a terminated worker");
        supervisor.abort();
    }
}
