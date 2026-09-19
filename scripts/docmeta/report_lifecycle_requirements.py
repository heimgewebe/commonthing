#!/usr/bin/env python3
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess


@dataclass(frozen=True)
class RequiredFieldRule:
    field: str
    code: str
    message: str


BASE_REPORT_RULES = (
    RequiredFieldRule(
        field="lifecycle_state",
        code="missing_lifecycle_state",
        message="report documents should define lifecycle_state",
    ),
    RequiredFieldRule(
        field="status",
        code="missing_status",
        message="report documents should define status",
    ),
)

STATUS_RULES = {
    "active": (
        RequiredFieldRule(
            field="lifecycle",
            code="missing_lifecycle",
            message="active reports should define lifecycle",
        ),
        RequiredFieldRule(
            field="review_after",
            code="missing_review_after",
            message="active/draft reports should define review_after",
        ),
    ),
    "draft": (
        RequiredFieldRule(
            field="review_after",
            code="missing_review_after",
            message="active/draft reports should define review_after",
        ),
    ),
}

LIFECYCLE_STATE_RULES = {
    "active": (
        RequiredFieldRule(
            field="lifecycle",
            code="missing_lifecycle",
            message="active reports should define lifecycle",
        ),
        RequiredFieldRule(
            field="owner_task",
            code="missing_owner_task",
            message="active reports should define owner_task",
        ),
        RequiredFieldRule(
            field="review_after",
            code="missing_review_after",
            message="active/draft reports should define review_after",
        ),
    ),
    "deferred": (
        RequiredFieldRule(
            field="lifecycle",
            code="missing_lifecycle",
            message="deferred reports should define lifecycle",
        ),
        RequiredFieldRule(
            field="owner_task",
            code="missing_owner_task",
            message="deferred reports should define owner_task",
        ),
        RequiredFieldRule(
            field="review_after",
            code="missing_review_after",
            message="deferred reports should define review_after",
        ),
    ),
    "superseded": (
        RequiredFieldRule(
            field="lifecycle",
            code="missing_lifecycle",
            message="superseded reports should define lifecycle",
        ),
        RequiredFieldRule(
            field="owner_task",
            code="missing_owner_task",
            message="superseded reports should define owner_task",
        ),
        RequiredFieldRule(
            field="superseded_by",
            code="missing_superseded_by",
            message="superseded reports should define superseded_by",
        ),
    ),
    "archived": (
        RequiredFieldRule(
            field="lifecycle",
            code="missing_lifecycle",
            message="archived reports should define lifecycle",
        ),
        RequiredFieldRule(
            field="owner_task",
            code="missing_owner_task",
            message="archived reports should define owner_task",
        ),
    ),
}


def string_value(value: object) -> str:
    """Return normalized contract text; non-string scalars are not text."""
    if isinstance(value, str):
        return value.strip()
    return ""


def required_report_field_rules(
    frontmatter: Mapping[str, object],
) -> tuple[RequiredFieldRule, ...]:
    doc_type = string_value(frontmatter.get("doc_type")).lower()
    if doc_type != "report":
        return ()

    status = string_value(frontmatter.get("status")).lower()
    lifecycle_state = string_value(frontmatter.get("lifecycle_state")).lower()

    candidates = (
        *BASE_REPORT_RULES,
        *STATUS_RULES.get(status, ()),
        *LIFECYCLE_STATE_RULES.get(lifecycle_state, ()),
    )

    rules: list[RequiredFieldRule] = []
    seen_codes: set[str] = set()
    for rule in candidates:
        if rule.code in seen_codes:
            continue
        rules.append(rule)
        seen_codes.add(rule.code)
    return tuple(rules)


def missing_required_report_field_rules(
    frontmatter: Mapping[str, object],
) -> tuple[RequiredFieldRule, ...]:
    return tuple(
        rule
        for rule in required_report_field_rules(frontmatter)
        if not string_value(frontmatter.get(rule.field))
    )


def missing_required_report_fields(
    frontmatter: Mapping[str, object],
) -> tuple[str, ...]:
    return tuple(
        rule.field
        for rule in missing_required_report_field_rules(frontmatter)
    )

