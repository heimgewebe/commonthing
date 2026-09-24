---
id: versionierungs-blaupause
title: "commonThing Deploy-Versionierung und Browser-Aktualität"
doc_type: blueprint
status: draft
summary: "Blaupause für eine saubere, beobachtbare Deploy-Identität, die konsistent von Build bis zur UI-Diagnose bleibt."
relations:
  - type: relates_to
    target: docs/blueprints/versionierungs-statusgrundlage.md
  - type: relates_to
    target: docs/deployment.md
---

# commonThing Deploy-Versionierung und Browser-Aktualität

commonThing bekommt eine saubere, beobachtbare Deploy-Identität, die entlang der ganzen Kette konsistent bleibt: Build → version.json → Edge/Caddy → Deploy-Verify → UI-Diagnose → kontrollierte Client-Reaktion.

## 0. Zielbild

commonThing soll zuverlässig Folgendes leisten:

- Jeder Frontend-Build erzeugt eine technische, maschinenlesbare Build-Identität.
- Diese Identität wird ungecacht und kanonisch ausgeliefert.
- Der Deploy-Prozess prüft hart, dass diese Identität korrekt verfügbar ist.
- Die UI zeigt diese Identität an, sodass Browser A vs. Browser B direkt vergleichbar wird.
- Der Browser kann später kontrolliert erkennen, dass eine neuere Version verfügbar ist.
- Kein Service Worker, kein PWA-Umbau, keine unnötige Cache-Magie.

## 1. Architekturprinzipien

### 1.1 Kanonische Wahrheit statt impliziter Annahmen

Die gültige Version eines ausgelieferten Frontends stammt aus einem expliziten Artefakt:

`/_app/version.json`

### 1.2 Build erzeugt Wahrheit, nicht der Browser

Die Versionswahrheit entsteht im Build. Der Browser liest sie nur.

### 1.3 Edge darf Wahrheit nicht verfälschen

`version.json` muss immer frisch kommen:

`Cache-Control: no-store`

### 1.4 HTML und Assets bleiben getrennt behandelt

- HTML / Entry-Routen: `no-cache, must-revalidate`
- Immutable Assets: `public, max-age=31536000, immutable`
- `version.json`: `no-store`

### 1.5 Diagnose vor Automatisierung

Erst wenn die Version sauber sichtbar und prüfbar ist, darf der Browser aktiv auf Versionswechsel reagieren.

## 2. Kanonisches Versionsartefakt

### 2.1 Zielpfad

Kanonischer Diagnosepfad:

`/_app/version.json`

Begründung:

- Liegt nah an der Build-Ausgabe.
- Passt in die bestehende Asset-/Caddy-Logik.
- Ist eindeutig technisch, nicht fachlich semantisiert.

### 2.2 JSON-Schema (minimal)

Pflichtfelder:

```json
{
  "version": "4f9a0e3",
  "build_id": "4f9a0e3-1742155012000",
  "built_at": "2026-03-16T20:10:12Z"
}
```

Optional, nur wenn sauber ableitbar:

```json
{
  "release": "1.4.2"
}
```

### 2.3 Semantik der Felder

#### version

Kanonische und deterministische Artefakt-ID (z.B. Git-Commit). Bleibt für identische Quellcodes gleich.

#### build_id

Optionaler, nicht-kanonischer Bezeichner für den spezifischen CI-Lauf, meist zusammengesetzt aus Commit und Timestamp.

#### built_at

UTC-Zeitstempel des Builds. Optional, falls Determinismus oberste Prio hat (kann via `SOURCE_DATE_EPOCH` überschrieben werden).

#### release

Nur verwenden, wenn das Repo bereits einen sauberen fachlichen Release-Begriff hat. Nicht künstlich erfinden.

### 2.4 Was nicht passieren darf

