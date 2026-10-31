use axum::{
    body::Body,
    extract::State,
    http::{
        header::{CONTENT_LENGTH, SET_COOKIE},
        HeaderValue, Request, StatusCode,
    },
    middleware::Next,
    response::{IntoResponse, Response},
};
use axum_extra::extract::cookie::{Cookie, CookieJar, SameSite};
use chrono::{DateTime, Utc};
use time::{Duration as CookieDuration, OffsetDateTime};

const SESSION_COOKIE_EXPIRY_SAFETY_SECONDS: i64 = 5 * 60;

use crate::{
    auth::{role::Role, session::Session},
    config::DomainReadSource,
    routes::auth::SESSION_COOKIE_NAME,
    state::ApiState,
};

#[derive(Clone, Debug)]
pub struct AuthContext {
    pub authenticated: bool,
    pub account_id: Option<String>,
    pub device_id: Option<String>,
    pub role: Role,
    pub expires_at: Option<DateTime<Utc>>,
}

fn persistent_session_cookie(session: &Session, secure: bool) -> Cookie<'static> {
    // Max-Age starts when the browser receives the response, while the database
    // expiry clock has already advanced. Expire the browser cookie five minutes
    // earlier so normal request/proxy latency cannot let it outlive the server
    // session. A session in its final five minutes therefore receives a removal
    // cookie intentionally: the browser contract expires conservatively before
    // the server contract. Both attributes derive from the stored expires_at.
    let remaining_seconds = session
        .cookie_max_age_seconds()
        .saturating_sub(SESSION_COOKIE_EXPIRY_SAFETY_SECONDS)
        .max(0);
    if remaining_seconds == 0 {
        return removal_session_cookie(secure);
    }

    let cookie_expires_at =
        session.expires_at - chrono::Duration::seconds(SESSION_COOKIE_EXPIRY_SAFETY_SECONDS);
    let expires = OffsetDateTime::from_unix_timestamp(cookie_expires_at.timestamp())
        .expect("validated session expiry must fit into cookie timestamp range");

    Cookie::build((SESSION_COOKIE_NAME, session.id.clone()))
        .path("/")
        .http_only(true)
        .same_site(SameSite::Lax)
        .secure(secure)
        .max_age(CookieDuration::seconds(remaining_seconds))
        .expires(expires)
        .build()
}

fn removal_session_cookie(secure: bool) -> Cookie<'static> {
    Cookie::build((SESSION_COOKIE_NAME, ""))
        .path("/")
        .http_only(true)
        .same_site(SameSite::Lax)
        .secure(secure)
        .max_age(CookieDuration::ZERO)
        .expires(OffsetDateTime::UNIX_EPOCH)
        .build()
}

fn append_cookie(response: &mut Response, cookie: Cookie<'static>) {
    let value = HeaderValue::try_from(cookie.to_string())
        .expect("session cookie must always form a valid Set-Cookie header");
    response.headers_mut().append(SET_COOKIE, value);
}

fn is_removal_cookie(cookie: &Cookie<'_>) -> bool {
    cookie.max_age().is_some_and(|age| age.whole_seconds() <= 0)
        || cookie
            .expires_datetime()
            .is_some_and(|expires| expires <= OffsetDateTime::now_utc())
}

