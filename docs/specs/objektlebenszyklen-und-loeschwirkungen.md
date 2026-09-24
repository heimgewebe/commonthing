---
id: specs.objektlebenszyklen-und-loeschwirkungen
title: Objektlebenszyklen und Löschwirkungen
summary: Kanonischer Vertrag für Stilllegung, Archivierung, Tombstone, Anonymisierung, Redaktion, Purge und das Verwerfen abgeleiteter Projektionen.
doc_type: specification
status: canonical
canonicality: normative
lifecycle_state: active
role: norm
organ: product-domain
owner: product-domain
last_reviewed: 2026-07-27
review_after: 2026-10-19
depends_on: []
relations:
  - type: relates_to
    target: docs/specs/garnrolle-knoten-faden.md
  - type: relates_to
    target: docs/specs/governance-antraege.md
  - type: relates_to
    target: docs/datenmodell.md
  - type: relates_to
    target: docs/domain/vocabulary.md
verifies_with:
  - contracts/domain/conversation.schema.json
  - contracts/domain/message.schema.json
  - apps/api/tests/db_node_conversations.rs
attention_source_status: none
attention_source_rationale: "Definiert Lebenszykluswirkungen, aber derzeit keinen persönlichen ausstehenden Handlungs-, Neuheits-, Beteiligungs- oder Wartezustand."
---

# Objektlebenszyklen und Löschwirkungen

## Zweck

In commonThing darf das Wort **löschen** nicht mehrere technisch und fachlich
verschiedene Wirkungen verdecken. Ein Kartenobjekt kann aus der aktiven Welt
verschwinden, während die durch mehrere Personen entstandene Geschichte erhalten
bleiben muss. Umgekehrt kann ein einzelner Inhalt zurückgezogen werden, ohne die
Existenz des Vorgangs aus der Chronik zu tilgen.

Dieser Vertrag trennt deshalb sieben Wirkungen. Jeder destruktive Endpunkt, jede
Datenbankkaskade und jede Benutzeroberfläche muss genau eine dieser Wirkungen
benennen.

## Kanonische Wirkungen

### Stilllegen oder zurückziehen

Das Objekt verlässt aktive Karte, Listen, Suche und gewöhnliche Schreibpfade.
Seine dauerhafte Geschichte kann bestehen bleiben. Stilllegung ist keine
physische Datenvernichtung.

### Archivieren

Ein abgeschlossener oder elternlos gewordener Zusammenhang bleibt öffentlich
lesbar und erhält einen stabilen historischen Kontext. Neue gewöhnliche Inhalte
und normale Inhaltsbearbeitungen sind ausgeschlossen.

### Tombstonen

Der Nutzinhalt eines Beitrags wird entfernt. Identität des Beitrags, Zeitpunkt,
Gesprächszuordnung und eine minimale öffentliche Markierung bleiben erhalten.
Ein Tombstone ist der gewöhnliche Rückzugs- und Moderationspfad für Beiträge.

### Anonymisieren

Eine aktive Personenbindung wird gelöst, ohne sachliche oder öffentliche
historische Snapshots umzuschreiben. Eine später wiederverwendete Account-ID darf
keine alte Bearbeitungsbefugnis erben.

### Redigieren

Ein Inhalt wird aus Moderations-, Sicherheits- oder Rechtsgründen aus der
öffentlichen Projektion entfernt. Die öffentliche Darstellung entspricht
mindestens einem Tombstone. Weitergehende interne Evidenz benötigt einen eigenen,
zugriffsbeschränkten Vertrag und ist nicht durch eine gewöhnliche Adminaktion
begründet.

### Purgen

Datensatz und Nutzinhalt werden physisch vernichtet. Purge ist kein gewöhnlicher
CRUD-Pfad. Er verlangt einen eng begrenzten, protokollierten Wirkungsplan für
Primärdaten, Replikate, Suchindizes, Caches, Exporte und Backups. Ein fehlender
Purgevertrag führt fail-closed zur Ablehnung.

### Abgeleitete Projektion verwerfen

Ein berechenbarer Cache-, Such-, Karten- oder Fadendatensatz wird entfernt und
kann aus seiner belegten fachlichen Quelle neu entstehen. Das Verwerfen einer
Projektion darf die Quelle nicht löschen.

## Grundregeln

1. **Aktive Welt und Geschichte sind getrennte Ebenen.** Das Entfernen eines
   Kartenobjekts darf fremde Beiträge, Entscheidungen oder Webungsaktionen nicht
   kaskadierend vernichten.
2. **Öffentliche Beiträge werden tombstoniert, nicht physisch gelöscht.** Das
   gilt für Autorenrückzug und administrative Moderation.
