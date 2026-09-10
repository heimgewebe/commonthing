---
id: docs.techstack
title: Techstack
doc_type: architecture
status: active
summary: Tatsächlich vorhandener Technologie-Stack, getrennt nach Betrieb, Implementierung und Planung.
relations:
  - type: relates_to
    target: architecture/overview.md
  - type: relates_to
    target: docs/architekturstruktur.md
  - type: relates_to
    target: docs/datenmodell.md
  - type: relates_to
    target: architecture/weltgewebe-os.md
  - type: relates_to
    target: docs/adr/ADR-0010__kubernetes-kanonische-plattform.md
---

# Techstack

Dieses Dokument trennt vier Zustände. „Im Repository vorhanden“ bedeutet nicht
automatisch „produktiv betrieben“.

## Heute implementiert und kanonisch

### Web

- SvelteKit 2 und Svelte 5
- TypeScript und Vite
- statischer Adapter
- MapLibre GL und PMTiles
- Playwright und Vitest
- pnpm 9 mit versioniertem Lockfile

### API

- Rust 2021
- Axum 0.8 und Tokio
- sqlx für PostgreSQL
- WebAuthn über `webauthn-rs`
- SMTP über `lettre`
- Prometheus-Metriken

`rust-version = 1.85.0` beschreibt die minimale Cargo-Kompatibilität des
API-Pakets. Die repositoryweit gepinnte Entwicklungs-/CI-Toolchain kann neuer
sein. Beide Angaben sind verschiedene Verträge und dürfen nicht gleichgesetzt
werden.

### Datenhaltung

- PostgreSQL als Produktionswahrheit für Accounts/Garnrollen, Knoten, Fäden,
  Sitzungen, Passkey-Credentials und Migrationen
- JSONL als lokaler, Legacy-, Import-/Export- und expliziter Rollbackpfad
- fail-closed Lese- und Schreibschalter, damit keine versteckte Doppelwahrheit
  entsteht
- kein allgemeiner Transactional-Outbox-Pfad
- kein vollständig belegtes PostGIS-Modell; Koordinaten liegen derzeit als
  `DOUBLE PRECISION` vor

### Delivery

- Docker und Docker Compose
- Caddy als Frontdoor und Reverse Proxy
- VPS-Ziel `commonserver`
- statische interne Caddy-Auslieferung von `apps/web/build`
- Cloudflare- und Vercel-Status als zusätzliche Build-/Vorschaubelege

### Qualität und Sicherheit

- GitHub Actions mit Rust-, Web-, Datenbank-, Auth-, Proxy-, Basemap- und
  CodeQL-Prüfungen
- externe GitHub Actions und reusable Workflows sind auf 40-stellige Commit-SHAs
  gepinnt; lokale Actions und `docker://` folgen der im Guard dokumentierten
  Ausnahmepolicy
- `cargo deny`
- Domain-Contract-Validierung
- Security-Header-Policy in `policies/security.yml`, statisch geprüft gegen
  produktionsrelevante Caddyfiles
- Caddy-Adaptions- und Compose-Vorprüfungen
- logische PostgreSQL-Backups und Restore-Proofs über `scripts/ops/`
- risikogewichtetes, head- und diffgebundenes Selbstreview vor Merge

## Im Repository vorhanden, aber nicht allgemein produktiv belegt

- NATS im Produktions-Compose und API-Verbindungsfläche
- PgBouncer im Core-Profil
- Prometheus-Konfiguration und optionale Observability-Profile
- PWA-/Offline-nahe Webbestandteile
- Rollencontract sowie private und föderierte Gesprächstypen

Für diese Punkte ist jeweils ein eigener Runtime- oder Ende-zu-Ende-Beleg nötig.

## Kanonische Zielplattform, noch nicht als Runtime belegt

- Kubernetes als Primärorchestrierung für Staging und Produktion
- GitOps-Reconciliation und unveränderliche Images
- Gateway API, Network Policies und beobachtbare Dienstflüsse
- portable PostgreSQL-HA-, Backup- und Restore-Verträge
- NATS JetStream mit Transactional Outbox und idempotenten Konsumenten
- autonome Gewebe-Zellen mit fachlicher Föderation

Diese Punkte sind durch `architecture/weltgewebe-os.md` und ADR-0010 bis ADR-0012 als Zielrichtung entschieden. Sie sind noch keine Behauptung über eine laufende Produktionsruntime. Compose bleibt bis zum belegten Cutover der reale Betriebsweg und ein begrenzter Entwicklungs-/Recoverypfad.

## Geplant oder noch unvollständig

- weitere Vereinfachung des Garnrollen-Produktflusses
- private Nachrichten, Anhänge und föderierte Gespräche
- föderierte Identitäten, Zellbeziehungen und gemeinsame Räume
- normalisierte Geoabfragen beziehungsweise PostGIS
- verlässliche Eventprojektionen und Transactional Outbox
- Multi-Instanz-Kohärenz durch Shared State, Projektionsgeneration, Outbox und Multi-Instance-Guard
- konsolidierte Observability mit definierten SLOs
- WAL-/PITR- oder Object-Lock-Backupstrategie, falls sie betrieblich benötigt wird

## Nicht als heutige Betriebswahrheit belegt

Folgende Fähigkeiten oder Technologien dürfen trotz Zielentscheidung nicht als bereits betrieben bezeichnet werden:

- Kubernetes-Produktionsbetrieb
- Multi-AZ- oder Multi-Region-Betrieb
- KeyDB
- Typesense oder MeiliSearch
- Debezium
- Nomad als neue Primärzielrichtung
- Loki/Tempo als vollständig betriebene Plattform
- automatische Cosign-/SLSA-Attestierung aller Artefakte
- automatisierte Rotation sämtlicher Schlüssel

## Entscheidungsregel

Eine konkrete Implementierung wird erst Teil der betriebenen Runtime, wenn

1. sie der kanonischen Weltgewebe-OS-Architektur entspricht,
2. ein konkretes Produkt- oder Betriebsproblem belegt ist,
3. der einfachere vorhandene Stack nicht genügt oder spätere invasive Migrationen verhindert werden müssen,
4. Betriebs- und Wiederherstellungspfad definiert sind,
5. Tests, Ownership und Wirkungsevidenz existieren,
6. die Änderung in Code und Zielruntime nachgewiesen ist.
