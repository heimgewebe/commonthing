"""Planning ownership ratchet.

Active planning artifacts should bind directly to a work owner with a non-empty
``owner_task`` frontmatter field. Existing task-control/roadmap registrations
remain accepted as a migration-only compatibility fallback.

The legacy registration checker is intentionally reused instead of copied. This
keeps historical board/index semantics stable while allowing new planning work
to stop growing the repository-local shadow control plane.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from scripts.docmeta import check_planning_registration as legacy

_OWNER_TASK_RE = re.compile(r"^owner_task:\s*(.*?)\s*$", re.MULTILINE)


def _owner_task(text: str) -> str | None:
    """Return the explicit owner_task value from frontmatter, if non-empty."""
    if not text or not text.startswith("---"):
        return None
    parts = text.split("\n---", 1)
    if len(parts) < 2:
        return None
    match = _OWNER_TASK_RE.search(parts[0][3:])
    if match is None:
        return None
    value = match.group(1).strip().strip("\"'")
    return value or None


def run_checks(config=None):
    """Check explicit planning ownership with legacy registration fallback."""
    if config is None:
        config, _ = legacy.load_config()

    registered_paths, control_errors = legacy.get_registered_paths(config)
    artifacts = legacy.get_all_planning_artifacts(config)
    findings = []

    # Compatibility control files still need to be readable while any existing
    # planning artifacts depend on them. A later migration slice can remove
    # these checks once all active artifacts have explicit owner_task bindings.
    for code, path, reason in control_errors:
        findings.append(
            {
                "code": code,
                "path": path,
                "reason": reason,
                "suggestion": "Keep the legacy compatibility source readable until its remaining planning artifacts have explicit owner_task bindings.",
                "source": "planning-ownership",
            }
        )

    terminal = set(
        config.get("terminal_statuses", legacy._DEFAULT_CONFIG["terminal_statuses"])
    )

    for rel_path in artifacts:
        text, err = legacy._read_text(rel_path)
        if err:
            findings.append(
                {
                    "code": "FILE_READ_ERROR",
                    "path": rel_path,
                    "reason": err,
                    "suggestion": "Ensure the file exists and is readable.",
                    "source": "planning-ownership",
                }
            )
            continue

        meta = legacy._parse_scalars(text)
        relations = legacy._get_relations(text)

        if not legacy.is_planning_doc(rel_path, meta, config):
            continue
        if meta.get("status") in terminal:
            continue
        if _owner_task(text) is not None:
            continue
        if legacy.is_registered(rel_path, registered_paths, meta, relations, config):
            continue

        findings.append(
            {
                "code": "UNOWNED_PLANNING_ARTIFACT",
                "path": rel_path,
                "reason": "Active planning artifact has neither an explicit owner_task binding nor a legacy task-control/roadmap registration.",
                "suggestion": (
                    "Add a non-empty frontmatter owner_task binding (preferred; work authority stays external). "
                    "During migration only, the existing docs/tasks board/index or docs/roadmap registration remains accepted as a compatibility fallback."
                ),
                "source": "planning-ownership",
            }
        )

    return findings


def _emit_text(findings, mode):
    if not findings:
        print("Planning ownership check passed (0 issues).")
        return
    print(f"\n--- Planning Ownership Drift ({len(findings)}) ---", file=sys.stderr)
    for finding in findings:
        if mode == "warn":
            print(
                f"::warning file={finding['path']}::[{finding['code']}] {finding['reason']}"
            )
        else:
            print(f"[{finding['code']}] {finding['path']}", file=sys.stderr)
            print(f"  Reason: {finding['reason']}", file=sys.stderr)
            print(f"  Fix:    {finding.get('suggestion', '')}\n", file=sys.stderr)
    print(f"Check finished with {len(findings)} issue(s).", file=sys.stderr)


def _emit_json(findings, mode):
    ordered = sorted(findings, key=lambda item: (item.get("path", ""), item.get("code", "")))
    print(
        json.dumps(
            {
                "findings": ordered,
                "finding_count": len(ordered),
                "format": "json",
                "mode": mode,
                "ok": not ordered,
            },
            sort_keys=True,
            indent=2,
        )
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Require explicit planning ownership with legacy registration fallback."
    )
    parser.add_argument(
        "--mode",
        choices=["report", "warn", "strict"],
        default="report",
        help="report (default, exit 0), warn (GH annotations, exit 0), strict (exit 1 on findings).",
    )
    parser.add_argument("--strict", action="store_true", help="Alias for --mode strict.")
    parser.add_argument(
        "--format",
        dest="fmt",
        choices=["text", "json"],
        default="text",
    )
    args = parser.parse_args(argv)
    mode = "strict" if args.strict else args.mode

    config, config_finding = legacy.load_config()
    findings = run_checks(config)
    if config_finding:
        config_finding = {**config_finding, "source": "planning-ownership"}
        findings = [config_finding] + findings

    if args.fmt == "json":
        _emit_json(findings, mode)
    else:
        _emit_text(findings, mode)
    return 1 if mode == "strict" and findings else 0


if __name__ == "__main__":
    sys.exit(main())
