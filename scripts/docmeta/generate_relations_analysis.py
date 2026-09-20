"""
Relations Analysis Generator — semantic graph analysis of document relations.

Read-only analysis that makes relation quality visible:
1. Cycle detection in depends_on chains
2. Hub detection (high inbound/outbound counts)
3. Isolated documents (no inbound AND no outbound relations)
4. Type distribution statistics
5. Semantic warnings (heuristic, non-blocking)

Output: docs/_generated/relations-analysis.md
"""

import argparse
import os
import sys
import tempfile
from collections import defaultdict

from scripts.docmeta.docmeta import REPO_ROOT
from scripts.docmeta.generated_check import write_or_check
from scripts.docmeta.relations_parser import extract_relations_from_content

# Thresholds for heuristic warnings
HUB_OUTBOUND_THRESHOLD = 8
HUB_INBOUND_THRESHOLD = 10


def _raise_walk_error(error):
    raise error


def collect_relations_graph():
    """
    Walk all docs/*.md (excluding _generated) and build the relations graph.

    Returns:
        edges: list of (source, type, target) tuples
        all_docs: set of all doc paths found
    """
    docs_dir = os.path.join(REPO_ROOT, "docs")
    edges = []
    all_docs = set()

    for root, dirs, files in os.walk(docs_dir, onerror=_raise_walk_error):
        dirs[:] = sorted(directory for directory in dirs if directory != "_generated")
        for file in sorted(files):
            if not file.endswith(".md"):
                continue
            abs_path = os.path.join(root, file)
            rel_path = os.path.relpath(abs_path, REPO_ROOT)
            all_docs.add(rel_path)

            with open(abs_path, "r", encoding="utf-8") as f:
                content = f.read()

            relations = extract_relations_from_content(content)
            for rel in relations:
                if not isinstance(rel, dict):
                    continue
                rel_type = rel.get("type", "")
                target = rel.get("target", "")
                if rel_type and target:
                    edges.append((rel_path, rel_type, target))

    return edges, all_docs


def find_cycles(edges):
    """
    Detect cycles in depends_on edges using iterative DFS.

    Returns:
        list of cycles, each cycle is a list of node paths forming a loop
    """
    # Build adjacency list for depends_on only
    graph = defaultdict(list)
    for source, rel_type, target in edges:
        if rel_type == "depends_on":
            graph[source].append(target)

    visited = set()
    in_stack = set()
    cycles = []

    for start_node in graph:
        if start_node in visited:
            continue

        # Iterative DFS with explicit stack
        stack = [(start_node, [start_node], 0)]
        while stack:
            node, path, idx = stack.pop()

            neighbors = graph.get(node, [])
            if idx == 0:
                if node in in_stack:
                    # Found cycle — extract it
                    if node in path[:-1]:
                        cycle_start = path.index(node)
                        cycles.append(path[cycle_start:])
                    continue
                if node in visited:
                    continue
                in_stack.add(node)

            found_next = False
            for i in range(idx, len(neighbors)):
                neighbor = neighbors[i]
                if neighbor in in_stack:
                    # Cycle detected
                    cycle_path = path + [neighbor]
                    cycle_start = cycle_path.index(neighbor)
                    cycles.append(cycle_path[cycle_start:])
                elif neighbor not in visited:
                    # Push continuation point, then explore neighbor
                    stack.append((node, path, i + 1))
                    stack.append((neighbor, path + [neighbor], 0))
                    found_next = True
                    break

            if not found_next:
                in_stack.discard(node)
                visited.add(node)

    return cycles


def compute_degree_stats(edges, all_docs):
    """
    Compute inbound and outbound degree for each document.

    Returns:
        dict: {doc_path: {"outbound": int, "inbound": int, "outbound_by_type": {}, "inbound_by_type": {}}}
    """
    stats = {
        doc: {
            "outbound": 0,
            "inbound": 0,
            "outbound_by_type": defaultdict(int),
            "inbound_by_type": defaultdict(int),
        }
        for doc in all_docs
    }

    for source, rel_type, target in edges:
        if (s := stats.get(source)) is not None:
            s["outbound"] += 1
            s["outbound_by_type"][rel_type] += 1
        if (t := stats.get(target)) is not None:
            t["inbound"] += 1
            t["inbound_by_type"][rel_type] += 1

    return stats


def find_isolated_docs(stats):
    """Find documents with zero inbound AND zero outbound relations."""
    isolated = []
    for doc, s in sorted(stats.items()):
        if s["outbound"] == 0 and s["inbound"] == 0:
            # Skip index/README files — they are structural, not relational
            basename = os.path.basename(doc)
            if basename in ("index.md", "README.md"):
                continue
            isolated.append(doc)
    return isolated


def find_hubs(stats):
    """Find documents with high outbound or inbound relation counts."""
    outbound_hubs = []
    inbound_hubs = []

    for doc, s in sorted(stats.items()):
        if s["outbound"] >= HUB_OUTBOUND_THRESHOLD:
            outbound_hubs.append((doc, s["outbound"]))
        if s["inbound"] >= HUB_INBOUND_THRESHOLD:
            inbound_hubs.append((doc, s["inbound"]))

    outbound_hubs.sort(key=lambda x: -x[1])
    inbound_hubs.sort(key=lambda x: -x[1])
    return outbound_hubs, inbound_hubs


def compute_type_distribution(edges):
    """Count relations by type."""
    dist = defaultdict(int)
    for _, rel_type, _ in edges:
        dist[rel_type] += 1
    return dict(dist)


