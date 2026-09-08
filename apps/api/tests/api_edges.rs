use anyhow::{Context, Result};
use axum::{
    body,
    http::{Request, StatusCode},
    middleware::{from_fn, from_fn_with_state},
    routing::post,
    Router,
};
use serial_test::serial;
mod helpers;

use helpers::{insert_test_node, read_account_details, set_gewebe_in_dir, test_node};
use std::{fs, path::PathBuf, sync::Arc};
use tokio::sync::RwLock;
use tower::ServiceExt;
use weltgewebe_api::{
    auth::{
        accounts::AccountStore, rate_limit::AuthRateLimiter, role::Role, session::SessionBackend,
    },
    config::{AppConfig, DomainReadSource},
    middleware::{auth::auth_middleware, authz::require_write, csrf::require_csrf},
    routes::{
        accounts::{AccountInternal, AccountPublic, GarnrolleMapState},
        api_router,
        edges::create_edge,
    },
    state::ApiState,
    telemetry::{BuildInfo, Metrics},
    test_helpers::EnvGuard,
};

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

    // Load edges from file (environment variable must be set before calling this)
    let edges = weltgewebe_api::routes::edges::load_edges().await;

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
        edges: Arc::new(tokio::sync::RwLock::new(edges)),
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

fn make_tmp_dir() -> tempfile::TempDir {
    tempfile::tempdir().expect("tmpdir")
}
fn write_lines(path: &PathBuf, lines: &[&str]) {
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, lines.join("\n")).unwrap();
}

#[tokio::test]
#[serial]
async fn edges_filter_src_dst() -> anyhow::Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    let edges_path = in_dir.join("demo.edges.jsonl");
    let _env = set_gewebe_in_dir(&in_dir);

    write_lines(
        &edges_path,
        &[
            r#"{"id":"e1","source_id":"n1","target_id":"n2","edge_kind":"reference"}"#,
            r#"{"id":"e2","source_id":"n1","target_id":"n3","edge_kind":"reference"}"#,
            r#"{"id":"e3","source_id":"n2","target_id":"n3","edge_kind":"reference"}"#,
        ],
    );

    let state = test_state().await?;
    let app = Router::new().merge(api_router()).with_state(state);

    let res = app
        .clone()
        .oneshot(Request::get("/edges?source_id=n1").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::OK);
    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let v: serde_json::Value = serde_json::from_slice(&body)?;
    assert_eq!(v.as_array().context("must be array")?.len(), 2);

    let res = app
        .oneshot(Request::get("/edges?source_id=n1&target_id=n2").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::OK);
    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let v: serde_json::Value = serde_json::from_slice(&body)?;
    let arr = v.as_array().context("must be array")?;
    assert_eq!(arr.len(), 1);
    assert_eq!(
        arr[0]
            .get("id")
            .context("id missing")?
            .as_str()
            .context("must be string")?,
        "e1"
    );

    Ok(())
}

#[tokio::test]
#[serial]
async fn edges_offset_pagination() -> anyhow::Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    let edges_path = in_dir.join("demo.edges.jsonl");
    let _env = set_gewebe_in_dir(&in_dir);

    write_lines(
        &edges_path,
        &[
            r#"{"id":"e1","source_id":"n1","target_id":"n2","edge_kind":"reference"}"#,
            r#"{"id":"e2","source_id":"n1","target_id":"n3","edge_kind":"reference"}"#,
            r#"{"id":"e3","source_id":"n2","target_id":"n3","edge_kind":"reference"}"#,
        ],
    );

    let state = test_state().await?;
    let app = Router::new().merge(api_router()).with_state(state);

    // limit=1&offset=0 liefert erstes Element (e1)
    let res = app
        .clone()
        .oneshot(Request::get("/edges?limit=1&offset=0").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::OK);
    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let v: serde_json::Value = serde_json::from_slice(&body)?;
    let arr = v.as_array().context("must be array")?;
    assert_eq!(arr.len(), 1);
    assert_eq!(arr[0]["id"], "e1");

    // limit=1&offset=1 liefert zweites Element (e2)
    let res = app
        .clone()
        .oneshot(Request::get("/edges?limit=1&offset=1").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::OK);
    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let v: serde_json::Value = serde_json::from_slice(&body)?;
    let arr = v.as_array().context("must be array")?;
    assert_eq!(arr.len(), 1);
    assert_eq!(arr[0]["id"], "e2");

    // Offset außerhalb der Ergebnislänge liefert leere Liste
    let res = app
        .clone()
        .oneshot(Request::get("/edges?limit=10&offset=100").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::OK);
    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let v: serde_json::Value = serde_json::from_slice(&body)?;
    let arr = v.as_array().context("must be array")?;
    assert_eq!(arr.len(), 0);

    // Ungültiger Offset (negativ) -> 400
    let res = app
        .clone()
        .oneshot(Request::get("/edges?offset=-1").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);

    // Ungültiger Offset (kein Integer) -> 400
    let res = app
        .oneshot(Request::get("/edges?offset=abc").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);

    Ok(())
}

#[tokio::test]
#[serial]
async fn edges_invalid_limit() -> anyhow::Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    let edges_path = in_dir.join("demo.edges.jsonl");
    let _env = set_gewebe_in_dir(&in_dir);

    write_lines(
        &edges_path,
        &[r#"{"id":"e1","source_id":"n1","target_id":"n2","edge_kind":"reference"}"#],
    );

    let state = test_state().await?;
    let app = Router::new().merge(api_router()).with_state(state);

    // limit=abc -> 400
    let res = app
        .clone()
        .oneshot(Request::get("/edges?limit=abc").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);

    // limit=-1 -> 400
    let res = app
        .oneshot(Request::get("/edges?limit=-1").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);

    Ok(())
}

