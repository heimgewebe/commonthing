"""Dev compose publishes its ports on loopback unless LAN access is opted in."""

from pathlib import Path
import re
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[3]
COMPOSE = ROOT / "infra/compose/compose.core.yml"

# A published port must name its host interface. Without one, Docker binds all
# interfaces and the dev database (password `welt:gewebe`) is reachable from
# any network the machine joins.
BOUND_PORT = re.compile(r"^\$\{(?P<var>[A-Z_]+):-(?P<default>[^}]+)\}:\d+:\d+$")
LOOPBACK_DEFAULTS = {"127.0.0.1", "::1"}


def published_ports(compose: dict) -> list[tuple[str, str]]:
    ports = []
    for name, service in (compose.get("services") or {}).items():
        for entry in service.get("ports") or []:
            if not isinstance(entry, str):
                raise AssertionError(f"{name}: long-form port {entry!r} is not checked")
            ports.append((name, entry))
    return ports


def unsafe_ports(compose: dict) -> list[str]:
    problems = []
    for name, entry in published_ports(compose):
        match = BOUND_PORT.match(entry)
        if match is None:
            problems.append(f"{name}: {entry!r} has no bind variable")
        elif match["default"] not in LOOPBACK_DEFAULTS:
            problems.append(f"{name}: {entry!r} does not default to loopback")
    return problems


class ComposeDevPortBindingTest(unittest.TestCase):
    def test_every_dev_port_defaults_to_loopback(self) -> None:
        compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
        self.assertTrue(published_ports(compose), "expected published dev ports")
        self.assertEqual(unsafe_ports(compose), [])

    def test_unbound_or_public_default_is_rejected(self) -> None:
        compose = {
            "services": {
                "db": {"ports": ["5432:5432"]},
                "api": {"ports": ["${DEV_BIND:-0.0.0.0}:8080:8080"]},
                "web": {"ports": ["${DEV_BIND:-127.0.0.1}:5173:5173"]},
            }
        }
        self.assertEqual(
            unsafe_ports(compose),
            [
                "db: '5432:5432' has no bind variable",
                "api: '${DEV_BIND:-0.0.0.0}:8080:8080' does not default to loopback",
            ],
        )


if __name__ == "__main__":
    unittest.main()
