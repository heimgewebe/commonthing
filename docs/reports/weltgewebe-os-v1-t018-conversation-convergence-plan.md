---
id: reports.weltgewebe-os-v1-t018-conversation-convergence-plan
title: "WELTGEWEBE-OS-V1-T018: Governance- und Knotengespräche kontrolliert konvergieren"
doc_type: report
status: active
lifecycle_state: active
lifecycle: planning
owner_task: WELTGEWEBE-OS-001
review_after: 2026-10-20
canonicality: evidence
created: 2026-07-20
lang: de
summary: >
  Commit- und produktionsgebundener Umsetzungsplan, um den separaten
  Governance-Gesprächspfad verlustfrei in das kanonische Conversation- und
  Message-Modell zu überführen, ohne Doppelwahrheit, ungeprüfte Rechteausweitung
  oder irreversiblen Ein-Schritt-Cutover.
relations:
  - type: relates_to
    target: docs/specs/governance-antraege.md
  - type: relates_to
    target: docs/datenmodell.md
  - type: relates_to
    target: contracts/domain/conversation.schema.json
  - type: relates_to
    target: contracts/domain/message.schema.json
  - type: relates_to
    target: apps/api/src/routes/conversations.rs
  - type: relates_to
    target: apps/api/src/governance.rs
  - type: relates_to
    target: apps/web/src/lib/components/governance/ProposalDetail.svelte
---

# WELTGEWEBE-OS-V1-T018: Gesprächsmodelle kontrolliert konvergieren

## 1. Entscheidung in einem Satz

commonThing erweitert das bestehende kanonische Modell
`domain_conversations` / `domain_messages` um den ausdrücklich typisierten
Gesprächsgegenstand `governance_proposal`, schaltet den Governance-Pfad in drei
reversiblen Stufen auf dieses Modell um und entfernt `governance_messages` erst
nach produktivem Mengen-, Hash-, API- und Browserbeweis.

Für Nicht-Programmierer: Heute gibt es zwei verschiedene Schubladen für
Gesprächsbeiträge. Der Plan baut eine gemeinsame, besser geschützte Schublade,
kopiert vorhandene Inhalte kontrolliert hinein, leitet neue Beiträge nur noch
dorthin und entsorgt die alte Schublade erst, wenn alles nachgezählt wurde.

## 2. Auftrag und Abgrenzung

Dieser Plan konkretisiert den Bureau-Task `WELTGEWEBE-OS-V1-T018`.
Voraussetzung ist der abgeschlossene Task `WELTGEWEBE-OS-V1-T017` mit
Weltgewebe-PR #1501.

Der Plan umfasst:

- ein gemeinsames physisches Conversation- und Message-Modell;
- genau ein Governance-Gespräch je Antrag;
- verlustfreie und idempotente Bestandsmigration;
- eine einzige Schreibwahrheit zu jedem Zeitpunkt;
- gemeinsame Schutzmechanismen für Pagination, Idempotenz, Rate-Limit,
  Bearbeitung, Tombstone-Löschung und Autorenentmachtung;
- kompatible Übergangsrouten und eine schrittweise UI-Umstellung;
- Produktionsbeweise und spätere Entfernung des alten Pfads.

Der Plan implementiert noch nichts und autorisiert insbesondere nicht:

- private Nachrichten, Anhänge, Audio oder Video;
- föderierte Gesprächszustellung;
- eine ungeprüfte Produktionsmigration;
- das Löschen historischer Beiträge ohne vorherigen Beweis;
- einen unbegrenzten Backfill;
- die pauschale Änderung aller Governance- oder Webungsrechte.

## 3. Belegter Ausgangszustand

### 3.1 Identität des geprüften Stands

| Gegenstand | Belegter Wert am 20. Juli 2026 |
|---|---|
| Weltgewebe `origin/main` | `3f4d16a3b695530cb4ffbd7184cbf5bf8e94d675` |
| öffentliche API-Produktion | `3f4d16a3b695530cb4ffbd7184cbf5bf8e94d675` |
| T017-Merge-Commit | `2b2feaf11a1a6da0dc573764e4fe7c14a358685b` |
| T017-Diff-SHA-256 | `2394ce1268ad4ae188591018876cffc154c81eb0a695343363bd307f0903f7cb` |
| T018-Bureau-Zustand | `planned`, Lane `later`, Rang 18 |

