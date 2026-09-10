---
id: blueprints.domain-data-postgres-cutover
title: Domain Data PostgreSQL Cutover
doc_type: blueprint
status: active
lang: de
canonicality: planning
summary: >
  Status- und Rückfallvertrag für OPT-ARC-001: PostgreSQL ist im
  Produktionsvertrag die Domänenwahrheit; JSONL bleibt lokaler,
  Legacy- und Rollbackpfad ohne versteckte Doppelwahrheit.
relations:
  - type: relates_to
    target: docs/tasks/board.md
  - type: relates_to
    target: docs/tasks/index.json
  - type: relates_to
    target: docs/reports/optimierungsstatus.md
  - type: relates_to
    target: docs/reports/domain-account-write-path-proof.md
  - type: relates_to
    target: docs/reports/domain-postgres-instance-coherence-decision.md
  - type: relates_to
    target: docs/reports/domain-node-write-path-proof.md
  - type: relates_to
    target: docs/reports/optimierungsbericht.md
  - type: relates_to
    target: docs/specs/contract.md
  - type: relates_to
    target: docs/specs/list-pagination-api.md
  - type: relates_to
    target: apps/api/src/routes/nodes.rs
  - type: relates_to
    target: apps/api/src/routes/edges.rs
  - type: relates_to
    target: apps/api/src/routes/accounts.rs
  - type: relates_to
    target: apps/api/src/state.rs
---

# Domain Data PostgreSQL Cutover

## Problemstellung

OPT-ARC-001 ist nicht einfach „PostgreSQL verwenden“. Es geht um einen
kontrollierten Persistenzvertrag für die Domänendaten `nodes`, `edges` und
`accounts`.

Der belegte Repository- und Produktionsvertrag nach der Remediation vom
2026-07-12:

- PostgreSQL-Domain-Tabellen und Migrationen für nodes, edges und accounts existieren.
- `commonserver` ist in Compose, Runtime-Dokumentation und `.env.prod.example`
  auf PostgreSQL als Lese- und Schreibwahrheit für Accounts/Garnrollen,
  Knoten und Fäden ausgerichtet.
- JSONL bleibt lokaler Default für Rückwärtskompatibilität und ein
  expliziter Legacy-, Import-/Export- oder Rollbackpfad. Es ist keine offene
  Produktions-Cutover-Wahrheit mehr.
- PostgreSQL-Read-Path existiert hinter Domain-Read-Konfiguration.
- PostgreSQL-Write-Paths existieren für:
  - `POST /accounts`
  - `POST /nodes`
  - `PATCH /nodes`
  - `PUT /nodes`
  - `DELETE /nodes`
  - `POST /edges`
- `APP_CONFIG_PATH` ist fail-closed: eine explizit gesetzte,
  fehlerhafte Config fällt nicht still auf Defaults zurück.
- Neue PostgreSQL-Account-Create-Zeilen persistieren eine stabile
  `webauthn_user_id`; Cache und Reload erhalten dieselbe UUID.
- Lokale Runtime-Caches bestehen weiter.
- Sessions und Passkey-Credentials gehören im Produktionspfad ebenfalls zur
  PostgreSQL-Datensicherung und Restore-Prüfung.
- Frische Liveaussagen über `commonserver` brauchen weiterhin Runtime-Evidence
  mit Datum; diese Blaupause schreibt keine Runtimewerte zeitlos fest.

Diese Blaupause ordnet deshalb nicht mehr einen offenen Produktions-Cutover,
sondern die verbleibenden Belege, Prüfregeln und Rückfalllogik um den
PostgreSQL-Produktionsvertrag.

## Verifizierter Ist-Zustand

