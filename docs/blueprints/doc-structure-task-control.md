---
id: blueprint-doc-structure-task-control
title: commonThing Dokumentationsstruktur und Task-Steuerung
doc_type: blueprint
status: draft
summary: >
  Zielbild für eine schmale Task-Control-Schicht, die Navigation,
  Optimierungsstatus, maschinenlesbare Task-Artefakte und GitHub-Arbeitsobjekte
  verbindet, ohne eine neue Wahrheitsschicht einzuführen.
relations:
  - type: relates_to
    target: docs/reports/optimierungsstatus.md
  - type: relates_to
    target: docs/blueprints/agent-operability-blaupause.md
  - type: relates_to
    target: docs/blueprints/doc-structure-task-control-roadmap.md
  - type: relates_to
    target: docs/blueprints/doc-structure-task-control-examples.md
---

# Blaupause: Dokumentationsstruktur und Task-Steuerung für commonThing

## 0. Status und Grenze

Dieses Dokument ist ein **Zielbild**, keine Umsetzung und keine neue
Wahrheitsschicht. Es beschreibt, welche operative Task-Control-Schicht später
entstehen soll. Verbindliche Statuswechsel bleiben bei den vorhandenen
Statusmatrizen und belegten Repo-Artefakten.

Die operative Reihenfolge lebt in
[doc-structure-task-control-roadmap.md](doc-structure-task-control-roadmap.md).
Konkrete YAML-, JSON- und Workflow-Beispiele leben in
[doc-structure-task-control-examples.md](doc-structure-task-control-examples.md).

## 1. Kurzfassung

commonThing besitzt bereits eine starke Dokumentationsmaschine. Es fehlen nicht
primär weitere Dokumente, sondern eine klarere Arbeitsumlaufbahn zwischen
Orientierung, Nachweis und Arbeitspaket:

```text
README.md
→ docs/index.md
→ docs/tasks/
→ docs/reports/optimierungsstatus.{md,json}
→ GitHub Issues / Projects / PRs / Release Labels
```

Ziel ist weniger Kontextverlust zwischen Navigation, belegtem Status und
konkreter Arbeit. Das Repo hat ein Gedächtnis; die Task-Control-Schicht soll
das Kurzzeitgedächtnis ergänzen.

## 2. These / Antithese / Synthese

### These

Das Repo ist bereits ungewöhnlich agenten- und dokumentationsorientiert.
Vorhanden sind unter anderem:

- Frontmatter und Docmeta-Struktur,
- `AGENTS.md` als agentischer Leseleitfaden,
- `repo.meta.yaml` als Truth-Model- und Precedence-Schicht,
- generierte Diagnoseartefakte unter `docs/_generated/`,
- Statusmatrizen für einzelne Bereiche,
- Dokumenten-Relations- und Coverage-Mechanismen.

### Antithese

Die vorhandene Stärke ist fragmentiert. Aufgaben, Nachweise und
Navigationspunkte liegen verteilt in README, Fahrplan, Reports, generierten
Diagnosen und Statusmatrizen. GitHub-native Arbeitsobjekte wie Issues,
Project-Felder, Issue Forms und Labels sind noch nicht ausreichend als
Steuerungsschicht angebunden.

### Synthese

Die bestehende Dokumentationsintelligenz bleibt erhalten, wird aber durch eine
schmale, maschinenlesbare Task-Control-Schicht ergänzt:

- `docs/tasks/board.md` für Menschen,
- `docs/tasks/index.json` für Agents,
- `docs/reports/optimierungsstatus.json` als maschinenlesbarer Zwilling,
- Issue Forms für strukturierte Arbeitspakete,
- leichtes PR-Meta-Template,
- Label-Taxonomie,
- Generator und Guard für Task-Index, Konsistenz und Drift.

## 3. Ziele

Menschen sollen schneller erkennen:

- was offen ist,
- was priorisiert ist,
- wo Nachweise liegen,
- welche Datei autoritativ ist,
- was nur Navigation oder Diagnose ist.

Agents sollen zuverlässiger:

- To-dos erkennen,
- Status und Evidenz auswerten,
- Pfade prüfen,
- fehlende Akzeptanzkriterien melden,
- GitHub-Issues, PRs und Dokumente miteinander verbinden.

Das Repo soll vermeiden:

- doppelte Task-Wahrheiten,
- veraltete Navigationspfade,
- Markdown-only-Steuerung,
- „optimiert, aber nicht nachweisbar“,
- Agenten-Halluzination durch veraltete Pfade.

## 4. Nicht-Ziele

