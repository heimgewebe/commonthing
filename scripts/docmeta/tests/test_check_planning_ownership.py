import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from scripts.docmeta import check_planning_ownership as ownership
from scripts.docmeta import check_planning_registration as legacy


class TestCheckPlanningOwnership(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.repo_root = self.test_dir.name
        self.root_patcher = patch(
            "scripts.docmeta.check_planning_registration.REPO_ROOT",
            self.repo_root,
        )
        self.root_patcher.start()

        os.makedirs(os.path.join(self.repo_root, "docs/tasks"), exist_ok=True)
        os.makedirs(os.path.join(self.repo_root, "docs/blueprints"), exist_ok=True)
        self.write_file("docs/tasks/index.json", "{}")
        self.write_file("docs/tasks/board.md", "")
        self.write_file("docs/roadmap.md", "")

    def tearDown(self):
        self.root_patcher.stop()
        self.test_dir.cleanup()

    def write_file(self, rel_path, content):
        full_path = os.path.join(self.repo_root, rel_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as handle:
            handle.write(content)

    def test_explicit_owner_task_passes_without_local_registration(self):
        self.write_file(
            "docs/blueprints/owned.md",
            "---\nstatus: active\nowner_task: BUREAU-COMMONTHING-123\n---\nBody\n",
        )

        self.assertEqual(ownership.run_checks(), [])

    def test_quoted_owner_task_passes(self):
        self.write_file(
            "docs/blueprints/owned.md",
            "---\nstatus: active\nowner_task: 'BUREAU-COMMONTHING-123'\n---\nBody\n",
        )

        self.assertEqual(ownership.run_checks(), [])

    def test_empty_owner_task_does_not_count_as_ownership(self):
        self.write_file(
            "docs/blueprints/unowned.md",
            "---\nstatus: active\nowner_task:\n---\nBody\n",
        )

        findings = ownership.run_checks()
        self.assertEqual(
            [finding["code"] for finding in findings],
            ["UNOWNED_PLANNING_ARTIFACT"],
        )

    def test_owner_task_in_body_does_not_count_as_frontmatter_ownership(self):
        self.write_file(
            "docs/blueprints/unowned.md",
            "---\nstatus: active\n---\nowner_task: BUREAU-COMMONTHING-123\n",
        )

        findings = ownership.run_checks()
        self.assertEqual(
            [finding["code"] for finding in findings],
            ["UNOWNED_PLANNING_ARTIFACT"],
        )

    def test_legacy_index_registration_remains_accepted_during_migration(self):
        self.write_file(
            "docs/tasks/index.json",
            json.dumps(
                {
                    "tasks": [
                        {
                            "id": "LEGACY-1",
                            "evidence": ["docs/blueprints/legacy.md"],
                        }
                    ]
                }
            ),
        )
        self.write_file(
            "docs/blueprints/legacy.md",
            "---\nstatus: active\n---\nBody\n",
        )

        self.assertEqual(ownership.run_checks(), [])

    def test_unowned_unregistered_active_plan_is_reported(self):
        self.write_file(
            "docs/blueprints/unowned.md", "---\nstatus: active\n---\nBody\n"
        )

        findings = ownership.run_checks()
        matching = [
            finding
            for finding in findings
            if finding["code"] == "UNOWNED_PLANNING_ARTIFACT"
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["path"], "docs/blueprints/unowned.md")
        self.assertIn("owner_task", matching[0]["suggestion"])

    def test_terminal_plan_needs_no_owner(self):
        self.write_file(
            "docs/blueprints/archived.md",
            "---\nstatus: archived\n---\nBody\n",
        )

        self.assertEqual(ownership.run_checks(), [])

    def test_strict_mode_blocks_only_real_ownership_findings(self):
        self.write_file(
            "docs/blueprints/unowned.md",
            "---\nstatus: active\n---\nBody\n",
        )
        with patch("sys.stderr", new_callable=io.StringIO):
            exit_code = ownership.main(["--mode", "strict"])
        self.assertEqual(exit_code, 1)

        self.write_file(
            "docs/blueprints/unowned.md",
            "---\nstatus: active\nowner_task: BUREAU-COMMONTHING-123\n---\nBody\n",
        )
        with patch("sys.stdout", new_callable=io.StringIO):
            exit_code = ownership.main(["--mode", "strict"])
        self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()
