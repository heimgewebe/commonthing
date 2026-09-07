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
use std::sync::Arc;
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
        accounts::{AccountInternal, AccountPublic, GarnrolleMapState},
        api_router,
    },
    state::ApiState,
    telemetry::{BuildInfo, Metrics},
};

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
            weltgewebe_api::state::OrderedCache::new(),
        )),
        nodes_persist: Arc::new(tokio::sync::Mutex::new(())),
        accounts_persist: Arc::new(tokio::sync::Mutex::new(())),
        domain_projection_gate: std::sync::Arc::new(tokio::sync::RwLock::new(())),
        domain_projection_reload: std::sync::Arc::new(tokio::sync::Mutex::new(())),
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

fn operator(id: &str, role: Role) -> AccountInternal {
    AccountInternal {
        public: AccountPublic {
            id: id.to_string(),
            kind: "garnrolle".to_string(),
            title: format!("Operator {id}"),
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

/// Build a router (auth + csrf wired like prod) with a single operator account
/// and an active session for it. Returns (app, session_cookie, state).
async fn app_with_operator(
    in_dir: &std::path::Path,
    operator_id: &str,
    role: Role,
) -> Result<(Router, String, ApiState)> {
    app_with_operator_read_source(in_dir, operator_id, role, DomainReadSource::Jsonl).await
}

async fn app_with_operator_read_source(
    in_dir: &std::path::Path,
    operator_id: &str,
    role: Role,
    domain_read_source: DomainReadSource,
) -> Result<(Router, String, ApiState)> {
    app_with_operator_sources(
        in_dir,
        operator_id,
        role,
        domain_read_source,
        DomainAccountWriteSource::Jsonl,
    )
    .await
}

async fn app_with_operator_sources(
    in_dir: &std::path::Path,
    operator_id: &str,
    role: Role,
    domain_read_source: DomainReadSource,
    domain_account_write_source: DomainAccountWriteSource,
) -> Result<(Router, String, ApiState)> {
    let mut accounts = AccountStore::new();
    accounts.insert(operator(operator_id, role));

    let mut state = test_state().await?;
    state.config.domain_read_source = domain_read_source;
    state.config.domain_account_write_source = domain_account_write_source;
    state.accounts = Arc::new(RwLock::new(accounts));

    let session = state
        .sessions
        .create(operator_id.to_string(), None)
        .await
        .expect("session create");
    let cookie = format!("gewebe_session={}", session.id);

    let _ = in_dir; // GEWEBE_IN_DIR is set by the caller via EnvGuard
    let app = Router::new()
        .merge(api_router())
        .layer(from_fn_with_state(state.clone(), auth_middleware))
        .layer(axum::middleware::from_fn(require_csrf))
        .with_state(state.clone());
    Ok((app, cookie, state))
}

fn post_accounts(cookie: Option<&str>, json_body: &str) -> Request<body::Body> {
    let mut builder = Request::post("/accounts")
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
async fn admin_creates_account_persists_and_lists() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, _state) = app_with_operator(&in_dir, "admin1", Role::Admin).await?;

    // POST a new account with an exact public position (radius_m default 0).
    let res = app
        .clone()
        .oneshot(post_accounts(
            Some(&cookie),
            r#"{"title":"Alice","location":{"lat":53.5503,"lon":9.9932},"tags":["real"]}"#,
        ))
        .await?;
    assert_eq!(res.status(), StatusCode::CREATED);

    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let created: serde_json::Value = serde_json::from_slice(&bytes)?;
    let new_id = created["id"].as_str().context("id present")?.to_string();
    assert_eq!(created["title"], "Alice");
    // radius_m=0 => public_pos equals the provided location.
    assert_eq!(created["public_pos"]["lat"], 53.5503);
    assert_eq!(created["public_pos"]["lon"], 9.9932);
    // location must never be exposed publicly.
    assert!(created.get("location").is_none());

    // GET /accounts contains the created account.
    let res = app
        .clone()
        .oneshot(Request::get("/accounts").body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::OK);
    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let list: serde_json::Value = serde_json::from_slice(&bytes)?;
    let found = list
        .as_array()
        .context("array")?
        .iter()
        .any(|a| a["id"] == serde_json::Value::String(new_id.clone()));
    assert!(found, "created account must appear in GET /accounts");

    // Durability: the JSONL file on disk contains the new account id.
    let file = in_dir.join("demo.accounts.jsonl");
    let contents = std::fs::read_to_string(&file)?;
    assert!(
        contents.contains(&new_id),
        "created account must be persisted to JSONL"
    );

    Ok(())
}

#[tokio::test]
#[serial]
async fn weber_cannot_create_account() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, _state) = app_with_operator(&in_dir, "weber1", Role::Weber).await?;

    let res = app
        .oneshot(post_accounts(
            Some(&cookie),
            r#"{"title":"X","location":{"lat":1.0,"lon":2.0}}"#,
        ))
        .await?;
    assert_eq!(res.status(), StatusCode::FORBIDDEN);
    Ok(())
}

