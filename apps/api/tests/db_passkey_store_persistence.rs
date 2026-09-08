//! Integration proof: AUTH-PG-002 DbPasskeyStore with direct PostgreSQL.
//!
//! Proves that a registered WebAuthn credential persisted by one store instance
//! is recoverable by a freshly constructed store instance (i.e. it survives a
//! store/app re-initialisation — the restart-stability invariant), that
//! duplicate credential IDs are rejected by the database, that lookups and
//! removals stay account-scoped, and that `update_credential` persists the
//! advanced signature counter.
//!
//! On top of the store-layer proofs, this file carries the AUTH-PG-002-C1
//! route-level Register→Reload→Auth proof (`passkey_register_reload_auth_route_proof`):
//! the real routes behind the real auth middleware, driven by a software
//! authenticator that does REAL ES256 cryptography, across a full app-state
//! reinitialisation. See the `soft_passkey` module and the test doc comment.
//!
//! Run with:
//!   DATABASE_URL=postgres://welt:gewebe@localhost:5432/weltgewebe \
//!     cargo test --locked -p weltgewebe-api \
//!     --test db_passkey_store_persistence -- --include-ignored --test-threads=1
//!
//! Notes:
//! - Tests are ignored by default to keep offline paths green (except the
//!   DB-free soft-authenticator self-check, which pins the WebAuthn fixture).
//! - DATABASE_URL must point to direct PostgreSQL (not PgBouncer at :6432).
//! - Fixtures use a recognizable account-id namespace and are cleaned up.

mod support;

use std::{net::SocketAddr, path::PathBuf, str::FromStr, sync::Arc};

use axum::{
    body,
    extract::{connect_info::MockConnectInfo, ConnectInfo, State},
    http::{HeaderMap, Request, StatusCode},
    response::IntoResponse,
    Json, Router,
};
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use serde_json::json;
use sqlx::postgres::{PgConnectOptions, PgPoolOptions};
use tower::ServiceExt;
use uuid::Uuid;
use webauthn_rs::prelude::*;

use tokio::sync::{Mutex, RwLock};

use weltgewebe_api::{
    auth::{
        accounts::AccountStore,
        passkeys::{build_webauthn, PasskeyStore},
        passkeys_db::{credential_id_key, DbPasskeyStore, DbPasskeyStoreError},
        passkeys_runtime,
        rate_limit::AuthRateLimiter,
        role::Role,
        session::SessionBackend,
        session_db::DbSessionStore,
    },
    config::{
        AppConfig, DomainAccountWriteSource, DomainEdgeWriteSource, DomainNodeWriteSource,
        DomainReadSource, PasskeyCredentialSource,
    },
    domain_db::load_accounts_from_postgres,
    routes::{
        accounts::{AccountInternal, AccountPublic, GarnrolleMapState},
        auth::{passkey_auth_options, PasskeyAuthOptionsPayload, SESSION_COOKIE_NAME},
    },
    state::{ApiState, OrderedCache},
    telemetry::{BuildInfo, Metrics},
};

fn direct_database_url() -> String {
    let url = std::env::var("DATABASE_URL").expect(
        "DATABASE_URL must be set to run db_passkey_store_persistence tests; \
         point it to direct PostgreSQL (port 5432)",
    );
    assert!(
        !url.contains(":6432"),
        "DATABASE_URL must target direct PostgreSQL, not PgBouncer (port 6432)"
    );
    support::postgres_proof::validated_direct_disposable_url(url)
}

async fn connect_pool() -> sqlx::PgPool {
    let connect_opts = PgConnectOptions::from_str(&direct_database_url())
        .expect("DATABASE_URL must be a valid postgres connection string");
    let pool = PgPoolOptions::new()
        .max_connections(2)
        .connect_with(connect_opts)
        .await
        .expect("failed to connect to direct PostgreSQL");
    ensure_migrations(&pool).await;
    pool
}

async fn ensure_migrations(pool: &sqlx::PgPool) {
    let migrations_dir: PathBuf = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("migrations");
    let migrator = sqlx::migrate::Migrator::new(migrations_dir)
        .await
        .expect("failed to load migrations");
    migrator.run(pool).await.expect("failed to run migrations");
}

fn unique_account_id(test_name: &str) -> String {
    format!("db-passkey-store-{test_name}-{}", Uuid::new_v4())
}

async fn cleanup_account(pool: &sqlx::PgPool, account_id: &str) {
    sqlx::query("DELETE FROM passkey_credentials WHERE account_id = $1")
        .bind(account_id)
        .execute(pool)
        .await
        .expect("failed to cleanup account passkeys");
}

