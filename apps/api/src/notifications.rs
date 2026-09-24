//! Optional, privacy-preserving Web Push delivery for committed private messages.
//!
//! Private messages remain canonical in `domain_messages` and visible in the
//! `/nachrichten` inbox. This module consumes the existing transactional outbox,
//! creates idempotent per-subscription delivery receipts, and sends only a
//! neutral encrypted hint. Message content and author identity never enter the
//! push payload or the delivery tables.

use std::{
    env,
    net::IpAddr,
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use anyhow::{anyhow, Context};
use async_nats::{
    jetstream::consumer::{self, PullConsumer},
    Client,
};
use axum::{
    extract::{Path, State},
    http::{
        header::{ORIGIN, REFERER},
        HeaderMap, StatusCode,
    },
    response::{IntoResponse, Response},
    Extension, Json,
};
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use futures_util::StreamExt;
use p256::{
    ecdsa::{signature::Signer, Signature, SigningKey},
    PublicKey,
};
use serde::{Deserialize, Serialize};
use serde_json::json;
use sha2::{Digest, Sha256};
use sqlx::PgPool;
use url::Url;
use uuid::Uuid;
use web_push_native::{Auth, WebPushBuilder};

use crate::{middleware::auth::AuthContext, outbox, state::ApiState};

const PUSH_SUBJECT: &str = "weltgewebe.domain.message.created";
const PUSH_CONSUMER_NAME: &str = "weltgewebe-web-push-direct-message-v1";
const CLAIM_BATCH_SIZE: i64 = 16;
const CLAIM_LEASE_SECONDS: i32 = 30;
const MAX_DELIVERY_ATTEMPTS: i32 = 8;
const MAX_ACTIVE_PUSH_SUBSCRIPTIONS_PER_ACCOUNT: i64 = 20;
const PUSH_ENDPOINT_HASH_HEADER: &str = "x-weltgewebe-push-endpoint-hash";
const PUSH_TTL: Duration = Duration::from_secs(6 * 60 * 60);

#[derive(Clone)]
pub struct WebPushService {
    client: reqwest::Client,
    vapid_key: Arc<SigningKey>,
    public_key: String,
    contact: String,
    allowed_host_suffixes: Arc<Vec<String>>,
}

impl WebPushService {
    pub fn from_env() -> anyhow::Result<Option<Self>> {
        let private_key = non_empty_env("WEB_PUSH_VAPID_PRIVATE_KEY");
        let contact = non_empty_env("WEB_PUSH_VAPID_CONTACT");
        let allowed_hosts = non_empty_env("WEB_PUSH_ALLOWED_HOST_SUFFIXES");

        if private_key.is_none() && contact.is_none() && allowed_hosts.is_none() {
            return Ok(None);
        }

        let private_key = private_key.ok_or_else(|| {
            anyhow!("WEB_PUSH_VAPID_PRIVATE_KEY is required when Web Push is configured")
        })?;
        let contact = contact.ok_or_else(|| {
            anyhow!("WEB_PUSH_VAPID_CONTACT is required when Web Push is configured")
        })?;
        let allowed_hosts = allowed_hosts.ok_or_else(|| {
            anyhow!("WEB_PUSH_ALLOWED_HOST_SUFFIXES is required when Web Push is configured")
        })?;

        Self::from_parts(&private_key, &contact, &allowed_hosts).map(Some)
    }

    fn from_parts(private_key: &str, contact: &str, allowed_hosts: &str) -> anyhow::Result<Self> {
        let private_key_bytes = URL_SAFE_NO_PAD
            .decode(private_key.trim())
            .context("WEB_PUSH_VAPID_PRIVATE_KEY must be unpadded base64url")?;
        if private_key_bytes.len() != 32 {
            return Err(anyhow!(
                "WEB_PUSH_VAPID_PRIVATE_KEY must decode to exactly 32 bytes"
            ));
        }
        let vapid_key = SigningKey::from_slice(&private_key_bytes)
            .context("WEB_PUSH_VAPID_PRIVATE_KEY is not a valid P-256 private key")?;
        validate_vapid_contact(contact)?;

        let allowed_host_suffixes: Vec<String> = allowed_hosts
            .split(',')
            .map(normalize_host_suffix)
            .collect::<anyhow::Result<Vec<_>>>()?;
        if allowed_host_suffixes.is_empty() {
            return Err(anyhow!(
                "WEB_PUSH_ALLOWED_HOST_SUFFIXES must contain at least one HTTPS push provider host"
            ));
        }

        let public_key =
            URL_SAFE_NO_PAD.encode(vapid_key.verifying_key().to_encoded_point(false).as_bytes());
        let client = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .connect_timeout(Duration::from_secs(5))
            .timeout(Duration::from_secs(15))
            .build()
            .context("failed to build Web Push HTTP client")?;

        Ok(Self {
            client,
            vapid_key: Arc::new(vapid_key),
            public_key,
            contact: contact.trim().to_string(),
            allowed_host_suffixes: Arc::new(allowed_host_suffixes),
        })
    }

    pub fn public_key(&self) -> &str {
        &self.public_key
    }

    fn validate_endpoint(&self, endpoint: &str) -> anyhow::Result<Url> {
        if !(16..=2048).contains(&endpoint.len()) {
            return Err(anyhow!(
                "push endpoint length is outside the accepted range"
            ));
        }
        let url = Url::parse(endpoint).context("push endpoint is not a valid URL")?;
        if url.scheme() != "https" {
            return Err(anyhow!("push endpoint must use HTTPS"));
        }
        if !url.username().is_empty() || url.password().is_some() || url.fragment().is_some() {
            return Err(anyhow!(
                "push endpoint must not contain credentials or a fragment"
            ));
        }
        if url.port().is_some_and(|port| port != 443) {
            return Err(anyhow!("push endpoint may only use the HTTPS default port"));
        }
        let host = url
            .host_str()
            .ok_or_else(|| anyhow!("push endpoint has no host"))?
            .trim_end_matches('.')
            .to_ascii_lowercase();
        if host.parse::<IpAddr>().is_ok() {
            return Err(anyhow!("push endpoint must use an allow-listed DNS host"));
        }
        let allowed = self.allowed_host_suffixes.iter().any(|suffix| {
            host == *suffix
                || host
                    .strip_suffix(suffix)
                    .is_some_and(|prefix| prefix.ends_with('.'))
        });
        if !allowed {
            return Err(anyhow!("push endpoint host is not allow-listed"));
        }
        Ok(url)
    }

    fn validate_subscription(
        &self,
        endpoint: &str,
        p256dh: &str,
        auth: &str,
    ) -> anyhow::Result<()> {
        self.validate_endpoint(endpoint)?;
        decode_subscription_keys(p256dh, auth)?;
        Ok(())
    }

    async fn send_private_message_hint(
        &self,
        endpoint: &str,
        p256dh: &str,
        auth: &str,
        conversation_id: &str,
    ) -> anyhow::Result<PushSendOutcome> {
        let endpoint = self.validate_endpoint(endpoint)?;
        let (user_public_key, auth_secret) = decode_subscription_keys(p256dh, auth)?;
        let payload = private_message_payload(conversation_id)?;
        let builder = WebPushBuilder::new(
            endpoint
                .as_str()
                .parse()
                .context("push endpoint URI is invalid")?,
            user_public_key,
            auth_secret,
        )
        .with_valid_duration(PUSH_TTL);
        let request = builder
            .build(payload)
            .context("failed to encrypt Web Push payload")?;
        let authorization = vapid_authorization(
            &self.vapid_key,
            &self.public_key,
            &endpoint,
            &self.contact,
            PUSH_TTL,
        )?;
        let (parts, body) = request.into_parts();
        let mut outgoing = self
            .client
            .request(reqwest::Method::POST, parts.uri.to_string())
            .header(reqwest::header::AUTHORIZATION, authorization);
        for (name, value) in &parts.headers {
            outgoing = outgoing.header(name.as_str(), value.as_bytes());
        }
        let response = outgoing
            .body(body)
            .send()
            .await
            .context("Web Push provider request failed")?;
        let status = response.status();
        if status.is_success() {
            return Ok(PushSendOutcome::Sent);
        }
        if status == reqwest::StatusCode::NOT_FOUND || status == reqwest::StatusCode::GONE {
            return Ok(PushSendOutcome::Gone(format!(
                "push provider returned HTTP {}",
                status.as_u16()
            )));
        }
        if status == reqwest::StatusCode::TOO_MANY_REQUESTS || status.is_server_error() {
            return Ok(PushSendOutcome::Retry(format!(
                "push provider returned HTTP {}",
                status.as_u16()
            )));
        }
        Ok(PushSendOutcome::Quarantined(format!(
            "push provider rejected request with HTTP {}",
            status.as_u16()
        )))
    }
}

fn vapid_authorization(
    signing_key: &SigningKey,
    public_key: &str,
    endpoint: &Url,
    contact: &str,
    validity: Duration,
) -> anyhow::Result<String> {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .context("system clock is before the Unix epoch")?
        .as_secs();
    let validity = validity.as_secs().min(24 * 60 * 60);
    let expiration = now
        .checked_add(validity)
        .ok_or_else(|| anyhow!("VAPID expiration overflow"))?;
    let header = URL_SAFE_NO_PAD.encode(
        serde_json::to_vec(&json!({ "typ": "JWT", "alg": "ES256" }))
            .context("failed to encode VAPID header")?,
    );
    let claims = URL_SAFE_NO_PAD.encode(
        serde_json::to_vec(&json!({
            "aud": endpoint.origin().ascii_serialization(),
            "exp": expiration,
            "sub": contact,
        }))
        .context("failed to encode VAPID claims")?,
    );
    let signing_input = format!("{header}.{claims}");
    let signature: Signature = signing_key.sign(signing_input.as_bytes());
    let signature = URL_SAFE_NO_PAD.encode(signature.to_bytes());
    Ok(format!(
        "vapid t={signing_input}.{signature}, k={public_key}"
    ))
}

fn non_empty_env(name: &str) -> Option<String> {
    env::var(name)
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}

fn validate_vapid_contact(contact: &str) -> anyhow::Result<()> {
    let parsed = Url::parse(contact.trim()).context("WEB_PUSH_VAPID_CONTACT is not a URL")?;
    match parsed.scheme() {
        "mailto" if !parsed.path().trim().is_empty() => Ok(()),
        "https" if parsed.host_str().is_some() => Ok(()),
        _ => Err(anyhow!(
            "WEB_PUSH_VAPID_CONTACT must be a mailto: or HTTPS contact URL"
        )),
    }
}

fn normalize_host_suffix(value: &str) -> anyhow::Result<String> {
    let value = value
        .trim()
        .trim_start_matches('.')
        .trim_end_matches('.')
        .to_ascii_lowercase();
    if value.is_empty()
        || !value.contains('.')
        || value.contains('*')
        || value.contains('/')
        || value.contains(':')
        || value.parse::<IpAddr>().is_ok()
    {
        return Err(anyhow!(
            "WEB_PUSH_ALLOWED_HOST_SUFFIXES contains an invalid DNS suffix"
        ));
    }
    if value.split('.').any(|label| {
        label.is_empty()
            || label.starts_with('-')
            || label.ends_with('-')
            || !label
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
    }) {
        return Err(anyhow!(
            "WEB_PUSH_ALLOWED_HOST_SUFFIXES contains an invalid DNS label"
        ));
    }
    Ok(value)
}

fn decode_subscription_keys(p256dh: &str, auth: &str) -> anyhow::Result<(PublicKey, Auth)> {
    if p256dh.len() > 128 || auth.len() > 64 {
        return Err(anyhow!("push subscription key is too long"));
    }
    let p256dh = URL_SAFE_NO_PAD
        .decode(p256dh)
        .context("p256dh must be unpadded base64url")?;
    let auth = URL_SAFE_NO_PAD
        .decode(auth)
        .context("auth must be unpadded base64url")?;
    if auth.len() != 16 {
        return Err(anyhow!("push auth secret must decode to exactly 16 bytes"));
    }
    let public_key = PublicKey::from_sec1_bytes(&p256dh)
        .map_err(|_| anyhow!("p256dh is not a valid P-256 public key"))?;
    let mut auth_secret = Auth::default();
    auth_secret.copy_from_slice(&auth);
    Ok((public_key, auth_secret))
}

fn private_message_payload(conversation_id: &str) -> anyhow::Result<Vec<u8>> {
    let conversation_id = Uuid::parse_str(conversation_id)
        .context("private-message push target is not a UUID")?
        .to_string();
    serde_json::to_vec(&json!({
        "version": 1,
        "kind": "direct_message",
        "title": "commonThing",
        "body": "Neue private Nachricht",
        "url": format!("/nachrichten?id={conversation_id}"),
        "tag": format!("direct-message:{conversation_id}")
    }))
    .context("failed to encode Web Push payload")
}

#[derive(Debug)]
enum PushSendOutcome {
    Sent,
    Gone(String),
    Retry(String),
    Quarantined(String),
}

#[derive(Debug)]
pub struct NotificationApiError {
    status: StatusCode,
    code: &'static str,
    message: &'static str,
}

impl NotificationApiError {
    fn new(status: StatusCode, code: &'static str, message: &'static str) -> Self {
        Self {
            status,
            code,
            message,
        }
    }
}

impl IntoResponse for NotificationApiError {
    fn into_response(self) -> Response {
        (
            self.status,
            Json(json!({ "code": self.code, "message": self.message })),
        )
            .into_response()
    }
}

fn account_id(auth: &AuthContext) -> Result<&str, NotificationApiError> {
    auth.account_id.as_deref().ok_or_else(|| {
        NotificationApiError::new(
            StatusCode::UNAUTHORIZED,
            "authentication_required",
            "an authenticated account is required",
        )
    })
}

fn database_pool(state: &ApiState) -> Result<&PgPool, NotificationApiError> {
    state.db_pool.as_ref().ok_or_else(|| {
        NotificationApiError::new(
            StatusCode::SERVICE_UNAVAILABLE,
            "notification_store_unavailable",
            "notification preferences require the canonical PostgreSQL store",
        )
    })
}

fn push_service(state: &ApiState) -> Result<&WebPushService, NotificationApiError> {
    if state.nats_client.is_none() {
        return Err(NotificationApiError::new(
            StatusCode::SERVICE_UNAVAILABLE,
            "push_delivery_unavailable",
            "Web Push delivery is not connected to the transactional event stream",
        ));
    }
    state.web_push.as_deref().ok_or_else(|| {
        NotificationApiError::new(
            StatusCode::SERVICE_UNAVAILABLE,
            "push_not_configured",
            "Web Push is not configured on this commonThing cell",
        )
    })
}

fn push_origin_error() -> NotificationApiError {
    NotificationApiError::new(
        StatusCode::FORBIDDEN,
        "non_canonical_push_origin",
        "Web Push subscriptions must be registered from the canonical application origin",
    )
}

fn http_origin(value: &str) -> Option<String> {
    let parsed = Url::parse(value).ok()?;
    if !matches!(parsed.scheme(), "http" | "https") {
        return None;
    }
    Some(parsed.origin().ascii_serialization())
}

fn require_canonical_push_origin(
    app_base_url: Option<&str>,
    headers: &HeaderMap,
) -> Result<(), NotificationApiError> {
    let Some(app_base_url) = app_base_url else {
        return Ok(());
    };
    let canonical_origin = http_origin(app_base_url).ok_or_else(|| {
        tracing::error!(
            app_base_url,
            "invalid APP_BASE_URL while validating Web Push origin"
        );
        NotificationApiError::new(
            StatusCode::SERVICE_UNAVAILABLE,
            "push_origin_configuration_invalid",
            "Web Push origin configuration is invalid",
        )
    })?;
    let request_origin = if let Some(origin) = headers.get(ORIGIN) {
        origin.to_str().ok().and_then(http_origin)
    } else {
        headers
            .get(REFERER)
            .and_then(|referer| referer.to_str().ok())
            .and_then(http_origin)
    };

    if request_origin.as_deref() != Some(canonical_origin.as_str()) {
        tracing::warn!(
            ?request_origin,
            %canonical_origin,
            "rejected Web Push registration from non-canonical origin"
        );
        return Err(push_origin_error());
    }
    Ok(())
}

fn database_error(context: &'static str, error: sqlx::Error) -> NotificationApiError {
    tracing::error!(%error, context, "notification database operation failed");
    NotificationApiError::new(
        StatusCode::INTERNAL_SERVER_ERROR,
        "notification_database_error",
        "the notification store could not complete the request",
    )
}

fn current_endpoint_hash(headers: &HeaderMap) -> Result<Option<String>, NotificationApiError> {
    let Some(value) = headers.get(PUSH_ENDPOINT_HASH_HEADER) else {
        return Ok(None);
    };
    let value = value.to_str().map_err(|_| {
        NotificationApiError::new(
            StatusCode::BAD_REQUEST,
            "invalid_push_device_marker",
            "the current push device marker is invalid",
        )
    })?;
    if value.len() != 64
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err(NotificationApiError::new(
            StatusCode::BAD_REQUEST,
            "invalid_push_device_marker",
            "the current push device marker is invalid",
        ));
    }
    Ok(Some(value.to_string()))
}

