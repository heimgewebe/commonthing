from __future__ import annotations

import base64
import importlib.util
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[3]
SCRIPT = ROOT / "scripts/ops/ensure_web_push_vapid_env.py"
SPEC = importlib.util.spec_from_file_location("ensure_web_push_vapid_env", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class WebPushVapidEnvTests(unittest.TestCase):
    def env_file(self, root: Path, content: str) -> Path:
        path = root / "weltgewebe.env"
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)
        return path

    def values(self, path: Path) -> dict[str, str]:
        values: dict[str, str] = {}
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            if not raw_line or raw_line.lstrip().startswith("#") or "=" not in raw_line:
                continue
            key, value = raw_line.split("=", 1)
            values[key.strip()] = value.strip()
        return values

    def test_creates_complete_valid_configuration_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self.env_file(root, "DATABASE_URL=postgres://example\n")
            before = path.read_bytes()

            result = MODULE.ensure_web_push_env(path)

            self.assertEqual(result, "created")
            values = self.values(path)
            private_key = values[MODULE.PRIVATE_KEY_NAME]
            decoded = base64.urlsafe_b64decode(private_key + "=")
            self.assertEqual(len(decoded), 32)
            self.assertGreater(int.from_bytes(decoded, "big"), 0)
            self.assertLess(int.from_bytes(decoded, "big"), MODULE.P256_ORDER)
            self.assertEqual(values[MODULE.CONTACT_NAME], MODULE.DEFAULT_CONTACT)
            self.assertEqual(
                values[MODULE.ALLOWED_HOSTS_NAME],
                MODULE.DEFAULT_ALLOWED_HOST_SUFFIXES,
            )
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            backup = root / "weltgewebe.env.pre-web-push-v1"
            self.assertEqual(backup.read_bytes(), before)
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)

    def test_second_run_preserves_exact_bytes_and_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self.env_file(root, "A=1\n")

            self.assertEqual(MODULE.ensure_web_push_env(path), "created")
            created = path.read_bytes()
            key = self.values(path)[MODULE.PRIVATE_KEY_NAME]

            self.assertEqual(MODULE.ensure_web_push_env(path), "preserved")
            self.assertEqual(path.read_bytes(), created)
            self.assertEqual(self.values(path)[MODULE.PRIVATE_KEY_NAME], key)

    def test_partial_configuration_fails_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = "WEB_PUSH_VAPID_CONTACT=mailto:ops@example.org\n"
            path = self.env_file(root, content)

            with self.assertRaisesRegex(MODULE.WebPushEnvError, "partial"):
                MODULE.ensure_web_push_env(path)

            self.assertEqual(path.read_text(encoding="utf-8"), content)
            self.assertFalse((root / "weltgewebe.env.pre-web-push-v1").exists())

    def test_duplicate_target_key_fails_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = (
                "WEB_PUSH_VAPID_PRIVATE_KEY=\n"
                "WEB_PUSH_VAPID_PRIVATE_KEY=\n"
            )
            path = self.env_file(root, content)

            with self.assertRaisesRegex(MODULE.WebPushEnvError, "duplicate"):
                MODULE.ensure_web_push_env(path)

            self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_invalid_complete_key_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = (
                "WEB_PUSH_VAPID_PRIVATE_KEY=not-a-valid-private-key\n"
                f"WEB_PUSH_VAPID_CONTACT={MODULE.DEFAULT_CONTACT}\n"
                f"WEB_PUSH_ALLOWED_HOST_SUFFIXES={MODULE.DEFAULT_ALLOWED_HOST_SUFFIXES}\n"
            )
            path = self.env_file(root, content)

            with self.assertRaises(MODULE.WebPushEnvError):
                MODULE.ensure_web_push_env(path)

            self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_disabled_bootstrap_preserves_empty_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = "WEB_PUSH_VAPID_BOOTSTRAP_MODE=disabled\nA=1\n"
            path = self.env_file(root, content)

            self.assertEqual(MODULE.ensure_web_push_env(path), "disabled")
            self.assertEqual(path.read_text(encoding="utf-8"), content)
            self.assertFalse((root / "weltgewebe.env.pre-web-push-v1").exists())

    def test_ip_provider_suffix_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = (
                "WEB_PUSH_VAPID_PRIVATE_KEY="
                + base64.urlsafe_b64encode((1).to_bytes(32, "big"))
                .decode("ascii")
                .rstrip("=")
                + "\n"
                + f"WEB_PUSH_VAPID_CONTACT={MODULE.DEFAULT_CONTACT}\n"
                + "WEB_PUSH_ALLOWED_HOST_SUFFIXES=127.0.0.1\n"
            )
            path = self.env_file(root, content)

            with self.assertRaises(MODULE.WebPushEnvError):
                MODULE.ensure_web_push_env(path)

            self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_symlink_runtime_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = self.env_file(root, "A=1\n")
            symlink = root / "linked.env"
            symlink.symlink_to(target)

            with self.assertRaisesRegex(MODULE.WebPushEnvError, "safe regular file"):
                MODULE.ensure_web_push_env(symlink)

    def test_release_activation_preflights_hold_and_timer_before_vapid(self) -> None:
        activation = (
            ROOT / "scripts/ops/activate-production-reconciler-from-release.sh"
        ).read_text(encoding="utf-8")
        hold = activation.index('python3 -I "$authorization_checker" --commit "$COMMIT"')
        timer_preflight = activation.index("--preflight-only")
        bootstrap = activation.index('python3 -I "$web_push_bootstrap"')
        installer = activation.index(
            'WELTGEWEBE_SOURCE_CHECKOUT="$SOURCE_CHECKOUT"', bootstrap
        )
        self.assertLess(hold, timer_preflight)
        self.assertLess(timer_preflight, bootstrap)
        self.assertLess(bootstrap, installer)
        self.assertIn(
            'require_root_safe_regular_file "$RUNTIME_ENV" "runtime environment"',
            activation,
        )
        self.assertIn('--env-file "$RUNTIME_ENV"', activation)
        self.assertNotIn("WEB_PUSH_VAPID_PRIVATE_KEY=", activation)

    def test_cli_never_prints_private_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self.env_file(root, "A=1\n")

            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--env-file", str(path)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            private_key = self.values(path)[MODULE.PRIVATE_KEY_NAME]
            combined = result.stdout + result.stderr
            self.assertNotIn(private_key, combined)
            self.assertEqual(result.stdout.strip(), "web_push_vapid_env=created")


if __name__ == "__main__":
    unittest.main()
