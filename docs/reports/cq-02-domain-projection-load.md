---
id: reports.cq-02-domain-projection-load
title: "CQ-02 — PostgreSQL Domain Projection Mixed-Load Proof"
doc_type: report
status: active
lifecycle_state: active
lifecycle: proof
owner_task: WELTGEWEBE-OS-002
canonicality: evidence
created: 2026-09-07
last_reviewed: 2026-09-08
review_after: 2027-01-08
lang: de
summary: >
  Messgebundener CQ-02-Abschluss für die PostgreSQL-Domain-Projektion. Der
  unveränderte 1k/100k-Mixed-Load-Vertrag belegt auf API-Commit
  9be2048de8f56ee65184cba113016336f6742602 bei 100.000 Nodes und
  500.000 Edges 15/15 erfolgreiche PATCHes, 0 dropped iterations, 0 HTTP 503
  und 0 Full-Reloads. Der lokale V→V+1-Handoff ist transaktionsgebunden vor
  COMMIT markiert; Session-Requests und Mutationen bleiben strict-current.
relations:
  - type: relates_to
    target: docs/reports/domain-postgres-instance-coherence-decision.md
  - type: relates_to
    target: apps/api/src/state.rs
  - type: relates_to
    target: apps/api/src/domain_db.rs
  - type: relates_to
    target: apps/api/src/middleware/domain_projection.rs
  - type: relates_to
    target: apps/api/src/routes/nodes.rs
  - type: relates_to
    target: apps/api/tests/db_domain_node_write_path.rs
  - type: relates_to
    target: .github/workflows/domain-projection-load.yml
  - type: relates_to
    target: scripts/performance/domain_projection_load.py
  - type: relates_to
    target: scripts/performance/domain_projection_k6.js
---

# CQ-02 — PostgreSQL Domain Projection Mixed-Load Proof

## Kurzurteil

CQ-02 ist für den **gemessenen Single-Instance-Node-PATCH-Vertrag** technisch
geschlossen.

Der Auftrag war ausdrücklich **messen vor refactoren**. Die Messung hat ein
reales O(N)-Problem nachgewiesen: Ein PostgreSQL-Write konnte einen vollständigen
Reload von Accounts, Nodes und Edges auslösen. Bei 100.000 Nodes und 500.000
Edges dauerte dieser Pfad Sekunden und drückte Writer unter angebotener Last aus
dem Takt.

Die finale Lösung bleibt schmal:

1. anonyme GET/HEAD-Requests ohne den kanonischen `gewebe_session`-Cookie dürfen
   während eines bereits laufenden Generation-Checks/Reloads die vorherige
   **vollständige** Projektion weiterverwenden;
2. ein isolierter lokaler PostgreSQL-Node-PATCH darf genau eine nachgewiesene
   Generation V→V+1 lokal fast-forwarden;
3. dieser lokale Handoff wird nicht aus `nodes_persist` abgeleitet, sondern
   **innerhalb derselben PostgreSQL-Transaktion** nach dem Node-UPDATE/Trigger und
   vor COMMIT markiert, solange die Transaktion die Projektionsversionszeile
   gesperrt hält;
4. ein strict-current Request, der exakt diesen lokalen V+1-Handoff sieht,
   wartet ausschließlich auf **diesen Marker**. Die kumulierte tatsächliche
   Marker-Wartezeit ist auf 1 Sekunde begrenzt; danach wird fail-closed abgebrochen.
   Nach Marker-Clear werden DB-, Marker- und lokale Generation neu klassifiziert,
   ohne redundanten O(N)-Reload.

Externe oder mehrfache Drift wird nicht verdeckt. Session-Requests und Mutationen
bleiben strict-current.

## Exakte Evidence-Bindung

Terminaler CQ-02-Lastvertrag:

- GitHub Actions Run: `34223676110`
- Job: `102052513492`
- gemessener API-Commit:
  `9be2048de8f56ee65184cba113016336f6742602`