#[tokio::test]
#[serial]
async fn unauthenticated_cannot_create_account() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, _cookie, _state) = app_with_operator(&in_dir, "admin1", Role::Admin).await?;

    // No session cookie -> 401 (require_admin), CSRF is skipped without a cookie.
    let res = app
        .oneshot(post_accounts(
            None,
            r#"{"title":"X","location":{"lat":1.0,"lon":2.0}}"#,
        ))
        .await?;
    assert_eq!(res.status(), StatusCode::UNAUTHORIZED);
    Ok(())
}

#[tokio::test]
#[serial]
async fn invalid_input_returns_400() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, _state) = app_with_operator(&in_dir, "admin1", Role::Admin).await?;

    // Out-of-range latitude.
    let res = app
        .clone()
        .oneshot(post_accounts(
            Some(&cookie),
            r#"{"title":"X","location":{"lat":91.0,"lon":2.0}}"#,
        ))
        .await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);

    // Out-of-range longitude.
    let res = app
        .clone()
        .oneshot(post_accounts(
            Some(&cookie),
            r#"{"title":"X","location":{"lat":1.0,"lon":181.0}}"#,
        ))
        .await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);

    // Radius below the supported privacy range.
    let res = app
        .clone()
        .oneshot(post_accounts(
            Some(&cookie),
            r#"{"title":"X","location":{"lat":1.0,"lon":2.0},"radius_m":49}"#,
        ))
        .await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);

    // Radius above the supported privacy range.
    let res = app
        .clone()
        .oneshot(post_accounts(
            Some(&cookie),
            r#"{"title":"X","location":{"lat":1.0,"lon":2.0},"radius_m":5001}"#,
        ))
        .await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);

    // Boundary values remain valid.
    for radius_m in [50_u32, 5_000_u32] {
        let id = uuid::Uuid::new_v4();
        let response = app
            .clone()
            .oneshot(post_accounts(
                Some(&cookie),
                &format!(
                    r#"{{"id":"{id}","title":"Boundary","location":{{"lat":1.0,"lon":2.0}},"radius_m":{radius_m}}}"#
                ),
            ))
            .await?;
        assert_eq!(response.status(), StatusCode::CREATED);
    }

    // Missing location.
    let res = app
        .clone()
        .oneshot(post_accounts(Some(&cookie), r#"{"title":"X"}"#))
        .await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);

    // Missing title.
    let res = app
        .clone()
        .oneshot(post_accounts(
            Some(&cookie),
            r#"{"location":{"lat":1.0,"lon":2.0}}"#,
        ))
        .await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);

    // type=ron is rejected in v0.
    let res = app
        .oneshot(post_accounts(
            Some(&cookie),
            r#"{"title":"X","type":"ron","location":{"lat":1.0,"lon":2.0}}"#,
        ))
        .await?;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);

    Ok(())
}

