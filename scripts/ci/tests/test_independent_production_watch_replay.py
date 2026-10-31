#!/usr/bin/env python3
"""Crash-state P1 tests: reconfirmed pending ALARM must not strand without state."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[2] / "ops/independent_production_watch.py"
SPEC = importlib.util.spec_from_file_location("watch_replay", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
watch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watch)
START = datetime(2026, 10, 25, 13, 0, tzinfo=timezone.utc)


def report(moment: datetime, *, active: bool) -> dict:
    return {
        "checked_at_utc": moment.isoformat().replace("+00:00", "Z"),
        "status": "ALARM" if active else "HEALTHY",
        "issues": [{
            "code": "frontend_cache", "severity": "P1",
            "actual": "public-cache", "expected": "no-store",
            "component": "frontend", "source": "live fixture",
        }] if active else [],
        "commits": {}, "main_observation": None, "latest_schedule": None,
    }


class MissingStatePendingAlarmTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="watch-replay-p1-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def create_pending_alarm(self) -> dict:
        first = report(START, active=True)
        with mock.patch.object(
            watch, "_append_durable_events",
            side_effect=OSError("synthetic journal fsync failure"),
        ):
            self.assertEqual(
                watch.record(first, self.directory, serialized_poll=True),
                "ALARM",
            )
        heartbeat = json.loads(
            (self.directory / "heartbeat.json").read_text(encoding="utf-8")
        )
        self.assertEqual(heartbeat["status"], "ALARM")
        self.assertEqual(len(watch._pending_transition_entries(heartbeat)), 1)
        self.assertEqual(
            watch._pending_transition_entries(heartbeat)[0]["event"], "ALARM",
        )
        self.assertFalse((self.directory / "events.jsonl").exists())
        (self.directory / "state.json").unlink()
        return heartbeat

    def journal(self) -> list[dict]:
        path = self.directory / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_reconfirmed_pending_alarm_replays_exact_original_once_after_state_loss(self):
        heartbeat = self.create_pending_alarm()
        original = watch._pending_transition_entries(heartbeat)[0]["payload"]
        reconfirmed = report(START + timedelta(minutes=1), active=True)
        self.assertEqual(
            watch.record(reconfirmed, self.directory, serialized_poll=True), "ALARM",
        )
        self.assertEqual(self.journal(), [original])
        rebuilt = json.loads((self.directory / "state.json").read_text())
        self.assertEqual([x[0] for x in rebuilt["severe"]], ["frontend_cache"])
        self.assertEqual(watch._pending_transition_entries(rebuilt), [])
        self.assertEqual(
            watch.record(
                report(START + timedelta(minutes=2), active=True),
                self.directory, serialized_poll=True,
            ),
            None,
        )
        self.assertEqual(self.journal(), [original])
        self.assertEqual(
            watch.record(
                report(START + timedelta(minutes=3), active=False),
                self.directory, serialized_poll=True,
            ),
            "RECOVERY",
        )
        self.assertEqual([x["event"] for x in self.journal()], ["ALARM", "RECOVERY"])

    def test_unconfirmed_pending_alarm_never_becomes_synthetic_recovery(self):
        self.create_pending_alarm()
        self.assertEqual(
            watch.record(
                report(START + timedelta(minutes=1), active=False),
                self.directory, serialized_poll=True,
            ),
            "MONITOR_DATA_FAILURE",
        )
        self.assertFalse((self.directory / "state.json").exists())
        self.assertEqual(self.journal(), [])
        heartbeat = json.loads((self.directory / "heartbeat.json").read_text())
        self.assertEqual(heartbeat["status"], "ALARM")
        self.assertEqual(len(watch._pending_transition_entries(heartbeat)), 1)

    def test_mixed_recovery_pending_remains_unreplayable_without_state(self):
        heartbeat = self.create_pending_alarm()
        recovery = report(START + timedelta(seconds=30), active=False)
        heartbeat["pending_transitions"].append({
            "event": "RECOVERY",
            "payload": {"event": "RECOVERY", **recovery},
        })
        watch.write_json(self.directory / "heartbeat.json", heartbeat)
        self.assertEqual(
            watch.record(
                report(START + timedelta(minutes=1), active=True),
                self.directory, serialized_poll=True,
            ),
            "MONITOR_DATA_FAILURE",
        )
        self.assertFalse((self.directory / "state.json").exists())
        self.assertEqual(self.journal(), [])


if __name__ == "__main__":
    unittest.main()