from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github/workflows/staging-image-promotion.yml"
CONTRACT = ROOT / "platform/image-promotion.contract.json"
VERIFIER = ROOT / "scripts/ci/verify_staging_image_attestation.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures/staging_image_promotion"

CHECKOUT = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
BUILDX = "docker/setup-buildx-action@bb05f3f5519dd87d3ba754cc423b652a5edd6d2c"
UPLOAD = "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
DOWNLOAD = "actions/download-artifact@484a0b528fb4d7bd804637ccb632e47a0e638317"
BUILDX_VERSION = "v0.36.1"
BUILDKIT_IMAGE = (
    "moby/buildkit@sha256:"
    "28a898719c18a33f4e8000685287fa36fd0dd9560c6440227d3a732d79bb41d8"
)
COMMIT = "a" * 40
SOURCE = "https://github.com/heimgewebe/commonthing"


SPEC = importlib.util.spec_from_file_location("staging_image_verifier", VERIFIER)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def load_workflow() -> dict:
    payload = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(payload, dict)
    return payload


def load_fixture(name: str) -> dict:
    payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def named_step(job: dict, name: str) -> dict:
    for step in job["steps"]:
        if isinstance(step, dict) and step.get("name") == name:
            return step
    raise AssertionError(f"missing workflow step: {name}")


def uses_steps(job: dict, action: str) -> list[dict]:
    return [
        step
        for step in job["steps"]
        if isinstance(step, dict) and step.get("uses") == action
    ]


