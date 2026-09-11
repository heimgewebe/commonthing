---
id: deploy.README
title: Deployment-Übersicht
doc_type: reference
status: active
summary: Kanonischer Deployment-Stand und normative Beschreibung der Laufzeitumgebung.
relations:
  - type: relates_to
    target: docs/deployment.md
  - type: relates_to
    target: docs/deployment_governance.md
  - type: relates_to
    target: docs/deploy/vps.md
  - type: relates_to
    target: docs/deploy/security.md
  - type: relates_to
    target: docs/deploy/domain-mail-migration-ionos-to-inwx-mailbox-brevo.md
  - type: relates_to
    target: docs/deploy/secondary-domain-web-surfaces.md
---
# Weltgewebe – Deployment

Dieses Dokument beschreibt den **kanonischen Deployment-Stand** von Weltgewebe.
Es ist normativ. Abweichungen davon gelten als Drift.

**Aktuelle Produktion:** `commonserver` über den Public-VPS-Pfad. Der historische
Heimserver-Pfad ist retired/deprecated und kein Produktionsziel mehr.

**Weitere Dokumente:**

- [VPS-Deployment](vps.md) – kanonisches Produktionsrunbook für `commonserver`
- [Domain-/Providerarchitektur und historischer DDNS-Pfad](domain-mail-migration-ionos-to-inwx-mailbox-brevo.md) – Providerstand, stillgelegter Heimberry-Schreibpfad und Runtime-Beweisgrenze
- [Sekundäre Domain-Webflächen](secondary-domain-web-surfaces.md) – Artefakt- und Handoff-Vertrag für die Weltweberei-Informationsfläche und den späteren Heimserver-Edge (keine öffentliche Einsatzbereitschaft)
- [Deployment-Änderungsprotokoll](./CHANGELOG.md) – Infrastrukturänderungen und deren Auswirkungen
- [Drift-Taxonomie & Guard-Policy](./DRIFT_POLICY.md) – Klassifizierung und Handling von Drift

---

## 1. Zweck

Ziel dieser Dokumentation ist es,

- den **operativen Wahrheitsort** des Deployments festzulegen,
- den **Unterschied zwischen Konfiguration und Live-Zustand** klar zu machen,
- und den **Deployment-Snapshot** semantisch einzuordnen.

Dieses Dokument beschreibt **was gelten soll**.
Der Snapshot beschreibt **was ist**.

---

## 2. Kanonische Wahrheiten

### Operativer Repo-Pfad

```text
/opt/weltgewebe
```

Nur dieser Pfad ist operative Deployment-Quelle.
Andere Kopien oder Exporte gelten als nicht autoritativ.

### Docker-Compose

- **Kanonische Datei:**

  ```text
  infra/compose/compose.prod.yml
  ```

- **Compose-Projektname:**

  ```text
  weltgewebe
  ```

### Project Identity Enforcement & Zombie Guard

Das Deployment-Script `weltgewebe-up` setzt standardmäßig den Projektnamen `weltgewebe` und empfiehlt dessen Verwendung.
Es verhindert aktiv den Start, wenn ein paralleles "Zombie"-Projekt (z. B. mit Namen `compose` oder `infra`)
erkannt wird, das dieselben Konfigurationsdateien nutzt.

Dies verhindert Drift und Port-Kollisionen.

**Diagnose:**
Der Guard listet alle blockierenden Container mit Name, Projekt-Label und Config-Pfad auf.

**Remediation:**

1. Manuell: `docker compose -p <fremd_projekt> down`
2. Automatisch: Script mit `--purge-compose-leaks` starten. Das Script bindet die erkannten Ziele zuerst read-only an ihre unveränderliche 64-stellige Docker-Container-ID und führt `docker rm -f` erst aus, nachdem die vollständige fail-closed Deployment-Preflight-Kette erfolgreich war. Das konfigurierte externe `EDGE_GATEWAY_CONTAINER` ist ausdrücklich ausgenommen; wird es als Zombie erkannt, bricht der Lauf fail-closed ab und verlangt eine getrennte Remediation des Edge-Stacks.

**Optionen:**

