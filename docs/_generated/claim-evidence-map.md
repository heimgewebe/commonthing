---
id: docs.generated.claim-evidence-map
title: Claim Evidence Map
doc_type: generated
status: active
summary: Automatisch generierte Claim-Evidence-Map (Lenskit Bridge).
---

# Claim Evidence Map

Generated automatically. Do not edit.

| id | doc | locator | status | owner | last_verified | evidence |
| --- | --- | --- | --- | --- | --- | --- |
| claim-agent-safe-001 | docs/claims/registry.yml | claims[id=CLAIM-AGENT-SAFE-001] | partial | docs-mechanik | 2026-06-05 | 4 items |
| claim-agent-safe-002 | docs/claims/registry.yml | claims[id=CLAIM-AGENT-SAFE-002] | partial | docs-mechanik | 2026-06-05 | 3 items |
| claim-agent-safe-003 | docs/claims/registry.yml | claims[id=CLAIM-AGENT-SAFE-003] | partial | docs-mechanik | 2026-06-05 | 4 items |
| claim-agent-safe-005 | docs/claims/registry.yml | claims[id=CLAIM-AGENT-SAFE-005] | partial | docs-mechanik | 2026-06-26 | 8 items |
| claim-agent-safe-006 | docs/claims/registry.yml | claims[id=CLAIM-AGENT-SAFE-006] | partial | docs-mechanik | 2026-06-26 | 7 items |
| claim-agent-safe-007 | docs/claims/registry.yml | claims[id=CLAIM-AGENT-SAFE-007] | partial | docs-mechanik | 2026-06-27 | 10 items |
| claim-agent-safe-008 | docs/claims/registry.yml | claims[id=CLAIM-AGENT-SAFE-008] | partial | docs-mechanik | 2026-06-27 | 7 items |
| claim-domain-garnrolle-001 | docs/claims/registry.yml | claims[id=CLAIM-DOMAIN-GARNROLLE-001] | partial | product-domain | 2026-07-11 | 4 items |
| claim-map-truth-001 | docs/claims/registry.yml | claims[id=CLAIM-MAP-TRUTH-001] | partial | product-map | 2026-07-11 | 5 items |
| claim-runtime-live-prod-001 | docs/claims/registry.yml | claims[id=CLAIM-RUNTIME-LIVE-PROD-001] | partial | runtime | 2026-09-10 | 5 items |
| claim-runtime-prod-postgres-001 | docs/claims/registry.yml | claims[id=CLAIM-RUNTIME-PROD-POSTGRES-001] | partial | runtime | 2026-09-10 | 7 items |
| claim-ui-state-001 | docs/claims/registry.yml | claims[id=CLAIM-UI-STATE-001] | partial | product-ui | 2026-07-11 | 4 items |
| claim-ui-surface-001 | docs/claims/registry.yml | claims[id=CLAIM-UI-SURFACE-001] | partial | product-ui | 2026-07-11 | 5 items |

## Details

### CLAIM-AGENT-SAFE-001

- Entry: `claim-agent-safe-001`
- Locator: `claims[id=CLAIM-AGENT-SAFE-001]`
- Status: `partial`
- Owner: `docs-mechanik`
- Last verified: `2026-06-05`

Evidence:

| Kind | Target |
| ---- | ------ |
| `file` | `scripts/agent/check_agent_preflight.py` |
| `test` | `scripts/agent/tests/test_check_agent_preflight.py` |
| `file` | `.github/workflows/agent-safety-preflight.yml` |
| `file` | `docs/security/agent-write-scope-baseline.md` |

Does not prove:

- A green verify does not prove the claims are true or complete, only that no declared claim contradicts its declared evidence.

### CLAIM-AGENT-SAFE-002

- Entry: `claim-agent-safe-002`
- Locator: `claims[id=CLAIM-AGENT-SAFE-002]`
- Status: `partial`
- Owner: `docs-mechanik`
- Last verified: `2026-06-05`

Evidence:

| Kind | Target |
| ---- | ------ |
| `file` | `scripts/docmeta/generate_agent_readiness.py` |
| `test` | `scripts/docmeta/tests/test_generate_agent_readiness.py` |
| `file` | `docs/_generated/agent-readiness.md` |

Does not prove:

- A green verify does not prove the claims are true or complete, only that no declared claim contradicts its declared evidence.

### CLAIM-AGENT-SAFE-003

- Entry: `claim-agent-safe-003`
- Locator: `claims[id=CLAIM-AGENT-SAFE-003]`
- Status: `partial`
- Owner: `docs-mechanik`
- Last verified: `2026-06-05`

