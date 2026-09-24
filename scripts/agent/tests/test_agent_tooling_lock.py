"""Tests for the hash-bound Python dependency contract used by agent tooling."""

from __future__ import annotations

import re
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.agent.validate_agent_tooling_lock import (
    EXPECTED_ARTIFACT_COUNT,
    EXPECTED_ARTIFACT_SET_SHA256,
    EXPECTED_DEPENDENCIES,
    EXPECTED_DEPENDENCY,
    EXPECTED_PYTEST_DEPENDENCY,
    validate_tooling_lock,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


class TestAgentToolingLock(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        target = self.root / "tools" / "py"
        target.mkdir(parents=True)
        shutil.copy2(REPO_ROOT / "tools/py/pyproject.toml", target / "pyproject.toml")
        shutil.copy2(REPO_ROOT / "tools/py/uv.lock", target / "uv.lock")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _pyyaml_lock_slice(self) -> tuple[str, int, int]:
        """Return (raw, start, end) of the [[package]] pyyaml block in the lock."""
        path = self.root / "tools/py/uv.lock"
        raw = path.read_text(encoding="utf-8")
        match = re.search(
            r'(?ms)^\[\[package\]\]\nname = "pyyaml"\n.*?(?=^\[\[package\]\]|\Z)',
            raw,
        )
        self.assertIsNotNone(match)
        assert match is not None
        return raw, match.start(), match.end()

    def _replace_lock_once(self, old: str, new: str) -> None:
        path = self.root / "tools/py/uv.lock"
        raw = path.read_text(encoding="utf-8")
        self.assertEqual(raw.count(old), 1)
        path.write_text(raw.replace(old, new, 1), encoding="utf-8")

    def test_repository_lock_contract_is_valid(self) -> None:
        self.assertEqual(validate_tooling_lock(REPO_ROOT), [])

    def test_wrong_artifact_hash_is_rejected(self) -> None:
        raw, start, end = self._pyyaml_lock_slice()
        pyyaml_block = raw[start:end]
        match = re.search(r"sha256:[0-9a-f]{64}", pyyaml_block)
        self.assertIsNotNone(match)
        assert match is not None
        mutated_block = pyyaml_block.replace(match.group(0), "sha256:" + "0" * 64, 1)
        path = self.root / "tools/py/uv.lock"
        path.write_text(raw[:start] + mutated_block + raw[end:], encoding="utf-8")
        findings = validate_tooling_lock(self.root)
        self.assertTrue(
            any(item["code"] == "AGENT_TOOLING_LOCK_ARTIFACT_SET" for item in findings)
        )

    def test_missing_artifact_hash_is_rejected(self) -> None:
        raw, start, end = self._pyyaml_lock_slice()
        pyyaml_block = raw[start:end]
        mutated_block, count = re.subn(
            r', hash = "sha256:[0-9a-f]{64}"',
            "",
            pyyaml_block,
            count=1,
        )
        self.assertEqual(count, 1)
        path = self.root / "tools/py/uv.lock"
        path.write_text(raw[:start] + mutated_block + raw[end:], encoding="utf-8")
        findings = validate_tooling_lock(self.root)
        self.assertTrue(
            any(item["code"] == "AGENT_TOOLING_LOCK_ARTIFACT" for item in findings)
        )

    def test_unreviewed_distribution_is_rejected(self) -> None:
        raw, start, end = self._pyyaml_lock_slice()
        pyyaml_block = raw[start:end]
        marker = "wheels = [\n"
        self.assertEqual(pyyaml_block.count(marker), 1)
        fake = (
            '    { url = "https://files.pythonhosted.org/packages/00/fake.whl", '
            'hash = "sha256:' + "1" * 64 + '", size = 1 },\n'
        )
        mutated_block = pyyaml_block.replace(marker, marker + fake, 1)
        path = self.root / "tools/py/uv.lock"
        path.write_text(raw[:start] + mutated_block + raw[end:], encoding="utf-8")
        findings = validate_tooling_lock(self.root)
        self.assertTrue(
            any(item["code"] == "AGENT_TOOLING_LOCK_ARTIFACT_SET" for item in findings)
        )

    def test_pyproject_version_drift_is_rejected(self) -> None:
        path = self.root / "tools/py/pyproject.toml"
        raw = path.read_text(encoding="utf-8")
        self.assertIn(EXPECTED_DEPENDENCY, raw)
        path.write_text(raw.replace(EXPECTED_DEPENDENCY, "PyYAML==6.0.3"), encoding="utf-8")
        findings = validate_tooling_lock(self.root)
        self.assertTrue(
            any(item["code"] == "AGENT_TOOLING_DEPENDENCY_PIN" for item in findings)
        )

    def test_pytest_pin_drift_is_rejected(self) -> None:
        path = self.root / "tools/py/pyproject.toml"
        raw = path.read_text(encoding="utf-8")
        self.assertIn(EXPECTED_PYTEST_DEPENDENCY, raw)
        path.write_text(
            raw.replace(EXPECTED_PYTEST_DEPENDENCY, "pytest==8.3.5"), encoding="utf-8"
        )
        findings = validate_tooling_lock(self.root)
        self.assertTrue(
            any(item["code"] == "AGENT_TOOLING_DEPENDENCY_PIN" for item in findings)
        )

    def test_expected_dependencies_include_pytest_and_pyyaml(self) -> None:
        self.assertEqual(
            set(EXPECTED_DEPENDENCIES),
            {EXPECTED_DEPENDENCY, EXPECTED_PYTEST_DEPENDENCY},
        )
        pyproject = (REPO_ROOT / "tools/py/pyproject.toml").read_text(encoding="utf-8")
        for required in EXPECTED_DEPENDENCIES:
            self.assertIn(required, pyproject)

    def test_workflow_and_justfile_use_the_locked_project(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/agent-safety-preflight.yml").read_text(
            encoding="utf-8"
        )
        docs_guard = (REPO_ROOT / ".github/workflows/docs-guard.yml").read_text(
            encoding="utf-8"
        )
        justfile = (REPO_ROOT / "Justfile").read_text(encoding="utf-8")
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

        self.assertNotRegex(workflow, r"pip install .*PyYAML|pip install .*pyyaml")
        self.assertIn("uv sync --project tools/py --locked", workflow)
        self.assertIn(
            "uv run --project tools/py --locked python -m scripts.agent.validate_agent_tooling_lock",
            workflow,
        )
        # docs-guard must use the locked tools/py env only for make validate.
        self.assertIn("uv sync --project tools/py --locked", docs_guard)
        self.assertNotRegex(
            docs_guard,
            r"pip install .*pytest|pip install .*PyYAML|pip install .*pyyaml",
        )
        self.assertIn(
            "uv run --project tools/py --locked python -m scripts.agent.validate_agent_tooling_lock",
            justfile,
        )
        self.assertIn(
            "uv run --project tools/py --locked python -m scripts.agent.validate_repo_agent_contract",
            justfile,
        )
        # Full make validate path must share the same uv-locked agent semantics.
        self.assertIn("UV_RUN := uv run --project $(UV_PROJECT) --locked", makefile)
        self.assertIn("require-uv-tooling", makefile)
        self.assertIn("agent-contract-check", makefile)
        self.assertIn(
            "$(UV_RUN) python -m scripts.agent.validate_agent_tooling_lock",
            makefile,
        )
        self.assertIn(
            "$(UV_RUN) python -m scripts.agent.validate_repo_agent_contract",
            makefile,
        )
        self.assertIn(
            "$(UV_RUN) python -m unittest discover scripts/agent/tests/",
            makefile,
        )
        self.assertIn(
            "$(CI_TEST_GIT_ENV) $(UV_RUN) python -m unittest discover scripts/ci/tests/",
            makefile,
        )
        self.assertIn(
            "$(UV_RUN) python -m pytest -q scripts/ci/tests/test_semantic_search_production_activation.py",
            makefile,
        )
        self.assertNotRegex(
            makefile,
            r"(?m)^validate-tests:.*\n(?:\t.*\n)*?\tpython3 -m unittest discover scripts/agent/tests/",
        )

    def test_make_validate_path_has_no_bare_host_python3(self) -> None:
        """Negativtest: full make validate Python path must not invoke host python3."""
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        # Recipe lines under validate* / platform* / cell-pilot* / agent-contract /
        # require-uv-tooling / ci-validate / docs-guard must not call bare python3.
        recipe_targets = (
            "require-uv-tooling",
            "agent-contract-check",
            "validate-tests",
            "cell-pilot-check",
            "platform-check",
            "platform-render",
            "platform-kind-proof",
            "validate-core",
            "generate-system-map",
            "check-system-map-drift",
            "validate-guards",
            "validate-shell-tests",
            "validate",
            "ci-validate",
            "docs-guard",
        )
        lines = makefile.splitlines()
        current: str | None = None
        offenders: list[str] = []
        target_re = re.compile(r"^([A-Za-z0-9_.-]+):")
        for line in lines:
            header = target_re.match(line)
            if header and not line.startswith("\t"):
                current = header.group(1)
                continue
            if current not in recipe_targets:
                continue
            if not line.startswith("\t"):
                continue
            body = line[1:]
            # Allow only comments that mention python3, not command invocations.
            if body.lstrip().startswith("#"):
                continue
            if re.search(r"(^|[\s;|&])python3\b", body):
                offenders.append(f"{current}: {body.strip()}")
        self.assertEqual(
            offenders,
            [],
            "bare host python3 must not appear in the full make validate path: "
            + "; ".join(offenders),
        )

    def test_make_validate_shell_dependencies_have_no_bare_host_python3(self) -> None:
        """All reviewed shell checks reached by make validate use tools/py for Python."""
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        recipe_targets = {
            "validate-guards",
            "validate-shell-tests",
        }
        direct: set[str] = set()
        current: str | None = None
        target_re = re.compile(r"^([A-Za-z0-9_.-]+):")
        shell_re = re.compile(r"^\tbash\s+(scripts/[A-Za-z0-9_./-]+\.sh)\s*$")
        for line in makefile.splitlines():
            header = target_re.match(line)
            if header and not line.startswith("\t"):
                current = header.group(1)
                continue
            if current not in recipe_targets:
                continue
            shell = shell_re.match(line)
            if shell:
                direct.add(shell.group(1))
        expected_direct = {
            "scripts/docmeta/repo-structure-guard.sh",
            "scripts/docmeta/docs-relations-guard.sh",
            "scripts/docmeta/generated-files-guard.sh",
            "scripts/docmeta/coverage-guard.sh",
            "scripts/tests/test_weltgewebe_up_git_branch.sh",
            "scripts/tests/test_weltgewebe_up_frontend_required.sh",
            "scripts/tests/test_weltgewebe_up_deploy_scope.sh",
            "scripts/tests/test_verify_deployment.sh",
            "scripts/tests/test_verify_deployment_isolation.sh",
            "scripts/tests/test_version_guard.sh",
            "scripts/tests/test_basemap_mode_guard.sh",
            "scripts/tests/test_basemap_runtime_proof_contract.sh",
            "scripts/tests/test_security_headers_guard.sh",
            "scripts/tests/test_repo_contract_guards.sh",
            "scripts/tests/test_postgres_backup_restore_contract.sh",
            "scripts/tests/test_offhost_backup_pull_contract.sh",
            "scripts/tests/test_web_artifact_install_contract.sh",
            "scripts/tests/test_api_release_identity_contract.sh",
            "scripts/tests/test_runtime_contract.sh",
        }
        self.assertEqual(direct, expected_direct)
        reviewed = direct | {
            "scripts/guard/compose-image-guard.sh",
            "scripts/guard/security-headers-guard.sh",
        }
        offenders: list[str] = []
        for relative in sorted(reviewed):
            for line_number, line in enumerate(
                (REPO_ROOT / relative).read_text(encoding="utf-8").splitlines(), 1
            ):
                stripped = line.lstrip()
                if stripped.startswith("#!") or stripped.startswith("#"):
                    continue
                if re.search(r"(^|[\s;|&])python3\b", line):
                    offenders.append(f"{relative}:{line_number}: {stripped}")
        self.assertEqual(
            offenders,
            [],
            "bare host python3 found in reviewed make validate shell checks: "
            + "; ".join(offenders),
        )

    def test_reviewed_artifact_contract_is_explicit(self) -> None:
        self.assertEqual(EXPECTED_ARTIFACT_COUNT, 28)
        self.assertRegex(EXPECTED_ARTIFACT_SET_SHA256, r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
