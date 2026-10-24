//! Database-backed passkey credential store for direct PostgreSQL persistence.
//!
//! This mirrors the in-memory [`PasskeyStore`](super::passkeys::PasskeyStore)
//! API but persists registered WebAuthn credentials in the
//! `passkey_credentials` table so they survive process restarts.
//!
//! ## Scope
//!
//! This is the persistence layer only (AUTH-PG-002, store slice). The routes
//! are **not** switched to it here; the in-memory store remains the default. A
//! follow-up slice introduces the opt-in runtime facade that selects this
//! backend behind explicit configuration.
//!
//! ## Security
//!
//! * Stored credentials are server-side WebAuthn *public* credential records
//!   plus the signature counter and backup flags (no private key). They are
//!   nevertheless treated as private auth data: never log them in raw form and
//!   never expose them through public account projections.
//! * [`DbPasskeyStore::find_by_credential_id`] is global (cross-account) by
//!   design — it resolves the owning account for a presented assertion. It
//!   authorises nothing on its own.
//! * [`DbPasskeyStore::update_credential`] is owner-bound (`account_id` +
//!   `credential_id`) as defence-in-depth and runs inside a transaction with a
//!   row lock so the signature counter cannot regress under a concurrent
//!   update.
//! * The `credential_id` PRIMARY KEY is the final source of truth for duplicate
//!   detection; a duplicate insert maps to
//!   [`DbPasskeyStoreError::DuplicateCredentialId`].
//!
//! ## sqlx binding note
//!
//! The crate's sqlx build has no `uuid` or `json` feature (see `Cargo.toml`),
//! so — exactly like `domain_db` — `UUID` is bound as text with a `$n::uuid`
//! cast and `JSONB` is bound/read as text with `$n::jsonb` / `column::text`.

use sqlx::{PgPool, Row};
use uuid::Uuid;
use webauthn_rs::prelude::*;

use super::passkeys::StoredPasskey;

/// Errors returned by [`DbPasskeyStore`] operations.
#[derive(Debug, thiserror::Error)]
pub enum DbPasskeyStoreError {
    #[error("passkey credential already exists")]
    DuplicateCredentialId,
    #[error("credential not found for account")]
    NotFound,
    #[error("passkey credential-id encoding failed")]
    CredentialIdEncoding,
    #[error("passkey credential (de)serialization failed")]
    Serialization(#[source] serde_json::Error),
    #[error("passkey credential backend unavailable")]
    Backend(#[source] sqlx::Error),
}

/// Stable, deterministic text key for a credential ID.
///
/// The key is the lowercase hex encoding of the raw credential-ID bytes.
/// `CredentialID` derefs to its byte vector (`AsRef<[u8]>`), so the key is
/// derived from the bytes rather than from any internal wrapper representation
/// — it stays stable across webauthn-rs point releases. It is used as the
/// `passkey_credentials.credential_id` primary key.
pub fn credential_id_key(credential_id: &CredentialID) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let bytes: &[u8] = credential_id.as_ref();
    let mut encoded = Vec::with_capacity(bytes.len() * 2);
    for byte in bytes {
        encoded.push(HEX[(byte >> 4) as usize]);
        encoded.push(HEX[(byte & 0x0f) as usize]);
    }
    String::from_utf8(encoded).expect("hex encoding emits valid UTF-8")
}

fn hex_nibble(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        b'A'..=b'F' => Some(byte - b'A' + 10),
        _ => None,
    }
}

fn credential_id_from_key(key: &str) -> Result<CredentialID, DbPasskeyStoreError> {
    if key.len() % 2 != 0 {
        return Err(DbPasskeyStoreError::CredentialIdEncoding);
    }

    let mut bytes = Vec::with_capacity(key.len() / 2);
    for pair in key.as_bytes().chunks_exact(2) {
        let high = hex_nibble(pair[0]).ok_or(DbPasskeyStoreError::CredentialIdEncoding)?;
        let low = hex_nibble(pair[1]).ok_or(DbPasskeyStoreError::CredentialIdEncoding)?;
        bytes.push((high << 4) | low);
    }
    Ok(CredentialID::from(bytes))
}

