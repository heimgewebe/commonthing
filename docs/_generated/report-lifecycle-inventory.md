---
id: docs.generated.report-lifecycle-inventory
title: Report Lifecycle Inventory
doc_type: generated
status: active
canonicality: derived
summary: Automatisch generiertes Inventar der Report-Lifecycle-Metadaten.
---
# Report Lifecycle Inventory

Generated automatically. Do not edit manually.
This inventory is descriptive only. Absent core lifecycle metadata is expected at this stage and is not a policy judgement.
Primary references are exact path matches in canonical documentation surfaces. Derived generated references are reported separately.

## Summary

| Metric | Count |
| --- | ---: |
| files_total | 52 |
| files_with_frontmatter | 52 |
| files_without_frontmatter | 0 |
| files_with_status | 52 |
| files_missing_status | 0 |
| files_with_lifecycle_state | 47 |
| files_missing_lifecycle_state | 5 |
| files_with_lifecycle | 46 |
| files_missing_lifecycle | 6 |
| files_with_owner_task | 48 |
| files_missing_owner_task | 4 |
| files_with_review_after | 32 |
| files_missing_review_after | 20 |
| files_primary_referenced | 46 |
| files_primary_unreferenced | 6 |
| files_with_derived_references | 52 |
| files_with_relations | 52 |
| truth_contract_migrated | 0 |
| truth_contract_deprecated | 20 |
| truth_contract_not_decision_relevant | 32 |
| files_with_missing_supersession_target | 0 |

## Controlled Evidence Surfaces

This section is a readable projection of `.wgx/generated-artifacts.yml`; the registry remains the only machine authority.

| Metric | Count |
| --- | ---: |
| controlled_surfaces | 19 |
| generated_surfaces | 18 |
| curated_surfaces | 1 |
| declared_consumer_edges | 51 |
| exclusive_claims | 19 |
| justified_overlap_pairs | 10 |

| Path | Kind | Scope | Sources | Generator | Checks | Consumers | Exclusive claim | Does not establish |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| docs/_generated/agent-readiness.md | generated | Repository agent contracts, claim fixtures, preflight and readiness inputs. | contracts/agent; scripts/agent; scripts/docmeta/generate_agent_readiness.py; docs/claims/registry.yml | python3 -m scripts.docmeta.generate_agent_readiness | python3 -m scripts.docmeta.generate_agent_readiness --check | .github/workflows/docs-guard.yml: Executes the registry tests and documentation validation gates in CI.; scripts/docmeta/generated-files-guard.sh: Fails closed when the committed controlled surface or its registry declaration drifts.; docs/blueprints/blueprint-agent-safety-control-layer.md: Uses the readiness diagnostics to explain the safety-control handoff boundary. | Readiness diagnostics derived from the declared agent contract and its evidence fixtures. | Canonical policy, runtime state, or deployment truth.; Correctness of the underlying source claims beyond the declared generator method. |
| docs/_generated/architecture-drift.md | generated | Canonical architecture documents and their declared implementation relations. | docs; scripts/docmeta/generate_architecture_drift.py | python3 -m scripts.docmeta.generate_architecture_drift | python3 -m scripts.docmeta.generate_architecture_drift --check | .github/workflows/docs-guard.yml: Executes the registry tests and documentation validation gates in CI.; scripts/docmeta/generated-files-guard.sh: Fails closed when the committed controlled surface or its registry declaration drifts.; docs/policies/architecture-critique.md: Uses architecture drift as diagnostic input for structured critique. | Documentation-level architecture drift detected from canonical architecture sources. | Canonical policy, runtime state, or deployment truth.; Correctness of the underlying source claims beyond the declared generator method. |
| docs/_generated/backlinks.md | generated | Exact reverse references between indexed documentation paths. | docs; scripts/docmeta/generate_backlinks.py | python3 -m scripts.docmeta.generate_backlinks | python3 -m scripts.docmeta.generate_backlinks --check | .github/workflows/docs-guard.yml: Executes the registry tests and documentation validation gates in CI.; scripts/docmeta/generated-files-guard.sh: Fails closed when the committed controlled surface or its registry declaration drifts.; scripts/ci/fixtures/repoground_vertical_pilot.v1.json: Uses backlink output as a retrieval and navigation fixture. | Reverse documentation references for every indexed target path. | Canonical policy, runtime state, or deployment truth.; Correctness of the underlying source claims beyond the declared generator method. |
| docs/_generated/change-resonance.md | generated | Declared documentation and implementation relations affected by a source change. | docs; scripts/docmeta/generate_change_resonance.py | python3 -m scripts.docmeta.generate_change_resonance | python3 -m scripts.docmeta.generate_change_resonance --check | .github/workflows/docs-guard.yml: Executes the registry tests and documentation validation gates in CI.; scripts/docmeta/generated-files-guard.sh: Fails closed when the committed controlled surface or its registry declaration drifts. | Potential relation-based change impact across documented repository surfaces. | Canonical policy, runtime state, or deployment truth.; Correctness of the underlying source claims beyond the declared generator method. |
| docs/_generated/claim-evidence-map.md | generated | Registered claims, freshness declarations and their linked evidence paths. | docs/doc-freshness-registry.yml; docs/claims/registry.yml; scripts/docmeta/generate_claim_evidence_map.py; scripts/docmeta/validate_doc_freshness_registry.py | python3 -m scripts.docmeta.generate_claim_evidence_map | python3 -m scripts.docmeta.generate_claim_evidence_map --check | .github/workflows/docs-guard.yml: Executes the registry tests and documentation validation gates in CI.; scripts/docmeta/generated-files-guard.sh: Fails closed when the committed controlled surface or its registry declaration drifts.; docs/claims/README.md: Explains how readers use the generated claim-to-evidence projection. | Exact declared links from registered claims to repository evidence. | Canonical policy, runtime state, or deployment truth.; Correctness of the underlying source claims beyond the declared generator method. |
| docs/_generated/doc-coverage.md | generated | Frontmatter and registration coverage of repository documentation. | docs; scripts/docmeta/generate_doc_coverage.py | python3 -m scripts.docmeta.generate_doc_coverage | python3 -m scripts.docmeta.generate_doc_coverage --check | .github/workflows/docs-guard.yml: Executes the registry tests and documentation validation gates in CI.; scripts/docmeta/generated-files-guard.sh: Fails closed when the committed controlled surface or its registry declaration drifts.; audit/impl-registry.yaml: Uses coverage diagnostics when assessing mapped implementation evidence. | Documentation metadata and registration coverage for scanned documents. | Canonical policy, runtime state, or deployment truth.; Correctness of the underlying source claims beyond the declared generator method. |
| docs/_generated/doc-index.md | generated | Forward navigation metadata for indexed documentation. | docs; scripts/docmeta/generate-doc-index.sh | bash scripts/docmeta/generate-doc-index.sh | bash scripts/docmeta/generate-doc-index.sh --check | .github/workflows/docs-guard.yml: Executes the registry tests and documentation validation gates in CI.; scripts/docmeta/generated-files-guard.sh: Fails closed when the committed controlled surface or its registry declaration drifts.; docs/blueprints/doc-structure-task-control-examples.md: Uses the document index as the navigation example surface. | Forward documentation index entries derived from document metadata. | Canonical policy, runtime state, or deployment truth.; Correctness of the underlying source claims beyond the declared generator method. |
| docs/_generated/impl-index.md | generated | Registered implementation paths under apps, scripts and contracts. | apps; scripts; contracts; scripts/docmeta/generate-impl-index.sh | bash scripts/docmeta/generate-impl-index.sh | bash scripts/docmeta/generate-impl-index.sh --check | .github/workflows/docs-guard.yml: Executes the registry tests and documentation validation gates in CI.; scripts/docmeta/generated-files-guard.sh: Fails closed when the committed controlled surface or its registry declaration drifts.; audit/impl-registry.yaml: Cross-checks registered implementation paths against the generated implementation index. | Forward index of implementation files and their repository registration context. | Canonical policy, runtime state, or deployment truth.; Correctness of the underlying source claims beyond the declared generator method. |
| docs/_generated/implicit-dependencies.md | generated | Dependency-like relations inferred from repository documentation metadata. | docs; scripts/docmeta/generate_implicit_dependencies.py | python3 -m scripts.docmeta.generate_implicit_dependencies | python3 -m scripts.docmeta.generate_implicit_dependencies --check | .github/workflows/docs-guard.yml: Executes the registry tests and documentation validation gates in CI.; scripts/docmeta/generated-files-guard.sh: Fails closed when the committed controlled surface or its registry declaration drifts. | Potential undeclared documentation dependencies inferred from existing relations. | Canonical policy, runtime state, or deployment truth.; Correctness of the underlying source claims beyond the declared generator method. |
| docs/_generated/knowledge-gaps.md | generated | Missing or incomplete documentation knowledge indicated by metadata and relations. | docs; scripts/docmeta/generate_knowledge_gaps.py | python3 -m scripts.docmeta.generate_knowledge_gaps | python3 -m scripts.docmeta.generate_knowledge_gaps --check | .github/workflows/docs-guard.yml: Executes the registry tests and documentation validation gates in CI.; scripts/docmeta/generated-files-guard.sh: Fails closed when the committed controlled surface or its registry declaration drifts. | Diagnostic knowledge gaps in the documented repository model. | Canonical policy, runtime state, or deployment truth.; Correctness of the underlying source claims beyond the declared generator method. |
| docs/_generated/orphans.md | generated | Documentation files without qualifying inbound or structural relations. | docs; scripts/docmeta/generate_orphans.py | python3 -m scripts.docmeta.generate_orphans | python3 -m scripts.docmeta.generate_orphans --check | .github/workflows/docs-guard.yml: Executes the registry tests and documentation validation gates in CI.; scripts/docmeta/generated-files-guard.sh: Fails closed when the committed controlled surface or its registry declaration drifts. | Potentially orphaned documentation paths under the repository relation rules. | Canonical policy, runtime state, or deployment truth.; Correctness of the underlying source claims beyond the declared generator method. |
| docs/_generated/relates-to-audit.md | generated | The relates_to subset of declared documentation relations. | docs; scripts/docmeta/generate_relates_to_audit.py | python3 -m scripts.docmeta.generate_relates_to_audit | python3 -m scripts.docmeta.generate_relates_to_audit --check | .github/workflows/docs-guard.yml: Executes the registry tests and documentation validation gates in CI.; scripts/docmeta/generated-files-guard.sh: Fails closed when the committed controlled surface or its registry declaration drifts.; scripts/ci/fixtures/repoground_vertical_pilot.v1.json: Uses relates_to audit output as a bounded relation-retrieval fixture. | Audit of relates_to edges and their exact targets. | Canonical policy, runtime state, or deployment truth.; Correctness of the underlying source claims beyond the declared generator method. |
| docs/_generated/relations-analysis.md | generated | All supported declared documentation relation types and targets. | docs; scripts/docmeta/generate_relations_analysis.py | python3 -m scripts.docmeta.generate_relations_analysis | python3 -m scripts.docmeta.generate_relations_analysis --check | .github/workflows/docs-guard.yml: Executes the registry tests and documentation validation gates in CI.; scripts/docmeta/generated-files-guard.sh: Fails closed when the committed controlled surface or its registry declaration drifts.; scripts/ci/fixtures/repoground_vertical_pilot.v1.json: Uses relation analysis as a broader graph-retrieval fixture. | Aggregate relation-graph diagnostics across supported relation types. | Canonical policy, runtime state, or deployment truth.; Correctness of the underlying source claims beyond the declared generator method. |
| docs/_generated/report-lifecycle-inventory.md | generated | All Markdown reports under docs/reports plus declared generated and curated control surfaces. | docs/reports; scripts/docmeta/generate_report_lifecycle_inventory.py | python3 -m scripts.docmeta.generate_report_lifecycle_inventory | python3 -m scripts.docmeta.generate_report_lifecycle_inventory --check | .github/workflows/docs-guard.yml: Executes the registry tests and documentation validation gates in CI.; scripts/docmeta/generated-files-guard.sh: Fails closed when the committed controlled surface or its registry declaration drifts.; docs/process/report-lifecycle.md: Uses the inventory to explain lifecycle coverage and remaining migration work. | Descriptive lifecycle, reference and control-contract inventory for report and evidence surfaces. | Canonical policy, runtime state, or deployment truth.; Correctness of the underlying source claims beyond the declared generator method. |
| docs/_generated/report-lifecycle.md | generated | Policy compliance of report lifecycle metadata and truth contracts. | docs/reports; scripts/docmeta/generate_report_lifecycle.py; docs/process/report-lifecycle.md | python3 -m scripts.docmeta.generate_report_lifecycle | python3 -m scripts.docmeta.generate_report_lifecycle --check | .github/workflows/docs-guard.yml: Executes the registry tests and documentation validation gates in CI.; scripts/docmeta/generated-files-guard.sh: Fails closed when the committed controlled surface or its registry declaration drifts.; docs/process/report-lifecycle-contract-alignment.md: Uses compliance output to track truth-contract alignment. | Report-lifecycle compliance findings against the canonical lifecycle policy. | Canonical policy, runtime state, or deployment truth.; Correctness of the underlying source claims beyond the declared generator method. |
| docs/_generated/staleness-report.md | generated | Review dates and freshness metadata declared by documentation. | docs; scripts/docmeta/generate_staleness_report.py | python3 -m scripts.docmeta.generate_staleness_report | python3 -m scripts.docmeta.generate_staleness_report --check | .github/workflows/docs-guard.yml: Executes the registry tests and documentation validation gates in CI.; scripts/docmeta/generated-files-guard.sh: Fails closed when the committed controlled surface or its registry declaration drifts. | Documentation review-age and declared freshness diagnostics. | Canonical policy, runtime state, or deployment truth.; Correctness of the underlying source claims beyond the declared generator method. |
| docs/_generated/supersession-map.md | generated | Declared document supersession and replacement edges. | docs; scripts/docmeta/generate_supersession_map.py | python3 -m scripts.docmeta.generate_supersession_map | python3 -m scripts.docmeta.generate_supersession_map --check | .github/workflows/docs-guard.yml: Executes the registry tests and documentation validation gates in CI.; scripts/docmeta/generated-files-guard.sh: Fails closed when the committed controlled surface or its registry declaration drifts. | Exact declared supersession paths between documentation surfaces. | Canonical policy, runtime state, or deployment truth.; Correctness of the underlying source claims beyond the declared generator method. |
| docs/_generated/system-map.md | generated | Repository documents, implementation registrations and declared structural relations. | docs; scripts/docmeta/generate_system_map.py | python3 -m scripts.docmeta.generate_system_map | python3 -m scripts.docmeta.generate_system_map --check | .github/workflows/docs-guard.yml: Executes the registry tests and documentation validation gates in CI.; scripts/docmeta/generated-files-guard.sh: Fails closed when the committed controlled surface or its registry declaration drifts.; architecture/blueprint.docmeta-engine.md: Uses the system map as the generated structural view of the docmeta engine. | Structural repository map connecting documented components and registered implementation surfaces. | Canonical policy, runtime state, or deployment truth.; Correctness of the underlying source claims beyond the declared generator method. |
| docs/tasks/index.json | curated_index | Curated repository task-control records derived from the task board and task schema. | docs/tasks/board.md; docs/tasks/schema.json; docs/reports/optimierungsstatus.json | curated / no generator | python3 -m scripts.docmeta.validate_task_index docs/tasks/index.json; python3 -m scripts.docmeta.generate_task_index --check | .github/workflows/docs-guard.yml: Executes the registry tests and documentation validation gates in CI.; scripts/docmeta/generated-files-guard.sh: Fails closed when the committed controlled surface or its registry declaration drifts.; scripts/docmeta/check_planning_registration.py: Uses the curated index to verify planning registration. | Canonical repository-local task index for documentation task-control consumers. | A Bureau claim, worker lease, dispatch decision, or execution authority.; Runtime completion of any indexed task. |

