---
id: deploy.heim-first-phase0
title: Heim-First Phase 0
doc_type: reference
status: deprecated
summary: Historische Phase-0-Dokumentation des früheren Heim-First-Deployment-Ansatzes; kein aktueller Deploymentvertrag.
relations:
  - type: relates_to
    target: docs/deploy/README.md
  - type: relates_to
    target: docs/deploy/heimserver.deployment.md
---

> [!WARNING]
> Dieses Dokument hält ausschließlich den historischen Phase-0-Stand fest.
> Der frühere Heimserver-Pfad ist retired und besitzt keine aktuelle Owner-
> oder Runtime-Autorität. Der kanonische aktuelle Deployment-Stand steht in
> `docs/deploy/README.md`.

# Heim-first UI (Phase 0) Deployment

This document records the historical deployment changes introduced for the
"Heim-first UI" Phase 0 implementation. It does not define a current deployment
target or authorize a runtime.

## Changes

The following entries describe what Phase 0 introduced at that time; they are
historical implementation notes, not current deployment instructions.

- **Infrastructure**:
  - `infra/caddy/Caddyfile`: Restored to its original state (Dev-Gateway proxying to `web:5173`).
  - `infra/caddy/Caddyfile.dev`: Created as the explicit configuration for the development environment.
  - `infra/caddy/Caddyfile.heim`: Created for the former Heimserver deployment.
    - Served static UI files locally for `weltgewebe.home.arpa` using `tls internal`.
    - Proxied API requests to `api:8080`.
    - Enforced security headers (CSP, X-Frame-Options, Referrer-Policy).
    - It remains a repo-internal historical/reference artifact and does not establish an active Edge owner or runtime.
  - `infra/compose/compose.prod.yml`:
    - Added a volume mount for `apps/web/build` artifacts to the Caddy container.
    - Updated the Caddyfile mount to use `Caddyfile.heim`.
  - `infra/compose/compose.core.yml`:
    - Updated the Caddyfile mount to use `Caddyfile.dev`.

## Historical purpose

At the time, these changes made `weltgewebe.home.arpa` the authoritative UI
source in the local network and removed the Cloudflare Pages dependency for
local access. That statement is historical and must not be read as current
deployment truth.

Any future local Edge requires a separately bound current owner plus fresh
deployment and runtime evidence. The current canonical production path is
defined only by `docs/deploy/README.md`.

## Verification

This file is retained as historical deployment evidence. It does not satisfy
current deployment, owner, Edge, DNS, TLS, or runtime verification gates.