fn passkey_from_text(text: &str) -> Result<Passkey, DbPasskeyStoreError> {
    serde_json::from_str(text).map_err(DbPasskeyStoreError::Serialization)
}

fn passkey_to_text(passkey: &Passkey) -> Result<String, DbPasskeyStoreError> {
    serde_json::to_string(passkey).map_err(DbPasskeyStoreError::Serialization)
}

/// Database-backed store for registered passkey credentials.
///
/// Credentials inserted here survive across server restarts.
#[derive(Clone)]
pub struct DbPasskeyStore {
    pool: PgPool,
}

impl DbPasskeyStore {
    pub fn new(pool: PgPool) -> Self {
        Self { pool }
    }

    /// Inserts a passkey for an account.
    ///
    /// Rejects duplicate credential IDs globally via the `credential_id`
    /// primary key — the database is the final arbiter, so concurrent inserts
    /// of the same credential are resolved correctly.
    pub async fn insert(
        &self,
        account_id: &str,
        webauthn_user_id: Uuid,
        passkey: &Passkey,
    ) -> Result<(), DbPasskeyStoreError> {
        let key = credential_id_key(passkey.cred_id());
        let credential = passkey_to_text(passkey)?;
        // Coordinate passkey writes with generation-fenced GETs and guest exit.
        // Reserve before SQLx can acquire a connection and wait on FK locks.
        let _pool_budget = crate::state::reserve_postgres_domain_operation()
            .await
            .map_err(DbPasskeyStoreError::Backend)?;

        let result = sqlx::query(
            "INSERT INTO passkey_credentials \
                 (credential_id, account_id, webauthn_user_id, credential, created_at, updated_at) \
             VALUES ($1, $2, $3::uuid, $4::jsonb, NOW(), NOW()) \
             ON CONFLICT (credential_id) DO NOTHING",
        )
        .bind(&key)
        .bind(account_id)
        .bind(webauthn_user_id.to_string())
        .bind(&credential)
        .execute(&self.pool)
        .await
        .map_err(DbPasskeyStoreError::Backend)?;

        if result.rows_affected() == 0 {
            return Err(DbPasskeyStoreError::DuplicateCredentialId);
        }
        Ok(())
    }

    /// Lists all passkeys owned by the given account, oldest first.
    pub async fn list_for_account(
        &self,
        account_id: &str,
    ) -> Result<Vec<Passkey>, DbPasskeyStoreError> {
        let rows = sqlx::query(
            "SELECT credential::text AS credential FROM passkey_credentials \
             WHERE account_id = $1 \
             ORDER BY created_at, credential_id",
        )
        .bind(account_id)
        .fetch_all(&self.pool)
        .await
        .map_err(DbPasskeyStoreError::Backend)?;

        rows.into_iter()
            .map(|row| {
                let text: String = row
                    .try_get("credential")
                    .map_err(DbPasskeyStoreError::Backend)?;
                passkey_from_text(&text)
            })
            .collect()
    }

    /// Returns all credential IDs for passkeys owned by the given account.
    ///
    /// Used by `register/options` to populate `exclude_credentials`; it must
    /// read from the persistent store so the duplicate-registration policy
    /// holds across restarts.
    pub async fn credential_ids_for_account(
        &self,
        account_id: &str,
    ) -> Result<Vec<CredentialID>, DbPasskeyStoreError> {
        let rows = sqlx::query(
            "SELECT credential_id FROM passkey_credentials \
             WHERE account_id = $1 \
             ORDER BY created_at, credential_id",
        )
        .bind(account_id)
        .fetch_all(&self.pool)
        .await
        .map_err(DbPasskeyStoreError::Backend)?;

        rows.into_iter()
            .map(|row| {
                let key: String = row
                    .try_get("credential_id")
                    .map_err(DbPasskeyStoreError::Backend)?;
                credential_id_from_key(&key)
            })
            .collect()
    }

