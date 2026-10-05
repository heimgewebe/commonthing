//! Process shutdown coordination.
//!
//! The API runs as PID 1 in its container. Without a handler the kernel ignores
//! SIGTERM for PID 1, so `docker stop` and Kubernetes always fell through to
//! SIGKILL. This module turns SIGTERM/SIGINT into one shutdown signal that the
//! HTTP server and the background loops share, and bounds every drain step so a
//! hanging request or worker cannot hold the process past its grace period.
//!
//! Work that is still running when the budget ends is not lost: the outbox,
//! Web Push and federation delivery workers claim rows with a lease, so an
//! aborted claim becomes due again once its lease expires.

use std::{env, future::Future, sync::Arc, time::Duration};

use anyhow::{anyhow, Context};
use tokio::sync::watch;

pub const SHUTDOWN_GRACE_ENV: &str = "WELTGEWEBE_API_SHUTDOWN_GRACE_SECONDS";

/// Budget for draining in-flight HTTP requests after the signal. Together with
/// [`POOL_CLOSE_TIMEOUT`], [`AUDIT_APPEND_DRAIN_TIMEOUT`] and
/// [`RUNTIME_SHUTDOWN_TIMEOUT`] it stays below Docker's default 10 s stop
/// timeout.
pub const DEFAULT_SHUTDOWN_GRACE: Duration = Duration::from_secs(6);
const MAX_SHUTDOWN_GRACE_SECONDS: u64 = 300;

/// Upper bound for returning pooled PostgreSQL connections after the drain.
pub const POOL_CLOSE_TIMEOUT: Duration = Duration::from_secs(1);

/// Upper bound for waiting on a node-mutation audit append that is still
/// being written when `run` returns (see `node_mutation::wait_for_audit_appends`).
pub const AUDIT_APPEND_DRAIN_TIMEOUT: Duration = Duration::from_secs(1);

/// Upper bound for tearing down the Tokio runtime once `run` has returned. It
/// covers tasks that never yield and blocking work that would otherwise make
/// runtime drop wait indefinitely.
pub const RUNTIME_SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(1);

/// Owner side of the shutdown signal. Cloning shares the same signal.
#[derive(Clone, Debug)]
pub struct Shutdown {
    sender: Arc<watch::Sender<bool>>,
}

impl Default for Shutdown {
    fn default() -> Self {
        Self::new()
    }
}

impl Shutdown {
    pub fn new() -> Self {
        let (sender, _receiver) = watch::channel(false);
        Self {
            sender: Arc::new(sender),
        }
    }

    pub fn trigger(&self) {
        self.sender.send_replace(true);
    }

    pub fn is_triggered(&self) -> bool {
        *self.sender.borrow()
    }

    pub fn signal(&self) -> ShutdownSignal {
        ShutdownSignal {
            receiver: self.sender.subscribe(),
        }
    }
}

/// Observer side of the shutdown signal, handed to the server and workers.
#[derive(Clone, Debug)]
pub struct ShutdownSignal {
    receiver: watch::Receiver<bool>,
}

impl ShutdownSignal {
    /// Resolves once shutdown was triggered, or immediately if it already was.
    /// A dropped [`Shutdown`] also counts as shutdown, so no waiter can hang on
    /// an owner that no longer exists.
    pub async fn wait(mut self) {
        let _ = self.receiver.wait_for(|triggered| *triggered).await;
    }
}

/// Installs the SIGTERM/SIGINT handlers now and returns a future that resolves
/// on the first of them. Installing eagerly matters for PID 1: a signal that
/// arrives before a handler exists is discarded by the kernel, so the handlers
/// must be in place before the first startup step that can block.
pub fn install_termination_handler(
) -> anyhow::Result<impl Future<Output = anyhow::Result<&'static str>> + Send + 'static> {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{signal, SignalKind};
        let mut terminate =
            signal(SignalKind::terminate()).context("failed to install SIGTERM handler")?;
        let mut interrupt =
            signal(SignalKind::interrupt()).context("failed to install SIGINT handler")?;
        Ok(async move {
            tokio::select! {
                _ = terminate.recv() => Ok("SIGTERM"),
                _ = interrupt.recv() => Ok("SIGINT"),
            }
        })
    }
    #[cfg(not(unix))]
    {
        Ok(async {
            tokio::signal::ctrl_c()
                .await
                .context("failed to install Ctrl-C handler")?;
            Ok("Ctrl-C")
        })
    }
}