#[derive(Debug, Serialize)]
pub struct NotificationPreferencesView {
    direct_messages_push: bool,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UpdateNotificationPreferencesRequest {
    direct_messages_push: bool,
}

pub async fn get_notification_preferences(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
) -> Result<Json<NotificationPreferencesView>, NotificationApiError> {
    let account_id = account_id(&auth)?;
    let pool = database_pool(&state)?;
    let enabled: Option<bool> = sqlx::query_scalar(
        "SELECT direct_messages_push FROM notification_preferences WHERE account_id = $1",
    )
    .bind(account_id)
    .fetch_optional(pool)
    .await
    .map_err(|error| database_error("load notification preferences", error))?;
    Ok(Json(NotificationPreferencesView {
        direct_messages_push: enabled.unwrap_or(false),
    }))
}

pub async fn update_notification_preferences(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
    Json(request): Json<UpdateNotificationPreferencesRequest>,
) -> Result<Json<NotificationPreferencesView>, NotificationApiError> {
    let account_id = account_id(&auth)?;
    let pool = database_pool(&state)?;
    let mut tx = pool
        .begin()
        .await
        .map_err(|error| database_error("begin notification preference update", error))?;
    let enabled: bool = sqlx::query_scalar(
        "INSERT INTO notification_preferences (account_id, direct_messages_push) \
         VALUES ($1, $2) \
         ON CONFLICT (account_id) DO UPDATE \
         SET direct_messages_push = EXCLUDED.direct_messages_push, updated_at = NOW() \
         RETURNING direct_messages_push",
    )
    .bind(account_id)
    .bind(request.direct_messages_push)
    .fetch_one(&mut *tx)
    .await
    .map_err(|error| database_error("update notification preferences", error))?;

    if !enabled {
        sqlx::query(
            "UPDATE web_push_deliveries \
             SET status = 'cancelled', claimed_until = NULL, last_error = 'disabled by account' \
             WHERE subscription_id IN ( \
                 SELECT id FROM web_push_subscriptions WHERE account_id = $1 \
             ) AND status IN ('pending', 'retry', 'sending')",
        )
        .bind(account_id)
        .execute(&mut *tx)
        .await
        .map_err(|error| database_error("cancel disabled push deliveries", error))?;
    }

    tx.commit()
        .await
        .map_err(|error| database_error("commit notification preference update", error))?;
    Ok(Json(NotificationPreferencesView {
        direct_messages_push: enabled,
    }))
}

#[derive(Debug, Serialize)]
pub struct PushConfigView {
    enabled: bool,
    application_server_key: Option<String>,
}

pub async fn get_push_config(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
) -> Result<Json<PushConfigView>, NotificationApiError> {
    account_id(&auth)?;
    let enabled =
        state.db_pool.is_some() && state.nats_client.is_some() && state.web_push.is_some();
    Ok(Json(PushConfigView {
        enabled,
        application_server_key: if enabled {
            state
                .web_push
                .as_ref()
                .map(|service| service.public_key().to_string())
        } else {
            None
        },
    }))
}

#[derive(Debug, Serialize)]
pub struct ManagedPushSubscriptionView {
    id: String,
    created_at: chrono::DateTime<chrono::Utc>,
    updated_at: chrono::DateTime<chrono::Utc>,
    current: bool,
}

#[derive(Debug, Serialize)]
pub struct PushSubscriptionsView {
    items: Vec<ManagedPushSubscriptionView>,
    limit: i64,
}

pub async fn list_push_subscriptions(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
    headers: HeaderMap,
) -> Result<Json<PushSubscriptionsView>, NotificationApiError> {
    let account_id = account_id(&auth)?;
    let pool = database_pool(&state)?;
    let current_hash = current_endpoint_hash(&headers)?;
    let rows: Vec<(
        String,
        chrono::DateTime<chrono::Utc>,
        chrono::DateTime<chrono::Utc>,
        String,
    )> = sqlx::query_as(
        "SELECT id::text, created_at, updated_at, endpoint_hash \
             FROM web_push_subscriptions \
             WHERE account_id = $1 AND disabled_at IS NULL \
             ORDER BY created_at DESC, id DESC",
    )
    .bind(account_id)
    .fetch_all(pool)
    .await
    .map_err(|error| database_error("list active push subscriptions", error))?;
    let items = rows
        .into_iter()
        .map(
            |(id, created_at, updated_at, endpoint_hash)| ManagedPushSubscriptionView {
                id,
                created_at,
                updated_at,
                current: current_hash.as_deref() == Some(endpoint_hash.as_str()),
            },
        )
        .collect();
    Ok(Json(PushSubscriptionsView {
        items,
        limit: MAX_ACTIVE_PUSH_SUBSCRIPTIONS_PER_ACCOUNT,
    }))
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RegisterPushSubscriptionRequest {
    endpoint: String,
    p256dh: String,
    auth: String,
}

#[derive(Debug, Serialize)]
pub struct PushSubscriptionView {
    id: String,
}

pub async fn register_push_subscription(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
    headers: HeaderMap,
    Json(request): Json<RegisterPushSubscriptionRequest>,
) -> Result<(StatusCode, Json<PushSubscriptionView>), NotificationApiError> {
    require_canonical_push_origin(state.config.app_base_url.as_deref(), &headers)?;
    let account_id = account_id(&auth)?;
    let pool = database_pool(&state)?;
    let service = push_service(&state)?;
    service
        .validate_subscription(&request.endpoint, &request.p256dh, &request.auth)
        .map_err(|error| {
            tracing::debug!(%error, "rejected invalid browser push subscription");
            NotificationApiError::new(
                StatusCode::BAD_REQUEST,
                "invalid_push_subscription",
                "the browser push subscription is invalid or uses an unapproved provider",
            )
        })?;

    let endpoint_hash = hex::encode(Sha256::digest(request.endpoint.as_bytes()));
    let mut tx = pool
        .begin()
        .await
        .map_err(|error| database_error("begin push subscription update", error))?;
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))")
        .bind(format!("web-push-account:{account_id}"))
        .fetch_one(&mut *tx)
        .await
        .map_err(|error| database_error("lock push account", error))?;
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))")
        .bind(format!("web-push-endpoint:{endpoint_hash}"))
        .fetch_one(&mut *tx)
        .await
        .map_err(|error| database_error("lock push endpoint", error))?;
    let active_subscriptions: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM web_push_subscriptions \
         WHERE account_id = $1 AND disabled_at IS NULL AND endpoint_hash <> $2",
    )
    .bind(account_id)
    .bind(&endpoint_hash)
    .fetch_one(&mut *tx)
    .await
    .map_err(|error| database_error("count active push subscriptions", error))?;
    if active_subscriptions >= MAX_ACTIVE_PUSH_SUBSCRIPTIONS_PER_ACCOUNT {
        return Err(NotificationApiError::new(
            StatusCode::TOO_MANY_REQUESTS,
            "push_subscription_limit_reached",
            "the account has reached the active Web Push device limit",
        ));
    }
    sqlx::query(
        "UPDATE web_push_deliveries \
         SET status = 'gone', claimed_until = NULL, last_error = 'subscription refreshed' \
         WHERE subscription_id IN ( \
             SELECT id FROM web_push_subscriptions \
             WHERE account_id = $2 AND endpoint_hash = $1 \
         ) AND status IN ('pending', 'retry', 'sending')",
    )
    .bind(&endpoint_hash)
    .bind(account_id)
    .execute(&mut *tx)
    .await
    .map_err(|error| database_error("retire previous push deliveries", error))?;

    let id = Uuid::new_v4().to_string();
    let stored_id: Option<String> = sqlx::query_scalar(
        "INSERT INTO web_push_subscriptions ( \
             id, account_id, endpoint, endpoint_hash, p256dh, auth_secret \
         ) VALUES ($1::uuid, $2, $3, $4, $5, $6) \
         ON CONFLICT (endpoint_hash) DO UPDATE \
         SET endpoint = EXCLUDED.endpoint, p256dh = EXCLUDED.p256dh, \
             auth_secret = EXCLUDED.auth_secret, \
             disabled_at = NULL, last_error = NULL, updated_at = NOW() \
         WHERE web_push_subscriptions.account_id = EXCLUDED.account_id \
         RETURNING id::text",
    )
    .bind(id)
    .bind(account_id)
    .bind(&request.endpoint)
    .bind(&endpoint_hash)
    .bind(&request.p256dh)
    .bind(&request.auth)
    .fetch_optional(&mut *tx)
    .await
    .map_err(|error| database_error("store push subscription", error))?;
    let Some(stored_id) = stored_id else {
        return Err(NotificationApiError::new(
            StatusCode::CONFLICT,
            "push_subscription_conflict",
            "the browser push subscription could not be registered for this account",
        ));
    };
    tx.commit()
        .await
        .map_err(|error| database_error("commit push subscription", error))?;
    Ok((
        StatusCode::CREATED,
        Json(PushSubscriptionView { id: stored_id }),
    ))
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DeletePushSubscriptionRequest {
    endpoint: String,
}

