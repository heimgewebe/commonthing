---
id: specs.ui-interaction
title: UI-Interaktionsvertrag
summary: Kanonischer Vertrag für Karte, Fokuspanel, Werkzeugfächer, Aufmerksamkeit, Kartenlinsen, Farbschema, Komposition und Zugänglichkeit.
doc_type: specification
status: canonical
canonicality: normative
lifecycle_state: active
role: norm
organ: product-ui
owner: product-ui
last_reviewed: 2026-08-22
review_after: 2026-10-11
depends_on:
  - specs.ui-state-machine
  - specs.garnrolle-knoten-faden
relations:
  - type: supersedes
    target: docs/blueprints/ui-interaction-doctrine.md
  - type: supersedes
    target: docs/blueprints/ui-blaupause.md
verifies_with:
  - apps/web/src/lib/components/ContextPanel.svelte
  - apps/web/src/lib/components/ToolFan.svelte
  - apps/web/src/lib/components/AttentionBubbles.svelte
  - apps/web/src/lib/accountAttentionRuntime.ts
  - apps/web/src/app.html
  - apps/web/static/theme-init.js
  - apps/web/src/lib/styles/tokens.css
  - apps/web/src/lib/components/TopBarAuth.svelte
  - apps/web/src/routes/settings/+page.svelte
  - apps/web/src/lib/stores/mapChrome.ts
  - apps/web/src/lib/components/SearchOverlay.svelte
  - apps/web/src/lib/components/FilterOverlay.svelte
  - apps/web/tests/attention-bubbles.spec.ts
  - apps/web/tests/map-interaction.spec.ts
  - apps/web/tests/ui-filter.spec.ts
  - apps/web/tests/ui-search.spec.ts
  - apps/web/tests/tool-fan-layout.spec.ts
  - apps/web/tests/context-panel-sheet.spec.ts
  - apps/web/tests/theme.spec.ts
attention_source_status: none
attention_source_rationale: "Definiert die Attention-Darstellung und Projektion selbst, ist aber keine Quelle kanonischer persönlicher Fachfakten."
---

# UI-Interaktionsvertrag

## Leitidee

commonThing ist ein kartenbasiertes Koordinationsinterface. Die Karte ist der öffentliche Wahrnehmungsraum; Details und Handlungen werden nicht in parallelen Dashboards verteilt, sondern in einem einzigen kontextbezogenen Arbeitsraum gebündelt.

## Drei Hauptflächen

| Fläche         | Verantwortung                                       |
| -------------- | --------------------------------------------------- |
| Karte          | räumlicher Überblick, Auswahl und sichtbares Gewebe |
| Fokuspanel     | Details, Gespräche, Entscheidungen und Handlungen   |
| Werkzeugfächer | Finden, Karteninhalt und Webungsaktionen            |

Aktuelle Aufmerksamkeit ist keine vierte Hauptfläche. Sie erscheint als kompakte, nichtmodale Orientierung in der Kartenleiste und führt in die bereits vorhandenen Fachräume.

Es gibt keinen zweiten Detail-Drawer, kein dauerhaftes Seitenmenü für Objektinhalte und keine frei schwebenden Hauptformulare über der Karte.

## Karte

Die Karte bleibt während Fokus und Komposition sichtbar. Sie darf gedämpft oder teilweise verdeckt werden, verliert aber nicht ihre Rolle als räumlicher Zusammenhang.

Interaktionen:

- Marker oder Faden wählen → Fokus öffnen;
- leere Kartenfläche wählen → Fokus schließen, sofern kein lokaler Dialog Vorrang hat;
- Karte bewegen → kein neuer globaler Zustand;
- neue Webung beginnen → Komposition im Fokuspanel.

Der erste Kartenausschnitt folgt einer eindeutigen Priorität:

1. Ein ausdrücklich per URL adressierter Knoten oder eine Garnrolle wird gezeigt.
2. Sonst startet eine angemeldete Person auf ihrer öffentlich verorteten Garnrolle.
3. Fehlt eine verortete eigene Garnrolle oder eine Anmeldung, gilt der kanonische Fallback-Ausschnitt.

Die Kamera wird möglichst vor dem ersten sichtbaren Kartenbild bestimmt. Ein unnötiger Zwischensprung vom Fallback zur eigenen Garnrolle ist zu vermeiden.

## Fokuspanel

