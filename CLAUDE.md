---
id: repo.claude
title: CLAUDE
doc_type: runbook
status: active
canonicality: supplementary
summary: Claude Code operational interface for commonThing — commands, navigation, and workflow shortcuts.
---

# CLAUDE.md — commonThing

> **For Claude Code (AI assistant) working in this repository.**
> This file is an operational interface. It does NOT define rules, policies, or architecture.

This operational document primarily references the following canonical sources:

- `repo.meta.yaml`
- `AGENTS.md`
- `agent-policy.yaml`
- `docs/policies/agent-reading-protocol.md`

If any conflict exists between this document and any canonical sources defined in `repo.meta.yaml`, those canonical sources always override this document.

---

## Role of This Document

This document provides:

- operational commands
- navigation guidance
- workflow shortcuts
- shortcuts to canonical activation rules

It does NOT provide:

- normative rules
- architectural decisions
- truth definitions
- automatic activation of cognitive modules

For coding conventions, see **`AGENTS.md`**. For architecture principles, see `repo.meta.yaml` and the canonical policies.

---

## Mandatory Reading Order

Before making any changes, read these files in order:

1. `repo.meta.yaml` — truth model and precedence rules
2. `AGENTS.md` — canonical coding guidelines and operational boundaries
3. `agent-policy.yaml` — write permissions and required checks
4. `docs/policies/agent-reading-protocol.md` — binding decision and abort protocol

`docs/index.md` is navigation only. `docs/_generated/*` is diagnostic only — never canonical.

Canonical cognitive modules are not part of the default reading order. Their structure is defined in `repo.meta.yaml` under `cognitive_modules`. They represent optional, high-cost reasoning layers and should only be loaded when explicitly required by the task. If a cognitive module is relevant, it must be loaded only through the activation logic defined in `docs/policies/agent-reading-protocol.md`. For commonThing, this applies in particular to `docs/policies/architecture-critique.md`.

### Roadmap / Status Tasks

When a task touches roadmap, status, report, blueprint, or auth-flow planning,
also read:

- `docs/roadmap.md`
- the relevant sub-roadmap
- the relevant status matrix or report

Per `AGENTS.md`, treat updates to `docs/roadmap.md` as downstream of
sub-roadmaps or status reports. In practice, only update it after the relevant
sub-roadmap or status report already documents and substantiates the status
change.

---

## What This Repository Is

**commonThing** is a mobile-first web application for managing nodes, edges, and domain relationships, with a focus on ethics, UX, and community autonomy.

| Layer | Technology |
|---|---|
| Frontend | SvelteKit 2 / Svelte 5 (TypeScript, static adapter) |
| Backend | Rust / Axum 0.8 (async, tokio) |
| Database | PostgreSQL 16 + PgBouncer 1.20 |
| Messaging | NATS JetStream (optional, Gate C) |
| Proxy | Caddy |
| Orchestration | Docker Compose (multi-profile) |

---

## Repository Layout

```text
commonthing/
├── apps/
│   ├── api/          # Rust/Axum HTTP API
│   └── web/          # SvelteKit frontend
├── contracts/domain/ # JSON Schema domain contracts (highest truth precedence)
├── docs/             # ADRs, architecture specs, runbooks (German)
│   ├── adr/          # Architecture Decision Records
│   ├── blueprints/   # UI, auth, map design specs
│   ├── specs/        # API contracts, privacy specs
│   ├── policies/     # Agent/operational policies
│   └── _generated/   # Auto-generated diagnostics (read-only, never edit)
├── infra/
│   ├── compose/      # Docker Compose profiles
│   └── caddy/        # Reverse proxy config
├── scripts/          # CI, dev, docmeta, and tooling scripts
├── ci/               # Budget assertions, smoke tests
├── .github/workflows/ # 20+ CI/CD workflows
├── Justfile          # Primary task runner
├── Cargo.toml        # Rust workspace root
└── package.json      # pnpm workspace root
```

---

## Development Commands

### Task Runner (`just`)

```bash
just up              # Start dev stack (Docker Compose, dev profile)
just down            # Stop dev stack + remove volumes
just ci              # Full CI pipeline: web + API + dependency check
just fmt             # Format all Rust code
just clippy          # Lint Rust (warnings = errors)
just test            # Run all Rust tests
just check           # Hygiene: fmt + clippy + test + contracts + cargo-deny
just lint            # Lint shell scripts (bash -n, shfmt, shellcheck)
just db-wait         # Wait for Postgres readiness
just db-migrate      # Run database migrations
just contracts-domain-check  # Validate JSON Schema contracts
just demo-data       # Generate demo data (if not present)
just serve-demo      # Start demo API server (PORT=8080 default)
```

### Frontend (`apps/web`)

```bash
cd apps/web
pnpm install --frozen-lockfile
pnpm dev             # Dev server (localhost:5173)
pnpm build           # Production build
pnpm check           # Svelte type checking
pnpm lint            # Prettier + ESLint (max-warnings=0)
pnpm test:unit       # Vitest unit tests
pnpm test:setup      # Install Playwright browsers (one-time)
pnpm test            # Playwright E2E tests
pnpm run ci          # Bundle budget + lint + check (used in CI)
```

### API (`apps/api`)

```bash
cd apps/api
cargo fmt -- --check         # Check formatting
cargo clippy -- -D warnings  # Lint (deny all warnings)
cargo build --locked         # Build (locked dependencies)
cargo test --locked          # Run all tests
cargo run                    # Start API server
```

### Docker Compose Profiles

