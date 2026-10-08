from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.error
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
    # A well-formed readback for ``commit``: the headers and artifact
    # declaration a newer live commit must still satisfy to count as superseded.
    short = commit[:8] if commit else None
    return {
        "url": "https://example.invalid/",
        "status": status,
        "commit": commit,
        "version": short,
        "error": error,
        "headers": {
            "cache-control": "no-store",
            "x-weltgewebe-api-build": commit,
            "x-weltgewebe-build": short,
        },
        "artifact_tree": {
            "schema_version": 1,
            "sha256": "0" * 64,
            "file_count": 1,
            "compile_revision": commit,
            "provenance": "unattested",
            "error": None,
        },
    }


def receipt(frontend: str | None, api: str | None = None, *, passed: bool = False) -> dict:
    return {
        "schema_version": 3,
        "expected_commit": frontend,
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
        rollout_start=lambda _live, _target: 10_000 - age,
    )


class AlertWorkflowTest(unittest.TestCase):
    def test_alert_job_runs_after_failures_but_not_after_cancellation(self) -> None:
        workflow = (OPS.parents[1] / ".github/workflows/production-live-contract.yml").read_text(
            encoding="utf-8"
        )
        job = re.split(r"\n  \S", workflow.split("\n  alert:\n", 1)[1], maxsplit=1)[0]
        condition = next(line for line in job.splitlines() if line.strip().startswith("if:"))
        self.assertIn("!cancelled()", condition)
        self.assertNotIn("always()", condition)
        self.assertIn("github.event_name != 'pull_request'", condition)


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

    def test_current_requires_a_complete_consistent_verifier_receipt(self) -> None:
        valid = receipt(C, passed=True)
        self.assertEqual(classify(valid, C).state, "current")
        wrong_schema = {**valid, "schema_version": 2}
        wrong_expected = {**valid, "expected_commit": B}
        missing_expected = {key: value for key, value in valid.items() if key != "expected_commit"}
        contradictory = {**valid, "reasons": ["not verified"]}
        missing_header = {**valid, "frontend": {**valid["frontend"], "headers": {}}}
        missing_artifact = {**valid, "frontend": {**valid["frontend"], "artifact_tree": None}}
        for broken in (
            wrong_schema, wrong_expected, missing_expected, contradictory,
            missing_header, missing_artifact,
        ):
            with self.subTest(broken=broken):
                result = classify(broken, C)
                self.assertEqual(result.state, "invalid", result.reason)
                self.assertTrue(result.alert)

    def test_passing_receipt_for_an_older_target_is_not_current(self) -> None:
        # main moved from B to C after the run resolved B as its target.
        pending = classify(receipt(B, passed=True), B, main=C, age=300)
        self.assertEqual(pending.state, "pending", pending.reason)
        self.assertFalse(pending.alert)
        stale = classify(receipt(B, passed=True), B, main=C, age=1200)
        self.assertEqual(stale.state, "stale", stale.reason)
        self.assertTrue(stale.alert)
        off_main = classify(receipt(X, passed=True), X, main=C)
        self.assertEqual(off_main.state, "divergent", off_main.reason)

    def test_intermediate_live_commit_is_lag_not_superseded(self) -> None:
        # Target A, B is live, main already at C: C is not proven live.
        pending = classify(receipt(B), A, main=C, age=300)
        self.assertEqual(pending.state, "pending", pending.reason)
        stale = classify(receipt(B), A, main=C, age=1200)
        self.assertEqual(stale.state, "stale", stale.reason)
        self.assertTrue(stale.alert)

    def test_grace_period_does_not_excuse_other_contract_failures(self) -> None:
        broken = receipt(B)
        broken["frontend"]["headers"]["cache-control"] = "max-age=60"
        result = classify(broken, C, age=300)
        self.assertEqual(result.state, "invalid", result.reason)
        self.assertTrue(result.alert)

    def test_superseded_requires_a_valid_receipt_for_the_newer_commit(self) -> None:
        broken = receipt(C)
        broken["frontend"]["headers"]["cache-control"] = "max-age=60"
        result = classify(broken, B)
        self.assertEqual(result.state, "invalid")
        self.assertTrue(result.alert)
        self.assertIn("no-store", result.reason)

        undeclared = receipt(C)
        undeclared["frontend"]["artifact_tree"] = None
        self.assertEqual(classify(undeclared, B).state, "invalid")

        stale_header = receipt(C)
        stale_header["api"]["headers"]["x-weltgewebe-api-build"] = B
        self.assertEqual(classify(stale_header, B).state, "invalid")

    def test_split_deployment_fingerprint_tracks_both_commits(self) -> None:
        first = classify(receipt(A, api=B), C)
        second = classify(receipt(B, api=C), C)
        self.assertEqual(first.state, "divergent")
        self.assertTrue(first.fingerprint().startswith(f"divergent:{A}/{B}#"))
        self.assertNotEqual(first.fingerprint(), second.fingerprint())

    def test_outage_fingerprint_tracks_the_readable_side(self) -> None:
        def half_down(frontend: str | None, api: str | None) -> dict:
            data = receipt(frontend or A, api or A)
            for name, commit in (("frontend", frontend), ("api", api)):
                if commit is None:
                    data[name] = endpoint(None, 0, "down")
            return data

        first = classify(half_down(A, None), C)
        moved = classify(half_down(B, None), C)
        flipped = classify(half_down(None, B), C)
        self.assertEqual(first.state, "outage")
        self.assertTrue(first.fingerprint().startswith(f"outage:{A}/none#"))
        self.assertEqual(len({first.fingerprint(), moved.fingerprint(), flipped.fingerprint()}), 3)

    def test_later_merges_do_not_restart_the_grace_period(self) -> None:
        # A is live; B landed long ago, but a fresh merge C is main's head now.
        landed = {B: 10_000 - 5000, C: 10_000 - 60}

        def first_after(live: str, target: str) -> int | None:
            self.assertEqual((live, target), (A, C))
            return landed[B]

        result = CLASSIFY.classify(
            receipt(A),
            expected_commit=C,
            main_commit=C,
            now=10_000,
            pending_grace_seconds=1200,
            is_ancestor=ancestor,
            rollout_start=first_after,
        )
        self.assertEqual(result.state, "stale")
        self.assertTrue(result.alert)

    def test_future_commit_time_does_not_extend_the_grace_period(self) -> None:
        result = classify(receipt(B), C, age=-3600)
        self.assertEqual(result.state, "stale")
        self.assertIn("future", result.reason)

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
        bad_gateway = classify(data, C)
        self.assertEqual(bad_gateway.state, "outage")
        # Same commits, new diagnosis: the alert must be updated, not deduplicated.
        self.assertNotEqual(result.fingerprint(), bad_gateway.fingerprint())
        data["api"] = endpoint(C, status=500)
        self.assertNotEqual(bad_gateway.fingerprint(), classify(data, C).fingerprint())
        # Both sides down: a change behind an unchanged frontend failure counts.
        data["frontend"] = endpoint(None, status=0, error="timed out")
        both = classify(data, C)
        self.assertIn("frontend", both.reason)
        self.assertIn("api", both.reason)
        data["api"] = endpoint(None, status=0, error="timed out")
        self.assertNotEqual(both.fingerprint(), classify(data, C).fingerprint())

    def test_alert_details_show_both_endpoint_commits(self) -> None:
        half_down = receipt(A)
        half_down["api"] = endpoint(None, status=502)
        payload = CLASSIFY.asdict(classify(half_down, C))
        details = ALERT._details(payload, "https://run")
        self.assertIn(f"Frontend-Commit: `{A}`", details)
        self.assertIn("API-Commit: `None`", details)

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
            rollout_start=lambda _live, _target: None,
        )
        self.assertEqual(result.state, "stale")

    def test_git_helpers_read_real_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            commits = []
            for index, stamp in enumerate(("1700000000", "1700000050", "1700000100")):
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
            old, middle, new = commits
            self.assertTrue(CLASSIFY.git_is_ancestor(repo, old, new))
            self.assertFalse(CLASSIFY.git_is_ancestor(repo, new, old))
            self.assertIsNone(CLASSIFY.git_is_ancestor(repo, X, new))
            self.assertEqual(CLASSIFY.git_commit_time(repo, new), 1700000100)
            # The rollout is owed since the first commit after the live one.
            self.assertEqual(CLASSIFY.git_rollout_start(repo, old, new), 1700000050)
            self.assertEqual(CLASSIFY.git_rollout_start(repo, middle, new), 1700000100)
            self.assertIsNone(CLASSIFY.git_rollout_start(repo, new, new))
            self.assertIsNone(CLASSIFY.git_rollout_start(repo, X, new))

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
            self.assertTrue(payload["fingerprint"].startswith("outage:none#"))

    def test_schaubild_failure_turns_a_green_state_into_an_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipt_path = Path(tmp) / "receipt.json"
            schaubild = Path(tmp) / "schaubild.json"
            output = Path(tmp) / "state.json"
            receipt_path.write_text(json.dumps(receipt(C, passed=True)))
            args = ["--receipt", str(receipt_path), "--expected-commit", C, "--main-commit", C,
                    "--repo", tmp, "--schaubild-receipt", str(schaubild), "--output", str(output)]

            converged = {
                "schema_version": CLASSIFY.SCHAUBILD_SCHEMA,
                "state": "current",
                "action_required": False,
                "locked_source_commit": A,
                "locked_image_digest": "sha256:" + "e" * 64,
                "desired_source_commit": A,
                "desired_workflow_run_id": 4242,
                "desired_published_at": "2026-10-08T12:00:00Z",
            }
            trimmed = {k: v for k, v in converged.items() if not k.startswith(
                ("locked_image", "desired_workflow", "desired_published"))}
            schaubild.write_text(json.dumps(converged))
            self.assertEqual(CLASSIFY.main(args), 0)
            self.assertEqual(json.loads(output.read_text())["state"], "current")

            # "current" without the producer's proof fields is not convergence.
            for unproven in (
                {"state": "current", "action_required": False},
                {**converged, "schema_version": 2},
                {**converged, "desired_source_commit": B},
                {**converged, "locked_source_commit": None},
                trimmed,
                {**converged, "locked_image_digest": "latest"},
                {**converged, "desired_workflow_run_id": 0},
                {**converged, "desired_workflow_run_id": True},
                {**converged, "desired_workflow_run_id": "4242"},
                {**converged, "desired_published_at": "2026-10-08T12:00:00"},
                {**converged, "desired_published_at": "yesterday"},
                {**converged, "desired_published_at": None},
            ):
                schaubild.write_text(json.dumps(unproven))
                self.assertEqual(CLASSIFY.main(args), 1, unproven)
                self.assertEqual(json.loads(output.read_text())["state"], "invalid")

            schaubild.write_text(json.dumps({"state": "stale", "action_required": True}))
            self.assertEqual(CLASSIFY.main(args), 1)
            payload = json.loads(output.read_text())
            self.assertEqual(payload["state"], "invalid")
            self.assertIn("Schaubild", payload["reason"])

            schaubild.unlink()
            self.assertEqual(CLASSIFY.main(args), 1)
            self.assertIn("unreadable", json.loads(output.read_text())["reason"])

    def test_schaubild_schema_matches_the_producer(self) -> None:
        producer = (OPS.parent / "preflight/schauwerk_release_convergence.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(f'SCHEMA = "{CLASSIFY.SCHAUBILD_SCHEMA}"', producer)
        # The proof fields checked on the green path are the ones it emits.
        for field in ("locked_image_digest", "desired_workflow_run_id", "desired_published_at"):
            self.assertIn(f'"{field}"', producer)

    def test_schaubild_failure_keeps_a_more_specific_alert(self) -> None:
        specific = classify(receipt(None), C)
        self.assertTrue(specific.alert)
        combined = CLASSIFY.with_schaubild(specific, "Schaubild broken")
        self.assertEqual(combined.state, specific.state)
        self.assertIn(specific.reason, combined.reason)
        self.assertIn("Schaubild broken", combined.reason)
        self.assertIs(CLASSIFY.with_schaubild(specific, None), specific)

    def test_changed_failure_cause_on_the_same_commit_changes_the_fingerprint(self) -> None:
        cache = receipt(C)
        cache["reasons"] = ["frontend version readback is not served with Cache-Control: no-store"]
        tree = receipt(C)
        tree["reasons"] = ["frontend artifact_tree declaration is missing"]
        both = receipt(C)
        both["reasons"] = list(reversed(cache["reasons"] + tree["reasons"]))
        both_again = receipt(C)
        both_again["reasons"] = cache["reasons"] + tree["reasons"]
        prints = [classify(r, C).fingerprint() for r in (cache, tree, both)]
        self.assertTrue(all(p.startswith(f"invalid:{C}#") for p in prints), prints)
        self.assertEqual(len(set(prints)), 3)
        # Order of the same causes does not matter.
        self.assertEqual(prints[2], classify(both_again, C).fingerprint())
        # A newer-commit revalidation failure carries its causes too.
        broken = receipt(C)
        broken["frontend"]["headers"]["cache-control"] = "max-age=60"
        self.assertTrue(classify(broken, B).causes)

    def test_schaubild_failure_names_the_release_pair_and_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "schaubild.json"

            def failure(payload: dict) -> str | None:
                path.write_text(json.dumps(payload))
                return CLASSIFY.schaubild_failure(path)

            pending = {"state": "promotion_pending", "action_required": True,
                       "locked_source_commit": A}
            first = failure({**pending, "desired_source_commit": B})
            newer = failure({**pending, "desired_source_commit": C})
            self.assertIn(B, first)
            self.assertNotEqual(first, newer)
            invalid = {"state": "invalid", "action_required": True}
            self.assertNotEqual(
                failure({**invalid, "error": "lock missing"}),
                failure({**invalid, "error": "no accepted release"}),
            )
            green = classify(receipt(C, passed=True), C)
            self.assertNotEqual(
                CLASSIFY.with_schaubild(green, first).fingerprint(),
                CLASSIFY.with_schaubild(green, newer).fingerprint(),
            )

    def test_changed_schaubild_failure_changes_the_fingerprint(self) -> None:
        green = classify(receipt(C, passed=True), C)
        pending = CLASSIFY.with_schaubild(green, "Schaubild state 'promotion_pending'")
        unreadable = CLASSIFY.with_schaubild(green, "Schaubild receipt is unreadable")
        self.assertNotEqual(pending.fingerprint(), unreadable.fingerprint())
        specific = classify(receipt(None), C)
        self.assertNotEqual(
            CLASSIFY.with_schaubild(specific, "a").fingerprint(),
            CLASSIFY.with_schaubild(specific, "b").fingerprint(),
        )

    def test_recovery_of_one_cause_changes_the_fingerprint(self) -> None:
        # Receipt and Schaubild fail on the same commit, then only Schaubild.
        both = CLASSIFY.with_schaubild(classify(receipt(C, passed=False), C), "Schaubild broken")
        only = CLASSIFY.with_schaubild(classify(receipt(C, passed=True), C), "Schaubild broken")
        self.assertEqual((both.state, only.state), ("invalid", "invalid"))
        self.assertNotEqual(both.fingerprint(), only.fingerprint())
        self.assertNotEqual(only.fingerprint(), classify(receipt(C, passed=False), C).fingerprint())


class FakeIssues:
    def __init__(self) -> None:
        self.issues: list[dict[str, Any]] = []
        self.notifications: list[str] = []

    def open_issues(self, label: str) -> list[dict[str, Any]]:
        return [i for i in self.issues if i["state"] == "open" and label in i["labels"]]

    def comments(self, number: int) -> list[dict[str, Any]]:
        return [
            body if isinstance(body, dict) else {
                "body": body,
                "user": {"login": ALERT.ALERT_BOT_LOGIN, "id": ALERT.ALERT_BOT_ID},
            }
            for body in self.issues[number - 1]["comments"]
        ]

    def create_issue(self, title: str, body: str, label: str) -> dict[str, Any]:
        issue = {"number": len(self.issues) + 1, "title": title, "body": body,
                 "labels": [label], "state": "open", "comments": [],
                 "user": {"login": ALERT.ALERT_BOT_LOGIN, "id": ALERT.ALERT_BOT_ID}}
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
    # Same shape as the JSON the classifier writes (tuples become lists).
    return json.loads(json.dumps(payload))


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

    def test_spoofed_issue_comment_does_not_suppress_alarm_update(self) -> None:
        issues = FakeIssues()
        run = "https://example.invalid/run"
        self.assertEqual(ALERT.reconcile(issues, state("stale"), run), "opened")
        changed = state("outage", None)
        issues.issues[0]["comments"].append({
            "body": ALERT._marker(changed["fingerprint"]),
            "user": {"login": "untrusted-user", "id": 12345},
        })
        self.assertEqual(ALERT.reconcile(issues, changed, run), "updated")
        self.assertEqual(ALERT.reconcile(issues, changed, run), "unchanged")
        self.assertEqual(issues.notifications, ["open 1", "comment 1"])

    def test_untrusted_issue_body_is_not_a_delivery_receipt(self) -> None:
        forged = {"body": ALERT._marker("stale:spoof"),
                  "user": {"login": "untrusted-user", "id": 12345}}
        self.assertIsNone(ALERT.latest_fingerprint(forged, []))

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

    def test_incomplete_or_contradictory_classification_alerts_as_monitor_failure(self) -> None:
        payloads = [
            {"state": "stale"},
            {"state": "stale", "alert": "true", "fingerprint": "stale:x"},
            {"state": "stale", "alert": False, "fingerprint": "stale:x"},
            {"state": "current", "alert": True, "fingerprint": "current:x"},
            {"state": "stale", "alert": True},
            {"state": "stale", "alert": True, "fingerprint": "a b"},
            {"state": "unknown", "alert": True, "fingerprint": "unknown:x"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            for payload in payloads:
                path.write_text(json.dumps(payload))
                loaded = ALERT.load_classification(path)
                self.assertEqual(loaded["state"], "monitor_failure", payload)
                self.assertIs(loaded["alert"], True)
                issues = FakeIssues()
                self.assertEqual(ALERT.reconcile(issues, loaded, "r"), "opened", payload)

    def test_malformed_non_alerting_classification_cannot_resolve(self) -> None:
        bare_current = {"state": "current", "alert": False}
        wrong_head = state("current", C) | {"main_commit": B}
        no_reason = state("superseded", C)
        del no_reason["reason"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            for payload in (bare_current, wrong_head, no_reason):
                path.write_text(json.dumps(payload))
                loaded = ALERT.load_classification(path)
                self.assertEqual(loaded["state"], "monitor_failure", payload)
                issues = FakeIssues()
                issues.create_issue("Produktionsalarm: stale", "", ALERT.ALERT_LABEL)
                self.assertNotEqual(ALERT.reconcile(issues, loaded, "r"), "resolved", payload)
                self.assertEqual(issues.issues[0]["state"], "open")

    def test_monitor_failure_fingerprint_follows_its_reason(self) -> None:
        missing = ALERT.load_classification(None)
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "state.json"
            broken.write_text("{not json")
            unreadable = ALERT.load_classification(broken)
        self.assertNotEqual(missing["fingerprint"], unreadable["fingerprint"])
        self.assertEqual(missing["fingerprint"], ALERT.load_classification(None)["fingerprint"])

    def test_real_classifications_load_unchanged(self) -> None:
        self.assertEqual(ALERT.NON_ALERTING_STATES, CLASSIFY.NON_ALERTING_STATES)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            for name in sorted(ALERT.ALERTING_STATES | ALERT.NON_ALERTING_STATES):
                payload = state(name, C)
                path.write_text(json.dumps(payload))
                self.assertEqual(ALERT.load_classification(path), payload, name)

    def test_drill_issue_is_closed_even_if_the_comment_fails(self) -> None:
        issues = FlakyCommentIssues()
        with self.assertRaises(OSError):
            ALERT.drill(issues, "r")
        self.assertEqual(issues.issues[0]["state"], "closed")

    def test_drill_notifies_and_cleans_up_without_touching_real_alerts(self) -> None:
        issues = FakeIssues()
        self.assertEqual(ALERT.drill(issues, "r"), "drill")
        self.assertEqual(issues.notifications[0], "open 1")
        self.assertEqual(issues.issues[0]["labels"], [ALERT.DRILL_LABEL])
        self.assertEqual(issues.issues[0]["state"], "closed")
        self.assertEqual(issues.open_issues(ALERT.ALERT_LABEL), [])


class FlakyCommentIssues(FakeIssues):
    def comment(self, number: int, body: str) -> None:
        raise OSError("transient")


class RecordingClient(ALERT.GitHubIssueClient):
    def __init__(self, label_status: int | None, returned_labels: list[str]) -> None:
        super().__init__("owner/repo", "token")
        self.calls: list[tuple[str, str]] = []
        self._label_status = label_status
        self._returned_labels = returned_labels

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, path))
        if path == "/labels" and self._label_status is not None:
            raise urllib.error.HTTPError(path, self._label_status, "error", None, None)
        if path == "/issues":
            return {"number": 7, "labels": [{"name": n} for n in self._returned_labels]}
        return None


class GitHubIssueClientTest(unittest.TestCase):
    def test_label_is_provisioned_before_the_issue(self) -> None:
        client = RecordingClient(None, [ALERT.ALERT_LABEL])
        client.create_issue("t", "b", ALERT.ALERT_LABEL)
        self.assertEqual(client.calls, [("POST", "/labels"), ("POST", "/issues")])

    def test_existing_label_is_accepted(self) -> None:
        client = RecordingClient(422, [ALERT.ALERT_LABEL])
        self.assertEqual(client.create_issue("t", "b", ALERT.ALERT_LABEL)["number"], 7)

    def test_other_label_errors_are_not_swallowed(self) -> None:
        with self.assertRaises(urllib.error.HTTPError):
            RecordingClient(403, [ALERT.ALERT_LABEL]).create_issue("t", "b", ALERT.ALERT_LABEL)

    def test_comments_follow_every_page(self) -> None:
        class Paged(ALERT.GitHubIssueClient):
            def __init__(self) -> None:
                super().__init__("owner/repo", "token")

            def _request(self, method: str, path: str, payload: Any = None) -> Any:
                page = int(path.rsplit("page=", 1)[1])
                return [{"body": f"c{page}-{i}"} for i in range(100 if page < 3 else 5)]

        comments = Paged().comments(1)
        self.assertEqual(len(comments), 205)
        self.assertEqual(comments[-1]["body"], "c3-4")

    def test_unlabelled_issue_fails_delivery(self) -> None:
        with self.assertRaises(ValueError):
            RecordingClient(None, []).create_issue("t", "b", ALERT.ALERT_LABEL)


if __name__ == "__main__":
    unittest.main()