fn postgres_passkey_runtime_state(pool: sqlx::PgPool) -> ApiState {
    let config = AppConfig {
        max_guest_owned_nodes: 1_000,
        domain_read_source: DomainReadSource::Postgres,
        domain_account_write_source: DomainAccountWriteSource::Jsonl,
        domain_node_write_source: DomainNodeWriteSource::Jsonl,
        domain_edge_write_source: DomainEdgeWriteSource::Jsonl,
        passkey_credential_source: PasskeyCredentialSource::Postgres,
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
        auth_log_magic_token: true,
        webauthn_rp_id: Some("commonthing.net".to_string()),
        webauthn_rp_origin: Some("https://commonthing.net".to_string()),
        webauthn_rp_name: Some("commonThing Test".to_string()),
    };
    let metrics = Metrics::try_new(BuildInfo {
        version: "test",
        commit: "test",
        build_timestamp: "test",
    })
    .expect("metrics");
    let rate_limiter = Arc::new(AuthRateLimiter::new(&config));
    let webauthn = build_webauthn(&config)
        .expect("webauthn config must be valid")
        .expect("webauthn must be configured");

    ApiState {
        db_pool: Some(pool),
        db_pool_configured: true,
        nats_client: None,
        nats_configured: false,
        config,
        metrics,
        sessions: SessionBackend::new_in_memory(),
        challenges: Default::default(),
        tokens: Default::default(),
        step_up_tokens: Default::default(),
        accounts: Arc::new(RwLock::new(AccountStore::new())),
        nodes: Arc::new(RwLock::new(OrderedCache::new())),
        nodes_persist: Arc::new(Mutex::new(())),
        accounts_persist: Arc::new(Mutex::new(())),
        domain_projection_gate: std::sync::Arc::new(tokio::sync::RwLock::new(())),
        domain_projection_reload: std::sync::Arc::new(tokio::sync::Mutex::new(())),
        domain_projection_local_node_patch_handoff: std::sync::Arc::new(
            std::sync::atomic::AtomicI64::new(-1),
        ),
        domain_projection_version: std::sync::Arc::new(std::sync::atomic::AtomicI64::new(0)),
        edges: Arc::new(RwLock::new(OrderedCache::new())),
        rate_limiter,
        mailer: None,
        webauthn: Some(webauthn),
        passkey_registrations: Default::default(),
        passkey_registration_grants: Default::default(),
        passkey_authentications: Default::default(),
        passkeys: PasskeyStore::new(),
        web_push: None,
    }
}

/// Builds a deterministic, valid `Passkey` from a seed without a browser or
/// authenticator. The credential id is `[seed; 32]`; the rest is a minimal
/// ES256 record. This is the same fixture shape used by the passkeys unit
/// tests.
fn test_passkey(credential_seed: u8) -> Passkey {
    let credential_id = vec![credential_seed; 32];
    let credential_id_b64 = URL_SAFE_NO_PAD.encode(&credential_id);
    serde_json::from_value(json!({
        "cred": {
            "cred_id": credential_id_b64,
            "cred": {
                "type_": "ES256",
                "key": {
                    "EC_EC2": {
                        "curve": "SECP256R1",
                        "x": vec![1_u8; 32],
                        "y": vec![2_u8; 32]
                    }
                }
            },
            "counter": 0,
            "transports": null,
            "user_verified": false,
            "backup_eligible": false,
            "backup_state": false,
            "registration_policy": "preferred",
            "extensions": {
                "cred_protect": "NotRequested",
                "hmac_create_secret": "NotRequested"
            },
            "attestation": {
                "data": "None",
                "metadata": "None"
            },
            "attestation_format": "None"
        }
    }))
    .expect("passkey fixture must deserialize")
}

/// Builds a genuine `AuthenticationResult` (via serde) advancing the counter,
/// targeting the given credential id — no browser needed.
fn test_authentication_result(credential_id: &CredentialID, counter: u32) -> AuthenticationResult {
    let cred_id_value = serde_json::to_value(credential_id).expect("credential id serializes");
    serde_json::from_value(json!({
        "cred_id": cred_id_value,
        "needs_update": true,
        "user_verified": true,
        "backup_state": false,
        "backup_eligible": false,
        "counter": counter,
        "extensions": {}
    }))
    .expect("authentication result fixture must deserialize")
}

/// The core restart-stability proof: a credential inserted via `store1` is
/// found again via a freshly constructed `store2` (separate pool), i.e. it
/// survives store/app re-initialisation.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
async fn db_passkey_store_persists_across_reinit() {
    let pool1 = connect_pool().await;
    let pool2 = connect_pool().await;
    let account_id = unique_account_id("persistence");
    let webauthn_user_id = Uuid::new_v4();
    let passkey = test_passkey(11);
    let cred_id = passkey.cred_id().clone();

    cleanup_account(&pool1, &account_id).await;

    let store1 = DbPasskeyStore::new(pool1.clone());
    store1
        .insert(&account_id, webauthn_user_id, &passkey)
        .await
        .expect("insert should succeed");

    // Fresh store over a fresh pool — simulates a process restart.
    let store2 = DbPasskeyStore::new(pool2.clone());

    let listed = store2
        .list_for_account(&account_id)
        .await
        .expect("list should succeed");
    assert_eq!(listed.len(), 1, "credential must persist across reinit");
    assert_eq!(listed[0].cred_id(), &cred_id);

    let found = store2
        .find_by_credential_id(&cred_id)
        .await
        .expect("find should succeed")
        .expect("credential must be found after reinit");
    assert_eq!(found.account_id, account_id);
    assert_eq!(found.passkey.cred_id(), &cred_id);

    let ids = store2
        .credential_ids_for_account(&account_id)
        .await
        .expect("credential_ids should succeed");
    assert_eq!(ids, vec![cred_id.clone()]);

    cleanup_account(&pool2, &account_id).await;
    pool1.close().await;
    pool2.close().await;
}

/// Duplicate credential ids are rejected by the database (the PRIMARY KEY is
/// the final source of truth), even across accounts.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
async fn db_passkey_store_rejects_duplicate_credential_id() {
    let pool = connect_pool().await;
    let account_a = unique_account_id("dup-a");
    let account_b = unique_account_id("dup-b");
    let passkey = test_passkey(22);

    cleanup_account(&pool, &account_a).await;
    cleanup_account(&pool, &account_b).await;

    let store = DbPasskeyStore::new(pool.clone());
    store
        .insert(&account_a, Uuid::new_v4(), &passkey)
        .await
        .expect("first insert should succeed");

    let duplicate = store.insert(&account_b, Uuid::new_v4(), &passkey).await;
    assert!(
        matches!(duplicate, Err(DbPasskeyStoreError::DuplicateCredentialId)),
        "duplicate credential id must be rejected, got {duplicate:?}"
    );

    cleanup_account(&pool, &account_a).await;
    cleanup_account(&pool, &account_b).await;
    pool.close().await;
}

