#!/usr/bin/env python3
"""Audit immutable GitHub Action refs and selected upstream provenance.

The guard is dependency-free and hermetic. It never resolves network data at
runtime. Upstream evidence is selected and read back separately, then committed
here as a small exact record. Unit tests inject alternate evidence to prove
fail-closed handling of moved tags, missing evidence, and mismatched pins.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

USES_RE = re.compile(
    r"^\s*-?\s*uses\s*:\s*(?P<uses>[^#\s]+)(?:\s+#\s*(?P<comment>.*))?$"
)
JOB_RE = re.compile(r"^  (?P<job>[A-Za-z0-9_-]+)\s*:\s*(?:#.*)?$")
HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
TAG_COMMENT_RE = re.compile(
    r"(?:^|\s)tag:\s*(?P<tag>[A-Za-z0-9][A-Za-z0-9._-]{0,79})(?:\s|$)"
)
UNTAGGED_COMMENT_RE = re.compile(
    r"(?:^|\s)provenance:\s*untagged(?:\s|$)", re.IGNORECASE
)
MUTABLE_DEFAULT_BRANCHES = {"main", "master", "trunk"}
BLOCKING_POLICIES = {"named-ref", "mutable-default-branch", "missing-ref"}
BLOCKING_PROVENANCE = {"tag_mismatch", "unresolved"}
DECLARATION_REQUIRED_KINDS = {"github-action"}
PROVENANCE_ACTIONS = {"actions/cache"}

EXPECTED_ACTION_CONSUMERS: Mapping[str, Mapping[str, int]] = {
    "actions/cache": {
        ".github/workflows/api-smoke.yml": 1,
        ".github/workflows/auth-passkey-register-proof.yml": 1,
        ".github/workflows/auth-session-persistence-proof.yml": 1,
        ".github/workflows/ci.yml": 3,
        ".github/workflows/kubernetes-platform-proof.yml": 4,
        ".github/workflows/python-tooling.yml": 1,
        ".github/workflows/reusable-web-check.yml": 1,
    }
}


class EvidenceError(RuntimeError):
    """Locked provenance evidence is malformed or contradictory."""


@dataclass(frozen=True)
class ActionRef:
    workflow: Path
    job: str
    uses: str
    kind: str
    target: str
    ref_type: str
    ref_value: str
    policy: str
    declared_tag: str | None
    explicit_untagged: bool


@dataclass(frozen=True)
class ProvenanceEvidence:
    action: str
    declared_tag: str | None
    selected_tag_commit: str
    readback_tag_commit: str
    product_version: str
    evidence_source: str


@dataclass(frozen=True)
class ProvenanceRecord:
    workflow: str
    job: str
    action: str
    pinned_commit: str
    declared_tag: str | None
    tag_commit: str | None
    product_version: str | None
    classification: str
    evidence_source: str


LOCKED_EVIDENCE: tuple[ProvenanceEvidence, ...] = (
    ProvenanceEvidence(
        action="actions/cache",
        declared_tag="v6.1.0",
        selected_tag_commit="55cc8345863c7cc4c66a329aec7e433d2d1c52a9",
        readback_tag_commit="55cc8345863c7cc4c66a329aec7e433d2d1c52a9",
        product_version="6.1.0",
        evidence_source=(
            "github-api:actions/cache tag v6.1.0 and package.json@v6.1.0; "
            "selected/readback 2026-08-02"
        ),
    ),
    ProvenanceEvidence(
        action="actions/cache",
        declared_tag=None,
        selected_tag_commit="3edfce9056124e459a23f683a21433670d47daca",
        readback_tag_commit="3edfce9056124e459a23f683a21433670d47daca",
        product_version="6.1.0+@actions/cache-6.2.0",
        evidence_source=(
            "github-api:actions/cache compare v6.1.0...3edfce9056124e459a23f683a21433670d47daca "
            "and package.json@3edfce9; selected/readback 2026-08-02"
        ),
    ),
)


def clean_uses(raw_uses: str) -> str:
    return raw_uses.strip().strip('"\'')


def workflow_paths(workflows_dir: Path) -> list[Path]:
    return sorted({*workflows_dir.glob("*.yml"), *workflows_dir.glob("*.yaml")})


def split_ref(uses: str) -> tuple[str, str]:
    target, sep, ref = uses.partition("@")
    return target, ref if sep else ""


def ref_type_for(ref_value: str) -> str:
    if not ref_value:
        return "no-ref"
    if HEX40_RE.fullmatch(ref_value):
        return "sha"
    return "named-ref"


def kind_for(target: str) -> str:
    if target.startswith("./") or target.startswith("../"):
        return "local-action"
    if target.startswith("docker://"):
        return "docker-image"
    if ".github/workflows/" in target:
        return "reusable-workflow"
    return "github-action"


def policy_for(kind: str, ref_type: str, ref_value: str) -> str:
    if kind == "local-action":
        return "local"
    if kind == "docker-image":
        return "docker-ref"
    if ref_type == "sha":
        return "pinned-sha"
    if ref_type == "no-ref":
        return "missing-ref"
    if ref_value in MUTABLE_DEFAULT_BRANCHES:
        return "mutable-default-branch"
    return "named-ref"


def declaration_for(comment: str) -> tuple[str | None, bool]:
    tag_match = TAG_COMMENT_RE.search(comment)
    explicit_untagged = bool(UNTAGGED_COMMENT_RE.search(comment))
    return (tag_match.group("tag") if tag_match else None, explicit_untagged)


def scan_workflow(path: Path) -> list[ActionRef]:
    refs: list[ActionRef] = []
    current_job = "unknown"
    in_jobs = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("jobs:"):
            in_jobs = True
            current_job = "unknown"
            continue
        if in_jobs and line and not line.startswith((" ", "#")):
            in_jobs = False
            current_job = "unknown"
        if in_jobs:
            job_match = JOB_RE.match(line)
            if job_match:
                current_job = job_match.group("job")
        uses_match = USES_RE.match(line)
        if not uses_match:
            continue
        uses = clean_uses(uses_match.group("uses"))
        comment = (uses_match.group("comment") or "").strip()
        target, ref_value = split_ref(uses)
        kind = kind_for(target)
        ref_type = ref_type_for(ref_value)
        declared_tag, explicit_untagged = declaration_for(comment)
        refs.append(
            ActionRef(
                workflow=path,
                job=current_job,
                uses=uses,
                kind=kind,
                target=target,
                ref_type=ref_type,
                ref_value=ref_value,
                policy=policy_for(kind, ref_type, ref_value),
                declared_tag=declared_tag,
                explicit_untagged=explicit_untagged,
            )
        )
    return refs


def scan(workflows_dir: Path) -> list[ActionRef]:
    refs: list[ActionRef] = []
    for path in workflow_paths(workflows_dir):
        refs.extend(scan_workflow(path))
    return refs


def _strict_evidence_record(raw: Any) -> ProvenanceEvidence:
    required = {
        "action",
        "declared_tag",
        "selected_tag_commit",
        "readback_tag_commit",
        "product_version",
        "evidence_source",
    }
    if not isinstance(raw, dict) or set(raw) != required:
        raise EvidenceError("provenance evidence fields are incomplete or unknown")
    declared_tag = raw["declared_tag"]
    if declared_tag is not None and (
        not isinstance(declared_tag, str) or not declared_tag.strip()
    ):
        raise EvidenceError("declared_tag must be null or a non-empty string")
    for field in ("action", "product_version", "evidence_source"):
        value = raw[field]
        if not isinstance(value, str) or not value or value != value.strip():
            raise EvidenceError(f"{field} must be a non-empty trimmed string")
    for field in ("selected_tag_commit", "readback_tag_commit"):
        value = raw[field]
        if not isinstance(value, str) or not HEX40_RE.fullmatch(value):
            raise EvidenceError(f"{field} must be a lowercase 40-character commit")
    return ProvenanceEvidence(
        action=raw["action"],
        declared_tag=declared_tag,
        selected_tag_commit=raw["selected_tag_commit"],
        readback_tag_commit=raw["readback_tag_commit"],
        product_version=raw["product_version"],
        evidence_source=raw["evidence_source"],
    )


def load_evidence(path: Path | None) -> tuple[ProvenanceEvidence, ...]:
    if path is None:
        return LOCKED_EVIDENCE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EvidenceError(f"cannot read provenance evidence: {error}") from error
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "evidence"}:
        raise EvidenceError("evidence document must contain schema_version and evidence")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise EvidenceError("unsupported provenance evidence schema")
    if not isinstance(raw["evidence"], list):
        raise EvidenceError("evidence must be a list")
    evidence = tuple(_strict_evidence_record(item) for item in raw["evidence"])
    keys = [(item.action, item.declared_tag, item.readback_tag_commit) for item in evidence]
    if len(set(keys)) != len(keys):
        raise EvidenceError("provenance evidence contains duplicate identities")
    return evidence


def _matching_evidence(
    ref: ActionRef, evidence: Sequence[ProvenanceEvidence]
) -> ProvenanceEvidence | None:
    candidates = [item for item in evidence if item.action == ref.target]
    if ref.declared_tag is not None:
        candidates = [item for item in candidates if item.declared_tag == ref.declared_tag]
    elif ref.explicit_untagged:
        candidates = [
            item
            for item in candidates
            if item.declared_tag is None
            and item.readback_tag_commit == ref.ref_value
        ]
    else:
        return None
    return candidates[0] if len(candidates) == 1 else None


def classify_provenance(
    ref: ActionRef, evidence: Sequence[ProvenanceEvidence]
) -> ProvenanceRecord | None:
    if ref.kind != "github-action" or ref.target not in PROVENANCE_ACTIONS:
        return None
    selected = _matching_evidence(ref, evidence)
    tag_commit = selected.readback_tag_commit if selected else None
    source = selected.evidence_source if selected else "missing-or-ambiguous-evidence"
    product_version = selected.product_version if selected else None

    if ref.declared_tag is not None and ref.explicit_untagged:
        classification = "unresolved"
    elif ref.ref_type != "sha":
        classification = "unresolved"
    elif selected is None:
        classification = "unresolved"
    elif selected.selected_tag_commit != selected.readback_tag_commit:
        classification = "unresolved"
        source = f"{selected.evidence_source}; tag-moved-between-selection-and-readback"
    elif ref.declared_tag is not None:
        classification = (
            "exact_tag"
            if ref.ref_value == selected.readback_tag_commit
            else "tag_mismatch"
        )
    elif ref.explicit_untagged:
        classification = "untagged_commit"
    else:
        classification = "unresolved"

    return ProvenanceRecord(
        workflow=ref.workflow.as_posix(),
        job=ref.job,
        action=ref.target,
        pinned_commit=ref.ref_value,
        declared_tag=ref.declared_tag,
        tag_commit=tag_commit,
        product_version=product_version,
        classification=classification,
        evidence_source=source,
    )


def provenance_records(
    refs: Sequence[ActionRef], evidence: Sequence[ProvenanceEvidence]
) -> list[ProvenanceRecord]:
    records = [classify_provenance(ref, evidence) for ref in refs]
    return sorted(
        (record for record in records if record is not None),
        key=lambda item: (item.action, item.workflow, item.job, item.pinned_commit),
    )


def undeclared_refs(refs: Sequence[ActionRef]) -> list[ActionRef]:
    """SHA pins that state neither a tag nor an explicit untagged provenance.

    A bare commit says what is executed but not what it is supposed to be, so
    neither a reviewer nor Renovate can tell whether the pin is current. The
    `# tag: <tag>` comment is what renovate.json's custom manager reads; the
    `# provenance: untagged` marker is the deliberate opt-out for commits that
    carry no tag at all.

    Reusable workflows are exempt. Their `owner/repo/path@sha` reference is
    already close to the 120-character yaml line limit, so a trailing comment
    does not fit, and the custom manager does not match path-suffixed
    references either — declaring one would serve no reader.
    """
    return [
        ref
        for ref in refs
        if ref.kind in DECLARATION_REQUIRED_KINDS
        and ref.ref_type == "sha"
        and ref.declared_tag is None
        and not ref.explicit_untagged
    ]


def consumer_contract_errors(refs: Sequence[ActionRef]) -> list[str]:
    errors: list[str] = []
    for action, expected_counts in EXPECTED_ACTION_CONSUMERS.items():
        counts: dict[str, int] = {}
        for ref in refs:
            if ref.target == action:
                path = ref.workflow.as_posix()
                counts[path] = counts.get(path, 0) + 1
        expected = set(expected_counts)
        observed = set(counts)
        for path in sorted(expected - observed):
            errors.append(f"{action}: missing expected consumer {path}")
        for path in sorted(observed - expected):
            errors.append(f"{action}: unexpected consumer {path}")
        for path in sorted(expected & observed):
            expected_count = expected_counts[path]
            if counts[path] != expected_count:
                errors.append(
                    f"{action}: expected {expected_count} uses in {path}, "
                    f"observed {counts[path]}"
                )
    return errors


def _counts(values: Iterable[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        result[value] = result.get(value, 0) + 1
    return dict(sorted(result.items()))


def report_payload(
    refs: Sequence[ActionRef],
    records: Sequence[ProvenanceRecord],
    contract_errors: Sequence[str],
) -> dict[str, Any]:
    pinning_blockers = [
        ref
        for ref in refs
        if ref.kind in {"github-action", "reusable-workflow"}
        and ref.policy in BLOCKING_POLICIES
    ]
    provenance_blockers = [
        record for record in records if record.classification in BLOCKING_PROVENANCE
    ]
    declaration_blockers = undeclared_refs(refs)
    return {
        "schema_version": 1,
        "status": (
            "fail"
            if pinning_blockers
            or provenance_blockers
            or declaration_blockers
            or contract_errors
            else "pass"
        ),
        "summary": {
            "total": len(refs),
            "kind": _counts(ref.kind for ref in refs),
            "policy": _counts(ref.policy for ref in refs),
            "provenance": _counts(record.classification for record in records),
        },
        "provenance": [asdict(record) for record in records],
        "consumer_contract_errors": list(contract_errors),
        "pinning_blockers": [
            {
                "workflow": ref.workflow.as_posix(),
                "job": ref.job,
                "uses": ref.uses,
                "policy": ref.policy,
            }
            for ref in pinning_blockers
        ],
        "declaration_blockers": [
            {
                "workflow": ref.workflow.as_posix(),
                "job": ref.job,
                "uses": ref.uses,
            }
            for ref in declaration_blockers
        ],
    }


def print_text_report(payload: Mapping[str, Any], refs: Sequence[ActionRef]) -> None:
    summary = payload["summary"]
    print("GitHub Actions reference pinning audit:")
    print(f"total={summary['total']}")
    for key, value in summary["kind"].items():
        print(f"kind.{key}={value}")
    for key, value in summary["policy"].items():
        print(f"policy.{key}={value}")
    for key, value in summary["provenance"].items():
        print(f"provenance.{key}={value}")
    for ref in refs:
        print(
            f"- {ref.workflow} {ref.job} -> {ref.uses} "
            f"kind={ref.kind} ref={ref.ref_type} policy={ref.policy}"
        )
    for record in payload["provenance"]:
        print(
            "- provenance "
            f"{record['workflow']} {record['job']} "
            f"action={record['action']} pinned_commit={record['pinned_commit']} "
            f"declared_tag={record['declared_tag']} tag_commit={record['tag_commit']} "
            f"classification={record['classification']} "
            f"evidence_source={record['evidence_source']}"
        )
    if payload["consumer_contract_errors"]:
        print()
        print("ERROR: action provenance consumer contract failed.")
        for error in payload["consumer_contract_errors"]:
            print(f"- {error}")
    if payload["pinning_blockers"]:
        print()
        print(
            "ERROR: external GitHub Actions and reusable workflows must be "
            "pinned to 40-character commit SHAs."
        )
        print(
            "Allowed without a GitHub SHA: local actions (./ or ../) and "
            "docker:// image actions."
        )
        for blocker in payload["pinning_blockers"]:
            print(
                f"- {blocker['workflow']} {blocker['job']}: "
                f"{blocker['uses']} policy={blocker['policy']}"
            )
    if payload["declaration_blockers"]:
        print()
        print(
            "ERROR: every SHA-pinned action must declare its provenance, either "
            "'# tag: <tag>' or '# provenance: untagged'."
        )
        print(
            "Undeclared pins are invisible to the Renovate custom manager in "
            "renovate.json and never receive update proposals."
        )
        for blocker in payload["declaration_blockers"]:
            print(
                f"- {blocker['workflow']} {blocker['job']}: {blocker['uses']}"
            )
    provenance_blockers = [
        record
        for record in payload["provenance"]
        if record["classification"] in BLOCKING_PROVENANCE
    ]
    if provenance_blockers:
        print()
        print("ERROR: selected GitHub Action provenance is not exact.")
        for record in provenance_blockers:
            print(
                f"- {record['workflow']} {record['job']}: "
                f"{record['action']} classification={record['classification']}"
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--workflows-dir", type=Path, default=Path(".github/workflows")
    )
    parser.add_argument("--evidence-file", type=Path)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument(
        "--enforce-consumers",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    args = parser.parse_args()

    try:
        evidence = load_evidence(args.evidence_file)
    except EvidenceError as error:
        payload = {
            "schema_version": 1,
            "status": "fail",
            "error": str(error),
        }
        if args.format == "json":
            print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        else:
            print(f"ERROR: {error}")
        return 1

    refs = scan(args.workflows_dir)
    records = provenance_records(refs, evidence)
    enforce_consumers = (
        args.enforce_consumers
        if args.enforce_consumers is not None
        else Path("repo.meta.yaml").is_file()
    )
    contract_errors = consumer_contract_errors(refs) if enforce_consumers else []
    payload = report_payload(refs, records, contract_errors)
    if args.format == "json":
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    else:
        print_text_report(payload, refs)
    return 0 if payload["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
