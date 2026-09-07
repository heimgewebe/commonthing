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
last_reviewed: 2026-09-07
review_after: 2027-01-07
lang: de
summary: >
  Messgebundener CQ-02-Abschluss für die PostgreSQL-Domain-Projektion. Der
  unveränderte 1k/100k-Mixed-Load-Vertrag belegt auf API-Commit
  28fd95bb81be89d344d3c8d837c15d64448bb8c8 bei 100.000 Nodes und
  500.000 Edges 16/16 erfolgreiche Writes, 0 dropped iterations und 0
  Full-Reloads. Authentifizierte Requests und Mutationen bleiben strict-current;
  nur anonyme sichere Reads dürfen begrenzt die vorherige vollständige
  Projektion verwenden.
relations:
  - type: relates_to
    target: docs/reports/domain-postgres-instance-coherence-decision.md
  - type: relates_to
    target: apps/api/src/state.rs
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

CQ-02 ist technisch geschlossen.

Der ursprüngliche Auftrag war ausdrücklich **messen vor refactoren**. Die
Messung hat ein reales Skalierungsproblem nachgewiesen: Ein PostgreSQL-Write
konnte einen vollständigen O(N)-Reload von Accounts, Nodes und Edges auslösen.
Bei 100.000 Nodes und 500.000 Edges dauerte ein solcher Reload Sekunden und
serialisierte Writer so stark, dass der angebotene Mixed-Load-Vertrag nicht
stabil gehalten wurde.

Die Lösung besteht nicht aus einem größeren Umbau, sondern aus drei schmalen
Koordinationsregeln:

1. anonyme GET/HEAD-Requests dürfen während eines bereits laufenden Reloads die
   vorherige **vollständige** Projektion weiterverwenden;
2. ein isolierter lokaler PostgreSQL-Node-PATCH darf seine exakt bekannte
   Generation V→V+1 lokal fast-forwarden;
3. im winzigen Fenster zwischen DB-Commit und diesem lokalen Fast-Forward darf
   ein anonymer sicherer Read bei exakt +1 Drift keinen redundanten Full-Reload
   starten, solange der vorhandene lokale Node-Write-Mutex den Handoff belegt.

Authentifizierte Requests und Mutationen bleiben strict-current. Externe oder
mehrfache Drift wird nicht übersprungen.

## Exakte Evidence-Bindung

Finaler unveränderter CQ-02-Lastvertrag:

- GitHub Actions Run: `34159375715`
- Job: `101857789348`
- gemessener API-Commit:
  `28fd95bb81be89d344d3c8d837c15d64448bb8c8`
- Workflow: `.github/workflows/domain-projection-load.yml`
- Dataset `scale_100k`: 100.000 Nodes / 500.000 Edges
- Mixed-Dauer: 30 Sekunden
- Reader: 10 VUs
- Writer: open-loop, 0,5 Writes/s
- Fail-closed: jeder dropped iteration, Write-Fehler oder fehlende
  Generation-Fortschritt macht den Job rot

Falls dieser Bericht in einem späteren docs-only Commit liegt, ist **nicht** der
Docs-Commit der gemessene Code. Die Messwahrheit bleibt ausdrücklich an
`28fd95bb81be89d344d3c8d837c15d64448bb8c8` gebunden.

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
Die gemessenen PATCHes laufen damit unter normaler Triggersemantik.

Der Writer patcht bestehende Nodes statt neue Nodes anzulegen. Dadurch bleibt
die Kardinalität konstant und der Benchmark misst Projection-Kohärenz statt
zusätzliche Node-/Origin-Faden-Erzeugung.

## Belegtes Ausgangsproblem

Vor den Architekturänderungen zeigten gültige 100k-Mixed-Messungen
Full-Reloads in der Größenordnung von rund 6,2 Sekunden; mehrere Reloads
summierten sich auf über 30 Sekunden und Writer konnten mehrere Sekunden
blockieren. Damit war die CQ-02-Prämisse erfüllt: Es gab ein reales,
skalierungsabhängiges Architekturproblem und nicht nur theoretisches
Refactoring-Potenzial.

