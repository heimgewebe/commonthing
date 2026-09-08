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
  dea155aeb31ce79bef10c1ab9d83dd6348a343d9 bei 100.000 Nodes und
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
   wartet nur auf **diesen Marker**, prüft danach die Generation neu und startet
   keinen redundanten O(N)-Reload.

Externe oder mehrfache Drift wird nicht verdeckt. Session-Requests und Mutationen
bleiben strict-current.

## Exakte Evidence-Bindung

Terminaler CQ-02-Lastvertrag:

- GitHub Actions Run: `34191668019`
- Job: `101950826233`
- gemessener API-Commit:
  `dea155aeb31ce79bef10c1ab9d83dd6348a343d9`
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
`dea155aeb31ce79bef10c1ab9d83dd6348a343d9` gebunden.

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
| `dea155ae…` | 34191668019 | **15/15, 0 Drops** | **66,6 ms** | **0** | final: Marker-CAS + handoffgebundenes strict-Warten |

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
solange **genau der beobachtete Handoff-Marker** aktiv ist. Dabei reihen sie sich
nicht per `nodes_persist.lock().await` hinter einen möglicherweise bereits
wartenden zweiten Writer ein. Sobald dieser Handoff endet, werden DB- und lokale
Generation neu gelesen.

Der Regressionstest stellt absichtlich einen Writer B vor dem strict-Request in
die Persist-Mutex-Warteschlange und hält B anschließend fest. Der strict-Refresh
muss trotzdem nach Ende von Handoff A fertig werden. Damit ist seine Wartezeit an
den beobachteten Handoff und nicht an die nächste Mutation gebunden.

### 5. Externe Drift bleibt sichtbar

Bei +2, CAS-Mismatch, fehlendem Marker, externer Mutation oder anderer
unerwarteter Generation wird **nicht** vorgespult. Der normale stabile
Full-Reload bleibt der fail-closed Reconciliation-Pfad.

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
- ein Fehler beim optionalen Post-Commit-Versionsreadback macht einen bereits
  committed PATCH nicht nachträglich zum falschen HTTP 500;
- die Projection-Duration-Histogramme reichen bis 30 s und können die historisch
  beobachteten >6-s-Reloads quantilisieren;
- Snapshot-Gauges werden bereits beim Boot initialisiert.

## Finale Messung

### Smoke read-heavy

- Reads: 234.786
- Ø 1,194 ms
- p50 1,025 ms
- p95 2,616 ms
- p99 4,140 ms
- max 24,189 ms
- Read-Fehler: 0
- HTTP 503: 0
- dropped iterations: 0
- Full-Reloads: 0

### Smoke mixed

Reads:

- 241.237
- Ø 1,159 ms
- p50 0,999 ms
- p95 2,519 ms
- p99 3,892 ms
- max 31,528 ms
- Read-Fehler: 0
- HTTP 503: 0

Writes:

- 16/16 erfolgreich
- Ø 47,563 ms
- p50 38 ms
- p95 106,5 ms
- p99 182,1 ms
- max 201 ms
- Write-Fehler: 0
- dropped iterations: 0

Projection:

- Version 1→17, Delta 16
- Full-Reloads: **0**
- stable snapshot retries: 0
- refresh/reload failures: 0

### 100k read-heavy

- 100.000 Nodes / 500.000 Edges
- Reads: 234.270
- Ø 1,191 ms
- p50 1,027 ms
- p95 2,584 ms
- p99 4,046 ms
- max 22,068 ms
- Read-Fehler: 0
- HTTP 503: 0
- dropped iterations: 0
- Full-Reloads: 0
- API Peak Memory: 609.956.659 Bytes
- API Peak CPU: 227,3 %
- max. PostgreSQL-Verbindungen: 12

### 100k mixed — terminaler CQ-02-Beweis

Reads:

- **235.375**
- Ø **1,188 ms**
- p50 1,026 ms
- p95 **2,570 ms**
- p99 **3,971 ms**
- max 28,330 ms
- Read-Fehler: 0
- HTTP 503: 0

Writes:

- **15/15 erfolgreich**
- Ø **35,133 ms**
- p50 27 ms
- p95 **66,6 ms**
- p99 84,52 ms
- max **89 ms**
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

- API Peak Memory: 610.900.378 Bytes
- API Peak CPU: 232,77 %
- max. PostgreSQL-Verbindungen: 11

## Regressionsevidence

Auf dem finalen Runtime-Head bestanden lokal und in der PR-CI:

- API-Lib-Tests: 555 PASS, 0 failed, 10 bewusst ignoriert;
- direkter PostgreSQL-Node-Write-Vertrag: 39/39 PASS;
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
- dass Full-Projections langfristig die Zielarchitektur sind.

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
