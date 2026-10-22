---
id: datenmodell
title: Datenmodell
doc_type: reference
status: active
summary: Aktuelles physisches Datenmodell, lokale Legacy-Defaults und PostgreSQL-Produktionsvertrag.
relations:
  - type: relates_to
    target: architecture/overview.md
  - type: relates_to
    target: docs/domain/vocabulary.md
  - type: relates_to
    target: docs/specs/objektlebenszyklen-und-loeschwirkungen.md
  - type: relates_to
    target: docs/techstack.md
  - type: relates_to
    target: docs/deploy/README.md
---

# Datenmodell

## Wichtigste Aussage

Der Repository-Produktionsvertrag in `infra/compose/compose.prod.yml` verwendet
PostgreSQL als Lese- und Schreibwahrheit für Accounts/Garnrollen, Knoten und
Fäden. Passkey-Credentials werden dort ebenfalls aus PostgreSQL gelesen und
geschrieben; `DATABASE_URL` bindet Sitzungen und weitere DB-Pfade an dieselbe
Persistenzschicht. Eine Aussage über den tatsächlich laufenden Server verlangt
zusätzlich einen datierten Runtime-Beleg.

JSONL bleibt der lokale, testbare Code-Default sowie ein historischer Import-,
Export- und ausdrücklich freizugebender Rückfallpfad. Dieser lokale Default ist
keine Aussage über die Produktionsarchitektur und kein stiller
Produktionsfallback.

## Quellenmatrix

| Domäne | Lokaler/Legacy-Default | Produktionsvertrag | Physische PostgreSQL-Fläche |
|---|---|---|---|
| Accounts/Garnrollen | JSONL lesen und anhängen | PostgreSQL lesen und schreiben | `domain_accounts` |
| Knoten | JSONL lesen und umschreiben | PostgreSQL lesen; `POST` und `PATCH` schreiben | `domain_nodes` |
| Fäden | JSONL lesen und anhängen | PostgreSQL lesen; fachliche Webungsaktionen schreiben serverseitig | `domain_edges` |
| Sitzungen | In-Memory ohne `DATABASE_URL` | PostgreSQL bei verpflichtender `DATABASE_URL` | `sessions` |
| Passkeys | In-Memory | PostgreSQL als Produktionsdefault | `passkey_credentials` |
| Öffentliche Knotengespräche | kein JSONL-Pfad | PostgreSQL lesen und schreiben | `domain_conversations`, `domain_messages` |

PostgreSQL-Schreiben verlangt PostgreSQL-Lesen. Die API verweigert ungültige
Mischzustände, damit Änderungen nach einem Neustart nicht unsichtbar werden.
Es gibt keinen allgemeinen Dual-Write. `compose.prod.yml` setzt Lesequelle und
alle vorhandenen Domänenschreibquellen gemeinsam auf `postgres`;
`.env.example` bleibt dagegen absichtlich eine lokal startbare JSONL-Vorlage.

Für Fäden gilt unabhängig von der physischen Quelle: Freie `note`-Texte werden
persistiert, gehören aber nicht zur öffentlichen Projektion von `GET /edges`,
`GET /edges/{id}` oder `GET /accounts/{id}`. Bei regulären Webungsaktionen darf
ein Account-Endpunkt nur beteiligt sein, wenn der Faden von der eigenen
angemeldeten Garnrolle ausgeht. Bereits bekannte Account-IDs müssen als
`account` typisiert sein; öffentliche Projektionen vertrauen niemals einer
bloßen ID-Kollision mit einem als `node` deklarierten Endpunkt. Eingehende
Account-Fäden sind dem administrativen Import-/Reparaturpfad vorbehalten und
werden als eingehende Beziehung statt als Eigenhandlung projiziert.

## Physische PostgreSQL-Tabellen

Die folgende Übersicht wird aus den aktuellen Migrationen abgeleitet.

### `sessions`

