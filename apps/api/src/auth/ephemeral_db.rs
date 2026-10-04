use chrono::{DateTime, Utc};
use serde_json::Value;
use sha2::{Digest, Sha256};
use sqlx::{PgPool, Postgres, Transaction};

#[derive(Clone)]
pub struct EphemeralDb {
    pool: PgPool,
}

#[derive(Debug, Clone)]
pub struct EphemeralRecord {
    pub payload: Value,
    pub account_id: Option<String>,
    pub device_id: Option<String>,
    pub created_at: DateTime<Utc>,
    pub expires_at: DateTime<Utc>,
}

pub struct NewEphemeralRecord<'a> {
    pub key_hash: &'a str,
    pub account_id: Option<&'a str>,
    pub device_id: Option<&'a str>,
    pub payload: &'a Value,
    pub created_at: DateTime<Utc>,
    pub expires_at: DateTime<Utc>,
}

#[derive(Debug, Clone, PartialEq)]
pub enum BoundConsumeResult {
    NotFound,
    BindingMismatch,
    Consumed(Value),
}

impl EphemeralDb {
    pub fn new(pool: PgPool) -> Self {
        Self { pool }
    }

    pub fn pool(&self) -> &PgPool {
        &self.pool
    }

    pub fn hash_opaque_id(value: &str) -> String {
        let mut hasher = Sha256::new();
        hasher.update(value.as_bytes());
        crate::auth::digest::encode_sha256_digest(hasher.finalize())
    }

    async fn lock_context(
        tx: &mut Transaction<'_, Postgres>,
        kind: &str,
        context_key: &str,
    ) -> Result<(), sqlx::Error> {
        let key = crate::advisory_lock::stable_advisory_lock_key(
            "weltgewebe:auth-ephemeral-context:v1",
            &[kind, context_key],
        );
        sqlx::query("SELECT pg_advisory_xact_lock($1::bigint)")
            .bind(key)
            .execute(&mut **tx)
            .await?;
        Ok(())
    }

