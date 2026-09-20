# Use the same Rust version as defined in toolchain.versions.yml and compose.core.yml
FROM rust:1.89.0-bookworm@sha256:948f9b08a66e7fe01b03a98ef1c7568292e07ec2e4fe90d88c07bb14563c84ff AS builder

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends pkg-config libssl-dev ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 1) Copy manifests first to cache dependencies
COPY Cargo.toml Cargo.lock ./
COPY apps/api/Cargo.toml apps/api/Cargo.toml

# 2) Fetch dependencies (this caches the index and downloads)

# DUMMY_TARGET_FOR_CARGO_FETCH
# Cargo erkennt Targets über src/main.rs|src/lib.rs. In dieser Stage sind ggf. nur Manifeste kopiert.
RUN mkdir -p apps/api/src && printf '%s\n' 'fn main() {}' > apps/api/src/main.rs
RUN cargo fetch --locked

# 3) Copy the rest of the workspace
COPY . .

# 4) Build the API binary in release mode
# Build against the checked-in lockfile for deterministic dependency resolution.
ARG CARGO_BUILD_JOBS=2
ARG GIT_COMMIT_SHA
ARG BUILD_TIMESTAMP
# Default to a conservative job count for Heimserver stability.
# CI or larger builders may override with --build-arg CARGO_BUILD_JOBS=<n>.
# Release provenance is compiled into the binary. The timestamp is derived from
# the Git commit, not the builder clock, so rebuilding the same commit is stable.
RUN printf '%s' "${GIT_COMMIT_SHA}" | grep -Eq '^[0-9a-f]{40}$' \
    && printf '%s' "${BUILD_TIMESTAMP}" | grep -Eq '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(Z|[+-][0-9]{2}:[0-9]{2})$' \
    && GIT_COMMIT_SHA="${GIT_COMMIT_SHA}" \
       BUILD_TIMESTAMP="${BUILD_TIMESTAMP}" \
       CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS}" \
       cargo build --release --locked --bin weltgewebe-api --bin search-backfill

# Runner stage
FROM debian:bookworm-slim@sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818

WORKDIR /app

# Install runtime dependencies
# wget is needed for healthchecks. ca-certificates for HTTPS.
# jq is kept for demo/dev tooling and smoke-style JSON inspection; bootstrap-first-account.sh does not require jq.
# libssl3 is required by OpenSSL-linked Rust dependencies such as webauthn-rs.
# libpcre2-8-0 is refreshed from bookworm-security so the pinned base does not
# retain the vulnerable package version reported by the image scanner.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates wget jq libssl3 libpcre2-8-0 passwd \
    && rm -rf /var/lib/apt/lists/*

# Copy the binary from builder
COPY --from=builder /app/target/release/weltgewebe-api /app/weltgewebe-api
COPY --from=builder /app/target/release/search-backfill /app/search-backfill

# Copy scripts for data seeding
# Note: Docker context is repo root
COPY scripts/dev/generate-demo-data.sh /usr/local/bin/generate-demo-data
COPY scripts/dev/bootstrap-first-account.sh /usr/local/bin/bootstrap-first-account
COPY apps/api/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY scripts/ops/search-worker-loop.sh /usr/local/bin/search-worker-loop
RUN chmod 0755 /usr/local/bin/generate-demo-data /usr/local/bin/bootstrap-first-account /usr/local/bin/entrypoint.sh /usr/local/bin/search-worker-loop
RUN groupadd --gid 10001 weltgewebe \
    && useradd --uid 10001 --gid 10001 --home-dir /app --shell /usr/sbin/nologin weltgewebe \
    && mkdir -p /data /tmp \
    && chown -R 10001:10001 /app /data /tmp

# Expose the port
COPY policies /app/policies
RUN find /app/policies -type d -exec chmod 0755 {} + \
    && find /app/policies -type f -exec chmod 0644 {} +
EXPOSE 8080
USER 10001:10001

# Use entrypoint to handle data initialization
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# Run the binary (passed to entrypoint)
CMD ["./weltgewebe-api"]
