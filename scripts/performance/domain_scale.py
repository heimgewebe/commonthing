#!/usr/bin/env python3
"""Generate and verify production-shaped Weltgewebe PostgreSQL scale fixtures.

The tool deliberately operates on a dedicated benchmark schema. It never writes
or truncates the public domain tables.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = Path("configs/performance/domain-scale.v1.json")
DOMAIN_DB_SOURCE = ROOT / "apps/api/src/domain_db.rs"
BENCHMARK_SCHEMA = "weltgewebe_perf"
SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
PSQL_SAFE_PATH_RE = re.compile(r"^/[A-Za-z0-9_./-]+$")
NODE_KINDS = ("Ort", "Projekt", "Person", "Ressource", "Gespraech")
RARE_NODE_KIND = "Projekt"
COMMON_NODE_KINDS = tuple(kind for kind in NODE_KINDS if kind != RARE_NODE_KIND)
RARE_NODE_KIND_INTERVAL = 100
EDGE_KINDS = ("bezieht_sich_auf", "wirkt_mit", "liegt_bei", "teilt", "antwortet_auf")
WORKLOAD_ORDER = (
    "node_cursor",
    "edge_cursor",
    "bbox_initial",
    "bbox_cursor",
    "outbound_neighbors",
    "inbound_neighbors",
    "kind_filter",
)
NODE_HEADER = ("id", "kind", "title", "lat", "lon", "created_at", "updated_at", "payload")
EDGE_HEADER = ("id", "source_id", "target_id", "edge_kind", "created_at", "payload")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class DomainScaleError(RuntimeError):
    """Raised when a scale fixture or query plan violates its contract."""


def canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _csv_data_row_count(path: Path, expected_header: Sequence[str]) -> int:
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            try:
                header = next(reader)
            except StopIteration as exc:
                raise DomainScaleError(f"fixture CSV is empty: {path}") from exc
            if tuple(header) != tuple(expected_header):
                raise DomainScaleError(f"fixture CSV header mismatch: {path}")
            count = 0
            for row_number, row in enumerate(reader, start=2):
                if len(row) != len(expected_header):
                    raise DomainScaleError(
                        f"fixture CSV row {row_number} has {len(row)} columns, expected {len(expected_header)}: {path}"
                    )
                count += 1
            return count
    except OSError as exc:
        raise DomainScaleError(f"cannot read fixture CSV {path}: {exc}") from exc


def load_config(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DomainScaleError(f"cannot read scale config {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise DomainScaleError("scale config must be a JSON object")
    validate_config(parsed)
    return parsed


def validate_config(config: Mapping[str, Any]) -> None:
    allowed_top = {"schema_version", "seed", "database_schema", "profiles", "plan_budgets"}
    extra = sorted(set(config) - allowed_top)
    if extra:
        raise DomainScaleError(f"unknown scale config keys: {', '.join(extra)}")
    if config.get("schema_version") != 1:
        raise DomainScaleError("schema_version must be 1")
    seed = config.get("seed")
    if not isinstance(seed, str) or not seed or len(seed) > 128:
        raise DomainScaleError("seed must be a non-empty string of at most 128 characters")
    schema = config.get("database_schema")
    if not isinstance(schema, str) or not SCHEMA_RE.fullmatch(schema):
        raise DomainScaleError("database_schema must be a safe lowercase PostgreSQL identifier")
    if schema != BENCHMARK_SCHEMA:
        raise DomainScaleError(
            f"database_schema must be the reserved benchmark schema {BENCHMARK_SCHEMA!r}"
        )

    profiles = config.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise DomainScaleError("profiles must be a non-empty object")
    for name, profile in profiles.items():
        if not isinstance(name, str) or not SCHEMA_RE.fullmatch(name):
            raise DomainScaleError(f"invalid profile name: {name!r}")
        if not isinstance(profile, dict) or set(profile) != {"nodes", "edges"}:
            raise DomainScaleError(f"profile {name} must contain exactly nodes and edges")
        for key in ("nodes", "edges"):
            value = profile[key]
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise DomainScaleError(f"profile {name} {key} must be a positive integer")
        if profile["nodes"] < 2:
            raise DomainScaleError(f"profile {name} must contain at least two nodes")
        if profile["edges"] < profile["nodes"]:
            raise DomainScaleError(f"profile {name} must contain at least one edge per node")

    budgets = config.get("plan_budgets")
    if not isinstance(budgets, dict):
        raise DomainScaleError("plan_budgets must be an object")
    if budgets.get("calibration_required") is not True:
        raise DomainScaleError("calibration_required must remain true until hardware baselines exist")
    enforced_profiles = budgets.get("enforced_profiles")
    if not isinstance(enforced_profiles, list) or not enforced_profiles:
        raise DomainScaleError("enforced_profiles must be a non-empty list")
    if any(not isinstance(name, str) or name not in profiles for name in enforced_profiles):
        raise DomainScaleError("enforced_profiles must reference known profiles")
    if len(set(enforced_profiles)) != len(enforced_profiles):
        raise DomainScaleError("enforced_profiles may not contain duplicates")
    max_temp_blocks = budgets.get("max_temp_blocks")
    if not isinstance(max_temp_blocks, int) or isinstance(max_temp_blocks, bool) or max_temp_blocks < 0:
        raise DomainScaleError("max_temp_blocks must be a non-negative integer")
    workloads = budgets.get("workloads")
    if not isinstance(workloads, dict) or tuple(workloads) != WORKLOAD_ORDER:
        raise DomainScaleError("workloads must list the canonical workload set in canonical order")
    for name, rules in workloads.items():
        expected_rule_keys = {
            "forbidden_node_types",
            "required_index",
            "required_index_cond_identifiers",
        }
        if not isinstance(rules, dict) or set(rules) != expected_rule_keys:
            raise DomainScaleError(
                f"workload {name} must define forbidden_node_types, required_index and required_index_cond_identifiers"
            )
        node_types = rules["forbidden_node_types"]
        if not isinstance(node_types, list) or not node_types:
            raise DomainScaleError(f"workload {name} forbidden_node_types must be non-empty")
        if any(not isinstance(item, str) or not item for item in node_types):
            raise DomainScaleError(f"workload {name} contains an invalid node type")
        required_index = rules["required_index"]
        if not isinstance(required_index, str) or not SCHEMA_RE.fullmatch(required_index):
            raise DomainScaleError(f"workload {name} required_index must be a safe index name")
        condition_tokens = rules["required_index_cond_identifiers"]
        if not isinstance(condition_tokens, list) or not condition_tokens:
            raise DomainScaleError(
                f"workload {name} required_index_cond_identifiers must be a non-empty list"
            )
        if any(not isinstance(item, str) or not item for item in condition_tokens):
            raise DomainScaleError(f"workload {name} contains an invalid index-condition token")


def _digest(seed: str, *parts: object) -> bytes:
    material = "\x1f".join((seed, *(str(part) for part in parts))).encode("utf-8")
    return hashlib.sha256(material).digest()


def _choice(seed: str, values: Sequence[str], *parts: object) -> str:
    return values[int.from_bytes(_digest(seed, *parts)[:8], "big") % len(values)]


def _unit_float(seed: str, *parts: object) -> float:
    return int.from_bytes(_digest(seed, *parts)[:8], "big") / float(1 << 64)


def _node_id(index: int) -> str:
    return f"node-{index:012d}"


def _edge_id(index: int) -> str:
    return f"edge-{index:012d}"


def _timestamp(index: int) -> str:
    day = 1 + (index % 28)
    hour = (index // 28) % 24
    minute = (index // (28 * 24)) % 60
    second = (index // (28 * 24 * 60)) % 60
    return f"2026-01-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}Z"


def _node_rows(seed: str, profile_name: str, count: int) -> Iterable[list[str]]:
    for index in range(count):
        kind = (
            RARE_NODE_KIND
            if index % RARE_NODE_KIND_INTERVAL == 0
            else _choice(seed, COMMON_NODE_KINDS, profile_name, "node-kind", index)
        )
        lat = -85.0 + 170.0 * _unit_float(seed, profile_name, "lat", index)
        lon = -180.0 + 360.0 * _unit_float(seed, profile_name, "lon", index)
        created_at = _timestamp(index)
        payload = {
            "scale_fixture": True,
            "scale_profile": profile_name,
            "summary": f"Deterministic scale node {index}",
            "tags": [kind.lower(), f"bucket-{index % 64:02d}"],
        }
        yield [
            _node_id(index),
            kind,
            f"Scale node {index}",
            f"{lat:.8f}",
            f"{lon:.8f}",
            created_at,
            created_at,
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        ]


def _edge_rows(seed: str, profile_name: str, edge_count: int, node_count: int) -> Iterable[list[str]]:
    for index in range(edge_count):
        if index < node_count:
            source_index = index
            target_index = (index + 1) % node_count
        else:
            source_index = int.from_bytes(
                _digest(seed, profile_name, "edge-source", index)[:8], "big"
            ) % node_count
            target_index = int.from_bytes(
                _digest(seed, profile_name, "edge-target", index)[:8], "big"
            ) % node_count
            if target_index == source_index:
                target_index = (target_index + 1) % node_count
        edge_kind = _choice(seed, EDGE_KINDS, profile_name, "edge-kind", index)
        payload = {
            "scale_fixture": True,
            "scale_profile": profile_name,
            "source_type": "Node",
            "target_type": "Node",
        }
        yield [
            _edge_id(index),
            _node_id(source_index),
            _node_id(target_index),
            edge_kind,
            _timestamp(index),
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        ]


def _atomic_csv(path: Path, header: Sequence[str], rows: Iterable[Sequence[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(header)
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def generate_fixture(config_path: Path, profile_name: str, output_dir: Path) -> dict[str, Any]:
    config = load_config(config_path)
    profiles = config["profiles"]
    if profile_name not in profiles:
        raise DomainScaleError(f"unknown profile {profile_name!r}")
    profile = profiles[profile_name]
    output_dir.mkdir(parents=True, exist_ok=True)
    nodes_path = output_dir / "domain_nodes.csv"
    edges_path = output_dir / "domain_edges.csv"
    manifest_path = output_dir / "manifest.json"

    _atomic_csv(
        nodes_path,
        NODE_HEADER,
        _node_rows(config["seed"], profile_name, profile["nodes"]),
    )
    _atomic_csv(
        edges_path,
        EDGE_HEADER,
        _edge_rows(config["seed"], profile_name, profile["edges"], profile["nodes"]),
    )

    manifest = {
        "schema_version": 1,
        "generator": "scripts/performance/domain_scale.py",
        "config_sha256": sha256_file(config_path),
        "database_schema": config["database_schema"],
        "profile": profile_name,
        "counts": {"nodes": profile["nodes"], "edges": profile["edges"]},
        "files": {
            "nodes": {"name": nodes_path.name, "sha256": sha256_file(nodes_path)},
            "edges": {"name": edges_path.name, "sha256": sha256_file(edges_path)},
        },
    }
    _atomic_json(manifest_path, manifest)
    return manifest


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DomainScaleError(f"cannot read fixture manifest {path}: {exc}") from exc
    expected_keys = {
        "schema_version",
        "generator",
        "config_sha256",
        "database_schema",
        "profile",
        "counts",
        "files",
    }
    if not isinstance(manifest, dict) or set(manifest) != expected_keys:
        raise DomainScaleError("fixture manifest must contain the canonical schema_version 1 fields")
    if manifest.get("schema_version") != 1:
        raise DomainScaleError("fixture manifest must use schema_version 1")
    if manifest.get("generator") != "scripts/performance/domain_scale.py":
        raise DomainScaleError("fixture manifest contains an unknown generator")
    config_sha256 = manifest.get("config_sha256")
    if not isinstance(config_sha256, str) or not SHA256_RE.fullmatch(config_sha256):
        raise DomainScaleError("fixture manifest contains an invalid config_sha256")
    schema = manifest.get("database_schema")
    if (
        not isinstance(schema, str)
        or not SCHEMA_RE.fullmatch(schema)
        or schema != BENCHMARK_SCHEMA
    ):
        raise DomainScaleError("fixture manifest must use the reserved benchmark schema")
    profile = manifest.get("profile")
    if not isinstance(profile, str) or not SCHEMA_RE.fullmatch(profile):
        raise DomainScaleError("fixture manifest contains an invalid profile")
    counts = manifest.get("counts")
    if not isinstance(counts, dict) or set(counts) != {"nodes", "edges"}:
        raise DomainScaleError("fixture manifest contains invalid counts")
    for key in ("nodes", "edges"):
        value = counts.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise DomainScaleError("fixture manifest contains invalid counts")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != {"nodes", "edges"}:
        raise DomainScaleError("fixture manifest contains invalid files")
    expected_files = {
        "nodes": (NODE_HEADER, counts["nodes"]),
        "edges": (EDGE_HEADER, counts["edges"]),
    }
    for key, (header, expected_count) in expected_files.items():
        entry = files.get(key)
        if not isinstance(entry, dict) or set(entry) != {"name", "sha256"}:
            raise DomainScaleError(f"fixture manifest contains an invalid {key} file entry")
        name = entry["name"]
        digest = entry["sha256"]
        if not isinstance(name, str) or Path(name).name != name:
            raise DomainScaleError(f"fixture manifest {key} filename must be local")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise DomainScaleError(f"fixture manifest {key} sha256 is invalid")
        file_path = path.parent / name
        if not file_path.is_file():
            raise DomainScaleError(f"fixture file is missing: {file_path}")
        if sha256_file(file_path) != digest:
            raise DomainScaleError(f"fixture file hash mismatch: {file_path}")
        actual_count = _csv_data_row_count(file_path, header)
        if actual_count != expected_count:
            raise DomainScaleError(
                f"fixture CSV row count mismatch for {key}: manifest={expected_count}, actual={actual_count}"
            )
    return manifest


def bind_manifest_to_config(
    manifest: Mapping[str, Any], config: Mapping[str, Any], config_sha256: str
) -> None:
    validate_config(config)
    if manifest.get("config_sha256") != config_sha256:
        raise DomainScaleError("fixture manifest was generated from another scale config")
    profile_name = manifest.get("profile")
    if profile_name not in config["profiles"]:
        raise DomainScaleError(f"fixture manifest references unknown profile {profile_name!r}")
    expected_counts = config["profiles"][profile_name]
    if manifest.get("counts") != expected_counts:
        raise DomainScaleError(
            f"fixture manifest counts do not match configured profile {profile_name!r}"
        )


def load_bound_manifest(path: Path, config_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    config = load_config(config_path)
    manifest = load_manifest(path)
    bind_manifest_to_config(manifest, config, sha256_file(config_path))
    return config, manifest


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _psql_meta_path_literal(path: Path) -> str:
    value = str(path.resolve())
    if not PSQL_SAFE_PATH_RE.fullmatch(value):
        raise DomainScaleError(
            "psql file paths must be absolute and contain only ASCII letters, digits, '/', '.', '_' or '-'"
        )
    return _sql_literal(value)


def render_load_sql(
    manifest_path: Path, output_path: Path, config_path: Path = DEFAULT_CONFIG
) -> None:
    _, manifest = load_bound_manifest(manifest_path, config_path)
    schema = manifest["database_schema"]
    nodes_path = (manifest_path.parent / manifest["files"]["nodes"]["name"]).resolve()
    edges_path = (manifest_path.parent / manifest["files"]["edges"]["name"]).resolve()
    sql = f"""\\set ON_ERROR_STOP on
