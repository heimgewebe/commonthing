use anyhow::{Context, Result};
use axum::{
    body,
    http::{Request, StatusCode},
    Router,
};
use chrono::{Duration, SecondsFormat, Utc};
mod helpers;

use helpers::{
    assert_account_has_single_node_relation, read_account_details, set_gewebe_in_dir, test_node,
    TEST_NODE_TIMESTAMP,
};
use serial_test::serial;
use std::{fs, sync::Arc};
use tokio::sync::RwLock;
use tower::ServiceExt;
use weltgewebe_api::{
    auth::{
        accounts::AccountStore, rate_limit::AuthRateLimiter, role::Role, session::SessionBackend,
    },
    config::AppConfig,
    routes::{
        accounts::{AccountInternal, AccountPublic},
        api_router,
        edges::Edge,
        nodes,
    },
    state::{ApiState, OrderedCache},
    telemetry::{BuildInfo, Metrics},
};

fn active_faden_timestamps() -> (String, String) {
    let created_at = Utc::now() - Duration::hours(1);
    let expires_at = created_at + Duration::hours(168);
    (
        created_at.to_rfc3339_opts(SecondsFormat::Micros, true),
        expires_at.to_rfc3339_opts(SecondsFormat::Micros, true),
    )
}

async fn test_state() -> Result<ApiState> {
    let metrics = Metrics::try_new(BuildInfo {
        version: "test",
        commit: "test",
        build_timestamp: "test",
    })?;

    let config = AppConfig {
        max_guest_owned_nodes: 1_000,
        domain_read_source: weltgewebe_api::config::DomainReadSource::Jsonl,
        domain_account_write_source: weltgewebe_api::config::DomainAccountWriteSource::Jsonl,
        domain_node_write_source: weltgewebe_api::config::DomainNodeWriteSource::Jsonl,
        domain_edge_write_source: weltgewebe_api::config::DomainEdgeWriteSource::Jsonl,
        passkey_credential_source: weltgewebe_api::config::PasskeyCredentialSource::InMemory,
        auth_public_login: false,
        auth_cookie_secure: weltgewebe_api::config::auth_cookie_secure_env_override()
            .unwrap_or(true),
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

    Ok(ApiState {
        db_pool: None,
        db_pool_configured: false,
        nats_client: None,
        nats_configured: false,
        config,
        metrics,
        sessions: SessionBackend::new_in_memory(),
        challenges: Default::default(),
        tokens: weltgewebe_api::auth::tokens::TokenStore::new(),
        step_up_tokens: weltgewebe_api::auth::step_up_tokens::StepUpTokenStore::new(),
        accounts: Arc::new(RwLock::new(AccountStore::new())),
        nodes: Arc::new(tokio::sync::RwLock::new(
            weltgewebe_api::state::OrderedCache::new(),
        )),
        nodes_persist: Arc::new(tokio::sync::Mutex::new(())),
        accounts_persist: Arc::new(tokio::sync::Mutex::new(())),
        domain_projection_gate: std::sync::Arc::new(tokio::sync::RwLock::new(())),
        domain_projection_reload: std::sync::Arc::new(tokio::sync::Mutex::new(())),
        domain_projection_local_node_patch_handoff: std::sync::Arc::new(
            std::sync::atomic::AtomicI64::new(-1),
        ),
        domain_projection_version: std::sync::Arc::new(std::sync::atomic::AtomicI64::new(0)),
        edges: Arc::new(tokio::sync::RwLock::new(
            weltgewebe_api::state::OrderedCache::new(),
        )),
        rate_limiter,
        mailer: None,
        webauthn: None,
        passkey_registrations: Default::default(),
        passkey_registration_grants: Default::default(),
        passkey_authentications: Default::default(),
        passkeys: Default::default(),
        web_push: None,
    })
}

#[tokio::test]
async fn accounts_list_is_sorted_and_limited() -> Result<()> {
    let mut state = test_state().await?;
    let mut accounts = AccountStore::new();

    // Insert accounts in unsorted order: u2, a1, u1
    // Expected sort order (lexicographical by ID): a1, u1, u2
    let ids = vec!["u2", "a1", "u1"];

    for id in ids {
        accounts.insert(AccountInternal {
            public: AccountPublic {
                id: id.to_string(),
                kind: "garnrolle".to_string(),
                title: format!("Title {}", id),
                summary: None,
                public_pos: None,
                map_state: weltgewebe_api::routes::accounts::GarnrolleMapState::NotOnMap,
                radius_m: 0,

                disabled: false,
                tags: vec![],
            },
            role: Role::Weber,
            email: None,
            webauthn_user_id: uuid::Uuid::new_v4(),
        });
    }

    state.accounts = Arc::new(RwLock::new(accounts));

    let app = Router::new().merge(api_router()).with_state(state);

    // Request limit=2. Expect "a1", "u1".
    let req = Request::get("/accounts?limit=2").body(body::Body::empty())?;
    let res = app.oneshot(req).await?;

    assert_eq!(res.status(), StatusCode::OK);

    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let v: serde_json::Value = serde_json::from_slice(&body)?;
    let arr = v.as_array().context("must be array")?;

    assert_eq!(arr.len(), 2, "Should return exactly 2 accounts");
    assert_eq!(arr[0]["id"], "a1", "First account should be a1");
    assert_eq!(arr[1]["id"], "u1", "Second account should be u1");

    Ok(())
}

#[tokio::test]
async fn accounts_offset_pagination() -> Result<()> {
    let mut state = test_state().await?;
    let mut accounts = AccountStore::new();

    // Insert in unsorted order; BTreeMap sorts lexicographically: a1, b2, c3
    for id in &["b2", "a1", "c3"] {
        accounts.insert(AccountInternal {
            public: AccountPublic {
                id: id.to_string(),
                kind: "garnrolle".to_string(),
                title: format!("Title {}", id),
                summary: None,
                public_pos: None,
                map_state: weltgewebe_api::routes::accounts::GarnrolleMapState::NotOnMap,
                radius_m: 0,
                disabled: false,
                tags: vec![],
            },
            role: Role::Weber,
            email: None,
            webauthn_user_id: uuid::Uuid::new_v4(),
        });
    }

    state.accounts = Arc::new(RwLock::new(accounts));
    let app = Router::new().merge(api_router()).with_state(state);

    // limit=1&offset=0 liefert erstes Element (a1 — BTreeMap sort)
    let req = Request::get("/accounts?limit=1&offset=0").body(body::Body::empty())?;
    let res = app.clone().oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);
    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let v: serde_json::Value = serde_json::from_slice(&body)?;
    let arr = v.as_array().context("must be array")?;
    assert_eq!(arr.len(), 1);
    assert_eq!(arr[0]["id"], "a1");

    // limit=1&offset=1 liefert zweites Element (b2)
    let req = Request::get("/accounts?limit=1&offset=1").body(body::Body::empty())?;
    let res = app.clone().oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);
    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let v: serde_json::Value = serde_json::from_slice(&body)?;
    let arr = v.as_array().context("must be array")?;
    assert_eq!(arr.len(), 1);
    assert_eq!(arr[0]["id"], "b2");

    // Offset außerhalb der Ergebnislänge liefert leere Liste
    let req = Request::get("/accounts?limit=10&offset=100").body(body::Body::empty())?;
    let res = app.clone().oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);
    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let v: serde_json::Value = serde_json::from_slice(&body)?;
    let arr = v.as_array().context("must be array")?;
    assert_eq!(arr.len(), 0);

    // Ungültiger Offset (negativ) -> 400
    let req = Request::get("/accounts?offset=-1").body(body::Body::empty())?;
    let res = app.clone().oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);

    // Ungültiger Offset (kein Integer) -> 400
    let req = Request::get("/accounts?offset=abc").body(body::Body::empty())?;
    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);

    Ok(())
}

