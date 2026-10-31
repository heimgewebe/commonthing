# Garnschrift C — Entwurf 02

`paths.json` ist das unveränderte, mit Python `base64`, `zlib` und `json`
decodierte Glyphenobjekt aus dem freigegebenen Entwurf 02. Werte: Vorschub und
Liste absoluter SVG-Zentrallinien (M/L/C/Q/Z); keine Fontkonturen.

Regenerieren vom Repo-Root, ohne zusätzliche Pakete:

```sh
python3 apps/web/scripts/garnschrift-c/generate.py
python3 -m unittest discover -s apps/web/scripts/garnschrift-c -p 'test_*.py'
```

Der Generator tastet Kurven ab und parametrisiert sie nach Bogenlänge.
Drei sinusförmig verdrillte, runde Teilstränge erhalten einen schmaleren
Lichtkern; rückwärtige Helixbögen werden vor vorderen Bögen gezeichnet.
Deterministische kleine Seitenschwankungen und vereinzelte abstehende Fasern
geben organische Unregelmäßigkeit. Es gibt keine äußere Kontur, Textmaske,
SVG-Text, Filter, Hintergrundfläche oder externe Ressourcen.

Die vier transparenten SVGs in `../../static/garnschrift-c/` sind eingecheckte
Build-Artefakte. Regeneration ist nur bei Änderungen der Quelle nötig und
nicht Teil des Browser-Runtimes. Harte Grenze: 200.000 Bytes pro Asset.
`dark` ist warmbeige, `light` dunkelteal. YarnWord reserviert die Bildproportionen
und nutzt das vorhandene `html[data-color-scheme]`; echte h1-Texte bleiben
zugänglich. Bei Ladefehlern und in Forced Colors bleibt Standardschrift sichtbar.
Die Komponente unterstützt SSR mit echtem Text ohne Inline-Styles. Die Anwendung
deaktiviert SSR derzeit zentral in `src/routes/+layout.ts`; daher liefern die
Seiten ohne JavaScript wie bisher nur die App-Hülle. Diese globale Einstellung
wird hier nicht geändert. Die Ladeereignisse markieren nur das jeweilige Bild im DOM; Themewechsel
benötigen keine eigenen Observer oder Stores. Beide Theme-Varianten werden
lokal geladen. Es gibt keine JavaScript-Berechnung der Garngeometrie.

## Abgleich mit dem Google-Drive-Export vom 31.10.2026

- Drive-Ordner: [commonthing](https://drive.google.com/drive/folders/1BTgj6vjMer6dL6PSAU559Xxxa4MXOiBL)
- Ursprungsdatei [font](https://drive.google.com/file/d/11oTu6lh8RRfTIVffXtGA7E0mo15mppxp/view), ID 11oTu6lh8RRfTIVffXtGA7E0mo15mppxp.
- Vollständig abgerufener Originalinhalt: text/html, 596302 Byte, SHA-256 ac3a912dd4036396b49f503a0d02d3b98679f3bcc476b40f846c270c6ed08112.
- Befund: gespeicherte ChatGPT-Bildfreigabe („Garnschrift-Probe auf dunklem Petrol“) mit Verweisen auf Raster-Bildvorschauen, aber **ohne** WOFF/WOFF2, @font-face, reproduzierbaren Fontgenerator oder verwendbare Schrift-Glyphenpfade. Die enthaltenen SVGs sind ChatGPT-Oberflächenicons.
- Konsequenz: Es wird weder dieses vollständige HTML als angebliche Schrift eingecheckt noch aus Bildvorschauen eine nicht existente Schriftdatei abgeleitet. Die Freigabe ist **visuelle Referenz**, keine technische Fontquelle.
- **Technisch verwendete und versionierte Quellen** bleiben die bereits im PR vorhandenen paths.json (63 Glyphen-Zentrallinien, aus dem zuvor freigegebenen Entwurf 02) und generate.py. Damit lassen sich die lokalen SVG-Wortgrafiken ohne Google Drive, ChatGPT, CDN oder Laufzeitgenerator reproduzieren.
- **Formatgrenze:** Die SVG-Dateien sind dekorative Wortbilder mit semantischem HTML-Text als Fallback, keine installierbare Fontfamilie. Gewöhnliche OpenType-/WOFF2-Konturen würden die einzelnen gedrehten Wollstränge, Lichtkanten und Fasern nicht getreu wiedergeben; deshalb wird hier bewusst keine WOFF2-Datei erzeugt.
- **Zeichenumfang:** A–Z, a–z, ÄÖÜäöüß, Leerzeichen, Bindestrich, Punkt und Doppelpunkt. Im Browser sind derzeit nur Login und Einstellungen als vorgerenderte SVGs eingebunden. Weitere Worte erfordern neue Build-Artefakte, nicht einfach eine CSS font-family.

**Designgrenze:** Die deterministische Vektordarstellung ist eine organische Näherung an Garnschrift C, keine pixelidentische Übernahme der fotografischen Bildvorlage. Lesbarkeit, Hell-/Dunkelmodus und bestehende Browser-Fallbacks haben Vorrang.
