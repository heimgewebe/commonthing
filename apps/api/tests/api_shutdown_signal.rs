//! Process-level proof that SIGTERM is honoured while startup still blocks.
//!
//! A silent TCP listener stands in for PostgreSQL: it accepts the connection
//! and never answers, so the API hangs in its initial pool acquire. The test
//! then sends SIGTERM and requires a controlled exit (an exit code, not death
//! by signal) well before the 30 s acquire timeout would have ended startup on
//! its own.
#![cfg(unix)]

use std::{
    net::TcpListener,
    process::{Command, ExitStatus, Stdio},
    time::{Duration, Instant},
};

fn sigterm_during_blocked_startup(extra_env: &[(&str, &str)]) -> ExitStatus {
    let silent_db = TcpListener::bind("127.0.0.1:0").expect("bind silent database stand-in");
    let port = silent_db.local_addr().unwrap().port();

    let mut command = Command::new(env!("CARGO_BIN_EXE_weltgewebe-api"));
    command
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env(
            "DATABASE_URL",
            format!("postgres://u:p@127.0.0.1:{port}/db"),
        )
        .env("API_BIND", "127.0.0.1:0")
        .env("RUST_LOG", "warn")
        .current_dir(env!("CARGO_MANIFEST_DIR"))
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    for (key, value) in extra_env {
        command.env(key, value);
    }
    let mut api = command.spawn().expect("spawn API binary");

    // Startup has reached the database step once the stand-in sees a client.
    let (_connection, _) = silent_db
        .accept()
        .expect("API must try to connect to the database");
    std::thread::sleep(Duration::from_millis(200));
    assert!(
        api.try_wait().unwrap().is_none(),
        "startup must still be blocked on the silent database"
    );

    let status = Command::new("kill")
        .args(["-TERM", &api.id().to_string()])
        .status()
        .expect("run kill");
    assert!(status.success());

    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        if let Some(exit) = api.try_wait().unwrap() {
            return exit;
        }
        if Instant::now() > deadline {
            let _ = api.kill();
            panic!("API did not exit within 5 s of SIGTERM during startup");
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}

#[test]
fn sigterm_during_blocked_startup_exits_cleanly() {
    let exit = sigterm_during_blocked_startup(&[]);
    assert_eq!(
        exit.code(),
        Some(0),
        "SIGTERM must be handled, not end the process by default action: {exit:?}"
    );
}

#[test]
fn sigterm_during_migration_only_startup_reports_failure() {
    // A migration job interrupted before its migrations finished must not look
    // successful to its orchestrator.
    let exit = sigterm_during_blocked_startup(&[("WELTGEWEBE_API_MIGRATION_ONLY", "1")]);
    assert_eq!(
        exit.code(),
        Some(1),
        "an interrupted migration-only run must fail with an exit code: {exit:?}"
    );
}
