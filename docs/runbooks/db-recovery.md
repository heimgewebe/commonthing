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
veröffentlicht. **Der Producer löscht keine dieser vier Dateiarten
automatisch.** Der bisherige Konfigurationswert `BACKUP_RETENTION_DAYS`
(Default 14) bleibt nur kompatibel lesbar und bewirkt keine Aussonderung:
Solange kein echter isolierter Restore eines geeigneten Ersatz-Backups
belegt und eine eigene Freigabe erteilt ist, dürfen auch alte oder
unvollständige Sicherungen nicht allein wegen ihres Alters verschwinden.
Das lässt den benötigten Speicherplatz wachsen; Kapazität und Sicherungsalter
müssen deshalb weiterhin aktiv überwacht werden. Vor `pg_dump` prüft der
Producer deshalb mit `df -Pk` den freien Speicher im Backup-Dateisystem:
`BACKUP_MIN_FREE_KIB` muss eine positive Ganzzahl sein (Default `4194304`,
also 4 GiB). Zusätzlich zur Reserve verlangt die Vorabprüfung Platz für
den nächsten Lauf, geschätzt aus dem größten vorhandenen lokalen
`*.sql.gz`: Faktor 2,5 (Klartext mit 25 % Wachstum plus Chiffrat in gleicher
Größe). Fehlen verlässliche Angaben oder reicht der Platz nicht, bricht
dieser Lauf vor dem Dump ohne neue Sicherungsdateien ab; historische
Recoverypunkte bleiben unverändert. Vor der age-Verschlüsselung prüft der
Producer erneut mit der gemessenen Dumpgröße. Reicht der Platz dann nicht,
bleibt das bereits veröffentlichte lokale Paar (`.sql.gz` plus
`.sha256.manifest`) erhalten, der Lauf endet mit Fehler und es entsteht kein
age-Abschlussmarker. Platz wird nie durch automatisches Löschen geschaffen.
Ein `BACKUP_LABEL` mit Endung `.age` wird abgelehnt, weil sein
Klartext-Manifest sonst wie ein age-Abschlussmarker benannt wäre.

**Grenze:** Diese Vorabprüfung ist weder ein Dateisystem-Quota noch eine
atomare Reservierung. Wachsende Dumps und gleichzeitige andere Schreiber
können den Datenträger weiterhin füllen. Daher Kapazität weiter überwachen
und einen getrennten Backup-Speicherbereich prüfen; die Schwelle darf
nicht als vollständiger ENOSPC-Schutz bewertet werden.

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

Verhalten: **lokale Recovery unabhängig, externe Verschlüsselung
fail-closed.** Der Producer veröffentlicht erst den lokalen `.sql.gz`-Dump
und sein `.sha256.manifest` (Manifest zuletzt). Fehlt danach `age`, fehlt
die öffentliche Empfängerdatei oder enthält diese ungültige beziehungsweise
private Schlüssel, schlägt **die zusätzliche externe Kopie** mit
Nichtnull-Exitstatus fehl. Der bereits fertig geprüfte lokale Dump samt
SHA256-Manifest bleibt erhalten. Weder ein vollständiges `.sql.gz.age`
noch das abschließende `.age.sha256.manifest` werden dann veröffentlicht.
Die fehlgeschlagene Off-Host-Sicherung bleibt im Systemd-Dienst sichtbar und
muss alarmiert und behoben werden; ein lokaler Dump ist **kein** Nachweis
einer Off-Host-Recovery.