### 3.2 Rein lesende Produktionsinventur

Die Abfrage lief direkt gegen die produktive PostgreSQL-Datenbank. Sie änderte
keine Zeile.

| Bestand | Anzahl |
|---|---:|
| `governance_proposals` | 0 |
| `governance_messages` | 0 |
| `domain_conversations` | 3 |
| `domain_messages` | 2 |
| alte Governance-Beiträge ohne vorhandenen Account | 0 |
| Anträge mit alten Gesprächsbeiträgen | 0 |

Folge: Auf der aktuellen Produktion ist kein historischer Governance-Beitrag zu
migrieren. Die Migrationslogik bleibt trotzdem Pflicht, weil sie auch bei
zwischenzeitlich entstehenden Daten, Testfixtures, Wiederherstellungen und
anderen Installationen korrekt sein muss. Ein Nullbestand ist kein Beweis für
einen korrekten Migrationsalgorithmus.

### 3.3 Zwei derzeitige Wahrheiten

#### Alter Governance-Pfad

`governance_messages` speichert:

- `id`, `proposal_id`, `author_account_id`, `author_title`, `body`, `created_at`;
- keine Änderungszeit;
- keine Tombstone-Löschung;
- keinen Idempotenzschlüssel;
- keine Pagination und kein Rate-Limit;
- keine Foreign-Key-Entmachtung gelöschter Autoren.

Die API verwendet eigene Routen unter `/proposals/{id}/messages`. Beiträge sind
nur während einer offenen Antragsphase möglich. Die Weboberfläche besitzt eine
eigene einfache Darstellung ohne Bearbeiten oder Löschen.

#### Kanonischer Knotengesprächspfad

`domain_conversations` / `domain_messages` besitzen:

- öffentliche Gespräche und stabil sortierte Nachrichten;
- Autoren-Snapshot und `ON DELETE SET NULL` für dauerhafte Entmachtung;
- Idempotenzschlüssel;
- `created_at`, `updated_at`, `deleted_at`;
- Tombstone-Löschung;
- Cursor-Pagination;
- transaktionssicheres Rate-Limit;
- Schutz vor überholten Änderungen mittels `If-Match`;
- datensparsame Outbox-Ereignisse.

Das aktuelle Schema erlaubt bei `conversation_type` ausschließlich `node` und
verlangt einen eindeutigen `node_id`. Es kann Governance-Anträge daher noch
nicht korrekt ausdrücken.

## 4. Der zentrale Widerspruch

Eine bloße Tabellenumbenennung wäre falsch. Der alte Pfad und der neue Pfad
haben verschiedene fachliche Fähigkeiten und Schutzstufen. Würde man nur
`governance_messages` nach `domain_messages` kopieren, blieben mindestens vier
Fragen unbeantwortet:

1. Welcher Antrag ist der Gegenstand des Gesprächs?
2. Welche Schreibregel gilt in welcher Antragsphase?
3. Was geschieht mit gelöschten oder nicht mehr vorhandenen Autoren?
4. Wie werden alte Beiträge ohne ursprünglichen Idempotenzschlüssel eindeutig
   und wiederholbar übernommen?

Zusätzlich widerspricht der derzeitige normative Governance-Vertrag der neueren
Produktentscheidung: Er verbietet Gästen Gesprächsbeiträge und andere
Webungsaktionen. Die festgelegte Zielrichtung lautet dagegen, dass angemeldete
Gäste bereits mitreden und Webungsaktionen ausführen dürfen; ausgeschlossen
bleibt insbesondere die Bearbeitung fremder Inhalte.

T018 darf diesen Konflikt nicht verdecken. Die Gesprächskonvergenz setzt deshalb
einen aktualisierten Berechtigungsvertrag voraus. Die übrigen Gastrechte für
Knoten, Fäden, Garnrollen, Veto und Abstimmung werden in einer eigenen,
koordinierten Aufgabe behandelt und nicht als Nebenwirkung dieser
Datenmigration eingeführt.

## 5. Zielmodell

### 5.1 Gesprächsgegenstand ausdrücklich modellieren

