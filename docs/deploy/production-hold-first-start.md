---
id: deploy.production-hold-first-start
title: "Production HOLD: autorisierter erster Reconciler-Start"
doc_type: runbook
status: active
summary: "Nachweisgrenzen und Freigabeablauf für den ersten produktiven Reconciler-Start; keine eigenständige Produktionsfreigabe."
relations:
  - type: relates_to
    target: docs/deploy/merge-to-live.md
  - type: relates_to
    target: scripts/ops/verify_production_hold_release.py
---

# Production HOLD: ausdrücklich autorisierter erster Reconciler-Start

Status: technischer Root-Freigabevertrag, **keine** automatische Produktionsfreigabe.
Vor dem ersten Start müssen ein echter Restic-Restore, der verschlüsselte
Off-Host-PostgreSQL-Restore und die exakte Main-/OCI-Identität nachgewiesen sein.
Privacy-, Alarm- und Empfangsprozesse werden separat betrieben; sie sind keine
zusätzlichen Hash-Pflichtfelder für den technischen Produktionsstart.

## Root-gebundene Releaseentscheidung

Ein autorisierter Root-Operator erstellt nach Prüfung der **tatsächlich
ausgeführten Wiederherstellungen** die Datei
`/etc/weltgewebe/production-hold-release.json`. Die Datei gehört
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
  "authorized_by": "<autorisierte Person oder Rolle>",
  "approved_at": "<ISO-8601-Zeitpunkt mit Zeitzone>"
}
```

Die beiden Hashfelder sind **Referenzen auf echte Wiederherstellungsbelege**.
Der Validator prüft Format, root-geschützten Speicherort, Identität von Main
und OCI sowie die Entscheidung; die Belege muss der Operator tatsächlich lesen.
Ein syntaktisch gültiger Datensatz ersetzt keinen realen age-Off-Host-Restore.
Die bisherigen Felder `privacy_acceptance_sha256`,
`alarm_delivery_sha256` und `human_acknowledgement_sha256`
werden aus Kompatibilitätsgründen weiterhin akzeptiert und bei Angabe
validiert, sind aber für neue Releases **nicht nötig**. Ein privater age-Key
auf dem getrennten Wiederherstellungsrechner genügt; ein dritter Offline-Rechner
ist keine Release-Bedingung.

Die Freigabe gilt ausschließlich für den angegebenen Main-Commit und
Schaubild-OCI-Digest. Fortschreitendes `main` benötigt eine neue dokumentierte
Freigabe; ein Timer darf nie automatisch einen neueren Commit durchwinken.
Zum erneuten HOLD die Root-Freigabe entziehen und den Timer unter der zuständigen
Operatorautorität deaktivieren. Bereits laufende Deployments benötigen zusätzlich
sicheres Betriebshandling; die reine Dateientfernung ist kein atomarer Kill-Switch.

## Recovery-Frische und Alarmierung im Betrieb

Die früheren Prüfverträge nennen **26 Stunden** für den lokalen Backup-Nachweis,
**36 Stunden** für den verschlüsselten Off-Host-Pull und **192 Stunden** für
einen lokalen Restore-Proof. Diese Intervalle messen unterschiedliche Aussagen
und sind **kein** vereinbartes gemeinsames Recovery Point Objective (RPO).
Die für die Produktionsfreigabe zulässige maximale Datenverlustspanne und
das Alter der echten extern restaurierten Kopie müssen anhand eines separaten
fachlichen Recovery-Receipts entschieden werden. Ein lokaler 192-Stunden-Test
ersetzt niemals die isolierte Entschlüsselung einer externen age-Kopie.

Ein unabhängiger Watcher soll Versionsdrift, Backupfehler und Störungen
erkennen. Alarmzustellung und tatsächliche Kenntnisnahme müssen als eigene
Betriebsaussagen dokumentiert werden, dürfen aber den technischen Reconcile
nach bestandenem Recovery nicht künstlich blockieren. Der Watcher darf nicht
behaupten, dass ein Mensch eine Nachricht gelesen hat.

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
  rückwirkend fällige Boot-Auslösung beim Enable. Vor jeder gemeinsam genutzten
  State-, Lock-, Git-Tracking-Ref-, Timer-, VAPID- oder Release-Wirkung prüfen
  Installer, Aktivator, Reconciler und direkter Deploy-Helfer **beide**
  Root-HOLD-Identitäten: den exakten Main-Commit und den OCI-Digest aus dem
  unveränderlichen Git-Objekt `<commit>:infra/schauwerk-editor/release-lock.json`.
  Ist der genehmigte Commit noch nicht lokal vorhanden, darf ausschließlich
  dessen Objektinhalt mit `--no-write-fetch-head` und ohne Tracking-Ref-Ziel
  aus dem Git-Remote geholt werden. Dies verändert den lokalen Git-Objektspeicher,
  aber weder FETCH_HEAD noch Branch-/Remote-Refs; es ist kein Deploymentbeweis
  und keine Freigabe für weitere Nebenwirkungen. Der Reconciler validiert beide
  Identitäten erneut nach dem regulären lockgebundenen Fetch, vor Build-Cache-
  sowie vor Deployment- und Migrationsänderungen.
- Ein erfolgreicher Installer, manueller Service-Run oder aktivierter Timer
  **beweist noch kein Produktions-PASS**. Erforderlich sind ein terminaler
  Reconcile-Receipt, tatsächliche Docker-OCI-Identität, Frontend/API-Identität,
  nativer Renderer-Browserreadback sowie unabhängige Watcher-Konvergenz.

**Verboten:** Die bestehende HOLD-Sperre durch `systemctl enable --now`,
direktes `docker compose up`, alte Klartext-Pulls oder eine synthetische
Offline-Identity zu umgehen.