def generate_warnings(edges, stats, cycles):
    """Generate semantic warnings (heuristic, non-blocking)."""
    warnings = []

    # Cycle warnings
    for cycle in cycles:
        chain = " → ".join(cycle)
        warnings.append(f"⚠️ depends_on cycle: {chain}")

    # Hub warnings
    outbound_hubs, inbound_hubs = find_hubs(stats)
    for doc, count in outbound_hubs:
        warnings.append(f"⚠️ High outbound count ({count}): `{doc}` — possible over-linking")
    for doc, count in inbound_hubs:
        warnings.append(f"⚠️ High inbound count ({count}): `{doc}` — central dependency, review carefully")

    # Supersession chain check: supersedes without deprecated status on target
    # (light heuristic — just flag for human review)
    supersession_targets = set()
    for source, rel_type, target in edges:
        if rel_type == "supersedes":
            supersession_targets.add(target)

    return warnings


def write_output(edges, all_docs, stats, cycles, warnings, out_file=None):
    """Write the relations-analysis.md output file."""
    if out_file is None:
        out_file = os.path.join(REPO_ROOT, "docs", "_generated", "relations-analysis.md")
    os.makedirs(os.path.dirname(out_file), exist_ok=True)

    type_dist = compute_type_distribution(edges)
    isolated = find_isolated_docs(stats)
    outbound_hubs, inbound_hubs = find_hubs(stats)

    docs_with_relations = sum(1 for s in stats.values() if s["outbound"] > 0)
    docs_as_targets = sum(1 for s in stats.values() if s["inbound"] > 0)

    with open(out_file, "w", encoding="utf-8") as f:
        f.write("---\n")
        f.write("id: docs.generated.relations-analysis\n")
        f.write("title: Relations Analysis\n")
        f.write("doc_type: generated\n")
        f.write("status: active\n")
        f.write("summary: Automatische Analyse des Relationsgraphen — Zyklen, Hubs, Isolation, Verteilung.\n")
        f.write("---\n\n")
        f.write("## Weltgewebe Relations Analysis\n\n")
        f.write("Generated automatically. Do not edit.\n\n")

        # Overview
        f.write("### Übersicht\n\n")
        f.write("| Metrik | Wert |\n")
        f.write("| --- | --- |\n")
        f.write(f"| Dokumente gesamt | {len(all_docs)} |\n")
        f.write(f"| Dokumente mit ausgehenden Relationen | {docs_with_relations} |\n")
        f.write(f"| Dokumente als Ziel referenziert | {docs_as_targets} |\n")
        f.write(f"| Relationen gesamt | {len(edges)} |\n")
        for rel_type in sorted(type_dist.keys()):
            f.write(f"| — {rel_type} | {type_dist[rel_type]} |\n")
        f.write(f"| Isolierte Dokumente | {len(isolated)} |\n")
        f.write(f"| depends_on Zyklen | {len(cycles)} |\n")
        f.write("\n")

        # Warnings
        f.write("### Warnungen\n\n")
        f.write("> Heuristische Hinweise — keine CI-Fehler. Zyklen deuten auf zirkuläre Abhängigkeiten, hohe Vernetzung auf zentrale Dokumente, die bei Änderungen besondere Aufmerksamkeit erfordern.\n\n")
        if warnings:
            for w in warnings:
                f.write(f"- {w}\n")
            f.write("\n")
        else:
            f.write("_Keine Warnungen._\n\n")

        # Cycles
        f.write("### Zyklen (depends_on)\n\n")
        if cycles:
            for cycle in cycles:
                chain = " → ".join(f"`{c}`" for c in cycle)
                f.write(f"- {chain}\n")
        else:
            f.write("_Keine Zyklen gefunden._\n")
        f.write("\n")

        # Hubs
        f.write("### Hubs (hohe Vernetzung)\n\n")
        if outbound_hubs or inbound_hubs:
            if outbound_hubs:
                f.write("**Ausgehend (outbound):**\n\n")
                for doc, count in outbound_hubs:
                    f.write(f"- `{doc}` — {count} ausgehende Relationen\n")
                f.write("\n")
            if inbound_hubs:
                f.write("**Eingehend (inbound):**\n\n")
                for doc, count in inbound_hubs:
                    f.write(f"- `{doc}` — {count} eingehende Relationen\n")
                f.write("\n")
        else:
            f.write("_Keine auffälligen Hubs._\n\n")

        # Isolated documents
        f.write("### Isolierte Dokumente\n\n")
        f.write("> Dokumente ohne eingehende und ausgehende Relationen (index.md/README.md ausgenommen).\n\n")
        if isolated:
            for doc in isolated:
                f.write(f"- `{doc}`\n")
        else:
            f.write("_Keine isolierten Dokumente._\n")

    return out_file


def main(argv: list[str] | None = None) -> int:
    """Main entry point for the relations analysis generator."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="compare output without rewriting it")
    args = parser.parse_args(argv)
    try:
        edges, all_docs = collect_relations_graph()
        stats = compute_degree_stats(edges, all_docs)
        cycles = find_cycles(edges)
        warnings = generate_warnings(edges, stats, cycles)
        target = os.path.join(REPO_ROOT, "docs", "_generated", "relations-analysis.md")
        if args.check:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp:
                tmp_path = tmp.name
            try:
                write_output(edges, all_docs, stats, cycles, warnings, out_file=tmp_path)
                with open(tmp_path, "r", encoding="utf-8") as f:
                    content = f.read()
            finally:
                try:
                    os.unlink(tmp_path)
                except FileNotFoundError:
                    pass
            return write_or_check(target, content, check=True, label=os.path.relpath(target, REPO_ROOT))
        out_file = write_output(edges, all_docs, stats, cycles, warnings)
        print(f"Generated {out_file}")
        return 0
    except Exception as e:
        print(f"Error generating relations analysis: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