#[tokio::test]
#[serial]
async fn create_profile_fields_are_bounded_and_normalised_before_jsonl_side_effects() -> Result<()>
{
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, state) = app_with_operator(&in_dir, "admin1", Role::Admin).await?;

    let invalid_payloads = [
        serde_json::json!({
            "id": "55555555-5555-4555-8555-555555555551",
            "title": "Summary too long",
            "location": {"lat": 53.55, "lon": 9.99},
            "summary": "x".repeat(501)
        }),
        serde_json::json!({
            "id": "55555555-5555-4555-8555-555555555552",
            "title": "Too many tags",
            "location": {"lat": 53.55, "lon": 9.99},
            "tags": (0..65).map(|index| format!("tag-{index}" )).collect::<Vec<_>>()
        }),
        serde_json::json!({
            "id": "55555555-5555-4555-8555-555555555553",
            "title": "Tag too long",
            "location": {"lat": 53.55, "lon": 9.99},
            "tags": ["x".repeat(65)]
        }),
        serde_json::json!({
            "id": "55555555-5555-4555-8555-555555555555",
            "title": "Object summary",
            "location": {"lat": 53.55, "lon": 9.99},
            "summary": {"text": "wrong type"}
        }),
        serde_json::json!({
            "id": "55555555-5555-4555-8555-555555555556",
            "title": "String tags",
            "location": {"lat": 53.55, "lon": 9.99},
            "tags": "wrong type"
        }),
        serde_json::json!({
            "id": "55555555-5555-4555-8555-555555555557",
            "title": "Mixed tags",
            "location": {"lat": 53.55, "lon": 9.99},
            "tags": ["valid", 7, "also-valid"]
        }),
        serde_json::json!({
            "id": "55555555-5555-4555-8555-555555555558",
            "title": "Null summary",
            "location": {"lat": 53.55, "lon": 9.99},
            "summary": null
        }),
        serde_json::json!({
            "id": "55555555-5555-4555-8555-555555555559",
            "title": "Null tags",
            "location": {"lat": 53.55, "lon": 9.99},
            "tags": null
        }),
    ];

    for payload in invalid_payloads {
        let id = payload["id"].as_str().context("invalid payload id")?;
        let response = app
            .clone()
            .oneshot(post_accounts(Some(&cookie), &payload.to_string()))
            .await?;
        assert_eq!(response.status(), StatusCode::BAD_REQUEST);
        assert!(state.accounts.read().await.get(id).is_none());
    }
    assert!(
        !in_dir.join("demo.accounts.jsonl").exists(),
        "invalid profile fields must not append JSONL"
    );

    let id = "55555555-5555-4555-8555-555555555554";
    let international_tag = "🐋".repeat(64);
    let expected_tags = vec![
        "alpha".to_string(),
        "beta".to_string(),
        international_tag.clone(),
    ];
    let expected_tags_json = serde_json::to_value(&expected_tags)?;
    let payload = serde_json::json!({
        "id": id,
        "title": "Normalised",
        "location": {"lat": 53.55, "lon": 9.99},
        "summary": "  Gemeinsame Dinge  ",
        "tags": [" alpha ", "alpha", "", " beta ", international_tag]
    });
    let response = app
        .clone()
        .oneshot(post_accounts(Some(&cookie), &payload.to_string()))
        .await?;
    assert_eq!(response.status(), StatusCode::CREATED);
    let bytes = body::to_bytes(response.into_body(), usize::MAX).await?;
    let created: serde_json::Value = serde_json::from_slice(&bytes)?;
    assert_eq!(created["summary"], "Gemeinsame Dinge");
    assert_eq!(created["tags"], expected_tags_json);

    let cached = state.accounts.read().await;
    let account = cached.get(id).context("normalised account in cache")?;
    assert_eq!(account.public.summary.as_deref(), Some("Gemeinsame Dinge"));
    assert_eq!(account.public.tags, expected_tags);
    drop(cached);

    let persisted = std::fs::read_to_string(in_dir.join("demo.accounts.jsonl"))?;
    let record: serde_json::Value = serde_json::from_str(persisted.trim())?;
    assert_eq!(record["summary"], "Gemeinsame Dinge");
    assert_eq!(record["tags"], expected_tags_json);

    Ok(())
}

