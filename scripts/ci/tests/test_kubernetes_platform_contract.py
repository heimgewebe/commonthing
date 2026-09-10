from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import shlex
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.error
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[3]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    original_sys_path = list(sys.path)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = original_sys_path
    return module


class KubernetesPlatformContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.validator = load_module(
            "weltgewebe_platform_validator",
            ROOT / "scripts/platform/validate_platform.py",
        )
        cls.reference = load_module(
            "weltgewebe_kind_reference",
            ROOT / "scripts/platform/kind_reference.py",
        )
        cls.bootstrap = load_module(
            "weltgewebe_platform_bootstrap",
            ROOT / "scripts/platform/bootstrap_tools.py",
        )
        cls.proof_identity = load_module(
            "weltgewebe_proof_identity",
            ROOT / "scripts/platform/proof_identity.py",
        )
        cls.oci_mirror = load_module(
            "weltgewebe_oci_proof_mirror",
            ROOT / "scripts/platform/oci_proof_mirror.py",
        )
        cls.staging_cell = load_module(
            "weltgewebe_staging_cell",
            ROOT / "scripts/platform/staging_cell.py",
        )

    def test_toolchain_lock_binds_installed_binary_digests(self) -> None:
        lock = json.loads(
            (ROOT / "platform/toolchain.lock.json").read_text(encoding="utf-8")
        )
        for name, spec in lock["tools"].items():
            with self.subTest(name=name):
                digest = spec.get("binary_sha256")
                self.assertIsInstance(digest, str)
                self.assertEqual(len(digest), 64)
                self.assertTrue(all(ch in "0123456789abcdef" for ch in digest))

    def test_tool_bootstrap_selection_is_exact_and_deduplicated(self) -> None:
        lock = {"tools": {"kustomize": {"version": "x"}, "trivy": {"version": "y"}}}
        selected = self.bootstrap._selected_tool_specs(lock, ["trivy", "trivy"])
        self.assertEqual(selected, {"trivy": {"version": "y"}})
        with self.assertRaisesRegex(RuntimeError, "unknown tool selection"):
            self.bootstrap._selected_tool_specs(lock, ["missing"])

    def test_staging_readme_bootstraps_tools_into_canonical_cache_before_up(self) -> None:
        readme = (ROOT / "platform/README.md").read_text(encoding="utf-8")
        bootstrap = (
            'uv run --project tools/py --locked python scripts/platform/bootstrap_tools.py '
            '--cache "$HOME/.local/state/commonthing/staging-cell/toolchain"'
        )
        up = (
            'uv run --project tools/py --locked python scripts/platform/staging_cell.py '
            'up --owner-id "$COMMONTHING_STAGING_OWNER_ID"'
        )
        self.assertIn('export COMMONTHING_STAGING_OWNER_ID="owner-t084-staging"', readme)
        self.assertIn(bootstrap, readme)
        self.assertIn(up, readme)
        self.assertLess(readme.index(bootstrap), readme.index(up))
        self.assertIn("toolchain/receipt.json", readme)

    def test_tool_download_retries_transient_gateway_failure_and_cleans_temps(self) -> None:
        payload = b"kind-binary"
        expected = hashlib.sha256(payload).hexdigest()
        transient = urllib.error.HTTPError(
            "https://downloads.example/kind", 504, "gateway timeout", {}, None
        )

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "downloads" / "kind"
            with mock.patch.object(
                self.bootstrap.urllib.request,
                "urlopen",
                side_effect=[transient, Response(payload)],
            ) as urlopen, mock.patch.object(self.bootstrap.time, "sleep") as sleep:
                self.bootstrap._download(
                    "https://downloads.example/kind", expected, destination
                )
            self.assertEqual(destination.read_bytes(), payload)
            self.assertEqual(urlopen.call_count, 2)
            sleep.assert_called_once_with(1.0)
            self.assertEqual(list(destination.parent.glob("*.download.tmp")), [])

    def test_tool_download_hash_mismatch_is_integrity_failure_without_retry(self) -> None:
        payload = b"tampered"

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "downloads" / "kind"
            with mock.patch.object(
                self.bootstrap.urllib.request, "urlopen", return_value=Response(payload)
            ) as urlopen, mock.patch.object(self.bootstrap.time, "sleep") as sleep:
                with self.assertRaises(self.bootstrap.DownloadIntegrityError):
                    self.bootstrap._download(
                        "https://downloads.example/kind", "0" * 64, destination
                    )
            urlopen.assert_called_once()
            sleep.assert_not_called()
            self.assertFalse(destination.exists())
            self.assertEqual(list(destination.parent.glob("*.download.tmp")), [])

    def test_tool_download_mirror_is_controlled_and_falls_back_to_canonical(self) -> None:
        canonical = "https://github.com/org/repo/releases/download/v1/tool"
        mirror = self.bootstrap._mirror_url(
            canonical, "https://mirror.example/platform-cache"
        )
        self.assertEqual(
            mirror,
            "https://mirror.example/platform-cache/github.com/org/repo/releases/download/v1/tool",
        )
        with self.assertRaises(self.bootstrap.DownloadError):
            self.bootstrap._mirror_url(canonical, "http://mirror.example/cache")

    def test_tool_download_rechecks_symlink_after_lock_acquisition(self) -> None:
        payload = b"cached"
        expected = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "downloads" / "kind"
            destination.parent.mkdir(parents=True)
            target = root / "target"
            target.write_bytes(payload)

            @contextmanager
            def replace_with_symlink(_destination):
                destination.symlink_to(target)
                yield

            with mock.patch.object(
                self.bootstrap, "_download_lock", side_effect=replace_with_symlink
            ), mock.patch.object(self.bootstrap.urllib.request, "urlopen") as urlopen:
                with self.assertRaises(self.bootstrap.DownloadIntegrityError):
                    self.bootstrap._download(
                        "https://downloads.example/kind", expected, destination
                    )
            urlopen.assert_not_called()

    def test_tool_download_uses_valid_cached_digest_without_network(self) -> None:
        payload = b"cached"
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "downloads" / "kind"
            destination.parent.mkdir(parents=True)
            destination.write_bytes(payload)
            with mock.patch.object(self.bootstrap.urllib.request, "urlopen") as urlopen:
                self.bootstrap._download(
                    "https://downloads.example/kind",
                    hashlib.sha256(payload).hexdigest(),
                    destination,
                )
            urlopen.assert_not_called()

    def test_tool_archive_extracts_regular_files_into_new_root(self) -> None:
        payload = b"helm-binary"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "tool.tar.gz"
            destination = root / "extract"
            with tarfile.open(archive, "w:gz") as handle:
                directory = tarfile.TarInfo("linux-amd64")
                directory.type = tarfile.DIRTYPE
                handle.addfile(directory)
                binary = tarfile.TarInfo("linux-amd64/helm")
                binary.size = len(payload)
                handle.addfile(binary, io.BytesIO(payload))

            self.bootstrap._safe_extract_tar(archive, destination)

            self.assertEqual((destination / "linux-amd64/helm").read_bytes(), payload)

    def test_tool_archive_rejects_symlink_escape_before_any_write(self) -> None:
        payload = b"escape"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "attack.tar.gz"
            destination = root / "extract"
            outside = root / "outside"
            outside.mkdir()
            with tarfile.open(archive, "w:gz") as handle:
                link = tarfile.TarInfo("link")
                link.type = tarfile.SYMTYPE
                link.linkname = "../outside"
                handle.addfile(link)
                file_member = tarfile.TarInfo("link/pwned.txt")
                file_member.size = len(payload)
                handle.addfile(file_member, io.BytesIO(payload))

            with self.assertRaisesRegex(RuntimeError, "unsupported archive member type"):
                self.bootstrap._safe_extract_tar(archive, destination)

            self.assertFalse((outside / "pwned.txt").exists())
            self.assertFalse(destination.exists())

    def test_tool_archive_rejects_links_devices_and_fifo(self) -> None:
        unsafe_types = {
            "hardlink": tarfile.LNKTYPE,
            "character-device": tarfile.CHRTYPE,
            "block-device": tarfile.BLKTYPE,
            "fifo": tarfile.FIFOTYPE,
        }
        for label, member_type in unsafe_types.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                archive = root / "attack.tar.gz"
                destination = root / "extract"
                with tarfile.open(archive, "w:gz") as handle:
                    member = tarfile.TarInfo("unsafe")
                    member.type = member_type
                    if member_type == tarfile.LNKTYPE:
                        member.linkname = "target"
                    handle.addfile(member)

                with self.assertRaisesRegex(
                    RuntimeError, "unsupported archive member type"
                ):
                    self.bootstrap._safe_extract_tar(archive, destination)
                self.assertFalse(destination.exists())

    def test_tool_archive_rejects_parent_traversal_before_any_write(self) -> None:
        payload = b"escape"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "attack.tar.gz"
            destination = root / "extract"
            outside = root / "outside.txt"
            with tarfile.open(archive, "w:gz") as handle:
                member = tarfile.TarInfo("../outside.txt")
                member.size = len(payload)
                handle.addfile(member, io.BytesIO(payload))

            with self.assertRaisesRegex(RuntimeError, "unsafe archive member path"):
                self.bootstrap._safe_extract_tar(archive, destination)

            self.assertFalse(outside.exists())
            self.assertFalse(destination.exists())

    def test_tool_archive_refuses_preexisting_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "tool.tar.gz"
            destination = root / "extract"
            destination.mkdir()
            marker = destination / "marker"
            marker.write_text("keep", encoding="utf-8")
            with tarfile.open(archive, "w:gz") as handle:
                member = tarfile.TarInfo("tool")
                member.size = 1
                handle.addfile(member, io.BytesIO(b"x"))

            with self.assertRaisesRegex(RuntimeError, "destination already exists"):
                self.bootstrap._safe_extract_tar(archive, destination)

            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_manual_cell_profile_keeps_delivery_explicit_and_secrets_external(self) -> None:
        contract = json.loads(
            (ROOT / "platform/cell-profile.contract.json").read_text(encoding="utf-8")
        )
        self.assertEqual(contract["profile_id"], "gewebezelle-manual-v1")
        self.assertEqual(contract["status"], "manual-pilot")
        self.assertFalse(contract["self_service"])
        self.assertFalse(contract["operator_api"])
        self.assertIn(
            "automatic peer discovery or trust", contract["nonclaims"]
        )

        config_map = yaml.safe_load(
            (
                ROOT
                / "platform/apps/weltgewebe/base/config-map.yaml"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            config_map["data"]["FEDERATION_DELIVERY_ENABLED"], "false"
        )

        deployment = yaml.safe_load(
            (
                ROOT
                / "platform/apps/weltgewebe/base/api-deployment.yaml"
            ).read_text(encoding="utf-8")
        )
        env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
        signing_key = next(
            item for item in env if item["name"] == "FEDERATION_SIGNING_KEY_B64"
        )
        secret_ref = signing_key["valueFrom"]["secretKeyRef"]
        self.assertEqual(secret_ref["key"], "federation-signing-key-b64")
        self.assertTrue(secret_ref["optional"])

        secret_contract = json.loads(
            (
                ROOT / "platform/apps/weltgewebe/secret-contract.json"
            ).read_text(encoding="utf-8")
        )
        self.assertIn(
            "federation-signing-key-b64", secret_contract["optional_keys"]
        )
        egress_path = ROOT / contract["artifacts"]["federation_delivery_egress_template"]
        egress_policy = yaml.safe_load(egress_path.read_text(encoding="utf-8"))
        self.assertEqual(egress_policy["apiVersion"], "cilium.io/v2")
        self.assertEqual(egress_policy["kind"], "CiliumNetworkPolicy")
        selector = egress_policy["spec"]["endpointSelector"]["matchLabels"]
        self.assertEqual(selector["app.kubernetes.io/name"], "weltgewebe-api")
        dns_rule, delivery_rule = egress_policy["spec"]["egress"]
        self.assertEqual(
            dns_rule["toEndpoints"],
            [
                {
                    "matchLabels": {
                        "k8s:io.kubernetes.pod.namespace": "kube-system",
                        "k8s:k8s-app": "kube-dns",
                    }
                }
            ],
        )
        self.assertEqual(
            dns_rule["toPorts"],
            [
                {
                    "ports": [
                        {"port": "53", "protocol": "UDP"},
                        {"port": "53", "protocol": "TCP"},
                    ],
                    "rules": {"dns": [{"matchPattern": "*"}]},
                }
            ],
        )
        self.assertEqual(
            delivery_rule["toFQDNs"], [{"matchName": "peer.example.invalid"}]
        )
        self.assertNotIn("matchPattern", delivery_rule["toFQDNs"][0])
        for rule in egress_policy["spec"]["egress"]:
            self.assertNotIn("toEntities", rule)
            self.assertNotIn("toCIDR", rule)
            self.assertNotIn("toCIDRSet", rule)
        self.assertEqual(
            delivery_rule["toPorts"],
            [{"ports": [{"port": "443", "protocol": "TCP"}]}],
        )
        self.assertNotIn(
            "cell-pilot/federation-delivery-egress.yaml",
            (
                ROOT / "platform/apps/weltgewebe/base/kustomization.yaml"
            ).read_text(encoding="utf-8"),
        )
        self.assertIn(
            "rendered cell overlay contains no federation egress placeholder and permits only the exact configured peer DNS names and TCP ports",
            contract["activation_gates"],
        )

        self.assertTrue(
            (ROOT / "docs/runbooks/gewebezelle-manual-pilot.md").is_file()
        )
        self.assertTrue(
            (
                ROOT
                / "apps/api/migrations/20260731000002_federation_delivery_worker.up.sql"
            ).is_file()
        )

    def test_kind_reference_requests_ha_required_kubectl_cnpg_tool(self) -> None:
        source = (ROOT / "scripts/platform/kind_reference.py").read_text(encoding="utf-8")
        self.assertIn('"kubectl_cnpg"', source)

    def test_static_platform_contract_passes(self) -> None:
        result = self.validator.validate(render=False)
        self.assertEqual(result["status"], "pass")

    def test_staging_cell_singleton_cluster_and_receipt_binding(self) -> None:
        self.staging_cell.require_singleton_cluster(self.staging_cell.DEFAULT_CLUSTER)
        with self.assertRaisesRegex(self.staging_cell.StagingCellError, "singleton"):
            self.staging_cell.require_singleton_cluster("other-staging")
        self.staging_cell.require_receipt_cluster(
            {"cluster": self.staging_cell.DEFAULT_CLUSTER},
            self.staging_cell.DEFAULT_CLUSTER,
        )
        with self.assertRaisesRegex(self.staging_cell.StagingCellError, "persisted cluster"):
            self.staging_cell.require_receipt_cluster(
                {"cluster": self.staging_cell.DEFAULT_CLUSTER},
                "other-staging",
            )

    def test_staging_cell_flux_revision_requires_exact_commit(self) -> None:
        commit = "a" * 40
        for revision in (commit, f"sha1:{commit}", f"main@sha1:{commit}"):
            with self.subTest(revision=revision):
                self.assertTrue(
                    self.staging_cell.flux_revision_matches_commit(revision, commit)
                )
        self.assertFalse(
            self.staging_cell.flux_revision_matches_commit(f"sha1:{'b' * 40}", commit)
        )

    def test_staging_cell_rejects_secret_rotation_over_retained_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            root = Path(tmp_name)
            material, source_sha = self.staging_cell.load_or_create_secret_material(root)
            self.staging_cell.atomic_json(
                root / "receipts/cell-bootstrap.json",
                {
                    "schema_version": 1,
                    "cluster": self.staging_cell.DEFAULT_CLUSTER,
                    "external_secret": {"source_sha256": source_sha},
                },
            )
            changed = {"schema_version": 1, **material}
            changed["database_password"] = "replacement"
            self.staging_cell.atomic_json(
                root / "secrets/staging-runtime.json", changed
            )
            with self.assertRaisesRegex(
                self.staging_cell.StagingCellError, "differs from the bootstrap receipt"
            ):
                self.staging_cell.load_or_create_secret_material(root)

    def test_staging_cell_secret_integrity_rejects_changed_values(self) -> None:
        source_sha = "a" * 64
        expected = {"username": "weltgewebe", "password": "secret"}
        document = {
            "metadata": {
                "name": "database",
                "namespace": "data",
                "annotations": {
                    "commonthing.net/external-secret-source-sha256": source_sha
                },
            },
            "data": {
                key: self.staging_cell.base64.b64encode(value.encode()).decode()
                for key, value in expected.items()
            },
        }
        self.assertTrue(
            self.staging_cell.secret_document_matches(
                document,
                name="database",
                namespace_name="data",
                source_sha=source_sha,
                expected_values=expected,
            )
        )
        document["data"]["password"] = self.staging_cell.base64.b64encode(
            b"changed"
        ).decode()
        self.assertFalse(
            self.staging_cell.secret_document_matches(
                document,
                name="database",
                namespace_name="data",
                source_sha=source_sha,
                expected_values=expected,
            )
        )

    def test_oci_proof_mirror_contract_is_private_digest_bound_and_budgeted(self) -> None:
        result = self.oci_mirror.validate_contract()
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["owner"], "heimgewebe/weltgewebe")
        self.assertEqual(result["source_kind"], "private-ghcr-digest-mirror")
        self.assertEqual(
            result["mirror_repository"],
            "ghcr.io/heimgewebe/weltgewebe-proof-oci",
        )
        self.assertEqual(result["visibility"], "private")
        self.assertEqual(result["repository_binding"], "heimgewebe/commonthing")
        self.assertEqual(result["image_count"], 25)
        self.assertTrue(result["retention"]["unbounded_growth_prevented"])
        self.assertEqual(result["retention"]["observed_package_versions"], 193)
        self.assertEqual(result["retention"]["package_version_hard_limit"], 512)
        self.assertEqual(result["retention"]["orphan_grace_days"], 14)

    def test_oci_mirror_suite_selection_is_exact(self) -> None:
        lock = self.oci_mirror._load_lock()
        kind = self.oci_mirror._selected_images(
            lock, ["kind-gitops", "app-build"]
        )
        ha = self.oci_mirror._selected_images(
            lock, ["ha-recovery", "app-build"]
        )
        self.assertEqual(len(kind), 15)
        self.assertEqual(len(ha), 23)
        self.assertEqual(len({name for name, _spec in kind}), 15)
        self.assertEqual(len({name for name, _spec in ha}), 23)

    def test_oci_mirror_wrong_digest_fails_without_retry(self) -> None:
        digest = "sha256:" + "a" * 64
        spec = {
            "mirror": "ghcr.io/heimgewebe/weltgewebe-proof-oci@" + digest,
            "local_ref": "example.invalid/image:test",
        }
        budgets = {"pull_attempts": 3, "retry_backoff_seconds": [5, 10]}
        with mock.patch.object(
            self.oci_mirror, "_local_image", return_value=None
        ), mock.patch.object(self.oci_mirror, "_run") as run, mock.patch.object(
            self.oci_mirror,
            "_repo_digests",
            return_value=["ghcr.io/heimgewebe/weltgewebe-proof-oci@sha256:" + "b" * 64],
        ), mock.patch.object(self.oci_mirror.time, "sleep") as sleep:
            with self.assertRaises(self.oci_mirror.IntegrityError):
                self.oci_mirror._pull_one("test", spec, budgets)
        run.assert_called_once_with(["docker", "pull", spec["mirror"]], timeout=900)
        sleep.assert_not_called()

    def test_oci_mirror_unavailable_uses_bounded_retries(self) -> None:
        digest = "sha256:" + "a" * 64
        spec = {
            "mirror": "ghcr.io/heimgewebe/weltgewebe-proof-oci@" + digest,
            "local_ref": "example.invalid/image:test",
        }
        budgets = {"pull_attempts": 3, "retry_backoff_seconds": [5, 10]}
        error = subprocess.CalledProcessError(1, ["docker", "pull"])
        with mock.patch.object(
            self.oci_mirror, "_local_image", return_value=None
        ), mock.patch.object(
            self.oci_mirror, "_run", side_effect=error
        ) as run, mock.patch.object(self.oci_mirror.time, "sleep") as sleep:
            with self.assertRaises(
                self.oci_mirror.ControlledMirrorUnavailableError
            ):
                self.oci_mirror._pull_one("test", spec, budgets)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [5.0, 10.0],
        )

    def test_oci_mirror_pull_tags_only_after_exact_digest_verification(self) -> None:
        digest = "sha256:" + "a" * 64
        spec = {
            "mirror": "ghcr.io/heimgewebe/weltgewebe-proof-oci@" + digest,
            "local_ref": "example.invalid/image:test",
        }
        budgets = {"pull_attempts": 3, "retry_backoff_seconds": [5, 10]}
        verified = {"image_id": "sha256:" + "c" * 64, "source": "local-verified"}
        with mock.patch.object(
            self.oci_mirror, "_local_image", side_effect=[None, verified]
        ), mock.patch.object(self.oci_mirror, "_run") as run, mock.patch.object(
            self.oci_mirror, "_repo_digests", return_value=[spec["mirror"]]
        ):
            result = self.oci_mirror._pull_one("test", spec, budgets)
        self.assertEqual(result["action"], "pulled")
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                ["docker", "pull", spec["mirror"]],
                ["docker", "tag", spec["mirror"], spec["local_ref"]],
            ],
        )

    def test_oci_mirror_normalizes_containerd_references(self) -> None:
        digest = "sha256:" + "a" * 64
        cases = {
            "nats:2.10-alpine": "docker.io/library/nats:2.10-alpine",
            "natsio/nats-box:v1": "docker.io/natsio/nats-box:v1",
            f"postgres:16@{digest}": f"docker.io/library/postgres:16@{digest}",
            "quay.io/cilium/cilium:v1": "quay.io/cilium/cilium:v1",
            "localhost:5000/proof:v1": "localhost:5000/proof:v1",
        }
        for reference, expected in cases.items():
            with self.subTest(reference=reference):
                self.assertEqual(
                    self.oci_mirror._containerd_reference(reference), expected
                )

    def test_oci_mirror_cri_binding_uses_exact_runtime_tag(self) -> None:
        locked_digest = "sha256:" + "a" * 64
        platform_digest = "sha256:" + "b" * 64
        image_id = "sha256:" + "c" * 64
        local_ref = "nats:2.10-alpine"
        runtime_ref = "docker.io/library/nats:2.10-alpine"
        payload = {
            "status": {
                "id": image_id,
                "repoTags": [runtime_ref],
                "repoDigests": [f"{runtime_ref}@{platform_digest}"],
            }
        }
        with mock.patch.object(
            self.oci_mirror, "_output", return_value=json.dumps(payload)
        ) as output:
            result = self.oci_mirror._kind_cri_image_binding(
                "proof-control-plane",
                local_ref,
                f"{runtime_ref}@{locked_digest}",
                locked_digest,
                image_id,
            )
        output.assert_called_once_with(
            [
                "docker",
                "exec",
                "proof-control-plane",
                "crictl",
                "--runtime-endpoint",
                "unix:///run/containerd/containerd.sock",
                "inspecti",
                runtime_ref,
            ],
            timeout=60,
        )
        self.assertEqual(result["runtime_ref"], runtime_ref)
        self.assertEqual(result["image_id"], image_id)
        self.assertEqual(result["locked_index_digest"], locked_digest)
        self.assertEqual(result["platform_target_digest"], platform_digest)
        self.assertEqual(result["platform_evidence_kind"], "cri_repo_digest")
        self.assertIsNone(result["containerd_target_digest"])
        self.assertIs(result["cri_image_status_verified"], True)

    def test_oci_mirror_cri_binding_falls_back_to_exact_containerd_target(
        self,
    ) -> None:
        locked_digest = "sha256:" + "a" * 64
        platform_digest = "sha256:" + "b" * 64
        image_id = "sha256:" + "c" * 64
        local_ref = "quay.io/cilium/cilium:v1.19.5"
        payload = {
            "status": {
                "id": image_id,
                "repoTags": [local_ref],
                "repoDigests": [],
            }
        }
        listing = (
            "REF TYPE DIGEST SIZE PLATFORMS LABELS\n"
            f"{local_ref} application/vnd.oci.image.index.v1+json "
            f"{platform_digest} 1.0MiB linux/amd64 -"
        )
        with mock.patch.object(
            self.oci_mirror,
            "_output",
            side_effect=[json.dumps(payload), listing],
        ) as output:
            result = self.oci_mirror._kind_cri_image_binding(
                "proof-control-plane",
                local_ref,
                f"{local_ref}@{locked_digest}",
                locked_digest,
                image_id,
            )
        self.assertEqual(output.call_count, 2)
        self.assertEqual(result["runtime_ref"], local_ref)
        self.assertEqual(result["repo_digests"], [])
        self.assertIsNone(result["selected_repo_digest"])
        self.assertEqual(result["containerd_target_digest"], platform_digest)
        self.assertEqual(
            result["platform_evidence_kind"], "containerd_target_digest"
        )
        self.assertEqual(result["platform_target_digest"], platform_digest)

    def test_oci_mirror_cri_binding_rejects_unrelated_repo_digest(
        self,
    ) -> None:
        locked_digest = "sha256:" + "a" * 64
        platform_digest = "sha256:" + "b" * 64
        image_id = "sha256:" + "c" * 64
        runtime_ref = "quay.io/cilium/cilium:v1.19.5"
        payload = {
            "status": {
                "id": image_id,
                "repoTags": [runtime_ref],
                "repoDigests": [
                    "quay.io/other/image@" + platform_digest
                ],
            }
        }
        with mock.patch.object(
            self.oci_mirror, "_output", return_value=json.dumps(payload)
        ) as output:
            with self.assertRaisesRegex(
                self.oci_mirror.IntegrityError, "ambiguous platform evidence"
            ):
                self.oci_mirror._kind_cri_image_binding(
                    "proof-control-plane",
                    runtime_ref,
                    f"{runtime_ref}@{locked_digest}",
                    locked_digest,
                    image_id,
                )
        output.assert_called_once()

    def test_oci_mirror_cri_binding_accepts_single_kind_import_digest(self) -> None:
        locked_digest = "sha256:" + "a" * 64
        platform_digest = "sha256:" + "b" * 64
        image_id = "sha256:" + "c" * 64
        local_ref = "chrislusf/seaweedfs:weltgewebe-test"
        runtime_ref = "docker.io/chrislusf/seaweedfs:weltgewebe-test"
        import_ref = f"docker.io/library/import-2026-07-26@{platform_digest}"
        payload = {
            "status": {
                "id": image_id,
                "repoTags": [runtime_ref],
                "repoDigests": [import_ref],
            }
        }
        with mock.patch.object(
            self.oci_mirror, "_output", return_value=json.dumps(payload)
        ):
            result = self.oci_mirror._kind_cri_image_binding(
                "proof-control-plane",
                local_ref,
                f"docker.io/chrislusf/seaweedfs@{locked_digest}",
                locked_digest,
                image_id,
            )
        self.assertEqual(result["runtime_ref"], runtime_ref)
        self.assertEqual(result["selected_repo_digest"], import_ref)
        self.assertEqual(result["platform_evidence_kind"], "cri_repo_digest")
        self.assertIsNone(result["containerd_target_digest"])
        self.assertEqual(result["platform_target_digest"], platform_digest)

    def test_oci_mirror_cri_binding_rejects_host_image_id_drift(self) -> None:
        locked_digest = "sha256:" + "a" * 64
        platform_digest = "sha256:" + "b" * 64
        runtime_ref = "docker.io/library/nats:2.10-alpine"
        payload = {
            "status": {
                "id": "sha256:" + "c" * 64,
                "repoTags": [runtime_ref],
                "repoDigests": [f"{runtime_ref}@{platform_digest}"],
            }
        }
        with mock.patch.object(
            self.oci_mirror, "_output", return_value=json.dumps(payload)
        ):
            with self.assertRaisesRegex(
                self.oci_mirror.IntegrityError, "CRI image ID drift"
            ):
                self.oci_mirror._kind_cri_image_binding(
                    "proof-control-plane",
                    "nats:2.10-alpine",
                    f"{runtime_ref}@{locked_digest}",
                    locked_digest,
                    "sha256:" + "d" * 64,
                )

    def test_oci_mirror_cri_binding_rejects_missing_runtime_tag(self) -> None:
        locked_digest = "sha256:" + "a" * 64
        image_id = "sha256:" + "c" * 64
        payload = {
            "status": {
                "id": image_id,
                "repoTags": ["docker.io/library/other:v1"],
                "repoDigests": [],
            }
        }
        with mock.patch.object(
            self.oci_mirror, "_output", return_value=json.dumps(payload)
        ):
            with self.assertRaisesRegex(
                self.oci_mirror.IntegrityError, "runtime tag is absent"
            ):
                self.oci_mirror._kind_cri_image_binding(
                    "proof-control-plane",
                    "nats:2.10-alpine",
                    "docker.io/library/nats:2.10-alpine@" + locked_digest,
                    locked_digest,
                    image_id,
                )

    def test_oci_mirror_internal_kind_commands_do_not_pollute_json_stdout(self) -> None:
        source = (ROOT / "scripts/platform/oci_proof_mirror.py").read_text()
        self.assertIn(
            '[kind, "load", "docker-image", "--name", cluster, *local_refs],\n        capture=True,',
            source,
        )
        self.assertNotIn('"images", "tag", "--force"', source)
        self.assertIn('"crictl",\n            "--runtime-endpoint"', source)

    def test_oci_mirror_blocks_registries_inside_kind_node(self) -> None:
        def output(argv, *, timeout=120):
            del timeout
            address = "127.0.0.1" if "ahostsv4" in argv else "::1"
            return f"{address} STREAM {argv[-1]}"

        with mock.patch.object(self.oci_mirror, "_run") as run, mock.patch.object(
            self.oci_mirror, "_output", side_effect=output
        ):
            result = self.oci_mirror._block_kind_registries("proof-control-plane")
        run.assert_called_once_with(
            [
                "docker", "exec", "proof-control-plane", "sh", "-ceu",
                mock.ANY,
            ],
            capture=True,
            timeout=60,
        )
        command = run.call_args.args[0][-1]
        self.assertIn("weltgewebe strict OCI registry blockade", command)
        for registry in self.oci_mirror.BLOCKED_REGISTRIES:
            self.assertIn(registry, command)
            self.assertEqual(result["registries"][registry]["ipv4"], ["127.0.0.1"])
            self.assertEqual(result["registries"][registry]["ipv6"], ["::1"])

    def test_oci_mirror_live_package_budget_is_fail_closed(self) -> None:
        lock = self.oci_mirror._load_lock()
        package = {
            "id": lock["mirror"]["package_id"],
            "name": "weltgewebe-proof-oci",
            "package_type": "container",
            "visibility": "private",
            "repository": {"full_name": "heimgewebe/commonthing"},
            "version_count": lock["budgets"]["package_version_hard_limit"] + 1,
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            self.oci_mirror, "_output", return_value=json.dumps(package)
        ):
            state = Path(tmp)
            with self.assertRaises(self.oci_mirror.BudgetError):
                self.oci_mirror.verify_live_package(state)
            receipt = json.loads((state / "live-package-receipt.json").read_text())
        self.assertEqual(receipt["status"], "fail")
        self.assertEqual(
            receipt["failure_class"], "integrity_or_budget_mismatch"
        )

    def test_oci_mirror_lock_rejects_target_digest_injection(self) -> None:
        lock = json.loads(
            (ROOT / "platform/oci-proof-mirror.lock.json").read_text()
        )
        lock["images"]["kind_node"]["mirror"] = (
            "ghcr.io/heimgewebe/weltgewebe-proof-oci@sha256:" + "0" * 64
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lock.json"
            path.write_text(json.dumps(lock))
            with mock.patch.object(self.oci_mirror, "LOCK_PATH", path):
                with self.assertRaises(self.oci_mirror.IntegrityError):
                    self.oci_mirror._load_lock()

    def test_oci_mirror_lock_rejects_seed_binding_drift(self) -> None:
        lock = json.loads(
            (ROOT / "platform/oci-proof-mirror.lock.json").read_text()
        )
        lock["images"]["kind_node"]["canonical"] = (
            "docker.io/kindest/node@sha256:" + "0" * 64
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lock.json"
            path.write_text(json.dumps(lock))
            with mock.patch.object(self.oci_mirror, "LOCK_PATH", path):
                with self.assertRaises(self.oci_mirror.IntegrityError):
                    self.oci_mirror._load_lock()

    def _write_mutated_oci_lock(self, mutate) -> Path:
        lock = json.loads(
            (ROOT / "platform/oci-proof-mirror.lock.json").read_text(encoding="utf-8")
        )
        mutate(lock)
        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        try:
            json.dump(lock, handle)
            handle.flush()
        finally:
            handle.close()
        return Path(handle.name)

    def test_oci_mirror_lock_rejects_unreachable_source_head(self) -> None:
        """Network-independent: synthetic commit id is not a local object."""
        path = self._write_mutated_oci_lock(
            lambda lock: lock["generation"].__setitem__(
                "source_head", "deadbeef" + "0" * 32
            )
        )
        try:
            with mock.patch.object(self.oci_mirror, "LOCK_PATH", path):
                with self.assertRaisesRegex(
                    self.oci_mirror.AncestryError, "unreachable"
                ):
                    self.oci_mirror._load_lock()
        finally:
            path.unlink(missing_ok=True)

    def test_oci_mirror_lock_rejects_source_head_outside_allowed_ancestry(self) -> None:
        """Network-independent: reachable object that is not an ancestor of HEAD."""
        # Orphan commit object in the object store, never linked into HEAD history.
        empty_tree = subprocess.check_output(
            ["git", "hash-object", "-t", "tree", "-w", "--stdin"],
            cwd=ROOT,
            input=b"",
        ).decode().strip()
        orphan_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "oci-ancestry-test",
            "GIT_AUTHOR_EMAIL": "oci-ancestry-test@invalid",
            "GIT_COMMITTER_NAME": "oci-ancestry-test",
            "GIT_COMMITTER_EMAIL": "oci-ancestry-test@invalid",
        }
        orphan = subprocess.check_output(
            [
                "git",
                "commit-tree",
                empty_tree,
                "-m",
                "oci-ancestry-negative-orphan",
            ],
            cwd=ROOT,
            text=True,
            env=orphan_env,
        ).strip()
        self.assertRegex(orphan, r"^[0-9a-f]{40}$")
        not_ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", orphan, "HEAD"],
            cwd=ROOT,
            capture_output=True,
        )
        self.assertNotEqual(not_ancestor.returncode, 0)
        path = self._write_mutated_oci_lock(
            lambda lock: lock["generation"].__setitem__("source_head", orphan)
        )
        try:
            with mock.patch.object(self.oci_mirror, "LOCK_PATH", path):
                with self.assertRaisesRegex(
                    self.oci_mirror.AncestryError, "outside allowed ancestry"
                ):
                    self.oci_mirror._load_lock()
        finally:
            path.unlink(missing_ok=True)

    def test_oci_mirror_lock_rejects_manipulated_seed_sha256(self) -> None:
        """Network-independent: seed digest drift fails before full validation."""
        path = self._write_mutated_oci_lock(
            lambda lock: lock["generation"].__setitem__("seed_sha256", "a" * 64)
        )
        try:
            with mock.patch.object(self.oci_mirror, "LOCK_PATH", path):
                with self.assertRaisesRegex(
                    self.oci_mirror.AncestryError, "not bound to source_head"
                ):
                    self.oci_mirror._load_lock()
        finally:
            path.unlink(missing_ok=True)

    def test_oci_mirror_generation_ancestry_runs_before_inventory_checks(self) -> None:
        """Ancestry failure must not require a complete images section."""
        lock = json.loads(
            (ROOT / "platform/oci-proof-mirror.lock.json").read_text(encoding="utf-8")
        )
        lock["generation"]["source_head"] = "deadbeef" + "0" * 32
        lock["images"] = {}  # would fail inventory if ancestry were deferred
        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        try:
            json.dump(lock, handle)
            handle.flush()
        finally:
            handle.close()
        path = Path(handle.name)
        try:
            with mock.patch.object(self.oci_mirror, "LOCK_PATH", path):
                with self.assertRaisesRegex(
                    self.oci_mirror.AncestryError, "unreachable"
                ):
                    self.oci_mirror._load_lock()
        finally:
            path.unlink(missing_ok=True)

    def test_strict_oci_rewrites_locked_digests_to_verified_runtime_tags(self) -> None:
        locked = "sha256:" + "a" * 64
        source = f"nats:2.10-alpine@{locked}"
        runtime = "docker.io/library/nats:2.10-alpine"
        documents = [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [{"name": "nats", "image": source}]
                        }
                    }
                },
            },
            {
                "apiVersion": "postgresql.cnpg.io/v1",
                "kind": "Cluster",
                "spec": {"imageName": source},
            },
        ]
        refs = {self.reference._normalize_oci_reference(source): runtime}
        with mock.patch.dict(
            os.environ, {self.reference.OCI_STRICT_ENV: "1"}
        ), mock.patch.object(self.reference, "_CONTROLLED_OCI_RUNTIME_REFS", refs):
            result = self.reference.enforce_controlled_oci_pull_policy(documents)
        self.assertEqual(
            result[0]["spec"]["template"]["spec"]["containers"][0]["image"],
            runtime,
        )
        self.assertEqual(result[1]["spec"]["imageName"], runtime)
        self.assertEqual(
            result[0]["spec"]["template"]["spec"]["containers"][0]["imagePullPolicy"],
            "Never",
        )
        self.assertEqual(result[1]["spec"]["imagePullPolicy"], "Never")

    def test_strict_oci_locked_digest_without_runtime_tag_fails_closed(self) -> None:
        locked = "sha256:" + "a" * 64
        reference = f"nats:2.10-alpine@{locked}"
        lock = {
            "images": {
                "local_nats": {
                    "canonical": f"docker.io/library/nats:2.10-alpine@{locked}",
                    "local_ref": "nats:2.10-alpine",
                    "digest": locked,
                    "load_into_kind": True,
                }
            }
        }
        with mock.patch.dict(
            os.environ, {self.reference.OCI_STRICT_ENV: "1"}
        ), mock.patch.object(self.reference, "_CONTROLLED_OCI_RUNTIME_REFS", {}), mock.patch.object(
            self.reference, "_oci_mirror_lock", return_value=lock
        ):
            with self.assertRaisesRegex(
                self.reference.ProofError, "runtime tag is unavailable"
            ):
                self.reference.controlled_oci_runtime_image(reference)

    def test_strict_oci_kind_receipt_binds_host_image_to_cri_tag(self) -> None:
        locked = "sha256:" + "a" * 64
        platform = "sha256:" + "b" * 64
        image_id = "sha256:" + "c" * 64
        canonical = f"docker.io/library/nats:2.10-alpine@{locked}"
        runtime = "docker.io/library/nats:2.10-alpine"
        lock = {
            "images": {
                "local_nats": {
                    "canonical": canonical,
                    "local_ref": "nats:2.10-alpine",
                    "digest": locked,
                    "suites": ["kind-gitops"],
                    "load_into_kind": True,
                }
            }
        }
        node = {
            "node": "proof-control-plane",
            "runtime_ref": runtime,
            "image_id": image_id,
            "repo_tags": [runtime],
            "repo_digests": [f"{runtime}@{platform}"],
            "selected_repo_digest": f"{runtime}@{platform}",
            "containerd_target_digest": None,
            "platform_evidence_kind": "cri_repo_digest",
            "locked_index_digest": locked,
            "platform_target_digest": platform,
            "cri_image_status_verified": True,
        }
        receipt = {
            "status": "pass",
            "strict": True,
            "cluster": "proof",
            "loaded_count": 1,
            "registry_blockades": [{"node": "proof-control-plane"}],
            "images": {
                "local_nats": {
                    "canonical": canonical,
                    "local_ref": "nats:2.10-alpine",
                    "runtime_ref": runtime,
                    "locked_index_digest": locked,
                    "platform_target_digest": platform,
                    "cri_image_id": image_id,
                    "image_id": image_id,
                    "nodes": [node],
                }
            },
        }
        with mock.patch.object(self.reference, "_oci_mirror_lock", return_value=lock):
            refs, digests, image_ids = (
                self.reference._validate_controlled_oci_kind_receipt(
                    receipt, "kind-gitops", "proof"
                )
            )
        self.assertEqual(refs[self.reference._normalize_oci_reference(canonical)], runtime)
        self.assertEqual(
            refs[self.reference._normalize_oci_reference("nats:2.10-alpine")],
            runtime,
        )
        self.assertEqual(digests[runtime], platform)
        self.assertEqual(image_ids[runtime], image_id)

    def test_strict_oci_kind_receipt_accepts_containerd_target_fallback(
        self,
    ) -> None:
        locked = "sha256:" + "a" * 64
        platform = "sha256:" + "b" * 64
        image_id = "sha256:" + "c" * 64
        canonical = f"quay.io/cilium/cilium:v1.19.5@{locked}"
        runtime = "quay.io/cilium/cilium:v1.19.5"
        lock = {
            "images": {
                "cilium_agent": {
                    "canonical": canonical,
                    "local_ref": runtime,
                    "digest": locked,
                    "suites": ["kind-gitops"],
                    "load_into_kind": True,
                }
            }
        }
        node = {
            "node": "proof-control-plane",
            "runtime_ref": runtime,
            "image_id": image_id,
            "repo_tags": [runtime],
            "repo_digests": [],
            "selected_repo_digest": None,
            "containerd_target_digest": platform,
            "platform_evidence_kind": "containerd_target_digest",
            "locked_index_digest": locked,
            "platform_target_digest": platform,
            "cri_image_status_verified": True,
        }
        receipt = {
            "status": "pass",
            "strict": True,
            "cluster": "proof",
            "loaded_count": 1,
            "registry_blockades": [{"node": "proof-control-plane"}],
            "images": {
                "cilium_agent": {
                    "canonical": canonical,
                    "local_ref": runtime,
                    "runtime_ref": runtime,
                    "locked_index_digest": locked,
                    "platform_target_digest": platform,
                    "cri_image_id": image_id,
                    "image_id": image_id,
                    "nodes": [node],
                }
            },
        }
        with mock.patch.object(
            self.reference, "_oci_mirror_lock", return_value=lock
        ):
            refs, digests, image_ids = (
                self.reference._validate_controlled_oci_kind_receipt(
                    receipt, "kind-gitops", "proof"
                )
            )
        self.assertEqual(refs[self.reference._normalize_oci_reference(canonical)], runtime)
        self.assertEqual(digests[runtime], platform)
        self.assertEqual(image_ids[runtime], image_id)

    def test_strict_cilium_helm_disables_digest_resolution(self) -> None:
        artifacts = {
            **{name: f"/{name}.yaml" for name in self.reference.GATEWAY_API_ARTIFACTS},
            "cilium_chart": "/cilium.tgz",
        }
        with mock.patch.dict(
            os.environ, {self.reference.OCI_STRICT_ENV: "1"}
        ), mock.patch.object(self.reference, "apply_file"), mock.patch.object(
            self.reference, "run"
        ) as run, mock.patch.object(
            self.reference, "wait_rollout"
        ), mock.patch.object(
            self.reference, "output", return_value=""
        ), mock.patch.object(
            self.reference, "apply_yaml"
        ):
            self.reference.install_platform_components(
                "kubectl", "flux", "helm", artifacts, "127.0.0.1"
            )
        helm_argv = run.call_args_list[0].args[0]
        for value in (
            "image.useDigest=false",
            "operator.image.useDigest=false",
            "envoy.image.useDigest=false",
            "hubble.relay.image.useDigest=false",
        ):
            self.assertIn(value, helm_argv)

    def test_strict_flux_local_data_patches_digest_images_to_runtime_tags(self) -> None:
        lock = {
            "images": {
                "local_postgres": {"canonical": "postgres@sha256:" + "a" * 64},
                "local_nats": {"canonical": "nats@sha256:" + "b" * 64},
            }
        }
        runtime = {
            self.reference._normalize_oci_reference(lock["images"]["local_postgres"]["canonical"]): "docker.io/library/postgres:16",
            self.reference._normalize_oci_reference(lock["images"]["local_nats"]["canonical"]): "docker.io/library/nats:2.10-alpine",
        }
        with mock.patch.dict(
            os.environ, {self.reference.OCI_STRICT_ENV: "1"}
        ), mock.patch.object(
            self.reference, "_oci_mirror_lock", return_value=lock
        ), mock.patch.object(
            self.reference, "_CONTROLLED_OCI_RUNTIME_REFS", runtime
        ):
            document = self.reference.flux_kustomization_document(
                ROOT / "platform/clusters/local/local-data.yaml"
            )
        patches = document["spec"]["patches"]
        self.assertEqual(len(patches), 2)
        rendered = [yaml.safe_load(item["patch"]) for item in patches]
        observed = {
            item["metadata"]["name"]: item["spec"]["template"]["spec"]["containers"][0]
            for item in rendered
        }
        self.assertEqual(observed["postgres"]["image"], "docker.io/library/postgres:16")
        self.assertEqual(observed["nats"]["image"], "docker.io/library/nats:2.10-alpine")
        self.assertEqual({item["imagePullPolicy"] for item in observed.values()}, {"Never"})

    def test_strict_oci_runtime_digest_is_receipt_bound(self) -> None:
        locked = "sha256:" + "a" * 64
        platform = "sha256:" + "b" * 64
        source = f"nats:2.10-alpine@{locked}"
        runtime = "docker.io/library/nats:2.10-alpine"
        with mock.patch.dict(
            os.environ, {self.reference.OCI_STRICT_ENV: "1"}
        ), mock.patch.object(
            self.reference,
            "_CONTROLLED_OCI_RUNTIME_REFS",
            {self.reference._normalize_oci_reference(source): runtime},
        ), mock.patch.object(
            self.reference,
            "_CONTROLLED_OCI_RUNTIME_DIGESTS",
            {runtime: platform},
        ):
            self.assertEqual(
                self.reference.controlled_oci_runtime_digest(source), platform
            )

    def test_strict_oci_runtime_identities_include_target_and_image_id(
        self,
    ) -> None:
        locked = "sha256:" + "a" * 64
        platform = "sha256:" + "b" * 64
        image_id = "sha256:" + "c" * 64
        source = f"nats:2.10-alpine@{locked}"
        runtime = "docker.io/library/nats:2.10-alpine"
        with mock.patch.dict(
            os.environ, {self.reference.OCI_STRICT_ENV: "1"}
        ), mock.patch.object(
            self.reference,
            "_CONTROLLED_OCI_RUNTIME_REFS",
            {self.reference._normalize_oci_reference(source): runtime},
        ), mock.patch.object(
            self.reference,
            "_CONTROLLED_OCI_RUNTIME_DIGESTS",
            {runtime: platform},
        ), mock.patch.object(
            self.reference,
            "_CONTROLLED_OCI_RUNTIME_IMAGE_IDS",
            {runtime: image_id},
        ):
            identities = self.reference.controlled_oci_runtime_identity_digests(
                source
            )
        self.assertEqual(identities, {platform, image_id})

    def test_strict_oci_dockerfiles_use_verified_local_base_tags(self) -> None:
        with mock.patch.dict(
            os.environ,
            {self.reference.OCI_STRICT_ENV: "1"},
            clear=False,
        ):
            api = self.reference.controlled_oci_dockerfile(
                ROOT / "apps/api/Dockerfile"
            )
            web = self.reference.controlled_oci_dockerfile(
                ROOT / "apps/web/Dockerfile"
            )
        api_from = [line for line in api.splitlines() if line.startswith("FROM ")]
        web_from = [line for line in web.splitlines() if line.startswith("FROM ")]
        self.assertTrue(api_from)
        self.assertTrue(web_from)
        self.assertFalse(any("@sha256:" in line for line in api_from + web_from))
        self.assertIn("FROM rust:1.89.0-bookworm AS builder", api_from)
        self.assertIn("FROM debian:bookworm-slim", api_from)
        self.assertIn("FROM node:20.19.0-alpine AS builder", web_from)
        self.assertIn("FROM caddy:2.7", web_from)

    def test_strict_oci_dockerfile_rejects_comment_only_contract_tokens(self) -> None:
        lock = json.loads(
            (ROOT / "platform/oci-proof-mirror.lock.json").read_text(encoding="utf-8")
        )
        rust = lock["images"]["build_rust"]
        debian = lock["images"]["build_debian"]
        source = (
            f"# {rust['local_ref']}@{rust['digest']}\n"
            f"# {debian['local_ref']}@{debian['digest']}\n"
            "FROM attacker.example/unreviewed:latest\n"
        )
        with mock.patch.dict(
            os.environ, {self.reference.OCI_STRICT_ENV: "1"}
        ), mock.patch.object(
            self.reference, "_oci_mirror_lock", return_value=lock
        ), mock.patch.object(
            self.reference.Path, "read_text", return_value=source
        ):
            with self.assertRaisesRegex(
                self.reference.ProofError, "uncontrolled OCI base image"
            ):
                self.reference.controlled_oci_dockerfile(Path("apps/api/Dockerfile"))

    def test_strict_oci_dockerfile_rejects_additional_external_stage(self) -> None:
        lock = json.loads(
            (ROOT / "platform/oci-proof-mirror.lock.json").read_text(encoding="utf-8")
        )
        rust = lock["images"]["build_rust"]
        debian = lock["images"]["build_debian"]
        source = (
            f"FROM {rust['local_ref']}@{rust['digest']} AS builder\n"
            f"FROM {debian['local_ref']}@{debian['digest']}\n"
            "FROM attacker.example/unreviewed:latest AS injected\n"
        )
        with mock.patch.dict(
            os.environ, {self.reference.OCI_STRICT_ENV: "1"}
        ), mock.patch.object(
            self.reference, "_oci_mirror_lock", return_value=lock
        ), mock.patch.object(
            self.reference.Path, "read_text", return_value=source
        ):
            with self.assertRaisesRegex(
                self.reference.ProofError, "uncontrolled OCI base image"
            ):
                self.reference.controlled_oci_dockerfile(Path("apps/api/Dockerfile"))

    def test_strict_oci_dockerfile_rejects_tab_separated_external_stage(self) -> None:
        lock = json.loads(
            (ROOT / "platform/oci-proof-mirror.lock.json").read_text(encoding="utf-8")
        )
        rust = lock["images"]["build_rust"]
        debian = lock["images"]["build_debian"]
        source = (
            f"FROM {rust['local_ref']}@{rust['digest']} AS builder\n"
            f"FROM {debian['local_ref']}@{debian['digest']}\n"
            "FROM\tattacker.example/unreviewed:latest AS injected\n"
        )
        with mock.patch.dict(
            os.environ, {self.reference.OCI_STRICT_ENV: "1"}
        ), mock.patch.object(
            self.reference, "_oci_mirror_lock", return_value=lock
        ), mock.patch.object(
            self.reference.Path, "read_text", return_value=source
        ):
            with self.assertRaisesRegex(
                self.reference.ProofError, "uncontrolled OCI base image"
            ):
                self.reference.controlled_oci_dockerfile(Path("apps/api/Dockerfile"))

    def test_strict_oci_dockerfile_rejects_bom_prefixed_external_stage(self) -> None:
        lock = json.loads(
            (ROOT / "platform/oci-proof-mirror.lock.json").read_text(encoding="utf-8")
        )
        rust = lock["images"]["build_rust"]
        debian = lock["images"]["build_debian"]
        source = (
            "\ufeffFROM attacker.example/unreviewed:latest AS injected\n"
            f"FROM {rust['local_ref']}@{rust['digest']} AS builder\n"
            f"FROM {debian['local_ref']}@{debian['digest']}\n"
            "COPY --from=injected /payload /payload\n"
        )
        with mock.patch.dict(
            os.environ, {self.reference.OCI_STRICT_ENV: "1"}
        ), mock.patch.object(
            self.reference, "_oci_mirror_lock", return_value=lock
        ), mock.patch.object(
            self.reference.Path, "read_text", return_value=source
        ):
            with self.assertRaisesRegex(
                self.reference.ProofError, "uncontrolled OCI base image"
            ):
                self.reference.controlled_oci_dockerfile(Path("apps/api/Dockerfile"))

    def test_strict_oci_dockerfile_rejects_adjacent_external_sources(self) -> None:
        lock = json.loads(
            (ROOT / "platform/oci-proof-mirror.lock.json").read_text(encoding="utf-8")
        )
        rust = lock["images"]["build_rust"]
        debian = lock["images"]["build_debian"]
        controlled = (
            f"FROM {rust['local_ref']}@{rust['digest']} AS builder\n"
            f"FROM {debian['local_ref']}@{debian['digest']} AS runtime\n"
        )
        cases = {
            "continued-from": (
                controlled
                + "FROM\\\n attacker.example/unreviewed:latest AS injected\n"
            ),
            "external-frontend": (
                "# syntax=attacker.example/dockerfile:latest\n" + controlled
            ),
            "external-copy": (
                controlled
                + "COPY --from=attacker.example/payload:latest /payload /payload\n"
            ),
            "external-run-mount": (
                controlled
                + "RUN --mount=type=bind,from=attacker.example/payload:latest "
                + "cat /payload\n"
            ),
            "escaped-copy-flag": (
                controlled
                + r"COPY --fr\om=attacker.example/payload:latest /payload /payload"
                + "\n"
            ),
            "quoted-run-mount-source": (
                controlled
                + 'RUN --mount=type=bind,"from=attacker.example/payload:latest",'
                + "target=/payload cat /payload\n"
            ),
            "escaped-run-mount-flag": (
                controlled
                + r"RUN --mo\unt=type=bind,from=attacker.example/payload:latest,target=/payload cat /payload"
                + "\n"
            ),
            "escaped-run-mount-source-key": (
                controlled
                + r"RUN --mount=type=bind,fr\om=attacker.example/payload:latest,target=/payload cat /payload"
                + "\n"
            ),
            "even-trailing-escapes-before-external-from": (
                controlled
                + r"RUN true \\"
                + "\nFROM attacker.example/unreviewed:latest AS injected\n"
            ),
            "unicode-decimal-copy-stage": (
                controlled + "COPY --from=٠ /payload /payload\n"
            ),
            "deferred-onbuild": controlled + "ONBUILD COPY . /payload\n",
            "remote-add": controlled + "ADD https://attacker.example/payload /payload\n",
        }
        for label, source in cases.items():
            with self.subTest(label=label), mock.patch.dict(
                os.environ, {self.reference.OCI_STRICT_ENV: "1"}
            ), mock.patch.object(
                self.reference, "_oci_mirror_lock", return_value=lock
            ), mock.patch.object(
                self.reference.Path, "read_text", return_value=source
            ):
                with self.assertRaises(self.reference.ProofError):
                    self.reference.controlled_oci_dockerfile(
                        Path("apps/api/Dockerfile")
                    )

    def test_strict_oci_dockerfile_accepts_buildkit_stage_whitespace_and_case(
        self,
    ) -> None:
        lock = json.loads(
            (ROOT / "platform/oci-proof-mirror.lock.json").read_text(encoding="utf-8")
        )
        rust = lock["images"]["build_rust"]
        debian = lock["images"]["build_debian"]
        source = (
            f"FROM\v{rust['local_ref']}@{rust['digest']} AS Builder\n"
            f"FROM\f{debian['local_ref']}@{debian['digest']} AS Runtime\n"
            "COPY --from=builder /payload /payload\n"
            "COPY --from=0 /payload0 /payload0\n"
            "FROM builder AS exported-builder\n"
        )
        with mock.patch.dict(
            os.environ, {self.reference.OCI_STRICT_ENV: "1"}
        ), mock.patch.object(
            self.reference, "_oci_mirror_lock", return_value=lock
        ), mock.patch.object(
            self.reference.Path, "read_text", return_value=source
        ):
            rewritten = self.reference.controlled_oci_dockerfile(
                Path("apps/api/Dockerfile")
            )
        self.assertIn(f"FROM\v{rust['local_ref']} AS Builder", rewritten)
        self.assertIn(f"FROM\f{debian['local_ref']} AS Runtime", rewritten)
        self.assertIn("COPY --from=builder", rewritten)
        self.assertIn("COPY --from=0", rewritten)
        self.assertIn("FROM builder AS exported-builder", rewritten)

    def test_strict_oci_dockerfile_allows_prior_stage_alias(self) -> None:
        lock = json.loads(
            (ROOT / "platform/oci-proof-mirror.lock.json").read_text(encoding="utf-8")
        )
        rust = lock["images"]["build_rust"]
        debian = lock["images"]["build_debian"]
        source = (
            f"FROM {rust['local_ref']}@{rust['digest']} AS Builder\n"
            f"FROM {debian['local_ref']}@{debian['digest']} AS Runtime\n"
            "FROM builder AS exported-builder\n"
        )
        with mock.patch.dict(
            os.environ, {self.reference.OCI_STRICT_ENV: "1"}
        ), mock.patch.object(
            self.reference, "_oci_mirror_lock", return_value=lock
        ), mock.patch.object(
            self.reference.Path, "read_text", return_value=source
        ):
            rewritten = self.reference.controlled_oci_dockerfile(
                Path("apps/api/Dockerfile")
            )
        self.assertIn(f"FROM {rust['local_ref']} AS Builder", rewritten)
        self.assertIn(f"FROM {debian['local_ref']} AS Runtime", rewritten)
        self.assertIn("FROM builder AS exported-builder", rewritten)

    def test_strict_image_builds_consume_dockerfiles_from_stdin(self) -> None:
        with mock.patch.dict(os.environ, {self.reference.OCI_STRICT_ENV: "1"}), mock.patch.object(
            self.reference,
            "_build_dockerfile",
            side_effect=[
                (["--file", "-"], "FROM rust:local AS builder\n"),
                (["--file", "-"], "FROM node:local AS builder\n"),
            ],
        ), mock.patch.object(self.reference, "run") as run, mock.patch.object(
            self.reference,
            "output",
            return_value="sha256:" + "a" * 64,
        ):
            self.reference.build_images("kind", "proof", "b" * 40, "timestamp")
        api_build = run.call_args_list[0]
        web_build = run.call_args_list[1]
        self.assertEqual(api_build.args[0][:5], ["docker", "build", "--pull=false", "--file", "-"])
        self.assertEqual(web_build.args[0][:5], ["docker", "build", "--pull=false", "--file", "-"])
        self.assertEqual(api_build.kwargs["input_text"], "FROM rust:local AS builder\n")
        self.assertEqual(web_build.kwargs["input_text"], "FROM node:local AS builder\n")

    def test_non_strict_image_builds_allow_normal_pull_behavior(self) -> None:
        with mock.patch.dict(os.environ, {self.reference.OCI_STRICT_ENV: ""}), mock.patch.object(
            self.reference, "run"
        ) as run, mock.patch.object(
            self.reference, "output", return_value="sha256:" + "a" * 64
        ):
            self.reference.build_images("kind", "proof", "b" * 40, "timestamp")
        self.assertNotIn("--pull=false", run.call_args_list[0].args[0])
        self.assertNotIn("--pull=false", run.call_args_list[1].args[0])

    def test_cnpg_manifest_binds_operator_and_bootstrap_image_to_runtime_tag(
        self,
    ) -> None:
        with mock.patch.dict(sys.modules, {"kind_reference": self.reference}):
            ha_reference = load_module(
                "weltgewebe_ha_reference_contract",
                ROOT / "scripts/platform/ha_reference.py",
            )
        tagged = "ghcr.io/cloudnative-pg/cloudnative-pg:1.30.0"
        runtime_image = (
            "ghcr.io/cloudnative-pg/cloudnative-pg:weltgewebe-a2701eb97cdd"
        )
        source = yaml.safe_dump_all(
            [
                {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "metadata": {
                        "name": "cnpg-controller-manager",
                        "namespace": "cnpg-system",
                    },
                    "spec": {
                        "template": {
                            "spec": {
                                "containers": [
                                    {
                                        "name": "manager",
                                        "image": tagged,
                                        "env": [
                                            {
                                                "name": "OPERATOR_IMAGE_NAME",
                                                "value": tagged,
                                            }
                                        ],
                                    }
                                ]
                            }
                        }
                    },
                }
            ],
            sort_keys=False,
        )
        runtime_refs = {
            ha_reference.ref._normalize_oci_reference(
                ha_reference.CNPG_OPERATOR_IMAGE
            ): runtime_image,
            ha_reference.ref._normalize_oci_reference(runtime_image): runtime_image,
        }
        with mock.patch.dict(
            os.environ, {ha_reference.ref.OCI_STRICT_ENV: "1"}
        ), mock.patch.object(
            ha_reference.ref, "_CONTROLLED_OCI_RUNTIME_REFS", runtime_refs
        ):
            documents = list(
                yaml.safe_load_all(ha_reference.render_cnpg_manifest(source))
            )
        manager = documents[0]["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(manager["image"], runtime_image)
        self.assertEqual(manager["imagePullPolicy"], "Never")
        self.assertEqual(manager["env"][0]["value"], runtime_image)

    def test_strict_oci_policy_sets_cnpg_cluster_pull_policy(self) -> None:
        cluster = {
            "apiVersion": "postgresql.cnpg.io/v1",
            "kind": "Cluster",
            "spec": {"instances": 3},
        }
        with mock.patch.dict(os.environ, {self.reference.OCI_STRICT_ENV: "1"}):
            result = self.reference.enforce_controlled_oci_pull_policy([cluster])
        self.assertEqual(result[0]["spec"]["imagePullPolicy"], "Never")

    def test_strict_oci_policy_forces_never_for_every_pod_container(self) -> None:
        documents = [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "spec": {
                    "template": {
                        "spec": {
                            "initContainers": [{"name": "init", "image": "init:v1"}],
                            "containers": [{"name": "app", "image": "app:v1"}],
                        }
                    }
                },
            }
        ]
        with mock.patch.dict(os.environ, {self.reference.OCI_STRICT_ENV: "1"}):
            result = self.reference.enforce_controlled_oci_pull_policy(documents)
        pod = result[0]["spec"]["template"]["spec"]
        self.assertEqual(pod["initContainers"][0]["imagePullPolicy"], "Never")
        self.assertEqual(pod["containers"][0]["imagePullPolicy"], "Never")

    def test_kubernetes_proof_workflow_is_reusable_and_auditable(self) -> None:
        pr_workflow_path = ROOT / ".github/workflows/kubernetes-platform.yml"
        pr_workflow_text = pr_workflow_path.read_text()
        pr_workflow = yaml.safe_load(pr_workflow_text)
        workflow_path = ROOT / ".github/workflows/kubernetes-platform-proof.yml"
        workflow_text = workflow_path.read_text()
        workflow = yaml.safe_load(workflow_text)
        self.assertEqual(set(pr_workflow["on"]), {"pull_request"})
        self.assertEqual(
            set(pr_workflow["jobs"]),
            {"contract", "trivy-rendered-security"},
        )
        self.assertNotIn("packages: read", pr_workflow_text)
        self.assertNotIn("github.token", pr_workflow_text)
        self.assertNotIn("pull_request", workflow["on"])
        self.assertEqual(set(workflow["on"]), {"push", "workflow_dispatch"})
        self.assertFalse(workflow["concurrency"]["cancel-in-progress"])
        self.assertIn("github.sha", workflow["concurrency"]["group"])
        self.assertNotIn("github.event.pull_request", workflow_text)
        self.assertNotIn("oci-proof-cache", workflow_text)
        for job_name in (
            "contract",
            "trivy-rendered-security",
            "kind-gitops-proof",
            "kind-ha-recovery-proof",
        ):
            checkout = workflow["jobs"][job_name]["steps"][0]
            self.assertTrue(str(checkout["uses"]).startswith("actions/checkout@"))
            self.assertEqual(checkout["with"]["fetch-depth"], 0)
            self.assertIs(checkout["with"]["persist-credentials"], False)

        def named_steps(job_name: str) -> dict[str, dict]:
            return {
                step["name"]: step
                for step in workflow["jobs"][job_name]["steps"]
                if "name" in step
            }

        for job_name, suite, cleanup_name in (
            ("kind-gitops-proof", "kind-gitops", "Reconcile owned kind proof resources"),
            ("kind-ha-recovery-proof", "ha-recovery", "Reconcile owned HA proof resources"),
        ):
            steps = named_steps(job_name)
            self.assertEqual(workflow["jobs"][job_name]["needs"], "contract")
            self.assertEqual(
                workflow["jobs"][job_name]["env"]["WELTGEWEBE_PROOF_OCI_STRICT"],
                "1",
            )
            self.assertEqual(
                workflow["jobs"][job_name]["permissions"],
                {"contents": "read", "packages": "read"},
            )
            init_step = steps["Initialize isolated OCI mirror credentials"]
            self.assertIn("$RUNNER_TEMP", init_step["run"])
            self.assertIn("$GITHUB_ENV", init_step["run"])
            live_step = steps["Verify live OCI mirror package budget"]
            self.assertEqual(live_step["env"], {"GH_TOKEN": "${{ github.token }}"})
            self.assertIn("oci_proof_mirror.py", live_step["run"])
            self.assertIn("verify-live", live_step["run"])
            auth_step = steps["Authenticate controlled OCI mirror"]
            self.assertEqual(auth_step["env"], {"GH_TOKEN": "${{ github.token }}"})
            self.assertIn("docker login ghcr.io", auth_step["run"])
            load_step = next(
                step for name, step in steps.items() if name.startswith("Load controlled")
            )
            self.assertIn("oci_proof_mirror.py", load_step["run"])
            self.assertIn("load-host", load_step["run"])
            self.assertIn(f"--suite {suite}", load_step["run"])
            self.assertIn("--suite app-build", load_step["run"])
            logout_step = steps["Remove OCI mirror credentials"]
            self.assertEqual(
                logout_step["if"],
                "always() && steps.proof-cache.outputs.cache-hit != 'true'",
            )
            self.assertIn("docker logout ghcr.io", logout_step["run"])
            self.assertIn('[[ -n "${DOCKER_CONFIG:-}" ]]', logout_step["run"])
            self.assertLess(
                logout_step["run"].index('[[ -n "${DOCKER_CONFIG:-}" ]]'),
                logout_step["run"].index("docker logout ghcr.io"),
            )
            self.assertIn("install -d -m 0700", init_step["run"])
            self.assertIn("install -d -m 0700", logout_step["run"])
            block_step = steps["Block all OCI registries after mirror load"]
            for registry in ("registry-1.docker.io", "quay.io", "ghcr.io"):
                self.assertIn(registry, block_step["run"])
            self.assertIn('echo "::1 $host"', block_step["run"])
            self.assertIn("getent ahostsv4", block_step["run"])
            self.assertIn("getent ahostsv6", block_step["run"])
            self.assertIn("cp -- /etc/hosts", block_step["run"])
            self.assertIn("sha256sum", block_step["run"])
            restore_hosts = steps["Restore OCI registry host resolution"]
            self.assertEqual(
                restore_hosts["if"],
                "always() && steps.proof-cache.outputs.cache-hit != 'true'",
            )
            self.assertIn("sudo tee /etc/hosts", restore_hosts["run"])
            self.assertIn('test "$observed" = "$expected"', restore_hosts["run"])
            offline_step = steps["Verify loaded OCI inputs offline"]
            self.assertIn("verify-host", offline_step["run"])
            self.assertIn(f"--suite {suite}", offline_step["run"])
            self.assertIn("--suite app-build", offline_step["run"])
            compute = next(step for name, step in steps.items() if name.startswith("Compute immutable"))
            restore = next(step for name, step in steps.items() if name.startswith("Restore immutable"))
            validate = next(step for name, step in steps.items() if name.startswith("Validate restored"))
            cleanup = steps[cleanup_name]
            self.assertIn(f"--suite {suite}", compute["run"])
            self.assertIn('--source-commit "$PROOF_SOURCE_COMMIT"', compute["run"])
            self.assertTrue(str(restore["uses"]).startswith("actions/cache@"))
            self.assertIn("steps.proof-identity.outputs.identity", restore["with"]["key"] )
            self.assertEqual(validate["if"], "steps.proof-cache.outputs.cache-hit == 'true'")
            self.assertIn("proof_identity.py validate", validate["run"])
            self.assertEqual(
                cleanup["if"],
                "always() && steps.proof-cache.outputs.cache-hit != 'true'",
            )
            self.assertIn("--receipt build/kubernetes-platform/", cleanup["run"])

        gitops = named_steps("kind-gitops-proof")
        self.assertEqual(
            gitops["Stage failure diagnostics"]["if"],
            "failure() && steps.proof-cache.outputs.cache-hit != 'true'",
        )
        self.assertEqual(
            gitops["Collect failure diagnostics"]["with"]["path"],
            "build/kubernetes-platform/failures/",
        )

        ha = named_steps("kind-ha-recovery-proof")
        stage_receipt = ha["Stage HA recovery receipt"]
        self.assertEqual(stage_receipt["if"], "success()")
        self.assertIn(
            "build/kubernetes-platform/reuse/ha-recovery/proof.json",
            stage_receipt["run"],
        )
        self.assertNotIn("ha-recovery-oci-mirror", stage_receipt["run"])
        upload_receipt = ha["Upload HA recovery receipt"]
        self.assertEqual(
            upload_receipt["if"],
            "success() && steps.proof-cache.outputs.cache-hit != 'true'",
        )
        self.assertIn("ha-recovery-identity.json", upload_receipt["with"]["path"])
        self.assertIn(
            "build/kubernetes-platform/ha-recovery-oci-mirror/*.json",
            upload_receipt["with"]["path"],
        )
        restored_ha = ha["Upload restored HA recovery receipt"]
        self.assertEqual(
            restored_ha["if"],
            "success() && steps.proof-cache.outputs.cache-hit == 'true'",
        )
        self.assertNotIn("ha-recovery-oci-mirror", restored_ha["with"]["path"])

        direct_upload = gitops["Upload GitOps proof evidence"]
        self.assertEqual(
            direct_upload["if"],
            "success() && steps.proof-cache.outputs.cache-hit != 'true'",
        )
        restored_direct = gitops["Upload restored GitOps proof evidence"]
        self.assertEqual(
            restored_direct["if"],
            "success() && steps.proof-cache.outputs.cache-hit == 'true'",
        )
        self.assertNotIn("kind-gitops-oci-mirror", restored_direct["with"]["path"])

        for job_name in ("kind-gitops-proof", "kind-ha-recovery-proof"):
            for step in workflow["jobs"][job_name]["steps"]:
                if str(step.get("uses", "")).startswith("actions/upload-artifact@"):
                    self.assertNotIn(".cache/", str((step.get("with") or {}).get("path", "")))

    def test_privileged_ci_proofs_are_isolated_from_pull_requests(self) -> None:
        pr_path = ROOT / ".github/workflows/kubernetes-platform.yml"
        proof_path = ROOT / ".github/workflows/kubernetes-platform-proof.yml"
        pr_text = pr_path.read_text()
        proof_text = proof_path.read_text()
        pr_workflow = yaml.safe_load(pr_text)
        proof_workflow = yaml.safe_load(proof_text)

        self.assertEqual(set(pr_workflow["on"]), {"pull_request"})
        self.assertNotIn("kind-gitops-proof", pr_workflow["jobs"])
        self.assertNotIn("kind-ha-recovery-proof", pr_workflow["jobs"])
        self.assertNotIn("packages: read", pr_text)
        self.assertNotIn("github.token", pr_text)
        self.assertNotIn("pull_request", proof_workflow["on"])
        self.assertEqual(set(proof_workflow["on"]), {"push", "workflow_dispatch"})
        expected_head = proof_workflow["on"]["workflow_dispatch"]["inputs"]["expected_head"]
        self.assertIs(expected_head["required"], True)
        self.assertEqual(expected_head["type"], "string")
        guard = proof_workflow["jobs"]["dispatch-head-contract"]
        self.assertEqual(guard["timeout-minutes"], 2)
        self.assertEqual(proof_workflow["jobs"]["contract"]["needs"], "dispatch-head-contract")
        guard_step = guard["steps"][0]
        self.assertEqual(guard_step["env"]["EVENT_NAME"], "${{ github.event_name }}")
        self.assertEqual(guard_step["env"]["EXPECTED_HEAD"], "${{ inputs.expected_head }}")
        self.assertEqual(guard_step["env"]["CHECKED_OUT_HEAD"], "${{ github.sha }}")
        self.assertEqual(guard_step["env"]["CHECKED_OUT_REF"], "${{ github.ref }}")
        self.assertIn('[[ "$EXPECTED_HEAD" =~ ^[0-9a-f]{40}$ ]]', guard_step["run"])
        self.assertIn('test "$CHECKED_OUT_REF" = "refs/heads/main"', guard_step["run"])
        self.assertIn('test "$CHECKED_OUT_HEAD" = "$EXPECTED_HEAD"', guard_step["run"])

        for job_name in ("kind-gitops-proof", "kind-ha-recovery-proof"):
            job = proof_workflow["jobs"][job_name]
            self.assertEqual(
                job["if"],
                "github.ref == 'refs/heads/main' && (github.event_name == 'push' || github.sha == inputs.expected_head)",
            )
            self.assertEqual(
                job["permissions"], {"contents": "read", "packages": "read"}
            )
            self.assertEqual(job["env"]["PROOF_SOURCE_COMMIT"], "${{ github.sha }}")

        steps = proof_workflow["jobs"]["kind-gitops-proof"]["steps"]
        named_steps = {step["name"]: step for step in steps if "name" in step}
        self.assertNotIn("Run pull-request direct reference proof", named_steps)
        gitops = named_steps["Run commit-bound GitOps reference proof"]
        self.assertEqual(
            gitops["if"], "steps.proof-cache.outputs.cache-hit != 'true'"
        )
        self.assertEqual(
            shlex.split(gitops["run"]),
            [
                "uv",
                "run",
                "--project",
                "tools/py",
                "--locked",
                "python",
                "scripts/platform/kind_reference.py",
                "proof",
                "--cluster",
                "$CLUSTER_NAME",
                "--mode",
                "gitops",
                "--source-commit",
                "$GITHUB_SHA",
                "--owner-id",
                "$PROOF_OWNER_ID",
            ],
        )
        self.assertNotIn("github.event.pull_request", proof_text)
        self.assertNotIn("SOURCE_REF:", proof_text)
        self.assertNotIn("github.head_ref || github.ref_name", proof_text)

    def test_platform_proof_jobs_share_locked_uv_tooling(self) -> None:
        """All Python-executing jobs must share tools/py lock identity (not pip)."""
        workflow_path = ROOT / ".github/workflows/kubernetes-platform-proof.yml"
        workflow_text = workflow_path.read_text(encoding="utf-8")
        workflow = yaml.safe_load(workflow_text)
        self.assertNotIn('python-version: "3.10"', workflow_text)
        self.assertNotRegex(
            workflow_text,
            r"pip install .*PyYAML|pip install .*pyyaml|pip install .*pytest",
        )
        self.assertNotIn("PyYAML==6.0.3", workflow_text)
        python_jobs = (
            "contract",
            "trivy-rendered-security",
            "kind-gitops-proof",
            "kind-ha-recovery-proof",
        )
        for job_name in python_jobs:
            job = workflow["jobs"][job_name]
            steps = job["steps"]
            setup_python = next(
                step
                for step in steps
                if str(step.get("uses", "")).startswith("actions/setup-python@")
            )
            self.assertEqual(
                setup_python["with"]["python-version-file"],
                ".python-version",
                job_name,
            )
            self.assertNotIn("python-version", setup_python.get("with", {}))
            setup_uv = next(
                step
                for step in steps
                if str(step.get("uses", "")).startswith("astral-sh/setup-uv@")
            )
            self.assertEqual(
                setup_uv["with"]["version"],
                "${{ steps.uv-version.outputs.uv_version }}",
                job_name,
            )
            sync_step = next(
                step
                for step in steps
                if step.get("run") == "uv sync --project tools/py --locked"
            )
            self.assertEqual(
                sync_step["name"], "Sync hash-locked Python tooling", job_name
            )
            for step in steps:
                run = step.get("run")
                if not isinstance(run, str):
                    continue
                if "scripts/platform/" in run or "scripts/security/" in run:
                    self.assertIn(
                        "uv run --project tools/py --locked python",
                        run,
                        f"{job_name}: {step.get('name', run[:40])}",
                    )
                    self.assertNotRegex(
                        run,
                        r"(?m)^[ \t]*python[ \t]+scripts/",
                        f"{job_name}: bare python remains",
                    )

    def test_proof_identity_covers_all_api_image_inputs(self) -> None:
        for suite in ("kind-gitops", "ha-recovery"):
            selectors = set(self.proof_identity.SUITE_INPUTS[suite])
            self.assertIn("configs/", selectors)
            self.assertIn("scripts/ops/", selectors)
            self.assertIn("apps/api/", selectors)
            self.assertIn("scripts/dev/", selectors)

    def test_proof_identity_ignores_unrelated_inputs_and_rejects_tampering(self) -> None:
        commit = "0123456789abcdef0123456789abcdef01234567"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "platform").mkdir()
            (root / "scripts/platform").mkdir(parents=True)
            (root / "docs").mkdir()
            (root / "platform/toolchain.lock.json").write_text("{}\n")
            (root / "platform/oci-proof-mirror.lock.json").write_text("{}\n")
            (root / "scripts/platform/proof_identity.py").write_text("helper-v1\n")
            (root / "docs/unrelated.md").write_text("one\n")
            tracked = (
                "platform/toolchain.lock.json",
                "platform/oci-proof-mirror.lock.json",
                "scripts/platform/proof_identity.py",
                "docs/unrelated.md",
            )
            with mock.patch.object(self.proof_identity, "ROOT", root), mock.patch.object(
                self.proof_identity, "_tracked_files", return_value=tracked
            ), mock.patch.dict(
                self.proof_identity.SUITE_INPUTS,
                {"kind-gitops": ("platform/", "scripts/platform/")},
                clear=True,
            ):
                first = self.proof_identity.compute_identity("kind-gitops", commit)
                (root / "docs/unrelated.md").write_text("two\n")
                second = self.proof_identity.compute_identity("kind-gitops", commit)
                self.assertEqual(first["identity_sha256"], second["identity_sha256"])
                (root / "scripts/platform/proof_identity.py").write_text("helper-v2\n")
                third = self.proof_identity.compute_identity("kind-gitops", commit)
                self.assertNotEqual(first["identity_sha256"], third["identity_sha256"])

                identity_path = root / "identity.json"
                identity_path.write_bytes(self.proof_identity._canonical_json(third))
                proof_path = root / "source-proof.json"
                proof_path.write_text(json.dumps({
                    "status": "pass",
                    "tool_lock_sha256": third["tool_lock_sha256"],
                    "commit": commit,
                    "source_commit": commit,
                    "production_changed": False,
                }))
                reuse = root / "reuse"
                with mock.patch.object(
                    self.proof_identity, "_checkout_commit", return_value=commit
                ), mock.patch.object(
                    self.proof_identity, "_validate_controlled_oci_proof"
                ):
                    self.proof_identity.record(identity_path, proof_path, reuse)
                    self.proof_identity.validate(
                        identity_path, reuse / "record.json", reuse / "proof.json"
                    )
                    with (reuse / "proof.json").open("a") as handle:
                        handle.write(" ")
                    with self.assertRaises(self.proof_identity.IdentityError):
                        self.proof_identity.validate(
                            identity_path, reuse / "record.json", reuse / "proof.json"
                        )

    def test_proof_validate_rejects_crafted_policy_and_record_fields(self) -> None:
        identity = {
            "schema_version": 1,
            "suite": "kind-gitops",
            "source_commit": "1" * 40,
            "input_manifest_sha256": "2" * 64,
            "tool_lock_sha256": "3" * 64,
            "invalidation_contract": [],
            "identity_sha256": "4" * 64,
        }
        proof = {
            "status": "pass",
            "tool_lock_sha256": "3" * 64,
            "commit": "5" * 40,
            "source_commit": "1" * 40,
            "production_changed": False,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity_path = root / "identity.json"
            source_proof_path = root / "source-proof.json"
            reuse = root / "reuse"
            identity_path.write_text(json.dumps(identity))
            source_proof_path.write_text(json.dumps(proof))
            with mock.patch.object(
                self.proof_identity, "compute_identity", return_value=identity
            ), mock.patch.object(
                self.proof_identity, "_checkout_commit", return_value="5" * 40
            ), mock.patch.object(
                self.proof_identity, "_validate_controlled_oci_proof"
            ):
                self.proof_identity.record(identity_path, source_proof_path, reuse)

                proof_path = reuse / "proof.json"
                record_path = reuse / "record.json"
                crafted_proof = json.loads(proof_path.read_text())
                crafted_proof["production_changed"] = None
                proof_bytes = self.proof_identity._canonical_json(crafted_proof)
                proof_path.write_bytes(proof_bytes)
                crafted_record = json.loads(record_path.read_text())
                crafted_record["proof_receipt_sha256"] = hashlib.sha256(proof_bytes).hexdigest()
                crafted_record["production_changed"] = None
                record_path.write_bytes(self.proof_identity._canonical_json(crafted_record))
                with self.assertRaisesRegex(
                    self.proof_identity.IdentityError, "production_changed=false"
                ):
                    self.proof_identity.validate(identity_path, record_path, proof_path)

                self.proof_identity.record(identity_path, source_proof_path, reuse)
                drifted_record = json.loads(record_path.read_text())
                drifted_record["proof_commit"] = "6" * 40
                record_path.write_bytes(self.proof_identity._canonical_json(drifted_record))
                with self.assertRaisesRegex(
                    self.proof_identity.IdentityError, "record commit"
                ):
                    self.proof_identity.validate(identity_path, record_path, proof_path)

    def test_proof_oci_receipts_are_semantically_bound(self) -> None:
        digest = "sha256:" + "a" * 64
        image_id = "sha256:" + "b" * 64
        platform_digest = "sha256:" + "c" * 64
        lock = {
            "images": {
                "runtime": {
                    "canonical": f"registry.example/runtime@{digest}",
                    "local_ref": "registry.example/runtime:local",
                    "digest": digest,
                    "suites": ["kind-gitops"],
                    "load_into_kind": True,
                },
                "builder": {
                    "canonical": f"registry.example/builder@{digest}",
                    "local_ref": "registry.example/builder:local",
                    "digest": digest,
                    "suites": ["app-build"],
                    "load_into_kind": False,
                },
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_path = root / "platform/oci-proof-mirror.lock.json"
            lock_path.parent.mkdir(parents=True)
            lock_bytes = (json.dumps(lock, sort_keys=True) + "\n").encode()
            lock_path.write_bytes(lock_bytes)
            lock_sha256 = hashlib.sha256(lock_bytes).hexdigest()
            identity = {
                "suite": "kind-gitops",
                "oci_mirror_lock_sha256": lock_sha256,
            }
            host_images = {
                name: {"source": "local-verified", "image_id": image_id}
                for name in lock["images"]
            }
            blockade = {
                "node": "proof-control-plane",
                "registries": {
                    registry: {"ipv4": ["127.0.0.1"], "ipv6": ["::1"]}
                    for registry in self.proof_identity.BLOCKED_REGISTRIES
                },
            }
            runtime_ref = lock["images"]["runtime"]["local_ref"]
            cluster_image = {
                "canonical": lock["images"]["runtime"]["canonical"],
                "local_ref": runtime_ref,
                "digest_ref": f"{runtime_ref}@{digest}",
                "runtime_ref": runtime_ref,
                "locked_index_digest": digest,
                "image_id": image_id,
                "cri_image_id": image_id,
                "platform_target_digest": platform_digest,
                "nodes": [
                    {
                        "node": "proof-control-plane",
                        "runtime_ref": runtime_ref,
                        "image_id": image_id,
                        "repo_tags": [runtime_ref],
                        "repo_digests": [],
                        "selected_repo_digest": None,
                        "containerd_target_digest": platform_digest,
                        "platform_evidence_kind": "containerd_target_digest",
                        "locked_index_digest": digest,
                        "platform_target_digest": platform_digest,
                        "cri_image_status_verified": True,
                    }
                ],
            }
            proof = {
                "cluster": "proof",
                "oci_controlled_source": {
                    "strict": True,
                    "host": {
                        "status": "pass",
                        "strict": True,
                        "lock_sha256": lock_sha256,
                        "selected_count": 2,
                        "images": host_images,
                        "failures": {},
                    },
                    "cluster": {
                        "status": "pass",
                        "strict": True,
                        "lock_sha256": lock_sha256,
                        "cluster": "proof",
                        "loaded_count": 1,
                        "images": {"runtime": cluster_image},
                        "registry_blockades": [blockade],
                    },
                },
            }
            with mock.patch.object(self.proof_identity, "ROOT", root):
                self.proof_identity._validate_controlled_oci_proof(identity, proof)
                drifted = json.loads(json.dumps(proof))
                drifted["oci_controlled_source"]["cluster"]["registry_blockades"] = []
                with self.assertRaisesRegex(
                    self.proof_identity.IdentityError, "registry blockade evidence"
                ):
                    self.proof_identity._validate_controlled_oci_proof(identity, drifted)
                drifted = json.loads(json.dumps(proof))
                drifted["oci_controlled_source"]["host"]["lock_sha256"] = "d" * 64
                with self.assertRaisesRegex(
                    self.proof_identity.IdentityError, "different mirror lock"
                ):
                    self.proof_identity._validate_controlled_oci_proof(identity, drifted)
                drifted = json.loads(json.dumps(proof))
                drifted["oci_controlled_source"]["host"]["images"]["runtime"]["image_id"] = "sha256:" + "d" * 64
                with self.assertRaisesRegex(
                    self.proof_identity.IdentityError, "OCI image binding is invalid"
                ):
                    self.proof_identity._validate_controlled_oci_proof(identity, drifted)
                drifted = json.loads(json.dumps(proof))
                drifted["oci_controlled_source"]["cluster"]["images"]["runtime"]["nodes"] = []
                with self.assertRaisesRegex(
                    self.proof_identity.IdentityError, "node image bindings are missing"
                ):
                    self.proof_identity._validate_controlled_oci_proof(identity, drifted)
                drifted = json.loads(json.dumps(proof))
                drifted["oci_controlled_source"]["cluster"]["registry_blockades"][0]["node"] = "unrelated-node"
                with self.assertRaisesRegex(
                    self.proof_identity.IdentityError, "image and blockade nodes disagree"
                ):
                    self.proof_identity._validate_controlled_oci_proof(identity, drifted)
                drifted = json.loads(json.dumps(proof))
                drifted["oci_controlled_source"]["strict"] = False
                with self.assertRaisesRegex(
                    self.proof_identity.IdentityError, "strict controlled OCI"
                ):
                    self.proof_identity._validate_controlled_oci_proof(identity, drifted)

    def test_ha_cached_proof_allows_cluster_local_platform_digests(self) -> None:
        locked = "sha256:" + "a" * 64
        image_id = "sha256:" + "b" * 64
        lock = {
            "images": {
                "runtime": {
                    "canonical": f"registry.example/runtime@{locked}",
                    "local_ref": "registry.example/runtime:local",
                    "digest": locked,
                    "suites": ["ha-recovery"],
                    "load_into_kind": True,
                }
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_path = root / "platform/oci-proof-mirror.lock.json"
            lock_path.parent.mkdir(parents=True)
            lock_bytes = (json.dumps(lock, sort_keys=True) + "\n").encode()
            lock_path.write_bytes(lock_bytes)
            lock_sha256 = hashlib.sha256(lock_bytes).hexdigest()
            runtime_ref = lock["images"]["runtime"]["local_ref"]

            def receipt(cluster: str, platform: str) -> dict[str, object]:
                node = f"{cluster}-control-plane"
                image = {
                    "canonical": lock["images"]["runtime"]["canonical"],
                    "local_ref": runtime_ref,
                    "digest_ref": f"{runtime_ref}@{locked}",
                    "runtime_ref": runtime_ref,
                    "locked_index_digest": locked,
                    "image_id": image_id,
                    "cri_image_id": image_id,
                    "platform_target_digest": platform,
                    "nodes": [{
                        "node": node,
                        "runtime_ref": runtime_ref,
                        "image_id": image_id,
                        "repo_tags": [runtime_ref],
                        "repo_digests": [],
                        "selected_repo_digest": None,
                        "containerd_target_digest": platform,
                        "platform_evidence_kind": "containerd_target_digest",
                        "locked_index_digest": locked,
                        "platform_target_digest": platform,
                        "cri_image_status_verified": True,
                    }],
                }
                blockade = {
                    "node": node,
                    "registries": {
                        registry: {"ipv4": ["127.0.0.1"], "ipv6": ["::1"]}
                        for registry in self.proof_identity.BLOCKED_REGISTRIES
                    },
                }
                return {
                    "status": "pass",
                    "strict": True,
                    "lock_sha256": lock_sha256,
                    "cluster": cluster,
                    "loaded_count": 1,
                    "images": {"runtime": image},
                    "registry_blockades": [blockade],
                }

            proof = {
                "primary_cluster": "primary",
                "restore_cluster": "restore",
                "oci_controlled_source": {
                    "strict": True,
                    "host": {
                        "status": "pass",
                        "strict": True,
                        "lock_sha256": lock_sha256,
                        "selected_count": 1,
                        "images": {
                            "runtime": {
                                "source": "local-verified",
                                "image_id": image_id,
                            }
                        },
                        "failures": {},
                    },
                    "primary_cluster": receipt("primary", "sha256:" + "c" * 64),
                    "restore_cluster": receipt("restore", "sha256:" + "d" * 64),
                },
            }
            identity = {
                "suite": "ha-recovery",
                "oci_mirror_lock_sha256": lock_sha256,
            }
            with mock.patch.object(self.proof_identity, "ROOT", root):
                self.proof_identity._validate_controlled_oci_proof(identity, proof)

                duplicate_cluster = json.loads(json.dumps(proof))
                duplicate_cluster["restore_cluster"] = "primary"
                duplicate_cluster["oci_controlled_source"]["restore_cluster"][
                    "cluster"
                ] = "primary"
                with self.assertRaisesRegex(
                    self.proof_identity.IdentityError, "cluster bindings must be distinct"
                ):
                    self.proof_identity._validate_controlled_oci_proof(
                        identity, duplicate_cluster
                    )

                foreign_node = json.loads(json.dumps(proof))
                primary_receipt = foreign_node["oci_controlled_source"][
                    "primary_cluster"
                ]
                primary_receipt["images"]["runtime"]["nodes"][0][
                    "node"
                ] = "foreign-control-plane"
                primary_receipt["registry_blockades"][0][
                    "node"
                ] = "foreign-control-plane"
                with self.assertRaisesRegex(
                    self.proof_identity.IdentityError, "node cluster binding drifted"
                ):
                    self.proof_identity._validate_controlled_oci_proof(
                        identity, foreign_node
                    )

                overlapping_nodes = json.loads(json.dumps(proof))
                overlapping_nodes["restore_cluster"] = "primary-restore"
                restore_receipt = overlapping_nodes["oci_controlled_source"][
                    "restore_cluster"
                ]
                restore_receipt["cluster"] = "primary-restore"
                shared_node = "primary-restore-control-plane"
                for field in ("primary_cluster", "restore_cluster"):
                    receipt_value = overlapping_nodes["oci_controlled_source"][field]
                    receipt_value["images"]["runtime"]["nodes"][0][
                        "node"
                    ] = shared_node
                    receipt_value["registry_blockades"][0]["node"] = shared_node
                with self.assertRaisesRegex(
                    self.proof_identity.IdentityError, "node inventories overlap"
                ):
                    self.proof_identity._validate_controlled_oci_proof(
                        identity, overlapping_nodes
                    )

    def test_proof_record_rejects_receipt_from_different_checkout(self) -> None:
        identity = {
            "schema_version": 1,
            "suite": "kind-gitops",
            "source_commit": "1" * 40,
            "input_manifest_sha256": "2" * 64,
            "tool_lock_sha256": "3" * 64,
            "invalidation_contract": [],
            "identity_sha256": "4" * 64,
        }
        proof = {
            "status": "pass",
            "tool_lock_sha256": "3" * 64,
            "commit": "5" * 40,
            "production_changed": False,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity_path = root / "identity.json"
            proof_path = root / "proof.json"
            identity_path.write_text(json.dumps(identity))
            proof_path.write_text(json.dumps(proof))
            with mock.patch.object(
                self.proof_identity, "compute_identity", return_value=identity
            ), mock.patch.object(
                self.proof_identity, "_checkout_commit", return_value="6" * 40
            ):
                with self.assertRaisesRegex(
                    self.proof_identity.IdentityError, "current checkout commit"
                ):
                    self.proof_identity.record(identity_path, proof_path, root / "reuse")

    def test_proof_record_requires_explicit_no_production_change(self) -> None:
        identity = {
            "schema_version": 1,
            "suite": "ha-recovery",
            "source_commit": "1" * 40,
            "input_manifest_sha256": "2" * 64,
            "tool_lock_sha256": "3" * 64,
            "invalidation_contract": [],
            "identity_sha256": "4" * 64,
        }
        proof = {
            "status": "pass",
            "tool_lock_sha256": "3" * 64,
            "commit": "5" * 40,
            "production_changed": None,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity_path = root / "identity.json"
            proof_path = root / "proof.json"
            identity_path.write_text(json.dumps(identity))
            proof_path.write_text(json.dumps(proof))
            with mock.patch.object(
                self.proof_identity, "compute_identity", return_value=identity
            ), mock.patch.object(
                self.proof_identity, "_checkout_commit", return_value="5" * 40
            ):
                with self.assertRaisesRegex(
                    self.proof_identity.IdentityError, "production_changed=false"
                ):
                    self.proof_identity.record(identity_path, proof_path, root / "reuse")

    def test_cli_parser_accepts_exact_source_commit(self) -> None:
        commit = "0123456789abcdef0123456789abcdef01234567"
        args = self.reference.argument_parser().parse_args(
            [
                "proof",
                "--cluster",
                "reference",
                "--mode",
                "gitops",
                "--source-commit",
                commit,
            ]
        )
        self.assertEqual(args.command, "proof")
        self.assertEqual(args.source_commit, commit)
        self.assertIsNone(args.source_ref)
        with self.assertRaises(SystemExit):
            self.reference.argument_parser().parse_args(
                [
                    "proof",
                    "--mode",
                    "gitops",
                    "--source-ref",
                    "main",
                    "--source-commit",
                    commit,
                ]
            )

    def test_source_binding_fails_before_cluster_work_for_invalid_modes(self) -> None:
        commit = "0123456789abcdef0123456789abcdef01234567"
        self.reference.validate_source_binding(
            "direct", source_ref=None, source_commit=None
        )
        self.reference.validate_source_binding(
            "gitops", source_ref=None, source_commit=commit
        )
        self.reference.validate_source_binding(
            "gitops", source_ref="main", source_commit=None
        )
        invalid = (
            ("unsupported", None, commit, "unsupported proof mode"),
            ("direct", "main", None, "invalid for direct"),
            ("direct", None, commit, "invalid for direct"),
            ("gitops", None, None, "exactly one"),
            ("gitops", "main", commit, "exactly one"),
            ("gitops", None, "abc", "full lowercase"),
            ("gitops", None, "A" * 40, "full lowercase"),
        )
        for mode, source_ref, source_commit, message in invalid:
            with self.subTest(mode=mode, source_ref=source_ref), self.assertRaisesRegex(
                self.reference.ProofError, message
            ):
                self.reference.validate_source_binding(
                    mode, source_ref=source_ref, source_commit=source_commit
                )

    def test_commit_source_must_match_local_workspace_head(self) -> None:
        commit = "0123456789abcdef0123456789abcdef01234567"
        self.reference.validate_workspace_binding(
            source_commit=None, workspace_commit=commit
        )
        self.reference.validate_workspace_binding(
            source_commit=commit, workspace_commit=commit
        )
        with self.assertRaisesRegex(
            self.reference.ProofError, "exact local workspace HEAD"
        ):
            self.reference.validate_workspace_binding(
                source_commit="fedcba9876543210fedcba9876543210fedcba98",
                workspace_commit=commit,
            )

    def test_flux_source_can_bind_exact_commit_or_explicit_branch(self) -> None:
        commit = "0123456789abcdef0123456789abcdef01234567"
        by_commit = self.reference.flux_source_document(commit=commit)
        by_branch = self.reference.flux_source_document(branch="main")
        self.assertEqual(by_commit["spec"]["ref"], {"commit": commit})
        self.assertEqual(by_branch["spec"]["ref"], {"branch": "main"})
        with self.assertRaisesRegex(self.reference.ProofError, "exactly one"):
            self.reference.flux_source_document()
        with self.assertRaisesRegex(self.reference.ProofError, "exactly one"):
            self.reference.flux_source_document(branch="main", commit=commit)

    def test_nonlocal_overlays_reject_local_fixture_markers(self) -> None:
        production = "platform/apps/weltgewebe/overlays/production"
        for marker in self.validator.LOCAL_FIXTURE_SENTINELS:
            with self.subTest(marker=marker), self.assertRaisesRegex(
                self.validator.ContractError, "local-only fixture"
            ):
                self.validator._assert_nonlocal_overlay_fixture_boundary(
                    production, marker
                )
        self.validator._assert_nonlocal_overlay_fixture_boundary(
            "platform/apps/weltgewebe/overlays/local",
            "\n".join(self.validator.LOCAL_FIXTURE_SENTINELS),
        )
        self.validator._assert_nonlocal_overlay_fixture_boundary(
            production, "WELTGEWEBE_API_MIGRATION_ONLY"
        )

    def test_toolchain_is_hash_bound(self) -> None:
        lock = json.loads((ROOT / "platform/toolchain.lock.json").read_text())
        self.assertEqual(lock["schema_version"], 1)
        for section in (lock["tools"], lock["artifacts"]):
            for name, entry in section.items():
                self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$", name)
        self.assertIn("@sha256:", lock["kubernetes"]["kind_node_image"])

    def test_gateway_api_pin_matches_cilium_required_crds(self) -> None:
        lock = json.loads((ROOT / "platform/toolchain.lock.json").read_text())
        expected = {
            "gateway_api_gatewayclasses": ("GatewayClass", "7d958ad3965ea4a4996616846d9ecaf1fededbf9351b9752b8b85973936ae8c8"),
            "gateway_api_gateways": ("Gateway", "a02ea425fc901f197b668c9ddd56375e1f6896994914c6e6b9b4fdb85cf3ba6e"),
            "gateway_api_httproutes": ("HTTPRoute", "98c6777c22309d319292e9c288ee632006c9ffdd4272383d6f9dffa3fbccaf14"),
            "gateway_api_referencegrants": ("ReferenceGrant", "d74fc2f8e90094f4c4d6dc13a2d720011a40e30e5640b3e2a2051fac820f6584"),
            "gateway_api_grpcroutes": ("GRPCRoute", "b72068b42cb32051ca609e5a8dfefb16904d164abe2e71d9f3c776fae41c4dab"),
        }
        gateway_entries = {
            name: entry
            for name, entry in lock["artifacts"].items()
            if name.startswith("gateway_api_")
        }
        self.assertEqual(set(gateway_entries), set(expected))
        for name, (kind, sha256) in expected.items():
            entry = gateway_entries[name]
            self.assertEqual(entry["version"], "1.4.1")
            self.assertEqual(entry["required_crd_kind"], kind)
            self.assertEqual(entry["sha256"], sha256)
        self.assertNotIn("TLSRoute", json.dumps(gateway_entries))
        self.assertEqual(
            self.reference.GATEWAY_API_ARTIFACTS, tuple(expected)
        )

    def test_tool_install_is_atomic_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "downloaded-kind"
            destination = root / "bin" / "kind"
            source.write_bytes(b"new-kind-binary")
            destination.parent.mkdir()
            destination.write_bytes(b"old-kind-binary")
            real_copy2 = self.bootstrap.shutil.copy2
            real_replace = os.replace
            copy_targets: list[Path] = []

            def guarded_copy(source_path, destination_path):
                target = Path(destination_path)
                self.assertNotEqual(target, destination)
                copy_targets.append(target)
                return real_copy2(source_path, destination_path)

            with mock.patch.object(
                self.bootstrap.shutil, "copy2", side_effect=guarded_copy
            ), mock.patch.object(
                self.bootstrap.os, "replace", wraps=real_replace
            ) as replace:
                self.bootstrap._install_executable(source, destination)

            self.assertEqual(destination.read_bytes(), source.read_bytes())
            self.assertTrue(destination.stat().st_mode & 0o110)
            self.assertEqual(len(copy_targets), 1)
            replace.assert_called_once_with(copy_targets[0], destination)

            with mock.patch.object(self.bootstrap.shutil, "copy2") as copy:
                self.bootstrap._install_executable(source, destination)
            copy.assert_not_called()

    def test_gateway_artifact_contract_rejects_extra_or_wrong_crds(self) -> None:
        spec = {"required_crd_kind": "HTTPRoute"}
        compatible = """apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
spec:
  names:
    kind: HTTPRoute
"""
        wrong = compatible.replace("HTTPRoute", "TLSRoute")
        extra = compatible + "---\n" + wrong
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "gateway.yaml"
            artifact.write_text(compatible)
            self.bootstrap._assert_artifact_contract("http", artifact, spec)
            artifact.write_text(wrong)
            with self.assertRaisesRegex(RuntimeError, "HTTPRoute"):
                self.bootstrap._assert_artifact_contract("http", artifact, spec)
            artifact.write_text(extra)
            with self.assertRaisesRegex(RuntimeError, "HTTPRoute"):
                self.bootstrap._assert_artifact_contract("http", artifact, spec)

    def test_reference_never_adopts_existing_cluster(self) -> None:
        with mock.patch.object(self.reference, "clusters", return_value={"occupied"}):
            with self.assertRaisesRegex(self.reference.ProofError, "never adopted"):
                self.reference.assert_available_cluster_name("kind", "occupied")

    def test_reference_refuses_unmarked_cluster_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            original = self.reference.MARKERS
            self.reference.MARKERS = Path(tmp)
            try:
                with self.assertRaisesRegex(self.reference.ProofError, "regular ownership marker"):
                    self.reference.delete_owned_cluster(
                        "kind",
                        "foreign",
                        expected_commit="a" * 40,
                        expected_owner_id="owner-proof",
                    )
            finally:
                self.reference.MARKERS = original

    def test_reference_never_silently_removes_stale_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            original = self.reference.MARKERS
            self.reference.MARKERS = Path(tmp)
            try:
                marker = self.reference.marker_path("stale")
                marker.write_text("{}\n", encoding="utf-8")
                with mock.patch.object(self.reference, "clusters", return_value=set()):
                    with self.assertRaisesRegex(self.reference.ProofError, "stale ownership marker"):
                        self.reference.assert_available_cluster_name("kind", "stale")
                self.assertTrue(marker.is_file())
            finally:
                self.reference.MARKERS = original

    def test_reference_refuses_cleanup_for_wrong_commit_or_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            original = self.reference.MARKERS
            self.reference.MARKERS = Path(tmp)
            try:
                self.reference.write_marker("proof", "a" * 40, "owner-a")
                with mock.patch.object(self.reference, "clusters", return_value=set()):
                    with self.assertRaisesRegex(self.reference.ProofError, "exact owner binding"):
                        self.reference.delete_owned_cluster(
                            "kind",
                            "proof",
                            expected_commit="b" * 40,
                            expected_owner_id="owner-a",
                        )
                    with self.assertRaisesRegex(self.reference.ProofError, "exact owner binding"):
                        self.reference.delete_owned_cluster(
                            "kind",
                            "proof",
                            expected_commit="a" * 40,
                            expected_owner_id="owner-b",
                        )
                self.assertTrue(self.reference.marker_path("proof").is_file())
            finally:
                self.reference.MARKERS = original

    def test_reference_reservation_records_exact_owner_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            original = self.reference.MARKERS
            self.reference.MARKERS = Path(tmp)
            try:
                with mock.patch.object(self.reference, "clusters", return_value=set()):
                    self.reference.reserve_cluster_name(
                        "kind", "proof", "a" * 40, "owner-proof"
                    )
                marker = json.loads(
                    self.reference.marker_path("proof").read_text(encoding="utf-8")
                )
                self.assertEqual(marker["schema_version"], 2)
                self.assertEqual(marker["commit"], "a" * 40)
                self.assertEqual(marker["owner_id"], "owner-proof")
            finally:
                self.reference.MARKERS = original

    def test_reference_refuses_symlink_ownership_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            original = self.reference.MARKERS
            self.reference.MARKERS = Path(tmp)
            try:
                target = Path(tmp) / "target.json"
                target.write_text("{}\n", encoding="utf-8")
                self.reference.marker_path("proof").symlink_to(target)
                with self.assertRaisesRegex(
                    self.reference.ProofError, "regular ownership marker"
                ):
                    self.reference.delete_owned_cluster(
                        "kind",
                        "proof",
                        expected_commit="a" * 40,
                        expected_owner_id="owner-proof",
                    )
            finally:
                self.reference.MARKERS = original

    def test_reference_cleanup_fsyncs_marker_and_kubeconfig_directories(self) -> None:
        commit = "a" * 40
        owner = "owner-durable-cleanup"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            markers = root / "clusters"
            kubeconfigs = root / "kubeconfigs"
            markers.mkdir()
            kubeconfigs.mkdir()
            original_markers = self.reference.MARKERS
            original_kubeconfigs = self.reference.KUBECONFIGS
            self.reference.MARKERS = markers
            self.reference.KUBECONFIGS = kubeconfigs
            try:
                self.reference.write_marker("proof", commit, owner)
                self.reference.kubeconfig_path("proof").write_text(
                    "apiVersion: v1\n", encoding="utf-8"
                )
                with (
                    mock.patch.object(self.reference, "clusters", return_value=set()),
                    mock.patch.object(
                        self.reference,
                        "_fsync_directory",
                        wraps=self.reference._fsync_directory,
                    ) as fsync_directory,
                ):
                    deleted = self.reference.delete_owned_cluster_if_present(
                        "kind",
                        "proof",
                        expected_commit=commit,
                        expected_owner_id=owner,
                    )
                self.assertTrue(deleted)
                self.assertFalse(self.reference.marker_path("proof").exists())
                self.assertFalse(self.reference.kubeconfig_path("proof").exists())
                fsync_directory.assert_has_calls(
                    [mock.call(markers), mock.call(kubeconfigs)], any_order=False
                )
            finally:
                self.reference.MARKERS = original_markers
                self.reference.KUBECONFIGS = original_kubeconfigs

    def test_creation_reservation_failure_uses_durable_state_cleanup(self) -> None:
        commit = "a" * 40
        owner = "owner-durable-rollback"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            markers = root / "clusters"
            kubeconfigs = root / "kubeconfigs"
            original_markers = self.reference.MARKERS
            original_kubeconfigs = self.reference.KUBECONFIGS
            self.reference.MARKERS = markers
            self.reference.KUBECONFIGS = kubeconfigs
            try:
                with (
                    mock.patch.object(self.reference, "clusters", return_value=set()),
                    mock.patch.object(
                        self.reference,
                        "_remove_cluster_state_files_durably",
                        wraps=self.reference._remove_cluster_state_files_durably,
                    ) as durable_cleanup,
                ):
                    with self.assertRaisesRegex(self.reference.ProofError, "create failed"):
                        with self.reference.cluster_creation_reservation(
                            "kind", "proof", commit, owner
                        ):
                            raise self.reference.ProofError("create failed")
                durable_cleanup.assert_called_once_with(
                    "proof", marker_missing_ok=True
                )
                self.assertFalse(self.reference.marker_path("proof").exists())
            finally:
                self.reference.MARKERS = original_markers
                self.reference.KUBECONFIGS = original_kubeconfigs

    def test_reference_refuses_cluster_without_marker_in_if_present_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            original = self.reference.MARKERS
            self.reference.MARKERS = Path(tmp)
            try:
                with mock.patch.object(
                    self.reference, "clusters", return_value={"proof"}
                ):
                    with self.assertRaisesRegex(
                        self.reference.ProofError, "exists without ownership marker"
                    ):
                        self.reference.delete_owned_cluster_if_present(
                            "kind",
                            "proof",
                            expected_commit="a" * 40,
                            expected_owner_id="owner-proof",
                        )
            finally:
                self.reference.MARKERS = original

    def test_kind_cluster_creation_retries_only_transient_registry_failure(self) -> None:
        transient = subprocess.CalledProcessError(
            1,
            ["kind", "create", "cluster"],
            output="",
            stderr="429 Too Many Requests from registry-1.docker.io",
        )
        success = subprocess.CompletedProcess(
            ["kind", "create", "cluster"], 0, stdout="created\n", stderr=""
        )
        with tempfile.TemporaryDirectory() as tmp:
            original = self.reference.MARKERS
            self.reference.MARKERS = Path(tmp)
            try:
                reservation = mock.MagicMock()
                with mock.patch.object(
                    self.reference,
                    "cluster_creation_reservation",
                    return_value=reservation,
                ) as reserve, mock.patch.object(
                    self.reference, "run", side_effect=[transient, success]
                ) as run, mock.patch.object(
                    self.reference, "configure_cluster_access"
                ) as configure, mock.patch.object(
                    self.reference.time, "sleep"
                ) as sleep:
                    self.reference.create_kind_cluster(
                        "kind",
                        "proof",
                        "kindest/node@sha256:" + "a" * 64,
                        "kind.yaml",
                        "a" * 40,
                        "owner-proof",
                        timeout=600,
                    )
                self.assertEqual(run.call_count, 2)
                self.assertEqual(reserve.call_count, 2)
                configure.assert_called_once_with("kind", "proof")
                sleep.assert_called_once_with(5.0)
            finally:
                self.reference.MARKERS = original

    def test_kind_cluster_creation_does_not_retry_contract_failure(self) -> None:
        permanent = subprocess.CalledProcessError(
            1,
            ["kind", "create", "cluster"],
            output="",
            stderr="invalid kind configuration",
        )
        with tempfile.TemporaryDirectory() as tmp:
            original = self.reference.MARKERS
            self.reference.MARKERS = Path(tmp)
            try:
                with mock.patch.object(
                    self.reference,
                    "cluster_creation_reservation",
                    return_value=mock.MagicMock(),
                ), mock.patch.object(
                    self.reference, "run", side_effect=permanent
                ) as run, mock.patch.object(
                    self.reference.time, "sleep"
                ) as sleep:
                    with self.assertRaises(subprocess.CalledProcessError):
                        self.reference.create_kind_cluster(
                            "kind",
                            "proof",
                            "kindest/node@sha256:" + "a" * 64,
                            "kind.yaml",
                            "a" * 40,
                            "owner-proof",
                            timeout=600,
                        )
                run.assert_called_once()
                sleep.assert_not_called()
            finally:
                self.reference.MARKERS = original

    def test_exact_owner_cleanup_reconciles_marker_after_sigkill(self) -> None:
        commit = "a" * 40
        owner = "owner-sigkill"
        cluster = "proof-sigkill"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            markers = root / "clusters"
            kubeconfigs = root / "kubeconfigs"
            child = f"""
import importlib.util
import time
from pathlib import Path
spec = importlib.util.spec_from_file_location(
    'sigkill_kind_reference',
    {str(ROOT / 'scripts/platform/kind_reference.py')!r},
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.MARKERS = Path({str(markers)!r})
module.KUBECONFIGS = Path({str(kubeconfigs)!r})
module.write_marker({cluster!r}, {commit!r}, {owner!r})
print('marker-published', flush=True)
time.sleep(60)
"""
            process = subprocess.Popen(
                [sys.executable, "-c", child],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(process.stdout.readline().strip(), "marker-published")
                process.kill()
                process.communicate(timeout=5)
                self.assertLess(process.returncode, 0)
                self.assertTrue((markers / f"{cluster}.json").is_file())

                original_markers = self.reference.MARKERS
                original_kubeconfigs = self.reference.KUBECONFIGS
                self.reference.MARKERS = markers
                self.reference.KUBECONFIGS = kubeconfigs
                try:
                    with mock.patch.object(
                        self.reference, "clusters", return_value=set()
                    ):
                        deleted = self.reference.delete_owned_cluster_if_present(
                            "kind",
                            cluster,
                            expected_commit=commit,
                            expected_owner_id=owner,
                        )
                    self.assertTrue(deleted)
                    self.assertFalse((markers / f"{cluster}.json").exists())
                finally:
                    self.reference.MARKERS = original_markers
                    self.reference.KUBECONFIGS = original_kubeconfigs
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)

    def test_reference_marker_publication_is_no_clobber_and_crash_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            original = self.reference.MARKERS
            self.reference.MARKERS = Path(tmp)
            try:
                with mock.patch.object(
                    self.reference.os, "link", side_effect=OSError("link failed")
                ):
                    with self.assertRaisesRegex(OSError, "link failed"):
                        self.reference.write_marker(
                            "proof", "a" * 40, "owner-proof"
                        )
                self.assertFalse(
                    self.reference.marker_path("proof").exists()
                )
                self.assertEqual(
                    list(Path(tmp).glob(".proof.*.marker.tmp")), []
                )
            finally:
                self.reference.MARKERS = original

    def test_reference_serializes_competing_reservations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            original = self.reference.MARKERS
            self.reference.MARKERS = Path(tmp)
            outcomes: list[str] = []
            start = threading.Barrier(2)

            def reserve() -> None:
                start.wait(timeout=1)
                try:
                    self.reference.reserve_cluster_name(
                        "kind", "proof", "a" * 40, "owner-proof"
                    )
                    outcomes.append("reserved")
                except self.reference.ProofError:
                    outcomes.append("rejected")

            try:
                with mock.patch.object(
                    self.reference, "clusters", return_value=set()
                ):
                    threads = [threading.Thread(target=reserve) for _ in range(2)]
                    for thread in threads:
                        thread.start()
                    for thread in threads:
                        thread.join(timeout=2)
                self.assertCountEqual(outcomes, ["reserved", "rejected"])
            finally:
                self.reference.MARKERS = original

    def test_creation_reservation_blocks_exact_owner_cleanup_until_create_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            original = self.reference.MARKERS
            self.reference.MARKERS = Path(tmp)
            created: set[str] = set()
            entered = threading.Event()
            release = threading.Event()
            cleanup_started = threading.Event()
            cleanup_finished = threading.Event()
            cleanup_result: list[bool] = []

            def observed_clusters(_kind: str) -> set[str]:
                return set(created)

            def creator() -> None:
                with self.reference.cluster_creation_reservation(
                    "kind", "proof", "a" * 40, "owner-proof"
                ):
                    entered.set()
                    release.wait(timeout=2)
                    created.add("proof")

            def cleanup() -> None:
                cleanup_started.set()
                cleanup_result.append(
                    self.reference.delete_owned_cluster_if_present(
                        "kind",
                        "proof",
                        expected_commit="a" * 40,
                        expected_owner_id="owner-proof",
                    )
                )
                cleanup_finished.set()

            try:
                with mock.patch.object(
                    self.reference, "clusters", side_effect=observed_clusters
                ), mock.patch.object(self.reference, "run"):
                    creator_thread = threading.Thread(target=creator)
                    cleanup_thread = threading.Thread(target=cleanup)
                    creator_thread.start()
                    self.assertTrue(entered.wait(timeout=1))
                    cleanup_thread.start()
                    self.assertTrue(cleanup_started.wait(timeout=1))
                    time.sleep(0.05)
                    self.assertFalse(cleanup_finished.is_set())
                    release.set()
                    creator_thread.join(timeout=2)
                    cleanup_thread.join(timeout=2)
                self.assertEqual(cleanup_result, [True])
                self.assertFalse(self.reference.marker_path("proof").exists())
            finally:
                release.set()
                self.reference.MARKERS = original

    def test_creation_reservation_preserves_original_error_when_rollback_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            original = self.reference.MARKERS
            self.reference.MARKERS = Path(tmp)
            try:
                with mock.patch.object(
                    self.reference,
                    "clusters",
                    side_effect=[set(), {"proof"}],
                ), mock.patch.object(
                    self.reference,
                    "run",
                    side_effect=self.reference.ProofError("rollback failed"),
                ):
                    with self.assertRaisesRegex(
                        self.reference.ProofError, "create failed"
                    ):
                        with self.reference.cluster_creation_reservation(
                            "kind", "proof", "a" * 40, "owner-proof"
                        ):
                            raise self.reference.ProofError("create failed")
                self.assertTrue(self.reference.marker_path("proof").is_file())
            finally:
                self.reference.MARKERS = original

    def test_web_container_copies_postinstall_script_before_install(self) -> None:
        dockerfile = (ROOT / "apps/web/Dockerfile").read_text()
        script_copy = dockerfile.index(
            "COPY apps/web/scripts/verify-cookie-version.js ./apps/web/scripts/verify-cookie-version.js"
        )
        install = dockerfile.index("RUN pnpm -C apps/web install --frozen-lockfile")
        self.assertLess(script_copy, install)
        self.assertIn("COPY apps/web ./apps/web", dockerfile)
        self.assertIn("COPY map-style ./map-style", dockerfile)
        self.assertIn(
            "COPY policies/performance.v1.json ./policies/performance.v1.json",
            dockerfile,
        )
        self.assertIn(
            "COPY --from=builder /workspace/apps/web/build /srv/weltgewebe",
            dockerfile,
        )
        self.assertIn("pnpm -C apps/web run build:container", dockerfile)
        package = (ROOT / "apps/web/package.json").read_text()
        self.assertIn("assert-route-performance-budget.mjs --budget-only", package)

    def test_web_container_removes_caddy_capability_before_nonroot_user(self) -> None:
        dockerfile = (ROOT / "apps/web/Dockerfile").read_text()
        final_stage = dockerfile.index(
            "FROM caddy:2.7@sha256:"
            "236c6a30ccb84fa412a5360ca8b586d804faba0621ea182fb45902608cd8a563"
        )
        remove_capability = dockerfile.index("RUN setcap -r /usr/bin/caddy")
        chmod_config = dockerfile.index("&& chmod 0444 /etc/caddy/Caddyfile")
        user = dockerfile.index("USER 10001:10001")
        self.assertLess(final_stage, remove_capability)
        self.assertLess(remove_capability, chmod_config)
        self.assertLess(chmod_config, user)
        self.assertNotIn("caddy.uncapped", dockerfile)

    def test_api_container_scripts_are_world_readable_and_executable(self) -> None:
        dockerfile = (ROOT / "apps/api/Dockerfile").read_text()
        self.assertIn(
            "RUN chmod 0755 /usr/local/bin/generate-demo-data "
            "/usr/local/bin/bootstrap-first-account /usr/local/bin/entrypoint.sh",
            dockerfile,
        )
        self.assertNotIn("RUN chmod +x /usr/local/bin/generate-demo-data", dockerfile)

    def test_api_container_normalizes_policy_permissions_before_nonroot_user(self) -> None:
        dockerfile = (ROOT / "apps/api/Dockerfile").read_text()
        copy_policies = dockerfile.index("COPY policies /app/policies")
        chmod_directories = dockerfile.index(
            "find /app/policies -type d -exec chmod 0755 {} +"
        )
        chmod_files = dockerfile.index(
            "find /app/policies -type f -exec chmod 0644 {} +"
        )
        user = dockerfile.index("USER 10001:10001")
        self.assertLess(copy_policies, chmod_directories)
        self.assertLess(chmod_directories, chmod_files)
        self.assertLess(chmod_files, user)

    def test_ready_api_pods_exclude_terminating_and_unready_pods(self) -> None:
        document = {
            "items": [
                {
                    "metadata": {"name": "terminating", "uid": "uid-old", "deletionTimestamp": "now"},
                    "status": {
                        "phase": "Running",
                        "conditions": [{"type": "Ready", "status": "True"}],
                        "containerStatuses": [{"ready": True}],
                    },
                },
                {
                    "metadata": {"name": "unready", "uid": "uid-unready"},
                    "status": {
                        "phase": "Running",
                        "conditions": [{"type": "Ready", "status": "False"}],
                        "containerStatuses": [{"ready": False}],
                    },
                },
                {
                    "metadata": {"name": "current", "uid": "uid-current"},
                    "status": {
                        "phase": "Running",
                        "conditions": [{"type": "Ready", "status": "True"}],
                        "containerStatuses": [{"ready": True}],
                    },
                },
            ]
        }
        with mock.patch.object(
            self.reference, "output", return_value=json.dumps(document)
        ):
            self.assertEqual(
                self.reference.ready_api_pods("kubectl", "weltgewebe"),
                [("current", "uid-current")],
            )

    def test_api_version_reselects_current_ready_pod_after_failure(self) -> None:
        temporary_failure = subprocess.CalledProcessError(4, ["wget"])
        response = '{"git_commit":"0123456789abcdef0123456789abcdef01234567"}'
        first = {
            "items": [{
                "metadata": {"name": "api-old", "uid": "uid-old"},
                "status": {
                    "phase": "Running",
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "containerStatuses": [{"ready": True}],
                },
            }]
        }
        second = {
            "items": [
                {
                    "metadata": {"name": "api-old", "uid": "uid-old", "deletionTimestamp": "now"},
                    "status": {
                        "phase": "Running",
                        "conditions": [{"type": "Ready", "status": "True"}],
                        "containerStatuses": [{"ready": True}],
                    },
                },
                {
                    "metadata": {"name": "api-new", "uid": "uid-new"},
                    "status": {
                        "phase": "Running",
                        "conditions": [{"type": "Ready", "status": "True"}],
                        "containerStatuses": [{"ready": True}],
                    },
                },
            ]
        }
        with mock.patch.object(
            self.reference,
            "output",
            side_effect=[
                json.dumps(first),
                temporary_failure,
                json.dumps(second),
                response,
            ],
        ) as output_mock, mock.patch.object(
            self.reference.time, "sleep"
        ) as sleep:
            observed = self.reference.api_version("kubectl", "weltgewebe")
        self.assertEqual(observed, response)
        self.assertEqual(output_mock.call_args_list[1].args[0][4], "api-old")
        self.assertEqual(output_mock.call_args_list[3].args[0][4], "api-new")
        sleep.assert_called_once_with(1)

    def test_restart_proof_requires_complete_pod_replacement(self) -> None:
        response = '{"git_commit":"0123456789abcdef0123456789abcdef01234567"}'
        with (
            mock.patch.object(
                self.reference,
                "ready_api_pod_uids",
                side_effect=[{"old-a", "old-b"}, {"new-a", "new-b"}],
            ),
            mock.patch.object(
                self.reference, "api_version", side_effect=[response, response]
            ),
            mock.patch.object(self.reference, "run"),
            mock.patch.object(self.reference, "wait_rollout"),
        ):
            result = self.reference.prove_restart("kubectl", "weltgewebe")
        self.assertEqual(result["before"], result["after"])
        self.assertEqual(result["replaced_replicas"], "2")
        self.assertNotEqual(
            result["before_pods_sha256"], result["after_pods_sha256"]
        )

    def test_restart_proof_rejects_surviving_ready_pod(self) -> None:
        response = '{"git_commit":"0123456789abcdef0123456789abcdef01234567"}'
        with (
            mock.patch.object(
                self.reference,
                "ready_api_pod_uids",
                side_effect=[{"old-a", "old-b"}, {"old-a", "new-b"}],
            ),
            mock.patch.object(self.reference, "api_version", return_value=response),
            mock.patch.object(self.reference, "run"),
            mock.patch.object(self.reference, "wait_rollout"),
        ):
            with self.assertRaisesRegex(
                self.reference.ProofError, "did not replace every ready pod"
            ):
                self.reference.prove_restart("kubectl", "weltgewebe")

    def test_reference_binds_cluster_access_after_cluster_creation(self) -> None:
        source = (ROOT / "scripts/platform/kind_reference.py").read_text()
        self.assertNotIn('"--wait",\n                "180s"', source)
        self.assertIn("def create_kind_cluster(", source)
        self.assertIn("configure_cluster_access(kind, name)", source)
        self.assertIn("create_kind_cluster(\n            kind,\n            args.cluster,", source)
        self.assertIn('"--for=condition=Ready", "nodes"', source)

    def test_control_plane_address_uses_kind_network(self) -> None:
        payload = [
            {
                "NetworkSettings": {
                    "Networks": {
                        "other": {"IPAddress": "172.19.0.9"},
                        "kind": {"IPAddress": "172.18.0.2"},
                    }
                }
            }
        ]
        with mock.patch.object(
            self.reference, "output", return_value=json.dumps(payload)
        ):
            self.assertEqual(
                self.reference.control_plane_address("reference"), "172.18.0.2"
            )

    def test_http_route_wait_reads_parent_scoped_conditions(self) -> None:
        route = {
            "metadata": {"generation": 3},
            "status": {
                "parents": [
                    {
                        "parentRef": {
                            "name": "weltgewebe",
                            "namespace": "weltgewebe-gateway",
                        },
                        "conditions": [
                            {
                                "type": "Accepted",
                                "status": "True",
                                "observedGeneration": 3,
                            },
                            {
                                "type": "ResolvedRefs",
                                "status": "True",
                                "observedGeneration": 3,
                            },
                        ],
                    }
                ]
            }
        }
        with mock.patch.object(
            self.reference, "output", return_value=json.dumps(route)
        ) as output:
            self.reference.wait_http_route_parent_condition(
                "kubectl",
                "weltgewebe",
                "weltgewebe",
                "Accepted",
                parent_name="weltgewebe",
                parent_namespace="weltgewebe-gateway",
                timeout_seconds=1,
            )
        output.assert_called_once_with(
            [
                "kubectl",
                "-n",
                "weltgewebe",
                "get",
                "httproute/weltgewebe",
                "-o",
                "json",
            ]
        )

    def test_http_route_wait_rejects_stale_generation(self) -> None:
        stale = {
            "metadata": {"generation": 2},
            "status": {
                "parents": [
                    {
                        "parentRef": {
                            "name": "weltgewebe",
                            "namespace": "weltgewebe-gateway",
                        },
                        "conditions": [
                            {
                                "type": "Accepted",
                                "status": "True",
                                "observedGeneration": 1,
                            }
                        ],
                    }
                ]
            },
        }
        current = json.loads(json.dumps(stale))
        current["status"]["parents"][0]["conditions"][0][
            "observedGeneration"
        ] = 2
        with mock.patch.object(
            self.reference,
            "output",
            side_effect=[json.dumps(stale), json.dumps(current)],
        ), mock.patch.object(self.reference.time, "sleep") as sleep:
            self.reference.wait_http_route_parent_condition(
                "kubectl",
                "weltgewebe",
                "weltgewebe",
                "Accepted",
                parent_name="weltgewebe",
                parent_namespace="weltgewebe-gateway",
                timeout_seconds=10,
            )
        sleep.assert_called_once_with(2)

    def test_gateway_contract_uses_cilium_kube_proxy_replacement(self) -> None:
        kind_config = (ROOT / "platform/clusters/local/kind.yaml").read_text()
        source = (ROOT / "scripts/platform/kind_reference.py").read_text()
        self.assertIn("disableDefaultCNI: true", kind_config)
        self.assertIn("kubeProxyMode: none", kind_config)
        self.assertIn('"gatewayAPI.enabled=true"', source)
        self.assertNotIn('"gatewayAPI.hostNetwork.enabled=true"', source)
        self.assertIn('"nodeIPAM.enabled=true"', source)
        self.assertIn('"defaultLBServiceIPAM=nodeipam"', source)
        self.assertIn('"kubeProxyReplacement=true"', source)
        self.assertIn('f"k8sServiceHost={api_server_host}"', source)
        self.assertIn('"k8sServicePort=6443"', source)

    def test_gateway_addresses_use_status_ipv4_values(self) -> None:
        document = {"status": {"addresses": [
            {"type": "Hostname", "value": "ignored.example"},
            {"type": "IPAddress", "value": "172.22.0.3"},
            {"type": "IPAddress", "value": "172.22.0.4"},
            {"type": "IPAddress", "value": "172.22.0.3"},
        ]}}
        with mock.patch.object(self.reference, "output", return_value=json.dumps(document)):
            self.assertEqual(self.reference.gateway_addresses("kubectl"), ["172.22.0.3", "172.22.0.4"])

    def test_gateway_listener_port_requires_one_http_listener(self) -> None:
        document = {
            "spec": {"listeners": [{"protocol": "HTTP", "port": 80}]}
        }
        with mock.patch.object(
            self.reference, "output", return_value=json.dumps(document)
        ):
            self.assertEqual(self.reference.gateway_listener_port("kubectl"), 80)
        document["spec"]["listeners"].append({"protocol": "HTTP", "port": 8080})
        with mock.patch.object(
            self.reference, "output", return_value=json.dumps(document)
        ):
            with self.assertRaisesRegex(
                self.reference.ProofError, "exactly one HTTP listener"
            ):
                self.reference.gateway_listener_port("kubectl")

    def test_gateway_service_binds_load_balancer_listener_port(self) -> None:
        document = {
            "spec": {
                "type": "LoadBalancer",
                "ports": [{"protocol": "TCP", "port": 80, "nodePort": 31293}],
            }
        }
        with mock.patch.object(
            self.reference, "output", return_value=json.dumps(document)
        ):
            self.assertEqual(
                self.reference.gateway_service_listener_port(
                    "kubectl", "gateway-service", 80
                ),
                80,
            )
        document["spec"]["type"] = "NodePort"
        with mock.patch.object(
            self.reference, "output", return_value=json.dumps(document)
        ):
            with self.assertRaisesRegex(self.reference.ProofError, "not a LoadBalancer"):
                self.reference.gateway_service_listener_port(
                    "kubectl", "gateway-service", 80
                )

    def test_kind_nodes_require_nonempty_cluster(self) -> None:
        with mock.patch.object(
            self.reference,
            "output",
            return_value="control-plane\nworker\n",
        ):
            self.assertEqual(
                self.reference.kind_nodes("kind", "cluster"),
                ["control-plane", "worker"],
            )
        with mock.patch.object(self.reference, "output", return_value=""):
            with self.assertRaisesRegex(self.reference.ProofError, "no nodes"):
                self.reference.kind_nodes("kind", "cluster")

    def test_gateway_http_probe_uses_status_address_listener_port(self) -> None:
        health = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"healthy", stderr=b""
        )
        web = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"web", stderr=b""
        )
        api_nodes = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"[]", stderr=b""
        )
        with (
            mock.patch.object(
                self.reference,
                "kind_nodes",
                return_value=["cluster-worker"],
            ),
            mock.patch.object(
                self.reference.subprocess,
                "run",
                side_effect=[health, web, api_nodes],
            ) as run_mock,
        ):
            result = self.reference.probe_gateway_http(
                "kind",
                "cluster",
                ["172.22.0.3"],
                80,
                timeout_seconds=1,
            )
        self.assertEqual(
            result,
            ("cluster-worker", "172.22.0.3", b"healthy", b"web", b"[]"),
        )
        argv = run_mock.call_args_list[0].args[0]
        self.assertEqual(argv[:3], ["docker", "exec", "cluster-worker"])
        self.assertIn("curl", argv)
        self.assertIn("--fail", argv)
        self.assertIn("--max-time", argv)
        self.assertEqual(argv[-1], "http://172.22.0.3:80/health/live")
        self.assertEqual(
            run_mock.call_args_list[1].args[0][-1],
            "http://172.22.0.3:80/",
        )
        self.assertEqual(
            run_mock.call_args_list[2].args[0][-1],
            "http://172.22.0.3:80/api/nodes",
        )

    def test_gateway_http_probe_rejects_broken_api_rewrite(self) -> None:
        health = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"healthy", stderr=b""
        )
        web = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"web", stderr=b""
        )
        broken_api = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b'{"not":"a-list"}', stderr=b""
        )
        with (
            mock.patch.object(
                self.reference,
                "kind_nodes",
                return_value=["cluster-worker"],
            ),
            mock.patch.object(
                self.reference.subprocess,
                "run",
                side_effect=[health, web, broken_api],
            ),
            mock.patch.object(
                self.reference.time,
                "monotonic",
                side_effect=[0, 1],
            ),
        ):
            with self.assertRaisesRegex(
                self.reference.ProofError, "did not serve listener HTTP"
            ):
                self.reference.probe_gateway_http(
                    "kind",
                    "cluster",
                    ["172.22.0.3"],
                    80,
                    timeout_seconds=0,
                )

    def test_gateway_policy_allows_only_cilium_ingress_identity(self) -> None:
        source = (
            ROOT
            / "platform/apps/weltgewebe/base/network-policy-gateway-ingress.yaml"
        ).read_text()
        self.assertIn("apiVersion: cilium.io/v2", source)
        self.assertIn("kind: CiliumNetworkPolicy", source)
        self.assertIn("fromEntities:", source)
        self.assertIn("- ingress", source)
        self.assertIn('port: "8080"', source)
        self.assertNotIn("namespaceSelector:", source)

    def test_gateway_proof_does_not_port_forward_selectorless_service(self) -> None:
        source = (ROOT / "scripts/platform/kind_reference.py").read_text()
        self.assertNotIn("def port_forward", source)
        self.assertNotIn('"port-forward"', source)
        self.assertNotIn("gateway_node_port", source)
        self.assertIn(
            "gateway_service_listener_port(kubectl, service, listener_port)", source
        )

    def test_full_proof_uses_canonical_builder_signature(self) -> None:
        source = (ROOT / "scripts/platform/kind_reference.py").read_text()
        self.assertIn(
            "image_ids = build_images(kind, args.cluster, commit, timestamp)",
            source,
        )
        self.assertNotIn("commit_timestamp()", source)
        self.assertNotIn("load_images(tools", source)

    def test_local_fixture_is_public_config_map_only(self) -> None:
        data_fixture = (
            ROOT / "platform/infrastructure/local-data/fixture-config-map.yaml"
        ).read_text()
        app_fixture = (
            ROOT / "platform/apps/weltgewebe/migration/local/fixture-config-map.yaml"
        ).read_text()
        self.assertIn("kind: ConfigMap", data_fixture)
        self.assertIn("kind: ConfigMap", app_fixture)
        self.assertIn("local-test-only-weltgewebe", data_fixture)
        self.assertIn("local-test-only-weltgewebe", app_fixture)
        self.assertNotIn("kind: Secret", data_fixture + app_fixture)
        patch = (
            ROOT / "platform/apps/weltgewebe/overlays/local/database-url-patch.yaml"
        ).read_text()
        self.assertIn("configMapKeyRef:", patch)
        self.assertIn("name: weltgewebe-local-fixture", patch)

    def test_migration_is_a_declarative_completion_gated_job(self) -> None:
        job = self.reference.yaml.safe_load(
            (ROOT / "platform/apps/weltgewebe/migration/local/job.yaml").read_text()
        )
        self.assertEqual(job["apiVersion"], "batch/v1")
        self.assertEqual(job["kind"], "Job")
        self.assertEqual(job["spec"]["backoffLimit"], 0)
        pod = job["spec"]["template"]["spec"]
        self.assertEqual(pod["restartPolicy"], "Never")
        self.assertFalse(pod["automountServiceAccountToken"])
        container = pod["containers"][0]
        environment = {item["name"]: item for item in container["env"]}
        self.assertEqual(environment["WELTGEWEBE_API_MIGRATION_ONLY"]["value"], "1")
        self.assertEqual(environment["WELTGEWEBE_API_STARTUP_MIGRATIONS"]["value"], "run")
        self.assertEqual(
            environment["DATABASE_URL"]["valueFrom"]["configMapKeyRef"],
            {"name": "weltgewebe-local-fixture", "key": "database-url"},
        )
        source = (ROOT / "scripts/platform/kind_reference.py").read_text()
        self.assertIn("migrate_direct(kubectl, kustomize)", source)
        self.assertIn('"job/weltgewebe-migration"', source)
        self.assertIn('"Complete"', source)
        self.assertNotIn("runtime_secret_document", source)
        self.assertNotIn("migration_pod", source)

    def test_flux_chain_gates_app_on_completed_migration(self) -> None:
        migration = self.reference.yaml.safe_load(
            (ROOT / "platform/clusters/local/migration.yaml").read_text()
        )
        app = self.reference.yaml.safe_load(
            (ROOT / "platform/clusters/local/app.yaml").read_text()
        )
        self.assertEqual(
            migration["spec"]["dependsOn"], [{"name": "weltgewebe-local-data"}]
        )
        self.assertTrue(migration["spec"]["force"])
        self.assertEqual(app["spec"]["dependsOn"], [{"name": "weltgewebe-migration"}])

    def test_reference_exposes_only_full_proof_and_owned_down(self) -> None:
        source = (ROOT / "scripts/platform/kind_reference.py").read_text()
        self.assertIn('subparsers.add_parser("proof")', source)
        self.assertIn('subparsers.add_parser("down")', source)
        for command in ("phase-a", "phase-b", "phase-c", "phase-d"):
            self.assertNotIn(f'subparsers.add_parser("{command}")', source)
        for helper in ("data_up", "app_verify", "refresh_images", "local_data_up"):
            self.assertNotIn(f"def {helper}", source)

    def test_secret_contract_contains_no_values(self) -> None:
        contract = json.loads(
            (ROOT / "platform/apps/weltgewebe/secret-contract.json").read_text()
        )
        self.assertEqual(contract["required_keys"], ["database-url"])
        self.assertNotIn("stringData", contract)
        self.assertNotIn("data", contract)


if __name__ == "__main__":
    unittest.main()
