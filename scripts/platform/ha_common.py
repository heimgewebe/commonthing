from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml

import kind_reference as ref

ROOT = Path(__file__).resolve().parents[2]

def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

def wait_until(
    description: str,
    probe,
    *,
    timeout_seconds: int = 600,
    interval: float = 2.0,
    retry_on_probe_error: bool = True,
):
    deadline = time.monotonic() + timeout_seconds
    last: Any = None
    while time.monotonic() < deadline:
        try:
            last = probe()
            if last:
                return last
        except (subprocess.CalledProcessError, json.JSONDecodeError, ref.ProofError):
            if not retry_on_probe_error:
                raise
        time.sleep(interval)
    raise ref.ProofError(f"timed out waiting for {description}; last={last!r}")

def _load_yaml_documents(source: str, release_name: str) -> list[Any]:
    try:
        documents = [
            document
            for document in yaml.safe_load_all(source)
            if document is not None
        ]
    except yaml.YAMLError as error:
        raise ref.ProofError(f"{release_name} is not valid YAML") from error
    if not documents:
        raise ref.ProofError(f"{release_name} contains no YAML documents")
    return documents

def current_primary(kubectl: str, cluster: str = "postgres-ha") -> str:
    return ref.output([kubectl, "-n", "weltgewebe-data", "get", f"cluster/{cluster}", "-o", "jsonpath={.status.currentPrimary}"])

def psql(kubectl: str, sql: str, *, cluster: str = "postgres-ha") -> str:
    primary = current_primary(kubectl, cluster)
    if not primary:
        raise ref.ProofError(f"PostgreSQL cluster {cluster} has no current primary")
    return ref.output([kubectl, "-n", "weltgewebe-data", "exec", primary, "--", "psql", "-d", "weltgewebe", "-Atqc", sql])
