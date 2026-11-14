//! Disposable PostgreSQL proof: one saved public Garnrollen profile tag becomes
//! exactly one canonical node with an explicit P2 audience, atomically.
mod support;

use axum::{
    body::{to_bytes, Body},
    extract::{connect_info::MockConnectInfo, Path, State},
    http::{header, Method, Request, StatusCode},
    middleware::from_fn_with_state,
    response::Response,
    Extension, Json, Router,
};
use serde_json::{json, Value};
use serial_test::serial;
use sqlx::{postgres::PgPoolOptions, PgPool};
use std::{net::SocketAddr, sync::Arc};
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
        accounts::{
            get_account, move_profile_tag_to_resource, update_own_garnrolle_profile,
            MoveProfileTagRequest, UpdateOwnGarnrolleRequest,
        },
        api_router,
        node_audience::get_node_audience,
        nodes::get_node,
    },
    state::{ApiState, OrderedCache},
    telemetry::{BuildInfo, Metrics},
    test_helpers::EnvGuard,
};

fn config(postgres: bool, max_guest_owned_nodes: usize) -> AppConfig {
    let (read, account, node, edge) = if postgres {
        (
            DomainReadSource::Postgres,
            DomainAccountWriteSource::Postgres,
            DomainNodeWriteSource::Postgres,
            DomainEdgeWriteSource::Postgres,
        )
    } else {
        (
            DomainReadSource::Jsonl,
            DomainAccountWriteSource::Jsonl,
            DomainNodeWriteSource::Jsonl,
            DomainEdgeWriteSource::Jsonl,
        )
    };
    AppConfig {
        max_guest_owned_nodes,
        domain_read_source: read,
        domain_account_write_source: account,
        domain_node_write_source: node,
        domain_edge_write_source: edge,
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
    }
}

