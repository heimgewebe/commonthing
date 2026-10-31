#!/usr/bin/env python3
"""Crash recovery P1: a confirmed P1 during state read EIO must remain replayable."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[2] / "ops/independent_production_watch.py"
SPEC = importlib.util.spec_from_file_location("watch_transient_read", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
watch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watch)
START = datetime(2026, 10, 26, 17, tzinfo=timezone.utc)


def observation(n: int, *, active: bool) -> dict:
    return {
        "checked_at_utc": (START + timedelta(minutes=n)).isoformat().replace("+00:00", "Z"),
        "status": "ALARM" if active else "HEALTHY",
        "issues": ([{"code": "frontend_cache", "severity": "P1", "actual": "public",
                     "expected": "no-store", "component": "frontend", "source": "fixture"}]
                   if active else []),
        "commits": {}, "main_observation": None, "latest_schedule": None,
    }


class TransientStateReadIncidentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="p1-transient-state-read-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def journal(self) -> list[dict]:
        path = self.directory / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def fail_once_during_p1(self, n: int = 1) -> dict:
        state = self.directory / "state.json"
        original = Path.read_text
        failed = [False]
        def transient_read(path: Path, *args, **kwargs):
            if path == state and not failed[0]:
                failed[0] = True
                raise OSError("injected transient incident read EIO")
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, "read_text", transient_read):
            self.assertEqual(
                watch.record(observation(n, active=True), self.directory, serialized_poll=True),
                "MONITOR_DATA_FAILURE",
            )
        self.assertTrue(failed[0])
        heartbeat = json.loads((self.directory / "heartbeat.json").read_text())
        self.assertEqual(heartbeat["status"], "ALARM")
        return heartbeat

    def test_new_p1_survives_eio_then_healthy_poll_as_alarm_and_recovery(self) -> None:
        self.assertIsNone(watch.record(observation(0, active=False), self.directory, serialized_poll=True))
        hb = self.fail_once_during_p1()
        self.assertIn("frontend_cache", {i["code"] for i in hb["issues"]})
        watch.record(observation(2, active=False), self.directory, serialized_poll=True)
        self.assertEqual([row["event"] for row in self.journal()], ["ALARM", "RECOVERY"])
        self.assertEqual(self.journal()[0]["checked_at_utc"], observation(1, active=True)["checked_at_utc"])
        self.assertEqual(json.loads((self.directory / "state.json").read_text())["severe"], [])
        watch.record(observation(3, active=False), self.directory, serialized_poll=True)
        self.assertEqual([row["event"] for row in self.journal()], ["ALARM", "RECOVERY"])

    def test_reconfirmed_p1_after_eio_has_one_alarm_no_duplicate(self) -> None:
        self.assertIsNone(watch.record(observation(0, active=False), self.directory, serialized_poll=True))
        self.fail_once_during_p1()
        watch.record(observation(2, active=True), self.directory, serialized_poll=True)
        self.assertEqual([row["event"] for row in self.journal()], ["ALARM"])
        watch.record(observation(3, active=False), self.directory, serialized_poll=True)
        self.assertEqual([row["event"] for row in self.journal()], ["ALARM", "RECOVERY"])

    def test_existing_active_p1_does_not_get_duplicate_alarm(self) -> None:
        self.assertEqual(watch.record(observation(0, active=True), self.directory, serialized_poll=True), "ALARM")
        self.fail_once_during_p1()
        watch.record(observation(2, active=False), self.directory, serialized_poll=True)
        self.assertEqual([row["event"] for row in self.journal()], ["ALARM", "RECOVERY"])


if __name__ == "__main__":
    unittest.main()
