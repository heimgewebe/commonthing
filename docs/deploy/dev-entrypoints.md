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

- **Frontend-/Hydrations-Einstieg:** `http://localhost:5173`
- **Entwicklungsproxy:** `http://localhost:8081`, ausschließlich für die vorhandenen Proxy-Pfade `/api/*` und `/basemap/*`

Die über Port 8081 weitergereichte Frontend-Seite ist kein unterstützter Browser-Einstieg. Der aktuelle Proxy-Vertrag lässt Vites Bootstrap dort bewusst nicht zu; deshalb kann die HTML-Antwort erscheinen, ohne dass die Anwendung interaktiv startet.

Port 5173 eignet sich im Compose-Dev-Stack für Frontend- und Hydrationsnachweise. Er ist dort jedoch kein gültiger Nachweis für API-gestützte UI- oder E2E-Flows: Vites `/api`-Proxy adressiert `127.0.0.1:8080` innerhalb des Webcontainers, während die API in einem separaten Container läuft. Port 8081 ist der richtige Prüfpunkt für das Proxy-Verhalten selbst und für direkte Aufrufe der vorhandenen `/api/*`- und `/basemap/*`-Routen.

Diese Trennung beschreibt den bestehenden Zustand; sie ändert weder Produktionspfade noch die Sicherheitsrichtlinien.
