---
id: docs.runbook
title: Runbook
doc_type: runbook
status: active
summary: Aktuelle lokale, diagnostische und wiederherstellungsbezogene Betriebsabläufe.
relations:
  - type: relates_to
    target: runbooks/README.md
  - type: relates_to
    target: docs/runbook.observability.md
  - type: relates_to
    target: docs/deploy/README.md
---

# Allgemeines Runbook

Dieses Dokument enthält nur Abläufe, die mit der aktuellen Repositorystruktur
vereinbar sind. Für öffentliche Deployments, Migrationen und Mailbetrieb gelten
die spezialisierten Runbooks unter [`docs/deploy/`](deploy/README.md).

## 1. Lokales Onboarding

### Voraussetzungen

- Git
- Docker mit Compose
- `just`
- Node.js und pnpm gemäß den versionierten Repositorymetadaten
- Rust nur für direkte API-Entwicklung außerhalb des Containerpfads

### Start

```bash
git clone <repo-url>
cd weltgewebe
cp .env.example .env
just up
```

Prüfen:

```bash
curl -fsS http://127.0.0.1:8081/api/health/live
curl -fsS http://127.0.0.1:8081/api/health/ready
```

Die Web-/Caddy-Frontdoor liegt im Standard-Core-Profil auf Port `8081`. Der
SvelteKit-Devserver kann für direkte Webentwicklung separat gestartet werden;
er ist nicht mit dem Compose-Frontdoor gleichzusetzen.

### Qualitätsprüfung

```bash
just check
just ci
```

`just ci` ist der breitere lokale Spiegel. Datenbank-, Browser- und
Deploybeweise besitzen zusätzliche Workflows und können externe Dienste oder
Container benötigen.

## 2. Diagnose

### Vor jeder Änderung

1. Git-Head, Branch und Dirty-State prüfen.
2. laufende Compose-Projekte und Zielprofile prüfen.
3. effektive Env nur redigiert beziehungsweise als Vorhandenseinsmetadaten
   lesen.
4. Datenquellschalter und Migrationsmodus prüfen.
5. erst danach einen Reparaturpfad wählen.

### Health und Logs

```bash
docker compose -f infra/compose/compose.core.yml ps
curl -fsS http://127.0.0.1:8081/api/health/live
curl -fsS http://127.0.0.1:8081/api/health/ready
docker compose -f infra/compose/compose.core.yml logs --tail=200 api caddy
```

Logs dürfen nicht unredigiert weitergegeben werden, wenn Token-, Mail- oder
Secretwerte enthalten sein könnten.

### Typische Fehlerklassen

| Symptom | zuerst prüfen |
|---|---|
| `502` oder leere Frontdoor | Caddy-Upstream, Compose-Netz, TLS-/Hostvertrag |
| Loginmail fehlt | Public-Login-Schalter, SMTP-Bereitschaft, Absender und redigierte Logs |
| Login gilt nur scheinbar lokal | Cookie-Scope, Sessionstore, `/auth/me`, Browserprofil |
| Änderung verschwindet nach Neustart | Domain-Lese- und Schreibquelle |
| API startet nicht | Konfigurationsvalidierung, Proxyentscheidung, Migrationsmodus |
| Readiness `event_chain=false` | Outbox-Worker, Stream-/Durable-Vertrag, aktuell ausführbaren Rückstand und bounded Receipt-Probe prüfen |
| Karte bleibt leer | Web-Build, Basemap-Konfiguration, PMTiles-Range und API-Daten getrennt |

Bei PostgreSQL als Domänenlesequelle oder aktiviertem Domänenschreibpfad bedeutet
grüne Readiness, dass Relay und Receipt-Consumer laufen, der erwartete JetStream
samt durablem Consumer semantisch passt, kein *jetzt ausführbares* Outbox-Ereignis
mehr als 60 Sekunden überfällig ist und der bounded Receipt-Probe im aktuellen
Zehn-Minuten-Fenster keine fällige Lücke findet. Retry-Backoff in der Zukunft und
historische Lücken außerhalb dieses Fensters sind kein Readiness-Ausfall. Die
privaten Metriken `domain_event_chain_snapshot_up`,
`domain_outbox_actionable_pending`, `domain_outbox_quarantine_present`,
`domain_outbox_oldest_actionable_age_seconds`,
`domain_event_receipt_probe_missing`, `domain_event_receipt_probe_age_seconds`
und `domain_event_worker_up` liefern die begrenzte Diagnose. Eine einzelne bewusst
quarantänisierte Poison-Nachricht ist sichtbar, aber allein kein globaler
Ausfall; Requeue bleibt eine explizite Operatorentscheidung.

### Webartefakt-Umschaltung und verifizierter Readback

`scripts/ops/install-web-artifact.sh` validiert Archiv, Dateibaum, Version und
Commit. Eine hostweite `flock`-Sperre serialisiert Umschaltung und Rollback. Ein
bereits aktiver, bytegleich geprüfter Release kann idempotent erneut verifiziert
werden, ohne Caddy neu zu erzeugen. Bei einem neuen Release wird der Webroot
atomar per Symlink umgeschaltet und Caddy danach neu erzeugt. Der laufende alte
Container hält bis dahin seinen bisherigen Bind-Mount; der öffentliche Readback
kann deshalb sinnvoll erst nach der Neuerzeugung den neuen Stand prüfen.