`domain_conversations` wird additiv erweitert:

- `node_id` wird nullable;
- neue Spalte `proposal_id UUID REFERENCES governance_proposals(id)`;
- `conversation_type` erlaubt `node` und `governance_proposal`;
- eine Check-Constraint erzwingt genau einen passenden Gegenstand;
- partielle Unique-Indizes erzwingen genau ein Gespräch je Knoten oder Antrag.

Sinngemäßer Vertrag:

```sql
CHECK (
  (conversation_type = 'node'
   AND node_id IS NOT NULL
   AND proposal_id IS NULL)
  OR
  (conversation_type = 'governance_proposal'
   AND node_id IS NULL
   AND proposal_id IS NOT NULL)
)
```

Warum zwei ausdrückliche Foreign Keys statt `subject_type + subject_id`:
PostgreSQL kann so die Existenz des Knotens oder Antrags direkt garantieren. Ein
allgemeiner polymorpher Textverweis wäre flexibler, verlöre aber diese starke
Datenbankgarantie und bräuchte fehleranfällige Trigger.

### 5.2 Deterministische Gesprächs-ID

Jeder Antrag erhält eine reproduzierbare Gesprächs-ID, abgeleitet aus:

```text
weltgewebe:governance-proposal-conversation:v1:<proposal_id>
```

Die Ableitung folgt der vorhandenen deterministischen ID-Strategie für
Knotengespräche. Wiederholte Migrationen erzeugen dadurch keine zweiten
Gespräche.

### 5.3 Antragsphase nicht duplizieren

Der Zustand `consent`, `voting`, `accepted` oder `rejected` bleibt ausschließlich
in `governance_proposals`. `domain_conversations` speichert keine zweite
`open`- oder `closed`-Wahrheit.

Beim Schreiben sperrt die gemeinsame Conversation-Schicht den zugehörigen
Antrag und prüft die aktuelle Phase. So bleibt ein abgeschlossener Antrag
lesbar, nimmt aber keine neuen Beiträge mehr an.

### 5.4 Nachrichtensemantik vereinheitlichen

Alte Governance-Beiträge werden wie folgt abgebildet:

| Alt | Ziel |
|---|---|
| `governance_messages.id` | unverändert `domain_messages.id` |
| `proposal_id` | deterministisch auf `conversation_id` auflösen |
| `author_account_id` | nur behalten, wenn der Account existiert; sonst `NULL` |
| `author_title` | unverändert als dauerhafter Snapshot |
| `body` | `content` |
| kein Idempotenzschlüssel | deterministisch aus der alten Message-ID ableiten |
| `created_at` | unverändert |
| keine Änderungszeit | `updated_at = created_at` |
| keine Löschinformation | `deleted_at = NULL` |

Der deterministische Migrations-Idempotenzschlüssel darf nicht mit regulären
Client-Schlüsseln kollidieren und muss durch einen eigenen Namensraum gebunden
sein.

## 6. Berechtigungsvertrag

### 6.1 Ziel für Gespräche

| Handlung | Öffentlich ohne Anmeldung | Gast | Weber | Admin |
|---|---:|---:|---:|---:|
| Gespräch lesen | ja | ja | ja | ja |
| Beitrag in offenem Gespräch erstellen | nein | ja | ja | ja |
| eigenen Beitrag bearbeiten | nein | ja | ja | ja |
| fremden Beitrag bearbeiten | nein | nein | nein | nein |
| eigenen Beitrag tombstonen | nein | ja | ja | ja |
| fremden Beitrag tombstonen | nein | nein | nein | ja |
| Beitrag in abgeschlossenem Antrag erstellen | nein | nein | nein | nein |

Diese Matrix gilt für die Gesprächsschicht. Die weitergehende Produktregel zu
allen Webungsaktionen benötigt einen eigenen normativen und getesteten
Berechtigungs-Slice. T018 darf dafür gemeinsame Auth-Helfer vorbereiten, aber
nicht Veto, Abstimmung, Knoten- oder Fadenrechte ungeprüft verändern.

### 6.2 Kein stilles `require_write`

