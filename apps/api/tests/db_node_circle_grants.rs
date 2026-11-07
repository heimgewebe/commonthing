//! Disposable PostgreSQL proof of one canonical node with explicit circle read grants.
mod support;

use axum::{
    extract::{Path, State},
    http::StatusCode,
    Extension, Json,
};
use sqlx::{postgres::PgPoolOptions, PgPool};
use std::sync::Arc;
use tokio::sync::RwLock;
use uuid::Uuid;
use weltgewebe_api::domain_db::load_nodes_bbox_from_postgres;
use weltgewebe_api::{
    auth::{
        accounts::AccountStore, rate_limit::AuthRateLimiter, role::Role, session::SessionBackend,
    },
    config::{
        AppConfig, DomainAccountWriteSource, DomainEdgeWriteSource, DomainNodeWriteSource,
        DomainReadSource, PasskeyCredentialSource,
    },
    middleware::auth::AuthContext,
    routes::node_audience::{get_node_audience, set_node_audience, SetNodeAudience},
    state::{ApiState, OrderedCache},
    telemetry::{BuildInfo, Metrics},
};

/// Minimal production-configured state for direct GET/PUT handler proof.
/// HTTP CSRF/session middleware is tested separately; this tests the actual
/// owner authorization and PostgreSQL write with three distinct identities.
fn p2_audience_state(pool: PgPool) -> ApiState {
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
            version: "p2-proof",
            commit: "p2-proof",
            build_timestamp: "p2-proof",
        })
        .expect("metrics"),
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

async fn readable(pool: &PgPool, node: &str, actor: Option<&str>) -> bool {
    sqlx::query_scalar::<_, bool>(
        "SELECT weltgewebe_node_readable_to(
                    n.id, n.search_visibility,
                    weltgewebe_search_node_owner_account_id(n.payload), $2::text)
           FROM domain_nodes n WHERE n.id=$1",
    )
    .bind(node)
    .bind(actor)
    .fetch_one(pool)
    .await
    .expect("readable policy query")
}

