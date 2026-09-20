#!/usr/bin/env python3
"""Validate the exact hash-bound Python dependency set for agent tooling."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT_REL = "tools/py/pyproject.toml"
LOCK_REL = "tools/py/uv.lock"
EXPECTED_PACKAGE = "pyyaml"
EXPECTED_VERSION = "6.0.2"
EXPECTED_DEPENDENCY = "PyYAML==6.0.2"
EXPECTED_PYTEST_PACKAGE = "pytest"
EXPECTED_PYTEST_VERSION = "9.0.3"
EXPECTED_PYTEST_DEPENDENCY = "pytest==9.0.3"
EXPECTED_DEPENDENCIES = (EXPECTED_DEPENDENCY, EXPECTED_PYTEST_DEPENDENCY)
EXPECTED_LOCK_VERSION = 1
EXPECTED_LOCK_REVISION = 3
EXPECTED_ARTIFACT_COUNT = 28
EXPECTED_ARTIFACT_SET_SHA256 = (
    "9f33626f8cbf4de0bfe3237d88489b983a7f2097703c2ac16a6b22c5ff7a55e1"
)
HASH_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _finding(code: str, path: str, message: str) -> dict[str, str]:
    return {"code": code, "path": path, "message": message}


def _load_toml(path: Path) -> dict[str, Any]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("TOML root must be an object")
    return data


def _artifact_identity(artifact: dict[str, Any]) -> dict[str, Any] | None:
    url = artifact.get("url")
    digest = artifact.get("hash")
    size = artifact.get("size")
    if not isinstance(url, str) or not url:
        return None
    if not isinstance(digest, str) or HASH_PATTERN.fullmatch(digest) is None:
        return None
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        return None
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "files.pythonhosted.org":
        return None
    return {"url": url, "hash": digest, "size": size}


def _artifact_set_sha256(artifacts: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        sorted(artifacts, key=lambda item: item["url"]),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_tooling_lock(repo_root: Path) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    pyproject_path = repo_root / PYPROJECT_REL
    lock_path = repo_root / LOCK_REL

    try:
        pyproject = _load_toml(pyproject_path)
        lock = _load_toml(lock_path)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        return [_finding("AGENT_TOOLING_LOCK_PARSE", LOCK_REL, str(exc))]

    project = pyproject.get("project")
    dependencies = project.get("dependencies") if isinstance(project, dict) else None
    if not isinstance(dependencies, list):
        findings.append(
            _finding(
                "AGENT_TOOLING_DEPENDENCY_PIN",
                f"{PYPROJECT_REL}:project.dependencies",
                "project.dependencies must be a list",
            )
        )
        dependencies = []
    for required in EXPECTED_DEPENDENCIES:
        if required not in dependencies:
            findings.append(
                _finding(
                    "AGENT_TOOLING_DEPENDENCY_PIN",
                    f"{PYPROJECT_REL}:project.dependencies",
                    f"required exact dependency is missing: {required}",
                )
            )

    if lock.get("version") != EXPECTED_LOCK_VERSION:
        findings.append(
            _finding(
                "AGENT_TOOLING_LOCK_FORMAT",
                f"{LOCK_REL}:version",
                f"expected lock version {EXPECTED_LOCK_VERSION}",
            )
        )
    if lock.get("revision") != EXPECTED_LOCK_REVISION:
        findings.append(
            _finding(
                "AGENT_TOOLING_LOCK_FORMAT",
                f"{LOCK_REL}:revision",
                f"expected lock revision {EXPECTED_LOCK_REVISION}",
            )
        )

    packages = lock.get("package")
    if not isinstance(packages, list):
        findings.append(
            _finding(
                "AGENT_TOOLING_LOCK_PACKAGE",
                f"{LOCK_REL}:package",
                "package entries must be a list",
            )
        )
        return findings

    matching = [
        package
        for package in packages
        if isinstance(package, dict) and package.get("name") == EXPECTED_PACKAGE
    ]
    if len(matching) != 1:
        findings.append(
            _finding(
                "AGENT_TOOLING_LOCK_PACKAGE",
                f"{LOCK_REL}:package",
                f"expected exactly one {EXPECTED_PACKAGE} package entry",
            )
        )
        return findings

    package = matching[0]
    if package.get("version") != EXPECTED_VERSION:
        findings.append(
            _finding(
                "AGENT_TOOLING_LOCK_VERSION",
                f"{LOCK_REL}:package.{EXPECTED_PACKAGE}.version",
                f"expected {EXPECTED_PACKAGE} {EXPECTED_VERSION}",
            )
        )

    pytest_matching = [
        entry
        for entry in packages
        if isinstance(entry, dict) and entry.get("name") == EXPECTED_PYTEST_PACKAGE
    ]
    if len(pytest_matching) != 1:
        findings.append(
            _finding(
                "AGENT_TOOLING_LOCK_PACKAGE",
                f"{LOCK_REL}:package",
                f"expected exactly one {EXPECTED_PYTEST_PACKAGE} package entry",
            )
        )
    elif pytest_matching[0].get("version") != EXPECTED_PYTEST_VERSION:
        findings.append(
            _finding(
                "AGENT_TOOLING_LOCK_VERSION",
                f"{LOCK_REL}:package.{EXPECTED_PYTEST_PACKAGE}.version",
                f"expected {EXPECTED_PYTEST_PACKAGE} {EXPECTED_PYTEST_VERSION}",
            )
        )

    raw_artifacts: list[Any] = [package.get("sdist")]
    wheels = package.get("wheels")
    if isinstance(wheels, list):
        raw_artifacts.extend(wheels)
    else:
        findings.append(
            _finding(
                "AGENT_TOOLING_LOCK_ARTIFACT",
                f"{LOCK_REL}:package.{EXPECTED_PACKAGE}.wheels",
                "wheels must be a list",
            )
        )

    identities: list[dict[str, Any]] = []
    for index, artifact in enumerate(raw_artifacts):
        identity = _artifact_identity(artifact) if isinstance(artifact, dict) else None
        if identity is None:
            findings.append(
                _finding(
                    "AGENT_TOOLING_LOCK_ARTIFACT",
                    f"{LOCK_REL}:package.{EXPECTED_PACKAGE}.artifacts[{index}]",
                    "artifact must declare an approved HTTPS URL, SHA-256 hash and positive size",
                )
            )
        else:
            identities.append(identity)

    urls = [identity["url"] for identity in identities]
    if len(urls) != len(set(urls)):
        findings.append(
            _finding(
                "AGENT_TOOLING_LOCK_ARTIFACT",
                f"{LOCK_REL}:package.{EXPECTED_PACKAGE}",
                "artifact URLs must be unique",
            )
        )

    if len(identities) != EXPECTED_ARTIFACT_COUNT:
        findings.append(
            _finding(
                "AGENT_TOOLING_LOCK_ARTIFACT_SET",
                f"{LOCK_REL}:package.{EXPECTED_PACKAGE}",
                f"expected exactly {EXPECTED_ARTIFACT_COUNT} reviewed artifacts",
            )
        )
    elif _artifact_set_sha256(identities) != EXPECTED_ARTIFACT_SET_SHA256:
        findings.append(
            _finding(
                "AGENT_TOOLING_LOCK_ARTIFACT_SET",
                f"{LOCK_REL}:package.{EXPECTED_PACKAGE}",
                "artifact URL/hash/size set differs from the reviewed contract",
            )
        )

    project_packages = [
        package
        for package in packages
        if isinstance(package, dict) and package.get("name") == "weltgewebe-tools"
    ]
    if len(project_packages) != 1:
        findings.append(
            _finding(
                "AGENT_TOOLING_LOCK_PROJECT",
                f"{LOCK_REL}:package",
                "expected exactly one weltgewebe-tools project entry",
            )
        )
    else:
        metadata = project_packages[0].get("metadata")
        requires_dist = metadata.get("requires-dist") if isinstance(metadata, dict) else None
        expected_requires_dist = [
            {
                "name": EXPECTED_PYTEST_PACKAGE,
                "specifier": f"=={EXPECTED_PYTEST_VERSION}",
            },
            {"name": EXPECTED_PACKAGE, "specifier": f"=={EXPECTED_VERSION}"},
        ]
        if requires_dist != expected_requires_dist:
            findings.append(
                _finding(
                    "AGENT_TOOLING_LOCK_PROJECT",
                    f"{LOCK_REL}:package.weltgewebe-tools.metadata.requires-dist",
                    "project metadata must bind exact pytest and PyYAML versions",
                )
            )

    unique: dict[tuple[str, str, str], dict[str, str]] = {}
    for finding in findings:
        unique[(finding["code"], finding["path"], finding["message"])] = finding
    return [unique[key] for key in sorted(unique)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate the hash-bound Python dependency set for agent tooling"
    )
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    args = parser.parse_args(argv)
    findings = validate_tooling_lock(Path(args.repo_root))
    payload = {
        "status": "valid" if not findings else "invalid",
        "pyproject": PYPROJECT_REL,
        "lock": LOCK_REL,
        "package": f"{EXPECTED_PACKAGE}=={EXPECTED_VERSION}",
        "packages": [
            f"{EXPECTED_PACKAGE}=={EXPECTED_VERSION}",
            f"{EXPECTED_PYTEST_PACKAGE}=={EXPECTED_PYTEST_VERSION}",
        ],
        "artifact_set_sha256": EXPECTED_ARTIFACT_SET_SHA256,
        "findings_count": len(findings),
        "findings": findings,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not findings else 1


if __name__ == "__main__":
    raise SystemExit(main())
