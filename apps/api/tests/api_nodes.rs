use anyhow::{Context, Result};
use axum::{
    body,
    http::{Request, StatusCode},
    Router,
};
use serial_test::serial;
mod helpers;

use axum::middleware::from_fn_with_state;
use helpers::set_gewebe_in_dir;
use std::{fs, path::PathBuf, sync::Arc};
use tokio::sync::RwLock;
use tower::ServiceExt;
use weltgewebe_api::{
    auth::{
        accounts::AccountStore, rate_limit::AuthRateLimiter, role::Role, session::SessionBackend,
    },
    config::{
        AppConfig, DomainAccountWriteSource, DomainEdgeWriteSource, DomainNodeWriteSource,
        DomainReadSource,
    },
    middleware::{auth::auth_middleware, csrf::require_csrf},
    routes::{
        accounts::{AccountInternal, AccountPublic},
        api_router,
    },
    state::ApiState,
    telemetry::{BuildInfo, Metrics},
    test_helpers::EnvGuard,
};

async fn create_session(
    state: &ApiState,
    account_id: &str,
    existing_device_id: Option<&str>,
) -> weltgewebe_api::auth::session::Session {
    state
        .sessions
        .create(
            account_id.to_string(),
            existing_device_id.map(std::string::ToString::to_string),
        )
        .await
        .expect("in-memory session backend must create session")
}

async fn test_state() -> Result<ApiState> {
    let metrics = Metrics::try_new(BuildInfo {
        version: "test",
        commit: "test",
        build_timestamp: "test",
    })?;

    let config = AppConfig {
        max_guest_owned_nodes: 1_000,
        domain_read_source: DomainReadSource::Jsonl,
        domain_account_write_source: DomainAccountWriteSource::Jsonl,
        domain_node_write_source: DomainNodeWriteSource::Jsonl,
        domain_edge_write_source: DomainEdgeWriteSource::Jsonl,
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
            weltgewebe_api::routes::nodes::load_nodes().await?,
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

fn make_tmp_dir() -> tempfile::TempDir {
    tempfile::tempdir().expect("tmpdir")
}

fn write_lines(path: &PathBuf, lines: &[&str]) {
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, lines.join("\n")).unwrap();
}

fn node_etag(node: &serde_json::Value) -> String {
    format!(
        "\"{}\"",
        node["updated_at"]
            .as_str()
            .expect("node response must contain updated_at"),
    )
}

/// Helper: Ensures correct setup order (File Write -> Env -> State Init)
async fn app_with_nodes(in_dir: &std::path::Path, lines: &[&str]) -> (Router, EnvGuard) {
    let nodes_path = in_dir.join("demo.nodes.jsonl");
    write_lines(&nodes_path, lines);
    let env = set_gewebe_in_dir(in_dir);
    let state = test_state().await.unwrap();
    let app = Router::new().merge(api_router()).with_state(state);
    (app, env)
}

#[tokio::test]
#[serial]
async fn nodes_bbox_and_limit() -> anyhow::Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");

    let (app, _env) = app_with_nodes(
        &in_dir,
        &[
            r#"{"id":"n1","location":{"lon":9.9,"lat":53.55},"title":"A"}"#,
            r#"{"id":"n2","location":{"lon":11.0,"lat":54.2},"title":"B"}"#,
            r#"{"id":"n3","location":{"lon":10.2,"lat":53.6},"title":"C"}"#,
        ],
    )
    .await;

    // BBox über Hamburg herum (soll n1 & n3 treffen)
    let res = app
        .clone()
        .oneshot(Request::get("/nodes?bbox=9.5,53.4,10.5,53.8&limit=10").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::OK);
    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let v: serde_json::Value = serde_json::from_slice(&body)?;
    let arr = v.as_array().context("must be array")?;
    assert_eq!(arr.len(), 2);
    let ids: Vec<_> = arr
        .iter()
        .map(|x| {
            x.get("id")
                .expect("id missing")
                .as_str()
                .expect("must be string")
                .to_string()
        })
        .collect();
    assert!(ids.contains(&"n1".to_string()) && ids.contains(&"n3".to_string()));

    // Vertauschte BBox-Koordinaten sollen ebenfalls normalisiert werden
    let res = app
        .clone()
        .oneshot(Request::get("/nodes?bbox=10.5,53.8,9.5,53.4&limit=10").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::OK);
    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let v: serde_json::Value = serde_json::from_slice(&body)?;
    let arr = v.as_array().context("must be array")?;
    assert_eq!(arr.len(), 2);
    let ids: Vec<_> = arr
        .iter()
        .map(|x| {
            x.get("id")
                .expect("id missing")
                .as_str()
                .expect("must be string")
                .to_string()
        })
        .collect();
    assert!(ids.contains(&"n1".to_string()) && ids.contains(&"n3".to_string()));

    // Ungültige BBox ergibt 400 Bad Request
    let res = app
        .clone()
        .oneshot(Request::get("/nodes?bbox=oops").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);

    // Limit=1
    let res = app
        .oneshot(Request::get("/nodes?limit=1").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::OK);
    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let v: serde_json::Value = serde_json::from_slice(&body)?;
    assert_eq!(v.as_array().context("must be array")?.len(), 1);

    Ok(())
}

