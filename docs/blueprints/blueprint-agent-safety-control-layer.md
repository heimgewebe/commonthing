---
id: docs.blueprints.agent-safety-control-layer
title: "Blueprint — Agent Safety Control Layer"
doc_type: blueprint
status: draft
summary: "Vollausbau eines KI-narrensicheren Agent-/Evidence-Kontrollsystems für commonThing: Safety Preflight, Claim Evidence, Agent Contracts, Non-Ideal Guard, Dry-Run Runner, Run Evidence und gated Write Mode."
relations:
  - type: relates_to
    target: docs/blueprints/agent-operability-blaupause.md
  - type: relates_to
    target: docs/roadmap.md
  - type: relates_to
    target: repo.meta.yaml
  - type: relates_to
    target: AGENTS.md
  - type: relates_to
    target: agent-policy.yaml
  - type: relates_to
    target: docs/policies/agent-reading-protocol.md
  - type: relates_to
    target: docs/tasks/index.json
  - type: relates_to
    target: audit/impl-registry.yaml
  - type: relates_to
    target: docs/reports/agent-readiness-audit.md
---

# Blueprint — Agent Safety Control Layer

## 0. Dialektische Ausgangslage

### These

commonThing wird nahezu vollständig agentisch entwickelt. Deshalb reicht es nicht, Agents gute Hinweise zu geben. Das Repository muss so gebaut sein, dass Agents auch bei unvollständigem Kontext, zu breitem Auftrag, veralteter Roadmap oder fehlender Evidence nicht still falsch handeln können.

### Antithese

commonThing besitzt bereits starke Kontrollflächen: Docmeta, AGENTS-Policy, Task-Control, Statusmatrizen, Generated Reports, CI-Guards, Deploy-Snapshot und Drift-Guards. Ein weiterer Governance-Layer kann selbst zur Driftquelle werden, wenn er bestehende Strukturen ersetzt statt sie zu härten.

### Synthese

Der Agent Safety Control Layer ist kein paralleles Steuerungssystem. Er erweitert vorhandene Kontrollflächen um fehlende agentische Sicherheitsmechaniken:

- Pfad- und Scope-Sicherheit
- Claim-Evidence-Pflicht
- maschinenlesbare Agent-Contracts
- Non-Ideal-Task-Blockade
- Handoff-Validierung
- Dry-Run-Ausführung
- agentische Run-Evidence
- gated Write Mode
- Roadmap-/Blueprint-Ratchet

Kurz: commonThing wird nicht nur agentenfreundlich, sondern agentensicher.

### Quellenbasis

Interne Grundlage sind insbesondere `docs/reports/agent-readiness-audit.md` sowie `docs/blueprints/agent-operability-blaupause.md`. Externe bzw. repoübergreifende Vergleichsmuster aus LensKit und Vibe-Lab dienen als Inspirationsachsen, werden aber nicht als commonThing-Primärnorm behandelt.

Dieses Blueprint ist ein Ziel- und Planungsartefakt. Es ist nicht selbst Evidence dafür, dass die beschriebenen Mechaniken existieren.

## 1. Ziel

Dieses Blueprint definiert den Vollausbau eines Agent-Safety-Control-Layers für commonThing.

Ziel ist, dass jede agentische Änderung folgende Kette erfüllt:

```text
Task
→ erlaubte Pfade
→ Claims
→ Evidence
→ Validierung
→ Handoff
→ Run-Artefakte
→ CI/Statusentscheidung
```

Nicht der Agent entscheidet, ob etwas fertig ist. Der Agent erzeugt Evidence. Fertig wird etwas erst durch maschinenprüfbare Belege, CI, Statusmatrix und Review.

### 1.1 Status dieses Blueprints

Dieses Dokument beschreibt eine Zielarchitektur. Die im Wellenplan genannten Pfade, Skripte, Contracts, Workflows und Artefakte gelten erst dann als existierend oder bindend, wenn sie im Code beziehungsweise in CI implementiert und über Claim-/Evidence-Checks belegt sind.

Insbesondere dürfen Agents aus diesem Blueprint keine Implementierungsverfügbarkeit ableiten.

## 2. Nicht-Ziele

Dieses Blueprint verfolgt ausdrücklich nicht:

- keine zweite Task-Control-Schicht
- kein Big-Bang-Umbau
- kein sofortiger Write Mode
- keine vollständige Claim-Registry im ersten Schritt
- kein vollständiges RepoLens-Bundle
- keine Issue-Form-Kathedrale
- keine Execution-Proofs für Kleinkram
- kein Agent-pass bei Widerspruch
- kein Generated-Artefakt als Primärwahrheit

Die Kunst besteht nicht darin, das Repo mit Kontrolltafeln vollzustellen. Die Kunst besteht darin, Agents genau dort zu blockieren, wo falsche Produktivität entsteht.

## 3. Systeminvarianten

### 3.1 Kein done durch Agent

Ein Agent darf keinen finalen Status setzen.

Verboten:

```yaml
status: done
roadmap: [x]
blueprint: active
```

sofern nicht maschinenlesbare Evidence vorliegt.

Erlaubt:

```yaml
decision: pass_proposed
evidence: present
validation: pass
residual_gap: none
```

Finale Status entstehen nur durch:

- CI
- Claim-Evidence-Check
- Statusmatrix-Regel
- Review
- expliziten Proof

### 3.2 Kein Claim ohne Evidence

Jede handlungsleitende Aussage braucht Evidence.

Beispiele für Claims:

- Feature X ist implementiert.
- Guard Y erzwingt Z.
- Blueprint A ist active.
- Roadmap-Phase B ist done.
- Generated Report C ist aktuell.
- Agent Capability D ist verfügbar.

Zulässige Evidence:

- Codepfad
- Test
- CI-Workflow
- Proof-Dokument
- Runtime-Snapshot
- Run-Artefakt
- Decision-Artefakt

### 3.3 Kein Task ohne Scope

Jeder agentische Task braucht:

```yaml
task_id:
goal:
task_type:
allowed_paths:
forbidden_paths:
claims:
expected_evidence:
validation_commands:
delete_allowed: false
```

Fehlt eine dieser Kerninformationen, wird der Task blockiert.

### 3.4 Kein direkter Edit an Generated-Artefakten

`docs/_generated/*` ist Diagnose, Projektion oder Navigation. Es ist nie Primärwahrheit.

Direkte Änderungen an `docs/_generated/*` sind verboten. Änderungen müssen über Generatoren erfolgen.

### 3.5 Widerspruch blockiert

Bei widersprechenden Quellen gilt:

```text
stop
mark_contradiction
emit_evidence_gap
no_write
```

Der Agent darf keine stille Synthese erzeugen. Widerspruch ist ein Zustand, kein Schönheitsfehler.

## 4. Architekturüberblick

```text
repo.meta.yaml
AGENTS.md
agent-policy.yaml
docs/policies/agent-reading-protocol.md
        │
        ▼
docs/tasks/board.md ──→ docs/tasks/index.json
        │                    │
        ▼                    ▼
docs/claims/registry.yml ←→ audit/impl-registry.yaml
        │                    │
        ▼                    ▼
docs/_generated/claim-evidence-map.md
docs/_generated/impl-evidence.md/json
        │
        ▼
contracts/agent/*.schema.json
scripts/agent/check_non_ideal_task.py
scripts/docmeta/validate_agent_handoff.py
scripts/agent/run_task.py
        │
        ▼
artifacts/agent-runs/<run-id>/
        │
        ▼
CI Ratchet:
report-only → warn → blocking → fail-closed
```

## 5. Rollen der vorhandenen Kontrollflächen

### 5.1 Primärnorm

- `repo.meta.yaml`
- `AGENTS.md`
- `agent-policy.yaml`
- `docs/policies/agent-reading-protocol.md`
- `contracts/**`

Diese Artefakte definieren, was gilt.

### 5.2 Arbeitssteuerung

- `docs/tasks/board.md`
- `docs/tasks/index.json`
- `docs/tasks/schema.json`
- `.github/workflows/task-index.yml`

Diese Artefakte definieren, woran gearbeitet wird.

### 5.3 Evidence-Flächen

Bestehende Evidence-Flächen:

- `audit/impl-registry.yaml`
- `docs/proofs/**`
- CI-Workflows
- Runtime-/Deploy-Snapshots

Geplante Evidence-Flächen:

- `docs/claims/registry.yml` — erst nach PR 3 `evidence/minimal-claim-spine`
- `artifacts/agent-runs/**` — erst nach PR 7 `agent/run-evidence-lite`

Diese Artefakte definieren, was belegt ist. Geplante Evidence-Flächen dürfen bis zu ihrer Implementierung nicht als verfügbare Primärquelle gelesen werden.

### 5.4 Diagnose und Projektion

- `docs/_generated/**`

Diese Artefakte helfen beim Lesen, Entscheiden und Prüfen. Sie ersetzen keine Primärnorm.

## 6. Umsetzung als Wellenplan

Der Ausbau erfolgt nicht als Big Bang. Jede Welle muss eine konkrete Fehlerklasse blockieren oder sichtbar machen.

### Welle 1 — Sofortige Agent-Sicherheit

#### PR 1 — agent/safety-preflight

##### PR 1 — agent/safety-preflight: Zweck

Gefährliche Agentenfehler sofort blockieren, noch bevor ein vollständiger Agent-Runner existiert.

##### PR 1 — agent/safety-preflight: Neue oder geänderte Artefakte

- `docs/security/agent-write-scope-baseline.md`
- `scripts/agent/check_changed_paths_scope.py`
- `scripts/agent/check_generated_direct_edit.py`
- `scripts/agent/check_agent_status_claims.py`
- `scripts/agent/check_agent_preflight.py`

Optional:

- `.github/workflows/agent-safety-preflight.yml`

Bevorzugt wird aber die Integration in bestehende Guard-Strukturen, sofern das ohne Verrenkung möglich ist.

##### PR 1 — agent/safety-preflight: Regeln

1. `docs/_generated/*` darf nicht direkt editiert werden.
2. Roadmap-`[x]` braucht Claim- oder Proof-Referenz.
3. Statusmatrix `done` braucht `proof_ref`.
4. Changed paths müssen `allowed_paths` entsprechen.
5. Workflow-Dateien brauchen `task_type: ci_change`.
6. Infra-/Deploy-Dateien brauchen `task_type: infra_change` und `proof_ref`.
7. Delete braucht `delete_allowed: true`.
8. Agent darf kein `done` setzen.

