---
id: specs.governance-antraege
title: Anträge, Konsent, Veto und Abstimmung
summary: Kanonischer Produkt- und Ausführungsvertrag für die allgemeine Antragsmechanik und die Aufnahme von Gästen als Weber.
doc_type: specification
status: canonical
canonicality: normative
lifecycle_state: active
role: norm
organ: governance
owner: governance
last_reviewed: 2026-08-16
review_after: 2026-10-12
depends_on:
  - specs.garnrolle-knoten-faden
relations:
  - type: relates_to
    target: docs/domain/vocabulary.md
  - type: relates_to
    target: docs/specs/ortsweberei-webgemeindezentrum.md
verifies_with:
  - apps/api/tests/api_governance_guards.rs
  - apps/api/tests/db_governance.rs
  - apps/web/src/lib/api/governance.test.ts
  - apps/web/tests/governance.spec.ts
  - apps/web/tests/proofs/governance-full-flow.proof.ts
attention_source_status: source
attention_source_rationale: "Eigene offene Anträge und betrachterbezogene Governance-Beteiligung liefern kanonische persönliche Fakten für WARTET und MITWIRKEN."
attention_source_facts:
  - proposal.applicant_account_id zusammen mit offenem Verfahrensstatus
  - proposal.viewer_participation mit may_vote, may_veto, vote_choice und has_veto
  - proposal.last_activity_at und serverkalibriertes remaining_seconds
attention_projection:
  - apps/web/src/lib/components/topBarAttentionState.ts
attention_transition_tests:
  - apps/web/src/lib/components/topBarAttentionState.test.ts
  - apps/web/tests/attention-bubbles.spec.ts
---

# Anträge, Konsent, Veto und Abstimmung

## Grundsatz

Alles, was in commonThing gemeinschaftlich verändert werden kann, soll über
einen transparenten Antragsmechanismus disponibel sein. Jeder Antrag besitzt
eine Informationsseite, einen öffentlichen Gesprächsraum, eindeutige Fristen
und eine dauerhaft nachvollziehbare Zustandsfolge.

Der erste produktive Antragstyp ist der **Weberantrag**. Er überführt einen
mitwirkenden Gast nach gemeinschaftlicher Prüfung in den Weberstatus. Die
Garnrolle besteht bereits vorher; die Annahme verleiht zusätzliche Pflege- und
Entscheidungsrechte.

Der zweite produktive Antragstyp ist der **Sachantrag**. Er hält einen
gemeinschaftlichen Beschluss der zuständigen Ortsweberei fest. Er verwendet
denselben Konsent-, Veto-, Gesprächs-, Abstimmungs- und Finalisierungspfad wie
der Weberantrag; es gibt keine zweite Entscheidungsengine.

## Gast und Weber

Ein neu registrierter Account beginnt als `gast` und besitzt genau eine
Garnrolle.

Ein Gast darf:

- alles Öffentliche in commonThing ansehen, suchen und filtern;
- die eigene Garnrolle beschreiben und freiwillig verankern;
- Knoten knüpfen und eigene Knoten pflegen;
- zulässige, serverseitig abgeleitete Fäden auslösen;
- in offenen Knoten- und Antragsgesprächen mitreden;
- den eigenen Weberstatus beantragen;
- den eigenen Gastaccount auflösen.

Ein Gast darf keine fremden oder historisch eigentümerlosen Knoten bearbeiten.
Formale Vetos und Abstimmungen sind Webern und Administratoren vorbehalten.
Über den eigenen Weberantrag darf der Antragsteller auch nach einem möglichen
Rollenwechsel nicht selbst entscheiden; diese Selbstentscheidungsgrenze gilt
rollenunabhängig.

Die Zahl eigener Gastknoten ist pro Account begrenzt. Der Betriebsstandard liegt
bei 1.000 Knoten und kann über `MAX_GUEST_OWNED_NODES` als positive Ganzzahl
enger oder weiter gefasst werden. Die Grenze wird serverseitig und bei
PostgreSQL unter dem Account-Lock geprüft; ein idempotenter Retry derselben
`operation_id` bleibt auch am Limit zulässig. Weber und Administratoren sind
von dieser Gastgrenze nicht betroffen.

Ein Weber darf zusätzlich fremde und gemeinschaftliche Knoten pflegen sowie bei
fremden Anträgen ein begründetes Veto einlegen und abstimmen. Weber und
Administratoren dürfen Sachanträge stellen; Gäste dürfen weiterhin nur den
eigenen Weberstatus beantragen.

Administratoren besitzen darüber hinaus moderative Rechte.

## Weberantrag

Der Gast löst **Weberstatus beantragen** aus. Pro Gast darf höchstens ein
offener Weberantrag bestehen. Der Antrag enthält:

- Antragsteller und Anzeigename;
- optionale Vorstellung oder Begründung;
- Status und vollständige Fristen;
- öffentliche Vetos;
- Stimmenzählung;
- Gesprächsraum;
- Ergebnis und Ausführungsstatus.

Der Antragsteller darf im eigenen Verfahren mitreden, aber weder ein formales
Veto gegen den eigenen Antrag einlegen noch über die eigene Aufnahme abstimmen.

## Sachantrag

Ein Sachantrag gehört genau zu einem aktiven Webgemeindezentrum und enthält:

- einen Titel mit höchstens 200 Zeichen;
- Antragsteller und Anzeigename-Snapshot;
- eine optionale Begründung;
- optional genau einen konkreten, bei Antragstellung existierenden Knoten;
- bei Knotenbezug dessen ID und Titelsnapshot;
- denselben Verfahrenszustand, dieselben Fristen und denselben Gesprächsraum wie
  ein Weberantrag.

Mehrere offene Sachanträge desselben Antragstellers sind zulässig. Der
Antragsteller darf auch beim eigenen Sachantrag weder Veto noch Stimme abgeben.
Ein Knoten-Sachantrag ist kein zweiter Antrag: Derselbe Datensatz ist über den
Webrat des Zentrums und zusätzlich über den Knoten erreichbar.

Wird der referenzierte Knoten später regulär aus dem aktiven Gewebe entfernt,
wird die aktive Knotenbindung gelöst. Der Titelsnapshot und der Sachantrag
bleiben als Verfahrens- und Beschlussspur erhalten.

## Rücknahme eines offenen Antrags

Der Antragsteller darf den eigenen Antrag während einer tatsächlich noch offenen
Konsent- oder Abstimmungsphase zurückziehen. Die Rücknahme löscht nichts. Der
Antrag wechselt auf den finalen Status `withdrawn`, erhält einen
Abschlusszeitpunkt und bleibt mit Gespräch, Vetos und bereits abgegebenen Stimmen
lesbar. Nach Ablauf der jeweiligen Frist ist eine verspätete Rücknahme nicht mehr
zulässig, auch wenn der Sweeper die fällige Entscheidung noch nicht ausgewertet
hat.

Die Rücknahme ist keine formale Selbstentscheidung über Annahme oder Ablehnung.
Sie ist ausschließlich die Beendigung des eigenen noch offenen Antrags.

## Aufhebung eines angenommenen Sachbeschlusses

Ein angenommener Sachantrag wird niemals nachträglich auf `rejected` gesetzt oder
gelöscht. Soll ein solcher Beschluss aufgehoben werden, stellt ein Weber oder
Administrator einen **neuen Sachantrag zur Aufhebung**. Dieser neue Antrag enthält
einen unveränderlichen Verweis `repeals_proposal_id` auf den ursprünglichen
Sachantrag und durchläuft exakt dieselbe Konsent-, Veto-, Gesprächs-, Abstimmungs-
und Finalisierungsengine wie jeder andere Sachantrag.

Erst wenn der Aufhebungsantrag `accepted` ist, gilt der frühere Beschluss als
aufgehoben. Seine eigene historische Entscheidung bleibt `accepted`; die
Oberfläche leitet den späteren Zustand aus der angenommenen Aufhebungsrelation ab
und zeigt beide Entscheidungen miteinander verknüpft. Ein abgelehnter oder
zurückgezogener Aufhebungsantrag verändert den alten Beschluss nicht und sperrt
keinen späteren neuen Versuch. Parallel darf höchstens ein offenes oder bereits
angenommenes Aufhebungsverfahren denselben Beschluss adressieren.

Aufhebungsanträge sind weiterhin `sachantrag`; es entsteht kein dritter
Antragstyp und keine zweite Entscheidungsengine. Ein Aufhebungsantrag selbst wird
nicht rekursiv aufgehoben. Soll eine Gemeinschaft später wieder eine frühere
Regelung herstellen, geschieht das durch einen neuen inhaltlichen Sachantrag.

## Erste Phase: sieben Tage Konsent

Nach dem Stellen befindet sich der Antrag exakt sieben Tage im Status
`consent`.

Jeder Weber oder Administrator außer dem Antragsteller selbst kann in dieser
Zeit ein begründetes Veto einlegen. Das Veto muss einen nichtleeren Einwand
enthalten.

Nach Ablauf gilt:

- **kein Veto:** Antrag wird angenommen;
- **mindestens ein Veto:** Antrag wechselt in die Abstimmungsphase.

Ein Veto lehnt den Antrag nicht ab. Es erzwingt Beratung und eine ausdrückliche
Abstimmung.

## Zweite Phase: sieben Tage Gespräch und Abstimmung

