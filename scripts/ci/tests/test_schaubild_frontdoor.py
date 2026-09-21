"""Contract for the separately versioned Schaubild native-runtime frontdoor."""

from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile
import unittest


class SchaubildFrontdoorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = pathlib.Path(__file__).resolve().parents[3]
        self.caddy = (self.repo / "infra/caddy/Caddyfile.vps").read_text(encoding="utf-8")
        self.compose = (self.repo / "infra/compose/compose.vps.override.yml").read_text(
            encoding="utf-8"
        )
        self.deploy = (self.repo / "scripts/weltgewebe-up").read_text(encoding="utf-8")

    def test_editor_is_a_digest_pinned_private_sidecar(self) -> None:
        self.assertIn(
            "image: ${SCHAUWERK_SCHAUBILD_IMAGE:?SCHAUWERK_SCHAUBILD_IMAGE must be set}",
            self.compose,
        )
        self.assertIn("pull_policy: missing", self.compose)
        self.assertNotIn("pull_policy: always", self.compose)
        self.assertIn("read_only: true", self.compose)
        self.assertIn("/tmp:rw,noexec,nosuid,size=64m", self.compose)
        self.assertIn("--trusted-reverse-proxy", self.compose)
        self.assertIn("--trusted-proxy-source-cidr", self.compose)
        self.assertIn(
            "${SCHAUWERK_SCHAUBILD_TRUSTED_PROXY_CIDR:-172.16.0.0/12}",
            self.compose,
        )
        self.assertIn("--public-base-path", self.compose)
        self.assertIn("- /schaubild", self.compose)
        self.assertIn("cap_drop:", self.compose)
        self.assertIn("- ALL", self.compose)
        self.assertNotIn("/srv/schauwerk-editor-release", self.compose)
        self.assertNotIn("/srv/schauwerk-editor-release", self.caddy)

    def test_edge_startup_does_not_hard_depend_on_schaubild_health(self) -> None:
        caddy_service = self.compose.split("\n  caddy:\n", 1)[1]
        self.assertNotIn("depends_on:", caddy_service)
        self.assertIn('"schaubild" in dependencies', self.deploy)
        self.assertIn(
            "Caddy must remain available when the Schaubild runtime is degraded",
            self.deploy,
        )

    def test_full_deploy_requires_cache_resilient_digest_pull_policy(self) -> None:
        self.assertIn('service.get("pull_policy") != "missing"', self.deploy)
        self.assertIn(
            "services.schaubild must reuse the exact cached digest when present",
            self.deploy,
        )

    def test_editor_route_precedes_generic_routes_and_proxies_private_runtime(self) -> None:
        root_redirect = "@schauwerkRoot path /schaubild"
        route = "handle_path /schaubild/*"
        trailing = "@trailingSlash path_regexp ^/.+/$"
        generic = "# Serve only real files or explicitly prerendered Svelte routes."
        for needle in (root_redirect, route, trailing, generic):
            self.assertIn(needle, self.caddy)
        self.assertLess(self.caddy.index(root_redirect), self.caddy.index(trailing))
        self.assertLess(self.caddy.index(route), self.caddy.index(trailing))
        self.assertLess(self.caddy.index(route), self.caddy.index(generic))
        self.assertIn("redir * /schaubild/ 308", self.caddy)
        self.assertIn("reverse_proxy schaubild:8765", self.caddy)
        self.assertIn("header_up Host 127.0.0.1:8765", self.caddy)
        self.assertIn("header_up -Forwarded", self.caddy)
        self.assertIn("header_up X-Forwarded-For {remote_host}", self.caddy)

    def test_postflight_accepts_exact_32_hex_native_capability_token(self) -> None:
        script = (self.repo / "scripts/weltgewebe-up").read_text(encoding="utf-8")
        self.assertIn(
            r"/schaubild/native/[0-9a-f]{32}/index\.html",
            script,
        )
        self.assertNotIn(
            r"/schaubild/native/[0-9a-f]{64}/index\.html",
            script,
        )

    def _schaubild_health_wait_function(self) -> str:
        start = self.deploy.index("wait_for_schaubild_runtime_health() {")
        end = self.deploy.index(
            "\n}\n\nrestore_scoped_migration_mode()", start
        ) + 2
        return self.deploy[start:end]

    def test_full_deploy_waits_for_transient_schaubild_health(self) -> None:
        function = self._schaubild_health_wait_function()
        with tempfile.TemporaryDirectory() as temporary:
            counter = pathlib.Path(temporary) / "health-counter"
            environment = os.environ.copy()
            environment["HEALTH_COUNTER"] = str(counter)
            environment["WELTGEWEBE_SCHAUWERK_HEALTH_TIMEOUT_SECONDS"] = "3"
            harness = f"""\
set -euo pipefail
{function}
sleep() {{ :; }}
docker() {{
  count=0
  if [[ -e "$HEALTH_COUNTER" ]]; then
    count="$(cat "$HEALTH_COUNTER")"
  fi
  count=$((count + 1))
  printf '%s\\n' "$count" > "$HEALTH_COUNTER"
  if ((count == 1)); then
    printf 'starting\\n'
  else
    printf 'healthy\\n'
  fi
}}
wait_for_schaubild_runtime_health schaubild-test
"""
            result = subprocess.run(
                ["bash", "-c", harness],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(counter.read_text(encoding="utf-8").strip(), "2")
        self.assertIn(
            'wait_for_schaubild_runtime_health "$SCHAUWERK_RUNTIME_CONTAINER_ID"',
            self.deploy,
        )

    def test_full_deploy_rejects_nontransient_schaubild_health(self) -> None:
        function = self._schaubild_health_wait_function()
        harness = f"""\
set -euo pipefail
{function}
sleep() {{ exit 98; }}
docker() {{ printf 'unhealthy\\n'; }}
wait_for_schaubild_runtime_health schaubild-test
"""
        result = subprocess.run(
            ["bash", "-c", harness],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("health is 'unhealthy'", result.stderr)

    def test_editor_csp_allows_native_same_origin_api_and_legacy_frame_only(self) -> None:
        expected = (
            "header @schauwerkResponse >Content-Security-Policy \"default-src 'self'; "
            "script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
            "frame-src https://embed.diagrams.net; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none';\""
        )
        self.assertIn(expected, self.caddy)
        self.assertIn(
            "@schauwerkResponse {\n\t\tpath /schaubild /schaubild/*\n"
            "\t\tnot path /schaubild/native/*\n\t}",
            self.caddy,
        )
        self.assertIn(
            "not path /api/* /health/* /schaubild /schaubild/*",
            self.caddy,
        )
        self.assertEqual(self.caddy.count("frame-src https://embed.diagrams.net"), 1)

    def test_native_viewer_is_frameable_only_by_same_origin_schaubild(self) -> None:
        native_csp = (
            "header @schauwerkNativeResponse >Content-Security-Policy \"default-src 'self'; "
            "script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
            "frame-src 'none'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'self';\""
        )
        self.assertIn(
            "@schauwerkNativeResponse path /schaubild/native/*",
            self.caddy,
        )
        self.assertIn(native_csp, self.caddy)
        self.assertIn(
            'header @schauwerkNativeResponse >X-Frame-Options "SAMEORIGIN"',
            self.caddy,
        )
        self.assertIn(
            "@frameDenied {\n\t\tnot path /schaubild/native/*\n\t}",
            self.caddy,
        )
        self.assertIn('header @frameDenied X-Frame-Options "DENY"', self.caddy)

    def test_native_postflight_verifies_browser_embedding_headers(self) -> None:
        self.assertIn(
            'SCHAUWERK_NATIVE_EXPECTED_FRAME_ANCESTORS="frame-ancestors \'self\'"',
            self.deploy,
        )
        self.assertIn(
            'SCHAUWERK_NATIVE_EXPECTED_X_FRAME_OPTIONS="SAMEORIGIN"',
            self.deploy,
        )
        self.assertIn(
            'SCHAUWERK_NATIVE_VIEWER_HEADERS_OUT="$(mktemp)"',
            self.deploy,
        )
        self.assertIn(
            "native viewer response does not permit same-origin embedding",
            self.deploy,
        )


if __name__ == "__main__":
    unittest.main()
