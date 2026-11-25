"""Real filesystem and schema negative proofs for the immutable root HOLD gate."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock
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

    def test_root_validated_approved_commit_projection_exposes_only_sha(self) -> None:
        approval = self.check(commit=None, image=None)
        buffer = io.StringIO()
        with mock.patch.object(verifier.os, "geteuid", return_value=0):
            with mock.patch.object(verifier, "read_authorization", return_value=approval):
                with redirect_stdout(buffer):
                    result = verifier.main(["--approved-commit"])
        self.assertEqual(result, 0)
        self.assertEqual(buffer.getvalue(), COMMIT + "\n")
        self.assertNotIn(IMAGE, buffer.getvalue())
        self.assertNotIn("restic", buffer.getvalue())

    def test_genuine_recovery_only_release_needs_no_formal_ack_hashes(self) -> None:
        for key in verifier.OPTIONAL_RECEIPTS:
            del self.data[key]
        self.write()
        self.assertEqual(self.check()["commit"], COMMIT)
        self.assertEqual(self.check()["restic_authority_sha256"], "2" * 64)

    def test_both_recovery_receipts_remain_mandatory(self) -> None:
        for key in verifier.REQUIRED_RECEIPTS:
            with self.subTest(key=key):
                original = self.data.pop(key)
                self.write()
                self.reject()
                self.data[key] = original

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


    def test_exact_commit_lock_digest_rejects_mismatched_root_grant(self) -> None:
        import subprocess

        repo = self.root / "producer"
        repo.mkdir()
        lock = repo / "infra" / "schauwerk-editor"
        lock.mkdir(parents=True)
        data = {
            "schema_version": "weltgewebe-schauwerk-runtime-lock.v1",
            "source_repository": "heimgewebe/schauwerk",
            "source_commit": "c" * 40,
            "image_repository": "ghcr.io/heimgewebe/schauwerk-schaubild",
            "image_digest": IMAGE,
            "public_base_path": "/schaubild",
        }
        (lock / "release-lock.json").write_text(json.dumps(data), encoding="utf-8")
        for command in (
            ["git", "init", "-q", str(repo)],
            ["git", "-C", str(repo), "add", "infra/schauwerk-editor/release-lock.json"],
            ["git", "-C", str(repo), "-c", "user.name=Fixture", "-c",
             "user.email=fixture@example.invalid", "commit", "-qm", "lock"],
        ):
            subprocess.run(command, check=True, capture_output=True, text=True)
        commit = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
        ).strip()
        self.data["commit"] = commit
        self.write()
        self.assertEqual(
            verifier.read_locked_image_digest(repo, commit, expected_uid=self.uid),
            IMAGE,
        )
        alternate = repo / ".git" / "objects" / "info" / "alternates"
        alternate.write_text(str(self.root / "untrusted-objects") + "\n", encoding="utf-8")
        with self.assertRaisesRegex(verifier.HoldReleaseError, "alternate"):
            verifier.read_locked_image_digest(repo, commit, expected_uid=self.uid)
        alternate.unlink()
        self.assertEqual(self.check(commit=commit, image=IMAGE)["commit"], commit)
        self.data["schaubild_image_digest"] = "sha256:" + "d" * 64
        self.write()
        # The caller must bind the same immutable source digest before effects.
        self.reject(commit=commit, image=verifier.read_locked_image_digest(
            repo, commit, expected_uid=self.uid
        ))
        data["image_digest"] = "latest"
        (lock / "release-lock.json").write_text(json.dumps(data), encoding="utf-8")
        # An unchanged immutable source SHA must still resolve to the old blob.
        self.assertEqual(
            verifier.read_locked_image_digest(repo, commit, expected_uid=self.uid),
            IMAGE,
        )
        with self.assertRaises(verifier.HoldReleaseError):
            verifier.read_locked_image_digest(repo, "e" * 40, expected_uid=self.uid)



    def effective(self, commit: str, image: str = IMAGE) -> dict[str, object]:
        return verifier.read_effective_authorization(
            self.root, expected_uid=self.uid, expected_commit=commit, expected_image=image,
        )

    def fixture_repo(self) -> tuple[Path, str]:
        repo = self.root / "routine-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        lock = repo / "infra/schauwerk-editor/release-lock.json"
        lock.parent.mkdir(parents=True)
        lock.write_text(json.dumps({
            "schema_version": "weltgewebe-schauwerk-runtime-lock.v1",
            "source_repository": "heimgewebe/schauwerk",
            "source_commit": "c" * 40,
            "image_repository": "ghcr.io/heimgewebe/schauwerk-schaubild",
            "image_digest": IMAGE,
            "public_base_path": "/schaubild",
        }))
        base = self.commit(repo)
        self.data["commit"] = base
        self.data["routine_web_releases"] = True
        self.write()
        return repo, base

    def commit(self, repo: Path) -> str:
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run([
            "git", "-C", str(repo), "-c", "user.name=Fixture",
            "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture",
        ], check=True)
        return subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

    def change(self, repo: Path, path: str, content: str = "test") -> str:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return self.commit(repo)

    def admit(self, repo: Path, target: str) -> dict[str, object]:
        with mock.patch.object(verifier, "verify_local_recovery") as recovery:
            with mock.patch.object(verifier, "verify_main_ci", return_value=123) as ci:
                receipt = verifier.admit_routine_release(
                    repo, target, root=self.root, expected_uid=self.uid,
                )
        recovery.assert_called_once_with(expected_uid=self.uid)
        ci.assert_called_once_with(target)
        return receipt

    def test_standing_grant_is_opt_in_and_exact_receipt_bound(self) -> None:
        repo, base = self.fixture_repo()
        target = self.change(repo, "apps/web/src/routes/commons/+page.svelte")
        raw = self.file.read_bytes()
        with self.assertRaises(verifier.HoldReleaseError):
            self.effective(target)
        receipt = self.admit(repo, target)
        self.assertEqual(self.file.read_bytes(), raw, "operator decision must never be overwritten")
        self.assertEqual(receipt["ci_run_id"], 123)
        self.assertEqual(self.effective(target)["commit"], target)
        self.assertEqual(self.effective(base)["commit"], base)
        self.assertEqual((self.parent / verifier.ROUTINE_FILE).stat().st_mode & 0o777, 0o600)
        with self.assertRaises(verifier.HoldReleaseError):
            self.effective("f" * 40)
        with self.assertRaises(verifier.HoldReleaseError):
            self.effective(target, "sha256:" + "d" * 64)
        self.data["routine_web_releases"] = False
        self.write()
        with self.assertRaises(verifier.HoldReleaseError):
            self.effective(target)
        self.data["routine_web_releases"] = True
        self.data["authorized_by"] = "operator:new-decision"
        self.write()
        with self.assertRaises(verifier.HoldReleaseError):
            self.effective(target)
        self.file.unlink()
        with self.assertRaises(verifier.HoldReleaseError):
            self.effective(target)

    def test_explicit_hold_and_opt_in_invalid_types_deny_routine(self) -> None:
        repo, _ = self.fixture_repo()
        target = self.change(repo, "apps/web/src/test.ts")
        for value in (False, "true", 1, None):
            with self.subTest(value=value):
                self.data["routine_web_releases"] = value
                self.write()
                with self.assertRaises(verifier.HoldReleaseError):
                    verifier.admit_routine_release(repo, target, root=self.root, expected_uid=self.uid)
        self.data["routine_web_releases"] = True
        self.data["decision"] = "hold"
        self.write()
        with self.assertRaises(verifier.HoldReleaseError):
            verifier.admit_routine_release(repo, target, root=self.root, expected_uid=self.uid)
        self.assertFalse((self.parent / verifier.ROUTINE_FILE).exists())

    def test_backend_build_policy_oci_and_symlink_changes_require_exact_grant(self) -> None:
        repo, base = self.fixture_repo()
        for path in ("apps/api/src/main.rs", ".github/workflows/ci.yml",
                     "apps/web/package.json", "scripts/ops/anything.sh",
                     "apps/web/src/lib/auth/store.ts", "apps/web/src/hooks.client.ts",
                     "apps/web/src/lib/components/topBarAuthState.ts",
                     "apps/web/src/lib/components/TopBar.svelte",
                     "infra/schauwerk-editor/release-lock.json"):
            with self.subTest(path=path):
                subprocess.run(["git", "-C", str(repo), "reset", "--hard", base],
                               check=True, capture_output=True)
                commit = self.change(repo, path, "changed")
                with self.assertRaises(verifier.HoldReleaseError):
                    verifier.verify_routine_source(repo, self.data, commit, expected_uid=self.uid)
        subprocess.run(["git", "-C", str(repo), "reset", "--hard", base], check=True, capture_output=True)
        path = repo / "apps/web/src/linked.ts"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to("/etc/passwd")
        commit = self.commit(repo)
        with self.assertRaises(verifier.HoldReleaseError):
            verifier.verify_routine_source(repo, self.data, commit, expected_uid=self.uid)

    def test_routine_rejects_non_descendant_and_rollback_to_sibling(self) -> None:
        repo, base = self.fixture_repo()
        first = self.change(repo, "apps/web/src/one.ts")
        self.admit(repo, first)
        second = self.change(repo, "apps/web/src/two.ts")
        self.admit(repo, second)
        self.assertEqual(self.effective(second)["commit"], second)
        with self.assertRaises(verifier.HoldReleaseError):
            self.effective(first)
        subprocess.run(["git", "-C", str(repo), "reset", "--hard", base], check=True, capture_output=True)
        sibling = self.change(repo, "apps/web/src/sibling.ts")
        with self.assertRaises(verifier.HoldReleaseError):
            self.admit(repo, sibling)
        self.assertEqual(self.effective(second)["commit"], second)

    def test_failed_ci_or_recovery_does_not_publish_receipt(self) -> None:
        repo, _ = self.fixture_repo()
        target = self.change(repo, "apps/web/src/next.ts")
        with mock.patch.object(verifier, "verify_local_recovery", side_effect=verifier.HoldReleaseError("stale")):
            with self.assertRaises(verifier.HoldReleaseError):
                verifier.admit_routine_release(repo, target, root=self.root, expected_uid=self.uid)
        with mock.patch.object(verifier, "verify_local_recovery"):
            with mock.patch.object(verifier, "verify_main_ci", side_effect=verifier.HoldReleaseError("pending")):
                with self.assertRaises(verifier.HoldReleaseError):
                    verifier.admit_routine_release(repo, target, root=self.root, expected_uid=self.uid)
        self.assertFalse((self.parent / verifier.ROUTINE_FILE).exists())

    def test_main_ci_exact_success_and_latest_attempt_only(self) -> None:
        run = {
            "head_sha": COMMIT, "head_branch": "main", "event": "push",
            "path": ".github/workflows/ci.yml",
            "repository": {"full_name": "heimgewebe/commonthing"},
            "head_repository": {"full_name": "heimgewebe/commonthing"},
            "run_number": 1, "run_attempt": 1, "id": 123,
            "status": "completed", "conclusion": "success",
        }
        self.assertEqual(verifier.select_main_ci_run({"workflow_runs": [run]}, COMMIT), 123)
        for changes in (
            {"head_sha": "f" * 40}, {"head_branch": "topic"}, {"event": "pull_request"},
            {"path": ".github/workflows/production-live-contract.yml"},
            {"head_repository": {"full_name": "other/repo"}},
            {"status": "in_progress"}, {"conclusion": "failure"},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(verifier.HoldReleaseError):
                    verifier.select_main_ci_run({"workflow_runs": [{**run, **changes}]}, COMMIT)
        for pending in ({**run, "run_number": 2, "status": "queued"},
                        {**run, "run_attempt": 2, "conclusion": "failure"}):
            with self.assertRaises(verifier.HoldReleaseError):
                verifier.select_main_ci_run({"workflow_runs": [run, pending]}, COMMIT)

    def test_local_recovery_requires_current_real_file_and_restore_record(self) -> None:
        backup = self.root / "backups"
        backup.mkdir()
        (backup / "proofs").mkdir()
        now = datetime(2026, 11, 24, 20, 0, tzinfo=timezone.utc)
        stem = "weltgewebe-postgres-20261124T000000Z"
        file = backup / (stem + ".sql.gz")
        file.write_bytes(b"backup")
        manifest = backup / (stem + ".sha256.manifest")
        proof = backup / "proofs" / (stem + ".restore-proof")
        def write_records(backup_hours: int = 18, restore_hours: int = 48) -> None:
            manifest.write_text(
                "contract=weltgewebe-postgres-backup-v1\n"
                f"created_at={(now-timedelta(hours=backup_hours)).isoformat()}\n"
                f"file={file.name}\nsha256={hashlib.sha256(file.read_bytes()).hexdigest()}\n"
                f"bytes={file.stat().st_size}\n"
            )
            proof.write_text(
                "contract=weltgewebe-postgres-restore-proof-v1\n"
                f"created_at={(now-timedelta(hours=restore_hours)).isoformat()}\n"
                f"backup_file={file.name}\nbackup_sha256={'1'*64}\nresult=ok\n"
            )
        write_records()
        # The encrypted sibling is not a plaintext backup manifest.
        (backup / (stem + ".age.sha256.manifest")).write_text("encrypted sibling")
        verifier.verify_local_recovery(backup, expected_uid=self.uid, now=now)
        for bh, rh in ((27, 48), (18, 193), (-1, 48)):
            write_records(bh, rh)
            with self.assertRaises(verifier.HoldReleaseError):
                verifier.verify_local_recovery(backup, expected_uid=self.uid, now=now)
        write_records()
        file.write_bytes(b"broken")  # same length, different checksum
        with self.assertRaises(verifier.HoldReleaseError):
            verifier.verify_local_recovery(backup, expected_uid=self.uid, now=now)
        file.unlink()
        with self.assertRaises(verifier.HoldReleaseError):
            verifier.verify_local_recovery(backup, expected_uid=self.uid, now=now)


    def test_recovery_selection_uses_publication_order_and_restore_time(self) -> None:
        backup = self.root / "backups"
        backup.mkdir()
        proofs = backup / "proofs"
        proofs.mkdir()
        now = datetime(2026, 11, 25, 4, 0, tzinfo=timezone.utc)

        def publish(label: str, hours: int, sequence: int | None) -> None:
            stem = "weltgewebe-postgres-" + label
            data = backup / (stem + ".sql.gz")
            data.write_bytes(label.encode())
            record = (
                "contract=weltgewebe-postgres-backup-v1\n"
                f"created_at={(now-timedelta(hours=hours)).isoformat()}\n"
                f"file={data.name}\nsha256={hashlib.sha256(data.read_bytes()).hexdigest()}\n"
                f"bytes={data.stat().st_size}\n"
            )
            if sequence is not None:
                record += f"publication_sequence={sequence}\n"
            (backup / (stem + ".sha256.manifest")).write_text(record)

        def restored(label: str, hours: int) -> None:
            stem = "weltgewebe-postgres-" + label
            (proofs / (stem + ".restore-proof")).write_text(
                "contract=weltgewebe-postgres-restore-proof-v1\n"
                f"created_at={(now-timedelta(hours=hours)).isoformat()}\n"
                f"backup_file={stem}.sql.gz\nbackup_sha256={'1'*64}\nresult=ok\n"
            )

        # The previous implementation ignored manual backups and selected proofs
        # by filename; the older z-label must not strand the current deployment.
        publish("20261124T000000Z", 28, 1)
        publish("a-manual", 2, 2)
        publish("z-legacy", 1, None)
        restored("z-manual", 240)
        restored("a-manual", 1)
        verifier.verify_local_recovery(backup, expected_uid=self.uid, now=now)

        # Publication sequence, not apparent freshness, identifies the latest
        # completed dump, even when a clock correction has moved time backwards.
        publish("older-clock", 27, 3)
        with self.assertRaises(verifier.HoldReleaseError):
            verifier.verify_local_recovery(backup, expected_uid=self.uid, now=now)
        publish("older-clock", 1, 3)
        verifier.verify_local_recovery(backup, expected_uid=self.uid, now=now)
        publish("duplicate-sequence", 1, 3)
        with self.assertRaises(verifier.HoldReleaseError):
            verifier.verify_local_recovery(backup, expected_uid=self.uid, now=now)

    def test_recovery_selection_legacy_manifests_use_recorded_time(self) -> None:
        backup = self.root / "backups"
        backup.mkdir()
        proofs = backup / "proofs"
        proofs.mkdir()
        now = datetime(2026, 11, 25, 4, 0, tzinfo=timezone.utc)
        for label, hours in (("z-old", 40), ("a-current", 1)):
            stem = "weltgewebe-postgres-" + label
            data = backup / (stem + ".sql.gz")
            data.write_bytes(b"backup")
            stamp = (now-timedelta(hours=hours)).isoformat()
            sha = hashlib.sha256(data.read_bytes()).hexdigest()
            (backup / (stem + ".sha256.manifest")).write_text(
                "contract=weltgewebe-postgres-backup-v1\n"
                f"created_at={stamp}\nfile={data.name}\nsha256={sha}\nbytes=6\n"
            )
            (proofs / (stem + ".restore-proof")).write_text(
                "contract=weltgewebe-postgres-restore-proof-v1\n"
                f"created_at={stamp}\nbackup_file={data.name}\nbackup_sha256={sha}\nresult=ok\n"
            )
        verifier.verify_local_recovery(backup, expected_uid=self.uid, now=now)

    def test_concurrent_operator_revocation_during_admission_leaves_no_effective_grant(self) -> None:
        repo, _ = self.fixture_repo()
        target = self.change(repo, "apps/web/src/new.ts")
        def revoke(_commit: str) -> int:
            self.data["decision"] = "hold"
            self.write()
            return 123
        with mock.patch.object(verifier, "verify_local_recovery"):
            with mock.patch.object(verifier, "verify_main_ci", side_effect=revoke):
                with self.assertRaises(verifier.HoldReleaseError):
                    verifier.admit_routine_release(repo, target, root=self.root, expected_uid=self.uid)
        self.assertFalse((self.parent / verifier.ROUTINE_FILE).exists())
        with self.assertRaises(verifier.HoldReleaseError):
            self.effective(target)


if __name__ == "__main__":
    unittest.main()
