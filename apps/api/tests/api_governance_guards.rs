//! Governance-Schutzinvarianten ohne Datenbank.
//!
//! Beweist die drei Verteidigungslinien des Antragssystems, die auch ohne
//! PostgreSQL gelten müssen:
//!
//! 1. **Fail closed:** Ohne konfigurierten Pool antworten alle
//!    Governance-Endpunkte mit 503 — es gibt keinen JSONL- oder
//!    In-Memory-Fallback für Anträge.
//! 2. **Gast-Governance-Grenze:** Gesprächsraum-Beiträge sind für Gäste erlaubt;
//!    formale Vetos und Stimmen werden für Gäste mit 403 abgewiesen — noch vor
//!    jedem Datenbankzugriff. Unangemeldete erhalten 401.
//! 3. **Keine direkte Fadenmutation:** Fäden besitzen keinen öffentlichen
//!    POST-, PATCH-, PUT- oder DELETE-Pfad. Sie entstehen ausschließlich als
//!    serverseitige Projektion einer Webungsaktion.
//!
//! Diese Tests laufen ohne DATABASE_URL und sind nicht `#[ignore]`d.

use std::sync::Arc;

use axum::{
    body,
    http::{Request, StatusCode},
    middleware::from_fn_with_state,
    Router,
};
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
        accounts::{AccountInternal, AccountPublic, GarnrolleMapState},
        api_router,
    },
    state::ApiState,
    telemetry::{BuildInfo, Metrics},
};

const GAST_ID: &str = "cccccccc-cccc-4ccc-8ccc-000000000001";
const WEBER_ID: &str = "cccccccc-cccc-4ccc-8ccc-000000000002";
const PROPOSAL_ID: &str = "cccccccc-cccc-4ccc-8ccc-000000000009";

fn test_metrics() -> Metrics {
    Metrics::try_new(BuildInfo {
        version: "test",
        commit: "test",
        build_timestamp: "test",
    })
    .expect("metrics")
}

