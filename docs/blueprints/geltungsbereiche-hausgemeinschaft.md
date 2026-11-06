---
id: blueprints.geltungsbereiche-hausgemeinschaft
title: "Geltungsbereiche und Hausgemeinschaft – universeller Umsetzungsplan"
doc_type: blueprint
status: deferred
created: 2026-11-06
lang: de
summary: >-
  Stufenplan für Sichtbarkeit, Adressierung, Mitwirkung und Benachrichtigungen
  auf bestehenden Garnrollen, Knoten, Fäden und Webungsaktionen; die
  Hausgemeinschaft dient als erster konkreter Geltungsbereich.
relations:
  - type: relates_to
    target: docs/specs/garnrolle-knoten-faden.md
  - type: relates_to
    target: docs/specs/privacy-api.md
  - type: relates_to
    target: docs/specs/private-nachrichten.md
  - type: relates_to
    target: docs/specs/ortsweberei-webgemeindezentrum.md
  - type: relates_to
    target: docs/specs/map-experience.md
  - type: relates_to
    target: docs/roadmap.md
---

# Geltungsbereiche und Hausgemeinschaft

**Stand:** 6. November 2026. **Art:** Ausführungsplan, keine Behauptung bereits
implementierter Funktionen und noch keine Änderung kanonischer Produktverträge.
**Umsetzung:** Noch nicht aktiv beauftragt. Die Roadmap liegt zur späteren
Aktivierung vor; es gibt bislang keine registrierte Bureau-Ausführungsaufgabe.
Ein aufgenommener Kandidat ersetzt keine solche Aufgabe.

## 1. Auftrag und Erfolgskriterium

Die Hausgemeinschaft ist **keine weitere Objektsorte, Karte, Gruppenplattform
oder Ressourcenverwaltung**. Commonthing besitzt bereits Garnrollen, Knoten,
Fäden, Webungsaktionen, Gespräche, Karte und Push-Infrastruktur. Sie bleiben
die einzige fachliche Darstellungs- und Handlungswelt.

Wir ergänzen **Geltungsbereiche**: Wer darf eine vorhandene Information
sehen? Wen betrifft sie? Wer darf mitwirken? Wer wird aktiv benachrichtigt?
„Nur Hausgemeinschaft“ ist der erste konkrete, mitgliedschaftsbasierte
Geltungsbereich; dieselbe Mechanik muss später auch für andere Gemeinschaften,
Straßen, Quartiere und öffentlich adressierte räumliche Umgebungen taugen.

**Abnahme am Nutzerweg:** Zwei freiwillig verbundene Garnrollen im gleichen
Haus können ein bereits vorhandenes Gut oder einen Knoten nur untereinander
sichtbar machen und gezielt adressieren. Ein dritter Account sieht weder Inhalt
noch verräterische Metadaten; reguläre öffentliche Knoten bleiben unverändert.
Danach kann derselbe Knoten ohne Kopie ausdrücklich für einen größeren Kreis
freigegeben werden. Push erfolgt nur bei bestehender Empfänger- und
Gerätefreigabe. Das Ganze funktioniert auf der bestehenden Karte und in
Safari auf dem iPad.

## 2. Vorhandene Wahrheit und Abgrenzung

| Bestehender Vertrag/Pfad | Wiederverwenden, nicht ersetzen |
|---|---|
| `docs/specs/garnrolle-knoten-faden.md` | Ein Account = eine Garnrolle; Knoten, Urheberbindung, Gespräch, Webungsaktion, Fäden. Dort sind `search_visibility` = `public`/`private`/`hidden`/`revoked` und serverseitige Lesegrenzen beschrieben (teilweise ausdrücklich als Änderungsentwurf). |
| `docs/specs/privacy-api.md` | `map_state`/`public_pos` regeln **Kartenverortung**, nicht die Zugriffsberechtigung auf Güter oder Hausangelegenheiten. |
| `docs/specs/private-nachrichten.md` und `apps/api/src/notifications.rs` | Web Push existiert **für private Direktnachrichten**, mit separater Konto-/Gerätefreigabe, Outbox, neutralen Push-Inhalten und idempotenter Zustellung. Kein allgemeiner Ereignis-Push ist damit belegt. |
| `docs/specs/ortsweberei-webgemeindezentrum.md` | Ortsweberei/Gewebezelle/Webgemeindezentrum haben bereits eigene Zuständigkeit. Eine Hausgemeinschaft ist **keine neue Ortsweberei oder Gewebezelle**. |
| `docs/specs/map-experience.md` | Bestehende Karte, Filter, Fokuspanel und mobile Interaktionen erhalten. |

