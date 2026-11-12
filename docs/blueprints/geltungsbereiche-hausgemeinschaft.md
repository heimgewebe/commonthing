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

**Stand:** 12. November 2026. **Art:** Aktiver Umsetzungsplan mit getrennten
Produktivabnahmen; dieses Dokument ist kein Laufzeitbeleg.

**Bereits in main:** Grundvertrag P0 (#2065), freiwillige Mitgliedschaft P1
(#2069), Kreisfreigabe an Knoten P2 (#2073), neue Einzelressourcen (#2078)
und die Überführung vorhandener `good:`-/`skill:`-Tags P2b (#2083).
Die zusätzlichen PostgreSQL- und HTTP-Mehrrollenregressionen #2089/#2090
sowie die idempotente 503-Behandlung #2103 wurden ebenfalls integriert.
Der zuvor in diesem Plan als *nicht integriert* bezeichnete P2b-Entwurf
ist daher historisch überholt. **Weiter offen:** #2081 und #1970 für
authentifizierte Produktions- und reale Safari-Nutzerwege; die PRs #2108
(Kartenüberlagerung) und #2109 (Push-Fehler) sind am 12.11. noch offen.
Die hier neu vereinbarten persönlichen Hausgemeinschafts-Ereignisnachrichten
sind **Plan**, nicht bereits implementierte Postfach-Funktion.

**Unmittelbare Prioritäten:** (1) Mitgliedschaftszuwachs und Rücknahme
bestehender Freigaben nachvollziehbar regeln; (2) Einladungen, Annahmen,
Ablehnungen und weitere direkt betreffende Vorgänge als persönliche
Nachrichten im bestehenden Postfach sichtbar machen; (3) Mehrkonten- und
iPad-Safari-Abnahme; (4) Kartenbedienbarkeit und Push-Regressionen.
P4 (weitere Geltungsbereiche/räumliche Adressierung) bleibt danach.
Der frühere Bureau-Kandidat ist nicht publiziert und keine registrierte
Aufgabe.

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
| `docs/specs/private-nachrichten.md`, `/nachrichten`, `apps/web/src/lib/accountAttentionRuntime.ts` und `apps/api/src/notifications.rs` | Das vorhandene persönliche Postfach und die Topbar zählen ungelesene **1:1-Nachrichten**; die bestehenden Web-Push-Opt-ins, Outbox und neutralen Push-Inhalte betreffen Direktnachrichten. Hausgemeinschaftsereignisse sind dort heute **nicht** als kanonische persönliche Nachrichten angebunden. Bestehende Zustellung erweitern, nicht ersetzen. |
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
4. **Persönliche Nachricht / optionaler Push:** Direkt an eine Person
   gerichtete oder sie unmittelbar betreffende Vorgänge erzeugen eine
   **persistente persönliche Nachricht** im vorhandenen Postfach – auch ohne
   Push-Opt-in. Für allgemeine Ressourcen- und Themenaktivität ist ein
   freiwilliges Abonnement nötig. Betriebssystem-Push bleibt zusätzlich
   freiwillig und erfordert Konto- **und** Gerätefreigabe.

Empfängermengen unterscheiden sich bewusst: **persönliches Postfach** =
konkret betroffene, aktiv berechtigte Konten; **abonnierte Ereignisse** =
adressierte, aktuell leseberechtigte Konten mit passendem Abonnement;
**Push** = daraus freigegebene Konten und Geräte. Fehlendes Push-Abonnement
darf eine Einladung oder Ablehnung niemals im Postfach verstecken.

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

### Nachtrag: Aufnahme, Austritt und Begriffsgrenzen (12.11.2026)

**Aktuelle Berechtigungskonsequenz:** Nach Annahme einer Einladung wird ein
neues aktives Mitglied grundsätzlich auch zum Leser **bereits zuvor**
kreisfreigegebener Knoten. Ein beliebiges aktives Mitglied kann derzeit
einladen; die API erlaubt den eigenen Austritt, aber noch keinen geregelten
Widerruf einer fremden Mitgliedschaft. Diese Regel ist weder als
Einwilligung der betroffenen Ressourcenbesitzer noch als fertiger
Konfliktmechanismus zu behandeln.

**Vor Implementierung entscheiden und sichtbar machen:** Welche explizite
Zustimmung gilt für künftige Mitglieder und für bereits freigegebene
Inhalte? Minimale Lösung ohne zweite Verwaltung: beim Freigeben die
Reichweite für **künftige** Mitglieder unmissverständlich erklären,
Aufnahmen allen Betroffenen im Postfach anzeigen, ein begrenztes Verfahren
zum Widerspruch/Korrigieren einer fehlerhaften Aufnahme festlegen und
betroffenen Besitzern einen wirksamen Widerruf ihrer Knotenfreigaben
ermöglichen. Keine stillschweigende Ausweitung geschützter Bestandsinhalte.
Eine schwerfällige neue Haus-Governance ist kein Standardziel.

**Begriffe:** Der Kartenstapel exakt übereinanderliegender öffentlicher
Garnrollen heißt **„Garnrollen an diesem Ort“**, auch wenn niemand
einer Gemeinschaft beigetreten ist. **„Hausgemeinschaft“** bezeichnet
nur einen ausdrücklich bestätigten Kreis. Gleiche Koordinaten sind
niemals Mitglieds- oder Leserechtsbeweis.

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

#### Umsetzung P0

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

#### Umsetzung P1

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

### P1-Nachtrag – Persönliche Nachrichten für Mitgliedschaftsvorgänge

Die P1-Mitgliedschaft darf nicht nur als Status in der Garnrolle sichtbar sein.
Bereits **Gründungsvorschlag und Einladung** erscheinen beim Empfänger als
**ungelesene persönliche Nachricht** in `/nachrichten` und in der vorhandenen
Topbar-Aufmerksamkeitsanzeige. **Annehmen** und **Ablehnen** sind direkt aus
dieser Nachricht und weiterhin am bestehenden Garnrollenpanel möglich.

Annahme oder Ablehnung erzeugt nach bestätigter Zustandsänderung eine
persönliche Ergebnisnachricht an den Einladenden. Bei weiterer Aufnahme,
Austritt, Rücknahme einer Einladung oder Auflösung erhalten die tatsächlich
betroffenen bisherigen Mitglieder eine passende persönliche Mitteilung.
Kein Browser-Push, keine Benachrichtigungserlaubnis und kein Abonnement
sind nötig, **um die Nachricht im Postfach zu erhalten**; die externe
Push-Zustellung bleibt optional. Mehr zur Ereignismatrix und technischen
Realisierung in P3.

**Fertig, wenn:** A lädt B ein; B sieht eine ungelesene Nachricht in
Postfach und Topbar, kann daraus annehmen oder ablehnen; A sieht das
jeweilige Ergebnis als neue persönliche Nachricht. Nach Reload oder
Gerätewechsel bleiben Status und Gelesen-Zustand konsistent. Ein gescheiterter
Mitgliedschaftswechsel erzeugt keine falsche Erfolgsmeldung.

### P2 – „Nur Hausgemeinschaft“ an vorhandenen Knoten

#### Umsetzung P2

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

#### Umsetzung P2b

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

#### P2b: gespeicherten Profil-Tag umziehen (Issue #2081, umgesetzt; Abnahme offen)

**Aktueller Integrationsstand:** Der zuvor nur entworfene Umzug ist
mit PR #2083 in `main` integriert. #2089 und #2090 ergänzen reale
PostgreSQL- und HTTP-Cookie-Regressionsbelege, #2103 korrigiert die
idempotente Wiederholung nach unbekanntem 503-Ergebnis. Der nachfolgende
Mechanikabschnitt beschreibt daher **implementierten** Code, keine
ausstehende Änderung. **Offen:** nachgewiesener vollständiger
authentifizierter Mehrkonten-Nutzerweg auf der aktuell aktiven
Produktivversion und physischer iPad-Safari-Test; #2081 nicht voreilig
schließen.

- **API:** `POST /api/accounts/me/profile/resources` mit `tag` (exakt ein
  gespeicherter `good:`/`skill:`-Tag), `scope` (`private` = Nur ich,
  `circles` = nur bestätigte aktive Kreise, `public`), `circle_ids`,
  `operation_id`. Antwort `201` (neu) bzw. `200` (Replay) mit Knoten,
  Audience und dem neuen eigenen Profil; `Cache-Control: private, no-store`.
- **Atomar in PostgreSQL:** eine Transaktion entfernt genau diesen Tag aus
  `domain_accounts.public_payload`, legt den Knoten (`good:` → `resource`,
  `skill:` → `offer`, ohne Kartenort) mit seiner ersten dauerhaften
  Sichtbarkeit an, schreibt den Pflicht-Ursprungsfaden und die Kreis-Grants.
  Sperrreihenfolge: Account-Lifecycle-Lock → Accountzeile → Kreis-
  Zustimmungszeilen (vor jedem generationserhöhenden Schreiben) →
  Operation-Lock → Knoten-Mutation-Lock. Jeder Fehler rollt alles zurück;
  der Tag bleibt dann öffentlich.
- **Idempotenz:** Operation-IDs liegen in einem eigenen Namensraum. Ein
  Replay liefert denselben Knoten nur, wenn Tag, Art, Titel **und** aktuelle
  Audience exakt der Anfrage entsprechen und der Tag nicht wieder öffentlich
  ist; sonst `409`.
- **Kein Lost Update:** `PATCH /accounts/me/profile` akzeptiert
  `expected_tags`; weicht der gespeicherte Satz ab, folgt `409`. Ohne
  `expected_tags` verhindert ein privater Marker (`moved_profile_tags`,
  nie öffentlich projiziert), dass ein veralteter Client einen umgezogenen
  Wert erneut veröffentlicht. Der Marker wird nie still verdrängt; bei 256
  Einträgen schlägt ein weiterer Umzug geschlossen fehl.
- **JSONL:** nicht unterstützt (`503`), keine Teilschreibungen.
- **Oberfläche:** „Meine Garnrolle“ bietet nur gespeicherte `good:`/
  `skill:`-Tags an, Standard „Nur ich“, ein Hinweis verneint die
  Rückholbarkeit fremder Kopien. Ungespeicherte andere Eingaben bleiben
  erhalten; bei `409` nur ehrliche Meldung und Tag-Abgleich, kein
  automatisches erneutes Speichern.
- **Belege (bereits integriert und ausbaubar):** `apps/api/tests/db_profile_tag_resource.rs` gegen
  Wegwerf-PostgreSQL (Atomarität inkl. injiziertem Grant-Fehler, Replay,
  parallele Instanzen, Fremdzugriff, Marker-Kapazität, JSONL-`503`) sowie
  `apps/web/tests/garnrolle-move-resource.spec.ts` (gemockte API; kein
  Datenschutzbeweis).
- **Offen:** `/search` wird nur über dieselbe SQL-Lesepolicy, nicht über den
  vollständigen Suchpfad mit Embeddings geprüft; Massenmigration bleibt
  bewusst ausgeschlossen; Altbestand ohne `expected_tags` (ältere Bundles)
  ist nur über den Marker abgesichert; echte authentifizierte Mehrkonten-
  und iPad-Abnahme fehlen.

### P2-Nachtrag – Datenschutz, Endabnahme und Kartenbedienung

- **P0 Datenschutz:** #2081 (P2b) und #1970 anhand mehrerer *real*
  authentifizierter Produktionskonten inklusive ehemaligem Mitglied
  über Karte, BBOX, Suche, Direktlink, Gespräch, Garnrollenaktivität
  und Cache abnehmen. Die bereits gemergten Tests #2089/#2090 nicht
  wiederholen, sondern die fehlende echte End-to-End-Gegenprobe ergänzen.
- **P0 Mitgliedschaftsentscheidung:** vor Freigabe beliebiger neuer
  Mitglieder den oben beschriebenen Bestandszugriff und den wirksamen
  Rücknahmeweg anhand eines privaten Knotens A→B→C überprüfen.
- **P1 Karte:** #2108 zur Auswahl überlagerter, autorisierter Marker
  abschließen; Kartennamen „Garnrollen an diesem Ort“ gegenüber
  bestätigter Hausgemeinschaft trennen. Kein Verschieben echter
  geografischer Anker und keine neue Datenkopie.
- **P1 Push:** offenen Fix #2109 übernehmen und regressionsprüfen,
  damit ein fehlgeschlagener Aktivierungsversuch bestehende
  Geräte-Abonnements nicht löscht. Erst dann den Kanal erweitern.
- **P2 Skalierung:** #1471 anhand echter Browser- und API-Messungen
  weiter abnehmen; bekannte Teilbeweise nicht mit Vollabnahme für
  extrem viele gleichzeitig sichtbare Entitäten verwechseln.

### P3 – Persönliche Ereignisnachrichten im vorhandenen Postfach

**Verbindlicher Produktvertrag:** Persönlich adressierte oder unmittelbar
betreffende Ereignisse sind **persönliche Nachrichten** und erscheinen
**dauerhaft lesbar unter `/nachrichten`**, inklusive Ungelesen-Zähler,
Topbar-Briefsymbol und kontextbezogenem Direktlink. Die Nutzer müssen nicht
in den Einstellungen nach Einladungen suchen. Das Postfach ist die Wahrheit;
Push weckt nur auf und kann fehlen.

| Ereignis | Persönliche Empfänger | Darstellung / mögliche Aktion |
|---|---|---|
| Hausgemeinschaft vorgeschlagen / eingeladen | konkret Eingeladener | „Einladung zur Hausgemeinschaft“; **Annehmen / Ablehnen** |
| Einladung angenommen | Einladender; betroffene bisherige Mitglieder | „Beitritt bestätigt“; Kreis öffnen |
| Einladung abgelehnt | Einladender | „Einladung abgelehnt“; als erledigt markieren |
| Einladung zurückgezogen / gegenstandslos | Eingeladener und betroffene Initiatoren | Korrekturstatus; **keine** wirkungslose Beitrittstaste |
| Neues Mitglied bestätigt | bisherige betroffene Mitglieder | Personenänderung und Hinweis auf Reichweite bestehender Freigaben |
| Austritt, Ausschluss oder Auflösung | noch betroffene Mitglieder, relevante Besitzer | Änderung des Zugriffsstatus; Kreis/Freigaben prüfen |
| Eigene Kreisfreigabe geändert oder widerrufen | direkt betroffene, aktuell berechtigte Personen | neutrale persönliche Statusmitteilung; keine privaten Daten nach Widerruf |
| Direkte Antwort, Erwähnung, persönlich anvertraute Aufgabe oder Entscheidung | direkt adressierte/beteiligte Person | vorhandenen Knoten, Faden, Antrag oder Gespräch öffnen |
| Neues Angebot/Bedarf, Thema oder allgemeine Kreisaktivität | **nur freiwillige Abonnenten** mit aktuellem Leserecht | abonnierte persönliche Hinweismeldung ohne Antwortpflicht |

Die Aufzählung ist offen für spätere Ereignisarten, **nicht** für
wahlloses Senden jeder Kartenbewegung, Ansicht, Änderung oder
öffentlichen Aktivität. Die Regel lautet:
**Direkte Betroffenheit/Adressierung → persönliche Nachricht;
allgemeine Aktivität → Opt-in; rein technische Vorgänge → kein Postfach.**
Ablehnung darf nicht still verschwinden; zugleich dürfen Sender und
Empfänger nicht zu einem privaten Chat oder einer Antwort verpflichtet
werden.

#### Technische Umsetzung – kleinster kompatibler Schnitt

1. Bestehende Nachrichtendomäne `docs/specs/private-nachrichten.md`,
   `/nachrichten`, `accountAttentionRuntime.ts`, Topbar-Ungelesen-
   Projektion und transaktionale Outbox auf Erweiterbarkeit prüfen.
   **Keine zweite Nachrichten-App, kein Haus-Feed, keine parallele
   Wahrheitsdatenbank.** Der aktuelle `direct`-Vertrag ist auf zwei
   menschliche Teilnehmer begrenzt: Ereignisse daher als klar
   gekennzeichnete, **typisierte System-/Ereignisnachrichten** im selben
   persönlichen Postfach darstellen; kein vorgetäuschter menschlicher
   Absender und keine künstlich erzeugte 1:1-Konversation.
2. Fachlicher Ereigniscommit und Nachrichtenauftrag atomar per bestehender
   Outbox binden. Jede Kombination aus kanonischer Ereignis-ID, Empfänger
   und Ereignisart erzeugt höchstens **eine** Postfachnachricht.
   Einladungs-Retry, parallele Annahme/Ablehnung und wiederholte
   Verarbeitung dürfen weder doppelte Nachrichten noch widersprüchliche
   Aktionen erzeugen. Fehlende Postfachzustellung darf die fachliche
   Mitgliedschaftstransaktion nicht verfälschen; Rückstände müssen
   wiederauffindbar/nachlieferbar sein.
3. Persistenter, accountgebundener Gelesen-Zustand; ungelesen bis zum
   tatsächlichen Öffnen, keine automatische Lesebestätigung allein durch
   Refresh. Bei Konto- oder Rollenwechsel private Projektionsdaten
   verwerfen. Einladungs-Aktionsknöpfe rufen nur die vorhandene
   autorisierte API auf, lesen danach Status frisch und bleiben nach
   Ablehnung, Widerruf oder Annahme nicht fälschlich aktiv.
4. **Zugriff bei Erzeugung, Anzeige, Navigation und optionalem Push
   erneut prüfen.** Nach Austritt darf ein alter Nachrichtendirektlink
   keinen früheren Knoten mehr öffnen. Wo berechtigte Historie gezeigt
   werden darf, nur geeignete Status-/Akteurangaben; keine fremden
   Knotentitel, Bewohnerlisten, IDs oder Metadaten in verbotenen
   Projektionen. Servergesteuerte Systemnachrichten nicht durch
   Benutzer-Blockieren, menschliche Senderlimits oder Nutzer-Imitation
   erzeugen; Anti-Spam/Rate-Limits für Einladungsvorgänge separat
   und verhältnismäßig halten.
5. Bestehendes Briefsymbol zählt private Direktnachrichten **und**
   persönliche Ereignisnachrichten genau einmal, ohne Doppelanzeige oder
   Wechsel der Prioritätslogik. Eine unerledigte Einladung wird als
   **Handlung möglich**, die Ablehnung/Annahme als **neue Information**
   angezeigt, nicht als Antwortpflicht.
6. Optionaler Web Push benutzt erst nach bewusster Konto- **und**
   Gerätefreigabe den bisherigen Kanal mit neutralem Inhalt,
   idempotenten Zustellbelegen und erneuter Autorisierung. Fehlgeschlagene
   oder ausgeschaltete Push-Abonnements beeinflussen weder Postfach
   noch den gespeicherten Vorgang. Vorher #2109 integrieren.

**Fertig, wenn:** Einladung, Ablehnung, Annahme, Rücknahme, Eintritt
und Austritt revisionsgebunden als echte, ungelesene persönliche
Postfachnachrichten mit funktionierenden Aktionen und Topbar-Status
geprüft sind; andere Konten sehen sie nicht. Zusätzlich erhält ein
berechtigtes Mitglied eine abonnierte Ressourcenmeldung, während
Nichtmitglied, ehemaliges Mitglied, nicht abonnierter Empfänger und
ausgeschaltetes Gerät keine unzulässigen Hinweise bekommen.
Vollständige PostgreSQL/API- und Browserregression einschließlich
echter Mehrkonten-Produktion und physischem iPad-Safari; Push-Ausfall
lässt das Postfach funktionsfähig.

### P4 – Gleiche Reichweite auf großer Karte

#### Umsetzung P4

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
6. A lädt B ein: die Einladung erscheint sofort als **persönliche,
   ungelesene Nachricht** in `/nachrichten` und der Topbar.
   B nimmt aus der Nachricht an oder lehnt ab; A bekommt jeweils
   genau eine verständliche persönliche Ergebnisnachricht. Ein
   Wiederholen erzeugt keine Dublette. Rücknahme und Austritt
   korrigieren Einladungsstatus und Direktlinks.
7. C wird später aufgenommen: vorhandene Mitglieder sehen die
   Aufnahme als persönliche Nachricht. Explizit prüfen, welche
   bisherigen Hausinhalte C nach vereinbarter Bestandsfreigabe
   lesen darf. Ein fehlerhafter Beitritt muss wirksam korrigierbar sein.
8. Persönliche Nachrichten sind **ohne Push-Opt-in** abrufbar,
   abonniertes allgemeines Ressourcengeschehen dagegen nur nach
   Opt-in und aktueller Rechteprüfung; kein sensibler Push-Payload,
   kein Dubletten-Push bei Retry. Gerät aus, Mitglied aus,
   Push-Provider down: keine unerlaubte Zustellung,
   keine verlorene kanonische Webungsaktion.
9. Safari auf physischem iPad (Mitgliedschaft, Postfach, Ablehnung,
   Bearbeitung, Sichtbarkeit, Push-Freigabe und Seitenneuladen) sowie
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
| P1-Nachtrag | Einladungen/Annahmen/Ablehnungen als persönliche Nachrichten im vorhandenen Postfach | Ja: unmittelbarer sozialer Nutzerweg |
| P2-Nachtrag | Aufnahme- und Bestandsfreigaberegel, authentifizierte Mehrkontenabnahme, Karten-UX | Ja: kontrollierbarer echter Nutzerweg |
| P3 | Weitere personenbezogene Ereignisse, abonnierte Ressourcenhinweise und optionaler Push | Ja |
| P4 | Dieselbe Policy für weitere Kreise und öffentliche räumliche Adressierung | Ja |

Pro Schnitt: kleinster kohärenter Diff, DB/API/UI-Tests und negative
Berechtigungstests, tatsächliche Deploy-Revisionsidentität und manueller
Nutzerweg. Kein künstlicher PR-Stack, wenn ein kleiner Schnitt bereits
abgenommen werden kann. P2b nutzt dieselbe Policy und gehört zum gewünschten Ressourcenfall.
Persönliche Nachrichten über Einladungen, Ablehnungen und Annahmen
sind **unmittelbarer Bestandteil der Hausgemeinschaft**, nicht ein
optionales spätes Push-Feature. Größere Reichweite, abonnierbare
Aktivitätsströme und zusätzlicher Push folgen erst danach.

**Kanonisierung:** Dieses Papier ist bewusst ein **Blueprint**. Bevor
ein neuer Berechtigungszustand produktiv eingeführt wird, den
kanonischen Vertrag `docs/specs/garnrolle-knoten-faden.md` und die
betroffenen Datenschutz-/Benachrichtigungsverträge gezielt fortschreiben.
Ein Plan allein erteilt keine Schreib-, Admin- oder Wohnsitzrechte.