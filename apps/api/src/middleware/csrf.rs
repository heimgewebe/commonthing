use std::env;

use axum::{
    body::Body,
    http::{Method, Request, StatusCode, Uri},
    middleware::Next,
    response::{IntoResponse, Response},
};
use axum_extra::extract::cookie::CookieJar;

use crate::routes::auth::SESSION_COOKIE_NAME;

/// Middleware to enforce CSRF protection via Origin/Referer checks.
///
/// Note: This middleware relies on the `Host` header being accurate. In reverse proxy setups
/// (e.g., Caddy, Cloudflare), ensure the proxy is configured to forward the original Host
/// or overwrite it correctly so it matches the external Origin/Referer seen by the client.
pub async fn require_csrf(jar: CookieJar, req: Request<Body>, next: Next) -> Response {
    let method = req.method();

    // 1. Pass through safe methods (GET, HEAD, OPTIONS). TRACE excluded.
    if method == Method::GET || method == Method::HEAD || method == Method::OPTIONS {
        return next.run(req).await;
    }

    // 2. Explicit exemptions
    let path = req.uri().path();
    if is_magic_link_consume_post(req.method(), path) {
        // Magic-link consume is a one-time login confirmation POST that may be
        // submitted from an email-opened document with `Origin: null`. It must
        // remain narrowly exempt from the session-cookie Origin/Referer guard
        // because the handler is authoritative: it validates the submitted
        // token, verifies the token-bound nonce cookie, and only then creates a
        // session. Invalid token/nonce cases must continue to fail there.
        return next.run(req).await;
    }

    // Logout is deliberately exempt (see CSRF_EXEMPT_MUTATING_ROUTES in
    // apps/api/tests/auth_security_invariants.rs): sign-out must stay reachable
    // without the Origin/Referer path. There is no `/auth/login` route — the
    // login entry points are `/auth/dev/login` and `/auth/magic-link/request`,
    // and both are already covered by the no-session-cookie skip below.
    if path.ends_with("/auth/logout") {
        return next.run(req).await;
    }

    // 3. Skip if no session cookie
    if jar.get(SESSION_COOKIE_NAME).is_none() {
        return next.run(req).await;
    }

    let headers = req.headers();

    // 4. Allowlist Check (Origin OR Referer)
    if let Ok(allowlist) = env::var("CSRF_ALLOWED_ORIGINS") {
        let allowed_list = parse_allowed_origins(&allowlist).accepted;

        // Check Origin
        if let Some(origin) = headers.get("origin").and_then(|v| v.to_str().ok()) {
            if allowed_list.contains(&origin.to_ascii_lowercase()) {
                return next.run(req).await;
            }
        }

        // Check Referer
        if let Some(referer) = headers.get("referer").and_then(|v| v.to_str().ok()) {
            let ref_lc = referer.to_ascii_lowercase();
            if allowed_list
                .iter()
                .any(|allowed| ref_lc == *allowed || ref_lc.starts_with(&format!("{}/", allowed)))
            {
                return next.run(req).await;
            }
        }
    }

    // 5. Host Validation
    let host_raw = headers
        .get("host")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");

    if host_raw.is_empty() {
        tracing::warn!("CSRF check failed: Missing Host header");
        return StatusCode::FORBIDDEN.into_response();
    }

    let (host_domain, host_port) = parse_host_header(host_raw);

    // Robust localhost check
    let is_localhost = host_domain == "localhost"
        || host_domain == "127.0.0.1"
        || host_domain == "::1"
        || host_domain == "[::1]";

    let uri_path = req.uri().path();

    // 6. Check Origin
    if let Some(origin) = headers.get("origin").and_then(|v| v.to_str().ok()) {
        let (origin_scheme, origin_host_raw) = if let Some(rest) = origin.strip_prefix("https://") {
            ("https", rest)
        } else if let Some(rest) = origin.strip_prefix("http://") {
            ("http", rest)
        } else {
            ("", origin)
        };

        if origin_host_raw.contains(['/', '?', '#']) {
            tracing::warn!(?origin, ?host_raw, uri = ?uri_path, "CSRF check failed: Invalid Origin format");
            return StatusCode::FORBIDDEN.into_response();
        }

        if origin_scheme == "http" && !is_localhost {
            tracing::warn!(
                ?origin,
                ?host_raw,
                uri = ?uri_path,
                "CSRF check failed: Insecure Origin (HTTP) on non-localhost"
            );
            return StatusCode::FORBIDDEN.into_response();
        }

        let (origin_domain, origin_port) = parse_host_header(origin_host_raw);
        let domains_match = host_domain.eq_ignore_ascii_case(&origin_domain);
        let ports_match = same_effective_port(host_port, origin_port, origin_scheme);

        if !domains_match || !ports_match {
            tracing::warn!(?origin, ?host_raw, uri = ?uri_path, "CSRF check failed: Origin mismatch");
            return StatusCode::FORBIDDEN.into_response();
        }
        return next.run(req).await;
    }

    // 7. Fallback: Check Referer (Parsed as URI)
    if let Some(referer) = headers.get("referer").and_then(|v| v.to_str().ok()) {
        let referer_uri = match referer.parse::<Uri>() {
            Ok(u) => u,
            Err(_) => {
                tracing::warn!(?referer, ?host_raw, uri = ?uri_path, "CSRF check failed: Invalid Referer URI");
                return StatusCode::FORBIDDEN.into_response();
            }
        };

        let ref_scheme = referer_uri.scheme_str().unwrap_or("http");

        if ref_scheme == "http" && !is_localhost {
            tracing::warn!(
                ?referer,
                ?host_raw,
                uri = ?uri_path,
                "CSRF check failed: Insecure Referer (HTTP) on non-localhost"
            );
            return StatusCode::FORBIDDEN.into_response();
        }

        let (ref_host, ref_port) = if let Some(auth) = referer_uri.authority() {
            (auth.host().to_string(), auth.port_u16())
        } else {
            tracing::warn!(?referer, ?host_raw, uri = ?uri_path, "CSRF check failed: Relative Referer");
            return StatusCode::FORBIDDEN.into_response();
        };

        let domains_match = host_domain.eq_ignore_ascii_case(&ref_host);
        let ports_match = same_effective_port(host_port, ref_port, ref_scheme);

        if !domains_match || !ports_match {
            tracing::warn!(?referer, ?host_raw, uri = ?uri_path, "CSRF check failed: Referer mismatch");
            return StatusCode::FORBIDDEN.into_response();
        }
        return next.run(req).await;
    }

    // 8. Block if neither is present
    tracing::warn!(method = ?method, ?host_raw, uri = ?uri_path, "CSRF check failed: Missing Origin and Referer");
    StatusCode::FORBIDDEN.into_response()
}