##### PR 1 — agent/safety-preflight: Minimaler Agent-Marker

Zu Beginn genügt ein YAML-Block im PR-Body oder eine kleine Task-Datei:

```yaml
task_id: WG-TASK-...
task_type: doc_change
allowed_paths:
  - docs/...
claims:
  - claim....
validation:
  - make ci-validate
delete_allowed: false
```

##### PR 1 — agent/safety-preflight: Ratchet

- Stufe 1: report-only
- Stufe 2: warn
- Stufe 3: blocking für:
  - direct edit in `docs/_generated`
  - Roadmap-`[x]` ohne Claim/Proof
  - Status `done` ohne `proof_ref`
  - forbidden paths

##### PR 1 — agent/safety-preflight: Akzeptanzkriterien

- Ein Patch mit direktem Edit an `docs/_generated/*` wird erkannt.
- Ein Roadmap-Haken ohne Claim/Proof wird erkannt.
- Ein Statusmatrix-`done` ohne `proof_ref` wird erkannt.
- Ein Pfad außerhalb `allowed_paths` wird erkannt.
- Alle Checks liefern maschinenlesbare Fehlercodes.

##### PR 1 — agent/safety-preflight: Fehlercodes

- `GENERATED_DIRECT_EDIT`
- `ROADMAP_DONE_WITHOUT_CLAIM`
- `STATUS_DONE_WITHOUT_PROOF`
- `PATH_OUT_OF_SCOPE`
- `WORKFLOW_CHANGE_WITHOUT_TASK_TYPE`
- `INFRA_CHANGE_WITHOUT_PROOF`
- `DELETE_WITHOUT_PERMISSION`

#### PR 2 — agent/readiness-hard-fail

##### PR 2 — agent/readiness-hard-fail: Zweck

Agent-Readiness darf keine Reife suggerieren, solange Kernmechaniken fehlen.

##### PR 2 — agent/readiness-hard-fail: Artefakte

- `scripts/docmeta/generate_agent_readiness.py`
- `docs/_generated/agent-readiness.md`
- `docs/_generated/agent-readiness.json`

##### PR 2 — agent/readiness-hard-fail: Capability-Matrix

| Capability | Statusregel |
|---|---|
| Agent Policy | `pass`, wenn `AGENTS.md` und `agent-policy.yaml` vorhanden |
| Claim Evidence | `open`, solange minimale Claim-Registry fehlt |
| Command Contracts | `open`, solange `contracts/agent/*.schema.json` fehlt |
| Handoff Validator | `open`, solange Validator fehlt |
| Non-Ideal Guard | `open`, solange Guard fehlt |
| Dry-Run Runner | `open`, solange Runner fehlt |
| Run Evidence | `open`, solange Run-Artefakte fehlen |
| Overall | maximal `partial`, solange eine Hard-Capability fehlt |

##### PR 2 — agent/readiness-hard-fail: Akzeptanzkriterien

- Gesamtstatus kann nicht `pass` sein, solange Contracts, Handoff-Validator oder Runner fehlen.
- Report nennt fehlende Artefakte konkret.
- Report unterscheidet `open`, `partial`, `pass`, `fail`.
- Report bleibt Diagnose und markiert sich selbst als nicht-kanonisch.

### Welle 2 — Evidence Backbone

#### PR 3 — evidence/minimal-claim-spine

##### PR 3 — evidence/minimal-claim-spine: Zweck

Handlungsleitende Claims werden maschinenlesbar und beweispflichtig.

##### PR 3 — evidence/minimal-claim-spine: Artefakte

- `docs/claims/registry.yml`
- `docs/claims/schema.json`
- `scripts/docmeta/check_claim_evidence.py`
- `docs/_generated/claim-evidence-map.md`
- `docs/_generated/claim-evidence.json`

##### PR 3 — evidence/minimal-claim-spine: Start-Claim-Typen

```yaml
claim_type:
  - roadmap_done
  - agent_capability_available
  - generated_artifact_current
  - ci_enforces
  - security_guard_enforced
```

##### PR 3 — evidence/minimal-claim-spine: Beispiel

```yaml
- id: claim.agent.safety_preflight.enforced
  claim_type: security_guard_enforced
  source:
    document: docs/roadmap.md
    section: agent-operability
  status: verified
  required_level: ci
  evidence:
    files:
      - scripts/agent/check_changed_paths_scope.py
      - scripts/agent/check_generated_direct_edit.py
      - scripts/agent/check_agent_status_claims.py
    workflows:
      - .github/workflows/agent-safety-preflight.yml
```

##### PR 3 — evidence/minimal-claim-spine: Statuswerte

- `open`
- `partial`
- `verified`
- `stale`
- `contradicted`

##### PR 3 — evidence/minimal-claim-spine: Akzeptanzkriterien

- Mindestens zehn handlungsleitende Claims sind modelliert.
- Roadmap-`[x]` in Agent-/CI-/Generated-/Guard-Bereichen braucht Claim-Eintrag.
- Fehlende Evidence wird als `missing` oder `partial` berichtet.
- Widersprüchliche Evidence erzeugt `contradicted`.
- Initialer Modus: report-only.