### Overlap Justifications

| Surface | Related surface | Distinction / consumer justification |
| --- | --- | --- |
| docs/_generated/agent-readiness.md | docs/_generated/claim-evidence-map.md | `docs/claims/README.md` uses evidence links; readiness is a separate execution gate. |
| docs/_generated/architecture-drift.md | docs/_generated/staleness-report.md | `docs/policies/architecture-critique.md` uses structural drift, not review-age status. |
| docs/_generated/backlinks.md | docs/_generated/doc-index.md | `scripts/ci/fixtures/repoground_vertical_pilot.v1.json` tests lookup direction. |
| docs/_generated/backlinks.md | docs/_generated/supersession-map.md | `scripts/ci/fixtures/repoground_vertical_pilot.v1.json` tests backlink edge types. |
| docs/_generated/change-resonance.md | docs/_generated/implicit-dependencies.md | `.github/workflows/docs-guard.yml` checks declared impact apart from inferred dependencies. |
| docs/_generated/doc-coverage.md | docs/_generated/orphans.md | `audit/impl-registry.yaml` uses metadata coverage; orphan checks stay relation-based. |
| docs/_generated/impl-index.md | docs/_generated/system-map.md | `architecture/blueprint.docmeta-engine.md` uses the map; the index remains a path list. |
| docs/_generated/implicit-dependencies.md | docs/_generated/knowledge-gaps.md | `.github/workflows/docs-guard.yml` checks inferred edges apart from missing knowledge. |
| docs/_generated/relates-to-audit.md | docs/_generated/relations-analysis.md | `scripts/ci/fixtures/repoground_vertical_pilot.v1.json` tests relation scopes. |
| docs/_generated/report-lifecycle-inventory.md | docs/_generated/report-lifecycle.md | `docs/process/report-lifecycle.md` uses inventory; alignment uses compliance findings. |

## Doc Type Distribution

| doc_type | Count |
| --- | ---: |
| documentation | 1 |
| reference | 1 |
| report | 46 |
| status | 2 |
| status-matrix | 2 |

## Reports