Der heutige Middleware-Helfer `require_write` bedeutet faktisch
`weber | admin`. Er ist für die neue Gesprächsmatrix zu grob. Die
Conversation-Routen erhalten eine ausdrücklich benannte Prüfung, zum Beispiel
`require_authenticated_conversation_write`, während Autor- und Adminrechte
weiter im Handler geprüft werden.

Damit wird die Produktregel lesbar und testbar, statt als Nebenwirkung einer
allgemeinen Rollenprüfung verborgen zu bleiben.

## 7. Umsetzung in drei Releases

Ein Release ist ein exakt gebauter und deployter Git-Commit. Die Aufteilung
verhindert, dass Schema, Datenkopie, API-Umschaltung und Löschen der alten
Tabelle in einem irreversiblen Schritt zusammenfallen.

### Release A — Additives Zielmodell und adaptive Runtime

Ziel: Der neue Code versteht beide Zustände, ohne den alten Pfad bereits
abzuschalten.

1. `domain_conversations` additiv um Governance-Gegenstände erweitern.
2. Deterministische Gesprächserzeugung für vorhandene und neue Anträge ergänzen.
3. Gemeinsamen Conversation-Service aus dem node-spezifischen Router lösen.
4. Typabhängige Schreibregel ergänzen:
   - Node-Gespräch: öffentlich lesbar, angemeldete Rollen gemäß neuer Matrix;
   - Governance-Gespräch: zusätzlich offene Antragsphase prüfen.
5. Eine kleine Cutover-Zustandstabelle oder gleichwertige kanonische
   Datenbankmarke einführen.
6. Governance-Read- und Write-Pfade wählen ihre Quelle anhand dieser Marke:
   - vor Cutover: alter Pfad;
   - nach Cutover: kanonischer Pfad.
7. Alte und neue API-Replikate dürfen nicht gleichzeitig unterschiedliche
   Tabellen beschreiben. Daher wird die Cutover-Marke erst nach vollständigem
   Rollout von Release A gesetzt.

Wichtig: Release A schreibt nie gleichzeitig in beide Nachrichtentabellen.
Dual-Write würde neue Abweichungen erst erzeugen, die T018 beseitigen soll.

### Release B — Gebundene Datenmigration und Umschaltung

Ziel: Bestand kopieren, beweisen und in derselben kontrollierten Operation auf
die neue Wahrheit umschalten.

1. Produktionsbackup und PITR-Bereitschaft nachweisen.
2. Vorherige Inventur als Receipt sichern:
   - Zeilenzahlen;
   - Anzahl betroffener Anträge;
   - verwaiste Autoren;
   - minimale und maximale Zeitstempel;
   - kanonischer SHA-256 über sortierte fachliche Felder.
3. Eine transaktionsgebundene Advisory-Lock-Kennung für den Cutover erwerben.
4. Alte Governance-Schreiber über die Cutover-Marke fail-closed sperren.
5. Fehlende Governance-Gespräche deterministisch anlegen.
6. Alte Beiträge mit `INSERT ... ON CONFLICT` idempotent übernehmen.
7. Zielzählung und Zielhash innerhalb derselben Operation vergleichen.
8. Nur bei vollständiger Übereinstimmung die Cutover-Marke auf `canonical`
   setzen und committen.
9. Bei jeder Abweichung die gesamte Transaktion zurückrollen.
10. Die alte Tabelle anschließend nur noch read-only als Rückfallevidenz
    erhalten.

### Release C — UI-Konvergenz und Legacy-Rückbau

Ziel: Die gemeinsame Wahrheit sichtbar nutzen und erst nach Beobachtungsfrist
die alte Fläche entfernen.

1. Eine gemeinsame Conversation-Clientbibliothek für Node und Governance nutzen.
2. Wiederverwendbare Thread-Komponente einführen; Governance behält eigene
   Antragsdarstellung, Veto, Abstimmung und Verlauf.
3. Proposal-Routen zunächst als Kompatibilitätsadapter auf den kanonischen
   Service erhalten.
4. Eigene Bearbeiten- und Tombstone-Aktionen zugänglich anbieten.
5. Cursor-Pagination und sichtbarkeitsgebundenes Aktualisieren übernehmen.
6. Nach mindestens einem erfolgreichen Produktionszyklus und vollständigem
   Readback:
   - alte Schreibfunktionen entfernen;
   - `governance_messages` in einer separaten Migration löschen;
   - Kompatibilitätsadapter nur entfernen, wenn keine produktive UI oder
     Integration sie mehr verwendet.

