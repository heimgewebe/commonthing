#!/usr/bin/env python3
"""Git smart protocol may verify the public main ref during GitHub REST throttling."""
from __future__ import annotations

from datetime import datetime, timezone
from importlib import util
from pathlib import Path
import os
import signal
import subprocess
import tempfile
import time
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
            mock.patch("subprocess.Popen") as runner,
        ):
            runner.return_value.communicate.return_value = (returned.stdout, "")
            runner.return_value.returncode = 0
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
            mock.patch("subprocess.Popen") as runner,
        ):
            runner.return_value.communicate.return_value = (returned.stdout, "")
            runner.return_value.returncode = 0
            with self.assertRaises(ValueError):
                watch.fetch("main")

    def test_timed_out_git_transport_leaves_no_helper_process(self):
        # A real non-interactive Git transport can leave descendant helpers
        # after Python kills only the direct subprocess on TimeoutExpired.
        with tempfile.TemporaryDirectory(prefix="watch-git-process-group-") as tmp:
            root = Path(tmp)
            helper_pid = root / "helper.pid"
            git = root / "git"
            git.write_text(
                "#!/bin/sh\n"
                "sleep 60 &\n"
                f"echo $! > '{helper_pid}'\n"
                "wait\n",
                encoding="utf-8",
            )
            git.chmod(0o700)
            environ = {"PATH": f"{root}:{os.environ.get('PATH', '')}"}
            try:
                with mock.patch.dict(os.environ, environ):
                    with self.assertRaises((ValueError, subprocess.TimeoutExpired)):
                        watch._public_main_ref_over_git(timeout_seconds=1)
                self.assertTrue(helper_pid.exists(), "fake Git helper never started")
                pid = int(helper_pid.read_text(encoding="ascii").strip())
                for _ in range(15):
                    try:
                        stat_path = Path(f"/proc/{pid}/stat")
                        if not stat_path.exists():
                            break
                        # A zombie no longer consumes CPU or network resources.
                        if stat_path.read_text().split()[2] == "Z":
                            break
                    except (FileNotFoundError, ProcessLookupError):
                        break
                    time.sleep(0.05)
                else:
                    self.fail("timed-out Git transport orphaned a live helper process")
            finally:
                if helper_pid.exists():
                    try:
                        os.kill(int(helper_pid.read_text().strip()), signal.SIGKILL)
                    except (ProcessLookupError, ValueError):
                        pass

    def test_global_watcher_alarm_kills_git_and_helper_process(self):
        # The total watcher SIGALRM is a BaseException. Previously it killed
        # only the GNU timeout supervisor and orphaned Git transport helpers.
        with tempfile.TemporaryDirectory(prefix="watch-git-global-alarm-") as tmp:
            root = Path(tmp)
            helper_pid = root / "helper.pid"
            git = root / "git"
            git.write_text(
                "#!/bin/sh\n"
                "sleep 60 &\n"
                f"echo $! > '{helper_pid}'\n"
                "wait\n",
                encoding="utf-8",
            )
            git.chmod(0o700)
            environ = {"PATH": f"{root}:{os.environ.get('PATH', '')}"}
            old_handler = signal.signal(signal.SIGALRM, watch._watch_deadline)
            try:
                with mock.patch.dict(os.environ, environ):
                    signal.setitimer(signal.ITIMER_REAL, 1.0)
                    with self.assertRaises(watch.WatchRuntimeExceeded):
                        watch._public_main_ref_over_git(timeout_seconds=10)
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, old_handler)
            self.assertTrue(helper_pid.exists(), "fake Git helper never started")
            pid = int(helper_pid.read_text(encoding="ascii").strip())
            try:
                for _ in range(15):
                    stat_path = Path(f"/proc/{pid}/stat")
                    if not stat_path.exists():
                        break
                    if stat_path.read_text().split()[2] == "Z":
                        break
                    time.sleep(0.05)
                else:
                    self.fail("global watcher alarm orphaned a live Git helper")
            finally:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_git_fallback_is_not_used_for_failed_product_endpoints(self):
        with (
            mock.patch.object(watch, "urlopen", side_effect=self.limited_url),
            mock.patch("subprocess.Popen") as runner,
        ):
            with self.assertRaises(ValueError):
                watch.fetch("api")
        runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()