Diese Blaupause ist ausdrücklich nicht:

- ein vollständiger Umbau der Dokumentation,
- eine Ablösung von `docs/` durch GitHub Projects,
- ein schweres PR-Bürokratieformular,
- eine Einführung von Agents als Default-Branch-Schreiber,
- eine neue Wahrheitsschicht neben bestehenden Dokumenten.

GitHub-Issues und Projects sind Arbeitsobjekte, nicht Wahrheit. Markdown und
JSON bleiben repo-lokale Artefakte. Die Arbeitsebene bekommt nur bessere
Umlaufbahnen.

## 5. Grundprinzipien

### 5.1 Navigation ≠ Wahrheit

`docs/index.md`, `docs/tasks/board.md` und generierte `_generated`-Artefakte
sind Navigation, Diagnose oder Arbeitssteuerung. Sie dürfen auf Wahrheit
verweisen, aber nicht still Wahrheit ersetzen.

### 5.2 Markdown für Menschen, JSON für Maschinen

Jede operative Statusfläche mit längerfristiger Steuerungsfunktion braucht
einen maschinenlesbaren Zwilling. Bestehende Muster wie
`auth-status-matrix.{md,json}` und `map-status-matrix.{md,json}` werden dafür
als Strukturvorbild genutzt, nicht als automatische Wahrheitserweiterung.

### 5.3 Task-ID vor Freitext

Jedes relevante Arbeitspaket bekommt eine stabile ID, zum Beispiel:

```text
OPT-DOC-001
OPT-TASK-002
OPT-AGENT-003
AUTH-PERSIST-004
MAP-VIS-005
```

Diese ID verbindet Markdown-Abschnitt, JSON-Eintrag, Issue, PR,
Implementierung, Nachweis und Restlücke.

### 5.4 Diagnose darf laut sein

Generated Reports wie `impl-index`, `doc-coverage`, `orphans`,
`knowledge-gaps`, `staleness-report`, `relations-analysis` und
`relates-to-audit` sollen nicht versteckt bleiben. Sie gehören in die vordere
Navigation, bleiben aber Diagnose.

### 5.5 Agents schreiben Vorschläge, keine Wahrheit

Agents dürfen lesen, prüfen, indizieren, Drift melden, PRs vorbereiten und
Task-Vorschläge erzeugen. Sie dürfen keinen erledigten Status ohne Evidenz
setzen, Markdown-Lücken nicht interpretativ glätten und nicht vorhandene Pfade
nicht als Zielpfade verwenden.

## 6. Zielartefakte

Die Task-Control-Schicht besteht aus schmalen, getrennten Artefakten:

| Artefakt | Rolle | Wahrheitsstatus |
|---|---|---|
| `docs/tasks/README.md` | Einstieg in Task-Control | Orientierung |
| `docs/tasks/board.md` | Menschliche Arbeitskarte | Arbeitssteuerung, keine Wahrheit |
| `docs/tasks/index.json` | Maschinenlesbarer Task-Index | abgeleitet / validiert |
| `docs/tasks/schema.json` | Schema für Task-Index | Validierungsvertrag |
| `docs/reports/optimierungsstatus.json` | Maschinenlesbarer Zwilling des Markdown-Status | abgeleitet aus Statusmatrix |
| Issue Forms | GitHub-native Eingabe strukturierter Tasks | Arbeitsobjekte — **optional, aktuell zurückgestellt** |
| PR-Template | leichter Review-Kontext | Arbeitsobjekt — **optional, aktuell zurückgestellt** |
| Label-Taxonomie | Triage- und Release-Sprache | Prozesskonvention — optional |
| Generator/Guard | Drift-Abwehr | Prüfmechanismus — **nächste Priorität** |

Wichtig: Sobald `docs/tasks/index.json` oder
`docs/reports/optimierungsstatus.json` eingeführt werden, muss explizit
entschieden werden, ob sie generiert, kuratiert oder hybrid gepflegt werden.
Sie dürfen nicht unmarkiert als zweite Wahrheit entstehen.

## 7. Vorgeschlagene Zielstruktur

```text
README.md
CONTRIBUTING.md
AGENTS.md
repo.meta.yaml

docs/
  index.md
  tasks/
    README.md
    board.md
    index.json
    schema.json
  reports/
    optimierungsstatus.md
    optimierungsstatus.json
    auth-status-matrix.md
    auth-status-matrix.json
    map-status-matrix.md
    map-status-matrix.json

.github/
  ISSUE_TEMPLATE/
    task.yml
    bug.yml
    doc-drift.yml
    decision.yml
  pull_request_template.md
  release.yml
  workflows/
    task-index.yml

scripts/docmeta/
  generate_task_index.py
  validate_task_index.py
  sync_optimierungsstatus_json.py
```