    /// Finds a passkey by credential ID across all accounts.
    ///
    /// Cross-account by design: it resolves the owning account for a presented
    /// assertion. It does not authorise anything on its own.
    pub async fn find_by_credential_id(
        &self,
        credential_id: &CredentialID,
    ) -> Result<Option<StoredPasskey>, DbPasskeyStoreError> {
        let key = credential_id_key(credential_id);
        let row = sqlx::query(
            "SELECT account_id, credential::text AS credential FROM passkey_credentials \
             WHERE credential_id = $1",
        )
        .bind(&key)
        .fetch_optional(&self.pool)
        .await
        .map_err(DbPasskeyStoreError::Backend)?;

        let Some(row) = row else {
            return Ok(None);
        };

        let account_id: String = row
            .try_get("account_id")
            .map_err(DbPasskeyStoreError::Backend)?;
        let text: String = row
            .try_get("credential")
            .map_err(DbPasskeyStoreError::Backend)?;
        let passkey = passkey_from_text(&text)?;
        Ok(Some(StoredPasskey {
            account_id,
            passkey,
        }))
    }

    /// Removes a credential for an account.
    ///
    /// Returns `true` if the credential existed for the given account and was
    /// removed. Credentials of other accounts are left untouched (the
    /// `account_id` predicate keeps the delete owner-bound).
    pub async fn remove_for_account(
        &self,
        account_id: &str,
        credential_id: &CredentialID,
    ) -> Result<bool, DbPasskeyStoreError> {
        let key = credential_id_key(credential_id);
        let _pool_budget = crate::state::reserve_postgres_domain_operation()
            .await
            .map_err(DbPasskeyStoreError::Backend)?;
        let result = sqlx::query(
            "DELETE FROM passkey_credentials \
             WHERE account_id = $1 AND credential_id = $2",
        )
        .bind(account_id)
        .bind(&key)
        .execute(&self.pool)
        .await
        .map_err(DbPasskeyStoreError::Backend)?;

        Ok(result.rows_affected() > 0)
    }

    /// Applies a WebAuthn [`AuthenticationResult`] (signature counter, backup
    /// flags) to the stored credential it refers to.
    ///
    /// The credential must belong to `account_id` (owner-bound,
    /// defence-in-depth); a missing credential or a cross-account mismatch
    /// fail-closes with [`DbPasskeyStoreError::NotFound`]. The read-modify-write
    /// runs inside a transaction with `SELECT ... FOR UPDATE` so the counter
    /// cannot regress under a concurrent update.
    ///
    /// Returns `true` if the stored credential changed, `false` if it matched
    /// but nothing changed (e.g. a synced passkey reporting a constant counter).
    pub async fn update_credential(
        &self,
        account_id: &str,
        auth_result: &AuthenticationResult,
    ) -> Result<bool, DbPasskeyStoreError> {
        let key = credential_id_key(auth_result.cred_id());
        // UPDATE ... FOR UPDATE can wait for a guest-exit DELETE while that exit
        // waits for a protected GET's generation row lock. Join the existing
        // weighted pool budget before checking out the SQLx transaction.
        let _pool_budget = crate::state::reserve_postgres_domain_operation()
            .await
            .map_err(DbPasskeyStoreError::Backend)?;

        let mut tx = self
            .pool
            .begin()
            .await
            .map_err(DbPasskeyStoreError::Backend)?;

        let row = sqlx::query(
            "SELECT credential::text AS credential FROM passkey_credentials \
             WHERE credential_id = $1 AND account_id = $2 \
             FOR UPDATE",
        )
        .bind(&key)
        .bind(account_id)
        .fetch_optional(&mut *tx)
        .await
        .map_err(DbPasskeyStoreError::Backend)?;

        let Some(row) = row else {
            // tx is dropped (rolled back) on return.
            return Err(DbPasskeyStoreError::NotFound);
        };

        let text: String = row
            .try_get("credential")
            .map_err(DbPasskeyStoreError::Backend)?;
        let mut passkey = passkey_from_text(&text)?;

        // `Passkey::update_credential` only yields `None` on a credential-id
        // mismatch. We located the row by `auth_result.cred_id()`, so `None`
        // is unreachable here; surface it as a hard failure rather than
        // silently smoothing it to "no change".
        let changed = match passkey.update_credential(auth_result) {
            Some(changed) => changed,
            None => return Err(DbPasskeyStoreError::NotFound),
        };

        if changed {
            let updated = passkey_to_text(&passkey)?;
            sqlx::query(
                "UPDATE passkey_credentials \
                 SET credential = $1::jsonb, updated_at = NOW(), last_used_at = NOW() \
                 WHERE credential_id = $2 AND account_id = $3",
            )
            .bind(&updated)
            .bind(&key)
            .bind(account_id)
            .execute(&mut *tx)
            .await
            .map_err(DbPasskeyStoreError::Backend)?;
        }

        tx.commit().await.map_err(DbPasskeyStoreError::Backend)?;
        Ok(changed)
    }
}

