# commonThing API

The commonThing API is a Rust-based Axum service that powers the platform's backend capabilities.
This README provides a quick orientation for running and developing the service locally.

## Quickstart

1. **Install dependencies**
   - [Rust toolchain](https://www.rust-lang.org/tools/install) (stable)
   - A running PostgreSQL instance (or use `make up` / `just up` for the dev stack)
   - Optional: a running NATS server when developing features that need messaging

2. **Copy the environment template**

   ```bash
   cp ../../.env.example .env
   ```

3. **Adjust the required environment variables** (either in `.env` or the shell).
   Values defined in `.env` take precedence over the defaults from Docker Compose when you use the
   local development stack.
   Recommended settings:
   - `API_BIND` &mdash; socket address to bind the API (default `0.0.0.0:8080`)
   - `DATABASE_URL` &mdash; PostgreSQL connection string (e.g. `postgres://user:password@localhost:5432/weltgewebe`)
   - `NATS_URL` &mdash; URL of the NATS server (e.g. `nats://127.0.0.1:4222`) when messaging is enabled
   - `MAX_GUEST_OWNED_NODES` &mdash; positive per-account cap for nodes created by guests (default `1000`);
     `0` is rejected at startup, while idempotent retries of an already accepted `operation_id` remain allowed at the cap.
   - `AUTH_COOKIE_SECURE` &mdash; Set to `0` if you are developing locally over HTTP (non-HTTPS) to ensure cookies
     are accepted by the browser.

4. **Run the API**

   ```bash
   cargo run
   ```

   By default the service listens on <http://localhost:8080>.

## Security: CSRF & Proxy Configuration

The API implements strict CSRF protection for all state-changing endpoints (POST, PATCH, PUT, DELETE) that use session
authentication.

### Host Header Requirement

The middleware validates the `Origin` and `Referer` headers against the request's `Host` header. If you deploy behind
a reverse proxy (e.g., Caddy, Cloudflare, NGINX):

- **Preferred Standard:** Ensure the proxy **forwards the original Host header** (e.g., `Host: weltgewebe.org`) to
  the API. This maintains the "same-origin" integrity check naturally.
- **Exception/Fallback:** If the proxy *must* rewrite the Host header (e.g., to `api:8080` internally), the CSRF
  check will fail because the browser sends an `Origin` matching the external domain. Only in this case should you add
  the external origin to `CSRF_ALLOWED_ORIGINS`. Do not use the allowlist as a permanent workaround for misconfigured
  proxies if Host forwarding is possible.

The middleware determines HTTP vs HTTPS enforcement solely based on the `Origin` or `Referer` scheme provided by the
client (except for localhost). It does **not** currently use `X-Forwarded-Proto` to infer protocol security.

### Development Overrides

For local development where Origin/Host might mismatch (e.g. strict frontend/backend separation on different ports),
you can whitelist origins:

- `CSRF_ALLOWED_ORIGINS` &mdash; Comma-separated list of allowed origins
  (e.g., `http://localhost:5173,https://my-dev-env.com`).

**Note:** Initial authentication flows (such as `/auth/magic-link/request` or `/auth/dev/login`) typically do not
carry an authenticated session cookie and therefore bypass the session-bound CSRF checks. Additional route-specific
bypass behavior (e.g., for `/auth/logout`) is defined by the backend middleware and should be verified against
the implementation.

## Developer Login (Security Warning)

For development purposes, a specialized login route is available to facilitate role-based testing without
authentication providers.

- **Variable:** `AUTH_DEV_LOGIN`
- **Default:** `0` (disabled)
- **Usage:** Set to `1` to enable `GET /auth/dev/accounts` and bypass production authentication flows.
- **WARNING:** **NEVER enable this in production.** It exposes a list of all accounts and allows impersonation of
  any role (Admin/Weber) without credentials.

### Remote Access & Proxies

By default, dev-login is restricted to requests originating from **localhost**.

- **`AUTH_DEV_LOGIN_ALLOW_REMOTE`**: Set to `1` to allow requests from non-localhost IPs (e.g., LAN devices).
  **Use with extreme caution.**
- **`AUTH_PUBLIC_LOGIN`**: Set to `1` to enable public Magic Link login flow (`/auth/magic-link/request`).
  Defaults to `0` (disabled).
- **`APP_BASE_URL`**: Base URL for constructing Magic Links sent via email/log.
  Defaults to `http://localhost:5173`.
- **`AUTH_TRUSTED_PROXIES`**: Comma-separated list of trusted proxy IPs or CIDRs (e.g., `127.0.0.1,::1,10.0.0.0/8`).
  - If set, the API will trust `Forwarded` and `X-Forwarded-For` headers from these IPs to determine the
    effective client IP.
  - **Default:** If unset, defaults to `127.0.0.1,::1` (localhost) to support local development proxies out-of-the-box.
  - Required if running the API behind a local reverse proxy (e.g., Caddy) that forwards traffic from localhost.

## Observability

- `GET /health/live` is dependency-free liveness and remains suitable for restart decisions.
- `GET /health/ready` checks PostgreSQL, NATS, and policy availability concurrently. Each check is bounded to
  750 ms and the complete readiness evaluation to 1,000 ms. A timeout returns `503 Service Unavailable`; in
  verbose mode the affected check includes a bounded timeout diagnostic. External Docker and Kubernetes probe
  timeouts must remain above the internal 1,000 ms budget so the API can return the controlled response itself.
- `GET /metrics` renders Prometheus metrics including `http_requests_total{method,path}` and `build_info`.

## Database Migrations

Migrations live in `apps/api/migrations/` and are managed with the
[sqlx-cli](https://github.com/launchbadge/sqlx/tree/main/sqlx-cli).

Install once:

```bash
cargo install sqlx-cli --version 0.8.1 --locked --no-default-features --features native-tls,postgres
```

Run from the **repository root** with `DATABASE_URL` set:

```bash
export DATABASE_URL=postgres://welt:gewebe@localhost:5432/weltgewebe
sqlx migrate run --source apps/api/migrations     # apply pending migrations
sqlx migrate revert --source apps/api/migrations  # roll back last migration
```

Or use the Justfile shortcut (requires `DATABASE_URL` in the environment):

```bash
just db-migrate
```

> **Note:** This PR introduces the migration schema only. Auth-session persistence
> (`DbSessionStore`) and startup/CI migration automation are not yet activated.

## Development tasks

```bash
# Format the code
cargo fmt -- --check

# Lint
cargo clippy -- -D warnings

# Run tests
cargo test
```

All commands should be executed from the `apps/api` directory unless otherwise noted.