Das Fokuspanel ist der einzige Detail-, Entscheidungs- und Handlungsraum.

Responsive Darstellung:

- mobil als Bottom Sheet mit den Raststufen Vorschau, Halb und Voll;
- auf breiten Bildschirmen als rechtes Seitenpanel.

Fokus startet mobil in der Vorschau und kann bewusst erweitert werden. Komposition öffnet ausreichend groß. Alle Stufen teilen denselben Fachzustand und dieselben Inhalte; sie sind keine zusätzlichen globalen Zustände.

Typische Tabs:

- Knoten: Übersicht, Gespräch, Anträge, Verlauf;
- Garnrolle: Profil, Aktivität, Knoten;
- Faden: Art, beteiligte Endpunkte, Zeit und vorhandene Notiz.

Tabs sind lokal. Sie werden erst Teil der URL, wenn ein eigener verbindlicher Tab-Vertrag existiert.

## Werkzeugfächer

Der Werkzeugfächer formuliert Absichten, ohne der Karte dauerhaft eine Leiste zu entziehen. Sein kompakter Wurzelknopf sitzt unten mittig. Geöffnet besitzt die erste Ebene genau drei stabile Hauptäste:

- **Finden** öffnet die Suchlinse;
- **Karteninhalt** öffnet die Auswahl der sichtbaren Kartenarten;
- **Weben** öffnet eine zweite, höchstens eine Ebene tiefe Auswahl realer Webungsaktionen.

Die Webungsebene trennt die fachliche Kategorie von der konkreten Handlung. Der aktuelle Produktstand bietet dort:

- **Knoten knüpfen** für jeden angemeldeten Account;
- **Antrag stellen** für Gäste als direkter Einstieg in den produktiv vorhandenen Weberantrag.

Gäste können eigene Knoten im Fokuspanel bearbeiten. An fremden oder historisch
eigentümerlosen Knoten bleibt der Bearbeitungstab verborgen; Weber und
Administratoren erhalten dort die gemeinschaftliche Pflegeaktion.

Weitere Antragstypen dürfen erst erscheinen, wenn ihr Serververtrag tatsächlich produktiv ist. Eine deaktivierte oder beschriftete Oberfläche darf keine nicht vorhandene Schreibfähigkeit vortäuschen. Rollenabhängige Berechtigungen dürfen die drei Hauptäste nicht verschieben.

Der Werkzeugfächer schließt durch erneutes Betätigen, Escape, Fokuswechsel nach außen oder Auswahl außerhalb. Er ist kein modaler Dialog und hält den Tastaturfokus daher nicht gefangen. Beim Schließen per Escape kehrt der Fokus zum Wurzelknopf zurück. Animationen verwenden Transformation und Deckkraft und entfallen bei reduzierter Bewegung.

## Aufmerksamkeit und Governance

### Zukunftsregel für neue Fachfunktionen

Jede neue oder wesentlich erweiterte kanonische Fachfunktion muss ihre persönliche Attention-Relevanz entscheiden, sobald sie für einen Account eine notwendige Handlung, neue persönliche Information, eine konkrete Beteiligungsmöglichkeit oder einen eigenen laufenden Vorgang erzeugen kann. Diese Prüfung findet beim Fachvertrag statt, nicht in einem separaten Attention-Register und nicht als pauschales Kästchen für jeden Pull Request.

Kanonische Produktverträge tragen dafür `attention_source_status: source | none | blocked` plus eine Begründung. `source` benennt die kanonischen persönlichen Fakten, die Projektionsstelle und Übergangstests. `blocked` ist nur zulässig, wenn der fehlende kanonische Fachfakt und ein konkreter Bureau-Folgetask benannt sind; solange bleibt die Projektion fail-closed. `none` bedeutet eine bewusste Entscheidung, dass der Vertrag selbst keine persönliche Attention-Wahrheit erzeugt. Die Metadaten beschreiben die Architektur und sind niemals Laufzeit- oder Nutzerzustand.

Die Kartenleiste zeigt oben links kleine Aufmerksamkeitsblasen für aktuell offene, für den angemeldeten Account relevante Sachverhalte. Diese Blasen sind keine Ereignishistorie, kein Benachrichtigungsarchiv und keine zweite Fachwahrheit. Sie werden aus den kanonischen Fach-APIs abgeleitet; ein Invalidierungssignal fordert lediglich eine erneute Lesung dieser Wahrheit an.