#[tokio::test]
#[serial]
async fn invalid_domain_write_config_blocks_account_create_without_side_effects() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, state) = app_with_operator_sources(
        &in_dir,
        "admin1",
        Role::Admin,
        DomainReadSource::Jsonl,
        DomainAccountWriteSource::Postgres,
    )
    .await?;

    let id = "44444444-4444-4444-8444-444444444444";
    let body =
        format!(r#"{{"id":"{id}","title":"InvalidCfg","location":{{"lat":53.55,"lon":9.99}}}}"#);

    let res = app
        .clone()
        .oneshot(post_accounts(Some(&cookie), &body))
        .await?;
    assert_eq!(res.status(), StatusCode::INTERNAL_SERVER_ERROR);
    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let response = String::from_utf8(bytes.to_vec())?;
    assert!(response.contains("INVALID_DOMAIN_WRITE_CONFIG"));

    assert!(state.accounts.read().await.get(id).is_none());
    let accounts_path = in_dir.join("demo.accounts.jsonl");
    assert!(
        !accounts_path.exists(),
        "invalid config must not append the account JSONL file"
    );

    Ok(())
}

#[tokio::test]
#[serial]
async fn postgres_read_source_blocks_account_create_without_persisting() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, state) =
        app_with_operator_read_source(&in_dir, "admin1", Role::Admin, DomainReadSource::Postgres)
            .await?;

    let id = "33333333-3333-4333-8333-333333333333";
    let body =
        format!(r#"{{"id":"{id}","title":"Blocked","location":{{"lat":53.55,"lon":9.99}}}}"#);

    let res = app
        .clone()
        .oneshot(post_accounts(Some(&cookie), &body))
        .await?;
    assert_eq!(res.status(), StatusCode::CONFLICT);
    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let response = String::from_utf8(bytes.to_vec())?;
    assert!(response.contains("DOMAIN_READ_SOURCE_READ_ONLY"));

    assert!(state.accounts.read().await.get(id).is_none());
    let accounts_path = in_dir.join("demo.accounts.jsonl");
    assert!(
        !accounts_path.exists(),
        "blocked create must not append the account JSONL file"
    );

    Ok(())
}

#[tokio::test]
#[serial]
async fn duplicate_id_returns_409() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, _state) = app_with_operator(&in_dir, "admin1", Role::Admin).await?;

    let id = "11111111-1111-4111-8111-111111111111";
    let body = format!(r#"{{"id":"{id}","title":"First","location":{{"lat":1.0,"lon":2.0}}}}"#);

    let res = app
        .clone()
        .oneshot(post_accounts(Some(&cookie), &body))
        .await?;
    assert_eq!(res.status(), StatusCode::CREATED);

    // Same id again -> 409 Conflict.
    let res = app.oneshot(post_accounts(Some(&cookie), &body)).await?;
    assert_eq!(res.status(), StatusCode::CONFLICT);
    Ok(())
}

/// radius_m=0 (default): public_pos must equal the submitted location exactly.
/// This proves the create path does not silently discard or alter exact coordinates.
#[tokio::test]
#[serial]
async fn radius_m_zero_public_pos_is_exact() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, _state) = app_with_operator(&in_dir, "admin1", Role::Admin).await?;

    let id = "22222222-2222-4222-8222-222222222222";
    let lat = 53.5503_f64;
    let lon = 9.9932_f64;

    let res = app
        .oneshot(post_accounts(
            Some(&cookie),
            &format!(
                r#"{{"id":"{id}","title":"Exact","location":{{"lat":{lat},"lon":{lon}}},"radius_m":0}}"#
            ),
        ))
        .await?;
    assert_eq!(res.status(), StatusCode::CREATED);

    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let created: serde_json::Value = serde_json::from_slice(&bytes)?;

    // radius_m=0 => public_pos must equal the submitted location exactly.
    assert_eq!(
        created["public_pos"]["lat"], lat,
        "radius_m=0: public_pos.lat must equal submitted location.lat"
    );
    assert_eq!(
        created["public_pos"]["lon"], lon,
        "radius_m=0: public_pos.lon must equal submitted location.lon"
    );
    // location must never be exposed publicly.
    assert!(created.get("location").is_none());
    Ok(())
}

