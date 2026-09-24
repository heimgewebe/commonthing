---
id: docs.blueprints.weltgewebe-os-masterplan
title: commonThing OS — Masterplan
doc_type: roadmap
status: active
summary: >
  Bündelt Produktverfassung, föderierte Zellen, Kubernetes-Plattform, Ereignisrückgrat, Grabowski-Betrieb und die beweisgebundene Umsetzungsreihenfolge.
relations:
  - type: depends_on
    target: architecture/commonthing-os.md
  - type: relates_to
    target: docs/roadmap.md
  - type: relates_to
    target: docs/adr/ADR-0010__kubernetes-kanonische-plattform.md
  - type: relates_to
    target: docs/adr/ADR-0011__foederierte-gewebezellen.md
  - type: relates_to
    target: docs/adr/ADR-0012__ereignisrueckgrat-transactional-outbox.md
  - type: relates_to
    target: docs/reports/weltgewebe-os-foundation-status.md
  - type: relates_to
    target: docs/tasks/board.md
---

# commonThing OS — Masterplan

## 1. Ziel

commonThing wird als föderiertes Betriebssystem gesellschaftlicher Koordination entwickelt:

```text
lokale autonome Gewebe-Zellen
+ globale Identitäten und Beziehungen
+ versionierte Ereignisse
+ gemeinsame Räume
+ Kubernetes-native Laufzeit
+ Grabowski als kontrollierter Operator
= commonThing OS
```

Das Ziel ist kein zentral beherrschtes Weltportal. Viele unabhängige Zellen sollen lokale Wahrheit, Regeln und Betriebsfähigkeit behalten und trotzdem Nachbarschaft, Projekte und öffentliche Zusammenhänge über Zellgrenzen hinweg bilden können.

## 2. Leitentscheidung

Die Planung maximiert zukünftige Funktionalität, ohne heutige Komplexität blind zu maximieren.

> Was später einen invasiven Umbau erfordern würde, wird früh vorbereitet. Was später additiv eingeschaltet werden kann, wird erst durch reale Anforderungen und Belege aktiviert.

### Früh bindend

- globale Objekt- und Identitätsadressen,
- Ursprung und Provenienz,
- explizite Reichweite,
- Multi-Instanz-Korrektheit,
- persistente Auth-Zustände,
- Transactional Outbox,
- versionierte Domain-Ereignisse,
- idempotente Konsumenten,
- deklarative Plattformverträge,
- Observability,
- portable Backups und Restore-Proofs,
- föderierbare Fachgrenzen.

### Später additiv

- weitere Regionen und Betreiber,
- globale Such- und Kartenindizes,
- eigene GewebeZelle-API,
- Edge- und Offlinezellen,
- lokale KI und Beschleuniger,
- CRDTs für nachgewiesene Objektklassen,
- zusätzliche Identitätsportabilität.

## 3. Produktvision

### 3.1 Nachbarschaftshorizont

Die Karte zeigt abhängig von Maßstab und Kontext nicht nur mehr oder weniger Marker, sondern unterschiedliche Bedeutungsebenen:

- lokal: konkrete Orte, Garnrollen, Bedarfe, Angebote und Handlungen,
- regional: Projekte, Bewegungen, Versorgungsnetze und offene Bedarfscluster,
- überregional: Zellbeziehungen, Ressourcenflüsse und gemeinsame Räume,
- global: Föderationslandschaft, Themen, Brücken und große Zusammenhänge.

### 3.2 Nachbarschaftsradar

Das System kann mögliche Fäden vorschlagen, wenn beispielsweise:

- ein Bedarf zu einem Angebot in einer Nachbarzelle passt,
- zwei Projekte dasselbe Problem bearbeiten,
- eine ungenutzte Ressource regional gesucht wird,
- Wissen aus einer Zelle ein Problem in einer anderen lösen kann.

Vorschläge sind erklärbar und begründen keine automatische Handlungsvollmacht.

### 3.3 Gemeinsame Räume

Menschen aus mehreren Zellen können Projekte, Veranstaltungen, Entscheidungen, Dokumente, Ressourcen und Aufgaben gemeinsam verwalten. Gemeinsame Räume besitzen explizite Regeln und sind nicht mit globaler Beliebig-Editierbarkeit gleichzusetzen.