#[tokio::test]
async fn accounts_invalid_limit() -> Result<()> {
    let mut state = test_state().await?;
    let mut accounts = AccountStore::new();

    accounts.insert(AccountInternal {
        public: AccountPublic {
            id: "a1".to_string(),
            kind: "garnrolle".to_string(),
            title: "Title a1".to_string(),
            summary: None,
            public_pos: None,
            map_state: weltgewebe_api::routes::accounts::GarnrolleMapState::NotOnMap,
            radius_m: 0,
            disabled: false,
            tags: vec![],
        },
        role: Role::Weber,
        email: None,
        webauthn_user_id: uuid::Uuid::new_v4(),
    });

    state.accounts = Arc::new(RwLock::new(accounts));
    let app = Router::new().merge(api_router()).with_state(state);

    // limit=abc -> 400
    let req = Request::get("/accounts?limit=abc").body(body::Body::empty())?;
    let res = app.clone().oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);

    // limit=-1 -> 400
    let req = Request::get("/accounts?limit=-1").body(body::Body::empty())?;
    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);

    Ok(())
}

#[tokio::test]
async fn accounts_limit_is_clamped_to_max_page_size() -> Result<()> {
    let mut state = test_state().await?;
    let mut accounts = AccountStore::new();

    // Seed more accounts than the page-size cap so the clamp is observable.
    for i in 0..1100 {
        accounts.insert(AccountInternal {
            public: AccountPublic {
                id: format!("a{i:04}"),
                kind: "garnrolle".to_string(),
                title: format!("Title {i}"),
                summary: None,
                public_pos: None,
                map_state: weltgewebe_api::routes::accounts::GarnrolleMapState::NotOnMap,
                radius_m: 0,
                disabled: false,
                tags: vec![],
            },
            role: Role::Weber,
            email: None,
            webauthn_user_id: uuid::Uuid::new_v4(),
        });
    }

    state.accounts = Arc::new(RwLock::new(accounts));
    let app = Router::new().merge(api_router()).with_state(state);

    // A limit above MAX_PAGE_SIZE (1000) must be clamped, mirroring /edges.
    let req = Request::get("/accounts?limit=5000").body(body::Body::empty())?;
    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);
    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let v: serde_json::Value = serde_json::from_slice(&body)?;
    assert_eq!(v.as_array().context("must be array")?.len(), 1000);

    Ok(())
}