/// `find_by_credential_id` resolves the correct owner and never cross-account
/// authorises; `remove_for_account` is owner-bound.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
async fn db_passkey_store_find_and_remove_are_account_scoped() {
    let pool = connect_pool().await;
    let account_a = unique_account_id("scope-a");
    let account_b = unique_account_id("scope-b");
    let a_key = test_passkey(33);
    let b_key = test_passkey(44);
    let a_cred = a_key.cred_id().clone();
    let b_cred = b_key.cred_id().clone();

    cleanup_account(&pool, &account_a).await;
    cleanup_account(&pool, &account_b).await;

    let store = DbPasskeyStore::new(pool.clone());
    store
        .insert(&account_a, Uuid::new_v4(), &a_key)
        .await
        .expect("insert account a");
    store
        .insert(&account_b, Uuid::new_v4(), &b_key)
        .await
        .expect("insert account b");

    let found = store
        .find_by_credential_id(&a_cred)
        .await
        .expect("find should succeed")
        .expect("a credential must be found");
    assert_eq!(found.account_id, account_a);

    // Wrong account cannot remove another account's credential.
    assert!(
        !store
            .remove_for_account(&account_b, &a_cred)
            .await
            .expect("remove should succeed"),
        "other account must not remove a credential it does not own"
    );
    assert!(
        store
            .find_by_credential_id(&a_cred)
            .await
            .expect("find should succeed")
            .is_some(),
        "credential must still exist after a wrong-account remove"
    );

    // Owner removes its own credential.
    assert!(
        store
            .remove_for_account(&account_a, &a_cred)
            .await
            .expect("remove should succeed"),
        "owner must remove its own credential"
    );
    assert!(
        store
            .find_by_credential_id(&a_cred)
            .await
            .expect("find should succeed")
            .is_none(),
        "removed credential must no longer be found"
    );
    assert!(
        store
            .find_by_credential_id(&b_cred)
            .await
            .expect("find should succeed")
            .is_some(),
        "other account credential must remain"
    );

    cleanup_account(&pool, &account_a).await;
    cleanup_account(&pool, &account_b).await;
    pool.close().await;
}

/// `update_credential` persists the advanced signature counter (replay/counter
/// semantics survive restart) and stays owner-bound.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
async fn db_passkey_store_update_credential_persists_counter() {
    let pool1 = connect_pool().await;
    let pool2 = connect_pool().await;
    let account_id = unique_account_id("update");
    let other_account = unique_account_id("update-other");
    let passkey = test_passkey(55);
    let cred_id = passkey.cred_id().clone();

    cleanup_account(&pool1, &account_id).await;
    cleanup_account(&pool1, &other_account).await;

    let store = DbPasskeyStore::new(pool1.clone());
    store
        .insert(&account_id, Uuid::new_v4(), &passkey)
        .await
        .expect("insert should succeed");

    let auth_result = test_authentication_result(&cred_id, 7);

    // Cross-account update must fail-close and mutate nothing.
    let cross = store.update_credential(&other_account, &auth_result).await;
    assert!(
        matches!(cross, Err(DbPasskeyStoreError::NotFound)),
        "cross-account update must be rejected, got {cross:?}"
    );

    // Owner update advances the counter 0 -> 7.
    assert!(
        store
            .update_credential(&account_id, &auth_result)
            .await
            .expect("update should succeed"),
        "advancing the signature counter must report a change"
    );

    // Re-applying the same result is a no-op.
    assert!(
        !store
            .update_credential(&account_id, &auth_result)
            .await
            .expect("second update should succeed"),
        "re-applying an identical result must report no change"
    );

    // The advanced counter must be durable: a fresh store sees counter == 7,
    // so a replayed assertion with the old counter would be rejected later.
    let store2 = DbPasskeyStore::new(pool2.clone());
    let reloaded = store2
        .find_by_credential_id(&cred_id)
        .await
        .expect("find should succeed")
        .expect("credential must persist");
    let reloaded_value = serde_json::to_value(&reloaded.passkey).expect("passkey serializes");
    let counter = reloaded_value
        .get("cred")
        .and_then(|c| c.get("counter"))
        .and_then(|c| c.as_u64())
        .expect("counter present in stored credential");
    assert_eq!(counter, 7, "advanced counter must be persisted");

    cleanup_account(&pool2, &account_id).await;
    pool1.close().await;
    pool2.close().await;
}

/// Sanity check that the deterministic credential-id key used as the primary
/// key is reversible — kept here too so a DB-side regression surfaces in the
/// same proof job.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
async fn db_passkey_store_credential_id_key_is_primary_key() {
    let pool = connect_pool().await;
    let account_id = unique_account_id("pk");
    let passkey = test_passkey(66);
    let cred_id = passkey.cred_id().clone();

    cleanup_account(&pool, &account_id).await;

    let store = DbPasskeyStore::new(pool.clone());
    store
        .insert(&account_id, Uuid::new_v4(), &passkey)
        .await
        .expect("insert should succeed");

    let key = credential_id_key(&cred_id);
    let stored_account: Option<String> =
        sqlx::query_scalar("SELECT account_id FROM passkey_credentials WHERE credential_id = $1")
            .bind(&key)
            .fetch_optional(&pool)
            .await
            .expect("query should succeed");
    assert_eq!(stored_account.as_deref(), Some(account_id.as_str()));

    cleanup_account(&pool, &account_id).await;
    pool.close().await;
}

