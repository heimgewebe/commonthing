#!/usr/bin/env python3
"""Generate and verify hash-bound Weltgewebe review evidence.

The module is intentionally standard-library only.  It is executed from the
trusted default branch by the review-evidence workflow; pull-request code is
never imported or executed by that workflow.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

if __package__:
    from .attention_impact_contract import (
        attention_impact_decision,
        canonical_product_docs_from_manifest_text,
        product_logic_changes,
        validate_attention_impact_contract_binding,
    )
else:
    from attention_impact_contract import (
        attention_impact_decision,
        canonical_product_docs_from_manifest_text,
        product_logic_changes,
        validate_attention_impact_contract_binding,
    )

SCHEMA_VERSION = 1
MAX_ARTIFACT_BYTES = 100 * 1024 * 1024
MAX_EVIDENCE_PAYLOAD_BYTES = 20 * 1024
MIN_REVIEW_REPORT_BYTES = 120
SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
REPORT_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GITHUB_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
SAFE_OPAQUE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico"}
)
SAFE_OPAQUE_PREFIXES = (
    "docs/",
    "apps/web/static/",
    "apps/web/src/lib/assets/",
    "static/",
)
RISK_ORDER = {"R0": 0, "R1": 1, "R2": 2, "R3": 3}
REQUIRED_REVIEWS = {"R0": 0, "R1": 1, "R2": 2, "R3": 2}
AUTHORIZED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
HIGH_RISK_AXES = {
    "security",
    "privacy",
    "data-integrity",
    "concurrency",
    "migration",
    "operations",
}
ALLOWED_AXES = HIGH_RISK_AXES | {
    "correctness",
    "regression",
    "architecture",
    "maintainability",
    "accessibility",
    "user-experience",
    "testing",
}
REVIEW_MODES = {"external", "self-primary", "self-fallback"}
REVIEW_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
MAX_FALLBACK_REASON_BYTES = 512
RISK_RE = re.compile(r"<!--\s*weltgewebe-risk:\s*(R[0-3])\s*-->", re.IGNORECASE)
EVIDENCE_START_RE = re.compile(r"<!--\s*weltgewebe-review-evidence\b", re.IGNORECASE)
FORWARDED_EVIDENCE_START_RE = re.compile(r"<!--\s*weltgewebe-forwarded-review\b", re.IGNORECASE)


class GovernanceError(RuntimeError):
    """A fail-closed governance error."""


def load_allowed_attesters(path: Path) -> frozenset[str]:
    raw = _load_json(path)
    required = {"schema_version", "allowed_attesters"}
    if not isinstance(raw, dict) or set(raw) != required:
        raise GovernanceError(
            "attester authority file must contain exactly schema_version and allowed_attesters"
        )
    if raw["schema_version"] != SCHEMA_VERSION:
        raise GovernanceError("unsupported attester authority schema")
    values = raw["allowed_attesters"]
    if not isinstance(values, list) or not values:
        raise GovernanceError("allowed_attesters must be a non-empty list")
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str) or value != value.strip():
            raise GovernanceError("allowed_attesters contains an invalid login")
        if not GITHUB_LOGIN_RE.fullmatch(value):
            raise GovernanceError("allowed_attesters contains an invalid GitHub login")
        normalized.append(value.casefold())
    if len(set(normalized)) != len(normalized):
        raise GovernanceError("allowed_attesters must not contain duplicates")
    return frozenset(normalized)


@dataclass(frozen=True)
class DiffStats:
    changed_files: tuple[str, ...]
    additions: int
    deletions: int
    binary_files: tuple[str, ...]
    opaque_files: tuple[str, ...] = ()
    attention_changed_files: tuple[str, ...] = ()

    @property
    def changed_lines(self) -> int:
        return self.additions + self.deletions

    @property
    def attention_files(self) -> tuple[str, ...]:
        return self.attention_changed_files or self.changed_files


@dataclass(frozen=True)
class Bundle:
    pr_number: int
    base_sha: str
    head_sha: str
    merge_base_sha: str
    diff_sha256: str
    patch_sha256: str
    manifest_path: Path
    diff_path: Path
    patch_path: Path
    request_path: Path
    stats: DiffStats


def _git(repo: Path, args: Sequence[str], *, text: bool = False) -> bytes | str:
    command = [
        "git",
        "-c",
        "core.pager=cat",
        "-c",
        "pager.diff=false",
        "-c",
        "diff.external=",
        "-c",
        "diff.trustExitCode=false",
        "-c",
        "core.fsmonitor=false",
        "-C",
        str(repo),
        *args,
    ]
    completed = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
    )
    if completed.returncode != 0:
        stderr = (
            completed.stderr if text else completed.stderr.decode("utf-8", "replace")
        )
        raise GovernanceError(
            f"git command failed ({completed.returncode}): {stderr.strip()}"
        )
    return completed.stdout


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _resolve_sha(repo: Path, revision: str) -> str:
    output = _git(repo, ["rev-parse", "--verify", f"{revision}^{{commit}}"], text=True)
    return str(output).strip()


def _decode_git_path(raw: bytes) -> str:
    return raw.decode("utf-8", "surrogateescape")


def _parse_numstat_z(raw: bytes) -> tuple[int, int, tuple[str, ...]]:
    """Parse Git's NUL-delimited numstat stream, including rename records.

    A normal record is ``additions<TAB>deletions<TAB>path<NUL>``. For a
    rename/copy Git emits an empty path in that record followed by the old and
    new paths as two additional NUL-delimited fields. Filenames may contain
    tabs, so the first record is split at most twice.
    """

    additions = 0
    deletions = 0
    binary: list[str] = []
    if not raw:
        return additions, deletions, tuple(binary)

    fields = raw.split(b"\0")
    if fields[-1] != b"":
        raise GovernanceError("git numstat stream is not NUL terminated")

    records = iter(fields[:-1])
    for entry in records:
        if not entry:
            raise GovernanceError("git numstat produced an empty record")
        parts = entry.split(b"\t", 2)
        if len(parts) != 3:
            raise GovernanceError("git numstat produced a malformed record")
        added_raw, deleted_raw, path_raw = parts
        if path_raw:
            path = _decode_git_path(path_raw)
        else:
            try:
                old_path_raw = next(records)
                new_path_raw = next(records)
            except StopIteration as exc:
                raise GovernanceError(
                    "git numstat rename record is incomplete"
                ) from exc
            if not old_path_raw or not new_path_raw:
                raise GovernanceError("git numstat rename paths must not be empty")
            path = _decode_git_path(new_path_raw)
        if not path:
            raise GovernanceError("git numstat path must not be empty")
        if (added_raw == b"-") != (deleted_raw == b"-"):
            raise GovernanceError("git numstat contains a partial binary marker")
        if added_raw == b"-":
            binary.append(path)
            continue
        try:
            added = int(added_raw)
            deleted = int(deleted_raw)
        except ValueError as exc:
            raise GovernanceError("git numstat contains non-numeric totals") from exc
        if added < 0 or deleted < 0:
            raise GovernanceError("git numstat contains negative totals")
        additions += added
        deletions += deleted
    return additions, deletions, tuple(binary)


def _diff_stats(repo: Path, base_sha: str, head_sha: str) -> DiffStats:
    range_spec = f"{base_sha}...{head_sha}"
    names_raw = _git(repo, ["diff", "--name-only", "-z", "--find-renames", range_spec])
    assert isinstance(names_raw, bytes)
    names = tuple(_decode_git_path(part) for part in names_raw.split(b"\0") if part)
    attention_names_raw = _git(
        repo, ["diff", "--name-only", "-z", "--no-renames", range_spec]
    )
    assert isinstance(attention_names_raw, bytes)
    attention_names = tuple(
        dict.fromkeys(
            _decode_git_path(part)
            for part in attention_names_raw.split(b"\0")
            if part
        )
    )

    numstat_raw = _git(repo, ["diff", "--numstat", "-z", "--find-renames", range_spec])
    assert isinstance(numstat_raw, bytes)
    additions, deletions, binary_files = _parse_numstat_z(numstat_raw)
    return DiffStats(
        names,
        additions,
        deletions,
        binary_files,
        binary_files,
        attention_names,
    )


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "unknown"


def _canonical_json(data: Any) -> bytes:
    return (
        json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def parse_risk_class(pr_body: str) -> str | None:
    matches = [match.upper() for match in RISK_RE.findall(pr_body or "")]
    if len(matches) != 1:
        return None
    return matches[0]


def _is_safe_opaque_asset(path: str) -> bool:
    lowered = path.lower()
    return (
        lowered.startswith(SAFE_OPAQUE_PREFIXES)
        and Path(lowered).suffix in SAFE_OPAQUE_SUFFIXES
    )


def _partition_opaque_files(
    paths: Iterable[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    reviewable: list[str] = []
    blocking: list[str] = []
    for path in paths:
        (reviewable if _is_safe_opaque_asset(path) else blocking).append(path)
    return tuple(reviewable), tuple(blocking)


def minimum_risk_for_paths(paths: Iterable[str]) -> str:
    minimum = "R0"
    for raw_path in paths:
        path = raw_path.lower()
        if (
            path.startswith(".github/workflows/")
            or path
            in {
                ".github/grabowski-required-checks.json",
                ".github/review-evidence-authorities.json",
                ".github/pull_request_template.md",
                "scripts/quality/review_governance.py",
                "docs/process/merge-quality-gate.md",
            }
            or path.startswith("infra/")
            or path.startswith("scripts/ops/")
            or "/migrations/" in f"/{path}"
            or any(
                token in path
                for token in ("auth", "security", "privacy", "session", "passkey")
            )
            or path.startswith("docs/deploy/")
            or path.startswith("docs/runbooks/")
        ):
            return "R3"
        if path.endswith(".md"):
            continue
        if _is_safe_opaque_asset(path):
            minimum = max((minimum, "R1"), key=RISK_ORDER.__getitem__)
            continue
        if (
            path.startswith("apps/api/")
            or path.startswith("apps/web/")
            or path.startswith("src/")
            or path.startswith("scripts/")
            or path.startswith("tests/")
            or path.endswith(("cargo.lock", "pnpm-lock.yaml", "package-lock.json"))
            or path.endswith(
                (".rs", ".py", ".ts", ".tsx", ".js", ".mjs", ".svelte", ".sql", ".sh")
            )
        ):
            minimum = "R2"
        else:
            minimum = max((minimum, "R1"), key=RISK_ORDER.__getitem__)
    return minimum


def _r0_scope_valid(stats: DiffStats) -> tuple[bool, str]:
    if stats.changed_lines > 50:
        return (
            False,
            f"R0 permits at most 50 changed lines, found {stats.changed_lines}",
        )
    unsafe = [path for path in stats.changed_files if not path.endswith(".md")]
    if unsafe:
        return False, f"R0 permits Markdown-only changes, found: {', '.join(unsafe)}"
    return True, ""


def _validate_binding(
    *, pr_number: int, base_sha: str, head_sha: str, merge_base_sha: str
) -> None:
    if pr_number < 1:
        raise GovernanceError("PR number must be a positive integer")
    for name, value in (
        ("base_sha", base_sha),
        ("head_sha", head_sha),
        ("merge_base_sha", merge_base_sha),
    ):
        if not SHA40_RE.fullmatch(value):
            raise GovernanceError(f"{name} must be a lowercase 40-character Git SHA")


def _validate_stats(stats: DiffStats) -> None:
    if stats.additions < 0 or stats.deletions < 0:
        raise GovernanceError("diff totals must be non-negative")
    if len(set(stats.changed_files)) != len(stats.changed_files):
        raise GovernanceError("changed_files must not contain duplicates")
    if len(set(stats.binary_files)) != len(stats.binary_files):
        raise GovernanceError("binary_files must not contain duplicates")
    if len(set(stats.opaque_files)) != len(stats.opaque_files):
        raise GovernanceError("opaque_files must not contain duplicates")
    if len(set(stats.attention_files)) != len(stats.attention_files):
        raise GovernanceError("attention_changed_files must not contain duplicates")
    if not set(stats.binary_files).issubset(stats.changed_files):
        raise GovernanceError("binary_files must be a subset of changed_files")
    if not set(stats.opaque_files).issubset(stats.changed_files):
        raise GovernanceError("opaque_files must be a subset of changed_files")
    for field_name, paths in (
        ("changed_files", stats.changed_files),
        ("attention_changed_files", stats.attention_files),
    ):
        for file_path in paths:
            if (
                not isinstance(file_path, str)
                or not file_path
                or file_path != file_path.strip()
                or file_path.startswith("/")
                or any(part in {"", ".", ".."} for part in file_path.split("/"))
                or any(unicodedata.category(char).startswith("C") for char in file_path)
            ):
                raise GovernanceError(f"{field_name} contains an invalid path")


def _build_bundle(
    *,
    output_dir: Path,
    pr_number: int,
    base_sha: str,
    head_sha: str,
    merge_base_sha: str,
    risk_class: str | None,
    diff_bytes: bytes,
    patch_bytes: bytes,
    stats: DiffStats,
    source: str,
) -> Bundle:
    _validate_binding(
        pr_number=pr_number,
        base_sha=base_sha,
        head_sha=head_sha,
        merge_base_sha=merge_base_sha,
    )
    _validate_stats(stats)
    if not diff_bytes:
        raise GovernanceError("review diff is empty")
    if not patch_bytes:
        raise GovernanceError("review patch is empty")
    if len(diff_bytes) > MAX_ARTIFACT_BYTES or len(patch_bytes) > MAX_ARTIFACT_BYTES:
        raise GovernanceError(
            f"review artifacts exceed the {MAX_ARTIFACT_BYTES}-byte safety limit"
        )
    # Git diff records are LF-framed. bytes.splitlines() also treats a bare CR
    # inside textual hunk content as a boundary and could fabricate a diff header.
    diff_file_count = sum(
        1 for line in diff_bytes.split(b"\n") if line.startswith(b"diff --git ")
    )
    if diff_file_count != len(stats.changed_files):
        raise GovernanceError(
            "review diff file count does not match materialized changed-file list: "
            f"diff={diff_file_count}, metadata={len(stats.changed_files)}"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    diff_hash = _sha256(diff_bytes)
    patch_hash = _sha256(patch_bytes)
    stem = f"weltgewebe-pr-{pr_number}-{_safe_name(head_sha[:12])}-{diff_hash[:12]}"
    diff_path = output_dir / f"{stem}.diff"
    patch_path = output_dir / f"{stem}.patch"
    manifest_path = output_dir / f"{stem}.review.json"
    request_path = output_dir / f"{stem}.review-request.md"
    diff_path.write_bytes(diff_bytes)
    patch_path.write_bytes(patch_bytes)
    reviewable_opaque, blocking_opaque = _partition_opaque_files(stats.opaque_files)

    binding = {
        "pr_number": pr_number,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "merge_base_sha": merge_base_sha,
        "diff_sha256": diff_hash,
        "risk_class": risk_class,
    }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "kind": "weltgewebe-review-bundle",
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": source,
        "binding": binding,
        "stats": {
            "changed_files": list(stats.changed_files),
            "attention_changed_files": list(stats.attention_files),
            "changed_file_count": len(stats.changed_files),
            "additions": stats.additions,
            "deletions": stats.deletions,
            "binary_files": list(stats.binary_files),
            "opaque_files": list(stats.opaque_files),
            "reviewable_opaque_files": list(reviewable_opaque),
            "blocking_opaque_files": list(blocking_opaque),
        },
        "artifacts": {
            "diff": {
                "filename": diff_path.name,
                "sha256": diff_hash,
                "bytes": len(diff_bytes),
            },
            "patch": {
                "filename": patch_path.name,
                "sha256": patch_hash,
                "bytes": len(patch_bytes),
            },
        },
    }
    manifest_path.write_bytes(_canonical_json(manifest))

    opaque_note = ""
    if reviewable_opaque:
        opaque_note += (
            "\n- Visuell im GitHub-Dateidiff zu prüfende Raster-Assets: "
            + ", ".join(f"`{item}`" for item in reviewable_opaque)
            + "\n"
        )
    if blocking_opaque:
        opaque_note += (
            "\n- Nicht zuverlässig prüfbare und daher blockierende Dateien: "
            + ", ".join(f"`{item}`" for item in blocking_opaque)
            + "\n"
        )
    request = f"""# Weltgewebe – exakter Reviewauftrag

