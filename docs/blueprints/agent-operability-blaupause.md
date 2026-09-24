---
id: docs.blueprints.agent-operability
title: Minimaler Agent-Operability-Kern
doc_type: blueprint
status: draft
summary: Definition des minimalen Action-Layers zur Ausführung konkreter Entwicklungsaufgaben.
relations:
  - type: relates_to
    target: docs/policies/agent-reading-protocol.md
  - type: relates_to
    target: docs/reports/agent-readiness-audit.md
  - type: relates_to
    target: AGENTS.md
  - type: relates_to
    target: agent-policy.yaml
---

# Minimaler Agent-Operability-Kern

## Dialektik

These:
Wir bauen jetzt den minimalen Action-Layer, damit Agents endlich wirksam arbeiten können.
→ Fokus: sofort nutzbar, konkret, implementierbar.

Antithese:
Selbst dieser „minimale“ Layer kann entgleisen:
→ zu viele Contracts, zu abstrakt, keine echte Nutzung → wieder Theorie.

Synthese:
Baue nur das, was in der nächsten echten PR benutzt wird.
→ „Blueprint → Task → Command → PR“ muss sofort funktionieren.

## Ziel

Agents sollen konkrete Entwicklungsaufgaben strukturiert ausführen können.

Nicht mehr. Nicht weniger.


## Architektur

### Status

Diese Blaupause beschreibt einen Zielzustand.
Die beschriebenen Strukturen (z. B. `/agent/`, `wgx task run`, `experiments/`) sind konzeptionell und derzeit nicht vollständig im Repository implementiert.

Sie dienen als Referenz für zukünftige Implementierungen und dürfen nicht als bestehende Systemstruktur interpretiert werden.


Blueprint → Task → Commands → Execution → Ergebnis

Kein:

- Signal-System
- Policy Engine
- Emergenz

Nur Durchstich von Denken → Handeln

## Kernkomponenten

### Command Contracts

WICHTIG:
Diese Commands sind konzeptionelle Contracts und müssen perspektivisch in `contracts/` als maschinenvalidierbare Schemas formalisiert werden. Maßgeblich ist dabei JSON Schema als kanonisches Format, konsistent zur bestehenden Contract-Struktur und Validierung im Repo. Die YAML-Blöcke in dieser Blaupause dienen nur der lesbaren Skizze, nicht als alternative Schema-Quelle.

Zweck:
Strukturierte, überprüfbare Aktion.

#### C1: command.read_context

```yaml
id: command.read_context
type: command
input:
  - paths[]
output:
  - extracted_facts
  - uncertainties
constraints:
  - must_reference_files
```

Ersetzt:

- „lies repo“
- „analysiere code“

#### C2: command.write_change

```yaml
id: command.write_change
type: command
input:
  - target_file
  - change_type (create|update|delete)
  - content
constraints:
  - target_proof_required
  - no_generated_files
```

Verbindet mit der bestehenden Diagnose-Regel.

#### C3: command.validate_change

```yaml
id: command.validate_change
type: command
input:
  - checks[]
output:
  - success
  - errors[]
checks:
  - lint
  - test
  - docs-guard
```

Warum genau diese drei?
Weil sie exakt abdecken:

- Phase Verstehen: `read_context`
- Phase Handeln: `write_change`
- Phase Prüfen: `validate_change`

Weitere Commands sind in dieser Phase iterativer Overhead.

### Task-System

Zweck:
Strukturierte Arbeitssequenz.

#### Minimal-Template

```yaml
task:
  id: fix_map_submission
  goal: "Formularübermittlung implementieren"
  constraints:
    - no breaking changes
    - contract compliance
  steps:
    - command: command.read_context
    - command: command.write_change
    - command: command.validate_change
```

WICHTIG:
Task ≠ Beschreibung
Task = ausführbare Struktur

### Geplanter Validierungslayer für Agent-Tasks

