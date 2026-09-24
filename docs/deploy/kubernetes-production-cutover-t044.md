---
id: deploy.kubernetes-production-cutover-t044
title: "T044 — Produktionscutover: R5-Vorabnahme und Wirkungsgates"
doc_type: runbook
status: active
canonicality: operational
lifecycle_state: active
owner_task: WELTGEWEBE-OS-V1-T044
last_reviewed: 2026-09-24
review_after: 2026-10-07
summary: >
  Revisionsgebundener R5-Preflight für den Wechsel von Compose/Caddy-Blue auf
  Kubernetes-Green. Bindet die am 23.09.2026 live beobachtete Produktions- und
  Referenzplattformwahrheit und blockiert jede Produktionswirkung bis Zielplattform,
  Capability-Parität, Datenübergang, Backup und Rollback konkret belegt sind.
relations:
  - type: depends_on
    target: docs.reports.kubernetes-platform-foundation-status
  - type: relates_to
    target: deploy.vps
  - type: relates_to
    target: architecture.semantic-search
  - type: relates_to
    target: adr.ADR-0010__kubernetes-kanonische-plattform
---

# T044 — Produktionscutover: R5-Vorabnahme und Wirkungsgates

## 1. Zweck und Autorität

Dieses Dokument ist der versionierte Cutover-Vertrag für
`WELTGEWEBE-OS-V1-T044`. Es beschreibt den frisch beobachteten Zustand und die
Eintritts-, Stop- und Abschlussbedingungen für den Produktionswechsel.

Es ist **keine Aktivierungsfreigabe**. Insbesondere autorisiert es weder DNS-,
Traffic-, Writer-, Datenbank- noch Kubernetes-Produktionsmutationen.

Beobachtungsbasis:

- Datum: 2026-09-23
- geschützter Public-`main`: `04f182c2ce8a9269c520719166e04aa13d8c7178`
- T044: Revision 5, kein aktueller Verification-Stamp
- T084: aktueller Verification-Stamp vorhanden
- produktive Blue-Runtime: `commonserver`
- Referenz-Green: `commonthing-staging`

## 2. Dialektische Disposition

**These:** Die Kubernetes-/GitOps-Plattform ist hinreichend belegt, um den
Produktionscutover vorzubereiten.

**Antithese:** Der vorhandene `commonthing-staging`-Cluster ist ausdrücklich
kein Produktionscluster. Er belegt weder öffentliches DNS/TLS noch einen
produktiven Load Balancer. Außerdem fehlen dort heute produktive Fähigkeiten,
die Blue real ausführt.

**Disposition:** R5 ist begonnen, aber noch nicht bestanden. Blue bleibt alleinige
Produktions- und Writer-Autorität. R6 darf erst beginnen, wenn die unten
markierten BLOCKED-Gates geschlossen sind.

## 3. Blue — reale Produktion

Frischer Readback vom VPS `commonserver`:

### 3.1 Revision und Frontdoor

- Compose-Projekt: `weltgewebe`
- ausgelieferter Build: `04f182c2`
- exakter Main-Commit: `04f182c2ce8a9269c520719166e04aa13d8c7178`
- kanonischer Web-Origin: `https://commonthing.net`
- kanonischer API-Origin: `https://api.commonthing.net`
- Legacy-Web/API bleiben Kompatibilitätspfade
- DNS für `commonthing.net`, `api.commonthing.net`, `weltgewebe.net` und
  `api.weltgewebe.net` zeigte auf `94.16.121.119`
- Web-Readback: HTTP 200
- API-Readiness: HTTP 200, Datenbank/Event-Chain/NATS/Policy = ready

### 3.2 Laufende Fähigkeiten

Blue führt real aus:

- Caddy/Public TLS/Hostrouting
- statisches Web
- API
- PostgreSQL
- NATS JetStream
- lokalen Ollama-Embedding-Runtimepfad
- persistenten Search-Worker
- Schauwerk/Schaubild-Editor
- lokale Basemap-/Style-/Glyph-/PMTiles-Auslieferung
- Public Login/Auth-Vertrag

### 3.3 Daten- und Writer-Wahrheit

API-Konfiguration:

- Domain-Read-Source: PostgreSQL
- Account-/Node-/Edge-Write-Source: PostgreSQL
- Passkey-Credential-Source: PostgreSQL
- `AUTH_PUBLIC_LOGIN=1`
- NATS: `nats://nats:4222`

