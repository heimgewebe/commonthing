---
id: deployment-contract
title: Deployment Contract and Preflight Guard
doc_type: guide
status: active
summary: Anleitung und Dokumentation zum Deployment.
last_reviewed: "2026-07-13"
relations:
  - type: relates_to
    target: docs/deploy/README.md
  - type: relates_to
    target: docs/deployment_governance.md
  - type: relates_to
    target: docs/deploy/security.md
  - type: relates_to
    target: docs/runbook.md
---

## Required runtime artifacts

Production deploys assume the following runtime artifacts exist.

Always required:

- `policies/limits.yaml`

Conditionally required (when the frontend is deployed):

- `apps/web/build/index.html`
- `apps/web/build/_app`

## Runtime Contract for Static UI

commonThing UI deployment fundamentally operates on three coupled layers:

1. **Build layer**: `pnpm build` produces the static artifacts in `apps/web/build`.
2. **Container mount layer**: A bind mount structurally exposes these artifacts to the `edge-caddy` container (`/srv/weltgewebe-web`).
3. **Edge serving layer**: Caddy reads and serves these static files to the client.

A successful frontend build does _not_ automatically guarantee a successful deployment unless the container mount layer correctly reflects the newly built files. The complete chain (Build + Container-Mount + Edge-Serving) is necessary for server-side deployment correctness. It is a known issue with Docker bind mounts that they can drift from the host directory state (meaning the container sees an empty or outdated directory despite the host having the latest files).

To enforce correct runtime state, the deploy pipeline includes an active guard. After building the UI, `weltgewebe-up` verifies that the `edge-caddy` container can genuinely read the critical build artifacts (`test -s /srv/weltgewebe-web/index.html && test -d /srv/weltgewebe-web/_app`). If this check fails, the pipeline forces a refresh of the edge deployment stack to restore the `edge-caddy` mount coupling, provided frontend delivery is required for the current deploy run.

> **Note on Verification**: The deployment script infers the need for frontend validation dynamically (e.g. from `ENABLE_CADDY`). For deterministic testing or reproducible diagnostic runs, the environment variable `REQUIRE_FRONTEND` can be explicitly set to override this behavior (only numeric values `1` and `0` are supported, where `1` forces the validation path and `0` explicitly disables it).

### Client-Cache-Kohärenz

Server-side correctness does not intrinsically prevent browsers from rendering stale application states due to aggressive caching. To reduce client divergence and make cache behavior deterministic at the delivery layer, the infrastructure implements distinct caching strategies based on the asset type:

1. **Revalidating Routing (HTML/Root)**: Core HTML entrypoints (e.g. `index.html`, `/map`) strictly use `Cache-Control: no-cache, must-revalidate` to ensure browsers always check for the latest application shell upon load.
2. **Aggressive Caching (Immutable Assets)**: Hashed internal assets located under `/_app/immutable/` are served with `Cache-Control: public, max-age=31536000, immutable`.
3. **Build Diagnosis**: `/_app/version.json` is a hard requirement for deployment and must be served with `Cache-Control: no-store`. It provides a machine-readable build identifier capable of diagnosing client-vs-server build discrepancies.
   - **Client-visible diagnostics**: The technical build identifier is also shown directly in the Settings UI.
   - **Primary use**: Enables immediate comparison of delivered versions across clients (for example, Browser A vs. Browser B).

_Note (Phase C Preparation): Future Evaluation: The current bind-mount model could theoretically be replaced by a dedicated Web-Container architecture to eliminate host-mount drift entirely._

### Basemap Artifact Deployment (Best Effort)

The deployment script (`weltgewebe-up`) attempts to provide every regional PMTiles source required by `map-style/style.json` in the `build/basemap/` directory before stack initialization. Hamburg and Schleswig-Holstein are built and published independently. Each region has a versioned artifact plus stable `.pmtiles` and `.meta.json` aliases. The guard remains best effort, but it reports missing regions separately so one existing region cannot hide another missing one.

#### PMTiles HTTP representation contract

