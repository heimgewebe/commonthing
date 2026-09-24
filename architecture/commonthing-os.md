---
id: architecture.commonthing-os
title: commonThing OS — kanonische Zielarchitektur
summary: Verbindliche langfristige Architektur für ein föderiertes, lokal souveränes und global verbundenes Koordinationssystem.
role: norm
organ: governance
status: canonical
canonicality: normative
lifecycle_state: active
owner: governance
review_after: 2026-10-15
last_reviewed: 2026-07-15
depends_on:
  - overview
relations:
  - type: supersedes
    target: architecture/weltgewebe-os.md
  - type: relates_to
    target: docs/adr/ADR-0010__kubernetes-kanonische-plattform.md
  - type: relates_to
    target: docs/adr/ADR-0011__foederierte-gewebezellen.md
  - type: relates_to
    target: docs/adr/ADR-0012__ereignisrueckgrat-transactional-outbox.md
  - type: relates_to
    target: docs/blueprints/weltgewebe-os-masterplan.md
  - type: relates_to
    target: docs/reports/weltgewebe-os-foundation-status.md
verifies_with: []
---

# commonThing OS

## 1. Zweck

commonThing wird als offene gesellschaftliche Betriebsschicht entwickelt. Es ersetzt keine Gerätebetriebssysteme. Es verbindet Menschen, Orte, Gemeinschaften, Wissen, Ressourcen, Bedarfe, Angebote, Vorhaben, Regeln und kontrollierte digitale Werkzeuge zu einem handlungsfähigen Gewebe.

Die Leitformel lautet:

> Globale Beziehungen. Lokale Wahrheit. Gemeinsame Handlungsfähigkeit.

Diese Zielarchitektur ist bindend für neue Produkt-, Daten-, Plattform- und Betriebsentscheidungen. Sie behauptet nicht, dass der heutige Laufzeitstand das Ziel bereits erreicht.

## 2. Verfassungsprinzipien

### 2.1 Lokale Heimat, keine lokale Gefangenschaft

Jede Identität und jedes kanonische Objekt besitzt eine Heimatzelle oder Ursprungszelle. Beziehungen und gemeinsame Vorhaben dürfen Zellgrenzen überschreiten.

### 2.2 Die Quelle führt

Jedes kanonische Objekt besitzt mindestens:

- eine stabile globale Adresse,
- eine Ursprungszelle,
- eine Version,
- eine Provenienz,
- einen Verantwortungsbereich,
- Änderungs-, Lösch- und Konfliktregeln.

Fremde Zellen dürfen überprüfbare Kopien, Caches und Projektionen halten. Sie dürfen nicht still eine zweite kanonische Wahrheit erzeugen.

### 2.3 Föderation ist freiwillige Verbindung

Eine Zelle entscheidet selbst:

- mit welchen Zellen sie Beziehungen eingeht,
- welche Ereignisklassen sie importiert oder exportiert,
- welche Inhalte sie indexiert,
- welche Regeln für eingehende Interaktionen gelten,
- welche Identitäten oder Zellen begrenzt oder blockiert werden.

### 2.4 Privatheit ist eine Architekturgrenze

Sensible Daten bleiben grundsätzlich in der Heimatzelle. Föderiert werden nur explizit freigegebene Daten und Ereignisse. Sichtbarkeit ist fachlicher Vertragsbestandteil, nicht nachträgliche UI-Dekoration.

### 2.5 Globale Dienste sind ableitbar

Globale Suche, Kartenindizes, Zellverzeichnisse und öffentliche Themenprojektionen dürfen nützlich sein, aber keine unersetzliche Primärwahrheit halten. Ihr Ausfall darf lokale Nutzung und direkte Zellbeziehungen nicht blockieren.

### 2.6 Deklarativer Sollzustand vor manueller Laufzeit

Produktionszustände werden aus versionierten, geprüften Verträgen erzeugt. Manuelle Laufzeitänderungen dürfen keine dauerhafte zweite Wahrheit bilden.

### 2.7 Wirkung braucht Belege

Eine Änderung gilt nicht allein wegen eines erfolgreichen Befehls als wirksam. Relevante Vorgänge benötigen Belege zu Sollzustand, Version, Laufzeitwirkung, Tests, SLOs, Wiederherstellung und möglicher Rücknahme.

### 2.8 Automatisierung unterliegt derselben Verfassung

Grabowski und andere Agenten erhalten nur explizite, begrenzte Autorität. Technische Erreichbarkeit begründet keine fachliche oder organisatorische Zuständigkeit.

## 3. Elementare Fachobjekte

### Garnrolle

Souveräne Ausgangsstelle einer Person, Gruppe, Organisation oder eines anderen Akteurs. Sie bündelt Identität, Fähigkeiten, Interessen, Beziehungen, Angebote, Bedarfe, Mitgliedschaften und ausdrücklich autorisierte Automatisierungen.

### Knoten

Global adressierbarer Gegenstand der gemeinsamen Welt, beispielsweise Ort, Projekt, Gruppe, Veranstaltung, Ressource, Bedarf, Angebot, Infrastruktur, Dokument, Wissen, Entscheidung oder öffentlicher Dienst.

