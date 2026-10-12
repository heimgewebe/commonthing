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
import signal
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

URLS = {
    "frontend": "https://commonthing.net/_app/version.json",
    "api": "https://commonthing.net/api/version",
    "main": "https://api.github.com/repos/heimgewebe/commonthing/commits/main",
    "schedule": "https://api.github.com/repos/heimgewebe/commonthing/actions/workflows/production-live-contract.yml/runs?event=schedule&per_page=2",
}
SCHEDULE_COMPLETED_URL = (
    "https://api.github.com/repos/heimgewebe/commonthing/actions/workflows/"
    "production-live-contract.yml/runs?event=schedule&status=completed&per_page=1"
)
SHA = re.compile(r"^[0-9a-f]{40}$")
UTC = timezone.utc
LIMIT = 512 * 1024
THRESHOLD = 45 * 60
WATCH_MAX_SECONDS = 240


class WatchRuntimeExceeded(BaseException):
    """The entire cron invocation exceeded its fixed time budget."""


class ConfirmedProductHTTPError(ValueError):
    """Both observations confirmed a critical product HTTP endpoint failure."""


class ConfirmedProductVersionError(ConfirmedProductHTTPError):
    """Both observations confirmed a malformed production commit field."""


def permanent_product_http_failure(name, status):
    return (
        name in {"frontend", "api"}
        and type(status) is int
        and status != 200
        and status not in {408, 425, 429}
    )


# Each P1 is cleared only after the observations needed to verify it succeed.
# Another still-visible P1 cannot hide an unverified incident.
P1_UNVERIFIABLE_ON = {
    "frontend_cache": {"unavailable_frontend", "invalid_commit_frontend"},
    "invalid_commit_frontend": {"unavailable_frontend"},
    "invalid_commit_api": {"unavailable_api"},
    "frontend_api_diverge": {
        "unavailable_frontend", "unavailable_api",
        "invalid_commit_frontend", "invalid_commit_api",
    },
    "stale_frontend": {
        "unavailable_frontend", "unavailable_main",
        "invalid_commit_frontend", "invalid_commit_main",
    },
    "stale_api": {
        "unavailable_api", "unavailable_main",
        "invalid_commit_api", "invalid_commit_main",
    },
}


def timestamp(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except (ValueError, AttributeError, TypeError, OverflowError):
        return None


def fetch(name):
    error = None
    product_failures = 0
    version_failures = 0
    for _ in range(2):
        try:
            url = SCHEDULE_COMPLETED_URL if name == "schedule_completed" else URLS[name]
            req = Request(url, headers={
                "User-Agent": "commonthing-independent-watch/1",
                "Accept": "application/vnd.github+json",
            })
            with urlopen(req, timeout=8) as response:
                if response.status != 200:
                    if permanent_product_http_failure(name, response.status):
                        product_failures += 1
                    raise ValueError(f"HTTP {response.status}")
                content = response.read(LIMIT + 1)
                if len(content) > LIMIT:
                    if name in {"frontend", "api"}:
                        product_failures += 1
                    raise ValueError("response exceeds limit")
                try:
                    data = json.loads(content)
                except (ValueError, UnicodeError) as exc:
                    if name in {"frontend", "api"}:
                        product_failures += 1
                    raise ValueError("invalid JSON response") from exc
                if not isinstance(data, dict):
                    if name in {"frontend", "api"}:
                        product_failures += 1
                    raise ValueError("JSON root not an object")
                if name in {"frontend", "api"}:
                    commit = data.get("commit")
                    if not isinstance(commit, str) or SHA.fullmatch(commit) is None:
                        product_failures += 1
                        version_failures += 1
                        raise ValueError("invalid public product version commit")
                return data, response.headers.get("Cache-Control", "")
        except HTTPError as exc:
            if permanent_product_http_failure(name, exc.code):
                product_failures += 1
            error = f"HTTPError: HTTP {exc.code}"
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:150]}"
    if name in {"frontend", "api"} and product_failures == 2:
        if version_failures == 2:
            raise ConfirmedProductVersionError(
                f"two confirmed invalid public product version commits: {error}"
            )
        raise ConfirmedProductHTTPError(f"two confirmed product HTTP failures: {error}")
    raise ValueError(f"two reads failed: {error}")