## 8. Begrenzung der Outbox-Wirkung

Eine Bestandsmigration darf nicht pro historischem Beitrag die Kartenprojektion
invalidieren oder einen unbegrenzten Ereignisschub erzeugen.

Der Migrationstrigger benötigt deshalb einen ausdrücklich transaktionslokalen
Backfill-Modus. In diesem Modus:

- werden keine inhaltsreichen Einzelereignisse erzeugt;
- bleiben reguläre Laufzeitschreibvorgänge unverändert atomar;
- wird nach erfolgreicher Migration genau ein zusammenfassendes, datensparsames
  Ereignis mit Conversation-Typ, Anzahl und Lauf-ID erzeugt;
- erscheinen weder Nachrichtentext noch private Identifikatoren in der Outbox.

Die Projektoren müssen weiterhin typgebunden entscheiden. Eine
Governance-Nachricht ist kein Grund, die Kartenprojektion eines Knotens neu zu
berechnen.

## 9. Datenbeweis

### 9.1 Kanonischer Quellhash

Der Quellhash wird über nach `created_at, id` sortierte Datensätze gebildet und
bindet mindestens:

```text
message_id
proposal_id
author_account_id oder leer
author_title
body
created_at in UTC
```

### 9.2 Kanonischer Zielhash

Der Zielhash normalisiert die Zielspalten zurück auf dieselbe fachliche Form.
Er darf technische Zusatzfelder wie den deterministischen Idempotenzschlüssel
nur separat prüfen, nicht in die Gleichheit des Inhalts hineinmischen.

### 9.3 Pflichtgleichheiten

Vor Umschaltung müssen gelten:

- Quellzeilen = Zielzeilen für `governance_proposal`;
- Quellanträge mit Nachrichten = Zielgespräche mit Nachrichten;
- Quellhash = Zielhash;
- jede alte Message-ID existiert genau einmal im Ziel;
- jeder Antrag besitzt genau ein Governance-Gespräch;
- kein Node-Gespräch wurde verändert;
- verwaiste Autoren besitzen im Ziel `author_account_id = NULL`, aber denselben
  Anzeigenamen und Inhalt.

Der Nullbestand der Produktion wird mitgetestet, aber zusätzlich sind gefüllte
Fixtures Pflicht.

## 10. Rollback nach Phase

### Vor Release A

Kein neuer Zustand. Branch oder PR kann ohne Datenwirkung verworfen werden.

### Nach Release A, vor Cutover

Additive Spalten, Constraints und Codepfade sind ungenutzt. Rückbau erfolgt über
die passende Down-Migration oder einen nachfolgenden Korrekturcommit.

### Während Release B

Zähl- oder Hashfehler rollen die Cutover-Transaktion vollständig zurück. Die
alte Tabelle bleibt Schreibwahrheit.

### Nach erfolgreichem Cutover, vor Legacy-Drop

Die Cutover-Marke kann nur nach einem geprüften Rückfallplan zurückgesetzt
werden. Neue Bearbeitungen und Tombstones besitzen im alten Schema keine
verlustfreie Darstellung. Deshalb ist ein blindes Rückkopieren ausgeschlossen.
Mögliche Rückfälle sind:

1. bevorzugt ein Vorwärtsfix, während die kanonischen Daten erhalten bleiben;
2. bei schwerem Datenfehler PITR auf den unmittelbar vor Cutover bewiesenen
   Wiederherstellungspunkt;
3. kein manuelles Umschreiben einzelner Produktionszeilen.

### Nach Legacy-Drop

Ein Rückfall auf das alte Modell ist nur noch über Backup/PITR möglich. Der Drop
ist deshalb eine eigene, später gemergte und separat freizugebende Migration.

## 11. Test- und Beweismatrix

### PostgreSQL

- leere Datenbank;
- mehrere Anträge mit und ohne Beiträge;
- identische Zeitstempel und stabile UUID-Sortierung;
- Unicode, Zeilenumbrüche und Grenzlängen;
- vorhandener und gelöschter Autor;
- wiederholter Backfill ohne neue Zeile;
- absichtlich veränderte Zielzeile führt zum Hashabbruch;
- konkurrierender Alt-Schreibversuch während Cutover wird blockiert;
- Node-Gespräche und ihre Schutztrigger bleiben unverändert;
- Up- und Down-Migration auf PostgreSQL 16.

