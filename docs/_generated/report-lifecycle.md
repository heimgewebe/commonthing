---
id: docs.generated.report-lifecycle
title: Report Lifecycle Overview
doc_type: generated
status: active
canonicality: derived
summary: Automatisch generierte Übersicht der Report-Lifecycle-Zustände.
---
# Report Lifecycle Overview

Generated automatically. Do not edit manually.

This overview is descriptive only. It surfaces lifecycle metadata and validator findings for planning; it is not a CI guard and not a policy judgement.

## Summary

| Metric | Count |
| --- | ---: |
| files_scanned | 52 |
| reports_checked | 46 |
| reports_ignored_non_report | 6 |
| reports_with_lifecycle_state | 46 |
| reports_missing_lifecycle_state | 0 |
| findings_total | 0 |

## Lifecycle State Summary

| lifecycle_state | Count |
| --- | ---: |
| active | 27 |
| deferred | 0 |
| superseded | 6 |
| archived | 13 |
| missing | 0 |

## Finding Summary

| Code | Count |
| --- | ---: |
| _None_ | 0 |

## Active Reports

| Report | status | lifecycle | owner_task | review_after | findings |
| --- | --- | --- | --- | --- | --- |
| docs/reports/auth-pg-002-controlled-preflight.md | active | planning | AUTH-PG-002 | 2026-09-30 |  |
| docs/reports/auth-pg-002-cutover-plan.md | active | planning | AUTH-PG-002 | 2026-09-30 |  |
| docs/reports/auth-pg-002-passkey-db-store.md | active | proof | AUTH-PG-002 | 2026-09-30 |  |
| docs/reports/auth-pg-002-passkey-fk-readiness-audit.md | active | proof | AUTH-PG-002 | 2026-09-30 |  |
| docs/reports/auth-pg-002-passkey-runtime-audit-heimserver-2026-07-01.md | active | proof | AUTH-PG-002 | 2026-09-30 |  |
| docs/reports/auth-pg-002-passkey-runtime-audit-plan.md | active | planning | AUTH-PG-002 | 2026-09-30 |  |
| docs/reports/auth-pg-002-passkey-runtime-facade.md | active | proof | AUTH-PG-002 | 2026-09-30 |  |
| docs/reports/auth-pg-002-runtime-schema-readiness-heimserver-2026-07-01.md | active | proof | AUTH-PG-002 | 2026-09-30 |  |
| docs/reports/auth-pg-002-schema-preflight-ci.md | active | proof | AUTH-PG-002 | 2026-09-30 |  |
| docs/reports/auth-pg-003-backfill-readiness.md | active | proof | AUTH-PG-003 | 2026-09-30 |  |
| docs/reports/auth-pg-003-runtime-audit-heimserver-2026-07-01.md | active | proof | AUTH-PG-003 | 2026-09-30 |  |
| docs/reports/auth-pg-003-runtime-audit-wg-pg-proof-2026-07-01.md | active | proof | AUTH-PG-003 | 2026-09-30 |  |
| docs/reports/cost-report.md | active | generated | DOCMETA-REPORT-LIFECYCLE-001 | 2026-09-29 |  |
| docs/reports/cq-02-domain-projection-load.md | active | proof | WELTGEWEBE-OS-002 | 2027-01-08 |  |
| docs/reports/domain-edge-cache-limit-design.md | active | decision-prep | DOMAIN-PG-003 | 2026-09-29 |  |
| docs/reports/domain-edge-faden-lifecycle-proof.md | active | proof | OPT-ARC-001 | 2026-10-17 |  |
| docs/reports/domain-postgres-instance-coherence-decision.md | active | audit | WELTGEWEBE-OS-002 | 2027-01-16 |  |
| docs/reports/domain-provider-role-finding.md | active | audit | DEPLOY-DNS-001 | 2026-07-23 |  |
| docs/reports/domain-runtime-data-source-reconciliation.md | active | audit | DB-PROOF-001 | 2026-07-18 |  |
| docs/reports/garnrolle-identity-cutover-proof.md | active | proof | OPT-ARC-001 | 2026-10-24 |  |
| docs/reports/github-action-ref-pinning-audit.md | active | audit | OPT-INF-002 | 2026-09-30 |  |
| docs/reports/github-actions-node24-readiness.md | active | audit | OPT-CI-005 | 2026-09-29 |  |
| docs/reports/map-status.md | active | audit | DOCMETA-REPORT-LIFECYCLE-001 | 2026-08-12 |  |
| docs/reports/optimierungsbericht.md | active | audit | DOCMETA-REPORT-LIFECYCLE-001 | 2026-09-29 |  |
| docs/reports/proof-matrix-generalization-decision.md | active | decision | DOCMETA-PROOF-001 | 2026-09-29 |  |
| docs/reports/repo-audit-2026-07-02.md | active | audit | REPO-AUDIT-001 | 2026-10-31 |  |
| docs/reports/weltgewebe-os-v1-t018-conversation-convergence-plan.md | active | planning | WELTGEWEBE-OS-001 | 2026-10-20 |  |

