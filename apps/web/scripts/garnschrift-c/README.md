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
Die gebackenen fotografischen Schlagschatten werden aus der Alpha-Maske entfernt; die dichten Wollkerne und die echte Faserstruktur bleiben erhalten. Die optischen Abstände werden anhand dieser gereinigten Konturen neu berechnet.
Für die helle Oberfläche bleibt die kontrastreiche dunkelteale Garnvariante
mit ihren Helligkeitsabstufungen erhalten. Alle vier PNGs werden weiterhin
deterministisch aus der unveränderten Originalquelle erzeugt.

Die beiden zusätzlich vorgerenderten PNG-Wortbilder sind `Login` und
`Einstellungen`; auch sie verwenden dieselbe schattenfreie Faserfreistellung. Der iPad-Sichttest zeigte: Auch ein pauschaler
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

Die daraus reproduzierbar erzeugten PNGs haben kleinere Abmessungen. `YarnWord.svelte` stellt
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
Produkt im Browser geprüft werden. Die native Farbschrift wurde auf dem
echten iPad (iPadOS 26.7.1, WKWebView Canvas2D) einschließlich Satzzeichen
und Kerning beider Farbvarianten abgenommen. Die globale Verwendung
vergrößert kleine UI-Texte für die native Bitmapdarstellung.


## Globale Wollschrift als Standard

Wollschrift C ist standardmäßig auf allen HTML-Oberflächen eingeschaltet:
Überschriften, Fließtext, Navigation, Schaltflächen und Eingaben nutzen
die lokal gehosteten Garn-Farbschriften. Die Theme-Farben wählen echtes
Wollweiß für dunklen beziehungsweise Petrol für hellen Hintergrund.
Die revisionsgenau neu erzeugten WOFF-Dateien mit vollständigen Unterlängen und ohne fotografische Schlagschatten werden geladen.
Die Stile liegen früh im HTML-Head, damit kein verspätetes Nachladen der
Gestaltung entsteht; pro aktiver Farbvariante wird die passende WOFF
nur bei Verwendung angefordert (font-display: swap).

Schriftgrößen unter 17 px werden für lesbare HTML-Texte angehoben;
kleine Navigationsüberschriften erhalten mindestens 18 px. Primäraktionen
bekommen für die feste Garnfarbe eine kontrastierende Stofffläche mit
Akzentkante. Im systemweiten Hochkontrastmodus (Forced Colors) schaltet
die Oberfläche auf die Systemschrift zurück. Technische Kennungen,
Prüfsummen und Code behalten die eindeutige Monospace-Darstellung.
Kartentexte innerhalb von MapLibre-Kartenkacheln sind keine DOM-Texte
und nicht über Web-Font-CSS austauschbar.

Die Einstellung „Wollschrift verwenden“ kann in diesem Browser bei
Bedarf ausgeschaltet werden; ein früheres explizites Opt-out wird
respektiert. `?wollschrift=1` oder `?wollschrift=0` setzt diese
Auswahl direkt. Diese Wahl wird lokal unter
`commonthing.wool-everywhere` gespeichert. Bei fehlender Fontdatei
bleibt die HTML-Systemschrift als Fallback benutzbar. Die bestehenden
Garn-PNG-Wortbilder bleiben unangetastet.

Reproduzieren mit Python 3.13 und fonttools==4.61.1:

```sh
python3 -m pip install fonttools==4.61.1
python3 apps/web/scripts/garnschrift-c/generate_bitmap_font.py
python3 -m unittest discover -s apps/web/scripts/garnschrift-c -p 'test_*.py'
```

Die ursprüngliche PNG-Quelle wird SHA-geprüft und bleibt unverändert. Aus ihr werden nur die korrigierten Glyphenausschnitte und die transparente Alpha-Maske neu erzeugt.
Die nativen Wollbuchstaben sind gegenüber dem ersten Versuch minimal schlanker: Der
Farbschriftgenerator reduziert die Alpha-Deckkraft nur an den tatsächlichen
Faserkanten um höchstens 2/5 eines Rasterpixels. Wollfarben, dreifache
Zwirnung, Bildabmessungen, Grundlinien und Zeichenbreiten bleiben erhalten.
Nur das Kerning der nativen Farbschrift folgt den endgültigen Glyphenpositionen. Die produktiven Garn-PNG-Wortbilder werden nicht verändert.

