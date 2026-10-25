"""Exercise the existing conditional basemap merge gate, including negative cases."""

from __future__ import annotations

import fnmatch
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[3]
CI = ROOT / ".github/workflows/ci.yml"
BASEMAP = ROOT / ".github/workflows/basemap-runtime-proof.yml"


def affected_paths() -> list[str]:
    workflow = CI.read_text(encoding="utf-8")
    filtered = workflow.split("            basemap:\n", 1)[1].split(
        "            heavy:\n", 1
    )[0]
    return re.findall(r"^              - '([^']+)'$", filtered, re.MULTILINE)


def standalone_push_paths() -> list[str]:
    """Read main-push regional-proof triggers independently of the PR filter."""
    workflow = BASEMAP.read_text(encoding="utf-8")
    filtered = workflow.split("  push:\n", 1)[1].split(
        "  workflow_dispatch:", 1
    )[0]
    return re.findall(r'^      - "([^"]+)"$', filtered, re.MULTILINE)


def proof_gate_script() -> str:
    workflow = BASEMAP.read_text(encoding="utf-8")
    marker = (
        "      - name: Fail closed on any missing, skipped or failed regional proof\n"
        "        run: |\n"
    )
    lines = workflow.split(marker, 1)[1].splitlines()
    command_lines = []
    for line in lines:
        if line and not line.startswith("          "):
            break
        command_lines.append(line[10:] if line else "")
    return "\n".join(command_lines)


def gate_result(hamburg: str, schleswig_holstein: str, visual: str) -> int:
    script = proof_gate_script()
    replacements = {
        "${{ needs.basemap-pmtiles-content-proof.result }}": hamburg,
        "${{ needs.basemap-schleswig-holstein-content-proof.result }}": schleswig_holstein,
        "${{ needs.basemap-visual-proof.result }}": visual,
    }
    for old, new in replacements.items():
        assert old in script, f"missing exact same-run GitHub Actions binding: {old}"
        script = script.replace(old, new)
    assert "${{" not in script, "unbound GitHub Actions expression in proof gate"
    return subprocess.run(["bash", "-c", script], check=False, capture_output=True).returncode