/// Runtime-facade proof: when `passkey_credential_source=postgres`, the facade
/// uses `DbPasskeyStore` rather than the in-memory `PasskeyStore`. A credential
/// inserted through one `ApiState` is visible through a fresh `ApiState` backed
/// by a separate pool.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
async fn passkey_runtime_facade_postgres_persists_across_state_reinit() {
    let pool1 = connect_pool().await;
    let account_id = unique_account_id("runtime-facade");
    cleanup_account(&pool1, &account_id).await;

    let state1 = postgres_passkey_runtime_state(pool1.clone());
    let passkey = test_passkey(81);
    let credential_id = passkey.cred_id().clone();
    let webauthn_user_id = Uuid::new_v4();

    passkeys_runtime::insert(&state1, &account_id, webauthn_user_id, passkey)
        .await
        .expect("runtime facade must persist to postgres");

    let pool2 = connect_pool().await;
    let state2 = postgres_passkey_runtime_state(pool2.clone());
    let ids = passkeys_runtime::credential_ids_for_account(&state2, &account_id)
        .await
        .expect("runtime facade must list from postgres after reinit");
    assert_eq!(ids, vec![credential_id.clone()]);

    let stored = passkeys_runtime::find_by_credential_id(&state2, &credential_id)
        .await
        .expect("runtime facade must search postgres after reinit")
        .expect("credential must exist after reinit");
    assert_eq!(stored.account_id, account_id);

    cleanup_account(&pool2, &stored.account_id).await;
}

/// Route-level proof: `auth/options` reads registered credentials through the
/// runtime facade. The in-memory `PasskeyStore` stays empty; the only credential
/// lives in PostgreSQL.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
async fn passkey_auth_options_route_reads_postgres_runtime_facade() {
    let pool = connect_pool().await;
    let account_id = unique_account_id("runtime-route-auth-options");
    cleanup_account(&pool, &account_id).await;

    let state = postgres_passkey_runtime_state(pool.clone());
    let email = format!("{account_id}@example.invalid");
    let webauthn_user_id = Uuid::new_v4();
    {
        let mut accounts = state.accounts.write().await;
        accounts.insert(AccountInternal {
            public: AccountPublic {
                id: account_id.clone(),
                kind: "garnrolle".to_string(),
                title: "Runtime Facade User".to_string(),
                summary: None,
                public_pos: None,
                map_state: GarnrolleMapState::NotOnMap,
                radius_m: 0,
                disabled: false,
                tags: vec![],
            },
            role: Role::Weber,
            email: Some(email.clone()),
            webauthn_user_id,
        });
    }

    let passkey = test_passkey(82);
    let credential_id = passkey.cred_id().clone();
    passkeys_runtime::insert(&state, &account_id, webauthn_user_id, passkey)
        .await
        .expect("runtime facade must persist credential to postgres");
    assert!(
        state.passkeys.list_for_account(&account_id).is_empty(),
        "test must prove the route does not accidentally read the in-memory store"
    );

    let response = passkey_auth_options(
        State(state.clone()),
        ConnectInfo(SocketAddr::from(([127, 0, 0, 1], 12345))),
        HeaderMap::new(),
        Ok(Json(PasskeyAuthOptionsPayload { email })),
    )
    .await
    .into_response();

    assert_eq!(response.status(), StatusCode::OK);
    assert!(
        !response
            .headers()
            .contains_key(axum::http::header::SET_COOKIE),
        "auth/options must never set a session cookie"
    );
    let bytes = body::to_bytes(response.into_body(), usize::MAX)
        .await
        .expect("response body must be readable");
    let json: serde_json::Value = serde_json::from_slice(&bytes).expect("response must be JSON");
    let allow_credentials = json["options"]["publicKey"]["allowCredentials"]
        .as_array()
        .expect("allowCredentials must be present");
    assert_eq!(allow_credentials.len(), 1);
    let id_b64 = allow_credentials[0]["id"]
        .as_str()
        .expect("allow credential id must be string");
    let decoded_id = URL_SAFE_NO_PAD
        .decode(id_b64)
        .expect("credential id must be base64url");
    assert_eq!(decoded_id, credential_id.as_ref());

    cleanup_account(&pool, &account_id).await;
}

// ═══════════════════════════════════════════════════════════════════════════
// AUTH-PG-002-C1 — Route-level Register→Reload→Auth proof
// ═══════════════════════════════════════════════════════════════════════════

/// Test-only software authenticator doing REAL cryptography: a genuine P-256
/// keypair, a spec-conformant CBOR "none"-format attestation object and a real
/// ES256 assertion signature over `authenticatorData || SHA-256(clientDataJSON)`.
///
/// Nothing on the server side is mocked or shortcut: the route handlers run the
/// full `finish_passkey_registration` / `finish_passkey_authentication`
/// verification against these payloads, exactly as they would against a browser
/// authenticator. The DB-free self-check below
/// (`soft_authenticator_passes_bare_webauthn_ceremonies`) pins this fixture
/// against webauthn-rs so it cannot rot silently.
mod soft_passkey {
    use base64::engine::general_purpose::URL_SAFE_NO_PAD;
    use base64::Engine;
    use openssl::{
        bn::{BigNum, BigNumContext},
        ec::{EcGroup, EcKey},
        ecdsa::EcdsaSig,
        nid::Nid,
        pkey::Private,
    };
    use serde_json::{json, Value};
    use sha2::{Digest, Sha256};

