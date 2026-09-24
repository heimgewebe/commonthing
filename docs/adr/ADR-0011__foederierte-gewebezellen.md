---
id: adr.ADR-0011__foederierte-gewebezellen
title: ADR-0011 — Föderierte Gewebe-Zellen
doc_type: reference
status: active
summary: >
  Entscheidet autonome lokale Gewebe-Zellen mit globalen Identitäten, Beziehungen und gemeinsamen Räumen als langfristiges Skalierungsmodell.
relations:
  - type: relates_to
    target: architecture/commonthing-os.md
  - type: relates_to
    target: docs/specs/garnrolle-knoten-faden.md
  - type: relates_to
    target: docs/blueprints/weltgewebe-os-masterplan.md
---

# ADR-0011 — Föderierte Gewebe-Zellen

Datum: 2026-07-15
Status: Accepted

## Kontext

commonThing soll lokale Selbstbestimmung, Datenschutz, regionale Betriebsfähigkeit und globale Zusammenarbeit zugleich ermöglichen. Eine zentrale Welt-Datenbank oder ein weltweiter Einzelcluster würden diese Ziele gegeneinander ausspielen.

## Entscheidung

commonThing skaliert über autonome Gewebe-Zellen.

Eine Gewebe-Zelle ist eine betriebliche und soziale Domäne mit:

- eigener Primärwahrheit,
- eigenen Mitgliedern und Objekten,
- eigener Moderation und Governance,
- eigener Infrastruktur oder klarer Mandantenisolation,
- expliziten Beziehungen zu anderen Zellen,
- lokaler Betriebsfähigkeit bei zeitweiser Trennung vom Gesamtnetz.

Zellen bleiben über globale Identitäten, stabile Objektadressen, signierte Ereignisse, direkte Nachbarschaftsbeziehungen und gemeinsame Räume verbunden.

## Kernregel

> Menschen und Informationen besitzen eine lokale Heimat, aber keine lokale Gefangenschaft.

## Eigentumsmodell

Jedes föderierte Objekt besitzt genau eine kanonische Ursprungszelle. Sie führt:

- kanonischen Zustand,
- Objektversion,
- Berechtigungen,
- Löschentscheidung,
- Konfliktentscheidung,
- Herkunft und Signatur.

Andere Zellen dürfen überprüfbare Kopien, Suchprojektionen, Caches und eigene Beziehungen zum Objekt halten.

## Reichweiten

- `private`: nur explizit Berechtigte,
- `local`: nur Heimatzelle,
- `neighbourhood`: ausgewählte verbundene Zellen,
- `global`: öffentlich föderierbar und indexierbar.

Zusätzliche Zielgruppen dürfen konkrete Zellen, gemeinsame Räume, direkte Beziehungen oder thematische Verbünde benennen.

## Zellbeziehungen

Zellbeziehungen sind erstklassige Objekte. Sie können ausdrücken:

- geografische Nachbarschaft,
- thematische Nähe,
- institutionelle Partnerschaft,
- gemeinsames Projekt,
- Vertrauensniveau,
- erlaubte Ereignisklassen,
- Import- und Exportregeln.

Föderation ist nicht automatisch vollständig. Jede Zelle entscheidet über Annahme, Quarantäne, Begrenzung und Blockierung.

## Gemeinsame Räume

Zellübergreifende Projekte, Gruppen, Veranstaltungen und Entscheidungen werden als gemeinsame Räume modelliert. Ihre Regeln bestimmen:

- Teilnehmer,
- erlaubte Ereignisse,
- Zuständigkeit,
- Moderation,
- Konfliktbehandlung,
- Aufbewahrung und Austritt.

Nicht jedes gewöhnliche Objekt erhält globale Mehrschreiber-Semantik.

## Nachbarschaft

Nachbarschaft ist nicht nur geografisch. commonThing führt getrennte Nähegraphen für soziale, thematische, ökologische, institutionelle, infrastrukturelle, zeitliche und bedarfsbezogene Nähe.

Die Produktoberfläche darf daraus Nachbarschaftshorizonte und ein Vorschlagsradar ableiten. Vorschläge begründen keine automatische Handlungsvollmacht.

## Globale Dienste

Globale Zellregister, Karten- und Suchindizes sind abgeleitete öffentliche Projektionen. Sie müssen austauschbar und rekonstruierbar sein. Ihr Ausfall darf lokale und direkte föderierte Nutzung nicht verhindern.

## Protokollgrenze

Die öffentliche Föderation verwendet ein versioniertes fachliches Protokoll. Sie setzt keinen gemeinsamen Kubernetes-Cluster, kein geteiltes internes Netzwerk und keinen direkten NATS-Zugriff voraus.

Das Protokoll muss mindestens definieren:

- Zell- und Akteursidentität,
- Schlüssel und Schlüsseldrehung,
- Ereignisumschlag,
- Objektadresse und Ursprung,
- Reichweite,
- Inbox und Outbox,
- Deduplikation,
- Aktualisierung und Löschung,
- Fehler, Wiederholung und Quarantäne,
- Versionsaushandlung.

## Sicherheitsgrenzen

- unbekannte Zellen beginnen in begrenztem Vertrauen,
- externe Ereignisse werden signiert und ratenbegrenzt,
- sensible Daten werden nicht allein durch eine Sichtbarkeitsbitte geschützt,
- lokale Moderation bleibt maßgeblich,
- Löschung gegenüber bösartigen Fremdsystemen ist nicht vollständig erzwingbar und darf nicht überbehauptet werden.

## Alternativen

### Zentrale globale Plattform

Verworfen als Grundmodell. Sie wäre einfacher zu starten, würde aber lokale Datenhoheit, unabhängigen Betrieb und Resilienz begrenzen.

### Weltweite synchrone Datenbank

Verworfen. Latenz, Partitionen, Eigentum und Konflikte würden zu einer globalen Schreibautorität zwingen.

### Vollständig voneinander isolierte Installationen

Verworfen. Sie würden lokale Souveränität erhalten, aber das zentrale Ziel globaler Beziehungen und gemeinsamer Handlungsräume verfehlen.

## Konsequenzen

- globale IDs, Ursprung und Reichweite werden früh im Fachmodell berücksichtigt,
- lokale Datenbankgrenzen bleiben sichtbar,
- Föderation wird nicht mit Infrastrukturvernetzung verwechselt,
- der erste Produktbeweis besteht aus zwei unabhängigen Testzellen,
- ein späterer Zelloperator wird aus realen Zellprofilen abgeleitet,
- Offline- und Partitionstoleranz werden als spätere, aber vorbereitete Fähigkeit behandelt.
