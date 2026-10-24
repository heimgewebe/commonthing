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
Die Beobachtung selbst verschickt keine Benachrichtigungen. Ein gesonderter,
standardmäßig nicht eingerichteter Transport kann ausschließlich ihre
bereits dauerhaft protokollierten ALARM-/RECOVERY-Übergänge versenden.

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
in [Issue #1939](https://github.com/heimgewebe/commonthing/issues/1939).

## Opt-in: externe Alarmzustellung

Die bestehende lokale, fsync-gesicherte Ereignisdatei
`~/.local/commonthing-watch-state/events.jsonl` bleibt die einzige
Ereignisquelle. `scripts/ops/independent_production_watch_dispatch.py`
verwendet denselben Fünf-Minuten-Cronlauf, unmittelbar **nach** dem Watcher.
Der Watcher liefert bei ALARM Exitcode 2; das Semikolon im Cronvertrag
führt den Transport trotzdem aus. Der Sender ist kein neuer Daemon und
schreibt weder GitHub noch Commonthing.

**Ohne bestätigten Empfänger wird nichts gesendet.** Vor einer
autorisierten Einrichtung muss ein tatsächlich vorgesehener Alarmempfänger
die Verwendung des bereits anderweitig eingesetzten Ntfy-Transports
bestätigen. Der Topic-Schlüssel liegt nur in
`~/.config/commonthing-watch/ntfy-topic` (Datei 0600,
Elternverzeichnis 0700, gleicher Eigentümer); dieser Schlüssel darf weder
im Repository noch in Receipts, Cronzeilen oder Logs erscheinen.
Das Sendeskript enthält keine Empfängeradresse und keine
umgebungsvariablengesteuerte Ausweichadresse.

Die Enrollment-Operation versendet selbst keine Nachricht:

```bash
/usr/bin/python3 -B /home/alex/.local/commonthing-production-watch-dispatch.py \
  --enroll --acknowledge-prior-events-not-delivered
```

Sie verlangt eine vorhandene, lesbare und vollständig abgeschlossene
Journalhistorie sowie den ausdrücklich eingerichteten Topic-Schlüssel.
Das create-only `transport-cursor.json` bindet Journal-Inode,
Byteposition, SHA-256-Präfix und den Hash des Topic-Schlüssels.
Bereits vorhandene Ereignisse werden **nicht** rückwirkend als zugestellt
deklariert oder versandt. Insbesondere bleibt ein vor Enrollment
entstandener, derzeit noch aktiver ALARM unbestätigt. Ein Empfängerwechsel
darf nicht automatisch die bestehende Cursorbindung umwidmen.

Danach sendet derselbe Cronlauf höchstens acht neue, vollständige
ALARM-/RECOVERY-Journaleinträge in FIFO-Reihenfolge. Bei HTTP- oder
Filesystemfehler wird nicht über den betroffenen Eintrag hinweg
fortgesetzt. Exakt identische Ereignisbytes werden im begrenzten
beobachteten Fenster dedupliziert. Der HTTP-Status eines angenommenen
POST und die Ereignis-ID werden erst nach einem erfolgreichen HTTP-Call
in einer fsync-gesicherten lokalen Cursorrevision gespeichert.
Fehlende, kaputte, ausgewechselte oder nicht kanonisch gewachsene
Journal-/Cursordateien blockieren. Keine automatische Daten- oder
Historienlöschung, kein Retention-Prune, kein neuer Alarm allein
durch einen Test.

**Nachweisgrenze:** Ein HTTP 200/201/202 besagt nur, dass der
Ntfy-Server den POST angenommen hat. Es ist kein Beleg für
Geräteauslieferung oder menschliche Kenntnisnahme. Erfolgt ein
Prozessabbruch nach dem extern angenommenen POST, aber vor dem
Cursor-fsync, kann ein neuer Aufruf denselben Alarm erneut senden.
Genau-einmal-Netzwerkeffekte werden nicht behauptet. Die
`last_acceptance` enthält deshalb ausdrücklich die Nichtbeweise.

Vor Produktionsfreigabe getrennt und aktuell nachweisen:

1. Akzeptierter, exakt commitgebundener Code ist auf Heimberry installiert;
   Cron-Readback, Skriptbytes und beobachteter Lauf stimmen überein.
2. Ein **automatisch vom Watcher** erzeugtes echtes Testereignis wurde
   durch den Cron-Transport extern akzeptiert, anschließend am
   autorisierten Ziel tatsächlich empfangen und über dieselbe Event-ID
   quittiert.
3. Die menschliche Kenntnisnahme wird gesondert und ereignisgebunden
   belegt; ein Transportreceipt genügt nicht.
4. Der vollständige Ausfall von Heimberry/Cron/Watcher wird durch
   eine **davon unabhängige** Beobachtungsinstanz erkannt. Diese
   Funktion wird vom eigenen Crontransport nicht nachgewiesen.

Diese Änderungen ersetzen weder externe PostgreSQL-Recovery noch
Datenschutz-, Restic-, R3-, Captain- oder Root-HOLD-Gates.
