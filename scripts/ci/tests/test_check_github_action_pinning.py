from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "check_github_action_pinning.py"
CACHE_TAG_COMMIT = "55cc8345863c7cc4c66a329aec7e433d2d1c52a9"
CACHE_UNTAGGED_COMMIT = "3edfce9056124e459a23f683a21433670d47daca"
EXPECTED_CACHE_WORKFLOWS = {
    "api-smoke.yml": 1,
    "auth-passkey-register-proof.yml": 1,
    "auth-session-persistence-proof.yml": 1,
    "ci.yml": 3,
    "kubernetes-platform-proof.yml": 4,
    "python-tooling.yml": 1,
    "reusable-web-check.yml": 1,
}


class CheckGitHubActionPinningTest(unittest.TestCase):
    def run_checker(
        self,
        workflow: str,
        *,
        arguments: tuple[str, ...] = (),
        evidence: dict[str, object] | None = None,
        repo_marker: bool = False,
        additional_workflows: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            workflows_dir = repo / ".github" / "workflows"
            workflows_dir.mkdir(parents=True)
            (workflows_dir / "audit.yml").write_text(workflow, encoding="utf-8")
            for name, content in (additional_workflows or {}).items():
                (workflows_dir / name).write_text(content, encoding="utf-8")
            if repo_marker:
                (repo / "repo.meta.yaml").write_text("repo_name: test\n", encoding="utf-8")
            command = [sys.executable, str(SCRIPT), *arguments]
            if evidence is not None:
                evidence_path = repo / "evidence.json"
                evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
                command.extend(("--evidence-file", str(evidence_path)))
            return subprocess.run(
                command,
                cwd=repo,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )

    def test_sha_pinned_action_is_classified(self) -> None:
        result = self.run_checker(
            """
name: pin
on: workflow_dispatch
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@1234567890123456789012345678901234567890 # tag: v7.0.1
"""
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("kind.github-action=1", result.stdout)
        self.assertIn("policy.pinned-sha=1", result.stdout)

    def test_sha_pin_without_provenance_declaration_fails(self) -> None:
        result = self.run_checker(
            """
name: pin
on: workflow_dispatch
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@1234567890123456789012345678901234567890
"""
        )
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("must declare its provenance", result.stdout)

    def test_explicit_untagged_marker_satisfies_the_declaration(self) -> None:
        result = self.run_checker(
            """
name: pin
on: workflow_dispatch
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@1234567890123456789012345678901234567890 # provenance: untagged (1 commit after v7.0.1)
"""
        )
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_local_and_docker_refs_need_no_declaration(self) -> None:
        result = self.run_checker(
            """
name: pin
on: workflow_dispatch
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: ./.github/actions/setup
      - uses: docker://alpine:3.21
"""
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("kind.local-action=1", result.stdout)
        self.assertIn("kind.docker-image=1", result.stdout)

    def test_declaration_blockers_are_reported_in_json(self) -> None:
        result = self.run_checker(
            """
name: pin
on: workflow_dispatch
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@1234567890123456789012345678901234567890
""",
            arguments=("--format", "json"),
        )
        self.assertEqual(result.returncode, 1, result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "fail")
        self.assertEqual(
            payload["declaration_blockers"],
            [
                {
                    "workflow": ".github/workflows/audit.yml",
                    "job": "audit",
                    "uses": (
                        "actions/checkout@"
                        "1234567890123456789012345678901234567890"
                    ),
                }
            ],
        )

    def test_named_action_ref_is_classified(self) -> None:
        result = self.run_checker(
            """
name: tag
on: workflow_dispatch
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
"""
        )
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("policy.named-ref=1", result.stdout)
        self.assertIn("must be pinned to 40-character commit SHAs", result.stdout)

    def test_reusable_default_branch_is_classified(self) -> None:
        result = self.run_checker(
            """
name: reusable
on: workflow_dispatch
jobs:
  call:
    uses: owner/repo/.github/workflows/reusable.yml@main
"""
        )
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("kind.reusable-workflow=1", result.stdout)
        self.assertIn("policy.mutable-default-branch=1", result.stdout)

    def test_local_action_is_not_sha_pinning_scope(self) -> None:
        result = self.run_checker(
            """
name: local
on: workflow_dispatch
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: ./.github/actions/local
"""
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("kind.local-action=1", result.stdout)
        self.assertIn("policy.local=1", result.stdout)

    def test_docker_image_action_is_not_sha_pinning_scope(self) -> None:
        result = self.run_checker(
            """
name: docker
on: workflow_dispatch
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: docker://alpine:3.20
"""
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("kind.docker-image=1", result.stdout)
        self.assertIn("policy.docker-ref=1", result.stdout)

    def test_missing_external_ref_is_blocking(self) -> None:
        result = self.run_checker(
            """
name: missing
on: workflow_dispatch
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout
"""
        )
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("policy.missing-ref=1", result.stdout)

    def test_exact_cache_tag_is_machine_readable_and_passes(self) -> None:
        result = self.run_checker(
            f"""
name: cache
on: workflow_dispatch
jobs:
  cache:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/cache@{CACHE_TAG_COMMIT} # tag: v6.1.0
""",
            arguments=("--format", "json"),
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        payload = json.loads(result.stdout)
        record = payload["provenance"][0]
        self.assertEqual(record["action"], "actions/cache")
        self.assertEqual(record["pinned_commit"], CACHE_TAG_COMMIT)
        self.assertEqual(record["declared_tag"], "v6.1.0")
        self.assertEqual(record["tag_commit"], CACHE_TAG_COMMIT)
        self.assertEqual(record["classification"], "exact_tag")
        self.assertIn("github-api:", record["evidence_source"])

    def test_cache_tag_mismatch_is_rejected(self) -> None:
        result = self.run_checker(
            f"""
name: cache
on: workflow_dispatch
jobs:
  cache:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/cache@{CACHE_UNTAGGED_COMMIT} # tag: v6.1.0
""",
            arguments=("--format", "json"),
        )
        self.assertEqual(result.returncode, 1, result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["provenance"][0]["classification"], "tag_mismatch")
        self.assertEqual(payload["provenance"][0]["tag_commit"], CACHE_TAG_COMMIT)

    def test_explicit_untagged_commit_requires_matching_evidence(self) -> None:
        result = self.run_checker(
            f"""
name: cache
on: workflow_dispatch
jobs:
  cache:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/cache@{CACHE_UNTAGGED_COMMIT} # provenance: untagged
""",
            arguments=("--format", "json"),
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload["provenance"][0]["classification"], "untagged_commit"
        )

    def test_cache_pin_without_provenance_marker_is_unresolved(self) -> None:
        result = self.run_checker(
            f"""
name: cache
on: workflow_dispatch
jobs:
  cache:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/cache@{CACHE_TAG_COMMIT}
""",
            arguments=("--format", "json"),
        )
        self.assertEqual(result.returncode, 1, result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["provenance"][0]["classification"], "unresolved")

    def test_moved_tag_between_selection_and_readback_is_unresolved(self) -> None:
        moved = "0123456789012345678901234567890123456789"
        evidence = {
            "schema_version": 1,
            "evidence": [
                {
                    "action": "actions/cache",
                    "declared_tag": "v6.1.0",
                    "selected_tag_commit": CACHE_TAG_COMMIT,
                    "readback_tag_commit": moved,
                    "product_version": "6.1.0",
                    "evidence_source": "test-fixture",
                }
            ],
        }
        result = self.run_checker(
            f"""
name: cache
on: workflow_dispatch
jobs:
  cache:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/cache@{CACHE_TAG_COMMIT} # tag: v6.1.0
""",
            arguments=("--format", "json"),
            evidence=evidence,
        )
        self.assertEqual(result.returncode, 1, result.stdout)
        payload = json.loads(result.stdout)
        record = payload["provenance"][0]
        self.assertEqual(record["classification"], "unresolved")
        self.assertIn("tag-moved", record["evidence_source"])

    def test_missing_upstream_evidence_is_unresolved(self) -> None:
        result = self.run_checker(
            f"""
name: cache
on: workflow_dispatch
jobs:
  cache:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/cache@{CACHE_TAG_COMMIT} # tag: v6.1.0
""",
            arguments=("--format", "json"),
            evidence={"schema_version": 1, "evidence": []},
        )
        self.assertEqual(result.returncode, 1, result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["provenance"][0]["classification"], "unresolved")

    def test_invalid_evidence_commit_fails_closed(self) -> None:
        evidence = {
            "schema_version": 1,
            "evidence": [
                {
                    "action": "actions/cache",
                    "declared_tag": "v6.1.0",
                    "selected_tag_commit": "short",
                    "readback_tag_commit": CACHE_TAG_COMMIT,
                    "product_version": "6.1.0",
                    "evidence_source": "test-fixture",
                }
            ],
        }
        result = self.run_checker(
            "name: empty\non: workflow_dispatch\njobs: {}\n",
            arguments=("--format", "json"),
            evidence=evidence,
        )
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("selected_tag_commit", json.loads(result.stdout)["error"])

    def test_repository_consumer_contract_requires_exact_counts(self) -> None:
        def workflow(count: int) -> str:
            uses = "\n".join(
                f"      - uses: actions/cache@{CACHE_TAG_COMMIT} # tag: v6.1.0"
                for _ in range(count)
            )
            return f"""
name: cache
on: workflow_dispatch
jobs:
  cache:
    runs-on: ubuntu-latest
    steps:
{uses}
"""

        files = {
            name: workflow(count)
            for name, count in EXPECTED_CACHE_WORKFLOWS.items()
        }
        result = self.run_checker(
            "name: other\non: workflow_dispatch\njobs: {}\n",
            arguments=("--format", "json"),
            repo_marker=True,
            additional_workflows=files,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(json.loads(result.stdout)["consumer_contract_errors"], [])

        missing = dict(files)
        missing.pop("reusable-web-check.yml")
        result = self.run_checker(
            "name: other\non: workflow_dispatch\njobs: {}\n",
            arguments=("--format", "json"),
            repo_marker=True,
            additional_workflows=missing,
        )
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(
            ".github/workflows/reusable-web-check.yml",
            " ".join(json.loads(result.stdout)["consumer_contract_errors"]),
        )

        wrong_count = dict(files)
        wrong_count["ci.yml"] = workflow(2)
        result = self.run_checker(
            "name: other\non: workflow_dispatch\njobs: {}\n",
            arguments=("--format", "json"),
            repo_marker=True,
            additional_workflows=wrong_count,
        )
        self.assertEqual(result.returncode, 1, result.stdout)
        errors = " ".join(json.loads(result.stdout)["consumer_contract_errors"])
        self.assertIn("expected 3 uses", errors)
        self.assertIn("observed 2", errors)


if __name__ == "__main__":
    unittest.main()