3. **Leere automatisch erzeugte Hüllen dürfen verschwinden.** Eine Conversation
   ohne Beiträge besitzt keine eigenständige öffentliche Geschichte und darf mit
   ihrem Elternobjekt hart gelöscht werden.
4. **Nichtleere Eltern dürfen nicht hart gelöscht werden.** Nichtleere aktive und
   archivierte Conversations sind gegen direkte Datenbanklöschung geschützt.
5. **Accountaustritt ändert keine fremde Wahrheit.** Aktive Accountbindungen
   werden gelöst; zulässige Autoren- und Verfahrenssnapshots bleiben erhalten.
6. **Purge ist eine eigene Hochrisikooperation.** Ein gewöhnliches `DELETE`
   darf niemals stillschweigend als rechtlicher, moderativer oder administrativer
   Purge dienen.
7. **Die Wirkung wird zurückgegeben.** Eine destruktive API-Antwort nennt
   mindestens Objektzustand, entfernte Projektionen, erhaltene Archive und deren
   stabile Kennungen.
8. **Der Begriff in der Oberfläche entspricht der Wirkung.** „Aus dem Gewebe
   entfernen“, „Beitrag zurückziehen“ und „Archiv öffnen“ sind präziser als ein
   unqualifiziertes „Löschen“.

## Objektmatrix

| Objektklasse | Gewöhnliche Nutzeraktion | Erhalten | Entfernt | Physischer Purge |
|---|---|---|---|---|
| Knoten ohne öffentliche Geschichte | aus dem Gewebe entfernen | fachliche Outbox-/Auditspur nach jeweiligem Vertrag | Knoten und abgeleitete Fäden; leere Conversation | nur über Wartungs- oder Rechtsvertrag |
| Knoten mit Gesprächsbeiträgen | aus dem Gewebe entfernen | Conversation, Beiträge, Knoten-ID- und Titelsnapshot | aktiver Knoten und verbundene Fadenprojektionen | nicht über Node-DELETE |
| Abgeleiteter Faden | auflösen oder Projektion verwerfen | zugrunde liegende Webungsaktion | aktive Fadenprojektion | nur falls Quelle ebenfalls rechtmäßig purgiert wird |
| Aktive Conversation ohne Beiträge | mit Elternobjekt entfernen | keine eigenständige Gesprächsgeschichte | Conversation | zulässig als leere Hülle |
| Aktive Conversation mit Beiträgen | Elternobjekt entfernen | Conversation und Beiträge | aktive Elternbindung | verboten |
| Archivierte Conversation | lesen | Archivkontext und Beiträge | keine gewöhnliche Löschung | nur gesonderter Purgevertrag |
| Beitrag | zurückziehen oder moderieren | ID, Zuordnung, Zeiten, Tombstone | Nutzinhalt | nur gesonderter Purgevertrag |
| Account/Garnrolle | austreten | zulässige fremde Beiträge und Snapshots | Loginmittel, Sitzungen, Garnrolle, aktive Autorenbindungen | nach Accountvertrag |
| Governance-Antrag ohne Verfahrensgeschichte | zurückziehen, sofern Fachvertrag es erlaubt | notwendige Auditspur | leere Zielhüllen und abhängige Projektionen | nach Governance-Vertrag |
| Governance-Antrag mit Beiträgen, Veto oder Stimme | abschließen oder archivieren | Antrag, Verfahren, Beiträge und Entscheidungen | aktive Bearbeitbarkeit | gewöhnliches Hard-Delete verboten |
| Cache, Suchindex, Preview, temporäre Datei | verwerfen | kanonische Quelle | Projektion | unmittelbar zulässig, sofern reproduzierbar |

## Knotengespräch

Ein PostgreSQL-Knoten besitzt während seines aktiven Lebenszyklus genau eine
öffentliche Conversation.

### Aktiver Zustand

```text
lifecycle_state = active
node_id = aktuelle Knoten-ID
node_id_snapshot = null
node_title_snapshot = null
archived_at = null
```

Neue Beiträge und normale Bearbeitungen folgen Rollen-, Autoren-, Rate-Limit- und
Versionsregeln. Ein Beitrag wird über den bestehenden DELETE-Endpunkt
**tombstoniert**, nicht physisch gelöscht.

### Archivierter Zustand

```text
lifecycle_state = archived
node_id = null
node_id_snapshot = letzte Knoten-ID
node_title_snapshot = letzter Knotentitel
archived_at = Archivierungszeitpunkt
```

