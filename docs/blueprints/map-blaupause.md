---
id: map-blaupause
title: Basemap-Architektur-Blaupause
doc_type: blueprint
status: active
summary: >
  Architektur-Blaupause für einen souveränen Basemap-Stack basierend auf
  MapLibre, PMTiles und einer reproduzierbaren Tile-Generierungs-Pipeline
  für commonThing-Overlays.
relations:
  - type: relates_to
    target: docs/specs/map-experience.md
  - type: relates_to
    target: docs/roadmap.md
---

# Basemap-Architektur-Blaupause

> Status: Aktives Architektur-Leitdokument für die Karten-/Basemap-Schicht.
> Dauerhafte UX-Regeln stehen in [`docs/specs/map-experience.md`](../specs/map-experience.md); offene Umsetzungsschritte werden in der Master-Roadmap geführt.

## Kontext

Wenn die Karte der Kern des Systems ist, sollte sie vollständig souverän betrieben werden:
eigene Daten → eigenes Tile-Artefakt → eigener Stil → eigener Hosting-Pfad.
Das führt zu einer Architektur MapLibre + PMTiles + eigener Pipeline.
Die Karte wird als Kerninfrastruktur und nicht als UI-Service betrachtet.

## Ist-Zustand

Die Implementierung nutzt im lokalen Entwicklungsbetrieb nun standardmäßig den lokalen
souveränen Basemap-Modus (`local-sovereign`).

Das Dev-Setup ist über Vite angebunden und für die lokale Style-Auslieferung im
Entwicklungsfluss verifiziert. Produktionshosting und die produktive
Standardschaltung des finalen PMTiles-Artefakts bleiben ausstehend.

## Abwägungen

Zu frühe Souveränität kann operative Komplexität erzeugen:
Tile-Builds, OSM-Updates, Style-Assets, CDN-Konfiguration.
Viele Projekte verlieren hier Geschwindigkeit, was bei selbst gehosteten Basemaps bedacht werden muss.

## Entwurfsprinzipien

Die ideale Blaupause ist souverän, aber modular:

- Basemap ist ein Artefakt, kein Service
- Hosting ist serverlos möglich (PMTiles)
- Pipeline ist reproduzierbar
- MapLibre bleibt reine Rendering-Engine
- Overlays bleiben komplett entkoppelt

Damit erhält man Souveränität ohne Architekturbruch.

## Architekturziele

- Volle Basemap-Souveränität
- Anbieterunabhängigkeit
- Reproduzierbare Builds
- Artefaktbasiertes Deployment

## Artefaktfluss

- OpenStreetMap-Daten
- → Tile-Generierung
- → MBTiles
- → PMTiles-Artefakt
- → Hosting
- → MapLibre Rendering
- → commonThing-Overlay-Layer

Ziel: Die Basemap wird als Artefakt erzeugt und verteilt, nicht als externer Kartenservice konsumiert.

---

## 1. Systemarchitektur

```text
               OSM Daten
                   │
                   ▼
            Tile Generation
         (planetiler / tilemaker)
                   │
                   ▼
               PMTiles
          basemap-vX.pmtiles
                   │
                   ▼
                Storage
       (S3 / R2 / Heimserver / CDN)
                   │
                   ▼
                MapLibre
                   │
        ┌──────────┴───────────┐
        ▼                      ▼
      Edges                  Nodes
      Overlay                Overlay
```

### Prinzip

Basemap = Infrastruktur
Overlay = commonThing

---

## 2. Komponenten

### 2.1 Rendering Engine

MapLibre GL

Vorteile:

- Open Source
- Mapbox-Style kompatibel
- PMTiles kompatibel
- keine Vendorbindung

---

### 2.2 Tile-Format

PMTiles

Eigenschaften:


- Single-File Tileset
- Range Requests
- CDN-freundlich
- offlinefähig

Beispiel:

`basemap-europe-v1.pmtiles`

### Tileset-Strategie

Mögliche Startpunkte:

- `hamburg.pmtiles` – schnelle lokale Iteration, geringste Speicherkosten
- `germany.pmtiles` – guter Mittelweg aus Abdeckung und Build-Dauer
- `europe.pmtiles` – maximale Reichweite, aber höchste Build- und Speicherkosten

Damit wird klar unterschieden zwischen Large Scale und Local Scale.

---

### 2.3 Tile-Generierung

Empfohlenes Tool:

planetiler

Vorteile:

- extrem schnell
- gute OSM-Pipeline
- aktiv entwickelt

Alternativen:

- tilemaker
- openmaptiles

---

### 2.4 Datenquelle

OpenStreetMap

Regionen:

- planet
- europe
- germany
- hamburg

---

### 2.5 Hosting

Optionen:

Cloud

- Cloudflare R2
- S3
- Backblaze

Heimserver

`tiles.weltgewebe.org`

CDN

optional:

Cloudflare CDN

---

## 3. Repositories (empfohlene Struktur)

Eine logische und physische Trennung der Repositories ist sinnvoll:

- weltgewebe-basemap
- weltgewebe-map-style
- weltgewebe-web

---