BEGIN;
DROP SCHEMA IF EXISTS {schema} CASCADE;
CREATE SCHEMA {schema};
CREATE TABLE {schema}.domain_nodes (LIKE public.domain_nodes INCLUDING ALL);
CREATE TABLE {schema}.domain_edges (LIKE public.domain_edges INCLUDING ALL);

-- The scale foundation intentionally creates only the early domain tables.
-- Later production columns used by measured queries must still exist in the
-- isolated benchmark schema so the exact production SQL can execute unchanged.
ALTER TABLE {schema}.domain_nodes
    ADD COLUMN IF NOT EXISTS search_visibility TEXT NOT NULL DEFAULT 'public';

\\copy {schema}.domain_nodes (id, kind, title, lat, lon, created_at, updated_at, payload) FROM {_psql_meta_path_literal(nodes_path)} WITH (FORMAT csv, HEADER true)
\\copy {schema}.domain_edges (id, source_id, target_id, edge_kind, created_at, payload) FROM {_psql_meta_path_literal(edges_path)} WITH (FORMAT csv, HEADER true)

ANALYZE {schema}.domain_nodes;
ANALYZE {schema}.domain_edges;

SELECT 'nodes' AS relation, count(*) AS rows FROM {schema}.domain_nodes
UNION ALL
SELECT 'edges' AS relation, count(*) AS rows FROM {schema}.domain_edges
ORDER BY relation;
COMMIT;
"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(sql, encoding="utf-8", newline="\n")


