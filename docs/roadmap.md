---
id: docs.roadmap
title: commonThing — Master-Umsetzungsroadmap
doc_type: roadmap
status: active
created: 2026-05-08
lang: de
summary: Koordiniert aktive Umsetzungsstränge gegen kanonische Verträge, Statusberichte und Tasks.
relations:
  - type: depends_on
    target: docs/process/fahrplan.md
  - type: depends_on
    target: docs/blueprints/auth-roadmap.md
  - type: depends_on
    target: docs/blueprints/auth-persistence-runtime-proof.md
  - type: depends_on
    target: docs/specs/ui-interaction.md
  - type: depends_on
    target: docs/blueprints/map-blaupause.md
  - type: depends_on
    target: docs/specs/map-experience.md
  - type: depends_on
    target: docs/specs/ui-state-machine.md
  - type: depends_on
    target: docs/blueprints/agent-operability-blaupause.md
  - type: depends_on
    target: docs/blueprints/doc-structure-task-control-roadmap.md
  - type: depends_on
    target: docs/blueprints/versionierungs-blaupause.md
  - type: depends_on
    target: docs/blueprints/weltgewebe-os-masterplan.md
  - type: depends_on
    target: docs/reports/weltgewebe-os-foundation-status.md
  - type: depends_on
    target: docs/reports/optimierungsstatus.md
  - type: depends_on
    target: docs/reports/auth-status-matrix.md
  - type: depends_on
    target: docs/reports/map-status.md
---

# commonThing — Master-Umsetzungsroadmap

> **Zweck.** Diese Roadmap ist der **Koordinations-Index** über alle
> aktiven Umsetzungsstränge. Sie ordnet Verträge, Belege und offene Arbeit,
> ist aber **keine** neue Architekturentscheidung und **kein** eigener
> Produktvertrag.
>
> **Abgrenzung.** `docs/process/fahrplan.md` ordnet die infrastrukturellen
> Gates A–D. Diese Datei ordnet die thematischen Stränge.
> `docs/reports/optimierungsstatus.md` führt den belegten Statusbeweis.
> Hier: Reihenfolge & Verlinkung. Dort: Statusmessung mit Nachweisen.

## Statuslegende

- `[ ]` offen
- `[~]` in Arbeit
- `[x]` erledigt (Beleg im Sub-Dokument oder Statusmatrix)
- `[?]` Status unklar — erfordert Belegung in der jeweiligen Statusmatrix

Der eigentliche Wahrheitsbeweis lebt in den Sub-Roadmaps und Statusmatrizen.
Haken hier sind verdichtete Repräsentationen, keine Eigenwahrheiten.

## Leitprinzipien

1. **Kein Haken ohne Beleg.** Verweise auf konkrete Sub-Tasks oder
   Statuszeilen. Kein stilles Glätten.
2. **Sub-Roadmap führt.** Wenn diese Datei und eine Sub-Roadmap divergieren,
   führt die Sub-Roadmap. Diese Datei ist abhängig.
3. **Klein schneiden.** Aufnahme nur dort, wo Koordinationsbedarf zwischen
   Themen besteht. Themen-interne Schritte bleiben in der Sub-Roadmap.
4. **Reihenfolge vor Vollständigkeit.** Diese Datei priorisiert
   Abhängigkeiten zwischen Themen, nicht die Themen-internen Phasen.

## Relations-Hinweis

Diese Roadmap hat bewusst viele ausgehende `depends_on`-Relationen, weil sie
als Koordinations-Index aus Sub-Roadmaps und Statusmatrizen abgeleitet ist.
Ein hoher Outbound-Wert ist hier erwartet und kein eigenständiger Architektur-Hub.
Neue Relationen dürfen nur ergänzt werden, wenn ein Dokument wirklich als
Status- oder Reihenfolgequelle für die Master-Roadmap dient.

## Themen-Übersicht