/// One simulated API instance. Two instances on the same pool share no
/// process mutex, so only PostgreSQL can serialize their writes.
fn instance(pool: Option<PgPool>, config: AppConfig) -> ApiState {
    ApiState {
        db_pool_configured: pool.is_some(),
        db_pool: pool,
        nats_client: None,
        nats_configured: false,
        metrics: Metrics::try_new(BuildInfo {
            version: "p2b-proof",
            commit: "p2b-proof",
            build_timestamp: "p2b-proof",
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

async fn pg_instance(pool: &PgPool, max_guest_owned_nodes: usize) -> ApiState {
    let state = instance(Some(pool.clone()), config(true, max_guest_owned_nodes));
    *state.accounts.write().await = load_accounts_from_postgres(pool)
        .await
        .expect("load PostgreSQL accounts into cache");
    state
}

fn actor(id: &str, role: Role) -> AuthContext {
    AuthContext {
        authenticated: true,
        account_id: Some(id.to_owned()),
        device_id: None,
        role,
        expires_at: None,
    }
}

async fn disposable_pool() -> PgPool {
    let url = support::postgres_proof::validated_direct_disposable_url(
        std::env::var("DATABASE_URL").expect("disposable PostgreSQL required"),
    );
    let pool = PgPoolOptions::new()
        .max_connections(8)
        .connect(&url)
        .await
        .expect("connect disposable PostgreSQL");
    sqlx::migrate!("./migrations")
        .run(&pool)
        .await
        .expect("apply migrations");
    pool
}

const OWNER_TAGS: [&str; 5] = [
    "account",
    "garnrolle",
    "good:Leiter",
    "skill:Fahrrad reparieren",
    "interest:Commons",
];

async fn insert_account(pool: &PgPool, id: &str, role: &str, tags: &[&str]) {
    sqlx::query(
        "INSERT INTO domain_accounts(id,title,kind,role,map_state,location_lat,location_lon,public_payload)
         VALUES($1,$1,'garnrolle',$2,'not_on_map',53.555,10.055,$3::jsonb)",
    )
    .bind(id)
    .bind(role)
    .bind(json!({"summary": "Gemeinsame Dinge", "tags": tags}).to_string())
    .execute(pool)
    .await
    .expect("insert proof account");
}

async fn active_circle(pool: &PgPool, members: &[&str]) -> Uuid {
    let circle = Uuid::new_v4();
    sqlx::query(
        "INSERT INTO domain_audience_circles(id,kind,name,created_by_account_id,state)
         VALUES($1,'household','Proof house',$2,'active')",
    )
    .bind(circle)
    .bind(members[0])
    .execute(pool)
    .await
    .expect("circle");
    for member in members {
        sqlx::query(
            "INSERT INTO domain_audience_circle_members
              (circle_id, account_id, status, joined_at, invited_by_account_id)
             VALUES ($1,$2,'active',NOW(),$3)",
        )
        .bind(circle)
        .bind(member)
        .bind(members[0])
        .execute(pool)
        .await
        .expect("active member");
    }
    circle
}

async fn stored_tags(pool: &PgPool, account: &str) -> Vec<String> {
    let tags: Option<Value> =
        sqlx::query_scalar("SELECT public_payload->'tags' FROM domain_accounts WHERE id=$1")
            .bind(account)
            .fetch_one(pool)
            .await
            .expect("stored tags");
    tags.and_then(|value| serde_json::from_value(value).ok())
        .unwrap_or_default()
}

async fn owned_node_ids(pool: &PgPool, account: &str) -> Vec<String> {
    sqlx::query_scalar(
        "SELECT id FROM domain_nodes WHERE payload->>'created_by_account_id'=$1 ORDER BY id",
    )
    .bind(account)
    .fetch_all(pool)
    .await
    .expect("owned nodes")
}

async fn readable(pool: &PgPool, node: &str, reader: Option<&str>) -> bool {
    sqlx::query_scalar::<_, bool>(
        "SELECT weltgewebe_node_readable_to(
                    n.id, n.search_visibility,
                    weltgewebe_search_node_owner_account_id(n.payload), $2::text)
           FROM domain_nodes n WHERE n.id=$1",
    )
    .bind(node)
    .bind(reader)
    .fetch_one(pool)
    .await
    .expect("readable policy query")
}

fn move_request(
    tag: &str,
    scope: &str,
    circles: &[Uuid],
    operation: Uuid,
) -> MoveProfileTagRequest {
    serde_json::from_value(json!({
        "tag": tag,
        "scope": scope,
        "circle_ids": circles,
        "operation_id": operation,
    }))
    .expect("valid move request")
}

async fn body_json(response: Response) -> Value {
    let bytes = to_bytes(response.into_body(), usize::MAX)
        .await
        .expect("response body");
    serde_json::from_slice(&bytes).expect("JSON response")
}

async fn convert(
    state: &ApiState,
    account: &str,
    role: Role,
    request: MoveProfileTagRequest,
) -> Result<(StatusCode, Value), (StatusCode, String)> {
    let response = move_profile_tag_to_resource(
        State(state.clone()),
        Extension(actor(account, role)),
        Json(request),
    )
    .await?;
    let status = response.status();
    assert_eq!(
        response
            .headers()
            .get("cache-control")
            .and_then(|value| value.to_str().ok()),
        Some("private, no-store"),
        "moved resource response must not be cached"
    );
    Ok((status, body_json(response).await))
}

async fn patch_profile(
    state: &ApiState,
    account: &str,
    body: Value,
) -> Result<Vec<String>, (StatusCode, String)> {
    let request: UpdateOwnGarnrolleRequest =
        serde_json::from_value(body).expect("valid profile PATCH payload");
    let Json(profile) = update_own_garnrolle_profile(
        State(state.clone()),
        Extension(actor(account, Role::Weber)),
        Json(request),
    )
    .await?;
    Ok(profile.tags)
}

async fn public_account_json(state: &ApiState, account: &str, viewer: Option<&str>) -> String {
    let Json(details) = get_account(
        State(state.clone()),
        Path(account.to_string()),
        viewer.map(|id| Extension(actor(id, Role::Weber))),
    )
    .await
    .expect("public account details");
    serde_json::to_string(&details).expect("serialize account details")
}

fn without(tags: &[&str], removed: &str) -> Vec<String> {
    tags.iter()
        .filter(|tag| **tag != removed)
        .map(|tag| tag.to_string())
        .collect()
}

#[tokio::test]
#[ignore = "requires DATABASE_URL for an isolated disposable direct PostgreSQL database"]
async fn one_saved_tag_moves_atomically_into_one_circle_node_and_replays() {
    let pool = disposable_pool().await;
    let owner = Uuid::new_v4().to_string();
    let member = Uuid::new_v4().to_string();
    let outsider = Uuid::new_v4().to_string();
    insert_account(&pool, &owner, "weber", &OWNER_TAGS).await;
    insert_account(&pool, &member, "weber", &["account", "garnrolle"]).await;
    insert_account(&pool, &outsider, "weber", &["account", "garnrolle"]).await;
    let circle = active_circle(&pool, &[&owner, &member]).await;
    let state = pg_instance(&pool, 1_000).await;

    // Before: the value is a public profile tag for everyone.
    assert!(public_account_json(&state, &owner, None)
        .await
        .contains("good:Leiter"));

    let operation = Uuid::new_v4();
    let (status, body) = convert(
        &state,
        &owner,
        Role::Weber,
        move_request("good:Leiter", "circles", &[circle], operation),
    )
    .await
    .expect("owner moves saved tag");
    assert_eq!(status, StatusCode::CREATED);
    let node_id = body["node"]["id"].as_str().expect("node id").to_string();
    assert_eq!(body["node"]["kind"], "resource");
    assert_eq!(body["node"]["title"], "Leiter");
    assert_eq!(body["node"]["search_visibility"], "private");
    assert_eq!(body["audience"]["scope"], "circles");
    assert_eq!(body["audience"]["circle_ids"], json!([circle]));
    let expected_tags = without(&OWNER_TAGS, "good:Leiter");
    assert_eq!(body["profile"]["tags"], json!(expected_tags));

    // Exactly the selected tag left the public profile; all others remain.
    assert_eq!(stored_tags(&pool, &owner).await, expected_tags);
    let summary: Option<String> =
        sqlx::query_scalar("SELECT public_payload->>'summary' FROM domain_accounts WHERE id=$1")
            .bind(&owner)
            .fetch_one(&pool)
            .await
            .expect("summary");
    assert_eq!(summary.as_deref(), Some("Gemeinsame Dinge"));
    assert_eq!(owned_node_ids(&pool, &owner).await, vec![node_id.clone()]);

    // Node, origin Faden and grant are committed together.
    let origin_fäden: i64 =
        sqlx::query_scalar("SELECT count(*) FROM domain_edges WHERE source_id=$1 AND target_id=$2")
            .bind(&owner)
            .bind(&node_id)
            .fetch_one(&pool)
            .await
            .expect("origin Faden count");
    assert_eq!(origin_fäden, 1);
    let grants: Vec<Uuid> =
        sqlx::query_scalar("SELECT circle_id FROM domain_node_circle_grants WHERE node_id=$1")
            .bind(&node_id)
            .fetch_all(&pool)
            .await
            .expect("grants");
    assert_eq!(grants, vec![circle]);

    // Read policy: owner and active member only.
    assert!(readable(&pool, &node_id, Some(&owner)).await);
    assert!(readable(&pool, &node_id, Some(&member)).await);
    assert!(!readable(&pool, &node_id, Some(&outsider)).await);
    assert!(!readable(&pool, &node_id, None).await);

    // Public account projection and activity no longer expose the value.
    for viewer in [None, Some(outsider.as_str())] {
        let projected = public_account_json(&state, &owner, viewer).await;
        assert!(
            !projected.contains("Leiter"),
            "public profile/activity exposed a removed tag to an unauthorized reader"
        );
        assert!(projected.contains("skill:Fahrrad reparieren"));
    }
    // A second instance loading from PostgreSQL sees the same truth.
    let other_instance = pg_instance(&pool, 1_000).await;
    assert!(!public_account_json(&other_instance, &owner, None)
        .await
        .contains("Leiter"));

    // Foreign accounts cannot inspect the owner's audience decision.
    let foreign = get_node_audience(
        State(state.clone()),
        Path(node_id.clone()),
        Extension(actor(&member, Role::Weber)),
    )
    .await
    .expect_err("member must not inspect owner's audience");
    assert_eq!(foreign.0, StatusCode::NOT_FOUND);

    // Replay of the same operation: same node, no second node, no mutation.
    let (status, replay) = convert(
        &state,
        &owner,
        Role::Weber,
        move_request("good:Leiter", "circles", &[circle], operation),
    )
    .await
    .expect("replay is idempotent");
    assert_eq!(status, StatusCode::OK);
    assert_eq!(replay["node"]["id"], json!(node_id));
    assert_eq!(owned_node_ids(&pool, &owner).await, vec![node_id.clone()]);
    assert_eq!(stored_tags(&pool, &owner).await, expected_tags);

    // A retry that asks for another audience must not be answered with the
    // stored circle node as if it had the requested scope.
    for (scope, circles) in [("private", vec![]), ("public", vec![])] {
        let mismatch = convert(
            &state,
            &owner,
            Role::Weber,
            move_request("good:Leiter", scope, &circles, operation),
        )
        .await
        .expect_err("replay with another audience");
        assert_eq!(mismatch.0, StatusCode::CONFLICT, "{scope}");
    }
    assert!(readable(&pool, &node_id, Some(&member)).await);
    assert!(!readable(&pool, &node_id, Some(&outsider)).await);

    // Same operation id for different data is a conflict, not a second write.
    let conflict = convert(
        &state,
        &owner,
        Role::Weber,
        move_request("skill:Fahrrad reparieren", "private", &[], operation),
    )
    .await
    .expect_err("operation id reuse for another tag");
    assert_eq!(conflict.0, StatusCode::CONFLICT);
    assert_eq!(stored_tags(&pool, &owner).await, expected_tags);

    // A tag that is not (or no longer) saved cannot be moved.
    for tag in ["good:Leiter", "good:Nicht gespeichert"] {
        let missing = convert(
            &state,
            &owner,
            Role::Weber,
            move_request(tag, "private", &[], Uuid::new_v4()),
        )
        .await
        .expect_err("unsaved tag must not create a node");
        assert_eq!(missing.0, StatusCode::CONFLICT);
    }
    assert_eq!(owned_node_ids(&pool, &owner).await, vec![node_id.clone()]);

    // Another account cannot move the owner's tag: it is not its own tag.
    let foreign_move = convert(
        &state,
        &member,
        Role::Weber,
        move_request("skill:Fahrrad reparieren", "public", &[], Uuid::new_v4()),
    )
    .await
    .expect_err("foreign tag");
    assert_eq!(foreign_move.0, StatusCode::CONFLICT);
    assert!(owned_node_ids(&pool, &member).await.is_empty());
    assert!(stored_tags(&pool, &owner)
        .await
        .contains(&"skill:Fahrrad reparieren".to_string()));

    // Stale profile editors cannot republish the moved value.
    let stale_with_precondition = patch_profile(
        &state,
        &owner,
        json!({
            "title": owner, "map_state": "not_on_map",
            "summary": "Gemeinsame Dinge",
            "tags": OWNER_TAGS,
            "expected_tags": OWNER_TAGS,
        }),
    )
    .await
    .expect_err("stale expected_tags");
    assert_eq!(stale_with_precondition.0, StatusCode::CONFLICT);
    let legacy_without_precondition = patch_profile(
        &state,
        &owner,
        json!({
            "title": owner, "map_state": "not_on_map",
            "summary": "Gemeinsame Dinge",
            "tags": OWNER_TAGS,
        }),
    )
    .await
    .expect_err("legacy editor republishing moved tag");
    assert_eq!(legacy_without_precondition.0, StatusCode::CONFLICT);
    assert_eq!(stored_tags(&pool, &owner).await, expected_tags);

    // A fresh editor keeps working; other drafts/fields are still saved.
    let saved = patch_profile(
        &state,
        &owner,
        json!({
            "title": owner, "map_state": "not_on_map",
            "summary": "Neu",
            "tags": ["skill:Fahrrad reparieren", "interest:Commons", "good:Werkzeug"],
            "expected_tags": expected_tags,
        }),
    )
    .await
    .expect("fresh PATCH");
    assert!(saved.contains(&"good:Werkzeug".to_string()));
    assert!(!saved.contains(&"good:Leiter".to_string()));
    let marker: Value = sqlx::query_scalar(
        "SELECT private_payload->'moved_profile_tags' FROM domain_accounts WHERE id=$1",
    )
    .bind(&owner)
    .fetch_one(&pool)
    .await
    .expect("moved marker survives unrelated PATCH");
    assert_eq!(marker, json!(["good:Leiter"]));
    assert!(!public_account_json(&state, &owner, None)
        .await
        .contains("moved_profile_tags"));

    // P2b composition proof: a formerly confirmed circle member must lose
    // BOTH the cached direct node route and the account activity projection
    // when membership ends. The owner's converted node and public tags remain.
    assert!(get_node(
        State(state.clone()),
        Path(node_id.clone()),
        Some(Extension(actor(&member, Role::Weber))),
    )
    .await
    .is_ok());
    let version_before: i64 =
        sqlx::query_scalar("SELECT version FROM domain_projection_state WHERE singleton=TRUE")
            .fetch_one(&pool)
            .await
            .expect("projection version before departure");
    sqlx::query("DELETE FROM domain_audience_circle_members WHERE circle_id=$1 AND account_id=$2")
        .bind(circle)
        .bind(&member)
        .execute(&pool)
        .await
        .expect("former member leaves P2b audience circle");
    let version_after: i64 =
        sqlx::query_scalar("SELECT version FROM domain_projection_state WHERE singleton=TRUE")
            .fetch_one(&pool)
            .await
            .expect("projection version after departure");
    assert!(
        version_after > version_before,
        "departure invalidates privacy reads"
    );
    assert!(!readable(&pool, &node_id, Some(&member)).await);
    assert!(matches!(
        get_node(
            State(state.clone()),
            Path(node_id.clone()),
            Some(Extension(actor(&member, Role::Weber))),
        )
        .await,
        Err(StatusCode::NOT_FOUND)
    ));
    assert!(
        !public_account_json(&state, &owner, Some(&member))
            .await
            .contains("Leiter"),
        "former member must not see moved resource in account relations or activity"
    );
    assert!(readable(&pool, &node_id, Some(&owner)).await);
    // The earlier fresh PATCH added "good:Werkzeug": preserve CURRENT tags.
    assert_eq!(stored_tags(&pool, &owner).await, saved);
}

#[tokio::test]
#[ignore = "requires DATABASE_URL for an isolated disposable direct PostgreSQL database"]
async fn failed_grant_or_limit_leaves_tag_public_and_creates_nothing() {
    let pool = disposable_pool().await;
    let owner = Uuid::new_v4().to_string();
    let stranger = Uuid::new_v4().to_string();
    insert_account(&pool, &owner, "weber", &OWNER_TAGS).await;
    insert_account(&pool, &stranger, "weber", &["account", "garnrolle"]).await;
    let foreign_circle = active_circle(&pool, &[&stranger]).await;
    let own_circle = active_circle(&pool, &[&owner]).await;
    let state = pg_instance(&pool, 1_000).await;

    // A circle without the owner's active membership is not grantable.
    let denied = convert(
        &state,
        &owner,
        Role::Weber,
        move_request("good:Leiter", "circles", &[foreign_circle], Uuid::new_v4()),
    )
    .await
    .expect_err("foreign circle");
    assert_eq!(denied.0, StatusCode::NOT_FOUND);
    assert_eq!(
        stored_tags(&pool, &owner).await,
        OWNER_TAGS.map(String::from)
    );
    assert!(owned_node_ids(&pool, &owner).await.is_empty());

    // Inject a grant failure AFTER node and Faden INSERT inside the same
    // transaction. Everything, including the tag removal, must roll back.
    let function = format!("p2b_fail_grant_{}", Uuid::new_v4().simple());
    sqlx::query(&format!(
        "CREATE FUNCTION {function}() RETURNS TRIGGER LANGUAGE plpgsql AS $$
         BEGIN
           IF NEW.circle_id = '{own_circle}'::uuid THEN
             RAISE EXCEPTION 'injected p2b grant failure';
           END IF;
           RETURN NEW;
         END; $$"
    ))
    .execute(&pool)
    .await
    .expect("failure function");
    sqlx::query(&format!(
        "CREATE TRIGGER {function} BEFORE INSERT ON domain_node_circle_grants
         FOR EACH ROW EXECUTE FUNCTION {function}()"
    ))
    .execute(&pool)
    .await
    .expect("failure trigger");
    let failed = convert(
        &state,
        &owner,
        Role::Weber,
        move_request("good:Leiter", "circles", &[own_circle], Uuid::new_v4()),
    )
    .await
    .expect_err("injected grant failure");
    sqlx::query(&format!(
        "DROP TRIGGER {function} ON domain_node_circle_grants"
    ))
    .execute(&pool)
    .await
    .expect("drop failure trigger");
    sqlx::query(&format!("DROP FUNCTION {function}()"))
        .execute(&pool)
        .await
        .expect("drop failure function");
    assert_eq!(failed.0, StatusCode::INTERNAL_SERVER_ERROR);
    assert_eq!(
        stored_tags(&pool, &owner).await,
        OWNER_TAGS.map(String::from)
    );
    assert!(owned_node_ids(&pool, &owner).await.is_empty());
    let dangling: i64 = sqlx::query_scalar("SELECT count(*) FROM domain_edges WHERE source_id=$1")
        .bind(&owner)
        .fetch_one(&pool)
        .await
        .expect("edges");
    assert_eq!(dangling, 0, "no origin Faden without node and tag move");
    assert!(state.nodes.read().await.iter_in_order().next().is_none());

    // Guest node cap: fail closed, nothing moved.
    let guest = Uuid::new_v4().to_string();
    insert_account(&pool, &guest, "gast", &OWNER_TAGS).await;
    let capped = pg_instance(&pool, 0).await;
    let limited = convert(
        &capped,
        &guest,
        Role::Gast,
        move_request("skill:Fahrrad reparieren", "private", &[], Uuid::new_v4()),
    )
    .await
    .expect_err("guest limit");
    assert_eq!(limited.0, StatusCode::TOO_MANY_REQUESTS);
    assert_eq!(
        stored_tags(&pool, &guest).await,
        OWNER_TAGS.map(String::from)
    );

    // Marker capacity is fail-closed: nothing is evicted, nothing is moved.
    let full: Vec<String> = (0..256).map(|index| format!("good:alt-{index}")).collect();
    sqlx::query(
        "UPDATE domain_accounts SET private_payload = jsonb_build_object('moved_profile_tags', $2::jsonb) WHERE id=$1",
    )
    .bind(&owner)
    .bind(json!(full).to_string())
    .execute(&pool)
    .await
    .expect("seed full marker");
    let at_capacity = convert(
        &state,
        &owner,
        Role::Weber,
        move_request("good:Leiter", "private", &[], Uuid::new_v4()),
    )
    .await
    .expect_err("marker capacity reached");
    assert_eq!(at_capacity.0, StatusCode::CONFLICT);
    assert_eq!(
        stored_tags(&pool, &owner).await,
        OWNER_TAGS.map(String::from)
    );
    assert!(owned_node_ids(&pool, &owner).await.is_empty());
    let kept: Value = sqlx::query_scalar(
        "SELECT private_payload->'moved_profile_tags' FROM domain_accounts WHERE id=$1",
    )
    .bind(&owner)
    .fetch_one(&pool)
    .await
    .expect("marker");
    assert_eq!(kept, json!(full), "no marker may be evicted");
    // The oldest marker still blocks a legacy PATCH from republishing it.
    let legacy = patch_profile(
        &state,
        &owner,
        json!({"title": owner, "map_state": "not_on_map", "tags": ["good:alt-0"]}),
    )
    .await
    .expect_err("legacy PATCH with oldest marker");
    assert_eq!(legacy.0, StatusCode::CONFLICT);

    // Disabled account cannot move anything.
    sqlx::query("UPDATE domain_accounts SET disabled=TRUE WHERE id=$1")
        .bind(&owner)
        .execute(&pool)
        .await
        .expect("disable");
    let disabled = convert(
        &state,
        &owner,
        Role::Weber,
        move_request("good:Leiter", "private", &[], Uuid::new_v4()),
    )
    .await
    .expect_err("disabled account");
    assert_eq!(disabled.0, StatusCode::UNAUTHORIZED);
    assert!(owned_node_ids(&pool, &owner).await.is_empty());
}

#[tokio::test]
#[ignore = "requires DATABASE_URL for an isolated disposable direct PostgreSQL database"]
async fn concurrent_instances_cannot_lose_updates_or_duplicate_nodes() {
    let pool = disposable_pool().await;
    for round in 0..6 {
        let owner = Uuid::new_v4().to_string();
        insert_account(&pool, &owner, "weber", &OWNER_TAGS).await;
        let first = pg_instance(&pool, 1_000).await;
        let second = pg_instance(&pool, 1_000).await;

        // (a) Two instances replay the same operation concurrently.
        let operation = Uuid::new_v4();
        let (left, right) = tokio::join!(
            convert(
                &first,
                &owner,
                Role::Weber,
                move_request("good:Leiter", "private", &[], operation)
            ),
            convert(
                &second,
                &owner,
                Role::Weber,
                move_request("good:Leiter", "private", &[], operation)
            ),
        );
        let mut statuses = [
            left.expect("first instance").0,
            right.expect("second instance").0,
        ];
        statuses.sort();
        assert_eq!(
            statuses,
            [StatusCode::OK, StatusCode::CREATED],
            "round {round}"
        );
        assert_eq!(owned_node_ids(&pool, &owner).await.len(), 1);

        // (b) A different tag moves while a stale full-profile PATCH races.
        let stale_tags = without(&OWNER_TAGS, "good:Leiter");
        let (moved, patched) = tokio::join!(
            convert(
                &first,
                &owner,
                Role::Weber,
                move_request("skill:Fahrrad reparieren", "public", &[], Uuid::new_v4())
            ),
            patch_profile(
                &second,
                &owner,
                json!({
                    "title": "Parallel", "map_state": "not_on_map",
                    "tags": stale_tags,
                    "expected_tags": stale_tags,
                })
            ),
        );
        let moved = moved.expect("conversion always wins or follows the PATCH");
        assert_eq!(moved.0, StatusCode::CREATED);
        if let Err(error) = &patched {
            assert_eq!(error.0, StatusCode::CONFLICT, "round {round}");
        }
        let final_tags = stored_tags(&pool, &owner).await;
        assert!(
            !final_tags.contains(&"skill:Fahrrad reparieren".to_string()),
            "round {round}: concurrent PATCH republished moved tag: {final_tags:?}"
        );
        assert!(!final_tags.contains(&"good:Leiter".to_string()));
        assert!(final_tags.contains(&"interest:Commons".to_string()));
        assert_eq!(owned_node_ids(&pool, &owner).await.len(), 2);
        let title: String = sqlx::query_scalar("SELECT title FROM domain_accounts WHERE id=$1")
            .bind(&owner)
            .fetch_one(&pool)
            .await
            .expect("title");
        assert_eq!(
            title == "Parallel",
            patched.is_ok(),
            "round {round}: PATCH outcome must match durable title"
        );
    }

    // (c) Two different tags of one account move concurrently: no lost update.
    let owner = Uuid::new_v4().to_string();
    insert_account(&pool, &owner, "weber", &OWNER_TAGS).await;
    let first = pg_instance(&pool, 1_000).await;
    let second = pg_instance(&pool, 1_000).await;
    let (left, right) = tokio::join!(
        convert(
            &first,
            &owner,
            Role::Weber,
            move_request("good:Leiter", "private", &[], Uuid::new_v4())
        ),
        convert(
            &second,
            &owner,
            Role::Weber,
            move_request("skill:Fahrrad reparieren", "private", &[], Uuid::new_v4())
        ),
    );
    assert_eq!(left.expect("left").0, StatusCode::CREATED);
    assert_eq!(right.expect("right").0, StatusCode::CREATED);
    assert_eq!(
        stored_tags(&pool, &owner).await,
        vec!["account", "garnrolle", "interest:Commons"]
    );
    let marker: Value = sqlx::query_scalar(
        "SELECT private_payload->'moved_profile_tags' FROM domain_accounts WHERE id=$1",
    )
    .bind(&owner)
    .fetch_one(&pool)
    .await
    .expect("marker");
    let mut marker: Vec<String> = serde_json::from_value(marker).expect("marker list");
    marker.sort();
    assert_eq!(marker, vec!["good:Leiter", "skill:Fahrrad reparieren"]);
}

fn with_tag(tags: &[String], extra: &str) -> Vec<String> {
    let mut all = tags.to_vec();
    all.push(extra.to_string());
    all
}

/// A browser opened before the update has no `expected_tags` and re-sends every
/// field through the comma/trim form, so `good: Leiter` returns as `good:Leiter`
/// and `good:Hammer, Bohrer` as two tags. None of them may become public again.
#[tokio::test]
#[ignore = "requires DATABASE_URL for an isolated disposable direct PostgreSQL database"]
async fn legacy_form_projection_cannot_republish_moved_tags() {
    let pool = disposable_pool().await;
    let owner = Uuid::new_v4().to_string();
    let saved = [
        "account",
        "garnrolle",
        "good: Leiter",
        "good:Hammer, Bohrer",
        "skill: Fahrrad reparieren",
        "interest:Commons",
    ];
    insert_account(&pool, &owner, "weber", &saved).await;
    let state = pg_instance(&pool, 1_000).await;
    for tag in [
        "good: Leiter",
        "good:Hammer, Bohrer",
        "skill: Fahrrad reparieren",
    ] {
        let (status, _) = convert(
            &state,
            &owner,
            Role::Weber,
            move_request(tag, "private", &[], Uuid::new_v4()),
        )
        .await
        .expect("move saved tag");
        assert_eq!(status, StatusCode::CREATED);
    }
    let remaining = vec!["account", "garnrolle", "interest:Commons"];
    assert_eq!(stored_tags(&pool, &owner).await, remaining);

    let marker = || async {
        let marker: Value = sqlx::query_scalar(
            "SELECT private_payload->'moved_profile_tags' FROM domain_accounts WHERE id=$1",
        )
        .bind(&owner)
        .fetch_one(&pool)
        .await
        .expect("moved marker");
        let mut marker: Vec<String> = serde_json::from_value(marker).expect("marker list");
        marker.sort();
        marker
    };
    let expected_marker = vec![
        "good: Leiter",
        "good:Hammer, Bohrer",
        "skill: Fahrrad reparieren",
    ];
    assert_eq!(marker().await, expected_marker);

    let legacy = |tags: &[&str]| {
        let mut all = remaining.clone();
        all.extend_from_slice(tags);
        json!({"title": owner, "map_state": "not_on_map", "tags": all})
    };
    for republished in [
        vec!["good:Leiter"],
        vec!["good:Hammer"],
        vec!["good:Bohrer"],
        vec!["good:Hammer", "good:Bohrer"],
        vec!["skill:Fahrrad reparieren"],
        vec!["good: Leiter"],
        vec!["good:Hammer, Bohrer"],
        vec!["good:Werkzeug", "good:Bohrer"],
    ] {
        let error = patch_profile(&state, &owner, legacy(&republished))
            .await
            .expect_err("legacy PATCH republishing a moved tag");
        assert_eq!(error.0, StatusCode::CONFLICT, "{republished:?}");
        assert_eq!(stored_tags(&pool, &owner).await, remaining);
        assert_eq!(marker().await, expected_marker);
    }

    // Unrelated tags, including other prefixes with the same words, still save.
    let saved_other = patch_profile(
        &state,
        &owner,
        legacy(&["good:Leiterwagen", "skill:Leiter", "interest:Hammer"]),
    )
    .await
    .expect("legacy PATCH without moved tags");
    assert!(saved_other.contains(&"good:Leiterwagen".to_string()));
    assert!(saved_other.contains(&"skill:Leiter".to_string()));
    assert_eq!(marker().await, expected_marker);

    // Another tag was published in between; the replay is still blocked and
    // the markers survive.
    let current = stored_tags(&pool, &owner).await;
    patch_profile(
        &state,
        &owner,
        json!({
            "title": owner, "map_state": "not_on_map",
            "tags": with_tag(&current, "good:Werkzeug"),
            "expected_tags": current,
        }),
    )
    .await
    .expect("fresh editor publishes another tag");
    let before_replay = stored_tags(&pool, &owner).await;
    let replay = patch_profile(
        &state,
        &owner,
        json!({
            "title": owner, "map_state": "not_on_map",
            "tags": with_tag(&before_replay, "good:Leiter"),
        }),
    )
    .await
    .expect_err("replayed legacy PATCH");
    assert_eq!(replay.0, StatusCode::CONFLICT);
    assert_eq!(stored_tags(&pool, &owner).await, before_replay);
    assert!(before_replay.contains(&"good:Werkzeug".to_string()));
    assert_eq!(marker().await, expected_marker);

    // Re-sending already public tags is retention, not republication.
    patch_profile(
        &state,
        &owner,
        json!({"title": owner, "map_state": "not_on_map", "tags": before_replay}),
    )
    .await
    .expect("legacy PATCH keeping the public tags");
    assert_eq!(marker().await, expected_marker);

    // A deliberate republish with a real baseline still works for the exact tag.
    patch_profile(
        &state,
        &owner,
        json!({
            "title": owner, "map_state": "not_on_map",
            "tags": with_tag(&before_replay, "good: Leiter"),
            "expected_tags": before_replay,
        }),
    )
    .await
    .expect("deliberate republish with expected_tags");
    assert!(stored_tags(&pool, &owner)
        .await
        .contains(&"good: Leiter".to_string()));
    assert_eq!(
        marker().await,
        vec!["good:Hammer, Bohrer", "skill: Fahrrad reparieren"]
    );
    let public = public_account_json(&state, &owner, None).await;
    assert!(!public.contains("moved_profile_tags"));
    assert!(!public.contains("Bohrer"));
}

/// JSONL cannot atomically move a tag across account and node files. It must
/// say so (503) instead of silently writing half of the operation.
#[tokio::test]
async fn jsonl_mode_rejects_profile_tag_move_without_writing() {
    let state = instance(None, config(false, 1_000));
    let error = move_profile_tag_to_resource(
        State(state.clone()),
        Extension(actor("jsonl-owner", Role::Weber)),
        Json(move_request("good:Leiter", "private", &[], Uuid::new_v4())),
    )
    .await
    .expect_err("JSONL is unsupported");
    assert_eq!(error.0, StatusCode::SERVICE_UNAVAILABLE);
    assert!(state.nodes.read().await.iter_in_order().next().is_none());

    let unauthenticated = move_profile_tag_to_resource(
        State(state),
        Extension(AuthContext {
            authenticated: false,
            account_id: None,
            device_id: None,
            role: Role::Gast,
            expires_at: None,
        }),
        Json(move_request("good:Leiter", "private", &[], Uuid::new_v4())),
    )
    .await
    .expect_err("anonymous");
    assert_eq!(unauthenticated.0, StatusCode::UNAUTHORIZED);
}

/// Same middleware sequence as the production router, on an isolated
/// PostgreSQL fixture. Separate browsers are modelled by real cookie sessions.
fn p2b_session_router(state: ApiState) -> Router {
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

async fn p2b_http(
    app: &Router,
    method: Method,
    path: &str,
    session_cookie: Option<&str>,
    payload: Option<Value>,
) -> (StatusCode, String) {
    let mut request = Request::builder()
        .method(method)
        .uri(path)
        .header(header::HOST, "localhost")
        .header(header::ORIGIN, "http://localhost");
    if let Some(cookie) = session_cookie {
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
        .oneshot(request.body(body).expect("HTTP request"))
        .await
        .expect("HTTP response");
    let status = response.status();
    let bytes = to_bytes(response.into_body(), usize::MAX)
        .await
        .expect("HTTP response bytes");
    (status, String::from_utf8_lossy(&bytes).into_owned())
}

async fn p2b_login_cookie(app: &Router, account_id: &str) -> String {
    let request = Request::builder()
        .method(Method::POST)
        .uri("/auth/dev/login")
        .header(header::HOST, "localhost")
        .header(header::ORIGIN, "http://localhost")
        .header(header::CONTENT_TYPE, "application/json")
        .body(Body::from(json!({"account_id": account_id}).to_string()))
        .expect("login request");
    let response = app.clone().oneshot(request).await.expect("login response");
    assert_eq!(response.status(), StatusCode::OK, "test-only local login");
    response
        .headers()
        .get(header::SET_COOKIE)
        .expect("login produces real session cookie")
        .to_str()
        .expect("ASCII session cookie")
        .split(';')
        .next()
        .expect("cookie pair")
        .to_string()
}

/// Real HTTP authentication, canonical PostgreSQL, and production middleware
/// ordering. The only shortcut is a dev-login restricted to the local proof.
#[tokio::test]
#[serial]
#[ignore = "requires disposable direct PostgreSQL DATABASE_URL"]
async fn converted_tag_is_scoped_by_real_http_sessions_and_live_circle_departure() {
    let _dev_login = EnvGuard::set("AUTH_DEV_LOGIN", "1");
    let _remote_login = EnvGuard::unset("AUTH_DEV_LOGIN_ALLOW_REMOTE");
    let pool = disposable_pool().await;
    let owner = Uuid::new_v4().to_string();
    let member = Uuid::new_v4().to_string();
    let outsider = Uuid::new_v4().to_string();
    let guest = Uuid::new_v4().to_string();
    let tag = format!("good:P2b-session-proof-{}", Uuid::new_v4().simple());
    let title = tag.trim_start_matches("good:");
    insert_account(
        &pool,
        &owner,
        "weber",
        &["account", "garnrolle", &tag, "interest:Commons"],
    )
    .await;
    for (id, role) in [(&member, "weber"), (&outsider, "weber"), (&guest, "gast")] {
        insert_account(&pool, id, role, &["account", "garnrolle"]).await;
    }
    let circle = active_circle(&pool, &[&owner, &member]).await;

    let mut state = pg_instance(&pool, 1_000).await;
    state.sessions = SessionBackend::new(DbSessionStore::new(pool.clone()));
    state.config.auth_cookie_secure = false; // loopback HTTP proof only
    let app = p2b_session_router(state);
    let owner_cookie = p2b_login_cookie(&app, &owner).await;
    let member_cookie = p2b_login_cookie(&app, &member).await;
    let outsider_cookie = p2b_login_cookie(&app, &outsider).await;
    let guest_cookie = p2b_login_cookie(&app, &guest).await;

    for (account, cookie) in [
        (&owner, &owner_cookie),
        (&member, &member_cookie),
        (&outsider, &outsider_cookie),
        (&guest, &guest_cookie),
    ] {
        let (status, body) = p2b_http(&app, Method::GET, "/auth/me", Some(cookie), None).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(
            serde_json::from_str::<Value>(&body).expect("auth JSON")["account_id"],
            account.as_str(),
            "every independent session must resolve its own account"
        );
    }

    let (anonymous, _) = p2b_http(
        &app,
        Method::POST,
        "/accounts/me/profile/resources",
        None,
        Some(json!({
            "tag": tag,
            "scope": "private",
            "circle_ids": [],
            "operation_id": Uuid::new_v4(),
        })),
    )
    .await;
    assert_eq!(anonymous, StatusCode::UNAUTHORIZED);

    let (created_status, created_body) = p2b_http(
        &app,
        Method::POST,
        "/accounts/me/profile/resources",
        Some(&owner_cookie),
        Some(json!({
            "tag": tag,
            "scope": "circles",
            "circle_ids": [circle],
            "operation_id": Uuid::new_v4(),
        })),
    )
    .await;
    assert_eq!(created_status, StatusCode::CREATED, "{created_body}");
    let response: Value = serde_json::from_str(&created_body).expect("created JSON");
    let node_id = response["node"]["id"]
        .as_str()
        .expect("converted node has one ID");
    assert_eq!(response["audience"]["scope"], "circles");
    assert_eq!(
        owned_node_ids(&pool, &owner).await,
        vec![node_id.to_string()]
    );
    assert!(!stored_tags(&pool, &owner).await.contains(&tag));

    let node_path = format!("/nodes/{node_id}");
    let account_path = format!("/accounts/{owner}");
    for (label, cookie, can_read) in [
        ("owner", Some(owner_cookie.as_str()), true),
        ("confirmed-member", Some(member_cookie.as_str()), true),
        ("outsider", Some(outsider_cookie.as_str()), false),
        ("guest", Some(guest_cookie.as_str()), false),
        ("anonymous", None, false),
    ] {
        let (status, _) = p2b_http(&app, Method::GET, &node_path, cookie, None).await;
        assert_eq!(
            status,
            if can_read {
                StatusCode::OK
            } else {
                StatusCode::NOT_FOUND
            },
            "{label} direct-link privacy"
        );
        let (list_status, list) =
            p2b_http(&app, Method::GET, "/nodes?limit=100", cookie, None).await;
        assert_eq!(list_status, StatusCode::OK, "{label} list: {list}");
        assert_eq!(list.contains(node_id), can_read, "{label} node list");
        // Even a confirmed circle member must never see a non-public
        // resource through the explicit public-only discovery contract.
        let (public_status, public_list) = p2b_http(
            &app,
            Method::GET,
            "/nodes?public_only=true&limit=100",
            cookie,
            None,
        )
        .await;
        assert_eq!(public_status, StatusCode::OK, "{label} public-only list");
        assert!(
            !public_list.contains(node_id),
            "{label} public-only discovery exposed a private circle resource"
        );
        let (account_status, account) =
            p2b_http(&app, Method::GET, &account_path, cookie, None).await;
        assert_eq!(account_status, StatusCode::OK, "{label} account");
        assert_eq!(
            account.contains(title),
            can_read,
            "{label} account relations and activity"
        );
    }

    // A formerly authorized member exits via the real authenticated,
    // CSRF-protected endpoint. The next GET must reject its *existing* cookie.
    let departure_path = format!("/circles/{circle}/membership");
    let (leave_status, leave_body) = p2b_http(
        &app,
        Method::DELETE,
        &departure_path,
        Some(&member_cookie),
        None,
    )
    .await;
    assert_eq!(leave_status, StatusCode::NO_CONTENT, "{leave_body}");
    let (former_direct, _) =
        p2b_http(&app, Method::GET, &node_path, Some(&member_cookie), None).await;
    assert_eq!(
        former_direct,
        StatusCode::NOT_FOUND,
        "former member direct link"
    );
    for path in [
        "/nodes?limit=100",
        "/nodes?public_only=true&limit=100",
        account_path.as_str(),
    ] {
        let (status, body) = p2b_http(&app, Method::GET, path, Some(&member_cookie), None).await;
        assert_eq!(status, StatusCode::OK);
        assert!(!body.contains(title), "former member leaked via {path}");
        assert!(
            !body.contains(node_id),
            "former member can still enumerate private node ID via {path}"
        );
    }
    let (owner_status, _) =
        p2b_http(&app, Method::GET, &node_path, Some(&owner_cookie), None).await;
    assert_eq!(
        owner_status,
        StatusCode::OK,
        "owner retains private resource"
    );
    assert!(
        stored_tags(&pool, &owner)
            .await
            .contains(&"interest:Commons".to_string()),
        "unrelated public tag survives authenticated conversion and departure"
    );
}