### API

- öffentliche Lesewege für beide Conversation-Typen;
- Proposal-Adapter und kanonische Route liefern denselben Inhalt;
- Gast, Weber und Admin gemäß Gesprächsmatrix;
- fremde Bearbeitung immer verboten;
- Admin darf fremd tombstonen, aber nicht fremd bearbeiten;
- abgeschlossener Antrag bleibt lesbar und verweigert neue Beiträge;
- Idempotenz, Rate-Limit, `If-Match` und Cursorgrenzen für Governance;
- gelöschter Account erbt nach Neuanlage mit gleicher Text-ID keine
  Autorenrechte.

### Web

- Proposal-Detail nutzt den kanonischen Thread;
- Entwurf bleibt bei Aktualisierung erhalten;
- eigene Bearbeiten- und Entfernen-Aktionen sind tastaturbedienbar;
- Gast kann im offenen Gespräch schreiben;
- abgeschlossener Antrag zeigt verständlichen Lesezustand;
- alte und neue Pagination werden nicht doppelt dargestellt;
- Kontextwechsel überschreibt keine neueren Nachrichten;
- Chromium und Firefox, Desktop und schmale Ansicht, ohne Überlauf und
  Konsolenfehler.

### Rollout-Simulation

- Release-A-Instanzen vor Cutover nutzen alle den alten Pfad;
- nach gesetzter Marke nutzen alle Release-A-Instanzen den kanonischen Pfad;
- alte, noch nicht Release-A-fähige Instanzen verhindern die Freigabe;
- Wiederholung nach Prozessabbruch bleibt idempotent;
- ein fehlgeschlagener Beweis setzt die Marke nicht.

## 12. Merge- und Produktionsgates

Vor jedem Merge müssen vorliegen:

- exakter Basis- und Head-Commit;
- vollständiger Diff als herunterladbares Artefakt;
- SHA-256 des Diffs;
- Self-Review und unabhängiger kontrastierender Review, beide an denselben Head
  und Diff gebunden;
- grüne Rust-, Web-, Python-, PostgreSQL- und Browserprüfungen;
- Migrations-Dry-Run mit Mengen- und Hashreceipt;
- beweisbarer Rückfallpunkt;
- Required Merge Gate grün.

Nach dem Cutover müssen zurückgelesen werden:

- exakter Produktionscommit von Frontend und API;
- Cutover-Marke und angewandte Migration;
- alte und neue Zeilenzahlen sowie Hashgleichheit;
- mindestens ein repräsentativer Governance-Leseweg;
- Gast-Schreibweg in offenem Gespräch;
- eigene Bearbeitung und Tombstone-Löschung;
- verweigerte fremde Bearbeitung;
- geschlossener Antrag;
- Outbox-Mengen und ausbleibende Karteninvalidierung;
- kein aktiver Alt-Schreiber.

Erst danach darf Release C den Legacy-Drop vorbereiten.

## 13. Arbeitspakete

| Paket | Inhalt | Optimierungsgrad | Hauptrisiko |
|---|---|---:|---|
| T018-A | Normativer Conversation- und Berechtigungsvertrag | hoch | Produktregel bleibt widersprüchlich |
| T018-B | Additives Schema und deterministische Antrag-Gespräche | mittel | Constraint- oder FK-Fehler |
| T018-C | Gemeinsamer Conversation-Service und adaptive Quelle | hoch | geteilte Replikate schreiben verschieden |
| T018-D | Dry-Run, Hashbeweis und transaktionaler Cutover | sehr hoch | Datenverlust oder Doppelwahrheit |
| T018-E | Governance-UI auf gemeinsamen Thread umstellen | mittel | Bedien- oder Zustandsregression |
| T018-F | Produktionsreadback und Beobachtungsfrist | hoch | versteckter Alt-Schreiber |
| T018-G | Separater Legacy-Drop | mittel | zu frühe Irreversibilität |

