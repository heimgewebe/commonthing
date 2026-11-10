use std::{collections::HashSet, env, sync::Arc, time::Duration};

use anyhow::{Context, Result};
use axum::{
    body::Body,
    http::{Request, StatusCode},
    response::Response,
    Router,
};
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use chrono::{DateTime, Utc};
use ed25519_dalek::{Signer, SigningKey};
use serde::Serialize;
use serde_json::{json, Value};
use sqlx::postgres::PgPoolOptions;
use tower::ServiceExt;
use uuid::Uuid;
use weltgewebe_api::federation::{
    router, CellIdentity, FederationEvent, FederationRepository, FederationService,
    MemoryFederationRepository, PeerPolicy, PostgresFederationRepository, PublishRequest,
    ReceiveStatus,
};

#[derive(Serialize)]
struct SigningPayload<'a> {
    protocol_version: &'a str,
    schema_version: u16,
    event_id: Uuid,
    event_type: &'a str,
    origin_cell_id: &'a str,
    actor: &'a str,
    object_address: &'a str,
    object_kind: &'a str,
    object_version: i64,
    previous_version: Option<i64>,
    created_at: DateTime<Utc>,
    scope: &'a str,
    neighbourhood_targets: &'a [String],
    payload: &'a Value,
    key_id: &'a str,
}

fn resign(event: &mut FederationEvent, seed: u8) -> Result<()> {
    let bytes = serde_jcs::to_vec(&SigningPayload {
        protocol_version: &event.protocol_version,
        schema_version: event.schema_version,
        event_id: event.event_id,
        event_type: &event.event_type,
        origin_cell_id: &event.origin_cell_id,
        actor: &event.actor,
        object_address: &event.object_address,
        object_kind: &event.object_kind,
        object_version: event.object_version,
        previous_version: event.previous_version,
        created_at: event.created_at,
        scope: &event.scope,
        neighbourhood_targets: &event.neighbourhood_targets,
        payload: &event.payload,
        key_id: &event.key_id,
    })?;
    let signing_key = SigningKey::from_bytes(&[seed; 32]);
    event.signature = URL_SAFE_NO_PAD.encode(signing_key.sign(&bytes).to_bytes());
    Ok(())
}

fn identity(cell_id: &str, key_id: &str, seed: u8) -> CellIdentity {
    CellIdentity::new(
        cell_id,
        format!("https://{cell_id}.example.test"),
        key_id,
        [seed; 32],
    )
    .expect("test identity")
}

async fn post_event(app: &Router, event: &FederationEvent) -> Result<Response> {
    Ok(app
        .clone()
        .oneshot(
            Request::post("/federation/v1/events")
                .header("content-type", "application/json")
                .body(Body::from(serde_json::to_vec(event)?))?,
        )
        .await?)
}

async fn get_missing_object(app: &Router) -> Result<Response> {
    Ok(app
        .clone()
        .oneshot(
            Request::get("/federation/v1/objects?address=wg%3A%2F%2Fcell-a%2Fnode%2Fmissing")
                .body(Body::empty())?,
        )
        .await?)
}

