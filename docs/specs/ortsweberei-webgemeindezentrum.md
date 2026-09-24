---
id: specs.ortsweberei-webgemeindezentrum
title: Ortsweberei und Webgemeindezentrum
summary: Kanonischer Produktvertrag für die lokale Gemeinschaft als Gewebezelle und ihr genau einmal vorhandenes, bewusst verortetes Webgemeindezentrum.
doc_type: specification
status: canonical
canonicality: normative
lifecycle_state: active
role: norm
organ: product-domain
owner: product-domain
last_reviewed: 2026-08-08
review_after: 2026-11-02
depends_on:
  - specs.garnrolle-knoten-faden
  - specs.governance-antraege
relations:
  - type: relates_to
    target: docs/adr/ADR-0011__foederierte-gewebezellen.md
  - type: relates_to
    target: docs/adr/ADR-0015__ortsweberei-gewebezelle-webgemeindezentrum.md
  - type: relates_to
    target: docs/specs/map-experience.md
verifies_with:
  - apps/api/migrations/20260802000001_ortsweberei_webgemeindezentrum.up.sql
  - apps/api/src/routes/webgemeindezentren.rs
  - apps/api/tests/db_ortsweberei_webgemeindezentrum.rs
  - apps/web/src/lib/map/scene.ts
  - apps/web/src/lib/components/panels/WebgemeindezentrumPanel.svelte
  - apps/web/tests/webgemeindezentrum-hammer-park.spec.ts
attention_source_status: none
attention_source_rationale: "Ordnet lokale Gemeinschaft und Webrat; persönliche Antrags- und Beteiligungsfakten bleiben kanonisch beim Governance-Vertrag."
---

# Ortsweberei und Webgemeindezentrum

## Grundsatz

Eine Ortsweberei ist die lokale Gemeinschaft von commonThing. Sie bildet
logisch genau eine Gewebezelle und besitzt genau ein aktives
Webgemeindezentrum. Das Zentrum verbindet die digitale Ortsweberei mit einem
bewusst gewählten Ort auf der Karte, an dem sich die Gemeinschaft treffen kann
oder künftig treffen möchte.

Diese Eins-zu-eins-Regeln sind Fachregeln, keine Behauptung über Serverzahlen:
Ein gemeinsamer Betreiber darf mehrere voneinander getrennte Gewebezellen
hosten. Umgekehrt bleibt die Identität einer Ortsweberei stabil, wenn ihre Zelle
später den Betreiber wechselt.

## Verantwortungen

| Begriff | Verantwortung |
|---|---|
| Ortsweberei | Gemeinschaft, Mitgliedschaft, Regeln, Entscheidungen und gemeinsame Mittel |
| Gewebezelle | Primärwahrheit, Daten- und Governance-Grenze, Föderationsidentität und Betrieb |
| Webgemeindezentrum | Kartenanker, öffentlicher Zugang, Treffort und Einstieg in die lokale Selbstverwaltung |

Webrat, Gewebekonto, örtlicher Gesprächsraum und lokale Vorhaben gehören zur
Ortsweberei. Sie sind über das Zentrum erreichbar, aber nicht automatisch
weitere geographische Mittelpunkte.

## Invarianten

Für den produktiven Datenpfad gelten später serverseitig, nicht nur in der
Benutzeroberfläche:

1. Jede aktive Ortsweberei besitzt genau eine stabile Gewebezellen-ID.
2. Jede aktive Ortsweberei besitzt genau ein aktives Webgemeindezentrum.
3. Ein aktives Zentrum gehört genau einer Ortsweberei und ihrer Gewebezelle.
4. Der aktive Zentrumspunkt liegt an einem bewusst beschlossenen Ort und wird
   nicht aus einem Gebiets- oder Mitglieder-Mittelwert geraten.
5. Ein Standortwechsel erhält Zentrum-ID, Ortsweberei-ID, Gewebezellen-ID,
   URLs und Chronik.
6. Eine Auflösung archiviert Ortsweberei und Zentrum gemeinsam; sie hinterlässt
   keine aktive Ortsweberei ohne Zentrum.
7. Anträge und andere lokale Governance-Objekte nennen ihre zuständige
   Ortsweberei eindeutig.

## Standortzustände

Der aktive Standort besitzt genau einen öffentlich verständlichen Zustand:

- `desired`: gewünschter Treffort; Nutzung noch nicht bestätigt;
- `provisional`: vorläufig nutzbarer Treffort;
- `confirmed`: regelmäßige Nutzung und Zugänglichkeit sind geklärt;
- `unavailable`: Zentrum bleibt Orientierungsanker, ist derzeit aber nicht als
  Treffort nutzbar;
