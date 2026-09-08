//! End-to-end proof for WELTGEWEBE-OS-V1-T002.
//!
//! Requires an isolated PostgreSQL database whose name contains `_test` and a
//! JetStream-enabled NATS server. The test constructs two independent API
//! states plus a third restart state against the same database.
//!
//! DATABASE_URL=postgres://welt@127.0.0.1:55432/weltgewebe_t002_test \
//! NATS_URL=nats://127.0.0.1:54222 \
//! cargo test --locked -p weltgewebe-api --test db_multi_instance_foundation \
//!   -- --ignored --test-threads=1

mod support;

use std::{
    net::{IpAddr, Ipv4Addr},
    path::PathBuf,
    sync::{atomic::AtomicI64, Arc},
    time::Duration,
};

use anyhow::Result;
use axum::{body, extract::State, response::IntoResponse, Extension};
use serial_test::serial;
use sqlx::{PgPool, Row};
use tokio::sync::{Mutex, RwLock};
use url::Url;
use uuid::Uuid;
use webauthn_rs::prelude::{Passkey, WebauthnBuilder};
use weltgewebe_api::{
    auth::{
        challenges::ChallengeIntent,
        passkeys::{
            start_passkey_authentication, start_passkey_registration, ConsumeGrantResult,
            PasskeyAuthenticationStore, PasskeyRegistrationGrantStore, PasskeyRegistrationStore,
            PasskeyStore, RegistrationInput,
        },
        rate_limit::{AuthRateLimiter, RateLimitError},
        role::Role,
        session::SessionBackend,
        step_up_tokens::{ConsumeMatchResult, StepUpTokenStore},
        tokens::TokenStore,
    },
    config::{
        AppConfig, AutoProvisionRole, DomainAccountWriteSource, DomainEdgeWriteSource,
        DomainNodeWriteSource, DomainReadSource, PasskeyCredentialSource,
    },
    domain_db::{domain_projection_version, load_stable_domain_projection_from_postgres},
    middleware::auth::AuthContext,
    outbox,
    state::ApiState,
    telemetry::{BuildInfo, Metrics},
};

const NODE_ID: &str = "multi-instance-node-1";
const ACCOUNT_ID: &str = "multi-instance-account-1";
const DEVICE_ID: &str = "multi-instance-device-1";

fn database_url() -> String {
    let url = std::env::var("DATABASE_URL")
        .expect("DATABASE_URL must point to a direct disposable PostgreSQL database");
    support::postgres_proof::validated_direct_disposable_url(url)
}

async fn migrate(pool: &PgPool) {
    let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("migrations");
    sqlx::migrate::Migrator::new(dir)
        .await
        .expect("load migrations")
        .run(pool)
        .await
        .expect("run migrations");
}

async fn reset(pool: &PgPool) {
    sqlx::query(
        "TRUNCATE domain_event_consumptions, domain_outbox, auth_rate_limit_counters, \
         auth_ephemeral_state, domain_edges, domain_nodes, domain_accounts RESTART IDENTITY CASCADE",
    )
    .execute(pool)
    .await
    .expect("reset isolated multi-instance test database");
    sqlx::query("UPDATE domain_projection_state SET version = 0 WHERE singleton = TRUE")
        .execute(pool)
        .await
        .expect("reset projection version");
}

