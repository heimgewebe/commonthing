#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import platform
import shutil
import stat
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

import yaml

ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = ROOT / "platform/toolchain.lock.json"
DEFAULT_CACHE = ROOT / ".cache/weltgewebe-platform"
DOWNLOAD_ATTEMPTS = 4
DOWNLOAD_BACKOFF_SECONDS = (1.0, 2.0, 4.0)
RETRYABLE_HTTP_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
MIRROR_ENV = "WELTGEWEBE_PLATFORM_DOWNLOAD_MIRROR"


class DownloadError(RuntimeError):
    failure_class = "download_error"


class DownloadUnavailableError(DownloadError):
    failure_class = "external_dependency_unavailable"


class DownloadIntegrityError(DownloadError):
    failure_class = "download_integrity_failure"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _download_lock(destination: Path) -> Iterator[None]:
    lock_path = destination.parent / f".{destination.name}.download.lock"
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    with os.fdopen(descriptor, "a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _mirror_url(url: str, mirror_base: str | None = None) -> str | None:
    base = mirror_base if mirror_base is not None else os.environ.get(MIRROR_ENV)
    if not base:
        return None
    parsed_base = urllib.parse.urlsplit(base)
    if (
        parsed_base.scheme != "https"
        or not parsed_base.netloc
        or parsed_base.username is not None
        or parsed_base.password is not None
        or parsed_base.query
        or parsed_base.fragment
    ):
        raise DownloadError(
            f"{MIRROR_ENV} must be an https origin/path without credentials, query, or fragment"
        )
    source = urllib.parse.urlsplit(url)
    if source.scheme != "https" or not source.netloc or source.query or source.fragment:
        raise DownloadError(f"unsupported canonical download URL: {url}")
    prefix = parsed_base.path.rstrip("/")
    path = f"{prefix}/{source.netloc}{source.path}"
    return urllib.parse.urlunsplit((parsed_base.scheme, parsed_base.netloc, path, "", ""))


def _download_urls(url: str) -> tuple[str, ...]:
    mirror = _mirror_url(url)
    return (mirror, url) if mirror and mirror != url else (url,)


def _retry_delay(error: BaseException, attempt: int) -> float:
    if isinstance(error, urllib.error.HTTPError):
        retry_after = error.headers.get("Retry-After") if error.headers else None
        if retry_after and retry_after.isdigit():
            return min(float(retry_after), 30.0)
    return DOWNLOAD_BACKOFF_SECONDS[min(attempt, len(DOWNLOAD_BACKOFF_SECONDS) - 1)]


def _retryable(error: BaseException) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        return error.code in RETRYABLE_HTTP_STATUS
    return isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError))


def _download_once(url: str, temporary: Path) -> None:
    request = urllib.request.Request(
        url, headers={"User-Agent": "weltgewebe-platform-bootstrap/2"}
    )
    with temporary.open("wb") as handle:
        with urllib.request.urlopen(request, timeout=90) as response:
            shutil.copyfileobj(response, handle)
        handle.flush()
        os.fsync(handle.fileno())


