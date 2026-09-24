---
id: runbooks.incident-response
title: Incident Response Runbook
doc_type: reference
status: active
summary: >
  Eigenständiger Incident-Response-Ablauf für commonThing über Web, API, Datenebene
  und Edge: Erkennung, Ersteinschätzung, Eindämmung, Analyse, Wiederherstellung,
  Nachbereitung, Evidenzsicherung und Kommunikationspfad.
relations:
  - type: relates_to
    target: docs/runbooks/README.md
  - type: relates_to
    target: docs/runbook.md
  - type: relates_to
    target: docs/runbook.observability.md
  - type: relates_to
    target: docs/runbooks/db-recovery.md
  - type: relates_to
    target: docs/deploy/security.md
---
# Incident Response Runbook

Eigenständiger Ablauf für operative, sicherheits- und datenschutzbezogene
Vorfälle im commonThing-Stack. Dieses Runbook ist die Prozessebene; konkrete
Datenwiederherstellung beschreibt [DB Recovery](db-recovery.md), der
quartalsweise Probelauf steht in [`docs/runbook.md` §2](../runbook.md).

## 1. Ziel und Anwendungsbereich

Ziel ist ein reproduzierbarer, evidenzsichernder Umgang mit Vorfällen, die
Verfügbarkeit, Integrität, Vertraulichkeit oder Datenschutz des commonThing-Stacks
betreffen.

Betroffene Komponenten:

- **Web** (SvelteKit, statisch ausgeliefert)
- **API** (Rust/Axum)
- **Datenebene:** PostgreSQL (Sessions/Auth) und JSONL-Domänendaten unter
  `GEWEBE_IN_DIR` (Standard `.gewebe/in`)
- **Event-Pfad** (optional, Gate C): `outbox` → NATS JetStream → Projektoren
- **Edge:** Caddy / `edge-caddy`

Vorfallklassen:

- **Verfügbarkeit:** Ausfall, Degradierung, fehlschlagende Healthchecks.
- **Integrität/Daten:** Korruption oder Verlust von Domänen- oder Sessiondaten →
  Übergabe an [DB Recovery](db-recovery.md).
- **Sicherheit:** unautorisierter Zugriff, Leak von Secrets (SMTP, Cookie-/Session-
  Konfiguration), Missbrauch der Auth-Endpunkte.
- **Datenschutz (DSGVO):** Offenlegung personenbezogener Daten — insbesondere
  Account-Positionen (`public_pos`/`location`), E-Mail-Adressen, Magic-Link-Token.

Nicht in diesem Runbook: reguläre Deployments
([`docs/deploy/README.md`](../deploy/README.md)) und der quartalsweise
Disaster-Recovery-Drill ([`docs/runbook.md` §2](../runbook.md)), der diesen
Ablauf einübt.

### Rollen

| Rolle | Aufgabe |
|---|---|
| Incident Lead | Koordiniert, entscheidet über Eskalation und Kommunikation, führt die Timeline |
| Operator | Führt technische Maßnahmen aus (Logs, Container, Restore) |
| Comms | Interne und externe Kommunikation, DSGVO-Meldepflichten |

> **Hinweis Heimserver-Realität:** In Single-Maintainer-Betrieb hält eine Person
> ggf. alle Rollen. Die Schritte und die Timeline bleiben trotzdem verbindlich.

## 2. Erkennung

Signalquellen:

- **Healthcheck:** `GET /api/health/live` antwortet nicht mit `200`.
- **Edge:** gehäufte `5xx` oder TLS-Fehler über Caddy.
- **Observability** (`infra/compose/compose.observ.yml`, siehe
  [Observability Runbook](../runbook.observability.md)): Prometheus
  (`:9090`), Grafana (`:3001`), Loki-Logs (`:3100`), Tempo-Traces (`:3200`).
- **Auth-Anomalien:** `429`-Wellen oder ungewöhnliche Magic-Link-Anfragen
  in API-/Edge-Logs; aktive Drosselung liegt je nach Deployment bei
  API-Rate-Limits, vorgeschaltetem LB/WAF, Firewall oder fail2ban. Caddy-
  Rate-Limits in `infra/caddy/Caddyfile.prod` sind aktuell nicht aktiv.
