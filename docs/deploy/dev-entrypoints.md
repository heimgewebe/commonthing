---
id: deploy.dev-entrypoints
title: Lokale Entwicklungs-Einstiegspunkte
doc_type: reference
status: active
summary: Kanonische lokale URLs für UI und Proxy-Pfade im Entwicklungsprofil.
relations:
  - type: relates_to
    target: docs/deploy/README.md
  - type: relates_to
    target: docs/deployment.md
---

# Lokale Entwicklungs-Einstiegspunkte

Für das lokale Entwicklungsprofil gelten zwei getrennte Einstiegspunkte:

- **Interaktive Weboberfläche:** `http://localhost:5173`
- **Entwicklungsproxy:** `http://localhost:8081`, ausschließlich für die vorhandenen Proxy-Pfade `/api/*` und `/basemap/*`

Die über Port 8081 weitergereichte Frontend-Seite ist kein unterstützter Browser-Einstieg. Der aktuelle Proxy-Vertrag lässt Vites Bootstrap dort bewusst nicht zu; deshalb kann die HTML-Antwort erscheinen, ohne dass die Anwendung interaktiv startet.

Für Browser-, UI- und Frontend-Nachweise ist daher Port 5173 zu verwenden. Port 8081 ist nur dann der richtige Prüfpunkt, wenn ausdrücklich das Proxy-Verhalten selbst geprüft wird.

Diese Trennung beschreibt den bestehenden Zustand; sie ändert weder Produktionspfade noch die Sicherheitsrichtlinien.
