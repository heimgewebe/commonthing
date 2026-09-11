---
id: deploy.heimserver.deployment
title: Heimserver Deployment
doc_type: reference
status: deprecated
summary: Historisches Heimserver-Deployment-Runbook; nicht mehr Produktionsziel.
relations:
  - type: relates_to
    target: docs/deploy/README.md
  - type: relates_to
    target: docs/deploy/heimserver.integration.md
  - type: relates_to
    target: docs/deployment.md
---

> [!WARNING]
> Der Heimserver ist kein aktives Produktionsziel mehr. Kanonische Produktion
> ist `commonserver` über den Public-VPS-Pfad. Dieses Dokument bleibt nur als
> historischer/Legacy-Kontext erhalten.

# Weltgewebe – Deployment Runbook (Heimserver)

## Architektur (Ist)

- Weltgewebe-Stack läuft als Docker Compose Projekt `weltgewebe`:
  - Services: `db` (Postgres 16), `api`, `nats` (JetStream), optional Service `caddy`
    (Container typischerweise `weltgewebe-caddy-1`, im Stack meist aus oder `--scale caddy=0`)
- Edge-Gateway läuft separat als Compose Projekt `edge`:
  - Container: `edge-caddy` (bindet Ports 80/443)
  - Edge-Caddy ist mit `weltgewebe_default` verbunden (wichtig für DNS/Reverse Proxy)
  - Der Service `caddy` (Container typischerweise `weltgewebe-caddy-1`) publiziert **keine Host-Ports**,
    routet aber container-intern im produktiven Deployment.

## DNS / Hosts

- Intern: `*.home.arpa`
- API: `api.weltgewebe.home.arpa` → edge-caddy → `weltgewebe-api-1:8080`
- Frontend-Alias: `weltgewebe.home.arpa`
  - `/api/*` → lokal an `weltgewebe-api-1:8080`
  - sonst → lokale statische UI, bereitgestellt durch die Heimserver-Frontdoor (Edge-Caddy) aus dem Weltgewebe-Build-Pfad

## Env / Secrets

- `/opt/weltgewebe/.env` (wird via `--env-file` genutzt)
- ENV-Path override: `export WELTGEWEBE_ENV_FILE=/pfad/zur/.env`
  - Beispiel: `docker compose --env-file "$WELTGEWEBE_ENV_FILE" ... up -d --build`
  - Hinweis: `--env-file` (CLI) ist für die Compose-Interpolation nötig, `WELTGEWEBE_ENV_FILE` (Env Var) für die
    Injektion in den Container via `env_file:` im Override.
  - Beide müssen im selben Aufruf-Kontext gelten: `export WELTGEWEBE_ENV_FILE=...` und
    `docker compose --env-file "$WELTGEWEBE_ENV_FILE" ...`

## Standard-Kommandos

- Stack (via Script, empfohlen):

  Installieren (Symlink): `sudo ln -sf /opt/weltgewebe/scripts/weltgewebe-up /usr/local/bin/weltgewebe-up`

  Das Skript nutzt standardmäßig:
  - `REPO_DIR=/opt/weltgewebe`
  - `ENV_FILE=/opt/weltgewebe/.env`
  - Deploy-Branch-Vertrag: standardmäßig `main` (abweichende Branches nur explizit)

  ```sh
  # Default (pullt und deployt explizit den Default-Branch `main`, ohne Caddy):
  weltgewebe-up

  # Expliziter Feature-Branch-Deploy (bewusst):
  weltgewebe-up --branch feat/dein-zweig

  # Alternativ per Environment-Override:
  WELTGEWEBE_DEPLOY_BRANCH=feat/dein-zweig weltgewebe-up

  # Nur bewusstes Debugging: aktuell ausgecheckten Branch deployen
  weltgewebe-up --current-branch

  # Offline Recovery (kein fetch/switch/pull):
  weltgewebe-up --no-pull

  # Optional: mit Caddy (wenn Ports frei):
  weltgewebe-up --with-caddy
  ```

  **Offline Recovery:** `weltgewebe-up --no-pull` funktioniert auch ohne Internetverbindung,
  da es keine Git-Sync-/Branchwechsel-Operationen ausführt (kein fetch, kein switch, kein pull)
  und den aktuell vorhandenen Checkout deployt.

  **Format-Hinweis:** `--branch` erwartet einen Branch-Namen wie `main` oder `feat/x`,
  nicht `origin/main`.

  **Hinweis:** `--current-branch` ist für absichtliche Abweichungen gedacht (z. B. Debugging),
  nicht für den regulären Heimserver-Betrieb.

  Für abweichende Installationspfade: `REPO_DIR` und `ENV_FILE` setzen.
  `REPO_DIR=/pfad ENV_FILE=/pfad/.env weltgewebe-up`

