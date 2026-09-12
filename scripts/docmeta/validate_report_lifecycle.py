#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.docmeta.docmeta import parse_frontmatter
from scripts.docmeta.report_lifecycle_requirements import (
    extract_truth_contract_markdown,
    missing_required_report_field_rules,
    string_value as _string_value,
    validate_truth_contract,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
VALID_MODES = ("report", "warn", "strict")
VALID_REPORT_STATUSES = frozenset(("active", "archived", "draft", "deprecated", "obsolete"))
VALID_LIFECYCLE_STATES = frozenset(("active", "deferred", "superseded", "archived"))
VALID_LIFECYCLES = frozenset(("audit", "decision", "decision-prep", "generated", "planning", "proof"))
VALID_OWNER_STATUSES = frozenset(("blocked", "contradicted", "done", "obsolete", "open", "partial"))
ACTIVE_OWNER_LIFECYCLE_STATES = frozenset(("active", "deferred"))
TERMINAL_OWNER_STATUSES = frozenset(("contradicted", "obsolete"))
ISO_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
OPT_STATUS_ID_RE = re.compile(r"\|\s*(OPT-[A-Z0-9-]+)\s*\|")
BUREAU_OWNER_TASK_RE = re.compile(r"^BUREAU-[A-Za-z0-9._:-]+$")


@dataclass(frozen=True)
class Finding:
    path: str
    code: str
    severity: str
    message: str
    field: str | None = None


def _load_frontmatter(path: Path) -> dict[str, object]:
    fm = parse_frontmatter(str(path))
    if fm is None:
        return {}
    return fm


def _has_parseable_frontmatter(path: Path) -> bool:
    return parse_frontmatter(str(path)) is not None


def _iter_report_paths(root: Path) -> list[Path]:
    reports_dir = root / "docs" / "reports"
    if not reports_dir.exists():
        return []
    return sorted([p for p in reports_dir.rglob("*.md") if p.is_file()])


def _iter_truth_report_paths(root: Path) -> list[Path]:
    return [
        path
        for path in (
            root / "docs" / "_generated" / "report-lifecycle.md",
            root / "docs" / "_generated" / "report-lifecycle-inventory.md",
        )
        if path.is_file()
    ]


def extract_truth_contract(markdown: str) -> dict[str, object]:
    value = extract_truth_contract_markdown(markdown)
    if not isinstance(value, dict):
        raise ValueError("truth_contract_not_object")
    return value


def validate_truth_report(path: Path, root: Path) -> tuple[str, ...]:
    try:
        contract = extract_truth_contract(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return (str(exc) or "truth_contract_invalid_json",)
    return validate_truth_contract(contract, root=root)


def _changed_report_paths(root: Path, changed_from: str, changed_to: str) -> list[Path]:
    completed = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", changed_from, changed_to],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    )
    reports_dir = (root / "docs" / "reports").resolve()
    paths: set[Path] = set()
    for rel in completed.stdout.splitlines():
        candidate = (root / rel).resolve()
        if candidate.suffix != ".md" or not candidate.is_file():
            continue
        try:
            candidate.relative_to(reports_dir)
        except ValueError:
            continue
        paths.add(candidate)
    return sorted(paths)


def _invalid_review_after(value: str) -> bool:
    if not ISO_DATE_RE.match(value):
        return True
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return True
    return False


def _superseded_by_target_exists(value: str, root: Path) -> bool:
    target = Path(value)
    if target.is_absolute() or ".." in target.parts:
        return False
    return (root / target).is_file()


def _is_external_owner_task(value: str) -> bool:
    """Return whether owner_task delegates authority to the external Bureau task plane."""
    return BUREAU_OWNER_TASK_RE.fullmatch(value) is not None


def _registered_owner_tasks(root: Path) -> set[str] | None:
    registered: set[str] = set()
    sources_found = False

    task_index = root / "docs" / "tasks" / "index.json"
    if task_index.is_file():
        sources_found = True
        try:
            data = json.loads(task_index.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        for item in data.get("tasks", []):
            task_id = _string_value(item.get("id") if isinstance(item, dict) else None)
            if task_id:
                registered.add(task_id)

    opt_status = root / "docs" / "reports" / "optimierungsstatus.md"
    if opt_status.is_file():
        sources_found = True
        registered.update(OPT_STATUS_ID_RE.findall(opt_status.read_text(encoding="utf-8")))

    if not sources_found:
        return None
    return registered


def _parse_opt_status_rows(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    headers: list[str] = []
    in_matrix = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() == "## Matrix":
            in_matrix = True
            continue
        if in_matrix and line.startswith("## "):
            break
        if not in_matrix or not line.startswith("|"):
            continue
        cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
        if cells and all(set(cell) <= set("-: ") for cell in cells):
            continue
        if not headers:
            headers = cells
            continue
        if len(cells) == len(headers):
            rows.append(dict(zip(headers, cells, strict=True)))
    return rows


def _owner_task_statuses(root: Path) -> dict[str, set[str]] | None:
    statuses: dict[str, set[str]] = {}
    sources_found = False

    task_index = root / "docs" / "tasks" / "index.json"
    if task_index.is_file():
        sources_found = True
        try:
            data = json.loads(task_index.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        for item in data.get("tasks", []):
            if not isinstance(item, dict):
                continue
            task_id = _string_value(item.get("id"))
            status = _string_value(item.get("status")).strip().lower()
            if task_id and status:
                statuses.setdefault(task_id, set()).add(status)

    opt_status = root / "docs" / "reports" / "optimierungsstatus.md"
    if opt_status.is_file():
        sources_found = True
        for row in _parse_opt_status_rows(opt_status):
            task_id = _string_value(row.get("id"))
            status = _string_value(row.get("status")).strip().lower()
            if task_id and status:
                statuses.setdefault(task_id, set()).add(status)

    if not sources_found:
        return None
    return statuses


def _owner_status_finding(
    rel_path: str,
    owner_task: str,
    lifecycle_state: str,
    owner_statuses: dict[str, set[str]] | None,
) -> Finding | None:
    if not owner_task or owner_statuses is None or owner_task not in owner_statuses:
        return None
    statuses = owner_statuses[owner_task]
    unknown = sorted(status for status in statuses if status not in VALID_OWNER_STATUSES)
    if unknown:
        return Finding(
            path=rel_path,
            code="invalid_owner_status",
            severity="warn",
            field="owner_task",
            message=f"owner_task {owner_task} has unsupported status value(s): {', '.join(unknown)}",
        )
    if len(statuses) > 1:
        return Finding(
            path=rel_path,
            code="invalid_owner_status",
            severity="warn",
            field="owner_task",
            message=f"owner_task {owner_task} resolves to inconsistent status values: {', '.join(sorted(statuses))}",
        )
    status = next(iter(statuses))
    if lifecycle_state in ACTIVE_OWNER_LIFECYCLE_STATES and status in TERMINAL_OWNER_STATUSES:
        return Finding(
            path=rel_path,
            code="invalid_owner_status",
            severity="warn",
            field="owner_task",
            message=f"owner_task {owner_task} has terminal status {status} for {lifecycle_state} report",
        )
    return None


def _validate_report(path: Path, frontmatter: dict[str, object], root: Path) -> list[Finding]:
    try:
        rel_path = path.relative_to(root).as_posix()
    except ValueError:
        rel_path = str(path)

    findings = [
        Finding(
            path=rel_path,
            code=requirement.code,
            severity="warn",
            field=requirement.field,
            message=requirement.message,
        )
        for requirement in missing_required_report_field_rules(frontmatter)
    ]

    doc_type = _string_value(frontmatter.get("doc_type")).strip().lower()
    if doc_type != "report":
        return findings

    status = _string_value(frontmatter.get("status")).strip().lower()
    if status and status not in VALID_REPORT_STATUSES:
        findings.append(Finding(
            path=rel_path,
            code="invalid_status",
            severity="warn",
            field="status",
            message="status must be one of: active, archived, deprecated, draft, obsolete",
        ))

    lifecycle_state = _string_value(frontmatter.get("lifecycle_state")).strip().lower()
    if lifecycle_state and lifecycle_state not in VALID_LIFECYCLE_STATES:
        findings.append(Finding(
            path=rel_path,
            code="invalid_lifecycle_state",
            severity="warn",
            field="lifecycle_state",
            message="lifecycle_state must be one of: active, archived, deferred, superseded",
        ))

    lifecycle = _string_value(frontmatter.get("lifecycle")).strip().lower()
    if lifecycle and lifecycle not in VALID_LIFECYCLES:
        findings.append(Finding(
            path=rel_path,
            code="invalid_lifecycle",
            severity="warn",
            field="lifecycle",
            message="lifecycle must be one of: audit, decision, decision-prep, generated, planning, proof",
        ))

    review_after = _string_value(frontmatter.get("review_after")).strip()
    if review_after and _invalid_review_after(review_after):
        findings.append(Finding(
            path=rel_path,
            code="invalid_review_after",
            severity="warn",
            field="review_after",
            message="review_after must be a valid ISO date in YYYY-MM-DD format",
        ))

    owner_task = _string_value(frontmatter.get("owner_task")).strip()
    external_owner_task = _is_external_owner_task(owner_task)
    registered_owner_tasks = _registered_owner_tasks(root)
    if (
        owner_task
        and not external_owner_task
        and registered_owner_tasks is not None
        and owner_task not in registered_owner_tasks
    ):
        findings.append(Finding(
            path=rel_path,
            code="invalid_owner_task",
            severity="warn",
            field="owner_task",
            message=(
                "owner_task must resolve in docs/tasks/index.json or "
                "docs/reports/optimierungsstatus.md, or use a canonical BUREAU-* external task id"
            ),
        ))
    elif not external_owner_task:
        owner_status_finding = _owner_status_finding(
            rel_path=rel_path,
            owner_task=owner_task,
            lifecycle_state=lifecycle_state,
            owner_statuses=_owner_task_statuses(root),
        )
        if owner_status_finding is not None:
            findings.append(owner_status_finding)

    superseded_by = _string_value(frontmatter.get("superseded_by")).strip()
    if superseded_by:
        if superseded_by == rel_path:
            findings.append(Finding(
                path=rel_path,
                code="invalid_superseded_by",
                severity="warn",
                field="superseded_by",
                message="superseded_by must not point to the report itself",
            ))
        elif not _superseded_by_target_exists(superseded_by, root):
            findings.append(Finding(
                path=rel_path,
                code="invalid_superseded_by",
                severity="warn",
                field="superseded_by",
                message="superseded_by must point to an existing repository file",
            ))

    return findings


def _build_summary(
    findings: list[Finding],
    files_scanned: int,
    reports_checked: int,
    reports_ignored_non_report: int
) -> dict[str, int]:
    summary = {
        "files_scanned": files_scanned,
        "reports_checked": reports_checked,
        "reports_ignored_non_report": reports_ignored_non_report,
        "findings_total": len(findings),
        "missing_status": 0,
        "missing_lifecycle": 0,
        "missing_owner_task": 0,
        "missing_review_after": 0,
        "missing_superseded_by": 0,
        "missing_lifecycle_state": 0,
        "missing_frontmatter": 0,
        "invalid_status": 0,
        "invalid_lifecycle": 0,
        "invalid_lifecycle_state": 0,
        "invalid_review_after": 0,
        "invalid_superseded_by": 0,
        "invalid_owner_task": 0,
        "invalid_owner_status": 0,
    }
    for f in findings:
        if f.code in summary:
            summary[f.code] += 1
    return summary


def _render_report(findings: list[Finding], summary: dict[str, int], mode: str) -> str:
    lines = [
        "# Report Lifecycle Validation",
        "",
        f"Mode: {mode}",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| files_scanned | {summary['files_scanned']} |",
        f"| reports_checked | {summary['reports_checked']} |",
        f"| reports_ignored_non_report | {summary['reports_ignored_non_report']} |",
        f"| findings_total | {summary['findings_total']} |",
        f"| missing_status | {summary['missing_status']} |",
        f"| missing_lifecycle | {summary['missing_lifecycle']} |",
        f"| missing_owner_task | {summary['missing_owner_task']} |",
        f"| missing_review_after | {summary['missing_review_after']} |",
        f"| missing_superseded_by | {summary['missing_superseded_by']} |",
        f"| missing_lifecycle_state | {summary['missing_lifecycle_state']} |",
        f"| missing_frontmatter | {summary['missing_frontmatter']} |",
        f"| invalid_status | {summary['invalid_status']} |",
        f"| invalid_lifecycle | {summary['invalid_lifecycle']} |",
        f"| invalid_lifecycle_state | {summary['invalid_lifecycle_state']} |",
        f"| invalid_review_after | {summary['invalid_review_after']} |",
        f"| invalid_superseded_by | {summary['invalid_superseded_by']} |",
        f"| invalid_owner_task | {summary['invalid_owner_task']} |",
        f"| invalid_owner_status | {summary['invalid_owner_status']} |",
        "",
        "## Findings",
        "",
    ]
    if findings:
        lines.append("| Path | Severity | Code | Field | Message |")
        lines.append("| --- | --- | --- | --- | --- |")
        for f in findings:
            field_str = f.field if f.field else ""
            lines.append(f"| {f.path} | {f.severity} | {f.code} | {field_str} | {f.message} |")
    else:
        lines.append("No findings.")
    return "\n".join(lines) + "\n"


def _gha_escape_data(value: str) -> str:
    return (
        value
        .replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
    )


def _gha_escape_property(value: str) -> str:
    return (
        _gha_escape_data(value)
        .replace(":", "%3A")
        .replace(",", "%2C")
    )


def _render_github_warnings(findings: list[Finding]) -> str:
    lines: list[str] = []
    for finding in findings:
        file_prop = _gha_escape_property(str(finding.path))
        title_prop = _gha_escape_property("Report lifecycle finding")
        message = _gha_escape_data(f"{finding.code}: {finding.message}")
        lines.append(f"::warning file={file_prop},title={title_prop}::{message}")
    return "\n".join(lines)


def run(root: Path, mode: str, changed_from: str | None = None, changed_to: str = "HEAD") -> tuple[str, int]:
    if mode not in VALID_MODES:
        raise ValueError(f"unsupported report lifecycle mode: {mode}")
    paths = (
        _changed_report_paths(root, changed_from, changed_to)
        if changed_from
        else _iter_report_paths(root)
    )
    all_findings = []
    reports_checked = 0
    reports_ignored_non_report = 0

    for p in paths:
        fm = _load_frontmatter(p)
        doc_type = _string_value(fm.get("doc_type")).strip().lower()
        if doc_type == "report":
            reports_checked += 1
        else:
            reports_ignored_non_report += 1
        if not _has_parseable_frontmatter(p):
            try:
                rel_path = p.relative_to(root).as_posix()
            except ValueError:
                rel_path = str(p)
            all_findings.append(Finding(
                path=rel_path,
                code="missing_frontmatter",
                severity="warn",
                field=None,
                message="docs/reports markdown files must have parseable frontmatter",
            ))
            continue
        findings = _validate_report(p, fm, root)
        all_findings.extend(findings)

    for truth_path in _iter_truth_report_paths(root):
        for violation in validate_truth_report(truth_path, root):
            all_findings.append(
                Finding(
                    path=truth_path.relative_to(root).as_posix(),
                    code="invalid_truth_contract",
                    severity="warn",
                    message=violation,
                    field=None,
                )
            )

    all_findings.sort(key=lambda f: (f.path, f.code))

    summary = _build_summary(
        all_findings,
        files_scanned=len(paths),
        reports_checked=reports_checked,
        reports_ignored_non_report=reports_ignored_non_report,
    )
    output = _render_report(all_findings, summary, mode)

    if mode == "warn":
        warnings = _render_github_warnings(all_findings)
        if warnings:
            output = output + "\n" + warnings + "\n"

    if mode == "strict" and summary["findings_total"] > 0:
        return output, 1

    return output, 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate report lifecycle metadata.")
    parser.add_argument(
        "--mode",
        choices=VALID_MODES,
        default="report",
        help="Validation mode"
    )
    parser.add_argument(
        "--root",
        type=str,
        default=None,
        help="Alternative repository root path"
    )
    parser.add_argument(
        "--changed-from",
        type=str,
        default=None,
        help="Git revision to diff from for changed-only validation"
    )
    parser.add_argument(
        "--changed-to",
        type=str,
        default="HEAD",
        help="Git revision to diff to for changed-only validation"
    )
    args = parser.parse_args(argv)

    root_path = Path(args.root) if args.root else REPO_ROOT

    try:
        report_str, exit_code = run(
            root_path,
            args.mode,
            changed_from=args.changed_from,
            changed_to=args.changed_to,
        )
        sys.stdout.write(report_str)
        return exit_code
    except Exception as e:
        sys.stderr.write(f"Error: {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
