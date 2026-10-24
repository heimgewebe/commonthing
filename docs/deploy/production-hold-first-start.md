# Production HOLD: ausdrücklich autorisierter erster Reconciler-Start

Status: Sicherheitsvertrag, **keine** Produktionsfreigabe. Der absichtlich deaktivierte
`weltgewebe-production-reconcile.timer` bleibt deaktiviert, solange irgendein
Backup-, Offline-Restore-, Privacy-, Alarmierungs-, Review- oder Captain-Gate offen ist.

## Root-gebundene Releaseentscheidung

Nur ein gesondert autorisierter Root-Operator darf nach unabhängiger Prüfung
der **tatsächlich ausgeführten** Primärbelege die Datei
`/etc/weltgewebe/production-hold-release.json` erzeugen. Die Datei gehört
root, ist kein Symlink/Hardlink und besitzt Modus `0600`. Weder dieses
Repository noch der Installer erzeugen oder überschreiben sie.

Verbindliches Schema (Werte sind **keine** Produktionsbelege, sondern
schematische Platzhalter):

```json
{
  "schema_version": 1,
  "kind": "weltgewebe.production-hold-release.v1",
  "environment": "production",
  "decision": "release",
  "commit": "<40-stelliger freigegebener Commonthing-Main-SHA>",
  "schaubild_image_digest": "sha256:<64-stelliger akzeptierter OCI-Digest>",
  "offhost_age_recovery_sha256": "<SHA-256 des echten externen Entschlüsselungs- und PostgreSQL-Restore-Receipts>",
  "restic_authority_sha256": "<SHA-256 eines unabhängigen Restic-Authority-Receipts>",
  "privacy_acceptance_sha256": "<SHA-256 der revisionsgültigen Datenschutzfreigabe>",
  "alarm_delivery_sha256": "<SHA-256 der automatischen externen Alarmzustellung>",
  "human_acknowledgement_sha256": "<SHA-256 der konkreten menschlichen Empfangsbestätigung>",
  "authorized_by": "<autorisierte Person oder Rolle>",
  "approved_at": "<ISO-8601-Zeitpunkt mit Zeitzone>"
}
```

Die Hashfelder sind nur **Referenzen auf vorher unabhängig geprüfte
Artefakte**. Der Validator prüft das Format, den root-geschützten Speicherort,
die Identität von Main und OCI und die formale Entscheidung. Er überprüft nicht
selbst die Richtigkeit oder Unveränderlichkeit der referenzierten externen
Receipts. Ein syntaktisch gültiger Freigabedatensatz ist **kein** Ersatz für
tatsächlichen age-Off-Host-Restore, getrennte Key-Verwahrung, Datenschutz-R3,
nachgewiesene Alarmzustellung und menschliche Kenntnisnahme.

Die Freigabe gilt ausschließlich für den angegebenen Main-Commit und
Schaubild-OCI-Digest. Fortschreitendes `main` benötigt eine neue dokumentierte
Freigabe; ein Timer darf nie automatisch einen neueren Commit durchwinken.
Zum erneuten HOLD die Root-Freigabe entziehen und den Timer unter der zuständigen
Operatorautorität deaktivieren. Bereits laufende Deployments benötigen zusätzlich
sicheres Betriebshandling; die reine Dateientfernung ist kein atomarer Kill-Switch.

## Recovery-Frische und menschliche Alarmbestätigung

Die früheren Prüfverträge nennen **26 Stunden** für den lokalen Backup-Nachweis,
**36 Stunden** für den verschlüsselten Off-Host-Pull und **192 Stunden** für
einen lokalen Restore-Proof. Diese Intervalle messen unterschiedliche Aussagen
und sind **kein** vereinbartes gemeinsames Recovery Point Objective (RPO).
Die für die Produktionsfreigabe zulässige maximale Datenverlustspanne und
das Alter der echten extern restaurierten Kopie müssen anhand eines separaten
fachlichen Recovery-Receipts entschieden werden. Ein lokaler 192-Stunden-Test
ersetzt niemals die isolierte Entschlüsselung einer externen age-Kopie.

Der menschliche Alarmempfänger muss vor Freigabe explizit autorisiert und
erreichbar sein. Die automatische Zustellbestätigung und die an **dieses
konkrete Ereignis** gebundene menschliche Empfangsbestätigung bleiben
getrennte Nachweise. Der Watcher darf den Empfang nicht selbst behaupten.

## Aktivierung und Readback

- `activate-production-reconciler-from-release.sh` validiert Releasepfad,
  root-gebundene Freigabe und den **read-only** Installer-Preflight, bevor
  `ensure_web_push_vapid_env.py` einen privaten VAPID-Key erzeugen könnte.
- Der Installer validiert die Freigabe erneut und vergleicht den Commit zuerst
  gegen `ls-remote origin main`. Ein inaktiver Timer sperrt `--defer-reconcile`
  vor jeder VAPID-Mutation. In der expliziten erstmaligen Startvariante
  deaktiviert er einen vorhandenen Timer vor Systemd-Installation, startet
  danach **zuerst** den Service und aktiviert den Timer erst nach dessen Erfolg.
- Der Timer verwendet `OnActiveSec=2min` statt `OnBootSec=45s`, somit keine
  rückwirkend fällige Boot-Auslösung beim Enable. Der Reconciler prüft die
  Root-Freigabe und den exakten Remote-Main vor Zustandsschreibvorgängen,
  erneut nach Fetch, mit dem OCI-Digest sowie vor Build-Cache- und
  Deployment-/Migrationsänderungen.
- Ein erfolgreicher Installer, manueller Service-Run oder aktivierter Timer
  **beweist noch kein Produktions-PASS**. Erforderlich sind ein terminaler
  Reconcile-Receipt, tatsächliche Docker-OCI-Identität, Frontend/API-Identität,
  nativer Renderer-Browserreadback sowie unabhängige Watcher-Konvergenz.

**Verboten:** Die bestehende HOLD-Sperre durch `systemctl enable --now`,
direktes `docker compose up`, alte Klartext-Pulls oder eine synthetische
Offline-Identity zu umgehen.