def evaluate(now, reader=fetch, *, previous_main_observation=None):
    data, headers, issues = {}, {}, []
    def flag(code, component, actual, expected, source, severity="P1", duration=None):
        item = {"code": code, "component": component, "actual": actual,
                "expected": expected, "source": (SCHEDULE_COMPLETED_URL if source == "schedule_completed" else URLS[source]),
                "severity": severity}
        if duration is not None:
            item["duration_seconds"] = round(duration)
        issues.append(item)

    for name in URLS:
        try:
            data[name], headers[name] = reader(name)
        except ConfirmedProductVersionError as exc:
            flag("invalid_commit_" + name, "product version response", str(exc)[:200],
                 "canonical commit in two production responses", name, "P1")
        except ConfirmedProductHTTPError as exc:
            flag("unavailable_" + name, "product HTTP response", str(exc)[:200],
                 "HTTP 200 from production", name, "P1")
        except Exception as exc:
            flag("unavailable_" + name, "observation channel", str(exc)[:200],
                 "HTTP 200 and valid JSON", name,
                 "INFO" if name == "schedule" else "P2")

    commits = {}
    for name in ("frontend", "api", "main"):
        if name in data:
            field = "sha" if name == "main" else "commit"
            value = data[name].get(field)
            if not isinstance(value, str) or not SHA.fullmatch(value):
                flag("invalid_commit_" + name, name, str(value)[:100],
                     "40-character hexadecimal commit", name,
                     "P2" if name == "main" else "P1")
            else:
                commits[name] = value

    if "frontend" in data:
        cache_directives = [p.strip().lower() for p in headers["frontend"].split(",")]
        if "no-store" not in cache_directives:
            flag("frontend_cache", "frontend", headers["frontend"][:120],
                 "Cache-Control: no-store", "frontend")
    if "frontend" in commits and "api" in commits and commits["frontend"] != commits["api"]:
        flag("frontend_api_diverge", "production commits",
             {"frontend": commits["frontend"], "api": commits["api"]},
             "frontend == API", "frontend")

    # Git commit timestamps are not GitHub branch-ref update timestamps.
    # Persist the first observed main ref instead of falsely ageing a fresh
    # fast-forward/push by an existing commit's old committer date.
    main_observation = None
    if "main" in commits:
        previous = previous_main_observation
        first_seen = now
        if isinstance(previous, dict) and previous.get("sha") == commits["main"]:
            recorded = timestamp(previous.get("first_seen"))
            if recorded is not None and 0 <= (now - recorded).total_seconds():
                first_seen = recorded
        main_observation = {
            "sha": commits["main"],
            "first_seen": first_seen.isoformat().replace("+00:00", "Z"),
        }
        # Carry the first observed mismatch across repeated main advances;
        # a later main SHA cannot erase an unresolved production divergence.
        prior_divergence = previous.get("diverged_since") if isinstance(previous, dict) else None
        prior_divergence = prior_divergence if isinstance(prior_divergence, dict) else {}
        diverged_since = {}
        for name in ("frontend", "api"):
            recorded = timestamp(prior_divergence.get(name))
            if name not in commits:
                # An unreadable endpoint has not converged; preserve the
                # previously observed mismatch across a new main SHA.
                if recorded is not None and recorded <= now:
                    diverged_since[name] = recorded.isoformat().replace("+00:00", "Z")
                continue
            if commits[name] == commits["main"]:
                continue
            anchor = recorded if recorded is not None and recorded <= now else first_seen
            diverged_since[name] = anchor.isoformat().replace("+00:00", "Z")
            age = (now - anchor).total_seconds()
            if age >= THRESHOLD:
                flag("stale_" + name, name, commits[name],
                     commits["main"], name, duration=age)
        if diverged_since:
            main_observation["diverged_since"] = diverged_since
        metadata = data["main"].get("commit")
        committer = metadata.get("committer") if isinstance(metadata, dict) else None
        commit_time = timestamp(committer.get("date") if isinstance(committer, dict) else None)
        commit_age = (now - commit_time).total_seconds() if commit_time else None
        if commit_age is None or commit_age < -120:
            flag("main_time_unreliable", "GitHub main", str(commit_age),
                 "valid commit timestamp", "main", "P2")
    newest_schedule = None
    if "schedule" in data:
        rows = data["schedule"].get("workflow_runs")
        if not isinstance(rows, list):
            flag("invalid_schedule_json", "GitHub schedule", str(type(rows)),
                 "workflow_runs array", "schedule", "INFO")
        else:
            scheduled = [r for r in rows if isinstance(r, dict)
                         and r.get("event") == "schedule" and timestamp(r.get("created_at"))]
            if not scheduled:
                flag("no_schedule", "GitHub schedule", "no event=schedule",
                     "scheduled run within 45 minutes", "schedule", "INFO")
            else:
                run = max(scheduled, key=lambda r: timestamp(r["created_at"]))
                newest_schedule = {"created_at": run["created_at"], "id": run.get("id"),
                                   "status": run.get("status"), "conclusion": run.get("conclusion")}
                age = (now - timestamp(run["created_at"])).total_seconds()
                if age >= THRESHOLD:
                    flag("schedule_stale", "GitHub schedule", run["created_at"],
                         "event=schedule within 45 minutes", "schedule",
                         severity="INFO", duration=age)
                elif age < -120:
                    flag("schedule_time_future", "GitHub schedule", str(round(age)),
                         "valid timestamp", "schedule", "INFO")
                # A newer queued/in-progress run is not proof that the most
                # recent completed scheduled check has recovered.
                completed = [item for item in scheduled if item.get("status") == "completed"]
                completed_source = "schedule"
                if not completed:
                    # Queued/in-progress runs can obscure older completed runs
                    # beyond the short latest-runs page; read completed runs directly.
                    try:
                        prior, _ = reader("schedule_completed")
                        history = prior.get("workflow_runs") if isinstance(prior, dict) else None
                        if not isinstance(history, list):
                            flag("invalid_completed_schedule_json", "GitHub completed schedule",
                                 str(type(history)), "workflow_runs array",
                                 "schedule_completed", "INFO")
                        else:
                            completed_source = "schedule_completed"
                            completed = [item for item in history
                                         if isinstance(item, dict)
                                         and item.get("event") == "schedule"
                                         and item.get("status") == "completed"
                                         and timestamp(item.get("created_at"))]
                            if not completed:
                                flag("no_completed_schedule", "GitHub completed schedule",
                                     "no completed schedule in history",
                                     "completed event=schedule run", "schedule_completed", "INFO")
                    except Exception as exc:
                        flag("unavailable_completed_schedule", "GitHub completed schedule",
                             str(exc)[:200], "valid completed schedule history",
                             "schedule_completed", "INFO")
                last_done = max(
                    completed, key=lambda item: timestamp(item["created_at"]),
                    default=None,
                )
                if last_done is not None and last_done.get("conclusion") != "success":
                    flag("schedule_failed", "GitHub scheduled check",
                         str(last_done.get("conclusion")),
                         "successful scheduled run", completed_source, "INFO")

    return {
        "checked_at_utc": now.isoformat().replace("+00:00", "Z"),
        "status": "ALARM" if any(i["severity"] == "P1" for i in issues)
                  else "MONITOR_DATA_FAILURE" if any(i["severity"] == "P2" for i in issues)
                  else "HEALTHY",
        "commits": commits, "main_observation": main_observation,
        "latest_schedule": newest_schedule, "issues": issues,
    }


