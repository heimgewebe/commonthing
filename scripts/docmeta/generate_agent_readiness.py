#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.agent import run_task
from scripts.agent.json_contract import (
    DuplicateKeyError,
    UnsupportedSchemaError,
    load_json_strict,
    loads_json_strict,
    validate_instance,
)
from scripts.agent.validate_handoff import validate_handoff
from scripts.agent.validate_repo_agent_contract import validate_repo_agent_contract
from scripts.docmeta import agent_entrypoint_smoke, validate_claim_registry
from scripts.docmeta.docmeta import REPO_ROOT


@dataclass(frozen=True)
class CapabilityResult:
    id: str
    dimension: str
    title: str
    hard: bool
    status: str
    evidence: list[str]
    missing: list[str]
    rationale: str


VALID_STATUSES = {"pass", "partial", "open", "fail"}

HANDOFF_TASK_FILE = "tests/fixtures/agent/handoff-task.json"
HANDOFF_VALID_FILE = "tests/fixtures/agent/handoff-valid.json"
DRY_RUN_TASK_FILE = "tests/fixtures/agent/valid-doc-drift-task.json"
HANDOFF_REQUIRED_FILES = [
    "contracts/agent/task.schema.json",
    "contracts/agent/handoff.schema.json",
    "scripts/agent/json_contract.py",
    "scripts/agent/check_non_ideal_task.py",
    "scripts/agent/validate_handoff.py",
    "scripts/agent/tests/test_validate_handoff.py",
    "scripts/docmeta/docmeta.py",
    "scripts/docmeta/validate_claim_registry.py",
    "docs/claims/registry.yml",
    HANDOFF_TASK_FILE,
    HANDOFF_VALID_FILE,
]
DRY_RUN_REQUIRED_FILES = [
    "scripts/agent/run_task.py",
    "scripts/agent/tests/test_run_task.py",
    DRY_RUN_TASK_FILE,
]
DISCOVERY_REQUIRED_FILES = [
    "agent-contract.json",
    "contracts/agent/agent-contract.schema.json",
    "AGENTS.md",
    "scripts/agent/validate_repo_agent_contract.py",
    "scripts/docmeta/agent_entrypoint_smoke.py",
]

RUN_EVIDENCE_REQUIRED_FILES = [
    "contracts/agent/validation.schema.json",
    "contracts/agent/run-result.schema.json",
    "scripts/agent/json_contract.py",
    "scripts/agent/run_task.py",
    "scripts/agent/tests/test_json_contract.py",
    "scripts/agent/tests/test_run_task.py",
    "scripts/agent/validate_agent_contracts.py",
    "scripts/contracts-agent-check.sh",
    "docs/reference/agent-run-evidence-lite.md",
    "tests/fixtures/agent/validation-valid.json",
    "tests/fixtures/agent/run-result-valid.json",
    DRY_RUN_TASK_FILE,
]


def _handoff_failure(
    presence: CapabilityResult,
    diagnostic: str,
) -> CapabilityResult:
    diagnostic = diagnostic.strip()
    if len(diagnostic) > 240:
        diagnostic = diagnostic[:237] + "..."
    suffix = f": {diagnostic}" if diagnostic else "."
    return CapabilityResult(
        id=presence.id,
        dimension=presence.dimension,
        title=presence.title,
        hard=presence.hard,
        status="fail",
        evidence=presence.evidence,
        missing=["functional handoff smoke"],
        rationale=f"{presence.rationale} Functional smoke failed{suffix}",
    )


def _capability_failure(
    presence: CapabilityResult,
    missing_item: str,
    diagnostic: str,
) -> CapabilityResult:
    diagnostic = diagnostic.strip()
    if len(diagnostic) > 240:
        diagnostic = diagnostic[:237] + "..."
    suffix = f": {diagnostic}" if diagnostic else "."
    return CapabilityResult(
        id=presence.id,
        dimension=presence.dimension,
        title=presence.title,
        hard=presence.hard,
        status="fail",
        evidence=presence.evidence,
        missing=[missing_item],
        rationale=f"{presence.rationale} Functional smoke failed{suffix}",
    )