| Path | doc_type | status | lifecycle_state | lifecycle | owner_task | review_after | superseded_by | truth migration | primary refs | derived refs | relations | absent core lifecycle fields | supersession target diagnostic |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | --- | --- |
| docs/reports/agent-readiness-audit.md | documentation | active |  |  |  |  |  | not_decision_relevant | 3 | 4 | 1 | lifecycle, owner_task, review_after, lifecycle_state |  |
| docs/reports/auth-persistence-direct-proof-diagnose-audit.md | report | deprecated | superseded | audit | OPT-API-002 |  | docs/reports/optimierungsstatus.md | deprecated | 0 | 5 | 4 | review_after |  |
| docs/reports/auth-persistence-next-step.md | report | deprecated | superseded | decision-prep | OPT-API-002 |  | docs/reports/optimierungsstatus.md | deprecated | 5 | 6 | 4 | review_after |  |
| docs/reports/auth-persistence-readiness.md | report | deprecated | superseded | decision-prep | OPT-API-002 |  | docs/reports/optimierungsstatus.md | deprecated | 5 | 6 | 3 | review_after |  |
| docs/reports/auth-persistence-runtime-proof.md | report | deprecated | superseded | proof | OPT-API-002 |  | docs/reports/optimierungsstatus.md | deprecated | 4 | 5 | 6 | review_after |  |
| docs/reports/auth-persistence-runtime-target-reconciliation.md | report | deprecated | archived | audit | OPT-API-002 |  |  | deprecated | 2 | 5 | 5 | review_after |  |
| docs/reports/auth-pg-002-controlled-preflight.md | report | active | active | planning | AUTH-PG-002 | 2026-09-30 |  | not_decision_relevant | 1 | 4 | 2 |  |  |
| docs/reports/auth-pg-002-cutover-plan.md | report | active | active | planning | AUTH-PG-002 | 2026-09-30 |  | not_decision_relevant | 7 | 4 | 4 |  |  |
| docs/reports/auth-pg-002-passkey-db-store.md | report | active | active | proof | AUTH-PG-002 | 2026-09-30 |  | not_decision_relevant | 4 | 4 | 3 |  |  |
| docs/reports/auth-pg-002-passkey-fk-readiness-audit.md | report | active | active | proof | AUTH-PG-002 | 2026-09-30 |  | not_decision_relevant | 2 | 4 | 4 |  |  |
| docs/reports/auth-pg-002-passkey-runtime-audit-heimserver-2026-07-01.md | report | active | active | proof | AUTH-PG-002 | 2026-09-30 |  | not_decision_relevant | 2 | 4 | 4 |  |  |
| docs/reports/auth-pg-002-passkey-runtime-audit-plan.md | report | active | active | planning | AUTH-PG-002 | 2026-09-30 |  | not_decision_relevant | 2 | 4 | 3 |  |  |
| docs/reports/auth-pg-002-passkey-runtime-facade.md | report | active | active | proof | AUTH-PG-002 | 2026-09-30 |  | not_decision_relevant | 4 | 4 | 3 |  |  |
| docs/reports/auth-pg-002-runtime-schema-readiness-heimserver-2026-07-01.md | report | active | active | proof | AUTH-PG-002 | 2026-09-30 |  | not_decision_relevant | 4 | 4 | 4 |  |  |
| docs/reports/auth-pg-002-schema-preflight-ci.md | report | active | active | proof | AUTH-PG-002 | 2026-09-30 |  | not_decision_relevant | 0 | 4 | 4 |  |  |
| docs/reports/auth-pg-003-backfill-readiness.md | report | active | active | proof | AUTH-PG-003 | 2026-09-30 |  | not_decision_relevant | 3 | 4 | 4 |  |  |
| docs/reports/auth-pg-003-runtime-audit-heimserver-2026-07-01.md | report | active | active | proof | AUTH-PG-003 | 2026-09-30 |  | not_decision_relevant | 1 | 4 | 4 |  |  |
| docs/reports/auth-pg-003-runtime-audit-wg-pg-proof-2026-07-01.md | report | active | active | proof | AUTH-PG-003 | 2026-09-30 |  | not_decision_relevant | 1 | 4 | 2 |  |  |
| docs/reports/auth-status-matrix.md | reference | active |  |  |  |  |  | not_decision_relevant | 16 | 5 | 3 | lifecycle, owner_task, review_after, lifecycle_state |  |
| docs/reports/cost-report.md | report | active | active | generated | DOCMETA-REPORT-LIFECYCLE-001 | 2026-09-29 |  | not_decision_relevant | 1 | 4 | 3 |  |  |
| docs/reports/cq-02-domain-projection-load.md | report | active | active | proof | WELTGEWEBE-OS-002 | 2027-01-08 |  | not_decision_relevant | 1 | 5 | 9 |  |  |
| docs/reports/domain-account-email-uniqueness-audit.md | report | deprecated | archived | audit | OPT-ARC-001 |  |  | deprecated | 2 | 5 | 4 | review_after |  |
| docs/reports/domain-account-write-path-proof.md | report | deprecated | archived | proof | OPT-ARC-001 |  |  | deprecated | 6 | 5 | 6 | review_after |  |
| docs/reports/domain-backfill-proof.md | report | deprecated | archived | proof | OPT-ARC-001 |  |  | deprecated | 2 | 5 | 4 | review_after |  |
| docs/reports/domain-edge-cache-limit-design.md | report | active | active | decision-prep | DOMAIN-PG-003 | 2026-09-29 |  | not_decision_relevant | 0 | 4 | 5 |  |  |
| docs/reports/domain-edge-create-semantics-preflight.md | report | deprecated | superseded | decision-prep | OPT-ARC-001 |  | docs/reports/domain-edge-write-path-proof.md | deprecated | 1 | 6 | 7 | review_after |  |
| docs/reports/domain-edge-faden-lifecycle-proof.md | report | active | active | proof | OPT-ARC-001 | 2026-10-17 |  | not_decision_relevant | 1 | 5 | 3 |  |  |
| docs/reports/domain-edge-reference-audit.md | report | deprecated | archived | audit | OPT-ARC-001 |  |  | deprecated | 3 | 5 | 6 | review_after |  |
| docs/reports/domain-edge-write-path-proof.md | report | deprecated | superseded | proof | OPT-ARC-001 | 2026-07-16 | docs/reports/domain-edge-faden-lifecycle-proof.md | deprecated | 3 | 7 | 8 |  |  |
| docs/reports/domain-node-write-path-proof.md | report | deprecated | archived | proof | OPT-ARC-001 |  |  | deprecated | 4 | 5 | 6 | review_after |  |
| docs/reports/domain-postgres-instance-coherence-decision.md | report | active | active | audit | WELTGEWEBE-OS-002 | 2027-01-16 |  | not_decision_relevant | 7 | 6 | 13 |  |  |
| docs/reports/domain-provider-role-finding.md | report | active | active | audit | DEPLOY-DNS-001 | 2026-07-23 |  | not_decision_relevant | 4 | 4 | 3 |  |  |
| docs/reports/domain-read-path-proof.md | report | deprecated | archived | proof | OPT-ARC-001 |  |  | deprecated | 5 | 5 | 5 | review_after |  |
| docs/reports/domain-runtime-data-source-reconciliation.md | report | active | active | audit | DB-PROOF-001 | 2026-07-18 |  | not_decision_relevant | 1 | 4 | 5 |  |  |
| docs/reports/garnrolle-identity-cutover-proof.md | report | active | active | proof | OPT-ARC-001 | 2026-10-24 |  | not_decision_relevant | 1 | 4 | 4 |  |  |
| docs/reports/github-action-ref-pinning-audit.md | report | active | active | audit | OPT-INF-002 | 2026-09-30 |  | not_decision_relevant | 0 | 4 | 4 |  |  |
| docs/reports/github-actions-node24-readiness.md | report | active | active | audit | OPT-CI-005 | 2026-09-29 |  | not_decision_relevant | 2 | 4 | 5 |  |  |
| docs/reports/inwx-zone-reconciliation-plan.md | report | deprecated | archived | planning | DEPLOY-DNS-001 |  |  | deprecated | 1 | 5 | 4 | review_after |  |
| docs/reports/kubernetes-platform-foundation-status.md | status | active |  |  | WELTGEWEBE-OS-006 | 2026-08-16 |  | not_decision_relevant | 1 | 5 | 7 | lifecycle, lifecycle_state |  |
| docs/reports/map-architekturkritik.md | report | deprecated | archived | audit | DOCMETA-REPORT-LIFECYCLE-001 | 2026-09-29 |  | deprecated | 4 | 5 | 2 |  |  |
| docs/reports/map-basemap-proof-gap-reconciliation.md | report | deprecated | archived | audit | DOCMETA-REPORT-LIFECYCLE-001 | 2026-09-29 |  | deprecated | 2 | 6 | 6 |  |  |
| docs/reports/map-status-matrix.md | status-matrix | deprecated | archived |  |  |  |  | deprecated | 8 | 6 | 3 | lifecycle, owner_task, review_after |  |
| docs/reports/map-status.md | report | active | active | audit | DOCMETA-REPORT-LIFECYCLE-001 | 2026-08-12 |  | not_decision_relevant | 4 | 6 | 2 |  |  |
| docs/reports/optimierungsbericht.md | report | active | active | audit | DOCMETA-REPORT-LIFECYCLE-001 | 2026-09-29 |  | not_decision_relevant | 3 | 4 | 4 |  |  |
| docs/reports/optimierungsstatus.md | status-matrix | active |  |  |  |  |  | not_decision_relevant | 25 | 5 | 4 | lifecycle, owner_task, review_after, lifecycle_state |  |
| docs/reports/passkey-register-verify-prep.md | report | deprecated | archived | decision-prep | AUTH-PG-002 |  |  | deprecated | 1 | 5 | 4 | review_after |  |
| docs/reports/planning-registration-findings.md | report | deprecated | archived | audit | TASK-CTL-005 |  |  | deprecated | 1 | 5 | 2 | review_after |  |
| docs/reports/proof-matrix-generalization-decision.md | report | active | active | decision | DOCMETA-PROOF-001 | 2026-09-29 |  | not_decision_relevant | 1 | 4 | 6 |  |  |
| docs/reports/repo-audit-2026-07-02.md | report | active | active | audit | REPO-AUDIT-001 | 2026-10-31 |  | not_decision_relevant | 1 | 4 | 4 |  |  |
| docs/reports/report-lifecycle-restbestand-triage.md | report | deprecated | archived | audit | DOCMETA-REPORT-LIFECYCLE-001 |  |  | deprecated | 0 | 5 | 3 | review_after |  |
| docs/reports/weltgewebe-os-foundation-status.md | status | active |  |  | WELTGEWEBE-OS-001 | 2026-08-15 |  | not_decision_relevant | 2 | 4 | 6 | lifecycle, lifecycle_state |  |
| docs/reports/weltgewebe-os-v1-t018-conversation-convergence-plan.md | report | active | active | planning | WELTGEWEBE-OS-001 | 2026-10-20 |  | not_decision_relevant | 0 | 4 | 7 |  |  |

## Absent Core Lifecycle Metadata

| Path | Absent fields |
| --- | --- |
| docs/reports/agent-readiness-audit.md | lifecycle, owner_task, review_after, lifecycle_state |
| docs/reports/auth-persistence-direct-proof-diagnose-audit.md | review_after |
| docs/reports/auth-persistence-next-step.md | review_after |
| docs/reports/auth-persistence-readiness.md | review_after |
| docs/reports/auth-persistence-runtime-proof.md | review_after |
| docs/reports/auth-persistence-runtime-target-reconciliation.md | review_after |
| docs/reports/auth-status-matrix.md | lifecycle, owner_task, review_after, lifecycle_state |
| docs/reports/domain-account-email-uniqueness-audit.md | review_after |
| docs/reports/domain-account-write-path-proof.md | review_after |
| docs/reports/domain-backfill-proof.md | review_after |
| docs/reports/domain-edge-create-semantics-preflight.md | review_after |
| docs/reports/domain-edge-reference-audit.md | review_after |
| docs/reports/domain-node-write-path-proof.md | review_after |
| docs/reports/domain-read-path-proof.md | review_after |
| docs/reports/inwx-zone-reconciliation-plan.md | review_after |
| docs/reports/kubernetes-platform-foundation-status.md | lifecycle, lifecycle_state |
| docs/reports/map-status-matrix.md | lifecycle, owner_task, review_after |
| docs/reports/optimierungsstatus.md | lifecycle, owner_task, review_after, lifecycle_state |
| docs/reports/passkey-register-verify-prep.md | review_after |
| docs/reports/planning-registration-findings.md | review_after |
| docs/reports/report-lifecycle-restbestand-triage.md | review_after |
| docs/reports/weltgewebe-os-foundation-status.md | lifecycle, lifecycle_state |

