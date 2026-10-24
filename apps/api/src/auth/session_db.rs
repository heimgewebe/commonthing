use async_trait::async_trait;
use sqlx::{postgres::PgRow, PgPool, Row};
use uuid::Uuid;

use super::session::{Session, SessionBackendError, SessionLifetime, SessionOps, SessionResult};

// Trusted static SQL fragment shared by the session queries below. It must
// never contain configuration or request data; all external values remain
// parameter-bound through sqlx.
// Session IO runs outside the projection fence in authentication middleware,
// but can block on row locks held by guest exit. All SQLx session operations
// must therefore share the same admission budget with generation-fenced reads
// and canonical domain writers; no new queue or connection pool is needed.
async fn reserve_sqlx_session_capacity() -> SessionResult<tokio::sync::SemaphorePermit<'static>> {
    crate::state::reserve_postgres_domain_operation()
        .await
        .map_err(|_| SessionBackendError::Unavailable)
}

const SESSION_COLUMNS_WITH_REMAINING_LIFETIME: &str =
    "id, account_id, device_id, created_at, last_active, expires_at, \
     GREATEST(0, FLOOR(EXTRACT(EPOCH FROM (expires_at - NOW()))))::BIGINT \
     AS cookie_max_age_seconds";

/// Database-backed session store for direct PostgreSQL persistence.
/// Sessions created here survive across server restarts.
///
/// PostgreSQL is the sole clock authority for persisted sessions. Creation,
/// expiry checks, and the remaining browser-cookie lifetime are therefore all
/// calculated from the same `NOW()` value inside each statement. This keeps a
/// database-backed browser cookie from outliving its server-side session even
/// when the API and database hosts have slightly different wall clocks.
#[derive(Clone)]
pub struct DbSessionStore {
    pool: PgPool,
    lifetime: SessionLifetime,
}

impl DbSessionStore {
    /// Build a database-backed store with `SessionLifetime::default()`. This
    /// constructor is deterministic and does not read runtime environment; the
    /// application composition root injects validated configuration through
    /// `with_lifetime`.
    pub fn new(pool: PgPool) -> Self {
        Self::with_lifetime(pool, SessionLifetime::default())
    }

    pub fn with_lifetime(pool: PgPool, lifetime: SessionLifetime) -> Self {
        Self { pool, lifetime }
    }

    fn session_from_row(row: PgRow) -> Result<Session, sqlx::Error> {
        Ok(Session {
            id: row.try_get("id")?,
            account_id: row.try_get("account_id")?,
            device_id: row.try_get("device_id")?,
            created_at: row.try_get("created_at")?,
            last_active: row.try_get("last_active")?,
            expires_at: row.try_get("expires_at")?,
            cookie_max_age_seconds: row.try_get("cookie_max_age_seconds")?,
        })
    }
}

#[async_trait]
impl SessionOps for DbSessionStore {
    async fn create(
        &self,
        account_id: String,
        existing_device_id: Option<String>,
    ) -> SessionResult<Session> {
        let _session_budget = reserve_sqlx_session_capacity().await?;
        let session_id = Uuid::new_v4().to_string();
        let device_id = existing_device_id.unwrap_or_else(|| Uuid::new_v4().to_string());
        // SAFETY: the only interpolated fragment is the trusted module constant;
        // all account/session values remain parameter-bound below.
        let query = format!(
            "INSERT INTO sessions (id, account_id, device_id, created_at, last_active, expires_at)
             VALUES ($1, $2, $3, NOW(), NOW(),
                     NOW() + ($4::DOUBLE PRECISION * INTERVAL '1 second'))
             RETURNING {SESSION_COLUMNS_WITH_REMAINING_LIFETIME}"
        );
        let row = sqlx::query(&query)
            .bind(&session_id)
            .bind(&account_id)
            .bind(&device_id)
            .bind(self.lifetime.seconds())
            .fetch_one(&self.pool)
            .await
            .map_err(|_| SessionBackendError::Unavailable)?;

        Self::session_from_row(row).map_err(|_| SessionBackendError::Unavailable)
    }