#[tokio::test]
#[serial]
async fn node_details_include_only_current_public_creator_titles() -> anyhow::Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    write_lines(
        &in_dir.join("demo.nodes.jsonl"),
        &[
            r#"{"id":"active-node","location":{"lon":10.0,"lat":53.5},"title":"A","created_at":"2025-01-01T12:00:00Z","updated_at":"2025-01-02T12:00:00Z","created_by_account_id":"active-account"}"#,
            r#"{"id":"disabled-node","location":{"lon":10.1,"lat":53.6},"title":"B","created_by_account_id":"disabled-account"}"#,
            r#"{"id":"missing-node","location":{"lon":10.2,"lat":53.7},"title":"C","created_by_account_id":"missing-account"}"#,
            r#"{"id":"blank-node","location":{"lon":10.3,"lat":53.8},"title":"D","created_by_account_id":"blank-account"}"#,
            r#"{"id":"anonymous-node","location":{"lon":10.4,"lat":53.9},"title":"E"}"#,
        ],
    );
    let _env = set_gewebe_in_dir(&in_dir);

    let mut active = weber_account("active-account");
    active.public.title = "  Öffentliche Garnrolle  ".to_string();
    let mut disabled = weber_account("disabled-account");
    disabled.public.title = "Verborgene Garnrolle".to_string();
    disabled.public.disabled = true;
    let mut blank = weber_account("blank-account");
    blank.public.title = "   ".to_string();
    let mut accounts = AccountStore::new();
    accounts.insert(active);
    accounts.insert(disabled);
    accounts.insert(blank);

    let mut state = test_state().await?;
    state.accounts = Arc::new(RwLock::new(accounts));
    let app = Router::new().merge(api_router()).with_state(state);

    let cases = [
        (
            "active-node",
            Some("active-account"),
            Some("Öffentliche Garnrolle"),
        ),
        ("disabled-node", Some("disabled-account"), None),
        ("missing-node", Some("missing-account"), None),
        ("blank-node", Some("blank-account"), None),
        ("anonymous-node", None, None),
    ];

    for (node_id, expected_creator_id, expected_current_title) in cases {
        let response = app
            .clone()
            .oneshot(Request::get(format!("/nodes/{node_id}")).body(body::Body::empty())?)
            .await?;
        assert_eq!(response.status(), StatusCode::OK);
        let body = body::to_bytes(response.into_body(), usize::MAX).await?;
        let node: serde_json::Value = serde_json::from_slice(&body)?;

        assert_eq!(node["id"], node_id, "flattened node id remains top-level");
        assert!(node.get("node").is_none(), "response remains flat");
        assert_eq!(
            node.get("created_by_account_id")
                .and_then(serde_json::Value::as_str),
            expected_creator_id,
        );
        assert_eq!(
            node.get("created_by_account_current_title")
                .and_then(serde_json::Value::as_str),
            expected_current_title,
        );
        if expected_current_title.is_none() {
            assert!(
                node.get("created_by_account_current_title").is_none(),
                "non-public creator titles are omitted instead of serialized as null",
            );
        }

        let history = node["history"]
            .as_array()
            .context("history must be an array")?;
        assert_eq!(
            history.last().and_then(|event| event["kind"].as_str()),
            Some("created")
        );
        if node_id == "active-node" {
            assert_eq!(history.len(), 2);
            assert_eq!(history[0]["kind"], "updated");
            assert_eq!(history[0]["date"], "2025-01-02T12:00:00Z");
            assert_eq!(history[1]["date"], "2025-01-01T12:00:00Z");
        } else {
            assert_eq!(history.len(), 1);
        }
    }

    Ok(())
}

