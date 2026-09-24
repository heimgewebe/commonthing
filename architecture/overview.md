---
id: overview
title: Architecture Overview
summary: Aktuelle Komponenten, Datenflüsse, Wahrheitsorte und Ausbaugrenzen des commonThing-Systems.
role: norm
organ: governance
status: canonical
canonicality: normative
lifecycle_state: active
owner: governance
review_after: 2026-10-11
last_reviewed: 2026-07-11
depends_on: []
relations:
  - type: relates_to
    target: architecture/security.md
  - type: relates_to
    target: runtime/README.md
  - type: relates_to
    target: docs/techstack.md
  - type: relates_to
    target: architecture/commonthing-os.md
verifies_with: []
---

# Architecture Overview

## Systemgrenze

commonThing ist ein Karten- und Koordinationssystem. Die heute implementierte
Kernfläche besteht aus:

1. einer statisch baubaren SvelteKit-Webanwendung,
2. einer Rust-/Axum-API,
3. Caddy als öffentlichem beziehungsweise lokalem Frontdoor,
4. JSONL- und opt-in PostgreSQL-Persistenzpfaden,
5. PostgreSQL für Sitzungen und ausgewählte Auth-/Domänenpfade,
6. NATS als im Produktions-Compose vorhandener, optional genutzter Eventkanal.

Öffentliche Knotengespräche sind als PostgreSQL-, API- und Webpfad vorhanden.
Private Nachrichten, föderale Gespräche, allgemeine Moderationsabläufe und
Gewebekonten sind weiterhin nur teilweise als Contracts oder Konzepte vorhanden.

## Kanonische Zielrichtung

Die langfristige Zielarchitektur steht in [`architecture/commonthing-os.md`](commonthing-os.md). commonThing wird als föderiertes System autonomer Gewebe-Zellen mit globalen Identitäten, Beziehungen und gemeinsamen Räumen entwickelt. Kubernetes ist die kanonische Zielplattform; Compose bleibt die heutige reale Runtime und ein begrenzter Entwicklungs-/Recoverypfad.

Der PostgreSQL-Mehrinstanzvertrag ist seit dem 16. Juli 2026 belegt und durch einen stärkeren Guard geschützt. Das aktiviert noch keine Produktionsreplikation; diese bleibt Teil des Kubernetes-/GitOps-Rollouts.

## Komponenten

| Komponente | Pfad | heutige Verantwortung |
|---|---|---|
| Web | `apps/web` | Karte, Login, Einstellungen, Account- und Knotenansichten, statischer Build |
| API | `apps/api` | Authentifizierung, Sitzungen, Accounts, Knoten, Fäden, öffentliche Knotengespräche, Health und Metriken |
| Domain-Contracts | `contracts/domain` | JSON-Schema-Verträge für Account, Knoten, Faden sowie den produktiven öffentlichen Knotengesprächsschnitt |
| Caddy | `infra/caddy` | TLS-/Host-Routing, Proxygrenze, Sicherheitsheader und Web-Upstream |
| Compose | `infra/compose` | lokale, produktive und zielbezogene Servicezusammenstellung |
| PostgreSQL | `apps/api/migrations` | Sitzungen, Domain-Tabellen und Passkey-Credentials; Nutzung hängt von expliziten Quellschaltern ab |
| JSONL | `.gewebe/in` beziehungsweise konfigurierte Datenpfade | Standardquelle für Domänendaten und Standard-Schreibpfad |
| NATS | Produktions-Compose und `NATS_URL` | Verbindungs- und Readinessfläche; kein vollständig belegter Outbox-/Projektionskern |

## Hauptdatenflüsse

### Lesen von Domänendaten

```text
Web -> Caddy -> Axum API -> konfigurierte Domain-Lesequelle
                              |- JSONL (Standard)
                              `- PostgreSQL (explizites Opt-in)
```

### Schreiben von Domänendaten

Accounts, Knoten und Fäden besitzen getrennte Quellschalter. PostgreSQL-Schreiben
ist nur zulässig, wenn auch aus PostgreSQL gelesen wird. Es gibt keinen
stillschweigenden Fallback und keinen allgemeinen Dual-Write.

### Anmeldung

```text
Browser -> Magic Link oder Passkey -> API -> Session Store
Browser <- HttpOnly/SameSite/Secure-Sitzungscookie
```

Der Browser speichert keinen Bearer-Token in `localStorage` oder
`sessionStorage`. Normale Tabs und Fenster desselben Browserprofils teilen die
Cookie-Sitzung; andere Profile und private Kontexte bleiben getrennt.

### Karte

Die Webanwendung lädt eine MapLibre-Karte und PMTiles-basierte Basiskarten. Die
Komponente besitzt einen expliziten asynchronen Abbauvertrag, damit eine laufende
Initialisierung nach dem Verlassen der Seite keine Kartenressourcen zurücklässt.

## Wahrheitsmodell

Bei Konflikten gilt die Reihenfolge aus `repo.meta.yaml`:

1. Domain-Contracts,
2. kanonische Policies,
3. Runtime-Konfiguration und Code,
4. normative Spezifikationen,
5. Berichte und Navigationsartefakte.

Migrationen und ausführbare Konfiguration präzisieren den realen physischen
Zustand. Eine Roadmap darf einen geplanten Zustand nicht als bereits betrieben
darstellen.

## Bewusste Ausbaugrenzen

- JSONL ist noch nicht vollständig durch PostgreSQL abgelöst.
- Accounts und öffentliche Projektionen verwenden ausschließlich Garnrolle plus
  `map_state`. Die frühere RoN-Identität und die Spalte `mode` sind entfernt.
- Der produktive Gesprächspfad ist auf genau einen öffentlichen Raum je
  PostgreSQL-Knoten begrenzt; private Räume, Anhänge und Föderation fehlen.
- NATS im Stack bedeutet noch keinen belegten Transactional-Outbox-Betrieb.
- Kubernetes als Zielplattform bedeutet noch keinen laufenden Cluster oder HA-Beleg.
- Föderationsverträge bedeuten noch keine öffentliche Zellföderation.
- Externe Vorschauplattformen sind keine Quelle für die Produktionsrouting-
  oder Runtimewahrheit.

## Nächster fachlicher Integrationsbeweis

Der wichtigste vertikale Schnitt ist:

1. Account anmelden,
2. Garnrolle bearbeiten und verorten,
3. Knoten dauerhaft anlegen,
4. Knoten nach Neuladen auf Karte und Profil wiederfinden,
5. Faden anlegen und erneut lesen.

Dieser Schnitt soll ohne Demo-Fallback und mit eindeutigem Persistenzpfad
belegt werden.
