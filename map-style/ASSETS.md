# Asset Manifest: Glyphs & Sprites

Dieses Dokument dokumentiert die Herkunft, Lizenzierung und Integration der visuellen Assets (Schriften und Icons), die
von der souveränen Basemap (`style.json`) genutzt werden.

> **Status-Hinweis:** Dieses Manifest dokumentiert die Herkunft und Lizenz der lokal gehosteten Schriftarten (Glyphs). Da die Basemap visuell beruhigt ist und nur Infrastruktur darstellt, sind aktuell keine Sprites (Icons) erforderlich.

## Strategie

Um echte **Style-Souveränität** (Phase 2) zu erreichen, dürfen wir keine externen Abhängigkeiten (wie CartoCDN oder
Mapbox-Server) für Schriften (Glyphs) oder Icons (Sprites) verwenden. Diese externen Aufrufe gefährden
Offline-Fähigkeit, Caching und führen zu stillen Tracking-Risiken durch Dritte.

Daher ist die Strategie für alle visuell benötigten Assets:

1. **Lokales Hosting**: Alle benötigten Assets sollen physisch in diesem Verzeichnis (`map-style/`) abgelegt und lokal
   ausgeliefert werden.
2. **Pfad-Integration**: In der `style.json` verweisen `glyphs` auf den stabilen Runtime-Pfad (`/local-basemap/glyphs/{fontstack}/{range}.pbf`). Die `sprite` Eigenschaft entfällt.
3. **Klare Lizenzen**: Jedes Asset muss mit einer kompatiblen, dokumentierten Open-Source-Lizenz versehen sein.

## Glyphs (Schriften)

Verzeichnis: `map-style/glyphs/`

MapLibre benötigt Schriften im Protocol Buffer Format (PBF), gesplittet in Unicode-Ranges.

- **Ausgewählte Schriftart:** Noto Sans Regular (wie in `style.json` referenziert)
- **Lizenz:** SIL Open Font License (OFL)
- **Herkunft / Erzeugung:** Die Schriftarten (v2.0) werden über das deterministische Skript `scripts/basemap/fetch-glyphs.sh` reproduzierbar vom offiziellen [OpenMapTiles Fonts Release](https://github.com/openmaptiles/fonts/releases) geladen und über einen SHA256-Hash validiert. Da es sich um kompilierte Artefakte handelt, sind die `.pbf`-Dateien nicht im Repo eingecheckt. Das Deployment-Skript `weltgewebe-up` versucht als "Best Effort Guard", fehlende Fonts beim Stack-Start aus der definierten Quelle zu beziehen. Für die lokale Entwicklung (ohne `weltgewebe-up`) muss das Skript einmalig manuell ausgeführt werden (siehe `map-style/glyphs/README.md`).

## Sprites (entfallen)

*Entfällt (bewusst).* Sprites sind nicht vergessen, sondern im aktuellen MVP-Architekturziel bewusst nicht Bestandteil der Basemap. Da die commonThing-Basemap gemäß Architektur-Blaupause eine streng visuell beruhigte Infrastrukturebene ohne POIs oder komplexe Füllmuster darstellt, werden keine Sprites in der `style.json` referenziert oder benötigt. commonThing-Overlay bleibt der exklusive Ort für ikonische und semantische Darstellungen (Nodes, Edges, Activity).
