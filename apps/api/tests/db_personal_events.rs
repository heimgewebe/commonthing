//! P3a: real PostgreSQL + handler proof for account-private circle messages.
mod support;

use std::{net::SocketAddr, sync::Arc};

use axum::{
    body::{self, Body},
    extract::{connect_info::MockConnectInfo, Path, Query, State},
    http::{header, Method, Request, StatusCode},
    middleware::from_fn_with_state,
    Extension, Router,
};
use serde_json::{json, Value};
use serial_test::serial;
use sqlx::{postgres::PgPoolOptions, PgPool};
use tokio::sync::RwLock;
use tower::ServiceExt;
use uuid::Uuid;
use weltgewebe_api::{
    auth::{
        accounts::AccountStore, rate_limit::AuthRateLimiter, role::Role, session::SessionBackend,
        session_db::DbSessionStore,
    },
    config::{
        AppConfig, DomainAccountWriteSource, DomainEdgeWriteSource, DomainNodeWriteSource,
        DomainReadSource, PasskeyCredentialSource,
    },
    domain_db::load_accounts_from_postgres,
    middleware::{
        auth::{auth_middleware, AuthContext},
        csrf::require_csrf,
        domain_projection::ensure_current_domain_projection,
    },
    routes::{
        api_router,
        circles::{
            accept_invitation, leave_circle, withdraw_invitation, InvitationRevisionQuery,
            LeaveCircleQuery,
        },
        personal_events::{list_personal_events, mark_personal_event_read, PersonalEventPageQuery},
    },
    state::{ApiState, OrderedCache},
    telemetry::{BuildInfo, Metrics},
    test_helpers::EnvGuard,
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

    // Account retirement must not fail if an active or pending member is
    // deleted while other circle members remain. PostgreSQL's FK cascades
    // the membership and must preserve only neutral system notifications.
    sqlx::query(
        "INSERT INTO domain_audience_circle_members
         (circle_id, account_id, status, invited_by_account_id, joined_at)
         VALUES ($1, $2, 'active', $3, NOW())",
    )
    .bind(circle)
    .bind(&stranger)
    .bind(&a)
    .execute(&pool)
    .await
    .unwrap();
    sqlx::query("DELETE FROM domain_accounts WHERE id=$1")
        .bind(&stranger)
        .execute(&pool)
        .await
        .expect("deleting a member may not fail on personal event subject FK");
    let departed_subject_retired: bool = sqlx::query_scalar(
        "SELECT EXISTS (
           SELECT 1 FROM domain_personal_event_messages e
           JOIN domain_messages m ON m.id=e.message_id
           JOIN domain_conversations inbox ON inbox.id=m.conversation_id
           WHERE inbox.account_event_owner_id=$1 AND e.event_kind='circle_left'
             AND e.subject_account_id IS NULL
        )",
    )
    .bind(&a)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert!(
        departed_subject_retired,
        "account deletion must leave neutral historical mail"
    );

    sqlx::query(
        "INSERT INTO domain_audience_circle_members
         (circle_id, account_id, status, invited_by_account_id)
         VALUES ($1, $2, 'invited', $3)",
    )
    .bind(circle)
    .bind(&b)
    .bind(&a)
    .execute(&pool)
    .await
    .unwrap();
    sqlx::query("DELETE FROM domain_accounts WHERE id=$1")
        .bind(&b)
        .execute(&pool)
        .await
        .expect("deleting an invitee may not abort the inviter's mailbox");
    let declined_subject_retired: bool = sqlx::query_scalar(
        "SELECT EXISTS (
           SELECT 1 FROM domain_personal_event_messages e
           JOIN domain_messages m ON m.id=e.message_id
           JOIN domain_conversations inbox ON inbox.id=m.conversation_id
           WHERE inbox.account_event_owner_id=$1 AND e.event_kind='circle_declined'
             AND e.subject_account_id IS NULL
        )",
    )
    .bind(&a)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert!(
        declined_subject_retired,
        "retiring an invitee must not preserve a broken subject FK"
    );

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
async fn invited_member_waits_on_acl_generation_before_locking_a_mailbox() {
    let url = support::postgres_proof::validated_direct_disposable_url(
        std::env::var("DATABASE_URL").expect("disposable PostgreSQL URL"),
    );
    let pool = PgPoolOptions::new()
        .max_connections(8)
        .connect(&url)
        .await
        .expect("connect PostgreSQL");
    sqlx::migrate!("./migrations").run(&pool).await.unwrap();

    let tag = Uuid::new_v4();
    let founder = format!("p3a-fence-founder-{tag}");
    let invitee = format!("p3a-fence-invitee-{tag}");
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
         VALUES($1,'household','Fence order proof',$2)",
    )
    .bind(circle)
    .bind(&founder)
    .execute(&pool)
    .await
    .unwrap();
    sqlx::query(
        "INSERT INTO domain_audience_circle_members
         (circle_id,account_id,status,invited_by_account_id,joined_at)
         VALUES($1,$2,'active',$2,NOW())",
    )
    .bind(circle)
    .bind(&founder)
    .execute(&pool)
    .await
    .unwrap();

    // Pre-create the inboxes: if an invitation locks one prematurely,
    // SELECT FOR UPDATE NOWAIT below detects that actual row lock. A newly
    // inserted (uncommitted) inbox would otherwise be invisible to the probe.
    for owner in [&founder, &invitee] {
        sqlx::query(
            "INSERT INTO domain_conversations
             (id,conversation_type,visibility,account_event_owner_id)
             VALUES($1,'account_event','participants',$2)",
        )
        .bind(Uuid::new_v4())
        .bind(owner)
        .execute(&pool)
        .await
        .unwrap();
    }

    let mut generation_holder = pool.begin().await.unwrap();
    sqlx::query(
        "UPDATE domain_projection_state
         SET version=version+1 WHERE singleton=TRUE",
    )
    .execute(&mut *generation_holder)
    .await
    .unwrap();

    // A live invitation is now forced to wait for that generation row.
    // Before the P2 trigger-order fix it grabs both mailbox locks *first*,
    // making another transaction that holds generation and needs an inbox
    // deadlock. After the fix, no mailbox can be locked before generation.
    let mut pending_invitation = pool.begin().await.unwrap();
    let app_name = format!("p3a-lock-order-{}", Uuid::new_v4().simple());
    sqlx::query("SELECT set_config('application_name', $1, true)")
        .bind(&app_name)
        .execute(&mut *pending_invitation)
        .await
        .unwrap();
    let founder_for_job = founder.clone();
    let invitee_for_job = invitee.clone();
    let pending_job = tokio::spawn(async move {
        sqlx::query(
            "INSERT INTO domain_audience_circle_members
             (circle_id,account_id,status,invited_by_account_id)
             VALUES($1,$2,'invited',$3)",
        )
        .bind(circle)
        .bind(&invitee_for_job)
        .bind(&founder_for_job)
        .execute(&mut *pending_invitation)
        .await?;
        pending_invitation.commit().await
    });

    tokio::time::timeout(std::time::Duration::from_secs(5), async {
        loop {
            let waiting: bool = sqlx::query_scalar(
                "SELECT EXISTS(
                   SELECT 1 FROM pg_stat_activity
                   WHERE application_name=$1 AND wait_event_type='Lock'
                 )",
            )
            .bind(&app_name)
            .fetch_one(&pool)
            .await
            .unwrap();
            if waiting {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(25)).await;
        }
    })
    .await
    .expect("pending invitation must reach the locked generation fence");

    let mut mailbox_probe = pool.begin().await.unwrap();
    let _: Uuid = sqlx::query_scalar(
        "SELECT id FROM domain_conversations
         WHERE conversation_type='account_event' AND account_event_owner_id=$1
         FOR UPDATE NOWAIT",
    )
    .bind(&invitee)
    .fetch_one(&mut *mailbox_probe)
    .await
    .expect("an invitation waiting on generation must not already own its mailbox");
    mailbox_probe.rollback().await.unwrap();

    generation_holder.rollback().await.unwrap();
    tokio::time::timeout(std::time::Duration::from_secs(5), pending_job)
        .await
        .expect("invitation must finish once generation fence is released")
        .expect("invitation task must not panic")
        .expect("invitation must commit without deadlock");

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