/// radius_m>0: the API creates a private random projection, exposes only its
/// public point and keeps that point inside the declared geodesic radius.
#[tokio::test]
#[serial]
async fn radius_m_positive_uses_private_projection_inside_true_radius() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, _state) = app_with_operator(&in_dir, "admin1", Role::Admin).await?;

    let id = "33333333-3333-4333-8333-333333333333";
    let lat = 53.5503_f64;
    let lon = 9.9932_f64;
    let radius_m = 500_u32;

    let res = app
        .clone()
        .oneshot(post_accounts(
            Some(&cookie),
            &format!(
                r#"{{"id":"{id}","title":"Radius","location":{{"lat":{lat},"lon":{lon}}},"radius_m":{radius_m}}}"#
            ),
        ))
        .await?;
    assert_eq!(res.status(), StatusCode::CREATED);

    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let created: serde_json::Value = serde_json::from_slice(&bytes)?;
    let public_lat = created["public_pos"]["lat"]
        .as_f64()
        .context("public_pos.lat must be a number")?;
    let public_lon = created["public_pos"]["lon"]
        .as_f64()
        .context("public_pos.lon must be a number")?;

    assert_eq!(created["map_state"], "radius");
    assert_eq!(created["radius_m"], radius_m);
    assert!(created.get("location").is_none());
    assert!(created.get("radius_projection").is_none());
    assert!(created.get("anchor").is_none());

    fn great_circle_distance_m(lat1: f64, lon1: f64, lat2: f64, lon2: f64) -> f64 {
        const EARTH_RADIUS_M: f64 = 6_371_008.8;
        let lat1 = lat1.to_radians();
        let lat2 = lat2.to_radians();
        let delta_lat = lat2 - lat1;
        let delta_lon = (lon2 - lon1).to_radians();
        let haversine = (delta_lat / 2.0).sin().powi(2)
            + lat1.cos() * lat2.cos() * (delta_lon / 2.0).sin().powi(2);
        2.0 * EARTH_RADIUS_M * haversine.clamp(0.0, 1.0).sqrt().asin()
    }

    let distance_m = great_circle_distance_m(lat, lon, public_lat, public_lon);
    assert!(
        distance_m > 0.0,
        "radius projection must not expose the exact residence"
    );
    assert!(
        distance_m <= f64::from(radius_m) + 0.05,
        "public point is {distance_m}m away, outside the declared {radius_m}m radius"
    );

    let persisted = std::fs::read_to_string(in_dir.join("demo.accounts.jsonl"))?;
    let record: serde_json::Value = serde_json::from_str(persisted.trim())?;
    assert!(record.get("radius_projection").is_some());
    assert_eq!(record["location"]["lat"], lat);
    assert_eq!(record["location"]["lon"], lon);
    Ok(())
}

/// The random point is generated once and persisted privately. Reads of the
/// same account reuse that binding instead of deriving a point from public data.
#[tokio::test]
#[serial]
async fn radius_m_positive_public_pos_is_stable_after_persistence() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);

    let (app, cookie, _state) = app_with_operator(&in_dir, "admin1", Role::Admin).await?;

    let id = "44444444-4444-4444-8444-444444444444";
    let lat = 53.55_f64;
    let lon = 9.99_f64;
    let radius_m = 250_u32;

    let res = app
        .clone()
        .oneshot(post_accounts(
            Some(&cookie),
            &format!(
                r#"{{"id":"{id}","title":"Stable","location":{{"lat":{lat},"lon":{lon}}},"radius_m":{radius_m}}}"#
            ),
        ))
        .await?;
    assert_eq!(res.status(), StatusCode::CREATED);
    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let created: serde_json::Value = serde_json::from_slice(&bytes)?;
    let post_pos = created["public_pos"].clone();

    let res = app
        .oneshot(axum::http::Request::get(format!("/accounts/{id}")).body(body::Body::empty())?)
        .await?;
    assert_eq!(res.status(), StatusCode::OK);
    let bytes = body::to_bytes(res.into_body(), usize::MAX).await?;
    let fetched: serde_json::Value = serde_json::from_slice(&bytes)?;

    assert_eq!(post_pos, fetched["public_pos"]);
    assert!(fetched.get("location").is_none());
    assert!(fetched.get("radius_projection").is_none());
    Ok(())
}

