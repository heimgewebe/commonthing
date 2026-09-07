---
id: docs.generated.relates-to-audit
title: Relates-To Audit
doc_type: generated
status: active
summary: Strukturelle Beobachtung der relates_to-Nutzung — Typen, Cluster, Beispiele.
---

## Weltgewebe Relates-To Audit

Generated automatically. Do not edit.

### Zusammenfassung

| Metrik | Wert |
| --- | --- |
| Relationen gesamt | 734 |
| — depends_on | 33 |
| — implements | 1 |
| — relates_to | 678 |
| — supersedes | 13 |
| — verifies | 9 |
| relates_to Anteil | 92% |

### Mögliche supersedes-Lücken

> Dokument-Paare mit namensähnlichen Mustern, die möglicherweise eine supersedes-Relation benötigen.

_Keine Lücken erkannt._

### Cluster-Analyse (relates_to)

> Zusammenhängende Gruppen im relates_to-Graphen.

**Cluster 1** (289 Dokumente):

- `.github/workflows/api.yml`
- `.github/workflows/basemap-runtime-proof.yml`
- `.github/workflows/domain-projection-load.yml`
- `.github/workflows/opt-arc-001-db-proof-matrix.yml`
- `.github/workflows/public-login-smtp-readiness.yml`
- `.github/workflows/reusable-web-check.yml`
- `AGENTS.md`
- `agent-policy.yaml`
- `apps/api/migrations/20260531000002_create_domain_edges.up.sql`
- `apps/api/migrations/20260716000001_multi_instance_foundation.up.sql`
- `apps/api/migrations/20260724000001_remove_ron_legacy.up.sql`
- `apps/api/src/auth/accounts.rs`
- `apps/api/src/auth/ephemeral_db.rs`
- `apps/api/src/domain_db.rs`
- `apps/api/src/governance.rs`
- `apps/api/src/middleware/domain_projection.rs`
- `apps/api/src/outbox.rs`
- `apps/api/src/routes/accounts.rs`
- `apps/api/src/routes/conversations.rs`
- `apps/api/src/routes/edges.rs`
- `apps/api/src/routes/nodes.rs`
- `apps/api/src/state.rs`
- `apps/api/tests/db_domain_account_write_path.rs`
- `apps/api/tests/db_domain_backfill.rs`
- `apps/api/tests/db_domain_node_write_path.rs`
- `apps/api/tests/db_multi_instance_foundation.rs`
- `apps/web/Caddyfile.container`
- `apps/web/src/lib/components/governance/ProposalDetail.svelte`
- `architecture/overview.md`
- `architecture/security.md`
- `architecture/weltgewebe-os.md`
- `audit/impl-registry.yaml`
- `contracts/agent/handoff.schema.json`
- `contracts/agent/run-result.schema.json`
- `contracts/agent/task.schema.json`
- `contracts/agent/validation.schema.json`
- `contracts/domain/conversation.schema.json`
- `contracts/domain/edge.schema.json`
- `contracts/domain/message.schema.json`
- `docs/_generated/report-lifecycle-inventory.md`
- `docs/adr/0043-edge-vs-conversation.md`
- `docs/adr/ADR-0001__clean-slate-docs-monorepo.md`
- `docs/adr/ADR-0002__reentry-kriterien.md`
- `docs/adr/ADR-0003__privacy-ungenauigkeitsradius-ron.md`
- `docs/adr/ADR-0004__fahrplan-verweis.md`
- `docs/adr/ADR-0005-auth.md`
- `docs/adr/ADR-0006__auth-magic-link-session-passkey.md`
- `docs/adr/ADR-0007__auth-persistence-production-db-path.md`
- `docs/adr/ADR-0008__domain-mail-provider-boundaries.md`
- `docs/adr/ADR-0009__garnrolle-verortung-sichtbarkeit.md`
- `docs/adr/ADR-0010__kubernetes-kanonische-plattform.md`
- `docs/adr/ADR-0011__foederierte-gewebezellen.md`
- `docs/adr/ADR-0012__ereignisrueckgrat-transactional-outbox.md`
- `docs/adr/ADR-0013__ha-referenzzelle-und-wiederherstellung.md`
- `docs/adr/ADR-0014__accountable-collective-node-mutations.md`
- `docs/adr/ADR-0015__ortsweberei-gewebezelle-webgemeindezentrum.md`
- `docs/architecture/weltgewebe-os-convergence-adapter.md`
- `docs/architekturstruktur.md`
- `docs/blueprints/agent-operability-blaupause.md`
- `docs/blueprints/auth-persistence-runtime-proof.md`
- `docs/blueprints/auth-roadmap.md`
- `docs/blueprints/blueprint-agent-safety-control-layer.md`
- `docs/blueprints/doc-structure-task-control-examples.md`
- `docs/blueprints/doc-structure-task-control-roadmap.md`
- `docs/blueprints/doc-structure-task-control.md`
- `docs/blueprints/domain-data-postgres-cutover.md`
- `docs/blueprints/domain-scale-foundation.md`
- `docs/blueprints/kartenklarheit-phase6.md`
- `docs/blueprints/kartenklarheit-roadmap.md`
- `docs/blueprints/kartenklarheit.md`
- `docs/blueprints/map-blaupause.md`
- `docs/blueprints/map-roadmap.md`
- `docs/blueprints/ui-blaupause.md`
- `docs/blueprints/ui-interaction-doctrine.md`
- `docs/blueprints/ui-roadmap.md`
- `docs/blueprints/ui-state-machine.md`
- `docs/blueprints/versionierungs-blaupause.md`
- `docs/blueprints/versionierungs-statusgrundlage.md`
- `docs/blueprints/weltgewebe-os-masterplan.md`
- `docs/blueprints/weltgewebe.auth-and-ui-routing.md`
- `docs/blueprints/weltgewebe.config.diff.md`
- `docs/blueprints/weltgewebe.deploy.plan.md`
- `docs/claims/README.md`
- `docs/datenmodell.md`
- `docs/deploy/CHANGELOG.md`
- `docs/deploy/DRIFT_POLICY.md`
- `docs/deploy/README.md`
- `docs/deploy/commonthing.naming.md`
- `docs/deploy/domain-mail-migration-ionos-to-inwx-mailbox-brevo.md`
- `docs/deploy/heim-first-phase0.md`
- `docs/deploy/heimserver.deployment.md`
- `docs/deploy/heimserver.integration.md`
- `docs/deploy/map-html-canonical-route.md`
- `docs/deploy/merge-to-live.md`
- `docs/deploy/public-app-base-url.md`
- `docs/deploy/public-metrics-boundary.md`
- `docs/deploy/schauwerk-editor-frontdoor.md`
- `docs/deploy/secondary-domain-web-surfaces.md`
- `docs/deploy/security.md`
- `docs/deploy/vps-db-initialization-boundary.md`
- `docs/deploy/vps-http-route-smoke-risks.md`
- `docs/deploy/vps-http-route-smoke.md`
- `docs/deploy/vps-http-smoke.md`
- `docs/deploy/vps-migration-safe-runtime-smoke.md`
- `docs/deploy/vps.md`
- `docs/deploy/weltgewebe.naming.md`
- `docs/deployment.md`
- `docs/deployment_governance.md`
- `docs/dev/codespaces.md`
- `docs/domain/modules.md`
- `docs/domain/vocabulary.md`
- `docs/edge/systemd/README.md`
- `docs/geist-und-plan.md`
- `docs/inhalt.md`
- `docs/konzepte/garnrolle-und-verortung.md`
- `docs/konzepte/garnrolle.md`
- `docs/overview/inhalt.md`
- `docs/overview/zusammenstellung.md`
- `docs/policies/agent-reading-protocol.md`
- `docs/policies/architecture-critique.md`
- `docs/policies/orientierung.md`
- `docs/process/README.md`
- `docs/process/bash-tooling-guidelines.md`
- `docs/process/ci-workflow-composition.md`
- `docs/process/fahrplan.md`
- `docs/process/report-lifecycle-contract-alignment.md`
- `docs/process/report-lifecycle.md`
- `docs/process/sprache.md`
- `docs/proofs/basemap-hamburg-artifact-proof.md`
- `docs/proofs/gewebezelle-two-operator-pilot-contract-v1.md`
- `docs/proofs/repoground-agent-utility-v1-t003-vertical-pilot.md`
- `docs/proofs/sqlx-pgbouncer-session-crud-proof.md`
- `docs/proofs/sqlx-postgres-direct-session-crud-proof.md`
- `docs/proofs/weltgewebe-os-v1-t005-two-cell-proof.md`
- `docs/proofs/weltgewebe-os-v1-t032-federation-delivery.md`
- `docs/proofs/weltgewebe-os-v1-t036-documentation-drift-reconciliation.md`
- `docs/quickstart-gate-c.md`
- `docs/reference/agent-dry-run-runner.md`
- `docs/reference/agent-handoff-contract.md`
- `docs/reference/agent-operability-fixture-matrix.md`
- `docs/reference/agent-run-evidence-lite.md`
- `docs/reference/generated-artifact-control.md`
- `docs/reference/glossar.md`
- `docs/reports/agent-readiness-audit.md`
- `docs/reports/auth-persistence-direct-proof-diagnose-audit.md`
- `docs/reports/auth-persistence-next-step.md`
- `docs/reports/auth-persistence-readiness.md`
- `docs/reports/auth-persistence-runtime-proof.md`
- `docs/reports/auth-persistence-runtime-target-reconciliation.md`
- `docs/reports/auth-pg-002-controlled-preflight.md`
- `docs/reports/auth-pg-002-cutover-plan.md`
- `docs/reports/auth-pg-002-passkey-db-store.md`
- `docs/reports/auth-pg-002-passkey-fk-readiness-audit.md`
- `docs/reports/auth-pg-002-passkey-runtime-audit-heimserver-2026-07-01.md`
- `docs/reports/auth-pg-002-passkey-runtime-audit-plan.md`
- `docs/reports/auth-pg-002-passkey-runtime-facade.md`
- `docs/reports/auth-pg-002-runtime-schema-readiness-heimserver-2026-07-01.md`
- `docs/reports/auth-pg-002-schema-preflight-ci.md`
- `docs/reports/auth-pg-003-backfill-readiness.md`
- `docs/reports/auth-pg-003-runtime-audit-heimserver-2026-07-01.md`
- `docs/reports/auth-pg-003-runtime-audit-wg-pg-proof-2026-07-01.md`
- `docs/reports/auth-status-matrix.md`
- `docs/reports/cq-02-domain-projection-load.md`
- `docs/reports/domain-account-email-uniqueness-audit.md`
- `docs/reports/domain-account-write-path-proof.md`
- `docs/reports/domain-backfill-proof.md`
- `docs/reports/domain-edge-cache-limit-design.md`
- `docs/reports/domain-edge-create-semantics-preflight.md`
- `docs/reports/domain-edge-faden-lifecycle-proof.md`
- `docs/reports/domain-edge-reference-audit.md`
- `docs/reports/domain-edge-write-path-proof.md`
- `docs/reports/domain-node-write-path-proof.md`
- `docs/reports/domain-postgres-instance-coherence-decision.md`
- `docs/reports/domain-provider-role-finding.md`
- `docs/reports/domain-read-path-proof.md`
- `docs/reports/domain-runtime-data-source-reconciliation.md`
- `docs/reports/garnrolle-identity-cutover-proof.md`
- `docs/reports/github-action-ref-pinning-audit.md`
- `docs/reports/github-actions-node24-readiness.md`
- `docs/reports/inwx-zone-reconciliation-plan.md`
- `docs/reports/kubernetes-platform-foundation-status.md`
- `docs/reports/map-architekturkritik.md`
- `docs/reports/map-basemap-proof-gap-reconciliation.md`
- `docs/reports/map-status-matrix.md`
- `docs/reports/opt-arc-001-db-proof-matrix.json`
- `docs/reports/optimierungsbericht.md`
- `docs/reports/optimierungsstatus.md`
- `docs/reports/passkey-register-verify-prep.md`
- `docs/reports/planning-registration-findings.md`
- `docs/reports/proof-matrix-generalization-decision.md`
- `docs/reports/repo-audit-2026-07-02.md`
- `docs/reports/report-lifecycle-restbestand-triage.md`
- `docs/reports/weltgewebe-os-foundation-status.md`
- `docs/reports/weltgewebe-os-v1-t018-conversation-convergence-plan.md`
- `docs/roadmap.md`
- `docs/runbook.md`
- `docs/runbook.observability.md`
- `docs/runbooks/README.md`
- `docs/runbooks/codespaces-recovery.md`
- `docs/runbooks/commonthing-mail-identity-cutover.md`
- `docs/runbooks/db-recovery.md`
- `docs/runbooks/domain-mail-cutover.md`
- `docs/runbooks/gewebezelle-manual-pilot.md`
- `docs/runbooks/gewebezelle-two-operator-pilot-v1.md`
- `docs/runbooks/incident-response.md`
- `docs/runbooks/kubernetes-ha-recovery-proof.md`
- `docs/runbooks/ops.runbook.weltgewebe-selfhost-deploy.md`
- `docs/runbooks/uv-tooling.md`
- `docs/runbooks/weltgewebe-ddns-runtime-verification.md`
- `docs/security/agent-write-scope-baseline.md`
- `docs/specs/auth-api.md`
- `docs/specs/auth-blueprint.md`
- `docs/specs/auth-state-machine.md`
- `docs/specs/auth-ui.md`
- `docs/specs/contract.md`
- `docs/specs/federation-core.md`
- `docs/specs/federation-wire-v1.md`
- `docs/specs/garnrolle-knoten-faden.md`
- `docs/specs/governance-antraege.md`
- `docs/specs/knoten-gewebe-visualisierung.md`
- `docs/specs/list-pagination-api.md`
- `docs/specs/map-experience.md`
- `docs/specs/objektlebenszyklen-und-loeschwirkungen.md`
- `docs/specs/ortsweberei-webgemeindezentrum.md`
- `docs/specs/privacy-api.md`
- `docs/specs/privacy-ui.md`
- `docs/specs/private-nachrichten.md`
- `docs/specs/ui-interaction.md`
- `docs/tasks/DEPLOY-DNS-001B.md`
- `docs/tasks/README.md`
- `docs/tasks/board.md`
- `docs/tasks/index.json`
- `docs/techstack.md`
- `docs/vision.md`
- `docs/weltgewebe-agenten-manifest.md`
- `docs/zusammenstellung.md`
- `infra/caddy/Caddyfile.http-smoke`
- `infra/caddy/Caddyfile.vps`
- `infra/compose/compose.observ.yml`
- `infra/compose/compose.prod.override.yml`
- `infra/compose/compose.vps.override.yml`
- `platform/README.md`
- `platform/cell-pilot/two-operator-pilot.contract.json`
- `platform/cell-profile.contract.json`
- `repo.meta.yaml`
- `runbooks/README.md`
- `scripts/agent/check_non_ideal_task.py`
- `scripts/agent/json_contract.py`
- `scripts/agent/run_task.py`
- `scripts/agent/tests/test_check_non_ideal_task.py`
- `scripts/agent/tests/test_run_task.py`
- `scripts/agent/tests/test_validate_handoff.py`
- `scripts/agent/validate_handoff.py`
- `scripts/basemap/build-hamburg-pmtiles.sh`
- `scripts/ci/check_actions_node24_readiness.py`
- `scripts/ci/check_github_action_pinning.py`
- `scripts/ci/fixtures/repoground_vertical_pilot.v1.json`
- `scripts/ci/tests/test_check_actions_node24_readiness.py`
- `scripts/ci/tests/test_check_github_action_pinning.py`
- `scripts/ci/tests/test_garnrolle_ontology_contract.py`
- `scripts/ci/tests/test_reconcile_public_login_smtp_env.py`
- `scripts/ci/tests/test_repoground_vertical_pilot.py`
- `scripts/ci/validate_repoground_vertical_pilot.py`
- `scripts/docmeta/audit_account_email_uniqueness.py`
- `scripts/docmeta/audit_domain_edge_references.py`
- `scripts/docmeta/check_planning_registration.py`
- `scripts/docmeta/generate_agent_readiness.py`
- `scripts/docmeta/tests/test_validate_opt_arc_001_db_proof_matrix.py`
- `scripts/docmeta/validate_claim_registry.py`
- `scripts/docmeta/validate_opt_arc_001_db_proof_matrix.py`
- `scripts/docmeta/validate_report_lifecycle.py`
- `scripts/guard/basemap-runtime-proof.sh`
- `scripts/guard/domain-multi-instance-guard.sh`
- `scripts/ops/activate-production-reconciler-from-release.sh`
- `scripts/ops/check_public_live_readiness.py`
- `scripts/ops/check_vps_db_migration_history_shape.py`
- `scripts/ops/check_vps_migration_safe_runtime_env.py`
- `scripts/ops/deploy-exact-commit-vps.sh`
- `scripts/ops/postgres-backup.sh`
- `scripts/ops/postgres-restore-latest-proof.sh`
- `scripts/ops/postgres-restore-proof.sh`
- `scripts/ops/pull-production-postgres-backup.sh`
- `scripts/ops/reconcile-production-main-vps.sh`
- `scripts/ops/reconcile_public_login_smtp_env.py`
- `scripts/ops/resolve_vps_public_bind.py`
- `scripts/performance/domain_projection_k6.js`
- `scripts/performance/domain_projection_load.py`
- `scripts/tests/test_domain_multi_instance_guard.sh`
- `tests/fixtures/agent/handoff-valid.json`

