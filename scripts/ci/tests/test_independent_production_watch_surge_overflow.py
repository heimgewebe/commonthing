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
                        and len(self._history(directory)) == 17
                        and not interrupted):
                    interrupted.append(True)
                    raise KeyboardInterrupt("crash after old prefix fsync")
                return real_write(path, value)

            with patch.object(watch, "write_json", side_effect=abort_before_new_state):
                with self.assertRaises(KeyboardInterrupt):
                    watch.record(self._poll(1, ["a", "b"]), directory,
                                 serialized_poll=True)
            self.assertEqual(interrupted, [True])
            self.assertEqual(len(self._history(directory)), 17)
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


    def test_nineteenth_and_twentieth_transient_alarms_survive_long_journal_outage(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self._initial(directory)
            with patch.object(watch, "_append_durable_events",
                              side_effect=OSError("journal unavailable")):
                for minute, codes in [
                    (1, ["a", "b"]),
                    (2, ["a", "c"]),
                    (3, ["a", "d"]),
                ]:
                    self.assertEqual(
                        watch.record(self._poll(minute, codes), directory,
                                     serialized_poll=True),
                        "MONITOR_DATA_FAILURE",
                    )
                    self.assertEqual(self._history(directory), [])
            watch.record(self._poll(4, ["a"]), directory, serialized_poll=True)
            self.assertEqual(
                watch.record(self._poll(5, []), directory, serialized_poll=True),
                "RECOVERY",
            )
            history = self._history(directory)
            alarms = [event for event in history if event["event"] == "ALARM"]
            self.assertEqual(len(alarms), 20, "19th/20th P1 was discarded")
            for code in ("b", "c", "d"):
                self.assertEqual(sum(
                    any(issue.get("code") == code for issue in entry.get("issues", []))
                    for entry in alarms
                ), 1, f"transient P1 {code} was lost or duplicated")
            self.assertEqual(history[-1]["event"], "RECOVERY")

    def test_after_partial_drain_new_alarm_remains_durable_and_ordered(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self._initial(directory)
            real_append = watch._append_durable_events
            append_calls = []

            def append_then_fail(where, entries, *args, **kwargs):
                append_calls.append(len(entries))
                if len(append_calls) == 1:
                    return real_append(where, entries, *args, **kwargs)
                raise OSError("journal fsync unavailable")

            with patch.object(watch, "_append_durable_events",
                              side_effect=OSError("initial outage")):
                watch.record(self._poll(1, ["a", "b"]), directory,
                             serialized_poll=True)
            with patch.object(watch, "_append_durable_events",
                              side_effect=append_then_fail):
                watch.record(self._poll(2, ["a", "c"]), directory,
                             serialized_poll=True)
            watch.record(self._poll(3, ["a"]), directory, serialized_poll=True)
            watch.record(self._poll(4, []), directory, serialized_poll=True)
            history = self._history(directory)
            alarms = [event for event in history if event["event"] == "ALARM"]
            self.assertEqual(len(alarms), 19)
            for code in ("b", "c"):
                self.assertEqual(sum(
                    any(issue.get("code") == code for issue in event.get("issues", []))
                    for event in alarms
                ), 1)
            self.assertEqual(history[-1]["event"], "RECOVERY")

    def test_overflow_requires_confirmed_p1_alarm(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self._initial(directory)
            healthy = self._poll(1, [])
            malformed = {
                "event": "ALARM",
                "payload": {"event": "ALARM", "status": "HEALTHY",
                            "checked_at_utc": NOW.isoformat(), "issues": []},
            }
            heartbeat = json.loads((directory / "heartbeat.json").read_text())
            heartbeat["pending_surge_overflow"] = [malformed]
            watch.write_json(directory / "heartbeat.json", heartbeat)
            self.assertEqual(watch.record(
                healthy, directory, serialized_poll=True,
            ), "MONITOR_DATA_FAILURE")
            self.assertEqual(self._history(directory), [])
            self.assertEqual(
                json.loads((directory / "state.json").read_text())["severe"],
                [["a", "incident A"]],
            )


    def test_saturation_blocks_confirmation_before_live_evaluation(self):
        from unittest.mock import patch
        import sys

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            entries = self._initial(directory)
            used = sum(len(json.dumps(e, sort_keys=True, separators=(",", ":")).encode())
                       + 1 for e in entries)
            reserve = 256
            with patch.object(watch, "OUTBOX_MAX_BYTES", used + reserve - 1), \
                 patch.object(watch, "OUTBOX_ADMISSION_RESERVE", reserve), \
                 patch.object(watch, "_evaluate_cycle",
                              side_effect=AssertionError("a new P1 would be unspoolable")), \
                 patch.object(sys, "argv", ["watch", "--state-dir", str(directory)]):
                self.assertFalse(watch._observation_admitted(directory))
                self.assertEqual(watch.main(), 2)
            heartbeat = json.loads((directory / "heartbeat.json").read_text())
            self.assertIn(heartbeat["status"], ("ALARM", "MONITOR_DATA_FAILURE"))
            self.assertNotIn("unobserved_new_p1",
                             {i.get("code") for i in heartbeat.get("issues", [])})


    def _repeat_epochs(self, directory, count=10):
        entries = []
        for i in range(count * 2):
            alarm = i % 2 == 0
            entries.append({
                "event": "ALARM" if alarm else "RECOVERY",
                "payload": {
                    "event": "ALARM" if alarm else "RECOVERY",
                    "status": "ALARM" if alarm else "HEALTHY",
                    "checked_at_utc": (NOW + timedelta(seconds=i)).isoformat(),
                    "issues": ([{"code": "frontend_cache", "severity": "P1",
                                 "actual": "bad cache"}] if alarm else []),
                    "sequence": i,
                },
            })
        for name in ("state.json", "heartbeat.json"):
            snapshot = ({
                "severe": [], "uncertain": [],
                "checked_at_utc": (NOW + timedelta(seconds=20)).isoformat(),
            } if name == "state.json" else {
                "status": "HEALTHY", "issues": [],
                "checked_at_utc": (NOW + timedelta(seconds=20)).isoformat(),
            })
            snapshot["pending_transitions"] = entries[:watch.OUTBOX_SURGE_LIMIT]
            snapshot["pending_surge_overflow"] = entries[watch.OUTBOX_SURGE_LIMIT:]
            watch.write_json(directory / name, snapshot)
        return entries

    def test_repeated_p1_code_after_queued_recovery_gets_new_alarm(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self._repeat_epochs(directory)
            with patch.object(watch, "_append_durable_events",
                              side_effect=OSError("journal not writable")):
                self.assertEqual(watch.record(
                    self._poll(1, ["frontend_cache"]), directory,
                    serialized_poll=True,
                ), "MONITOR_DATA_FAILURE")
            queued = watch._pending_transition_entries(json.loads(
                (directory / "heartbeat.json").read_text()))
            self.assertEqual(len(queued), 21)
            watch.record(self._poll(2, []), directory, serialized_poll=True)
            history = self._history(directory)
            self.assertEqual(sum(e["event"] == "ALARM" for e in history), 11)
            self.assertEqual(sum(e["event"] == "RECOVERY" for e in history), 11)

    def test_full_byte_budget_drains_preexisting_fifo_without_new_observation(self):
        import sys

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            entries = self._repeat_epochs(directory)
            used = sum(len(json.dumps(e, sort_keys=True, separators=(",", ":")).encode())
                       + 1 for e in entries)
            reserve = 400
            with patch.object(watch, "OUTBOX_MAX_BYTES", used + reserve - 1), \
                 patch.object(watch, "OUTBOX_ADMISSION_RESERVE", reserve), \
                 patch.object(watch, "_evaluate_cycle",
                              side_effect=AssertionError("no room for fresh P1")), \
                 patch.object(sys, "argv", ["watch", "--state-dir", str(directory)]):
                self.assertFalse(watch._observation_admitted(directory))
                self.assertEqual(watch.main(), 2)
            history = self._history(directory)
            self.assertEqual(len(history), 21)
            self.assertEqual([e["event"] for e in history[:20]],
                             ["ALARM", "RECOVERY"] * 10)
            self.assertEqual(history[-1]["event"], "MONITOR_DATA_FAILURE")
            self.assertEqual(sum(e["event"] == "ALARM" for e in history), 10)
            self.assertTrue(watch._observation_admitted(directory))


    def test_recovery_overflow_rejects_active_p1_payload(self):
        for status, issues in [
            ("ALARM", [{"code": "frontend_cache", "severity": "P1"}]),
            ("MONITOR_DATA_FAILURE",
             [{"code": "frontend_cache", "severity": "P1"}]),
        ]:
            malformed = {
                "event": "RECOVERY",
                "payload": {
                    "event": "RECOVERY", "status": status,
                    "checked_at_utc": NOW.isoformat(), "issues": issues,
                },
            }
            with self.subTest(status=status), self.assertRaises(ValueError):
                watch._pending_transition_entries({
                    "pending_surge_overflow": [malformed],
                })


if __name__ == "__main__":
    unittest.main()