def _evaluate_discovery_contract(root: Path) -> CapabilityResult:
    presence = _evaluate_required_files(
        root=root,
        cap_id="discover",
        dimension="discover",
        title="Repository discovery contract",
        hard=True,
        required_files=DISCOVERY_REQUIRED_FILES,
        rationale=(
            "The repository must expose a deterministic machine contract, "
            "entry card, validator, and entrypoint smoke before planning."
        ),
    )
    if presence.status != "pass":
        return presence

    try:
        contract_findings = validate_repo_agent_contract(root)
        entrypoint_findings = agent_entrypoint_smoke.run_checks(root)
    except (OSError, RuntimeError, ValueError) as exc:
        return _capability_failure(
            presence, "functional discovery smoke", str(exc)
        )

    if contract_findings:
        diagnostic = json.dumps(
            contract_findings[:3], ensure_ascii=False, sort_keys=True
        )
        return _capability_failure(
            presence, "functional discovery smoke", diagnostic
        )
    if entrypoint_findings:
        return _capability_failure(
            presence,
            "functional discovery smoke",
            "; ".join(entrypoint_findings[:3]),
        )

    return CapabilityResult(
        id=presence.id,
        dimension=presence.dimension,
        title=presence.title,
        hard=presence.hard,
        status="pass",
        evidence=presence.evidence,
        missing=[],
        rationale=(
            f"{presence.rationale} The canonical contract validator and "
            "entrypoint smoke both pass."
        ),
    )


def _evaluate_handoff_validation(root: Path) -> CapabilityResult:
    presence = _evaluate_required_files(
        root=root,
        cap_id="handoff_validation",
        dimension="validate",
        title="Handoff validation",
        hard=True,
        required_files=HANDOFF_REQUIRED_FILES,
        rationale=(
            "Handoff-Checks begrenzen unvollstaendige oder unsichere Uebergaben."
        ),
    )
    if presence.status != "pass":
        return presence

    env = os.environ.copy()
    env["PYTHONPATH"] = str(root)
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.agent.validate_handoff",
                "--task-file",
                HANDOFF_TASK_FILE,
                "--handoff-file",
                HANDOFF_VALID_FILE,
            ],
            cwd=root,
            env=env,
            check=False,
            text=True,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _handoff_failure(presence, str(exc))

    try:
        payload = loads_json_strict(completed.stdout)
    except (json.JSONDecodeError, DuplicateKeyError) as exc:
        return _handoff_failure(presence, f"invalid JSON output: {exc}")

    expected_output = (
        completed.returncode == 0
        and isinstance(payload, dict)
        and payload.get("status") == "valid"
        and payload.get("findings_count") == 0
        and payload.get("findings") == []
        and payload.get("task_file") == HANDOFF_TASK_FILE
        and payload.get("handoff_file") == HANDOFF_VALID_FILE
    )
    if not expected_output:
        diagnostic = completed.stderr or completed.stdout
        return _handoff_failure(presence, diagnostic or "unexpected CLI result")

    return CapabilityResult(
        id=presence.id,
        dimension=presence.dimension,
        title=presence.title,
        hard=presence.hard,
        status="pass",
        evidence=presence.evidence,
        missing=[],
        rationale=(
            f"{presence.rationale} Required files and the canonical CLI smoke "
            "both pass."
        ),
    )


def _repository_state_bytes(root: Path) -> bytes:
    return run_task.repository_state_bytes(root)


def _git_head(root: Path) -> str:
    return run_task.resolve_git_head(root)