- `relocation_proposed`: ein Ersatzstandort befindet sich im
  Entscheidungsverfahren.

Die Oberfläche übersetzt diese technischen Werte in verständliche deutsche
Bezeichnungen. Sie darf `desired` oder `provisional` nicht als bestätigte
öffentliche Zugänglichkeit ausgeben.

## Standortwahl und Datenschutz

Bevorzugt wird ein real zugänglicher Gemeinschaftsort, etwa eine Bibliothek,
ein Kulturzentrum, ein Gemeinschaftsraum, ein kooperierender Betrieb oder ein
öffentlicher Platz. Ist noch kein Raum gesichert, darf die Ortsweberei einen
gewünschten Treffort markieren und diesen Status offen benennen.

Eine Privatwohnung darf nur veröffentlicht werden, wenn der betroffene
Bewohner ausdrücklich zustimmt und Zugangszeiten sowie Bedingungen öffentlich
angegeben werden. Ohne diese Voraussetzungen wird ein anderer öffentlicher oder
hinreichend ungenauer Ort gewählt. Das System darf eine private Adresse nicht
automatisch aus Account- oder Garnrollendaten übernehmen.


## Erste produktive Instanz

Die bisherige einzelne Gewebezelle wird als **Ortsweberei Hamm** gebunden:

- Ortsweberei-ID: `ortsweberei-hamm`;
- Gewebezellen-ID: `hamm.weltgewebe.net`;
- Zentrum-ID: `webgemeindezentrum-hammer-park`;
- öffentlicher Name: **Webgemeindezentrum Hammer Park**;
- erster Kartenanker: ungefähr `53.5585, 10.0580` auf einer Grünfläche im
  Hammer Park;
- Standortzustand: `desired` / **Gewünschter Treffort**.

Die Koordinate ist ein bewusst gesetzter gemeinschaftlicher Kartenanker, keine
Vermessungs-, Reservierungs- oder Genehmigungsbehauptung. Die genaue Stelle,
Zugänglichkeit und regelmäßige Nutzbarkeit werden erst durch einen späteren
Ortsweberei-Entscheid und reale Prüfung präzisiert. Bis dahin muss jede
öffentliche Darstellung den gewünschten Status sichtbar erhalten.

## Kartenvertrag

Das Webgemeindezentrum ist ein eigener typisierter Strukturknoten. Es ist kein
gewöhnliches Angebot, kein persönlicher Accountmarker und kein zeitlich
verblassender Aktivitätsfaden.

Die normale Kartenansicht zeigt mindestens:

- Name der Ortsweberei;
- Standortzustand;
- Einstieg in Übersicht, Zusammenkommen und Webrat;
- dringende lokale Entscheidungen oder den nächsten öffentlichen Termin nur in
  einer kompakten, nicht überladenden Form.

Bei kleinen Zoomstufen dürfen Zentren gebündelt werden. Bei Auswahl bleibt die
Zentrum-ID der Fokus; Detailbereiche werden im Kontextpanel geöffnet. Die
Basiskarte enthält das Zentrum nicht als eingebranntes Kartenmerkmal, sondern es
bleibt eine commonThing-Domänenentität.

## Zentrumoberfläche

Das Fokuspanel gliedert sich schrittweise in:

1. **Übersicht:** Beschreibung, Gebiet, Standortzustand und wichtige lokale
   Aktivitäten;
2. **Zusammenkommen:** Treffzeiten, Zugang, Barrierefreiheit, Weg und Termine;
3. **Webrat:** Anträge, Einspruchsfristen, Abstimmungen, Entscheidungen und
   Delegationen;
4. **Gewebe:** örtliche Knoten, Ressourcen, Vorhaben und Bedarfe;
5. **Gewebekonto:** Bestand, Einnahmen, beschlossene Ausgaben und
   Nachvollziehbarkeit.

Nicht implementierte Bereiche werden nicht als funktionsfähige Schaltflächen
simuliert.

## Governance und Verortung

Die Gründung einer Ortsweberei legt fachlich gemeinsam an:

- Ortsweberei;
- stabile Gewebezellen-Identität;
- vorläufiges oder gewünschtes Webgemeindezentrum;
- lokalen Webrat und Gesprächsraum.

Ein Umzug des Zentrums ist eine nachvollziehbare Governance-Entscheidung. Der
alte und der vorgeschlagene Ort werden im Verfahren klar unterschieden. Erst
die angenommene Entscheidung ändert die aktive Kartenposition; der frühere Ort
bleibt in der Chronik nachvollziehbar.

