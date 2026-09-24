---
id: specs.garnrolle-knoten-faden
title: Garnrolle, Knoten und Faden
summary: Kanonischer Produktvertrag für Gastidentität, Garnrolle, Knoten, Webungsaktionen und daraus abgeleitete Fäden.
doc_type: specification
status: canonical
canonicality: normative
lifecycle_state: active
role: norm
organ: product-domain
owner: product-domain
last_reviewed: 2026-08-02
review_after: 2026-10-12
depends_on: []
relations:
  - type: relates_to
    target: docs/specs/governance-antraege.md
  - type: relates_to
    target: docs/specs/objektlebenszyklen-und-loeschwirkungen.md
  - type: supersedes
    target: docs/konzepte/garnrolle-und-verortung.md
  - type: supersedes
    target: docs/specs/privacy-ui.md
  - type: relates_to
    target: docs/adr/ADR-0009__garnrolle-verortung-sichtbarkeit.md
  - type: relates_to
    target: docs/domain/vocabulary.md
verifies_with:
  - contracts/domain/account.schema.json
  - contracts/domain/node.schema.json
  - contracts/domain/edge.schema.json
  - apps/api/tests/api_governance_guards.rs
  - apps/api/tests/api_accounts.rs
  - apps/api/tests/db_domain_edge_write_path.rs
  - apps/api/tests/db_node_conversations.rs
  - apps/web/src/lib/demo/resolvers.test.ts
  - apps/web/tests/garnrolle-self-service.spec.ts
  - apps/web/tests/garnrolle-relations.spec.ts
  - apps/web/tests/komposition.spec.ts
attention_source_status: none
attention_source_rationale: "Definiert langlebige Identitäten und Gewebebeziehungen, derzeit jedoch keinen persönlichen offenen Vorgang oder Beteiligungsfakt für Attention."
---
# Garnrolle, Knoten und Faden

## Grundsatz

Jeder registrierte Account besitzt genau eine Garnrolle. Die Garnrolle ist der
persönliche Ausgangspunkt in commonThing; die Berechtigungsrolle `gast`, `weber`
oder `admin` beschreibt dagegen, welche Verantwortung der Account übernehmen
darf. Sichtbarkeit und Kartenposition werden ausschließlich durch `map_state`
gesteuert.

Ein Account beginnt als `gast`. Gäste dürfen bereits weben: die eigene
Garnrolle beschreiben und verankern, Knoten knüpfen, zulässige Fäden auslösen
und in offenen Gesprächen mitreden. Der Weberstatus ist keine technische
Freischaltung der ersten Garnrolle, sondern die gemeinschaftlich bestätigte
Befugnis, auch fremde beziehungsweise gemeinschaftliche Inhalte zu pflegen.
Formale Veto- und Stimmrechte stehen ausschließlich Webern und Administratoren
bei fremden Weberanträgen zu; über den eigenen Antrag darf niemand selbst entscheiden.

## Rollen und Fähigkeiten

| Fähigkeit | Gast | Weber | Admin |
|---|---:|---:|---:|
| Eigene Garnrolle beschreiben und verankern | ja | ja | ja |
| Knoten knüpfen | ja | ja | ja |
| Eigenen Knoten bearbeiten oder entfernen | ja | ja | ja |
| Fremden oder eigentümerlosen Knoten pflegen | nein | ja | ja |
| In offenen Knoten- und Antragsgesprächen schreiben | ja | ja | ja |
| Eigenen Gesprächsbeitrag bearbeiten | ja | ja | ja |
| Veto und Stimme zu fremden Weberanträgen | nein | ja | ja |
| Moderativ fremde Beiträge entfernen | nein | nein | ja |

Nicht angemeldete Besucher dürfen öffentliche Inhalte lesen, aber keine
Webungsaktion ausführen.

## Garnrolle

