---
id: versionierungs-statusgrundlage
title: "commonThing – Versionierungs-Statusgrundlage"
doc_type: blueprint
status: active
summary: "Belastbare Arbeitsgrundlage und Ist-Stand-Dokumentation für alle Folgeschritte zur commonThing-Versionierung."
relations:
  - type: relates_to
    target: docs/blueprints/versionierungs-blaupause.md
---

# commonThing – Versionierungs-Statusgrundlage

## 1. Ziel und Zweck

Dieses Dokument dient als belastbare, repo-belegte Ist-Stand-Analyse der commonThing-Deploy-Versionierung. Es beendet den Zyklus aus isolierten Patches und unkoordinierten Reparaturen, indem es eine präzise Bestandsaufnahme liefert. Es ist die kanonische Arbeitsgrundlage für die Einordnung und Nachvollziehbarkeit der inzwischen umgesetzten Versionierungsverträge (Cache-Guards, Caddy-Header, Testsemantik).

## 2. Repo-belegter Ist-Stand

### 2.1 Build / version.json

- **Erzeugung:** `apps/web/scripts/generate-version.js` erzeugt die Datei `build/_app/version.json`.
- **Schema:** Das Skript schreibt ein JSON mit den Feldern `version` (Short SHA oder Commit, Fallback "unknown"), `build_id` (Short SHA + Timestamp, Fallback `unknown-<epochMs>` (Millisekunden seit Unix-Epoch)), `built_at` (ISO Timestamp) und optional `commit`.
- **Kanonisches Feld:** Im Skript wird `version` explizit deklariert: `const version = shortSha || commit || "unknown"; // Canonical artifact ID (deterministic)`.

### 2.2 Caddy / Cache

- **Caddyfile:** `infra/caddy/Caddyfile.heim` trennt zwischen `/_app/immutable/*` (Header `public, max-age=31536000, immutable`) und dem Rest via `try_files` (Header `no-cache, must-revalidate`).
- **Caddy-Regel:** Es existiert ein separater Block für `/_app/version.json` mit dem geforderten `no-store` Header.

### 2.3 weltgewebe-up / Guards

- **Frontend Guard:** `scripts/weltgewebe-up` prüft harte Cache-Regeln für HTML (`no-cache, must-revalidate`) und Immutable Assets (`max-age=31536000, immutable`).
- **version.json Guard:** Die Überprüfung von `/_app/version.json` in `weltgewebe-up` ist ein harter Contract. Fehler bei der JSON-Validierung oder Erreichbarkeit führen zum Deployment-Abbruch (`exit 1`).
- **REQUIRE_FRONTEND:** Wenn nicht gesetzt, gilt das Default-Verhalten. Wenn gesetzt, sind ausschließlich die Werte `0` oder `1` erlaubt. Jeder andere Wert führt zu sofortigem Abbruch (`exit 1`) vor Ausführung der Guards.

### 2.4 UI-Diagnose

- **Implementierung:** Die heutige Vollansicht liegt unter `apps/web/src/routes/build/+page.svelte`; der weiterhin passende Browservertrag liegt in `apps/web/tests/version-diagnostics.spec.ts`. Die frühere Komponente `VersionDiagnostics.svelte` ist nicht mehr Teil des Repositorys.
- **Begriffsverwendung:** Die Build-Diagnose verlangt für den Serverstand ein nichtleeres `version`-Feld und vergleicht es mit der lokal eingebetteten Build-Version. `build_id`, `built_at`, `commit` und `release` werden nur als zusätzlicher Kontext angezeigt, wenn sie vorhanden sind.
- **Cache:** Die Vollansicht lädt `/_app/version.json` explizit mit `cache: "no-store"`.

### 2.5 Tests

- **Test-Skript:** `scripts/tests/test_verify_deployment.sh` enthält Test 22 für "Cache Guards Logic".
- **Vorhandene Tests:** Das Skript testet den Cache-Guard aktuell über Sub-Tests `22a` (HTML-Cache) bis `22c` (Positiv-Pfad).
- **Weitere Tests:** Die in der Blaupause (`versionierungs-blaupause.md`) erwähnten Sub-Tests `22d` (`version.json` ohne `no-store`) und `22e` (`version.json` erreichbar, aber ohne brauchbare kanonische Versionsangabe) sind ebenfalls im Code implementiert.

## 3. Kanonische Begriffe

- **version:** Identifiziert den **Artefaktinhalt**. Die deterministische ID (i.d.R. der Git-Commit oder Short-SHA).
- **build_id:** Identifiziert den **konkreten Buildlauf**. Ein volatiler Bezeichner für den CI-Lauf (z.B. `<sha>-<timestamp>`).
- **built_at:** Ist reiner **Kontext, nicht Identität**. ISO-Timestamp des Build-Zeitpunkts.
- **version.json:** Die maschinenlesbare JSON-Diagnosequelle (`/_app/version.json`), die diese drei Metadaten zur Laufzeit verfügbar macht.
- **Cache-Control:** HTTP-Header zur Steuerung des Caching-Verhaltens.
- **REQUIRE_FRONTEND:** Eine Umgebungsvariable, die explizit steuert, ob Frontend-Guards durchgesetzt werden. Gültige Werte sind als `0` (deaktiviert) oder `1` (erzwungen) definiert.

> **Wichtig:** Diese scharfe Trennung von Inhalt (`version`) und Kontext (`build_id`, `built_at`) schützt vor semantischem Rückfall in zukünftigen Skripten.

