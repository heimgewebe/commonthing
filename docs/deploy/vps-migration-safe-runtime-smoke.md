---
id: deploy.vps-migration-safe-runtime-smoke
title: VPS Migration-Safe Runtime Smoke
doc_type: runbook
status: draft
summary: Bounded commonserver runtime smoke path that may start the real API only after proving verify-applied startup migration mode.
relations:
  - type: relates_to
    target: docs/deploy/vps-http-smoke.md
  - type: relates_to
    target: docs/deploy/vps-http-route-smoke.md
  - type: relates_to
    target: docs/deploy/vps-http-route-smoke-risks.md
  - type: relates_to
    target: docs/deploy/vps-db-initialization-boundary.md
  - type: relates_to
    target: infra/compose/compose.vps.override.yml
  - type: relates_to
    target: scripts/ops/check_vps_migration_safe_runtime_env.py
---

# VPS Migration-Safe Runtime Smoke

This runbook defines the narrow runtime evidence path after the
`WELTGEWEBE_API_STARTUP_MIGRATIONS=verify-applied` startup mode exists.

It is not route-only evidence. A real API process may start only after the
migration-mode boundary is proven. A successful run still does not prove DNS,
INWX, ACME, HTTPS, mail, SMTP, frontend, basemap, or cutover readiness.

## Decision boundary

Use `docs/deploy/vps-http-route-smoke.md` when the operation proves only Caddy
route shape and must not start the production API process.

Use this runbook only when all of the following are true:

- a fresh operator decision permits a bounded runtime smoke on `commonserver`
- the selected repo commit is explicit
- starting the real API is necessary for the evidence
- database migrations are outside scope
- the selected runtime env source resolves through `services.api.env_file`
- the effective env source sets
  `WELTGEWEBE_API_STARTUP_MIGRATIONS=verify-applied`
- the VPS compose override does not set that variable in
  `services.api.environment`

If any of these points is false, stop. The correct result is blocked, not an
improvised smoke.

## What this can prove

A passing migration-safe runtime smoke can prove only:

- the selected VPS checkout can start the API without applying SQLx migration SQL
- `_sqlx_migrations` contains the embedded up-migration versions as successful
  records with matching checksums
- duplicate, failed, extra, missing, pending, or checksum-mismatched migration
  history blocks startup
- selected loopback Host-header health checks pass during the approved window

It does not prove full schema semantics, app behavior, frontend readiness,
public DNS, TLS, mail, SMTP, or production cutover.

## Hard non-actions

Do not:

- publish confidential runtime values
- paste raw rendered compose output into GitHub if it contains confidential values
- change DNS, INWX, ACME, HTTPS, mail, SMTP, credentials, or provider state
- run database migrations
- use the normal API startup mode under a no-migration boundary
- close #1348 on config-only, route-only, or synthetic-upstream evidence

The recurring mistake is to treat route shape as app readiness. This runbook keeps
those evidence classes separate.

## VPS runtime defaults

The VPS compose override is intentionally smoke-safe by default after the initial
setup deploy for public login: `AUTH_PUBLIC_LOGIN` defaults to `0`, and related
auth delivery/provisioning values remain explicit runtime-env choices.

After the Public-VPS credential-source cutover, the preferred selected runtime
env source is:

```text
/etc/weltgewebe/weltgewebe.env
```

`scripts/weltgewebe-up` uses that file for `DEPLOY_TARGET=vps` when no explicit
`ENV_FILE` is set and the file exists. An explicit `ENV_FILE=/path/to/runtime.env`
still takes precedence. The legacy `/opt/weltgewebe/.env` path may exist as a
rollback source, but it is not the preferred Public-VPS target path.

The startup migration mode is different: it must remain in the selected runtime
env source, not in `services.api.environment`. The source-level helper rejects a
service-level `WELTGEWEBE_API_STARTUP_MIGRATIONS` key because it can override the
selected `env_file` and hide the effective startup mode.

Public login may be enabled later, but only with an explicit token-delivery
mechanism such as SMTP or another reviewed delivery path. Do not use the VPS
smoke default as a claim that production auth readiness is complete.

For a one-time empty-database setup, an operator may deliberately run an approved
DB initialization path. After that window, return the selected runtime env source
to `WELTGEWEBE_API_STARTUP_MIGRATIONS=verify-applied` so normal restarts verify
migration history instead of applying migrations.

## Source-level preflight

Before any runtime start, run the narrow env/compose source-boundary probe from
the selected checkout:

```bash
python3 scripts/ops/check_vps_migration_safe_runtime_env.py \
  --compose-source infra/compose/compose.vps.override.yml \
  --env-file <selected-runtime-env-file> \
  --expected-mode verify-applied
```

If the compose file relies on `WELTGEWEBE_ENV_FILE` interpolation, either export
that variable in the operator shell or pass it explicitly without exposing
confidential values:

```bash
python3 scripts/ops/check_vps_migration_safe_runtime_env.py \
  --compose-source infra/compose/compose.vps.override.yml \
  --env-file <selected-runtime-env-file> \
  --compose-env WELTGEWEBE_ENV_FILE=<selected-runtime-env-file> \
  --expected-mode verify-applied
```

The helper checks only the allowed migration-mode key in the effective env source
and the non-confidential compose source. It does not invoke Docker, start
containers, render the full compose model, or print arbitrary runtime config.

The helper intentionally supports only a narrow, source-level Compose subset that
can be proven without rendering the full model:

