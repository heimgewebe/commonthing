---
id: deploy.schauwerk-editor-frontdoor
title: Schauwerk Schaubild Frontdoor
doc_type: runbook
status: active
summary: Delivery contract for the separately versioned native Schaubild runtime at the shared Commonthing HTTPS edge.
relations:
  - type: relates_to
    target: infra/caddy/Caddyfile.vps
  - type: relates_to
    target: infra/compose/compose.vps.override.yml
  - type: relates_to
    target: infra/schauwerk-editor/release-lock.json
  - type: relates_to
    target: docs/deploy/vps.md
---
# Schauwerk Schaubild frontdoor

The public entry point is `https://commonthing.net/schaubild/`. Commonthing owns
only the public HTTPS edge and the consumer binding. Renderer and product runtime
remain owned and versioned by `heimgewebe/schauwerk`.

## Runtime boundary

Production runs a private Compose service named `schaubild` from the exact OCI
reference declared in `infra/schauwerk-editor/release-lock.json`:

```text
ghcr.io/heimgewebe/schauwerk-schaubild@sha256:<digest>
```

The lock is `weltgewebe-schauwerk-runtime-lock.v1` and binds exactly:

- `source_repository = heimgewebe/schauwerk`;
- the full 40-hex Schauwerk source commit;
- `image_repository = ghcr.io/heimgewebe/schauwerk-schaubild`;
- one immutable `sha256:` image digest;
- `public_base_path = /schaubild`.

Mutable tags such as `latest` are not deployment authority.

The sidecar has no published host port. It runs read-only, drops all Linux
capabilities, enables `no-new-privileges`, and receives only a bounded 64 MiB
`/tmp` tmpfs. Its server binds the Compose network only in explicit
`--trusted-reverse-proxy` mode, admits proxy peers only from
`SCHAUWERK_SCHAUBILD_TRUSTED_PROXY_CIDR` (default `172.16.0.0/12` for the
Docker bridge contract), and is configured with `--public-base-path /schaubild`.

Caddy owns the public boundary and starts independently of Schaubild health. A
missing or unhealthy renderer may therefore degrade only `/schaubild/*` with an
upstream error while the main site, API and basemap remain available.

- `/schaubild` redirects to `/schaubild/`;
- `handle_path /schaubild/*` strips the public prefix and proxies to
  `schaubild:8765`;
- the upstream `Host` is set to the runtime's admitted loopback-style host
  contract;
- any client-supplied `Forwarded` chain is removed and `X-Forwarded-For` is
  overwritten with exactly Caddy's observed `{remote_host}`; the runtime trusts
  this identity only when the direct peer belongs to the configured proxy CIDR;
- Schaubild responses use the runtime's `Cache-Control: no-store` and security
  headers;
- the outer Schaubild shell and all non-native-viewer Commonthing documents remain
  non-frameable with `frame-ancestors 'none'` and `X-Frame-Options: DENY`;
- generated native viewer resources under `/schaubild/native/*` are the one
  path-scoped framing exception: the shell embeds the viewer in a same-origin
  iframe, so Caddy replaces the upstream framing headers with
  `frame-ancestors 'self'` and `X-Frame-Options: SAMEORIGIN`;
- the path-specific CSP permits `connect-src 'self'` for the native render API
  and permits `frame-src 'self' https://embed.diagrams.net`: same-origin for the
  native viewer, plus the remote origin for explicit legacy
  Mermaid/JSON-Canvas/draw.io compatibility.

No renderer implementation is copied into Commonthing.

## Admission

Before the first mutating full VPS Compose action, `scripts/weltgewebe-up`
validates the repository-owned runtime lock and exports its exact digest reference
as `SCHAUWERK_SCHAUBILD_IMAGE`. The authoritative Compose render must then show
exactly that image on the `schaubild` service and `pull_policy: missing`. The
digest remains immutable deployment authority, while an already cached exact
digest can be reused when GHCR is temporarily unavailable. If the digest is not
cached, Compose still pulls that exact digest before the sidecar can start.

A missing, malformed, mutable, wrong-repository or wrong-base-path lock fails
closed. Bounded `api` and `migration` deployment scopes do not own the public
Schaubild cutover.

## Post-deploy readback

A successful full deployment is not complete until all of these are observed:

1. the running `weltgewebe` Compose service `schaubild` is unique and healthy;
2. its configured image equals the exact reviewed `image@sha256:digest`;
3. `/schaubild/` returns 200 through the freshly deployed Caddy listener;
4. `/schaubild/manifest.json` reports
   `schauwerk-standalone-editor-manifest.v2`;
5. `editor_engine == schauwerk-native-diagram-v1`;
6. `cutover_status == native-primary-with-legacy-compatibility`;
7. the native renderer metadata reports
   `api_path == /schaubild/api/native-viewer`;
8. the public native POST/viewer path remains same-origin and bounded;
9. the generated native viewer response exposes exactly one same-origin framing
   contract (`frame-ancestors 'self'` plus `X-Frame-Options: SAMEORIGIN`) so
   the product shell can display the native result without widening framing for
   the rest of Commonthing.

The production reconciler includes the exact OCI image identity plus public native
manifest semantics in its same-commit no-op decision. A matching Commonthing
frontend/API commit is insufficient when Schaubild still runs another digest.

## Manual readback

```bash
curl -sS -o /dev/null -w '%{http_code} %{redirect_url}\n' \
  https://commonthing.net/schaubild
curl -fsSI https://commonthing.net/schaubild/
curl -fsS https://commonthing.net/schaubild/manifest.json
```

A browser readback must additionally confirm that canonical
`schauwerk-representation-input.v1` input reaches the native viewer rather than
the diagrams.net compatibility surface.

## Rollback

Rollback uses another reviewed immutable Schauwerk digest. Update the runtime lock
to the verified previous source commit and `image_digest`, then run the normal
full exact-revision Commonthing deployment. A locally cached exact rollback digest
remains usable during a temporary registry outage; an uncached digest still
requires the registry. Do not retag an existing mutable name and do not directly
mutate the running container.

If the shared-edge change itself causes a regression, restore the prior Commonthing
revision through the existing exact-revision production path. In either case,
repeat container-identity, manifest, CSP and public browser readback before calling
the rollback verified.
