#!/usr/bin/env python3
"""Deliver production live-contract alerts as one deduplicated GitHub issue.

A red workflow run is easy to ignore and repeats every few minutes. Instead,
an alerting classification opens one issue labelled ``production-alert``;
the operator is explicitly assigned; actual notification delivery is not
proven until acknowledged. While the alert lasts, a new comment is added only
when its fingerprint (state and live commit) changes. Only a ``current`` classification resolves the issue: ``pending``
and ``superseded`` runs are no production proof and leave it open.

``--drill`` opens and immediately closes a separately labelled test issue, so
the delivery path can be proven without breaking production.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Protocol

ALERT_LABEL = "production-alert"
DRILL_LABEL = "production-alert-drill"
FINGERPRINT_RE = re.compile(r"<!-- production-alert-fingerprint: (\S+) -->")
API_ROOT = "https://api.github.com"
# The repository GITHUB_TOKEN writes as this verified GitHub service identity.
ALERT_BOT_LOGIN = "github-actions[bot]"
ALERT_BOT_ID = 41898282
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
# Mirrors classify_production_live_state.py; the alert job checks out only scripts/ops.
ALERTING_STATES = frozenset({"stale", "divergent", "invalid", "outage", "monitor_failure"})
NON_ALERTING_STATES = frozenset({"current", "superseded", "pending"})


class IssueClient(Protocol):
    def open_issues(self, label: str) -> list[dict[str, Any]]: ...
    def ensure_assignee(self, issue: dict[str, Any]) -> None: ...
    def comments(self, number: int) -> list[dict[str, Any]]: ...
    def create_issue(self, title: str, body: str, label: str) -> dict[str, Any]: ...
    def comment(self, number: int, body: str) -> None: ...
    def close(self, number: int) -> None: ...


class GitHubIssueClient:
    def __init__(
        self, repository: str, token: str, timeout: float = 15.0,
        assignee: str | None = None,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError(f"invalid repository {repository!r}")
        self._base = f"{API_ROOT}/repos/{repository}"
        if assignee is not None and not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", assignee):
            raise ValueError("invalid alert assignee")
        self._token = token
        self._timeout = timeout
        self._assignee = assignee

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base}{path}",
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            body = response.read()
        return json.loads(body) if body else None

    def open_issues(self, label: str) -> list[dict[str, Any]]:
        issues = self._request(
            "GET", f"/issues?state=open&labels={label}&sort=created&direction=asc&per_page=100"
        )
        result = [issue for issue in issues if "pull_request" not in issue]
        if label == ALERT_LABEL:
            # A public issue labelled by a third party cannot impersonate our alert.
            result = [issue for issue in result if _trusted_alert_author(issue)]
        return result

    def _has_assignee(self, issue: dict[str, Any]) -> bool:
        # GitHub logins are case-insensitive; reject missing or non-string logins.
        return self._assignee is not None and any(
            isinstance(account, dict)
            and isinstance(account.get("login"), str)
            and account["login"].casefold() == self._assignee.casefold()
            for account in issue.get("assignees") or []
        )

    def ensure_assignee(self, issue: dict[str, Any]) -> None:
        """Repair only the handled issue; fail if GitHub does not confirm ownership."""
        if not self._assignee:
            raise ValueError("production alert assignee is not configured")
        if self._has_assignee(issue):
            return
        number = issue.get("number")
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise ValueError("alert issue has invalid number")
        updated = self._request(
            "POST", f"/issues/{number}/assignees", {"assignees": [self._assignee]}
        )
        self._require_assignee(updated)

    def _require_assignee(self, issue: dict[str, Any]) -> None:
        if self._assignee and not self._has_assignee(issue):
            raise ValueError(f"issue #{issue.get('number')} has no verified alert assignee")

    def comments(self, number: int) -> list[dict[str, Any]]:
        # Follow every page: the newest fingerprint may sit past comment 100.
        result: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self._request("GET", f"/issues/{number}/comments?per_page=100&page={page}")
            result.extend(batch or [])
            if not batch or len(batch) < 100:
                return result
            page += 1

    def ensure_label(self, label: str) -> None:
        # Issue creation does not reliably provision a missing label, and an
        # unlabelled alert would never be found again by open_issues().
        try:
            self._request("POST", "/labels", {"name": label, "color": "b60205"})
        except urllib.error.HTTPError as exc:
            if exc.code != 422:  # 422: the label already exists
                raise

    def create_issue(self, title: str, body: str, label: str) -> dict[str, Any]:
        self.ensure_label(label)
        # Publish the incident before assigning it. GitHub rejects a create
        # with an unassignable login (HTTP 422), which must never hide an alarm.
        payload: dict[str, Any] = {"title": title, "body": body, "labels": [label]}
        issue = self._request("POST", "/issues", payload)
        try:
            names = {item.get("name") for item in issue.get("labels") or [] if isinstance(item, dict)}
            if label not in names:
                raise ValueError(f"issue #{issue.get('number')} was created without label {label!r}")
            if self._assignee:
                # Verify ownership separately; on failure the real issue stays open.
                self.ensure_assignee(issue)
        except (OSError, ValueError):
            # Unlike a real alert, a failed controlled drill must not be stranded.
            number = issue.get("number")
            if (
                label == DRILL_LABEL
                and isinstance(number, int)
                and not isinstance(number, bool)
                and number > 0
            ):
                try:
                    self.close(number)
                except (OSError, ValueError) as cleanup_exc:
                    raise ValueError(
                        f"drill issue #{number} is unverified and could not be closed"
                    ) from cleanup_exc
            raise
        return issue

    def comment(self, number: int, body: str) -> None:
        self._request("POST", f"/issues/{number}/comments", {"body": body})

    def close(self, number: int) -> None:
        self._request("PATCH", f"/issues/{number}", {"state": "closed", "state_reason": "completed"})


def _marker(fingerprint: str) -> str:
    return f"<!-- production-alert-fingerprint: {fingerprint} -->"


def _details(classification: dict[str, Any], run_url: str) -> str:
    return "\n".join(
        [
            f"- Zustand: `{classification.get('state')}`",
            f"- Grund: {classification.get('reason')}",
            f"- Erwarteter Commit: `{classification.get('expected_commit')}`",
            f"- Live-Commit: `{classification.get('live_commit')}`",
            # Split and partly unreadable deployments have no single live commit.
            f"- Frontend-Commit: `{classification.get('frontend_commit')}`",
            f"- API-Commit: `{classification.get('api_commit')}`",
            f"- main: `{classification.get('main_commit')}`",
            f"- Lauf: {run_url}",
        ]
    )


def _trusted_alert_author(entry: dict[str, Any]) -> bool:
    # Comments in public issues are untrusted, including hidden HTML markers.
    user = entry.get("user")
    return (
        isinstance(user, dict)
        and user.get("id") == ALERT_BOT_ID
        and user.get("login") == ALERT_BOT_LOGIN
    )


def latest_fingerprint(issue: dict[str, Any], comments: list[dict[str, Any]]) -> str | None:
    fingerprint = None
    for entry in [issue, *comments]:
        if not isinstance(entry, dict) or not _trusted_alert_author(entry):
            continue
        for match in FINGERPRINT_RE.finditer(entry.get("body") or ""):
            fingerprint = match.group(1)
    return fingerprint


def monitor_failure(reason: str) -> dict[str, Any]:
    return {
        "state": "monitor_failure",
        "alert": True,
        "reason": reason,
        "expected_commit": None,
        "main_commit": None,
        "live_commit": None,
        # The reason distinguishes a missing, unreadable or malformed classification.
        "fingerprint": "monitor_failure:none#"
        + hashlib.sha256(reason.encode()).hexdigest()[:12],
    }


def reconcile(client: IssueClient, classification: dict[str, Any], run_url: str) -> str:
    # Inspect first and publish any new alarm detail before checking ownership.
    # GitHub rejecting an assignee must still fail the run, never suppress an
    # escalation comment or block the primary issue because of duplicates.
    state = classification.get("state")
    open_alerts = client.open_issues(ALERT_LABEL)
    issue = open_alerts[0] if open_alerts else None

    if classification.get("alert") is True:
        fingerprint = str(classification.get("fingerprint") or f"{state}:none")
        if issue is None:
            client.create_issue(
                f"Produktionsalarm: {state}",
                "Der Production-Live-Contract meldet einen Zustand, der Handeln braucht.\n\n"
                + _details(classification, run_url)
                + "\n\nDieses Issue wird geschlossen, sobald ein Lauf den aktuellen "
                "main-Stand wieder als live und konsistent nachweist.\n\n"
                + _marker(fingerprint),
                ALERT_LABEL,
            )
            return "opened"
        changed = latest_fingerprint(issue, client.comments(issue["number"])) != fingerprint
        if changed:
            client.comment(
                issue["number"],
                "Der Alarm hat sich geändert.\n\n"
                + _details(classification, run_url) + "\n\n" + _marker(fingerprint),
            )
        client.ensure_assignee(issue)
        return "updated" if changed else "unchanged"

    if state == "current" and issue is not None:
        client.comment(
            issue["number"],
            "Erholt: Der aktuelle main-Stand ist wieder live und konsistent.\n\n"
            + _details(classification, run_url),
        )
        client.close(issue["number"])
        return "resolved"
    # pending/superseded are not recovery: the still-open incident needs an owner.
    if issue is not None:
        client.ensure_assignee(issue)
    return "unchanged"


def drill(client: IssueClient, run_url: str) -> str:
    issue = client.create_issue(
        "Produktionsalarm-Probe",
        "Kontrollierter Testalarm des Production-Live-Contracts. Wenn diese "
        f"Meldung angekommen ist, funktioniert die Zustellung.\n\n- Lauf: {run_url}",
        DRILL_LABEL,
    )
    try:
        client.comment(issue["number"], "Probe beendet; das Issue wird geschlossen.")
    finally:
        # A stranded drill issue would read as a real open alarm.
        client.close(issue["number"])
    return "drill"


def load_classification(path: Path | None) -> dict[str, Any]:
    if path is None:
        return monitor_failure("no classification was produced")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return monitor_failure(f"classification is unreadable: {exc}")
    if not isinstance(payload, dict) or not isinstance(payload.get("state"), str):
        return monitor_failure("classification has no state")
    state, alert = payload["state"], payload.get("alert")
    if state not in ALERTING_STATES | NON_ALERTING_STATES:
        return monitor_failure(f"classification has unknown state {state!r}")
    if not isinstance(alert, bool) or alert != (state in ALERTING_STATES):
        return monitor_failure(f"classification alert flag {alert!r} contradicts state {state!r}")
    problem = _schema_problem(payload)
    if problem:
        return monitor_failure(f"classification is malformed: {problem}")
    return payload


def _schema_problem(payload: dict[str, Any]) -> str | None:
    # Checked for every state: a malformed "current" must never close an alarm.
    if payload.get("schema_version") != 1:
        return f"schema_version {payload.get('schema_version')!r}"
    for name in ("expected_commit", "main_commit"):
        if not _is_commit(payload.get(name)):
            return f"{name} is not a full SHA"
    live = payload.get("live_commit")
    if live is not None and not _is_commit(live):
        return "live_commit is neither null nor a full SHA"
    if not isinstance(payload.get("reason"), str):
        return "reason is not a string"
    fingerprint = payload.get("fingerprint")
    if not (isinstance(fingerprint, str) and re.fullmatch(r"\S+", fingerprint)):
        return "no usable fingerprint"
    if payload["state"] == "current" and not (
        live == payload["expected_commit"] == payload["main_commit"]
    ):
        return "current without the expected main head live"
    return None


def _is_commit(value: Any) -> bool:
    return isinstance(value, str) and COMMIT_RE.fullmatch(value) is not None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--classification", type=Path)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--drill", action="store_true")
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    assignee = os.environ.get("PRODUCTION_ALERT_ASSIGNEE", "")
    if not token or not repository or not assignee:
        print(
            "ERROR: GITHUB_TOKEN, GITHUB_REPOSITORY and PRODUCTION_ALERT_ASSIGNEE are required",
            file=sys.stderr,
        )
        return 2
    try:
        client = GitHubIssueClient(repository, token, assignee=assignee)
        if args.drill:
            action = drill(client, args.run_url)
        else:
            path = args.classification if args.classification and args.classification.exists() else None
            action = reconcile(client, load_classification(path), args.run_url)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"ERROR: alert delivery failed: {exc}", file=sys.stderr)
        return 1
    print(f"production_alert_action={action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
