#!/usr/bin/env python3
"""Real cron admission tests for corrupt watcher incident state."""
from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[2] / "ops/independent_production_watch.py"
SPEC = importlib.util.spec_from_file_location("independent_watch_admission", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
watch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watch)


class WatchCorruptedStateAdmissionTests(unittest.TestCase):
    def test_corrupt_state_still_observes_confirmed_product_p1(self):
        for corrupt in (b"{malformed", b"\xff\xfe"):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                (directory / "state.json").write_bytes(corrupt)
                (directory / "heartbeat.json").write_text(
                    json.dumps({
                        "status": "HEALTHY",
                        "checked_at_utc": "2026-10-25T10:00:00Z",
                        "issues": [],
                    }),
                    encoding="utf-8",
                )
                report = {
                    "checked_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "status": "ALARM",
                    "issues": [{
                        "code": "frontend_cache", "severity": "P1",
                        "actual": "public", "expected": "private, no-store",
                        "source": "live frontend", "component": "frontend",
                    }],
                    "commits": {}, "main_observation": None,
                    "latest_schedule": None,
                }
                with (
                    mock.patch.object(sys, "argv", [
                        "independent_production_watch.py", "--state-dir", tmp,
                    ]),
                    mock.patch.object(signal, "alarm", return_value=0),
                    mock.patch.object(watch, "_evaluate_cycle", return_value=report) as observed,
                ):
                    self.assertEqual(watch.main(), 2)
                observed.assert_called_once_with(directory)
                self.assertEqual((directory / "state.json").read_bytes(), corrupt)
                self.assertFalse((directory / "events.jsonl").exists())
                heartbeat = json.loads((directory / "heartbeat.json").read_text())
                self.assertEqual(heartbeat["status"], "ALARM")
                self.assertEqual(heartbeat["observation_status"], "ALARM")
                self.assertIn("frontend_cache", {
                    item["code"] for item in heartbeat["issues"]
                })
                self.assertIn("unverifiable_state", {
                    item["code"] for item in heartbeat["issues"]
                })

    def test_unreadable_heartbeat_still_blocks_pre_poll_capacity_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "state.json").write_bytes(b"{malformed")
            (directory / "heartbeat.json").write_bytes(b"\xff")
            self.assertFalse(watch._observation_admitted(directory))


if __name__ == "__main__":
    unittest.main()