pub async fn delete_push_subscription(
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
    Json(request): Json<DeletePushSubscriptionRequest>,
) -> Result<StatusCode, NotificationApiError> {
    let account_id = account_id(&auth)?;
    let pool = database_pool(&state)?;
    if !(16..=2048).contains(&request.endpoint.len()) {
        return Err(NotificationApiError::new(
            StatusCode::BAD_REQUEST,
            "invalid_push_subscription",
            "the browser push subscription is invalid",
        ));
    }
    let endpoint_hash = hex::encode(Sha256::digest(request.endpoint.as_bytes()));
    let mut tx = pool
        .begin()
        .await
        .map_err(|error| database_error("begin push subscription removal", error))?;
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))")
        .bind(format!("web-push-account:{account_id}"))
        .fetch_one(&mut *tx)
        .await
        .map_err(|error| database_error("lock push account", error))?;
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))")
        .bind(format!("web-push-endpoint:{endpoint_hash}"))
        .fetch_one(&mut *tx)
        .await
        .map_err(|error| database_error("lock push endpoint", error))?;
    sqlx::query(
        "UPDATE web_push_deliveries \
         SET status = 'cancelled', claimed_until = NULL, last_error = 'disabled by account' \
         WHERE subscription_id IN ( \
             SELECT id FROM web_push_subscriptions \
             WHERE account_id = $1 AND endpoint_hash = $2 \
         ) AND status IN ('pending', 'retry', 'sending')",
    )
    .bind(account_id)
    .bind(&endpoint_hash)
    .execute(&mut *tx)
    .await
    .map_err(|error| database_error("retire disabled push deliveries", error))?;
    sqlx::query(
        "UPDATE web_push_subscriptions \
         SET disabled_at = COALESCE(disabled_at, NOW()), updated_at = NOW(), last_error = NULL \
         WHERE account_id = $1 AND endpoint_hash = $2",
    )
    .bind(account_id)
    .bind(endpoint_hash)
    .execute(&mut *tx)
    .await
    .map_err(|error| database_error("disable push subscription", error))?;
    tx.commit()
        .await
        .map_err(|error| database_error("commit push subscription removal", error))?;
    Ok(StatusCode::NO_CONTENT)
}