def _validate_dry_run_payload(
    root: Path, payload: object, *, expected_head: str
) -> str | None:
    if not isinstance(payload, dict):
        return "runner output must be a JSON object"
    if payload.get("mode") != "dry_run":
        return "mode is not dry_run"
    if payload.get("status") != "planned":
        return "status is not planned"
    if payload.get("findings") != []:
        return "findings are not empty"
    if payload.get("repository_unchanged") is not True:
        return "repository_unchanged is not true"

    try:
        task = load_json_strict(root / DRY_RUN_TASK_FILE)
        task_bytes = (root / DRY_RUN_TASK_FILE).read_bytes()
    except (OSError, json.JSONDecodeError, DuplicateKeyError) as exc:
        return f"dry-run task dependency failed: {exc}"
    if not isinstance(task, dict):
        return "dry-run task is not an object"

    expected_digest = hashlib.sha256(task_bytes).hexdigest()
    expected_plan = {
        "allowed_paths": task["allowed_paths"],
        "forbidden_paths": task["forbidden_paths"],
        "delete_allowed": task["delete_allowed"],
        "planned_changed_paths": [],
        "planned_deleted_paths": [],
    }
    expected_accounting = {
        "expected_evidence": task["expected_evidence"],
        "evidence_produced": [],
        "missing_evidence": task["expected_evidence"],
    }
    expected_fields = {
        "task_file": DRY_RUN_TASK_FILE,
        "task_id": task["task_id"],
        "task_contract_sha256": expected_digest,
        "source_revision": expected_head,
        "execution_plan": expected_plan,
        "evidence_accounting": expected_accounting,
    }
    for field, expected in expected_fields.items():
        if payload.get(field) != expected:
            return f"{field} does not match the canonical dry-run task"

    stages = payload.get("stages")
    if not isinstance(stages, list):
        return "stages are missing"
    if [
        item.get("name") for item in stages if isinstance(item, dict)
    ] != run_task.STAGE_NAMES:
        return "stage sequence does not match the runner contract"
    if any(
        not isinstance(item, dict) or item.get("status") != "passed" for item in stages
    ):
        return "not all dry-run stages passed"

    handoff = payload.get("handoff")
    if not isinstance(handoff, dict):
        return "handoff is missing"
    if handoff.get("outcome") != "incomplete":
        return "handoff outcome is not incomplete"
    if handoff.get("source_revision") != expected_head:
        return "handoff source_revision does not match Git HEAD"
    if handoff.get("task_id") != task["task_id"]:
        return "handoff task_id does not match the canonical dry-run task"
    if handoff.get("task_contract_sha256") != expected_digest:
        return "handoff task digest does not match the canonical dry-run task"

    expected_validation_results = [
        {"command": command, "status": "not_run"}
        for command in task["validation_commands"]
    ]
    if handoff.get("validation_results") != expected_validation_results:
        return "handoff validation_results do not match the canonical task"

    try:
        registry, parser_findings, parser_exit = validate_claim_registry.load_registry(
            root / "docs/claims/registry.yml"
        )
    except (OSError, json.JSONDecodeError, DuplicateKeyError) as exc:
        return f"dry-run validation dependency failed: {exc}"
    if parser_exit != 0 or registry is None:
        return f"claim registry could not be loaded: {parser_findings}"
    findings = validate_handoff(
        task,
        handoff,
        task_bytes=task_bytes,
        repo_root=root,
        claim_registry=registry,
    )
    if findings:
        return f"handoff validator findings: {findings[:3]}"
    return None