#[cfg(test)]
mod tests {
    //! Pure (no-database) tests that pin the two persistence invariants the DB
    //! store relies on: that a `Passkey` survives a JSON round-trip (the JSONB
    //! column form) unchanged, and that the credential-ID text key is a stable,
    //! reversible encoding of the raw credential-ID bytes.
    //!
    //! The insert/list/find/update/remove behaviour against a real database is
    //! covered by the ignored integration proof in
    //! `tests/db_passkey_store_persistence.rs`.

    use super::*;
    use serde_json::json;

    fn test_passkey(credential_seed: u8) -> Passkey {
        // The `cred_id` field inside the Passkey JSON is base64url (webauthn-rs
        // serde format); base64 is a dev-dependency available to unit tests.
        use base64::engine::general_purpose::URL_SAFE_NO_PAD;
        use base64::Engine;
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

    fn decode_hex(s: &str) -> Vec<u8> {
        assert!(s.len() % 2 == 0, "hex string must have even length");
        (0..s.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&s[i..i + 2], 16).expect("valid hex pair"))
            .collect()
    }

    #[test]
    fn passkey_survives_json_roundtrip() {
        let passkey = test_passkey(9);
        // `to_string` / `from_str` is exactly what the JSONB text binding uses.
        let text = passkey_to_text(&passkey).expect("passkey serializes to json text");
        let restored = passkey_from_text(&text).expect("passkey deserializes from json text");
        assert_eq!(
            restored.cred_id(),
            passkey.cred_id(),
            "credential id must be preserved across the JSONB round-trip"
        );
    }

    #[test]
    fn credential_id_key_is_stable_and_reversible() {
        let bytes = vec![0xABu8; 32];
        let credential_id = CredentialID::from(bytes.clone());
        let key = credential_id_key(&credential_id);

        // Deterministic: same input -> same key.
        assert_eq!(key, credential_id_key(&credential_id));

        // Reversible: the key decodes back to the original bytes.
        assert_eq!(
            decode_hex(&key),
            bytes,
            "key must round-trip back to the raw bytes"
        );
    }

    #[test]
    fn credential_id_key_matches_passkey_cred_id() {
        let passkey = test_passkey(42);
        let key = credential_id_key(passkey.cred_id());
        assert_eq!(
            decode_hex(&key),
            vec![42u8; 32],
            "key derived from a passkey must match its raw credential-id bytes"
        );
    }

    #[test]
    fn credential_id_from_key_round_trips_lowercase_and_uppercase_hex() {
        let bytes = vec![0xABu8; 32];
        let lowercase = "ab".repeat(32);
        let uppercase = "AB".repeat(32);
        assert_eq!(credential_id_from_key(&lowercase).unwrap().as_ref(), bytes);
        assert_eq!(credential_id_from_key(&uppercase).unwrap().as_ref(), bytes);
    }

    #[test]
    fn credential_id_from_key_rejects_malformed_hex() {
        assert!(matches!(
            credential_id_from_key("abc"),
            Err(DbPasskeyStoreError::CredentialIdEncoding)
        ));
        assert!(matches!(
            credential_id_from_key("zz"),
            Err(DbPasskeyStoreError::CredentialIdEncoding)
        ));
    }
}
