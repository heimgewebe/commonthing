"""
Backlinks Generator — builds a reverse-reference graph from document relations.

Uses the centralized relations parser (no duplicate parsing logic).

Output: docs/_generated/backlinks.md
"""

import os
import argparse
import sys
from collections import defaultdict

from scripts.docmeta.docmeta import REPO_ROOT
from scripts.docmeta.generated_check import write_or_check
from scripts.docmeta.relations_parser import (
    collect_file_relations,
)

OUT_FILE = os.path.join(REPO_ROOT, "docs", "_generated", "backlinks.md")

HEADER = """\
---
id: docs.generated.backlinks
title: Backlinks Graph
doc_type: generated
status: active
summary: Automatisch generierter Graph der Rückverweise.
---

## Weltgewebe Backlinks

Generated automatically. Do not edit.

"""


def render_backlinks() -> str:

    file_relations = collect_file_relations(["docs"], REPO_ROOT)

    backlinks = defaultdict(list)
    for source_path, rels in file_relations.items():
        for entry in rels:
            if isinstance(entry, dict):
                rel_type = entry.get("type", "")
                target = entry.get("target", "")
                if rel_type and target:
                    backlinks[target].append((source_path, rel_type))

    if not backlinks:
        body = "_No relations found._\n"
    else:
        blocks = []
        for target in sorted(backlinks.keys()):
            block_lines = [f"## {target}\n"]
            for source, rel in sorted(backlinks[target]):
                block_lines.append(f"- [{rel}] {source}")
            blocks.append("\n".join(block_lines))
        body = "\n\n".join(blocks) + "\n"
    return HEADER + body


def generate_backlinks() -> None:
    rc = write_or_check(OUT_FILE, render_backlinks(), label=os.path.relpath(OUT_FILE, REPO_ROOT))
    if rc:
        raise SystemExit(rc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="compare output without rewriting it")
    args = parser.parse_args(argv)
    return write_or_check(
        OUT_FILE,
        render_backlinks(),
        check=args.check,
        label=os.path.relpath(OUT_FILE, REPO_ROOT),
    )


if __name__ == "__main__":
    sys.exit(main())