def _evaluate_dry_run_runner(root: Path) -> CapabilityResult:
    presence = _evaluate_required_files(
        root=root,
        cap_id="dry_run_runner",
        dimension="plan",
        title="Dry-run runner",
        hard=True,
        required_files=DRY_RUN_REQUIRED_FILES,
        rationale="Dry-Run Runner prueft Agentenpfade ohne schreibende Seiteneffekte.",
    )
    if presence.status != "pass":
        return presence

    env = os.environ.copy()
    env["PYTHONPATH"] = str(root)
    env.setdefault("LC_ALL", "C.UTF-8")
    try:
        expected_head = _git_head(root)
        before = _repository_state_bytes(root)
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.agent.run_task",
                "--dry-run",
                "--no-persist",
                DRY_RUN_TASK_FILE,
            ],
            cwd=root,
            env=env,
            check=False,
            text=True,
            capture_output=True,
            timeout=20,
        )
        after = _repository_state_bytes(root)
    except (
        OSError,
        RuntimeError,
        run_task.RunnerError,
        subprocess.TimeoutExpired,
    ) as exc:
        return _capability_failure(presence, "functional dry-run smoke", str(exc))

    if before != after:
        return _capability_failure(
            presence,
            "functional dry-run smoke",
            "Git-visible repository content changed during smoke",
        )
    try:
        payload = loads_json_strict(completed.stdout)
    except (json.JSONDecodeError, DuplicateKeyError) as exc:
        return _capability_failure(
            presence,
            "functional dry-run smoke",
            f"invalid JSON output: {exc}",
        )
    if completed.returncode != 0 or completed.stderr:
        return _capability_failure(
            presence,
            "functional dry-run smoke",
            completed.stderr or f"unexpected exit code {completed.returncode}",
        )
    payload_error = _validate_dry_run_payload(
        root, payload, expected_head=expected_head
    )
    if payload_error is not None:
        return _capability_failure(
            presence,
            "functional dry-run smoke",
            payload_error,
        )

    return CapabilityResult(
        id=presence.id,
        dimension=presence.dimension,
        title=presence.title,
        hard=presence.hard,
        status="pass",
        evidence=presence.evidence,
        missing=[],
        rationale=(
            f"{presence.rationale} Required files and the canonical dry-run "
            "smoke both pass."
        ),
    )