pub async fn disable_push_subscription_by_id(
    Path(subscription_id): Path<String>,
    State(state): State<ApiState>,
    Extension(auth): Extension<AuthContext>,
    headers: HeaderMap,
) -> Result<StatusCode, NotificationApiError> {
    let account_id = account_id(&auth)?;
    let pool = database_pool(&state)?;
    let current_hash = current_endpoint_hash(&headers)?;
    let Ok(subscription_id) = Uuid::parse_str(&subscription_id) else {
        return Ok(StatusCode::NO_CONTENT);
    };
    let mut tx = pool
        .begin()
        .await
        .map_err(|error| database_error("begin managed push subscription removal", error))?;
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))")
        .bind(format!("web-push-account:{account_id}"))
        .fetch_one(&mut *tx)
        .await
        .map_err(|error| database_error("lock push account", error))?;
    let endpoint_hash: Option<String> = sqlx::query_scalar(
        "SELECT endpoint_hash FROM web_push_subscriptions \
         WHERE account_id = $1 AND id = $2 AND disabled_at IS NULL",
    )
    .bind(account_id)
    .bind(subscription_id)
    .fetch_optional(&mut *tx)
    .await
    .map_err(|error| database_error("find managed push subscription", error))?;
    let Some(endpoint_hash) = endpoint_hash else {
        tx.commit().await.map_err(|error| {
            database_error("commit idempotent push subscription removal", error)
        })?;
        return Ok(StatusCode::NO_CONTENT);
    };
    if current_hash.as_deref() == Some(endpoint_hash.as_str()) {
        return Err(NotificationApiError::new(
            StatusCode::CONFLICT,
            "push_subscription_is_current_device",
            "the current push device must be disabled from its local browser controls",
        ));
    }
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))")
        .bind(format!("web-push-endpoint:{endpoint_hash}"))
        .fetch_one(&mut *tx)
        .await
        .map_err(|error| database_error("lock push endpoint", error))?;
    sqlx::query(
        "UPDATE web_push_deliveries \
         SET status = 'cancelled', claimed_until = NULL, last_error = 'disabled by account' \
         WHERE subscription_id = $2 \
           AND subscription_id IN ( \
               SELECT id FROM web_push_subscriptions WHERE account_id = $1 AND id = $2 \
           ) \
           AND status IN ('pending', 'retry', 'sending')",
    )
    .bind(account_id)
    .bind(subscription_id)
    .execute(&mut *tx)
    .await
    .map_err(|error| database_error("retire managed push deliveries", error))?;
    sqlx::query(
        "UPDATE web_push_subscriptions \
         SET disabled_at = NOW(), updated_at = NOW(), last_error = NULL \
         WHERE account_id = $1 AND id = $2 AND disabled_at IS NULL",
    )
    .bind(account_id)
    .bind(subscription_id)
    .execute(&mut *tx)
    .await
    .map_err(|error| database_error("disable managed push subscription", error))?;
    tx.commit()
        .await
        .map_err(|error| database_error("commit managed push subscription removal", error))?;
    Ok(StatusCode::NO_CONTENT)
}

