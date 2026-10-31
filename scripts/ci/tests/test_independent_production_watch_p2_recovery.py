#!/usr/bin/env python3
"""A valid pending RECOVERY may retain unrelated P2 read failures."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[2] / "ops/independent_production_watch.py"
SPEC = importlib.util.spec_from_file_location("watch_p2_recovery_p1", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
watch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watch)
START = datetime(2026, 10, 26, 19, tzinfo=timezone.utc)


def report(n: int, *, active: bool = False, p2: str | None = None) -> dict:
    issues = ([{
        "code": "frontend_cache", "severity": "P1", "actual": "public-cache",
        "expected": "no-store", "component": "frontend", "source": "test",
    }] if active else [])
    if p2 is not None:
        issues.append({
            "code": p2, "severity": "P2", "actual": "HTTP timeout",
            "expected": "independent observation", "component": "probe", "source": "test",
        })
    return {
        "checked_at_utc": (START + timedelta(minutes=n)).isoformat().replace("+00:00", "Z"),
        "status": "ALARM" if active else "MONITOR_DATA_FAILURE" if p2 else "HEALTHY",
        "issues": issues, "commits": {}, "main_observation": None, "latest_schedule": None,
    }


class PendingRecoveryWithUnrelatedP2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="p1-recovery-unrelated-p2-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def journal(self) -> list[dict]:
        path = self.directory / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def pending_p2_recovery(self) -> dict:
        self.assertEqual(
            watch.record(report(0, active=True), self.directory, serialized_poll=True), "ALARM",
        )
        with mock.patch.object(
            watch, "_append_durable_events",
            side_effect=OSError("injected recovery journal append failure"),
        ):
            self.assertEqual(
                watch.record(report(1, p2="unavailable_main"),
                             self.directory, serialized_poll=True),
                "RECOVERY",
            )
        heartbeat = json.loads((self.directory / "heartbeat.json").read_text())
        entries = watch._pending_transition_entries(heartbeat)
        self.assertEqual([entry["event"] for entry in entries], ["RECOVERY"])
        self.assertEqual(entries[0]["payload"]["status"], "MONITOR_DATA_FAILURE")
        self.assertEqual([event["event"] for event in self.journal()], ["ALARM"])
        (self.directory / "state.json").unlink()
        return entries[0]["payload"]

    def test_recovery_with_historical_unrelated_p2_replays_exact_payload(self) -> None:
        original = self.pending_p2_recovery()
        watch.record(report(2), self.directory, serialized_poll=True)
        self.assertEqual([row["event"] for row in self.journal()], ["ALARM", "RECOVERY"])
        self.assertEqual(self.journal()[1], original)
        self.assertEqual(json.loads((self.directory / "state.json").read_text())["severe"], [])
        watch.record(report(3), self.directory, serialized_poll=True)
        self.assertEqual([row["event"] for row in self.journal()], ["ALARM", "RECOVERY"])

    def test_unrelated_p2_can_persist_without_blocking_verified_recovery(self) -> None:
        original = self.pending_p2_recovery()
        watch.record(report(2, p2="unavailable_main"), self.directory, serialized_poll=True)
        self.assertEqual([row["event"] for row in self.journal() if row["event"] in {"ALARM", "RECOVERY"}],
                         ["ALARM", "RECOVERY"])
        self.assertEqual(next(row for row in self.journal() if row["event"] == "RECOVERY"), original)

    def test_frontend_p2_cannot_act_as_reconfirmed_recovery_of_frontend_cache(self) -> None:
        self.pending_p2_recovery()
        self.assertEqual(
            watch.record(report(2, p2="unavailable_frontend"),
                         self.directory, serialized_poll=True),
            "MONITOR_DATA_FAILURE",
        )
        self.assertEqual([row["event"] for row in self.journal()], ["ALARM"])
        self.assertFalse((self.directory / "state.json").exists())

    def test_missing_original_alarm_never_authorizes_p2_recovery(self) -> None:
        self.pending_p2_recovery()
        (self.directory / "events.jsonl").unlink()
        self.assertEqual(
            watch.record(report(2), self.directory, serialized_poll=True),
            "MONITOR_DATA_FAILURE",
        )
        self.assertFalse((self.directory / "state.json").exists())


if __name__ == "__main__":
    unittest.main()