def _evaluate_run_evidence_lite(root: Path) -> CapabilityResult:
    presence = _evaluate_required_files(
        root=root,
        cap_id="run_evidence_lite",
        dimension="validate",
        title="Run evidence lite",
        hard=True,
        required_files=RUN_EVIDENCE_REQUIRED_FILES,
        rationale=(
            "Erfolgreiche geplante Dry-Runs muessen ein schema-valides, "
            "task- und revisionsgebundenes Evidenzbuendel atomar publizieren."
        ),
    )
    if presence.status != "pass":
        return presence

    env = os.environ.copy()
    env["PYTHONPATH"] = str(root)
    env.setdefault("LC_ALL", "C.UTF-8")
    try:
        before = _repository_state_bytes(root)
        expected_head = _git_head(root)
        expected_state_sha256 = hashlib.sha256(before).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run-evidence"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scripts.agent.run_task",
                    "--dry-run",
                    "--output-dir",
                    str(target),
                    DRY_RUN_TASK_FILE,
                ],
                cwd=root,
                env=env,
                check=False,
                text=True,
                capture_output=True,
                timeout=20,
            )
            after = _repository_state_bytes(root)
            if completed.returncode != 0 or completed.stderr:
                return _capability_failure(
                    presence,
                    "functional run-evidence smoke",
                    completed.stderr or f"unexpected exit code {completed.returncode}",
                )
            if before != after:
                return _capability_failure(
                    presence,
                    "functional run-evidence smoke",
                    "Git-visible repository content changed during smoke",
                )
            payload = loads_json_strict(completed.stdout)
            expected_files = set(run_task.EVIDENCE_FILES)
            actual_files = {item.name for item in target.iterdir()}
            if actual_files != expected_files:
                return _capability_failure(
                    presence,
                    "functional run-evidence smoke",
                    f"unexpected bundle files: {sorted(actual_files)}",
                )
            task_bytes = (root / DRY_RUN_TASK_FILE).read_bytes()
            if (target / "task.yml").read_bytes() != task_bytes:
                return _capability_failure(
                    presence,
                    "functional run-evidence smoke",
                    "task.yml does not preserve the exact task bytes",
                )
            validation = load_json_strict(target / "validation.json")
            run_result = load_json_strict(target / "run-result.json")
            validation_schema = load_json_strict(
                root / "contracts/agent/validation.schema.json"
            )
            run_result_schema = load_json_strict(
                root / "contracts/agent/run-result.schema.json"
            )
            validation_findings = validate_instance(validation, validation_schema)
            result_findings = validate_instance(run_result, run_result_schema)
            if validation_findings or result_findings:
                return _capability_failure(
                    presence,
                    "functional run-evidence smoke",
                    f"schema findings: {validation_findings + result_findings}",
                )
            if not isinstance(payload, dict) or payload.get("run_id") != run_result.get(
                "run_id"
            ):
                return _capability_failure(
                    presence,
                    "functional run-evidence smoke",
                    "stdout and persisted run IDs do not match",
                )
            if (
                run_result.get("task_contract_sha256")
                != hashlib.sha256(task_bytes).hexdigest()
            ):
                return _capability_failure(
                    presence,
                    "functional run-evidence smoke",
                    "run result is not bound to the exact task bytes",
                )
            repository_state = run_result.get("repository_state")
            if not isinstance(repository_state, dict):
                return _capability_failure(
                    presence,
                    "functional run-evidence smoke",
                    "repository state binding is missing",
                )
            if (
                repository_state.get("source_revision") != expected_head
                or run_result.get("source_revision") != expected_head
                or validation.get("source_revision") != expected_head
            ):
                return _capability_failure(
                    presence,
                    "functional run-evidence smoke",
                    "source revision binding does not match current Git HEAD",
                )
            if (
                repository_state.get("git_visible_sha256") != expected_state_sha256
                or run_result.get("repository_state_sha256") != expected_state_sha256
                or validation.get("repository_state_sha256") != expected_state_sha256
            ):
                return _capability_failure(
                    presence,
                    "functional run-evidence smoke",
                    "repository state binding does not match the observed state",
                )
            if run_result.get("outcome") != run_result.get("handoff", {}).get(
                "outcome"
            ):
                return _capability_failure(
                    presence,
                    "functional run-evidence smoke",
                    "run outcome is not bound to the handoff outcome",
                )
            started_at = run_result.get("started_at")
            completed_at = run_result.get("completed_at")
            if (
                not isinstance(started_at, str)
                or not isinstance(completed_at, str)
                or started_at > completed_at
            ):
                return _capability_failure(
                    presence,
                    "functional run-evidence smoke",
                    "run chronology is missing or invalid",
                )
            artifacts = run_result.get("artifacts")
            if not isinstance(artifacts, dict):
                return _capability_failure(
                    presence,
                    "functional run-evidence smoke",
                    "run-result artifacts must be an object",
                )
            for artifact_name in ("task", "handoff", "validation"):
                artifact = artifacts.get(artifact_name)
                if not isinstance(artifact, dict):
                    return _capability_failure(
                        presence,
                        "functional run-evidence smoke",
                        f"missing artifact record: {artifact_name}",
                    )
                artifact_path = target / artifact["path"]
                if (
                    hashlib.sha256(artifact_path.read_bytes()).hexdigest()
                    != artifact["sha256"]
                ):
                    return _capability_failure(
                        presence,
                        "functional run-evidence smoke",
                        f"artifact hash mismatch: {artifact['path']}",
                    )
    except (
        OSError,
        RuntimeError,
        json.JSONDecodeError,
        DuplicateKeyError,
        UnsupportedSchemaError,
        run_task.RunnerError,
        subprocess.TimeoutExpired,
    ) as exc:
        return _capability_failure(presence, "functional run-evidence smoke", str(exc))

    return CapabilityResult(
        id=presence.id,
        dimension=presence.dimension,
        title=presence.title,
        hard=presence.hard,
        status="pass",
        evidence=presence.evidence,
        missing=[],
        rationale=(
            f"{presence.rationale} Required files and the functional persistence "
            "smoke both pass."
        ),
    )