Die zweite Phase schließt unmittelbar an die erste an und dauert exakt weitere
sieben Tage. Die maximale reguläre Verfahrensdauer beträgt vierzehn Tage.

Jeder Weber oder Administrator außer dem Antragsteller selbst besitzt pro Antrag
genau eine aktuelle Stimme. Sie kann bis zum Fristende geändert werden:

- `ja`;
- `nein`;
- `enthaltung`.

Es gibt kein Quorum und keine Mindestbeteiligung. Die Entscheidungsregel ist:

```text
Ja-Stimmen > Nein-Stimmen
```

Daraus folgt:

- mehr Ja als Nein: angenommen;
- Gleichstand: abgelehnt;
- 0:0: abgelehnt;
- Enthaltungen verändern den Vergleich nicht.

## Ausführung eines angenommenen Weberantrags

Annahme und Rollenwechsel sind ein einziger PostgreSQL-Vorgang. Innerhalb
derselben Transaktion:

1. wechselt der Account von `gast` zu `weber`;
2. wechselt der Antrag auf `accepted`;
3. wird der Abschlusszeitpunkt gespeichert.

Nicht verändert werden:

- Account- und Garnrollen-ID;
- Anzeigename, Beschreibung und Tags;
- `map_state`, Position oder Radius;
- Urheberschaft bereits geknüpfter Knoten;
- bestehende Fäden und Gesprächsbeiträge.

Wiederholte Fristenauswertung ist idempotent. Es darf weder ein angenommener
Antrag ohne Weberrolle noch eine zweite Garnrolle entstehen.

## Wirkung eines angenommenen Sachantrags

Ein angenommener Sachantrag wechselt in derselben Finalisierungsengine auf
`accepted`, ohne eine Accountrolle zu verändern. Er ist in diesem Schnitt ein
**Beschluss**, kein automatischer allgemeiner Datenmutator. Insbesondere ruft
die Annahme keinen direkten `PATCH`, `PUT` oder `DELETE` auf einem Knoten auf und
umgeht weder dessen Autorisierung noch Konflikt-, Audit- oder
`collective_write_guard`-Grenzen. Eine spätere beschlussspezifische Ausführung
benötigt einen eigenen eng typisierten Vertrag; der Sachantrag selbst ist kein
beliebiger Mutations-Executor.

## Fristenauswertung

Fristen werden serverseitig ausgewertet. Ein Browser ist weder Uhr noch
Ausführungsinstanz.

Die API wertet fällige Anträge bei relevanten Zugriffen aus. Zusätzlich läuft
im API-Prozess ein regelmäßiger Sweeper. Beide Wege benutzen dieselben
gesperrten und idempotenten Datenbankübergänge.

Governance ist nur aktiv, wenn Account-Lesen und Account-Schreiben kanonisch
aus PostgreSQL erfolgen. Fehlt diese Voraussetzung, antworten die Endpunkte
fail-closed statt auf eine zweite Wahrheit auszuweichen.

## Gesprächsraum

Jeder Antrag besitzt einen öffentlichen, lesbaren Gesprächsraum.

- Nicht angemeldete Besucher dürfen lesen.
- Angemeldete Gäste, Weber und Administratoren dürfen während einer offenen
  Phase Beiträge verfassen.
- Nach Abschluss bleibt der Gesprächsraum als Verfahrensnachweis lesbar, aber
  geschlossen.
- Beim Löschen eines Gastaccounts bleiben Beiträge als Verfahrensspur erhalten;
  die aktive Accountbindung des Beitrags wird entfernt.

Mitreden ist ein Webungsrecht jedes angemeldeten Accounts. Formale Vetos und
Stimmen setzen Weber- oder Administratorstatus voraus. Die Selbstentscheidung
des Antragstellers bleibt zusätzlich ausgeschlossen.

## Gast-Austritt

Ein Gast kann den eigenen Account vollständig auflösen. Details zur Erhaltung
und Anonymisierung gemeinschaftlicher Spuren stehen in
`docs/specs/garnrolle-knoten-faden.md`.

Der Austritt entfernt leere eigene Weberanträge, Passkeys, Sitzungen und die
Garnrolle. Anträge mit Beiträgen, Veto, Stimme oder bereits gefällter Entscheidung
bleiben als Verfahrensspur erhalten: Die aktive Accountbindung wird gelöst, der
Anzeigename bleibt als Snapshot, und ein noch offenes Verfahren wird abgelehnt.
Gemeinschaftlich sichtbare Knoten und Beiträge werden nicht stillschweigend
vernichtet. Der Pfad ist für Weber und Administratoren gesperrt,
weil deren Austritt bereits übernommene gemeinschaftliche Verantwortung berührt
und einen eigenen späteren Governance- und Datenschutzprozess benötigt.

## Anträge-Oberfläche