fn patch_own_profile(cookie: Option<&str>, json_body: &str) -> Request<body::Body> {
    let mut builder = Request::patch("/accounts/me/profile")
        .header("Content-Type", "application/json")
        .header("Host", "localhost")
        .header("Origin", "http://localhost");
    if let Some(cookie) = cookie {
        builder = builder.header("Cookie", cookie);
    }
    builder
        .body(body::Body::from(json_body.to_string()))
        .expect("profile request")
}

#[tokio::test]
#[serial]
async fn weber_updates_only_own_jsonl_garnrolle_and_reads_private_profile() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);
    let own_id = "weber-profile-1";
    let other_id = "other-profile-1";
    std::fs::write(
        in_dir.join("demo.accounts.jsonl"),
        format!(
            "{}\n{}\n",
            serde_json::json!({
                "id": own_id,
                "type": "garnrolle",
                "title": "Eigene Garnrolle",
                "role": "weber",
                "map_state": "not_on_map",
                "radius_m": 0
            }),
            serde_json::json!({
                "id": other_id,
                "type": "garnrolle",
                "title": "Fremde Garnrolle",
                "role": "weber",
                "map_state": "not_on_map",
                "radius_m": 0
            })
        ),
    )?;

    let (app, cookie, state) = app_with_operator(&in_dir, own_id, Role::Weber).await?;
    let response = app
        .clone()
        .oneshot(patch_own_profile(
            Some(&cookie),
            r#"{
              "title":"Meine gesetzte Garnrolle",
              "summary":"Selbst gespeichert",
              "tags":["skill:Kochen","interest:Commons"],
              "address":"Poelsweg 2, Hamburg",
              "map_state":"exact",
              "location":{"lat":53.5604,"lon":10.0630}
            }"#,
        ))
        .await?;
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = body::to_bytes(response.into_body(), usize::MAX).await?;
    let profile: serde_json::Value = serde_json::from_slice(&bytes)?;
    assert_eq!(profile["id"], own_id);
    assert_eq!(profile["title"], "Meine gesetzte Garnrolle");
    assert_eq!(profile["address"], "Poelsweg 2, Hamburg");
    assert_eq!(profile["location"]["lat"], 53.5604);
    assert_eq!(profile["map_state"], "exact");
    assert!(profile.get("email").is_none());
    assert!(profile.get("role").is_none());

    let response = app
        .clone()
        .oneshot(
            Request::get("/accounts/me/profile")
                .header("Cookie", &cookie)
                .body(body::Body::empty())?,
        )
        .await?;
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = body::to_bytes(response.into_body(), usize::MAX).await?;
    let reloaded: serde_json::Value = serde_json::from_slice(&bytes)?;
    assert_eq!(reloaded["address"], "Poelsweg 2, Hamburg");
    assert_eq!(reloaded["location"]["lon"], 10.0630);

    let contents = std::fs::read_to_string(in_dir.join("demo.accounts.jsonl"))?;
    let records: Vec<serde_json::Value> = contents
        .lines()
        .map(serde_json::from_str)
        .collect::<Result<_, _>>()?;
    let latest_own = records
        .iter()
        .rev()
        .find(|record| record["id"] == own_id)
        .context("latest own record")?;
    assert_eq!(latest_own["title"], "Meine gesetzte Garnrolle");
    let other_records = records
        .iter()
        .filter(|record| record["id"] == other_id)
        .count();
    assert_eq!(other_records, 1, "foreign Garnrolle must not be written");

    let cache = state.accounts.read().await;
    let own = cache.get(own_id).context("own account in cache")?;
    assert_eq!(own.public.title, "Meine gesetzte Garnrolle");
    assert_eq!(own.public.map_state, GarnrolleMapState::Exact);
    Ok(())
}