fn seed_account(id: &str) -> AccountInternal {
    AccountInternal {
        public: AccountPublic {
            id: id.to_string(),
            kind: "garnrolle".to_string(),
            title: format!("Title {id}"),
            summary: None,
            public_pos: None,
            map_state: weltgewebe_api::routes::accounts::GarnrolleMapState::NotOnMap,
            radius_m: 0,
            disabled: false,
            tags: vec![],
        },
        role: Role::Weber,
        email: None,
        webauthn_user_id: uuid::Uuid::new_v4(),
    }
}

#[tokio::test]
async fn accounts_cursor_pagination_envelope_and_walk() -> Result<()> {
    let mut state = test_state().await?;
    let mut accounts = AccountStore::new();

    // Insert unsorted; BTreeMap + cursor_page both yield id-ascending order.
    for id in &["a3", "a1", "a5", "a2", "a4"] {
        accounts.insert(seed_account(id));
    }
    state.accounts = Arc::new(RwLock::new(accounts));
    let app = Router::new().merge(api_router()).with_state(state);

    // Page 1: envelope with the first two ids.
    let req = Request::get("/accounts?pagination=cursor&limit=2").body(body::Body::empty())?;
    let res = app.clone().oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);
    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let v: serde_json::Value = serde_json::from_slice(&body)?;
    let items = v["items"].as_array().context("items must be array")?;
    assert_eq!(items.len(), 2);
    assert_eq!(items[0]["id"], "a1");
    assert_eq!(items[1]["id"], "a2");
    assert_eq!(v["page"]["has_more"], true);
    let cursor1 = v["page"]["next_cursor"]
        .as_str()
        .context("next_cursor must be present on page 1")?
        .to_string();

    // Page 2: next id-ordered slice, no duplicates from page 1.
    let uri = format!("/accounts?cursor={cursor1}&limit=2");
    let res = app
        .clone()
        .oneshot(Request::get(uri.as_str()).body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::OK);
    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let v: serde_json::Value = serde_json::from_slice(&body)?;
    let items = v["items"].as_array().context("items must be array")?;
    assert_eq!(items.len(), 2);
    assert_eq!(items[0]["id"], "a3");
    assert_eq!(items[1]["id"], "a4");
    let cursor2 = v["page"]["next_cursor"]
        .as_str()
        .context("next_cursor must be present on page 2")?
        .to_string();

    // Page 3 (last): single remaining id, has_more false, next_cursor null.
    let uri = format!("/accounts?cursor={cursor2}&limit=2");
    let res = app
        .oneshot(Request::get(uri.as_str()).body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::OK);
    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let v: serde_json::Value = serde_json::from_slice(&body)?;
    let items = v["items"].as_array().context("items must be array")?;
    assert_eq!(items.len(), 1);
    assert_eq!(items[0]["id"], "a5");
    assert_eq!(v["page"]["has_more"], false);
    assert!(v["page"]["next_cursor"].is_null());

    Ok(())
}

