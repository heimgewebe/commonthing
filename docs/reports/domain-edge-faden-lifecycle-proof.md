---
id: reports.domain-edge-faden-lifecycle-proof
title: "Domain Edge — Faden-Lifecycle-Proof"
doc_type: report
status: active
lifecycle_state: active
lifecycle: proof
owner_task: OPT-ARC-001
created: 2026-07-17
review_after: 2026-10-17
canonicality: evidence
lang: de
summary: >
  Lokaler, diffgebundener Implementierungs- und Regressionstest für den
  exakt 168 Stunden langen Lebenszyklus unverzwirnter Fäden einschließlich
  rückwirkender Ableitung für datierte Legacy-Projektionen ohne persistiertes
  expires_at. Belegt sind servereigene Ablaufzeit, Filterung vor Paginierung,
  404 nach Ablauf und kontinuierliches lineares Kartenverblassen.
  Garn und Verzwirnung bleiben ein eigener, noch zu spezifizierender Vertrag.
relations:
  - type: supersedes
    target: docs/reports/domain-edge-write-path-proof.md
  - type: relates_to
    target: docs/specs/garnrolle-knoten-faden.md
  - type: relates_to
    target: contracts/domain/edge.schema.json
---

# Domain Edge — Faden-Lifecycle-Proof

## Ergebnis

**LOCAL_PROVEN.** Jeder neu erzeugte Faden erhält serverseitig
`expires_at = created_at + 168 Stunden`. Clientwerte für `created_at` und
`expires_at` bleiben verboten. Ab `now == expires_at` verschwindet der Faden
aus aktiven Listen, Einzelabrufen, Account-Projektionen und der Karte. Seine
persistierte Webungsaktion wird nicht gelöscht. Für bestehende Projektionen mit
gültigem `created_at`, aber fehlendem `expires_at`, wird dieselbe Grenze beim
Lesen abgeleitet, ohne gespeicherte Daten oder die Chronik umzuschreiben.

## Belegte Semantik

| Achse | Vertrag |
|---|---|
| Zeitautorität | API-Server setzt `created_at` und leitet `expires_at` daraus ab. |
| Dauer | Exakt 168 Stunden; keine Konfiguration und kein Refresh durch spätere Aktionen. |
| JSONL | Neue Fäden speichern beide Zeitstempel. Datierte Legacy-Zeilen ohne `expires_at` bleiben unverändert. |
| PostgreSQL | `created_at` bleibt Spalte; `expires_at` wird für neue Fäden im bestehenden JSONB-Payload gespeichert und beim Reload rekonstruiert. |
| Aktive Projektion | Filterung erfolgt vor Offset- und Cursor-Paginierung; Einzelabruf liefert nach Ablauf 404. |
| Nebenprojektion | Account-Details verwenden denselben aktiven Fadenprädikat. |
| Darstellung | Die lineare Deckkraft wird aus vorgeparsten Millisekunden berechnet und minütlich neu projiziert; ein separater Einmal-Timer entfernt den nächsten Faden exakt bei `expires_at`. |
| Legacy | Fehlt das `expires_at`-Feld ganz, wird es aus einem gültigen `created_at` exakt abgeleitet. Vollständig undatierte Datensätze bleiben sichtbar; Persistenz und Chronik werden nicht verändert. |
| Korruption | Ungültige vorhandene Zeitstempel, ein explizites `expires_at: null` bei datiertem `created_at`, strukturell fehlerhafte PostgreSQL-Payload-Werte sowie explizite, nicht exakt 168 Stunden auseinanderliegende Grenzen werden fail-closed ausgeblendet beziehungsweise beim Laden übersprungen — keiner dieser Fälle wird mit dem legitimen, fehlenden Feld verwechselt. |
| Garn | Dauerhaft und ausgenommen, aber ohne geratenes Feld oder öffentliches CRUD; eigener Folgeauftrag. |

## Revalidierung 2026-07-29

- API-Lifecycle-Unit-Tests: 9 bestanden, 0 fehlgeschlagen.
- API-Edge-Integrationstests: 28 bestanden, 0 fehlgeschlagen. Enthalten ist ein
  vor Einführung des Lifecycle-Vertrags erzeugter Faden mit `created_at`, aber
  ohne `expires_at`, der vor Paginierung gefiltert wird und im Einzelabruf 404
  liefert.
- Karten-Lifecycle-Tests: 5 bestanden, 0 fehlgeschlagen.
- Svelte-Typ- und Komponentenprüfung: 0 Fehler, 0 Warnungen.
- Domain-Contracts: sechs Schemata und alle Beispielinstanzen bestanden.
- Rust-Formatprüfung, Clippy mit Warnungen als Fehler und Web-Lint: bestanden.