### Faden

Bedeutungstragende Beziehung oder Vorgang zwischen Garnrollen und Knoten. Beispiele sind `bietet`, `benötigt`, `betreut`, `gehört zu`, `arbeitet mit`, `vertraut`, `blockiert`, `beschließt` oder `ersetzt`.

### GewebeZelle

Autonome betriebliche und soziale Domäne mit eigener Datenhoheit, Moderation, Infrastruktur oder Isolation, Regeln und Föderationsbeziehungen.

### GewebeIdentität

Global adressierbare Identität mit lokaler Heimat. Identität, öffentliche Profildaten und vertrauliche Kontodaten werden getrennt modelliert. Ein kontrollierter späterer Zellwechsel muss architektonisch möglich bleiben.

### GewebeZellBeziehung

Explizite Beziehung zwischen Zellen. Sie kann geografische Nachbarschaft, institutionelle Partnerschaft, thematische Nähe, gemeinsames Projekt, Vertrauensniveau oder erlaubte Ereignisklassen ausdrücken.

### FöderiertesObjekt

Objekt mit kanonischem Zustand in einer Ursprungszelle, das von anderen Zellen referenziert, angezeigt, durchsucht oder in erlaubter Form beantwortet werden darf.

### GemeinsamerRaum

Zellübergreifender Handlungsraum für Projekte, Gruppen, Veranstaltungen, Abstimmungen, Ressourcen und gemeinsame Aufgaben.

### Auftrag und Beleg

Expliziter Auftrag an einen Menschen oder Agenten und dessen strukturierter Wirkungsbeleg. Auftrag, Autorität, Scope, Ergebnis, Auswirkungen, offene Verpflichtungen und Wiederherstellungspfad bleiben unterscheidbar.

## 4. Reichweite und Nähe

Jedes föderierbare Objekt oder Ereignis besitzt eine explizite Reichweite:

| Reichweite | Bedeutung |
|---|---|
| privat | nur ausdrücklich Berechtigte |
| lokal | nur Heimatzelle |
| nachbarschaftlich | ausgewählte verbundene Zellen |
| global | öffentlich föderierbar und indexierbar |

Nähe wird als Mehrfachgraph modelliert:

- geografisch,
- sozial,
- thematisch,
- institutionell,
- ökologisch,
- infrastrukturell,
- zeitlich,
- bedarfsbezogen.

Damit kann fachliche oder soziale Nähe wichtiger sein als reine Entfernung.

## 5. Drei getrennte Verbindungsnetze

### Infrastrukturverbund

Zwischen Clustern desselben Betreibers für Monitoring, Backups, Disaster Recovery, administrative Dienste und kontrollierte interne Serviceexports.

### Ereignisverbund

Zwischen eng vertrauten Nachbarzellen für ausgewählte Domain-Ereignisse, regionale Projektionen und Kapazitätsvermittlung. Interne Subjects und Netze werden nicht pauschal freigegeben.

### Öffentliche Fachföderation

Zwischen unabhängigen Betreibern für Identitäten, Knoten, Fäden, gemeinsame Räume, Aktivitäten, Moderations- und Löschereignisse. Diese Ebene verwendet ein versioniertes fachliches Protokoll und setzt keinen internen Kubernetes- oder NATS-Zugriff voraus.

## 6. Daten- und Ereignismodell

### 6.1 Lokale Primärwahrheit

PostgreSQL mit PostGIS bleibt die kanonische lokale Datenbasis einer Zelle. Es gibt keine globale Multi-Primary-Datenbank als Grundmodell.

### 6.2 Transactional Outbox

Jede projektions- oder föderationsrelevante Mutation schreibt Fachzustand und Outbox-Ereignis in derselben Datenbanktransaktion. Erst danach veröffentlicht ein Relay das Ereignis.

### 6.3 Idempotente Konsumenten

Ereignisse dürfen mehrfach zugestellt werden, aber nur einmal fachlich wirken. Ereignis-ID, Schema-Version, Ursprung, Objektversion und Deduplikationsregeln sind verpflichtend.

### 6.4 Caches sind ableitbar

Prozesslokale Caches dürfen keine alleinige fachliche Autorität besitzen. Sie müssen aus gemeinsamer Wahrheit rekonstruierbar und über geprüfte Mechanismen invalidierbar sein.

### 6.5 Gemeinsame Bearbeitung

Nicht jedes Objekt ist global gemeinsam editierbar. Das Standardmodell ist Ursprungseigentum. Gemeinsame Räume, Vorschlagsverfahren oder CRDTs werden nur für explizit begründete Objektklassen eingeführt.

## 7. Plattformarchitektur

Kubernetes ist die kanonische Zielplattform für Staging und Produktion. Der heutige Compose-Betrieb bleibt reale Laufzeit und Recovery-/Kleinprofil, ist aber nicht mehr die langfristige Primärarchitektur.

Die Plattform folgt diesen Regeln:

- eine gemeinsame Anwendungsbasis mit kleinen Umgebungs-Overlays,
- unveränderliche, digestgebundene Images,
- GitOps-Reconciliation,
- Gateway API als kanonische Eingangsschicht,
- Network Policies und beobachtbare Dienstflüsse,
- Policy as Code,
- OpenTelemetry als Telemetrierückgrat,
- portable Daten-, Backup- und Restore-Verträge,
- keine Produktionsfreigabe ohne Wirkungstest und Rücknahmepfad.

Compose darf als generierter oder streng geprüfter Minimal-, Entwicklungs- und Recoverypfad erhalten bleiben. Manuell getrennt gepflegte, semantisch divergierende Compose- und Kubernetes-Wahrheiten sind verboten.

## 8. Multi-Instanz-Invariante

Der frühere Single-Instance-Guard wurde nach dem diffgebundenen Zwei-API-/Restart-Beweis durch den Multi-Instance-Guard ersetzt. Produktionsreplikation bleibt trotzdem ein eigener Plattform- und Betriebsentscheid.

Vor horizontaler API-Skalierung sind mindestens zu belegen:

- alle autoritativen Auth- und Domain-Zustände sind gemeinsam erreichbar,
- Caches sind abgeleitet und sicher invalidiert,
- parallele Schreibvorgänge bleiben korrekt,
- Neustart und Podwechsel verlieren keine Wahrheit,
- verzögerte und doppelte Ereigniszustellung erzeugen keine falsche Wirkung,
- zwei API-Instanzen bestehen denselben fachlichen Test gegen dieselbe Datenbank.

## 9. Grabowski als kontrollierter Operator

Grabowski bildet den ausführenden Regelkreis über Architektur, Repository, Plattform und Laufzeit:

```text
Absicht
→ Vertrag und Scope
→ isolierte Änderung
→ Tests und Review
→ versioniertes Artefakt
→ GitOps-Rollout
→ Laufzeitbeobachtung
→ Wirkungsbeleg
→ Abschluss, Nacharbeit oder Rücknahme
```

Agentenworkloads sollen später isoliert mit Ressourcen-, Daten-, Netz-, Zeit- und Kostenbudgets ausgeführt werden. Grabowski darf keine unklare Primärinstanz ernennen, Sicherheitsregeln umgehen oder nicht belegte Daten als Wahrheit behandeln.

## 10. Regionale und globale Gestalt

Das bevorzugte Skalierungsmodell sind autonome regionale oder institutionelle Gewebe-Zellen, nicht ein weltweiter Einzelcluster.

Beispiele:

- Hamburg,
- Schleswig-Holstein,
- Kommune,
- Universität,
- Verein,
- Freifunk-Verbund,
- thematische Initiative.

Zellen können lokale Dienste und Datenhoheit behalten und gleichzeitig direkte Nachbarschaft, gemeinsame Räume und globale öffentliche Projektionen nutzen.

## 11. Verbindliche Entscheidungsregel

> Invasive Grundlagen werden früh vorbereitet. Additive Fähigkeiten werden erst durch reale Anforderungen und Belege aktiviert.

Früh festzulegen sind globale IDs, Ursprung, Versionierung, Sichtbarkeit, Multi-Instanz-Korrektheit, Ereignisverträge, deklarative Plattformbeschreibung, Observability und Wiederherstellbarkeit.

Später aktivierbar sind weitere Regionen, globale Indizes, lokale KI, GPU-Börsen, Edge-Zellen, eigene Kubernetes-Operatoren, CRDTs und zusätzliche Identitätsmechanismen.

## 12. Verworfene Grundmuster

- unmittelbarer Lift-and-Shift des heutigen Compose-Stacks,
- ein weltweiter Kubernetes-Einzelcluster,
- globale Multi-Primary-PostgreSQL-Wahrheit,
- manuell doppelt gepflegte Plattformbeschreibungen,
- vollständiges Service Mesh ohne belegten Bedarf,
- eigener GewebeZelle-Operator vor stabilen realen Zellprofilen,
- Serverless für alle Workloads,
- Blockchain als allgemeine Wahrheitsgrundlage,
- Entfernung oder Schwächung des Multi-Instance-Guards ohne gleichwertigen Kohärenzbeweis.

## 13. Abgrenzung des heutigen Zustands

Heute belegt sind unter anderem Web, Rust/Axum-API, PostgreSQL-Persistenzpfade, NATS im Stack, Karten- und Produktflächen, die implementierten Multi-Instanz-Kohärenzpfade, Transactional Outbox sowie starke Agenten- und Task-Control-Grundlagen.

Noch nicht belegt sind Kubernetes-Produktionsbetrieb, eine über mehrere physische Hosts oder vergleichbar unabhängige Fehlerdomänen verteilte HA-Referenzzelle und öffentliche Zellföderation. Der aktuelle kind-Proof simuliert logische Zonen vollständig auf einem einzelnen CI-Host.

Diese verbleibende Lücke ist ausdrücklich und darf nicht durch Zielarchitektur- oder Statusberichte geglättet werden.