**Wichtig:** Dies beschreibt das **Zielbild und die Sollstruktur**. Die folgenden Assertions sind **noch nicht kanonisch** und **noch keine bindende Repo-Policy**. Eine spätere Extraktion und Kanonisierung (z. B. in `docs/policies/`) ist erst nach einer Pilot-Erprobung an realen Tasks denkbar.

Um Tasks sauber zu begrenzen, sind folgende Assertions als geplante Invarianten vorgesehen:

- **A0.1 – Discovery-Prädikat**
  Ein Task ist erst gültig definierbar, wenn explizit festgelegt ist:
  - `scope`
  - `counts_as_usage`
  - `does_not_count`

  Im Zielbild würde dies z. B. wie folgt konkretisiert:

  ```yaml
  task.discovery_predicate:
    scope: apps/web/src/
    counts_as_usage:
      - TypeScript-Typreferenz
    does_not_count:
      - Kommentare
      - String-Literale
  ```

- **A0 – Kontext-Vollständigkeit**
  `read_context` ist nur dann ausreichend, wenn alle Entscheidungen daraus allein begründbar sind.

- **A1 – Kausalkette**
  Eine Scope-Erweiterung ist nur zulässig, wenn eine rekonstruierbare Kausalkette vom Ziel zur neu betroffenen Datei vorliegt.

- **A2 – Unabhängigkeitstest**
  Wenn eine entdeckte Änderung auch ohne das Kernziel als eigener sinnvoller Task bestehen könnte, muss abgebrochen und neu geschnitten werden.

- **A3 – Lokalität**
  Alle Entscheidungen müssen innerhalb eines gemeinsamen Entscheidungskontexts haltbar sein; wenn externer Zusatzkontext nötig wird, ist der Task neu zu schneiden.

#### Geplante Entscheidungslogik

Im Zielzustand würde daraus im Task-Schnitt und Agent-Loop folgende kompakte Entscheidungslogik folgen:

1. Discovery-Prädikat vorhanden?
   - nein → Task noch nicht gültig definierbar
2. `read_context` vollständig?
   - nein → Kontext erweitern
3. neue Artefakte entdeckt?
   - nein → `write_change`
   - ja → A1 prüfen
4. A1 gültig?
   - nein → A2 prüfen
5. A2 verletzt?
   - ja → abbrechen / Scope neu schneiden
6. A3 verletzt?
   - ja → abbrechen / Task neu schneiden
7. sonst → `write_change`, danach `validate_change`

Dieser Validierungslayer ist nicht Teil der initialen Minimal-Implementierung, sondern ein geplanter Präzisierungsschritt für die Pilot-Erprobung realer Tasks.

### Agent-Loop

```text
1. read_context
2. decide (implizit im Agent)
3. write_change
4. validate_change
```

Kein Planner-Agent in dieser Iteration erforderlich.

### Execution

CLI (bewusst simpel):

```text
wgx task run fix_map_submission
```

*(Hinweis: `wgx` dient hier als möglicher CLI-Einstiegspunkt; daraus folgt keine allgemeine Repo-Abhängigkeit des commonThing-Kerns von externen Systemstrukturen. commonThing bleibt eigenständig.)*

Was passiert intern?
load task → execute commands sequentially → log results

## Optionale spätere Erweiterungen nach Phase 3

Die folgenden Elemente gehören nicht zum zwingenden Minimal-Kern. Sie orientieren sich teilweise an erprobten experimentellen Mustern aus anderen Repos, werden hier aber ausschließlich als optionale, native Erweiterungen für commonThing geführt. Sie sind nicht Voraussetzung für Phase 1–3 der Roadmap und werden nur nach erfolgreicher Basiserprobung relevant, um die Agent-Produktivität gezielt zu erhöhen:

### 1. Experiment-Struktur (Kernmodul)

Zweck: Strukturierte Hypothesenprüfung statt implizitem Trial & Error.

```text
experiments/
  <feature>/
    manifest.yml
    method.md
    results/
      decision.yml
      evidence.jsonl
```

**Minimal-Schema (`manifest.yml`)**:

```yaml
id: map_submission_fix
hypothesis: "Form submission fails due to missing API binding"
success_criteria:
  - submission works end-to-end
schema_version: 1
```

Einsatzregel: Nur für architekturkritische oder unsichere Features.

### 2. Evidence-Log (`evidence.jsonl`)

Zweck: Realität wird explizit gemacht.

```json
{
  "event_type": "submission_failed",
  "component": "map_form",
  "count": 7,
  "timestamp": "2026-04-09"
}
```

Pflicht-Vokabular (Minimal):
`event_type`:

- `error`
- `success`
- `retry`
- `manual_intervention`
- `unexpected_behavior`

**Regel:** Agenten dürfen nur diese Event-Typen verwenden, Erweiterungen müssen explizit dokumentiert werden.

Einsatzregel: Minimal starten, nur kritische Events loggen.

### 3. Decision Artifacts

Zweck: Beendet Denk-Schleifen.

Das folgende Minimal-Schema ist verpflichtend:

```yaml
decision:
  id: map_submission_strategy
  status: adopted
  choice: "client-side validation + API endpoint"
  rationale: "..."
  alternatives:
    - server-side only
    - hybrid
```

**Regel:** Jede architekturrelevante Änderung MUSS ein Decision Artifact erzeugen.

Einsatzregel: Pflicht für Architekturentscheidungen und größere PRs.

### 4. Golden Example (Referenzfall)

Zweck: Lebende Dokumentation, Testfall und Onboarding.

```text
experiments/map_submission/
  CONTEXT.md
  INITIAL.md
  method.md
  result.md
  decision.yml
  evidence.jsonl
```

**Definition:** Ein Golden Example gilt nur als vollständig, wenn:

- `CONTEXT.md` vorhanden
- `INITIAL.md` vorhanden
- `decision.yml` vorhanden
- `evidence.jsonl` vorhanden

Einsatzregel: Genau 1–2 perfekte Beispiele, regelmäßig aktualisieren.

### 5. Scaffolding-CLI

Zweck: Reduziert Reibung und Fehler bei Struktur.

Beispiel: `wgx new experiment map_submission`

Einsatzregel: Minimal implementieren (kein Overengineering).

### 6. Experiment-Chaining

Zweck: Macht Iteration nachvollziehbar.

```yaml
manifest:
  id: map_submission_v2
  replicated_from: map_submission_v1
```

Einsatzregel: Optional, aber empfohlen bei Iterationen.

### 7. Minimaler reaktiver Loop (1 Use Case!)

Zweck: Erste Form von „System reagiert selbst“.

```yaml
state: map_submission_broken
signal: critical_issue
policy: create_fix_task
action: open_task
```

**Regel:** Der reaktive Loop darf initial nur für EINEN Use Case verwendet werden. Eine Ausweitung erfordert ein Decision Artifact.

Einsatzregel: Exakt 1 Loop implementieren, nicht mehr.

### 8. Staleness / Revalidierung

Zweck: Verhindert veraltetes Wissen.

```yaml
last_validated: 2026-04-01
next_review_due: 2026-05-01
```

Einsatzregel: Phase 2 (nicht sofort).

### 9. Schema-Versionierung

Zweck: Verhindert stille Breaking Changes.

```yaml
schema_version: 1
```

`schema_version` ist Pflicht für:

- `manifest.yml`
- `decision.yml`
- catalog entries

Einsatzregel: Pflicht für experiments, decisions, catalog.

### 10. Contribution Contract

Zweck: Verhindert Wildwuchs.

Jede Änderung muss einem Typ entsprechen:

- **experiment** → erzeugt oder verändert `experiments/`
- **decision** → verändert Entscheidungslogik
- **fix** → direkte Code-/Doc-Korrektur ohne Experiment

**Regel:** Jede PR muss einen dieser Typen explizit angeben.

Einsatzregel: Minimal halten.

---

### Nicht übernehmen (explizit)