#[tokio::test]
#[serial]
async fn expired_and_pre_lifecycle_edges_are_hidden_before_pagination_and_single_get(
) -> anyhow::Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    let edges_path = in_dir.join("demo.edges.jsonl");
    let _env = set_gewebe_in_dir(&in_dir);

    write_lines(
        &edges_path,
        &[
            r#"{"id":"e-expired","source_id":"n1","target_id":"n2","edge_kind":"reference","created_at":"2020-01-01T00:00:00Z","expires_at":"2020-01-08T00:00:00Z"}"#,
            r#"{"id":"e-pre-lifecycle","source_id":"n1","target_id":"n4","edge_kind":"reference","created_at":"2020-01-01T00:00:00Z"}"#,
            r#"{"id":"e-active","source_id":"n1","target_id":"n3","edge_kind":"reference"}"#,
        ],
    );

    let state = test_state().await?;
    let app = Router::new().merge(api_router()).with_state(state);

    let res = app
        .clone()
        .oneshot(Request::get("/edges?limit=1&offset=0").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::OK);
    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let items: serde_json::Value = serde_json::from_slice(&body)?;
    let items = items
        .as_array()
        .context("offset response must be an array")?;
    assert_eq!(items.len(), 1);
    assert_eq!(items[0]["id"], "e-active");

    let res = app
        .clone()
        .oneshot(Request::get("/edges?pagination=cursor&limit=10").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::OK);
    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let page: serde_json::Value = serde_json::from_slice(&body)?;
    let items = page["items"]
        .as_array()
        .context("cursor response items must be an array")?;
    assert_eq!(items.len(), 1);
    assert_eq!(items[0]["id"], "e-active");

    let res = app
        .clone()
        .oneshot(Request::get("/edges/e-expired").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::NOT_FOUND);

    let res = app
        .oneshot(Request::get("/edges/e-pre-lifecycle").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::NOT_FOUND);

    Ok(())
}

#[tokio::test]
#[serial]
async fn edges_cursor_pagination_envelope_and_walk() -> anyhow::Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    let edges_path = in_dir.join("demo.edges.jsonl");
    let _env = set_gewebe_in_dir(&in_dir);

    // Insertion order is deliberately unsorted to prove cursor mode sorts by id.
    write_lines(
        &edges_path,
        &[
            r#"{"id":"e3","source_id":"n1","target_id":"n2","edge_kind":"reference"}"#,
            r#"{"id":"e1","source_id":"n1","target_id":"n2","edge_kind":"reference"}"#,
            r#"{"id":"e2","source_id":"n1","target_id":"n2","edge_kind":"reference"}"#,
        ],
    );

    let state = test_state().await?;
    let app = Router::new().merge(api_router()).with_state(state);

    // Page 1: envelope shape with id-ascending order.
    let res = app
        .clone()
        .oneshot(Request::get("/edges?pagination=cursor&limit=2").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::OK);
    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let v: serde_json::Value = serde_json::from_slice(&body)?;
    let items = v["items"].as_array().context("items must be array")?;
    assert_eq!(items.len(), 2);
    assert_eq!(items[0]["id"], "e1");
    assert_eq!(items[1]["id"], "e2");
    assert_eq!(v["page"]["has_more"], true);
    let cursor1 = v["page"]["next_cursor"]
        .as_str()
        .context("next_cursor must be present on page 1")?
        .to_string();

    // Page 2 (last): remaining id, no duplicates, has_more false, next_cursor null.
    let uri = format!("/edges?cursor={cursor1}&limit=2");
    let res = app
        .oneshot(Request::get(uri.as_str()).body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::OK);
    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let v: serde_json::Value = serde_json::from_slice(&body)?;
    let items = v["items"].as_array().context("items must be array")?;
    assert_eq!(items.len(), 1);
    assert_eq!(items[0]["id"], "e3");
    assert_eq!(v["page"]["has_more"], false);
    assert!(v["page"]["next_cursor"].is_null());

    Ok(())
}

#[tokio::test]
#[serial]
async fn edges_cursor_respects_filter_and_invalid_cursor() -> anyhow::Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    let edges_path = in_dir.join("demo.edges.jsonl");
    let _env = set_gewebe_in_dir(&in_dir);

    write_lines(
        &edges_path,
        &[
            r#"{"id":"e1","source_id":"n1","target_id":"n2","edge_kind":"reference"}"#,
            r#"{"id":"e2","source_id":"n9","target_id":"n3","edge_kind":"reference"}"#,
            r#"{"id":"e3","source_id":"n1","target_id":"n3","edge_kind":"reference"}"#,
        ],
    );

    let state = test_state().await?;
    let app = Router::new().merge(api_router()).with_state(state);

    // Cursor mode keeps the source_id filter: only e1 and e3 match n1.
    let res = app
        .clone()
        .oneshot(
            Request::get("/edges?pagination=cursor&source_id=n1&limit=10")
                .body(body::Body::empty())?,
        )
        .await?;
    assert_eq!(res.status(), StatusCode::OK);
    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let v: serde_json::Value = serde_json::from_slice(&body)?;
    let items = v["items"].as_array().context("items must be array")?;
    assert_eq!(items.len(), 2);
    assert_eq!(items[0]["id"], "e1");
    assert_eq!(items[1]["id"], "e3");
    assert_eq!(v["page"]["has_more"], false);
    assert!(v["page"]["next_cursor"].is_null());

    // Malformed cursor -> deterministic 400.
    let res = app
        .oneshot(Request::get("/edges?cursor=abc").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);

    Ok(())
}

#[tokio::test]
#[serial]
async fn edges_cursor_limit_zero_is_bad_request() -> anyhow::Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    let edges_path = in_dir.join("demo.edges.jsonl");
    let _env = set_gewebe_in_dir(&in_dir);

    write_lines(
        &edges_path,
        &[
            r#"{"id":"e1","source_id":"n1","target_id":"n2","edge_kind":"reference"}"#,
            r#"{"id":"e2","source_id":"n9","target_id":"n3","edge_kind":"reference"}"#,
        ],
    );

    let state = test_state().await?;
    let app = Router::new().merge(api_router()).with_state(state);

    // In cursor mode, limit=0 must return 400 Bad Request.
    let res = app
        .oneshot(Request::get("/edges?pagination=cursor&limit=0").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);

    Ok(())
}

// ---------------------------------------------------------------------------
// Internal derived-Faden projection — no production HTTP write surface
// ---------------------------------------------------------------------------

// Valid UUIDs for contract-conforming create payloads (`source_id`,
// `target_id`, and optional `id` must be UUID-formatted per PR-1).
const CREATE_SOURCE_ID: &str = "00000000-0000-0000-0000-00000000000a";
const CREATE_TARGET_ID: &str = "00000000-0000-0000-0000-00000000000b";
const CREATE_EDGE_ID: &str = "00000000-0000-0000-0000-00000000000c";

fn writer_account(id: &str, role: Role) -> AccountInternal {
    AccountInternal {
        public: AccountPublic {
            id: id.to_string(),
            kind: "garnrolle".to_string(),
            title: format!("Writer {id}"),
            summary: None,
            public_pos: None,
            map_state: GarnrolleMapState::NotOnMap,
            radius_m: 0,
            disabled: false,
            tags: vec![],
        },
        role,
        email: None,
        webauthn_user_id: uuid::Uuid::new_v4(),
    }
}