Bis zur produktiven Ortsweberei-Zuordnung bleibt der Wurzelknopf **Gemeinsam**
im Kartenkopf die globale Governance-Sicht. Im Zielmodell öffnet das
Webgemeindezentrum zusätzlich den Webrat seiner Ortsweberei. Globale und lokale
Sicht referenzieren dieselben Anträge; sie erzeugen keine Duplikate.

Der globale Einstieg zeigt reale Governance-Sichten: alle Anträge, offene
Konsentverfahren, Vetos, Gespräche mit tatsächlichen Beiträgen und laufende
Abstimmungen. Das Stellen eines Antrags erscheint als eigene Webungsaktion im
unteren Werkzeugfächer.

Die Oberfläche bietet:

- Liste und Informationsseite aller Anträge;
- Verfahrensart, Status, vollständige Frist und nächsten möglichen Ausgang;
- eine zugängliche Prozessanzeige für Antragstellung, Einspruchsfrist,
  gegebenenfalls Gespräch und Abstimmung sowie Entscheidung;
- Vetos und Beitragszahlen sowie Stimmen erst, wenn eine Abstimmungsphase
  existiert oder abgeschlossen wurde;
- keine als Fäden gezeichneten Kategorien, Zähler oder Verfahrensphasen;
- den öffentlichen Gesprächsraum;
- für angemeldete Accounts das Beitragsfeld;
- für Weber und Administratoren bei fremden Anträgen die kontextabhängigen
  Veto- und Abstimmungsaktionen;
- für Gäste Weberantrag und Gast-Austritt;
- für Weber und Administratoren Sachanträge im Webrat eines Zentrums und,
  sofern ein Knoten Ziel ist, zusätzlich im Antragsbereich dieses Knotens;
- für den Antragsteller bei einem offenen eigenen Verfahren die Aktion
  **Antrag zurückziehen**;
- bei angenommenen Sachbeschlüssen für Weber und Administratoren die Aktion
  **Aufhebung beantragen** sowie sichtbare Verweise zwischen ursprünglichem
  Beschluss und laufendem oder angenommenem Aufhebungsverfahren.

`GET /api/proposals` liefert für jeden Antrag `message_count`. Solange die
Governance-Gespräche noch nicht auf die kanonische Release-B-Konversation
umgeschaltet sind, stammt dieser Zähler aus `governance_messages`, der derzeit
maßgeblichen Beitragstabelle. Die Sicht `?ereignis=gespraech` zeigt genau
Anträge mit `message_count > 0` — unabhängig davon, ob sie sich in Konsent,
Abstimmung oder bereits im Abschluss befinden. Ein Abstimmungsstatus allein
belegt kein Gespräch. Der Zähler ist eine Momentaufnahme der Listenabfrage; er
ersetzt weder das Laden der Beiträge noch deren eigene spätere Pagination.
Während eines gestaffelten Rollouts behandelt die neue Oberfläche eine noch
fehlende Zählung aus einer älteren API-Version sicher als null.

Die Oberfläche darf keine Rechte simulieren. Jeder Schutz wird zusätzlich auf
dem Server durchgesetzt.

Der releasegebundene Governance-Vollflussbeweis startet Browser, Web-App und echte
API gegen eine Wegwerf-PostgreSQL-Datenbank. Er belegt sowohl die automatische
Annahme ohne Veto als auch Veto, änderbare Stimme, Ja-Mehrheit, Ausschluss der
Selbstentscheidung und den atomaren Rollenwechsel unter Beibehaltung der
Account-ID. Seine Hilfsrouten werden ausschließlich mit dem Cargo-Feature
`integration-testing` kompiliert und existieren nicht im Produktions-Binary.

## Antragstypen

Der Datenbankvertrag lässt ausschließlich `weberantrag` und `sachantrag` zu.
Beide verwenden dieselbe Zustandsmaschine. Ein Aufhebungsverfahren ist ein
Sachantrag mit `repeals_proposal_id`, kein zusätzlicher Antragstyp. Weitere
Antragstypen bleiben ohne eigene Spezifikation und Migration fail-closed
ausgeschlossen.

## Nicht-Ziele dieses Schnitts

- kein Quorum;
- keine Mindestbeteiligung;
- keine gewichteten Stimmen;
- keine automatische Beförderung nach Zeit oder Aktivität;
- keine direkte Vergabe des Weberstatus durch einzelne Administratoren;
- kein automatischer beliebiger Mutations-Executor aus angenommenen
  Sachanträgen;
- kein Überschreiben oder Löschen eines früher angenommenen Beschlusses bei
  seiner späteren Aufhebung;
- keine zweite Governance-Wahrheit außerhalb PostgreSQL;
- noch kein allgemeiner Austrittsprozess für Weber.