    async fn get(&self, session_id: &str) -> SessionResult<Option<Session>> {
        // PostgreSQL executes the data-modifying CTE exactly once. Referencing
        // it from the SELECT makes that side effect explicit; concurrent readers
        // remain safe under MVCC because a losing DELETE yields an empty CTE and
        // the expiry predicate still prevents the stale row from being returned.
        // SAFETY: the only interpolated fragment is the trusted module constant;
        // the session id remains parameter-bound below.
        let _session_budget = reserve_sqlx_session_capacity().await?;
        let query = format!(
            "WITH expired AS (
                 DELETE FROM sessions
                 WHERE id = $1 AND expires_at <= NOW()
                 RETURNING id
             )
             SELECT {SESSION_COLUMNS_WITH_REMAINING_LIFETIME}
             FROM sessions
             WHERE id = $1
               AND expires_at > NOW()
               AND NOT EXISTS (SELECT 1 FROM expired)"
        );
        let row = sqlx::query(&query)
            .bind(session_id)
            .fetch_optional(&self.pool)
            .await
            .map_err(|_| SessionBackendError::Unavailable)?;

        row.map(Self::session_from_row)
            .transpose()
            .map_err(|_| SessionBackendError::Unavailable)
    }

    async fn delete(&self, session_id: &str) -> SessionResult<()> {
        let _session_budget = reserve_sqlx_session_capacity().await?;
        sqlx::query("DELETE FROM sessions WHERE id = $1")
            .bind(session_id)
            .execute(&self.pool)
            .await
            .map_err(|_| SessionBackendError::Unavailable)?;

        Ok(())
    }

    async fn touch(&self, session_id: &str) -> SessionResult<()> {
        let _session_budget = reserve_sqlx_session_capacity().await?;
        sqlx::query(
            "UPDATE sessions
             SET last_active = NOW()
             WHERE id = $1
               AND expires_at > NOW()
               AND last_active < NOW() - INTERVAL '5 minutes'",
        )
        .bind(session_id)
        .execute(&self.pool)
        .await
        .map_err(|_| SessionBackendError::Unavailable)?;

        Ok(())
    }

    async fn list_by_account(&self, account_id: &str) -> SessionResult<Vec<Session>> {
        // SAFETY: the only interpolated fragment is the trusted module constant;
        // the account id remains parameter-bound below.
        let _session_budget = reserve_sqlx_session_capacity().await?;
        let query = format!(
            "SELECT {SESSION_COLUMNS_WITH_REMAINING_LIFETIME}
             FROM sessions
             WHERE account_id = $1 AND expires_at > NOW()
             ORDER BY created_at DESC"
        );
        let rows = sqlx::query(&query)
            .bind(account_id)
            .fetch_all(&self.pool)
            .await
            .map_err(|_| SessionBackendError::Unavailable)?;

        rows.into_iter()
            .map(Self::session_from_row)
            .collect::<Result<Vec<_>, _>>()
            .map_err(|_| SessionBackendError::Unavailable)
    }

    async fn delete_by_device(&self, account_id: &str, device_id: &str) -> SessionResult<()> {
        let _session_budget = reserve_sqlx_session_capacity().await?;
        sqlx::query(
            "DELETE FROM sessions 
             WHERE account_id = $1 AND device_id = $2",
        )
        .bind(account_id)
        .bind(device_id)
        .execute(&self.pool)
        .await
        .map_err(|_| SessionBackendError::Unavailable)?;

        Ok(())
    }

    async fn delete_all_by_account(&self, account_id: &str) -> SessionResult<()> {
        let _session_budget = reserve_sqlx_session_capacity().await?;
        sqlx::query("DELETE FROM sessions WHERE account_id = $1")
            .bind(account_id)
            .execute(&self.pool)
            .await
            .map_err(|_| SessionBackendError::Unavailable)?;

        Ok(())
    }
}
