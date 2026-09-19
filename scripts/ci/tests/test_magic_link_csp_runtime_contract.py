from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest

REPO = Path(__file__).resolve().parents[3]
RUNNING_IN_GITHUB_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"
CADDY_BINARY = shutil.which("caddy")
CADDY_DOCKER_IMAGE = "caddy:2.8.4"
MAGIC_PATH = "/api/auth/magic-link/consume"
MAGIC_POLICY = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none';"
STRICT_POLICY = "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none';"
UPSTREAM_POLICY = "default-src https://upstream.invalid; form-action https://upstream.invalid;"


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


class CspUpstreamHandler(BaseHTTPRequestHandler):
    def _respond(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Security-Policy", UPSTREAM_POLICY)
        self.end_headers()
        self.wfile.write(b"upstream")

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        self._respond()

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        self._respond()

    def log_message(self, format: str, *args: object) -> None:
        pass


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def csp_values(headers: list[tuple[str, str]]) -> list[str]:
    return [value for name, value in headers if name.lower() == "content-security-policy"]


@unittest.skipUnless(
    CADDY_BINARY or DOCKER_BINARY or RUNNING_IN_GITHUB_ACTIONS,
    "caddy binary or docker required for runtime header proof",
)
class MagicLinkCspRuntimeContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if RUNNING_IN_GITHUB_ACTIONS and not (CADDY_BINARY or DOCKER_BINARY):
            raise AssertionError(
                "GitHub Actions requires a caddy binary or reachable Docker daemon "
                "for the runtime header proof"
            )

    def test_edge_overwrites_upstream_csp_with_one_canonical_policy(self) -> None:
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), CspUpstreamHandler)
        upstream_port = int(upstream.server_address[1])
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()

        edge_port = free_port()
        source = (REPO / "infra" / "caddy" / "Caddyfile").read_text(encoding="utf-8")
        source = source.replace(
            "{\n  auto_https off\n}",
            "{\n  auto_https off\n  admin off\n}",
            1,
        )
        source = source.replace(":8081 {", f"http://127.0.0.1:{edge_port} {{", 1)
        source = source.replace(
            "reverse_proxy api:8080",
            f"reverse_proxy 127.0.0.1:{upstream_port}",
            1,
        )
        source = source.replace(
            "reverse_proxy /* web:5173",
            f"reverse_proxy /* 127.0.0.1:{upstream_port}",
            1,
        )

        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "Caddyfile"
            config.write_text(source, encoding="utf-8")
            cidfile = Path(tmp) / "caddy.cid"
            if CADDY_BINARY:
                command = [
                    CADDY_BINARY,
                    "run",
                    "--config",
                    str(config),
                    "--adapter",
                    "caddyfile",
                ]
            else:
                assert DOCKER_BINARY is not None
                command = [
                    DOCKER_BINARY,
                    "run",
                    "--rm",
                    "--network",
                    "host",
                    "--cidfile",
                    str(cidfile),
                    "-v",
                    f"{config}:/etc/caddy/Caddyfile:ro",
                    CADDY_DOCKER_IMAGE,
                    "caddy",
                    "run",
                    "--config",
                    "/etc/caddy/Caddyfile",
                    "--adapter",
                    "caddyfile",
                ]

            stderr_path = Path(tmp) / "caddy.stderr"
            with stderr_path.open("w", encoding="utf-8") as stderr_file:
                process = subprocess.Popen(
                    command,
                    cwd=REPO,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_file,
                    text=True,
                )
            try:
                startup_timeout = 10 if CADDY_BINARY else 60
                deadline = time.monotonic() + startup_timeout
                while time.monotonic() < deadline:
                    try:
                        connection = http.client.HTTPConnection("127.0.0.1", edge_port, timeout=1)
                        connection.request("GET", "/api/health")
                        response = connection.getresponse()
                        response.read()
                        connection.close()
                        break
                    except OSError:
                        time.sleep(0.05)
                else:
                    stderr = stderr_path.read_text(encoding="utf-8") if stderr_path.exists() else ""
                    state = (
                        f"process exited with {process.returncode}"
                        if process.poll() is not None
                        else f"process still running after {startup_timeout}s"
                    )
                    self.fail(f"caddy runtime did not become ready ({state}): {stderr}")

                cases = (
                    ("GET", MAGIC_PATH, MAGIC_POLICY),
                    ("GET", "/api/health", STRICT_POLICY),
                    ("POST", MAGIC_PATH, STRICT_POLICY),
                )
                for method, path, expected_policy in cases:
                    with self.subTest(method=method, path=path):
                        connection = http.client.HTTPConnection("127.0.0.1", edge_port, timeout=2)
                        body = "token=x&nonce=y" if method == "POST" else None
                        headers = (
                            {"Content-Type": "application/x-www-form-urlencoded"}
                            if method == "POST"
                            else {}
                        )
                        connection.request(method, path, body=body, headers=headers)
                        response = connection.getresponse()
                        response_headers = response.getheaders()
                        response.read()
                        connection.close()

                        self.assertEqual(response.status, 200)
                        self.assertEqual(csp_values(response_headers), [expected_policy])
                        self.assertNotIn(UPSTREAM_POLICY, csp_values(response_headers))
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                if process.stderr is not None:
                    process.stderr.close()
                if not CADDY_BINARY and DOCKER_BINARY and cidfile.exists():
                    container_id = cidfile.read_text(encoding="utf-8").strip()
                    if container_id:
                        subprocess.run(
                            [DOCKER_BINARY, "rm", "-f", container_id],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            check=False,
                        )
                upstream.shutdown()
                upstream.server_close()
                upstream_thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