Every `.pmtiles` response under `/local-basemap/` uses the explicit media type `application/octet-stream`. The contract applies to versioned files and stable aliases and is identical for the development Caddyfile, the Heim reference, the VPS Caddyfile, and the isolated Caddy proof. A valid public representation must satisfy all of the following:

- a full or HEAD representation returns HTTP 200, `Content-Type: application/octet-stream`, and `Accept-Ranges: bytes`;
- `Range: bytes=0-15` returns HTTP 206 with the same `Content-Type` and a matching `Content-Range`;
- the full GET and Range response bodies begin with the PMTiles signature; HEAD is used only for the 200/header contract.

`scripts/ops/check_public_live_readiness.py` checks this contract for the stable and versioned Hamburg and Schleswig-Holstein paths after deployment.

#### Bounded deep PMTiles validation

`apps/web/scripts/validate-pmtiles.mjs` is the publication gate for regional archives. It uses the repository's pinned `pmtiles` reader to validate the v3 header and section boundaries, traverse every reachable root and leaf directory, reconcile directory counts with the header, read metadata, and decode a deterministic bounded sample of real MVT payloads. The sampled layers must agree with metadata and cover every source layer used by `map-style/style.json`.

The two regional content-proof jobs run the validator on the exact artifacts they build and upload its JSON receipt. Unit tests also prove fail-closed behavior for a truncated archive, a corrupt root directory, and malformed MVT bytes. The gate is deliberately bounded: it does not scan every tile and does not establish cartographic completeness, OSM semantic correctness, or source freshness.

Example for a locally available artifact:

```bash
cd apps/web
pnpm validate:pmtiles -- \
  --archive hamburg=../../build/basemap/basemap-hamburg-v0.1.0.pmtiles \
  --style ../../map-style/style.json \
  --output /tmp/hamburg-pmtiles-deep-validation.json
```

#### `PUBLIC_BASEMAP_MODE` Contract

To instruct the production frontend to actually consume the locally hosted sovereign basemap artifact (instead of falling back to external remote styles), the `PUBLIC_BASEMAP_MODE` environment variable must be explicitly set during the frontend build step.

- **Name:** `PUBLIC_BASEMAP_MODE`
- **Allowed Values:** `local-sovereign` | `remote-style`
- **Default Behavior:** If unset or invalid, the application falls back to `local-sovereign` in local development/testing contexts, and `remote-style` in production builds.
- **Purpose:** This flag acts as a deployment-time enablement switch. It allows target environments (like a Heimserver) to actively opt into the fully sovereign map architecture without requiring code changes.
- **Note:** Setting this flag enables the _frontend capability_. A fully operational rollout still strictly requires the underlying infrastructure (e.g., Caddy routing for `/local-basemap/` and the physical PMTiles artifact) to be proven and present at runtime.

To locally verify the artifact state:

```bash
find build/basemap -maxdepth 1 \
  \( -name "basemap-hamburg*.pmtiles" -o -name "basemap-schleswig-holstein*.pmtiles" \) -print
```

## Preflight guard

`scripts/weltgewebe-up` runs `scripts/preflight/runtime_contract.sh` before `docker compose up`.

The guard validates required artifacts and aborts the deployment early if mandatory runtime contracts are violated.

## Begrenzte API- und Migrationsrollouts

Der normale Aufruf verwendet weiterhin `--deploy-scope full` und darf den gesamten Compose-Stack abgleichen.
Für Änderungen, die nur die API betreffen, existieren zwei engere Wirkungsradien:

```bash
# API neu bauen oder aktualisieren, aber keine Migration anwenden.
./scripts/weltgewebe-up --deploy-scope api

# Migration ausschließlich durch einen API-Neustart anwenden und danach
# automatisch wieder auf verify-applied zurückschalten.
./scripts/weltgewebe-up --deploy-scope migration
```

Beide begrenzten Pfade:

- setzen `DEPLOY_FRONTEND_MODE=off` und `REQUIRE_FRONTEND=0` voraus;
- erzeugen vor jeder Containeränderung einen JSON-Plan unter
  `.ops/deploy-plan-<scope>.json` oder unter dem mit `--deploy-plan-file`
  gewählten Dateinamen innerhalb desselben Zustandsverzeichnisses;
- rufen Compose ausschließlich mit `--no-deps ... api` auf;
- verwenden bewusst kein `--remove-orphans`;
- werden durch die untergeordnete Compose-Wirkungssperre gegen parallele
  Containeränderungen geschützt; der produktive Merge-to-live-Pfad besitzt
  zusätzlich die gemeinsame Lock-Domäne
  `weltgewebe-production-deployment-v1`;
- behandeln `db`, `nats` und `caddy` als geschützte Dienste und brechen ab,
  wenn sich deren Containeridentität während des Laufs ändert;
- verweigern den Start, wenn PostgreSQL oder NATS nicht bereits laufen; der
  API-Scope verlangt auf dem VPS zusätzlich ein laufendes Caddy. Der
  Migration-Scope darf Caddy dagegen bereits fehlend vorfinden, damit er einen
  durch eine ausstehende Migration verursachten Gesamtausfall beheben kann. Ein
  vorhandenes Caddy bleibt in beiden Scopes identitätsgeschützt.

Mit `--plan-only` wird der JSON-Plan erzeugt, ohne einen Container zu verändern.
Der Scope `api` erzwingt `WELTGEWEBE_API_STARTUP_MIGRATIONS=verify-applied`.
Der Scope `migration` startet die API einmalig mit `run`, wartet auf einen gesunden
Zustand und erzeugt anschließend ausschließlich die API erneut mit
`verify-applied`. Schlägt das Migrationsfenster fehl, versucht der Pfad ebenfalls,
den sicheren Prüfmodus wiederherzustellen und meldet den Lauf als fehlgeschlagen.

Der JSON-Plan enthält Ziel- und Schutzdienste, die Containeridentitäten vor dem
Lauf, den Git-Stand, die exakten Compose-Argumente und den erlaubten Wirkungsradius.
Er ist ein Ausführungsbeleg, ersetzt aber weder Backup noch Restore-Proof vor einer
produktiven Migration.

## API-Releaseidentität

Release-Builds der API müssen den vollständigen Git-Commit und dessen RFC3339-
Commitzeitpunkt bereits beim Kompilieren in das Binary einbetten. `weltgewebe-up`
leitet beide Werte aus dem ausgewählten Deploy-Commit ab und überschreibt damit
mögliche veraltete Werte aus Runtime-Dateien. Der Docker-Build verweigert fehlende
oder formal ungültige Werte; ein Release-Binary kann daher nicht mit
`commit: unknown` oder `build_timestamp: unknown` entstehen.

Nach einem produktiven API-Rollout wird die öffentliche Identität mit folgendem
Vertrag geprüft:

```bash
EXPECTED_COMMIT=<voller-commit> \
EXPECTED_BUILD_TIMESTAMP=<git-commitzeit> \
./scripts/ops/verify-api-release-identity.sh
```

Der Readback verlangt HTTP 200, valides JSON, die exakte vollständige Commit-ID,
den exakten deterministischen Zeitstempel und den direkt von der API gelieferten
`X-Weltgewebe-API-Build`-Header mit demselben vollständigen Commit. Der globale
`X-Weltgewebe-Build`-Header bleibt dagegen die Identität des Web-/Caddy-Builds.
Der Paketwert `version` bleibt davon getrennt und bezeichnet weiterhin die
Cargo-Paketversion.

## CSP contract

The production frontend uses SvelteKit's hash-mode CSP. SvelteKit writes a document-level CSP meta tag whose
`script-src` contains the build-generated SHA-256 hashes for the prerendered inline bootstrap scripts.
`script-src 'unsafe-inline'` is forbidden.

The static-app Caddy edge deliberately does **not** add `default-src` or `script-src` to its HTTP CSP. Multiple CSP
policies intersect; an edge policy without the same build-generated hashes would therefore block the legitimate
hash-authorized SvelteKit bootstrap. Directives that must remain edge-owned, including `frame-ancestors`, plus the
non-script resource directives remain in the Caddy header.