#[tokio::test]
#[serial]
async fn weber_radius_profile_keeps_projection_binding_private_and_stable() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);
    let account_id = "weber-radius-profile-1";
    std::fs::write(
        in_dir.join("demo.accounts.jsonl"),
        serde_json::json!({
            "id": account_id,
            "type": "garnrolle",
            "title": "Radius Garnrolle",
            "role": "weber",
            "map_state": "not_on_map",
            "radius_m": 0
        })
        .to_string()
            + "\n",
    )?;
    let (app, cookie, _state) = app_with_operator(&in_dir, account_id, Role::Weber).await?;

    let radius_body = r#"{
      "title":"Radius Garnrolle",
      "summary":"Privat gebunden",
      "tags":["interest:Commons"],
      "address":"Poelsweg 2, Hamburg",
      "map_state":"radius",
      "radius_m":250,
      "location":{"lat":53.5604,"lon":10.0630}
    }"#;
    let response = app
        .clone()
        .oneshot(patch_own_profile(Some(&cookie), radius_body))
        .await?;
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = body::to_bytes(response.into_body(), usize::MAX).await?;
    let profile: serde_json::Value = serde_json::from_slice(&bytes)?;
    assert_eq!(profile["map_state"], "radius");
    assert_eq!(profile["radius_m"], 250);
    assert!(profile.get("radius_projection").is_none());
    assert!(profile.get("public_pos").is_none());

    let read_latest_binding = || -> Result<serde_json::Value> {
        let contents = std::fs::read_to_string(in_dir.join("demo.accounts.jsonl"))?;
        let record = contents
            .lines()
            .rev()
            .map(serde_json::from_str::<serde_json::Value>)
            .collect::<Result<Vec<_>, _>>()?
            .into_iter()
            .find(|record| record["id"] == account_id)
            .context("latest radius profile record")?;
        Ok(record["radius_projection"].clone())
    };
    let initial_binding = read_latest_binding()?;
    assert_eq!(initial_binding["version"], 1);
    assert_eq!(initial_binding["radius_m"], 250);

    let response = app
        .clone()
        .oneshot(patch_own_profile(
            Some(&cookie),
            r#"{
              "title":"Radius Garnrolle",
              "summary":"Privat gebunden",
              "tags":["interest:Commons"],
              "address":"Poelsweg 2, Hamburg",
              "map_state":"not_on_map"
            }"#,
        ))
        .await?;
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(read_latest_binding()?, initial_binding);

    let response = app
        .clone()
        .oneshot(patch_own_profile(Some(&cookie), radius_body))
        .await?;
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(read_latest_binding()?, initial_binding);

    let response = app
        .oneshot(
            Request::get("/accounts/me/profile")
                .header("Cookie", &cookie)
                .body(body::Body::empty())?,
        )
        .await?;
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = body::to_bytes(response.into_body(), usize::MAX).await?;
    let reloaded: serde_json::Value = serde_json::from_slice(&bytes)?;
    assert!(reloaded.get("radius_projection").is_none());
    assert!(reloaded.get("public_pos").is_none());
    Ok(())
}

