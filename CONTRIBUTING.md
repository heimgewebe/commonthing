# CONTRIBUTING

Wie im commonThing-Repository gearbeitet wird: Orientierung, Routing, Workflow, Qualität.

Verbindliche Grundlagen vor jedem Patch:

- [`repo.meta.yaml`](repo.meta.yaml) — Truth Model und Konfliktauflösung.
- [`AGENTS.md`](AGENTS.md) — Agentenleitfaden und Coding Guidelines.
- [`agent-policy.yaml`](agent-policy.yaml) — Schreibrechte und Required Checks.
- [`docs/policies/agent-reading-protocol.md`](docs/policies/agent-reading-protocol.md) — bindendes Lese- und Abbruchprotokoll.

Weitere Referenzen:

- [`docs/architekturstruktur.md`](docs/architekturstruktur.md) — Repo-Architekturüberblick.
- [`docs/techstack.md`](docs/techstack.md) — Stack-Referenz (SvelteKit, Rust/Axum, Postgres + Outbox, JetStream, Caddy).
- [`docs/runbook.md`](docs/runbook.md) — Betrieb, DR/DSGVO-Drills.
- [`docs/datenmodell.md`](docs/datenmodell.md) — Tabellen, Projektionen, Events.
- [`policies/performance.v1.json`](policies/performance.v1.json) — Performance-Budgets.

Kurzprinzip: **„Richtig routen, klein schneiden, sauber messen.“**

## 1. Reale Repo-Topographie

**Auszug wichtiger Hauptverzeichnisse und Hilfsbereiche**  
Vollständige Discovery-Liste: [`repo.meta.yaml:discovery_roots`](repo.meta.yaml).

```text
.github/
  workflows/           # CI/CD
apps/
  api/                 # Rust/Axum HTTP-API
  web/                 # SvelteKit-Frontend
architecture/          # Architektur-Notizen
audit/                 # impl-registry.yaml — Implementierungs-Mapping
configs/               # App-Defaults (app.defaults.yml)
contracts/
  domain/              # JSON-Schema-Domain-Contracts
docs/                  # ADRs, Blueprints, Specs, Reports, Policies, _generated/, tasks/
infra/
  caddy/               # Reverse Proxy
  compose/             # Docker-Compose-Profile
policies/              # Soft-Limits, Performance, Retention, SLOs
scripts/               # Tooling, insbesondere scripts/docmeta/
src/                   # Gemeinsame Sources
tools/                 # Development Tools
ci/                    # Budgets, Smoke-Tests
Root                   # Justfile, Makefile, repo.meta.yaml, AGENTS.md, README.md, Lizenz
```

Noch nicht als reale Zielstruktur zu verwenden: `apps/worker/`, `apps/search/` und gemeinsame Library-Pakete unter `packages/`. Diese Strukturen sind Zielbild oder spätere Gate-Arbeit. Patches dürfen sie nicht nebenbei erfinden.

Details: siehe [`docs/architekturstruktur.md`](docs/architekturstruktur.md).

## 2. Routing-Matrix „Wohin gehört was?“

Nur reale Zielordner. Was nicht existiert, wird hier nicht als Gegenwart aufgelistet.

- **Neue Seite oder Route im UI:** `apps/web/src/routes/...` (`+page.svelte`, `+page.ts`, `+server.ts`).
- **UI-Komponente, Store, Util:** `apps/web/src/lib/...`.
- **Statische Assets:** `apps/web/static/`.
- **Neuer API-Endpoint:** `apps/api/src/routes/...`; reale Route-Module: `accounts`, `auth`, `edges`, `health`, `meta`, `nodes`, `query`.
- **Auth-Logik:** `apps/api/src/auth/...` (Sessions, Passkeys, Tokens, Rate-Limiting, Rollen).
- **Middleware:** `apps/api/src/middleware/...` (Auth, AuthZ, CSRF).
- **Querschnitt:** `apps/api/src/{config,state,mailer,utils}.rs`, `apps/api/src/telemetry/...`.
- **DB-Migrationen:** `apps/api/migrations/` (`YYYYMMDDHHMM__beschreibung.sql`).
- **Compose-Profile:** `infra/compose/*.yml`.
- **Proxy, Headers, CSP:** `infra/caddy/`.
- **CI-Workflow:** `.github/workflows/*.yml`.
- **Performance-Budget:** `policies/performance.v1.json`.
- **Soft-Limits / SLOs:** `policies/`.
- **App-Defaults:** `configs/app.defaults.yml`.
- **Architektur-Entscheidung:** `docs/adr/ADR-xxx__<slug>.md`.
- **Architektur-Blaupause:** `docs/blueprints/<slug>.md`.
- **Spezifikation:** `docs/specs/<slug>.md`.
- **Statusbericht / Diagnose:** `docs/reports/<slug>.md` plus optional dokumentierter `.json`-Zwilling.
- **Runbook:** `docs/runbook.md` oder vorhandene Runbook-Pfade gemäß `docs/index.md`.
- **Domain-Contract:** `contracts/domain/<entity>.schema.json`.
- **Task-Control-Artefakt:** `docs/tasks/...`.
- **Doku-Indexer / Relations-Skript:** `scripts/docmeta/`.