- Keine Platzhalter.
- Keine geratenen Werte.
- Vermischung von Inhalt und Zeit in der kanonischen `version`.
- Kein stilles Weglassen von `version`, wenn `commit` fehlt (Fallback auf Epoche, falls unvermeidbar).

## 3. Build-Pipeline

### 3.1 Build-ID-Quelle

Bevorzugte Reihenfolge:

1. `version`: `git rev-parse HEAD` (oder short SHA).
2. `build_id`: `<short-sha>-<epoch-ms>`
3. `built_at`: UTC-Timestamp, ggf. via `SOURCE_DATE_EPOCH` fixiert.

Beispiel `version.json`:

```json
{
  "version": "4f9a0e3",
  "build_id": "4f9a0e3-1742155012000",
  "built_at": "2026-03-16T20:10:12Z"
}
```

Warum Duales Modell:

- `version` ermöglicht echte Reproduzierbarkeit (Commit A erzeugt Version A).
- `build_id` + `built_at` behält den CI-Kontext für Deployment-Tracking, ohne die kanonische Identität zu vergiften.

### 3.2 Erzeugungsort

Neue Build-Hilfsdatei:

`apps/web/scripts/generate-version.js`

Aufgabe:

- Build-ID bestimmen.
- JSON erzeugen.
- Sicher nach `build/_app/version.json` schreiben.

### 3.3 Build-Integration

In `apps/web/package.json`:

- Nach `vite build`.
- Keine manuelle Nachbearbeitung außerhalb des Build-Flows.
- Keine CI-exklusive Sonderlogik, wenn lokal und CI denselben Output produzieren können.

### 3.4 Stop-Kriterium für diesen Teil

Erfüllt, wenn:

- `pnpm build` reproduzierbar `build/_app/version.json` erzeugt.
- Die Datei `version` und `built_at` enthält.
- Die Datei dort liegt, wo Caddy sie real ausliefert.

## 4. Edge-/Caddy-Semantik

### 4.1 Ziel

Caddy muss drei Klassen sauber trennen.

#### Klasse A — HTML / Root / Routen

Header: `Cache-Control: no-cache, must-revalidate`

#### Klasse B — immutable assets

Pfad: `/_app/immutable/*`
Header: `Cache-Control: public, max-age=31536000, immutable`

#### Klasse C — version endpoint

Pfad: `/_app/version.json`
Header: `Cache-Control: no-store`

### 4.2 Regelreihenfolge

Die `version.json`-Regel muss vor die allgemeine HTML-/Fallback-Regel.

### 4.3 Stop-Kriterium für diesen Teil

Erfüllt, wenn:

- `curl -I /_app/version.json` liefert `Cache-Control: no-store`.
- `curl -I /map` liefert weiter `no-cache, must-revalidate`.
- `curl -I /_app/immutable/...` liefert weiter `immutable`.

## 5. Deploy-Verify in scripts/weltgewebe-up

### 5.1 Ziel

`weltgewebe-up` darf Frontend-Erfolg nicht mehr nur implizit an HTML/Assets festmachen, sondern muss die Versionierungswahrheit mitprüfen.

### 5.2 Verify-Reihenfolge

- **Schritt 1 — Frontend erreichbar:** `/map` oder kanonische Route erreichbar, HTML strukturell plausibel.
- **Schritt 2 — HTML-Cache-Header korrekt:** `no-cache`, `must-revalidate`.
- **Schritt 3 — immutable asset erreichbar:** im HTML referenziertes Asset erreichbar, HTTP 200.
- **Schritt 4 — immutable header korrekt:** `immutable`, `max-age=31536000`.
- **Schritt 5 — version endpoint erreichbar:** `/_app/version.json`, HTTP 200.
- **Schritt 6 — version endpoint ungecached:** `Cache-Control: no-store`.
- **Schritt 7 — version payload semantisch gültig:** JSON parsebar, `version` vorhanden und nicht leer, optional `built_at` parsebar.

### 5.3 Fehlersemantik