def _production_bbox_sql(function_name: str) -> str:
    """Read the exact SQL literal used by one production Rust BBOX loader."""

    try:
        source = DOMAIN_DB_SOURCE.read_text(encoding="utf-8")
    except OSError as exc:
        raise DomainScaleError(f"cannot read production domain SQL source {DOMAIN_DB_SOURCE}: {exc}") from exc
    marker = f"pub async fn {function_name}("
    start = source.find(marker)
    if start < 0:
        raise DomainScaleError(f"production BBOX loader not found: {function_name}")
    next_function = source.find("\npub async fn ", start + len(marker))
    block = source[start : next_function if next_function >= 0 else len(source)]
    match = re.search(r'sqlx::query_as\(\s*"(?P<sql>.*?)"\s*,\s*\)', block, flags=re.DOTALL)
    if match is None:
        raise DomainScaleError(f"production BBOX SQL literal not found: {function_name}")
    raw = re.sub(r"\\\r?\n\s*", " ", match.group("sql"))
    if "\\" in raw:
        raise DomainScaleError(f"unsupported escape in production BBOX SQL literal: {function_name}")
    sql = " ".join(raw.split())
    if sql.count("FROM domain_nodes") != 1:
        raise DomainScaleError(f"production BBOX SQL must reference domain_nodes exactly once: {function_name}")
    return sql


