from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[3]
SCRIPT = ROOT / "apps" / "web" / "scripts" / "generate-version.js"
REVISION_ENV_NAMES = (
    "GIT_COMMIT_SHA",
    "GITHUB_SHA",
    "VERCEL",
    "VERCEL_GIT_COMMIT_SHA",
    "VITE_VERCEL_GIT_COMMIT_SHA",
    "PUBLIC_VERCEL_GIT_COMMIT_SHA",
    "CF_PAGES",
    "CF_PAGES_COMMIT_SHA",
)


def clean_revision_environment(**overrides: str) -> dict[str, str]:
    env = os.environ.copy()
    for name in REVISION_ENV_NAMES:
        env.pop(name, None)
    env.update(overrides)
    return env


class GenerateVersionEnvironmentTests(unittest.TestCase):
    commit = "7b65127e852561997fa6a45b8cb3bfcef38e1eb8"

    def run_generator(
        self,
        commit: str,
        source_date_epoch: str = "1784139708",
        *,
        bind_artifact_tree: bool = False,
        build_files: dict[str, str] | None = None,
        output_directory: str = "build",
        populate_complete_output: bool = True,
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        files: dict[str, str] = {}
        if bind_artifact_tree and populate_complete_output:
            files.update(
                {
                    "login.html": "<main>login</main>\n",
                    "antraege.html": "<main>antraege</main>\n",
                    "settings.html": "<main>settings</main>\n",
                    "map.html": "<main>map</main>\n",
                    "_app/compile-revision.json": json.dumps(
                        {"schema_version": 1, "compile_revision": commit}
                    )
                    + "\n",
                }
            )
        files.update(build_files or {})
        for relative_path, contents in files.items():
            target = root / output_directory / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents, encoding="utf-8")
        env = clean_revision_environment(
            GIT_COMMIT_SHA=commit,
            SOURCE_DATE_EPOCH=source_date_epoch,
        )
        command = ["node", str(SCRIPT), "--server"]
        if bind_artifact_tree:
            command.append("--artifact-tree")
        result = subprocess.run(
            command,
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        version_root = output_directory if bind_artifact_tree else "build"
        return result, root / version_root / "_app" / "version.json"

    def test_explicit_commit_produces_deterministic_identity_without_git(self) -> None:
        result, version_file = self.run_generator(self.commit)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(version_file.read_text(encoding="utf-8"))
        self.assertEqual(payload["commit"], self.commit)
        self.assertEqual(payload["version"], self.commit[:8])
        self.assertEqual(payload["build_id"], f"{self.commit[:8]}-1784139708000")
        self.assertEqual(payload["built_at"], "2026-07-15T18:21:48.000Z")
        self.assertNotIn("artifact_tree", payload)

    def test_server_generation_writes_prodverity_manifest_bound_to_full_commit(self) -> None:
        result, version_file = self.run_generator(self.commit, bind_artifact_tree=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest_file = version_file.parents[1] / ".well-known" / "prodverity.json"
        self.assertTrue(manifest_file.is_file())
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        self.assertEqual(
            manifest,
            {
                "revision": self.commit,
                "checks": [
                    {"id": "homepage", "path": "/", "expected_status": 200},
                    {
                        "id": "frontend-version",
                        "path": "/_app/version.json",
                        "expected_status": 200,
                    },
                    {"id": "api-version", "path": "/api/version", "expected_status": 200},
                ],
            },
        )

    def test_client_generation_writes_compile_revision_marker(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        env = clean_revision_environment(
            GIT_COMMIT_SHA=self.commit,
            SOURCE_DATE_EPOCH="1784139708",
        )
        result = subprocess.run(
            ["node", str(SCRIPT), "--client"],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        marker = json.loads(
            (root / "static" / "_app" / "compile-revision.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            marker,
            {"schema_version": 1, "compile_revision": self.commit},
        )

    def test_vercel_commit_generates_client_identity_without_git(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        env = clean_revision_environment(
            VERCEL="1",
            VERCEL_GIT_COMMIT_SHA=self.commit,
            SOURCE_DATE_EPOCH="1784139708",
        )
        result = subprocess.run(
            ["node", str(SCRIPT), "--client"],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(
            (root / "src" / "lib" / "generated" / "buildVersion.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(payload["commit"], self.commit)

    def test_conflicting_platform_and_explicit_commits_fail_closed(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        env = clean_revision_environment(
            GIT_COMMIT_SHA=self.commit,
            VERCEL="1",
            VERCEL_GIT_COMMIT_SHA="a" * 40,
            SOURCE_DATE_EPOCH="1784139708",
        )
        result = subprocess.run(
            ["node", str(SCRIPT), "--server"],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Conflicting build revisions", result.stderr)

    def test_platform_commit_without_provider_flag_fails_closed(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        env = clean_revision_environment(
            VERCEL_GIT_COMMIT_SHA=self.commit,
            SOURCE_DATE_EPOCH="1784139708",
        )
        result = subprocess.run(
            ["node", str(SCRIPT), "--client"],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires VERCEL=1", result.stderr)

    def test_artifact_tree_binding_requires_a_completed_build(self) -> None:
        result, version_file = self.run_generator(
            self.commit,
            bind_artifact_tree=True,
            build_files={
                "index.html": "<main>Weltgewebe</main>\n",
                "_app/immutable/app.js": "export const ready = true;\n",
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(version_file.read_text(encoding="utf-8"))
        self.assertEqual(payload["artifact_tree"]["schema_version"], 1)
        self.assertRegex(payload["artifact_tree"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(payload["artifact_tree"]["file_count"], 7)
        self.assertEqual(payload["artifact_tree"]["compile_revision"], self.commit)
        self.assertEqual(payload["artifact_tree"]["provenance"], "unattested")

    def test_artifact_tree_binding_selects_vercel_static_output(self) -> None:
        result, version_file = self.run_generator(
            self.commit,
            bind_artifact_tree=True,
            output_directory=".vercel/output/static",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(version_file.is_file())
        payload = json.loads(version_file.read_text(encoding="utf-8"))
        self.assertEqual(payload["commit"], self.commit)
        self.assertEqual(payload["artifact_tree"]["provenance"], "unattested")

    def test_artifact_tree_binding_rejects_ambiguous_complete_outputs(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        for output_directory in ("build", ".vercel/output/static"):
            for route in ("login.html", "antraege.html", "settings.html", "map.html"):
                target = root / output_directory / route
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("<main>complete</main>\n", encoding="utf-8")
            marker = root / output_directory / "_app" / "compile-revision.json"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                json.dumps({"schema_version": 1, "compile_revision": self.commit})
                + "\n",
                encoding="utf-8",
            )
        env = clean_revision_environment(
            GIT_COMMIT_SHA=self.commit,
            SOURCE_DATE_EPOCH="1784139708",
        )
        result = subprocess.run(
            ["node", str(SCRIPT), "--server", "--artifact-tree"],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Multiple complete route build directories", result.stderr)

    def test_stale_client_build_cannot_be_relabelled(self) -> None:
        stale_commit = "a" * 40
        result, version_file = self.run_generator(
            self.commit,
            bind_artifact_tree=True,
            build_files={
                "index.html": "<main>stale build</main>\n",
                "_app/compile-revision.json": json.dumps(
                    {"schema_version": 1, "compile_revision": stale_commit}
                )
                + "\n",
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match server revision", result.stderr)
        self.assertFalse(version_file.exists())

    def test_artifact_tree_binding_fails_without_a_completed_build(self) -> None:
        result, version_file = self.run_generator(
            self.commit,
            bind_artifact_tree=True,
            populate_complete_output=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(version_file.exists())

    def test_invalid_explicit_commit_fails_closed(self) -> None:
        result, version_file = self.run_generator("7b65127e")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GIT_COMMIT_SHA", result.stderr)
        self.assertFalse(version_file.exists())

    def test_invalid_source_date_epoch_fails_closed(self) -> None:
        result, version_file = self.run_generator(self.commit, "yesterday")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SOURCE_DATE_EPOCH", result.stderr)
        self.assertFalse(version_file.exists())


if __name__ == "__main__":
    unittest.main()