### 3.4 Vertrauenssichtbarkeit

Oberflächen zeigen Ursprung, Aktualität, Objektversion, Vertrauenspfad, Moderationskontext und widersprechende Quellen, statt fremde Projektionen als undifferenzierte Wahrheit darzustellen.

### 3.5 Persönlicher Mitweber

Eine Garnrolle kann später einen persönlichen digitalen Mitweber autorisieren. Er darf zusammenfassen, Vorschläge erklären, Routinevorgänge vorbereiten und begrenzte Aufträge übergeben. Rechte, Datenzugriff, Zeit, Kosten und Wirkung bleiben explizit.

### 3.6 Offlinefähige Zellen

Kleine Zellen sollen später lokale Karte, Suche, Kommunikation und Ereignisaufnahme zeitweise ohne Außenverbindung betreiben und kontrolliert synchronisieren können.

## 4. Zielarchitektur

### 4.1 Zellen statt Weltcluster

Regionale, institutionelle oder thematische Zellen bilden die primären Betriebs- und Governancegrenzen. Beispiele sind Hamburg, Schleswig-Holstein, eine Kommune, Universität, Freifunk-Region oder Initiative.

### 4.2 Lokale Wahrheit

PostgreSQL/PostGIS führt den lokalen kanonischen Zustand. Föderation repliziert keine globale Schreibdatenbank, sondern überträgt signierte, versionierte und reichweitengebundene Ereignisse und Projektionen.

### 4.3 Ereignisrückgrat

Fachmutation und Outbox-Eintrag sind atomar. JetStream trägt Projektionen, Benachrichtigungen, Chronik, Föderation und spätere Agentenarbeit. Konsumenten sind idempotent und replayfähig.

### 4.4 Plattform

Kubernetes wird kanonische Zielplattform. Flux, Gateway API, Cilium/Hubble, OpenTelemetry und Policy as Code bilden den bevorzugten frühen Plattformkern. Konkrete Implementierungen bleiben durch Messung und Betriebsbelege revidierbar.

### 4.5 Grabowski

Grabowski koordiniert den vollständigen Regelkreis von Absicht über isolierte Änderung und Review bis GitOps-Rollout, Laufzeitbeobachtung, Wirkungsbeleg und Rücknahme.

## 5. Umsetzungswellen

## Welle 0 — Verfassung und Entscheidungsverträge

**Ziel:** Das Zielbild wird kanonisch und prüfbar.

**Ergebnisse:**

- `architecture/commonthing-os.md`,
- ADRs zu Kubernetes, Zellen und Ereignisrückgrat,
- dieser Masterplan,
- belegter Statusbericht,
- Bureau-Gesamtinitiative und abhängige Systemaufträge.

**Gate:** Neue Architekturentscheidungen müssen Widersprüche explizit per ADR behandeln.

## Welle 1 — Multi-Instanz-Wahrheit

**Ziel:** Die API wird fachlich horizontal skalierbar.

**Arbeit:**

- Inventar aller prozesslokalen Zustände,
- gemeinsame Persistenz aller autoritativen Auth-Zwischenzustände,
- abgeleitete Domain-Caches oder direkte DB-Reads,
- Transactional Outbox,
- idempotente Konsumenten,
- Cacheinvalidierung,
- Graceful Shutdown und Draining,
- Expand/Migrate/Contract-Migrationen,
- Zwei-API-Kohärenztest.

**Gate:** Neustart, Podwechsel, Parallelität, verzögerte und doppelte Ereignisse erzeugen keine divergierende Fachwahrheit.

## Welle 2 — Kubernetes-native Grundlage

**Ziel:** Eine portable Plattformwahrheit.

**Arbeit:**

- `platform/`-Struktur,
- lokale Kind- oder K3d-Referenz,
- Kustomize-Basis und Overlays,
- digestgebundene Images,
- Flux,
- Gateway API,
- Cilium/Hubble,
- OpenTelemetry,
- Policy as Code,
- Secret-Vertrag,
- Preview-Umgebungen,
- gleicher Manifestsatz in CI und Staging.

**Gate:** Ein leerer lokaler Cluster ist reproduzierbar aus versionierten Artefakten aufbaubar.