Beobachtete PostgreSQL-Zustände, nicht als SLO oder Sollwert zu lesen:

- `domain_accounts=13`
- `domain_nodes=4`
- `domain_edges=16`
- `domain_outbox=292`
- `domain_event_consumptions=292`
- `search_index_generations=1`
- `search_node_projections=4`
- `search_projection_jobs=23`

JetStream:

- Stream: `WELTGEWEBE_DOMAIN`
- Consumers: 2
- Messages: 7
- letzter beobachteter Stream-Seq: 292

### 3.4 Semantic Search

Aktiv:

- Provider: `local:ollama`
- Modell: `qwen3-embedding:4b`
- Modellrevision:
  `sha256:df5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff907`
- Dimension: 2560
- Runtime: `ollama:0.12.6@http://127.0.0.1:11434`
- Provider-URL: exakt `http://127.0.0.1:11434/`

Der Search-Worker-Vertrag akzeptiert absichtlich nur literal Loopback. Ein
clusterweiter Ollama-Service ist daher **keine** äquivalente Kleinänderung.

### 3.5 Hostkapazität

Am Beobachtungszeitpunkt:

- 4 vCPU
- ca. 8,33 GB RAM
- kein Swap
- ca. 144 GB freier Plattenspeicher

Der bestehende Produktions-Search-Vertrag verlangt vor seinem Rollout mindestens
3 Online-CPUs, 5 GiB verfügbaren Arbeitsspeicher und 8 GiB freien Speicher.
Darum gilt ein zweiter vollständiger Green-Stack auf demselben VPS **nicht** als
kapazitiv bewiesen. Co-Residence benötigt einen eigenen Messbeweis und darf nicht
aus dem aktuellen Idle-Readback abgeleitet werden.

## 4. Green — heutige Kubernetes-Referenz

`commonthing-staging` meldete beim frischen Readback `status=ready`.

### 4.1 Bindungen

- Cluster: `commonthing-staging`
- aktiver App-Commit:
  `bb1e26d47b50d38ec720b123d55255c05436100b`
- Bootstrap-/Datenrevision:
  `6a10c76666b9769fdfddb62ecb3fbf0c2c5df935`

Damit ist Green aktuell **nicht** auf demselben App-Commit wie Blue/Public-Main.

### 4.2 Workloads

Bereit:

- API: 2/2
- Web: 2/2
- PostgreSQL: 1/1
- NATS: 1/1
- Flux-Controller
- Cilium Gateway API
- PostgreSQL-PVC: Bound, 10 Gi
- NATS-PVC: Bound, 5 Gi
- Runtime-/Database-Secrets: ready
- Registry-Pull-Secret: ready

Gateway:

- Cilium Gateway Accepted/Programmed
- HTTPRoute: `/health` + `/api` -> API, `/` -> Web
- lokaler T084-Hostproof erfolgt über `127.0.0.1:18084`

Nicht dadurch belegt:

- öffentliches DNS
- öffentliches TLS
- produktiver Load Balancer
- Produktionswriter
- Produktionsdatenparität

### 4.3 Fehlende produktive Fähigkeiten

Im heutigen Green-Readback fehlen:

- Ollama
- Search-Worker
- Schauwerk/Schaubild
- öffentlich belegter Basemap-/PMTiles-Frontdoor
- öffentliches TLS/DNS
- produktionsgebundene Secret-/Edge-Autorität

Außerdem steht Green auf `AUTH_PUBLIC_LOGIN=0`, während Blue produktiv
`AUTH_PUBLIC_LOGIN=1` verwendet.

## 5. Blue-vs-Green-Matrix

| Capability | Blue | Green heute | R5-Disposition |
| --- | --- | --- | --- |
| Web | aktiv, öffentlich | 2/2 ready | offen: exakte Revision + Public Frontdoor |
| API | aktiv, öffentlich | 2/2 ready | offen: exakte Revision + Public Frontdoor |
| PostgreSQL | produktive Wahrheit | persistent ready | BLOCKED: Produktionsdatenübergang fehlt |
| NATS/JetStream | produktiv | persistent ready | BLOCKED: Stream-/Consumer-Übergang fehlt |
| Auth/Public Login | aktiv, `1` | `0` | BLOCKED: Parität/Entscheidung fehlt |
| Ollama | aktiv | fehlt | BLOCKED |
| Search-Worker | aktiv | fehlt | BLOCKED |
| Semantic Search | aktiv | nicht vollständig lauffähig | BLOCKED |
| Schauwerk/Schaubild | aktiv | fehlt | BLOCKED |
| Basemap/PMTiles | öffentlich über Caddy | kein öffentlicher Beweis | BLOCKED |
| DNS/TLS/Public Edge | aktiv | kein Produktionsziel | BLOCKED |
| Backup/Restore | Compose-Pfad + T084-Evidenz getrennt | T084 belegt | BLOCKED für konkretes Produktionsziel |
| Writer-Autorität | Blue | keine | korrekt: Blue bleibt alleiniger Writer |

