#!/usr/bin/env python3
"""Regression: provisional P1 replay must survive committed batches and torn tails."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[2] / "ops/independent_production_watch.py"
SPEC = importlib.util.spec_from_file_location("watch_provisional_journal_regression", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
watch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watch)
START = datetime(2026, 10, 31, 7, tzinfo=timezone.utc)


def report(minute: int, alarm: bool) -> dict:
    return {
        "checked_at_utc": (START + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z"),
        "status": "ALARM" if alarm else "HEALTHY",
        "issues": ([{
            "code": "frontend_cache", "severity": "P1", "actual": "public-cache",
            "expected": "no-store", "component": "frontend", "source": "fixture",
        }] if alarm else []),
        "commits": {}, "main_observation": None, "latest_schedule": None,
    }


class ProvisionalJournalCrashTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="pr1947-provisional-journal-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def provisional_alarm(self) -> dict:
        self.assertIsNone(watch.record(report(0, False), self.directory, serialized_poll=True))
        state_path = self.directory / "state.json"
        real_read = Path.read_text
        failed = []

        def fail_once(path: Path, *args, **kwargs):
            if path == state_path and not failed:
                failed.append(True)
                raise OSError("single transient state read failure")
            return real_read(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", fail_once):
            self.assertEqual(
                watch.record(report(1, True), self.directory, serialized_poll=True),
                "MONITOR_DATA_FAILURE",
            )
        self.assertEqual(failed, [True])
        heartbeat = json.loads((self.directory / "heartbeat.json").read_text())
        pending = watch._pending_transition_entries(heartbeat)
        self.assertEqual([entry["event"] for entry in pending], ["ALARM"])
        self.assertTrue(pending[0]["payload"].get("state_read_unverified"))
        return pending[0]

    def history(self) -> list[dict]:
        path = self.directory / "events.jsonl"
        return ([json.loads(line) for line in path.read_text().splitlines()]
                if path.exists() else [])

    def test_fsynced_alarm_recovery_batch_survives_crash_before_snapshot_clear(self) -> None:
        original_alarm = self.provisional_alarm()
        real_append = watch._append_durable_events
        failed = []

        def append_then_crash(directory: Path, events: list, committed=None):
            real_append(directory, events, committed)
            if any(entry["event"] in {"ALARM", "RECOVERY"} for entry in events) and not failed:
                failed.append(True)
                raise KeyboardInterrupt("crash after both events were fsynced")

        with mock.patch.object(watch, "_append_durable_events", append_then_crash):
            with self.assertRaises(KeyboardInterrupt):
                watch.record(report(2, False), self.directory, serialized_poll=True)
        self.assertEqual(failed, [True])
        old = self.history()
        self.assertEqual([e["event"] for e in old], ["ALARM", "RECOVERY"])
        self.assertEqual(old[0], original_alarm["payload"])
        snapshot = json.loads((self.directory / "heartbeat.json").read_text())
        self.assertEqual([e["event"] for e in watch._pending_transition_entries(snapshot)],
                         ["ALARM", "RECOVERY"])

        result = watch.record(report(3, False), self.directory, serialized_poll=True)
        self.assertNotEqual(result, "MONITOR_DATA_FAILURE")
        self.assertEqual(self.history(), old)
        for name in ("state.json", "heartbeat.json"):
            state = json.loads((self.directory / name).read_text())
            self.assertEqual(watch._pending_transition_entries(state), [])

        watch.record(report(4, True), self.directory, serialized_poll=True)
        watch.record(report(5, False), self.directory, serialized_poll=True)
        history = [e for e in self.history() if e["event"] in {"ALARM", "RECOVERY"}]
        self.assertEqual([e["event"] for e in history],
                         ["ALARM", "RECOVERY", "ALARM", "RECOVERY"])
        self.assertEqual(history[2]["checked_at_utc"], report(4, True)["checked_at_utc"])

    def test_unterminated_journal_tail_repaired_before_provisional_admission(self) -> None:
        first_alarm = self.provisional_alarm()
        journal = self.directory / "events.jsonl"
        with journal.open("ab") as out:
            out.write(b'{"event":"ALARM","broken":')
            out.flush()
            os.fsync(out.fileno())
        self.assertEqual(
            watch.record(report(2, True), self.directory, serialized_poll=True),
            "ALARM",
        )
        self.assertEqual(self.history(), [first_alarm["payload"]])
        self.assertNotIn(b"broken", journal.read_bytes())
        watch.record(report(3, False), self.directory, serialized_poll=True)
        self.assertEqual([e["event"] for e in self.history()], ["ALARM", "RECOVERY"])

    def test_newline_terminated_invalid_journal_is_not_discarded(self) -> None:
        self.provisional_alarm()
        journal = self.directory / "events.jsonl"
        corrupted = b"not-valid-json\n"
        journal.write_bytes(corrupted)
        self.assertEqual(
            watch.record(report(2, True), self.directory, serialized_poll=True),
            "MONITOR_DATA_FAILURE",
        )
        self.assertEqual(journal.read_bytes(), corrupted)
        self.assertEqual(
            watch.record(report(3, False), self.directory, serialized_poll=True),
            "MONITOR_DATA_FAILURE",
        )


if __name__ == "__main__":
    unittest.main()