- `--purge-compose-leaks`: Entfernt automatisch erkannte Zombie-Container ausschließlich über die zuvor gebundenen Container-IDs und erst unmittelbar vor dem eigentlichen Deploy. Das externe `EDGE_GATEWAY_CONTAINER` wird niemals über diesen Pfad entfernt. Ohne die Option bleibt der Guard fail-closed und löscht nichts.
- `--build-web`: Erzwingt einen Frontend-Build (erfordert `pnpm`).
- `--no-build-web`: Unterdrückt den Auto-Build des Frontends (warnt nur).

- Weitere Compose-Dateien (nicht primär produktiv):
  - `compose.core.yml` – Basiskomponenten
  - `compose.observ.yml` – Observability / Zusatzdienste
  - `compose.ops.override.yml` – Lokale Entwicklungs-/Ops-Umgebung (NATS + API-Port-Mapping für Debugging)

Der Web-Service des `dev`-Profils besitzt absichtlich kein eigenes `git` und
leitet deshalb keine Versionsidentität aus dem Containerdateisystem ab. Der
Aufrufer muss den vollständigen Quellcommit als `GIT_COMMIT_SHA` binden:
`make up` verwendet `git rev-parse HEAD`, der GitHub-Compose-Smoke den exakten
`github.sha`. Direkte Compose-Aufrufe müssen denselben Wert ausdrücklich
setzen. Fehlt er, bleibt die Client-Buildidentität fail-closed; ein erfundener
oder zeitabhängiger Ersatzcommit ist unzulässig.

Der nur im `dev`-Profil verwendete PgBouncer-Container ist in
`compose.core.yml` sowohl auf eine veröffentlichte Version als auch auf den
OCI-Manifest-Digest gepinnt. Bei Aktualisierungen müssen Registry-Existenz,
`linux/amd64` und `linux/arm64` sowie der vollständige Compose-Smoke erneut
belegt werden; `latest` ist dafür nicht zulässig.

Der Container lauscht intern auf Port `5432`; deshalb verbindet sich die API im
Compose-Netz über `pgbouncer:5432`. Nur die optionale Veröffentlichung auf dem
Entwicklungsrechner bleibt `6432:5432`, damit PgBouncer nicht mit dem direkten
PostgreSQL-Port verwechselt wird. PostgreSQL 16 verwendet SCRAM-Passwörter;
der Dev-Pooler muss daher `AUTH_TYPE=scram-sha-256` verwenden. `trust` oder ein
MD5-generiertes Userlist-Passwort sind mit diesem Pfad nicht zulässig.

Die repo-internen Caddy-Dateien `Caddyfile` und `Caddyfile.dev` müssen direkt
mit dem in `compose.core.yml` gepinnten Caddy-Image validierbar sein. Veraltete
globale Optionen wie `experimental_http3` dürfen nicht wieder eingeführt
werden; HTTP/3 wird von aktuellen Caddy-Versionen ohne diesen historischen
Schalter verwaltet.

---

## 3. Services & Netzwerk

### Services

| Service | Rolle | Netzwerk |
| ------- | ----- | -------- |
| api | Applikationslogik | intern |
| caddy | Stack-Routing | intern |
| db | PostgreSQL | intern |

### Netzwerkdetails

- **API**
  - läuft intern auf `8080`
  - **nicht** host-published
- **Caddy**
  - routet innerhalb des Stacks
  - **nicht** host-published im produktiven Heimserver-Deployment

**Konsequenz:**
Health-Checks dürfen **nicht** über `127.0.0.1:8080` (Host) erfolgen, sondern müssen container-intern laufen.

---

## 4. Persistenz (Volumes)

Docker Compose verwendet automatisch ein Prefix:

```text
<compose-project>_<volume-name>
```

### Logische Volumes

| Logisch | Compose-Name |
| ------- | ------------ |
| pg_data_prod | weltgewebe_pg_data_prod |
| gewebe_fs_data | weltgewebe_gewebe_fs_data |
| caddy_data | weltgewebe_caddy_data |
| caddy_config | weltgewebe_caddy_config |

Snapshots speichern **beide Namen**, um Verwechslungen zu vermeiden.
Sollten weitere Volumes live existieren (z. B. Legacy-Volumes), werden diese im Live-Snapshot ebenfalls mit Prefix erkannt.

Die kanonischen Volume-Suffixe sind im Compose-YAML definiert; Snapshot erkennt live alle `${COMPOSE_PROJECT}_*`.
Die obige Tabelle dient als Referenz für erwartete Volumes.