#[tokio::test]
#[serial]
async fn nodes_patch_info_lifecycle() -> anyhow::Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    let nodes_path = in_dir.join("demo.nodes.jsonl");

    write_lines(
        &nodes_path,
        &[r#"{"id":"n1","location":{"lon":10.0,"lat":53.5},"title":"A","info":"Old Info"}"#],
    );
    let _env = set_gewebe_in_dir(&in_dir);

    // Setup Auth State with Account
    let mut account_map = AccountStore::new();
    let account = AccountPublic {
        id: "cccccccc-cccc-4ccc-8ccc-000000000001".to_string(),
        kind: "garnrolle".to_string(),
        title: "Weber".to_string(),
        summary: None,
        public_pos: None,
        map_state: weltgewebe_api::routes::accounts::GarnrolleMapState::NotOnMap,
        radius_m: 0,

        disabled: false,
        tags: vec![],
    };
    account_map.insert(AccountInternal {
        public: account,
        role: Role::Weber,
        email: Some("cccccccc-cccc-4ccc-8ccc-000000000001@example.com".to_string()),
        webauthn_user_id: uuid::Uuid::new_v4(),
    });

    let mut state = test_state().await?;
    state.accounts = Arc::new(RwLock::new(account_map));

    // Create Session
    let session = create_session(&state, "cccccccc-cccc-4ccc-8ccc-000000000001", None).await;
    let cookie_val = format!("gewebe_session={}", session.id);

    let app = Router::new()
        .merge(api_router())
        .layer(from_fn_with_state(state.clone(), auth_middleware))
        .layer(axum::middleware::from_fn(require_csrf))
        .with_state(state);

    let req = Request::get("/nodes/n1").body(body::Body::empty())?;
    let res = app.clone().oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);
    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let initial: serde_json::Value = serde_json::from_slice(&body)?;
    let mut current_etag = node_etag(&initial);

    // 1. Update info -> "New Info"
    // Note: We MUST provide Origin or Referer because a session cookie is present,
    // otherwise CSRF middleware will block it. The latest node version is also
    // required so an older browser cannot silently overwrite a newer mutation.
    let req = Request::patch("/nodes/n1")
        .header("Content-Type", "application/json")
        .header("Cookie", &cookie_val)
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .header("If-Match", &current_etag)
        .body(body::Body::from(r#"{"info":"New Info"}"#))?;
    let res = app.clone().oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);

    // Check response and carry its exact version into the next mutation.
    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let v: serde_json::Value = serde_json::from_slice(&body)?;
    assert_eq!(v["info"], "New Info");
    current_etag = node_etag(&v);

    // Check persistence by reading via GET
    let req = Request::get("/nodes/n1").body(body::Body::empty())?;
    let res = app.clone().oneshot(req).await?;
    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let v: serde_json::Value = serde_json::from_slice(&body)?;
    assert_eq!(v["info"], "New Info");

    // 2. Empty PATCH (No-op) -> Info remains "New Info"
    let req = Request::patch("/nodes/n1")
        .header("Content-Type", "application/json")
        .header("Cookie", &cookie_val)
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .header("If-Match", &current_etag)
        .body(body::Body::from(r#"{}"#))?;
    let res = app.clone().oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);
    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let v: serde_json::Value = serde_json::from_slice(&body)?;
    assert_eq!(v["info"], "New Info"); // Still there
    current_etag = node_etag(&v);

    // 3. Set info to null -> Info removed
    let req = Request::patch("/nodes/n1")
        .header("Content-Type", "application/json")
        .header("Cookie", &cookie_val)
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .header("If-Match", &current_etag)
        .body(body::Body::from(r#"{"info":null}"#))?;
    let res = app.clone().oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::OK);
    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let v: serde_json::Value = serde_json::from_slice(&body)?;
    assert!(v.get("info").is_none() || v["info"].is_null());

    // Check persistence
    let req = Request::get("/nodes/n1").body(body::Body::empty())?;
    let res = app.clone().oneshot(req).await?;
    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let v: serde_json::Value = serde_json::from_slice(&body)?;
    assert!(v.get("info").is_none() || v["info"].is_null());

    Ok(())
}

#[tokio::test]
#[serial]
async fn nodes_patch_rejects_oversized_info_without_side_effects() -> anyhow::Result<()> {
    const INFO_MAX_LEN: usize = 20_000;
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    let nodes_path = in_dir.join("demo.nodes.jsonl");
    let original_line = r#"{"id":"n1","location":{"lon":10.0,"lat":53.5},"title":"A","info":"Old Info","updated_at":"2026-01-01T00:00:00Z"}"#;
    write_lines(&nodes_path, &[original_line]);
    let _env = set_gewebe_in_dir(&in_dir);

    let mut account_map = AccountStore::new();
    account_map.insert(AccountInternal {
        public: AccountPublic {
            id: "cccccccc-cccc-4ccc-8ccc-000000000001".to_string(),
            kind: "garnrolle".to_string(),
            title: "Weber".to_string(),
            summary: None,
            public_pos: None,
            map_state: weltgewebe_api::routes::accounts::GarnrolleMapState::NotOnMap,
            radius_m: 0,
            disabled: false,
            tags: vec![],
        },
        role: Role::Weber,
        email: Some("cccccccc-cccc-4ccc-8ccc-000000000001@example.com".to_string()),
        webauthn_user_id: uuid::Uuid::new_v4(),
    });

    let mut state = test_state().await?;
    state.accounts = Arc::new(RwLock::new(account_map));
    let session = create_session(&state, "cccccccc-cccc-4ccc-8ccc-000000000001", None).await;
    let cookie = format!("gewebe_session={}", session.id);
    let app = Router::new()
        .merge(api_router())
        .layer(from_fn_with_state(state.clone(), auth_middleware))
        .layer(axum::middleware::from_fn(require_csrf))
        .with_state(state.clone());

    let current = app
        .clone()
        .oneshot(Request::get("/nodes/n1").body(body::Body::empty())?)
        .await?;
    assert_eq!(current.status(), StatusCode::OK);
    let current_body = body::to_bytes(current.into_body(), usize::MAX).await?;
    let current_node: serde_json::Value = serde_json::from_slice(&current_body)?;
    let etag = node_etag(&current_node);

    let request_body = serde_json::json!({ "info": "a".repeat(INFO_MAX_LEN + 1) }).to_string();
    let response = app
        .oneshot(
            Request::patch("/nodes/n1")
                .header("Content-Type", "application/json")
                .header("Cookie", cookie)
                .header("Host", "localhost")
                .header("Origin", "http://localhost")
                .header("If-Match", etag)
                .body(body::Body::from(request_body))?,
        )
        .await?;
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    let response_body = body::to_bytes(response.into_body(), usize::MAX).await?;
    assert_eq!(
        String::from_utf8(response_body.to_vec())?,
        "invalid node patch request: info exceeds the maximum length of 20000"
    );

    assert_eq!(fs::read_to_string(&nodes_path)?, original_line);
    let cached_info = state
        .nodes
        .read()
        .await
        .get("n1")
        .and_then(|node| node.info.clone());
    assert_eq!(cached_info.as_deref(), Some("Old Info"));
    Ok(())
}

async fn assert_nodes_patch_corruption_keeps_canonical_jsonl_unchanged(
    corruption: &[u8],
) -> anyhow::Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    let nodes_path = in_dir.join("demo.nodes.jsonl");
    fs::create_dir_all(&in_dir)?;

    let valid_line = br#"{"id":"n1","location":{"lon":10.0,"lat":53.5},"title":"A","info":"Old Info","updated_at":"2026-01-01T00:00:00Z"}"#;
    let mut startup_bytes = valid_line.to_vec();
    startup_bytes.extend_from_slice(b"\n");
    fs::write(&nodes_path, &startup_bytes)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let mut account_map = AccountStore::new();
    account_map.insert(AccountInternal {
        public: AccountPublic {
            id: "cccccccc-cccc-4ccc-8ccc-000000000001".to_string(),
            kind: "garnrolle".to_string(),
            title: "Weber".to_string(),
            summary: None,
            public_pos: None,
            map_state: weltgewebe_api::routes::accounts::GarnrolleMapState::NotOnMap,
            radius_m: 0,
            disabled: false,
            tags: vec![],
        },
        role: Role::Weber,
        email: Some("cccccccc-cccc-4ccc-8ccc-000000000001@example.com".to_string()),
        webauthn_user_id: uuid::Uuid::new_v4(),
    });

    let mut state = test_state().await?;
    state.accounts = Arc::new(RwLock::new(account_map));

    // Corrupt the canonical file only after a valid startup. This keeps the
    // test focused on the PATCH rewrite path while the startup loader itself
    // remains strict.
    let mut original = startup_bytes;
    original.extend_from_slice(corruption);
    fs::write(&nodes_path, &original)?;
    let session = create_session(&state, "cccccccc-cccc-4ccc-8ccc-000000000001", None).await;
    let cookie = format!("gewebe_session={}", session.id);
    let app = Router::new()
        .merge(api_router())
        .layer(from_fn_with_state(state.clone(), auth_middleware))
        .layer(axum::middleware::from_fn(require_csrf))
        .with_state(state.clone());

    let current = app
        .clone()
        .oneshot(Request::get("/nodes/n1").body(body::Body::empty())?)
        .await?;
    assert_eq!(current.status(), StatusCode::OK);
    let current_body = body::to_bytes(current.into_body(), usize::MAX).await?;
    let current_node: serde_json::Value = serde_json::from_slice(&current_body)?;
    let etag = node_etag(&current_node);

    let response = app
        .oneshot(
            Request::patch("/nodes/n1")
                .header("Content-Type", "application/json")
                .header("Cookie", cookie)
                .header("Host", "localhost")
                .header("Origin", "http://localhost")
                .header("If-Match", etag)
                .body(body::Body::from(r#"{"info":"New Info"}"#))?,
        )
        .await?;
    assert_eq!(response.status(), StatusCode::INTERNAL_SERVER_ERROR);

    assert_eq!(fs::read(&nodes_path)?, original);
    let cached_info = state
        .nodes
        .read()
        .await
        .get("n1")
        .and_then(|node| node.info.clone());
    assert_eq!(cached_info.as_deref(), Some("Old Info"));
    assert_eq!(
        fs::read_dir(&in_dir)?
            .filter_map(|entry| entry.ok())
            .filter(|entry| {
                entry
                    .file_name()
                    .to_string_lossy()
                    .starts_with("demo.nodes.jsonl.tmp.")
            })
            .count(),
        0,
        "failed PATCH must remove its temporary rewrite file"
    );

    Ok(())
}

#[tokio::test]
#[serial]
async fn nodes_patch_read_error_keeps_canonical_jsonl_unchanged() -> anyhow::Result<()> {
    assert_nodes_patch_corruption_keeps_canonical_jsonl_unchanged(&[0xff, 0xfe, b'\n']).await
}

#[tokio::test]
#[serial]
async fn nodes_patch_unprojectable_retained_record_keeps_canonical_jsonl_unchanged(
) -> anyhow::Result<()> {
    assert_nodes_patch_corruption_keeps_canonical_jsonl_unchanged(b"{\"id\":\"bad\"}\n").await
}

#[tokio::test]
#[serial]
async fn postgres_read_source_blocks_node_patch_without_persisting() -> anyhow::Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    let nodes_path = in_dir.join("demo.nodes.jsonl");
    let original_line =
        r#"{"id":"n1","location":{"lon":10.0,"lat":53.5},"title":"A","info":"Old Info"}"#;
    write_lines(&nodes_path, &[original_line]);
    let _env = set_gewebe_in_dir(&in_dir);

    let mut account_map = AccountStore::new();
    let account = AccountPublic {
        id: "cccccccc-cccc-4ccc-8ccc-000000000001".to_string(),
        kind: "garnrolle".to_string(),
        title: "Weber".to_string(),
        summary: None,
        public_pos: None,
        map_state: weltgewebe_api::routes::accounts::GarnrolleMapState::NotOnMap,
        radius_m: 0,
        disabled: false,
        tags: vec![],
    };
    account_map.insert(AccountInternal {
        public: account,
        role: Role::Weber,
        email: Some("cccccccc-cccc-4ccc-8ccc-000000000001@example.com".to_string()),
        webauthn_user_id: uuid::Uuid::new_v4(),
    });

    let mut state = test_state().await?;
    state.config.domain_read_source = DomainReadSource::Postgres;
    state.accounts = Arc::new(RwLock::new(account_map));

    let session = create_session(&state, "cccccccc-cccc-4ccc-8ccc-000000000001", None).await;
    let cookie_val = format!("gewebe_session={}", session.id);

    let app = Router::new()
        .merge(api_router())
        .layer(from_fn_with_state(state.clone(), auth_middleware))
        .layer(axum::middleware::from_fn(require_csrf))
        .with_state(state.clone());

    // No If-Match on purpose: the storage-mode guard is a normal request
    // applicability check and must reject this mutation before preconditions.
    let req = Request::patch("/nodes/n1")
        .header("Content-Type", "application/json")
        .header("Cookie", &cookie_val)
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .body(body::Body::from(r#"{"info":"New Info"}"#))?;
    let res = app.clone().oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::CONFLICT);
    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let response = String::from_utf8(bytes.to_vec())?;
    assert!(response.contains("DOMAIN_READ_SOURCE_READ_ONLY"));

    let cached_info = state
        .nodes
        .read()
        .await
        .get("n1")
        .and_then(|node| node.info.as_deref().map(str::to_string));
    assert_eq!(cached_info.as_deref(), Some("Old Info"));
    assert_eq!(fs::read_to_string(&nodes_path)?, original_line);

    Ok(())
}

#[tokio::test]
#[serial]
async fn nodes_accept_string_coordinates() -> anyhow::Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");

    let (app, _env) = app_with_nodes(
        &in_dir,
        &[r#"{"id":"n1","location":{"lon":"9.9","lat":"53.55"},"title":"A"}"#],
    )
    .await;

    let res = app
        .oneshot(Request::get("/nodes").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::OK);

    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let v: serde_json::Value = serde_json::from_slice(&body)?;
    let arr = v.as_array().context("must be array")?;
    assert_eq!(arr.len(), 1);

    let node = arr.first().context("node missing")?;
    assert_eq!(node.get("id").and_then(|value| value.as_str()), Some("n1"));
    assert_eq!(
        node.get("location")
            .and_then(|value| value.get("lat"))
            .and_then(|value| value.as_f64()),
        Some(53.55)
    );
    assert_eq!(
        node.get("location")
            .and_then(|value| value.get("lon"))
            .and_then(|value| value.as_f64()),
        Some(9.9)
    );

    Ok(())
}

#[tokio::test]
#[serial]
async fn nodes_fill_missing_updated_at_from_created_at() -> anyhow::Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");

    let (app, _env) = app_with_nodes(&in_dir, &[
        r#"{"id":"n1","location":{"lon":9.9,"lat":53.55},"title":"A","created_at":"2024-01-02T03:04:05Z"}"#,
    ]).await;

    let res = app
        .oneshot(Request::get("/nodes").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::OK);

    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let v: serde_json::Value = serde_json::from_slice(&body)?;
    let arr = v.as_array().context("must be array")?;
    assert_eq!(arr.len(), 1);

    let node = arr.first().context("node missing")?;
    let created_at = node
        .get("created_at")
        .and_then(|value| value.as_str())
        .context("created_at missing")?;
    let updated_at = node
        .get("updated_at")
        .and_then(|value| value.as_str())
        .context("updated_at missing")?;

    assert_eq!(created_at, "2024-01-02T03:04:05Z");
    assert_eq!(updated_at, created_at);

    Ok(())
}

#[tokio::test]
#[serial]
async fn nodes_patch_without_origin_fails() -> anyhow::Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    let nodes = in_dir.join("demo.nodes.jsonl");
    let _env = set_gewebe_in_dir(&in_dir);

    // Initial node
    write_lines(
        &nodes,
        &[r#"{"id":"n1","location":{"lon":10.0,"lat":53.5},"title":"A","info":"Old Info"}"#],
    );

    // Setup Auth State with Account
    let mut account_map = AccountStore::new();
    let account = AccountPublic {
        id: "cccccccc-cccc-4ccc-8ccc-000000000001".to_string(),
        kind: "garnrolle".to_string(),
        title: "Weber".to_string(),
        summary: None,
        public_pos: None,
        map_state: weltgewebe_api::routes::accounts::GarnrolleMapState::NotOnMap,
        radius_m: 0,

        disabled: false,
        tags: vec![],
    };
    account_map.insert(AccountInternal {
        public: account,
        role: Role::Weber,
        email: Some("cccccccc-cccc-4ccc-8ccc-000000000001@example.com".to_string()),
        webauthn_user_id: uuid::Uuid::new_v4(),
    });

    let mut state = test_state().await?;
    state.accounts = Arc::new(RwLock::new(account_map));

    // Create Session
    let session = create_session(&state, "cccccccc-cccc-4ccc-8ccc-000000000001", None).await;
    let cookie_val = format!("gewebe_session={}", session.id);

    let app = Router::new()
        .merge(api_router())
        .layer(from_fn_with_state(state.clone(), auth_middleware))
        .layer(axum::middleware::from_fn(require_csrf))
        .with_state(state);

    // Attempt PATCH with cookie but NO Origin/Referer logic that satisfies CSRF.
    // We explicitly set Host to ensure the 403 comes from the missing Origin/Referer check,
    // not from a missing Host header check.
    let req = Request::patch("/nodes/n1")
        .header("Content-Type", "application/json")
        .header("Cookie", &cookie_val)
        .header("Host", "localhost")
        // No Origin, No Referer
        .body(body::Body::from(r#"{"info":"Hacked Info"}"#))?;

    let res = app.clone().oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::FORBIDDEN);

    Ok(())
}

#[tokio::test]
#[serial]
async fn nodes_offset_pagination() -> anyhow::Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");

    let (app, _env) = app_with_nodes(
        &in_dir,
        &[
            r#"{"id":"n1","location":{"lon":9.9,"lat":53.55},"title":"A"}"#,
            r#"{"id":"n2","location":{"lon":10.1,"lat":53.6},"title":"B"}"#,
            r#"{"id":"n3","location":{"lon":10.2,"lat":53.7},"title":"C"}"#,
        ],
    )
    .await;

    // limit=1&offset=0 liefert erstes Element (n1)
    let res = app
        .clone()
        .oneshot(Request::get("/nodes?limit=1&offset=0").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::OK);
    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let v: serde_json::Value = serde_json::from_slice(&body)?;
    let arr = v.as_array().context("must be array")?;
    assert_eq!(arr.len(), 1);
    assert_eq!(arr[0]["id"], "n1");

    // limit=1&offset=1 liefert zweites Element (n2)
    let res = app
        .clone()
        .oneshot(Request::get("/nodes?limit=1&offset=1").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::OK);
    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let v: serde_json::Value = serde_json::from_slice(&body)?;
    let arr = v.as_array().context("must be array")?;
    assert_eq!(arr.len(), 1);
    assert_eq!(arr[0]["id"], "n2");

    // Offset außerhalb der Ergebnislänge liefert leere Liste
    let res = app
        .clone()
        .oneshot(Request::get("/nodes?limit=10&offset=100").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::OK);
    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let v: serde_json::Value = serde_json::from_slice(&body)?;
    let arr = v.as_array().context("must be array")?;
    assert_eq!(arr.len(), 0);

    // Ungültiger Offset (negativ) -> 400
    let res = app
        .clone()
        .oneshot(Request::get("/nodes?offset=-1").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);

    // Ungültiger Offset (kein Integer) -> 400
    let res = app
        .oneshot(Request::get("/nodes?offset=abc").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);

    Ok(())
}

#[tokio::test]
#[serial]
async fn nodes_invalid_limit() -> anyhow::Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");

    let (app, _env) = app_with_nodes(
        &in_dir,
        &[r#"{"id":"n1","location":{"lon":9.9,"lat":53.55},"title":"A"}"#],
    )
    .await;

    // limit=abc -> 400
    let res = app
        .clone()
        .oneshot(Request::get("/nodes?limit=abc").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);

    // limit=-1 -> 400
    let res = app
        .oneshot(Request::get("/nodes?limit=-1").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);

    Ok(())
}

#[tokio::test]
#[serial]
async fn nodes_robustness_with_dirty_data() -> anyhow::Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");

    // We verify:
    // n1: Clean
    // n2: Kind is number -> Unknown
    // n3: Summary is array -> None
    // n4: Title is boolean -> Untitled
    // n5: Info is object -> None
    let (app, _env) = app_with_nodes(
        &in_dir,
        &[
            r#"{"id": "n1", "kind": "Clean", "title": "Clean Node", "location": {"lat": 52.5, "lon": 13.4}}"#,
            r#"{"id": "n2", "kind": 123, "title": "Dirty Kind", "location": {"lat": 52.5, "lon": 13.4}}"#,
            r#"{"id": "n3", "kind": "Dirty Summary", "title": "Dirty Summary", "summary": ["oops"], "location": {"lat": 52.5, "lon": 13.4}}"#,
            r#"{"id": "n4", "kind": "Dirty Title", "title": true, "location": {"lat": 52.5, "lon": 13.4}}"#,
            r#"{"id": "n5", "kind": "Dirty Info", "title": "Dirty Info", "info": {"foo": "bar"}, "location": {"lat": 52.5, "lon": 13.4}}"#,
            r#"{"id": "n6", "title": "Dirty Tags", "tags": ["clean", 123, null, "also-clean"], "location": {"lat": 52.5, "lon": 13.4}}"#,
        ],
    )
    .await;

    let res = app
        .oneshot(Request::get("/nodes").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::OK);

    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let v: serde_json::Value = serde_json::from_slice(&body)?;
    let arr = v.as_array().context("must be array")?;

    // Check that all 6 nodes loaded (robustness)
    assert_eq!(arr.len(), 6);

    // Helper to find node by id
    let find_node = |id: &str| arr.iter().find(|n| n["id"] == id).unwrap();

    // n1: Clean
    let n1 = find_node("n1");
    assert_eq!(n1["kind"], "Clean");

    // n2: Kind is number -> Unknown
    let n2 = find_node("n2");
    assert_eq!(n2["kind"], "Unknown");

    // n3: Summary is array -> None (null)
    let n3 = find_node("n3");
    assert!(n3.get("summary").is_none() || n3["summary"].is_null());

    // n4: Title is boolean -> Untitled
    let n4 = find_node("n4");
    assert_eq!(n4["title"], "Untitled");

    // n5: Info is object -> None (null)
    let n5 = find_node("n5");
    assert!(n5.get("info").is_none() || n5["info"].is_null());

    // n6: Tags mixed -> ["clean", "also-clean"]
    let n6 = find_node("n6");
    let tags = n6["tags"].as_array().expect("tags must be array");
    assert_eq!(tags.len(), 2);
    assert_eq!(tags[0], "clean");
    assert_eq!(tags[1], "also-clean");

    Ok(())
}

#[tokio::test]
#[serial]
async fn nodes_limit_is_clamped_to_max_page_size() -> anyhow::Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");

    // Seed more nodes than the page-size cap so the clamp is observable.
    let lines: Vec<String> = (0..1100)
        .map(|i| format!(r#"{{"id":"n{i}","location":{{"lon":10.0,"lat":53.5}},"title":"N{i}"}}"#))
        .collect();
    let line_refs: Vec<&str> = lines.iter().map(String::as_str).collect();

    let (app, _env) = app_with_nodes(&in_dir, &line_refs).await;

    // A limit above MAX_PAGE_SIZE (1000) must be clamped, mirroring /edges.
    let res = app
        .oneshot(Request::get("/nodes?limit=5000").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::OK);
    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let v: serde_json::Value = serde_json::from_slice(&body)?;
    assert_eq!(v.as_array().context("must be array")?.len(), 1000);

    Ok(())
}

#[tokio::test]
#[serial]
async fn nodes_cursor_pagination_envelope_and_walk() -> anyhow::Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");

    // Insertion order is deliberately unsorted to prove cursor mode sorts by
    // stable id ascending, independent of the legacy file/insertion order.
    let (app, _env) = app_with_nodes(
        &in_dir,
        &[
            r#"{"id":"n3","location":{"lon":10.0,"lat":53.5},"title":"C"}"#,
            r#"{"id":"n1","location":{"lon":10.0,"lat":53.5},"title":"A"}"#,
            r#"{"id":"n5","location":{"lon":10.0,"lat":53.5},"title":"E"}"#,
            r#"{"id":"n2","location":{"lon":10.0,"lat":53.5},"title":"B"}"#,
            r#"{"id":"n4","location":{"lon":10.0,"lat":53.5},"title":"D"}"#,
        ],
    )
    .await;

    // Page 1: cursor mode returns the {items, page} envelope, not a bare array.
    let res = app
        .clone()
        .oneshot(Request::get("/nodes?pagination=cursor&limit=2").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::OK);
    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let v: serde_json::Value = serde_json::from_slice(&body)?;
    assert!(v.is_object(), "cursor mode must return an envelope object");
    let items = v["items"].as_array().context("items must be array")?;
    assert_eq!(items.len(), 2);
    assert_eq!(items[0]["id"], "n1");
    assert_eq!(items[1]["id"], "n2");
    assert_eq!(v["page"]["limit"], 2);
    assert_eq!(v["page"]["has_more"], true);
    let cursor1 = v["page"]["next_cursor"]
        .as_str()
        .context("next_cursor must be present on page 1")?
        .to_string();

    // Page 2: following next_cursor yields the next id-ordered slice with no
    // duplicates from page 1.
    let uri = format!("/nodes?cursor={cursor1}&limit=2");
    let res = app
        .clone()
        .oneshot(Request::get(uri.as_str()).body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::OK);
    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let v: serde_json::Value = serde_json::from_slice(&body)?;
    let items = v["items"].as_array().context("items must be array")?;
    assert_eq!(items.len(), 2);
    assert_eq!(items[0]["id"], "n3");
    assert_eq!(items[1]["id"], "n4");
    assert_eq!(v["page"]["has_more"], true);
    let cursor2 = v["page"]["next_cursor"]
        .as_str()
        .context("next_cursor must be present on page 2")?
        .to_string();

    // Page 3 (last): has_more is false and next_cursor is null.
    let uri = format!("/nodes?cursor={cursor2}&limit=2");
    let res = app
        .oneshot(Request::get(uri.as_str()).body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::OK);
    let body = body::to_bytes(res.into_body(), usize::MAX).await?;
    let v: serde_json::Value = serde_json::from_slice(&body)?;
    let items = v["items"].as_array().context("items must be array")?;
    assert_eq!(items.len(), 1);
    assert_eq!(items[0]["id"], "n5");
    assert_eq!(v["page"]["has_more"], false);
    assert!(
        v["page"]["next_cursor"].is_null(),
        "last page next_cursor must be null"
    );

    Ok(())
}

#[tokio::test]
#[serial]
async fn nodes_cursor_invalid_is_bad_request() -> anyhow::Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    let (app, _env) = app_with_nodes(
        &in_dir,
        &[r#"{"id":"n1","location":{"lon":9.9,"lat":53.55},"title":"A"}"#],
    )
    .await;

    // A malformed cursor token is a deterministic 400, never silent mis-pagination.
    let res = app
        .oneshot(Request::get("/nodes?cursor=zz").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);

    Ok(())
}

#[tokio::test]
#[serial]
async fn nodes_cursor_limit_is_clamped_to_max_page_size() -> anyhow::Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");

    // Zero-padded ids keep lexicographic order aligned with numeric order.
    let lines: Vec<String> = (0..1100)
        .map(|i| {
            format!(r#"{{"id":"n{i:04}","location":{{"lon":10.0,"lat":53.5}},"title":"N{i}"}}"#)
        })
        .collect();
    let line_refs: Vec<&str> = lines.iter().map(String::as_str).collect();
    let (app, _env) = app_with_nodes(&in_dir, &line_refs).await;

    // Even in cursor mode, a limit above MAX_PAGE_SIZE (1000) is clamped.
    let res = app
        .oneshot(Request::get("/nodes?pagination=cursor&limit=5000").body(body::Body::empty())?)
        .await?;
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
#[serial]
async fn nodes_cursor_limit_zero_is_bad_request() -> anyhow::Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");

    let lines = vec![
        r#"{"id":"n1","location":{"lon":10.0,"lat":53.5},"title":"N1"}"#,
        r#"{"id":"n2","location":{"lon":10.0,"lat":53.5},"title":"N2"}"#,
    ];
    let (app, _env) = app_with_nodes(&in_dir, &lines).await;

    // In cursor mode, limit=0 must return 400 Bad Request.
    let res = app
        .oneshot(Request::get("/nodes?pagination=cursor&limit=0").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);

    Ok(())
}

// ── POST /nodes ──────────────────────────────────────────────────────────

fn weber_account(id: &str) -> AccountInternal {
    AccountInternal {
        public: AccountPublic {
            id: id.to_string(),
            kind: "garnrolle".to_string(),
            title: "Weber".to_string(),
            summary: None,
            public_pos: None,
            map_state: weltgewebe_api::routes::accounts::GarnrolleMapState::NotOnMap,
            radius_m: 0,
            disabled: false,
            tags: vec![],
        },
        role: Role::Weber,
        email: Some(format!("{id}@example.com")),
        webauthn_user_id: uuid::Uuid::new_v4(),
    }
}

fn gast_account(id: &str) -> AccountInternal {
    let mut account = weber_account(id);
    account.role = Role::Gast;
    account
}

async fn app_with_account(
    in_dir: &std::path::Path,
    account: AccountInternal,
) -> (Router, String, ApiState, EnvGuard) {
    let env = set_gewebe_in_dir(in_dir);
    let id = account.public.id.clone();

    let mut account_map = AccountStore::new();
    account_map.insert(account);

    let mut state = test_state().await.unwrap();
    state.accounts = Arc::new(RwLock::new(account_map));

    let session = create_session(&state, &id, None).await;
    let cookie = format!("gewebe_session={}", session.id);

    let app = Router::new()
        .merge(api_router())
        .layer(from_fn_with_state(state.clone(), auth_middleware))
        .layer(axum::middleware::from_fn(require_csrf))
        .with_state(state.clone());

    (app, cookie, state, env)
}

fn post_node_req(cookie: Option<&str>, json_body: &str) -> Request<body::Body> {
    let mut builder = Request::post("/nodes")
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

#[tokio::test]
#[serial]
async fn nodes_post_creates_persists_and_reloads() -> anyhow::Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;

    let (app, cookie, state, _env) = app_with_account(
        &in_dir,
        weber_account("cccccccc-cccc-4ccc-8ccc-000000000001"),
    )
    .await;

    let body = r#"{"title":"New Node","kind":"Werkstatt","address":"Musterstraße 1, 12345 Musterstadt","location":{"lat":53.55,"lon":9.99},"summary":"Short summary","tags":["a","b"],"operation_id":"10000000-0000-0000-0000-000000000010"}"#;
    let res = app
        .clone()
        .oneshot(post_node_req(Some(&cookie), body))
        .await?;
    assert_eq!(res.status(), StatusCode::CREATED);

    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let created: serde_json::Value = serde_json::from_slice(&bytes)?;
    let id = created["id"].as_str().context("id present")?.to_string();
    assert!(
        uuid::Uuid::parse_str(&id).is_ok(),
        "server must generate a UUID id"
    );
    assert_eq!(created["title"], "New Node");
    assert_eq!(created["kind"], "Werkstatt");
    assert_eq!(created["address"], "Musterstraße 1, 12345 Musterstadt");
    assert_eq!(created["location"]["lat"], 53.55);
    assert_eq!(created["location"]["lon"], 9.99);
    assert_eq!(created["created_at"], created["updated_at"]);

    // JSONL durably appended.
    let nodes_path = in_dir.join("demo.nodes.jsonl");
    let contents = fs::read_to_string(&nodes_path)?;
    assert_eq!(contents.lines().count(), 1);
    let record: serde_json::Value = serde_json::from_str(contents.lines().next().unwrap())?;
    assert_eq!(record["address"], "Musterstraße 1, 12345 Musterstadt");

    // Cache updated (read-your-writes).
    {
        let nodes = state.nodes.read().await;
        let node = nodes.get(&id).expect("created node present in cache");
        assert_eq!(node.title, "New Node");
        assert_eq!(
            node.address.as_deref(),
            Some("Musterstraße 1, 12345 Musterstadt")
        );
    }

    // Simulated restart: reload from JSONL alone reconstructs the node.
    let reloaded = weltgewebe_api::routes::nodes::load_nodes().await?;
    let node = reloaded.get(&id).expect("node must reload after restart");
    assert_eq!(node.title, "New Node");
    assert_eq!(
        node.address.as_deref(),
        Some("Musterstraße 1, 12345 Musterstadt")
    );
    assert_eq!(node.tags, vec!["a".to_string(), "b".to_string()]);

    Ok(())
}

#[tokio::test]
#[serial]
async fn nodes_post_rejects_corrupt_existing_jsonl_after_startup() -> anyhow::Result<()> {
    for corrupt_line in [
        r#"{broken_json"#,
        r#"{"id":"missing-required-node-fields"}"#,
    ] {
        let tmp = make_tmp_dir();
        let in_dir = tmp.path().join("in");
        std::fs::create_dir_all(&in_dir)?;

        let (app, cookie, state, _env) = app_with_account(
            &in_dir,
            weber_account("cccccccc-cccc-4ccc-8ccc-000000000001"),
        )
        .await;
        let nodes_path = in_dir.join("demo.nodes.jsonl");
        fs::write(&nodes_path, corrupt_line)?;
        let before = fs::read(&nodes_path)?;
        let request_body = r#"{"title":"New Node","kind":"Werkstatt","address":"Musterstraße 1","location":{"lat":53.55,"lon":9.99},"operation_id":"10000000-0000-0000-0000-000000000099"}"#;

        let res = app
            .oneshot(post_node_req(Some(&cookie), request_body))
            .await?;
        assert_eq!(res.status(), StatusCode::INTERNAL_SERVER_ERROR);
        let response = body::to_bytes(res.into_body(), usize::MAX).await?;
        let text = String::from_utf8_lossy(&response);
        assert!(
            text.contains("failed to inspect node create operation"),
            "body: {text}"
        );

        assert_eq!(
            fs::read(&nodes_path)?,
            before,
            "corrupt source must stay byte-identical"
        );
        assert_eq!(
            state.nodes.read().await.len(),
            0,
            "failed create must not populate cache"
        );
    }

    Ok(())
}

#[tokio::test]
#[serial]
async fn nodes_post_replays_same_operation_without_second_write() -> anyhow::Result<()> {
    const OPERATION_ID: &str = "10000000-0000-0000-0000-000000000001";
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;

    let (app, cookie, _initial_state, _env) = app_with_account(
        &in_dir,
        weber_account("cccccccc-cccc-4ccc-8ccc-000000000001"),
    )
    .await;
    let request_body = format!(
        r#"{{"title":"Retry Safe Node","kind":"Werkstatt","address":"Musterstraße 1","location":{{"lat":53.55,"lon":9.99}},"summary":"Same action","operation_id":"{OPERATION_ID}"}}"#
    );

    let first_attempt = app
        .clone()
        .oneshot(post_node_req(Some(&cookie), &request_body));
    let second_attempt = app
        .clone()
        .oneshot(post_node_req(Some(&cookie), &request_body));
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
    let first_body = body::to_bytes(first.into_body(), usize::MAX).await?;
    let second_body = body::to_bytes(second.into_body(), usize::MAX).await?;
    let first_node: serde_json::Value = serde_json::from_slice(&first_body)?;
    let second_node: serde_json::Value = serde_json::from_slice(&second_body)?;
    assert_eq!(first_node["id"], second_node["id"]);

    // Build a fresh API/cache/session state to simulate a restart after the
    // server committed but the first response was lost.
    let (restarted_app, restarted_cookie, restarted_state, _restart_env) = app_with_account(
        &in_dir,
        weber_account("cccccccc-cccc-4ccc-8ccc-000000000001"),
    )
    .await;
    let replay = restarted_app
        .clone()
        .oneshot(post_node_req(Some(&restarted_cookie), &request_body))
        .await?;
    assert_eq!(replay.status(), StatusCode::OK);
    let replay_body = body::to_bytes(replay.into_body(), usize::MAX).await?;
    let replay_node: serde_json::Value = serde_json::from_slice(&replay_body)?;
    assert_eq!(replay_node["id"], first_node["id"]);
    assert!(replay_node.get("operation_id").is_none());
    assert!(replay_node.get("_create_operation_id").is_none());

    let nodes_path = in_dir.join("demo.nodes.jsonl");
    let contents = fs::read_to_string(&nodes_path)?;
    assert_eq!(contents.lines().count(), 1);
    let record: serde_json::Value = serde_json::from_str(contents.lines().next().unwrap())?;
    assert_eq!(
        record["_create_actor_id"],
        "cccccccc-cccc-4ccc-8ccc-000000000001"
    );
    assert_eq!(record["_create_operation_id"], OPERATION_ID);
    assert_eq!(restarted_state.nodes.read().await.len(), 1);

    // The key identifies one semantic action. Reusing it for changed data is a
    // conflict and must still leave exactly one durable node.
    let changed_body = format!(
        r#"{{"title":"Different Node","kind":"Werkstatt","address":"Musterstraße 1","location":{{"lat":53.55,"lon":9.99}},"summary":"Same action","operation_id":"{OPERATION_ID}"}}"#
    );
    let conflict = restarted_app
        .oneshot(post_node_req(Some(&restarted_cookie), &changed_body))
        .await?;
    assert_eq!(conflict.status(), StatusCode::CONFLICT);
    assert_eq!(fs::read_to_string(&nodes_path)?.lines().count(), 1);

    // The same UUID belongs to the account/action pair, not globally to all
    // users. Another account may independently use it for a different node.
    let (other_app, other_cookie, _other_state, _other_env) = app_with_account(
        &in_dir,
        weber_account("dddddddd-dddd-4ddd-8ddd-000000000002"),
    )
    .await;
    let other = other_app
        .oneshot(post_node_req(Some(&other_cookie), &changed_body))
        .await?;
    assert_eq!(other.status(), StatusCode::CREATED);
    assert_eq!(fs::read_to_string(nodes_path)?.lines().count(), 2);

    Ok(())
}

#[tokio::test]
#[serial]
async fn nodes_post_requires_authentication() -> anyhow::Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let state = test_state().await?;
    let app = Router::new()
        .merge(api_router())
        .layer(from_fn_with_state(state.clone(), auth_middleware))
        .layer(axum::middleware::from_fn(require_csrf))
        .with_state(state);

    let body = r#"{"title":"New Node","kind":"Werkstatt","address":"Somewhere","location":{"lat":53.55,"lon":9.99}}"#;
    let res = app.oneshot(post_node_req(None, body)).await?;
    assert_eq!(res.status(), StatusCode::UNAUTHORIZED);

    Ok(())
}