    /// WebAuthn authenticator-data flags. The passkey flow in webauthn-rs pins
    /// `UserVerificationPolicy::Required` for registration AND authentication,
    /// so UV must be asserted in both ceremonies.
    const FLAG_UP: u8 = 0x01;
    const FLAG_UV: u8 = 0x04;
    const FLAG_AT: u8 = 0x40;

    pub struct SoftPasskey {
        key: EcKey<Private>,
        pub credential_id: Vec<u8>,
        rp_id: String,
        origin: String,
    }

    impl SoftPasskey {
        pub fn new(rp_id: &str, origin: &str) -> Self {
            let group =
                EcGroup::from_curve_name(Nid::X9_62_PRIME256V1).expect("P-256 group must exist");
            let key = EcKey::generate(&group).expect("P-256 key generation must succeed");
            let mut credential_id = vec![0u8; 32];
            openssl::rand::rand_bytes(&mut credential_id)
                .expect("credential id randomness must be available");
            Self {
                key,
                credential_id,
                rp_id: rp_id.to_string(),
                origin: origin.to_string(),
            }
        }

        pub fn credential_id_b64(&self) -> String {
            URL_SAFE_NO_PAD.encode(&self.credential_id)
        }

        fn public_key_coordinates(&self) -> (Vec<u8>, Vec<u8>) {
            let group =
                EcGroup::from_curve_name(Nid::X9_62_PRIME256V1).expect("P-256 group must exist");
            let mut ctx = BigNumContext::new().expect("bignum context must allocate");
            let mut x = BigNum::new().expect("bignum x must allocate");
            let mut y = BigNum::new().expect("bignum y must allocate");
            self.key
                .public_key()
                .affine_coordinates(&group, &mut x, &mut y, &mut ctx)
                .expect("public key must expose affine coordinates");
            (
                x.to_vec_padded(32).expect("x coordinate must fit 32 bytes"),
                y.to_vec_padded(32).expect("y coordinate must fit 32 bytes"),
            )
        }

        /// COSE_Key (EC2 / ES256) in canonical CBOR:
        /// `{1: 2, 3: -7, -1: 1, -2: x, -3: y}`.
        fn cose_public_key(&self) -> Vec<u8> {
            let (x, y) = self.public_key_coordinates();
            let mut out = vec![
                0xa5, // map(5)
                0x01, 0x02, // 1 (kty)  => 2 (EC2)
                0x03, 0x26, // 3 (alg)  => -7 (ES256)
                0x20, 0x01, // -1 (crv) => 1 (P-256)
                0x21, 0x58, 0x20, // -2 (x) => bytes(32)
            ];
            out.extend_from_slice(&x);
            out.extend_from_slice(&[0x22, 0x58, 0x20]); // -3 (y) => bytes(32)
            out.extend_from_slice(&y);
            out
        }

        fn client_data(&self, ceremony_type: &str, challenge_b64: &str) -> Vec<u8> {
            serde_json::to_vec(&json!({
                "type": ceremony_type,
                "challenge": challenge_b64,
                "origin": self.origin,
                "crossOrigin": false,
            }))
            .expect("client data must serialize")
        }

        /// CBOR byte-string header for the given payload length.
        fn push_cbor_bytes_header(len: usize, out: &mut Vec<u8>) {
            if len <= 23 {
                out.push(0x40 + len as u8);
            } else if len <= 255 {
                out.extend_from_slice(&[0x58, len as u8]);
            } else {
                let len = u16::try_from(len).expect("payload must fit a 16-bit CBOR length");
                out.push(0x59);
                out.extend_from_slice(&len.to_be_bytes());
            }
        }

        /// `RegisterPublicKeyCredential`-shaped JSON carrying a "none"-format
        /// attestation object over the given creation challenge.
        pub fn create_registration_response(&self, challenge_b64: &str) -> Value {
            let mut auth_data = Vec::new();
            auth_data.extend_from_slice(&Sha256::digest(self.rp_id.as_bytes()));
            auth_data.push(FLAG_UP | FLAG_UV | FLAG_AT);
            auth_data.extend_from_slice(&0u32.to_be_bytes()); // signature counter 0
            auth_data.extend_from_slice(&[0u8; 16]); // AAGUID (none attestation)
            let cred_len = u16::try_from(self.credential_id.len())
                .expect("credential id must fit a 16-bit length");
            auth_data.extend_from_slice(&cred_len.to_be_bytes());
            auth_data.extend_from_slice(&self.credential_id);
            auth_data.extend_from_slice(&self.cose_public_key());

            // attestationObject: {"fmt": "none", "attStmt": {}, "authData": <bytes>}
            let mut attestation_object = vec![0xa3]; // map(3)
            attestation_object.push(0x63);
            attestation_object.extend_from_slice(b"fmt");
            attestation_object.push(0x64);
            attestation_object.extend_from_slice(b"none");
            attestation_object.push(0x67);
            attestation_object.extend_from_slice(b"attStmt");
            attestation_object.push(0xa0); // map(0)
            attestation_object.push(0x68);
            attestation_object.extend_from_slice(b"authData");
            Self::push_cbor_bytes_header(auth_data.len(), &mut attestation_object);
            attestation_object.extend_from_slice(&auth_data);

            let client_data = self.client_data("webauthn.create", challenge_b64);
            json!({
                "id": self.credential_id_b64(),
                "rawId": self.credential_id_b64(),
                "response": {
                    "attestationObject": URL_SAFE_NO_PAD.encode(&attestation_object),
                    "clientDataJSON": URL_SAFE_NO_PAD.encode(&client_data),
                    "transports": ["internal"],
                },
                "type": "public-key",
                "extensions": {},
            })
        }

