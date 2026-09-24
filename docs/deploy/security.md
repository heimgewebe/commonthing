---
id: deploy.security
title: Deploy Security
doc_type: architecture
status: active
summary: Security configuration and CSP rules for deployment.
relations:
  - type: relates_to
    target: docs/deployment.md
  - type: relates_to
    target: docs/deploy/README.md
---

## Deploy Security

## CSP Contract Guard

The frontend build may contain an inline bootstrap script.
If CSP blocks inline scripts, the application will render a blank page.

The deploy guard ensures that:

inline bootstrap → CSP allows inline execution

## Web Push für private Nachrichten

Web Push ist ein optionaler Zustellkanal. Das private Nachrichtenpostfach und
der kanonische PostgreSQL-Nachrichtenspeicher bleiben verbindlich. Eine
fehlgeschlagene, verspätete oder deaktivierte Push-Zustellung darf weder eine
Nachricht verändern noch ihren Zugriff über `/nachrichten` beeinflussen.

### Betriebsvariablen

Die API akzeptiert Web Push nur als vollständige Dreierkonfiguration:

- `WEB_PUSH_VAPID_PRIVATE_KEY`: privater P-256-Schlüssel als exakt 32 Byte,
  unaufgefüllt base64url-kodiert;
- `WEB_PUSH_VAPID_CONTACT`: betriebliche Kontaktadresse als `mailto:`- oder
  HTTPS-URL;
- `WEB_PUSH_ALLOWED_HOST_SUFFIXES`: kommagetrennte Liste zugelassener
  Push-Anbieter-DNS-Endungen.

Sind alle drei Variablen leer, bleibt Push geschlossen und die übrige API läuft
normal. Ist nur ein Teil gesetzt oder ist ein Wert ungültig, verweigert die API
den Start. Der private Schlüssel gehört ausschließlich in die geschützte
Runtime-Umgebung. Er darf weder im Repository noch in Compose-Dateien,
Build-Artefakten, Protokollen oder Beweisdateien stehen.

Vorgesehene Anbietergrenze für die erste Aktivierung:

```text
WEB_PUSH_ALLOWED_HOST_SUFFIXES=fcm.googleapis.com,push.services.mozilla.com,web.push.apple.com
```

Die API akzeptiert ausschließlich HTTPS, den Standardport 443 und exakt diese
DNS-Namen oder echte Subdomains. IP-Adressen, Zugangsdaten, Wildcards,
URL-Pfade und Weiterleitungen werden zurückgewiesen. Eine Erweiterung der Liste
ist daher eine eigene Sicherheitsentscheidung und kein gewöhnlicher
Konfigurationswechsel.

### Produktions-Bootstrap

Der kanonische VPS-Deploypfad führt vor der ersten Containerwirkung
`scripts/ops/ensure_web_push_vapid_env.py` aus. Der Bootstrap arbeitet
fail-closed:

- Sind alle drei Variablen leer oder nicht vorhanden, erzeugt er einmalig einen
  kryptografisch zufälligen P-256-Schlüssel und schreibt das vollständige
  Dreierpaket atomar in `/etc/weltgewebe/weltgewebe.env`.
- Ist eine vollständige Konfiguration vorhanden, validiert und bewahrt er sie
  bytegenau. Der Schlüssel wird insbesondere nicht bei jedem Deploy erneuert.
- Teilkonfigurationen, Duplikate, ungültige Werte, Symlinks, fremde Eigentümer,
  unsichere Dateirechte und übergroße Dateien brechen den Deploy vor der
  Containerwirkung ab.
- Der private Wert erscheint weder in der Konsolenausgabe noch in
  Deployment-Receipts. Vor der ersten Änderung entsteht eine geschützte
  Sicherung `weltgewebe.env.pre-web-push-v1`.

Andere Zellen können die drei Werte weiterhin über ihren eigenen
Secret-Speicher setzen. Der automatische Bootstrap ist an den privilegierten,
exakt commitgebundenen VPS-Releasepfad gebunden.

### Sichere Aktivierungsreihenfolge

1. Der VPS-Releasepfad erzeugt oder validiert die vollständige
   Web-Push-Konfiguration im geschützten Runtime-Env.
2. Die Migration `20260804000001_web_push_notifications` läuft über den
   kanonischen migrationssicheren API-Deploypfad.
3. Die neue API-Version startet. Der Start muss
   `Privacy-safe Web Push delivery configured` protokollieren; ein
   teilkonfigurierter Zustand ist unzulässig.
4. Mit einer angemeldeten Sitzung `/api/push/config` lesen. Erst
   `enabled: true` zusammen mit einem nicht leeren öffentlichen
   Anwendungsschlüssel belegt die betriebliche Aktivierung.
5. Auf einem Testkonto die Kontofreigabe einschalten und genau ein Testgerät
   registrieren. Die Browserberechtigung muss aus einer bewussten Nutzeraktion
   entstehen.
6. Eine private Testnachricht senden und auf dem Gerät prüfen, dass der Hinweis
   weder Nachrichtentext noch Absendername oder Account-ID zeigt und nur zur
   gleichursprünglichen commonThing-Unterhaltung führt.
7. Das Gerät wieder deaktivieren und durch eine weitere Testnachricht belegen,
   dass kein neuer Push-Hinweis entsteht, während beide Nachrichten weiterhin
   im Postfach vorhanden sind.

Ein grüner API-Readback allein belegt noch keine Zustellung durch Apple, Mozilla
oder FCM. Die erste Produktionsaktivierung benötigt deshalb getrennte
Provider- und Gerätebelege für die tatsächlich unterstützten Plattformen.

### Rückbau

Für einen sofortigen Kanalausfall werden zuerst
`WEB_PUSH_VAPID_BOOTSTRAP_MODE=disabled` gesetzt und danach alle drei
Web-Push-Variablen gemeinsam entfernt. Anschließend wird ausschließlich die API
über den migrationssicheren Deploypfad neu gestartet. Der nächste Release
bewahrt den deaktivierten Zustand, statt eine neue VAPID-Identität zu erzeugen.

Die Datenbankmigration wird nicht zurückgerollt: Einstellungen,
Geräteabonnements und Zustellbelege bleiben für Diagnose und kontrollierte
Wiederaktivierung erhalten, der Zustellarbeiter startet jedoch nicht. Der
Rückbau verändert weder gespeicherte Nachrichten noch das Postfach. Bereits von
einem externen Anbieter angenommene Hinweise können technisch nicht
zurückgerufen werden.

Das bloße Leeren der drei Variablen ohne den Bootstrap-Schalter ist absichtlich
gleichbedeutend mit „erstmalige Einrichtung“ und würde eine neue Identität
erzeugen.

### Schlüsselrotation

Eine Änderung des VAPID-Schlüssels ist keine transparente Secret-Rotation.
Bestehende Browserabonnements wurden an den bisherigen öffentlichen Schlüssel
gebunden und müssen kontrolliert erneuert werden. Bis ein eigener
Rotationsworkflow mit Abonnementinventar, Geräteerneuerung und Readback besteht,
darf der Schlüssel nur im Rahmen eines geplanten Push-Rückbaus und einer neuen
Gerätefreigaberunde ersetzt werden.

Die vollständige Produkt- und Datenschutzgrenze steht in
[`docs/specs/private-nachrichten.md`](../specs/private-nachrichten.md).