---

## 5. Konfiguration & Env-Variablen

`docker compose` rendert auch dann, wenn bestimmte Env-Variablen fehlen
(z. B. `DATABASE_URL`, `POSTGRES_*`, `NATS_URL`).

In diesem Fall entstehen **WARNINGS** und Default-Werte (leere Strings).

Der Deployment-Snapshot markiert dies explizit als:

```yaml
render_degraded: true
```

Das ist **keine Validierung**, sondern eine **sichtbare Beobachtung**.

### Performance & Limits

Die folgenden Quellenvariablen besitzen zwei bewusst getrennte Default-Ebenen:
Der API-Code und `.env.example` halten Domänendaten lokal im JSONL-Modus. Der
Produktions-Compose-Vertrag `infra/compose/compose.prod.yml` setzt dagegen Lesen
und alle vorhandenen Domänenschreibpfade gemeinsam auf `postgres`. Ein laufender
Serverzustand muss zusätzlich durch Runtime-Evidence belegt werden.

- **MAX_EDGES_CACHE**: Obergrenze der beim Start geladenen Edges (Default `500000`).
  Bei Erreichen wird die Datei nicht weiter gelesen und eine Warnung geloggt.
- **policies/limits.yaml / max_nodes_jsonl_mb**: maximale resultierende Größe
  für kanonische JSONL-Knoten-Appends und -Rewrites. Der Grenzfall ist erlaubt;
  ein zusätzliches Byte wird vor dem kanonischen Commit abgewiesen.
- **policies/limits.yaml / max_edges_jsonl_mb**: entsprechender wirksamer
  Grenzwert für JSONL-Fäden einschließlich Reaktivierungs- und
  Kaskaden-Rewrites. PostgreSQL-Schreibpfade lesen diese Dateigrenzen nicht.
  Löschungen dürfen einen bereits übergroßen JSONL-Bestand weiterhin verkleinern;
  ein Kapazitätsverstoß bei neuen/großen Writes antwortet mit HTTP 507 statt 500.
- **WELTGEWEBE_DOMAIN_READ_SOURCE**: lokal `jsonl`, Produktion `postgres`.
  `postgres` lädt Accounts, Knoten und Fäden beim API-Start aus den
  PostgreSQL-Domänentabellen.
- **WELTGEWEBE_DOMAIN_ACCOUNT_WRITE_SOURCE**: lokal `jsonl`, Produktion `postgres`.
  `postgres` persistiert Account-Erzeugung einschließlich Auth-
  Autoprovisionierung sowie `PATCH /accounts/me/profile` für die eigene
  Garnrolle in `domain_accounts`.
- **WELTGEWEBE_DOMAIN_NODE_WRITE_SOURCE**: lokal `jsonl`, Produktion `postgres`.
  `postgres` persistiert `POST /nodes` und `PATCH /nodes/{id}` in
  `domain_nodes`.
- **WELTGEWEBE_DOMAIN_EDGE_WRITE_SOURCE**: lokal `jsonl`, Produktion `postgres`.
  `postgres` persistiert `POST /edges` in `domain_edges`.
- **WELTGEWEBE_PASSKEY_CREDENTIAL_SOURCE**: lokaler In-Memory-Default,
  Produktion `postgres`. `postgres` persistiert Credentials und Signaturzähler
  in `passkey_credentials`.

`POST /nodes` und `POST /edges` akzeptieren optional eine UUID als
`operation_id`. Sie wird vom Webclient einmal pro Nutzeraktion erzeugt und bei
einem ungewissen Antwortausfall wiederverwendet. Der Server bindet sie an den
authentifizierten Account und die jeweilige Ressourcenart:

- erster erfolgreicher Schreibvorgang: `201 Created`;
- identische Wiederholung: `200 OK` mit derselben serverseitigen Objekt-ID und
  ohne zweite persistierte Zeile;
- dieselbe Kennung mit anderen fachlichen Daten: `409 Conflict`;
- fehlende `operation_id`: bisheriges Verhalten für ältere Clients.

