mod support;

use std::{path::PathBuf, sync::Arc};

use axum::{body, http::Request, Router};
use serial_test::serial;
use sqlx::{Executor, PgPool};
use tokio::sync::RwLock;
use tower::ServiceExt;
use weltgewebe_api::{
    auth::{accounts::AccountStore, rate_limit::AuthRateLimiter, session::SessionBackend},
    config::{
        AppConfig, DomainAccountWriteSource, DomainEdgeWriteSource, DomainNodeWriteSource,
        DomainReadSource, PasskeyCredentialSource,
    },
    domain_db::{
        load_accounts_from_postgres, load_edges_from_postgres, load_nodes_bbox_from_postgres,
        load_nodes_from_postgres,
    },
    routes::{accounts::GarnrolleMapState, api_router},
    state::{ApiState, OrderedCache},
    telemetry::{BuildInfo, Metrics},
    test_helpers::EnvGuard,
};

async fn direct_pool() -> PgPool {
    let url = std::env::var("DATABASE_URL")
        .expect("DATABASE_URL must point at a direct PostgreSQL database");
    support::postgres_proof::assert_direct_disposable_database_url(&url);
    PgPool::connect(&url).await.expect("connect to PostgreSQL")
}

async fn run_migrations(pool: &PgPool) {
    let migrations_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("migrations");
    let migrator = sqlx::migrate::Migrator::new(migrations_dir)
        .await
        .expect("failed to load migrations");
    migrator.run(pool).await.expect("failed to run migrations");
}

async fn clean(pool: &PgPool) {
    pool.execute("DELETE FROM domain_edges WHERE id LIKE 'rp-%'")
        .await
        .expect("clean domain_edges");
    pool.execute("DELETE FROM domain_nodes WHERE id LIKE 'rp-%'")
        .await
        .expect("clean domain_nodes");
    pool.execute("DELETE FROM domain_accounts WHERE id LIKE 'rp-%'")
        .await
        .expect("clean domain_accounts");
}

// Each DB proof test starts from a clean rp-* fixture namespace.
async fn prepare_pool() -> PgPool {
    let pool = direct_pool().await;
    run_migrations(&pool).await;
    clean(&pool).await;
    pool
}

fn postgres_bbox_route_state(pool: PgPool) -> ApiState {
    let config = AppConfig {
        max_guest_owned_nodes: 1_000,
        domain_read_source: DomainReadSource::Postgres,
        domain_account_write_source: DomainAccountWriteSource::Postgres,
        domain_node_write_source: DomainNodeWriteSource::Postgres,
        domain_edge_write_source: DomainEdgeWriteSource::Postgres,
        passkey_credential_source: PasskeyCredentialSource::InMemory,
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
    };
    let metrics = Metrics::try_new(BuildInfo {
        version: "test",
        commit: "test",
        build_timestamp: "test",
    })
    .expect("test metrics");
    let rate_limiter = Arc::new(AuthRateLimiter::new(&config));

    ApiState {
        db_pool: Some(pool),
        db_pool_configured: true,
        nats_client: None,
        nats_configured: false,
        config,
        metrics,
        sessions: SessionBackend::new_in_memory(),
        challenges: Default::default(),
        tokens: weltgewebe_api::auth::tokens::TokenStore::new(),
        step_up_tokens: weltgewebe_api::auth::step_up_tokens::StepUpTokenStore::new(),
        accounts: Arc::new(RwLock::new(AccountStore::new())),
        // Deliberately empty: this proof fails on the old O(N) cache-backed route.
        nodes: Arc::new(RwLock::new(OrderedCache::new())),
        nodes_persist: Arc::new(tokio::sync::Mutex::new(())),
        accounts_persist: Arc::new(tokio::sync::Mutex::new(())),
        domain_projection_gate: Arc::new(RwLock::new(())),
        domain_projection_reload: std::sync::Arc::new(tokio::sync::Mutex::new(())),
        domain_projection_local_node_patch_handoff: std::sync::Arc::new(
            std::sync::atomic::AtomicI64::new(-1),
        ),
        domain_projection_version: Arc::new(std::sync::atomic::AtomicI64::new(0)),
        edges: Arc::new(RwLock::new(OrderedCache::new())),
        rate_limiter,
        mailer: None,
        webauthn: None,
        passkey_registrations: Default::default(),
        passkey_registration_grants: Default::default(),
        passkey_authentications: Default::default(),
        passkeys: Default::default(),
        web_push: None,
    }
}