| Thema | Sub-Roadmap | Statusbeleg |
|---|---|---|
| commonThing OS | [weltgewebe-os-masterplan.md](blueprints/weltgewebe-os-masterplan.md), [kanonische Zielarchitektur](../architecture/commonthing-os.md) | [weltgewebe-os-foundation-status.md](reports/weltgewebe-os-foundation-status.md) |
| Auth | [auth-roadmap.md](blueprints/auth-roadmap.md) | [auth-status-matrix.md](reports/auth-status-matrix.md) |
| Auth-Persistenz (Runtime-Proof) | [auth-persistence-runtime-proof.md](blueprints/auth-persistence-runtime-proof.md) | [ADR-0007](adr/ADR-0007__auth-persistence-production-db-path.md), [optimierungsstatus.md](reports/optimierungsstatus.md) |
| UI | [UI-Interaktionsvertrag](specs/ui-interaction.md), [Zustandsmaschine](specs/ui-state-machine.md) | [Kartenstatus](reports/map-status.md) |
| Karte und Basemap | [Kartenerlebnis](specs/map-experience.md), [Basemap-Blaupause](blueprints/map-blaupause.md) | [Kartenstatus](reports/map-status.md) |
| Agent-Operability | [agent-operability-blaupause.md](blueprints/agent-operability-blaupause.md) | [agent-readiness-audit.md](reports/agent-readiness-audit.md) |
| Agent Safety Control Layer | [blueprint-agent-safety-control-layer.md](blueprints/blueprint-agent-safety-control-layer.md) | Blueprint gemergt; Umsetzung erfolgt in der Task-Control-Schicht: [board.md](tasks/board.md), [index.json](tasks/index.json) (`AGENT-SAFE-001` bis `AGENT-SAFE-008`, `TASK-CTL-004`). `AGENT-SAFE-006` und `AGENT-SAFE-007` sind auf `main`; `AGENT-SAFE-008` setzt den minimalen Generated-Artifact-Kontrollvertrag um. Keine Aussage, dass alle Blueprint-PRs bereits umgesetzt seien. |
| Dokumentationsstruktur & Task-Steuerung | [doc-structure-task-control-roadmap.md](blueprints/doc-structure-task-control-roadmap.md), [Blueprint](blueprints/doc-structure-task-control.md) | Beleg in der Task-Control-Schicht: [tasks/README.md](tasks/README.md), [board.md](tasks/board.md), [index.json](tasks/index.json), Drift-Guard `.github/workflows/task-index.yml`. Phase 2 vorhanden; Phase 4 Check-Modus + CI-Guard vorhanden (CI-Lauf-Nachweis offen, `TASK-CTL-003`); Schreibgenerator/Bot-PRs und Implementierungs-Mapping offen. Kein gesonderter OPT-Eintrag in `reports/optimierungsstatus.md` — Task-Control-Phasen laufen über `docs/tasks/`. |
| Versionierung | [versionierungs-blaupause.md](blueprints/versionierungs-blaupause.md) | [versionierungs-statusgrundlage.md](blueprints/versionierungs-statusgrundlage.md) |
| Optimierungsfront | (kein Sub-Plan) | [optimierungsstatus.md](reports/optimierungsstatus.md), [optimierungsbericht.md](reports/optimierungsbericht.md) |

## Strang commonThing OS

Die Zielarchitektur ist föderiert und Kubernetes-native, ohne den heutigen Compose- und Single-Instance-Zustand als bereits migriert darzustellen. Die ausführliche Reihenfolge steht im [commonThing-OS-Masterplan](blueprints/weltgewebe-os-masterplan.md).