Regeln:

- eine zugrunde liegende Sache erzeugt höchstens eine sichtbare Aufmerksamkeitseinheit; insbesondere gilt eine Direktunterhaltung als eine Einheit und ein Antrag als eine Einheit;
- Aufmerksamkeit ist eine reine Projektion der kanonischen Fachwahrheit. Sie speichert weder einen eigenen Erledigt-Zustand noch eine parallele Benachrichtigungs- oder Aufgabenwahrheit;
- jede sichtbare Einheit erhält genau eine abgeleitete Bedeutung: **Handlung erforderlich**, **Neu für dich**, **Mitwirkung möglich** oder **Läuft ohne dein Zutun**; diese Bedeutungen sind Darstellungssemantik und keine neuen Fachzustände;
- die Bedeutungen werden sichtbar und deterministisch geordnet: **Handlung erforderlich** → **Neu für dich** → **Mitwirkung möglich** → **Läuft ohne dein Zutun**. Innerhalb derselben Bedeutung entscheidet die jüngste fachlich relevante Änderung; bei Gleichstand entscheidet die stabile Identität. Eine Frist darf diese Bedeutungsreihenfolge nicht verändern;
- **Handlung erforderlich** darf nur entstehen, wenn die Fachwahrheit eine konkrete notwendige Handlung dieses Accounts belegt. Fehlt diese Evidenz, wird keine Pflicht behauptet;
- **Neu für dich** bezeichnet neue Information, etwa eine ungelesene Direktunterhaltung, und behauptet keine Antwortpflicht;
- **Mitwirkung möglich** bezeichnet eine belegte freiwillige Beteiligungsmöglichkeit. Eine laufende Abstimmung darf nur dann persönlich so projiziert werden, wenn der Server die Teilnahmeberechtigung und den eigenen Abstimmungsstand für diesen Account belegt;
- **Läuft ohne dein Zutun** ist auf eigene offene Vorgänge begrenzt und signalisiert ausdrücklich, dass aktuell keine Handlung des Accounts nötig ist. Mehrere solche Vorgänge werden zu genau einem ruhigen Sammelstatus verdichtet. Dieser Sammelstatus bleibt auch bei aktiverer Aufmerksamkeit sichtbar, wird aber durch die feste Bedeutungsreihenfolge dahinter eingeordnet; die Vorgänge bleiben in `/antraege` erreichbar;
- echte Fachfristen werden in Karte beziehungsweise Überlauf als Zeitangabe sichtbar und dürfen die Darstellung verstärken, erzeugen aber keine eigene Attention-Klasse und verändern weder die Bedeutungsreihenfolge noch die zeitliche Reihenfolge innerhalb einer Klasse. Für offene Governance liefert der Server `remaining_seconds` aus seiner eigenen Uhr; Attention kalibriert daraus die Restzeit und zieht anschließend nur die seit dem bestätigten Listenread lokal verstrichene Zeit ab. Eine falsch gestellte Geräteuhr darf eine Beteiligungsmöglichkeit deshalb weder vorzeitig ausblenden noch künstlich dringlich machen. Die Projektion verwirft abgelaufene Möglichkeiten und berechnet ihre reine Darstellung mindestens minütlich neu; die fachliche Frist selbst bleibt ausschließlich Serverwahrheit. Antworten älterer oder gemockter APIs ohne `remaining_seconds` dürfen während des Cutovers auf den absoluten Fristzeitpunkt zurückfallen. Alter allein erzeugt niemals Dringlichkeit;
- persönliche Beteiligungsfakten und fachliche Aktivitätszeitpunkte gehören in die kanonische Fach-API. Die Proposal-Liste liefert dafür `viewer_participation` mit `vote_choice`, `has_veto`, `may_vote` und `may_veto` sowie `last_activity_at` als jüngsten öffentlichen Fachzeitpunkt aus Antragserzeugung, bereits vollzogenem Phasenwechsel, Veto, Gesprächsbeitrag oder Stimme. Eine zukünftige Frist ist keine Aktivität; `consent_until` darf erst nach dem tatsächlichen Wechsel in die Abstimmungsphase als vergangener Phasenzeitpunkt eingehen. Attention übernimmt `last_activity_at` und rekonstruiert ihn nicht selbst aus `created_at`, `consent_until` oder anderen Ersatzfeldern. Bei angemeldeten Accounts bedeutet `vote_choice: null` explizit „keine eigene Stimme vorhanden“. Anonyme Leser erhalten `viewer_participation: null`. Die bisherigen flachen Felder `can_vote`, `own_vote`, `can_veto` und `own_veto` dürfen während des Cutovers ausschließlich als Wire-Kompatibilität für bereits geladene ältere Browserstände mitgeliefert werden; sie sind keine zweite kanonische Semantik und neue Verbraucher dürfen sich nicht mehr daran binden. Attention darf Beteiligungsfakten nicht durch Detailabfragen je Antrag rekonstruieren;
- Rollenwechsel werden zusätzlich clientseitig fail-closed maskiert: eine alte Beteiligungsberechtigung darf nach einer Herabstufung nicht bis zum nächsten Netzwerkabruf sichtbar bleiben;
- auf schmalen Ansichten bleibt nur eine begrenzte Zahl von Blasen sichtbar; weitere Einheiten bleiben über einen berührbaren `+N`-Überlauf erreichbar und behalten dieselbe semantische Reihenfolge;
- jede direkte Bedienfläche besitzt mindestens 44 × 44 Pixel; Bedeutung muss zusätzlich zu Farbe durch Form, Umrandung, Symbol oder Text erkennbar sein;
- neue Aufmerksamkeit darf die Kartenkamera niemals selbständig bewegen;
- das Öffnen einer Blase markiert nichts als erledigt. Es hält die Karte offen und zeigt eine kompakte, nichtmodale Aufmerksamkeitskarte mit Bedeutung, Grund und genau einer Hauptaktion; erst diese Aktion führt in die bestehende kanonische Fachansicht;
- generisches Wegwischen, Snooze, Pinning, Attention-Historie oder ein eigener `gesehen`-Zustand gehören nicht zu diesem Vertrag;
- reduzierte Bewegung verhindert nicht die Zustandsänderung, sondern nur deren dekorative Animation.

