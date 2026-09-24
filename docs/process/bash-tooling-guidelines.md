---
id: process.bash-tooling-guidelines
title: Bash Tooling Guidelines
doc_type: reference
status: active
summary: Richtlinien für Shell-Skripting und Bash-Tooling im Projekt.
relations:
  - type: relates_to
    target: docs/process/README.md
---
# Bash-Tooling-Richtlinien

Diese Richtlinien beschreiben, wie wir Shell-Skripte im commonThing-Projekt
entwickeln, prüfen und ausführen.
Sie kombinieren generelle Best Practices (Formatierung, Checks) mit projektspezifischen Vorgaben
wie Devcontainer-Setup, CLI-Bootstrap und SemVer.

## Ziele

- Einheitliche Formatierung der Skripte.
- Automatisierte Qualitätssicherung mit statischer Analyse.
- Gute Developer Experience für wiederkehrende Aufgaben.
- Projektkontext: sauberes Devcontainer-Setup, klare CLI-Kommandos, reproduzierbare SemVer-Logik.

## Kernwerkzeuge

### shfmt

- Formatierung gemäß POSIX-kompatiblen Standards.
- Nutze `shfmt -w` für automatische Formatierung.
- Setze `shfmt -d` in CI-Checks ein, um Abweichungen aufzuzeigen.

### ShellCheck

- Analysiert Skripte auf Fehler, Portabilität und Stilfragen.
- Lokaler Aufruf: `shellcheck <skript>`.
- In CI-Pipelines verpflichtend.

### Bash Language Server (optional)

- Bietet Editor-Unterstützung (Autocompletion, Inlay-Hints).
- Installierbar via `npm install -g bash-language-server`.
- Im Editor als LSP aktivieren.

## Arbeitsweise

1. Skripte beginnen mit `#!/usr/bin/env bash` und enthalten `set -euo pipefail`.
2. Vor Commit: `shfmt` und `shellcheck` lokal ausführen.
3. Ergebnisse der Checks im Pull Request sichtbar machen.
4. Neue Tools → Dokumentation hier ergänzen.
5. CI-Checks sind verbindlich; Ausnahmen werden dokumentiert.

## Projektspezifische Ergänzungen

### Devcontainer-Setup

- **Bash-Version dokumentieren** (z. B. Hinweis auf `nameref` → Bash ≥4.3).
- **Paketsammlungen per Referenz (`local -n`)** statt Subshell-Kopien.
- **`check`-Ziel ignorieren**, falls versehentlich mitinstalliert.

### CLI-Bootstrap (`wgx`)

- Debug-Ausgabe optional via `WGX_DEBUG=1`.
- Dispatcher validiert Subcommands:
  - Ohne Argument → Usage + `exit 1`.
  - Unbekannte Befehle → Fehlermeldung auf Englisch (für CI-Logs).
  - Usage-Hilfe auf `stderr`.

### SemVer-Caret-Ranges

- `^0.0.x` → nur Patch-Updates erlaubt.
- Major-Sprünge blockiert (`^1.2.3` darf nicht auf `2.0.0` gehen).
- Automatisierte Bats-Tests dokumentieren dieses Verhalten.

## Troubleshooting

- Legacy-Skripte mit `# shellcheck disable=...` markieren und begründen.
- Plattformunterschiede (Linux/macOS) im Skript kommentieren.
- `shfmt`-Fehler → prüfen, ob Tabs statt Spaces verwendet wurden (wir nutzen nur Spaces).

---

Diese Leitlinien werden zum **Gate-C-Übergang** erneut evaluiert und ggf. in
produktive Skripte überführt.
Weitere Infos werden im Fahrplan dokumentiert.