Der Vertrag gilt in JSONL- und PostgreSQL-Modus und über einen API-Neustart
hinweg. Der ausführbare JSONL→PostgreSQL-Backfill-Beweis weist nach, dass
reservierte Vorgangsmetadaten ausschließlich in die internen Spalten übernommen
werden. Das Repository behauptet damit kein allgemeines Produktionskommando.
Der Vertrag ersetzt keine fachliche Duplikatprüfung und führt unabhängig
angelegte reale Knoten oder Fäden niemals automatisch zusammen.

Für einen PostgreSQL-Cutover müssen **Read-Quelle und alle tatsächlich
verwendeten Schreibquellen gemeinsam** auf `postgres` stehen. PostgreSQL-
Schreiben bei JSONL-Lesen oder fehlendem Pool ist verboten; die API bricht beim
Start ab oder der enge Routenschutz blockiert den Write. Es gibt keinen
stillen JSONL-Fallback und kein Dual-Write. Die Reihenfolge lautet immer:
Datenbank schreiben, danach In-Memory-Cache aktualisieren.

- **AUTH_AUTO_PROVISION_ROLE**: Default `gast`; zulässig sind `gast` und
  `weber`. `weber` verlangt eine konkrete `AUTH_ALLOW_EMAILS`- oder
  `AUTH_ALLOW_EMAIL_DOMAINS`-Liste und ist bei offener Registrierung verboten.
  `admin` wird abgelehnt. Ein Magic Link wird erst erzeugt, nachdem die neue
  Garnrolle dauerhaft gespeichert wurde.


### Maschinenlesbarer Produktionsbeleg

`scripts/ops/collect_production_runtime_evidence.py` erzeugt einen
maschinenlesbaren, wertredigierten Zustandsbeleg. Die Runtime-Erhebung ist rein
lesend und führt für jeden erlaubten Laufzeitschalter genau einen
`printenv`-Aufruf im laufenden API-Container aus. Die vollständige Containerumgebung wird weder
gelesen noch ausgegeben. Erlaubt sind ausschließlich:

- die vier Domänen-Lese-/Schreibquellen;
- die Passkey-Persistenzquelle;
- der API-Migrationsmodus.

Der Sammler verbindet diese Werte mit öffentlicher API-Readiness, Web- und
API-Buildidentität sowie exakt ausgewählten Backup- und Restore-Artefakten. Ausgewählte Metadaten und Dumps müssen reguläre
Dateien sein; Symlinks und übergroße Metadatendateien werden abgelehnt. Er
verwendet standardmäßig dieselbe Compose-Auswahl wie der VPS-Betrieb:
`compose.prod.yml`, danach `compose.vps.override.yml`, Projekt `weltgewebe` und
`/etc/weltgewebe/weltgewebe.env`. Abweichende Deployments müssen ihre
Compose-Dateien ausdrücklich und in derselben Reihenfolge mit wiederholtem
`--compose-file` angeben.

Beispiel mit konkret ausgewählten Belegen:

```bash
python3 scripts/ops/collect_production_runtime_evidence.py \
  --backup-manifest /var/backups/weltgewebe/postgres/<exact>.sha256.manifest \
  --restore-proof /var/backups/weltgewebe/postgres/proofs/<exact>.restore-proof \
  --output /var/lib/weltgewebe/evidence/production-runtime.json
```

Ein optionales `--secondary-copy-manifest` muss auf ein kopiertes Manifest
zeigen, dessen Dump im selben Verzeichnis liegt. Ein hashgleiches Abbild wird
nur als `secondary_copy` ausgewiesen. Es beweist weder einen anderen Host noch
eine getrennte Ausfallzone und hebt den Gesamtbeleg deshalb nicht über
`status=partial`. Ein gebrochener Pflichtbeweis oder eine widersprüchliche
Zweitkopie liefert `fail` und Code `1`; `partial` endet mit Code `2`.
`evidence_boundary.does_not_prove` nennt die fehlende Off-host- und
Disaster-Recovery-Grenze unabhängig davon, ob eine lokale Zweitkopie vorliegt.

Zur Migration bleibt `--offhost-backup-manifest` vorübergehend als veralteter,
in der CLI-Hilfe verborgener Alias für `--secondary-copy-manifest` erhalten;
beide Namen gleichzeitig sind unzulässig. Der Alias ändert keine Aussage:
Auch damit ist die Zweitkopie nur lokal/hashgleich und niemals ein Off-host-
oder Vollpass-Beleg. Nachgelagerte Aufrufer müssen Code `2` ausdrücklich als
intentionale gesunde Obergrenze behandeln und dürfen ihn nicht in einen Fehler
oder einen Vollpass umdeuten.

