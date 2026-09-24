---
id: deployment_governance
title: Deployment Governance
doc_type: reference
status: active
summary: Port-Ownership-Regeln und Health-Check-Strategie für Heimserver-Deployments.
relations:
  - type: relates_to
    target: docs/deployment.md
  - type: relates_to
    target: docs/deploy/README.md
---
# Deployment Governance: Port Ownership

To ensure stable operation on home servers alongside other services, `weltgewebe-up` enforces strict port ownership rules.

## Reserved Ports

| Port | Owner | Description |
| :--- | :--- | :--- |
| **8081** | **Pi-hole (FTL)** | Strictly reserved. commonThing must NOT use this port. |
| **8080** | **commonThing API** | Internal container port. Default unpublished. |
| **80/443** | **Edge (Caddy)** | Reverse Proxy handling public traffic and TLS. |

## Health Check Strategy

`weltgewebe-up` prioritizes Docker Native Health first; otherwise, it falls back to HTTP Health via explicit
`HEALTH_URL` or host port mapping to determine service health without making assumptions about host environments:

1. **Docker Native Health (Priority):**
   * Default. Uses `docker inspect` to check if a `HEALTHCHECK` is defined and validates its internal health status.
   * Prevents erroneous port probing and false alarms.

2. **HTTP Health (Fallback):**
   * **Explicit URL (`HEALTH_URL`):**
     Used if a native check doesn't exist and `HEALTH_URL` is set in the environment.
   * **Host Port Mapping:**
     Used as the final HTTP fallback if the API container has a valid, non-zero port published to the host.
     Example: `127.0.0.1:32768` -> `8080/tcp`.

## Configuration

| Variable | Default | Description |
| :--- | :--- | :--- |
| `API_INTERNAL_PORT` | `8080` | Internal port of the API service (if modified). |
| `HEALTH_URL` | *(unset)* | Full URL to force a specific health check endpoint. |
