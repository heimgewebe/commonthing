//! Reproducible Auth security-invariant proofs (auth-roadmap Phase 7).
//!
//! Closes the two clearest gaps named in `docs/reports/auth-status-matrix.md` §2.9:
//!   1. Systematic CSRF coverage of all currently listed CSRF-relevant mutating
//!      endpoints (previously only `/auth/session/refresh` was covered).
//!   2. Anti-enumeration *parity*: a known and an unknown email must yield an
//!      identical status, a byte-identical body, and parity of the security-
//!      relevant response headers (previously only the unknown side was checked).
//!
//! The CSRF coverage test (`csrf_blocks_all_mutating_endpoints_without_origin`)
//! exercises the production-style middleware stack wired in `src/lib.rs`
//! (`auth_middleware` as a route layer, `require_csrf` as the outer layer). The
//! anti-enumeration test
//! (`magic_link_request_is_indistinguishable_for_known_and_unknown_email`)
//! deliberately uses the narrower `app_plain()` router: `/auth/magic-link/request`
//! carries no session cookie and is not CSRF-relevant, so only response parity
//! needs proving there.

use anyhow::Result;
use axum::{
    body,
    extract::connect_info::MockConnectInfo,
    http::{header, Method, Request, StatusCode},
    Router,
};
use serial_test::serial;
use std::{collections::BTreeSet, fs, net::SocketAddr, path::PathBuf, sync::Arc};
use tokio::sync::RwLock;
use tower::ServiceExt;
use weltgewebe_api::{
    auth::{
        accounts::AccountStore, rate_limit::AuthRateLimiter, role::Role, session::SessionBackend,
    },
    config::AppConfig,
    routes::{
        accounts::{AccountInternal, AccountPublic, GarnrolleMapState},
        api_router,
        auth::GENERIC_LOGIN_MSG,
    },
    state::ApiState,
    telemetry::{BuildInfo, Metrics},
    test_helpers::EnvGuard,
};

const KNOWN_EMAIL: &str = "u1@example.com";
const ADMIN_ID: &str = "u-admin";

fn account(id: &str, title: &str, role: Role, email: &str) -> AccountInternal {
    AccountInternal {
        public: AccountPublic {
            id: id.to_string(),
            kind: "garnrolle".to_string(),
            title: title.to_string(),
            summary: None,
            public_pos: None,
            map_state: GarnrolleMapState::NotOnMap,
            radius_m: 0,
            disabled: false,
            tags: vec![],
        },
        role,
        email: Some(email.to_string()),
        webauthn_user_id: uuid::Uuid::new_v4(),
    }
}