Evidence:

| Kind | Target |
| ---- | ------ |
| `file` | `docs/claims/registry.yml` |
| `file` | `docs/claims/README.md` |
| `file` | `scripts/docmeta/validate_claim_registry.py` |
| `test` | `scripts/docmeta/tests/test_validate_claim_registry.py` |

Does not prove:

- A green verify does not prove the claims are true or complete, only that no declared claim contradicts its declared evidence.

### CLAIM-AGENT-SAFE-005

- Entry: `claim-agent-safe-005`
- Locator: `claims[id=CLAIM-AGENT-SAFE-005]`
- Status: `partial`
- Owner: `docs-mechanik`
- Last verified: `2026-06-26`

Evidence:

| Kind | Target |
| ---- | ------ |
| `file` | `contracts/agent/handoff.schema.json` |
| `file` | `scripts/agent/json_contract.py` |
| `file` | `scripts/agent/validate_handoff.py` |
| `file` | `scripts/contracts-agent-check.sh` |
| `test` | `scripts/agent/tests/test_validate_handoff.py` |
| `file` | `docs/reference/agent-operability-fixture-matrix.md` |
| `file` | `.github/workflows/agent-safety-preflight.yml` |
| `file` | `.github/workflows/contracts-validate.yml` |

Does not prove:

- A green verify does not prove the claims are true or complete, only that no declared claim contradicts its declared evidence.

### CLAIM-AGENT-SAFE-006

- Entry: `claim-agent-safe-006`
- Locator: `claims[id=CLAIM-AGENT-SAFE-006]`
- Status: `partial`
- Owner: `docs-mechanik`
- Last verified: `2026-06-26`

Evidence:

| Kind | Target |
| ---- | ------ |
| `file` | `scripts/agent/run_task.py` |
| `test` | `scripts/agent/tests/test_run_task.py` |
| `file` | `scripts/docmeta/generate_agent_readiness.py` |
| `test` | `scripts/docmeta/tests/test_generate_agent_readiness.py` |
| `test` | `scripts/docmeta/tests/test_agent_readiness_smoke_contract.py` |
| `file` | `.github/workflows/agent-safety-preflight.yml` |
| `file` | `docs/reference/agent-dry-run-runner.md` |

Does not prove:

- A green verify does not prove the claims are true or complete, only that no declared claim contradicts its declared evidence.

### CLAIM-AGENT-SAFE-007

- Entry: `claim-agent-safe-007`
- Locator: `claims[id=CLAIM-AGENT-SAFE-007]`
- Status: `partial`
- Owner: `docs-mechanik`
- Last verified: `2026-06-27`

Evidence:

| Kind | Target |
| ---- | ------ |
| `file` | `contracts/agent/validation.schema.json` |
| `file` | `contracts/agent/run-result.schema.json` |
| `file` | `scripts/agent/run_task.py` |
| `test` | `scripts/agent/tests/test_run_task.py` |
| `file` | `scripts/agent/validate_agent_contracts.py` |
| `file` | `scripts/contracts-agent-check.sh` |
| `file` | `scripts/docmeta/generate_agent_readiness.py` |
| `test` | `scripts/docmeta/tests/test_generate_agent_readiness.py` |
| `file` | `.github/workflows/agent-safety-preflight.yml` |
| `file` | `docs/reference/agent-run-evidence-lite.md` |

Does not prove:

- A green verify does not prove the claims are true or complete, only that no declared claim contradicts its declared evidence.

### CLAIM-AGENT-SAFE-008

- Entry: `claim-agent-safe-008`
- Locator: `claims[id=CLAIM-AGENT-SAFE-008]`
- Status: `partial`
- Owner: `docs-mechanik`
- Last verified: `2026-06-27`

Evidence:

| Kind | Target |
| ---- | ------ |
| `file` | `.wgx/generated-artifacts.yml` |
| `file` | `scripts/docmeta/validate_generated_artifacts.py` |
| `test` | `scripts/docmeta/tests/test_validate_generated_artifacts.py` |
| `file` | `scripts/docmeta/generate_agent_readiness.py` |
| `file` | `scripts/docmeta/generated-files-guard.sh` |
| `file` | `.github/workflows/docs-guard.yml` |
| `file` | `docs/reference/generated-artifact-control.md` |

Does not prove:

- A green verify does not prove the claims are true or complete, only that no declared claim contradicts its declared evidence.

### CLAIM-DOMAIN-GARNROLLE-001