#[tokio::test]
#[ignore = "requires an isolated FEDERATION_TEST_DATABASE_URL"]
async fn postgres_repository_survives_restart_and_keeps_quarantine_separate() -> Result<()> {
    let database_url = env::var("FEDERATION_TEST_DATABASE_URL")
        .context("FEDERATION_TEST_DATABASE_URL must identify an isolated PostgreSQL database")?;
    let pool = PgPoolOptions::new()
        .max_connections(5)
        .connect(&database_url)
        .await?;
    sqlx::migrate!("./migrations").run(&pool).await?;

    let canonical_function: String = sqlx::query_scalar(
        "SELECT pg_get_functiondef('federation_canonical_neighbourhood_targets(jsonb)'::regprocedure)",
    )
    .fetch_one(&pool)
    .await?;
    assert!(
        canonical_function.contains("ORDER BY target COLLATE \"C\""),
        "database canonicalization must use C collation to match Rust string ordering"
    );

    let identity_a = identity("cell-a", "key-a", 61);
    let secondary_key = identity("cell-a", "key-a-secondary", 63).peer_key();
    let sender = FederationService::new(
        identity("cell-a", "key-a", 61),
        Arc::new(MemoryFederationRepository::new()),
    );
    let receiver_identity = identity("cell-b", "key-b", 62);
    let receiver = FederationService::new(
        receiver_identity.clone(),
        Arc::new(PostgresFederationRepository::new(pool.clone())),
    );
    receiver
        .install_peer(PeerPolicy {
            remote_cell_id: "cell-a".to_string(),
            state: "trusted".to_string(),
            allow_neighbourhood: true,
            allowed_event_types: HashSet::from([
                "object.upserted".to_string(),
                "object.deleted".to_string(),
            ]),
            keys: vec![identity_a.peer_key(), secondary_key.clone()],
        })
        .await?;
    let installed_key_states: Vec<(String, bool, bool)> = sqlx::query_as(
        "SELECT key_id, active, retired_at IS NOT NULL \
         FROM federation_peer_keys \
         WHERE remote_cell_id = 'cell-a' \
         ORDER BY key_id",
    )
    .fetch_all(&pool)
    .await?;
    assert_eq!(
        installed_key_states,
        vec![
            ("key-a".to_string(), true, false),
            ("key-a-secondary".to_string(), true, false),
        ]
    );
    let mut replacement_key = identity_a.peer_key();
    replacement_key.public_key = [77; 32];
    let replacement_error = receiver
        .install_peer(PeerPolicy {
            remote_cell_id: "cell-a".to_string(),
            state: "blocked".to_string(),
            allow_neighbourhood: false,
            allowed_event_types: HashSet::from(["object.deleted".to_string()]),
            keys: vec![replacement_key],
        })
        .await
        .expect_err("an existing cell/key id pair must reject replacement key bytes");
    assert!(replacement_error.to_string().contains("immutable"));
    let (peer_state, stored_key): (String, Vec<u8>) = sqlx::query_as(
        "SELECT relationship.state, key.public_key \
         FROM federation_peer_relationships AS relationship \
         JOIN federation_peer_keys AS key USING (remote_cell_id) \
         WHERE relationship.remote_cell_id = 'cell-a' AND key.key_id = 'key-a'",
    )
    .fetch_one(&pool)
    .await?;
    assert_eq!(peer_state, "trusted");
    assert_eq!(stored_key, identity_a.peer_key().public_key);
    let secondary_active: bool = sqlx::query_scalar(
        "SELECT active FROM federation_peer_keys \
         WHERE remote_cell_id = 'cell-a' AND key_id = 'key-a-secondary'",
    )
    .fetch_one(&pool)
    .await?;
    assert!(
        secondary_active,
        "failed immutable-key reinstall must roll back retirement of omitted keys"
    );

    let event = sender
        .publish_local(PublishRequest {
            actor: "system:postgres-proof".to_string(),
            event_type: "object.upserted".to_string(),
            object_address: "wg://cell-a/node/durable-node".to_string(),
            object_kind: "node".to_string(),
            object_version: 1,
            previous_version: None,
            scope: "global".to_string(),
            neighbourhood_targets: vec![],
            payload: json!({"title": "Durable node"}),
        })
        .await?;
    assert_eq!(
        receiver.receive(event.clone()).await?.status,
        ReceiveStatus::Applied
    );
    let direction_change = sqlx::query(
        "UPDATE federation_event_receipts SET direction = 'outbox' WHERE event_id = $1",
    )
    .bind(event.event_id)
    .execute(&pool)
    .await
    .expect_err("event receipt direction must be immutable");
    assert!(direction_change
        .to_string()
        .contains("federation event receipt direction is immutable"));

    // Reconstruct the service from the same database as a process-restart proof.
    drop(receiver);
    let restarted_repository = Arc::new(PostgresFederationRepository::new(pool.clone()));
    let restarted = FederationService::new(receiver_identity.clone(), restarted_repository.clone());
    let persisted = restarted
        .object("wg://cell-a/node/durable-node")
        .await?
        .expect("verified remote object must survive service reconstruction");
    assert_eq!(persisted.object_version, 1);
    assert_eq!(persisted.payload["title"], "Durable node");
    assert_eq!(
        restarted.receive(event.clone()).await?.status,
        ReceiveStatus::Duplicate
    );

    let mut tampered = event;
    tampered.event_id = uuid::Uuid::new_v4();
    tampered.payload = json!({"title": "Tampered after signing"});
    let outcome = restarted.receive(tampered).await?;
    assert_eq!(outcome.status, ReceiveStatus::Rejected);
    assert_eq!(
        outcome.reason.as_deref(),
        Some("event authentication rejected")
    );
    let quarantine = restarted.quarantined().await?;
    assert!(quarantine.is_empty());

    // Two independently signed first versions race for the same address.
    // The address-bound transaction lock must serialize the transition so
    // exactly one applies and the other is quarantined as stale.
    let race_sender_one = FederationService::new(
        identity("cell-a", "key-a", 61),
        Arc::new(MemoryFederationRepository::new()),
    );
    let race_sender_two = FederationService::new(
        identity("cell-a", "key-a", 61),
        Arc::new(MemoryFederationRepository::new()),
    );
    let race_event_one = race_sender_one
        .publish_local(PublishRequest {
            actor: "system:postgres-race-proof".to_string(),
            event_type: "object.upserted".to_string(),
            object_address: "wg://cell-a/node/race-node".to_string(),
            object_kind: "node".to_string(),
            object_version: 1,
            previous_version: None,
            scope: "global".to_string(),
            neighbourhood_targets: vec![],
            payload: json!({"winner": "one"}),
        })
        .await?;
    let race_event_two = race_sender_two
        .publish_local(PublishRequest {
            actor: "system:postgres-race-proof".to_string(),
            event_type: "object.upserted".to_string(),
            object_address: "wg://cell-a/node/race-node".to_string(),
            object_kind: "node".to_string(),
            object_version: 1,
            previous_version: None,
            scope: "global".to_string(),
            neighbourhood_targets: vec![],
            payload: json!({"winner": "two"}),
        })
        .await?;
    let (first, second) = tokio::join!(
        restarted.receive(race_event_one),
        restarted.receive(race_event_two)
    );
    let outcomes = [first?, second?];
    assert_eq!(
        outcomes
            .iter()
            .filter(|outcome| outcome.status == ReceiveStatus::Applied)
            .count(),
        1
    );
    assert_eq!(
        outcomes
            .iter()
            .filter(|outcome| outcome.status == ReceiveStatus::Quarantined)
            .count(),
        1
    );
    let quarantine = restarted.quarantined().await?;
    assert_eq!(quarantine.len(), 1);

    // Two valid envelopes with the same event id but different object locks
    // must also serialize. One receipt applies; the conflicting envelope is
    // quarantined instead of leaking a unique-constraint error as HTTP 500.
    let collision_event_one = race_sender_one
        .publish_local(PublishRequest {
            actor: "system:event-id-race-proof".to_string(),
            event_type: "object.upserted".to_string(),
            object_address: "wg://cell-a/node/event-id-race-one".to_string(),
            object_kind: "node".to_string(),
            object_version: 1,
            previous_version: None,
            scope: "global".to_string(),
            neighbourhood_targets: vec![],
            payload: json!({"winner": "one"}),
        })
        .await?;
    let mut collision_event_two = race_sender_two
        .publish_local(PublishRequest {
            actor: "system:event-id-race-proof".to_string(),
            event_type: "object.upserted".to_string(),
            object_address: "wg://cell-a/node/event-id-race-two".to_string(),
            object_kind: "node".to_string(),
            object_version: 1,
            previous_version: None,
            scope: "global".to_string(),
            neighbourhood_targets: vec![],
            payload: json!({"winner": "two"}),
        })
        .await?;
    collision_event_two.event_id = collision_event_one.event_id;
    resign(&mut collision_event_two, 61)?;
    let (first, second) = tokio::join!(
        restarted.receive(collision_event_one),
        restarted.receive(collision_event_two)
    );
    let outcomes = [first?, second?];
    assert_eq!(
        outcomes
            .iter()
            .filter(|outcome| outcome.status == ReceiveStatus::Applied)
            .count(),
        1
    );
    assert_eq!(
        outcomes
            .iter()
            .filter(|outcome| outcome.status == ReceiveStatus::Quarantined)
            .count(),
        1
    );
    assert_eq!(restarted.quarantined().await?.len(), 2);

    let neighbourhood_v1 = sender
        .publish_local(PublishRequest {
            actor: "system:postgres-scope-proof".to_string(),
            event_type: "object.upserted".to_string(),
            object_address: "wg://cell-a/node/immutable-audience".to_string(),
            object_kind: "node".to_string(),
            object_version: 1,
            previous_version: None,
            scope: "neighbourhood".to_string(),
            neighbourhood_targets: vec!["cell-c".to_string(), "cell-b".to_string()],
            payload: json!({"version": 1}),
        })
        .await?;
    assert_eq!(
        restarted.receive(neighbourhood_v1).await?.status,
        ReceiveStatus::Applied
    );

    // Rust canonicalizes strings by byte/codepoint order. PostgreSQL must use
    // C collation for the same ordering; locale-sensitive ordering such as
    // en_US may otherwise place `aab` before `a-b` and violate the DB CHECK.
    let locale_sensitive_neighbourhood = sender
        .publish_local(PublishRequest {
            actor: "system:postgres-collation-proof".to_string(),
            event_type: "object.upserted".to_string(),
            object_address: "wg://cell-a/node/collation-audience".to_string(),
            object_kind: "node".to_string(),
            object_version: 1,
            previous_version: None,
            scope: "neighbourhood".to_string(),
            neighbourhood_targets: vec!["cell-b".to_string(), "aab".to_string(), "a-b".to_string()],
            payload: json!({"version": 1}),
        })
        .await?;
    assert_eq!(
        restarted
            .receive(locale_sensitive_neighbourhood)
            .await?
            .status,
        ReceiveStatus::Applied
    );
    let locale_sensitive_stored = restarted
        .object("wg://cell-a/node/collation-audience")
        .await?
        .expect("locale-sensitive neighbourhood object");
    assert_eq!(
        locale_sensitive_stored.neighbourhood_targets,
        vec!["a-b".to_string(), "aab".to_string(), "cell-b".to_string(),]
    );

    let neighbourhood_v2 = sender
        .publish_local(PublishRequest {
            actor: "system:postgres-scope-proof".to_string(),
            event_type: "object.upserted".to_string(),
            object_address: "wg://cell-a/node/immutable-audience".to_string(),
            object_kind: "node".to_string(),
            object_version: 2,
            previous_version: Some(1),
            scope: "neighbourhood".to_string(),
            neighbourhood_targets: vec!["cell-b".to_string(), "cell-c".to_string()],
            payload: json!({"version": 2}),
        })
        .await?;
    let mut changed_scope = neighbourhood_v2.clone();
    changed_scope.event_id = Uuid::new_v4();
    changed_scope.scope = "global".to_string();
    changed_scope.neighbourhood_targets.clear();
    resign(&mut changed_scope, 61)?;
    let scope_outcome = restarted.receive(changed_scope).await?;
    assert_eq!(scope_outcome.status, ReceiveStatus::Quarantined);
    assert_eq!(
        scope_outcome.reason.as_deref(),
        Some("object scope cannot change")
    );
    assert_eq!(
        restarted.receive(neighbourhood_v2.clone()).await?.status,
        ReceiveStatus::Applied
    );
    let stored = restarted
        .object("wg://cell-a/node/immutable-audience")
        .await?
        .expect("neighbourhood object");
    assert_eq!(
        stored.neighbourhood_targets,
        vec!["cell-b".to_string(), "cell-c".to_string()]
    );
    let mut changed_audience = neighbourhood_v2;
    changed_audience.event_id = Uuid::new_v4();
    changed_audience.object_version = 3;
    changed_audience.previous_version = Some(2);
    changed_audience.neighbourhood_targets = vec!["cell-b".to_string(), "cell-d".to_string()];
    resign(&mut changed_audience, 61)?;
    let audience_outcome = restarted.receive(changed_audience).await?;
    assert_eq!(audience_outcome.status, ReceiveStatus::Quarantined);
    assert_eq!(
        audience_outcome.reason.as_deref(),
        Some("object neighbourhood audience cannot change")
    );

    // Reproduce the revocation TOCTOU window deterministically. The receive
    // path first observes the still-committed active key, then blocks on the
    // final in-transaction policy lock while a revocation is uncommitted. Once
    // revocation commits, acceptance must re-read the now-inactive key and
    // quarantine the event without mutating object state.
    let revocation_race_event = race_sender_one
        .publish_local(PublishRequest {
            actor: "system:postgres-revocation-race-proof".to_string(),
            event_type: "object.upserted".to_string(),
            object_address: "wg://cell-a/node/revocation-race".to_string(),
            object_kind: "node".to_string(),
            object_version: 1,
            previous_version: None,
            scope: "global".to_string(),
            neighbourhood_targets: vec![],
            payload: json!({"title": "Must lose to committed revocation"}),
        })
        .await?;
    let mut revocation_tx = pool.begin().await?;
    sqlx::query(
        "UPDATE federation_peer_keys SET active = FALSE, retired_at = NOW() \
         WHERE remote_cell_id = 'cell-a' AND key_id = 'key-a'",
    )
    .execute(&mut *revocation_tx)
    .await?;
    let race_receiver = restarted.clone();
    let receive_task =
        tokio::spawn(async move { race_receiver.receive(revocation_race_event).await });
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert!(
        !receive_task.is_finished(),
        "receive must wait for the in-flight peer-key policy update"
    );
    revocation_tx.commit().await?;
    let revocation_race_outcome = receive_task.await??;
    assert_eq!(revocation_race_outcome.status, ReceiveStatus::Quarantined);
    assert!(revocation_race_outcome
        .reason
        .as_deref()
        .unwrap_or_default()
        .contains("inactive"));
    assert!(restarted
        .object("wg://cell-a/node/revocation-race")
        .await?
        .is_none());
    assert_eq!(restarted.quarantined().await?.len(), 5);

    // Explicitly inactive means revoked for new deliveries. Rotation without
    // revocation remains possible by retaining the old verification key active.
    let mut inactive_key = identity_a.peer_key();
    inactive_key.active = false;
    restarted
        .install_peer(PeerPolicy {
            remote_cell_id: "cell-a".to_string(),
            state: "trusted".to_string(),
            allow_neighbourhood: true,
            allowed_event_types: HashSet::from([
                "object.upserted".to_string(),
                "object.deleted".to_string(),
            ]),
            keys: vec![inactive_key],
        })
        .await?;
    let revoked_sender = FederationService::new(
        identity("cell-a", "key-a", 61),
        Arc::new(MemoryFederationRepository::new()),
    );
    let revoked_event = revoked_sender
        .publish_local(PublishRequest {
            actor: "system:postgres-revocation-proof".to_string(),
            event_type: "object.upserted".to_string(),
            object_address: "wg://cell-a/node/revoked-delivery".to_string(),
            object_kind: "node".to_string(),
            object_version: 1,
            previous_version: None,
            scope: "global".to_string(),
            neighbourhood_targets: vec![],
            payload: json!({"title": "Must stay quarantined"}),
        })
        .await?;
    let revoked_outcome = restarted.receive(revoked_event).await?;
    assert_eq!(revoked_outcome.status, ReceiveStatus::Quarantined);
    assert!(revoked_outcome
        .reason
        .as_deref()
        .unwrap_or_default()
        .contains("inactive"));
    assert_eq!(restarted.quarantined().await?.len(), 6);

    sqlx::query(
        "INSERT INTO federation_quarantine \
         (event_id, origin_cell_id, reason, envelope_sha256, envelope, received_at) \
         VALUES ('ffffffff-ffff-ffff-ffff-ffffffffffff', 'cell-a', \
                 'retention-fixture', repeat('f', 64), '{}'::jsonb, \
                 NOW() - INTERVAL '31 days')",
    )
    .execute(&pool)
    .await?;
    sqlx::query(
        "INSERT INTO federation_quarantine \
         (event_id, origin_cell_id, reason, envelope_sha256, envelope) \
         SELECT ('00000000-0000-0000-0000-' || lpad(value::text, 12, '0'))::uuid, \
                'cell-a', 'capacity-fixture', \
                md5(value::text) || md5(value::text), \
                jsonb_build_object('fixture', value) \
         FROM generate_series(1, 1001) AS value",
    )
    .execute(&pool)
    .await?;
    let capacity_event = revoked_sender
        .publish_local(PublishRequest {
            actor: "system:postgres-quarantine-bound-proof".to_string(),
            event_type: "object.upserted".to_string(),
            object_address: "wg://cell-a/node/quarantine-bound".to_string(),
            object_kind: "node".to_string(),
            object_version: 1,
            previous_version: None,
            scope: "global".to_string(),
            neighbourhood_targets: vec![],
            payload: json!({"title": "Bounded"}),
        })
        .await?;
    assert_eq!(
        restarted.receive(capacity_event).await?.status,
        ReceiveStatus::Quarantined
    );
    let quarantine_count: i64 = sqlx::query_scalar(
        "SELECT COUNT(*) FROM federation_quarantine WHERE origin_cell_id = 'cell-a'",
    )
    .fetch_one(&pool)
    .await?;
    assert_eq!(quarantine_count, 1_000);
    let expired_count: i64 = sqlx::query_scalar(
        "SELECT COUNT(*) FROM federation_quarantine \
         WHERE origin_cell_id = 'cell-a' AND reason = 'retention-fixture'",
    )
    .fetch_one(&pool)
    .await?;
    assert_eq!(expired_count, 0);

    let local = restarted
        .publish_local(PublishRequest {
            actor: "system:postgres-proof".to_string(),
            event_type: "object.upserted".to_string(),
            object_address: "wg://cell-b/shared-room/durable-room".to_string(),
            object_kind: "shared-room".to_string(),
            object_version: 1,
            previous_version: None,
            scope: "global".to_string(),
            neighbourhood_targets: vec![],
            payload: json!({"title": "Durable room"}),
        })
        .await?;
    assert_eq!(local.object_version, 1);
    let pending = restarted.pending_outbox().await?;
    assert_eq!(pending.len(), 1);
    assert_eq!(
        pending[0].object_address,
        "wg://cell-b/shared-room/durable-room"
    );

    // Reactivate cell-a before namespace collision checks so peer-policy
    // rejection cannot mask the event-id invariant under test.
    restarted
        .install_peer(PeerPolicy {
            remote_cell_id: "cell-a".to_string(),
            state: "trusted".to_string(),
            allow_neighbourhood: true,
            allowed_event_types: HashSet::from([
                "object.upserted".to_string(),
                "object.deleted".to_string(),
            ]),
            keys: vec![identity_a.peer_key()],
        })
        .await?;

    // The event-id namespace is global across durable inbox and outbox. A
    // remote envelope cannot reuse a locally published id, and a local write
    // cannot reuse an id that was already accepted from a peer.
    let local_namespace_event = restarted
        .publish_local(PublishRequest {
            actor: "system:postgres-event-id-namespace".to_string(),
            event_type: "object.upserted".to_string(),
            object_address: "wg://cell-b/node/local-namespace".to_string(),
            object_kind: "node".to_string(),
            object_version: 1,
            previous_version: None,
            scope: "global".to_string(),
            neighbourhood_targets: vec![],
            payload: json!({"title": "Local namespace owner"}),
        })
        .await?;
    let mut remote_collision = revoked_sender
        .publish_local(PublishRequest {
            actor: "system:postgres-event-id-namespace".to_string(),
            event_type: "object.upserted".to_string(),
            object_address: "wg://cell-a/node/remote-namespace-collision".to_string(),
            object_kind: "node".to_string(),
            object_version: 1,
            previous_version: None,
            scope: "global".to_string(),
            neighbourhood_targets: vec![],
            payload: json!({"title": "Must collide"}),
        })
        .await?;
    remote_collision.event_id = local_namespace_event.event_id;
    resign(&mut remote_collision, 61)?;
    let collision = restarted.receive(remote_collision).await?;
    assert_eq!(collision.status, ReceiveStatus::Quarantined);
    assert_eq!(
        collision.reason.as_deref(),
        Some("event id collision with different envelope")
    );

    let inbound_first = revoked_sender
        .publish_local(PublishRequest {
            actor: "system:postgres-event-id-namespace".to_string(),
            event_type: "object.upserted".to_string(),
            object_address: "wg://cell-a/node/inbound-namespace-first".to_string(),
            object_kind: "node".to_string(),
            object_version: 1,
            previous_version: None,
            scope: "global".to_string(),
            neighbourhood_targets: vec![],
            payload: json!({"title": "Inbound namespace owner"}),
        })
        .await?;
    assert_eq!(
        restarted.receive(inbound_first.clone()).await?.status,
        ReceiveStatus::Applied
    );
    let mut local_conflict = inbound_first;
    local_conflict.origin_cell_id = "cell-b".to_string();
    local_conflict.object_address = "wg://cell-b/node/local-after-inbound".to_string();
    local_conflict.key_id = "key-b".to_string();
    local_conflict.signature.clear();
    let local_error = restarted_repository
        .persist_local(&local_conflict)
        .await
        .expect_err("inbox event id must block local outbox persistence");
    assert!(local_error
        .to_string()
        .contains("local federation event id already exists"));

    // Two independently constructed services sharing one PostgreSQL database
    // must consume one verified-peer bucket rather than one bucket per replica.
    let shared_one = FederationService::new(
        receiver_identity.clone(),
        Arc::new(PostgresFederationRepository::new(pool.clone())),
    );
    let shared_two = FederationService::new(
        receiver_identity.clone(),
        Arc::new(PostgresFederationRepository::new(pool.clone())),
    );
    let identity_c = identity("cell-c", "key-c", 63);
    shared_one
        .install_peer(PeerPolicy {
            remote_cell_id: "cell-c".to_string(),
            state: "trusted".to_string(),
            allow_neighbourhood: true,
            allowed_event_types: HashSet::from(["object.upserted".to_string()]),
            keys: vec![identity_c.peer_key()],
        })
        .await?;
    let sender_c = FederationService::new(identity_c, Arc::new(MemoryFederationRepository::new()));
    let shared_event = sender_c
        .publish_local(PublishRequest {
            actor: "system:postgres-shared-rate-proof".to_string(),
            event_type: "object.upserted".to_string(),
            object_address: "wg://cell-c/node/shared-rate-proof".to_string(),
            object_kind: "node".to_string(),
            object_version: 1,
            previous_version: None,
            scope: "global".to_string(),
            neighbourhood_targets: vec![],
            payload: json!({"title": "Shared rate bucket"}),
        })
        .await?;
    assert_eq!(
        shared_one.receive(shared_event.clone()).await?.status,
        ReceiveStatus::Applied
    );
    for _ in 0..=120 {
        assert_eq!(
            shared_two.receive(shared_event.clone()).await?.status,
            ReceiveStatus::Duplicate
        );
    }
    let duplicate_rate_count: i64 = sqlx::query_scalar(
        "SELECT COALESCE(SUM(request_count), 0)::bigint \
         FROM federation_rate_limit_counters \
         WHERE scope = 'receive-origin' AND subject = 'cell-b:cell-c'",
    )
    .fetch_one(&pool)
    .await?;
    assert_eq!(
        duplicate_rate_count, 1,
        "exact authenticated duplicates must not consume the shared origin bucket"
    );

    // Both receives below must stay in the same PostgreSQL clock minute:
    // crossing the boundary legitimately replenishes the peer's quota.
    // Avoid starting the proof near that boundary, using the database clock
    // rather than the (potentially skewed) CI runner clock.
    let seconds_left: i64 = sqlx::query_scalar(
        "SELECT 60 - (floor(extract(epoch FROM clock_timestamp()))::bigint % 60)",
    )
    .fetch_one(&pool)
    .await?;
    if seconds_left <= 15 {
        tokio::time::sleep(Duration::from_secs((seconds_left + 1) as u64)).await;
    }

    // Prime the active database-time bucket immediately below the limit. The
    // previous 119-event loop could straddle a minute boundary and make this
    // integration proof nondeterministic even though the shared counter was
    // correct. Direct seeding keeps the proof focused on cross-replica sharing.
    sqlx::query(
        "WITH database_window AS (\
           SELECT floor(extract(epoch FROM clock_timestamp()) / 60)::bigint * 60 AS window_start\
         ) \
         INSERT INTO federation_rate_limit_counters \
         (scope, subject, window_start, window_seconds, request_count, expires_at) \
         SELECT 'receive-origin', 'cell-b:cell-c', window_start, 60, 119, \
                to_timestamp(window_start + 120) \
         FROM database_window \
         ON CONFLICT (scope, subject, window_start, window_seconds) \
         DO UPDATE SET request_count = 119, expires_at = EXCLUDED.expires_at",
    )
    .execute(&pool)
    .await?;
    let last_allowed_event = sender_c
        .publish_local(PublishRequest {
            actor: "system:postgres-shared-rate-proof".to_string(),
            event_type: "object.upserted".to_string(),
            object_address: "wg://cell-c/node/shared-rate-last-allowed".to_string(),
            object_kind: "node".to_string(),
            object_version: 1,
            previous_version: None,
            scope: "global".to_string(),
            neighbourhood_targets: vec![],
            payload: json!({"last_allowed": true}),
        })
        .await?;
    assert_eq!(
        shared_one.receive(last_allowed_event).await?.status,
        ReceiveStatus::Applied
    );
    let limited_event = sender_c
        .publish_local(PublishRequest {
            actor: "system:postgres-shared-rate-proof".to_string(),
            event_type: "object.upserted".to_string(),
            object_address: "wg://cell-c/node/shared-rate-limited".to_string(),
            object_kind: "node".to_string(),
            object_version: 1,
            previous_version: None,
            scope: "global".to_string(),
            neighbourhood_targets: vec![],
            payload: json!({"limited": true}),
        })
        .await?;
    let limited = shared_two
        .receive(limited_event)
        .await
        .expect_err("121st verified event across replicas must be limited");
    assert!(limited.to_string().contains("rate limit"));

    // The global receive and public object-read circuit-breaker buckets are
    // shared across routers backed by the same PostgreSQL database. Seed each
    // shared DB window just below its high circuit-breaker threshold so the
    // proof stays fast while still crossing the replica boundary.
    for scope in ["receive-global", "object-read-global"] {
        sqlx::query(
            "WITH database_window AS (\
               SELECT floor(extract(epoch FROM clock_timestamp()) / 60)::bigint * 60 AS window_start\
             ) \
             INSERT INTO federation_rate_limit_counters \
             (scope, subject, window_start, window_seconds, request_count, expires_at) \
             SELECT $1, 'cell-b', window_start, 60, 5999, to_timestamp(window_start + 120) \
             FROM database_window \
             ON CONFLICT (scope, subject, window_start, window_seconds) \
             DO UPDATE SET request_count = 5999, expires_at = EXCLUDED.expires_at",
        )
        .bind(scope)
        .execute(&pool)
        .await?;
    }
    let app_one = router(shared_one);
    let app_two = router(shared_two);
    let unknown_sender = FederationService::new(
        identity("cell-d", "key-d", 64),
        Arc::new(MemoryFederationRepository::new()),
    );
    let unknown_event = unknown_sender
        .publish_local(PublishRequest {
            actor: "system:postgres-global-rate-proof".to_string(),
            event_type: "object.upserted".to_string(),
            object_address: "wg://cell-d/node/global-rate-proof".to_string(),
            object_kind: "node".to_string(),
            object_version: 1,
            previous_version: None,
            scope: "global".to_string(),
            neighbourhood_targets: vec![],
            payload: json!({"title": "Unknown peer"}),
        })
        .await?;
    assert_eq!(
        post_event(&app_one, &unknown_event).await?.status(),
        StatusCode::ACCEPTED
    );
    let global_limited = post_event(&app_two, &unknown_event).await?;
    assert_eq!(global_limited.status(), StatusCode::TOO_MANY_REQUESTS);
    assert_eq!(
        global_limited
            .headers()
            .get("retry-after")
            .and_then(|value| value.to_str().ok()),
        Some("60")
    );

    assert_eq!(
        get_missing_object(&app_one).await?.status(),
        StatusCode::NOT_FOUND
    );
    let read_limited = get_missing_object(&app_two).await?;
    assert_eq!(read_limited.status(), StatusCode::TOO_MANY_REQUESTS);
    assert_eq!(
        read_limited
            .headers()
            .get("retry-after")
            .and_then(|value| value.to_str().ok()),
        Some("60")
    );

    // Shared-backend failure is fail-closed: it becomes 500 rather than a
    // local-memory bypass of the rate limit.
    pool.close().await;
    assert_eq!(
        post_event(&app_one, &unknown_event).await?.status(),
        StatusCode::INTERNAL_SERVER_ERROR
    );
    Ok(())
}

