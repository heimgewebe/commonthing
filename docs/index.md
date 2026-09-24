---
id: docs.index
title: commonThing - Doku-Index
doc_type: index
status: active
canonicality: navigation
lifecycle_state: active
summary: Navigationsindex zu den aktuellen Verträgen, Betriebsdokumenten, Arbeitsplänen und historischen Belegen.
---
# commonThing – Doku-Index

> Dieser Index ist Navigation, keine eigenständige Wahrheit. Bei Konflikten gilt die Rangfolge aus `repo.meta.yaml`. Kanonische Dokumente sind ausschließlich die in `manifest/repo-index.yaml` registrierten Dateien.

## In fünf Minuten

1. [Vision](vision.md) – wozu commonThing dient.
2. [commonThing OS](../architecture/commonthing-os.md) – verbindliche langfristige Zielarchitektur.
3. [Architektur](../architecture/overview.md) – was heute im System existiert.
4. [Garnrolle, Knoten und Faden](specs/garnrolle-knoten-faden.md) – Produktdomäne.
5. [Föderationskern](specs/federation-core.md) – Zell-, Ursprungs- und Reichweiteninvarianten.
6. [UI-Interaktionsvertrag](specs/ui-interaction.md) und [Zustandsmaschine](specs/ui-state-machine.md) – Bedienlogik.
7. [Kartenerlebnis](specs/map-experience.md) – Kartenwahrheit und Darstellung.
8. [Runtime](../runtime/README.md) und [Runbooks](../runbooks/README.md) – Betrieb.

## Kanonische Produktverträge

- [Garnrolle, Knoten und Faden](specs/garnrolle-knoten-faden.md)
- [UI-Interaktionsvertrag](specs/ui-interaction.md)
- [UI-Zustandsmaschine](specs/ui-state-machine.md)
- [Kartenerlebnis](specs/map-experience.md)
- [Föderationskern](specs/federation-core.md)

## Weitere aktive Spezifikationen

Diese Dokumente präzisieren Teilbereiche, sind aber nicht als eigene kanonische Zone im Manifest registriert.

- [Auth UI](specs/auth-ui.md)
- [Auth API](specs/auth-api.md)
- [Garnrollen-Sichtbarkeit API](specs/privacy-api.md)
- [Domänenvokabular](domain/vocabulary.md)

## Architektur und Daten

- [commonThing OS](../architecture/commonthing-os.md)
- [Architecture Overview](../architecture/overview.md)
- [Security Architecture](../architecture/security.md)
- [Semantic Search v1](../architecture/semantic-search.md)
- [Techstack](techstack.md)
- [Datenmodell](datenmodell.md)
- [Repositorystruktur](architekturstruktur.md)
- [ADRs](adr/)

## Planung und Umsetzung

Roadmaps und Blaupausen beschreiben Arbeit oder mögliche Zielbilder. Sie sind nicht automatisch aktuelle Produktwahrheit.

- [Master-Roadmap](roadmap.md)
- [commonThing-OS-Masterplan](blueprints/weltgewebe-os-masterplan.md)
- [commonThing-OS-Foundation-Status](reports/weltgewebe-os-foundation-status.md)
- [Auth-Roadmap](blueprints/auth-roadmap.md)
- [Domain-PostgreSQL-Cutover](blueprints/domain-data-postgres-cutover.md)
- [Basemap-Architektur](blueprints/map-blaupause.md)
- [Versionierung](blueprints/versionierungs-blaupause.md)
- [Task-Control](tasks/README.md)
- [Task Board](tasks/board.md)

## Betrieb

- [Runtime Reality](../runtime/README.md)
- [Runbooks](../runbooks/README.md)
- [VPS-Deployment](deploy/vps.md)
- [Deploymentübersicht](deploy/README.md)
- [Incident Response](runbooks/incident-response.md)
- [DB Recovery](runbooks/db-recovery.md)

## Belege und Diagnosen

Berichte und Proofs belegen einen bestimmten Stand und dürfen aktuelle Verträge oder Livebeobachtung nicht überstimmen.

- [Kartenstatus](reports/map-status.md)
- [Auth Status Matrix](reports/auth-status-matrix.md)
- [Optimierungsstatus](reports/optimierungsstatus.md)
- [Proofs](proofs/)
- [Reports](reports/)

## Dokumentationssystem

- [Docmeta-Vertrag](../architecture/docmeta.schema.md)
- [Agent Reading Protocol](policies/agent-reading-protocol.md)
- [Report Lifecycle](process/report-lifecycle.md)
- [Generierte Systemkarte](_generated/system-map.md)
- [Dokumentenindex](_generated/doc-index.md)
- [Ablösungskarte](_generated/supersession-map.md)
- [Staleness-Report](_generated/staleness-report.md)

## Historische Produkttexte

`docs/inhalt.md`, `docs/zusammenstellung.md`, `docs/geist-und-plan.md` sowie abgelöste UI- und Kartenblaupausen bleiben als Entstehungs- und Entscheidungsbelege erhalten. Sie sind mit `status: deprecated` markiert und nicht Teil der aktiven Lesereihenfolge.
