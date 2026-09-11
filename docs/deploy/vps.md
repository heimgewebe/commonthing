---
id: deploy.vps
title: VPS-Deployment
doc_type: reference
status: active
summary: Dokumentation zum VPS-basierten Deployment.
relations:
  - type: relates_to
    target: docs/deploy/README.md
  - type: relates_to
    target: scripts/ops/check_public_live_readiness.py
  - type: relates_to
    target: scripts/ops/resolve_vps_public_bind.py
  - type: relates_to
    target: scripts/ops/reconcile_public_login_smtp_env.py
  - type: relates_to
    target: scripts/ci/tests/test_reconcile_public_login_smtp_env.py
  - type: relates_to
    target: .github/workflows/public-login-smtp-readiness.yml
---
# VPS Deployment Runbook

Dieses Runbook beschreibt den kanonischen Public-Produktionspfad für commonThing unter `commonthing.net`.
Der VPS `commonserver` stellt API, Datenbank, NATS und den Caddy-Frontdoor bereit. Das
Frontend wird im VPS-Checkout gebaut und vom Stack-internen Caddy unter der
Domain ausgeliefert.

## Voraussetzungen

1. **VPS**: Ein Linux-Server (z.B. Ubuntu) mit öffentlicher statischer IPv4 (und optional IPv6).
2. **Domain**: Zugriff auf die DNS-Verwaltung für `commonthing.net` und die Legacy-Domain `weltgewebe.net`.
3. **Docker & Docker Compose**: Müssen auf dem VPS installiert sein.

## 1. DNS Konfiguration

Richte folgende DNS-Records ein, damit die Domain auf deinen VPS zeigt:

* **A-Record**: `commonthing.net` -> `<VPS_IPV4_ADRESSE>`
* **A-Record**: `www.commonthing.net` -> `<VPS_IPV4_ADRESSE>`
* **Legacy-A-Record**: `weltgewebe.net` -> `<VPS_IPV4_ADRESSE>`
* **Legacy-A-Record**: `www.weltgewebe.net` -> `<VPS_IPV4_ADRESSE>`
* **A-Record**: `api.commonthing.net` -> `<VPS_IPV4_ADRESSE>`
* **Legacy-A-Record**: `api.weltgewebe.net` -> `<VPS_IPV4_ADRESSE>`
* **AAAA-Record** (nur falls IPv6 geprüft und freigegeben ist): entsprechende Hostnamen -> `<VPS_IPV6_ADRESSE>`

`api.commonthing.net` ist der kanonische API-Host. `api.weltgewebe.net` bleibt
als Kompatibilitätsname auf demselben API-Edge erhalten, bis seine spätere
Entfernung separat belegt ist. Caddy liefert die App
<!-- commonthing-naming: legacy -->
nur unter `commonthing.net`; `www.commonthing.net`, `weltgewebe.net` und
`www.weltgewebe.net` leiten permanent und URI-erhaltend auf den Apex um.

**API-Domain-Cutover:** Die Produktions-Caddy-Konfiguration für
`api.commonthing.net` muss zuerst gemergt, deployt und per Host-Routing-Smoke
belegt sein. Erst danach darf der DNS-Record von `api.commonthing.net` auf die
freigegebene VPS-Adresse umgestellt werden. Nach dem autoritativen DNS-Readback
werden öffentliches TLS, `/health/ready`, die private `/metrics`-Grenze und der
Legacy-Host erneut geprüft. Ein DNS-Wechsel vor dem Edge-Deploy oder die
vorzeitige Entfernung von `api.weltgewebe.net` ist nicht Teil dieses Ablaufs.

**Erster Domain-Cutover:** Nach dem Merge zuerst die A-Records von
`commonthing.net` und `www.commonthing.net` auf die ausgewählte VPS-Adresse
umstellen und autoritativ zurücklesen; erst danach den exakten Merge-Commit
aktivieren und den öffentlichen HTTPS-Readback ausführen. Der DNS-freie
HTTP-Smoke darf vorher laufen, aktiviert aber keinen Produktionsstack. Die
Legacy-Webhosts bleiben bis zur Stack-Aktivierung auf dem bisherigen Origin
erreichbar.

