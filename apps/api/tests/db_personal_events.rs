//! P3a: real PostgreSQL + handler proof for account-private circle messages.
mod support;

use std::sync::Arc;

use axum::{
    body,
    extract::{Path, Query, State},
    http::StatusCode,
    Extension,
};
use serde_json::{json, Value};
use sqlx::{postgres::PgPoolOptions, PgPool};
use tokio::sync::RwLock;
use uuid::Uuid;
use weltgewebe_api::{
    auth::{
        accounts::AccountStore, rate_limit::AuthRateLimiter, role::Role, session::SessionBackend,
    },
    config::{
        AppConfig, DomainAccountWriteSource, DomainEdgeWriteSource, DomainNodeWriteSource,
        DomainReadSource, PasskeyCredentialSource,
    },
    middleware::auth::AuthContext,
    routes::{
        circles::{
            accept_invitation, leave_circle, withdraw_invitation, InvitationRevisionQuery,
            LeaveCircleQuery,
        },
        personal_events::{list_personal_events, mark_personal_event_read, PersonalEventPageQuery},
    },
    state::{ApiState, OrderedCache},
    telemetry::{BuildInfo, Metrics},
};

fn app_state(pool: PgPool) -> ApiState {
    let config = AppConfig {
        max_guest_owned_nodes: 1_000,
        domain_read_source: DomainReadSource::Postgres,
        domain_account_write_source: DomainAccountWriteSource::Postgres,
        domain_node_write_source: DomainNodeWriteSource::Postgres,
        domain_edge_write_source: DomainEdgeWriteSource::Postgres,
        passkey_credential_source: PasskeyCredentialSource::InMemory,
        auth_public_login: false,
        auth_cookie_secure: true,
        app_base_url: None,
        auth_trusted_proxies: None,
        auth_allow_emails: None,
        auth_allow_email_domains: None,
        auth_auto_provision: false,
        auth_auto_provision_role: weltgewebe_api::config::AutoProvisionRole::Gast,
        auth_rl_ip_per_min: None,
        auth_rl_ip_per_hour: None,
        auth_rl_email_per_min: None,
        auth_rl_email_per_hour: None,
        search_rl_ip_per_min: None,
        search_rl_ip_per_hour: None,
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
    ApiState {
        db_pool: Some(pool),
        db_pool_configured: true,
        nats_client: None,
        nats_configured: false,
        metrics: Metrics::try_new(BuildInfo {
            version: "personal-events-proof",
            commit: "personal-events-proof",
            build_timestamp: "personal-events-proof",
        })
        .unwrap(),
        rate_limiter: Arc::new(AuthRateLimiter::new(&config)),
        config,
        sessions: SessionBackend::new_in_memory(),
        challenges: Default::default(),
        tokens: weltgewebe_api::auth::tokens::TokenStore::new(),
        step_up_tokens: weltgewebe_api::auth::step_up_tokens::StepUpTokenStore::new(),
        accounts: Arc::new(RwLock::new(AccountStore::new())),
        nodes: Arc::new(RwLock::new(OrderedCache::new())),
        nodes_persist: Arc::new(tokio::sync::Mutex::new(())),
        accounts_persist: Arc::new(tokio::sync::Mutex::new(())),
        domain_projection_gate: Arc::new(RwLock::new(())),
        domain_projection_reload: Arc::new(tokio::sync::Mutex::new(())),
        domain_projection_local_node_patch_handoff: Arc::new(std::sync::atomic::AtomicI64::new(-1)),
        domain_projection_version: Arc::new(std::sync::atomic::AtomicI64::new(0)),
        edges: Arc::new(RwLock::new(OrderedCache::new())),
        mailer: None,
        webauthn: None,
        passkey_registrations: Default::default(),
        passkey_registration_grants: Default::default(),
        passkey_authentications: Default::default(),
        passkeys: Default::default(),
        web_push: None,
    }
}

fn actor(id: &str) -> AuthContext {
    AuthContext {
        authenticated: true,
        account_id: Some(id.to_owned()),
        device_id: None,
        role: Role::Weber,
        expires_at: None,
    }
}

async fn page(pool: &PgPool, user: &str) -> Value {
    let query: PersonalEventPageQuery = serde_json::from_value(json!({})).unwrap();
    let response = list_personal_events(
        State(app_state(pool.clone())),
        Extension(actor(user)),
        Query(query),
    )
    .await
    .unwrap();
    assert_eq!(
        response.headers().get("cache-control").unwrap(),
        "private, no-store"
    );
    let bytes = body::to_bytes(response.into_body(), 1024 * 1024)
        .await
        .unwrap();
    serde_json::from_slice(&bytes).unwrap()
}

async fn kinds(pool: &PgPool, owner: &str) -> Vec<String> {
    sqlx::query_scalar(
        "SELECT e.event_kind FROM domain_conversations inbox
         JOIN domain_messages m ON m.conversation_id=inbox.id
         JOIN domain_personal_event_messages e ON e.message_id=m.id
         WHERE inbox.conversation_type='account_event'
           AND inbox.account_event_owner_id=$1
         ORDER BY m.created_at, m.id",
    )
    .bind(owner)
    .fetch_all(pool)
    .await
    .unwrap()
}

#[tokio::test]
#[ignore = "requires DATABASE_URL for a disposable direct PostgreSQL database"]
async fn circle_notifications_are_account_private_atomic_and_nonduplicating() {
    let url = support::postgres_proof::validated_direct_disposable_url(
        std::env::var("DATABASE_URL").expect("disposable PostgreSQL URL"),
    );
    let pool = PgPoolOptions::new()
        .max_connections(5)
        .connect(&url)
        .await
        .expect("connect PostgreSQL");
    sqlx::migrate!("./migrations").run(&pool).await.unwrap();

    let token = Uuid::new_v4();
    let a = format!("personal-owner-{token}");
    let b = format!("personal-invitee-{token}");
    let stranger = format!("personal-stranger-{token}");
    for id in [&a, &b, &stranger] {
        sqlx::query(
            "INSERT INTO domain_accounts(id,title,kind,location_lat,location_lon)
             VALUES($1,$1,'garnrolle',53.555,10.055)",
        )
        .bind(id)
        .execute(&pool)
        .await
        .unwrap();
    }
    let circle = Uuid::new_v4();
    sqlx::query(
        "INSERT INTO domain_audience_circles(id,kind,name,created_by_account_id)
         VALUES($1,'household','Hidden House',$2)",
    )
    .bind(circle)
    .bind(&a)
    .execute(&pool)
    .await
    .unwrap();
    sqlx::query(
        "INSERT INTO domain_audience_circle_members
           (circle_id,account_id,status,invited_by_account_id,joined_at)
         VALUES ($1,$2,'active',$2,NOW()),($1,$3,'invited',$2,NULL)",
    )
    .bind(circle)
    .bind(&a)
    .bind(&b)
    .execute(&pool)
    .await
    .unwrap();

    let initial = page(&pool, &b).await;
    assert_eq!(initial["unread_count"], 1);
    assert_eq!(initial["items"][0]["event_kind"], "circle_invited");
    assert_eq!(initial["items"][0]["actionable"], true);
    assert_eq!(initial["items"][0]["circle_name"], "Hidden House");
    assert_eq!(kinds(&pool, &a).await, vec!["circle_invitation_sent"]);
    assert_eq!(page(&pool, &a).await["unread_count"], 0);

    // HTTP retries that do not change membership must not yield a new message
    // or another transactional domain.message.created outbox record.
    sqlx::query(
        "INSERT INTO domain_audience_circle_members(
            circle_id,account_id,status,invited_by_account_id)
         VALUES($1,$2,'invited',$3) ON CONFLICT(circle_id,account_id) DO NOTHING",
    )
    .bind(circle)
    .bind(&b)
    .bind(&a)
    .execute(&pool)
    .await
    .unwrap();
    assert_eq!(kinds(&pool, &b).await, vec!["circle_invited"]);

    let incoming_id = Uuid::parse_str(initial["items"][0]["id"].as_str().unwrap()).unwrap();
    let initial_invitation_created_at = initial["items"][0]["invitation_created_at"]
        .as_str()
        .unwrap()
        .to_owned();
    let rejected = mark_personal_event_read(
        State(app_state(pool.clone())),
        Extension(actor(&stranger)),
        Path(incoming_id),
    )
    .await;
    assert_eq!(rejected.unwrap_err().0, StatusCode::NOT_FOUND);
    mark_personal_event_read(
        State(app_state(pool.clone())),
        Extension(actor(&b)),
        Path(incoming_id),
    )
    .await
    .unwrap();
    assert_eq!(page(&pool, &b).await["unread_count"], 0);
    assert!(page(&pool, &stranger).await["items"]
        .as_array()
        .unwrap()
        .is_empty());

    sqlx::query(
        "UPDATE domain_audience_circle_members SET status='active', joined_at=NOW()
         WHERE circle_id=$1 AND account_id=$2 AND status='invited'",
    )
    .bind(circle)
    .bind(&b)
    .execute(&pool)
    .await
    .unwrap();
    sqlx::query("UPDATE domain_audience_circles SET state='active' WHERE id=$1")
        .bind(circle)
        .execute(&pool)
        .await
        .unwrap();
    assert!(kinds(&pool, &a).await.contains(&"circle_joined".to_owned()));
    assert_eq!(page(&pool, &b).await["items"][0]["actionable"], false);

    // P0 counterexample: invitation is accepted in tab 2, while a stale tab
    // still displays "Ablehnen". Status-CAS must prevent accidental departure.
    let query: LeaveCircleQuery =
        serde_json::from_value(json!({"expected_status":"invited"})).unwrap();
    let obsolete_decline = leave_circle(
        State(app_state(pool.clone())),
        Extension(actor(&b)),
        Path(circle),
        Query(query),
    )
    .await;
    assert_eq!(obsolete_decline.unwrap_err().0, StatusCode::CONFLICT);
    let still_active: String = sqlx::query_scalar(
        "SELECT status FROM domain_audience_circle_members
         WHERE circle_id=$1 AND account_id=$2",
    )
    .bind(circle)
    .bind(&b)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(
        still_active, "active",
        "old decline must not revoke accepted membership"
    );

    // Leaving must withdraw the prior circle ID/name/action even though a
    // neutral, historically correct personal message remains in the inbox.
    sqlx::query(
        "DELETE FROM domain_audience_circle_members
         WHERE circle_id=$1 AND account_id=$2",
    )
    .bind(circle)
    .bind(&b)
    .execute(&pool)
    .await
    .unwrap();
    assert!(kinds(&pool, &a).await.contains(&"circle_left".to_owned()));
    let former = page(&pool, &b).await;
    assert!(former["items"][0]["circle_id"].is_null());
    assert!(former["items"][0]["circle_name"].is_null());
    assert_eq!(former["items"][0]["actionable"], false);

    // A new invitation is a real new transition; withdrawing it is not an
    // invitee decline and preserves the sender's right to read their history.
    sqlx::query(
        "INSERT INTO domain_audience_circle_members(
          circle_id,account_id,status,invited_by_account_id)
         VALUES($1,$2,'invited',$3)",
    )
    .bind(circle)
    .bind(&b)
    .bind(&a)
    .execute(&pool)
    .await
    .unwrap();
    // Old invitations must never become actionable after re-inviting the
    // same person into the same community in a later transaction.
    let reinvited = page(&pool, &b).await;
    let items = reinvited["items"].as_array().unwrap();
    let prior = items
        .iter()
        .find(|item| item["id"].as_str() == Some(incoming_id.to_string().as_str()))
        .expect("original invitation remains historical mail");
    assert_eq!(
        prior["actionable"], false,
        "old invitation became actionable"
    );
    let current = items
        .iter()
        .find(|item| {
            item["event_kind"] == "circle_invited"
                && item["id"].as_str() != Some(incoming_id.to_string().as_str())
        })
        .expect("new invitation exists");
    assert_eq!(current["actionable"], true);
    let from_a = page(&pool, &a).await;
    let sent: Vec<_> = from_a["items"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|item| item["event_kind"] == "circle_invitation_sent")
        .collect();
    assert_eq!(sent.len(), 2);
    assert_eq!(
        sent.iter()
            .filter(|item| item["actionable"] == true)
            .count(),
        1
    );

    // A stale tab must not reject, accept, or withdraw a *new* invitation
    // after the old one was accepted and the member later re-invited.
    // Status-only CAS would mistakenly affect this newer pending row.
    let old_round: LeaveCircleQuery = serde_json::from_value(json!({
        "expected_status": "invited",
        "expected_invitation_created_at": initial_invitation_created_at
    }))
    .unwrap();
    let stale_decline = leave_circle(
        State(app_state(pool.clone())),
        Extension(actor(&b)),
        Path(circle),
        Query(old_round),
    )
    .await;
    assert_eq!(stale_decline.unwrap_err().0, StatusCode::CONFLICT);

    let old_accept: InvitationRevisionQuery = serde_json::from_value(json!({
        "expected_invitation_created_at": initial_invitation_created_at
    }))
    .unwrap();
    let stale_accept = accept_invitation(
        State(app_state(pool.clone())),
        Extension(actor(&b)),
        Path(circle),
        Query(old_accept),
    )
    .await;
    assert_eq!(stale_accept.unwrap_err().0, StatusCode::CONFLICT);

    let old_withdraw: InvitationRevisionQuery = serde_json::from_value(json!({
        "expected_invitation_created_at": initial_invitation_created_at
    }))
    .unwrap();
    let stale_withdraw = withdraw_invitation(
        State(app_state(pool.clone())),
        Extension(actor(&a)),
        Path((circle, b.clone())),
        Query(old_withdraw),
    )
    .await;
    assert_eq!(stale_withdraw.unwrap_err().0, StatusCode::NOT_FOUND);
    let still_invited: String = sqlx::query_scalar(
        "SELECT status FROM domain_audience_circle_members
         WHERE circle_id=$1 AND account_id=$2",
    )
    .bind(circle)
    .bind(&b)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(
        still_invited, "invited",
        "new invite must survive all stale actions"
    );

    let mut tx = pool.begin().await.unwrap();
    sqlx::query("SELECT set_config('weltgewebe.circle_invitation_withdrawal','yes',true)")
        .fetch_one(&mut *tx)
        .await
        .unwrap();
    sqlx::query(
        "DELETE FROM domain_audience_circle_members
         WHERE circle_id=$1 AND account_id=$2 AND status='invited'",
    )
    .bind(circle)
    .bind(&b)
    .execute(&mut *tx)
    .await
    .unwrap();
    tx.commit().await.unwrap();
    let b_kinds = kinds(&pool, &b).await;
    assert_eq!(
        b_kinds
            .iter()
            .filter(|kind| *kind == "circle_invited")
            .count(),
        2
    );
    assert!(b_kinds.contains(&"circle_withdrawn".to_owned()));
    assert!(!kinds(&pool, &a)
        .await
        .contains(&"circle_declined".to_owned()));

    // All emitted messages reuse the canonical outbox transaction.
    let outbox: i64 = sqlx::query_scalar(
        "SELECT COUNT(*)::bigint FROM domain_outbox
         WHERE event_type='domain.message.created'
         AND aggregate_id IN (
             SELECT m.id::text FROM domain_messages m
             JOIN domain_conversations inbox ON inbox.id=m.conversation_id
             WHERE inbox.conversation_type='account_event'
               AND inbox.account_event_owner_id=$1
         )",
    )
    .bind(&b)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(outbox, b_kinds.len() as i64);

    // A fresh pending invitation can still be explicitly declined with the
    // same status-CAS. This must notify A as declined, not as withdrawn.
    sqlx::query(
        "INSERT INTO domain_audience_circle_members
         (circle_id,account_id,status,invited_by_account_id)
         VALUES($1,$2,'invited',$3)",
    )
    .bind(circle)
    .bind(&b)
    .bind(&a)
    .execute(&pool)
    .await
    .unwrap();
    let current_page = page(&pool, &b).await;
    let current_invitation_created_at = current_page["items"]
        .as_array()
        .unwrap()
        .iter()
        .find(|item| item["event_kind"] == "circle_invited" && item["actionable"] == true)
        .unwrap()["invitation_created_at"]
        .as_str()
        .unwrap()
        .to_owned();
    let valid_decline: LeaveCircleQuery = serde_json::from_value(json!({
        "expected_status": "invited",
        "expected_invitation_created_at": current_invitation_created_at
    }))
    .unwrap();
    let declined = leave_circle(
        State(app_state(pool.clone())),
        Extension(actor(&b)),
        Path(circle),
        Query(valid_decline),
    )
    .await;
    assert!(
        declined.is_ok(),
        "a still-pending invitation must be rejectable"
    );
    let still_member: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM domain_audience_circle_members
         WHERE circle_id=$1 AND account_id=$2)",
    )
    .bind(circle)
    .bind(&b)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert!(!still_member);
    assert!(kinds(&pool, &a)
        .await
        .contains(&"circle_declined".to_owned()));

    // No test resources survive the disposable scenario.
    sqlx::query("DELETE FROM domain_audience_circles WHERE id=$1")
        .bind(circle)
        .execute(&pool)
        .await
        .unwrap();
    for id in [&a, &b, &stranger] {
        sqlx::query("DELETE FROM domain_accounts WHERE id=$1")
            .bind(id)
            .execute(&pool)
            .await
            .unwrap();
    }
}

