---
id: reports.domain-postgres-instance-coherence-decision
title: "Domain PostgreSQL Instance Coherence Decision — DOMAIN-PG-002"
doc_type: report
status: active
lifecycle_state: active
lifecycle: audit
owner_task: WELTGEWEBE-OS-002
review_after: 2027-01-16
created: 2026-06-18
last_reviewed: 2026-09-09
lang: de
summary: >
  Die frühere Single-Instance-Grenze ist durch einen geprüften PostgreSQL-
  Kohärenzvertrag ersetzt: kurzlebiger Auth-Zustand und Rate-Limits sind
  gemeinsam persistiert, Domain-Projektionen generationsgebunden, Mutationen
  erzeugen atomare Outbox-Ereignisse und ein Zwei-Instanz-/Restart-Beweis
  schützt den Vertrag. Produktionsreplikation bleibt ein getrennter Rollout.
relations:
  - type: relates_to
    target: docs/blueprints/domain-data-postgres-cutover.md
  - type: relates_to
    target: docs/tasks/index.json
  - type: relates_to
    target: docs/tasks/board.md
  - type: relates_to
    target: apps/api/src/state.rs
  - type: relates_to
    target: apps/api/src/middleware/domain_projection.rs
  - type: relates_to
    target: apps/api/src/routes/nodes.rs
  - type: relates_to
    target: docs/reports/cq-02-domain-projection-load.md
  - type: relates_to
    target: apps/api/src/auth/ephemeral_db.rs
  - type: relates_to
    target: apps/api/src/outbox.rs
  - type: relates_to
    target: apps/api/migrations/20260716000001_multi_instance_foundation.up.sql
  - type: relates_to
    target: apps/api/tests/db_multi_instance_foundation.rs
  - type: relates_to
    target: scripts/guard/domain-multi-instance-guard.sh
  - type: relates_to
    target: scripts/tests/test_domain_multi_instance_guard.sh
---

# Domain PostgreSQL Instance Coherence Decision

- Initiative: `WELTGEWEBE-OS-V1`
- Bureau-Aufgabe: `WELTGEWEBE-OS-V1-T002`
- lokale Aufgaben: `WELTGEWEBE-OS-002` bis `WELTGEWEBE-OS-005`
- Entscheidung: **PostgreSQL-gestützter Multi-Instance-Vertrag**
- frühere Entscheidung: Single-Instance-Invariante, abgelöst am 16. Juli 2026

## Kurzurteil

Mehrere API-Prozesse dürfen denselben PostgreSQL-Domainpfad verwenden, ohne
prozesslokale Fachwahrheit auseinanderlaufen zu lassen. Der Vertrag beruht nicht
auf einer best-effort NATS-Invalidierung, sondern auf PostgreSQL als gemeinsamer
Wahrheit:

1. kurzlebige Auth-Zustände und Rate-Limits liegen gemeinsam in PostgreSQL;
2. Domain-Mutationen erhöhen atomar eine monotone Projektionsgeneration;
3. PostgreSQL-gestützte Requests prüfen die Generation; strict-current Requests
   reconciliieren Drift sofort, während ausschließlich anonyme GET/HEAD-Requests
   begrenzt die vorherige vollständige Projektion verwenden dürfen;
4. derselbe Trigger schreibt die versionierte Domain-Mutation in eine
   transaktionale Outbox;
5. mehrere Relays claimen konkurrierend mit `FOR UPDATE SKIP LOCKED`;
6. JetStream und eine PostgreSQL-Verbrauchsquittung begrenzen
   Doppelzustellungen;
7. Fehler erhalten Backoff und nach zehn Versuchen eine explizite Quarantäne.

Der Vertrag hebt **nicht** automatisch die Produktionsreplikazahl an. Compose
und die laufende Produktion bleiben bis zum getrennten Kubernetes-/GitOps-
Rollout unverändert. Mehrinstanz-Korrektheit und produktive Hochverfügbarkeit
sind verschiedene Beweise.