#[tokio::test]
#[serial]
async fn nodes_post_allows_gast_and_binds_creator() -> anyhow::Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let guest_id = "eeeeeeee-eeee-4eee-8eee-000000000003";

    let (app, cookie, state, _env) = app_with_account(&in_dir, gast_account(guest_id)).await;

    let body = r#"{"title":"Gastknoten","kind":"Werkstatt","address":"Somewhere","location":{"lat":53.55,"lon":9.99},"operation_id":"10000000-0000-0000-0000-000000000011"}"#;
    let res = app.oneshot(post_node_req(Some(&cookie), body)).await?;
    assert_eq!(res.status(), StatusCode::CREATED);
    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let created: serde_json::Value = serde_json::from_slice(&bytes)?;
    assert_eq!(created["created_by_account_id"], guest_id);

    let id = created["id"].as_str().context("created node id")?;
    let nodes = state.nodes.read().await;
    let node = nodes.get(id).context("guest node in cache")?;
    assert_eq!(node.created_by_account_id.as_deref(), Some(guest_id));

    Ok(())
}

#[tokio::test]
#[serial]
async fn nodes_post_without_origin_fails() -> anyhow::Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let env = set_gewebe_in_dir(&in_dir);

    let mut account_map = AccountStore::new();
    account_map.insert(weber_account("cccccccc-cccc-4ccc-8ccc-000000000001"));
    let mut state = test_state().await?;
    state.accounts = Arc::new(RwLock::new(account_map));
    let session = create_session(&state, "cccccccc-cccc-4ccc-8ccc-000000000001", None).await;
    let cookie_val = format!("gewebe_session={}", session.id);

    let app = Router::new()
        .merge(api_router())
        .layer(from_fn_with_state(state.clone(), auth_middleware))
        .layer(axum::middleware::from_fn(require_csrf))
        .with_state(state);

    let req = Request::post("/nodes")
        .header("Content-Type", "application/json")
        .header("Cookie", &cookie_val)
        .header("Host", "localhost")
        // No Origin, No Referer.
        .body(body::Body::from(
            r#"{"title":"New Node","kind":"Werkstatt","address":"Somewhere","location":{"lat":53.55,"lon":9.99}}"#,
        ))?;
    let res = app.oneshot(req).await?;
    assert_eq!(res.status(), StatusCode::FORBIDDEN);
    drop(env);

    Ok(())
}