#[tokio::test]
async fn accounts_cursor_invalid_is_bad_request() -> Result<()> {
    let mut state = test_state().await?;
    let mut accounts = AccountStore::new();
    accounts.insert(seed_account("a1"));
    state.accounts = Arc::new(RwLock::new(accounts));
    let app = Router::new().merge(api_router()).with_state(state);

    // Odd-length / non-hex cursor token -> deterministic 400.
    let req = Request::get("/accounts?cursor=zzz").body(body::Body::empty())?;
    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);

    Ok(())
}

#[tokio::test]
async fn accounts_cursor_limit_is_clamped_to_max_page_size() -> Result<()> {
    let mut state = test_state().await?;
    let mut accounts = AccountStore::new();
    for i in 0..1100 {
        accounts.insert(seed_account(&format!("a{i:04}")));
    }
    state.accounts = Arc::new(RwLock::new(accounts));
    let app = Router::new().merge(api_router()).with_state(state);

    let req = Request::get("/accounts?pagination=cursor&limit=5000").body(body::Body::empty())?;
    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);
    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let v: serde_json::Value = serde_json::from_slice(&body)?;
    let items = v["items"].as_array().context("items must be array")?;
    assert_eq!(items.len(), 1000);
    assert_eq!(v["page"]["limit"], 1000);
    assert_eq!(v["page"]["has_more"], true);

    Ok(())
}

#[tokio::test]
async fn accounts_cursor_limit_zero_is_bad_request() -> Result<()> {
    let state = test_state().await?;
    let app = Router::new().merge(api_router()).with_state(state);

    let req = Request::get("/accounts?pagination=cursor&limit=0").body(body::Body::empty())?;
    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);

    Ok(())
}