- [~] Welle 0 — Verfassung und Architekturentscheidungen: kanonische Zielarchitektur, ADR-0010 bis ADR-0012, Föderationskern, Masterplan und Statusbericht in diesem Änderungsschnitt.
- [ ] Welle 1 — Multi-Instanz-Wahrheit: State-Audit, Shared Auth State, abgeleitete Caches, Transactional Outbox, idempotente Konsumenten und Zwei-API-Kohärenz.
- [ ] Welle 2 — Kubernetes-native Grundlage: lokale Referenz, kanonische Manifeste, GitOps, Gateway, Netzwerkpolicies, Telemetrie und Previewpfad.
- [ ] Welle 3 — Hochverfügbare Referenzzelle: Datenbank-/Messagingfailover, PITR, Blank-Cluster-Restore und gemessene RTO/RPO.
- [ ] Welle 4 — Föderationskern-Runtime: Zellidentitäten, globale Objektadressen, signierte Ereignisse, Reichweite, Inbox/Outbox, Quarantäne und Konformitätstests.
- [ ] Welle 5 — Zwei-Zellen-Nachbarschaft: unabhängige Testzellen, gemeinsame Räume, zellübergreifende Knoten/Fäden und Partition-Recovery.
- [ ] Welle 6+ — globale rekonstruierbare Projektionen, deklarative Zellprofile, Grabowski-Rechengewebe und Edge-/Offlinezellen nur nach den vorherigen Beweisen.

Der Welle-1-Kohärenzbeweis ist seit dem 16. Juli 2026 erfüllt und der Multi-Instance-Guard aktiv. Produktionsskalierung oder Migration benötigen weiterhin einen eigenen Plattform- und Betriebsbeweis.

## Strang Auth

Reihenfolge: Kanonisierung → Step-up → Persistenz-Runtime-Proof → DbSessionStore.