Nicht als neue lokale Evidenz behauptet werden der vollständige Web-Build, das
gesamte API-Testkorpus, `cargo deny` und die Remote-CI. Diese Prüfungen bleiben
den repositoryweiten beziehungsweise PR-gebundenen Gates vorbehalten.

## Revalidierung 2026-07-30

Vier von Codex gemeldete Befunde auf vorherigen Heads sind behoben:

- P2: Die API unterschied beim JSONL- und PostgreSQL-Laden ein fehlendes
  `expires_at` nicht von einem explizit gespeicherten `expires_at: null`,
  wodurch ein datierter Legacy-Faden mit explizitem `null` fälschlich eine
  rückwirkend abgeleitete Ablaufzeit erhielt statt fail-closed ausgeblendet zu
  werden. `Edge.expires_at` ist jetzt ein Tri-State
  (`Option<Option<LifecycleTimestamp>>`), rund-trip-fest für beide
  Persistenzpfade.
- P2: Die Kartengrenze (`edgeLifecycle.ts`) behandelte ein fehlendes
  `created_at` wie den undatierten Legacy-Zustand statt es als nichtkonform
  abzulehnen; sie unterscheidet jetzt `undefined` (immer ungültig) von einem
  expliziten `null` (nur mit explizitem `expires_at: null` gültig).
- P3: Eine strukturell fehlerhafte (weder fehlende, `null`- noch String-)
  `expires_at`-Nutzlast im PostgreSQL-JSONB-Payload wurde zuvor stillschweigend
  wie ein explizites `null` behandelt; bei fehlendem `created_at` hätte das
  denselben Datensatz wie einen legitimen, dauerhaft sichtbaren undatierten
  Altbestand erscheinen lassen. Der PostgreSQL-Ladepfad überspringt solche
  Zeilen jetzt mit einer Warnung, statt sie zu maskieren.
- P2: Der vorherige Fix für den obigen Befund führte selbst eine Paginierungs-
  Regression ein: `load_edges_from_postgres` überspringt fehlerhafte Zeilen,
  ohne `seen` zu erhöhen, während die SQL-Abfrage die physischen Zeilen
  weiterhin fest auf `max_edges + 1` begrenzte. Sortierten mehrere fehlerhafte
  Zeilen vor gültigen und überschritt die Tabelle das Cache-Limit, gingen
  dadurch gültige Fäden stillschweigend verloren und `truncated` blieb
  fälschlich `false`. Die Abfrage begrenzt die Zeilenzahl jetzt nicht mehr per
  SQL; die Schleife selbst bricht erst ab, sobald `max_edges + 1` **gültige**
  Zeilen untersucht wurden.

Aktualisierte lokale Prüfbelege:

- API-Lifecycle-Unit-Tests (`routes::edges::tests`): 10 bestanden, 0
  fehlgeschlagen (zuvor 9; neuer Test deckt das explizite `null` bei
  datiertem `created_at` ab).
- Neue PostgreSQL-Payload-Unit-Tests (`domain_db::edge_write_path_tests`): 3
  zusätzliche Tests für die Tri-State-Unterscheidung und das Verwerfen
  strukturell fehlerhafter Nutzlasten.
- Karten-Lifecycle-Tests: 6 bestanden, 0 fehlgeschlagen (zuvor 5; neuer Test
  deckt das fehlende `created_at` an der Kartengrenze ab).
- Vollständige lokale Rust-Bibliothekstests (470 bestanden), `cargo clippy
  --all-targets -- -D warnings` und `cargo fmt --check`: bestanden.
- Neuer PostgreSQL-Integrationstest
  `edges_loader_skips_malformed_rows_without_losing_later_valid_edges`
  (`db_domain_read_path`) reproduziert exakt das von Codex beschriebene
  Szenario (Cache-Limit 2, zwei fehlerhafte Zeilen vor zwei gültigen) gegen
  eine disponible lokale PostgreSQL-16-Instanz; zusammen mit den bestehenden
  `db_domain_read_path`- (10 bestanden) und `db_domain_edge_write_path`-Tests
  (8 bestanden) grün.
- Vollständige lokale Web-Vitest-Suite (275 bestanden über 36 Dateien),
  `svelte-check` (0 Fehler) und `pnpm lint`: bestanden.
- `just contracts-domain-check`: alle sechs Schemata und Beispielinstanzen
  weiterhin gültig; dieser Fix ändert kein Schema.