## Welle 3 — Hochverfügbare Referenzzelle

**Ziel:** Gemessene Hochverfügbarkeit, nicht nur mehrere Pods.

**Arbeit:**

- mehrere API-Replikate,
- repliziertes PostgreSQL,
- dreiknotiges JetStream,
- Fehlerdomänen,
- Backups und PITR,
- SLO-Dashboards,
- kontrollierte Upgrades,
- Ausfall- und Chaosprüfungen,
- Blank-Cluster-Restore.

**Gate:** RTO, RPO, Rollback und Nichtverlust fachlicher Mutationen sind gemessen.

## Welle 4 — Föderationskern

**Ziel:** Sicheres Protokoll für Zellen, Identitäten und Objekte.

**Arbeit:**

- Zellidentität und Schlüsseldrehung,
- globale Adressen,
- signierter Ereignisumschlag,
- Reichweitenmodell,
- Inbox/Outbox,
- Deduplikation,
- Aktualisierung und Löschung,
- Vertrauensstufen,
- Quarantäne,
- Protokollversionierung,
- Konformitätstests.

**Gate:** Eine isolierte zweite Testzelle kann öffentliche Objekte sicher lesen, referenzieren und Aktualisierungen empfangen.

## Welle 5 — Zwei-Zellen-Nachbarschaft

**Ziel:** Erster echter föderierter Produktbeweis, bevorzugt Hamburg und Schleswig-Holstein.

**Arbeit:**

- Zellbeziehung,
- geografische und thematische Nähe,
- zellübergreifende Knoten und Fäden,
- gemeinsamer Projektraum,
- nachbarschaftliche Suche,
- Nachbarschaftsradar,
- lokale Moderation,
- Betrieb bei zeitweiser Trennung.

**Gate:** Beide Zellen arbeiten unabhängig weiter und konvergieren nach Wiederverbindung ohne stille Datenverluste.

## Welle 6 — Globale öffentliche Projektionen

**Ziel:** Globale Sicht ohne zentrale Herrschaft.

**Arbeit:** Zellregister, Kartenindex, Suche, Themenindizes, unabhängige Indexbetreiber, Widerruf, Ablauf und Provenienzsicht.

**Gate:** Ausfall eines Indexes blockiert keine lokale oder direkte föderierte Nutzung.

## Welle 7 — GewebeZelle als Plattformprodukt

**Ziel:** Neue Zellen werden deklarativ und reproduzierbar erzeugt.

**Arbeit:** stabile Zellprofile, SLO-Klassen, Upgradepfade, Betreiberverträge und erst danach eine eigene GewebeZelle-API oder einen Operator.

**Gate:** Eine Testzelle kann erzeugt, aktualisiert und wiederhergestellt werden.

## Welle 8 — Grabowski-Rechengewebe

**Ziel:** Agenten- und Batcharbeit wird kontrolliert verteilt.

**Arbeit:** isolierte Jobs, Kueue-Quoten, Prioritäten, Kostenbudgets, regionale Ausführung und optionale Beschleuniger.

**Gate:** Kein Auftrag kann seine Ressourcen-, Daten-, Netz- oder Autoritätsgrenzen überschreiten.

## Welle 9 — Edge- und Offlinegewebe

**Ziel:** Kleine souveräne Zellen für Gemeinschaften, Freifunk und schwache Netze.

**Gate:** Definierter Offlinebetrieb und kontrollierter späterer Abgleich sind belegt.

## 6. Systemausrichtung im lokalen Ökosystem