#[tokio::test]
#[serial]
async fn gast_reads_and_updates_own_private_profile() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);
    let account_id = "gast-profile-1";
    std::fs::write(
        in_dir.join("demo.accounts.jsonl"),
        serde_json::json!({
            "id": account_id,
            "type": "garnrolle",
            "title": "Gast Garnrolle",
            "role": "gast",
            "map_state": "not_on_map",
            "radius_m": 0,
            "address": "Private Testadresse",
            "location": {"lat": 53.5, "lon": 10.0}
        })
        .to_string()
            + "
",
    )?;
    let (app, cookie, _state) = app_with_operator(&in_dir, account_id, Role::Gast).await?;

    let response = app
        .clone()
        .oneshot(
            Request::get("/accounts/me/profile")
                .header("Cookie", &cookie)
                .body(body::Body::empty())?,
        )
        .await?;
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = body::to_bytes(response.into_body(), usize::MAX).await?;
    let profile: serde_json::Value = serde_json::from_slice(&bytes)?;
    assert_eq!(profile["id"], account_id);
    assert_eq!(profile["address"], "Private Testadresse");
    assert_eq!(profile["location"]["lat"], 53.5);
    assert!(profile.get("role").is_none());

    let response = app
        .clone()
        .oneshot(patch_own_profile(
            Some(&cookie),
            r#"{"title":"Mitwirkende Gastgarnrolle","tags":["gast"],"map_state":"not_on_map"}"#,
        ))
        .await?;
    assert_eq!(response.status(), StatusCode::OK);

    let response = app
        .clone()
        .oneshot(
            Request::get("/accounts/me/profile")
                .header("Cookie", &cookie)
                .body(body::Body::empty())?,
        )
        .await?;
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = body::to_bytes(response.into_body(), usize::MAX).await?;
    let updated: serde_json::Value = serde_json::from_slice(&bytes)?;
    assert_eq!(updated["title"], "Mitwirkende Gastgarnrolle");
    assert_eq!(updated["map_state"], "not_on_map");
    assert_eq!(updated["address"], "Private Testadresse");
    assert_eq!(updated["location"]["lon"], 10.0);
    assert_eq!(
        updated["tags"],
        serde_json::json!(["gast", "account", "garnrolle"])
    );

    let response = app
        .clone()
        .oneshot(patch_own_profile(
            Some(&cookie),
            r#"{"title":"Mitwirkende Gastgarnrolle","tags":["gast"],"map_state":"not_on_map","clear_address":true,"clear_location":true}"#,
        ))
        .await?;
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = body::to_bytes(response.into_body(), usize::MAX).await?;
    let cleared: serde_json::Value = serde_json::from_slice(&bytes)?;
    assert!(cleared.get("address").is_none());
    assert!(cleared.get("location").is_none());

    let response = app
        .oneshot(
            Request::get("/accounts/me/profile")
                .header("Cookie", &cookie)
                .body(body::Body::empty())?,
        )
        .await?;
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = body::to_bytes(response.into_body(), usize::MAX).await?;
    let reloaded: serde_json::Value = serde_json::from_slice(&bytes)?;
    assert!(reloaded.get("address").is_none());
    assert!(reloaded.get("location").is_none());
    Ok(())
}

#[tokio::test]
#[serial]
async fn own_profile_update_requires_authentication_and_rejects_identity_fields() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let in_dir = tmp.path().join("in");
    std::fs::create_dir_all(&in_dir)?;
    let _env = set_gewebe_in_dir(&in_dir);
    std::fs::write(
        in_dir.join("demo.accounts.jsonl"),
        serde_json::json!({
            "id": "weber-profile-2",
            "type": "garnrolle",
            "title": "Eigene Garnrolle",
            "role": "weber",
            "map_state": "not_on_map",
            "radius_m": 0
        })
        .to_string()
            + "\n",
    )?;
    let (app, cookie, _state) = app_with_operator(&in_dir, "weber-profile-2", Role::Weber).await?;

    let response = app
        .clone()
        .oneshot(patch_own_profile(
            None,
            r#"{"title":"X","tags":[],"map_state":"not_on_map"}"#,
        ))
        .await?;
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);

    let response = app
        .oneshot(patch_own_profile(
            Some(&cookie),
            r#"{
              "id":"other-profile",
              "role":"admin",
              "title":"X",
              "tags":[],
              "map_state":"not_on_map"
            }"#,
        ))
        .await?;
    assert_eq!(response.status(), StatusCode::UNPROCESSABLE_ENTITY);
    Ok(())
}