#[tokio::test]
#[ignore]
#[serial]
async fn node_bbox_route_reads_postgres_directly_with_cursor_contract() {
    let pool = prepare_pool().await;
    sqlx::query(
        "INSERT INTO domain_nodes (id, kind, title, lat, lon, payload) VALUES \
         ('rp-a-c', 'place', 'A-C', 53.50, 9.90, '{}'::jsonb), \
         ('rp-ab', 'place', 'AB', 53.55, 9.95, '{}'::jsonb), \
         ('rp-z', 'place', 'Z', 53.60, 10.00, '{}'::jsonb), \
         ('rp-bbox-outside', 'place', 'Outside', 54.50, 11.00, '{}'::jsonb)",
    )
    .execute(&pool)
    .await
    .expect("insert BBOX nodes");

    let direct = load_nodes_bbox_from_postgres(&pool, 53.4, 53.7, 9.8, 10.1, 10, 0)
        .await
        .expect("direct BBOX query");
    let direct_ids = direct
        .iter()
        .map(|node| node.id.clone())
        .collect::<Vec<_>>();
    assert_eq!(direct_ids.len(), 3);

    let app = Router::new()
        .merge(api_router())
        .with_state(postgres_bbox_route_state(pool.clone()));

    // The process node cache is deliberately empty. The BBOX route below must
    // still return PostgreSQL rows directly; focused node details intentionally
    // remain on the version-fenced projection cache and are not part of this proof.
    let response = app
        .clone()
        .oneshot(
            Request::get("/nodes?bbox=9.8,53.4,10.1,53.7&pagination=cursor&limit=2")
                .body(body::Body::empty())
                .unwrap(),
        )
        .await
        .expect("first BBOX route page");
    assert_eq!(response.status(), axum::http::StatusCode::OK);
    let first_body = body::to_bytes(response.into_body(), usize::MAX)
        .await
        .expect("first BBOX body");
    let first: serde_json::Value = serde_json::from_slice(&first_body).expect("first BBOX JSON");
    assert_eq!(first["items"].as_array().expect("items").len(), 2);
    assert_eq!(first["items"][0]["id"], direct_ids[0]);
    assert_eq!(first["items"][1]["id"], direct_ids[1]);
    assert_eq!(first["page"]["has_more"], true);
    let cursor = first["page"]["next_cursor"]
        .as_str()
        .expect("next cursor")
        .to_string();

    let response = app
        .clone()
        .oneshot(
            Request::get(format!(
                "/nodes?bbox=9.8,53.4,10.1,53.7&pagination=cursor&limit=2&cursor={cursor}"
            ))
            .body(body::Body::empty())
            .unwrap(),
        )
        .await
        .expect("second BBOX route page");
    assert_eq!(response.status(), axum::http::StatusCode::OK);
    let second_body = body::to_bytes(response.into_body(), usize::MAX)
        .await
        .expect("second BBOX body");
    let second: serde_json::Value = serde_json::from_slice(&second_body).expect("second BBOX JSON");
    assert_eq!(second["items"].as_array().expect("items").len(), 1);
    assert_eq!(second["items"][0]["id"], direct_ids[2]);
    assert_eq!(second["page"]["has_more"], false);
    assert!(second["page"]["next_cursor"].is_null());

    let response = app
        .oneshot(
            Request::get("/nodes?bbox=9.8,53.4,10.1,53.7&offset=18446744073709551615")
                .body(body::Body::empty())
                .unwrap(),
        )
        .await
        .expect("oversized legacy BBOX offset response");
    assert_eq!(
        response.status(),
        axum::http::StatusCode::BAD_REQUEST,
        "an offset outside PostgreSQL's signed 64-bit range is client input, not a server failure",
    );

    clean(&pool).await;
}