def _as_rel(root: Path, path: Path) -> str:
    return str(path.relative_to(root)).replace("\\", "/")


def _files_for_patterns(root: Path, patterns: Iterable[str]) -> list[str]:
    matches: set[str] = set()
    for pattern in patterns:
        for path in root.glob(pattern):
            if path.is_file():
                matches.add(_as_rel(root, path))
    return sorted(matches)


def _files_for_regex(root: Path, search_roots: Iterable[str], regex: str) -> list[str]:
    matcher = re.compile(regex)
    matches: list[str] = []
    for rel_root in search_roots:
        base = root / rel_root
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            rel = _as_rel(root, path)
            if matcher.search(rel.lower()):
                matches.append(rel)
    return sorted(set(matches))


def _evaluate_required_files(
    root: Path,
    cap_id: str,
    dimension: str,
    title: str,
    hard: bool,
    required_files: list[str],
    rationale: str,
) -> CapabilityResult:
    evidence: list[str] = []
    missing: list[str] = []
    for rel in required_files:
        path = root / rel
        if path.exists() and not path.is_file():
            return CapabilityResult(
                id=cap_id,
                dimension=dimension,
                title=title,
                hard=hard,
                status="fail",
                evidence=evidence,
                missing=[rel],
                rationale=f"{rationale} Expected file path resolves to non-file artifact.",
            )
        if path.is_file():
            evidence.append(rel)
        else:
            missing.append(rel)

    if not missing:
        status = "pass"
    elif evidence:
        status = "partial"
    else:
        status = "open"

    return CapabilityResult(
        id=cap_id,
        dimension=dimension,
        title=title,
        hard=hard,
        status=status,
        evidence=evidence,
        missing=missing,
        rationale=rationale,
    )

DIMENSIONS = [
    "discover",
    "understand",
    "plan",
    "workspace",
    "write",
    "validate",
    "review",
    "publish",
    "deploy",
    "cleanup",
    "recovery",
]
DIMENSION_ORDER = {dimension: index for index, dimension in enumerate(DIMENSIONS)}


def _dimension_rank(result: CapabilityResult) -> int:
    try:
        return DIMENSION_ORDER[result.dimension]
    except KeyError as exc:
        raise ValueError(
            f"Unknown capability dimension for {result.id}: {result.dimension}"
        ) from exc

def _external_capability(cap_id: str, dimension: str, title: str) -> CapabilityResult:
    return CapabilityResult(
        id=cap_id,
        dimension=dimension,
        title=title,
        hard=False,
        status="open",
        evidence=[],
        missing=["external_operator_execution"],
        rationale="Capability resides with the external operator (Grabowski).",
    )