Nach stale-while-refresh, isoliertem Node-PATCH-Fast-Forward und der
Auth-Sicherheitskorrektur blieb auf Head
`0e469b2913328690371545875f224132299ec322` genau ein Restproblem.

Run `34152729385`, 100k mixed:

- 14 erfolgreiche Writes, 0 Write-Fehler
- Writer Ø 1.292,1 ms
- Writer p50 897,5 ms
- Writer p95 3.270,25 ms
- Writer p99 4.099,65 ms
- Writer max 4.307 ms
- **1 dropped iteration**
- Projection-Version 1→15
- **9 Full-Reloads**
- Reload-Zeit gesamt 22,609 s
- 900.000 Node-Zeilen und 4.500.000 Edge-Zeilen durch Reloads erneut geladen

Damit war der einzelne Drop kein bloßes Summarizer-Artefakt: Die Writer-Latenz
lag regelmäßig über dem Zwei-Sekunden-Ankunftsabstand, während gleichzeitig
neun vollständige 100k/500k-Snapshots entstanden.

## Rest-Race und minimaler Fix

`PATCH /nodes/{id}` hält bereits `nodes_persist` über den kritischen lokalen
Abschnitt:

DB-Commit → Cache-Update → DB-Version lesen → exakter lokaler Versions-CAS.

Im Rest-Race konnte ein anonymer Reader nach dem DB-Commit bereits V+1 sehen,
bevor der Writer seinen lokalen Marker von V auf V+1 gesetzt hatte. Dieser
Reader konnte deshalb einen vollständigen Reload starten, obwohl derselbe
lokale Writer die neue Node-Generation bereits vollständig in den Cache
übernahm.

Der finale Fix in `apps/api/src/state.rs` nutzt **keinen neuen globalen Lock**
und führt kein Lock-Upgrade ein. Ein sicherer Read darf nur dann deferren, wenn
alle Bedingungen gleichzeitig gelten:

- Freshness-Modus ist `AllowStaleWhileRefreshing`;
- Node-Write-Source ist PostgreSQL;
- beobachtete DB-Generation ist exakt `local + 1`;
- der bereits existierende `nodes_persist`-Mutex ist gerade belegt.

Bei +2 oder größerer Drift wird normal vollständig reconciliert. Strict-current
Requests nehmen diesen Pfad nie.

## Sicherheitsinvarianten

Der Performance-Fix ändert die Auth-Sicherheitsgrenze nicht.

`apps/api/src/middleware/domain_projection.rs` erlaubt die vorherige vollständige
Projection nur für:

- `GET` oder `HEAD`;
- **ohne** den kanonischen `gewebe_session`-Cookie.

Damit bleiben Requests mit Session-Cookie strict-current, bevor die nachfolgende
Auth-Middleware Account-`disabled` und Rolleninformationen aus der Projection
liest. Mutationen bleiben ebenfalls strict-current.

Weitere Invarianten:

- niemals teilweise geladene Snapshot-Generation sichtbar;
- kein blindes Vorspulen über externe Drift;
- Fast-Forward nur bei exakt einer erwarteten Generation;
- schlägt das Versions-Readback nach bereits committed Write fehl, wird kein
  falscher HTTP-500 für den committed Write erzeugt; die lokale Projection bleibt
  stale und reconciliert später normal;
- keine Lastschwelle, Write-Rate oder Drop-Toleranz wurde gelockert.

## Finale Messung

### Smoke read-heavy

- Reads: 202.811
- Ø 1,365 ms
- p50 1,146 ms
- p95 3,064 ms
- p99 4,836 ms
- max 71,733 ms
- Read-Fehler: 0
- HTTP 503: 0
- dropped iterations: 0
- Full-Reloads: 0

### Smoke mixed

Reads:

- 209.475
- Ø 1,320 ms
- p50 1,113 ms
- p95 2,952 ms
- p99 4,495 ms
- max 36,899 ms
- Read-Fehler: 0
- HTTP 503: 0

Writes:

- 16/16 erfolgreich
- Ø 34,875 ms
- p50 35 ms
- p95 52 ms
- p99 52 ms
- max 52 ms
- Write-Fehler: 0
- dropped iterations: 0