/// `CSRF_ALLOWED_ORIGINS` split into usable origins and rejected entries.
pub(crate) struct AllowedOrigins {
    pub accepted: Vec<String>,
    pub rejected: Vec<String>,
}

/// Parses `CSRF_ALLOWED_ORIGINS`. Entries are lowercased and one trailing `/`
/// is dropped, because a browser `Origin` never carries one. Anything that is
/// not a bare `http(s)://host[:port]` origin can never match and is rejected,
/// so startup can report it instead of the origin failing silently.
pub(crate) fn parse_allowed_origins(raw: &str) -> AllowedOrigins {
    let mut accepted = Vec::new();
    let mut rejected = Vec::new();
    for entry in raw
        .split(',')
        .map(str::trim)
        .filter(|entry| !entry.is_empty())
    {
        let normalized = entry.to_ascii_lowercase();
        let normalized = normalized.strip_suffix('/').unwrap_or(&normalized);
        let authority = normalized
            .strip_prefix("https://")
            .or_else(|| normalized.strip_prefix("http://"));
        match authority {
            Some(authority)
                if !authority.is_empty() && !authority.contains(['/', '?', '#', '@']) =>
            {
                accepted.push(normalized.to_string())
            }
            _ => rejected.push(entry.to_string()),
        }
    }
    AllowedOrigins { accepted, rejected }
}

/// Logs `CSRF_ALLOWED_ORIGINS` entries that can never match. Called once at
/// startup; the middleware ignores the same entries on every request.
pub fn warn_on_invalid_allowed_origins() {
    if let Ok(raw) = env::var("CSRF_ALLOWED_ORIGINS") {
        for entry in parse_allowed_origins(&raw).rejected {
            tracing::warn!(
                entry = %entry,
                "CSRF_ALLOWED_ORIGINS entry is not a bare http(s)://host[:port] origin and is ignored"
            );
        }
    }
}

/// Compare ports as part of a complete `(scheme, host, effective port)` origin.
///
/// A missing port does not mean "either standard port": its effective value is
/// determined by the presented Origin/Referer scheme. Consequently, an HTTPS
/// origin without a port is equivalent to an explicit `:443`, never `:80`.
fn same_effective_port(host_port: Option<u16>, candidate_port: Option<u16>, scheme: &str) -> bool {
    let default_port = match scheme {
        "http" => 80,
        "https" => 443,
        _ => return false,
    };

    host_port.unwrap_or(default_port) == candidate_port.unwrap_or(default_port)
}

