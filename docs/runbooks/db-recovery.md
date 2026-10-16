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

Der Pull läuft fail-closed. Er wählt den neuesten vollständigen Satz über das
Off-Host-Manifest (`*.age.sha256.manifest`), das `postgres-backup.sh` als
letzte Datei veröffentlicht; ein abgebrochener Lauf mit Chiffrat ohne dieses
Manifest wird übergangen. Übertragen wird nur das zugehörige
`*.sql.gz.age`, der Klartext-Dump wird nie über SSH angefordert. Findet er
keinen vollständigen Satz (z. B. weil auf dem Host kein Empfänger
konfiguriert ist), bricht er ab. Er
prüft Dateinamen, Vertrag, SHA256 und Größe des Chiffrats gegen das
Off-Host-Manifest, verlangt den age-Header `age-encryption.org/v1` und prüft,
dass beide Manifeste denselben Klartext-SHA256 nennen. Jeder im Off-Host-Manifest
genannte Empfänger (`recipients=`) muss in der auf `heim-pc` unabhängig
gepinnten Datei `PULL_AGE_RECIPIENTS_FILE` stehen (Standard
`~/.config/weltgewebe/postgres-backup-age-recipients.txt`, ein öffentlicher
`age1…`-Schlüssel pro Zeile, Inhalt = öffentlicher Schlüssel der offline
verwahrten Identity). Fehlt die Datei oder nennt das Manifest einen anderen
Empfänger, bricht der Pull ab. Das fängt einen vertauschten oder falsch
konfigurierten Empfänger auf `commonserver` ab, nicht einen kompromittierten
Producer: X25519-Stanzas verraten den Empfänger nicht, die Prüfung vertraut
also der Manifestangabe. `age` und der private
Schlüssel werden auf `heim-pc` dafür nicht benötigt. Erst danach schreibt er
pro Pull einen unveränderlichen Receipt unter `receipts/` (create-only,
`<backup>.pulled-<UTC>.receipt`) und aktualisiert `latest-pull.receipt` als
Zeiger auf den neuesten (`contract=weltgewebe-offhost-backup-pull-v2`,
`encryption=age`, `recipients=`). Receipts unterliegen keiner Retention. Ein vorhandener Wochen-Restore-Proof wird
mitgenommen. **Restic-Abdeckung ist separat zu prüfen:** Der am
15. Oktober 2026 tatsächlich gelesene `restic-backup-1930.service` startet
`heim-pc-restic-backup` mit den Quellen `~/repos`,
`~/artifacts/merges` und `~/.local/share/schauwerk/fundus` sowie einem
Sentinel. `~/merges` ist nach `realpath` ein anderer Pfad als
`~/artifacts/merges` und in diesem Skript **nicht direkt** als Quelle
aufgeführt. Das allein beweist weder eine vorhandene Off-Host-Sicherung
dieser PostgreSQL-Dumps noch, dass andere Sicherungspfade sie ausschließen.
Vor einer age-only-Aussage tatsächliche Backup-Scheduler, Quellen,
Ausschlüsse und relevante Restic-Snapshotpfade live feststellen.

Systemd auf `heim-pc` wird nicht direkt aus einem beweglichen Checkout
kopiert. Vor jeder (Re-)Aktivierung muss `commonserver` auf `heim-pc` als
kanonisches SSH-Ziel funktionieren; damit kann die Hostumbenennung den täglichen
Off-Host-Pull nicht still unterbrechen:

```bash
set -euo pipefail
backup_dir=/var/backups/weltgewebe/postgres
getent hosts commonserver > /dev/null
age_manifest="$(
  ssh -o BatchMode=yes -o ConnectTimeout=5 commonserver \
    "sudo -n find '$backup_dir' -maxdepth 1 -type f -name 'weltgewebe-postgres-*.age.sha256.manifest' -printf '%f\\n'" |
    sort |
    tail -n1
)"
if [[ "$age_manifest" != weltgewebe-postgres-*.age.sha256.manifest ]]; then
  printf 'Kein vollständiges verschlüsseltes PostgreSQL-Backup auf commonserver gefunden.\n' >&2
  exit 1
fi
[[ "$age_manifest" =~ ^[A-Za-z0-9._-]+$ && "$age_manifest" != *'..'* ]] || {
  printf 'Unsafe remote backup filename; preflight stopped.\n' >&2
  exit 1
}
base="${age_manifest%.age.sha256.manifest}"
for file in "$age_manifest" "$base.sql.gz.age" "$base.sha256.manifest"; do
  ssh -o BatchMode=yes -o ConnectTimeout=5 commonserver \
    "sudo -n cat -- '$backup_dir/$file'" > /dev/null
done
# Der Wochen-Proof ist optional und fehlt oft für das neueste Backup. Daher
# nur die Sudo-Berechtigung prüfen (`sudo -l`), nicht die Existenz der Datei.
proof="$backup_dir/proofs/$base.restore-proof"
ssh -o BatchMode=yes -o ConnectTimeout=5 commonserver \
  "sudo -n -l test -f '$proof' && sudo -n -l cat -- '$proof'" > /dev/null
# Jeder Empfänger des neuesten Backups muss auf heim-pc gepinnt sein.
# Gleiche Regeln wie im Pull: genau ein Feld, kanonische Liste, keine Doppelten.
# Die Pin-Datei wird wie im Pull geparst: lesbar, kein privater Schlüssel,
# nur kanonische age1-Zeilen (getrimmt, `#`-Kommentare erlaubt), nicht leer.
pinned="$HOME/.config/weltgewebe/postgres-backup-age-recipients.txt"
[[ -f "$pinned" && -r "$pinned" ]] || {
  printf 'Pin-Datei fehlt oder ist nicht lesbar: %s\n' "$pinned" >&2
  exit 1
}
if grep -q 'AGE-SECRET-KEY-' "$pinned"; then
  printf 'Pin-Datei enthält eine private age-Identität: %s\n' "$pinned" >&2
  exit 1