fn config() -> AppConfig {
    AppConfig {
        max_guest_owned_nodes: 1_000,
        domain_read_source: DomainReadSource::Postgres,
        domain_account_write_source: DomainAccountWriteSource::Postgres,
        domain_node_write_source: DomainNodeWriteSource::Postgres,
        domain_edge_write_source: DomainEdgeWriteSource::Postgres,
        passkey_credential_source: PasskeyCredentialSource::InMemory,
        auth_public_login: true,
        auth_cookie_secure: true,
        app_base_url: Some("https://example.test".to_string()),
        auth_trusted_proxies: None,
        auth_allow_emails: None,
        auth_allow_email_domains: None,
        auth_auto_provision: false,
        auth_auto_provision_role: AutoProvisionRole::Gast,
        auth_rl_ip_per_min: Some(2),
        auth_rl_ip_per_hour: None,
        auth_rl_email_per_min: Some(2),
        auth_rl_email_per_hour: None,
        node_mutation_rate_limits: Default::default(),
        smtp_host: None,
        smtp_port: None,
        smtp_user: None,
        smtp_pass: None,
        smtp_from: None,
        webauthn_rp_id: Some("commonthing.net".to_string()),
        webauthn_rp_origin: Some("https://commonthing.net".to_string()),
        webauthn_rp_name: Some("commonThing Test".to_string()),
        auth_log_magic_token: false,
    }
}

fn test_passkey(credential_seed: u8) -> Passkey {
    use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
    let credential_id = URL_SAFE_NO_PAD.encode(vec![credential_seed; 32]);
    serde_json::from_value(serde_json::json!({
        "cred": {
            "cred_id": credential_id,
            "cred": {
                "type_": "ES256",
                "key": {
                    "EC_EC2": {
                        "curve": "SECP256R1",
                        "x": vec![1_u8; 32],
                        "y": vec![2_u8; 32]
                    }
                }
            },
            "counter": 0,
            "transports": null,
            "user_verified": false,
            "backup_eligible": false,
            "backup_state": false,
            "registration_policy": "preferred",
            "extensions": {
                "cred_protect": "NotRequested",
                "hmac_create_secret": "NotRequested"
            },
            "attestation": {"data": "None", "metadata": "None"},
            "attestation_format": "None"
        }
    }))
    .expect("passkey authentication fixture must deserialize")
}

fn metrics() -> Metrics {
    Metrics::try_new(BuildInfo {
        version: "multi-instance-test",
        commit: "multi-instance-test",
        build_timestamp: "multi-instance-test",
    })
    .expect("metrics")
}

async fn api_state(pool: PgPool, nats: async_nats::Client) -> Result<ApiState> {
    let (accounts, nodes, edges, version) =
        load_stable_domain_projection_from_postgres(&pool).await?;
    let cfg = config();
    Ok(ApiState {
        db_pool: Some(pool.clone()),
        db_pool_configured: true,
        nats_client: Some(nats),
        nats_configured: true,
        config: cfg.clone(),
        metrics: metrics(),
        sessions: SessionBackend::new_in_memory(),
        challenges: weltgewebe_api::auth::challenges::ChallengeStore::new_postgres(pool.clone()),
        tokens: TokenStore::new_postgres(pool.clone()),
        step_up_tokens: StepUpTokenStore::new_postgres(pool.clone()),
        accounts: Arc::new(RwLock::new(accounts)),
        nodes: Arc::new(RwLock::new(nodes)),
        nodes_persist: Arc::new(Mutex::new(())),
        accounts_persist: Arc::new(Mutex::new(())),
        domain_projection_gate: Arc::new(RwLock::new(())),
        domain_projection_reload: std::sync::Arc::new(tokio::sync::Mutex::new(())),
        domain_projection_local_node_patch_handoff: std::sync::Arc::new(
            std::sync::atomic::AtomicI64::new(-1),
        ),
        domain_projection_version: Arc::new(AtomicI64::new(version)),
        edges: Arc::new(RwLock::new(edges)),
        rate_limiter: Arc::new(AuthRateLimiter::new_postgres(&cfg, pool.clone())),
        mailer: None,
        webauthn: None,
        passkey_registrations: PasskeyRegistrationStore::new_postgres(pool.clone()),
        passkey_registration_grants: PasskeyRegistrationGrantStore::new_postgres(pool.clone()),
        passkey_authentications: PasskeyAuthenticationStore::new_postgres(pool),
        passkeys: PasskeyStore::new(),
        web_push: None,
    })
}