**Sitzungen und Passkeys:** Das Session-Cookie ist hostgebunden und wird nicht
von `weltgewebe.net` auf `commonthing.net` übertragen. Beim ersten Redirect ist
daher eine einmalige Neuanmeldung erwarteter Cutover-Effekt, kein Datenverlust.
`WEBAUTHN_RP_*` darf nicht allein aus der Beispielkonfiguration in die Runtime
übernommen werden: Vor einer Aktivierung oder RP-Änderung müssen die effektive
Live-Konfiguration und vorhandene Passkey-Credentials separat geprüft werden.

Der Deploy bindet Caddy niemals automatisch an eine beliebige Adresse, wenn der VPS
mehrere globale IPv4- oder IPv6-Adressen besitzt. Bei genau einer globalen IPv4
wird sie automatisch erkannt. Bei Mehrdeutigkeit muss `CADDY_BIND` explizit auf
die Adresse gesetzt werden, auf die die A-Records zeigen. IPv6 bleibt optional:
ohne globale IPv6-Adresse wird keine öffentliche IPv6-Bindung erzeugt; bei mehreren
globalen IPv6-Adressen muss `CADDY_IPV6_BIND` explizit zur freigegebenen AAAA-Adresse
(z. B. `[2001:db8::10]`) gesetzt werden.

## 2. Server Vorbereitung

Stelle sicher, dass Docker und das Docker Compose Plugin installiert sind.

```bash
# Beispiel für Ubuntu
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2
sudo usermod -aG docker $USER
# (Neu einloggen, damit Gruppenrechte greifen)
```

## 3. Deployment

### A. Repository klonen oder Dateien kopieren

Kopiere das Repository auf den VPS (z.B. nach `/opt/weltgewebe` oder `~/weltgewebe`).

### B. Umgebungsvariablen und Secrets

Für den Public-VPS-Pfad liegt die Runtime-Secret-Quelle außerhalb des
Git-Checkouts:

```text
/etc/weltgewebe/weltgewebe.env
owner: root
mode: 0600
```

Initial kann diese Datei aus `.env.prod.example` aufgebaut werden:

```bash
sudo install -d -m 700 -o root -g root /etc/weltgewebe
sudo install -m 600 -o root -g root .env.prod.example /etc/weltgewebe/weltgewebe.env
sudoedit /etc/weltgewebe/weltgewebe.env
```

**WICHTIG (Secrets):**

* Die Runtime-Datei enthält sensible Daten. Sie darf **niemals** ins Git-Repository committet werden.
* Secret-Werte gehören nicht in GitHub-Kommentare, Logs oder Chat.
* `/opt/weltgewebe/.env` kann als Legacy-/Rollback-Quelle existieren.

Anpassungen:

* **Datenbank**: Wähle ein starkes Passwort für `POSTGRES_PASSWORD` und passe
  `DATABASE_URL` entsprechend an.
* **Web Upstream**: Konfiguriere den Host und die URL deines Frontends (Vercel oder Cloudflare).
  * `WEB_UPSTREAM_HOST`: **Nur die Domain** ohne Schema (z.B. `leitstand.pages.dev`).
  * `WEB_UPSTREAM_URL`: Die volle Origin **ohne Pfad**, muss mit `https://` beginnen
    (z.B. `https://leitstand.pages.dev`).

### C. Starten

`scripts/weltgewebe-up` ist der einzige unterstützte Deploy-Einstiegspunkt:

```bash
# Baut bzw. pullt Container und startet den Stack
./scripts/weltgewebe-up --with-caddy

# Optional: Mit Image-Cleanup (Vorsicht!)
PRUNE_IMAGES=1 ./scripts/deploy_vps.sh
```

`scripts/deploy_vps.sh` ist nur noch ein deprecateter Shim, der an
`weltgewebe-up` delegiert.

Für einen reinen API-Rollout darf nicht der gesamte Stack neu abgeglichen werden:

```bash
sudo -n ./scripts/weltgewebe-up --deploy-scope api
```

Für ein kontrolliertes Migrationsfenster gilt:

```bash
sudo -n ./scripts/weltgewebe-up --deploy-scope migration
```

Der Migrationsscope startet nur `api` mit `--no-deps`, wendet die eingebetteten
Migrationen an und setzt den API-Container anschließend automatisch wieder auf
`WELTGEWEBE_API_STARTUP_MIGRATIONS=verify-applied`. PostgreSQL, NATS und Caddy
müssen bereits laufen und ihre Containeridentitäten müssen über den gesamten
Lauf unverändert bleiben. Vor der Mutation wird ein maschinenlesbarer JSON-Plan
unter `.ops/deploy-plan-migration.json` geschrieben. Mit `--plan-only` kann der
Wirkungsplan ohne Containeränderung geprüft werden.

Jeder API-Release wird mit dem vollständigen Git-Commit und dessen unveränderlichem
Git-Commitzeitpunkt kompiliert. Fehlende Werte brechen den Release-Build ab. Nach
einem API-Rollout muss die öffentliche Releaseidentität separat geprüft werden:

```bash
commit="$(git rev-parse HEAD)"
build_timestamp="$(git show -s --format=%cI "$commit")"
EXPECTED_COMMIT="$commit" \
EXPECTED_BUILD_TIMESTAMP="$build_timestamp" \
./scripts/ops/verify-api-release-identity.sh
```

Der Readback vergleicht `/api/version` mit dem von der API selbst gelieferten
`X-Weltgewebe-API-Build`-Header. Der globale `X-Weltgewebe-Build`-Header gehört
weiterhin zum Web-/Caddy-Build und darf bei einem API-only-Rollout unverändert
bleiben. Dadurch ist für diese Prüfung kein Caddy-Neustart nötig.

Ein manueller Aufruf **muss** die VPS-Override-Datei mitgeben. Ohne sie fehlen
`Caddyfile.vps`, `APP_BASE_URL`, `POLICY_LIMITS_PATH`, die IPv6-Bindings sowie
`AUTH_TRUSTED_PROXIES` und die `AUTH_RL_*`-Rate-Limits — Caddy bliebe an
`127.0.0.1` gebunden und der Host wäre nicht öffentlich erreichbar:

```bash
docker compose \
  --env-file /etc/weltgewebe/weltgewebe.env \
  -f infra/compose/compose.prod.yml \
  -f infra/compose/compose.vps.override.yml \
  up -d --build
```

**Troubleshooting:**

Alle Compose-Aufrufe brauchen dieselbe Datei- und Projektauswahl wie der Deploy,
sonst sprechen sie einen anders zusammengesetzten Stack an. Der Kürze halber:

```bash
alias wg-compose='docker compose --env-file /etc/weltgewebe/weltgewebe.env -p weltgewebe \
  -f infra/compose/compose.prod.yml -f infra/compose/compose.vps.override.yml'
```

Wenn API-Healthchecks fehlschlagen, prüfe im Container:

```bash
wg-compose logs api
# Der Container-Healthcheck verwendet ausschließlich Readiness.
wg-compose exec api wget -qO- http://localhost:8080/health/ready
# Liveness ist nur eine getrennte Diagnose für Prozesslebendigkeit.
wg-compose exec api wget -qO- http://localhost:8080/health/live
```

Ein erfolgreicher Liveness-Aufruf darf einen fehlgeschlagenen Readiness-Aufruf
nicht überstimmen: Der Container bleibt ungesund, solange die API ihre
Abhängigkeiten und Policy-Verträge nicht als bereit meldet. Im
PostgreSQL-Produktionspfad umfasst dies ausdrücklich die beaufsichtigten
Outbox-Worker, den semantisch passenden JetStream/Durable-Consumer sowie die
60-Sekunden-Grenze für *jetzt ausführbaren* Outbox-Rückstand sowie den
Receipt-Probe im festen aktuellen Zehn-Minuten-Fenster. Geplante Retry-Backoffs
und ältere Historienlücken blockieren Readiness nicht. Quarantäne wird über
private Metriken beobachtet;
eine einzelne bewusst quarantänisierte Nachricht überstimmt Readiness nicht.