#### PR 4 — evidence/impl-registry-critical

##### PR 4 — evidence/impl-registry-critical: Zweck

Die vorhandene `audit/impl-registry.yaml` wird für kritische Pfade evidence-fähig gemacht.

##### PR 4 — evidence/impl-registry-critical: Startumfang

- `impl.workflow.ci`
- `impl.contracts`
- `impl.service.api`
- `impl.service.web`
- `impl.infra.compose`
- `impl.agent.safety-preflight`

##### PR 4 — evidence/impl-registry-critical: Schema-Erweiterung

```yaml
id: impl.workflow.ci
path:
  - .github/workflows/
criticality: high
documented_by:
  - docs/...
verified_by:
  - .github/workflows/agent-safety-preflight.yml
claim_refs:
  - claim.agent.safety_preflight.enforced
evidence_level: ci
last_verified: "2026-06-01"
```

##### PR 4 — evidence/impl-registry-critical: Regeln

- Impl-Registry ist Evidence-Quelle, nicht Claim-Wahrheit.
- Claims dürfen auf Impl-Entries verweisen.
- Impl-Entries dürfen nicht automatisch Roadmap-Status setzen.
- `criticality: high` ohne `verified_by` wird zunächst warn, später fail.

##### PR 4 — evidence/impl-registry-critical: Akzeptanzkriterien

Alle kritischen Einträge haben:

- `documented_by`
- `verified_by`
- `claim_refs`
- `evidence_level`

### Welle 3 — Agent-Vertrag

#### PR 5 — agent/minimal-contracts-and-guard

##### PR 5 — agent/minimal-contracts-and-guard: Zweck

Agent-Tasks erhalten ein maschinenlesbares Minimalformat, und unklare Tasks werden blockiert.

Contracts ohne Guard sind zu weich. Guard ohne Contracts ist zu schwammig. Diese PR verbindet beides.

##### PR 5 — agent/minimal-contracts-and-guard: Artefakte

- `contracts/agent/task.schema.json`
- `contracts/agent/command.read_context.schema.json`
- `contracts/agent/command.write_change.schema.json`
- `contracts/agent/command.validate_change.schema.json`
- `contracts/agent/handoff.schema.json`
- `scripts/agent/check_non_ideal_task.py`
- `scripts/docmeta/validate_agent_handoff.py`
- `tests/fixtures/agent/valid-doc-drift-task.json`
- `tests/fixtures/agent/valid-roadmap-claim-task.json`
- `tests/fixtures/agent/valid-generated-refresh-task.json`
- `tests/fixtures/agent/invalid-missing-scope.json`
- `tests/fixtures/agent/invalid-forbidden-path.json`
- `tests/fixtures/agent/invalid-status-done-by-agent.json`
- `docs/reference/agent-operability-fixture-matrix.md`

##### PR 5 — agent/minimal-contracts-and-guard: Task-Pflichtfelder

```yaml
task_id:
goal:
task_type:
allowed_paths:
forbidden_paths:
claims:
expected_evidence:
validation_commands:
delete_allowed: false
```

##### PR 5 — agent/minimal-contracts-and-guard: Command-Kette

```text
read_context
→ write_change
→ validate_change
→ emit_handoff
```

##### PR 5 — agent/minimal-contracts-and-guard: Non-Ideal-Fehlercodes

- `NO_ALLOWED_PATHS`
- `NO_VALIDATION_COMMAND`
- `NO_EXPECTED_EVIDENCE`
- `CLAIM_WITHOUT_REGISTRY_ENTRY`
- `ROADMAP_DONE_WITHOUT_PROOF`
- `FORBIDDEN_PATH`
- `SCOPE_TOO_BROAD`
- `STATUS_DONE_BY_AGENT`
- `CONTRADICTION_FOUND`

##### PR 5 — agent/minimal-contracts-and-guard: Akzeptanzkriterien

- Drei reale commonThing-Tasktypen sind valide modelliert.
- Drei gefährliche Negativfälle scheitern.
- Fixture-Matrix dokumentiert Coverage und bekannte Lücken.
- Ein Task ohne Scope oder Validation kann nicht pass werden.
- `blocked` gilt als korrektes Sicherheitsergebnis.

### Welle 4 — Ausführung ohne Schreibmacht

#### PR 6 — agent/dry-run-runner

##### PR 6 — agent/dry-run-runner: Zweck

Agenten können echte Tasks trocken ausführen, ohne Dateien zu ändern.

##### PR 6 — agent/dry-run-runner: Artefakte

- `scripts/agent/run_task.py`
- `.github/workflows/agent-operability-smoke.yml`

##### PR 6 — agent/dry-run-runner: Ablauf

```text
load task
validate schema
run non-ideal guard
read_context
prepare patch plan
validate expected evidence
emit handoff.json
emit run-result.json
```

##### PR 6 — agent/dry-run-runner: Standard

- `--dry-run` ist default.
- `--write` existiert noch nicht.

##### PR 6 — agent/dry-run-runner: Smoke-Test

```bash
python -m scripts.agent.run_task --dry-run --no-persist tests/fixtures/agent/valid-doc-drift-task.json
python -m scripts.agent.run_task --dry-run --no-persist tests/fixtures/agent/valid-roadmap-claim-task.json
python -m scripts.agent.run_task --dry-run --no-persist tests/fixtures/agent/valid-generated-refresh-task.json
```