        /// `PublicKeyCredential`-shaped JSON carrying a REAL ES256 signature
        /// over `authenticatorData || SHA-256(clientDataJSON)`.
        pub fn create_assertion_response(&self, challenge_b64: &str, counter: u32) -> Value {
            let mut auth_data = Vec::new();
            auth_data.extend_from_slice(&Sha256::digest(self.rp_id.as_bytes()));
            auth_data.push(FLAG_UP | FLAG_UV);
            auth_data.extend_from_slice(&counter.to_be_bytes());

            let client_data = self.client_data("webauthn.get", challenge_b64);
            let mut signed_payload = auth_data.clone();
            signed_payload.extend_from_slice(&Sha256::digest(&client_data));
            let digest = Sha256::digest(&signed_payload);
            let signature = EcdsaSig::sign(&digest, &self.key)
                .expect("assertion signing must succeed")
                .to_der()
                .expect("assertion signature must encode to DER");

            json!({
                "id": self.credential_id_b64(),
                "rawId": self.credential_id_b64(),
                "response": {
                    "authenticatorData": URL_SAFE_NO_PAD.encode(&auth_data),
                    "clientDataJSON": URL_SAFE_NO_PAD.encode(&client_data),
                    "signature": URL_SAFE_NO_PAD.encode(&signature),
                    "userHandle": null,
                },
                "type": "public-key",
                "extensions": {},
            })
        }
    }
}

/// DB-free self-check (deliberately NOT ignored): the soft authenticator must
/// pass a bare webauthn-rs register + authenticate ceremony with the library's
/// full cryptographic verification. This runs in the offline test path and pins
/// the fixture the route-level PostgreSQL proof below depends on.
#[test]
fn soft_authenticator_passes_bare_webauthn_ceremonies() {
    let rp_origin = Url::parse("https://example.com").expect("origin must parse");
    let webauthn = WebauthnBuilder::new("example.com", &rp_origin)
        .expect("webauthn builder must accept rp")
        .build()
        .expect("webauthn must build");

    let (ccr, reg_state) = webauthn
        .start_passkey_registration(Uuid::new_v4(), "proof@example.invalid", "Proof User", None)
        .expect("registration ceremony must start");
    let ccr_json = serde_json::to_value(&ccr).expect("ccr must serialize");
    let creation_challenge = ccr_json["publicKey"]["challenge"]
        .as_str()
        .expect("creation challenge must be present");

    let authenticator = soft_passkey::SoftPasskey::new("example.com", "https://example.com");
    let register_credential: RegisterPublicKeyCredential =
        serde_json::from_value(authenticator.create_registration_response(creation_challenge))
            .expect("registration response must parse as RegisterPublicKeyCredential");
    let passkey = webauthn
        .finish_passkey_registration(&register_credential, &reg_state)
        .expect("real webauthn verification must accept the soft attestation");
    assert_eq!(
        passkey.cred_id().as_ref(),
        authenticator.credential_id.as_slice(),
        "registered credential id must match the soft authenticator"
    );

    let (rcr, auth_state) = webauthn
        .start_passkey_authentication(std::slice::from_ref(&passkey))
        .expect("authentication ceremony must start");
    let rcr_json = serde_json::to_value(&rcr).expect("rcr must serialize");
    let request_challenge = rcr_json["publicKey"]["challenge"]
        .as_str()
        .expect("request challenge must be present");

    let assertion: PublicKeyCredential =
        serde_json::from_value(authenticator.create_assertion_response(request_challenge, 1))
            .expect("assertion response must parse as PublicKeyCredential");
    let auth_result = webauthn
        .finish_passkey_authentication(&assertion, &auth_state)
        .expect("real webauthn verification must accept the soft assertion");
    assert_eq!(
        auth_result.counter(),
        1,
        "verified assertion must advance the signature counter"
    );
    assert!(
        auth_result.needs_update(),
        "counter 0 -> 1 must require a credential state update"
    );
}

/// Builds the pre-/post-restart app state the way `lib.rs` does at startup:
/// accounts are loaded from PostgreSQL via the production loader and sessions
/// are backed by the PostgreSQL session store.
async fn postgres_route_proof_state(pool: sqlx::PgPool) -> ApiState {
    let mut state = postgres_passkey_runtime_state(pool.clone());
    let accounts = load_accounts_from_postgres(&pool)
        .await
        .expect("accounts must load from PostgreSQL like at startup");
    state.accounts = Arc::new(RwLock::new(accounts));
    state.sessions = SessionBackend::new(DbSessionStore::new(pool));
    state
}

/// Real router + real auth middleware, mirroring the production wiring (same
/// shape as `app_with_auth` in `api_auth.rs`).
fn route_proof_app(state: ApiState) -> Router {
    Router::new()
        .merge(weltgewebe_api::routes::api_router())
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            weltgewebe_api::middleware::auth::auth_middleware,
        ))
        .layer(MockConnectInfo(SocketAddr::from(([127, 0, 0, 1], 8080))))
        .with_state(state)
}