#[tokio::test]
async fn account_details_project_outgoing_relation_without_public_note() -> Result<()> {
    const ACCOUNT_ID: &str = "account-1";
    const NODE_ID: &str = "node-1";
    let (created_at, expires_at) = active_faden_timestamps();

    let mut state = test_state().await?;
    let mut accounts = AccountStore::new();
    let mut account = seed_account(ACCOUNT_ID);
    account.public.title = "Alexander Mohr".to_string();
    account.public.summary = Some("schaunmermal".to_string());
    accounts.insert(account);
    state.accounts = Arc::new(RwLock::new(accounts));

    let mut nodes = OrderedCache::new();
    let mut node = test_node(NODE_ID, "fairschenkbox", Some("sharing is caring"));
    node.created_by_account_id = Some(ACCOUNT_ID.to_string());
    nodes.insert(NODE_ID.to_string(), node);
    state.nodes = Arc::new(RwLock::new(nodes));

    let mut edges = OrderedCache::new();
    edges.insert(
        "edge-1".to_string(),
        Edge {
            id: "edge-1".to_string(),
            source_id: ACCOUNT_ID.to_string(),
            source_type: Some("account".to_string()),
            target_id: NODE_ID.to_string(),
            target_type: Some("node".to_string()),
            edge_kind: "reference".to_string(),
            faden_type: None,
            faden_subject_id: None,
            note: Some("nicht öffentlich".to_string()),
            created_at: Some(created_at.clone().into()),
            expires_at: Some(Some(expires_at.into())),
        },
    );
    state.edges = Arc::new(RwLock::new(edges));

    let app = Router::new().merge(api_router()).with_state(state);
    let value = read_account_details(&app, ACCOUNT_ID).await?;

    assert_eq!(value["title"], "Alexander Mohr");
    let projected_at = assert_account_has_single_node_relation(
        &value,
        ACCOUNT_ID,
        NODE_ID,
        "fairschenkbox",
        "reference",
        "Hat den Knoten \"fairschenkbox\" geknüpft.",
    );
    assert_eq!(projected_at, TEST_NODE_TIMESTAMP);

    Ok(())
}

#[tokio::test]
async fn account_details_omit_expired_faden_projection() -> Result<()> {
    const ACCOUNT_ID: &str = "account-expired";
    const NODE_ID: &str = "node-expired";

    let mut state = test_state().await?;
    let mut accounts = AccountStore::new();
    accounts.insert(seed_account(ACCOUNT_ID));
    state.accounts = Arc::new(RwLock::new(accounts));

    let mut nodes = OrderedCache::new();
    let mut node = test_node(NODE_ID, "Vergangener Knoten", None);
    node.created_by_account_id = Some(ACCOUNT_ID.to_string());
    nodes.insert(NODE_ID.to_string(), node);
    state.nodes = Arc::new(RwLock::new(nodes));

    let mut edges = OrderedCache::new();
    edges.insert(
        "edge-expired".to_string(),
        Edge {
            id: "edge-expired".to_string(),
            source_id: ACCOUNT_ID.to_string(),
            source_type: Some("account".to_string()),
            target_id: NODE_ID.to_string(),
            target_type: Some("node".to_string()),
            edge_kind: "reference".to_string(),
            faden_type: None,
            faden_subject_id: None,
            note: None,
            created_at: Some("2020-01-01T00:00:00Z".to_string().into()),
            expires_at: Some(Some("2020-01-08T00:00:00Z".to_string().into())),
        },
    );
    state.edges = Arc::new(RwLock::new(edges));

    let app = Router::new().merge(api_router()).with_state(state);
    let value = read_account_details(&app, ACCOUNT_ID).await?;
    assert_eq!(value["nodes"].as_array().map(Vec::len), Some(0));
    assert_eq!(value["activity"].as_array().map(Vec::len), Some(1));
    assert_eq!(
        value["activity"][0]["event"],
        "Hat den Knoten \"Vergangener Knoten\" geknüpft."
    );
    assert_eq!(value["activity"][0]["date"], TEST_NODE_TIMESTAMP);

    Ok(())
}