    pub async fn insert(
        &self,
        kind: &str,
        context_key: Option<&str>,
        record: NewEphemeralRecord<'_>,
    ) -> Result<(), sqlx::Error> {
        sqlx::query(
            "INSERT INTO auth_ephemeral_state \
             (state_kind, key_hash, context_key, account_id, device_id, payload, created_at, expires_at) \
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
        )
        .bind(kind)
        .bind(record.key_hash)
        .bind(context_key)
        .bind(record.account_id)
        .bind(record.device_id)
        .bind(record.payload)
        .bind(record.created_at)
        .bind(record.expires_at)
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    /// Replace any active or stale record for one logical context and insert the
    /// new record under the same transaction-scoped advisory lock. Used for
    /// "newest request wins" semantics such as magic links per normalized email.
    pub async fn replace_context(
        &self,
        kind: &str,
        context_key: &str,
        record: NewEphemeralRecord<'_>,
    ) -> Result<(), sqlx::Error> {
        let mut tx = self.pool.begin().await?;
        Self::lock_context(&mut tx, kind, context_key).await?;
        sqlx::query("DELETE FROM auth_ephemeral_state WHERE state_kind = $1 AND context_key = $2")
            .bind(kind)
            .bind(context_key)
            .execute(&mut *tx)
            .await?;
        sqlx::query(
            "INSERT INTO auth_ephemeral_state \
             (state_kind, key_hash, context_key, account_id, device_id, payload, created_at, expires_at) \
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
        )
        .bind(kind)
        .bind(record.key_hash)
        .bind(context_key)
        .bind(record.account_id)
        .bind(record.device_id)
        .bind(record.payload)
        .bind(record.created_at)
        .bind(record.expires_at)
        .execute(&mut *tx)
        .await?;
        tx.commit().await?;
        Ok(())
    }

    /// Return an existing active record for a context or insert and return the
    /// supplied record. The advisory lock makes context reuse deterministic
    /// across API processes.
    pub async fn get_or_insert_context(
        &self,
        kind: &str,
        context_key: &str,
        record: NewEphemeralRecord<'_>,
    ) -> Result<EphemeralRecord, sqlx::Error> {
        let mut tx = self.pool.begin().await?;
        Self::lock_context(&mut tx, kind, context_key).await?;
        sqlx::query(
            "DELETE FROM auth_ephemeral_state \
             WHERE state_kind = $1 AND context_key = $2 AND expires_at <= NOW()",
        )
        .bind(kind)
        .bind(context_key)
        .execute(&mut *tx)
        .await?;
        let existing = sqlx::query_as::<
            _,
            (
                Value,
                Option<String>,
                Option<String>,
                DateTime<Utc>,
                DateTime<Utc>,
            ),
        >(
            "SELECT payload, account_id, device_id, created_at, expires_at \
             FROM auth_ephemeral_state \
             WHERE state_kind = $1 AND context_key = $2 AND expires_at > NOW() \
             FOR UPDATE",
        )
        .bind(kind)
        .bind(context_key)
        .fetch_optional(&mut *tx)
        .await?;
        if let Some((payload, account_id, device_id, created_at, expires_at)) = existing {
            tx.commit().await?;
            return Ok(EphemeralRecord {
                payload,
                account_id,
                device_id,
                created_at,
                expires_at,
            });
        }
        sqlx::query(
            "INSERT INTO auth_ephemeral_state \
             (state_kind, key_hash, context_key, account_id, device_id, payload, created_at, expires_at) \
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
        )
        .bind(kind)
        .bind(record.key_hash)
        .bind(context_key)
        .bind(record.account_id)
        .bind(record.device_id)
        .bind(record.payload)
        .bind(record.created_at)
        .bind(record.expires_at)
        .execute(&mut *tx)
        .await?;
        tx.commit().await?;
        Ok(EphemeralRecord {
            payload: record.payload.clone(),
            account_id: record.account_id.map(ToOwned::to_owned),
            device_id: record.device_id.map(ToOwned::to_owned),
            created_at: record.created_at,
            expires_at: record.expires_at,
        })
    }

    pub async fn peek(
        &self,
        kind: &str,
        key_hash: &str,
    ) -> Result<Option<EphemeralRecord>, sqlx::Error> {
        let row = sqlx::query_as::<
            _,
            (
                Value,
                Option<String>,
                Option<String>,
                DateTime<Utc>,
                DateTime<Utc>,
            ),
        >(
            "SELECT payload, account_id, device_id, created_at, expires_at \
             FROM auth_ephemeral_state \
             WHERE state_kind = $1 AND key_hash = $2 AND expires_at > NOW()",
        )
        .bind(kind)
        .bind(key_hash)
        .fetch_optional(&self.pool)
        .await?;
        Ok(row.map(
            |(payload, account_id, device_id, created_at, expires_at)| EphemeralRecord {
                payload,
                account_id,
                device_id,
                created_at,
                expires_at,
            },
        ))
    }

    pub async fn consume(
        &self,
        kind: &str,
        key_hash: &str,
    ) -> Result<Option<EphemeralRecord>, sqlx::Error> {
        let row = sqlx::query_as::<
            _,
            (
                Value,
                Option<String>,
                Option<String>,
                DateTime<Utc>,
                DateTime<Utc>,
            ),
        >(
            "DELETE FROM auth_ephemeral_state \
             WHERE state_kind = $1 AND key_hash = $2 AND expires_at > NOW() \
             RETURNING payload, account_id, device_id, created_at, expires_at",
        )
        .bind(kind)
        .bind(key_hash)
        .fetch_optional(&self.pool)
        .await?;
        if row.is_none() {
            sqlx::query(
                "DELETE FROM auth_ephemeral_state \
                 WHERE state_kind = $1 AND key_hash = $2 AND expires_at <= NOW()",
            )
            .bind(kind)
            .bind(key_hash)
            .execute(&self.pool)
            .await?;
        }
        Ok(row.map(
            |(payload, account_id, device_id, created_at, expires_at)| EphemeralRecord {
                payload,
                account_id,
                device_id,
                created_at,
                expires_at,
            },
        ))
    }

    pub async fn consume_bound(
        &self,
        kind: &str,
        key_hash: &str,
        expected_account_id: Option<&str>,
        expected_device_id: Option<&str>,
        payload_binding: Option<(&str, &str)>,
    ) -> Result<BoundConsumeResult, sqlx::Error> {
        let mut tx = self.pool.begin().await?;
        let row = sqlx::query_as::<_, (Value, Option<String>, Option<String>, DateTime<Utc>)>(
            "SELECT payload, account_id, device_id, expires_at \
             FROM auth_ephemeral_state \
             WHERE state_kind = $1 AND key_hash = $2 \
             FOR UPDATE",
        )
        .bind(kind)
        .bind(key_hash)
        .fetch_optional(&mut *tx)
        .await?;
        let Some((payload, account_id, device_id, expires_at)) = row else {
            tx.commit().await?;
            return Ok(BoundConsumeResult::NotFound);
        };
        if expires_at <= Utc::now() {
            sqlx::query("DELETE FROM auth_ephemeral_state WHERE state_kind = $1 AND key_hash = $2")
                .bind(kind)
                .bind(key_hash)
                .execute(&mut *tx)
                .await?;
            tx.commit().await?;
            return Ok(BoundConsumeResult::NotFound);
        }
        let account_matches = expected_account_id.is_none_or(|expected| {
            account_id
                .as_deref()
                .is_some_and(|observed| observed == expected)
        });
        let device_matches = expected_device_id.is_none_or(|expected| {
            device_id
                .as_deref()
                .is_some_and(|observed| observed == expected)
        });
        let payload_matches = payload_binding.is_none_or(|(field, expected)| {
            payload.get(field).and_then(Value::as_str) == Some(expected)
        });
        if !(account_matches && device_matches && payload_matches) {
            tx.commit().await?;
            return Ok(BoundConsumeResult::BindingMismatch);
        }
        sqlx::query("DELETE FROM auth_ephemeral_state WHERE state_kind = $1 AND key_hash = $2")
            .bind(kind)
            .bind(key_hash)
            .execute(&mut *tx)
            .await?;
        tx.commit().await?;
        Ok(BoundConsumeResult::Consumed(payload))
    }

    /// Insert one active record only when the global per-kind capacity has not
    /// been reached. The transaction-scoped advisory lock serializes capacity
    /// checks across API processes.
    pub async fn insert_with_capacity(
        &self,
        kind: &str,
        record: NewEphemeralRecord<'_>,
        max_entries: i64,
    ) -> Result<bool, sqlx::Error> {
        let mut tx = self.pool.begin().await?;
        Self::lock_context(&mut tx, kind, "__capacity__").await?;
        let active: i64 = sqlx::query_scalar(
            "SELECT COUNT(*) FROM auth_ephemeral_state WHERE state_kind = $1 AND expires_at > NOW()",
        )
        .bind(kind)
        .fetch_one(&mut *tx)
        .await?;
        if active >= max_entries {
            tx.commit().await?;
            return Ok(false);
        }
        sqlx::query(
            "INSERT INTO auth_ephemeral_state \
             (state_kind, key_hash, account_id, device_id, payload, created_at, expires_at) \
             VALUES ($1, $2, $3, $4, $5, $6, $7)",
        )
        .bind(kind)
        .bind(record.key_hash)
        .bind(record.account_id)
        .bind(record.device_id)
        .bind(record.payload)
        .bind(record.created_at)
        .bind(record.expires_at)
        .execute(&mut *tx)
        .await?;
        tx.commit().await?;
        Ok(true)
    }

    pub async fn count_active(&self, kind: &str) -> Result<i64, sqlx::Error> {
        sqlx::query_scalar(
            "SELECT COUNT(*) FROM auth_ephemeral_state \
             WHERE state_kind = $1 AND expires_at > NOW()",
        )
        .bind(kind)
        .fetch_one(&self.pool)
        .await
    }

    pub async fn cleanup_expired(&self, kind: &str) -> Result<u64, sqlx::Error> {
        let result = sqlx::query(
            "DELETE FROM auth_ephemeral_state WHERE state_kind = $1 AND expires_at <= NOW()",
        )
        .bind(kind)
        .execute(&self.pool)
        .await?;
        Ok(result.rows_affected())
    }
}

pub async fn cleanup_expired_shared_auth(pool: &PgPool) -> Result<(u64, u64), sqlx::Error> {
    let auth = sqlx::query("DELETE FROM auth_ephemeral_state WHERE expires_at <= NOW()")
        .execute(pool)
        .await?
        .rows_affected();
    let limits = sqlx::query("DELETE FROM auth_rate_limit_counters WHERE expires_at <= NOW()")
        .execute(pool)
        .await?
        .rows_affected();
    Ok((auth, limits))
}

pub fn spawn_cleanup_loop(
    pool: PgPool,
    stop: crate::shutdown::ShutdownSignal,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        let mut ticker = tokio::time::interval(std::time::Duration::from_secs(60));
        let stop = stop.wait();
        tokio::pin!(stop);
        loop {
            tokio::select! {
                _ = ticker.tick() => {}
                () = &mut stop => break,
            }
            match cleanup_expired_shared_auth(&pool).await {
                Ok((auth, limits)) if auth > 0 || limits > 0 => tracing::debug!(
                    auth_rows = auth,
                    rate_limit_rows = limits,
                    "Expired shared authentication state removed"
                ),
                Ok(_) => {}
                Err(error) => tracing::warn!(
                    error = %error,
                    "Expired shared authentication state cleanup failed"
                ),
            }
        }
    })
}

#[cfg(test)]
mod cleanup_loop_tests {
    use super::spawn_cleanup_loop;
    use crate::shutdown::Shutdown;
    use sqlx::postgres::PgPoolOptions;
    use std::time::Duration;

    #[tokio::test]
    async fn cleanup_loop_stops_on_shutdown_signal() {
        // Nothing listens on port 1, so every cleanup round fails fast; the loop
        // must still keep running until the signal and then end.
        let pool = PgPoolOptions::new()
            .acquire_timeout(Duration::from_millis(200))
            .connect_lazy("postgres://cleanup@127.0.0.1:1/cleanup")
            .unwrap();
        let shutdown = Shutdown::new();
        let handle = spawn_cleanup_loop(pool, shutdown.signal());
        tokio::time::sleep(Duration::from_millis(100)).await;
        assert!(!handle.is_finished(), "loop must run until shutdown");

        shutdown.trigger();
        tokio::time::timeout(Duration::from_secs(2), handle)
            .await
            .expect("cleanup loop must stop after the shutdown signal")
            .unwrap();
    }
}