/// Runs a startup phase unless shutdown is requested first. Returns `None`
/// when the signal won; the phase future is then dropped at its current await
/// point. Startup steps that write are transactional (migrations) or
/// idempotent on the next start, so dropping them is safe to resume.
pub async fn abort_on_shutdown<F: Future>(phase: F, signal: ShutdownSignal) -> Option<F::Output> {
    tokio::select! {
        biased;
        () = signal.wait() => None,
        output = phase => Some(output),
    }
}

/// Reads the HTTP drain budget from [`SHUTDOWN_GRACE_ENV`].
pub fn grace_period_from_env() -> anyhow::Result<Duration> {
    match env::var(SHUTDOWN_GRACE_ENV) {
        Ok(raw) => parse_grace_period(&raw),
        Err(env::VarError::NotPresent) => Ok(DEFAULT_SHUTDOWN_GRACE),
        Err(error) => Err(anyhow!(error))
            .with_context(|| format!("failed to read {SHUTDOWN_GRACE_ENV} from the environment")),
    }
}

fn parse_grace_period(raw: &str) -> anyhow::Result<Duration> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return Ok(DEFAULT_SHUTDOWN_GRACE);
    }
    let seconds: u64 = trimmed.parse().map_err(|_| {
        anyhow!("invalid {SHUTDOWN_GRACE_ENV} value {raw:?}; expected whole seconds")
    })?;
    if seconds == 0 || seconds > MAX_SHUTDOWN_GRACE_SECONDS {
        return Err(anyhow!(
            "invalid {SHUTDOWN_GRACE_ENV} value {raw:?}; expected 1..={MAX_SHUTDOWN_GRACE_SECONDS}"
        ));
    }
    Ok(Duration::from_secs(seconds))
}

/// How the HTTP server stopped.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DrainOutcome {
    /// Every in-flight request finished inside the grace period.
    Drained,
    /// The grace period ran out with requests still open; they are dropped.
    GraceExpired,
}