def _render_production_bbox_sql(
    function_name: str, schema: str, bindings: Mapping[int, str]
) -> str:
    query = _production_bbox_sql(function_name).replace(
        "FROM domain_nodes", f"FROM {schema}.domain_nodes", 1
    )
    placeholders = {int(value) for value in re.findall(r"\$(\d+)", query)}
    if placeholders != set(bindings):
        raise DomainScaleError(
            f"production BBOX placeholder contract drifted for {function_name}: "
            f"expected {sorted(bindings)}, observed {sorted(placeholders)}"
        )

    def replace_binding(match: re.Match[str]) -> str:
        return bindings[int(match.group(1))]

    return re.sub(r"\$(\d+)", replace_binding, query)


def _explain_block(name: str, plan_dir: Path, query: str) -> str:
    plan_path = (plan_dir / f"{name}.json").resolve()
    return (
        f"\\o {_psql_meta_path_literal(plan_path)}\n"
        f"EXPLAIN (ANALYZE, BUFFERS, WAL, FORMAT JSON) {query};\n"
        "\\o\n"
    )


def render_workload_sql(
    manifest_path: Path,
    plan_dir: Path,
    output_path: Path,
    config_path: Path = DEFAULT_CONFIG,
) -> None:
    _, manifest = load_bound_manifest(manifest_path, config_path)
    schema = manifest["database_schema"]
    node_count = manifest["counts"]["nodes"]
    edge_count = manifest["counts"]["edges"]
    middle_node = _node_id(node_count // 2)
    middle_edge = _edge_id(edge_count // 2)
    neighbor_node = _node_id(node_count // 3)
    plan_dir.mkdir(parents=True, exist_ok=True)
    bbox_initial_query = _render_production_bbox_sql(
        "load_nodes_bbox_from_postgres",
        schema,
        {1: "-10.0", 2: "10.0", 3: "-10.0", 4: "10.0", 5: "1001", 6: "0", 7: "NULL"},
    )
    bbox_cursor_query = _render_production_bbox_sql(
        "load_nodes_bbox_after_id_from_postgres",
        schema,
        {
            1: "-10.0",
            2: "10.0",
            3: "-10.0",
            4: "10.0",
            5: _sql_literal(middle_node),
            6: "1001",
            7: "NULL",
        },
    )
    blocks = [
        _explain_block(
            "node_cursor",
            plan_dir,
            f"SELECT id, kind, title, lat, lon FROM {schema}.domain_nodes WHERE id > {_sql_literal(middle_node)} ORDER BY id ASC LIMIT 1000",
        ),
        _explain_block(
            "edge_cursor",
            plan_dir,
            f"SELECT id, source_id, target_id, edge_kind FROM {schema}.domain_edges WHERE id > {_sql_literal(middle_edge)} ORDER BY id ASC LIMIT 1000",
        ),
        _explain_block("bbox_initial", plan_dir, bbox_initial_query),
        _explain_block("bbox_cursor", plan_dir, bbox_cursor_query),
        _explain_block(
            "outbound_neighbors",
            plan_dir,
            f"SELECT id, source_id, target_id, edge_kind FROM {schema}.domain_edges WHERE source_id = {_sql_literal(neighbor_node)} LIMIT 5000",
        ),
        _explain_block(
            "inbound_neighbors",
            plan_dir,
            f"SELECT id, source_id, target_id, edge_kind FROM {schema}.domain_edges WHERE target_id = {_sql_literal(neighbor_node)} LIMIT 5000",
        ),
        _explain_block(
            "kind_filter",
            plan_dir,
            f"SELECT id, kind, title FROM {schema}.domain_nodes WHERE kind = 'Projekt' LIMIT 500",
        ),
    ]
    sql = "\\set ON_ERROR_STOP on\n\\pset format unaligned\n\\pset tuples_only on\n" + "\n".join(blocks)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(sql, encoding="utf-8", newline="\n")


def _plan_nodes(plan: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    yield plan
    children = plan.get("Plans", [])
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                yield from _plan_nodes(child)


def _plan_node_types(plan: Mapping[str, Any]) -> list[str]:
    return [
        node_type
        for node in _plan_nodes(plan)
        if isinstance((node_type := node.get("Node Type")), str)
    ]


def _sql_condition_identifiers(condition: str) -> set[str]:
    """Return exact SQL identifiers outside single-quoted string literals."""

    without_literals: list[str] = []
    index = 0
    in_literal = False
    while index < len(condition):
        char = condition[index]
        if char == "'":
            if in_literal and index + 1 < len(condition) and condition[index + 1] == "'":
                without_literals.extend((" ", " "))
                index += 2
                continue
            in_literal = not in_literal
            without_literals.append(" ")
        elif in_literal:
            without_literals.append(" ")
        else:
            without_literals.append(char)
        index += 1
    if in_literal:
        return set()
    return {
        match.group(0).lower()
        for match in re.finditer(r"[A-Za-z_][A-Za-z0-9_]*", "".join(without_literals))
    }


def _index_evidence(
    plan: Mapping[str, Any], required_index: str, condition_identifiers: Sequence[str]
) -> list[dict[str, Any]]:
    required_identifiers = {identifier.lower() for identifier in condition_identifiers}
    evidence: list[dict[str, Any]] = []
    for node in _plan_nodes(plan):
        if node.get("Index Name") != required_index:
            continue
        condition = node.get("Index Cond")
        if not isinstance(condition, str) or not condition.strip():
            continue
        actual_loops = node.get("Actual Loops")
        if (
            not isinstance(actual_loops, (int, float))
            or isinstance(actual_loops, bool)
            or actual_loops <= 0
        ):
            continue
        identifiers = _sql_condition_identifiers(condition)
        if not required_identifiers.issubset(identifiers):
            continue
        evidence.append(
            {
                "node_type": str(node.get("Node Type", "")),
                "index_name": required_index,
                "index_cond": condition,
                "index_cond_identifiers": sorted(identifiers),
                "actual_loops": actual_loops,
            }
        )
    return evidence


def _temp_blocks(plan: Mapping[str, Any]) -> int:
    total = 0
    for node in _plan_nodes(plan):
        for key in ("Temp Read Blocks", "Temp Written Blocks"):
            value = node.get(key, 0)
            if isinstance(value, int) and not isinstance(value, bool):
                total += value
    return total


def _read_explain(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DomainScaleError(f"cannot read PostgreSQL plan {path}: {exc}") from exc
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise DomainScaleError(f"PostgreSQL plan {path} must contain one FORMAT JSON result")
    document = payload[0]
    if not isinstance(document.get("Plan"), dict):
        raise DomainScaleError(f"PostgreSQL plan {path} has no root Plan")
    return document


def check_plans(
    config: Mapping[str, Any], plan_dir: Path, profile_name: str
) -> dict[str, Any]:
    validate_config(config)
    if profile_name not in config["profiles"]:
        raise DomainScaleError(f"unknown plan profile {profile_name!r}")
    budgets = config["plan_budgets"]
    budget_enforced = profile_name in budgets["enforced_profiles"]
    max_temp_blocks = budgets["max_temp_blocks"]
    results: dict[str, Any] = {}
    failures: list[str] = []
    for workload in WORKLOAD_ORDER:
        path = plan_dir / f"{workload}.json"
        document = _read_explain(path)
        plan = document["Plan"]
        rules = budgets["workloads"][workload]
        node_types = _plan_node_types(plan)
        forbidden = rules["forbidden_node_types"]
        present_forbidden = sorted(set(node_types).intersection(forbidden))
        required_index = rules["required_index"]
        index_evidence = _index_evidence(
            plan, required_index, rules["required_index_cond_identifiers"]
        )
        temp_blocks = _temp_blocks(plan)
        if budget_enforced and present_forbidden:
            failures.append(
                f"{workload}: forbidden PostgreSQL plan nodes: {', '.join(present_forbidden)}"
            )
        if budget_enforced and not index_evidence:
            failures.append(
                f"{workload}: required index {required_index} with a matching Index Cond was not executed"
            )
        if budget_enforced and temp_blocks > max_temp_blocks:
            failures.append(
                f"{workload}: temporary blocks {temp_blocks} exceed budget {max_temp_blocks}"
            )
        results[workload] = {
            "node_types": node_types,
            "required_index": required_index,
            "required_index_evidence": index_evidence,
            "temp_blocks": temp_blocks,
            "planning_time_ms": document.get("Planning Time"),
            "execution_time_ms": document.get("Execution Time"),
        }
    return {
        "schema_version": 1,
        "status": "fail" if failures else "pass",
        "profile": profile_name,
        "budget_enforced": budget_enforced,
        "calibration_required": budgets["calibration_required"],
        "failures": failures,
        "workloads": results,
    }


def _write_report(path: Path | None, report: Mapping[str, Any]) -> None:
    if path is not None:
        _atomic_json(path, report)
    sys.stdout.buffer.write(canonical_json_bytes(report))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("validate", help="validate the scale contract")

    generate = subparsers.add_parser("generate", help="generate deterministic CSV fixtures")
    generate.add_argument("--profile", required=True)
    generate.add_argument("--output-dir", type=Path, required=True)

    load_sql = subparsers.add_parser("render-load", help="render isolated PostgreSQL load SQL")
    load_sql.add_argument("--manifest", type=Path, required=True)
    load_sql.add_argument("--output", type=Path, required=True)

    workload_sql = subparsers.add_parser("render-workload", help="render EXPLAIN workload SQL")
    workload_sql.add_argument("--manifest", type=Path, required=True)
    workload_sql.add_argument("--plan-dir", type=Path, required=True)
    workload_sql.add_argument("--output", type=Path, required=True)

    check = subparsers.add_parser("check", help="check PostgreSQL FORMAT JSON plans")
    check.add_argument("--manifest", type=Path, required=True)
    check.add_argument("--plan-dir", type=Path, required=True)
    check.add_argument("--report", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "validate":
            _write_report(None, {"schema_version": 1, "status": "pass", "config": str(args.config)})
        elif args.command == "generate":
            manifest = generate_fixture(args.config, args.profile, args.output_dir)
            _write_report(None, manifest)
        elif args.command == "render-load":
            render_load_sql(args.manifest, args.output, args.config)
            _write_report(None, {"schema_version": 1, "status": "pass", "output": str(args.output)})
        elif args.command == "render-workload":
            render_workload_sql(args.manifest, args.plan_dir, args.output, args.config)
            _write_report(None, {"schema_version": 1, "status": "pass", "output": str(args.output)})
        elif args.command == "check":
            manifest = load_manifest(args.manifest)
            bind_manifest_to_config(manifest, config, sha256_file(args.config))
            report = check_plans(config, args.plan_dir, manifest["profile"])
            _write_report(args.report, report)
            if report["status"] != "pass":
                print("domain-scale: " + "; ".join(report["failures"]), file=sys.stderr)
                return 2
        else:
            parser.error(f"unsupported command {args.command}")
    except DomainScaleError as exc:
        print(f"domain-scale: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