/// Build a router (auth + csrf wired like prod) with a single account of the
/// given role and an active session for it. Returns (app, session_cookie,
/// state). `GEWEBE_IN_DIR` must already be set by the caller via EnvGuard.
async fn app_with_session(
    role: Role,
    domain_read_source: DomainReadSource,
) -> Result<(Router, String, ApiState)> {
    app_with_session_and_edge_write(
        role,
        domain_read_source,
        weltgewebe_api::config::DomainEdgeWriteSource::Jsonl,
    )
    .await
}

/// Like [`app_with_session`], but with an explicit internal Faden projection source so
/// tests can construct write-source combinations the config loader forbids
/// (defensive route-guard coverage).
async fn app_with_session_and_edge_write(
    role: Role,
    domain_read_source: DomainReadSource,
    domain_edge_write_source: weltgewebe_api::config::DomainEdgeWriteSource,
) -> Result<(Router, String, ApiState)> {
    app_with_session_for_account(
        "writer1",
        role,
        domain_read_source,
        domain_edge_write_source,
    )
    .await
}

async fn app_with_session_for_account(
    account_id: &str,
    role: Role,
    domain_read_source: DomainReadSource,
    domain_edge_write_source: weltgewebe_api::config::DomainEdgeWriteSource,
) -> Result<(Router, String, ApiState)> {
    let mut accounts = AccountStore::new();
    accounts.insert(writer_account(account_id, role));

    let mut state = test_state().await?;
    state.config.domain_read_source = domain_read_source;
    state.config.domain_edge_write_source = domain_edge_write_source;
    state.accounts = Arc::new(RwLock::new(accounts));

    let session = state
        .sessions
        .create(account_id.to_string(), None)
        .await
        .expect("session create");
    let cookie = format!("gewebe_session={}", session.id);

    let app = Router::new()
        .merge(api_router())
        .route(
            "/__test/derived-edges",
            post(create_edge).route_layer(from_fn(require_write)),
        )
        .layer(from_fn_with_state(state.clone(), auth_middleware))
        .layer(axum::middleware::from_fn(require_csrf))
        .with_state(state.clone());
    Ok((app, cookie, state))
}

fn post_edges(cookie: Option<&str>, json_body: &str) -> Request<body::Body> {
    let mut builder = Request::post("/__test/derived-edges")
        .header("Content-Type", "application/json")
        .header("Host", "localhost")
        .header("Origin", "http://localhost");
    if let Some(c) = cookie {
        builder = builder.header("Cookie", c);
    }
    builder
        .body(body::Body::from(json_body.to_string()))
        .unwrap()
}

fn valid_create_body() -> String {
    format!(
        r#"{{"source_id":"{CREATE_SOURCE_ID}","source_type":"node","target_id":"{CREATE_TARGET_ID}","target_type":"node","edge_kind":"reference"}}"#
    )
}

fn jsonl_lines(path: &std::path::Path) -> Vec<serde_json::Value> {
    match fs::read_to_string(path) {
        Ok(contents) => contents
            .lines()
            .filter(|l| !l.trim().is_empty())
            .map(|l| serde_json::from_str(l).expect("JSONL line must parse"))
            .collect(),
        Err(_) => Vec::new(),
    }
}

async fn read_json_body(res: axum::response::Response) -> Result<serde_json::Value> {
    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    Ok(serde_json::from_slice(&bytes)?)
}

async fn read_text_body(res: axum::response::Response) -> Result<String> {
    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    Ok(String::from_utf8_lossy(&bytes).into_owned())
}

#[tokio::test]
#[serial]
async fn post_edges_binds_account_source_to_authenticated_weber() -> Result<()> {
    const OWN_ACCOUNT_ID: &str = "00000000-0000-0000-0000-00000000000d";
    const FOREIGN_ACCOUNT_ID: &str = "00000000-0000-0000-0000-00000000000e";

    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    fs::create_dir_all(&in_dir)?;
    let edges_path = in_dir.join("demo.edges.jsonl");
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, state) = app_with_session_for_account(
        OWN_ACCOUNT_ID,
        Role::Weber,
        DomainReadSource::Jsonl,
        weltgewebe_api::config::DomainEdgeWriteSource::Jsonl,
    )
    .await?;
    state
        .accounts
        .write()
        .await
        .insert(writer_account(FOREIGN_ACCOUNT_ID, Role::Weber));

    let disguised_foreign_body = format!(
        r#"{{"source_id":"{FOREIGN_ACCOUNT_ID}","source_type":"node","target_id":"{CREATE_TARGET_ID}","target_type":"node","edge_kind":"reference"}}"#
    );
    let res = app
        .clone()
        .oneshot(post_edges(Some(&cookie), &disguised_foreign_body))
        .await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);
    assert!(
        !edges_path.exists(),
        "known account ids disguised as nodes must be rejected before persistence"
    );
    assert!(state.edges.read().await.is_empty());

    let foreign_body = format!(
        r#"{{"source_id":"{FOREIGN_ACCOUNT_ID}","source_type":"account","target_id":"{CREATE_TARGET_ID}","target_type":"node","edge_kind":"reference"}}"#
    );
    let res = app
        .clone()
        .oneshot(post_edges(Some(&cookie), &foreign_body))
        .await?;
    assert_eq!(res.status(), StatusCode::FORBIDDEN);
    assert!(
        !edges_path.exists(),
        "foreign account source must be rejected before persistence"
    );
    assert!(state.edges.read().await.is_empty());

    let incoming_foreign_body = format!(
        r#"{{"source_id":"{CREATE_TARGET_ID}","source_type":"node","target_id":"{FOREIGN_ACCOUNT_ID}","target_type":"account","edge_kind":"reference"}}"#
    );
    let res = app
        .clone()
        .oneshot(post_edges(Some(&cookie), &incoming_foreign_body))
        .await?;
    assert_eq!(res.status(), StatusCode::FORBIDDEN);
    assert!(
        !edges_path.exists(),
        "node-to-account edge must be rejected before persistence for a Weber"
    );
    assert!(state.edges.read().await.is_empty());

    let own_body = format!(
        r#"{{"source_id":"{OWN_ACCOUNT_ID}","source_type":"account","target_id":"{CREATE_TARGET_ID}","target_type":"node","edge_kind":"reference"}}"#
    );
    let res = app.oneshot(post_edges(Some(&cookie), &own_body)).await?;
    assert_eq!(res.status(), StatusCode::CREATED);
    let persisted = jsonl_lines(&edges_path);
    assert_eq!(persisted.len(), 1);
    assert_eq!(persisted[0]["source_id"], OWN_ACCOUNT_ID);

    Ok(())
}