| Domain | Lokaler Default | Produktionsvertrag | Status |
|---|---|---|---|
| nodes | JSONL read/write | PostgreSQL Read + `POST /nodes` + `PATCH /nodes/{id}` + `PUT /nodes/{id}` + `DELETE /nodes/{id}` | Produktionswahrheit laut Repo-Vertrag; Livebeleg separat |
| edges | JSONL read/legacy | PostgreSQL Read + `POST /edges` | Produktionswahrheit laut Repo-Vertrag; Edge-Referenzpolitik offen |
| accounts | JSONL read/create | PostgreSQL Read + `POST /accounts` | Produktionswahrheit laut Repo-Vertrag; weitere Auth-Mutationen offen |

Zusatzdetails:

- `nodes`: Schema/Backfill/Read-Path proof-geführt; opt-in Node-Create,
  Node-Patch, Node-Replace und Node-Delete vorhanden; nicht Default.
- `edges`: Schema/Backfill/Read-Path proof-geführt; opt-in Edge-Create
  vorhanden; nicht Default.
- `accounts`: Schema/Backfill/Read-Path proof-geführt; opt-in Account-Create
  vorhanden; neue PostgreSQL-Creates persistieren `webauthn_user_id`.

Zusatzbefund:

- `ApiState` hält weiterhin prozesslokale In-Memory-Caches.
- PostgreSQL ist im Produktionsvertrag Domain-Wahrheit; lokale Defaults bleiben
  aus Rückwärtskompatibilität JSONL.
- Config-Gates müssen explizit gesetzt werden; fehlerhafte `APP_CONFIG_PATH`-Konfigurationen fail-closed.
- PostgreSQL-Write-Slices sind getrennt implementiert:
  - E-A Account-Create
  - E-B Node-Create, Node-Patch, Node-Replace und Node-Delete
  - E-C Edge-Create
- Account-Create, Step-up-E-Mail, normalisierte E-Mail-Eindeutigkeit und der
  PostgreSQL-Passkey-Store sind implementiert und datenbankgetestet.
- Offen bleiben der Legacy-Backfill und ein späteres `NOT NULL` für
  `webauthn_user_id`, Management-UI sowie ein echter Plattform-Passkey-Beleg.

## Zielzustand

Der Produktionsvertrag erreicht den zentralen Zielzustand eines klaren
primären Truth-Layers für Domänendaten. Vollständig erledigt ist OPT-ARC-001
erst nach den unten genannten Live-, Referenz- und Legacy-Belegen:

- PostgreSQL ist die primäre Persistenzschicht für `nodes`, `edges` und
  `accounts`.
- JSONL ist entweder vollständig aus dem Runtime-Pfad entfernt oder bleibt
  nur noch explizit als Seed-, Import-, Export- oder Legacy-Artefakt erhalten.
  Es darf keine versteckte Runtime-Wahrheit mehr sein.
- Laufzeit-Caches dürfen weiter existieren, aber nur als abgeleitete oder
  read-through-Caches mit klarer Reload-/Invalidierungssemantik.
- Der Wire-Vertrag aus `docs/specs/list-pagination-api.md` bleibt stabil:
  - Legacy-Array-Antworten bleiben kompatibel.
  - Der Cursor-Envelope bleibt kompatibel.
  - Stabile ID-Sortierung im Cursor-Modus bleibt kompatibel.
  - Die `limit=0`-Validierung im Cursor-Modus bleibt unverändert.

## Vorgeschlagenes Tabellenmodell

Die folgenden Tabellen beschreiben Zielmodell und bereits teilweise implementierten Stand.
Konkrete Abweichungen und offene Constraints bleiben je Phase zu prüfen.

### `domain_nodes`

- Primärschlüssel: `id` als unveränderte, string-basierte Domänen-ID.
- Payload: ein flexibles `jsonb`-Dokument für nicht normalisierte Felder.
- Explizite Spalten: mindestens `kind`, `title`, eine geographisch
  indexierbare Standortrepräsentation für `location.lat`/`location.lon`,
  sowie `created_at` und `updated_at`.
- Indexe: Primärschlüssel auf `id`, geographischer Index auf der
  Standortrepräsentation (`location.lat`/`location.lon` oder spätere
  PostGIS-/bbox-Spalte), plus ggf. ein Zeitindex für Sortier-/Pflegezwecke.