- Workflow: `.github/workflows/domain-projection-load.yml`
- Dataset `scale_100k`: 100.000 Nodes / 500.000 Edges
- Mixed-Dauer: 30 Sekunden
- Reader: 10 VUs
- Writer: open-loop, 0,5 PATCHes/s
- gemessene API-Instanzen: **1**
- `multi_instance_load_proven`: **false**

Der Summarizer ist fail-closed. Ein CQ-02-Lauf wird rot bei:

- dropped iterations;
- Read-/Write-Fehlern oder HTTP 503;
- fehlenden erfolgreichen PATCHes;
- inkonsistenter Write-Zählung;
- `version_delta != write_successes` im Mixed-Lauf;
- Refresh- oder Reload-Fehlern;
- **jedem Full-Reload** (`--max-reloads 0`);
- Stable-Snapshot-Retry im Zero-Reload-Vertrag;
- Commit-Mismatch zwischen erwarteter und tatsächlich gemessener API-Revision.

Wenn dieser Bericht in einem späteren docs-only Commit liegt, ist der Docs-Commit
**nicht** der gemessene Runtime-Code. Die Messwahrheit bleibt an
`9be2048de8f56ee65184cba113016336f6742602` gebunden.

## Messpfad

Der Harness misst vier Kombinationen:

| Profil | Daten | Last |
|---|---:|---|
| smoke | 1.000 Nodes / 5.000 Edges | 30 s read-heavy |
| smoke | 1.000 Nodes / 5.000 Edges | 30 s mixed |
| scale_100k | 100.000 Nodes / 500.000 Edges | 30 s read-heavy |
| scale_100k | 100.000 Nodes / 500.000 Edges | 30 s mixed |

Die Fixture ist vorbestehender Datenbestand. Beim Bulk-Import werden **nur** die
beiden Projection-Outbox-/Versions-Trigger `domain_nodes_outbox` und
`domain_edges_outbox` temporär unterdrückt. Andere Produkttrigger bleiben aktiv.
Vor der Messung werden beide Projection-Trigger wieder aktiviert und geprüft.
Die gemessenen PATCHes laufen unter normaler Triggersemantik.

Der Writer patcht bestehende Nodes. Dadurch bleibt die Kardinalität konstant und
der Benchmark misst Projektionskohärenz statt zusätzliche Node-/Faden-Erzeugung.
Für jeden Mixed-Lauf werden acht vorhandene Node-IDs fail-closed aus der Fixture
ermittelt.

Die Ressourcen- und Connection-Sampler laufen mit `workload_duration + 20 s`,
damit Docker-/k6-Anlauf das Messfenster nicht abschneiden kann. Der Workflow
wartet derzeit auf das natürliche Ende dieser Sampler. Ein kooperatives
Stop-and-Flush zur Einsparung der Restpufferzeit ist als eigener Bureau-Kandidat
`candidate-7b884f9b21757d7e450a0b03` erfasst; rohes `SIGINT` ist kein zulässiger
Ersatz, weil die Sampler bislang keine Receipt-Flush-Garantie für Signale haben.

## Belegtes Ausgangsproblem und Iterationen

Die Entwicklung ist absichtlich als Messfolge erhalten:

| Runtime | Run | 100k mixed | Writer p95 | Full-Reloads | Aussage |
|---|---:|---:|---:|---:|---|
| `0e469b29…` | 34152729385 | 14 Writes, 1 Drop | 3.270,3 ms | 9 | O(N)-Problem belegt |
| `28fd95bb…` | 34159375715 | 16/16, 0 Drops | 43,25 ms | 0 | performant, später zu breite Handoff-Erkennung gefunden |
| `d9a47e50…` | 34187986270 | 15/15, 0 Drops | 3.306,5 ms | 4 | expliziter Marker war nach COMMIT noch zu spät |
| `636b6746…` | 34190724808 | 16/16, 0 Drops | 49,25 ms | 0 | transaktionsgebundener Pre-COMMIT-Marker schließt Scheduler-Lücke |
| `dea155ae…` | 34191668019 | 15/15, 0 Drops | 66,6 ms | 0 | Marker-CAS + handoffgebundenes strict-Warten; später weitere Cache-Races gefunden |
| `35284a94…` | 34210014972 | CQ-02-Vertrag grün | — | 0 | Zwischenhead vor späterer Cache-Kohärenzprüfung |
| `19672426…` | 34215556861 | CQ-02-Vertrag grün | — | 0 | externe Vor-Drift darf keinen einzelnen V+2-Node in einen V-Cache publizieren |
| `22d2eea…` | 34218911401 | CQ-02-Vertrag grün | — | 0 | Marker-before-local Load-Ordering + 1-s-Timeout bei festhängendem Handoff |
| `af8b5198…` | 34221412242 | **16/16, 0 Drops** | **148,75 ms** | **0** | stale DB read bei lokal bereits publiziertem V+1 wird billig bestätigt; Restore-Regression ergänzt |
| `9be2048d…` | 34223676110 | **15/15, 0 Drops** | **58,3 ms** | **0** | finaler Runtime-Head: maximal 1 s kumulierte tatsächliche Marker-Wartezeit |

Der zwischenzeitliche Run `34187986270` war besonders wichtig: Er war unter
dem damaligen Harness formal grün, obwohl vier Full-Reloads und Writer-Tails im
Sekundenbereich zurückgekehrt waren. Deshalb ist `0 Full-Reloads` jetzt selbst
eine harte Acceptance-Bedingung des Summarizers.

## Finaler Handoff-Vertrag

### 1. Warum `nodes_persist` nicht der Beweis ist

`nodes_persist` serialisiert lokale Node-Persistenz im Prozess. Der Mutex wird
aber bereits **vor** dem PostgreSQL-Commit gehalten. Er kann deshalb nicht
beweisen, dass ein beobachtetes DB-V+1 zu unserem lokalen PATCH gehört. Ein
fremder Writer könnte V+1 bereits committed haben, während unser lokaler PATCH
noch an einer Row-Lock- oder Query-Stelle wartet.

Anonyme Reads klassifizieren einen lokalen Handoff daher **nicht** anhand dieses
Mutexes.

### 2. Transaktionsgebundener Marker vor COMMIT

`patch_node_in_postgres_with_projection_precommit` nutzt eine Eigenschaft des
bestehenden PostgreSQL-Vertrags:

- das reale Node-UPDATE feuert `weltgewebe_enqueue_domain_event`;
- der Trigger erhöht `domain_projection_state.version` in **derselben
  Transaktion**;
- diese Änderung sperrt die Singleton-Versionszeile bis zum COMMIT;
- nach dem Trigger kann die Transaktion ihre eigene neue Version lesen, während
  andere Connections sie noch nicht als committed V+1 sehen können.

Nur bei einem echten UPDATE liefert der Hook `Some(transaction_version)`. Ein
semantischer No-op-PATCH liefert `None` und beansprucht keine lokale Generation.

Ist `transaction_version == local + 1`, versucht der Prozess per
`compare_exchange(NO_HANDOFF, expected)` genau diesen Handoff zu markieren. Ein
bereits aktiver Marker wird **nicht überschrieben**. Der RAII-Guard bleibt über
COMMIT, Cache-Publikation und den lokalen Versions-Fast-Forward bestehen; sein
`Drop` löscht nur den von ihm selbst erwarteten Marker per CAS.

Damit existiert weder das alte „Mutex gehalten = unser V+1“-Problem noch das
spätere Scheduler-Fenster „COMMIT sichtbar, Marker noch -1“.

### 3. Anonyme sichere Reads

Ein anonymer GET/HEAD ohne `gewebe_session` darf bei

`handoff_version == observed_db_version == local_version + 1`

