"""Executable branch and ordering checks for safe production timer upgrade."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ACTIVATOR = ROOT / "scripts/ops/activate-production-reconciler-from-release.sh"
INSTALLER = ROOT / "scripts/ops/install-production-reconciler.sh"
SERVICE = "weltgewebe-production-reconcile.service"
TIMER = "weltgewebe-production-reconcile.timer"


class ProductionTimerUpgradeTests(unittest.TestCase):
    def test_deferred_update_quiesces_timer_before_install_and_restores_last(self) -> None:
        source = INSTALLER.read_text(encoding="utf-8")
        preflight_end = source.index("if ((PREFLIGHT_ONLY == 1)); then")
        disable = source.index("systemctl disable --now " + TIMER)
        fetch = source.index('git -C "$REPO_DIR" fetch --no-tags origin')
        first_write = source.index('atomic_install "$staging/weltgewebe_secure_receipt_io.py"')
        loaded = source.index("systemctl daemon-reload")
        deferred = source.index("if ((DEFER_RECONCILE == 1)); then", loaded)
        initial = source.index("\nelse\n", deferred)
        deferred_branch = source[deferred:initial]
        self.assertLess(preflight_end, disable)
        self.assertLess(disable, fetch)
        self.assertLess(disable, first_write)
        self.assertLess(first_write, loaded)
        self.assertIn("systemctl enable --now " + TIMER, deferred_branch)
        self.assertIn("systemctl is-enabled --quiet", deferred_branch)
        self.assertIn("systemctl is-active --quiet", deferred_branch)
        self.assertNotIn("systemctl start " + SERVICE, deferred_branch)
        self.assertLess(loaded, source.index("systemctl enable --now " + TIMER, deferred))
        self.assertIn("systemctl start " + SERVICE, source[initial:])
        # An aborted upgrade must not leave an enabled timer that can start
        # a partially updated helper contract after reboot.
        self.assertNotIn("systemctl stop " + TIMER, source[preflight_end:loaded])

    def _invoke_running_service_gate(
        self, *, timer_enabled: str, timer_active: str,
        service_state: str = "activating", valid_handoff: bool = True
    ) -> subprocess.CompletedProcess[str]:
        source = ACTIVATOR.read_text(encoding="utf-8")
        begin = source.index('reconciler_service_state="$(systemctl show --property=ActiveState --value ' + SERVICE + ')"')
        end = source.index("\n# The release authorization above", begin)
        actual_guard = source[begin:end]
        with tempfile.TemporaryDirectory(prefix="timer-upgrade-gate-") as temp:
            binary = Path(temp) / "systemctl"
            binary.write_text(
                "#!/usr/bin/env bash\n"
                "case \"$1\" in\n"
                "  is-active)\n"
                "    [[ \"$3\" == \"weltgewebe-production-reconcile.service\" && "
                "\"$MOCK_SERVICE_ACTIVE\" == 1 ]]\n"
                "    exit $? ;;\n"
                "  show)\n"
                "    case \"$2\" in\n"
                "      --property=UnitFileState) echo \"$MOCK_TIMER_ENABLED\" ;;\n"
                "      --property=ActiveState) if [[ \"$4\" == \"weltgewebe-production-reconcile.service\" ]]; then echo \"$MOCK_SERVICE_STATE\"; else echo \"$MOCK_TIMER_ACTIVE\"; fi ;;\n"
                "      *) exit 22 ;;\n"
                "    esac ;;\n"
                "  *) exit 23 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            binary.chmod(0o755)
            env = {
                **os.environ,
                "PATH": temp + os.pathsep + os.environ.get("PATH", ""),
                "MOCK_SERVICE_STATE": service_state,
                "MOCK_TIMER_ENABLED": timer_enabled,
                "MOCK_TIMER_ACTIVE": timer_active,
                "WELTGEWEBE_PRODUCTION_LOCK_OWNER_ENTRYPOINT":
                    "reconciler" if valid_handoff else "direct",
                "WELTGEWEBE_PRODUCTION_LOCK_DOMAIN": "weltgewebe-production-deployment-v1",
                "WELTGEWEBE_DEPLOY_INVOCATION_ID": "a" * 64,
            }
            command = (
                "set -Eeuo pipefail\n"
                "fail() { echo \"ERROR: $*\" >&2; exit 1; }\n"
                "COMMIT=" + "a" * 40 + "\nrelease_dir_real=/tmp/test-release\n"
                + actual_guard + "\nprintf 'CONTINUED\\n'\n"
            )
            return subprocess.run(["bash", "-c", command], env=env,
                                  capture_output=True, text=True, timeout=8)

    def test_running_reconciler_skips_only_initial_service_bootstrap(self) -> None:
        initial = self._invoke_running_service_gate(
            timer_enabled="disabled", timer_active="inactive"
        )
        self.assertEqual(initial.returncode, 0, initial.stderr)
        self.assertIn("already_running", initial.stdout)
        self.assertNotIn("CONTINUED", initial.stdout)

        deferred = self._invoke_running_service_gate(
            timer_enabled="enabled", timer_active="active"
        )
        self.assertEqual(deferred.returncode, 0, deferred.stderr)
        self.assertIn("CONTINUED", deferred.stdout)
        self.assertNotIn("already_running", deferred.stdout)

        for enabled, active in (("enabled", "inactive"), ("disabled", "active")):
            with self.subTest(enabled=enabled, active=active):
                inconsistent = self._invoke_running_service_gate(
                    timer_enabled=enabled, timer_active=active
                )
                self.assertNotEqual(inconsistent.returncode, 0)
                self.assertNotIn("CONTINUED", inconsistent.stdout)

        invalid = self._invoke_running_service_gate(
            timer_enabled="enabled", timer_active="active", valid_handoff=False
        )
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("exact inherited deployment handoff", invalid.stderr)

    def test_oneshot_activating_and_active_both_block_recursive_bootstrap(self) -> None:
        for service_state in ("activating", "active"):
            with self.subTest(service_state=service_state):
                result = self._invoke_running_service_gate(
                    timer_enabled="disabled", timer_active="inactive",
                    service_state=service_state,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("already_running", result.stdout)
                self.assertNotIn("CONTINUED", result.stdout)

    def test_unknown_and_failed_service_states_fail_closed(self) -> None:
        for service_state in ("failed", "deactivating", "reloading", ""):
            with self.subTest(service_state=service_state):
                result = self._invoke_running_service_gate(
                    timer_enabled="disabled", timer_active="inactive",
                    service_state=service_state,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unsupported reconciler service state", result.stderr)


    def _execute_compose_lock_handoff(
        self, *, timer_unit: str, timer_active: str, service_state: str,
        child_expects_lock_free: bool, child_fails: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        wrapper = (ROOT / "scripts/weltgewebe-up").read_text(encoding="utf-8")
        begin = wrapper.index("      # Scoped initial bootstrap Compose lock handoff BEGIN")
        end = wrapper.index("      # Scoped initial bootstrap Compose lock handoff END", begin)
        actual_code = wrapper[begin:end]
        with tempfile.TemporaryDirectory(prefix="production-compose-lock-test-") as temp:
            directory = Path(temp)
            lock_file = directory / "compose.lock"
            activator = directory / "activator.sh"
            activator.write_text(
                '#!/usr/bin/env bash\nset -Eeuo pipefail\n'
                'if [[ "$CHILD_EXPECTS_LOCK_FREE" == "1" ]]; then\n'
                '  flock -n "$LOCK_FILE" -c true || exit 73\n'
                'else\n'
                '  if flock -n "$LOCK_FILE" -c true; then exit 74; fi\n'
                'fi\n'
                'if [[ "$CHILD_FAILS" == "1" ]]; then exit 77; fi\n'
                'echo INNER_ACTIVATOR_PASSED\n',
                encoding="utf-8",
            )
            activator.chmod(0o755)
            script = (
                'set -Eeuo pipefail\n'
                'systemctl() {\n'
                '  case "$*" in\n'
                '    "show --property=UnitFileState --value weltgewebe-production-reconcile.timer") echo "$TIMER_UNIT" ;;\n'
                '    "show --property=ActiveState --value weltgewebe-production-reconcile.timer") echo "$TIMER_ACTIVE" ;;\n'
                '    "show --property=ActiveState --value weltgewebe-production-reconcile.service") echo "$SERVICE_STATE" ;;\n'
                '    *) return 92 ;;\n'
                '  esac\n'
                '}\n'
                'exec {DEPLOY_LOCK_FD}> "$LOCK_FILE"\n'
                'flock -n "$DEPLOY_LOCK_FD" || exit 81\n'
                'reconciler_activator="$FAKE_ACTIVATOR"\n'
                'release_dir="/fake/release"\n'
                'release_commit="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\n'
                + actual_code
                + '\nif flock -n "$LOCK_FILE" -c true; then exit 82; fi\n'
                'echo OUTER_COMPOSE_LOCK_REACQUIRED\n'
            )
            environment = {
                **os.environ,
                "LOCK_FILE": str(lock_file),
                "FAKE_ACTIVATOR": str(activator),
                "TIMER_UNIT": timer_unit,
                "TIMER_ACTIVE": timer_active,
                "SERVICE_STATE": service_state,
                "CHILD_EXPECTS_LOCK_FREE": "1" if child_expects_lock_free else "0",
                "CHILD_FAILS": "1" if child_fails else "0",
            }
            return subprocess.run(
                ["bash", "-c", script], env=environment,
                text=True, capture_output=True, timeout=10,
            )

    def test_initial_reconciler_first_start_releases_and_restores_compose_lock(self) -> None:
        result = self._execute_compose_lock_handoff(
            timer_unit="disabled", timer_active="inactive",
            service_state="inactive", child_expects_lock_free=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("INNER_ACTIVATOR_PASSED", result.stdout)
        self.assertIn("OUTER_COMPOSE_LOCK_REACQUIRED", result.stdout)

    def test_deferred_and_reentrant_activators_preserve_compose_exclusion(self) -> None:
        for timer_unit, timer_active, service_state in (
            ("enabled", "active", "inactive"),
            ("disabled", "inactive", "activating"),
        ):
            with self.subTest(state=(timer_unit, timer_active, service_state)):
                result = self._execute_compose_lock_handoff(
                    timer_unit=timer_unit, timer_active=timer_active,
                    service_state=service_state, child_expects_lock_free=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("OUTER_COMPOSE_LOCK_REACQUIRED", result.stdout)

    def test_failed_first_start_never_continues_after_unlocked_activation(self) -> None:
        result = self._execute_compose_lock_handoff(
            timer_unit="disabled", timer_active="inactive",
            service_state="inactive", child_expects_lock_free=True,
            child_fails=True,
        )
        self.assertEqual(result.returncode, 77, result.stderr)
        self.assertNotIn("OUTER_COMPOSE_LOCK_REACQUIRED", result.stdout)

    def test_non_reentrant_path_proceeds_without_active_service(self) -> None:
        result = self._invoke_running_service_gate(
            timer_enabled="disabled", timer_active="inactive", service_state="inactive"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CONTINUED", result.stdout)


if __name__ == "__main__":
    unittest.main()
