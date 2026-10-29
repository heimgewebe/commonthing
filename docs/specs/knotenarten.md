---
id: specs.knotenarten
title: Knotenarten – fachlicher Katalog v2
doc_type: specification
status: canonical
last_reviewed: 2026-10-29
review_after: 2027-01-29
canonicality: normative
lifecycle_state: active
role: norm
organ: product-domain
owner: product
summary: Eindeutige Knotenarten ohne generischen Standardtyp; unabhängige Themen und Fähigkeiten.
depends_on:
  - specs.garnrolle-knoten-faden
relations:
  - type: relates_to
    target: docs/domain/vocabulary.md
  - type: relates_to
    target: docs/specs/ui-interaction.md
  - type: relates_to
    target: docs/specs/map-experience.md
verifies_with:
  - contracts/domain/node-kinds.v1.json
  - contracts/domain/node.schema.json
  - apps/web/src/lib/ui/productLanguage.ts
  - apps/web/src/lib/components/panels/KompositionPanel.svelte
  - apps/web/src/lib/components/panels/NodePanel.svelte
attention_source_status: none
attention_source_rationale: Knotenklassifikation selbst erzeugt keine persönliche offene Handlung; Reservierungen und Aufgaben erhalten eigene Fachquellen.
---

# Knotenarten (V2)

## Grundsatz

**Knoten** ist der universelle Oberbegriff für Gegenstände gemeinschaftlicher
Koordination, **keine Knotenart**. Jeder neu veröffentlichte Knoten muss exakt
eine fachliche Primärart besitzen. `standard`, `Unknown`, `unclassified`
und frei erfundene Kind-Werte sind keine gültigen Arten.

Der maschinenlesbare Katalog liegt unter
[`contracts/domain/node-kinds.v1.json`](../../contracts/domain/node-kinds.v1.json).
Englische IDs sind beständig; deutsche Bezeichnungen sind reine Darstellung.

| ID | Bezeichnung | Fachlicher Gegenstand |
| --- | --- | --- |
| `place` | Ort | Räumlicher Bezugspunkt wie ein Gemeinschaftsgarten |
| `resource` | Ressource | Nutzbares materielles oder immaterielles Gut, etwa ein Lastenrad |
| `project` | Projekt | Vorhaben mit einem angestrebten Ergebnis |
| `event` | Ereignis | Zeitlich bestimmtes Geschehen, etwa ein Nachbarschaftstreffen |
| `need` | Bedarf | Konkreter Wunsch nach Ressourcen oder Unterstützung |
| `offer` | Angebot | Konkrete angebotene Leistung oder Unterstützung |

### Fachliche Abgrenzung

Eine Gemeinschaftswerkstatt ist ein Ort; der Akkuschrauber darin eine Ressource.
Ein Zaunbau-Vorhaben ist ein Projekt; dafür fehlende Bretter sind ein Bedarf.
Die angebotene Mithilfe ist ein Angebot; der gemeinsame Bautag ein Ereignis.

Kollektivgut beschreibt einen möglichen gemeinschaftlichen Nutzungs- oder
Verantwortungscharakter. Es ist keine zusätzliche Art.
Garnrolle, Faden, Gespräch, Governance-Antrag, Ortsweberei und
Webgemeindezentrum bleiben eigene vorhandene Fachobjekte.

## Unabhängige Dimensionen

- **Art:** Was ist es? Exakt eine Primärart.
- **Themen:** Worum geht es? Mehrere kontrollierte Themen möglich.
- **Fähigkeiten:** Was kann man tun? Nur aus real bereitgestellten Fachfunktionen
  abgeleitet, nicht aus willkürlichen Typ-Flags.
- **Zustand:** Wie steht es darum? Aus dem jeweiligen Fachbereich, nicht aus
  der Knotenart abgeleitet.

Eine Ressource muss nicht reservierbar sein; ein Ort kann reservierbar sein.
Projekt-Aufgaben, Beteiligung und Buchung bleiben eigene dauerhafte Fachvorgänge.
Vergängliche Fadenprojektionen ersetzen niemals deren Persistenz.

## Ortsbezug

Knoten können ortsunabhängig sein. Keine künstliche Adresse und keine
Platzhalterkoordinaten. Ein nicht verorteter Knoten erscheint in Suche und
Listen, nicht als erfundener Marker. Geografische Kartenabfragen berücksichtigen
nur tatsächlich verortete Knoten.

Diese Fähigkeit ist in [PR #1969](https://github.com/heimgewebe/commonthing/pull/1969)
als noch nicht integrierter eigener Produktpfad bearbeitet. Sie darf nicht
parallel und abweichend implementiert werden.

## Eingabe und Validierung

Neue Knoten benötigen ID, Titel und gültige Primärart. Sinnvolle zusätzliche
Angaben hängen von der Art ab, ohne die Veröffentlichung an bereits später
vorgesehene Funktionalität zu koppeln. Vorübergehende Bearbeitungsentwürfe
müssen nicht veröffentlicht werden.

Server und Browser nutzen dieselben zulässigen Art-IDs. Unbekannte Eingaben
werden ausdrücklich abgewiesen, nicht zu einer generischen Art normalisiert.

## Bestandsbereinigung und Schnitt

Das alte `standard`/`Unknown`-Modell hat keinen fachlichen Bestandsschutz.
Es wird keine dauerhafte Kompatibilitätsruntime und keine siebte Auffangart
eingeführt. Vor dem produktiven Schnitt müssen die tatsächlich vorhandenen
historischen Arten und abhängigen Knoten-/Faden-/Gesprächsdaten erfasst werden.

Nur belegte, fachlich eindeutige Knoten dürfen explizit neu klassifiziert
werden; unklare alte Daten bleiben bis zur kontrollierten Bereinigung außerhalb
der neuen Veröffentlichung. Vor destruktiven Datenbankänderungen ist ein echter
Rückfallpunkt erforderlich. Der Quellcode-Schnitt ist nicht gleichbedeutend mit
einem bereits vollzogenen Produktions-Datencutover.

## UI und Qualität

Knotenauswahl und Editierdialog bieten dieselben sechs Arten. Keine
automatische Art-Voreinstellung, keine freie Kind-Texteingabe. Ein Knoten
mit fehlender Art wird nicht still als Ressource, Ort oder Ereignis ausgegeben.

Karte, Filter und Suche verwenden Art und Thema als getrennte Achsen.
Die vorhandene Authentifizierung, Knotengespräche und Autoritätsregeln bleiben
von dieser Klassifikation unabhängig.

Akzeptanz: Je eine Anlage, Bearbeitung, Suche und Anzeige für alle sechs
Arten; Ablehnung alter/ungültiger Arten bei neuen Schreibvorgängen; kein
unvermittelter Datenverlust, keine gefälschten geografischen Positionen,
keine künstlichen Knotenarten in der regulären Oberfläche.