fn build_state() -> Result<ApiState> {
    let metrics = Metrics::try_new(BuildInfo {
        version: "test",
        commit: "test",
        build_timestamp: "test",
    })?;

    // Public login on (so `/auth/magic-link/request` is reachable) with token
    // delivery via log (no SMTP). Rate limits stay unset so the limiter is a
    // no-op and `is_open_registration()` is false — an unknown email therefore
    // stays unknown instead of being auto-provisioned.
    let config = AppConfig {
        max_guest_owned_nodes: 1_000,
        domain_read_source: weltgewebe_api::config::DomainReadSource::Jsonl,
        domain_account_write_source: weltgewebe_api::config::DomainAccountWriteSource::Jsonl,
        domain_node_write_source: weltgewebe_api::config::DomainNodeWriteSource::Jsonl,
        domain_edge_write_source: weltgewebe_api::config::DomainEdgeWriteSource::Jsonl,
        passkey_credential_source: weltgewebe_api::config::PasskeyCredentialSource::InMemory,
        auth_public_login: true,
        auth_cookie_secure: weltgewebe_api::config::auth_cookie_secure_env_override()
            .unwrap_or(true),
        app_base_url: Some("http://localhost".to_string()),
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
        auth_log_magic_token: true,
        webauthn_rp_id: None,
        webauthn_rp_origin: None,
        webauthn_rp_name: None,
    };

    let rate_limiter = Arc::new(AuthRateLimiter::new(&config));

    let mut accounts = AccountStore::new();
    accounts.insert(account(ADMIN_ID, "Admin", Role::Admin, "admin@example.com"));
    accounts.insert(account("u1", "User One", Role::Gast, KNOWN_EMAIL));

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

/// Plain router (no security middleware) — used for the public `/auth/magic-link/request`
/// path, which carries no session cookie and is therefore not CSRF-relevant.
fn app_plain(state: ApiState) -> Router {
    Router::new()
        .merge(api_router())
        .layer(MockConnectInfo(SocketAddr::from(([127, 0, 0, 1], 8080))))
        .with_state(state)
}

/// Router wired exactly like production (`src/lib.rs`): `auth_middleware` as a
/// route layer, `require_csrf` as the outer layer that runs first on ingress.
fn app_with_security(state: ApiState) -> Router {
    Router::new()
        .merge(
            api_router()
                .route_layer(axum::middleware::from_fn_with_state(
                    state.clone(),
                    weltgewebe_api::middleware::auth::auth_middleware,
                ))
                .layer(axum::middleware::from_fn(
                    weltgewebe_api::middleware::csrf::require_csrf,
                )),
        )
        .layer(MockConnectInfo(SocketAddr::from(([127, 0, 0, 1], 8080))))
        .with_state(state)
}

/// A CSRF rejection from `require_csrf` is a bare `403 Forbidden` with an empty
/// body. This distinguishes it from a handler-level `403 STEP_UP_REQUIRED`,
/// which carries a JSON body.
async fn assert_csrf_blocked(app: &Router, method: Method, path: &str, session_cookie: &str) {
    let req = Request::builder()
        .method(method.clone())
        .uri(path)
        .header("Cookie", session_cookie)
        .header("Host", "localhost")
        .body(body::Body::empty())
        .expect("request builds");

    let res = app.clone().oneshot(req).await.expect("router responds");
    let status = res.status();
    let bytes = body::to_bytes(res.into_body(), usize::MAX)
        .await
        .expect("body reads");

    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "cross-site {method} {path} (no Origin/Referer) must be rejected by CSRF middleware"
    );
    assert!(
        bytes.is_empty(),
        "CSRF rejection for {method} {path} must have an empty body (got {} bytes), \
         otherwise the 403 came from a handler and not the CSRF guard",
        bytes.len()
    );
}

