# Weltgewebe

Weltgewebe ist ein aktives, im Aufbau befindliches Karten- und Koordinationssystem für
Kollektivgüter, lokale Beziehungen und gemeinschaftliche Handlungsfähigkeit. Die
kanonische Zielarchitektur entwickelt es zum föderierten Weltgewebe OS: lokale
autonome Zellen mit globalen Identitäten, Beziehungen und gemeinsamen Räumen. Das
Repository enthält die Webanwendung, die Rust-API, Datenverträge,
Datenbankmigrationen, Compose-Profile, Caddy-Konfiguration und Betriebswerkzeuge.

## Aktueller Zustand

- **Web:** SvelteKit, TypeScript, MapLibre GL und PMTiles.
- **API:** Rust, Axum und Tokio.
- **Authentifizierung:** Magic Links, Passkeys und profilweit persistente
  HTTP-only-Sitzungscookies.
- **Domänendaten:** Im belegten Produktionspfad `commonserver` ist PostgreSQL die
  Lese- und Schreibwahrheit für Accounts/Garnrollen, Knoten und Fäden. JSONL
  bleibt nur lokaler, Legacy- oder expliziter Rollback-/Importpfad.
- **Betrieb:** Docker Compose und Caddy; der öffentliche Produktionspfad ist
  `commonserver` mit statischer interner Caddy-Auslieferung des Web-Builds.
- **Produktfluss:** Anmeldung, persistente Garnrolle, Knoten und zugehöriger
  Faden sind als PostgreSQL-Produktionsfluss belegt. Offene Arbeit betrifft
  Skalierung, Referenzintegrität, wiederholte Geräteabnahme und weitere
  Produktbereiche, nicht mehr den ersten vertikalen Persistenzschnitt.

## Zielarchitektur

[`architecture/weltgewebe-os.md`](architecture/weltgewebe-os.md) ist die verbindliche langfristige Zielarchitektur. Kubernetes ist die kanonische Zielplattform, während Docker Compose die heutige reale Runtime und ein begrenzter Entwicklungs-/Recoverypfad bleibt. Der PostgreSQL-Mehrinstanzvertrag ist durch Shared Auth State, generationsgebundene Domain-Projektionen, Transactional Outbox, idempotente Konsumenten und einen Zwei-API-/Restart-Beweis geschützt. Die Produktionsreplikazahl bleibt bis zum getrennten Plattformrollout unverändert.

Historische ADRs und Berichte bleiben als Entscheidungs- und
Entwicklungsgeschichte erhalten. Sie dürfen den aktuellen Code, die Migrationen
oder die hier verlinkten kanonischen Ist-Dokumente nicht überstimmen.

`docs/_generated/*` enthält ausschließlich abgeleitete Diagnose-, Index- und
Navigationsartefakte. Diese Dateien werden über registrierte Generatoren
aktualisiert und sind keine eigenständige Wahrheits- oder Entscheidungsschicht.

## Start hier

| Frage | Wahrheitsort |
|---|---|
| Was ist das langfristige Ziel? | [`architecture/weltgewebe-os.md`](architecture/weltgewebe-os.md) |
| Was läuft heute? | [`runtime/README.md`](runtime/README.md) |
| Wie sind die Komponenten verbunden? | [`architecture/overview.md`](architecture/overview.md) |
| Welche Sicherheitsgrenzen gelten? | [`architecture/security.md`](architecture/security.md) |
| Wie wird betrieben oder diagnostiziert? | [`runbooks/README.md`](runbooks/README.md) |
| Welche Daten liegen wo? | [`docs/datenmodell.md`](docs/datenmodell.md) |
| Welche Technik ist real, optional oder geplant? | [`docs/techstack.md`](docs/techstack.md) |
| Welche Dokumente und Wissensbereiche existieren? | [`docs/index.md`](docs/index.md) |
| Wie wird deployt? | [`docs/deploy/README.md`](docs/deploy/README.md) |
| Welche Begriffe gelten fachlich? | [`docs/domain/vocabulary.md`](docs/domain/vocabulary.md) |
| Welche Quellen haben Vorrang? | [`repo.meta.yaml`](repo.meta.yaml), Agent-Norm: [`agent-contract.json`](agent-contract.json) |

## Für Agents

Vor Änderungen gilt diese Leseordnung:

1. [`agent-contract.json`](agent-contract.json)
2. [`repo.meta.yaml`](repo.meta.yaml)
3. [`AGENTS.md`](AGENTS.md)
4. [`agent-policy.yaml`](agent-policy.yaml)
5. [`docs/policies/agent-reading-protocol.md`](docs/policies/agent-reading-protocol.md)
6. [`docs/index.md`](docs/index.md) als Navigation, nicht als Wahrheitsquelle
7. die betroffenen Contracts, Migrationen, Runtime-Konfigurationen und Tests
8. erst danach Planungsdokumente und historische Berichte

