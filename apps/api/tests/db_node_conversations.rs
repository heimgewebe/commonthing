//! PostgreSQL + Axum proof for the public conversation owned by each node.
//!
//! Run with a direct PostgreSQL connection:
//! `DATABASE_URL=postgres://... cargo test --locked --test db_node_conversations -- --include-ignored`

use std::{path::PathBuf, str::FromStr, sync::Arc, time::Duration};

use axum::{
    body,
    http::{HeaderMap, Request, StatusCode},
    middleware::from_fn_with_state,
    Router,
};
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use p256::elliptic_curve::sec1::ToEncodedPoint;
use serial_test::serial;
use sqlx::postgres::{PgConnectOptions, PgPoolOptions};
use tower::ServiceExt;
use weltgewebe_api::{
    auth::{
        accounts::AccountStore, rate_limit::AuthRateLimiter, role::Role, session::SessionBackend,
    },
    config::{
        AppConfig, DomainAccountWriteSource, DomainEdgeWriteSource, DomainNodeWriteSource,
        DomainReadSource,
    },
    domain_db::{delete_node_with_edges_in_postgres, NodeConversationDeleteEffect},
    middleware::{auth::auth_middleware, csrf::require_csrf},
    notifications::WebPushService,
    routes::{
        accounts::{AccountInternal, AccountPublic, GarnrolleMapState},
        api_router,
    },
    state::{ApiState, OrderedCache},
    telemetry::{BuildInfo, Metrics},
};

const AUTHOR_ID: &str = "61000000-0000-4000-8000-000000000001";
const OTHER_ID: &str = "61000000-0000-4000-8000-000000000002";
const ADMIN_ID: &str = "61000000-0000-4000-8000-000000000003";
const GUEST_ID: &str = "61000000-0000-4000-8000-000000000004";
const NODE_ID: &str = "61000000-0000-4000-8000-000000000005";

fn direct_database_url() -> String {
    let url = std::env::var("DATABASE_URL")
        .expect("DATABASE_URL must point to direct PostgreSQL for this test");
    assert!(!url.contains(":6432"), "do not run through PgBouncer");
    url
}

struct IsolatedTestDatabase {
    pool: sqlx::PgPool,
    admin_pool: sqlx::PgPool,
    schema: String,
}

impl IsolatedTestDatabase {
    fn app_pool(&self) -> sqlx::PgPool {
        self.pool.clone()
    }

    async fn cleanup(self) {
        let Self {
            pool,
            admin_pool,
            schema,
        } = self;
        pool.close().await;
        sqlx::query(&format!("DROP SCHEMA {schema} CASCADE"))
            .execute(&admin_pool)
            .await
            .expect("drop isolated conversation proof schema");
        admin_pool.close().await;
    }
}

async fn pool() -> IsolatedTestDatabase {
    let options = PgConnectOptions::from_str(&direct_database_url()).expect("valid DATABASE_URL");
    let admin_pool = PgPoolOptions::new()
        .max_connections(1)
        .connect_with(options.clone())
        .await
        .expect("connect PostgreSQL for isolated schema management");
    let schema = format!("conversation_proof_{}", uuid::Uuid::new_v4().simple());
    sqlx::query(&format!("CREATE SCHEMA {schema}"))
        .execute(&admin_pool)
        .await
        .expect("create isolated conversation proof schema");

    let connection_schema = schema.clone();
    let pool = PgPoolOptions::new()
        .max_connections(5)
        .after_connect(move |connection, _metadata| {
            let schema = connection_schema.clone();
            Box::pin(async move {
                sqlx::query(&format!("SET search_path TO {schema}, public"))
                    .execute(connection)
                    .await?;
                Ok(())
            })
        })
        .connect_with(options)
        .await
        .expect("connect PostgreSQL in isolated conversation proof schema");
    let migrations = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("migrations");
    sqlx::migrate::Migrator::new(migrations)
        .await
        .expect("load migrations")
        .run(&pool)
        .await
        .expect("run migrations in isolated conversation proof schema");
    IsolatedTestDatabase {
        pool,
        admin_pool,
        schema,
    }
}