##### PR 6 — agent/dry-run-runner: Akzeptanzkriterien

- Drei reale Tasktypen laufen deterministisch durch.
- Mit `--no-persist` werden weder Task-Zieldateien noch Evidence-Dateien erzeugt.
- Handoff wird erzeugt.
- Run-Result wird erzeugt.
- Ungültige Tasks werden vor Kontextlesen oder Patchplanung blockiert.

#### PR 7 — agent/run-evidence-lite

> **Umsetzungsstand 2026-06-27:** Der Lite-Slice ist als `AGENT-SAFE-007`
> mit PR #1265 auf `main` integriert und post-merge verifiziert. Persistiert
> werden nur erfolgreich geplante Dry-Runs. Universelle Failure-Evidence,
> externe Attestierung und Write Mode bleiben Folgearbeit.

##### PR 7 — agent/run-evidence-lite: Zweck

Jeder Dry-Run erzeugt minimale, schema-valide Evidence.

##### PR 7 — agent/run-evidence-lite: Artefakte

```text
artifacts/agent-runs/<run-id>/
  task.yml
  handoff.json
  validation.json
  run-result.json
```

##### PR 7 — agent/run-evidence-lite: Noch nicht enthalten

- `evidence.jsonl`
- `decision.yml`
- `changed-files.txt`

Diese kommen erst nach stabilem Runner.

##### PR 7 — agent/run-evidence-lite: Akzeptanzkriterien

- Jeder erfolgreich geplante persistierte Dry-Run erzeugt einen eindeutigen `run_id`.
- Jeder erfolgreich geplante persistierte Dry-Run schreibt schema-valide Run-Artefakte.
- Run-Artefakte enthalten Task-ID, Claims, Validierung und Ergebnis.
- Blockierte und betriebsfehlerhafte Runs bleiben außerhalb des Lite-Slices und werden nicht als erfolgreiches Evidence-Bundle ausgegeben.

### Welle 5 — Generated und Roadmap hart machen

#### PR 8 — docs/generated-control-minimal

> **Umsetzungsstand 2026-06-27:** `AGENT-SAFE-008` implementiert den
> Minimalvertrag im Branch `feat/agent-generated-control-minimal`.
> Der reale Claim-Map-Pfad ist `docs/_generated/claim-evidence-map.md`.
> `docs/tasks/index.json` ist ein kuratierter kanonischer Index und wird daher
> über Schema- und Cross-Artifact-Checks kontrolliert, nicht als Generator-Output.

##### PR 8 — docs/generated-control-minimal: Zweck

Die gefährlichsten Generated-Artefakte werden zuerst kontrolliert.

##### PR 8 — docs/generated-control-minimal: Startumfang

- `docs/_generated/agent-readiness.md`
- `docs/_generated/claim-evidence-map.md`
- `docs/tasks/index.json`

##### PR 8 — docs/generated-control-minimal: Artefakt

- `.wgx/generated-artifacts.yml`

##### PR 8 — docs/generated-control-minimal: Beispiel

```yaml
artifacts:
  docs/_generated/agent-readiness.md:
    role: diagnostic
    canonicality: derived
    generator: scripts/docmeta/generate_agent_readiness.py
    source:
      - contracts/agent/
      - scripts/agent/
      - docs/claims/registry.yml
    commit_required: true
    blocking: true
```

##### PR 8 — docs/generated-control-minimal: Akzeptanzkriterien

- Agent-Readiness und Claim-Evidence-Map dürfen nicht manuell editiert werden.
- Beide Derived-Artefakte sind aus deklarierten Quellen regenerierbar und schreibfrei prüfbar.
- Der kuratierte Task-Index bleibt kanonisch und muss Schema- sowie Cross-Artifact-Checks bestehen.
- Quellenänderungen erzeugen erwartete Regeneration oder einen blockierenden Driftbefund.
- Direkte Edits an Derived-Artefakten werden blockiert.

#### PR 9 — docs/roadmap-ratchet-minimal

##### PR 9 — docs/roadmap-ratchet-minimal: Zweck

Gefährliche Statuslügen werden blockiert.

##### PR 9 — docs/roadmap-ratchet-minimal: Regeln

- Roadmap-`[x]` in Agent-/CI-/Generated-Bereichen braucht verified Claim.
- Statusmatrix `done` braucht `proof_ref`.
- Blueprint `active` im Agent-Bereich braucht verified Claim-Gruppe.

##### PR 9 — docs/roadmap-ratchet-minimal: Artefakte

- `scripts/docmeta/check_roadmap_claims.py`
- `scripts/docmeta/check_statusmatrix_proofs.py`
- `scripts/docmeta/check_blueprint_activation.py`

##### PR 9 — docs/roadmap-ratchet-minimal: Akzeptanzkriterien

- Agent-Operability-Haken kann nicht gesetzt werden, solange Contracts, Guard oder Runner fehlen.
- `done` ohne `proof_ref` scheitert.
- Blueprint `active` ohne verified Claim-Gruppe scheitert.
- Modus startet warn, wird später blocking.

### Welle 6 — External Operator Execution (Migration & Delegation)