`Optimierungsgrad` bezeichnet hier, wie stark ein Paket auf Sicherheit,
Wiederholbarkeit und spätere Wartung statt auf die kleinste Codeänderung
optimiert ist.

## 14. Alternativpfad

### Alternative: Alte Tabelle behalten und nur die Oberfläche angleichen

Man könnte `governance_messages` behalten, im UI dieselben Komponenten verwenden
und Schutzfunktionen dort nachbauen.

Vorteile:

- kleinere anfängliche Migration;
- geringeres kurzfristiges Schemaänderungsrisiko.

Nachteile:

- dauerhaft zwei Nachrichtentabellen und zwei Schreibdienste;
- Schutzlogik muss doppelt implementiert und getestet werden;
- spätere Features driften erneut auseinander;
- T018-Ziel einer kanonischen Wahrheit wird nicht erreicht.

Bewertung: sinnvoll nur, wenn das kurzfristige Änderungsrisiko absolut höher
gewichtet wird als Architektur, Sicherheit und Wartbarkeit. Für den erklärten
T018-Auftrag wird die Alternative verworfen.

### Alternative: Allgemeines `subject_type + subject_id`

Vorteil: beliebig viele spätere Gesprächsgegenstände ohne neue Spalte.

Nachteil: PostgreSQL kann die referenzierte Zeile nicht mit einem einfachen
Foreign Key garantieren. Integrität wandert in Trigger und Anwendungscode.

Bewertung: erst sinnvoll, wenn mindestens ein dritter realer
Gesprächsgegenstand mit klarer Semantik ansteht. Für Node und Antrag sind zwei
explizite Foreign Keys sicherer.

## 15. Risiken und Gegenmaßnahmen

| Risiko | Gewicht | Gegenmaßnahme |
|---|---:|---|
| Rollenregel bleibt widersprüchlich | hoch | normativen Berechtigungsslice vor Schema-Cutover mergen |
| gemischte API-Replikate | hoch | adaptive Release-A-Runtime und DB-gebundene Cutover-Marke |
| historische Autoren erhalten falsche Rechte | hoch | FK `ON DELETE SET NULL`, Snapshot erhalten, Wiederverwendungs-Test |
| Backfill erzeugt Ereignissturm | hoch | transaktionslokaler Backfill-Modus und ein Summenereignis |
| Hash normalisiert Inhalte falsch | hoch | byte- und fachlich definierte UTC-/Textkanonisierung mit Fixtures |
| Nullbestand vermittelt falsche Sicherheit | mittel | gefüllte und fehlerhafte PostgreSQL-Fixtures als Pflichtgate |
| Legacy-Drop erfolgt zu früh | hoch | eigener PR nach Produktionsbeweis und Beobachtungsfrist |
| Rückfall nach neuen Edits ist nicht verlustfrei | hoch | Vorwärtsfix bevorzugen, PITR als harter Rückfall, kein Blind-Reverse-Copy |

## 16. Definition of Done

T018 ist erst `verified`, wenn:

1. jeder Knoten und jeder Antrag genau ein typisiertes Gespräch besitzen kann;
2. alle Governance-Beiträge ausschließlich in `domain_messages` geschrieben
   werden;
3. vorhandene Beiträge nach Anzahl, ID und fachlichem SHA-256 identisch
   übernommen sind;
4. Node- und Governance-Gespräche dieselben technischen Schutzmechanismen
   verwenden;
5. die beschlossene Gesprächs-Berechtigungsmatrix vollständig getestet ist;
6. Produktion auf dem exakten Merge-Commit läuft und repräsentative Livewege
   bestanden sind;
7. kein alter Schreiber mehr existiert;
8. `governance_messages` erst nach separatem Rückbau- und Readback-Gate entfernt
   wurde;
9. Branch, Worktree, Leases und Beweisartefakte ordnungsgemäß abgeschlossen
   sind.

## 17. Freigabeentscheidung

Der Plan ist umsetzungsreif, wenn zusätzlich der normative Gast-
Berechtigungsvertrag veröffentlicht und der Task ausdrücklich aus `later` in
eine aktive Queue-Lane priorisiert wurde.

Bis dahin bleibt T018 geplant. Es erfolgt keine Produktionsmigration und keine
stille Änderung von Rollenrechten.