- Ops-Drift zurückführen (weltgewebe-pr):

  Lokale Änderungen auf dem Heimserver (Drift) können als PR zurückgeführt werden.
  Das Skript `scripts/weltgewebe-pr` erkennt Änderungen, erstellt einen Branch und öffnet einen PR
  (GitHub CLI empfohlen).

  ```sh
  # Plan-Modus (Dry-Run, zeigt was passieren würde):
  scripts/weltgewebe-pr --plan

  # Standard: Branch + Commit + Push + PR erstellen:
  scripts/weltgewebe-pr --title "ops: fix caddy config"

  # Als Draft-PR:
  scripts/weltgewebe-pr --draft
  ```

  **Sicherheit:** `.env` und Secrets werden automatisch blockiert.
  Nur Pfade in der Allowlist (`docs/`, `scripts/`, `infra/`, `configs/`, `.github/`) werden gestaged.

- Stack (Manuell):

  ```sh
  docker compose --env-file /opt/weltgewebe/.env -p weltgewebe \
    -f infra/compose/compose.prod.yml -f infra/compose/compose.prod.override.yml \
    up -d --build --scale caddy=0
  ```

- Edge:
  - `cd /opt/heimgewebe/edge && docker compose -p edge -f docker-compose.yml up -d --force-recreate`

## Healthchecks

Die Edge-CA (Caddy „tls internal") muss einmalig exportiert werden:

```sh
docker exec edge-caddy cat /data/caddy/pki/authorities/local/root.crt \
  > /opt/heimgewebe/edge/edge-ca.crt
```

- API via edge:
  - `curl --cacert /opt/heimgewebe/edge/edge-ca.crt -fsS https://api.weltgewebe.home.arpa/health/ready`
- API via Container (Docker Native, falls nötig):
  - `docker compose exec api wget -qO- http://localhost:8080/health/ready`
- Alias via edge:
  - `curl --cacert /opt/heimgewebe/edge/edge-ca.crt -fsS https://weltgewebe.home.arpa/api/health/ready`

## Login (Magic Link)

- Request:

  ```sh
  curl --cacert /opt/heimgewebe/edge/edge-ca.crt -fsS \
    -X POST https://weltgewebe.home.arpa/api/auth/magic-link/request \
    -H 'content-type: application/json' -d '{"email":"..."}'
  ```

- Token-Link erscheint in API logs nur wenn `AUTH_LOG_MAGIC_TOKEN=true` (Debug).

### Public Magic-Link Login (Option C)

Supported with strict rate limits (mandatory):

- `AUTH_PUBLIC_LOGIN=1`
- `AUTH_AUTO_PROVISION=1`
- `AUTH_AUTO_PROVISION_ROLE=gast`
- `AUTH_ALLOW_EMAILS` unset or empty (triggers Open Registration mode)
- `AUTH_LOG_MAGIC_TOKEN=0` (Security mandatory for Production)

`AUTH_AUTO_PROVISION_ROLE=weber` ist nur für eine geschlossene Zulassung mit
konkretem `AUTH_ALLOW_EMAILS` oder `AUTH_ALLOW_EMAIL_DOMAINS` erlaubt. Offene
Registrierung mit `weber` sowie jede Autoprovisionierung als `admin` führen zu
einem harten Startfehler.

**Required Rate Limits (Environment Variables):**

- `AUTH_RL_IP_PER_MIN=5`
- `AUTH_RL_IP_PER_HOUR=30`
- `AUTH_RL_EMAIL_PER_MIN=2`
- `AUTH_RL_EMAIL_PER_HOUR=10`

Die Per-IP-Limits sind nur wirksam, wenn die API die echte Client-IP auflösen
kann. Hinter Caddy verlangt das eine Proxy-Deklaration; ohne sie verweigert die
API den Start:

- `AUTH_TRUSTED_PROXIES=127.0.0.1,::1,172.16.0.0/12` (Docker-Bridge-Netz)

`compose.prod.override.yml` setzt diesen Wert bereits als Default.

**Konsequenz:**

- Jeder kann Magic-Link anfordern.
- Unbekannte Emails werden automatisch provisioniert (Auto-Provision).
- Abuse-Risiko steigt.
- Start bricht ab, wenn Rate-Limits fehlen.

**Risiken:**

- Spam-Missbrauch
- Token-Flood
- Enumeration über Timing
- Mail-Kostensteigerung

## Troubleshooting

### 308 Redirect bei /api

Ursache: alias-block macht `redir` statt `handle_path /api/* reverse_proxy ...`.
Fix: alias-block auf Proxy-Route umbauen.

### 502 no such host / lookup weltgewebe-api-1

Ursache: edge-caddy ist nicht im `weltgewebe_default` Network.
Fix: `docker network connect weltgewebe_default edge-caddy` oder Compose Netz extern eintragen.

### Port 80 already in use beim Service `caddy` (Container typischerweise `weltgewebe-caddy-1`)

Ursache: ein anderes Caddy (edge) belegt 80/443.
Lösung:

- Entweder Service skalieren: `--scale caddy=0`

  ```sh
  docker compose --env-file /opt/weltgewebe/.env -p weltgewebe \
    -f infra/compose/compose.prod.yml -f infra/compose/compose.prod.override.yml \
    up -d --build --scale caddy=0 --remove-orphans
  ```

- Oder Ports im Stack deaktivieren: `ports: []` in `infra/compose/compose.prod.override.yml` definieren.