## Relations

| Path | Count | Types | Targets |
| --- | ---: | --- | --- |
| docs/reports/agent-readiness-audit.md | 1 | relates_to | docs/policies/agent-reading-protocol.md |
| docs/reports/auth-persistence-direct-proof-diagnose-audit.md | 4 | relates_to | docs/adr/ADR-0007__auth-persistence-production-db-path.md, docs/proofs/sqlx-postgres-direct-session-crud-proof.md, docs/reports/auth-persistence-next-step.md, docs/reports/auth-persistence-readiness.md |
| docs/reports/auth-persistence-next-step.md | 4 | relates_to, supersedes | docs/adr/ADR-0006__auth-magic-link-session-passkey.md, docs/blueprints/auth-roadmap.md, docs/reports/auth-persistence-readiness.md, docs/specs/auth-api.md |
| docs/reports/auth-persistence-readiness.md | 3 | relates_to | docs/adr/ADR-0006__auth-magic-link-session-passkey.md, docs/blueprints/auth-roadmap.md, docs/specs/auth-api.md |
| docs/reports/auth-persistence-runtime-proof.md | 6 | relates_to | docs/adr/ADR-0006__auth-magic-link-session-passkey.md, docs/adr/ADR-0007__auth-persistence-production-db-path.md, docs/blueprints/auth-persistence-runtime-proof.md, docs/blueprints/auth-roadmap.md, docs/proofs/sqlx-postgres-direct-session-crud-proof.md, docs/reports/auth-persistence-next-step.md |
| docs/reports/auth-persistence-runtime-target-reconciliation.md | 5 | relates_to | docs/adr/ADR-0007__auth-persistence-production-db-path.md, docs/blueprints/auth-persistence-runtime-proof.md, docs/proofs/sqlx-pgbouncer-session-crud-proof.md, docs/reports/auth-persistence-runtime-proof.md, docs/roadmap.md |
| docs/reports/auth-pg-002-controlled-preflight.md | 2 | depends_on, relates_to | docs/reports/auth-pg-002-runtime-schema-readiness-heimserver-2026-07-01.md, docs/reports/auth-status-matrix.md |
| docs/reports/auth-pg-002-cutover-plan.md | 4 | relates_to | docs/adr/ADR-0007__auth-persistence-production-db-path.md, docs/reports/auth-pg-002-passkey-db-store.md, docs/reports/auth-pg-002-passkey-runtime-facade.md, docs/reports/auth-status-matrix.md |
| docs/reports/auth-pg-002-passkey-db-store.md | 3 | relates_to | docs/adr/ADR-0007__auth-persistence-production-db-path.md, docs/reports/auth-status-matrix.md, docs/reports/passkey-register-verify-prep.md |
| docs/reports/auth-pg-002-passkey-fk-readiness-audit.md | 4 | relates_to | docs/reports/auth-pg-002-cutover-plan.md, docs/reports/auth-pg-002-passkey-db-store.md, docs/reports/auth-pg-002-passkey-runtime-facade.md, docs/reports/auth-status-matrix.md |
| docs/reports/auth-pg-002-passkey-runtime-audit-heimserver-2026-07-01.md | 4 | relates_to | docs/reports/auth-pg-002-cutover-plan.md, docs/reports/auth-pg-002-passkey-fk-readiness-audit.md, docs/reports/auth-pg-002-passkey-runtime-audit-plan.md, docs/reports/auth-status-matrix.md |
| docs/reports/auth-pg-002-passkey-runtime-audit-plan.md | 3 | relates_to | docs/reports/auth-pg-002-cutover-plan.md, docs/reports/auth-pg-002-passkey-fk-readiness-audit.md, docs/reports/auth-status-matrix.md |
| docs/reports/auth-pg-002-passkey-runtime-facade.md | 3 | relates_to | docs/adr/ADR-0007__auth-persistence-production-db-path.md, docs/reports/auth-pg-002-passkey-db-store.md, docs/reports/auth-status-matrix.md |
| docs/reports/auth-pg-002-runtime-schema-readiness-heimserver-2026-07-01.md | 4 | relates_to | docs/reports/auth-pg-002-cutover-plan.md, docs/reports/auth-pg-002-passkey-runtime-audit-heimserver-2026-07-01.md, docs/reports/auth-pg-002-passkey-runtime-audit-plan.md, docs/reports/auth-status-matrix.md |
| docs/reports/auth-pg-002-schema-preflight-ci.md | 4 | depends_on, relates_to | docs/reports/auth-pg-002-controlled-preflight.md, docs/reports/auth-pg-002-cutover-plan.md, docs/reports/auth-pg-002-runtime-schema-readiness-heimserver-2026-07-01.md, docs/reports/auth-status-matrix.md |
| docs/reports/auth-pg-003-backfill-readiness.md | 4 | relates_to | docs/reports/auth-pg-002-cutover-plan.md, docs/reports/auth-pg-002-passkey-runtime-facade.md, docs/reports/auth-status-matrix.md, docs/reports/opt-arc-001-db-proof-matrix.json |
| docs/reports/auth-pg-003-runtime-audit-heimserver-2026-07-01.md | 4 | relates_to | docs/reports/auth-pg-002-passkey-runtime-audit-heimserver-2026-07-01.md, docs/reports/auth-pg-002-runtime-schema-readiness-heimserver-2026-07-01.md, docs/reports/auth-pg-003-backfill-readiness.md, docs/reports/auth-pg-003-runtime-audit-wg-pg-proof-2026-07-01.md |
| docs/reports/auth-pg-003-runtime-audit-wg-pg-proof-2026-07-01.md | 2 | relates_to | docs/reports/auth-pg-002-runtime-schema-readiness-heimserver-2026-07-01.md, docs/reports/auth-pg-003-backfill-readiness.md |
| docs/reports/auth-status-matrix.md | 3 | relates_to | docs/adr/ADR-0006__auth-magic-link-session-passkey.md, docs/adr/ADR-0007__auth-persistence-production-db-path.md, docs/blueprints/auth-roadmap.md |
| docs/reports/cost-report.md | 3 | relates_to | .github/workflows/cost-report.yml, tools/py/cost/model.csv, tools/py/cost/report.py |
| docs/reports/cq-02-domain-projection-load.md | 9 | relates_to | .github/workflows/domain-projection-load.yml, apps/api/src/domain_db.rs, apps/api/src/middleware/domain_projection.rs, apps/api/src/routes/nodes.rs, apps/api/src/state.rs, apps/api/tests/db_domain_node_write_path.rs, docs/reports/domain-postgres-instance-coherence-decision.md, scripts/performance/domain_projection_k6.js, scripts/performance/domain_projection_load.py |
| docs/reports/domain-account-email-uniqueness-audit.md | 4 | relates_to | apps/api/src/auth/accounts.rs, apps/api/src/routes/accounts.rs, docs/blueprints/domain-data-postgres-cutover.md, scripts/docmeta/audit_account_email_uniqueness.py |
| docs/reports/domain-account-write-path-proof.md | 6 | relates_to | .github/workflows/api.yml, apps/api/tests/db_domain_account_write_path.rs, docs/blueprints/domain-data-postgres-cutover.md, docs/reports/domain-read-path-proof.md, docs/reports/optimierungsstatus.md, docs/tasks/board.md |
| docs/reports/domain-backfill-proof.md | 4 | relates_to | .github/workflows/api.yml, apps/api/tests/db_domain_backfill.rs, docs/blueprints/domain-data-postgres-cutover.md, docs/tasks/index.json |
| docs/reports/domain-edge-cache-limit-design.md | 5 | relates_to | apps/api/src/domain_db.rs, apps/api/src/routes/edges.rs, docs/blueprints/domain-data-postgres-cutover.md, docs/tasks/board.md, docs/tasks/index.json |
| docs/reports/domain-edge-create-semantics-preflight.md | 7 | relates_to | contracts/domain/edge.schema.json, docs/blueprints/domain-data-postgres-cutover.md, docs/reports/domain-account-write-path-proof.md, docs/reports/domain-node-write-path-proof.md, docs/reports/opt-arc-001-db-proof-matrix.json, docs/tasks/board.md, docs/tasks/index.json |
| docs/reports/domain-edge-faden-lifecycle-proof.md | 3 | relates_to, supersedes | contracts/domain/edge.schema.json, docs/reports/domain-edge-write-path-proof.md, docs/specs/garnrolle-knoten-faden.md |
| docs/reports/domain-edge-reference-audit.md | 6 | relates_to | apps/api/migrations/20260531000002_create_domain_edges.up.sql, contracts/domain/edge.schema.json, docs/blueprints/domain-data-postgres-cutover.md, docs/reports/opt-arc-001-db-proof-matrix.json, docs/tasks/board.md, scripts/docmeta/audit_domain_edge_references.py |
| docs/reports/domain-edge-write-path-proof.md | 8 | relates_to, supersedes | docs/blueprints/domain-data-postgres-cutover.md, docs/reports/domain-account-write-path-proof.md, docs/reports/domain-edge-create-semantics-preflight.md, docs/reports/domain-node-write-path-proof.md, docs/reports/domain-read-path-proof.md, docs/reports/optimierungsstatus.md, docs/tasks/board.md, docs/tasks/index.json |
| docs/reports/domain-node-write-path-proof.md | 6 | relates_to | docs/blueprints/domain-data-postgres-cutover.md, docs/reports/domain-account-write-path-proof.md, docs/reports/domain-read-path-proof.md, docs/reports/optimierungsstatus.md, docs/tasks/board.md, docs/tasks/index.json |
| docs/reports/domain-postgres-instance-coherence-decision.md | 13 | relates_to | apps/api/migrations/20260716000001_multi_instance_foundation.up.sql, apps/api/src/auth/ephemeral_db.rs, apps/api/src/middleware/domain_projection.rs, apps/api/src/outbox.rs, apps/api/src/routes/nodes.rs, apps/api/src/state.rs, apps/api/tests/db_multi_instance_foundation.rs, docs/blueprints/domain-data-postgres-cutover.md, docs/reports/cq-02-domain-projection-load.md, docs/tasks/board.md, docs/tasks/index.json, scripts/guard/domain-multi-instance-guard.sh, scripts/tests/test_domain_multi_instance_guard.sh |
| docs/reports/domain-provider-role-finding.md | 3 | relates_to | docs/deploy/domain-mail-migration-ionos-to-inwx-mailbox-brevo.md, docs/runbooks/domain-mail-cutover.md, docs/tasks/board.md |
| docs/reports/domain-read-path-proof.md | 5 | relates_to | docs/blueprints/domain-data-postgres-cutover.md, docs/reports/domain-account-write-path-proof.md, docs/reports/domain-backfill-proof.md, docs/reports/optimierungsstatus.md, docs/tasks/board.md |
| docs/reports/domain-runtime-data-source-reconciliation.md | 5 | relates_to | docs/blueprints/domain-data-postgres-cutover.md, docs/reports/domain-edge-reference-audit.md, docs/reports/opt-arc-001-db-proof-matrix.json, docs/tasks/board.md, docs/tasks/index.json |
| docs/reports/garnrolle-identity-cutover-proof.md | 4 | relates_to | apps/api/migrations/20260724000001_remove_ron_legacy.up.sql, docs/datenmodell.md, docs/domain/vocabulary.md, scripts/ci/tests/test_garnrolle_ontology_contract.py |
| docs/reports/github-action-ref-pinning-audit.md | 4 | relates_to | docs/tasks/board.md, docs/tasks/index.json, scripts/ci/check_github_action_pinning.py, scripts/ci/tests/test_check_github_action_pinning.py |
| docs/reports/github-actions-node24-readiness.md | 5 | relates_to | .github/workflows/opt-arc-001-db-proof-matrix.yml, docs/tasks/board.md, docs/tasks/index.json, scripts/ci/check_actions_node24_readiness.py, scripts/ci/tests/test_check_actions_node24_readiness.py |
| docs/reports/inwx-zone-reconciliation-plan.md | 4 | relates_to | docs/deploy/domain-mail-migration-ionos-to-inwx-mailbox-brevo.md, docs/reports/domain-provider-role-finding.md, docs/runbooks/domain-mail-cutover.md, docs/tasks/board.md |
| docs/reports/kubernetes-platform-foundation-status.md | 7 | depends_on, relates_to, verifies | docs/adr/ADR-0010__kubernetes-kanonische-plattform.md, docs/reports/domain-postgres-instance-coherence-decision.md, docs/tasks/board.md, platform/README.md, scripts/platform/ha_reference.py, scripts/platform/kind_reference.py, scripts/platform/validate_platform.py |
| docs/reports/map-architekturkritik.md | 2 | relates_to | docs/blueprints/kartenklarheit-roadmap.md, docs/reports/map-status-matrix.md |
| docs/reports/map-basemap-proof-gap-reconciliation.md | 6 | relates_to | .github/workflows/basemap-runtime-proof.yml, docs/blueprints/kartenklarheit-phase6.md, docs/blueprints/kartenklarheit-roadmap.md, docs/proofs/basemap-hamburg-artifact-proof.md, docs/reports/map-status-matrix.md, scripts/guard/basemap-runtime-proof.sh |
| docs/reports/map-status-matrix.md | 3 | relates_to | docs/blueprints/kartenklarheit-roadmap.md, docs/blueprints/ui-interaction-doctrine.md, docs/reports/map-architekturkritik.md |
| docs/reports/map-status.md | 2 | supersedes, verifies | docs/reports/map-status-matrix.md, docs/specs/map-experience.md |
| docs/reports/optimierungsbericht.md | 4 | relates_to | docs/datenmodell.md, docs/policies/agent-reading-protocol.md, docs/reports/optimierungsstatus.md, docs/techstack.md |
| docs/reports/optimierungsstatus.md | 4 | depends_on, relates_to | docs/policies/agent-reading-protocol.md, docs/reports/auth-persistence-readiness.md, docs/reports/domain-read-path-proof.md, docs/reports/optimierungsbericht.md |
| docs/reports/passkey-register-verify-prep.md | 4 | relates_to | docs/adr/ADR-0006__auth-magic-link-session-passkey.md, docs/blueprints/auth-roadmap.md, docs/reports/auth-status-matrix.md, docs/specs/auth-api.md |
| docs/reports/planning-registration-findings.md | 2 | relates_to | docs/tasks/index.json, scripts/docmeta/check_planning_registration.py |
| docs/reports/proof-matrix-generalization-decision.md | 6 | relates_to | .github/workflows/opt-arc-001-db-proof-matrix.yml, docs/reports/opt-arc-001-db-proof-matrix.json, docs/tasks/board.md, docs/tasks/index.json, scripts/docmeta/tests/test_validate_opt_arc_001_db_proof_matrix.py, scripts/docmeta/validate_opt_arc_001_db_proof_matrix.py |
| docs/reports/repo-audit-2026-07-02.md | 4 | relates_to | docs/policies/agent-reading-protocol.md, docs/policies/architecture-critique.md, docs/reports/optimierungsstatus.md, docs/tasks/board.md |
| docs/reports/report-lifecycle-restbestand-triage.md | 3 | relates_to | docs/process/report-lifecycle.md, docs/tasks/index.json, scripts/docmeta/validate_report_lifecycle.py |
| docs/reports/weltgewebe-os-foundation-status.md | 6 | depends_on, relates_to | architecture/weltgewebe-os.md, docs/blueprints/weltgewebe-os-masterplan.md, docs/proofs/weltgewebe-os-v1-t032-federation-delivery.md, docs/reports/domain-postgres-instance-coherence-decision.md, docs/reports/kubernetes-platform-foundation-status.md, docs/tasks/board.md |
| docs/reports/weltgewebe-os-v1-t018-conversation-convergence-plan.md | 7 | relates_to | apps/api/src/governance.rs, apps/api/src/routes/conversations.rs, apps/web/src/lib/components/governance/ProposalDetail.svelte, contracts/domain/conversation.schema.json, contracts/domain/message.schema.json, docs/datenmodell.md, docs/specs/governance-antraege.md |

