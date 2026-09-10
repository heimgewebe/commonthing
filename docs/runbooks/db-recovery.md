---
id: runbooks.db-recovery
title: DB Recovery Runbook
doc_type: runbook
status: active
summary: >
  Wiederherstellungsablauf für die produktive PostgreSQL-Wahrheit von
  Weltgewebe: logische Backups, isolierter Restore-Proof, Off-Host-Kopie,
  Integritätsprüfung und kontrollierte Promotion.
relations:
  - type: relates_to
    target: docs/runbooks/README.md
  - type: relates_to
    target: docs/runbook.md
  - type: relates_to
    target: docs/runbooks/incident-response.md
  - type: relates_to
    target: docs/adr/ADR-0007__auth-persistence-production-db-path.md
  - type: relates_to
    target: docs/datenmodell.md
  - type: relates_to
    target: scripts/ops/postgres-backup.sh
  - type: relates_to
    target: scripts/ops/postgres-restore-proof.sh
  - type: relates_to
    target: scripts/ops/postgres-restore-latest-proof.sh
  - type: relates_to
    target: scripts/ops/pull-production-postgres-backup.sh
---
# DB Recovery Runbook

Dieses Runbook beschreibt nur den implementierten Vertrag. Es behauptet weder
WAL/PITR noch einen aktuellen Timer- oder Backupzustand ohne frischen
Runtimebeleg.

## 1. Datenwahrheit

Im Produktionspfad liegt die primäre Wahrheit in PostgreSQL:

- `domain_accounts` – Accounts/Garnrollen;
- `domain_nodes` – Knoten;
- `domain_edges` – Fäden;
- `sessions` – Browsersitzungen;
- `passkey_credentials` – Passkeys;
- `_sqlx_migrations` – Migrationsverlauf.

JSONL ist nur lokaler, historischer, Import-/Export- oder ausdrücklich
freigegebener Rückfallpfad. Ein JSONL-Export ersetzt kein aktuelles
PostgreSQL-Backup.

## 2. Automatischer Backupvertrag auf `commonserver`

Kanonisches Skript:

```text
scripts/ops/postgres-backup.sh
```

Die produktive Systemd-Unit verwendet den laufenden Container
`weltgewebe-db-1`. Dadurch muss kein zusätzliches Datenbankpasswort in einer
zweiten Datei gepflegt werden.

Der Vertrag prüft vor dem Dump:

- Erreichbarkeit mit `select 1`;
- Vorhandensein aller sechs Pflichtstrukturen;
- konkrete Container- oder Datenbankverbindung.

Danach entstehen unter `/var/backups/weltgewebe/postgres`:

- `weltgewebe-postgres-<UTC>.sql.gz`;
- `weltgewebe-postgres-<UTC>.sha256.manifest`.

Beide Dateien werden mit Modus `0600` geschrieben. Gzip-Integrität, SHA256,
Größe, Erstellungszeit, Tabellenumfang und – soweit verfügbar – Git-Commit
stehen im Manifest. Unvollständige temporäre Dateien werden nicht sichtbar.
Die Standard-Retention beträgt 14 Tage.

Systemd:

Eine getrennte, kurzlebige Vorbereitungs-Unit legt zunächst
`/var/backups/weltgewebe` mit Modus `0750` und das engere PostgreSQL-Ziel mit
Modus `0700` an. Sie führt ausschließlich feste `/usr/bin/install`-Befehle aus,
hat eine begrenzte Capability-Menge und darf unter `/var` nur in
`/var/backups` schreiben. Der eigentliche Dumpdienst startet erst danach und
besitzt ausschließlich Schreibzugriff auf
`/var/backups/weltgewebe/postgres`. Dadurch bleibt die Erststartfähigkeit
erhalten, ohne den umfassenderen Schreibbereich während des Backupskripts zu
öffnen.

- `weltgewebe-postgres-backup-prepare.service`;
- `weltgewebe-postgres-backup.service`;
- `weltgewebe-postgres-backup.timer` – täglich gegen 02:15 Uhr mit begrenzter
  Zufallsverzögerung.

## 3. Isolierter Restore-Proof

Der Wochenlauf

```text
scripts/ops/postgres-restore-latest-proof.sh
```

startet einen Wegwerfcontainer mit:

- dem digestgebundenen PostgreSQL-16-Image;
- `--network none`;
- flüchtigem `tmpfs`-Datenverzeichnis;
- eindeutigem Restore-/Proof-Namen.

`postgres-restore-proof.sh` prüft Manifest und Gzip, verlangt ein leeres Ziel,
spielt den Dump ein und verifiziert danach alle Pflichtstrukturen. Das
Proof-Artefakt endet nur bei Erfolg mit `result=ok` und liegt mit Modus `0600`
unter dem Runtime-Default `/var/backups/weltgewebe/postgres/proofs/` (über `RESTORE_PROOF_DIR` konfigurierbar). Der Wegwerfcontainer wird in jedem Ausgang entfernt.

Systemd:

- `weltgewebe-postgres-restore-proof.service`;
- `weltgewebe-postgres-restore-proof.timer` – wöchentlich sonntags gegen
  03:15 Uhr.

Ein manueller Restore in eine externe Wegwerfdatenbank bleibt möglich. Dabei
muss `RESTORE_DATABASE_URL` sichtbar auf `restore`, `proof`, `tmp` oder `test`
zeigen und darf niemals der produktiven `DATABASE_URL` entsprechen.

## 4. Off-Host-Kette

Ein lokaler Dump auf dem VPS schützt nicht vor Hostverlust. Deshalb holt
`heim-pc` den neuesten Dump und das Manifest täglich über SSH ab:

```text
scripts/ops/pull-production-postgres-backup.sh
```

Ziel:

```text
~/merges/weltgewebe-production-backups
```

Der Pull läuft fail-closed, validiert den Dateinamen und den SHA256-Wert und
schreibt eine `latest-pull.receipt`. Ein vorhandener Wochen-Restore-Proof wird
mitgenommen. Der bestehende Restic-Lauf auf `heim-pc` sichert `~/merges`
anschließend in das entfernte Restic-Repository und liest einen Sentinel aus
dem exakten Snapshot zurück.

Systemd auf `heim-pc` wird nicht direkt aus einem beweglichen Checkout
kopiert. Vor jeder (Re-)Aktivierung muss `commonserver` auf `heim-pc` als
kanonisches SSH-Ziel funktionieren; damit kann die Hostumbenennung den täglichen
Off-Host-Pull nicht still unterbrechen:

```bash
backup_dir=/var/backups/weltgewebe/postgres
getent hosts commonserver
latest="$(ssh -o BatchMode=yes -o ConnectTimeout=5 commonserver \
  "sudo -n find '$backup_dir' -maxdepth 1 -type f -name 'weltgewebe-postgres-*.sql.gz' -printf '%f\\n' | sort | tail -n1")"
[[ "$latest" == weltgewebe-postgres-*.sql.gz ]]
manifest="${latest%.sql.gz}.sha256.manifest"
ssh -o BatchMode=yes -o ConnectTimeout=5 commonserver \
  "sudo -n cat -- '$backup_dir/$manifest'" > /dev/null
```

Damit werden genau die beiden verpflichtenden privilegierten Leseoperationen
(`find` und `cat`) geprüft, die auch der Pull verwendet; ein allgemeines
`sudo -n true` reicht bei befehlsbezogenen Sudoers-Regeln nicht aus. Der
Readback vom 2026-09-10 hat DNS/SSH sowie diese `find`-/`cat`-Prüfung
erfolgreich bestanden.
`REMOTE_HOST` bleibt als expliziter Kompatibilitäts-Override erhalten; der
Default und neue Installationen verwenden `commonserver`.

Anschließend installiert der folgende Befehl das Pullskript unter einem
unveränderlichen, vollständigen Git-Commit, erzeugt daraus die User-Unit, legt
den schreibbaren Zielpfad vor dem Sandboxstart an und aktiviert den Timer:

```bash
EXPECTED_COMMIT="$(git rev-parse HEAD)" \
  scripts/ops/install-postgres-offhost-pull-user-units.sh --enable-now
```

Die Installation schreibt zusätzlich ein zeitgestempeltes, SHA256- und
commitgebundenes `install.receipt`. Die eingecheckte Service-Datei ist eine Vorlage; ihr
`@WELTGEWEBE_COMMIT@`-Platzhalter darf nicht ungeprüft als aktive Unit kopiert
werden. `ProtectKernelModules` wird in der User-Unit bewusst nicht verwendet,
weil diese Kernel-Capability im User-Manager nicht portabel verfügbar ist. Alle
übrigen Sandboxgrenzen bleiben aktiv.

- `weltgewebe-postgres-offhost-pull.service`;
- `weltgewebe-postgres-offhost-pull.timer` – täglich gegen 04:15 Uhr.

Diese Kette ist ein täglicher Off-Host-Vertrag. Sie ist kein RPO-von-fünf-Minuten
und kein Ersatz für WAL/PITR.

## 5. Vorfall und Wiederherstellung

1. Vorfall und betroffenen Commit/Image-Stand sichern.
2. Schreibzugriffe stoppen; keine Volumes löschen und kein `just down` als
   Routine verwenden.
3. Backup, Manifest und möglichst zugehörigen Restore-Proof auswählen.
4. Den Dump zuerst in einer neuen, isolierten PostgreSQL-Instanz prüfen.
5. SHA, Tabellen, Migrationen und repräsentative Datensätze kontrollieren.
6. Erst dann eine neue produktive Datenbank aus dem geprüften Dump aufbauen.
7. API zunächst mit `WELTGEWEBE_API_STARTUP_MIGRATIONS=verify-applied`
   starten, sofern keine Migration ausdrücklich freigegeben wurde.
8. Health, Anmeldung, Sessionpersistenz, Garnrolle, Knoten, Faden und – sofern
   eingerichtet – Passkeypfad prüfen.
9. Promotion erst nach vollständigem Readback; sonst Quarantäne und früheres
   Backup verwenden.

## 6. Minimaler Integritätscheck

```sql
select version, success from _sqlx_migrations order by version;
select count(*) from domain_accounts;
select count(*) from domain_nodes;
select count(*) from domain_edges;
select count(*) from sessions;
select count(*) from passkey_credentials;
```

Zusätzlich müssen bekannte, nicht sensible Stichproben über die API lesbar sein.
Private E-Mail-, Adress-, Token- oder Positionsdaten gehören nicht in Tickets
oder geteilte Testumgebungen.

## 7. Grenzen und Evidenz

Nicht implementiert:

- WAL-Archivierung und Point-in-Time-Recovery;
- Object Lock;
- automatisches Multi-Region-Failover;
- Outbox-/Projektor-Replay als Recoveryquelle.

Eine aktuelle Backupaussage braucht mindestens:

- Timer- und Servicezustand;
- neuesten Dump und Manifest;
- erfolgreichen Restore-Proof;
- Off-Host-Pull-Receipt;
- Snapshotbeleg des bestehenden Restic-Laufs;
- Datum, Commit/Image und offene Lücken.