Der aktuelle Quellensatz umfasst ungelesene Direktunterhaltungen, eigene offene Weber- und Sachanträge sowie kollektive Governance nur dann, wenn die kanonische Liste für den aktuellen Account eine noch offene formale Beteiligungsmöglichkeit belegt. Der aktuelle Produktstand besitzt noch keinen Fachfall, der zuverlässig **Handlung erforderlich** erzeugt; die Darstellung unterstützt ihn bereits, die Projektion erfindet ihn aber nicht.

Allgemeine Governance-Navigation ist keine persönliche Aufmerksamkeit. Die Vollansicht `/antraege` bleibt der kanonische Lese- und Navigationsraum und bietet weiterhin reale Filter für:

- alle Anträge;
- offene Konsentverfahren;
- Anträge mit Veto;
- Gespräche mit tatsächlichen Beiträgen;
- laufende Abstimmungen.

Das Stellen eines Antrags bleibt eine Webungsaktion im Werkzeugfächer beziehungsweise im passenden Fachraum. Ein zusätzlicher Governancefächer in der Kartenmitte ist nicht Teil der kanonischen Chrome.

## Kartenlinsen

Finden und Karteninhalt sind lokale Kartenlinsen:

- sie verändern die sichtbare Szene;
- sie erzeugen keinen neuen globalen Zustand;
- sie schließen sich gegenseitig;
- sie erscheinen kompakt oberhalb der Karte und reservieren keine dauerhafte untere Fläche;
- die Karte bleibt die primäre Trefferfläche; Finden zeigt höchstens sechs automatische Vorschläge, weitere Treffer öffnen nur auf bewusste Anforderung; Sicht zeigt keine automatische Trefferliste, nur Typauswahl, aktive Anzahl und Rücksetzen;
- passende Knoten und Garnrollen werden auf der Karte hervorgehoben;
- liegt ein Treffer außerhalb des nutzbaren Kartenausschnitts, zeigt ein Richtungsmarker am Bildschirmrand zu ihm;
- Richtungsmarker bleiben außerhalb von Topbar, sichtbaren Kartenlinsen, sichtbarer Aufmerksamkeitskarte oder sichtbarem Aufmerksamkeitsüberlauf, Werkzeugfächer und Fokuspanel und besitzen mindestens 44 × 44 Pixel;
- ein Treffer oder Richtungsmarker kann die Karte fokussieren und das Fokuspanel öffnen;
- ein API-Fehler darf nicht wie eine normale leere Ergebnismenge aussehen.