#[tokio::test]
#[serial]
async fn post_account_edge_projects_garnrolle_relation_and_survives_reload() -> Result<()> {
    const ACCOUNT_ID: &str = "00000000-0000-0000-0000-000000000021";
    const NODE_ID: &str = "00000000-0000-0000-0000-000000000022";
    const NODE_TITLE: &str = "Vorwärtsnachweis";

    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    fs::create_dir_all(&in_dir)?;
    let edges_path = in_dir.join("demo.edges.jsonl");
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, state) = app_with_session_for_account(
        ACCOUNT_ID,
        Role::Weber,
        DomainReadSource::Jsonl,
        weltgewebe_api::config::DomainEdgeWriteSource::Jsonl,
    )
    .await?;
    insert_test_node(
        &state,
        test_node(
            NODE_ID,
            NODE_TITLE,
            Some("Neu über den regulären Schreibpfad verknüpft"),
        ),
    )
    .await;

    let request_body = format!(
        r#"{{"source_id":"{ACCOUNT_ID}","source_type":"account","target_id":"{NODE_ID}","target_type":"node","edge_kind":"reference"}}"#
    );
    let response = app
        .clone()
        .oneshot(post_edges(Some(&cookie), &request_body))
        .await?;
    assert_eq!(response.status(), StatusCode::CREATED);
    let created = read_json_body(response).await?;
    let created_id = created["id"]
        .as_str()
        .context("created edge must carry id")?;

    let persisted = jsonl_lines(&edges_path);
    assert_eq!(persisted.len(), 1);
    assert_eq!(persisted[0]["id"], created_id);
    assert_eq!(persisted[0]["source_id"], ACCOUNT_ID);
    assert_eq!(persisted[0]["target_id"], NODE_ID);

    let immediate_details = read_account_details(&app, ACCOUNT_ID).await?;
    assert_eq!(immediate_details["nodes"].as_array().map(Vec::len), Some(1));
    assert_eq!(immediate_details["nodes"][0]["node_id"], NODE_ID);
    assert_eq!(immediate_details["nodes"][0]["node_title"], NODE_TITLE);
    assert_eq!(immediate_details["nodes"][0]["edge_kind"], "reference");
    assert_eq!(
        immediate_details["activity"].as_array().map(Vec::len),
        Some(0),
        "a manually written relation is current topology, not durable activity"
    );

    let (restarted_app, _restarted_cookie, restarted_state) = app_with_session_for_account(
        ACCOUNT_ID,
        Role::Weber,
        DomainReadSource::Jsonl,
        weltgewebe_api::config::DomainEdgeWriteSource::Jsonl,
    )
    .await?;
    insert_test_node(
        &restarted_state,
        test_node(
            NODE_ID,
            NODE_TITLE,
            Some("Neu über den regulären Schreibpfad verknüpft"),
        ),
    )
    .await;

    let restarted_details = read_account_details(&restarted_app, ACCOUNT_ID).await?;
    assert_eq!(restarted_details["nodes"].as_array().map(Vec::len), Some(1));
    assert_eq!(restarted_details["nodes"][0]["node_id"], NODE_ID);
    assert_eq!(restarted_details["nodes"][0]["node_title"], NODE_TITLE);
    assert_eq!(restarted_details["nodes"][0]["edge_kind"], "reference");
    assert_eq!(
        restarted_details["activity"].as_array().map(Vec::len),
        Some(0),
        "reload must not turn a raw relation into a creation event"
    );

    Ok(())
}

#[tokio::test]
#[serial]
async fn post_edges_admin_retains_foreign_account_operator_path() -> Result<()> {
    const ADMIN_ID: &str = "00000000-0000-0000-0000-00000000000f";
    const FOREIGN_ACCOUNT_ID: &str = "00000000-0000-0000-0000-000000000010";

    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, _) = app_with_session_for_account(
        ADMIN_ID,
        Role::Admin,
        DomainReadSource::Jsonl,
        weltgewebe_api::config::DomainEdgeWriteSource::Jsonl,
    )
    .await?;
    let body = format!(
        r#"{{"source_id":"{FOREIGN_ACCOUNT_ID}","source_type":"account","target_id":"{CREATE_TARGET_ID}","target_type":"node","edge_kind":"reference"}}"#
    );
    let res = app
        .clone()
        .oneshot(post_edges(Some(&cookie), &body))
        .await?;
    assert_eq!(res.status(), StatusCode::CREATED);

    let incoming_body = format!(
        r#"{{"source_id":"{CREATE_TARGET_ID}","source_type":"node","target_id":"{FOREIGN_ACCOUNT_ID}","target_type":"account","edge_kind":"reference"}}"#
    );
    let res = app
        .oneshot(post_edges(Some(&cookie), &incoming_body))
        .await?;
    assert_eq!(res.status(), StatusCode::CREATED);

    Ok(())
}

