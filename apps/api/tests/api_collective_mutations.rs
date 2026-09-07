use anyhow::Result;
use axum::{
    body,
    http::{Request, StatusCode},
    middleware::from_fn_with_state,
    Router,
};
use serial_test::serial;
use std::{fs, path::Path, sync::Arc};
use tokio::sync::RwLock;
use tower::ServiceExt;

mod helpers;

use helpers::set_gewebe_in_dir;
use weltgewebe_api::{
    auth::{
        accounts::AccountStore,
        rate_limit::{AuthRateLimiter, NodeMutationRateDecision, RateLimitError},
        role::Role,
        session::SessionBackend,
    },
    config::{
        AppConfig, DomainAccountWriteSource, DomainEdgeWriteSource, DomainNodeWriteSource,
        DomainReadSource, NodeMutationRateLimitConfig,
    },
    middleware::{auth::auth_middleware, csrf::require_csrf},
    node_mutation::NodeMutationOperation,
    routes::{
        accounts::{AccountInternal, AccountPublic},
        api_router,
        edges::Edge,
    },
    state::{ApiState, OrderedCache},
    telemetry::{BuildInfo, Metrics},
};

async fn state_for_role(role: Role) -> Result<ApiState> {
    state_for_role_with_mutation_limits(role, NodeMutationRateLimitConfig::default()).await
}