def write_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as out:
        json.dump(value, out, sort_keys=True, separators=(",", ":"))
        out.write("\n")
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)


def record(result, directory, *, serialized_poll=False):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        # Polls may evaluate concurrently, but only the newest observation
        # may replace persistent state. Check under the same record lock so
        # an older delayed poll cannot erase a newer alarm or main anchor.
        try:
            previous_heartbeat = json.loads(
                (directory / "heartbeat.json").read_text(encoding="utf-8")
            )
        except (FileNotFoundError, ValueError):
            previous_heartbeat = None
        if isinstance(previous_heartbeat, dict):
            last_checked = timestamp(previous_heartbeat.get("checked_at_utc"))
            checked = timestamp(result.get("checked_at_utc"))
            if not serialized_poll and last_checked is not None and (checked is None or checked < last_checked):
                return None
        path = directory / "state.json"
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            previous = {}
        if not isinstance(previous, dict):
            previous = {}
        # The incident state is replaced before heartbeat.json. A failed
        # heartbeat write must not let an older overlapping poll erase P1.
        state_checked = timestamp(previous.get("checked_at_utc"))
        checked = timestamp(result.get("checked_at_utc"))
        if not serialized_poll and state_checked is not None and (checked is None or checked < state_checked):
            return None
        raw_severe = previous.get("severe")
        raw_severe = raw_severe if isinstance(raw_severe, list) else []
        # Malformed state is not evidence for either an incident or a recovery.
        old_severe = sorted(
            item for item in raw_severe
            if isinstance(item, list) and len(item) == 2
            and all(isinstance(value, str) for value in item)
            and item[0] not in {"schedule_stale", "schedule_failed", "no_schedule"}
        )
        old_uncertain = previous.get("uncertain")
        old_uncertain = old_uncertain if isinstance(old_uncertain, list) else []
        severe = sorted([x["code"], str(x["actual"])[:160]]
                        for x in result["issues"] if x["severity"] == "P1")
        observed_p1_codes = {item[0] for item in severe}
        uncertain = sorted(x["code"] for x in result["issues"] if x["severity"] == "P2")
        observed_codes = {item["code"] for item in result["issues"]}
        # A confirmed product HTTP-5xx incident cannot recover merely because
        # the next poll times out. Both outcomes use unavailable_<component>,
        # but only the latter is P2 observation uncertainty.
        uncertain_product_http = {"unavailable_frontend", "unavailable_api"} & set(uncertain)
        # Never infer a recovery during a new-main grace while the affected
        # production commit still mismatches the currently observed main.
        commits = result.get("commits")
        commits = commits if isinstance(commits, dict) else {}
        for item in old_severe:
            code = item[0]
            # Same-code P1 with refreshed details supersedes stale prior detail.
            if code in observed_p1_codes:
                continue
            name = code.removeprefix("stale_")
            still_stale = (
                code in {"stale_frontend", "stale_api"}
                and name in commits and "main" in commits
                and commits[name] != commits["main"]
            )
            if item not in severe and (
                P1_UNVERIFIABLE_ON.get(code, set()) & observed_codes
                or still_stale or code in uncertain_product_http
                or "watcher_execution_failed" in observed_codes
            ):
                severe.append(item)
        severe.sort()
        # Retained P1s are real active incidents, even when this single poll
        # fell inside a fresh-main grace period or a reference channel failed.
        detected = sorted([x["code"], str(x["actual"])[:160]]
                          for x in result["issues"] if x["severity"] == "P1")
        retained = [item for item in severe if item not in detected]
        if retained:
            result["observation_status"] = result["status"]
            result["retained_prior_p1"] = retained
        if severe:
            result["status"] = "ALARM"

        # A surviving incident with changed details, or partial recovery,
        # is not a new escalation. Only an actually new P1 code is an ALARM.
        old_codes = {item[0] for item in old_severe}
        new_codes = {item[0] for item in severe}
        transition = ("ALARM" if new_codes - old_codes
                      else "RECOVERY" if old_severe and not severe else None)
        uncertainty_event = ("MONITOR_DATA_FAILURE"
                             if uncertain and uncertain != old_uncertain else None)
        new_event = transition or uncertainty_event
        events = [event for event in (transition, uncertainty_event) if event]
        if events:
            try:
                with (directory / "events.jsonl").open("a", encoding="utf-8") as out:
                    for event_name in events:
                        event = {"event": event_name, **result,
                                 "issue_url": "https://github.com/heimgewebe/commonthing/issues/1939"}
                        out.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
                    out.flush()
                    os.fsync(out.fileno())
            except OSError as exc:
                # Auxiliary event history must not block durable incident state.
                # It is NOT evidence of notification delivery.
                result["issues"].append({
                    "code": "event_log_write_failed",
                    "component": "watcher event history",
                    "actual": f"{type(exc).__name__}: {str(exc)[:150]}",
                    "expected": "append and fsync local event history",
                    "source": str(directory / "events.jsonl"),
                    "severity": "P2",
                })
                uncertain = sorted(set(uncertain) | {"event_log_write_failed"})
                if not severe:
                    result["observation_status"] = result["status"]
                    result["status"] = "MONITOR_DATA_FAILURE"
        current_main = result.get("main_observation")
        previous_main = previous.get("main_observation") if isinstance(previous, dict) else None
        if not isinstance(current_main, dict):
            current_main = previous_main
        write_json(path, {"severe": severe, "uncertain": uncertain,
                          "main_observation": current_main,
                          "checked_at_utc": result["checked_at_utc"]})
        write_json(directory / "heartbeat.json", result)
    return new_event