### Styles

In every shipped path `style-src` is `'self'` and carries no `'unsafe-inline'`. Unlike `script-src` there is no per-document style policy to
delegate to, so the edge header itself is the closed policy and the preflight rejects an edge CSP whose `style-src` is
missing or permissive.

The frontend therefore ships no inline styles: the `app.html` shell wrapper uses the `.app-root` class from
`apps/web/src/app.css`, the inert polyfill no longer injects a style element at runtime, and every component style
lives in a stylesheet. Svelte 5 needs no exception here — its transitions run through `element.animate()` and its
`style:` directives through `CSSStyleDeclaration.setProperty()`, neither of which CSP governs.

One path is deliberately exempt. `infra/caddy/Caddyfile.dev` fronts the Vite dev server, which injects imported CSS as
runtime `<style>` elements for HMR; those elements are CSP-governed, so the dev proxy keeps `style-src 'self'
'unsafe-inline'`. `infra/compose/compose.core.yml` mounts that file only under the `dev` profile, so it reaches no
deployed host. The exception is declared as `content_security_policy.dev_proxy_style_src` in `policies/security.yml`
rather than left to a file comment, and `scripts/guard/security-headers-guard.sh` now pins the dev file to exactly that
value and keeps `script-src` closed there as well — coverage the file did not have before.

One framework-owned inline style attribute remains outside the repository's control: SvelteKit hardcodes the
visually-hidden styling of its live region `#svelte-announcer` in the client runtime. The browser drops that attribute
under this policy, so `apps/web/src/app.css` re-hides the region with an explicit rule.
`apps/web/tests/csp-inline-styles.spec.ts` loads the app under the real edge policy and asserts that this is the only
CSP violation and that the region stays hidden.

The magic-link confirmation document served by the API is the one place that still emits an inline style element. Its
policy binds that element to its own SHA-256, derived from `MAGIC_LINK_CONFIRM_STYLE` in `apps/api/src/routes/auth.rs`.
The hash is never hardcoded: `scripts/guard/security-headers-guard.sh` re-derives it from that constant and fails
closed when a Caddy contract carries a stale value.

### Preflight

The static preflight `scripts/preflight/csp_contract_static.sh` fails closed before deploy. It recursively validates
**every** compiled HTML artifact, requires exactly one CSP meta policy with `script-src`, rejects `unsafe-inline`,
verifies every inline script body against a matching SHA-256 source expression, and rejects any inline style attribute
or style element. Because non-HTML active documents do not receive a meta CSP, the same guard also rejects scriptable
constructs in static SVG artifacts. This keeps the split edge/document CSP contract explicit without relying on a
running server.

### Caddyfile Source of Truth

In Heimserver environments, the Heimserver's Edge-Caddy acts as the primary reverse proxy and frontdoor, meaning
`infra/caddy/Caddyfile.heim` strictly serves as a repository-internal reference for the expected routing.

It is an architectural invariant that the actively deployed Edge-Caddyfile (e.g., in `/opt/heimgewebe/edge/Caddyfile`)
remains synchronized with the repository's reference proxy routing.
To ensure the CSP contract is valid, `scripts/weltgewebe-up` explicitly
resolves and evaluates the active deployment target file (e.g., the mounted host path) rather than the repository
template, guaranteeing the validation guard tests the exact configuration that governs the running container.

## Postflight Guards & Failure Bundles

After launching the stack, `weltgewebe-up` executes a series of Integration Guards
(verifying DNS, container health, and proxy routes).
If these critical assertions fail, the script fails hard and automatically generates a diagnostic `Failure Bundle`
(symlinked to `/tmp/weltgewebe-deploy-failure`) capturing the precise Docker state, logs,
and curl outputs to aid debugging without relying on manual archaeology.

## Future Work

- [ ] Add missing tests for the deploy-hardening guards introduced with `weltgewebe-up`.
