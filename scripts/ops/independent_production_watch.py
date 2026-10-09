#!/usr/bin/env python3
"""Off-GitHub read-only Commonthing production checker.

Only reads public URLs. Writes evidence to the local per-user state directory.
No GitHub mutation, email, webhook or claimed notification delivery.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import sys
from urllib.request import Request, urlopen

URLS = {
    "frontend": "https://commonthing.net/_app/version.json",
    "api": "https://commonthing.net/api/version",
    "main": "https://api.github.com/repos/heimgewebe/commonthing/commits/main",
    "schedule": "https://api.github.com/repos/heimgewebe/commonthing/actions/workflows/production-live-contract.yml/runs?event=schedule&per_page=2",
}
SHA = re.compile(r"^[0-9a-f]{40}$")
UTC = timezone.utc
LIMIT = 512 * 1024
THRESHOLD = 45 * 60


def timestamp(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except (ValueError, AttributeError, TypeError):
        return None


def fetch(name):
    error = None
    for _ in range(2):
        try:
            req = Request(URLS[name], headers={
                "User-Agent": "commonthing-independent-watch/1",
                "Accept": "application/vnd.github+json",
            })
            with urlopen(req, timeout=8) as response:
                if response.status != 200:
                    raise ValueError(f"HTTP {response.status}")
                content = response.read(LIMIT + 1)
                if len(content) > LIMIT:
                    raise ValueError("response exceeds limit")
                data = json.loads(content)
                if not isinstance(data, dict):
                    raise ValueError("JSON root not an object")
                return data, response.headers.get("Cache-Control", "")
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:150]}"
    raise ValueError(f"two reads failed: {error}")


def evaluate(now, reader=fetch):
    data, headers, issues = {}, {}, []
    def flag(code, component, actual, expected, source, severity="P1", duration=None):
        item = {"code": code, "component": component, "actual": actual,
                "expected": expected, "source": URLS[source], "severity": severity}
        if duration is not None:
            item["duration_seconds"] = round(duration)
        issues.append(item)

    for name in URLS:
        try:
            data[name], headers[name] = reader(name)
        except Exception as exc:
            flag("unavailable_" + name, "observation channel", str(exc)[:200],
                 "HTTP 200 and valid JSON", name, "P2")

    commits = {}
    for name in ("frontend", "api", "main"):
        if name in data:
            field = "sha" if name == "main" else "commit"
            value = data[name].get(field)
            if not isinstance(value, str) or not SHA.fullmatch(value):
                flag("invalid_commit_" + name, name, str(value)[:100],
                     "40-character hexadecimal commit", name)
            else:
                commits[name] = value

    if "frontend" in data:
        cache_directives = [p.strip().lower() for p in headers["frontend"].split(",")]
        if "no-store" not in cache_directives:
            flag("frontend_cache", "frontend", headers["frontend"][:120],
                 "Cache-Control: no-store", "frontend")
    if "frontend" in commits and "api" in commits and commits["frontend"] != commits["api"]:
        flag("frontend_api_diverge", "production commits", commits,
             "frontend == API", "frontend")

    if "main" in commits:
        commit_time = timestamp(data["main"].get("commit", {}).get("committer", {}).get("date"))
        age = (now - commit_time).total_seconds() if commit_time else None
        if age is None or age < -120:
            flag("main_time_unreliable", "GitHub main", str(age),
                 "valid commit timestamp", "main", "P2")
        if age is not None and age >= THRESHOLD:
            for name in ("frontend", "api"):
                if name in commits and commits[name] != commits["main"]:
                    flag("stale_" + name, name, commits[name],
                         commits["main"], name, duration=age)

    newest_schedule = None
    if "schedule" in data:
        rows = data["schedule"].get("workflow_runs")
        if not isinstance(rows, list):
            flag("invalid_schedule_json", "GitHub schedule", str(type(rows)),
                 "workflow_runs array", "schedule")
        else:
            scheduled = [r for r in rows if isinstance(r, dict)
                         and r.get("event") == "schedule" and timestamp(r.get("created_at"))]
            if not scheduled:
                flag("no_schedule", "GitHub schedule", "no event=schedule",
                     "scheduled run within 45 minutes", "schedule")
            else:
                run = max(scheduled, key=lambda r: timestamp(r["created_at"]))
                newest_schedule = {"created_at": run["created_at"], "id": run.get("id"),
                                   "status": run.get("status"), "conclusion": run.get("conclusion")}
                age = (now - timestamp(run["created_at"])).total_seconds()
                if age > THRESHOLD:
                    flag("schedule_stale", "GitHub schedule", run["created_at"],
                         "event=schedule within 45 minutes", "schedule", duration=age)
                elif age < -120:
                    flag("schedule_time_future", "GitHub schedule", str(round(age)),
                         "valid timestamp", "schedule", "P2")
                if run.get("status") == "completed" and run.get("conclusion") != "success":
                    flag("schedule_failed", "GitHub scheduled check", str(run.get("conclusion")),
                         "successful scheduled run", "schedule")

    return {
        "checked_at_utc": now.isoformat().replace("+00:00", "Z"),
        "status": "ALARM" if any(i["severity"] == "P1" for i in issues)
                  else "MONITOR_DATA_FAILURE" if issues else "HEALTHY",
        "commits": commits, "latest_schedule": newest_schedule, "issues": issues,
    }


def write_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as out:
        json.dump(value, out, sort_keys=True, separators=(",", ":"))
        out.write("\n")
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)


def record(result, directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        path = directory / "state.json"
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            previous = {}
        severe = sorted([x["code"], str(x["actual"])[:160]]
                        for x in result["issues"] if x["severity"] == "P1")
        uncertain = sorted(x["code"] for x in result["issues"] if x["severity"] == "P2")
        new_event = None
        old_severe = previous.get("severe", [])
        if severe and severe != old_severe:
            new_event = "ALARM"
        elif old_severe and not severe and not uncertain:
            new_event = "RECOVERY"
        elif uncertain and uncertain != previous.get("uncertain", []):
            new_event = "MONITOR_DATA_FAILURE"
        if new_event:
            event = {"event": new_event, **result,
                     "issue_url": "https://github.com/heimgewebe/commonthing/issues/1939"}
            with (directory / "events.jsonl").open("a", encoding="utf-8") as out:
                out.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
                out.flush()
                os.fsync(out.fileno())
        # Never erase a prior confirmed incident due to a failed HTTP observation.
        if not severe and uncertain:
            severe = old_severe
        write_json(path, {"severe": severe, "uncertain": uncertain})
        write_json(directory / "heartbeat.json", result)
    return new_event


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--state-dir", type=Path,
                        default=Path.home() / ".local/state/commonthing-watch")
    args = parser.parse_args()
    result = evaluate(datetime.now(UTC))
    if args.dry_run:
        print(json.dumps(result, sort_keys=True))
        return 0 if not result["issues"] else 2
    event = record(result, args.state_dir)
    if event:
        print(event, result["checked_at_utc"],
              ",".join(x["code"] for x in result["issues"]))
    return 0 if not result["issues"] else 2


if __name__ == "__main__":
    sys.exit(main())