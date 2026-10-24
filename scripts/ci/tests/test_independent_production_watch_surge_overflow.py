#!/usr/bin/env python3
"""Crash/replay regression: a new P1 must survive an already full surge queue."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import importlib.util
import json
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / "ops/independent_production_watch.py"
spec = importlib.util.spec_from_file_location("watch_surge_regression", SCRIPT)
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)
NOW = datetime(2026, 10, 24, 16, 0, tzinfo=timezone.utc)


class FullSurgeRegression(unittest.TestCase):
    def _initial(self, directory):
        initial = NOW.isoformat()
        entries = [
            {"event": "ALARM", "payload": {
                "event": "ALARM", "status": "ALARM", "checked_at_utc": initial,
                "issues": [{"code": f"prior_{index}", "severity": "P1"}],
                "sequence": index,
            }}
            for index in range(17)
        ]
        watch.write_json(directory / "state.json", {
            "severe": [["a", "incident A"]], "uncertain": [],
            "checked_at_utc": initial, "pending_transitions": entries,
        })
        watch.write_json(directory / "heartbeat.json", {
            "status": "ALARM", "checked_at_utc": initial,
            "issues": [{"code": "a", "severity": "P1", "actual": "incident A"}],
            "pending_transitions": entries,
        })
        return entries

    def _poll(self, minute, codes):
        return {
            "checked_at_utc": (NOW + timedelta(minutes=minute)).isoformat(),
            "status": "ALARM" if codes else "HEALTHY",
            "issues": [
                {"code": code, "severity": "P1", "actual": f"incident {code}",
                 "component": "test"} for code in codes
            ],
            "commits": {}, "main_observation": None, "latest_schedule": None,
        }

    def _history(self, directory):
        journal = directory / "events.jsonl"
        return ([json.loads(line) for line in journal.read_text().splitlines()]
                if journal.exists() else [])

    def _assert_b_exactly_once(self, history):
        b = [item for item in history if item["event"] == "ALARM"
             and any(i.get("code") == "b" for i in item.get("issues", []))]
        self.assertEqual(len(b), 1, "confirmed transient P1 B was lost or duplicated")

    def test_eighteenth_transient_alarm_is_journaled_before_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self._initial(directory)
            self.assertEqual(watch.record(
                self._poll(1, ["a", "b"]), directory, serialized_poll=True,
            ), "ALARM")
            self.assertEqual(watch.record(
                self._poll(2, ["a"]), directory, serialized_poll=True,
            ), None)
            self.assertEqual(watch.record(
                self._poll(3, []), directory, serialized_poll=True,
            ), "RECOVERY")
            history = self._history(directory)
            self.assertEqual([e["event"] for e in history],
                             ["ALARM"] * 18 + ["RECOVERY"])
            self._assert_b_exactly_once(history)
            self.assertEqual(history[-1]["event"], "RECOVERY")

    def test_crash_after_durable_overflow_anchor_replays_b_without_repoll(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            old = self._initial(directory)
            with patch.object(watch, "_append_durable_events",
                              side_effect=KeyboardInterrupt("simulated abrupt crash")):
                with self.assertRaises(KeyboardInterrupt):
                    watch.record(self._poll(1, ["a", "b"]),
                                 directory, serialized_poll=True)
            heartbeat = json.loads((directory / "heartbeat.json").read_text())
            self.assertEqual(watch._pending_transition_entries(heartbeat)[-1]
                             ["payload"]["issues"][-1]["code"], "b")
            self.assertEqual(len(json.loads((directory / "state.json").read_text())
                                 ["pending_transitions"]), len(old))
            watch.record(self._poll(2, ["a"]), directory, serialized_poll=True)
            self._assert_b_exactly_once(self._history(directory))
            self.assertEqual(len(self._history(directory)), 18)

    def test_failed_drain_preserves_overflow_for_transient_b(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self._initial(directory)
            with patch.object(watch, "_append_durable_events",
                              side_effect=OSError("fsync denied")):
                self.assertEqual(watch.record(
                    self._poll(1, ["a", "b"]), directory, serialized_poll=True,
                ), "MONITOR_DATA_FAILURE")
            heartbeat = json.loads((directory / "heartbeat.json").read_text())
            self.assertEqual(len(watch._pending_transition_entries(heartbeat)), 18)
            watch.record(self._poll(2, ["a"]), directory, serialized_poll=True)
            self._assert_b_exactly_once(self._history(directory))
            self.assertEqual(len(self._history(directory)), 18)


    def test_crash_after_prefix_fsync_before_state_snapshot_replays_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self._initial(directory)
            real_write = watch.write_json
            interrupted = []

            def abort_before_new_state(path, value):
                if (path == directory / "state.json"
                        and len(self._history(directory)) == 2
                        and not interrupted):
                    interrupted.append(True)
                    raise KeyboardInterrupt("crash after old prefix fsync")
                return real_write(path, value)

            with patch.object(watch, "write_json", side_effect=abort_before_new_state):
                with self.assertRaises(KeyboardInterrupt):
                    watch.record(self._poll(1, ["a", "b"]), directory,
                                 serialized_poll=True)
            self.assertEqual(interrupted, [True])
            self.assertEqual(len(self._history(directory)), 2)
            self.assertEqual(len(watch._pending_transition_entries(
                json.loads((directory / "heartbeat.json").read_text()),
            )), 18)

            # B has disappeared, but it was already durably confirmed.
            self.assertIsNotNone(watch.record(
                self._poll(2, ["a"]), directory, serialized_poll=True,
            ))
            self.assertEqual(watch.record(
                self._poll(3, []), directory, serialized_poll=True,
            ), "RECOVERY")
            history = self._history(directory)
            self.assertEqual([entry["event"] for entry in history],
                             ["ALARM"] * 18 + ["RECOVERY"])
            self._assert_b_exactly_once(history)


    def _initial_with_recoveries(self, directory):
        entries = self._initial(directory)
        # A persistently queued chronological chain can contain recoveries
        # between independent incident epochs.
        for index in range(1, len(entries), 2):
            entries[index] = {
                "event": "RECOVERY",
                "payload": {
                    "event": "RECOVERY", "status": "HEALTHY",
                    "checked_at_utc": entries[index]["payload"]["checked_at_utc"],
                    "issues": [], "sequence": index,
                },
            }
        for name in ("state.json", "heartbeat.json"):
            path = directory / name
            data = json.loads(path.read_text())
            data["pending_transitions"] = entries
            watch.write_json(path, data)
        return entries

    def test_recovery_prefix_drain_permits_eighteenth_alarm(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            entries = self._initial_with_recoveries(directory)
            self.assertEqual(watch.record(
                self._poll(1, ["a", "b"]), directory, serialized_poll=True,
            ), "ALARM")
            history = self._history(directory)
            self.assertEqual(len(history), 18)
            self.assertEqual([h["event"] for h in history[:17]],
                             [e["event"] for e in entries])
            self._assert_b_exactly_once(history)
            self.assertEqual(watch.record(
                self._poll(2, []), directory, serialized_poll=True,
            ), "RECOVERY")
            history = self._history(directory)
            self.assertEqual(len(history), 19)
            self.assertEqual(history[-1]["event"], "RECOVERY")

    def test_recovery_prefix_replays_durable_overflow_after_outage(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            entries = self._initial_with_recoveries(directory)
            with patch.object(watch, "_append_durable_events",
                              side_effect=OSError("temporary journal outage")):
                self.assertEqual(watch.record(
                    self._poll(1, ["a", "b"]), directory, serialized_poll=True,
                ), "MONITOR_DATA_FAILURE")
            heartbeat = json.loads((directory / "heartbeat.json").read_text())
            self.assertEqual(len(watch._pending_transition_entries(heartbeat)), 18)
            self.assertEqual(self._history(directory), [])
            self.assertIsNotNone(watch.record(
                self._poll(2, ["a"]), directory, serialized_poll=True,
            ))
            history = self._history(directory)
            self.assertEqual(len(history), 18)
            self.assertEqual([h["event"] for h in history[:17]],
                             [e["event"] for e in entries])
            self._assert_b_exactly_once(history)
            self.assertEqual(watch.record(
                self._poll(3, []), directory, serialized_poll=True,
            ), "RECOVERY")
            self.assertEqual(self._history(directory)[-1]["event"], "RECOVERY")


if __name__ == "__main__":
    unittest.main()
