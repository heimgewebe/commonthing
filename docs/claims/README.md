---
id: docs.claims.readme
title: Claim-Registry
doc_type: reference
status: active
summary: Minimal machine-readable Claim-Evidence registry for AGENT-SAFE-003.
relations:
  - type: relates_to
    target: docs/blueprints/blueprint-agent-safety-control-layer.md
  - type: relates_to
    target: docs/tasks/index.json
  - type: relates_to
    target: scripts/docmeta/validate_claim_registry.py
---

# Claim-Registry

## Zweck

Die Claim-Registry bindet zentrale Claims an konkrete Evidence-Pfade und
Validierungskommandos. Sie ist maschinenlesbar und deterministisch pruefbar.

Die Registry entscheidet nicht final ueber Wahrheit oder Produktstatus.
Sie liefert nur eine pruefbare Belegstruktur fuer Review, CI und Task-Control.

## Verhaeltnis zu anderen Flaechen

- Roadmap und Task-Control bleiben die Arbeitssteuerung.
- Agent-Readiness bleibt ein diagnostischer Statusreport.
- CI und menschliches Review treffen die finalen Freigabeentscheidungen.

## Datei und Format

Die Registry liegt unter `docs/claims/registry.yml`.

Wegen fehlender Projektabhaengigkeit fuer YAML-Parsing wird ein strenger
JSON-kompatibler YAML-Subset genutzt. Das heisst: Die Datei hat `.yml`,
enthaelt aber JSON-Struktur, damit die Validierung ohne externe Dependency
stabil bleibt.

## Statuswerte

Erlaubte Claim-Statuswerte:

- `proposed`
- `established`
- `superseded`
- `rejected`

Nicht erlaubt sind Done-Semantiken wie `done`, `verified` oder `proven`.

## Evidence-Kinds

Typische `kind`-Werte:

- `implementation`
- `test`
- `ci`
- `documentation`
- `generated-report`
- `registry`

## Evidence-Pfade

- Evidence-`path` muss relativ zum Repository-Root sein.
- Absolute Pfade (zum Beispiel `/etc/hosts`) sind unzulaessig.
- Parent-Traversal (zum Beispiel `../foo`) ist unzulaessig.
- Bei `status: established` muessen repo-interne Evidence-Pfade zusaetzlich
  existieren.

## Nicht-Ziele dieses Slices

Der urspruengliche Claim-Spine-Slice fuehrte nicht ein:

- Agent-Contract-Suite
- Handoff-Validator
- Non-Ideal-Guard
- Dry-Run-Runner
- Write-Mode
- Blocking-CI
- Vollstaendige Proof-Engine

AGENT-SAFE-004 und AGENT-SAFE-005 sind inzwischen separat implementiert und
belegt. AGENT-SAFE-006 fuehrt den read-only Dry-Run-Runner ein. Write Mode,
Blocking-CI fuer alle Agent-Safety-Befunde und vollstaendige Run-Evidence
bleiben Folgearbeiten.

## Freshness Registry (Lenskit Bridge)

`docs/doc-freshness-registry.yml` ist der Lenskit-kompatible Bridge-Contract.
Er exponiert die Claim-Evidence-Bindung aus `docs/claims/registry.yml` im
Format `lenskit.doc_freshness_registry` v1.0.

`docs/claims/registry.yml` bleibt die kanonische Claim-Quelle.
Die Bridge beweist keine Claims; sie macht die Claim-Evidence-Bindung fuer
Lenskit sichtbar.

`last_verified` belegt nur die Bridge-Validierung gegen die Claim-Registry
und repo-lokale Evidence-Pfade. Es beweist nicht, dass ein Claim wahr ist.

Evidence-Kinds werden explizit gemappt:

- `implementation`, `documentation`, `ci`, `generated-report`, `registry` -> Lenskit `file`
- `test` -> Lenskit `test`

- Validierung: `python3 scripts/docmeta/validate_doc_freshness_registry.py`
- Generierte Uebersicht (report-only):
  - `docs/_generated/claim-evidence-map.md`

## Freshness Scope Policy

Die Freshness-Registry wird nicht durch frei wachsende Eintraege erweitert,
sondern durch `scripts/docmeta/freshness_scope_policy.yml` begrenzt.

Aktuell aktive Familie:

- `CLAIM-AGENT-SAFE-*` -> `claim-agent-safe-*`
- mirror_mode: `exact`
- require_live_check: true

Das bedeutet:

- Alle Claims einer aktiven Familie muessen in `docs/doc-freshness-registry.yml`
  gespiegelt sein.
- Eintraege ausserhalb aktiver Familien sind Findings.
- Die Policy entscheidet den Scope, nicht der Validator-Code.

`require_live_check: true` bedeutet strukturell: Jeder Eintrag der Familie
traegt mindestens eine Evidence, die gegen das Live-Dateisystem geprueft wird
(`file`/`test`/`proof`). Es wird keine Wall-Clock-Freshness berechnet.

Hinweis zu `proof`: `proof` ist in `EVIDENCE_KINDS_CHECK_PATH` und damit
live-geprueft. Es ist aber kein commonThing-Claim-Evidence-Kind (nicht in
`CLAIM_EVIDENCE_KIND_TO_LENSKIT`). Ein Bridge-Eintrag mit `proof` besteht
`require_live_check`, aber wird beim Cross-Check Findings erzeugen, wenn die
Claim-Evidence-Seite keine passende Gegenseite aufweist.

### registry_doc-Semantik

In diesem Slice gilt:

- Der Validator unterstuetzt genau eine Claims-Registry pro Lauf.
- Jede aktive Familie muss `registry_doc` gleich dem tatsaechlich verwendeten
  Claims-Pfad (`--claims`, Default `docs/claims/registry.yml`) setzen; sonst
  ist die Policy ungueltig (`FRESHNESS_SCOPE_POLICY_INVALID`).
- In-scope Freshness-Eintraege spiegeln ihr `doc`-Feld gegen
  `family.registry_doc`.
- Out-of-scope Eintraege werden als Finding gemeldet und nicht zusaetzlich
  ueber einen globalen doc-Default geprueft.
- Kein Multi-Registry-Loading in diesem Slice.

### Neue Freshness-Family hinzufuegen

Operative Schritte:

- Claim-Familie in `docs/claims/registry.yml` anlegen.
- Active Family in `scripts/docmeta/freshness_scope_policy.yml` eintragen.
- `claim_id_prefix`, `entry_id_prefix`, `registry_doc`, `mirror_mode`,
  `require_live_check` und `status` setzen.
- Bei `mirror_mode: exact` muss jeder aktive Claim der Familie einen passenden
  Entry in `docs/doc-freshness-registry.yml` haben.
- Das `doc`-Feld eines Entries muss `family.registry_doc` entsprechen.
- Bei `require_live_check: true` muss mindestens eine live-gepruefte Evidence
  vorhanden sein. Live-geprueft sind genau die Kinds aus
  `EVIDENCE_KINDS_CHECK_PATH` (aktuell `file`/`test`/`proof`).
  `proof` erfuellt diese Bedingung, ist aber kein commonThing-Claim-Evidence-Kind
  und erzeugt Cross-Check-Findings wenn die Claim-Evidence keine Gegenseite hat.
- Danach pruefen:
  - `python3 scripts/docmeta/validate_doc_freshness_registry.py`
  - `python3 -m scripts.docmeta.generate_claim_evidence_map --check`
  - `python3 -m scripts.docmeta.generate_task_index --check`