class BasemapRequiredMergeGateTests(unittest.TestCase):
    def test_regional_proofs_all_success(self) -> None:
        self.assertEqual(gate_result("success", "success", "success"), 0)

    def test_hamburg_failure_blocks(self) -> None:
        self.assertNotEqual(gate_result("failure", "success", "success"), 0)

    def test_schleswig_holstein_failure_blocks(self) -> None:
        self.assertNotEqual(gate_result("success", "failure", "success"), 0)

    def test_visual_failure_or_skip_blocks(self) -> None:
        for visual in ("failure", "skipped", "", "cancelled"):
            with self.subTest(visual=visual):
                self.assertNotEqual(gate_result("success", "success", visual), 0)

    def test_missing_or_skipped_regional_content_blocks(self) -> None:
        for hamburg, sh in (("skipped", "success"), ("success", ""), ("", "")):
            with self.subTest(hamburg=hamburg, sh=sh):
                self.assertNotEqual(gate_result(hamburg, sh, "success"), 0)

    def test_maps_and_gate_changes_trigger_workflow(self) -> None:
        globs = affected_paths()
        for path in (
            "scripts/basemap/prepare-water-polygons.py",
            "scripts/basemap/test_prepare_water_polygons.py",
            "scripts/basemap/build-hamburg-pmtiles.sh",
            "apps/web/src/lib/map/basemap.ts",
            "apps/web/src/lib/maplibre/runtime.ts",
            "apps/web/src/lib/components/map/MapRouteSurface.svelte",
            "apps/web/src/routes/map/+page.svelte",
            "map-style/regions.json",
            ".github/workflows/ci.yml",
            ".github/workflows/basemap-runtime-proof.yml",
            "scripts/ci/tests/test_basemap_merge_gate.py",
        ):
            with self.subTest(path=path):
                self.assertTrue(any(fnmatch.fnmatchcase(path, g) for g in globs))

    def test_shared_map_layout_and_styles_trigger_both_proof_paths(self) -> None:
        # /map inherits the root layout, app.css and token-based map controls.
        for path in (
            "apps/web/src/routes/+layout.svelte",
            "apps/web/src/app.css",
            "apps/web/src/lib/styles/tokens.css",
        ):
            for source, globs in (
                ("pull_request", affected_paths()),
                ("main_push", standalone_push_paths()),
            ):
                with self.subTest(path=path, source=source):
                    self.assertTrue(
                        any(fnmatch.fnmatchcase(path, pattern) for pattern in globs)
                    )

    def test_shared_map_bootstrap_security_inputs_enable_regional_proofs(self) -> None:
        # Browser /map boots via hooks.client and the shared document/layout.
        # Their direct loaders affect regional MapLibre and CSP evidence.
        for path in (
            "apps/web/src/hooks.client.ts",
            "apps/web/src/lib/utils/preloadRecovery.ts",
            "apps/web/src/app.html",
            "apps/web/static/theme-init.js",
            "apps/web/src/lib/utils/inert-polyfill.ts",
            "apps/web/src/lib/utils/ua-flags.ts",
        ):
            for source, globs in (
                ("pull_request", affected_paths()),
                ("main_push", standalone_push_paths()),
            ):
                with self.subTest(path=path, source=source):
                    self.assertTrue(
                        any(fnmatch.fnmatchcase(path, pattern) for pattern in globs)
                    )

    def test_unrelated_mobile_navigation_does_not_build_maps(self) -> None:
        globs = affected_paths()
        for path in (
            "apps/web/src/routes/build/+page.svelte",
            "apps/web/src/routes/settings/+page.svelte",
            "apps/web/src/lib/components/NotificationSettings.svelte",
            "apps/api/src/notifications.rs",
        ):
            with self.subTest(path=path):
                self.assertFalse(any(fnmatch.fnmatchcase(path, g) for g in globs))
                self.assertFalse(
                    any(fnmatch.fnmatchcase(path, g) for g in standalone_push_paths())
                )

    def test_pr_path_filter_uses_readonly_pr_api(self) -> None:
        # dorny/paths-filter reads the changed-file REST API on pull_request.
        # Without this scope, conditional regional evidence never runs.
        ci = CI.read_text(encoding="utf-8")
        docs_changes = re.search(
            r"(?ms)^  docs-changes:\n(.*?)(?=^  [A-Za-z][\w-]*:\n|\Z)",
            ci,
        )
        self.assertIsNotNone(docs_changes)
        self.assertIn(
            "    permissions:\n      contents: read\n      pull-requests: read\n",
            docs_changes.group(1),
        )

    def test_same_run_proofs_are_required_by_global_gate(self) -> None:
        ci = CI.read_text(encoding="utf-8")
        bm = BASEMAP.read_text(encoding="utf-8")
        self.assertIn("uses: ./.github/workflows/basemap-runtime-proof.yml", ci)
        self.assertIn("- basemap-proofs", ci)
        self.assertIn("needs.basemap-proofs.result", ci)
        self.assertIn('[[ "${basemap_result}" == "success" ]] || failed=1', ci)
        self.assertIn('[[ "${basemap_result}" == "skipped" ]] || failed=1', ci)
        self.assertIn("  workflow_call: {}", bm)
        self.assertNotIn("  pull_request:", bm.split("jobs:", 1)[0])
        self.assertIn("group: basemap-regional-${{ github.workflow }}-${{ github.ref }}", bm)
        self.assertIn("  required-basemap-proofs:", bm)
        self.assertIn("- basemap-pmtiles-content-proof", bm)
        self.assertIn("- basemap-schleswig-holstein-content-proof", bm)
        self.assertIn("- basemap-visual-proof", bm)


    def test_reusable_concurrency_does_not_cancel_caller(self) -> None:
        # A called workflow inherits github.workflow from CI. Using the same
        # group with cancel-in-progress would cancel its own caller.
        ci = CI.read_text(encoding="utf-8")
        bm = BASEMAP.read_text(encoding="utf-8")

        def concurrency_group(workflow: str) -> str:
            match = re.search(r"(?m)^concurrency:\n(?:  #[^\n]*\n)*  group: ([^\n]+)$", workflow)
            self.assertIsNotNone(match)
            return match.group(1)

        self.assertNotEqual(concurrency_group(ci), concurrency_group(bm))


if __name__ == "__main__":
    unittest.main()
