# Garnschrift C – fotografische Garnvorlage

## Verbindliche Quelle

Die vom Nutzer freigegebene Datei **echte font** im [Google-Drive-Ordner
commonthing](https://drive.google.com/drive/folders/1BTgj6vjMer6dL6PSAU559Xxxa4MXOiBL)
ist ein Bild des Alphabets aus tatsächlich gestalteten, dreifach gezwirnten
Wollfäden. Ihre private Google-Drive-ID lautet
`1bjgFLZxltB6mlgSFaOha_nGkK-NZuuJZ`.

- Format: `image/png`, RGB, 1448 × 1086 Pixel, 2.438.325 Byte.
- Originalquelle im Repository:
  [garnschrift-c-contact-sheet.png](../../../../docs/assets/garnschrift-c-contact-sheet.png).
- Original-SHA-256:
  `eb09901816a4fc04a001cd2a7c2e51f12ca6b04e68a892727087cd1bcd76e2c2`.
- Das Bild enthält Groß- und Kleinbuchstaben einschließlich deutscher Umlaute,
  Ziffern, Sonderzeichen und Beispielwörter.
- Das ursprüngliche Drive-Objekt `font` (ID
  `11oTu6lh8RRfTIVffXtGA7E0mo15mppxp`) ist dagegen nur eine gespeicherte
  ChatGPT-HTML-Seite. Sie enthielt keine Schriftdateien.

**Technische Grenze:** Das neue PNG ist ein Schriftmusterblatt, keine
installierbare WOFF2-, OpenType- oder TrueType-Schrift. Rasterisierte
Wolloberflächen lassen sich nicht als gewöhnliche monochrome Fontkonturen
erhalten. Die Weboberfläche setzt deshalb lokale, transparente **PNG-Wortbilder**
ein. Ein echtes, semantisches HTML-`h1` mit lesbarer Standardschrift bleibt
unabhängig von den Bildern im DOM.

## Aktuelle Umsetzung

`raster_from_sheet.py` liest das originale RGB-PNG ausschließlich mit der
Python-Standardbibliothek, kontrolliert seinen SHA-256-Wert und seine Dimensionen,
und verwendet feste, überprüfbare Rechtecke für die einzelnen Originalbuchstaben.
Die abgetrennten Garnpixel werden aus dem petrolfarbenen Hintergrund freigestellt.
Für die helle Oberfläche wird eine dunkelteale Garnvariante unter Bewahrung der
Helligkeitsabstufungen erzeugt.

Die beiden tatsächlich in Commonthing verwendeten Titel sind `Login` und
`Einstellungen`. Es werden pro Wort und Theme transparente PNG-Dateien erzeugt,
ohne Netzwerkanfragen beim Generieren, ohne Browser-JavaScript zur Bildgeometrie
und ohne externe Font-CDN-Abhängigkeit.

| Ziel in `apps/web/static/garnschrift-c/` | Auflösung | Zweck |
| --- | --- | --- |
| `login-dark.png`, `login-light.png` | 325 × 145 | Anmelden |
| `einstellungen-dark.png`, `einstellungen-light.png` | 764 × 145 | Einstellungen |

Vom Repository-Root reproduzieren und prüfen:

```sh
python3 apps/web/scripts/garnschrift-c/raster_from_sheet.py
python3 -m unittest discover -s apps/web/scripts/garnschrift-c -p 'test_*.py'
```

Der Generator unterstützt darüber hinaus die im Blatt lesbaren Buchstaben
`A-Z`, `a-z`, `ÄÖÜäöüß`, `0-9` und ausgewählte Sonderzeichen.
Weitere Worte können als eigene Build-Artefakte erzeugt werden. Es gibt
**keine allgemeine `font-family`**, die beliebigen Browsertext automatisch
aus dem Bild zusammensetzt.

`YarnWord.svelte` zeigt die passende lokale Theme-Bildvariante erst nach
erfolgreichem Laden. Das Überschriftenmaß wird reserviert. Bei Bildausfall und
in Forced Colors bleibt der echte Überschriftentext sichtbar. Beide Theme-Dateien
werden zurzeit angefragt; die Einzeldateien sind deutlich kleiner als die
früheren SVG-Wortbilder. Karte, Anmeldung, Fokuspaneel und globale
Typografie werden dadurch nicht umstrukturiert.

## Frühere Vektorentwicklung

`paths.json` und `generate.py` bleiben als nachvollziehbarer,
reproduzierbarer SVG-Entwurf 02 erhalten. Dieser enthält 63 geometrische
Glyphen-Zentrallinien, aus denen sinusförmig verdrillte Fäden und Fasern
gezeichnet werden. Die vier SVG-Buildartefakte bleiben vorerst für Vergleich
und Rückfall erhalten, werden aber **von der aktiven `YarnWord`-Komponente
nicht geladen**. Es gibt somit nur einen aktiven Browser-Darstellungspfad.

```sh
python3 apps/web/scripts/garnschrift-c/generate.py
```

## Grenzen vor einer Produktivfreigabe

Die Vorlage ist ein 1448-Pixel-Raster, die einzelnen Buchstaben sind relativ
klein. Bei großer Darstellung oder hochauflösenden Bildschirmen kann
Rasterweichzeichnung auftreten. Die freigestellten Fasern sind eine
Pixelapproximation, keine verlustfreie 3D-Rekonstruktion. Die genaue
Materialwirkung, Hell-/Dunkelkontrast und iPad-Safari müssen am endgültigen
Produkt im Browser geprüft werden. Für kleine Fließtexte und Formularbeschriftungen
bleibt bewusst die zugängliche Standardschrift erhalten.