Fünfter von Codex gemeldeter Befund, ebenfalls behoben: Der vorherige
Paginierungs-Fix prüfte nur, ob eine Zeile strukturell ladbar war, nicht ob
die daraus konstruierte Kante überhaupt jemals aktiv sein kann. Ein
datiertes `created_at` mit explizitem `expires_at: null` ist strukturell
gültig, aber laut `edge_is_active_at` für jedes `now` zurückgewiesen und
hätte weiterhin einen Cache-Slot verbraucht. `edge_is_active_at`s Validierung
ist jetzt in einen gemeinsamen `edge_lifecycle_window`-Helper extrahiert; die
neue `edge_is_permanently_unreachable`-Funktion nutzt ihn, sodass Leseweg und
PostgreSQL-Ladeweg exakt übereinstimmen, welche Zeilen niemals sichtbar
werden können.

- API-Lifecycle-Unit-Tests (`routes::edges::tests`): 12 bestanden, 0
  fehlgeschlagen (zuvor 10; ein neuer Test prüft die Übereinstimmung von
  `edge_is_active_at` und `edge_is_permanently_unreachable`, ein weiterer —
  von @alexdermohr direkt beigetragen — die explizite null/null-Serialisierung
  der undatierten Legacy-Projektion).
- Neuer PostgreSQL-Integrationstest
  `edges_loader_excludes_permanently_unreachable_rows_from_cache_capacity`
  (`db_domain_read_path`, jetzt 11 bestanden) reproduziert das beschriebene
  Szenario gegen eine disponible lokale PostgreSQL-16-Instanz.
- Vollständige lokale Rust-Bibliothekstests: 472 bestanden.