- Eindeutigkeitsregeln: `id` bleibt eindeutig; zusätzliche fachliche
  Eindeutigkeiten nur dann, wenn die bestehende Domäne sie ausdrücklich verlangt.
- Migrationsprovenienz: `source_format`, `source_path`, `source_row`,
  `source_digest` und `import_batch_id` sind sinnvoll, wenn Import/Backfill
  reproduzierbar bleiben soll.

### `domain_edges`

- Primärschlüssel: `id` als unveränderte, string-basierte Domänen-ID.
- Payload: ein flexibles `jsonb`-Dokument für nicht normalisierte Felder.
- Explizite Spalten: `source_id`, `target_id`, optional ein Relationstyp oder
  Label, dazu `created_at`.
- `updated_at` ist **nicht** in der aktuellen `Edge`-Struct
  (`apps/api/src/routes/edges.rs`) und nicht im Domain-Vertrag
  (`contracts/domain/edge.schema.json`) definiert. Die Spalte wird in der
  Phase-B-Migration daher weggelassen. Falls Edge-Mutations-Semantik später
  eingeführt wird, muss eine eigene Migration `updated_at` ergänzen.
- Indexe: Primärschlüssel auf `id`, Einzelindizes auf `source_id` und
  `target_id`, plus ein zusammengesetzter Index für häufige Join-/Filterpfade.
- Eindeutigkeitsregeln: mindestens `id`; weitere Constraints nur, wenn sie aus
  dem aktuellen Domänenvertrag ableitbar sind.
- Edge-Create existiert opt-in.
- Aktuelle Edge-Create-Semantik nutzt serialisierten PostgreSQL-Pfad
  mit Tabellenlock, Duplicate-Precheck, Cache-Limit-Check und finalem Insert.
- Performance-/Limit-Strategie bleibt offen.
- Migrationsprovenienz: analog zu `domain_nodes`.
- Foreign-Key-Entscheidung: **ausstehendes explizites Orphan-/Referenz-Audit**.
  Default-Kandidat sind strikte FKs auf `domain_nodes(id)` für `source_id`
  und `target_id`, weil sie die stärkere Integritätsgarantie liefern.
  Der spätere SQL-Migrations-PR muss diese Entscheidung aber anhand eines
  Daten-/Code-Audits treffen: Falls das aktuelle Modell externe, fehlende oder
  entitätsübergreifende Referenzen zulässt, ist alternativ eine lose
  Referenzsemantik mit explizitem Integrity-Guard oder Quarantäne-Report zu
  wählen. Der Backfill darf Orphaned Edges niemals still verwerfen,
  normalisieren oder glätten; er muss entweder laut scheitern oder die Fälle
  explizit quarantänisieren.

### `domain_accounts`

- Primärschlüssel: `id` als unveränderte, string-basierte Domänen-ID.
- Payload: getrennte Speicherung von öffentlicher Projektion und privaten/
  operativen Feldern, zum Beispiel via `public_payload jsonb` und
  `private_payload jsonb` oder über klar benannte Einzelspalten.
- Explizite Spalten: `radius_m`, `role`, `email`, `webauthn_user_id`,
  Statusfelder wie `disabled`, dazu `created_at` und `updated_at`.
- `public_pos` ist **keine gespeicherte Spalte**. Bei `map_state=exact` wird
  sie aus der privaten Position projiziert. Bei `map_state=radius` stammt sie
  aus einer kryptografisch zufälligen, typisierten Bindung im
  `private_payload`. Diese Bindung bleibt bei unverändertem Ort und Radius
  stabil, wird bei deren Änderung ersetzt und erscheint nie in öffentlichen
  Antworten oder Domain-Events. Historische Radiuszeilen ohne gültige Bindung
  werden seit Migration `20260718000001_radius_projection_privacy` fail-closed
  als `not_on_map` behandelt.
