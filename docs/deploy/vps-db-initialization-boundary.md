---
id: deploy.vps-db-initialization-boundary
title: VPS DB Initialization Boundary
doc_type: runbook
status: active
summary: Decision and evidence boundary for initializing or repairing commonserver PostgreSQL outside the no-migration smoke scope.
relations:
  - type: relates_to
    target: docs/deploy/vps-migration-safe-runtime-smoke.md
  - type: relates_to
    target: scripts/ops/check_vps_db_migration_history_shape.py
---
# VPS DB Initialization Boundary

This runbook defines the decision boundary for `commonserver` database
initialization or repair after the DB-history preflight blocks #1348.

GitHub tracking context: #1348 is the DNS-free VPS HTTP smoke issue and #1359 is
the DB initialization boundary decision issue. These issue references are kept in
body text rather than `relations`, because docmeta relations require
repo-relative targets that exist in the checkout.

It is not a deploy instruction and not an authorization to mutate production
state. It exists to prevent an empty database from being initialized implicitly
by a normal API or smoke retry.

## Current trigger

Use this boundary only after the no-migration DB-history preflight reports a
blocking shape such as:

```text
status=blocked
history_table=absent
user_relation_count=0
base_table_count=0
reason=app_schema_empty_or_migration_history_absent
```

That result means the selected database does not currently prove that SQLx
migration history exists. A normal API startup or compose retry must not be used
to get past it.

## Hard stop for #1348

When the preflight blocks because the app schema or migration-history table is
absent, #1348 remains blocked. Do not treat a database initialization as part of
the DNS-free HTTP smoke.

A DB initialization or repair window is a separate operation class. It may apply
migration SQL and therefore sits outside the no-migration boundary of #1348.

## Allowed next decisions

Exactly one of these decisions must be recorded before continuing:

1. **Approve a bounded DB initialization or repair window.**
   This may initialize schema state or repair migration-history state, but only
   inside an explicit receipt-bound window.
2. **Select a genuinely DB-free route-only smoke.**
   This may collect routing evidence only. It must not claim API readiness,
   database readiness, or completion of #1348 as full runtime evidence.

## Forbidden shortcuts

Do not:

- start the production API to force database creation
- run `docker compose up` to see whether startup repairs the DB
- run the normal API startup migration path as a workaround
- change DNS, INWX, ACME, HTTPS, mail, SMTP, credential-source, or cutover state
- print `.env`, database URLs, passwords, tokens, or secret-bearing command lines
- close #1348 using route-only or DB-initialization evidence alone

## Pre-window evidence

Before any approved DB initialization or repair window, record:

- timestamp
- host identity, expected `commonserver`
- selected repo commit
- current `/opt/weltgewebe` worktree state, without secret output
- DB-history preflight JSON payload
- whether the operation is initialization or repair
- exact command class to be used, without secret values
- rollback or restore stance, if available

## Window constraints

During the approved window:

- run only the selected DB operation class
- do not start Caddy as part of the DB operation
- do not perform DNS, HTTPS, mail, SMTP, credential-source, or cutover work
- do not print secret-bearing environment files
- keep stdout/stderr suitable for public issue receipts

If the selected DB operation applies migration SQL, state that explicitly in the
receipt. Do not describe it as a no-migration smoke action.

## Post-window evidence

After the window, record:

- post-state DB-history preflight JSON payload
- whether migration SQL was applied
- whether the database now has non-history app tables
- whether failed or duplicate migration-history rows exist
- explicit non-actions for DNS, HTTPS, mail, SMTP, credentials, and cutover
- follow-up status for #1348:
  - still blocked
  - ready for a separately reviewed runtime retry
  - completed only if later loopback/runtime checks pass

## Completion boundary

This runbook is complete when the repository documents the decision boundary and
CI guards ensure that #1348 documentation does not bypass it.

It does not complete #1348 and it does not approve DB mutation by itself.
