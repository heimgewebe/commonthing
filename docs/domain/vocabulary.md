---
id: domain.vocabulary
title: Domänenvokabular
doc_type: reference
status: active
summary: Aktuelle Zuordnung der Produkt- und Lebenszyklusbegriffe zu API und Datenmodell.
relations:
  - type: relates_to
    target: docs/domain/modules.md
  - type: relates_to
    target: docs/datenmodell.md
  - type: relates_to
    target: docs/specs/objektlebenszyklen-und-loeschwirkungen.md
  - type: relates_to
    target: docs/specs/contract.md
  - type: relates_to
    target: docs/reports/garnrolle-identity-cutover-proof.md
  - type: relates_to
    target: docs/specs/ortsweberei-webgemeindezentrum.md
  - type: relates_to
    target: docs/specs/knotenarten.md
---

# Domänenvokabular

## Objekte

| Produktbegriff | Technik/API | Bedeutung | Aktueller Status |
|---|---|---|---|
| Garnrolle | Account, `/accounts` | genau ein persönlicher Ausgangspunkt je Account | produktiver PostgreSQL-Pfad belegt |
| Kartenstatus | `map_state` | `not_on_map`, `exact` oder `radius`; Eigenschaft der Garnrolle, kein Kontotyp | produktiv |
| Knoten | Node, `/nodes` | Fachlicher Oberbegriff für Ort, Ressource, Projekt, Ereignis, Bedarf oder Angebot | API- und Browser-Schreibpfad produktiv belegt |
| Knotenart | `Node.kind` | Eine der sechs fachlichen Arten laut [Knotenartenvertrag](../specs/knotenarten.md), keine generische Standardart | V2 im Quellcode-Schnitt, Produktivaktivierung gesondert |
| Faden | Edge, `/edges` | serverseitig abgeleitete Beziehung zwischen Garnrollen und/oder Knoten | öffentliche Leseprojektion; Erzeugung durch fachliche Webungsaktionen |
| Gesprächsraum | Conversation, `/conversations` | öffentlicher Diskussionsraum; Knotengespräche können aktiv oder archiviert sein | Knotengespräch produktiv |
| Gesprächsarchiv | Conversation mit `lifecycle_state=archived` | vom früheren Elternobjekt gelöste, stabile und öffentlich lesbare Gesprächsgeschichte | für Knotengespräche implementiert |
| Beitrag | Message, `/conversations/{id}/messages` | Klartextbeitrag mit Autoren-Snapshot und möglichem Tombstone | Knotengespräch produktiv |
| Berechtigungsrolle | `role` (`gast`, `weber`, `admin`) | Gast webt und spricht mit; Weber pflegt zusätzlich fremde gemeinschaftliche Inhalte und besitzt formale Veto-/Stimmrechte; Admin moderiert | implementiert |
| Ortsweberei | `/ortswebereien` | lokale Gemeinschaft mit Mitgliedschaft, Regeln, Vorhaben und gemeinsamen Mitteln | erste Runtime-Instanz `Ortsweberei Hamm`; Governance-Schreibpfad noch offen |
| Gewebezelle | `gewebezellen.id` / Cell-ID | technische und föderative Heimat genau einer Ortsweberei; ein Betreiber darf mehrere Zellen hosten | erste Produktbindung `hamm.weltgewebe.net` in PostgreSQL |
| Webgemeindezentrum | `/webgemeindezentren` | genau ein aktiver Karten-, Treff- und Governance-Anker je aktiver Ortsweberei | erste Runtime-Instanz im Hammer Park, Zustand `desired`; Bestätigung noch offen |

## Lebenszyklusverben

| Begriff | Bedeutung |
|---|---|
| Aus dem Gewebe entfernen | Objekt aus aktiver Karte, Listen und Schreibpfaden nehmen; keine Aussage über historische Datensätze |
| Archivieren | Geschichte lesbar und stabil erhalten, aber gewöhnliche Fortsetzung sperren |
| Tombstonen | Nutzinhalt entfernen, minimale Ereignis- und Zuordnungsspur erhalten |
| Anonymisieren | aktive Personenbindung lösen, zulässige historische Snapshots erhalten |
| Redigieren | Inhalt aus Moderations-, Sicherheits- oder Rechtsgründen aus der öffentlichen Projektion nehmen |
| Purgen | Primärdatensatz und alle geregelten Kopien physisch vernichten; eigener Hochrisikopfad |
| Projektion verwerfen | berechenbare Ableitung entfernen, ohne ihre fachliche Quelle zu löschen |

Ein unqualifiziertes **„löschen“** ist in neuen Produktspezifikationen, API-
Verträgen und Benutzertexten zu vermeiden, sobald mehr als eine dieser Wirkungen
möglich ist.

## Entfernte Begriffe

Die frühere Identität „Rolle ohne Namen“ und ihre technischen Felder wurden nach
belegtem Produktionscutover entfernt. Eine nicht öffentlich verortete Person ist
weiterhin eine Garnrolle mit `map_state=not_on_map`.

`role` bedeutet nur Berechtigungsrolle. Historische Dokumente, die `role` als
Identitätsobjekt verwenden, sind nicht die aktuelle Fachsprache.

**`thread` ist als technischer Sammelbegriff verboten; eine Beziehung ist ein
`edge`, ein Diskussionsraum eine `conversation`.**
