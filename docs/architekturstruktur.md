---
id: docs.architecture.overview
title: Architekturüberblick
doc_type: architecture
status: active
summary: Aktuelle Repositorystruktur, Verantwortungsgrenzen und bewusst geplante Erweiterungen.
relations:
  - type: relates_to
    target: architecture/overview.md
  - type: relates_to
    target: docs/techstack.md
  - type: relates_to
    target: docs/datenmodell.md
  - type: relates_to
    target: architecture/commonthing-os.md
---

# Architektur und Repositorystruktur

Dieses Dokument erklärt, **wo** die heute vorhandenen Systemteile liegen. Die
Komponenten- und Datenflusswahrheit steht in
[`architecture/overview.md`](../architecture/overview.md); der tatsächliche
Technologiestand in [`docs/techstack.md`](techstack.md).

## Aktuelle Struktur

```text
weltgewebe/
├── apps/
│   ├── api/                 Rust-/Axum-API, Migrationen und API-Tests
│   └── web/                 SvelteKit-Webanwendung und Browserbeweise
├── architecture/            kurze kanonische Architektur- und Sicherheitskarten
├── contracts/domain/        JSON-Schema-Verträge für fachliche Objekte
├── configs/                 Anwendungsvorgaben
├── docs/                    Spezifikationen, Entscheidungen, Berichte und Planung
├── infra/
│   ├── caddy/               Frontdoor- und Proxyverträge
│   └── compose/             lokale, produktive und optionale Compose-Profile
├── policies/                technische Grenzwerte und Sicherheitsvorgaben
├── runbooks/                operative Einstiegskarten
├── runtime/                 Laufzeitvertrag ohne ungeprüfte Livebehauptung
├── scripts/                 Guards, Deploy-, Daten- und Entwicklungswerkzeuge
├── tests/                   repositoryweite Tests
├── .github/workflows/       CI-, Proof- und Sicherheitsabläufe
├── repo.meta.yaml           Wahrheits- und Discoveryvertrag
└── README.md                Einstieg und aktueller Gesamtzustand
```

## Verantwortungen

### `apps/web`

- statisch baubare SvelteKit-Anwendung,
- Karte mit MapLibre und PMTiles,
- Login-, Einstellungs-, Account- und Knotenansichten,
- Vitest-/Playwright-Tests und Buildbelege.

Ein produktiver Laufzeit-SSR-Server ist nicht der aktuelle Standard.

### `apps/api`

- HTTP-API mit Axum,
- Magic-Link-, Passkey- und Sitzungslogik,
- Accounts, Knoten, Fäden und öffentliche Knotengespräche,
- JSONL- und opt-in PostgreSQL-Pfade; Gespräche ausschließlich in PostgreSQL,
- Health- und Metrikendpunkte.

Es gibt derzeit keinen produktiven Outbox-Relay und keinen Projector-Worker.
Öffentliche Knotengespräche sind persistiert; private Nachrichten und allgemeine
Gesprächstypen sind nicht implementiert.

### `contracts/domain`

Die Contracts beschreiben aktuelle und teilweise geplante Fachobjekte. Ein
vorhandenes Schema beweist nicht automatisch einen API-, Datenbank- oder
UI-Pfad. Conversation und Message beschreiben den produktiven öffentlichen
Knotengesprächsschnitt. Private Gesprächstypen und historische Rollen bleiben
Vertragsvorbereitung und dürfen nicht als fertiges Subsystem gelesen werden.

### `infra`

Docker Compose und Caddy sind die aktuelle reale Delivery-Basis. Vorhandene Profile
werden in [`runtime/README.md`](../runtime/README.md) eingeordnet. Kubernetes ist
nach ADR-0010 die kanonische Zielplattform, aber noch keine belegte heutige
Produktionsruntime. Nomad ist keine neue Primärzielrichtung.

### `docs`, `architecture`, `runtime`, `runbooks`

- `architecture/`: knappe kanonische System-, Ziel- und Sicherheitswahrheit,
- `runtime/`: unterstützte Laufzeitverträge und Beobachtungsgrenzen,
- `runbooks/`: operative Einstiege,
- `docs/`: Detailverträge, ADRs, Berichte und Planung.

Historische Berichte und ADRs bleiben erhalten, dürfen aktuellen Code oder die
kanonischen Einstiegskarten aber nicht überstimmen.

## Noch nicht vorhandene Zielkomponenten

Neue Verzeichnisse oder Dienste werden erst angelegt, wenn ein konkreter Bedarf
und ein Betriebsvertrag bestehen. Aktuell nicht als Standard vorhanden sind:

- `apps/worker` für Projektionen oder Outbox-Relay,
- `platform/` als kanonischer Kubernetes-/GitOps-Vertrag,
- eigenständige Search-Services,
- **Geplant, aktuell nicht vorhanden:** `packages/` als gemeinsame SDK-Schicht,
- belegter Kubernetes-Produktionsbetrieb,
- Föderationsgateway und Zellkonformitätstests,
- Event-Sourcing als allgemeines Persistenzmodell,
- vollständige Observability-Plattform mit verbindlichen SLOs.

Die fehlende Runtime-Implementierung hebt die Zielentscheidung nicht auf. Neue
Strukturen werden entlang `architecture/commonthing-os.md` eingeführt, sobald der
jeweilige kleine Implementierungsschnitt und sein Betriebsvertrag belegt sind.

## Änderungsregel

Eine Strukturänderung ist erst kanonisch, wenn:

1. der ausführbare Pfad im Repository vorhanden ist,
2. Ownership und Wiederherstellung geklärt sind,
3. Tests oder Runtimebelege existieren,
4. diese Datei, `architecture/overview.md` und `docs/techstack.md` konsistent
   aktualisiert wurden.
