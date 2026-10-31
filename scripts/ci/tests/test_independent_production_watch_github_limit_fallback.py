#!/usr/bin/env python3
"""Git smart protocol may verify the public main ref during GitHub REST throttling."""
from __future__ import annotations

from datetime import datetime, timezone
from importlib import util
from pathlib import Path
import subprocess
import unittest
from unittest import mock
from urllib.error import HTTPError

SCRIPT = Path(__file__).resolve().parents[2] / "ops/independent_production_watch.py"
SPEC = util.spec_from_file_location("watch_github_rate_fallback", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
watch = util.module_from_spec(SPEC)
SPEC.loader.exec_module(watch)
SHA = "a" * 40
NOW = datetime(2026, 10, 31, 6, 20, tzinfo=timezone.utc)


class GitMainRateFallbackTests(unittest.TestCase):
    @staticmethod
    def limited_url(request, *args, **kwargs):
        raise HTTPError(request.full_url, 403, "anonymous rate limit", None, None)

    def test_http_403_uses_only_exact_public_git_ref(self):
        returned = subprocess.CompletedProcess(
            ["git", "ls-remote"], 0, stdout=f"{SHA}\trefs/heads/main\n", stderr=""
        )
        with (
            mock.patch.object(watch, "urlopen", side_effect=self.limited_url),
            mock.patch("subprocess.run", return_value=returned) as runner,
        ):
            value, cache = watch.fetch("main")
        self.assertEqual(value["sha"], SHA)
        self.assertEqual(value.get("observation_transport"), "git_https")
        self.assertEqual(cache, "")
        self.assertEqual(runner.call_count, 1)
        argv = runner.call_args.args[0]
        self.assertIn("ls-remote", argv)
        self.assertIn("https://github.com/heimgewebe/commonthing.git", argv)
        self.assertIn("refs/heads/main", argv)

    def test_git_fallback_main_ref_without_api_committer_does_not_fake_p2(self):
        def fixture(name):
            if name in {"frontend", "api"}:
                return {"commit": SHA}, "no-store"
            if name == "main":
                return {"sha": SHA, "observation_transport": "git_https"}, ""
            if name == "schedule":
                return {"workflow_runs": []}, ""
            raise AssertionError(name)
        value = watch.evaluate(NOW, fixture)
        self.assertEqual(value["commits"], {
            "frontend": SHA, "api": SHA, "main": SHA,
        })
        self.assertEqual(value["status"], "HEALTHY")
        self.assertNotIn("main_time_unreliable",
                         [issue["code"] for issue in value["issues"]])

    def test_invalid_public_git_ref_never_claims_healthy(self):
        returned = subprocess.CompletedProcess(
            ["git", "ls-remote"], 0, stdout=f"{SHA}\trefs/tags/changed\n", stderr=""
        )
        with (
            mock.patch.object(watch, "urlopen", side_effect=self.limited_url),
            mock.patch("subprocess.run", return_value=returned),
        ):
            with self.assertRaises(ValueError):
                watch.fetch("main")

    def test_git_fallback_is_not_used_for_failed_product_endpoints(self):
        with (
            mock.patch.object(watch, "urlopen", side_effect=self.limited_url),
            mock.patch("subprocess.run") as runner,
        ):
            with self.assertRaises(ValueError):
                watch.fetch("api")
        runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