def evaluate_capabilities(repo_root: Path) -> list[CapabilityResult]:
    results: list[CapabilityResult] = []

    results.append(_evaluate_discovery_contract(repo_root))

    results.append(
        _evaluate_required_files(
            root=repo_root,
            cap_id="agent_policy",
            dimension="understand",
            title="Agent policy baseline",
            hard=False,
            required_files=["AGENTS.md", "agent-policy.yaml"],
            rationale="Agenten brauchen dokumentierte Grenzen und Schreibregeln.",
        )
    )

    results.append(
        _evaluate_required_files(
            root=repo_root,
            cap_id="agent_contracts",
            dimension="understand",
            title="Agent contracts",
            hard=True,
            required_files=["contracts/agent/task.schema.json"],
            rationale="Contracts definieren maschinenlesbare Agent-Task-Grenzen.",
        )
    )

    results.append(_evaluate_dry_run_runner(repo_root))

    results.append(_external_capability("workspace", "workspace", "Workspace Management"))
    results.append(_external_capability("write", "write", "Code Mutation"))

    results.append(
        _evaluate_required_files(
            root=repo_root,
            cap_id="safety_preflight",
            dimension="validate",
            title="Safety preflight guard",
            hard=False,
            required_files=[
                "scripts/agent/check_agent_preflight.py",
                "scripts/agent/tests/test_check_agent_preflight.py",
                ".github/workflows/agent-safety-preflight.yml",
                "docs/security/agent-write-scope-baseline.md",
            ],
            rationale="Report-only Preflight schafft belastbare Baseline vor Blocking.",
        )
    )

    results.append(
        _evaluate_required_files(
            root=repo_root,
            cap_id="claim_evidence_spine",
            dimension="validate",
            title="Claim evidence spine",
            hard=True,
            required_files=[
                "docs/claims/registry.yml",
                "scripts/docmeta/validate_claim_registry.py",
            ],
            rationale="Ohne Claim-Registry und Validator fehlt maschinenlesbare Evidenzbindung.",
        )
    )

    results.append(_evaluate_handoff_validation(repo_root))

    results.append(
        _evaluate_required_files(
            root=repo_root,
            cap_id="non_ideal_guard",
            dimension="validate",
            title="Non-ideal guard",
            hard=True,
            required_files=[
                "scripts/agent/check_non_ideal_task.py",
                "scripts/agent/tests/test_check_non_ideal_task.py",
            ],
            rationale="Non-Ideal-Guard erkennt riskante Ausnahmefaelle vor Ausfuehrung.",
        )
    )

    results.append(_evaluate_run_evidence_lite(repo_root))

    results.append(_external_capability("review", "review", "Review and Approval"))
    results.append(_external_capability("publish", "publish", "Publish Artifacts"))
    results.append(_external_capability("deploy", "deploy", "Deploy Changes"))
    results.append(_external_capability("cleanup", "cleanup", "Cleanup Workspaces"))
    results.append(_external_capability("recovery", "recovery", "State Recovery"))

    for result in results:
        if result.status not in VALID_STATUSES:
            raise ValueError(f"Invalid status for {result.id}: {result.status}")

    return results


def determine_overall_status(
    results: list[CapabilityResult],
) -> tuple[str, str, list[str]]:
    hard_gaps = [r.id for r in results if r.hard and r.status != "pass"]
    failing = [r.id for r in results if r.status == "fail"]

    if failing:
        reason = f"Inconsistent capability state detected: {', '.join(failing)}"
        return "fail", reason, hard_gaps

    if hard_gaps:
        reason = f"Hard capabilities are still missing: {', '.join(hard_gaps)}"
        return "not_execution_ready", reason, hard_gaps

    if all(r.status in ["pass", "open"] for r in results):
        if any(r.status == "pass" for r in results):
            return (
                "plan_ready",
                "Repository logic supports planning and validation. Execution relies on external operator.",
                [],
            )
        else:
            return (
                "open",
                "No capability evidence detected yet.",
                [r.id for r in results if r.hard],
            )

    return "partial", "Capabilities are partially implemented.", hard_gaps