def read_previous_main_observation(directory):
    try:
        state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    recorded = state.get("main_observation")
    if not isinstance(recorded, dict):
        return None
    sha = recorded.get("sha")
    seen = timestamp(recorded.get("first_seen"))
    if not isinstance(sha, str) or not SHA.fullmatch(sha) or seen is None:
        return None
    observation = {"sha": sha, "first_seen": seen.isoformat().replace("+00:00", "Z")}
    raw_divergence = recorded.get("diverged_since")
    if isinstance(raw_divergence, dict):
        divergence = {}
        for name in ("frontend", "api"):
            value = timestamp(raw_divergence.get(name))
            if value is not None:
                divergence[name] = value.isoformat().replace("+00:00", "Z")
        if divergence:
            observation["diverged_since"] = divergence
    return observation


def _watch_deadline(_signal, _frame):
    # Not an Exception: fetch() deliberately retries ordinary transport faults.
    # The entire invocation must still abort after its overall runtime budget.
    raise WatchRuntimeExceeded("watcher total runtime deadline exceeded")


def watcher_failure_report(now, exc):
    return {
        "checked_at_utc": now.isoformat().replace("+00:00", "Z"),
        "status": "MONITOR_DATA_FAILURE",
        "commits": {},
        "main_observation": None,
        "latest_schedule": None,
        "issues": [{
            "code": "watcher_execution_failed",
            "component": "watcher",
            "actual": f"{type(exc).__name__}: {str(exc)[:150]}",
            "expected": "bounded successful watcher observation",
            "source": "local watcher execution",
            "severity": "P2",
        }],
    }


