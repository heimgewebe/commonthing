---
id: runbooks.db-recovery
title: DB Recovery Runbook
doc_type: runbook
status: active
summary: >
  Wiederherstellungsablauf für die produktive PostgreSQL-Wahrheit von
  commonThing: logische Backups, isolierter Restore-Proof, age-verschlüsselte
  Off-Host-Kopie, Integritätsprüfung und kontrollierte Promotion.
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

- `weltgewebe-postgres-<UTC>.sql.gz` – Klartext-Dump, bleibt ausschließlich
  auf dem Host;
- `weltgewebe-postgres-<UTC>.sha256.manifest` – Metadaten zum Klartext-Dump;
- `weltgewebe-postgres-<UTC>.sql.gz.age` – age-Chiffrat desselben Dumps, die
  einzige Dump-Form, die den Host verlässt;
- `weltgewebe-postgres-<UTC>.age.sha256.manifest` – Manifest über das
  Chiffrat (`contract=weltgewebe-postgres-backup-age-v1`).

Alle Dateien werden mit Modus `0600` geschrieben. Gzip-Integrität, SHA256,
Größe, Erstellungszeit, Tabellenumfang und – soweit verfügbar – Git-Commit
stehen im Klartext-Manifest. Das Off-Host-Manifest enthält SHA256 und Größe
des Chiffrats, das Format `age-encryption.org/v1`, die öffentlichen
Empfänger sowie Dateiname und SHA256 des zugehörigen Klartext-Dumps.
Unvollständige temporäre Dateien werden nicht sichtbar; das
Off-Host-Manifest wird als letzte Datei eines vollständigen Satzes
veröffentlicht. Die Standard-Retention beträgt 14 Tage und gilt für alle
vier Dateiarten.

Der Dump enthält E-Mail-Adressen und private Kartenanker. Der
Klartext-Dump liegt auf demselben Host wie das Datenbankvolume und erzeugt
dort keine neue Exposition; der wöchentliche Restore-Proof (Abschnitt 3)
arbeitet unverändert auf ihm. Für die Off-Host-Kette wird dagegen nur das
Chiffrat erzeugt und übertragen.

### Verschlüsselung der Off-Host-Kopie (age)

Das Skript verschlüsselt mit `age` ausschließlich an **öffentliche**
X25519-Empfänger (`age1…`). Der private Schlüssel (Identity) wird weder auf
`commonserver` noch auf `heim-pc` benötigt und bleibt offline.

Konfiguration: `BACKUP_AGE_RECIPIENTS_FILE`, in der Systemd-Unit auf
`/etc/weltgewebe/postgres-backup-age-recipients.txt` gesetzt (gleicher
Default im Skript). Pro Zeile steht ein `age1…`-Empfänger; Leerzeilen und
`#`-Kommentare sind erlaubt. Mehrere Empfänger (z. B. ein zweiter
Notfallschlüssel) sind möglich.

Verhalten: **fail-closed.** Fehlt `age`, fehlt die Empfängerdatei, ist sie
leer, enthält sie einen anderen Eintrag als einen `age1…`-Empfänger oder
versehentlich einen privaten Schlüssel (`AGE-SECRET-KEY-…`), bricht das
Skript vor dem Dump ab und schreibt keine Datei. Begründung: Das Skript prüft
alle Pflichtvoraussetzungen (Werkzeuge, Verbindung, Schreibrecht, Health,
Pflichttabellen) vor dem ersten Artefakt und veröffentlicht nur vollständige
Sätze; ein unverschlüsselter Teilstand ohne Chiffrat würde diesen Vertrag
aufweichen. Ein Abschalten der Verschlüsselung ist nicht vorgesehen. Der
Ausfall ist über den fehlgeschlagenen Systemd-Dienst sichtbar.

Deployment-Voraussetzungen auf `commonserver` (vor dem ersten Lauf dieses
Skriptstands, sonst schlägt der tägliche Backupdienst fehl):

1. `age` installieren, z. B. `apt-get install age` (stellt `/usr/bin/age`
   bereit).
