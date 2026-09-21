from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import unittest

REPO = Path(__file__).resolve().parents[3]
RUNNING_IN_GITHUB_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"
CADDY_BINARY = shutil.which("caddy")
CADDY_DOCKER_IMAGE = "caddy:2.8.4"
MAGIC_LINK_CONFIRM_PATH = "/api/auth/magic-link/consume"
STRICT_POLICY = "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none';"
SCHAUWERK_PATHS = ["/schaubild", "/schaubild/*"]
SCHAUWERK_NATIVE_PATHS = ["/schaubild/native/*"]
SCHAUWERK_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
    "frame-src 'self' https://embed.diagrams.net; connect-src 'self'; object-src 'none'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none';"
)
SCHAUWERK_NATIVE_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
    "frame-src 'none'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'self';"
)

def magic_link_style_hash() -> str:
    """Derive the magic-link style hash from the Rust constant that renders it.

    Hardcoding it here would let the edge policy drift away from the document
    it is supposed to authorize.
    """
    source = (REPO / "apps" / "api" / "src" / "routes" / "auth.rs").read_text(encoding="utf-8")
    matches = re.findall(
        r'const\s+MAGIC_LINK_CONFIRM_STYLE:\s*&str\s*=\s*"([^"\\]*)"\s*;',
        source,
    )
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one MAGIC_LINK_CONFIRM_STYLE literal, found {len(matches)}"
        )
    digest = base64.b64encode(hashlib.sha256(matches[0].encode("utf-8")).digest()).decode("ascii")
    return f"'sha256-{digest}'"


MAGIC_STYLE_HASH = magic_link_style_hash()
MAGIC_POLICY = (
    "default-src 'none'; "
    f"style-src {MAGIC_STYLE_HASH}; "
    "form-action 'self'; base-uri 'none'; frame-ancestors 'none';"
)
CASES = (
    ("infra/caddy/Caddyfile", None, ["/api/*"]),
    ("infra/caddy/Caddyfile.heim", "weltgewebe.home.arpa", ["/api/*"]),
    ("infra/caddy/Caddyfile.vps", "commonthing.net", ["/api/*", "/health/*"]),
)