| System | Rolle im Zielbild | Verbindliche Ausrichtung |
|---|---|---|
| commonThing | Produkt- und Fachkern | Zellen, Identitäten, Knoten, Fäden, Räume, Reichweiten, Föderation |
| Grabowski | autonomer, kontrollierter Operator | vollständiger Regelkreis, GitOps, SLO- und Recoverybelege, isolierte Agentenjobs |
| Bureau | Verpflichtungs- und Aufgabenwahrheit | Initiative, Abhängigkeiten, Claims, Akzeptanz und Abschlussbelege |
| Konvergenzregelkreis | allgemeiner Regelkreisvertrag | portable Soll-Ist-Wirkungs- und Rücknahmesemantik, ohne commonThing-Fachwahrheit zu duplizieren |
| Chronik | Ereignis- und Wirkungsverlauf | versionierte Receipts, Herkunft, Deployment- und Föderationsereignisse |
| Systemkatalog | Auffindbarkeit und Rollenwahrheit | commonThing-OS-Komponenten, Zellen, Einstiegspunkte und Beziehungen sichtbar machen |
| Leitstand | operative Sicht | SLOs, Deployments, Föderationszustand, Fehlerbudgets und offene Verpflichtungen |
| Schauwerk | visuelle Erklärbarkeit | Systemkarte, Zellgraph, Ereignisfluss, Nachbarschaft und Betriebszustand |
| Commonworld | globale Erfahrungs- und Erkundungsschicht | öffentliche Welt- und Zellprojektionen, keine zweite Domänenwahrheit |
| RepoBrief/Lenskit | Code- und Änderungskontext | Call Graph, Impact, Tests und Kontext für sichere große Änderungen |
| Heimlern/Vibe-Lab | Lern- und Wirkungsauswertung | nur belegte Outcomes; keine operative Wahrheitskonkurrenz |

Jedes System behält eine klare Rolle. Funktionsduplikate werden nicht durch Kopieren des commonThing-Modells geschaffen, sondern durch Verträge und Projektionen verbunden.

## 7. Erste Initiativekette

1. Verfassung und ADRs veröffentlichen.
2. Multi-Instance-State-Audit ausführen.
3. Shared Auth State schließen.
4. Transactional Outbox und erstes Ereignisschema implementieren.
5. Zwei-API-Kohärenz beweisen.
6. lokale Kubernetes- und GitOps-Grundlage schaffen.
7. Observability Spine verbinden.
8. HA-Referenzzelle aufbauen.
9. Föderationsprotokoll v0 spezifizieren und testen.
10. Zwei-Zellen-Pilot durchführen.
11. Ökosystemprojektionen und Operatorflächen nachziehen.
12. erst aus realen Profilen eine GewebeZelle-Plattform-API ableiten.

## 8. Verbotene Abkürzungen

- Multi-Instance-Guard ohne gleichwertigen Kohärenzbeweis entfernen oder schwächen,
- heutigen Compose-Stack unverändert nach Kubernetes verschieben,
- Compose und Kubernetes als unabhängige Wahrheiten pflegen,
- einen Einzelhost als hochverfügbar bezeichnen,
- eine globale Multi-Primary-Datenbank einführen,
- interne NATS- oder Clusterverbindungen als öffentliches Föderationsprotokoll verwenden,
- einen eigenen Operator vor stabilen Zellprofilen bauen,
- globale Suche mit unbeschränkter Datensammlung gleichsetzen,
- Agentenreichweite aus technischer Zugriffsmöglichkeit ableiten.

## 9. Erfolgskriterien

### Produkt

- sinnvolle lokale und zellübergreifende Fäden,
- erfolgreiche Bedarfs-Angebots-Verbindungen,
- gemeinsame Projekte und Räume,
- unabhängige aktive Zellen.

### Föderation

- Zustellerfolg und Konvergenzzeit,
- Signatur- und Quarantänebefunde,
- Aktualisierungs- und Löschlatenz,
- Zahl unabhängiger kompatibler Betreiber.

### Betrieb

- Verfügbarkeit und Fehlerbudget,
- gemessenes RTO/RPO,
- Restore- und Rollbackerfolg,
- Driftzeit,
- reproduzierbare Deployments.

### Grabowski

- autonom abgeschlossene, belegte Vorgänge,
- verhinderte Scopeverletzungen,
- Zeit bis zum Wirkungsbeleg,
- offene Verpflichtungen und Alter,
- Kosten pro belegtem Ergebnis.

## 10. Gegenwartsgrenze

Dieser Masterplan ist eine verbindliche Richtung und Arbeitsordnung. Er behauptet nicht, dass Kubernetes, Multi-Instanz-Kohärenz, HA oder Föderation bereits produktiv vorhanden sind. Der aktuelle belegte Stand steht in `docs/reports/weltgewebe-os-foundation-status.md` und den jeweiligen spezialisierten Statusberichten.
