//! Two real API OS processes must agree on committed PostgreSQL visibility.
//!
//! This is deliberately NOT two Axum routers in one process. It starts the
//! actual production API binary twice, with separate process-local node caches.
//! Run only against a disposable direct PostgreSQL DATABASE_URL via the
//! existing run-postgres-integration-proofs.sh reset/guard contract.

#![cfg(unix)]
mod support;

use anyhow::{anyhow, bail, Context, Result};
use serde_json::Value;
use sqlx::PgPool;
use std::{
    net::TcpListener,
    path::Path,
    process::{Child, Command, Stdio},
    time::Duration,
};
use tokio::time::sleep;

struct ApiChildren(Vec<Child>);

impl Drop for ApiChildren {
    fn drop(&mut self) {
        for child in &mut self.0 {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

fn free_loopback_port() -> Result<u16> {
    let listener = TcpListener::bind("127.0.0.1:0").context("reserve temporary loopback port")?;
    Ok(listener.local_addr()?.port())
}

fn start_api(children: &mut ApiChildren, home: &Path, db_url: &str, port: u16) -> Result<()> {
    std::fs::create_dir_all(home)?;
    let child = Command::new(env!("CARGO_BIN_EXE_weltgewebe-api"))
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("HOME", home)
        .env("XDG_CONFIG_HOME", home.join(".config"))
        .env("XDG_DATA_HOME", home.join(".local/share"))
        .env("XDG_STATE_HOME", home.join(".local/state"))
        .env("DATABASE_URL", db_url)
        .env("API_BIND", format!("127.0.0.1:{port}"))
        .env("WELTGEWEBE_API_STARTUP_MIGRATIONS", "verify-applied")
        .env("WELTGEWEBE_DOMAIN_READ_SOURCE", "postgres")
        .env("WELTGEWEBE_DOMAIN_ACCOUNT_WRITE_SOURCE", "postgres")
        .env("WELTGEWEBE_DOMAIN_NODE_WRITE_SOURCE", "postgres")
        .env("WELTGEWEBE_DOMAIN_EDGE_WRITE_SOURCE", "postgres")
        .env("AUTH_GUEST_EXIT_STEP_UP_ENABLED", "1")
        .env("RUST_LOG", "error")
        .current_dir(home)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .context("spawn real weltgewebe-api child process")?;
    children.0.push(child);
    Ok(())
}

async fn wait_for_listener(
    children: &mut ApiChildren,
    port: u16,
    client: &reqwest::Client,
) -> Result<()> {
    for _ in 0..200 {
        for child in &mut children.0 {
            if let Some(status) = child.try_wait()? {
                bail!("independent API child terminated before HTTP startup: {status}");
            }
        }
        // Readiness only means the real HTTP listener accepts a response; the
        // protected node requests below establish their own status/content.
        if client
            .get(format!("http://127.0.0.1:{port}/health"))
            .send()
            .await
            .is_ok()
        {
            return Ok(());
        }
        sleep(Duration::from_millis(50)).await;
    }
    bail!("independent API listener {port} did not become reachable")
}

fn contains_exact_node_id(value: &Value, id: &str) -> bool {
    match value {
        Value::Array(rows) => rows.iter().any(|row| contains_exact_node_id(row, id)),
        Value::Object(map) => {
            map.get("id").and_then(Value::as_str) == Some(id)
                || map.values().any(|row| contains_exact_node_id(row, id))
        }
        _ => false,
    }
}

async fn listed(client: &reqwest::Client, port: u16, path: &str, id: &str) -> Result<bool> {
    let response = client
        .get(format!("http://127.0.0.1:{port}{path}"))
        .send()
        .await
        .context("request protected node listing from real API process")?;
    let status = response.status();
    if status != reqwest::StatusCode::OK {
        bail!("real API listing returned {status} instead of 200 for {path}");
    }
    let body: Value = response
        .json()
        .await
        .context("decode protected node listing")?;
    Ok(contains_exact_node_id(&body, id))
}

#[tokio::test]
#[ignore = "requires disposable direct PostgreSQL and two loopback API child processes"]
async fn committed_visibility_is_coherent_across_two_real_http_api_processes() -> Result<()> {
    let db_url = std::env::var("DATABASE_URL")
        .context("DATABASE_URL required for isolated two-process HTTP proof")?;
    support::postgres_proof::assert_direct_disposable_database_url(&db_url);

    let pool = PgPool::connect(&db_url).await?;
    let migrations_dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("migrations");
    sqlx::migrate::Migrator::new(migrations_dir)
        .await?
        .run(&pool)
        .await
        .context("migrate only the guarded disposable PostgreSQL database")?;

    let id = format!("rp-two-process-{}", uuid::Uuid::new_v4());
    sqlx::query(
        "INSERT INTO domain_nodes (id, kind, title, lat, lon, payload, search_visibility)
         VALUES ($1, 'place', 'Two process visibility fixture', 53.5, 9.9, '{}'::jsonb, 'public')",
    )
    .bind(&id)
    .execute(&pool)
    .await?;

    let temp = tempfile::tempdir()?;
    let first = free_loopback_port()?;
    let second = free_loopback_port()?;
    anyhow::ensure!(first != second, "API child ports must be distinct");
    let mut children = ApiChildren(Vec::new());
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(7))
        .build()?;

    start_api(&mut children, &temp.path().join("first"), &db_url, first)?;
    wait_for_listener(&mut children, first, &client).await?;
    start_api(&mut children, &temp.path().join("second"), &db_url, second)?;
    wait_for_listener(&mut children, second, &client).await?;
    anyhow::ensure!(
        children.0[0].id() != children.0[1].id(),
        "separate operating-system API process IDs required"
    );

    let paths = [
        "/nodes?limit=100",
        "/api/nodes?limit=100",
        "/nodes?pagination=cursor&limit=100",
        "/nodes?bbox=9.8,53.4,10.1,53.7&limit=100",
    ];

    // Both processes independently cached the public fixture at startup.
    for port in [first, second] {
        for path in paths {
            anyhow::ensure!(
                listed(&client, port, path, &id).await?,
                "public fixture not visible to API process {port} at {path}"
            );
        }
    }

    // A third database connection commits an actual public -> private update.
    // No process-local Tokio lock or cache is shared between either HTTP API.
    let update = sqlx::query("UPDATE domain_nodes SET search_visibility = 'private' WHERE id = $1")
        .bind(&id)
        .execute(&pool)
        .await?;
    anyhow::ensure!(
        update.rows_affected() == 1,
        "privacy fixture update must commit"
    );

    for port in [first, second] {
        for path in paths {
            anyhow::ensure!(
                !listed(&client, port, path, &id).await?,
                "PRIVATE NODE EXPOSED after commit by independent API process {port} at {path}"
            );
        }
    }

    // A later independent commit must also invalidate the stale private cache.
    let update = sqlx::query("UPDATE domain_nodes SET search_visibility = 'public' WHERE id = $1")
        .bind(&id)
        .execute(&pool)
        .await?;
    anyhow::ensure!(update.rows_affected() == 1, "public regrant must commit");
    for port in [first, second] {
        for path in paths {
            anyhow::ensure!(
                listed(&client, port, path, &id).await?,
                "API process {port} did not reload public grant at {path}"
            );
        }
    }

    drop(children);
    let deleted = sqlx::query("DELETE FROM domain_nodes WHERE id = $1")
        .bind(&id)
        .execute(&pool)
        .await?;
    if deleted.rows_affected() != 1 {
        return Err(anyhow!(
            "test fixture cleanup did not delete exactly one row"
        ));
    }
    pool.close().await;
    Ok(())
}