Der ursprünglich geplante generische repo-eigene `--write`-Runner wird **verworfen**.

**Architekturentscheidung:**

- **Das Repository** besitzt Contract, Validator und Prüfprofile (Validation-Profiles). Es entscheidet, *was* erlaubt ist und *wie* es geprüft wird.
- **Grabowski** (oder ein kompatibler externer Operator) besitzt Workspace, Lease, Mutation, Review, Veröffentlichung, Recovery und Cleanup.

Das Repository ist nicht der ausführende Agent, sondern die normative Definitions- und Validierungsschicht.

#### PR 10 — Migration zu External Operator Execution

##### Phasenweiser Migrationsplan

Der Übergang zur neuen Trennung von Repo-Validation und Extern-Execution erfordert einen strikten, phasenweisen Migrationsplan. Nichts darf gelöscht werden, bevor Consumer und Tests migriert sind.

1. **Paralleler Betrieb:** Der neue `agent-contract.json` Validator (`validate_repo_agent_contract.py`) und der bestehende Preflight laufen doppelt in CI (`agent-safety-preflight.yml`).
2. **YAML-Projektionen:** Die alten `agent-policy.yaml` und `repo.meta.yaml` werden als "Compatibility Projections" geführt. Der Contract-Validator prüft auf Drift.
3. **Ausführungsprofile:** Freie task-gesteuerte Shell-Kommandos (`validation_commands`) sind nur ein Übergangsformat. Sie besitzen künftig keine Ausführungsautorität mehr. Zukünftige Ausführungen müssen benannte, geprüfte Profile (`validation_profiles`) aus dem Contract nutzen.
4. **Rückbaukriterien:**
   - Der doppelte Preflight darf erst zurückgebaut werden, wenn alle Consumer auf den Strict-JSON Contract und die Profil-Validierung umgestellt haben und alle Paritätstests (`validate_projection_parity`) dauerhaft grün bleiben.
   - Der YAML-Minimalparser und manuelle Taskprojektionen werden erst entfernt, wenn Grabowski/Operator nachweislich den JSON-Contract nativ nutzt.

##### PR 10 — Migration: Akzeptanzkriterien

- Der Agent-Contract definiert explizit `repo_role: repo_contract_authority` und `operator_role: external_operator_execution`.
- Autoritätsdateien (`agent-contract.json`, `.wgx/generated-artifacts.yml`) sind ausdrücklich bewachte Schreibziele.
- Trusted Generators dürfen ausschließlich Ziele unter `generated_artifacts.allowed_target_prefixes` erzeugen; `secrets/` und `snapshots/` sind ausgeschlossen.
- `repo_generic_write_mode: rejected` ist manifestiert.
- Freie Shell-Kommandos sind als deprecated / Übergangsformat dokumentiert.
- Paralleler Betrieb ist durch Checks (z.B. Projection Drift) abgesichert.

#### PR 11 — run-evidence-full (Operator-Driven)

Vollständige Observability wird vom externen Operator getrieben.

##### Artefakte

```text
artifacts/agent-runs/<run-id>/
  task.yml
  handoff.json
  evidence.jsonl
  decision.yml
  validation.json
  changed-files.txt
  run-result.json
```

Der externe Operator schreibt die Artefakte und nutzt die repo-eigenen Profil-Validatoren, um den Handoff-Status auf `pass_proposed` zu setzen.

### Welle 7 — Skalierung

#### PR 12 — evidence/claim-spine-expand

Ausweitung auf:

- Auth
- Map/Basemap
- Deploy/Snapshot
- Docmeta
- Security

##### PR 12 — evidence/claim-spine-expand: Akzeptanzkriterium

Alle zentralen Roadmap-Haken in Kernbereichen haben Claim-Evidence.

#### PR 13 — docs/generated-control-full

Alle `docs/_generated/*` bekommen:

- `source`
- `generator`
- `role`
- `canonicality`
- `commit_required`
- `blocking`

##### PR 13 — docs/generated-control-full: Akzeptanzkriterium

Alle Generated-Artefakte sind klassifiziert, regenerierbar und gegen direkte Edits geschützt.

#### PR 14 — evidence/execution-proof-selective

Nur für riskante Klassen:

- Auth
- Sessions
- DB/Migration
- Deploy/Infra
- Agent write mode
- Basemap/Tile strategy
- Security workflows

Nicht für:

- Tippfehler
- kleine Doku-Fixes
- normale Statusupdates

##### PR 14 — evidence/execution-proof-selective: Artefakte

```text
experiments/<id>/
  manifest.yml
  method.md
  run_meta.json
  result.md
  evidence.jsonl
  decision.yml
```

##### PR 14 — evidence/execution-proof-selective: Akzeptanzkriterium

`adopted` ohne `run_meta.json`, Evidence und Decision ist unmöglich.

#### PR 15 — governance/traceability-light

Pflichtfelder für relevante Änderungen:

- Task-ID
- Claim(s)
- Allowed paths
- Changed paths
- Evidence
- Validation
- Residual gap

Ausnahme:

```yaml
no-task-required: true
reason: typo-only
```

##### PR 15 — governance/traceability-light: Akzeptanzkriterium

Kein relevanter PR ohne Task-/Claim-/Evidence-Verbindung.

