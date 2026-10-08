#!/usr/bin/env python3
"""Classify a production live receipt against the current main lineage.

`verify_public_release_commit.py` answers one question: is the expected commit
live? A "no" can mean very different things, and only some of them need a
human. This classifier separates them:

* ``current``     the expected commit is live and consistent and still main's head.
* ``superseded``  main's head, a newer commit that contains the expected one, is live
                  and passes every receipt check against that newer commit.
                  This run is not a production proof; the newer commit's own
                  run is. It never resolves an open alert.
* ``pending``     production still serves an older main commit, but the first
                  main commit after it landed less than the grace period ago.
                  Later merges do not restart that clock.
* ``stale``       production still serves an older main commit after the grace
                  period: the deploy or the reconciler is stuck.
* ``divergent``   frontend and API disagree, or the live commit is not on the
                  main lineage at all.
* ``invalid``     the expected (or a newer main) commit is live, but the
                  receipt fails for another reason (cache headers, build
                  headers, artifact declaration).
* ``outage``      an endpoint could not be read or did not answer 200.
* ``monitor_failure`` the receipt is missing or unreadable.

The Schaubild release convergence is a mandatory part of the same contract.
When ``--schaubild-receipt`` is given and that receipt is missing or not
``current``, an otherwise non-alerting state becomes ``invalid``; no run can
resolve an alert while Schaubild has not converged.

Only ``current``, ``superseded`` and ``pending`` exit 0.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable

COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
# Matches the push run's 1200 s wait: once that wait is over, a lag is stale.
DEFAULT_PENDING_GRACE_SECONDS = 1200
NON_ALERTING_STATES = frozenset({"current", "superseded", "pending"})
SCHAUBILD_SCHEMA = "weltgewebe-schauwerk-release-convergence.v1"


@dataclass(frozen=True)
class Classification:
    schema_version: int
    state: str
    alert: bool
    expected_commit: str
    main_commit: str
    live_commit: str | None
    reason: str
    frontend_commit: str | None = None
    api_commit: str | None = None
    # "only" when Schaubild is the sole alert cause, "also" when it fails next
    # to another alert, so recovery of either cause changes the fingerprint.
    schaubild: str | None = None
    # The failed contract checks behind an ``invalid`` state, so a change of
    # cause on the same commit (one check repaired, another broken) is visible.
    causes: tuple[str, ...] = ()

    def fingerprint(self) -> str:
        """Stable identity of an alert, used to deduplicate notifications."""
        if self.live_commit is None and (self.frontend_commit or self.api_commit):
            # A split or partly unreadable deployment has no single live
            # commit; keep both sides so A/B -> B/C or a moving outage is a change.
            identity = f"{self.state}:{self.frontend_commit or 'none'}/{self.api_commit or 'none'}"
        else:
            identity = f"{self.state}:{self.live_commit or 'none'}"
        if self.causes:
            digest = hashlib.sha256("\n".join(sorted(self.causes)).encode()).hexdigest()
            identity = f"{identity}#{digest[:12]}"
        return f"{identity}+schaubild-{self.schaubild}" if self.schaubild else identity


def _is_commit(value: Any) -> bool:
    return isinstance(value, str) and bool(COMMIT_RE.fullmatch(value))


def git_is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool | None:
    """True/False from `git merge-base --is-ancestor`; None if git cannot tell."""
    completed = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, descendant],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    return None


def git_commit_time(repo: Path, commit: str) -> int | None:
    completed = subprocess.run(
        ["git", "-C", str(repo), "show", "-s", "--format=%ct", commit],
        check=False,
        capture_output=True,
        text=True,
    )
    raw = completed.stdout.strip()
    if completed.returncode != 0 or not raw.isdigit():
        return None
    return int(raw)


def git_rollout_start(repo: Path, live: str, target: str) -> int | None:
    """Committer time of the first main commit after ``live`` on the way to ``target``.

    The rollout has been owed since that commit landed. Measuring from
    ``target`` instead would let every further merge restart the grace period
    while production stays stuck on ``live``.
    """
    completed = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--first-parent", "--reverse", f"{live}..{target}"],
        check=False,
        capture_output=True,
        text=True,
    )
    lines = completed.stdout.split()
    if completed.returncode != 0 or not lines:
        return None
    return git_commit_time(repo, lines[0])


def revalidate_against(receipt: dict[str, Any], commit: str) -> list[str]:
    """Re-run the receipt checks as if ``commit`` had been the expected one.

    A newer live commit fails the original receipt for its commit fields
    alone. Every other invariant (cache headers, build headers, artifact
    declaration) must still hold before the run may count as ``superseded``.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import verify_public_release_commit as verify
    finally:
        sys.path.pop(0)

    def endpoint(raw: Any) -> Any:
        tree = raw.get("artifact_tree")
        return verify.EndpointResult(
            url=str(raw.get("url") or ""),
            status=raw.get("status"),
            commit=raw.get("commit"),
            version=raw.get("version"),
            headers={str(k).lower(): str(v) for k, v in (raw.get("headers") or {}).items()},
            error=raw.get("error"),
            artifact_tree=verify.ArtifactTreeResult(**tree) if isinstance(tree, dict) else None,
        )

    try:
        result = verify.evaluate(commit, endpoint(receipt["frontend"]), endpoint(receipt["api"]))
    except (KeyError, TypeError, AttributeError) as exc:
        return [f"receipt cannot be revalidated against {commit}: {exc}"]
    return list(result.reasons)


