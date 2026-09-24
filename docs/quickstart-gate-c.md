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
#    - Frontend: http://localhost:8081
#    - API Live: http://localhost:8081/api/health/live
#    - API Version: http://localhost:8081/api/version

# 4. Logs verfolgen (optional)
make logs

# 5. Stack anhalten
make down
```

## Hinweise

- In der lokalen Entwicklungsumgebung ist der primäre Einstiegspunkt der Proxy auf Port `8081`.
  (Im Heimserver-Produktionsbetrieb ist der Port 8081 reserviert und commonThing publiziert keinen eigenen Host-Port.)
- Das Frontend (Port `5173`) wird automatisch vom Proxy bedient.
- Frontend nutzt `PUBLIC_API_BASE=/api` (siehe `apps/web/.env.development`).
- Compose-Profil `dev` schützt vor Verwechslungen mit späteren prod-Stacks.
- `make smoke` triggert den GitHub-Workflow `compose-smoke` für einen E2E-Boot-Test.
- CSP ist im Dev gelockert; für externe Tiles Domains ergänzen.