Der Readback akzeptiert kleingeschriebene HTTP/2-Headernamen, entfernt
abschließenden Leerraum, verlangt `Cache-Control: no-store` und bindet
`X-Weltgewebe-Build`, Version und Commit gemeinsam. Sowohl einzelne
`curl`-Aufrufe als auch die gesamte Prüfschleife besitzen Zeitgrenzen; Anzahl,
Dauer und letzter Fehlergrund werden ausgegeben. Ein Rollback löscht einen neuen
Release nur, wenn derselbe Lauf ihn angelegt hat und seine Dateisystemidentität
unverändert ist. Dies ist ein verifizierter Umschalt- und Rollbackvertrag, aber
kein Blue-Green- oder allgemeiner Zero-Downtime-Vertrag. Der Default von
`WEB_RELEASES_DIR` in `scripts/ops/install-web-artifact.sh` ist
`/opt/weltgewebe/apps/web/releases/`; dieses Verzeichnis ist Laufzeitzustand und
darf den Produktionscheckout nicht verschmutzen.

## 3. Datenquellen und Migrationen

Im belegten Produktionspfad `commonserver` ist PostgreSQL die Lese- und
Schreibwahrheit für Accounts/Garnrollen, Knoten und Fäden. JSONL bleibt für
lokale Entwicklung, Legacy-Daten, Import/Export und explizite Rollbacks
erhalten, ist aber keine offene Produktions-Cutover-Behauptung mehr. Vor jeder
Änderung an Datenquellen:

1. Backfill-/Orphan-Belege lesen,
2. PostgreSQL-Lese- und Schreibquellen gemeinsam prüfen,
3. JSONL nur als bewusst dokumentierten Legacy-/Rollbackpfad verwenden,
4. Migrationen separat autorisieren,
5. Restart- und Readback-Verhalten prüfen.

Ein Health-Smoke, Compose-Start oder erfolgreicher Build erteilt keine
Migrationsfreigabe. Siehe
[`docs/deploy/vps-db-initialization-boundary.md`](deploy/vps-db-initialization-boundary.md).

## 4. Wiederherstellung

### Heute belegbarer Rahmen

Das Repository enthält jetzt einen kanonischen logischen PostgreSQL-Backup- und
Restore-Proof-Vertrag unter `scripts/ops/`. Es enthält weiterhin keinen
implementierten WAL-/PITR-, Object-Lock-, Outbox-Replay- oder
Projector-Rebuild-Vertrag. RTO/RPO-Werte dürfen deshalb nur für den tatsächlich
betriebenen logischen Backup-Pfad behauptet werden.

Ein sicherer Wiederherstellungstest muss in einer entbehrlichen Umgebung:

1. den exakten Repository-/Image-Stand festhalten,
2. `scripts/ops/postgres-backup.sh` mit Retention, Gzip-Test und
   SHA256-Manifest ausführen,
3. `scripts/ops/postgres-restore-latest-proof.sh` einen netzlosen
   PostgreSQL-Wegwerfcontainer erstellen und wieder entfernen lassen,
4. Migrationen über `_sqlx_migrations` und den konfigurierten
   `WELTGEWEBE_API_STARTUP_MIGRATIONS`-Modus prüfen,
5. die effektiven Domainquellen dokumentieren,
6. den Off-Host-Pull nach `~/merges/weltgewebe-production-backups` und den
   nachfolgenden Restic-Snapshot belegen,
7. API-Readiness, Auth-Sitzung, Passkey-Credential-Store und repräsentative
   Domänenlesevorgänge testen,
8. Ergebnis, Dauer, Datenstand und Restlücken festhalten.

Ohne registrierte Sicherungsquelle wird kein hypothetisches S3-, WAL-, Nomad-,
Outbox- oder JetStream-Verfahren erfunden.

### Noch nötig für weitergehende Wiederherstellungsziele

- ausdrücklich beschlossene RTO/RPO-Ziele;
- WAL-/PITR nur bei einem belegten Bedarf an feinerem RPO;
- regelmäßige Auswertung von Timer-, Pull- und Restic-Belegen;
- wiederholte Drills nach Änderungen am Schema oder Betriebsweg.

## 5. Public Login und SMTP

Der aktuelle Produktionsablauf steht in
[`docs/deploy/vps.md`](deploy/vps.md) und im Mail-Migrationsrunbook. Mindestregeln:

- `AUTH_PUBLIC_LOGIN` erst nach grünem SMTP-Readiness-Preflight aktivieren,
- `AUTH_LOG_MAGIC_TOKEN` in Produktion deaktiviert halten,
- `AUTH_TRUSTED_PROXIES` ausdrücklich setzen,
- Secretwerte nicht ausgeben,
- nach Änderung API-Readiness und reale Mailzustellung prüfen.

Die API erzwingt IP- und E-Mail-basierte Ratelimits. Caddy bereinigt fremde
Forwardingheader und setzt die beobachtete Remote-Adresse neu. Ein alternativer
Proxy muss denselben Vertrag erfüllen.

## 6. Account- und Produkt-Smokes

Der erste Admin kann über den ausdrücklich aktivierten Bootstrap-Pfad erzeugt
werden. Weitere Accounts entstehen über den administrativen API-Pfad. Die
konkreten Befehle und Variablen stehen in den Dev-/Deployskripten; echte Namen,
Adressen und E-Mail-Adressen gehören nicht in Repositorybeispiele oder Logs.

Der erste durchgängige persistente Produkt-Smoke ist belegt: Anmeldung,
Garnrolle, Knoten, Faden, Neuladen und API-Neustart erhalten denselben
PostgreSQL-Stand. Nach relevanten Deploys wird dieser Pfad erneut geprüft.
Weitere Produkt-Smokes betreffen repräsentative Datenmengen, Geräteparität,
Edge-Referenzintegrität und zusätzliche Produktbereiche.

## 7. Stoppen und Aufräumen

```bash
just down
```

Volumes, Daten, fremde Compose-Projekte oder Runtime-Secrets werden nicht als
Routine-Aufräumaktion gelöscht. Solche Eingriffe brauchen einen eigenen
Wiederherstellungs- und Autorisierungsbeleg.
