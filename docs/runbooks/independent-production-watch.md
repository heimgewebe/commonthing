---
id: runbooks.independent-production-watch
title: Unabhängige Produktionswache
doc_type: reference
status: active
summary: Off-GitHub-Beobachtung, lokale P1/P2-Evidenz, Grenzen von Cron und Benachrichtigung.
relations:
  - type: relates_to
    target: docs/runbooks/README.md
  - type: relates_to
    target: docs/runbook.observability.md
---
# Unabhängige Produktionswache (Heimberry)

## Zweck und Grenzen

Die versionierte Quelle liegt unter scripts/ops/independent_production_watch.py,
die Cron-Vorlage unter scripts/ops/independent_production_watch.crontab. Die
Wache prüft von Heimberry aus vier öffentliche Endpunkte (Frontend, API,
GitHub-main und geplante GitHub-Workflowläufe). Die Netzwerkzugriffe sind
lesend; lokal schreibt sie heartbeat.json, state.json und events.jsonl.
Sie verschickt keine E-Mail, GitHub-Issue, Push-Benachrichtigung oder Webhook.

Der Repository-Code ist nicht automatisch der auf Heimberry ausgeführte Code.
Installation, Cron-Ausführung und tatsächlicher Alarmempfang sind getrennt
über Grabowski mit Live-Readback zu belegen. Fixture-Tests allein beweisen
keine ununterbrochene Produktivüberwachung.

## Zustände und Alarme

- P1 / ALARM: belegte Produktabweichung, insbesondere Frontend-/API-Divergenz,
  falscher Frontend-Cache oder zu lange ausbleibende Konvergenz nach einem
  tatsächlich lokal beobachteten neuen GitHub-main-SHA.
- P2 / MONITOR_DATA_FAILURE: fehlerhafte Primärdaten, insbesondere eine
  fehlende oder ungültige main-SHA. Das ist kein Produktionsfehlernachweis.
  Ein bestehender P1-Zustand bleibt bis zur bestätigten Wiederherstellung.
- INFO: ausbleibende, fehlerhafte oder unlesbare GitHub-Scheduleläufe;
  sie sind kein eigener Produktions-P1.

Die 45-Minuten-Konvergenzfrist beginnt beim ersten lokal beobachteten
GitHub-main-SHA und wird in state.json als main_observation gespeichert.
Ein älterer Git-Commit-Zeitstempel darf einen frischen Push nicht als
überfälliges Deployment ausgeben. Nach einem Zustandverlust beginnt die
Frist vorsichtig neu: echte Verzögerungen können dadurch später erkannt
werden, aber es entsteht kein unbelegter Sofortalarm. Transiente
GitHub-Referenzfehler löschen den letzten gültigen Beobachtungsanker nicht.

Frontend-/API-Divergenzen werden anhand der beiden Produktions-SHAs
dedupliziert, unabhängig von späteren Änderungen auf main. Eine RECOVERY
benötigt wieder gültige Primärdaten; reine Messfehler reichen nicht.

## Betrieb und Verifikation

Historischer Installationspfad auf Heimberry:
/home/alex/.local/commonthing-production-watch.py
Cron läuft unter dem Benutzer alex alle fünf Minuten. Die versionierte
Crontab ist ein Sollvertrag, kein aktueller Runtime-Nachweis.

Vor jeder produktiven Änderung: Skript-/Crontab-SHA, Zustand von cron.service,
letzten automatisch erzeugten Heartbeat und state.json über Grabowski lesen.
Nach autorisierter Aktualisierung frisch auf der Zielmaschine gegen den
tatsächlich ausgelieferten Source-Commit prüfen.

Lokale source-only Checks:
    python3 -B scripts/ci/tests/test_independent_production_watch.py
    ruff check --no-cache --isolated scripts/ops/independent_production_watch.py scripts/ci/tests/test_independent_production_watch.py

Lokale ALARM-Events sind ausdrücklich keine nachgewiesen zugestellten
Benachrichtigungen. Die Wache beweist ihre eigene Cron-Liveness nicht
unabhängig; ein externer Empfänger samt echtem Benachrichtigungstest bleibt
in Issue #1939 offen: https://github.com/heimgewebe/commonthing/issues/1939 .