Die JSON-Ausgabe verwendet für diesen korrigierten Vertrag Schema-Version 2,
enthält UTC-Prüfzeitpunkt, explizite Read-only-/Redaktionsmarker und eine
`evidence_boundary` mit bewiesenen und nicht bewiesenen Aussagen. Bei
`--output` muss das Elternverzeichnis bereits existieren und darf kein Symlink
sein. Nur die Berichtdatei selbst wird atomar mit Dateimodus `0600` geschrieben;
der Sammler erzeugt keine Verzeichnisse. Der Bericht beweist den beobachteten
Zeitpunkt, nicht zukünftige Verfügbarkeit oder die fachliche Richtigkeit jeder
Datenbankzeile.


---

## 6. Deployment Snapshot

Der Snapshot ist eine **maschinelle Zustandsaufnahme**.

### Modi

| Modus | Zweck |
| ----- | ----- |
| dry | CI / Konfigurationsstand |
| live | Heimserver / Realzustand |

### Dry Snapshot (CI)

- kein laufender Compose-Stack erforderlich; Compose-Rendering erfolgt best-effort (CI-Umgebung).
- erfasst:
  - Compose-Datei-Hash
  - Render-WARNINGS
  - erwartete Services & Volumes

### Live Snapshot (Heimserver)

```bash
cd /opt/weltgewebe
SNAPSHOT_MODE=live bash scripts/deploy-snapshot.sh
```

- erfasst zusätzlich:
  - laufende Container (Status, Digest)
  - Volumes (dynamisch ermittelt per Prefix)
  - Bind-Mounts
  - Health (standardmäßig per Container-Check via `wget`/`curl` Fallback)

---

## 7. Nicht-Ziele

Der Snapshot ist **kein**:

- Auto-Deploy-Mechanismus
- Secret-Management
- Monitoring-Ersatz

Er dient ausschließlich der **Drift-Sichtbarmachung**.

---

## 8. Geltung

Bei Widerspruch gilt:

```text
Live-Snapshot > Dokumentation > Annahmen
```

Drift ist kein Fehler – **unsichtbare Drift ist es**.

Detaillierte Klassifizierung: [Drift-Taxonomie & Guard-Policy](./DRIFT_POLICY.md)

---

## 9. Feature Flags (Public Login)

Das System unterstützt öffentlichen Magic-Link-Login. Standardmäßig bleibt dieser
**deaktiviert**. Für den Public-VPS-Produktionspfad wird Public Login erst nach
einem separaten SMTP-Delivery-Receipt aktiviert.

### Produktionskonfiguration

```bash
AUTH_PUBLIC_LOGIN=1
AUTH_LOG_MAGIC_TOKEN=0
APP_BASE_URL=https://commonthing.net
SMTP_HOST=<provider-host>
SMTP_PORT=587
SMTP_AUTH=on
SMTP_USER=<secret>
SMTP_PASS=<secret>
SMTP_FROM=noreply@login.commonthing.net
```

`SMTP_FROM=noreply@login.commonthing.net` ist der kanonische Zielwert. Er darf
erst in die Produktions-Runtime übernommen werden, nachdem mailbox.org und Brevo
die neuen commonThing-Mailidentitäten angenommen haben und ihre exakt geforderten
DNS-Records autoritativ nachgewiesen sind. Bis dahin bleibt der belegte Legacy-
Absender in der laufenden Runtime unverändert.

Vor einem Rollout wird die vorbereitete Runtime-Secret-Quelle read-only geprüft:

```bash
python3 scripts/ops/check_public_login_smtp_readiness.py \
  --env-file /etc/weltgewebe/weltgewebe.env \
  --production-public-login \
  --expected-smtp-from noreply@login.commonthing.net
```

Der Check gibt nur Status- und Presence-Metadaten aus, keine SMTP-Werte.

### Regeln