#### PR 16 — docs/owner-staleness-later

Owner/Review-Intervalle für langlebige Roadmaps und Blueprints.

##### PR 16 — docs/owner-staleness-later: Frontmatter-Erweiterung

```yaml
owner: alex
review_interval_days: 45
last_reviewed: "2026-06-01"
next_review_due: "2026-07-16"
staleness_policy: warn
```

##### PR 16 — docs/owner-staleness-later: Akzeptanzkriterium

Keine aktive Roadmap oder Policy ohne Owner und Review-Intervall.

#### PR 17 — agent/handoff-validation-v1

##### PR 17 — agent/handoff-validation-v1: Zweck

Handoff-Validierung auf AJV-kompilierbare JSON-Schema-Assertions heben. Strict-JSON-Loading (doppelte Keys, NaN/Infinity), deterministische Validierung gegen Draft-07-Schemas, Digest-Freshness-Guard, AJV-Shell-Check und isolierter Smoke als Readiness-Gate.

##### PR 17 — agent/handoff-validation-v1: Neue Artefakte

- `scripts/agent/json_contract.py` — Strict JSON-Loader + deterministische Schema-Validation (kein AJV-Abhängigkeit für Python-Tests)
- `scripts/agent/validate_agent_contracts.py` — prüft task.schema.json und handoff.schema.json gegen die Repository-Fixtures
- `scripts/agent/update_handoff_fixtures.py` — hält task_contract_sha256-Felder in allen Digest-Fixtures aktuell
- `scripts/contracts-agent-check.sh` — AJV-Shell-Check für beide Schemas (optional in CI)
- `scripts/agent/tests/test_json_contract.py`
- `scripts/agent/tests/test_update_handoff_fixtures.py`
- `scripts/agent/tests/test_validate_agent_contracts.py`
- `docs/reference/agent-handoff-contract.md`

##### PR 17 — agent/handoff-validation-v1: Geänderte Artefakte

- `scripts/agent/validate_handoff.py` — nutzt `json_contract.load_json_strict`, delegiert Schema-Checks an `validate_instance`, prüft Evidence-Dateien, PATH_STATE_CONTRADICTION, VALIDATION_RESULT_DUPLICATE
- `scripts/agent/check_non_ideal_task.py` — nutzt `json_contract.validate_instance` statt manueller Feldprüfung; `load_claim_registry`-Alias für `validate_handoff`
- `scripts/docmeta/generate_agent_readiness.py` — `handoff_validation` Capability führt jetzt Datei-Präsenz + Subprocess-Smoke durch
- `contracts/agent/handoff.schema.json` — `producer` erhält `maxLength` + `pattern`; `validation_results` erhält `uniqueItems`
- `contracts/agent/task.schema.json` — alle Array-Felder erhalten `uniqueItems: true`
- `tests/fixtures/agent/handoff-task.json` — erweiterte `allowed_paths`, `expected_evidence`, `validation_commands`
- `tests/fixtures/agent/handoff-valid.json` — aktualisierter Digest + erweiterte Evidence

##### PR 17 — agent/handoff-validation-v1: Akzeptanzkriterien

- `python3 -m scripts.agent.validate_agent_contracts` gibt exit 0
- `python3 -m scripts.agent.validate_handoff --task-file tests/fixtures/agent/handoff-task.json --handoff-file tests/fixtures/agent/handoff-valid.json` gibt exit 0
- `python3 -m unittest discover scripts/agent/tests/ -v` ohne Fehler
- `python3 -m scripts.agent.update_handoff_fixtures --check` gibt exit 0
- `handoff_validation` in `docs/_generated/agent-readiness.md` zeigt `pass`

## 7. Ratchet-Strategie

Jeder neue Check durchläuft vier Stufen:

```text
report-only
→ warn
→ blocking
→ fail-closed
```

Regeln:

- Neue Checks starten nicht sofort fail-closed.
- Jeder Warnmodus braucht Ablaufdatum.
- Blocking beginnt zuerst bei sicherheitskritischen Agent-Fehlern.
- Fail-closed gilt nur, wenn False Positives niedrig sind.

## 8. Risiko- und Nutzenabschätzung

### Nutzen

| Nutzenklasse | Wirkung |
|---|---|
| weniger Scope-Drift | Agents können nur erlaubte Pfade ändern |
| weniger Roadmap-Drift | `[x]` braucht Evidence |
| weniger Scheinkohärenz | Widerspruch blockiert |
| bessere Reviews | Evidence liegt strukturiert vor |
| sicherere Writes | Write Mode erst nach Gates |
| bessere Agent-Diagnose | Readiness zeigt echte Lücken |
| reproduzierbare Agent-Arbeit | Run-Artefakte mit `run_id` |

### Risiken

| Risiko | Gegenmaßnahme |
|---|---|
| CI wird zu streng | Ratchet statt Big Bang |
| neue Artefakte erzeugen Pflegekosten | minimaler Start, Expansion später |
| Claim-Registry wird Doppelwahrheit | Claims verweisen auf Status, Impl, Proofs |
| Runner bleibt Spielzeug | drei echte Tasktypen als Pflicht |
| Generated-Control wird zu breit | Minimalumfang zuerst |
| Write Mode wird gefährlich | erst nach Safety, Contracts, Guard, Evidence |
| Agents werden oft blockiert | Blockade ist korrektes Sicherheitsergebnis |