die vorherige vollständige Projektion für diesen Request weiterverwenden. Das
ist ein bewusst begrenztes stale-while-handoff-Fenster; niemals wird eine
teilweise geladene Generation veröffentlicht.

Ist der Single-Flight-Koordinator bereits durch einen Generation-Check oder
Reload belegt, dürfen weitere anonyme sichere Reads ebenfalls die vorherige
vollständige Projektion verwenden. Die Metrik `refresh_deferred` zählt derzeit
beide Situationen und ist deshalb **keine reine Reload-Alarmmetrik**.

### 4. Strict-current Requests

Mutationen und Requests mit `gewebe_session` sind strict-current. Sehen sie exakt
den markierten lokalen V+1-Handoff, starten sie nicht spekulativ einen
Full-Reload. Sie halten den bestehenden Single-Flight-Koordinator und warten nur,
solange **genau der beobachtete Handoff-Marker** aktiv ist. `nodes_persist` wird
weder als Herkunftsbeweis noch als Completion-Signal verwendet.

Die Wartezeit ist liveness-bounded: Über eine Kette unmittelbar folgender lokaler
Handoffs werden höchstens 1 Sekunde **tatsächlicher Marker-Wartezeit** kumuliert.
PostgreSQL-Roundtrips zwischen bereits abgeschlossenen Handoffs zählen nicht in
dieses Budget. Bleibt ein Marker hängen, endet der strict Refresh fail-closed mit
einem Fehler statt unbegrenzt zu blockieren oder unter behaupteter Ownership
spekulativ voll zu laden. Nach jedem Marker-Clear startet die billige
Klassifikation erneut.

Für die Handoff-Ende-Klassifikation wird der Marker per Acquire **vor** der
lokalen Projection-Version geladen. Der Writer publiziert die lokale Version
vor dem CAS-Clear des RAII-Guards. Wer das Clear beobachtet, muss deshalb beim
anschließenden Versionsload auch die vorangegangene Veröffentlichung sehen.

Die Regressionen stellen sowohl einen wartenden zweiten Writer als auch einen
absichtlich festhängenden Marker nach. Der erste darf das Warten auf Handoff A
nicht verlängern; der zweite muss innerhalb der 1-s-Grenze fail-closed enden.

### 5. Externe Drift und lokale Ahead-Races bleiben konservativ

Bei +2, CAS-Mismatch, fehlendem Marker, externer Mutation oder anderer
unerwarteter Generation wird **nicht** vorgespult. Existierte fremde Drift bereits
vor dem lokalen PATCH und erzeugt unser PATCH dadurch V+2, publiziert der Handler
seinen einzelnen Node **nicht** in den alten V-Cache. Die Prozessprojektion bleibt
auf der letzten nachweislich vollständigen Generation, bis normale Reconciliation
Accounts, Nodes und Edges gemeinsam austauscht.

Die DB-Version wird vor den lokalen Atomics gelesen. Deshalb kann ein Writer V+1
vollständig publizieren, nachdem der Reader noch V aus PostgreSQL gesehen hat.
Wenn `local_version > observed`, bestätigt ein zweiter billiger DB-Read diesen
Fall: Hat PostgreSQL inzwischen zur lokalen Version aufgeholt, ist kein O(N)-Reload
nötig. Bleibt die niedrigere DB-Generation stabil, fällt der Pfad bewusst in die
normale Reconciliation; damit wird ein aktuell noch niedriger Restore/PITR-Stand
nicht von einem numerisch neueren Prozesscache überstimmt.

Das hat eine bewusste Konsequenz: Wenn ein anonymer sicherer Read bei echter
externer Drift selbst der Single-Flight-Owner wird, kann **dieser eine Request**
auf die kohärente Reconciliation warten. Parallel eintreffende anonyme sichere
Reads sehen den belegten Single-Flight-Koordinator und dürfen weiter den alten
vollständigen Snapshot verwenden. Fremde Wahrheit wird also nicht als angeblich
lokaler Handoff versteckt.