Startet die API gar nicht und meldet `AUTH_TRUSTED_PROXIES is not set`, dann
läuft der Stack ohne `compose.vps.override.yml` (siehe oben) oder die
Runtime-Env-Datei überschreibt den Default mit einem leeren Wert.

### D. Backup (Strategie)

Richte einen Cronjob ein, um regelmäßig Dumps der Datenbank zu erstellen und alte Backups zu rotieren
(z.B. 14 Tage behalten).

1. Verzeichnis anlegen:

   ```bash
   mkdir -p /var/backups/weltgewebe
   ```

2. Cronjob einrichten (`crontab -e`):

   Verwende `set -o pipefail`, um Fehler in der Pipe (z.B. bei `pg_dump`) korrekt zu erkennen und zu loggen.

   ```bash
   # Täglich um 3 Uhr nachts: Dump erstellen, zippen, rotieren und Fehler loggen
   0 3 * * * /bin/bash -c 'set -o pipefail; docker compose -f /opt/weltgewebe/infra/compose/compose.prod.yml \
     exec -T db pg_dump -U welt weltgewebe | gzip > /var/backups/weltgewebe/db_$(date +\%F).sql.gz \
     && find /var/backups/weltgewebe/ -name "db_*.sql.gz" -mtime +14 -delete' \
     || echo "backup failed $(date)" >> /var/backups/weltgewebe/backup.log
   ```

## Wartung

* **Logs ansehen**: `wg-compose logs -f` (Alias siehe Troubleshooting oben)
* **Neustart**: `wg-compose restart`
* **Updates**: Repository aktualisieren (`git pull`), dann `./scripts/weltgewebe-up` ausführen.

## 4. Aktueller Zielpfad: VPS als Public Runtime

Für den Public-VPS-Pfad ist `scripts/weltgewebe-up` der bevorzugte
Deploy-Wrapper. `vps` ist der Default-Zieltyp:

```bash
cd /opt/weltgewebe
sudo -n env \
  GIT_CONFIG_COUNT=1 \
  GIT_CONFIG_KEY_0=safe.directory \
  GIT_CONFIG_VALUE_0=/opt/weltgewebe \
  DEPLOY_TARGET=vps \
  ./scripts/weltgewebe-up --branch main
```

Der privilegierte Operatorpfad ist beabsichtigt: Die kanonische Runtime-Datei
liegt in einem nur für `root` zugänglichen Verzeichnis. Ein unprivilegierter
Aufruf bricht deshalb klar und ohne Legacy-Fallback ab.

Wenn `ENV_FILE` nicht oder leer gesetzt ist, wählt der VPS-Zieltyp immer
`/etc/weltgewebe/weltgewebe.env`. Fehlt diese Datei oder ist sie für den
aufrufenden Operator nicht lesbar, endet der Deploy fail-closed. Für Tests,
Rollback oder abweichende Installationen kann eine andere, ausdrücklich
gewählte Quelle verwendet werden:

```bash
DEPLOY_TARGET=vps ENV_FILE=/path/to/runtime.env ./scripts/weltgewebe-up --branch main
```

SMTP-Zugangsdaten werden nur über TLS übertragen: Port `465` nutzt implizites
TLS, die Submission-Ports `587` und `2525` erzwingen STARTTLS. Auf anderen
Ports verweigert die API SMTP-Authentifizierung, statt Zugangsdaten im Klartext
zu senden.

Eine bestehende Repo-lokale Runtime-Datei darf nicht vollständig über die
kanonische Datei kopiert werden. Für die einmalige, selektive Übernahme der
Public-Login- und SMTP-Schlüssel dient der wertredigierende Reconciler.
Quelle, Ziel und ihre Verzeichnisse müssen root-eigen und nicht gruppen- oder
weltbeschreibbar sein. Das Backup-Verzeichnis wird einmalig als `0700`
vorbereitet:

```bash
sudo -n install -d -m 0700 -o root -g root /var/backups/weltgewebe/env
```

Die Vorschau läuft ebenfalls als `root` und führt dieselben Pfad-, Eigentümer-,
Modus-, Symlink- und Inhaltsprüfungen wie der Schreibvorgang aus. Sie verändert
keine Runtime-Datei und gibt keine Werte aus:

```bash
cd /opt/weltgewebe
PREVIEW="$(sudo -n python3 scripts/ops/reconcile_public_login_smtp_env.py \
  --source /opt/weltgewebe/.env \
  --destination /etc/weltgewebe/weltgewebe.env \
  --backup-dir /var/backups/weltgewebe/env \
  --smtp-from-override noreply@login.commonthing.net \
  --json)"
printf '%s\n' "$PREVIEW"
PLAN_SHA256="$(printf '%s\n' "$PREVIEW" | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["plan_sha256"])')"
```

`--apply` akzeptiert ausschließlich den Hash dieser Vorschau. Ändert sich
Quelle oder Ziel zwischen beiden Schritten, bricht der Vorgang vor Backup und
Mutation ab:

```bash
sudo -n python3 scripts/ops/reconcile_public_login_smtp_env.py \
  --source /opt/weltgewebe/.env \
  --destination /etc/weltgewebe/weltgewebe.env \
  --backup-dir /var/backups/weltgewebe/env \
  --smtp-from-override noreply@login.commonthing.net \
  --apply \
  --expected-plan-sha256 "$PLAN_SHA256" \
  --json
```

Die Operation übernimmt ausschließlich die dokumentierten Auth-/SMTP-Schlüssel aus
der Quelle und setzt zusätzlich `APP_BASE_URL` unabhängig vom Quellwert exakt auf
`https://commonthing.net`. `--smtp-from-override` akzeptiert nur
`noreply@login.commonthing.net` oder `noreply@login.weltgewebe.net`; damit kann ein
Mail-Rollback die bereits abgeschlossene Webidentität nicht zurückdrehen. Derselbe
Override muss bei Dry-run und Apply verwendet werden, weil sein geplanter Zielinhalt
im `plan_sha256` gebunden ist.

Vor dem atomischen Austausch legt das Werkzeug ein bytegenaues `0600`-Backup an,
liest dieses vor dem Austausch bytegenau zurück und gibt keine Secret-Werte aus.
Der `plan_sha256` bindet Quell-, Ziel- und Backup-Pfad, Geräte- und
Inode-Identitäten, Eigentümer, Modi, vollständigen Quelltext, bisherigen
Zielinhalt und geplanten Zielinhalt. Dadurch kann die Vorschau weder auf eine
andere Datei noch auf ein ausgetauschtes gleichlautendes File angewendet werden.
Einzelne Wert-Hashes werden nicht ausgegeben. Ein bereits gehaltener
Reconcile-Lock führt nach spätestens 15 Sekunden zu einem Fehler statt zu
unbegrenztem Warten.

### Restore der kanonischen Env-Datei

Der JSON-Receipt des Apply-Schritts nennt den erzeugten Backup-Pfad. Vor einem
Restore wird die laufende fehlerhafte Konfiguration nicht editiert, sondern das
Backup als neue `0600`-Datei vorbereitet und atomisch eingesetzt:

```bash
BACKUP=/var/backups/weltgewebe/env/weltgewebe.env.bak-<receipt-suffix>
sudo -n install -m 0600 -o root -g root \
  "$BACKUP" /etc/weltgewebe/.weltgewebe.env.restore
sudo -n mv -f \
  /etc/weltgewebe/.weltgewebe.env.restore \
  /etc/weltgewebe/weltgewebe.env
```

Danach wird derselbe kanonische Deploy-Pfad erneut ausgeführt und die
Readiness geprüft. Das Backup bleibt bis zum erfolgreichen Live-Smoke erhalten.

Der VPS-Zieltyp unterscheidet sich vom historischen Heimserver-Ziel:

* er nutzt `infra/compose/compose.vps.override.yml`,
* er startet den internen Caddy-Service standardmäßig mit,
* er verwendet `infra/caddy/Caddyfile.vps`,
* er erzwingt keine `*.home.arpa`-DNS-Guards,
* er verlangt kein `/opt/heimgewebe/edge` und kein `edge-ca.crt`.

