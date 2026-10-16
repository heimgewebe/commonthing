"""Live Kind worker sysctl verification is a fail-closed startup boundary.

No Docker or Kubernetes side effects: Docker observations are mocked.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.platform import staging_cell as staging


class KindWorkerSrcmarkProofTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="t044-srcmark-proof-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        staging.render_kind_config(self.root)
        self.source = self.root / "generated/kind-cilium-srcmark.conf"
        self.cluster = staging.DEFAULT_CLUSTER
        self.nodes = (
            f"{self.cluster}-control-plane",
            f"{self.cluster}-worker",
            f"{self.cluster}-worker2",
        )
        self.ids = {name: hex(index + 10)[2:] * 64 for index, name in enumerate(self.nodes)}
        self.mount = {
            "Type": "bind",
            "Source": str(self.source.resolve()),
            "Destination": staging.KIND_WORKER_SRCMARK_DESTINATION,
            "RW": False,
        }

    def _probe(self, *, mark: str = "0\n0\n0", wrong_node: str = "",
               bad_mount: dict | None = None, drift_id: bool = False) -> list[list[str]]:
        observed: list[list[str]] = []
        id_reads: dict[str, int] = {}

        def response(argv: list[str]) -> str:
            observed.append(argv)
            if argv[:3] == ["docker", "inspect", "--format"]:
                node = argv[4]
                self.assertIn(node, self.nodes)
                if argv[3] == "{{.Id}}":
                    count = id_reads.get(node, 0)
                    id_reads[node] = count + 1
                    if drift_id and node == self.nodes[1] and count:
                        return "f" * 64
                    return self.ids[node]
                if argv[3] == "{{json .Mounts}}":
                    mounts = [] if node == self.nodes[0] else [dict(self.mount)]
                    if node == wrong_node:
                        mounts = [dict(bad_mount)] if bad_mount is not None else []
                    return json.dumps(mounts)
            if argv[:3] == ["docker", "exec", self.nodes[1]] or argv[:3] == ["docker", "exec", self.nodes[2]]:
                self.assertEqual(argv[3:5], ["sysctl", "-n"])
                self.assertEqual(argv[5:], [
                    "net.ipv4.conf.all.src_valid_mark",
                    "net.ipv4.conf.default.src_valid_mark",
                    "net.ipv4.conf.eth0.src_valid_mark",
                ])
                return mark
            self.fail(f"unexpected observer invocation: {argv}")

        with mock.patch.object(staging.reference, "output", side_effect=response):
            staging.reference.require_kind_worker_srcmark_runtime(self.root, self.cluster)
        return observed

    def test_both_workers_and_control_plane_are_bound_and_live(self) -> None:
        calls = self._probe()
        self.assertEqual(len([a for a in calls if a[0:2] == ["docker", "exec"]]), 2)
        self.assertEqual(len([a for a in calls if a[3:4] == ["{{.Id}}"]]), 6)

    def test_absent_mount_fails_closed(self) -> None:
        with self.assertRaisesRegex(staging.reference.ProofError, "srcmark"):
            self._probe(wrong_node=self.nodes[2])

    def test_mutable_or_wrong_source_mount_fails_closed(self) -> None:
        for entry in (
            {**self.mount, "RW": True},
            {**self.mount, "Source": "/tmp/foreign-srcmark.conf"},
            {**self.mount, "Destination": "/tmp/foreign.conf"},
        ):
            with self.subTest(entry=entry), self.assertRaisesRegex(staging.reference.ProofError, "srcmark"):
                self._probe(wrong_node=self.nodes[1], bad_mount=entry)

    def test_control_plane_must_not_receive_worker_srcmark(self) -> None:
        with self.assertRaisesRegex(staging.reference.ProofError, "srcmark"):
            self._probe(wrong_node=self.nodes[0], bad_mount=self.mount)

    def test_nonzero_effective_sysctl_fails_closed(self) -> None:
        for mark in ("1\n0\n0", "0\n1\n0", "0\n0\n1", "0\n0", ""):
            with self.subTest(mark=mark), self.assertRaisesRegex(staging.reference.ProofError, "srcmark"):
                self._probe(mark=mark)

    def test_container_identity_replacement_fails_closed(self) -> None:
        with self.assertRaisesRegex(staging.reference.ProofError, "srcmark"):
            self._probe(drift_id=True)

    def test_missing_or_modified_source_fails_before_observation(self) -> None:
        self.source.chmod(0o600)
        self.source.write_text("net.ipv4.conf.all.src_valid_mark = 1\n", encoding="utf-8")
        self.source.chmod(0o400)
        with mock.patch.object(staging.reference, "output") as output:
            with self.assertRaisesRegex(staging.reference.ProofError, "srcmark"):
                staging.reference.require_kind_worker_srcmark_runtime(self.root, self.cluster)
            output.assert_not_called()


if __name__ == "__main__":
    unittest.main()
