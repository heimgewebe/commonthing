#!/usr/bin/env python3
"""Pure-fixture and transition tests for the off-GitHub observation checker."""
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / "ops/independent_production_watch.py"
spec = importlib.util.spec_from_file_location("independent_watch", SCRIPT)
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)
NOW = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)
MAIN = "a" * 40
OLD = "b" * 40


class WatchTests(unittest.TestCase):

    def test_crash_surge_capacity_fails_closed_without_corrupting_old_outbox(self):
        import json

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            stamp = NOW.isoformat().replace("+00:00", "Z")
            entries = []
            for index in range(17):
                payload = {
                    "event": "ALARM", "status": "ALARM",
                    "checked_at_utc": stamp, "issues": [
                        {"code": f"old_{index}", "severity": "P1"}
                    ], "sequence": index,
                }
                entries.append({"event": "ALARM", "payload": payload})
            for name in ("state.json", "heartbeat.json"):
                anchor = ({"severe": [], "uncertain": []} if name == "state.json"
                          else {"status": "HEALTHY", "issues": []})
                anchor["checked_at_utc"] = stamp
                anchor["pending_transitions"] = entries
                (directory / name).write_text(json.dumps(anchor), encoding="utf-8")

            fresh = {
                "checked_at_utc": (NOW + timedelta(minutes=1)).isoformat(),
                "status": "ALARM", "issues": [
                    {"code": "new_p1", "actual": "new failure",
                     "severity": "P1", "component": "test"}
                ], "commits": {}, "main_observation": None,
                "latest_schedule": None,
            }
            self.assertEqual(watch.record(fresh, directory, serialized_poll=True),
                             "MONITOR_DATA_FAILURE")
            unchanged = json.loads((directory / "state.json").read_text())
            self.assertEqual(unchanged["severe"], [])
            self.assertEqual(len(unchanged["pending_transitions"]), 17)
            heartbeat = json.loads((directory / "heartbeat.json").read_text())
            self.assertEqual(heartbeat["status"], "ALARM")
            self.assertEqual(len(heartbeat["pending_transitions"]), 17)
            self.assertIn("new_p1",
                          {row["code"] for row in heartbeat["issues"]})
            self.assertIn("unverifiable_state",
                          {row["code"] for row in heartbeat["issues"]})
            self.assertFalse((directory / "events.jsonl").exists())


    def test_missing_state_retains_active_heartbeat_p1_when_endpoint_unverifiable(self):
        """A missing state.json is not proof that a confirmed incident ended."""
        import json

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            first = watch.evaluate(NOW, self.fixtures(cache="public, max-age=30"))
            self.assertEqual(watch.record(first, directory), "ALARM")
            self.assertEqual(json.loads((directory / "heartbeat.json").read_text())
                             ["status"], "ALARM")
            (directory / "state.json").unlink()

            normal = self.fixtures()
            def missing_frontend(name):
                if name == "frontend":
                    raise TimeoutError("frontend observation unavailable")
                return normal(name)

            uncertain = watch.evaluate(NOW + timedelta(minutes=1),
                                       missing_frontend)
            self.assertEqual(uncertain["status"], "MONITOR_DATA_FAILURE")
            self.assertEqual(watch.record(uncertain, directory),
                             "MONITOR_DATA_FAILURE")
            heartbeat = json.loads((directory / "heartbeat.json").read_text())
            self.assertEqual(heartbeat["status"], "ALARM")
            self.assertIn("frontend_cache",
                          {item["code"] for item in heartbeat["issues"]})
            self.assertIn("unverifiable_state",
                          {item["code"] for item in heartbeat["issues"]})
            self.assertFalse((directory / "state.json").exists())


    def test_crash_during_full_outbox_drain_keeps_seventeenth_alarm(self):
        """The newly confirmed P1 must be durable before draining old events."""
        import json
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            stamp = NOW.isoformat().replace("+00:00", "Z")
            backlog = []
            for index in range(16):
                payload = {
                    "event": "ALARM",
                    "status": "ALARM",
                    "checked_at_utc": stamp,
                    "issues": [{"code": f"prior_{index}", "severity": "P1",
                                "actual": "prior"}],
                    "sequence": index,
                }
                backlog.append({"event": "ALARM", "payload": payload})
            (directory / "state.json").write_text(json.dumps({
                "severe": [], "uncertain": [], "checked_at_utc": stamp,
                "pending_transitions": backlog,
            }), encoding="utf-8")
            (directory / "heartbeat.json").write_text(json.dumps({
                "status": "HEALTHY", "issues": [], "checked_at_utc": stamp,
                "pending_transitions": backlog,
            }), encoding="utf-8")

            def observation():
                return {
                    "checked_at_utc": (NOW + timedelta(minutes=1)).isoformat(),
                    "status": "ALARM", "commits": {}, "main_observation": None,
                    "latest_schedule": None,
                    "issues": [{"code": "frontend_cache", "component": "frontend",
                                "actual": "missing no-store", "expected": "no-store",
                                "source": "fixture", "severity": "P1"}],
                }

            # Model sudden process death exactly at the first journal drain:
            # a normal caught OSError would not reproduce the crash window.
            with patch.object(watch, "_append_durable_events",
                              side_effect=SystemExit("simulated power loss")):
                with self.assertRaises(SystemExit):
                    watch.record(observation(), directory, serialized_poll=True)
            durable = json.loads((directory / "state.json").read_text())
            self.assertEqual(len(durable["pending_transitions"]), 17)
            self.assertEqual(durable["severe"], [["frontend_cache", "missing no-store"]])
            heartbeat = json.loads((directory / "heartbeat.json").read_text())
            self.assertEqual(len(heartbeat["pending_transitions"]), 17)

            # Once storage works, all 16 older events precede the P1; nothing
            # is lost or duplicated despite the previous hard process crash.
            watch.record(observation(), directory, serialized_poll=True)
            journal = [json.loads(line) for line in
                       (directory / "events.jsonl").read_text().splitlines()]
            self.assertEqual(len(journal), 17)
            self.assertEqual([row["sequence"] for row in journal[:16]],
                             list(range(16)))
            self.assertEqual(journal[-1]["event"], "ALARM")
            self.assertEqual(journal[-1]["issues"][0]["code"], "frontend_cache")
            self.assertNotIn("pending_transitions",
                             json.loads((directory / "state.json").read_text()))

    def fixtures(self, *, frontend=MAIN, api=MAIN, main=MAIN,
                 commit_age=5, schedule_age=5, cache="public, no-store",
                 schedule_event="schedule", status="completed", conclusion="success"):
        sources = {
            "frontend": ({"commit": frontend}, cache),
            "api": ({"commit": api}, ""),
            "main": ({"sha": main, "commit": {"committer": {"date":
                (NOW - timedelta(minutes=commit_age)).isoformat()}}}, ""),
            "schedule": ({"workflow_runs": [{"id": 1, "event": schedule_event,
                "created_at": (NOW - timedelta(minutes=schedule_age)).isoformat(),
                "status": status, "conclusion": conclusion}]}, ""),
        }
        return lambda name: sources[name]

    def codes(self, **kwargs):
        return {issue["code"] for issue in
                watch.evaluate(NOW, self.fixtures(**kwargs))["issues"]}

    def test_healthy(self):
        result = watch.evaluate(NOW, self.fixtures())
        self.assertEqual(result["status"], "HEALTHY")
        self.assertFalse(result["issues"])

    def test_schedule_stale_when_more_than_45m(self):
        delayed = watch.evaluate(NOW, self.fixtures(schedule_age=46))
        self.assertEqual(delayed["status"], "HEALTHY")
        self.assertEqual(
            [item["severity"] for item in delayed["issues"]
             if item["code"] == "schedule_stale"], ["INFO"])
        self.assertIn("schedule_stale", self.codes(schedule_age=46))
        self.assertNotIn("schedule_stale", self.codes(schedule_age=44))

    def test_only_actual_schedule_counts(self):
        result = watch.evaluate(NOW, self.fixtures(schedule_event="workflow_dispatch"))
        self.assertEqual(result["status"], "HEALTHY")
        self.assertEqual([(i["code"], i["severity"]) for i in result["issues"]],
                         [("no_schedule", "INFO")])

    def test_main_convergence_grace(self):
        observed = {
            "sha": MAIN,
            "first_seen": (NOW - timedelta(minutes=44)).isoformat(),
        }
        recent = watch.evaluate(
            NOW, self.fixtures(frontend=OLD, api=OLD, commit_age=900),
            previous_main_observation=observed,
        )
        self.assertNotIn("stale_frontend", {i["code"] for i in recent["issues"]})
        observed["first_seen"] = (NOW - timedelta(minutes=46)).isoformat()
        stale = watch.evaluate(
            NOW, self.fixtures(frontend=OLD, api=OLD, commit_age=1),
            previous_main_observation=observed,
        )
        self.assertIn("stale_frontend", {i["code"] for i in stale["issues"]})

    def test_frontend_api_and_cache(self):
        self.assertIn("frontend_api_diverge", self.codes(frontend=OLD, api=MAIN))
        self.assertIn("frontend_cache", self.codes(cache="public, max-age=30"))
        self.assertIn("invalid_commit_api", self.codes(api="short"))

    def test_schedule_failure_and_transport_failure_are_distinct(self):
        schedule_failure = watch.evaluate(NOW, self.fixtures(conclusion="failure"))
        self.assertEqual(schedule_failure["status"], "HEALTHY")
        self.assertEqual([(i["code"], i["severity"]) for i in schedule_failure["issues"]],
                         [("schedule_failed", "INFO")])
        def reader(name):
            if name == "api":
                raise ValueError("two reads failed: timeout")
            return self.fixtures()(name)
        result = watch.evaluate(NOW, reader)
        self.assertEqual(result["status"], "MONITOR_DATA_FAILURE")
        self.assertEqual([i["code"] for i in result["issues"]], ["unavailable_api"])

    def test_new_schedule_in_progress_cannot_hide_last_completed_failure(self):
        base_reader = self.fixtures()
        def reader(name):
            if name != "schedule":
                return base_reader(name)
            return ({
                "workflow_runs": [
                    {"id": 2, "event": "schedule", "created_at": NOW.isoformat(),
                     "status": "in_progress", "conclusion": None},
                    {"id": 1, "event": "schedule",
                     "created_at": (NOW - timedelta(minutes=5)).isoformat(),
                     "status": "completed", "conclusion": "failure"},
                ]
            }, "")
        report = watch.evaluate(NOW, reader)
        self.assertEqual(report["status"], "HEALTHY")
        self.assertEqual(report["latest_schedule"]["status"], "in_progress")
        self.assertEqual(
            [(i["code"], i["severity"]) for i in report["issues"]],
            [("schedule_failed", "INFO")],
        )

    def test_schedule_transport_failure_does_not_claim_production_failure(self):
        def unreadable_schedule(name):
            if name == "schedule":
                raise ValueError("GitHub schedule API unavailable")
            return self.fixtures()(name)
        result = watch.evaluate(NOW, unreadable_schedule)
        self.assertEqual(result["status"], "HEALTHY")
        self.assertEqual([(i["code"], i["severity"]) for i in result["issues"]],
                         [("unavailable_schedule", "INFO")])

    def test_schedule_legacy_alarm_state_is_silently_migrated(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "state.json").write_text(
                json.dumps({"severe": [["schedule_stale", "2026-10-09T13:00:00Z"]],
                            "uncertain": []}), encoding="utf-8")
            info_only = watch.evaluate(NOW, self.fixtures(schedule_age=90))
            self.assertEqual(info_only["status"], "HEALTHY")
            self.assertIsNone(watch.record(info_only, directory))
            self.assertEqual(json.loads((directory / "state.json").read_text())["severe"], [])
            self.assertFalse((directory / "events.jsonl").exists())

    def test_production_alarm_remains_actionable_even_with_schedule_gap(self):
        result = watch.evaluate(NOW, self.fixtures(frontend=OLD, schedule_age=90))
        self.assertEqual(result["status"], "ALARM")
        self.assertEqual({i["code"]: i["severity"] for i in result["issues"]},
                         {"frontend_api_diverge": "P1", "schedule_stale": "INFO"})

    def test_invalid_main_sha_is_monitor_uncertainty_not_production_alarm(self):
        result = watch.evaluate(NOW, self.fixtures(main="not-a-sha"))
        self.assertEqual(result["status"], "MONITOR_DATA_FAILURE")
        self.assertEqual(
            [(issue["code"], issue["severity"]) for issue in result["issues"]],
            [("invalid_commit_main", "P2")],
        )
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(watch.record(result, Path(tmp)), "MONITOR_DATA_FAILURE")
            import json
            self.assertEqual(json.loads((Path(tmp) / "state.json").read_text())["severe"], [])

    def test_main_advance_does_not_duplicate_same_frontend_api_divergence(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            first = watch.evaluate(NOW, self.fixtures(frontend=OLD))
            self.assertEqual(watch.record(first, directory), "ALARM")
            after_main = watch.evaluate(
                NOW + timedelta(minutes=1),
                self.fixtures(frontend=OLD, main="c" * 40),
            )
            self.assertEqual(watch.record(after_main, directory), None)
            self.assertEqual(len((directory / "events.jsonl").read_text().splitlines()), 1)

    def test_convergence_grace_starts_with_observed_main_ref_not_commit_date(self):
        # A newly pushed old commit is not necessarily a 46-minute-old branch update.
        first = watch.evaluate(NOW, self.fixtures(frontend=OLD, api=OLD, commit_age=900))
        self.assertFalse(
            any(issue["code"].startswith("stale_") for issue in first["issues"])
        )
        observed = {
            "sha": MAIN,
            "first_seen": (NOW - timedelta(minutes=46)).isoformat(),
        }
        overdue = watch.evaluate(
            NOW, self.fixtures(frontend=OLD, api=OLD, commit_age=2),
            previous_main_observation=observed,
        )
        self.assertIn("stale_frontend", {i["code"] for i in overdue["issues"]})
        self.assertIn("stale_api", {i["code"] for i in overdue["issues"]})
        fresh_main = watch.evaluate(
            NOW, self.fixtures(frontend=OLD, api=OLD, main="c" * 40,
                               commit_age=900),
            previous_main_observation=observed,
        )
        self.assertNotIn("stale_frontend", {i["code"] for i in fresh_main["issues"]})

    def test_main_observation_persists_across_transient_reference_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            first = watch.evaluate(NOW, self.fixtures())
            watch.record(first, directory)
            seen = watch.read_previous_main_observation(directory)
            self.assertEqual(seen["sha"], MAIN)
            self.assertEqual(seen["first_seen"], NOW.isoformat().replace("+00:00", "Z"))
            def missing_main(name):
                if name == "main":
                    raise ValueError("upstream unavailable")
                return self.fixtures()(name)
            lost = watch.evaluate(
                NOW + timedelta(minutes=4), missing_main,
                previous_main_observation=seen,
            )
            watch.record(lost, directory)
            self.assertEqual(watch.read_previous_main_observation(directory), seen)
            overdue = watch.evaluate(
                NOW + timedelta(minutes=46),
                self.fixtures(frontend=OLD, api=OLD, commit_age=1),
                previous_main_observation=watch.read_previous_main_observation(directory),
            )
            self.assertIn("stale_frontend", {i["code"] for i in overdue["issues"]})

    def test_new_main_grace_does_not_recover_existing_stale_incident(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            prior_observation = {
                "sha": MAIN,
                "first_seen": (NOW - timedelta(minutes=46)).isoformat(),
            }
            overdue = watch.evaluate(
                NOW, self.fixtures(frontend=OLD, api=OLD),
                previous_main_observation=prior_observation,
            )
            self.assertEqual(watch.record(overdue, directory), "ALARM")
            former = json.loads((directory / "state.json").read_text())["severe"]
            self.assertEqual({entry[0] for entry in former}, {"stale_frontend", "stale_api"})

            new_main = "c" * 40
            new_grace = watch.evaluate(
                NOW + timedelta(minutes=1),
                self.fixtures(frontend=OLD, api=OLD, main=new_main),
                previous_main_observation=prior_observation,
            )
            self.assertNotIn("stale_frontend", [i["code"] for i in new_grace["issues"]])
            self.assertIsNone(watch.record(new_grace, directory))
            self.assertEqual(json.loads((directory / "state.json").read_text())["severe"], former)
            heartbeat = json.loads((directory / "heartbeat.json").read_text())
            self.assertEqual(heartbeat["status"], "ALARM")
            self.assertEqual(heartbeat["observation_status"], "HEALTHY")
            self.assertEqual(heartbeat["retained_prior_p1"], former)
            self.assertEqual(len((directory / "events.jsonl").read_text().splitlines()), 1)

            converged = watch.evaluate(
                NOW + timedelta(minutes=2),
                self.fixtures(frontend=new_main, api=new_main, main=new_main),
                previous_main_observation=watch.read_previous_main_observation(directory),
            )
            self.assertEqual(watch.record(converged, directory), "RECOVERY")
            self.assertEqual(json.loads((directory / "state.json").read_text())["severe"], [])

    def test_malformed_nested_main_metadata_is_monitor_uncertainty(self):
        import json
        for nested in (None, [], {"committer": None}, {"committer": []},
                       {"committer": {"date": []}}):
            with self.subTest(nested=nested), tempfile.TemporaryDirectory() as tmp:
                def broken_main(name):
                    if name == "main":
                        return ({"sha": MAIN, "commit": nested}, "")
                    return self.fixtures()(name)
                result = watch.evaluate(NOW, broken_main)
                self.assertEqual(result["status"], "MONITOR_DATA_FAILURE")
                self.assertIn("main_time_unreliable", {x["code"] for x in result["issues"]})
                self.assertEqual(watch.record(result, Path(tmp)), "MONITOR_DATA_FAILURE")
                self.assertTrue((Path(tmp) / "heartbeat.json").is_file())
                self.assertEqual(json.loads((Path(tmp) / "state.json").read_text())["severe"], [])

    def test_two_unfinished_schedule_runs_use_last_completed_fallback(self):
        original = self.fixtures()
        def reader(name):
            if name == "schedule":
                return ({"workflow_runs": [
                    {"id": 3, "event": "schedule", "created_at": NOW.isoformat(),
                     "status": "queued", "conclusion": None},
                    {"id": 2, "event": "schedule",
                     "created_at": (NOW - timedelta(minutes=5)).isoformat(),
                     "status": "in_progress", "conclusion": None},
                ]}, "")
            if name == "schedule_completed":
                return ({"workflow_runs": [
                    {"id": 1, "event": "schedule",
                     "created_at": (NOW - timedelta(minutes=10)).isoformat(),
                     "status": "completed", "conclusion": "failure"},
                ]}, "")
            return original(name)
        report = watch.evaluate(NOW, reader)
        self.assertEqual(report["latest_schedule"]["id"], 3)
        self.assertEqual(report["status"], "HEALTHY")
        self.assertIn("schedule_failed", {i["code"] for i in report["issues"]})
        self.assertEqual(
            [i["severity"] for i in report["issues"] if i["code"] == "schedule_failed"],
            ["INFO"],
        )

    def test_unconfirmed_peer_p1_survives_other_visible_p1(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            observed = {
                "sha": MAIN,
                "first_seen": (NOW - timedelta(minutes=46)).isoformat(),
            }
            first = watch.evaluate(
                NOW,
                self.fixtures(api=OLD, cache="public, max-age=30"),
                previous_main_observation=observed,
            )
            self.assertEqual(watch.record(first, directory), "ALARM")
            original = json.loads((directory / "state.json").read_text())["severe"]
            self.assertIn("stale_api", [item[0] for item in original])

            def missing_api(name):
                if name == "api":
                    raise ValueError("temporary API observer outage")
                return self.fixtures(api=OLD, cache="public, max-age=30")(name)

            unknown = watch.evaluate(
                NOW + timedelta(minutes=1), missing_api,
                previous_main_observation=observed,
            )
            self.assertEqual(watch.record(unknown, directory), "MONITOR_DATA_FAILURE")
            retained = json.loads((directory / "state.json").read_text())["severe"]
            self.assertEqual(retained, original)
            restored = watch.evaluate(
                NOW + timedelta(minutes=2),
                self.fixtures(api=OLD, cache="public, max-age=30"),
                previous_main_observation=observed,
            )
            self.assertIsNone(watch.record(restored, directory))
            self.assertEqual(len((directory / "events.jsonl").read_text().splitlines()), 2)

    def test_malformed_state_shapes_do_not_disable_heartbeat(self):
        import json
        malformed = [None, [], {"severe": None, "uncertain": None},
                     {"severe": [None, {}, ["frontend_cache", None]],
                      "uncertain": None}]
        for state in malformed:
            with self.subTest(state=state), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                state_bytes = json.dumps(state).encode()
                (directory / "state.json").write_bytes(state_bytes)
                healthy = watch.evaluate(NOW, self.fixtures())
                self.assertEqual(watch.record(healthy, directory),
                                 "MONITOR_DATA_FAILURE")
                self.assertEqual((directory / "state.json").read_bytes(),
                                 state_bytes)
                heartbeat = json.loads((directory / "heartbeat.json").read_text())
                self.assertEqual(heartbeat["status"], "MONITOR_DATA_FAILURE")
                self.assertIn(
                    "unverifiable_state",
                    [item["code"] for item in heartbeat["issues"]],
                )
                self.assertEqual(healthy["status"], "MONITOR_DATA_FAILURE")
                self.assertFalse((directory / "events.jsonl").exists())


    def test_stale_production_survives_another_main_commit(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            initial = watch.evaluate(NOW, self.fixtures(frontend=OLD, api=OLD))
            self.assertIsNone(watch.record(initial, directory))
            overdue = watch.evaluate(
                NOW + timedelta(minutes=46),
                self.fixtures(frontend=OLD, api=OLD),
                previous_main_observation=watch.read_previous_main_observation(directory),
            )
            self.assertIn("stale_frontend", {i["code"] for i in overdue["issues"]})
            self.assertEqual(watch.record(overdue, directory), "ALARM")
            main2 = "c" * 40
            still_stale = watch.evaluate(
                NOW + timedelta(minutes=47),
                self.fixtures(frontend=OLD, api=OLD, main=main2),
                previous_main_observation=watch.read_previous_main_observation(directory),
            )
            self.assertIn("stale_frontend", {i["code"] for i in still_stale["issues"]})
            self.assertIn("stale_api", {i["code"] for i in still_stale["issues"]})
            self.assertIsNone(watch.record(still_stale, directory))
            self.assertEqual(
                [json.loads(line)["event"] for line in
                 (directory / "events.jsonl").read_text().splitlines()],
                ["ALARM"],
            )
            deployed = watch.evaluate(
                NOW + timedelta(minutes=48),
                self.fixtures(frontend=main2, api=main2, main=main2),
                previous_main_observation=watch.read_previous_main_observation(directory),
            )
            self.assertEqual(watch.record(deployed, directory), "RECOVERY")

    def test_repeated_main_advances_cannot_restart_unreported_divergence_grace(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            initial = watch.evaluate(NOW, self.fixtures(frontend=OLD, api=OLD))
            self.assertIsNone(watch.record(initial, directory))
            for minutes, new_main in ((25, "c" * 40), (43, "d" * 40)):
                seen = watch.read_previous_main_observation(directory)
                report = watch.evaluate(
                    NOW + timedelta(minutes=minutes),
                    self.fixtures(frontend=OLD, api=OLD, main=new_main),
                    previous_main_observation=seen,
                )
                self.assertNotIn("stale_frontend", {i["code"] for i in report["issues"]})
                self.assertIsNone(watch.record(report, directory))
                persisted = json.loads((directory / "state.json").read_text())
                self.assertEqual(
                    persisted["main_observation"]["diverged_since"]["frontend"],
                    NOW.isoformat().replace("+00:00", "Z"),
                )
            overdue = watch.evaluate(
                NOW + timedelta(minutes=46),
                self.fixtures(frontend=OLD, api=OLD, main="e" * 40),
                previous_main_observation=watch.read_previous_main_observation(directory),
            )
            self.assertEqual(
                {i["code"] for i in overdue["issues"] if i["severity"] == "P1"},
                {"stale_frontend", "stale_api"},
            )
            self.assertEqual(watch.record(overdue, directory), "ALARM")

    def test_mixed_http_503_and_timeout_does_not_claim_confirmed_product_failure(self):
        from unittest.mock import patch
        from urllib.error import HTTPError

        server_error = HTTPError(watch.URLS["frontend"], 503, "unavailable", {}, None)
        with patch.object(watch, "urlopen",
                          side_effect=[server_error, TimeoutError("transport timeout")]):
            with self.assertRaises(ValueError) as failure:
                watch.fetch("frontend")
        self.assertNotIsInstance(failure.exception, watch.ConfirmedProductHTTPError)

    def test_verified_recovery_is_logged_despite_unrelated_p2(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            first = watch.evaluate(NOW, self.fixtures(cache="public, max-age=30"))
            self.assertEqual(watch.record(first, directory), "ALARM")
            normal = self.fixtures()
            def missing_main(name):
                if name == "main":
                    raise TimeoutError("GitHub reference channel failure")
                return normal(name)
            uncertain = watch.evaluate(NOW + timedelta(minutes=1), missing_main)
            self.assertEqual(uncertain["status"], "MONITOR_DATA_FAILURE")
            self.assertEqual(watch.record(uncertain, directory), "RECOVERY")
            events = [
                json.loads(line)["event"] for line in
                (directory / "events.jsonl").read_text().splitlines()
            ]
            self.assertEqual(events, ["ALARM", "RECOVERY", "MONITOR_DATA_FAILURE"])

    def test_confirmed_http_503_alarm_survives_same_component_timeout(self):
        import json

        for component in ("frontend", "api"):
            with self.subTest(component=component), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                normal = self.fixtures()

                def http_503(name):
                    if name == component:
                        raise watch.ConfirmedProductHTTPError("two confirmed HTTP 503")
                    return normal(name)

                alarm = watch.evaluate(NOW, http_503)
                self.assertEqual(alarm["status"], "ALARM")
                self.assertEqual(watch.record(alarm, directory), "ALARM")
                original = json.loads((directory / "state.json").read_text())["severe"]
                self.assertEqual([row[0] for row in original],
                                 ["unavailable_" + component])

                def timeout(name):
                    if name == component:
                        raise TimeoutError("observation timeout")
                    return normal(name)

                uncertain = watch.evaluate(
                    NOW + timedelta(minutes=1), timeout,
                    previous_main_observation=watch.read_previous_main_observation(directory),
                )
                self.assertEqual(uncertain["status"], "MONITOR_DATA_FAILURE")
                self.assertEqual(
                    [(row["code"], row["severity"]) for row in uncertain["issues"]],
                    [("unavailable_" + component, "P2")],
                )
                self.assertEqual(watch.record(uncertain, directory),
                                 "MONITOR_DATA_FAILURE")
                self.assertEqual(
                    json.loads((directory / "state.json").read_text())["severe"], original,
                )
                heartbeat = json.loads((directory / "heartbeat.json").read_text())
                self.assertEqual(heartbeat["status"], "ALARM")
                self.assertEqual(heartbeat["observation_status"], "MONITOR_DATA_FAILURE")

                recovered = watch.evaluate(
                    NOW + timedelta(minutes=2), normal,
                    previous_main_observation=watch.read_previous_main_observation(directory),
                )
                self.assertEqual(watch.record(recovered, directory), "RECOVERY")
                self.assertEqual(
                    json.loads((directory / "state.json").read_text())["severe"], [],
                )
                self.assertEqual(
                    [json.loads(line)["event"] for line in
                     (directory / "events.jsonl").read_text().splitlines()],
                    ["ALARM", "MONITOR_DATA_FAILURE", "RECOVERY"],
                )

    def test_confirmed_401_403_404_410_endpoint_failures_are_p1(self):
        from unittest.mock import patch
        from urllib.error import HTTPError

        for component in ("frontend", "api"):
            for code in (401, 403, 404, 410):
                with self.subTest(component=component, code=code):
                    error = HTTPError(watch.URLS[component], code, "route absent", {}, None)
                    with patch.object(watch, "urlopen", side_effect=error) as request:
                        with self.assertRaises(watch.ConfirmedProductHTTPError):
                            watch.fetch(component)
                        self.assertEqual(request.call_count, 2)
                    normal = self.fixtures()

                    def absent(name):
                        if name == component:
                            raise watch.ConfirmedProductHTTPError(
                                f"two confirmed product HTTP {code}"
                            )
                        return normal(name)

                    report = watch.evaluate(NOW, absent)
                    self.assertEqual(report["status"], "ALARM")
                    self.assertEqual(
                        [(item["code"], item["severity"]) for item in report["issues"]],
                        [("unavailable_" + component, "P1")],
                    )

    def test_repeated_malformed_200_payload_is_confirmed_product_failure(self):
        from unittest.mock import patch

        for component in ("frontend", "api", "main", "schedule"):
            for payload in (b"not-json", b"[]", bytes(watch.LIMIT + 1)):
                with self.subTest(component=component, payload=payload):
                    normal = self.fixtures()

                    class Response:
                        status = 200
                        headers = {"Cache-Control": "public, no-store"}

                        def __enter__(self):
                            return self

                        def __exit__(self, *_):
                            return False

                        def read(self, _limit):
                            return payload

                    def reader(name):
                        return watch.fetch(name) if name == component else normal(name)

                    with patch.object(watch, "urlopen", return_value=Response()) as request:
                        observed = watch.evaluate(NOW, reader)
                    self.assertEqual(request.call_count, 2)
                    issues = [(issue["code"], issue["severity"])
                              for issue in observed["issues"]]
                    expected_severity = ("P1" if component in ("frontend", "api")
                                         else "P2" if component == "main" else "INFO")
                    self.assertIn(("unavailable_" + component, expected_severity), issues)
                    expected_status = ("ALARM" if expected_severity == "P1"
                                       else "MONITOR_DATA_FAILURE" if expected_severity == "P2"
                                       else "HEALTHY")
                    self.assertEqual(observed["status"], expected_status)
                    with tempfile.TemporaryDirectory() as tmp:
                        event = watch.record(observed, Path(tmp))
                        self.assertEqual(event, None if expected_severity == "INFO"
                                         else "ALARM" if expected_severity == "P1"
                                         else "MONITOR_DATA_FAILURE")
                        self.assertTrue((Path(tmp) / "heartbeat.json").is_file())

    def test_single_malformed_200_plus_timeout_is_not_confirmed_p1(self):
        from unittest.mock import patch

        for component in ("frontend", "api"):
            with self.subTest(component=component):
                attempts = []
                normal = self.fixtures()

                class Response:
                    status = 200
                    headers = {"Cache-Control": "public, no-store"}

                    def __enter__(self):
                        return self

                    def __exit__(self, *_):
                        return False

                    def read(self, _limit):
                        return b"[]"  # Invalid version payload, but only once.

                def intermittent(_request, timeout):
                    attempts.append(timeout)
                    if len(attempts) == 1:
                        return Response()
                    raise TimeoutError("read timed out")

                def reader(name):
                    return watch.fetch(name) if name == component else normal(name)

                with patch.object(watch, "urlopen", side_effect=intermittent):
                    observed = watch.evaluate(NOW, reader)
                self.assertEqual(len(attempts), 2)
                self.assertEqual(observed["status"], "MONITOR_DATA_FAILURE")
                self.assertEqual(
                    [(item["code"], item["severity"]) for item in observed["issues"]],
                    [("unavailable_" + component, "P2")],
                )

    def test_timestamp_normalization_overflow_keeps_observer_alive(self):
        import json

        for value in ("0001-01-01T00:00:00+14:00",
                      "9999-12-31T23:59:59-14:00"):
            with self.subTest(value=value):
                self.assertIsNone(watch.timestamp(value))

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "state.json").write_text(
                json.dumps({"main_observation": {
                    "sha": MAIN, "first_seen": "0001-01-01T00:00:00+14:00"
                }}), encoding="utf-8"
            )
            self.assertIsNone(watch.read_previous_main_observation(directory))
            normal = self.fixtures()

            def reader(name):
                data, headers = normal(name)
                if name == "main":
                    data = {"sha": MAIN, "commit": {
                        "committer": {"date": "0001-01-01T00:00:00+14:00"}
                    }}
                return data, headers

            observed = watch.evaluate(NOW, reader)
            self.assertEqual(observed["status"], "MONITOR_DATA_FAILURE")
            self.assertIn("main_time_unreliable",
                          {row["code"] for row in observed["issues"]})
            self.assertEqual(watch.record(observed, directory),
                             "MONITOR_DATA_FAILURE")
            self.assertTrue((directory / "heartbeat.json").is_file())

    def test_http_429_rate_limit_stays_monitor_uncertainty(self):
        from unittest.mock import patch
        from urllib.error import HTTPError

        for component in ("frontend", "api"):
            with self.subTest(component=component):
                error = HTTPError(watch.URLS[component], 429, "throttled", {}, None)
                with patch.object(watch, "urlopen", side_effect=error) as request:
                    with self.assertRaises(ValueError) as failure:
                        watch.fetch(component)
                    self.assertNotIsInstance(failure.exception,
                                             watch.ConfirmedProductHTTPError)
                    self.assertEqual(request.call_count, 2)

    def test_partial_recovery_does_not_raise_duplicate_alarm(self):
        import json

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            active = watch.evaluate(
                NOW,
                self.fixtures(frontend=OLD, api=MAIN, cache="public, max-age=30"),
            )
            self.assertEqual(watch.record(active, directory), "ALARM")
            prior = json.loads((directory / "state.json").read_text())["severe"]
            self.assertEqual({item[0] for item in prior},
                             {"frontend_api_diverge", "frontend_cache"})

            partial = watch.evaluate(
                NOW + timedelta(minutes=1), self.fixtures(frontend=OLD, api=MAIN),
                previous_main_observation=watch.read_previous_main_observation(directory),
            )
            self.assertIsNone(watch.record(partial, directory))
            open_incidents = json.loads((directory / "state.json").read_text())["severe"]
            self.assertEqual([item[0] for item in open_incidents],
                             ["frontend_api_diverge"])
            self.assertEqual(
                [json.loads(line)["event"] for line in
                 (directory / "events.jsonl").read_text().splitlines()],
                ["ALARM"],
            )
            healthy = watch.evaluate(
                NOW + timedelta(minutes=2), self.fixtures(),
                previous_main_observation=watch.read_previous_main_observation(directory),
            )
            self.assertEqual(watch.record(healthy, directory), "RECOVERY")

    def test_repeated_same_incident_with_changed_details_is_not_new_alarm(self):
        import json

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            normal = self.fixtures()

            def confirmed(detail):
                def reader(name):
                    if name == "frontend":
                        raise watch.ConfirmedProductHTTPError(detail)
                    return normal(name)
                return reader

            first = watch.evaluate(NOW, confirmed("two confirmed HTTP 503"))
            self.assertEqual(watch.record(first, directory), "ALARM")
            next_observation = watch.evaluate(
                NOW + timedelta(minutes=1), confirmed("two confirmed HTTP 502"),
            )
            self.assertIsNone(watch.record(next_observation, directory))
            severe = json.loads((directory / "state.json").read_text())["severe"]
            self.assertEqual([item[0] for item in severe], ["unavailable_frontend"])
            self.assertIn("502", severe[0][1])
            self.assertEqual(
                [json.loads(line)["event"] for line in
                 (directory / "events.jsonl").read_text().splitlines()],
                ["ALARM"],
            )

    def test_out_of_order_polls_cannot_overwrite_newer_heartbeat_or_alarm(self):
        import json

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            old_healthy = watch.evaluate(NOW, self.fixtures())
            newer_alarm = watch.evaluate(
                NOW + timedelta(minutes=1), self.fixtures(frontend=OLD),
            )
            self.assertEqual(watch.record(newer_alarm, directory), "ALARM")
            self.assertIsNone(watch.record(old_healthy, directory))
            saved = json.loads((directory / "state.json").read_text())
            self.assertEqual([row[0] for row in saved["severe"]],
                             ["frontend_api_diverge"])
            heartbeat = json.loads((directory / "heartbeat.json").read_text())
            self.assertEqual(heartbeat["checked_at_utc"],
                             newer_alarm["checked_at_utc"])
            self.assertEqual(heartbeat["status"], "ALARM")
            self.assertEqual(
                [json.loads(row)["event"] for row in
                 (directory / "events.jsonl").read_text().splitlines()],
                ["ALARM"],
            )
            recovered = watch.evaluate(
                NOW + timedelta(minutes=2), self.fixtures(),
                previous_main_observation=watch.read_previous_main_observation(directory),
            )
            self.assertEqual(watch.record(recovered, directory), "RECOVERY")

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            old_alarm = watch.evaluate(NOW, self.fixtures(frontend=OLD))
            newer_healthy = watch.evaluate(
                NOW + timedelta(minutes=1), self.fixtures(),
            )
            self.assertIsNone(watch.record(newer_healthy, directory))
            self.assertIsNone(watch.record(old_alarm, directory))
            self.assertEqual(
                json.loads((directory / "state.json").read_text())["severe"], [],
            )
            self.assertFalse((directory / "events.jsonl").exists())

    def test_two_http_503_reads_are_production_failure_not_transport_noise(self):
        from unittest.mock import patch
        from urllib.error import HTTPError
        error = HTTPError(watch.URLS["frontend"], 503, "unavailable", {}, None)
        with patch.object(watch, "urlopen", side_effect=error) as request:
            with self.assertRaises(watch.ConfirmedProductHTTPError):
                watch.fetch("frontend")
            self.assertEqual(request.call_count, 2)
        normal = self.fixtures()
        def failed_http(name):
            if name == "frontend":
                raise watch.ConfirmedProductHTTPError("two confirmed HTTP 503")
            return normal(name)
        observed = watch.evaluate(NOW, failed_http)
        self.assertEqual(observed["status"], "ALARM")
        self.assertEqual(
            [(i["code"], i["severity"]) for i in observed["issues"]],
            [("unavailable_frontend", "P1")],
        )
        def failed_transport(name):
            if name == "frontend":
                raise TimeoutError("unknown observation transport")
            return normal(name)
        self.assertEqual(watch.evaluate(NOW, failed_transport)["status"],
                         "MONITOR_DATA_FAILURE")

    def test_null_github_commit_shape_preserves_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            normal = self.fixtures()
            def malformed(name):
                if name == "main":
                    return {"sha": MAIN, "commit": None}, ""
                return normal(name)
            result = watch.evaluate(NOW, malformed)
            self.assertEqual(result["status"], "MONITOR_DATA_FAILURE")
            self.assertIn("main_time_unreliable", {i["code"] for i in result["issues"]})
            self.assertEqual(watch.record(result, directory), "MONITOR_DATA_FAILURE")
            self.assertTrue((directory / "heartbeat.json").exists())

    def test_stale_incidents_survive_main_advance_until_production_converges(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            observed = {
                "sha": MAIN,
                "first_seen": (NOW - timedelta(minutes=46)).isoformat(),
            }
            first = watch.evaluate(
                NOW, self.fixtures(frontend=OLD, api=OLD),
                previous_main_observation=observed,
            )
            self.assertEqual(watch.record(first, directory), "ALARM")
            new_main = "c" * 40
            grace = watch.evaluate(
                NOW + timedelta(minutes=1),
                self.fixtures(frontend=OLD, api=OLD, main=new_main),
                previous_main_observation=observed,
            )
            self.assertEqual(grace["status"], "HEALTHY")
            self.assertIsNone(watch.record(grace, directory))
            open_incidents = json.loads((directory / "state.json").read_text())["severe"]
            self.assertEqual({item[0] for item in open_incidents},
                             {"stale_api", "stale_frontend"})
            self.assertEqual(len((directory / "events.jsonl").read_text().splitlines()), 1)
            converged = watch.evaluate(
                NOW + timedelta(minutes=2),
                self.fixtures(frontend=new_main, api=new_main, main=new_main),
                previous_main_observation=grace["main_observation"],
            )
            self.assertEqual(watch.record(converged, directory), "RECOVERY")

    def test_malformed_nested_github_main_metadata_stays_p2_and_writes_heartbeat(self):
        for nested in (
            None, [], "invalid", {"committer": None},
            {"committer": []}, {"committer": {"date": {}}},
        ):
            with self.subTest(nested=nested), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                base_reader = self.fixtures()

                def malformed_main(name):
                    if name == "main":
                        return ({"sha": MAIN, "commit": nested}, "")
                    return base_reader(name)

                observed = watch.evaluate(NOW, malformed_main)
                self.assertEqual(observed["status"], "MONITOR_DATA_FAILURE")
                self.assertIn("main_time_unreliable",
                              {item["code"] for item in observed["issues"]})
                self.assertEqual(watch.record(observed, directory),
                                 "MONITOR_DATA_FAILURE")
                self.assertTrue((directory / "heartbeat.json").is_file())

    def test_schedule_two_unfinished_runs_do_not_hide_last_completed_failure(self):
        import json
        from unittest.mock import patch

        def schedule_run(run_id, minutes, status, conclusion):
            return {"id": run_id, "event": "schedule",
                    "created_at": (NOW - timedelta(minutes=minutes)).isoformat(),
                    "status": status, "conclusion": conclusion}

        newest = [schedule_run(3, 1, "queued", None),
                  schedule_run(2, 3, "in_progress", None)]
        last_completed = schedule_run(1, 6, "completed", "failure")
        calls = []

        class Response:
            status = 200
            headers = {"Cache-Control": ""}

            def __init__(self, data):
                self.payload = json.dumps(data).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self, _limit):
                return self.payload

        def fake_urlopen(request, timeout):
            calls.append(request.full_url)
            if "status=completed" in request.full_url:
                return Response({"workflow_runs": [last_completed]})
            return Response({"workflow_runs": newest})

        base_reader = self.fixtures()
        def reader(name):
            if name in {"schedule", "schedule_completed"}:
                return watch.fetch(name)
            return base_reader(name)

        # evaluate() owns the bounded fallback read, not fetch("schedule").
        with patch.object(watch, "urlopen", side_effect=fake_urlopen):
            observed = watch.evaluate(NOW, reader)
        self.assertEqual(len(calls), 2)
        self.assertTrue(any("status=completed" in url for url in calls))
        self.assertEqual(observed["status"], "HEALTHY")
        self.assertEqual(observed["latest_schedule"]["status"], "queued")
        self.assertIn("schedule_failed", {item["code"] for item in observed["issues"]})

    def test_cron_activation_has_independent_critical_registry_entry(self):
        import re
        registry = (SCRIPT.parents[2] / "audit/impl-registry.yaml").read_text(encoding="utf-8")
        self.assertRegex(
            registry,
            re.compile(
                r"(?m)^  - id: impl.workflow.independent-production-watch-cron\n"
                r"    path: scripts/ops/independent_production_watch\.crontab\n"
                r"    impl_type: workflow\n"
                r"    status: active\n"
                r"    criticality: high\n",
            ),
        )
        crontab = (SCRIPT.parents[2] / "scripts/ops/independent_production_watch.crontab").read_text(
            encoding="utf-8"
        )
        self.assertIn("*/5 * * * * /usr/bin/python3 -B /home/alex/.local/commonthing-production-watch.py", crontab)

    def test_critical_watcher_is_in_evidence_registry(self):
        registry = SCRIPT.parents[2] / "audit/impl-registry.yaml"
        text = registry.read_text(encoding="utf-8")
        self.assertIn("impl.guard.independent-production-watch", text)
        self.assertIn("scripts/ci/tests/test_independent_production_watch.py", text)
        self.assertIn("docs/runbooks/independent-production-watch.md", text)

    def test_dedupe_recovery_and_unknown_not_false_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            alarm = watch.evaluate(NOW, self.fixtures(frontend=OLD))
            self.assertEqual(watch.record(alarm, directory), "ALARM")
            self.assertIsNone(watch.record(alarm, directory))
            def unreadable(name):
                if name == "api":
                    raise ValueError("unreachable after retry")
                return self.fixtures()(name)
            unknown = watch.evaluate(NOW, unreadable)
            self.assertEqual(watch.record(unknown, directory), "MONITOR_DATA_FAILURE")
            self.assertTrue(__import__("json").loads((directory / "state.json").read_text())["severe"])
            recovery = watch.evaluate(NOW, self.fixtures())
            self.assertEqual(watch.record(recovery, directory), "RECOVERY")
            self.assertIsNone(watch.record(recovery, directory))
            lines = (directory / "events.jsonl").read_text().splitlines()
            self.assertEqual(len(lines), 3)
            self.assertTrue((directory / "heartbeat.json").is_file())


    def test_recovered_divergence_anchor_not_resurrected_on_later_main_push(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            prior = {
                "sha": MAIN,
                "first_seen": (NOW - timedelta(minutes=46)).isoformat(),
            }
            lagging = watch.evaluate(
                NOW, self.fixtures(frontend=OLD, api=OLD),
                previous_main_observation=prior,
            )
            self.assertEqual(watch.record(lagging, directory), "ALARM")
            recovered = watch.evaluate(
                NOW + timedelta(minutes=1), self.fixtures(),
                previous_main_observation=watch.read_previous_main_observation(directory),
            )
            self.assertEqual(watch.record(recovered, directory), "RECOVERY")
            saved = watch.read_previous_main_observation(directory)
            self.assertEqual(saved["sha"], MAIN)
            self.assertNotIn("diverged_since", saved)
            next_main = "c" * 40
            fresh_push = watch.evaluate(
                NOW + timedelta(minutes=2),
                self.fixtures(frontend=MAIN, api=MAIN, main=next_main),
                previous_main_observation=saved,
            )
            self.assertFalse(
                [item["code"] for item in fresh_push["issues"]
                 if item["code"] in {"stale_frontend", "stale_api"}]
            )
            self.assertEqual(json.loads((directory / "state.json").read_text())["severe"], [])

    def test_unobserved_frontend_keeps_prior_divergence_anchor_across_main_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            beginning = watch.evaluate(
                NOW, self.fixtures(frontend=OLD, api=OLD),
            )
            self.assertIsNone(watch.record(beginning, directory))
            later_main = "c" * 40
            baseline_reader = self.fixtures(frontend=OLD, api=OLD, main=later_main)
            def missing_frontend(name):
                if name == "frontend":
                    raise OSError("temporary watcher transport outage")
                return baseline_reader(name)
            blind = watch.evaluate(
                NOW + timedelta(minutes=20), missing_frontend,
                previous_main_observation=watch.read_previous_main_observation(directory),
            )
            self.assertEqual(blind["status"], "MONITOR_DATA_FAILURE")
            self.assertEqual(watch.record(blind, directory), "MONITOR_DATA_FAILURE")
            anchor = NOW.isoformat().replace("+00:00", "Z")
            saved = watch.read_previous_main_observation(directory)
            self.assertEqual(saved["diverged_since"]["frontend"], anchor)
            self.assertEqual(saved["diverged_since"]["api"], anchor)
            overdue = watch.evaluate(
                NOW + timedelta(minutes=46),
                self.fixtures(frontend=OLD, api=OLD, main=later_main),
                previous_main_observation=saved,
            )
            self.assertEqual(
                {i["code"] for i in overdue["issues"] if i["severity"] == "P1"},
                {"stale_frontend", "stale_api"},
            )


    def test_invalid_version_schema_needs_two_confirmed_responses(self):
        import json
        from unittest.mock import patch

        for component in ("frontend", "api"):
            for payload in ({"commit": "not-a-sha"}, {}):
                with self.subTest(component=component, payload=payload):
                    attempts = []

                    class Response:
                        status = 200
                        headers = {"Cache-Control": "public, no-store"}

                        def __init__(self, body):
                            self.body = json.dumps(body).encode("utf-8")

                        def __enter__(self):
                            return self

                        def __exit__(self, *_):
                            return False

                        def read(self, _limit):
                            return self.body

                    def fake_urlopen(_request, timeout):
                        attempts.append(timeout)
                        if len(attempts) == 1:
                            return Response(payload)
                        raise TimeoutError("transient second read timeout")

                    normal = self.fixtures()
                    def reader(name):
                        return watch.fetch(name) if name == component else normal(name)

                    with patch.object(watch, "urlopen", side_effect=fake_urlopen):
                        observed = watch.evaluate(NOW, reader)
                    self.assertEqual(len(attempts), 2)
                    self.assertEqual(observed["status"], "MONITOR_DATA_FAILURE")
                    self.assertEqual(
                        [(issue["code"], issue["severity"]) for issue in observed["issues"]],
                        [("unavailable_" + component, "P2")],
                    )

                    with patch.object(watch, "urlopen", return_value=Response(payload)) as requests:
                        with self.assertRaises(watch.ConfirmedProductHTTPError):
                            watch.fetch(component)
                        self.assertEqual(requests.call_count, 2)

                    successful = [Response(payload), Response({"commit": MAIN})]
                    with patch.object(watch, "urlopen", side_effect=successful) as requests:
                        self.assertEqual(watch.fetch(component)[0]["commit"], MAIN)
                        self.assertEqual(requests.call_count, 2)

    def test_new_stale_commit_replaces_prior_incident_detail(self):
        import json

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            prior = {
                "sha": MAIN,
                "first_seen": (NOW - timedelta(minutes=60)).isoformat(),
                "diverged_since": {
                    "frontend": (NOW - timedelta(minutes=60)).isoformat(),
                },
            }
            original = watch.evaluate(
                NOW, self.fixtures(frontend=OLD),
                previous_main_observation=prior,
            )
            self.assertEqual(watch.record(original, directory), "ALARM")
            successor_sha = "c" * 40
            advanced = watch.evaluate(
                NOW + timedelta(minutes=1),
                self.fixtures(frontend=successor_sha),
                previous_main_observation=watch.read_previous_main_observation(directory),
            )
            self.assertEqual(advanced["status"], "ALARM")
            self.assertIsNone(watch.record(advanced, directory))
            severe = json.loads((directory / "state.json").read_text())["severe"]
            self.assertEqual(
                [item for item in severe if item[0] == "stale_frontend"],
                [["stale_frontend", successor_sha]],
            )
            self.assertNotIn("retained_prior_p1", advanced)
            self.assertEqual(len((directory / "events.jsonl").read_text().splitlines()), 1)


    def test_permanent_non200_and_legacy_invalid_commit(self):
        import json
        from unittest.mock import patch
        from urllib.error import HTTPError

        class Response:
            headers = {"Cache-Control": "public, no-store"}

            def __init__(self, payload, status=200):
                self.status = status
                self.payload = json.dumps(payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self, _limit):
                return self.payload

        for component in ("frontend", "api"):
            for code in (204, 400, 405, 418):
                with self.subTest(component=component, code=code):
                    if code == 204:
                        response = Response({"commit": MAIN}, code)
                        side_effect = None
                    else:
                        response = None
                        side_effect = HTTPError(
                            watch.URLS[component], code, "non-200 response", {}, None
                        )
                    with patch.object(
                        watch, "urlopen", return_value=response,
                        side_effect=side_effect,
                    ) as request:
                        with self.assertRaises(watch.ConfirmedProductHTTPError):
                            watch.fetch(component)
                    self.assertEqual(request.call_count, 2)

            for code in (408, 425, 429):
                with self.subTest(component=component, transient=code):
                    failure = HTTPError(
                        watch.URLS[component], code, "transient response", {}, None
                    )
                    with patch.object(watch, "urlopen", side_effect=failure) as request:
                        with self.assertRaises(ValueError) as caught:
                            watch.fetch(component)
                    self.assertNotIsInstance(
                        caught.exception, watch.ConfirmedProductHTTPError
                    )
                    self.assertEqual(request.call_count, 2)

            with self.subTest(component=component, legacy_p1=True):
                with tempfile.TemporaryDirectory() as tmp:
                    directory = Path(tmp)
                    old_code = "invalid_commit_" + component
                    (directory / "state.json").write_text(
                        json.dumps({"severe": [[old_code, "legacy bad commit"]],
                                    "uncertain": []}),
                        encoding="utf-8",
                    )
                    original = self.fixtures()

                    def reader(name):
                        return watch.fetch(name) if name == component else original(name)

                    with patch.object(
                        watch, "urlopen",
                        return_value=Response({"commit": "bad"}),
                    ) as request:
                        observed = watch.evaluate(NOW, reader)
                    self.assertEqual(request.call_count, 2)
                    self.assertEqual(
                        [(i["code"], i["severity"]) for i in observed["issues"]],
                        [(old_code, "P1")],
                    )
                    self.assertIsNone(watch.record(observed, directory))
                    severe = json.loads(
                        (directory / "state.json").read_text(encoding="utf-8")
                    )["severe"]
                    self.assertEqual(len(severe), 1)
                    self.assertEqual(severe[0][0], old_code)
                    self.assertFalse((directory / "events.jsonl").exists())
                    self.assertEqual(observed["status"], "ALARM")


    def test_cache_p1_survives_two_confirmed_invalid_commit_responses(self):
        import json

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            initial = watch.evaluate(NOW, self.fixtures(cache="public, max-age=30"))
            self.assertEqual(watch.record(initial, directory), "ALARM")
            normal = self.fixtures()
            def bad_version(name):
                if name == "frontend":
                    raise watch.ConfirmedProductVersionError(
                        "two confirmed invalid frontend commit fields"
                    )
                return normal(name)
            invalid = watch.evaluate(NOW + timedelta(minutes=1), bad_version)
            self.assertEqual(invalid["status"], "ALARM")
            self.assertEqual(watch.record(invalid, directory), "ALARM")
            severe = json.loads((directory / "state.json").read_text())["severe"]
            self.assertEqual(
                {item[0] for item in severe},
                {"frontend_cache", "invalid_commit_frontend"},
            )
            self.assertIn("frontend_cache", [row[0] for row in invalid["retained_prior_p1"]])
            still_wrong = watch.evaluate(
                NOW + timedelta(minutes=2),
                self.fixtures(cache="public, max-age=30"),
            )
            self.assertIsNone(watch.record(still_wrong, directory))
            self.assertEqual(
                [item[0] for item in json.loads(
                    (directory / "state.json").read_text())["severe"]],
                ["frontend_cache"],
            )
            events = [json.loads(line)["event"] for line in
                      (directory / "events.jsonl").read_text().splitlines()]
            self.assertEqual(events, ["ALARM", "ALARM"])

    def test_newer_atomic_state_time_survives_failed_heartbeat_write(self):
        import json
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            early = watch.evaluate(NOW, self.fixtures())
            self.assertIsNone(watch.record(early, directory))
            newer = watch.evaluate(
                NOW + timedelta(minutes=3),
                self.fixtures(cache="public, max-age=30"),
            )
            original_write_json = watch.write_json
            def fail_only_heartbeat(path, value):
                if path.name == "heartbeat.json":
                    raise OSError("simulated heartbeat replace failed")
                return original_write_json(path, value)
            with patch.object(watch, "write_json", side_effect=fail_only_heartbeat):
                with self.assertRaises(OSError):
                    watch.record(newer, directory)

            state_before = json.loads((directory / "state.json").read_text())
            self.assertEqual(
                [item[0] for item in state_before["severe"]], ["frontend_cache"]
            )
            # Previous heartbeat is still old despite the newer committed alarm.
            heartbeat = json.loads((directory / "heartbeat.json").read_text())
            self.assertEqual(heartbeat["checked_at_utc"], early["checked_at_utc"])

            stale = watch.evaluate(NOW + timedelta(minutes=1), self.fixtures())
            self.assertIsNone(watch.record(stale, directory))
            state_after = json.loads((directory / "state.json").read_text())
            self.assertEqual(state_after, state_before)
            # A heartbeat failure happens before journal publication now.
            # The durable state outbox retains the first alarm for later replay.
            self.assertFalse((directory / "events.jsonl").exists())
            self.assertEqual(
                [x["event"] for x in state_after["pending_transitions"]],
                ["ALARM"],
            )
            later = watch.evaluate(
                NOW + timedelta(minutes=4),
                self.fixtures(cache="public, max-age=30"),
            )
            self.assertEqual(watch.record(later, directory), "ALARM")
            published = [json.loads(line) for line in
                         (directory / "events.jsonl").read_text().splitlines()]
            self.assertEqual([x["event"] for x in published], ["ALARM"])
            self.assertEqual(
                published[0]["checked_at_utc"], newer["checked_at_utc"],
            )


    def test_completed_schedule_errors_bind_completed_url_and_45m_boundary(self):
        base = self.fixtures()
        now = NOW

        def unreadable_completed(name):
            if name == "schedule":
                return (
                    {"workflow_runs": [
                        {"id": 2, "event": "schedule", "status": "queued",
                         "created_at": (now - timedelta(minutes=1)).isoformat()},
                    ]}, "",
                )
            if name == "schedule_completed":
                raise TimeoutError("completed-runs-only endpoint failed")
            return base(name)

        observed = watch.evaluate(NOW, unreadable_completed)
        issues = [item for item in observed["issues"]
                  if item["code"] == "unavailable_completed_schedule"]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["source"], watch.SCHEDULE_COMPLETED_URL)
        self.assertEqual(issues[0]["severity"], "INFO")
        self.assertEqual(observed["status"], "HEALTHY")

        exactly_45 = watch.evaluate(NOW, self.fixtures(schedule_age=45))
        flagged = [item for item in exactly_45["issues"]
                   if item["code"] == "schedule_stale"]
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["severity"], "INFO")


    def test_unreadable_state_file_fails_closed_without_false_recovery(self):
        import json
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            initial = watch.evaluate(NOW, self.fixtures(cache="public, max-age=30"))
            self.assertEqual(watch.record(initial, directory), "ALARM")
            previous_state = (directory / "state.json").read_bytes()
            previous_heartbeat = (directory / "heartbeat.json").read_bytes()
            original_read = Path.read_text

            def protected_read(path, *args, **kwargs):
                if path == directory / "state.json":
                    raise PermissionError("simulated unreadable incident state")
                return original_read(path, *args, **kwargs)

            recovery = watch.evaluate(NOW + timedelta(minutes=1), self.fixtures())
            with patch.object(Path, "read_text", new=protected_read):
                self.assertEqual(watch.record(recovery, directory),
                                 "MONITOR_DATA_FAILURE")

            self.assertEqual((directory / "state.json").read_bytes(), previous_state)
            heartbeat = json.loads((directory / "heartbeat.json").read_text())
            self.assertNotEqual((directory / "heartbeat.json").read_bytes(),
                                previous_heartbeat)
            self.assertEqual(heartbeat["status"], "ALARM")
            self.assertIn("frontend_cache", [i["code"] for i in heartbeat["issues"]])
            self.assertIn("unverifiable_state", [i["code"] for i in heartbeat["issues"]])
            events = [json.loads(line)["event"] for line in
                      (directory / "events.jsonl").read_text().splitlines()]
            self.assertEqual(events, ["ALARM"])

    def test_event_journal_failure_preserves_state_and_heartbeat(self):
        import json
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            healthy = watch.evaluate(NOW, self.fixtures())
            self.assertIsNone(watch.record(healthy, directory))
            alarm = watch.evaluate(
                NOW + timedelta(minutes=1),
                self.fixtures(cache="public, max-age=30"),
            )
            original_open = Path.open

            def cannot_append(path, *args, **kwargs):
                if path == directory / "events.jsonl":
                    raise OSError("simulated event history unavailable")
                return original_open(path, *args, **kwargs)

            with patch.object(Path, "open", new=cannot_append):
                self.assertEqual(watch.record(alarm, directory), "ALARM")

            state = json.loads((directory / "state.json").read_text())
            heartbeat = json.loads((directory / "heartbeat.json").read_text())
            self.assertEqual([x[0] for x in state["severe"]], ["frontend_cache"])
            self.assertIn("event_log_write_failed", state["uncertain"])
            self.assertEqual(heartbeat["status"], "ALARM")
            self.assertIn(
                "event_log_write_failed",
                [issue["code"] for issue in heartbeat["issues"]],
            )
            self.assertEqual(
                heartbeat["checked_at_utc"],
                alarm["checked_at_utc"],
            )

    def test_completed_failure_uses_completed_history_source(self):
        base_reader = self.fixtures()
        queue = [
            {"id": 3, "event": "schedule", "status": "queued",
             "created_at": (NOW - timedelta(minutes=1)).isoformat()},
            {"id": 2, "event": "schedule", "status": "in_progress",
             "created_at": (NOW - timedelta(minutes=2)).isoformat()},
        ]
        completed = {
            "id": 1, "event": "schedule", "status": "completed",
            "created_at": (NOW - timedelta(minutes=3)).isoformat(),
            "conclusion": "failure",
        }

        def reader(name):
            if name == "schedule":
                return {"workflow_runs": queue}, ""
            if name == "schedule_completed":
                return {"workflow_runs": [completed]}, ""
            return base_reader(name)

        observed = watch.evaluate(NOW, reader)
        failures = [i for i in observed["issues"]
                    if i["code"] == "schedule_failed"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["source"], watch.SCHEDULE_COMPLETED_URL)
        self.assertEqual(failures[0]["severity"], "INFO")


    def test_main_failsafe_observation_and_total_runtime_budget(self):
        import json
        import signal
        import sys
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            argv = ["independent_production_watch.py", "--state-dir", tmp]
            with patch.object(sys, "argv", argv), patch.object(
                watch, "evaluate", side_effect=RuntimeError("unexpected evaluation failure")
            ), patch.object(signal, "alarm", return_value=0) as alarm:
                code = watch.main()
            self.assertEqual(code, 2)
            self.assertIn(((240,), {}), [
                (call.args, call.kwargs) for call in alarm.call_args_list
            ])
            self.assertEqual(alarm.call_args.args, (0,))
            heartbeat = json.loads((directory / "heartbeat.json").read_text())
            self.assertEqual(heartbeat["status"], "MONITOR_DATA_FAILURE")
            self.assertEqual(
                [(i["code"], i["severity"]) for i in heartbeat["issues"]],
                [("watcher_execution_failed", "P2")],
            )
            self.assertTrue((directory / "state.json").is_file())
            self.assertEqual(
                json.loads((directory / "state.json").read_text())["uncertain"],
                ["watcher_execution_failed"],
            )

    def test_serialized_main_poll_survives_backward_wall_clock_jump(self):
        import json
        import signal
        import sys
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            future = datetime.now(timezone.utc) + timedelta(hours=3)
            previous = watch.evaluate(
                future, self.fixtures(cache="public, max-age=30"),
            )
            self.assertEqual(watch.record(previous, directory), "ALARM")
            earlier_clock = datetime.now(timezone.utc) - timedelta(seconds=1)
            corrected = watch.evaluate(earlier_clock, self.fixtures())
            self.assertEqual(corrected["status"], "HEALTHY")
            with patch.object(sys, "argv", [
                "independent_production_watch.py", "--state-dir", tmp,
            ]), patch.object(
                watch, "evaluate", return_value=corrected,
            ), patch.object(signal, "alarm", return_value=0):
                self.assertEqual(watch.main(), 0)
            state = json.loads((directory / "state.json").read_text())
            self.assertEqual(state["severe"], [])
            self.assertEqual(state["checked_at_utc"], corrected["checked_at_utc"])
            self.assertTrue((directory / "poll.lock").is_file())


    def test_deadline_signal_interrupts_retry_instead_of_becoming_p2_timeout(self):
        import signal
        from unittest.mock import patch

        original = signal.signal(signal.SIGALRM, watch._watch_deadline)
        try:
            def interrupt(_request, timeout):
                signal.raise_signal(signal.SIGALRM)
            with patch.object(watch, "urlopen", side_effect=interrupt) as urlopen:
                with self.assertRaises(watch.WatchRuntimeExceeded):
                    watch.fetch("frontend")
            self.assertEqual(urlopen.call_count, 1)
        finally:
            signal.signal(signal.SIGALRM, original)

    def test_main_record_failure_best_effort_heartbeat_retains_p1(self):
        import json
        import signal
        import sys
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            initial = watch.evaluate(NOW, self.fixtures(cache="public, max-age=30"))
            self.assertEqual(watch.record(initial, directory), "ALARM")
            real_record = watch.record
            attempts = []

            def intermittent_record(*args, **kwargs):
                attempts.append(1)
                if len(attempts) == 1:
                    raise OSError("transient state journal unavailable")
                return real_record(*args, **kwargs)

            with patch.object(sys, "argv", [
                "independent_production_watch.py", "--state-dir", tmp,
            ]), patch.object(
                watch, "evaluate", return_value=watch.evaluate(
                    datetime.now(timezone.utc), self.fixtures(),
                )
            ), patch.object(
                watch, "record", side_effect=intermittent_record,
            ), patch.object(signal, "alarm", return_value=0):
                self.assertEqual(watch.main(), 2)

            self.assertEqual(len(attempts), 2)
            state = json.loads((directory / "state.json").read_text())
            heartbeat = json.loads((directory / "heartbeat.json").read_text())
            self.assertEqual([x[0] for x in state["severe"]], ["frontend_cache"])
            self.assertEqual(heartbeat["status"], "ALARM")
            self.assertIn(
                "watcher_execution_failed",
                [i["code"] for i in heartbeat["issues"]],
            )


    def test_first_new_p1_survives_transient_state_replace_failure(self):
        import json
        import signal
        import sys
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            new_alarm = watch.evaluate(
                NOW, self.fixtures(cache="public, max-age=30")
            )
            self.assertEqual(new_alarm["status"], "ALARM")
            actual_write = watch.write_json
            failures = []

            def fail_first_state_replace(path, value):
                if path.name == "state.json" and not failures:
                    failures.append("state.json")
                    raise OSError("simulated first state replace failure")
                return actual_write(path, value)

            with patch.object(sys, "argv", [
                "independent_production_watch.py", "--state-dir", tmp,
            ]), patch.object(
                watch, "evaluate", return_value=new_alarm,
            ), patch.object(
                watch, "write_json", side_effect=fail_first_state_replace,
            ), patch.object(signal, "alarm", return_value=0):
                self.assertEqual(watch.main(), 2)

            self.assertEqual(failures, ["state.json"])
            persisted = json.loads((directory / "state.json").read_text())
            self.assertIn("frontend_cache", [row[0] for row in persisted["severe"]])
            heartbeat = json.loads((directory / "heartbeat.json").read_text())
            self.assertEqual(heartbeat["status"], "ALARM")
            self.assertIn(
                "watcher_execution_failed",
                [row["code"] for row in heartbeat["issues"]],
            )
            # A snapshot retry must not repeat the ALARM already journaled.
            self.assertEqual(
                [json.loads(line)["event"] for line in
                 (directory / "events.jsonl").read_text().splitlines()],
                ["ALARM", "MONITOR_DATA_FAILURE"],
            )
            # The retry stamps its actual wall-clock time, not fixture NOW.
            later_at = watch.timestamp(heartbeat["checked_at_utc"]) + timedelta(minutes=1)
            later = watch.evaluate(later_at, self.fixtures())
            self.assertEqual(watch.record(later, directory), "RECOVERY")

    def test_overall_deadline_rearmed_after_evaluation_timeout(self):
        import json
        import signal
        import sys
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with patch.object(sys, "argv", [
                "independent_production_watch.py", "--state-dir", tmp,
            ]), patch.object(
                watch, "evaluate",
                side_effect=watch.WatchRuntimeExceeded("simulated 240s deadline"),
            ), patch.object(signal, "alarm", return_value=0) as alarm:
                self.assertEqual(watch.main(), 2)
            self.assertEqual(
                [call.args for call in alarm.call_args_list],
                [(watch.WATCH_MAX_SECONDS,), (10,), (0,)],
            )
            heartbeat = json.loads((directory / "heartbeat.json").read_text())
            self.assertEqual(heartbeat["status"], "MONITOR_DATA_FAILURE")
            self.assertEqual(heartbeat["issues"][0]["code"], "watcher_execution_failed")


    def test_late_schedule_deadline_keeps_earlier_confirmed_product_p1(self):
        import json
        from unittest.mock import patch

        for mode, code in (
            ("confirmed_http", "unavailable_frontend"),
            ("cache_header", "frontend_cache"),
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                normal = self.fixtures()
                actual_evaluate = watch.evaluate

                def reader(name):
                    if name == "frontend" and mode == "confirmed_http":
                        raise watch.ConfirmedProductHTTPError("two observed HTTP 503")
                    if name == "frontend" and mode == "cache_header":
                        data, _ = normal(name)
                        return data, "public, max-age=3600"
                    if name == "schedule":
                        raise watch.WatchRuntimeExceeded(
                            "global deadline during later scheduled endpoint"
                        )
                    return normal(name)

                def interrupted_evaluation(
                    now, *, previous_main_observation=None, issue_sink=None,
                ):
                    options = {"previous_main_observation": previous_main_observation}
                    if issue_sink is not None:
                        options["issue_sink"] = issue_sink
                    return actual_evaluate(now, reader, **options)

                with patch.object(
                    watch, "evaluate", side_effect=interrupted_evaluation,
                ):
                    result = watch._evaluate_cycle(directory)
                self.assertEqual(result["status"], "ALARM")
                self.assertIn(
                    code, [issue["code"] for issue in result["issues"]]
                )
                self.assertIn(
                    "schedule_deadline_exceeded",
                    [issue["code"] for issue in result["issues"]],
                )
                self.assertEqual(watch.record(result, directory), "ALARM")
                severe = json.loads((directory / "state.json").read_text())["severe"]
                self.assertIn(code, [item[0] for item in severe])


    def test_corrupt_state_keeps_prior_heartbeat_p1_and_no_false_recovery(self):
        import json

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            original = watch.evaluate(NOW, self.fixtures(cache="public, max-age=30"))
            self.assertEqual(watch.record(original, directory), "ALARM")
            clean_state = (directory / "state.json").read_bytes()
            damaged_state = b'{"severe":[["frontend_cache",'
            (directory / "state.json").write_bytes(damaged_state)

            healthy = watch.evaluate(NOW + timedelta(minutes=1), self.fixtures())
            self.assertEqual(watch.record(healthy, directory),
                             "MONITOR_DATA_FAILURE")
            self.assertEqual((directory / "state.json").read_bytes(), damaged_state)
            heartbeat = json.loads((directory / "heartbeat.json").read_text())
            self.assertEqual(heartbeat["status"], "ALARM")
            self.assertIn("frontend_cache",
                          [item["code"] for item in heartbeat["issues"]])
            self.assertIn("unverifiable_state",
                          [item["code"] for item in heartbeat["issues"]])
            self.assertEqual(
                [json.loads(line)["event"] for line in
                 (directory / "events.jsonl").read_text().splitlines()],
                ["ALARM"],
            )
            # After an explicitly restored validated state, real recovery works.
            (directory / "state.json").write_bytes(clean_state)
            later = watch.evaluate(NOW + timedelta(minutes=2), self.fixtures())
            self.assertEqual(watch.record(later, directory), "RECOVERY")


    def test_event_batch_is_serialized_before_one_append(self):
        import json
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            normal = self.fixtures(cache="public, max-age=30")

            def failing_reference(name):
                if name == "main":
                    raise TimeoutError("GitHub reference unavailable")
                return normal(name)

            observed = watch.evaluate(NOW, failing_reference)
            self.assertEqual(observed["status"], "ALARM")
            self.assertEqual(
                {x["severity"] for x in observed["issues"]}, {"P1", "P2"},
            )
            actual_open = Path.open
            writes = []

            class EventWriter:
                def __init__(self, stream):
                    self.stream = stream

                def __enter__(self):
                    self.stream.__enter__()
                    return self

                def __exit__(self, *args):
                    return self.stream.__exit__(*args)

                def write(self, content):
                    writes.append(content)
                    return self.stream.write(content)

                def flush(self):
                    return self.stream.flush()

                def fileno(self):
                    return self.stream.fileno()

            def monitored_open(path, *args, **kwargs):
                stream = actual_open(path, *args, **kwargs)
                return (
                    EventWriter(stream)
                    if path == directory / "events.jsonl" else stream
                )

            with patch.object(Path, "open", new=monitored_open):
                self.assertEqual(watch.record(observed, directory), "ALARM")

            self.assertEqual(len(writes), 1)
            self.assertTrue(writes[0].endswith(b"\n"))
            self.assertEqual(
                [json.loads(line)["event"] for line in writes[0].splitlines()],
                ["ALARM", "MONITOR_DATA_FAILURE"],
            )


    def test_event_fsync_failure_truncates_uncommitted_append(self):
        import json
        import os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            first = watch.evaluate(NOW, self.fixtures(cache="public, max-age=30"))
            actual_fsync = watch.os.fsync
            failed = []

            def interrupted_fsync(fd):
                destination = os.readlink(f"/proc/self/fd/{fd}")
                if destination == str(directory / "events.jsonl") and not failed:
                    failed.append(True)
                    raise OSError("simulated event journal fsync failure")
                return actual_fsync(fd)

            with patch.object(watch.os, "fsync", side_effect=interrupted_fsync):
                self.assertEqual(watch.record(first, directory), "ALARM")
            self.assertEqual(failed, [True])
            self.assertEqual((directory / "events.jsonl").read_bytes(), b"")
            heartbeat = json.loads((directory / "heartbeat.json").read_text())
            self.assertEqual(heartbeat["status"], "ALARM")
            self.assertIn("event_log_write_failed",
                          [item["code"] for item in heartbeat["issues"]])
            self.assertEqual(
                [item[0] for item in
                 json.loads((directory / "state.json").read_text())["severe"]],
                ["frontend_cache"],
            )

            recovered = watch.evaluate(
                NOW + timedelta(minutes=1), self.fixtures()
            )
            self.assertEqual(watch.record(recovered, directory), "RECOVERY")
            self.assertEqual(
                [json.loads(line)["event"] for line in
                 (directory / "events.jsonl").read_text().splitlines()],
                ["ALARM", "RECOVERY"],
            )


    def test_snapshot_replace_fsyncs_parent_for_state_and_heartbeat(self):
        import json
        import os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            actual_fsync = watch.os.fsync
            calls = []

            def capture_fsync(fd):
                calls.append(os.readlink(f"/proc/self/fd/{fd}"))
                return actual_fsync(fd)

            with patch.object(watch.os, "fsync", side_effect=capture_fsync):
                for name in ("state.json", "heartbeat.json"):
                    target = directory / name
                    for revision in (1, 2):
                        watch.write_json(target, {"revision": revision})
                        self.assertEqual(json.loads(target.read_text()),
                                         {"revision": revision})

            self.assertEqual(calls, [
                str(directory / "state.json.tmp"), str(directory),
                str(directory / "state.json.tmp"), str(directory),
                str(directory / "heartbeat.json.tmp"), str(directory),
                str(directory / "heartbeat.json.tmp"), str(directory),
            ])

    def test_failed_parent_fsync_is_not_success_and_retains_confirmed_p1(self):
        import json
        import os
        import signal
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            alarm = watch.evaluate(
                NOW, self.fixtures(cache="public, max-age=30")
            )
            self.assertEqual(alarm["status"], "ALARM")
            original_fsync = watch.os.fsync
            failures = []
            last_file = [None]

            def fail_first_directory_sync(fd):
                destination = os.readlink(f"/proc/self/fd/{fd}")
                if destination.endswith(".tmp"):
                    last_file[0] = destination
                if (destination == str(directory)
                        and last_file[0] == str(directory / "state.json.tmp")
                        and not failures):
                    failures.append(destination)
                    raise OSError("simulated parent directory fsync failure")
                return original_fsync(fd)

            with patch.object(
                watch.os, "fsync", side_effect=fail_first_directory_sync,
            ), patch.object(signal, "alarm", return_value=0):
                self.assertEqual(watch._persist_cycle(alarm, directory), 2)

            self.assertEqual(failures, [str(directory)])
            state = json.loads((directory / "state.json").read_text())
            self.assertIn("frontend_cache", [item[0] for item in state["severe"]])
            heartbeat = json.loads((directory / "heartbeat.json").read_text())
            self.assertEqual(heartbeat["status"], "ALARM")
            self.assertIn(
                "watcher_execution_failed",
                [item["code"] for item in heartbeat["issues"]],
            )
            events = [json.loads(line)["event"] for line in
                      (directory / "events.jsonl").read_text().splitlines()]
            self.assertEqual(events.count("ALARM"), 1)


    def test_transient_prior_anchor_read_error_preserves_grace(self):
        import json
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            initial = watch.evaluate(
                NOW, self.fixtures(frontend=OLD, api=OLD)
            )
            self.assertEqual(initial["status"], "HEALTHY")
            watch.record(initial, directory)
            anchor = watch.read_previous_main_observation(directory)
            self.assertIn("diverged_since", anchor)
            read_text = Path.read_text
            failures = []

            def interrupted_read(path, *args, **kwargs):
                if path == directory / "state.json" and not failures:
                    failures.append(True)
                    raise OSError("simulated transient state read failure")
                return read_text(path, *args, **kwargs)

            # evaluate(reader=fetch) has a default-bound function; patch its
            # entrypoint explicitly so this test is hermetic on every runner.
            original_evaluate = watch.evaluate
            fixture_reader = self.fixtures(frontend=OLD, api=OLD)

            def evaluate_fixture(now, *, previous_main_observation=None, issue_sink=None):
                return original_evaluate(
                    now, fixture_reader,
                    previous_main_observation=previous_main_observation,
                    issue_sink=issue_sink,
                )

            class StableNow(datetime):
                @classmethod
                def now(cls, tz=None):
                    return NOW + timedelta(minutes=1)

            with patch.object(Path, "read_text", new=interrupted_read), patch.object(
                watch, "evaluate", side_effect=evaluate_fixture,
            ), patch.object(watch, "datetime", StableNow):
                result = watch._evaluate_cycle(directory)

            self.assertEqual(failures, [True])
            self.assertEqual(result["status"], "MONITOR_DATA_FAILURE")
            self.assertIn(
                "unverifiable_state",
                [item["code"] for item in result["issues"]],
            )
            self.assertEqual(watch.record(result, directory),
                             "MONITOR_DATA_FAILURE")
            state = json.loads((directory / "state.json").read_text())
            self.assertEqual(state["main_observation"], anchor)
            overdue = watch.evaluate(
                NOW + timedelta(minutes=46),
                self.fixtures(frontend=OLD, api=OLD),
                previous_main_observation=watch.read_previous_main_observation(directory),
            )
            self.assertEqual(
                {item["code"] for item in overdue["issues"]
                 if item["severity"] == "P1"},
                {"stale_frontend", "stale_api"},
            )

    def test_new_event_journal_fsyncs_directory_before_snapshot(self):
        import os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            alarm = watch.evaluate(
                NOW, self.fixtures(cache="public, max-age=30")
            )
            actual = watch.os.fsync
            calls = []

            def monitored_fsync(fd):
                calls.append(os.readlink(f"/proc/self/fd/{fd}"))
                return actual(fd)

            with patch.object(watch.os, "fsync", side_effect=monitored_fsync):
                self.assertEqual(watch.record(alarm, directory), "ALARM")

            event_index = calls.index(str(directory / "events.jsonl"))
            self.assertEqual(calls[event_index + 1], str(directory))
            self.assertEqual(calls[event_index + 2],
                             str(directory / "state.json.tmp"))

    def test_failed_journal_directory_sync_rolls_back_event_and_keeps_p1(self):
        import json
        import os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            alarm = watch.evaluate(
                NOW, self.fixtures(cache="public, max-age=30")
            )
            original = watch.os.fsync
            faults = []

            last_synced_file = [None]
            def fail_first_directory_fsync(fd):
                destination = os.readlink(f"/proc/self/fd/{fd}")
                if destination == str(directory / "events.jsonl"):
                    last_synced_file[0] = destination
                # The ALARM intent is now durably persisted before the journal.
                # Inject the error on the journal's own directory fsync.
                if (destination == str(directory)
                        and last_synced_file[0] == str(directory / "events.jsonl")
                        and not faults):
                    faults.append(True)
                    raise OSError("simulated journal directory fsync failure")
                return original(fd)

            with patch.object(
                watch.os, "fsync", side_effect=fail_first_directory_fsync,
            ):
                self.assertEqual(watch.record(alarm, directory), "ALARM")

            self.assertEqual(faults, [True])
            self.assertEqual((directory / "events.jsonl").read_bytes(), b"")
            state = json.loads((directory / "state.json").read_text())
            self.assertIn("frontend_cache", [item[0] for item in state["severe"]])
            heartbeat = json.loads((directory / "heartbeat.json").read_text())
            self.assertEqual(heartbeat["status"], "ALARM")
            self.assertIn(
                "event_log_write_failed",
                [item["code"] for item in heartbeat["issues"]],
            )


    def test_deadline_during_partial_event_append_rolls_back_fragment(self):
        import json
        import signal
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            alarm = watch.evaluate(
                NOW, self.fixtures(cache="public, max-age=30")
            )
            original_open = Path.open
            interrupted = []

            class InterruptedWriter:
                def __init__(self, stream):
                    self.stream = stream

                def __enter__(self):
                    self.stream.__enter__()
                    return self

                def __exit__(self, *args):
                    return self.stream.__exit__(*args)

                def fileno(self):
                    return self.stream.fileno()

                def write(self, payload):
                    if not interrupted:
                        interrupted.append(True)
                        self.stream.write(payload[:5])
                        raise watch.WatchRuntimeExceeded("deadline during append")
                    return self.stream.write(payload)

            def interrupted_open(path, *args, **kwargs):
                stream = original_open(path, *args, **kwargs)
                if path == directory / "events.jsonl" and "ab" in args:
                    return InterruptedWriter(stream)
                return stream

            with patch.object(Path, "open", new=interrupted_open), patch.object(
                signal, "alarm", return_value=0,
            ):
                self.assertEqual(watch._persist_cycle(alarm, directory), 2)

            self.assertEqual(interrupted, [True])
            lines = (directory / "events.jsonl").read_text().splitlines()
            self.assertEqual([json.loads(line)["event"] for line in lines],
                             ["ALARM", "MONITOR_DATA_FAILURE"])
            state = json.loads((directory / "state.json").read_text())
            self.assertIn("frontend_cache", [row[0] for row in state["severe"]])
            heartbeat = json.loads((directory / "heartbeat.json").read_text())
            self.assertEqual(heartbeat["status"], "ALARM")



    def test_deadline_rearmed_before_journal_rollback_io(self):
        import os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            alarm = watch.evaluate(NOW, self.fixtures(cache="max-age=3600"))
            real_fsync = watch.os.fsync
            real_truncate = watch.os.ftruncate
            events = []

            def expired_journal_sync(fd):
                if (os.readlink(f"/proc/self/fd/{fd}")
                        == str(directory / "events.jsonl")
                        and "timeout" not in events):
                    events.append("timeout")
                    raise watch.WatchRuntimeExceeded("deadline in journal fsync")
                return real_fsync(fd)

            def inspect_rollback(fd, offset):
                events.append("rollback")
                return real_truncate(fd, offset)

            with patch.object(watch.os, "fsync", side_effect=expired_journal_sync), (
                patch.object(watch.os, "ftruncate", side_effect=inspect_rollback)
            ), patch.object(watch.signal, "getsignal",
                            return_value=watch._watch_deadline), patch.object(
                watch.signal, "alarm",
                side_effect=lambda seconds: events.append(("alarm", seconds)),
            ):
                with self.assertRaises(watch.WatchRuntimeExceeded):
                    watch.record(alarm, directory)

            self.assertEqual(events.count("rollback"), 1)
            self.assertIn(("alarm", 10), events)
            self.assertLess(events.index(("alarm", 10)), events.index("rollback"))
            self.assertEqual((directory / "events.jsonl").read_bytes(), b"")

    def test_optional_schedule_deadline_retains_primary_mismatch_anchor(self):
        import json

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            normal = self.fixtures(frontend=OLD, api=OLD)

            def schedule_deadline(name):
                if name in {"schedule", "schedule_completed"}:
                    raise watch.WatchRuntimeExceeded("optional schedule hung")
                return normal(name)

            first = watch.evaluate(NOW, schedule_deadline)
            self.assertEqual(first["status"], "MONITOR_DATA_FAILURE")
            self.assertIn("frontend", first["main_observation"]["diverged_since"])
            self.assertEqual(watch.record(first, directory),
                             "MONITOR_DATA_FAILURE")
            persisted = json.loads((directory / "state.json").read_text())
            expected = persisted["main_observation"]["diverged_since"]
            self.assertEqual(expected["frontend"],
                             NOW.isoformat().replace("+00:00", "Z"))

            overdue = watch.evaluate(
                NOW + timedelta(minutes=46), schedule_deadline,
                previous_main_observation=watch.read_previous_main_observation(directory),
            )
            self.assertEqual(overdue["status"], "ALARM")
            self.assertIn("stale_frontend", [x["code"] for x in overdue["issues"]])

    def test_retry_before_journal_append_emits_original_new_alarm(self):
        import json
        import signal
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            alarm = watch.evaluate(NOW, self.fixtures(cache="max-age=3600"))
            actual_read = Path.read_text
            failures = []

            def transient_read(path, *args, **kwargs):
                if path == directory / "state.json" and not failures:
                    failures.append(True)
                    raise OSError("synthetic incident-state read error before event append")
                return actual_read(path, *args, **kwargs)

            with patch.object(Path, "read_text", new=transient_read), (
                patch.object(signal, "alarm", return_value=0)
            ):
                self.assertEqual(watch._persist_cycle(alarm, directory), 2)

            self.assertEqual(failures, [True])
            self.assertFalse((directory / "events.jsonl").exists())
            self.assertFalse((directory / "state.json").exists())
            heartbeat = json.loads((directory / "heartbeat.json").read_text())
            self.assertEqual(heartbeat["status"], "ALARM")
            self.assertIn("unverifiable_state", [i["code"] for i in heartbeat["issues"]])
            self.assertIn("frontend_cache", [i["code"] for i in heartbeat["issues"]])
            # When the state becomes readable, this same P1 must still journal
            # its first ALARM, not a RECOVERY or duplicate failed transition.
            subsequent = watch.evaluate(
                NOW + timedelta(minutes=1), self.fixtures(cache="max-age=3600")
            )
            self.assertEqual(watch.record(subsequent, directory), "ALARM")
            self.assertEqual(
                [json.loads(x)["event"] for x in
                 (directory / "events.jsonl").read_text().splitlines()],
                ["ALARM"],
            )


    def test_persistent_heartbeat_read_error_preserves_new_confirmed_p1(self):
        import json
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            confirmed = watch.evaluate(
                NOW, self.fixtures(cache="max-age=3600")
            )
            self.assertEqual(confirmed["status"], "ALARM")
            real_read = Path.read_text
            denied = []

            def unreadable_heartbeat(path, *args, **kwargs):
                if path == directory / "heartbeat.json":
                    denied.append(True)
                    raise PermissionError("simulated heartbeat read access denied")
                return real_read(path, *args, **kwargs)

            with patch.object(Path, "read_text", new=unreadable_heartbeat):
                self.assertEqual(watch.record(confirmed, directory), "ALARM")
                older = watch.evaluate(
                    NOW - timedelta(minutes=1), self.fixtures()
                )
                self.assertIsNone(watch.record(older, directory))

            self.assertEqual(len(denied), 2)
            state = json.loads((directory / "state.json").read_text())
            self.assertEqual([x[0] for x in state["severe"]],
                             ["frontend_cache"])
            self.assertEqual(state["checked_at_utc"],
                             NOW.isoformat().replace("+00:00", "Z"))
            heartbeat = json.loads((directory / "heartbeat.json").read_text())
            self.assertEqual(heartbeat["status"], "ALARM")
            self.assertEqual(
                [json.loads(x)["event"] for x in
                 (directory / "events.jsonl").read_text().splitlines()],
                ["ALARM"],
            )

    def test_recovery_journal_waits_for_durable_state_and_heartbeat(self):
        import json
        import signal
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            alarm = watch.evaluate(
                NOW, self.fixtures(cache="max-age=3600")
            )
            self.assertEqual(watch.record(alarm, directory), "ALARM")
            recovery = watch.evaluate(
                NOW + timedelta(minutes=1), self.fixtures()
            )
            actual_write = watch.write_json
            failures = []

            def fail_first_recovered_state(path, value):
                if path.name == "state.json" and not failures:
                    failures.append(True)
                    raise OSError("simulated recovery state replace failure")
                return actual_write(path, value)

            with patch.object(
                watch, "write_json", side_effect=fail_first_recovered_state
            ), patch.object(signal, "alarm", return_value=0):
                self.assertEqual(watch._persist_cycle(recovery, directory), 2)

            self.assertEqual(failures, [True])
            events = [json.loads(x)["event"] for x in
                      (directory / "events.jsonl").read_text().splitlines()]
            self.assertEqual(events, ["ALARM", "MONITOR_DATA_FAILURE"])
            persisted = json.loads((directory / "state.json").read_text())
            self.assertEqual([x[0] for x in persisted["severe"]],
                             ["frontend_cache"])
            heartbeat = json.loads((directory / "heartbeat.json").read_text())
            self.assertEqual(heartbeat["status"], "ALARM")

            later = watch.evaluate(
                datetime.now(timezone.utc) + timedelta(minutes=1),
                self.fixtures(),
            )
            self.assertEqual(watch.record(later, directory), "RECOVERY")
            events = [json.loads(x)["event"] for x in
                      (directory / "events.jsonl").read_text().splitlines()]
            self.assertEqual(events, [
                "ALARM", "MONITOR_DATA_FAILURE", "RECOVERY",
            ])



    def test_unreadable_state_still_observes_and_heartbeats_new_product_p1(self):
        import json
        import signal
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            healthy = watch.evaluate(NOW, self.fixtures())
            self.assertIsNone(watch.record(healthy, directory))
            state_before = (directory / "state.json").read_bytes()
            base_reader = self.fixtures(cache="max-age=3600")
            attempted = []
            actual_read = Path.read_text

            def denied_state(path, *args, **kwargs):
                if path == directory / "state.json":
                    raise PermissionError("simulated persistent state read denial")
                return actual_read(path, *args, **kwargs)

            def observing(name):
                attempted.append(name)
                return base_reader(name)

            actual_evaluate = watch.evaluate

            def using_fixture(now, *, previous_main_observation=None, issue_sink=None):
                return actual_evaluate(
                    now, observing,
                    previous_main_observation=previous_main_observation,
                    issue_sink=issue_sink,
                )

            with patch.object(Path, "read_text", new=denied_state), patch.object(
                watch, "evaluate", side_effect=using_fixture,
            ), patch.object(signal, "alarm", return_value=0):
                observed = watch._evaluate_cycle(directory)
                self.assertIn("frontend", attempted)
                self.assertIn(
                    "frontend_cache",
                    [issue["code"] for issue in observed["issues"]],
                )
                self.assertEqual(observed["status"], "ALARM")
                self.assertEqual(watch._persist_cycle(observed, directory), 2)

            self.assertEqual((directory / "state.json").read_bytes(), state_before)
            heartbeat = json.loads((directory / "heartbeat.json").read_text())
            self.assertEqual(heartbeat["status"], "ALARM")
            self.assertIn("frontend_cache", [i["code"] for i in heartbeat["issues"]])
            self.assertIn("unverifiable_state", [i["code"] for i in heartbeat["issues"]])
            self.assertFalse((directory / "events.jsonl").exists())



    def test_recovery_heartbeat_replace_failure_retains_transition_intent(self):
        import json
        import signal
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            alarm = watch.evaluate(
                NOW, self.fixtures(cache="public, max-age=30")
            )
            self.assertEqual(watch.record(alarm, directory), "ALARM")
            recovery = watch.evaluate(
                NOW + timedelta(minutes=1), self.fixtures()
            )
            original_write = watch.write_json
            failures = []

            def fail_once_on_recovery_heartbeat(path, value):
                if path.name == "heartbeat.json" and not failures:
                    failures.append(True)
                    raise OSError("recovery heartbeat replace unavailable")
                return original_write(path, value)

            with patch.object(
                watch, "write_json", side_effect=fail_once_on_recovery_heartbeat
            ), patch.object(signal, "alarm", return_value=0):
                self.assertEqual(watch._persist_cycle(recovery, directory), 2)

            self.assertEqual(failures, [True])
            state = json.loads((directory / "state.json").read_text())
            heartbeat = json.loads((directory / "heartbeat.json").read_text())
            self.assertIn("frontend_cache", [row[0] for row in state["severe"]])
            self.assertEqual(heartbeat["status"], "ALARM")
            self.assertEqual(
                [json.loads(line)["event"] for line in
                 (directory / "events.jsonl").read_text().splitlines()],
                ["ALARM", "MONITOR_DATA_FAILURE"],
            )
            # The best-effort retry uses wall-clock now; a later live poll
            # must carry a newer timestamp than that retry's heartbeat.
            later = watch.evaluate(
                datetime.now(timezone.utc) + timedelta(minutes=1),
                self.fixtures(),
            )
            self.assertEqual(watch.record(later, directory), "RECOVERY")
            self.assertEqual(
                [json.loads(line)["event"] for line in
                 (directory / "events.jsonl").read_text().splitlines()],
                ["ALARM", "MONITOR_DATA_FAILURE", "RECOVERY"],
            )
            self.assertEqual(
                json.loads((directory / "state.json").read_text())["severe"], []
            )

    def test_transient_state_read_preserves_new_main_and_old_mismatch_anchors(self):
        import json

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            starting = watch.evaluate(
                NOW, self.fixtures(frontend=OLD, api=OLD)
            )
            self.assertIsNone(watch.record(starting, directory))
            next_main = "c" * 40
            next_now = NOW + timedelta(minutes=1)
            observed = watch.evaluate(
                next_now,
                self.fixtures(frontend=OLD, api=OLD, main=next_main),
            )
            observed["issues"].append({
                "code": "unverifiable_state", "component": "prior state",
                "actual": "PermissionError", "expected": "readable prior state",
                "source": str(directory / "state.json"), "severity": "P2",
            })
            observed["status"] = "MONITOR_DATA_FAILURE"
            self.assertEqual(
                watch.record(observed, directory), "MONITOR_DATA_FAILURE"
            )
            state = json.loads((directory / "state.json").read_text())
            anchor = state["main_observation"]
            self.assertEqual(anchor["sha"], next_main)
            self.assertEqual(
                anchor["first_seen"], next_now.isoformat().replace("+00:00", "Z")
            )
            self.assertEqual(anchor["diverged_since"], {
                "frontend": NOW.isoformat().replace("+00:00", "Z"),
                "api": NOW.isoformat().replace("+00:00", "Z"),
            })

    def test_transient_state_read_carries_only_unresolved_overdue_mismatch(self):
        import json

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            old = watch.evaluate(
                NOW, self.fixtures(frontend=OLD, api=OLD)
            )
            self.assertIsNone(watch.record(old, directory))
            current = "c" * 40
            elapsed = NOW + timedelta(minutes=46)
            observed = watch.evaluate(
                elapsed,
                self.fixtures(frontend=current, api=OLD, main=current),
            )
            observed["issues"].append({
                "code": "unverifiable_state", "component": "prior state",
                "actual": "PermissionError", "expected": "readable prior state",
                "source": str(directory / "state.json"), "severity": "P2",
            })
            observed["status"] = "MONITOR_DATA_FAILURE"
            self.assertEqual(watch.record(observed, directory), "ALARM")
            state = json.loads((directory / "state.json").read_text())
            anchor = state["main_observation"]
            self.assertEqual(anchor["sha"], current)
            self.assertEqual(
                anchor["diverged_since"],
                {"api": NOW.isoformat().replace("+00:00", "Z")},
            )
            # API differs from freshly advanced main and frontend; this is
            # both a cross-product divergence and an overdue main mismatch.
            self.assertEqual(
                [row[0] for row in state["severe"]],
                ["frontend_api_diverge", "stale_api"],
            )
            self.assertEqual(
                [issue["code"] for issue in observed["issues"]
                 if issue["severity"] == "P1"],
                ["frontend_api_diverge", "stale_api"],
            )



    def test_transient_state_read_unavailable_api_retains_prior_mismatch_anchor(self):
        import json

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            initial = watch.evaluate(
                NOW, self.fixtures(frontend=OLD, api=OLD)
            )
            self.assertIsNone(watch.record(initial, directory))
            first = watch.read_previous_main_observation(directory)
            self.assertEqual(first["diverged_since"], {
                "frontend": NOW.isoformat().replace("+00:00", "Z"),
                "api": NOW.isoformat().replace("+00:00", "Z"),
            })

            later = NOW + timedelta(minutes=4)
            fixtures = self.fixtures(frontend=MAIN, api=OLD)

            def unavailable_api(name):
                if name == "api":
                    raise PermissionError("transient API observation failure")
                return fixtures(name)

            observed = watch.evaluate(later, unavailable_api)
            # Simulate the state read failing once during preliminary
            # evaluation, but succeeding under record()'s exclusive lock.
            observed["issues"].append({
                "code": "unverifiable_state", "component": "prior state",
                "actual": "PermissionError", "expected": "readable prior state",
                "source": str(directory / "state.json"), "severity": "P2",
            })
            self.assertIn("unavailable_api", [i["code"] for i in observed["issues"]])
            self.assertEqual(observed["main_observation"]["sha"], MAIN)
            self.assertNotIn("diverged_since", observed["main_observation"])
            self.assertEqual(watch.record(observed, directory), "MONITOR_DATA_FAILURE")
            persisted = json.loads((directory / "state.json").read_text())
            self.assertEqual(persisted["main_observation"]["diverged_since"], {
                "api": NOW.isoformat().replace("+00:00", "Z"),
            })
            self.assertNotIn("frontend", persisted["main_observation"]["diverged_since"])

            # The old API anchor must be authoritative as soon as the API is
            # readable again; its 45-minute grace must not restart.
            next_poll = watch.evaluate(
                NOW + timedelta(minutes=46),
                self.fixtures(frontend=MAIN, api=OLD),
                previous_main_observation=watch.read_previous_main_observation(directory),
            )
            self.assertIn("stale_api", [i["code"] for i in next_poll["issues"]])
            self.assertNotIn("stale_frontend", [i["code"] for i in next_poll["issues"]])



    def test_main_watch_deadline_keeps_independent_product_p1(self):
        import json
        import signal
        from unittest.mock import patch

        for mode, frontend, api, expected in (
            ("diverged", OLD, MAIN, "frontend_api_diverge"),
            ("invalid_api", MAIN, "bad-sha", "invalid_commit_api"),
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                attempted = []
                source = self.fixtures(frontend=frontend, api=api)
                original_evaluate = watch.evaluate

                def deadline_on_main(name):
                    attempted.append(name)
                    if name == "main":
                        raise watch.WatchRuntimeExceeded(
                            "simulated total deadline on GitHub main"
                        )
                    return source(name)

                def evaluate_fixture(
                    now, *, previous_main_observation=None, issue_sink=None
                ):
                    return original_evaluate(
                        now, deadline_on_main,
                        previous_main_observation=previous_main_observation,
                        issue_sink=issue_sink,
                    )

                with patch.object(
                    watch, "evaluate", side_effect=evaluate_fixture
                ):
                    report = watch._evaluate_cycle(directory)
                self.assertEqual(attempted, ["frontend", "api", "main"])
                self.assertEqual(report["status"], "ALARM")
                codes = [issue["code"] for issue in report["issues"]]
                self.assertIn(expected, codes)
                self.assertIn("watcher_execution_failed", codes)
                with patch.object(signal, "alarm", return_value=0):
                    self.assertEqual(watch._persist_cycle(report, directory), 2)
                state = json.loads((directory / "state.json").read_text())
                self.assertIn(expected, [entry[0] for entry in state["severe"]])
                heartbeat = json.loads((directory / "heartbeat.json").read_text())
                self.assertEqual(heartbeat["status"], "ALARM")

    def test_invalid_utf8_heartbeat_cannot_hide_new_confirmed_p1(self):
        import json

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.assertIsNone(watch.record(watch.evaluate(
                NOW, self.fixtures()
            ), directory))
            (directory / "heartbeat.json").write_bytes(b"\xff")
            confirmed = watch.evaluate(
                NOW + timedelta(minutes=1),
                self.fixtures(cache="max-age=3600"),
            )
            self.assertEqual(watch.record(confirmed, directory), "ALARM")
            state = json.loads((directory / "state.json").read_text())
            self.assertIn("frontend_cache", [item[0] for item in state["severe"]])
            heartbeat = json.loads((directory / "heartbeat.json").read_text())
            self.assertEqual(heartbeat["status"], "ALARM")
            self.assertIn("frontend_cache", [i["code"] for i in heartbeat["issues"]])

    def test_invalid_utf8_state_keeps_live_observation_and_original_bytes(self):
        import json
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.assertIsNone(watch.record(watch.evaluate(
                NOW, self.fixtures()
            ), directory))
            (directory / "state.json").write_bytes(b"\xff\xfe")
            source = self.fixtures(cache="max-age=3600")
            attempted = []
            original_evaluate = watch.evaluate

            def observe_product(name):
                attempted.append(name)
                return source(name)

            def evaluate_fixture(
                now, *, previous_main_observation=None, issue_sink=None
            ):
                return original_evaluate(
                    now, observe_product,
                    previous_main_observation=previous_main_observation,
                    issue_sink=issue_sink,
                )

            with patch.object(watch, "evaluate", side_effect=evaluate_fixture):
                report = watch._evaluate_cycle(directory)
            self.assertIn("frontend", attempted)
            self.assertEqual(report["status"], "ALARM")
            self.assertIn("frontend_cache", [i["code"] for i in report["issues"]])
            self.assertIn("unverifiable_state", [i["code"] for i in report["issues"]])
            self.assertEqual(
                watch.record(report, directory), "MONITOR_DATA_FAILURE"
            )
            self.assertEqual((directory / "state.json").read_bytes(), b"\xff\xfe")
            heartbeat = json.loads((directory / "heartbeat.json").read_text())
            self.assertEqual(heartbeat["status"], "ALARM")
            self.assertIn("frontend_cache", [i["code"] for i in heartbeat["issues"]])


    def test_failed_alarm_journal_append_retries_original_transition(self):
        import json
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.assertIsNone(watch.record(
                watch.evaluate(NOW, self.fixtures()), directory,
            ))
            original = watch.evaluate(
                NOW + timedelta(minutes=1),
                self.fixtures(cache="public, max-age=30"),
            )
            normal_open = Path.open
            failed = []
            def fail_first_append(path, *args, **kwargs):
                if path == directory / "events.jsonl" and not failed:
                    failed.append(True)
                    raise OSError("temporary journal append failure")
                return normal_open(path, *args, **kwargs)
            with patch.object(Path, "open", new=fail_first_append):
                self.assertEqual(watch.record(original, directory), "ALARM")
            self.assertEqual(failed, [True])
            followup = watch.evaluate(
                NOW + timedelta(minutes=2),
                self.fixtures(cache="public, max-age=30"),
            )
            self.assertEqual(watch.record(followup, directory), "ALARM")
            events = [json.loads(line) for line in
                      (directory / "events.jsonl").read_text().splitlines()]
            self.assertEqual([x["event"] for x in events], ["ALARM"])
            self.assertEqual(events[0]["checked_at_utc"], original["checked_at_utc"])
            self.assertIsNone(watch.record(
                watch.evaluate(
                    NOW + timedelta(minutes=3),
                    self.fixtures(cache="public, max-age=30"),
                ), directory,
            ))
            self.assertEqual(
                [json.loads(line)["event"] for line in
                 (directory / "events.jsonl").read_text().splitlines()],
                ["ALARM"],
            )

    def test_recovery_after_state_rename_parent_fsync_fault_is_retried(self):
        import json
        import os
        import signal
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            alarm = watch.evaluate(
                NOW, self.fixtures(cache="public, max-age=30"),
            )
            self.assertEqual(watch.record(alarm, directory), "ALARM")
            recovered = watch.evaluate(
                NOW + timedelta(minutes=1), self.fixtures(),
            )
            fsync = watch.os.fsync
            last_file = [None]
            failures = []
            def fail_post_replace_directory_fsync(fd):
                name = os.readlink(f"/proc/self/fd/{fd}")
                if name.endswith(".tmp"):
                    last_file[0] = name
                if (name == str(directory)
                        and last_file[0] == str(directory / "state.json.tmp")
                        and not failures):
                    failures.append(True)
                    raise OSError("state rename succeeded, parent fsync failed")
                return fsync(fd)
            with patch.object(
                watch.os, "fsync",
                side_effect=fail_post_replace_directory_fsync,
            ), patch.object(signal, "alarm", return_value=0):
                self.assertEqual(watch._persist_cycle(recovered, directory), 2)
            self.assertEqual(failures, [True])
            self.assertNotIn("RECOVERY", [json.loads(x)["event"] for x in
                (directory / "events.jsonl").read_text().splitlines()])
            self.assertEqual(
                watch.record(watch.evaluate(
                    datetime.now(timezone.utc) + timedelta(minutes=1),
                    self.fixtures(),
                ), directory),
                "RECOVERY",
            )
            events = [json.loads(x) for x in
                      (directory / "events.jsonl").read_text().splitlines()]
            self.assertEqual([x["event"] for x in events].count("RECOVERY"), 1)
            self.assertEqual(
                [x for x in events if x["event"] == "RECOVERY"][0][
                    "checked_at_utc"
                ], recovered["checked_at_utc"],
            )
            self.assertEqual(
                json.loads((directory / "state.json").read_text())["severe"], [],
            )


    def test_replayed_recovery_dedupes_after_postappend_snapshot_failure(self):
        import json
        import signal
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            alarm = watch.evaluate(
                NOW, self.fixtures(cache="public, max-age=30"),
            )
            self.assertEqual(watch.record(alarm, directory), "ALARM")
            recovered = watch.evaluate(
                NOW + timedelta(minutes=1), self.fixtures(),
            )
            actual_write = watch.write_json
            failures = []
            def fail_final_snapshot(path, value):
                if (path.name == "state.json" and
                        "pending_transitions" not in value and not failures):
                    failures.append(True)
                    raise OSError("outbox-clear failed after durable event append")
                return actual_write(path, value)
            with patch.object(
                watch, "write_json", side_effect=fail_final_snapshot,
            ), patch.object(signal, "alarm", return_value=0):
                self.assertEqual(watch._persist_cycle(recovered, directory), 2)
            self.assertEqual(failures, [True])
            saved = [json.loads(line)["event"] for line in
                     (directory / "events.jsonl").read_text().splitlines()]
            self.assertEqual(saved.count("RECOVERY"), 1)
            later = watch.evaluate(
                datetime.now(timezone.utc) + timedelta(minutes=1),
                self.fixtures(),
            )
            self.assertIsNone(watch.record(later, directory))
            events = [json.loads(line)["event"] for line in
                      (directory / "events.jsonl").read_text().splitlines()]
            self.assertEqual(events.count("RECOVERY"), 1)
            self.assertFalse(
                json.loads((directory / "state.json").read_text()).get(
                    "pending_transitions",
                ),
            )



    def test_replayed_alarm_reconciles_when_journal_exceeds_sixteen_megabytes(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            # Append a valid 17-MiB history BEFORE the incident. Dedupe
            # must scan to the actual event beyond the old 16-MiB bound.
            with (directory / "events.jsonl").open("ab") as out:
                out.write(b'{"event":"MONITOR_DATA_FAILURE"}\n' * 550_000)
            alarm = watch.evaluate(
                NOW, self.fixtures(cache="public, max-age=30"),
            )
            self.assertEqual(watch.record(alarm, directory), "ALARM")
            with (directory / "events.jsonl").open("rb") as src:
                src.seek(0, 2)
                src.seek(-4096, 2)
                original = json.loads(src.read().splitlines()[-1])
            pending = [{"event": "ALARM", "payload": original}]
            for anchor in ("state.json", "heartbeat.json"):
                item_path = directory / anchor
                saved = json.loads(item_path.read_text())
                saved["pending_transitions"] = pending
                watch.write_json(item_path, saved)
            journal_size = (directory / "events.jsonl").stat().st_size
            self.assertGreater(journal_size, 16 * 1024 * 1024)
            followup = watch.evaluate(
                NOW + timedelta(minutes=1),
                self.fixtures(cache="public, max-age=30"),
            )
            self.assertIsNone(watch.record(followup, directory))
            self.assertEqual((directory / "events.jsonl").stat().st_size,
                             journal_size)
            self.assertFalse(json.loads(
                (directory / "state.json").read_text()
            ).get("pending_transitions"))

    def test_journal_replay_does_not_match_malformed_line_suffix(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            payload = {
                "event": "ALARM", "status": "ALARM",
                "checked_at_utc": NOW.isoformat(), "issues": [],
            }
            entry = {"event": "ALARM", "payload": payload}
            expected = (
                json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode()
            # The next bounded chunk happens to look like a valid JSONL row
            # but starts in the middle of a single malformed long row.
            with (directory / "events.jsonl").open("wb") as out:
                out.write(b"x" * (len(expected) + 1) + expected)
            self.assertFalse(watch._transition_already_journaled(directory, entry))


    def test_failed_snapshot_retry_keeps_new_main_grace_and_mismatch_anchor(self):
        import json
        import signal
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            initial = watch.evaluate(
                NOW, self.fixtures(frontend=OLD, api=OLD, main=OLD),
            )
            self.assertIsNone(watch.record(initial, directory))
            observed = watch.evaluate(
                NOW + timedelta(minutes=1),
                self.fixtures(frontend=OLD, api=OLD, main=MAIN),
                previous_main_observation=watch.read_previous_main_observation(directory),
            )
            self.assertEqual(observed["status"], "HEALTHY")
            expected_anchor = observed["main_observation"]
            self.assertEqual(expected_anchor["sha"], MAIN)
            self.assertEqual(set(expected_anchor["diverged_since"]),
                             {"frontend", "api"})
            actual_write = watch.write_json
            faults = []

            def fail_once(path, payload):
                if path == directory / "state.json" and not faults:
                    faults.append(True)
                    raise OSError("first new-main state write was interrupted")
                return actual_write(path, payload)

            with patch.object(watch, "write_json", side_effect=fail_once), \
                 patch.object(signal, "alarm", return_value=0):
                self.assertEqual(watch._persist_cycle(observed, directory), 2)

            self.assertEqual(faults, [True])
            persisted = json.loads((directory / "state.json").read_text())
            self.assertEqual(persisted["main_observation"], expected_anchor)
            resumed = watch.evaluate(
                NOW + timedelta(minutes=47),
                self.fixtures(frontend=OLD, api=OLD, main=MAIN),
                previous_main_observation=watch.read_previous_main_observation(directory),
            )
            self.assertEqual(
                {i["code"] for i in resumed["issues"] if i["severity"] == "P1"},
                {"stale_frontend", "stale_api"},
            )


    def test_pending_recovery_orders_before_independent_new_alarm(self):
        import json
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            first = watch.evaluate(
                NOW, self.fixtures(cache="public, max-age=30"),
            )
            self.assertEqual(watch.record(first, directory), "ALARM")
            recovered_a = watch.evaluate(
                NOW + timedelta(minutes=1), self.fixtures(),
            )
            real_open = Path.open
            failures = []

            def fail_recovery_append(path, *args, **kwargs):
                if path == directory / "events.jsonl" and "ab" in args and not failures:
                    failures.append(True)
                    raise OSError("first RECOVERY append failed")
                return real_open(path, *args, **kwargs)

            with patch.object(Path, "open", new=fail_recovery_append):
                self.assertEqual(watch.record(recovered_a, directory), "RECOVERY")
            self.assertEqual(failures, [True])

            different_alarm = watch.evaluate(
                NOW + timedelta(minutes=2),
                self.fixtures(frontend=OLD, api=MAIN),
            )
            self.assertEqual(watch.record(different_alarm, directory), "ALARM")
            recovered_b = watch.evaluate(
                NOW + timedelta(minutes=3), self.fixtures(),
            )
            self.assertEqual(watch.record(recovered_b, directory), "RECOVERY")
            history = [json.loads(line) for line in
                       (directory / "events.jsonl").read_text().splitlines()]
            self.assertEqual(
                [entry["event"] for entry in history],
                ["ALARM", "RECOVERY", "ALARM", "RECOVERY"],
            )
            self.assertEqual(
                history[1]["checked_at_utc"], recovered_a["checked_at_utc"],
            )
            self.assertEqual(
                history[3]["checked_at_utc"], recovered_b["checked_at_utc"],
            )

    def test_crash_after_durable_alarm_before_outbox_clear_is_recovered(self):
        import json
        from unittest.mock import patch

        for cleared in (False, True):
            with self.subTest(recovered_next_poll=cleared):
                with tempfile.TemporaryDirectory() as tmp:
                    directory = Path(tmp)
                    first = watch.evaluate(
                        NOW, self.fixtures(cache="public, max-age=30"),
                    )
                    real_write = watch.write_json
                    interrupted = []

                    def kill_after_alarm_append(path, payload):
                        if (path == directory / "state.json"
                                and (directory / "events.jsonl").exists()
                                and not interrupted):
                            journal = (directory / "events.jsonl").read_text()
                            if '"event":"ALARM"' in journal:
                                interrupted.append(True)
                                raise KeyboardInterrupt("hard stop after fsynced ALARM")
                        return real_write(path, payload)

                    with patch.object(
                        watch, "write_json", side_effect=kill_after_alarm_append,
                    ), self.assertRaises(KeyboardInterrupt):
                        watch.record(first, directory)
                    self.assertEqual(interrupted, [True])
                    next_poll = watch.evaluate(
                        NOW + timedelta(minutes=1),
                        self.fixtures(
                            cache=("public, no-store" if cleared
                                   else "public, max-age=30"),
                        ),
                    )
                    self.assertEqual(
                        watch.record(next_poll, directory),
                        ("RECOVERY" if cleared else None),
                    )
                    history = [json.loads(line) for line in
                               (directory / "events.jsonl").read_text().splitlines()]
                    self.assertEqual(
                        [entry["event"] for entry in history],
                        (["ALARM", "RECOVERY"] if cleared else ["ALARM"]),
                    )
                    self.assertEqual(
                        history[0]["checked_at_utc"], first["checked_at_utc"],
                    )


    def test_full_outbox_drains_durable_prefix_before_next_alarm(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.assertIsNone(
                watch.record(watch.evaluate(NOW, self.fixtures()), directory),
            )
            # Simulate sixteen individually valid ALARMs queued while the
            # event journal was unwritable. It is writable again now.
            pending = []
            for index in range(16):
                report = watch.evaluate(
                    NOW + timedelta(seconds=index + 1),
                    self.fixtures(cache="public, max-age=30"),
                )
                pending.append({
                    "event": "ALARM",
                    "payload": {
                        "event": "ALARM", **report,
                        "issue_url": "https://github.com/heimgewebe/commonthing/issues/1939",
                    },
                })
            for name in ("state.json", "heartbeat.json"):
                target = directory / name
                saved = json.loads(target.read_text())
                saved["pending_transitions"] = pending
                watch.write_json(target, saved)
            new_alarm = watch.evaluate(
                NOW + timedelta(minutes=1),
                self.fixtures(cache="public, max-age=30"),
            )
            self.assertEqual(watch.record(new_alarm, directory), "ALARM")
            history = [json.loads(line) for line in
                       (directory / "events.jsonl").read_text().splitlines()]
            self.assertEqual(len(history), 17)
            self.assertEqual([x["event"] for x in history], ["ALARM"] * 17)
            self.assertEqual(
                [x["checked_at_utc"] for x in history[:16]],
                [x["payload"]["checked_at_utc"] for x in pending],
            )
            state = json.loads((directory / "state.json").read_text())
            self.assertEqual([x[0] for x in state["severe"]], ["frontend_cache"])
            self.assertFalse(state.get("pending_transitions"))


    def test_full_outbox_drain_fsync_failure_preserves_old_and_new_p1(self):
        import json
        import os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            watch.record(watch.evaluate(NOW, self.fixtures()), directory)
            pending = [
                {
                    "event": "ALARM",
                    "payload": {
                        "event": "ALARM",
                        **watch.evaluate(
                            NOW + timedelta(seconds=index + 1),
                            self.fixtures(cache="public, max-age=30"),
                        ),
                        "issue_url": "https://github.com/heimgewebe/commonthing/issues/1939",
                    },
                }
                for index in range(16)
            ]
            for name in ("state.json", "heartbeat.json"):
                path = directory / name
                old = json.loads(path.read_text())
                old["pending_transitions"] = pending
                watch.write_json(path, old)
            first_alarm = watch.evaluate(
                NOW + timedelta(minutes=1),
                self.fixtures(cache="public, max-age=30"),
            )
            real_fsync = watch.os.fsync
            failure = []

            def fail_first_journal_fsync(fd):
                file = os.readlink(f"/proc/self/fd/{fd}")
                if file == str(directory / "events.jsonl") and not failure:
                    failure.append(True)
                    raise OSError("journal not yet writable")
                return real_fsync(fd)

            with patch.object(watch.os, "fsync", side_effect=fail_first_journal_fsync):
                self.assertEqual(
                    watch.record(first_alarm, directory), "MONITOR_DATA_FAILURE",
                )
            self.assertEqual(failure, [True])
            self.assertEqual((directory / "events.jsonl").read_bytes(), b"")
            state = json.loads((directory / "state.json").read_text())
            # The failed fsync cannot erase the seventeenth confirmed ALARM.
            self.assertEqual(len(state["pending_transitions"]), 17)
            self.assertEqual(
                state["pending_transitions"][-1]["payload"]["checked_at_utc"],
                first_alarm["checked_at_utc"],
            )
            heartbeat = json.loads((directory / "heartbeat.json").read_text())
            self.assertEqual(len(heartbeat["pending_transitions"]), 17)
            self.assertEqual(heartbeat["status"], "ALARM")
            self.assertIn("frontend_cache", [x["code"] for x in heartbeat["issues"]])
            restored = watch.evaluate(
                NOW + timedelta(minutes=2),
                self.fixtures(cache="public, max-age=30"),
            )
            self.assertEqual(watch.record(restored, directory), "ALARM")
            history = [json.loads(line) for line in
                       (directory / "events.jsonl").read_text().splitlines()]
            self.assertEqual(len(history), 17)
            self.assertEqual(
                [x["checked_at_utc"] for x in history[:16]],
                [x["payload"]["checked_at_utc"] for x in pending],
            )

    def test_full_outbox_drain_crash_before_next_snapshot_dedupes(self):
        import json
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            watch.record(watch.evaluate(NOW, self.fixtures()), directory)
            pending = [
                {
                    "event": "ALARM",
                    "payload": {
                        "event": "ALARM",
                        **watch.evaluate(
                            NOW + timedelta(seconds=index + 1),
                            self.fixtures(cache="public, max-age=30"),
                        ),
                        "issue_url": "https://github.com/heimgewebe/commonthing/issues/1939",
                    },
                }
                for index in range(16)
            ]
            for name in ("state.json", "heartbeat.json"):
                path = directory / name
                old = json.loads(path.read_text())
                old["pending_transitions"] = pending
                watch.write_json(path, old)
            first = watch.evaluate(
                NOW + timedelta(minutes=1),
                self.fixtures(cache="public, max-age=30"),
            )
            real_write = watch.write_json
            stopped = []

            def stop_after_drain(path, payload):
                if (path == directory / "state.json"
                        and not stopped
                        and (directory / "events.jsonl").exists()
                        and len((directory / "events.jsonl").read_text().splitlines()) == 16):
                    stopped.append(True)
                    raise KeyboardInterrupt("crash after old durable drain")
                return real_write(path, payload)

            with patch.object(watch, "write_json", side_effect=stop_after_drain):
                with self.assertRaises(KeyboardInterrupt):
                    watch.record(first, directory)
            self.assertEqual(stopped, [True])
            resumed = watch.evaluate(
                NOW + timedelta(minutes=2),
                self.fixtures(cache="public, max-age=30"),
            )
            self.assertEqual(watch.record(resumed, directory), "ALARM")
            history = [json.loads(line) for line in
                       (directory / "events.jsonl").read_text().splitlines()]
            self.assertEqual(len(history), 17)
            self.assertEqual([x["event"] for x in history], ["ALARM"] * 17)
            self.assertFalse(json.loads(
                (directory / "state.json").read_text(),
            ).get("pending_transitions"))


    def test_crash_mid_journal_append_repairs_unterminated_tail(self):
        import json
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            first = watch.evaluate(
                NOW, self.fixtures(cache="public, max-age=30"),
            )
            real_open = Path.open
            killed = []

            class KillMidWrite:
                def __init__(self, stream):
                    self.stream = stream

                def __enter__(self):
                    self.stream.__enter__()
                    return self

                def __exit__(self, *args):
                    return self.stream.__exit__(*args)

                def fileno(self):
                    return self.stream.fileno()

                def write(self, data):
                    if not killed:
                        killed.append(True)
                        self.stream.write(data[:7])
                        raise KeyboardInterrupt("power loss mid journal write")
                    return self.stream.write(data)

            def interrupted_open(path, *args, **kwargs):
                stream = real_open(path, *args, **kwargs)
                if path == directory / "events.jsonl" and "ab" in args:
                    return KillMidWrite(stream)
                return stream

            with patch.object(Path, "open", new=interrupted_open):
                with self.assertRaises(KeyboardInterrupt):
                    watch.record(first, directory)
            self.assertEqual(killed, [True])
            journal = directory / "events.jsonl"
            self.assertEqual(journal.read_bytes(), b'{"check')
            persisted = json.loads((directory / "state.json").read_text())
            self.assertEqual(
                [x["event"] for x in persisted["pending_transitions"]],
                ["ALARM"],
            )

            repeat = watch.evaluate(
                NOW + timedelta(minutes=1),
                self.fixtures(cache="public, max-age=30"),
            )
            self.assertEqual(watch.record(repeat, directory), "ALARM")
            data = journal.read_bytes()
            self.assertTrue(data.endswith(b"\n"))
            self.assertEqual(len(data.splitlines()), 1)
            decoded = json.loads(data)
            self.assertEqual(decoded["event"], "ALARM")
            self.assertEqual(decoded["checked_at_utc"], first["checked_at_utc"])

if __name__ == "__main__":
    unittest.main()