## Primary Referenced Reports

- `docs/reports/agent-readiness-audit.md`
  - `docs/blueprints/agent-operability-blaupause.md`
  - `docs/blueprints/blueprint-agent-safety-control-layer.md`
  - `docs/proofs/weltgewebe-os-v1-t036-documentation-drift-reconciliation.md`

- `docs/reports/auth-persistence-next-step.md`
  - `docs/blueprints/auth-persistence-runtime-proof.md`
  - `docs/proofs/sqlx-pgbouncer-session-crud-proof.md`
  - `docs/reports/auth-persistence-direct-proof-diagnose-audit.md`
  - `docs/reports/auth-persistence-runtime-proof.md`
  - `docs/reports/passkey-register-verify-prep.md`

- `docs/reports/auth-persistence-readiness.md`
  - `docs/blueprints/auth-persistence-runtime-proof.md`
  - `docs/proofs/weltgewebe-os-v1-t036-documentation-drift-reconciliation.md`
  - `docs/reports/auth-persistence-direct-proof-diagnose-audit.md`
  - `docs/reports/auth-persistence-next-step.md`
  - `docs/reports/optimierungsstatus.md`

- `docs/reports/auth-persistence-runtime-proof.md`
  - `docs/adr/ADR-0007__auth-persistence-production-db-path.md`
  - `docs/proofs/sqlx-pgbouncer-session-crud-proof.md`
  - `docs/proofs/sqlx-postgres-direct-session-crud-proof.md`
  - `docs/reports/auth-persistence-runtime-target-reconciliation.md`

- `docs/reports/auth-persistence-runtime-target-reconciliation.md`
  - `docs/adr/ADR-0007__auth-persistence-production-db-path.md`
  - `docs/proofs/weltgewebe-os-v1-t036-documentation-drift-reconciliation.md`

- `docs/reports/auth-pg-002-controlled-preflight.md`
  - `docs/reports/auth-pg-002-schema-preflight-ci.md`

- `docs/reports/auth-pg-002-cutover-plan.md`
  - `docs/reports/auth-pg-002-passkey-fk-readiness-audit.md`
  - `docs/reports/auth-pg-002-passkey-runtime-audit-heimserver-2026-07-01.md`
  - `docs/reports/auth-pg-002-passkey-runtime-audit-plan.md`
  - `docs/reports/auth-pg-002-passkey-runtime-facade.md`
  - `docs/reports/auth-pg-002-runtime-schema-readiness-heimserver-2026-07-01.md`
  - `docs/reports/auth-pg-002-schema-preflight-ci.md`
  - `docs/reports/auth-pg-003-backfill-readiness.md`

- `docs/reports/auth-pg-002-passkey-db-store.md`
  - `docs/proofs/weltgewebe-os-v1-t036-documentation-drift-reconciliation.md`
  - `docs/reports/auth-pg-002-cutover-plan.md`
  - `docs/reports/auth-pg-002-passkey-fk-readiness-audit.md`
  - `docs/reports/auth-pg-002-passkey-runtime-facade.md`

- `docs/reports/auth-pg-002-passkey-fk-readiness-audit.md`
  - `docs/reports/auth-pg-002-passkey-runtime-audit-heimserver-2026-07-01.md`
  - `docs/reports/auth-pg-002-passkey-runtime-audit-plan.md`

- `docs/reports/auth-pg-002-passkey-runtime-audit-heimserver-2026-07-01.md`
  - `docs/reports/auth-pg-002-runtime-schema-readiness-heimserver-2026-07-01.md`
  - `docs/reports/auth-pg-003-runtime-audit-heimserver-2026-07-01.md`