#[tokio::test]
#[serial]
async fn post_edges_creates_edge_in_jsonl_mode() -> Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    fs::create_dir_all(&in_dir)?;
    let edges_path = in_dir.join("demo.edges.jsonl");
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, state) = app_with_session(Role::Weber, DomainReadSource::Jsonl).await?;

    let res = app
        .clone()
        .oneshot(post_edges(Some(&cookie), &valid_create_body()))
        .await?;
    assert_eq!(res.status(), StatusCode::CREATED);
    let v = read_json_body(res).await?;

    // Server generated a UUID id and an RFC3339 created_at.
    let id = v["id"].as_str().context("id must be string")?.to_string();
    uuid::Uuid::parse_str(&id).context("id must be a UUID")?;
    let created_at = v["created_at"]
        .as_str()
        .context("created_at must be string")?
        .to_string();
    let created_at_parsed =
        chrono::DateTime::parse_from_rfc3339(&created_at).context("created_at must be RFC3339")?;
    let expires_at = v["expires_at"]
        .as_str()
        .context("expires_at must be string")?
        .to_string();
    let expires_at_parsed =
        chrono::DateTime::parse_from_rfc3339(&expires_at).context("expires_at must be RFC3339")?;
    assert_eq!(
        expires_at_parsed.signed_duration_since(created_at_parsed),
        chrono::Duration::hours(168)
    );
    assert_eq!(v["source_type"], "node");
    assert_eq!(v["target_type"], "node");
    assert_eq!(v["source_id"], CREATE_SOURCE_ID);
    assert_eq!(v["target_id"], CREATE_TARGET_ID);
    assert_eq!(v["edge_kind"], "reference");

    // Exactly one durable JSONL line (the in_dir started empty), carrying the
    // same server-generated values as the response.
    let contents = fs::read_to_string(&edges_path)?;
    assert!(contents.ends_with('\n'), "JSONL line must end with newline");
    let lines = jsonl_lines(&edges_path);
    assert_eq!(lines.len(), 1);
    assert_eq!(lines[0]["id"], id.as_str());
    assert_eq!(lines[0]["created_at"], created_at.as_str());
    assert!(
        lines[0].get("note").is_none(),
        "absent note must be omitted"
    );
    assert_eq!(lines[0]["expires_at"], expires_at.as_str());

    // Cache contains the edge.
    {
        let cache = state.edges.read().await;
        let cached = cache.get(&id).context("edge must be in cache")?;
        assert_eq!(cached.created_at.as_deref(), Some(created_at.as_str()));
        assert_eq!(
            cached
                .expires_at
                .as_ref()
                .and_then(|inner| inner.as_deref()),
            Some(expires_at.as_str())
        );
        assert_eq!(cached.source_type.as_deref(), Some("node"));
        assert_eq!(cached.target_type.as_deref(), Some("node"));
    }

    // GET /edges/{id} serves the created edge.
    let uri = format!("/edges/{id}");
    let res = app
        .oneshot(Request::get(uri.as_str()).body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::OK);
    let fetched = read_json_body(res).await?;
    assert_eq!(fetched["id"], id.as_str());
    assert_eq!(fetched["created_at"], created_at.as_str());

    Ok(())
}

#[tokio::test]
#[serial]
async fn post_edges_replays_same_operation_without_second_write() -> Result<()> {
    const OPERATION_ID: &str = "20000000-0000-0000-0000-000000000001";
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    fs::create_dir_all(&in_dir)?;
    let edges_path = in_dir.join("demo.edges.jsonl");
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, _initial_state) =
        app_with_session(Role::Weber, DomainReadSource::Jsonl).await?;
    let request_body = format!(
        r#"{{"source_id":"{CREATE_SOURCE_ID}","source_type":"node","target_id":"{CREATE_TARGET_ID}","target_type":"node","edge_kind":"reference","operation_id":"{OPERATION_ID}"}}"#
    );

    let first_attempt = app
        .clone()
        .oneshot(post_edges(Some(&cookie), &request_body));
    let second_attempt = app
        .clone()
        .oneshot(post_edges(Some(&cookie), &request_body));
    let (first, second) = tokio::join!(first_attempt, second_attempt);
    let first = first?;
    let second = second?;
    let first_status = first.status();
    let second_status = second.status();
    assert!(
        (first_status == StatusCode::CREATED && second_status == StatusCode::OK)
            || (first_status == StatusCode::OK && second_status == StatusCode::CREATED),
        "parallel retries must produce one 201 and one 200, got {first_status} and {second_status}"
    );
    let first_edge = read_json_body(first).await?;
    let second_edge = read_json_body(second).await?;
    assert_eq!(first_edge["id"], second_edge["id"]);

    let (restarted_app, restarted_cookie, restarted_state) =
        app_with_session(Role::Weber, DomainReadSource::Jsonl).await?;
    let replay = restarted_app
        .clone()
        .oneshot(post_edges(Some(&restarted_cookie), &request_body))
        .await?;
    assert_eq!(replay.status(), StatusCode::OK);
    let replay_edge = read_json_body(replay).await?;
    assert_eq!(replay_edge["id"], first_edge["id"]);
    assert!(replay_edge.get("operation_id").is_none());
    assert!(replay_edge.get("_create_operation_id").is_none());

    let records = jsonl_lines(&edges_path);
    assert_eq!(records.len(), 1);
    assert_eq!(records[0]["_create_operation_id"], OPERATION_ID);
    assert_eq!(restarted_state.edges.read().await.len(), 1);

    let changed_body = format!(
        r#"{{"source_id":"{CREATE_SOURCE_ID}","source_type":"node","target_id":"{CREATE_TARGET_ID}","target_type":"node","edge_kind":"reference","note":"different","operation_id":"{OPERATION_ID}"}}"#
    );
    let conflict = restarted_app
        .oneshot(post_edges(Some(&restarted_cookie), &changed_body))
        .await?;
    assert_eq!(conflict.status(), StatusCode::CONFLICT);
    assert_eq!(jsonl_lines(&edges_path).len(), 1);

    let (other_app, other_cookie, _other_state) = app_with_session_for_account(
        "writer2",
        Role::Weber,
        DomainReadSource::Jsonl,
        weltgewebe_api::config::DomainEdgeWriteSource::Jsonl,
    )
    .await?;
    let other = other_app
        .oneshot(post_edges(Some(&other_cookie), &changed_body))
        .await?;
    assert_eq!(other.status(), StatusCode::CREATED);
    assert_eq!(jsonl_lines(&edges_path).len(), 2);

    Ok(())
}

#[tokio::test]
#[serial]
async fn post_edges_accepts_client_uuid_id() -> Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    fs::create_dir_all(&in_dir)?;
    let edges_path = in_dir.join("demo.edges.jsonl");
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, _state) = app_with_session(Role::Weber, DomainReadSource::Jsonl).await?;

    let body_json = format!(
        r#"{{"id":"{CREATE_EDGE_ID}","source_id":"{CREATE_SOURCE_ID}","source_type":"node","target_id":"{CREATE_TARGET_ID}","target_type":"node","edge_kind":"reference"}}"#
    );
    let res = app.oneshot(post_edges(Some(&cookie), &body_json)).await?;
    assert_eq!(res.status(), StatusCode::CREATED);
    let v = read_json_body(res).await?;
    assert_eq!(v["id"], CREATE_EDGE_ID);

    let lines = jsonl_lines(&edges_path);
    assert_eq!(lines.len(), 1);
    assert_eq!(lines[0]["id"], CREATE_EDGE_ID);

    Ok(())
}