def render_report(
    results: list[CapabilityResult],
    overall: str,
    reason: str,
    hard_gaps: list[str],
) -> str:
    lines: list[str] = []
    lines.append("---")
    lines.append("id: docs.generated.agent-readiness")
    lines.append("title: Agent Readiness")
    lines.append("doc_type: generated")
    lines.append("status: active")
    lines.append("summary: Deterministische Agent-Readiness-Matrix.")
    lines.append("---")
    lines.append("")
    lines.append("## Weltgewebe Agent Readiness")
    lines.append("")
    lines.append("Generated automatically. Do not edit.")
    lines.append("")
    lines.append("## Overall Status")
    lines.append("")
    lines.append(f"- **Overall:** {overall}")
    lines.append(f"- **Reason:** {reason}")
    lines.append("")
    lines.append("## Capability Matrix")
    lines.append("")
    lines.append("| Dimension | Capability | Status | Hard | Evidence | Missing | Rationale |")
    lines.append("|---|---|---|---:|---|---|---|")

    handoff_evidence: list[str] = []

    # Sort results by DIMENSIONS order
    sorted_results = sorted(results, key=_dimension_rank)

    for result in sorted_results:
        if result.id == "handoff_validation":
            handoff_evidence = result.evidence
            evidence = "See Handoff Evidence"
        else:
            evidence = (
                ", ".join(f"`{item}`" for item in result.evidence)
                if result.evidence
                else "-"
            )
        missing = (
            ", ".join(f"`{item}`" for item in result.missing) if result.missing else "-"
        )
        hard = "yes" if result.hard else "no"
        lines.append(
            f"| {result.dimension} | {result.id} | {result.status} | {hard} | {evidence} | "
            f"{missing} | {result.rationale} |"
        )

    lines.append("")
    lines.append("## Handoff Evidence")
    lines.append("")
    if handoff_evidence:
        lines.extend(f"- `{item}`" for item in handoff_evidence)
    else:
        lines.append("- No handoff evidence detected.")

    lines.append("")
    lines.append("## Residual Gaps")
    lines.append("")
    if hard_gaps:
        for capability in hard_gaps:
            lines.append(f"- Hard capability missing: {capability}")
    else:
        lines.append("- No residual hard repository gaps detected.")

    lines.append("")
    lines.append("## External Operator Dependencies")
    lines.append("")
    external_dependencies = [
        result
        for result in sorted_results
        if result.status == "open"
        and "external_operator_execution" in result.missing
    ]
    if external_dependencies:
        for result in external_dependencies:
            lines.append(
                f"- `{result.dimension}` / `{result.id}`: "
                "provided by the external operator (Grabowski), not by this repository."
            )
    else:
        lines.append("- No external operator dependencies detected.")

    lines.append("")
    lines.append("## Interpretation Rule")
    lines.append("")
    lines.append("Dieser Report ist diagnostisch. Er aktiviert keinen Blocking-Mode.")
    lines.append(
        "`pass` bezeichnet nur die read-only Contract- und Planungsfaehigkeit "
        "der Agent-Safety-Schicht. Es bestaetigt keine Task-Ausfuehrung, "
        "keine Run-Attestierung, keine Patch-Anwendung, keinen Write Mode und "
        "keine autonome Merge-Faehigkeit."
    )
    lines.append("")
    return "\n".join(lines)


def _expected_content(root: Path) -> str:
    results = evaluate_capabilities(root)
    overall, reason, hard_missing = determine_overall_status(results)
    return render_report(results, overall, reason, hard_missing)


def generate(repo_root: str | Path | None = None) -> Path:
    root = Path(repo_root) if repo_root is not None else Path(REPO_ROOT)
    out_file = root / "docs" / "_generated" / "agent-readiness.md"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(_expected_content(root), encoding="utf-8")
    return out_file


def check(repo_root: str | Path | None = None) -> list[str]:
    root = Path(repo_root) if repo_root is not None else Path(REPO_ROOT)
    out_file = root / "docs" / "_generated" / "agent-readiness.md"
    actual = out_file.read_text(encoding="utf-8") if out_file.is_file() else None
    return [] if actual == _expected_content(root) else [str(out_file.relative_to(root))]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the agent-readiness report")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the committed report without writing it",
    )
    args = parser.parse_args(argv)
    try:
        if args.check:
            drift = check()
            if drift:
                print(
                    "Agent readiness drift detected in: " + ", ".join(drift),
                    file=sys.stderr,
                )
                print(
                    "Run: python3 -m scripts.docmeta.generate_agent_readiness",
                    file=sys.stderr,
                )
                return 1
            print("Agent readiness is up to date.")
            return 0
        out_file = generate()
        print(f"Generated {out_file}")
        return 0
    except Exception as exc:
        print(f"Error generating agent readiness: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