- `docs/reports/auth-pg-002-passkey-runtime-audit-plan.md`
  - `docs/reports/auth-pg-002-passkey-runtime-audit-heimserver-2026-07-01.md`
  - `docs/reports/auth-pg-002-runtime-schema-readiness-heimserver-2026-07-01.md`

- `docs/reports/auth-pg-002-passkey-runtime-facade.md`
  - `docs/reports/auth-pg-002-cutover-plan.md`
  - `docs/reports/auth-pg-002-passkey-db-store.md`
  - `docs/reports/auth-pg-002-passkey-fk-readiness-audit.md`
  - `docs/reports/auth-pg-003-backfill-readiness.md`

- `docs/reports/auth-pg-002-runtime-schema-readiness-heimserver-2026-07-01.md`
  - `docs/reports/auth-pg-002-controlled-preflight.md`
  - `docs/reports/auth-pg-002-schema-preflight-ci.md`
  - `docs/reports/auth-pg-003-runtime-audit-heimserver-2026-07-01.md`
  - `docs/reports/auth-pg-003-runtime-audit-wg-pg-proof-2026-07-01.md`

- `docs/reports/auth-pg-003-backfill-readiness.md`
  - `docs/reports/auth-pg-003-runtime-audit-heimserver-2026-07-01.md`
  - `docs/reports/auth-pg-003-runtime-audit-wg-pg-proof-2026-07-01.md`
  - `docs/tasks/board.md`

- `docs/reports/auth-pg-003-runtime-audit-heimserver-2026-07-01.md`
  - `docs/reports/auth-pg-003-backfill-readiness.md`

- `docs/reports/auth-pg-003-runtime-audit-wg-pg-proof-2026-07-01.md`
  - `docs/reports/auth-pg-003-runtime-audit-heimserver-2026-07-01.md`

- `docs/reports/auth-status-matrix.md`
  - `docs/adr/ADR-0006__auth-magic-link-session-passkey.md`
  - `docs/blueprints/auth-roadmap.md`
  - `docs/proofs/weltgewebe-os-v1-t036-documentation-drift-reconciliation.md`
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
  - `docs/reports/optimierungsstatus.md`
  - `docs/reports/passkey-register-verify-prep.md`
  - `docs/roadmap.md`

- `docs/reports/cost-report.md`
  - `docs/tasks/board.md`

- `docs/reports/cq-02-domain-projection-load.md`
  - `docs/reports/domain-postgres-instance-coherence-decision.md`

- `docs/reports/domain-account-email-uniqueness-audit.md`
  - `docs/proofs/weltgewebe-os-v1-t036-documentation-drift-reconciliation.md`
  - `docs/reports/domain-backfill-proof.md`

- `docs/reports/domain-account-write-path-proof.md`
  - `docs/blueprints/domain-data-postgres-cutover.md`
  - `docs/proofs/weltgewebe-os-v1-t036-documentation-drift-reconciliation.md`
  - `docs/reports/domain-edge-create-semantics-preflight.md`
  - `docs/reports/domain-edge-write-path-proof.md`
  - `docs/reports/domain-node-write-path-proof.md`
  - `docs/reports/domain-read-path-proof.md`

- `docs/reports/domain-backfill-proof.md`
  - `docs/proofs/weltgewebe-os-v1-t036-documentation-drift-reconciliation.md`
  - `docs/reports/domain-read-path-proof.md`

- `docs/reports/domain-edge-create-semantics-preflight.md`
  - `docs/reports/domain-edge-write-path-proof.md`

- `docs/reports/domain-edge-faden-lifecycle-proof.md`
  - `docs/reports/domain-edge-write-path-proof.md`

- `docs/reports/domain-edge-reference-audit.md`
  - `docs/proofs/weltgewebe-os-v1-t036-documentation-drift-reconciliation.md`
  - `docs/reports/domain-runtime-data-source-reconciliation.md`
  - `docs/tasks/board.md`

- `docs/reports/domain-edge-write-path-proof.md`
  - `docs/proofs/weltgewebe-os-v1-t036-documentation-drift-reconciliation.md`
  - `docs/reports/domain-edge-create-semantics-preflight.md`
  - `docs/reports/domain-edge-faden-lifecycle-proof.md`

- `docs/reports/domain-node-write-path-proof.md`
  - `docs/blueprints/domain-data-postgres-cutover.md`
  - `docs/proofs/weltgewebe-os-v1-t036-documentation-drift-reconciliation.md`
  - `docs/reports/domain-edge-create-semantics-preflight.md`
  - `docs/reports/domain-edge-write-path-proof.md`

- `docs/reports/domain-postgres-instance-coherence-decision.md`
  - `docs/adr/ADR-0010__kubernetes-kanonische-plattform.md`
  - `docs/adr/ADR-0012__ereignisrueckgrat-transactional-outbox.md`
  - `docs/blueprints/domain-data-postgres-cutover.md`
  - `docs/reports/cq-02-domain-projection-load.md`
  - `docs/reports/kubernetes-platform-foundation-status.md`
  - `docs/reports/weltgewebe-os-foundation-status.md`
  - `docs/tasks/board.md`

- `docs/reports/domain-provider-role-finding.md`
  - `docs/deploy/secondary-domain-web-surfaces.md`
  - `docs/reports/inwx-zone-reconciliation-plan.md`
  - `docs/runbooks/domain-mail-cutover.md`
  - `docs/tasks/board.md`

- `docs/reports/domain-read-path-proof.md`
  - `docs/proofs/weltgewebe-os-v1-t036-documentation-drift-reconciliation.md`
  - `docs/reports/domain-account-write-path-proof.md`
  - `docs/reports/domain-edge-write-path-proof.md`
  - `docs/reports/domain-node-write-path-proof.md`
  - `docs/reports/optimierungsstatus.md`

- `docs/reports/domain-runtime-data-source-reconciliation.md`
  - `docs/tasks/board.md`

- `docs/reports/garnrolle-identity-cutover-proof.md`
  - `docs/domain/vocabulary.md`

- `docs/reports/github-actions-node24-readiness.md`
  - `docs/reports/optimierungsstatus.md`
  - `docs/tasks/board.md`

- `docs/reports/inwx-zone-reconciliation-plan.md`
  - `docs/tasks/DEPLOY-DNS-001B.md`

- `docs/reports/kubernetes-platform-foundation-status.md`
  - `docs/reports/weltgewebe-os-foundation-status.md`

- `docs/reports/map-architekturkritik.md`
  - `docs/blueprints/kartenklarheit-phase6.md`
  - `docs/blueprints/kartenklarheit-roadmap.md`
  - `docs/blueprints/kartenklarheit.md`
  - `docs/reports/map-status-matrix.md`

- `docs/reports/map-basemap-proof-gap-reconciliation.md`
  - `docs/blueprints/kartenklarheit-roadmap.md`
  - `docs/reports/map-status-matrix.md`

- `docs/reports/map-status-matrix.md`
  - `docs/blueprints/kartenklarheit-phase6.md`
  - `docs/blueprints/kartenklarheit-roadmap.md`
  - `docs/blueprints/kartenklarheit.md`
  - `docs/blueprints/map-roadmap.md`
  - `docs/blueprints/ui-interaction-doctrine.md`
  - `docs/reports/map-architekturkritik.md`
  - `docs/reports/map-basemap-proof-gap-reconciliation.md`
  - `docs/reports/map-status.md`

- `docs/reports/map-status.md`
  - `docs/reports/map-status-matrix.md`
  - `docs/reports/optimierungsstatus.md`
  - `docs/roadmap.md`
  - `docs/tasks/board.md`

- `docs/reports/optimierungsbericht.md`
  - `docs/blueprints/domain-data-postgres-cutover.md`
  - `docs/proofs/weltgewebe-os-v1-t036-documentation-drift-reconciliation.md`
  - `docs/reports/optimierungsstatus.md`

- `docs/reports/optimierungsstatus.md`
  - `docs/blueprints/auth-persistence-runtime-proof.md`
  - `docs/blueprints/doc-structure-task-control-examples.md`
  - `docs/blueprints/doc-structure-task-control-roadmap.md`
  - `docs/blueprints/doc-structure-task-control.md`
  - `docs/blueprints/domain-data-postgres-cutover.md`
  - `docs/process/ci-workflow-composition.md`
  - `docs/process/report-lifecycle-contract-alignment.md`
  - `docs/process/report-lifecycle.md`
  - `docs/proofs/weltgewebe-os-v1-t036-documentation-drift-reconciliation.md`
  - `docs/reports/auth-persistence-direct-proof-diagnose-audit.md`
  - `docs/reports/auth-persistence-next-step.md`
  - `docs/reports/auth-persistence-readiness.md`
  - `docs/reports/auth-persistence-runtime-proof.md`
  - `docs/reports/domain-account-write-path-proof.md`
  - `docs/reports/domain-edge-create-semantics-preflight.md`
  - `docs/reports/domain-edge-write-path-proof.md`
  - `docs/reports/domain-node-write-path-proof.md`
  - `docs/reports/domain-read-path-proof.md`
  - `docs/reports/optimierungsbericht.md`
  - `docs/reports/repo-audit-2026-07-02.md`
  - `docs/reports/report-lifecycle-restbestand-triage.md`
  - `docs/roadmap.md`
  - `docs/specs/list-pagination-api.md`
  - `docs/tasks/README.md`
  - `docs/tasks/board.md`

- `docs/reports/passkey-register-verify-prep.md`
  - `docs/reports/auth-pg-002-passkey-db-store.md`

- `docs/reports/planning-registration-findings.md`
  - `docs/tasks/board.md`

- `docs/reports/proof-matrix-generalization-decision.md`
  - `docs/tasks/board.md`

- `docs/reports/repo-audit-2026-07-02.md`
  - `docs/tasks/board.md`

- `docs/reports/weltgewebe-os-foundation-status.md`
  - `docs/blueprints/weltgewebe-os-masterplan.md`
  - `docs/roadmap.md`

