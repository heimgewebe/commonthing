"""Planning ownership ratchet.

Active planning artifacts should bind directly to the external Bureau work plane with a
canonical ``BUREAU-*`` ``owner_task`` frontmatter field. Existing task-control/roadmap
registrations remain accepted as a migration-only compatibility fallback.

The legacy registration checker is intentionally reused instead of copied. This keeps
historical board/index semantics stable while allowing new planning work to stop growing
the repository-local shadow control plane.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from scripts.docmeta import check_planning_registration as legacy

# Deliberately horizontal whitespace only. ``\s`` would also consume newlines and could
# turn an empty ``owner_task:`` into the following frontmatter key.
_OWNER_TASK_RE = re.compile(r"^owner_task:[ \t]*(.*?)[ \t]*\r?$", re.MULTILINE)
_EXTERNAL_OWNER_TASK_RE = re.compile(r"^BUREAU-[A-Za-z0-9._:-]+$")


def _owner_task(text: str) -> str | None:
    """Return the top-level owner_task scalar from frontmatter, if non-empty."""
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


def _is_external_owner_task(value: str | None) -> bool:
    """True only for the explicit Bureau-owned external task namespace."""
    return value is not None and _EXTERNAL_OWNER_TASK_RE.fullmatch(value) is not None


def _legacy_control_findings(control_errors):
    """Project legacy control errors only while an active artifact needs the fallback."""
    return [
        {
            "code": code,
            "path": path,
            "reason": reason,
            "suggestion": (
                "Keep this legacy compatibility source readable only while remaining "
                "active planning artifacts still depend on local registration."
            ),
            "source": "planning-ownership",
        }
        for code, path, reason in control_errors
    ]


def run_checks(config=None):
    """Check explicit Bureau ownership with legacy registration fallback."""
    if config is None:
        config, _ = legacy.load_config()

    registered_paths, control_errors = legacy.get_registered_paths(config)
    artifacts = legacy.get_all_planning_artifacts(config)
    findings = []

    legacy_paths_raw = config.get("legacy_fallback_paths")
    if (
        not isinstance(legacy_paths_raw, list)
        or any(not isinstance(path, str) or not path for path in legacy_paths_raw)
        or len(set(legacy_paths_raw)) != len(legacy_paths_raw)
    ):
        return [{
            "code": "LEGACY_FALLBACK_CONFIG_INVALID",
            "path": "scripts/docmeta/planning_registration.yml",
            "reason": "legacy_fallback_paths must be an explicit unique list of repository paths.",
            "suggestion": "Declare the finite migration inventory; do not infer or auto-grow it from board/index/roadmap.",
            "source": "planning-ownership",
        }]
    legacy_paths = set(legacy_paths_raw)
    legacy_dependency_present = False

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
        if _is_external_owner_task(_owner_task(text)):
            continue
        if rel_path in legacy_paths:
            legacy_dependency_present = True
            if legacy.is_registered(rel_path, registered_paths, meta, relations, config):
                continue

        findings.append(
            {
                "code": "UNOWNED_PLANNING_ARTIFACT",
                "path": rel_path,
                "reason": (
                    "Active planning artifact has neither a canonical external BUREAU-* "
                    "owner_task binding nor an allowlisted legacy migration registration."
                ),
                "suggestion": (
                    "Add a canonical BUREAU-* frontmatter owner_task binding (preferred; "
                    "work authority stays external). During migration only, the existing "
                    "docs/tasks board/index or docs/roadmap registration remains accepted "
                    "only for paths already listed in legacy_fallback_paths."
                ),
                "source": "planning-ownership",
            }
        )

    # Compatibility sources are required only while a currently active, non-external
    # artifact belongs to the explicit migration inventory. New registrations cannot
    # enlarge that inventory and therefore cannot grow the shadow control plane.
    if legacy_dependency_present and control_errors:
        findings = _legacy_control_findings(control_errors) + findings

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
    ordered = sorted(
        findings, key=lambda item: (item.get("path", ""), item.get("code", ""))
    )
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
    parser.add_argument(
        "--strict", action="store_true", help="Alias for --mode strict."
    )
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