fi
declare -A pinned_set=()
while IFS= read -r line || [[ -n "$line" ]]; do
  line="${line#"${line%%[![:space:]]*}"}"
  line="${line%"${line##*[![:space:]]}"}"
  [[ -z "$line" || "$line" == '#'* ]] && continue
  [[ "$line" =~ ^age1[02-9ac-hj-np-z]{58}$ ]] || {
    printf 'Pin-Datei enthält einen ungültigen Eintrag: %s\n' "$pinned" >&2
    exit 1
  }
  pinned_set["$line"]=1
done < "$pinned"
((${#pinned_set[@]} > 0)) || {
  printf 'Pin-Datei enthält keinen age1-Empfänger: %s\n' "$pinned" >&2
  exit 1
}
age_manifest_text="$(
  ssh -o BatchMode=yes -o ConnectTimeout=5 commonserver \
    "sudo -n cat -- '$backup_dir/$age_manifest'"
)"
[[ "$(grep -c '^recipients=' <<< "$age_manifest_text")" == 1 ]] || {
  printf 'Off-Host-Manifest muss recipients= genau einmal enthalten.\n' >&2
  exit 1
}
recipients="$(awk -F= '$1 == "recipients" {print substr($0, 12)}' <<< "$age_manifest_text")"
recipient_list_re='^age1[02-9ac-hj-np-z]{58}(,age1[02-9ac-hj-np-z]{58})*$'
[[ "$recipients" =~ $recipient_list_re ]] || {
  printf 'Empfängerliste im Off-Host-Manifest ist nicht kanonisch: %s\n' "$recipients" >&2
  exit 1
}
IFS=, read -r -a backup_recipients <<< "$recipients"
if [[ -n "$(printf '%s\n' "${backup_recipients[@]}" | sort | uniq -d)" ]]; then
  printf 'Off-Host-Manifest nennt einen Empfänger doppelt.\n' >&2
  exit 1
fi
for recipient in "${backup_recipients[@]}"; do
  [[ -n "${pinned_set[$recipient]:-}" ]] || {
    printf 'Empfänger nicht in %s gepinnt: %s\n' "$pinned" "$recipient" >&2
    exit 1
  }
done
```

Damit werden genau die privilegierten Operationen geprüft, die auch der Pull
verwendet: `find` auf die Abschlussmarker, `cat` auf Abschlussmarker,
Chiffrat und Klartext-Manifest sowie `test -f` und `cat` auf den optionalen
Restore-Proof unter `proofs/`. Der Pull selbst prüft dieselbe Berechtigung per
`sudo -n -l` und bricht ab, wenn sie fehlt oder die SSH-Verbindung bei der
Proof-Abfrage scheitert; nur ein eindeutig fehlender Proof wird übergangen.
Der Preflight findet eine fehlende Regel aber schon vor der Aktivierung. Befehlsbezogene
**Sicherheitssperre für den aktuellen Entwurf:** Sudoers-Globs wie
`*.sha256.manifest` sind **keine** enge Argumentautorisierung. In
Sudoers können Platzhalter auch Leerzeichen, zusätzliche Argumente und
Pfadkomponenten erfassen. Daher dürfen die unten beschriebenen direkten
`sudo find`-, `sudo cat`- und `sudo test`-Aufrufe **noch nicht**
als produktionssichere Freigabe konfiguriert werden. Vor einem Rollout ist
ein root-eigener, nicht durch den Pull-Account modifizierbarer Lese-Wrapper
mit strikt geprüftem Basename, no-follow-Dateizugriff und negativ getesteter
Sudoers-Argumentgrenze notwendig. Anschließend müssen Pullskript und
produktiver Preflight dieselbe Schnittstelle verwenden.

Ein allgemeines `sudo -n true` reicht bei
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

Der reguläre Pull (`PULL_MODE=normal`, Default) bleibt gesperrt, solange
Altbestände im Ziel liegen. Die explizite Erstübertragung
`PULL_MODE=migration-stage` darf dagegen ein neues **Chiffrat** in einem
separaten Verzeichnis bereitstellen. Dieser Modus:

- benötigt ein explizites `MIGRATION_STAGE_DIR` außerhalb von
  `~/merges`, `~/artifacts/merges`, `~/repos`, dem Schauwerk-Fundus und
  des regulären `DEST_DIR`;
- verwendet nur den age-verschlüsselten Dump, beide Metadatenmanifeste und
  einen optionalen Wochen-Proof;
- verweigert Überschreiben bereits vorhandener Staging-Artefakte;
- verändert weder vorhandene Klartext-Restore-Punkte noch den normalen
  `latest-pull.receipt` und erzeugt **keinen neuen Erfolgs-Receipt**.

Das Staging ist **kein Nachweis der Entschlüsselbarkeit** oder des richtigen
Empfängers. Vor einer Migration muss die tatsächliche Restic-Quellliste
bestätigt und das Staging-Verzeichnis auf einem passenden Datenträger mit
Modus `0700` außerhalb dieser Quellen eingerichtet werden:

```bash
umask 077
# STAGE vorher auf einem nicht durch Restic erfassten Pfad festlegen.
install -d -m 0700 "$STAGE"
PULL_MODE=migration-stage MIGRATION_STAGE_DIR="$STAGE" \
  scripts/ops/pull-production-postgres-backup.sh
```

Die age-Datei und Manifeste danach auf ein vertrauenswürdiges **Offlinegerät**
übertragen. Dort mit der unabhängig verwahrten Identity entschlüsseln, den
SHA-256 des entschlüsselten Gzip-Dumps mit dem Klartextmanifest vergleichen,
`gzip -t` ausführen und einen echten Restore in eine isolierte
Wegwerfdatenbank durchführen. Den Empfänger unabhängig gegen die ursprünglich
beabsichtigte öffentliche age-Identität abgleichen. Nur nach protokolliertem
Recovery-PASS dürfen die alten Off-Host-Klartextkopien separat zur Entfernung
freigegeben werden; deren Löschung ist **nicht** Teil dieses Skripts.

**Verbleibende technische Sperre:** Das Staging verhindert neue Klartext-Pulls,
unterbindet aber noch nicht automatisch laufende Restic-Snapshots von
vorhandenen `*.sql.gz`-Altbeständen. Vor Produktivfreigabe ist eine
reproduzierbar überprüfte Restic-Einschluss-/Ausschlussregel beziehungsweise
ein fail-closed Snapshot-Gate mit negativem Test verpflichtend. Eine bloß
dokumentierte Warnung oder deaktivierter Backup-Timer gilt nicht als Ersatz.


Vor dem age-Vertrag hat der Pull Klartext-Dumps (`weltgewebe-postgres-*.sql.gz`)
nach `~/merges/weltgewebe-production-backups` kopiert. Ein neuer age-Pull
verweigert einen Erfolgs-Receipt, solange irgendeine `*.sql.gz`-Datei im Ziel
liegt, unabhängig vom aktuell gesetzten `BACKUP_PREFIX` und rekursiv, also auch
in liegengebliebenen `.pull.*`-Arbeitsverzeichnissen abgebrochener Pulls;
er löscht Wiederherstellungspunkte nicht selbst. Vor der Freigabe von Restic
und des verschlüsselten Pulls: Eine neue age-Kopie mit der **offline
verwahrten Identity** in einer isolierten Umgebung tatsächlich entschlüsseln,
Klartext-SHA256 und gzip-Integrität sowie Restore-Proof prüfen. Erst nach
diesem Nachweis den Altbestand erfassen und eine ausdrückliche dokumentierte
Löschentscheidung treffen. Die folgende Inventur verändert keine Daten:

```bash
find ~/merges/weltgewebe-production-backups \( -type f -o -type l \) \
  -name '*.sql.gz' -print
```

Die tatsächliche Bereinigung ist ein separater dokumentierter Operator-
Eingriff nach Beleg des Ersatz-Restore-Punktes; kein blindes `find -delete`.
Vor Aktivierung dieses Backup-Skriptstands auf commonserver müssen `age`,
lesbare öffentliche Empfänger und die erfolgreiche Empfängervalidierung im
tatsächlichen Systemd-Kontext nachgewiesen sein, andernfalls kein Deploy.
Zuerst Client und Sudoers ohne Klartext-Pull auf age konvergieren, dann erst
Producer und neue Backup-Unit aktivieren. Bis zum offline verifizierten
Chiffrat, gelöschten lokalen Altbeständen und bewerteter Restic-Historie
keinen weiteren Restic-Snapshot aus diesem Backup-Ziel als sichere age-only
Kopie ausgeben. Das GREEN isolierter Skripttests ist keine Betriebsfreigabe.

Alte Klartext-Kopien können zusätzlich in früheren Restic-Snapshots von
`~/merges` enthalten sein. Existenz und Umfang sind durch echte
Snapshot-Auflistung und Dateipfadprüfung zu belegen, nicht aus Timern abzuleiten. Sie verschwinden dort erst, wenn die betroffenen
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
# Der entschlüsselte Dump wird bei jedem Ende dieser Shell entfernt, auch nach
# einem Fehler in age, SHA-Vergleich, gzip -t oder Restore und nach Abbruch.
trap 'rm -rf -- "$work"' EXIT
trap 'exit 130' INT TERM HUP
age -d -i /pfad/zur/weltgewebe-backup-identity.txt \
  -o "$work/$base.sql.gz" "$src/$base.sql.gz.age"
cp "$src/$base.sha256.manifest" "$work/"
expected="$(awk -F= '$1=="sha256" {print $2}' "$work/$base.sha256.manifest")"
[[ "$(sha256sum "$work/$base.sql.gz" | awk '{print $1}')" == "$expected" ]]
gzip -t "$work/$base.sql.gz"
```

In **derselben** Shell kann danach `scripts/ops/postgres-restore-proof.sh` mit
`BACKUP_FILE="$work/$base.sql.gz"` gegen eine Wegwerfdatenbank laufen
(Abschnitt 3); anschließend gilt Abschnitt 5. Der entschlüsselte Dump ist
Klartext mit personenbezogenen Daten. Mit dem Ende der Shell ist er gelöscht.
Verlangt die Vorfallrichtlinie, ihn länger aufzubewahren, ist das eine
ausdrückliche Entscheidung: vor dem Ende `trap - EXIT` setzen, Ablageort,
Zweck und Löschtermin im Vorfallprotokoll festhalten.

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