Ein sechster, in derselben Runde gemeldeter Befund ("Exclude already-expired
rows from startup cache") wurde geprüft und **nicht** übernommen: Der
PostgreSQL-Ladeweg hält bereits abgelaufene, aber strukturell gültige Fäden
absichtlich im Cache — der Create-Pfad prüft `edges.get(&edge.id).is_some()`
für Duplicate-ID-Erkennung und Operation-Replay explizit gegen den
vollständigen Cache, unabhängig vom Aktivitätsstatus. Ein bereits abgelaufener
Faden aus dem Cache auszuschließen würde diese Chronik-/Idempotenzgarantie
brechen (siehe „Grenzen und Folgetasks“: abgelaufene Projektionen bleiben aus
Chronik- und Idempotenzgründen persistiert). Das allgemeinere Problem — feste
Cache-Kapazität bei sehr großen Edge-Beständen — bleibt als
`WELTGEWEBE-EDGE-PROJECTION-INDEX-BENCH-V1` in Bureau-PR #649 registriert und
erfordert dort eine reproduzierbare 500k-Messung statt einer punktuellen
Änderung der Admission-Reihenfolge in diesem PR.

Codex fand denselben Konsistenzfehler danach an zwei weiteren Stellen, die
`edge_is_permanently_unreachable` noch nicht berücksichtigten:

- Der JSONL-Leseloader (`load_edges`) zählte dauerhaft unerreichbare Zeilen
  weiterhin gegen `MAX_EDGES_CACHE`, bevor er sie verwarf — derselbe Fehler
  wie zuvor im PostgreSQL-Loader, nur für den JSONL-Pfad. Behoben durch
  dieselbe Umstellung der Prüfreihenfolge; die Duplicate-ID-/Operation-Scan-
  Funktion `inspect_edge_persistence_for_create` zählt ebenfalls nur noch
  erreichbare Zeilen gegen das Limit, erkennt Duplikate und Replays aber
  weiterhin über die gesamte Datei unabhängig vom Erreichbarkeitsstatus.
- Der PostgreSQL-Schreibpfad (`insert_domain_edge`) prüfte die Kapazität
  weiterhin über ein rohes `SELECT COUNT(*)`, das dauerhaft unerreichbare
  Zeilen mitzählte, obwohl sie keinen Cache-Slot mehr belegen — neue, gültige
  Fäden wären dadurch dauerhaft blockiert geblieben, sobald die Tabelle genug
  solcher Zeilen enthielt. Behoben durch eine gezielte SQL-Bedingung, die
  genau den konkreten, günstig prüfbaren Fall ausschließt (datiertes
  `created_at` mit explizitem `expires_at: null`) statt das volle
  Rust-seitige Prädikat je Schreibzugriff nachzubilden (das einen
  Payload-Volltabellenscan pro Create erfordern würde). Ein erster
  Implementierungsversuch fiel selbst einer Drei-Werte-Logik-Falle zum Opfer
  (`payload -> 'expires_at' = 'null'::jsonb` liefert SQL-`NULL`, nicht
  `false`, wenn der Schlüssel fehlt, wodurch auch reguläre Zeilen ohne
  `expires_at` fälschlich ausgeschlossen wurden); korrigiert über den
  `payload ? 'expires_at'`-Existenzoperator, der immer einen definiten
  Wahrheitswert liefert.

Neue Regressionstests: `post_edges_admits_create_when_limit_filled_by_permanently_unreachable_row`
(JSONL, `api_edges`), `postgres_edge_create_admits_when_limit_filled_by_permanently_unreachable_row`
(PostgreSQL, `db_domain_edge_write_path`, gegen eine disponible lokale
PostgreSQL-16-Instanz verifiziert, zusammen mit den bestehenden 9 Tests
dieser Datei weiterhin grün). Vollständige lokale Rust-Bibliothekstests (472
bestanden), `cargo clippy --all-targets -- -D warnings` und
`cargo fmt --check`: bestanden.

Codex bemängelte anschließend zurecht, dass die gezielte SQL-Bedingung nur
den einen konkreten Fall (datiert + explizites `null`) abdeckte, während
`edge_is_permanently_unreachable` auch undatierte Zeilen mit konkretem
`expires_at`-String sowie strukturell fehlerhafte (weder `null` noch
String-)Werte ausschließt. Die Bedingung deckt jetzt zusätzlich beide sicher
prüfbaren Fälle über `jsonb_typeof` ab (das nie einen Laufzeitfehler wirft,
unabhängig vom Zellinhalt). Bewusst weiterhin nicht abgedeckt bleibt ein
datiertes `created_at` mit einem vorhandenen, aber nicht exakt 168 Stunden
entfernten oder unparsebaren `expires_at`-String: Eine Prüfung dieses Falls
würde einen Cast des Payload-Strings auf einen Zeitstempel erfordern, der
bei irgendeiner nicht-Zeitstempel-Zeichenkette in der gesamten Tabelle einen
harten Datenbankfehler auslöst statt zu einem Boolean auszuwerten — aus einer
konservativen Überzählung würde ein Query-Ausfall. Das Schließen dieser
Lücke bräuchte eine sichere Cast-Funktion (eine Schemamigration), keine
WHERE-Bedingung; der Leseweg weist einen solchen Datensatz ohnehin schon
beim Laden fail-closed ab, sodass im schlimmsten Fall eine Zeile, die über
diesen API-eigenen Schreibpfad (der stets eine kanonische Dauer erzeugt)
gar nicht entstehen kann, die Kapazität etwas zu früh als erschöpft zählt.

Zwei weitere Regressionstests: `postgres_edge_create_admits_when_limit_filled_by_malformed_expiry_row`
und `postgres_edge_create_admits_when_limit_filled_by_undated_row_with_expiry`
(`db_domain_edge_write_path`, jetzt 11 bestanden, gegen eine disponible
lokale PostgreSQL-16-Instanz verifiziert). Vollständige lokale
Rust-Bibliothekstests (472 bestanden), `cargo clippy --all-targets -- -D
warnings` und `cargo fmt --check`: weiterhin bestanden.

Codex fand danach einen weiteren, subtilen Fall: Ein datiertes `payload`,
das selbst kein JSON-Objekt ist (etwa das Array `["expires_at"]`), lässt
PostgreSQLs `?`-Operator — der auch Array-Elemente statt nur Objektschlüssel
matcht — fälschlich `true` liefern, während `payload -> 'expires_at'` dabei
SQL-`NULL` zurückgibt; `jsonb_typeof(NULL)` ist wiederum `NULL`, wodurch die
gesamte Ausschlussbedingung zu `NULL` auswertet und die Zeile fälschlich von
der Kapazitätszählung ausgeschlossen wird. `payload_lifecycle_field` sucht
Schlüssel dagegen über `Value::get`, das für jeden Nicht-Objekt-Wert
unabhängig vom Inhalt `None` liefert — der Loader behandelt ein
Nicht-Objekt-Payload also wie `{}` (eine erreichbare, datierte
Legacy-Kante mit abgeleiteter Ablaufzeit) und zählt sie mit. Die
Kapazitätsprüfung muss dasselbe tun, sonst kann ein Create trotz vollem
Cache erfolgreich sein.

Behoben durch eine äußere Bedingung `jsonb_typeof(payload) = 'object'`, die
der gesamten Ausschlussklausel vorausgeht: Da die Spalte `payload` per
Schema `NOT NULL` ist, liefert `jsonb_typeof(payload)` immer einen
definiten Wahrheitswert, wodurch `FALSE AND …` für jeden Nicht-Objekt-Wert
sicher zu `FALSE` auswertet, statt die SQL-`NULL`-Falle erneut auszulösen.
Neuer Regressionstest: `postgres_edge_create_rejects_when_limit_filled_by_non_object_payload_row`
(jetzt 12 Tests in `db_domain_edge_write_path`, gegen eine disponible
lokale PostgreSQL-16-Instanz verifiziert; erwartet — anders als die
vorherigen drei „admits“-Tests — eine Ablehnung, da diese Zeile tatsächlich
einen Cache-Slot belegt). Vollständige lokale Rust-Bibliothekstests (472
bestanden), `cargo clippy --all-targets -- -D warnings` und
`cargo fmt --check`: weiterhin bestanden.

## Grenzen und Folgetasks

- Vollständig undatierte Legacy-Projektionen bleiben sichtbar, weil ihr Alter
  nicht ohne Schätzung rekonstruiert werden kann.
- Abgelaufene Projektionen bleiben aus Chronik- und Idempotenzgründen
  persistiert; sichere Archivierung oder Kompaktion ist separat registriert.
- Die feste Sieben-Tage-Frist besitzt keine Runtime-Oberfläche mehr; AppConfig
  weist die entfernten Schlüssel `fade_days` und `HA_FADE_DAYS` fail-closed ab.
- Garn und Verzwirnung benötigen einen eigenen Domänen-, Ereignis- und
  Persistenzvertrag.
- Endpoint- und Ablaufindexierung für sehr große Edge-Bestände ist als
  `WELTGEWEBE-EDGE-PROJECTION-INDEX-BENCH-V1` in Bureau-PR #649 registriert; sie
  erfordert zuerst eine reproduzierbare 500k-Messung.
- MapLibre-Feature-State ist als `WELTGEWEBE-MAP-EDGE-FEATURE-STATE-V1` in
  Bureau-PR #649 registriert und bleibt profilinggebunden, weil der auf 250
  Fäden begrenzte minütliche `setData`-Pfad derzeit keinen belegten Engpass
  darstellt.
- Remote-CI- und Live-Evidence werden nach PR und Merge ergänzt; dieser Bericht
  behauptet bis dahin ausschließlich den lokal reproduzierten Stand.

## Revalidierung 2026-11-26: aktive Kapazität statt historischer Zeilenzahl

Die frühere Entscheidung gegen das Ausschließen abgelaufener Zeilen aus dem
Cache (Revalidierung 2026-07-30) ist durch den nun vollständigen persistenten
Duplicate-ID-/Operationsscan und die PostgreSQL-Identitätsprüfung überholt.
Historische Zeilen bleiben unverändert gespeichert und für Replays verfügbar;
sie belegen nach Ablauf keinen aktiven Cache-Slot mehr. Zukünftige und
vollständig undatierte, gültige Projektionen behalten ihren Slot.

- Ein gemeinsames Zulassungsprädikat begrenzt JSONL-Laden, PostgreSQL-Laden
  und Cache-Publikation. PostgreSQL filtert abgelaufene Zeilen vor dem
  Payload-Streaming über den typisierten Zeitstempel und die kanonische
  168-Stunden-Frist. Die bestehende konservative Zählung nicht sicher
  interpretierbarer Payload-Zeitstrings bleibt erhalten.
- Neuaufnahme und Reaktivierung prüfen dieselbe persistente Kapazität unter
  derselben Schreibserialisierung; eine bereits zugelassene Identität kann
  ihren eigenen Platz wiederverwenden. Replays abgelaufener Ergebnisse
  verdrängen keine neueren aktiven Projektionen. Keine Datenlöschung,
  Schemamigration oder Änderung des Lebenszyklusvertrags.
- Die drei Ausgangsregressionen (JSONL-Kapazität, PostgreSQL-Kapazität,
  Reaktivierung am Limit) wurden vor der Reparatur reproduziert und sind
  anschließend grün. Der frühere statisch datierte Test für einen aktiven
  Nicht-Objekt-Payload verwendet nun einen aktuellen Erstellungszeitpunkt;
  die Kapazitätsassertion bleibt unverändert.
- Lokaler Prüfbeleg `5b6cc531cc2c46c0a00500d7`: 20 Lifecycle-Unit-, 38 API-Edge-,
  15 PostgreSQL-Edge-Write- und 26 PostgreSQL-Read-Tests bestanden, einschließlich
  Ablaufgrenze, Zukunft/Legacy, Pruning, Replay und konkurrierender Zulassung.
  Zusätzlich 58 PostgreSQL-Node-Write- und 9 Account-Write-Tests bestanden.
  Insgesamt 166 Tests, ohne Fehler; Remote-CI separat.
  Dieser Nachweis ist kein Produktionsrollout.
