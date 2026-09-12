---
id: tasks.readme
title: Task-Control – Einstieg
doc_type: guide
status: active
summary: >
  Einstieg in die historische Task-Control-Kompatibilität von commonthing.
  Erklärt externe Arbeitsautorität, owner_task-Bindung und die Grenzen von docs/tasks/.
relations:
  - type: depends_on
    target: docs/reports/optimierungsstatus.md
  - type: relates_to
    target: docs/tasks/board.md
  - type: relates_to
    target: docs/policies/agent-reading-protocol.md
---

# Task-Control – Einstieg

## Zweck

`docs/tasks/` ist eine repository-lokale Kompatibilitäts- und Evidenzschicht für
historische Arbeitssteuerung. Sie ist keine zweite Wahrheitsschicht und nicht die
operative Arbeitsautorität.

Operative Arbeit wird außerhalb des Produkt-Repositories koordiniert. Für die aktuelle
Operator-Architektur ist Bureau die Arbeits-/Task-Autorität; Grabowski führt Arbeit aus.
commonthing bleibt Autorität für Produktcode, Produktwissen und repo-spezifische Verträge.

## Planning-Ownership-Ratchet

Aktive Planungsartefakte sollen ihre Arbeit künftig direkt über ein wohlgeformtes
`BUREAU-*`-`owner_task` im Frontmatter binden, zum Beispiel:

```yaml
owner_task: BUREAU-COMMONTHING-...
```

`BUREAU-*`-`owner_task` ist ein Verweis auf die externe Arbeitsautorität, keine lokale Kopie ihres
Status. commonthing prüft deshalb nur, dass die Bindung explizit vorhanden ist; Existenz,
Status, Queue, Claims oder Priorität des referenzierten Tasks werden nicht noch einmal im
Repo nachgebaut.

Bereits bestehende Planungsartefakte dürfen während der Migration weiterhin über
`docs/tasks/index.json`, `docs/tasks/board.md` oder `docs/roadmap.md` registriert sein,
**aber nur**, wenn ihr Pfad bereits in `legacy_fallback_paths` in
`scripts/docmeta/planning_registration.yml` steht. Diese endliche Liste ist die
Shrink-only-Migrationsmenge; neue Pfade dürfen sie nicht durch eine zusätzliche lokale
Registrierung vergrößern. Der CI-Guard `scripts.docmeta.check_planning_ownership` akzeptiert daher:

1. terminale Planungsartefakte ohne aktive Ownership;
2. aktive Planungsartefakte mit kanonischem `BUREAU-*`-`owner_task` als bevorzugten Pfad;
3. nur allowlistete Altartefakte mit vorhandener lokaler Registrierung als vorübergehenden Legacy-Fallback.

Damit kann die repo-lokale Schattensteuerung nur schrumpfen: Ein Altpfad wird entweder
archiviert oder extern gebunden und anschließend aus `legacy_fallback_paths` entfernt.

## Rollenklärung der Artefakte

| Datei | Rolle | Schreibstatus |
|---|---|---|
| `docs/tasks/board.md` | Historische/menschliche Arbeitskarte; Legacy-Kompatibilität | Manuell gepflegt, nicht bevorzugte Ownership |
| `docs/tasks/index.json` | Kuratierter Legacy-Task-Index (`manual_phase2_seed`) | Manuell; keine neue operative Arbeitsautorität |
| `docs/tasks/schema.json` | Validierungsvertrag für den Legacy-Index | Änderungen nur mit begründetem PR |
| `docs/reports/optimierungsstatus.md` | Belegte menschliche Statusmatrix | Maßgeblich für den dokumentierten Wahrheitsgehalt |
| `docs/reports/optimierungsstatus.json` | Maschinenlesbarer Zwilling der Statusmatrix | Kein eigenständiger Statusträger |

## Wahrheitsklärung