#[tokio::test]
#[ignore]
#[serial]
async fn nodes_loader_reconstructs_public_shape() {
    let pool = prepare_pool().await;
    sqlx::query(
        "INSERT INTO domain_nodes (id, kind, title, lat, lon, payload) \
         VALUES ('rp-node-a', 'place', 'Read Path Node', 53.55, 9.99, \
         '{\"summary\":\"Summary\",\"info\":\"Info\",\"tags\":[\"a\",\"b\"]}'::jsonb)",
    )
    .execute(&pool)
    .await
    .expect("insert node");

    let cache = load_nodes_from_postgres(&pool).await.expect("load nodes");
    let node = cache.get("rp-node-a").expect("node present");

    assert_eq!(node.title, "Read Path Node");
    assert_eq!(node.summary.as_deref(), Some("Summary"));
    assert_eq!(node.tags, vec!["a".to_string(), "b".to_string()]);
    clean(&pool).await;
}

#[tokio::test]
#[ignore]
#[serial]
async fn edges_loader_respects_max_edges_cache_limit() {
    let pool = prepare_pool().await;
    let _limit = EnvGuard::set("MAX_EDGES_CACHE", "1");
    for id in ["rp-edge-a", "rp-edge-b"] {
        sqlx::query(
            "INSERT INTO domain_edges (id, source_id, target_id, edge_kind, payload) \
             VALUES ($1, 'rp-node-a', 'rp-node-b', 'relates', '{}'::jsonb)",
        )
        .bind(id)
        .execute(&pool)
        .await
        .expect("insert edge");
    }

    let cache = load_edges_from_postgres(&pool).await.expect("load edges");

    assert!(
        cache.len() <= 1,
        "with MAX_EDGES_CACHE=1, loader must materialise at most one edge"
    );
    clean(&pool).await;
}

#[tokio::test]
#[ignore]
#[serial]
async fn edges_loader_skips_malformed_rows_without_losing_later_valid_edges() {
    let pool = prepare_pool().await;
    let _limit = EnvGuard::set("MAX_EDGES_CACHE", "2");
    // Sort before the valid rows (id ascending) and carry a structurally
    // malformed expires_at payload (neither absent, null, nor a string).
    for id in ["rp-edge-a", "rp-edge-b"] {
        sqlx::query(
            "INSERT INTO domain_edges (id, source_id, target_id, edge_kind, payload) \
             VALUES ($1, 'rp-node-a', 'rp-node-b', 'relates', '{\"expires_at\": 12345}'::jsonb)",
        )
        .bind(id)
        .execute(&pool)
        .await
        .expect("insert malformed edge");
    }
    for id in ["rp-edge-c", "rp-edge-d"] {
        sqlx::query(
            "INSERT INTO domain_edges (id, source_id, target_id, edge_kind, payload) \
             VALUES ($1, 'rp-node-a', 'rp-node-b', 'relates', '{}'::jsonb)",
        )
        .bind(id)
        .execute(&pool)
        .await
        .expect("insert valid edge");
    }

    let cache = load_edges_from_postgres(&pool).await.expect("load edges");

    assert_eq!(
        cache.len(),
        2,
        "two malformed rows sorting before two valid rows must not prevent \
         both later valid edges from loading, even though the cache limit \
         equals the number of valid edges"
    );
    assert!(cache.get("rp-edge-c").is_some());
    assert!(cache.get("rp-edge-d").is_some());
    clean(&pool).await;
}