- Schreibpfad-Abdeckung: Der Cutover muss nicht nur Account-Erzeugung,
  sondern auch spätere Account-Mutationen abdecken, insbesondere
  Step-up-E-Mail-Änderungen und WebAuthn-Credential-Writeback. Die Spalte
  `webauthn_user_id` wird bei neuen PostgreSQL-Account-Zeilen persistiert.
  Legacy-Fälle ohne diese UUID bleiben vorerst erhalten; Backfill/Audit und
  späteres `NOT NULL` bleiben ein eigener Post-Cutover-Schnitt.
- Indexe: Primärschlüssel auf `id`, eindeutiger Index auf `email` oder
  `lower(email)`, falls E-Mail-Login oder Lookup das benötigen.
- Eindeutigkeitsregeln: öffentliche und private Sicht müssen getrennt bleiben;
  sensitive Felder dürfen nie über die öffentliche Projektion in die API
  leaken.
- Migrationsprovenienz: analog zu `domain_nodes`.

## Cutover-Phasen

| Phase | Inhalt | Ergebnis / aktueller Stand |
|---|---|---|
| A | Blueprint und Statusabgleich | abgeschlossen; aktive Wahrheitsdokumente auf Produktionsstand gebracht |
| B | SQL-Schema-Entwurf und Migrationstests | implementiert; Edge-FK-/Orphan-Gate offen |
| C | Backfill-/Import-Pfad | implementiert und proof-geführt |
| D | Read-Path hinter Config | implementiert; Produktion setzt PostgreSQL, lokaler Legacy-Default bleibt JSONL |
| E-A | Account-Create-Write-Path | implementiert; Produktion schreibt PostgreSQL |
| E-B | Node-Schreibpfad | implementiert; Produktion schreibt PostgreSQL; `POST`, `PATCH`, `PUT` und `DELETE` sind proof-geführt |
| E-C | Edge-Create-Write-Path | implementiert; Produktion schreibt PostgreSQL |
| E-Rest | Weitere Account-/Integritätsbelege | Step-up-E-Mail, E-Mail-Unique und Passkey-Store implementiert; Legacy-Backfill/NOT NULL und Edge-Referenzpolitik separat offen |
| F | Runtime-Smoke und Betriebsentscheidung | Produktionscutover und vertikaler Garnrolle–Knoten–Faden-Smoke am 11./12.07.2026 belegt; nach Deploys neu zu erheben |
| G | JSONL-Demontage | Legacy-/Rollback-Aufräumung offen |

## Offene Folgebelege nach Phase E-C

- PostgreSQL-vs-JSONL-Listenparität bleibt als Kompatibilitätsbeleg relevant:
  Legacy-`offset`/`limit` und Cursor-Paginierung müssen gegen den bestehenden
  API-Vertrag prüfbar bleiben.
- Edge-Orphan-/Referenz-Audit ist offen: Vor einer strikten FK-Migration muss
  entschieden werden, ob `domain_edges.source_id`/`target_id` strikte
  Foreign Keys auf `domain_nodes(id)` erhalten oder ob eine lose
  Referenzsemantik mit Guard/Quarantäne-Report bewusst akzeptiert wird.
- Multi-Instance-Kohärenz ist entschieden (`DOMAIN-PG-002`, Option A):
  prozesslokale Caches sind nicht instanzübergreifend kohärent; horizontale
  API-Skalierung bleibt bis zu einer getesteten Kohärenzlösung ausgeschlossen.
  Siehe `docs/reports/domain-postgres-instance-coherence-decision.md`.
- E-Mail-Eindeutigkeit, Step-up-E-Mail-Persistenz und PostgreSQL-Passkey-Store
  sind implementiert; Plattform-Passkey-E2E und Managementpfade bleiben offen.
- Legacy-Accounts ohne persistierte WebAuthn-UUID brauchen Backfill/Audit
  vor späterem `NOT NULL` auf der Spalte.
- Edge-Create funktioniert opt-in, aber Lock-/Limit-Strategie ist
  nicht performance-optimiert.