Fachliche Trennungen wie `apps/api/src/domain/`, `apps/api/src/repo/` oder `apps/api/src/events/` sind Zielbild, aber aktuell nicht als reale Zielordner zu verwenden. Neue Unterordner erst nach eigenem Architektur- oder Refactoring-PR einführen — nicht als freie Routing-Entscheidung.

Outbox-Projektoren, Search-Projektoren, DSGVO-/DR-Rebuilder und Search-Adapter entstehen erst mit der entsprechenden Gate-Phase.

## 3. Doku-Rollen und Schreibgrenzen

| Pfad | Rolle | Schreibstatus |
|---|---|---|
| `repo.meta.yaml`, `AGENTS.md`, `agent-policy.yaml`, `docs/policies/*` | Kanonische Wahrheits- und Steuerungsschicht | guarded |
| `contracts/domain/*.schema.json` | Höchste Wahrheits-Präzedenz | guarded, erfordert `contracts-domain-check` |
| `docs/adr/*`, `docs/specs/*`, `docs/blueprints/*` | Normative Spezifikation und Architekturplanung | guarded |
| `docs/reports/*` | Diagnostische Berichte und Statusmatrizen mit Evidenz | guarded |
| `docs/index.md` | Navigation, **keine** Wahrheit | guarded |
| `docs/tasks/*` | Task-Control-Arbeitssteuerung, **keine** Wahrheitsschicht | manuell kuratiert in Phase 2; Statuswechsel nur mit Evidenz |
| `docs/_generated/*` | Diagnose, automatisch generiert | **forbidden** für manuelle Edits |
| `audit/impl-registry.yaml` | Implementierungs-Mapping | dokumentiert; Änderungen nur mit konkretem Zielbeleg und Review |
| `secrets/`, `snapshots/` | Sensitive Daten / Snapshots | **forbidden** |

Vollständige bindende Pfadliste siehe [`agent-policy.yaml`](agent-policy.yaml).

## 4. Task-Control-Artefakte

`docs/tasks/` ist die Arbeitssteuerungs-Schicht. Sie macht offene Arbeit auffindbar, ersetzt aber keine Statusmatrix, keinen Report und keinen Codebeweis.

- [`docs/tasks/README.md`](docs/tasks/README.md) erklärt Rolle, Grenzen und Pflege der Task-Control-Artefakte.
- [`docs/tasks/board.md`](docs/tasks/board.md) ist eine **menschliche Arbeitskarte**, keine Wahrheitsschicht.
- [`docs/tasks/index.json`](docs/tasks/index.json) ist ein **maschinenlesbarer Task-Index**. Manuelle Änderungen sind in Phase 2 erlaubt, solange `curation` klar als `"manual_phase2_seed"` oder vergleichbar markiert ist.
- [`docs/tasks/schema.json`](docs/tasks/schema.json) ist der **Validierungsvertrag**. Änderungen brauchen einen begründeten PR.
- [`docs/reports/optimierungsstatus.md`](docs/reports/optimierungsstatus.md) bleibt die menschliche Statusmatrix für OPT-* Einträge.
- [`docs/reports/optimierungsstatus.json`](docs/reports/optimierungsstatus.json) ist deren maschinenlesbarer Zwilling, kein eigenständiger Statusträger.

Kein Status in `docs/tasks/index.json` oder `docs/reports/optimierungsstatus.json` darf dem Markdown widersprechen. Stille Status-Upgrades sind verboten. `done` erfordert Evidenz.

Validator lokal ausführen:

```bash
python3 -m scripts.docmeta.validate_task_index docs/tasks/index.json
```

Drift-Check lokal ausführen:

```bash
python3 -m scripts.docmeta.generate_task_index --check
```

Task-Control-Artefakte bleiben manuell kuratiert, werden aber durch den `--check`-Modus gegen Drift geprüft (Board ↔ Index ↔ Optimierungsstatus, Evidenz- und Doku-Pfade). Der Check ist ein Prüfmechanismus, keine neue Wahrheitsschicht, und schreibt keine Dateien. Automatische Generierung oder Bot-PRs bleiben eine spätere Entscheidung. Im CI läuft der Check über `.github/workflows/task-index.yml`.

Derselbe Workflow prüft über `scripts/docmeta/agent_entrypoint_smoke.py` zusätzlich die Grund-Synchronität der Agenten-Einstiegspunkte: dass README, `AGENTS.md` und das [Agent Reading Protocol](docs/policies/agent-reading-protocol.md) dieselbe Leseordnung führen, `docs/index.md` sich als Navigation markiert und README/Roadmap keine veralteten Task-Control-Aussagen mehr behaupten. Auch dieser Guard ist nur ein deterministischer Prüfmechanismus ohne Schreibzugriff und keine neue Wahrheitsschicht.

### GitHub-Arbeitsobjekte (Phase 3 — bewusst zurückgestellt)