def _evaluate_cycle(directory):
    now = datetime.now(UTC)
    try:
        previous = read_previous_main_observation(directory)
        return evaluate(now, previous_main_observation=previous)
    except WatchRuntimeExceeded as exc:
        return watcher_failure_report(now, exc)
    except Exception as exc:
        return watcher_failure_report(now, exc)


def _persist_cycle(result, directory):
    try:
        event = record(result, directory, serialized_poll=True)
    except (WatchRuntimeExceeded, Exception) as exc:
        # Best effort only. No local write can prove its own availability when
        # storage is completely inaccessible; an external liveness watcher
        # remains necessary.
        failure = watcher_failure_report(datetime.now(UTC), exc)
        signal.alarm(10)
        try:
            record(failure, directory, serialized_poll=True)
        except (WatchRuntimeExceeded, Exception) as recovery_exc:
            print(
                f"watcher heartbeat failed: {type(recovery_exc).__name__}",
                file=sys.stderr,
            )
        return 2
    if event:
        print(event, result["checked_at_utc"],
              ",".join(x["code"] for x in result["issues"]))
    return 0 if result["status"] == "HEALTHY" else 2


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--state-dir", type=Path,
                        default=Path.home() / ".local/commonthing-watch-state")
    args = parser.parse_args()
    previous_handler = signal.signal(signal.SIGALRM, _watch_deadline)
    signal.alarm(WATCH_MAX_SECONDS)
    try:
        if args.dry_run:
            result = _evaluate_cycle(args.state_dir)
            print(json.dumps(result, sort_keys=True))
            return 0 if result["status"] == "HEALTHY" else 2
        args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Exclude a second cron invocation from the entire read/evaluate/write
        # cycle. A new pass after an NTP wall-clock step must not be blocked by
        # the persisted timestamp of an older completed pass.
        with (args.state_dir / "poll.lock").open("a+") as poll_lock:
            try:
                fcntl.flock(poll_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 2
            result = _evaluate_cycle(args.state_dir)
            return _persist_cycle(result, args.state_dir)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)


if __name__ == "__main__":
    sys.exit(main())