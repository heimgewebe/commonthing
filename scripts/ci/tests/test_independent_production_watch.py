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
        self.assertNotIn("stale_frontend", self.codes(frontend=OLD, api=OLD, commit_age=44))
        self.assertIn("stale_frontend", self.codes(frontend=OLD, api=OLD, commit_age=46))

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
