#!/usr/bin/env python3
"""A reconciled tentative ALARM survives later journal failures without duplicates."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[2] / "ops/independent_production_watch.py"
SPEC = importlib.util.spec_from_file_location("watch_tentative_retry_p1", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
watch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watch)
START = datetime(2026, 10, 26, 18, tzinfo=timezone.utc)


def report(n: int, active: bool) -> dict:
    return {
        "checked_at_utc": (START + timedelta(minutes=n)).isoformat().replace("+00:00", "Z"),
        "status": "ALARM" if active else "HEALTHY",
        "issues": ([{
            "code": "frontend_cache", "severity": "P1", "actual": "public-cache",
            "expected": "no-store", "component": "frontend", "source": "test",
        }] if active else []),
        "commits": {}, "main_observation": None, "latest_schedule": None,
    }


class ProvisionalAlarmJournalRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="p1-tentative-retry-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def journal(self) -> list[dict]:
        path = self.directory / "events.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def provisional_alarm(self) -> dict:
        self.assertIsNone(watch.record(report(0, False), self.directory, serialized_poll=True))
        state = self.directory / "state.json"
        original = Path.read_text
        failed = [False]

        def read_error(path: Path, *args, **kwargs):
            if path == state and not failed[0]:
                failed[0] = True
                raise OSError("injected transient state read failure")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", read_error):
            self.assertEqual(
                watch.record(report(1, True), self.directory, serialized_poll=True),
                "MONITOR_DATA_FAILURE",
            )
        self.assertTrue(failed[0])
        heartbeat = json.loads((self.directory / "heartbeat.json").read_text())
        pending = watch._pending_transition_entries(heartbeat)
        self.assertEqual([x["event"] for x in pending], ["ALARM"])
        self.assertTrue(pending[0]["payload"]["state_read_unverified"])
        return pending[0]["payload"]

    def test_reconciled_p1_replays_original_alarm_after_second_journal_error(self) -> None:
        original = self.provisional_alarm()
        with mock.patch.object(
            watch, "_append_durable_events",
            side_effect=OSError("injected journal append failure"),
        ):
            watch.record(report(2, False), self.directory, serialized_poll=True)
        state = json.loads((self.directory / "state.json").read_text())
        self.assertEqual(state["severe"], [])  # RECOVERY already confirmed in this poll.
        self.assertEqual([x["event"] for x in watch._pending_transition_entries(state)],
                         ["ALARM", "RECOVERY"])
        self.assertEqual(self.journal(), [])
        watch.record(report(3, False), self.directory, serialized_poll=True)
        rows = self.journal()
        self.assertEqual([x["event"] for x in rows], ["ALARM", "RECOVERY"])
        self.assertEqual(rows[0], original)
        self.assertEqual(json.loads((self.directory / "state.json").read_text())["severe"], [])
        watch.record(report(4, False), self.directory, serialized_poll=True)
        self.assertEqual(self.journal(), rows)

    def test_second_poll_reconfirms_active_p1_before_retry(self) -> None:
        original = self.provisional_alarm()
        with mock.patch.object(
            watch, "_append_durable_events",
            side_effect=OSError("injected journal append failure"),
        ):
            watch.record(report(2, True), self.directory, serialized_poll=True)
        watch.record(report(3, True), self.directory, serialized_poll=True)
        self.assertEqual(self.journal(), [original])
        watch.record(report(4, False), self.directory, serialized_poll=True)
        self.assertEqual([x["event"] for x in self.journal()], ["ALARM", "RECOVERY"])


if __name__ == "__main__":
    unittest.main()