#[derive(Debug, Deserialize)]
struct PublishedDomainEvent {
    schema_version: u8,
    event_id: i64,
    aggregate_id: String,
    event_type: String,
}

pub async fn start(
    pool: PgPool,
    client: Client,
    service: Arc<WebPushService>,
) -> anyhow::Result<()> {
    let (_context, stream) = outbox::ensure_stream(client).await?;
    let consumer: PullConsumer = stream
        .get_or_create_consumer(
            PUSH_CONSUMER_NAME,
            consumer::pull::Config {
                durable_name: Some(PUSH_CONSUMER_NAME.to_string()),
                description: Some(
                    "Materializes privacy-safe Web Push deliveries for new private messages"
                        .to_string(),
                ),
                deliver_policy: consumer::DeliverPolicy::New,
                ack_policy: consumer::AckPolicy::Explicit,
                ack_wait: Duration::from_secs(30),
                max_deliver: 20,
                filter_subject: PUSH_SUBJECT.to_string(),
                ..Default::default()
            },
        )
        .await
        .context("failed to get or create Web Push message consumer")?;

    tokio::spawn(event_consumer_loop(pool.clone(), consumer));
    tokio::spawn(delivery_loop(pool, service));
    Ok(())
}

async fn event_consumer_loop(pool: PgPool, consumer: PullConsumer) {
    loop {
        let mut messages = match consumer.messages().await {
            Ok(messages) => messages,
            Err(error) => {
                tracing::error!(%error, "Web Push event consumer could not fetch messages");
                tokio::time::sleep(Duration::from_secs(1)).await;
                continue;
            }
        };
        while let Some(message) = messages.next().await {
            let message = match message {
                Ok(message) => message,
                Err(error) => {
                    tracing::warn!(%error, "Web Push event consumer message error");
                    break;
                }
            };
            let envelope: PublishedDomainEvent = match serde_json::from_slice(&message.payload) {
                Ok(envelope) => envelope,
                Err(error) => {
                    tracing::error!(%error, "Malformed Web Push domain event discarded");
                    let _ = message.ack().await;
                    continue;
                }
            };
            if envelope.schema_version != 1 || envelope.event_type != "domain.message.created" {
                let _ = message.ack().await;
                continue;
            }
            let message_id = match Uuid::parse_str(&envelope.aggregate_id) {
                Ok(id) => id.to_string(),
                Err(error) => {
                    tracing::error!(event_id = envelope.event_id, %error, "Web Push event has invalid message id");
                    let _ = message.ack().await;
                    continue;
                }
            };
            match materialize_direct_message_deliveries(&pool, envelope.event_id, &message_id).await
            {
                Ok(created) => {
                    tracing::debug!(
                        event_id = envelope.event_id,
                        created,
                        "Web Push delivery receipts materialized"
                    );
                    if let Err(error) = message.ack().await {
                        tracing::warn!(event_id = envelope.event_id, %error, "Web Push event ack failed");
                    }
                }
                Err(error) => {
                    tracing::error!(event_id = envelope.event_id, %error, "Web Push delivery materialization failed; leaving event unacked");
                }
            }
        }
    }
}