| Spalte | Typ | Bedeutung |
|---|---|---|
| `id` | `TEXT` PK | serverseitige Session-ID |
| `account_id` | `TEXT` | zugehöriger Account |
| `device_id` | `TEXT` | Browser-/Gerätebezug |
| `created_at` | `TIMESTAMPTZ` | Erstellung |
| `last_active` | `TIMESTAMPTZ` | letzte Aktivität |
| `expires_at` | `TIMESTAMPTZ` | serverseitiger Ablauf |

Derzeit besteht kein Foreign Key auf `domain_accounts`, weil lokale Legacy-,
Import- und ausdrücklich freigegebene Rückfallpfade weiterhin JSONL-Identitäten
verarbeiten können.

### `domain_accounts`

| Gruppe | Spalten |
|---|---|
| Identität | `id`, `kind`, `title`, `mode` |
| Sichtbarkeit/Ort | `radius_m`, `location_lat`, `location_lon` |
| Betrieb/Auth | `disabled`, `role`, `email`, `webauthn_user_id` |
| Zeit | `created_at`, `updated_at` |
| Restfelder | `public_payload`, `private_payload` |

E-Mail-Adressen besitzen einen trim-/case-normalisierten partiellen Unique-Index.
Bei `map_state=radius` wird `public_pos` aus einer kryptografisch zufälligen,
privat persistierten Projektionsbindung gelesen. Sie bleibt bei unverändertem Ort
und Radius stabil, auch nach zeitweisem Ausblenden, und wird erst bei Änderung
eines dieser Werte ersetzt. Fehlt eine gültige Bindung, wird die Garnrolle
fail-closed als `not_on_map` projiziert. Die private Position und die Bindung
erscheinen nie in der öffentlichen Account-Antwort.

`kind` ist auf `garnrolle` begrenzt. `map_state` enthält `not_on_map`, `exact`
oder `radius`. Eine separate Identitäts- oder Modusspalte existiert nicht mehr.

### `domain_nodes`

| Spalte | Typ | Bedeutung |
|---|---|---|
| `id` | `TEXT` PK | Knoten-ID |
| `kind` | `TEXT` | Knotentyp |
| `title` | `TEXT` | Titel |
| `lat`, `lon` | `DOUBLE PRECISION` | optionale Position |
| `created_at`, `updated_at` | `TIMESTAMPTZ` | Zeitangaben |
| `payload` | `JSONB` | übrige JSONL-Felder einschließlich optionaler unveränderlicher `created_by_account_id` |
| `create_actor_id`, `create_operation_id` | `TEXT`, nullable | accountgebundene Wiederholungssicherheit für `POST /nodes` |

Neue Knoten erhalten `created_by_account_id` ausschließlich aus der
Authentifizierungssitzung. Das Feld wird bei späteren Ersetzungen beibehalten.
Altbestand und nach einem Gast-Austritt anonymisierte Knoten besitzen keine
aktive Urheberbindung; Gäste können sie deshalb nicht bearbeiten, Weber und
Administratoren jedoch gemeinschaftlich pflegen.

Es gibt derzeit keine PostGIS-Geometrie. Der Geoindex ist ein einfacher B-Tree
auf `(lat, lon)`.

Knoten können auch Wissen, Dokumente und digitale Commons ohne festen Ort
beschreiben. `address` und `location` sind unabhängig optional: fehlend oder
`null` bedeutet jeweils „nicht angegeben“. Eine vorhandene Position enthält
immer beide endlichen WGS84-Werte (`lat` −90 bis 90, `lon` −180 bis 180).
`POST` und vollständiges `PUT` akzeptieren beide Formen; beim `PUT` entfernt
ein fehlender oder `null` gesetzter Ortswert die bisherige Projektion. `PATCH`
behält seinen bestehenden Teilvertrag für Info und Suchsichtbarkeit.

Die additive Migration `20261017000001_optional_node_location` ergänzt einen
`NOT VALID`-Check für vollständige, gültige Koordinatenpaare. Sie schreibt keine
Bestandsdaten um; die bereits nullable Spalten speichern ortslose Knoten als
`NULL`/`NULL`. Neue Schreibvorgänge werden geprüft, historisch ungültige
Teilpaare benötigen weiterhin eine explizite Datenbereinigung. Der Loader liest
gültige ortslose Knoten regulär. BBOX-Abfragen liefern ausschließlich verortete
Knoten; allgemeine Listen, Einzelabruf, Suche, Urheberbindung und abgeleitete
Fäden setzen keinen Ort voraus. `/commons` bietet eine gemeinsame Listen- und
Suchansicht; Detail-Links nutzen weiter `/map?focus=node:<id>` ohne erfundene
Kartenposition oder Kamerafahrt für ortslose Knoten.