Das Archiv ist über seine stabile Conversation-ID lesbar. Neue Beiträge und
normale Inhaltsänderungen sind gesperrt. Autoren dürfen eigene Beiträge weiterhin
zurückziehen; Administratoren dürfen Beiträge weiterhin moderativ tombstonieren.
Beim Accountaustritt darf ausschließlich `author_account_id` gelöst werden.

Die Conversation selbst und ihre Messagezeilen dürfen nicht durch direkte
Datenbanklöschung verschwinden. Die Archivierung erzeugt ein eigenes
`domain.conversation.archived`-Outboxereignis.

## Strukturierte Löschwirkung

Der erfolgreiche Entfernungspfad eines Knotens soll eine Wirkung dieser Form
zurückgeben:

```json
{
  "node_id": "…",
  "node_state": "removed",
  "removed_edge_ids": ["…"],
  "conversation": {
    "effect": "deleted_empty"
  }
}
```

Für einen JSONL-Betrieb ohne Conversation-Subsystem lautet die Wirkung:

```json
{
  "node_id": "…",
  "node_state": "removed",
  "removed_edge_ids": ["…"],
  "conversation": {
    "effect": "not_applicable"
  }
}
```

`not_applicable` bedeutet ausschließlich, dass dieser Persistenzpfad keine
Conversation verwaltet. Es darf nicht als Löschung einer leeren Conversation
interpretiert werden.

oder bei vorhandenen Beiträgen:

```json
{
  "node_id": "…",
  "node_state": "removed",
  "removed_edge_ids": ["…"],
  "conversation": {
    "effect": "archived",
    "archive_id": "…",
    "archive_url": "/api/conversations/…"
  }
}
```

Ein Client darf den Erfolg nicht allein aus einem leeren HTTP-Status ableiten,
wenn Nebenwirkungen für den Nutzer relevant sind. Bei `archived` bleibt nach
der Kartenaktualisierung eine sichtbare Quittung mit direktem Link
`Archiv öffnen` erhalten. Daraus folgt keine allgemeine Archivübersicht.

## Governance-Konvergenz

Governance-Conversations verwenden dieselbe Conversation-Tabelle. Solange ein
vollständiger Governance-Archivpfad noch nicht implementiert ist, bleibt die
Löschung eines Antrags mit Gesprächsbeiträgen fail-closed blockiert. Eine spätere
Migration darf keine neuen objektspezifischen Snapshotspalten nach demselben
Muster anhäufen. Sie soll einen typisierten historischen Subjektkontext verwenden,
der die hier definierten Zustände beibehält.

## Entscheidung für gewöhnliche Knotenmutationen

Der kollektive Node-Vertrag ist in
[`ADR-0014`](../adr/ADR-0014__accountable-collective-node-mutations.md)
konkretisiert:

- Ein gewöhnliches Node-DELETE entfernt den Knoten permanent aus der aktiven
  Welt; es ist weder Soft-Delete noch öffentliches Undo.
- Der Versuch bleibt durch PostgreSQL-Transaktion beziehungsweise JSONL-
  Write-ahead-Journal bis zum erfolgreichen Abschluss rückrollbar.
- Nach erfolgreichem Abschluss bleiben die nach dieser Spezifikation zulässigen
  Conversations, Beiträge, Outbox- und privaten Auditbelege erhalten.
- Ein öffentlicher Purge- oder Restore-Endpunkt existiert nicht. Purge bleibt bis
  zum vollständigen Hochrisikovertrag fail-closed.

## Purgeanforderungen

Ein späterer Purgepfad muss vor seiner Aktivierung belegen:

1. konkrete Rechts- oder Betriebsgrundlage;
2. autorisierte Rolle und enges Objektziel;
3. Primärdatensätze und alle ableitbaren Kopien;
4. Wirkung auf Föderation, Outbox, Suchindex, Cache, Export und Backup;
5. idempotenten Wiederholungs- und Abbruchvertrag;
6. manipulationsgeschützten Receipt ohne gelöschten Nutzinhalt;
7. Tests für Teilfehler und Wiederanlauf.

Fehlt einer dieser Punkte, bleibt Purge nicht implementiert und gewöhnliche
Tombstone-, Anonymisierungs- oder Archivpfade gelten.

## Nicht-Ziele

- keine Behauptung, dass verschlüsselte Key-Erase-Speicherung heute produktiv ist;
- keine allgemeine Mindest- oder Höchstaufbewahrungsfrist ohne eigenen Beschluss;
- kein freier Admin-Hard-Delete;
- keine automatische Übertragung dieses Vertrags auf private oder föderierte
  Inhalte ohne deren eigene Datenschutz- und Zustellverträge.
