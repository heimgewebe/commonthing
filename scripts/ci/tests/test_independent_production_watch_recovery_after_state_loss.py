#!/usr/bin/env python3
"""Crash recovery P1: a durable pending RECOVERY must survive missing state.json."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[2] / "ops/independent_production_watch.py"
SPEC = importlib.util.spec_from_file_location("watch_recovery_lost_state", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
watch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watch)
START = datetime(2026, 10, 26, 17, tzinfo=timezone.utc)


def observation(n: int, *, active: bool, unverifiable: bool = False) -> dict:
    issues = ([{
        "code": "frontend_cache", "severity": "P1", "actual": "public-cache",
        "expected": "no-store", "component": "frontend", "source": "fixture",
    }] if active else [])
    if unverifiable:
        issues.append({
            "code": "unavailable_frontend", "severity": "P2",
            "actual": "timeout", "expected": "confirmed healthy", "component": "frontend",
            "source": "fixture",
        })
    return {
        "checked_at_utc": (START + timedelta(minutes=n)).isoformat().replace("+00:00", "Z"),
        "status": "ALARM" if active else "MONITOR_DATA_FAILURE" if unverifiable else "HEALTHY",
        "issues": issues, "commits": {}, "main_observation": None, "latest_schedule": None,
    }


class PendingRecoveryAfterLostStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="p1-recovery-lost-state-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def journal(self) -> list[dict]:
        path = self.directory / "events.jsonl"
        return [json.loads(row) for row in path.read_text().splitlines()] if path.exists() else []

    def pending_recovery_after_lost_state(self) -> dict:
        self.assertEqual(watch.record(observation(0, active=True), self.directory, serialized_poll=True), "ALARM")
        with mock.patch.object(watch, "_append_durable_events", side_effect=OSError("simulated journal fsync failure")):
            self.assertEqual(watch.record(observation(1, active=False), self.directory, serialized_poll=True), "RECOVERY")
        heartbeat = json.loads((self.directory / "heartbeat.json").read_text())
        entries = watch._pending_transition_entries(heartbeat)
        self.assertEqual([e["event"] for e in entries], ["RECOVERY"])
        self.assertEqual([r["event"] for r in self.journal()], ["ALARM"])
        (self.directory / "state.json").unlink()
        return entries[0]["payload"]

    def test_recovery_intent_replayed_exactly_once_after_lost_state(self) -> None:
        original = self.pending_recovery_after_lost_state()
        watch.record(observation(2, active=False), self.directory, serialized_poll=True)
        journal = self.journal()
        self.assertEqual([r["event"] for r in journal], ["ALARM", "RECOVERY"])
        self.assertEqual(journal[1], original)
        self.assertTrue((self.directory / "state.json").exists())
        state = json.loads((self.directory / "state.json").read_text())
        self.assertEqual(state["severe"], [])
        self.assertEqual(watch._pending_transition_entries(state), [])
        watch.record(observation(3, active=False), self.directory, serialized_poll=True)
        self.assertEqual(self.journal(), journal)

    def test_recovery_intent_rejected_if_last_journal_alarm_is_newer(self) -> None:
        original = self.pending_recovery_after_lost_state()
        bad_alarm = observation(4, active=True)
        bad_alarm = {"event": "ALARM", **bad_alarm}
        with (self.directory / "events.jsonl").open("a") as output:
            output.write(json.dumps(bad_alarm, sort_keys=True, separators=(",", ":")) + "\n")
            output.flush()
        self.assertEqual(
            watch.record(observation(5, active=False), self.directory, serialized_poll=True),
            "MONITOR_DATA_FAILURE",
        )
        self.assertFalse((self.directory / "state.json").exists())
        self.assertNotIn(original, self.journal())

    def test_recovery_intent_not_replayed_from_uncertain_product_observation(self) -> None:
        original = self.pending_recovery_after_lost_state()
        self.assertEqual(
            watch.record(observation(2, active=False, unverifiable=True),
                         self.directory, serialized_poll=True),
            "MONITOR_DATA_FAILURE",
        )
        self.assertEqual([row["event"] for row in self.journal()], ["ALARM"])
        self.assertFalse((self.directory / "state.json").exists())
        heartbeat = json.loads((self.directory / "heartbeat.json").read_text())
        self.assertEqual([r["event"] for r in watch._pending_transition_entries(heartbeat)], ["RECOVERY"])


if __name__ == "__main__":
    unittest.main()