Contract-Weiterentwicklung zu Issue #1466: Der bestehende Node-Schema-Identifier
und die API-Endpunkte bleiben bestehen; das Antwortwertespektrum erweitert sich
um fehlende beziehungsweise `null` gesetzte Ortswerte. Vorhandene geografische
Requests und Antworten behalten ihre Form. Clients mit bisher zwingendem
`location`-Typ müssen vor Freischaltung ortsloser Schreibvorgänge aktualisiert
werden. Migration, API und nullable-fähiger Webclient gehören deshalb in einen
gemeinsamen Release; ein alter Client ist für neue ortslose Datensätze nicht
kompatibel. Ein Rollback darf keine Ersatzkoordinaten erzeugen oder diese
Datensätze löschen; der Migration-Down entfernt nur den zusätzlichen Check.

`create_actor_id` und `create_operation_id` sind entweder gemeinsam gesetzt
oder gemeinsam `NULL`. Der Accountwert muss nichtleer und die Vorgangskennung
eine kanonische UUID sein. Ein partieller Unique-Index erlaubt pro Account nur
einen Knoten je Vorgangskennung. Die Vorgangskennung ist keine Knoten-ID und
keine fachliche Duplikaterkennung nach Titel, Adresse oder Koordinaten. Sie
identifiziert ausschließlich die Wiederholung desselben Speichervorgangs.

### `domain_edges`

| Spalte | Typ | Bedeutung |
|---|---|---|
| `id` | `TEXT` PK | Faden-ID |
| `source_id`, `target_id` | `TEXT` | Endpunkte |
| `edge_kind` | `TEXT` | Beziehungsart |
| `created_at` | `TIMESTAMPTZ` | Erstellung |
| `payload` | `JSONB` | Typinformationen, Notiz und Restfelder |
| `create_actor_id`, `create_operation_id` | `TEXT`, nullable | accountgebundene Wiederholungssicherheit für `POST /edges` |

Foreign Keys sind bewusst noch nicht gesetzt. Vor einer FK-Entscheidung ist ein
Orphan- und Referenzaudit erforderlich; bestehende Fäden dürfen nicht
stillschweigend verworfen werden.

Für Fäden gilt derselbe Vorgangsvertrag wie für Knoten: Account und
Vorgangskennung sind gemeinsam eindeutig. Ein Wiederholungsversuch mit derselben
fachlichen Anfrage liefert den bereits gespeicherten Faden; eine Wiederverwendung
derselben Kennung für andere Daten wird als Konflikt abgewiesen.

### `passkey_credentials`

| Spalte | Typ | Bedeutung |
|---|---|---|
| `credential_id` | `TEXT` PK | hexkodierte WebAuthn-Credential-ID |
| `account_id` | `TEXT` | zugehöriger Account |
| `webauthn_user_id` | `UUID` | stabiler WebAuthn-User-Handle |
| `credential` | `JSONB` | öffentlicher Credential-Datensatz und Counter |
| Zeitfelder | `TIMESTAMPTZ` | Erstellung, Änderung, letzte Nutzung |

Die Tabelle speichert keine privaten Passkey-Schlüssel. Ein Foreign Key zu
`domain_accounts` bleibt bis zum belegten Legacy-Backfill und zur abschließenden
Identitätsbereinigung bewusst ausgesetzt.

### `domain_conversations`

Ein aktives Knotengespräch referenziert seinen Knoten über `node_id`. Wird ein
Knoten mit Beiträgen aus dem Gewebe entfernt, setzt dieselbe Transaktion
`node_id` auf `NULL`, übernimmt letzte Knoten-ID und letzten Knotentitel nach
`node_id_snapshot` und `node_title_snapshot` und setzt `archived_at`.