def _download(url: str, expected_sha256: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise DownloadIntegrityError(
            f"refusing symlinked download destination: {destination}"
        )
    with _download_lock(destination):
        if destination.is_symlink():
            raise DownloadIntegrityError(
                f"refusing symlinked download destination: {destination}"
            )
        if destination.is_file() and _sha256(destination) == expected_sha256:
            return
        errors: list[str] = []
        for candidate in _download_urls(url):
            for attempt in range(DOWNLOAD_ATTEMPTS):
                fd, temporary_name = tempfile.mkstemp(
                    dir=destination.parent,
                    prefix=f".{destination.name}.",
                    suffix=".download.tmp",
                )
                os.close(fd)
                temporary = Path(temporary_name)
                try:
                    _download_once(candidate, temporary)
                    actual = _sha256(temporary)
                    if actual != expected_sha256:
                        raise DownloadIntegrityError(
                            "download hash mismatch for "
                            f"{candidate}: expected {expected_sha256}, got {actual}"
                        )
                    os.replace(temporary, destination)
                    _fsync_directory(destination.parent)
                    return
                except DownloadIntegrityError:
                    raise
                except Exception as error:
                    errors.append(
                        f"{candidate} attempt {attempt + 1}/{DOWNLOAD_ATTEMPTS}: "
                        f"{type(error).__name__}: {error}"
                    )
                    if not _retryable(error) or attempt + 1 >= DOWNLOAD_ATTEMPTS:
                        break
                    time.sleep(_retry_delay(error, attempt))
                finally:
                    temporary.unlink(missing_ok=True)
        raise DownloadUnavailableError(
            "external dependency download unavailable after bounded retries: "
            + " | ".join(errors)
        )


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _install_executable(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise RuntimeError(f"refusing to replace symlinked tool destination: {destination}")
    executable_bits = stat.S_IXUSR | stat.S_IXGRP
    if destination.is_file() and _sha256(destination) == _sha256(source):
        destination.chmod(destination.stat().st_mode | executable_bits)
        return
    with tempfile.NamedTemporaryFile(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)
    try:
        shutil.copy2(source, tmp_path)
        tmp_path.chmod(tmp_path.stat().st_mode | executable_bits)
        with tmp_path.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(tmp_path, destination)
        _fsync_directory(destination.parent)
    finally:
        tmp_path.unlink(missing_ok=True)


def _assert_artifact_contract(name: str, path: Path, spec: dict[str, Any]) -> None:
    required_kind = spec.get("required_crd_kind")
    if required_kind is None:
        return
    observed: list[str] = []
    for document in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if not isinstance(document, dict):
            continue
        if document.get("kind") != "CustomResourceDefinition":
            raise RuntimeError(f"artifact {name} contains a non-CRD document")
        kind = document.get("spec", {}).get("names", {}).get("kind")
        if not isinstance(kind, str):
            raise RuntimeError(f"artifact {name} contains a CRD without a kind")
        observed.append(kind)
    if observed != [required_kind]:
        raise RuntimeError(
            f"artifact {name} must contain only CRD {required_kind}, got {observed}"
        )


def _validated_tar_members(
    handle: tarfile.TarFile,
) -> list[tuple[tarfile.TarInfo, Path]]:
    validated: list[tuple[tarfile.TarInfo, Path]] = []
    member_types: dict[Path, str] = {}

    for member in handle.getmembers():
        relative = PurePosixPath(member.name)
        if not member.name or relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError(f"unsafe archive member path: {member.name}")
        parts = tuple(part for part in relative.parts if part not in {"", "."})
        if not parts:
            if member.isdir():
                continue
            raise RuntimeError(f"unsafe archive member path: {member.name}")
        target = Path(*parts)

        if member.isdir():
            member_type = "directory"
        elif member.isfile():
            member_type = "file"
        else:
            raise RuntimeError(f"unsupported archive member type: {member.name}")

        previous = member_types.get(target)
        if previous is not None and (previous != member_type or member_type == "file"):
            raise RuntimeError(f"conflicting archive member: {member.name}")
        member_types[target] = member_type
        validated.append((member, target))

    for target in member_types:
        for parent in target.parents:
            if parent == Path("."):
                break
            if member_types.get(parent) == "file":
                raise RuntimeError(f"archive file shadows parent directory: {parent}")

    return validated


def _safe_extract_tar(archive: Path, destination: Path) -> None:
    if destination.is_symlink() or destination.exists():
        raise RuntimeError(f"archive destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    with tarfile.open(archive, "r:gz") as handle:
        members = _validated_tar_members(handle)
        destination.mkdir(mode=0o700)
        try:
            for member, relative in members:
                target = destination / relative
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    if target.is_symlink() or not target.is_dir():
                        raise RuntimeError(f"unsafe archive directory: {member.name}")
                    continue

                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                source = handle.extractfile(member)
                if source is None:
                    raise RuntimeError(f"archive file is unreadable: {member.name}")
                flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
                if hasattr(os, "O_CLOEXEC"):
                    flags |= os.O_CLOEXEC
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                with source, os.fdopen(os.open(target, flags, 0o600), "wb") as output:
                    shutil.copyfileobj(source, output)
                    output.flush()
                    os.fsync(output.fileno())
            _fsync_directory(destination)
            _fsync_directory(destination.parent)
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            _fsync_directory(destination.parent)
            raise


def _check_host(lock: dict[str, Any]) -> None:
    machine = platform.machine().lower()
    if platform.system() != "Linux" or machine not in {"x86_64", "amd64"}:
        raise RuntimeError(
            f"tool lock supports linux-amd64 only; observed {platform.system()}-{machine}"
        )
    if lock.get("platform") != "linux-amd64":
        raise RuntimeError("unexpected platform lock")


def _selected_tool_specs(lock: dict[str, Any], requested: list[str] | None) -> dict[str, Any]:
    tools = lock["tools"]
    if requested is None:
        return tools
    names = list(dict.fromkeys(requested))
    unknown = [name for name in names if name not in tools]
    if unknown:
        raise RuntimeError(f"unknown tool selection: {', '.join(unknown)}")
    return {name: tools[name] for name in names}


def install(
    cache: Path,
    *,
    tool_names: list[str] | None = None,
    include_artifacts: bool = True,
    lock_bytes: bytes | None = None,
) -> dict[str, Any]:
    if lock_bytes is None:
        lock_bytes = LOCK_PATH.read_bytes()
    if not isinstance(lock_bytes, bytes):
        raise RuntimeError("toolchain lock payload must be bytes")
    try:
        lock = json.loads(lock_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("toolchain lock payload is invalid") from exc
    if lock.get("schema_version") != 1:
        raise RuntimeError("unsupported toolchain lock schema")
    _check_host(lock)
    downloads = cache / "downloads"
    bin_dir = cache / "bin"
    artifact_dir = cache / "artifacts"
    bin_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    tool_paths: dict[str, str] = {}

    for name, spec in _selected_tool_specs(lock, tool_names).items():
        filename = Path(urllib.parse.urlparse(spec["url"]).path).name
        archive = downloads / filename
        _download(spec["url"], spec["sha256"], archive)
        destination = bin_dir / spec["binary"]
        if spec["format"] == "binary":
            _install_executable(archive, destination)
        elif spec["format"] == "tar.gz":
            with tempfile.TemporaryDirectory(dir=cache) as tmp:
                tmp_dir = Path(tmp)
                extract_root = tmp_dir / "extract"
                _safe_extract_tar(archive, extract_root)
                candidate = next(
                    (path for path in extract_root.rglob(spec["binary"]) if path.is_file()),
                    None,
                )
                if candidate is None:
                    raise RuntimeError(f"binary {spec['binary']} missing from {archive}")
                _install_executable(candidate, destination)
        else:
            raise RuntimeError(f"unsupported format {spec['format']} for {name}")
        expected_binary_sha256 = spec.get("binary_sha256")
        if (
            not isinstance(expected_binary_sha256, str)
            or len(expected_binary_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in expected_binary_sha256)
        ):
            raise RuntimeError(f"tool {name} has no canonical binary_sha256 in the lock")
        actual_binary_sha256 = _sha256(destination)
        if actual_binary_sha256 != expected_binary_sha256:
            raise DownloadIntegrityError(
                f"installed tool hash mismatch for {name}: "
                f"expected {expected_binary_sha256}, got {actual_binary_sha256}"
            )
        tool_paths[name] = str(destination)

    artifact_paths: dict[str, str] = {}
    if include_artifacts:
        for name, spec in lock["artifacts"].items():
            destination = artifact_dir / spec["filename"]
            _download(spec["url"], spec["sha256"], destination)
            _assert_artifact_contract(name, destination, spec)
            artifact_paths[name] = str(destination)

    receipt = {
        "schema_version": 1,
        "lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
        "cache": str(cache),
        "download_policy": {
            "attempts_per_source": DOWNLOAD_ATTEMPTS,
            "canonical_fallback": True,
            "digest_required": True,
            "mirror_configured": bool(os.environ.get(MIRROR_ENV)),
        },
        "tools": tool_paths,
        "artifacts": artifact_paths,
        "kubernetes": lock["kubernetes"],
    }
    _atomic_write_json(cache / "receipt.json", receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--tool", action="append", dest="tools")
    parser.add_argument("--skip-artifacts", action="store_true")
    args = parser.parse_args()
    try:
        receipt = install(
            args.cache.resolve(),
            tool_names=args.tools,
            include_artifacts=not args.skip_artifacts,
        )
    except DownloadError as error:
        print(
            json.dumps(
                {
                    "status": "error",
                    "failure_class": error.failure_class,
                    "detail": str(error),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    if args.json:
        print(json.dumps(receipt, sort_keys=True))
    else:
        print(receipt["cache"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