Deployment-Voraussetzungen auf `commonserver` für eine **vollständig
verschlüsselte externe** Backupkette (ohne sie läuft nur das lokale Backup):

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
`*.sql.gz.age`, der Klartext-Dump wird nie über SSH angefordert. Jeder
Fernzugriff läuft über den root-eigenen Lese-Wrapper
`/usr/local/libexec/weltgewebe/postgres-backup-read`
(`scripts/ops/postgres-backup-read.sh`, siehe unten); der Pull ruft per
`sudo -n` nichts anderes auf. Findet er
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
`encryption=age`, `recipients=`). Receipts unterliegen keiner Retention.
Der Off-Host-Pull löscht derzeit **keine** bestehenden Sicherungen,
Chiffrate, Manifeste oder Restore-Belege automatisch – auch keine abgelaufenen
oder noch nicht zertifizierten Artefakte. `LOCAL_RETENTION_DAYS` bleibt zur
Kompatibilität mit vorhandenen Installationen als Eingabe akzeptiert, löst
aber ausdrücklich keine Löschung mehr aus. Ein echter Ersatz-Restore mit der
unabhängigen Offline-Identity und eine gesonderte Freigabe sind die
Voraussetzung jeder späteren Aussonderung. Dadurch kann der Speicherbedarf
wachsen; freien Platz und Backup-Alter deshalb unabhängig überwachen.
Neu erzeugte History-Receipts tragen
zusätzlich `history_sequence`, eine pro Sicherungsbasis strikt ansteigende
Veröffentlichungsnummer. Der `latest-pull.receipt`-Zeiger wird anhand dieser
Nummer statt anhand der möglicherweise rückläufigen Systemuhr repariert.
Nach einem Absturz wiedergefundene ältere Proof-Versionen erhalten zusätzlich
`history_backfill=1`: Sie bleiben als geprüfte historische Belege erhalten,
überschreiben aber niemals eine bereits zertifizierte neuere
`latest-pull.receipt`-Version. Alte Receipts ohne dieses Feld bleiben lesbar; bei mehreren alten
unsequenzierten Receipts und verlorenem oder nicht zuordenbarem Latest-Zeiger
bricht die automatische Wahl jedoch sicher ab, statt einen möglicherweise
falschen neuesten Stand zu behaupten. Ein vorhandener Wochen-Restore-Proof wird
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
reader=/usr/local/libexec/weltgewebe/postgres-backup-read
getent hosts commonserver > /dev/null
age_manifest="$(
  ssh -o BatchMode=yes -o ConnectTimeout=5 commonserver \
    "sudo -n '$reader' latest weltgewebe-postgres"
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
for kind in age-manifest ciphertext manifest; do
  ssh -o BatchMode=yes -o ConnectTimeout=5 commonserver \
    "sudo -n '$reader' read '$base' '$kind'" > /dev/null
done
# Der Wochen-Proof ist optional und fehlt oft für das neueste Backup. Daher nur
# einen eindeutigen Zustand verlangen; ein vorhandener Proof muss lesbar sein.
proof_state="$(
  ssh -o BatchMode=yes -o ConnectTimeout=5 commonserver \
    "sudo -n '$reader' proof-state '$base'"
)"
case "$proof_state" in
  absent) ;;
  present)
    ssh -o BatchMode=yes -o ConnectTimeout=5 commonserver \
      "sudo -n '$reader' read '$base' proof" > /dev/null
    ;;
  *)
    printf 'Unerwarteter Proof-Zustand: %s\n' "$proof_state" >&2
    exit 1
    ;;
esac
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
    "sudo -n '$reader' read '$base' age-manifest"
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

Damit wird genau die privilegierte Schnittstelle geprüft, die auch der Pull
verwendet: `latest`, `read` für Off-Host-Manifest, Chiffrat und
Klartext-Manifest sowie `proof-state` und gegebenenfalls `read … proof` für
den optionalen Restore-Proof. Der Pull bricht ab, wenn SSH, sudo oder der
Wrapper bei der Proof-Abfrage scheitern; nur ein eindeutig fehlender Proof
(`absent`) wird übergangen. Der Preflight findet eine fehlende Regel schon vor
der Aktivierung.

**Lese-Wrapper und Sudoers auf `commonserver`:**

Direkte `sudo find`-, `sudo cat`- und `sudo test`-Regeln sind **nicht**
zulässig: Sudoers-Platzhalter wie `*.sha256.manifest` erfassen auch
Leerzeichen, zusätzliche Argumente und Pfadkomponenten und sind deshalb keine
enge Argumentautorisierung. Stattdessen erhält das Pull-Konto genau einen
Befehl, den root-eigenen Wrapper `scripts/ops/postgres-backup-read.sh`. Eine
Sudoers-Regel ohne Argumentliste erlaubt beliebige Argumente; die
Argumentgrenze liegt daher im Wrapper selbst:

- nur `latest PREFIX`, `read BASE KIND` (`ciphertext`, `age-manifest`,
  `manifest`, `proof`) und `proof-state BASE`, jeweils mit exakter
  Argumentanzahl;
- Namen nur `^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$` ohne `..`; das
  Backupverzeichnis ist im Wrapper fest verdrahtet und hängt an jeden Namen
  ein festes Suffix, der Klartext-Dump ist damit nicht adressierbar;
- der Interpreter ist absolut (`#!/bin/bash -p`): weder ein `PATH` des
  Aufrufers (ohne `secure_path`) noch `BASH_ENV` oder exportierte Funktionen
  bestimmen, welcher Code als root läuft;
- Backup- und Proof-Verzeichnis müssen echte, root-eigene Verzeichnisse ohne
  Gruppen-/Weltschreibrecht sein; gelesen werden nur reguläre Dateien, ohne
  Symlinks zu folgen (`dd iflag=nofollow`).

Die negativen Fälle (fremde Operationen, zusätzliche oder fehlende
Argumente, Pfadbestandteile, Klartext-Suffix, Symlinks, FIFOs, unsichere
Verzeichnisrechte, fremder Besitzer) prüft
`scripts/tests/test_offhost_backup_pull_contract.sh`. Installation als root
auf `commonserver` aus einem geprüften Checkout; `PULL_USER` ist das Konto,
mit dem `heim-pc` sich per SSH anmeldet:

```bash
set -euo pipefail
umask 022
PULL_USER="${PULL_USER:?PULL_USER auf das SSH-Konto von heim-pc setzen}"
reader=/usr/local/libexec/weltgewebe/postgres-backup-read
install -d -o root -g root -m 0755 /usr/local/libexec/weltgewebe
install -o root -g root -m 0755 scripts/ops/postgres-backup-read.sh "$reader"
rule="$(mktemp)"
printf '%s ALL=(root) NOPASSWD: %s\n' "$PULL_USER" "$reader" > "$rule"
visudo -cf "$rule"
install -o root -g root -m 0440 "$rule" /etc/sudoers.d/weltgewebe-postgres-backup-read
rm -f -- "$rule"
visudo -c
```

Alte Regeln für `find`, `cat` oder `test` auf das Backupverzeichnis danach
entfernen. Erst ein Runtime-Readback schließt diese Sperre; isolierte
Skripttests ersetzen ihn nicht. Von `heim-pc` aus:

```bash
set -euo pipefail
reader=/usr/local/libexec/weltgewebe/postgres-backup-read
ssh commonserver 'sudo -n -l' # nur der Wrapper, keine find/cat/test-Regel
for denied in \
  "sudo -n /usr/bin/cat /etc/shadow" \
  "sudo -n /usr/bin/find /var/backups/weltgewebe/postgres" \
  "sudo -n '$reader' read '../../../etc/shadow' manifest" \
  "sudo -n '$reader' read 'weltgewebe-postgres-x' plaintext" \
  "sudo -n '$reader' cat /var/backups/weltgewebe/postgres"; do
  if ssh -o BatchMode=yes commonserver "$denied" > /dev/null 2>&1; then
    printf 'Unerwartet erlaubt: %s\n' "$denied" >&2
    exit 1
  fi
done
```

Ergebnis von `sudo -n -l`, Schleifenlauf und Datum als Beleg festhalten.
`REMOTE_HOST` bleibt als expliziter Kompatibilitäts-Override erhalten; der
Default und neue Installationen verwenden `commonserver`. `REMOTE_BACKUP_DIR`
wird nicht mehr unterstützt und lässt den Pull abbrechen, weil der Wrapper das
Verzeichnis festlegt.

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