fn account(id: &str, role: Role) -> AccountInternal {
    AccountInternal {
        public: AccountPublic {
            id: id.to_string(),
            kind: "garnrolle".to_string(),
            title: format!("Konto {id}"),
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

fn test_config() -> AppConfig {
    AppConfig {
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
    }
}

/// App ohne PostgreSQL-Pool mit einem Gast- und einem Weber-Account.
/// Liefert `(app, gast_cookie, weber_cookie)`.
async fn app_without_database() -> (Router, String, String) {
    let mut accounts = AccountStore::new();
    accounts.insert(account(GAST_ID, Role::Gast));
    accounts.insert(account(WEBER_ID, Role::Weber));

    let config = test_config();
    let rate_limiter = Arc::new(AuthRateLimiter::new(&config));

    let state = ApiState {
        db_pool: None,
        db_pool_configured: false,
        nats_client: None,
        nats_configured: false,
        config,
        metrics: test_metrics(),
        sessions: SessionBackend::new_in_memory(),
        challenges: Default::default(),
        tokens: weltgewebe_api::auth::tokens::TokenStore::new(),
        step_up_tokens: weltgewebe_api::auth::step_up_tokens::StepUpTokenStore::new(),
        accounts: Arc::new(tokio::sync::RwLock::new(accounts)),
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
    };

    let gast_session = state
        .sessions
        .create(GAST_ID.to_string(), None)
        .await
        .expect("gast session");
    let weber_session = state
        .sessions
        .create(WEBER_ID.to_string(), None)
        .await
        .expect("weber session");

    let app = Router::new()
        .merge(api_router())
        .layer(from_fn_with_state(state.clone(), auth_middleware))
        .layer(axum::middleware::from_fn(require_csrf))
        .with_state(state);

    (
        app,
        format!("gewebe_session={}", gast_session.id),
        format!("gewebe_session={}", weber_session.id),
    )
}

fn request(
    method: &str,
    path: &str,
    cookie: Option<&str>,
    json_body: Option<&str>,
) -> Request<body::Body> {
    let mut builder = Request::builder()
        .method(method)
        .uri(path)
        .header("Host", "localhost")
        .header("Origin", "http://localhost");
    if let Some(cookie) = cookie {
        builder = builder.header("Cookie", cookie);
    }
    if let Some(json_body) = json_body {
        builder = builder.header("Content-Type", "application/json");
        return builder
            .body(body::Body::from(json_body.to_string()))
            .expect("request");
    }
    builder.body(body::Body::empty()).expect("request")
}

// ---------------------------------------------------------------------------
// 1. Fail closed ohne PostgreSQL
// ---------------------------------------------------------------------------

#[tokio::test]
async fn governance_reads_fail_closed_without_database() {
    let (app, _, _) = app_without_database().await;

    for path in [
        "/proposals",
        &format!("/proposals/{PROPOSAL_ID}"),
        &format!("/proposals/{PROPOSAL_ID}/messages"),
        "/nodes/node-a/conversation",
        &format!("/conversations/{PROPOSAL_ID}"),
        &format!("/conversations/{PROPOSAL_ID}/messages"),
    ] {
        let response = app
            .clone()
            .oneshot(request("GET", path, None, None))
            .await
            .expect("response");
        assert_eq!(
            response.status(),
            StatusCode::SERVICE_UNAVAILABLE,
            "GET {path} must fail closed without PostgreSQL"
        );
    }
}

#[tokio::test]
async fn governance_writes_fail_closed_without_database() {
    let (app, gast_cookie, weber_cookie) = app_without_database().await;

    // Der eigene Weberantrag und der eigene Austritt sind für Gäste erlaubt —
    // aber niemals ohne kanonische Datenbank.
    let create = app
        .clone()
        .oneshot(request(
            "POST",
            "/proposals",
            Some(&gast_cookie),
            Some(r#"{"kind":"weberantrag"}"#),
        ))
        .await
        .expect("response");
    assert_eq!(create.status(), StatusCode::SERVICE_UNAVAILABLE);

    let create_sach = app
        .clone()
        .oneshot(request(
            "POST",
            "/proposals",
            Some(&weber_cookie),
            Some(r#"{"kind":"sachantrag","title":"Treffzeit beschließen"}"#),
        ))
        .await
        .expect("response");
    assert_eq!(create_sach.status(), StatusCode::SERVICE_UNAVAILABLE);

    let withdraw = app
        .clone()
        .oneshot(request(
            "POST",
            &format!("/proposals/{PROPOSAL_ID}/withdraw"),
            Some(&gast_cookie),
            None,
        ))
        .await
        .expect("response");
    assert_eq!(withdraw.status(), StatusCode::SERVICE_UNAVAILABLE);

    let repeal = app
        .clone()
        .oneshot(request(
            "POST",
            &format!("/proposals/{PROPOSAL_ID}/repeal"),
            Some(&weber_cookie),
            Some(r#"{}"#),
        ))
        .await
        .expect("response");
    assert_eq!(repeal.status(), StatusCode::SERVICE_UNAVAILABLE);

    let exit = app
        .clone()
        .oneshot(request(
            "POST",
            "/accounts/me/exit",
            Some(&gast_cookie),
            None,
        ))
        .await
        .expect("response");
    assert_eq!(exit.status(), StatusCode::SERVICE_UNAVAILABLE);

    // Auch Weber-Webungsaktionen sind ohne Pool fail-closed.
    let veto = app
        .clone()
        .oneshot(request(
            "POST",
            &format!("/proposals/{PROPOSAL_ID}/veto"),
            Some(&weber_cookie),
            Some(r#"{"reason":"Begründung"}"#),
        ))
        .await
        .expect("response");
    assert_eq!(veto.status(), StatusCode::SERVICE_UNAVAILABLE);

    let vote = app
        .clone()
        .oneshot(request(
            "PUT",
            &format!("/proposals/{PROPOSAL_ID}/vote"),
            Some(&weber_cookie),
            Some(r#"{"choice":"ja"}"#),
        ))
        .await
        .expect("response");
    assert_eq!(vote.status(), StatusCode::SERVICE_UNAVAILABLE);
}

// ---------------------------------------------------------------------------
// 2. Gast-Webungsrechte und formale Selbstentscheidungsgrenze
// ---------------------------------------------------------------------------

#[tokio::test]
async fn guest_discussion_passes_but_formal_decisions_are_forbidden() {
    let (app, gast_cookie, _) = app_without_database().await;

    let veto = app
        .clone()
        .oneshot(request(
            "POST",
            &format!("/proposals/{PROPOSAL_ID}/veto"),
            Some(&gast_cookie),
            Some(r#"{"reason":"Begründung"}"#),
        ))
        .await
        .expect("response");
    assert_eq!(
        veto.status(),
        StatusCode::FORBIDDEN,
        "guest veto must be rejected before touching the database"
    );

    let vote = app
        .clone()
        .oneshot(request(
            "PUT",
            &format!("/proposals/{PROPOSAL_ID}/vote"),
            Some(&gast_cookie),
            Some(r#"{"choice":"ja"}"#),
        ))
        .await
        .expect("response");
    assert_eq!(
        vote.status(),
        StatusCode::FORBIDDEN,
        "guest vote must be rejected before touching the database"
    );

    let repeal = app
        .clone()
        .oneshot(request(
            "POST",
            &format!("/proposals/{PROPOSAL_ID}/repeal"),
            Some(&gast_cookie),
            Some(r#"{}"#),
        ))
        .await
        .expect("response");
    assert_eq!(
        repeal.status(),
        StatusCode::FORBIDDEN,
        "guest repeal must be rejected before touching the database"
    );

    let message = app
        .clone()
        .oneshot(request(
            "POST",
            &format!("/proposals/{PROPOSAL_ID}/messages"),
            Some(&gast_cookie),
            Some(r#"{"body":"Hallo"}"#),
        ))
        .await
        .expect("response");
    assert_eq!(
        message.status(),
        StatusCode::SERVICE_UNAVAILABLE,
        "guest discussion must pass authorization and fail closed only at the missing database"
    );

    let node_message = app
        .clone()
        .oneshot(request(
            "POST",
            &format!("/conversations/{PROPOSAL_ID}/messages"),
            Some(&gast_cookie),
            Some(r#"{"content":"Hallo"}"#),
        ))
        .await
        .expect("response");
    assert_eq!(
        node_message.status(),
        StatusCode::SERVICE_UNAVAILABLE,
        "guest node discussion must pass authorization and fail closed only at the missing database"
    );
}

#[tokio::test]
async fn anonymous_webungsaktionen_are_unauthorized() {
    let (app, _, _) = app_without_database().await;

    for (method, path, body_json) in [
        ("POST", format!("/proposals/{PROPOSAL_ID}/withdraw"), "{}"),
        ("POST", format!("/proposals/{PROPOSAL_ID}/repeal"), "{}"),
        (
            "POST",
            format!("/proposals/{PROPOSAL_ID}/veto"),
            r#"{"reason":"Begründung"}"#,
        ),
        (
            "PUT",
            format!("/proposals/{PROPOSAL_ID}/vote"),
            r#"{"choice":"ja"}"#,
        ),
        (
            "POST",
            format!("/proposals/{PROPOSAL_ID}/messages"),
            r#"{"body":"Hallo"}"#,
        ),
        (
            "POST",
            format!("/conversations/{PROPOSAL_ID}/messages"),
            r#"{"content":"Hallo"}"#,
        ),
        (
            "PATCH",
            format!("/conversations/{PROPOSAL_ID}/messages/{PROPOSAL_ID}"),
            r#"{"content":"Hallo"}"#,
        ),
        (
            "DELETE",
            format!("/conversations/{PROPOSAL_ID}/messages/{PROPOSAL_ID}"),
            "{}",
        ),
    ] {
        let response = app
            .clone()
            .oneshot(request(method, &path, None, Some(body_json)))
            .await
            .expect("response");
        assert_eq!(
            response.status(),
            StatusCode::UNAUTHORIZED,
            "{method} {path} without a session must be 401"
        );
    }
}

#[tokio::test]
async fn weber_cannot_use_guest_exit_path() {
    let (app, _, weber_cookie) = app_without_database().await;
    let response = app
        .oneshot(request(
            "POST",
            "/accounts/me/exit",
            Some(&weber_cookie),
            Some("{}"),
        ))
        .await
        .expect("response");
    assert_eq!(response.status(), StatusCode::CONFLICT);
}

// ---------------------------------------------------------------------------
// 3. Keine direkte Fadenmutation
// ---------------------------------------------------------------------------

#[tokio::test]
async fn edges_have_no_patch_or_delete_surface() {
    let (app, _, weber_cookie) = app_without_database().await;

    for method in ["PATCH", "DELETE", "PUT"] {
        let response = app
            .clone()
            .oneshot(request(
                method,
                &format!("/edges/{PROPOSAL_ID}"),
                Some(&weber_cookie),
                Some("{}"),
            ))
            .await
            .expect("response");
        assert_eq!(
            response.status(),
            StatusCode::METHOD_NOT_ALLOWED,
            "{method} /edges/{{id}} must not exist — Fäden sind abgeleitete \
             Visualisierungen ohne direkten Mutationspfad"
        );
    }
}

#[tokio::test]
async fn edges_have_no_direct_create_surface() {
    let (app, gast_cookie, weber_cookie) = app_without_database().await;

    let edge_body = r#"{"source_id":"cccccccc-cccc-4ccc-8ccc-000000000001","source_type":"account","target_id":"cccccccc-cccc-4ccc-8ccc-000000000005","target_type":"node","edge_kind":"reference"}"#;

    for cookie in [
        None,
        Some(gast_cookie.as_str()),
        Some(weber_cookie.as_str()),
    ] {
        let response = app
            .clone()
            .oneshot(request("POST", "/edges", cookie, Some(edge_body)))
            .await
            .expect("response");
        assert_eq!(
            response.status(),
            StatusCode::METHOD_NOT_ALLOWED,
            "no account may create Fäden directly"
        );
    }
}