#[tokio::test]
#[serial]
async fn post_edges_persists_note_when_present() -> Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    fs::create_dir_all(&in_dir)?;
    let edges_path = in_dir.join("demo.edges.jsonl");
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, state) = app_with_session(Role::Weber, DomainReadSource::Jsonl).await?;

    let body_json = format!(
        r#"{{"source_id":"{CREATE_SOURCE_ID}","source_type":"node","target_id":"{CREATE_TARGET_ID}","target_type":"node","edge_kind":"reference","note":"hello edge"}}"#
    );
    let res = app
        .clone()
        .oneshot(post_edges(Some(&cookie), &body_json))
        .await?;
    assert_eq!(res.status(), StatusCode::CREATED);
    let v = read_json_body(res).await?;
    assert_eq!(v["note"], "hello edge");
    let id = v["id"].as_str().context("id must be string")?.to_string();

    let lines = jsonl_lines(&edges_path);
    assert_eq!(lines.len(), 1);
    assert_eq!(lines[0]["note"], "hello edge");

    {
        let cache = state.edges.read().await;
        let cached = cache.get(&id).context("edge must be in cache")?;
        assert_eq!(cached.note.as_deref(), Some("hello edge"));
    }

    let res = app
        .clone()
        .oneshot(Request::get(format!("/edges/{id}")).body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::OK);
    let public_edge = read_json_body(res).await?;
    assert!(
        public_edge.get("note").is_none(),
        "public edge detail must omit free-text notes"
    );

    let res = app
        .clone()
        .oneshot(Request::get("/edges").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::OK);
    let public_edges = read_json_body(res).await?;
    assert!(
        public_edges[0].get("note").is_none(),
        "public edge list must omit free-text notes"
    );

    let res = app
        .oneshot(Request::get("/edges?pagination=cursor&limit=10").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::OK);
    let cursor_page = read_json_body(res).await?;
    assert!(
        cursor_page["items"][0].get("note").is_none(),
        "public cursor edge list must omit free-text notes"
    );

    Ok(())
}

#[tokio::test]
#[serial]
async fn post_edges_rejects_duplicate_id() -> Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    fs::create_dir_all(&in_dir)?;
    let edges_path = in_dir.join("demo.edges.jsonl");
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, state) = app_with_session(Role::Weber, DomainReadSource::Jsonl).await?;

    let body_json = format!(
        r#"{{"id":"{CREATE_EDGE_ID}","source_id":"{CREATE_SOURCE_ID}","source_type":"node","target_id":"{CREATE_TARGET_ID}","target_type":"node","edge_kind":"reference"}}"#
    );
    let res = app
        .clone()
        .oneshot(post_edges(Some(&cookie), &body_json))
        .await?;
    assert_eq!(res.status(), StatusCode::CREATED);

    let res = app.oneshot(post_edges(Some(&cookie), &body_json)).await?;
    assert_eq!(res.status(), StatusCode::CONFLICT);
    let text = read_text_body(res).await?;
    assert!(text.contains("edge id already exists"), "body: {text}");

    // No second JSONL line, cache stays at one edge.
    assert_eq!(jsonl_lines(&edges_path).len(), 1);
    assert_eq!(state.edges.read().await.len(), 1);

    Ok(())
}

#[tokio::test]
#[serial]
async fn post_edges_rejects_invalid_uuid() -> Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    fs::create_dir_all(&in_dir)?;
    let edges_path = in_dir.join("demo.edges.jsonl");
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, state) = app_with_session(Role::Weber, DomainReadSource::Jsonl).await?;

    let body_json = format!(
        r#"{{"source_id":"not-a-uuid","source_type":"node","target_id":"{CREATE_TARGET_ID}","target_type":"node","edge_kind":"reference"}}"#
    );
    let res = app.oneshot(post_edges(Some(&cookie), &body_json)).await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);
    let text = read_text_body(res).await?;
    assert!(text.contains("invalid UUID for source_id"), "body: {text}");

    assert!(!edges_path.exists(), "rejected create must not write JSONL");
    assert_eq!(state.edges.read().await.len(), 0);

    Ok(())
}

#[tokio::test]
#[serial]
async fn post_edges_rejects_invalid_operation_uuid() -> Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);
    let (app, cookie, state) = app_with_session(Role::Weber, DomainReadSource::Jsonl).await?;
    let body_json = format!(
        r#"{{"source_id":"{CREATE_SOURCE_ID}","source_type":"node","target_id":"{CREATE_TARGET_ID}","target_type":"node","edge_kind":"reference","operation_id":"not-a-uuid"}}"#
    );
    let res = app.oneshot(post_edges(Some(&cookie), &body_json)).await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);
    assert!(state.edges.read().await.is_empty());
    Ok(())
}

#[tokio::test]
#[serial]
async fn post_edges_rejects_missing_source_type() -> Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    fs::create_dir_all(&in_dir)?;
    let edges_path = in_dir.join("demo.edges.jsonl");
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, state) = app_with_session(Role::Weber, DomainReadSource::Jsonl).await?;

    // source_type is required by the PR-1 contract; the serde-level rejection
    // (missing field) must surface as a deterministic 400.
    let body_json = format!(
        r#"{{"source_id":"{CREATE_SOURCE_ID}","target_id":"{CREATE_TARGET_ID}","target_type":"node","edge_kind":"reference"}}"#
    );
    let res = app.oneshot(post_edges(Some(&cookie), &body_json)).await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);
    let text = read_text_body(res).await?;
    assert!(text.contains("source_type"), "body: {text}");

    assert!(!edges_path.exists(), "rejected create must not write JSONL");
    assert_eq!(state.edges.read().await.len(), 0);

    Ok(())
}

#[tokio::test]
#[serial]
async fn post_edges_rejects_expires_at() -> Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    fs::create_dir_all(&in_dir)?;
    let edges_path = in_dir.join("demo.edges.jsonl");
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, state) = app_with_session(Role::Weber, DomainReadSource::Jsonl).await?;

    // expires_at must be rejected (deny_unknown_fields), never silently dropped.
    let body_json = format!(
        r#"{{"source_id":"{CREATE_SOURCE_ID}","source_type":"node","target_id":"{CREATE_TARGET_ID}","target_type":"node","edge_kind":"reference","expires_at":"2027-01-01T00:00:00Z"}}"#
    );
    let res = app.oneshot(post_edges(Some(&cookie), &body_json)).await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);
    let text = read_text_body(res).await?;
    assert!(text.contains("expires_at"), "body: {text}");

    assert!(!edges_path.exists(), "rejected create must not write JSONL");
    assert_eq!(state.edges.read().await.len(), 0);

    Ok(())
}