## Derived Referenced Reports

- `docs/reports/agent-readiness-audit.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`

- `docs/reports/auth-persistence-direct-proof-diagnose-audit.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`
  - `docs/_generated/staleness-report.md`

- `docs/reports/auth-persistence-next-step.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`
  - `docs/_generated/staleness-report.md`
  - `docs/_generated/supersession-map.md`

- `docs/reports/auth-persistence-readiness.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`
  - `docs/_generated/staleness-report.md`
  - `docs/_generated/supersession-map.md`

- `docs/reports/auth-persistence-runtime-proof.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`
  - `docs/_generated/staleness-report.md`

- `docs/reports/auth-persistence-runtime-target-reconciliation.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`
  - `docs/_generated/staleness-report.md`

- `docs/reports/auth-pg-002-controlled-preflight.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`

- `docs/reports/auth-pg-002-cutover-plan.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`

- `docs/reports/auth-pg-002-passkey-db-store.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`

- `docs/reports/auth-pg-002-passkey-fk-readiness-audit.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`

- `docs/reports/auth-pg-002-passkey-runtime-audit-heimserver-2026-07-01.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`

- `docs/reports/auth-pg-002-passkey-runtime-audit-plan.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`

- `docs/reports/auth-pg-002-passkey-runtime-facade.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`

- `docs/reports/auth-pg-002-runtime-schema-readiness-heimserver-2026-07-01.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`

- `docs/reports/auth-pg-002-schema-preflight-ci.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`

- `docs/reports/auth-pg-003-backfill-readiness.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`

- `docs/reports/auth-pg-003-runtime-audit-heimserver-2026-07-01.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`

- `docs/reports/auth-pg-003-runtime-audit-wg-pg-proof-2026-07-01.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`

- `docs/reports/auth-status-matrix.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/relations-analysis.md`
  - `docs/_generated/report-lifecycle.md`

- `docs/reports/cost-report.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`

- `docs/reports/cq-02-domain-projection-load.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/relations-analysis.md`
  - `docs/_generated/report-lifecycle.md`

- `docs/reports/domain-account-email-uniqueness-audit.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`
  - `docs/_generated/staleness-report.md`

- `docs/reports/domain-account-write-path-proof.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`
  - `docs/_generated/staleness-report.md`

- `docs/reports/domain-backfill-proof.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`
  - `docs/_generated/staleness-report.md`

- `docs/reports/domain-edge-cache-limit-design.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`

- `docs/reports/domain-edge-create-semantics-preflight.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`
  - `docs/_generated/staleness-report.md`
  - `docs/_generated/supersession-map.md`

- `docs/reports/domain-edge-faden-lifecycle-proof.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`
  - `docs/_generated/supersession-map.md`

- `docs/reports/domain-edge-reference-audit.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`
  - `docs/_generated/staleness-report.md`

- `docs/reports/domain-edge-write-path-proof.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/relations-analysis.md`
  - `docs/_generated/report-lifecycle.md`
  - `docs/_generated/staleness-report.md`
  - `docs/_generated/supersession-map.md`

- `docs/reports/domain-node-write-path-proof.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`
  - `docs/_generated/staleness-report.md`

- `docs/reports/domain-postgres-instance-coherence-decision.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/impl-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/relations-analysis.md`
  - `docs/_generated/report-lifecycle.md`

- `docs/reports/domain-provider-role-finding.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`

- `docs/reports/domain-read-path-proof.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`
  - `docs/_generated/staleness-report.md`

- `docs/reports/domain-runtime-data-source-reconciliation.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`

- `docs/reports/garnrolle-identity-cutover-proof.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`

- `docs/reports/github-action-ref-pinning-audit.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`

- `docs/reports/github-actions-node24-readiness.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`

- `docs/reports/inwx-zone-reconciliation-plan.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`
  - `docs/_generated/staleness-report.md`

- `docs/reports/kubernetes-platform-foundation-status.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/impl-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`

- `docs/reports/map-architekturkritik.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`
  - `docs/_generated/staleness-report.md`

- `docs/reports/map-basemap-proof-gap-reconciliation.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/impl-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`
  - `docs/_generated/staleness-report.md`

- `docs/reports/map-status-matrix.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`
  - `docs/_generated/staleness-report.md`
  - `docs/_generated/supersession-map.md`

- `docs/reports/map-status.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/claim-evidence-map.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/impl-index.md`
  - `docs/_generated/report-lifecycle.md`
  - `docs/_generated/supersession-map.md`

- `docs/reports/optimierungsbericht.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`

- `docs/reports/optimierungsstatus.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/relations-analysis.md`
  - `docs/_generated/report-lifecycle.md`

- `docs/reports/passkey-register-verify-prep.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`
  - `docs/_generated/staleness-report.md`

- `docs/reports/planning-registration-findings.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`
  - `docs/_generated/staleness-report.md`

- `docs/reports/proof-matrix-generalization-decision.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`

- `docs/reports/repo-audit-2026-07-02.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`

- `docs/reports/report-lifecycle-restbestand-triage.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`
  - `docs/_generated/staleness-report.md`

- `docs/reports/weltgewebe-os-foundation-status.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`

- `docs/reports/weltgewebe-os-v1-t018-conversation-convergence-plan.md`
  - `docs/_generated/backlinks.md`
  - `docs/_generated/doc-index.md`
  - `docs/_generated/relates-to-audit.md`
  - `docs/_generated/report-lifecycle.md`

## Primary Unreferenced Reports

- `docs/reports/auth-persistence-direct-proof-diagnose-audit.md`
- `docs/reports/auth-pg-002-schema-preflight-ci.md`
- `docs/reports/domain-edge-cache-limit-design.md`
- `docs/reports/github-action-ref-pinning-audit.md`
- `docs/reports/report-lifecycle-restbestand-triage.md`
- `docs/reports/weltgewebe-os-v1-t018-conversation-convergence-plan.md`

## Supersession Target Diagnostics

None.

## Parse Warnings

None.


## Truth Contract Migration

| State | Count |
| --- | ---: |
| migrated | 0 |
| deprecated | 20 |
| not_decision_relevant | 32 |

## Machine-readable truth contract

Schema: `contracts/audit-report-truth.schema.json`

