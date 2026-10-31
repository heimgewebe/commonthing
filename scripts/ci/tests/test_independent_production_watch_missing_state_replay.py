#!/usr/bin/env python3
"""Regression: a durable pending ALARM survives loss of state.json."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import importlib.util
import json
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / "ops/independent_production_watch.py"
spec = importlib.util.spec_from_file_location("watch_missing_state_pending_regression", SCRIPT)
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)
NOW = datetime(2026, 10, 26, 7, 0, tzinfo=timezone.utc)


class MissingStatePendingAlarmRegression(unittest.TestCase):
    @staticmethod
    def report(minute, codes):
        return {
            "checked_at_utc": (NOW + timedelta(minutes=minute)).isoformat(),
            "status": "ALARM" if codes else "HEALTHY",
            "issues": [
                {
                    "code": code,
                    "severity": "P1",
                    "actual": f"confirmed incident {code}",
                    "component": "regression fixture",
                }
                for code in codes
            ],
            "commits": {},
            "main_observation": None,
            "latest_schedule": None,
        }

    def failed_first_alarm(self, directory):
        with patch.object(
            watch, "_append_durable_events", side_effect=OSError("journal unavailable")
        ):
            self.assertEqual(
                watch.record(self.report(0, ["a"]), directory, serialized_poll=True),
                "ALARM",
            )
        heartbeat = json.loads((directory / "heartbeat.json").read_text())
        pending = watch._pending_transition_entries(heartbeat)
        self.assertEqual([entry["event"] for entry in pending], ["ALARM"])
        self.assertEqual([item["code"] for item in pending[0]["payload"]["issues"] if item.get("severity") == "P1"], ["a"])
        self.assertFalse((directory / "events.jsonl").exists())
        (directory / "state.json").unlink()
        return pending[0]

    @staticmethod
    def history(directory):
        path = directory / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_reconfirmed_pending_alarm_is_replayed_once_after_state_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            pending = self.failed_first_alarm(directory)
            # A later poll reconfirms the same P1. The original event payload
            # must be replayed rather than replaced by a newer synthetic ALARM.
            self.assertEqual(
                watch.record(self.report(1, ["a"]), directory, serialized_poll=True),
                "ALARM",
            )
            history = self.history(directory)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0], pending["payload"])
            self.assertEqual([event["event"] for event in history], ["ALARM"])
            rebuilt = json.loads((directory / "state.json").read_text())
            self.assertEqual([x[0] for x in rebuilt["severe"]], ["a"])
            self.assertEqual(watch._pending_transition_entries(rebuilt), [])
            # A genuine later recovery must not repeat or erase that alarm.
            self.assertEqual(
                watch.record(self.report(2, []), directory, serialized_poll=True),
                "RECOVERY",
            )
            self.assertEqual([x["event"] for x in self.history(directory)],
                             ["ALARM", "RECOVERY"])

    def test_missing_state_and_unconfirmed_alarm_remains_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            pending = self.failed_first_alarm(directory)
            result = watch.record(self.report(1, []), directory, serialized_poll=True)
            self.assertEqual(result, "MONITOR_DATA_FAILURE")
            self.assertFalse((directory / "state.json").exists())
            self.assertEqual(self.history(directory), [])
            heartbeat = json.loads((directory / "heartbeat.json").read_text())
            self.assertEqual(watch._pending_transition_entries(heartbeat), [pending])
            self.assertEqual(heartbeat["status"], "ALARM")

    def test_mismatched_pending_alarm_does_not_replay_as_current_incident(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            pending = self.failed_first_alarm(directory)
            # A different P1 cannot validate the missing prior state.
            self.assertEqual(
                watch.record(self.report(1, ["b"]), directory, serialized_poll=True),
                "MONITOR_DATA_FAILURE",
            )
            self.assertFalse((directory / "state.json").exists())
            self.assertEqual(self.history(directory), [])
            heartbeat = json.loads((directory / "heartbeat.json").read_text())
            self.assertEqual(watch._pending_transition_entries(heartbeat), [pending])


    def test_reconfirmed_pending_alarm_replays_after_older_journaled_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.assertEqual(
                watch.record(self.report(-2, ["old"]), directory, serialized_poll=True),
                "ALARM",
            )
            self.assertEqual(
                watch.record(self.report(-1, []), directory, serialized_poll=True),
                "RECOVERY",
            )
            self.assertEqual([e["event"] for e in self.history(directory)],
                             ["ALARM", "RECOVERY"])
            pending = None
            with patch.object(
                watch, "_append_durable_events", side_effect=OSError("second epoch unavailable")
            ):
                watch.record(self.report(0, ["a"]), directory, serialized_poll=True)
            heartbeat = json.loads((directory / "heartbeat.json").read_text())
            pending = watch._pending_transition_entries(heartbeat)[0]
            self.assertEqual(pending["event"], "ALARM")
            (directory / "state.json").unlink()
            # Last journal event belongs to the older, already recovered epoch.
            self.assertEqual(
                watch.record(self.report(1, ["a"]), directory, serialized_poll=True),
                "ALARM",
            )
            history = self.history(directory)
            self.assertEqual([e["event"] for e in history],
                             ["ALARM", "RECOVERY", "ALARM"])
            self.assertEqual(history[-1], pending["payload"])
            self.assertEqual(
                watch.record(self.report(2, []), directory, serialized_poll=True),
                "RECOVERY",
            )
            self.assertEqual([e["event"] for e in self.history(directory)],
                             ["ALARM", "RECOVERY", "ALARM", "RECOVERY"])

    def test_newer_journal_recovery_never_adopts_stale_pending_alarm(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            pending = self.failed_first_alarm(directory)
            newer = {
                "event": "RECOVERY",
                "status": "HEALTHY",
                "checked_at_utc": (NOW + timedelta(minutes=2)).isoformat(),
                "issues": [],
            }
            # Simulate an independent committed later epoch while the older
            # heartbeat outbox persists. A stale ALARM may not be reattached.
            watch._append_durable_events(directory, [newer])
            self.assertEqual(watch._last_journaled_transition(directory), newer)
            self.assertEqual(
                watch.record(self.report(3, ["a"]), directory, serialized_poll=True),
                "MONITOR_DATA_FAILURE",
            )
            self.assertFalse((directory / "state.json").exists())
            self.assertEqual(self.history(directory), [newer])
            heartbeat = json.loads((directory / "heartbeat.json").read_text())
            self.assertEqual(watch._pending_transition_entries(heartbeat), [pending])

    def test_journaled_pending_alarm_after_state_loss_is_not_replayed_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            real_write = watch.write_json
            interrupted = []

            def crash_after_journal_fsync(path, value):
                if (
                    path == directory / "state.json"
                    and (directory / "events.jsonl").exists()
                    and not interrupted
                ):
                    interrupted.append(True)
                    raise KeyboardInterrupt("simulated crash before outbox clear")
                return real_write(path, value)

            with patch.object(watch, "write_json", side_effect=crash_after_journal_fsync):
                with self.assertRaises(KeyboardInterrupt):
                    watch.record(self.report(0, ["a"]), directory, serialized_poll=True)
            self.assertEqual(interrupted, [True])
            heartbeat = json.loads((directory / "heartbeat.json").read_text())
            pending = watch._pending_transition_entries(heartbeat)
            self.assertEqual(len(pending), 1)
            self.assertEqual(self.history(directory), [pending[0]["payload"]])
            (directory / "state.json").unlink()
            # The ALARM exists durably already: reconstruct active state,
            # clear its duplicate outbox, and later allow one genuine recovery.
            self.assertIsNone(
                watch.record(self.report(1, ["a"]), directory, serialized_poll=True)
            )
            self.assertEqual(self.history(directory), [pending[0]["payload"]])
            rebuilt = json.loads((directory / "state.json").read_text())
            self.assertEqual([item[0] for item in rebuilt["severe"]], ["a"])
            self.assertEqual(watch._pending_transition_entries(rebuilt), [])
            self.assertEqual(
                watch.record(self.report(2, []), directory, serialized_poll=True),
                "RECOVERY",
            )
            self.assertEqual([e["event"] for e in self.history(directory)],
                             ["ALARM", "RECOVERY"])

if __name__ == "__main__":
    unittest.main()