- **Nutzermeldungen.**

Erste Sichtprüfung:

```bash
docker compose -f infra/compose/compose.prod.yml ps
curl -fsS https://commonthing.net/api/health/live
docker compose -f infra/compose/compose.prod.yml logs -n 200 api
```

## 3. Ersteinschätzung

1. **Schweregrad einordnen:**
   - **SEV1:** Totalausfall, Datenverlust oder aktiver Datenabfluss.
   - **SEV2:** spürbare Degradierung, einzelne Schicht betroffen.
   - **SEV3:** kleiner, eingrenzbarer Defekt ohne Datenrisiko.
2. **Blast Radius bestimmen:** betroffene Schicht (Edge/Web/API/DB/Stream).
3. **Datenschutzfrage zuerst klären:** Sind personenbezogene Daten betroffen?
   Wenn ja → DSGVO-Pfad in [§9](#9-kommunikationspfad) aktivieren.
4. **Laufzustand:** einmaliger Crash oder andauernder Angriff/Fehler?
5. **Vorfall ausrufen:** Lead benennen, Timeline starten (Zeitstempel, Beobachtung,
   Maßnahme, Wirkung).

## 4. Eindämmung

Ziel: Schaden stoppen, **ohne Evidenz oder Daten zu zerstören**.

- **Edge zuerst:** Bei Missbrauch zuerst eine aktive Schutzebene nutzen:
  vorgeschalteter LB/WAF, Firewall oder fail2ban (falls vorhanden).
- **Caddy-Hinweis:** Rate-Limits in `infra/caddy/Caddyfile.prod` sind aktuell
  nicht aktiv/verfügbar; Änderungen dort wirken erst mit passendem
  Caddy-Build/-Deployment.
- **Public Login drosseln:** Bei Magic-Link-Missbrauch `AUTH_PUBLIC_LOGIN=0`
  setzen (siehe [`docs/runbook.md` §3](../runbook.md)).
- **Secrets rotieren:** Bei Verdacht auf Leak SMTP-Zugang, Cookie-/Session-
  Konfiguration und betroffene Tokens erneuern.
- **Isolieren statt löschen:** Betroffene Container anhalten oder vom Netz nehmen;
  Daten und Logs bleiben erhalten.
- **Datenabfluss kappen:** Bei Offenlegung personenbezogener Daten den
  Auslieferungspfad (z. B. Web über Caddy) schließen, bevor an der Ursache
  gearbeitet wird.

> **Kritisch:** `just down` stoppt den Stack **und entfernt Volumes**. Während
> eines Vorfalls niemals `just down` auf produktive Daten anwenden — das
> vernichtet Datenbank und Evidenz. Stattdessen gezielt einzelne Container
> stoppen.

## 5. Analyse

- **Logs korrelieren:** Loki / `docker compose ... logs` über Web, API, Caddy.
- **Metriken/Traces:** Prometheus und Tempo für Zeitfenster und Latenzspitzen.
- **Letzte Änderung prüfen:** `git log`, Build-Metadaten öffentlich über
  `GET /api/version` (Edge) und intern im Compose-/Container-Netz über
  `http://api:8080/version` (vollständiger `GIT_COMMIT_SHA` und deterministischer
  Git-Commitzeitpunkt in `BUILD_TIMESTAMP`),
  Konfigurations-Drift gegen [`docs/deploy/DRIFT_POLICY.md`](../deploy/DRIFT_POLICY.md).
- **Datenebene begutachten:** JSONL unter `GEWEBE_IN_DIR` (`.gewebe/in`) und
  PostgreSQL-Zustand (Migrationsstand, `sessions`; `outbox` nur falls Gate C /
  Outbox im Deployment aktiv).
- **Reproduktion:** Wenn möglich in isolierter Umgebung nachstellen, nicht am
  Produktivsystem.

## 6. Wiederherstellung

Je nach Ursache:

- **Datenkorruption/-verlust:** Übergabe an [DB Recovery](db-recovery.md).
- **Fehlerhaftes Deployment:** Rollback auf das vorige Image/Tag.
- **Konfigurationsfehler:** Bekannten guten Stand wiederherstellen
  (`infra/compose/*`, `infra/caddy/*`).
- **Schemastand:** Bei Bedarf Migrationen erneut anwenden (`just db-migrate`);
  die API führt `sqlx::migrate!` ohnehin beim Start aus.
- **Event-Pfad (nur falls Gate C / Outbox im Deployment aktiv):** Outbox-Relay
  und Projektoren neu starten, damit Lese-Modelle (`faden_view` etc.) wieder
  aufschließen. Falls nicht aktiv: diesen Schritt überspringen.

Abschluss erst nach erfolgreicher Verifikation (siehe
[DB Recovery §8](db-recovery.md) und [§2 Erkennung](#2-erkennung)):

```bash
curl -fsS https://commonthing.net/api/health/live
just smoke-seed
```

## 7. Nachbereitung

- **Blameless Postmortem:** Timeline, Ursache, Wirkung, Erkennungslücken.
- **Maßnahmen ableiten:** konkrete, zugeordnete Folge-Aufgaben.
- **Runbooks pflegen:** dieses Runbook, [DB Recovery](db-recovery.md) und ggf.
  [`docs/runbook.md` §2](../runbook.md) mit den Erkenntnissen aktualisieren.
- **Drill abgleichen:** Falls relevant, den quartalsweisen DR-Drill anpassen.

## 8. Evidenzsicherung

Vor jeder verändernden Maßnahme sichern:

- **Logs:** `docker compose -f infra/compose/compose.prod.yml logs --no-color > incident-<zeitstempel>-api.log`
- **Laufzeitstand:** Container-/Image-IDs, `GET /api/version` (Edge) bzw. intern `http://api:8080/version`, relevante Metriken.
- **Datenstand:** Bei Datenvorfällen forensische Kopie ziehen (PostgreSQL-Dump,
  Kopie der JSONL-Dateien) — getrennt vom späteren Restore-Ziel.

> **DSGVO-Disziplin (Akzeptanzkriterium: kein Datenleck-Risiko):** Evidenz
> enthält regelmäßig personenbezogene Daten (E-Mails, Koordinaten, Token).
> Daher:
>
> - Evidenz **nicht** ins Git-Repository, **nicht** in öffentliche Tickets/PRs.
> - `.gewebe/in/` bleibt git-ignored — diesen Zustand nicht aufheben.
> - In Tickets und PRs personenbezogene Daten redigieren.
> - Zugriff beschränken, Aufbewahrung minimieren und befristen.

## 9. Kommunikationspfad

- **Intern:** Lead koordiniert; Beobachtungen und Entscheidungen laufend in der
  Timeline festhalten (auch im Single-Operator-Betrieb).
- **Extern:** Bei Verfügbarkeits- oder Datenschutzwirkung sachliche
  Statusmeldung an Betroffene; keine Spekulation.
- **DSGVO-Meldepflicht:** Bei einer Verletzung des Schutzes personenbezogener
  Daten Meldepflichten prüfen (Art. 33 DSGVO: Aufsichtsbehörde binnen 72 h;
  Art. 34: Benachrichtigung Betroffener bei hohem Risiko). Bewertung und
  Entscheidung in der Timeline dokumentieren.

## 10. Verweise auf relevante Systeme und Artefakte

- [DB Recovery Runbook](db-recovery.md) — Datenwiederherstellung im Detail
- [`docs/runbook.md`](../runbook.md) — Onboarding, DR-Drill (§2), Public-Login-
  Konfiguration (§3), Account-Bootstrap (§4)
- [Observability Runbook](../runbook.observability.md) — Prometheus/Grafana/Loki/Tempo
- [`docs/deploy/security.md`](../deploy/security.md) — Sicherheitsleitplanken
- [`docs/deploy/DRIFT_POLICY.md`](../deploy/DRIFT_POLICY.md) — Konfigurations-Drift
- `infra/compose/*` — Compose-Profile (`compose.core.yml`, `compose.prod.yml`,
  `compose.observ.yml`, `compose.stream.yml`)
- `infra/caddy/Caddyfile.prod` — Edge-Routing (Rate-Limits dort aktuell nicht aktiv/verfügbar)
- `apps/api` — Healthchecks (`/api/health/live`), `/api/version` (Edge) bzw. intern `/version`