#[tokio::test]
#[ignore = "requires DATABASE_URL for an isolated disposable direct PostgreSQL database"]
async fn explicit_node_grants_require_live_consent_and_revoke_all_read_paths() {
    let url = support::postgres_proof::validated_direct_disposable_url(
        std::env::var("DATABASE_URL").expect("disposable PostgreSQL required"),
    );
    let pool = PgPoolOptions::new()
        .max_connections(4)
        .connect(&url)
        .await
        .expect("connect disposable PostgreSQL");
    sqlx::migrate!("./migrations")
        .run(&pool)
        .await
        .expect("apply migrations");

    let token = Uuid::new_v4();
    let a = format!("circle-proof-owner-{token}");
    let b = format!("circle-proof-member-{token}");
    let outsider = format!("circle-proof-outsider-{token}");
    let node = format!("circle-proof-node-{token}");
    let circle = Uuid::new_v4();

    // Same precise location intentionally confers no node read capability.
    for id in [&a, &b, &outsider] {
        sqlx::query(
            "INSERT INTO domain_accounts(id,title,kind,location_lat,location_lon)
             VALUES($1,$1,'garnrolle',53.555,10.055)",
        )
        .bind(id)
        .execute(&pool)
        .await
        .expect("account");
    }
    sqlx::query(
        "INSERT INTO domain_nodes(id,kind,title,lat,lon,payload,search_visibility)
         VALUES($1,'place','SECRET HOUSE TITLE',53.555,10.055,$2::jsonb,'private')",
    )
    .bind(&node)
    .bind(serde_json::json!({"created_by_account_id":a}).to_string())
    .execute(&pool)
    .await
    .expect("private node");
    sqlx::query(
        "INSERT INTO domain_audience_circles(id,kind,name,created_by_account_id)
         VALUES($1,'household','Test house',$2)",
    )
    .bind(circle)
    .bind(&a)
    .execute(&pool)
    .await
    .expect("forming house");
    sqlx::query(
        "INSERT INTO domain_audience_circle_members
          (circle_id, account_id, status, joined_at, invited_by_account_id)
         VALUES ($1,$2,'active',NOW(),$2),
                ($1,$3,'invited',NULL,$2)",
    )
    .bind(circle)
    .bind(&a)
    .bind(&b)
    .execute(&pool)
    .await
    .expect("forming circle members");
    sqlx::query("INSERT INTO domain_node_circle_grants(node_id,circle_id) VALUES($1,$2)")
        .bind(&node)
        .bind(circle)
        .execute(&pool)
        .await
        .expect("explicit grant");

    assert!(readable(&pool, &node, Some(&a)).await);
    assert!(
        !readable(&pool, &node, Some(&b)).await,
        "pending invitation must not read"
    );
    assert!(
        !readable(&pool, &node, Some(&outsider)).await,
        "same location must not grant"
    );
    assert!(!readable(&pool, &node, None).await, "anonymous cannot read");

    sqlx::query("UPDATE domain_audience_circle_members SET status='active',joined_at=NOW() WHERE circle_id=$1 AND account_id=$2")
        .bind(circle).bind(&b).execute(&pool).await.expect("consent");
    sqlx::query("UPDATE domain_audience_circles SET state='active' WHERE id=$1")
        .bind(circle)
        .execute(&pool)
        .await
        .expect("activate");

    // Exercise the real Rust handler too: a previous regression decoded a
    // result set of circle UUIDs with fetch_one instead of fetch_all.
    sqlx::query("DELETE FROM domain_node_circle_grants WHERE node_id=$1")
        .bind(&node)
        .execute(&pool)
        .await
        .expect("clear fixture grant before owner PUT");
    let state = p2_audience_state(pool.clone());
    let owner_request: SetNodeAudience = serde_json::from_value(serde_json::json!({
        "scope": "circles", "circle_ids": [circle],
        "expected_scope": "private", "expected_circle_ids": []
    }))
    .expect("valid owner payload");
    let granted = set_node_audience(
        State(state.clone()),
        Path(node.clone()),
        Extension(actor(&a)),
        Json(owner_request),
    )
    .await
    .expect("owner circles PUT");
    assert_eq!(
        granted.status(),
        StatusCode::OK,
        "owner's explicit grant must succeed"
    );
    assert_eq!(
        granted
            .headers()
            .get("cache-control")
            .and_then(|h| h.to_str().ok()),
        Some("private, no-store")
    );

    let foreign_get = get_node_audience(
        State(state.clone()),
        Path(node.clone()),
        Extension(actor(&b)),
    )
    .await
    .expect_err("member must not inspect owner's audience choices");
    assert_eq!(foreign_get.0, StatusCode::NOT_FOUND);
    let foreign_put: SetNodeAudience = serde_json::from_value(serde_json::json!({
        "scope": "public", "circle_ids": [],
        "expected_scope": "circles", "expected_circle_ids": [circle]
    }))
    .expect("valid non-owner payload");
    let denied = set_node_audience(
        State(state.clone()),
        Path(node.clone()),
        Extension(actor(&b)),
        Json(foreign_put),
    )
    .await
    .expect_err("member must never change owner's visibility");
    assert_eq!(denied.0, StatusCode::NOT_FOUND);

    let stale: SetNodeAudience = serde_json::from_value(serde_json::json!({
        "scope": "public", "circle_ids": [],
        "expected_scope": "private", "expected_circle_ids": []
    }))
    .expect("stale client payload");
    let denied = set_node_audience(
        State(state),
        Path(node.clone()),
        Extension(actor(&a)),
        Json(stale),
    )
    .await
    .expect_err("stale client must not silently publish private resource");
    assert_eq!(denied.0, StatusCode::PRECONDITION_FAILED);
    assert!(
        readable(&pool, &node, Some(&b)).await,
        "failed writes preserve grant"
    );

    assert!(
        readable(&pool, &node, Some(&b)).await,
        "member can read private node"
    );
    let bbox = load_nodes_bbox_from_postgres(&pool, 53.5, 53.6, 10.0, 10.1, 10, 0, Some(&b))
        .await
        .expect("member BBOX");
    assert!(
        bbox.iter().any(|n| n.id == node),
        "member BBOX includes node"
    );
    let outsider_bbox =
        load_nodes_bbox_from_postgres(&pool, 53.5, 53.6, 10.0, 10.1, 10, 0, Some(&outsider))
            .await
            .expect("stranger BBOX");
    assert!(
        !outsider_bbox.iter().any(|n| n.id == node),
        "stranger BBOX hides node"
    );
    let anonymous_bbox = load_nodes_bbox_from_postgres(&pool, 53.5, 53.6, 10.0, 10.1, 10, 0, None)
        .await
        .expect("anonymous BBOX");
    assert!(
        !anonymous_bbox.iter().any(|n| n.id == node),
        "anonymous BBOX hides node"
    );

    let before_exit: i64 =
        sqlx::query_scalar("SELECT version FROM domain_projection_state WHERE singleton=TRUE")
            .fetch_one(&pool)
            .await
            .expect("generation before exit");
    sqlx::query("DELETE FROM domain_audience_circle_members WHERE circle_id=$1 AND account_id=$2")
        .bind(circle)
        .bind(&b)
        .execute(&pool)
        .await
        .expect("leave circle");
    let after_exit: i64 =
        sqlx::query_scalar("SELECT version FROM domain_projection_state WHERE singleton=TRUE")
            .fetch_one(&pool)
            .await
            .expect("generation after exit");
    assert!(
        after_exit > before_exit,
        "departure must invalidate existing domain privacy fence"
    );
    assert!(
        !readable(&pool, &node, Some(&b)).await,
        "departure revokes immediately"
    );

    // Re-joining does not bypass the additional requirement that the owner
    // account itself remains active and keeps its original identity.
    sqlx::query(
        "INSERT INTO domain_audience_circle_members
             (circle_id,account_id,status,invited_by_account_id,joined_at)
         VALUES($1,$2,'active',$3,NOW())",
    )
    .bind(circle)
    .bind(&b)
    .bind(&a)
    .execute(&pool)
    .await
    .expect("rejoin");
    assert!(readable(&pool, &node, Some(&b)).await);
    sqlx::query("UPDATE domain_accounts SET disabled=TRUE WHERE id=$1")
        .bind(&a)
        .execute(&pool)
        .await
        .expect("disable owner");
    assert!(
        !readable(&pool, &node, Some(&b)).await,
        "disabled owner cannot leave private content visible to circle members"
    );
    sqlx::query("UPDATE domain_accounts SET disabled=FALSE WHERE id=$1")
        .bind(&a)
        .execute(&pool)
        .await
        .expect("restore owner");
    assert!(readable(&pool, &node, Some(&b)).await);

    // A new creator identity means old circle consent is no longer valid.
    sqlx::query(
        "UPDATE domain_nodes
         SET payload=jsonb_set(payload,'{created_by_account_id}',to_jsonb($2::text))
         WHERE id=$1",
    )
    .bind(&node)
    .bind(&outsider)
    .execute(&pool)
    .await
    .expect("change creator identity");
    assert!(
        !readable(&pool, &node, Some(&b)).await,
        "transferred node must not inherit another owner's sharing grants"
    );
    let remaining: i64 =
        sqlx::query_scalar("SELECT count(*) FROM domain_node_circle_grants WHERE node_id=$1")
            .bind(&node)
            .fetch_one(&pool)
            .await
            .expect("grants removed on owner change");
    assert_eq!(remaining, 0);
    sqlx::query(
        "UPDATE domain_nodes
         SET payload=jsonb_set(payload,'{created_by_account_id}',to_jsonb($2::text))
         WHERE id=$1",
    )
    .bind(&node)
    .bind(&a)
    .execute(&pool)
    .await
    .expect("restore original owner");
    sqlx::query("INSERT INTO domain_node_circle_grants(node_id,circle_id) VALUES($1,$2)")
        .bind(&node)
        .bind(circle)
        .execute(&pool)
        .await
        .expect("fresh, explicit owner consent");
    assert!(readable(&pool, &node, Some(&b)).await);

    // Former owner consent cannot accidentally resurrect when a node moves
    // through public, hidden or private again.
    sqlx::query("UPDATE domain_nodes SET search_visibility='hidden' WHERE id=$1")
        .bind(&node)
        .execute(&pool)
        .await
        .expect("hide node");
    let n: i64 =
        sqlx::query_scalar("SELECT count(*) FROM domain_node_circle_grants WHERE node_id=$1")
            .bind(&node)
            .fetch_one(&pool)
            .await
            .expect("count purged grants");
    assert_eq!(n, 0, "hidden transition must purge previous grants");
    assert!(!readable(&pool, &node, Some(&b)).await);
    assert!(
        readable(&pool, &node, Some(&a)).await,
        "legacy owner-hidden privilege preserved"
    );
    sqlx::query("UPDATE domain_nodes SET search_visibility='private' WHERE id=$1")
        .bind(&node)
        .execute(&pool)
        .await
        .expect("return private");
    assert!(
        !readable(&pool, &node, Some(&b)).await,
        "revoked grant must not reactivate"
    );
    sqlx::query("UPDATE domain_nodes SET search_visibility='revoked' WHERE id=$1")
        .bind(&node)
        .execute(&pool)
        .await
        .expect("revoke node");
    assert!(
        !readable(&pool, &node, Some(&a)).await,
        "revoked blocks even owner"
    );
    assert!(!readable(&pool, &node, Some(&b)).await);
    sqlx::query("DELETE FROM domain_nodes WHERE id=$1")
        .bind(&node)
        .execute(&pool)
        .await
        .expect("clean node");
    sqlx::query("DELETE FROM domain_audience_circles WHERE id=$1")
        .bind(circle)
        .execute(&pool)
        .await
        .expect("clean house");
    for id in [&a, &b, &outsider] {
        sqlx::query("DELETE FROM domain_accounts WHERE id=$1")
            .bind(id)
            .execute(&pool)
            .await
            .expect("clean account");
    }
}