Die Garnrolle ist der Account selbst. Es gibt weder einen zweiten
Garnrollen-Datensatz nach der Aufnahme als Weber noch einen getrennten
Gastmodus der Identität.

Jede neue Garnrolle beginnt datenschutzsicher mit:

```text
map_state = not_on_map
```

Der Account kann anschließend bewusst wählen:

- `not_on_map`: keine öffentliche Position;
- `exact`: ausdrücklich bestätigte genaue Position;
- `radius`: stabile, abgeleitete ungefähre Position innerhalb des gewählten
  Radius.

Nur der angemeldete Account darf sein Garnrollenprofil verändern. Interne
Positionen und private Projektionsbindungen erscheinen nie in öffentlichen
Antworten.

## Knoten

Knoten beschreiben Orte, Kollektivgüter, Ressourcen, Vorhaben, Bedürfnisse oder
Angebote. Jeder neu erzeugte Knoten erhält serverseitig eine unveränderliche
Urheberbindung:

```text
created_by_account_id = Account-ID der authentifizierten Sitzung
```

Der Client darf diese Bindung weder vorgeben noch ändern. Beim Ersetzen oder
Bearbeiten eines Knotens bleibt sie erhalten.

Der Detailendpunkt `GET /api/nodes/{id}` ergänzt diese unveränderliche Bindung
um zwei abgeleitete, nicht persistierte Projektionen:

- `created_by_account_current_title` ist ausschließlich der **heutige öffentliche**
  Garnrollenname. Das Feld fehlt bei deaktivierten, fehlenden oder leeren
  Accounts und ist kein historischer Namenssnapshot.
- `history` enthält die typisierten Ereignisse `created` und – sofern
  `updated_at` von `created_at` abweicht – `updated`, jeweils aus den
  autoritativen Knotendaten abgeleitet und neuestes Ereignis zuerst.

Die Benutzeroberfläche darf eine interne Account-ID niemals als sichtbaren
Namensersatz ausgeben. Ein öffentlicher aktueller Name darf zur heutigen
Garnrolle führen; eine unveränderliche historische Namensanzeige benötigt ein
eigenes persistiertes Ereignis- und Snapshotmodell.

Für bestehende Knoten, deren Urheber historisch nicht belegt ist, wird keine
Urheberschaft erfunden. Sie besitzen keine `created_by_account_id`. Daraus folgt:

- Gäste dürfen solche Knoten nicht bearbeiten oder entfernen;
- Weber dürfen sie gemeinschaftlich pflegen;
- Administratoren behalten den moderativen Pfad.

Ein Gast darf einen vorhandenen Knoten nur verändern, wenn dessen gespeicherte
Urheberbindung exakt der Account-ID seiner aktuellen Sitzung entspricht. Diese
Prüfung erfolgt auf dem Server und gilt unabhängig von der sichtbaren
Oberfläche. Versionsvorbedingungen (`If-Match`) schützen zusätzlich gegen
überschriebene Paralleländerungen.

## Fäden

Fäden sind Beziehungen zwischen Garnrollen und Knoten. Der produktive
Webungsweg erzeugt sie serverseitig als Folge einer fachlichen Handlung, etwa
wenn ein angemeldeter Account einen Knoten knüpft. Ein Client darf dabei keine
fremde Garnrolle als handelnden Ausgangspunkt vortäuschen.

Dieser Schnitt eröffnet bewusst keinen allgemeinen manuellen
`POST /edges`-Editor. Ein solcher Editor benötigt einen eigenen Vertrag für
Fadenarten, Eigentum, Änderung, Entfernung und Missbrauchsschutz. Bis dahin
bleiben Fäden aus den belegten Webungsaktionen abgeleitete, öffentlich lesbare
Projektionen.

Freie interne Fadennotizen gehören nicht zur öffentlichen Projektion.

Fäden sind keine frei editierbaren Fachobjekte. Für abgeleitete Fadenprojektionen
gelten weiterhin diese harten Regeln:

1. Es gibt keinen allgemeinen öffentlichen `POST`, `PUT`, `PATCH` oder `DELETE`-Pfad,
   mit dem Clients beliebige Fäden als eigene Wahrheit erzeugen oder verändern.
2. Die Benutzeroberfläche bietet keinen unabhängigen Fadeneditor.
3. Der Server erzeugt oder repariert eine vorgesehene Projektion mit stabiler
   Operations-ID, damit Wiederholungen keine Doppelprojektionen erzeugen.
4. Aus der Projektion muss auf die belegte fachliche Webungsaktion zurückgeführt
   werden können.

Beim Knotenknüpfen erzeugt der Server nach der dauerhaften Knotenanlage den
zugehörigen Garnrolle-Knoten-Faden. Der Account-Lifecycle wird dabei vom Beginn
der Knotenpersistenz bis zum erfolgreichen Faden oder zur Kompensation
serialisiert. Schlägt die Projektion dauerhaft fehl, darf kein verwaister neuer
Knoten als erfolgreicher Gesamtvorgang zurückbleiben.

Anträge, Vetos, Abstimmungen und Gesprächsbeiträge bleiben eigene dauerhafte
Governance-Datensätze. Sie sind keine Fäden und werden auf der
Antragsinformationsseite nicht als Fadenbündel gezeichnet. Ihre Oberfläche zeigt
stattdessen Verfahrensphase, Fristen, nächsten möglichen Ausgang und die jeweils
relevanten Zähler. Eine spätere Governance-Linse darf nur ausdrücklich
spezifizierte, aus einer belegten Handlung abgeleitete Beziehungen mit echten,
typisierten Endpunkten darstellen. Zähler, Kategorien und Verfahrensphasen
erzeugen weder `domain_edges` noch eine zweite Beziehungswahrheit.

### Aktivität einer Garnrolle

Die Aktivitätsansicht einer Garnrolle ist eine dauerhafte Leseprojektion
belegter fachlicher Handlungen und ausdrücklich **keine Liste ihrer derzeit
aktiven Fäden**. Ein Faden zeigt die zeitlich begrenzte Beteiligung im aktuellen
Gewebe; sein Verblassen darf die zugrunde liegende Aktivität nicht aus der
Chronik entfernen.

Für die Projektion gelten folgende Regeln:

1. Das Knüpfen eines Knotens wird aus seiner weiterhin aktiven
   Urheberbindung abgeleitet. Fehlt diese bei einem Legacy-Knoten, darf nur ein
   eng zeitgebundener, frühester Herkunftsfaden mit explizit als Account und
   Knoten typisierten Endpunkten als Rückfallevidenz dienen; untypisierte Fäden
   und Gesprächsfäden sind davon auszuschließen. Fehlt der ursprüngliche
   Erstellungszeitstempel `created_at`, wird keine Aktivität aus einem späteren
   Änderungszeitpunkt geraten. Wird eine Accountidentität
   abgelöst, dürfen interne Erzeugungs- oder Idempotenzfelder die entfernte
   Urheberbindung nicht wiederherstellen.
2. Gesprächsbeiträge werden aus den dauerhaften Beitragsdatensätzen als eigene
   Aktivitätsart ausgewiesen und niemals als erneutes Knotenknüpfen bezeichnet.
   Eine Tombstone-Löschung entfernt den Inhalt, nicht den belegten
   Beitragsvorgang oder dessen Zählung.
3. Mehrere Beiträge derselben Garnrolle zum selben Knotengespräch am selben
   UTC-Kalendertag dürfen zu einem Eintrag mit exakter Anzahl zusammengefasst
   werden. Inhalte der Beiträge gehören nicht in die öffentliche
   Aktivitätsprojektion.
4. Ein bloßer, eingehender oder aus einer nicht klassifizierten Beteiligung
   abgeleiteter Faden darf keine Aktivität erfinden.