/// Drives `server` (already wired to stop accepting on `signal`) and gives it
/// `grace` to finish in-flight requests once the signal fires.
pub async fn drain_within<S, E>(
    server: S,
    signal: ShutdownSignal,
    grace: Duration,
) -> Result<DrainOutcome, E>
where
    S: Future<Output = Result<(), E>>,
{
    let deadline = async move {
        signal.wait().await;
        tokio::time::sleep(grace).await;
    };
    tokio::pin!(server);
    tokio::pin!(deadline);
    tokio::select! {
        result = &mut server => result.map(|()| DrainOutcome::Drained),
        () = &mut deadline => Ok(DrainOutcome::GraceExpired),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{routing::get, Router};
    use std::{future::IntoFuture, net::SocketAddr};
    use tokio::{
        io::{AsyncReadExt, AsyncWriteExt},
        net::{TcpListener, TcpStream},
    };

    #[test]
    fn grace_period_defaults_and_bounds() {
        assert_eq!(parse_grace_period("").unwrap(), DEFAULT_SHUTDOWN_GRACE);
        assert_eq!(parse_grace_period(" 25 ").unwrap(), Duration::from_secs(25));
        assert!(parse_grace_period("0").is_err());
        assert!(parse_grace_period("301").is_err());
        assert!(parse_grace_period("1.5").is_err());
        assert!(parse_grace_period("-3").is_err());
    }

    #[test]
    fn default_budget_fits_docker_stop_timeout() {
        let total = DEFAULT_SHUTDOWN_GRACE
            + POOL_CLOSE_TIMEOUT
            + AUDIT_APPEND_DRAIN_TIMEOUT
            + RUNTIME_SHUTDOWN_TIMEOUT;
        assert!(total < Duration::from_secs(10), "{total:?}");
    }

    #[tokio::test]
    async fn signal_waits_until_triggered_and_then_resolves_immediately() {
        let shutdown = Shutdown::new();
        let waiter = tokio::spawn(shutdown.signal().wait());
        tokio::task::yield_now().await;
        assert!(!waiter.is_finished());

        shutdown.trigger();
        tokio::time::timeout(Duration::from_secs(1), waiter)
            .await
            .expect("waiter must resolve after trigger")
            .unwrap();
        assert!(shutdown.is_triggered());

        // A signal taken after the trigger must not wait.
        tokio::time::timeout(Duration::from_millis(50), shutdown.signal().wait())
            .await
            .expect("late subscriber must see the trigger");
    }

    #[tokio::test]
    async fn dropped_owner_releases_waiters() {
        let shutdown = Shutdown::new();
        let signal = shutdown.signal();
        drop(shutdown);
        tokio::time::timeout(Duration::from_millis(50), signal.wait())
            .await
            .expect("dropping the owner must release waiters");
    }

    #[tokio::test]
    async fn startup_phase_is_abandoned_once_shutdown_is_triggered() {
        let shutdown = Shutdown::new();
        let phase = tokio::spawn(abort_on_shutdown(
            std::future::pending::<()>(),
            shutdown.signal(),
        ));
        tokio::time::sleep(Duration::from_millis(50)).await;
        assert!(!phase.is_finished());

        shutdown.trigger();
        let outcome = tokio::time::timeout(Duration::from_secs(1), phase)
            .await
            .expect("a hanging startup phase must not outlive the signal")
            .unwrap();
        assert_eq!(outcome, None);

        // Without a signal the phase result passes through.
        let shutdown = Shutdown::new();
        assert_eq!(
            abort_on_shutdown(async { 7 }, shutdown.signal()).await,
            Some(7)
        );
    }

    async fn spawn_server(
        shutdown: &Shutdown,
        grace: Duration,
    ) -> (SocketAddr, tokio::task::JoinHandle<DrainOutcome>) {
        let app = Router::new()
            .route(
                "/slow",
                get(|| async {
                    tokio::time::sleep(Duration::from_millis(300)).await;
                    "done"
                }),
            )
            .route("/hang", get(std::future::pending::<&'static str>));
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let signal = shutdown.signal();
        let server = axum::serve(listener, app)
            .with_graceful_shutdown(signal.clone().wait())
            .into_future();
        let handle =
            tokio::spawn(async move { drain_within(server, signal, grace).await.unwrap() });
        (addr, handle)
    }

    async fn send_request(addr: SocketAddr, path: &str) -> TcpStream {
        let mut stream = TcpStream::connect(addr).await.unwrap();
        let request = format!("GET {path} HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n");
        stream.write_all(request.as_bytes()).await.unwrap();
        stream
    }

    #[tokio::test]
    async fn in_flight_request_completes_before_server_returns() {
        let shutdown = Shutdown::new();
        let (addr, server) = spawn_server(&shutdown, Duration::from_secs(5)).await;

        let mut stream = send_request(addr, "/slow").await;
        tokio::time::sleep(Duration::from_millis(50)).await;
        shutdown.trigger();

        let mut response = String::new();
        stream.read_to_string(&mut response).await.unwrap();
        assert!(response.starts_with("HTTP/1.1 200"), "{response}");
        assert!(response.ends_with("done"), "{response}");

        let outcome = tokio::time::timeout(Duration::from_secs(2), server)
            .await
            .expect("server must return after the drain")
            .unwrap();
        assert_eq!(outcome, DrainOutcome::Drained);

        // The listener is closed: new connections are refused.
        assert!(TcpStream::connect(addr).await.is_err());
    }

    #[tokio::test]
    async fn hanging_request_cannot_block_shutdown_past_grace() {
        let shutdown = Shutdown::new();
        let grace = Duration::from_millis(200);
        let (addr, server) = spawn_server(&shutdown, grace).await;

        let _stream = send_request(addr, "/hang").await;
        tokio::time::sleep(Duration::from_millis(50)).await;
        let started = tokio::time::Instant::now();
        shutdown.trigger();

        let outcome = tokio::time::timeout(Duration::from_secs(2), server)
            .await
            .expect("grace period must bound the drain")
            .unwrap();
        assert_eq!(outcome, DrainOutcome::GraceExpired);
        assert!(started.elapsed() >= grace);
    }
}