- `docs/reports/optimierungsstatus.md` bleibt die kanonische menschliche Wahrheitsquelle für die dort dokumentierten OPT-IDs und deren belegten Status.
- `docs/tasks/index.json` ist ein historisch gewachsener, kuratierter Task-Control-Index. Er ist keine vollständige Task-Wahrheit und keine Voraussetzung für neue Planung mit kanonischem `BUREAU-*`-`owner_task`.
- Bureau-/externe Task-Zustände werden nicht in `index.json` gespiegelt. `owner_task` ist nur die Bindung an diese externe Autorität.
- `docs/reports/optimierungsstatus.json` ist ein maschinenlesbarer Zwilling und dient als Lookup-Fläche. Es besitzt keinen eigenen Wahrheitsstatus.
- Kein Status in `index.json` oder `optimierungsstatus.json` darf dem Markdown widersprechen.
- `done` gilt nicht ohne reproduzierbaren Evidenz-Eintrag in der Statusmatrix.
- Stille Statusupgrades sind verboten.

## Curation-Status

Solange `curation: "manual_phase2_seed"` gesetzt ist, darf `index.json` für seine
bestehenden Legacy-Einträge manuell gepflegt werden. Diese Pflege darf jedoch keine
neuen Planungsartefakte legitimieren: Nur `legacy_fallback_paths` definiert den noch
zulässigen Altbestand.

Der in TASK-CTL-003 eingeführte `generate_task_index.py --check` bleibt ein reiner
Drift-Prüfmechanismus ohne Schreibzugriff. Er schützt den noch vorhandenen Legacy-Bestand,
macht diesen Bestand aber nicht zur Arbeitsautorität.

## Phase-Stand

| Phase | Artefakte | Status |
|---|---|---|
| Phase 2 | `docs/tasks/*`, `docs/reports/optimierungsstatus.json`, Validator | **Legacy-Kompatibilität vorhanden** |
| Ownership-Ratchet | `owner_task`, `check_planning_ownership`, CI | **Aktiv** — externe Ownership bevorzugt, Legacy-Fallback bleibt |
| Phase 3 | `.github/ISSUE_TEMPLATE/*`, `.github/pull_request_template.md` | **Zurückgestellt** — kein belegter Mehrwert gegenüber freien PR-Bodies |
| Legacy-Drift | `scripts/docmeta/generate_task_index.py`, CI-Guard | **Übergang** — schützt bestehenden Seed, erzeugt keine neue Wahrheit |

## GitHub-Arbeitsobjekte

Issue Forms, PR-Template und Release-Konfiguration sind keine Voraussetzung für die
Arbeitssteuerung. GitHub-Metadaten ersetzen weder Bureau-Ownership noch belastbare
Produkt-/Runtime-Evidenz.

## Validator des Legacy-Index

```bash
python3 -m scripts.docmeta.validate_task_index docs/tasks/index.json
```

Exit 0 bei Erfolg, 1 bei Validierungsfehlern. Keine stillen Fixes, kein Schreiben durch
den Validator.

## Planning-Ownership-Check

```bash
python3 -m scripts.docmeta.check_planning_ownership --mode strict
```

Der Check verlangt für aktive Planung entweder die bevorzugte kanonische `BUREAU-*`-
`owner_task`-Bindung oder, während der Migration, eine vorhandene Legacy-Registrierung. Terminale
Planungsdokumente benötigen keine aktive Ownership.

## Legacy-Drift-Check

```bash
python3 -m scripts.docmeta.generate_task_index --check
```

Der Check vergleicht den noch vorhandenen Legacy-Bestand aus `board.md`, `index.json` und
`docs/reports/optimierungsstatus.json`. Er bleibt vorerst bestehen, bis die von ihm
geschützten aktiven Planungsartefakte auf kanonische externe Ownership migriert sind.

Beide Prüfungen laufen im CI über `.github/workflows/task-index.yml`. Keine davon darf
aus dem Repository eine zweite operative Task-Wahrheit machen.