## Vollständiges Zustandsinventar

| Zustand | Autorität | Produktionspersistenz | Lebensdauer | Mehrinstanzvertrag | Fehlerwirkung |
|---|---|---|---|---|---|
| Accounts | PostgreSQL | dauerhaft | fachlich | Generation + stabiler Reload | Request 503 bei nicht ladbarer Projektion |
| Knoten | PostgreSQL | dauerhaft | fachlich | Generation + stabiler Reload | Request 503 bei nicht ladbarer Projektion |
| Fäden | PostgreSQL | dauerhaft | fachlich | Generation + stabiler Reload | Request 503 bei nicht ladbarer Projektion |
| Prozesslokale Domain-Strukturen | Projektion, keine Autorität | nein | Prozess | atomarer Gesamttausch unter Request-Gate | keine partielle Mischgeneration |
| Sessions | PostgreSQL bei `DATABASE_URL` | TTL | Tage | bestehender gemeinsamer Session-Store | Auth fail-closed |
| Magic-Link-Tokens | PostgreSQL | TTL | 15 Minuten | newest-wins pro normalisierter Mail, single-use | 503 statt lokalem Fallback |
| Step-up-Challenges | PostgreSQL | TTL | 5 Minuten | kontextgebunden, instanzübergreifend konsumierbar | 503 statt lokalem Fallback |
| Step-up-Tokens | PostgreSQL | TTL | 5 Minuten | Konto-, Gerät- und Challenge-Bindung in einer Consume-Transaktion | Replay bleibt blockiert |
| Passkey-Registrierungsgrants | PostgreSQL | TTL | kurz | Konto-/Gerätebindung, single-use | Replay bleibt blockiert |
| WebAuthn-Registrierungszustand | PostgreSQL | TTL | Zeremonie | nur serverseitig serialisiert, single-use | 503 bei Backendfehler |
| WebAuthn-Authentifizierungszustand | PostgreSQL | TTL | Zeremonie | globale Kapazitätsprüfung unter Advisory Lock | 503 bei Überlast/Backendfehler |
| Auth-Rate-Limits | PostgreSQL | Fenster | Minute/Stunde | gemeinsame feste Fenster über alle Prozesse | 429 bei Limit, 503 bei Backendfehler |
| Raw-Magic-Token-Testindex | Prozesslokale Testinstrumentierung | nein | Testprozess | keine Produktionsautorität | keine Produktwirkung |
| Metriken/Logger/Clients | Prozesslokale Beobachtung | nein | Prozess | absichtlich lokal | keine Fachwahrheit |
| Domain-Outbox | PostgreSQL | bis Publish/Archiv | Ereignis | atomar mit Fachmutation | kein stiller Ereignisverlust |
| Verbrauchsquittungen | PostgreSQL | dauerhaft/archivierbar | Konsumentenvertrag | `(consumer_name,event_id)` create-once | doppelte Zustellung ohne doppelte Fachwirkung |

Ohne `DATABASE_URL` bleiben die bestehenden In-Memory-Stores als begrenzter
lokaler Entwicklungs- und Testpfad erhalten. Diese Konfiguration begründet
keinen Mehrinstanzanspruch.

## Projektionskohärenz

`domain_projection_state.version` wird von denselben PostgreSQL-Triggern erhöht,
die Outbox-Ereignisse anlegen. Eine API-Instanz speichert nur die zuletzt
vollständig verarbeitete Generation. PostgreSQL bleibt die gemeinsame Autorität;
die Prozessprojektion ist ein Cache mit explizitem Generationenzaun.

Seit CQ-02 gilt nicht mehr pauschal „bei jeder Drift vor jedem Request Full-Reload“.
Der Laufzeitvertrag unterscheidet stattdessen drei Fälle:

1. **Strict-current:** Mutationen sowie Requests mit dem kanonischen
   `gewebe_session`-Cookie müssen die aktuelle committed Generation sehen. Bei
   normaler Drift wird die stabile vollständige Projektion reconciliiert, bevor
   der Handler läuft. Sieht ein strict Request dagegen exakt einen
   transaktionsgebunden markierten lokalen Node-PATCH-Handoff V→V+1, startet er
   keinen redundanten O(N)-Reload. `nodes_persist` ist weder Herkunftsbeweis noch
   Completion-Signal. Der Request wartet per `tokio::sync::Notify`, prüft aber
   vor und nach jeder Weckung erneut den atomaren Marker; Notify trägt selbst
   keinen Zustand. DB-Abfragen, erneute Klassifikationsrunden und Handoff-Warten
   teilen ein einziges kumulatives 1-Sekunden-Budget plus explizites
   Iterationslimit. Ein festhängender oder dauerhaft wechselnder Zustand endet
   fail-closed statt den Single-Flight-Koordinator unbegrenzt zu halten. Das ist
   sicherheitsrelevant, weil die nachfolgende Auth-Middleware Account-`disabled`
   und Rolleninformationen aus der Projection liest.
2. **Anonyme sichere Reads:** Nur GET/HEAD ohne `gewebe_session` dürfen während
   eines bereits aktiven Generation-Checks/Reloads oder beim exakt markierten
   lokalen V+1-Handoff vorübergehend die vorherige **vollständige** Projektion
   verwenden. Es wird nie eine teilweise geladene Generation veröffentlicht.
3. **Exakter lokaler Node-PATCH-Handoff:** Ein echter PostgreSQL-Node-PATCH
   erhöht über seinen Trigger `domain_projection_state.version` in derselben
   Transaktion. Erst nachdem diese Transaktion ihre eigene neue Version als
   exakt `local + 1` gelesen hat und dabei die Versionszeile bis COMMIT gesperrt
   hält, darf der Prozess den Handoff per CAS markieren. Der Marker ist damit
   bereits vor Sichtbarkeit des Commits vorhanden, ohne fremdes V+1 als lokal
   zu klassifizieren. Nach COMMIT wird der einzelne Node nur dann in den Cache
   publiziert und die lokale Projection-Version nur dann fast-forwarded, wenn
   die Transaktion nachweislich genau die nächste vollständige Generation selbst
   erzeugt **und den zugehörigen Handoff-Guard tatsächlich erworben** hat. Ein
   passender Versionswert ohne Guard ist kein Ownership-Beweis. Die lokale
   Projection-Version wird vor dem CAS-Clear des RAII-Guards veröffentlicht;
   beim Refresh wird deshalb der Marker
   per Acquire vor der lokalen Version gelesen. Semantische No-op-PATCHes erhalten
   keinen Handoff-Marker.

Der +1-Pfad ist bewusst eng. Springt die DB-Version um mehr als eine Generation,
ist der lokale CAS nicht mehr passend oder stammt die Drift von einem externen
Writer, wird **nicht** blind vorgespult. Existierte externe Drift bereits vor dem
lokalen PATCH und erzeugt dieser deshalb V+2, bleibt der Prozesscache auf der
letzten vollständigen Generation; der einzelne lokale Node wird nicht partiell
hineingemischt.

Umgekehrt kann die erste DB-Abfrage noch V gesehen haben, obwohl der lokale Writer
anschließend V+1 vollständig publiziert hat. Bei `local_version > observed`
bestätigt deshalb ein zweiter DB-Read den Catch-up. Nur bei bestätigter Gleichheit
wird der unnötige O(N)-Reload unterdrückt; eine stabil niedrigere DB-Generation
fällt in normale Reconciliation und schützt damit den aktuell noch niedrigeren
Restore/PITR-Fall. Eine echte Restore-Epoch-Identität über wiederverwendbare
Versionsnummern ist damit nicht bewiesen und bleibt T049.