async fn materialize_direct_message_deliveries(
    pool: &PgPool,
    event_id: i64,
    message_id: &str,
) -> anyhow::Result<u64> {
    let result = sqlx::query(
        "INSERT INTO web_push_deliveries ( \
             source_event_id, subscription_id, conversation_id, notification_kind \
         ) \
         SELECT $1, subscription.id, message.conversation_id, 'direct_message' \
         FROM domain_messages AS message \
         JOIN domain_conversations AS conversation \
           ON conversation.id = message.conversation_id \
          AND conversation.conversation_type = 'direct' \
          AND conversation.visibility = 'participants' \
          AND conversation.deleted_at IS NULL \
         JOIN domain_direct_conversation_participants AS recipient \
           ON recipient.conversation_id = conversation.id \
          AND recipient.account_id IS NOT NULL \
          AND recipient.account_id IS DISTINCT FROM message.author_account_id \
         JOIN notification_preferences AS preference \
           ON preference.account_id = recipient.account_id \
          AND preference.direct_messages_push = TRUE \
         JOIN web_push_subscriptions AS subscription \
           ON subscription.account_id = recipient.account_id \
          AND subscription.disabled_at IS NULL \
         WHERE message.id = $2::uuid \
           AND message.deleted_at IS NULL \
         ON CONFLICT DO NOTHING",
    )
    .bind(event_id)
    .bind(message_id)
    .execute(pool)
    .await
    .context("failed to materialize direct-message Web Push deliveries")?;
    Ok(result.rows_affected())
}