# --- Audit report truth contract v1 ------------------------------------------------------------------
# Kept in this module so generators and validators share one fail-closed contract.
TRUTH_SCHEMA_VERSION = 1
TRUTH_REQUIRED_FIELDS = (
    "schema_version",
    "status",
    "coverage",
    "source_revision",
    "generated_at",
    "sources",
    "limitations",
    "does_not_establish",
)
TRUTH_ALLOWED_STATUSES = frozenset(
    {
        "pass",
        "healthy",
        "no_material_drift",
        "partial",
        "fail",
        "unknown",
        "not_decision_relevant",
        "deprecated",
    }
)
TRUTH_POSITIVE_STATUSES = frozenset({"pass", "healthy", "no_material_drift"})
TRUTH_COVERAGE_FIELDS = frozenset(
    {
        "scope",
        "complete",
        "fresh",
        "method",
        "checked_items",
        "total_items",
        "failures",
    }
)
TRUTH_COVERAGE_METHODS = frozenset({"exact", "bounded", "heuristic"})
TRUTH_FENCE = "json audit-report-truth.v1"
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TRUTH_OPENING_RE = re.compile(
    rf"(?m)^```{re.escape(TRUTH_FENCE)}[ \t]*\r?$"
)
_TRUTH_CLOSING_RE = re.compile(r"(?m)^```[ \t]*\r?$")
_TRUTH_NESTED_FENCE_RE = re.compile(r"(?m)^```[^\r\n]+\r?$")
_TRUTH_PARSE_ERROR_KEY = "__truth_contract_parse_error__"
_ZERO_SHA40 = "0" * 40
_PROVENANCE_UNAVAILABLE_STATE = "unavailable"


def unavailable_provenance(reason: str) -> dict[str, str]:
    normalized = reason.strip()
    if not normalized:
        raise ValueError("unavailable provenance requires a reason")
    return {"state": _PROVENANCE_UNAVAILABLE_STATE, "reason": normalized}


def _is_unavailable_provenance(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == {"state", "reason"}
        and value.get("state") == _PROVENANCE_UNAVAILABLE_STATE
        and isinstance(value.get("reason"), str)
        and bool(str(value.get("reason")).strip())
    )


def _truth_string_list(value: object) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, str) and bool(item.strip()) for item in value
    )


def truth_contract_missing_fields(
    value: Mapping[str, object],
) -> tuple[str, ...]:
    return tuple(field for field in TRUTH_REQUIRED_FIELDS if field not in value)


def _git_revision_is_ancestor(root: Path, revision: str) -> bool:
    try:
        return subprocess.run(
            ["git", "merge-base", "--is-ancestor", revision, "HEAD"],
            cwd=root,
            capture_output=True,
            check=False,
        ).returncode == 0
    except OSError:
        return False


def _git_blob_sha256(root: Path, revision: str, relative: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "show", f"{revision}:{relative.as_posix()}"],
            cwd=root,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return hashlib.sha256(completed.stdout).hexdigest()


def _canonical_iso_timestamp(value: str) -> str | None:
    """Normalise a strict ISO-8601 timestamp to one deterministic spelling.

    `git show -s --format=%cI` schreibt einen Nullversatz je nach git-Version
    als `+00:00` oder als `Z`. Beide bezeichnen denselben Zeitpunkt, erzeugen
    aber unterschiedliche Dateiinhalte — ein generiertes Artefakt wäre dann
    nicht mehr reproduzierbar, sondern abhängig von der git-Version des
    ausführenden Systems. Kanonisch ist hier die Schreibweise von
    `datetime.isoformat()`, also der numerische Versatz.
    """
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.isoformat()


def _git_commit_timestamp(root: Path, revision: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "show", "-s", "--format=%cI", revision],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return _canonical_iso_timestamp(completed.stdout.strip())


def _git_is_shallow_repository(root: Path) -> bool | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--is-shallow-repository"],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    value = completed.stdout.strip()
    if value not in {"true", "false"}:
        return None
    return value == "true"