## 6. Harte R5-Blocker

### B1 — reale Green-Zielplattform fehlt

Der lokale T084-kind-Cluster ist Referenz-/Abnahmeumgebung und darf nicht als
Produktionscluster umetikettiert werden.

Für T044 fehlt ein konkret beobachtbares Produktionsziel mit mindestens:

- Host/Provider bzw. Clusteridentität
- CPU/RAM/Storage-Kapazität
- Fehlerdomäne
- öffentlicher Edge-/Load-Balancer-Pfad
- DNS-/TLS-Verantwortung
- Secretbereitstellung
- Backupziel
- Restorepfad
- Operator-/Writer-Autorität

Bis diese Zielidentität feststeht, wird **keine** Produktions-Kubernetes-Topologie
aus der lokalen Staging-Implementierung extrapoliert.

Frischer Gegencheck vom 24.09.2026:

- Die SSH-Ziele `wg-prod-1` und `commonserver` lösen beide auf `94.16.121.119:22`
  mit demselben Operatorbenutzer auf; beide Live-Probes melden den Hostnamen
  `commonserver`. Sie sind damit zwei Namen für denselben beobachteten
  Produktionshost und **kein** getrenntes Green-Ziel.
- Auf dem exakten Public-Main `04f182c2ce8a9269c520719166e04aa13d8c7178`
  existiert kein `platform/clusters/production`. Deklarierte Clusterkompositionen
  sind `local`, `staging` und `ha`.
- `platform/apps/weltgewebe/overlays/production` ist ein Anwendungs-Overlay;
  seine Existenz belegt keinen provisionierten oder betriebsbereiten
  Produktionscluster.
- Ein In-place-Wechsel auf `commonserver` bleibt ein möglicher Alternativpfad,
  ist aber **nicht freigegeben oder belegt**. Vor einer solchen Festlegung
  müssten mindestens Kapazität unter realer Blue-Last, Port-/Edge-Kollisionen,
  Storage-Isolation, progressive Traffic-Steuerung, Writer-Fencing, Search/Ollama,
  Rollback und Recovery auf demselben Host separat bewiesen werden.

Damit ist B1 nach dem Gegencheck enger: Es fehlt nicht nur die Zielidentität auf
dem Papier; im heute registrierten und erreichbaren Bestand wurde **kein zweiter
externer Green-Host gefunden**. Daraus folgt weder die Erlaubnis, neue
kostenpflichtige Infrastruktur zu beschaffen, noch die Freigabe für einen
In-place-Cutover.

### B2 — Semantic-Search-Parität fehlt

Blue führt Ollama und Search-Worker real aus; Green nicht.

Der Fix muss den heutigen Vertrag erhalten oder ihn ausdrücklich mit eigener
Evidenz ersetzen. Insbesondere darf `127.0.0.1:11434` nicht still durch einen
clusterweiten Providerpfad ersetzt werden.

Die Workerlogik ist für parallele Worker lease-/claimgebunden ausgelegt
(`FOR UPDATE SKIP LOCKED`), aber daraus folgt noch keine geeignete
Ollama-/Storage-Topologie für den unbekannten Produktionscluster.

### B3 — Frontdoor-/Schauwerk-Parität fehlt

Blue besitzt zusätzliche produktive Edge-Fähigkeiten, die nicht durch
API/Web-Gateway-Readiness abgedeckt sind:

- Schauwerk/Schaubild
- Basemap/PMTiles/Styles/Glyphs
- Redirect-/Legacy-Hostvertrag
- Sicherheitsheader
- öffentliches TLS

Diese müssen vor R6 entweder Kubernetes-native sein, explizit extern
weiterbetrieben werden oder mit eigener Evidenz retired werden.

