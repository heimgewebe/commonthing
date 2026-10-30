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
        load_accounts_from_postgres, load_edges_from_postgres,
        load_nodes_bbox_after_id_from_postgres, load_nodes_bbox_from_postgres,
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

    let direct = load_nodes_bbox_from_postgres(&pool, 53.4, 53.7, 9.8, 10.1, 10, 0, None)
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
async fn historical_invalid_node_coordinates_do_not_abort_full_load() {
    // A NOT VALID check preserves pre-migration rows even when their complete
    // coordinate pair is out of range. This proof only uses a disposable PG URL
    // (enforced by prepare_pool), never a production schema.
    let pool = prepare_pool().await;
    sqlx::query(
        "INSERT INTO domain_nodes (id, kind, title, lat, lon, payload) VALUES \
         ('rp-valid-legacy-neighbor', 'place', 'Valid Neighbor', 53.55, 9.99, '{}'::jsonb)",
    )
    .execute(&pool)
    .await
    .expect("insert valid neighbor");

    // Reproduce the pre-migration historical state atomically: temporarily
    // remove the new CHECK, insert the old bad row, restore CHECK ... NOT VALID
    // within the same transaction before exposing anything to other sessions.
    let mut tx = pool
        .begin()
        .await
        .expect("begin legacy fixture transaction");
    sqlx::query("ALTER TABLE domain_nodes DROP CONSTRAINT domain_nodes_location_pair")
        .execute(&mut *tx)
        .await
        .expect("temporarily drop location check in disposable DB");
    sqlx::query(
        "INSERT INTO domain_nodes (id, kind, title, lat, lon, payload) VALUES \
         ('rp-invalid-historical', 'place', 'Historical Invalid', 123.45, 9.99, '{}'::jsonb)",
    )
    .execute(&mut *tx)
    .await
    .expect("insert legacy coordinate outside valid latitude");
    sqlx::query(
        "ALTER TABLE domain_nodes ADD CONSTRAINT domain_nodes_location_pair CHECK (\
          (lat IS NULL AND lon IS NULL) OR (lat IS NOT NULL AND lon IS NOT NULL \
          AND lat BETWEEN -90 AND 90 AND lon BETWEEN -180 AND 180)\
        ) NOT VALID",
    )
    .execute(&mut *tx)
    .await
    .expect("reinstate NOT VALID protection before commit");
    tx.commit().await.expect("commit protected legacy fixture");

    let nodes = load_nodes_from_postgres(&pool)
        .await
        .expect("a historical bad row must not abort a complete node load");
    assert!(nodes.get("rp-valid-legacy-neighbor").is_some());
    assert!(nodes.get("rp-invalid-historical").is_none());

    let bbox = load_nodes_bbox_from_postgres(&pool, 53.4, 53.7, 9.8, 10.1, 10, 0, None)
        .await
        .expect("valid geographic bounding box must remain usable");
    assert!(bbox
        .iter()
        .any(|node| node.id == "rp-valid-legacy-neighbor"));
    assert!(!bbox.iter().any(|node| node.id == "rp-invalid-historical"));

    // An explicit by-id read still fails visibly rather than rewriting
    // corrupt historical coordinates into a fabricated null location.
    let err = weltgewebe_api::domain_db::load_node_from_postgres(&pool, "rp-invalid-historical")
        .await
        .expect_err("direct corrupt row must remain invalid");
    assert!(matches!(
        err,
        weltgewebe_api::domain_db::NodeWriteError::Mapping(_)
    ));

    // Re-added CHECK is NOT VALID only for historical rows. New bad writes
    // must still be rejected by the database.
    assert!(sqlx::query(
        "INSERT INTO domain_nodes (id, kind, title, lat, lon, payload) VALUES \
         ('rp-new-invalid', 'place', 'Not Allowed', 123.45, 9.99, '{}'::jsonb)",
    )
    .execute(&pool)
    .await
    .is_err());
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
    let jsonl_nodes = weltgewebe_api::routes::nodes::load_nodes()
        .await
        .expect("load canonical JSONL nodes");
    let pg_nodes = load_nodes_from_postgres(&pool).await.unwrap();

    let jsonl_edges = weltgewebe_api::routes::edges::load_edges()
        .await
        .expect("load canonical JSONL edges");
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

/// Run only against the test harness disposable PostgreSQL DATABASE_URL.
/// Privacy filtering must happen in SQL before LIMIT/OFFSET and id cursors.
#[tokio::test]
#[ignore]
#[serial]
async fn postgres_bbox_private_node_is_filtered_before_limit_and_cursor() {
    let pool = prepare_pool().await;
    sqlx::query(
        "INSERT INTO domain_accounts (id,title,disabled) VALUES ('rp-private-owner','Fixture Owner',FALSE)"
    ).execute(&pool).await.expect("insert synthetic owner");
    sqlx::query(
        "INSERT INTO domain_nodes (id,kind,title,lat,lon,payload,search_visibility) VALUES \
        ('rp-a-public','place','A',53.5,10.0,'{}'::jsonb,'public'),\
        ('rp-b-private','place','B',53.5,10.0,'{\"created_by_account_id\":\"rp-private-owner\"}'::jsonb,'private'),\
        ('rp-c-public','place','C',53.5,10.0,'{}'::jsonb,'public'),\
        ('rp-d-hidden','place','D',53.5,10.0,'{\"created_by_account_id\":\"rp-private-owner\"}'::jsonb,'hidden'),\
        ('rp-e-revoked','place','E',53.5,10.0,'{\"created_by_account_id\":\"rp-private-owner\"}'::jsonb,'revoked')"
    ).execute(&pool).await.expect("insert mixed-privacy BBOX");
    let ids = |nodes: Vec<weltgewebe_api::routes::nodes::Node>| {
        nodes.into_iter().map(|n| n.id).collect::<Vec<_>>()
    };
    let public = load_nodes_bbox_from_postgres(&pool, 53.4, 53.6, 9.9, 10.1, 2, 0, None)
        .await
        .expect("anonymous BBOX");
    assert_eq!(ids(public), vec!["rp-a-public", "rp-c-public"]);
    let cursor = load_nodes_bbox_after_id_from_postgres(
        &pool,
        53.4,
        53.6,
        9.9,
        10.1,
        1,
        "rp-a-public",
        None,
    )
    .await
    .expect("anonymous cursor");
    assert_eq!(ids(cursor), vec!["rp-c-public"]);
    let wrong =
        load_nodes_bbox_from_postgres(&pool, 53.4, 53.6, 9.9, 10.1, 10, 0, Some("rp-other-owner"))
            .await
            .expect("nonowner BBOX");
    assert_eq!(ids(wrong), vec!["rp-a-public", "rp-c-public"]);
    let owner = load_nodes_bbox_from_postgres(
        &pool,
        53.4,
        53.6,
        9.9,
        10.1,
        10,
        0,
        Some("rp-private-owner"),
    )
    .await
    .expect("owner BBOX");
    assert_eq!(
        ids(owner),
        vec!["rp-a-public", "rp-b-private", "rp-c-public", "rp-d-hidden"]
    );
    let owner_cursor = load_nodes_bbox_after_id_from_postgres(
        &pool,
        53.4,
        53.6,
        9.9,
        10.1,
        1,
        "rp-a-public",
        Some("rp-private-owner"),
    )
    .await
    .expect("owner cursor");
    assert_eq!(ids(owner_cursor), vec!["rp-b-private"]);
    clean(&pool).await;
}

/// A canonical private node can be absent from the process read projection
/// while a legacy edge is untyped and collides with a public account ID.
/// PostgreSQL must disambiguate it instead of assuming the account grants access.
#[tokio::test]
#[ignore]
#[serial]
async fn untyped_account_edge_collision_with_unprojected_node_is_hidden() {
    let pool = prepare_pool().await;
    sqlx::query(
        "INSERT INTO domain_accounts
            (id,kind,title,map_state,radius_m,disabled,role,public_payload,private_payload)
         VALUES
            ('rp-collision','garnrolle','Synthetic Account','not_on_map',0,FALSE,'weber','{}','{}'),
            ('rp-only-account','garnrolle','Other Account','not_on_map',0,FALSE,'weber','{}','{}')",
    )
    .execute(&pool)
    .await
    .expect("seed canonical account identities");
    sqlx::query(
        "INSERT INTO domain_nodes (id,kind,title,lat,lon,payload,search_visibility)
         VALUES
            ('rp-collision','place','Confidential node',53.50,10.00,'{}'::jsonb,'private'),
            ('rp-public-target','place','Public target',53.55,10.00,'{}'::jsonb,'public')",
    )
    .execute(&pool)
    .await
    .expect("seed colliding and public node");
    sqlx::query(
        "INSERT INTO domain_edges (id,source_id,target_id,edge_kind,payload)
         VALUES
            ('rp-untyped-collision','rp-collision','rp-public-target','reference','{}'::jsonb),
            ('rp-untyped-account','rp-only-account','rp-public-target','reference','{}'::jsonb)",
    )
    .execute(&pool)
    .await
    .expect("seed untyped old edges");
    let state = postgres_bbox_route_state(pool.clone());
    *state.accounts.write().await = load_accounts_from_postgres(&pool).await.expect("accounts");
    let mut nodes = load_nodes_from_postgres(&pool)
        .await
        .expect("node read cache");
    assert!(
        nodes.remove("rp-collision").is_some(),
        "simulate omitted private node"
    );
    *state.nodes.write().await = nodes;
    *state.edges.write().await = load_edges_from_postgres(&pool).await.expect("edge cache");
    let app = Router::new().merge(api_router()).with_state(state);
    let list = app
        .clone()
        .oneshot(Request::get("/edges").body(body::Body::empty()).unwrap())
        .await
        .expect("list legacy edges");
    assert_eq!(list.status(), axum::http::StatusCode::OK);
    let content = String::from_utf8(
        body::to_bytes(list.into_body(), usize::MAX)
            .await
            .expect("body")
            .to_vec(),
    )
    .expect("UTF-8");
    assert!(
        !content.contains("rp-untyped-collision"),
        "private collision leaked: {content}"
    );
    assert!(
        content.contains("rp-untyped-account"),
        "legacy true account link disappeared"
    );
    let blocked = app
        .clone()
        .oneshot(
            Request::get("/edges/rp-untyped-collision")
                .body(body::Body::empty())
                .unwrap(),
        )
        .await
        .expect("read private collision");
    assert_eq!(blocked.status(), axum::http::StatusCode::NOT_FOUND);
    let allowed = app
        .oneshot(
            Request::get("/edges/rp-untyped-account")
                .body(body::Body::empty())
                .unwrap(),
        )
        .await
        .expect("read real legacy account");
    assert_eq!(allowed.status(), axum::http::StatusCode::OK);
    clean(&pool).await;
}

// A real PostgreSQL + HTTP proof for the delayed search fence on both aliases.
// The handler simulates SQL candidate selection followed by ranking; the
// external embedding boundary is covered in the T006 service proof.
#[tokio::test]
#[ignore = "requires an isolated DATABASE_URL"]
#[serial]
async fn search_aliases_fence_sql_candidates_until_http_response() {
    use axum::{middleware::from_fn_with_state, response::IntoResponse, routing::get, Json};
    use tokio::{
        sync::{oneshot, Notify},
        time::{sleep, timeout, Duration},
    };
    use weltgewebe_api::middleware::domain_projection::{
        acquire_search_privacy_fence, ensure_current_domain_projection,
    };

    let pool = prepare_pool().await;
    sqlx::query(
        "INSERT INTO domain_nodes (id,kind,title,lat,lon,payload,search_visibility)
         VALUES ('rp-search-fence','place','Private Search Marker',53.6,10.0,'{}'::jsonb,'public')",
    )
    .execute(&pool)
    .await
    .expect("seed public searchable node");
    let state = postgres_bbox_route_state(pool.clone());
    state
        .refresh_domain_projection_if_stale()
        .await
        .expect("load public projection");

    for path in ["/search", "/nodes/search"] {
        let entered = Arc::new(Notify::new());
        let release = Arc::new(Notify::new());
        let app = Router::new()
            .route(
                path,
                get({
                    let pool = pool.clone();
                    let state = state.clone();
                    let entered = Arc::clone(&entered);
                    let release = Arc::clone(&release);
                    move || {
                        let pool = pool.clone();
                        let state = state.clone();
                        let entered = Arc::clone(&entered);
                        let release = Arc::clone(&release);
                        async move {
                            // Match the actual search's canonical selection
                            // after (not during) the embedding provider phase.
                            let fence = acquire_search_privacy_fence(&state, None)
                                .await
                                .expect("acquire delayed search privacy fence")
                                .expect("anonymous search authorized");
                            let title: String = sqlx::query_scalar(
                                "SELECT title FROM domain_nodes
                                 WHERE id='rp-search-fence' AND search_visibility='public'",
                            )
                            .fetch_one(&pool)
                            .await
                            .expect("read still-public SQL search candidate");
                            entered.notify_one();
                            release.notified().await; // simulated ranking/JSON
                            let response =
                                Json(serde_json::json!({"title": title})).into_response();
                            fence
                                .finish(response)
                                .await
                                .expect("release search privacy fence")
                        }
                    }
                }),
            )
            .layer(from_fn_with_state(
                state.clone(),
                ensure_current_domain_projection,
            ))
            .with_state(state.clone());

        let reader = tokio::spawn(async move {
            app.oneshot(Request::get(path).body(body::Body::empty()).unwrap())
                .await
                .expect("search HTTP response")
        });
        timeout(Duration::from_secs(5), entered.notified())
            .await
            .expect("search selected SQL candidate before visibility commit");

        let other_pool = direct_pool().await;
        let (pid_tx, pid_rx) = oneshot::channel();
        let writer = tokio::spawn(async move {
            let mut connection = other_pool
                .acquire()
                .await
                .expect("separate writer connection");
            let pid: i32 = sqlx::query_scalar("SELECT pg_backend_pid()")
                .fetch_one(&mut *connection)
                .await
                .expect("writer PID");
            pid_tx.send(pid).expect("send writer PID");
            sqlx::query(
                "UPDATE domain_nodes SET search_visibility='private'
                 WHERE id='rp-search-fence'",
            )
            .execute(&mut *connection)
            .await
            .expect("cross-process private commit");
        });
        let writer_pid = timeout(Duration::from_secs(3), pid_rx)
            .await
            .expect("writer connection ready")
            .expect("writer PID available");
        // A mere delay could be falsely green on a slow CI runner: demand
        // the real PostgreSQL blocking PID while the HTTP response is held.
        let blockers = timeout(Duration::from_secs(4), async {
            loop {
                let blocker_pids: Vec<i32> = sqlx::query_scalar("SELECT pg_blocking_pids($1)")
                    .bind(writer_pid)
                    .fetch_one(&pool)
                    .await
                    .expect("read actual PostgreSQL lock blockers");
                if !blocker_pids.is_empty() {
                    break blocker_pids;
                }
                assert!(
                    !writer.is_finished(),
                    "private commit overtook search response on {path}"
                );
                sleep(Duration::from_millis(25)).await;
            }
        })
        .await
        .expect("writer must wait for held HTTP privacy fence");
        assert!(
            !blockers.is_empty(),
            "actual PostgreSQL lock wait on {path}"
        );

        release.notify_one();
        let response = timeout(Duration::from_secs(5), reader)
            .await
            .expect("complete held search response")
            .expect("search handler task");
        assert_eq!(response.status(), axum::http::StatusCode::OK);
        let json: serde_json::Value = serde_json::from_slice(
            &body::to_bytes(response.into_body(), usize::MAX)
                .await
                .expect("search JSON bytes"),
        )
        .expect("parse search JSON");
        assert_eq!(json["title"], "Private Search Marker");
        timeout(Duration::from_secs(5), writer)
            .await
            .expect("private commit after search reply")
            .expect("writer task");

        sqlx::query(
            "UPDATE domain_nodes SET search_visibility='public'
             WHERE id='rp-search-fence'",
        )
        .execute(&pool)
        .await
        .expect("reset public synthetic node for next alias");
        state
            .refresh_domain_projection_if_stale()
            .await
            .expect("refresh reset search visibility");
    }
    clean(&pool).await;
}

#[tokio::test]
#[ignore = "requires an isolated DATABASE_URL"]
#[serial]
async fn sensitive_get_holds_global_visibility_version_fence_until_response() {
    use axum::{middleware::from_fn_with_state, routing::get, Json};
    use std::sync::atomic::{AtomicUsize, Ordering};
    use tokio::sync::Notify;
    use tokio::time::{timeout, Duration};
    use weltgewebe_api::middleware::domain_projection::ensure_current_domain_projection;

    let pool = prepare_pool().await;
    sqlx::query(
        "INSERT INTO domain_nodes (id,kind,title,lat,lon,payload,search_visibility)
         VALUES ('rp-fenced-privacy','place','Previously Public',53.6,10.0,'{}'::jsonb,'public')",
    )
    .execute(&pool)
    .await
    .expect("seed public node");
    let state = postgres_bbox_route_state(pool.clone());
    state
        .refresh_domain_projection_if_stale()
        .await
        .expect("materialize initial public projection");

    let entered = Arc::new(Notify::new());
    let release = Arc::new(Notify::new());
    let calls = Arc::new(AtomicUsize::new(0));
    let app = Router::new()
        .route(
            "/nodes/fence-probe",
            get({
                let state = state.clone();
                let entered = Arc::clone(&entered);
                let release = Arc::clone(&release);
                let calls = Arc::clone(&calls);
                move || {
                    let state = state.clone();
                    let entered = Arc::clone(&entered);
                    let release = Arc::clone(&release);
                    let calls = Arc::clone(&calls);
                    async move {
                        // Read the same process-local projection that node and
                        // related account/edge APIs authorize against.
                        let visibility = state
                            .nodes
                            .read()
                            .await
                            .get("rp-fenced-privacy")
                            .map(|node| node.search_visibility.as_str().to_string());
                        if calls.fetch_add(1, Ordering::SeqCst) == 0 {
                            // Keep the first response incomplete while a second
                            // DB connection attempts public -> private.
                            entered.notify_one();
                            release.notified().await;
                        }
                        Json(serde_json::json!({ "visibility": visibility }))
                    }
                }
            }),
        )
        .layer(from_fn_with_state(
            state.clone(),
            ensure_current_domain_projection,
        ))
        .with_state(state);

    let first_request = tokio::spawn({
        let app = app.clone();
        async move {
            app.oneshot(
                Request::get("/nodes/fence-probe")
                    .body(body::Body::empty())
                    .unwrap(),
            )
            .await
            .expect("first fenced GET")
        }
    });
    timeout(Duration::from_secs(5), entered.notified())
        .await
        .expect("GET reached cache-backed handler");

    // A different PgPool emulates a second API process with no shared Tokio
    // RwLock. Its visibility commit updates domain_projection_state through the
    // installed trigger and MUST wait for the in-flight response's SQL row fence.
    let other_pool = direct_pool().await;
    let mut writer = tokio::spawn(async move {
        sqlx::query(
            "UPDATE domain_nodes SET search_visibility='private'
             WHERE id='rp-fenced-privacy'",
        )
        .execute(&other_pool)
        .await
        .expect("cross-process visibility change");
    });
    assert!(
        timeout(Duration::from_millis(200), &mut writer)
            .await
            .is_err(),
        "the private visibility commit must not overtake an in-flight public response",
    );

    release.notify_one();
    let initial = timeout(Duration::from_secs(5), first_request)
        .await
        .expect("public response completed")
        .expect("GET task");
    assert_eq!(initial.status(), axum::http::StatusCode::OK);
    let initial_json: serde_json::Value = serde_json::from_slice(
        &body::to_bytes(initial.into_body(), usize::MAX)
            .await
            .expect("public response bytes"),
    )
    .expect("public response JSON");
    assert_eq!(initial_json["visibility"], "public");
    timeout(Duration::from_secs(5), writer)
        .await
        .expect("writer commits after read fence releases")
        .expect("writer task");

    let later = app
        .oneshot(
            Request::get("/nodes/fence-probe")
                .body(body::Body::empty())
                .unwrap(),
        )
        .await
        .expect("second fenced GET");
    assert_eq!(later.status(), axum::http::StatusCode::OK);
    let later_json: serde_json::Value = serde_json::from_slice(
        &body::to_bytes(later.into_body(), usize::MAX)
            .await
            .expect("private response bytes"),
    )
    .expect("private response JSON");
    assert_eq!(
        later_json["visibility"], "private",
        "next GET must refresh the committed PostgreSQL visibility"
    );
    clean(&pool).await;
}

#[tokio::test]
#[ignore = "requires an isolated DATABASE_URL"]
#[serial]
async fn queued_visibility_writer_precedes_new_privacy_get_across_api_states() {
    use axum::{middleware::from_fn_with_state, routing::get, Json};
    use tokio::{
        sync::Notify,
        time::{sleep, timeout, Duration},
    };
    use weltgewebe_api::middleware::domain_projection::ensure_current_domain_projection;

    let pool = prepare_pool().await;
    sqlx::query(
        "INSERT INTO domain_nodes (id,kind,title,lat,lon,payload,search_visibility)
         VALUES ('rp-fair-writer','place','Writer fairness',53.6,10.0,'{}'::jsonb,'public')",
    )
    .execute(&pool)
    .await
    .expect("seed fair writer fixture");

    // Independent app states and SQL pools model separate API replicas; a
    // local Tokio gate in either replica must not hide a PostgreSQL bypass.
    let first_state = postgres_bbox_route_state(pool.clone());
    first_state
        .refresh_domain_projection_if_stale()
        .await
        .expect("first API state ready");
    let second_pool = direct_pool().await;
    let second_state = postgres_bbox_route_state(second_pool);
    second_state
        .refresh_domain_projection_if_stale()
        .await
        .expect("second independent API state ready");

    let entered = Arc::new(Notify::new());
    let release = Arc::new(Notify::new());
    let app1 = Router::new()
        .route(
            "/nodes/fair-queue",
            get({
                let state = first_state.clone();
                let entered = Arc::clone(&entered);
                let release = Arc::clone(&release);
                move || {
                    let state = state.clone();
                    let entered = Arc::clone(&entered);
                    let release = Arc::clone(&release);
                    async move {
                        let visibility = state
                            .nodes
                            .read()
                            .await
                            .get("rp-fair-writer")
                            .map(|node| node.search_visibility.as_str().to_string());
                        entered.notify_one();
                        release.notified().await;
                        Json(serde_json::json!({"visibility": visibility}))
                    }
                }
            }),
        )
        .layer(from_fn_with_state(
            first_state.clone(),
            ensure_current_domain_projection,
        ))
        .with_state(first_state);
    let app2 = Router::new()
        .route(
            "/nodes/fair-queue",
            get({
                let state = second_state.clone();
                move || {
                    let state = state.clone();
                    async move {
                        let visibility = state
                            .nodes
                            .read()
                            .await
                            .get("rp-fair-writer")
                            .map(|node| node.search_visibility.as_str().to_string());
                        Json(serde_json::json!({"visibility": visibility}))
                    }
                }
            }),
        )
        .layer(from_fn_with_state(
            second_state.clone(),
            ensure_current_domain_projection,
        ))
        .with_state(second_state);

    let first = tokio::spawn(async move {
        app1.oneshot(
            Request::get("/nodes/fair-queue")
                .body(body::Body::empty())
                .unwrap(),
        )
        .await
        .expect("first API response")
    });
    timeout(Duration::from_secs(5), entered.notified())
        .await
        .expect("first GET must own its full-response PG generation fence");

    let writer_pool = direct_pool().await;
    let writer = tokio::spawn(async move {
        sqlx::query(
            "UPDATE domain_nodes SET search_visibility='private'
             WHERE id='rp-fair-writer' /* fair-writer-probe */",
        )
        .execute(&writer_pool)
        .await
        .expect("actual privacy writer commits");
    });
    // Do not guess that the writer is waiting: prove a live PG lock conflict.
    let mut observed_waiting_writer = false;
    for _ in 0..70 {
        let waiting: i64 = sqlx::query_scalar(
            "SELECT COUNT(*) FROM pg_stat_activity
             WHERE query LIKE '%fair-writer-probe%'
               AND wait_event_type = 'Lock'
               AND cardinality(pg_blocking_pids(pid)) > 0
               AND pid <> pg_backend_pid()",
        )
        .fetch_one(&pool)
        .await
        .expect("observe actual PostgreSQL lock wait");
        if waiting > 0 {
            observed_waiting_writer = true;
            break;
        }
        sleep(Duration::from_millis(20)).await;
    }
    assert!(
        observed_waiting_writer,
        "privacy writer never reached PG lock wait"
    );

    let mut second = tokio::spawn(async move {
        app2.oneshot(
            Request::get("/nodes/fair-queue")
                .body(body::Body::empty())
                .unwrap(),
        )
        .await
        .expect("second API response")
    });
    let early_second = timeout(Duration::from_millis(400), &mut second).await;
    let barged = early_second.is_ok();

    release.notify_one();
    let first_result = timeout(Duration::from_secs(6), first)
        .await
        .expect("first response completes")
        .expect("first task");
    assert_eq!(first_result.status(), axum::http::StatusCode::OK);
    timeout(Duration::from_secs(6), writer)
        .await
        .expect("queued visibility writer must commit after first reader")
        .expect("writer task");
    let second_result = match early_second {
        Ok(result) => result.expect("second task"),
        Err(_) => timeout(Duration::from_secs(6), second)
            .await
            .expect("second reader continues after writer")
            .expect("second task"),
    };
    assert_eq!(second_result.status(), axum::http::StatusCode::OK);
    let parsed: serde_json::Value = serde_json::from_slice(
        &body::to_bytes(second_result.into_body(), usize::MAX)
            .await
            .expect("second response bytes"),
    )
    .expect("parse second JSON");
    assert!(
        !barged,
        "new FOR SHARE privacy GET bypassed an already queued public->private writer",
    );
    assert_eq!(
        parsed["visibility"], "private",
        "second independent API response must reflect committed privacy change"
    );
    clean(&pool).await;
}

#[tokio::test]
#[ignore = "requires an isolated DATABASE_URL"]
#[serial]
async fn own_profile_get_does_not_hold_generation_row_while_waiting_for_profile_writer() {
    use axum::{routing::get, Json};
    use tokio::{
        sync::Notify,
        time::{timeout, Duration},
    };
    use weltgewebe_api::middleware::domain_projection::ensure_current_domain_projection;

    let pool = prepare_pool().await;
    let state = postgres_bbox_route_state(pool.clone());
    state
        .refresh_domain_projection_if_stale()
        .await
        .expect("initial projection loaded");

    let account_writer = state.accounts_persist.lock().await;
    let reached_handler = Arc::new(Notify::new());
    let app = Router::new()
        .route(
            "/accounts/me/profile",
            get({
                let state = state.clone();
                let reached_handler = Arc::clone(&reached_handler);
                move || {
                    let state = state.clone();
                    let reached_handler = Arc::clone(&reached_handler);
                    async move {
                        reached_handler.notify_one();
                        let _profile_guard = state.accounts_persist.lock().await;
                        Json(serde_json::json!({"ok": true}))
                    }
                }
            }),
        )
        .layer(axum::middleware::from_fn_with_state(
            state.clone(),
            ensure_current_domain_projection,
        ))
        .with_state(state.clone());

    let reader = tokio::spawn(async move {
        app.oneshot(
            Request::get("/accounts/me/profile")
                .body(body::Body::empty())
                .unwrap(),
        )
        .await
        .expect("own profile request")
    });
    timeout(Duration::from_secs(5), reached_handler.notified())
        .await
        .expect("profile handler was admitted and is waiting for accounts_persist");

    // Account PATCH owns accounts_persist before its domain_accounts UPDATE.
    // Simulate the trigger's exact singleton row update from a second
    // connection while the GET is still waiting for the writer mutex.
    // If GET holds a global FOR SHARE lock already, neither can finish.
    let second_pool = direct_pool().await;
    timeout(
        Duration::from_secs(2),
        sqlx::query(
            "UPDATE domain_projection_state SET version = version + 1 WHERE singleton = TRUE",
        )
        .execute(&second_pool),
    )
    .await
    .expect("profile writer cannot wait on GET's generation lock")
    .expect("simulated profile writer committed");

    drop(account_writer);
    let response = timeout(Duration::from_secs(5), reader)
        .await
        .expect("GET completed after profile writer")
        .expect("GET task");
    assert_eq!(response.status(), axum::http::StatusCode::OK);
    clean(&pool).await;
}

#[tokio::test]
#[ignore = "requires an isolated DATABASE_URL"]
#[serial]
async fn authenticated_node_get_does_not_hold_generation_lock_during_session_io() {
    use axum::{middleware::from_fn_with_state, routing::get};
    use tokio::time::{timeout, Duration};
    use weltgewebe_api::{
        auth::session_db::DbSessionStore,
        middleware::{auth::auth_middleware, domain_projection::ensure_current_domain_projection},
        routes::auth::SESSION_COOKIE_NAME,
    };

    let pool = prepare_pool().await;
    let account_id = "rp-guest-exit-fence";
    sqlx::query(
        "INSERT INTO domain_accounts \
         (id,kind,title,map_state,radius_m,disabled,role,public_payload,private_payload) \
         VALUES ($1,'garnrolle','Guest exit fence','not_on_map',0,FALSE,'gast','{}','{}')",
    )
    .bind(account_id)
    .execute(&pool)
    .await
    .expect("seed guest");
    let mut state = postgres_bbox_route_state(pool.clone());
    state.sessions = SessionBackend::new(DbSessionStore::new(pool.clone()));
    state
        .refresh_domain_projection_if_stale()
        .await
        .expect("initial current account cache");
    let session = state
        .sessions
        .create(account_id.to_string(), None)
        .await
        .expect("create DB-backed session");
    sqlx::query("UPDATE sessions SET last_active = NOW() - INTERVAL '10 minutes' WHERE id = $1")
        .bind(&session.id)
        .execute(&pool)
        .await
        .expect("force real session touch");

    // The actual request nesting must match the production Router: session
    // get/touch on the outside, then the canonical PostgreSQL visibility fence.
    let app = Router::new()
        .route(
            "/nodes/fence-probe",
            get(|| async { axum::http::StatusCode::OK }),
        )
        .layer(from_fn_with_state(
            state.clone(),
            ensure_current_domain_projection,
        ))
        .route_layer(from_fn_with_state(state.clone(), auth_middleware))
        .with_state(state);

    let mut exiting = pool.begin().await.expect("guest exit transaction");
    let locked = sqlx::query("DELETE FROM sessions WHERE id = $1")
        .bind(&session.id)
        .execute(&mut *exiting)
        .await
        .expect("lock session row before domain node reader");
    assert_eq!(locked.rows_affected(), 1);

    let mut reader = tokio::spawn(async move {
        app.oneshot(
            Request::get("/nodes/fence-probe")
                .header("cookie", format!("{SESSION_COOKIE_NAME}={}", session.id))
                .body(body::Body::empty())
                .unwrap(),
        )
        .await
        .expect("GET with a concurrently departing guest")
    });

    assert!(
        timeout(Duration::from_millis(150), &mut reader)
            .await
            .is_err(),
        "the GET should be awaiting the session row before exit commits"
    );

    // On the broken ordering, the GET already owns SELECT generation FOR
    // SHARE and waits for this session DELETE, while this account DELETE
    // waits for the generation row: an application-level cross-connection
    // deadlock. The writer must commit while the GET is still pending.
    timeout(
        Duration::from_secs(3),
        sqlx::query("DELETE FROM domain_accounts WHERE id = $1")
            .bind(account_id)
            .execute(&mut *exiting),
    )
    .await
    .expect("guest exit must not wait for the GET generation fence")
    .expect("delete departing account");
    exiting.commit().await.expect("commit guest exit");
    let response = timeout(Duration::from_secs(5), reader)
        .await
        .expect("GET resumed after guest exit")
        .expect("GET task");
    assert_eq!(
        response.status(),
        axum::http::StatusCode::UNAUTHORIZED,
        "deleted guest sessions must not retain authorization after the exit commit"
    );
    clean(&pool).await;
}

#[tokio::test]
#[ignore = "requires an isolated DATABASE_URL"]
#[serial]
async fn authenticated_role_is_revalidated_after_post_session_projection_refresh() {
    use axum::{middleware::from_fn_with_state, routing::get};
    use weltgewebe_api::{
        auth::role::Role,
        middleware::{auth::AuthContext, domain_projection::ensure_current_domain_projection},
    };

    let pool = prepare_pool().await;
    let account_id = "rp-guarded-role-downgrade";
    sqlx::query(
        "INSERT INTO domain_accounts \
         (id,kind,title,map_state,radius_m,disabled,role,public_payload,private_payload) \
         VALUES ($1,'garnrolle','Guarded role','not_on_map',0,FALSE,'admin','{}','{}')",
    )
    .bind(account_id)
    .execute(&pool)
    .await
    .expect("seed admin");
    let state = postgres_bbox_route_state(pool.clone());
    state
        .refresh_domain_projection_if_stale()
        .await
        .expect("initial cached admin");
    let stale_authenticated = AuthContext {
        authenticated: true,
        account_id: Some(account_id.to_string()),
        device_id: None,
        role: Role::Admin,
        expires_at: None,
    };
    sqlx::query("UPDATE domain_accounts SET role = 'gast' WHERE id = $1")
        .bind(account_id)
        .execute(&pool)
        .await
        .expect("admin was downgraded in another API process");

    let app = Router::new()
        .route(
            "/nodes/role-probe",
            get(|| async { axum::http::StatusCode::OK }),
        )
        .layer(from_fn_with_state(
            state.clone(),
            ensure_current_domain_projection,
        ))
        .with_state(state);
    let mut request = Request::get("/nodes/role-probe")
        .body(body::Body::empty())
        .unwrap();
    request.extensions_mut().insert(stale_authenticated);

    let response = app.oneshot(request).await.expect("guarded request");
    assert_eq!(
        response.status(),
        axum::http::StatusCode::UNAUTHORIZED,
        "a stale Admin session must not retain authority after the current DB generation downgrades it"
    );
    clean(&pool).await;
}

// A genuine HTTP POST race: the proposal commits while its node is public.
// Pause its post-commit Faden projection on the real account advisory lock;
// a separate PostgreSQL pool can then revoke the node before the POST replies.
#[tokio::test]
#[serial]
#[ignore = "requires an isolated DATABASE_URL"]
async fn proposal_post_response_masks_target_after_visibility_commit() {
    use axum::{routing::post, Extension};
    use sha2::{Digest, Sha256};
    use tokio::time::{sleep, timeout, Duration};
    use weltgewebe_api::{
        auth::role::Role, middleware::auth::AuthContext, routes::governance::create_proposal,
    };
    let pool = direct_pool().await;
    run_migrations(&pool).await;
    let owner = "2fbc2488-45e9-4a96-84cc-2f8df70ed881";
    let target = "rp-post-fence-target";
    // Clean only our synthetic IDs; proposals must precede referenced nodes.
    sqlx::query("DELETE FROM governance_proposals WHERE applicant_account_id=$1")
        .bind(owner)
        .execute(&pool)
        .await
        .expect("remove old proof proposal");
    sqlx::query("DELETE FROM domain_edges WHERE source_id=$1")
        .bind(owner)
        .execute(&pool)
        .await
        .expect("remove old Faden");
    sqlx::query("DELETE FROM domain_nodes WHERE id=$1")
        .bind(target)
        .execute(&pool)
        .await
        .expect("remove old target");
    sqlx::query("DELETE FROM domain_accounts WHERE id=$1")
        .bind(owner)
        .execute(&pool)
        .await
        .expect("remove old actor");

    sqlx::query(
        "INSERT INTO domain_accounts
          (id,kind,title,map_state,radius_m,disabled,role,public_payload,private_payload)
         VALUES ($1,'garnrolle','Proof Weber','not_on_map',0,FALSE,'weber','{}','{}')",
    )
    .bind(owner)
    .execute(&pool)
    .await
    .expect("seed Weber account");
    sqlx::query(
        "INSERT INTO domain_nodes (id,kind,title,lat,lon,payload,search_visibility)
         VALUES ($1,'place','Previously Public Target',53.6,10.0,'{}'::jsonb,'public')",
    )
    .bind(target)
    .execute(&pool)
    .await
    .expect("seed public target");
    let state = postgres_bbox_route_state(pool.clone());
    state
        .refresh_domain_projection_if_stale()
        .await
        .expect("load account projection");

    // This is the precise account-lifecycle lock the post-commit Faden uses.
    let mut h = Sha256::new();
    h.update(b"weltgewebe:account-lifecycle:v1");
    h.update((owner.len() as u64).to_be_bytes());
    h.update(owner.as_bytes());
    let digest = h.finalize();
    let key = i64::from_be_bytes(digest[..8].try_into().expect("hash prefix"));
    let other_pool = direct_pool().await;
    let mut held_faden = other_pool.begin().await.expect("begin blocker");
    sqlx::query("SELECT pg_advisory_xact_lock($1::bigint)")
        .bind(key)
        .execute(&mut *held_faden)
        .await
        .expect("hold Faden advisory lock");

    let auth = AuthContext {
        authenticated: true,
        account_id: Some(owner.to_owned()),
        device_id: None,
        role: Role::Weber,
        expires_at: None,
    };
    let app = Router::new()
        .route("/proposals", post(create_proposal))
        .layer(Extension(auth))
        .with_state(state);
    let req = Request::post("/proposals")
        .header("content-type", "application/json")
        .body(body::Body::from(
            serde_json::json!({
                "kind": "sachantrag",
                "title": "Proof public target",
                "target_node_id": target
            })
            .to_string(),
        ))
        .expect("real HTTP POST");
    let mut task = tokio::spawn(async move { app.oneshot(req).await.expect("POST handler") });

    timeout(Duration::from_secs(8), async {
        loop {
            let n: i64 = sqlx::query_scalar(
                "SELECT count(*) FROM governance_proposals
                 WHERE applicant_account_id=$1 AND target_node_id=$2",
            )
            .bind(owner)
            .bind(target)
            .fetch_one(&other_pool)
            .await
            .expect("read committed proof proposal");
            if n == 1 {
                break;
            }
            assert!(!task.is_finished(), "POST finished before proposal commit");
            sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .expect("proposal commit was not observable");
    assert!(
        timeout(Duration::from_millis(200), &mut task)
            .await
            .is_err(),
        "POST was not held after the durable proposal commit",
    );
    // This must commit after the original proposal but before its JSON reply.
    timeout(
        Duration::from_secs(4),
        sqlx::query("UPDATE domain_nodes SET search_visibility='private' WHERE id=$1")
            .bind(target)
            .execute(&other_pool),
    )
    .await
    .expect("writer did not commit while Faden blocked")
    .expect("commit private target");
    held_faden
        .rollback()
        .await
        .expect("release synthetic Faden blocker");

    let response = timeout(Duration::from_secs(8), task)
        .await
        .expect("POST completes")
        .expect("POST task");
    assert_eq!(response.status(), axum::http::StatusCode::CREATED);
    let json: serde_json::Value = serde_json::from_slice(
        &body::to_bytes(response.into_body(), usize::MAX)
            .await
            .expect("POST JSON bytes"),
    )
    .expect("decode POST response");
    assert_eq!(json["kind"], "sachantrag");
    assert!(
        json.get("target_node_id").is_none() && json.get("target_node_title").is_none(),
        "public-to-private committed before POST reply but target leaked: {json}",
    );

    sqlx::query("DELETE FROM governance_proposals WHERE applicant_account_id=$1")
        .bind(owner)
        .execute(&pool)
        .await
        .expect("clean proof proposal");
    sqlx::query("DELETE FROM domain_edges WHERE source_id=$1")
        .bind(owner)
        .execute(&pool)
        .await
        .expect("clean proof Faden");
    sqlx::query("DELETE FROM domain_nodes WHERE id=$1")
        .bind(target)
        .execute(&pool)
        .await
        .expect("clean proof target");
    sqlx::query("DELETE FROM domain_accounts WHERE id=$1")
        .bind(owner)
        .execute(&pool)
        .await
        .expect("clean proof actor");
}

#[tokio::test]
#[serial]
#[ignore = "requires an isolated DATABASE_URL"]
async fn public_proposal_http_read_fences_target_visibility_through_response() {
    use axum::{routing::get, Extension};
    use tokio::time::{sleep, timeout, Duration};
    use weltgewebe_api::{
        auth::role::Role,
        governance::create_sach_proposal,
        middleware::auth::AuthContext,
        routes::governance::{get_proposal, list_proposals},
    };

    // A previous failed isolated run can have left a proposal referencing
    // this fixture node. Clear only our own proposal before generic rp-* cleanup.
    let pool = direct_pool().await;
    run_migrations(&pool).await;
    sqlx::query(
        "DELETE FROM governance_proposals WHERE applicant_account_id = 'rp-proposal-fence-author'",
    )
    .execute(&pool)
    .await
    .expect("clean any previous failed proposal proof");
    clean(&pool).await;
    let account = "rp-proposal-fence-author";
    let target = "rp-proposal-fence-target";
    sqlx::query(
        "INSERT INTO domain_accounts \
         (id, kind, title, map_state, radius_m, disabled, role, public_payload, private_payload) \
         VALUES ($1, 'garnrolle', 'Proof author', 'not_on_map', 0, FALSE, 'weber', '{}', '{}')",
    )
    .bind(account)
    .execute(&pool)
    .await
    .expect("seed proposal author");
    sqlx::query(
        "INSERT INTO domain_nodes \
         (id, kind, title, lat, lon, payload, search_visibility) \
         VALUES ($1, 'place', 'Private-after-commit target', 53.5, 9.9, '{}'::jsonb, 'public')",
    )
    .bind(target)
    .execute(&pool)
    .await
    .expect("seed public proposal target");
    let proposal = create_sach_proposal(
        &pool,
        account,
        "Proof author",
        "Visibility fence",
        None,
        Some(target),
        chrono::Utc::now(),
    )
    .await
    .expect("create proposal referencing public target");

    // The actual Governance HTTP handlers run with a deliberately anonymous
    // authentication context. No test-only middleware is allowed to supply the
    // visibility fence that production handlers must own after finalize_due.
    let app = Router::new()
        .route("/proposals", get(list_proposals))
        .route("/proposals/{id}", get(get_proposal))
        .with_state(postgres_bbox_route_state(pool.clone()))
        .layer(Extension(AuthContext {
            authenticated: false,
            account_id: None,
            device_id: None,
            role: Role::Gast,
            expires_at: None,
        }));

    for path in [
        format!("/proposals/{}", proposal.id),
        "/proposals".to_string(),
    ] {
        sqlx::query("UPDATE domain_nodes SET search_visibility = 'public' WHERE id = $1")
            .bind(target)
            .execute(&pool)
            .await
            .expect("regrant public fixture");

        // Block the proposal SELECT on its veto-count subquery. This creates
        // an observable interval during response construction without any
        // synthetic pause or production-only instrumentation.
        let mut blocker = pool.begin().await.expect("begin veto table blocker");
        sqlx::query("LOCK TABLE governance_vetoes IN ACCESS EXCLUSIVE MODE")
            .execute(&mut *blocker)
            .await
            .expect("hold unrelated veto read until test releases it");

        let request = tokio::spawn({
            let app = app.clone();
            let path = path.clone();
            async move {
                app.oneshot(Request::get(path).body(body::Body::empty()).unwrap())
                    .await
                    .expect("public proposal HTTP response")
            }
        });

        let mut blocked_select_seen = false;
        for _ in 0..150 {
            let waiting: i64 = sqlx::query_scalar(
                "SELECT count(*) FROM pg_locks \
                 WHERE relation = 'governance_vetoes'::regclass AND NOT granted",
            )
            .fetch_one(&pool)
            .await
            .expect("observe real PostgreSQL query waiter");
            if waiting > 0 {
                blocked_select_seen = true;
                break;
            }
            sleep(Duration::from_millis(20)).await;
        }
        assert!(
            blocked_select_seen,
            "real HTTP proposal query never reached veto table"
        );
        assert!(
            !request.is_finished(),
            "request must be in flight before privacy mutation"
        );

        // An independent PgPool (no Tokio/cache sharing) commits the canonical
        // privacy change. It may not overtake the in-flight public response.
        let other_pool = direct_pool().await;
        let mut writer = tokio::spawn(async move {
            sqlx::query(
                "UPDATE domain_nodes SET search_visibility = 'private' \
                 WHERE id = 'rp-proposal-fence-target'",
            )
            .execute(&other_pool)
            .await
        });
        // Do not treat slow scheduling/connection setup as proof of privacy.
        // Observe a real PostgreSQL lock wait from THIS node writer, blocked
        // by a backend that holds RowShareLock on the generation singleton.
        let mut writer_blocked_on_privacy_fence = false;
        for _ in 0..150 {
            if writer.is_finished() {
                break;
            }
            let waiting: i64 = sqlx::query_scalar(
                r#"SELECT count(*) FROM pg_stat_activity writer
                   WHERE writer.datname = current_database()
                     AND writer.wait_event_type = 'Lock'
                     AND writer.query LIKE 'UPDATE domain_nodes SET search_visibility = %'
                     AND EXISTS (
                         SELECT 1 FROM pg_locks guard
                         WHERE guard.pid = ANY(pg_blocking_pids(writer.pid))
                           AND guard.relation = 'domain_projection_state'::regclass
                           AND guard.mode = 'RowShareLock' AND guard.granted
                     )"#,
            )
            .fetch_one(&pool)
            .await
            .expect("inspect exact blocked privacy writer");
            if waiting > 0 {
                writer_blocked_on_privacy_fence = true;
                break;
            }
            sleep(Duration::from_millis(20)).await;
        }
        assert!(
            writer_blocked_on_privacy_fence,
            "independent writer did not wait on the in-flight proposal generation fence at {path}",
        );
        assert!(
            timeout(Duration::from_millis(300), &mut writer)
                .await
                .is_err(),
            "public-to-private DB commit overtook the in-flight proposal response on {path}",
        );
        blocker
            .rollback()
            .await
            .expect("release veto table blocker");
        let initial = timeout(Duration::from_secs(7), request)
            .await
            .expect("public response finished")
            .expect("public HTTP task");
        assert_eq!(initial.status(), axum::http::StatusCode::OK);
        let initial_bytes = body::to_bytes(initial.into_body(), usize::MAX)
            .await
            .expect("serialize public response");
        let initial_body: serde_json::Value =
            serde_json::from_slice(&initial_bytes).expect("decode public proposal JSON");
        assert!(
            initial_body.to_string().contains(target),
            "public target must be present before private commit at {path}",
        );
        timeout(Duration::from_secs(7), writer)
            .await
            .expect("privacy writer completes after read fence drops")
            .expect("privacy writer task")
            .expect("privacy writer commit");

        let private = app
            .clone()
            .oneshot(
                Request::get(path.clone())
                    .body(body::Body::empty())
                    .unwrap(),
            )
            .await
            .expect("private proposal HTTP response");
        assert_eq!(private.status(), axum::http::StatusCode::OK);
        let private_bytes = body::to_bytes(private.into_body(), usize::MAX)
            .await
            .expect("serialize private response");
        let private_body: serde_json::Value =
            serde_json::from_slice(&private_bytes).expect("decode private proposal JSON");
        assert!(
            !private_body.to_string().contains(target),
            "private target ID leaked in later HTTP proposal response at {path}",
        );
    }

    sqlx::query("DELETE FROM governance_proposals WHERE id = $1::uuid")
        .bind(&proposal.id)
        .execute(&pool)
        .await
        .expect("clean proposal fixture before deleting target");
    clean(&pool).await;
}