2. Den öffentlichen Empfänger ablegen:

   ```bash
   install -d -o root -g root -m 0755 /etc/weltgewebe
   printf '%s\n' 'age1…' > /etc/weltgewebe/postgres-backup-age-recipients.txt
   chmod 0644 /etc/weltgewebe/postgres-backup-age-recipients.txt
   ```

3. Die aktualisierte `weltgewebe-postgres-backup.service` (mit
   `Environment=BACKUP_AGE_RECIPIENTS_FILE=…`) installieren und
   `systemctl daemon-reload` ausführen.

Schlüsselerzeugung – ausschließlich auf einem Offline-Gerät, niemals auf
`commonserver` oder `heim-pc`:

```bash
umask 077
age-keygen -o weltgewebe-backup-identity.txt
age-keygen -y weltgewebe-backup-identity.txt   # öffentlicher Empfänger age1…
```

Nur die mit `age-keygen -y` ausgegebene `age1…`-Zeile gehört in die
Empfängerdatei. Die Identity-Datei wird offline (z. B. auf zwei getrennten
verschlüsselten Datenträgern) aufbewahrt; ihr Verlust macht alle Off-Host-
Kopien unlesbar.

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
`heim-pc` täglich über SSH das neueste **Chiffrat** (`*.sql.gz.age`), sein
Off-Host-Manifest und das Klartext-Metadatenmanifest (`*.sha256.manifest`,
enthält nur Dateiname, SHA256, Größe, Zeit, Tabellenliste und Commit – keine
Dumpinhalte) ab:

```text
scripts/ops/pull-production-postgres-backup.sh
```

Ziel:

```text
~/merges/weltgewebe-production-backups
```

Der Pull läuft fail-closed. Er wählt ausschließlich `*.sql.gz.age`-Dateien
aus und fordert den Klartext-Dump nie über SSH an; findet er kein Chiffrat
(z. B. weil auf dem Host kein Empfänger konfiguriert ist), bricht er ab. Er
prüft Dateinamen, Vertrag, SHA256 und Größe des Chiffrats gegen das
Off-Host-Manifest, verlangt den age-Header `age-encryption.org/v1` und prüft,
dass beide Manifeste denselben Klartext-SHA256 nennen. `age` und der private
Schlüssel werden auf `heim-pc` dafür nicht benötigt. Erst danach schreibt er
eine `latest-pull.receipt` (`contract=weltgewebe-offhost-backup-pull-v2`,
`encryption=age`). Ein vorhandener Wochen-Restore-Proof wird
mitgenommen. Der bestehende Restic-Lauf auf `heim-pc` sichert `~/merges`
anschließend in das entfernte Restic-Repository und liest einen Sentinel aus
dem exakten Snapshot zurück.

Systemd auf `heim-pc` wird nicht direkt aus einem beweglichen Checkout
kopiert. Vor jeder (Re-)Aktivierung muss `commonserver` auf `heim-pc` als
kanonisches SSH-Ziel funktionieren; damit kann die Hostumbenennung den täglichen
Off-Host-Pull nicht still unterbrechen:

```bash
set -euo pipefail
backup_dir=/var/backups/weltgewebe/postgres
getent hosts commonserver > /dev/null
latest="$(
  ssh -o BatchMode=yes -o ConnectTimeout=5 commonserver \
    "sudo -n find '$backup_dir' -maxdepth 1 -type f -name 'weltgewebe-postgres-*.sql.gz.age' -printf '%f\\n'" |
    sort |
    tail -n1
)"
if [[ "$latest" != weltgewebe-postgres-*.sql.gz.age ]]; then
  printf 'Kein verschlüsseltes PostgreSQL-Backup auf commonserver gefunden.\n' >&2
  exit 1
fi
age_manifest="${latest%.sql.gz.age}.age.sha256.manifest"
ssh -o BatchMode=yes -o ConnectTimeout=5 commonserver \
  "sudo -n cat -- '$backup_dir/$age_manifest'" > /dev/null
```