## 4. Vertragsmatrix

| Artefakt / Feld | Produzent | Konsument | Erwartete Semantik | Aktueller Status | Drift-Risiko |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `version.json.version` | `generate-version.js` | UI, `weltgewebe-up` | Kanonische ID | Implementiert & konsistent | Niedrig |
| `version.json.build_id` | `generate-version.js` | UI, `weltgewebe-up` | Volatile CI Run ID | Implementiert & konsistent | Niedrig |
| Caddy Cache-Control für `/_app/version.json` | `Caddyfile.heim` | Browser, `weltgewebe-up` | `no-store` | Implementiert & konsistent | Niedrig |
| `weltgewebe-up` Frontend Guard für `version.json` | `weltgewebe-up` | CI/CD | Harter Fehler bei Fehlen oder invalidem JSON | Implementiert & konsistent | Niedrig |
| UI-Diagnoseanzeige | `apps/web/src/routes/build/+page.svelte` | Benutzer | Vergleicht lokale und serverseitige `version`; zeigt weitere Buildfelder als Kontext | Implementiert (`no-store`) | Niedrig |
| Deploy-Tests (22a, 22b, 22c) | `test_verify_deployment.sh` | CI/CD | Absicherung der Cache-Header | Implementiert | Mittel |
| Deploy-Tests (22d, 22e) | `test_verify_deployment.sh` | CI/CD | Absicherung von `version.json` Constraints | Implementiert | Mittel |

## 5. Testklassifikation

Die Deploy-Verify-Tests müssen klar in zwei semantische Gruppen getrennt werden:

- **Positivtest:** Zwingender Exit 0 erwartet. Bestätigt, dass der Guard im Erfolgsfall passiert wird.
- **Negativtest:** Zwingender Exit != 0 erwartet. Bestätigt, dass der Guard bei Verletzung der Constraints hart fehlschlägt.

- **22a: Missing HTML Cache Header**
  - *Typ:* Negativtest (Erwarteter Exit != 0)
  - *Verhalten:* Simuliert fehlende `no-cache, must-revalidate` Header für `/map`. Schlägt korrekt fehl.
- **22b: Missing Asset Cache Header**
  - *Typ:* Negativtest (Erwarteter Exit != 0)
  - *Verhalten:* Simuliert fehlende `immutable` Header für Assets. Schlägt korrekt fehl.
- **22c: Valid Cache Headers (Positive Path)**
  - *Typ:* Positivtest (Erwarteter Exit 0)
  - *Verhalten:* Simuliert korrekte Header für HTML und Assets sowie eine gültige `version.json`-Antwort. Geht erfolgreich durch.
- **22d: version.json ohne no-store**
  - *Typ:* Negativtest (Erwarteter Exit != 0)
  - *Status:* Implementiert in `test_verify_deployment.sh`.
- **22e: version.json ohne brauchbare kanonische Versionsangabe**
  - *Typ:* Negativtest (Erwarteter Exit != 0)
  - *Status:* Implementiert in `test_verify_deployment.sh`.

## 6. Verbleibende Klarstellungen

- Aktuell bestehen keine offenen Widersprüche mehr bzgl. version.json-Caching und -Guarding.
- **Klarstellung REQUIRE_FRONTEND:** REQUIRE_FRONTEND ist technisch strikt validiert (0|1, sonst `exit 1`), dient aber konzeptionell primär als Override-/Testhebel.

### Beantwortung der Kernfragen

1. **Ist version aktuell kanonisch oder build_id?**
   `version` ist kanonisch. `generate-version.js` und die Build-Diagnose unter `apps/web/src/routes/build/+page.svelte` behandeln `version` konsistent als die primäre Artefakt-ID.
2. **Ist das aktuelle Schema wirklich konsistent zwischen den Dateien?**
   Ja, das Schema (Fokus auf `version`, sekundär `build_id`/`built_at`) ist zwischen Generator, UI und Blaupause konsistent. Der Infrastruktur-Code (`Caddyfile.heim`, `weltgewebe-up`) setzt die daraus resultierenden *Verträge* (`no-store`, hartes Failen) durch.
3. **Ist REQUIRE_FRONTEND heute eine saubere Override-Schnittstelle oder nur pragmatischer Testhebel?**
   Die Validierung ist technisch strikt umgesetzt, konzeptionell aber eher ein pragmatischer Override-Mechanismus.
4. **Welche Tests rund um 22c/22d/22e sind logisch korrekt benannt und welche nicht?**
   `22c` ist korrekt als Positivtest. `22d` und `22e` sind korrekt als Negativtests implementiert.
5. **Ist die UI-Diagnose inhaltlich schon „fertig genug“, sodass PR 2 im Wesentlichen als erledigt gelten kann?**
   Ja. Die frühere Einzelkomponente wurde durch die vollständige `/build`-Diagnoseseite ersetzt; `apps/web/tests/version-diagnostics.spec.ts` prüft den Einstieg und den Live-Versionsreadback.

## 7. Abschlussstatus

### Fokus-PR: Härtung des version.json Vertrags in Infrastruktur und Tests (Abgeschlossen)

Die Härtung des version.json Vertrags ist in Infrastruktur und Tests abgeschlossen. `no-store` Header werden gesetzt, weltgewebe-up prüft hart und die Tests 22d/22e sind implementiert.