`Caddyfile.vps` dient den statischen SvelteKit-Build aus `apps/web/build`.
Für vorgerenderte HTML-Routen muss der Catch-all neben exakten Dateien auch
`{path}.html` versuchen, bevor er auf `/index.html` fällt. Andernfalls würde ein
Pfad wie `/settings` den generischen App-Shell-Fallback statt `settings.html`
ausliefern und könnte eine falsche oder veraltete Route initialisieren.

Erwartete Reihenfolge:

```caddy
try_files {path} {path}.html /index.html
```

Vor dem INWX-DNS-Cutover darf der Stack lokal auf dem VPS über den HTTP-Host-Header
geprüft werden, ohne öffentliche DNS-Records umzubiegen:

```bash
curl -H 'Host: commonthing.net' http://127.0.0.1/health/proxy
curl -H 'Host: commonthing.net' http://127.0.0.1/api/health/ready
curl -sS -o /dev/null -D - -H 'Host: api.commonthing.net' \
  http://127.0.0.1/health/ready
```

Der letzte Request muss bereits vor dem DNS-Wechsel mit HTTP `308` exakt auf
`https://api.commonthing.net/health/ready` zeigen. Das beweist die neue
Host-Routing-Regel, aber noch kein öffentlich gültiges TLS-Zertifikat.

Nach dem DNS-Cutover sind zusätzlich die öffentlichen HTTPS-Pfade zu prüfen:

```bash
curl -fsS https://commonthing.net/api/health/ready
curl -fsS https://api.commonthing.net/health/ready
curl -sS -o /dev/null -w '%{http_code}\n' https://api.commonthing.net/metrics
# commonthing-naming: legacy
curl -fsS https://api.weltgewebe.net/health/ready
curl -sS -o /dev/null -w '%{http_code}\n' https://api.weltgewebe.net/metrics
curl -sS -o /dev/null -D - 'https://weltgewebe.net/map?from=legacy'
```

Beide Metrics-Requests müssen `404` liefern. Der neue Host ist damit kanonisch
belegt, während der alte Host ausdrücklich als Kompatibilitätsweg weiterläuft.

### Public live readiness receipt

Nach einem Public-Cutover kann der öffentliche Zustand mit einem read-only
Operator-Check reproduzierbar geprüft werden:

```bash
DEPLOY_COMMIT="<ausgelieferter-commit-sha>"

# commonthing-naming: legacy
python3 scripts/ops/check_public_live_readiness.py \
  --legacy-api-domain api.weltgewebe.net \
  --expected-ip 94.16.121.119 \
  --api-domain api.commonthing.net \
  --expected-version "${DEPLOY_COMMIT:0:8}" \
  --expected-commit "${DEPLOY_COMMIT}" \
  --authoritative-server ns.inwx.de \
  --authoritative-server ns2.inwx.de \
  --authoritative-server ns3.inwx.eu
```

`DEPLOY_COMMIT` ist der Commit, dessen Frontend/API tatsächlich ausgeliefert
werden soll. Das ist nicht automatisch der aktuelle lokale `HEAD`: reine
Doku-/Tooling-PRs können `main` vor den live ausgelieferten App-Build setzen. Für
einen reinen Oberflächen-Receipt ohne Versionsbindung können `--expected-version`
und `--expected-commit` weggelassen werden.

Wenn IPv6 freigegeben und die AAAA-Records gesetzt sind, wird zusätzlich die
IPv6-DNS-Erwartung geprüft:

```bash
  --expected-ipv6 2a03:4000:21:c74:b47a:7bff:fee6:70d
```