type DeliveryRow = (i64, String, String, i32, String, String, String);

async fn claim_deliveries(pool: &PgPool) -> anyhow::Result<Vec<DeliveryRow>> {
    sqlx::query_as(
        "WITH picked AS ( \
             SELECT delivery.source_event_id, delivery.subscription_id \
             FROM web_push_deliveries AS delivery \
             JOIN web_push_subscriptions AS subscription \
               ON subscription.id = delivery.subscription_id \
              AND subscription.disabled_at IS NULL \
             JOIN notification_preferences AS preference \
               ON preference.account_id = subscription.account_id \
              AND preference.direct_messages_push = TRUE \
             WHERE ( \
                 delivery.status IN ('pending', 'retry') \
                 OR (delivery.status = 'sending' AND delivery.claimed_until <= NOW()) \
             ) \
               AND delivery.available_at <= NOW() \
             ORDER BY delivery.available_at, delivery.source_event_id, delivery.subscription_id \
             FOR UPDATE OF delivery SKIP LOCKED \
             LIMIT $1 \
         ), claimed AS ( \
             UPDATE web_push_deliveries AS delivery \
             SET status = 'sending', \
                 attempt_count = delivery.attempt_count + 1, \
                 claimed_until = NOW() + make_interval(secs => $2) \
             FROM picked \
             WHERE delivery.source_event_id = picked.source_event_id \
               AND delivery.subscription_id = picked.subscription_id \
             RETURNING delivery.source_event_id, delivery.subscription_id, \
                       delivery.conversation_id, delivery.attempt_count \
         ) \
         SELECT claimed.source_event_id, claimed.subscription_id::text, \
                claimed.conversation_id::text, claimed.attempt_count, \
                subscription.endpoint, subscription.p256dh, subscription.auth_secret \
         FROM claimed \
         JOIN web_push_subscriptions AS subscription \
           ON subscription.id = claimed.subscription_id",
    )
    .bind(CLAIM_BATCH_SIZE)
    .bind(CLAIM_LEASE_SECONDS)
    .fetch_all(pool)
    .await
    .context("failed to claim Web Push deliveries")
}

async fn delivery_loop(pool: PgPool, service: Arc<WebPushService>) {
    loop {
        match claim_deliveries(&pool).await {
            Ok(deliveries) if deliveries.is_empty() => {
                tokio::time::sleep(Duration::from_millis(500)).await;
            }
            Ok(deliveries) => {
                for delivery in deliveries {
                    let outcome = service
                        .send_private_message_hint(
                            &delivery.4,
                            &delivery.5,
                            &delivery.6,
                            &delivery.2,
                        )
                        .await
                        .unwrap_or_else(|error| PushSendOutcome::Retry(error.to_string()));
                    if let Err(error) =
                        finish_delivery(&pool, delivery.0, &delivery.1, delivery.3, outcome).await
                    {
                        tracing::error!(source_event_id = delivery.0, %error, "Web Push delivery outcome could not be recorded");
                    }
                }
            }
            Err(error) => {
                tracing::error!(%error, "Web Push delivery claim failed");
                tokio::time::sleep(Duration::from_secs(1)).await;
            }
        }
    }
}

async fn finish_delivery(
    pool: &PgPool,
    event_id: i64,
    subscription_id: &str,
    attempt_count: i32,
    outcome: PushSendOutcome,
) -> anyhow::Result<()> {
    match outcome {
        PushSendOutcome::Sent => {
            sqlx::query(
                "UPDATE web_push_deliveries \
                 SET status = 'sent', sent_at = NOW(), claimed_until = NULL, last_error = NULL \
                 WHERE source_event_id = $1 AND subscription_id = $2::uuid \
                   AND status = 'sending'",
            )
            .bind(event_id)
            .bind(subscription_id)
            .execute(pool)
            .await
            .context("failed to mark Web Push delivery sent")?;
        }
        PushSendOutcome::Gone(error) => {
            let error = bounded_error(&error);
            let mut tx = pool.begin().await?;
            sqlx::query(
                "UPDATE web_push_subscriptions \
                 SET disabled_at = COALESCE(disabled_at, NOW()), updated_at = NOW(), last_error = $2 \
                 WHERE id = $1::uuid",
            )
            .bind(subscription_id)
            .bind(&error)
            .execute(&mut *tx)
            .await?;
            sqlx::query(
                "UPDATE web_push_deliveries \
                 SET status = 'gone', claimed_until = NULL, last_error = $3 \
                 WHERE source_event_id = $1 AND subscription_id = $2::uuid \
                   AND status = 'sending'",
            )
            .bind(event_id)
            .bind(subscription_id)
            .bind(&error)
            .execute(&mut *tx)
            .await?;
            tx.commit().await?;
        }
        PushSendOutcome::Retry(error) if attempt_count < MAX_DELIVERY_ATTEMPTS => {
            let error = bounded_error(&error);
            let exponent = u32::try_from(attempt_count.clamp(1, 8)).unwrap_or(8);
            let delay_seconds = (2_i32.pow(exponent) * 2).min(600);
            sqlx::query(
                "UPDATE web_push_deliveries \
                 SET status = 'retry', claimed_until = NULL, \
                     available_at = NOW() + make_interval(secs => $3), last_error = $4 \
                 WHERE source_event_id = $1 AND subscription_id = $2::uuid \
                   AND status = 'sending'",
            )
            .bind(event_id)
            .bind(subscription_id)
            .bind(delay_seconds)
            .bind(error)
            .execute(pool)
            .await
            .context("failed to reschedule Web Push delivery")?;
        }
        PushSendOutcome::Retry(error) | PushSendOutcome::Quarantined(error) => {
            let error = bounded_error(&error);
            sqlx::query(
                "UPDATE web_push_deliveries \
                 SET status = 'quarantined', claimed_until = NULL, last_error = $3 \
                 WHERE source_event_id = $1 AND subscription_id = $2::uuid \
                   AND status = 'sending'",
            )
            .bind(event_id)
            .bind(subscription_id)
            .bind(error)
            .execute(pool)
            .await
            .context("failed to quarantine Web Push delivery")?;
        }
    }
    Ok(())
}