def classify(
    receipt: dict[str, Any] | None,
    *,
    expected_commit: str,
    main_commit: str,
    now: float,
    pending_grace_seconds: int,
    is_ancestor: Callable[[str, str], bool | None],
    rollout_start: Callable[[str, str], int | None],
) -> Classification:
    observed: dict[str, str | None] = {"frontend": None, "api": None}

    def result(
        state: str, live: str | None, reason: str, causes: tuple[str, ...] = ()
    ) -> Classification:
        return Classification(
            schema_version=1,
            state=state,
            alert=state not in NON_ALERTING_STATES,
            expected_commit=expected_commit,
            main_commit=main_commit,
            live_commit=live,
            reason=reason,
            frontend_commit=observed["frontend"],
            api_commit=observed["api"],
            causes=causes,
        )

    def failed(state: str, live: str | None, reason: str) -> Classification:
        # Without a single live commit, the reason is the failure identity:
        # HTTP 500 turning into a timeout on the same commits is a change.
        return result(state, live, reason, (reason,))

    if not isinstance(receipt, dict):
        return result("monitor_failure", None, "production live receipt is missing or unreadable")

    frontend = receipt.get("frontend") if isinstance(receipt.get("frontend"), dict) else {}
    api = receipt.get("api") if isinstance(receipt.get("api"), dict) else {}
    frontend_commit = frontend.get("commit")
    api_commit = api.get("commit")
    # Recorded before any return so outage and split fingerprints carry
    # whichever side is still readable.
    observed["frontend"] = frontend_commit if _is_commit(frontend_commit) else None
    observed["api"] = api_commit if _is_commit(api_commit) else None

    for name, endpoint in (("frontend", frontend), ("api", api)):
        if endpoint.get("error") or endpoint.get("status") != 200:
            return failed(
                "outage",
                None,
                f"{name} readback failed: HTTP {endpoint.get('status')!r}, "
                f"error {endpoint.get('error')!r}",
            )

    if not (_is_commit(frontend_commit) and _is_commit(api_commit)):
        return failed(
            "divergent",
            None,
            f"live commit is not a full SHA on both endpoints: frontend {frontend_commit!r}, "
            f"API {api_commit!r}",
        )
    if frontend_commit != api_commit:
        return failed(
            "divergent",
            None,
            f"frontend serves {frontend_commit}, API serves {api_commit}",
        )
    live = frontend_commit

    def lag(live: str, target: str) -> Classification:
        # The grace period excuses only commit lag, never another contract failure.
        failures = revalidate_against(receipt, live)
        if failures:
            return result("invalid", live, "; ".join(failures), tuple(failures))
        # The clock starts at the first commit after ``live``, so a stream of
        # merges cannot keep a stuck deployment pending forever.
        owed_since = rollout_start(live, target)
        if owed_since is None:
            return result(
                "stale", live, f"rollout start after {live} is unknown; treating lag as stale"
            )
        age = int(now) - owed_since
        if age < 0:
            # A future timestamp (clock skew, imported commit) must not extend
            # the grace period indefinitely.
            return result(
                "stale", live, f"the first commit after {live} is dated {-age}s in the future"
            )
        if age < pending_grace_seconds:
            return result(
                "pending",
                live,
                f"older main commit {live} is live; rollout towards {target} is owed for "
                f"{age}s (grace {pending_grace_seconds}s)",
            )
        return result(
            "stale",
            live,
            f"older main commit {live} is still live {age}s after the next main commit landed",
        )

    if live == expected_commit:
        if receipt.get("pass") is True:
            if main_commit == expected_commit:
                return result("current", live, "expected commit is live and consistent")
            # main moved after the run resolved its target: this receipt proves
            # an older commit and must not resolve an alarm for the newer one.
            if is_ancestor(live, main_commit):
                return lag(live, main_commit)
            return result("divergent", live, f"live commit {live} is not on main {main_commit}")
        reasons = receipt.get("reasons")
        causes = tuple(str(r) for r in reasons) if isinstance(reasons, list) and reasons else (
            "receipt did not pass",
        )
        return result("invalid", live, "; ".join(causes), causes)

    expected_is_older = is_ancestor(expected_commit, live)
    if expected_is_older:
        on_main = is_ancestor(live, main_commit)
        if on_main:
            if live != main_commit:
                # An intermediate commit is live while main has moved further.
                return lag(live, main_commit)
            failures = revalidate_against(receipt, live)
            if failures:
                return result("invalid", live, "; ".join(failures), tuple(failures))
            return result(
                "superseded",
                live,
                f"newer main commit {live} containing {expected_commit} is live",
            )
        return result("divergent", live, f"live commit {live} is not on main {main_commit}")

    live_is_older = is_ancestor(live, expected_commit)
    if live_is_older:
        return lag(live, expected_commit)

    if expected_is_older is None or live_is_older is None:
        return result("divergent", live, f"live commit {live} is unknown to the main history")
    return result("divergent", live, f"live commit {live} diverges from {expected_commit}")