#[tokio::test]
#[ignore]
#[serial]
async fn edges_loader_excludes_permanently_unreachable_rows_from_cache_capacity() {
    let pool = prepare_pool().await;
    let _limit = EnvGuard::set("MAX_EDGES_CACHE", "2");
    // A dated created_at paired with an explicit `expires_at: null` is
    // structurally well-formed (not caught by payload_lifecycle_field's
    // malformed check) but non-canonical: edge_is_active_at rejects it for
    // every `now`. It must not consume one of the two cache slots.
    for id in ["rp-edge-a", "rp-edge-b"] {
        sqlx::query(
            "INSERT INTO domain_edges (id, source_id, target_id, edge_kind, created_at, payload) \
             VALUES ($1, 'rp-node-a', 'rp-node-b', 'relates', '2026-06-01T00:00:00Z', \
             '{\"expires_at\": null}'::jsonb)",
        )
        .bind(id)
        .execute(&pool)
        .await
        .expect("insert permanently unreachable edge");
    }
    for id in ["rp-edge-c", "rp-edge-d"] {
        sqlx::query(
            "INSERT INTO domain_edges (id, source_id, target_id, edge_kind, payload) \
             VALUES ($1, 'rp-node-a', 'rp-node-b', 'relates', '{}'::jsonb)",
        )
        .bind(id)
        .execute(&pool)
        .await
        .expect("insert valid edge");
    }

    let cache = load_edges_from_postgres(&pool).await.expect("load edges");

    assert_eq!(
        cache.len(),
        2,
        "permanently unreachable rows sorting before valid rows must not \
         prevent both later valid edges from loading"
    );
    assert!(cache.get("rp-edge-c").is_some());
    assert!(cache.get("rp-edge-d").is_some());
    assert!(cache.get("rp-edge-a").is_none());
    assert!(cache.get("rp-edge-b").is_none());
    clean(&pool).await;
}

#[tokio::test]
#[ignore]
#[serial]
async fn accounts_loader_rebuilds_email_index_and_rejects_removed_fields() {
    let pool = prepare_pool().await;
    sqlx::query(
        "INSERT INTO domain_accounts \
         (id, kind, title, map_state, radius_m, disabled, location_lat, location_lon, role, email, public_payload, private_payload) \
         VALUES \
         ('rp-account-a', 'garnrolle', 'Visible', 'exact', 0, false, 53.55, 9.99, 'gast', 'ReadPath@Example.test', '{\"summary\":\"Public\"}'::jsonb, '{}'::jsonb), \
         ('rp-account-b', 'garnrolle', 'Suppressed', 'exact', 0, false, 53.56, 9.98, 'gast', NULL, '{}'::jsonb, '{\"suppress_public_pos\":true}'::jsonb)",
    )
    .execute(&pool)
    .await
    .expect("insert accounts");

    let store = load_accounts_from_postgres(&pool)
        .await
        .expect("load accounts");
    let visible = store
        .get_by_email("readpath@example.test")
        .expect("case-insensitive email lookup");
    assert_eq!(visible.public.summary.as_deref(), Some("Public"));
    assert!(visible.public.public_pos.is_some());
    assert!(
        store.get("rp-account-b").is_none(),
        "removed suppress_public_pos field must reject the row"
    );
    clean(&pool).await;
}

#[tokio::test]
#[ignore]
#[serial]
async fn accounts_loader_requires_valid_private_radius_binding() {
    let pool = prepare_pool().await;
    sqlx::query(
        "INSERT INTO domain_accounts \
         (id, kind, title, map_state, radius_m, disabled, location_lat, location_lon, role, public_payload, private_payload) \
         VALUES \
         ('rp-account-approximate', 'garnrolle', 'Legacy approximate', 'radius', 250, false, 53.55, 9.99, 'gast', '{}'::jsonb, '{\"visibility\":\"approximate\"}'::jsonb), \
         ('rp-account-safe-radius', 'garnrolle', 'Safe radius', 'radius', 250, false, 53.55, 9.99, 'gast', '{}'::jsonb, \
          '{\"radius_projection\":{\"version\":1,\"anchor\":{\"lat\":53.55,\"lon\":9.99},\"radius_m\":250,\"public_pos\":{\"lat\":53.5505,\"lon\":9.99}}}'::jsonb)",
    )
    .execute(&pool)
    .await
    .expect("insert radius accounts");

    let store = load_accounts_from_postgres(&pool)
        .await
        .expect("load accounts");
    let safe = store
        .get("rp-account-safe-radius")
        .expect("safe radius account present");

    assert!(
        store.get("rp-account-approximate").is_none(),
        "removed visibility field must reject the radius row"
    );

    assert_eq!(safe.public.map_state, GarnrolleMapState::Radius);
    assert_eq!(safe.public.radius_m, 250);
    let public_pos = safe.public.public_pos.as_ref().expect("safe public point");
    assert!((public_pos.lat - 53.5505).abs() < 1e-9);
    assert!((public_pos.lon - 9.99).abs() < 1e-9);
    let public_json = serde_json::to_value(&safe.public).expect("serialize safe account");
    assert!(public_json.get("radius_projection").is_none());
    assert!(public_json.get("location").is_none());
    clean(&pool).await;
}

