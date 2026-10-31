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
