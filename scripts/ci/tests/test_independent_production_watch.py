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
        self.assertIn("schedule_stale", self.codes(schedule_age=46))
        self.assertNotIn("schedule_stale", self.codes(schedule_age=44))

    def test_only_actual_schedule_counts(self):
        self.assertIn("no_schedule", self.codes(schedule_event="workflow_dispatch"))

    def test_main_convergence_grace(self):
        self.assertNotIn("stale_frontend", self.codes(frontend=OLD, api=OLD, commit_age=44))
        self.assertIn("stale_frontend", self.codes(frontend=OLD, api=OLD, commit_age=46))

    def test_frontend_api_and_cache(self):
        self.assertIn("frontend_api_diverge", self.codes(frontend=OLD, api=MAIN))
        self.assertIn("frontend_cache", self.codes(cache="public, max-age=30"))
        self.assertIn("invalid_commit_api", self.codes(api="short"))

    def test_schedule_failure_and_transport_failure_are_distinct(self):
        self.assertIn("schedule_failed", self.codes(conclusion="failure"))
        def reader(name):
            if name == "api":
                raise ValueError("two reads failed: timeout")
            return self.fixtures()(name)
        result = watch.evaluate(NOW, reader)
        self.assertEqual(result["status"], "MONITOR_DATA_FAILURE")
        self.assertEqual([i["code"] for i in result["issues"]], ["unavailable_api"])

    def test_dedupe_recovery_and_unknown_not_false_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            alarm = watch.evaluate(NOW, self.fixtures(schedule_age=50))
            self.assertEqual(watch.record(alarm, directory), "ALARM")
            self.assertIsNone(watch.record(alarm, directory))
            def unreadable(name):
                if name == "schedule":
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
