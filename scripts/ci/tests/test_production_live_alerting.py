from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

OPS = Path(__file__).parents[2] / "ops"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, OPS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CLASSIFY = load("classify_production_live_state")
ALERT = load("production_alert_issue")

A, B, C, X = ("a" * 40, "b" * 40, "c" * 40, "d" * 40)
# Linear main history A <- B <- C; X is unrelated.
ORDER = [A, B, C]


def ancestor(a: str, d: str) -> bool | None:
    if a not in ORDER or d not in ORDER:
        return False
    return ORDER.index(a) <= ORDER.index(d)


def endpoint(commit: str | None, status: int = 200, error: str | None = None) -> dict:
    return {"status": status, "commit": commit, "error": error}


def receipt(frontend: str | None, api: str | None = None, *, passed: bool = False) -> dict:
    return {
        "pass": passed,
        "reasons": [] if passed else ["mismatch"],
        "frontend": endpoint(frontend),
        "api": endpoint(frontend if api is None else api),
    }


def classify(data: dict | None, expected: str, main: str = C, age: int = 5000):
    return CLASSIFY.classify(
        data,
        expected_commit=expected,
        main_commit=main,
        now=10_000,
        pending_grace_seconds=1200,
        is_ancestor=ancestor,
        commit_time=lambda _commit: 10_000 - age,
    )


class ClassifyProductionLiveStateTest(unittest.TestCase):
    def test_states(self) -> None:
        cases = {
            "current": classify(receipt(C, passed=True), C),
            "superseded": classify(receipt(C), B),
            "pending": classify(receipt(B), C, age=300),
            "stale": classify(receipt(B), C, age=1200),
            "invalid": classify(receipt(C, passed=False), C),
        }
        for state, result in cases.items():
            with self.subTest(state=state):
                self.assertEqual(result.state, state, result.reason)
        self.assertFalse(cases["current"].alert)
        self.assertFalse(cases["superseded"].alert)
        self.assertFalse(cases["pending"].alert)
        self.assertTrue(cases["stale"].alert)
        self.assertTrue(cases["invalid"].alert)

    def test_split_and_off_main_live_commits_are_divergent(self) -> None:
        self.assertEqual(classify(receipt(C, api=B), C).state, "divergent")
        self.assertEqual(classify(receipt(X), C).state, "divergent")
        # A commit newer than the expected one but not on main is no supersession.
        self.assertEqual(classify(receipt(C), A, main=B).state, "divergent")

    def test_unreadable_endpoints_are_an_outage(self) -> None:
        data = receipt(C)
        data["api"] = endpoint(None, status=0, error="timed out")
        result = classify(data, C)
        self.assertEqual(result.state, "outage")
        self.assertTrue(result.alert)
        data["api"] = endpoint(C, status=502)
        self.assertEqual(classify(data, C).state, "outage")

    def test_missing_receipt_is_a_monitor_failure(self) -> None:
        result = classify(None, C)
        self.assertEqual(result.state, "monitor_failure")
        self.assertTrue(result.alert)

    def test_unknown_commit_age_never_counts_as_pending(self) -> None:
        result = CLASSIFY.classify(
            receipt(B),
            expected_commit=C,
            main_commit=C,
            now=10_000,
            pending_grace_seconds=1200,
            is_ancestor=ancestor,
            commit_time=lambda _commit: None,
        )
        self.assertEqual(result.state, "stale")

    def test_git_helpers_read_real_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            commits = []
            for index, stamp in enumerate(("1700000000", "1700000100")):
                subprocess.run(
                    ["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", str(index)],
                    check=True,
                    env={**env, "GIT_COMMITTER_DATE": f"{stamp} +0000"},
                )
                commits.append(
                    subprocess.run(
                        ["git", "-C", str(repo), "rev-parse", "HEAD"],
                        check=True, capture_output=True, text=True,
                    ).stdout.strip()
                )
            old, new = commits
            self.assertTrue(CLASSIFY.git_is_ancestor(repo, old, new))
            self.assertFalse(CLASSIFY.git_is_ancestor(repo, new, old))
            self.assertIsNone(CLASSIFY.git_is_ancestor(repo, X, new))
            self.assertEqual(CLASSIFY.git_commit_time(repo, new), 1700000100)

    def test_cli_exit_code_follows_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipt_path = Path(tmp) / "receipt.json"
            output = Path(tmp) / "state.json"
            receipt_path.write_text(json.dumps({"frontend": endpoint(None, 0, "down")}))
            code = CLASSIFY.main([
                "--receipt", str(receipt_path), "--expected-commit", C,
                "--main-commit", C, "--output", str(output),
            ])
            self.assertEqual(code, 1)
            payload = json.loads(output.read_text())
            self.assertEqual(payload["state"], "outage")
            self.assertEqual(payload["fingerprint"], "outage:none")