## Sicherheitsinvarianten

`apps/api/src/middleware/domain_projection.rs` erlaubt die vorherige vollständige
Projection nur für:

- `GET` oder `HEAD`;
- **ohne** den kanonischen `gewebe_session`-Cookie.

Der aktuelle Auth-Vertrag verwendet diesen Cookie als kanonische
Request-Authentifizierung. Würde später ein weiterer Auth-Mechanismus wie Bearer
Auth eingeführt, muss dieser Klassifikator vor dessen Aktivierung erweitert
werden.

Weitere Invarianten:

- Session-Requests und Mutationen bleiben strict-current;
- nie wird eine teilweise geladene Snapshot-Generation sichtbar;
- kein blindes Vorspulen über externe Drift;
- der lokale Marker ist single-owner per CAS;
- No-op-PATCHes beanspruchen keine Generation;
- Fast-Forward nur bei exakt einer erwarteten Node-PATCH-Generation;
- Single-Node-Cache-Publish nur, wenn die lokale Transaktion die komplette nächste
  Generation tatsächlich selbst beweist;
- Marker wird beim Refresh vor der lokalen Version gelesen;
- strict Handoff-Warten ist markergebunden und auf 1 s tatsächliche Wartezeit begrenzt;
- ein Fehler beim optionalen Post-Commit-Versionsreadback macht einen bereits
  committed PATCH nicht nachträglich zum falschen HTTP 500;
- die Projection-Duration-Histogramme reichen bis 30 s und können die historisch
  beobachteten >6-s-Reloads quantilisieren;
- Snapshot-Gauges werden bereits beim Boot initialisiert.

## Finale Messung

### Smoke read-heavy

- Reads: 184.475
- Ø 1,509 ms
- p50 1,290 ms
- p95 3,296 ms
- p99 5,062 ms
- max 44,938 ms
- Read-Fehler: 0
- HTTP 503: 0
- dropped iterations: 0
- Full-Reloads: 0

### Smoke mixed

Reads:

- 189.958
- Ø 1,459 ms
- p50 1,253 ms
- p95 3,179 ms
- p99 4,805 ms
- max 23,674 ms
- Read-Fehler: 0
- HTTP 503: 0

Writes:

- 16/16 erfolgreich
- Ø 36,313 ms
- p50 35 ms
- p95 51,25 ms
- p99 51,85 ms
- max 52 ms
- Write-Fehler: 0
- dropped iterations: 0

Projection:

- Version 1→17, Delta 16
- Full-Reloads: **0**
- stable snapshot retries: 0
- refresh/reload failures: 0

### 100k read-heavy

- 100.000 Nodes / 500.000 Edges
- Reads: 185.393
- Ø 1,498 ms
- p50 1,285 ms
- p95 3,249 ms
- p99 4,989 ms
- max 43,184 ms
- Read-Fehler: 0
- HTTP 503: 0
- dropped iterations: 0
- Full-Reloads: 0
- API Peak Memory: 595.381.453 Bytes
- API Peak CPU: 212,72 %
- max. PostgreSQL-Verbindungen: 12

### 100k mixed — terminaler CQ-02-Beweis

Reads:

- **186.655**
- Ø **1,485 ms**
- p50 1,275 ms
- p95 **3,209 ms**
- p99 **4,825 ms**
- max 26,474 ms
- Read-Fehler: 0
- HTTP 503: 0

Writes:

- **15/15 erfolgreich**
- Ø **35,8 ms**
- p50 32 ms
- p95 **58,3 ms**
- p99 75,66 ms
- max **80 ms**
- Write-Fehler: 0
- **dropped iterations: 0**

Projection:

- Version **1→16**, Delta **15** = 15 erfolgreiche PATCHes
- **Full-Reloads: 0**
- reload failures: 0
- refresh failures: 0
- stable snapshot retries: 0
- während der Messung erneut geladene Rows: 0

Ressourcen:

- API Peak Memory: 596.744.602 Bytes
- API Peak CPU: 212,91 %
- max. PostgreSQL-Verbindungen: 11

## Regressionsevidence

Auf dem finalen Runtime-Head bestanden lokal und in der PR-CI:

- API-Lib-Tests: 555 PASS, 0 failed, 10 bewusst ignoriert;
- direkter PostgreSQL-Node-Write-Vertrag: **44/44 PASS**;
- Clippy mit `-D warnings`;
- Rustfmt und `git diff --check`;
- CQ-02-Harness-Vertrag einschließlich negativer Fail-Closed-Fälle;
- PostgreSQL-Integrationssuite und direkter Node-Write-Pfad in GitHub Actions;
- Auth-/Governance-Proofs und CodeQL.

Die Node-Write-Regressionen beweisen insbesondere:

- exakter lokaler +1-Fast-Forward;
- externe/concurrent Drift wird nicht fast-forwarded;
- anonymer safe read defert nur beim expliziten lokalen Handoff;
- strict refresh wartet auf den aktuellen Handoff statt O(N) zu laden;
- ein bereits wartender zweiter Writer verlängert diesen strict-Handoff nicht;
- ein zweiter Handoff-Marker überschreibt den aktiven Marker nicht;
- ein festhängender exakter Handoff endet bounded/fail-closed statt unendlich zu warten;
- externe Vor-Drift verhindert partielles Single-Node-Publish in eine alte Generation;
- ein lokal bereits publiziertes V+1 nach stale DB-Read löst keinen unnötigen Reload aus;
- eine stabil niedrigere DB-Generation erzwingt Reconciliation;
- ein No-op-PATCH erhält keinen lokalen Generationsmarker.

## Bewusste Grenzen

CQ-02 beweist **nicht**:

- Multi-Instance-Performance. Der Last-Harness startet genau eine API-Instanz;
- dass Node-Create, Replace oder Delete denselben V→V+1-Fast-Forward besitzen.
  Diese Pfade können mehrere Triggergenerationen erzeugen und bleiben beim
  normalen Reconciliation-Vertrag;
- dass der gemeinsame Check-/Reload-Mutex optimal für Telemetrie oder sehr hohe
  Read-Raten ist;
- dass `MAX_SWAP_ATTEMPTS = 5` unter stetiger Fremdmutation die optimale
  Retry-/Backoff-Strategie ist;
- dass Full-Projections langfristig die Zielarchitektur sind;
- vollständige Restore/PITR-Identität über wiederverwendbare Versionsnummern. Der
  jetzige Regressionstest beweist die stabile **niedrigere** DB-Generation; eine
  echte Epoch-/UUID-Identität für Restores bleibt T049.

Die breitere Beseitigung vollständiger Prozessprojektionen, gezielte
Invalidierung, Multi-Instance-Skalierung und begrenzte DB-Abfragen sind bereits
im Bureau-Task `WELTGEWEBE-OS-V1-T049` gebündelt. Die unscharfe
`refresh_deferred`-Metrik gehört in denselben Architekturpfad.

## Entscheidung

Für CQ-02 ist **kein weiterer Cache-Umbau** gerechtfertigt. Der gemessene
Bottleneck ist ohne neue globale Sperre geschlossen, und die Review-Races sind
auf die tatsächliche PostgreSQL-Transaktionsgrenze gebunden.

Optimierungsgrad: **hoch für den gemessenen Node-PATCH-Bottleneck, bewusst eng
für die Gesamtarchitektur**. Der zentrale Trade-off bleibt sichtbar: anonyme
sichere Reads dürfen in engen Koordinationsfenstern einen älteren vollständigen
Snapshot sehen; Session-Requests, Mutationen und externe Drift bleiben
strict/fail-closed.

Nächster Audit-Slice nach governed Merge und Reconciliation: **CQ-05**. Die
größere Projektionsarchitektur bleibt unabhängig davon T049.
