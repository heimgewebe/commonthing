#!/usr/bin/env python3
"""Off-GitHub read-only Commonthing production checker.

Only reads public URLs. Writes evidence to the local per-user state directory.
No GitHub mutation, email, webhook or claimed notification delivery.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
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
# Keep the ordinary state projection bounded to 17 entries. A prolonged
# event-journal outage must not silently drop incident 19, 20 or beyond:
# spill further transition intentions into the *same* durable state snapshots.
# This is a byte-budgeted FIFO, not a second monitoring system. Normal cron
# stops observing BEFORE this budget is exhausted, until journal replay frees
# space; monitoring becomes explicitly unverifiable, never falsely healthy.
OUTBOX_NORMAL_LIMIT = 16
OUTBOX_SURGE_LIMIT = OUTBOX_NORMAL_LIMIT + 1
OUTBOX_MAX_BYTES = 8 * 1024 * 1024
OUTBOX_ADMISSION_RESERVE = 2 * 65536


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


def _public_main_ref_over_git():
    """Fallback to GitHub's public Git ref when anonymous REST is throttled.

    The ref (not its commit timestamp) is authoritative. Never synthesize
    GitHub REST commit metadata or accept a different branch/tag.
    """
    completed = subprocess.run(
        ["git", "ls-remote", "https://github.com/heimgewebe/commonthing.git",
         "refs/heads/main"],
        capture_output=True, text=True, timeout=8, check=False,
    )
    if completed.returncode != 0 or not isinstance(completed.stdout, str):
        raise ValueError("public git ref unavailable")
    match = re.fullmatch(r"([0-9a-f]{40})\trefs/heads/main\n?", completed.stdout)
    if match is None:
        raise ValueError("invalid public git main ref")
    return {"sha": match.group(1), "observation_transport": "git_https"}


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
            if name == "main" and exc.code == 403:
                # Shared anonymous GitHub REST budgets can be exhausted by
                # unrelated processes. The public Git ref is an independent
                # precise read of the same branch without API quota.
                try:
                    return _public_main_ref_over_git(), ""
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    # Do not invent a main SHA if both transports fail.
                    pass
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


def evaluate(now, reader=fetch, *, previous_main_observation=None, issue_sink=None):
    data, headers = {}, {}
    issues = issue_sink if issue_sink is not None else []
    def flag(code, component, actual, expected, source, severity="P1", duration=None):
        item = {"code": code, "component": component, "actual": actual,
                "expected": expected, "source": (SCHEDULE_COMPLETED_URL if source == "schedule_completed" else URLS[source]),
                "severity": severity}
        if duration is not None:
            item["duration_seconds"] = round(duration)
        issues.append(item)

    def observe(name):
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
        else:
            # Preserve confirmed frontend cache faults if a later URL hangs.
            if name == "frontend":
                cache_directives = [p.strip().lower() for p in headers[name].split(",")]
                if "no-store" not in cache_directives:
                    flag("frontend_cache", "frontend", headers[name][:120],
                         "Cache-Control: no-store", "frontend")

    # Frontend/API independently prove product failures: classify them
    # BEFORE the optional GitHub main read can consume the global SIGALRM.
    # A deadline on main must not erase an already-confirmed production split
    # or invalid product SHA. Main-dependent staleness is evaluated below.
    for name in ("frontend", "api"):
        observe(name)

    commits = {}
    for name in ("frontend", "api"):
        if name in data:
            value = data[name].get("commit")
            if not isinstance(value, str) or not SHA.fullmatch(value):
                flag("invalid_commit_" + name, name, str(value)[:100],
                     "40-character hexadecimal commit", name)
            else:
                commits[name] = value
    if "frontend" in commits and "api" in commits and commits["frontend"] != commits["api"]:
        flag("frontend_api_diverge", "production commits",
             {"frontend": commits["frontend"], "api": commits["api"]},
             "frontend == API", "frontend")

    observe("main")
    if "main" in data:
        value = data["main"].get("sha")
        if not isinstance(value, str) or not SHA.fullmatch(value):
            flag("invalid_commit_main", "main", str(value)[:100],
                 "40-character hexadecimal commit", "main", "P2")
        else:
            commits["main"] = value

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
        if (data["main"].get("observation_transport") != "git_https"
                and (commit_age is None or commit_age < -120)):
            flag("main_time_unreliable", "GitHub main", str(commit_age),
                 "valid commit timestamp", "main", "P2")
    newest_schedule = None
    # Schedule telemetry is optional. Its deadline must not discard the
    # completed frontend/API/main observation or reset the staleness anchor.
    try:
        observe("schedule")
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

    except WatchRuntimeExceeded as exc:
        flag("schedule_deadline_exceeded", "GitHub schedule",
             str(exc)[:160], "bounded optional schedule observation",
             "schedule", severity="P2")

    return {
        "checked_at_utc": now.isoformat().replace("+00:00", "Z"),
        "status": "ALARM" if any(i["severity"] == "P1" for i in issues)
                  else "MONITOR_DATA_FAILURE" if any(i["severity"] == "P2" for i in issues)
                  else "HEALTHY",
        "commits": commits, "main_observation": main_observation,
        "latest_schedule": newest_schedule, "issues": issues,
    }


def _fsync_directory(directory):
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def write_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as out:
        json.dump(value, out, sort_keys=True, separators=(",", ":"))
        out.write("\n")
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)
    # A file fsync alone cannot commit the renamed directory entry.
    # Treat parent-sync errors as uncertain, not successful, writes.
    _fsync_directory(path.parent)


def _record_unverifiable_prior_state(result, directory, heartbeat, cause, *, state_read_uncertain=False):
    # Never replace incident history if its old P1 entries cannot be parsed.
    # The local heartbeat is a separate best-effort liveness channel, not a
    # substitute for the corrupted original state.json.
    failure = watcher_failure_report(
        timestamp(result.get("checked_at_utc")) or datetime.now(UTC),
        ValueError(cause),
    )
    failure["issues"][0].update({
        "code": "unverifiable_state",
        "component": "watcher incident state",
        "actual": cause[:150],
        "expected": "readable well-formed prior state.json",
        "source": str(directory / "state.json"),
    })
    failure["observation_status"] = result.get("status")
    confirmed = []
    seen = set()
    for observed in (
        result.get("issues", []),
        heartbeat.get("issues", []) if isinstance(heartbeat, dict) else [],
    ):
        if not isinstance(observed, list):
            continue
        for item in observed:
            if not isinstance(item, dict) or item.get("severity") != "P1":
                continue
            code = item.get("code")
            if not isinstance(code, str) or not code or code in seen:
                continue
            seen.add(code)
            confirmed.append(item)
    prior_retained = (
        heartbeat.get("retained_prior_p1", [])
        if isinstance(heartbeat, dict) else []
    )
    if isinstance(prior_retained, list):
        for item in prior_retained:
            if (not isinstance(item, list) or len(item) != 2
                    or not all(isinstance(part, str) for part in item)):
                continue
            code, actual = item
            if code in seen:
                continue
            seen.add(code)
            confirmed.append({
                "code": code, "component": "prior watcher P1",
                "actual": actual[:160], "expected": "confirmed recovery",
                "source": "local persisted heartbeat", "severity": "P1",
            })
    if confirmed:
        failure["issues"].extend(confirmed)
        failure["status"] = "ALARM"
    # A crash may leave different confirmed intents in the heartbeat and
    # in the just-attempted state update. Choosing only the longer snapshot
    # silently loses a same-sized, non-overlapping P1. Retain the older
    # heartbeat prefix first, then the newer observation, deduplicating only
    # exact entries. Never discard a confirmed intent to satisfy the budget.
    durable_intent = []
    for anchor in (heartbeat, result):
        try:
            entries = _pending_transition_entries(anchor)
        except (ValueError, TypeError):
            continue
        for entry in entries:
            if entry not in durable_intent:
                durable_intent.append(entry)
    # A confirmed product P1 during a transient state read failure is real
    # evidence, but cannot yet be classified against the unknown prior epoch.
    # Preserve an explicitly provisional, typed ALARM intent under the same
    # fsynced heartbeat FIFO. A later readable state/journal must reconcile it
    # before any delivery or inferred RECOVERY.
    if state_read_uncertain and result.get("status") == "ALARM":
        already_pending = set()
        for entry in durable_intent:
            if entry["event"] != "ALARM":
                continue
            for item in entry["payload"].get("issues", []):
                if isinstance(item, dict) and item.get("severity") == "P1":
                    code = item.get("code")
                    if isinstance(code, str):
                        already_pending.add(code)
        newly_confirmed = []
        for item in result.get("issues", []):
            if not isinstance(item, dict) or item.get("severity") != "P1":
                continue
            code = item.get("code")
            if isinstance(code, str) and code and code not in already_pending:
                already_pending.add(code)
                newly_confirmed.append(item)
        if newly_confirmed:
            original = {
                "event": "ALARM",
                **{key: deepcopy(value) for key, value in result.items()
                   if key not in {"pending_transitions", "pending_surge_overflow"}},
                "status": "ALARM",
                "issues": newly_confirmed,
                "state_read_unverified": True,
                "issue_url": "https://github.com/heimgewebe/commonthing/issues/1939",
            }
            durable_intent.append({"event": "ALARM", "payload": original})
    if durable_intent:
        outbox = {
            "pending_transitions": durable_intent[:OUTBOX_SURGE_LIMIT],
        }
        if len(durable_intent) > OUTBOX_SURGE_LIMIT:
            outbox["pending_surge_overflow"] = durable_intent[OUTBOX_SURGE_LIMIT:]
        # A full combined outbox must block the overwrite, not silently
        # truncate it. The preexisting heartbeat remains authoritative.
        _pending_transition_entries(outbox)
        failure.update(outbox)
    result.clear()
    result.update(failure)
    write_json(directory / "heartbeat.json", result)
    return "MONITOR_DATA_FAILURE"


def _pending_transition_entries(snapshot):
    """Read a bounded, typed transition outbox from durable incident anchors."""
    if not isinstance(snapshot, dict):
        return []
    entries = snapshot.get("pending_transitions", [])
    if not isinstance(entries, list) or len(entries) > OUTBOX_SURGE_LIMIT:
        raise ValueError("malformed pending transition outbox")
    # The continuation is stored in the same crash-synced state/heartbeat
    # files, not in another queue service. A fixed additional slot would
    # merely move loss from the eighteenth incident to the nineteenth.
    overflow = snapshot.get("pending_surge_overflow", [])
    if not isinstance(overflow, list):
        raise ValueError("malformed pending surge overflow")
    entries = [*entries, *overflow]
    validated = []
    size = 2
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != {"event", "payload"}:
            raise ValueError("malformed pending transition entry")
        payload = entry["payload"]
        if (
            entry["event"] not in {"ALARM", "RECOVERY"}
            or not isinstance(payload, dict)
            or payload.get("event") != entry["event"]
            or payload.get("status") not in {"ALARM", "HEALTHY", "MONITOR_DATA_FAILURE"}
            or timestamp(payload.get("checked_at_utc")) is None
            or not isinstance(payload.get("issues"), list)
            or len(json.dumps(payload, sort_keys=True)) > 65536
        ):
            raise ValueError("invalid pending transition payload")
        if index >= len(snapshot.get("pending_transitions", [])):
            # Overflow must be a genuine typed transition, not a fabricated
            # ALARM from a healthy heartbeat or RECOVERY carrying active P1.
            p1 = [
                issue for issue in payload["issues"]
                if isinstance(issue, dict) and issue.get("severity") == "P1"
                and isinstance(issue.get("code"), str) and issue.get("code")
            ]
            if entry["event"] == "ALARM" and (
                payload.get("status") != "ALARM" or not p1
            ):
                raise ValueError("invalid overflow ALARM evidence")
            if entry["event"] == "RECOVERY" and (
                payload.get("status") == "ALARM" or p1
            ):
                raise ValueError("invalid overflow RECOVERY evidence")
        size += len(json.dumps(entry, sort_keys=True, separators=(",", ":")).encode("utf-8")) + 1
        if size > OUTBOX_MAX_BYTES:
            raise ValueError("pending outbox exceeds byte budget")
        if entry not in validated:
            validated.append(entry)
    return validated


def _transition_already_journaled(directory, entry):
    """Confirm an outbox entry by exact durable bytes, never by a status flag.

    A crash after journal fsync but before clearing the outbox must not
    replay the already committed ALARM or RECOVERY. Never assume absence
    from a truncated journal read (which would create duplicates).
    """
    expected = (
        json.dumps(entry["payload"], sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    try:
        with (directory / "events.jsonl").open("rb") as reader:
            # A fixed overall journal limit eventually prevents all replay.
            # Scan the append-only history with bounded per-line memory, even
            # if a malformed line is arbitrarily long. Production SIGALRM
            # bounds wall time; an incomplete scan never asserts absence.
            at_line_start = True
            while chunk := reader.readline(len(expected) + 1):
                if at_line_start and chunk == expected:
                    # Visible bytes after a previous fsync error are not proof
                    # of durability: sync the journal and directory first.
                    os.fsync(reader.fileno())
                    _fsync_directory(directory)
                    return True
                at_line_start = chunk.endswith(b"\n")
            return False
    except FileNotFoundError:
        return False


def _last_journaled_transition(directory):
    """Read the last complete durable ALARM/RECOVERY, never journal existence.

    A preexisting events.jsonl may contain an already recovered incident or
    only observation diagnostics. Neither proves the newest heartbeat P1 was
    journaled. Bound each line, reject an incomplete/corrupt journal, and sync
    the observed bytes before using them as crash-recovery evidence.
    """
    last = None
    try:
        with (directory / "events.jsonl").open("rb") as reader:
            while line := reader.readline(65537):
                if len(line) > 65536 or not line.endswith(b"\n"):
                    raise ValueError("unverifiable journal line")
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("invalid journal record")
                if event.get("event") in {"ALARM", "RECOVERY"}:
                    last = event
            os.fsync(reader.fileno())
        _fsync_directory(directory)
    except FileNotFoundError:
        return None
    return last


def _verified_lost_state_recovery(directory, heartbeat, result):
    """Rebuild only a single confirmed, journal-anchored pending RECOVERY.

    Deleting state.json cannot erase a fsynced recovery intent. Conversely,
    an ambiguous journal, newer ALARM epoch or unverified current product
    observation must never manufacture recovery.
    """
    if (
        not isinstance(heartbeat, dict)
        or result.get("status") not in {"HEALTHY", "MONITOR_DATA_FAILURE"}
    ):
        return None
    entries = _pending_transition_entries(heartbeat)
    if len(entries) != 1 or entries[0]["event"] != "RECOVERY":
        return None
    recovery = entries[0]["payload"]
    if recovery.get("status") not in {"HEALTHY", "MONITOR_DATA_FAILURE"}:
        return None
    journal = _last_journaled_transition(directory)
    if not isinstance(journal, dict) or journal.get("event") != "ALARM":
        return None
    if journal.get("status") != "ALARM":
        return None
    journal_at = timestamp(journal.get("checked_at_utc"))
    recovery_at = timestamp(recovery.get("checked_at_utc"))
    heartbeat_at = timestamp(heartbeat.get("checked_at_utc"))
    observed_at = timestamp(result.get("checked_at_utc"))
    if (
        journal_at is None or recovery_at is None
        or heartbeat_at is None or observed_at is None
        or not journal_at < recovery_at <= heartbeat_at <= observed_at
    ):
        return None
    old_issues = journal.get("issues")
    if not isinstance(old_issues, list):
        return None
    severe = []
    seen = set()
    for item in old_issues:
        if not isinstance(item, dict) or item.get("severity") != "P1":
            continue
        code = item.get("code")
        if not isinstance(code, str) or not code or code in seen:
            return None
        seen.add(code)
        severe.append([code, str(item.get("actual", ""))[:160]])
    if not severe:
        return None
    # P2 observations about an unrelated dependency do not erase an
    # independently confirmed RECOVERY. But either the historical recovery
    # or the current poll must retain uncertainty when it masks any
    # earlier P1's required observation. Unknown P2 codes stay fail-closed.
    admissible_p2 = {
        "unavailable_frontend", "unavailable_api", "unavailable_main",
        "invalid_commit_main", "main_time_unreliable",
        "schedule_deadline_exceeded", "event_log_write_failed",
    }
    old_codes = {entry[0] for entry in severe}
    for observed in (recovery, result):
        observed_issues = observed.get("issues")
        if not isinstance(observed_issues, list):
            return None
        p2_codes = set()
        for issue in observed_issues:
            if not isinstance(issue, dict):
                return None
            severity = issue.get("severity")
            if severity == "P1":
                return None
            if severity == "P2":
                code = issue.get("code")
                if not isinstance(code, str) or code not in admissible_p2:
                    return None
                p2_codes.add(code)
            elif severity != "INFO":
                return None
        if any(P1_UNVERIFIABLE_ON.get(code, set()) & p2_codes
               for code in old_codes):
            return None
    return {
        "severe": sorted(severe),
        "uncertain": [],
        "main_observation": heartbeat.get("main_observation"),
        "checked_at_utc": heartbeat.get("checked_at_utc"),
    }


def _last_complete_journal_offset(directory, expected_end):
    """Return end of the last newline-terminated event using bounded memory.

    A hard crash during an append can leave a partial suffix even though its
    in-process rollback handler never ran. The watcher emits only newline-
    terminated events, so an unterminated suffix is not committed evidence.
    """
    with (directory / "events.jsonl").open("rb") as reader:
        if os.fstat(reader.fileno()).st_size != expected_end:
            raise OSError("journal changed while inspecting incomplete tail")
        cursor = expected_end
        while cursor:
            start = max(0, cursor - 65536)
            reader.seek(start)
            data = reader.read(cursor - start)
            if len(data) != cursor - start:
                raise OSError("short journal tail read")
            newline = data.rfind(b"\n")
            if newline >= 0:
                return start + newline + 1
            cursor = start
    return 0


def _append_durable_events(directory, journal_events, committed_event_names=None):
    """Append one atomic JSONL batch; preserve exact old end on failure."""
    # Prepare one atomic append batch: old pending transitions
    # always precede subsequent monitor-data evidence.
    batch = [
        json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
        for event in journal_events
    ]
    # Unbuffered single append with rollback to the exact old
    # size on a short write or fsync failure. The surrounding
    # record flock serializes all watcher-owned journal writers.
    payload = "".join(batch).encode("utf-8")
    with (directory / "events.jsonl").open("ab", buffering=0) as out:
        fd = out.fileno()
        previous_end = os.lseek(fd, 0, os.SEEK_END)
        if previous_end:
            complete_end = _last_complete_journal_offset(
                directory, previous_end,
            )
            if complete_end < previous_end:
                # Only an uncommitted, unterminated suffix is discarded;
                # earlier complete JSONL rows are never rewritten.
                os.ftruncate(fd, complete_end)
                os.fsync(fd)
                _fsync_directory(directory)
                previous_end = complete_end
        try:
            written = out.write(payload)
            if written != len(payload):
                raise OSError("short event history append")
            os.fsync(fd)
            # A newly created journal requires a durable directory entry.
            _fsync_directory(directory)
            if committed_event_names is not None:
                committed_event_names.update(
                    entry["event"] for entry in journal_events
                )
        except (OSError, WatchRuntimeExceeded) as append_error:
            # SIGALRM is one-shot; rollback itself can block on the
            # same faulty filesystem. Bound this last-resort I/O.
            if signal.getsignal(signal.SIGALRM) is _watch_deadline:
                signal.alarm(10)
            try:
                os.ftruncate(fd, previous_end)
                os.fsync(fd)
            except OSError as rollback_error:
                raise OSError(
                    f"event append failed: {append_error}; "
                    f"rollback failed: {rollback_error}"
                ) from rollback_error
            raise


def record(result, directory, *, serialized_poll=False, suppress_transition_event=False,
           committed_event_names=None):
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
        except (OSError, ValueError):
            # A heartbeat read fault is observer uncertainty, not absence of
            # authoritative incident state. Continue with state.json checks.
            previous_heartbeat = None
        if isinstance(previous_heartbeat, dict):
            last_checked = timestamp(previous_heartbeat.get("checked_at_utc"))
            checked = timestamp(result.get("checked_at_utc"))
            if not serialized_poll and last_checked is not None and (checked is None or checked < last_checked):
                return None
        path = directory / "state.json"
        no_prior_state = False
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            no_prior_state = True
            previous = {}
        except OSError as exc:
            # A failed read cannot prove an empty incident journal. Preserve
            # state.json and publish newly observed P1s via the heartbeat.
            return _record_unverifiable_prior_state(
                result, directory, previous_heartbeat,
                f"unreadable state JSON: {type(exc).__name__}",
                state_read_uncertain=True,
            )
        except (ValueError, UnicodeError) as exc:
            return _record_unverifiable_prior_state(
                result, directory, previous_heartbeat,
                f"malformed state JSON: {type(exc).__name__}",
                state_read_uncertain=True,
            )
        if not no_prior_state and not isinstance(previous, dict):
            return _record_unverifiable_prior_state(
                result, directory, previous_heartbeat, "state root is not object",
                state_read_uncertain=True,
            )
        if no_prior_state and isinstance(previous_heartbeat, dict):
            try:
                recovery_preimage = _verified_lost_state_recovery(
                    directory, previous_heartbeat, result,
                )
            except (OSError, ValueError, UnicodeError, TypeError) as exc:
                return _record_unverifiable_prior_state(
                    result, directory, previous_heartbeat,
                    f"cannot verify lost-state recovery: {type(exc).__name__}",
                )
            if recovery_preimage is not None:
                previous = recovery_preimage
                no_prior_state = False
            # Losing state.json cannot prove that a confirmed incident ended.
            # A reconfirmed P1 may reconstruct the missing state only after
            # checking whether its ALARM is the last journaled incident epoch.
            past_issues = previous_heartbeat.get("issues")
            prior_alarm = (
                previous_heartbeat.get("status") == "ALARM"
                or bool(previous_heartbeat.get("retained_prior_p1"))
                or bool(previous_heartbeat.get("pending_transitions"))
                or (isinstance(past_issues, list) and any(
                    isinstance(item, dict) and item.get("severity") == "P1"
                    for item in past_issues
                ))
            )
            if no_prior_state and prior_alarm:
                prior_actuals = {
                    item["code"]: str(item.get("actual", ""))[:160]
                    for item in (past_issues if isinstance(past_issues, list) else [])
                    if isinstance(item, dict) and item.get("severity") == "P1"
                    and isinstance(item.get("code"), str)
                }
                prior_retained = previous_heartbeat.get("retained_prior_p1")
                if isinstance(prior_retained, list):
                    prior_actuals.update({
                        item[0]: item[1][:160] for item in prior_retained
                        if isinstance(item, list) and len(item) == 2
                        and all(isinstance(x, str) for x in item)
                        and item[0] not in prior_actuals
                    })
                confirmed_now = {
                    item["code"]: str(item.get("actual", ""))[:160]
                    for item in result.get("issues", [])
                    if isinstance(item, dict) and item.get("severity") == "P1"
                    and isinstance(item.get("code"), str)
                }
                try:
                    pending_heartbeat = _pending_transition_entries(previous_heartbeat)
                    journal = _last_journaled_transition(directory)
                except (OSError, ValueError, UnicodeError, TypeError) as exc:
                    return _record_unverifiable_prior_state(
                        result, directory, previous_heartbeat,
                        f"cannot validate lost-state journal: {type(exc).__name__}",
                    )
                # Only a single reconfirmed ALARM is recoverable when the
                # journal is empty, its last RECOVERY predates the intent,
                # or the identical ALARM was already fsynced before a crash.
                # Ambiguous or mixed outboxes remain fail-closed.
                pending_alarm_codes = []
                if len(pending_heartbeat) == 1 and pending_heartbeat[0]["event"] == "ALARM":
                    pending_alarm_codes = [
                        item.get("code")
                        for item in pending_heartbeat[0]["payload"]["issues"]
                        if isinstance(item, dict) and item.get("severity") == "P1"
                    ]
                journal_precedes_pending = (
                    journal is None
                    or (
                        len(pending_heartbeat) == 1
                        and journal == pending_heartbeat[0]["payload"]
                        and journal.get("event") == "ALARM"
                    )
                )
                if (
                    not journal_precedes_pending
                    and journal.get("event") == "RECOVERY"
                    and len(pending_heartbeat) == 1
                ):
                    pending_at = timestamp(
                        pending_heartbeat[0]["payload"].get("checked_at_utc")
                    )
                    journal_at = timestamp(journal.get("checked_at_utc"))
                    journal_precedes_pending = (
                        pending_at is not None
                        and journal_at is not None
                        and pending_at > journal_at
                    )
                pending_replay = (
                    journal_precedes_pending
                    and len(pending_heartbeat) == 1
                    and previous_heartbeat.get("status") == "ALARM"
                    and bool(pending_alarm_codes)
                    and all(isinstance(code, str) and code for code in pending_alarm_codes)
                    and len(pending_alarm_codes) == len(set(pending_alarm_codes))
                    and set(pending_alarm_codes) == set(prior_actuals)
                )
                if (
                    not prior_actuals
                    or not prior_actuals.keys() <= confirmed_now.keys()
                    or (pending_heartbeat and not pending_replay)
                ):
                    return _record_unverifiable_prior_state(
                        result, directory, previous_heartbeat,
                        "state.json missing while heartbeat retains incident evidence",
                    )
                journaled_codes = set()
                if journal is not None and journal.get("event") == "ALARM":
                    issues = journal.get("issues")
                    if not isinstance(issues, list):
                        return _record_unverifiable_prior_state(
                            result, directory, previous_heartbeat,
                            "last ALARM journal payload malformed",
                        )
                    journaled_codes = {
                        item["code"] for item in issues
                        if isinstance(item, dict) and item.get("severity") == "P1"
                        and isinstance(item.get("code"), str)
                    }
                    retained = journal.get("retained_prior_p1")
                    if isinstance(retained, list):
                        journaled_codes.update(
                            item[0] for item in retained
                            if isinstance(item, list) and len(item) == 2
                            and isinstance(item[0], str)
                        )
                if prior_actuals.keys() <= journaled_codes or pending_replay:
                    # The ALARM was already fsynced or is one reconfirmed
                    # heartbeat intent awaiting replay. Rebuild active state;
                    # exact-byte journal deduplication prevents a second ALARM.
                    # A later healthy poll can then journal RECOVERY.
                    previous = {
                        "severe": sorted([
                            code, confirmed_now.get(code, old_actual)
                        ] for code, old_actual in prior_actuals.items()),
                        "uncertain": [],
                        "main_observation": previous_heartbeat.get("main_observation"),
                        "checked_at_utc": previous_heartbeat.get("checked_at_utc"),
                    }
                    no_prior_state = False
                elif journal is None or journal.get("event") == "RECOVERY":
                    # The heartbeat came from a pre-journal failure, or the
                    # history belongs to an earlier, recovered epoch. Admit
                    # one new durable ALARM instead of trapping it forever.
                    pass
                else:
                    # A different active journal incident is inconsistent
                    # with this heartbeat. Never overwrite it by guessing.
                    return _record_unverifiable_prior_state(
                        result, directory, previous_heartbeat,
                        "state.json missing and active journal disagrees with heartbeat",
                    )
        # Evaluation may have been unable to read this same file, but the
        # locked persistence step can now read the original anchor. Keep the
        # independently observed current main SHA, and merge only mismatches
        # still present on this poll. A newly converged component must not
        # inherit a stale mismatch just because a prior read failed.
        current_main = result.get("main_observation")
        previous_main = previous.get("main_observation") if isinstance(previous, dict) else None
        had_anchor_read_fault = any(
            item.get("code") == "unverifiable_state"
            for item in result.get("issues", [])
            if isinstance(item, dict)
        )
        if had_anchor_read_fault and isinstance(previous_main, dict):
            if isinstance(current_main, dict):
                observed_at = timestamp(result.get("checked_at_utc"))
                prior_first = timestamp(previous_main.get("first_seen"))
                current_first = timestamp(current_main.get("first_seen"))
                if (
                    current_main.get("sha") == previous_main.get("sha")
                    and prior_first is not None
                    and observed_at is not None
                    and prior_first <= observed_at
                    and (current_first is None or prior_first <= current_first)
                ):
                    current_main["first_seen"] = prior_first.isoformat().replace("+00:00", "Z")
                old_divergence = previous_main.get("diverged_since")
                current_divergence = current_main.get("diverged_since")
                old_divergence = old_divergence if isinstance(old_divergence, dict) else {}
                current_divergence = (
                    current_divergence if isinstance(current_divergence, dict) else {}
                )
                commits_now = result.get("commits")
                commits_now = commits_now if isinstance(commits_now, dict) else {}
                for name in ("frontend", "api"):
                    if name not in current_divergence:
                        # The preliminary state read failed, so evaluate()
                        # cannot recover the old per-component anchor itself.
                        # A missing API/frontend commit is NOT evidence of
                        # convergence. Carry its prior mismatch timestamp
                        # until the component is successfully observed.
                        prior_at = timestamp(old_divergence.get(name))
                        if (
                            name not in commits_now
                            and prior_at is not None
                            and observed_at is not None
                            and prior_at <= observed_at
                        ):
                            current_divergence[name] = (
                                prior_at.isoformat().replace("+00:00", "Z")
                            )
                        else:
                            # A reported commit equal to main proves that
                            # component has converged; do not retain the
                            # old mismatch just because another read failed.
                            continue
                    prior_at = timestamp(old_divergence.get(name))
                    fresh_at = timestamp(current_divergence[name])
                    if (
                        prior_at is not None and observed_at is not None
                        and prior_at <= observed_at
                        and (fresh_at is None or prior_at <= fresh_at)
                    ):
                        current_divergence[name] = prior_at.isoformat().replace("+00:00", "Z")
                    anchor_at = timestamp(current_divergence[name])
                    if (
                        anchor_at is None or observed_at is None
                        or name not in commits_now or "main" not in commits_now
                        or commits_now[name] == commits_now["main"]
                    ):
                        continue
                    age = (observed_at - anchor_at).total_seconds()
                    stale_code = "stale_" + name
                    if age >= THRESHOLD and not any(
                        isinstance(item, dict) and item.get("code") == stale_code
                        for item in result["issues"]
                    ):
                        # Reconstitute a P1 already overdue at this observation
                        # instead of silently postponing it until the next poll.
                        result["issues"].append({
                            "code": stale_code, "component": name,
                            "actual": commits_now[name], "expected": commits_now["main"],
                            "source": URLS[name], "severity": "P1",
                            "duration_seconds": round(age),
                        })
                        result["status"] = "ALARM"
                if current_divergence:
                    current_main["diverged_since"] = current_divergence
            else:
                current_main = previous_main
        elif not isinstance(current_main, dict):
            current_main = previous_main
        result["main_observation"] = current_main

        raw_severe = [] if no_prior_state else previous.get("severe")
        if not isinstance(raw_severe, list) or any(
            not isinstance(item, list) or len(item) != 2
            or not all(isinstance(value, str) for value in item)
            for item in raw_severe
        ):
            return _record_unverifiable_prior_state(
                result, directory, previous_heartbeat,
                "missing or malformed severe incident list",
            )
        # The incident state is replaced before heartbeat.json. A failed
        # heartbeat write must not let an older overlapping poll erase P1.
        state_checked = timestamp(previous.get("checked_at_utc"))
        checked = timestamp(result.get("checked_at_utc"))
        if not serialized_poll and state_checked is not None and (checked is None or checked < state_checked):
            return None
        old_severe = sorted(
            item for item in raw_severe
            if item[0] not in {"schedule_stale", "schedule_failed", "no_schedule"}
        )
        # State normally owns this outbox. A recovery writes heartbeat first;
        # if state.json is replaced but its directory fsync then fails, the
        # earlier heartbeat is the only durable evidence of that RECOVERY.
        try:
            pending = []
            for anchor in (previous, previous_heartbeat):
                for entry in _pending_transition_entries(anchor):
                    if entry not in pending:
                        pending.append(entry)
            # Reconcile already committed journal bytes BEFORE enforcing
            # capacity: state and heartbeat may contain overlapping snapshots
            # from opposite sides of a successful fsync.
            # Crash-consistent exact-once retry: the first append may be
            # durable even when its following state-clear fsync failed.
            # Any read failure remains an error, preserving prior anchors.
            pending = [
                entry for entry in pending
                if not _transition_already_journaled(directory, entry)
            ]
        except (OSError, ValueError, TypeError) as exc:
            return _record_unverifiable_prior_state(
                result, directory, previous_heartbeat,
                f"invalid pending transition outbox: {type(exc).__name__}",
            )
        # A transient read EIO must not drop an independently confirmed P1
        # from the fsynced heartbeat when the next poll is already HEALTHY.
        # Import it as an older episode only after the real state and journal
        # are readable and their timestamps/incident codes are consistent.
        tentative = []
        for anchor in (previous, previous_heartbeat):
            for entry in _pending_transition_entries(anchor):
                if (
                    entry["event"] == "ALARM"
                    and entry["payload"].get("state_read_unverified") is True
                    # Only as-yet unjournaled intents need provisional
                    # timeline admission. Already fsynced alarm/recovery
                    # batches were reconciled out of pending above.
                    and entry in pending
                    and entry not in tentative
                ):
                    tentative.append(entry)
        if tentative:
            try:
                # Reuse bounded append-time tail repair only for an actual
                # unterminated suffix. A complete journal needs no write:
                # an unnecessary empty append may itself fail and suppress
                # previously confirmed transitions.
                try:
                    with (directory / "events.jsonl").open("rb") as probe:
                        end = probe.seek(0, os.SEEK_END)
                        if end:
                            probe.seek(-1, os.SEEK_END)
                            if probe.read(1) != b"\n":
                                _append_durable_events(directory, [])
                except FileNotFoundError:
                    pass
                journal_last = _last_journaled_transition(directory)
            except (OSError, ValueError, UnicodeError, TypeError) as exc:
                return _record_unverifiable_prior_state(
                    result, directory, previous_heartbeat,
                    f"cannot verify tentative P1 journal: {type(exc).__name__}",
                )
            journal_codes = set()
            journal_at = None
            if journal_last is not None:
                journal_at = timestamp(journal_last.get("checked_at_utc"))
                if journal_last.get("event") == "ALARM":
                    journal_issues = journal_last.get("issues")
                    if not isinstance(journal_issues, list):
                        return _record_unverifiable_prior_state(
                            result, directory, previous_heartbeat,
                            "tentative P1 conflicts with invalid ALARM journal",
                        )
                    journal_codes = {
                        item.get("code") for item in journal_issues
                        if isinstance(item, dict) and item.get("severity") == "P1"
                        and isinstance(item.get("code"), str)
                    }
            checked = timestamp(result.get("checked_at_utc"))
            state_checked = timestamp(previous.get("checked_at_utc"))
            old_codes = {item[0] for item in old_severe}
            for entry in tentative:
                payload = entry["payload"]
                at = timestamp(payload.get("checked_at_utc"))
                items = payload.get("issues")
                # The earlier read-EIO P1 can be incorporated into the
                # authoritative state before its original journal append
                # succeeds. Retrying after another journal fsync failure
                # must not reject that same persisted intent just because
                # state.checked_at advanced. Require exact state-outbox
                # membership and either still-active codes or a later
                # independently confirmed pending RECOVERY.
                state_entries = _pending_transition_entries(previous)
                position = next(
                    (i for i, value in enumerate(state_entries) if value == entry),
                    None,
                )
                p1_codes = {
                    item.get("code") for item in items
                    if isinstance(item, dict)
                    and item.get("severity") == "P1"
                    and isinstance(item.get("code"), str) and item["code"]
                } if isinstance(items, list) else set()
                followed_by_recovery = False
                if position is not None and at is not None and state_checked is not None:
                    followed_by_recovery = any(
                        later["event"] == "RECOVERY"
                        and later["payload"].get("status")
                            in {"HEALTHY", "MONITOR_DATA_FAILURE"}
                        and isinstance(later["payload"].get("issues"), list)
                        and not any(
                            isinstance(issue, dict) and issue.get("severity") == "P1"
                            for issue in later["payload"]["issues"]
                        )
                        and timestamp(later["payload"].get("checked_at_utc")) is not None
                        and at < timestamp(later["payload"]["checked_at_utc"])
                            <= state_checked
                        for later in state_entries[position + 1:]
                    )
                state_reconciled = (
                    position is not None and bool(p1_codes)
                    and (p1_codes <= old_codes or followed_by_recovery)
                )
                if (
                    not isinstance(items, list) or not items
                    or payload.get("status") != "ALARM"
                    or at is None or checked is None or at > checked
                    or (state_checked is not None and at < state_checked
                        and not state_reconciled)
                    or (journal_last is not None and (
                        journal_at is None or journal_at > at
                    ))
                ):
                    return _record_unverifiable_prior_state(
                        result, directory, previous_heartbeat,
                        "unverifiable transient P1 timeline",
                    )
                p1 = [
                    item for item in items
                    if isinstance(item, dict) and item.get("severity") == "P1"
                    and isinstance(item.get("code"), str) and item["code"]
                ]
                codes = [item["code"] for item in p1]
                if not codes or len(codes) != len(set(codes)) or len(p1) != len(items):
                    return _record_unverifiable_prior_state(
                        result, directory, previous_heartbeat,
                        "malformed transient P1 evidence",
                    )
                if set(codes) <= old_codes and set(codes) <= journal_codes:
                    # This old incident is already represented and journaled.
                    # Never emit a second ALARM just for a transient read EIO.
                    if entry in pending:
                        pending.remove(entry)
                    continue
                if followed_by_recovery:
                    # The state already durably owns a newer confirmed
                    # recovery epoch. Replay the older ALARM before its
                    # pending RECOVERY; never re-activate its P1 as current.
                    continue
                for item in p1:
                    code = item["code"]
                    if code not in old_codes:
                        old_severe.append([code, str(item.get("actual", ""))[:160]])
                        old_codes.add(code)
            old_severe.sort()
        # A RECOVERY is committed to heartbeat before state to avoid
        # inventing a recovered incident on a failed state write. If that
        # precise newer heartbeat exists but state still owns the older
        # severe episode, a separately confirmed recurrence is a NEW epoch,
        # not a continuation of the stale state. The old RECOVERY remains
        # pending and must be persisted before any journal append.
        if old_severe and isinstance(previous_heartbeat, dict):
            heartbeat_pending = _pending_transition_entries(previous_heartbeat)
            state_pending = _pending_transition_entries(previous)
            heartbeat_recovery = (
                heartbeat_pending[-1] if heartbeat_pending
                and heartbeat_pending[-1]["event"] == "RECOVERY" else None
            )
            heartbeat_time = timestamp(previous_heartbeat.get("checked_at_utc"))
            state_time = timestamp(previous.get("checked_at_utc"))
            recovery_payload = (
                heartbeat_recovery["payload"] if heartbeat_recovery is not None else {}
            )
            recovery_time = timestamp(recovery_payload.get("checked_at_utc"))
            # The error fallback may copy the verified recovery into state and
            # advance both snapshot timestamps. Its ORIGINAL payload time is
            # then the episode boundary, not the newer failure heartbeat time.
            recovery_retried_into_state = (
                heartbeat_recovery is not None
                and heartbeat_recovery in state_pending
                and recovery_time is not None
                and state_time is not None
                and state_time >= recovery_time
                and result.get("status") == "ALARM"
                and any(
                    isinstance(issue, dict) and issue.get("severity") == "P1"
                    for issue in result.get("issues", [])
                )
                and timestamp(result.get("checked_at_utc")) is not None
                and recovery_time < timestamp(result["checked_at_utc"])
            )
            recovery_only_in_heartbeat = (
                heartbeat_recovery is not None
                and heartbeat_recovery not in state_pending
                and recovery_time == heartbeat_time
            )
            if (
                # A failed write is never proof of recovery: require the old
                # durable, P1-free confirmed RECOVERY payload and FIFO tail.
                result.get("status") in {"ALARM", "HEALTHY"}
                and recovery_payload.get("status") in {"HEALTHY", "MONITOR_DATA_FAILURE"}
                and isinstance(recovery_payload.get("issues"), list)
                and not any(
                    isinstance(issue, dict) and issue.get("severity") == "P1"
                    for issue in recovery_payload["issues"]
                )
                and heartbeat_recovery in pending
                and pending[-1] == heartbeat_recovery
                and heartbeat_time is not None
                and state_time is not None
                and heartbeat_time >= state_time
                and (recovery_only_in_heartbeat or recovery_retried_into_state)
            ):
                old_severe = []
        # The emergency heartbeat may have observed B before the old queue
        # could be drained and state.json replaced. B is a *durable* prior P1
        # even when it vanishes on the very next poll; otherwise its journaled
        # ALARM could never receive a genuine aggregate RECOVERY.
        if isinstance(previous_heartbeat, dict):
            overflow = previous_heartbeat.get("pending_surge_overflow", [])
            known_codes = {item[0] for item in old_severe}
            # Old recovery boundaries matter. A historical ALARM that was
            # followed by a durable RECOVERY does not remain a current P1:
            # treating every overflow ALARM as active silently suppresses the
            # *next* genuine ALARM with the same incident code.
            for entry in reversed(overflow):
                if entry["event"] == "RECOVERY":
                    break
                for issue in entry["payload"]["issues"]:
                    if not isinstance(issue, dict):
                        continue
                    code = issue.get("code")
                    if (issue.get("severity") == "P1"
                            and isinstance(code, str) and code
                            and code not in known_codes):
                        old_severe.append([code, str(issue.get("actual", ""))[:160]])
                        known_codes.add(code)
            old_severe.sort()
        old_uncertain = previous.get("uncertain")
        old_uncertain = old_uncertain if isinstance(old_uncertain, list) else []
        severe = sorted([x["code"], str(x["actual"])[:160]]
                        for x in result["issues"] if x["severity"] == "P1")
        observed_p1_codes = {item[0] for item in severe}
        uncertain = sorted(x["code"] for x in result["issues"] if x["severity"] == "P2")
        observed_codes = {item["code"] for item in result["issues"]}
        # A capacity-degraded cron has deliberately NOT evaluated endpoints.
        # Carry only old durable incident state, without asserting an unseen
        # ALARM or inferring a RECOVERY during journal backpressure.
        if suppress_transition_event and "watcher_outbox_at_capacity" in observed_codes:
            severe = list(old_severe)
            if severe:
                result["retained_prior_p1"] = severe
                result["observation_status"] = result["status"]
                result["status"] = "ALARM"
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
                or "unverifiable_state" in observed_codes
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

        def recovery_replay_blocked(index, unknown_codes):
            # A prior state can still mention incident A when its already
            # durable heartbeat records RECOVERY(A). Independently confirmed
            # incident B must not hold that recovery behind itself.
            # If any old P1 code is still active, permit the older recovery
            # only when a NEW ALARM has been queued after it. This preserves
            # FIFO episode order across transient journal failures without
            # clearing a continuing old incident on an uncertain observation.
            if unknown_codes:
                return True
            if not (old_codes & new_codes):
                return False
            return not any(
                later["event"] == "ALARM" for later in pending[index + 1:]
            )

        # The fallback retry must not re-emit a transition the first call
        # may already have committed to the local event journal.
        events = [event for event in (
            None if suppress_transition_event else transition, uncertainty_event,
        ) if event]
        # Include the actual first-observation payload, not a synthetic later
        # poll's status. Both incident anchors persist this bounded outbox.
        previous_pending_count = len(pending)
        drained_event = None
        for event_name in events:
            if event_name in {"ALARM", "RECOVERY"}:
                # The old state may still contain the incident when the
                # recovery's first snapshot failed before os.replace. The
                # heartbeat already carries that exact uncommitted RECOVERY;
                # do not add a second event with a later checked_at timestamp.
                if event_name == "RECOVERY":
                    # A retry of the SAME recovery leaves the earlier
                    # outbox RECOVERY at its tail. An independent ALARM
                    # queued after it begins another incident epoch, whose
                    # later recovery must not be suppressed.
                    latest_recovery = next(
                        (index for index in range(len(pending) - 1, -1, -1)
                         if pending[index]["event"] == "RECOVERY"),
                        None,
                    )
                    if latest_recovery is not None and not any(
                        entry["event"] == "ALARM"
                        for entry in pending[latest_recovery + 1:]
                    ):
                        continue
                payload = {
                    "event": event_name,
                    **{key: deepcopy(value) for key, value in result.items()
                       if key not in {"pending_transitions", "pending_surge_overflow"}},
                    "issue_url": "https://github.com/heimgewebe/commonthing/issues/1939",
                }
                entry = {"event": event_name, "payload": payload}
                if entry not in pending:
                    pending.append(entry)

        def persist_snapshots(*, heartbeat_first=False):
            # Keep the legacy 17-entry projection and store the remaining FIFO
            # as an explicitly bounded continuation under the same fsync.
            result.pop("pending_surge_overflow", None)
            if pending:
                result["pending_transitions"] = pending[:OUTBOX_SURGE_LIMIT]
                if len(pending) > OUTBOX_SURGE_LIMIT:
                    result["pending_surge_overflow"] = pending[OUTBOX_SURGE_LIMIT:]
            else:
                result.pop("pending_transitions", None)
            state_snapshot = {
                "severe": severe, "uncertain": uncertain,
                "main_observation": current_main,
                "checked_at_utc": result["checked_at_utc"],
            }
            for key in ("pending_transitions", "pending_surge_overflow"):
                if key in result:
                    state_snapshot[key] = result[key]
            if heartbeat_first:
                write_json(directory / "heartbeat.json", result)
            write_json(path, state_snapshot)
            if not heartbeat_first:
                write_json(directory / "heartbeat.json", result)

        if pending:
            # The durable bound is a byte budget, not a magic event count.
            # At an actual full filesystem no algorithm can promise to
            # capture an arbitrary number of new observations. The normal
            # cron admission check below suspends new polls before that point.
            pending_bytes = sum(
                len(json.dumps(entry, sort_keys=True, separators=(",", ":")).encode("utf-8")) + 1
                for entry in pending
            )
            if pending_bytes > OUTBOX_MAX_BYTES:
                return _record_unverifiable_prior_state(
                    result, directory, previous_heartbeat,
                    "pending transition storage exhausted: observer must stop",
                )
        if len(pending) > OUTBOX_NORMAL_LIMIT:
            # Persist ALL newly confirmed P1s, RECOVERY intents and the
            # entire FIFO before writing even one older journal event.
            # On crash or fsync failure, both anchors can replay exactly
            # these bytes. A later drain never precedes its durable intent.
            persist_snapshots(heartbeat_first=any(
                entry["event"] == "RECOVERY" for entry in pending
            ))
            uncertain_replay = {
                "watcher_execution_failed", "unverifiable_state"
            } & observed_codes
            ready_old = []
            for index, entry in enumerate(pending[:previous_pending_count]):
                if entry["event"] == "RECOVERY" and recovery_replay_blocked(
                    index, uncertain_replay,
                ):
                    break
                ready_old.append(entry)
            if len(ready_old) < len(pending) - OUTBOX_NORMAL_LIMIT:
                return _record_unverifiable_prior_state(
                    result, directory, previous_heartbeat,
                    "pending transition outbox at capacity with blocked recovery",
                )
            try:
                _append_durable_events(
                    directory, [entry["payload"] for entry in ready_old],
                )
            except OSError as exc:
                return _record_unverifiable_prior_state(
                    result, directory, previous_heartbeat,
                    f"pending outbox drain failed: {type(exc).__name__}",
                )
            drained_event = ready_old[-1]["event"] if ready_old else None
            pending = pending[len(ready_old):]
        # Journal order must follow the outbox exactly. An older RECOVERY
        # can only be committed once its state/heartbeat recovery anchors
        # are verified. If it is still blocked, later ALARMs are held too;
        # otherwise a new incident could overtake an older recovery.
        # A previous confirmed recovery followed by a NEW P1 is replayable
        # when the preceding state was already clear (old_severe == []).
        unverified = {"watcher_execution_failed", "unverifiable_state"} & observed_codes
        ready_pending = []
        if not suppress_transition_event:
            for index, entry in enumerate(pending):
                if entry["event"] == "RECOVERY" and recovery_replay_blocked(
                    index, unverified,
                ):
                    break
                ready_pending.append(entry)
        replayed_event = next(
            (entry["event"] for entry in ready_pending
             if entry not in _pending_transition_entries(previous)
             and entry not in _pending_transition_entries(previous_heartbeat)),
            None,
        )
        if replayed_event is None and ready_pending:
            replayed_event = ready_pending[0]["event"]

        # Durably record ALARM intent before the journal write. Only a
        # fully committed recovery in both state/heartbeat may be journaled;
        # failed writes must never fabricate recovered history.
        recovery_before_event = (
            any(entry["event"] == "RECOVERY" for entry in ready_pending)
        )
        # Persist transition intent BEFORE the first journal append. Without
        # this durable preimage, a crash after ALARM fsync and before the
        # later snapshot would lose the state and cause duplicate alarms or
        # a permanently missing recovery. Recovery still writes heartbeat
        # first, retaining the original confirmed P1 until state commits.
        if ready_pending:
            persist_snapshots(heartbeat_first=recovery_before_event)
        event_log_failed = False
        journal_events = [entry["payload"] for entry in ready_pending]
        journal_events.extend(
            {"event": event_name, **{
                key: value for key, value in result.items()
                if key not in {"pending_transitions", "pending_surge_overflow"}
            }, "issue_url": "https://github.com/heimgewebe/commonthing/issues/1939"}
            for event_name in events if event_name == "MONITOR_DATA_FAILURE"
        )
        if journal_events:
            try:
                _append_durable_events(
                    directory, journal_events, committed_event_names,
                )
                # Append and both syncs finished; remove only those entries
                # actually published. Any deferred recovery stays pending.
                pending[:] = [
                    entry for entry in pending if entry not in ready_pending
                ]
            except OSError as exc:
                event_log_failed = True
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
        # A failed journal append adds a P2 to the report. For a committed
        # recovery, refresh both snapshots only in that case; otherwise
        # ordinary observations keep the established write order.
        if not recovery_before_event or event_log_failed or ready_pending:
            persist_snapshots()
    return new_event or (replayed_event if not event_log_failed else None) or drained_event


def read_previous_main_observation(directory):
    try:
        state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
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


def _observation_admitted(directory):
    """Stop *before* confirming an event if the durable FIFO has no room.

    Finite storage cannot preserve an unbounded outage. Rather than confirm
    a P1 and silently discard its intent at the byte budget, suspend ordinary
    polls with explicit observer failure until the existing journal catches up.
    """
    with (directory / "lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        for name in ("state.json", "heartbeat.json"):
            try:
                snapshot = json.loads((directory / name).read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            except (ValueError, UnicodeError):
                if name == "state.json":
                    # A corrupt incident anchor is not proof of a full outbox.
                    # Observe product endpoints anyway; record() preserves the
                    # original bytes and reports new P1s in the heartbeat.
                    # The separately readable heartbeat still limits capacity.
                    continue
                return False
            except OSError:
                return False
            try:
                pending = _pending_transition_entries(snapshot)
            except (ValueError, TypeError):
                return False
            bytes_used = sum(
                len(json.dumps(entry, sort_keys=True, separators=(",", ":")).encode("utf-8")) + 1
                for entry in pending
            )
            if bytes_used > OUTBOX_MAX_BYTES - OUTBOX_ADMISSION_RESERVE:
                return False
    return True


def _evaluate_cycle(directory):
    now = datetime.now(UTC)
    observed_issues = []
    try:
        try:
            previous = read_previous_main_observation(directory)
        except (OSError, ValueError) as exc:
            # Prior state is uncertain, but the live product endpoints can
            # still independently confirm critical HTTP/cache failures.
            previous = None
            observed_issues.append({
                "code": "unverifiable_state",
                "component": "watcher previous incident state",
                "actual": f"{type(exc).__name__}: {str(exc)[:150]}",
                "expected": "readable prior state.json",
                "source": str(directory / "state.json"),
                "severity": "P2",
            })
        return evaluate(
            now, previous_main_observation=previous, issue_sink=observed_issues,
        )
    except (WatchRuntimeExceeded, Exception) as exc:
        # Only preserve confirmed P1s collected before the failing read.
        # Unknown later endpoints remain a P2 observer failure.
        failure = watcher_failure_report(now, exc)
        confirmed_p1 = [
            item for item in observed_issues
            if isinstance(item, dict) and item.get("severity") == "P1"
        ]
        if confirmed_p1:
            failure["issues"].extend(confirmed_p1)
            failure["status"] = "ALARM"
        return failure


def _persist_cycle(result, directory, *, suppress_transition_event=False):
    # SIGALRM is one-shot. Evaluation may already have consumed the whole
    # budget; rearm so a blocked state/heartbeat fsync cannot hang cron.
    signal.alarm(10)
    committed_events = set()
    try:
        event = record(
            result, directory, serialized_poll=True,
            suppress_transition_event=suppress_transition_event,
            committed_event_names=committed_events,
        )
    except (WatchRuntimeExceeded, Exception) as exc:
        # Best effort only. No local write can prove its own availability when
        # storage is completely inaccessible; an external liveness watcher
        # remains necessary.
        failure = watcher_failure_report(datetime.now(UTC), exc)
        # The first write may fail before persisting a fully observed new main
        # and its frontend/API divergence grace. Keep that primary observation
        # in the fallback snapshot; a blank failure report would restart the
        # 45-minute clock on every subsequent failed first write. This is
        # evidence from the completed poll, not a synthetic healthy status.
        completed_main = result.get("main_observation")
        if isinstance(completed_main, dict):
            failure["main_observation"] = completed_main
        # A failed state replace must not turn a confirmed NEW product P1
        # into generic P2 uncertainty. Preserve the observation from this
        # poll even when no prior state.json contains the incident yet.
        observed_p1 = [
            item for item in result.get("issues", [])
            if isinstance(item, dict) and item.get("severity") == "P1"
        ]
        if observed_p1:
            failure["issues"].extend(observed_p1)
            failure["status"] = "ALARM"
        signal.alarm(10)
        try:
            record(
                failure, directory, serialized_poll=True,
                suppress_transition_event=bool(
                    committed_events & {"ALARM", "RECOVERY"}
                ),
            )
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
    # Cron inherits the user's umask (observed 0002 on Heimberry). New
    # incident journals and lockfiles must not be writable by the group.
    previous_umask = os.umask(0o077)
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
            admitted = _observation_admitted(args.state_dir)
            if admitted:
                result = _evaluate_cycle(args.state_dir)
            else:
                result = watcher_failure_report(
                    datetime.now(UTC),
                    ValueError("durable watcher outbox at capacity or unverifiable"),
                )
                # This is a capacity report, NOT a new product observation.
                # Do not block replay of earlier, already verified recoveries
                # with the generic "watcher_execution_failed" uncertainty.
                result["issues"][0].update({
                    "code": "watcher_outbox_at_capacity",
                    "component": "watcher durable event queue",
                    "expected": "journal drain frees admission headroom",
                    "source": str(args.state_dir / "state.json"),
                })
            return _persist_cycle(
                result, args.state_dir,
                suppress_transition_event=not admitted,
            )
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)
        os.umask(previous_umask)


if __name__ == "__main__":
    sys.exit(main())