async fn post_json(
    app: &Router,
    path: &str,
    cookie: Option<&str>,
    payload: &serde_json::Value,
) -> (StatusCode, HeaderMap, serde_json::Value) {
    let mut builder = Request::post(path)
        .header("Host", "example.com")
        .header("Content-Type", "application/json");
    if let Some(cookie) = cookie {
        builder = builder.header("Cookie", cookie);
    }
    let request = builder
        .body(axum::body::Body::from(payload.to_string()))
        .expect("request must build");
    let response = app
        .clone()
        .oneshot(request)
        .await
        .expect("request must reach the router");
    let status = response.status();
    let headers = response.headers().clone();
    let bytes = body::to_bytes(response.into_body(), usize::MAX)
        .await
        .expect("response body must be readable");
    let json = if bytes.is_empty() {
        serde_json::Value::Null
    } else {
        serde_json::from_slice(&bytes).expect("response must be JSON")
    };
    (status, headers, json)
}

async fn stored_credential_counter(pool: &sqlx::PgPool, credential_key: &str) -> i64 {
    sqlx::query_scalar(
        "SELECT (credential->'cred'->>'counter')::bigint \
         FROM passkey_credentials WHERE credential_id = $1",
    )
    .bind(credential_key)
    .fetch_one(pool)
    .await
    .expect("credential row must exist in PostgreSQL")
}

async fn cleanup_route_proof_fixture(pool: &sqlx::PgPool, account_id: &str) {
    sqlx::query("DELETE FROM passkey_credentials WHERE account_id = $1")
        .bind(account_id)
        .execute(pool)
        .await
        .expect("failed to cleanup fixture passkey credentials");
    sqlx::query("DELETE FROM sessions WHERE account_id = $1")
        .bind(account_id)
        .execute(pool)
        .await
        .expect("failed to cleanup fixture sessions");
    sqlx::query("DELETE FROM domain_accounts WHERE id = $1")
        .bind(account_id)
        .execute(pool)
        .await
        .expect("failed to cleanup fixture domain account");
}