Damit werden genau die verpflichtenden privilegierten Leseoperationen
(`find` und `cat`) geprüft, die auch der Pull verwendet. Befehlsbezogene
Sudoers-Regeln auf `commonserver` müssen dafür das neue `find`-Muster
`weltgewebe-postgres-*.sql.gz.age` und `cat` auf `*.sql.gz.age`,
`*.age.sha256.manifest` und `*.sha256.manifest` erlauben; `cat` auf den
Klartext-Dump `*.sql.gz` wird vom Pull nicht mehr benötigt und sollte aus
den Regeln entfernt werden. `set -euo pipefail` sorgt zusätzlich dafür, dass ein fehlgeschlagenes SSH/`sudo find` nicht durch
das nachgeschaltete `sort`/`tail` verdeckt wird; ein leerer oder unerwarteter
Dateiname bricht explizit ab. Ein allgemeines `sudo -n true` reicht bei
befehlsbezogenen Sudoers-Regeln nicht aus. Der Readback vom 2026-09-10 hat
DNS/SSH sowie die damalige `find`-/`cat`-Prüfung auf `*.sql.gz` erfolgreich
bestanden; für das age-Muster liegt noch kein Runtime-Readback vor.
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

### Altbestand unverschlüsselter Off-Host-Kopien

Vor dem age-Vertrag hat der Pull Klartext-Dumps (`weltgewebe-postgres-*.sql.gz`)
nach `~/merges/weltgewebe-production-backups` kopiert. Die lokale Retention
löscht sie zwar nach `LOCAL_RETENTION_DAYS` (Default 30 Tage), sie müssen aber
nach der Umstellung **manuell sofort gelöscht** werden:

```bash
find ~/merges/weltgewebe-production-backups -maxdepth 1 -type f \
  -name 'weltgewebe-postgres-*.sql.gz' -print -delete
```

Diese Klartext-Kopien sind zusätzlich in älteren Restic-Snapshots von
`~/merges` enthalten. Sie verschwinden dort erst, wenn die betroffenen
Snapshots bewusst per `restic forget`/`restic prune` entfernt werden; diese
Entscheidung (Verlust älterer Wiederherstellungspunkte gegen Entfernung der
Klartextdaten) ist manuell zu treffen und zu dokumentieren.

### Wiederherstellung aus der Off-Host-Kopie

Nur auf einem vertrauenswürdigen Gerät mit Zugriff auf die Offline-Identity,
in einem Verzeichnis mit Modus `0700`:

```bash
set -euo pipefail
umask 077
src=~/merges/weltgewebe-production-backups
base=weltgewebe-postgres-<UTC>
work="$(mktemp -d)"
age -d -i /pfad/zur/weltgewebe-backup-identity.txt \
  -o "$work/$base.sql.gz" "$src/$base.sql.gz.age"
cp "$src/$base.sha256.manifest" "$work/"
expected="$(awk -F= '$1=="sha256" {print $2}' "$work/$base.sha256.manifest")"
[[ "$(sha256sum "$work/$base.sql.gz" | awk '{print $1}')" == "$expected" ]]
gzip -t "$work/$base.sql.gz"
```

Danach kann `scripts/ops/postgres-restore-proof.sh` mit
`BACKUP_FILE="$work/$base.sql.gz"` gegen eine Wegwerfdatenbank laufen
(Abschnitt 3); anschließend gilt Abschnitt 5. Der entschlüsselte Dump ist
Klartext mit personenbezogenen Daten und wird nach Gebrauch gelöscht.

## 5. Vorfall und Wiederherstellung

1. Vorfall und betroffenen Commit/Image-Stand sichern.
2. Schreibzugriffe stoppen; keine Volumes löschen und kein `just down` als
   Routine verwenden.
3. Backup, Manifest und möglichst zugehörigen Restore-Proof auswählen; eine
   Off-Host-Kopie zuvor nach Abschnitt 4 offline entschlüsseln.
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
- neuesten Dump, Chiffrat und beide Manifeste;
- erfolgreichen Restore-Proof;
- Off-Host-Pull-Receipt;
- Snapshotbeleg des bestehenden Restic-Laufs;
- Datum, Commit/Image und offene Lücken.