def schaubild_failure(path: Path) -> str | None:
    """Reason the Schaubild convergence receipt fails, or None if it is current."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"Schaubild release convergence receipt is unreadable: {exc}"
    if not isinstance(payload, dict):
        return "Schaubild release convergence receipt is not an object"
    if payload.get("state") != "current" or payload.get("action_required") is not False:
        # The release pair and the error are part of the failure, so a new
        # desired release or a new diagnosis is reported, not deduplicated away.
        details = "".join(
            f", {key} {payload[key]!r}"
            for key in ("locked_source_commit", "desired_source_commit", "error")
            if key in payload
        )
        return (
            f"Schaubild release convergence is not current: state {payload.get('state')!r}, "
            f"action_required {payload.get('action_required')!r}{details}"
        )
    # A bare {"state": "current"} is no proof: the producer's shape and an
    # agreeing release pair must be present before Schaubild counts as converged.
    locked = payload.get("locked_source_commit")
    desired = payload.get("desired_source_commit")
    if payload.get("schema_version") != SCHAUBILD_SCHEMA:
        return (
            "Schaubild release convergence receipt has schema "
            f"{payload.get('schema_version')!r}, expected {SCHAUBILD_SCHEMA!r}"
        )
    if not (_is_commit(locked) and _is_commit(desired)) or locked != desired:
        return (
            "Schaubild release convergence receipt reports current without an agreeing "
            f"release pair: locked {locked!r}, desired {desired!r}"
        )
    return None


def with_schaubild(classification: Classification, failure: str | None) -> Classification:
    if failure is None:
        return classification
    if classification.alert:
        # Keep the more specific alert, but record Schaubild so it stays visible.
        return replace(
            classification,
            reason=f"{classification.reason}; {failure}",
            schaubild="also",
            causes=(*classification.causes, failure),
        )
    return replace(
        classification,
        state="invalid",
        alert=True,
        reason=failure,
        schaubild="only",
        causes=(failure,),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--main-commit", required=True)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument(
        "--pending-grace-seconds", type=int, default=DEFAULT_PENDING_GRACE_SECONDS
    )
    parser.add_argument("--schaubild-receipt", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def load_receipt(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for name in ("expected_commit", "main_commit"):
        if not COMMIT_RE.fullmatch(getattr(args, name)):
            print(f"ERROR: --{name.replace('_', '-')} must be a full SHA", file=sys.stderr)
            return 2
    if args.pending_grace_seconds < 0:
        print("ERROR: --pending-grace-seconds must not be negative", file=sys.stderr)
        return 2

    classification = classify(
        load_receipt(args.receipt),
        expected_commit=args.expected_commit,
        main_commit=args.main_commit,
        now=time.time(),
        pending_grace_seconds=args.pending_grace_seconds,
        is_ancestor=lambda a, d: git_is_ancestor(args.repo, a, d),
        rollout_start=lambda live, target: git_rollout_start(args.repo, live, target),
    )
    if args.schaubild_receipt is not None:
        classification = with_schaubild(classification, schaubild_failure(args.schaubild_receipt))
    payload = asdict(classification)
    payload["fingerprint"] = classification.fingerprint()
    args.output.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    print(f"production_live_state={classification.state} reason={classification.reason}")
    return 1 if classification.alert else 0


if __name__ == "__main__":
    raise SystemExit(main())
