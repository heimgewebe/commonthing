"""Contract tests for the exact-revision API runtime evidence workflow."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "domain-scale.yml"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


class ApiRuntimeWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = WORKFLOW.read_text(encoding="utf-8")
        cls.ci_source = CI_WORKFLOW.read_text(encoding="utf-8")
        marker = "  api-runtime-evidence:\n"
        if marker not in cls.source:
            raise AssertionError("api-runtime-evidence job is missing")
        cls.job = marker + cls.source.split(marker, 1)[1]

    def test_runtime_job_follows_the_real_domain_scale_plan_proof(self) -> None:
        self.assertIn("needs: foundation", self.job)
        self.assertIn(
            "python3 -B scripts/performance/domain_scale.py generate \\\n", self.job
        )
        self.assertIn(
            "python3 -B scripts/performance/domain_scale.py render-load \\\n", self.job
        )
        self.assertIn("SELECT count(*) FROM weltgewebe_perf.domain_nodes", self.job)
        self.assertIn("SELECT count(*) FROM weltgewebe_perf.domain_edges", self.job)
        self.assertIn("weltgewebe_search_generation_activation_ready", self.job)
        self.assertIn("weltgewebe_activate_search_generation", self.job)

    def test_ready_200_requires_pinned_live_jetstream_workers(self) -> None:
        import json

        pinned = json.loads(
            (ROOT / "scripts/ci/postgres-proof-contract.json").read_text(
                encoding="utf-8"
            )
        )["jetstream_image"]
        self.assertRegex(pinned, r"^nats@sha256:[0-9a-f]{64}$")
        nats = self.job.index("name: Start isolated JetStream for measured API")
        api = self.job.index("name: Start and warm the measured API container")
        self.assertLess(nats, api)
        start = self.job[nats:api]
        self.assertIn("scripts/ci/postgres-proof-contract.json", start)
        self.assertIn("jetstream_image", start)
        self.assertIn("--network bridge", start)
        self.assertIn("(( DOCKER_SERVER_MAJOR >= 28 ))", start)
        self.assertNotIn("--network host", start)
        self.assertIn("--publish 127.0.0.1:4222:4222", start)
        self.assertIn("--publish 127.0.0.1:8222:8222", start)
        self.assertIn('"${NATS_IMAGE}" -js -n "${NATS_SERVER_NAME}" -m 8222', start)
        self.assertIn('NATS_CONTAINER_ID="$(docker run', start)
        self.assertIn("check_t048_jetstream.py", start)
        self.assertIn('--server-name "${NATS_SERVER_NAME}"', start)
        self.assertIn('--container-id "${NATS_CONTAINER_ID}"', start)
        self.assertIn("name: Verify JetStream is empty before measured load", self.job)
        end = self.job[self.job.index("name: Stop isolated JetStream"): ]
        self.assertIn("if: ${{ always() }}", end)
        self.assertIn('docker rm --force "${NATS_CONTAINER}"', end)
        warmup = self.job[api:self.job.index("name: Bind live API runtime")]
        self.assertIn('--env "NATS_URL=nats://127.0.0.1:4222"', warmup)
        self.assertIn("--env WELTGEWEBE_DOMAIN_JETSTREAM_REPLICAS=1", warmup)

    def test_isolated_fixture_backlog_is_reset_before_readiness(self) -> None:
        fixture = self.job.index("name: Load the canonical fixture into the migrated API database")
        reset = self.job.index("name: Reset synthetic fixture outbox before readiness")
        broker = self.job.index("name: Start isolated JetStream for measured API")
        self.assertLess(fixture, reset)
        self.assertLess(reset, broker)
        source = self.job[reset:broker]
        self.assertIn('psql "${DATABASE_URL}" -v ON_ERROR_STOP=1', source)
        self.assertIn("aggregate_type NOT IN ('node', 'edge', 'conversation')", source)
        self.assertIn("published_at IS NOT NULL", source)
        self.assertEqual(source.count("$t048_fixture$"), 2)
        self.assertIn("quarantined_at IS NOT NULL", source)
        self.assertIn("attempt_count > 0", source)
        self.assertIn("domain_event_consumptions", source)
        self.assertIn("OR EXISTS (SELECT 1 FROM web_push_deliveries)", source)
        self.assertIn("SELECT count(*) FROM web_push_deliveries", source)
        self.assertIn("TRUNCATE TABLE domain_event_consumptions, domain_outbox, web_push_deliveries RESTART IDENTITY RESTRICT", source)
        self.assertNotIn("RESTART IDENTITY CASCADE", source)
        self.assertIn("CREATE TABLE t048_outbox_fk_guard", source)
        self.assertIn("FOREIGN KEY (id) REFERENCES domain_outbox(id)", source)
        self.assertIn("WHEN SQLSTATE '0A000'", source)
        self.assertIn("SELECT count(*) FROM domain_outbox", source)
        self.assertIn("SELECT count(*) FROM domain_nodes", source)

    def test_readiness_is_stable_before_starting_measured_load(self) -> None:
        start = self.job.index("name: Start and warm the measured API container")
        bound = self.job.index("name: Bind live API runtime to the exact fixture")
        measure = self.job.index(
            "name: Measure the canonical mixed-health-and-read scenario"
        )
        self.assertLess(start, bound)
        self.assertLess(bound, measure)
        warmup = self.job[start:bound]
        self.assertIn("http://127.0.0.1:8787/health/ready", warmup)
        self.assertIn("READINESS_STREAK=$((READINESS_STREAK + 1))", warmup)
        self.assertIn('if [[ "${READINESS_STREAK}" -lt 3 ]]', warmup)
        self.assertRegex(warmup, r"else\s*\n\s*READINESS_STREAK=0")

    def test_readiness_200_200_503_requires_three_new_200s(self) -> None:
        start = self.job.index("name: Start and warm the measured API container")
        bound = self.job.index("name: Bind live API runtime to the exact fixture")
        warmup = self.job[start:bound]
        loop = warmup[
            warmup.index("READINESS_STREAK=0") :
            warmup.index('if [[ "${READINESS_STREAK}" -lt 3 ]]')
        ]
        fake_curl = '''
responses=(200 200 503 200 200 200)
cursor=0
curl() {
  local status="${responses[cursor]:-503}"
  cursor=$((cursor + 1))
  [[ "${status}" == 200 ]]
}
sleep() { :; }
'''
        script = fake_curl + loop + 'echo "${READINESS_STREAK}:${cursor}"\n'
        result = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, check=True
        )
        self.assertEqual(result.stdout.strip(), "3:6")

    def test_scenario_values_come_from_the_canonical_policy(self) -> None:
        self.assertIn(
            "from scripts.performance.api_runtime_evidence import api_runtime_section, load_policy",
            self.job,
        )
        self.assertIn('load_policy(Path("policies/performance.v1.json"))', self.job)
        for output in (
            "virtual_users",
            "duration_seconds",
            "dataset_profile",
            "concurrency_profile",
            "search_query",
            "database_profile",
            "node_count",
            "edge_count",
        ):
            self.assertIn(f'"{output}"', self.job)
        self.assertNotRegex(self.job, r"API_RUNTIME_VUS:\s*10")
        self.assertNotRegex(self.job, r"API_RUNTIME_DURATION_SECONDS:\s*30")
        self.assertNotIn("thresholds:", self.job)

    def test_k6_and_api_images_are_digest_bound(self) -> None:
        self.assertRegex(self.job, r"K6_IMAGE: grafana/k6@sha256:[0-9a-f]{64}")
        self.assertRegex(self.job, r"image: postgres:16@sha256:[0-9a-f]{64}")
        self.assertRegex(self.job, r"image: registry:2\.8\.3@sha256:[0-9a-f]{64}")
        self.assertIn('docker run --rm "${K6_IMAGE}" version', self.job)
        self.assertNotIn("K6_LINUX_AMD64_SHA256", self.job)
        checkout = self.job[
            self.job.index("uses: actions/checkout@") : self.job.index(
                "uses: actions/setup-python@"
            )
        ]
        self.assertIn(
            "ref: ${{ github.event.pull_request.head.sha || github.sha }}", checkout
        )
        self.assertIn('COMMIT="$(git rev-parse HEAD)"', self.job)
        self.assertIn('--build-arg "GIT_COMMIT_SHA=${COMMIT}"', self.job)
        self.assertIn('--build-arg "BUILD_TIMESTAMP=${BUILD_TIMESTAMP}"', self.job)
        self.assertIn('API_IMAGE_TAG="${API_IMAGE_REPOSITORY}:${COMMIT}"', self.job)
        self.assertIn('docker push "${API_IMAGE_TAG}"', self.job)
        self.assertIn('API_IMAGE_DIGEST="${API_IMAGE_REPOSITORY}@${API_DIGEST}"', self.job)
        self.assertIn('"${{ steps.image.outputs.image }}"', self.job)
        self.assertIn('commit=\\"${{ steps.image.outputs.commit }}\\"', self.job)

    def test_live_measurement_collects_every_required_input_during_one_run(
        self,
    ) -> None:
        binding = self.job[
            self.job.index("name: Bind live API runtime to the exact fixture") :
            self.job.index("name: Measure the canonical mixed-health-and-read scenario")
        ]
        self.assertIn("api_runtime_live_binding.py", binding)
        self.assertIn('POSTGRES_CONTAINER: ${{ job.services.postgres.id }}', binding)
        self.assertIn('--k6-image "${K6_IMAGE}"', binding)
        self.assertIn('--run-id "${RUN_ID}"', binding)
        measurement = self.job[
            self.job.index("name: Measure the canonical mixed-health-and-read scenario") :
            self.job.index("name: Assemble and independently verify exact-revision evidence")
        ]
        self.assertIn("container_resource_sampler.py sample", measurement)
        self.assertIn("postgres_connection_sampler.py sample", measurement)
        self.assertGreaterEqual(measurement.count('--run-id "${RUN_ID}"'), 2)
        self.assertIn('"${K6_IMAGE}" run scripts/performance/api_runtime_k6.js', measurement)
        self.assertIn("API_RUNTIME_RUN_ID", measurement)
        self.assertIn("API_RUNTIME_K6_IMAGE", measurement)
        self.assertIn("API_RUNTIME_DATASET_MANIFEST_SHA256", measurement)
        self.assertIn("API_RUNTIME_CONCURRENCY_PROFILE", measurement)
        self.assertIn("API_RUNTIME_SEARCH_QUERY: ${{ steps.contract.outputs.search_query }}", measurement)
        self.assertIn("SAMPLER_DURATION=$((${{ steps.contract.outputs.duration_seconds }} + 20))", measurement)
        self.assertNotIn("SAMPLER_DURATION=$((${{ steps.contract.outputs.duration_seconds }} + 5))", measurement)
        self.assertNotIn("SELECT count(*) FROM pg_stat_activity", measurement)
        self.assertNotRegex(measurement, r"API_RUNTIME_SEARCH_QUERY=\w+")

    def test_evidence_is_assembled_verified_and_uploaded(self) -> None:
        check = self.job.index(
            "api_runtime_evidence.py check", self.job.index("Measure the canonical")
        )
        verify = self.job.index("api_runtime_evidence.py verify", check)
        upload = self.job.index("name: Upload API runtime evidence", verify)
        self.assertLess(check, verify)
        self.assertLess(verify, upload)
        for evidence_file in (
            "report.json",
            "k6-summary.json",
            "metrics-before.prom",
            "metrics-after.prom",
            "resource-receipt.json",
            "database-connections.json",
            "runtime-binding.json",
            "fixture/manifest.json",
            "fixture/domain_nodes.csv",
            "fixture/domain_edges.csv",
        ):
            self.assertIn(evidence_file, self.job[upload:])
        self.assertIn("if-no-files-found: error", self.job[upload:])

    def test_regression_fixture_proves_a_bound_threshold_failure(self) -> None:
        regression = self.job[
            self.job.index(
                "name: Prove the API runtime gate fails closed"
            ) : self.job.index("name: Build the exact API revision")
        ]
        self.assertIn("api_runtime_evidence.py regression-fixture", regression)
        self.assertIn('--runtime-binding "${REGRESSION_DIR}/runtime-binding.json"', regression)
        self.assertIn('--database-connections-receipt "${REGRESSION_DIR}/database-connections.json"', regression)
        self.assertNotIn("--database-connections 1", regression)
        self.assertIn('if [[ "${CHECK_STATUS}" -ne 2 ]]', regression)
        self.assertIn("report_path.is_file()", regression)
        self.assertIn("json.loads(report_path.read_text", regression)
        self.assertIn('report.get("status") != "fail"', regression)
        self.assertIn('report.get("failures")', regression)
        self.assertIn('report.get("revision") != {', regression)
        self.assertIn('"git_head": expected_head', regression)
        self.assertIn('"measured_api_commit": expected_head', regression)
        self.assertIn('"policy_sha256": expected_policy_sha256', regression)
        self.assertIn('2> "${VERIFY_STDERR}"', regression)
        self.assertIn('if [[ "${VERIFY_STATUS}" -ne 2 ]]', regression)
        self.assertIn("does not pass the performance gate: 'fail'", regression)
        self.assertNotIn("continue-on-error", regression)
        self.assertNotIn("|| true", regression)

    def test_hidden_fixture_projections_are_explicitly_redacted(self) -> None:
        projection = self.job[
            self.job.index("INSERT INTO search_node_projections (") : self.job.index(
                "UPDATE search_projection_jobs"
            )
        ]
        normalized = " ".join(projection.split())
        for public_value, hidden_value in (
            (
                "repeat('0', 64)",
                "'e0f631f5602e764ef8a5f14e36d2d81663b20cd305a30af0dad6c0d759e5a955'",
            ),
            ("n.title", "'[nicht öffentlich]'"),
            (
                "ARRAY(SELECT jsonb_array_elements_text(n.payload -> 'tags'))",
                "'{}'::TEXT[]",
            ),
            ("coalesce(n.payload ->> 'summary', n.title)", "'[nicht öffentlich]'"),
            ("'de'", "'und'"),
            ("n.kind", "'[nicht öffentlich]'"),
            ("'active'", "'hidden'"),
            ("ARRAY['public']::TEXT[]", "'{}'::TEXT[]"),
            ("'ready'", "'unavailable'"),
            (
                "array_fill(0.0::DOUBLE PRECISION, ARRAY[g.dimension])",
                "NULL",
            ),
        ):
            self.assertIn(
                "CASE WHEN n.search_visibility = 'public' "
                f"THEN {public_value} ELSE {hidden_value} END",
                normalized,
            )
        self.assertIn("v.source_version, v.source_revision", normalized)

    def test_runtime_proof_is_reusable_without_a_duplicate_pr_run(self) -> None:
        trigger_prefix = self.source.split("jobs:", 1)[0]
        self.assertIn("workflow_call:", trigger_prefix)
        self.assertIn("workflow_dispatch:", trigger_prefix)
        self.assertNotIn("pull_request:", trigger_prefix)
        self.assertNotIn("push:", trigger_prefix)
        self.assertIn(
            "group: domain-scale-${{ github.workflow }}-${{ github.ref }}",
            trigger_prefix,
        )

    def test_required_merge_gate_owns_the_runtime_proof(self) -> None:
        docs_changes = self.ci_source[
            self.ci_source.index("  docs-changes:\n") : self.ci_source.index(
                "  api-runtime-proof:\n"
            )
        ]
        self.assertIn(
            "api_runtime: ${{ steps.filter.outputs.api_runtime }}", docs_changes
        )
        for path in (
            ".github/workflows/ci.yml",
            ".github/workflows/domain-scale.yml",
            "configs/performance/domain-scale.v1.json",
            "configs/app.defaults.yml",
            "policies/limits.yaml",
            "policies/performance.v1.json",
            ".python-version",
            "scripts/performance/**",
            "scripts/basemap/run-measured-container.py",
            "scripts/tests/test_domain_scale.py",
            "scripts/ci/tests/test_api_runtime_evidence.py",
            "scripts/ci/tests/test_api_runtime_workflow.py",
            "apps/api/**",
            "Cargo.toml",
            "Cargo.lock",
        ):
            self.assertIn(f"- '{path}'", docs_changes, path)

        runtime_start = docs_changes.index("            api_runtime:")
        runtime_end = docs_changes.index("            heavy:", runtime_start)
        runtime_filter = docs_changes[runtime_start:runtime_end]
        self.assertIn(
            "- 'scripts/ci/postgres-proof-contract.json'",
            runtime_filter,
            "Changing the pinned JetStream image must run T048",
        )

        caller = self.ci_source[
            self.ci_source.index("  api-runtime-proof:\n") : self.ci_source.index(
                "  guard-tests:\n"
            )
        ]
        self.assertIn("needs: docs-changes", caller)
        self.assertIn("needs.docs-changes.outputs.api_runtime == 'true'", caller)
        self.assertIn("github.event_name == 'workflow_dispatch'", caller)
        self.assertIn("uses: ./.github/workflows/domain-scale.yml", caller)

        required = self.ci_source[self.ci_source.index("  required-merge-gate:\n") :]
        self.assertIn("- docs-changes", required)
        self.assertIn("- api-runtime-proof", required)
        self.assertIn('"docs-changes=${{ needs.docs-changes.result }}"', required)
        self.assertIn(
            'api_runtime_result="${{ needs.api-runtime-proof.result }}"', required
        )
        self.assertIn(
            'if [[ "${api_runtime_required}" == "true" ]]', required
        )
        self.assertIn(
            '[[ "${api_runtime_result}" == "success" ]] || failed=1', required
        )
        self.assertIn(
            '[[ "${api_runtime_result}" == "skipped" ]] || failed=1', required
        )


if __name__ == "__main__":
    unittest.main()