class FakeIssues:
    def __init__(self) -> None:
        self.issues: list[dict[str, Any]] = []
        self.notifications: list[str] = []

    def open_issues(self, label: str) -> list[dict[str, Any]]:
        return [i for i in self.issues if i["state"] == "open" and label in i["labels"]]

    def comments(self, number: int) -> list[dict[str, Any]]:
        return [{"body": body} for body in self.issues[number - 1]["comments"]]

    def create_issue(self, title: str, body: str, label: str) -> dict[str, Any]:
        issue = {"number": len(self.issues) + 1, "title": title, "body": body,
                 "labels": [label], "state": "open", "comments": []}
        self.issues.append(issue)
        self.notifications.append(f"open {issue['number']}")
        return issue

    def comment(self, number: int, body: str) -> None:
        self.issues[number - 1]["comments"].append(body)
        self.notifications.append(f"comment {number}")

    def close(self, number: int) -> None:
        self.issues[number - 1]["state"] = "closed"


def state(name: str, live: str | None = B) -> dict[str, Any]:
    result = CLASSIFY.Classification(1, name, name not in CLASSIFY.NON_ALERTING_STATES,
                                     C, C, live, f"{name} reason")
    payload = result.__dict__.copy()
    payload["fingerprint"] = result.fingerprint()
    return payload


class ProductionAlertIssueTest(unittest.TestCase):
    def test_alert_is_deduplicated_updated_and_resolved(self) -> None:
        issues = FakeIssues()
        run = "https://example.invalid/run"
        self.assertEqual(ALERT.reconcile(issues, state("stale"), run), "opened")
        self.assertEqual(ALERT.reconcile(issues, state("stale"), run), "unchanged")
        self.assertEqual(ALERT.reconcile(issues, state("outage", None), run), "updated")
        self.assertEqual(ALERT.reconcile(issues, state("outage", None), run), "unchanged")
        self.assertEqual(issues.notifications, ["open 1", "comment 1"])

        # Neither a pending nor a superseded run is a production proof.
        self.assertEqual(ALERT.reconcile(issues, state("pending"), run), "unchanged")
        self.assertEqual(ALERT.reconcile(issues, state("superseded", C), run), "unchanged")
        self.assertEqual(issues.issues[0]["state"], "open")

        self.assertEqual(ALERT.reconcile(issues, state("current", C), run), "resolved")
        self.assertEqual(issues.issues[0]["state"], "closed")
        self.assertIn("Erholt", issues.issues[0]["comments"][-1])

        # A new incident after recovery notifies again.
        self.assertEqual(ALERT.reconcile(issues, state("stale"), run), "opened")
        self.assertEqual(len(issues.issues), 2)

    def test_current_without_open_alert_is_silent(self) -> None:
        issues = FakeIssues()
        self.assertEqual(ALERT.reconcile(issues, state("current", C), "r"), "unchanged")
        self.assertEqual(issues.notifications, [])

    def test_missing_classification_alerts_as_monitor_failure(self) -> None:
        issues = FakeIssues()
        self.assertEqual(ALERT.reconcile(issues, ALERT.load_classification(None), "r"), "opened")
        self.assertIn("monitor_failure", issues.issues[0]["title"])
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "state.json"
            broken.write_text("{not json")
            self.assertEqual(ALERT.load_classification(broken)["state"], "monitor_failure")

    def test_drill_notifies_and_cleans_up_without_touching_real_alerts(self) -> None:
        issues = FakeIssues()
        self.assertEqual(ALERT.drill(issues, "r"), "drill")
        self.assertEqual(issues.notifications[0], "open 1")
        self.assertEqual(issues.issues[0]["labels"], [ALERT.DRILL_LABEL])
        self.assertEqual(issues.issues[0]["state"], "closed")
        self.assertEqual(issues.open_issues(ALERT.ALERT_LABEL), [])


if __name__ == "__main__":
    unittest.main()
