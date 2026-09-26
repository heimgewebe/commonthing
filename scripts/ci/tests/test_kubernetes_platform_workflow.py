from __future__ import annotations

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
PROOF_WORKFLOW = ROOT / ".github/workflows/kubernetes-platform-proof.yml"
PR_WORKFLOW = ROOT / ".github/workflows/kubernetes-platform.yml"
UPLOAD_ACTION = "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"


class KubernetesPlatformWorkflowDiagnosticsTests(unittest.TestCase):
    def test_pull_request_workflow_runs_experiment_b_contract(self) -> None:
        workflow = yaml.safe_load(PR_WORKFLOW.read_text(encoding="utf-8"))
        steps = workflow["jobs"]["contract"]["steps"]
        named = {step["name"]: step for step in steps if "name" in step}

        compile_step = named["Compile platform tools"]["run"]
        self.assertIn("scripts/platform/experiment_b.py", compile_step)
        self.assertIn("scripts/platform/experiment_b_runtime.py", compile_step)

        platform_tests = named["Run platform contract tests"]["run"]
        self.assertIn(
            "scripts.ci.tests.test_kubernetes_platform_workflow",
            platform_tests,
        )

        experiment_tests = named["Run Experiment-B contract tests"]["run"]
        self.assertIn("scripts/platform/experiment_b_test.py -v", experiment_tests)
        self.assertIn(
            "scripts/platform/experiment_b_runtime_test.py -v",
            experiment_tests,
        )

        render = named["Render and validate all platform targets"]["run"]
        self.assertIn("scripts/platform/validate_platform.py --render", render)

    def test_failed_live_package_receipt_is_uploaded_for_both_proof_jobs(self) -> None:
        workflow = yaml.safe_load(PROOF_WORKFLOW.read_text(encoding="utf-8"))
        test_path = "scripts/ci/tests/test_kubernetes_platform_workflow.py"
        self.assertIn(test_path, workflow["on"]["push"]["paths"])
        contract_steps = workflow["jobs"]["contract"]["steps"]
        contract_test = next(
            step for step in contract_steps if step.get("name") == "Run platform contract tests"
        )
        self.assertIn("scripts.ci.tests.test_kubernetes_platform_workflow", contract_test["run"])

        cases = {
            "kind-gitops-proof": (
                "kubernetes-kind-oci-live-package-${{ github.run_id }}-${{ github.run_attempt }}",
                "build/kubernetes-platform/kind-gitops-oci-mirror/live-package-receipt.json",
            ),
            "kind-ha-recovery-proof": (
                "kubernetes-ha-oci-live-package-${{ github.run_id }}-${{ github.run_attempt }}",
                "build/kubernetes-platform/ha-recovery-oci-mirror/live-package-receipt.json",
            ),
        }

        for job_name, (artifact_name, receipt_path) in cases.items():
            with self.subTest(job=job_name):
                steps = workflow["jobs"][job_name]["steps"]
                named = {step["name"]: step for step in steps if "name" in step}
                live = named["Verify live OCI mirror package budget"]
                upload = named["Upload failed live OCI package receipt"]

                self.assertEqual(live["id"], "verify-live-package")
                self.assertIn("verify-live", live["run"])
                self.assertEqual(
                    upload["if"],
                    "failure() && steps.verify-live-package.outcome == 'failure' && steps.proof-cache.outputs.cache-hit != 'true'",
                )
                self.assertEqual(upload["uses"], UPLOAD_ACTION)
                self.assertEqual(upload["with"]["name"], artifact_name)
                self.assertEqual(upload["with"]["path"], receipt_path)
                self.assertEqual(upload["with"]["if-no-files-found"], "error")
                self.assertEqual(upload["with"]["retention-days"], 14)
                self.assertNotIn(".cache/", receipt_path)
                self.assertNotIn("DOCKER_CONFIG", receipt_path)
                self.assertLess(steps.index(live), steps.index(upload))
                self.assertLess(
                    steps.index(upload),
                    steps.index(named["Authenticate controlled OCI mirror"]),
                )


if __name__ == "__main__":
    unittest.main()
