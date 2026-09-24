---
id: adr.ADR-0012__ereignisrueckgrat-transactional-outbox
title: ADR-0012 — Ereignisrückgrat mit Transactional Outbox
doc_type: reference
status: active
summary: >
  Entscheidet Transactional Outbox, versionierte Domain-Ereignisse und idempotente Konsumenten als Grundlage für Projektionen, Skalierung und Föderation.
relations:
  - type: relates_to
    target: architecture/commonthing-os.md
  - type: relates_to
    target: docs/reports/domain-postgres-instance-coherence-decision.md
  - type: relates_to
    target: docs/blueprints/weltgewebe-os-masterplan.md
---

# ADR-0012 — Ereignisrückgrat mit Transactional Outbox

Datum: 2026-07-15
Status: Accepted

## Kontext

commonThing benötigt künftig mehrere API-Instanzen, abgeleitete Such- und Kartenprojektionen, Benachrichtigungen, Agentenworkloads und föderierte Zustellung. Direkte Best-Effort-Nachrichten nach einer Datenbankänderung können verloren gehen. Prozesslokale Caches und Zwischenzustände verhindern derzeit eine sichere horizontale Skalierung.

## Entscheidung

PostgreSQL bleibt die lokale kanonische Fachwahrheit. Jede Mutation, die andere Komponenten oder Zellen zuverlässig erfahren müssen, schreibt innerhalb derselben Transaktion:

1. den neuen Fachzustand,
2. ein versioniertes Outbox-Ereignis.

Ein getrenntes Relay veröffentlicht bestätigte Outbox-Ereignisse an NATS JetStream. Konsumenten verarbeiten mindestens einmal zugestellte Ereignisse idempotent.

## Ereignisumschlag

Ein Domain-Ereignis enthält mindestens:

- eindeutige Ereignis-ID,
- Ereignistyp,
- Schema-Version,
- Ursprungszelle,
- Akteur oder Systemursprung,
- Objekt-ID,
- Objektversion oder erwartete Vorgängerversion,
- Transaktions- oder Korrelations-ID,
- Erzeugungszeit,
- Reichweite,
- Nutzdaten oder referenzierbare Änderung,
- Deduplikations- und Aufbewahrungsinformationen.

Öffentlich föderierte Ereignisse ergänzen Signatur, Schlüsselreferenz und Protokollversion.

## Zustellungssemantik

Die Plattform verspricht keine globale Exactly-once-Zustellung. Sie verwendet:

- mindestens einmalige Zustellung,
- stabile Ereignis-IDs,
- idempotente Konsumenten,
- atomare Konsumentenfortschritte, wo erforderlich,
- Dead-Letter- oder Quarantänepfade,
- beobachtbare Wiederholungen,
- begrenzte Aufbewahrung und Replay-Verträge.

## Operativer Readiness-Vertrag

Sobald PostgreSQL als Domänenlesequelle oder für mindestens einen
Domänenschreibpfad aktiviert ist, gehört die vollständige lokale Kette zur
Readiness: PostgreSQL-Transaktion und
`domain_outbox`, Relay, der erwartete Stream `WELTGEWEBE_DOMAIN`, der durable
Pull-Consumer `weltgewebe-api-domain-receipts-v1` und dessen persistenter
Verbrauchsbeleg in `domain_event_consumptions`. Streamname und Subjectbindung
sowie Durable-Name, Filter und explizite Acknowledgements werden semantisch
geprüft; eine bloße NATS-Verbindung genügt nicht.

Relay und Receipt-Consumer sind essentielle beaufsichtigte Worker. Ein
unerwarteter Exit setzt ihren Liveness-Wert zunächst auf fehlgeschlagen; der
Supervisor startet den Worker nach kurzer Pause erneut. Solange PostgreSQL Teil
der aktiven Domänenwahrheit ist, bleibt Readiness während eines tatsächlichen
Worker-Ausfalls fail-closed. Sind PostgreSQL und NATS verfügbar, startet der
Outbox-Drain auch im JSONL-Modus, damit ein Rollback keine bereits persistierte
PostgreSQL-Outbox strandet.

Readiness bewertet bewusst den **aktuellen** Lieferzustand und ist kein
unbegrenzter Historien-Audit. Als ungesund gilt ein nicht quarantänisiertes
Ereignis erst, wenn es *jetzt ausführbar* ist und seit mehr als 60 Sekunden
überfällig bleibt. Geplante Retry-Backoffs (`available_at` in der Zukunft)
werden daher nicht als festgefahrener Rückstand gezählt. Für durable Receipts
prüft eine indexgestützte Sonde alle fälligen veröffentlichten Ereignisse im
festen Zehn-Minuten-Fenster, die mindestens 60 Sekunden alt sind. Eine alte
historische Receipt-Lücke kann damit die gegenwärtige Produktion nicht dauerhaft
auf `503` festhalten; solche Lücken gehören in Audit/Recovery, nicht in
Readiness. Die Readiness-Abfragen verwenden dafür die bestehenden
`available_at`-Indizes plus eigene partielle Indizes für veröffentlichte und
quarantänisierte Outbox-Zeilen.