- Entry: `claim-domain-garnrolle-001`
- Locator: `claims[id=CLAIM-DOMAIN-GARNROLLE-001]`
- Status: `partial`
- Owner: `product-domain`
- Last verified: `2026-07-11`

Evidence:

| Kind | Target |
| ---- | ------ |
| `file` | `docs/specs/garnrolle-knoten-faden.md` |
| `file` | `contracts/domain/account.schema.json` |
| `file` | `apps/api/src/routes/accounts.rs` |
| `test` | `apps/web/tests/garnrolle-self-service.spec.ts` |

Does not prove:

- A green verify does not prove the claims are true or complete, only that no declared claim contradicts its declared evidence.

### CLAIM-MAP-TRUTH-001

- Entry: `claim-map-truth-001`
- Locator: `claims[id=CLAIM-MAP-TRUTH-001]`
- Status: `partial`
- Owner: `product-map`
- Last verified: `2026-07-11`

Evidence:

| Kind | Target |
| ---- | ------ |
| `file` | `docs/specs/map-experience.md` |
| `file` | `apps/web/src/routes/map/+page.ts` |
| `file` | `apps/web/src/lib/map/scene.ts` |
| `test` | `apps/web/src/lib/map/scene.test.ts` |
| `test` | `apps/web/tests/map-load-fallback.spec.ts` |

Does not prove:

- A green verify does not prove the claims are true or complete, only that no declared claim contradicts its declared evidence.

### CLAIM-RUNTIME-LIVE-PROD-001

- Entry: `claim-runtime-live-prod-001`
- Locator: `claims[id=CLAIM-RUNTIME-LIVE-PROD-001]`
- Status: `partial`
- Owner: `runtime`
- Last verified: `2026-09-10`

Evidence:

| Kind | Target |
| ---- | ------ |
| `file` | `docs/reports/map-status.md` |
| `file` | `runtime/README.md` |
| `file` | `docs/deploy/CHANGELOG.md` |
| `file` | `scripts/docmeta/validate_doc_freshness_registry.py` |
| `file` | `scripts/docmeta/freshness_scope_policy.yml` |

Does not prove:

- A green verify does not prove the claims are true or complete, only that no declared claim contradicts its declared evidence.

### CLAIM-RUNTIME-PROD-POSTGRES-001

- Entry: `claim-runtime-prod-postgres-001`
- Locator: `claims[id=CLAIM-RUNTIME-PROD-POSTGRES-001]`
- Status: `partial`
- Owner: `runtime`
- Last verified: `2026-09-10`

Evidence:

| Kind | Target |
| ---- | ------ |
| `file` | `README.md` |
| `file` | `runtime/README.md` |
| `file` | `docs/deploy/CHANGELOG.md` |
| `file` | `infra/compose/compose.prod.yml` |
| `file` | `.env.prod.example` |
| `file` | `scripts/guard/compose-image-guard.sh` |
| `test` | `scripts/tests/test_repo_contract_guards.sh` |

Does not prove:

- A green verify does not prove the claims are true or complete, only that no declared claim contradicts its declared evidence.

### CLAIM-UI-STATE-001

- Entry: `claim-ui-state-001`
- Locator: `claims[id=CLAIM-UI-STATE-001]`
- Status: `partial`
- Owner: `product-ui`
- Last verified: `2026-07-11`

Evidence:

| Kind | Target |
| ---- | ------ |
| `file` | `docs/specs/ui-state-machine.md` |
| `file` | `apps/web/src/lib/stores/uiView.ts` |
| `file` | `apps/web/src/lib/stores/uiInvariants.ts` |
| `test` | `apps/web/src/lib/stores/uiInvariants.test.ts` |

Does not prove:

- A green verify does not prove the claims are true or complete, only that no declared claim contradicts its declared evidence.

### CLAIM-UI-SURFACE-001

- Entry: `claim-ui-surface-001`
- Locator: `claims[id=CLAIM-UI-SURFACE-001]`
- Status: `partial`
- Owner: `product-ui`
- Last verified: `2026-07-11`

Evidence:

| Kind | Target |
| ---- | ------ |
| `file` | `docs/specs/ui-interaction.md` |
| `file` | `apps/web/src/lib/components/ContextPanel.svelte` |
| `file` | `apps/web/src/lib/stores/overlayManager.ts` |
| `test` | `apps/web/tests/ui-filter.spec.ts` |
| `test` | `apps/web/tests/map-interaction.spec.ts` |

Does not prove:

- A green verify does not prove the claims are true or complete, only that no declared claim contradicts its declared evidence.
