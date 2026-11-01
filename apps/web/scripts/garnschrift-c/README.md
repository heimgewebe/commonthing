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

**Quelle und zwei Darstellungsarten:** Das originale PNG ist ein
fotografischer Kontaktbogen, keine Fontdatei. Die zwei bestehenden Überschriften
verwenden weiterhin die PNG-Wortbilder. Zusätzlich erzeugt
generate_bitmap_font.py echte OpenType-Farbschriften in WOFF1-Containern mit
sbix-PNG-Glyphen aus den Originalfasern und paarweisem optischem Kerning.
Es entstehen keine künstlichen Umrisse oder CSS-Stickereien. Browser setzen
beliebigen semantischen Text einschließlich Formularinhalten direkt mit
den Bitmapglyphen; nicht vorhandene Zeichen nutzen den Systemfont.

## Aktuelle Umsetzung

`raster_from_sheet.py` liest das originale RGB-PNG ausschließlich mit der
Python-Standardbibliothek, kontrolliert seinen SHA-256-Wert und seine Dimensionen,
und verwendet feste, überprüfbare Rechtecke für die einzelnen Originalbuchstaben.
Die abgetrennten Garnpixel werden aus dem petrolfarbenen Hintergrund freigestellt.
Für das dunkle Oberflächenthema wird die gemessene Helligkeit der Originalfasern
auf eine natürliche Wollweißpalette abgebildet: schattierte Vertiefungen
(#C6C3BA), Wollgrundfarbe (#F0EEE7) und sanfte Faserreflexe (#FCFAF5).
Die Original-Luminanzanker (ungefähr 75/175/225) erhalten die Zwirnung;
**Alpha-Maske, Konturen und optisches Kerning bleiben unverändert.**
Für die helle Oberfläche bleibt die kontrastreiche dunkelteale Garnvariante
mit ihren Helligkeitsabstufungen erhalten. Alle vier PNGs werden weiterhin
deterministisch aus der unveränderten Originalquelle erzeugt.

Die beiden tatsächlich in Commonthing verwendeten Titel sind `Login` und
`Einstellungen`. Der iPad-Sichttest zeigte: Auch ein pauschaler
Buchstabenabstand von null Pixeln lässt ungleichmäßige große Lücken.
Der Generator berechnet deshalb **optisches Kerning pro Buchstabenpaar**:
Er vergleicht die dichten Garnpixel (Alpha ab 100) auf den gemeinsamen
Grundlinien-Zeilen und verringert den Abstand, bis zwischen den Garnkernen
mindestens zwei Pixel bleiben. Die Annäherung beträgt maximal neun Pixel
und höchstens ein Drittel der jeweils schmaleren Glyphe. Leichte
Faserüberlappungen werden korrekt transparent übereinandergeblendet,
statt bereits gezeichnete Wollfasern abzuschneiden. Leerzeichen bleiben
unverändert. Die Berechnung läuft nur beim Erzeugen der Dateien, niemals
während der Browserbenutzung.

Die neuen PNGs haben kleinere Abmessungen. `YarnWord.svelte` stellt
`Login` mit 7 rem und `Einstellungen` mit 16 rem Breite dar, damit
die Garnfäden nicht durch horizontale Streckung wieder auseinandergezogen
werden. Semantische Überschriften und Standardschrift-Fallback bleiben.

Es werden pro Wort und Theme transparente PNG-Dateien erzeugt,
ohne Netzwerkanfragen beim Generieren, ohne Browser-JavaScript zur Bildgeometrie
und ohne externe Font-CDN-Abhängigkeit.

| Ziel in `apps/web/static/garnschrift-c/` | Auflösung | Zweck |
| --- | --- | --- |
| `login-dark.png`, `login-light.png` | 260 × 145 | Anmelden |
| `einstellungen-dark.png`, `einstellungen-light.png` | 592 × 145 | Einstellungen |

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
Produkt im Browser geprüft werden. Standardmäßig bleibt für kleine Fließtexte
und Formularbeschriftungen die zugängliche Systemschrift erhalten. Der
Opt-in-Versuch darunter testet die echten Garnglyphen ausdrücklich auch dort.


## Opt-in-Probe: Wollschrift für lesbare UI-Texte

In Einstellungen → Darstellung den Schalter „Wollschrift testen (Experiment)“
aktivieren oder /settings?wollschrift=1 aufrufen. Das Experiment speichert
nur commonthing.wool-everywhere im aktuellen Browser. Abschalten mit dem
Schalter oder ?wollschrift=0. Standard bleibt aus: kein Font- oder zusätzlicher
Stylesheet-Download im normalen App-Startpfad. Die Styles und beiden
Schriftvarianten werden nur beim aktiven Versuch geladen. Die lokal versionierten WOFF-Dateien enthalten
natürliches Wollweiß für dunkle und kontrastreiches Petrol für helle Themen.
Der Theme-Wechsel übernimmt die bestehende Einstellung. Original-PNGs,
semantische HTML-Elemente, Browser-Selektion, Texteingaben und Lade-Fallbacks
bleiben unberührt. Eine Browserbibliothek oder Laufzeit-Rasterisierung
gibt es nicht. Wegen der begrenzten Rasterauflösung und der fest eingefärbten
Garnpixel bleiben lange Fließtexte, kleine Hinweise, Formulare, Eingabefelder,
alle kleinen Bedienelemente, Primäraktionen mit Farbfüllung, Pfeilnavigation
und technische Monospace-Kennungen in der zugänglichen Systemschrift.
Kurze Navigation und größere Überschriften tragen weiterhin echte
Wollglyphen. Die Einstellungsnavigation verwendet im Experiment mindestens
18 px, da die dreifache Faserstruktur bei den früheren 14 px verschwamm. Nicht im Alphabet vorhandene
Sonderzeichen verwenden den regulären Font-Fallback. In MapLibre-
Kartenkacheln eingebettete Kartentexte sind nicht Teil des HTML-Fontpfads.

Reproduzieren mit Python 3.13 und fonttools==4.61.1:

```sh
python3 -m pip install fonttools==4.61.1
python3 apps/web/scripts/garnschrift-c/generate_bitmap_font.py
python3 -m unittest discover -s apps/web/scripts/garnschrift-c -p 'test_*.py'
```

Die ursprüngliche PNG-Quelle wird SHA-geprüft und bleibt unverändert.
Im opt-in-Schriftversuch sind die Buchstaben minimal schlanker: Der
Farbschriftgenerator reduziert die Alpha-Deckkraft nur an den tatsächlichen
Faserkanten um höchstens 2/5 eines Rasterpixels. Wollfarben, dreifache
Zwirnung, Bildabmessungen, Grundlinien, Zeichenbreiten und Kerning
bleiben erhalten. Die produktiven Garn-PNG-Wortbilder werden nicht verändert.

Im systemweiten Hochkontrastmodus (Forced Colors) wird die Bitmap-Schrift zugunsten lesbarer Systemschrift deaktiviert.