### B4 — Datenparität fehlt

Die T084-Daten sind Recovery-/Staging-Evidenz, nicht automatisch der aktuelle
Produktionsdatenstand. Vor Writer-Fencing müssen PostgreSQL, JetStream,
Suchprojektion und sonstiger persistenter Fachzustand aus Blue gegen das konkrete
Green-Ziel abgeglichen werden.

## 7. Eintrittsgates für R6

R6 darf erst starten, wenn **alle** folgenden Bedingungen erfüllt sind:

1. ein konkretes Produktions-Green ist identifiziert und live beobachtet;
2. dessen Kapazität und Fehlerdomäne sind dokumentiert;
3. Current Main und gewünschte Release-Revision sind erneut frisch bestimmt;
4. digestgebundene API-/Web-Promotion für die gewünschte Revision liegt vor;
5. Search/Ollama/Schauwerk/Basemap/Auth sind jeweils als
   Kubernetes-native, explizit extern oder retired entschieden und belegt;
6. Green kann aktuelle Produktionsdaten aufnehmen, ohne Blue-Writer zu berühren;
7. frisches Blue-Backup und konkreter Restorepfad sind belegt;
8. Rollback auf Blue ist weiterhin möglich;
9. keine fremde T044-/Produktionswriter-Lane ist aktiv;
10. keine reale Produktionsmutation wurde aus einem älteren Preflight abgeleitet.

## 8. R6 — Generalprobe

Zuerst read-only:

1. Web
2. API
3. Auth
4. Karten-/Basemapdaten
5. Fachdaten
6. Suche
7. Schauwerk/Schaubild
8. Event-/Projektionsreadback

Danach ausschließlich kontrollierte Testwrites gegen Green.

Für jedes Szenario werden Blue und Green unter demselben fachlichen Vertrag
verglichen. HTTP 200 allein genügt nicht; Daten-, Auth-, Event- und
Projektionssemantik müssen übereinstimmen.

## 9. R7 — Writer-Fencing

Unveränderliche Invariante:

> Eine Schreibklasse hat genau einen autoritativen Writer.

Sequenz:

```text
Blue Writer
  -> letzter Datenabgleich
  -> Blue Writer Fence
  -> finale Konvergenz
  -> Green Readback
  -> Green Writer Authority
```

Keine Phase darf Blue und Green gleichzeitig als unabhängige Writer zulassen.

## 10. R8 — Traffic-Cutover

Erst nach bestätigter Green-Writer-Autorität:

1. exakte Green-Revision/Digests erneut prüfen;
2. öffentliches Routing umstellen;
3. Web/API/Auth/Fachdaten/Search/Schauwerk/Basemap lesen;
4. kontrollierten Write und Event-/Projektionsnachzug prüfen.

Automatischer STOP bzw. Rückfall bei:

- falscher Revision oder falschem Digest
- unklarer Writer-Autorität
- Datenabweichung
- Auth-Regression
- Eventverlust oder -duplikation
- Search-/Schauwerk-/Basemap-Verlust
- erheblicher, vorher definierter Fehler- oder Latenzverletzung
- fehlendem Recovery-/Rollbackbeweis

Es werden keine SLO-, RTO- oder RPO-Zielwerte erfunden. Grenzwerte werden aus
frischen Messungen und dem konkreten Zielvertrag abgeleitet.

## 11. R9 — Abschluss

T044 darf erst terminalisiert werden, wenn seine elf Acceptance-Kriterien
revisionsgebunden gegen die tatsächlich laufende Produktion authentifiziert
wurden und `verification-stamp WELTGEWEBE-OS-V1-T044` erfolgreich ist.

Der alte Compose-Pfad bleibt danach während der festgelegten Beobachtungs- und
Rollbackfrist deaktivierte Recovery-Option; er wird nicht sofort gelöscht.

## 12. Aktuelle Entscheidung

Stand dieses R5-Preflights:

- **Blue bleibt Produktion.**
- **Blue bleibt alleiniger Writer.**
- **T084-Staging bleibt Referenz, nicht Produktionsziel.**
- **Kein Traffic-/DNS-/Writer-Cutover.**
- **Keine neue Plattformschicht.**
- Nächster harter Hebel ist die konkrete, kapazitiv belegte
  Produktions-Green-Zielidentität; anschließend werden die bereits belegten
  Capability-Lücken gegen genau dieses Ziel geschlossen.