Der öffentliche Zustand wird ohne zweite Wahrheitsquelle aus diesen Feldern
abgeleitet:

| Zustand | `node_id` | Snapshots | `archived_at` |
|---|---|---|---|
| `active` | gesetzt | `NULL` | `NULL` |
| `archived` | `NULL` | gesetzt | gesetzt |

Ein leerer automatisch erzeugter Gesprächsraum darf mit seinem Knoten
kaskadierend verschwinden. Eine nichtleere aktive oder archivierte Conversation
ist dagegen auf Datenbankebene gegen direktes Hard-Delete geschützt. Das
Archivieren erzeugt zusätzlich zum generischen Änderungsereignis das spezifische
Outboxereignis `domain.conversation.archived`.

### `domain_messages`

`domain_messages` speichert Klartextbeiträge, Autoren-Snapshots, Idempotenz und
Tombstones. Ein gewöhnliches SQL-`DELETE` ist nicht der fachliche Löschpfad und
wird abgewiesen. Rückzug und Moderation setzen `content = NULL` sowie
`deleted_at`; die Message-ID, Gesprächszuordnung und Zeitspur bleiben erhalten.

In einem Archiv gelten folgende Regeln:

- neue Beiträge und normale Inhaltsänderungen sind gesperrt;
- Autor oder Administrator dürfen einen vorhandenen Beitrag tombstonen;
- Accountlöschung darf ausschließlich `author_account_id` auf `NULL` setzen;
- direkte physische Löschung der Conversation oder ihrer Messages ist gesperrt.

Der Autorenname bleibt als öffentlicher Snapshot bestehen. Eine später
wiederverwendete Account-ID kann damit keine alten Beiträge übernehmen.

## JSONL-Modell

JSONL-Datensätze folgen den JSON-Schemas in `contracts/domain`. Im lokalen oder
ausdrücklich aktivierten Legacy-Modus lädt die API sie beim Start in begrenzte
In-Memory-Stores. Schreibpfade serialisieren check-and-write innerhalb eines
Prozesses und verwenden `fsync`; sie sind kein Ersatz für eine transaktionale
Mehrprozessdatenbank. In Produktion darf JSONL nur als dokumentierter Import-,
Export- oder Rollbackpfad eingesetzt werden, nie als stiller Ersatz für einen
fehlgeschlagenen PostgreSQL-Pfad.

## Gesprächs- und Rollenverträge

`conversation.schema.json` beschreibt aktive und archivierte Knotengespräche als
explizite Zustände. `message.schema.json` beschreibt Beiträge und Tombstones.
Das Entfernen eines Knotens ist vom Erhalt einer nichtleeren Conversation
getrennt; Details und weitere Objektklassen normiert
`docs/specs/objektlebenszyklen-und-loeschwirkungen.md`.

Conversation-/Message-Ereignisse landen atomar in `domain_outbox`, erhöhen aber
nicht die Kartenprojektion.

`role.schema.json` beschreibt weiterhin ein geplantes Objekt ohne produktive
`roles`-Tabelle. Private Gespräche, Anhänge und föderierte Zustellung sind nicht
Teil des heutigen Datenmodells.

## Cutover-Regeln

Ein vollständiger PostgreSQL-Cutover ist erst belegt, wenn:

1. Backfill und Orphan-Audit grün sind,
2. die betroffene Lesequelle PostgreSQL ist,
3. alle benötigten Mutationen PostgreSQL schreiben,
4. kein restart-unsichtbarer JSONL-Pfad verbleibt,
5. Rollback und Wiederherstellung definiert sind,
6. DB- und Browser-Ende-zu-Ende-Beweise grün sind,
7. die Produktionsruntime die Schalter frisch belegt.

## Logisches Zielmodell

Das Produktmodell besteht aus einer Garnrolle je Account, `map_state` für
Sichtbarkeit und Verortung, Knoten als Kollektivgüter oder Orte sowie Fäden als
Beziehungen. Die frühere RoN-Identität und ihre Modusspalte sind entfernt.