#[tokio::test]
#[ignore]
#[serial]
async fn accounts_loader_rejects_removed_private_visibility() {
    let pool = prepare_pool().await;
    sqlx::query(
        "INSERT INTO domain_accounts \
         (id, kind, title, map_state, radius_m, disabled, location_lat, location_lon, role, public_payload, private_payload) \
         VALUES \
         ('rp-account-private', 'garnrolle', 'Private', 'exact', 0, false, 53.55, 9.99, 'gast', '{}'::jsonb, '{\"visibility\":\"private\"}'::jsonb)",
    )
    .execute(&pool)
    .await
    .expect("insert private account");

    let store = load_accounts_from_postgres(&pool)
        .await
        .expect("load accounts");
    assert!(
        store.get("rp-account-private").is_none(),
        "removed private visibility field must reject the row"
    );
    clean(&pool).await;
}

#[tokio::test]
#[ignore]
#[serial]
async fn accounts_loader_explicit_not_on_map_hides_internal_location() {
    let pool = prepare_pool().await;
    sqlx::query(
        "INSERT INTO domain_accounts \
         (id, kind, title, map_state, radius_m, disabled, location_lat, location_lon, role, public_payload, private_payload) \
         VALUES \
         ('rp-account-explicit-hidden', 'garnrolle', 'Explicitly Hidden', 'not_on_map', 0, false, 53.55, 9.99, 'gast', '{}'::jsonb, '{}'::jsonb)",
    )
    .execute(&pool)
    .await
    .expect("insert explicitly hidden account");

    let store = load_accounts_from_postgres(&pool)
        .await
        .expect("load accounts");
    let account = store
        .get("rp-account-explicit-hidden")
        .expect("explicitly hidden account present");

    assert_eq!(account.public.map_state, GarnrolleMapState::NotOnMap);
    assert_eq!(account.public.radius_m, 0);
    assert!(account.public.public_pos.is_none());
    let public_json = serde_json::to_value(&account.public).expect("serialize public account");
    assert!(public_json.get("location").is_none());
    assert!(public_json.get("public_pos").is_none());
    clean(&pool).await;
}

#[tokio::test]
#[ignore]
#[serial]
async fn accounts_loader_rejects_removed_ron_flag() {
    let pool = prepare_pool().await;
    sqlx::query(
        "INSERT INTO domain_accounts \
         (id, kind, title, map_state, radius_m, disabled, location_lat, location_lon, role, public_payload, private_payload) \
         VALUES \
         ('rp-account-ron-flag', 'garnrolle', 'RoN Flag', 'exact', 0, false, 53.55, 9.99, 'gast', '{}'::jsonb, '{\"ron_flag\":true}'::jsonb)",
    )
    .execute(&pool)
    .await
    .expect("insert ron-flag account");

    let store = load_accounts_from_postgres(&pool)
        .await
        .expect("load accounts");
    assert!(
        store.get("rp-account-ron-flag").is_none(),
        "removed ron_flag must reject the row"
    );
    clean(&pool).await;
}

#[tokio::test]
#[ignore]
#[serial]
async fn empty_tables_with_only_fixtures_deleted_do_not_fail() {
    let pool = prepare_pool().await;

    let _nodes = load_nodes_from_postgres(&pool).await.expect("load nodes");
    let _edges = load_edges_from_postgres(&pool).await.expect("load edges");
    let _accounts = load_accounts_from_postgres(&pool)
        .await
        .expect("load accounts");
}

