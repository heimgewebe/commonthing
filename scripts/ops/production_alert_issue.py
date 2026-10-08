#!/usr/bin/env python3
"""Deliver production live-contract alerts as one deduplicated GitHub issue.

A red workflow run is easy to ignore and repeats every few minutes. Instead,
an alerting classification opens one issue labelled ``production-alert``;
repository watchers get a notification for it. While the alert lasts, a new
comment is added only when the alert's fingerprint (state and live commit)
changes. Only a ``current`` classification resolves the issue: ``pending``
and ``superseded`` runs are no production proof and leave it open.

``--drill`` opens and immediately closes a separately labelled test issue, so
the delivery path can be proven without breaking production.
"""

from __future__ import annotations

import argparse
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


class IssueClient(Protocol):
    def open_issues(self, label: str) -> list[dict[str, Any]]: ...
    def comments(self, number: int) -> list[dict[str, Any]]: ...
    def create_issue(self, title: str, body: str, label: str) -> dict[str, Any]: ...
    def comment(self, number: int, body: str) -> None: ...
    def close(self, number: int) -> None: ...


class GitHubIssueClient:
    def __init__(self, repository: str, token: str, timeout: float = 15.0) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError(f"invalid repository {repository!r}")
        self._base = f"{API_ROOT}/repos/{repository}"
        self._token = token
        self._timeout = timeout

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
        return [issue for issue in issues if "pull_request" not in issue]

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
        issue = self._request("POST", "/issues", {"title": title, "body": body, "labels": [label]})
        names = {item.get("name") for item in issue.get("labels") or [] if isinstance(item, dict)}
        if label not in names:
            raise ValueError(f"issue #{issue.get('number')} was created without label {label!r}")
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
            f"- main: `{classification.get('main_commit')}`",
            f"- Lauf: {run_url}",
        ]
    )


def latest_fingerprint(issue: dict[str, Any], comments: list[dict[str, Any]]) -> str | None:
    fingerprint = None
    for text in [issue.get("body") or ""] + [c.get("body") or "" for c in comments]:
        for match in FINGERPRINT_RE.finditer(text):
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
        "fingerprint": "monitor_failure:none",
    }


def reconcile(client: IssueClient, classification: dict[str, Any], run_url: str) -> str:
    open_alerts = client.open_issues(ALERT_LABEL)
    issue = open_alerts[0] if open_alerts else None
    state = classification.get("state")

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
        if latest_fingerprint(issue, client.comments(issue["number"])) == fingerprint:
            return "unchanged"
        client.comment(
            issue["number"],
            "Der Alarm hat sich geändert.\n\n" + _details(classification, run_url) + "\n\n"
            + _marker(fingerprint),
        )
        return "updated"

    if state == "current" and issue is not None:
        client.comment(
            issue["number"],
            "Erholt: Der aktuelle main-Stand ist wieder live und konsistent.\n\n"
            + _details(classification, run_url),
        )
        client.close(issue["number"])
        return "resolved"
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
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--classification", type=Path)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--drill", action="store_true")
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not repository:
        print("ERROR: GITHUB_TOKEN and GITHUB_REPOSITORY are required", file=sys.stderr)
        return 2
    client = GitHubIssueClient(repository, token)
    try:
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