#[tokio::test]
#[ignore = "requires an isolated FEDERATION_TEST_DATABASE_URL"]
async fn federation_hardening_migration_rejects_legacy_scope_or_audience_drift() -> Result<()> {
    let database_url = env::var("FEDERATION_TEST_DATABASE_URL")
        .context("FEDERATION_TEST_DATABASE_URL must identify an isolated PostgreSQL database")?;
    let pool = PgPoolOptions::new()
        .max_connections(2)
        .connect(&database_url)
        .await?;

    sqlx::raw_sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        .execute(&pool)
        .await?;
    sqlx::raw_sql(include_str!(
        "../migrations/20260720000001_federation_core.up.sql"
    ))
    .execute(&pool)
    .await?;

    sqlx::query(
        "INSERT INTO federation_objects \
         (object_address, origin_cell_id, object_kind, object_version, scope, payload) \
         VALUES ('wg://cell-a/node/legacy-drift', 'cell-a', 'node', 2, 'neighbourhood', '{}'::jsonb)",
    )
    .execute(&pool)
    .await?;
    for (event_id, version, scope, targets) in [
        (
            Uuid::parse_str("00000000-0000-4000-8000-000000000001")?,
            1_i64,
            "global",
            json!([]),
        ),
        (
            Uuid::parse_str("00000000-0000-4000-8000-000000000002")?,
            2_i64,
            "neighbourhood",
            json!(["aab", "a-b"]),
        ),
    ] {
        let envelope = json!({
            "scope": scope,
            "neighbourhood_targets": targets,
        });
        sqlx::query(
            "INSERT INTO federation_inbox \
             (event_id, origin_cell_id, object_address, object_version, event_type, schema_version, scope, envelope_sha256, envelope) \
             VALUES ($1, 'cell-a', 'wg://cell-a/node/legacy-drift', $2, 'object.upserted', 1, $3, $4, $5)",
        )
        .bind(event_id)
        .bind(version)
        .bind(scope)
        .bind("0".repeat(64))
        .bind(envelope)
        .execute(&pool)
        .await?;
    }

    sqlx::raw_sql(include_str!(
        "../migrations/20260722000004_federation_core_hardening.up.sql"
    ))
    .execute(&pool)
    .await?;

    let migration_error = sqlx::raw_sql(include_str!(
        "../migrations/20260722000006_federation_core_post_merge_hardening.up.sql"
    ))
    .execute(&pool)
    .await
    .expect_err("legacy scope/audience drift must fail the additive hardening migration closed");
    assert!(migration_error.to_string().contains(
        "legacy federation object history changed immutable scope or neighbourhood audience"
    ));

    // This test deliberately installs only a migration subset. Restore the disposable
    // database so other ignored federation persistence proofs can share this test binary.
    sqlx::raw_sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        .execute(&pool)
        .await?;

    Ok(())
}
