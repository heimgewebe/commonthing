use weltgewebe_api::{run, shutdown::RUNTIME_SHUTDOWN_TIMEOUT};

// glibc malloc keeps the freed projection after each reload resident
// (fragmentation across arenas); mimalloc returns it to the OS.
#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;

fn main() -> anyhow::Result<()> {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?;
    let result = runtime.block_on(run());
    // Dropping the runtime would wait without limit for tasks that never yield
    // and for blocking work; bound that so a hanging worker cannot hold PID 1.
    runtime.shutdown_timeout(RUNTIME_SHUTDOWN_TIMEOUT);
    result
}