def _usable_docker() -> str | None:
    """Docker-Pfad nur, wenn auch der Daemon erreichbar ist.

    shutil.which findet die CLI auch dort, wo kein Daemon läuft (Container ohne
    gemounteten Socket, rootless-Setups ohne Session). Die Tests unten fallen
    dann nicht in den Skip, sondern scheitern mit einem Verbindungsfehler, der
    wie eine echte Regression aussieht.
    """
    binary = shutil.which("docker")
    if binary is None:
        return None
    try:
        probe = subprocess.run(
            [binary, "info"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return binary if probe.returncode == 0 else None


DOCKER_BINARY = _usable_docker()


def adapt(relative: str) -> dict:
    if CADDY_BINARY:
        command = [CADDY_BINARY, "adapt", "--config", relative, "--adapter", "caddyfile"]
    elif DOCKER_BINARY:
        command = [
            DOCKER_BINARY,
            "run",
            "--rm",
            "--network",
            "none",
            "-v",
            f"{REPO}:/repo:ro",
            "-w",
            "/repo",
            CADDY_DOCKER_IMAGE,
            "caddy",
            "adapt",
            "--config",
            relative,
            "--adapter",
            "caddyfile",
        ]
    else:
        raise AssertionError("caddy binary or docker is required for semantic adaptation tests")
    result = subprocess.run(
        command,
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout)


def app_routes(config: dict, host: str | None) -> list[dict]:
    servers = config["apps"]["http"]["servers"]
    if host is None:
        return next(iter(servers.values()))["routes"]
    for server in servers.values():
        if ":443" not in server.get("listen", []):
            continue
        for route in server.get("routes", []):
            matches = route.get("match", [])
            if any(host in matcher.get("host", []) for matcher in matches):
                handles = route.get("handle", [])
                if len(handles) == 1 and handles[0].get("handler") == "subroute":
                    return handles[0].get("routes", [])
    raise AssertionError(f"no HTTPS app route for {host}")


def collect_csp(routes: list[dict]) -> list[dict]:
    found: list[dict] = []
    for route in routes:
        for handler in route.get("handle", []):
            if handler.get("handler") == "headers":
                response = handler.get("response", {})
                for value in response.get("set", {}).get("Content-Security-Policy", []):
                    found.append(
                        {
                            "match": route.get("match"),
                            "policy": value,
                            "deferred": response.get("deferred", False),
                        }
                    )
            if handler.get("handler") == "subroute":
                found.extend(collect_csp(handler.get("routes", [])))
    return found


def directive_map(policy: str) -> dict[str, tuple[str, ...]]:
    directives: dict[str, tuple[str, ...]] = {}
    for raw in policy.split(";"):
        raw = raw.strip()
        if not raw:
            continue
        parts = raw.split()
        directives[parts[0]] = tuple(parts[1:])
    return directives


@unittest.skipUnless(
    CADDY_BINARY or DOCKER_BINARY or RUNNING_IN_GITHUB_ACTIONS,
    "caddy binary or docker required for semantic adaptation tests",
)
class StaticAppCaddyAdaptedCspTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if RUNNING_IN_GITHUB_ACTIONS and not (CADDY_BINARY or DOCKER_BINARY):
            raise AssertionError(
                "GitHub Actions requires a caddy binary or reachable Docker daemon "
                "for semantic adaptation tests"
            )

    def test_legacy_map_html_redirect_adapts_on_vps_and_container_edges(self) -> None:
        for relative in ("infra/caddy/Caddyfile.vps", "apps/web/Caddyfile.container"):
            with self.subTest(caddyfile=relative):
                source = (REPO / relative).read_text(encoding="utf-8")
                self.assertEqual(source.count("@legacyMapHtml path /map.html"), 1)
                if relative == "infra/caddy/Caddyfile.vps":
                    self.assertEqual(source.count("handle @legacyMapHtml {"), 1)
                    self.assertEqual(source.count("\n\t\troute {\n\t\t\turi replace /map.html /map"), 1)
                    self.assertNotIn("\n\troute @legacyMapHtml {", source)
                else:
                    self.assertEqual(source.count("route @legacyMapHtml {"), 1)
                self.assertEqual(source.count("uri replace /map.html /map"), 1)
                self.assertEqual(source.count("redir {uri} 308"), 1)
                adapted = json.dumps(adapt(relative), sort_keys=True)
                self.assertIn('"path": ["/map.html"]', adapted)
                self.assertIn('"find": "/map.html"', adapted)
                self.assertIn('"replace": "/map"', adapted)
                self.assertIn('"Location": ["{http.request.uri}"]', adapted)
                self.assertIn('"status_code": 308', adapted)

    def test_vps_schauwerk_redirect_and_prefix_strip_are_semantically_adapted(self) -> None:
        routes = app_routes(adapt("infra/caddy/Caddyfile.vps"), "commonthing.net")

        redirect = next(
            route
            for route in routes
            if route.get("match") == [{"path": ["/schaubild"]}]
        )
        redirect_json = json.dumps(redirect, sort_keys=True)
        self.assertIn('"Location": ["/schaubild/"]', redirect_json)
        self.assertIn('"status_code": 308', redirect_json)

        proxied = next(
            route
            for route in routes
            if route.get("match") == [{"path": ["/schaubild/*"]}]
        )
        proxied_json = json.dumps(proxied, sort_keys=True)
        self.assertIn('"strip_path_prefix": "/schaubild"', proxied_json)
        self.assertIn('"handler": "reverse_proxy"', proxied_json)
        self.assertIn('"dial": "schaubild:8765"', proxied_json)
        self.assertIn('"Host": ["127.0.0.1:8765"]', proxied_json)
        self.assertNotIn('"handler": "file_server"', proxied_json)
        self.assertNotIn("/srv/schauwerk-editor-release", proxied_json)

    def test_vps_legacy_redirect_precedes_catchall_static_handle_after_adapt(self) -> None:
        routes = app_routes(adapt("infra/caddy/Caddyfile.vps"), "commonthing.net")

        legacy_index = next(
            index
            for index, route in enumerate(routes)
            if route.get("match") == [{"path": ["/map.html"]}]
            and '"status_code": 308' in json.dumps(route, sort_keys=True)
        )
        fallback_index = next(
            index
            for index, route in enumerate(routes)
            if not route.get("match")
            and '"file_server"' in json.dumps(route, sort_keys=True)
        )

        self.assertLess(
            legacy_index,
            fallback_index,
            "the catch-all static handle would otherwise serve map.html before the redirect",
        )

    def test_vps_adapts_one_canonical_app_host_and_permanent_legacy_redirects(self) -> None:
        config = adapt("infra/caddy/Caddyfile.vps")
        app_routes(config, "commonthing.net")

        adapted = json.dumps(config, sort_keys=True)
        self.assertEqual(
            adapted.count(
                '"host": ["www.commonthing.net", "weltgewebe.net", "www.weltgewebe.net"]'
            ),
            2,
        )
        self.assertIn(
            '"Location": ["https://commonthing.net{http.request.uri}"]',
            adapted,
        )
        self.assertGreaterEqual(adapted.count('"status_code": 308'), 4)

        legacy_route = next(
            route
            for server in config["apps"]["http"]["servers"].values()
            if ":443" in server.get("listen", [])
            for route in server.get("routes", [])
            if any(
                matcher.get("host")
                == ["www.commonthing.net", "weltgewebe.net", "www.weltgewebe.net"]
                for matcher in route.get("match", [])
            )
        )
        legacy_adapted = json.dumps(legacy_route, sort_keys=True)
        self.assertIn('"status_code": 308', legacy_adapted)
        self.assertNotIn('"file_server"', legacy_adapted)
        self.assertNotIn('"reverse_proxy"', legacy_adapted)

    def test_adapted_app_route_has_exact_matchers_and_canonical_edge_csp(self) -> None:
        for relative, host, protected_paths in CASES:
            with self.subTest(caddyfile=relative):
                policies = collect_csp(app_routes(adapt(relative), host))
                is_vps = relative == "infra/caddy/Caddyfile.vps"
                self.assertEqual(len(policies), 5 if is_vps else 3, policies)

                magic = [item for item in policies if item["policy"] == MAGIC_POLICY]
                strict = [item for item in policies if item["policy"] == STRICT_POLICY]
                schauwerk = [item for item in policies if item["policy"] == SCHAUWERK_POLICY]
                schauwerk_native = [
                    item for item in policies if item["policy"] == SCHAUWERK_NATIVE_POLICY
                ]
                frontend = [
                    item
                    for item in policies
                    if item["policy"]
                    not in {
                        MAGIC_POLICY,
                        STRICT_POLICY,
                        SCHAUWERK_POLICY,
                        SCHAUWERK_NATIVE_POLICY,
                    }
                ]
                self.assertEqual(len(magic), 1, policies)
                self.assertEqual(len(strict), 1, policies)
                self.assertEqual(len(schauwerk), 1 if is_vps else 0, policies)
                self.assertEqual(len(schauwerk_native), 1 if is_vps else 0, policies)
                self.assertEqual(len(frontend), 1, policies)

                magic_match = [
                    {
                        "method": ["GET"],
                        "path": [MAGIC_LINK_CONFIRM_PATH],
                    }
                ]
                strict_match = [
                    {
                        "not": [
                            {
                                "method": ["GET"],
                                "path": [MAGIC_LINK_CONFIRM_PATH],
                            }
                        ],
                        "path": protected_paths,
                    }
                ]
                frontend_paths = [*protected_paths, *SCHAUWERK_PATHS] if is_vps else protected_paths
                frontend_match = [{"not": [{"path": frontend_paths}]}]

                self.assertEqual(magic[0]["match"], magic_match)
                self.assertEqual(strict[0]["match"], strict_match)
                self.assertEqual(frontend[0]["match"], frontend_match)
                if is_vps:
                    self.assertEqual(
                        schauwerk[0]["match"],
                        [
                            {
                                "not": [{"path": SCHAUWERK_NATIVE_PATHS}],
                                "path": SCHAUWERK_PATHS,
                            }
                        ],
                    )
                    self.assertTrue(schauwerk[0]["deferred"])
                    self.assertEqual(
                        directive_map(schauwerk[0]["policy"]),
                        {
                            "default-src": ("'self'",),
                            "script-src": ("'self'",),
                            "style-src": ("'self'",),
                            "img-src": ("'self'", "data:", "blob:"),
                            "frame-src": ("'self'", "https://embed.diagrams.net"),
                            "connect-src": ("'self'",),
                            "object-src": ("'none'",),
                            "base-uri": ("'none'",),
                            "form-action": ("'none'",),
                            "frame-ancestors": ("'none'",),
                        },
                    )
                    self.assertEqual(
                        schauwerk_native[0]["match"],
                        [{"path": SCHAUWERK_NATIVE_PATHS}],
                    )
                    self.assertTrue(schauwerk_native[0]["deferred"])
                    self.assertEqual(
                        directive_map(schauwerk_native[0]["policy"]),
                        {
                            "default-src": ("'self'",),
                            "script-src": ("'self'",),
                            "style-src": ("'self'",),
                            "img-src": ("'self'", "data:", "blob:"),
                            "frame-src": ("'none'",),
                            "connect-src": ("'self'",),
                            "object-src": ("'none'",),
                            "base-uri": ("'none'",),
                            "form-action": ("'none'",),
                            "frame-ancestors": ("'self'",),
                        },
                    )

                self.assertTrue(
                    magic[0]["deferred"],
                    "magic-link CSP must overwrite any upstream CSP after proxying",
                )
                self.assertTrue(
                    strict[0]["deferred"],
                    "strict API CSP must overwrite any upstream CSP after proxying",
                )

                self.assertEqual(
                    directive_map(magic[0]["policy"]),
                    {
                        "default-src": ("'none'",),
                        "style-src": (MAGIC_STYLE_HASH,),
                        "form-action": ("'self'",),
                        "base-uri": ("'none'",),
                        "frame-ancestors": ("'none'",),
                    },
                )
                self.assertNotIn("script-src", directive_map(magic[0]["policy"]))

                frontend_directives = directive_map(frontend[0]["policy"])
                self.assertNotIn("default-src", frontend_directives)
                self.assertNotIn("script-src", frontend_directives)
                self.assertEqual(frontend_directives["frame-ancestors"], ("'none'",))
                self.assertEqual(frontend_directives["style-src"], ("'self'",))
                for name, tokens in frontend_directives.items():
                    self.assertNotIn(
                        "'unsafe-inline'",
                        tokens,
                        f"frontend CSP must not allow {name} 'unsafe-inline'",
                    )


if __name__ == "__main__":
    unittest.main()