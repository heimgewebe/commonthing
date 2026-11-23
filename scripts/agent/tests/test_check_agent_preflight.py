"""
Tests für scripts/agent/check_agent_preflight.py (AGENT-SAFE-001).

Alle Checks werden mit temporären Fixtures getestet; echte Repo-Dateien
werden nicht als alleinige Testgrundlage verwendet und nie geschrieben.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.agent.check_agent_preflight import (
    _parse_simple_yaml,
    check_delete_without_permission,
    check_generated_direct_edit,
    check_infra_change_without_proof,
    check_path_scope,
    check_roadmap_done_without_claim,
    check_status_done_without_proof,
    check_task_metadata,
    check_workflow_change_task_type,
    main,
    run_preflight,
)


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------


def _minimal_task(**overrides) -> dict:
    task = {
        "task_id": "AGENT-SAFE-001",
        "task_type": "doc_change",
        "allowed_paths": ["docs/", "scripts/agent/"],
        "validation": ["make ci-validate"],
        "expected_evidence": ["docs/security/agent-write-scope-baseline.md"],
    }
    task.update(overrides)
    return task


def _write_yaml(content: str) -> str:
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    )
    f.write(content)
    f.close()
    return f.name


def _write_md(content: str, suffix: str = ".md") -> str:
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=suffix, delete=False, encoding="utf-8"
    )
    f.write(content)
    f.close()
    return f.name


# ---------------------------------------------------------------------------
# _parse_simple_yaml
# ---------------------------------------------------------------------------


class TestParseSimpleYaml(unittest.TestCase):
    def test_parses_scalar_fields(self):
        path = _write_yaml("task_id: TASK-CTL-001\ntask_type: doc_change\n")
        try:
            data = _parse_simple_yaml(path)
            self.assertEqual(data["task_id"], "TASK-CTL-001")
            self.assertEqual(data["task_type"], "doc_change")
        finally:
            os.unlink(path)

    def test_parses_frontmatter_block(self):
        path = _write_yaml(
            "---\ntask_id: OPT-API-001\ntask_type: ci_change\n---\n"
        )
        try:
            data = _parse_simple_yaml(path)
            self.assertEqual(data["task_id"], "OPT-API-001")
        finally:
            os.unlink(path)

    def test_parses_inline_list(self):
        path = _write_yaml(
            "allowed_paths: [docs/, scripts/]\n"
        )
        try:
            data = _parse_simple_yaml(path)
            self.assertEqual(data["allowed_paths"], ["docs/", "scripts/"])
        finally:
            os.unlink(path)

    def test_parses_block_list(self):
        path = _write_yaml(
            "allowed_paths:\n  - docs/\n  - scripts/\n"
        )
        try:
            data = _parse_simple_yaml(path)
            self.assertEqual(data["allowed_paths"], ["docs/", "scripts/"])
        finally:
            os.unlink(path)

    def test_parses_delete_allowed_true(self):
        path = _write_yaml("delete_allowed: true\n")
        try:
            data = _parse_simple_yaml(path)
            self.assertEqual(data["delete_allowed"], "true")
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# check_task_metadata
# ---------------------------------------------------------------------------


class TestCheckTaskMetadata(unittest.TestCase):
    def test_valid_task_has_no_findings(self):
        self.assertEqual(check_task_metadata(_minimal_task()), [])

    def test_missing_task_id(self):
        task = _minimal_task(task_id="")
        codes = [f["code"] for f in check_task_metadata(task)]
        self.assertIn("MISSING_TASK_ID", codes)

    def test_invalid_task_id_format(self):
        task = _minimal_task(task_id="agent-safe-001")
        codes = [f["code"] for f in check_task_metadata(task)]
        self.assertIn("MISSING_TASK_ID", codes)

    def test_missing_task_type(self):
        task = _minimal_task(task_type="")
        codes = [f["code"] for f in check_task_metadata(task)]
        self.assertIn("MISSING_TASK_TYPE", codes)

    def test_missing_allowed_paths(self):
        task = _minimal_task(allowed_paths=[])
        codes = [f["code"] for f in check_task_metadata(task)]
        self.assertIn("MISSING_ALLOWED_PATHS", codes)

    def test_missing_validation(self):
        task = _minimal_task()
        del task["validation"]
        codes = [f["code"] for f in check_task_metadata(task)]
        self.assertIn("MISSING_VALIDATION", codes)

    def test_validation_commands_accepted(self):
        task = _minimal_task()
        del task["validation"]
        task["validation_commands"] = ["make ci-validate"]
        self.assertEqual(check_task_metadata(task), [])

    def test_missing_evidence(self):
        task = _minimal_task()
        del task["expected_evidence"]
        codes = [f["code"] for f in check_task_metadata(task)]
        self.assertIn("MISSING_EXPECTED_EVIDENCE", codes)

    def test_evidence_field_accepted(self):
        task = _minimal_task()
        del task["expected_evidence"]
        task["evidence"] = ["some/proof.md"]
        self.assertEqual(check_task_metadata(task), [])

    def test_multiple_missing_fields_all_reported(self):
        task = {"allowed_paths": ["docs/"]}
        codes = [f["code"] for f in check_task_metadata(task)]
        self.assertIn("MISSING_TASK_ID", codes)
        self.assertIn("MISSING_TASK_TYPE", codes)
        self.assertIn("MISSING_VALIDATION", codes)
        self.assertIn("MISSING_EXPECTED_EVIDENCE", codes)


# ---------------------------------------------------------------------------
# check_path_scope
# ---------------------------------------------------------------------------


class TestCheckPathScope(unittest.TestCase):
    def test_path_inside_allowed(self):
        self.assertEqual(
            check_path_scope(["docs/foo.md"], ["docs/"]), []
        )

    def test_path_outside_allowed(self):
        findings = check_path_scope(["scripts/deploy.sh"], ["docs/"])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["code"], "PATH_OUT_OF_SCOPE")
        self.assertIn("scripts/deploy.sh", findings[0]["message"])

    def test_multiple_paths_mixed(self):
        findings = check_path_scope(
            ["docs/foo.md", "apps/web/index.ts"], ["docs/"]
        )
        codes = [f["code"] for f in findings]
        self.assertEqual(codes, ["PATH_OUT_OF_SCOPE"])
        self.assertIn("apps/web/index.ts", findings[0]["path"])

    def test_empty_allowed_paths_no_findings(self):
        self.assertEqual(
            check_path_scope(["docs/foo.md"], []), []
        )

    def test_windows_backslash_paths_normalized(self):
        findings = check_path_scope(["docs\\foo.md"], ["docs/"])
        self.assertEqual(findings, [])

    def test_docs_bad_is_not_allowed_by_docs_prefix(self):
        findings = check_path_scope(["docs_bad/foo.md"], ["docs/"])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["code"], "PATH_OUT_OF_SCOPE")


# ---------------------------------------------------------------------------
# check_generated_direct_edit
# ---------------------------------------------------------------------------


class TestCheckGeneratedDirectEdit(unittest.TestCase):
    def test_generated_path_flagged(self):
        findings = check_generated_direct_edit(["docs/_generated/doc-index.md"])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["code"], "GENERATED_DIRECT_EDIT")

    def test_regular_docs_path_not_flagged(self):
        self.assertEqual(
            check_generated_direct_edit(["docs/security/agent-write-scope-baseline.md"]),
            [],
        )

    def test_multiple_generated_all_flagged(self):
        paths = ["docs/_generated/a.md", "docs/_generated/b.md"]
        self.assertEqual(len(check_generated_direct_edit(paths)), 2)


# ---------------------------------------------------------------------------
# check_roadmap_done_without_claim
# ---------------------------------------------------------------------------


class TestCheckRoadmapDoneWithoutClaim(unittest.TestCase):
    def test_done_checkbox_without_claim_flagged(self):
        """[x] ohne Claim in docs/roadmap.md wird erkannt."""
        import shutil
        cwd = os.getcwd()
        tmpdir = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(tmpdir, "docs"))
            with open(os.path.join(tmpdir, "docs", "roadmap.md"), "w", encoding="utf-8") as f:
                f.write("- [x] Implementiert\n")
            os.chdir(tmpdir)
            findings = check_roadmap_done_without_claim(["docs/roadmap.md"])
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0]["code"], "ROADMAP_DONE_WITHOUT_CLAIM")
        finally:
            os.chdir(cwd)
            shutil.rmtree(tmpdir)

    def test_done_checkbox_in_docs_path_flagged(self):
        """Alias-Test: gleicher positiver Fund."""
        import shutil
        cwd = os.getcwd()
        tmpdir = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(tmpdir, "docs"))
            with open(os.path.join(tmpdir, "docs", "roadmap.md"), "w", encoding="utf-8") as f:
                f.write("- [x] Implementiert\n")
            os.chdir(tmpdir)
            findings = check_roadmap_done_without_claim(["docs/roadmap.md"])
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0]["code"], "ROADMAP_DONE_WITHOUT_CLAIM")
        finally:
            os.chdir(cwd)
            shutil.rmtree(tmpdir)

    def test_done_checkbox_with_proof_not_flagged(self):
        """[x] mit proof_ref in docs/roadmap.md wird nicht gemeldet."""
        import shutil
        cwd = os.getcwd()
        tmpdir = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(tmpdir, "docs"))
            with open(os.path.join(tmpdir, "docs", "roadmap.md"), "w", encoding="utf-8") as f:
                f.write("- [x] Implementiert proof_ref: PR#42\n")
            os.chdir(tmpdir)
            findings = check_roadmap_done_without_claim(["docs/roadmap.md"])
            self.assertEqual(findings, [])
        finally:
            os.chdir(cwd)
            shutil.rmtree(tmpdir)

    def test_non_docs_path_not_flagged(self):
        """Absoluter oder nicht-docs/ Pfad mit [x] wird nicht gemeldet."""
        path = _write_md("- [x] Implementiert\n")
        try:
            findings = check_roadmap_done_without_claim([path])
            self.assertEqual(findings, [])
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# check_status_done_without_proof
# ---------------------------------------------------------------------------


class TestCheckStatusDoneWithoutProof(unittest.TestCase):
    def test_status_done_without_proof_flagged(self):
        import shutil
        cwd = os.getcwd()
        tmpdir = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(tmpdir, "docs", "tasks"))
            with open(os.path.join(tmpdir, "docs", "tasks", "task.yaml"), "w", encoding="utf-8") as f:
                f.write("status: done\ntitle: Foo\n")
            os.chdir(tmpdir)
            findings = check_status_done_without_proof(["docs/tasks/task.yaml"])
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0]["code"], "STATUS_DONE_WITHOUT_PROOF")
        finally:
            os.chdir(cwd)
            shutil.rmtree(tmpdir)

    def test_status_done_with_proof_not_flagged(self):
        import shutil
        cwd = os.getcwd()
        tmpdir = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(tmpdir, "docs", "reports"))
            with open(os.path.join(tmpdir, "docs", "reports", "status.yaml"), "w", encoding="utf-8") as f:
                f.write("status: done\nproof_ref: PR#42\n")
            os.chdir(tmpdir)
            findings = check_status_done_without_proof(["docs/reports/status.yaml"])
            self.assertEqual(findings, [])
        finally:
            os.chdir(cwd)
            shutil.rmtree(tmpdir)

    def test_status_open_not_flagged(self):
        import shutil
        cwd = os.getcwd()
        tmpdir = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(tmpdir, "docs", "tasks"))
            with open(os.path.join(tmpdir, "docs", "tasks", "task.yaml"), "w", encoding="utf-8") as f:
                f.write("status: open\ntitle: Foo\n")
            os.chdir(tmpdir)
            findings = check_status_done_without_proof(["docs/tasks/task.yaml"])
            self.assertEqual(findings, [])
        finally:
            os.chdir(cwd)
            shutil.rmtree(tmpdir)

    def test_status_done_next_line_has_evidence(self):
        import shutil
        cwd = os.getcwd()
        tmpdir = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(tmpdir, "docs", "tasks"))
            with open(os.path.join(tmpdir, "docs", "tasks", "task.yaml"), "w", encoding="utf-8") as f:
                f.write("status: done\nevidence: some/proof.md\n")
            os.chdir(tmpdir)
            findings = check_status_done_without_proof(["docs/tasks/task.yaml"])
            self.assertEqual(findings, [])
        finally:
            os.chdir(cwd)
            shutil.rmtree(tmpdir)

    def test_json_status_done_without_proof_flagged(self):
        import shutil
        cwd = os.getcwd()
        tmpdir = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(tmpdir, "docs", "tasks"))
            with open(os.path.join(tmpdir, "docs", "tasks", "index.json"), "w", encoding="utf-8") as f:
                f.write('{"status": "done", "title": "Foo"}\n')
            os.chdir(tmpdir)
            findings = check_status_done_without_proof(["docs/tasks/index.json"])
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0]["code"], "STATUS_DONE_WITHOUT_PROOF")
        finally:
            os.chdir(cwd)
            shutil.rmtree(tmpdir)

    def test_status_done_outside_scope_not_flagged(self):
        path = _write_yaml("status: done\ntitle: Foo\n")
        try:
            findings = check_status_done_without_proof([path])
            self.assertEqual(findings, [])
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# check_workflow_change_task_type
# ---------------------------------------------------------------------------


class TestCheckWorkflowChangeTaskType(unittest.TestCase):
    def test_workflow_change_with_ci_change_ok(self):
        self.assertEqual(
            check_workflow_change_task_type(
                [".github/workflows/ci.yml"], "ci_change"
            ),
            [],
        )

    def test_workflow_change_without_ci_change_flagged(self):
        findings = check_workflow_change_task_type(
            [".github/workflows/ci.yml"], "doc_change"
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["code"], "WORKFLOW_CHANGE_WITHOUT_TASK_TYPE")

    def test_no_workflow_change_no_finding(self):
        self.assertEqual(
            check_workflow_change_task_type(["docs/foo.md"], "doc_change"), []
        )


# ---------------------------------------------------------------------------
# check_infra_change_without_proof
# ---------------------------------------------------------------------------


class TestCheckInfraChangeWithoutProof(unittest.TestCase):
    def test_infra_change_with_proper_task_and_proof_ok(self):
        task = _minimal_task(
            task_type="infra_change",
            expected_evidence=["docs/deploy/CHANGELOG.md"],
        )
        self.assertEqual(
            check_infra_change_without_proof(["infra/compose/compose.core.yml"], task),
            [],
        )

    def test_infra_change_without_task_type_flagged(self):
        task = _minimal_task(task_type="doc_change")
        findings = check_infra_change_without_proof(
            ["infra/compose/compose.core.yml"], task
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["code"], "INFRA_CHANGE_WITHOUT_PROOF")

    def test_infra_change_without_proof_flagged(self):
        task = _minimal_task(task_type="infra_change")
        del task["expected_evidence"]
        findings = check_infra_change_without_proof(
            ["infra/caddy/Caddyfile"], task
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["code"], "INFRA_CHANGE_WITHOUT_PROOF")

    def test_deploy_change_type_accepted(self):
        task = _minimal_task(
            task_type="deploy_change",
            expected_evidence=["docs/deploy/CHANGELOG.md"],
        )
        self.assertEqual(
            check_infra_change_without_proof(["infra/compose/compose.core.yml"], task),
            [],
        )

    def test_non_infra_path_not_flagged(self):
        task = _minimal_task(task_type="doc_change")
        self.assertEqual(
            check_infra_change_without_proof(["docs/foo.md"], task), []
        )


# ---------------------------------------------------------------------------
# check_delete_without_permission
# ---------------------------------------------------------------------------


class TestCheckDeleteWithoutPermission(unittest.TestCase):
    def test_delete_without_permission_flagged(self):
        task = _minimal_task(delete_allowed="false")
        findings = check_delete_without_permission(["docs/old.md"], task)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["code"], "DELETE_WITHOUT_PERMISSION")

    def test_delete_with_permission_ok(self):
        task = _minimal_task(delete_allowed="true")
        self.assertEqual(
            check_delete_without_permission(["docs/old.md"], task), []
        )

    def test_no_deleted_paths_no_finding(self):
        task = _minimal_task()
        self.assertEqual(check_delete_without_permission([], task), [])

    def test_delete_allowed_default_is_false(self):
        task = _minimal_task()
        findings = check_delete_without_permission(["docs/old.md"], task)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["code"], "DELETE_WITHOUT_PERMISSION")


# ---------------------------------------------------------------------------
# run_preflight (Integrationstest)
# ---------------------------------------------------------------------------


class TestRunPreflight(unittest.TestCase):
    def test_clean_task_no_findings(self):
        task = _minimal_task()
        self.assertEqual(run_preflight(task), [])

    def test_generated_edit_detected(self):
        task = _minimal_task(
            allowed_paths=["docs/_generated/", "scripts/agent/"]
        )
        findings = run_preflight(
            task, changed_paths=["docs/_generated/doc-index.md"]
        )
        codes = [f["code"] for f in findings]
        self.assertIn("GENERATED_DIRECT_EDIT", codes)

    def test_path_out_of_scope_detected(self):
        task = _minimal_task()
        findings = run_preflight(task, changed_paths=["apps/web/index.ts"])
        codes = [f["code"] for f in findings]
        self.assertIn("PATH_OUT_OF_SCOPE", codes)

    def test_all_missing_fields_reported(self):
        task = {}
        findings = run_preflight(task)
        codes = [f["code"] for f in findings]
        self.assertIn("MISSING_TASK_ID", codes)
        self.assertIn("MISSING_TASK_TYPE", codes)
        self.assertIn("MISSING_ALLOWED_PATHS", codes)
        self.assertIn("MISSING_VALIDATION", codes)
        self.assertIn("MISSING_EXPECTED_EVIDENCE", codes)

    def test_deleted_paths_in_scope_but_not_allowed(self):
        task = _minimal_task()
        findings = run_preflight(task, deleted_paths=["docs/old.md"])
        codes = [f["code"] for f in findings]
        self.assertIn("DELETE_WITHOUT_PERMISSION", codes)


# ---------------------------------------------------------------------------
# CLI (main)
# ---------------------------------------------------------------------------


class TestMain(unittest.TestCase):
    def _task_file(self, **overrides) -> str:
        task = _minimal_task(**overrides)
        lines = ["---\n"]
        for k, v in task.items():
            if isinstance(v, list):
                lines.append(f"{k}:\n")
                for item in v:
                    lines.append(f"  - {item}\n")
            else:
                lines.append(f"{k}: {v}\n")
        lines.append("---\n")
        return _write_yaml("".join(lines))

    def test_valid_task_exits_0(self):
        path = self._task_file()
        try:
            rc = main(["--task-file", path])
            self.assertEqual(rc, 0)
        finally:
            os.unlink(path)

    def test_module_cli_smoke_covers_valid_and_missing_task(self):
        """Exercise the real -m entrypoint formerly duplicated by CI steps."""
        root = Path(__file__).resolve().parents[3]
        path = self._task_file()
        try:
            valid = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scripts.agent.check_agent_preflight",
                    "--task-file",
                    path,
                    "--changed-paths",
                    "docs/security/agent-write-scope-baseline.md",
                    "--mode",
                    "report-only",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(valid.returncode, 0, valid.stderr)
            self.assertEqual(json.loads(valid.stdout)["findings_count"], 0)
            missing = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scripts.agent.check_agent_preflight",
                    "--task-file",
                    "/nonexistent/task.yaml",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(missing.returncode, 2)
            self.assertIn("Task-Datei nicht gefunden", missing.stderr)
        finally:
            os.unlink(path)

    def test_missing_task_file_exits_2(self):
        rc = main(["--task-file", "/nonexistent/task.yaml"])
        self.assertEqual(rc, 2)

    def test_warn_mode_exits_1_on_findings(self):
        path = self._task_file(task_id="")
        try:
            rc = main(["--task-file", path, "--mode", "warn"])
            self.assertEqual(rc, 1)
        finally:
            os.unlink(path)

    def test_report_only_mode_exits_0_on_findings(self):
        path = self._task_file(task_id="")
        try:
            rc = main(["--task-file", path, "--mode", "report-only"])
            self.assertEqual(rc, 0)
        finally:
            os.unlink(path)

    def test_output_is_valid_json(self):
        path = self._task_file()
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                main(["--task-file", path])
            output = buf.getvalue()
            data = json.loads(output)
            self.assertIn("findings", data)
            self.assertIn("mode", data)
        finally:
            os.unlink(path)

    def test_missing_required_task_file_arg_returns_2(self):
        rc = main([])
        self.assertEqual(rc, 2)

    def test_changed_paths_out_of_scope_reported(self):
        path = self._task_file()
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                main([
                    "--task-file", path,
                    "--changed-paths", "apps/web/index.ts",
                ])
            data = json.loads(buf.getvalue())
            codes = [f["code"] for f in data["findings"]]
            self.assertIn("PATH_OUT_OF_SCOPE", codes)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