#[tokio::test]
#[serial]
async fn nodes_post_rejects_invalid_payloads_with_stable_400() -> anyhow::Result<()> {
    let tmp = make_tmp_dir();
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;

    let (app, cookie, state, _env) = app_with_account(
        &in_dir,
        weber_account("cccccccc-cccc-4ccc-8ccc-000000000001"),
    )
    .await;

    let cases = [
        // Missing required address.
        r#"{"title":"No Address","kind":"Werkstatt","location":{"lat":53.5,"lon":10.0}}"#,
        // Missing required title.
        r#"{"kind":"Werkstatt","address":"Somewhere","location":{"lat":53.5,"lon":10.0}}"#,
        // Missing required kind.
        r#"{"title":"No Kind","address":"Somewhere","location":{"lat":53.5,"lon":10.0}}"#,
        // Missing required location.
        r#"{"title":"No Location","kind":"Werkstatt","address":"Somewhere"}"#,
        // Blank title after trim.
        r#"{"title":"   ","kind":"Werkstatt","address":"Somewhere","location":{"lat":53.5,"lon":10.0}}"#,
        // Out-of-bounds latitude.
        r#"{"title":"Bad Lat","kind":"Werkstatt","address":"Somewhere","location":{"lat":999.0,"lon":10.0}}"#,
        // Non-finite longitude.
        r#"{"title":"Bad Lon","kind":"Werkstatt","address":"Somewhere","location":{"lat":10.0,"lon":1e400}}"#,
        // Unknown top-level field.
        r#"{"title":"Extra","kind":"Werkstatt","address":"Somewhere","location":{"lat":10.0,"lon":10.0},"wat":true}"#,
        // Server-owned id supplied by client.
        r#"{"id":"00000000-0000-0000-0000-000000000001","title":"Client Id","kind":"Werkstatt","address":"Somewhere","location":{"lat":10.0,"lon":10.0}}"#,
        // Client operation id must be a UUID.
        r#"{"title":"Bad Operation","kind":"Werkstatt","address":"Somewhere","location":{"lat":10.0,"lon":10.0},"operation_id":"not-a-uuid"}"#,
        // Explicit null is not the same as omitting the optional operation id.
        r#"{"title":"Null Operation","kind":"Werkstatt","address":"Somewhere","location":{"lat":10.0,"lon":10.0},"operation_id":null}"#,
        // Too many tags.
        &format!(
            r#"{{"title":"Too Many Tags","kind":"Werkstatt","address":"Somewhere","location":{{"lat":10.0,"lon":10.0}},"tags":[{}]}}"#,
            (0..40)
                .map(|i| format!("\"t{i}\""))
                .collect::<Vec<_>>()
                .join(",")
        ),
    ];

    for body in cases {
        let res = app
            .clone()
            .oneshot(post_node_req(Some(&cookie), body))
            .await?;
        assert_eq!(
            res.status(),
            StatusCode::BAD_REQUEST,
            "expected 400 for payload: {body}"
        );
    }

    assert!(
        state.nodes.read().await.is_empty(),
        "invalid create requests must not create a node"
    );
    assert!(
        !in_dir.join("demo.nodes.jsonl").exists(),
        "invalid create requests must not write JSONL"
    );

    Ok(())
}