#### Harte Fehler

- Endpoint fehlt.
- Falscher Header.
- JSON kaputt.
- `version` fehlt.

### 5.4 REQUIRE_FRONTEND

Die Override-Idee ist sinnvoll.

- Wenn `REQUIRE_FRONTEND` nicht gesetzt ist -> dynamisch bestimmen.
- Wenn gesetzt -> nur `0` oder `1` erlaubt.
- Leerer String = ungültig.
- `true`, `false`, `yes`, `no` = ungültig.

Gewünschtes Verhalten:

- `REQUIRE_FRONTEND=1` -> Frontend-Verifikation erzwingen.
- `REQUIRE_FRONTEND=0` -> Frontend-Verifikation explizit deaktivieren.
- Alles andere -> fail fast.

### 5.5 Stop-Kriterium für diesen Teil

Erfüllt, wenn:

- `weltgewebe-up` bei kaputtem/missing `version.json` klar scheitert.
- `REQUIRE_FRONTEND=1` reproduzierbar den Frontend-Pfad erzwingt.
- Ungültige `REQUIRE_FRONTEND`-Werte früh und explizit fehlschlagen.

## 6. Teststrategie

### 6.1 Grundsatz

Die Tests sollen Verträge beweisen, nicht curl generisch emulieren.

### 6.2 Deployment-Testmatrix

In `scripts/tests/test_verify_deployment.sh` minimal, aber scharf:

- **22a:** Fehlende HTML-Cache-Header (Erwartung: Fehler).
- **22b:** Fehlende immutable Asset-Header (Erwartung: Fehler).
- **22c:** Positiver Komplettpfad (Erwartung: Erfolg).
- **22d:** `version.json` ohne `no-store` (Erwartung: Fehler).
- **22e:** `version.json` erreichbar, aber ohne brauchbare Build-ID (Erwartung: Fehler).

### 6.3 Testdisziplin

- Kein globales `|| true`.
- Nur Negativtests gezielt abfangen.
- Keine Seitenskripte (z.B. `fix_test_22.sh`).
- Keine Testartefakte im Diff.
- Keine Literal-Dateien wie `$HEADER_FILE`.
- Nur kanonische Testdatei anfassen.

### 6.4 Minimaler Mock-Ansatz

Nur die relevanten Pfade deterministisch bedienen:

- `/map`
- `/_app/immutable/test.js`
- `/_app/version.json`

### 6.5 Stop-Kriterium für diesen Teil

Erfüllt, wenn:

- Die 22a–22e-Matrix vollständig grün bzw. kontrolliert rot/grün wie erwartet läuft.
- Keine Debug-/Artefaktdateien im Diff landen.
- Test 22 nicht mehr implizit an Caddy-Service-Discovery hängt, wenn `REQUIRE_FRONTEND=1` gesetzt ist.

## 7. Dokumentation

### 7.1 docs/deployment.md

Dokumentieren, knapp und sauber:

- `/_app/version.json` ist die maschinenlesbare Diagnosequelle des aktuell ausgelieferten Frontend-Builds.
- `no-store` ist bewusst gewählt.
- HTML, immutable assets und version endpoint haben getrennte Cache-Semantik.
- `REQUIRE_FRONTEND` kann für reproduzierbare Verifikation explizit gesetzt werden.
- Nur `0` und `1` sind gültig.

### 7.2 Was nicht in die Doku gehört

- Kein zukünftiges Polling, als wäre es schon da.
- Kein Service-Worker-Ausblick.

### 7.3 Stop-Kriterium

Erfüllt, wenn:

- Doku sprachlich konsistent ist.
- Override-Semantik präzise ist.

## 8. UI-Diagnose

### 8.1 Ziel

Settings oder ein anderer diskreter Ort zeigt primär die kanonische Version:

`Version abc123`

Optional, falls Release und Build-ID vorhanden sind:

`Release 1.2.0 · Version abc123`
`(Build abc123-174... · gebaut am … UTC)`

Die Bezeichnung „Version“ entspricht hierbei dem kanonischen Feld `version` der Artefakt-Identität. Die Angabe „Build“ (und der Zeitstempel) entspringt `build_id` bzw. `built_at` und liefert lediglich den sekundären Ausführungskontext des CI-Laufs.

### 8.2 Anforderungen

- `fetch('/_app/version.json', { cache: 'no-store' })`
- Fehler -> Version unbekannt.
- Invalide `built_at` -> Timestamp ausblenden, nicht crashen.

### 8.3 Testfälle

- Valid payload.
- Fetch fail.
- Release fehlt.
- Invalid `built_at`.

## 9. Kontrollierte Browser-Selbstaktualisierung

### 9.1 Ziel

Browser erkennt:

`server_version != local_version`

und zeigt einen Hinweis an.

### 9.2 Trigger

- Beim App-Start.
- `visibilitychange`.
- `pageshow` für bfcache-Rückkehr.
- Optional später Intervall, aber nur wenn wirklich nötig.

### 9.3 UX

- Banner/Toast: "Neue Version verfügbar".
- Aktion: "Neu laden".
- Kein stilles Auto-Reload.

## 10. Roadmap

### Phase A — Basis konsolidieren

- [x] Prüfen, ob `build/_app/version.json` bei vorhandener Git-Metadaten-/Epoch-Basis deterministisch erzeugt wird (sonst pragmatischer Timestamp-Fallback).
- [x] Schema minimal halten: `version`, `built_at`, optional `commit`.
- [x] Sicherstellen, dass die Build-ID pro Build eindeutig ist.
- [x] Sicherstellen, dass `version` das kanonische Feld ist und kein Alias-System entsteht.
- [x] Build-Integration in `apps/web/package.json` verifizieren.

**Stop-Kriterium:** `pnpm build` erzeugt `build/_app/version.json` (reproduzierbar, sofern Quell-Metadaten wie Git-Commit oder `SOURCE_DATE_EPOCH` verfügbar sind).

### Phase B — Edge-Semantik härten

- [x] Caddy-Regel für `/_app/version.json` mit `Cache-Control: no-store` bestätigen.
- [x] Regelreihenfolge gegen HTML-Fallback absichern.
- [x] HTML-Regel auf `no-cache, must-revalidate` verifizieren.
- [x] Immutable-Regel unverändert funktional bestätigen.

**Stop-Kriterium:** Header-Matrix für HTML / immutable / version ist korrekt.

### Phase C — Deploy-Verify finalisieren

- [x] `weltgewebe-up` auf harte Prüfung von `version.json` ausrichten.
- [x] `version`-Feld als Pflicht prüfen.
- [x] Bei invalidem JSON klar scheitern.
- [x] `REQUIRE_FRONTEND`-Override validieren.
- [x] Leeren oder ungültigen `REQUIRE_FRONTEND` fail fast behandeln.
- [x] Doku zur Override-Semantik ergänzen.

**Stop-Kriterium:** `weltgewebe-up` behandelt `version.json` und `REQUIRE_FRONTEND` deterministisch und explizit.

### Phase D — Testharness bereinigen

- [x] `scripts/tests/test_verify_deployment.sh` nur minimal für 22a–22e erweitern.
- [x] Keinen generischen Monster-Mock bauen.
- [x] Keine Hilfsskripte außerhalb der kanonischen Testdatei.
- [x] Keine Artefakte im Diff.
- [x] 22a–22e sauber durchlaufen lassen.

**Stop-Kriterium:** Die Vertragsmatrix ist vollständig und stabil.

### Phase E — UI-Diagnose (bereits implementiert)

Status: implementiert

Referenzen:

- `apps/web/src/lib/components/VersionDiagnostics.svelte`
- `apps/web/tests/version-diagnostics.spec.ts`