#[tokio::test]
#[ignore]
#[serial]
async fn jsonl_postgres_legacy_list_order_gap_diagnostic() {
    let pool = prepare_pool().await;
    let temp_dir = tempfile::tempdir().expect("create temp dir");
    let _env = EnvGuard::set("GEWEBE_IN_DIR", temp_dir.path().to_str().unwrap());

    // 1. Prepare non-ID-sorted JSONL fixtures (c, a, b order)
    let nodes_jsonl = "\
{\"id\":\"rp-list-node-c\",\"kind\":\"place\",\"title\":\"C\",\"location\":{\"lat\":0.0,\"lon\":0.0},\"payload\":{}}
{\"id\":\"rp-list-node-a\",\"kind\":\"place\",\"title\":\"A\",\"location\":{\"lat\":0.0,\"lon\":0.0},\"payload\":{}}
{\"id\":\"rp-list-node-b\",\"kind\":\"place\",\"title\":\"B\",\"location\":{\"lat\":0.0,\"lon\":0.0},\"payload\":{}}
";
    tokio::fs::write(temp_dir.path().join("demo.nodes.jsonl"), nodes_jsonl)
        .await
        .unwrap();

    let edges_jsonl = "\
{\"id\":\"rp-list-edge-c\",\"source_id\":\"rp-list-node-c\",\"target_id\":\"rp-list-node-a\",\"edge_kind\":\"relates\",\"payload\":{}}
{\"id\":\"rp-list-edge-a\",\"source_id\":\"rp-list-node-a\",\"target_id\":\"rp-list-node-b\",\"edge_kind\":\"relates\",\"payload\":{}}
{\"id\":\"rp-list-edge-b\",\"source_id\":\"rp-list-node-b\",\"target_id\":\"rp-list-node-c\",\"edge_kind\":\"relates\",\"payload\":{}}
";
    tokio::fs::write(temp_dir.path().join("demo.edges.jsonl"), edges_jsonl)
        .await
        .unwrap();

    let accounts_jsonl = "\
{\"id\":\"rp-list-account-c\",\"type\":\"garnrolle\",\"title\":\"C\",\"map_state\":\"not_on_map\",\"role\":\"gast\",\"email\":\"rp-list-account-c@example.invalid\"}
{\"id\":\"rp-list-account-a\",\"type\":\"garnrolle\",\"title\":\"A\",\"map_state\":\"not_on_map\",\"role\":\"gast\",\"email\":\"rp-list-account-a@example.invalid\"}
{\"id\":\"rp-list-account-b\",\"type\":\"garnrolle\",\"title\":\"B\",\"map_state\":\"not_on_map\",\"role\":\"gast\",\"email\":\"rp-list-account-b@example.invalid\"}
";
    tokio::fs::write(temp_dir.path().join("demo.accounts.jsonl"), accounts_jsonl)
        .await
        .unwrap();

    // 2. Prepare PostgreSQL fixtures (inserted in c, a, b order)
    sqlx::query(
        "INSERT INTO domain_nodes (id, kind, title, lat, lon, payload) \
         VALUES \
         ('rp-list-node-c', 'place', 'C', 0.0, 0.0, '{}'::jsonb), \
         ('rp-list-node-a', 'place', 'A', 0.0, 0.0, '{}'::jsonb), \
         ('rp-list-node-b', 'place', 'B', 0.0, 0.0, '{}'::jsonb)",
    )
    .execute(&pool)
    .await
    .expect("insert nodes");

    sqlx::query(
        "INSERT INTO domain_edges (id, source_id, target_id, edge_kind, payload) \
         VALUES \
         ('rp-list-edge-c', 'rp-list-node-c', 'rp-list-node-a', 'relates', '{}'::jsonb), \
         ('rp-list-edge-a', 'rp-list-node-a', 'rp-list-node-b', 'relates', '{}'::jsonb), \
         ('rp-list-edge-b', 'rp-list-node-b', 'rp-list-node-c', 'relates', '{}'::jsonb)",
    )
    .execute(&pool)
    .await
    .expect("insert edges");

    sqlx::query(
        "INSERT INTO domain_accounts \
         (id, kind, title, map_state, role, email, public_payload, private_payload) \
         VALUES \
         ('rp-list-account-c', 'garnrolle', 'C', 'not_on_map', 'gast', 'rp-list-account-c@example.invalid', '{}'::jsonb, '{}'::jsonb), \
         ('rp-list-account-a', 'garnrolle', 'A', 'not_on_map', 'gast', 'rp-list-account-a@example.invalid', '{}'::jsonb, '{}'::jsonb), \
         ('rp-list-account-b', 'garnrolle', 'B', 'not_on_map', 'gast', 'rp-list-account-b@example.invalid', '{}'::jsonb, '{}'::jsonb)"
    )
    .execute(&pool)
    .await
    .expect("insert accounts");

    // 3. Execute Loaders
    let jsonl_nodes = weltgewebe_api::routes::nodes::load_nodes().await;
    let pg_nodes = load_nodes_from_postgres(&pool).await.unwrap();

    let jsonl_edges = weltgewebe_api::routes::edges::load_edges().await;
    let pg_edges = load_edges_from_postgres(&pool).await.unwrap();

    let jsonl_accounts = weltgewebe_api::routes::accounts::load_all_accounts()
        .await
        .expect("load canonical JSONL accounts");
    let pg_accounts = load_accounts_from_postgres(&pool).await.unwrap();

    // 4. Assert Diagnostic Outcomes
    // Nodes: Legacy JSONL loader retains file order. PG loader uses ORDER BY id ASC.
    // The PostgreSQL proof database may contain unrelated local rows. Scope the
    // comparison to this diagnostic's rp-list-* fixtures so the assertion proves
    // list-order semantics, not database cleanliness.
    let jsonl_node_ids: Vec<&str> = jsonl_nodes
        .iter_in_order()
        .map(|n| n.id.as_str())
        .filter(|id| id.starts_with("rp-list-node-"))
        .collect();
    let postgres_node_ids: Vec<&str> = pg_nodes
        .iter_in_order()
        .map(|n| n.id.as_str())
        .filter(|id| id.starts_with("rp-list-node-"))
        .collect();

    // Intentional gap assertion:
    // This diagnostic records the current legacy-order mismatch between JSONL
    // file/cache order and PostgreSQL id order. When TODO 3 is resolved by
    // implementing blueprint-required legacy order preservation or by explicitly
    // revising the blueprint first, this diagnostic must be updated or replaced
    // by the final parity proof.

    assert_ne!(jsonl_node_ids, postgres_node_ids);
    assert_eq!(
        jsonl_node_ids,
        vec!["rp-list-node-c", "rp-list-node-a", "rp-list-node-b"]
    );
    assert_eq!(
        postgres_node_ids,
        vec!["rp-list-node-a", "rp-list-node-b", "rp-list-node-c"]
    );

    // Edges: Legacy JSONL loader retains file order. PG loader uses ORDER BY id ASC.
    let jsonl_edge_ids: Vec<&str> = jsonl_edges
        .iter_in_order()
        .map(|e| e.id.as_str())
        .filter(|id| id.starts_with("rp-list-edge-"))
        .collect();
    let postgres_edge_ids: Vec<&str> = pg_edges
        .iter_in_order()
        .map(|e| e.id.as_str())
        .filter(|id| id.starts_with("rp-list-edge-"))
        .collect();

    // Intentional gap assertion:
    // This diagnostic records the current legacy-order mismatch between JSONL
    // file/cache order and PostgreSQL id order. When TODO 3 is resolved by
    // implementing blueprint-required legacy order preservation or by explicitly
    // revising the blueprint first, this diagnostic must be updated or replaced
    // by the final parity proof.

    assert_ne!(jsonl_edge_ids, postgres_edge_ids);
    assert_eq!(
        jsonl_edge_ids,
        vec!["rp-list-edge-c", "rp-list-edge-a", "rp-list-edge-b"]
    );
    assert_eq!(
        postgres_edge_ids,
        vec!["rp-list-edge-a", "rp-list-edge-b", "rp-list-edge-c"]
    );

    // Accounts: AccountStore uses BTreeMap, so both loaders yield ID-ascending order.
    let jsonl_account_ids: Vec<&str> = jsonl_accounts
        .iter()
        .map(|(id, _)| id.as_str())
        .filter(|id| id.starts_with("rp-list-account-"))
        .collect();
    let postgres_account_ids: Vec<&str> = pg_accounts
        .iter()
        .map(|(id, _)| id.as_str())
        .filter(|id| id.starts_with("rp-list-account-"))
        .collect();

    assert_eq!(jsonl_account_ids, postgres_account_ids);
    assert_eq!(
        jsonl_account_ids,
        vec![
            "rp-list-account-a",
            "rp-list-account-b",
            "rp-list-account-c"
        ]
    );

    // Keep the shared proof database tidy after the successful diagnostic run.
    clean(&pool).await;
}
