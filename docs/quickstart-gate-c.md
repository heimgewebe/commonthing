---
id: quickstart-gate-c
title: Quickstart Gate C
doc_type: reference
status: active
summary: Schnellstart-Anleitung für den Gate-C-Dev-Stack.
relations:
  - type: relates_to
    target: docs/process/fahrplan.md
  - type: relates_to
    target: docs/dev/codespaces.md
---
# Quickstart · Gate C (Dev-Stack)

```bash
# 1. Env-Datei erstellen (falls noch nicht geschehen)
cp .env.example .env

# 2. Stack starten
make up

# 3. URLs prüfen (nur lokale Entwicklung)
#    - Frontend/Hydration: http://localhost:5173
#      (im Compose-Stack nicht für API-gestützte UI-/E2E-Nachweise)
#    - Devproxy, kein UI-Einstieg: http://localhost:8081
#    - API Live: http://localhost:8081/api/health/live
#    - API Version: http://localhost:8081/api/version

# 4. Logs verfolgen (optional)
make logs

# 5. Stack anhalten
make down
```

## Hinweise

- Port `8081` ist im Dev-Stack der Proxy-Einstieg für `/api/*` und `/basemap/*`, aber kein unterstützter Browser-/UI-Einstieg.
  (Im Heimserver-Produktionsbetrieb ist der Port 8081 reserviert und commonThing publiziert keinen eigenen Host-Port.)
- Das Frontend ist direkt auf Port `5173` exponiert. Im Compose-Stack zeigt Vites `/api`-Proxy jedoch auf `127.0.0.1:8080` im Webcontainer; deshalb ist `:5173` dort nur für Frontend-/Hydrationsnachweise geeignet, nicht für API-gestützte UI-/E2E-Flows.
- Frontend nutzt `PUBLIC_API_BASE=/api` (siehe `apps/web/.env.development`).
- Die kanonische Abgrenzung steht in `docs/deploy/dev-entrypoints.md`.
- Compose-Profil `dev` schützt vor Verwechslungen mit späteren prod-Stacks.
- `make smoke` triggert den GitHub-Workflow `compose-smoke` für einen E2E-Boot-Test.
- CSP ist im Dev gelockert; für externe Tiles Domains ergänzen.