Issue Forms, PR-Template und Release-Konfiguration werden aktuell nicht eingeführt. Begründung: Ein fixes PR-Template erhöht den Formular-Overhead und kann Agents schlechter machen, weil sie dann Formulartext produzieren statt kontextgenau zu berichten. Der eigentliche Engpass ist die manuelle Drift-Gefahr im Task-Index — nicht fehlende Formulare.

Issue Forms können später separat eingeführt werden, wenn externe Beitragende ohne Projekteinblick aktiv werden. Release-Konfiguration kann separat betrachtet werden, wenn der Release-Prozess stabilisiert ist.

Diese Entscheidung schließt TASK-CTL-002: GitHub-native Arbeitsobjekte bleiben vorerst bewusste Nicht-Implementierung. Neue Dateien unter `.github/ISSUE_TEMPLATE/` oder `.github/pull_request_template.md` brauchen künftig einen belegten Nutzen und einen eigenen PR-Schnitt.

Noch offen für Folge-PRs:

- **Task-Index-Generator und CI-Guard** (TASK-CTL-003) — Drift-Check (`generate_task_index.py --check`) und `.github/workflows/task-index.yml` eingeführt; CI-Lauf-Nachweis steht noch aus
- Implementierungs-Mapping-Ausbau

## 5. Arbeitsweise und Workflow

- Branch-Strategie: kurzes Feature-Branching gegen `main`.
- Pull Requests: klein, thematisch fokussiert, mit klarer Evidenz.
- Commit-Präfixe: `feat(web): …`, `feat(api): …`, `feat(infra): …`, `fix(...)`, `chore(...)`, `refactor(...)`, `docs(adr|runbook|...)`.

PR-Prozess:

1. Lokal mindestens `just check` für schnelle Hygiene-Checks ausführen.
2. Bei Änderungen unter `apps/web/`: zusätzlich `just ci` oder spezifische Web-Checks in `apps/web/` ausführen.
3. Zweck, Scope und „Wie getestet“ im PR kurz erläutern.
4. Bei Architektur- oder Sicherheitsauswirkungen: ADR, Blueprint, Report oder Runbook-Update beilegen oder verlinken.
5. Keine generierten Diagnoseartefakte manuell editieren.

Check-Logik:

- `just check` = schneller lokaler Hygiene-Check: Rust fmt/clippy/test, Demo-Daten, Domain-Contracts, `cargo deny`.
- `just ci` = breiterer CI-Spiegel, insbesondere bei Web/API-Änderungen.
- `make ci-validate` = deterministische Doku-/Validierungsstrecke, Alias zu `make validate`.

CI-Gates können Builds brechen:

- Frontend-Budget aus `policies/performance.v1.json`.
- Lints/Formatter: Web (ESLint/Prettier, `max-warnings=0`), Rust (`cargo fmt`, `cargo clippy -D warnings`).
- Tests (`pnpm test`, `cargo test --locked`).
- Sicherheits- und Konsistenzchecks (`cargo deny`, `docs-guard.yml`, Compose-Smoke).

Generierte Dateien unter `docs/_generated/` sind abgeleitete Diagnoseartefakte. CI kann sie für Beobachtbarkeit regenerieren; Drift wird gemeldet und soll zeitnah behoben oder bewusst dokumentiert werden.

## 6. Domain-Contracts lokal validieren

JSON-Schemas und Beispiele unter `contracts/domain/` prüfen:

Voraussetzungen:

- Node.js ≥ 20
- `ajv-cli` und `ajv-formats`, zum Beispiel:

```bash
pnpm install -g ajv-cli ajv-formats
```

Ausführung:

```bash
just contracts-domain-check
```

oder:

```bash
pnpm run contracts:domain:check
```

Das Skript kompiliert alle Schemas und validiert die Beispiel-Instanzen dagegen.

## 7. Tooling-Differenzierung: lokal vs. CI

- **`scripts/tools/yq-pin.sh`** — lokaler Installer für reproduzierbare Umgebung ohne `sudo`. Lädt `yq` mit Wiederholungen, prüft Checksums und legt das Binary unter `~/.local/bin` ab.
- **CI-Workflows (`.github/workflows/ci.yml`)** — nutzen einen eigenen gepinnten `yq`-Installer, da Runner root-Rechte und ein frisches Dateisystem haben.
- **Link-Prüfung** — im CI läuft `lychee` mit strengen Parametern (`--retry`, niedrige Parallelität). Der nächtliche Workflow (`links.yml`) ist Watchdog mit reduziertem Profil, kein hartes Qualitäts-Gate.

## 8. Konflikt- und Drift-Regel

Wenn Dokumente widersprechen:

1. `repo.meta.yaml` und explizite Contracts prüfen.
2. Danach `AGENTS.md`, `agent-policy.yaml` und Policies prüfen.
3. Danach ADRs, Specs, Blueprints, Reports und Codepfade prüfen.
4. Navigation wie `docs/index.md` nicht als Wahrheit verwenden.
5. Widerspruch nicht glätten. Blockieren, Befund dokumentieren, minimalen Patch vorschlagen.

Wenn ein Pfad nicht existiert, wird er nicht als Gegenwarts-Zielpfad dokumentiert. Zielbilder müssen als Zielbilder markiert werden.