Ein Weberantrag und spätere Sachanträge sind über den Webrat der zuständigen
Ortsweberei auffindbar. Bezieht sich ein Sachantrag zusätzlich auf einen
konkreten Knoten, darf derselbe Antrag auch von dort aus erreichbar sein. Es
entsteht dabei kein zweiter Antrag.

Ein Knotenbezug ersetzt die Zuständigkeit des Zentrums nicht. Der Node-Antrag
ist derselbe Center-Antrag mit einem zusätzlichen Ziel und einem dauerhaften
Knotentitel-Snapshot. Seine Annahme ist ein Beschluss; sie mutiert den Knoten
nicht automatisch.

Eine kanonische Zuordnung jedes gewöhnlichen Knotens zu genau einer
Ortsweberei existiert in diesem Vertragsstand noch nicht. Deshalb gilt für die
Anlage ohne ausdrücklich übergebene Zentrum-ID: Sie ist nur zulässig, wenn
genau ein aktives Webgemeindezentrum eindeutig aufgelöst werden kann. Bei
mehreren aktiven Zentren bleibt die Anlage fail-closed, bis eine kanonische
Node→Ortsweberei-Zuordnung beschlossen und implementiert ist. Ein Client darf
diese Zuständigkeit nicht aus Koordinaten oder Kartenentfernung raten.

## Fäden

Antragstellung, Veto, Stimme, Gesprächsbeitrag und Verfahrensphase bleiben
Governance-Datensätze. Sie werden auf der Antragsseite nicht als dekorative
Fadenbündel gezeichnet.

Eine spätere Governance-Linse darf nur ausdrücklich spezifizierte Beziehungen
zeigen, deren typisierte Endpunkte und auslösende Handlung belegt sind. Zähler,
Kategorien und Nullwerte sind keine Fäden. Eine UI-Projektion darf keine
`domain_edges` erzeugen oder eine fehlende Domainbeziehung vortäuschen.

## Ereignisse und Chronik

Die spätere Runtime soll mindestens folgende Ereignisse unterscheiden:

- Ortsweberei angelegt;
- Gewebezellen-Identität zugewiesen;
- Webgemeindezentrum verortet;
- Standort vorgeschlagen;
- Standort bestätigt;
- Zentrum verlegt;
- Treffort vorübergehend nicht verfügbar;
- Ortsweberei und Zentrum archiviert.

Die Ereignisnamen im Code dürfen technisch anders heißen. Ihre fachlichen
Bedeutungen und Übergänge müssen jedoch eindeutig bleiben.

## Einführungsgrenze

Der erste Runtime-Schnitt führt kanonische Ortsweberei-, Gewebezellen- und
Zentrum-Datensätze, serverseitige Eindeutigkeitsregeln, öffentliche
Leseendpunkte, Kartenprojektion und Standortchronik ein. Die erste Instanz wird
nicht im Client erfunden, sondern durch die PostgreSQL-Migration als
Primärwahrheit angelegt.

Produktiv freigeschaltet sind inzwischen die bestehenden Weberanträge sowie
Sachanträge im Webrat des Zentrums und mit optionalem Knotenbezug. Noch nicht
Teil dieses Schnitts sind dagegen automatische Ausführungsbefehle aus einem
angenommenen Sachantrag, Trefftermine, Gewebekonto, mehrere Betreiber oder die
Bestätigung der tatsächlichen Nutzung im Hammer Park. Diese offenen Bereiche
dürfen nicht als funktionsfähige Schaltflächen oder als bestätigte
Zugänglichkeit simuliert werden.

## Abnahmekriterien für die Runtime

Der erste Runtime-Schnitt gilt als belegt, wenn:

- Datenbank und API die Eins-zu-eins-Invarianten erzwingen;
- eine Ortsweberei nicht ohne Zentrum aktiviert werden kann;
- Standortstatus und tatsächliche Zugänglichkeit getrennt sichtbar sind;
- keine Privatadresse automatisch veröffentlicht wird;
- Zentrum, Ortsweberei und Gewebezelle stabile Identitäten besitzen;
- Governance-Objekte eindeutig der zuständigen Ortsweberei zugeordnet sind;
- Standortwechsel chronologisch nachvollziehbar sind;
- Karte, Mobilansicht, Tastatur und Bildschirmleser unterstützt werden;
- eine spätere technische Verselbständigung der Gewebezelle keine neuen
  fachlichen IDs verlangt.