#[tokio::test]
#[ignore = "requires DATABASE_URL for a disposable direct PostgreSQL database"]
async fn opposite_direction_invitations_reuse_two_mailboxes_without_deadlock() {
    let url = support::postgres_proof::validated_direct_disposable_url(
        std::env::var("DATABASE_URL").expect("disposable PostgreSQL URL"),
    );
    let pool = PgPoolOptions::new()
        .max_connections(8)
        .connect(&url)
        .await
        .expect("connect PostgreSQL");
    sqlx::migrate!("./migrations").run(&pool).await.unwrap();

    // All row mutations must acquire the global ACL generation fence before
    // any account-owned mailbox lock, including mixed founder/invitee INSERTs.
    // PostgreSQL fires AFTER triggers on the same event in name order.
    let ordered_triggers: Vec<String> = sqlx::query_scalar(
        "SELECT tgname::text FROM pg_trigger
         WHERE tgrelid='domain_audience_circle_members'::regclass
           AND tgname IN ('circle_members_acl_generation', 'circle_members_personal_events')
         ORDER BY tgname",
    )
    .fetch_all(&pool)
    .await
    .unwrap();
    assert_eq!(
        ordered_triggers,
        [
            "circle_members_acl_generation",
            "circle_members_personal_events"
        ],
        "ACL generation must be locked before personal mailbox delivery"
    );

    let tag = Uuid::new_v4();
    let a = format!("p3a-cross-a-{tag}");
    let b = format!("p3a-cross-b-{tag}");
    for id in [&a, &b] {
        sqlx::query(
            "INSERT INTO domain_accounts(id,title,kind,location_lat,location_lon)
             VALUES($1,$1,'garnrolle',53.555,10.055)",
        )
        .bind(id)
        .execute(&pool)
        .await
        .unwrap();
    }
    let house_a = Uuid::new_v4();
    let house_b = Uuid::new_v4();
    for (circle, founder) in [(house_a, &a), (house_b, &b)] {
        sqlx::query(
            "INSERT INTO domain_audience_circles(id,kind,name,created_by_account_id)
             VALUES($1,'household','Cross invite test',$2)",
        )
        .bind(circle)
        .bind(founder)
        .execute(&pool)
        .await
        .unwrap();
        sqlx::query(
            "INSERT INTO domain_audience_circle_members
             (circle_id,account_id,status,invited_by_account_id,joined_at)
             VALUES($1,$2,'active',$2,NOW())",
        )
        .bind(circle)
        .bind(founder)
        .execute(&pool)
        .await
        .unwrap();
    }
    // Both requests touch the *same two mailboxes* but in reverse
    // invitee/inviter order. Each transition must commit atomically with
    // both personal messages rather than PostgreSQL cancelling one as deadlocked.
    let (left, right) = tokio::time::timeout(std::time::Duration::from_secs(10), async {
        tokio::join!(
            sqlx::query(
                "INSERT INTO domain_audience_circle_members
                     (circle_id,account_id,status,invited_by_account_id)
                     VALUES($1,$2,'invited',$3)"
            )
            .bind(house_a)
            .bind(&b)
            .bind(&a)
            .execute(&pool),
            sqlx::query(
                "INSERT INTO domain_audience_circle_members
                     (circle_id,account_id,status,invited_by_account_id)
                     VALUES($1,$2,'invited',$3)"
            )
            .bind(house_b)
            .bind(&a)
            .bind(&b)
            .execute(&pool),
        )
    })
    .await
    .expect("cross-circle invitations must not hang");
    left.expect("A invites B without SQL deadlock");
    right.expect("B invites A without SQL deadlock");
    for owner in [&a, &b] {
        let actual = kinds(&pool, owner).await;
        assert!(actual.contains(&"circle_invited".to_owned()));
        assert!(actual.contains(&"circle_invitation_sent".to_owned()));
    }

    for circle in [house_a, house_b] {
        sqlx::query("DELETE FROM domain_audience_circles WHERE id=$1")
            .bind(circle)
            .execute(&pool)
            .await
            .unwrap();
    }
    for id in [&a, &b] {
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
         DISABLE TRIGGER circle_members_personal_events",
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
         ENABLE TRIGGER circle_members_personal_events",
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

/// One disposable, production-middleware HTTP proof: four distinct cookie
/// sessions share canonical PostgreSQL, not mocked request AuthContexts.
/// Browser rendering and live production A/B acceptance remain separate.
fn personal_events_session_router(state: ApiState) -> Router {
    Router::new()
        .merge(
            api_router()
                .layer(from_fn_with_state(
                    state.clone(),
                    ensure_current_domain_projection,
                ))
                .route_layer(from_fn_with_state(state.clone(), auth_middleware))
                .layer(axum::middleware::from_fn(require_csrf)),
        )
        .layer(MockConnectInfo(SocketAddr::from(([127, 0, 0, 1], 18081))))
        .with_state(state)
}

async fn personal_events_http(
    app: &Router,
    method: Method,
    path: &str,
    cookie: Option<&str>,
    payload: Option<Value>,
    with_origin: bool,
) -> (StatusCode, String) {
    let mut request = Request::builder()
        .method(method)
        .uri(path)
        .header(header::HOST, "localhost");
    if with_origin {
        request = request.header(header::ORIGIN, "http://localhost");
    }
    if let Some(cookie) = cookie {
        request = request.header(header::COOKIE, cookie);
    }
    let body = if let Some(value) = payload {
        request = request.header(header::CONTENT_TYPE, "application/json");
        Body::from(value.to_string())
    } else {
        Body::empty()
    };
    let response = app
        .clone()
        .oneshot(request.body(body).expect("P3a HTTP request"))
        .await
        .expect("P3a HTTP response");
    let status = response.status();
    let bytes = body::to_bytes(response.into_body(), 1024 * 1024)
        .await
        .expect("bounded P3a HTTP response");
    (status, String::from_utf8_lossy(&bytes).into_owned())
}

async fn personal_events_login(app: &Router, account_id: &str) -> String {
    let request = Request::post("/auth/dev/login")
        .header(header::HOST, "localhost")
        .header(header::ORIGIN, "http://localhost")
        .header(header::CONTENT_TYPE, "application/json")
        .body(Body::from(json!({"account_id": account_id}).to_string()))
        .expect("local-only test login");
    let response = app.clone().oneshot(request).await.expect("login response");
    assert_eq!(response.status(), StatusCode::OK, "disposable dev login");
    response
        .headers()
        .get(header::SET_COOKIE)
        .expect("real session Set-Cookie")
        .to_str()
        .expect("ASCII session cookie")
        .split(';')
        .next()
        .expect("session cookie pair")
        .to_owned()
}

async fn personal_events_inbox(app: &Router, cookie: &str) -> Value {
    let (status, response) = personal_events_http(
        app,
        Method::GET,
        "/personal-events",
        Some(cookie),
        None,
        true,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{response}");
    let page: Value = serde_json::from_str(&response).expect("personal inbox JSON");
    support::domain_contract::assert_valid(
        &support::domain_contract::load("personal-event"),
        "#/definitions/event_page",
        &page,
        "GET /personal-events",
    );
    page
}

/// Validates one real HTTP response body against a definition of a domain
/// contract, so the router output itself is checked, not a hand-built struct.
fn assert_contract(schema: &str, pointer: &str, body: &str, context: &str) -> Value {
    let value: Value = serde_json::from_str(body).unwrap_or_else(|e| panic!("{context}: {e}"));
    support::domain_contract::assert_valid(
        &support::domain_contract::load(schema),
        pointer,
        &value,
        context,
    );
    value
}

#[tokio::test]
#[serial]
#[ignore = "requires DATABASE_URL for a disposable direct PostgreSQL database"]
async fn independent_http_cookies_deliver_private_circle_invites_answers_and_read_receipts() {
    let _dev_login = EnvGuard::set("AUTH_DEV_LOGIN", "1");
    let _remote_login = EnvGuard::unset("AUTH_DEV_LOGIN_ALLOW_REMOTE");
    let _csrf_override = EnvGuard::unset("CSRF_ALLOWED_ORIGINS");
    let url = support::postgres_proof::validated_direct_disposable_url(
        std::env::var("DATABASE_URL").expect("disposable PostgreSQL URL"),
    );
    let pool = PgPoolOptions::new()
        .max_connections(8)
        .connect(&url)
        .await
        .expect("connect disposable PostgreSQL");
    sqlx::migrate!("./migrations").run(&pool).await.unwrap();

    let suffix = Uuid::new_v4().simple();
    let a = format!("p3a-cookie-founder-{suffix}");
    let b = format!("p3a-cookie-invitee-{suffix}");
    let c = format!("p3a-cookie-decliner-{suffix}");
    let guest = format!("p3a-cookie-guest-{suffix}");
    for (id, role) in [
        (&a, "weber"),
        (&b, "weber"),
        (&c, "weber"),
        (&guest, "gast"),
    ] {
        sqlx::query(
            "INSERT INTO domain_accounts(id,title,kind,role,map_state,location_lat,location_lon)
             VALUES($1,$1,'garnrolle',$2,'not_on_map',53.555,10.055)",
        )
        .bind(id)
        .bind(role)
        .execute(&pool)
        .await
        .unwrap();
    }

    let mut state = app_state(pool.clone());
    *state.accounts.write().await = load_accounts_from_postgres(&pool)
        .await
        .expect("load the real PostgreSQL account projection");
    state.sessions = SessionBackend::new(DbSessionStore::new(pool.clone()));
    state.config.auth_cookie_secure = false; // localhost-only proof, never production
    let app = personal_events_session_router(state);
    let cookie_a = personal_events_login(&app, &a).await;
    let cookie_b = personal_events_login(&app, &b).await;
    let cookie_c = personal_events_login(&app, &c).await;
    let cookie_guest = personal_events_login(&app, &guest).await;
    assert_ne!(
        cookie_a, cookie_b,
        "different users have independent sessions"
    );
    assert_ne!(cookie_b, cookie_c, "session identity must not be reused");

    for (id, cookie) in [
        (&a, &cookie_a),
        (&b, &cookie_b),
        (&c, &cookie_c),
        (&guest, &cookie_guest),
    ] {
        let (status, response) =
            personal_events_http(&app, Method::GET, "/auth/me", Some(cookie), None, true).await;
        assert_eq!(status, StatusCode::OK, "{response}");
        assert_eq!(
            serde_json::from_str::<Value>(&response).unwrap()["account_id"],
            id.as_str(),
            "every cookie must resolve only its real owner"
        );
    }
    let (anonymous, _) =
        personal_events_http(&app, Method::GET, "/personal-events", None, None, true).await;
    assert_eq!(anonymous, StatusCode::UNAUTHORIZED);

    // A real HTTP POST creates the circle and inserts the two PostgreSQL
    // personal events in the same transaction: no manual message fixture.
    let (created, response) = personal_events_http(
        &app,
        Method::POST,
        "/circles",
        Some(&cookie_a),
        Some(json!({
            "kind": "household",
            "name": "Cookie-Proof House",
            "invitee_account_id": b
        })),
        true,
    )
    .await;
    assert_eq!(created, StatusCode::CREATED, "{response}");
    assert_contract(
        "audience-circle",
        "#/definitions/own_circle",
        &response,
        "POST /circles",
    );
    let circle = serde_json::from_str::<Value>(&response).unwrap()["id"]
        .as_str()
        .expect("created household id")
        .to_owned();

    let incoming = personal_events_inbox(&app, &cookie_b).await;
    assert_eq!(incoming["unread_count"], 1);
    assert_eq!(incoming["items"][0]["event_kind"], "circle_invited");
    assert_eq!(incoming["items"][0]["actionable"], true);
    let event_id = incoming["items"][0]["id"].as_str().unwrap();
    let timestamp = incoming["items"][0]["invitation_created_at"]
        .as_str()
        .unwrap()
        .replace('+', "%2B");
    let sent = personal_events_inbox(&app, &cookie_a).await;
    assert_eq!(sent["unread_count"], 0);
    assert_eq!(sent["items"][0]["event_kind"], "circle_invitation_sent");
    for cookie in [&cookie_c, &cookie_guest] {
        let page = personal_events_inbox(&app, cookie).await;
        assert_eq!(page["unread_count"], 0);
        assert!(page["items"].as_array().unwrap().is_empty());
    }

    let read_path = format!("/personal-events/{event_id}/read");
    let (csrf, _) =
        personal_events_http(&app, Method::POST, &read_path, Some(&cookie_b), None, false).await;
    assert_eq!(
        csrf,
        StatusCode::FORBIDDEN,
        "read receipt needs a same-origin request"
    );
    let (foreign, _) =
        personal_events_http(&app, Method::POST, &read_path, Some(&cookie_c), None, true).await;
    assert_eq!(
        foreign,
        StatusCode::NOT_FOUND,
        "other accounts cannot read B's event"
    );
    let (marked, _) =
        personal_events_http(&app, Method::POST, &read_path, Some(&cookie_b), None, true).await;
    assert_eq!(marked, StatusCode::NO_CONTENT);
    assert_eq!(
        personal_events_inbox(&app, &cookie_b).await["unread_count"],
        0
    );

    let accept_path =
        format!("/circles/{circle}/accept?expected_invitation_created_at={timestamp}");
    let (accepted, reply) = personal_events_http(
        &app,
        Method::POST,
        &accept_path,
        Some(&cookie_b),
        None,
        true,
    )
    .await;
    assert_eq!(accepted, StatusCode::OK, "{reply}");
    let accepted_view = assert_contract(
        "audience-circle",
        "#/definitions/own_circle",
        &reply,
        "POST /circles/{id}/accept",
    );
    assert_eq!(accepted_view["state"], "active");
    assert_eq!(accepted_view["membership_status"], "active");
    assert!(accepted_view["invitation_created_at"].is_null());
    let (replayed, _) = personal_events_http(
        &app,
        Method::POST,
        &accept_path,
        Some(&cookie_b),
        None,
        true,
    )
    .await;
    assert_eq!(replayed, StatusCode::CONFLICT);
    let a_events = personal_events_inbox(&app, &cookie_a).await;
    assert_eq!(
        a_events["items"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|x| x["event_kind"] == "circle_joined")
            .count(),
        1
    );
    assert_eq!(
        personal_events_inbox(&app, &cookie_b).await["items"][0]["actionable"],
        false
    );

    // A invites C twice. The first INSERT sends one event; the second
    // attempt must not masquerade as a newly sent invitation.
    let invite_path = format!("/circles/{circle}/invitations");
    for attempt in 0..2 {
        let (status, _) = personal_events_http(
            &app,
            Method::POST,
            &invite_path,
            Some(&cookie_a),
            Some(json!({"account_id": c})),
            true,
        )
        .await;
        assert_eq!(
            status,
            if attempt == 0 {
                StatusCode::NO_CONTENT
            } else {
                StatusCode::CONFLICT
            }
        );
    }
    let c_inbox = personal_events_inbox(&app, &cookie_c).await;
    assert_eq!(c_inbox["unread_count"], 1);
    assert_eq!(
        c_inbox["items"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|x| x["event_kind"] == "circle_invited")
            .count(),
        1
    );
    // Contract: /circles/me lists the reader's own pending invitation, and
    // the member list is visible only to active members and lists only them.
    let (status, mine) = personal_events_http(
        &app,
        Method::GET,
        "/circles/me",
        Some(&cookie_c),
        None,
        true,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{mine}");
    let mine = assert_contract(
        "audience-circle",
        "#/definitions/own_circle_page",
        &mine,
        "GET /circles/me (invited)",
    );
    assert_eq!(mine[0]["membership_status"], "invited");
    assert!(mine[0]["invitation_created_at"].is_string());
    let members_path = format!("/circles/{circle}/members");
    let (status, _) = personal_events_http(
        &app,
        Method::GET,
        &members_path,
        Some(&cookie_c),
        None,
        true,
    )
    .await;
    assert_eq!(
        status,
        StatusCode::NOT_FOUND,
        "invited accounts see no members"
    );
    let (status, members) = personal_events_http(
        &app,
        Method::GET,
        &members_path,
        Some(&cookie_b),
        None,
        true,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{members}");
    let members = assert_contract(
        "audience-circle-membership",
        "#/definitions/member_list",
        &members,
        "GET /circles/{id}/members",
    );
    let listed: Vec<&str> = members
        .as_array()
        .unwrap()
        .iter()
        .map(|member| member["account_id"].as_str().unwrap())
        .collect();
    let mut expected = vec![a.as_str(), b.as_str()];
    expected.sort_unstable();
    assert_eq!(listed, expected, "only active members, never the invitee");

    let c_round = c_inbox["items"][0]["invitation_created_at"]
        .as_str()
        .unwrap()
        .replace('+', "%2B");
    let decline_path = format!(
        "/circles/{circle}/membership?expected_status=invited&expected_invitation_created_at={c_round}"
    );
    let (declined, _) = personal_events_http(
        &app,
        Method::DELETE,
        &decline_path,
        Some(&cookie_c),
        None,
        true,
    )
    .await;
    assert_eq!(declined, StatusCode::NO_CONTENT);
    let (stale, _) = personal_events_http(
        &app,
        Method::DELETE,
        &decline_path,
        Some(&cookie_c),
        None,
        true,
    )
    .await;
    assert_eq!(stale, StatusCode::CONFLICT);
    let c_after = personal_events_inbox(&app, &cookie_c).await;
    assert_eq!(c_after["items"][0]["actionable"], false);
    assert!(c_after["items"][0]["circle_id"].is_null());
    assert!(c_after["items"][0]["circle_name"].is_null());
    let a_after = personal_events_inbox(&app, &cookie_a).await;
    assert_eq!(
        a_after["items"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|x| x["event_kind"] == "circle_declined")
            .count(),
        1
    );
    assert_eq!(
        a_after["items"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|x| x["event_kind"] == "circle_invitation_sent")
            .count(),
        2
    );
    assert_eq!(
        personal_events_inbox(&app, &cookie_guest).await["unread_count"],
        0
    );

    // The canonical 1:1 inbox remains separate from typed system messages.
    for cookie in [&cookie_a, &cookie_b] {
        let (status, response) = personal_events_http(
            &app,
            Method::GET,
            "/direct-conversations",
            Some(cookie),
            None,
            true,
        )
        .await;
        assert_eq!(status, StatusCode::OK, "{response}");
        assert!(
            !response.contains("Cookie-Proof House"),
            "circle system events are not synthetic direct messages"
        );
    }

    // Contract: the canonical PostgreSQL rows behind these responses.
    let circle_uuid = Uuid::parse_str(&circle).unwrap();
    let rows = |sql: &'static str| {
        let pool = pool.clone();
        async move {
            sqlx::query_scalar::<_, Value>(sql)
                .bind(circle_uuid)
                .fetch_all(&pool)
                .await
                .unwrap()
        }
    };
    for (schema, sql, expected) in [
        (
            "audience-circle",
            "SELECT to_jsonb(c) FROM domain_audience_circles c WHERE id=$1",
            1,
        ),
        (
            "audience-circle-membership",
            "SELECT to_jsonb(m) FROM domain_audience_circle_members m WHERE circle_id=$1",
            2,
        ),
        (
            "personal-event",
            "SELECT to_jsonb(e) FROM domain_personal_event_messages e WHERE circle_id=$1",
            // invited+sent (B), joined (A), invited+sent (C), declined (A)
            6,
        ),
    ] {
        let records = rows(sql).await;
        assert_eq!(records.len(), expected, "{schema} rows");
        let contract = support::domain_contract::load(schema);
        for record in &records {
            support::domain_contract::assert_valid(&contract, "#", record, schema);
        }
    }

    sqlx::query("DELETE FROM domain_audience_circles WHERE id=$1")
        .bind(circle_uuid)
        .execute(&pool)
        .await
        .unwrap();
    for id in [&a, &b, &c, &guest] {
        sqlx::query("DELETE FROM domain_accounts WHERE id=$1")
            .bind(id)
            .execute(&pool)
            .await
            .unwrap();
    }
}

/// Regression: duplicate founding and duplicate invites must not claim new
/// deliveries; the member list may not silently hide everyone after member 100.
#[tokio::test]
#[serial]
#[ignore = "requires DATABASE_URL for a disposable direct PostgreSQL database"]
async fn household_create_invite_and_full_member_list_regression() {
    use weltgewebe_api::routes::circles::{create_circle, invite_account, list_circle_members};

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
    let owner = format!("fix-owner-{token}");
    let neighbor = format!("fix-neighbor-{token}");
    let third = format!("fix-third-{token}");
    for id in [&owner, &neighbor, &third] {
        sqlx::query(
            "INSERT INTO domain_accounts(id,title,kind,location_lat,location_lon)
             VALUES ($1,$1,'garnrolle',53.55,10.05)",
        )
        .bind(id)
        .execute(&pool)
        .await
        .unwrap();
    }

    let input = || {
        axum::Json(
            serde_json::from_value(json!({
                "kind": "household", "invitee_account_id": neighbor
            }))
            .unwrap(),
        )
    };
    let created = create_circle(
        State(app_state(pool.clone())),
        Extension(actor(&owner)),
        input(),
    )
    .await
    .unwrap();
    assert_eq!(created.status(), StatusCode::CREATED);
    let response: Value = serde_json::from_slice(
        &body::to_bytes(created.into_body(), 1024 * 1024)
            .await
            .unwrap(),
    )
    .unwrap();
    support::domain_contract::assert_valid(
        &support::domain_contract::load("audience-circle"),
        "#/definitions/own_circle",
        &response,
        "create_circle",
    );
    let id = Uuid::parse_str(response["id"].as_str().unwrap()).unwrap();
    assert_eq!(kinds(&pool, &neighbor).await, vec!["circle_invited"]);

    let duplicate = create_circle(
        State(app_state(pool.clone())),
        Extension(actor(&owner)),
        input(),
    )
    .await
    .unwrap_err();
    assert_eq!(duplicate.0, StatusCode::CONFLICT);

    let reverse = create_circle(
        State(app_state(pool.clone())),
        Extension(actor(&neighbor)),
        axum::Json(
            serde_json::from_value(json!({
                "kind": "household", "invitee_account_id": owner
            }))
            .unwrap(),
        ),
    )
    .await
    .unwrap_err();
    assert_eq!(reverse.0, StatusCode::CONFLICT);
    assert_eq!(kinds(&pool, &neighbor).await, vec!["circle_invited"]);

    let invited_at: chrono::DateTime<chrono::Utc> = sqlx::query_scalar(
        "SELECT created_at FROM domain_audience_circle_members
         WHERE circle_id=$1 AND account_id=$2",
    )
    .bind(id)
    .bind(&neighbor)
    .fetch_one(&pool)
    .await
    .unwrap();
    let accepted: InvitationRevisionQuery = serde_json::from_value(json!({
        "expected_invitation_created_at": invited_at
    }))
    .unwrap();
    accept_invitation(
        State(app_state(pool.clone())),
        Extension(actor(&neighbor)),
        Path(id),
        Query(accepted),
    )
    .await
    .unwrap();

    let third_invitation = || {
        axum::Json(
            serde_json::from_value(json!({
                "account_id": third
            }))
            .unwrap(),
        )
    };
    let sent = invite_account(
        State(app_state(pool.clone())),
        Extension(actor(&owner)),
        Path(id),
        third_invitation(),
    )
    .await
    .unwrap();
    assert_eq!(sent.status(), StatusCode::NO_CONTENT);
    let duplicate_invite = invite_account(
        State(app_state(pool.clone())),
        Extension(actor(&owner)),
        Path(id),
        third_invitation(),
    )
    .await
    .unwrap_err();
    assert_eq!(duplicate_invite.0, StatusCode::CONFLICT);
    assert_eq!(kinds(&pool, &third).await, vec!["circle_invited"]);

    // 100 additional accepted members: they must all be visible, in addition
    // to the two founders. The third invitee remains invisible until accepted.
    let prefix = format!("fix-extra-{token}-");
    sqlx::query(
        "INSERT INTO domain_accounts(id,title,kind,location_lat,location_lon)
         SELECT $1 || g::text, $1 || g::text, 'garnrolle',53.55,10.05
         FROM generate_series(1,100) g",
    )
    .bind(&prefix)
    .execute(&pool)
    .await
    .unwrap();
    sqlx::query(
        "INSERT INTO domain_audience_circle_members
          (circle_id,account_id,status,invited_by_account_id,joined_at)
         SELECT $1, $2 || g::text, 'active', $3, NOW()
         FROM generate_series(1,100) g",
    )
    .bind(id)
    .bind(&prefix)
    .bind(&owner)
    .execute(&pool)
    .await
    .unwrap();
    let members = list_circle_members(
        State(app_state(pool.clone())),
        Extension(actor(&owner)),
        Path(id),
    )
    .await
    .unwrap();
    let members_json: Value = serde_json::from_slice(
        &body::to_bytes(members.into_body(), 1024 * 1024)
            .await
            .unwrap(),
    )
    .unwrap();
    assert_eq!(members_json.as_array().unwrap().len(), 102);
    support::domain_contract::assert_valid(
        &support::domain_contract::load("audience-circle-membership"),
        "#/definitions/member_list",
        &members_json,
        "list_circle_members",
    );
}

/// M2 (audit 2026-11-22): every invitation and every decline bumps the global
/// privacy generation and forces a full projection reload in every API
/// process. The PostgreSQL-shared pair budget must stop an endless
/// propose/decline loop across replicas, while declining always stays free.
#[tokio::test]
#[serial]
#[ignore = "requires DATABASE_URL for a disposable direct PostgreSQL database"]
async fn shared_invitation_budget_stops_decline_reinvite_loops_across_replicas() {
    use weltgewebe_api::auth::rate_limit::CIRCLE_INVITATION_PAIR_LIMIT;
    use weltgewebe_api::routes::circles::create_circle;

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
    let inviter = format!("m2-inviter-{token}");
    let invitee = format!("m2-invitee-{token}");
    for id in [&inviter, &invitee] {
        sqlx::query(
            "INSERT INTO domain_accounts(id,title,kind,location_lat,location_lon)
             VALUES ($1,$1,'garnrolle',53.55,10.05)",
        )
        .bind(id)
        .execute(&pool)
        .await
        .unwrap();
    }
    // Two API processes with their own limiter instances share only PostgreSQL.
    let replica = || {
        let mut state = app_state(pool.clone());
        state.rate_limiter = Arc::new(AuthRateLimiter::new_postgres(&state.config, pool.clone()));
        state
    };
    let (first, second) = (replica(), replica());
    let propose = |state: ApiState| {
        let (inviter, invitee) = (inviter.clone(), invitee.clone());
        async move {
            create_circle(
                State(state),
                Extension(actor(&inviter)),
                axum::Json(
                    serde_json::from_value(json!({
                        "kind": "household", "invitee_account_id": invitee
                    }))
                    .unwrap(),
                ),
            )
            .await
            .unwrap()
        }
    };
    let decline = |state: ApiState, circle: Uuid| {
        let invitee = invitee.clone();
        async move {
            let query: LeaveCircleQuery =
                serde_json::from_value(json!({"expected_status":"invited"})).unwrap();
            leave_circle(
                State(state),
                Extension(actor(&invitee)),
                Path(circle),
                Query(query),
            )
            .await
            .unwrap()
            .status()
        }
    };

    let mut circles = Vec::new();
    for round in 0..CIRCLE_INVITATION_PAIR_LIMIT.2 {
        let state = if round % 2 == 0 { &first } else { &second };
        let created = propose(state.clone()).await;
        assert_eq!(created.status(), StatusCode::CREATED, "round {round}");
        let view: Value = serde_json::from_slice(
            &body::to_bytes(created.into_body(), 1024 * 1024)
                .await
                .unwrap(),
        )
        .unwrap();
        let circle = Uuid::parse_str(view["id"].as_str().unwrap()).unwrap();
        // The decline runs on the other replica and is never rate limited.
        let other = if round % 2 == 0 { &second } else { &first };
        assert_eq!(decline(other.clone(), circle).await, StatusCode::NO_CONTENT);
        circles.push(circle);
    }

    let generation = || async {
        sqlx::query_scalar::<_, i64>(
            "SELECT version FROM domain_projection_state WHERE singleton = TRUE",
        )
        .fetch_one(&pool)
        .await
        .unwrap()
    };
    let before = generation().await;
    let limited = propose(second.clone()).await;
    assert_eq!(
        generation().await,
        before,
        "a refused proposal must not force a projection reload"
    );
    assert_eq!(limited.status(), StatusCode::TOO_MANY_REQUESTS);
    let retry_after: u64 = limited.headers()[header::RETRY_AFTER]
        .to_str()
        .unwrap()
        .parse()
        .unwrap();
    assert!((1..=86_400).contains(&retry_after), "{retry_after}");
    assert_eq!(
        limited.headers()[header::CACHE_CONTROL],
        "private, no-store",
        "a refusal must not be cached either"
    );

    let open_or_new: i64 = sqlx::query_scalar(
        "SELECT COUNT(*) FROM domain_audience_circles WHERE created_by_account_id=$1",
    )
    .bind(&inviter)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(
        open_or_new,
        i64::try_from(circles.len()).unwrap(),
        "the refused proposal wrote no circle"
    );
    assert_eq!(
        kinds(&pool, &invitee)
            .await
            .iter()
            .filter(|kind| kind.as_str() == "circle_invited")
            .count(),
        circles.len(),
        "the refused proposal delivered no personal event"
    );
}