```json audit-report-truth.v1
{
  "coverage": {
    "checked_items": 71,
    "complete": true,
    "failures": 0,
    "fresh": true,
    "method": "exact",
    "scope": "all Markdown files under docs/reports and all generated/curated control surfaces declared in .wgx/generated-artifacts.yml",
    "total_items": 71
  },
  "does_not_establish": [
    "The correctness of claims inside individual reports, runtime use of declared consumers, or deployment truth."
  ],
  "generated_at": "2026-09-10T17:45:24+02:00",
  "limitations": [
    "The inventory evaluates repository metadata, exact path references and declared control contracts, not runtime behaviour."
  ],
  "schema_version": 1,
  "source_revision": "a7e9e07dd34552620d99fe2404e3d59a44b90b46",
  "sources": [
    {
      "path": ".wgx/generated-artifacts.yml",
      "sha256": "8191345252da31384a8ee1318ffce25741ae352a27a8ddde7775a8763564b63f"
    },
    {
      "path": "docs/reports/agent-readiness-audit.md",
      "sha256": "a2470245a3a062f9383207285855cf856e3990a28c947354d34ce2795f5cd1c8"
    },
    {
      "path": "docs/reports/auth-persistence-direct-proof-diagnose-audit.md",
      "sha256": "902506a78b6aa86553fd96b679478b574fc41f8caa41a303c7c0e4139ca5a8a5"
    },
    {
      "path": "docs/reports/auth-persistence-next-step.md",
      "sha256": "b0a13b6ca831871d931a01b42a34ee31d52e1cd164e2052e71fdeafced9463e1"
    },
    {
      "path": "docs/reports/auth-persistence-readiness.md",
      "sha256": "61d1fcca65a84897e931ea6477c337b8558548411a9fff1e9df64f9d3c15b9be"
    },
    {
      "path": "docs/reports/auth-persistence-runtime-proof.md",
      "sha256": "4a14e37f487df4557353f1b43b8420904edcbc15bc9cf4904b0739e1fc5baefe"
    },
    {
      "path": "docs/reports/auth-persistence-runtime-target-reconciliation.md",
      "sha256": "e9f045e7ad1b184f2967299fa9d36cca7c78efe432df46ad0c44436224a60501"
    },
    {
      "path": "docs/reports/auth-pg-002-controlled-preflight.md",
      "sha256": "8d7a8b8ce2e627e36b2f9cff90f491edd66cfd139b15f486aef569f1177b1de2"
    },
    {
      "path": "docs/reports/auth-pg-002-cutover-plan.md",
      "sha256": "c3ddc2dd4ce75d8002055000734c3d0ac09d44e87b8b879885989a5472a0637c"
    },
    {
      "path": "docs/reports/auth-pg-002-passkey-db-store.md",
      "sha256": "7ae19e8461d418be4d2a3286bee6ad0090266714a735ac0d0a936e62204f8fde"
    },
    {
      "path": "docs/reports/auth-pg-002-passkey-fk-readiness-audit.md",
      "sha256": "f8395159704acfd5d2e99c813bef576c5e2362c90d4f5d428d2bcaf5f3befb53"
    },
    {
      "path": "docs/reports/auth-pg-002-passkey-runtime-audit-heimserver-2026-07-01.md",
      "sha256": "ab2e43a161e5f4767d01f5e3c825499589281a2f81e33e5c5d2c2cc7168f29d7"
    },
    {
      "path": "docs/reports/auth-pg-002-passkey-runtime-audit-plan.md",
      "sha256": "ad93a25cccd370ae6aa97c6177f1edabb6493f8ae7c5ec8f45715d3b67731fbb"
    },
    {
      "path": "docs/reports/auth-pg-002-passkey-runtime-facade.md",
      "sha256": "b33db73904c3e5d3e6a8309bb219b8694e5a5a69008a9b1d69c039eb48c6aeb4"
    },
    {
      "path": "docs/reports/auth-pg-002-runtime-schema-readiness-heimserver-2026-07-01.md",
      "sha256": "f624cab838c31e73daf6145d409a488824985497570c1191c6c093eb28c8e071"
    },
    {
      "path": "docs/reports/auth-pg-002-schema-preflight-ci.md",
      "sha256": "39300bf511336b1613c80cc96dce3ddfc361d9285604af573040d69d43d5c1d9"
    },
    {
      "path": "docs/reports/auth-pg-003-backfill-readiness.md",
      "sha256": "5a1c8cda64819229c8d1d302a42fad5f907b370bb44c8d7271f62825babf8c15"
    },
    {
      "path": "docs/reports/auth-pg-003-runtime-audit-heimserver-2026-07-01.md",
      "sha256": "524949b350495befb87a1dca9dd8c4349b86aaf7e25ac5732c1a93d5548e9f8b"
    },
    {
      "path": "docs/reports/auth-pg-003-runtime-audit-wg-pg-proof-2026-07-01.md",
      "sha256": "ecb5bb03ae690bedaade52b0656ae042180b06a6edca83bc7fa486af83b15c61"
    },
    {
      "path": "docs/reports/auth-status-matrix.md",
      "sha256": "d11caeeff96963286a9619900ee6194e7342f37cb809c556140c448c1cee2a82"
    },
    {
      "path": "docs/reports/cost-report.md",
      "sha256": "af871bbceb436e1380e43699e8b1800ebcb7b539d74087451fc7dff41bcdf882"
    },
    {
      "path": "docs/reports/cq-02-domain-projection-load.md",
      "sha256": "0736195917f3e9e874a058a1cc53e28a62dfa2792b760cd855ad36d01527168a"
    },
    {
      "path": "docs/reports/domain-account-email-uniqueness-audit.md",
      "sha256": "5c0fdc3f0a8c6d84cd1f1c15d54026c9ed31c5a238a40c0d9bd6b17db90d857f"
    },
    {
      "path": "docs/reports/domain-account-write-path-proof.md",
      "sha256": "3833e752a31d37acca9caad2b20fc250a3b0352051d01024cdc7da484718e161"
    },
    {
      "path": "docs/reports/domain-backfill-proof.md",
      "sha256": "ad8f3913b7494616951c9943562b47c7f29cdbb3039a48fd3eb6f4e4962184e3"
    },
    {
      "path": "docs/reports/domain-edge-cache-limit-design.md",
      "sha256": "2282d518631cfc82724604c2ed6390df21815c7533680aa4dd4796c17a495d88"
    },
    {
      "path": "docs/reports/domain-edge-create-semantics-preflight.md",
      "sha256": "80406c9107d3609c773a2263d393d3296ce330f27cb6a6fe131d3f8998d426c0"
    },
    {
      "path": "docs/reports/domain-edge-faden-lifecycle-proof.md",
      "sha256": "a2ae3a25b53c85d74caed62843651c59e856c80cd2eaf429c3ef35750fcee39a"
    },
    {
      "path": "docs/reports/domain-edge-reference-audit.md",
      "sha256": "dbc8ce7112f7dee29def077858481e4d5338815aac2c835162a5ffa2bd70cae1"
    },
    {
      "path": "docs/reports/domain-edge-write-path-proof.md",
      "sha256": "d96e6873141dc6d48406b651b2396646f6ab8880ce97bad17e582b32a41a60bb"
    },
    {
      "path": "docs/reports/domain-node-write-path-proof.md",
      "sha256": "c8bfaa8fbcd6bf1593c06c577624409e1543f348f82d33033666f55e6646fad3"
    },
    {
      "path": "docs/reports/domain-postgres-instance-coherence-decision.md",
      "sha256": "5604d7e0f413d63a66cd71b3b2a43447b119abf0a57351211c7acd479af39642"
    },
    {
      "path": "docs/reports/domain-provider-role-finding.md",
      "sha256": "25cf909e20b225666332cb8a780000ac4dd908db5286eefa585c607399c67c17"
    },
    {
      "path": "docs/reports/domain-read-path-proof.md",
      "sha256": "e251d03d97b54f77ad62cefcfa5113c49b403b5dc661e49180f212ca1911b96d"
    },
    {
      "path": "docs/reports/domain-runtime-data-source-reconciliation.md",
      "sha256": "4ab4f957fbe574c925058d81ea96ffd35bd0d991f4e09035c1a4775270d3b016"
    },
    {
      "path": "docs/reports/garnrolle-identity-cutover-proof.md",
      "sha256": "4a369b7e178aefc5c3ea0121f325da4de07d39cc209e1326716ebcbe2cc51ea3"
    },
    {
      "path": "docs/reports/github-action-ref-pinning-audit.md",
      "sha256": "ec0ee760ff9f924b1daeeb9c94b1d976ab945abe51f5046f4520ad2225b42f14"
    },
    {
      "path": "docs/reports/github-actions-node24-readiness.md",
      "sha256": "4a1966c7e66cae1d4eb1161919ac6d1ec58f618e0be8b713381ca798626e90a1"
    },
    {
      "path": "docs/reports/inwx-zone-reconciliation-plan.md",
      "sha256": "924e9d5e0eb451bfce1d46c89dd5019a10e3f0673c43bf7e42d6b48d38e489c7"
    },
    {
      "path": "docs/reports/kubernetes-platform-foundation-status.md",
      "sha256": "4377b5d352139c75a53668fc868c430af5ccd1224a610387bed730ace62fc722"
    },
    {
      "path": "docs/reports/map-architekturkritik.md",
      "sha256": "3608a68c30e1a93be8f69a92cb24146789755d877a64afd2e1462cb441ee44e3"
    },
    {
      "path": "docs/reports/map-basemap-proof-gap-reconciliation.md",
      "sha256": "a3e95aaaebc6879052b449340a3eae49f15a8619a0b485f4284802a9b63eb30c"
    },
    {
      "path": "docs/reports/map-status-matrix.md",
      "sha256": "d704493e09e76de5e5ae0663ab75672213dc9e1ed24adf270603815363d23848"
    },
    {
      "path": "docs/reports/map-status.md",
      "sha256": "24a4b5778d7ef870cfa898fc7c114da8721ddb98de85ab0c7e2b32f671a89c46"
    },
    {
      "path": "docs/reports/optimierungsbericht.md",
      "sha256": "e55e2e9e676d05e49e3373f9d47b675c61c837afb234aa72b57885f2b9e16830"
    },
    {
      "path": "docs/reports/optimierungsstatus.md",
      "sha256": "a90079fee67d6351835cc04e337c655a9bb7a81675ecb15aea9a92bf89571142"
    },
    {
      "path": "docs/reports/passkey-register-verify-prep.md",
      "sha256": "c6ab2043919bbb18abf6bc49a5b5d134fd740ee8a849933f340a2fd8c0966dfe"
    },
    {
      "path": "docs/reports/planning-registration-findings.md",
      "sha256": "e16bbf8edd9ba1d35643bf3f1239dd7dc2897455b091e237455742acb2bac352"
    },
    {
      "path": "docs/reports/proof-matrix-generalization-decision.md",
      "sha256": "ec3c2ebfa1e4e5f7db5e315f27eeeda65b4df5c64ec483433a8db10d38fe5b49"
    },
    {
      "path": "docs/reports/repo-audit-2026-07-02.md",
      "sha256": "9f10f83c58269467b46548201f3bb2328343aa444706900143573daa88cadd67"
    },
    {
      "path": "docs/reports/report-lifecycle-restbestand-triage.md",
      "sha256": "b817a468d72bae2b73fb45f5bca749e1ea8088d4d7303db0ef1a420580a72b87"
    },
    {
      "path": "docs/reports/weltgewebe-os-foundation-status.md",
      "sha256": "f3b21dd0dead470dbb09cc5838534c2fc32d22a85fe9cfe258464e33bb308653"
    },
    {
      "path": "docs/reports/weltgewebe-os-v1-t018-conversation-convergence-plan.md",
      "sha256": "680a44fa3507f4191fbe32b0068b3bf8a77e6979739ea7d88d5c4b07d43870a9"
    }
  ],
  "status": "no_material_drift"
}
```

## Generated Report Truth Contract Migration

| File | inventory_status | reason |
| --- | --- | --- |
| docs/_generated/agent-readiness.md | not_decision_relevant | generated output is descriptive and not used as a decision gate |
| docs/_generated/architecture-drift.md | not_decision_relevant | generated output is descriptive and not used as a decision gate |
| docs/_generated/backlinks.md | not_decision_relevant | generated output is descriptive and not used as a decision gate |
| docs/_generated/change-resonance.md | not_decision_relevant | generated output is descriptive and not used as a decision gate |
| docs/_generated/claim-evidence-map.md | not_decision_relevant | generated output is descriptive and not used as a decision gate |
| docs/_generated/doc-coverage.md | not_decision_relevant | generated output is descriptive and not used as a decision gate |
| docs/_generated/doc-index.md | not_decision_relevant | generated output is descriptive and not used as a decision gate |
| docs/_generated/impl-index.md | not_decision_relevant | generated output is descriptive and not used as a decision gate |
| docs/_generated/implicit-dependencies.md | not_decision_relevant | generated output is descriptive and not used as a decision gate |
| docs/_generated/knowledge-gaps.md | not_decision_relevant | generated output is descriptive and not used as a decision gate |
| docs/_generated/orphans.md | not_decision_relevant | generated output is descriptive and not used as a decision gate |
| docs/_generated/relates-to-audit.md | not_decision_relevant | generated output is descriptive and not used as a decision gate |
| docs/_generated/relations-analysis.md | not_decision_relevant | generated output is descriptive and not used as a decision gate |
| docs/_generated/report-lifecycle-inventory.md | migrated | valid audit-report truth contract |
| docs/_generated/report-lifecycle.md | migrated | valid audit-report truth contract |
| docs/_generated/staleness-report.md | not_decision_relevant | generated output is descriptive and not used as a decision gate |
| docs/_generated/supersession-map.md | not_decision_relevant | generated output is descriptive and not used as a decision gate |
| docs/_generated/system-map.md | not_decision_relevant | generated output is descriptive and not used as a decision gate |