#[tokio::test]
#[serial]
async fn account_details_skip_jsonl_creation_without_original_timestamp() -> Result<()> {
    const ACCOUNT_ID: &str = "account-undated-jsonl";
    const NODE_ID: &str = "node-undated-jsonl";
    const UPDATED_AT: &str = "2026-07-13T05:00:00Z";

    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);
    fs::write(
        in_dir.join("demo.nodes.jsonl"),
        format!(
            "{{\"id\":\"{NODE_ID}\",\"kind\":\"resource\",\"title\":\"Undatierter Knoten\",\"updated_at\":\"{UPDATED_AT}\",\"created_by_account_id\":\"{ACCOUNT_ID}\",\"location\":{{\"lat\":53.5,\"lon\":10.0}}}}\n"
        ),
    )?;

    let loaded_nodes = nodes::load_nodes().await;
    let loaded = loaded_nodes.get(NODE_ID).context("legacy node must load")?;
    assert_eq!(
        loaded.created_at, UPDATED_AT,
        "public compatibility fallback remains stable"
    );
    assert!(!loaded.has_authoritative_created_at);

    let mut state = test_state().await?;
    let mut accounts = AccountStore::new();
    accounts.insert(seed_account(ACCOUNT_ID));
    state.accounts = Arc::new(RwLock::new(accounts));
    state.nodes = Arc::new(RwLock::new(loaded_nodes));

    let app = Router::new().merge(api_router()).with_state(state);
    let value = read_account_details(&app, ACCOUNT_ID).await?;
    assert_eq!(value["activity"].as_array().map(Vec::len), Some(0));

    Ok(())
}

#[tokio::test]
async fn account_details_deduplicate_parallel_faeden_without_inventing_activity() -> Result<()> {
    const ACCOUNT_ID: &str = "account-parallel";
    const NODE_ID: &str = "node-parallel";
    let (created_at, expires_at) = active_faden_timestamps();

    let mut state = test_state().await?;
    let mut accounts = AccountStore::new();
    accounts.insert(seed_account(ACCOUNT_ID));
    state.accounts = Arc::new(RwLock::new(accounts));

    let mut node = test_node(NODE_ID, "Mehrfach beteiligter Knoten", None);
    node.created_by_account_id = Some(ACCOUNT_ID.to_string());
    let mut nodes = OrderedCache::new();
    nodes.insert(NODE_ID.to_string(), node);
    state.nodes = Arc::new(RwLock::new(nodes));

    let mut edges = OrderedCache::new();
    for edge_id in ["edge-parallel-a", "edge-parallel-b"] {
        edges.insert(
            edge_id.to_string(),
            Edge {
                id: edge_id.to_string(),
                source_id: ACCOUNT_ID.to_string(),
                source_type: Some("account".to_string()),
                target_id: NODE_ID.to_string(),
                target_type: Some("node".to_string()),
                edge_kind: "reference".to_string(),
                faden_type: None,
                faden_subject_id: None,
                note: None,
                created_at: Some(created_at.clone().into()),
                expires_at: Some(Some(expires_at.clone().into())),
            },
        );
    }
    state.edges = Arc::new(RwLock::new(edges));

    let app = Router::new().merge(api_router()).with_state(state);
    let value = read_account_details(&app, ACCOUNT_ID).await?;
    let projected_at = assert_account_has_single_node_relation(
        &value,
        ACCOUNT_ID,
        NODE_ID,
        "Mehrfach beteiligter Knoten",
        "reference",
        "Hat den Knoten \"Mehrfach beteiligter Knoten\" geknüpft.",
    );
    assert_eq!(projected_at, TEST_NODE_TIMESTAMP);

    Ok(())
}

