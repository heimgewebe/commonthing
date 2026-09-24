---
id: specs.contract
title: Historischer Entwurf: Tombstone und Key-Erase
doc_type: reference
status: active
canonicality: supporting
lifecycle_state: superseded
summary: Historischer Zielentwurf für Event-Sourcing, Inhaltsverschlüsselung und Key-Erase; keine Beschreibung der heutigen commonThing-Runtime.
relations:
  - type: relates_to
    target: docs/domain/vocabulary.md
  - type: relates_to
    target: docs/datenmodell.md
  - type: relates_to
    target: docs/specs/objektlebenszyklen-und-loeschwirkungen.md
---

# Historischer Entwurf – Tombstone und Key-Erase

> **Nicht kanonisch:** Dieses Dokument beschreibt einen früheren Zielentwurf.
> Event-Sourcing, objektbezogene Inhaltsverschlüsselung, Transparency-Log und
> Key-Erase sind nicht als heutige produktive commonThing-Wahrheit belegt.
> Maßgeblich für aktuelle Löschwirkungen ist
> `docs/specs/objektlebenszyklen-und-loeschwirkungen.md`; das reale physische
> Modell steht in `docs/datenmodell.md`.

## Ursprünglicher Scope

Der Entwurf bezog sich auf Beiträge, Kommentare und Artefakte und verband eine
unveränderliche Ereignisspur mit verschlüsselten Nutzinhalten. Diese Architektur
ist als mögliche spätere Ausbaurichtung dokumentiert, aber nicht implementiert.

## 1. Entworfenes Modell

- **Event-Sourcing:** Jede Änderung wäre ein Event; die Ereignisspur bliebe
  unveränderlich.
- **Inhalt:** Nutzinhalte würden mit einem objektbezogenen Daten-Key
  verschlüsselt.
- **Identität:** Nutzer würden Events signieren; der Server würde Batches über
  ein Transparency-Log verankern.

Keine dieser drei Aussagen darf ohne einen frischen Implementierungs- und
Runtimebeleg als heutiger Zustand ausgegeben werden.

## 2. Entworfene Löschwirkung

Der frühere Entwurf kombinierte:

1. ein logisches `DeleteEvent` beziehungsweise einen Tombstone;
2. das Verwerfen des zugehörigen Daten-Keys;
3. eine minimale verbleibende Ereignisspur.

In diesem Zielbild wäre der Inhalt selbst für Administratoren nicht mehr
rekonstruierbar. Das heutige System besitzt jedoch keinen belegten allgemeinen
Key-Erase-Pfad. Aktuell gelten deshalb die expliziten Wirkungen Stilllegen,
Archivieren, Tombstonen, Anonymisieren, Redigieren, Purgen und Projektion
verwerfen aus der kanonischen Lebenszyklus-Spezifikation.

## 3. Frühere Rechts- und Moderationsidee

Der Entwurf sah für rechtswidrige Inhalte einen sofortigen Takedown und einen
intern versiegelten Forensiknachweis vor. Auch dieser Pfad ist nicht als heutige
Funktion implementiert. Ein späterer Rechts- oder Purgepfad benötigt einen
eigenen engen Vertrag, Autorisierung, Wirkungsplan und auditfähigen Receipt.

## 4. Frühere API-Idee

Vorgesehen war eine Tombstone-Antwort ohne Content-Payload sowie ein idempotentes
`DELETE`, das Tombstone und Key-Erase gemeinsam auslöst. Die heutigen Endpunkte
sind daran nicht stillschweigend zu messen. Ihre tatsächliche Semantik wird in
den aktiven Domain-Spezifikationen, JSON-Schemas und Implementierungstests
festgelegt.

## 5. Migrationsidee

Bis zu einer möglichen produktiven Inhaltsverschlüsselung wurde Soft-Delete plus
Scrub als Zwischenweg erwogen. Auch daraus folgt keine heutige Verpflichtung,
Backups oder Replikate ohne einen implementierten und geprüften Purgevertrag zu
verändern.

## 6. Transparenzidee

Wöchentliche Transparency-Anker und öffentliche Löschstatistiken waren Teil des
Zielbilds. Sie sind nicht als aktive Runtime belegt.

## 7. Weiterhin verwendbare Grundintuition

Die weiterhin tragfähige Aussage des Entwurfs lautet:

> Nutzinhalt, historische Minimalspur und aktive Projektion besitzen verschiedene
> Lebenszyklen.

Die konkrete heutige Auslegung dieser Trennung steht ausschließlich im
kanonischen Objektlebenszyklusvertrag.