def _parse_iso_instant(value: str | None) -> datetime | None:
    """Parse a strict ISO-8601 timestamp into an aware datetime, or None."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _same_instant(left: str | None, right: str | None) -> bool:
    """Compare two ISO-8601 timestamps by instant, not by spelling.

    `git show -s --format=%cI` renders a zero UTC offset as `+00:00` in some
    git versions and as `Z` in others. Beide bezeichnen denselben Zeitpunkt;
    ein Stringvergleich würde die Schreibweise des ausführenden git zu einem
    Provenance-Befund machen und damit etwas prüfen, das der Vertrag nicht
    behauptet.
    """
    left_instant = _parse_iso_instant(left)
    right_instant = _parse_iso_instant(right)
    if left_instant is None or right_instant is None:
        return False
    return left_instant == right_instant


def _ensure_git_revision(root: Path, revision: str) -> str | None:
    """Resolve local provenance without fetching or mutating the repository."""
    return _git_commit_timestamp(root, revision)


def validate_truth_contract(
    value: object,
    *,
    expected_revision: str | None = None,
    root: Path | None = None,
) -> tuple[str, ...]:
    """Validate the machine-readable audit-report truth contract fail-closed."""
    if not isinstance(value, Mapping):
        return ("contract_not_object",)
    if set(value) == {_TRUTH_PARSE_ERROR_KEY}:
        parse_error = value.get(_TRUTH_PARSE_ERROR_KEY)
        return (parse_error,) if isinstance(parse_error, str) else ("truth_contract_invalid",)

    violations: list[str] = []
    keys = set(value)
    required = set(TRUTH_REQUIRED_FIELDS)
    violations.extend(f"missing_{field}" for field in sorted(required - keys))
    violations.extend(f"unknown_{field}" for field in sorted(keys - required))

    if value.get("schema_version") != TRUTH_SCHEMA_VERSION:
        violations.append("invalid_schema_version")

    status = value.get("status")
    if not isinstance(status, str) or status not in TRUTH_ALLOWED_STATUSES:
        violations.append("invalid_status")
        status = "unknown"

    coverage = value.get("coverage")
    coverage_values: dict[str, object] = {}
    if not isinstance(coverage, Mapping):
        violations.append("invalid_coverage")
    else:
        coverage_values = dict(coverage)
        coverage_keys = set(coverage_values)
        violations.extend(
            f"missing_coverage_{field}"
            for field in sorted(TRUTH_COVERAGE_FIELDS - coverage_keys)
        )
        violations.extend(
            f"unknown_coverage_{field}"
            for field in sorted(coverage_keys - TRUTH_COVERAGE_FIELDS)
        )
        scope = coverage_values.get("scope")
        if not isinstance(scope, str) or not scope.strip():
            violations.append("invalid_coverage_scope")
        if not isinstance(coverage_values.get("complete"), bool):
            violations.append("invalid_coverage_complete")
        if not isinstance(coverage_values.get("fresh"), bool):
            violations.append("invalid_coverage_fresh")
        if coverage_values.get("method") not in TRUTH_COVERAGE_METHODS:
            violations.append("invalid_coverage_method")
        for field in ("checked_items", "total_items", "failures"):
            item = coverage_values.get(field)
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                violations.append(f"invalid_coverage_{field}")

    revision_value = value.get("source_revision")
    revision: str | None = None
    revision_unavailable = _is_unavailable_provenance(revision_value)
    if isinstance(revision_value, str) and _SHA40_RE.fullmatch(revision_value) is not None:
        if revision_value == _ZERO_SHA40:
            violations.append("invalid_source_revision")
        else:
            revision = revision_value
            if expected_revision is not None and revision != expected_revision:
                violations.append("source_revision_mismatch")
    elif not revision_unavailable:
        violations.append("invalid_source_revision")

    generated_at_value = value.get("generated_at")
    generated_at: str | None = None
    generated_at_unavailable = _is_unavailable_provenance(generated_at_value)
    if isinstance(generated_at_value, str):
        generated_at = generated_at_value
        try:
            parsed = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("timezone missing")
        except ValueError:
            violations.append("invalid_generated_at")
            generated_at = None
    elif not generated_at_unavailable:
        violations.append("invalid_generated_at")

    if revision_unavailable != generated_at_unavailable:
        violations.append("inconsistent_provenance_state")

    revision_resolved = False
    if root is not None and revision is not None:
        shallow = _git_is_shallow_repository(root)
        if shallow is not False:
            violations.append("source_history_unavailable")
        else:
            commit_timestamp = _ensure_git_revision(root, revision)
            if commit_timestamp is None:
                violations.append("source_revision_not_found")
            else:
                revision_resolved = True
                if not _git_revision_is_ancestor(root, revision):
                    violations.append("source_revision_not_ancestor")
                if not _same_instant(generated_at, commit_timestamp):
                    violations.append("generated_at_revision_mismatch")

    sources = value.get("sources")
    if not isinstance(sources, list) or not sources:
        violations.append("invalid_sources")
    else:
        seen_paths: set[str] = set()
        for index, source in enumerate(sources):
            if not isinstance(source, Mapping) or set(source) != {"path", "sha256"}:
                violations.append(f"invalid_source_{index}")
                continue
            source_path = source.get("path")
            digest = source.get("sha256")
            if not isinstance(source_path, str) or not source_path.strip():
                violations.append(f"invalid_source_path_{index}")
                continue
            if source_path in seen_paths:
                violations.append(f"duplicate_source_path_{index}")
            else:
                seen_paths.add(source_path)
            relative = Path(source_path)
            if relative.is_absolute() or ".." in relative.parts:
                violations.append(f"invalid_source_path_{index}")
            if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
                violations.append(f"invalid_source_sha256_{index}")
            elif root is not None and not relative.is_absolute() and ".." not in relative.parts:
                full_path = root / relative
                if not full_path.is_file():
                    violations.append(f"missing_source_{index}")
                elif hashlib.sha256(full_path.read_bytes()).hexdigest() != digest:
                    violations.append(f"source_digest_mismatch_{index}")
                elif revision_resolved:
                    revision_digest = _git_blob_sha256(root, revision, relative)
                    if revision_digest is None:
                        violations.append(f"source_missing_at_revision_{index}")
                    elif revision_digest != digest:
                        violations.append(f"source_revision_digest_mismatch_{index}")

    if not _truth_string_list(value.get("limitations")):
        violations.append("invalid_limitations")
    if not _truth_string_list(value.get("does_not_establish")):
        violations.append("invalid_does_not_establish")

    if status in TRUTH_POSITIVE_STATUSES:
        if revision is None:
            violations.append("positive_status_source_revision_unavailable")
        if generated_at is None:
            violations.append("positive_status_generated_at_unavailable")
        if not isinstance(coverage, Mapping):
            violations.append("positive_status_invalid_coverage")
        else:
            checked = coverage_values.get("checked_items")
            total = coverage_values.get("total_items")
            if coverage_values.get("complete") is not True:
                violations.append("positive_status_incomplete_coverage")
            if coverage_values.get("fresh") is not True:
                violations.append("positive_status_stale_coverage")
            if coverage_values.get("method") == "heuristic":
                violations.append("positive_status_heuristic_coverage")
            if coverage_values.get("failures") != 0:
                violations.append("positive_status_has_failures")
            if (
                isinstance(checked, int)
                and not isinstance(checked, bool)
                and isinstance(total, int)
                and not isinstance(total, bool)
                and checked != total
            ):
                violations.append("positive_status_partial_item_coverage")

    return tuple(dict.fromkeys(violations))


def build_truth_contract(
    *,
    status: str,
    scope: str,
    complete: bool,
    fresh: bool,
    method: str,
    checked_items: int,
    total_items: int,
    failures: int,
    source_revision: object,
    generated_at: object,
    sources: list[dict[str, str]],
    limitations: list[str],
    does_not_establish: list[str],
) -> dict[str, object]:
    contract: dict[str, object] = {
        "schema_version": TRUTH_SCHEMA_VERSION,
        "status": status,
        "coverage": {
            "scope": scope,
            "complete": complete,
            "fresh": fresh,
            "method": method,
            "checked_items": checked_items,
            "total_items": total_items,
            "failures": failures,
        },
        "source_revision": source_revision,
        "generated_at": generated_at,
        "sources": sources,
        "limitations": limitations,
        "does_not_establish": does_not_establish,
    }
    violations = validate_truth_contract(contract)
    if violations:
        raise ValueError("invalid truth contract: " + ", ".join(violations))
    return contract


def truth_contract_markdown(contract: Mapping[str, object]) -> str:
    return (
        "## Machine-readable truth contract\n\n"
        "Schema: `contracts/audit-report-truth.schema.json`\n\n"
        f"```{TRUTH_FENCE}\n"
        + json.dumps(contract, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n```\n"
    )


def extract_truth_contract_markdown(markdown: str) -> object:
    marker = f"```{TRUTH_FENCE}"
    marker_count = markdown.count(marker)
    openings = list(_TRUTH_OPENING_RE.finditer(markdown))
    if marker_count == 0:
        raise ValueError("truth_contract_missing")
    if marker_count != len(openings):
        raise ValueError("truth_contract_inline_fence")
    if len(openings) > 1:
        first_closing = _TRUTH_CLOSING_RE.search(markdown, openings[0].end())
        if first_closing is not None and openings[1].start() < first_closing.start():
            raise ValueError("truth_contract_nested_fence")
        raise ValueError("truth_contract_multiple")

    opening = openings[0]
    payload_start = markdown.find("\n", opening.end())
    if payload_start < 0:
        raise ValueError("truth_contract_unclosed")
    payload_start += 1
    closing = _TRUTH_CLOSING_RE.search(markdown, payload_start)
    if closing is None:
        raise ValueError("truth_contract_unclosed")
    payload = markdown[payload_start:closing.start()]
    if _TRUTH_NESTED_FENCE_RE.search(payload):
        raise ValueError("truth_contract_nested_fence")
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("truth_contract_invalid_json") from exc


def parse_truth_contract_markdown(markdown: str) -> object | None:
    try:
        return extract_truth_contract_markdown(markdown)
    except ValueError as exc:
        code = str(exc)
        if code == "truth_contract_missing":
            return None
        return {_TRUTH_PARSE_ERROR_KEY: code}


def source_manifest(root: Path, paths: Sequence[Path]) -> list[dict[str, str]]:
    manifest: list[dict[str, str]] = []
    root_resolved = root.resolve()
    for path in sorted({candidate.resolve() for candidate in paths}):
        try:
            relative = path.relative_to(root_resolved).as_posix()
        except ValueError as exc:
            raise ValueError(f"source outside repository: {path}") from exc
        if not path.is_file():
            raise FileNotFoundError(relative)
        manifest.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    if not manifest:
        raise ValueError("truth contract requires at least one source")
    return manifest


def source_revision_metadata(
    root: Path,
    source_paths: Sequence[Path],
) -> tuple[object, object, bool]:
    """Resolve provenance from local Git state without network or repo mutation."""
    relative_paths: list[str] = []
    root_resolved = root.resolve()
    for path in source_paths:
        try:
            relative_paths.append(path.resolve().relative_to(root_resolved).as_posix())
        except ValueError as exc:
            raise ValueError(f"source outside repository: {path}") from exc
    relative_paths = sorted(dict.fromkeys(relative_paths))
    if not relative_paths:
        unavailable = unavailable_provenance("no_source_paths")
        return (unavailable, unavailable.copy(), False)
    shallow = _git_is_shallow_repository(root)
    if shallow is not False:
        unavailable = unavailable_provenance("git_history_unavailable")
        return (unavailable, unavailable.copy(), False)
    try:
        revision = subprocess.run(
            ["git", "log", "-1", "--format=%H", "--", *relative_paths],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        if _SHA40_RE.fullmatch(revision) is None or revision == _ZERO_SHA40:
            raise ValueError("source revision unavailable in local history")
        generated_at = _git_commit_timestamp(root, revision)
        if generated_at is None:
            raise ValueError("source revision timestamp unavailable in local history")
        differs_from_revision = subprocess.run(
            ["git", "diff", "--quiet", revision, "--", *relative_paths],
            cwd=root,
            check=False,
        ).returncode
        working_tree_changes = subprocess.run(
            [
                "git",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                *relative_paths,
            ],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        fresh = differs_from_revision == 0 and not working_tree_changes
        return revision, generated_at, fresh
    except (OSError, subprocess.CalledProcessError, ValueError):
        unavailable = unavailable_provenance("git_provenance_unavailable")
        return (unavailable, unavailable.copy(), False)


def report_truth_migration_state(
    frontmatter: Mapping[str, object],
    *,
    truth_contract_present: bool = False,
) -> str:
    if truth_contract_present:
        return "migrated"
    lifecycle_state = string_value(frontmatter.get("lifecycle_state")).lower()
    status = string_value(frontmatter.get("status")).lower()
    if lifecycle_state in {"archived", "superseded"} or status in {
        "deprecated",
        "obsolete",
    }:
        return "deprecated"
    return "not_decision_relevant"