- Vollständige epistemische Dokumentstruktur (zu schwergewichtig)
- Benchmark-System (zu früh, falscher Fokus)
- Export-/IR-System (bereits andere Mechaniken vorhanden)
- Vollständiger Intelligence Layer (commonThing hat bereits eigene Architektur)


Die folgende Roadmap priorisiert ausschließlich den operativen Minimalpfad. Die im vorigen Abschnitt beschriebenen optionalen Erweiterungen gehören nicht zum Initialumfang und werden erst nach erfolgreicher Basiserprobung des Kerns relevant.

## Roadmap

### Ausführungsprinzip

- Jede Phase beginnt mit Diagnose (Ist-Zustand erfassen)
- Keine Implementierung ohne Target-Proof
- Jede Aktion muss auf konkrete Dateien/Outputs referenzieren
- Die Roadmap ist kein To-do, sondern ein kontrollierter Ausführungsprozess

### Phase 1: Minimaler Kern (Command & Task Definitionen)

- **Ziel:** Erstellung der ersten drei Commands und einer validen Task.
- **Diagnose:** Existieren bereits `agent/`-Ordner oder analoge Strukturen im Repo? Müssen die vorgeschlagenen Zielpfade neu angelegt werden?
- **Erwartete Outputs:** Konkrete Nachweise aus Repo-Struktur/Suchtreffern über belegte Pfade oder belegte Nicht-Existenz.
- **Stop-Kriterium:** Konkret festgelegte und belegte Zielpfade für Commands und Task.
- **Umsetzung (erst danach):**
  - Erstelle `read_context.yaml` im diagnostisch festgelegten Pfad.
  - Erstelle `write_change.yaml` im diagnostisch festgelegten Pfad.
  - Erstelle `validate_change.yaml` im diagnostisch festgelegten Pfad.
  - Definiere erste Test-Task-Datei `fix_map_submission.yaml` mit der Task-ID `fix_map_submission` im diagnostisch festgelegten Pfad (Task-ID und Dateiname können übereinstimmen, sind aber konzeptionell zu unterscheiden).

### Phase 2: Execution Engine

- **Ziel:** Minimaler Ausführungs-Runner für Agenten-Tasks.
- **Diagnose:** Gibt es bereits bestehende CLI-/Script-Einstiegspunkte, die genutzt werden können?
- **Erwartete Outputs:** Explizite Entscheidung für eine Sprache (z. B. Python oder TS) und einen Dateipfad.
- **Stop-Kriterium:** Sprache entschieden, Zielpfad entschieden, Minimalumfang (Runner kann genau 1 Task ausführen) entschieden.
- **Umsetzung (erst danach):**
  - Implementiere den zuvor festgelegten Basis-Runner im diagnostisch gewählten Zielpfad.
  - Dokumentiere die diagnostisch gewählte lokale Ausführungsform und binde den Runner an diese konkrete Ausführungsform an.

### Phase 3: Integration & Erprobung

- **Ziel:** Vollständiger Durchlauf einer Task.
- **Diagnose:** Ist die Task `fix_map_submission` lauffähig und fehlerfrei definiert?
- **Erwartete Outputs:** Konkreter Mindestnachweis (Runner-Output/Exit-Signal, konkret veränderte Dateien, Ergebnis der Validierungsschritte).
- **Stop-Kriterium:** Eindeutiger, nachweisbarer Erfolgsnachweis des gesamten Loops (read -> write -> validate).
- **Umsetzung (erst danach):**
  - Führe `fix_map_submission` aus.
  - Wenn belegtes Fehlverhalten oder fehlende Ausführbarkeit vorliegt, passe die betroffenen Commands gezielt an und validiere den gesamten Loop erneut. Die Ursache ist im Runner-Output, Log oder einem zugehörigen Entscheidungsartefakt zu dokumentieren.

### Phase 4: Optionale experimentelle Erweiterungen