**Restic-Snapshot-Gate:** Das Staging verhindert neue Klartext-Pulls, aber
nicht, dass ein laufender Restic-Snapshot vorhandene `*.sql.gz`-Altbestände
erneut erfasst. Dafür gibt es `scripts/ops/restic-plaintext-gate.sh`. Es
bekommt die Snapshot-Quellen als absolute Pfade und bricht ab, sobald unter
einer Quelle eine `*.sql.gz`-Datei oder ein so benannter Symlink liegt, auch
über eine symlinkte Quellwurzel, sowie wenn eine Quelle fehlt oder nicht
vollständig lesbar ist. Es liest nur und löscht nichts. Die negativen Fälle
prüft `scripts/tests/test_offhost_backup_pull_contract.sh`. Der
Off-Host-Installer legt das Gate commitgebunden neben das Pullskript
(`~/.local/libexec/weltgewebe/<commit>/restic-plaintext-gate.sh`, Hash im
`install.receipt`).

**Verbindliche Grenze des Preflight-Gates:** Der Dateiscan endet **vor**
dem eigentlichen Restic-Snapshot. Entsteht danach ein neuer
`*.sql.gz`-Dump, kann Restic ihn trotz erfolgreichem Gate erfassen.
Der Scan allein ist daher **kein** vollständiger Datenschutzschutz. Der
maßgebliche Schutz ist eine Ausschlussregel, die **Restic selbst beim
Snapshot** anwendet. Auf `heim-pc` verwendet der live geprüfte Aufruf
`heim-pc-restic-backup run` die Datei
`$HOME/.config/restic/heim-pc-excludes` über `--exclude-file`. Diese
Datei darf **nicht** die ungeankerte Zeile `**/*.sql.gz` enthalten:
Der tatsächlich laufende Scope-Validator in `heim-pc-restic-backup`
weist diese als außerhalb der genehmigten Snapshotquellen zurück; der
Restic-Dienst würde fehlschlagen. Stattdessen sind nach gesonderter
Betriebsfreigabe diese **drei absolut verankerten** Muster für die
aktuell beobachteten Heim-PC-Quellen vorgesehen:

```text
/home/alex/repos/**/*.sql.gz
/home/alex/artifacts/merges/**/*.sql.gz
/home/alex/.local/share/schauwerk/fundus/**/*.sql.gz
```

**Installationsreihenfolge ist verbindlich:** Die beiden ersten
Muster werden vom aktuellen Wrapper akzeptiert, das Fundus-Muster
jedoch erst nach Integration, unabhängig geprüfter Freigabe und
gesondert autorisiertem Rollout von
infra-PR `heimgewebe/infra#165` (für den CI-Linkcheck nicht erreichbar).
Dieser eng begrenzte Fix erlaubt ausschließlich
`$FUNDUS/**/*.sql.gz`; er lässt Ausschlüsse des gesamten Fundus oder
von `*.sql.gz.age` weiterhin nicht zu. **Kein Muster in der
produktiven Datei aktivieren, solange das tatsächlich installierte
Wrapper-Release den vollständigen Satz nicht annimmt.** Den
Hostpfad vor einer Freigabe frisch lesen, nicht aus dem Runbook
als zeitlose Konstante ableiten.

Ein synthetischer Test gegen den tatsächlichen
Restic-Snapshot-Mechanismus bestätigte: Sechs **erst nach dem
Vorabscan** entstandene `*.sql.gz`-Dateien unter den drei
Quellwurzeln wurden beim Backup ausgeschlossen; normale Dateien,
Fundus-Material und `*.sql.gz.age` blieben erhalten. Der
frühere Live-Wrapper wies das Fundus-Muster noch zurück; der
geänderte Quellstand aus infra-PR #165 akzeptiert es bei weiterhin
verbotenen breiten Fundus-Ausschlüssen (RED/GREEN und 61/61
Restic-Runtime-Tests). Diese synthetischen Nachweise ersetzen
**nicht** einen tatsächlichen Produktiv-Snapshot. Sollte der Fundus
jemals eine legitime `*.sql.gz`-Quelldatei enthalten, kann sein
bestehender Inventar-/Restore-Vergleich fail-closed abbrechen;
diesen Konflikt vor einer Betriebsfreigabe inventarisieren.