- [x] `VersionDiagnostics.svelte` integriert
- [x] Build-Version in UI sichtbar
- [x] Optional `release` anzeigen, sofern im Payload vorhanden
- [x] `built_at` nur bei gültigem Datum anzeigen
- [x] Fallback "Version unbekannt"
- [x] Playwright Tests vorhanden

**Stop-Kriterium (bereits erfüllt):** Browser A und Browser B können ihre ausgelieferte Build-ID direkt vergleichen.

### Phase F — Kontrollierte Selbstaktualisierung (PR 3)

- [x] Lokale Build-ID im Client vorhalten.
- [x] Frischen Server-Stand via `version.json` holen.
- [x] Vergleich bei App-Start.
- [x] Vergleich bei `visibilitychange`.
- [x] Vergleich bei `pageshow` / bfcache.
- [x] Hinweis bei neuer Version anzeigen.
- [x] Manueller Reload-Button.
- [x] Tests für same-build / changed-build / fetch-fail.

**Stop-Kriterium:** Neue Deploys werden erkannt, aber nicht magisch erzwungen.

### Phase G — Optionale Vertiefung

- [x] Diagnose-Seite `/build` prüfen.
  - _Umgesetzt: `apps/web/src/routes/build/+page.svelte` zeigt den lokal gebündelten Build-Stand und vergleicht ihn live gegen `/_app/version.json`. Tests in `apps/web/tests/build-page.spec.ts` decken Sync-, Mismatch-, Unreachable- und Invalid-Pfad sowie den Refresh-Button ab._
- [~] Optional `X-Weltgewebe-Build` Header.
  - _Teilweise: Alle Caddyfiles (`Caddyfile`, `Caddyfile.dev`, `Caddyfile.heim`, `Caddyfile.prod`) enthalten den `header`-Eintrag `X-Weltgewebe-Build "{$WELTGEWEBE_BUILD}"`. Die Deploy-Pipeline setzt `WELTGEWEBE_BUILD` noch nicht — das ist ein ausstehender Deploy-Schritt. Ein Laufzeittest (curl) zur Verifikation des tatsächlichen Header-Werts steht ebenfalls aus._
- [x] Optional Support-/Debug-Ansicht.
  - _Umgesetzt: Die `/build`-Route dient zugleich als Support-/Debug-Ansicht und ist bewusst über `<meta name="robots" content="noindex">` von Suchmaschinen ausgeschlossen._
- [ ] Erst danach über Service Worker überhaupt reden.

## 11. Empfohlene PR-Struktur

### PR 1 — Versionierungsfundament

- `generate-version.js`
- Build-Integration
- `version.json`
- Caddy `no-store`
- `weltgewebe-up` Verify
- 22a–22e Tests
- Doku

### PR 2 — UI-Diagnose konsolidieren (bereits umgesetzt)

Status: bereits implementiert

- `VersionDiagnostics.svelte` implementiert
- Integration in Settings/Diagnosebereich
- Anzeige von `version` (Build-ID) im UI
- Playwright-Tests vorhanden

### PR 3 — Browser-Selbstaktualisierung

- Build-Vergleich
- Lifecycle-Hooks
- Banner/Reload
- Tests

### PR 4 — Optional Diagnoseausbau (teilweise umgesetzt)

Status: `/build`-Route und Support-Ansicht umgesetzt; `X-Weltgewebe-Build` Header vorbereitet, aber Deploy-Pipeline-Integration ausstehend.

- [x] `/build`-Route in `apps/web/src/routes/build/+page.svelte`
- [~] `X-Weltgewebe-Build` Header in allen Caddyfiles (env: `WELTGEWEBE_BUILD`) — Caddy-Einträge vorhanden, Deploy setzt Variable noch nicht
- [x] Support-Hilfen: `/build` zeigt lokal gebündelten Stand, Server-Stand und Abgleich