Ein Full-Reload lädt Accounts, Nodes und Edges parallel außerhalb des Request-
Write-Gates. Eine Generation wird vor und nach dem Laden gelesen; überlappt eine
Mutation den Load, wird bis zu einem festen Limit erneut geladen. Erst danach
wird unter dem Projection-Write-Gate die vollständige Generation gemeinsam
ausgetauscht. Während eines Handlers schützt das Projection-Read-Gate weiterhin
vor einem partiellen Cache-Swap.

CQ-02 hat den **Single-Instance-Node-PATCH-Vertrag** nach dem
Liveness-Nachfolger erneut unter unverändertem Mixed-Load gemessen. Run
`34328529159`, Job `102391380221`, auf dem exakten API-Commit
`b4b154e5fa6ba39824088978417489a89e9f9794` belegt bei 100.000 Nodes und
500.000 Edges 15/15 erfolgreiche PATCHes, Projection-Version 1→16, Writer-p95
70,5 ms, 0 Write-/Read-Fehler, 0 HTTP 503, 0 dropped iterations, 0
Stable-Snapshot-Retries und **0 Full-Reloads** während des 30-Sekunden-Mixed-
Fensters. Der Harness lief mit genau einer API-Instanz und weist deshalb
ausdrücklich `multi_instance_load_proven: false` aus. Der detaillierte Messbeleg
liegt in `docs/reports/cq-02-domain-projection-load.md`.

Direkte `TRUNCATE`- oder andere triggerumgehende Wartung an Domain-Tabellen ist
außerhalb dieses Laufzeitvertrags und muss mit kontrolliertem Neustart oder
expliziter Projektionsneubildung verbunden werden.

Die stale-safe Klassifikation ist derzeit absichtlich an das Fehlen des
kanonischen `gewebe_session`-Cookies gebunden. Eine aktuelle Router-Prüfung fand
keinen alternativen projection-sensitiven GET/HEAD-Authentifizierungsweg; der
Magic-Link-GET prüft nur Token-Präsenz und trifft dort keine Rollen-/Account-
Entscheidung. Würde künftig Bearer- oder ein anderer Auth-Carrier eingeführt, muss
diese Invariante vor Aktivierung maschinell erweitert werden; der Follow-up ist
in T049 gebündelt.

## Gemeinsamer Auth-Zustand

`auth_ephemeral_state` hält ausschließlich kurzlebigen serverseitigen Zustand.
Opaque IDs werden vor Speicherung gehasht. Kontextoperationen verwenden
transaktionsgebundene PostgreSQL-Advisory-Locks, damit beispielsweise zwei
parallel angeforderte Magic Links nicht beide als aktuell gelten.

WebAuthn-Zeremoniezustand wird über die dafür vorgesehene
`webauthn-rs`-Serialisierung serverseitig gespeichert. Dafür ist in Version 0.5
das bewusst auffällig benannte Feature `danger-allow-state-serialisation`
erforderlich. Seine Freigabe gilt ausschließlich für kurzlebigen,
serverseitigen PostgreSQL-Zustand: nie für Cookies, Browserdaten, Logs oder eine
langfristige Fachrepräsentation. Die Payload bleibt an gehashte opaque IDs,
Konto und Gerät gebunden, besitzt eine kurze TTL und wird single-use konsumiert.
Ein Bibliotheksupgrade oder eine stabile, enger typisierte Persistenzschnittstelle
muss dieses Feature erneut sicherheitsgebunden bewerten; stilles Ausweiten des
Serialisierungsumfangs ist unzulässig. Ein periodischer Sweeper entfernt
abgelaufene Auth- und Rate-Limit-Zeilen auch ohne neuen Traffic.