fn config() -> AppConfig {
    AppConfig {
        max_guest_owned_nodes: 1_000,
        domain_read_source: DomainReadSource::Postgres,
        domain_account_write_source: DomainAccountWriteSource::Postgres,
        domain_node_write_source: DomainNodeWriteSource::Postgres,
        domain_edge_write_source: DomainEdgeWriteSource::Postgres,
        passkey_credential_source: weltgewebe_api::config::PasskeyCredentialSource::InMemory,
        auth_public_login: false,
        auth_cookie_secure: true,
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

fn test_web_push_service() -> Arc<WebPushService> {
    const PRIVATE_KEY: &str = "WEB_PUSH_VAPID_PRIVATE_KEY";
    const CONTACT: &str = "WEB_PUSH_VAPID_CONTACT";
    const HOSTS: &str = "WEB_PUSH_ALLOWED_HOST_SUFFIXES";
    let previous = [PRIVATE_KEY, CONTACT, HOSTS].map(|name| (name, std::env::var_os(name)));
    std::env::set_var(PRIVATE_KEY, URL_SAFE_NO_PAD.encode([1_u8; 32]));
    std::env::set_var(CONTACT, "mailto:push-test@example.invalid");
    std::env::set_var(HOSTS, "push.example.invalid");
    let service = WebPushService::from_env()
        .expect("construct test Web Push service")
        .expect("test Web Push service configured");
    for (name, value) in previous {
        if let Some(value) = value {
            std::env::set_var(name, value);
        } else {
            std::env::remove_var(name);
        }
    }
    Arc::new(service)
}

fn valid_push_subscription_body(endpoint: &str) -> String {
    let secret = p256::SecretKey::from_slice(&[2_u8; 32]).expect("valid test subscription key");
    let public = secret.public_key().to_encoded_point(false);
    serde_json::json!({
        "endpoint": endpoint,
        "p256dh": URL_SAFE_NO_PAD.encode(public.as_bytes()),
        "auth": URL_SAFE_NO_PAD.encode([3_u8; 16]),
    })
    .to_string()
}

fn account(id: &str, title: &str, role: Role) -> AccountInternal {
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
        email: None,
        webauthn_user_id: uuid::Uuid::new_v4(),
    }
}

async fn seed_account(pool: &sqlx::PgPool, id: &str, title: &str, role: &str) {
    sqlx::query(
        "INSERT INTO domain_accounts
         (id, kind, title, map_state, radius_m, disabled, role, public_payload, private_payload)
         VALUES ($1, 'garnrolle', $2, 'not_on_map', 0, FALSE, $3, '{}', '{}')
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(id)
    .bind(title)
    .bind(role)
    .execute(pool)
    .await
    .expect("seed account");
}

async fn app_with_web_push(
    pool: sqlx::PgPool,
    web_push: Option<Arc<WebPushService>>,
) -> (Router, String, String, String, String) {
    let mut accounts = AccountStore::new();
    accounts.insert(account(AUTHOR_ID, "Autorin", Role::Weber));
    accounts.insert(account(OTHER_ID, "Anderer Weber", Role::Weber));
    accounts.insert(account(ADMIN_ID, "Administration", Role::Admin));
    accounts.insert(account(GUEST_ID, "Gast", Role::Gast));
    let config = config();
    let sessions = SessionBackend::new_in_memory();
    let author_session = sessions
        .create(AUTHOR_ID.to_string(), None)
        .await
        .expect("author session");
    let other_session = sessions
        .create(OTHER_ID.to_string(), None)
        .await
        .expect("other session");
    let admin_session = sessions
        .create(ADMIN_ID.to_string(), None)
        .await
        .expect("admin session");
    let guest_session = sessions
        .create(GUEST_ID.to_string(), None)
        .await
        .expect("guest session");
    let metrics = Metrics::try_new(BuildInfo {
        version: "test",
        commit: "test",
        build_timestamp: "test",
    })
    .expect("metrics");
    let nats_client = if web_push.is_some() {
        let nats_url = std::env::var("NATS_URL")
            .expect("NATS_URL is required for the Web Push integration proof");
        Some(
            async_nats::connect(nats_url)
                .await
                .expect("connect Web Push integration proof to NATS"),
        )
    } else {
        None
    };
    let nats_configured = nats_client.is_some();
    let state = ApiState {
        db_pool: Some(pool),
        db_pool_configured: true,
        nats_client,
        nats_configured,
        config: config.clone(),
        metrics,
        sessions,
        challenges: Default::default(),
        tokens: weltgewebe_api::auth::tokens::TokenStore::new(),
        step_up_tokens: weltgewebe_api::auth::step_up_tokens::StepUpTokenStore::new(),
        accounts: Arc::new(tokio::sync::RwLock::new(accounts)),
        nodes: Arc::new(tokio::sync::RwLock::new(OrderedCache::new())),
        nodes_persist: Arc::new(tokio::sync::Mutex::new(())),
        accounts_persist: Arc::new(tokio::sync::Mutex::new(())),
        domain_projection_gate: Arc::new(tokio::sync::RwLock::new(())),
        domain_projection_reload: std::sync::Arc::new(tokio::sync::Mutex::new(())),
        domain_projection_version: Arc::new(std::sync::atomic::AtomicI64::new(0)),
        edges: Arc::new(tokio::sync::RwLock::new(OrderedCache::new())),
        rate_limiter: Arc::new(AuthRateLimiter::new(&config)),
        mailer: None,
        webauthn: None,
        passkey_registrations: Default::default(),
        passkey_registration_grants: Default::default(),
        passkey_authentications: Default::default(),
        passkeys: Default::default(),
        web_push,
    };
    let router = Router::new()
        .merge(api_router())
        .layer(from_fn_with_state(state.clone(), auth_middleware))
        .layer(axum::middleware::from_fn(require_csrf))
        .with_state(state);
    (
        router,
        format!("gewebe_session={}", author_session.id),
        format!("gewebe_session={}", other_session.id),
        format!("gewebe_session={}", admin_session.id),
        format!("gewebe_session={}", guest_session.id),
    )
}

async fn app(pool: sqlx::PgPool) -> (Router, String, String, String, String) {
    app_with_web_push(pool, None).await
}

fn request(
    method: &str,
    path: &str,
    cookie: Option<&str>,
    body_json: Option<&str>,
    idempotency_key: Option<&str>,
    if_match: Option<&str>,
) -> Request<body::Body> {
    let mut builder = Request::builder()
        .method(method)
        .uri(path)
        .header("Host", "localhost")
        .header("Origin", "http://localhost");
    if let Some(cookie) = cookie {
        builder = builder.header("Cookie", cookie);
    }
    if let Some(key) = idempotency_key {
        builder = builder.header("Idempotency-Key", key);
    }
    if let Some(version) = if_match {
        builder = builder.header("If-Match", format!("\"{version}\""));
    }
    if let Some(body_json) = body_json {
        builder = builder.header("Content-Type", "application/json");
        return builder
            .body(body::Body::from(body_json.to_string()))
            .expect("request");
    }
    builder.body(body::Body::empty()).expect("request")
}

async fn json_response_with_headers(
    app: &Router,
    request: Request<body::Body>,
) -> (StatusCode, HeaderMap, serde_json::Value) {
    let response = app.clone().oneshot(request).await.expect("response");
    let status = response.status();
    let headers = response.headers().clone();
    let bytes = body::to_bytes(response.into_body(), usize::MAX)
        .await
        .expect("response body");
    let value = if bytes.is_empty() {
        serde_json::Value::Null
    } else {
        serde_json::from_slice(&bytes).expect("JSON response")
    };
    (status, headers, value)
}

async fn json_response(
    app: &Router,
    request: Request<body::Body>,
) -> (StatusCode, serde_json::Value) {
    let (status, _, value) = json_response_with_headers(app, request).await;
    (status, value)
}

/// One participant's own projection of a private conversation, read back from the
/// inbox so assertions see exactly what the client would render.
async fn direct_conversation_item(
    app: &Router,
    cookie: &str,
    conversation_id: &str,
) -> serde_json::Value {
    let (status, inbox) = json_response(
        app,
        request(
            "GET",
            "/direct-conversations",
            Some(cookie),
            None,
            None,
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    inbox["items"]
        .as_array()
        .expect("private inbox items")
        .iter()
        .find(|item| item["id"].as_str() == Some(conversation_id))
        .cloned()
        .expect("private conversation in the participant inbox")
}

async fn set_account_disabled(pool: &sqlx::PgPool, id: &str, disabled: bool) {
    sqlx::query("UPDATE domain_accounts SET disabled = $2 WHERE id = $1")
        .bind(id)
        .bind(disabled)
        .execute(pool)
        .await
        .expect("switch account activation state");
}

#[tokio::test]
#[serial]
#[ignore = "requires direct PostgreSQL"]
async fn private_message_push_preference_cancels_queued_delivery() {
    if std::env::var_os("NATS_URL").is_none() {
        eprintln!(
            "skipping Web Push device recovery proof: NATS_URL is provided by the dedicated PostgreSQL integration lane"
        );
        return;
    }

    let database = pool().await;
    let pool = database.app_pool();
    seed_account(&pool, AUTHOR_ID, "Autorin", "weber").await;
    seed_account(&pool, OTHER_ID, "Anderer Weber", "weber").await;

    let conversation_id = "62000000-0000-4000-8000-000000000001";
    let message_id = "62000000-0000-4000-8000-000000000002";
    let subscription_id = "62000000-0000-4000-8000-000000000003";
    let current_subscription_id = "62000000-0000-4000-8000-000000000005";
    let foreign_subscription_id = "62000000-0000-4000-8000-000000000006";
    let race_subscription_id = "62000000-0000-4000-8000-000000000008";
    let mut tx = pool.begin().await.expect("begin direct conversation proof");
    sqlx::query(
        "INSERT INTO domain_conversations (
             id, node_id, conversation_type, visibility, direct_pair_key,
             direct_created_by_account_id
         ) VALUES ($1::uuid, NULL, 'direct', 'participants', $2, $3)",
    )
    .bind(conversation_id)
    .bind(format!("{AUTHOR_ID}:{OTHER_ID}"))
    .bind(AUTHOR_ID)
    .execute(&mut *tx)
    .await
    .expect("insert direct conversation");
    for (slot, account_id, title) in [
        (1_i16, AUTHOR_ID, "Autorin"),
        (2_i16, OTHER_ID, "Anderer Weber"),
    ] {
        sqlx::query(
            "INSERT INTO domain_direct_conversation_participants (
                 conversation_id, slot, account_id, account_title_snapshot
             ) VALUES ($1::uuid, $2, $3, $4)",
        )
        .bind(conversation_id)
        .bind(slot)
        .bind(account_id)
        .bind(title)
        .execute(&mut *tx)
        .await
        .expect("insert direct conversation participant");
    }
    sqlx::query(
        "INSERT INTO domain_messages (
             id, conversation_id, author_account_id, author_title, content,
             idempotency_key
         ) VALUES ($1::uuid, $2::uuid, $3, 'Autorin', 'Verbindliche Nachricht',
                   '62000000-0000-4000-8000-000000000004'::uuid)",
    )
    .bind(message_id)
    .bind(conversation_id)
    .bind(AUTHOR_ID)
    .execute(&mut *tx)
    .await
    .expect("insert private message");
    tx.commit().await.expect("commit direct conversation proof");

    let source_event_id: i64 = sqlx::query_scalar(
        "SELECT id FROM domain_outbox
         WHERE aggregate_type = 'message' AND aggregate_id = $1
           AND event_type = 'domain.message.created'",
    )
    .bind(message_id)
    .fetch_one(&pool)
    .await
    .expect("read committed private-message event");
    sqlx::query(
        "INSERT INTO notification_preferences (account_id, direct_messages_push)
         VALUES ($1, TRUE)",
    )
    .bind(OTHER_ID)
    .execute(&pool)
    .await
    .expect("enable recipient push preference");
    sqlx::query(
        "INSERT INTO web_push_subscriptions (
             id, account_id, endpoint, endpoint_hash, p256dh, auth_secret
         ) VALUES ($1::uuid, $2, $3, $4, $5, $6)",
    )
    .bind(subscription_id)
    .bind(OTHER_ID)
    .bind("https://push.example.invalid/subscription-proof")
    .bind("a".repeat(64))
    .bind("p".repeat(80))
    .bind("a".repeat(16))
    .execute(&pool)
    .await
    .expect("insert recipient browser subscription");
    sqlx::query(
        "INSERT INTO web_push_subscriptions (
             id, account_id, endpoint, endpoint_hash, p256dh, auth_secret
         ) VALUES ($1::uuid, $2, $3, $4, $5, $6)",
    )
    .bind(current_subscription_id)
    .bind(OTHER_ID)
    .bind("https://push.example.invalid/subscription-current")
    .bind("b".repeat(64))
    .bind("q".repeat(80))
    .bind("b".repeat(16))
    .execute(&pool)
    .await
    .expect("insert current browser subscription");
    sqlx::query(
        "INSERT INTO web_push_subscriptions (
             id, account_id, endpoint, endpoint_hash, p256dh, auth_secret
         ) VALUES ($1::uuid, $2, $3, $4, $5, $6)",
    )
    .bind(race_subscription_id)
    .bind(OTHER_ID)
    .bind("https://push.example.invalid/race-old-subscription")
    .bind("d".repeat(64))
    .bind("u".repeat(80))
    .bind("f".repeat(16))
    .execute(&pool)
    .await
    .expect("insert race target subscription");
    for index in 0_u64..17 {
        sqlx::query(
            "INSERT INTO web_push_subscriptions (
                 id, account_id, endpoint, endpoint_hash, p256dh, auth_secret
             ) VALUES ($1::uuid, $2, $3, $4, $5, $6)",
        )
        .bind(uuid::Uuid::new_v4())
        .bind(OTHER_ID)
        .bind(format!("https://push.example.invalid/extra-{index:02}"))
        .bind(format!("{:064x}", index + 1))
        .bind("r".repeat(80))
        .bind("c".repeat(16))
        .execute(&pool)
        .await
        .expect("insert extra browser subscription");
    }
    sqlx::query(
        "INSERT INTO web_push_subscriptions (
             id, account_id, endpoint, endpoint_hash, p256dh, auth_secret
         ) VALUES ($1::uuid, $2, $3, $4, $5, $6)",
    )
    .bind(foreign_subscription_id)
    .bind(AUTHOR_ID)
    .bind("https://push.example.invalid/foreign-subscription")
    .bind("30321d15294cc533fecae134512cacd1dfddc0135c3f3ed14340fee4a74ab7ae")
    .bind("s".repeat(80))
    .bind("d".repeat(16))
    .execute(&pool)
    .await
    .expect("insert foreign browser subscription");
    sqlx::query(
        "INSERT INTO web_push_deliveries (
             source_event_id, subscription_id, conversation_id
         ) VALUES ($1, $2::uuid, $3::uuid)",
    )
    .bind(source_event_id)
    .bind(subscription_id)
    .bind(conversation_id)
    .execute(&pool)
    .await
    .expect("queue private-message push delivery");
    sqlx::query(
        "INSERT INTO web_push_deliveries (
             source_event_id, subscription_id, conversation_id
         ) VALUES ($1, $2::uuid, $3::uuid)",
    )
    .bind(source_event_id)
    .bind(foreign_subscription_id)
    .bind(conversation_id)
    .execute(&pool)
    .await
    .expect("queue foreign-account push delivery");

    let (app, _author, other, _admin, _guest) =
        app_with_web_push(pool.clone(), Some(test_web_push_service())).await;
    let mut list_request = request("GET", "/push/subscriptions", Some(&other), None, None, None);
    list_request.headers_mut().insert(
        "x-weltgewebe-push-endpoint-hash",
        "b".repeat(64).parse().expect("current push marker header"),
    );
    let (status, managed) = json_response(&app, list_request).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(managed["limit"], 20);
    let items = managed["items"].as_array().expect("managed push items");
    assert_eq!(
        items.len(),
        20,
        "only the account's own active subscriptions"
    );
    assert_eq!(
        items.iter().filter(|item| item["current"] == true).count(),
        1,
        "exactly the transient endpoint hash marks the current device"
    );
    assert!(items.iter().any(|item| {
        item["id"].as_str() == Some(current_subscription_id) && item["current"] == true
    }));
    assert!(!items
        .iter()
        .any(|item| item["id"].as_str() == Some(foreign_subscription_id)));
    let serialized = managed.to_string();
    for secret_field in [
        "\"endpoint\"",
        "\"endpoint_hash\"",
        "\"p256dh\"",
        "\"auth_secret\"",
    ] {
        assert!(!serialized.contains(secret_field));
    }
    assert!(!serialized.contains("subscription-proof"));

    let mut current_delete = request(
        "DELETE",
        &format!("/push/subscriptions/{current_subscription_id}"),
        Some(&other),
        None,
        None,
        None,
    );
    current_delete.headers_mut().insert(
        "x-weltgewebe-push-endpoint-hash",
        "b".repeat(64).parse().expect("current push marker header"),
    );
    let (status, current_error) = json_response(&app, current_delete).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(current_error["code"], "push_subscription_is_current_device");

    let mut foreign_delete = request(
        "DELETE",
        &format!("/push/subscriptions/{foreign_subscription_id}"),
        Some(&other),
        None,
        None,
        None,
    );
    foreign_delete.headers_mut().insert(
        "x-weltgewebe-push-endpoint-hash",
        "b".repeat(64).parse().expect("current push marker header"),
    );
    let (status, _) = json_response(&app, foreign_delete).await;
    assert_eq!(status, StatusCode::NO_CONTENT);
    let foreign_still_active: bool = sqlx::query_scalar(
        "SELECT EXISTS (
             SELECT 1 FROM web_push_subscriptions
             WHERE id = $1::uuid AND account_id = $2 AND disabled_at IS NULL
         )",
    )
    .bind(foreign_subscription_id)
    .bind(AUTHOR_ID)
    .fetch_one(&pool)
    .await
    .expect("foreign subscription remains active");
    assert!(foreign_still_active);

    let replacement_endpoint = "https://push.example.invalid/replacement-subscription";
    let replacement_body = valid_push_subscription_body(replacement_endpoint);
    let (status, limit_error) = json_response(
        &app,
        request(
            "POST",
            "/push/subscriptions",
            Some(&other),
            Some(&replacement_body),
            None,
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::TOO_MANY_REQUESTS);
    assert_eq!(limit_error["code"], "push_subscription_limit_reached");

    for attempt in 0..2 {
        let mut old_delete = request(
            "DELETE",
            &format!("/push/subscriptions/{subscription_id}"),
            Some(&other),
            None,
            None,
            None,
        );
        old_delete.headers_mut().insert(
            "x-weltgewebe-push-endpoint-hash",
            "b".repeat(64).parse().expect("current push marker header"),
        );
        let (status, _) = json_response(&app, old_delete).await;
        assert_eq!(status, StatusCode::NO_CONTENT, "disable attempt {attempt}");
    }
    let retired_delivery: (String, Option<String>) = sqlx::query_as(
        "SELECT status, last_error FROM web_push_deliveries
         WHERE source_event_id = $1 AND subscription_id = $2::uuid",
    )
    .bind(source_event_id)
    .bind(subscription_id)
    .fetch_one(&pool)
    .await
    .expect("read retired old-device delivery");
    assert_eq!(retired_delivery.0, "cancelled");
    assert_eq!(retired_delivery.1.as_deref(), Some("disabled by account"));
    let active_after_cleanup: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM web_push_subscriptions
         WHERE account_id = $1 AND disabled_at IS NULL",
    )
    .bind(OTHER_ID)
    .fetch_one(&pool)
    .await
    .expect("count active subscriptions after cleanup");
    assert_eq!(active_after_cleanup, 19);

    let foreign_endpoint = "https://push.example.invalid/foreign-subscription";
    let foreign_body = valid_push_subscription_body(foreign_endpoint);
    let (status, foreign_register_error) = json_response(
        &app,
        request(
            "POST",
            "/push/subscriptions",
            Some(&other),
            Some(&foreign_body),
            None,
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(foreign_register_error["code"], "push_subscription_conflict");
    let foreign_owner: String =
        sqlx::query_scalar("SELECT account_id FROM web_push_subscriptions WHERE id = $1::uuid")
            .bind(foreign_subscription_id)
            .fetch_one(&pool)
            .await
            .expect("foreign registration keeps original account owner");
    assert_eq!(foreign_owner, AUTHOR_ID);
    let foreign_delivery_status: String = sqlx::query_scalar(
        "SELECT status FROM web_push_deliveries
         WHERE source_event_id = $1 AND subscription_id = $2::uuid",
    )
    .bind(source_event_id)
    .bind(foreign_subscription_id)
    .fetch_one(&pool)
    .await
    .expect("foreign registration keeps foreign delivery receipt untouched");
    assert_eq!(foreign_delivery_status, "pending");

    let (status, replacement) = json_response(
        &app,
        request(
            "POST",
            "/push/subscriptions",
            Some(&other),
            Some(&replacement_body),
            None,
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED);
    assert!(replacement["id"].as_str().is_some());
    let active_after_replacement: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM web_push_subscriptions
         WHERE account_id = $1 AND disabled_at IS NULL",
    )
    .bind(OTHER_ID)
    .fetch_one(&pool)
    .await
    .expect("count active subscriptions after replacement");
    assert_eq!(active_after_replacement, 20);

    let race_endpoint = "https://push.example.invalid/race-replacement";
    let race_body = valid_push_subscription_body(race_endpoint);
    let mut blocker = pool.begin().await.expect("begin push account race blocker");
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))")
        .bind(format!("web-push-account:{OTHER_ID}"))
        .fetch_one(&mut *blocker)
        .await
        .expect("hold push account race lock");

    let delete_app = app.clone();
    let delete_cookie = other.clone();
    let delete_task = tokio::spawn(async move {
        let mut delete = request(
            "DELETE",
            &format!("/push/subscriptions/{race_subscription_id}"),
            Some(&delete_cookie),
            None,
            None,
            None,
        );
        delete.headers_mut().insert(
            "x-weltgewebe-push-endpoint-hash",
            "b".repeat(64).parse().expect("current push marker header"),
        );
        json_response(&delete_app, delete).await
    });
    let register_app = app.clone();
    let register_cookie = other.clone();
    let concurrent_body = race_body.clone();
    let register_task = tokio::spawn(async move {
        json_response(
            &register_app,
            request(
                "POST",
                "/push/subscriptions",
                Some(&register_cookie),
                Some(&concurrent_body),
                None,
                None,
            ),
        )
        .await
    });
    tokio::time::sleep(Duration::from_millis(25)).await;
    blocker
        .commit()
        .await
        .expect("release push account race lock");

    let (delete_status, _) = delete_task.await.expect("delete race task");
    let (register_status, register_result) = register_task.await.expect("register race task");
    assert_eq!(delete_status, StatusCode::NO_CONTENT);
    assert!(
        register_status == StatusCode::CREATED || register_status == StatusCode::TOO_MANY_REQUESTS,
        "registration must either consume the freed slot or safely observe the full account"
    );
    let active_after_race: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM web_push_subscriptions
         WHERE account_id = $1 AND disabled_at IS NULL",
    )
    .bind(OTHER_ID)
    .fetch_one(&pool)
    .await
    .expect("count subscriptions after register/delete race");
    assert!(
        active_after_race <= 20,
        "race must never oversubscribe the account"
    );
    if register_status == StatusCode::TOO_MANY_REQUESTS {
        assert_eq!(register_result["code"], "push_subscription_limit_reached");
        let (retry_status, retry) = json_response(
            &app,
            request(
                "POST",
                "/push/subscriptions",
                Some(&other),
                Some(&race_body),
                None,
                None,
            ),
        )
        .await;
        assert_eq!(retry_status, StatusCode::CREATED);
        assert!(retry["id"].as_str().is_some());
    } else {
        assert!(register_result["id"].as_str().is_some());
    }
    let active_after_race_recovery: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM web_push_subscriptions
         WHERE account_id = $1 AND disabled_at IS NULL",
    )
    .bind(OTHER_ID)
    .fetch_one(&pool)
    .await
    .expect("count subscriptions after race recovery");
    assert_eq!(active_after_race_recovery, 20);
    let race_target_disabled: bool = sqlx::query_scalar(
        "SELECT disabled_at IS NOT NULL FROM web_push_subscriptions
         WHERE id = $1::uuid AND account_id = $2",
    )
    .bind(race_subscription_id)
    .bind(OTHER_ID)
    .fetch_one(&pool)
    .await
    .expect("read race target disable state");
    assert!(race_target_disabled);

    sqlx::query(
        "INSERT INTO web_push_deliveries (
             source_event_id, subscription_id, conversation_id
         ) VALUES ($1, $2::uuid, $3::uuid)",
    )
    .bind(source_event_id)
    .bind(current_subscription_id)
    .bind(conversation_id)
    .execute(&pool)
    .await
    .expect("queue current-device delivery for preference proof");

    let (status, before) = json_response(
        &app,
        request(
            "GET",
            "/notifications/preferences",
            Some(&other),
            None,
            None,
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(before["direct_messages_push"], true);

    let (status, after) = json_response(
        &app,
        request(
            "PUT",
            "/notifications/preferences",
            Some(&other),
            Some(r#"{"direct_messages_push":false}"#),
            None,
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(after["direct_messages_push"], false);

    let delivery: (String, Option<String>) = sqlx::query_as(
        "SELECT status, last_error FROM web_push_deliveries
         WHERE source_event_id = $1 AND subscription_id = $2::uuid",
    )
    .bind(source_event_id)
    .bind(current_subscription_id)
    .fetch_one(&pool)
    .await
    .expect("read cancelled push delivery");
    assert_eq!(delivery.0, "cancelled");
    assert_eq!(delivery.1.as_deref(), Some("disabled by account"));

    let message_content: String =
        sqlx::query_scalar("SELECT content FROM domain_messages WHERE id = $1::uuid")
            .bind(message_id)
            .fetch_one(&pool)
            .await
            .expect("canonical message remains stored after push opt-out");
    assert_eq!(message_content, "Verbindliche Nachricht");

    database.cleanup().await;
}

#[tokio::test]
#[serial]
#[ignore = "requires direct PostgreSQL"]
async fn node_conversation_vertical_slice() {
    let database = pool().await;
    let pool = database.app_pool();
    seed_account(&pool, AUTHOR_ID, "Autorin", "weber").await;
    seed_account(&pool, OTHER_ID, "Anderer Weber", "weber").await;
    seed_account(&pool, ADMIN_ID, "Administration", "admin").await;
    seed_account(&pool, GUEST_ID, "Gast", "gast").await;

    let invalid_author_title = sqlx::query(
        "INSERT INTO domain_accounts
         (id, kind, title, map_state, radius_m, disabled, role, public_payload, private_payload)
         VALUES ('conversation-proof-invalid-title', 'garnrolle', $1, 'not_on_map', 0, FALSE,
                 'weber', '{}', '{}')",
    )
    .bind("x".repeat(201))
    .execute(&pool)
    .await;
    assert!(
        invalid_author_title.is_err(),
        "new canonical accounts must satisfy the author snapshot title contract"
    );

    const EMPTY_NODE_ID: &str = "61000000-0000-4000-8000-000000000006";
    sqlx::query("DELETE FROM domain_conversations WHERE node_id = $1")
        .bind(EMPTY_NODE_ID)
        .execute(&pool)
        .await
        .expect("clean empty-node conversation proof");
    sqlx::query("DELETE FROM domain_nodes WHERE id = $1")
        .bind(EMPTY_NODE_ID)
        .execute(&pool)
        .await
        .expect("clean empty-node proof");
    sqlx::query(
        "INSERT INTO domain_nodes (id, kind, title, created_at, updated_at, payload)
         VALUES ($1, 'Ort', 'Leerer Gesprächsknoten', NOW(), NOW(), '{}')",
    )
    .bind(EMPTY_NODE_ID)
    .execute(&pool)
    .await
    .expect("insert empty node and generated conversation");
    let empty_conversation_id: String =
        sqlx::query_scalar("SELECT weltgewebe_node_conversation_id($1)::text")
            .bind(EMPTY_NODE_ID)
            .fetch_one(&pool)
            .await
            .expect("derive empty conversation id");
    let empty_outcome = delete_node_with_edges_in_postgres(&pool, EMPTY_NODE_ID)
        .await
        .expect("empty node deletion remains available");
    assert!(empty_outcome.removed_edge_ids.is_empty());
    assert_eq!(
        empty_outcome.conversation,
        NodeConversationDeleteEffect::DeletedEmpty
    );
    let empty_conversation_exists: bool = sqlx::query_scalar(
        "SELECT EXISTS (SELECT 1 FROM domain_conversations WHERE id = $1::uuid)",
    )
    .bind(&empty_conversation_id)
    .fetch_one(&pool)
    .await
    .expect("read empty conversation after node deletion");
    assert!(
        !empty_conversation_exists,
        "an empty generated conversation still cascades away with its node"
    );
    sqlx::query("DELETE FROM domain_outbox WHERE aggregate_id = ANY($1::text[])")
        .bind(vec![EMPTY_NODE_ID.to_string(), empty_conversation_id])
        .execute(&pool)
        .await
        .expect("clean empty-node outbox proof");

    let version_before_node: i64 =
        sqlx::query_scalar("SELECT version FROM domain_projection_state WHERE singleton")
            .fetch_one(&pool)
            .await
            .expect("projection version before node");
    sqlx::query(
        "INSERT INTO domain_nodes (id, kind, title, created_at, updated_at, payload)
         VALUES ($1, 'Ort', 'Gesprächsknoten', NOW(), NOW(), '{}')",
    )
    .bind(NODE_ID)
    .execute(&pool)
    .await
    .expect("insert node and conversation atomically");
    let version_after_node: i64 =
        sqlx::query_scalar("SELECT version FROM domain_projection_state WHERE singleton")
            .fetch_one(&pool)
            .await
            .expect("projection version after node");
    assert_eq!(
        version_after_node,
        version_before_node + 1,
        "the node changes the map projection exactly once; its conversation does not"
    );

    let (conversation_id, deterministic_id, conversation_count): (String, String, i64) =
        sqlx::query_as(
            "SELECT min(id::text), weltgewebe_node_conversation_id($1)::text, count(*)
             FROM domain_conversations WHERE node_id = $1",
        )
        .bind(NODE_ID)
        .fetch_one(&pool)
        .await
        .expect("read generated conversation");
    assert_eq!(conversation_count, 1);
    assert_eq!(conversation_id, deterministic_id);
    let conversation_event: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM domain_outbox
         WHERE aggregate_type = 'conversation' AND aggregate_id = $1
           AND event_type = 'domain.conversation.created'",
    )
    .bind(&conversation_id)
    .fetch_one(&pool)
    .await
    .expect("conversation outbox event");
    assert_eq!(conversation_event, 1);

    let active_null = sqlx::query(
        "INSERT INTO domain_messages (
             id, conversation_id, author_account_id, author_title, content, idempotency_key
         ) VALUES (
             '71000000-0000-4000-8000-000000000001'::uuid, $1::uuid, $2, 'Autorin', NULL,
             '71000000-0000-4000-8000-000000000002'::uuid
         )",
    )
    .bind(&conversation_id)
    .bind(AUTHOR_ID)
    .execute(&pool)
    .await;
    assert!(
        active_null.is_err(),
        "an active message with NULL content must be rejected by PostgreSQL"
    );

    let whitespace_only = sqlx::query(
        "INSERT INTO domain_messages (
             id, conversation_id, author_account_id, author_title, content, idempotency_key
         ) VALUES (
             '71000000-0000-4000-8000-000000000003'::uuid, $1::uuid, $2, 'Autorin', $3,
             '71000000-0000-4000-8000-000000000004'::uuid
         )",
    )
    .bind(&conversation_id)
    .bind(AUTHOR_ID)
    .bind("\n\t")
    .execute(&pool)
    .await;
    assert!(
        whitespace_only.is_err(),
        "an active message containing only whitespace must be rejected by PostgreSQL"
    );

    let (app, author, other, admin, guest) = app(pool.clone()).await;
    let (status, conversation) = json_response(
        &app,
        request(
            "GET",
            &format!("/nodes/{NODE_ID}/conversation"),
            None,
            None,
            None,
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(conversation["id"], conversation_id);
    assert_eq!(conversation["lifecycle_state"], "active");
    assert_eq!(conversation["visibility"], "public");

    for (method, path, body) in [
        ("GET", "/conversations/not-a-uuid", None),
        ("GET", "/conversations/not-a-uuid/messages", None),
        (
            "PATCH",
            &format!("/conversations/{conversation_id}/messages/not-a-uuid"),
            Some(r#"{"content":"Ungültig"}"#),
        ),
    ] {
        let (status, _) = json_response(
            &app,
            request(
                method,
                path,
                Some(&author),
                body,
                None,
                Some("2026-01-01T00:00:00Z"),
            ),
        )
        .await;
        assert_eq!(
            status,
            StatusCode::NOT_FOUND,
            "invalid UUID paths must fail closed"
        );
    }

    let message_path = format!("/conversations/{conversation_id}/messages");
    let (status, guest_message) = json_response(
        &app,
        request(
            "POST",
            &message_path,
            Some(&guest),
            Some(r#"{"content":"Gastbeitrag"}"#),
            Some("70000000-0000-4000-8000-000000000001"),
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED);
    assert_eq!(guest_message["author_account_id"], GUEST_ID);
    assert_eq!(guest_message["author_title"], "Gast");

    let (status, invalid_content) = json_response(
        &app,
        request(
            "POST",
            &message_path,
            Some(&author),
            Some(r#"{"content":"   \n\t"}"#),
            Some("70000000-0000-4000-8000-000000000099"),
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(invalid_content["code"], "invalid_message_content");

    let first_key = "70000000-0000-4000-8000-000000000002";
    let (status, first) = json_response(
        &app,
        request(
            "POST",
            &message_path,
            Some(&author),
            Some(r#"{"content":"Erster Beitrag"}"#),
            Some(first_key),
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED);
    assert_eq!(first["author_title"], "Autorin");

    let (status, replay) = json_response(
        &app,
        request(
            "POST",
            &message_path,
            Some(&author),
            Some(r#"{"content":"Erster Beitrag"}"#),
            Some(first_key),
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(first["id"], replay["id"]);

    let participation_after_replay: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM domain_edges
         WHERE target_id = $1
           AND payload ->> 'target_type' = 'node'
           AND payload ->> 'source_type' = 'account'
           AND payload ->> 'faden_type' = 'conversation'
           AND payload ->> 'faden_subject_id' = $2",
    )
    .bind(NODE_ID)
    .bind(&conversation_id)
    .fetch_one(&pool)
    .await
    .expect("count message participation Fäden after replay");
    assert_eq!(
        participation_after_replay, 2,
        "guest contribution plus first author contribution create two Fäden; replay creates none",
    );

    let (status, conflict) = json_response(
        &app,
        request(
            "POST",
            &message_path,
            Some(&author),
            Some(r#"{"content":"Andere Daten"}"#),
            Some(first_key),
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(conflict["code"], "idempotency_key_conflict");

    for (key, content) in [
        ("70000000-0000-4000-8000-000000000003", "Zweiter"),
        ("70000000-0000-4000-8000-000000000004", "Dritter"),
    ] {
        let (status, _) = json_response(
            &app,
            request(
                "POST",
                &message_path,
                Some(&author),
                Some(&format!(r#"{{"content":"{content}"}}"#)),
                Some(key),
                None,
            ),
        )
        .await;
        assert_eq!(status, StatusCode::CREATED);
    }

    let (status, author_details) = json_response(
        &app,
        request(
            "GET",
            &format!("/accounts/{AUTHOR_ID}"),
            None,
            None,
            None,
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let author_activity = author_details["activity"]
        .as_array()
        .expect("account activity must be an array");
    assert_eq!(
        author_activity.len(),
        1,
        "same-day contributions are one durable summary; replay creates no extra activity"
    );
    assert_eq!(
        author_activity[0]["event"],
        "Hat 3 Beiträge zum Gespräch über den Knoten \"Gesprächsknoten\" geschrieben."
    );
    assert!(author_activity[0]
        .get("event")
        .and_then(|event| event.as_str())
        .is_some_and(|event| !event.contains("geknüpft")));
    assert!(author_activity[0].get("content").is_none());

    let (status, page_one) = json_response(
        &app,
        request(
            "GET",
            &format!("{message_path}?limit=2"),
            None,
            None,
            None,
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(page_one["items"].as_array().map(Vec::len), Some(2));
    assert_eq!(page_one["page"]["has_more"], true);
    let cursor = page_one["page"]["next_cursor"]
        .as_str()
        .expect("next cursor");
    let (status, page_two) = json_response(
        &app,
        request(
            "GET",
            &format!("{message_path}?limit=2&cursor={cursor}"),
            None,
            None,
            None,
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(page_two["items"].as_array().map(Vec::len), Some(2));
    assert_eq!(page_two["page"]["has_more"], false);
    assert!(page_two["page"]["next_cursor"].is_null());
    let first_page_ids: Vec<&str> = page_one["items"]
        .as_array()
        .expect("first page")
        .iter()
        .filter_map(|item| item["id"].as_str())
        .collect();
    assert!(!first_page_ids.contains(&page_two["items"][0]["id"].as_str().unwrap()));

    let item_path = format!("{message_path}/{}", first["id"].as_str().unwrap());
    let (status, forbidden) = json_response(
        &app,
        request(
            "PATCH",
            &item_path,
            Some(&other),
            Some(r#"{"content":"Fremde Änderung"}"#),
            None,
            first["updated_at"].as_str(),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN);
    assert_eq!(forbidden["code"], "message_author_required");

    let (status, guest_edit_forbidden) = json_response(
        &app,
        request(
            "PATCH",
            &item_path,
            Some(&guest),
            Some(r#"{"content":"Gast ändert fremden Beitrag"}"#),
            None,
            first["updated_at"].as_str(),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN);
    assert_eq!(guest_edit_forbidden["code"], "message_author_required");

    let (status, guest_delete_forbidden) = json_response(
        &app,
        request(
            "DELETE",
            &item_path,
            Some(&guest),
            None,
            None,
            first["updated_at"].as_str(),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN);
    assert_eq!(guest_delete_forbidden["code"], "message_owner_required");

    let (status, required) = json_response(
        &app,
        request(
            "PATCH",
            &item_path,
            Some(&author),
            Some(r#"{"content":"Eigene Änderung"}"#),
            None,
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::PRECONDITION_REQUIRED);
    assert_eq!(required["code"], "message_version_required");

    let (status, stale) = json_response(
        &app,
        request(
            "PATCH",
            &item_path,
            Some(&author),
            Some(r#"{"content":"Eigene Änderung"}"#),
            None,
            Some("2020-01-01T00:00:00Z"),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::PRECONDITION_FAILED);
    assert_eq!(stale["code"], "message_version_conflict");

    let (status, updated) = json_response(
        &app,
        request(
            "PATCH",
            &item_path,
            Some(&author),
            Some(r#"{"content":"Eigene Änderung"}"#),
            None,
            first["updated_at"].as_str(),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(updated["content"], "Eigene Änderung");

    let (status, admin_edit_forbidden) = json_response(
        &app,
        request(
            "PATCH",
            &item_path,
            Some(&admin),
            Some(r#"{"content":"Administrative Änderung"}"#),
            None,
            updated["updated_at"].as_str(),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN);
    assert_eq!(admin_edit_forbidden["code"], "message_author_required");

    let (status, tombstone) = json_response(
        &app,
        request(
            "DELETE",
            &item_path,
            Some(&admin),
            None,
            None,
            updated["updated_at"].as_str(),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert!(tombstone["content"].is_null());
    assert!(tombstone["deleted_at"].is_string());
    let persisted: (Option<String>, bool) = sqlx::query_as(
        "SELECT content, deleted_at IS NOT NULL FROM domain_messages WHERE id = $1::uuid",
    )
    .bind(first["id"].as_str().unwrap())
    .fetch_one(&pool)
    .await
    .expect("persisted tombstone");
    assert_eq!(persisted, (None, true));

    let (status, repeated_tombstone) = json_response(
        &app,
        request(
            "DELETE",
            &item_path,
            Some(&admin),
            None,
            None,
            tombstone["updated_at"].as_str(),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(repeated_tombstone["updated_at"], tombstone["updated_at"]);

    let (status, edit_tombstone) = json_response(
        &app,
        request(
            "PATCH",
            &item_path,
            Some(&author),
            Some(r#"{"content":"Wiederbelebung"}"#),
            None,
            tombstone["updated_at"].as_str(),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(edit_tombstone["code"], "message_deleted");

    let author_delete_target: (String, chrono::DateTime<chrono::Utc>) = sqlx::query_as(
        "SELECT id::text, updated_at FROM domain_messages
         WHERE conversation_id = $1::uuid AND content = 'Dritter'",
    )
    .bind(&conversation_id)
    .fetch_one(&pool)
    .await
    .expect("message for author tombstone proof");
    let author_delete_path = format!("{message_path}/{}", author_delete_target.0);
    let (status, author_tombstone) = json_response(
        &app,
        request(
            "DELETE",
            &author_delete_path,
            Some(&author),
            None,
            None,
            Some(
                &author_delete_target
                    .1
                    .to_rfc3339_opts(chrono::SecondsFormat::AutoSi, true),
            ),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert!(author_tombstone["content"].is_null());
    assert!(author_tombstone["deleted_at"].is_string());

    let (status, author_activity_after_tombstones) = json_response(
        &app,
        request(
            "GET",
            &format!("/accounts/{AUTHOR_ID}"),
            None,
            None,
            None,
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        author_activity_after_tombstones["activity"]
            .as_array()
            .map(Vec::len),
        Some(1)
    );
    assert_eq!(
        author_activity_after_tombstones["activity"][0]["event"],
        "Hat 3 Beiträge zum Gespräch über den Knoten \"Gesprächsknoten\" geschrieben.",
        "tombstoning withdraws message content but keeps the durable contribution count"
    );

    let version_after_messages: i64 =
        sqlx::query_scalar("SELECT version FROM domain_projection_state WHERE singleton")
            .fetch_one(&pool)
            .await
            .expect("projection version after messages");
    assert_eq!(
        version_after_messages,
        version_after_node + 4,
        "four genuine contributions project activity: two create stable account Fäden and two reactivate the author's existing Faden; the exact replay projects nothing",
    );
    let payloads: Vec<String> = sqlx::query_scalar(
        "SELECT payload::text FROM domain_outbox
         WHERE aggregate_type = 'message' AND aggregate_id = $1",
    )
    .bind(first["id"].as_str().unwrap())
    .fetch_all(&pool)
    .await
    .expect("message outbox payloads");
    assert!(!payloads.is_empty());
    for payload in payloads {
        assert!(!payload.contains("Eigene Änderung"));
        assert!(!payload.contains("Autorin"));
        assert!(!payload.contains(AUTHOR_ID));
    }

    for index in 10..17 {
        let key = format!("70000000-0000-4000-8000-{index:012}");
        let content = format!("Rate-Beitrag {index}");
        let (status, _) = json_response(
            &app,
            request(
                "POST",
                &message_path,
                Some(&author),
                Some(&format!(r#"{{"content":"{content}"}}"#)),
                Some(&key),
                None,
            ),
        )
        .await;
        assert_eq!(status, StatusCode::CREATED);
    }
    let (status, headers, limited) = json_response_with_headers(
        &app,
        request(
            "POST",
            &message_path,
            Some(&author),
            Some(r#"{"content":"Ein Beitrag zu viel"}"#),
            Some("70000000-0000-4000-8000-000000000017"),
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::TOO_MANY_REQUESTS);
    assert_eq!(
        headers
            .get("retry-after")
            .and_then(|value| value.to_str().ok()),
        Some("60")
    );
    assert_eq!(limited["code"], "message_rate_limited");

    let (status, replay_after_limit) = json_response(
        &app,
        request(
            "POST",
            &message_path,
            Some(&author),
            Some(r#"{"content":"Zweiter"}"#),
            Some("70000000-0000-4000-8000-000000000003"),
            None,
        ),
    )
    .await;
    assert_eq!(
        status,
        StatusCode::OK,
        "an idempotent replay stays available after the creation limit is reached"
    );
    assert_eq!(replay_after_limit["content"], "Zweiter");

    // Binding check (idempotency scope): replay protection must be bound to the
    // conversation as well as the author, never to the author alone.
    let idempotency_columns: Vec<String> = sqlx::query_scalar(
        "SELECT a.attname \
         FROM pg_constraint c \
         JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) \
         WHERE c.conname = 'domain_messages_author_idempotency_unique' \
         ORDER BY a.attname",
    )
    .fetch_all(&pool)
    .await
    .expect("read idempotency constraint columns");
    assert!(
        idempotency_columns.contains(&"conversation_id".to_string()),
        "idempotency must be bound to the conversation"
    );
    assert!(
        idempotency_columns.contains(&"author_account_id".to_string()),
        "idempotency must be bound to the author"
    );
    assert!(
        idempotency_columns.contains(&"idempotency_key".to_string()),
        "idempotency must include the client key"
    );

    let active_participation_faden_count: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM domain_edges
         WHERE target_id = $1
           AND payload ->> 'target_type' = 'node'
           AND payload ->> 'source_type' = 'account'
           AND payload ->> 'faden_type' = 'conversation'
           AND payload ->> 'faden_subject_id' = $2",
    )
    .bind(NODE_ID)
    .bind(&conversation_id)
    .fetch_one(&pool)
    .await
    .expect("count active conversation participation Fäden");
    assert_eq!(
        active_participation_faden_count, 2,
        "each participating account keeps one conversation Faden; accepted contributions reactivate it, while replays and rejected writes add none",
    );

    // Binding check (account exit): a hard account delete must not be blocked by
    // existing public contributions, and the author snapshot must survive so the
    // history stays readable.
    let author_messages_before: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM domain_messages
         WHERE conversation_id = $1::uuid AND author_account_id = $2",
    )
    .bind(&conversation_id)
    .bind(AUTHOR_ID)
    .fetch_one(&pool)
    .await
    .expect("count author messages in the exercised conversation");
    assert!(
        author_messages_before >= 1,
        "the author must hold contributions before the deletion is exercised"
    );
    let deleted_author = sqlx::query("DELETE FROM domain_accounts WHERE id = $1")
        .bind(AUTHOR_ID)
        .execute(&pool)
        .await
        .expect("account deletion must not be blocked by existing messages");
    assert_eq!(deleted_author.rows_affected(), 1);
    let (surviving_messages, surviving_title, all_author_ids_cleared): (i64, String, bool) =
        sqlx::query_as(
            "SELECT count(*), min(author_title), bool_and(author_account_id IS NULL)
             FROM domain_messages
             WHERE conversation_id = $1::uuid AND author_title = 'Autorin'",
        )
        .bind(&conversation_id)
        .fetch_one(&pool)
        .await
        .expect("messages survive author deletion");
    assert_eq!(
        surviving_messages, author_messages_before,
        "contributions survive the deleted account instead of cascading away"
    );
    assert_eq!(
        surviving_title, "Autorin",
        "the author snapshot title is preserved after the account is gone"
    );
    assert!(
        all_author_ids_cleared,
        "deleted identities must lose ownership of every historical contribution"
    );

    seed_account(&pool, AUTHOR_ID, "Neu verwendete Kennung", "weber").await;
    let reclaim_target: (String, chrono::DateTime<chrono::Utc>) = sqlx::query_as(
        "SELECT id::text, updated_at FROM domain_messages
         WHERE conversation_id = $1::uuid AND content = 'Zweiter'",
    )
    .bind(&conversation_id)
    .fetch_one(&pool)
    .await
    .expect("message for reused-id ownership proof");
    let reclaim_path = format!("{message_path}/{}", reclaim_target.0);
    let (status, reclaim_denied) = json_response(
        &app,
        request(
            "PATCH",
            &reclaim_path,
            Some(&author),
            Some(r#"{"content":"Unrechtmäßige Übernahme"}"#),
            None,
            Some(&reclaim_target.1.to_rfc3339()),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN);
    assert_eq!(reclaim_denied["code"], "message_author_required");

    let direct_active_delete_error =
        sqlx::query("DELETE FROM domain_conversations WHERE id = $1::uuid")
            .bind(&conversation_id)
            .execute(&pool)
            .await
            .expect_err("PostgreSQL must reject deletion of a non-empty active conversation");
    assert_eq!(
        direct_active_delete_error
            .as_database_error()
            .and_then(|error| error.constraint()),
        Some("domain_conversations_history_guard")
    );

    let outcome = delete_node_with_edges_in_postgres(&pool, NODE_ID)
        .await
        .expect("node deletion must archive a non-empty public conversation");
    assert_eq!(
        outcome.removed_edge_ids.len(),
        2,
        "node deletion removes both stable account participation Fäden",
    );
    assert_eq!(
        outcome.conversation,
        NodeConversationDeleteEffect::Archived {
            archive_id: conversation_id.clone()
        }
    );

    let node_still_exists: bool =
        sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM domain_nodes WHERE id = $1)")
            .bind(NODE_ID)
            .fetch_one(&pool)
            .await
            .expect("read node after deletion");
    assert!(!node_still_exists);

    let (active_node_id, node_id_snapshot, node_title_snapshot, archived_at): (
        Option<String>,
        String,
        String,
        Option<chrono::DateTime<chrono::Utc>>,
    ) = sqlx::query_as(
        "SELECT node_id, node_id_snapshot, node_title_snapshot, archived_at
         FROM domain_conversations WHERE id = $1::uuid",
    )
    .bind(&conversation_id)
    .fetch_one(&pool)
    .await
    .expect("read archived conversation");
    assert_eq!(active_node_id, None);
    assert_eq!(node_id_snapshot, NODE_ID);
    assert_eq!(node_title_snapshot, "Gesprächsknoten");
    assert!(archived_at.is_some());
    let archive_event_count: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM domain_outbox          WHERE aggregate_type = 'conversation' AND aggregate_id = $1            AND event_type = 'domain.conversation.archived'",
    )
    .bind(&conversation_id)
    .fetch_one(&pool)
    .await
    .expect("read conversation archive outbox event");
    assert_eq!(archive_event_count, 1);

    let archive_path = format!("/conversations/{conversation_id}");
    let (status, archive_view) =
        json_response(&app, request("GET", &archive_path, None, None, None, None)).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(archive_view["lifecycle_state"], "archived");
    assert!(archive_view["node_id"].is_null());
    assert_eq!(archive_view["node_id_snapshot"], NODE_ID);
    assert_eq!(archive_view["node_title_snapshot"], "Gesprächsknoten");
    assert!(archive_view["archived_at"].is_string());

    let (status, guest_account_after_archive) = json_response(
        &app,
        request(
            "GET",
            &format!("/accounts/{GUEST_ID}"),
            None,
            None,
            None,
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        guest_account_after_archive["activity"]
            .as_array()
            .map(Vec::len),
        Some(1),
        "an archived but public conversation remains durable account activity"
    );
    assert_eq!(
        guest_account_after_archive["activity"][0]["event"],
        "Hat einen Beitrag zum Gespräch über den Knoten \"Gesprächsknoten\" geschrieben."
    );

    let (status, archived_messages) =
        json_response(&app, request("GET", &message_path, None, None, None, None)).await;
    assert_eq!(status, StatusCode::OK);
    assert!(
        archived_messages["items"]
            .as_array()
            .is_some_and(|items| !items.is_empty()),
        "archived history remains publicly readable"
    );

    let (status, archived_write) = json_response(
        &app,
        request(
            "POST",
            &message_path,
            Some(&author),
            Some(r#"{"content":"Nachträglicher Beitrag"}"#),
            Some("70000000-0000-4000-8000-000000000018"),
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(archived_write["code"], "conversation_archived");

    let (status, archived_edit) = json_response(
        &app,
        request(
            "PATCH",
            &item_path,
            Some(&admin),
            Some(r#"{"content":"Nachträgliche Änderung"}"#),
            None,
            tombstone["updated_at"].as_str(),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(archived_edit["code"], "conversation_archived");

    let archived_guest_path = format!(
        "{message_path}/{}",
        guest_message["id"].as_str().expect("guest message id")
    );
    let (status, archived_tombstone) = json_response(
        &app,
        request(
            "DELETE",
            &archived_guest_path,
            Some(&guest),
            None,
            None,
            guest_message["updated_at"].as_str(),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert!(archived_tombstone["content"].is_null());
    assert!(archived_tombstone["deleted_at"].is_string());

    let direct_tombstone_target: String = sqlx::query_scalar(
        "SELECT id::text FROM domain_messages          WHERE conversation_id = $1::uuid AND content = 'Zweiter'",
    )
    .bind(&conversation_id)
    .fetch_one(&pool)
    .await
    .expect("message for direct archive tombstone proof");
    let direct_tombstone = sqlx::query(
        "UPDATE domain_messages          SET content = NULL,              deleted_at = GREATEST(NOW(), updated_at + INTERVAL '1 microsecond'),              updated_at = GREATEST(NOW(), updated_at + INTERVAL '1 microsecond')          WHERE id = $1::uuid",
    )
    .bind(&direct_tombstone_target)
    .execute(&pool)
    .await
    .expect("PostgreSQL must allow the canonical one-way archive tombstone");
    assert_eq!(direct_tombstone.rows_affected(), 1);

    let direct_insert_error = sqlx::query(
        "INSERT INTO domain_messages (
             id, conversation_id, author_account_id, author_title, content, idempotency_key
         ) VALUES (
             '74000000-0000-4000-8000-000000000001'::uuid,
             $1::uuid,
             $2,
             'Neu verwendete Kennung',
             'Direkter Archivbeitrag',
             '74000000-0000-4000-8000-000000000002'::uuid
         )",
    )
    .bind(&conversation_id)
    .bind(AUTHOR_ID)
    .execute(&pool)
    .await
    .expect_err("PostgreSQL must reject direct inserts into an archived conversation");
    assert_eq!(
        direct_insert_error
            .as_database_error()
            .and_then(|error| error.constraint()),
        Some("domain_messages_archived_conversation_guard")
    );

    let direct_update_error = sqlx::query(
        "UPDATE domain_messages SET content = 'Direkte Archivänderung' WHERE id = $1::uuid",
    )
    .bind(guest_message["id"].as_str().unwrap())
    .execute(&pool)
    .await
    .expect_err("PostgreSQL must reject direct content changes in an archive");
    assert_eq!(
        direct_update_error
            .as_database_error()
            .and_then(|error| error.constraint()),
        Some("domain_messages_archived_conversation_guard")
    );

    let deleted_guest = sqlx::query("DELETE FROM domain_accounts WHERE id = $1")
        .bind(GUEST_ID)
        .execute(&pool)
        .await
        .expect("account exit may still detach identity from an archived contribution");
    assert_eq!(deleted_guest.rows_affected(), 1);
    let guest_snapshot: (Option<String>, String) = sqlx::query_as(
        "SELECT author_account_id, author_title FROM domain_messages WHERE id = $1::uuid",
    )
    .bind(guest_message["id"].as_str().unwrap())
    .fetch_one(&pool)
    .await
    .expect("read archived guest contribution after account exit");
    assert_eq!(guest_snapshot, (None, "Gast".to_string()));

    let direct_delete_error = sqlx::query("DELETE FROM domain_messages WHERE id = $1::uuid")
        .bind(guest_message["id"].as_str().unwrap())
        .execute(&pool)
        .await
        .expect_err("PostgreSQL must reject direct deletes from an archive");
    assert_eq!(
        direct_delete_error
            .as_database_error()
            .and_then(|error| error.constraint()),
        Some("domain_messages_physical_delete_guard")
    );

    let direct_archive_update_error = sqlx::query(
        "UPDATE domain_conversations SET node_title_snapshot = 'Manipulierter Titel' WHERE id = $1::uuid",
    )
    .bind(&conversation_id)
    .execute(&pool)
    .await
    .expect_err("PostgreSQL must reject direct updates of an archived conversation record");
    assert_eq!(
        direct_archive_update_error
            .as_database_error()
            .and_then(|error| error.constraint()),
        Some("domain_conversations_history_guard")
    );

    let direct_archive_delete_error =
        sqlx::query("DELETE FROM domain_conversations WHERE id = $1::uuid")
            .bind(&conversation_id)
            .execute(&pool)
            .await
            .expect_err("PostgreSQL must reject deletion of an archived conversation record");
    assert_eq!(
        direct_archive_delete_error
            .as_database_error()
            .and_then(|error| error.constraint()),
        Some("domain_conversations_history_guard")
    );
    let archive_still_exists: bool = sqlx::query_scalar(
        "SELECT EXISTS (SELECT 1 FROM domain_conversations WHERE id = $1::uuid)",
    )
    .bind(&conversation_id)
    .fetch_one(&pool)
    .await
    .expect("read archive after rejected direct deletion");
    assert!(archive_still_exists);

    database.cleanup().await;
}

#[tokio::test]
#[serial]
#[ignore = "requires direct PostgreSQL"]
async fn governance_conversation_write_gate_blocks_legacy_and_allows_canonical_http_mutations() {
    const PROPOSAL_ID: &str = "83000000-0000-4000-8000-000000000001";
    const LEGACY_MESSAGE_ID: &str = "83000000-0000-4000-8000-000000000002";
    const LEGACY_IDEMPOTENCY_KEY: &str = "83000000-0000-4000-8000-000000000003";
    const CANONICAL_IDEMPOTENCY_KEY: &str = "83000000-0000-4000-8000-000000000004";

    let database = pool().await;
    let pool = database.app_pool();
    sqlx::query("UPDATE domain_conversation_cutover_state SET governance_source = 'legacy', updated_at = NOW() WHERE singleton")
        .execute(&pool)
        .await
        .expect("reset governance source");
    seed_account(&pool, AUTHOR_ID, "Autorin", "weber").await;
    sqlx::query(
        "INSERT INTO governance_proposals (
             id, kind, webgemeindezentrum_id, applicant_account_id, applicant_title, summary, status,
             created_at, consent_until
         ) VALUES (
             $1::uuid, 'weberantrag', 'webgemeindezentrum-hammer-park', $2, 'Autorin', 'HTTP Cutover Proof',
             'consent', NOW(), NOW() + INTERVAL '7 days'
         )",
    )
    .bind(PROPOSAL_ID)
    .bind(AUTHOR_ID)
    .execute(&pool)
    .await
    .expect("insert governance proposal");

    let conversation_id: String = sqlx::query_scalar(
        "SELECT id::text FROM domain_conversations WHERE proposal_id = $1::uuid",
    )
    .bind(PROPOSAL_ID)
    .fetch_one(&pool)
    .await
    .expect("read governance conversation");

    let (app, author, _, _, _) = app(pool.clone()).await;
    let message_path = format!("/conversations/{conversation_id}/messages");

    let (status, legacy_create) = json_response(
        &app,
        request(
            "POST",
            &message_path,
            Some(&author),
            Some(r#"{"content":"Noch Legacy"}"#),
            Some(CANONICAL_IDEMPOTENCY_KEY),
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(legacy_create["code"], "conversation_write_not_active");

    sqlx::query(
        "INSERT INTO domain_messages (
             id, conversation_id, author_account_id, author_title, content, idempotency_key
         ) VALUES ($1::uuid, $2::uuid, $3, 'Autorin', 'Vorhandener Testbeitrag', $4::uuid)",
    )
    .bind(LEGACY_MESSAGE_ID)
    .bind(&conversation_id)
    .bind(AUTHOR_ID)
    .bind(LEGACY_IDEMPOTENCY_KEY)
    .execute(&pool)
    .await
    .expect("seed governance message for legacy mutation gate");

    let legacy_version: chrono::DateTime<chrono::Utc> =
        sqlx::query_scalar("SELECT updated_at FROM domain_messages WHERE id = $1::uuid")
            .bind(LEGACY_MESSAGE_ID)
            .fetch_one(&pool)
            .await
            .expect("read legacy message version");
    let legacy_item_path = format!("{message_path}/{LEGACY_MESSAGE_ID}");
    for method in ["PATCH", "DELETE"] {
        let body = if method == "PATCH" {
            Some(r#"{"content":"Darf noch nicht mutieren"}"#)
        } else {
            None
        };
        let (status, blocked) = json_response(
            &app,
            request(
                method,
                &legacy_item_path,
                Some(&author),
                body,
                None,
                Some(&legacy_version.to_rfc3339()),
            ),
        )
        .await;
        assert_eq!(
            status,
            StatusCode::CONFLICT,
            "{method} must be blocked on legacy"
        );
        assert_eq!(blocked["code"], "conversation_write_not_active");
    }

    sqlx::query("UPDATE domain_conversation_cutover_state SET governance_source = 'canonical', updated_at = NOW() WHERE singleton")
        .execute(&pool)
        .await
        .expect("enable canonical governance source");

    let (status, created) = json_response(
        &app,
        request(
            "POST",
            &message_path,
            Some(&author),
            Some(r#"{"content":"Kanonischer Beitrag"}"#),
            Some(CANONICAL_IDEMPOTENCY_KEY),
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED);

    let canonical_item_path = format!(
        "{message_path}/{}",
        created["id"].as_str().expect("message id")
    );
    let (status, updated) = json_response(
        &app,
        request(
            "PATCH",
            &canonical_item_path,
            Some(&author),
            Some(r#"{"content":"Kanonisch geändert"}"#),
            None,
            created["updated_at"].as_str(),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(updated["content"], "Kanonisch geändert");

    let (status, deleted) = json_response(
        &app,
        request(
            "DELETE",
            &canonical_item_path,
            Some(&author),
            None,
            None,
            updated["updated_at"].as_str(),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert!(deleted["deleted_at"].is_string());

    // CI provides a fresh disposable PostgreSQL database. Historical conversation and message
    // fixtures intentionally remain because the production contract forbids physical cleanup.
    sqlx::query("UPDATE domain_conversation_cutover_state SET governance_source = 'legacy', updated_at = NOW() WHERE singleton")
        .execute(&pool)
        .await
        .expect("restore legacy governance source");
    database.cleanup().await;
}

#[tokio::test]
#[serial]
#[ignore = "requires direct PostgreSQL"]
async fn private_direct_conversation_enforces_participants_unread_block_and_identity_exit() {
    const FIRST_KEY: &str = "84000000-0000-4000-8000-000000000001";
    const SECOND_KEY: &str = "84000000-0000-4000-8000-000000000002";
    const OUTSIDER_KEY: &str = "84000000-0000-4000-8000-000000000003";
    const ADMIN_PARTICIPANT_KEY: &str = "84000000-0000-4000-8000-000000000004";
    const EXITED_COUNTERPART_KEY: &str = "84000000-0000-4000-8000-000000000005";
    const DISABLED_COUNTERPART_KEY: &str = "84000000-0000-4000-8000-000000000006";
    const CONCURRENT_DISABLED_COUNTERPART_KEY: &str = "84000000-0000-4000-8000-000000000007";

    let database = pool().await;
    let pool = database.app_pool();
    seed_account(&pool, AUTHOR_ID, "Autorin", "weber").await;
    seed_account(&pool, OTHER_ID, "Anderer Weber", "weber").await;
    seed_account(&pool, ADMIN_ID, "Administration", "admin").await;
    seed_account(&pool, GUEST_ID, "Gast", "gast").await;

    let (app, author, other, admin, _) = app(pool.clone()).await;

    let (status, anonymous_inbox) = json_response(
        &app,
        request("GET", "/direct-conversations", None, None, None, None),
    )
    .await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
    assert_eq!(anonymous_inbox["code"], "authentication_required");

    let (status, created) = json_response(
        &app,
        request(
            "POST",
            "/direct-conversations",
            Some(&author),
            Some(&format!(r#"{{"recipient_account_id":"{OTHER_ID}"}}"#)),
            None,
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED);
    assert_eq!(created["counterpart_account_id"], OTHER_ID);
    assert_eq!(created["counterpart_title"], "Anderer Weber");
    assert_eq!(created["can_send"], true);
    let conversation_id = created["id"]
        .as_str()
        .expect("private conversation id")
        .to_string();

    let (status, reverse_open) = json_response(
        &app,
        request(
            "POST",
            "/direct-conversations",
            Some(&other),
            Some(&format!(r#"{{"recipient_account_id":"{AUTHOR_ID}"}}"#)),
            None,
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(reverse_open["id"], conversation_id);

    let metadata_path = format!("/conversations/{conversation_id}");
    for cookie in [None, Some(admin.as_str())] {
        let (status, hidden) = json_response(
            &app,
            request("GET", &metadata_path, cookie, None, None, None),
        )
        .await;
        assert_eq!(status, StatusCode::NOT_FOUND);
        assert_eq!(hidden["code"], "conversation_not_found");
    }
    let (status, metadata) = json_response(
        &app,
        request("GET", &metadata_path, Some(&author), None, None, None),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(metadata["conversation_type"], "direct");
    assert_eq!(metadata["visibility"], "participants");

    let message_path = format!("/conversations/{conversation_id}/messages");
    let (status, first_message) = json_response(
        &app,
        request(
            "POST",
            &message_path,
            Some(&author),
            Some(r#"{"content":"Hallo, das ist privat."}"#),
            Some(FIRST_KEY),
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED);
    assert_eq!(first_message["content"], "Hallo, das ist privat.");

    let (status, outsider_write) = json_response(
        &app,
        request(
            "POST",
            &message_path,
            Some(&admin),
            Some(r#"{"content":"Nicht erlaubt"}"#),
            Some(OUTSIDER_KEY),
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(outsider_write["code"], "conversation_not_found");

    let (status, recipient_inbox) = json_response(
        &app,
        request(
            "GET",
            "/direct-conversations",
            Some(&other),
            None,
            None,
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(recipient_inbox["items"][0]["id"], conversation_id);
    assert_eq!(recipient_inbox["items"][0]["unread_count"], 1);
    assert_eq!(
        recipient_inbox["items"][0]["last_message_preview"],
        "Hallo, das ist privat."
    );

    let (status, recipient_messages) = json_response(
        &app,
        request("GET", &message_path, Some(&other), None, None, None),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        recipient_messages["items"][0]["content"],
        "Hallo, das ist privat."
    );

    let read_path = format!("/direct-conversations/{conversation_id}/read");
    let (status, read_body) = json_response(
        &app,
        request(
            "POST",
            &read_path,
            Some(&other),
            Some(&format!(
                r#"{{"through_message_id":"{}"}}"#,
                first_message["id"].as_str().expect("first message id")
            )),
            None,
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::NO_CONTENT);
    assert!(read_body.is_null());
    let (_, recipient_inbox) = json_response(
        &app,
        request(
            "GET",
            "/direct-conversations",
            Some(&other),
            None,
            None,
            None,
        ),
    )
    .await;
    assert_eq!(recipient_inbox["items"][0]["unread_count"], 0);

    let block_path = format!("/direct-conversations/{conversation_id}/block");
    let (status, blocked) = json_response(
        &app,
        request("PUT", &block_path, Some(&other), None, None, None),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(blocked["blocked_by_me"], true);
    assert_eq!(blocked["can_send"], false);

    // The blocked side must learn that sending is impossible before it composes a
    // message, without being told which participant blocked.
    let author_blocked_view = direct_conversation_item(&app, &author, &conversation_id).await;
    assert_eq!(author_blocked_view["blocked_by_me"], false);
    assert_eq!(
        author_blocked_view["can_send"], false,
        "a block by the counterpart must close the composer for both sides"
    );

    let (status, blocked_send) = json_response(
        &app,
        request(
            "POST",
            &message_path,
            Some(&author),
            Some(r#"{"content":"Darf nicht zugestellt werden"}"#),
            Some(SECOND_KEY),
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN);
    assert_eq!(blocked_send["code"], "direct_conversation_blocked");

    let (status, unblocked) = json_response(
        &app,
        request("DELETE", &block_path, Some(&other), None, None, None),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(unblocked["blocked_by_me"], false);
    assert_eq!(unblocked["can_send"], true);
    assert_eq!(
        direct_conversation_item(&app, &author, &conversation_id).await["can_send"],
        true
    );
    let (status, _) = json_response(
        &app,
        request(
            "POST",
            &message_path,
            Some(&author),
            Some(r#"{"content":"Wieder erlaubt"}"#),
            Some(SECOND_KEY),
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED);
    let (status, after_boundary_message) = json_response(
        &app,
        request(
            "GET",
            "/direct-conversations",
            Some(&other),
            None,
            None,
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        after_boundary_message["items"][0]["unread_count"], 1,
        "the read marker must stop at the exact loaded message boundary"
    );

    // A deactivated counterpart is a factual exit: the projection already hides it,
    // so the write path must refuse the send instead of accepting a message nobody
    // can receive.
    set_account_disabled(&pool, OTHER_ID, true).await;
    let deactivated_view = direct_conversation_item(&app, &author, &conversation_id).await;
    assert!(deactivated_view["counterpart_account_id"].is_null());
    assert_eq!(deactivated_view["counterpart_title"], "Anderer Weber");
    assert_eq!(deactivated_view["can_send"], false);
    let (status, deactivated_send) = json_response(
        &app,
        request(
            "POST",
            &message_path,
            Some(&author),
            Some(r#"{"content":"Darf ein stillgelegtes Konto nicht erreichen"}"#),
            Some(DISABLED_COUNTERPART_KEY),
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(
        deactivated_send["code"],
        "direct_conversation_counterpart_unavailable"
    );
    set_account_disabled(&pool, OTHER_ID, false).await;
    assert_eq!(
        direct_conversation_item(&app, &author, &conversation_id).await["can_send"],
        true,
        "reactivating the counterpart restores the private conversation"
    );

    // A deactivation that is already in flight must serialize with delivery. Without
    // an account-row lock, the send observes the previously committed active version
    // and can commit a message before the deactivation transaction commits.
    let mut deactivation = pool.begin().await.expect("begin concurrent deactivation");
    sqlx::query("UPDATE domain_accounts SET disabled = TRUE WHERE id = $1")
        .bind(OTHER_ID)
        .execute(&mut *deactivation)
        .await
        .expect("stage concurrent counterpart deactivation");
    let race_app = app.clone();
    let race_author = author.clone();
    let race_message_path = message_path.clone();
    let mut concurrent_send = tokio::spawn(async move {
        json_response(
            &race_app,
            request(
                "POST",
                &race_message_path,
                Some(&race_author),
                Some(r#"{"content":"Darf die Deaktivierung nicht überholen"}"#),
                Some(CONCURRENT_DISABLED_COUNTERPART_KEY),
                None,
            ),
        )
        .await
    });
    assert!(
        tokio::time::timeout(std::time::Duration::from_millis(200), &mut concurrent_send)
            .await
            .is_err(),
        "delivery must wait for the in-flight account lifecycle decision"
    );
    deactivation
        .commit()
        .await
        .expect("commit concurrent counterpart deactivation");
    let (status, concurrent_deactivated_send) =
        tokio::time::timeout(std::time::Duration::from_secs(5), concurrent_send)
            .await
            .expect("concurrent send must finish after deactivation commits")
            .expect("concurrent send task");
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(
        concurrent_deactivated_send["code"],
        "direct_conversation_counterpart_unavailable"
    );
    set_account_disabled(&pool, OTHER_ID, false).await;

    let (status, admin_conversation) = json_response(
        &app,
        request(
            "POST",
            "/direct-conversations",
            Some(&author),
            Some(&format!(r#"{{"recipient_account_id":"{ADMIN_ID}"}}"#)),
            None,
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED);
    let admin_conversation_id = admin_conversation["id"]
        .as_str()
        .expect("admin participant conversation id");
    let admin_message_path = format!("/conversations/{admin_conversation_id}/messages");
    let (status, author_message) = json_response(
        &app,
        request(
            "POST",
            &admin_message_path,
            Some(&author),
            Some(r#"{"content":"Auch ein beteiligter Administrator ist nicht mein Autor."}"#),
            Some(ADMIN_PARTICIPANT_KEY),
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED);
    let admin_delete_path = format!(
        "/conversations/{admin_conversation_id}/messages/{}",
        author_message["id"].as_str().expect("author message id")
    );
    let (status, admin_delete) = json_response(
        &app,
        request(
            "DELETE",
            &admin_delete_path,
            Some(&admin),
            None,
            None,
            author_message["updated_at"].as_str(),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN);
    assert_eq!(admin_delete["code"], "message_author_required");

    sqlx::query("DELETE FROM domain_accounts WHERE id = $1")
        .bind(OTHER_ID)
        .execute(&pool)
        .await
        .expect("remove direct-message counterpart from canonical domain store");
    let (status, after_exit) = json_response(
        &app,
        request(
            "GET",
            "/direct-conversations",
            Some(&author),
            None,
            None,
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let exited_conversation = after_exit["items"]
        .as_array()
        .expect("private inbox items")
        .iter()
        .find(|item| item["id"].as_str() == Some(conversation_id.as_str()))
        .expect("conversation with exited counterpart");
    assert!(exited_conversation["counterpart_account_id"].is_null());
    assert_eq!(exited_conversation["counterpart_title"], "Anderer Weber");
    assert_eq!(exited_conversation["can_send"], false);

    let (status, exited_send) = json_response(
        &app,
        request(
            "POST",
            &message_path,
            Some(&author),
            Some(r#"{"content":"Darf kein aufgelöstes Konto erreichen"}"#),
            Some(EXITED_COUNTERPART_KEY),
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(
        exited_send["code"],
        "direct_conversation_counterpart_unavailable"
    );

    seed_account(&pool, OTHER_ID, "Neues Konto mit alter Kennung", "weber").await;
    let (status, retired_pair) = json_response(
        &app,
        request(
            "POST",
            "/direct-conversations",
            Some(&author),
            Some(&format!(r#"{{"recipient_account_id":"{OTHER_ID}"}}"#)),
            None,
            None,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(retired_pair["code"], "direct_conversation_pair_retired");

    database.cleanup().await;
}
