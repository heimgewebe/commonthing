from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts.ci.tests import test_deploy_exact_commit_integration as deploy_integration

ROOT = Path(__file__).parents[3]
EXPECTED_GIT_MAINTENANCE_ENV = {
    "GIT_CONFIG_COUNT": "2",
    "GIT_CONFIG_KEY_0": "maintenance.auto",
    "GIT_CONFIG_VALUE_0": "false",
    "GIT_CONFIG_KEY_1": "gc.auto",
    "GIT_CONFIG_VALUE_1": "0",
}


class GitFixtureMaintenanceIsolationTests(unittest.TestCase):
    def test_validate_tests_disables_automatic_git_maintenance(self) -> None:
        result = subprocess.run(
            ["make", "--no-print-directory", "-n", "validate-tests"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        # make validate-tests runs scripts/ci discover via uv-locked tools/py.
        python_command = "python -m unittest discover scripts/ci/tests/"
        command = next(
            (
                line.strip()
                for line in result.stdout.splitlines()
                if line.strip().endswith(python_command)
                and "uv run --project tools/py --locked" in line
            ),
            None,
        )
        self.assertIsNotNone(command, result.stdout)
        assert command is not None

        python_index = command.index(python_command)
        expected_assignments = (
            "GIT_CONFIG_COUNT=2",
            "GIT_CONFIG_KEY_0=maintenance.auto",
            "GIT_CONFIG_VALUE_0=false",
            "GIT_CONFIG_KEY_1=gc.auto",
            "GIT_CONFIG_VALUE_1=0",
        )
        for assignment in expected_assignments:
            self.assertIn(assignment, command)
            self.assertLess(command.index(assignment), python_index)

    def test_direct_git_commands_disable_automatic_maintenance(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["git", "status"],
            returncode=0,
            stdout="",
            stderr="",
        )
        with mock.patch(
            "scripts.ci.tests.test_deploy_exact_commit_integration.subprocess.run",
            return_value=completed,
        ) as run_mock:
            deploy_integration.run(["git", "status"])

        effective_env = run_mock.call_args.kwargs["env"]
        for key, value in EXPECTED_GIT_MAINTENANCE_ENV.items():
            self.assertEqual(effective_env[key], value)

    def test_deploy_passes_maintenance_settings_after_sudo(self) -> None:
        fixture = SimpleNamespace(
            bin=ROOT / ".fixture-bin",
            source=ROOT / ".fixture-source",
            releases=ROOT / ".fixture-releases",
            runtime_env=ROOT / ".fixture-runtime.env",
            state=ROOT / ".fixture-state",
            commit="a" * 40,
            remote=ROOT / ".fixture-remote.git",
            artifact=ROOT / ".fixture-artifact.tar.gz",
            artifact_sha="b" * 64,
            test_deploy_script=ROOT / ".fixture-ops/deploy-exact-commit-vps.sh",
            root=ROOT / ".fixture-root",
        )
        fixture.base_environment = lambda: (
            deploy_integration.DeployExactCommitIntegrationTests.base_environment(
                fixture
            )
        )
        fixture.privileged = lambda argv: ["/usr/bin/sudo", "-n", *argv]
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        )

        with mock.patch.object(
            deploy_integration,
            "run",
            return_value=completed,
        ) as run_mock:
            deploy_integration.DeployExactCommitIntegrationTests.deploy(
                fixture,
                advance=False,
            )

        command = run_mock.call_args.args[0]
        self.assertEqual(command[:3], ["/usr/bin/sudo", "-n", "env"])
        deploy_index = command.index(str(fixture.test_deploy_script))
        for key, value in EXPECTED_GIT_MAINTENANCE_ENV.items():
            assignment = f"{key}={value}"
            self.assertIn(assignment, command)
            self.assertGreater(command.index(assignment), 2)
            self.assertLess(command.index(assignment), deploy_index)


if __name__ == "__main__":
    unittest.main()