Der Ist-Zustand jedes API-/DB-/UI-Pfades ist in Phase 0 gegen `main` **und
produktive Runtime** frisch zu prüfen. Spezifikationen oder grüne CI allein
beweisen keine aktuelle Auslieferung.

**Ausdrückliche Nicht-Ziele:** eigene Haus-Karte, Haus-Feed, Haus-Chat,
Haus-Knotenarten, zweite Ressourcenbank, zweite Garnrolle pro Mitglied,
automatische kollektive Garnrolle, zusätzliche Verwaltungsoberfläche,
automatische Veröffentlichung bei ausbleibender Antwort, eigener
Push-Dienst oder Zahlungsverkehr.

## 3. Vier voneinander unabhängige Entscheidungen

1. **Sichtbarkeit / Leserecht:** Wer kann den jeweiligen Inhalt über alle
   normalen Zugriffe überhaupt lesen? Vorschlag für die Bedienung: `Nur ich`,
   `Nur ausgewählte Gemeinschaft(en)` (zunächst Hausgemeinschaft), `Öffentlich`.
2. **Adressierung / Reichweite:** Welchen Kreis soll eine Webungsaktion
   besonders erreichen? Eine öffentliche Ressource kann nur die
   Hausgemeinschaft ansprechen; eine hausinterne Ressource darf keinen
   außerhalb berechtigten Empfänger erhalten.
3. **Mitwirkung:** Wer darf auf das Angebot/den Bedarf reagieren, einem
   Vorhaben beitreten oder Inhalte bearbeiten? Lesen, Reagieren, Bearbeiten,
   Moderieren und formale Weberrechte bleiben getrennt. Mitgliedschaft
   vergibt **niemals** automatisch fremde Schreib-/Weberrechte.
4. **Benachrichtigung / Abonnement:** Soll ein zulässiges, bestätigtes
   Ereignis überhaupt aktiv zugestellt werden? Nur an aktuell
   leseberechtigte Abonnenten mit wirksamer Konto- **und** Gerätefreigabe;
   In-App-Hinweise dürfen unabhängig von Browser-Push existieren.

Die effektive Empfängermenge lautet sinngemäß:

`Adressierte ∩ aktuell Leseberechtigte ∩ passende Abonnenten ∩ freigegebene Geräte`.

Ein größerer Adressierungskreis hebt Leserechte **nicht** auf. Änderungen der
Sichtbarkeit erfordern eine ausdrückliche Aktion des verantwortlichen Nutzers.

### Bestehende Zustände erhalten

`map_state` = `not_on_map`/`exact`/`radius` bleibt ausschließlich
Kartenverortung. `hidden` und `revoked` werden durch neue Geltungsbereiche
niemals stillschweigend wieder lesbar. Ein bereits privater Knoten wird
**nicht** beim Upgrade automatisch öffentlich oder hausintern.
Bestehende öffentliche Inhalte bleiben ohne Opt-in öffentlich.

Für die Umsetzung eine **einheitliche effektive Zugriffspolicy** spezifizieren,
die vorhandene `search_visibility`-Fälle und optionale Freigaben an
Gemeinschaften verarbeitet. Ein möglicher Zustand `restricted` mit einem oder
mehreren expliziten Kreis-Grants ist ein Entwurfsansatz, **kein vorab
freigegebenes Datenbankschema**. Phase 0 entscheidet anhand bestehender
Tabellen/Queries, ob ein kleinerer Zusatz zur vorhandenen Policy genügt.

### Inhalte unterschiedlich feingranular behandeln

