---
id: specs.map-experience
title: Kartenerlebnis
summary: Kanonischer UX-Vertrag für Kartenwahrheit, Layer, Fokus, Dichte und souveräne Basiskarte.
doc_type: specification
status: canonical
canonicality: normative
lifecycle_state: active
role: norm
organ: product-map
owner: product-map
last_reviewed: 2026-08-02
review_after: 2026-10-11
depends_on:
  - specs.ui-interaction
  - specs.ui-state-machine
  - specs.garnrolle-knoten-faden
relations:
  - type: supersedes
    target: docs/blueprints/kartenklarheit.md
  - type: relates_to
    target: docs/blueprints/map-blaupause.md
  - type: relates_to
    target: docs/specs/ortsweberei-webgemeindezentrum.md
verifies_with:
  - apps/web/src/lib/map/scene.ts
  - apps/web/src/lib/map/types.ts
  - apps/web/src/routes/map/+page.ts
  - apps/web/src/routes/map/+page.svelte
  - apps/web/tests/map-load-fallback.spec.ts
  - apps/web/tests/map-url-state.spec.ts
  - apps/web/tests/webgemeindezentrum-hammer-park.spec.ts
  - apps/web/tests/edge-visibility.spec.ts
attention_source_status: none
attention_source_rationale: "Definiert Kartenwahrheit und Kartenerlebnis, keine persönliche Attention-Quelle."
---
# Kartenerlebnis

## Zweck

Die Karte zeigt nicht nur Orte, sondern den Zustand gemeinschaftlichen Handelns. Sie muss räumlich verständlich, semantisch ehrlich und auch bei Teilausfällen bedienbar bleiben.

## Kartenwahrheit

Die Route unterscheidet mindestens:

- Daten erfolgreich geladen;
- Daten teilweise geladen;
- Kernszene nicht ladbar.

Eine leere Karte darf nicht zugleich „keine Daten“ und „API kaputt“ bedeuten. Degradierte Zustände werden sichtbar benannt.

Zwischen Rohdaten und Darstellung liegt ein Szenenmodell. Es entscheidet, welche Entitäten, Fäden, Filterergebnisse und Diagnosen sichtbar sind.

## Basiskarte und Gewebe

Die Basiskarte ist ruhige räumliche Infrastruktur. Fachliche commonThing-Objekte bleiben davon getrennt.

Layer-Reihenfolge:

1. Basiskarte;
2. Fäden;
3. Knoten, Garnrollen und Webgemeindezentren;
4. Fokus und Hervorhebung;
5. Kompositionsvorschau.

Die Basiskarte enthält keine Domain-Knoten, Garnrollen oder semantischen Fäden.

## Entitäten

Kartenelemente verwenden typisierte Varianten statt eines beliebigen Punktcontainers:

- Knoten;
- Garnrolle;
- Webgemeindezentrum als dauerhafter Strukturknoten einer Ortsweberei;
- später weitere klar definierte Varianten.

Eine Garnrolle mit `not_on_map` erhält keinen öffentlichen Marker. `exact` und
`radius` bleiben im Datenmodell unterscheidbar; ihre visuelle Darstellung darf
nicht wertend sein. Das Webgemeindezentrum wird bewusst von der Ortsweberei
verortet. Die Karte darf weder einen geografischen Mittelpunkt noch eine
private Accountadresse als Zentrum erraten.

## Fäden und Dichte

Aktivitätsdichte wird durch die Fäden selbst sichtbar:

- Anzahl;
- Überlagerung;
- Transparenz;
- zeitliches Verblassen, sofern fachlich beschlossen und implementiert.

Eine zusätzliche Heatmap ist nicht Teil des Zielmodells, weil sie eine zweite abstrakte Semantik neben den tatsächlichen Beziehungen einführen würde.

## Fokus

Auswahl hebt das gewählte Objekt und relevante Beziehungen hervor. Die Karte darf zum Ziel fliegen, muss aber unnötige Doppelbewegungen vermeiden. Ein noch nicht aufgelöster Deep Link darf nicht zunächst durch eine Standardzentrierung überschrieben werden.

## Suche und Filter

Suche und Filter wirken auf die Szene. Treffer und sichtbare Fäden müssen dieselbe gefilterte Wirklichkeit verwenden. Ein Listentreffer fokussiert die entsprechende Entität auf der Karte.

## Basemap-Souveränität

Ziel ist eine selbst kontrollierte MapLibre-/PMTiles-Basiskarte mit nachvollziehbarer Asset- und Lizenzkette. Beweisstufen bleiben getrennt:

- statische Konfiguration;
- clientseitiger Protokollpfad;
- HTTP-Range-Auslieferung;
- gültiger PMTiles-Inhalt;
- Browserinitialisierung;
- echte Tile-Datenlieferung;
- visuelle Korrektheit;
- produktionsnaher Pfad.

Ein grüner Teilbeweis darf nicht als vollständiger Produktionsbeweis bezeichnet werden.

## Leistung

Die Kartenroute muss auf Mobilgeräten flüssig bleiben. Neue Overlay- oder Dichtefunktionen werden erst nach Messung eingeführt. Semantische Korrektheit und Eingabereaktion haben Vorrang vor dekorativer Animation.

## Noch offene Produktentscheidungen

Diese Spezifikation behauptet nicht, dass folgende Fragen bereits abgeschlossen sind:

- endgültiger produktiver Basemap-Modus;
- vollständige visuelle Baseline über alle Zoomstufen;
- tiefere PMTiles-Struktur- und Tile-Payload-Beweise;
- spätere Projektionen wie Netzwerk-, Themen- oder Zeitsicht;
- konkrete Lebensdauer und Verzwirnung aller Fadenarten.

Offene Fragen gehören in Roadmaps oder Tasks, nicht als scheinbar fertige Regel in diesen Vertrag.