Prüfe ausschließlich den beigefügten Diff. Frühere oder spätere Versionen zählen nicht.

- PR: #{pr_number}
- Risikoklasse: {risk_class or "FEHLT"}
- Basis-Commit: `{base_sha}`
- Head-Commit: `{head_sha}`
- Merge-Basis: `{merge_base_sha}`
- Diff-SHA-256: `{diff_hash}`
- Quelle: `{source}`
- Geänderte Dateien: {len(stats.changed_files)}
- Zeilen: +{stats.additions} / -{stats.deletions}
{opaque_note}
Bewerte mindestens eine klar benannte Achse, etwa `correctness`, `security`,
`data-integrity`, `concurrency`, `architecture`, `testing` oder `operations`.
Benenne konkrete Befunde. Ein PASS ist nur zulässig, wenn alle Mergeblocker behoben
oder nachvollziehbar widerlegt sind. Sichere Raster-Assets in den dokumentierten Doku-/Web-Assetpfaden müssen
im GitHub-Dateidiff visuell geprüft werden. Andere Dateien ohne Textdarstellung
bleiben offene Mergeblocker.

Nach dem Review wird der vollständige Reviewtext als normaler Kommentartext
vor dem Marker hinterlegt. `report_sha256` ist der SHA-256 des getrimmten, auf LF-Zeilenumbrüche
normalisierten UTF-8-Reviewtexts vor dem Marker. Danach folgt genau ein Belegblock:

```text
<VOLLSTÄNDIGER REVIEWTEXT>
<!-- weltgewebe-review-evidence
{{
  "schema_version": 1,
  "pr_number": {pr_number},
  "base_sha": "{base_sha}",
  "head_sha": "{head_sha}",
  "diff_sha256": "{diff_hash}",
  "risk_class": "{risk_class or "R?"}",
  "reviewer": "NAME DES PRÜFERS",
  "report_sha256": "SHA-256 DES REVIEWTEXTS VOR DEM MARKER",
  "review_axis": "correctness",
  "verdict": "PASS",
  "findings_resolved": true
}}
-->
```

Der Kommentar muss von einem freigegebenen GitHub-Attestierer stammen. Leere,
zu kurze, mehrfach markierte oder nicht zum Berichtshash passende Kommentare
zählen nicht. Jeder neue Push, Basiswechsel oder Diffwechsel entwertet den Beleg
automatisch.

Ist nach einem dokumentierten Versuch kein Fremdreview verfügbar, darf dieselbe
Prüferidentität zwei getrennte Pässe attestieren. Der erste ergänzt
`"review_mode": "self-primary"` und eine eindeutige `review_session`; der zweite
verwendet `"review_mode": "self-fallback"`, eine andere `review_session` und einen
konkreten `fallback_reason`. Reviewachsen und Berichtshashes müssen verschieden
bleiben; eine Unabhängigkeit der beiden Self-Reviews wird nicht behauptet.
"""
    request_path.write_text(request, encoding="utf-8")
    return Bundle(
        pr_number=pr_number,
        base_sha=base_sha,
        head_sha=head_sha,
        merge_base_sha=merge_base_sha,
        diff_sha256=diff_hash,
        patch_sha256=patch_hash,
        manifest_path=manifest_path,
        diff_path=diff_path,
        patch_path=patch_path,
        request_path=request_path,
        stats=stats,
    )


def generate_bundle(
    *,
    repo: Path,
    output_dir: Path,
    base_revision: str,
    head_revision: str,
    pr_number: int,
    risk_class: str | None,
) -> Bundle:
    repo = repo.resolve()
    base_sha = _resolve_sha(repo, base_revision)
    head_sha = _resolve_sha(repo, head_revision)
    merge_base = str(_git(repo, ["merge-base", base_sha, head_sha], text=True)).strip()
    range_spec = f"{base_sha}...{head_sha}"
    diff_bytes = _git(
        repo,
        [
            "diff",
            "--binary",
            "--full-index",
            "--find-renames",
            "--no-ext-diff",
            range_spec,
        ],
    )
    patch_bytes = _git(
        repo,
        [
            "format-patch",
            "--stdout",
            "--binary",
            "--full-index",
            "--no-signature",
            f"{merge_base}..{head_sha}",
        ],
    )
    assert isinstance(diff_bytes, bytes)
    assert isinstance(patch_bytes, bytes)
    return _build_bundle(
        output_dir=output_dir,
        pr_number=pr_number,
        base_sha=base_sha,
        head_sha=head_sha,
        merge_base_sha=merge_base,
        risk_class=risk_class,
        diff_bytes=diff_bytes,
        patch_bytes=patch_bytes,
        stats=_diff_stats(repo, base_sha, head_sha),
        source="local-git-binary",
    )


def _materialized_metadata(path: Path) -> tuple[int, str, str, str, DiffStats]:
    raw = _load_json(path)
    if not isinstance(raw, dict):
        raise GovernanceError("materialized metadata must be a JSON object")
    required = {
        "schema_version",
        "pr_number",
        "base_sha",
        "head_sha",
        "merge_base_sha",
        "changed_file_count",
        "changed_files",
        "additions",
        "deletions",
        "opaque_files",
    }
    optional = {"attention_changed_files"}
    if not required.issubset(raw) or not set(raw).issubset(required | optional):
        missing = sorted(required - set(raw))
        extra = sorted(set(raw) - required - optional)
        raise GovernanceError(
            f"materialized metadata keys mismatch; missing={missing}, extra={extra}"
        )
    schema_version = raw["schema_version"]
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != SCHEMA_VERSION
    ):
        raise GovernanceError("unsupported materialized metadata schema")
    pr_number = raw["pr_number"]
    additions = raw["additions"]
    deletions = raw["deletions"]
    changed_file_count = raw["changed_file_count"]
    changed_files = raw["changed_files"]
    attention_changed_files = raw.get("attention_changed_files", changed_files)
    opaque_files = raw["opaque_files"]
    if not isinstance(pr_number, int) or isinstance(pr_number, bool):
        raise GovernanceError("materialized pr_number must be an integer")
    for name, value in (
        ("changed_file_count", changed_file_count),
        ("additions", additions),
        ("deletions", deletions),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise GovernanceError(f"materialized {name} must be a non-negative integer")
    if not isinstance(changed_files, list) or not all(
        isinstance(item, str) for item in changed_files
    ):
        raise GovernanceError("materialized changed_files must be a string list")
    if not isinstance(attention_changed_files, list) or not all(
        isinstance(item, str) for item in attention_changed_files
    ):
        raise GovernanceError(
            "materialized attention_changed_files must be a string list"
        )
    if not isinstance(opaque_files, list) or not all(
        isinstance(item, str) for item in opaque_files
    ):
        raise GovernanceError("materialized opaque_files must be a string list")
    if changed_file_count != len(changed_files):
        raise GovernanceError(
            "materialized changed_file_count does not match file list"
        )
    stats = DiffStats(
        tuple(changed_files),
        additions,
        deletions,
        (),
        tuple(opaque_files),
        tuple(attention_changed_files),
    )
    base_sha = raw["base_sha"]
    head_sha = raw["head_sha"]
    merge_base_sha = raw["merge_base_sha"]
    if not all(
        isinstance(value, str) for value in (base_sha, head_sha, merge_base_sha)
    ):
        raise GovernanceError("materialized Git SHAs must be strings")
    _validate_binding(
        pr_number=pr_number,
        base_sha=base_sha,
        head_sha=head_sha,
        merge_base_sha=merge_base_sha,
    )
    _validate_stats(stats)
    return pr_number, base_sha, head_sha, merge_base_sha, stats


def _materialized_diff_has_text_hunk(diff_bytes: bytes, path: str) -> bool:
    """Return true only when the exact GitHub diff positively exposes text for path.

    The Files API may omit its per-file patch field for both binary files and
    oversized textual diffs. Missing patch therefore remains an opaque
    candidate. We clear that candidate only when the separately downloaded,
    hash-bound full PR diff contains an ordinary textual hunk for the exact path.

    Git-quoted, renamed, or otherwise ambiguous paths deliberately fail closed.
    """

    try:
        encoded = path.encode("utf-8", "strict")
    except UnicodeEncodeError:
        return False
    if any(byte in encoded for byte in (0, 10, 13)):
        return False

    header = b"diff --git a/" + encoded + b" b/" + encoded + b"\n"
    if diff_bytes.startswith(header):
        start = 0
    else:
        anchored_header = b"\n" + header
        anchored_start = diff_bytes.find(anchored_header)
        if anchored_start < 0:
            return False
        start = anchored_start + 1
    next_start = diff_bytes.find(b"\ndiff --git ", start + len(header))
    end = len(diff_bytes) if next_start < 0 else next_start
    section = diff_bytes[start:end]
    if b"\nGIT binary patch\n" in section or b"\nBinary files " in section:
        return False
    return b"\n@@ " in section


def _resolve_materialized_opaque_files(
    stats: DiffStats, diff_bytes: bytes
) -> DiffStats:
    unresolved = tuple(
        path
        for path in stats.opaque_files
        if not _materialized_diff_has_text_hunk(diff_bytes, path)
    )
    return DiffStats(
        stats.changed_files,
        stats.additions,
        stats.deletions,
        stats.binary_files,
        unresolved,
        stats.attention_changed_files,
    )


def generate_materialized_bundle(
    *,
    output_dir: Path,
    metadata_file: Path,
    diff_file: Path,
    patch_file: Path,
    risk_class: str | None,
) -> Bundle:
    pr_number, base_sha, head_sha, merge_base_sha, stats = _materialized_metadata(
        metadata_file
    )
    diff_bytes = diff_file.read_bytes()
    stats = _resolve_materialized_opaque_files(stats, diff_bytes)
    return _build_bundle(
        output_dir=output_dir,
        pr_number=pr_number,
        base_sha=base_sha,
        head_sha=head_sha,
        merge_base_sha=merge_base_sha,
        risk_class=risk_class,
        diff_bytes=diff_bytes,
        patch_bytes=patch_file.read_bytes(),
        stats=stats,
        source="github-pull-api",
    )


def _flatten_comments(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise GovernanceError("comments JSON must be a list")
    flattened: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, list):
            flattened.extend(entry for entry in item if isinstance(entry, dict))
        elif isinstance(item, dict):
            flattened.append(item)
    return flattened


def _flatten_reviews(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise GovernanceError("reviews JSON must be a list")
    flattened: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, list):
            flattened.extend(entry for entry in item if isinstance(entry, dict))
        elif isinstance(item, dict):
            flattened.append(item)
    return flattened


def _normalized_report_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def _github_timestamp(value: Any) -> dt.datetime:
    if not isinstance(value, str) or not value:
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _numeric_id(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _decode_evidence_payload(
    body: str, payload_start: int
) -> tuple[dict[str, Any] | None, str | None]:
    """Decode one JSON object and require an adjacent comment terminator.

    ``JSONDecoder.raw_decode`` tracks the JSON boundary correctly even when a
    JSON string itself contains ``-->``. A first-substring search cannot do so.
    """

    source = body[payload_start:].lstrip()
    try:
        record, consumed = json.JSONDecoder().raw_decode(source)
    except json.JSONDecodeError:
        return None, "malformed"
    payload_text = source[:consumed]
    if len(payload_text.encode("utf-8")) > MAX_EVIDENCE_PAYLOAD_BYTES:
        return None, "oversized"
    remainder = source[consumed:].lstrip()
    if not remainder.startswith(("-->", "--!>")):
        return None, "malformed"
    if not isinstance(record, dict):
        return None, "malformed"
    return record, None


def _evidence_blocks(
    comments: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, int]:
    evidence: list[dict[str, Any]] = []
    parse_failures = 0
    oversized_payloads = 0
    for comment_index, comment in enumerate(comments):
        body = comment.get("body") or ""
        if not isinstance(body, str):
            continue
        starts = list(EVIDENCE_START_RE.finditer(body))
        for block_index, match in enumerate(starts):
            record, failure = _decode_evidence_payload(body, match.end())
            if failure is not None:
                parse_failures += 1
                if failure == "oversized":
                    oversized_payloads += 1
                continue
            assert record is not None
            report_text = _normalized_report_text(body[: match.start()])
            report_bytes = report_text.encode("utf-8")
            record = dict(record)
            record["_payload_keys"] = tuple(record.keys())
            record["_comment_index"] = comment_index
            record["_block_index"] = block_index
            record["_comment_evidence_block_count"] = len(starts)
            record["_report_text_bytes"] = len(report_bytes)
            record["_report_text_sha256"] = _sha256(report_bytes)
            record["_author_association"] = str(
                comment.get("author_association") or ""
            ).upper()
            user = comment.get("user") or {}
            record["_comment_author"] = (
                user.get("login") if isinstance(user, dict) else None
            )
            record["_comment_url"] = comment.get("html_url")
            record["_comment_id"] = _numeric_id(comment.get("id"))
            record["_updated_at"] = (
                comment.get("updated_at") or comment.get("created_at") or ""
            )
            evidence.append(record)
    return evidence, parse_failures, oversized_payloads


def _forwarded_evidence_blocks(
    comments: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, int]:
    evidence: list[dict[str, Any]] = []
    parse_failures = 0
    oversized_payloads = 0
    for comment_index, comment in enumerate(comments):
        body = comment.get("body") or ""
        if not isinstance(body, str):
            continue
        starts = list(FORWARDED_EVIDENCE_START_RE.finditer(body))
        for block_index, match in enumerate(starts):
            record, failure = _decode_evidence_payload(body, match.end())
            if failure is not None:
                parse_failures += 1
                if failure == "oversized":
                    oversized_payloads += 1
                continue
            assert record is not None
            report_text = _normalized_report_text(body[: match.start()])
            report_bytes = report_text.encode("utf-8")
            record = dict(record)
            record["_payload_keys"] = tuple(record.keys())
            record["_comment_index"] = comment_index
            record["_block_index"] = block_index
            record["_comment_evidence_block_count"] = len(starts)
            record["_report_text_bytes"] = len(report_bytes)
            record["_report_text_sha256"] = _sha256(report_bytes)
            record["_author_association"] = str(comment.get("author_association") or "").upper()
            user = comment.get("user") or {}
            record["_comment_author"] = user.get("login") if isinstance(user, dict) else None
            record["_comment_url"] = comment.get("html_url")
            record["_comment_id"] = _numeric_id(comment.get("id"))
            record["_updated_at"] = comment.get("updated_at") or comment.get("created_at") or ""
            evidence.append(record)
    return evidence, parse_failures, oversized_payloads


def _native_review_evidence(
    *,
    reviews: Sequence[dict[str, Any]],
    head_sha: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int, int]:
    latest: dict[str, dict[str, Any]] = {}
    unauthorized = 0
    malformed = 0
    for index, review in enumerate(reviews):
        state = str(review.get("state") or "").upper()
        if state not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            continue
        user = review.get("user") or {}
        login = user.get("login") if isinstance(user, dict) else None
        association = str(review.get("author_association") or "").upper()
        if not isinstance(login, str) or not GITHUB_LOGIN_RE.fullmatch(login):
            malformed += 1
            continue
        if association not in AUTHORIZED_ASSOCIATIONS:
            unauthorized += 1
            continue
        commit_id = review.get("commit_id")
        if not isinstance(commit_id, str) or not SHA40_RE.fullmatch(commit_id):
            malformed += 1
            continue
        ordering = (
            _github_timestamp(review.get("submitted_at")),
            _numeric_id(review.get("id")),
            index,
        )
        key = login.casefold()
        previous = latest.get(key)
        if previous is None or ordering >= previous["_ordering"]:
            latest[key] = {
                "reviewer": login,
                "attester": login,
                "state": state,
                "commit_id": commit_id,
                "review_axis": "github-approval",
                "review_url": review.get("html_url"),
                "_ordering": ordering,
            }

    accepted: list[dict[str, Any]] = []
    blocking: list[dict[str, Any]] = []
    stale = 0
    for record in latest.values():
        if record["commit_id"] != head_sha:
            stale += 1
            continue
        if record["state"] == "APPROVED":
            accepted.append(record)
        elif record["state"] == "CHANGES_REQUESTED":
            blocking.append(record)
    return accepted, blocking, stale, unauthorized, malformed


def evaluate_evidence(
    *,
    bundle: Bundle,
    risk_class: str | None,
    comments: Sequence[dict[str, Any]],
    allowed_attesters: frozenset[str],
    reviews: Sequence[dict[str, Any]] = (),
    pr_body: str | None = None,
    canonical_product_docs: frozenset[str] | None = None,
) -> dict[str, Any]:
    if not allowed_attesters or any(
        not isinstance(login, str) or not GITHUB_LOGIN_RE.fullmatch(login)
        for login in allowed_attesters
    ):
        raise GovernanceError(
            "allowed_attesters must be a non-empty set of GitHub logins"
        )
    normalized_attesters = frozenset(login.casefold() for login in allowed_attesters)
    reasons: list[str] = []
    attention_impact_required = False
    attention_impact_value = None
    if pr_body is not None:
        attention_impact_required = bool(product_logic_changes(bundle.stats.attention_files))
        attention_impact_value = attention_impact_decision(pr_body)
        if attention_impact_required and canonical_product_docs is None:
            reasons.append(
                "Attention impact: trusted canonical product docs are unavailable"
            )
        else:
            reasons.extend(
                f"Attention impact: {reason}"
                for reason in validate_attention_impact_contract_binding(
                    pr_body=pr_body,
                    changed_files=bundle.stats.attention_files,
                    canonical_product_docs=canonical_product_docs or (),
                )
            )
    if risk_class is None:
        reasons.append(
            "PR body must contain exactly one risk marker: <!-- weltgewebe-risk: R0|R1|R2|R3 -->"
        )
        risk_class = "R3"

    reviewable_opaque, blocking_opaque = _partition_opaque_files(
        bundle.stats.opaque_files
    )
    if blocking_opaque:
        reasons.append(
            "opaque files outside the reviewable raster-asset policy block the review evidence gate: "
            + ", ".join(blocking_opaque)
        )

    minimum_risk = minimum_risk_for_paths(bundle.stats.changed_files)
    if RISK_ORDER[risk_class] < RISK_ORDER[minimum_risk]:
        reasons.append(
            f"declared risk {risk_class} is below path-derived minimum {minimum_risk}"
        )
    if risk_class == "R0":
        valid, detail = _r0_scope_valid(bundle.stats)
        if not valid:
            reasons.append(detail)

    records, evidence_parse_failures, oversized_evidence = _evidence_blocks(comments)
    forwarded_records, forwarded_parse_failures, forwarded_oversized = _forwarded_evidence_blocks(comments)
    total_blocks_by_comment = {
        index: len(EVIDENCE_START_RE.findall(str(comment.get("body") or "")))
        + len(FORWARDED_EVIDENCE_START_RE.findall(str(comment.get("body") or "")))
        for index, comment in enumerate(comments)
    }
    for record in [*records, *forwarded_records]:
        record["_comment_evidence_block_count"] = total_blocks_by_comment.get(
            int(record.get("_comment_index", -1)), 0
        )
    exact: list[dict[str, Any]] = []
    stale = 0
    unauthorized = 0
    malformed = evidence_parse_failures + forwarded_parse_failures
    oversized_evidence += forwarded_oversized
    for record in records:
        comment_author = str(record.get("_comment_author") or "").strip()
        if (
            record.get("_author_association") not in AUTHORIZED_ASSOCIATIONS
            or comment_author.casefold() not in normalized_attesters
        ):
            unauthorized += 1
            continue
        required_fields = {
            "schema_version",
            "pr_number",
            "base_sha",
            "head_sha",
            "diff_sha256",
            "risk_class",
            "reviewer",
            "report_sha256",
            "review_axis",
            "verdict",
            "findings_resolved",
        }
        optional_fields = {"review_mode", "review_session", "fallback_reason"}
        payload_keys = record.get("_payload_keys", ())
        payload_key_set = set(payload_keys) if isinstance(payload_keys, tuple) else set()
        if (
            not isinstance(payload_keys, tuple)
            or any(str(key).startswith("_") for key in payload_keys)
            or not required_fields.issubset(payload_key_set)
            or not payload_key_set.issubset(required_fields | optional_fields)
        ):
            malformed += 1
            continue
        schema_version = record.get("schema_version")
        pr_number = record.get("pr_number")
        if (
            not isinstance(schema_version, int)
            or isinstance(schema_version, bool)
            or not isinstance(pr_number, int)
            or isinstance(pr_number, bool)
        ):
            malformed += 1
            continue
        if (
            schema_version != SCHEMA_VERSION
            or pr_number != bundle.pr_number
            or record.get("base_sha") != bundle.base_sha
            or record.get("head_sha") != bundle.head_sha
            or record.get("diff_sha256") != bundle.diff_sha256
            or str(record.get("risk_class", "")).upper() != risk_class
        ):
            stale += 1
            continue
        reviewer_value = record.get("reviewer")
        reviewer_raw = reviewer_value if isinstance(reviewer_value, str) else ""
        reviewer = unicodedata.normalize("NFC", reviewer_raw.strip())
        report_sha256 = str(record.get("report_sha256") or "").strip()
        axis = str(record.get("review_axis") or "").strip().lower()
        verdict = str(record.get("verdict") or "").strip().upper()
        review_mode_present = "review_mode" in payload_key_set
        review_session_present = "review_session" in payload_key_set
        fallback_reason_present = "fallback_reason" in payload_key_set
        review_mode_raw = record.get("review_mode", "external")
        review_mode = (
            review_mode_raw.strip().lower()
            if isinstance(review_mode_raw, str)
            else ""
        )
        review_session_raw = record.get("review_session", "")
        review_session = (
            review_session_raw.strip()
            if isinstance(review_session_raw, str)
            else ""
        )
        fallback_reason_raw = record.get("fallback_reason", "")
        fallback_reason = (
            unicodedata.normalize("NFC", fallback_reason_raw.strip())
            if isinstance(fallback_reason_raw, str)
            else ""
        )
        self_review_fields_valid = True
        if review_mode == "external":
            self_review_fields_valid = (
                (not review_mode_present or review_mode_raw == review_mode_raw.strip())
                and not review_session_present
                and not fallback_reason_present
            )
        elif review_mode in {"self-primary", "self-fallback"}:
            self_review_fields_valid = bool(
                review_mode_present
                and isinstance(review_mode_raw, str)
                and review_mode_raw == review_mode_raw.strip()
                and review_session_present
                and isinstance(review_session_raw, str)
                and review_session_raw == review_session_raw.strip()
                and REVIEW_SESSION_RE.fullmatch(review_session)
            )
            if review_mode == "self-primary":
                self_review_fields_valid = (
                    self_review_fields_valid and not fallback_reason_present
                )
            else:
                fallback_bytes = len(fallback_reason.encode("utf-8"))
                self_review_fields_valid = (
                    self_review_fields_valid
                    and fallback_reason_present
                    and isinstance(fallback_reason_raw, str)
                    and fallback_reason_raw == fallback_reason_raw.strip()
                    and 20 <= fallback_bytes <= MAX_FALLBACK_REASON_BYTES
                    and not any(
                        unicodedata.category(char).startswith("C")
                        for char in fallback_reason
                    )
                )
        else:
            self_review_fields_valid = False
        if (
            not reviewer
            or reviewer_raw != reviewer_raw.strip()
            or len(reviewer) > 120
            or any(unicodedata.category(char).startswith("C") for char in reviewer)
            or not REPORT_SHA256_RE.fullmatch(report_sha256)
            or report_sha256 != record.get("_report_text_sha256")
            or record.get("_report_text_bytes", 0) < MIN_REVIEW_REPORT_BYTES
            or record.get("_comment_evidence_block_count") != 1
            or axis not in ALLOWED_AXES
            or verdict not in {"PASS", "BLOCKED", "FAIL"}
            or not isinstance(record.get("findings_resolved"), bool)
            or not self_review_fields_valid
        ):
            malformed += 1
            continue
        record["reviewer"] = reviewer
        record["report_sha256"] = report_sha256
        record["review_axis"] = axis
        record["verdict"] = verdict
        record["review_mode"] = review_mode
        record["review_session"] = review_session or None
        record["fallback_reason"] = fallback_reason or None
        exact.append(record)

    for record in forwarded_records:
        comment_author = str(record.get("_comment_author") or "").strip()
        if (
            record.get("_author_association") not in AUTHORIZED_ASSOCIATIONS
            or comment_author.casefold() not in normalized_attesters
        ):
            unauthorized += 1
            continue
        required_fields = {
            "schema_version",
            "base_sha",
            "head_sha",
            "diff_sha256",
            "reviewer",
            "review_axis",
            "verdict",
            "findings_resolved",
        }
        payload_keys = record.get("_payload_keys", ())
        if (
            not isinstance(payload_keys, tuple)
            or any(str(key).startswith("_") for key in payload_keys)
            or set(payload_keys) != required_fields
        ):
            malformed += 1
            continue
        schema_version = record.get("schema_version")
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            malformed += 1
            continue
        if (
            schema_version != SCHEMA_VERSION
            or record.get("base_sha") != bundle.base_sha
            or record.get("head_sha") != bundle.head_sha
            or record.get("diff_sha256") != bundle.diff_sha256
        ):
            stale += 1
            continue
        reviewer_value = record.get("reviewer")
        reviewer_raw = reviewer_value if isinstance(reviewer_value, str) else ""
        reviewer = unicodedata.normalize("NFC", reviewer_raw.strip())
        axis = str(record.get("review_axis") or "").strip().lower()
        verdict = str(record.get("verdict") or "").strip().upper()
        if (
            not reviewer
            or reviewer_raw != reviewer_raw.strip()
            or len(reviewer) > 120
            or any(unicodedata.category(char).startswith("C") for char in reviewer)
            or record.get("_report_text_bytes", 0) < MIN_REVIEW_REPORT_BYTES
            or record.get("_comment_evidence_block_count") != 1
            or axis not in ALLOWED_AXES
            or verdict not in {"PASS", "BLOCKED", "FAIL"}
            or not isinstance(record.get("findings_resolved"), bool)
        ):
            malformed += 1
            continue
        record["reviewer"] = reviewer
        record["report_sha256"] = record["_report_text_sha256"]
        record["review_axis"] = axis
        record["verdict"] = verdict
        record["_forwarded"] = True
        exact.append(record)

    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for record in exact:
        key = (record["reviewer"].casefold(), record["review_axis"])
        ordering = (
            _github_timestamp(record.get("_updated_at")),
            int(record.get("_comment_id", 0)),
            int(record.get("_comment_index", 0)),
            int(record.get("_block_index", 0)),
        )
        previous = latest.get(key)
        if previous is None:
            latest[key] = record
            continue
        previous_ordering = (
            _github_timestamp(previous.get("_updated_at")),
            int(previous.get("_comment_id", 0)),
            int(previous.get("_comment_index", 0)),
            int(previous.get("_block_index", 0)),
        )
        if ordering >= previous_ordering:
            latest[key] = record

    blocking = [record for record in latest.values() if record["verdict"] != "PASS"]
    if blocking:
        reasons.append(
            "current exact review evidence contains blocking verdicts: "
            + ", ".join(
                f"{record['reviewer']}/{record['review_axis']}={record['verdict']}"
                for record in blocking
            )
        )

    accepted = [
        record
        for record in latest.values()
        if record["verdict"] == "PASS" and record.get("findings_resolved") is True
    ]
    (
        native_accepted,
        native_blocking,
        stale_native,
        unauthorized_native,
        malformed_native,
    ) = _native_review_evidence(
        reviews=reviews,
        head_sha=bundle.head_sha,
    )
    if native_blocking:
        reasons.append(
            "current-head GitHub reviews request changes: "
            + ", ".join(record["reviewer"] for record in native_blocking)
        )

    attested_reviewer_ids = {
        unicodedata.normalize("NFC", record["reviewer"]).casefold()
        for record in accepted
    }
    quick_approvals = (
        [
            record
            for record in native_accepted
            if unicodedata.normalize("NFC", record["reviewer"]).casefold()
            not in attested_reviewer_ids
        ]
        if risk_class == "R1"
        else []
    )
    required = REQUIRED_REVIEWS[risk_class]
    accepted_count = len(accepted) + len(quick_approvals)
    if accepted_count < required:
        suffix = (
            " (native current-head APPROVED reviews count for R1)"
            if risk_class == "R1"
            else ""
        )
        reasons.append(
            f"risk {risk_class} requires {required} exact PASS reviews, found {accepted_count}{suffix}"
        )

    reviewer_names = {record["reviewer"].casefold() for record in accepted}
    report_hashes = {record["report_sha256"] for record in accepted}
    axes = {record["review_axis"] for record in accepted}
    if required >= 2:
        self_primary = [
            record for record in accepted if record.get("review_mode") == "self-primary"
        ]
        self_fallback = [
            record for record in accepted if record.get("review_mode") == "self-fallback"
        ]
        primary_sessions = {
            record.get("review_session")
            for record in self_primary
            if record.get("review_session")
        }
        fallback_sessions = {
            record.get("review_session")
            for record in self_fallback
            if record.get("review_session")
        }
        explicit_self_fallback = (
            len(reviewer_names) == 1
            and bool(primary_sessions)
            and bool(fallback_sessions)
            and primary_sessions.isdisjoint(fallback_sessions)
            and all(record.get("fallback_reason") for record in self_fallback)
        )
        if len(reviewer_names) < 2 and not explicit_self_fallback:
            reasons.append(
                "R2/R3 require at least two distinct reviewer identities or an "
                "explicit self-primary/self-fallback pair with distinct review sessions"
            )
        if len(report_hashes) < 2:
            reasons.append("R2/R3 require at least two distinct review reports")
        if len(axes) < 2:
            reasons.append("R2/R3 require at least two distinct review axes")
    if risk_class == "R3" and not (axes & HIGH_RISK_AXES):
        reasons.append(
            "R3 requires at least one security, privacy, data-integrity, concurrency, migration or operations review"
        )

    accepted_summary = [
        {
            "kind": (
                "forwarded-external-review"
                if record.get("_forwarded")
                else "attested-report"
            ),
            "reviewer": record["reviewer"],
            "report_sha256": record["report_sha256"],
            "review_axis": record["review_axis"],
            "review_mode": record.get("review_mode", "external"),
            "review_session": record.get("review_session"),
            "attester": record.get("_comment_author"),
            "comment_url": record.get("_comment_url"),
        }
        for record in sorted(
            accepted,
            key=lambda item: (item["review_axis"], item["reviewer"].casefold()),
        )
    ]
    accepted_summary.extend(
        {
            "kind": "github-approval",
            "reviewer": record["reviewer"],
            "report_sha256": None,
            "review_axis": "github-approval",
            "attester": record["attester"],
            "comment_url": record.get("review_url"),
        }
        for record in sorted(
            quick_approvals, key=lambda item: item["reviewer"].casefold()
        )
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "weltgewebe-review-evaluation",
        "pass": not reasons,
        "risk_class": risk_class,
        "minimum_risk_class": minimum_risk,
        "attention_impact_required": attention_impact_required,
        "attention_impact_decision": attention_impact_value,
        "binding": {
            "pr_number": bundle.pr_number,
            "base_sha": bundle.base_sha,
            "head_sha": bundle.head_sha,
            "merge_base_sha": bundle.merge_base_sha,
            "diff_sha256": bundle.diff_sha256,
        },
        "reviewable_opaque_files": list(reviewable_opaque),
        "blocking_opaque_files": list(blocking_opaque),
        "required_review_count": required,
        "accepted_review_count": accepted_count,
        "accepted_reviews": accepted_summary,
        "stale_evidence_count": stale,
        "unauthorized_evidence_count": unauthorized,
        "malformed_evidence_count": malformed,
        "oversized_evidence_count": oversized_evidence,
        "stale_native_review_count": stale_native,
        "unauthorized_native_review_count": unauthorized_native,
        "malformed_native_review_count": malformed_native,
        "reasons": reasons,
    }


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GovernanceError(f"cannot read JSON {path}: {exc}") from exc


def _write_failure(output_dir: Path, message: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": "weltgewebe-review-evaluation",
        "pass": False,
        "reasons": [message],
    }
    (output_dir / "evaluation.json").write_bytes(_canonical_json(payload))


def _command_bundle(args: argparse.Namespace) -> int:
    body = (
        Path(args.pr_body_file).read_text(encoding="utf-8") if args.pr_body_file else ""
    )
    risk_class = parse_risk_class(body)
    bundle = generate_bundle(
        repo=Path(args.repo),
        output_dir=Path(args.output_dir),
        base_revision=args.base_sha,
        head_revision=args.head_sha,
        pr_number=args.pr_number,
        risk_class=risk_class,
    )
    print(bundle.manifest_path)
    return 0


def _command_bundle_materialized(args: argparse.Namespace) -> int:
    body = (
        Path(args.pr_body_file).read_text(encoding="utf-8") if args.pr_body_file else ""
    )
    bundle = generate_materialized_bundle(
        output_dir=Path(args.output_dir),
        metadata_file=Path(args.metadata_file),
        diff_file=Path(args.diff_file),
        patch_file=Path(args.patch_file),
        risk_class=parse_risk_class(body),
    )
    print(bundle.manifest_path)
    return 0


def _evaluate_and_write(
    *,
    output_dir: Path,
    bundle: Bundle,
    risk_class: str | None,
    pr_body: str,
    comments_file: Path,
    reviews_file: Path | None,
    authorities_file: Path,
) -> int:
    raw_comments = _load_json(comments_file)
    comments = _flatten_comments(raw_comments)
    reviews = _flatten_reviews(_load_json(reviews_file)) if reviews_file else []
    trusted_manifest = (
        Path(__file__).resolve().parents[2] / "manifest" / "repo-index.yaml"
    ).read_text(encoding="utf-8")
    trusted_product_docs = frozenset(
        canonical_product_docs_from_manifest_text(trusted_manifest)
    )
    evaluation = evaluate_evidence(
        bundle=bundle,
        risk_class=risk_class,
        comments=comments,
        reviews=reviews,
        allowed_attesters=load_allowed_attesters(authorities_file),
        pr_body=pr_body,
        canonical_product_docs=trusted_product_docs,
    )
    (output_dir / "evaluation.json").write_bytes(_canonical_json(evaluation))
    print(json.dumps(evaluation, sort_keys=True))
    return 0 if evaluation["pass"] else 1


def _command_evaluate(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir)
    try:
        body = Path(args.pr_body_file).read_text(encoding="utf-8")
        risk_class = parse_risk_class(body)
        bundle = generate_bundle(
            repo=Path(args.repo),
            output_dir=output_dir,
            base_revision=args.base_sha,
            head_revision=args.head_sha,
            pr_number=args.pr_number,
            risk_class=risk_class,
        )
        return _evaluate_and_write(
            output_dir=output_dir,
            bundle=bundle,
            risk_class=risk_class,
            pr_body=body,
            comments_file=Path(args.comments_file),
            reviews_file=Path(args.reviews_file) if args.reviews_file else None,
            authorities_file=Path(args.authorities_file),
        )
    except Exception as exc:  # fail closed and preserve a machine-readable receipt
        _write_failure(output_dir, f"internal governance failure: {exc}")
        print(f"review governance failed closed: {exc}", file=sys.stderr)
        return 2


def _command_evaluate_materialized(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir)
    try:
        body = Path(args.pr_body_file).read_text(encoding="utf-8")
        risk_class = parse_risk_class(body)
        bundle = generate_materialized_bundle(
            output_dir=output_dir,
            metadata_file=Path(args.metadata_file),
            diff_file=Path(args.diff_file),
            patch_file=Path(args.patch_file),
            risk_class=risk_class,
        )
        return _evaluate_and_write(
            output_dir=output_dir,
            bundle=bundle,
            risk_class=risk_class,
            pr_body=body,
            comments_file=Path(args.comments_file),
            reviews_file=Path(args.reviews_file) if args.reviews_file else None,
            authorities_file=Path(args.authorities_file),
        )
    except Exception as exc:  # fail closed and preserve a machine-readable receipt
        _write_failure(output_dir, f"internal governance failure: {exc}")
        print(f"review governance failed closed: {exc}", file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    bundle = subparsers.add_parser("bundle")
    bundle.add_argument("--repo", required=True)
    bundle.add_argument("--output-dir", required=True)
    bundle.add_argument("--base-sha", required=True)
    bundle.add_argument("--head-sha", required=True)
    bundle.add_argument("--pr-number", required=True, type=int)
    bundle.add_argument("--pr-body-file")

    evaluate = subparsers.add_parser("evaluate")
    evaluate.add_argument("--repo", required=True)
    evaluate.add_argument("--output-dir", required=True)
    evaluate.add_argument("--base-sha", required=True)
    evaluate.add_argument("--head-sha", required=True)
    evaluate.add_argument("--pr-number", required=True, type=int)
    evaluate.add_argument("--pr-body-file", required=True)
    evaluate.add_argument("--comments-file", required=True)
    evaluate.add_argument("--reviews-file")
    evaluate.add_argument("--authorities-file", required=True)

    for name in ("bundle-materialized", "evaluate-materialized"):
        command = subparsers.add_parser(name)
        command.add_argument("--output-dir", required=True)
        command.add_argument("--metadata-file", required=True)
        command.add_argument("--diff-file", required=True)
        command.add_argument("--patch-file", required=True)
        command.add_argument("--pr-body-file", required=name == "evaluate-materialized")
        if name == "evaluate-materialized":
            command.add_argument("--comments-file", required=True)
            command.add_argument("--reviews-file")
            command.add_argument("--authorities-file", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "bundle":
        return _command_bundle(args)
    if args.command == "bundle-materialized":
        return _command_bundle_materialized(args)
    if args.command == "evaluate-materialized":
        return _command_evaluate_materialized(args)
    return _command_evaluate(args)


if __name__ == "__main__":
    raise SystemExit(main())