/// Systematic CSRF proof: every mutating endpoint behind the CSRF layer rejects
/// a request that carries a session cookie but no same-origin Origin/Referer.
#[tokio::test]
#[serial]
async fn csrf_blocks_all_mutating_endpoints_without_origin() -> Result<()> {
    let _dev = EnvGuard::set("AUTH_DEV_LOGIN", "1");
    // Ensure the optional origin allowlist does not short-circuit the check.
    let _allow = EnvGuard::unset("CSRF_ALLOWED_ORIGINS");

    let state = build_state()?;
    let app = app_with_security(state);

    // Obtain a real session cookie via dev login (no cookie yet -> CSRF skips it).
    let login = Request::post("/auth/dev/login")
        .header("Content-Type", "application/json")
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .body(body::Body::from(format!(
            r#"{{"account_id":"{ADMIN_ID}"}}"#
        )))?;
    let login_res = app.clone().oneshot(login).await?;
    assert_eq!(login_res.status(), StatusCode::OK, "dev login must succeed");
    let set_cookie = login_res
        .headers()
        .get("Set-Cookie")
        .expect("dev login sets session cookie")
        .to_str()?;
    let session_cookie = set_cookie
        .split(';')
        .next()
        .expect("cookie has a value")
        .to_string();

    // All currently listed CSRF-relevant mutating endpoints reachable under the
    // CSRF layer. `/auth/logout` is intentionally exempt via a path arm in
    // `require_csrf`; the magic-link and passkey login entry points carry no
    // session cookie and are therefore not listed here. (There is no
    // `/auth/login` route — the login entry points are `/auth/dev/login` and
    // `/auth/magic-link/request`.)
    let mutating_endpoints: &[(Method, &str)] = &[
        (Method::POST, "/auth/session/refresh"),
        (Method::POST, "/auth/logout-all"),
        (Method::DELETE, "/auth/devices/any-device-id"),
        (Method::PUT, "/auth/me/email"),
        (Method::POST, "/auth/step-up/magic-link/request"),
        (Method::POST, "/auth/step-up/magic-link/consume"),
        (Method::POST, "/auth/passkeys/register/options"),
        (Method::PUT, "/notifications/preferences"),
        (Method::POST, "/push/subscriptions"),
        (Method::DELETE, "/push/subscriptions"),
        (Method::DELETE, "/push/subscriptions/any-subscription-id"),
        // Cross-cutting (non-auth) mutating routes to prove the guard is global.
        (Method::PATCH, "/nodes/any-node-id"),
        (Method::POST, "/machine/v1/nodes"),
        (Method::PATCH, "/machine/v1/nodes/any-node-id"),
        (Method::PUT, "/machine/v1/nodes/any-node-id"),
        (Method::DELETE, "/machine/v1/nodes/any-node-id"),
    ];

    for (method, path) in mutating_endpoints {
        assert_csrf_blocked(&app, method.clone(), path, &session_cookie).await;
    }

    let origin_null_refresh = Request::post("/auth/session/refresh")
        .header("Cookie", &session_cookie)
        .header("Host", "localhost")
        .header("Origin", "null")
        .body(body::Body::empty())?;
    let origin_null_refresh_res = app.clone().oneshot(origin_null_refresh).await?;
    assert_eq!(
        origin_null_refresh_res.status(),
        StatusCode::FORBIDDEN,
        "session refresh with Origin:null must remain protected by CSRF middleware"
    );
    let origin_null_refresh_body =
        body::to_bytes(origin_null_refresh_res.into_body(), usize::MAX).await?;
    assert!(
        origin_null_refresh_body.is_empty(),
        "Origin:null session refresh rejection must be the bare CSRF 403"
    );

    // Positive control: the same refresh request *with* a matching Origin passes
    // the CSRF guard, proving the rejections above are specifically CSRF-driven
    // and the middleware does not blanket-block legitimate same-origin traffic.
    let allowed = Request::post("/auth/session/refresh")
        .header("Cookie", &session_cookie)
        .header("Host", "localhost")
        .header("Origin", "http://localhost")
        .body(body::Body::empty())?;
    let allowed_res = app.clone().oneshot(allowed).await?;
    assert_eq!(
        allowed_res.status(),
        StatusCode::OK,
        "same-origin refresh must pass the CSRF guard"
    );

    Ok(())
}