5. Der Fadenverfall begrenzt weiterhin die aktuelle Karten- und
   Knotenbeziehungsprojektion, nicht die dauerhafte Aktivitätschronik.

### Auflösung unverzwirnter Fäden

Jeder neu abgeleitete, unverzwirnte Faden besitzt ab seiner Entstehung eine
eigene Lebensdauer von exakt **168 Stunden**. Der Server setzt `expires_at`
ausschließlich aus dem servereigenen `created_at`; ein Client darf weder Beginn
noch Ende vorgeben.

1. Die sichtbare Stärke nimmt zwischen `created_at` und `expires_at` kontinuierlich
   und linear von vollständig sichtbar auf unsichtbar ab. Die Karte projiziert
   den Wert minütlich neu; die exakte Ablaufgrenze wird unabhängig davon terminiert.
2. Bei `now == expires_at` gehört der Faden nicht mehr zur aktiven Projektion.
   Listen, Einzelabrufe und Karte geben ihn dann nicht mehr aus.
3. Eine spätere Webungsaktion verlängert keinen bestehenden Faden. Sie erzeugt,
   sofern fachlich vorgesehen, eine neue Projektion mit eigener Uhr.
4. Die zugrunde liegende Webungsaktion und ihre Chronik bleiben dauerhaft
   erhalten. Aufgelöst wird nur die abgeleitete Fadenprojektion.
5. Bei bestehenden Datensätzen mit gültigem `created_at`, aber ohne persistiertes
   `expires_at`, leiten API und Karte die Ablaufgrenze rückwirkend und
   deterministisch als `created_at + 168 Stunden` ab. Die Persistenz und Chronik
   werden dabei nicht umgeschrieben.
6. Vollständig undatierte Legacy-Datensätze ohne `created_at` und `expires_at`
   bleiben sichtbar, weil ihr Alter nicht ohne Schätzung rekonstruiert werden
   kann. Ein ungültiges vorhandenes `created_at` wird nicht als undatiert
   behandelt, sondern fail-closed ausgeblendet.
7. Ein vorhandener, aber ungültiger oder nicht exakt 168 Stunden langer
   Ablaufvertrag wird in aktiven Projektionen fail-closed ausgeblendet.

Ein verzwirnter, dauerhafter Zusammenhang heißt **Garn** und ist vom Fadenverfall
ausgenommen. Die Verzwirnungsaktion und ihre technische Garnrepräsentation sind
nicht Bestandteil dieses Vertragsstands. Bis zu einer eigenen kanonischen
Spezifikation darf dafür weder ein öffentliches CRUD noch ein geratenes Edge-Feld
eingeführt werden. `expires_at = null` ist deshalb kein regulärer Erzeugungswert
für neue Fäden, sondern ausschließlich ein Legacy- und späterer expliziter
Speicherzustand. Eine künftige Garnrepräsentation muss sich explizit von einem
unverzwirnten Faden unterscheiden und darf nicht allein aus `expires_at = null`
abgeleitet werden.

## Gespräche

Jeder PostgreSQL-Knoten besitzt genau einen öffentlichen Gesprächsraum.
Angemeldete Gäste, Weber und Administratoren dürfen während des offenen
Lebenszyklus Beiträge verfassen. Ein Beitrag besitzt einen Autoren-Snapshot und
eine aktive Accountbindung:

- nur der Autor darf den eigenen Beitrag bearbeiten;
- Autor oder Administrator dürfen ihn entfernen;
- beim Löschen eines Accounts wird die aktive Accountbindung entfernt, der
  historische Anzeigename und der Beitrag bleiben erhalten.

Ein Knoten darf unabhängig von vorhandenen Gesprächsbeiträgen aus dem aktiven
Gewebe gelöscht werden. Dabei gelten drei getrennte Wirkungen:

- der Knoten und seine verbundenen Fadenprojektionen werden regulär entfernt;
- ein leerer, automatisch erzeugter Gesprächsraum wird mit dem Knoten gelöscht;
- ein Gesprächsraum mit Beiträgen wird im selben Datenbankvorgang vom Knoten
  entkoppelt, mit Knoten-ID und letztem Knotentitel gekennzeichnet und als
  schreibgeschütztes Archiv erhalten.

Das Archiv bleibt über seine stabile Gesprächs-ID öffentlich lesbar. Nach der
Archivierung sind neue Beiträge und normale Inhaltsänderungen ausgeschlossen.
Autoren dürfen eigene Beiträge weiterhin zurückziehen, Administratoren dürfen
sie moderativ entfernen; beides erzeugt einen Tombstone statt einer physischen
Löschung. Beim Accountaustritt wird weiterhin nur die aktive Accountbindung
gelöst. Die frühere Diskussion darf nicht durch das Entfernen ihres Kartenobjekts
verschwinden oder nachträglich wiederbeschrieben werden. Die übergreifenden
Begriffe und Purgegrenzen normiert
`docs/specs/objektlebenszyklen-und-loeschwirkungen.md`.

## Aufnahme als Weber

Die Aufnahme folgt `docs/specs/governance-antraege.md`. Ein angenommener
Weberantrag ändert die Berechtigungsrolle von `gast` auf `weber`. Er erzeugt
keine neue Garnrolle und verändert weder Profil noch Kartenstatus, Position,
Urheberschaft eigener Knoten oder bisherige Gesprächsbeiträge.

Der zusätzliche Weberstatus verleiht die gemeinschaftliche Pflege fremder oder
historisch eigentümerloser Knoten sowie formale Veto- und Stimmrechte bei
fremden Weberanträgen.

## Gast-Austritt

Ein Gast darf den eigenen Account löschen. Der Austritt ist ein atomarer
PostgreSQL-Vorgang:

- eigene Weberanträge und deren abhängige Verfahrensdaten werden entfernt;
- Passkeys und Sitzungen werden entfernt;
- die Garnrolle wird entfernt;
- eigene, inzwischen gemeinschaftlich sichtbare Knoten bleiben bestehen und
  verlieren ihre aktive Urheberbindung;
- Fäden mit der gelöschten Garnrolle als Account-Endpunkt werden entfernt;
- Beiträge in fremden Anträgen und Knotengesprächen bleiben mit ihrem
  Anzeigenamen erhalten, verlieren aber die Account-ID;
- formale Vetos und Stimmen können einem löschbaren Gastkonto nicht zugeordnet
  sein; der Austritt von Webern und Administratoren ist nicht Teil dieses Pfads.

Der Austritt ist nur verfügbar, wenn Accounts, Knoten und Fäden kanonisch in
PostgreSQL gelesen und geschrieben werden. Ein Mischbetrieb wird fail-closed
abgewiesen, damit keine Spuren in einer zweiten Datenquelle zurückbleiben.

## Typischer Ablauf

1. Registrierung erzeugt eine Gast-Garnrolle im Zustand `not_on_map`.
2. Der Gast beschreibt und verankert die eigene Garnrolle freiwillig.
3. Der Gast knüpft einen Knoten; der Server bindet dessen Urheberschaft an den
   Account und erzeugt die zulässigen abgeleiteten Fäden.
4. Der Gast kommuniziert in Knoten- und Antragsgesprächen.
5. Der Gast kann den Weberstatus beantragen.
6. Bei Annahme bleibt die gesamte bisherige Identität unverändert; nur die
   gemeinschaftlichen Pflege- und Entscheidungsrechte kommen hinzu.

## Nicht-Ziele dieses Schnitts

- kein allgemeiner manueller Fadeneditor;
- keine freie Übertragung von Knoten-Eigentum;
- keine automatische Beförderung nach Aktivität oder Zeit;
- keine Bearbeitung fremder Gesprächsbeiträge außer administrativer Entfernung;
- keine erfundene Urheberschaft für Altbestand.