#[tokio::test]
async fn account_details_do_not_turn_incoming_relation_into_account_activity() -> Result<()> {
    const ACCOUNT_ID: &str = "account-incoming";
    const NODE_ID: &str = "node-incoming";
    let (created_at, expires_at) = active_faden_timestamps();

    let mut state = test_state().await?;
    let mut accounts = AccountStore::new();
    accounts.insert(seed_account(ACCOUNT_ID));
    state.accounts = Arc::new(RwLock::new(accounts));

    let mut nodes = OrderedCache::new();
    nodes.insert(
        NODE_ID.to_string(),
        test_node(NODE_ID, "Importknoten", None),
    );
    state.nodes = Arc::new(RwLock::new(nodes));

    let mut edges = OrderedCache::new();
    edges.insert(
        "edge-incoming".to_string(),
        Edge {
            id: "edge-incoming".to_string(),
            source_id: NODE_ID.to_string(),
            source_type: Some("node".to_string()),
            target_id: ACCOUNT_ID.to_string(),
            target_type: Some("account".to_string()),
            edge_kind: "reference".to_string(),
            faden_type: None,
            faden_subject_id: None,
            note: Some("interne Importnotiz".to_string()),
            created_at: Some(created_at.clone().into()),
            expires_at: Some(Some(expires_at.into())),
        },
    );
    state.edges = Arc::new(RwLock::new(edges));

    let app = Router::new().merge(api_router()).with_state(state);
    let value = read_account_details(&app, ACCOUNT_ID).await?;
    assert_eq!(value["nodes"].as_array().map(Vec::len), Some(1));
    assert_eq!(value["nodes"][0]["node_id"], NODE_ID);
    assert_eq!(value["nodes"][0]["node_title"], "Importknoten");
    assert_eq!(value["nodes"][0]["edge_kind"], "reference");
    assert_eq!(value["activity"].as_array().map(Vec::len), Some(0));

    Ok(())
}

#[tokio::test]
async fn account_details_ignore_node_typed_account_id_collision() -> Result<()> {
    const ACCOUNT_ID: &str = "account-mistyped";
    const NODE_ID: &str = "node-real";

    let mut state = test_state().await?;
    let mut accounts = AccountStore::new();
    accounts.insert(seed_account(ACCOUNT_ID));
    state.accounts = Arc::new(RwLock::new(accounts));

    let mut nodes = OrderedCache::new();
    nodes.insert(
        NODE_ID.to_string(),
        test_node(NODE_ID, "Echter Knoten", None),
    );
    state.nodes = Arc::new(RwLock::new(nodes));

    let mut edges = OrderedCache::new();
    edges.insert(
        "edge-type-confused".to_string(),
        Edge {
            id: "edge-type-confused".to_string(),
            source_id: ACCOUNT_ID.to_string(),
            source_type: Some("node".to_string()),
            target_id: NODE_ID.to_string(),
            target_type: Some("node".to_string()),
            edge_kind: "reference".to_string(),
            faden_type: None,
            faden_subject_id: None,
            note: None,
            created_at: Some("2026-07-13T05:55:00+00:00".to_string().into()),
            expires_at: None,
        },
    );
    state.edges = Arc::new(RwLock::new(edges));

    let app = Router::new().merge(api_router()).with_state(state);
    let value = read_account_details(&app, ACCOUNT_ID).await?;
    assert_eq!(value["nodes"].as_array().map(Vec::len), Some(0));
    assert_eq!(value["activity"].as_array().map(Vec::len), Some(0));

    Ok(())
}

#[tokio::test]
async fn guest_account_is_a_public_garnrolle_without_public_position() -> Result<()> {
    let mut state = test_state().await?;
    let mut accounts = AccountStore::new();
    let mut guest = seed_account("guest-hidden");
    guest.role = Role::Gast;
    accounts.insert(guest);
    accounts.insert(seed_account("weber-visible"));
    state.accounts = Arc::new(RwLock::new(accounts));

    let app = Router::new().merge(api_router()).with_state(state);

    let list = app
        .clone()
        .oneshot(Request::get("/accounts").body(body::Body::empty())?)
        .await?;
    assert_eq!(list.status(), StatusCode::OK);
    let bytes = body::to_bytes(list.into_body(), usize::MAX).await?;
    let accounts: serde_json::Value = serde_json::from_slice(&bytes)?;
    assert_eq!(accounts.as_array().map(Vec::len), Some(2));
    assert_eq!(accounts[0]["id"], "guest-hidden");
    assert_eq!(accounts[0]["map_state"], "not_on_map");
    assert!(accounts[0].get("public_pos").is_none());
    assert_eq!(accounts[1]["id"], "weber-visible");

    let detail = app
        .oneshot(Request::get("/accounts/guest-hidden").body(body::Body::empty())?)
        .await?;
    assert_eq!(detail.status(), StatusCode::OK);

    Ok(())
}