/// Anti-enumeration parity: a magic-link request for a known vs. an unknown
/// email must be indistinguishable — identical status, byte-identical body, and
/// parity of the security-relevant response headers (no `Set-Cookie` side
/// channel, identical `Content-Type`/`Cache-Control`) — and must never echo the
/// address or a token back to the caller.
#[tokio::test]
#[serial]
async fn magic_link_request_is_indistinguishable_for_known_and_unknown_email() -> Result<()> {
    let app = app_plain(build_state()?);

    let request_for = |email: &str| -> Result<Request<body::Body>> {
        Ok(Request::post("/auth/magic-link/request")
            .header("Content-Type", "application/json")
            .body(body::Body::from(format!(r#"{{"email":"{email}"}}"#)))?)
    };

    // Capture the security-relevant headers *before* consuming the body: a leak
    // here (e.g. a `Set-Cookie` only on the known path) would be an enumeration
    // side channel even when status and body match.
    let known_res = app.clone().oneshot(request_for(KNOWN_EMAIL)?).await?;
    let known_status = known_res.status();
    let known_content_type = known_res.headers().get(header::CONTENT_TYPE).cloned();
    let known_cache_control = known_res.headers().get(header::CACHE_CONTROL).cloned();
    let known_has_set_cookie = known_res.headers().contains_key(header::SET_COOKIE);
    let known_body = body::to_bytes(known_res.into_body(), usize::MAX).await?;

    let unknown_res = app
        .clone()
        .oneshot(request_for("nobody@example.com")?)
        .await?;
    let unknown_status = unknown_res.status();
    let unknown_content_type = unknown_res.headers().get(header::CONTENT_TYPE).cloned();
    let unknown_cache_control = unknown_res.headers().get(header::CACHE_CONTROL).cloned();
    let unknown_has_set_cookie = unknown_res.headers().contains_key(header::SET_COOKIE);
    let unknown_body = body::to_bytes(unknown_res.into_body(), usize::MAX).await?;

    assert_eq!(known_status, StatusCode::OK);
    assert_eq!(
        known_status, unknown_status,
        "status must be identical for known vs. unknown email"
    );
    assert_eq!(
        known_body, unknown_body,
        "response body must be byte-identical for known vs. unknown email"
    );

    // Header parity: the obvious enumeration side channels must not differ.
    assert!(
        !known_has_set_cookie,
        "magic-link request for a known email must not set a cookie"
    );
    assert!(
        !unknown_has_set_cookie,
        "magic-link request for an unknown email must not set a cookie"
    );
    assert_eq!(
        known_content_type, unknown_content_type,
        "Content-Type must be identical for known vs. unknown email"
    );
    assert_eq!(
        known_cache_control, unknown_cache_control,
        "Cache-Control must be identical for known vs. unknown email"
    );

    // Shape and non-leakage of the shared response.
    let body_val: serde_json::Value = serde_json::from_slice(&known_body)?;
    assert_eq!(body_val["ok"], true);
    assert_eq!(body_val["message"], GENERIC_LOGIN_MSG);

    let body_str = String::from_utf8_lossy(&known_body);
    assert!(
        !body_str.contains(KNOWN_EMAIL),
        "response must not echo the requested email"
    );
    assert!(
        !body_str.contains('@'),
        "response must not contain any email- or token-like material"
    );

    Ok(())
}

fn routes_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("src/routes")
}

const CSRF_COVERED_MUTATING_ROUTES: &[(&str, &str)] = &[
    ("POST", "/auth/session/refresh"),
    ("POST", "/auth/logout-all"),
    ("DELETE", "/auth/devices/{id}"),
    ("PUT", "/auth/me/email"),
    ("POST", "/auth/step-up/magic-link/request"),
    ("POST", "/auth/step-up/magic-link/consume"),
    ("POST", "/auth/passkeys/register/options"),
    ("POST", "/auth/passkeys/register/verify"),
    ("PATCH", "/nodes/{id}"),
    ("PUT", "/nodes/{id}"),
    ("DELETE", "/nodes/{id}"),
    ("POST", "/nodes"),
    ("POST", "/machine/v1/nodes"),
    ("PATCH", "/machine/v1/nodes/{id}"),
    ("PUT", "/machine/v1/nodes/{id}"),
    ("DELETE", "/machine/v1/nodes/{id}"),
    ("POST", "/conversations/{id}/messages"),
    ("POST", "/direct-conversations"),
    ("PUT", "/notifications/preferences"),
    ("POST", "/push/subscriptions"),
    ("DELETE", "/push/subscriptions"),
    ("DELETE", "/push/subscriptions/{id}"),
    ("POST", "/direct-conversations/{id}/read"),
    ("PUT", "/direct-conversations/{id}/block"),
    ("DELETE", "/direct-conversations/{id}/block"),
    (
        "PATCH",
        "/conversations/{conversation_id}/messages/{message_id}",
    ),
    (
        "DELETE",
        "/conversations/{conversation_id}/messages/{message_id}",
    ),
    ("POST", "/accounts"),
    ("PATCH", "/accounts/me/profile"),
    ("POST", "/proposals"),
    ("POST", "/proposals/{id}/withdraw"),
    ("POST", "/proposals/{id}/repeal"),
    ("POST", "/proposals/{id}/veto"),
    ("PUT", "/proposals/{id}/vote"),
    ("POST", "/proposals/{id}/messages"),
    ("POST", "/accounts/me/exit"),
    // Integration-testing-only governance deadline control. It is invoked from
    // an authenticated browser session and remains behind the normal CSRF layer.
    ("POST", "/governance/testing/proposals/{id}/advance"),
];

const CSRF_EXEMPT_MUTATING_ROUTES: &[(&str, &str)] = &[
    // Dev login is explicitly feature-gated and intentionally kept outside the CSRF policy.
    ("POST", "/auth/dev/login"),
    // Integration-testing-only browser-proof hooks. They do not ship in production builds.
    ("POST", "/auth/testing/passkeys/bootstrap-session"),
    ("POST", "/auth/testing/passkeys/register/grant"),
    // Test-only pre-session bootstrap: no ambient session authority exists yet.
    ("POST", "/auth/testing/governance/bootstrap-session"),
    // Magic-link request/consume are pre-session or redirect-driven entry points with their own flow handling.
    ("POST", "/auth/magic-link/request"),
    ("POST", "/auth/magic-link/consume"),
    // Passkey login (auth/options + auth/verify) are pre-session entry points: the
    // request carries no session cookie, so `require_csrf` skips them (step 3 of the
    // middleware) — exactly like magic-link request/consume above. There is no
    // ambient session authority to abuse, and `auth/verify` is authoritative: it
    // consumes single-use server-side state and verifies a real WebAuthn assertion
    // before any session is created. They are therefore exempt, not covered.
    ("POST", "/auth/passkeys/auth/options"),
    ("POST", "/auth/passkeys/auth/verify"),
    // Logout intentionally remains exempt so a sign-out action can be executed without the CSRF middleware path.
    ("POST", "/auth/logout"),
];

fn collect_declared_mutating_routes() -> BTreeSet<(String, String)> {
    let mut routes = BTreeSet::new();

    collect_route_sources(&routes_dir(), &mut routes);

    routes
}

fn collect_route_sources(dir: &PathBuf, routes: &mut BTreeSet<(String, String)>) {
    for entry in fs::read_dir(dir).expect("failed to read apps/api/src/routes") {
        let path = entry.expect("failed to read route entry").path();
        if path.is_dir() {
            collect_route_sources(&path, routes);
            continue;
        }

        if path.extension().and_then(|value| value.to_str()) != Some("rs") {
            continue;
        }

        let source = fs::read_to_string(&path).unwrap_or_else(|error| {
            panic!("failed to read route source {}: {}", path.display(), error)
        });
        routes.extend(collect_mutating_routes_from_source(&source));
    }
}

fn collect_mutating_routes_from_source(source: &str) -> BTreeSet<(String, String)> {
    let mut routes = BTreeSet::new();
    let mut cursor = 0;

    while let Some(route_start_rel) = source[cursor..].find(".route(") {
        let route_start = cursor + route_start_rel;
        let open_paren = route_start + ".route".len();
        let Some(route_end) = find_matching_paren(source, open_paren) else {
            break;
        };

        for route in extract_mutating_routes(&source[route_start..route_end]) {
            routes.insert(route);
        }

        cursor = route_end;
    }

    routes
}

fn find_matching_paren(source: &str, open_paren: usize) -> Option<usize> {
    let bytes = source.as_bytes();
    let mut depth = 0usize;

    for (index, byte) in bytes.get(open_paren..)?.iter().enumerate() {
        match byte {
            b'(' => depth += 1,
            b')' => {
                depth = depth.checked_sub(1)?;
                if depth == 0 {
                    return Some(open_paren + index + 1);
                }
            }
            _ => {}
        }
    }

    None
}

fn extract_mutating_routes(route_call: &str) -> Vec<(String, String)> {
    // This is a source-level, heuristic guard and intentionally not a full Rust/Axum parser.
    // It matches common router construction forms directly in source text and does not
    // resolve aliases/macros or full builder semantics across arbitrary indirection.
    let Some(path_start) = route_call.find('"').map(|index| index + 1) else {
        return Vec::new();
    };
    let Some(path_end) = route_call[path_start..]
        .find('"')
        .map(|index| index + path_start)
    else {
        return Vec::new();
    };
    let path = route_call[path_start..path_end].to_string();
    let normalized: String = route_call.chars().filter(|c| !c.is_whitespace()).collect();
    let has_on_call = normalized.contains("MethodRouter::on(")
        || normalized.contains(".on(")
        || normalized.contains("on(");

    let mut routes = Vec::new();
    for method in ["DELETE", "PUT", "PATCH", "POST"] {
        let method_lower = method.to_lowercase();
        let method_filter = format!("MethodFilter::{method}");
        let mutating_handler_match = normalized.contains(&format!("{method_lower}("))
            || normalized.contains(&format!("axum::routing::{method_lower}("));
        let mutating_service_match = normalized.contains(&format!("{method_lower}_service("))
            || normalized.contains(&format!("axum::routing::{method_lower}_service("));
        let mutating_on_match = has_on_call && normalized.contains(&method_filter);

        if mutating_handler_match || mutating_service_match || mutating_on_match {
            routes.push((method.to_string(), path.clone()));
        }
    }

    routes
}

#[test]
fn extract_mutating_routes_recognizes_on_and_service_forms() {
    let route = r#".route("/devices/{id}", MethodRouter::on(MethodFilter::POST | MethodFilter::PUT, handler).on(MethodFilter::PATCH, handler).on(MethodFilter::DELETE, handler))"#;
    let mutating = extract_mutating_routes(route)
        .into_iter()
        .map(|(method, _)| method)
        .collect::<BTreeSet<_>>();
    assert_eq!(
        mutating,
        BTreeSet::from([
            "DELETE".to_string(),
            "PATCH".to_string(),
            "POST".to_string(),
            "PUT".to_string()
        ])
    );

    let service_route = r#".route("/devices/{id}", on(MethodFilter::POST | MethodFilter::DELETE, handler).route_layer(axum::middleware::from_fn(require_auth)).and(axum::routing::put_service(service)).and(patch_service(service)).and(post_service(service)).and(delete_service(service)).and(axum::routing::post_service(service)).and(axum::routing::delete_service(service)))"#;
    let service_mutating = extract_mutating_routes(service_route)
        .into_iter()
        .map(|(method, _)| method)
        .collect::<BTreeSet<_>>();
    assert_eq!(
        service_mutating,
        BTreeSet::from([
            "DELETE".to_string(),
            "PATCH".to_string(),
            "POST".to_string(),
            "PUT".to_string()
        ])
    );
}

fn route_set(routes: &[(&str, &str)]) -> BTreeSet<(String, String)> {
    routes
        .iter()
        .map(|(method, path)| ((*method).to_string(), (*path).to_string()))
        .collect()
}

fn format_route_set(routes: &BTreeSet<(String, String)>) -> String {
    routes
        .iter()
        .map(|(method, path)| format!("{} {}", method, path))
        .collect::<Vec<_>>()
        .join(", ")
}

#[test]
fn csrf_mutating_route_drift_guard_matches_router_declarations() {
    let discovered_routes = collect_declared_mutating_routes();
    let covered_routes = route_set(CSRF_COVERED_MUTATING_ROUTES);
    let exempt_routes = route_set(CSRF_EXEMPT_MUTATING_ROUTES);
    let policy_routes = covered_routes
        .union(&exempt_routes)
        .cloned()
        .collect::<BTreeSet<_>>();

    let uncovered_routes = discovered_routes
        .difference(&policy_routes)
        .cloned()
        .collect::<BTreeSet<_>>();
    let stale_routes = policy_routes
        .difference(&discovered_routes)
        .cloned()
        .collect::<BTreeSet<_>>();

    assert!(
        uncovered_routes.is_empty(),
        "mutating route drift detected; add each new route to CSRF_COVERED_MUTATING_ROUTES or CSRF_EXEMPT_MUTATING_ROUTES: {}",
        format_route_set(&uncovered_routes)
    );

    assert!(
        stale_routes.is_empty(),
        "CSRF policy lists contain routes that are no longer declared: {}",
        format_route_set(&stale_routes)
    );
}

/// The passkey login routes are classified as CSRF-exempt (pre-session entry
/// points skipped by `require_csrf`), not covered. This locks the deliberate
/// classification so a future drift fix cannot silently move them into the
/// covered set, which would imply a protection the middleware does not apply.
#[test]
fn passkey_login_routes_are_classified_csrf_exempt() {
    let exempt = route_set(CSRF_EXEMPT_MUTATING_ROUTES);
    let covered = route_set(CSRF_COVERED_MUTATING_ROUTES);

    for route in [
        (
            "POST".to_string(),
            "/auth/passkeys/auth/options".to_string(),
        ),
        ("POST".to_string(), "/auth/passkeys/auth/verify".to_string()),
    ] {
        assert!(
            exempt.contains(&route),
            "{route:?} must be classified CSRF-exempt (pre-session login entry point)"
        );
        assert!(
            !covered.contains(&route),
            "{route:?} must not be listed as CSRF-covered: it is pre-session and skipped by require_csrf"
        );
    }
}