- Node-Delete ist als direkter Webprozess implementiert. PostgreSQL löscht
  Node plus betroffene Fadenprojektionen atomar in einer Transaktion; JSONL
  nutzt eine Journal-/Backup-Recovery-Transaktion über Nodes- und Edges-Datei
  im Gewebe-Datenverzeichnis.
- Legacy-untypisierte Edge-Endpunkte werden beim Node-Delete nur dann als
  Node-Endpunkte behandelt, wenn keine Account-/Role-Kollision belegt ist;
  unbekannte oder ungültige Typen bleiben Cutover-Blocker und fail-closed.
- Der Runtime-Smoke vom 11./12.07.2026 ist belegt; jede aktuelle Liveaussage
  braucht nach einem Deploy eine neue datierte Evidence.
- JSONL-Legacy-/Rollback-Aufräumung ist offen.

## Instance Coherence Boundary

Für den PostgreSQL-Domain-Pfad gilt seit dem 16. Juli 2026 der geprüfte
Multi-Instance-Vertrag aus `DOMAIN-PG-002` und `WELTGEWEBE-OS-V1-T002`.
PostgreSQL ist die gemeinsame Fach- und Auth-Wahrheit. Prozesslokale
Domain-Strukturen sind nur generationsgebundene Projektionen und werden vor
PostgreSQL-gestützten Requests bei Drift vollständig und stabil nachgeladen.

Operative Konsequenzen:

- Mehrere API-Prozesse gegen dieselbe PostgreSQL-Datenbank sind fachlich
  zulässig, solange der Multi-Instance-Guard und sein Zwei-API-/Restart-Beweis
  unverändert bestehen.
- Eine höhere Produktionsreplikazahl ist dennoch kein automatischer Teil dieses
  Schnitts. Sie benötigt einen getrennten Deployment-, SLO- und Rollbackbeweis.
- Domain-Mutationen müssen die Trigger-basierte Transactional Outbox auslösen;
  ein neuer triggerumgehender Schreibpfad ist unzulässig.
- Auth-Zwischenzustände und Rate-Limits dürfen im PostgreSQL-Betrieb nicht auf
  einen einzelnen Prozess zurückfallen. Backendfehler bleiben fail-closed.
- NATS allein begründet keine Kohärenz. Verbindlich sind PostgreSQL-
  Projektionsgeneration, Outbox-Atomizität und idempotente Konsumentenwirkung.
- Restore, PITR oder triggerumgehende Wartung müssen einen expliziten
  Projektionsneubildungs- beziehungsweise Neustartpfad einschließen.

Der statische Guard
`scripts/guard/domain-multi-instance-guard.sh` und sein Test
`scripts/tests/test_domain_multi_instance_guard.sh` schützen Shared Auth,
Projektionszaun, Outbox, konkurrierende Relays, Idempotenz, Quarantäne und den
Zwei-API-/Restart-Beweis. Der vollständige Vertrag und seine Nicht-Beweise sind
im Decision-Report dokumentiert.

## Regeln für die Datenmigration

- IDs müssen unverändert bleiben.
- Sortier- und Cursorverhalten darf sich nicht ändern.
- Accounts müssen öffentliche und private Projektion strikt getrennt halten.
- Accounts müssen Standort- und Privacy-Radius-Semantik exakt bewahren.
- Sensible Felder wie E-Mail, WebAuthn- und Session-bezogene Daten dürfen nicht
  in öffentliche Projektionen auslaufen.
- Account-Mutationen aus Auth-Flows müssen persistent, restart-stabil und
  paritätsgetestet sein; cache-only Writebacks sind nach dem Cutover unzulässig.
- Edges müssen `source_id` und `target_id` exakt bewahren.
- Nodes müssen Standortfelder exakt bewahren.
- Der Backfill muss idempotent sein.
- Import muss bei malformed JSONL laut scheitern, sofern keine explizite
  Quarantäne-Strategie dokumentiert ist.