- the checked API service must not use `extends`
- the checked API service must not use YAML merge, anchors, or aliases to define
  or import `environment` or `env_file`
- `env_file` interpolation is limited to `${VAR}`, `${VAR:-default}`, and
  `${VAR-default}`
- required, alternative, nested, or other Compose interpolation forms must be
  replaced by explicit simple paths or by explicit `--compose-env NAME=VALUE`
  inputs before the preflight

These are guard boundaries, not general Compose style rules. If a legitimate
runtime command needs broader Compose features, stop and add a separately reviewed
redacted effective-render check rather than weakening this source probe.

Expected redacted shape:

```text
PASS: migration-safe runtime-smoke source boundary is proven
- checked key: WELTGEWEBE_API_STARTUP_MIGRATIONS
- observed mode: verify-applied
- service-level migration override: absent
- helper printed confidential values: no
- helper started runtime: no
```

If the effective env source is missing the key, sets it more than once, sets a
value other than `verify-applied`, or the API service sets the key in
`environment`, stop.

## DB migration-history preflight

Before any further bounded runtime attempt on `commonserver`, run the DB history
shape preflight against the already-running PostgreSQL container:

```bash
python3 scripts/ops/check_vps_db_migration_history_shape.py --json
```

This helper is intentionally narrower than a runtime retry:

- it reads PostgreSQL catalog shape through the selected DB container
- it does not start the API
- it does not start or restart Compose services
- it does not apply migrations
- it does not read runtime env files
- it does not print confidential values

A `blocked` result is a hard stop for #1348 runtime work. In particular, stop
when `public._sqlx_migrations` is absent, when the app schema appears empty,
when migration history is empty, when failed history rows exist, or when duplicate
migration versions exist. Do not retry the API in normal `run` mode to get past
that condition, because normal startup may apply SQLx migrations.

If this preflight blocks, the next path must be one of two explicitly reviewed
operations:

1. an approved DB initialization or repair window, outside this no-migration smoke
   boundary; or
2. a genuinely DB-free route smoke that does not claim API/database readiness.

See `docs/deploy/vps-db-initialization-boundary.md` before selecting DB
initialization or repair. DB initialization or repair must not be treated as part
of the no-migration #1348 smoke.

The runtime receipt must include the preflight status and must not report #1348
as passed unless later loopback/runtime checks actually succeed under
`WELTGEWEBE_API_STARTUP_MIGRATIONS=verify-applied`.

## Compose evidence discipline

The source-level helper is not a replacement for a separately reviewed runtime
receipt. It verifies that the selected env source is bound to the compose
`env_file` declaration and that no service-level migration-mode override is
present in the checked compose source.

The helper checks only the compose source named by `--compose-source`. If the
runtime command uses multiple compose sources, pair this source check with a
separate redacted effective-render review before starting the runtime smoke.

The final receipt may mention that compose was rendered successfully, but it
must not paste raw rendered config or runtime env source contents. Instead record
sanitized facts:

- selected compose files
- selected Caddyfile path
- selected env source path
- whether the source-level boundary probe passed
- whether the API service has no service-level
  `WELTGEWEBE_API_STARTUP_MIGRATIONS` override
- whether the effective env source sets exactly
  `WELTGEWEBE_API_STARTUP_MIGRATIONS=verify-applied`

This deliberately orders evidence by safety. A sanitized key proof is weaker than
full config publication, but full config publication may disclose confidential
runtime values. The weaker proof is the correct one here.

## Runtime receipt requirements

If a separately approved runtime smoke is performed later, the receipt must state:

- timestamp
- host identity
- selected repo commit
- selected compose/Caddyfile/env source
- result of `check_vps_migration_safe_runtime_env.py`
- whether any container/API process was started
- API startup result under `verify-applied`
- loopback Host-header check results
- explicit non-actions for DNS, INWX, ACME, HTTPS, confidential runtime values,
  credentials, database migrations, mail, SMTP, and cutover
- explicit statement that this is migration-safe runtime evidence, not
  route-only evidence and not full production readiness

## Stop table

| Observation | Meaning | Decision |
| --- | --- | --- |
| Env key missing or duplicated in the effective source. | Effective migration mode is ambiguous. | Stop. |
| Env key is not `verify-applied`. | Normal startup may run SQLx migrations. | Stop. |
| Env key uses shell-style `export`. | Compose `env_file` syntax is intentionally restricted here. | Stop. |
| Env source contains an unparsable line with the migration-mode key. | The effective migration mode is ambiguous. | Stop. |
| Compose service environment sets the key. | It may override the selected env source. | Stop. |
| Compose service uses `extends` or guard-relevant YAML merge/anchor/alias constructs. | The source probe cannot prove the effective service model without a render. | Stop. |
| `env_file` uses unsupported interpolation. | The source probe cannot prove which env source Compose will select. | Stop. |
| `--env-file` is not the effective env source for the key. | The helper would be checking the wrong file. | Stop. |
| API refuses startup on pending, failed, extra, missing, duplicate, or checksum-mismatched migration history. | `verify-applied` did its job. | Record blocked runtime evidence; do not retry with `run`. |
| Health checks pass under `verify-applied`. | Bounded runtime smoke passed. | Keep #1348 open until the receipt is reviewed against its full acceptance criteria. |
| DNS/ACME/HTTPS/mail/SMTP/confidential-value work becomes necessary. | The operation left this scope. | Stop and create a new approved operation. |