- **WICHTIG:** NICHT vor erfolgreichem Abschluss von Phase 3 beginnen.
- **Ziel:** Etablierung eines leichtgewichtigen Experiment-Schemas für commonThing.
- **Diagnose:** Sind die grundlegenden Tasks stabil und verlässlich?
- **Erwartete Outputs:** Diagnostisch begründetes Scaffolding für ein experimentelles Verzeichnis und dessen Kernartefakte.
- **Stop-Kriterium:** Belegter Zielpfad und valides Schema für `manifest.yml` und `evidence.jsonl` existieren.
- **Umsetzung (erst danach):**
  - Lege minimales Scaffolding im diagnostisch festgelegten Zielpfad an.
  - Etabliere Schema für `manifest.yml` und `evidence.jsonl`.

## Aktivierung

Diese Blaupause wird angewendet, wenn:

- Agent Entwicklungsaufgaben ausführt
- Blueprint → Implementation überführt wird
- PR-Erstellung automatisiert wird

Nicht anzuwenden für:

- reine Analyse
- Dokumentationsaufgaben ohne Codeänderung

## Repo-Integration

Neue Struktur:

```text
/agent/
  commands/
    read_context.yaml
    write_change.yaml
    validate_change.yaml
  tasks/
    fix_map_submission.yaml
/scripts/
  run-task.ts (oder .py)
```

Verbindung zu bestehendem System:
Schon vorhanden (stark!):

- AGENTS.md → Regeln
- agent-policy.yaml → Guardrails
- CI → Validierung
- Contracts → Wahrheit

Der neue Layer macht die bereits vorhandenen Regeln, Guardrails und Validierungsmechanismen operativ ausführbar.

## Typische Fehler

Fehler 1: Zu viele Commands
→ Es werden drei Commands verwendet.

Fehler 2: Tasks als Text
→ ineffektiv:

```yaml
steps:
  - "analysiere code"
```

→ nicht maschinenvalidierbar

Fehler 3: Keine echte Nutzung
→ Erste Task muss echten Bug fixen.

Alternative Sinnachse:
Statt: „Wir bauen ein Agent-System“
Zielbild: „Wir bauen einen strikten, maschinenlesbaren Ausführungsvertrag“

## Risiko–Nutzen

Nutzen:

- sofort bessere PRs
- weniger Drift
- reproduzierbare Agent-Arbeit

Risiken:

1. Zu abstrakt → Lösung: nur echte Tasks bauen
2. Agent ignoriert Struktur → Lösung: strikt erzwingen
3. Doppelstruktur zu bestehenden Tools → Lösung: integrieren, nicht ersetzen

## Prämissencheck

Das funktioniert nur, wenn:

- Agenten aktiv eingesetzt werden
- echte Tasks definiert werden (keine Theorie)
- die Struktur konsequent durchgesetzt wird

Konkrete nächste Aktion:
Schritt 1: 3 Command YAMLs anlegen
Schritt 2: 1 echte Task definieren (`fix_map_submission`)
Schritt 3: minimalen Runner bauen

## Erste Implementierung

Erste Task im Repo:

```text
agent/tasks/fix_map_submission.yaml
```

Diese Task dient als Referenzimplementierung und muss real ausführbar sein.

## Maschinenlesbarer Kern

Der folgende Block fasst den operativen Minimal-Kern in kompakter, maschinenlesbarer Form zusammen.

```yaml
agent_operability:
  commands:
    - id: command.read_context
    - id: command.write_change
    - id: command.validate_change
  task_template:
    required_steps:
      - command.read_context
      - command.write_change
      - command.validate_change
  execution:
    type: cli
    entrypoint: scripts/run-task
```

## Essenz

Hebel: Task-System (nicht Agenten, nicht Emergenz)
Entscheidung: Minimaler Action Layer statt komplexer Architektur
Nächste Aktion: → 3 Commands + 1 echte Task implementieren

Unsicherheitsgrad: 0.14
Ursachen: genaue CLI-/Tooling-Struktur im Repo nicht vollständig bekannt
Interpolationsgrad: 0.19
Annahmen: kein bestehender Task-Runner, YAML als praktikables Format
