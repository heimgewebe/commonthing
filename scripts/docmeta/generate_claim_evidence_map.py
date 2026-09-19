#!/usr/bin/env python3
"""Generate the deterministic Claim Evidence Map from the Lenskit bridge registry.

Reads ``docs/doc-freshness-registry.yml`` (Lenskit bridge form) and renders:

* ``docs/_generated/claim-evidence-map.md`` (human navigable overview)

This generator is report-only. It surfaces the claim -> evidence linkage and the
static registry metadata. It does NOT prove any claim true and it does NOT
compute wall-clock freshness status.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.docmeta.docmeta import REPO_ROOT
from scripts.docmeta.validate_doc_freshness_registry import load_yaml_json, run_validation

REGISTRY_REL = "docs/doc-freshness-registry.yml"
MARKDOWN_REL = "docs/_generated/claim-evidence-map.md"
GENERATOR_REL = "scripts/docmeta/generate_claim_evidence_map.py"


def _claim_id_from_locator(locator: object) -> str:
    if isinstance(locator, str):
        prefix = "claims[id="
        if locator.startswith(prefix) and locator.endswith("]"):
            return locator[len(prefix):-1]
    return "UNKNOWN"


def _build_entries(data: object) -> list[dict[str, object]]:
    raw_entries = data.get("entries", []) if isinstance(data, dict) else []
    entries: list[dict[str, object]] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            continue
        evidence = raw.get("evidence") or []
        evidence_count = sum(1 for item in evidence if isinstance(item, dict))
        valid_evidence = [
            {"kind": item.get("kind"), "target": item.get("target")}
            for item in evidence
            if isinstance(item, dict)
        ]
        entries.append(
            {
                "id": raw.get("id"),
                "claim_id": _claim_id_from_locator(raw.get("locator")),
                "doc": raw.get("doc"),
                "locator": raw.get("locator"),
                "status": raw.get("status"),
                "owner": raw.get("owner"),
                "last_verified": raw.get("last_verified"),
                "evidence_count": evidence_count,
                "evidence": valid_evidence,
            }
        )

    entries.sort(key=lambda entry: str(entry.get("id") or ""))
    return entries


def render_markdown(data: object) -> str:
    entries = _build_entries(data)
    does_not_prove = data.get("does_not_prove", []) if isinstance(data, dict) else []
    lines: list[str] = []
    lines.append("---")
    lines.append("id: docs.generated.claim-evidence-map")
    lines.append("title: Claim Evidence Map")
    lines.append("doc_type: generated")
    lines.append("status: active")
    lines.append("summary: Automatisch generierte Claim-Evidence-Map (Lenskit Bridge).")
    lines.append("---")
    lines.append("")
    lines.append("# Claim Evidence Map")
    lines.append("")
    lines.append("Generated automatically. Do not edit.")
    lines.append("")
    lines.append("| id | doc | locator | status | owner | last_verified | evidence |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for entry in entries:
        lines.append(
            f"| {entry['id']} | {entry['doc']} | {entry['locator']} "
            f"| {entry['status']} | {entry['owner']} | {entry['last_verified']} "
            f"| {entry['evidence_count']} items |"
        )
    lines.append("")

    if entries:
        lines.append("## Details")
        lines.append("")
        for entry in entries:
            claim_id = str(entry.get("claim_id") or "UNKNOWN")
            lines.append(f"### {claim_id}")
            lines.append("")
            lines.append(f"- Entry: `{entry['id']}`")
            lines.append(f"- Locator: `{entry['locator']}`")
            lines.append(f"- Status: `{entry['status']}`")
            lines.append(f"- Owner: `{entry['owner']}`")
            lines.append(f"- Last verified: `{entry['last_verified']}`")
            lines.append("")
            lines.append("Evidence:")
            lines.append("")
            lines.append("| Kind | Target |")
            lines.append("| ---- | ------ |")
            for ev in entry.get("evidence", []):
                kind = str(ev["kind"]) if ev.get("kind") is not None else ""
                target = str(ev["target"]) if ev.get("target") is not None else ""
                lines.append(f"| `{kind}` | `{target}` |")
            if does_not_prove and isinstance(does_not_prove, list):
                lines.append("")
                lines.append("Does not prove:")
                lines.append("")
                for item in does_not_prove:
                    lines.append(f"- {item}")
            lines.append("")

    return "\n".join(lines)


def _load_registry(root: Path) -> object:
    data, error = load_yaml_json(root / REGISTRY_REL)
    if error is not None:
        raise ValueError(error)
    return data


def _validate_freshness_registry_or_raise(root: Path) -> None:
    """Refuse to generate reports from a structurally invalid freshness registry."""
    output, exit_code = run_validation(repo_root=root)
    if exit_code == 0:
        return

    findings = output.get("findings", []) if isinstance(output, dict) else []
    preview_parts: list[str] = []
    if isinstance(findings, list):
        for finding in findings[:5]:
            if isinstance(finding, dict):
                code = finding.get("code", "?")
                entry_id = finding.get("entry_id", "-")
                preview_parts.append(f"{code}:{entry_id}")

    preview = ", ".join(preview_parts) or "see validator output"
    count = output.get("findings_count", len(preview_parts)) if isinstance(output, dict) else "unknown"
    raise ValueError(f"Freshness registry validation failed ({count} findings): {preview}")


def generate(repo_root: str | Path | None = None) -> Path:
    root = Path(repo_root) if repo_root is not None else Path(REPO_ROOT)
    _validate_freshness_registry_or_raise(root)
    data = _load_registry(root)

    md_content = render_markdown(data)

    md_path = root / MARKDOWN_REL
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(md_content, encoding="utf-8")
    return md_path


def check(repo_root: str | Path | None = None) -> list[str]:
    """Return the list of output paths that drift from the registry (empty = ok)."""
    root = Path(repo_root) if repo_root is not None else Path(REPO_ROOT)
    _validate_freshness_registry_or_raise(root)
    data = _load_registry(root)

    expected_md = render_markdown(data)

    drift: list[str] = []
    md_path = root / MARKDOWN_REL

    actual_md = md_path.read_text(encoding="utf-8") if md_path.exists() else None

    if actual_md != expected_md:
        drift.append(MARKDOWN_REL)
    return drift


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the claim evidence map")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify generated outputs match the registry without writing files",
    )
    args = parser.parse_args(argv)

    if args.check:
        try:
            _validate_freshness_registry_or_raise(Path(REPO_ROOT))
            drift = check()
        except ValueError as exc:
            print(f"Error reading freshness registry: {exc}", file=sys.stderr)
            return 1
        if drift:
            print("Claim evidence map drift detected in: " + ", ".join(drift), file=sys.stderr)
            print("Run: python3 -m scripts.docmeta.generate_claim_evidence_map", file=sys.stderr)
            return 1
        print("Claim evidence map is up to date.")
        return 0

    try:
        _validate_freshness_registry_or_raise(Path(REPO_ROOT))
        generate()
    except ValueError as exc:
        print(f"Error generating claim evidence map: {exc}", file=sys.stderr)
        return 1
    print(f"Generated {MARKDOWN_REL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