Bei Knoten ist die kleinste Einheit zunächst **der Knoten** samt seinen
ablaufenden Fadenprojektionen und dem Knotengespräch. Bei Angaben auf einer
Garnrolle, insbesondere Gütern, Fähigkeiten und Plänen, darf die Wahl für
**eine einzelne Ressource** nicht versehentlich das gesamte Profil freigeben
oder verstecken. Vor einem Profil-Feld-Granularitätsschnitt die aktuelle
Account-API/DB prüfen; gegebenenfalls vorhandene verknüpfte Knoten verwenden,
ohne Ressourcen zu duplizieren oder Nutzer zu einer Migration zu zwingen.
Einen großen Profil-Refactor nicht als Voraussetzung für den ersten Knoten-MVP
einführen.

## 4. Hausgemeinschaft als erster Kreis

- **Entdeckung:** Treffen mehrere exakt veröffentlichte Garnrollen an
  einem Kartenpunkt zusammen, kann Commonthing die gemeinsame
  Hausgemeinschaft **anbieten**. Der bestehende Kartenstapel bleibt die
  Visualisierung. Auch Personen mit ungenauer/verborgener Position können
  über eine Einladung beitreten; ihre Privatposition wird nicht offengelegt.
- **Mitgliedschaft:** Gleiche Koordinate, gleicher Kartenpixel, Adresse,
  IP oder Wohnortbehauptung **verleihen niemals automatisch Leserechte**.
  Ein freiwilliger Beitritt benötigt eine gegenseitige Bestätigung.
  MVP-Vorschlag: Ein Account initiiert; ein zweiter stimmt zu und
  aktiviert so den Kreis. Weitere Beitritte brauchen Einladung und
  Annahme. Kein amtlicher Wohnsitznachweis für die Erstversion.
- **Stabile Identität:** Der Kreis hat intern eine stabile Kennung,
  Mitgliedschaften haben Lebenszyklus (eingeladen, aktiv, ausgetreten/
  widerrufen). Er hängt nicht dauerhaft an GPS-Koordinaten; Umzug,
  Änderung von `map_state` oder Karten-Zoom zerstören weder Mitgliedschaft
  noch bereits angelegte Knoten.
- **Austritt:** Der eigene Austritt ist möglich, widerruft Leserechte
  sofort und entfernt keine fremden Inhalte. Alte bereits zugestellte
  Push-Hinweise sind nicht rückholbar, dürfen aber beim Öffnen keine
  früheren Rechte gewähren.
- **Keine Parallelhierarchie:** Der Kreis dient zunächst als
  **Berechtigungs-/Adressierungssubjekt**, nicht als weitere Ortsweberei,
  Governance-Instanz oder personifizierte Garnrolle. Erweiterte
  Mitgliedschaftsverwaltung, Ausschlusskonflikte und formale Beschlüsse
  erst mit nachgewiesenem Bedarf spezifizieren.

Die zugrunde liegende, wiederverwendbare Abstraktion lautet **Kreis
(Geltungsbereich)**. `Hausgemeinschaft` ist eine erste Art dieses Kreises,
keine Sonderberechtigung im Karten-Rendering. Andere Kreise müssen sich
später ohne neue Node- oder Edge-Klassen ergänzen lassen.

## 5. Flächen, die dieselbe Policy verwenden müssen

Die **serverseitige** Berechtigungsentscheidung gilt für:

- einzelne Knoten und Garnrollen-Teilprojektionen; Listen, Karte/BBOX,
  Detailansicht, `/commons`, Filter, Autocomplete, semantische Suche;
- Fäden, Beziehungen und Aktivitätschronik (auch aggregierte
  Zähler, Titel, Snippets, IDs und Knotennamen);
- Knotengespräche, einzelne Beiträge, archivierte Gespräche,
  Direktlinks, Export und gegebenenfalls föderierte Projektionen;
- Caches, Suchindex, ETags, CDN-Cache-Schlüssel und serverseitige
  Vorberechnung: keine gemeinsame öffentliche Cache-Antwort mit
  hausinternen Daten; Authentifizierungswechsel muss sichtbare
  Ergebnisse aktualisieren;
- Benachrichtigungsplanung, tatsächliche Push-Auslieferung und
  Öffnen des Ziel-Links: Rechte zum relevanten Zeitpunkt erneut prüfen.

