"""Real filesystem and schema negative proofs for the immutable root HOLD gate."""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/ops/verify_production_hold_release.py"
SPEC = importlib.util.spec_from_file_location("weltgewebe_production_hold_verifier", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)

COMMIT = "a" * 40
IMAGE = "sha256:" + "b" * 64


class HoldReleaseVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="hold-release-gate-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.parent = self.root / "weltgewebe"
        self.parent.mkdir(mode=0o700)
        self.file = self.parent / "production-hold-release.json"
        self.uid = os.getuid()
        self.data = {
            "schema_version": 1,
            "kind": verifier.KIND,
            "environment": "production",
            "decision": "release",
            "commit": COMMIT,
            "schaubild_image_digest": IMAGE,
            "offhost_age_recovery_sha256": "1" * 64,
            "restic_authority_sha256": "2" * 64,
            "privacy_acceptance_sha256": "3" * 64,
            "alarm_delivery_sha256": "4" * 64,
            "human_acknowledgement_sha256": "5" * 64,
            "approved_at": "2026-10-24T07:00:00+00:00",
            "authorized_by": "operator:release",
        }
        self.write()

    def write(self, *, payload: str | None = None, mode: int = 0o600) -> None:
        self.file.write_text(payload if payload is not None else json.dumps(self.data), encoding="utf-8")
        self.file.chmod(mode)

    def check(self, *, commit: str | None = COMMIT, image: str | None = IMAGE) -> dict[str, object]:
        return verifier.read_authorization(
            self.root, expected_uid=self.uid,
            expected_commit=commit, expected_image=image,
        )

    def reject(self, *, commit: str | None = COMMIT, image: str | None = IMAGE) -> None:
        with self.assertRaises(verifier.HoldReleaseError):
            self.check(commit=commit, image=image)

    def test_exact_release_and_oci_digest_are_verified(self) -> None:
        self.assertEqual(self.check()["commit"], COMMIT)
        self.assertEqual(self.check()["schaubild_image_digest"], IMAGE)
        self.assertEqual(self.check(commit=None, image=None)["kind"], verifier.KIND)

    def test_hold_missing_wrong_commit_or_wrong_oci_deny(self) -> None:
        self.reject(commit="b" * 40)
        self.reject(image="sha256:" + "a" * 64)
        self.file.unlink()
        self.reject()

    def test_invalid_receipts_and_no_production_decision_deny(self) -> None:
        for key, value in (
            ("decision", "hold"),
            ("privacy_acceptance_sha256", ""),
            ("alarm_delivery_sha256", "false"),
            ("human_acknowledgement_sha256", None),
            ("schema_version", True),
            ("authorized_by", ""),
            ("approved_at", "2026-10-24"),
            ("commit", "a" * 39),
            ("schaubild_image_digest", "latest"),
        ):
            with self.subTest(key=key):
                original = self.data[key]
                self.data[key] = value
                self.write()
                self.reject()
                self.data[key] = original

    def test_extra_missing_and_duplicate_fields_deny(self) -> None:
        self.data["untrusted_gate"] = "PASS"
        self.write()
        self.reject()
        del self.data["untrusted_gate"]
        del self.data["offhost_age_recovery_sha256"]
        self.write()
        self.reject()
        self.data["offhost_age_recovery_sha256"] = "1" * 64
        raw = json.dumps(self.data)
        self.write(payload=raw[:-1] + ', "commit": "' + COMMIT + '"}')
        self.reject()

    def test_symlink_hardlink_mode_and_unsafe_parent_deny(self) -> None:
        target = self.root / "original"
        target.write_text(json.dumps(self.data), encoding="utf-8")
        target.chmod(0o600)
        self.file.unlink()
        self.file.symlink_to(target)
        self.reject()
        self.file.unlink()
        os.link(target, self.file)
        self.reject()
        self.file.unlink()
        self.write(mode=0o644)
        self.reject()
        self.write()
        self.parent.chmod(0o777)
        self.reject()

    def test_payload_size_and_partial_json_deny(self) -> None:
        self.write(payload=" " * 4097)
        self.reject()
        self.write(payload='{"schema_version":1')
        self.reject()

    def test_cli_has_no_caller_selected_path_or_uid(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn('parser.add_argument("--root"', source)
        self.assertNotIn('parser.add_argument("--path"', source)
        self.assertNotIn('parser.add_argument("--uid"', source)
        self.assertIn('if os.geteuid() != 0:', source)


if __name__ == "__main__":
    unittest.main()