- [x] Phase 0 — Kanonisierung & Drift-Stopp · [auth-roadmap §4](blueprints/auth-roadmap.md)
- [x] Phase 1 — Ist-vs-Ziel-Beweis · [auth-roadmap §5](blueprints/auth-roadmap.md)
- [x] Phase 2 — Session-/Device-Modell vervollständigen · [auth-roadmap §6](blueprints/auth-roadmap.md)
- [~] Phase 3 — Step-up Auth · Passkey-Register-Grant-Handoff belegt; Passkey Register-Verify ist durch CI belegt ([Run 27487642565](https://github.com/heimgewebe/weltgewebe/actions/runs/27487642565), Commit `cc54460`); Passkey-Login-Flow (`auth/options`, `auth/verify`), Management, UI und dauerhafte Persistenz bleiben offen · [auth-roadmap §7/§8](blueprints/auth-roadmap.md)
- [~] Phase 4 — Auth-Persistenz Runtime-Proof
  - SQL/psql-Migration + psql-basierter PgBouncer-CRUD-Smoke belegt
  - SQLx/Rust-CRUD gegen direkten PostgreSQL-Pfad ist belegt (PROVEN; Session-Tabellen-CRUD-Primitive)
  - SQLx/PgBouncer-Rust-Proof bleibt optionaler Dev-/Spezialpfad und weiterhin nicht belegt (NOT_PROVEN / READY_FOR_PROOF)
  - Zielarchitekturentscheidung geschlossen: Produktion nutzt direkten PostgreSQL-Zugriff via `DATABASE_URL`; PgBouncer ist kein Produktions-Gate
  - Belege: [ADR-0007](adr/ADR-0007__auth-persistence-production-db-path.md),
    [auth-persistence-runtime-proof.md](blueprints/auth-persistence-runtime-proof.md),
    [aktueller Status](reports/optimierungsstatus.md),
    [Direct-SQLx-Proof](proofs/sqlx-postgres-direct-session-crud-proof.md),
    [archivierter Zielarchitektur-Abgleich](reports/auth-persistence-runtime-target-reconciliation.md)
- [x] Phase 5 — `DbSessionStore`-Verdrahtung über vorhandene `SessionBackend`/`SessionOps`-Abstraktion · direkter SQLx/PostgreSQL-Persistenzpfad implementiert (PR #1072)
- [x] Phase 5b — CI-Gate für `db_session_store_persistence`-Test · PROVEN · Run [`26394569642`](https://github.com/heimgewebe/weltgewebe/actions/runs/26394569642), Job [`77692063785`](https://github.com/heimgewebe/weltgewebe/actions/runs/26394569642/job/77692063785), Commit `00a43a009c53c546355a14c08086131bd84cf8ad` (Branch `main`); direkter PostgreSQL-Port `5432` (nicht PgBouncer `6432`); `test db_session_store_persistence ... ok`, `6 passed; 0 failed`
- [ ] Phase 6 — nachgelagerter Teilstand: Cookie/session proof CI PROVEN (begrenzt) · Run [`26455010837`](https://github.com/heimgewebe/weltgewebe/actions/runs/26455010837), Job [`77886363989`](https://github.com/heimgewebe/weltgewebe/actions/runs/26455010837/job/77886363989), headSha `20c7e30136fc5872e286ab17738a64b0d03aec56`; `session_cookie_has_secure_attributes_on_magic_link_consume ... ok`, `session_cookie_insecure_when_auth_cookie_secure_disabled ... ok`, `2 passed; 0 failed`, `PROVEN: cookie/session proof tests passed (phase 6)`; verbleibende Auth-/Browser-/Passkey-Proofs weiterhin offen · [auth-status-matrix.md](reports/auth-status-matrix.md)

## Strang UI

Dauerhafte Regeln stehen in [UI-Interaktionsvertrag](specs/ui-interaction.md), [UI-Zustandsmaschine](specs/ui-state-machine.md) und [Garnrolle, Knoten und Faden](specs/garnrolle-knoten-faden.md).

- [x] Drei globale Zustände `navigation`, `fokus`, `komposition` mit geprüften Invarianten.
- [x] Ein Fokuspanel als Detail- und Handlungsraum; mobil Bottom Sheet, auf breiten Ansichten Seitenpanel.
- [x] Suche und Filter als gegenseitig ausschließende Kartenlinsen.
- [x] URL-Einstieg für `focus`, `lens` und `compose`; Panel-Tabs bleiben lokal.
- [x] Eigene Garnrolle beschreiben und über `not_on_map`, `exact` oder `radius` verorten.
- [x] Knoten weben und zugehörigen Faden im Browserpfad erzeugen.
- [~] Den vollständigen, dauerhaften Produktionsfluss Anmeldung → Garnrolle → Knoten → Faden nach Neuladen belegen.
- [~] Auth-UI-Integration: Step-up und Passkey-Flächen entlang der Auth-Roadmap vervollständigen.

## Strang Karte (Basemap und Kartenwahrheit)

Dauerhafte UX-Regeln stehen in [Kartenerlebnis](specs/map-experience.md); die Basemap-Zielarchitektur in [map-blaupause.md](blueprints/map-blaupause.md). Der aktuelle, zeitgebundene Befund steht im [Kartenstatus](reports/map-status.md).

- [x] Explizite Ladezustände und Ressourcendiagnostik statt stiller Leere.
- [x] Szenenmodell zwischen API-Rohdaten und Rendering.
- [x] Typisierte Kartenentitäten, Fokus, Suche, Filter und degradierte Zustände testseitig belegt.
- [x] PMTiles-Protokoll, lokaler Style und Caddy-Range-Beweise in getrennten Beweisstufen vorhanden.
- [~] Produktiven Basemap-Modus verbindlich entscheiden und gegen die reale Zielruntime prüfen.
- [ ] Echte Vector-Tile-Payload-Lieferung im produktionsnahen Pfad belegen.
- [ ] Visuelle Korrektheit auf repräsentativen Geräten und Zoomstufen prüfen.
- [ ] Kartenleistung mit real wachsendem Gewebe messen; erst danach zusätzliche Verdichtungsmechanismen erwägen.

## Strang Agent-Operability

- [?] Minimaler Action-Layer · Zielbild dokumentiert, Implementierungsstand prüfen · [agent-operability-blaupause.md](blueprints/agent-operability-blaupause.md)
- [?] Agent-Readiness vollständig grün · [agent-readiness-audit.md](reports/agent-readiness-audit.md)
- [~] Agent Safety Control Layer · Blueprint gemergt; Safety-Preflight, Readiness Hard Fail, Claim-Spine, Agent-Contracts, Non-Ideal-Guard, Handoff-Validierung und read-only Dry-Run sind ueber `AGENT-SAFE-001` bis `AGENT-SAFE-006` belegt; `AGENT-SAFE-007` ist als PR #1265 auf `main`; `AGENT-SAFE-008` implementiert den minimalen Generated-Artifact-Kontrollvertrag. Universelle Failure-Evidence, externe Attestierung und Write Mode bleiben offen · [blueprint-agent-safety-control-layer.md](blueprints/blueprint-agent-safety-control-layer.md)
- [x] Blueprint-/Roadmap-Registration-Guard · Guard-Mechanismus umgesetzt in `TASK-CTL-004` · [board.md](tasks/board.md)
- [x] Planning-Registration-Findings triagiert und Strict-Ratchet aktiv · `TASK-CTL-005`: 8 bestehende Findings triagiert (6 via Frontmatter-Relation registriert, 2 als `deprecated` terminal), Guard läuft blockierend in `--mode strict` · [board.md](tasks/board.md); archivierter Ausgangsbefund: [planning-registration-findings.md](reports/planning-registration-findings.md)

## Strang Versionierung

- [?] Versionierungsmodell festziehen · Blueprint ist draft · [versionierungs-blaupause.md](blueprints/versionierungs-blaupause.md)
- [?] Statusgrundlage gegen Repo prüfen · Statusgrundlage ist active, aber Master-Status bleibt nur aus Sub-Beleg ableitbar · [versionierungs-statusgrundlage.md](blueprints/versionierungs-statusgrundlage.md)

## Strang Optimierungsfront

- Verfolgt direkt in [optimierungsstatus.md](reports/optimierungsstatus.md). Diese Roadmap dupliziert die Tickets nicht.

## Themenübergreifende Reihenfolge (Was vor Was)

1. **commonThing-OS-Verfassung und Multi-Instance-State-Audit** vor jeder horizontalen Skalierung, Kubernetes-Produktionsmigration oder öffentlichen Föderation.
2. **Kubernetes-/GitOps-Grundlage** auf dem belegten Shared-State-, Outbox- und Multi-Instance-Vertrag aufbauen.
3. **Zwei-API-Kohärenzbeweis** vor Kubernetes-Replica-Skalierung.
4. **Kubernetes-Reproduzierbarkeit und Restore-Proof** vor einer HA-Behauptung.
5. **Föderationskern und Konformitätstests** vor dem Zwei-Zellen-Pilot.
6. **Direkter SQLx/Postgres-Persistenzpfad-Nachweis** vor produktivem `DbSessionStore`; PgBouncer-Proofs sind nach ADR-0007 optionaler Dev-/Spezialpfad.
7. **Auth-Phase 3 (Step-up)** vor **UI Auth-Integration**.
8. **Kartenklarheit Phase 5 (souveräne Pipeline)** vor **Phase 6 (Wahrheitsbeweis)**.
9. **Basemap-Roadmap Phase 3 (Runtime-Integration)** überlappt mit
   **Kartenklarheit Phase 5/6** — gemeinsame Schnittmenge: PMTiles-Auslieferung
   über Caddy mit reproduzierbarem Artefakt.
10. **Versionierungs-Statusgrundlage** vor jedem Feature, das semver-relevante
   Schemata berührt (Domain-Contracts).

## Pflege-Regeln

- Diese Datei ist nur dann anzupassen, wenn eine Sub-Roadmap einen Phasen-
  oder Reihenfolge-Wechsel bekommt. Themen-interne Detail-Tasks gehören
  **nicht** hierher.
- Statuswechsel hier nur, wenn die zugehörige Sub-Roadmap oder Statusmatrix
  den Wechsel bereits belegt.
- Bei Drift zwischen dieser Datei und einer Sub-Roadmap: Sub-Roadmap führt;
  diese Datei nachziehen oder einen Drift-Eintrag in
  [optimierungsstatus.md](reports/optimierungsstatus.md) anlegen.