Step-up-Challenges verwenden bewusst den exakten Kontext aus Konto, Gerät und
Intent. Dadurch kollidieren `LogoutAll`, Passkey-Registrierung, Geräteentfernung
und E-Mail-Änderung nicht miteinander. Unterschiedliche Zielwerte sind getrennte,
kurzlebige und gebundene Operationen; das bloße Entfernen des Intents aus dem
Kontextschlüssel würde dagegen eine neue Anfrage fälschlich mit dem Payload einer
älteren Operation wiederverwenden.

## Transaktionale Outbox

Trigger auf `domain_accounts`, `domain_nodes` und `domain_edges` schreiben für
Insert, Update und Delete ein Ereignis. No-op-Updates erzeugen keines. Dadurch
ist die Outbox nicht von einzelnen Route-Hooks abhängig und bleibt auch für
künftige Schreibpfade verbindlich. Die Erstinstallation erzeugt keine
Ereignisse für vorhandene Zeilen, weil die Trigger erst nach den additiven
Tabellen angelegt werden. Spätere Massenmutationen und Backfills müssen dagegen
in begrenzten Batches erfolgen und Outbox-Backlog, Lockzeiten sowie
Konsumentenfortschritt überwachen. Ein stilles Abschalten der Trigger wäre ein
Verlust des Ereignisvertrags und ist kein zulässiger Performance-Workaround.

Relays:

- claimen kleine Batches mit `FOR UPDATE SKIP LOCKED` in der Reihenfolge
  `(available_at, id)`, passend zum partiellen Pending-Index;
- setzen eine zeitlich begrenzte Claim-Lease;
- veröffentlichen mit `Nats-Msg-Id=domain-outbox-<id>`;
- markieren erst nach JetStream-Acknowledgement als publiziert;
- planen Fehler exponentiell mit eventgebundenem Jitter neu;
- quarantänisieren nach zehn Fehlversuchen;
- erlauben eine explizite, nur für unveröffentlichte Quarantäneereignisse gültige
  Operator-Requeue über `requeue_quarantined`. Vor und nach jeder Requeue sind
  Ereignisinhalt, Fehlerursache und betroffene Konsumenten zu prüfen; eine
  pauschale Wiederfreigabe aller Quarantäneereignisse ist unzulässig.

Ein Absturz zwischen Publish und PostgreSQL-Markierung kann erneut publizieren.
Das ist zulässige At-least-once-Semantik. Eine Vorabmarkierung als publiziert ist
ausdrücklich ausgeschlossen, weil ein Absturz danach ein nie versendetes
Ereignis dauerhaft als erledigt erscheinen ließe. Der Konsumentenvertrag verhindert
doppelte Fachwirkung über eine create-once-Verbrauchsquittung. Die
JetStream-Deduplizierung reduziert zusätzliche Duplikate, ersetzt diese
Quittung aber nicht.

## Beweise

CQ-02 ergänzt den bestehenden Mehrinstanz-**Korrektheits**beweis um reale,
revisionsgebundene **Single-Instance-Last**. Diese beiden Aussagen werden bewusst
nicht vermischt:

- `.github/workflows/domain-projection-load.yml` misst 1k und 100k jeweils
  read-heavy und mixed mit 10 Reader-VUs und 0,5 PATCHes/s auf einer API-Instanz;
- der Summarizer ist fail-closed für Drops, Read-/Write-Fehler, HTTP 503,
  Versionsdelta, Refresh-/Reload-Fehler, Stable-Snapshot-Retries und jeden
  Full-Reload (`max_reloads = 0`);
- der CQ-02-Nachfolger ergänzt gezielte Unit-Regressionen für Guard-Ownership,
  kumulatives Zeit-/Iterationsbudget sowie lost-wakeup-sicheres Notify-Warten.
  Der direkte PostgreSQL-Node-Write-Lauf ist 43/44 grün; der einzige rote
  Guest-Exit-Test (503 statt 403) scheitert auf frischer DB identisch auf dem
  unveränderten Merge-Commit `2beeb8e575ec83c0c8bdbb569997ef10dfd39249`
  und ist damit als vorbestehender Baseline-Fehler, nicht als CQ-02-Regression,
  eingegrenzt;