Im systemweiten Hochkontrastmodus (Forced Colors) wird die Bitmap-Schrift zugunsten lesbarer Systemschrift deaktiviert.


## iPad/Safari: optische Grundlinie von Satzzeichen

Die Satzzeichen stammen aus eigenen Zeilen des Kontaktbogens. Ein Punkt
endete dadurch 17 Pixel unter der Buchstaben-Grundlinie, das kaufmännische
Und 13 Pixel darunter; Bindestriche lagen zu tief. Bei Safari waren diese
Unterschiede gerade in kleinen Wollbeschriftungen sichtbar.

Der Farbschriftgenerator richtet den dichten Garnkern von Punkt, Komma,
Semikolon, Doppelpunkt, Ausrufe- und Fragezeichen, Strichen und mehreren
Sonderzeichen an einer einheitlichen typografischen Grundlinie aus.
RGB-Faserfarben, organische Ränder und Pixelstruktur bleiben erhalten.
Das optische Kerning wird anhand der bereits vertikal korrigierten Glyphen
neu berechnet: Auch Paarungen wie „O-“ und „2.“ dürfen keine dichten
Faserkerne überlappen lassen. Bei den Nicht-Unterlängenbuchstaben i und k werden außerdem
winzige, abgetrennte Quellbildflecken unterhalb der Grundlinie unterdrückt
(jeweils weniger als 1,3 % ihrer ursprünglichen Alpha-Deckung). Die
eigentlichen Wollfäden bleiben erhalten.
Für sauberes plattformübergreifendes Bitmap-Layout erhält jede
echte Glyphe einen **145 Pixel hohen transparenten Em-Kasten** und den
identischen sbix-Vertikalursprung -40. Damit gibt es keine je Buchstabe
unterschiedlichen sbix-Offsets mehr, die Safari abweichend auslegen kann.

Die Originalquelle und normalen Text-Fallbacks bleiben unverändert. Die vier Titel-PNGs werden gemeinsam mit den beiden WOFF-Dateien aus der gereinigten Alpha-Maske regeneriert. Kein CSS-Trick, keine neue
Browserberechnung und keine externe Schriftressource.
## iPad/Safari: vollständige Unterlängen und schattenfreie Garnkanten

Die originale Bildvorlage hat überlappende Alphabetzeilen. Die bisherigen
Ausschnitte der Buchstaben `g`, `p`, `q` und `y` endeten teilweise
**innerhalb** eines noch dichten Wollfadens. Die neue Extraktion reicht nur
bis zum natürlichen sichtbaren Fadenende. Bei `g` wird dabei ein fremder
Fleck aus der nächsten Zeile gezielt ausgeschlossen. `i`, `j` und `k`
enden vor den Bildpixeln der folgenden Alphabetzeile; so entsteht unter
dem `i` kein schwebender Kurzstrich.

Die alten grauen Schatten waren nicht durch `text-shadow` erzeugt, sondern
als schwache Deckkraft direkt in den fotografierten Faserbildern enthalten.
Die Freistellung entfernt Foto-Restdeckung bis Alpha 108, blendet die
Übergangszone bis 160 sanft aus und erhält die dichten echten Garnpixel samt
organischem Rand und innerer Zwirnung. Keine CSS-Schatten, keine Outline,
keine neue Schriftästhetik. Das identische Verfahren gilt für beide Themen
und die separat gespeicherten `Login`-/`Einstellungen`-PNGs.

Regressionen prüfen nun vollständige Unterlängen, keine fremden Unterstriche,
keine Schattenmaske, identische Originalquelle, WOFF-Bitmaps und Satzzeichen.