Vor einer Betriebsfreigabe die aktive Exclude-Datei, `ExecStart`,
`--exclude-file` und alle Snapshot-Quellen erneut lesen; die Regel
unter derselben Serviceidentität mit einem isolierten Test und einem
frisch erstellten Snapshot nachweisen. Dabei auch prüfen, ob
sicherungswürdige andere `*.sql.gz`-Dateien ausgenommen würden. Die
historischen PostgreSQL-Klartextbestände **nicht** löschen. Der bisherige
Preflight-Scanner kann bestehende Dumps weiterhin erkennen und den
ganzen Snapshot blockieren; seine spätere Deaktivierung oder Anpassung
bedarf ebenfalls einer gesonderten Freigabe und darf erst nach dem
wirksamen nativen Restic-Ausschluss erfolgen. Neue Scanner,
Watcher oder Dienste sind dafür nicht nötig.

Wirksam ist es erst als `ExecStartPre=` der Unit, die den Snapshot startet
(laut Readback vom 15. Oktober 2026 `restic-backup-1930.service`). Die
Quellliste des Gates muss exakt der Quellliste dieses Snapshots entsprechen;
vorher mit `systemctl cat restic-backup-1930.service` (bzw. `--user`,
passend zur tatsächlichen Unit) und dem gestarteten Skript abgleichen. Mit
dem dort festgestellten Unit-Verzeichnis, zum Beispiel für eine User-Unit:

```bash
set -euo pipefail
commit="$(git rev-parse HEAD)"
gate="$HOME/.local/libexec/weltgewebe/$commit/restic-plaintext-gate.sh"
dropin="$HOME/.config/systemd/user/restic-backup-1930.service.d"
[[ -x "$gate" ]]
install -d -m 0755 "$dropin"
printf '[Service]\nExecStartPre=%s %s %s %s\n' "$gate" \
  "$HOME/repos" "$HOME/artifacts/merges" "$HOME/.local/share/schauwerk/fundus" \
  > "$dropin/weltgewebe-plaintext-gate.conf"
systemctl --user daemon-reload
systemctl --user cat restic-backup-1930.service
```

Erst ein negativer Lauf auf `heim-pc` schließt diese Sperre: eine
inhaltsleere Testdatei `weltgewebe-postgres-gate-test.sql.gz` in eine Quelle
legen, den Snapshot-Service starten und belegen, dass er vor `restic`
fehlschlägt (`journalctl` zeigt `restic-plaintext-gate:` und keinen neuen
Snapshot), die Testdatei entfernen und einen erfolgreichen Lauf belegen.
Eine bloß dokumentierte Warnung oder ein deaktivierter Backup-Timer gilt
nicht als Ersatz.


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
# Für einen älteren Restore-Punkt BACKUP_RECEIPT auf einen passenden
# unveränderten receipts/<backup>.pulled-*.receipt setzen. BACKUP_BASE allein
# ist kein Integritätsbeleg. Für den neuesten Punkt gilt latest-pull.receipt.
receipt="${BACKUP_RECEIPT:-$src/latest-pull.receipt}"
case "$receipt" in
  "$src/latest-pull.receipt" | "$src/receipts/"*.receipt) ;;
  *) printf 'Ungültiger Pull-Receipt-Pfad.\n' >&2; exit 1 ;;
esac
[[ -f "$receipt" && ! -L "$receipt" ]] || {
  printf 'Pull-Receipt fehlt oder ist ein Symlink.\n' >&2
  exit 1
}
field() { awk -F= -v key="$1" '$1==key { print substr($0, length(key)+2); exit }' "$2"; }
for key in contract file sha256 plaintext_sha256 encryption recipients result; do
  [[ "$(grep -c "^$key=" "$receipt" || true)" == 1 ]] || {
    printf 'Pull-Receipt-Feld fehlt oder kommt mehrfach vor: %s\n' "$key" >&2
    exit 1
  }
done
[[ "$(field contract "$receipt")" == weltgewebe-offhost-backup-pull-v2 &&
   "$(field result "$receipt")" == ok && "$(field encryption "$receipt")" == age ]] || exit 1
# Gleiches BACKUP_PREFIX wie Producer und Pull (Default weltgewebe-postgres).
prefix="${BACKUP_PREFIX:-weltgewebe-postgres}"
[[ "$prefix" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$ && "$prefix" != *..* ]] || exit 1
file="$(field file "$receipt")"
[[ "$file" == "$prefix"-?*.sql.gz.age && "$file" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.sql\.gz\.age$ &&
   "$file" != *..* ]] || exit 1