/// AUTH-PG-002-C1 — the route-level Register→Reload→Auth proof (Cutover-Plan
/// Gate B, API-level). Crosses, in order and against the real route handlers
/// behind the real auth middleware:
///
/// 1. the account is loaded from PostgreSQL via the production startup loader
///    (`load_accounts_from_postgres`) — nothing is seeded in-memory;
/// 2. passkey registration completes through `POST /auth/passkeys/register/options`
///    and `POST /auth/passkeys/register/verify` with full
///    `finish_passkey_registration` cryptography (no shortcut);
/// 3. the app state is reinitialised: the first pool is closed, every cache and
///    ceremony store is rebuilt from scratch, accounts are re-loaded from
///    PostgreSQL — in-memory state does NOT survive;
/// 4. `POST /auth/passkeys/auth/options` finds the credential from PostgreSQL
///    (the in-memory PasskeyStore is verifiably empty the whole time);
/// 5. `POST /auth/passkeys/auth/verify` verifies a REAL ES256 assertion and
///    persists the advanced signature counter to PostgreSQL;
/// 6. only after that update is a session minted (DB-backed session store,
///    Set-Cookie present, session row bound to the account).
///
/// NOT proven here (explicitly): a real OS-process restart with a real browser
/// authenticator (Playwright follow-up slice), production cutover, FK
/// integrity, `webauthn_user_id` backfill.
#[tokio::test]
#[ignore = "requires DATABASE_URL pointing to direct PostgreSQL"]
async fn passkey_register_reload_auth_route_proof() {
    let pool1 = connect_pool().await;
    let account_id = unique_account_id("route-reload-auth");
    let email = format!("{account_id}@example.invalid");
    cleanup_route_proof_fixture(&pool1, &account_id).await;

    // Step 0: the account exists ONLY in PostgreSQL.
    let webauthn_user_id = Uuid::new_v4();
    sqlx::query(
        "INSERT INTO domain_accounts \
            (id, kind, title, map_state, radius_m, disabled, role, email, webauthn_user_id, public_payload, private_payload) \
         VALUES \
            ($1, 'garnrolle', 'Route Reload Proof User', 'not_on_map', 0, false, 'weber', $2, $3::uuid, '{}'::jsonb, '{}'::jsonb)",
    )
    .bind(&account_id)
    .bind(&email)
    .bind(webauthn_user_id.to_string())
    .execute(&pool1)
    .await
    .expect("failed to seed proof account in PostgreSQL");

    // Step 1: pre-restart app; accounts come from PostgreSQL via the
    // production startup loader.
    let state1 = postgres_route_proof_state(pool1.clone()).await;
    {
        let accounts = state1.accounts.read().await;
        let account = accounts
            .get(&account_id)
            .expect("account must be loaded from PostgreSQL");
        assert_eq!(
            account.webauthn_user_id, webauthn_user_id,
            "loaded account must carry the persisted webauthn_user_id"
        );
        assert_eq!(account.email.as_deref(), Some(email.as_str()));
    }

    // Real DB-backed session plus a real grant in the production grant store
    // (the same seam the merged browser proof uses via its testing route).
    let session = state1
        .sessions
        .create(account_id.clone(), Some("route-proof-device".to_string()))
        .await
        .expect("DB session backend must create the registration session");
    let cookie = format!("{SESSION_COOKIE_NAME}={}", session.id);
    let grant_id = state1
        .passkey_registration_grants
        .insert(account_id.clone(), session.device_id.clone());

    let app1 = route_proof_app(state1.clone());

    // Step 2a: register/options through the real route.
    let (status, _, options_body) = post_json(
        &app1,
        "/auth/passkeys/register/options",
        Some(&cookie),
        &json!({ "registration_grant_id": grant_id }),
    )
    .await;
    assert_eq!(
        status,
        StatusCode::OK,
        "register/options must succeed: {options_body}"
    );
    let registration_id = options_body["registration_id"]
        .as_str()
        .expect("registration_id must be present");
    let creation_challenge = options_body["options"]["publicKey"]["challenge"]
        .as_str()
        .expect("creation challenge must be present");
    let ceremony_user_id = options_body["options"]["publicKey"]["user"]["id"]
        .as_str()
        .expect("ceremony user id must be present");
    assert_eq!(
        URL_SAFE_NO_PAD
            .decode(ceremony_user_id)
            .expect("ceremony user id must be base64url"),
        webauthn_user_id.as_bytes().to_vec(),
        "ceremony must be bound to the PostgreSQL-loaded webauthn_user_id"
    );

    // Step 2b: register/verify with a REAL ES256 attestation.
    let authenticator =
        soft_passkey::SoftPasskey::new("commonthing.net", "https://commonthing.net");
    let (status, headers, verify_body) = post_json(
        &app1,
        "/auth/passkeys/register/verify",
        Some(&cookie),
        &json!({
            "registration_id": registration_id,
            "credential": authenticator.create_registration_response(creation_challenge),
        }),
    )
    .await;
    assert_eq!(
        status,
        StatusCode::OK,
        "register/verify must succeed: {verify_body}"
    );
    assert_eq!(verify_body, json!({ "ok": true }));
    assert!(
        !headers.contains_key(axum::http::header::SET_COOKIE),
        "register/verify must not mint a session"
    );

    // The credential landed in PostgreSQL — and NOT in the in-memory store.
    let credential_id = CredentialID::from(authenticator.credential_id.clone());
    let credential_key = credential_id_key(&credential_id);
    assert_eq!(
        stored_credential_counter(&pool1, &credential_key).await,
        0,
        "registered credential must be persisted with counter 0"
    );
    assert!(
        state1.passkeys.list_for_account(&account_id).is_empty(),
        "in-memory PasskeyStore must stay empty in postgres mode"
    );

    // Step 3: reinitialise. Drop the whole pre-restart app and close its pool;
    // rebuild every store from scratch and re-load accounts from PostgreSQL.
    drop(app1);
    drop(state1);
    pool1.close().await;

    let pool2 = connect_pool().await;
    let state2 = postgres_route_proof_state(pool2.clone()).await;
    let app2 = route_proof_app(state2.clone());

    // Step 4: auth/options must find the credential from PostgreSQL.
    let (status, headers, auth_options_body) = post_json(
        &app2,
        "/auth/passkeys/auth/options",
        None,
        &json!({ "email": email }),
    )
    .await;
    assert_eq!(
        status,
        StatusCode::OK,
        "auth/options must find the PostgreSQL credential after reload: {auth_options_body}"
    );
    assert!(
        !headers.contains_key(axum::http::header::SET_COOKIE),
        "auth/options must never set a cookie"
    );
    let authentication_id = auth_options_body["authentication_id"]
        .as_str()
        .expect("authentication_id must be present");
    let request_challenge = auth_options_body["options"]["publicKey"]["challenge"]
        .as_str()
        .expect("request challenge must be present");
    let allow_credentials = auth_options_body["options"]["publicKey"]["allowCredentials"]
        .as_array()
        .expect("allowCredentials must be present");
    assert_eq!(
        allow_credentials.len(),
        1,
        "exactly the reloaded credential must be allowed"
    );
    assert_eq!(
        allow_credentials[0]["id"].as_str(),
        Some(authenticator.credential_id_b64().as_str()),
        "allowCredentials must carry the PostgreSQL credential id"
    );

    // Steps 5 + 6: auth/verify with a REAL signature. The counter update must
    // land in PostgreSQL, and only then is a session minted.
    let (status, headers, auth_verify_body) = post_json(
        &app2,
        "/auth/passkeys/auth/verify",
        None,
        &json!({
            "authentication_id": authentication_id,
            "credential": authenticator.create_assertion_response(request_challenge, 1),
        }),
    )
    .await;
    assert_eq!(
        status,
        StatusCode::OK,
        "auth/verify must succeed after reload: {auth_verify_body}"
    );
    assert_eq!(auth_verify_body["ok"], json!(true));
    assert_eq!(
        auth_verify_body["account_id"].as_str(),
        Some(account_id.as_str())
    );
    let set_cookie = headers
        .get(axum::http::header::SET_COOKIE)
        .expect("auth/verify must mint a session cookie")
        .to_str()
        .expect("session cookie must be valid UTF-8");
    let session_prefix = format!("{SESSION_COOKIE_NAME}=");
    assert!(
        set_cookie.starts_with(&session_prefix),
        "auth/verify must set the session cookie, got: {set_cookie}"
    );

    // Step 5 evidence: the advanced signature counter crossed the PostgreSQL
    // boundary (0 -> 1), not just process memory.
    assert_eq!(
        stored_credential_counter(&pool2, &credential_key).await,
        1,
        "auth/verify must persist the advanced signature counter to PostgreSQL"
    );

    // Step 6 evidence: the minted session is DB-backed and account-bound.
    let session_id = set_cookie
        .split(';')
        .next()
        .expect("cookie must have a value segment")
        .trim_start_matches(&session_prefix)
        .to_string();
    let minted = state2
        .sessions
        .get(&session_id)
        .await
        .expect("session backend must answer")
        .expect("minted session must exist in the DB-backed store");
    assert_eq!(minted.account_id, account_id);

    cleanup_route_proof_fixture(&pool2, &account_id).await;
    pool2.close().await;
}