Projection:

- Version 1→17, Delta 16
- Full-Reloads: **0**
- stable snapshot retries: 0

### 100k read-heavy

- 100.000 Nodes / 500.000 Edges
- Reads: 229.736
- Ø 1,210 ms
- p50 1,033 ms
- p95 2,661 ms
- p99 4,180 ms
- max 26,619 ms
- Read-Fehler: 0
- HTTP 503: 0
- dropped iterations: 0
- Full-Reloads: 0
- API Peak Memory: 596.430.029 Bytes
- max. PostgreSQL-Verbindungen: 12

### 100k mixed — terminaler CQ-02-Beweis

Reads:

- 234.691
- Ø 1,187 ms
- p50 1,015 ms
- p95 2,619 ms
- p99 4,033 ms
- max 21,869 ms
- Read-Fehler: 0
- HTTP 503: 0

Writes:

- **16/16 erfolgreich**
- Ø **28,063 ms**
- p50 27,5 ms
- p95 **43,25 ms**
- p99 51,05 ms
- max **53 ms**
- Write-Fehler: 0
- **dropped iterations: 0**

Projection:

- Version 1→17, Delta 16
- **Full-Reloads: 0**
- reload failures: 0
- stable snapshot retries: 0
- während der Messung erneut geladene Rows: 0
- refresh failures: 0

Ressourcen:

- API Peak Memory: 597.688.320 Bytes
- API Peak CPU: 226,4 %
- max. PostgreSQL-Verbindungen: 11

## Wirkung des Restfixes

Vergleich der beiden exakten 100k-Mixed-Runs:

| Kennzahl | `0e469b29…` / Run 34152729385 | `28fd95bb…` / Run 34159375715 |
|---|---:|---:|
| erfolgreiche Writes | 14 | 16 |
| dropped iterations | 1 | **0** |
| Writer Ø | 1.292,1 ms | **28,1 ms** |
| Writer p95 | 3.270,3 ms | **43,3 ms** |
| Writer max | 4.307 ms | **53 ms** |
| Full-Reloads | 9 | **0** |
| Reload-Zeit gesamt | 22,609 s | **0 s** |
| Projection-Version | 1→15 | 1→17 |
| Write-Fehler | 0 | 0 |

Die Daten stützen damit die Rest-Race-Hypothese direkt: Nach Unterdrückung des
redundanten Commit→CAS-Handoff-Reloads verschwinden sowohl die Full-Reloads als
auch der letzte Drop; die Writer-Tail-Latenz fällt um Größenordnungen.

## Zusätzliche Regressionsevidence

Auf dem gemessenen Head war der PostgreSQL-Node-Write-Pfad einschließlich der
ignored Direct-PostgreSQL-Proofs grün. Der neue Test
`safe_projection_read_defers_exact_local_node_generation_handoff` modelliert den
post-commit/pre-CAS-Handoff deterministisch. Die Policy-Tests beweisen zusätzlich:

- nur `AllowStaleWhileRefreshing` darf deferren;
- nur PostgreSQL-Node-Writes;
- nur exakt +1;
- +2 wird nicht verdeckt;
- authentifizierte GET/HEAD-Requests dürfen die vorherige Projection nicht nutzen.

Workspace-Format, Clippy mit `-D warnings` und die API-Unit-Tests waren ebenfalls
grün. Die PR-CI führt zusätzlich PostgreSQL-Integrations-, Auth-, Map-Fullstack-,
CodeQL- und weitere Repository-Gates auf dem exakten Head aus.

## Entscheidung

CQ-02 braucht keinen weiteren Architekturumbau.

Der kleinste belegte Fix erfüllt den unveränderten Lastvertrag und bewahrt die
Sicherheits- und Kohärenzsemantik. Weitere Refactorings in diesem Pfad wären ohne
neue Messung spekulativ und widersprächen dem Auditprinzip „messen vor
optimieren“.

Nächster Audit-Slice nach governed Merge und Reconciliation: **CQ-05**, beginnend
mit einer schmalen Viewport-Data-Controller-Modulgrenze unter bestehenden
Map-Verhaltenstests.