#[tokio::test]
#[serial]
async fn post_edges_rejects_unauthenticated_write() -> Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    fs::create_dir_all(&in_dir)?;
    let edges_path = in_dir.join("demo.edges.jsonl");
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, _cookie, state) = app_with_session(Role::Weber, DomainReadSource::Jsonl).await?;

    // No session cookie -> 401 (require_write), CSRF is skipped without a cookie.
    let res = app.oneshot(post_edges(None, &valid_create_body())).await?;
    assert_eq!(res.status(), StatusCode::UNAUTHORIZED);

    assert!(!edges_path.exists(), "rejected create must not write JSONL");
    assert_eq!(state.edges.read().await.len(), 0);

    Ok(())
}

#[tokio::test]
#[serial]
async fn post_edges_rejects_gast_write() -> Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    fs::create_dir_all(&in_dir)?;
    let edges_path = in_dir.join("demo.edges.jsonl");
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, state) = app_with_session(Role::Gast, DomainReadSource::Jsonl).await?;

    let res = app
        .oneshot(post_edges(Some(&cookie), &valid_create_body()))
        .await?;
    assert_eq!(res.status(), StatusCode::FORBIDDEN);

    assert!(!edges_path.exists(), "rejected create must not write JSONL");
    assert_eq!(state.edges.read().await.len(), 0);

    Ok(())
}

#[tokio::test]
#[serial]
async fn post_edges_blocks_postgres_read_source() -> Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    fs::create_dir_all(&in_dir)?;
    let edges_path = in_dir.join("demo.edges.jsonl");
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, state) = app_with_session(Role::Weber, DomainReadSource::Postgres).await?;

    // Valid payload so the 409 guard outcome cannot be confused with a 400.
    let res = app
        .oneshot(post_edges(Some(&cookie), &valid_create_body()))
        .await?;
    assert_eq!(res.status(), StatusCode::CONFLICT);
    let text = read_text_body(res).await?;
    assert!(
        text.contains("DOMAIN_READ_SOURCE_READ_ONLY"),
        "body: {text}"
    );

    // No restart-invisible JSONL write, no phantom cache entry.
    assert!(!edges_path.exists(), "blocked create must not write JSONL");
    assert_eq!(state.edges.read().await.len(), 0);

    Ok(())
}

#[tokio::test]
#[serial]
async fn post_edges_rejects_invalid_jsonl_read_postgres_write_config() -> Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    fs::create_dir_all(&in_dir)?;
    let edges_path = in_dir.join("demo.edges.jsonl");
    let _env = set_gewebe_in_dir(&in_dir);

    // Config load forbids jsonl-read + postgres-edge-write; a manually
    // constructed state must still be rejected defensively with 500.
    let (app, cookie, state) = app_with_session_and_edge_write(
        Role::Weber,
        DomainReadSource::Jsonl,
        weltgewebe_api::config::DomainEdgeWriteSource::Postgres,
    )
    .await?;

    let res = app
        .oneshot(post_edges(Some(&cookie), &valid_create_body()))
        .await?;
    assert_eq!(res.status(), StatusCode::INTERNAL_SERVER_ERROR);
    let text = read_text_body(res).await?;
    assert!(text.contains("INVALID_DOMAIN_WRITE_CONFIG"), "body: {text}");

    // No JSONL write, no phantom cache entry, no JSONL fallback.
    assert!(!edges_path.exists(), "blocked create must not write JSONL");
    assert_eq!(state.edges.read().await.len(), 0);

    Ok(())
}

#[tokio::test]
#[serial]
async fn post_edges_does_not_update_cache_when_append_fails() -> Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    fs::create_dir_all(&in_dir)?;
    let edges_path = in_dir.join("demo.edges.jsonl");
    let _env = set_gewebe_in_dir(&in_dir);

    // Make the append fail portably: the JSONL path exists as a *directory*,
    // so opening it as a file for append errors out.
    fs::create_dir_all(&edges_path)?;

    let (app, cookie, state) = app_with_session(Role::Weber, DomainReadSource::Jsonl).await?;

    let res = app
        .oneshot(post_edges(Some(&cookie), &valid_create_body()))
        .await?;
    assert_eq!(res.status(), StatusCode::INTERNAL_SERVER_ERROR);
    let text = read_text_body(res).await?;
    assert!(text.contains("failed to persist edge"), "body: {text}");

    // Failed persistence must never leave a phantom edge in memory.
    assert_eq!(state.edges.read().await.len(), 0);

    Ok(())
}

#[tokio::test]
#[serial]
async fn post_edges_preserves_jsonl_boundary_after_unterminated_existing_record() -> Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    let edges_path = in_dir.join("demo.edges.jsonl");
    let _env = set_gewebe_in_dir(&in_dir);

    // Exactly one valid record WITHOUT a trailing newline (write_lines joins
    // lines without a final newline, so this fixture is truly unterminated).
    const OLD_ID: &str = "00000000-0000-0000-0000-0000000000aa";
    let old_line = format!(
        r#"{{"id":"{OLD_ID}","source_id":"{CREATE_SOURCE_ID}","target_id":"{CREATE_TARGET_ID}","edge_kind":"reference"}}"#
    );
    write_lines(&edges_path, &[old_line.as_str()]);
    let contents_before = fs::read_to_string(&edges_path)?;
    assert!(
        !contents_before.ends_with('\n'),
        "fixture must be unterminated for this test"
    );

    let (app, cookie, _state) = app_with_session(Role::Weber, DomainReadSource::Jsonl).await?;

    let res = app
        .clone()
        .oneshot(post_edges(Some(&cookie), &valid_create_body()))
        .await?;
    assert_eq!(res.status(), StatusCode::CREATED);
    let v = read_json_body(res).await?;
    let new_id = v["id"].as_str().context("id must be string")?.to_string();

    // A separator newline keeps the old and the new record on separate,
    // individually parseable lines — no glued JSON objects.
    let contents = fs::read_to_string(&edges_path)?;
    assert!(
        !contents.contains("}{"),
        "records must not be glued: {contents}"
    );
    let lines = jsonl_lines(&edges_path);
    assert_eq!(lines.len(), 2);
    assert_eq!(lines[0]["id"], OLD_ID);
    assert_eq!(lines[1]["id"], new_id.as_str());

    // Both edges are served.
    for id in [OLD_ID, new_id.as_str()] {
        let uri = format!("/edges/{id}");
        let res = app
            .clone()
            .oneshot(Request::get(uri.as_str()).body(body::Body::empty())?)
            .await?;
        assert_eq!(res.status(), StatusCode::OK, "GET /edges/{id}");
    }

    Ok(())
}