```bash
# Dev stack (web + api + db + pgbouncer + caddy)
docker compose -f infra/compose/compose.core.yml --profile dev up -d --build

# Optional observability (Prometheus + Grafana)
docker compose -f infra/compose/compose.observ.yml up -d
```

---

## Environment Variables

Copy `.env.example` to `.env` for local development. Key variables:

```bash
NODE_ENV=development
RUST_LOG=info
API_BIND=0.0.0.0:8080
AUTH_DEV_LOGIN=0
AUTH_COOKIE_SECURE=1
DATABASE_URL=postgres://welt:gewebe@localhost:5432/weltgewebe
PGBOUNCER_URL=postgres://welt:gewebe@localhost:6432/weltgewebe
```

Production uses `.env.prod.example`. Never commit `.env` files.

---

## Toolchain Versions

Refer to canonical version files (e.g. `toolchain.versions.yml`, `.node-version`) for exact versions.

---

## Write Permissions

| Path | Status |
|---|---|
| `docs/_generated/` | **FORBIDDEN** — auto-generated, never edit manually |
| `secrets/`, `snapshots/` | **FORBIDDEN** |
| `.github/workflows/`, `apps/`, `contracts/`, `docs/`, `infra/`, `scripts/`, `src/` | **Guarded** — requires target proof + required checks |
| `README.md`, `AGENTS.md`, `docs/` | Safe to read |

**Required checks before patching guarded paths:**
`repo-structure-guard`, `docs-relations-guard`, `generated-files-guard`, `coverage-guard`, `lint`, `test`

**Human review required for:** `security/`, `deployment/`, `credentials/`

---

## Coding Conventions

All coding conventions are defined in **`AGENTS.md`**. This section only highlights common entry points.

- **Rust:** `cargo fmt` + `cargo clippy -- -D warnings`; pass `--locked` to all cargo commands.
- **TypeScript/Svelte:** `pnpm lint` (Prettier + ESLint, `max-warnings=0`); Svelte 5 runes preferred.
- **Shell scripts:** POSIX/bash; `shfmt -d -i 2 -ci -sr` + `shellcheck -S style`.
- **Node scripts:** Success messages only after no error thrown; strict type checks on numeric values.
- **Documentation:** See `AGENTS.md` for language, frontmatter, and linking requirements.

---

## Testing

### Web

- **Unit tests:** Vitest, files matching `src/**/*.test.ts`, run with `pnpm test:unit`.
- **E2E tests:** Playwright, files in `apps/web/tests/*.spec.ts`.
  - Local: `pnpm test` (builds first, uses port 4173).
  - CI: `pnpm test:ci` (port 5173, HTML + JUnit reporters, 2 retries).
  - Install browsers once: `pnpm test:setup`.

### API

- Cargo test: `cargo test --locked` in `apps/api/`.
- Test helpers in `apps/api/src/test_helpers.rs`.
- Use `serial_test` for tests that cannot run in parallel.

---

## CI Workflows

Key workflows in `.github/workflows/`:

| Workflow | Triggers | What it does |
|---|---|---|
| `ci.yml` | push main, PRs | Full build/test pipeline; conditionally skips for docs-only changes |
| `web.yml` | `apps/web/**` changes | Build, lint, typecheck, vitest, Playwright E2E |
| `api.yml` | `apps/api/**` changes | fmt, clippy, build, test |
| `docs-guard.yml` | `docs/**` changes | Frontmatter validation, relations, coverage |
| `contracts-domain.yml` | `contracts/**` | JSON Schema validation |
| `security.yml` | Dependency changes | cargo-deny |

CI enforces: `cargo fmt --check`, `cargo clippy -- -D warnings`, `pnpm lint` (max-warnings=0), Playwright retries=2.

---

## Domain Contracts

JSON Schema files in `contracts/domain/` define the authoritative domain model:

- `node.schema.json` — Node entity
- `edge.schema.json` — Edge relationship
- `role.schema.json` — User role
- `account.schema.json` — Account
- `message.schema.json` — Message
- `conversation.schema.json` — Conversation

Changes here require `just contracts-domain-check` to pass.

---

## Architecture Notes

Architecture principles and the truth model precedence are defined in the canonical sources:
`repo.meta.yaml`, `AGENTS.md`, and `docs/policies/agent-reading-protocol.md`.

This file only highlights operational implications:

- **Small PRs:** Prefer narrow, focused changes with clear scope boundaries.
- **Gate C:** NATS JetStream is optional (Infrastructure-light status).

---

## Key Reference Files

| File | Purpose |
|---|---|
| `repo.meta.yaml` | Machine truth model, precedence, discovery roots |
| `AGENTS.md` | Canonical coding guidelines and operational rules |
| `agent-policy.yaml` | Write permissions and required checks |
| `docs/policies/agent-reading-protocol.md` | Binding reading and abort protocol |
| `docs/index.md` | Navigation hub (not a truth source) |
| `docs/techstack.md` | Technology decisions and rationale |
| `docs/datenmodell.md` | Database schema and domain model |
| `docs/adr/` | Architecture Decision Records |
| `policies/performance.v1.json` | Canonical performance contract |
| `toolchain.versions.yml` | Pinned toolchain versions |
| `Justfile` | All development tasks |

## Secondary Index Synchronization Invariants

When refactoring linear scans into O(1) lookups via secondary HashMaps (e.g., `account_passkeys` and `credential_index`), always introduce explicit tests that enforce the synchronization invariant:

- **Reinsert after remove**: Verify that removing a primary key successfully unlinks the secondary index, allowing subsequent re-insertion without collision.
- **Partial removal**: Verify that removing one value from a one-to-many relationship (like an account owning multiple keys) only removes the targeted key from the secondary index, leaving others intact.