#[tokio::test]
#[ignore = "requires DATABASE_URL for a disposable direct PostgreSQL database"]
async fn historical_pending_invitations_are_backfilled_once_on_activation() {
    let url = support::postgres_proof::validated_direct_disposable_url(
        std::env::var("DATABASE_URL").expect("disposable PostgreSQL URL"),
    );
    let pool = PgPoolOptions::new()
        .max_connections(5)
        .connect(&url)
        .await
        .expect("connect disposable PostgreSQL");
    sqlx::migrate!("./migrations").run(&pool).await.unwrap();

    let token = Uuid::new_v4();
    let founder = format!("personal-backfill-founder-{token}");
    let invitee = format!("personal-backfill-invitee-{token}");
    for id in [&founder, &invitee] {
        sqlx::query(
            "INSERT INTO domain_accounts(id,title,kind,location_lat,location_lon)
             VALUES($1,$1,'garnrolle',53.555,10.055)",
        )
        .bind(id)
        .execute(&pool)
        .await
        .unwrap();
    }
    let circle = Uuid::new_v4();
    sqlx::query(
        "INSERT INTO domain_audience_circles(id,kind,name,created_by_account_id)
         VALUES($1,'household','Historical test house',$2)",
    )
    .bind(circle)
    .bind(&founder)
    .execute(&pool)
    .await
    .unwrap();

    // Simulate an invitation written by the previous release, before the
    // personal event trigger existed. This is a disposable test database only.
    sqlx::query(
        "ALTER TABLE domain_audience_circle_members
         DISABLE TRIGGER audience_circle_personal_events",
    )
    .execute(&pool)
    .await
    .unwrap();
    sqlx::query(
        "INSERT INTO domain_audience_circle_members
           (circle_id,account_id,status,invited_by_account_id,joined_at)
         VALUES ($1,$2,'active',$2,NOW()),($1,$3,'invited',$2,NULL)",
    )
    .bind(circle)
    .bind(&founder)
    .bind(&invitee)
    .execute(&pool)
    .await
    .unwrap();
    sqlx::query(
        "ALTER TABLE domain_audience_circle_members
         ENABLE TRIGGER audience_circle_personal_events",
    )
    .execute(&pool)
    .await
    .unwrap();
    assert!(kinds(&pool, &invitee).await.is_empty());
    assert!(kinds(&pool, &founder).await.is_empty());

    // Execute the exact DO block from the migration, not a separate re-
    // implementation that could pass while the real migration is defective.
    let migration = include_str!("../migrations/20261112000001_household_personal_events.up.sql");
    let block_start = migration
        .rfind("\nDO $$\n")
        .expect("migration has one-time backfill DO");
    sqlx::raw_sql(&migration[block_start..])
        .execute(&pool)
        .await
        .expect("reconcile earlier pending invitations");
    assert_eq!(kinds(&pool, &invitee).await, vec!["circle_invited"]);
    assert_eq!(kinds(&pool, &founder).await, vec!["circle_invitation_sent"]);
    let incoming = page(&pool, &invitee).await;
    assert_eq!(incoming["unread_count"], 1);
    assert_eq!(incoming["items"][0]["actionable"], true);
    assert_eq!(page(&pool, &founder).await["unread_count"], 0);

    sqlx::query("DELETE FROM domain_audience_circles WHERE id=$1")
        .bind(circle)
        .execute(&pool)
        .await
        .unwrap();
    for id in [&founder, &invitee] {
        sqlx::query("DELETE FROM domain_accounts WHERE id=$1")
            .bind(id)
            .execute(&pool)
            .await
            .unwrap();
    }
}