fn has_supported_session_cookie_scope(cookie: &Cookie<'_>, secure: bool) -> bool {
    let secure_matches = if secure {
        cookie.secure() == Some(true)
    } else {
        cookie.secure() != Some(true)
    };

    cookie.domain().is_none()
        && cookie.path() == Some("/")
        && cookie.http_only() == Some(true)
        && cookie.same_site() == Some(SameSite::Lax)
        && secure_matches
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum SessionCookieNormalizationFailure {
    MissingServerSession,
    BackendUnavailable,
    UnsupportedCookieScope,
    DuplicateSessionCookies,
    MalformedSessionCookie,
}

impl SessionCookieNormalizationFailure {
    fn status(self) -> StatusCode {
        match self {
            Self::BackendUnavailable => StatusCode::SERVICE_UNAVAILABLE,
            Self::MissingServerSession
            | Self::UnsupportedCookieScope
            | Self::DuplicateSessionCookies
            | Self::MalformedSessionCookie => StatusCode::INTERNAL_SERVER_ERROR,
        }
    }
}

async fn normalize_session_cookie_headers(
    state: &ApiState,
    response: &mut Response,
) -> Result<bool, SessionCookieNormalizationFailure> {
    let existing = response
        .headers()
        .get_all(SET_COOKIE)
        .iter()
        .cloned()
        .collect::<Vec<_>>();
    if existing.is_empty() {
        return Ok(false);
    }

    response.headers_mut().remove(SET_COOKIE);
    let secure = state.config.auth_cookie_secure;
    let mut saw_session_cookie = false;
    let mut session_cookie_count = 0_u8;
    let mut normalized_session_cookie = None;
    let mut failure = None;

    for header in existing {
        let raw_pair = header
            .as_bytes()
            .split(|byte| *byte == b';')
            .next()
            .unwrap_or_default();
        let raw_pair = raw_pair.trim_ascii();
        let session_name = SESSION_COOKIE_NAME.as_bytes();
        let raw_is_session_cookie = raw_pair == session_name
            || raw_pair
                .strip_prefix(session_name)
                .is_some_and(|suffix| suffix.starts_with(b"="));
        let raw = header.to_str().ok();
        let parsed = raw
            .and_then(|value| Cookie::parse(value.to_owned()).ok())
            .map(|cookie| cookie.into_owned());

        let Some(cookie) = parsed else {
            if raw_is_session_cookie {
                saw_session_cookie = true;
                tracing::error!(
                    event = "auth.middleware.malformed_session_cookie",
                    "Refusing to emit a malformed session cookie"
                );
                failure.get_or_insert(SessionCookieNormalizationFailure::MalformedSessionCookie);
            } else {
                response.headers_mut().append(SET_COOKIE, header);
            }
            continue;
        };

        if cookie.name() != SESSION_COOKIE_NAME {
            response.headers_mut().append(SET_COOKIE, header);
            continue;
        }

        saw_session_cookie = true;
        session_cookie_count = session_cookie_count.saturating_add(1);
        if session_cookie_count > 1 {
            tracing::error!(
                event = "auth.middleware.duplicate_session_cookies",
                "Refusing to emit multiple session cookies in one response"
            );
            failure.get_or_insert(SessionCookieNormalizationFailure::DuplicateSessionCookies);
            continue;
        }

        if !has_supported_session_cookie_scope(&cookie, secure) {
            tracing::error!(
                event = "auth.middleware.unsupported_session_cookie_scope",
                "Refusing to emit a session cookie with a non-canonical scope"
            );
            failure.get_or_insert(SessionCookieNormalizationFailure::UnsupportedCookieScope);
            continue;
        }

        if is_removal_cookie(&cookie) || cookie.value().is_empty() {
            normalized_session_cookie = Some(removal_session_cookie(secure));
            continue;
        }

        match state.sessions.get(cookie.value()).await {
            Ok(Some(session)) => {
                normalized_session_cookie = Some(persistent_session_cookie(&session, secure));
            }
            Ok(None) => {
                tracing::error!(
                    event = "auth.middleware.session_cookie_without_session",
                    "Refusing to emit a session cookie without a matching server session"
                );
                failure.get_or_insert(SessionCookieNormalizationFailure::MissingServerSession);
            }
            Err(error) => {
                tracing::error!(
                    event = "auth.middleware.session_backend_failed",
                    operation = "normalize_set_cookie",
                    error = %error,
                    "Session backend operation failed while normalizing a response cookie"
                );
                failure = Some(SessionCookieNormalizationFailure::BackendUnavailable);
            }
        }
    }

    if failure.is_some() {
        append_cookie(response, removal_session_cookie(secure));
    } else if let Some(cookie) = normalized_session_cookie {
        append_cookie(response, cookie);
    }

    match failure {
        Some(failure) => Err(failure),
        None => Ok(saw_session_cookie),
    }
}

fn unavailable_response() -> Response {
    StatusCode::SERVICE_UNAVAILABLE.into_response()
}

pub async fn auth_middleware(
    State(state): State<ApiState>,
    jar: CookieJar,
    mut request: Request<Body>,
    next: Next,
) -> Response {
    let mut ctx = AuthContext {
        authenticated: false,
        account_id: None,
        device_id: None,
        role: Role::Gast,
        expires_at: None,
    };
    let mut session_id_to_touch = None;
    let mut clear_session_cookie = false;

    if let Some(cookie) = jar.get(SESSION_COOKIE_NAME) {
        let session = match state.sessions.get(cookie.value()).await {
            Ok(session) => session,
            Err(error) => {
                tracing::error!(
                    event = "auth.middleware.session_backend_failed",
                    operation = "get",
                    error = %error,
                    "Session backend operation failed during auth middleware"
                );
                return unavailable_response();
            }
        };

        if let Some(session) = session {
            // Session I/O must run outside the held PostgreSQL visibility
            // fence. Read this account's canonical status and role directly,
            // rather than reloading the entire domain cache before session I/O:
            // a full reload can wait on the same node/cache locks that writers
            // need to complete. The inner projection middleware subsequently
            // validates the authenticated account under the current generation.
            let account_state = if state.config.domain_read_source == DomainReadSource::Postgres {
                if let Some(pool) = state.db_pool.as_ref() {
                    match sqlx::query_as::<_, (bool, String)>(
                        "SELECT disabled, role FROM domain_accounts WHERE id = $1",
                    )
                    .bind(&session.account_id)
                    .fetch_optional(pool)
                    .await
                    {
                        Ok(row) => {
                            row.map(|(disabled, role)| (disabled, Role::from_str_lossy(&role)))
                        }
                        Err(error) => {
                            tracing::error!(%error, "cannot read canonical account during authentication");
                            return unavailable_response();
                        }
                    }
                } else {
                    // Synthetic API contract tests construct a PostgreSQL
                    // read-source state without a pool. Production rejects
                    // this configuration at startup.
                    let accounts = state.accounts.read().await;
                    accounts
                        .get(&session.account_id)
                        .map(|internal| (internal.public.disabled, internal.role.clone()))
                }
            } else {
                let accounts = state.accounts.read().await;
                accounts
                    .get(&session.account_id)
                    .map(|internal| (internal.public.disabled, internal.role.clone()))
            };

            match account_state {
                Some((false, role)) => {
                    ctx.authenticated = true;
                    ctx.account_id = Some(session.account_id.clone());
                    ctx.device_id = Some(session.device_id.clone());
                    ctx.role = role;
                    ctx.expires_at = Some(session.expires_at);
                    session_id_to_touch = Some(session.id);
                }
                Some((true, _)) | None => {
                    clear_session_cookie = true;
                    if let Err(error) = state.sessions.delete(&session.id).await {
                        tracing::error!(
                            event = "auth.middleware.session_backend_failed",
                            operation = "delete_inactive_account_session",
                            error = %error,
                            "Failed to delete a session for a missing or disabled account"
                        );
                        return unavailable_response();
                    }
                }
            }
        } else {
            clear_session_cookie = true;
        }
    }

    if let Some(session_id) = session_id_to_touch {
        if let Err(error) = state.sessions.touch(&session_id).await {
            tracing::error!(
                event = "auth.middleware.session_backend_failed",
                operation = "touch",
                error = %error,
                "Session backend operation failed during auth middleware"
            );
            return unavailable_response();
        }
    }

    request.extensions_mut().insert(ctx);
    let mut response = next.run(request).await;
    let saw_session_cookie = match normalize_session_cookie_headers(&state, &mut response).await {
        Ok(saw_session_cookie) => saw_session_cookie,
        Err(failure) => {
            tracing::error!(
                event = "auth.middleware.session_cookie_normalization_failed",
                failure = ?failure,
                "Failed to align browser cookie expiry with the server session"
            );
            *response.status_mut() = failure.status();
            response.headers_mut().remove(CONTENT_LENGTH);
            *response.body_mut() = Body::empty();
            return response;
        }
    };

    if clear_session_cookie && !saw_session_cookie {
        append_cookie(
            &mut response,
            removal_session_cookie(state.config.auth_cookie_secure),
        );
    }

    response
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Duration;

    fn session(expiry: DateTime<Utc>) -> Session {
        Session {
            id: "session-id".to_string(),
            account_id: "account-id".to_string(),
            device_id: "device-id".to_string(),
            created_at: Utc::now(),
            last_active: Utc::now(),
            expires_at: expiry,
            cookie_max_age_seconds: expiry.signed_duration_since(Utc::now()).num_seconds(),
        }
    }

    #[test]
    fn persistent_cookie_matches_server_expiry_and_security_contract() {
        let expiry = Utc::now() + Duration::days(30);
        let cookie = persistent_session_cookie(&session(expiry), true);

        assert_eq!(cookie.name(), SESSION_COOKIE_NAME);
        assert_eq!(cookie.value(), "session-id");
        assert_eq!(cookie.path(), Some("/"));
        assert_eq!(cookie.http_only(), Some(true));
        assert_eq!(cookie.secure(), Some(true));
        assert_eq!(cookie.same_site(), Some(SameSite::Lax));
        let max_age = cookie.max_age().unwrap().whole_seconds();
        assert!(max_age > 0);
        assert!(max_age <= Duration::days(30).num_seconds());
        assert!(max_age >= Duration::days(30).num_seconds() - 301);
        assert_eq!(
            cookie.expires_datetime().unwrap().unix_timestamp(),
            (expiry - Duration::seconds(SESSION_COOKIE_EXPIRY_SAFETY_SECONDS)).timestamp()
        );
    }

    #[test]
    fn removal_cookie_is_immediately_expired_with_same_scope() {
        let cookie = removal_session_cookie(false);

        assert_eq!(cookie.name(), SESSION_COOKIE_NAME);
        assert_eq!(cookie.value(), "");
        assert_eq!(cookie.path(), Some("/"));
        assert_eq!(cookie.http_only(), Some(true));
        assert_eq!(cookie.secure(), Some(false));
        assert_eq!(cookie.same_site(), Some(SameSite::Lax));
        assert_eq!(cookie.max_age().unwrap().whole_seconds(), 0);
        assert_eq!(
            cookie.expires_datetime().unwrap().unix_timestamp(),
            OffsetDateTime::UNIX_EPOCH.unix_timestamp()
        );
    }

    #[test]
    fn expires_only_cookie_is_recognized_as_removal() {
        let cookie = Cookie::build((SESSION_COOKIE_NAME, "old-session"))
            .path("/")
            .http_only(true)
            .same_site(SameSite::Lax)
            .secure(false)
            .expires(OffsetDateTime::UNIX_EPOCH)
            .build();

        assert!(is_removal_cookie(&cookie));
    }

    #[test]
    fn omitted_secure_attribute_matches_local_http_scope() {
        let cookie = Cookie::build((SESSION_COOKIE_NAME, "session-id"))
            .path("/")
            .http_only(true)
            .same_site(SameSite::Lax)
            .secure(false)
            .build();
        let parsed = Cookie::parse(cookie.to_string()).expect("cookie must parse");

        assert_eq!(parsed.secure(), None);
        assert!(has_supported_session_cookie_scope(&parsed, false));
    }

    #[test]
    fn non_canonical_domain_scope_is_rejected() {
        let cookie = Cookie::build((SESSION_COOKIE_NAME, "session-id"))
            .domain("example.test")
            .path("/")
            .http_only(true)
            .same_site(SameSite::Lax)
            .secure(true)
            .build();

        assert!(!has_supported_session_cookie_scope(&cookie, true));
    }
}