## Komposition

Komposition bedeutet, etwas ins Gewebe zu setzen. Sie läuft im Fokuspanel und besitzt:

- einen klaren Entwurf;
- eine sichtbare Hauptaktion;
- einen Abbruchweg;
- verständliche Validierungsfehler;
- eine eindeutige Reaktion nach erfolgreichem Speichern.

Primäre Kompositionen:

- eigene Garnrolle auf die Karte setzen;
- Knoten knüpfen;
- passenden Faden erzeugen.

## URL-Adressierung

Die URL adressiert fachliche Absichten, nicht die flüchtige Kamera.

Unterstützte Zielrichtung:

```text
?focus=node:<id>
?focus=garnrolle:<id>
?lens=filter
?lens=search
?compose=node
?compose=garnrolle
?tab=<tab>
```

Priorität beim Einstieg:

```text
compose > focus > lens
```

`tab` wird erst wirksam, wenn das gewählte Panel ein adressierbares Tabmodell besitzt. Mittelpunkt, Zoom, Drehung und Neigung bleiben MapLibre-Zustand und werden nicht automatisch in die URL gespiegelt.

## Farbschema und visuelle Gewebesprache

Die Oberfläche bietet genau drei Darstellungspräferenzen:

- **System** als Standard; folgt der aktuellen Hell-/Dunkelpräferenz des Geräts und reagiert während der Sitzung auf Änderungen;
- **Hell** als bewusste helle Darstellung;
- **Dunkel** als bewusste dunkle Darstellung.

Die Auswahl liegt im zentralen Einstellungsmenü. Die Kartenleiste führt mit einem einzigen Einstieg dorthin, statt dort parallele Darstellungsschalter zu zeigen. Private Nachrichten bleiben davon getrennt als direkter Arbeitsweg in der Kartenleiste erreichbar; ungelesene Direktunterhaltungen können zusätzlich als aktuelle Aufmerksamkeit erscheinen. Die Farbschema-Auswahl wird ausschließlich lokal im Browser gespeichert; sie gehört weder zum Konto noch zur öffentlichen Garnrolle und wird nicht föderiert.

Farben und Flächen stammen aus gemeinsamen semantischen Darstellungstokens. Komponenten dürfen daher keine dunkle oder helle Grunddarstellung voraussetzen. Status, Auswahl, Warnung und Fehler müssen zusätzlich zu Farbe durch Text, Form, Umrandung oder Symbol verständlich bleiben.

Die Gewebesprache darf Zusammengehörigkeit durch zurückhaltende Fadenlinien, Schichtungen und weiche Übergänge andeuten. Wiederholte Streifenmuster auf Seiten-, Menü- oder Inhaltsflächen sind nicht Teil dieser Sprache. Dekoration darf die Lesbarkeit, den Karteninhalt, den Fokus oder die Bedienziele nicht überlagern. Dekorative Bewegung ist nicht erforderlich und entfällt bei reduzierter Bewegung.

## Mobile-First und Zugänglichkeit

Pflichtregeln:

- alle Kernhandlungen per Tastatur erreichbar;
- sichtbarer Fokus;
- Escape schließt den vordersten relevanten Raum;
- Fokus kehrt nach dem Schließen möglichst zum Auslöser zurück;
- Tabs unterstützen Pfeiltasten, Pos1 und Ende;
- Schaltflächen besitzen verständliche Namen;
- Touchziele sind ausreichend groß;
- Formulare verwenden klare Labels und Fehlermeldungen;
- Animationen berücksichtigen reduzierte Bewegung.

## Sprache

Die Hauptführung verwendet Produktbegriffe. Technische Namen bleiben in Diagnose- oder Entwicklungsflächen.

## Nicht erlaubt

- mehrere konkurrierende Detailflächen;
- Finden oder Karteninhalt als globale Hauptzustände;
- Aufmerksamkeitsblasen als persistente zweite Kopie von Fachzuständen;
- stille API-Fallbacks, die Fehler als Leere darstellen;
- technische Feldnamen in der Nutzerführung;
- ein Kompositionsformular ohne eindeutigen Abbruch- und Erfolgsweg.