**Cluster 2** (7 Dokumente):

- `.github/PULL_REQUEST_TEMPLATE.md`
- `.github/grabowski-required-checks.json`
- `.github/workflows/ci.yml`
- `.github/workflows/review-evidence.yml`
- `docs/process/merge-quality-gate.md`
- `scripts/quality/review_governance.py`
- `scripts/quality/tests/test_review_governance.py`

**Cluster 3** (4 Dokumente):

- `.github/workflows/cost-report.yml`
- `docs/reports/cost-report.md`
- `tools/py/cost/model.csv`
- `tools/py/cost/report.py`

**Cluster 4** (3 Dokumente):

- `docs/adr/0042-consume-semantah-contracts.md`
- `docs/x-repo/peers-learnings.md`
- `docs/x-repo/semantAH.md`

### Konkrete Beispiele zur Prüfung

> Dokumente mit den meisten relates_to-Zielen und ihren konkreten Relationen.

**`docs/blueprints/domain-data-postgres-cutover.md`**:

- relates_to → `apps/api/src/routes/accounts.rs`
- relates_to → `apps/api/src/routes/edges.rs`
- relates_to → `apps/api/src/routes/nodes.rs`
- relates_to → `apps/api/src/state.rs`
- relates_to → `docs/reports/domain-account-write-path-proof.md`
- relates_to → `docs/reports/domain-node-write-path-proof.md`
- relates_to → `docs/reports/domain-postgres-instance-coherence-decision.md`
- relates_to → `docs/reports/optimierungsbericht.md`
- relates_to → `docs/reports/optimierungsstatus.md`
- relates_to → `docs/specs/contract.md`
- relates_to → `docs/specs/list-pagination-api.md`
- relates_to → `docs/tasks/board.md`
- relates_to → `docs/tasks/index.json`