class StagingImagePromotionWorkflowTests(unittest.TestCase):
    def test_promotion_is_manual_staging_only_and_minimally_privileged(self) -> None:
        workflow = load_workflow()
        self.assertEqual(set(workflow["on"]), {"workflow_dispatch"})
        self.assertEqual(
            set(workflow["on"]["workflow_dispatch"]["inputs"]), {"source_commit"}
        )
        self.assertEqual(
            workflow["permissions"], {"contents": "read", "packages": "write"}
        )
        self.assertNotIn("actions", workflow["permissions"])
        self.assertNotIn("id-token", workflow["permissions"])

        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("kubectl ", text)
        self.assertNotIn("flux ", text)
        self.assertNotIn("docker/setup-qemu-action@", text)
        self.assertNotIn("production_activation: true", text)

    def test_preflight_rejects_bad_input_before_checkout_and_binds_current_main(
        self,
    ) -> None:
        workflow = load_workflow()
        preflight = workflow["jobs"]["preflight"]
        steps = preflight["steps"]
        self.assertEqual(steps[0]["name"], "Validate dispatch input")
        self.assertEqual(steps[1]["uses"], CHECKOUT)
        self.assertIn("^[0-9a-f]{40}$", steps[0]["run"])

        verify = named_step(preflight, "Verify exact protected-main checkout")["run"]
        self.assertIn('test "$GITHUB_REF" = "refs/heads/main"', verify)
        self.assertIn('test "$GITHUB_SHA" = "$SOURCE_COMMIT"', verify)
        self.assertIn("repos/${GITHUB_REPOSITORY}/git/ref/heads/main", verify)
        self.assertIn('test "$main_head" = "$SOURCE_COMMIT"', verify)
        self.assertIn('test -z "$(git status --porcelain)"', verify)

    def test_native_matrix_is_explicit_and_collects_both_failures(self) -> None:
        workflow = load_workflow()
        build = workflow["jobs"]["build"]
        self.assertEqual(build["needs"], "preflight")
        self.assertEqual(build["strategy"]["fail-fast"], "false")
        matrix = build["strategy"]["matrix"]["include"]
        observed = {(row["runner"], row["platform"], row["arch"]) for row in matrix}
        self.assertEqual(
            observed,
            {
                ("ubuntu-24.04", "linux/amd64", "amd64"),
                ("ubuntu-24.04-arm", "linux/arm64", "arm64"),
            },
        )

    def test_buildx_and_buildkit_are_pinned_independently(self) -> None:
        workflow = load_workflow()
        self.assertEqual(workflow["env"]["BUILDX_VERSION"], BUILDX_VERSION)
        self.assertEqual(workflow["env"]["BUILDKIT_IMAGE"], BUILDKIT_IMAGE)
        for job_name in ("build", "promote"):
            with self.subTest(job=job_name):
                setup_steps = uses_steps(workflow["jobs"][job_name], BUILDX)
                self.assertEqual(len(setup_steps), 1)
                self.assertEqual(
                    setup_steps[0]["with"],
                    {
                        "version": "${{ env.BUILDX_VERSION }}",
                        "driver-opts": "image=${{ env.BUILDKIT_IMAGE }}",
                    },
                )

        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(f"{BUILDX} # tag: v4.2.0", text)
        self.assertIn(f"{CHECKOUT} # tag: v7.0.1", text)
        self.assertIn(f"{UPLOAD} # tag: v7.0.1", text)
        self.assertNotIn(f"{DOWNLOAD} # tag:", text)

    def test_architecture_digests_are_verified_before_export(self) -> None:
        workflow = load_workflow()
        cases = (
            ("Build and verify API by digest", "api"),
            ("Build and verify Web by digest", "web"),
        )
        for step_name, directory in cases:
            with self.subTest(step=step_name):
                run = named_step(workflow["jobs"]["build"], step_name)["run"]
                self.assertIn("--provenance=mode=max,version=v1", run)
                self.assertIn("--sbom=true", run)
                self.assertIn("push-by-digest=true", run)
                self.assertIn("name-canonical=true", run)
                self.assertIn(
                    "verify_staging_image_attestation.py attestation", run
                )
                self.assertIn("--slsa-file", run)
                self.assertIn("--image-file", run)
                self.assertIn("--sbom-file", run)
                self.assertLess(
                    run.index("verify_staging_image_attestation.py attestation"),
                    run.index(f'touch "build/staging-image-digests/{directory}/'),
                )

    def test_buildx_attestation_inspection_matches_pinned_input_shapes(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(text.count("{{json .Provenance.SLSA}}"), 2)
        self.assertEqual(text.count("{{json .Image}}"), 2)
        self.assertEqual(text.count("{{json .SBOM}}"), 2)
        self.assertEqual(text.count("index .Provenance"), 1)
        self.assertEqual(text.count(").SLSA}}"), 1)
        self.assertEqual(text.count("index .Image"), 1)
        self.assertEqual(text.count("index .SBOM"), 1)
        self.assertNotIn("jq -e '.SLSA'", text)
        self.assertNotIn("index .SLSA", text)

    def test_protected_main_is_revalidated_before_native_build_and_promotion(
        self,
    ) -> None:
        workflow = load_workflow()
        for job_name in ("build", "promote"):
            with self.subTest(job=job_name):
                run = named_step(
                    workflow["jobs"][job_name],
                    "Reverify exact protected-main source",
                )["run"]
                self.assertIn('test "$GITHUB_REF" = "refs/heads/main"', run)
                self.assertIn('test "$GITHUB_SHA" = "$SOURCE_COMMIT"', run)
                self.assertIn('test "$main_head" = "$SOURCE_COMMIT"', run)

    def test_same_run_artifact_handoff_does_not_expand_token_permissions(
        self,
    ) -> None:
        workflow = load_workflow()
        self.assertEqual(
            workflow["permissions"], {"contents": "read", "packages": "write"}
        )
        self.assertEqual(len(uses_steps(workflow["jobs"]["build"], UPLOAD)), 1)
        self.assertEqual(len(uses_steps(workflow["jobs"]["promote"], DOWNLOAD)), 1)
        self.assertEqual(len(uses_steps(workflow["jobs"]["promote"], UPLOAD)), 1)

    def test_manifest_is_previewed_verified_and_bound_before_tag_write(self) -> None:
        workflow = load_workflow()
        assemble = named_step(
            workflow["jobs"]["promote"],
            "Assemble canonical multi-arch indexes without overwriting immutable tags",
        )["run"]
        self.assertIn('test "${#api_digests[@]}" -eq 2', assemble)
        self.assertIn('test "${#web_digests[@]}" -eq 2', assemble)
        self.assertEqual(assemble.count("imagetools create --dry-run"), 2)
        self.assertEqual(
            assemble.count("verify_staging_image_attestation.py index"), 2
        )
        self.assertEqual(
            assemble.count("verify_staging_image_attestation.py manifest-digest"),
            2,
        )
        self.assertIn("verify_staging_image_attestation.py tag", assemble)
        self.assertIn("manifest unknown|name unknown|not found", assemble)
        self.assertLess(
            assemble.index("imagetools create --dry-run"),
            assemble.index('ensure_immutable_tag "$api_ref"'),
        )

    def test_final_evidence_aliases_and_receipt_are_digest_authoritative(
        self,
    ) -> None:
        workflow = load_workflow()
        verify = named_step(
            workflow["jobs"]["promote"],
            "Verify indexes, SLSA v1, SBOM, immutable aliases and write receipt",
        )["run"]
        self.assertIn("verify_staging_image_attestation.py index", verify)
        self.assertIn("verify_staging_image_attestation.py attestation", verify)
        self.assertIn("verify_staging_image_attestation.py tag", verify)
        self.assertIn('ensure_immutable_alias "$api_legacy_ref"', verify)
        self.assertIn('ensure_immutable_alias "$web_legacy_ref"', verify)
        self.assertIn('test "$actual_digest" = "$expected_digest"', verify)
        self.assertIn(
            'attestations: ["slsa-v1-provenance-mode-max", "sbom"]', verify
        )
        self.assertIn('image_identity: "digest-authoritative"', verify)
        self.assertIn(
            'tag_policy: "create-or-reuse-same-digest; reject-collision"', verify
        )
        self.assertIn(
            'canonical_reference: ($api_image + "@" + $api_digest)', verify
        )
        self.assertIn(
            'canonical_reference: ($web_image + "@" + $web_digest)', verify
        )

    def test_contract_stays_blocked_until_separate_activation_requirements_pass(
        self,
    ) -> None:
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        self.assertEqual(contract["schema_version"], 2)
        self.assertEqual(contract["status"], "blocked")
        self.assertEqual(contract["workflow_status"], "ready")
        self.assertIs(contract["production_activation"], False)
        self.assertEqual(contract["platforms"], ["linux/amd64", "linux/arm64"])
        self.assertEqual(
            contract["required_images"],
            [
                "ghcr.io/heimgewebe/commonthing-api",
                "ghcr.io/heimgewebe/commonthing-web",
            ],
        )


class StagingImageEvidenceVerifierTests(unittest.TestCase):
    def test_slsa_v1_fixture_passes(self) -> None:
        MODULE.verify_slsa_v1(
            load_fixture("slsa-v1-valid.json"), expected_commit=COMMIT
        )

    def test_slsa_v02_fixture_is_rejected_by_explicit_v1_contract(self) -> None:
        with self.assertRaisesRegex(MODULE.VerificationError, "buildDefinition"):
            MODULE.verify_slsa_v1(
                load_fixture("slsa-v0.2.json"), expected_commit=COMMIT
            )

    def test_wrong_commit_is_rejected(self) -> None:
        with self.assertRaisesRegex(MODULE.VerificationError, "commit"):
            MODULE.verify_slsa_v1(
                load_fixture("slsa-v1-valid.json"), expected_commit="b" * 40
            )

    def test_missing_slsa_completeness_is_rejected(self) -> None:
        payload = copy.deepcopy(load_fixture("slsa-v1-valid.json"))
        del payload["runDetails"]["metadata"]["buildkit_completeness"]
        with self.assertRaisesRegex(MODULE.VerificationError, "completeness"):
            MODULE.verify_slsa_v1(payload, expected_commit=COMMIT)

    def test_missing_sbom_is_rejected(self) -> None:
        with self.assertRaisesRegex(MODULE.VerificationError, "SBOM"):
            MODULE.verify_sbom({})

    def test_wrong_oci_revision_label_is_rejected(self) -> None:
        payload = copy.deepcopy(load_fixture("image-valid.json"))
        payload["config"]["Labels"]["org.opencontainers.image.revision"] = "b" * 40
        with self.assertRaisesRegex(MODULE.VerificationError, "revision"):
            MODULE.verify_image(
                payload,
                expected_commit=COMMIT,
                expected_source=SOURCE,
                expected_platform="linux/amd64",
            )

    def test_index_allows_only_expected_platforms_plus_attestations(self) -> None:
        MODULE.verify_index(load_fixture("index-valid.json"))
        payload = copy.deepcopy(load_fixture("index-valid.json"))
        payload["manifests"].append(
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": "sha256:" + "4" * 64,
                "size": 123,
                "platform": {"os": "linux", "architecture": "s390x"},
            }
        )
        with self.assertRaisesRegex(
            MODULE.VerificationError, "unexpected promoted platform"
        ):
            MODULE.verify_index(payload)

    def test_dry_run_manifest_digest_hashes_exact_bytes_without_cli_newline(
        self,
    ) -> None:
        raw = (FIXTURES / "index-valid.json").read_bytes().rstrip(b"\n")
        expected = "sha256:" + hashlib.sha256(raw).hexdigest()
        self.assertEqual(MODULE.manifest_digest_from_bytes(raw + b"\n"), expected)

    def test_immutable_tag_cases_are_fail_closed(self) -> None:
        fixture = load_fixture("tag-cases.json")
        expected = fixture["expected"]
        self.assertEqual(MODULE.tag_decision(expected, None), "create")
        self.assertEqual(
            MODULE.tag_decision(expected, fixture["same"]), "reuse"
        )
        with self.assertRaisesRegex(MODULE.VerificationError, "different digest"):
            MODULE.tag_decision(expected, fixture["different"])


if __name__ == "__main__":
    unittest.main()