## 9. Prämissencheck

Diese Blaupause gilt unter folgenden Prämissen:

1. commonThing wird überwiegend agentisch entwickelt.
2. Agentische Fehler sollen durch Repo-Mechanik, nicht durch Hoffnung, verhindert werden.
3. Bestehende Kontrollflächen bleiben führend.
4. Neue Artefakte dürfen keine Parallelwahrheit erzeugen.
5. CI darf stufenweise strenger werden.

Wenn Prämisse 1 fällt, wäre der Plan zu schwer. Wenn Prämisse 1 gilt, ist der Plan angemessen.

## 10. Alternative Sinnachse

Nicht fragen:

> Wie machen wir Agents produktiver?

Sondern:

> Wie machen wir falsche Agentenproduktivität unmöglich?

Das verschiebt die Architektur. Ziel ist nicht maximale Geschwindigkeit, sondern beweisfähige Geschwindigkeit.

## 11. Begriffsnotiz für Anfänger

### Agent

Ein Agent ist hier ein KI-gestützter Arbeiter, der Aufgaben im Repo liest, Änderungen vorbereitet und Validierungen ausführt.

### Claim

Ein Claim ist eine Behauptung, etwa: „Feature X ist implementiert“ oder „Guard Y erzwingt Z“.

### Evidence

Evidence ist der Beleg für einen Claim: Code, Test, CI, Proof oder Runtime-Snapshot.

### Handoff

Ein Handoff ist die Übergabeakte eines Agenten: Was war die Aufgabe, welche Dateien wurden betrachtet, was wurde geändert oder geplant, welche Evidence liegt vor?

### Non-Ideal Task

Ein Non-Ideal Task ist ein Auftrag, der zu unklar, zu breit oder unbelegt ist. Solche Aufgaben werden nicht improvisiert, sondern blockiert.

### Ratchet (Begriffsnotiz)

Ein Ratchet ist eine stufenweise Verschärfung: erst nur berichten, dann warnen, dann blockieren, dann fail-closed. Wie eine Ratsche: zurück geht es nicht still.

## 12. Metriken

### Frühmetriken

- Anzahl blockierter Generated-Direct-Edits
- Anzahl Roadmap-`[x]` ohne Claim
- Anzahl Status-`done` ohne `proof_ref`
- Anzahl Tasks ohne `allowed_paths`
- Anzahl Non-Ideal-Task-Blocks

### Reifemetriken

- `claim_evidence_coverage`
- `critical_impl_registry_coverage`
- `agent_contract_fixture_coverage`
- `dry_run_success_rate`
- `blocked_vs_failed_ratio`
- `generated_artifact_classification_coverage`
- `roadmap_claim_verification_rate`

### Zielwerte nach Vollausbau

- `critical_impl_registry_coverage: 100%`
- `agent_contract_fixture_coverage: 100%` für definierte Tasktypen
- `generated_artifact_classification_coverage: 100%`
- `roadmap_claim_verification_rate: > 90%` für zentrale Bereiche
- `direct_generated_edits: 0`
- `status_done_without_proof: 0`

## 13. Akzeptanz des Gesamt-Blueprints

Der Blueprint gilt als umgesetzt, wenn:

1. Safety Preflight blockiert konkrete Agentenfehler.
2. Agent-Readiness kann fehlende Hard-Capabilities nicht als pass anzeigen.
3. Handlungsleitende Claims sind maschinenlesbar.
4. Kritische Impl-Registry-Einträge haben Evidence.
5. Agent-Tasks sind schema-validierbar.
6. Non-Ideal-Tasks blockieren.
7. Dry-Run Runner führt echte Tasks deterministisch aus.
8. Run-Artefakte existieren.
9. Generated-Artefakte im Startumfang sind geschützt.
10. Roadmap-/Statuslügen in Agent-/CI-/Generated-Bereichen werden blockiert.
11. Write Mode ist nur gated möglich.

## 14. Essenz

**Hebel:** Früh konkrete Agentenfehler blockieren, dann Evidence und Contracts aufbauen.

**Entscheidung:** Vollausbau bleibt Ziel, aber nicht als Big Bang. Start mit Safety Preflight, Readiness Hard Fail, Minimal Claim Spine, Minimal Contracts und Non-Ideal Guard.

**Nächste Aktion:** PR 8 `docs/generated-control-minimal` abschließen und post-merge verifizieren.

Minimaler erster Scope:

- `docs/_generated` direct edit blockieren
- Roadmap-`[x]` ohne Claim/Proof blockieren
- Status `done` ohne `proof_ref` blockieren
- Changed paths gegen `allowed_paths` prüfen
- Workflow-/Infra-Änderungen `task_type`-pflichtig machen

Humoröse Randnotiz: Der Agent darf weiter kreativ sein. Nur nicht an den Bremsleitungen.

## 15. Epistemischer Status

Dieses Blueprint ist ein Ziel- und Planungsartefakt. Es beschreibt gewünschte Mechaniken, ersetzt aber keine Evidence. Umsetzung und Status müssen durch Tasks, Claims, CI, Proofs und Run-Artefakte belegt werden.