Unerlaubte Einzelzugriffe verhalten sich wie nicht existente Inhalte
(entsprechend bestehendem API-Vertrag: 404); Filterung erfolgt **vor**
Paginierung, Zählung, Ranking und Kartenprojektion. Admin-/Weberstatus
hebt private Leserechte nicht implizit auf. Bereits dauerhaft öffentlich
abgelegte Fremd-Caches oder Push-Empfänger können nicht zuverlässig
„zurückgeholt“ werden; die Oberfläche darf keine falsche
Rückwirkungszusage machen.

## 6. Vorgehen in kleinen, produktiv prüfbaren Schritten

### P0 – Bestandsaufnahme und minimaler Vertrag

#### Arbeit

1. Tatsächliche DB-Migrationen, `search_visibility`, Konto-Profile,
   Knoten-Lesewege, Push-Opt-ins und Outbox auf `main` und Runtime
   inventarisieren. Kennzeichnen: bereits produktiv / nur spezifiziert.
2. Ein einheitliches Zugriffsprädikat mit den bestehenden Zuständen
   festlegen; Entscheidung dokumentieren, ob vorhandene Strukturen für
   Kreis-Grants genügen. Keine neuen Datenkopien.
3. Erste Interaktionsmatrix für `Nur ich`/`Hausgemeinschaft`/`Öffentlich`,
   private/hidden/revoked, Gast/Weber, Nichtmitglied/Mitglied und
   Konto-/Geräte-Pushfreigabe als ausführbare Tests spezifizieren.

**Fertig, wenn:** API- und DB-Fundstellen bekannt sind, Migrations-
und Rückfallplan feststehen, keine bestehende Norm durch eine
implizite zweite Privacy-Definition verletzt wird.

### P1 – Freiwillige Hausmitgliedschaft, ohne neue Darstellungswelt

#### Arbeit

1. Stabile Kreis-ID und minimale Mitgliedschaft mit serverseitiger
   Bestätigung/Austritt und eigenen Lese-/Schreibgrenzen anlegen.
2. Hauszuordnung anhand identischer öffentlicher Position **nur
   vorschlagen**, manuell annehmen/bestätigen; ohne öffentliche Position
   explizite Einladung zulassen.
3. Innerhalb des vorhandenen Garnrollen-/Karten-Fokus anzeigen:
   „Hausgemeinschaft beitreten/verlassen“ und wer dazugehört, nur
   soweit die Beteiligten dies für den jeweiligen Betrachter freigeben.

**Fertig, wenn:** Zwei Testkonten verbinden sich ausdrücklich; ein
drittes Konto mit identischen Koordinaten erhält keinerlei
Mitgliedsrechte; Position verschieben und neu laden bewahren den
korrekten, unabhängig gepflegten Mitgliedschaftszustand.

### P2 – „Nur Hausgemeinschaft“ an vorhandenen Knoten

#### Arbeit

1. Sichtbarkeit in bestehender Knotenkomposition/-bearbeitung
   anbieten; bestehende `public`-, `private`-, `hidden`- und
   `revoked`-Semantik kompatibel halten. Kreis-Grants sind genau
   einem vorhandenen Knoten zugeordnet; keine zweite Haus-Knotenliste.
2. Gemeinsame API-Policy auf **alle** in Abschnitt 5 genannten
   Lesewege anwenden. Verwandte Gespräche, Fäden und Aktivität
   dürfen nur entsprechend ihrem Ursprung erscheinen.
3. Bei Sichtbarkeitswechsel Rechte und Caches korrekt invalidieren.
   Schreibrechte bleiben beim bisherigen Besitzer/zulässigen Weberpfad;
   gemeinschaftliche Bearbeitung ist **ein eigener späterer** Vertrag.

**Fertig, wenn:** Mitglied kann den Knoten über Karte, Suche,
Detail-Link und Gespräch lesen; Nichtmitglied/abgemeldeter Browser
bekommt keine ID, keinen Titel, keine Zählung oder Gesprächsdaten.
Nach Austritt verschwindet der Knoten auch aus bereits geladenen
Such-/Kartenergebnissen. Bestehende öffentliche Inhalte und das
Knotenweben funktionieren unverändert.