## 8. Governance-Schnitt

### 8.1 Einstieg und Navigation

README und `docs/index.md` sollen schneller erklären:

- wo Orientierung beginnt,
- welche Dokumente Wahrheit tragen,
- welche Artefakte Diagnose sind,
- wo aktive Arbeitspakete liegen.

### 8.2 Task-Control

`docs/tasks/board.md` ist die menschliche Arbeitskarte.
`docs/tasks/index.json` ist die maschinenlesbare Sicht. Beide dürfen nur dann
Status verdichten, wenn Evidenz oder eine explizite Leerstelle benannt ist.

### 8.3 GitHub-Umlauf

Issues, PRs, Labels und Release Notes bilden den Umlauf für Kollaboration und
Review. Sie ersetzen keine repo-lokale Wahrheit.

### 8.4 Generator und Guard

Generatoren dürfen Indizes schreiben oder prüfen. PR-Checks sollen Drift
melden; automatische Commits gehören in separate Bot-PRs, nicht in stille
PR-Checks.

## 9. Entscheidungslogik

Diese Blaupause gilt, wenn folgende Prämissen stimmen:

1. commonThing soll langfristig agentenlesbar bleiben.
2. Offene Aufgaben sollen nicht nur im Fließtext existieren.
3. GitHub Issues/PRs/Projects sollen Arbeitsumlauf sein, nicht Wahrheitsschicht.
4. Generated Artefacts sollen Diagnose liefern, aber nicht manuell gepflegt werden.
5. Statusänderungen brauchen Evidenz.

Wenn diese Prämissen nicht stimmen, ist ein anderer Pfad sinnvoll.

## 10. Alternativen

### Pfad A: GitHub-first

Alles wird über Issues und Projects gesteuert; Markdown verweist nur noch.
Das verbessert UI und Verantwortlichkeit, schwächt aber repo-lokale Analyse und
Offline-Fähigkeit.

### Pfad B: Docs-only

Alles bleibt in Markdown/JSON. Das ist dumpbar und plattformarm, aber schwächer
für Triage, Review und Verantwortlichkeit.

### Empfohlener Pfad: Hybrid

Markdown/JSON bleibt Quelle für Struktur und Nachweis. GitHub liefert Umlauf,
Review und Kollaboration. Das passt am besten zu einem bereits
dokumentationszentrierten Repo, das Arbeitssteuerung braucht.

## 11. Risiken

| Risiko | Folge | Gegenmaßnahme |
|---|---|---|
| Bürokratisierung | Templates werden mechanisch gefüllt. | Templates minimal halten. |
| Zweite Wahrheit | JSON und Markdown driften auseinander. | Generator + CI-Check. |
| CI-Reibung | PRs scheitern an zu strengen Guards. | Erst warnend, dann blocking. |
| falsche Kanonizität | Task-Board wird als Wahrheit gelesen. | Rollen klar markieren. |
| Owner-Leere | Aufgaben sind strukturiert, aber herrenlos. | `owner: unknown` explizit erlauben und reporten. |
| Tokenrisiko | Project API braucht zusätzliche Rechte. | Least privilege, GitHub App, kein breit gesetzter PAT. |

## 12. Epistemische Leerstellen

Für die endgültige Betriebsentscheidung fehlen:

1. GitHub Project v2 Status: Board, Felder, Privatheit und Aktivität sind nicht
   verifiziert.
2. Owner-Matrix: Verantwortliche für Docs, CI, Release und Statusschluss sind
   nicht festgelegt.
3. Automationsgrad: nur prüfen, Bot-PRs erzeugen oder Project-Felder setzen ist
   offen.

Diese Leerstellen müssen vor Phase 4 der Roadmap geschlossen oder als explizite
Lücken markiert werden. Phase 3 (GitHub-Arbeitsobjekte) ist bewusst zurückgestellt;
sie setzt keine Klärung dieser Leerstellen voraus.

## 13. Essenz

Hebel: Eine schmale Task-Control-Schicht verbindet bestehende Doku mit
GitHub-Arbeitsobjekten.

Entscheidung: Hybrid statt GitHub-only oder Docs-only.

Nächste Aktion: Mit dem operativen Schnitt in
[doc-structure-task-control-roadmap.md](doc-structure-task-control-roadmap.md)
beginnen, ohne den Blueprint als erledigte Implementierung zu lesen.