#[tokio::test]
#[serial]
async fn post_edges_rejects_create_when_edge_cache_limit_reached() -> Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    let edges_path = in_dir.join("demo.edges.jsonl");
    let _env = set_gewebe_in_dir(&in_dir);
    let _limit = EnvGuard::set("MAX_EDGES_CACHE", "1");

    const OLD_ID: &str = "00000000-0000-0000-0000-0000000000ab";
    let old_line = format!(
        r#"{{"id":"{OLD_ID}","source_id":"{CREATE_SOURCE_ID}","target_id":"{CREATE_TARGET_ID}","edge_kind":"reference"}}"#
    );
    write_lines(&edges_path, &[old_line.as_str()]);

    let (app, cookie, state) = app_with_session(Role::Weber, DomainReadSource::Jsonl).await?;
    assert_eq!(state.edges.read().await.len(), 1);

    // A new edge would land on a line index the loader never materializes
    // after a restart; the write must be rejected instead of going dark.
    let res = app
        .oneshot(post_edges(Some(&cookie), &valid_create_body()))
        .await?;
    assert_eq!(res.status(), StatusCode::CONFLICT);
    let text = read_text_body(res).await?;
    assert!(text.contains("edge cache limit reached"), "body: {text}");

    // No append, cache unchanged.
    assert_eq!(jsonl_lines(&edges_path).len(), 1);
    {
        let cache = state.edges.read().await;
        assert_eq!(cache.len(), 1);
        assert!(cache.get(OLD_ID).is_some());
    }

    Ok(())
}

/// A dated edge with an explicit `expires_at: null` is structurally
/// well-formed but permanently unreachable per `edge_is_active_at` — it must
/// not fill the cache-limit slot that a genuinely reachable edge would need,
/// so a create must succeed instead of hitting the cache-limit gate.
#[tokio::test]
#[serial]
async fn post_edges_admits_create_when_limit_filled_by_permanently_unreachable_row() -> Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    let edges_path = in_dir.join("demo.edges.jsonl");
    let _env = set_gewebe_in_dir(&in_dir);
    let _limit = EnvGuard::set("MAX_EDGES_CACHE", "1");

    const UNREACHABLE_ID: &str = "00000000-0000-0000-0000-0000000000ad";
    let unreachable_line = format!(
        r#"{{"id":"{UNREACHABLE_ID}","source_id":"{CREATE_SOURCE_ID}","target_id":"{CREATE_TARGET_ID}","edge_kind":"reference","created_at":"2026-06-01T00:00:00Z","expires_at":null}}"#
    );
    write_lines(&edges_path, &[unreachable_line.as_str()]);

    let (app, cookie, state) = app_with_session(Role::Weber, DomainReadSource::Jsonl).await?;
    // The permanently unreachable row does not consume the single slot.
    assert_eq!(state.edges.read().await.len(), 0);

    let res = app
        .oneshot(post_edges(Some(&cookie), &valid_create_body()))
        .await?;
    assert_eq!(res.status(), StatusCode::CREATED);

    assert_eq!(jsonl_lines(&edges_path).len(), 2);
    assert_eq!(state.edges.read().await.len(), 1);

    Ok(())
}

#[tokio::test]
#[serial]
async fn post_edges_rejects_duplicate_id_in_unloaded_edge_suffix() -> Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    let edges_path = in_dir.join("demo.edges.jsonl");
    let _env = set_gewebe_in_dir(&in_dir);
    let _limit = EnvGuard::set("MAX_EDGES_CACHE", "1");

    // Two valid records; the second carries the id the POST will reuse.
    const LOADED_ID: &str = "00000000-0000-0000-0000-0000000000ac";
    let loaded_line = format!(
        r#"{{"id":"{LOADED_ID}","source_id":"{CREATE_SOURCE_ID}","target_id":"{CREATE_TARGET_ID}","edge_kind":"reference"}}"#
    );
    let suffix_line = format!(
        r#"{{"id":"{CREATE_EDGE_ID}","source_id":"{CREATE_SOURCE_ID}","target_id":"{CREATE_TARGET_ID}","edge_kind":"reference"}}"#
    );
    write_lines(&edges_path, &[loaded_line.as_str(), suffix_line.as_str()]);

    let (app, cookie, state) = app_with_session(Role::Weber, DomainReadSource::Jsonl).await?;

    // The loader truncated at the limit: the suffix edge is NOT in the cache,
    // so only the file-level inspection can see it.
    {
        let cache = state.edges.read().await;
        assert_eq!(cache.len(), 1);
        assert!(cache.get(LOADED_ID).is_some());
        assert!(cache.get(CREATE_EDGE_ID).is_none());
    }

    let body_json = format!(
        r#"{{"id":"{CREATE_EDGE_ID}","source_id":"{CREATE_SOURCE_ID}","source_type":"node","target_id":"{CREATE_TARGET_ID}","target_type":"node","edge_kind":"reference"}}"#
    );
    let res = app.oneshot(post_edges(Some(&cookie), &body_json)).await?;
    assert_eq!(res.status(), StatusCode::CONFLICT);
    let text = read_text_body(res).await?;
    assert!(text.contains("edge id already exists"), "body: {text}");

    // No append: the file still holds exactly the two fixture records.
    let lines = jsonl_lines(&edges_path);
    assert_eq!(lines.len(), 2);
    assert_eq!(lines[1]["id"], CREATE_EDGE_ID);
    assert_eq!(state.edges.read().await.len(), 1);

    Ok(())
}

#[tokio::test]
#[serial]
async fn post_edges_rejects_create_when_edge_cache_limit_zero_and_file_missing() -> Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    fs::create_dir_all(&in_dir)?;
    let edges_path = in_dir.join("demo.edges.jsonl");
    let _env = set_gewebe_in_dir(&in_dir);
    let _limit = EnvGuard::set("MAX_EDGES_CACHE", "0");

    // No JSONL file: the missing-file branch must consult the limit, not
    // hardcode cache_limit_reached=false.
    let (app, cookie, state) = app_with_session(Role::Weber, DomainReadSource::Jsonl).await?;
    assert_eq!(state.edges.read().await.len(), 0);

    let res = app
        .oneshot(post_edges(Some(&cookie), &valid_create_body()))
        .await?;
    assert_eq!(res.status(), StatusCode::CONFLICT);
    let text = read_text_body(res).await?;
    assert!(text.contains("edge cache limit reached"), "body: {text}");

    // No JSONL written, cache still empty.
    assert!(
        !edges_path.exists(),
        "no JSONL must be written when limit=0"
    );
    assert_eq!(state.edges.read().await.len(), 0);

    Ok(())
}