base="${file%.sql.gz.age}"
[[ -z "${BACKUP_BASE:-}" || "${BACKUP_BASE%.sql.gz.age}" == "$base" ]] || {
  printf 'BACKUP_BASE stimmt nicht mit dem ausgewählten Receipt überein.\n' >&2
  exit 1
}
# Die mutable latest-Datei allein genügt nicht: exakt derselbe Beleg muss
# als create-only veröffentlichte historische Version vorhanden sein.
matched_history=0
for historical in "$src/receipts/${base}.pulled-"*.receipt; do
  [[ -e "$historical" || -L "$historical" ]] || continue
  [[ -f "$historical" && ! -L "$historical" ]] || exit 1
  cmp -s -- "$receipt" "$historical" && matched_history=1
done
((matched_history == 1)) || {
  printf 'Kein passender unveränderlicher Pull-Receipt vorhanden.\n' >&2
  exit 1
}
cipher_sha="$(field sha256 "$receipt")"
plaintext_sha="$(field plaintext_sha256 "$receipt")"
[[ "$cipher_sha" =~ ^[0-9a-f]{64}$ && "$plaintext_sha" =~ ^[0-9a-f]{64}$ ]] || exit 1
for manifest_path in "$src/$base.age.sha256.manifest" "$src/$base.sha256.manifest"; do
  [[ -f "$manifest_path" && ! -L "$manifest_path" ]] || exit 1
done
[[ "$(field file "$src/$base.age.sha256.manifest")" == "$file" &&
   "$(field plaintext_file "$src/$base.age.sha256.manifest")" == "$base.sql.gz" &&
   "$(field sha256 "$src/$base.age.sha256.manifest")" == "$cipher_sha" &&
   "$(field plaintext_sha256 "$src/$base.age.sha256.manifest")" == "$plaintext_sha" &&
   "$(field file "$src/$base.sha256.manifest")" == "$base.sql.gz" &&
   "$(field sha256 "$src/$base.sha256.manifest")" == "$plaintext_sha" ]] || {
  printf 'Manifeste stimmen nicht mit dem Pull-Receipt überein.\n' >&2
  exit 1
}
[[ -f "$src/$file" && ! -L "$src/$file" &&
   "$(sha256sum "$src/$file" | awk '{print $1}')" == "$cipher_sha" ]] || {
  printf 'Chiffrat stimmt nicht mit dem Pull-Receipt überein.\n' >&2
  exit 1
}
work="$(mktemp -d)"
# Der entschlüsselte Dump wird bei jedem Ende dieser Shell entfernt, auch nach
# einem Fehler in age, SHA-Vergleich, gzip -t oder Restore und nach Abbruch.
trap 'rm -rf -- "$work"' EXIT
trap 'exit 130' INT TERM HUP
age -d -i /pfad/zur/weltgewebe-backup-identity.txt \
  -o "$work/$base.sql.gz" "$src/$base.sql.gz.age"
cp "$src/$base.sha256.manifest" "$work/"
[[ "$(sha256sum "$work/$base.sql.gz" | awk '{print $1}')" == "$plaintext_sha" ]]
gzip -t "$work/$base.sql.gz"
```

Für einen historischen Wiederherstellungspunkt das passende
`BACKUP_RECEIPT` aus `receipts/` explizit auswählen; `BACKUP_BASE` ist
allenfalls eine zusätzliche Gleichheitsprüfung. Dieser Ablauf gilt für den
`normal`-Pull **mit** Erfolgs-Receipt, nicht für das ausdrücklich
noch unzertifizierte `migration-stage`-Staging ohne Receipt. Die
create-only gespeicherten Receipts verhindern versehentliche Verwechslungen,
sind aber **keine kryptografischen Signaturen** gegen einen Angreifer mit
Schreibzugriff auf Sicherung *und* Receipt. Die Prüfung wurde mit einer echten
synthetischen age-Verschlüsselung und positiven sowie manipulierten
Sicherungs-/Receipt-Kombinationen ausgeführt; ein echter produktiver Restore
ist damit nicht belegt.

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