- Wenn `AUTH_PUBLIC_LOGIN=1` gesetzt ist, **muss** `APP_BASE_URL` gesetzt sein.
- `APP_BASE_URL` muss `https://commonthing.net` sein.
- Ohne SMTP ist Magic Link Login nicht zustellbar.
- `AUTH_LOG_MAGIC_TOKEN=1` ist ausschließlich Debug/Development und kein Produktionsmodus.
- `SMTP_AUTH=off` ist für den Public-VPS-Rollout nicht zulässig, außer ein bewusst
  dokumentierter lokaler Relay-Sonderfall wird separat freigegeben.

---

## 10. Heimserver-Policy

Für den Betrieb auf einem Heimserver (z. B. hinter einer Firewall oder in einem lokalen Netzwerk) gilt ein striktes **Gateway-Prinzip**.

### Grundsätze

1. **Internal-Only Stack:**
   Weltgewebe ist im Heimserver-Produktionspfad internal-only. Die Frontdoor (Reverse Proxy mit den Ports 80 und 443) wird
   durch den Heimserver-Edge bereitgestellt.
   Das `weltgewebe-up` Script (Deployment Härtung) erzwingt dies auf API-Ebene fail-closed: Der *Host-Port Drift Guard*
   verhindert aktiv Deployments, bei denen der `api`-Service unzulässige Host-Ports (wie z.B. `8081`) exponiert.

2. **Referenzkonfiguration & Frontdoor:**
   `infra/caddy/Caddyfile.heim` dient als *repo-interne Referenz* für das Routing. Die operativ wirksame Frontdoor
   (Edge-Caddyfile) wird jedoch im Heimserver-Repository konfiguriert und durchgesetzt.

3. **Guards & Failure Bundles:**
   Das Deployment wird durch preflight `Guards` geschützt, z.B. CSP Contract Static Checks und Host-Port Prüfungen.
   Bei Integrationsfehlern auf Netzwerkebene nach dem Start erzeugt das Skript automatisch ein `Failure Bundle`
   (`weltgewebe-deploy-failure`) zur Diagnose, das Docker-Zustand, Logs und curl-Integrationstests sichert.

4. **Shared Network (Upstreams):**
   Lokale Upstream-Dienste (z. B. Leitstand) werden über ein dediziertes Docker-Netzwerk (`heimnet`) angebunden,
   nicht über Host-Ports.

### Einrichtung & Lokale Upstreams

1. **Netzwerk erstellen (Heimserver-Infrastruktur):**
   Damit externe Edge-Proxys oder Upstreams (z.B. Leitstand) sicher mit Weltgewebe kommunizieren können, wird ein
   dediziertes Netzwerk genutzt (statt Host-Ports).

   ```bash
   docker network create heimnet
   ```

2. **Lokale Edge-Simulation (Optionaler Override):**
   In Produktion übernimmt der Heimserver-Edge (außerhalb dieses Stacks) das Proxy-Routing.
   Für lokale Integrations- und Debug-Tests ohne reale Edge-Infrastruktur kann das Heimnet
   angebunden und die Referenz-Konfiguration lokal simuliert werden:

   ```bash
   docker compose \
     -f infra/compose/compose.prod.yml \
     -f infra/compose/compose.heimserver.override.yml \
     up -d
   ```

> **Hinweis für lokales Debugging (ohne Edge-Proxy):**
> Sollte der Stack *außerhalb* des Heimserver-Produktionspfades (z.B. für reine lokale Entwicklung) gestartet werden,
> bindet Caddy standardmäßig sicher an `127.0.0.1`. Setze `CADDY_BIND=0.0.0.0` (oder eine LAN-IP) in deiner
> `.env`-Datei, wenn du direkten Zugriff auf den Stack-internen Caddy benötigst. In Produktion übernimmt das Routing
> der Edge-Proxy.

### Verifikation

Prüfe, ob nur lokale Ports offen sind:

```bash
ss -lntp | grep -E ":(80|443)"
# Erwartet: 127.0.0.1:80, 127.0.0.1:443
```

Prüfe den Upstream-Zugriff (ohne DNS, via curl-Resolve):

```bash
# Zum lokalen Testen gegen den Host via --resolve (IP anpassen je nach Host-Binding):
curl -k --resolve leitstand.heimgewebe.home.arpa:443:<IP> https://leitstand.heimgewebe.home.arpa/

# Beispiele für <IP>:
# - Loopback: 127.0.0.1
# - LAN: 192.168.x.x
```