- die Middleware-Policy hält Session-Requests strict-current.

Der aktuelle 100k-Mixed-Lastbeweis des CQ-02-Nachfolgers ist Run
`34328529159`, Job `102391380221`, auf
`b4b154e5fa6ba39824088978417489a89e9f9794`; Details und die vollständige
Messfolge stehen in `docs/reports/cq-02-domain-projection-load.md`.

`apps/api/tests/db_multi_instance_foundation.rs` baut gegen eine isolierte
PostgreSQL-Datenbank und einen JetStream-Server auf:

- zwei unabhängig konstruierte `ApiState`-Instanzen;
- Magic-Link newest-wins und single-use über Instanzgrenzen;
- Challenge-, Step-up- und Passkey-Grant-Handoff;
- echten serialisierten WebAuthn-Registrierungszustand;
- globales Rate-Limit statt prozessweise vervielfachtem Budget;
- Domain-Insert und -Update mit Projektionsnachzug;
- dritte, neu konstruierte Restart-Instanz;
- zwei gleichzeitig gestartete Outbox-Relays;
- JetStream-Publish und PostgreSQL-Verbrauchsquittung;
- Replay-Unterdrückung, Backoff, Quarantäne und kontrollierte Requeue;
- den echten `logout_all`-Handler mit instanzübergreifend sichtbarer Challenge.

Der Test verweigert Datenbanknamen ohne `_test`, um versehentliche Ausführung
gegen nicht ausdrücklich isolierte Datenbanken zu verhindern.

## Neuer Guard

`scripts/guard/domain-multi-instance-guard.sh` ersetzt den früheren
Single-Instance-Guard im selben Schnitt. Er erlaubt eine Replikazahl größer als
eins, solange die stärkeren Voraussetzungen im Repository gemeinsam bestehen:

- Shared-Auth- und Rate-Limit-Schema;
- generationengebundene Domain-Projektion;
- Trigger-basierte transaktionale Outbox;
- konkurrierender Relay-Claim;
- Publisher- und Consumer-Idempotenz;
- Backoff und Quarantäne;
- Laufzeitverdrahtung;
- Zwei-Instanz- und Restart-Beweis.

`scripts/tests/test_domain_multi_instance_guard.sh` prüft den realen
Repositoryzustand, einen erlaubten Replica-3-Fall und negative Mutationen für
Shared Auth, Projektionsgate, `SKIP LOCKED`, Restartbeweis und den verbotenen
Rückfall zum alten Guard.

## Bewusste Grenzen

Dieser Schnitt beweist nicht:

- produktive Kubernetes- oder Compose-Replikation;
- PostgreSQL-Hochverfügbarkeit oder automatisches Failover;
- JetStream-Clusterhochverfügbarkeit;
- regionsübergreifende Föderationskonsistenz;
- beliebige externe Konsumenten ohne eigene idempotente Wirkungssperre;
- Schema-Migration während beliebiger alter und neuer Binärversionen zugleich.

Diese Punkte gehören zu `WELTGEWEBE-OS-006` ff. Der jetzige Vertrag entfernt
den fachlichen Single-Instance-Blocker, nicht alle Betriebsrisiken.

## Rückfallregel

Eine Änderung, die wieder prozesslokale Produktionsautorität einführt, den
Projektionszaun umgeht, Session-Requests stale werden lässt, den lokalen +1-
Fast-Forward über unbekannte Generationen ausweitet, Domain-Mutationen ohne
Outbox zulässt oder einen Konsumenten ohne Idempotenzwirkung hinzufügt, muss den
Multi-Instance- und CQ-02-Beweis im selben diffgebundenen Schnitt erweitern. Ein
stiller Rückfall auf eine einzelne API-Instanz oder das Lockern des Lastvertrags
ist keine zulässige Reparatur.