**`docs/reports/domain-postgres-instance-coherence-decision.md`**:

- relates_to → `apps/api/migrations/20260716000001_multi_instance_foundation.up.sql`
- relates_to → `apps/api/src/auth/ephemeral_db.rs`
- relates_to → `apps/api/src/middleware/domain_projection.rs`
- relates_to → `apps/api/src/outbox.rs`
- relates_to → `apps/api/src/routes/nodes.rs`
- relates_to → `apps/api/src/state.rs`
- relates_to → `apps/api/tests/db_multi_instance_foundation.rs`
- relates_to → `docs/blueprints/domain-data-postgres-cutover.md`
- relates_to → `docs/reports/cq-02-domain-projection-load.md`
- relates_to → `docs/tasks/board.md`
- relates_to → `docs/tasks/index.json`
- relates_to → `scripts/guard/domain-multi-instance-guard.sh`
- relates_to → `scripts/tests/test_domain_multi_instance_guard.sh`

**`docs/runbooks/README.md`**:

- relates_to → `docs/runbook.md`
- relates_to → `docs/runbooks/codespaces-recovery.md`
- relates_to → `docs/runbooks/commonthing-mail-identity-cutover.md`
- relates_to → `docs/runbooks/db-recovery.md`
- relates_to → `docs/runbooks/gewebezelle-manual-pilot.md`
- relates_to → `docs/runbooks/gewebezelle-two-operator-pilot-v1.md`
- relates_to → `docs/runbooks/incident-response.md`
- relates_to → `docs/runbooks/ops.runbook.weltgewebe-selfhost-deploy.md`
- relates_to → `docs/runbooks/uv-tooling.md`
- relates_to → `docs/runbooks/weltgewebe-ddns-runtime-verification.md`

### Hinweise

- Alle Ergebnisse dienen der strukturellen Sichtbarmachung.
- `relates_to` ist kein Fehler — die Verteilung zeigt den aktuellen Stand.
- Keine automatischen Korrekturen werden vorgenommen.
