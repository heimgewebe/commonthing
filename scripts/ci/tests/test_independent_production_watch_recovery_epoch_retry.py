#!/usr/bin/env python3
"""A confirmed recovery remains a new-incident epoch across a failed state write."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import signal
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[2] / "ops/independent_production_watch.py"
SPEC = importlib.util.spec_from_file_location("watch_recovery_epoch_retry_p1", SOURCE)
assert SPEC is not None and SPEC.loader is not None
watch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watch)
START = datetime(2026, 10, 31, 4, tzinfo=timezone.utc)


def report(minute: int, active: bool) -> dict:
    return {
        "checked_at_utc": (START + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z"),
        "status": "ALARM" if active else "HEALTHY",
        "issues": ([{
            "code": "frontend_cache", "severity": "P1",
            "actual": "public-cache", "expected": "no-store",
            "component": "frontend", "source": "test",
        }] if active else []),
        "commits": {}, "main_observation": None, "latest_schedule": None,
    }


class RecoveryEpochWriteRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="watch-recovery-epoch-retry-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def journal(self) -> list[dict]:
        path = self.directory / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_new_same_code_p1_after_recovery_state_write_failure(self) -> None:
        self.assertEqual(
            watch.record(report(0, True), self.directory, serialized_poll=True), "ALARM"
        )
        state_path = self.directory / "state.json"
        real_write = watch.write_json
        failures = []

        def fail_one_state_write(path: Path, value: dict) -> None:
            if path == state_path and not failures:
                failures.append("state.json")
                raise OSError("injected state write failure after recovery heartbeat")
            real_write(path, value)

        # This is the real _persist_cycle() error fallback, not just record().
        with mock.patch.object(watch, "write_json", side_effect=fail_one_state_write):
            try:
                self.assertEqual(watch._persist_cycle(report(1, False), self.directory), 2)
            finally:
                signal.alarm(0)
        self.assertEqual(failures, ["state.json"])
        stored = json.loads(state_path.read_text())
        pending = watch._pending_transition_entries(stored)
        self.assertEqual([item["event"] for item in pending], ["RECOVERY"])
        self.assertEqual([item[0] for item in stored["severe"]], ["frontend_cache"])
        self.assertEqual(
            pending[0]["payload"]["checked_at_utc"], report(1, False)["checked_at_utc"]
        )

        # New P1 after a verified prior recovery is a NEW episode, even if
        # the fallback copied that RECOVERY into the older state snapshot.
        watch.record(report(2, True), self.directory, serialized_poll=True)
        watch.record(report(3, True), self.directory, serialized_poll=True)
        watch.record(report(4, False), self.directory, serialized_poll=True)
        history = self.journal()
        transitions = [row for row in history if row["event"] in {"ALARM", "RECOVERY"}]
        self.assertEqual([row["event"] for row in transitions],
                         ["ALARM", "RECOVERY", "ALARM", "RECOVERY"])
        self.assertEqual(transitions[0]["checked_at_utc"], report(0, True)["checked_at_utc"])
        self.assertEqual(transitions[1]["checked_at_utc"], report(1, False)["checked_at_utc"])
        self.assertEqual(transitions[2]["checked_at_utc"], report(2, True)["checked_at_utc"])
        self.assertEqual(transitions[3]["checked_at_utc"], report(4, False)["checked_at_utc"])
        self.assertEqual(
            json.loads(state_path.read_text())["severe"], []
        )

    def test_unverified_p1_bearing_recovery_never_resets_old_alarm(self) -> None:
        watch.record(report(0, True), self.directory, serialized_poll=True)
        fake = {"event": "RECOVERY", "payload": {
            "event": "RECOVERY", **report(1, True),
        }}
        state_path = self.directory / "state.json"
        heartbeat_path = self.directory / "heartbeat.json"
        state = json.loads(state_path.read_text())
        heartbeat = json.loads(heartbeat_path.read_text())
        for snapshot in (state, heartbeat):
            snapshot["checked_at_utc"] = report(2, True)["checked_at_utc"]
            snapshot["pending_transitions"] = [fake]
        watch.write_json(state_path, state)
        watch.write_json(heartbeat_path, heartbeat)

        # A malformed RECOVERY carrying active P1 is not a verified epoch.
        watch.record(report(3, True), self.directory, serialized_poll=True)
        self.assertEqual([e["event"] for e in self.journal()], ["ALARM"])

    def test_alarm_payload_immutable_across_journal_and_heartbeat_errors(self) -> None:
        watch.record(report(0, True), self.directory, serialized_poll=True)
        state_path = self.directory / "state.json"
        real_write = watch.write_json
        state_failed = []

        def fail_recovery_state(path: Path, value: dict) -> None:
            if path == state_path and not state_failed:
                state_failed.append(True)
                raise OSError("first recovery state fsync failure")
            real_write(path, value)

        with mock.patch.object(watch, "write_json", side_effect=fail_recovery_state):
            try:
                watch._persist_cycle(report(1, False), self.directory)
            finally:
                signal.alarm(0)
        self.assertEqual(state_failed, [True])

        original_append = watch._append_durable_events
        journal_failed = []
        heartbeat_failed = []

        def fail_one_append(*args, **kwargs):
            if not journal_failed:
                journal_failed.append(True)
                raise OSError("one failed journal append")
            return original_append(*args, **kwargs)

        def fail_post_journal_heartbeat(path: Path, value: dict) -> None:
            p2 = isinstance(value, dict) and any(
                isinstance(issue, dict) and issue.get("code") == "event_log_write_failed"
                for issue in value.get("issues", [])
            )
            if path == self.directory / "heartbeat.json" and p2 and not heartbeat_failed:
                heartbeat_failed.append(True)
                raise OSError("heartbeat write failed after state succeeded")
            real_write(path, value)

        with (
            mock.patch.object(watch, "_append_durable_events", side_effect=fail_one_append),
            mock.patch.object(watch, "write_json", side_effect=fail_post_journal_heartbeat),
        ):
            try:
                watch._persist_cycle(report(2, True), self.directory)
            finally:
                signal.alarm(0)
        self.assertEqual(journal_failed, [True])
        self.assertEqual(heartbeat_failed, [True])

        watch.record(report(3, True), self.directory, serialized_poll=True)
        watch.record(report(4, False), self.directory, serialized_poll=True)
        transitions = [x for x in self.journal() if x["event"] in {"ALARM", "RECOVERY"}]
        self.assertEqual([x["event"] for x in transitions],
                         ["ALARM", "RECOVERY", "ALARM", "RECOVERY"])
        self.assertEqual([x["checked_at_utc"] for x in transitions],
                         [report(0, True)["checked_at_utc"],
                          report(1, False)["checked_at_utc"],
                          report(2, True)["checked_at_utc"],
                          report(4, False)["checked_at_utc"]])

    def test_unbroken_same_code_p1_does_not_create_duplicate_alarm(self) -> None:
        watch.record(report(0, True), self.directory, serialized_poll=True)
        watch.record(report(1, True), self.directory, serialized_poll=True)
        watch.record(report(2, True), self.directory, serialized_poll=True)
        self.assertEqual([x["event"] for x in self.journal()], ["ALARM"])
        watch.record(report(3, False), self.directory, serialized_poll=True)
        self.assertEqual([x["event"] for x in self.journal()], ["ALARM", "RECOVERY"])


if __name__ == "__main__":
    unittest.main()