async fn wait_for_event_receipt(pool: &PgPool, event_id: i64) -> Result<()> {
    for _ in 0..100 {
        let row = sqlx::query(
            "SELECT published_at IS NOT NULL AS published, quarantined_at IS NOT NULL AS quarantined, \
                    EXISTS (SELECT 1 FROM domain_event_consumptions WHERE event_id = $1) AS consumed \
             FROM domain_outbox WHERE id = $1",
        )
        .bind(event_id)
        .fetch_one(pool)
        .await?;
        let published: bool = row.try_get("published")?;
        let quarantined: bool = row.try_get("quarantined")?;
        let consumed: bool = row.try_get("consumed")?;
        anyhow::ensure!(!quarantined, "outbox event was quarantined");
        if published && consumed {
            return Ok(());
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    anyhow::bail!("outbox event was not published and consumed in time")
}

#[tokio::test]
#[ignore = "requires isolated PostgreSQL and JetStream"]
#[serial]
async fn two_instances_and_restart_share_truth_and_event_receipts() -> Result<()> {
    let pool = PgPool::connect(&database_url()).await?;
    migrate(&pool).await;
    reset(&pool).await;

    let nats_url =
        std::env::var("NATS_URL").unwrap_or_else(|_| "nats://127.0.0.1:54222".to_string());
    let nats_a = async_nats::connect(&nats_url).await?;
    let nats_b = async_nats::connect(&nats_url).await?;
    outbox::start(pool.clone(), nats_a.clone(), metrics()).await?;
    outbox::start(pool.clone(), nats_b.clone(), metrics()).await?;

    let state_a = api_state(pool.clone(), nats_a).await?;
    let state_b = api_state(pool.clone(), nats_b).await?;

    // The actual logout-all route must create its step-up challenge in the
    // shared backend, not in the process-local fallback store.
    let response = weltgewebe_api::routes::auth::logout_all(
        State(state_a.clone()),
        Extension(AuthContext {
            authenticated: true,
            account_id: Some(ACCOUNT_ID.to_string()),
            device_id: Some(DEVICE_ID.to_string()),
            role: Role::Weber,
            expires_at: None,
        }),
    )
    .await
    .into_response();
    assert_eq!(response.status(), axum::http::StatusCode::FORBIDDEN);
    let response_body = body::to_bytes(response.into_body(), usize::MAX).await?;
    let response_json: serde_json::Value = serde_json::from_slice(&response_body)?;
    let logout_challenge_id = response_json["challenge_id"]
        .as_str()
        .expect("logout-all response contains challenge id");
    let logout_challenge = state_b
        .challenges
        .get_shared(logout_challenge_id)
        .await?
        .expect("logout-all challenge is visible on second instance");
    assert!(matches!(
        logout_challenge.intent,
        ChallengeIntent::LogoutAll
    ));

    // Magic links are newest-wins and single-use across processes.
    let old = state_a
        .tokens
        .create_shared("multi-instance@example.test".to_string())
        .await?;
    let current = state_b
        .tokens
        .create_shared("multi-instance@example.test".to_string())
        .await?;
    assert!(state_a.tokens.peek_shared(&old).await?.is_none());
    assert_eq!(
        state_a.tokens.consume_shared(&current).await?,
        Some("multi-instance@example.test".to_string())
    );
    assert!(state_b.tokens.consume_shared(&current).await?.is_none());

    // Step-up challenge and token can cross process boundaries exactly once.
    let challenge = state_a
        .challenges
        .create_shared(
            ACCOUNT_ID.to_string(),
            DEVICE_ID.to_string(),
            ChallengeIntent::RemoveDevice {
                target_device_id: "multi-instance-target-device".to_string(),
            },
        )
        .await?;
    assert_eq!(
        state_b
            .challenges
            .get_shared(&challenge.id)
            .await?
            .expect("challenge visible on second instance")
            .account_id,
        ACCOUNT_ID
    );
    assert!(state_b
        .challenges
        .consume_shared(&challenge.id)
        .await?
        .is_some());
    assert!(state_a
        .challenges
        .consume_shared(&challenge.id)
        .await?
        .is_none());

    let step_token = state_a
        .step_up_tokens
        .create_shared(
            challenge.id.clone(),
            ACCOUNT_ID.to_string(),
            DEVICE_ID.to_string(),
        )
        .await?;
    assert_eq!(
        state_b
            .step_up_tokens
            .consume_if_matches_shared(&step_token, &challenge.id, ACCOUNT_ID, DEVICE_ID)
            .await?,
        ConsumeMatchResult::Consumed
    );
    assert_eq!(
        state_a
            .step_up_tokens
            .consume_if_matches_shared(&step_token, &challenge.id, ACCOUNT_ID, DEVICE_ID)
            .await?,
        ConsumeMatchResult::NotFound
    );

    let grant = state_a
        .passkey_registration_grants
        .insert_shared(ACCOUNT_ID.to_string(), DEVICE_ID.to_string())
        .await?;
    assert_eq!(
        state_b
            .passkey_registration_grants
            .consume_shared(&grant, ACCOUNT_ID, DEVICE_ID)
            .await?,
        ConsumeGrantResult::Consumed
    );

    // A real webauthn-rs registration ceremony survives process handoff.
    let origin = Url::parse("https://example.test")?;
    let webauthn = WebauthnBuilder::new("example.test", &origin)?.build()?;
    let input = RegistrationInput {
        webauthn_user_id: Uuid::new_v4(),
        user_name: "multi-instance@example.test",
        user_display_name: "Multi Instance Test",
    };
    let (_, registration_state) = start_passkey_registration(&webauthn, &input, None)?;
    let registration_id = state_a
        .passkey_registrations
        .insert_shared(ACCOUNT_ID.to_string(), registration_state)
        .await?;
    assert!(state_b
        .passkey_registrations
        .consume_shared(&registration_id, ACCOUNT_ID)
        .await?
        .is_some());
    assert!(state_a
        .passkey_registrations
        .consume_shared(&registration_id, ACCOUNT_ID)
        .await?
        .is_none());

    let (_, authentication_state) = start_passkey_authentication(&webauthn, &[test_passkey(9)])?;
    let authentication_id = state_a
        .passkey_authentications
        .insert_shared(ACCOUNT_ID.to_string(), authentication_state)
        .await?;
    let consumed_authentication = state_b
        .passkey_authentications
        .consume_shared(&authentication_id)
        .await?
        .expect("passkey authentication visible on second instance");
    assert_eq!(consumed_authentication.0, ACCOUNT_ID);
    assert!(state_a
        .passkey_authentications
        .consume_shared(&authentication_id)
        .await?
        .is_none());

    // Rate limiting is global, not multiplied by the number of instances.
    let ip = IpAddr::V4(Ipv4Addr::new(198, 51, 100, 42));
    let subject = "multi-instance-rate-key";
    state_a.rate_limiter.check_shared(ip, subject).await?;
    state_b.rate_limiter.check_shared(ip, subject).await?;
    assert!(matches!(
        state_a.rate_limiter.check_shared(ip, subject).await,
        Err(RateLimitError::IpLimited | RateLimitError::EmailLimited)
    ));

    // A committed DB write creates an outbox event in the same transaction and
    // invalidates both process-local projections through the version fence.
    sqlx::query(
        "INSERT INTO domain_nodes (id, kind, title, lat, lon, payload) \
         VALUES ($1, 'test', 'First title', 53.5, 10.0, '{}'::jsonb)",
    )
    .bind(NODE_ID)
    .execute(&pool)
    .await?;
    let version_after_insert = domain_projection_version(&pool).await?;
    anyhow::ensure!(
        version_after_insert > 0,
        "projection version did not advance"
    );
    let event_id: i64 = sqlx::query_scalar(
        "SELECT id FROM domain_outbox WHERE aggregate_id = $1 AND event_type = 'domain.node.created'",
    )
    .bind(NODE_ID)
    .fetch_one(&pool)
    .await?;
    wait_for_event_receipt(&pool, event_id).await?;
    let healthy_chain = outbox::load_event_chain_db_snapshot(&pool, 1, 600).await?;
    assert!(!healthy_chain.actionable_pending);
    assert!(!healthy_chain.quarantine_present);
    assert!(!healthy_chain.receipt_probe_missing);

    state_b.refresh_domain_projection_if_stale().await?;
    assert_eq!(
        state_b
            .nodes
            .read()
            .await
            .get(NODE_ID)
            .expect("node visible on second API instance")
            .title,
        "First title"
    );

    // Safe reads do not queue behind an already-running O(N) projection reload.
    // They may continue on the previous complete snapshot, while strict refresh
    // still catches the new generation once the single-flight owner finishes.
    sqlx::query("UPDATE domain_nodes SET title = 'Deferred title' WHERE id = $1")
        .bind(NODE_ID)
        .execute(&pool)
        .await?;
    let reload_guard = state_b.domain_projection_reload.lock().await;
    tokio::time::timeout(
        Duration::from_secs(1),
        state_b.refresh_domain_projection_for_read(),
    )
    .await
    .expect("safe read refresh must not wait for an active reload")?;
    assert_eq!(
        state_b
            .nodes
            .read()
            .await
            .get(NODE_ID)
            .expect("previous complete projection remains available")
            .title,
        "First title"
    );
    drop(reload_guard);
    state_b.refresh_domain_projection_if_stale().await?;
    assert_eq!(
        state_b
            .nodes
            .read()
            .await
            .get(NODE_ID)
            .expect("strict refresh catches deferred generation")
            .title,
        "Deferred title"
    );

    sqlx::query("UPDATE domain_nodes SET title = 'Second title' WHERE id = $1")
        .bind(NODE_ID)
        .execute(&pool)
        .await?;
    state_a.refresh_domain_projection_if_stale().await?;
    assert_eq!(
        state_a
            .nodes
            .read()
            .await
            .get(NODE_ID)
            .expect("updated node visible on first API instance")
            .title,
        "Second title"
    );

    // A fresh process reconstructs domain truth and can consume auth state
    // created before the restart.
    let restart_token = state_a
        .tokens
        .create_shared("restart@example.test".to_string())
        .await?;
    let nats_c = async_nats::connect(&nats_url).await?;
    let state_c = api_state(pool.clone(), nats_c).await?;
    assert_eq!(
        state_c
            .nodes
            .read()
            .await
            .get(NODE_ID)
            .expect("node reconstructed after restart")
            .title,
        "Second title"
    );
    assert_eq!(
        state_c.tokens.consume_shared(&restart_token).await?,
        Some("restart@example.test".to_string())
    );

    // Idempotency ledger accepts the first consumer effect and rejects replay.
    assert!(outbox::record_consumed_once(&pool, "test-consumer", 9_000_001).await?);
    assert!(!outbox::record_consumed_once(&pool, "test-consumer", 9_000_001).await?);

    // Failed relay attempts are delayed and ultimately quarantined. Keep the
    // synthetic event outside the live relays' claim window so this assertion
    // tests failure handling rather than racing the two workers started above.
    let failed_id: i64 = sqlx::query_scalar(
        "INSERT INTO domain_outbox \
         (aggregate_type, aggregate_id, event_type, payload, attempt_count, available_at) \
         VALUES ('node', 'multi-instance-failed', 'domain.node.updated', '{}'::jsonb, 1, \
                 NOW() + INTERVAL '1 hour') \
         RETURNING id",
    )
    .fetch_one(&pool)
    .await?;
    assert!(!outbox::mark_failed(&pool, failed_id, 1, "temporary").await?);
    let delayed: (bool, Option<String>, i32) = sqlx::query_as(
        "SELECT available_at > NOW(), last_error, attempt_count \
         FROM domain_outbox WHERE id = $1",
    )
    .bind(failed_id)
    .fetch_one(&pool)
    .await?;
    assert_eq!(delayed, (true, Some("temporary".to_string()), 1));
    assert!(outbox::mark_failed(&pool, failed_id, 10, "terminal").await?);
    let quarantined: (bool, Option<String>) = sqlx::query_as(
        "SELECT quarantined_at IS NOT NULL, last_error \
         FROM domain_outbox WHERE id = $1",
    )
    .bind(failed_id)
    .fetch_one(&pool)
    .await?;
    assert_eq!(quarantined, (true, Some("terminal".to_string())));
    let quarantined_chain = outbox::load_event_chain_db_snapshot(&pool, 1, 600).await?;
    assert!(quarantined_chain.quarantine_present);
    assert!(outbox::requeue_quarantined(&pool, failed_id).await?);
    let requeued: (bool, i32, bool, bool) = sqlx::query_as(
        "SELECT quarantined_at IS NULL, attempt_count, available_at <= NOW(), \
                last_error IS NULL \
         FROM domain_outbox WHERE id = $1",
    )
    .bind(failed_id)
    .fetch_one(&pool)
    .await?;
    assert_eq!(requeued, (true, 0, true, true));
    assert!(!outbox::requeue_quarantined(&pool, failed_id).await?);

    // Readiness is current-health, not an all-history integrity scan. A retry
    // scheduled into the future is not actionable yet; an old missing receipt
    // has aged out of the bounded health window; a recent missing receipt is
    // detected until its durable ledger entry exists.
    reset(&pool).await;
    sqlx::query(
        "INSERT INTO domain_outbox \
         (aggregate_type, aggregate_id, event_type, payload, created_at, available_at) \
         VALUES ('node', 'scheduled-retry', 'domain.node.updated', '{}'::jsonb, \
                 NOW() - INTERVAL '20 minutes', NOW() + INTERVAL '5 minutes')",
    )
    .execute(&pool)
    .await?;
    let scheduled = outbox::load_event_chain_db_snapshot(&pool, 60, 600).await?;
    assert!(!scheduled.actionable_pending);

    sqlx::query(
        "INSERT INTO domain_outbox \
         (aggregate_type, aggregate_id, event_type, payload, published_at) \
         VALUES ('node', 'historical-gap', 'domain.node.updated', '{}'::jsonb, \
                 NOW() - INTERVAL '20 minutes')",
    )
    .execute(&pool)
    .await?;
    let historical = outbox::load_event_chain_db_snapshot(&pool, 60, 600).await?;
    assert!(!historical.receipt_probe_missing);

    let recent_missing_id: i64 = sqlx::query_scalar(
        "INSERT INTO domain_outbox \
         (aggregate_type, aggregate_id, event_type, payload, published_at) \
         VALUES ('node', 'recent-gap', 'domain.node.updated', '{}'::jsonb, \
                 NOW() - INTERVAL '2 minutes') \
         RETURNING id",
    )
    .fetch_one(&pool)
    .await?;
    let recent_missing = outbox::load_event_chain_db_snapshot(&pool, 60, 600).await?;
    assert!(recent_missing.receipt_probe_missing);
    assert!(recent_missing.receipt_probe_age_seconds >= 60);
    assert!(
        outbox::record_consumed_once(
            &pool,
            "weltgewebe-api-domain-receipts-v1",
            recent_missing_id,
        )
        .await?
    );
    let recovered = outbox::load_event_chain_db_snapshot(&pool, 60, 600).await?;
    assert!(!recovered.receipt_probe_missing);

    reset(&pool).await;
    Ok(())
}