async fn state_for_role_with_mutation_limits(
    role: Role,
    node_mutation_rate_limits: NodeMutationRateLimitConfig,
) -> Result<ApiState> {
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
        node_mutation_rate_limits,
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

    let mut accounts = AccountStore::new();
    accounts.insert(AccountInternal {
        public: AccountPublic {
            id: ACTOR_ID.to_string(),
            kind: "garnrolle".to_string(),
            title: "Testgarnrolle".to_string(),
            summary: None,
            public_pos: None,
            map_state: weltgewebe_api::routes::accounts::GarnrolleMapState::NotOnMap,
            radius_m: 0,
            disabled: false,
            tags: vec![],
        },
        role,
        email: Some("actor@example.test".to_string()),
        webauthn_user_id: uuid::Uuid::new_v4(),
    });

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
        accounts: Arc::new(RwLock::new(accounts)),
        nodes: Arc::new(RwLock::new(
            weltgewebe_api::routes::nodes::load_nodes().await,
        )),
        nodes_persist: Arc::new(tokio::sync::Mutex::new(())),
        accounts_persist: Arc::new(tokio::sync::Mutex::new(())),
        domain_projection_gate: std::sync::Arc::new(tokio::sync::RwLock::new(())),
        domain_projection_reload: std::sync::Arc::new(tokio::sync::Mutex::new(())),
        domain_projection_version: std::sync::Arc::new(std::sync::atomic::AtomicI64::new(0)),
        edges: Arc::new(RwLock::new(OrderedCache::new())),
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

async fn authenticated_app(state: ApiState) -> (Router, String) {
    let session = state
        .sessions
        .create(ACTOR_ID.to_string(), None)
        .await
        .expect("in-memory session must be created");
    let cookie = format!("gewebe_session={}", session.id);
    let app = Router::new()
        .merge(api_router())
        .layer(from_fn_with_state(state.clone(), auth_middleware))
        .layer(axum::middleware::from_fn(require_csrf))
        .with_state(state);
    (app, cookie)
}

fn write_fixture(path: &Path, content: &str) {
    fs::create_dir_all(path.parent().expect("fixture parent")).expect("create fixture dir");
    fs::write(path, content).expect("write fixture");
}

const ACTOR_ID: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const INITIAL_NODE_ETAG: &str = "\"2026-01-01T00:00:00Z\"";

fn mutation_request_with_etag(
    method: &str,
    uri: &str,
    cookie: &str,
    payload: &str,
    etag: Option<&str>,
) -> Request<body::Body> {
    let mut request = Request::builder()
        .method(method)
        .uri(uri)
        .header("Content-Type", "application/json")
        .header("Cookie", cookie)
        .header("Host", "localhost")
        .header("Origin", "http://localhost");
    if let Some(etag) = etag {
        request = request.header("If-Match", etag);
    }
    request
        .body(body::Body::from(payload.to_string()))
        .expect("valid request")
}

fn mutation_request(method: &str, uri: &str, cookie: &str, payload: &str) -> Request<body::Body> {
    let etag = matches!(method, "PUT" | "PATCH" | "DELETE").then_some(INITIAL_NODE_ETAG);
    mutation_request_with_etag(method, uri, cookie, payload, etag)
}

#[tokio::test]
#[serial]
async fn node_replace_requires_current_if_match_version() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    let nodes_path = in_dir.join("demo.nodes.jsonl");
    write_fixture(
        &nodes_path,
        concat!(
            r#"{"id":"n1","kind":"Werkstatt","title":"Alt","address":"Alt 1","location":{"lat":53.5,"lon":10.0},"created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z"}"#,
            "\n",
        ),
    );
    let _env = set_gewebe_in_dir(&in_dir);
    let state = state_for_role(Role::Weber).await?;
    let (app, cookie) = authenticated_app(state.clone()).await;
    let payload = r#"{"title":"Neu","kind":"Werkstatt","address":"Neu 1","location":{"lat":53.55,"lon":10.05},"tags":[]}"#;

    let missing = mutation_request_with_etag("PUT", "/nodes/n1", &cookie, payload, None);
    let response = app.clone().oneshot(missing).await?;
    assert_eq!(response.status(), StatusCode::PRECONDITION_REQUIRED);
    let body = body::to_bytes(response.into_body(), usize::MAX).await?;
    let current: serde_json::Value = serde_json::from_slice(&body)?;
    assert_eq!(current["updated_at"], "2026-01-01T00:00:00Z");

    let stale = mutation_request_with_etag(
        "PUT",
        "/nodes/n1",
        &cookie,
        payload,
        Some("\"2025-12-31T23:59:59Z\""),
    );
    let response = app.clone().oneshot(stale).await?;
    assert_eq!(response.status(), StatusCode::PRECONDITION_FAILED);
    let body = body::to_bytes(response.into_body(), usize::MAX).await?;
    let current: serde_json::Value = serde_json::from_slice(&body)?;
    assert_eq!(current["title"], "Alt");
    let conflict_metrics = String::from_utf8(state.metrics.render()?)?;
    assert!(conflict_metrics.contains(r#"node_mutation_conflicts_total{operation="replace"} 2"#));
    assert!(conflict_metrics
        .contains(r#"node_mutations_total{operation="replace",outcome="conflict"} 2"#));
    assert!(!conflict_metrics.contains(ACTOR_ID));
    assert!(!conflict_metrics.contains("n1"));
    assert_eq!(
        state
            .nodes
            .read()
            .await
            .get("n1")
            .map(|node| node.title.as_str()),
        Some("Alt"),
    );

    // RFC 9110 normal request checks take precedence over preconditions: this
    // endpoint cannot replace a missing node, so it remains a 404 rather than
    // evaluating the supplied validator as a 412.
    let missing_node = mutation_request_with_etag(
        "PUT",
        "/nodes/missing",
        &cookie,
        payload,
        Some(INITIAL_NODE_ETAG),
    );
    assert_eq!(
        app.clone().oneshot(missing_node).await?.status(),
        StatusCode::NOT_FOUND,
    );

    let current = mutation_request_with_etag(
        "PUT",
        "/nodes/n1",
        &cookie,
        payload,
        Some(INITIAL_NODE_ETAG),
    );
    let response = app.oneshot(current).await?;
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(
        state
            .nodes
            .read()
            .await
            .get("n1")
            .map(|node| node.title.as_str()),
        Some("Neu"),
    );
    assert!(fs::read_to_string(nodes_path)?.contains(r#""title":"Neu""#));
    let audit = fs::read_to_string(in_dir.join(".node-mutation-audit/events.jsonl"))?;
    assert!(audit.contains(r#""operation":"replace""#));
    assert!(audit.contains(r#""state":"committed""#));
    assert!(
        !audit.contains(ACTOR_ID),
        "raw account ids must not enter mutation audit"
    );
    let metrics = String::from_utf8(state.metrics.render()?)?;
    assert!(metrics.contains(r#"node_mutations_total{operation="replace",outcome="success"} 1"#));
    assert!(!metrics.contains(ACTOR_ID));
    assert!(!metrics.contains("n1"));

    Ok(())
}

#[tokio::test]
#[serial]
async fn no_op_replace_is_still_actor_audited_without_advancing_version() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    let nodes_path = in_dir.join("demo.nodes.jsonl");
    let original = concat!(
        r#"{"id":"n1","kind":"Werkstatt","title":"Alt","address":"Alt 1","location":{"lat":53.5,"lon":10.0},"created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z"}"#,
        "
",
    );
    write_fixture(&nodes_path, original);
    let _env = set_gewebe_in_dir(&in_dir);
    let state = state_for_role(Role::Weber).await?;
    let (app, cookie) = authenticated_app(state.clone()).await;
    let request = mutation_request_with_etag(
        "PUT",
        "/nodes/n1",
        &cookie,
        r#"{"title":"Alt","kind":"Werkstatt","address":"Alt 1","location":{"lat":53.5,"lon":10.0},"tags":[]}"#,
        Some(INITIAL_NODE_ETAG),
    );

    let response = app.oneshot(request).await?;
    assert_eq!(response.status(), StatusCode::OK);
    let body = body::to_bytes(response.into_body(), usize::MAX).await?;
    let node: serde_json::Value = serde_json::from_slice(&body)?;
    assert_eq!(node["updated_at"], "2026-01-01T00:00:00Z");
    assert_eq!(fs::read_to_string(&nodes_path)?, original);

    let audit = fs::read_to_string(in_dir.join(".node-mutation-audit/events.jsonl"))?;
    let events = audit
        .lines()
        .map(serde_json::from_str::<serde_json::Value>)
        .collect::<Result<Vec<_>, _>>()?;
    assert_eq!(events.len(), 2);
    assert_eq!(events[0]["state"], "prepared");
    assert_eq!(events[1]["state"], "committed");
    assert_eq!(events[0]["operation_id"], events[1]["operation_id"]);
    assert_eq!(events[1]["before_hash"], events[1]["after_hash"]);
    assert!(!audit.contains(ACTOR_ID));

    let metrics = String::from_utf8(state.metrics.render()?)?;
    assert!(metrics.contains(r#"node_mutations_total{operation="replace",outcome="success"} 1"#));
    Ok(())
}

#[tokio::test]
#[serial]
async fn weber_can_replace_shared_node_and_delete_node_cascade() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    let nodes_path = in_dir.join("demo.nodes.jsonl");
    let edges_path = in_dir.join("demo.edges.jsonl");
    write_fixture(
        &nodes_path,
        concat!(
            r#"{"id":"n1","kind":"Werkstatt","title":"Alt","address":"Alt 1","location":{"lat":53.5,"lon":10.0},"created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z"}"#,
            "\n",
            r#"{"id":"n2","kind":"Ort","title":"Ziel","address":"Ziel 2","location":{"lat":53.6,"lon":10.1},"created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z"}"#,
            "\n",
        ),
    );
    write_fixture(
        &edges_path,
        concat!(
            r#"{"id":"e1","source_id":"n1","source_type":"node","target_id":"n2","target_type":"node","edge_kind":"reference","created_at":"2026-01-01T00:00:00Z"}"#,
            "\n",
        ),
    );
    let _env = set_gewebe_in_dir(&in_dir);
    let state = state_for_role(Role::Weber).await?;
    state.edges.write().await.insert(
        "e1".to_string(),
        Edge {
            id: "e1".to_string(),
            source_id: "n1".to_string(),
            source_type: Some("node".to_string()),
            target_id: "n2".to_string(),
            target_type: Some("node".to_string()),
            edge_kind: "reference".to_string(),
            faden_type: None,
            faden_subject_id: None,
            note: None,
            created_at: Some("2026-01-01T00:00:00Z".to_string().into()),
            expires_at: None,
        },
    );
    let (app, cookie) = authenticated_app(state.clone()).await;

    let replace = mutation_request(
        "PUT",
        "/nodes/n1",
        &cookie,
        r#"{"title":"Gemeinsam gepflegt","kind":"Werkstatt","address":"Neu 1","location":{"lat":53.55,"lon":10.05},"summary":"Gemeinsamer Knoten","info":"Pflegbar durch alle Weber","tags":["commons"]}"#,
    );
    let response = app.clone().oneshot(replace).await?;
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = body::to_bytes(response.into_body(), usize::MAX).await?;
    let node: serde_json::Value = serde_json::from_slice(&bytes)?;
    assert_eq!(node["title"], "Gemeinsam gepflegt");
    assert_eq!(node["created_at"], "2026-01-01T00:00:00Z");
    let current_etag = format!(
        "\"{}\"",
        node["updated_at"]
            .as_str()
            .expect("replace response has updated_at"),
    );

    // PUT intentionally shares the create contract: coordinates are nested
    // under `location`. Flat `lat`/`lon` fields must fail rather than silently
    // drifting away from the TypeScript client contract.
    let flat_location = mutation_request_with_etag(
        "PUT",
        "/nodes/n1",
        &cookie,
        r#"{"title":"Falscher Vertrag","kind":"Werkstatt","address":"Neu 1","lat":53.55,"lon":10.05,"tags":[]}"#,
        Some(&current_etag),
    );
    let response = app.clone().oneshot(flat_location).await?;
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    assert_eq!(
        state
            .nodes
            .read()
            .await
            .get("n1")
            .map(|node| node.title.as_str()),
        Some("Gemeinsam gepflegt")
    );

    let delete_node =
        mutation_request_with_etag("DELETE", "/nodes/n1", &cookie, "", Some(&current_etag));
    let response = app.clone().oneshot(delete_node).await?;
    assert_eq!(response.status(), StatusCode::OK);
    let receipt: serde_json::Value =
        serde_json::from_slice(&body::to_bytes(response.into_body(), usize::MAX).await?)?;
    assert_eq!(receipt["node_id"], "n1");
    assert_eq!(receipt["node_state"], "removed");
    assert_eq!(receipt["removed_edge_ids"], serde_json::json!(["e1"]));
    assert_eq!(receipt["conversation"]["effect"], "not_applicable");
    assert!(state.nodes.read().await.get("n1").is_none());
    assert!(state.edges.read().await.get("e1").is_none());
    assert!(!fs::read_to_string(&nodes_path)?.contains(r#""id":"n1""#));
    assert!(!fs::read_to_string(&edges_path)?.contains(r#""id":"e1""#));
    let audit = fs::read_to_string(in_dir.join(".node-mutation-audit/events.jsonl"))?;
    assert!(audit.contains(r#""operation":"replace""#));
    assert!(audit.contains(r#""operation":"delete""#));
    assert_eq!(audit.matches(r#""state":"committed""#).count(), 2);
    assert!(!audit.contains(ACTOR_ID));

    let response = app
        .clone()
        .oneshot(Request::get("/nodes/n1").body(body::Body::empty())?)
        .await?;
    assert_eq!(response.status(), StatusCode::NOT_FOUND);
    let response = app
        .oneshot(Request::get("/edges/e1").body(body::Body::empty())?)
        .await?;
    assert_eq!(response.status(), StatusCode::NOT_FOUND);

    Ok(())
}

#[tokio::test]
#[serial]
async fn mutation_limits_are_separate_and_admin_bypass_is_explicit() -> Result<()> {
    let limited = NodeMutationRateLimitConfig {
        replace_per_minute: 1,
        replace_per_hour: 10,
        delete_per_minute: 1,
        delete_per_hour: 10,
        admin_emergency_bypass: false,
    };
    let state = state_for_role_with_mutation_limits(Role::Admin, limited).await?;
    assert_eq!(
        state
            .rate_limiter
            .check_node_mutation(ACTOR_ID, Role::Admin, NodeMutationOperation::Replace)
            .await?,
        NodeMutationRateDecision::Enforced
    );
    assert!(matches!(
        state
            .rate_limiter
            .check_node_mutation(ACTOR_ID, Role::Admin, NodeMutationOperation::Patch)
            .await,
        Err(RateLimitError::AccountLimited)
    ));
    assert_eq!(
        state
            .rate_limiter
            .check_node_mutation(ACTOR_ID, Role::Admin, NodeMutationOperation::Delete)
            .await?,
        NodeMutationRateDecision::Enforced,
        "delete must use a bucket distinct from replace/patch"
    );

    let bypass = NodeMutationRateLimitConfig {
        replace_per_minute: 1,
        replace_per_hour: 1,
        delete_per_minute: 1,
        delete_per_hour: 1,
        admin_emergency_bypass: true,
    };
    let state = state_for_role_with_mutation_limits(Role::Admin, bypass).await?;
    for operation in [
        NodeMutationOperation::Replace,
        NodeMutationOperation::Patch,
        NodeMutationOperation::Delete,
    ] {
        for _ in 0..3 {
            assert_eq!(
                state
                    .rate_limiter
                    .check_node_mutation(ACTOR_ID, Role::Admin, operation)
                    .await?,
                NodeMutationRateDecision::AdminEmergencyBypass
            );
        }
    }
    Ok(())
}

#[tokio::test]
#[serial]
async fn permanent_node_delete_does_not_expose_public_purge() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    write_fixture(
        &in_dir.join("demo.nodes.jsonl"),
        concat!(
            r#"{"id":"n1","kind":"Werkstatt","title":"Alt","location":{"lat":53.5,"lon":10.0},"created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z"}"#,
            "\n",
        ),
    );
    let _env = set_gewebe_in_dir(&in_dir);
    let state = state_for_role(Role::Admin).await?;
    let (app, cookie) = authenticated_app(state).await;
    let request = mutation_request("POST", "/nodes/n1/purge", &cookie, "{}");
    let response = app.oneshot(request).await?;
    assert_eq!(response.status(), StatusCode::NOT_FOUND);
    Ok(())
}

#[tokio::test]
#[serial]
async fn node_delete_rejects_mixed_node_and_edge_write_sources() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    let nodes_path = in_dir.join("demo.nodes.jsonl");
    let original = concat!(
        r#"{"id":"n1","kind":"Werkstatt","title":"Altbestand","address":"Alt 1","location":{"lat":53.5,"lon":10.0},"created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z"}"#,
        "\n",
        r#"{"id":"n2","kind":"Werkstatt","title":"Fremd","address":"Fremd 2","location":{"lat":53.6,"lon":10.1},"created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z","created_by_account_id":"other"}"#,
        "\n",
    );
    write_fixture(&nodes_path, original);
    let _env = set_gewebe_in_dir(&in_dir);
    let mut state = state_for_role(Role::Weber).await?;
    state.config.domain_edge_write_source = DomainEdgeWriteSource::Postgres;
    let (app, cookie) = authenticated_app(state).await;

    let delete = mutation_request("DELETE", "/nodes/n1", &cookie, "");
    let response = app.oneshot(delete).await?;
    assert_eq!(response.status(), StatusCode::CONFLICT);
    assert_eq!(fs::read_to_string(&nodes_path)?, original);

    Ok(())
}

#[tokio::test]
#[serial]
async fn node_delete_jsonl_keeps_account_and_role_endpoint_collisions() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    let nodes_path = in_dir.join("demo.nodes.jsonl");
    let edges_path = in_dir.join("demo.edges.jsonl");
    write_fixture(
        &nodes_path,
        concat!(
            r#"{"id":"n1","kind":"Werkstatt","title":"Alt","address":"Alt 1","location":{"lat":53.5,"lon":10.0},"created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z"}"#,
            "\n",
            r#"{"id":"n2","kind":"Ort","title":"Ziel","address":"Ziel 2","location":{"lat":53.6,"lon":10.1},"created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z"}"#,
            "\n",
        ),
    );
    write_fixture(
        &edges_path,
        concat!(
            r#"{"id":"e-node","source_id":"n1","source_type":"node","target_id":"n2","target_type":"node","edge_kind":"reference","created_at":"2026-01-01T00:00:00Z"}"#,
            "\n",
            r#"{"id":"e-account-collision","source_id":"n1","source_type":"account","target_id":"n2","target_type":"node","edge_kind":"reference","created_at":"2026-01-01T00:00:00Z"}"#,
            "\n",
            r#"{"id":"e-role-collision","source_id":"n2","source_type":"node","target_id":"n1","target_type":"role","edge_kind":"reference","created_at":"2026-01-01T00:00:00Z"}"#,
            "\n",
        ),
    );
    let _env = set_gewebe_in_dir(&in_dir);
    let state = state_for_role(Role::Weber).await?;
    {
        let mut edges = state.edges.write().await;
        for (id, source_id, source_type, target_id, target_type) in [
            ("e-node", "n1", "node", "n2", "node"),
            ("e-account-collision", "n1", "account", "n2", "node"),
            ("e-role-collision", "n2", "node", "n1", "role"),
        ] {
            edges.insert(
                id.to_string(),
                Edge {
                    id: id.to_string(),
                    source_id: source_id.to_string(),
                    source_type: Some(source_type.to_string()),
                    target_id: target_id.to_string(),
                    target_type: Some(target_type.to_string()),
                    edge_kind: "reference".to_string(),
                    faden_type: None,
                    faden_subject_id: None,
                    note: None,
                    created_at: Some("2026-01-01T00:00:00Z".to_string().into()),
                    expires_at: None,
                },
            );
        }
    }
    let (app, cookie) = authenticated_app(state.clone()).await;

    let delete = mutation_request("DELETE", "/nodes/n1", &cookie, "");
    let response = app.oneshot(delete).await?;
    let status = response.status();
    let bytes = body::to_bytes(response.into_body(), usize::MAX).await?;
    assert_eq!(
        status,
        StatusCode::OK,
        "unexpected delete response: {}",
        String::from_utf8_lossy(&bytes)
    );

    assert!(state.nodes.read().await.get("n1").is_none());
    let edges = state.edges.read().await;
    assert!(edges.get("e-node").is_none());
    assert!(edges.get("e-account-collision").is_some());
    assert!(edges.get("e-role-collision").is_some());
    drop(edges);

    let edge_file = fs::read_to_string(&edges_path)?;
    assert!(!edge_file.contains(r#""id":"e-node""#));
    assert!(edge_file.contains(r#""id":"e-account-collision""#));
    assert!(edge_file.contains(r#""id":"e-role-collision""#));
    assert!(!fs::read_to_string(&nodes_path)?.contains(r#""id":"n1""#));

    Ok(())
}

#[tokio::test]
#[serial]
async fn node_delete_jsonl_deletes_unique_untyped_legacy_edge() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    let nodes_path = in_dir.join("demo.nodes.jsonl");
    let edges_path = in_dir.join("demo.edges.jsonl");
    write_fixture(
        &nodes_path,
        concat!(
            r#"{"id":"n1","kind":"Werkstatt","title":"Alt","address":"Alt 1","location":{"lat":53.5,"lon":10.0},"created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z"}"#,
            "\n",
            r#"{"id":"n2","kind":"Ort","title":"Ziel","address":"Ziel 2","location":{"lat":53.6,"lon":10.1},"created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z"}"#,
            "\n",
        ),
    );
    write_fixture(
        &edges_path,
        concat!(
            r#"{"id":"e-untyped-legacy","source_id":"n1","target_id":"n2","target_type":"node","edge_kind":"reference","created_at":"2026-01-01T00:00:00Z"}"#,
            "\n",
            r#"{"id":"e-kept","source_id":"n2","source_type":"node","target_id":"n3","target_type":"node","edge_kind":"reference","created_at":"2026-01-01T00:00:00Z"}"#,
            "\n",
        ),
    );
    let _env = set_gewebe_in_dir(&in_dir);
    let state = state_for_role(Role::Weber).await?;
    state.edges.write().await.insert(
        "e-untyped-legacy".to_string(),
        Edge {
            id: "e-untyped-legacy".to_string(),
            source_id: "n1".to_string(),
            source_type: None,
            target_id: "n2".to_string(),
            target_type: Some("node".to_string()),
            edge_kind: "reference".to_string(),
            faden_type: None,
            faden_subject_id: None,
            note: None,
            created_at: Some("2026-01-01T00:00:00Z".to_string().into()),
            expires_at: None,
        },
    );
    let (app, cookie) = authenticated_app(state.clone()).await;

    let delete = mutation_request("DELETE", "/nodes/n1", &cookie, "");
    let response = app.oneshot(delete).await?;
    let status = response.status();
    let bytes = body::to_bytes(response.into_body(), usize::MAX).await?;
    assert_eq!(
        status,
        StatusCode::OK,
        "unexpected delete response: {}",
        String::from_utf8_lossy(&bytes)
    );

    assert!(state.nodes.read().await.get("n1").is_none());
    assert!(state.edges.read().await.get("e-untyped-legacy").is_none());
    let edge_file = fs::read_to_string(&edges_path)?;
    assert!(!edge_file.contains(r#""id":"e-untyped-legacy""#));
    assert!(edge_file.contains(r#""id":"e-kept""#));
    assert!(!fs::read_to_string(&nodes_path)?.contains(r#""id":"n1""#));

    Ok(())
}

#[tokio::test]
#[serial]
async fn node_delete_jsonl_rejects_untyped_account_collision_without_partial_mutation() -> Result<()>
{
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    let nodes_path = in_dir.join("demo.nodes.jsonl");
    let edges_path = in_dir.join("demo.edges.jsonl");
    let nodes_original = concat!(
        r#"{"id":"n1","kind":"Werkstatt","title":"Alt","address":"Alt 1","location":{"lat":53.5,"lon":10.0},"created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z"}"#,
        "\n",
    );
    let edges_original = concat!(
        r#"{"id":"e-untyped-account-collision","source_id":"n1","target_id":"n2","target_type":"node","edge_kind":"reference","created_at":"2026-01-01T00:00:00Z"}"#,
        "\n",
    );
    write_fixture(&nodes_path, nodes_original);
    write_fixture(&edges_path, edges_original);
    let _env = set_gewebe_in_dir(&in_dir);
    let state = state_for_role(Role::Weber).await?;
    state.accounts.write().await.insert(AccountInternal {
        public: AccountPublic {
            id: "n1".to_string(),
            kind: "garnrolle".to_string(),
            title: "Kollidierende Garnrolle".to_string(),
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
    state.edges.write().await.insert(
        "e-untyped-account-collision".to_string(),
        Edge {
            id: "e-untyped-account-collision".to_string(),
            source_id: "n1".to_string(),
            source_type: None,
            target_id: "n2".to_string(),
            target_type: Some("node".to_string()),
            edge_kind: "reference".to_string(),
            faden_type: None,
            faden_subject_id: None,
            note: None,
            created_at: Some("2026-01-01T00:00:00Z".to_string().into()),
            expires_at: None,
        },
    );
    let (app, cookie) = authenticated_app(state.clone()).await;

    let delete = mutation_request("DELETE", "/nodes/n1", &cookie, "");
    let response = app.oneshot(delete).await?;
    assert_eq!(response.status(), StatusCode::CONFLICT);

    assert!(state.nodes.read().await.get("n1").is_some());
    assert!(state
        .edges
        .read()
        .await
        .get("e-untyped-account-collision")
        .is_some());
    assert_eq!(fs::read_to_string(&nodes_path)?, nodes_original);
    assert_eq!(fs::read_to_string(&edges_path)?, edges_original);

    Ok(())
}

#[tokio::test]
#[serial]
async fn node_delete_jsonl_rejects_untyped_role_collision_without_partial_mutation() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    let nodes_path = in_dir.join("demo.nodes.jsonl");
    let edges_path = in_dir.join("demo.edges.jsonl");
    let nodes_original = concat!(
        r#"{"id":"n1","kind":"Werkstatt","title":"Alt","address":"Alt 1","location":{"lat":53.5,"lon":10.0},"created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z"}"#,
        "\n",
        r#"{"id":"n2","kind":"Ort","title":"Ziel","address":"Ziel 2","location":{"lat":53.6,"lon":10.1},"created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z"}"#,
        "\n",
    );
    let edges_original = concat!(
        r#"{"id":"e-role-evidence","source_id":"n2","source_type":"node","target_id":"n1","target_type":"role","edge_kind":"reference","created_at":"2026-01-01T00:00:00Z"}"#,
        "\n",
        r#"{"id":"e-untyped-role-collision","source_id":"n1","target_id":"n2","target_type":"node","edge_kind":"reference","created_at":"2026-01-01T00:00:00Z"}"#,
        "\n",
    );
    write_fixture(&nodes_path, nodes_original);
    write_fixture(&edges_path, edges_original);
    let _env = set_gewebe_in_dir(&in_dir);
    let state = state_for_role(Role::Weber).await?;
    {
        let mut edges = state.edges.write().await;
        edges.insert(
            "e-role-evidence".to_string(),
            Edge {
                id: "e-role-evidence".to_string(),
                source_id: "n2".to_string(),
                source_type: Some("node".to_string()),
                target_id: "n1".to_string(),
                target_type: Some("role".to_string()),
                edge_kind: "reference".to_string(),
                faden_type: None,
                faden_subject_id: None,
                note: None,
                created_at: Some("2026-01-01T00:00:00Z".to_string().into()),
                expires_at: None,
            },
        );
        edges.insert(
            "e-untyped-role-collision".to_string(),
            Edge {
                id: "e-untyped-role-collision".to_string(),
                source_id: "n1".to_string(),
                source_type: None,
                target_id: "n2".to_string(),
                target_type: Some("node".to_string()),
                edge_kind: "reference".to_string(),
                faden_type: None,
                faden_subject_id: None,
                note: None,
                created_at: Some("2026-01-01T00:00:00Z".to_string().into()),
                expires_at: None,
            },
        );
    }
    let (app, cookie) = authenticated_app(state.clone()).await;

    let delete = mutation_request("DELETE", "/nodes/n1", &cookie, "");
    let response = app.oneshot(delete).await?;
    assert_eq!(response.status(), StatusCode::CONFLICT);

    assert!(state.nodes.read().await.get("n1").is_some());
    assert!(state.edges.read().await.get("e-role-evidence").is_some());
    assert!(state
        .edges
        .read()
        .await
        .get("e-untyped-role-collision")
        .is_some());
    assert_eq!(fs::read_to_string(&nodes_path)?, nodes_original);
    assert_eq!(fs::read_to_string(&edges_path)?, edges_original);

    Ok(())
}

#[tokio::test]
#[serial]
async fn node_delete_jsonl_rejects_invalid_endpoint_type_without_partial_mutation() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    let nodes_path = in_dir.join("demo.nodes.jsonl");
    let edges_path = in_dir.join("demo.edges.jsonl");
    let nodes_original = concat!(
        r#"{"id":"n1","kind":"Werkstatt","title":"Alt","address":"Alt 1","location":{"lat":53.5,"lon":10.0},"created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z"}"#,
        "\n",
        r#"{"id":"n2","kind":"Ort","title":"Ziel","address":"Ziel 2","location":{"lat":53.6,"lon":10.1},"created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z"}"#,
        "\n",
    );
    let edges_original = concat!(
        r#"{"id":"e-node","source_id":"n1","source_type":"node","target_id":"n2","target_type":"node","edge_kind":"reference","created_at":"2026-01-01T00:00:00Z"}"#,
        "\n",
        r#"{"id":"e-invalid-collision","source_id":"n2","source_type":"node","target_id":"n1","target_type":"group","edge_kind":"reference","created_at":"2026-01-01T00:00:00Z"}"#,
        "\n",
    );
    write_fixture(&nodes_path, nodes_original);
    write_fixture(&edges_path, edges_original);
    let _env = set_gewebe_in_dir(&in_dir);
    let state = state_for_role(Role::Weber).await?;
    state.edges.write().await.insert(
        "e-node".to_string(),
        Edge {
            id: "e-node".to_string(),
            source_id: "n1".to_string(),
            source_type: Some("node".to_string()),
            target_id: "n2".to_string(),
            target_type: Some("node".to_string()),
            edge_kind: "reference".to_string(),
            faden_type: None,
            faden_subject_id: None,
            note: None,
            created_at: Some("2026-01-01T00:00:00Z".to_string().into()),
            expires_at: None,
        },
    );
    let (app, cookie) = authenticated_app(state.clone()).await;

    let delete = mutation_request("DELETE", "/nodes/n1", &cookie, "");
    let response = app.oneshot(delete).await?;
    assert_eq!(response.status(), StatusCode::CONFLICT);

    assert!(state.nodes.read().await.get("n1").is_some());
    assert!(state.edges.read().await.get("e-node").is_some());
    assert_eq!(fs::read_to_string(&nodes_path)?, nodes_original);
    assert_eq!(fs::read_to_string(&edges_path)?, edges_original);

    Ok(())
}

#[tokio::test]
#[serial]
async fn concurrent_replace_and_delete_leave_one_coherent_jsonl_result() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    let nodes_path = in_dir.join("demo.nodes.jsonl");
    let edges_path = in_dir.join("demo.edges.jsonl");
    write_fixture(
        &nodes_path,
        concat!(
            r#"{"id":"n1","kind":"Werkstatt","title":"Alt","address":"Alt 1","location":{"lat":53.5,"lon":10.0},"created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z"}"#,
            "\n",
            r#"{"id":"n2","kind":"Ort","title":"Ziel","address":"Ziel 2","location":{"lat":53.6,"lon":10.1},"created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z"}"#,
            "\n",
        ),
    );
    write_fixture(
        &edges_path,
        concat!(
            r#"{"id":"e1","source_id":"n1","source_type":"node","target_id":"n2","target_type":"node","edge_kind":"reference","created_at":"2026-01-01T00:00:00Z"}"#,
            "\n",
        ),
    );
    let _env = set_gewebe_in_dir(&in_dir);
    let state = state_for_role(Role::Weber).await?;
    state.edges.write().await.insert(
        "e1".to_string(),
        Edge {
            id: "e1".to_string(),
            source_id: "n1".to_string(),
            source_type: Some("node".to_string()),
            target_id: "n2".to_string(),
            target_type: Some("node".to_string()),
            edge_kind: "reference".to_string(),
            faden_type: None,
            faden_subject_id: None,
            note: None,
            created_at: Some("2026-01-01T00:00:00Z".to_string().into()),
            expires_at: None,
        },
    );
    let (app, cookie) = authenticated_app(state.clone()).await;

    let replace = mutation_request(
        "PUT",
        "/nodes/n1",
        &cookie,
        r#"{"title":"Parallel gepflegt","kind":"Werkstatt","address":"Neu 1","location":{"lat":53.55,"lon":10.05},"tags":[]}"#,
    );
    let delete = mutation_request("DELETE", "/nodes/n1", &cookie, "");
    let (replace_response, delete_response) =
        tokio::join!(app.clone().oneshot(replace), app.clone().oneshot(delete),);
    let replace_status = replace_response?.status();
    let delete_status = delete_response?.status();

    match (replace_status, delete_status) {
        (StatusCode::OK, StatusCode::PRECONDITION_FAILED) => {
            assert_eq!(
                state
                    .nodes
                    .read()
                    .await
                    .get("n1")
                    .map(|node| node.title.as_str()),
                Some("Parallel gepflegt"),
            );
            assert!(state.edges.read().await.get("e1").is_some());
            assert!(fs::read_to_string(&nodes_path)?.contains("Parallel gepflegt"));
            assert!(fs::read_to_string(&edges_path)?.contains(r#""id":"e1""#));
        }
        (StatusCode::NOT_FOUND, StatusCode::OK) => {
            assert!(state.nodes.read().await.get("n1").is_none());
            assert!(state.edges.read().await.get("e1").is_none());
            assert!(!fs::read_to_string(&nodes_path)?.contains(r#""id":"n1""#));
            assert!(!fs::read_to_string(&edges_path)?.contains(r#""id":"e1""#));
        }
        other => panic!("unexpected concurrent mutation result: {other:?}"),
    }

    Ok(())
}

#[tokio::test]
#[serial]
async fn guest_can_create_and_mutate_own_node_with_server_owned_creator() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    write_fixture(&in_dir.join("demo.edges.jsonl"), "");
    let _env = set_gewebe_in_dir(&in_dir);
    let state = state_for_role(Role::Gast).await?;
    let (app, cookie) = authenticated_app(state.clone()).await;

    let create = mutation_request(
        "POST",
        "/nodes",
        &cookie,
        r#"{"title":"Gastknoten","kind":"Werkstatt","address":"Gastweg 1","location":{"lat":53.5,"lon":10.0},"tags":[],"operation_id":"aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"}"#,
    );
    let response = app.clone().oneshot(create).await?;
    let status = response.status();
    let bytes = body::to_bytes(response.into_body(), usize::MAX).await?;
    assert_eq!(
        status,
        StatusCode::CREATED,
        "unexpected create response: {}",
        String::from_utf8_lossy(&bytes)
    );
    let created: serde_json::Value = serde_json::from_slice(&bytes)?;
    assert_eq!(created["created_by_account_id"], ACTOR_ID);
    let node_id = created["id"].as_str().expect("created id");
    let etag = format!(
        "\"{}\"",
        created["updated_at"].as_str().expect("created updated_at")
    );

    let replace = mutation_request_with_etag(
        "PUT",
        &format!("/nodes/{node_id}"),
        &cookie,
        r#"{"title":"Eigener Gastknoten","kind":"Werkstatt","address":"Gastweg 2","location":{"lat":53.51,"lon":10.01},"tags":["eigen"]}"#,
        Some(&etag),
    );
    let response = app.oneshot(replace).await?;
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = body::to_bytes(response.into_body(), usize::MAX).await?;
    let updated: serde_json::Value = serde_json::from_slice(&bytes)?;
    assert_eq!(updated["title"], "Eigener Gastknoten");
    assert_eq!(updated["created_by_account_id"], ACTOR_ID);

    let persisted = fs::read_to_string(in_dir.join("demo.nodes.jsonl"))?;
    assert!(persisted.contains(&format!(r#""created_by_account_id":"{ACTOR_ID}""#)));
    Ok(())
}

#[tokio::test]
#[serial]
async fn guest_can_update_and_anchor_own_garnrolle() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    write_fixture(
        &in_dir.join("demo.accounts.jsonl"),
        concat!(
            r#"{"id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa","type":"garnrolle","title":"Gast","role":"gast","map_state":"not_on_map","radius_m":0}"#,
            "\n",
        ),
    );
    let _env = set_gewebe_in_dir(&in_dir);
    let state = state_for_role(Role::Gast).await?;
    let (app, cookie) = authenticated_app(state).await;
    let request = mutation_request(
        "PATCH",
        "/accounts/me/profile",
        &cookie,
        r#"{"title":"Verankerte Gastgarnrolle","summary":"Ich webe mit.","tags":["gast"],"address":"Gastweg 3","map_state":"exact","radius_m":0,"location":{"lat":53.52,"lon":10.02}}"#,
    );
    let response = app.oneshot(request).await?;
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = body::to_bytes(response.into_body(), usize::MAX).await?;
    let profile: serde_json::Value = serde_json::from_slice(&bytes)?;
    assert_eq!(profile["title"], "Verankerte Gastgarnrolle");
    assert_eq!(profile["map_state"], "exact");
    assert_eq!(profile["location"]["lat"], 53.52);
    Ok(())
}

#[tokio::test]
#[serial]
async fn guest_cannot_mutate_legacy_or_foreign_nodes() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    let nodes_path = in_dir.join("demo.nodes.jsonl");
    let original = concat!(
        r#"{"id":"n1","kind":"Werkstatt","title":"Altbestand","address":"Alt 1","location":{"lat":53.5,"lon":10.0},"created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z"}"#,
        "\n",
        r#"{"id":"n2","kind":"Werkstatt","title":"Fremd","address":"Fremd 2","location":{"lat":53.6,"lon":10.1},"created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z","created_by_account_id":"other"}"#,
        "\n",
    );
    write_fixture(&nodes_path, original);
    let _env = set_gewebe_in_dir(&in_dir);
    let state = state_for_role(Role::Gast).await?;
    let (app, cookie) = authenticated_app(state).await;

    for node_id in ["n1", "n2"] {
        let replace_without_precondition = mutation_request_with_etag(
            "PUT",
            &format!("/nodes/{node_id}"),
            &cookie,
            r#"{"title":"Verboten","kind":"Werkstatt","address":"Neu 1","location":{"lat":53.55,"lon":10.05}}"#,
            None,
        );
        assert_eq!(
            app.clone()
                .oneshot(replace_without_precondition)
                .await?
                .status(),
            StatusCode::FORBIDDEN,
        );

        let replace = mutation_request(
            "PUT",
            &format!("/nodes/{node_id}"),
            &cookie,
            r#"{"title":"Verboten","kind":"Werkstatt","address":"Neu 1","location":{"lat":53.55,"lon":10.05}}"#,
        );
        assert_eq!(
            app.clone().oneshot(replace).await?.status(),
            StatusCode::FORBIDDEN
        );
        let delete = mutation_request("DELETE", &format!("/nodes/{node_id}"), &cookie, "");
        assert_eq!(
            app.clone().oneshot(delete).await?.status(),
            StatusCode::FORBIDDEN
        );
    }
    assert_eq!(fs::read_to_string(&nodes_path)?, original);

    Ok(())
}
