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
                (directory / "state.json").write_text(json.dumps(state), encoding="utf-8")
                healthy = watch.evaluate(NOW, self.fixtures())
                self.assertIsNone(watch.record(healthy, directory))
                self.assertTrue((directory / "heartbeat.json").is_file())
                persisted = json.loads((directory / "state.json").read_text())
                self.assertEqual(persisted["severe"], [])
                self.assertEqual(persisted["uncertain"], [])
                self.assertFalse((directory / "events.jsonl").exists())

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


if __name__ == "__main__":
    unittest.main()