- Keine stillen Teilimporte.

## Rollback- und Fehlerbild

- Wenn Phase B fehlschlägt, wird die Migration verworfen.
- Wenn der Backfill fehlschlägt, dürfen Tabellen in Dev/Test neu aufgebaut
  werden; Produktion braucht dafür eine explizite Backup-/Restore-Strategie.
- Wenn Read-Parität fehlschlägt, darf die betroffene Änderung nicht promoted
  werden; Produktion wird nicht still auf JSONL umgedeutet.
- JSONL darf als Rollbackquelle nur genutzt werden, wenn ein konkreter,
  zeitlich passender Export/Backfill als bewusstes Rückfallartefakt
  dokumentiert ist. Andernfalls ist Restore aus geprüftem PostgreSQL-Backup
  der maßgebliche Pfad.
- Split-Brain zwischen JSONL und PostgreSQL ist zu vermeiden, nicht zu
  kaschieren.

## CI- und Proof-Anforderungen

Bereits vorhandene Proofs:

- Schema-Migrationen
- Backfill
- Read-Path
- Account-Create-Write-Path
- Node-Patch-Write-Path
- Edge-Create-Write-Path
- OPT-ARC-001-DB-Proof-Matrix-Guard

Weiter erforderlich:

- PostgreSQL-vs-JSONL-Listenparitäts-Proof:
  - `/nodes` und `/edges` müssen im Legacy-Modus die bisherige
    Einfüge-/Dateireihenfolge bewahren.
  - `/accounts` muss im Legacy-Modus die bisherige ID-Sortierung bewahren.
  - Der Cursor-Modus muss für alle drei Domänen weiterhin stabil nach ID
    sortieren.
- Edge-Orphan-/Referenz-Audit-Proof vor der finalen Entscheidung zwischen
  Foreign Keys und bewusst loser Referenzsemantik.
- Node-Delete-Cascade-Proof:
  - PostgreSQL: atomare Transaktion für Node plus betroffene Edge-Projektionen,
    inklusive eindeutiger untypisierter Legacy-Edges und fail-closed
    Account-/Role-/invalid-Type-Kollisionen.
  - JSONL: recoverbare Journal-/Backup-Transaktion über `demo.nodes.jsonl`
    und `demo.edges.jsonl`, inklusive Recovery vor Startup-Load.
- Runtime-Smoke für vollständigen PostgreSQL-Domain-Betrieb auf `commonserver`
- Multi-Instance-/Cache-Kohärenz-Proof, falls horizontale Skalierung
  erlaubt werden soll
- E-Mail-Unique-Proof
- Step-up-E-Mail-Persistenz-Proof
- WebAuthn-Credential-Writeback-Proof
- Legacy-/Rollback-Aufräumungs-Proof für JSONL

## Akzeptanzkriterien für OPT-ARC-001

OPT-ARC-001 bleibt im Board `partial`, solange die Folgebelege nicht komplett
vorliegen. Der Produktionsvertrag selbst ist auf PostgreSQL ausgerichtet.
Erledigt ist die Aufgabe erst, wenn alles Folgende nachweisbar ist:

- PostgreSQL-Migrationen existieren für `nodes`, `edges` und `accounts`.
- Der Runtime-Pfad kann PostgreSQL als primäre Quelle verwenden.
- JSONL ist nicht mehr die versteckte primäre Runtime-Wahrheit.
- Das API-Verhalten bleibt erhalten.
- Backfill/Import ist deterministisch und idempotent.
- CI belegt Migration und Runtime-Verhalten.
- Dokumentation und Statusartefakte werden erst nach diesem Beweis auf `done`
  gesetzt.
- PostgreSQL-vs-JSONL-Listenparität ist für Legacy- und Cursor-Modus
  belegt.
- Edge-Referenzintegrität ist durch Foreign Keys oder eine bewusst
  dokumentierte lose Referenzsemantik mit Guard/Quarantäne abgesichert.