/// Helper to parse host:port string using http::Uri logic.
fn parse_host_header(input: &str) -> (String, Option<u16>) {
    if let Some(rest) = input.strip_suffix(':') {
        return parse_host_header(rest);
    }

    let uri_string = format!("http://{}", input);

    if let Ok(uri) = uri_string.parse::<Uri>() {
        if let Some(authority) = uri.authority() {
            return (authority.host().to_string(), authority.port_u16());
        }
    }

    (input.to_string(), None)
}

fn is_magic_link_consume_post(method: &Method, path: &str) -> bool {
    *method == Method::POST
        && (path == "/auth/magic-link/consume" || path == "/api/auth/magic-link/consume")
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{middleware::from_fn, routing::post, Router};
    use tower::ServiceExt;

    #[test]
    fn allowed_origins_are_normalized_and_invalid_entries_rejected() {
        let parsed = parse_allowed_origins(
            " https://Gewebe.example/ ,http://localhost:5173,, gewebe.example,https://a.example/path,https://u@b.example",
        );
        assert_eq!(
            parsed.accepted,
            vec![
                "https://gewebe.example".to_string(),
                "http://localhost:5173".to_string()
            ]
        );
        assert_eq!(
            parsed.rejected,
            vec![
                "gewebe.example".to_string(),
                "https://a.example/path".to_string(),
                "https://u@b.example".to_string()
            ]
        );
    }

    #[test]
    fn test_parse_host_header() {
        assert_eq!(
            parse_host_header("example.com"),
            ("example.com".to_string(), None)
        );
        assert_eq!(
            parse_host_header("example.com:8080"),
            ("example.com".to_string(), Some(8080))
        );
        assert_eq!(
            parse_host_header("[::1]:8080"),
            ("[::1]".to_string(), Some(8080))
        );
        assert_eq!(parse_host_header("[::1]"), ("[::1]".to_string(), None));
    }

    /// Mirrors the exemption arm in `require_csrf`. Kept next to it so the two
    /// cannot drift; the router-level classification is enforced separately by
    /// `csrf_mutating_route_drift_guard_matches_router_declarations`.
    fn is_exempt_path(path: &str) -> bool {
        path.ends_with("/auth/logout")
    }

    #[test]
    fn logout_is_exempt_under_both_mount_points() {
        assert!(is_exempt_path("/auth/logout"));
        assert!(is_exempt_path("/api/auth/logout"));
    }

    #[test]
    fn logout_all_is_not_exempt() {
        // `logout-all` escalates to a step-up challenge and must stay behind the
        // Origin/Referer check. A sloppy `ends_with` must not swallow it.
        assert!(!is_exempt_path("/auth/logout-all"));
        assert!(!is_exempt_path("/api/auth/logout-all"));
    }

    #[test]
    fn login_entry_points_are_not_matched_by_the_exemption_arm() {
        // There is no `/auth/login` route. The real entry points rely on the
        // no-session-cookie skip, not on a path exemption.
        assert!(!is_exempt_path("/auth/dev/login"));
        assert!(!is_exempt_path("/auth/magic-link/request"));
    }

    #[test]
    fn magic_link_consume_post_is_exempt_only_for_post() {
        assert!(is_magic_link_consume_post(
            &Method::POST,
            "/auth/magic-link/consume"
        ));
        assert!(is_magic_link_consume_post(
            &Method::POST,
            "/api/auth/magic-link/consume"
        ));
        assert!(!is_magic_link_consume_post(
            &Method::PUT,
            "/auth/magic-link/consume"
        ));
        assert!(!is_magic_link_consume_post(
            &Method::POST,
            "/auth/magic-link/consume/extra"
        ));
    }

    #[test]
    fn test_referer_allowlist_match() {
        let allowlist = ["https://my-dev.com".to_string()];
        let referer_exact = "https://my-dev.com".to_string();
        let referer_sub = "https://my-dev.com/foo".to_string();
        let referer_bad = "https://my-dev.com.evil.com".to_string();

        let check = |ref_val: &str| {
            allowlist
                .iter()
                .any(|allowed| ref_val == allowed || ref_val.starts_with(&format!("{}/", allowed)))
        };

        assert!(check(&referer_exact));
        assert!(check(&referer_sub));
        assert!(!check(&referer_bad));
    }

    #[test]
    fn effective_port_is_bound_to_the_scheme() {
        // HTTPS without an explicit port is exactly port 443.
        assert!(same_effective_port(None, None, "https"));
        assert!(same_effective_port(None, Some(443), "https"));
        assert!(!same_effective_port(None, Some(80), "https"));
        assert!(!same_effective_port(None, Some(8443), "https"));

        // HTTP without an explicit port is exactly port 80.
        assert!(same_effective_port(None, None, "http"));
        assert!(same_effective_port(None, Some(80), "http"));
        assert!(!same_effective_port(None, Some(443), "http"));

        // Explicit request and candidate ports must remain identical.
        assert!(same_effective_port(Some(8080), Some(8080), "http"));
        assert!(!same_effective_port(Some(8080), Some(9090), "http"));

        // Unknown schemes are never treated as same-origin.
        assert!(!same_effective_port(None, None, ""));
        assert!(!same_effective_port(None, None, "ftp"));
    }

    fn csrf_test_app() -> Router {
        async fn accepted() -> StatusCode {
            StatusCode::NO_CONTENT
        }

        Router::new()
            .route("/auth/logout-all", post(accepted))
            .route("/api/auth/logout-all", post(accepted))
            .layer(from_fn(require_csrf))
    }

    async fn csrf_status(path: &str, host: &str, header: &str, value: &str) -> StatusCode {
        let request = Request::post(path)
            .header("host", host)
            .header("cookie", format!("{SESSION_COOKIE_NAME}=test-session"))
            .header(header, value)
            .body(Body::empty())
            .expect("CSRF test request builds");

        csrf_test_app()
            .oneshot(request)
            .await
            .expect("CSRF test app responds")
            .status()
    }

    #[tokio::test]
    async fn origin_effective_https_port_is_enforced_under_both_auth_mounts() {
        for path in ["/auth/logout-all", "/api/auth/logout-all"] {
            assert_eq!(
                csrf_status(path, "commonthing.net", "origin", "https://commonthing.net").await,
                StatusCode::NO_CONTENT
            );
            assert_eq!(
                csrf_status(
                    path,
                    "commonthing.net",
                    "origin",
                    "https://commonthing.net:443"
                )
                .await,
                StatusCode::NO_CONTENT
            );
            assert_eq!(
                csrf_status(
                    path,
                    "commonthing.net",
                    "origin",
                    "https://commonthing.net:80"
                )
                .await,
                StatusCode::FORBIDDEN
            );
            assert_eq!(
                csrf_status(
                    path,
                    "commonthing.net",
                    "origin",
                    "https://commonthing.net:8443"
                )
                .await,
                StatusCode::FORBIDDEN
            );
        }
    }

    #[tokio::test]
    async fn referer_effective_https_port_is_enforced_under_both_auth_mounts() {
        for path in ["/auth/logout-all", "/api/auth/logout-all"] {
            assert_eq!(
                csrf_status(
                    path,
                    "commonthing.net",
                    "referer",
                    "https://commonthing.net/account"
                )
                .await,
                StatusCode::NO_CONTENT
            );
            assert_eq!(
                csrf_status(
                    path,
                    "commonthing.net",
                    "referer",
                    "https://commonthing.net:443/account"
                )
                .await,
                StatusCode::NO_CONTENT
            );
            assert_eq!(
                csrf_status(
                    path,
                    "commonthing.net",
                    "referer",
                    "https://commonthing.net:80/account"
                )
                .await,
                StatusCode::FORBIDDEN
            );
            assert_eq!(
                csrf_status(
                    path,
                    "commonthing.net",
                    "referer",
                    "https://commonthing.net:8443/account"
                )
                .await,
                StatusCode::FORBIDDEN
            );
        }
    }

    #[tokio::test]
    async fn localhost_http_and_loopback_addresses_keep_exact_port_support() {
        for (host, origin) in [
            ("localhost", "http://localhost"),
            ("localhost:5173", "http://localhost:5173"),
            ("127.0.0.1", "http://127.0.0.1"),
            ("[::1]", "http://[::1]"),
        ] {
            assert_eq!(
                csrf_status("/auth/logout-all", host, "origin", origin).await,
                StatusCode::NO_CONTENT,
                "matching local HTTP origin {origin} must remain supported"
            );
        }

        assert_eq!(
            csrf_status(
                "/auth/logout-all",
                "localhost",
                "origin",
                "http://localhost:443"
            )
            .await,
            StatusCode::FORBIDDEN,
            "local HTTP must not collapse port 443 into the implicit port 80"
        );
        assert_eq!(
            csrf_status(
                "/auth/logout-all",
                "localhost:5173",
                "origin",
                "http://localhost:5174"
            )
            .await,
            StatusCode::FORBIDDEN,
            "explicit local development ports must match exactly"
        );
    }
}