## Deferred Reports

| Report | status | lifecycle | owner_task | review_after | findings |
| --- | --- | --- | --- | --- | --- |
| _None_ | | | | | |

## Superseded Reports

| Report | status | lifecycle | owner_task | review_after | findings |
| --- | --- | --- | --- | --- | --- |
| docs/reports/auth-persistence-direct-proof-diagnose-audit.md | deprecated | audit | OPT-API-002 |  |  |
| docs/reports/auth-persistence-next-step.md | deprecated | decision-prep | OPT-API-002 |  |  |
| docs/reports/auth-persistence-readiness.md | deprecated | decision-prep | OPT-API-002 |  |  |
| docs/reports/auth-persistence-runtime-proof.md | deprecated | proof | OPT-API-002 |  |  |
| docs/reports/domain-edge-create-semantics-preflight.md | deprecated | decision-prep | OPT-ARC-001 |  |  |
| docs/reports/domain-edge-write-path-proof.md | deprecated | proof | OPT-ARC-001 | 2026-07-16 |  |

## Archived Reports

| Report | status | lifecycle | owner_task | review_after | findings |
| --- | --- | --- | --- | --- | --- |
| docs/reports/auth-persistence-runtime-target-reconciliation.md | deprecated | audit | OPT-API-002 |  |  |
| docs/reports/domain-account-email-uniqueness-audit.md | deprecated | audit | OPT-ARC-001 |  |  |
| docs/reports/domain-account-write-path-proof.md | deprecated | proof | OPT-ARC-001 |  |  |
| docs/reports/domain-backfill-proof.md | deprecated | proof | OPT-ARC-001 |  |  |
| docs/reports/domain-edge-reference-audit.md | deprecated | audit | OPT-ARC-001 |  |  |
| docs/reports/domain-node-write-path-proof.md | deprecated | proof | OPT-ARC-001 |  |  |
| docs/reports/domain-read-path-proof.md | deprecated | proof | OPT-ARC-001 |  |  |
| docs/reports/inwx-zone-reconciliation-plan.md | deprecated | planning | DEPLOY-DNS-001 |  |  |
| docs/reports/map-architekturkritik.md | deprecated | audit | DOCMETA-REPORT-LIFECYCLE-001 | 2026-09-29 |  |
| docs/reports/map-basemap-proof-gap-reconciliation.md | deprecated | audit | DOCMETA-REPORT-LIFECYCLE-001 | 2026-09-29 |  |
| docs/reports/passkey-register-verify-prep.md | deprecated | decision-prep | AUTH-PG-002 |  |  |
| docs/reports/planning-registration-findings.md | deprecated | audit | TASK-CTL-005 |  |  |
| docs/reports/report-lifecycle-restbestand-triage.md | deprecated | audit | DOCMETA-REPORT-LIFECYCLE-001 |  |  |

## Unclassified Reports

| Report | status | lifecycle | owner_task | review_after | findings |
| --- | --- | --- | --- | --- | --- |
| _None_ | | | | | |

## Reports With Findings

| Report | lifecycle_state | status | findings |
| --- | --- | --- | --- |
| _None_ | | | |

## Reports With Missing Currently-Enforced Fields

Fields required by the currently implemented validator rules that are absent, in rule-precedence order. This reflects field presence only; it is not a full normative lifecycle judgement and does not cover enum, date, owner, or relation checks. Future validator rules may surface additional requirements.

| Report | status | lifecycle_state | Missing currently-enforced fields |
| --- | --- | --- | --- |
| _None_ | | | |

## Non-Report Files Under docs/reports

| File | doc_type | status |
| --- | --- | --- |
| docs/reports/agent-readiness-audit.md | documentation | active |
| docs/reports/auth-status-matrix.md | reference | active |
| docs/reports/kubernetes-platform-foundation-status.md | status | active |
| docs/reports/map-status-matrix.md | status-matrix | deprecated |
| docs/reports/optimierungsstatus.md | status-matrix | active |
| docs/reports/weltgewebe-os-foundation-status.md | status | active |

## Machine-readable truth contract

Schema: `contracts/audit-report-truth.schema.json`

```json audit-report-truth.v1
{
  "coverage": {
    "checked_items": 52,
    "complete": true,
    "failures": 0,
    "fresh": true,
    "method": "exact",
    "scope": "all Markdown files discovered under docs/reports",
    "total_items": 52
  },
  "does_not_establish": [
    "Runtime health, deployment health, or the correctness of claims inside individual reports."
  ],
  "generated_at": "2026-09-09T12:28:41+02:00",
  "limitations": [
    "The report reflects repository files only and does not execute product runtime checks."
  ],
  "schema_version": 1,
  "source_revision": "30b4e3743a957c4f0e8538e6ffc69798093ddf67",
  "sources": [
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
      "sha256": "5cd092da660ed4fd695d7d687916ea5a959682a302c6acade72751a31d245964"
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
      "sha256": "745ed87d75b60c20da299cc4b1a982c24e49bef2c6ae4b650a3470c9c4862787"
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
  "status": "pass"
}
```