Bei einem Widerspruch zwischen Statusdokument und ausführbarem Vertrag wird
nicht interpoliert. Der Widerspruch wird benannt und aufgelöst.

## Lokal starten

Voraussetzungen:

- Docker mit Compose
- `just`
- für Webentwicklung Node.js und pnpm gemäß den versionierten Metadaten

```bash
cp .env.example .env
just up
```

Prüfen:

- Web/Frontdoor: <http://localhost:8081>
- API-Liveness: <http://localhost:8081/api/health/live>
- API-Readiness: <http://localhost:8081/api/health/ready>

Stoppen:

```bash
just down
```

Die lokale Beispielkonfiguration ist nicht automatisch eine
Produktionskonfiguration. Öffentlicher Login, SMTP, Proxy-Vertrauen,
Datenbankmigrationen und Secrets besitzen zusätzliche fail-closed
Vorprüfungen.

## Entwicklung und Qualität

```bash
just check   # schneller Repository- und Contract-Check
just ci      # breiter lokaler Spiegel für Web, API und Abhängigkeiten
```

Weitere wichtige Prüfpfade:

- Domain-Contracts: `just contracts-domain-check`
- Web-E2E: siehe [`apps/web/README.md`](apps/web/README.md)
- Datenbankbeweise: `.github/workflows/db-*.yml`
- Proxy- und Deployvertrag: `.github/workflows/proxy-trust-preflight.yml`
- Dokumentwahrheit: `.github/workflows/docs-guard.yml`

## Produktbegriffe und technische Namen

| Produktbegriff | Technische Namen im Bestand | Bedeutung |
|---|---|---|
| Garnrolle | Account, teilweise historisch Role | persönlicher Ausgangspunkt im Gewebe |
| Knoten | Node | Ort, Ressource, Vorhaben oder anderer gemeinschaftlicher Bezugspunkt |
| Faden | Edge | Beziehung zwischen Garnrollen und/oder Knoten |
| Gespräch | Conversation/Message | geplant und vertraglich beschrieben, aber noch kein vollständiger produktiver Persistenzpfad |

Neue und öffentlich ausgegebene Accounts sind ausschließlich Garnrollen. Die
Kartenwirkung wird über `map_state=not_on_map|exact|radius` beschrieben. Die
frühere RoN-Identität und die Datenbankspalte `mode` sind nach belegtem
Produktionscutover entfernt.

## Daten- und Datenschutzgrenze

Weltgewebe verwendet notwendige, sichere Sitzungscookies für Anmeldung und
Sitzungserhalt. Der Datenschutzvertrag erlaubt diese Cookies, schließt aber
Werbe- und Trackingcookies sowie verdeckte Profilbildung aus. Öffentliche und
private Accountfelder werden getrennt behandelt; reale Privatpositionen dürfen
nicht als öffentliche Kartenkoordinaten ausgegeben werden.

## Deployment

Der kanonische öffentliche Pfad ist in
[`docs/deploy/vps.md`](docs/deploy/vps.md) beschrieben. Das Repository behauptet
keinen Livezustand allein aufgrund eingecheckter Konfiguration. Runtime- und
Deploybelege müssen frisch erhoben werden.

Statische Web-Builds werden unter anderem durch Cloudflare- und Vercel-
Integrationen geprüft. Diese Vorschauen sind zusätzliche Plattformbelege; die
öffentliche Routingwahrheit liegt im VPS-/Caddy-Vertrag. Der kanonische
Produktionspfad liefert den lokalen `apps/web/build`-Artefaktstand aus und
belegt ihn über `/_app/version.json` sowie den `X-Weltgewebe-Build`-Header.

## Planung und Status

- [`docs/blueprints/weltgewebe-os-masterplan.md`](docs/blueprints/weltgewebe-os-masterplan.md) ordnet die langfristigen Umsetzungswellen.
- [`docs/reports/weltgewebe-os-foundation-status.md`](docs/reports/weltgewebe-os-foundation-status.md) trennt Ziel und belegte Gegenwart.
- [`docs/tasks/board.md`](docs/tasks/board.md) ist eine repositoryinterne
  Arbeitskarte.
- [`docs/tasks/index.json`](docs/tasks/index.json) ist der maschinenlesbare
  Quellindex dieser Planung.
- GitHub-Issues bilden nur ausgewählte öffentliche oder operative Einzelthemen
  ab.
- Statusbehauptungen aus Berichten müssen gegen Code, Contracts, Migrationen,
  CI und Runtimebelege geprüft werden.

## Lizenz

Weltgewebe steht unter der
[GNU Affero General Public License 3.0 oder später](LICENSE).