fn bounded_error(error: &str) -> String {
    error.chars().take(2_000).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn service() -> WebPushService {
        let private_key = URL_SAFE_NO_PAD.encode([1_u8; 32]);
        WebPushService::from_parts(
            &private_key,
            "mailto:betrieb@example.invalid",
            "fcm.googleapis.com,push.services.mozilla.com,web.push.apple.com",
        )
        .expect("valid test Web Push service")
    }

    #[test]
    fn canonical_push_origin_guard_rejects_legacy_origin_after_cutover() {
        let mut headers = HeaderMap::new();
        headers.insert(ORIGIN, "https://weltgewebe.net".parse().unwrap());

        let error = require_canonical_push_origin(Some("https://commonthing.net"), &headers)
            .expect_err("legacy origin must be rejected");
        assert_eq!(error.status, StatusCode::FORBIDDEN);
        assert_eq!(error.code, "non_canonical_push_origin");
    }

    #[test]
    fn canonical_push_origin_guard_accepts_canonical_origin_and_referer() {
        let mut headers = HeaderMap::new();
        headers.insert(ORIGIN, "https://commonthing.net:443".parse().unwrap());
        require_canonical_push_origin(Some("https://commonthing.net"), &headers)
            .expect("default HTTPS port must normalize to the canonical origin");

        let mut referer_headers = HeaderMap::new();
        referer_headers.insert(
            REFERER,
            "https://commonthing.net/einstellungen".parse().unwrap(),
        );
        require_canonical_push_origin(Some("https://commonthing.net"), &referer_headers)
            .expect("canonical referer fallback must be accepted");
    }

    #[test]
    fn canonical_push_origin_guard_is_disabled_without_app_base_url() {
        let headers = HeaderMap::new();
        require_canonical_push_origin(None, &headers)
            .expect("development deployments without APP_BASE_URL keep existing behavior");
    }

    #[test]
    fn endpoint_allowlist_rejects_ssrf_shapes() {
        let service = service();
        assert!(service
            .validate_endpoint("https://fcm.googleapis.com/fcm/send/example")
            .is_ok());
        assert!(service
            .validate_endpoint("https://region.push.services.mozilla.com/wpush/v2/example")
            .is_ok());
        for endpoint in [
            "http://fcm.googleapis.com/fcm/send/example",
            "https://127.0.0.1/push/example",
            "https://user@fcm.googleapis.com/fcm/send/example",
            "https://evilfcm.googleapis.com/fcm/send/example",
            "https://example.invalid/push/example",
        ] {
            assert!(
                service.validate_endpoint(endpoint).is_err(),
                "unexpectedly accepted {endpoint}"
            );
        }
    }

    #[test]
    fn private_message_payload_is_neutral_and_deep_links_only_by_conversation() {
        let conversation_id = "11111111-1111-4111-8111-111111111111";
        let payload: serde_json::Value =
            serde_json::from_slice(&private_message_payload(conversation_id).unwrap()).unwrap();
        assert_eq!(payload["kind"], "direct_message");
        assert_eq!(payload["title"], "commonThing");
        assert_eq!(payload["body"], "Neue private Nachricht");
        assert_eq!(
            payload["url"],
            "/nachrichten?id=11111111-1111-4111-8111-111111111111"
        );
        assert!(payload.get("content").is_none());
        assert!(payload.get("author").is_none());
        assert!(payload.get("account_id").is_none());
    }

    #[test]
    fn vapid_header_is_es256_signed_and_bound_to_push_origin() {
        use p256::ecdsa::signature::Verifier;

        let service = service();
        let endpoint = service
            .validate_endpoint("https://fcm.googleapis.com/fcm/send/example")
            .unwrap();
        let authorization = vapid_authorization(
            &service.vapid_key,
            &service.public_key,
            &endpoint,
            &service.contact,
            Duration::from_secs(60),
        )
        .unwrap();
        let (token, key) = authorization
            .strip_prefix("vapid t=")
            .unwrap()
            .split_once(", k=")
            .unwrap();
        assert_eq!(key, service.public_key);

        let segments: Vec<&str> = token.split('.').collect();
        assert_eq!(segments.len(), 3);
        let claims: serde_json::Value =
            serde_json::from_slice(&URL_SAFE_NO_PAD.decode(segments[1]).unwrap()).unwrap();
        assert_eq!(claims["aud"], "https://fcm.googleapis.com");
        assert_eq!(claims["sub"], "mailto:betrieb@example.invalid");
        assert!(claims["exp"].as_u64().is_some());

        let signature =
            Signature::from_slice(&URL_SAFE_NO_PAD.decode(segments[2]).unwrap()).unwrap();
        service
            .vapid_key
            .verifying_key()
            .verify(
                format!("{}.{}", segments[0], segments[1]).as_bytes(),
                &signature,
            )
            .unwrap();
    }

    #[test]
    fn allowed_host_suffixes_are_dns_names_not_patterns() {
        assert_eq!(
            normalize_host_suffix(".Web.Push.Apple.Com.").unwrap(),
            "web.push.apple.com"
        );
        for invalid in [
            "",
            "localhost",
            "*.example.com",
            "https://example.com",
            "127.0.0.1",
        ] {
            assert!(normalize_host_suffix(invalid).is_err());
        }
    }
}
