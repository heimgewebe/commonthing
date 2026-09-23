from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "security.yml"
DOCKERFILE = ROOT / "apps" / "api" / "Dockerfile"
PINNED_TRIVY_ACTION = re.compile(r"^aquasecurity/trivy-action@[0-9a-f]{40}$")
RENDER_STEP = "Render Trivy findings into job summary and log"


def load_workflow() -> tuple[dict, dict]:
    payload = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    triggers = payload.get("on", payload.get(True))
    assert isinstance(triggers, dict)
    return payload, triggers


def image_scan_steps() -> list[dict]:
    payload, _ = load_workflow()
    return payload["jobs"]["image-scan"]["steps"]


def runtime_base_image() -> str:
    refs = [line.split()[1] for line in DOCKERFILE.read_text(encoding="utf-8").splitlines() if line.startswith("FROM ")]
    return refs[-1]


BASE_LAYERS = ["sha256:base-layer-1", "sha256:base-layer-2"]
FAKE_DOCKER = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${FAKE_DOCKER_LOG}"
case "$1" in
  pull) exit "${FAKE_DOCKER_PULL_RC:-0}" ;;
  image) printf '%s\\n' "${FAKE_BASE_LAYERS}" ;;
  *) exit 2 ;;
esac
"""


def vulnerability(
    vuln_id: str, pkg: str, severity: str, url: str | None = None, layer: str | None = None
) -> dict:
    finding = {
        "VulnerabilityID": vuln_id,
        "PkgName": pkg,
        "InstalledVersion": "1.0",
        "FixedVersion": "1.1",
        "Severity": severity,
        "PrimaryURL": url,
    }
    if layer is not None:
        finding["Layer"] = {"DiffID": layer}
    return finding


class SecurityWorkflowContractTest(unittest.TestCase):
    def test_trivy_scans_the_production_api_image_and_blocks_high_severity_findings(self) -> None:
        _, triggers = load_workflow()
        steps = image_scan_steps()

        build_step = next(step for step in steps if step.get("name") == "Build production API image")
        build_command = build_step["run"]
        self.assertIn("--file apps/api/Dockerfile", build_command)
        self.assertIn("GIT_COMMIT_SHA=${GITHUB_SHA}", build_command)
        self.assertIn("BUILD_TIMESTAMP=", build_command)
        self.assertIn("weltgewebe-api:trivy-scan", build_command)

        trivy_step = next(
            step for step in steps if str(step.get("uses", "")).startswith("aquasecurity/trivy-action@")
        )
        self.assertRegex(trivy_step["uses"], PINNED_TRIVY_ACTION)
        inputs = trivy_step["with"]
        self.assertEqual(inputs["scan-type"], "image")
        self.assertEqual(inputs["image-ref"], "weltgewebe-api:trivy-scan")
        self.assertEqual(inputs["scanners"], "vuln")
        self.assertEqual(inputs["vuln-type"], "os,library")
        self.assertEqual(inputs["severity"], "HIGH,CRITICAL")
        self.assertIs(inputs["ignore-unfixed"], True)
        self.assertEqual(str(inputs["exit-code"]), "1")
        self.assertEqual(inputs["format"], "json")
        self.assertEqual(inputs["output"], "trivy-image-report.json")

        trigger_paths = set(triggers["pull_request"]["paths"])
        self.assertLessEqual(
            {
                ".github/workflows/security.yml",
                "apps/api/Dockerfile",
                "apps/api/entrypoint.sh",
                "Cargo.lock",
            },
            trigger_paths,
        )

    def test_weekly_security_jobs_compare_the_schedule_string_directly(self) -> None:
        payload, _ = load_workflow()
        raw = WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("github.event.schedule.cron", raw)
        self.assertIn("github.event.schedule == '10 3 * * 0'", payload["jobs"]["deny"]["if"])
        self.assertIn("github.event.schedule == '25 3 * * 0'", payload["jobs"]["sbom"]["if"])
        self.assertIn("github.event.schedule == '25 3 * * 0'", payload["jobs"]["image-scan"]["if"])

    def test_findings_are_rendered_after_the_scan_even_when_it_fails(self) -> None:
        steps = image_scan_steps()
        names = [step.get("name") for step in steps]
        scan_index = next(
            index
            for index, step in enumerate(steps)
            if str(step.get("uses", "")).startswith("aquasecurity/trivy-action@")
        )
        render_index = names.index(RENDER_STEP)
        upload_index = names.index("Upload Trivy image report")
        self.assertLess(scan_index, render_index)
        self.assertLess(render_index, upload_index)

        render_step = steps[render_index]
        self.assertEqual(render_step.get("if"), "always()")
        self.assertEqual(render_step["env"]["TRIVY_REPORT"], steps[scan_index]["with"]["output"])


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "render step needs bash and jq")
class TrivyReportRenderingTest(unittest.TestCase):
    """Runs the workflow's own render script against synthetic Trivy reports."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        render_step = next(step for step in image_scan_steps() if step.get("name") == RENDER_STEP)
        self.script = render_step["run"]
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        fake_docker = bin_dir / "docker"
        fake_docker.write_text(FAKE_DOCKER, encoding="utf-8")
        fake_docker.chmod(0o755)
        self.docker_log = self.tmp / "docker.log"
        self.docker_env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_DOCKER_LOG": str(self.docker_log),
            "FAKE_BASE_LAYERS": json.dumps(BASE_LAYERS),
        }

    def render(
        self, report: dict | None, *, pull_fails: bool = False
    ) -> tuple[subprocess.CompletedProcess[str], str]:
        report_path = self.tmp / "trivy-image-report.json"
        if report is not None:
            report_path.write_text(json.dumps(report), encoding="utf-8")
        summary_path = self.tmp / "summary.md"
        summary_path.write_text("", encoding="utf-8")
        env = {
            **os.environ,
            **self.docker_env,
            "FAKE_DOCKER_PULL_RC": "1" if pull_fails else "0",
            "TRIVY_REPORT": str(report_path),
            "GITHUB_STEP_SUMMARY": str(summary_path),
        }
        result = subprocess.run(
            ["bash", "-c", self.script],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        return result, summary_path.read_text(encoding="utf-8")

    def assert_log_carries_summary(self, result: subprocess.CompletedProcess[str], summary: str) -> None:
        log_without_annotations = "".join(
            line for line in result.stdout.splitlines(keepends=True) if not line.startswith("::")
        )
        self.assertEqual(log_without_annotations, summary)

    @staticmethod
    def table_rows(summary: str) -> list[str]:
        return [line for line in summary.splitlines() if line.startswith("| ") and "---" not in line][1:]

    def test_findings_become_a_sorted_table_with_their_layer_provenance(self) -> None:
        report = {
            "Metadata": {"OS": {"Family": "debian", "Name": "12.15"}, "RepoDigests": []},
            "Results": [
                {
                    "Class": "os-pkgs",
                    "Type": "debian",
                    "Vulnerabilities": [
                        vulnerability("CVE-2026-2222", "zlib1g", "HIGH", layer=BASE_LAYERS[1]),
                        vulnerability(
                            "CVE-2026-1111",
                            "libssl3",
                            "CRITICAL",
                            "https://avd.example/cve-2026-1111",
                            layer="sha256:layer-after-last-from",
                        ),
                    ],
                },
                {
                    "Class": "lang-pkgs",
                    "Type": "rustbinary",
                    "Vulnerabilities": [vulnerability("GHSA-0000", "odd|crate", "HIGH", "https://gh.example/a")],
                },
            ],
        }
        result, summary = self.render(report)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"pull --quiet {runtime_base_image()}", self.docker_log.read_text(encoding="utf-8"))
        self.assertIn("## Trivy API image: 3 behebbare HIGH/CRITICAL-Treffer", summary)
        self.assertIn(f"`{runtime_base_image()}`", summary)
        self.assertIn("Im Image erkannt: debian 12.15", summary)
        self.assertEqual(
            self.table_rows(summary),
            [
                "| Dockerfile-Schicht (debian) | libssl3 | 1.0 | 1.1 | CRITICAL | "
                "[CVE-2026-1111](https://avd.example/cve-2026-1111) |",
                "| Abhängigkeit (rustbinary) | odd\\|crate | 1.0 | 1.1 | HIGH | [GHSA-0000](https://gh.example/a) |",
                "| Basisimage (debian) | zlib1g | 1.0 | 1.1 | HIGH | "
                "[CVE-2026-2222](https://nvd.nist.gov/vuln/detail/CVE-2026-2222) |",
            ],
        )
        self.assertIn("::error title=Trivy API image::3 behebbare HIGH/CRITICAL-Treffer", result.stdout)
        self.assertNotIn("::error", summary)
        self.assert_log_carries_summary(result, summary)

    def test_os_packages_are_never_called_base_image_without_layer_proof(self) -> None:
        findings = [
            vulnerability("CVE-2026-3333", "libpcre2-8-0", "HIGH", layer=BASE_LAYERS[0]),
            vulnerability("CVE-2026-4444", "wget", "HIGH"),
        ]
        report = {"Metadata": {}, "Results": [{"Class": "os-pkgs", "Type": "debian", "Vulnerabilities": findings}]}

        _, unreachable_base = self.render(report, pull_fails=True)
        self.assertNotIn("| Basisimage", unreachable_base)
        self.assertEqual(unreachable_base.count("| OS-Paket, Schicht unbekannt (debian) |"), 2)

        _, known_base = self.render(report)
        rows = self.table_rows(known_base)
        self.assertTrue(rows[0].startswith("| Basisimage (debian) | libpcre2-8-0 |"), rows)
        self.assertTrue(rows[1].startswith("| OS-Paket, Schicht unbekannt (debian) | wget |"), rows)

    def test_results_without_vulnerabilities_render_as_clean(self) -> None:
        report = {
            "Metadata": {"OS": {"Family": "debian", "Name": "12.15"}},
            "Results": [{"Class": "os-pkgs", "Type": "debian"}, {"Class": "lang-pkgs", "Type": "cargo"}],
        }
        result, summary = self.render(report)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("## Trivy API image: 0 behebbare HIGH/CRITICAL-Treffer", summary)
        self.assertIn("Keine Treffer oberhalb der Schwelle.", summary)
        self.assertNotIn("::error", result.stdout)
        self.assert_log_carries_summary(result, summary)

    def test_a_missing_report_is_named_without_failing_the_step(self) -> None:
        result, summary = self.render(None)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("## Trivy API image: kein Report", summary)
        self.assert_log_carries_summary(result, summary)

    def test_more_than_a_hundred_findings_are_truncated_with_a_pointer(self) -> None:
        findings = [
            vulnerability(f"CVE-2026-{index:04d}", f"pkg{index:03d}", "HIGH", layer=BASE_LAYERS[0])
            for index in range(101)
        ]
        report = {"Metadata": {}, "Results": [{"Class": "os-pkgs", "Type": "debian", "Vulnerabilities": findings}]}
        result, summary = self.render(report)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("## Trivy API image: 101 behebbare HIGH/CRITICAL-Treffer", summary)
        self.assertEqual(summary.count("| Basisimage (debian) |"), 100)
        self.assertIn("Artefakt `trivy-api-image-report`", summary)


if __name__ == "__main__":
    unittest.main()