Quarantäne ist ein kontrollierter Betriebszustand, keine zweite Fachwahrheit.
Ein absichtlich quarantänisiertes Poison Event bleibt beobachtbar, führt allein
aber nicht zum globalen Readiness-Ausfall. Ein syntaktisch beschädigtes internes
JetStream-Ereignis wird nicht bestätigt und deshalb erneut zugestellt, statt
ohne durable Receipt still verworfen zu werden. Die Metriken veröffentlichen
nur begrenzte Health-Signale ohne fachliche IDs: Worker-Liveness, Erfolg der
DB-Health-Sonde, aktuell ausführbaren Rückstand, Quarantäne-Präsenz, Alter des
ältesten ausführbaren Ereignisses und den bounded Receipt-Probe.

## Konsumenten

Typische Konsumenten sind:

- Cacheinvalidierung,
- Aktivitätsprojektion,
- Suche,
- Kartenprojektion,
- Benachrichtigung,
- Chronik,
- Föderationsausgang,
- Nachbarschaftsradar,
- Agenten- und Batchaufträge.

Jeder Konsument dokumentiert:

- abonnierte Ereignistypen und Versionen,
- Deduplikationsschlüssel,
- transaktionale Wirkung,
- Retry- und Quarantäneregeln,
- Replay-Verhalten,
- Daten- und Sichtbarkeitsgrenzen.

## Cachevertrag

Caches sind abgeleitete Beschleuniger, keine Primärwahrheit. Ein Cache muss:

- vollständig rekonstruierbar sein,
- eine bekannte Frische- und Invalidierungssemantik besitzen,
- bei unklarem Zustand fail-closed oder direkt gegen die Primärquelle lesen,
- Restart, verspätete Zustellung und Replay vertragen.

## Reihenfolge der Einführung

1. Inventar aller prozesslokalen autoritativen Zustände,
2. Persistenz aller Auth-Zwischenzustände,
3. Outbox-Schema und Relay,
4. versionierter Ereignisvertrag,
5. erster idempotenter Konsument,
6. Multi-API-Kohärenztest,
7. schrittweise Projektionen,
8. spätere Föderationszustellung.

## Nicht-Ziele dieses Beschlusses

- keine sofortige Event-Sourcing-Umstellung aller Fachobjekte,
- keine globale Ereignisreihenfolge,
- keine Entfernung relationaler Primärmodelle,
- keine öffentliche Föderation vor Protokoll- und Sicherheitsbeweisen,
- keine Produktionsreplikation allein aufgrund vorhandener NATS-Infrastruktur; erforderlich bleiben Outbox-, Idempotenz- und Zwei-Instanz-Beweise.

## Alternativen

### Direkte Veröffentlichung nach Commit

Verworfen, weil ein Prozessabsturz zwischen Commit und Veröffentlichung ein dauerhaft fehlendes Ereignis erzeugen kann.

### Dual Write in Datenbank und Broker

Verworfen ohne verteilte Transaktion. Es erzeugt zwei nicht atomare Wahrheiten.

### Vollständiges Event Sourcing ab sofort

Verworfen. Der zusätzliche Modell- und Migrationsaufwand ist nicht durch aktuelle Anforderungen belegt.

## Konsequenzen

- Ereignisse werden zu versionierten Verträgen,
- Mutationen müssen ihre projektionsrelevanten Wirkungen benennen,
- NATS-Verfügbarkeit ist nicht Teil der fachlichen Datenbanktransaktion,
- Backpressure und Relay-Rückstand werden messbar,
- horizontale Skalierung und Föderation erhalten eine gemeinsame Grundlage,
- Datenschutz- und Reichweitenregeln müssen bis in Ereignisse und Konsumenten fortgeführt werden.

## Akzeptanzkriterien für den ersten Implementierungsschnitt

- Outboxzeile und Fachmutation sind atomar,
- Relay kann nach Absturz fortsetzen,
- doppelte Veröffentlichung erzeugt keine doppelte Fachwirkung,
- unveröffentlichte und fehlgeschlagene Ereignisse sind beobachtbar,
- mindestens zwei API-Instanzen sehen nach einer Mutation konsistent denselben Zustand,
- Der vollständige Kohärenzbeweis ersetzt den Single-Instance-Guard durch einen stärkeren Multi-Instance-Guard.
