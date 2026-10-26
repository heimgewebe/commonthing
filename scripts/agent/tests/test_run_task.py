from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import inspect
import os
import json
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from scripts.agent import run_task
from scripts.agent.json_contract import load_json_strict, validate_instance
from scripts.agent.validate_handoff import validate_handoff
from scripts.docmeta.docmeta import REPO_ROOT
from scripts.docmeta.validate_claim_registry import load_registry


class TestRunTask(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(REPO_ROOT)
        self.source_revision = "1" * 40
        registry, findings, exit_code = load_registry(
            self.root / "docs/claims/registry.yml"
        )
        if exit_code != 0 or registry is None:
            raise AssertionError(f"claim registry must load for tests: {findings}")
        self.registry = registry

    def _fixture(self, name: str) -> str:
        return f"tests/fixtures/agent/{name}"

    def _fixture_path(self, name: str) -> Path:
        return self.root / self._fixture(name)

    def _run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "scripts.agent.run_task", *args],
            cwd=self.root,
            check=False,
            text=True,
            capture_output=True,
        )

    def _write_temp_task(self, data: object) -> str:
        handle = tempfile.NamedTemporaryFile(
            "w",
            suffix=".json",
            dir=self.root,
            delete=False,
            encoding="utf-8",
        )
        with handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        path = Path(handle.name)
        self.addCleanup(path.unlink, missing_ok=True)
        return str(path.relative_to(self.root))

    def _write_temp_raw(self, raw: str | bytes) -> str:
        mode = "wb" if isinstance(raw, bytes) else "w"
        kwargs = {} if isinstance(raw, bytes) else {"encoding": "utf-8"}
        handle = tempfile.NamedTemporaryFile(
            mode,
            suffix=".json",
            dir=self.root,
            delete=False,
            **kwargs,
        )
        with handle:
            handle.write(raw)
        path = Path(handle.name)
        self.addCleanup(path.unlink, missing_ok=True)
        return str(path.relative_to(self.root))

    @staticmethod
    def _init_git_repo(root: Path) -> None:
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"],
            cwd=root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=root,
            check=True,
        )
        seed = root / "seed.txt"
        seed.write_text("seed\n", encoding="utf-8")
        subprocess.run(["git", "add", "seed.txt"], cwd=root, check=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=root,
            check=True,
            capture_output=True,
        )

    @staticmethod
    def _parse_single_json_document(raw: str) -> object:
        decoder = json.JSONDecoder()
        payload, end = decoder.raw_decode(raw)
        if raw[end:].strip():
            raise AssertionError(f"stdout contains trailing non-JSON data: {raw[end:]}")
        return payload

    def _assert_valid_planned_result(self, result: dict, fixture: str) -> None:
        task_path = self.root / fixture
        task = load_json_strict(task_path)
        digest = hashlib.sha256(task_path.read_bytes()).hexdigest()

        self.assertEqual(result["mode"], "dry_run")
        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["task_file"], fixture)
        self.assertEqual(result["task_id"], task["task_id"])
        self.assertEqual(result["task_contract_sha256"], digest)
        self.assertEqual(result["source_revision"], self.source_revision)
        self.assertEqual(
            [item["name"] for item in result["stages"]], run_task.STAGE_NAMES
        )
        self.assertTrue(all(item["status"] == "passed" for item in result["stages"]))
        self.assertEqual(result["findings"], [])
        self.assertTrue(result["repository_unchanged"])

        plan = result["execution_plan"]
        self.assertEqual(plan["allowed_paths"], task["allowed_paths"])
        self.assertEqual(plan["forbidden_paths"], task["forbidden_paths"])
        self.assertEqual(plan["delete_allowed"], task["delete_allowed"])
        self.assertEqual(plan["planned_changed_paths"], [])
        self.assertEqual(plan["planned_deleted_paths"], [])

        handoff = result["handoff"]
        self.assertEqual(handoff["outcome"], "incomplete")
        self.assertEqual(handoff["changed_paths"], [])
        self.assertEqual(handoff["deleted_paths"], [])
        self.assertEqual(handoff["claims_addressed"], task["claims"])
        self.assertEqual(handoff["evidence_produced"], [])
        self.assertEqual(handoff["missing_evidence"], task["expected_evidence"])
        self.assertEqual(
            handoff["validation_results"],
            [
                {"command": command, "status": "not_run"}
                for command in task["validation_commands"]
            ],
        )
        self.assertEqual(handoff["blockers"], [])

        accounting = result["evidence_accounting"]
        self.assertEqual(accounting["expected_evidence"], task["expected_evidence"])
        self.assertEqual(accounting["evidence_produced"], [])
        self.assertEqual(accounting["missing_evidence"], task["expected_evidence"])

        findings = validate_handoff(
            task,
            handoff,
            task_bytes=task_path.read_bytes(),
            repo_root=self.root,
            claim_registry=self.registry,
        )
        self.assertEqual(findings, [])

    def test_positive_fixtures_plan_without_repository_changes(self):
        fixtures = (
            self._fixture("valid-doc-drift-task.json"),
            self._fixture("valid-roadmap-claim-task.json"),
            self._fixture("valid-generated-refresh-task.json"),
        )
        handoff_ids: set[str] = set()
        for fixture in fixtures:
            with self.subTest(fixture=fixture):
                before = run_task.repository_state_bytes(self.root)
                outcome = run_task._run_dry_run(
                    repo_root=self.root,
                    task_file=fixture,
                    source_revision_resolver=lambda _root: self.source_revision,
                )
                after = run_task.repository_state_bytes(self.root)
                self.assertEqual(before, after)
                self.assertEqual(outcome.exit_code, 0)
                self._assert_valid_planned_result(outcome.result, fixture)
                handoff_ids.add(outcome.result["handoff"]["handoff_id"])

        self.assertEqual(len(handoff_ids), len(fixtures))

    def test_generated_refresh_fixture_does_not_execute_generator_command(self):
        readiness = self.root / "docs/_generated/agent-readiness.md"
        before_bytes = readiness.read_bytes()
        outcome = run_task._run_dry_run(
            repo_root=self.root,
            task_file=self._fixture("valid-generated-refresh-task.json"),
            source_revision_resolver=lambda _root: self.source_revision,
        )
        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(readiness.read_bytes(), before_bytes)
        self.assertTrue(
            all(
                item["status"] == "not_run"
                for item in outcome.result["handoff"]["validation_results"]
            )
        )

    def test_cli_emits_single_json_document(self):
        proc = self._run_cli(
            "--dry-run",
            "--no-persist",
            self._fixture("valid-doc-drift-task.json"),
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, "")
        payload = self._parse_single_json_document(proc.stdout)
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["mode"], "dry_run")
        self.assertEqual(payload["status"], "planned")

    def test_output_bundle_contains_exact_bound_artifacts_without_absolute_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            output = base / "run"
            fixture = self._fixture("valid-doc-drift-task.json")
            outcome = run_task._run_dry_run(
                repo_root=self.root,
                task_file=fixture,
                source_revision_resolver=lambda _root: self.source_revision,
                output_dir=output,
                run_id_factory=lambda: "RUN-20990101T000000Z-abcdef123456",
            )
            self.assertEqual(outcome.exit_code, 0)
            self.assertEqual(
                sorted(path.name for path in output.iterdir()),
                sorted(run_task.EVIDENCE_FILES),
            )
            self.assertEqual(
                (output / "task.yml").read_bytes(), (self.root / fixture).read_bytes()
            )
            validation = load_json_strict(output / "validation.json")
            result = load_json_strict(output / "run-result.json")
            self.assertEqual(validation["run_id"], outcome.result["run_id"])
            self.assertEqual(result["run_id"], outcome.result["run_id"])
            self.assertEqual(
                result["task_contract_sha256"],
                hashlib.sha256((self.root / fixture).read_bytes()).hexdigest(),
            )
            self.assertEqual(
                result["artifacts"]["task"]["sha256"], result["task_contract_sha256"]
            )
            self.assertEqual(result["outcome"], result["handoff"]["outcome"])
            self.assertEqual(result["outcome"], "incomplete")
            self.assertLessEqual(result["started_at"], result["completed_at"])
            self.assertEqual(validation["created_at"], result["completed_at"])
            self.assertEqual(
                result["repository_state"]["source_revision"], self.source_revision
            )
            self.assertEqual(
                result["repository_state"]["git_visible_sha256"],
                validation["repository_state_sha256"],
            )
            with self.assertRaises(run_task.RunnerError) as traversal:
                run_task.validate_output_dir(self.root, Path("../outside"))
            self.assertEqual(traversal.exception.code, "OUTPUT_DIR_INVALID")
            for name in run_task.EVIDENCE_FILES:
                artifact_path = output / name
                self.assertNotIn(str(base).encode(), artifact_path.read_bytes())
                self.assertEqual(stat.S_IMODE(artifact_path.stat().st_mode), 0o600)

    def test_staging_reopens_original_inode_after_writes(self):
        # NixOS filesystems may return an empty stale stream for a directory
        # descriptor opened before children were written.
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "run"
            original_open = run_task.os.open
            original_listdir = run_task.os.listdir
            initial_staging_fd = None

            def observe_open(path, flags, *args, **kwargs):
                nonlocal initial_staging_fd
                descriptor = original_open(path, flags, *args, **kwargs)
                if (
                    initial_staging_fd is None
                    and isinstance(path, str)
                    and path.startswith(".agent-run-staging-")
                ):
                    initial_staging_fd = descriptor
                return descriptor

            def stale_initial_listdir(descriptor):
                if descriptor == initial_staging_fd:
                    return []
                return original_listdir(descriptor)

            with (
                unittest.mock.patch.object(run_task.os, "open", side_effect=observe_open),
                unittest.mock.patch.object(
                    run_task.os, "listdir", side_effect=stale_initial_listdir
                ),
            ):
                result = run_task._run_dry_run(
                    repo_root=self.root,
                    task_file=self._fixture("valid-doc-drift-task.json"),
                    source_revision_resolver=lambda _root: self.source_revision,
                    output_dir=output,
                )
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(
                sorted(path.name for path in output.iterdir()),
                sorted(run_task.EVIDENCE_FILES),
            )

    def test_staging_rejects_an_unexpected_fifth_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "run"
            original = run_task._write_file_sync_at

            def inject_extra_file(directory_fd, name, content):
                original(directory_fd, name, content)
                if name == "run-result.json":
                    original(directory_fd, "unexpected.txt", b"unexpected")

            with unittest.mock.patch.object(
                run_task, "_write_file_sync_at", side_effect=inject_extra_file
            ):
                with self.assertRaises(run_task.RunnerError) as ctx:
                    run_task._run_dry_run(
                        repo_root=self.root,
                        task_file=self._fixture("valid-doc-drift-task.json"),
                        source_revision_resolver=lambda _root: self.source_revision,
                        output_dir=output,
                    )
            self.assertEqual(ctx.exception.code, "RUN_EVIDENCE_INCOMPLETE")
            self.assertFalse(output.exists())
            self.assertEqual(list(Path(tmp).glob(".agent-run-staging-*")), [])

    def test_default_persistence_and_no_persist_are_distinct(self):
        run_id = "RUN-20990101T000001Z-abcdef123456"
        target = self.root / "artifacts" / "agent-runs" / run_id
        if target.exists():
            shutil.rmtree(target)
        self.addCleanup(shutil.rmtree, target, ignore_errors=True)

        outcome = run_task._run_dry_run(
            repo_root=self.root,
            task_file=self._fixture("valid-doc-drift-task.json"),
            source_revision_resolver=lambda _root: self.source_revision,
            persist=True,
            run_id_factory=lambda: run_id,
        )
        self.assertEqual(
            outcome.result["evidence_path"], f"artifacts/agent-runs/{run_id}"
        )
        self.assertEqual(
            sorted(path.name for path in target.iterdir()),
            sorted(run_task.EVIDENCE_FILES),
        )

        no_persist_id = "RUN-20990101T000002Z-abcdef123456"
        no_persist_target = self.root / "artifacts" / "agent-runs" / no_persist_id
        no_persist = run_task._run_dry_run(
            repo_root=self.root,
            task_file=self._fixture("valid-doc-drift-task.json"),
            source_revision_resolver=lambda _root: self.source_revision,
            persist=False,
            run_id_factory=lambda: no_persist_id,
        )
        self.assertNotIn("run_id", no_persist.result)
        self.assertFalse(no_persist_target.exists())

    def test_atomic_publish_never_replaces_existing_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "source"
            target = base / "target"
            source.mkdir()
            target.mkdir()
            (source / "new.txt").write_text("new\n", encoding="utf-8")
            (target / "sentinel.txt").write_text("keep\n", encoding="utf-8")
            parent_fd = run_task._open_directory_no_symlinks(base)
            try:
                with self.assertRaises(run_task.RunnerError) as ctx:
                    run_task._rename_noreplace_at(
                        parent_fd, source.name, parent_fd, target.name
                    )
            finally:
                os.close(parent_fd)

            self.assertEqual(ctx.exception.code, "OUTPUT_DIR_EXISTS")
            self.assertTrue((source / "new.txt").is_file())
            self.assertEqual(
                (target / "sentinel.txt").read_text(encoding="utf-8"),
                "keep\n",
            )

    def test_partial_write_does_not_publish_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            original = run_task._write_file_sync_at
            calls = 0

            def fail_second(directory_fd: int, name: str, data: bytes) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("synthetic write failure")
                original(directory_fd, name, data)

            with unittest.mock.patch.object(
                run_task, "_write_file_sync_at", side_effect=fail_second
            ):
                with self.assertRaises(run_task.RunnerError) as ctx:
                    run_task._run_dry_run(
                        repo_root=self.root,
                        task_file=self._fixture("valid-doc-drift-task.json"),
                        source_revision_resolver=lambda _root: self.source_revision,
                        output_dir=target,
                        run_id_factory=lambda: "RUN-20990101T000003Z-abcdef123456",
                    )
            self.assertEqual(ctx.exception.code, "OUTPUT_WRITE_FAILED")
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(tmp).glob(".agent-run-staging-*")), [])

    def test_staging_cleanup_failure_does_not_hide_write_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            with (
                unittest.mock.patch.object(
                    run_task,
                    "_write_file_sync_at",
                    side_effect=OSError("synthetic write failure"),
                ),
                unittest.mock.patch.object(
                    run_task,
                    "_cleanup_staging",
                    side_effect=OSError("synthetic staging cleanup failure"),
                ),
            ):
                with self.assertRaises(run_task.RunnerError) as ctx:
                    run_task._run_dry_run(
                        repo_root=self.root,
                        task_file=self._fixture("valid-doc-drift-task.json"),
                        source_revision_resolver=lambda _root: self.source_revision,
                        output_dir=target,
                        run_id_factory=lambda: "RUN-20990101T000005Z-abcdef123456",
                    )
            self.assertEqual(ctx.exception.code, "OUTPUT_WRITE_FAILED")
            self.assertTrue(
                any(
                    "synthetic staging cleanup failure" in item
                    for item in ctx.exception.cleanup_errors
                )
            )
            self.assertFalse(target.exists())

    def test_malformed_json_duplicate_keys_constants_and_utf8_fail_exit_two(self):
        cases = (
            ("{broken", "TASK_JSON_INVALID"),
            (
                '{"task_id":"AGENT-SAFE-004","task_id":"AGENT-SAFE-005"}',
                "TASK_JSON_DUPLICATE_KEY",
            ),
            ('{"value":NaN}', "TASK_JSON_INVALID"),
            ('{"value":Infinity}', "TASK_JSON_INVALID"),
            (b"\xff", "TASK_UTF8_INVALID"),
        )
        for raw, expected_code in cases:
            with self.subTest(expected_code=expected_code, raw=raw):
                rel = self._write_temp_raw(raw)
                proc = self._run_cli("--dry-run", rel)
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(proc.stdout, "")
                self.assertEqual(json.loads(proc.stderr)["code"], expected_code)

    def test_schema_invalid_task_blocks_with_truthful_stages(self):
        task = load_json_strict(self._fixture_path("valid-doc-drift-task.json"))
        task.pop("goal")
        rel = self._write_temp_task(task)

        proc = self._run_cli("--dry-run", rel)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stderr, "")
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["status"], "blocked")
        stages = {item["name"]: item["status"] for item in payload["stages"]}
        self.assertEqual(stages["capture_repository_state"], "passed")
        self.assertEqual(stages["load_task"], "passed")
        self.assertEqual(stages["validate_task_schema"], "blocked")
        self.assertEqual(stages["load_claim_registry"], "not_run")
        self.assertEqual(stages["resolve_source_revision"], "not_run")
        self.assertEqual(stages["verify_repository_unchanged"], "passed")
        self.assertEqual(stages["finalize_result"], "passed")
        self.assertIsNone(payload["source_revision"])
        self.assertTrue(payload["repository_unchanged"])
        self.assertIn(
            "TASK_SCHEMA_INVALID", {item["code"] for item in payload["findings"]}
        )

    def test_whitespace_only_field_blocks(self):
        task = load_json_strict(self._fixture_path("valid-doc-drift-task.json"))
        task["goal"] = "   "
        rel = self._write_temp_task(task)

        proc = self._run_cli("--dry-run", rel)
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertIn(
            "TASK_SCHEMA_INVALID", {item["code"] for item in payload["findings"]}
        )

    def test_non_ideal_and_unknown_claim_tasks_block(self):
        proc = self._run_cli(
            "--dry-run",
            self._fixture("invalid-forbidden-path.json"),
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn(
            "FORBIDDEN_PATH",
            {item["code"] for item in json.loads(proc.stdout)["findings"]},
        )

        task = load_json_strict(self._fixture_path("valid-roadmap-claim-task.json"))
        task["claims"] = ["CLAIM-DOES-NOT-EXIST-999"]
        rel = self._write_temp_task(task)
        proc = self._run_cli("--dry-run", rel)
        self.assertEqual(proc.returncode, 1)
        self.assertIn(
            "CLAIM_WITHOUT_REGISTRY_ENTRY",
            {item["code"] for item in json.loads(proc.stdout)["findings"]},
        )

    def test_absolute_parent_and_external_symlink_task_paths_fail_exit_two(self):
        absolute = str(self._fixture_path("valid-doc-drift-task.json").resolve())
        cases = (
            (absolute, "TASK_PATH_INVALID"),
            ("../outside.json", "TASK_PATH_INVALID"),
        )
        for task_arg, expected_code in cases:
            with self.subTest(task_arg=task_arg):
                proc = self._run_cli("--dry-run", task_arg)
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(json.loads(proc.stderr)["code"], expected_code)

        outside = Path(tempfile.NamedTemporaryFile(delete=False).name)
        self.addCleanup(outside.unlink, missing_ok=True)
        outside.write_bytes(
            self._fixture_path("valid-doc-drift-task.json").read_bytes()
        )
        link = self.root / "run-task-external-task-link.json"
        link.symlink_to(outside)
        self.addCleanup(link.unlink, missing_ok=True)

        proc = self._run_cli("--dry-run", str(link.relative_to(self.root)))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stderr)["code"], "TASK_PATH_INVALID")

    def test_public_runner_has_no_source_revision_override(self):
        parameters = inspect.signature(run_task.run_dry_run).parameters
        self.assertNotIn("source_revision", parameters)
        self.assertNotIn("source_revision_resolver", parameters)
        self.assertNotIn("repository_state_reader", parameters)

    def test_source_revision_unavailable_is_exit_two(self):
        stderr = io.StringIO()
        stdout = io.StringIO()
        with unittest.mock.patch.object(
            run_task,
            "resolve_git_head",
            side_effect=run_task.RunnerError(
                "SOURCE_REVISION_UNAVAILABLE",
                "no git head",
            ),
        ):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = run_task.main(
                    ["--dry-run", self._fixture("valid-doc-drift-task.json")]
                )
        self.assertEqual(rc, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            json.loads(stderr.getvalue())["code"], "SOURCE_REVISION_UNAVAILABLE"
        )

    def test_write_flag_is_invalid(self):
        proc = self._run_cli("--write", self._fixture("valid-doc-drift-task.json"))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(json.loads(proc.stderr)["code"], "INVALID_ARGUMENTS")

    def test_output_directory_rejections(self):
        in_repo = "tests/fixtures/agent/run-task-output"
        proc = self._run_cli(
            "--dry-run",
            "--output-dir",
            in_repo,
            self._fixture("valid-doc-drift-task.json"),
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stderr)["code"], "OUTPUT_DIR_IN_REPOSITORY")

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            target = base / "symlink-output"
            actual = base / "actual"
            target.symlink_to(actual)
            proc = self._run_cli(
                "--dry-run",
                "--output-dir",
                str(target),
                self._fixture("valid-doc-drift-task.json"),
            )
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(json.loads(proc.stderr)["code"], "OUTPUT_DIR_INVALID")

            non_empty = base / "non-empty"
            non_empty.mkdir()
            (non_empty / "existing.txt").write_text("x", encoding="utf-8")
            proc = self._run_cli(
                "--dry-run",
                "--output-dir",
                str(non_empty),
                self._fixture("valid-doc-drift-task.json"),
            )
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(json.loads(proc.stderr)["code"], "OUTPUT_DIR_EXISTS")

            file_target = base / "target-file"
            file_target.write_text("x", encoding="utf-8")
            proc = self._run_cli(
                "--dry-run",
                "--output-dir",
                str(file_target),
                self._fixture("valid-doc-drift-task.json"),
            )
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(json.loads(proc.stderr)["code"], "OUTPUT_DIR_EXISTS")

    def test_output_directory_rejects_parent_traversal_and_symlink_parent(self):
        with self.assertRaises(run_task.RunnerError) as traversal:
            run_task.validate_output_dir(self.root, Path("../outside-run"))
        self.assertEqual(traversal.exception.code, "OUTPUT_DIR_INVALID")

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            actual_parent = base / "actual-parent"
            actual_parent.mkdir()
            symlink_parent = base / "symlink-parent"
            symlink_parent.symlink_to(actual_parent, target_is_directory=True)

            with self.assertRaises(run_task.RunnerError) as symlink:
                run_task.validate_output_dir(self.root, symlink_parent / "run")
            self.assertEqual(symlink.exception.code, "OUTPUT_DIR_INVALID")

            existing_empty = base / "existing-empty"
            existing_empty.mkdir()
            with self.assertRaises(run_task.RunnerError) as existing:
                run_task.validate_output_dir(self.root, existing_empty)
            self.assertEqual(existing.exception.code, "OUTPUT_DIR_EXISTS")

    def test_default_evidence_root_rejects_symlink_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            repo.mkdir()
            outside = base / "outside"
            outside.mkdir()
            (repo / "artifacts").symlink_to(outside, target_is_directory=True)

            with self.assertRaises(run_task.RunnerError) as ctx:
                run_task._default_evidence_target(
                    repo, "RUN-20990101T000004Z-abcdef123456"
                )
            self.assertEqual(ctx.exception.code, "OUTPUT_DIR_INVALID")
            self.assertEqual(list(outside.iterdir()), [])

    def test_blocked_run_does_not_publish_requested_evidence(self):
        task = load_json_strict(self._fixture_path("valid-doc-drift-task.json"))
        task.pop("goal")
        rel = self._write_temp_task(task)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "blocked-run"
            outcome = run_task._run_dry_run(
                repo_root=self.root,
                task_file=rel,
                output_dir=target,
                source_revision_resolver=lambda _root: self.source_revision,
            )
            self.assertEqual(outcome.exit_code, 1)
            self.assertEqual(outcome.result["status"], "blocked")
            self.assertFalse(target.exists())

    def test_repository_drift_is_detected(self):
        states = [b"before", b"after"]

        def reader(_root: Path) -> bytes:
            return states.pop(0)

        with self.assertRaises(run_task.RunnerError) as ctx:
            run_task._run_dry_run(
                repo_root=self.root,
                task_file=self._fixture("valid-doc-drift-task.json"),
                source_revision_resolver=lambda _root: self.source_revision,
                repository_state_reader=reader,
            )
        self.assertEqual(ctx.exception.code, "REPO_MUTATED_DURING_DRY_RUN")

    def test_source_revision_change_is_detected(self):
        revisions = [self.source_revision, "2" * 40]

        def resolver(_root: Path) -> str:
            return revisions.pop(0)

        with self.assertRaises(run_task.RunnerError) as ctx:
            run_task._run_dry_run(
                repo_root=self.root,
                task_file=self._fixture("valid-doc-drift-task.json"),
                repository_state_reader=lambda _root: b"unchanged",
                source_revision_resolver=resolver,
            )
        self.assertEqual(ctx.exception.code, "SOURCE_REVISION_CHANGED_DURING_DRY_RUN")

    def test_repository_fingerprint_fails_closed_on_head_race(self):
        with (
            unittest.mock.patch.object(
                run_task, "resolve_git_head", side_effect=["1" * 40, "2" * 40]
            ),
            unittest.mock.patch.object(run_task, "_git_output", return_value=b""),
        ):
            with self.assertRaises(run_task.RunnerError) as ctx:
                run_task.repository_state_bytes(self.root)
        self.assertEqual(ctx.exception.code, "GIT_STATE_UNAVAILABLE")

    def test_repository_fingerprint_detects_head_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._init_git_repo(repo)
            before = run_task.repository_state_bytes(repo)
            subprocess.run(
                ["git", "commit", "--allow-empty", "-m", "second"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            after = run_task.repository_state_bytes(repo)
            self.assertNotEqual(before, after)

    def test_repository_fingerprint_detects_dirty_tracked_content_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._init_git_repo(repo)
            tracked = repo / "seed.txt"
            tracked.write_text("dirty-one\n", encoding="utf-8")
            before = run_task.repository_state_bytes(repo)
            tracked.write_text("dirty-two\n", encoding="utf-8")
            after = run_task.repository_state_bytes(repo)
            self.assertNotEqual(before, after)

    def test_repository_fingerprint_detects_untracked_content_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._init_git_repo(repo)
            untracked = repo / "untracked.txt"
            untracked.write_text("one\n", encoding="utf-8")
            before = run_task.repository_state_bytes(repo)
            untracked.write_text("two\n", encoding="utf-8")
            after = run_task.repository_state_bytes(repo)
            self.assertNotEqual(before, after)

    def test_blocked_task_is_covered_by_repository_guard(self):
        task = load_json_strict(self._fixture_path("valid-doc-drift-task.json"))
        task.pop("goal")
        rel = self._write_temp_task(task)
        states = [b"before", b"after"]

        def reader(_root: Path) -> bytes:
            return states.pop(0)

        with self.assertRaises(run_task.RunnerError) as ctx:
            run_task._run_dry_run(
                repo_root=self.root,
                task_file=rel,
                source_revision_resolver=lambda _root: self.source_revision,
                repository_state_reader=reader,
            )
        self.assertEqual(ctx.exception.code, "REPO_MUTATED_DURING_DRY_RUN")

    def test_prepublish_revision_drift_leaves_no_visible_bundle(self):
        revisions = [self.source_revision, self.source_revision, "2" * 40]

        def resolver(_root: Path) -> str:
            return revisions.pop(0)

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "output"
            with self.assertRaises(run_task.RunnerError) as ctx:
                run_task._run_dry_run(
                    repo_root=self.root,
                    task_file=self._fixture("valid-doc-drift-task.json"),
                    output_dir=output_dir,
                    repository_state_reader=lambda _root: b"unchanged",
                    source_revision_resolver=resolver,
                )
            self.assertFalse(output_dir.exists())
            self.assertEqual(list(Path(tmp).glob(".agent-run-staging-*")), [])
        self.assertEqual(ctx.exception.code, "SOURCE_REVISION_CHANGED_DURING_DRY_RUN")
        self.assertEqual(ctx.exception.cleanup_errors, [])

    def test_clock_regression_blocks_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "output"
            with unittest.mock.patch.object(
                run_task,
                "_utc_timestamp",
                side_effect=[
                    "2026-06-27T10:00:02.000000Z",
                    "2026-06-27T10:00:01.000000Z",
                ],
            ):
                with self.assertRaises(run_task.RunnerError) as ctx:
                    run_task._run_dry_run(
                        repo_root=self.root,
                        task_file=self._fixture("valid-doc-drift-task.json"),
                        output_dir=output_dir,
                        source_revision_resolver=lambda _root: self.source_revision,
                    )
            self.assertEqual(ctx.exception.code, "CLOCK_MOVED_BACKWARDS")
            self.assertFalse(output_dir.exists())

    def test_output_parent_swap_to_symlink_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            parent = base / "parent"
            parent.mkdir()
            outside = base / "outside"
            outside.mkdir()
            output_dir = parent / "output"
            original = run_task._open_directory_no_symlinks
            swapped = False

            def swap_before_open(path: Path) -> int:
                nonlocal swapped
                if path == parent and not swapped:
                    swapped = True
                    parent.rmdir()
                    parent.symlink_to(outside, target_is_directory=True)
                return original(path)

            with unittest.mock.patch.object(
                run_task,
                "_open_directory_no_symlinks",
                side_effect=swap_before_open,
            ):
                with self.assertRaises(run_task.RunnerError) as ctx:
                    run_task._run_dry_run(
                        repo_root=self.root,
                        task_file=self._fixture("valid-doc-drift-task.json"),
                        output_dir=output_dir,
                        source_revision_resolver=lambda _root: self.source_revision,
                    )
            self.assertEqual(ctx.exception.code, "OUTPUT_DIR_INVALID")
            self.assertEqual(list(outside.iterdir()), [])

    def test_git_environment_cannot_redirect_repository_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            other = base / "other"
            repo.mkdir()
            other.mkdir()
            self._init_git_repo(repo)
            self._init_git_repo(other)
            (other / "other.txt").write_text("other\n", encoding="utf-8")
            subprocess.run(["git", "add", "other.txt"], cwd=other, check=True)
            subprocess.run(
                ["git", "commit", "-m", "other state"],
                cwd=other,
                check=True,
                capture_output=True,
            )
            expected_head = run_task.resolve_git_head(repo)
            expected_state = run_task.repository_state_bytes(repo)
            hostile_environment = {
                "GIT_DIR": str(other / ".git"),
                "GIT_WORK_TREE": str(other),
                "GIT_INDEX_FILE": str(other / ".git" / "index"),
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.worktree",
                "GIT_CONFIG_VALUE_0": str(other),
            }
            with unittest.mock.patch.dict(
                os.environ, hostile_environment, clear=False
            ):
                self.assertEqual(run_task.resolve_git_head(repo), expected_head)
                self.assertEqual(run_task.repository_state_bytes(repo), expected_state)

    def test_parent_move_after_open_is_reported_after_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            parent = base / "parent"
            moved = base / "moved"
            parent.mkdir()
            output = parent / "run"
            original = run_task._rename_noreplace_at

            def move_parent_then_publish(*args, **kwargs):
                parent.rename(moved)
                parent.mkdir()
                return original(*args, **kwargs)

            with unittest.mock.patch.object(
                run_task,
                "_rename_noreplace_at",
                side_effect=move_parent_then_publish,
            ):
                with self.assertRaises(run_task.RunnerError) as ctx:
                    run_task._run_dry_run(
                        repo_root=self.root,
                        task_file=self._fixture("valid-doc-drift-task.json"),
                        output_dir=output,
                        repository_state_reader=lambda _root: b"unchanged",
                        source_revision_resolver=lambda _root: self.source_revision,
                    )
            self.assertEqual(ctx.exception.code, "OUTPUT_LOCATION_CHANGED")
            self.assertEqual(ctx.exception.evidence_path, str(output))
            self.assertFalse(output.exists())
            self.assertTrue((moved / "run").is_dir())

    def test_close_failure_after_publish_is_not_reported_as_write_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "run"
            original_open = run_task.os.open
            original_close = run_task.os.close
            staging_descriptor: int | None = None
            published = False

            def capture_staging_open(path, flags, *args, **kwargs):
                nonlocal staging_descriptor
                descriptor = original_open(path, flags, *args, **kwargs)
                if isinstance(path, str) and path.startswith(".agent-run-staging-"):
                    staging_descriptor = descriptor
                return descriptor

            original_rename = run_task._rename_noreplace_at

            def publish_then_arm_close(*args, **kwargs):
                nonlocal published
                result = original_rename(*args, **kwargs)
                published = True
                return result

            def fail_staging_close(descriptor: int) -> None:
                if published and descriptor == staging_descriptor:
                    raise OSError("synthetic staging close failure")
                original_close(descriptor)

            with (
                unittest.mock.patch.object(
                    run_task.os, "open", side_effect=capture_staging_open
                ),
                unittest.mock.patch.object(
                    run_task,
                    "_rename_noreplace_at",
                    side_effect=publish_then_arm_close,
                ),
                unittest.mock.patch.object(
                    run_task.os, "close", side_effect=fail_staging_close
                ),
            ):
                with self.assertRaises(run_task.RunnerError) as ctx:
                    run_task._run_dry_run(
                        repo_root=self.root,
                        task_file=self._fixture("valid-doc-drift-task.json"),
                        output_dir=output,
                        repository_state_reader=lambda _root: b"unchanged",
                        source_revision_resolver=lambda _root: self.source_revision,
                    )
            self.assertEqual(ctx.exception.code, "OUTPUT_FINALIZATION_UNCONFIRMED")
            self.assertEqual(ctx.exception.evidence_path, str(output))
            self.assertTrue(output.is_dir())
            self.assertTrue(
                any(
                    "synthetic staging close failure" in item
                    for item in ctx.exception.cleanup_errors
                )
            )

    def test_target_replacement_after_publish_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            output = base / "run"
            moved = base / "moved-run"
            original = run_task._assert_published_parent_identity

            def replace_target_then_check(parent, parent_fd, *, evidence_path):
                output.rename(moved)
                output.mkdir()
                return original(parent, parent_fd, evidence_path=evidence_path)

            with unittest.mock.patch.object(
                run_task,
                "_assert_published_parent_identity",
                side_effect=replace_target_then_check,
            ):
                with self.assertRaises(run_task.RunnerError) as ctx:
                    run_task._run_dry_run(
                        repo_root=self.root,
                        task_file=self._fixture("valid-doc-drift-task.json"),
                        output_dir=output,
                        repository_state_reader=lambda _root: b"unchanged",
                        source_revision_resolver=lambda _root: self.source_revision,
                    )
            self.assertEqual(ctx.exception.code, "OUTPUT_LOCATION_CHANGED")
            self.assertEqual(ctx.exception.evidence_path, str(output))
            self.assertEqual(list(output.iterdir()), [])
            self.assertEqual(
                sorted(path.name for path in moved.iterdir()),
                sorted(run_task.EVIDENCE_FILES),
            )

    def test_repository_fingerprint_ignores_global_git_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            repo.mkdir()
            self._init_git_repo(repo)
            home = base / "home"
            home.mkdir()
            (home / "global-ignore").write_text(
                ".claude/settings.local.json\n", encoding="utf-8"
            )
            (home / ".gitconfig").write_text(
                "[core]\n\texcludesfile = " + str(home / "global-ignore") + "\n",
                encoding="utf-8",
            )
            before = run_task.repository_state_bytes(repo)
            with unittest.mock.patch.dict(os.environ, {"HOME": str(home)}):
                configured_before = run_task.repository_state_bytes(repo)
            self.assertEqual(before, configured_before)

            hidden = repo / ".claude" / "settings.local.json"
            hidden.parent.mkdir()
            hidden.write_text('{"permissions":{"allow":["Bash(*)"]}}\n', encoding="utf-8")
            with unittest.mock.patch.dict(os.environ, {"HOME": str(home)}):
                after = run_task.repository_state_bytes(repo)
            self.assertNotEqual(before, after)

    def test_evidence_schemas_reject_hollow_or_fabricated_payloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "run"
            run_task._run_dry_run(
                repo_root=self.root,
                task_file=self._fixture("valid-doc-drift-task.json"),
                source_revision_resolver=lambda _root: self.source_revision,
                output_dir=output,
            )
            result = load_json_strict(output / "run-result.json")
            result_schema = load_json_strict(
                self.root / "contracts/agent/run-result.schema.json"
            )
            for field, value in (
                ("handoff", {}),
                ("execution_plan", {}),
                ("evidence_accounting", {}),
                ("stages", ["not-an-object"]),
                ("findings", ["fabricated"]),
            ):
                candidate = copy.deepcopy(result)
                candidate[field] = value
                self.assertTrue(
                    validate_instance(candidate, result_schema),
                    msg=f"schema accepted invalid {field}",
                )

            validation = load_json_strict(output / "validation.json")
            validation_schema = load_json_strict(
                self.root / "contracts/agent/validation.schema.json"
            )
            validation["checks"] = [
                {"name": f"arbitrary_{index}", "status": "passed"}
                for index in range(5)
            ]
            self.assertTrue(validate_instance(validation, validation_schema))

            result = load_json_strict(output / "run-result.json")
            result["stages"] = list(reversed(result["stages"]))
            self.assertTrue(validate_instance(result, result_schema))

            validation = load_json_strict(output / "validation.json")
            validation["created_at"] = "2026-02-31T10:00:00.000000Z"
            self.assertTrue(validate_instance(validation, validation_schema))

    def test_impossible_calendar_timestamp_blocks_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "run"
            with unittest.mock.patch.object(
                run_task,
                "_utc_timestamp",
                side_effect=[
                    "2026-02-29T10:00:00.000000Z",
                    "2026-02-29T10:00:01.000000Z",
                ],
            ):
                with self.assertRaises(run_task.RunnerError) as ctx:
                    run_task._run_dry_run(
                        repo_root=self.root,
                        task_file=self._fixture("valid-doc-drift-task.json"),
                        output_dir=output,
                        run_id_factory=lambda: "RUN-20260227T100001Z-abcdef123456",
                        repository_state_reader=lambda _root: b"unchanged",
                        source_revision_resolver=lambda _root: self.source_revision,
                    )
            self.assertEqual(ctx.exception.code, "RUN_EVIDENCE_SEMANTIC_INVALID")
            self.assertFalse(output.exists())

    def test_parent_fsync_failure_reports_published_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "run"
            original = run_task._fsync_fd
            calls = 0

            def fail_parent_sync(descriptor: int) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("synthetic parent fsync failure")
                original(descriptor)

            with unittest.mock.patch.object(
                run_task, "_fsync_fd", side_effect=fail_parent_sync
            ):
                with self.assertRaises(run_task.RunnerError) as ctx:
                    run_task._run_dry_run(
                        repo_root=self.root,
                        task_file=self._fixture("valid-doc-drift-task.json"),
                        output_dir=output,
                        source_revision_resolver=lambda _root: self.source_revision,
                    )
            self.assertEqual(ctx.exception.code, "OUTPUT_DURABILITY_UNCONFIRMED")
            self.assertTrue(output.is_dir())
            self.assertEqual(
                sorted(path.name for path in output.iterdir()),
                sorted(run_task.EVIDENCE_FILES),
            )
            self.assertTrue(
                any(
                    "synthetic parent fsync failure" in item
                    for item in ctx.exception.cleanup_errors
                )
            )

    def test_cross_artifact_binding_mismatch_blocks_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "run"
            original = run_task._validation_payload

            def mismatched_validation(**kwargs):
                payload = original(**kwargs)
                payload["task_id"] = "AGENT-SAFE-999"
                return payload

            with unittest.mock.patch.object(
                run_task, "_validation_payload", side_effect=mismatched_validation
            ):
                with self.assertRaises(run_task.RunnerError) as ctx:
                    run_task._run_dry_run(
                        repo_root=self.root,
                        task_file=self._fixture("valid-doc-drift-task.json"),
                        output_dir=output,
                        source_revision_resolver=lambda _root: self.source_revision,
                    )
            self.assertEqual(ctx.exception.code, "RUN_EVIDENCE_BINDING_INVALID")
            self.assertFalse(output.exists())

    def test_error_output_exposes_cleanup_failures(self):
        error = run_task.RunnerError("OUTPUT_WRITE_FAILED", "write failed")
        error.evidence_path = "/tmp/published-run"
        error.cleanup_errors.append("staging cleanup failed")
        with (
            unittest.mock.patch.object(run_task, "run_dry_run", side_effect=error),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            exit_code = run_task.main(
                ["--dry-run", "--no-persist", self._fixture("valid-doc-drift-task.json")]
            )
        self.assertEqual(exit_code, 2)
        payload = json.loads(stderr.getvalue())
        self.assertEqual(payload["evidence_path"], "/tmp/published-run")
        self.assertEqual(payload["cleanup_errors"], ["staging cleanup failed"])


    def test_invalid_generated_handoff_is_operational_error(self):
        with unittest.mock.patch.object(
            run_task,
            "validate_handoff",
            return_value=[{"code": "TEST", "message": "synthetic failure"}],
        ):
            with self.assertRaises(run_task.RunnerError) as ctx:
                run_task._run_dry_run(
                    repo_root=self.root,
                    task_file=self._fixture("valid-doc-drift-task.json"),
                    source_revision_resolver=lambda _root: self.source_revision,
                )
        self.assertEqual(ctx.exception.code, "HANDOFF_VALIDATION_FAILED")

    def test_incomplete_evidence_accounting_is_operational_error(self):
        with unittest.mock.patch.object(
            run_task,
            "_evidence_accounting",
            return_value={
                "expected_evidence": ["docs/tasks/board.md"],
                "evidence_produced": [],
                "missing_evidence": [],
            },
        ):
            with self.assertRaises(run_task.RunnerError) as ctx:
                run_task._run_dry_run(
                    repo_root=self.root,
                    task_file=self._fixture("valid-doc-drift-task.json"),
                    source_revision_resolver=lambda _root: self.source_revision,
                )
        self.assertEqual(ctx.exception.code, "EVIDENCE_ACCOUNTING_INCOMPLETE")

    def test_incomplete_validation_accounting_is_rejected_by_handoff_validator(self):
        original_handoff = run_task._handoff

        def incomplete_handoff(
            task: dict[str, object], task_digest: str, source_revision: str
        ) -> dict[str, object]:
            handoff = original_handoff(task, task_digest, source_revision)
            handoff["validation_results"] = handoff["validation_results"][:-1]
            return handoff

        with unittest.mock.patch.object(
            run_task, "_handoff", side_effect=incomplete_handoff
        ):
            with self.assertRaises(run_task.RunnerError) as ctx:
                run_task._run_dry_run(
                    repo_root=self.root,
                    task_file=self._fixture("valid-doc-drift-task.json"),
                    source_revision_resolver=lambda _root: self.source_revision,
                )
        self.assertEqual(ctx.exception.code, "HANDOFF_VALIDATION_FAILED")


if __name__ == "__main__":
    unittest.main()