Der Check liest keine Runtime-Secrets und verändert keinen Serverzustand. Er
prüft DNS-A-Records, den kanonischen commonThing-HTTPS-Apex, permanente und
<!-- commonthing-naming: legacy -->
URI-erhaltende Redirects von `www.commonthing.net`, `weltgewebe.net` und
`www.weltgewebe.net`, `/map`, den kanonischen API-Host
`api.commonthing.net/health/ready` sowie den Legacy-API-Host
`api.weltgewebe.net/health/ready`. Die privaten öffentlichen Metrics-Grenzen
`/api/metrics`, `api.commonthing.net/metrics` und `api.weltgewebe.net/metrics`
müssen jeweils HTTP `404` liefern; zusätzlich prüft der Receipt `/_app/version.json`,
lokale Basemap-Style-, Glyph- und PMTiles-Auslieferung. Ein PASS ist ein
Public-HTTP(S)-/Basemap-Receipt,
aber kein Beweis für IPv6, Mail/SMTP oder Public Login. Der
Credential-Source-Cutover wird über den ausgewählten `ENV_FILE`-Pfad und die
Dateimetadaten der Runtime-Secret-Quelle belegt, nicht über den HTTP(S)-Check
allein.

## Lokale semantische Suchprojektion aktivieren

Die produktive semantische Suche verwendet keinen Cloud-Provider. Der gepinnte
Ollama-Sidecar teilt den Netzwerk-Namensraum der API und bindet ausschließlich
an `127.0.0.1:11434`; es wird kein Host-Port veröffentlicht. Das Modellvolume
ist regenerierbare Projektionsinfrastruktur, während `domain_nodes` in
PostgreSQL die einzige Datenwahrheit bleibt. Die Sichtbarkeit liegt in der
serverseitigen Spalte `domain_nodes.search_visibility`; gewöhnliche bestehende
Knoten werden bei der Migration als öffentlich übernommen. Malformed explizite
Altwerte werden geschlossen als verborgen behandelt. Clients können diese
Spalte nicht über beliebige Payload-Felder überschreiben.

Der normale commitgebundene Reconciler prüft vor Build oder Containeränderung,
ob mindestens 8 GiB freier Speicher, 5 GiB verfügbarer Arbeitsspeicher und drei
Online-CPUs vorhanden sind. Danach rollt er API, Sidecar und den begrenzten
Projektions-Worker gemeinsam aus. Der Worker wartet auf den exakt gepinnten
Modelldigest und verarbeitet danach sowohl den initialen Bestand als auch neue
Projektionsjobs fortlaufend. Öffentliche Knoten erhalten eine semantische
Projektion. Private Knoten mit gültigem Eigentümer bleiben ausschließlich als
autorisierte lexikalische PostgreSQL-Projektion erhalten und werden nie an
Ollama übergeben. Alle übrigen Sichtbarkeitszustände werden nur als
inhaltsfreier Platzhalter abgebildet.
Die Modellbeschaffung, der begrenzte Backfill und die Aktivierung erfolgen
absichtlich separat über `scripts/ops/activate-production-search-vps.sh`. Der
Helper hält denselben Produktionslock wie der Deploypfad, prüft Live-Commit,
Ressourcenreserve, Image-, Runtime- und Modelldigest, verarbeitet die
Projektionsjobs in begrenzten Batches und ruft den atomaren PostgreSQL-Gate erst
nach vollständiger Konvergenz auf. Eine fehlgeschlagene Vorbereitung verändert
keine kanonischen Knotendaten und lässt die Suche fail-closed.

Bei der Aktivierung wird die vorherige `active` Generation erfasst. Vor dem
Aufruf von `weltgewebe_activate_search_generation` führt das Skript eine
Kandidaten-Vorabprüfung durch (öffentlicher Probe-Knoten). Ist kein öffentlicher
Knoten vorhanden (z. B. rein private Datenbank), wird `semantic_probe_status` auf
`not_applicable` gesetzt. Nach der Umschaltung verifiziert das Skript die
Live-Suche (erfordert `mode == "hybrid"` und exakten Treffer des Probe-Knotens).
Schlägt die Verifikation fehl, wird automatisch auf die vorherige Generation
zurückgerollt (`weltgewebe_activate_search_generation(previous_generation_id)`).

Ein Online-Downgrade der Datenbankmigration (`down.sql`) wird bewusst durch eine
Exception blockiert, da private lexikalische Projektionen nicht ohne Re-Embedding
auf den alten Vektor-Zwangszustand zurückgeführt werden können. Im Fehlerfall ist
ein Roll-forward der Korrektur zu bevorzugen (bzw. Stoppen des Workers und
Roll-forward).