### P2b – Sichtbarkeit einzelner Garnrollen-Ressourcen

#### Arbeit

1. Prüfen, ob Güter, Fähigkeiten und Pläne heute als separat
   verknüpfte Knoten oder als Felder/Listen innerhalb eines
   Garnrollenprofils erscheinen. Keine neue Darstellungslogik.
2. Einzelne vorhandene Angebote/Bedarfe mit derselben Policy wie in
   P2 freigeben; die übrige Garnrolle und ihre öffentlichen Merkmale
   behalten ihre Sichtbarkeit. Erforderliche granulare Persistenz
   gezielt und abwärtskompatibel ergänzen.
3. Über alle Account-API-, Such-, Karten-, Profil- und
   Aktivitätsprojektionen prüfen, dass hausinterne Güter nicht
   versehentlich als öffentliche Profilfelder oder Such-Tags erscheinen.

**Fertig, wenn:** Die eigene Garnrolle kann beispielsweise
„Fähigkeit: Fahrrad reparieren“ öffentlich und „Meine Leiter“
hausintern zeigen, ohne Daten zu duplizieren, die übrige
Garnrolle zu verstecken oder einen neuen Ressourceneditor zu erfinden.

### P3 – Adressierung und freiwillige Hinweise

#### Arbeit

1. Webungsereignis darf gezielt an einen Kreis adressiert werden,
   ohne Knoten zu kopieren. Adressierung erweitert niemals Leserechte.
2. Erste In-App-Benachrichtigung/Beobachtung für neue, **lesbare**
   Haus-Ereignisse mit abonnierbarer Ereignisart; keine Pflichtreaktion.
3. Erst anschließend optional denselben vorhandenen Web-Push-Kanal
   erweitern: Konto-Opt-in, pro Gerät Browserfreigabe, Outbox,
   idempotente Belege, begrenzte Retries und **neutraler Payload**
   (keine privaten Titel, Namen, Adressen oder Texte).
4. Rechte und Abonnement beim Erzeugen **und** Zustellen prüfen;
   unterdrückte/abgemeldete Empfänger nicht später nachbeliefern.
   Geräteausschaltung/Entzug bricht ausstehende Aufträge ab.

**Fertig, wenn:** Nur abonnierte Mitglieder bekommen einen zulässigen
Hinweis; Nichtmitglieder, ausgeschiedene Mitglieder und deaktivierte
Geräte nicht. Browser-Push-Ausfall lässt Knoten und In-App-Zugriff
unbeeinträchtigt. Bestehende Direktnachrichten-Pushfunktion bleibt
regressionsfrei.

### P4 – Gleiche Reichweite auf großer Karte

#### Arbeit

1. Das Kreis-Prädikat für weitere freiwillige Gemeinschaften
   wiederverwenden; keine `if (household)`-Sonderarchitektur.
2. **Räumliche Adressierung** für bereits öffentlich sichtbare
   Webungsaktionen ergänzen: bewusst gewählter Umkreis/Quartier,
   ohne allein aus GPS/Adresse neue **Leserechte** abzuleiten.
3. Themen- und Knoten-Abonnements, optionale zeitliche
   Hervorhebung und Ausweitung eines Bedarfs in größere Kreise
   ermöglichen. Ausweitung nur ausdrücklich; gleicher Knoten,
   dieselbe ID und Chronik.

**Fertig, wenn:** Dieselbe Bedien- und Berechtigungskomponente
Hausgemeinschaft, einen anderen Kreis und öffentliches Umkreis-
Targeting bedient; weder Kopien noch private Daten in öffentlichen
Karten-/Such-/Pushprojektionen entstehen.

### Später, nur bei konkreter Nachfrage

Verbindliche Gruppenbeschlüsse, gemeinsame Konten, Reservierungen
und kollektive Garnrollen. Diese Optionen **dürfen P1–P3 nicht
blockieren**; sie verwenden dieselben Regeln, wenn sie kommen.

## 7. Ende-zu-Ende-Abnahme und Regressionen

Testmatrix in produktionsnaher PostgreSQL/API-Umgebung und im
tatsächlichen Browserpfad; mindestens:

1. Accounts A und B am selben Punkt, C am selben Punkt und D
   außerhalb: A/B treten ausdrücklich bei, C/D bleiben ohne
   Leserecht. Bloßer Kartenstapel bewirkt nichts.
2. A erzeugt **einen** privaten Haus-Knoten; B sieht ihn in Karte,
   Suche, Detail und Gespräch, C/D/Gast nicht. Versuche mit
   bekannter ID, Cursor, BBOX, Fäden, Aktivität und Suchindex
   verraten keine Metadaten.
3. B verlässt Kreis: Leserecht entfällt sofort; C wird später
   eingeladen und erhält es erst nach Bestätigung. Standortwechsel,
   `radius` und `not_on_map` ändern Mitgliedschaft nicht.
4. A erweitert einen hausinternen Knoten **ausdrücklich** auf
   öffentlich: gleiche Knoten-ID, keine Dublette. Rückstellung
   blockiert erneute Zugriffe auch nach Cache-/Sessionwechsel.
5. `hidden`/`revoked` bleiben geschlossen; unveränderte Altobjekte
   behalten ihre ursprüngliche Sichtbarkeit, private Altobjekte
   werden nicht migriert/offengelegt.
6. Benachrichtigung nur nach Opt-in und Rechteprüfung, kein
   sensibler Payload, kein Dubletten-Push bei Retry. Gerät aus,
   Mitglied aus, Push-Provider down: keine unerlaubte Zustellung,
   keine verlorene kanonische Webungsaktion.
7. Safari auf physischem iPad (Mitgliedschaft, Bearbeitung,
   Sichtbarkeit, Push-Freigabe und Seitenneuladen) sowie
   repräsentativer Desktop- und Tastaturfluss. Automatischer
   Touch-Browser-Test ersetzt diese Abnahme **nicht**.

**Release:** zuerst auf isolierter Vorschau mit zwei ausdrücklich
dafür vorgesehenen Testkonten, dann kontrollierter Rollout;
Migrationen vor Aktivierung und kompatible API-Rückfallstrategie.
Keine Produktion als „fertig“ melden, solange der echte
authentifizierte Nutzerweg und Leserechte in laufender Version
nicht nachgewiesen sind. Bereits gespeicherte private Inhalte
bei Rückfall niemals öffentlich projizieren.

## 8. Reihenfolge, Schnittgrenzen und Definition of Done

| Schritt | Ergebnisartefakt | Unabhängig nutzbar? |
|---|---|---|
| P0 | Bestandsmatrix, autoritativer Sichtbarkeitsvertrag, negative API-Tests | Ja: sichere Implementierungsgrundlage |
| P1 | Bestätigte stabile Hausmitgliedschaft am bestehenden Kartenstapel | Ja |
| P2 | Ein vorhandener Knoten mit „Nur Hausgemeinschaft“ auf **allen** Lesewegen | Ja: erster konkreter Nutzwert |
| P2b | Einzelne bestehende Garnrollen-Ressourcen mit demselben Leserecht | Ja |
| P3 | Abonnement + zulässige In-App-/optionale Push-Hinweise | Ja |
| P4 | Dieselbe Policy für weitere Kreise und öffentliche räumliche Adressierung | Ja |

Pro Schnitt: kleinster kohärenter Diff, DB/API/UI-Tests und negative
Berechtigungstests, tatsächliche Deploy-Revisionsidentität und manueller
Nutzerweg. Kein künstlicher PR-Stack, wenn ein kleiner Schnitt bereits
abgenommen werden kann. P2b nutzt dieselbe Policy und gehört zum gewünschten Ressourcenfall.
Erst nach sichtbarem Nutzen von P2/P2b die Benachrichtigungs- und
Reichweitenphase priorisieren.

**Kanonisierung:** Dieses Papier ist bewusst ein **Blueprint**. Bevor
ein neuer Berechtigungszustand produktiv eingeführt wird, den
kanonischen Vertrag `docs/specs/garnrolle-knoten-faden.md` und die
betroffenen Datenschutz-/Benachrichtigungsverträge gezielt fortschreiben.
Ein Plan allein erteilt keine Schreib-, Admin- oder Wohnsitzrechte.