### 3.1 basemap repo

```text
weltgewebe-basemap
 ├─ data/
 ├─ build/
 ├─ scripts/
 │   ├─ fetch-osm.sh
 │   ├─ build-tiles.sh
 │   └─ build-pmtiles.sh
 ├─ basemap.pmtiles
 └─ basemap.meta.json
```

---

### 3.2 map-style repo

MapLibre-Style-Dokumente sind Teil des `map-style` repositories.

Style-Ownership ist wichtig, weil:

- Glyphs
- Layer order
- Color palette (hell und dunkel als eigene Style-Dokumente)

sonst wieder fremd kontrolliert werden.

Darkmode der Basemap ist ein eigener MapLibre-Stil (`style-dark.json` /
`style-germany-dark.json`) mit gleicher Quellen-/Layerstruktur, nicht ein
CSS-Filter. Das Web wählt den Stil über das globale
`document.documentElement.dataset.colorScheme` (`light`|`dark`); semantische
Knoten- und Fadenfarben bleiben Overlay-Verantwortung.

```text
weltgewebe-map-style
 ├─ style.json
 ├─ style-dark.json
 ├─ style-germany.json
 ├─ style-germany-dark.json
 ├─ glyphs/
 └─ colors.json
```

---

### 3.3 Asset-Compliance & Lizenzierung

Souveränität umfasst nicht nur das Hosting, sondern auch eine lückenlos nachvollziehbare Rechtekette für alle Assets.

- **Karten-Attribution:** Die OSM-/ODbL-Pflichten müssen im Client (MapLibre UI) jederzeit korrekt und sichtbar erfüllt sein.
- **Style-Assets:** Herkunft und Lizenz von Glyphs (Fonts) müssen im `map-style` Repository dokumentiert sein. (Sprites sind im aktuellen Architekturziel bewusst nicht Bestandteil der Basemap, da die Semantik im Overlay liegt).
- **Keine stillen Abhängigkeiten:** Das finale Kartenprodukt darf nicht stillschweigend von fremden Diensten oder unklaren Fremdlizenzen abhängen.

---

### 3.4 weltgewebe-web

Hier lebt:

- MapLibre
- Overlay
- Nodes
- Edges

---

## 4. Tile-Pipeline

### Schritt 1

OSM Download

```bash
wget https://download.geofabrik.de/europe-latest.osm.pbf
```

> **Hinweis:** Dies ist ein **nicht-deterministischer Quickstart**. Für produktive Builds werden zwingend ein gepinnter Snapshot und eine SHA256-Verifikation benötigt (siehe z.B. `build-hamburg-pmtiles.sh`).

---

### Schritt 2

Tiles generieren

Empfohlene Build-Umgebung:

- RAM: 32-64 GB
- Storage: 100 GB
- CPU: multi-core

```bash
planetiler \
  --osm-path=europe.osm.pbf \
  --output=basemap.mbtiles
```

---

### Schritt 3

PMTiles erzeugen

```bash
pmtiles convert basemap.mbtiles basemap.pmtiles
```

---

### Schritt 4

Deploy

upload basemap.pmtiles

z. B.

`tiles.weltgewebe.org/basemap.pmtiles`

---

## 5. MapLibre Integration

```javascript
import { Protocol } from "pmtiles";

const protocol = new Protocol();
maplibregl.addProtocol("pmtiles", protocol.tile);

const map = new maplibregl.Map({
  container: "map",
  // resolveBasemapStyle(config, document.documentElement.dataset.colorScheme)
  style: "/style.json" // or /style-dark.json when color-scheme is dark
});
// On live scheme changes: map.setStyle(...); rehydrate edges/overlays once.
```

---

Style:

```json
{
 "sources": {
   "basemap": {
     "type": "vector",
     "url": "pmtiles://tiles.weltgewebe.org/basemap.pmtiles"
   }
 }
}
```

### MapLibre Layer Order

MapLibre Layer Order:

1. Basemap layers
2. Graph edges
3. Nodes (DOM markers)
4. Focus / highlight

---

## 6. Overlay-Philosophie

Basemap enthält:

- landcover
- water
- roads
- administrative boundaries
- place labels

Basemap enthält NICHT:

- domain nodes
- graph edges
- semantic overlays

Strikte Trennung. Das verhindert später die Frage: "Packen wir das noch in die Basemap?"

commonThing enthält:

- nodes
- edges
- focus
- komposition

---

## 7. Versionierung

Basemap ist Artefakt.

`basemap-v1.pmtiles`
`basemap-v2.pmtiles`

Deploy-Strategie:

`/local-basemap/basemap-hamburg.pmtiles` (Öffentliche Edge-Route)
`/local-basemap/basemap-hamburg.meta.json` (Öffentliche Metadaten-Route)

*(Interner Storage-Pfad: `/srv/weltgewebe-basemap/`)*

---

## 8. Update- und Publish-Strategie

OSM Updatezyklus:

- **Rhythmus:** Ereignis- oder zeitgetrieben (z. B. monatlich oder bei signifikanten OSM-Diffs/Regionsupdates).
- **Prozess:** Ein Build-Job (z. B. `build-hamburg-pmtiles.sh` oder `build-germany-pmtiles.sh`) lädt den definierten OSM-Snapshot (gepinnt via SHA256) herunter und erzeugt **ausschließlich** das versionierte PMTiles-Artefakt sowie die dazugehörige `.meta.json`. Er erzeugt **keine** stabilen Aliase oder Current-Pfade.

Publish- und Rollback-Strategie (Contract-First):

- **Atomic Switch (PMTiles & Meta):** Neue versionierte Artefakte (z. B. `basemap-hamburg-v2.pmtiles` und `basemap-hamburg-v2.meta.json`) werden zuerst vollständig neben den aktiven Artefakten in das interne Zielverzeichnis (z. B. `/srv/weltgewebe-basemap/`) transferiert.
- **Verifikation (Der Sentinel Contract):** Die Einsatzbereitschaft wird über die `.meta.json` definiert. Diese Datei darf erst geschrieben werden, nachdem das PMTiles-Artefakt erfolgreich transferiert und geprüft wurde.
  - Das Schema der `.meta.json` **muss** folgende Felder enthalten, um als Contract zu gelten:
    - `version`: Version des Builds
    - `artifact_name`: z. B. "basemap-hamburg-v2.pmtiles"
    - `sha256`: Hash der generierten `.pmtiles` Datei
    - `size_bytes`: Dateigröße
    - `status`: `"ready"` oder `"invalid"`
- **Aktivierung:** Der duale Symlink-Switch (oder die atomare Dateiumbenennung) darf **ausschließlich** erfolgen, wenn die `.meta.json` validiert wurde (`status == "ready"`, Hash/Size stimmen). Die Aktualisierung der Aliase muss zwingend in dieser sicheren, sequenziellen Reihenfolge erfolgen:
  1. `ln -sfn basemap-hamburg-v2.pmtiles basemap-hamburg.pmtiles` (Zuerst das Tile-Artefakt)
  2. `ln -sfn basemap-hamburg-v2.meta.json basemap-hamburg.meta.json` (IMMER zuletzt den Meta-Alias)

  *Begründung:* Dies verhindert Race Conditions, bei denen der Meta-Alias bereits auf `v2` zeigt (und Einsatzbereitschaft signalisiert), das PMTiles-Artefakt aber noch `v1` liefert. Der Meta-Alias fungiert als finaler Freigabe-Sentinel.
- **Rollback:** Bei Laufzeit-Anomalien werden beide Symlinks/Aliase sofort auf das vorherige, intakte Paar (z. B. `v1`) zurückgesetzt. Konkrete Rollback-Trigger können sein:
  - Erhöhte HTTP-Fehlerquote (z. B. 404/500 auf der Edge-Route)
  - Fehlgeschlagene Range-Responses (PMTiles Client fordert Bytes an, Server liefert unvollständig)
  - MapLibre Client-Init-Fehler (Sichtbarkeit/Ladezeit überschreitet Timeout)
  Alte Artefakte verbleiben für eine Karenzzeit von mindestens 14 Tagen im Storage.

## 9. Performance

PMTiles Vorteile:

- Weniger HTTP Requests
- CDN Cache
- Streaming Tiles

Typischer Ladevorgang:

PMTiles reduziert typischerweise die Anzahl einzelner Tile-Anfragen, da Tiles aus einem zusammenhängenden Artefakt per HTTP Range Requests gelesen werden.

---

## 10. Erweiterungen

Später möglich:

Regionale Tiles

`germany.pmtiles`
`europe.pmtiles`

Offline Mode

local pmtiles

---

## 11. Alternative Sinnachse

Langfristig könnte commonThing mehrere Kartenprojektionen haben:

- Geographie
- Netzwerk
- Themenraum
- Zeit

Die Basemap ist dann nur eine Projektion des Wissensraums.

---

## Essenz

Die ideale Basemap-Architektur für das System ist:

MapLibre
   +
PMTiles
   +
planetiler
   +
OpenStreetMap
   +
eigener Style

Das liefert:

- maximale Souveränität
- langfristige Kontrolle
- minimale Anbieterabhängigkeit
- hohe Performance

---

## Risiken

- Komplexität der Tile-Generierungs-Pipeline
- Management von OSM-Updates
- Speichergröße regionaler Tilesets (Europa Tileset: 10-20 GB)
- Buildzeit (Planet: mehrere Stunden)

---

## Annahmen

- Langfristiger Plattformanspruch für die Basemap
- Infrastruktur-Integration (z. B. Heimserver)
- MapLibre bleibt als Rendering-Engine erhalten

## Invariante: Fäden als Dichtevisualisierung

Die Aktivitätsdichte wird ausschließlich durch Fäden dargestellt.

Eigenschaften:

- Anzahl der Fäden → Dichte
- Transparenz / Verblassen → Zeit
- Überlagerung → Intensität

Separate Heatmaps sind nicht zulässig, da sie:

- eine zweite Semantik einführen
- die direkte Lesbarkeit von Handlung verfälschen
- das Systemprinzip unterlaufen
