#!/usr/bin/env python3
"""Bounded lifecycle driver for WELTGEWEBE-OS-V1-T085 Experiment B.

The driver owns only the temporary libvirt VM commonthing-experiment-b and
state below ~/.local/state/commonthing/experiment-b. It never targets production
DNS, production data, or a production Kubernetes context.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import csv
import ctypes
import fcntl
import functools
import hashlib
import ipaddress
import json
import math
import os
import pwd
import re
import secrets
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import types
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_HELPER = ROOT / "scripts/platform/experiment_b.py"
BOOTSTRAP_TOOLS_HELPER = ROOT / "scripts/platform/bootstrap_tools.py"
TOOLCHAIN_LOCK_PATH = ROOT / "platform/toolchain.lock.json"
CONFIG_PATH = ROOT / "platform/clusters/experiment-b/config.json"
CLUSTER = ROOT / "platform/clusters/experiment-b"
BOOTSTRAP_TEMPLATE = CLUSTER / "bootstrap-template.yaml"
NAMESPACES = CLUSTER / "namespaces"
MIGRATION = CLUSTER / "migration"
APP_OVERLAY = ROOT / "platform/apps/weltgewebe/overlays/experiment-b"
DEFAULT_STATE_ROOT = Path.home() / ".local/state/commonthing/experiment-b"
EXPERIMENT_B_LIFECYCLE_LOCK = DEFAULT_STATE_ROOT.parent / "experiment-b.lifecycle.lock"
VM_NAME = "commonthing-experiment-b"
LIBVIRT_URI = "qemu:///system"
POOL_NAME = "commonthing-experiment-b-pool"
POOL_TARGET = Path("/var/tmp/commonthing-experiment-b-libvirt")
BASE_VOLUME = "commonthing-experiment-b-base.qcow2"
VOLUME_NAME = "commonthing-experiment-b.qcow2"
APP_NAMESPACE = "commonthing-experiment-b"
DATA_NAMESPACE = "commonthing-data"
DOMAIN_EVENT_CONSUMER = "weltgewebe-api-domain-receipts-v1"
EXPECTED_PVCS = frozenset(
    {
        f"{DATA_NAMESPACE}/postgres-data",
        f"{DATA_NAMESPACE}/nats-data",
        f"{APP_NAMESPACE}/ollama-models",
    }
)
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
PERFORMANCE_POLICY = ROOT / "policies/performance.v1.json"
DOMAIN_SCALE = ROOT / "scripts/performance/domain_scale.py"
DOMAIN_SCALE_CONFIG = ROOT / "configs/performance/domain-scale.v1.json"
K6_WORKFLOW = ROOT / ".github/workflows/domain-scale.yml"
K6_WORKLOAD = ROOT / "scripts/performance/api_runtime_k6.js"
RETIREMENT_RECEIPT = Path.home() / ".local/state/commonthing/experiment-b-retirement.json"
T048_DOCUMENT_REVISION = "node-document-v4-canonical-visibility"
T048_NORMALIZATION_REVISION = "weltgewebe-search-normalization-v1"
T048_RANKING_REVISION = "weltgewebe-hybrid-ranking-v2"
T048_PUBLIC_CONTENT_SHA256 = "0" * 64
T048_HIDDEN_CONTENT_SHA256 = "e0f631f5602e764ef8a5f14e36d2d81663b20cd305a30af0dad6c0d759e5a955"
T048_REDACTED_TEXT = "[nicht öffentlich]"
K6_SUMMARY_STDOUT_MARKER = "__WELTGEWEBE_K6_SUMMARY_V1__"
K6_SUMMARY_MAX_BYTES = 4 * 1024 * 1024


class RuntimeErrorEB(RuntimeError):
    pass


class ContractError(RuntimeError):
    pass


def _reject_nonstandard_json_constant(value: str) -> Any:
    raise ValueError(f"non-standard JSON constant: {value}")


_BOUND_KUBECONFIG: ContextVar[str | None] = ContextVar(
    "experiment_b_bound_kubeconfig",
    default=None,
)
_BOUND_KUBECONFIG_FD: ContextVar[int | None] = ContextVar(
    "experiment_b_bound_kubeconfig_fd",
    default=None,
)
_BOUND_SOURCE_COMMIT: ContextVar[str | None] = ContextVar(
    "experiment_b_bound_source_commit",
    default=None,
)
_EXPERIMENT_B_LIFECYCLE_LOCK_HELD: ContextVar[bool] = ContextVar(
    "experiment_b_lifecycle_lock_held",
    default=False,
)
_TOOLCHAIN_SNAPSHOT_FDS: set[int] = set()
_TOOLCHAIN_SNAPSHOT_RECEIPTS: dict[tuple[str, str, str], dict[str, Any]] = {}


def _reopenable_proc_fd_path(fd: int) -> str:
    """Return a procfs path that survives child-side closefrom()."""
    if type(fd) is not int or fd < 0:
        raise RuntimeErrorEB("file descriptor path requires a non-negative integer")
    return f"/proc/{os.getpid()}/fd/{fd}"


def _bound_subprocess_pass_fds(
    pass_fds: tuple[int, ...] = (),
) -> tuple[int, ...]:
    values = list(pass_fds)
    bound_fd = _BOUND_KUBECONFIG_FD.get()
    if bound_fd is not None and bound_fd not in values:
        values.append(bound_fd)
    for snapshot_fd in sorted(_TOOLCHAIN_SNAPSHOT_FDS):
        if snapshot_fd not in values:
            values.append(snapshot_fd)
    return tuple(values)


def run(
    argv: list[str],
    *,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    capture: bool = True,
    check: bool = True,
    timeout: int = 900,
    pass_fds: tuple[int, ...] = (),
) -> subprocess.CompletedProcess[str]:
    with _bound_ssh_command(argv) as (command, ssh_pass_fds):
        result = subprocess.run(
            command,
            cwd=ROOT,
            input=input_text,
            env=env,
            text=True,
            capture_output=capture,
            timeout=timeout,
            check=False,
            pass_fds=_bound_subprocess_pass_fds((*pass_fds, *ssh_pass_fds)),
        )
    if check and result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeErrorEB(
            f"command failed ({result.returncode}): {command[0]}: {stderr[-2000:]}"
        )
    return result


def require_binary(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeErrorEB(f"required host command is missing: {name}")
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_blob_bytes(source_commit: str, path: Path) -> bytes:
    if COMMIT_RE.fullmatch(source_commit) is None:
        raise RuntimeErrorEB("Git blob binding requires an exact source commit")
    try:
        relative = path.relative_to(ROOT)
    except ValueError as exc:
        raise RuntimeErrorEB("Git blob binding path escapes the repository") from exc
    try:
        result = subprocess.run(
            ["git", "cat-file", "blob", f"{source_commit}:{relative.as_posix()}"],
            cwd=ROOT,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeErrorEB("Git blob binding timed out") from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeErrorEB(f"Git blob binding failed: {stderr[-1000:]}")
    return result.stdout


def _git_blob_sha256(source_commit: str, path: Path) -> str:
    return hashlib.sha256(_git_blob_bytes(source_commit, path)).hexdigest()


def _git_tree_regular_blob_paths(
    source_commit: str,
    subtree: Path,
) -> list[Path]:
    if COMMIT_RE.fullmatch(source_commit) is None:
        raise RuntimeErrorEB("Git tree binding requires an exact source commit")
    try:
        relative_subtree = subtree.relative_to(ROOT)
    except ValueError as exc:
        raise RuntimeErrorEB("Git tree binding path escapes the repository") from exc
    try:
        result = subprocess.run(
            [
                "git",
                "ls-tree",
                "-r",
                "-z",
                source_commit,
                "--",
                relative_subtree.as_posix(),
            ],
            cwd=ROOT,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeErrorEB("Git tree binding timed out") from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeErrorEB(f"Git tree binding failed: {stderr[-1000:]}")
    paths: list[Path] = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        try:
            metadata, raw_path = raw.split(b"\t", 1)
            mode, object_type, _object_id = metadata.decode("ascii").split()
            relative = Path(raw_path.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise RuntimeErrorEB("Git tree binding returned an invalid entry") from exc
        if (
            mode not in {"100644", "100755"}
            or object_type != "blob"
            or relative.is_absolute()
            or ".." in relative.parts
        ):
            raise RuntimeErrorEB(
                "Git tree binding contains a non-regular or unsafe entry"
            )
        try:
            relative.relative_to(relative_subtree)
        except ValueError as exc:
            raise RuntimeErrorEB(
                "Git tree binding entry escapes the requested subtree"
            ) from exc
        paths.append(relative)
    if not paths or len(paths) != len(set(paths)):
        raise RuntimeErrorEB("Git tree binding is empty or duplicated")
    return sorted(paths, key=lambda item: item.as_posix())


def state_root(value: str | None) -> Path:
    configured = Path(value).expanduser() if value else DEFAULT_STATE_ROOT
    root = Path(os.path.abspath(configured))
    allowed_root = Path(os.path.abspath(DEFAULT_STATE_ROOT.expanduser()))
    if root != allowed_root:
        try:
            root.relative_to(allowed_root)
        except ValueError as exc:
            raise RuntimeErrorEB(
                f"state root must be {allowed_root} or one of its descendants"
            ) from exc
    return root


def _ensure_state_root(root: Path) -> None:
    root_fd = _open_directory_nofollow(
        root,
        create=True,
        context="state root",
        require_owner=True,
    )
    try:
        os.fchmod(root_fd, 0o700)
    finally:
        os.close(root_fd)


def _open_directory_nofollow(
    path: Path,
    *,
    create: bool,
    context: str,
    require_owner: bool = False,
) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    cloexec = getattr(os, "O_CLOEXEC", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if not all(isinstance(flag, int) for flag in (nofollow, cloexec, directory)):
        raise RuntimeErrorEB(f"{context} is unsafe")
    flags = os.O_RDONLY | nofollow | cloexec | directory
    absolute = Path(os.path.abspath(path))
    try:
        directory_fd = os.open("/", flags)
    except OSError as exc:
        raise RuntimeErrorEB(f"{context} is unsafe") from exc
    try:
        for component in absolute.parts[1:]:
            try:
                next_fd = os.open(component, flags, dir_fd=directory_fd)
            except FileNotFoundError:
                if not create:
                    raise RuntimeErrorEB(f"{context} is unsafe")
                try:
                    os.mkdir(component, mode=0o700, dir_fd=directory_fd)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise RuntimeErrorEB(f"{context} is unsafe") from exc
                try:
                    next_fd = os.open(component, flags, dir_fd=directory_fd)
                except OSError as exc:
                    raise RuntimeErrorEB(f"{context} is unsafe") from exc
            except OSError as exc:
                raise RuntimeErrorEB(f"{context} is unsafe") from exc
            os.close(directory_fd)
            directory_fd = next_fd
        metadata = os.fstat(directory_fd)
        if not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeErrorEB(f"{context} is unsafe")
        if require_owner and metadata.st_uid != os.getuid():
            raise RuntimeErrorEB(f"{context} is unsafe")
        return directory_fd
    except Exception:
        os.close(directory_fd)
        raise


def _open_libvirt_pool_target(*, create: bool) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    cloexec = getattr(os, "O_CLOEXEC", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if not all(isinstance(flag, int) for flag in (nofollow, cloexec, directory)):
        raise RuntimeErrorEB("libvirt pool target is unsafe")
    parent_fd = _open_directory_nofollow(
        POOL_TARGET.parent,
        create=False,
        context="libvirt pool target parent",
    )
    pool_fd: int | None = None
    try:
        if create:
            try:
                os.mkdir(POOL_TARGET.name, mode=0o755, dir_fd=parent_fd)
            except FileExistsError:
                pass
            except OSError as exc:
                raise RuntimeErrorEB("libvirt pool target is unsafe") from exc
        try:
            pool_fd = os.open(
                POOL_TARGET.name,
                os.O_RDONLY | nofollow | cloexec | directory,
                dir_fd=parent_fd,
            )
        except OSError as exc:
            raise RuntimeErrorEB("libvirt pool target is unsafe") from exc
    finally:
        os.close(parent_fd)
    try:
        metadata = os.fstat(pool_fd)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
        ):
            raise RuntimeErrorEB("libvirt pool target is unsafe")
        if create:
            if os.listdir(pool_fd):
                raise RuntimeErrorEB(
                    "Experiment-B libvirt pool target already contains files"
                )
            try:
                os.fchmod(pool_fd, 0o755)
            except OSError as exc:
                raise RuntimeErrorEB("libvirt pool target is unsafe") from exc
            metadata = os.fstat(pool_fd)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o755
            ):
                raise RuntimeErrorEB("libvirt pool target is unsafe")
        result = pool_fd
        pool_fd = None
        return result
    finally:
        if pool_fd is not None:
            os.close(pool_fd)


def _libvirt_qemu_identity() -> tuple[int, int]:
    try:
        account = pwd.getpwnam("libvirt-qemu")
    except KeyError as exc:
        raise RuntimeErrorEB("libvirt-qemu account is unavailable") from exc
    uid = int(account.pw_uid)
    gid = int(account.pw_gid)
    if uid <= 0 or gid < 0:
        raise RuntimeErrorEB("libvirt-qemu identity is invalid")
    return uid, gid


def _libvirt_volume_xml(
    name: str,
    capacity_bytes: int,
    *,
    qemu_uid: int,
    qemu_gid: int,
    backing_path: Path | None = None,
) -> bytes:
    if name not in {BASE_VOLUME, VOLUME_NAME}:
        raise RuntimeErrorEB("libvirt volume name is outside Experiment-B scope")
    if type(capacity_bytes) is not int or capacity_bytes <= 0:
        raise RuntimeErrorEB("libvirt volume capacity is invalid")
    if type(qemu_uid) is not int or qemu_uid <= 0 or type(qemu_gid) is not int or qemu_gid < 0:
        raise RuntimeErrorEB("libvirt-qemu identity is invalid")
    volume = ET.Element("volume", {"type": "file"})
    ET.SubElement(volume, "name").text = name
    ET.SubElement(volume, "capacity", {"unit": "bytes"}).text = str(capacity_bytes)
    target = ET.SubElement(volume, "target")
    ET.SubElement(target, "format", {"type": "qcow2"})
    permissions = ET.SubElement(target, "permissions")
    ET.SubElement(permissions, "mode").text = "0600"
    ET.SubElement(permissions, "owner").text = str(qemu_uid)
    ET.SubElement(permissions, "group").text = str(qemu_gid)
    if backing_path is not None:
        if backing_path != POOL_TARGET / BASE_VOLUME:
            raise RuntimeErrorEB("libvirt backing path is outside Experiment-B scope")
        backing = ET.SubElement(volume, "backingStore")
        ET.SubElement(backing, "path").text = str(backing_path)
        ET.SubElement(backing, "format", {"type": "qcow2"})
    return ET.tostring(volume, encoding="utf-8", xml_declaration=True)


def _create_libvirt_volume(
    name: str,
    capacity_bytes: int,
    *,
    qemu_uid: int,
    qemu_gid: int,
    backing_path: Path | None = None,
) -> os.stat_result:
    payload = _libvirt_volume_xml(
        name,
        capacity_bytes,
        qemu_uid=qemu_uid,
        qemu_gid=qemu_gid,
        backing_path=backing_path,
    )
    with _sealed_snapshot_fd(
        payload,
        f"Experiment-B libvirt volume XML {name}",
    ) as volume_xml_fd:
        run(
            [
                "virsh",
                "-c",
                LIBVIRT_URI,
                "vol-create",
                POOL_NAME,
                f"/proc/self/fd/{volume_xml_fd}",
            ],
            pass_fds=(volume_xml_fd,),
        )
    path = POOL_TARGET / name
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise RuntimeErrorEB("libvirt volume permissions are unreadable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != qemu_uid
        or metadata.st_gid != qemu_gid
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        raise RuntimeErrorEB("libvirt volume permissions drifted")
    return metadata


def _unlink_state_path(path: Path, context: str) -> None:
    if not path.name or path.name in {".", ".."}:
        raise RuntimeErrorEB(f"{context} name is unsafe")
    parent_fd = _open_directory_nofollow(
        path.parent,
        create=True,
        context=f"{context} parent",
        require_owner=True,
    )
    try:
        try:
            os.unlink(path.name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise RuntimeErrorEB(f"{context} is unsafe") from exc
    finally:
        os.close(parent_fd)


def _atomic_write_bytes(path: Path, payload: bytes, mode: int) -> None:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    cloexec = getattr(os, "O_CLOEXEC", None)
    if not isinstance(nofollow, int) or not isinstance(cloexec, int):
        raise RuntimeErrorEB("state output cannot be written safely")
    if not isinstance(mode, int) or mode < 0 or mode > 0o777:
        raise RuntimeErrorEB("state output mode is invalid")
    if not path.name or path.name in {".", ".."}:
        raise RuntimeErrorEB("state output name is invalid")
    parent_fd = _open_directory_nofollow(
        path.parent,
        create=True,
        context="state output parent",
    )
    temporary_name = f".{path.name}.{secrets.token_hex(12)}.tmp"
    temporary_fd: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow | cloexec
        try:
            temporary_fd = os.open(
                temporary_name,
                flags,
                mode,
                dir_fd=parent_fd,
            )
        except OSError as exc:
            raise RuntimeErrorEB("state output cannot be created safely") from exc
        metadata = os.fstat(temporary_fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            raise RuntimeErrorEB("state output identity is unsafe")
        os.fchmod(temporary_fd, mode)
        view = memoryview(payload)
        offset = 0
        while offset < len(view):
            written = os.write(temporary_fd, view[offset:])
            if written <= 0:
                raise RuntimeErrorEB("state output write failed")
            offset += written
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = None
        try:
            os.replace(
                temporary_name,
                path.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        except OSError as exc:
            raise RuntimeErrorEB("state output replacement failed") from exc
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        try:
            os.unlink(temporary_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        finally:
            os.close(parent_fd)


def atomic_json(path: Path, payload: dict[str, Any], mode: int = 0o600) -> None:
    encoded = (
        json.dumps(payload, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _atomic_write_bytes(path, encoded, mode)


def atomic_bytes(path: Path, payload: bytes, mode: int = 0o600) -> None:
    _atomic_write_bytes(path, payload, mode)


def _write_kubeconfig(root: Path, kubeconfig: str) -> Path:
    path = root / "kubeconfig.yaml"
    atomic_bytes(path, kubeconfig.encode("utf-8"), mode=0o600)
    return path


def _open_performance_directory(root: Path) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    cloexec = getattr(os, "O_CLOEXEC", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if not all(isinstance(flag, int) for flag in (nofollow, cloexec, directory)):
        raise RuntimeErrorEB("performance state directory is unsafe")
    flags = os.O_RDONLY | nofollow | cloexec | directory
    try:
        root_fd = os.open(root, flags)
    except OSError as exc:
        raise RuntimeErrorEB("performance state directory is unsafe") from exc
    try:
        root_metadata = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid != os.getuid()
        ):
            raise RuntimeErrorEB("performance state directory is unsafe")
        try:
            os.mkdir("performance", mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        except OSError as exc:
            raise RuntimeErrorEB("performance state directory is unsafe") from exc
        try:
            performance_fd = os.open("performance", flags, dir_fd=root_fd)
        except OSError as exc:
            raise RuntimeErrorEB("performance state directory is unsafe") from exc
    finally:
        os.close(root_fd)
    try:
        metadata = os.fstat(performance_fd)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
        ):
            raise RuntimeErrorEB("performance state directory is unsafe")
        os.fchmod(performance_fd, 0o700)
    except Exception:
        os.close(performance_fd)
        raise
    return performance_fd


def _open_performance_text_output(root: Path, name: str) -> Any:
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
    ):
        raise RuntimeErrorEB("performance output path is unsafe")
    directory_fd = _open_performance_directory(root)
    output_fd: int | None = None
    try:
        try:
            os.unlink(name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise RuntimeErrorEB("performance output path is unsafe") from exc
        nofollow = getattr(os, "O_NOFOLLOW", None)
        cloexec = getattr(os, "O_CLOEXEC", None)
        if not isinstance(nofollow, int) or not isinstance(cloexec, int):
            raise RuntimeErrorEB("performance output path is unsafe")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow | cloexec
        try:
            output_fd = os.open(
                name,
                flags,
                0o600,
                dir_fd=directory_fd,
            )
        except OSError as exc:
            raise RuntimeErrorEB("performance output path is unsafe") from exc
        metadata = os.fstat(output_fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            raise RuntimeErrorEB("performance output path is unsafe")
        os.fchmod(output_fd, 0o600)
        handle = os.fdopen(output_fd, "w", encoding="utf-8")
        output_fd = None
        return handle
    finally:
        if output_fd is not None:
            os.close(output_fd)
        os.close(directory_fd)


def _write_performance_text(root: Path, name: str, payload: str) -> None:
    with _open_performance_text_output(root, name) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _serialize_experiment_b_lifecycle(function: Any) -> Any:
    @functools.wraps(function)
    def wrapped(root: Path, *args: Any, **kwargs: Any) -> Any:
        if _EXPERIMENT_B_LIFECYCLE_LOCK_HELD.get():
            return function(root, *args, **kwargs)

        lock_path = EXPERIMENT_B_LIFECYCLE_LOCK
        lock_parent_fd = _open_directory_nofollow(
            lock_path.parent,
            create=True,
            context="Experiment-B lifecycle lock directory",
            require_owner=True,
        )
        try:
            nofollow = getattr(os, "O_NOFOLLOW", None)
            cloexec = getattr(os, "O_CLOEXEC", None)
            if not isinstance(nofollow, int) or not isinstance(cloexec, int):
                raise RuntimeErrorEB(
                    "Experiment-B lifecycle lock cannot be opened safely"
                )
            flags = os.O_RDWR | os.O_CREAT | nofollow | cloexec
            try:
                lock_fd = os.open(
                    lock_path.name,
                    flags,
                    0o600,
                    dir_fd=lock_parent_fd,
                )
            except OSError as exc:
                raise RuntimeErrorEB(
                    "Experiment-B lifecycle lock cannot be opened"
                ) from exc
        finally:
            os.close(lock_parent_fd)
        acquired = False
        token = None
        try:
            metadata = os.fstat(lock_fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1
            ):
                raise RuntimeErrorEB(
                    "Experiment-B lifecycle lock identity is invalid"
                )
            os.fchmod(lock_fd, 0o600)
            try:
                fcntl.flock(
                    lock_fd,
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BlockingIOError as exc:
                raise RuntimeErrorEB(
                    "Experiment-B lifecycle is already running"
                ) from exc
            acquired = True
            token = _EXPERIMENT_B_LIFECYCLE_LOCK_HELD.set(True)
            _ensure_state_root(root)
            return function(root, *args, **kwargs)
        finally:
            if token is not None:
                _EXPERIMENT_B_LIFECYCLE_LOCK_HELD.reset(token)
            if acquired:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(lock_fd)

    return wrapped


PORTABILITY_DERIVED_RECEIPTS = ("portability.json",)
RECOVERY_ATTEMPT_INVALIDATES = (
    "recovery.json",
    "recovery-attempt.json",
    "recovery-failed.json",
    "status.json",
    "status-attempt.json",
    "portability.json",
)
FIXTURE_ATTEMPT_INVALIDATES = (
    "t048-fixture.json",
    "functional-readback.json",
    "functional-readback-attempt.json",
    "t048-load.json",
    "t048-load-attempt.json",
    "recovery.json",
    "recovery-attempt.json",
    "recovery-failed.json",
    "status.json",
    "status-attempt.json",
    "portability.json",
)


def _invalidate_receipts(root: Path, names: tuple[str, ...]) -> None:
    for name in names:
        _unlink_state_path(
            root / "receipts" / name,
            "receipt invalidation",
        )


def _begin_live_check_attempt(
    root: Path,
    receipt_stem: str,
    source_commit: str,
) -> tuple[Path, Path, int]:
    if not COMMIT_RE.fullmatch(source_commit):
        raise RuntimeErrorEB(f"{receipt_stem} attempt source commit is not exact")
    _invalidate_receipts(root, PORTABILITY_DERIVED_RECEIPTS)
    receipt_path = root / "receipts" / f"{receipt_stem}.json"
    attempt_path = root / "receipts" / f"{receipt_stem}-attempt.json"
    started_at_unix_ms = time.time_ns() // 1_000_000
    atomic_json(
        attempt_path,
        {
            "schema_version": 1,
            "status": "running",
            "source_commit": source_commit,
            "receipt": receipt_path.name,
            "started_at_unix_ms": started_at_unix_ms,
        },
    )
    _unlink_state_path(receipt_path, f"{receipt_stem} receipt")
    return receipt_path, attempt_path, started_at_unix_ms


def _complete_live_check_attempt(
    attempt_path: Path,
    receipt_path: Path,
    source_commit: str,
    started_at_unix_ms: int,
    status: str,
) -> None:
    atomic_json(
        attempt_path,
        {
            "schema_version": 1,
            "status": status,
            "source_commit": source_commit,
            "receipt": receipt_path.name,
            "started_at_unix_ms": started_at_unix_ms,
            "finished_at_unix_ms": time.time_ns() // 1_000_000,
            "receipt_sha256": sha256_file(receipt_path),
        },
    )


def _require_recovery_attempt_clear(root: Path) -> None:
    attempt_path = root / "receipts/recovery-attempt.json"
    if not attempt_path.is_file():
        return
    try:
        attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeErrorEB(
            "recovery proof refuses an unreadable previous attempt; rebuild the "
            "Experiment-B cell to establish a fresh baseline"
        ) from exc
    if not isinstance(attempt, dict) or attempt.get("schema_version") != 1:
        raise RuntimeErrorEB(
            "recovery proof refuses an invalid previous attempt; rebuild the "
            "Experiment-B cell to establish a fresh baseline"
        )
    status = attempt.get("status")
    if status in {"running", "failed"}:
        raise RuntimeErrorEB(
            "recovery proof refuses a retry after an incomplete or failed attempt; "
            "rebuild the Experiment-B cell to establish a fresh baseline"
        )
    if status != "pass":
        raise RuntimeErrorEB(
            "recovery proof refuses an unknown previous attempt state; rebuild the "
            "Experiment-B cell to establish a fresh baseline"
        )


RELEASE_DEPENDENT_RECEIPTS = (
    "t048-fixture.json",
    "semantic-search.json",
    "semantic-search-attempt.json",
    "functional-readback.json",
    "functional-readback-attempt.json",
    "t048-load.json",
    "t048-load-attempt.json",
    "recovery.json",
    "recovery-attempt.json",
    "recovery-failed.json",
    "status.json",
    "status-attempt.json",
    "portability.json",
)


SECRETS_ATTEMPT_INVALIDATES = (
    "secrets.json",
    "release.json",
    "release-attempt.json",
    *RELEASE_DEPENDENT_RECEIPTS,
)
PLATFORM_ATTEMPT_INVALIDATES = (
    "platform.json",
    *SECRETS_ATTEMPT_INVALIDATES,
)
K3S_ATTEMPT_INVALIDATES = (
    "k3s.json",
    *PLATFORM_ATTEMPT_INVALIDATES,
)
VM_ATTEMPT_INVALIDATES = (
    "vm-create.json",
    "vm-create-attempt.json",
    *K3S_ATTEMPT_INVALIDATES,
)


def _begin_release_attempt(
    root: Path,
    source_commit: str,
) -> tuple[Path, Path, int]:
    receipt_path, attempt_path, started_at_unix_ms = _begin_live_check_attempt(
        root, "release", source_commit
    )
    _invalidate_receipts(root, RELEASE_DEPENDENT_RECEIPTS)
    return receipt_path, attempt_path, started_at_unix_ms


def _sha256_fd(file_fd: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(file_fd, 1024 * 1024, offset)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)
        offset += len(chunk)


def download(
    url: str,
    expected_sha256: str,
    destination: Path,
    *,
    mode: int | None = None,
) -> None:
    if mode is not None and (
        not isinstance(mode, int)
        or isinstance(mode, bool)
        or mode < 0
        or mode > 0o777
    ):
        raise RuntimeErrorEB("download mode is invalid")
    if not destination.name or destination.name in {".", ".."}:
        raise RuntimeErrorEB("download destination is unsafe")

    directory_fd = _open_directory_nofollow(
        destination.parent,
        create=True,
        context="download directory",
        require_owner=True,
    )
    temporary_name = f".{destination.name}.{secrets.token_hex(12)}.tmp"
    temporary_fd: int | None = None
    try:
        if _state_entry_exists_at(
            directory_fd,
            destination.name,
            "download destination",
        ):
            existing_fd = _open_regular_state_file_at(
                directory_fd,
                destination.name,
                "download destination",
            )
            try:
                if _sha256_fd(existing_fd) == expected_sha256:
                    if mode is not None:
                        os.fchmod(existing_fd, mode)
                    return
            finally:
                os.close(existing_fd)

        nofollow = getattr(os, "O_NOFOLLOW", None)
        cloexec = getattr(os, "O_CLOEXEC", None)
        if not isinstance(nofollow, int) or not isinstance(cloexec, int):
            raise RuntimeErrorEB("download destination is unsafe")
        try:
            temporary_fd = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow | cloexec,
                0o600,
                dir_fd=directory_fd,
            )
        except OSError as exc:
            raise RuntimeErrorEB("download destination is unsafe") from exc
        metadata = os.fstat(temporary_fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            raise RuntimeErrorEB("download destination is unsafe")

        request = urllib.request.Request(
            url, headers={"User-Agent": "commonthing-experiment-b/1"}
        )
        digest = hashlib.sha256()
        with urllib.request.urlopen(request, timeout=90) as response:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                view = memoryview(chunk)
                offset = 0
                while offset < len(view):
                    written = os.write(temporary_fd, view[offset:])
                    if written <= 0:
                        raise RuntimeErrorEB("download write failed")
                    offset += written
        os.fsync(temporary_fd)
        observed = digest.hexdigest()
        if observed != expected_sha256:
            raise RuntimeErrorEB(
                f"download digest mismatch: expected {expected_sha256}, got {observed}"
            )
        os.fchmod(temporary_fd, mode if mode is not None else 0o600)
        os.close(temporary_fd)
        temporary_fd = None
        try:
            os.replace(
                temporary_name,
                destination.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
        except OSError as exc:
            raise RuntimeErrorEB("download destination replacement failed") from exc
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        finally:
            os.close(directory_fd)



def load_config(source_commit: str | None = None) -> dict[str, Any]:
    commit = source_commit if source_commit is not None else git_head()
    return _source_commit_config(commit)


def _source_commit_config(source_commit: str) -> dict[str, Any]:
    contract = _source_bound_contract(source_commit)
    try:
        config = json.loads(
            _git_blob_bytes(source_commit, CONFIG_PATH).decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeErrorEB(
            "source-commit Experiment-B config is invalid"
        ) from exc
    contract.validate_config(config)
    if config["vm"]["name"] != VM_NAME:
        raise RuntimeErrorEB("unexpected VM name")
    return config

def git_head() -> str:
    return run(["git", "rev-parse", "HEAD"]).stdout.strip()


def remote_main() -> str:
    lines = run(["git", "ls-remote", "origin", "refs/heads/main"]).stdout.splitlines()
    if len(lines) != 1:
        raise RuntimeErrorEB("could not resolve exactly one origin/main")
    return lines[0].split()[0]


def _current_protected_main_commit() -> str:
    head = git_head()
    if not COMMIT_RE.fullmatch(head):
        raise RuntimeErrorEB("checkout HEAD is not an exact lowercase commit SHA")
    current_main = remote_main()
    if current_main != head:
        raise RuntimeErrorEB("checkout HEAD is no longer current protected main")
    if run(["git", "status", "--porcelain"]).stdout.strip():
        raise RuntimeErrorEB("checkout must be clean to bind current protected main")
    return head


def _virsh_info_field(
    payload: str,
    field: str,
    context: str,
) -> str:
    values: list[str] = []
    for line in payload.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip() == field:
            values.append(value.strip())
    if len(values) != 1 or not values[0]:
        raise RuntimeErrorEB(
            f"{context} does not contain exactly one {field} field"
        )
    return values[0]


def preflight(expected_source_commit: str | None = None) -> dict[str, Any]:
    config = load_config()
    for command in (
        "virsh", "virt-install", "qemu-img", "ssh", "scp", "ssh-keygen", "docker",
        "bwrap",
    ):
        require_binary(command)
    if not Path("/dev/kvm").exists():
        raise RuntimeErrorEB("/dev/kvm is unavailable")
    if expected_source_commit is not None:
        if not COMMIT_RE.fullmatch(expected_source_commit):
            raise RuntimeErrorEB("expected source commit must be exact lowercase SHA-1")
        if git_head() != expected_source_commit:
            raise RuntimeErrorEB("checkout HEAD differs from expected source commit")
        if remote_main() != expected_source_commit:
            raise RuntimeErrorEB("expected source commit is no longer origin/main")
        if run(["git", "status", "--porcelain"]).stdout.strip():
            raise RuntimeErrorEB("expected-source checkout must be clean before runtime effects")

    network = run(
        ["virsh", "-c", LIBVIRT_URI, "net-info", config["vm"]["network"]]
    ).stdout
    if (
        _virsh_info_field(
            network,
            "Active",
            "libvirt default network",
        ).casefold()
        != "yes"
    ):
        raise RuntimeErrorEB("libvirt default network is not active")

    domain = run(
        ["virsh", "-c", LIBVIRT_URI, "dominfo", VM_NAME],
        check=False,
    )
    return {
        "status": "ok",
        "source_commit": git_head(),
        "remote_main": remote_main(),
        "domain_present": domain.returncode == 0,
        "vm": {
            "name": VM_NAME,
            "vcpu": config["vm"]["vcpu"],
            "memory_mib": config["vm"]["memory_mib"],
            "disk_gib": config["vm"]["disk_gib"],
            "network": config["vm"]["network"],
        },
    }


def _state_entry_exists_at(directory_fd: int, name: str, context: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RuntimeErrorEB(f"{context} is unsafe") from exc
    return True


def _open_regular_state_file_at(
    directory_fd: int,
    name: str,
    context: str,
) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    cloexec = getattr(os, "O_CLOEXEC", None)
    if not isinstance(nofollow, int) or not isinstance(cloexec, int):
        raise RuntimeErrorEB(f"{context} is unsafe")
    try:
        file_fd = os.open(
            name,
            os.O_RDONLY | nofollow | cloexec,
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise RuntimeErrorEB(f"{context} is unsafe") from exc
    try:
        metadata = os.fstat(file_fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            raise RuntimeErrorEB(f"{context} is unsafe")
    except Exception:
        os.close(file_fd)
        raise
    return file_fd


def _open_or_create_regular_state_file_at(
    directory_fd: int,
    name: str,
    context: str,
    *,
    mode: int = 0o600,
) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    cloexec = getattr(os, "O_CLOEXEC", None)
    if not isinstance(nofollow, int) or not isinstance(cloexec, int):
        raise RuntimeErrorEB(f"{context} is unsafe")
    try:
        file_fd = os.open(
            name,
            os.O_RDWR | os.O_CREAT | nofollow | cloexec,
            mode,
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise RuntimeErrorEB(f"{context} is unsafe") from exc
    try:
        metadata = os.fstat(file_fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            raise RuntimeErrorEB(f"{context} is unsafe")
        os.fchmod(file_fd, mode)
    except Exception:
        os.close(file_fd)
        raise
    return file_fd


@contextmanager
def _bound_ssh_command(
    argv: list[str],
) -> Iterator[tuple[list[str], tuple[int, ...]]]:
    if not argv or Path(argv[0]).name not in {"ssh", "scp"}:
        yield list(argv), ()
        return

    try:
        key_flag = argv.index("-i")
        private_path = Path(argv[key_flag + 1])
    except (ValueError, IndexError) as exc:
        raise RuntimeErrorEB("SSH private key binding is missing") from exc

    known_prefix = "UserKnownHostsFile="
    known_indices = [
        index
        for index, value in enumerate(argv)
        if isinstance(value, str) and value.startswith(known_prefix)
    ]
    if len(known_indices) != 1:
        raise RuntimeErrorEB("SSH known-hosts binding is missing")
    known_index = known_indices[0]
    known_hosts_path = Path(argv[known_index][len(known_prefix):])
    if (
        private_path.name != "id_ed25519"
        or known_hosts_path.name != "known_hosts"
        or private_path.parent != known_hosts_path.parent
    ):
        raise RuntimeErrorEB("SSH credential paths are unsafe")

    ssh_fd = _open_directory_nofollow(
        private_path.parent,
        create=False,
        context="SSH state directory",
        require_owner=True,
    )
    private_fd: int | None = None
    known_hosts_fd: int | None = None
    try:
        try:
            private_fd = _open_regular_state_file_at(
                ssh_fd,
                "id_ed25519",
                "SSH private key",
            )
            private_metadata = os.fstat(private_fd)
            if stat.S_IMODE(private_metadata.st_mode) != 0o600:
                raise RuntimeErrorEB("SSH private key mode is unsafe")
            known_hosts_fd = _open_or_create_regular_state_file_at(
                ssh_fd,
                "known_hosts",
                "SSH known-hosts file",
                mode=0o600,
            )
        except Exception:
            if known_hosts_fd is not None:
                os.close(known_hosts_fd)
                known_hosts_fd = None
            if private_fd is not None:
                os.close(private_fd)
                private_fd = None
            raise
    finally:
        os.close(ssh_fd)

    try:
        bound = list(argv)
        bound[key_flag + 1] = _reopenable_proc_fd_path(private_fd)
        bound[known_index] = (
            f"{known_prefix}{_reopenable_proc_fd_path(known_hosts_fd)}"
        )
        yield bound, (private_fd, known_hosts_fd)
    finally:
        if known_hosts_fd is not None:
            os.close(known_hosts_fd)
        if private_fd is not None:
            os.close(private_fd)


def ensure_ssh_key(root: Path) -> tuple[Path, Path]:
    private = root / "ssh/id_ed25519"
    public = root / "ssh/id_ed25519.pub"
    ssh_fd = _open_directory_nofollow(
        root / "ssh",
        create=True,
        context="SSH state directory",
        require_owner=True,
    )
    try:
        private_exists = _state_entry_exists_at(
            ssh_fd,
            "id_ed25519",
            "SSH private key",
        )
        public_exists = _state_entry_exists_at(
            ssh_fd,
            "id_ed25519.pub",
            "SSH public key",
        )
        if private_exists != public_exists:
            raise RuntimeErrorEB("SSH key pair is incomplete or unsafe")
        if not private_exists:
            run(
                [
                    "ssh-keygen",
                    "-q",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-C",
                    "commonthing-experiment-b",
                    "-f",
                    f"/proc/self/fd/{ssh_fd}/id_ed25519",
                ],
                pass_fds=(ssh_fd,),
            )
        private_fd = _open_regular_state_file_at(
            ssh_fd,
            "id_ed25519",
            "SSH private key",
        )
        try:
            public_fd = _open_regular_state_file_at(
                ssh_fd,
                "id_ed25519.pub",
                "SSH public key",
            )
            try:
                os.fchmod(private_fd, 0o600)
            finally:
                os.close(public_fd)
        finally:
            os.close(private_fd)
    finally:
        os.close(ssh_fd)
    return private, public


@_serialize_experiment_b_lifecycle
def prepare(
    root: Path,
    source_commit: str | None = None,
) -> dict[str, Any]:
    commit = (
        source_commit
        if source_commit is not None
        else _current_protected_main_commit()
    )
    config = load_config(commit)
    contract = _source_bound_contract(commit)
    _private_key, public_key = ensure_ssh_key(root)
    image = config["vm"]["image"]
    cloud_image = root / "downloads" / Path(image["url"]).name
    download(image["url"], image["sha256"], cloud_image)

    k3s = config["kubernetes"]
    k3s_binary = root / "downloads/k3s"
    download(
        k3s["binary_url"],
        k3s["binary_sha256"],
        k3s_binary,
        mode=0o755,
    )

    cloud_dir = root / "cloud-init"
    cloud = contract.render_cloud_init(public_key, cloud_dir, VM_NAME)
    image_info = json.loads(
        run(["qemu-img", "info", "--output=json", str(cloud_image)]).stdout
    )
    source_virtual_size = int(image_info.get("virtual-size", 0))
    if source_virtual_size <= 0:
        raise RuntimeErrorEB("cloud image virtual size is invalid")

    receipt = {
        "schema_version": 1,
        "status": "prepared",
        "config_sha256": _git_blob_sha256(commit, CONFIG_PATH),
        "cloud_image": str(cloud_image),
        "cloud_image_sha256": sha256_file(cloud_image),
        "cloud_image_virtual_size": source_virtual_size,
        "k3s_binary_sha256": sha256_file(k3s_binary),
        "ssh_public_key_sha256": sha256_file(public_key),
        "cloud_init": cloud,
    }
    atomic_json(root / "receipts/prepare.json", receipt)
    return receipt


def _libvirt_xml(*arguments: str) -> ET.Element:
    return ET.fromstring(run(["virsh", "-c", LIBVIRT_URI, *arguments]).stdout)


def _libvirt_volume_sha256(root: Path, volume_name: str) -> str:
    with tempfile.TemporaryDirectory(dir=root, prefix=".vm-substrate-") as tmp:
        downloaded = Path(tmp) / volume_name
        run([
            "virsh", "-c", LIBVIRT_URI, "vol-download", volume_name,
            str(downloaded), "--pool", POOL_NAME, "--sparse",
        ])
        return sha256_file(downloaded)


def _xml_bytes(element: ET.Element) -> int:
    units = {"bytes": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}
    return int(element.text or "0") * units[element.get("unit", "KiB")]


def _vm_definition(domain: ET.Element) -> dict[str, Any]:
    disks = domain.findall("./devices/disk[@device='disk']")
    interfaces = domain.findall("./devices/interface")
    if (
        len(disks) != 1 or len(interfaces) != 1
        or domain.findall("./devices/filesystem")
        or domain.findall("./devices/hostdev")
    ):
        raise RuntimeErrorEB("VM substrate requires one disk/NAT interface and no host mounts")
    disk, interface = disks[0], interfaces[0]
    source = disk.find("source")
    if disk.get("type") == "volume" and (
        source.get("pool") == POOL_NAME and source.get("volume") == VOLUME_NAME
    ):
        disk_path = str(POOL_TARGET / VOLUME_NAME)
    elif disk.get("type") == "file":
        disk_path = source.get("file")
    else:
        raise RuntimeErrorEB("VM substrate disk is not the Experiment-B volume")
    backing = disk.find("backingStore")
    if backing is not None and (
        backing.get("type") != "file"
        or backing.find("source").get("file") != str(POOL_TARGET / BASE_VOLUME)
        or backing.find("format").get("type") != "qcow2"
        or backing.find("backingStore/source") is not None
    ):
        raise RuntimeErrorEB("VM substrate libvirt backing relation drifted")
    vcpu = domain.find("vcpu")
    return {
        "vm": domain.findtext("name"),
        "uuid": domain.findtext("uuid"),
        "hypervisor": domain.get("type"),
        "vcpu": int(vcpu.text or "0"),
        "current_vcpu": int(vcpu.get("current", vcpu.text or "0")),
        "memory_bytes": _xml_bytes(domain.find("memory")),
        "current_memory_bytes": _xml_bytes(domain.find("currentMemory")),
        "interface_type": interface.get("type"),
        "network": interface.find("source").get("network"),
        "mac": interface.find("mac").get("address"),
        "disk_path": disk_path,
        "disk_format": disk.find("driver").get("type"),
        "disk_target": disk.find("target").get("dev"),
        "disk_bus": disk.find("target").get("bus"),
    }


def _validate_vm_substrate(substrate: Any, config: dict[str, Any]) -> None:
    vm = config["vm"]
    expected = {
        "vm": VM_NAME,
        "hypervisor": "kvm",
        "vcpu": vm["vcpu"],
        "current_vcpu": vm["vcpu"],
        "memory_bytes": vm["memory_mib"] * 1024**2,
        "current_memory_bytes": vm["memory_mib"] * 1024**2,
        "interface_type": "network",
        "network": vm["network"],
        "network_mode": "nat",
        "pool": POOL_NAME,
        "pool_type": "dir",
        "pool_target": str(POOL_TARGET),
        "volume": VOLUME_NAME,
        "disk_path": str(POOL_TARGET / VOLUME_NAME),
        "disk_key": str(POOL_TARGET / VOLUME_NAME),
        "disk_format": "qcow2",
        "disk_target": "vda",
        "disk_bus": "virtio",
        "disk_capacity_bytes": vm["disk_gib"] * 1024**3,
        "base_volume": BASE_VOLUME,
        "base_path": str(POOL_TARGET / BASE_VOLUME),
        "base_key": str(POOL_TARGET / BASE_VOLUME),
        "base_format": "qcow2",
        "base_image_sha256": vm["image"]["sha256"],
    }
    if not isinstance(substrate, dict):
        raise RuntimeErrorEB("VM substrate evidence is missing")
    for key, value in expected.items():
        if substrate.get(key) != value or type(substrate[key]) is not type(value):
            raise RuntimeErrorEB(f"VM substrate contract drifted: {key}")
    try:
        for key in ("uuid", "pool_uuid", "network_uuid"):
            if str(uuid.UUID(substrate[key])) != substrate[key]:
                raise ValueError(key)
        if not re.fullmatch(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}", substrate["mac"]):
            raise ValueError("mac")
        if not isinstance(substrate["network_bridge"], str) or not substrate["network_bridge"]:
            raise ValueError("network_bridge")
        for key in ("disk_device", "disk_inode"):
            if type(substrate[key]) is not int or substrate[key] < 0:
                raise ValueError(key)
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise RuntimeErrorEB("VM substrate identity is missing or invalid") from exc


def _live_vm_substrate(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Read the active guest and its actual QEMU backing chain, never cached prepare data."""
    try:
        domain = _libvirt_xml("dumpxml", VM_NAME)
        if int(domain.get("id", "-1")) < 0:
            raise RuntimeErrorEB("VM substrate domain is not active")
        substrate = _vm_definition(domain)
        if _vm_definition(_libvirt_xml("dumpxml", VM_NAME, "--inactive")) != substrate:
            raise RuntimeErrorEB("VM substrate live/persistent definitions differ")
        network = _libvirt_xml("net-dumpxml", config["vm"]["network"])
        network_bridge = network.find("bridge").get("name")
        if (
            network.findtext("name") != substrate["network"]
            or domain.find("./devices/interface/source").get("bridge") != network_bridge
        ):
            raise RuntimeErrorEB("VM substrate network attachment drifted")
        pool = _libvirt_xml("pool-dumpxml", POOL_NAME)
        disk = _libvirt_xml("vol-dumpxml", VOLUME_NAME, "--pool", POOL_NAME)
        base = _libvirt_xml("vol-dumpxml", BASE_VOLUME, "--pool", POOL_NAME)
        disk_path, base_path = str(POOL_TARGET / VOLUME_NAME), str(POOL_TARGET / BASE_VOLUME)
        if (
            disk.findtext("target/path") != disk_path
            or disk.findtext("backingStore/path") != base_path
            or disk.find("target/format").get("type") != "qcow2"
            or disk.find("backingStore/format").get("type") != "qcow2"
            or base.findtext("target/path") != base_path
            or base.find("backingStore/path") is not None
        ):
            raise RuntimeErrorEB("VM substrate volume/backing relation drifted")
        # QMP observes the running disk graph even when the host user cannot open
        # libvirt-owned images. It also detects live resize/backing-store overrides.
        blocks = json.loads(run([
            "virsh", "-c", LIBVIRT_URI, "qemu-monitor-command", VM_NAME,
            '{"execute":"query-block"}',
        ]).stdout)["return"]
        images = [
            block["inserted"]["image"] for block in blocks
            if not block.get("removable", False)
        ]
        if len(images) != 1:
            raise RuntimeErrorEB("VM substrate QEMU disk set drifted")
        image = images[0]
        backing = image.get("backing-image", {})
        if (
            image.get("filename") != disk_path or image.get("format") != "qcow2"
            or image.get("virtual-size") != config["vm"]["disk_gib"] * 1024**3
            or backing.get("filename") != base_path or backing.get("format") != "qcow2"
            or backing.get("backing-image") or backing.get("backing-filename")
        ):
            raise RuntimeErrorEB("VM substrate QEMU capacity/backing relation drifted")
        # Hash the uploaded base volume itself through libvirt, not the download
        # cache or the guest's mutable overlay; volume permissions stay unchanged.
        base_sha256 = _libvirt_volume_sha256(root, BASE_VOLUME)
        disk_stat = (POOL_TARGET / VOLUME_NAME).stat()
        substrate.update({
            "network_uuid": network.findtext("uuid"),
            "network_mode": network.find("forward").get("mode"),
            "network_bridge": network_bridge,
            "pool": pool.findtext("name"),
            "pool_uuid": pool.findtext("uuid"),
            "pool_type": pool.get("type"),
            "pool_target": pool.findtext("target/path"),
            "volume": disk.findtext("name"),
            "disk_key": disk.findtext("key"),
            "disk_capacity_bytes": _xml_bytes(disk.find("capacity")),
            "disk_device": disk_stat.st_dev,
            "disk_inode": disk_stat.st_ino,
            "base_volume": base.findtext("name"),
            "base_key": base.findtext("key"),
            "base_path": base.findtext("target/path"),
            "base_format": base.find("target/format").get("type"),
            "base_image_sha256": base_sha256,
        })
    except (ET.ParseError, ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
        raise RuntimeErrorEB("VM substrate readback is missing or malformed") from exc
    _validate_vm_substrate(substrate, config)
    return substrate


def _require_vm_create_receipt(
    receipt: Any, source_commit: str, config: dict[str, Any], root: Path
) -> None:
    if not isinstance(receipt, dict) or (
        receipt.get("schema_version") != 1
        or receipt.get("status") != "created"
        or receipt.get("source_commit") != source_commit
        or receipt.get("state_root") != str(root.resolve())
        or receipt.get("config_sha256") != sha256_file(CLUSTER / "config.json")
        or receipt.get("vm") != VM_NAME
        or receipt.get("pool") != POOL_NAME
        or receipt.get("volume") != VOLUME_NAME
        or receipt.get("network") != config["vm"]["network"]
    ):
        raise RuntimeErrorEB("VM creation receipt source/config binding drifted")
    _validate_vm_substrate(receipt.get("substrate"), config)


def _retire_domain_before_storage(
    target: str,
    context: str,
) -> None:
    state_result = run(
        ["virsh", "-c", LIBVIRT_URI, "domstate", target],
        check=False,
    )
    if state_result.returncode != 0:
        raise RuntimeErrorEB(
            f"{context} cannot prove the libvirt domain state"
        )
    state = " ".join(state_result.stdout.strip().casefold().split())
    if not state:
        raise RuntimeErrorEB(
            f"{context} returned an empty libvirt domain state"
        )
    if state != "shut off":
        destroyed = run(
            ["virsh", "-c", LIBVIRT_URI, "destroy", target],
            check=False,
        )
        if destroyed.returncode != 0:
            raise RuntimeErrorEB(
                f"{context} could not stop the libvirt domain; "
                "storage cleanup is forbidden"
            )
        state_result = run(
            ["virsh", "-c", LIBVIRT_URI, "domstate", target],
            check=False,
        )
        if state_result.returncode != 0:
            raise RuntimeErrorEB(
                f"{context} cannot prove the stopped libvirt domain state"
            )
        state = " ".join(
            state_result.stdout.strip().casefold().split()
        )
        if state != "shut off":
            raise RuntimeErrorEB(
                f"{context} libvirt domain is still active after destroy"
            )

    undefine = run(
        ["virsh", "-c", LIBVIRT_URI, "undefine", target, "--nvram"],
        check=False,
    )
    if undefine.returncode != 0:
        undefine = run(
            ["virsh", "-c", LIBVIRT_URI, "undefine", target],
            check=False,
        )
    if undefine.returncode != 0:
        raise RuntimeErrorEB(
            f"{context} could not undefine the stopped libvirt domain"
        )
    if run(
        ["virsh", "-c", LIBVIRT_URI, "dominfo", target],
        check=False,
    ).returncode == 0:
        raise RuntimeErrorEB(
            f"{context} libvirt domain still exists after undefine"
        )


def _cleanup_pool_after_domain_retirement(
    pool_target: str,
    context: str,
) -> None:
    volume_paths = {
        VOLUME_NAME: POOL_TARGET / VOLUME_NAME,
        BASE_VOLUME: POOL_TARGET / BASE_VOLUME,
    }
    for volume_name, path in volume_paths.items():
        if not path.exists():
            continue
        last_result: subprocess.CompletedProcess[str] | None = None
        for attempt in range(3):
            last_result = run(
                [
                    "virsh",
                    "-c",
                    LIBVIRT_URI,
                    "vol-delete",
                    volume_name,
                    "--pool",
                    pool_target,
                ],
                check=False,
            )
            if not path.exists():
                break
            if attempt < 2:
                time.sleep(1)
        if path.exists():
            detail = (
                ((last_result.stderr if last_result is not None else "") or "")
                .strip()
            )
            raise RuntimeErrorEB(
                f"{context} could not remove libvirt volume {volume_name}: "
                f"{detail[-1000:]}"
            )

    if POOL_TARGET.exists():
        unexpected = sorted(path.name for path in POOL_TARGET.iterdir())
        if unexpected:
            raise RuntimeErrorEB(
                f"{context} refuses unexpected libvirt pool contents: {unexpected}"
            )

    last_results: dict[str, subprocess.CompletedProcess[str]] = {}
    for attempt in range(3):
        if not _libvirt_resource_present("pool", POOL_NAME):
            break
        for command in ("pool-destroy", "pool-delete", "pool-undefine"):
            result = run(
                ["virsh", "-c", LIBVIRT_URI, command, pool_target],
                check=False,
            )
            last_results[command] = result
            if not _libvirt_resource_present("pool", POOL_NAME):
                break
        if not _libvirt_resource_present("pool", POOL_NAME):
            break
        if attempt < 2:
            time.sleep(1)

    if _libvirt_resource_present("pool", POOL_NAME):
        detail = "; ".join(
            f"{command}=rc{result.returncode}:"
            f"{((result.stderr or '').strip())[-300:]}"
            for command, result in sorted(last_results.items())
        )
        raise RuntimeErrorEB(
            f"{context} could not retire the libvirt storage pool after "
            f"bounded retries: {detail}"
        )

    for volume_name, path in volume_paths.items():
        if path.exists():
            raise RuntimeErrorEB(
                f"{context} volume path still exists after pool cleanup: "
                f"{volume_name}"
            )
    if POOL_TARGET.exists():
        if any(POOL_TARGET.iterdir()):
            raise RuntimeErrorEB(
                f"{context} libvirt pool directory is not empty after cleanup"
            )
        POOL_TARGET.rmdir()


@_serialize_experiment_b_lifecycle
def create_vm(root: Path) -> dict[str, Any]:
    if run(
        ["virsh", "-c", LIBVIRT_URI, "dominfo", VM_NAME],
        check=False,
    ).returncode == 0:
        raise RuntimeErrorEB(
            "Experiment-B VM already exists; refusing implicit replacement"
        )
    if run(
        ["virsh", "-c", LIBVIRT_URI, "pool-info", POOL_NAME],
        check=False,
    ).returncode == 0:
        raise RuntimeErrorEB(
            "Experiment-B libvirt pool already exists; run bounded teardown first"
        )

    RETIREMENT_RECEIPT.unlink(missing_ok=True)
    _retirement_attempt_path().unlink(missing_ok=True)
    _invalidate_receipts(root, VM_ATTEMPT_INVALIDATES)
    source_commit = _current_protected_main_commit()
    config = load_config(source_commit)
    config_sha256 = _git_blob_sha256(source_commit, CONFIG_PATH)
    root_identity = str(root.resolve())
    attempt_path = root / "receipts/vm-create-attempt.json"
    domain_target = str(uuid.uuid4())
    attempt = {
        "schema_version": 1,
        "status": "running",
        "source_commit": source_commit,
        "config_sha256": config_sha256,
        "state_root": root_identity,
        "vm": VM_NAME,
        "pool": POOL_NAME,
        "domain_target": domain_target,
    }
    atomic_json(
        attempt_path,
        attempt,
    )

    prepared = prepare(root, source_commit)
    qemu_uid, qemu_gid = _libvirt_qemu_identity()
    cloud_image = Path(prepared["cloud_image"])
    source_virtual_size = int(prepared["cloud_image_virtual_size"])
    pool_fd = _open_libvirt_pool_target(create=True)
    try:
        if os.listdir(pool_fd):
            raise RuntimeErrorEB(
                "Experiment-B libvirt pool target already contains files"
            )
        pool_target_stat = os.fstat(pool_fd)
        attempt.update(
            pool_target_device=pool_target_stat.st_dev,
            pool_target_inode=pool_target_stat.st_ino,
        )
        atomic_json(attempt_path, attempt)
    finally:
        os.close(pool_fd)

    pool_defined = False
    try:
        run(
            [
                "virsh", "-c", LIBVIRT_URI, "pool-define-as",
                POOL_NAME, "dir", "--target", str(POOL_TARGET),
            ]
        )
        pool_defined = True
        run(["virsh", "-c", LIBVIRT_URI, "pool-build", POOL_NAME])
        run(["virsh", "-c", LIBVIRT_URI, "pool-start", POOL_NAME])
        attempt["pool_target"] = _libvirt_resource_uuid("pool", POOL_NAME)
        atomic_json(attempt_path, attempt)
        _create_libvirt_volume(
            BASE_VOLUME,
            source_virtual_size,
            qemu_uid=qemu_uid,
            qemu_gid=qemu_gid,
        )
        run(
            [
                "virsh", "-c", LIBVIRT_URI, "vol-upload",
                BASE_VOLUME, str(cloud_image), "--pool", POOL_NAME, "--sparse",
            ],
            timeout=300,
        )
        if (
            _libvirt_volume_sha256(root, BASE_VOLUME)
            != str(config["vm"]["image"]["sha256"])
        ):
            raise RuntimeErrorEB(
                "uploaded cloud image digest drifted before VM boot"
            )
        attempt["base_image_sha256"] = str(config["vm"]["image"]["sha256"])
        atomic_json(attempt_path, attempt)
        run(["virsh", "-c", LIBVIRT_URI, "pool-refresh", POOL_NAME])
        volume_stat = _create_libvirt_volume(
            VOLUME_NAME,
            int(config["vm"]["disk_gib"]) * 1024**3,
            qemu_uid=qemu_uid,
            qemu_gid=qemu_gid,
            backing_path=POOL_TARGET / BASE_VOLUME,
        )
        attempt.update(
            volume_device=volume_stat.st_dev,
            volume_inode=volume_stat.st_ino,
        )
        atomic_json(attempt_path, attempt)
        cloud_init = prepared.get("cloud_init")
        user_data_path = root / "cloud-init/user-data.yaml"
        meta_data_path = root / "cloud-init/meta-data.yaml"
        if (
            not isinstance(cloud_init, dict)
            or cloud_init.get("user_data") != str(user_data_path)
            or cloud_init.get("meta_data") != str(meta_data_path)
            or not isinstance(cloud_init.get("user_data_sha256"), str)
            or re.fullmatch(
                r"[0-9a-f]{64}", str(cloud_init["user_data_sha256"])
            )
            is None
            or not isinstance(cloud_init.get("meta_data_sha256"), str)
            or re.fullmatch(
                r"[0-9a-f]{64}", str(cloud_init["meta_data_sha256"])
            )
            is None
        ):
            raise RuntimeErrorEB("prepared cloud-init binding is invalid")
        user_data_sha256 = str(cloud_init["user_data_sha256"])
        meta_data_sha256 = str(cloud_init["meta_data_sha256"])
        attempt["cloud_init_sha256"] = {
            "user_data": user_data_sha256,
            "meta_data": meta_data_sha256,
        }
        atomic_json(attempt_path, attempt)
        virt_install_argv = [
            "virt-install",
            "--connect", LIBVIRT_URI,
            "--name", VM_NAME,
            "--uuid", domain_target,
            "--memory", str(config["vm"]["memory_mib"]),
            "--vcpus", str(config["vm"]["vcpu"]),
            "--import",
            "--disk", f"vol={POOL_NAME}/{VOLUME_NAME},bus=virtio",
            "--network", f"network={config['vm']['network']},model=virtio",
            "--graphics", "none",
            "--noautoconsole",
            "--os-variant", config["vm"]["os_variant"],
        ]
        with (
            _verified_snapshot_fd(
                user_data_path,
                user_data_sha256,
                "prepared cloud-init user-data",
            ) as user_data_fd,
            _verified_snapshot_fd(
                meta_data_path,
                meta_data_sha256,
                "prepared cloud-init meta-data",
            ) as meta_data_fd,
        ):
            run(
                [
                    *virt_install_argv,
                    "--cloud-init",
                    (
                        f"user-data=/proc/self/fd/{user_data_fd},"
                        f"meta-data=/proc/self/fd/{meta_data_fd}"
                    ),
                ],
                timeout=120,
                pass_fds=(user_data_fd, meta_data_fd),
            )
        if _libvirt_resource_uuid("domain", VM_NAME) != domain_target:
            raise RuntimeErrorEB(
                "created VM UUID differs from the persisted creation identity"
            )
        substrate = _live_vm_substrate(root, config)
        if (
            substrate.get("uuid") != domain_target
            or substrate.get("pool_uuid") != attempt["pool_target"]
        ):
            raise RuntimeErrorEB("created VM substrate identity drifted from attempt")
        attempt["substrate_sha256"] = _stable_json_sha256(substrate)
        atomic_json(attempt_path, attempt)
        if (
            _current_protected_main_commit() != source_commit
            or sha256_file(CONFIG_PATH) != config_sha256
        ):
            raise RuntimeErrorEB("VM creation source/config changed during creation")
        receipt = {
            "schema_version": 1,
            "status": "created",
            "source_commit": source_commit,
            "state_root": root_identity,
            "config_sha256": config_sha256,
            "vm": VM_NAME,
            "pool": POOL_NAME,
            "volume": VOLUME_NAME,
            "network": config["vm"]["network"],
            "substrate": substrate,
        }
        atomic_json(root / "receipts/vm-create.json", receipt)
    except Exception:
        if run(
            ["virsh", "-c", LIBVIRT_URI, "dominfo", VM_NAME],
            check=False,
        ).returncode == 0:
            if _libvirt_resource_uuid("domain", VM_NAME) != domain_target:
                raise RuntimeErrorEB(
                    "Experiment-B VM creation rollback domain UUID drifted; "
                    "storage cleanup is forbidden"
                )
            _retire_domain_before_storage(
                domain_target,
                "Experiment-B VM creation rollback",
            )
        if pool_defined:
            pool_target = attempt.get("pool_target")
            if not isinstance(pool_target, str):
                raise RuntimeErrorEB(
                    "Experiment-B VM creation rollback has no verified pool UUID; "
                    "storage cleanup is forbidden"
                )
            if _libvirt_resource_uuid("pool", POOL_NAME) != pool_target:
                raise RuntimeErrorEB(
                    "Experiment-B VM creation rollback pool UUID drifted; "
                    "storage cleanup is forbidden"
                )
            _cleanup_pool_after_domain_retirement(
                pool_target,
                "Experiment-B VM creation rollback",
            )
        if POOL_TARGET.exists() and not any(POOL_TARGET.iterdir()):
            POOL_TARGET.rmdir()
        raise

    return receipt


def vm_ip() -> str:
    for _ in range(120):
        result = run(
            ["virsh", "-c", LIBVIRT_URI, "domifaddr", VM_NAME, "--source", "lease"],
            check=False,
        )
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 4 and "/" in fields[-1]:
                address = fields[-1].split("/", 1)[0]
                try:
                    parsed = ipaddress.ip_address(address)
                except ValueError:
                    continue
                if parsed.version == 4 and parsed.is_private:
                    return address
        time.sleep(2)
    raise RuntimeErrorEB("VM did not receive a libvirt NAT IPv4 lease")


def ssh_argv(root: Path, ip: str) -> list[str]:
    known_hosts = root / "ssh/known_hosts"
    return [
        "ssh",
        "-i", str(root / "ssh/id_ed25519"),
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        f"commonthing@{ip}",
    ]


def wait_ssh(root: Path, ip: str) -> None:
    for _ in range(90):
        result = run([*ssh_argv(root, ip), "true"], check=False)
        if result.returncode == 0:
            return
        time.sleep(2)
    raise RuntimeErrorEB("SSH did not become ready")


def scp_to(root: Path, ip: str, source: Path, destination: str) -> None:
    known_hosts = root / "ssh/known_hosts"
    run(
        [
            "scp",
            "-i", str(root / "ssh/id_ed25519"),
            "-o", f"UserKnownHostsFile={known_hosts}",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "BatchMode=yes",
            str(source),
            f"commonthing@{ip}:{destination}",
        ]
    )


def scp_fd_to(
    root: Path,
    ip: str,
    source_fd: int,
    destination: str,
) -> None:
    known_hosts = root / "ssh/known_hosts"
    run(
        [
            "scp",
            "-i", str(root / "ssh/id_ed25519"),
            "-o", f"UserKnownHostsFile={known_hosts}",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "BatchMode=yes",
            _reopenable_proc_fd_path(source_fd),
            f"commonthing@{ip}:{destination}",
        ],
        timeout=900,
        pass_fds=(source_fd,),
    )


def _open_verified_file(
    path: Path, expected_sha256: str, context: str
) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    cloexec = getattr(os, "O_CLOEXEC", None)
    if not isinstance(nofollow, int) or not isinstance(cloexec, int):
        raise RuntimeErrorEB(f"{context} cannot be opened safely")
    try:
        file_fd = os.open(path, os.O_RDONLY | cloexec | nofollow)
    except OSError as exc:
        raise RuntimeErrorEB(f"{context} is missing or unsafe") from exc
    try:
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeErrorEB(f"{context} is not a regular file")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(file_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        if digest.hexdigest() != expected_sha256:
            raise RuntimeErrorEB(
                f"{context} digest does not match expected source"
            )
        os.lseek(file_fd, 0, os.SEEK_SET)
    except Exception:
        os.close(file_fd)
        raise
    return file_fd



def _create_sealed_snapshot_fd(
    payload: bytes,
    context: str,
    *,
    mode: int = 0o400,
) -> int:
    if not isinstance(payload, bytes):
        raise RuntimeErrorEB(f"{context} immutable snapshot payload is invalid")
    if not isinstance(mode, int) or mode < 0 or mode > 0o777:
        raise RuntimeErrorEB(f"{context} immutable snapshot mode is invalid")
    if not sys.platform.startswith("linux"):
        raise RuntimeErrorEB(f"{context} cannot create an immutable snapshot")
    memfd_create = getattr(os, "memfd_create", None)
    allow_sealing = int(getattr(os, "MFD_ALLOW_SEALING", 0x0002))
    cloexec = int(getattr(os, "MFD_CLOEXEC", 0x0001))
    try:
        if callable(memfd_create):
            snapshot_fd = memfd_create(
                "commonthing-experiment-b-snapshot",
                flags=allow_sealing | cloexec,
            )
        else:
            libc = ctypes.CDLL(None, use_errno=True)
            native_memfd_create = libc.memfd_create
            native_memfd_create.argtypes = [ctypes.c_char_p, ctypes.c_uint]
            native_memfd_create.restype = ctypes.c_int
            snapshot_fd = native_memfd_create(
                b"commonthing-experiment-b-snapshot",
                allow_sealing | cloexec,
            )
            if snapshot_fd < 0:
                error_number = ctypes.get_errno()
                raise OSError(error_number, os.strerror(error_number))
    except (AttributeError, OSError) as exc:
        raise RuntimeErrorEB(
            f"{context} cannot create an immutable snapshot"
        ) from exc
    f_add_seals = int(getattr(fcntl, "F_ADD_SEALS", 1033))
    f_get_seals = int(getattr(fcntl, "F_GET_SEALS", 1034))
    f_seal_seal = int(getattr(fcntl, "F_SEAL_SEAL", 0x0001))
    f_seal_shrink = int(getattr(fcntl, "F_SEAL_SHRINK", 0x0002))
    f_seal_grow = int(getattr(fcntl, "F_SEAL_GROW", 0x0004))
    f_seal_write = int(getattr(fcntl, "F_SEAL_WRITE", 0x0008))
    try:
        view = memoryview(payload)
        offset = 0
        while offset < len(view):
            written = os.write(snapshot_fd, view[offset:])
            if written <= 0:
                raise RuntimeErrorEB(
                    f"{context} immutable snapshot write failed"
                )
            offset += written
        os.fchmod(snapshot_fd, mode)
        required_seals = (
            f_seal_seal
            | f_seal_shrink
            | f_seal_grow
            | f_seal_write
        )
        fcntl.fcntl(snapshot_fd, f_add_seals, required_seals)
        observed_seals = fcntl.fcntl(snapshot_fd, f_get_seals)
        if observed_seals & required_seals != required_seals:
            raise RuntimeErrorEB(
                f"{context} immutable snapshot sealing failed"
            )
        os.lseek(snapshot_fd, 0, os.SEEK_SET)
        if os.pread(snapshot_fd, len(payload), 0) != payload:
            raise RuntimeErrorEB(
                f"{context} immutable snapshot verification failed"
            )
        return snapshot_fd
    except Exception:
        os.close(snapshot_fd)
        raise


@contextmanager
def _sealed_snapshot_fd(
    payload: bytes,
    context: str,
    *,
    mode: int = 0o400,
) -> Iterator[int]:
    snapshot_fd = _create_sealed_snapshot_fd(
        payload,
        context,
        mode=mode,
    )
    try:
        yield snapshot_fd
    finally:
        os.close(snapshot_fd)


@contextmanager
def _verified_snapshot_fd(
    path: Path,
    expected_sha256: str,
    context: str,
) -> Iterator[int]:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    cloexec = getattr(os, "O_CLOEXEC", None)
    if not isinstance(nofollow, int) or not isinstance(cloexec, int):
        raise RuntimeErrorEB(f"{context} cannot be opened safely")
    try:
        source_fd = os.open(path, os.O_RDONLY | cloexec | nofollow)
    except OSError as exc:
        raise RuntimeErrorEB(f"{context} is missing or unsafe") from exc
    try:
        metadata = os.fstat(source_fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeErrorEB(f"{context} is not a regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        payload = b"".join(chunks)
    finally:
        os.close(source_fd)
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise RuntimeErrorEB(
            f"{context} digest does not match prepared source"
        )
    with _sealed_snapshot_fd(payload, context) as snapshot_fd:
        yield snapshot_fd


def _source_commit_kustomize_build(
    root: Path,
    source_commit: str,
    target: Path,
    snapshot_root: Path,
) -> str:
    if COMMIT_RE.fullmatch(source_commit) is None:
        raise RuntimeErrorEB(
            "source-commit kustomize render requires an exact commit"
        )
    try:
        target_relative = target.relative_to(ROOT)
        target.relative_to(snapshot_root)
        snapshot_root.relative_to(ROOT)
    except ValueError as exc:
        raise RuntimeErrorEB(
            "source-commit kustomize render target escapes its snapshot root"
        ) from exc

    paths = _git_tree_regular_blob_paths(source_commit, snapshot_root)
    if target_relative / "kustomization.yaml" not in {
        path for path in paths
    }:
        raise RuntimeErrorEB(
            "source-commit kustomize render has no bound kustomization"
        )
    toolchain_receipt = toolchain(root, source_commit)
    kustomize = toolchain_receipt.get("tools", {}).get("kustomize")
    if not isinstance(kustomize, str) or not kustomize:
        raise RuntimeErrorEB(
            "source-commit kustomize render requires pinned kustomize"
        )
    bwrap = require_binary("bwrap")
    sandbox_root = Path("/tmp/commonthing-source-snapshot")
    directory_paths: set[Path] = {sandbox_root}
    for relative in paths:
        parent = relative.parent
        while parent != Path("."):
            directory_paths.add(sandbox_root / parent)
            parent = parent.parent

    argv = [
        bwrap,
        "--unshare-all",
        "--share-net",
        "--die-with-parent",
        "--ro-bind",
        "/",
        "/",
        "--tmpfs",
        "/tmp",
    ]
    for directory in sorted(
        directory_paths,
        key=lambda value: (len(value.parts), value.as_posix()),
    ):
        argv.extend(["--dir", directory.as_posix()])

    snapshot_fds: list[int] = []
    with ExitStack() as stack:
        for relative in paths:
            payload = _git_blob_bytes(source_commit, ROOT / relative)
            snapshot_fd = stack.enter_context(
                _sealed_snapshot_fd(
                    payload,
                    f"source-commit kustomize blob {relative.as_posix()}",
                )
            )
            snapshot_fds.append(snapshot_fd)
            argv.extend(
                [
                    "--ro-bind-data",
                    str(snapshot_fd),
                    (sandbox_root / relative).as_posix(),
                ]
            )
        argv.extend(
            [
                "--chdir",
                sandbox_root.as_posix(),
                "--",
                kustomize,
                "build",
                (sandbox_root / target_relative).as_posix(),
            ]
        )
        result = run(
            argv,
            timeout=120,
            pass_fds=tuple(snapshot_fds),
        )
    return result.stdout




def _verified_snapshot_bytes(
    path: Path,
    expected_sha256: str,
    context: str,
) -> bytes:
    with _verified_snapshot_fd(path, expected_sha256, context) as snapshot_fd:
        size = os.fstat(snapshot_fd).st_size
        payload = os.pread(snapshot_fd, size, 0)
    if len(payload) != size:
        raise RuntimeErrorEB(f"{context} snapshot read was incomplete")
    return payload


def _open_verified_k3s_binary(path: Path, expected_sha256: str) -> int:
    return _open_verified_file(
        path, expected_sha256, "prepared k3s binary"
    )


def _k3s_contract_paths(config: dict[str, Any]) -> tuple[Path, Path]:
    binding = config.get("runtime_binding", {})
    if not isinstance(binding, dict):
        raise RuntimeErrorEB("Experiment-B runtime binding is invalid")
    config_value = binding.get("k3s_config")
    service_value = binding.get("k3s_service")
    if not isinstance(config_value, str) or not isinstance(service_value, str):
        raise RuntimeErrorEB("Experiment-B k3s file binding is invalid")
    config_path = ROOT / config_value
    service_path = ROOT / service_value
    if (
        config_path != CLUSTER / "k3s-config.yaml"
        or service_path != CLUSTER / "k3s.service"
        or not config_path.is_file()
        or not service_path.is_file()
        or config_path.is_symlink()
        or service_path.is_symlink()
    ):
        raise RuntimeErrorEB("Experiment-B k3s file binding drifted")
    return config_path, service_path


def _kubeconfig_server_payload(payload: bytes) -> str:
    try:
        payload = yaml.safe_load(payload.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise RuntimeErrorEB("Experiment-B kubeconfig is invalid") from exc
    if not isinstance(payload, dict):
        raise RuntimeErrorEB("Experiment-B kubeconfig is invalid")
    current = payload.get("current-context")
    contexts = payload.get("contexts")
    clusters = payload.get("clusters")
    if (
        not isinstance(current, str)
        or not current
        or not isinstance(contexts, list)
        or not isinstance(clusters, list)
    ):
        raise RuntimeErrorEB("Experiment-B kubeconfig context binding is invalid")
    matching_contexts = [
        item
        for item in contexts
        if isinstance(item, dict) and item.get("name") == current
    ]
    if len(matching_contexts) != 1:
        raise RuntimeErrorEB("Experiment-B kubeconfig current context is ambiguous")
    context_value = matching_contexts[0].get("context")
    cluster_name = (
        context_value.get("cluster")
        if isinstance(context_value, dict)
        else None
    )
    matching_clusters = [
        item
        for item in clusters
        if isinstance(item, dict) and item.get("name") == cluster_name
    ]
    if len(matching_clusters) != 1:
        raise RuntimeErrorEB("Experiment-B kubeconfig cluster binding is ambiguous")
    cluster_value = matching_clusters[0].get("cluster")
    server = (
        cluster_value.get("server")
        if isinstance(cluster_value, dict)
        else None
    )
    if not isinstance(server, str) or not server:
        raise RuntimeErrorEB("Experiment-B kubeconfig server binding is invalid")
    return server


def _kubeconfig_server(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        raise RuntimeErrorEB("Experiment-B kubeconfig must be a regular file")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise RuntimeErrorEB("Experiment-B kubeconfig is invalid") from exc
    return _kubeconfig_server_payload(payload)


def _require_kubernetes_target_binding(
    root: Path,
    source_commit: str | None = None,
) -> tuple[dict[str, Any], str, str]:
    receipt_path = root / "receipts/k3s.json"
    kubeconfig_path = root / "kubeconfig.yaml"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeErrorEB(
            "Experiment-B Kubernetes target binding requires valid k3s.json"
        ) from exc
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema_version") != 1
        or receipt.get("status") != "ready"
        or (
            source_commit is not None
            and receipt.get("source_commit") != source_commit
        )
        or not isinstance(receipt.get("vm_ip"), str)
        or not receipt.get("vm_ip")
        or not isinstance(receipt.get("kubeconfig_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", receipt["kubeconfig_sha256"]) is None
    ):
        raise RuntimeErrorEB("Experiment-B Kubernetes target receipt binding drifted")

    live_ip = vm_ip()
    if receipt.get("vm_ip") != live_ip:
        raise RuntimeErrorEB("Experiment-B k3s VM address drifted")
    if (
        not kubeconfig_path.is_file()
        or kubeconfig_path.is_symlink()
        or sha256_file(kubeconfig_path) != receipt["kubeconfig_sha256"]
        or (kubeconfig_path.stat().st_mode & 0o777) != 0o600
    ):
        raise RuntimeErrorEB("Experiment-B kubeconfig digest/mode drifted")
    expected_server = f"https://{live_ip}:6443"
    if _kubeconfig_server(kubeconfig_path) != expected_server:
        raise RuntimeErrorEB(
            "Experiment-B kubeconfig is not bound to the VM API server"
        )
    return receipt, live_ip, expected_server


def _kubernetes_target_identity(
    root: Path,
    source_commit: str,
) -> dict[str, str]:
    receipt, live_ip, expected_server = _require_kubernetes_target_binding(
        root, source_commit
    )
    return {
        "vm_ip": live_ip,
        "kubeconfig_sha256": str(receipt["kubeconfig_sha256"]),
        "server": expected_server,
    }


def _require_same_kubernetes_target(
    root: Path,
    source_commit: str,
    expected: dict[str, str],
    context: str,
) -> dict[str, str]:
    observed = _kubernetes_target_identity(root, source_commit)
    if observed != expected:
        raise RuntimeErrorEB(
            f"Kubernetes target identity changed during {context}"
        )
    return observed


def _parse_sha256sum_output(
    stdout: str, expected_paths: tuple[str, ...]
) -> dict[str, str]:
    observed: dict[str, str] = {}
    for raw_line in stdout.splitlines():
        fields = raw_line.strip().split(maxsplit=1)
        if len(fields) != 2:
            raise RuntimeErrorEB("guest k3s file digest output is malformed")
        digest, path = fields
        path = path.lstrip("*")
        if (
            re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or path not in expected_paths
            or path in observed
        ):
            raise RuntimeErrorEB("guest k3s file digest output is invalid")
        observed[path] = digest
    if set(observed) != set(expected_paths):
        raise RuntimeErrorEB("guest k3s file digest set is incomplete")
    return observed


def _parse_systemctl_properties(stdout: str) -> dict[str, str]:
    properties: dict[str, str] = {}
    for raw_line in stdout.splitlines():
        if "=" not in raw_line:
            raise RuntimeErrorEB("k3s systemd readback is malformed")
        key, value = raw_line.split("=", 1)
        if not key or key in properties:
            raise RuntimeErrorEB("k3s systemd readback is invalid")
        properties[key] = value
    expected = {
        "LoadState",
        "ActiveState",
        "SubState",
        "UnitFileState",
        "FragmentPath",
        "DropInPaths",
        "MainPID",
    }
    if set(properties) != expected:
        raise RuntimeErrorEB("k3s systemd readback is incomplete")
    return properties


def _k3s_process_identity_is_supported(process_exe: Any, process_argv: Any) -> bool:
    if process_exe == "/usr/local/bin/k3s":
        return process_argv == ["/usr/local/bin/k3s", "server"]
    return (
        isinstance(process_exe, str)
        and re.fullmatch(
            r"/var/lib/rancher/k3s/data/[0-9a-f]{64}/bin/k3s",
            process_exe,
        ) is not None
        and process_argv == ["/usr/local/bin/k3s server"]
    )


def _require_live_k3s_runtime(
    root: Path,
    config: dict[str, Any],
    source_commit: str,
) -> dict[str, Any]:
    receipt, live_ip, expected_server = _require_kubernetes_target_binding(
        root, source_commit
    )
    config_path, service_path = _k3s_contract_paths(config)
    expected_binary_sha256 = str(config["kubernetes"]["binary_sha256"])
    expected_config_sha256 = _git_blob_sha256(source_commit, config_path)
    expected_service_sha256 = _git_blob_sha256(source_commit, service_path)
    if (
        receipt.get("binary_sha256") != expected_binary_sha256
        or receipt.get("config_sha256") != expected_config_sha256
        or receipt.get("service_sha256") != expected_service_sha256
        or str(config["kubernetes"]["version"])
        not in str(receipt.get("k3s_version", ""))
        or receipt.get("live_kubelet_version")
        != str(config["kubernetes"]["version"])
    ):
        raise RuntimeErrorEB("Experiment-B k3s receipt binding drifted")

    guest_paths = (
        "/usr/local/bin/k3s",
        "/etc/rancher/k3s/config.yaml",
        "/etc/systemd/system/k3s.service",
    )
    digest_result = run(
        [
            *ssh_argv(root, live_ip),
            "sudo",
            "sha256sum",
            "--",
            *guest_paths,
        ],
        timeout=30,
    )
    guest_digests = _parse_sha256sum_output(
        digest_result.stdout, guest_paths
    )
    expected_guest_digests = {
        "/usr/local/bin/k3s": expected_binary_sha256,
        "/etc/rancher/k3s/config.yaml": expected_config_sha256,
        "/etc/systemd/system/k3s.service": expected_service_sha256,
    }
    if guest_digests != expected_guest_digests:
        raise RuntimeErrorEB("installed k3s files drifted from the pinned contract")

    systemctl = run(
        [
            *ssh_argv(root, live_ip),
            "sudo",
            "systemctl",
            "show",
            "k3s.service",
            "--no-pager",
            "--property=LoadState",
            "--property=ActiveState",
            "--property=SubState",
            "--property=UnitFileState",
            "--property=FragmentPath",
            "--property=DropInPaths",
            "--property=MainPID",
        ],
        timeout=30,
    )
    properties = _parse_systemctl_properties(systemctl.stdout)
    try:
        main_pid = int(properties["MainPID"])
    except (TypeError, ValueError) as exc:
        raise RuntimeErrorEB("k3s systemd MainPID is invalid") from exc
    if (
        properties["LoadState"] != "loaded"
        or properties["ActiveState"] != "active"
        or properties["SubState"] != "running"
        or properties["UnitFileState"] != "enabled"
        or properties["FragmentPath"] != "/etc/systemd/system/k3s.service"
        or properties["DropInPaths"] != ""
        or main_pid <= 0
    ):
        raise RuntimeErrorEB("k3s systemd service is not the pinned active unit")
    process_exe = run(
        [
            *ssh_argv(root, live_ip),
            "sudo",
            "readlink",
            "-f",
            f"/proc/{main_pid}/exe",
        ],
        timeout=30,
    ).stdout.strip()
    process_cmdline = run(
        [
            *ssh_argv(root, live_ip),
            "sudo",
            "cat",
            f"/proc/{main_pid}/cmdline",
        ],
        timeout=30,
    ).stdout
    argv = [value for value in process_cmdline.split("\0") if value]
    if not _k3s_process_identity_is_supported(process_exe, argv):
        raise RuntimeErrorEB("active k3s process identity drifted")
    reexec_binary_sha256: str | None = None
    if process_exe != "/usr/local/bin/k3s":
        # The pinned k3s launcher reexecs its packaged server from data/current.
        current_exe = run(
            [
                *ssh_argv(root, live_ip),
                "sudo",
                "readlink",
                "-f",
                "/var/lib/rancher/k3s/data/current/bin/k3s",
            ],
            timeout=30,
        ).stdout.strip()
        if current_exe != process_exe:
            raise RuntimeErrorEB("active k3s reexec target drifted")
        process_path = f"/proc/{main_pid}/exe"
        reexec_digests = _parse_sha256sum_output(
            run(
                [*ssh_argv(root, live_ip), "sudo", "sha256sum", "--", process_path],
                timeout=30,
            ).stdout,
            (process_path,),
        )
        reexec_binary_sha256 = reexec_digests[process_path]
        if reexec_binary_sha256 != config["kubernetes"]["reexec_binary_sha256"]:
            raise RuntimeErrorEB("active k3s reexec binary digest drifted")
    process_environment = run(
        [
            *ssh_argv(root, live_ip),
            "sudo",
            "cat",
            f"/proc/{main_pid}/environ",
        ],
        timeout=30,
    ).stdout
    environment_entries = [
        value for value in process_environment.split("\0") if value
    ]
    if any(
        entry.split("=", 1)[0].startswith("K3S_")
        for entry in environment_entries
        if "=" in entry
    ):
        raise RuntimeErrorEB("active k3s process has unexpected K3S environment overrides")

    return {
        "vm_ip": live_ip,
        "kubeconfig_sha256": receipt["kubeconfig_sha256"],
        "kubeconfig_server": expected_server,
        "binary_sha256": expected_binary_sha256,
        "config_sha256": expected_config_sha256,
        "service_sha256": expected_service_sha256,
        "service_active": True,
        "service_substate": "running",
        "unit_file_state": "enabled",
        "fragment_path": "/etc/systemd/system/k3s.service",
        "drop_ins_absent": True,
        "main_pid": main_pid,
        "process_exe": process_exe,
        "process_argv": argv,
        "reexec_binary_sha256": reexec_binary_sha256,
        "reexec_current_target_verified": reexec_binary_sha256 is not None,
        "environment_overrides_absent": True,
    }


@_serialize_experiment_b_lifecycle
def install_k3s(root: Path) -> dict[str, Any]:
    _invalidate_receipts(root, K3S_ATTEMPT_INVALIDATES)
    source_commit = _current_protected_main_commit()
    config = load_config(source_commit)
    vm_create_path = root / "receipts/vm-create.json"
    try:
        vm_create = json.loads(vm_create_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeErrorEB(
            "k3s installation requires a valid VM creation receipt"
        ) from exc
    _require_vm_create_receipt(vm_create, source_commit, config, root)
    live_substrate = _live_vm_substrate(root, config)
    if live_substrate != vm_create["substrate"]:
        raise RuntimeErrorEB(
            "k3s installation refuses VM substrate drift from creation receipt"
        )
    ip = vm_ip()
    wait_ssh(root, ip)
    k3s_binary = root / "downloads/k3s"
    expected_k3s_sha256 = str(config["kubernetes"]["binary_sha256"])
    k3s_fd = _open_verified_k3s_binary(k3s_binary, expected_k3s_sha256)
    try:
        config_path, service_path = _k3s_contract_paths(config)
        expected_config_sha256 = _git_blob_sha256(source_commit, config_path)
        expected_service_sha256 = _git_blob_sha256(source_commit, service_path)
        scp_fd_to(root, ip, k3s_fd, "/tmp/k3s")
    finally:
        os.close(k3s_fd)

    for source, expected_sha256, context, destination in (
        (
            config_path,
            expected_config_sha256,
            "k3s config source",
            "/tmp/config.yaml",
        ),
        (
            service_path,
            expected_service_sha256,
            "k3s service source",
            "/tmp/k3s.service",
        ),
    ):
        source_fd = _open_verified_file(source, expected_sha256, context)
        try:
            scp_fd_to(root, ip, source_fd, destination)
        finally:
            os.close(source_fd)

    staged_paths = ("/tmp/k3s", "/tmp/config.yaml", "/tmp/k3s.service")
    staged_digests = _parse_sha256sum_output(
        run(
            [*ssh_argv(root, ip), "sha256sum", "--", *staged_paths],
            timeout=30,
        ).stdout,
        staged_paths,
    )
    if staged_digests != {
        "/tmp/k3s": expected_k3s_sha256,
        "/tmp/config.yaml": expected_config_sha256,
        "/tmp/k3s.service": expected_service_sha256,
    }:
        raise RuntimeErrorEB("staged k3s files drifted before root install")

    install_command = (
        "sudo install -m 0755 /tmp/k3s /usr/local/bin/k3s && "
        "sudo install -d -m 0755 /etc/rancher/k3s && "
        "sudo install -m 0600 /tmp/config.yaml /etc/rancher/k3s/config.yaml && "
        "sudo install -m 0644 /tmp/k3s.service /etc/systemd/system/k3s.service"
    )
    run([*ssh_argv(root, ip), install_command], timeout=180)

    installed_paths = (
        "/usr/local/bin/k3s",
        "/etc/rancher/k3s/config.yaml",
        "/etc/systemd/system/k3s.service",
    )
    installed_digests = _parse_sha256sum_output(
        run(
            [*ssh_argv(root, ip), "sudo", "sha256sum", "--", *installed_paths],
            timeout=30,
        ).stdout,
        installed_paths,
    )
    if installed_digests != {
        "/usr/local/bin/k3s": expected_k3s_sha256,
        "/etc/rancher/k3s/config.yaml": expected_config_sha256,
        "/etc/systemd/system/k3s.service": expected_service_sha256,
    }:
        raise RuntimeErrorEB("installed k3s files drifted before service restart")

    service_command = (
        "sudo systemctl daemon-reload && "
        "sudo systemctl enable k3s && "
        "sudo systemctl restart k3s"
    )
    run([*ssh_argv(root, ip), service_command], timeout=180)
    live_node: dict[str, Any] | None = None
    for _ in range(90):
        result = run(
            [*ssh_argv(root, ip), "sudo /usr/local/bin/k3s kubectl get nodes -o json"],
            check=False,
        )
        if result.returncode == 0:
            try:
                inventory = json.loads(result.stdout)
                live_node = _require_exact_k3s_node_inventory(
                    inventory,
                    str(config["kubernetes"]["version"]),
                    require_ready=False,
                )
            except (json.JSONDecodeError, RuntimeErrorEB):
                live_node = None
            if live_node is not None:
                break
        time.sleep(2)
    else:
        raise RuntimeErrorEB(
            "k3s node did not become queryable at the exact pinned version"
        )

    kubeconfig_raw = run(
        [*ssh_argv(root, ip), "sudo cat /etc/rancher/k3s/k3s.yaml"]
    ).stdout
    kubeconfig = kubeconfig_raw.replace(
        "https://127.0.0.1:6443", f"https://{ip}:6443"
    )
    kubeconfig_path = _write_kubeconfig(root, kubeconfig)

    version = run(
        [*ssh_argv(root, ip), "/usr/local/bin/k3s --version"]
    ).stdout.splitlines()[0]
    if config["kubernetes"]["version"] not in version:
        raise RuntimeErrorEB(f"k3s version mismatch: {version}")
    receipt = {
        "schema_version": 1,
        "status": "ready",
        "source_commit": source_commit,
        "vm_ip": ip,
        "k3s_version": version,
        "live_kubelet_version": live_node["kubelet_version"],
        "binary_sha256": expected_k3s_sha256,
        "config_sha256": expected_config_sha256,
        "service_sha256": expected_service_sha256,
        "kubeconfig_sha256": sha256_file(kubeconfig_path),
    }
    atomic_json(root / "receipts/k3s.json", receipt)
    return receipt



def toolchain(
    root: Path,
    source_commit: str | None = None,
) -> dict[str, Any]:
    commit = (
        source_commit
        if source_commit is not None
        else (_BOUND_SOURCE_COMMIT.get() or git_head())
    )
    if COMMIT_RE.fullmatch(commit) is None:
        raise RuntimeErrorEB(
            "toolchain source commit must be exact"
        )
    bootstrap = _source_bound_bootstrap_tools(commit)
    lock_bytes = _git_blob_bytes(
        commit,
        TOOLCHAIN_LOCK_PATH,
    )
    try:
        lock = json.loads(lock_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeErrorEB(
            "source-commit toolchain lock is invalid"
        ) from exc
    if not isinstance(lock, dict) or lock.get("schema_version") != 1:
        raise RuntimeErrorEB(
            "source-commit toolchain lock schema is invalid"
        )
    lock_sha256 = hashlib.sha256(lock_bytes).hexdigest()
    cache_key = (
        str((root / "toolchain").resolve()),
        commit,
        lock_sha256,
    )
    cached = _TOOLCHAIN_SNAPSHOT_RECEIPTS.get(cache_key)
    if cached is not None:
        return cached

    receipt = bootstrap.install(
        root / "toolchain",
        tool_names=["kubectl", "kustomize", "flux", "helm"],
        include_artifacts=True,
        lock_bytes=lock_bytes,
    )
    if receipt.get("lock_sha256") != lock_sha256:
        raise RuntimeErrorEB(
            "installed toolchain receipt is not source-commit-bound"
        )

    bound_tools: dict[str, str] = {}
    bound_artifacts: dict[str, str] = {}
    new_fds: list[int] = []
    try:
        for name, path_value in receipt.get("tools", {}).items():
            spec = lock.get("tools", {}).get(name)
            expected_sha256 = (
                spec.get("binary_sha256")
                if isinstance(spec, dict)
                else None
            )
            if (
                not isinstance(path_value, str)
                or not isinstance(expected_sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
                is None
            ):
                raise RuntimeErrorEB(
                    f"source-commit tool contract is invalid: {name}"
                )
            source_fd = _open_verified_file(
                Path(path_value),
                expected_sha256,
                f"source-commit tool {name}",
            )
            try:
                size = os.fstat(source_fd).st_size
                payload = os.pread(source_fd, size, 0)
            finally:
                os.close(source_fd)
            if len(payload) != size:
                raise RuntimeErrorEB(
                    f"source-commit tool snapshot is incomplete: {name}"
                )
            snapshot_fd = _create_sealed_snapshot_fd(
                payload,
                f"source-commit tool {name}",
                mode=0o500,
            )
            new_fds.append(snapshot_fd)
            bound_tools[name] = f"/proc/self/fd/{snapshot_fd}"

        for name, path_value in receipt.get("artifacts", {}).items():
            spec = lock.get("artifacts", {}).get(name)
            expected_sha256 = (
                spec.get("sha256")
                if isinstance(spec, dict)
                else None
            )
            if (
                not isinstance(path_value, str)
                or not isinstance(expected_sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
                is None
            ):
                raise RuntimeErrorEB(
                    f"source-commit artifact contract is invalid: {name}"
                )
            source_fd = _open_verified_file(
                Path(path_value),
                expected_sha256,
                f"source-commit artifact {name}",
            )
            try:
                size = os.fstat(source_fd).st_size
                payload = os.pread(source_fd, size, 0)
            finally:
                os.close(source_fd)
            if len(payload) != size:
                raise RuntimeErrorEB(
                    f"source-commit artifact snapshot is incomplete: {name}"
                )
            snapshot_fd = _create_sealed_snapshot_fd(
                payload,
                f"source-commit artifact {name}",
            )
            new_fds.append(snapshot_fd)
            bound_artifacts[name] = f"/proc/self/fd/{snapshot_fd}"
    except Exception:
        for snapshot_fd in new_fds:
            os.close(snapshot_fd)
        raise

    _TOOLCHAIN_SNAPSHOT_FDS.update(new_fds)
    bound_receipt = {
        **receipt,
        "source_commit": commit,
        "tools": bound_tools,
        "artifacts": bound_artifacts,
    }
    _TOOLCHAIN_SNAPSHOT_RECEIPTS[cache_key] = bound_receipt
    return bound_receipt

def kube_env(root: Path) -> dict[str, str]:
    env = os.environ.copy()
    bound = _BOUND_KUBECONFIG.get()
    env["KUBECONFIG"] = (
        bound if bound is not None else str(root / "kubeconfig.yaml")
    )
    return env


@contextmanager
def _bound_kube_env(
    root: Path,
    expected_target: dict[str, str],
    source_commit: str,
) -> Iterator[dict[str, str]]:
    if COMMIT_RE.fullmatch(source_commit) is None:
        raise RuntimeErrorEB(
            "Experiment-B Kubernetes workflow source commit is invalid"
        )
    source = root / "kubeconfig.yaml"
    with _verified_snapshot_fd(
        source,
        expected_target["kubeconfig_sha256"],
        "Experiment-B Kubernetes workflow kubeconfig",
    ) as snapshot_fd:
        snapshot = Path(f"/proc/self/fd/{snapshot_fd}")
        snapshot_size = os.fstat(snapshot_fd).st_size
        snapshot_bytes = os.pread(snapshot_fd, snapshot_size, 0)
        if (
            len(snapshot_bytes) != snapshot_size
            or _kubeconfig_server_payload(snapshot_bytes)
            != expected_target["server"]
        ):
            raise RuntimeErrorEB(
                "Experiment-B Kubernetes workflow kubeconfig snapshot drifted"
            )
        toolchain(root, source_commit)
        source_token = _BOUND_SOURCE_COMMIT.set(source_commit)
        path_token = _BOUND_KUBECONFIG.set(str(snapshot))
        fd_token = _BOUND_KUBECONFIG_FD.set(snapshot_fd)
        try:
            yield kube_env(root)
        finally:
            _BOUND_KUBECONFIG_FD.reset(fd_token)
            _BOUND_KUBECONFIG.reset(path_token)
            _BOUND_SOURCE_COMMIT.reset(source_token)

def _cilium_helm_value_args(ip: str) -> list[str]:
    return [
        "--set", "gatewayAPI.enabled=true",
        "--set", "nodeIPAM.enabled=true",
        "--set", "defaultLBServiceIPAM=nodeipam",
        "--set", "kubeProxyReplacement=true",
        "--set", f"k8sServiceHost={ip}",
        "--set", "k8sServicePort=6443",
        "--set", "ipam.operator.clusterPoolIPv4PodCIDRList={10.42.0.0/16}",
        "--set", "hubble.relay.enabled=true",
        "--set", "hubble.ui.enabled=false",
        "--set", "operator.replicas=1",
    ]


def _flux_install_argv(flux: str, *, export: bool = False) -> list[str]:
    argv = [
        flux,
        "install",
        "--namespace=flux-system",
        "--components=source-controller,kustomize-controller,helm-controller,notification-controller",
    ]
    if export:
        argv.append("--export")
    return argv


@_serialize_experiment_b_lifecycle
def install_platform(root: Path) -> dict[str, Any]:
    _invalidate_receipts(root, PLATFORM_ATTEMPT_INVALIDATES)
    source_commit = _current_protected_main_commit()
    config = _source_commit_config(source_commit)
    _require_kubernetes_target_binding(root, source_commit)
    platform_target = _kubernetes_target_identity(root, source_commit)
    receipt = toolchain(root, source_commit)
    tools = receipt["tools"]
    artifacts = receipt["artifacts"]
    kubectl = tools["kubectl"]
    helm = tools["helm"]
    flux = tools["flux"]

    with _bound_kube_env(root, platform_target, source_commit) as env:
        _require_same_kubernetes_target(
            root,
            source_commit,
            platform_target,
            "platform installation before Gateway API CRDs",
        )
        for name in (
            "gateway_api_gatewayclasses",
            "gateway_api_gateways",
            "gateway_api_httproutes",
            "gateway_api_referencegrants",
            "gateway_api_grpcroutes",
        ):
            run([kubectl, "apply", "-f", artifacts[name]], env=env)
        _require_same_kubernetes_target(
            root,
            source_commit,
            platform_target,
            "platform installation after Gateway API CRDs",
        )

        ip = platform_target["vm_ip"]
        _require_same_kubernetes_target(
            root,
            source_commit,
            platform_target,
            "platform installation before Cilium",
        )
        run(
            [
                helm, "upgrade", "--install", "cilium", artifacts["cilium_chart"],
                "--namespace", "kube-system",
                *_cilium_helm_value_args(ip),
                "--wait", "--timeout", "10m",
            ],
            env=env,
            timeout=900,
        )
        _require_same_kubernetes_target(
            root,
            source_commit,
            platform_target,
            "platform installation after Cilium",
        )

        ready_node: dict[str, Any] | None = None
        last_node_error: Exception | None = None
        ready_deadline = time.monotonic() + 180
        while time.monotonic() < ready_deadline:
            remaining = ready_deadline - time.monotonic()
            probe_timeout = max(1, min(5, math.ceil(remaining)))
            try:
                nodes = json.loads(
                    run(
                        [kubectl, "get", "nodes", "-o", "json"],
                        env=env,
                        timeout=probe_timeout,
                    ).stdout
                )
                ready_node = _require_exact_k3s_node_inventory(
                    nodes,
                    str(config["kubernetes"]["version"]),
                )
            except (
                RuntimeErrorEB,
                json.JSONDecodeError,
                subprocess.TimeoutExpired,
            ) as exc:
                last_node_error = exc
            else:
                break
            sleep_seconds = min(
                2.0,
                max(0.0, ready_deadline - time.monotonic()),
            )
            if sleep_seconds:
                time.sleep(sleep_seconds)
        if ready_node is None:
            raise RuntimeErrorEB(
                "k3s node did not become Ready after Cilium convergence"
            ) from last_node_error

        _require_same_kubernetes_target(
            root,
            source_commit,
            platform_target,
            "platform installation before Flux",
        )
        run(
            _flux_install_argv(flux),
            env=env,
            timeout=600,
        )
        _require_same_kubernetes_target(
            root,
            source_commit,
            platform_target,
            "platform installation after Flux",
        )

        cilium_readback = _require_live_cilium_contract(
            root,
            config,
        )
        flux_readback: dict[str, Any] | None = None
        last_flux_error: RuntimeErrorEB | None = None
        for _ in range(90):
            try:
                flux_readback = _require_live_flux_controller_contract(root, receipt)
            except RuntimeErrorEB as exc:
                last_flux_error = exc
                time.sleep(2)
            else:
                break
        if flux_readback is None:
            raise RuntimeErrorEB(
                "Flux controllers did not converge to the pinned runtime contract"
            ) from last_flux_error
        _require_same_kubernetes_target(
            root,
            source_commit,
            platform_target,
            "platform success receipt",
        )
        result = {
            "schema_version": 1,
            "status": "ready",
            "source_commit": source_commit,
            "toolchain_lock_sha256": receipt["lock_sha256"],
            "vm_ip": ip,
            "kubernetes_target_sha256": _stable_json_sha256(platform_target),
            "cilium_runtime_image_ids": {
                "daemonset": cilium_readback["daemonset_pods"][
                    "runtime_image_ids_sha256"
                ],
                "operator": cilium_readback["operator_pods"][
                    "runtime_image_ids_sha256"
                ],
                "relay": cilium_readback["relay_pods"][
                    "runtime_image_ids_sha256"
                ],
            },
            "flux_runtime_image_ids": {
                name: controller["pods"]["runtime_image_ids_sha256"]
                for name, controller in flux_readback.items()
            },
        }
        atomic_json(root / "receipts/platform.json", result)
        return result


def render_namespaces(root: Path, source_commit: str) -> str:
    return _source_commit_kustomize_build(
        root,
        source_commit,
        NAMESPACES,
        NAMESPACES,
    )


def kubectl_apply(root: Path, manifest: str) -> None:
    tools = toolchain(root)["tools"]
    run(
        [tools["kubectl"], "apply", "-f", "-"],
        input_text=manifest,
        env=kube_env(root),
    )


def secret_manifest(
    namespace: str,
    name: str,
    literals: dict[str, str],
    *,
    secret_type: str | None = None,
    binary_data: dict[str, bytes] | None = None,
) -> str:
    data = {
        key: base64.b64encode(value.encode("utf-8")).decode("ascii")
        for key, value in literals.items()
    }
    for key, value in (binary_data or {}).items():
        data[key] = base64.b64encode(value).decode("ascii")
    payload: dict[str, Any] = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": namespace},
        "type": secret_type or "Opaque",
        "data": data,
    }
    return json.dumps(payload, sort_keys=True)


def _read_existing_database_secret_bytes(path: Path) -> bytes | None:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    cloexec = getattr(os, "O_CLOEXEC", None)
    if not isinstance(nofollow, int) or not isinstance(cloexec, int):
        raise RuntimeErrorEB(
            "Experiment-B database Secret source material is invalid"
        )
    try:
        file_fd = os.open(path, os.O_RDONLY | cloexec | nofollow)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeErrorEB(
            "Experiment-B database Secret source material is invalid"
        ) from exc
    try:
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeErrorEB(
                "Experiment-B database Secret source material is invalid"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(file_fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError as exc:
        raise RuntimeErrorEB(
            "Experiment-B database Secret source material is invalid"
        ) from exc
    finally:
        os.close(file_fd)


def ensure_secret_material(root: Path) -> tuple[dict[str, str], bytes]:
    path = root / "secrets/database.json"
    source_bytes = _read_existing_database_secret_bytes(path)
    if source_bytes is not None:
        try:
            data = json.loads(source_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeErrorEB(
                "Experiment-B database Secret source material is invalid"
            ) from exc
        expected_keys = ("username", "database", "password")
        if (
            not isinstance(data, dict)
            or set(data) != set(expected_keys)
            or any(
                not isinstance(data.get(key), str) or not data[key]
                for key in expected_keys
            )
        ):
            raise RuntimeErrorEB(
                "Experiment-B database Secret source material is invalid"
            )
        return {key: data[key] for key in expected_keys}, source_bytes
    data = {
        "username": "commonthing",
        "database": "commonthing",
        "password": secrets.token_hex(32),
    }
    source_bytes = (
        json.dumps(data, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    atomic_bytes(path, source_bytes)
    return data, source_bytes

def _database_url(database: dict[str, str]) -> str:
    return (
        "postgresql://"
        f"{urllib.parse.quote(database['username'], safe='')}:"
        f"{urllib.parse.quote(database['password'], safe='')}"
        f"@postgres.{DATA_NAMESPACE}.svc.cluster.local:5432/"
        f"{urllib.parse.quote(database['database'], safe='')}"
    )


def _read_registry_config_bytes(path: Path) -> bytes:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    cloexec = getattr(os, "O_CLOEXEC", None)
    if not isinstance(nofollow, int) or not isinstance(cloexec, int):
        raise RuntimeErrorEB("registry config must be a regular external file")
    try:
        file_fd = os.open(path, os.O_RDONLY | cloexec | nofollow)
    except OSError as exc:
        raise RuntimeErrorEB(
            "registry config must be a regular external file"
        ) from exc
    try:
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeErrorEB(
                "registry config must be a regular external file"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(file_fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError as exc:
        raise RuntimeErrorEB(
            "registry config must be a regular external file"
        ) from exc
    finally:
        os.close(file_fd)


def _registry_credential_is_usable(credential: Any) -> bool:
    if not isinstance(credential, dict):
        return False
    if "auth" in credential:
        auth = credential.get("auth")
        if not isinstance(auth, str) or not auth:
            return False
        try:
            decoded = base64.b64decode(auth.encode("ascii"), validate=True).decode(
                "utf-8"
            )
            username, password = decoded.split(":", 1)
        except (
            UnicodeEncodeError,
            UnicodeDecodeError,
            binascii.Error,
            ValueError,
        ):
            return False
        return bool(username) and bool(password)
    username = credential.get("username")
    password = credential.get("password")
    return (
        isinstance(username, str)
        and bool(username)
        and isinstance(password, str)
        and bool(password)
    )


@_serialize_experiment_b_lifecycle
def inject_secrets(root: Path, registry_config: Path) -> dict[str, Any]:
    _invalidate_receipts(root, SECRETS_ATTEMPT_INVALIDATES)
    source_commit = _current_protected_main_commit()
    _require_kubernetes_target_binding(root, source_commit)
    secrets_target = _kubernetes_target_identity(root, source_commit)
    registry_bytes = _read_registry_config_bytes(registry_config)
    try:
        registry_payload = json.loads(registry_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeErrorEB("registry config is not valid JSON") from exc
    auths = registry_payload.get("auths") if isinstance(registry_payload, dict) else None
    credential = auths.get("ghcr.io") if isinstance(auths, dict) else None
    if not _registry_credential_is_usable(credential):
        raise RuntimeErrorEB("registry config has no usable ghcr.io credential")
    db, database_bytes = ensure_secret_material(root)
    database_url = _database_url(db)
    registry_state = root / "secrets/registry.json"
    atomic_bytes(registry_state, registry_bytes)
    with _bound_kube_env(root, secrets_target, source_commit):
        kubectl_apply(root, render_namespaces(root, source_commit))
        kubectl_apply(
            root,
            secret_manifest(
                DATA_NAMESPACE,
                "commonthing-experiment-b-database",
                db,
            ),
        )
        kubectl_apply(
            root,
            secret_manifest(
                APP_NAMESPACE,
                "weltgewebe-runtime",
                {"database-url": database_url},
            ),
        )
        kubectl_apply(
            root,
            secret_manifest(
                APP_NAMESPACE,
                "commonthing-experiment-b-registry",
                {},
                secret_type="kubernetes.io/dockerconfigjson",
                binary_data={".dockerconfigjson": registry_bytes},
            ),
        )
    _require_same_kubernetes_target(
        root,
        source_commit,
        secrets_target,
        "secret injection success receipt",
    )
    receipt = {
        "schema_version": 1,
        "status": "ready",
        "source_commit": source_commit,
        "kubernetes_target_sha256": _stable_json_sha256(secrets_target),
        "database_secret": "commonthing-experiment-b-database",
        "runtime_secret": "weltgewebe-runtime",
        "registry_secret": "commonthing-experiment-b-registry",
        "database_source_sha256": hashlib.sha256(database_bytes).hexdigest(),
        "registry_source_sha256": hashlib.sha256(registry_bytes).hexdigest(),
        "secret_values_recorded": False,
    }
    atomic_json(root / "receipts/secrets.json", receipt)
    return receipt


@_serialize_experiment_b_lifecycle
def apply_release(
    root: Path,
    source_commit: str,
    api_digest: str,
    web_digest: str,
) -> dict[str, Any]:
    if not COMMIT_RE.fullmatch(source_commit):
        raise RuntimeErrorEB("source commit is not exact")
    if not DIGEST_RE.fullmatch(api_digest) or not DIGEST_RE.fullmatch(web_digest):
        raise RuntimeErrorEB("image digests must be exact sha256 values")
    receipt_path, attempt_path, attempt_started_at_unix_ms = (
        _begin_release_attempt(root, source_commit)
    )
    if _current_protected_main_commit() != source_commit:
        raise RuntimeErrorEB("release source is not current protected main")
    _require_kubernetes_target_binding(root, source_commit)
    release_target = _kubernetes_target_identity(root, source_commit)
    config = load_config(source_commit)
    api_replicas = int(config["semantic_search"]["api_replicas"])
    web_replicas = int(config["runtime_binding"]["web_replicas"])
    output = root / "bootstrap.yaml"
    bootstrap_template = _git_blob_bytes(
        source_commit,
        BOOTSTRAP_TEMPLATE,
    )
    contract = _source_bound_contract(source_commit)
    binding = contract.render_bootstrap_from_template(
        source_commit,
        api_digest,
        web_digest,
        output,
        bootstrap_template,
    )
    flux_contract = _flux_bootstrap_contract(root, binding)
    with _bound_kube_env(root, release_target, source_commit):
        kubectl_apply(root, str(flux_contract["bootstrap_manifest"]))
        kubectl = toolchain(root)["tools"]["kubectl"]
        env = kube_env(root)
        for _ in range(120):
            source_result = run(
                [
                    kubectl,
                    "-n",
                    "flux-system",
                    "get",
                    "gitrepository",
                    "commonthing-experiment-b",
                    "-o",
                    "json",
                ],
                env=env,
                check=False,
            )
            flux_result = run(
                [
                    kubectl,
                    "-n",
                    "flux-system",
                    "get",
                    "kustomizations",
                    "-o",
                    "json",
                ],
                env=env,
                check=False,
            )
            if source_result.returncode == 0 and flux_result.returncode == 0:
                try:
                    source_payload = json.loads(source_result.stdout)
                    flux_payload = json.loads(flux_result.stdout)
                    if not isinstance(source_payload, dict) or not isinstance(
                        flux_payload, dict
                    ):
                        raise RuntimeErrorEB("Flux readiness payload is not an object")
                    _require_flux_source_revision(
                        source_payload,
                        source_commit,
                        flux_contract["source_spec"],
                    )
                    _require_exact_flux_revision_ready(
                        flux_payload.get("items", []),
                        source_commit,
                        flux_contract["kustomization_specs"],
                    )
                    live_results = {
                        "api": run(
                            [
                                kubectl, "-n", APP_NAMESPACE, "get", "deployment",
                                "weltgewebe-api", "-o", "json",
                            ],
                            env=env,
                            check=False,
                        ),
                        "web": run(
                            [
                                kubectl, "-n", APP_NAMESPACE, "get", "deployment",
                                "weltgewebe-web", "-o", "json",
                            ],
                            env=env,
                            check=False,
                        ),
                        "migration": run(
                            [
                                kubectl, "-n", APP_NAMESPACE, "get", "job",
                                MIGRATION_JOB_NAME, "-o", "json",
                            ],
                            env=env,
                            check=False,
                        ),
                        "migration_pods": run(
                            [
                                kubectl, "-n", APP_NAMESPACE, "get", "pods",
                                "-l", f"batch.kubernetes.io/job-name={MIGRATION_JOB_NAME}",
                                "-o", "json",
                            ],
                            env=env,
                            check=False,
                        ),
                    }
                    if any(result.returncode != 0 for result in live_results.values()):
                        raise RuntimeErrorEB(
                            "Experiment-B release artifacts are not all queryable"
                        )
                    migration_pods_payload = json.loads(
                        live_results["migration_pods"].stdout
                    )
                    if not isinstance(migration_pods_payload, dict):
                        raise RuntimeErrorEB(
                            "Experiment-B migration Pod inventory is not an object"
                        )
                    _require_requested_release_artifacts(
                        root,
                        json.loads(live_results["api"].stdout),
                        json.loads(live_results["web"].stdout),
                        json.loads(live_results["migration"].stdout),
                        migration_pods_payload.get("items"),
                        api_digest,
                        web_digest,
                        api_replicas,
                        web_replicas,
                        source_commit,
                    )
                except (json.JSONDecodeError, RuntimeErrorEB):
                    pass
                else:
                    break
            time.sleep(5)
        else:
            raise RuntimeErrorEB(
                "Flux Experiment-B source and kustomizations did not converge "
                "to the exact release revision"
            )
    _require_same_kubernetes_target(
        root,
        source_commit,
        release_target,
        "release success receipt",
    )
    receipt = {
        "schema_version": 1,
        "status": "applied",
        "kubernetes_target_sha256": _stable_json_sha256(release_target),
        **binding,
    }
    atomic_json(receipt_path, receipt)
    _complete_live_check_attempt(
        attempt_path,
        receipt_path,
        source_commit,
        attempt_started_at_unix_ms,
        "pass",
    )
    return receipt


def _semantic_provider_runtime_binding(
    root: Path, source_commit: str
) -> dict[str, str]:
    pod_name, pod, runtime_binding = _require_t048_api_runtime_binding(
        root,
        source_commit,
    )
    metadata = pod.get("metadata")
    status_obj = pod.get("status")
    pod_uid = (
        metadata.get("uid")
        if isinstance(metadata, dict)
        else None
    )
    statuses = (
        status_obj.get("containerStatuses")
        if isinstance(status_obj, dict)
        else None
    )
    if not isinstance(pod_uid, str) or not pod_uid:
        raise RuntimeErrorEB(
            "semantic provider Pod UID is invalid"
        )
    if not isinstance(statuses, list):
        raise RuntimeErrorEB(
            "semantic provider container status inventory is invalid"
        )
    status_by_name: dict[str, dict[str, Any]] = {}
    for item in statuses:
        if not isinstance(item, dict):
            raise RuntimeErrorEB(
                "semantic provider container status inventory is invalid"
            )
        name = item.get("name")
        if (
            not isinstance(name, str)
            or not name
            or name in status_by_name
        ):
            raise RuntimeErrorEB(
                "semantic provider container status identity is invalid"
            )
        status_by_name[name] = item

    result: dict[str, str] = {
        "pod_name": pod_name,
        "pod_uid": pod_uid,
    }
    for field in (
        "contract_sha256",
        "pod_contract_sha256",
        "runtime_image_ids_sha256",
    ):
        value = runtime_binding.get(field)
        if not isinstance(value, str) or not value:
            raise RuntimeErrorEB(
                "semantic provider runtime binding field is invalid: "
                f"{field}"
            )
        result[field] = value
    for container_name, result_field in (
        ("search-worker", "search_worker_container_id"),
        ("ollama", "ollama_container_id"),
    ):
        item = status_by_name.get(container_name)
        container_id = (
            item.get("containerID")
            if isinstance(item, dict)
            else None
        )
        if (
            not isinstance(container_id, str)
            or re.fullmatch(
                r"containerd://[0-9a-f]{64}",
                container_id,
            )
            is None
        ):
            raise RuntimeErrorEB(
                "semantic provider runtime binding has no exact "
                f"{container_name} containerID"
            )
        result[result_field] = container_id
    return result


def _semantic_provider_live_readback(
    root: Path, source_commit: str
) -> dict[str, Any]:
    if not COMMIT_RE.fullmatch(source_commit):
        raise RuntimeErrorEB("semantic provider live source commit is not exact")
    semantic = load_config(source_commit)["semantic_search"]
    runtime_binding = _semantic_provider_runtime_binding(
        root,
        source_commit,
    )
    probe_container_id = runtime_binding[
        "search_worker_container_id"
    ]
    tags = _run_bound_container_command(
        root,
        source_commit,
        probe_container_id,
        [
            "wget",
            "-qO-",
            "http://127.0.0.1:11434/api/tags",
        ],
        context="semantic provider tags probe",
    ).decode("utf-8")
    payload = json.loads(tags)
    observed = ""
    for model in payload.get("models", []):
        if model.get("name") == semantic["model_id"]:
            observed = str(model.get("digest", ""))
            break
    expected = str(semantic["model_revision"]).removeprefix("sha256:")
    if observed.removeprefix("sha256:") != expected:
        raise RuntimeErrorEB(
            "Ollama model digest differs from the pinned revision"
        )

    probe_text = "commonThing Experiment B semantic continuity"
    probe_request = json.dumps(
        {"model": semantic["model_id"], "input": probe_text},
        separators=(",", ":"),
    )
    embedding_raw = _run_bound_container_command(
        root,
        source_commit,
        probe_container_id,
        [
            "wget",
            "-qO-",
            "--header=Content-Type: application/json",
            f"--post-data={probe_request}",
            "http://127.0.0.1:11434/api/embed",
        ],
        timeout=300,
        context="semantic provider embedding probe",
    ).decode("utf-8")
    embedding_payload = json.loads(embedding_raw)
    embeddings = embedding_payload.get("embeddings")
    dimension = int(semantic["dimension"])
    if (
        not isinstance(embeddings, list)
        or len(embeddings) != 1
        or not isinstance(embeddings[0], list)
        or len(embeddings[0]) != dimension
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in embeddings[0]
        )
    ):
        raise RuntimeErrorEB(
            "Ollama embedding smoke does not match the pinned finite dimension"
        )
    if (
        _semantic_provider_runtime_binding(root, source_commit)
        != runtime_binding
    ):
        raise RuntimeErrorEB(
            "semantic provider runtime changed during probe"
        )
    return {
        "source_commit": source_commit,
        "provider": semantic["provider"],
        "model_id": semantic["model_id"],
        "model_revision": semantic["model_revision"],
        "runtime_identity": semantic["runtime_identity"],
        "dimension": semantic["dimension"],
        "embedding_probe": True,
        "embedding_probe_sha256": hashlib.sha256(
            probe_text.encode("utf-8")
        ).hexdigest(),
        "runtime_binding_sha256": _stable_json_sha256(
            runtime_binding
        ),
        "literal_loopback": True,
    }

@_serialize_experiment_b_lifecycle
def semantic_activate(root: Path) -> dict[str, Any]:
    release_path = root / "receipts/release.json"
    if not release_path.is_file():
        raise RuntimeErrorEB("semantic provider proof requires an applied release receipt")
    release = json.loads(release_path.read_text(encoding="utf-8"))
    source_commit = str(release.get("source_commit", ""))
    if not COMMIT_RE.fullmatch(source_commit):
        raise RuntimeErrorEB("semantic provider proof release binding is not exact")
    receipt_path, attempt_path, attempt_started_at_unix_ms = (
        _begin_live_check_attempt(root, "semantic-search", source_commit)
    )
    if _current_protected_main_commit() != source_commit:
        raise RuntimeErrorEB("semantic provider proof is not bound to current protected main")
    _require_kubernetes_target_binding(root, source_commit)
    semantic_target = _kubernetes_target_identity(root, source_commit)
    with _bound_kube_env(root, semantic_target, source_commit):
        config = load_config(source_commit)
        semantic = config["semantic_search"]
        kubectl = toolchain(root)["tools"]["kubectl"]
        env = kube_env(root)
        egress_name = "commonthing-experiment-b-model-bootstrap-egress"
        temporary_egress = json.dumps(
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": egress_name, "namespace": APP_NAMESPACE},
                "spec": {
                    "podSelector": {
                        "matchLabels": {"app.kubernetes.io/name": "weltgewebe-api"}
                    },
                    "policyTypes": ["Egress"],
                    "egress": [
                        {
                            "to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}],
                            "ports": [{"protocol": "TCP", "port": 443}],
                        }
                    ],
                },
            },
            sort_keys=True,
        )
        kubectl_apply(root, temporary_egress)
        try:
            run(
                [
                    kubectl, "-n", APP_NAMESPACE,
                    "exec", "deployment/weltgewebe-api",
                    "-c", "ollama", "--", "ollama", "pull", semantic["model_id"],
                ],
                env=env,
                timeout=1800,
            )
            semantic_live = _semantic_provider_live_readback(root, source_commit)
        finally:
            _kubectl(
                root,
                [
                    "-n", APP_NAMESPACE, "delete", "networkpolicy",
                    egress_name, "--ignore-not-found=true",
                ],
            )
        remaining_egress = _kubectl(
            root,
            [
                "-n", APP_NAMESPACE, "get", "networkpolicy",
                egress_name, "--ignore-not-found=true", "-o", "name",
            ],
        )
        if remaining_egress.stdout.strip():
            raise RuntimeErrorEB("temporary model-bootstrap egress policy still exists")

    _require_same_kubernetes_target(
        root,
        source_commit,
        semantic_target,
        "semantic activation success receipt",
    )
    receipt = {
        "schema_version": 1,
        "status": "pass",
        "kubernetes_target_sha256": _stable_json_sha256(semantic_target),
        **semantic_live,
        "temporary_model_egress_removed": True,
        "database_generation_activation": False,
    }
    atomic_json(receipt_path, receipt)
    _complete_live_check_attempt(
        attempt_path,
        receipt_path,
        source_commit,
        attempt_started_at_unix_ms,
        "pass",
    )
    return receipt



EXPECTED_FLUX_KUSTOMIZATIONS = frozenset(
    {
        "commonthing-experiment-b-namespaces",
        "commonthing-experiment-b-data",
        "commonthing-experiment-b-migration",
        "commonthing-experiment-b-app",
        "commonthing-experiment-b-gateway",
    }
)


def _require_exact_flux_kustomizations(flux_readback: dict[str, Any]) -> None:
    observed = set(flux_readback)
    if observed == EXPECTED_FLUX_KUSTOMIZATIONS:
        return
    missing = sorted(EXPECTED_FLUX_KUSTOMIZATIONS - observed)
    unexpected = sorted(observed - EXPECTED_FLUX_KUSTOMIZATIONS)
    raise RuntimeErrorEB(
        "Experiment-B Flux Kustomization set mismatch: "
        f"missing={missing}; unexpected={unexpected}"
    )


def _flux_revision_matches_commit(revision: Any, source_commit: str) -> bool:
    if not COMMIT_RE.fullmatch(source_commit):
        return False
    value = str(revision or "")
    return value in {source_commit, f"sha1:{source_commit}"} or value.endswith(
        f"@sha1:{source_commit}"
    )


def _canonical_flux_spec(spec: Any) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise RuntimeErrorEB("Flux spec is not an object")
    canonical = json.loads(json.dumps(spec))
    for key, default in (
        ("suspend", False),
        ("force", False),
        ("deletionPolicy", "MirrorPrune"),
        ("provider", "generic"),
        ("timeout", "60s"),
    ):
        if canonical.get(key) == default:
            canonical.pop(key, None)

    source_ref = canonical.get("sourceRef")
    if isinstance(source_ref, dict) and source_ref.get("namespace") == "flux-system":
        source_ref.pop("namespace", None)

    ref = canonical.get("ref")
    if isinstance(ref, dict) and ref.get("recurseSubmodules") is False:
        ref.pop("recurseSubmodules", None)

    post_build = canonical.get("postBuild")
    if isinstance(post_build, dict):
        substitute_from = post_build.get("substituteFrom")
        if isinstance(substitute_from, list):
            for item in substitute_from:
                if isinstance(item, dict) and item.get("optional") is False:
                    item.pop("optional", None)

    duration_pattern = re.compile(
        r"^(?:(?P<hours>[0-9]+)h)?(?:(?P<minutes>[0-9]+)m)?(?:(?P<seconds>[0-9]+)s)?$"
    )
    for key in ("interval", "retryInterval", "timeout"):
        value = canonical.get(key)
        if not isinstance(value, str):
            continue
        match = duration_pattern.fullmatch(value)
        if match and any(match.groupdict().values()):
            canonical[key] = (
                int(match.group("hours") or 0) * 3600
                + int(match.group("minutes") or 0) * 60
                + int(match.group("seconds") or 0)
            )
    return canonical


def _require_flux_spec(
    live_spec: Any,
    expected_spec: Any,
    context: str,
) -> None:
    if _canonical_flux_spec(live_spec) != _canonical_flux_spec(expected_spec):
        raise RuntimeErrorEB(f"{context} spec drifted from the rendered bootstrap contract")


def _release_config_map_contract_projection(
    config_map: Any,
    context: str,
) -> dict[str, Any]:
    if not isinstance(config_map, dict):
        raise RuntimeErrorEB(f"{context} ConfigMap payload is invalid")
    data = config_map.get("data", {})
    binary_data = config_map.get("binaryData", {})
    immutable = config_map.get("immutable", False)
    if data is None:
        data = {}
    if binary_data is None:
        binary_data = {}
    if immutable is None:
        immutable = False
    if (
        not isinstance(data, dict)
        or not isinstance(binary_data, dict)
        or not isinstance(immutable, bool)
        or any(
            not isinstance(key, str)
            or not isinstance(value, str)
            for key, value in data.items()
        )
        or any(
            not isinstance(key, str)
            or not isinstance(value, str)
            for key, value in binary_data.items()
        )
    ):
        raise RuntimeErrorEB(
            f"{context} ConfigMap data contract is invalid"
        )
    return {
        "data": {
            str(key): str(value)
            for key, value in sorted(data.items())
        },
        "binaryData": {
            str(key): str(value)
            for key, value in sorted(binary_data.items())
        },
        "immutable": immutable,
    }


def _flux_bootstrap_contract(
    root: Path,
    binding: dict[str, Any],
) -> dict[str, Any]:
    bootstrap_path = root / "bootstrap.yaml"
    expected_sha256 = binding.get("sha256")
    if (
        not isinstance(expected_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
    ):
        raise RuntimeErrorEB("Experiment-B rendered bootstrap binding drifted")
    bootstrap_bytes = _verified_snapshot_bytes(
        bootstrap_path,
        expected_sha256,
        "rendered Experiment-B bootstrap",
    )
    try:
        bootstrap_manifest = bootstrap_bytes.decode("utf-8")
        documents = list(yaml.safe_load_all(bootstrap_manifest))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise RuntimeErrorEB("Experiment-B rendered bootstrap is invalid") from exc

    source_spec: dict[str, Any] | None = None
    release_config_map: dict[str, Any] | None = None
    kustomization_specs: dict[str, dict[str, Any]] = {}
    for document in documents:
        if not isinstance(document, dict):
            raise RuntimeErrorEB(
                "Experiment-B bootstrap contains an unexpected document"
            )
        metadata = document.get("metadata", {})
        if not isinstance(metadata, dict):
            raise RuntimeErrorEB(
                "Experiment-B bootstrap contains an unexpected document"
            )
        name = metadata.get("name")
        namespace = metadata.get("namespace")
        spec = document.get("spec")
        if (
            document.get("kind") == "GitRepository"
            and name == "commonthing-experiment-b"
            and namespace == "flux-system"
        ):
            if source_spec is not None or not isinstance(spec, dict):
                raise RuntimeErrorEB("Experiment-B bootstrap GitRepository is duplicated")
            source_spec = spec
        elif (
            document.get("kind") == "ConfigMap"
            and name == "commonthing-experiment-b-release"
            and namespace == "flux-system"
        ):
            if release_config_map is not None:
                raise RuntimeErrorEB(
                    "Experiment-B bootstrap release ConfigMap is duplicated"
                )
            release_config_map = _release_config_map_contract_projection(
                document,
                "Experiment-B bootstrap release ConfigMap",
            )
        elif (
            document.get("kind") == "Kustomization"
            and namespace == "flux-system"
            and isinstance(name, str)
            and name.startswith("commonthing-experiment-b-")
        ):
            if name in kustomization_specs or not isinstance(spec, dict):
                raise RuntimeErrorEB(
                    f"Experiment-B bootstrap Kustomization is duplicated: {name}"
                )
            kustomization_specs[name] = spec
        else:
            raise RuntimeErrorEB(
                "Experiment-B bootstrap contains an unexpected document"
            )

    if source_spec is None:
        raise RuntimeErrorEB("Experiment-B bootstrap GitRepository contract is missing")
    if release_config_map is None:
        raise RuntimeErrorEB(
            "Experiment-B bootstrap release ConfigMap contract is missing"
        )
    _require_exact_flux_kustomizations(kustomization_specs)
    return {
        "bootstrap_sha256": expected_sha256,
        "bootstrap_manifest": bootstrap_manifest,
        "source_spec": source_spec,
        "release_config_map": release_config_map,
        "kustomization_specs": kustomization_specs,
    }


def _require_live_release_config_map(
    root: Path,
    expected: dict[str, Any],
) -> dict[str, Any]:
    config_map = _kubectl_json(
        root,
        [
            "-n",
            "flux-system",
            "get",
            "configmap",
            "commonthing-experiment-b-release",
        ],
    )
    metadata = (
        config_map.get("metadata", {})
        if isinstance(config_map, dict)
        else {}
    )
    if (
        not isinstance(metadata, dict)
        or metadata.get("name") != "commonthing-experiment-b-release"
        or metadata.get("namespace") != "flux-system"
        or metadata.get("deletionTimestamp") is not None
    ):
        raise RuntimeErrorEB(
            "live Experiment-B release ConfigMap identity drifted"
        )
    observed = _release_config_map_contract_projection(
        config_map,
        "live Experiment-B release ConfigMap",
    )
    if observed != expected:
        raise RuntimeErrorEB(
            "live Experiment-B release ConfigMap contract drifted"
        )
    return {
        "contract_sha256": _stable_json_sha256(expected),
        "canonical": True,
    }


def _require_current_condition(
    document: Any,
    condition_type: str,
    context: str,
    *,
    conditions: Any = None,
) -> int:
    if not isinstance(document, dict):
        raise RuntimeErrorEB(f"{context} payload is not an object")
    metadata = document.get("metadata", {})
    if not isinstance(metadata, dict):
        raise RuntimeErrorEB(f"{context} metadata is not an object")
    try:
        generation = int(metadata.get("generation") or 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeErrorEB(f"{context} generation is invalid") from exc
    if generation < 1:
        raise RuntimeErrorEB(f"{context} generation is invalid")
    status_obj = document.get("status", {})
    selected = (
        conditions
        if conditions is not None
        else status_obj.get("conditions", []) if isinstance(status_obj, dict) else []
    )
    if not isinstance(selected, list):
        raise RuntimeErrorEB(f"{context} conditions are invalid")
    current = any(
        isinstance(condition, dict)
        and condition.get("type") == condition_type
        and condition.get("status") == "True"
        and condition.get("observedGeneration") == generation
        for condition in selected
    )
    if not current:
        raise RuntimeErrorEB(
            f"{context} is not currently {condition_type} at its current generation"
        )
    return generation


def _require_flux_source_revision(
    source: Any,
    source_commit: str,
    expected_spec: dict[str, Any],
) -> str:
    if not isinstance(source, dict):
        raise RuntimeErrorEB("Flux GitRepository payload is not an object")
    metadata = source.get("metadata", {})
    if (
        not isinstance(metadata, dict)
        or metadata.get("deletionTimestamp") is not None
    ):
        raise RuntimeErrorEB(
            "Flux GitRepository is pending deletion or malformed"
        )
    spec = source.get("spec", {})
    if not isinstance(spec, dict) or spec.get("suspend") is True:
        raise RuntimeErrorEB("Flux GitRepository is suspended or malformed")
    _require_flux_spec(spec, expected_spec, "Flux GitRepository")
    _require_current_condition(source, "Ready", "Flux GitRepository")
    revision = str(
        source.get("status", {}).get("artifact", {}).get("revision", "")
    )
    if not _flux_revision_matches_commit(revision, source_commit):
        raise RuntimeErrorEB("Flux GitRepository is not bound to the release commit")
    return revision


def _require_exact_flux_revision_ready(
    flux_items: Any,
    source_commit: str,
    expected_specs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(flux_items, list):
        raise RuntimeErrorEB("Flux Kustomization inventory is not a list")
    _require_exact_flux_kustomizations(expected_specs)

    flux_readback: dict[str, Any] = {}
    for item in flux_items:
        if not isinstance(item, dict):
            raise RuntimeErrorEB("Flux Kustomization inventory contains a non-object item")
        metadata = item.get("metadata", {})
        if not isinstance(metadata, dict):
            raise RuntimeErrorEB(
                "Flux Kustomization metadata is not an object"
            )
        name = str(metadata.get("name", ""))
        if not name.startswith("commonthing-experiment-b-"):
            continue
        if metadata.get("deletionTimestamp") is not None:
            raise RuntimeErrorEB(
                f"Flux Kustomization is pending deletion: {name}"
            )
        if name in flux_readback:
            raise RuntimeErrorEB(f"duplicate Flux Kustomization: {name}")
        expected_spec = expected_specs.get(name)
        if not isinstance(expected_spec, dict):
            raise RuntimeErrorEB(f"unexpected Flux Kustomization: {name}")
        spec = item.get("spec", {})
        if not isinstance(spec, dict) or spec.get("suspend") is True:
            raise RuntimeErrorEB(f"Flux Kustomization is suspended or malformed: {name}")
        _require_flux_spec(spec, expected_spec, f"Flux Kustomization {name}")
        generation = _require_current_condition(
            item, "Ready", f"Flux Kustomization {name}"
        )
        status_obj = item.get("status", {})
        revision = str(status_obj.get("lastAppliedRevision", ""))
        if not _flux_revision_matches_commit(revision, source_commit):
            raise RuntimeErrorEB(
                f"Flux Kustomization is not exact-revision Ready: {name}"
            )
        flux_readback[name] = {
            "ready": True,
            "revision": revision,
            "generation": generation,
            "spec_sha256": _stable_json_sha256(_canonical_flux_spec(spec)),
        }

    _require_exact_flux_kustomizations(flux_readback)
    return flux_readback


def _require_exact_k3s_node_inventory(
    nodes: Any,
    expected_version: str,
    *,
    require_ready: bool = True,
) -> dict[str, Any]:
    if not isinstance(nodes, dict) or nodes.get("kind") not in {"NodeList", "List"}:
        raise RuntimeErrorEB("Experiment B node inventory is not a Kubernetes node list")
    items = nodes.get("items")
    if not isinstance(items, list) or len(items) != 1:
        raise RuntimeErrorEB("Experiment B expects exactly one k3s VM node")
    node = items[0]
    if not isinstance(node, dict) or node.get("kind") != "Node":
        raise RuntimeErrorEB("Experiment B node inventory item is not a Kubernetes Node")
    metadata = node.get("metadata", {})
    status_obj = node.get("status", {})
    if (
        not isinstance(metadata, dict)
        or not metadata.get("name")
        or metadata.get("deletionTimestamp") is not None
        or not isinstance(status_obj, dict)
    ):
        raise RuntimeErrorEB("Experiment B node identity/deletion state is invalid")
    conditions = status_obj.get("conditions")
    ready_conditions = (
        [
            condition
            for condition in conditions
            if isinstance(condition, dict) and condition.get("type") == "Ready"
        ]
        if isinstance(conditions, list)
        else []
    )
    if len(ready_conditions) != 1:
        if require_ready:
            raise RuntimeErrorEB("Experiment B k3s node is not Ready")
        raise RuntimeErrorEB("Experiment B k3s node Ready condition is invalid")
    ready_status = ready_conditions[0].get("status")
    if ready_status not in {"True", "False"}:
        raise RuntimeErrorEB("Experiment B k3s node Ready condition is invalid")
    ready = ready_status == "True"
    if require_ready and not ready:
        raise RuntimeErrorEB("Experiment B k3s node is not Ready")
    info = status_obj.get("nodeInfo", {})
    kubelet = str(info.get("kubeletVersion", ""))
    os_image = str(info.get("osImage", ""))
    if "k3s" not in kubelet:
        raise RuntimeErrorEB("node is not a k3s runtime")
    if kubelet != expected_version:
        raise RuntimeErrorEB("node kubelet version does not match pinned k3s version")
    return {
        "node": str(metadata["name"]),
        "kubelet_version": kubelet,
        "os_image": os_image,
        "ready": ready,
    }


def _pvc_spec_projection(
    pvc: Any,
    context: str,
) -> dict[str, Any]:
    if not isinstance(pvc, dict):
        raise RuntimeErrorEB(f"{context} PVC payload is invalid")
    spec = pvc.get("spec", {})
    if not isinstance(spec, dict):
        raise RuntimeErrorEB(f"{context} PVC spec is invalid")
    access_modes = spec.get("accessModes") or []
    resources = spec.get("resources") or {}
    if (
        not isinstance(access_modes, list)
        or any(not isinstance(value, str) or not value for value in access_modes)
        or not isinstance(resources, dict)
    ):
        raise RuntimeErrorEB(f"{context} PVC access/resource contract is invalid")
    storage_class = spec.get("storageClassName")
    if storage_class is not None and (
        not isinstance(storage_class, str) or not storage_class
    ):
        raise RuntimeErrorEB(f"{context} PVC storageClassName is invalid")
    volume_mode = spec.get("volumeMode", "Filesystem")
    if volume_mode not in {"Filesystem", "Block"}:
        raise RuntimeErrorEB(f"{context} PVC volumeMode is invalid")
    return {
        "accessModes": sorted(access_modes),
        "storageClassName": storage_class,
        "volumeMode": volume_mode,
        "resources": json.loads(json.dumps(resources)),
        "selector": spec.get("selector"),
        "dataSource": spec.get("dataSource"),
        "dataSourceRef": spec.get("dataSourceRef"),
        "volumeAttributesClassName": spec.get("volumeAttributesClassName"),
    }


def _rendered_pvc_contract(
    root: Path,
    source_commit: str | None = None,
) -> dict[str, Any]:
    documents: list[dict[str, Any]] = []
    if source_commit is None:
        kustomize = toolchain(root)["tools"].get("kustomize")
        if not isinstance(kustomize, str) or not kustomize:
            raise RuntimeErrorEB("PVC contract requires pinned kustomize")
        rendered_targets = [
            (target, run([kustomize, "build", str(target)]).stdout)
            for target in (CLUSTER / "data", APP_OVERLAY)
        ]
    else:
        if COMMIT_RE.fullmatch(source_commit) is None:
            raise RuntimeErrorEB("PVC contract source commit is not exact")
        rendered_targets = [
            (
                CLUSTER / "data",
                _source_commit_kustomize_build(
                    root,
                    source_commit,
                    CLUSTER / "data",
                    CLUSTER / "data",
                ),
            ),
            (
                APP_OVERLAY,
                _source_commit_kustomize_build(
                    root,
                    source_commit,
                    APP_OVERLAY,
                    ROOT / "platform/apps/weltgewebe",
                ),
            ),
        ]
    for target, rendered in rendered_targets:
        try:
            documents.extend(
                document
                for document in yaml.safe_load_all(rendered)
                if isinstance(document, dict)
            )
        except yaml.YAMLError as exc:
            raise RuntimeErrorEB(
                f"rendered Experiment-B PVC contract is invalid: {target}"
            ) from exc

    result: dict[str, Any] = {}
    for document in documents:
        if document.get("kind") != "PersistentVolumeClaim":
            continue
        metadata = document.get("metadata", {})
        if not isinstance(metadata, dict):
            raise RuntimeErrorEB("rendered Experiment-B PVC metadata is invalid")
        namespace = str(metadata.get("namespace", ""))
        name = str(metadata.get("name", ""))
        if namespace not in {APP_NAMESPACE, DATA_NAMESPACE}:
            continue
        key = f"{namespace}/{name}"
        if key in result:
            raise RuntimeErrorEB(
                f"rendered Experiment-B PVC is duplicated: {key}"
            )
        spec = _pvc_spec_projection(
            document, f"rendered Experiment-B PVC {key}"
        )
        result[key] = {
            "spec": spec,
            "spec_sha256": _stable_json_sha256(spec),
        }
    if set(result) != EXPECTED_PVCS:
        raise RuntimeErrorEB(
            "rendered Experiment-B PVC set drifted from the expected contract"
        )
    return result


def _require_exact_healthy_pvcs(
    root: Path,
    pvc_items: Any,
    source_commit: str | None = None,
) -> dict[str, Any]:
    if not isinstance(pvc_items, list):
        raise RuntimeErrorEB("Experiment-B PVC inventory is not a list")
    expected = _rendered_pvc_contract(root, source_commit)

    live: dict[str, dict[str, Any]] = {}
    for item in pvc_items:
        if not isinstance(item, dict):
            raise RuntimeErrorEB("Experiment-B PVC inventory contains a non-object item")
        metadata = item.get("metadata", {})
        if not isinstance(metadata, dict):
            raise RuntimeErrorEB("Experiment-B PVC metadata is not an object")
        namespace = str(metadata.get("namespace", ""))
        name = str(metadata.get("name", ""))
        if namespace not in {APP_NAMESPACE, DATA_NAMESPACE}:
            continue
        key = f"{namespace}/{name}"
        if key in live:
            raise RuntimeErrorEB(f"Experiment-B PVC inventory contains duplicate: {key}")
        live[key] = item

    observed = set(live)
    if observed != set(expected):
        missing = sorted(set(expected) - observed)
        unexpected = sorted(observed - set(expected))
        raise RuntimeErrorEB(
            "Experiment-B PVC set mismatch: "
            f"missing={missing}; unexpected={unexpected}"
        )

    pvc_readback: dict[str, Any] = {}
    for key, expected_value in expected.items():
        item = live[key]
        metadata = item.get("metadata", {})
        if metadata.get("deletionTimestamp"):
            raise RuntimeErrorEB(f"Experiment-B PVC is pending deletion: {key}")
        status = item.get("status", {})
        phase = status.get("phase") if isinstance(status, dict) else None
        if phase != "Bound":
            raise RuntimeErrorEB(f"Experiment-B PVC is not Bound: {key}")
        observed_spec = _pvc_spec_projection(
            item, f"live Experiment-B PVC {key}"
        )
        if observed_spec != expected_value["spec"]:
            raise RuntimeErrorEB(
                f"Experiment-B PVC contract drifted: {key}"
            )
        pvc_readback[key] = {
            "phase": "Bound",
            "spec_sha256": expected_value["spec_sha256"],
            "canonical": True,
        }
    return pvc_readback


def _deployment_availability_snapshot(
    deployment: dict[str, Any], name: str, expected_replicas: int
) -> dict[str, Any]:
    metadata = deployment.get("metadata", {})
    spec = deployment.get("spec", {})
    status_obj = deployment.get("status", {})
    generation = int(metadata.get("generation") or 0)
    desired = int(spec.get("replicas") or 0)
    observed_generation = int(status_obj.get("observedGeneration") or 0)
    replicas = int(status_obj.get("replicas") or 0)
    updated = int(status_obj.get("updatedReplicas") or 0)
    ready = int(status_obj.get("readyReplicas") or 0)
    available = int(status_obj.get("availableReplicas") or 0)
    unavailable = int(status_obj.get("unavailableReplicas") or 0)
    available_condition = any(
        condition.get("type") == "Available" and condition.get("status") == "True"
        for condition in status_obj.get("conditions", [])
        if isinstance(condition, dict)
    )
    if (
        not isinstance(expected_replicas, int)
        or isinstance(expected_replicas, bool)
        or expected_replicas < 1
        or generation < 1
        or desired != expected_replicas
        or observed_generation < generation
        or replicas != desired
        or updated != desired
        or ready != desired
        or available != desired
        or unavailable != 0
        or not available_condition
    ):
        raise RuntimeErrorEB(
            f"Experiment-B deployment is not currently available: {name}"
        )
    return {
        "generation": generation,
        "observed_generation": observed_generation,
        "desired_replicas": desired,
        "updated_replicas": updated,
        "ready_replicas": ready,
        "available_replicas": available,
        "available": True,
    }


EXPECTED_FLUX_CONTROLLERS = frozenset(
    {
        "source-controller",
        "kustomize-controller",
        "helm-controller",
        "notification-controller",
    }
)


def _normalize_flux_env(
    value: Any,
    context: str,
) -> Any:
    if value is None:
        return None
    if not isinstance(value, list) or any(
        not isinstance(item, dict) for item in value
    ):
        raise RuntimeErrorEB(f"{context} env contract is invalid")
    normalized = json.loads(json.dumps(value))
    for item in normalized:
        value_from = item.get("valueFrom")
        if not isinstance(value_from, dict):
            continue
        field_ref = value_from.get("fieldRef")
        if isinstance(field_ref, dict) and field_ref.get("apiVersion") == "v1":
            field_ref.pop("apiVersion", None)
        resource_ref = value_from.get("resourceFieldRef")
        if (
            isinstance(resource_ref, dict)
            and resource_ref.get("divisor") == "0"
        ):
            resource_ref.pop("divisor", None)
    return normalized


def _normalize_flux_resources(
    value: Any,
    context: str,
) -> Any:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RuntimeErrorEB(f"{context} resources contract is invalid")
    normalized = json.loads(json.dumps(value))
    for bucket_name in ("limits", "requests"):
        bucket = normalized.get(bucket_name)
        if bucket is None:
            continue
        if not isinstance(bucket, dict):
            raise RuntimeErrorEB(
                f"{context} {bucket_name} resources contract is invalid"
            )
        cpu = bucket.get("cpu")
        if cpu is not None:
            if not isinstance(cpu, str):
                raise RuntimeErrorEB(
                    f"{context} CPU resource quantity is invalid"
                )
            bucket["cpu"] = _parse_cpu_quantity(cpu)
        memory = bucket.get("memory")
        if memory is not None:
            if not isinstance(memory, str):
                raise RuntimeErrorEB(
                    f"{context} memory resource quantity is invalid"
                )
            bucket["memory"] = _parse_memory_quantity(memory)
    return normalized


def _kube_api_access_volume_name(value: Any) -> str | None:
    if not isinstance(value, dict) or set(value) != {"name", "projected"}:
        return None
    name = value.get("name")
    if not isinstance(name, str) or not name.startswith("kube-api-access-"):
        return None
    suffix = name.removeprefix("kube-api-access-")
    if len(suffix) != 5 or not suffix.isalnum():
        return None
    projected = value.get("projected")
    if (
        not isinstance(projected, dict)
        or set(projected) != {"defaultMode", "sources"}
        or projected.get("defaultMode") != 420
    ):
        return None
    sources = projected.get("sources")
    if not isinstance(sources, list) or len(sources) != 3:
        return None
    by_kind: dict[str, dict[str, Any]] = {}
    for source in sources:
        if not isinstance(source, dict) or len(source) != 1:
            return None
        kind = next(iter(source))
        if kind in by_kind or not isinstance(source[kind], dict):
            return None
        by_kind[kind] = source[kind]
    if set(by_kind) != {
        "serviceAccountToken",
        "configMap",
        "downwardAPI",
    }:
        return None
    token = by_kind["serviceAccountToken"]
    expiration = token.get("expirationSeconds")
    if (
        set(token) != {"expirationSeconds", "path"}
        or token.get("path") != "token"
        or isinstance(expiration, bool)
        or not isinstance(expiration, int)
        or expiration <= 0
    ):
        return None
    if by_kind["configMap"] != {
        "name": "kube-root-ca.crt",
        "items": [{"key": "ca.crt", "path": "ca.crt"}],
    }:
        return None
    if by_kind["downwardAPI"] != {
        "items": [
            {
                "path": "namespace",
                "fieldRef": {
                    "apiVersion": "v1",
                    "fieldPath": "metadata.namespace",
                },
            }
        ]
    }:
        return None
    return name


def _normalize_flux_pod_spec(
    pod_spec: Any,
    context: str,
) -> dict[str, Any]:
    if not isinstance(pod_spec, dict):
        raise RuntimeErrorEB(f"{context} Pod spec is invalid")
    normalized = json.loads(json.dumps(pod_spec))
    volumes = normalized.get("volumes")
    if volumes is None:
        volumes = []
        normalized["volumes"] = volumes
    if not isinstance(volumes, list) or any(
        not isinstance(volume, dict) for volume in volumes
    ):
        raise RuntimeErrorEB(f"{context} volume contract is invalid")

    injected_names = [
        name
        for volume in volumes
        if (name := _kube_api_access_volume_name(volume)) is not None
    ]
    if len(injected_names) == 1:
        injected_name = injected_names[0]
        containers: list[dict[str, Any]] = []
        valid_mounts = True
        for field in ("containers", "initContainers"):
            items = normalized.get(field, [])
            if not isinstance(items, list) or any(
                not isinstance(item, dict) for item in items
            ):
                raise RuntimeErrorEB(
                    f"{context} {field} inventory is invalid"
                )
            containers.extend(items)
        for container in containers:
            mounts = container.get("volumeMounts") or []
            if not isinstance(mounts, list) or any(
                not isinstance(mount, dict) for mount in mounts
            ):
                raise RuntimeErrorEB(
                    f"{context} container volumeMounts contract is invalid"
                )
            matches = [
                mount
                for mount in mounts
                if mount.get("name") == injected_name
            ]
            if matches != [
                {
                    "mountPath": (
                        "/var/run/secrets/kubernetes.io/serviceaccount"
                    ),
                    "name": injected_name,
                    "readOnly": True,
                }
            ]:
                valid_mounts = False
                break
        if valid_mounts and containers:
            normalized["volumes"] = [
                volume
                for volume in volumes
                if volume.get("name") != injected_name
            ]
            for container in containers:
                mounts = container.get("volumeMounts") or []
                remaining = [
                    mount
                    for mount in mounts
                    if mount.get("name") != injected_name
                ]
                if remaining:
                    container["volumeMounts"] = remaining
                else:
                    container.pop("volumeMounts", None)

    tolerations = normalized.get("tolerations") or []
    if not isinstance(tolerations, list) or any(
        not isinstance(item, dict) for item in tolerations
    ):
        raise RuntimeErrorEB(f"{context} toleration contract is invalid")
    remaining_tolerations = json.loads(json.dumps(tolerations))
    for default_toleration in (
        {
            "effect": "NoExecute",
            "key": "node.kubernetes.io/not-ready",
            "operator": "Exists",
            "tolerationSeconds": 300,
        },
        {
            "effect": "NoExecute",
            "key": "node.kubernetes.io/unreachable",
            "operator": "Exists",
            "tolerationSeconds": 300,
        },
    ):
        if default_toleration in remaining_tolerations:
            remaining_tolerations.remove(default_toleration)
    normalized["tolerations"] = remaining_tolerations

    for field in ("containers", "initContainers"):
        items = normalized.get(field, [])
        if not isinstance(items, list):
            raise RuntimeErrorEB(f"{context} {field} inventory is invalid")
        for container in items:
            if not isinstance(container, dict):
                raise RuntimeErrorEB(
                    f"{context} {field} container contract is invalid"
                )
            container["env"] = _normalize_flux_env(
                container.get("env"), context
            )
            container["resources"] = _normalize_flux_resources(
                container.get("resources"), context
            )
    return normalized


def _flux_strategy_projection(
    spec: dict[str, Any],
    context: str,
) -> dict[str, Any]:
    value = spec.get("strategy")
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise RuntimeErrorEB(f"{context} strategy contract is invalid")
    strategy = json.loads(json.dumps(value))
    strategy_type = strategy.get("type", "RollingUpdate")
    if strategy_type == "RollingUpdate":
        rolling = strategy.get("rollingUpdate")
        if rolling is None:
            rolling = {}
        if not isinstance(rolling, dict):
            raise RuntimeErrorEB(
                f"{context} rolling update contract is invalid"
            )
        strategy = {
            "type": "RollingUpdate",
            "rollingUpdate": {
                "maxSurge": rolling.get("maxSurge", "25%"),
                "maxUnavailable": rolling.get(
                    "maxUnavailable", "25%"
                ),
            },
        }
    return strategy


def _flux_pod_spec_projection(
    pod_spec: Any,
    context: str,
    *,
    synthesize_system_priority: bool = False,
) -> dict[str, Any]:
    normalized = _normalize_flux_pod_spec(pod_spec, context)
    projection = _application_pod_spec_projection(
        normalized,
        context,
        synthesize_system_priority=synthesize_system_priority,
    )
    pod_spec = normalized
    projection.update(
        {
            "hostNetwork": pod_spec.get("hostNetwork", False),
            "hostPID": pod_spec.get("hostPID", False),
            "hostIPC": pod_spec.get("hostIPC", False),
            "dnsPolicy": pod_spec.get("dnsPolicy", "ClusterFirst"),
            "dnsConfig": pod_spec.get("dnsConfig"),
            "priorityClassName": pod_spec.get("priorityClassName", ""),
            "tolerations": pod_spec.get("tolerations") or [],
            "restartPolicy": pod_spec.get("restartPolicy", "Always"),
            "schedulerName": pod_spec.get(
                "schedulerName", "default-scheduler"
            ),
            "enableServiceLinks": pod_spec.get("enableServiceLinks", True),
            "shareProcessNamespace": pod_spec.get(
                "shareProcessNamespace", False
            ),
            "runtimeClassName": pod_spec.get("runtimeClassName"),
        }
    )
    return projection


def _deployment_lifecycle_projection(
    spec: Any,
    context: str,
) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise RuntimeErrorEB(f"{context} Deployment spec is invalid")
    paused = spec.get("paused", False)
    min_ready_seconds = spec.get("minReadySeconds", 0)
    progress_deadline_seconds = spec.get("progressDeadlineSeconds", 600)
    if (
        not isinstance(paused, bool)
        or isinstance(min_ready_seconds, bool)
        or not isinstance(min_ready_seconds, int)
        or min_ready_seconds < 0
        or isinstance(progress_deadline_seconds, bool)
        or not isinstance(progress_deadline_seconds, int)
        or progress_deadline_seconds < 1
        or progress_deadline_seconds <= min_ready_seconds
    ):
        raise RuntimeErrorEB(
            f"{context} Deployment lifecycle contract is invalid"
        )
    return {
        "paused": paused,
        "minReadySeconds": min_ready_seconds,
        "progressDeadlineSeconds": progress_deadline_seconds,
    }


def _deployment_rollout_projection(
    spec: Any,
    context: str,
) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise RuntimeErrorEB(f"{context} Deployment spec is invalid")
    revision_history_limit = spec.get("revisionHistoryLimit", 10)
    if (
        isinstance(revision_history_limit, bool)
        or not isinstance(revision_history_limit, int)
        or revision_history_limit < 0
    ):
        raise RuntimeErrorEB(
            f"{context} Deployment rollout contract is invalid"
        )
    return {
        "revisionHistoryLimit": revision_history_limit,
        "strategy": _flux_strategy_projection(spec, context),
    }


def _daemonset_rollout_projection(
    spec: Any,
    context: str,
) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise RuntimeErrorEB(f"{context} DaemonSet spec is invalid")
    min_ready_seconds = spec.get("minReadySeconds", 0)
    revision_history_limit = spec.get("revisionHistoryLimit", 10)
    update_strategy = spec.get("updateStrategy")
    if update_strategy is None:
        update_strategy = {}
    if (
        isinstance(min_ready_seconds, bool)
        or not isinstance(min_ready_seconds, int)
        or min_ready_seconds < 0
        or isinstance(revision_history_limit, bool)
        or not isinstance(revision_history_limit, int)
        or revision_history_limit < 0
        or not isinstance(update_strategy, dict)
    ):
        raise RuntimeErrorEB(
            f"{context} DaemonSet rollout contract is invalid"
        )
    update_strategy = json.loads(json.dumps(update_strategy))
    strategy_type = update_strategy.get("type", "RollingUpdate")
    if not isinstance(strategy_type, str) or not strategy_type:
        raise RuntimeErrorEB(
            f"{context} DaemonSet update strategy contract is invalid"
        )
    if strategy_type == "RollingUpdate":
        rolling = update_strategy.get("rollingUpdate")
        if rolling is None:
            rolling = {}
        if not isinstance(rolling, dict):
            raise RuntimeErrorEB(
                f"{context} DaemonSet rolling update contract is invalid"
            )
        update_strategy = {
            "type": "RollingUpdate",
            "rollingUpdate": {
                "maxUnavailable": rolling.get("maxUnavailable", 1),
                "maxSurge": rolling.get("maxSurge", 0),
            },
        }
    return {
        "minReadySeconds": min_ready_seconds,
        "revisionHistoryLimit": revision_history_limit,
        "updateStrategy": update_strategy,
    }


def _flux_deployment_contract(
    deployment: Any,
    context: str,
) -> dict[str, Any]:
    if not isinstance(deployment, dict):
        raise RuntimeErrorEB(f"{context} Deployment is invalid")
    spec = deployment.get("spec", {})
    template = spec.get("template", {}) if isinstance(spec, dict) else {}
    template_metadata = (
        template.get("metadata", {}) if isinstance(template, dict) else {}
    )
    pod_spec = template.get("spec", {}) if isinstance(template, dict) else {}
    replicas = spec.get("replicas", 1) if isinstance(spec, dict) else None
    if (
        isinstance(replicas, bool)
        or not isinstance(replicas, int)
        or replicas != 1
        or not isinstance(template_metadata, dict)
    ):
        raise RuntimeErrorEB(f"{context} replica/template contract is invalid")
    labels = template_metadata.get("labels", {})
    annotations = template_metadata.get("annotations", {})
    if not isinstance(labels, dict) or not isinstance(annotations, dict):
        raise RuntimeErrorEB(f"{context} template metadata contract is invalid")
    pod_contract = _flux_pod_spec_projection(
        pod_spec,
        context,
        synthesize_system_priority=True,
    )
    deployment_contract = {
        "replicas": replicas,
        "revisionHistoryLimit": spec.get("revisionHistoryLimit", 10),
        "strategy": _flux_strategy_projection(spec, context),
        **_deployment_lifecycle_projection(spec, context),
        "selector_labels": _pod_selector_match_labels(deployment, context),
        "template_labels": labels,
        "template_annotations": annotations,
        "pod_spec": pod_contract,
    }
    return {
        "replicas": replicas,
        "selector_labels": deployment_contract["selector_labels"],
        "images": _pod_spec_images(pod_spec, context),
        "contract": deployment_contract,
        "contract_sha256": _stable_json_sha256(deployment_contract),
        "pod_contract_sha256": _stable_json_sha256(pod_contract),
    }


def _expected_flux_controller_contract(
    root: Path,
    toolchain_receipt: dict[str, Any] | None = None,
    source_commit: str | None = None,
) -> dict[str, Any]:
    receipt = toolchain_receipt or toolchain(root, source_commit)
    tools = receipt.get("tools", {}) if isinstance(receipt, dict) else {}
    flux = tools.get("flux") if isinstance(tools, dict) else None
    if not isinstance(flux, str) or not flux:
        raise RuntimeErrorEB("pinned Flux toolchain binding is unavailable")
    rendered = run(_flux_install_argv(flux, export=True)).stdout
    try:
        documents = [
            item
            for item in yaml.safe_load_all(rendered)
            if isinstance(item, dict)
        ]
    except yaml.YAMLError as exc:
        raise RuntimeErrorEB("pinned Flux install render is invalid") from exc
    deployments = [
        document
        for document in documents
        if document.get("kind") == "Deployment"
        and document.get("metadata", {}).get("namespace") == "flux-system"
    ]
    names = {
        str(document.get("metadata", {}).get("name", ""))
        for document in deployments
    }
    if names != EXPECTED_FLUX_CONTROLLERS or len(deployments) != len(names):
        raise RuntimeErrorEB("pinned Flux controller Deployment set drifted")
    return {
        str(deployment["metadata"]["name"]): _flux_deployment_contract(
            deployment,
            f"pinned Flux Deployment {deployment['metadata']['name']}",
        )
        for deployment in deployments
    }


def _require_live_flux_controller_contract(
    root: Path,
    toolchain_receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    expected = _expected_flux_controller_contract(root, toolchain_receipt)
    items = _kubectl_json(
        root, ["-n", "flux-system", "get", "deployments"]
    ).get("items")
    pods = _kubectl_json(
        root, ["-n", "flux-system", "get", "pods"]
    ).get("items")
    if (
        not isinstance(items, list)
        or any(not isinstance(item, dict) for item in items)
        or not isinstance(pods, list)
        or any(not isinstance(item, dict) for item in pods)
    ):
        raise RuntimeErrorEB("Flux controller live inventory is invalid")
    selected = [
        item
        for item in items
        if str(item.get("metadata", {}).get("name", ""))
        in EXPECTED_FLUX_CONTROLLERS
    ]
    by_name = {
        str(item.get("metadata", {}).get("name", "")): item
        for item in selected
    }
    if (
        len(selected) != len(EXPECTED_FLUX_CONTROLLERS)
        or set(by_name) != EXPECTED_FLUX_CONTROLLERS
    ):
        missing = sorted(EXPECTED_FLUX_CONTROLLERS - set(by_name))
        raise RuntimeErrorEB(
            "Flux controller Deployment set is incomplete or duplicated: "
            f"{missing}"
        )
    result: dict[str, Any] = {}
    for name, contract_value in expected.items():
        deployment = by_name[name]
        metadata = deployment.get("metadata", {})
        if (
            not isinstance(metadata, dict)
            or metadata.get("namespace") != "flux-system"
            or metadata.get("deletionTimestamp") is not None
        ):
            raise RuntimeErrorEB(f"Flux controller identity drifted: {name}")
        observed = _flux_deployment_contract(
            deployment, f"live Flux Deployment {name}"
        )
        if observed["contract"] != contract_value["contract"]:
            raise RuntimeErrorEB(
                f"Flux controller Deployment contract drifted: {name}"
            )
        availability = _deployment_availability_snapshot(deployment, name, 1)
        matching_pods = _pods_matching_labels(
            pods, contract_value["selector_labels"]
        )
        pod_readback = _require_running_pod_image_contract(
            matching_pods,
            namespace="flux-system",
            workload=name,
            expected_replicas=1,
            expected_images=contract_value["images"],
            required_labels=contract_value["selector_labels"],
            context="Flux controller Pod",
        )
        pod_names: list[str] = []
        for pod in matching_pods:
            pod_metadata = (
                pod.get("metadata", {}) if isinstance(pod, dict) else {}
            )
            pod_spec = pod.get("spec", {}) if isinstance(pod, dict) else {}
            labels = (
                pod_metadata.get("labels", {})
                if isinstance(pod_metadata, dict)
                else {}
            )
            annotations = (
                pod_metadata.get("annotations", {})
                if isinstance(pod_metadata, dict)
                else {}
            )
            pod_name = (
                pod_metadata.get("name")
                if isinstance(pod_metadata, dict)
                else None
            )
            if (
                not isinstance(pod_name, str)
                or not pod_name
                or pod_name in pod_names
                or pod_metadata.get("namespace") != "flux-system"
                or pod_metadata.get("deletionTimestamp") is not None
                or not isinstance(labels, dict)
                or not isinstance(annotations, dict)
                or any(
                    labels.get(key) != value
                    for key, value in contract_value["contract"][
                        "template_labels"
                    ].items()
                )
                or any(
                    annotations.get(key) != value
                    for key, value in contract_value["contract"][
                        "template_annotations"
                    ].items()
                )
                or _flux_pod_spec_projection(
                    pod_spec, f"live Flux Pod {pod_name}"
                )
                != contract_value["contract"]["pod_spec"]
            ):
                raise RuntimeErrorEB(
                    f"Flux controller Pod contract drifted: {name}"
                )
            pod_names.append(pod_name)
        result[name] = {
            **availability,
            "images": observed["images"],
            "images_sha256": _stable_json_sha256(observed["images"]),
            "images_canonical": True,
            "pods": pod_readback,
            "contract_sha256": contract_value["contract_sha256"],
            "pod_contract_sha256": contract_value[
                "pod_contract_sha256"
            ],
            "pod_names": sorted(pod_names),
            "canonical": True,
        }

    expected_pod_names = {
        pod_name
        for controller in result.values()
        for pod_name in controller["pod_names"]
    }
    observed_pod_names: set[str] = set()
    for pod in pods:
        metadata = pod.get("metadata", {})
        pod_name = (
            metadata.get("name")
            if isinstance(metadata, dict)
            else None
        )
        if (
            not isinstance(metadata, dict)
            or not isinstance(pod_name, str)
            or not pod_name
            or metadata.get("namespace") != "flux-system"
            or metadata.get("deletionTimestamp") is not None
            or pod_name in observed_pod_names
        ):
            raise RuntimeErrorEB(
                "Flux controller Pod inventory contains invalid or duplicate Pods"
            )
        observed_pod_names.add(pod_name)
    if observed_pod_names != expected_pod_names:
        missing = sorted(expected_pod_names - observed_pod_names)
        unexpected = sorted(observed_pod_names - expected_pod_names)
        raise RuntimeErrorEB(
            "Flux controller Pod inventory contains noncanonical Pods: "
            f"missing={missing}; unexpected={unexpected}"
        )
    return result


def _require_flux_runtime_baseline(
    root: Path,
    source_commit: str,
    flux_readback: dict[str, Any],
) -> dict[str, str]:
    path = root / "receipts/platform.json"
    try:
        platform = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeErrorEB(
            "Experiment-B status requires valid Flux runtime baseline"
        ) from exc
    baseline = platform.get("flux_runtime_image_ids")
    if (
        not isinstance(platform, dict)
        or platform.get("schema_version") != 1
        or platform.get("status") != "ready"
        or platform.get("source_commit") != source_commit
        or not isinstance(baseline, dict)
        or set(baseline) != EXPECTED_FLUX_CONTROLLERS
        or any(
            not isinstance(value, str)
            or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in baseline.values()
        )
    ):
        raise RuntimeErrorEB("Experiment-B Flux runtime baseline is invalid")
    current = {
        name: controller.get("pods", {}).get("runtime_image_ids_sha256")
        for name, controller in flux_readback.items()
    }
    if current != baseline:
        raise RuntimeErrorEB(
            "live Flux controller runtime image IDs drifted from platform installation"
        )
    return {str(key): str(value) for key, value in baseline.items()}


def _container_images(document: Any, context: str) -> dict[str, str]:
    if not isinstance(document, dict):
        raise RuntimeErrorEB(f"{context} payload is not an object")
    containers = (
        document.get("spec", {})
        .get("template", {})
        .get("spec", {})
        .get("containers", [])
    )
    if not isinstance(containers, list):
        raise RuntimeErrorEB(f"{context} container inventory is invalid")
    images: dict[str, str] = {}
    for item in containers:
        if not isinstance(item, dict):
            raise RuntimeErrorEB(f"{context} container inventory is invalid")
        name = str(item.get("name", ""))
        image = str(item.get("image", ""))
        if not name or not image or name in images:
            raise RuntimeErrorEB(f"{context} container identity is invalid")
        images[name] = image
    return images


MIGRATION_JOB_NAME = "commonthing-experiment-b-migration"
_MIGRATION_CONTROLLER_LABELS = frozenset(
    {
        "batch.kubernetes.io/controller-uid",
        "batch.kubernetes.io/job-name",
        "controller-uid",
        "job-name",
    }
)


def _migration_template_metadata_projection(
    metadata: Any,
    context: str,
) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        raise RuntimeErrorEB(f"{context} template metadata is invalid")
    labels = metadata.get("labels", {})
    annotations = metadata.get("annotations", {})
    if not isinstance(labels, dict) or not isinstance(annotations, dict):
        raise RuntimeErrorEB(f"{context} template metadata contract is invalid")
    return {
        "labels": {
            str(key): str(value)
            for key, value in sorted(labels.items())
            if key not in _MIGRATION_CONTROLLER_LABELS
        },
        "annotations": {
            str(key): str(value)
            for key, value in sorted(annotations.items())
        },
    }


def _migration_job_contract(
    job: Any,
    context: str,
) -> dict[str, Any]:
    if not isinstance(job, dict):
        raise RuntimeErrorEB(f"{context} Job payload is invalid")
    spec = job.get("spec", {})
    template = spec.get("template", {}) if isinstance(spec, dict) else {}
    template_metadata = (
        template.get("metadata", {}) if isinstance(template, dict) else {}
    )
    pod_spec = template.get("spec", {}) if isinstance(template, dict) else {}
    if not isinstance(spec, dict) or not isinstance(template, dict):
        raise RuntimeErrorEB(f"{context} Job spec/template is invalid")

    parallelism = spec.get("parallelism", 1)
    completions = spec.get("completions", 1)
    backoff_limit = spec.get("backoffLimit", 6)
    suspend = spec.get("suspend", False)
    manual_selector = spec.get("manualSelector", False)
    if suspend is None:
        suspend = False
    if manual_selector is None:
        manual_selector = False
    if (
        isinstance(parallelism, bool)
        or not isinstance(parallelism, int)
        or parallelism < 1
        or isinstance(completions, bool)
        or not isinstance(completions, int)
        or completions < 1
        or isinstance(backoff_limit, bool)
        or not isinstance(backoff_limit, int)
        or backoff_limit < 0
        or not isinstance(suspend, bool)
        or not isinstance(manual_selector, bool)
    ):
        raise RuntimeErrorEB(f"{context} Job execution contract is invalid")
    metadata_contract = _migration_template_metadata_projection(
        template_metadata, context
    )
    pod_contract = _application_pod_spec_projection(
        pod_spec, f"{context} Pod template"
    )
    return {
        "parallelism": parallelism,
        "completions": completions,
        "backoffLimit": backoff_limit,
        "activeDeadlineSeconds": spec.get("activeDeadlineSeconds"),
        "ttlSecondsAfterFinished": spec.get("ttlSecondsAfterFinished"),
        "completionMode": spec.get("completionMode", "NonIndexed"),
        "suspend": suspend,
        "manualSelector": manual_selector,
        "podFailurePolicy": spec.get("podFailurePolicy"),
        "successPolicy": spec.get("successPolicy"),
        "backoffLimitPerIndex": spec.get("backoffLimitPerIndex"),
        "maxFailedIndexes": spec.get("maxFailedIndexes"),
        "managedBy": spec.get("managedBy"),
        "template_labels": metadata_contract["labels"],
        "template_annotations": metadata_contract["annotations"],
        "pod_spec": pod_contract,
    }



def _rendered_migration_job_contract(
    root: Path,
    api_digest: str,
    source_commit: str | None = None,
) -> dict[str, Any]:
    if not DIGEST_RE.fullmatch(api_digest):
        raise RuntimeErrorEB(
            "migration Job contract requires an exact API digest"
        )
    if source_commit is None:
        kustomize = toolchain(root)["tools"].get("kustomize")
        if not isinstance(kustomize, str) or not kustomize:
            raise RuntimeErrorEB(
                "migration Job contract requires pinned kustomize"
            )
        rendered = run([kustomize, "build", str(MIGRATION)]).stdout
    else:
        rendered = _source_commit_kustomize_build(
            root,
            source_commit,
            MIGRATION,
            MIGRATION,
        )
    rendered = rendered.replace("$" + "{API_DIGEST}", api_digest)
    try:
        documents = [
            document
            for document in yaml.safe_load_all(rendered)
            if isinstance(document, dict)
        ]
    except yaml.YAMLError as exc:
        raise RuntimeErrorEB(
            "rendered Experiment-B migration contract is invalid"
        ) from exc
    matches = [
        document
        for document in documents
        if document.get("kind") == "Job"
        and document.get("metadata", {}).get("name")
        == MIGRATION_JOB_NAME
        and document.get("metadata", {}).get("namespace")
        == APP_NAMESPACE
    ]
    if len(matches) != 1:
        raise RuntimeErrorEB(
            "rendered Experiment-B migration Job is ambiguous"
        )
    contract_value = _migration_job_contract(
        matches[0], "rendered Experiment-B migration Job"
    )
    return {
        "contract": contract_value,
        "contract_sha256": _stable_json_sha256(contract_value),
        "pod_contract_sha256": _stable_json_sha256(
            contract_value["pod_spec"]
        ),
    }

def _require_migration_job_runtime_contract(
    root: Path,
    migration: Any,
    migration_pods: Any,
    api_digest: str,
    source_commit: str | None = None,
) -> dict[str, Any]:
    expected = _rendered_migration_job_contract(
        root,
        api_digest,
        source_commit,
    )
    if not isinstance(migration, dict):
        raise RuntimeErrorEB("Experiment-B migration Job is invalid")
    metadata = migration.get("metadata", {})
    status = migration.get("status", {})
    if (
        not isinstance(metadata, dict)
        or metadata.get("name") != MIGRATION_JOB_NAME
        or metadata.get("namespace") != APP_NAMESPACE
        or metadata.get("deletionTimestamp") is not None
        or not isinstance(metadata.get("uid"), str)
        or not metadata.get("uid")
        or not isinstance(status, dict)
    ):
        raise RuntimeErrorEB(
            "Experiment-B migration Job identity/status drifted"
        )
    observed_contract = _migration_job_contract(
        migration, "live Experiment-B migration Job"
    )
    if observed_contract != expected["contract"]:
        raise RuntimeErrorEB(
            "Experiment-B migration Job contract drifted"
        )
    complete = any(
        isinstance(condition, dict)
        and condition.get("type") == "Complete"
        and condition.get("status") == "True"
        for condition in status.get("conditions", [])
    )
    succeeded = status.get("succeeded", 0) or 0
    if (
        isinstance(succeeded, bool)
        or not isinstance(succeeded, int)
        or succeeded != expected["contract"]["completions"]
        or not complete
    ):
        raise RuntimeErrorEB(
            "Experiment-B migration Job is not canonically complete"
        )
    if not isinstance(migration_pods, list) or not migration_pods:
        raise RuntimeErrorEB(
            "Experiment-B migration Pod inventory is empty or invalid"
        )
    job_uid = metadata["uid"]
    expected_labels = expected["contract"]["template_labels"]
    expected_annotations = expected["contract"]["template_annotations"]
    expected_pod_spec = expected["contract"]["pod_spec"]
    expected_containers = expected_pod_spec["containers"]
    expected_init = expected_pod_spec["init_containers"]
    pod_names: list[str] = []
    succeeded_pods = 0
    for pod in migration_pods:
        if not isinstance(pod, dict):
            raise RuntimeErrorEB(
                "Experiment-B migration Pod inventory is invalid"
            )
        pod_metadata = pod.get("metadata", {})
        pod_spec = pod.get("spec", {})
        pod_status = pod.get("status", {})
        if (
            not isinstance(pod_metadata, dict)
            or not isinstance(pod_status, dict)
        ):
            raise RuntimeErrorEB(
                "Experiment-B migration Pod metadata/status is invalid"
            )
        pod_name = pod_metadata.get("name")
        if (
            not isinstance(pod_name, str)
            or not pod_name
            or pod_name in pod_names
            or pod_metadata.get("namespace") != APP_NAMESPACE
            or pod_metadata.get("deletionTimestamp") is not None
        ):
            raise RuntimeErrorEB(
                "Experiment-B migration Pod identity drifted"
            )
        owners = pod_metadata.get("ownerReferences", [])
        owner_matches = [
            owner
            for owner in owners
            if isinstance(owner, dict)
            and owner.get("apiVersion") == "batch/v1"
            and owner.get("kind") == "Job"
            and owner.get("name") == MIGRATION_JOB_NAME
            and owner.get("uid") == job_uid
            and owner.get("controller") is True
        ] if isinstance(owners, list) else []
        if len(owner_matches) != 1:
            raise RuntimeErrorEB(
                "Experiment-B migration Pod owner binding drifted"
            )
        pod_metadata_contract = _migration_template_metadata_projection(
            pod_metadata, f"live Experiment-B migration Pod {pod_name}"
        )
        if any(
            pod_metadata_contract["labels"].get(key) != value
            for key, value in expected_labels.items()
        ) or any(
            pod_metadata_contract["annotations"].get(key) != value
            for key, value in expected_annotations.items()
        ):
            raise RuntimeErrorEB(
                "Experiment-B migration Pod metadata contract drifted"
            )
        if (
            _application_pod_spec_projection(
                pod_spec, f"live Experiment-B migration Pod {pod_name}"
            )
            != expected_pod_spec
        ):
            raise RuntimeErrorEB(
                "Experiment-B migration Pod contract drifted"
            )
        phase = pod_status.get("phase")
        if phase not in {"Succeeded", "Failed"}:
            raise RuntimeErrorEB(
                "Experiment-B migration Pod is not terminal"
            )
        if phase == "Succeeded":
            succeeded_pods += 1
            for field, expected_group in (
                ("containerStatuses", expected_containers),
                ("initContainerStatuses", expected_init),
            ):
                statuses = pod_status.get(field, [])
                if statuses is None:
                    statuses = []
                if (
                    not isinstance(statuses, list)
                    or any(not isinstance(item, dict) for item in statuses)
                ):
                    raise RuntimeErrorEB(
                        "Experiment-B migration Pod runtime status is invalid"
                    )
                by_name = {
                    str(item.get("name", "")): item
                    for item in statuses
                    if item.get("name")
                }
                if set(by_name) != set(expected_group):
                    raise RuntimeErrorEB(
                        "Experiment-B migration Pod container set drifted"
                    )
                for name, expected_container in expected_group.items():
                    runtime_status = by_name[name]
                    image = expected_container.get("image")
                    image_id = runtime_status.get("imageID")
                    if (
                        isinstance(image, str)
                        and "@" in image
                        and not _runtime_image_id_matches_digest(
                            image_id, image.rsplit("@", 1)[1]
                        )
                    ):
                        raise RuntimeErrorEB(
                            "Experiment-B migration Pod runtime image drifted"
                        )
                    terminated = (
                        runtime_status.get("state", {}).get("terminated")
                        if isinstance(runtime_status.get("state"), dict)
                        else None
                    )
                    if (
                        not isinstance(terminated, dict)
                        or terminated.get("exitCode") != 0
                    ):
                        raise RuntimeErrorEB(
                            "Experiment-B migration Pod did not terminate cleanly"
                        )
        pod_names.append(pod_name)

    if succeeded_pods != expected["contract"]["completions"]:
        raise RuntimeErrorEB(
            "Experiment-B migration succeeded Pod count drifted"
        )
    return {
        "contract_sha256": expected["contract_sha256"],
        "pod_contract_sha256": expected["pod_contract_sha256"],
        "pod_names": sorted(pod_names),
        "succeeded_pods": succeeded_pods,
        "canonical": True,
    }


def _require_requested_release_artifacts(
    root: Path,
    api: Any,
    web: Any,
    migration: Any,
    migration_pods: Any,
    api_digest: str,
    web_digest: str,
    api_replicas: int,
    web_replicas: int,
    source_commit: str | None = None,
) -> dict[str, Any]:
    expected_api = f"ghcr.io/heimgewebe/commonthing-api@{api_digest}"
    expected_web = f"ghcr.io/heimgewebe/commonthing-web@{web_digest}"
    api_images = _container_images(api, "Experiment-B API Deployment")
    web_images = _container_images(web, "Experiment-B Web Deployment")
    migration_images = _container_images(migration, "Experiment-B migration Job")

    if api_images.get("api") != expected_api:
        raise RuntimeErrorEB("live API image does not match immutable release digest")
    if api_images.get("search-worker") != expected_api:
        raise RuntimeErrorEB("live search-worker image does not match API release digest")
    if web_images.get("web") != expected_web:
        raise RuntimeErrorEB("live Web image does not match immutable release digest")
    if migration_images.get("migration") != expected_api:
        raise RuntimeErrorEB("live migration image does not match API release digest")

    deployments = {
        "weltgewebe-api": _deployment_availability_snapshot(
            api, "weltgewebe-api", api_replicas
        ),
        "weltgewebe-web": _deployment_availability_snapshot(
            web, "weltgewebe-web", web_replicas
        ),
    }
    migration_readback = _require_migration_job_runtime_contract(
        root,
        migration,
        migration_pods,
        api_digest,
        source_commit,
    )

    return {
        "deployments": deployments,
        "images": {
            "api": api_images.get("api"),
            "web": web_images.get("web"),
            "search_worker": api_images.get("search-worker"),
            "migration": migration_images.get("migration"),
        },
        "migration": migration_readback,
        "migration_complete": True,
    }


def _runtime_image_id_digest(image_id: Any) -> str | None:
    if not isinstance(image_id, str) or not image_id:
        return None
    candidate = image_id
    if "@" in candidate:
        candidate = candidate.rsplit("@", 1)[1]
    elif "://" in candidate:
        candidate = candidate.split("://", 1)[1]
    return candidate if DIGEST_RE.fullmatch(candidate) else None


def _runtime_image_id_matches_digest(image_id: Any, expected_digest: str) -> bool:
    if not DIGEST_RE.fullmatch(expected_digest):
        return False
    observed_digest = _runtime_image_id_digest(image_id)
    return (
        observed_digest is not None
        and secrets.compare_digest(observed_digest, expected_digest)
    )


def _pod_selector_match_labels(workload: Any, context: str) -> dict[str, str]:
    if not isinstance(workload, dict):
        raise RuntimeErrorEB(f"{context} workload is invalid")
    selector = workload.get("spec", {}).get("selector", {})
    if not isinstance(selector, dict):
        raise RuntimeErrorEB(f"{context} selector is invalid")
    labels = selector.get("matchLabels")
    expressions = selector.get("matchExpressions", [])
    if (
        not isinstance(labels, dict)
        or not labels
        or any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, str)
            or not value
            for key, value in labels.items()
        )
        or expressions not in (None, [])
    ):
        raise RuntimeErrorEB(f"{context} selector is not an exact matchLabels contract")
    return {str(key): str(value) for key, value in labels.items()}


def _pods_matching_labels(pods: Any, required_labels: dict[str, str]) -> list[dict[str, Any]]:
    if not isinstance(pods, list) or any(not isinstance(item, dict) for item in pods):
        raise RuntimeErrorEB("Pod inventory is invalid")
    matches: list[dict[str, Any]] = []
    for pod in pods:
        metadata = pod.get("metadata", {})
        labels = metadata.get("labels", {}) if isinstance(metadata, dict) else {}
        if (
            isinstance(labels, dict)
            and all(labels.get(key) == value for key, value in required_labels.items())
        ):
            matches.append(pod)
    return matches


def _pod_runtime_image_ids_sha256(
    pods: Any,
    expected_images: dict[str, dict[str, str]],
) -> str:
    if (
        not isinstance(pods, dict)
        or any(not isinstance(pod, dict) for pod in pods.values())
    ):
        raise RuntimeErrorEB("Pod runtime image-ID binding is invalid")
    binding: dict[str, dict[str, list[str]]] = {
        "containers": {},
        "init_containers": {},
    }
    for group in ("containers", "init_containers"):
        for container_name in expected_images[group]:
            values: list[str] = []
            for pod in pods.values():
                runtime_ids = pod.get("runtime_image_ids", {})
                group_ids = (
                    runtime_ids.get(group, {})
                    if isinstance(runtime_ids, dict)
                    else {}
                )
                image_id = (
                    group_ids.get(container_name)
                    if isinstance(group_ids, dict)
                    else None
                )
                observed_digest = _runtime_image_id_digest(image_id)
                if observed_digest is None:
                    raise RuntimeErrorEB("Pod runtime image-ID binding is incomplete")
                values.append(observed_digest)
            binding[group][container_name] = sorted(values)
    return _stable_json_sha256(binding)


def _require_running_pod_active_deadline_contract(
    pods: Any,
    *,
    expected_active_deadline_seconds: int | None,
    context: str,
) -> None:
    if not isinstance(pods, list):
        raise RuntimeErrorEB(f"{context} inventory is invalid")
    for pod in pods:
        if not isinstance(pod, dict):
            raise RuntimeErrorEB(f"{context} inventory is invalid")
        metadata = pod.get("metadata", {})
        spec = pod.get("spec", {})
        name = metadata.get("name") if isinstance(metadata, dict) else None
        observed = _pod_active_deadline_seconds(
            spec,
            f"{context} {name or '<unknown>'}",
        )
        if observed != expected_active_deadline_seconds:
            raise RuntimeErrorEB(
                f"{context} activeDeadlineSeconds drifted from workload contract"
            )


def _require_running_pod_image_contract(
    pods: Any,
    *,
    namespace: str,
    workload: str,
    expected_replicas: int,
    expected_images: dict[str, dict[str, str]],
    required_labels: dict[str, str],
    context: str,
) -> dict[str, Any]:
    if (
        not isinstance(namespace, str)
        or not namespace
        or not isinstance(workload, str)
        or not workload
        or not isinstance(expected_replicas, int)
        or isinstance(expected_replicas, bool)
        or expected_replicas < 1
        or not isinstance(expected_images, dict)
        or set(expected_images) != {"containers", "init_containers"}
        or not isinstance(expected_images.get("containers"), dict)
        or not expected_images["containers"]
        or not isinstance(expected_images.get("init_containers"), dict)
        or not isinstance(required_labels, dict)
        or not required_labels
    ):
        raise RuntimeErrorEB(f"{context} contract is invalid: {workload}")
    for group in ("containers", "init_containers"):
        if any(
            not isinstance(name, str)
            or not name
            or not isinstance(image, str)
            or not image
            for name, image in expected_images[group].items()
        ):
            raise RuntimeErrorEB(f"{context} image contract is invalid: {workload}/{group}")
    if not isinstance(pods, list) or len(pods) != expected_replicas:
        raise RuntimeErrorEB(
            f"{context} set does not match exact replica contract: {workload}"
        )

    expected_images_sha256 = _stable_json_sha256(expected_images)
    expected_digests: dict[str, dict[str, str | None]] = {
        "containers": {},
        "init_containers": {},
    }
    for group in ("containers", "init_containers"):
        for name, image in expected_images[group].items():
            digest: str | None = None
            if "@" in image:
                _repository, candidate = image.rsplit("@", 1)
                if not DIGEST_RE.fullmatch(candidate):
                    raise RuntimeErrorEB(
                        f"{context} image digest is invalid: {workload}/{name}"
                    )
                digest = candidate
            expected_digests[group][name] = digest

    observed: dict[str, Any] = {}
    for pod in pods:
        if not isinstance(pod, dict):
            raise RuntimeErrorEB(f"{context} inventory is invalid: {workload}")
        metadata = pod.get("metadata", {})
        spec = pod.get("spec", {})
        status_obj = pod.get("status", {})
        if (
            not isinstance(metadata, dict)
            or not isinstance(spec, dict)
            or not isinstance(status_obj, dict)
        ):
            raise RuntimeErrorEB(f"{context} inventory is invalid: {workload}")
        ephemeral_statuses = status_obj.get("ephemeralContainerStatuses", [])
        if not isinstance(ephemeral_statuses, list):
            raise RuntimeErrorEB(
                f"{context} ephemeral container status inventory is invalid: "
                f"{workload}"
            )
        if ephemeral_statuses:
            raise RuntimeErrorEB(
                f"{context} ephemeral container statuses are forbidden: "
                f"{workload}"
            )
        name = metadata.get("name")
        labels = metadata.get("labels", {})
        if (
            not isinstance(name, str)
            or not name
            or name in observed
            or metadata.get("namespace") != namespace
            or metadata.get("deletionTimestamp") is not None
            or not isinstance(labels, dict)
            or not all(labels.get(key) == value for key, value in required_labels.items())
            or status_obj.get("phase") != "Running"
        ):
            raise RuntimeErrorEB(f"{context} identity/state drifted: {workload}")

        ready = any(
            isinstance(condition, dict)
            and condition.get("type") == "Ready"
            and condition.get("status") == "True"
            for condition in status_obj.get("conditions", [])
        )
        if not ready:
            raise RuntimeErrorEB(f"{context} is not Ready: {workload}/{name}")

        live_images = _pod_spec_images(spec, f"{context} {name}")
        if live_images != expected_images:
            raise RuntimeErrorEB(
                f"{context} requested images drifted: {workload}/{name}"
            )

        runtime_image_ids: dict[str, dict[str, str]] = {
            "containers": {},
            "init_containers": {},
        }
        for status_field, group, must_be_running in (
            ("containerStatuses", "containers", True),
            ("initContainerStatuses", "init_containers", False),
        ):
            statuses = status_obj.get(status_field, [])
            if not isinstance(statuses, list):
                raise RuntimeErrorEB(
                    f"{context} container status is invalid: {workload}/{name}/{group}"
                )
            status_by_name: dict[str, dict[str, Any]] = {}
            for item in statuses:
                if not isinstance(item, dict):
                    raise RuntimeErrorEB(
                        f"{context} container status is invalid: {workload}/{name}/{group}"
                    )
                container_name = item.get("name")
                if (
                    not isinstance(container_name, str)
                    or not container_name
                    or container_name in status_by_name
                ):
                    raise RuntimeErrorEB(
                        f"{context} container status identity is invalid: "
                        f"{workload}/{name}/{group}"
                    )
                status_by_name[container_name] = item
            if set(status_by_name) != set(expected_images[group]):
                raise RuntimeErrorEB(
                    f"{context} container status set drifted: {workload}/{name}/{group}"
                )

            for container_name, requested_image in expected_images[group].items():
                item = status_by_name[container_name]
                state = item.get("state", {})
                image_id = item.get("imageID")
                observed_digest = _runtime_image_id_digest(image_id)
                expected_digest = expected_digests[group][container_name]
                state_valid = False
                if isinstance(state, dict):
                    if must_be_running:
                        state_valid = (
                            item.get("ready") is True
                            and isinstance(state.get("running"), dict)
                        )
                    else:
                        terminated = state.get("terminated")
                        state_valid = (
                            isinstance(state.get("running"), dict)
                            or (
                                isinstance(terminated, dict)
                                and terminated.get("exitCode") == 0
                            )
                        )
                if (
                    not state_valid
                    or observed_digest is None
                    or (
                        expected_digest is not None
                        and not secrets.compare_digest(observed_digest, expected_digest)
                    )
                ):
                    raise RuntimeErrorEB(
                        f"{context} runtime image ID drifted: "
                        f"{workload}/{name}/{container_name}"
                    )
                runtime_image_ids[group][container_name] = str(image_id)

        observed[name] = {
            "ready": True,
            "requested_images_sha256": expected_images_sha256,
            "runtime_image_ids": runtime_image_ids,
        }

    return {
        "expected_replicas": expected_replicas,
        "observed_replicas": len(observed),
        "requested_images_sha256": expected_images_sha256,
        "runtime_image_ids_sha256": _pod_runtime_image_ids_sha256(
            observed, expected_images
        ),
        "images_canonical": True,
        "pods": observed,
    }


def _require_running_pod_images(
    pods: Any,
    workload: str,
    expected_replicas: int,
    expected_images: dict[str, str],
) -> dict[str, Any]:
    result = _require_running_pod_image_contract(
        pods,
        namespace=APP_NAMESPACE,
        workload=workload,
        expected_replicas=expected_replicas,
        expected_images={"containers": expected_images, "init_containers": {}},
        required_labels={"app.kubernetes.io/name": workload},
        context="application Pod",
    )
    flat_sha256 = _stable_json_sha256(expected_images)
    result["requested_images_sha256"] = flat_sha256
    result.pop("runtime_image_ids_sha256", None)
    for pod in result["pods"].values():
        pod["requested_images_sha256"] = flat_sha256
        pod["runtime_image_ids"] = pod["runtime_image_ids"]["containers"]
    return result



def _require_exact_application_pod_inventory(
    pod_items: Any,
    expected_pod_names: set[str],
) -> list[str]:
    if (
        not isinstance(expected_pod_names, set)
        or not expected_pod_names
        or any(
            not isinstance(name, str) or not name
            for name in expected_pod_names
        )
    ):
        raise RuntimeErrorEB(
            "live application Pod inventory expected set is invalid"
        )
    if not isinstance(pod_items, list) or any(
        not isinstance(item, dict) for item in pod_items
    ):
        raise RuntimeErrorEB("live application Pod inventory is invalid")

    observed_pod_names: set[str] = set()
    for pod in pod_items:
        metadata = pod.get("metadata", {})
        pod_name = (
            metadata.get("name")
            if isinstance(metadata, dict)
            else None
        )
        if (
            not isinstance(metadata, dict)
            or not isinstance(pod_name, str)
            or not pod_name
            or metadata.get("namespace") != APP_NAMESPACE
            or metadata.get("deletionTimestamp") is not None
            or pod_name in observed_pod_names
        ):
            raise RuntimeErrorEB(
                "live application Pod inventory contains invalid or duplicate Pods"
            )
        observed_pod_names.add(pod_name)

    if observed_pod_names != expected_pod_names:
        missing = sorted(expected_pod_names - observed_pod_names)
        unexpected = sorted(observed_pod_names - expected_pod_names)
        raise RuntimeErrorEB(
            "live application Pod inventory contains noncanonical Pods: "
            f"missing={missing}; unexpected={unexpected}"
        )
    return sorted(observed_pod_names)

def _expected_live_secret_values(
    root: Path, source_commit: str
) -> dict[str, Any]:
    receipt_path = root / "receipts/secrets.json"
    database_path = root / "secrets/database.json"
    registry_path = root / "secrets/registry.json"
    if not database_path.is_file() or database_path.is_symlink():
        raise RuntimeErrorEB(
            "Experiment-B status requires private database Secret source material"
        )
    if not registry_path.is_file() or registry_path.is_symlink():
        raise RuntimeErrorEB(
            "Experiment-B status requires private registry Secret source material"
        )
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        database_bytes = database_path.read_bytes()
        registry_bytes = registry_path.read_bytes()
        database = json.loads(database_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeErrorEB(
            "Experiment-B status requires valid private Secret source material"
        ) from exc

    if not isinstance(receipt, dict) or (
        receipt.get("schema_version") != 1
        or receipt.get("status") != "ready"
        or receipt.get("source_commit") != source_commit
        or receipt.get("database_secret") != "commonthing-experiment-b-database"
        or receipt.get("runtime_secret") != "weltgewebe-runtime"
        or receipt.get("registry_secret") != "commonthing-experiment-b-registry"
        or receipt.get("secret_values_recorded") is not False
    ):
        raise RuntimeErrorEB("Experiment-B Secret receipt binding drifted")

    database_source_sha256 = receipt.get("database_source_sha256")
    if (
        not isinstance(database_source_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", database_source_sha256) is None
    ):
        raise RuntimeErrorEB("Experiment-B database Secret source digest is invalid")
    if not secrets.compare_digest(
        hashlib.sha256(database_bytes).hexdigest(),
        database_source_sha256,
    ):
        raise RuntimeErrorEB("Experiment-B database Secret source digest drifted")

    registry_source_sha256 = receipt.get("registry_source_sha256")
    if (
        not isinstance(registry_source_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", registry_source_sha256) is None
    ):
        raise RuntimeErrorEB("Experiment-B registry Secret source digest is invalid")
    if not secrets.compare_digest(
        hashlib.sha256(registry_bytes).hexdigest(),
        registry_source_sha256,
    ):
        raise RuntimeErrorEB("Experiment-B registry Secret source digest drifted")

    expected_database_keys = {"username", "database", "password"}
    if (
        not isinstance(database, dict)
        or set(database) != expected_database_keys
        or any(
            not isinstance(database.get(key), str) or not database[key]
            for key in expected_database_keys
        )
    ):
        raise RuntimeErrorEB("Experiment-B database Secret source material is invalid")

    database_url = _database_url(database)
    return {
        "database": {
            key: database[key].encode("utf-8")
            for key in expected_database_keys
        },
        "runtime": {"database-url": database_url.encode("utf-8")},
        "registry": {".dockerconfigjson": registry_bytes},
    }


def _require_live_secret(
    secret: Any,
    namespace: str,
    name: str,
    secret_type: str,
    required_keys: set[str],
    expected_values: dict[str, bytes] | None,
) -> None:
    if expected_values is not None and (
        set(expected_values) != required_keys
        or any(
            not isinstance(value, bytes) or not value
            for value in expected_values.values()
        )
    ):
        raise RuntimeErrorEB(
            f"Experiment-B Secret expected-content contract is invalid: {namespace}/{name}"
        )
    if not isinstance(secret, dict):
        raise RuntimeErrorEB(f"Experiment-B Secret is not an object: {namespace}/{name}")
    metadata = secret.get("metadata", {})
    if not isinstance(metadata, dict):
        raise RuntimeErrorEB(f"Experiment-B Secret metadata is invalid: {namespace}/{name}")
    if (
        metadata.get("name") != name
        or metadata.get("namespace") != namespace
        or metadata.get("deletionTimestamp")
    ):
        raise RuntimeErrorEB(
            f"Experiment-B Secret identity/deletion state is invalid: {namespace}/{name}"
        )
    if secret.get("type") != secret_type:
        raise RuntimeErrorEB(f"Experiment-B Secret type drifted: {namespace}/{name}")
    data = secret.get("data", {})
    if not isinstance(data, dict):
        raise RuntimeErrorEB(f"Experiment-B Secret data shape is invalid: {namespace}/{name}")
    present_keys = {
        str(key)
        for key, value in data.items()
        if isinstance(key, str) and isinstance(value, str) and bool(value)
    }
    missing = sorted(required_keys - present_keys)
    if missing:
        raise RuntimeErrorEB(
            f"Experiment-B Secret is missing required keys: {namespace}/{name}: {missing}"
        )
    for key in sorted(required_keys):
        try:
            decoded = base64.b64decode(data[key], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise RuntimeErrorEB(
                f"Experiment-B Secret contains invalid encoded data: {namespace}/{name}/{key}"
            ) from exc
        if (
            expected_values is not None
            and not secrets.compare_digest(decoded, expected_values[key])
        ):
            raise RuntimeErrorEB(
                f"Experiment-B Secret content drifted: {namespace}/{name}/{key}"
            )
    return None


def _verified_secret_readback(
    namespace: str,
    name: str,
    secret_type: str,
    required_keys: set[str],
    *,
    content_verified: bool = True,
) -> dict[str, Any]:
    return {
        "namespace": namespace,
        "name": name,
        "type": secret_type,
        "required_keys": sorted(required_keys),
        "present": True,
        "terminating": False,
        "content_verified": content_verified,
    }


def _require_gateway_ready(gateway: Any) -> dict[str, Any]:
    if not isinstance(gateway, dict):
        raise RuntimeErrorEB("Experiment-B Gateway payload is not an object")
    metadata = gateway.get("metadata", {})
    spec = gateway.get("spec", {})
    if not isinstance(metadata, dict) or not isinstance(spec, dict):
        raise RuntimeErrorEB("Experiment-B Gateway metadata/spec is invalid")
    if (
        metadata.get("name") != "commonthing-experiment-b"
        or metadata.get("namespace") != APP_NAMESPACE
        or metadata.get("deletionTimestamp") is not None
        or spec.get("gatewayClassName") != "cilium"
    ):
        raise RuntimeErrorEB("Experiment-B Gateway identity/class drifted")

    listeners = spec.get("listeners")
    if (
        not isinstance(listeners, list)
        or len(listeners) != 1
        or not isinstance(listeners[0], dict)
    ):
        raise RuntimeErrorEB("Experiment-B Gateway listener set drifted")
    listener = listeners[0]
    allowed = listener.get("allowedRoutes", {})
    namespaces = allowed.get("namespaces", {}) if isinstance(allowed, dict) else {}
    kinds = allowed.get("kinds", []) if isinstance(allowed, dict) else []
    expected_kind = {
        "group": "gateway.networking.k8s.io",
        "kind": "HTTPRoute",
    }
    if (
        listener.get("name") != "http"
        or listener.get("protocol") != "HTTP"
        or listener.get("port") != 80
        or not isinstance(namespaces, dict)
        or namespaces.get("from") != "Same"
        or not isinstance(kinds, list)
        or kinds != [expected_kind]
    ):
        raise RuntimeErrorEB("Experiment-B Gateway listener contract drifted")

    generation = _require_current_condition(
        gateway, "Programmed", "Experiment-B Gateway"
    )
    return {
        "generation": generation,
        "gateway_class": "cilium",
        "listener": "http",
        "programmed": True,
    }


def _require_httproute_ready(route: Any) -> dict[str, Any]:
    if not isinstance(route, dict):
        raise RuntimeErrorEB("Experiment-B HTTPRoute payload is not an object")

    metadata = route.get("metadata", {})
    spec = route.get("spec", {})
    if not isinstance(metadata, dict) or not isinstance(spec, dict):
        raise RuntimeErrorEB("Experiment-B HTTPRoute metadata/spec is invalid")
    if (
        metadata.get("name") != "commonthing-experiment-b"
        or metadata.get("namespace") != APP_NAMESPACE
        or metadata.get("deletionTimestamp") is not None
    ):
        raise RuntimeErrorEB("Experiment-B HTTPRoute identity is invalid")
    generation = int(metadata.get("generation") or 0)
    if generation < 1:
        raise RuntimeErrorEB("Experiment-B HTTPRoute generation is invalid")

    expected_parent = {
        "name": "commonthing-experiment-b",
        "namespace": APP_NAMESPACE,
        "sectionName": "http",
    }
    parent_refs = spec.get("parentRefs")
    if (
        not isinstance(parent_refs, list)
        or len(parent_refs) != 1
        or not isinstance(parent_refs[0], dict)
    ):
        raise RuntimeErrorEB("Experiment-B HTTPRoute parentRef drifted")
    spec_parent = parent_refs[0]
    normalized_spec_parent = {
        "name": str(spec_parent.get("name", "")),
        "namespace": str(spec_parent.get("namespace") or APP_NAMESPACE),
        "sectionName": str(spec_parent.get("sectionName", "")),
    }
    if (
        normalized_spec_parent != expected_parent
        or str(spec_parent.get("group") or "gateway.networking.k8s.io")
        != "gateway.networking.k8s.io"
        or str(spec_parent.get("kind") or "Gateway") != "Gateway"
    ):
        raise RuntimeErrorEB("Experiment-B HTTPRoute parentRef drifted")

    expected_rules = [
        {
            "paths": ["/health", "/api"],
            "backend": {"name": "weltgewebe-api", "port": 8080},
        },
        {
            "paths": ["/"],
            "backend": {"name": "weltgewebe-web", "port": 8080},
        },
    ]
    rules = spec.get("rules")
    if not isinstance(rules, list) or len(rules) != len(expected_rules):
        raise RuntimeErrorEB("Experiment-B HTTPRoute rule set drifted")
    normalized_rules: list[dict[str, Any]] = []
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) - {"matches", "backendRefs"}:
            raise RuntimeErrorEB("Experiment-B HTTPRoute rule shape drifted")
        matches = rule.get("matches")
        backend_refs = rule.get("backendRefs")
        if not isinstance(matches, list) or not matches:
            raise RuntimeErrorEB("Experiment-B HTTPRoute matches drifted")
        if (
            not isinstance(backend_refs, list)
            or len(backend_refs) != 1
            or not isinstance(backend_refs[0], dict)
        ):
            raise RuntimeErrorEB("Experiment-B HTTPRoute backend set drifted")

        paths: list[str] = []
        for match in matches:
            if not isinstance(match, dict) or set(match) != {"path"}:
                raise RuntimeErrorEB("Experiment-B HTTPRoute match shape drifted")
            path = match.get("path")
            if (
                not isinstance(path, dict)
                or set(path) != {"type", "value"}
                or path.get("type") != "PathPrefix"
                or not isinstance(path.get("value"), str)
            ):
                raise RuntimeErrorEB("Experiment-B HTTPRoute path match drifted")
            paths.append(path["value"])

        backend = backend_refs[0]
        if set(backend) - {"group", "kind", "name", "namespace", "port", "weight"}:
            raise RuntimeErrorEB("Experiment-B HTTPRoute backend shape drifted")
        weight = backend.get("weight", 1)
        if (
            str(backend.get("group") or "") != ""
            or str(backend.get("kind") or "Service") != "Service"
            or str(backend.get("namespace") or APP_NAMESPACE) != APP_NAMESPACE
            or not isinstance(weight, int)
            or isinstance(weight, bool)
            or weight != 1
            or not isinstance(backend.get("name"), str)
            or not isinstance(backend.get("port"), int)
            or isinstance(backend.get("port"), bool)
        ):
            raise RuntimeErrorEB("Experiment-B HTTPRoute backend contract drifted")
        normalized_rules.append(
            {
                "paths": paths,
                "backend": {
                    "name": backend["name"],
                    "port": backend["port"],
                },
            }
        )
    if normalized_rules != expected_rules:
        raise RuntimeErrorEB("Experiment-B HTTPRoute routing contract drifted")

    status_obj = route.get("status", {})
    parents = status_obj.get("parents", []) if isinstance(status_obj, dict) else []
    if not isinstance(parents, list):
        raise RuntimeErrorEB("Experiment-B HTTPRoute parent status is invalid")

    matching: list[dict[str, Any]] = []
    for parent in parents:
        if not isinstance(parent, dict):
            raise RuntimeErrorEB("Experiment-B HTTPRoute parent status contains a non-object")
        parent_ref = parent.get("parentRef", {})
        if not isinstance(parent_ref, dict):
            raise RuntimeErrorEB("Experiment-B HTTPRoute status parentRef is invalid")
        normalized_parent = {
            "name": str(parent_ref.get("name", "")),
            "namespace": str(parent_ref.get("namespace") or APP_NAMESPACE),
            "sectionName": str(parent_ref.get("sectionName", "")),
        }
        if (
            normalized_parent != expected_parent
            or parent.get("controllerName") != "io.cilium/gateway-controller"
        ):
            continue
        conditions = parent.get("conditions", [])
        if not isinstance(conditions, list):
            raise RuntimeErrorEB("Experiment-B HTTPRoute conditions are invalid")

        observed: dict[str, dict[str, Any]] = {}
        for condition in conditions:
            if not isinstance(condition, dict):
                continue
            condition_type = str(condition.get("type", ""))
            if condition_type not in {"Accepted", "ResolvedRefs"}:
                continue
            if condition.get("status") != "True":
                continue
            try:
                observed_generation = int(condition.get("observedGeneration"))
            except (TypeError, ValueError):
                continue
            if observed_generation != generation:
                continue
            observed[condition_type] = {
                "status": "True",
                "observed_generation": observed_generation,
            }
        if set(observed) == {"Accepted", "ResolvedRefs"}:
            matching.append(
                {
                    "controller_name": str(parent.get("controllerName", "")),
                    "parent_ref": normalized_parent,
                    "conditions": observed,
                }
            )

    if len(matching) != 1:
        raise RuntimeErrorEB(
            "Experiment-B HTTPRoute is not currently Accepted with ResolvedRefs"
        )
    return {
        "generation": generation,
        "accepted": True,
        "resolved_refs": True,
        **matching[0],
    }


def _final_recovery_state_readback(
    root: Path, source_commit: str
) -> dict[str, Any]:
    if not COMMIT_RE.fullmatch(source_commit):
        raise RuntimeErrorEB("final recovery-state source commit is not exact")
    recovery_failed_path = root / "receipts/recovery-failed.json"
    if recovery_failed_path.is_file():
        raise RuntimeErrorEB(
            "Experiment-B status is blocked by the latest failed recovery attempt"
        )
    recovery_path = root / "receipts/recovery.json"
    if not recovery_path.is_file():
        raise RuntimeErrorEB("Experiment-B status requires recovery receipt")
    try:
        recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeErrorEB("Experiment-B recovery receipt is not valid JSON") from exc
    if (
        not isinstance(recovery, dict)
        or recovery.get("status") != "pass"
        or recovery.get("source_commit") != source_commit
        or recovery.get("rpo_seconds") != 0
        or recovery.get("database_before") != recovery.get("database_after")
        or recovery.get("jetstream_before") != recovery.get("jetstream_after")
        or not isinstance(recovery.get("database_post_resume"), dict)
        or recovery.get("jetstream_post_resume") != recovery.get("jetstream_after")
    ):
        raise RuntimeErrorEB("Experiment-B recovery receipt binding is invalid")
    try:
        _require_database_runtime_continuity(
            recovery["database_after"],
            recovery["database_post_resume"],
            "recovery receipt post-resume database",
        )
    except RuntimeErrorEB as exc:
        raise RuntimeErrorEB("Experiment-B recovery receipt binding is invalid") from exc

    database_identity = _verified_database_client_identity(
        root,
        source_commit,
    )
    postgres_binding = _require_postgres_runtime_binding(
        root,
        source_commit,
    )
    current_database = _database_signature(
        root,
        database_identity=database_identity,
        source_commit=source_commit,
        postgres_binding=postgres_binding,
    )
    nats_binding = _require_nats_runtime_binding(
        root,
        source_commit,
    )
    current_jetstream = _jetstream_signature(
        root,
        source_commit=source_commit,
        nats_binding=nats_binding,
    )
    try:
        _require_database_runtime_continuity(
            recovery["database_post_resume"],
            current_database,
            "final recovery-state database",
        )
    except RuntimeErrorEB as exc:
        raise RuntimeErrorEB(
            "Experiment-B database/search state drifted after recovery"
        ) from exc
    if current_jetstream != recovery.get("jetstream_post_resume"):
        raise RuntimeErrorEB("Experiment-B JetStream state drifted after recovery")

    fixture_path = root / "receipts/t048-fixture.json"
    fixture = _validated_t048_fixture_receipt(root, source_commit)
    return {
        "recovery_receipt_sha256": sha256_file(recovery_path),
        "fixture_receipt_sha256": sha256_file(fixture_path),
        "rpo_seconds": 0,
        "database_signature": current_database,
        "jetstream_signature": current_jetstream,
        "fixture_live_binding": fixture.get("live_binding"),
    }


def _pod_spec_images(pod_spec: Any, context: str) -> dict[str, dict[str, str]]:
    if not isinstance(pod_spec, dict):
        raise RuntimeErrorEB(f"{context} pod spec is invalid")
    ephemeral_containers = pod_spec.get("ephemeralContainers", [])
    if not isinstance(ephemeral_containers, list):
        raise RuntimeErrorEB(
            f"{context} ephemeral container inventory is invalid"
        )
    if ephemeral_containers:
        raise RuntimeErrorEB(f"{context} ephemeral containers are forbidden")
    result: dict[str, dict[str, str]] = {}
    for field, output_key in (
        ("containers", "containers"),
        ("initContainers", "init_containers"),
    ):
        items = pod_spec.get(field, [])
        if not isinstance(items, list):
            raise RuntimeErrorEB(f"{context} {field} inventory is invalid")
        images: dict[str, str] = {}
        for item in items:
            if not isinstance(item, dict):
                raise RuntimeErrorEB(f"{context} {field} inventory is invalid")
            name = item.get("name")
            image = item.get("image")
            if (
                not isinstance(name, str)
                or not name
                or not isinstance(image, str)
                or not image
                or name in images
            ):
                raise RuntimeErrorEB(f"{context} {field} image binding is invalid")
            images[name] = image
        result[output_key] = images
    if not result["containers"]:
        raise RuntimeErrorEB(f"{context} has no containers")
    return result



def _namespace_security_contract_from_bytes(
    namespace_bytes: bytes,
    kustomization_bytes: bytes,
) -> dict[str, Any]:
    try:
        documents = [
            item
            for item in yaml.safe_load_all(namespace_bytes.decode("utf-8"))
            if isinstance(item, dict)
        ]
        kustomization = yaml.safe_load(
            kustomization_bytes.decode("utf-8")
        )
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise RuntimeErrorEB(
            "versioned Experiment-B Namespace contract is invalid"
        ) from exc

    expected_names = {APP_NAMESPACE, DATA_NAMESPACE}
    namespaces = [
        document
        for document in documents
        if document.get("kind") == "Namespace"
        and document.get("metadata", {}).get("name") in expected_names
    ]
    if (
        len(namespaces) != len(expected_names)
        or {
            str(item.get("metadata", {}).get("name", ""))
            for item in namespaces
        }
        != expected_names
        or not isinstance(kustomization, dict)
    ):
        raise RuntimeErrorEB(
            "versioned Experiment-B Namespace set is incomplete or duplicated"
        )

    common_labels: dict[str, str] = {}
    label_blocks = kustomization.get("labels", [])
    if not isinstance(label_blocks, list):
        raise RuntimeErrorEB(
            "Experiment-B Namespace Kustomize labels are invalid"
        )
    for block in label_blocks:
        pairs = block.get("pairs") if isinstance(block, dict) else None
        if (
            not isinstance(pairs, dict)
            or any(
                not isinstance(key, str)
                or not key
                or not isinstance(value, str)
                or not value
                for key, value in pairs.items()
            )
        ):
            raise RuntimeErrorEB(
                "Experiment-B Namespace Kustomize label block is invalid"
            )
        for key, value in pairs.items():
            if key in common_labels and common_labels[key] != value:
                raise RuntimeErrorEB(
                    "Experiment-B Namespace Kustomize labels conflict"
                )
            common_labels[str(key)] = str(value)

    result: dict[str, Any] = {}
    for namespace in namespaces:
        metadata = namespace.get("metadata", {})
        name = str(metadata.get("name", ""))
        labels = metadata.get("labels", {})
        if (
            not isinstance(labels, dict)
            or any(
                not isinstance(key, str)
                or not key
                or not isinstance(value, str)
                or not value
                for key, value in labels.items()
            )
        ):
            raise RuntimeErrorEB(
                f"versioned Namespace labels are invalid: {name}"
            )
        expected_labels = {
            **{str(key): str(value) for key, value in labels.items()},
            **common_labels,
            "kubernetes.io/metadata.name": name,
        }
        for required in (
            "pod-security.kubernetes.io/enforce",
            "pod-security.kubernetes.io/audit",
            "pod-security.kubernetes.io/warn",
        ):
            if expected_labels.get(required) != "restricted":
                raise RuntimeErrorEB(
                    f"versioned Namespace loses restricted Pod Security: {name}"
                )
        result[name] = {
            "labels": expected_labels,
            "labels_sha256": _stable_json_sha256(expected_labels),
        }
    return result


def _versioned_namespace_security_contract(
    source_commit: str | None = None,
) -> dict[str, Any]:
    if source_commit is None:
        try:
            namespace_bytes = (NAMESPACES / "namespaces.yaml").read_bytes()
            kustomization_bytes = (NAMESPACES / "kustomization.yaml").read_bytes()
        except OSError as exc:
            raise RuntimeErrorEB(
                "versioned Experiment-B Namespace contract is invalid"
            ) from exc
    else:
        namespace_bytes = _git_blob_bytes(
            source_commit, NAMESPACES / "namespaces.yaml"
        )
        kustomization_bytes = _git_blob_bytes(
            source_commit, NAMESPACES / "kustomization.yaml"
        )
    return _namespace_security_contract_from_bytes(
        namespace_bytes,
        kustomization_bytes,
    )

def _require_live_namespace_security_contract(
    root: Path,
    source_commit: str | None = None,
) -> dict[str, Any]:
    expected = _versioned_namespace_security_contract(source_commit)
    result: dict[str, Any] = {}
    for name, contract_value in expected.items():
        namespace = _kubectl_json(root, ["get", "namespace", name])
        metadata = (
            namespace.get("metadata", {})
            if isinstance(namespace, dict)
            else {}
        )
        labels = metadata.get("labels", {}) if isinstance(metadata, dict) else {}
        # Flux adds exactly these ownership labels to the reconciled Namespace.
        # All versioned security labels must still match without any extra keys.
        flux_owner_labels = {
            "kustomize.toolkit.fluxcd.io/name": "commonthing-experiment-b-namespaces",
            "kustomize.toolkit.fluxcd.io/namespace": "flux-system",
        }
        if not contract_value["labels"].keys().isdisjoint(flux_owner_labels):
            raise RuntimeErrorEB(f"versioned Namespace labels overlap Flux ownership: {name}")
        expected_live_labels = {**contract_value["labels"], **flux_owner_labels}
        if (
            not isinstance(metadata, dict)
            or metadata.get("name") != name
            or metadata.get("deletionTimestamp") is not None
            or not isinstance(labels, dict)
            or labels != expected_live_labels
        ):
            raise RuntimeErrorEB(
                f"live Namespace security labels drifted: {name}"
            )
        result[name] = {
            # The portable security projection excludes verified Flux ownership metadata.
            "labels": dict(contract_value["labels"]),
            "labels_sha256": contract_value["labels_sha256"],
            "canonical": True,
        }
    return result


def _service_spec_projection(service: Any, context: str) -> dict[str, Any]:
    if not isinstance(service, dict):
        raise RuntimeErrorEB(f"{context} Service payload is invalid")
    spec = service.get("spec", {})
    if not isinstance(spec, dict):
        raise RuntimeErrorEB(f"{context} Service spec is invalid")
    selector = spec.get("selector")
    ports = spec.get("ports")
    service_type = spec.get("type", "ClusterIP")
    external_ips = spec.get("externalIPs") or []
    load_balancer_source_ranges = spec.get("loadBalancerSourceRanges") or []
    external_traffic_policy = spec.get("externalTrafficPolicy")
    internal_traffic_policy = spec.get("internalTrafficPolicy", "Cluster")
    session_affinity = spec.get("sessionAffinity", "None")
    session_affinity_config = spec.get("sessionAffinityConfig")
    publish_not_ready = spec.get("publishNotReadyAddresses", False)
    allocate_lb_node_ports = spec.get("allocateLoadBalancerNodePorts")
    load_balancer_class = spec.get("loadBalancerClass")
    load_balancer_ip = spec.get("loadBalancerIP")
    external_name = spec.get("externalName")
    traffic_distribution = spec.get("trafficDistribution")
    health_check_node_port = spec.get("healthCheckNodePort")
    if (
        not isinstance(selector, dict)
        or not selector
        or any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, str)
            or not value
            for key, value in selector.items()
        )
        or not isinstance(ports, list)
        or not ports
        or service_type not in {"ClusterIP", "NodePort", "LoadBalancer"}
        or not isinstance(external_ips, list)
        or any(not isinstance(value, str) or not value for value in external_ips)
        or len(set(external_ips)) != len(external_ips)
        or not isinstance(load_balancer_source_ranges, list)
        or any(
            not isinstance(value, str) or not value
            for value in load_balancer_source_ranges
        )
        or len(set(load_balancer_source_ranges))
        != len(load_balancer_source_ranges)
        or external_traffic_policy not in {None, "Cluster", "Local"}
        or internal_traffic_policy not in {"Cluster", "Local"}
        or session_affinity not in {"None", "ClientIP"}
        or (
            session_affinity_config is not None
            and not isinstance(session_affinity_config, dict)
        )
        or not isinstance(publish_not_ready, bool)
        or (
            allocate_lb_node_ports is not None
            and not isinstance(allocate_lb_node_ports, bool)
        )
        or (
            load_balancer_class is not None
            and (
                not isinstance(load_balancer_class, str)
                or not load_balancer_class
            )
        )
        or (
            load_balancer_ip is not None
            and (not isinstance(load_balancer_ip, str) or not load_balancer_ip)
        )
        or (
            external_name is not None
            and (not isinstance(external_name, str) or not external_name)
        )
        or (
            traffic_distribution is not None
            and (
                not isinstance(traffic_distribution, str)
                or not traffic_distribution
            )
        )
        or (
            health_check_node_port is not None
            and (
                isinstance(health_check_node_port, bool)
                or not isinstance(health_check_node_port, int)
                or health_check_node_port < 1
                or health_check_node_port > 65535
            )
        )
    ):
        raise RuntimeErrorEB(
            f"{context} Service selector/exposure contract is invalid"
        )

    normalized_ports: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for item in ports:
        if not isinstance(item, dict):
            raise RuntimeErrorEB(f"{context} Service port inventory is invalid")
        name = item.get("name")
        port = item.get("port")
        target_port = item.get("targetPort")
        protocol = item.get("protocol", "TCP")
        app_protocol = item.get("appProtocol")
        node_port = item.get("nodePort")
        if (
            not isinstance(name, str)
            or not name
            or name in seen_names
            or isinstance(port, bool)
            or not isinstance(port, int)
            or port < 1
            or port > 65535
            or (
                isinstance(target_port, bool)
                or not isinstance(target_port, (int, str))
                or isinstance(target_port, str)
                and not target_port
            )
            or protocol not in {"TCP", "UDP", "SCTP"}
            or (
                app_protocol is not None
                and (not isinstance(app_protocol, str) or not app_protocol)
            )
            or (
                node_port is not None
                and (
                    isinstance(node_port, bool)
                    or not isinstance(node_port, int)
                    or node_port < 1
                    or node_port > 65535
                )
            )
        ):
            raise RuntimeErrorEB(f"{context} Service port contract is invalid")
        seen_names.add(name)
        normalized_ports.append(
            {
                "name": name,
                "port": port,
                "targetPort": target_port,
                "protocol": protocol,
                "appProtocol": app_protocol,
                "nodePort": node_port,
            }
        )

    return {
        "type": service_type,
        "headless": spec.get("clusterIP") == "None",
        "selector": {
            str(key): str(value)
            for key, value in sorted(selector.items())
        },
        "ports": sorted(normalized_ports, key=lambda value: value["name"]),
        "externalIPs": sorted(external_ips),
        "externalTrafficPolicy": external_traffic_policy,
        "internalTrafficPolicy": internal_traffic_policy,
        "publishNotReadyAddresses": publish_not_ready,
        "sessionAffinity": session_affinity,
        "sessionAffinityConfig": json.loads(
            json.dumps(session_affinity_config)
        )
        if session_affinity_config is not None
        else None,
        "loadBalancerSourceRanges": sorted(load_balancer_source_ranges),
        "loadBalancerClass": load_balancer_class,
        "loadBalancerIP": load_balancer_ip,
        "allocateLoadBalancerNodePorts": allocate_lb_node_ports,
        "healthCheckNodePort": health_check_node_port,
        "externalName": external_name,
        "trafficDistribution": traffic_distribution,
    }



def _data_service_contract_from_bytes(
    manifest_bytes: bytes,
    name: str,
    context: str,
) -> dict[str, Any]:
    try:
        documents = list(
            yaml.safe_load_all(manifest_bytes.decode("utf-8"))
        )
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise RuntimeErrorEB(
            f"{context} manifest is invalid: {name}"
        ) from exc
    matches = [
        document
        for document in documents
        if isinstance(document, dict)
        and document.get("kind") == "Service"
        and document.get("metadata", {}).get("name") == name
        and document.get("metadata", {}).get("namespace") == DATA_NAMESPACE
    ]
    if len(matches) != 1:
        raise RuntimeErrorEB(
            f"{context} does not contain exactly one Service: {name}"
        )
    projection = _service_spec_projection(
        matches[0], f"{context} {name}"
    )
    return {
        "spec": projection,
        "spec_sha256": _stable_json_sha256(projection),
    }


def _versioned_data_service_contract(
    path: Path,
    name: str,
) -> dict[str, Any]:
    try:
        manifest_bytes = path.read_bytes()
    except OSError as exc:
        raise RuntimeErrorEB(
            f"versioned data Service manifest is invalid: {name}"
        ) from exc
    return _data_service_contract_from_bytes(
        manifest_bytes,
        name,
        "versioned data Service",
    )


def _source_commit_data_service_contract(
    source_commit: str,
    path: Path,
    name: str,
) -> dict[str, Any]:
    return _data_service_contract_from_bytes(
        _git_blob_bytes(source_commit, path),
        name,
        "source-commit data Service",
    )

def _require_live_data_services(
    root: Path,
    source_commit: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in ("postgres", "nats"):
        path = CLUSTER / f"data/{name}.yaml"
        expected = (
            _versioned_data_service_contract(path, name)
            if source_commit is None
            else _source_commit_data_service_contract(
                source_commit,
                path,
                name,
            )
        )
        service = _kubectl_json(
            root, ["-n", DATA_NAMESPACE, "get", "service", name]
        )
        metadata = (
            service.get("metadata", {}) if isinstance(service, dict) else {}
        )
        if (
            not isinstance(metadata, dict)
            or metadata.get("name") != name
            or metadata.get("namespace") != DATA_NAMESPACE
            or metadata.get("deletionTimestamp") is not None
        ):
            raise RuntimeErrorEB(f"live data Service identity drifted: {name}")
        observed = _service_spec_projection(
            service, f"live data Service {name}"
        )
        if observed != expected["spec"]:
            raise RuntimeErrorEB(
                f"live data Service spec drifted from versioned manifest: {name}"
            )
        result[name] = {
            "spec": observed,
            "spec_sha256": expected["spec_sha256"],
            "canonical": True,
        }
    return result



def _source_commit_application_render(
    root: Path,
    release: dict[str, Any],
) -> str:
    source_commit = (
        release.get("source_commit")
        if isinstance(release, dict)
        else None
    )
    if not isinstance(source_commit, str) or COMMIT_RE.fullmatch(source_commit) is None:
        raise RuntimeErrorEB(
            "application contract requires an exact source commit"
        )
    return _source_commit_kustomize_build(
        root,
        source_commit,
        APP_OVERLAY,
        ROOT / "platform/apps/weltgewebe",
    )


def _rendered_application_service_contract(
    root: Path,
    release: dict[str, Any],
) -> dict[str, Any]:
    api_digest = release.get("api_digest") if isinstance(release, dict) else None
    web_digest = release.get("web_digest") if isinstance(release, dict) else None
    if (
        not isinstance(api_digest, str)
        or not DIGEST_RE.fullmatch(api_digest)
        or not isinstance(web_digest, str)
        or not DIGEST_RE.fullmatch(web_digest)
    ):
        raise RuntimeErrorEB(
            "application Service contract requires exact release digests"
        )
    rendered = _source_commit_application_render(root, release)
    rendered = rendered.replace("${API_DIGEST}", api_digest).replace(
        "${WEB_DIGEST}", web_digest
    )
    try:
        documents = [
            document
            for document in yaml.safe_load_all(rendered)
            if isinstance(document, dict)
        ]
    except yaml.YAMLError as exc:
        raise RuntimeErrorEB(
            "rendered Experiment-B application Service contract is invalid"
        ) from exc

    result: dict[str, Any] = {}
    for name in ("weltgewebe-api", "weltgewebe-web"):
        matches = [
            document
            for document in documents
            if document.get("kind") == "Service"
            and document.get("metadata", {}).get("name") == name
            and document.get("metadata", {}).get("namespace") == APP_NAMESPACE
        ]
        if len(matches) != 1:
            raise RuntimeErrorEB(
                f"rendered application Service is ambiguous: {name}"
            )
        projection = _service_spec_projection(
            matches[0], f"rendered application Service {name}"
        )
        result[name] = {
            "spec": projection,
            "spec_sha256": _stable_json_sha256(projection),
        }
    return result


def _require_live_application_services(
    root: Path,
    release: dict[str, Any],
) -> dict[str, Any]:
    expected = _rendered_application_service_contract(root, release)
    result: dict[str, Any] = {}
    for name, expected_value in expected.items():
        service = _kubectl_json(
            root, ["-n", APP_NAMESPACE, "get", "service", name]
        )
        metadata = service.get("metadata", {}) if isinstance(service, dict) else {}
        if (
            not isinstance(metadata, dict)
            or metadata.get("name") != name
            or metadata.get("namespace") != APP_NAMESPACE
            or metadata.get("deletionTimestamp") is not None
        ):
            raise RuntimeErrorEB(
                f"live application Service identity drifted: {name}"
            )
        observed = _service_spec_projection(
            service, f"live application Service {name}"
        )
        if observed != expected_value["spec"]:
            raise RuntimeErrorEB(
                f"live application Service spec drifted: {name}"
            )
        result[name] = {
            "spec": observed,
            "spec_sha256": expected_value["spec_sha256"],
            "canonical": True,
        }
    return result


def _probe_runtime_contract(value: Any, context: str) -> Any:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RuntimeErrorEB(f"{context} probe contract is invalid")
    normalized = json.loads(json.dumps(value))
    defaults = {
        "initialDelaySeconds": 0,
        "timeoutSeconds": 1,
        "periodSeconds": 10,
        "successThreshold": 1,
        "failureThreshold": 3,
    }
    for key, default in defaults.items():
        if normalized.get(key) == default:
            normalized.pop(key, None)
    http_get = normalized.get("httpGet")
    if isinstance(http_get, dict) and http_get.get("scheme") == "HTTP":
        http_get.pop("scheme", None)
    grpc = normalized.get("grpc")
    if isinstance(grpc, dict) and grpc.get("service") == "":
        grpc.pop("service", None)
    return normalized


def _container_runtime_contract(
    container: Any,
    context: str,
) -> dict[str, Any]:
    if not isinstance(container, dict):
        raise RuntimeErrorEB(f"{context} container contract is invalid")
    name = container.get("name")
    image = container.get("image")
    if (
        not isinstance(name, str)
        or not name
        or not isinstance(image, str)
        or not image
    ):
        raise RuntimeErrorEB(f"{context} container identity is invalid")
    fields = (
        "image",
        "imagePullPolicy",
        "command",
        "args",
        "env",
        "envFrom",
        "lifecycle",
        "resources",
        "securityContext",
        "volumeMounts",
        "workingDir",
        "restartPolicy",
    )
    ports = container.get("ports", [])
    if ports is None:
        ports = []
    if not isinstance(ports, list) or any(
        not isinstance(port, dict) for port in ports
    ):
        raise RuntimeErrorEB(f"{context} container ports contract is invalid")
    normalized_ports = json.loads(json.dumps(ports))
    for port in normalized_ports:
        port.setdefault("protocol", "TCP")
        if port.get("hostPort") == 0:
            port.pop("hostPort", None)
    result = {
        "name": name,
        **{field: container.get(field) for field in fields},
        "ports": normalized_ports,
    }
    for probe_field in (
        "startupProbe",
        "readinessProbe",
        "livenessProbe",
    ):
        result[probe_field] = _probe_runtime_contract(
            container.get(probe_field),
            f"{context} {name} {probe_field}",
        )
    return result


def _pod_active_deadline_seconds(
    pod_spec: Any,
    context: str,
) -> int | None:
    if not isinstance(pod_spec, dict):
        raise RuntimeErrorEB(f"{context} Pod spec is invalid")
    active_deadline_seconds = pod_spec.get("activeDeadlineSeconds")
    if (
        active_deadline_seconds is not None
        and (
            isinstance(active_deadline_seconds, bool)
            or not isinstance(active_deadline_seconds, int)
            or active_deadline_seconds < 1
        )
    ):
        raise RuntimeErrorEB(
            f"{context} Pod activeDeadlineSeconds contract is invalid"
        )
    return active_deadline_seconds


def _pod_priority_projection(
    pod_spec: dict[str, Any],
    context: str,
    *,
    synthesize_system_priority: bool = False,
) -> tuple[str, int]:
    priority_class_name = pod_spec.get("priorityClassName")
    if priority_class_name is None:
        priority_class_name = ""
    if not isinstance(priority_class_name, str):
        raise RuntimeErrorEB(
            f"{context} Pod priorityClassName contract is invalid"
        )
    priority = pod_spec.get("priority")
    if priority is None:
        priority = (
            {
                "system-cluster-critical": 2_000_000_000,
                "system-node-critical": 2_000_001_000,
            }.get(priority_class_name, 0)
            if synthesize_system_priority
            else 0
        )
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise RuntimeErrorEB(f"{context} Pod priority contract is invalid")
    return priority_class_name, priority


def _application_pod_spec_projection(
    pod_spec: Any,
    context: str,
    *,
    synthesize_system_priority: bool = False,
) -> dict[str, Any]:
    if not isinstance(pod_spec, dict):
        raise RuntimeErrorEB(f"{context} Pod spec is invalid")

    tolerations = pod_spec.get("tolerations") or []
    if not isinstance(tolerations, list) or any(
        not isinstance(item, dict) for item in tolerations
    ):
        raise RuntimeErrorEB(f"{context} toleration contract is invalid")
    normalized_tolerations = json.loads(json.dumps(tolerations))
    for default_toleration in (
        {
            "effect": "NoExecute",
            "key": "node.kubernetes.io/not-ready",
            "operator": "Exists",
            "tolerationSeconds": 300,
        },
        {
            "effect": "NoExecute",
            "key": "node.kubernetes.io/unreachable",
            "operator": "Exists",
            "tolerationSeconds": 300,
        },
    ):
        if default_toleration in normalized_tolerations:
            normalized_tolerations.remove(default_toleration)

    host_aliases = pod_spec.get("hostAliases") or []
    readiness_gates = pod_spec.get("readinessGates") or []
    ephemeral_containers = pod_spec.get("ephemeralContainers") or []
    active_deadline_seconds = _pod_active_deadline_seconds(
        pod_spec, context
    )
    if (
        not isinstance(host_aliases, list)
        or any(not isinstance(item, dict) for item in host_aliases)
        or not isinstance(readiness_gates, list)
        or any(not isinstance(item, dict) for item in readiness_gates)
        or not isinstance(ephemeral_containers, list)
        or any(not isinstance(item, dict) for item in ephemeral_containers)
    ):
        raise RuntimeErrorEB(
            f"{context} Pod hostAliases/readinessGates/ephemeralContainers "
            "contract is invalid"
        )
    if ephemeral_containers:
        raise RuntimeErrorEB(
            f"{context} Pod ephemeral containers are forbidden"
        )

    priority_class_name, priority = _pod_priority_projection(
        pod_spec,
        context,
        synthesize_system_priority=synthesize_system_priority,
    )
    result: dict[str, Any] = {
        "serviceAccountName": pod_spec.get(
            "serviceAccountName", "default"
        ),
        "automountServiceAccountToken": pod_spec.get(
            "automountServiceAccountToken"
        ),
        "terminationGracePeriodSeconds": pod_spec.get(
            "terminationGracePeriodSeconds", 30
        ),
        "activeDeadlineSeconds": active_deadline_seconds,
        "securityContext": pod_spec.get("securityContext"),
        "imagePullSecrets": pod_spec.get("imagePullSecrets") or [],
        "volumes": pod_spec.get("volumes") or [],
        "topologySpreadConstraints": pod_spec.get(
            "topologySpreadConstraints"
        ),
        "affinity": pod_spec.get("affinity"),
        "nodeSelector": pod_spec.get("nodeSelector"),
        "hostNetwork": pod_spec.get("hostNetwork", False),
        "hostPID": pod_spec.get("hostPID", False),
        "hostIPC": pod_spec.get("hostIPC", False),
        "dnsPolicy": pod_spec.get("dnsPolicy", "ClusterFirst"),
        "dnsConfig": pod_spec.get("dnsConfig"),
        "priorityClassName": priority_class_name,
        "tolerations": normalized_tolerations,
        "restartPolicy": pod_spec.get("restartPolicy", "Always"),
        "schedulerName": pod_spec.get(
            "schedulerName", "default-scheduler"
        ),
        "enableServiceLinks": pod_spec.get("enableServiceLinks", True),
        "shareProcessNamespace": pod_spec.get(
            "shareProcessNamespace", False
        ),
        "runtimeClassName": pod_spec.get("runtimeClassName"),
        "hostname": pod_spec.get("hostname"),
        "subdomain": pod_spec.get("subdomain"),
        "setHostnameAsFQDN": pod_spec.get("setHostnameAsFQDN", False),
        "hostAliases": json.loads(json.dumps(host_aliases)),
        "readinessGates": json.loads(json.dumps(readiness_gates)),
        "preemptionPolicy": pod_spec.get(
            "preemptionPolicy", "PreemptLowerPriority"
        ),
        "priority": priority,
    }
    for field, output_key in (
        ("containers", "containers"),
        ("initContainers", "init_containers"),
    ):
        items = pod_spec.get(field, [])
        if not isinstance(items, list):
            raise RuntimeErrorEB(f"{context} {field} inventory is invalid")
        projected: dict[str, Any] = {}
        for item in items:
            value = _container_runtime_contract(item, context)
            name = value["name"]
            if name in projected:
                raise RuntimeErrorEB(
                    f"{context} contains duplicate container identity: {name}"
                )
            projected[name] = value
        if output_key == "containers" and not projected:
            raise RuntimeErrorEB(f"{context} contains no containers")
        result[output_key] = projected
    return result


def _rendered_application_workload_contract(
    root: Path,
    release: dict[str, Any],
) -> dict[str, Any]:
    api_digest = release.get("api_digest") if isinstance(release, dict) else None
    web_digest = release.get("web_digest") if isinstance(release, dict) else None
    if (
        not isinstance(api_digest, str)
        or not DIGEST_RE.fullmatch(api_digest)
        or not isinstance(web_digest, str)
        or not DIGEST_RE.fullmatch(web_digest)
    ):
        raise RuntimeErrorEB(
            "application workload contract requires exact release digests"
        )
    rendered = _source_commit_application_render(root, release)
    rendered = rendered.replace("${API_DIGEST}", api_digest).replace(
        "${WEB_DIGEST}", web_digest
    )
    try:
        documents = [
            document
            for document in yaml.safe_load_all(rendered)
            if isinstance(document, dict)
        ]
    except yaml.YAMLError as exc:
        raise RuntimeErrorEB(
            "rendered Experiment-B application contract is invalid"
        ) from exc

    result: dict[str, Any] = {}
    for name in ("weltgewebe-api", "weltgewebe-web"):
        matches = [
            document
            for document in documents
            if document.get("kind") == "Deployment"
            and document.get("metadata", {}).get("name") == name
            and document.get("metadata", {}).get("namespace") == APP_NAMESPACE
        ]
        if len(matches) != 1:
            raise RuntimeErrorEB(
                f"rendered application Deployment is ambiguous: {name}"
            )
        deployment = matches[0]
        spec = deployment.get("spec", {})
        template = spec.get("template", {}) if isinstance(spec, dict) else {}
        template_metadata = (
            template.get("metadata", {}) if isinstance(template, dict) else {}
        )
        pod_spec = template.get("spec", {}) if isinstance(template, dict) else {}
        replicas = spec.get("replicas") if isinstance(spec, dict) else None
        if (
            isinstance(replicas, bool)
            or not isinstance(replicas, int)
            or replicas < 1
            or not isinstance(template_metadata, dict)
        ):
            raise RuntimeErrorEB(
                f"rendered application Deployment contract is invalid: {name}"
            )
        pod_contract = _application_pod_spec_projection(
            pod_spec, f"rendered application Deployment {name}"
        )
        deployment_contract = {
            "replicas": replicas,
            "revisionHistoryLimit": spec.get("revisionHistoryLimit", 10),
            "strategy": spec.get("strategy"),
            **_deployment_lifecycle_projection(
                spec, f"rendered application Deployment {name}"
            ),
            "selector_labels": _pod_selector_match_labels(
                deployment, f"rendered application Deployment {name}"
            ),
            "template_labels": template_metadata.get("labels", {}),
            "template_annotations": template_metadata.get("annotations", {}),
            "pod_spec": pod_contract,
        }
        if (
            not isinstance(deployment_contract["template_labels"], dict)
            or not isinstance(
                deployment_contract["template_annotations"], dict
            )
        ):
            raise RuntimeErrorEB(
                f"rendered application template metadata is invalid: {name}"
            )
        result[name] = {
            "contract": deployment_contract,
            "contract_sha256": _stable_json_sha256(deployment_contract),
            "pod_contract_sha256": _stable_json_sha256(pod_contract),
        }
    return result


def _service_account_contract_projection(
    service_account: Any,
    context: str,
) -> dict[str, Any]:
    if not isinstance(service_account, dict):
        raise RuntimeErrorEB(f"{context} ServiceAccount payload is invalid")
    metadata = service_account.get("metadata", {})
    if not isinstance(metadata, dict):
        raise RuntimeErrorEB(f"{context} ServiceAccount metadata is invalid")
    labels = metadata.get("labels", {})
    annotations = metadata.get("annotations", {})
    if not isinstance(labels, dict) or not isinstance(annotations, dict):
        raise RuntimeErrorEB(
            f"{context} ServiceAccount metadata contract is invalid"
        )

    def ref_names(field: str) -> list[str]:
        values = service_account.get(field, [])
        if values is None:
            values = []
        if not isinstance(values, list):
            raise RuntimeErrorEB(
                f"{context} ServiceAccount {field} contract is invalid"
            )
        names: list[str] = []
        for value in values:
            name = value.get("name") if isinstance(value, dict) else None
            if not isinstance(name, str) or not name:
                raise RuntimeErrorEB(
                    f"{context} ServiceAccount {field} entry is invalid"
                )
            names.append(name)
        if len(names) != len(set(names)):
            raise RuntimeErrorEB(
                f"{context} ServiceAccount {field} contains duplicates"
            )
        return sorted(names)

    automount = service_account.get("automountServiceAccountToken")
    if automount is not None and not isinstance(automount, bool):
        raise RuntimeErrorEB(
            f"{context} ServiceAccount automount contract is invalid"
        )
    return {
        "labels": {
            str(key): str(value)
            for key, value in sorted(labels.items())
        },
        "annotations": {
            str(key): str(value)
            for key, value in sorted(annotations.items())
        },
        "automountServiceAccountToken": automount,
        "imagePullSecrets": ref_names("imagePullSecrets"),
        "secrets": ref_names("secrets"),
    }


def _pdb_spec_projection(
    pdb: Any,
    context: str,
) -> dict[str, Any]:
    if not isinstance(pdb, dict):
        raise RuntimeErrorEB(f"{context} PodDisruptionBudget payload is invalid")
    spec = pdb.get("spec", {})
    if not isinstance(spec, dict):
        raise RuntimeErrorEB(f"{context} PodDisruptionBudget spec is invalid")
    selector = spec.get("selector")
    min_available = spec.get("minAvailable")
    max_unavailable = spec.get("maxUnavailable")
    unhealthy_policy = spec.get(
        "unhealthyPodEvictionPolicy",
        "IfHealthyBudget",
    )
    if (
        not isinstance(selector, dict)
        or not selector
        or (min_available is None and max_unavailable is None)
        or (min_available is not None and max_unavailable is not None)
        or unhealthy_policy
        not in {"IfHealthyBudget", "AlwaysAllow"}
    ):
        raise RuntimeErrorEB(
            f"{context} PodDisruptionBudget contract is invalid"
        )
    return {
        "minAvailable": min_available,
        "maxUnavailable": max_unavailable,
        "selector": json.loads(json.dumps(selector)),
        "unhealthyPodEvictionPolicy": unhealthy_policy,
    }


def _rendered_application_pdb_contract(
    root: Path,
    release: dict[str, Any],
) -> dict[str, Any]:
    api_digest = release.get("api_digest") if isinstance(release, dict) else None
    web_digest = release.get("web_digest") if isinstance(release, dict) else None
    if (
        not isinstance(api_digest, str)
        or not DIGEST_RE.fullmatch(api_digest)
        or not isinstance(web_digest, str)
        or not DIGEST_RE.fullmatch(web_digest)
    ):
        raise RuntimeErrorEB(
            "application PDB contract requires exact release digests"
        )
    rendered = _source_commit_application_render(root, release)
    rendered = rendered.replace("$" + "{API_DIGEST}", api_digest).replace(
        "$" + "{WEB_DIGEST}", web_digest
    )
    try:
        documents = [
            document
            for document in yaml.safe_load_all(rendered)
            if isinstance(document, dict)
        ]
    except yaml.YAMLError as exc:
        raise RuntimeErrorEB(
            "rendered Experiment-B PDB contract is invalid"
        ) from exc

    expected_names = {"weltgewebe-api", "weltgewebe-web"}
    result: dict[str, Any] = {}
    for document in documents:
        if (
            document.get("kind") != "PodDisruptionBudget"
            or document.get("metadata", {}).get("namespace")
            != APP_NAMESPACE
        ):
            continue
        name = document.get("metadata", {}).get("name")
        if not isinstance(name, str) or name not in expected_names:
            continue
        if name in result:
            raise RuntimeErrorEB(
                f"rendered application PDB is duplicated: {name}"
            )
        contract = _pdb_spec_projection(
            document, f"rendered application PDB {name}"
        )
        result[name] = {
            "contract": contract,
            "contract_sha256": _stable_json_sha256(contract),
        }
    if set(result) != expected_names:
        raise RuntimeErrorEB(
            "rendered application PDB set is incomplete"
        )
    return result


def _require_live_application_pdbs(
    root: Path,
    release: dict[str, Any],
) -> dict[str, Any]:
    expected = _rendered_application_pdb_contract(root, release)
    result: dict[str, Any] = {}
    for name, expected_value in expected.items():
        pdb = _kubectl_json(
            root,
            [
                "-n",
                APP_NAMESPACE,
                "get",
                "poddisruptionbudget",
                name,
            ],
        )
        metadata = pdb.get("metadata", {}) if isinstance(pdb, dict) else {}
        if (
            not isinstance(metadata, dict)
            or metadata.get("name") != name
            or metadata.get("namespace") != APP_NAMESPACE
            or metadata.get("deletionTimestamp") is not None
        ):
            raise RuntimeErrorEB(
                f"live application PDB identity drifted: {name}"
            )
        observed = _pdb_spec_projection(
            pdb, f"live application PDB {name}"
        )
        if observed != expected_value["contract"]:
            raise RuntimeErrorEB(
                f"live application PDB contract drifted: {name}"
            )
        result[name] = {
            "contract_sha256": expected_value["contract_sha256"],
            "canonical": True,
        }
    return result


def _rendered_application_service_account_contract(
    root: Path,
    release: dict[str, Any],
) -> dict[str, Any]:
    api_digest = release.get("api_digest") if isinstance(release, dict) else None
    web_digest = release.get("web_digest") if isinstance(release, dict) else None
    if (
        not isinstance(api_digest, str)
        or not DIGEST_RE.fullmatch(api_digest)
        or not isinstance(web_digest, str)
        or not DIGEST_RE.fullmatch(web_digest)
    ):
        raise RuntimeErrorEB(
            "application ServiceAccount contract requires exact release digests"
        )
    rendered = _source_commit_application_render(root, release)
    rendered = rendered.replace("$" + "{API_DIGEST}", api_digest).replace(
        "$" + "{WEB_DIGEST}", web_digest
    )
    try:
        documents = [
            document
            for document in yaml.safe_load_all(rendered)
            if isinstance(document, dict)
        ]
    except yaml.YAMLError as exc:
        raise RuntimeErrorEB(
            "rendered Experiment-B ServiceAccount contract is invalid"
        ) from exc

    referenced: set[str] = set()
    for workload in ("weltgewebe-api", "weltgewebe-web"):
        deployments = [
            document
            for document in documents
            if document.get("kind") == "Deployment"
            and document.get("metadata", {}).get("name") == workload
            and document.get("metadata", {}).get("namespace") == APP_NAMESPACE
        ]
        if len(deployments) != 1:
            raise RuntimeErrorEB(
                f"rendered application Deployment is ambiguous: {workload}"
            )
        service_account_name = (
            deployments[0]
            .get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("serviceAccountName")
        )
        if not isinstance(service_account_name, str) or not service_account_name:
            raise RuntimeErrorEB(
                f"rendered application Deployment has no ServiceAccount: {workload}"
            )
        referenced.add(service_account_name)

    result: dict[str, Any] = {}
    for name in sorted(referenced):
        matches = [
            document
            for document in documents
            if document.get("kind") == "ServiceAccount"
            and document.get("metadata", {}).get("name") == name
            and document.get("metadata", {}).get("namespace") == APP_NAMESPACE
        ]
        if len(matches) != 1:
            raise RuntimeErrorEB(
                f"rendered application ServiceAccount is ambiguous: {name}"
            )
        contract = _service_account_contract_projection(
            matches[0], f"rendered application ServiceAccount {name}"
        )
        result[name] = {
            "contract": contract,
            "contract_sha256": _stable_json_sha256(contract),
        }
    if not result:
        raise RuntimeErrorEB(
            "rendered application ServiceAccount contract is empty"
        )
    return result


def _require_live_application_service_accounts(
    root: Path,
    release: dict[str, Any],
) -> dict[str, Any]:
    expected = _rendered_application_service_account_contract(root, release)
    result: dict[str, Any] = {}
    for name, expected_value in expected.items():
        service_account = _kubectl_json(
            root,
            ["-n", APP_NAMESPACE, "get", "serviceaccount", name],
        )
        metadata = (
            service_account.get("metadata", {})
            if isinstance(service_account, dict)
            else {}
        )
        if (
            not isinstance(metadata, dict)
            or metadata.get("name") != name
            or metadata.get("namespace") != APP_NAMESPACE
            or metadata.get("deletionTimestamp") is not None
        ):
            raise RuntimeErrorEB(
                f"live application ServiceAccount identity drifted: {name}"
            )
        observed = _service_account_contract_projection(
            service_account, f"live application ServiceAccount {name}"
        )
        # Flux adds exactly these reconciliation ownership labels to the
        # rendered application ServiceAccounts. All other fields stay pinned.
        flux_owner_labels = {
            "kustomize.toolkit.fluxcd.io/name": "commonthing-experiment-b-app",
            "kustomize.toolkit.fluxcd.io/namespace": "flux-system",
        }
        expected_contract = expected_value["contract"]
        expected_labels = expected_contract["labels"]
        if not expected_labels.keys().isdisjoint(flux_owner_labels):
            raise RuntimeErrorEB(
                f"rendered application ServiceAccount labels overlap Flux ownership: {name}"
            )
        expected_live = {
            **expected_contract,
            "labels": {**expected_labels, **flux_owner_labels},
        }
        if observed != expected_live:
            raise RuntimeErrorEB(
                f"live application ServiceAccount contract drifted: {name}"
            )
        result[name] = {
            "contract_sha256": expected_value["contract_sha256"],
            "canonical": True,
        }
    return result


def _require_live_application_workloads(
    root: Path,
    release: dict[str, Any],
    deployments: dict[str, Any],
    pods_by_workload: dict[str, Any],
    names: tuple[str, ...] = ("weltgewebe-api", "weltgewebe-web"),
) -> dict[str, Any]:
    rendered = _rendered_application_workload_contract(root, release)
    if (
        not names
        or len(set(names)) != len(names)
        or any(name not in rendered for name in names)
    ):
        raise RuntimeErrorEB(
            "live application workload selection is invalid"
        )
    expected = {name: rendered[name] for name in names}
    if (
        set(deployments) != set(expected)
        or set(pods_by_workload) != set(expected)
    ):
        raise RuntimeErrorEB(
            "live application workload inventory is incomplete"
        )
    result: dict[str, Any] = {}
    for name, expected_value in expected.items():
        deployment = deployments[name]
        contract = expected_value["contract"]
        metadata = (
            deployment.get("metadata", {})
            if isinstance(deployment, dict)
            else {}
        )
        spec = (
            deployment.get("spec", {})
            if isinstance(deployment, dict)
            else {}
        )
        template = spec.get("template", {}) if isinstance(spec, dict) else {}
        template_metadata = (
            template.get("metadata", {}) if isinstance(template, dict) else {}
        )
        if (
            not isinstance(metadata, dict)
            or metadata.get("name") != name
            or metadata.get("namespace") != APP_NAMESPACE
            or metadata.get("deletionTimestamp") is not None
            or not isinstance(spec, dict)
            or not isinstance(template_metadata, dict)
        ):
            raise RuntimeErrorEB(
                f"live application Deployment identity drifted: {name}"
            )
        observed_contract = {
            "replicas": spec.get("replicas"),
            "revisionHistoryLimit": spec.get("revisionHistoryLimit", 10),
            "strategy": spec.get("strategy"),
            **_deployment_lifecycle_projection(
                spec, f"live application Deployment {name}"
            ),
            "selector_labels": _pod_selector_match_labels(
                deployment, f"live application Deployment {name}"
            ),
            "template_labels": template_metadata.get("labels", {}),
            "template_annotations": template_metadata.get(
                "annotations", {}
            ),
            "pod_spec": _application_pod_spec_projection(
                template.get("spec", {}),
                f"live application Deployment {name}",
            ),
        }
        if observed_contract != contract:
            raise RuntimeErrorEB(
                f"live application Deployment contract drifted: {name}"
            )

        pods = pods_by_workload[name]
        if not isinstance(pods, list) or len(pods) != contract["replicas"]:
            raise RuntimeErrorEB(
                f"live application Pod set drifted: {name}"
            )
        pod_names: list[str] = []
        for pod in pods:
            if not isinstance(pod, dict):
                raise RuntimeErrorEB(
                    f"live application Pod inventory is invalid: {name}"
                )
            pod_metadata = pod.get("metadata", {})
            pod_spec = pod.get("spec", {})
            labels = (
                pod_metadata.get("labels", {})
                if isinstance(pod_metadata, dict)
                else {}
            )
            annotations = (
                pod_metadata.get("annotations", {})
                if isinstance(pod_metadata, dict)
                else {}
            )
            pod_name = (
                pod_metadata.get("name")
                if isinstance(pod_metadata, dict)
                else None
            )
            if (
                not isinstance(pod_name, str)
                or not pod_name
                or pod_name in pod_names
                or pod_metadata.get("namespace") != APP_NAMESPACE
                or pod_metadata.get("deletionTimestamp") is not None
                or not isinstance(labels, dict)
                or not isinstance(annotations, dict)
                or any(
                    labels.get(key) != value
                    for key, value in contract["template_labels"].items()
                )
                or any(
                    annotations.get(key) != value
                    for key, value in contract[
                        "template_annotations"
                    ].items()
                )
                or _application_pod_spec_projection(
                    pod_spec, f"live application Pod {pod_name}"
                )
                != contract["pod_spec"]
            ):
                raise RuntimeErrorEB(
                    f"live application Pod contract drifted: {name}"
                )
            pod_names.append(pod_name)
        result[name] = {
            "contract_sha256": expected_value["contract_sha256"],
            "pod_contract_sha256": expected_value[
                "pod_contract_sha256"
            ],
            "pod_names": sorted(pod_names),
            "canonical": True,
        }
    return result


def _container_resources_contract(
    pod_spec: Any,
    container_name: str,
    context: str,
) -> dict[str, Any]:
    if not isinstance(pod_spec, dict):
        raise RuntimeErrorEB(f"{context} Pod spec is invalid")
    containers = pod_spec.get("containers", [])
    if not isinstance(containers, list):
        raise RuntimeErrorEB(f"{context} container inventory is invalid")
    matches = [
        item
        for item in containers
        if isinstance(item, dict) and item.get("name") == container_name
    ]
    if len(matches) != 1:
        raise RuntimeErrorEB(
            f"{context} does not contain exactly one container: {container_name}"
        )
    resources = matches[0].get("resources")
    if not isinstance(resources, dict):
        raise RuntimeErrorEB(
            f"{context} resources are missing: {container_name}"
        )
    requests = resources.get("requests")
    limits = resources.get("limits")
    if (
        not isinstance(requests, dict)
        or not requests
        or not isinstance(limits, dict)
        or not limits
        or any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, str)
            or not value
            for mapping in (requests, limits)
            for key, value in mapping.items()
        )
    ):
        raise RuntimeErrorEB(
            f"{context} resource contract is invalid: {container_name}"
        )
    return json.loads(json.dumps(resources))



def _data_container_resources_from_bytes(
    manifest_bytes: bytes,
    deployment_name: str,
    container_name: str,
    context: str,
) -> dict[str, Any]:
    try:
        documents = list(
            yaml.safe_load_all(manifest_bytes.decode("utf-8"))
        )
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise RuntimeErrorEB(
            f"{context} resource manifest is invalid: {deployment_name}"
        ) from exc
    matches = [
        document
        for document in documents
        if isinstance(document, dict)
        and document.get("kind") == "Deployment"
        and document.get("metadata", {}).get("name") == deployment_name
        and document.get("metadata", {}).get("namespace") == DATA_NAMESPACE
    ]
    if len(matches) != 1:
        raise RuntimeErrorEB(
            f"{context} is ambiguous: {deployment_name}"
        )
    return _container_resources_contract(
        matches[0].get("spec", {}).get("template", {}).get("spec"),
        container_name,
        f"{context} {deployment_name}",
    )


def _versioned_data_container_resources(
    path: Path,
    deployment_name: str,
    container_name: str,
) -> dict[str, Any]:
    try:
        manifest_bytes = path.read_bytes()
    except OSError as exc:
        raise RuntimeErrorEB(
            f"versioned data resource manifest is invalid: {deployment_name}"
        ) from exc
    return _data_container_resources_from_bytes(
        manifest_bytes,
        deployment_name,
        container_name,
        "versioned data Deployment",
    )


def _source_commit_data_container_resources(
    source_commit: str,
    path: Path,
    deployment_name: str,
    container_name: str,
) -> dict[str, Any]:
    return _data_container_resources_from_bytes(
        _git_blob_bytes(source_commit, path),
        deployment_name,
        container_name,
        "source-commit data Deployment",
    )

def _data_deployment_contract_from_bytes(
    manifest_bytes: bytes,
    name: str,
    context: str,
) -> dict[str, Any]:
    try:
        documents = list(
            yaml.safe_load_all(manifest_bytes.decode("utf-8"))
        )
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise RuntimeErrorEB(
            f"{context} manifest is invalid: {name}"
        ) from exc
    matches = [
        document
        for document in documents
        if isinstance(document, dict)
        and document.get("kind") == "Deployment"
        and document.get("metadata", {}).get("name") == name
        and document.get("metadata", {}).get("namespace") == DATA_NAMESPACE
    ]
    if len(matches) != 1:
        raise RuntimeErrorEB(
            f"{context} does not contain exactly one Deployment: {name}"
        )
    spec = matches[0].get("spec", {})
    replicas = spec.get("replicas")
    if isinstance(replicas, bool) or not isinstance(replicas, int) or replicas != 1:
        raise RuntimeErrorEB(
            f"{context} replica contract drifted: {name}"
        )
    deployment = matches[0]
    template = spec.get("template", {}) if isinstance(spec, dict) else {}
    template_metadata = (
        template.get("metadata", {}) if isinstance(template, dict) else {}
    )
    pod_spec = template.get("spec", {}) if isinstance(template, dict) else {}
    if not isinstance(template_metadata, dict):
        raise RuntimeErrorEB(
            f"{context} template metadata is invalid: {name}"
        )
    images = _pod_spec_images(
        pod_spec,
        f"{context} {name}",
    )
    selector = _pod_selector_match_labels(
        deployment, f"{context} {name}"
    )
    pod_contract = _application_pod_spec_projection(
        pod_spec, f"{context} {name}"
    )
    deployment_contract = {
        "replicas": replicas,
        "revisionHistoryLimit": spec.get("revisionHistoryLimit", 10),
        "strategy": spec.get("strategy"),
        **_deployment_lifecycle_projection(
            spec, f"{context} {name}"
        ),
        "selector_labels": selector,
        "template_labels": template_metadata.get("labels", {}),
        "template_annotations": template_metadata.get("annotations", {}),
        "pod_spec": pod_contract,
    }
    if (
        not isinstance(deployment_contract["template_labels"], dict)
        or not isinstance(deployment_contract["template_annotations"], dict)
    ):
        raise RuntimeErrorEB(
            f"{context} template metadata is invalid: {name}"
        )
    return {
        "replicas": replicas,
        "images": images,
        "selector_labels": selector,
        "contract": deployment_contract,
        "contract_sha256": _stable_json_sha256(deployment_contract),
        "pod_contract_sha256": _stable_json_sha256(pod_contract),
    }


def _versioned_data_deployment_contract(path: Path, name: str) -> dict[str, Any]:
    try:
        manifest_bytes = path.read_bytes()
    except OSError as exc:
        raise RuntimeErrorEB(
            f"versioned data Deployment manifest is invalid: {name}"
        ) from exc
    return _data_deployment_contract_from_bytes(
        manifest_bytes,
        name,
        "versioned data Deployment",
    )


def _source_commit_data_deployment_contract(
    source_commit: str,
    path: Path,
    name: str,
) -> dict[str, Any]:
    return _data_deployment_contract_from_bytes(
        _git_blob_bytes(source_commit, path),
        name,
        "source-commit data Deployment",
    )

def _require_live_data_deployments(
    root: Path,
    names: tuple[str, ...] = ("postgres", "nats"),
    *,
    source_commit: str | None = None,
) -> dict[str, Any]:
    manifests = {
        "postgres": CLUSTER / "data/postgres.yaml",
        "nats": CLUSTER / "data/nats.yaml",
    }
    if (
        not names
        or len(set(names)) != len(names)
        or any(name not in manifests for name in names)
    ):
        raise RuntimeErrorEB("live data Deployment selection is invalid")
    pod_items = _kubectl_json(
        root, ["-n", DATA_NAMESPACE, "get", "pods"]
    ).get("items")
    if not isinstance(pod_items, list) or any(
        not isinstance(item, dict) for item in pod_items
    ):
        raise RuntimeErrorEB("live data Pod inventory is invalid")

    result: dict[str, Any] = {}
    for name in names:
        path = manifests[name]
        expected = (
            _versioned_data_deployment_contract(path, name)
            if source_commit is None
            else _source_commit_data_deployment_contract(
                source_commit, path, name
            )
        )
        deployment = _kubectl_json(
            root, ["-n", DATA_NAMESPACE, "get", "deployment", name]
        )
        metadata = (
            deployment.get("metadata", {}) if isinstance(deployment, dict) else {}
        )
        if (
            not isinstance(metadata, dict)
            or metadata.get("name") != name
            or metadata.get("namespace") != DATA_NAMESPACE
            or metadata.get("deletionTimestamp") is not None
        ):
            raise RuntimeErrorEB(f"live data Deployment identity drifted: {name}")
        live_selector = _pod_selector_match_labels(
            deployment, f"live data Deployment {name}"
        )
        if live_selector != expected["selector_labels"]:
            raise RuntimeErrorEB(
                f"live data Deployment selector drifted from versioned manifest: {name}"
            )
        availability = _deployment_availability_snapshot(
            deployment, name, expected["replicas"]
        )
        live_spec = deployment.get("spec", {})
        live_template = (
            live_spec.get("template", {})
            if isinstance(live_spec, dict)
            else {}
        )
        live_template_metadata = (
            live_template.get("metadata", {})
            if isinstance(live_template, dict)
            else {}
        )
        if not isinstance(live_template_metadata, dict):
            raise RuntimeErrorEB(
                f"live data Deployment template metadata is invalid: {name}"
            )
        live_pod_spec = (
            live_template.get("spec", {})
            if isinstance(live_template, dict)
            else {}
        )
        live_images = _pod_spec_images(
            live_pod_spec,
            f"live data Deployment {name}",
        )
        if live_images != expected["images"]:
            raise RuntimeErrorEB(
                f"live data Deployment images drifted from versioned manifest: {name}"
            )
        observed_contract = {
            "replicas": live_spec.get("replicas"),
            "revisionHistoryLimit": live_spec.get(
                "revisionHistoryLimit", 10
            ),
            "strategy": live_spec.get("strategy"),
            **_deployment_lifecycle_projection(
                live_spec, f"live data Deployment {name}"
            ),
            "selector_labels": live_selector,
            "template_labels": live_template_metadata.get("labels", {}),
            "template_annotations": live_template_metadata.get(
                "annotations", {}
            ),
            "pod_spec": _application_pod_spec_projection(
                live_pod_spec, f"live data Deployment {name}"
            ),
        }
        if observed_contract != expected["contract"]:
            raise RuntimeErrorEB(
                f"live data Deployment contract drifted from versioned manifest: {name}"
            )

        matching_pods = _pods_matching_labels(
            pod_items, expected["selector_labels"]
        )
        pod_readback = _require_running_pod_image_contract(
            matching_pods,
            namespace=DATA_NAMESPACE,
            workload=name,
            expected_replicas=expected["replicas"],
            expected_images=expected["images"],
            required_labels=expected["selector_labels"],
            context="data Pod",
        )
        if len(matching_pods) != expected["replicas"]:
            raise RuntimeErrorEB(
                f"live data Pod set drifted: {name}"
            )
        pod_names: list[str] = []
        pod_container_ids: dict[str, dict[str, str]] = {}
        for pod in matching_pods:
            metadata = pod.get("metadata", {}) if isinstance(pod, dict) else {}
            pod_spec = pod.get("spec", {}) if isinstance(pod, dict) else {}
            labels = metadata.get("labels", {}) if isinstance(metadata, dict) else {}
            annotations = (
                metadata.get("annotations", {})
                if isinstance(metadata, dict)
                else {}
            )
            pod_name = metadata.get("name") if isinstance(metadata, dict) else None
            if (
                not isinstance(pod_name, str)
                or not pod_name
                or pod_name in pod_names
                or metadata.get("namespace") != DATA_NAMESPACE
                or metadata.get("deletionTimestamp") is not None
                or not isinstance(labels, dict)
                or not isinstance(annotations, dict)
                or any(
                    labels.get(key) != value
                    for key, value in expected["contract"][
                        "template_labels"
                    ].items()
                )
                or any(
                    annotations.get(key) != value
                    for key, value in expected["contract"][
                        "template_annotations"
                    ].items()
                )
                or _application_pod_spec_projection(
                    pod_spec, f"live data Pod {pod_name}"
                )
                != expected["contract"]["pod_spec"]
            ):
                raise RuntimeErrorEB(
                    f"live data Pod contract drifted: {name}"
                )
            status_obj = (
                pod.get("status", {})
                if isinstance(pod, dict)
                else {}
            )
            statuses = (
                status_obj.get("containerStatuses", [])
                if isinstance(status_obj, dict)
                else []
            )
            if not isinstance(statuses, list):
                raise RuntimeErrorEB(
                    f"live data Pod container runtime inventory is invalid: {name}"
                )
            status_by_name = {
                str(item.get("name", "")): item
                for item in statuses
                if isinstance(item, dict) and item.get("name")
            }
            runtime_ids: dict[str, str] = {}
            for container_name in expected["images"]["containers"]:
                item = status_by_name.get(container_name)
                container_id = (
                    item.get("containerID")
                    if isinstance(item, dict)
                    else None
                )
                if (
                    not isinstance(container_id, str)
                    or re.fullmatch(
                        r"containerd://[0-9a-f]{64}",
                        container_id,
                    )
                    is None
                ):
                    raise RuntimeErrorEB(
                        f"live data Pod containerID is invalid: "
                        f"{name}/{pod_name}/{container_name}"
                    )
                runtime_ids[container_name] = container_id
            pod_container_ids[pod_name] = runtime_ids
            pod_names.append(pod_name)

        result[name] = {
            **availability,
            "images_sha256": _stable_json_sha256(live_images),
            "images_canonical": True,
            "pods": pod_readback,
            "contract_sha256": expected["contract_sha256"],
            "pod_contract_sha256": expected[
                "pod_contract_sha256"
            ],
            "pod_names": sorted(pod_names),
            "container_ids": pod_container_ids,
            "canonical": True,
        }

    if set(names) == set(manifests):
        expected_pod_names = {
            pod_name
            for value in result.values()
            for pod_name in value["pod_names"]
        }
        observed_pod_names: set[str] = set()
        for pod in pod_items:
            metadata = pod.get("metadata", {}) if isinstance(pod, dict) else {}
            pod_name = metadata.get("name") if isinstance(metadata, dict) else None
            if (
                not isinstance(pod_name, str)
                or not pod_name
                or metadata.get("namespace") != DATA_NAMESPACE
                or pod_name in observed_pod_names
            ):
                raise RuntimeErrorEB(
                    "live data Pod inventory contains invalid or duplicate Pods"
                )
            observed_pod_names.add(pod_name)
        if observed_pod_names != expected_pod_names:
            missing = sorted(expected_pod_names - observed_pod_names)
            unexpected = sorted(observed_pod_names - expected_pod_names)
            raise RuntimeErrorEB(
                "live data Pod inventory contains noncanonical Pods: "
                f"missing={missing}; unexpected={unexpected}"
            )
    return result


def _normalize_cilium_pod_spec(
    pod_spec: Any,
    context: str,
) -> dict[str, Any]:
    if not isinstance(pod_spec, dict):
        raise RuntimeErrorEB(f"{context} Pod spec is invalid")
    normalized = json.loads(json.dumps(pod_spec))

    volumes = normalized.get("volumes") or []
    if not isinstance(volumes, list) or any(
        not isinstance(volume, dict) for volume in volumes
    ):
        raise RuntimeErrorEB(f"{context} volume contract is invalid")
    for volume in volumes:
        host_path = volume.get("hostPath")
        if isinstance(host_path, dict) and host_path.get("type") == "":
            host_path.pop("type", None)
        config_map = volume.get("configMap")
        if isinstance(config_map, dict) and config_map.get("defaultMode") == 420:
            config_map.pop("defaultMode", None)

    for field in ("containers", "initContainers"):
        items = normalized.get(field, [])
        if not isinstance(items, list) or any(
            not isinstance(item, dict) for item in items
        ):
            raise RuntimeErrorEB(f"{context} {field} inventory is invalid")
        for container in items:
            resources = container.get("resources")
            if resources is None:
                resources = {}
            if not isinstance(resources, dict):
                raise RuntimeErrorEB(
                    f"{context} container resources contract is invalid"
                )
            resources = json.loads(json.dumps(resources))
            for bucket_name in ("limits", "requests"):
                bucket = resources.get(bucket_name)
                if bucket is None:
                    continue
                if not isinstance(bucket, dict):
                    raise RuntimeErrorEB(
                        f"{context} {bucket_name} resources contract is invalid"
                    )
                for resource_name, value in list(bucket.items()):
                    if isinstance(value, bool) or not isinstance(
                        value, (str, int, float)
                    ):
                        raise RuntimeErrorEB(
                            f"{context} resource quantity is invalid"
                        )
                    try:
                        if resource_name == "cpu":
                            bucket[resource_name] = _parse_cpu_quantity(str(value))
                        elif resource_name == "memory":
                            bucket[resource_name] = _parse_memory_quantity(str(value))
                        elif isinstance(value, (int, float)):
                            bucket[resource_name] = str(value)
                    except (TypeError, ValueError) as exc:
                        raise RuntimeErrorEB(
                            f"{context} resource quantity is invalid"
                        ) from exc
            container["resources"] = resources

            mounts = container.get("volumeMounts")
            if mounts is None:
                continue
            if not isinstance(mounts, list) or any(
                not isinstance(mount, dict) for mount in mounts
            ):
                raise RuntimeErrorEB(
                    f"{context} container volumeMounts contract is invalid"
                )
            for mount in mounts:
                if mount.get("readOnly") is False:
                    mount.pop("readOnly", None)

    return normalized


def _cilium_pod_spec_projection(
    pod_spec: Any,
    context: str,
) -> dict[str, Any]:
    normalized = _normalize_cilium_pod_spec(pod_spec, context)
    projection = _application_pod_spec_projection(normalized, context)
    pod_spec = normalized
    projection.update(
        {
            "hostNetwork": pod_spec.get("hostNetwork", False),
            "hostPID": pod_spec.get("hostPID", False),
            "hostIPC": pod_spec.get("hostIPC", False),
            "dnsPolicy": pod_spec.get("dnsPolicy", "ClusterFirst"),
            "dnsConfig": pod_spec.get("dnsConfig"),
            "tolerations": pod_spec.get("tolerations") or [],
            "restartPolicy": pod_spec.get("restartPolicy", "Always"),
            "schedulerName": pod_spec.get(
                "schedulerName", "default-scheduler"
            ),
            "enableServiceLinks": pod_spec.get("enableServiceLinks", True),
            "shareProcessNamespace": pod_spec.get(
                "shareProcessNamespace", False
            ),
        }
    )
    return projection


def _expected_cilium_runtime_contract(
    root: Path,
    config: dict[str, Any],
    toolchain_receipt: dict[str, Any],
) -> dict[str, Any]:
    tools = toolchain_receipt.get("tools", {})
    artifacts = toolchain_receipt.get("artifacts", {})
    helm = tools.get("helm") if isinstance(tools, dict) else None
    chart = artifacts.get("cilium_chart") if isinstance(artifacts, dict) else None
    if not isinstance(helm, str) or not isinstance(chart, str):
        raise RuntimeErrorEB(
            "pinned Cilium chart/toolchain binding is unavailable"
        )

    rendered = run(
        [
            helm,
            "template",
            "cilium",
            chart,
            "--namespace",
            "kube-system",
            "--kube-version",
            str(config["kubernetes"]["kubernetes_version"]),
            *_cilium_helm_value_args(vm_ip()),
        ],
        env=kube_env(root),
    ).stdout
    try:
        documents = [
            document
            for document in yaml.safe_load_all(rendered)
            if isinstance(document, dict)
        ]
    except yaml.YAMLError as exc:
        raise RuntimeErrorEB("pinned Cilium chart render is invalid") from exc

    config_maps = [
        document
        for document in documents
        if document.get("kind") == "ConfigMap"
        and document.get("metadata", {}).get("name") == "cilium-config"
        and document.get("metadata", {}).get("namespace") == "kube-system"
    ]
    if len(config_maps) != 1:
        raise RuntimeErrorEB(
            "pinned Cilium chart does not render exactly one "
            "kube-system ConfigMap cilium-config"
        )
    config_map = config_maps[0]
    config_data = config_map.get("data", {})
    config_binary_data = config_map.get("binaryData", {})
    config_immutable = config_map.get("immutable", False)
    if (
        not isinstance(config_data, dict)
        or not isinstance(config_binary_data, dict)
        or not isinstance(config_immutable, bool)
    ):
        raise RuntimeErrorEB("pinned Cilium ConfigMap contract is invalid")
    config_map_contract = {
        "data": json.loads(json.dumps(config_data)),
        "binaryData": json.loads(json.dumps(config_binary_data)),
        "immutable": config_immutable,
    }

    def workload_contract(
        kind: str,
        name: str,
        context: str,
    ) -> dict[str, Any]:
        workloads = [
            document
            for document in documents
            if document.get("kind") == kind
            and document.get("metadata", {}).get("name") == name
            and document.get("metadata", {}).get("namespace") == "kube-system"
        ]
        if len(workloads) != 1:
            raise RuntimeErrorEB(
                f"pinned Cilium chart does not render exactly one "
                f"kube-system {kind} {name}"
            )
        workload = workloads[0]
        workload_spec = workload.get("spec", {})
        pod_spec = (
            workload_spec.get("template", {}).get("spec")
            if isinstance(workload_spec, dict)
            else None
        )
        result = {
            "images": _pod_spec_images(pod_spec, context),
            "selector_labels": _pod_selector_match_labels(
                workload, context
            ),
            "pod_spec": _cilium_pod_spec_projection(
                pod_spec, context
            ),
        }
        if kind == "DaemonSet":
            result["rollout"] = _daemonset_rollout_projection(
                workload_spec, context
            )
        if kind == "Deployment":
            replicas = (
                workload_spec.get("replicas", 1)
                if isinstance(workload_spec, dict)
                else None
            )
            if (
                isinstance(replicas, bool)
                or not isinstance(replicas, int)
                or replicas < 1
            ):
                raise RuntimeErrorEB(
                    f"{context} Deployment replica contract is invalid"
                )
            result["replicas"] = replicas
            result["lifecycle"] = _deployment_lifecycle_projection(
                workload_spec, context
            )
            result["rollout"] = _deployment_rollout_projection(
                workload_spec, context
            )
        return result

    return {
        "config_map": config_map_contract,
        "daemonset": workload_contract(
            "DaemonSet", "cilium", "pinned Cilium DaemonSet"
        ),
        "operator": workload_contract(
            "Deployment",
            "cilium-operator",
            "pinned Cilium operator Deployment",
        ),
        "relay": workload_contract(
            "Deployment",
            "hubble-relay",
            "pinned Hubble Relay Deployment",
        ),
    }


def _require_live_cilium_contract(
    root: Path, config: dict[str, Any]
) -> dict[str, Any]:
    toolchain_receipt = toolchain(root)
    tools = toolchain_receipt["tools"]
    helm = tools["helm"]
    env = kube_env(root)
    try:
        releases = json.loads(
            run(
                [
                    helm,
                    "list",
                    "--namespace",
                    "kube-system",
                    "--filter",
                    "^cilium$",
                    "--output",
                    "json",
                ],
                env=env,
            ).stdout
        )
        values = json.loads(
            run(
                [
                    helm,
                    "get",
                    "values",
                    "cilium",
                    "--namespace",
                    "kube-system",
                    "--all",
                    "--output",
                    "json",
                ],
                env=env,
            ).stdout
        )
    except json.JSONDecodeError as exc:
        raise RuntimeErrorEB(
            "live Cilium Helm state is not valid JSON"
        ) from exc
    expected_chart = f"cilium-{config['cilium']['chart_version']}"
    if (
        not isinstance(releases, list)
        or len(releases) != 1
        or not isinstance(releases[0], dict)
        or releases[0].get("name") != "cilium"
        or releases[0].get("namespace") != "kube-system"
        or releases[0].get("status") != "deployed"
        or releases[0].get("chart") != expected_chart
    ):
        raise RuntimeErrorEB(
            "live Cilium Helm release differs from the pinned contract"
        )
    if (
        not isinstance(values, dict)
        or values.get("kubeProxyReplacement") is not True
        or not isinstance(values.get("gatewayAPI"), dict)
        or values["gatewayAPI"].get("enabled") is not True
        or not isinstance(values.get("hubble"), dict)
        or not isinstance(values["hubble"].get("relay"), dict)
        or values["hubble"]["relay"].get("enabled") is not True
    ):
        raise RuntimeErrorEB(
            "live Cilium Gateway API/kube-proxy replacement/Hubble Relay "
            "configuration drifted"
        )

    expected_runtime = _expected_cilium_runtime_contract(
        root, config, toolchain_receipt
    )
    expected_config_map = expected_runtime["config_map"]
    live_config_map = _kubectl_json(
        root,
        ["-n", "kube-system", "get", "configmap", "cilium-config"],
    )
    config_metadata = (
        live_config_map.get("metadata", {})
        if isinstance(live_config_map, dict)
        else {}
    )
    live_config_data = (
        live_config_map.get("data", {})
        if isinstance(live_config_map, dict)
        else None
    )
    live_config_binary_data = (
        live_config_map.get("binaryData", {})
        if isinstance(live_config_map, dict)
        else None
    )
    live_config_immutable = (
        live_config_map.get("immutable", False)
        if isinstance(live_config_map, dict)
        else None
    )
    if (
        not isinstance(config_metadata, dict)
        or config_metadata.get("name") != "cilium-config"
        or config_metadata.get("namespace") != "kube-system"
        or config_metadata.get("deletionTimestamp") is not None
        or not isinstance(live_config_data, dict)
        or not isinstance(live_config_binary_data, dict)
        or not isinstance(live_config_immutable, bool)
    ):
        raise RuntimeErrorEB(
            "live Cilium ConfigMap identity/shape drifted"
        )
    live_config_contract = {
        "data": live_config_data,
        "binaryData": live_config_binary_data,
        "immutable": live_config_immutable,
    }
    if live_config_contract != expected_config_map:
        raise RuntimeErrorEB(
            "live Cilium ConfigMap drifted from the pinned chart render"
        )

    daemonset = _kubectl_json(
        root, ["-n", "kube-system", "get", "daemonset", "cilium"]
    )
    metadata = daemonset.get("metadata", {})
    status_obj = daemonset.get("status", {})
    if (
        not isinstance(metadata, dict)
        or metadata.get("name") != "cilium"
        or metadata.get("namespace") != "kube-system"
        or metadata.get("deletionTimestamp") is not None
    ):
        raise RuntimeErrorEB("live Cilium DaemonSet identity drifted")
    generation = int(metadata.get("generation") or 0)
    observed_generation = int(status_obj.get("observedGeneration") or 0)
    desired = int(status_obj.get("desiredNumberScheduled") or 0)
    updated = int(status_obj.get("updatedNumberScheduled") or 0)
    ready = int(status_obj.get("numberReady") or 0)
    available = int(status_obj.get("numberAvailable") or 0)
    unavailable = int(status_obj.get("numberUnavailable") or 0)
    if (
        generation < 1
        or observed_generation != generation
        or desired < 1
        or updated != desired
        or ready != desired
        or available != desired
        or unavailable != 0
    ):
        raise RuntimeErrorEB(
            "live Cilium DaemonSet is not fully converged"
        )

    expected_daemonset = expected_runtime["daemonset"]
    daemonset_spec = daemonset.get("spec", {})
    if (
        not isinstance(daemonset_spec, dict)
        or _daemonset_rollout_projection(
            daemonset_spec, "live Cilium DaemonSet"
        )
        != expected_daemonset["rollout"]
    ):
        raise RuntimeErrorEB(
            "live Cilium DaemonSet rollout drifted from the pinned chart render"
        )
    daemonset_pod_spec = (
        daemonset_spec.get("template", {}).get("spec")
    )
    live_images = _pod_spec_images(
        daemonset_pod_spec,
        "live Cilium DaemonSet",
    )
    if live_images != expected_daemonset["images"]:
        raise RuntimeErrorEB(
            "live Cilium DaemonSet images drifted from the "
            "pinned chart render"
        )
    daemonset_selector = _pod_selector_match_labels(
        daemonset, "live Cilium DaemonSet"
    )
    if (
        daemonset_selector != expected_daemonset["selector_labels"]
        or _cilium_pod_spec_projection(
            daemonset_pod_spec, "live Cilium DaemonSet"
        )
        != expected_daemonset["pod_spec"]
    ):
        raise RuntimeErrorEB(
            "live Cilium DaemonSet pod contract drifted from the "
            "pinned chart render"
        )

    operator = _kubectl_json(
        root,
        ["-n", "kube-system", "get", "deployment", "cilium-operator"],
    )
    operator_metadata = operator.get("metadata", {})
    if (
        not isinstance(operator_metadata, dict)
        or operator_metadata.get("name") != "cilium-operator"
        or operator_metadata.get("namespace") != "kube-system"
        or operator_metadata.get("deletionTimestamp") is not None
    ):
        raise RuntimeErrorEB(
            "live Cilium operator Deployment identity drifted"
        )
    expected_operator = expected_runtime["operator"]
    operator_spec = operator.get("spec", {})
    operator_availability = _deployment_availability_snapshot(
        operator, "cilium-operator", expected_operator["replicas"]
    )
    if (
        not isinstance(operator_spec, dict)
        or _deployment_lifecycle_projection(
            operator_spec, "live Cilium operator Deployment"
        )
        != expected_operator["lifecycle"]
        or _deployment_rollout_projection(
            operator_spec, "live Cilium operator Deployment"
        )
        != expected_operator["rollout"]
    ):
        raise RuntimeErrorEB(
            "live Cilium operator lifecycle drifted from the pinned chart render"
        )
    operator_pod_spec = (
        operator_spec.get("template", {}).get("spec")
        if isinstance(operator_spec, dict)
        else None
    )
    operator_images = _pod_spec_images(
        operator_pod_spec,
        "live Cilium operator Deployment",
    )
    if operator_images != expected_operator["images"]:
        raise RuntimeErrorEB(
            "live Cilium operator images drifted from the "
            "pinned chart render"
        )
    operator_selector = _pod_selector_match_labels(
        operator, "live Cilium operator Deployment"
    )
    if (
        operator_selector != expected_operator["selector_labels"]
        or _cilium_pod_spec_projection(
            operator_pod_spec, "live Cilium operator Deployment"
        )
        != expected_operator["pod_spec"]
    ):
        raise RuntimeErrorEB(
            "live Cilium operator pod contract drifted from the "
            "pinned chart render"
        )

    relay = _kubectl_json(
        root,
        ["-n", "kube-system", "get", "deployment", "hubble-relay"],
    )
    relay_metadata = relay.get("metadata", {})
    if (
        not isinstance(relay_metadata, dict)
        or relay_metadata.get("name") != "hubble-relay"
        or relay_metadata.get("namespace") != "kube-system"
        or relay_metadata.get("deletionTimestamp") is not None
    ):
        raise RuntimeErrorEB(
            "live Hubble Relay Deployment identity drifted"
        )
    expected_relay = expected_runtime["relay"]
    relay_spec = relay.get("spec", {})
    relay_availability = _deployment_availability_snapshot(
        relay, "hubble-relay", expected_relay["replicas"]
    )
    if (
        not isinstance(relay_spec, dict)
        or _deployment_lifecycle_projection(
            relay_spec, "live Hubble Relay Deployment"
        )
        != expected_relay["lifecycle"]
        or _deployment_rollout_projection(
            relay_spec, "live Hubble Relay Deployment"
        )
        != expected_relay["rollout"]
    ):
        raise RuntimeErrorEB(
            "live Hubble Relay lifecycle drifted from the pinned chart render"
        )
    relay_pod_spec = (
        relay_spec.get("template", {}).get("spec")
        if isinstance(relay_spec, dict)
        else None
    )
    relay_images = _pod_spec_images(
        relay_pod_spec,
        "live Hubble Relay Deployment",
    )
    if relay_images != expected_relay["images"]:
        raise RuntimeErrorEB(
            "live Hubble Relay images drifted from the pinned chart render"
        )
    relay_selector = _pod_selector_match_labels(
        relay, "live Hubble Relay Deployment"
    )
    if (
        relay_selector != expected_relay["selector_labels"]
        or _cilium_pod_spec_projection(
            relay_pod_spec, "live Hubble Relay Deployment"
        )
        != expected_relay["pod_spec"]
    ):
        raise RuntimeErrorEB(
            "live Hubble Relay pod contract drifted from the pinned chart render"
        )

    proxy_daemonsets = _kubectl_json(
        root, ["-n", "kube-system", "get", "daemonsets"]
    ).get("items")
    proxy_pods = _kubectl_json(
        root, ["-n", "kube-system", "get", "pods"]
    ).get("items")
    if (
        not isinstance(proxy_daemonsets, list)
        or not isinstance(proxy_pods, list)
        or not all(
            isinstance(item, dict)
            for item in (*proxy_daemonsets, *proxy_pods)
        )
    ):
        raise RuntimeErrorEB("kube-system workload inventory is invalid")

    daemonset_pod_items = _pods_matching_labels(
        proxy_pods, daemonset_selector
    )
    operator_pod_items = _pods_matching_labels(
        proxy_pods, operator_selector
    )
    relay_pod_items = _pods_matching_labels(proxy_pods, relay_selector)

    _require_running_pod_active_deadline_contract(
        daemonset_pod_items,
        expected_active_deadline_seconds=expected_daemonset["pod_spec"][
            "activeDeadlineSeconds"
        ],
        context="Cilium DaemonSet Pod",
    )
    _require_running_pod_active_deadline_contract(
        operator_pod_items,
        expected_active_deadline_seconds=expected_operator["pod_spec"][
            "activeDeadlineSeconds"
        ],
        context="Cilium operator Pod",
    )
    _require_running_pod_active_deadline_contract(
        relay_pod_items,
        expected_active_deadline_seconds=expected_relay["pod_spec"][
            "activeDeadlineSeconds"
        ],
        context="Hubble Relay Pod",
    )

    daemonset_pods = _require_running_pod_image_contract(
        daemonset_pod_items,
        namespace="kube-system",
        workload="cilium",
        expected_replicas=desired,
        expected_images=expected_daemonset["images"],
        required_labels=daemonset_selector,
        context="Cilium DaemonSet Pod",
    )
    operator_pods = _require_running_pod_image_contract(
        operator_pod_items,
        namespace="kube-system",
        workload="cilium-operator",
        expected_replicas=expected_operator["replicas"],
        expected_images=expected_operator["images"],
        required_labels=operator_selector,
        context="Cilium operator Pod",
    )
    relay_pods = _require_running_pod_image_contract(
        relay_pod_items,
        namespace="kube-system",
        workload="hubble-relay",
        expected_replicas=expected_relay["replicas"],
        expected_images=expected_relay["images"],
        required_labels=relay_selector,
        context="Hubble Relay Pod",
    )

    def is_kube_proxy(item: dict[str, Any]) -> bool:
        metadata = item.get("metadata", {})
        if not isinstance(metadata, dict):
            return False
        name = str(metadata.get("name") or "")
        labels = metadata.get("labels", {})
        if not isinstance(labels, dict):
            labels = {}
        return (
            name == "kube-proxy"
            or name.startswith("kube-proxy-")
            or labels.get("k8s-app") == "kube-proxy"
            or labels.get("component") == "kube-proxy"
        )

    if any(
        is_kube_proxy(item)
        for item in (*proxy_daemonsets, *proxy_pods)
    ):
        raise RuntimeErrorEB("kube-proxy is present in Experiment B")

    return {
        "chart": expected_chart,
        "chart_version": config["cilium"]["chart_version"],
        "gateway_api": True,
        "kube_proxy_replacement": True,
        "config_map_sha256": _stable_json_sha256(
            live_config_contract
        ),
        "config_map_canonical": True,
        "daemonset_generation": generation,
        "daemonset_desired": desired,
        "daemonset_ready": ready,
        "daemonset_images": live_images,
        "daemonset_images_canonical": True,
        "daemonset_contract_sha256": _stable_json_sha256(
            expected_daemonset
        ),
        "daemonset_pods": daemonset_pods,
        "operator": operator_availability,
        "operator_images": operator_images,
        "operator_images_canonical": True,
        "operator_contract_sha256": _stable_json_sha256(
            expected_operator
        ),
        "operator_pods": operator_pods,
        "relay": relay_availability,
        "relay_images": relay_images,
        "relay_images_canonical": True,
        "relay_contract_sha256": _stable_json_sha256(
            expected_relay
        ),
        "relay_pods": relay_pods,
        "kube_proxy_present": False,
    }


def _require_cilium_runtime_baseline(
    root: Path,
    source_commit: str,
    cilium_readback: dict[str, Any],
) -> dict[str, str]:
    path = root / "receipts/platform.json"
    try:
        platform = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeErrorEB(
            "Experiment-B status requires valid platform runtime baseline"
        ) from exc
    baseline = platform.get("cilium_runtime_image_ids")
    if (
        not isinstance(platform, dict)
        or platform.get("schema_version") != 1
        or platform.get("status") != "ready"
        or platform.get("source_commit") != source_commit
        or not isinstance(baseline, dict)
        or set(baseline) != {"daemonset", "operator", "relay"}
        or any(
            not isinstance(value, str)
            or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in baseline.values()
        )
    ):
        raise RuntimeErrorEB("Experiment-B Cilium runtime baseline is invalid")
    current = {
        "daemonset": cilium_readback.get("daemonset_pods", {}).get(
            "runtime_image_ids_sha256"
        ),
        "operator": cilium_readback.get("operator_pods", {}).get(
            "runtime_image_ids_sha256"
        ),
        "relay": cilium_readback.get("relay_pods", {}).get(
            "runtime_image_ids_sha256"
        ),
    }
    if current != baseline:
        raise RuntimeErrorEB(
            "live Cilium Pod runtime image IDs drifted from platform installation"
        )
    return {str(key): str(value) for key, value in baseline.items()}


def _stable_json_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()



def _network_policy_specs_from_bytes(
    manifest_bytes: bytes,
    namespace: str,
    context: str,
) -> dict[str, Any]:
    try:
        documents = list(
            yaml.safe_load_all(manifest_bytes.decode("utf-8"))
        )
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise RuntimeErrorEB(
            f"{context} NetworkPolicy contract is unreadable: {namespace}"
        ) from exc
    specs: dict[str, Any] = {}
    for document in documents:
        if not isinstance(document, dict) or document.get("kind") != "NetworkPolicy":
            continue
        metadata = document.get("metadata", {})
        spec = document.get("spec")
        if (
            not isinstance(metadata, dict)
            or metadata.get("namespace") != namespace
            or not isinstance(metadata.get("name"), str)
            or not isinstance(spec, dict)
        ):
            raise RuntimeErrorEB(
                f"{context} NetworkPolicy contract is invalid: {namespace}"
            )
        name = str(metadata["name"])
        if name in specs:
            raise RuntimeErrorEB(
                f"{context} NetworkPolicy contract contains duplicate: "
                f"{namespace}/{name}"
            )
        specs[name] = spec
    if not specs:
        raise RuntimeErrorEB(
            f"{context} NetworkPolicy contract is empty: {namespace}"
        )
    return specs


def _versioned_network_policy_specs(
    path: Path,
    namespace: str,
) -> dict[str, Any]:
    try:
        manifest_bytes = path.read_bytes()
    except OSError as exc:
        raise RuntimeErrorEB(
            f"versioned NetworkPolicy contract is unreadable: {namespace}"
        ) from exc
    return _network_policy_specs_from_bytes(
        manifest_bytes,
        namespace,
        "versioned",
    )


def _source_commit_network_policy_specs(
    source_commit: str,
    path: Path,
    namespace: str,
) -> dict[str, Any]:
    return _network_policy_specs_from_bytes(
        _git_blob_bytes(source_commit, path),
        namespace,
        "source-commit",
    )

def _require_live_runtime_contract(
    root: Path,
    config: dict[str, Any],
    source_commit: str | None = None,
) -> dict[str, Any]:
    runtime_binding = config.get("runtime_binding", {})
    expected_config_data = runtime_binding.get("config_map_data")
    expected_network_specs = runtime_binding.get("network_policy_specs")
    expected_cilium_specs = runtime_binding.get("cilium_network_policy_specs")
    data_network_policy_path = CLUSTER / "data/network-policy.yaml"
    expected_data_network_specs = (
        _versioned_network_policy_specs(
            data_network_policy_path,
            DATA_NAMESPACE,
        )
        if source_commit is None
        else _source_commit_network_policy_specs(
            source_commit,
            data_network_policy_path,
            DATA_NAMESPACE,
        )
    )
    if (
        not isinstance(expected_config_data, dict)
        or not isinstance(expected_network_specs, dict)
        or not isinstance(expected_cilium_specs, dict)
    ):
        raise RuntimeErrorEB("Experiment-B runtime contract configuration is invalid")

    config_map = _kubectl_json(
        root,
        ["-n", APP_NAMESPACE, "get", "configmap", "weltgewebe-runtime"],
    )
    config_metadata = config_map.get("metadata", {})
    live_config_data = config_map.get("data")
    if (
        not isinstance(config_metadata, dict)
        or config_metadata.get("namespace") != APP_NAMESPACE
        or config_metadata.get("name") != "weltgewebe-runtime"
        or config_metadata.get("deletionTimestamp") is not None
        or config_map.get("immutable") not in (None, False)
        or config_map.get("binaryData") not in (None, {})
        or not isinstance(live_config_data, dict)
        or live_config_data != expected_config_data
    ):
        raise RuntimeErrorEB("live Experiment-B runtime ConfigMap drifted")

    policy_items = _kubectl_json(
        root, ["-n", APP_NAMESPACE, "get", "networkpolicies"]
    ).get("items")
    if not isinstance(policy_items, list):
        raise RuntimeErrorEB("live Experiment-B NetworkPolicy inventory is invalid")
    live_network_specs: dict[str, Any] = {}
    for item in policy_items:
        if not isinstance(item, dict):
            raise RuntimeErrorEB("live Experiment-B NetworkPolicy inventory is invalid")
        metadata = item.get("metadata", {})
        spec = item.get("spec")
        if (
            not isinstance(metadata, dict)
            or metadata.get("namespace") != APP_NAMESPACE
            or not isinstance(metadata.get("name"), str)
            or metadata.get("deletionTimestamp") is not None
            or not isinstance(spec, dict)
        ):
            raise RuntimeErrorEB("live Experiment-B NetworkPolicy inventory is invalid")
        name = str(metadata["name"])
        if name in live_network_specs:
            raise RuntimeErrorEB("live Experiment-B NetworkPolicy inventory is duplicated")
        live_network_specs[name] = spec
    if live_network_specs != expected_network_specs:
        raise RuntimeErrorEB("live Experiment-B NetworkPolicy contract drifted")

    data_policy_items = _kubectl_json(
        root, ["-n", DATA_NAMESPACE, "get", "networkpolicies"]
    ).get("items")
    if not isinstance(data_policy_items, list):
        raise RuntimeErrorEB(
            "live Experiment-B data NetworkPolicy inventory is invalid"
        )
    live_data_network_specs: dict[str, Any] = {}
    for item in data_policy_items:
        if not isinstance(item, dict):
            raise RuntimeErrorEB(
                "live Experiment-B data NetworkPolicy inventory is invalid"
            )
        metadata = item.get("metadata", {})
        spec = item.get("spec")
        if (
            not isinstance(metadata, dict)
            or metadata.get("namespace") != DATA_NAMESPACE
            or not isinstance(metadata.get("name"), str)
            or metadata.get("deletionTimestamp") is not None
            or not isinstance(spec, dict)
        ):
            raise RuntimeErrorEB(
                "live Experiment-B data NetworkPolicy inventory is invalid"
            )
        name = str(metadata["name"])
        if name in live_data_network_specs:
            raise RuntimeErrorEB(
                "live Experiment-B data NetworkPolicy inventory is duplicated"
            )
        live_data_network_specs[name] = spec
    if live_data_network_specs != expected_data_network_specs:
        raise RuntimeErrorEB(
            "live Experiment-B data NetworkPolicy contract drifted"
        )

    cilium_items = _kubectl_json(
        root, ["-n", APP_NAMESPACE, "get", "ciliumnetworkpolicies"]
    ).get("items")
    if not isinstance(cilium_items, list):
        raise RuntimeErrorEB("live Experiment-B CiliumNetworkPolicy inventory is invalid")
    live_cilium_specs: dict[str, Any] = {}
    for item in cilium_items:
        if not isinstance(item, dict):
            raise RuntimeErrorEB(
                "live Experiment-B CiliumNetworkPolicy inventory is invalid"
            )
        metadata = item.get("metadata", {})
        spec = item.get("spec")
        if (
            not isinstance(metadata, dict)
            or metadata.get("namespace") != APP_NAMESPACE
            or not isinstance(metadata.get("name"), str)
            or metadata.get("deletionTimestamp") is not None
            or not isinstance(spec, dict)
        ):
            raise RuntimeErrorEB(
                "live Experiment-B CiliumNetworkPolicy inventory is invalid"
            )
        name = str(metadata["name"])
        if name in live_cilium_specs:
            raise RuntimeErrorEB(
                "live Experiment-B CiliumNetworkPolicy inventory is duplicated"
            )
        live_cilium_specs[name] = spec
    if live_cilium_specs != expected_cilium_specs:
        raise RuntimeErrorEB(
            "live Experiment-B CiliumNetworkPolicy contract drifted"
        )

    return {
        "config_map_data_sha256": _stable_json_sha256(live_config_data),
        "network_policy_specs_sha256": _stable_json_sha256(live_network_specs),
        "network_policy_names": sorted(live_network_specs),
        "data_network_policy_specs_sha256": _stable_json_sha256(
            live_data_network_specs
        ),
        "data_network_policy_names": sorted(live_data_network_specs),
        "cilium_network_policy_specs_sha256": _stable_json_sha256(
            live_cilium_specs
        ),
        "cilium_network_policy_names": sorted(live_cilium_specs),
        "temporary_model_egress_absent": (
            "commonthing-experiment-b-model-bootstrap-egress"
            not in live_network_specs
        ),
        "policy_specs_canonical": True,
    }


@_serialize_experiment_b_lifecycle
def status(root: Path) -> dict[str, Any]:
    release_path = root / "receipts/release.json"
    if not release_path.is_file():
        raise RuntimeErrorEB("Experiment-B status requires release receipt")
    release = json.loads(release_path.read_text(encoding="utf-8"))
    source_commit = str(release.get("source_commit", ""))
    if not COMMIT_RE.fullmatch(source_commit):
        raise RuntimeErrorEB("Experiment-B status release binding is not exact")
    receipt_path, attempt_path, attempt_started_at_unix_ms = (
        _begin_live_check_attempt(root, "status", source_commit)
    )
    if _current_protected_main_commit() != source_commit:
        raise RuntimeErrorEB("Experiment-B status release is not current protected main")
    config = _source_commit_config(source_commit)
    vm_create_path = root / "receipts/vm-create.json"
    try:
        vm_create = json.loads(vm_create_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeErrorEB("Experiment-B status requires valid vm-create.json") from exc
    _require_vm_create_receipt(vm_create, source_commit, config, root)
    vm_substrate = _live_vm_substrate(root, config)
    if vm_substrate != vm_create["substrate"]:
        raise RuntimeErrorEB("VM substrate drifted from creation receipt")
    status_target = _kubernetes_target_identity(root, source_commit)
    with _bound_kube_env(root, status_target, source_commit):
        k3s_runtime = _require_live_k3s_runtime(root, config, source_commit)
        toolchain_receipt = toolchain(root)
        tools = toolchain_receipt["tools"]
        env = kube_env(root)
        kubectl = tools["kubectl"]
        cilium_readback = _require_live_cilium_contract(root, config)
        cilium_readback["runtime_image_ids_baseline"] = (
            _require_cilium_runtime_baseline(root, source_commit, cilium_readback)
        )

        nodes = json.loads(run([kubectl, "get", "nodes", "-o", "json"], env=env).stdout)
        node_readback = _require_exact_k3s_node_inventory(
            nodes, str(config["kubernetes"]["version"])
        )
        kubelet = node_readback["kubelet_version"]
        os_image = node_readback["os_image"]

        flux_contract = _flux_bootstrap_contract(root, release)
        flux_controllers = _require_live_flux_controller_contract(
            root, toolchain_receipt
        )
        flux_runtime_baseline = _require_flux_runtime_baseline(
            root, source_commit, flux_controllers
        )
        source = _kubectl_json(
            root,
            ["-n", "flux-system", "get", "gitrepository", "commonthing-experiment-b"],
        )
        source_revision = _require_flux_source_revision(
            source,
            source_commit,
            flux_contract["source_spec"],
        )
        release_config_map_readback = _require_live_release_config_map(
            root,
            flux_contract["release_config_map"],
        )

        flux_items = _kubectl_json(
            root, ["-n", "flux-system", "get", "kustomizations"]
        ).get("items", [])
        flux_readback = _require_exact_flux_revision_ready(
            flux_items,
            source_commit,
            flux_contract["kustomization_specs"],
        )

        api = _kubectl_json(
            root, ["-n", APP_NAMESPACE, "get", "deployment", "weltgewebe-api"]
        )
        web = _kubectl_json(
            root, ["-n", APP_NAMESPACE, "get", "deployment", "weltgewebe-web"]
        )
        migration = _kubectl_json(
            root,
            [
                "-n", APP_NAMESPACE, "get", "job",
                MIGRATION_JOB_NAME,
            ],
        )
        application_pod_items = _kubectl_json(
            root,
            ["-n", APP_NAMESPACE, "get", "pods"],
        ).get("items")
        if not isinstance(application_pod_items, list) or any(
            not isinstance(item, dict)
            for item in application_pod_items
        ):
            raise RuntimeErrorEB(
                "live application Pod inventory is invalid"
            )
        api_pods = _pods_matching_labels(
            application_pod_items,
            {"app.kubernetes.io/name": "weltgewebe-api"},
        )
        web_pods = _pods_matching_labels(
            application_pod_items,
            {"app.kubernetes.io/name": "weltgewebe-web"},
        )
        migration_pods = _pods_matching_labels(
            application_pod_items,
            {
                "batch.kubernetes.io/job-name": MIGRATION_JOB_NAME,
            },
        )
        release_artifacts = _require_requested_release_artifacts(
            root,
            api,
            web,
            migration,
            migration_pods,
            str(release.get("api_digest", "")),
            str(release.get("web_digest", "")),
            int(config["semantic_search"]["api_replicas"]),
            int(config["runtime_binding"]["web_replicas"]),
            source_commit,
        )
        deployment_readback = release_artifacts["deployments"]
        namespace_security_readback = _require_live_namespace_security_contract(
            root,
            source_commit,
        )
        data_deployment_readback = _require_live_data_deployments(
            root,
            source_commit=source_commit,
        )
        data_service_readback = _require_live_data_services(
            root,
            source_commit,
        )
        api_containers = _container_images(api, "Experiment-B API Deployment")
        semantic = config["semantic_search"]
        if api_containers.get("ollama") != semantic["ollama_image"]:
            raise RuntimeErrorEB("live Ollama image does not match semantic-search pin")

        expected_api_pod_images = {
            "api": str(release_artifacts["images"]["api"]),
            "search-worker": str(release_artifacts["images"]["search_worker"]),
            "ollama": str(semantic["ollama_image"]),
        }
        expected_web_pod_images = {
            "web": str(release_artifacts["images"]["web"]),
        }
        pod_readback = {
            "weltgewebe-api": _require_running_pod_images(
                api_pods,
                "weltgewebe-api",
                int(config["semantic_search"]["api_replicas"]),
                expected_api_pod_images,
            ),
            "weltgewebe-web": _require_running_pod_images(
                web_pods,
                "weltgewebe-web",
                int(config["runtime_binding"]["web_replicas"]),
                expected_web_pod_images,
            ),
        }
        application_workloads = _require_live_application_workloads(
            root,
            release,
            {
                "weltgewebe-api": api,
                "weltgewebe-web": web,
            },
            {
                "weltgewebe-api": api_pods,
                "weltgewebe-web": web_pods,
            },
        )
        expected_application_pod_names = {
            pod_name
            for workload in application_workloads.values()
            for pod_name in workload["pod_names"]
        }
        expected_application_pod_names.update(
            release_artifacts["migration"]["pod_names"]
        )
        application_pod_inventory = (
            _require_exact_application_pod_inventory(
                application_pod_items,
                expected_application_pod_names,
            )
        )
        application_services = _require_live_application_services(root, release)
        application_service_accounts = (
            _require_live_application_service_accounts(root, release)
        )
        application_disruption_budgets = _require_live_application_pdbs(
            root,
            release,
        )
        semantic_provider = _semantic_provider_live_readback(root, source_commit)

        expected_secret_values = _expected_live_secret_values(root, source_commit)
        database_secret = _kubectl_json(
            root,
            [
                "-n", DATA_NAMESPACE, "get", "secret",
                "commonthing-experiment-b-database",
            ],
        )
        runtime_secret = _kubectl_json(
            root,
            ["-n", APP_NAMESPACE, "get", "secret", "weltgewebe-runtime"],
        )
        registry_secret = _kubectl_json(
            root,
            [
                "-n", APP_NAMESPACE, "get", "secret",
                "commonthing-experiment-b-registry",
            ],
        )
        _require_live_secret(
            database_secret,
            DATA_NAMESPACE,
            "commonthing-experiment-b-database",
            "Opaque",
            {"username", "database", "password"},
            expected_secret_values["database"],
        )
        _require_live_secret(
            runtime_secret,
            APP_NAMESPACE,
            "weltgewebe-runtime",
            "Opaque",
            {"database-url"},
            expected_secret_values["runtime"],
        )
        _require_live_secret(
            registry_secret,
            APP_NAMESPACE,
            "commonthing-experiment-b-registry",
            "kubernetes.io/dockerconfigjson",
            {".dockerconfigjson"},
            expected_secret_values["registry"],
        )
        secret_readback = {
            "database": _verified_secret_readback(
                DATA_NAMESPACE,
                "commonthing-experiment-b-database",
                "Opaque",
                {"username", "database", "password"},
            ),
            "runtime": _verified_secret_readback(
                APP_NAMESPACE,
                "weltgewebe-runtime",
                "Opaque",
                {"database-url"},
            ),
            "registry": _verified_secret_readback(
                APP_NAMESPACE,
                "commonthing-experiment-b-registry",
                "kubernetes.io/dockerconfigjson",
                {".dockerconfigjson"},
            ),
        }

        pvc_items = _kubectl_json(root, ["get", "pvc", "--all-namespaces"]).get("items", [])
        pvc_readback = _require_exact_healthy_pvcs(
            root,
            pvc_items,
            source_commit,
        )

        gateway = _kubectl_json(
            root, ["-n", APP_NAMESPACE, "get", "gateway", "commonthing-experiment-b"]
        )
        gateway_readback = _require_gateway_ready(gateway)

        httproute = _kubectl_json(
            root, ["-n", APP_NAMESPACE, "get", "httproute", "commonthing-experiment-b"]
        )
        httproute_readback = _require_httproute_ready(httproute)
        status_serving_runtime_before = _functional_serving_runtime_binding(
            root,
            source_commit,
        )
        status_serving_runtime_semantic_before = (
            _functional_serving_runtime_semantic_binding(
                status_serving_runtime_before
            )
        )
        with _guard_functional_service_endpoints(
            root,
            status_serving_runtime_before,
        ):
            status_serving_runtime_probe_start = (
                _functional_serving_runtime_binding(
                    root,
                    source_commit,
                )
            )
            status_serving_runtime_semantic_probe_start = (
                _functional_serving_runtime_semantic_binding(
                    status_serving_runtime_probe_start
                )
            )
            if (
                status_serving_runtime_semantic_probe_start
                != status_serving_runtime_semantic_before
            ):
                raise RuntimeErrorEB(
                    "application serving runtime changed before status Gateway probes"
                )
            gateway_data_plane = _gateway_data_plane_readback(
                root,
                source_commit,
            )
            status_serving_runtime_after = (
                _functional_serving_runtime_binding(
                    root,
                    source_commit,
                )
            )
            status_serving_runtime_semantic_after = (
                _functional_serving_runtime_semantic_binding(
                    status_serving_runtime_after
                )
            )
            if (
                status_serving_runtime_semantic_after
                != status_serving_runtime_semantic_probe_start
            ):
                raise RuntimeErrorEB(
                    "application serving runtime changed during status readback"
                )
        recovery_state = _final_recovery_state_readback(root, source_commit)
        runtime_contract_readback = _require_live_runtime_contract(
            root,
            config,
            source_commit,
        )
    _require_same_kubernetes_target(
        root,
        source_commit,
        status_target,
        "status live readback",
    )

    result = {
        "schema_version": 1,
        "status": "observed",
        "source_commit": source_commit,
        "kubernetes_target_sha256": _stable_json_sha256(status_target),
        "vm_create_sha256": sha256_file(vm_create_path),
        "vm_substrate": vm_substrate,
        "vm_ip": k3s_runtime["vm_ip"],
        "k3s_runtime": k3s_runtime,
        "node": node_readback["node"],
        "kubelet_version": kubelet,
        "node_ready": node_readback["ready"],
        "os_image": os_image,
        "cilium": cilium_readback,
        "runtime_contract": runtime_contract_readback,
        "flux_bootstrap_sha256": flux_contract["bootstrap_sha256"],
        "flux_source_revision": source_revision,
        "flux_release_config_map": release_config_map_readback,
        "flux_controllers": flux_controllers,
        "flux_runtime_image_ids_baseline": flux_runtime_baseline,
        "flux": flux_readback,
        "deployments": deployment_readback,
        "namespace_security": namespace_security_readback,
        "data_deployments": data_deployment_readback,
        "data_services": data_service_readback,
        "application_workloads": application_workloads,
        "application_pod_inventory": application_pod_inventory,
        "application_services": application_services,
        "application_service_accounts": application_service_accounts,
        "application_disruption_budgets": application_disruption_budgets,
        "pods": pod_readback,
        "images": {
            **release_artifacts["images"],
            "ollama": api_containers.get("ollama"),
        },
        "migration": release_artifacts["migration"],
        "migration_complete": release_artifacts["migration_complete"],
        "semantic_provider": semantic_provider,
        "secrets": secret_readback,
        "pvcs": pvc_readback,
        "gateway": gateway_readback,
        "gateway_programmed": True,
        "httproute": httproute_readback,
        "gateway_data_plane": gateway_data_plane,
        "gateway_serving_runtime": status_serving_runtime_semantic_after,
        "recovery_state": recovery_state,
        "kind_runtime": False,
        "staging_cell_runtime_controller": False,
    }
    atomic_json(receipt_path, result)
    _complete_live_check_attempt(
        attempt_path,
        receipt_path,
        source_commit,
        attempt_started_at_unix_ms,
        "pass",
    )
    return result


def _libvirt_resource_present(kind: str, name: str) -> bool:
    if kind == "domain":
        argv = ["virsh", "-c", LIBVIRT_URI, "list", "--all", "--name"]
    elif kind == "pool":
        argv = ["virsh", "-c", LIBVIRT_URI, "pool-list", "--all", "--name"]
    else:
        raise RuntimeErrorEB(f"unsupported libvirt resource kind: {kind}")
    result = run(argv, check=False)
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        raise RuntimeErrorEB(
            f"cannot prove libvirt {kind} state for {name}: {detail[-1000:]}"
        )
    return name in {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _libvirt_resource_uuid(kind: str, name: str) -> str:
    if kind == "domain":
        argv = ["virsh", "-c", LIBVIRT_URI, "domuuid", name]
    elif kind == "pool":
        argv = ["virsh", "-c", LIBVIRT_URI, "pool-uuid", name]
    else:
        raise RuntimeErrorEB(f"unsupported libvirt resource kind: {kind}")
    result = run(argv, check=False)
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        raise RuntimeErrorEB(
            f"cannot prove libvirt {kind} UUID for {name}: {detail[-1000:]}"
        )
    value = result.stdout.strip()
    try:
        parsed = str(uuid.UUID(value))
    except ValueError as exc:
        raise RuntimeErrorEB(
            f"libvirt {kind} UUID for {name} is invalid"
        ) from exc
    return parsed


def _retirement_attempt_path() -> Path:
    return RETIREMENT_RECEIPT.with_name(
        f"{RETIREMENT_RECEIPT.stem}-attempt{RETIREMENT_RECEIPT.suffix}"
    )


def _libvirt_volume_present(pool: str, name: str) -> bool:
    result = run(
        ["virsh", "-c", LIBVIRT_URI, "vol-list", pool],
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        raise RuntimeErrorEB(
            f"cannot prove libvirt volume state for {pool}/{name}: {detail[-1000:]}"
        )
    volume_names: set[str] = set()
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("-"):
            continue
        volume_names.add(stripped.split(None, 1)[0])
    return name in volume_names


def _require_teardown_state_root(root: Path) -> dict[str, Any]:
    root_identity = str(root.resolve())
    retirement_attempt = _retirement_attempt_path()
    if retirement_attempt.is_file():
        try:
            payload = json.loads(retirement_attempt.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeErrorEB(
                "Experiment-B teardown resume receipt is invalid"
            ) from exc
        domain_target = payload.get("domain_target") if isinstance(payload, dict) else None
        pool_target = payload.get("pool_target") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or payload.get("status") != "running"
            or payload.get("operation") != "teardown"
            or payload.get("state_root") != root_identity
            or payload.get("vm") != VM_NAME
            or payload.get("pool") != POOL_NAME
            or (
                domain_target is not None
                and (
                    not isinstance(domain_target, str)
                    or re.fullmatch(
                        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                        domain_target,
                    )
                    is None
                )
            )
            or (
                pool_target is not None
                and (
                    not isinstance(pool_target, str)
                    or re.fullmatch(
                        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                        pool_target,
                    )
                    is None
                )
            )
            or not isinstance(payload.get("evidence_receipts"), dict)
        ):
            raise RuntimeErrorEB(
                "Experiment-B teardown resume receipt does not own the VM resources"
            )
        return payload

    creation_path = root / "receipts/vm-create.json"
    attempt_path = root / "receipts/vm-create-attempt.json"
    if creation_path.is_file():
        path = creation_path
        allowed_statuses = {"created"}
    elif attempt_path.is_file():
        path = attempt_path
        allowed_statuses = {"running"}
    else:
        raise RuntimeErrorEB(
            "Experiment-B teardown has no state-root ownership receipt"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeErrorEB(
            "Experiment-B teardown state-root ownership receipt is invalid"
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("status") not in allowed_statuses
        or payload.get("state_root") != root_identity
        or payload.get("vm") != VM_NAME
        or payload.get("pool") != POOL_NAME
    ):
        raise RuntimeErrorEB(
            "Experiment-B teardown state root does not own the VM resources"
        )
    return payload


def _require_teardown_live_identity(
    root: Path,
    ownership: dict[str, Any],
) -> dict[str, Any]:
    domain_present = _libvirt_resource_present("domain", VM_NAME)
    pool_present = _libvirt_resource_present("pool", POOL_NAME)
    pool_target_stat: os.stat_result | None = None
    if pool_present:
        pool_fd = _open_libvirt_pool_target(create=False)
        try:
            pool_target_stat = os.fstat(pool_fd)
        finally:
            os.close(pool_fd)
    if ownership.get("operation") == "teardown":
        domain_target = ownership.get("domain_target")
        pool_target = ownership.get("pool_target")
        if domain_present:
            if not isinstance(domain_target, str):
                raise RuntimeErrorEB(
                    "teardown resume has no verified domain identity"
                )
            if _libvirt_resource_uuid("domain", VM_NAME) != domain_target:
                raise RuntimeErrorEB(
                    "teardown resume domain UUID drifted from the verified attempt"
                )
        if pool_present:
            if not isinstance(pool_target, str):
                raise RuntimeErrorEB(
                    "teardown resume has no verified pool identity"
                )
            if _libvirt_resource_uuid("pool", POOL_NAME) != pool_target:
                raise RuntimeErrorEB(
                    "teardown resume pool UUID drifted from the verified attempt"
                )
        return {
            "identity_verified": True,
            "domain_present": domain_present,
            "pool_present": pool_present,
            "domain_target": domain_target,
            "pool_target": pool_target,
            "substrate_sha256": ownership.get("substrate_sha256"),
        }

    if ownership.get("status") == "running":
        domain_target = ownership.get("domain_target")
        pool_target = ownership.get("pool_target")
        pool_device = ownership.get("pool_target_device")
        pool_inode = ownership.get("pool_target_inode")

        if pool_present:
            if (
                type(pool_device) is not int
                or pool_device < 0
                or type(pool_inode) is not int
                or pool_inode < 0
            ):
                raise RuntimeErrorEB(
                    "interrupted creation has no verified pool directory identity"
                )
            try:
                if pool_target_stat is None:
                    raise RuntimeErrorEB(
                        "interrupted creation pool identity cannot be read"
                    )
                pool_xml = _libvirt_xml("pool-dumpxml", POOL_NAME)
            except (OSError, ET.ParseError, AttributeError) as exc:
                raise RuntimeErrorEB(
                    "interrupted creation pool identity cannot be read"
                ) from exc
            if (
                pool_target_stat.st_dev != pool_device
                or pool_target_stat.st_ino != pool_inode
                or pool_xml.findtext("target/path") != str(POOL_TARGET)
            ):
                raise RuntimeErrorEB(
                    "interrupted creation pool directory identity drifted"
                )
            live_pool_uuid = _libvirt_resource_uuid("pool", POOL_NAME)
            if pool_target is not None:
                try:
                    normalized_pool_target = str(uuid.UUID(str(pool_target)))
                except ValueError as exc:
                    raise RuntimeErrorEB(
                        "interrupted creation pool UUID is invalid"
                    ) from exc
                if live_pool_uuid != normalized_pool_target:
                    raise RuntimeErrorEB(
                        "interrupted creation pool UUID drifted"
                    )
            pool_target = live_pool_uuid

        if domain_present:
            try:
                normalized_domain_target = str(uuid.UUID(str(domain_target)))
            except ValueError as exc:
                raise RuntimeErrorEB(
                    "interrupted creation has no valid domain UUID"
                ) from exc
            if _libvirt_resource_uuid("domain", VM_NAME) != normalized_domain_target:
                raise RuntimeErrorEB(
                    "interrupted creation domain UUID drifted"
                )
            if not pool_present:
                raise RuntimeErrorEB(
                    "interrupted creation domain has no owned storage pool"
                )
            volume_device = ownership.get("volume_device")
            volume_inode = ownership.get("volume_inode")
            base_image_sha256 = ownership.get("base_image_sha256")
            if (
                type(volume_device) is not int
                or volume_device < 0
                or type(volume_inode) is not int
                or volume_inode < 0
                or not isinstance(base_image_sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", base_image_sha256) is None
            ):
                raise RuntimeErrorEB(
                    "interrupted creation has no verified disk/backing identity"
                )
            try:
                volume_stat = (POOL_TARGET / VOLUME_NAME).stat()
                domain_definition = _vm_definition(
                    _libvirt_xml("dumpxml", VM_NAME)
                )
            except (OSError, ET.ParseError, AttributeError) as exc:
                raise RuntimeErrorEB(
                    "interrupted creation disk identity cannot be read"
                ) from exc
            if (
                volume_stat.st_dev != volume_device
                or volume_stat.st_ino != volume_inode
                or domain_definition.get("disk_path")
                != str(POOL_TARGET / VOLUME_NAME)
                or _libvirt_volume_sha256(root, BASE_VOLUME)
                != base_image_sha256
            ):
                raise RuntimeErrorEB(
                    "interrupted creation disk/backing identity drifted"
                )
            domain_target = normalized_domain_target

        return {
            "identity_verified": True,
            "domain_present": domain_present,
            "pool_present": pool_present,
            "domain_target": domain_target if domain_present else None,
            "pool_target": pool_target if pool_present else None,
            "substrate_sha256": ownership.get("substrate_sha256"),
        }

    if ownership.get("status") == "created":
        source_commit = ownership.get("source_commit")
        if not isinstance(source_commit, str) or not COMMIT_RE.fullmatch(source_commit):
            raise RuntimeErrorEB("teardown creation receipt has no exact source commit")
        config = load_config(source_commit)
        _require_vm_create_receipt(ownership, source_commit, config, root)
        if not domain_present or not pool_present:
            raise RuntimeErrorEB(
                "teardown cannot prove the complete live creation substrate"
            )
        live = _live_vm_substrate(root, config)
        if live != ownership.get("substrate"):
            raise RuntimeErrorEB(
                "teardown live VM/pool/disk identity drifted from vm-create.json"
            )
        return {
            "identity_verified": True,
            "domain_present": True,
            "pool_present": True,
            "domain_target": str(live["uuid"]),
            "pool_target": str(live["pool_uuid"]),
            "substrate_sha256": _stable_json_sha256(live),
        }

    if domain_present or pool_present:
        raise RuntimeErrorEB(
            "teardown refuses same-named libvirt resources from an interrupted "
            "creation because their UUID/backing identity was never committed"
        )
    return {
        "identity_verified": True,
        "domain_present": False,
        "pool_present": False,
        "domain_target": None,
        "pool_target": None,
        "substrate_sha256": None,
    }


@_serialize_experiment_b_lifecycle
def teardown(root: Path) -> dict[str, Any]:
    ownership = _require_teardown_state_root(root)
    live_identity = _require_teardown_live_identity(root, ownership)
    if ownership.get("operation") == "teardown":
        evidence_hashes = {
            str(name): str(value)
            for name, value in ownership["evidence_receipts"].items()
        }
    else:
        evidence_hashes: dict[str, str] = {}
        receipts_dir = root / "receipts"
        if receipts_dir.is_dir():
            for path in sorted(receipts_dir.glob("*.json")):
                evidence_hashes[path.name] = sha256_file(path)
        ownership = {
            "schema_version": 1,
            "status": "running",
            "operation": "teardown",
            "state_root": ownership["state_root"],
            "vm": VM_NAME,
            "pool": POOL_NAME,
            "domain_target": live_identity["domain_target"],
            "pool_target": live_identity["pool_target"],
            "substrate_sha256": live_identity["substrate_sha256"],
            "evidence_receipts": evidence_hashes,
        }
        atomic_json(_retirement_attempt_path(), ownership)

    if live_identity["domain_present"]:
        domain_target = str(live_identity["domain_target"])
        _retire_domain_before_storage(
            domain_target,
            "Experiment-B teardown",
        )

    volume_absence = {
        VOLUME_NAME: False,
        BASE_VOLUME: False,
    }
    if live_identity["pool_present"]:
        pool_target = str(live_identity["pool_target"])
        _cleanup_pool_after_domain_retirement(
            pool_target,
            "Experiment-B teardown",
        )
        for volume_name in volume_absence:
            volume_absence[volume_name] = True
    else:
        for volume_name in volume_absence:
            volume_absence[volume_name] = True

    if _libvirt_resource_present("domain", VM_NAME):
        raise RuntimeErrorEB("Experiment-B VM still exists after teardown")
    if _libvirt_resource_present("pool", POOL_NAME):
        raise RuntimeErrorEB("Experiment-B storage pool still exists after teardown")

    volume_paths = {
        VOLUME_NAME: POOL_TARGET / VOLUME_NAME,
        BASE_VOLUME: POOL_TARGET / BASE_VOLUME,
    }
    for volume_name, path in volume_paths.items():
        if path.exists():
            raise RuntimeErrorEB(
                f"Experiment-B volume path still exists after teardown: {volume_name}"
            )
    if POOL_TARGET.exists():
        if any(POOL_TARGET.iterdir()):
            raise RuntimeErrorEB("Experiment-B libvirt pool directory is not empty after teardown")
        POOL_TARGET.rmdir()

    if root.exists():
        shutil.rmtree(root, ignore_errors=False)
    result = {
        "schema_version": 1,
        "status": "retired",
        "vm": VM_NAME,
        "pool": POOL_NAME,
        "state_root": ownership["state_root"],
        "live_identity_verified": live_identity["identity_verified"],
        "substrate_sha256": live_identity["substrate_sha256"],
        "volumes_absent": volume_absence,
        "volume_paths_absent": {
            name: not path.exists() for name, path in volume_paths.items()
        },
        "pool_target_removed": not POOL_TARGET.exists(),
        "state_removed": not root.exists(),
        "evidence_receipts": evidence_hashes,
    }
    atomic_json(RETIREMENT_RECEIPT, result)
    _retirement_attempt_path().unlink(missing_ok=True)
    return result


def _source_commit_python_module(
    source_commit: str,
    path: Path,
    module_name: str,
) -> Any:
    if COMMIT_RE.fullmatch(source_commit) is None:
        raise RuntimeErrorEB("source-commit Python module requires an exact commit")
    try:
        relative = path.relative_to(ROOT)
    except ValueError as exc:
        raise RuntimeErrorEB("source-commit Python module escapes repository root") from exc
    payload = _git_blob_bytes(source_commit, path)
    try:
        source = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeErrorEB(
            f"source-commit Python module is not UTF-8: {relative.as_posix()}"
        ) from exc
    filename = (
        f"/__experiment_b_source_commit__/{source_commit}/"
        f"{relative.as_posix()}"
    )
    module = types.ModuleType(module_name)
    module.__file__ = filename
    module.__package__ = module_name.rpartition(".")[0]
    missing = object()
    previous = sys.modules.get(module_name, missing)
    sys.modules[module_name] = module
    try:
        exec(compile(source, filename, "exec"), module.__dict__)
    except Exception as exc:
        raise RuntimeErrorEB(
            f"source-commit Python module failed to load: {relative.as_posix()}"
        ) from exc
    finally:
        if previous is missing:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
    return module


def _source_bound_contract(source_commit: str) -> Any:
    module = _source_commit_python_module(
        source_commit,
        CONTRACT_HELPER,
        "_experiment_b_source_contract",
    )
    required = (
        "validate_config",
        "render_cloud_init",
        "render_bootstrap_from_template",
    )
    if any(not callable(getattr(module, name, None)) for name in required):
        raise RuntimeErrorEB(
            "source-commit Experiment-B contract helper is incomplete"
        )
    module.ROOT = ROOT
    module.CONFIG_PATH = CONFIG_PATH
    module.BOOTSTRAP_TEMPLATE = BOOTSTRAP_TEMPLATE
    module.ContractError = ContractError
    return module


def _source_bound_bootstrap_tools(source_commit: str) -> Any:
    module = _source_commit_python_module(
        source_commit,
        BOOTSTRAP_TOOLS_HELPER,
        "_experiment_b_source_bootstrap_tools",
    )
    if not callable(getattr(module, "install", None)):
        raise RuntimeErrorEB(
            "source-commit bootstrap-tools helper is incomplete"
        )
    module.ROOT = ROOT
    module.LOCK_PATH = TOOLCHAIN_LOCK_PATH
    return module


def _source_bound_live_binding(source_commit: str) -> Any:
    return _source_commit_python_module(
        source_commit,
        ROOT / "scripts/performance/api_runtime_live_binding.py",
        "_experiment_b_source_api_runtime_live_binding",
    )


def _performance_modules(source_commit: str) -> tuple[Any, Any]:
    live_binding = _source_bound_live_binding(source_commit)
    domain_scale = _source_commit_python_module(
        source_commit,
        DOMAIN_SCALE,
        "_experiment_b_source_domain_scale",
    )

    scripts_package = types.ModuleType("scripts")
    scripts_package.__path__ = []
    performance_package = types.ModuleType("scripts.performance")
    performance_package.__path__ = []
    performance_package.api_runtime_live_binding = live_binding
    scripts_package.performance = performance_package

    bindings = {
        "scripts": scripts_package,
        "scripts.performance": performance_package,
        "scripts.performance.api_runtime_live_binding": live_binding,
    }
    missing = object()
    previous = {
        name: sys.modules.get(name, missing)
        for name in bindings
    }
    try:
        sys.modules.update(bindings)
        evidence = _source_commit_python_module(
            source_commit,
            ROOT / "scripts/performance/api_runtime_evidence.py",
            "_experiment_b_source_api_runtime_evidence",
        )
    finally:
        for name, value in previous.items():
            if value is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
    return evidence, domain_scale


def _source_bound_dataset_binding(
    source_commit: str,
    manifest_path: Path,
    contract: dict[str, Any],
    domain_scale: Any,
) -> dict[str, Any]:
    proof = contract.get("dataset_proof")
    if not isinstance(proof, dict):
        raise RuntimeErrorEB("T048 dataset proof contract is invalid")
    expected_generator = DOMAIN_SCALE.relative_to(ROOT).as_posix()
    expected_config = DOMAIN_SCALE_CONFIG.relative_to(ROOT).as_posix()
    if (
        proof.get("generator") != expected_generator
        or proof.get("config") != expected_config
        or not isinstance(proof.get("profile"), str)
    ):
        raise RuntimeErrorEB("T048 dataset proof authority paths drifted")

    config_bytes = _git_blob_bytes(source_commit, DOMAIN_SCALE_CONFIG)
    with _sealed_snapshot_fd(
        config_bytes,
        "source-commit T048 domain-scale config",
    ) as config_fd:
        config_path = Path(f"/proc/self/fd/{config_fd}")
        try:
            _config, manifest = domain_scale.load_bound_manifest(
                manifest_path,
                config_path,
            )
        except Exception as exc:
            error_type = getattr(domain_scale, "DomainScaleError", None)
            if error_type is not None and isinstance(exc, error_type):
                raise RuntimeErrorEB(
                    "T048 dataset manifest is not bound to source-commit config"
                ) from exc
            raise
        with tempfile.TemporaryDirectory(
            prefix="experiment-b-t048-canonical-"
        ) as temporary:
            try:
                expected_manifest = domain_scale.generate_fixture(
                    config_path,
                    str(proof["profile"]),
                    Path(temporary) / "fixture",
                )
            except Exception as exc:
                error_type = getattr(domain_scale, "DomainScaleError", None)
                if error_type is not None and isinstance(exc, error_type):
                    raise RuntimeErrorEB(
                        "source-commit T048 canonical fixture generation failed"
                    ) from exc
                raise
        if not isinstance(expected_manifest, dict) or manifest != expected_manifest:
            raise RuntimeErrorEB(
                "T048 retained fixture is not canonical source-commit generator output"
            )
    if manifest.get("profile") != proof["profile"]:
        raise RuntimeErrorEB("T048 dataset manifest profile drifted")
    files = manifest.get("files")
    counts = manifest.get("counts")
    if not isinstance(files, dict) or not isinstance(counts, dict):
        raise RuntimeErrorEB("T048 dataset manifest binding is incomplete")
    return {
        "manifest_sha256": sha256_file(manifest_path),
        "generator": manifest["generator"],
        "config_sha256": manifest["config_sha256"],
        "database_schema": manifest["database_schema"],
        "profile": manifest["profile"],
        "counts": dict(counts),
        "files": {
            "nodes": dict(files["nodes"]),
            "edges": dict(files["edges"]),
        },
    }


def _kubectl(
    root: Path,
    arguments: list[str],
    *,
    input_text: str | None = None,
    check: bool = True,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    kubectl = toolchain(root)["tools"]["kubectl"]
    return run(
        [kubectl, *arguments],
        input_text=input_text,
        env=kube_env(root),
        check=check,
        timeout=timeout,
    )


def _kubectl_json(root: Path, arguments: list[str]) -> dict[str, Any]:
    result = _kubectl(root, [*arguments, "-o", "json"])
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeErrorEB(f"kubectl JSON readback failed for {arguments!r}") from exc
    if not isinstance(value, dict):
        raise RuntimeErrorEB(f"kubectl JSON readback is not an object for {arguments!r}")
    return value


def _endpoint_slice_collection_json(
    root: Path,
    namespace: str,
    service_name: str,
) -> dict[str, Any]:
    if not isinstance(namespace, str) or not namespace:
        raise RuntimeErrorEB("EndpointSlice namespace is invalid")
    if not isinstance(service_name, str) or not service_name:
        raise RuntimeErrorEB("EndpointSlice Service name is invalid")
    namespace_path = urllib.parse.quote(namespace, safe="")
    query = urllib.parse.urlencode(
        {"labelSelector": f"kubernetes.io/service-name={service_name}"}
    )
    result = _kubectl(
        root,
        [
            "get",
            "--raw",
            (
                f"/apis/discovery.k8s.io/v1/namespaces/"
                f"{namespace_path}/endpointslices?{query}"
            ),
        ],
    )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeErrorEB(
            f"EndpointSlice raw JSON readback failed: {namespace}/{service_name}"
        ) from exc
    if not isinstance(value, dict):
        raise RuntimeErrorEB(
            f"EndpointSlice raw JSON readback is not an object: "
            f"{namespace}/{service_name}"
        )
    return value


def _gateway_collection_json(root: Path) -> dict[str, Any]:
    namespace_path = urllib.parse.quote(APP_NAMESPACE, safe="")
    query = urllib.parse.urlencode(
        {"fieldSelector": "metadata.name=commonthing-experiment-b"}
    )
    result = _kubectl(
        root,
        [
            "get",
            "--raw",
            (
                f"/apis/gateway.networking.k8s.io/v1/namespaces/"
                f"{namespace_path}/gateways?{query}"
            ),
        ],
    )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeErrorEB("Gateway raw JSON readback failed") from exc
    if not isinstance(value, dict):
        raise RuntimeErrorEB("Gateway raw JSON readback is not an object")
    return value


def _httproute_collection_json(root: Path) -> dict[str, Any]:
    namespace_path = urllib.parse.quote(APP_NAMESPACE, safe="")
    result = _kubectl(
        root,
        [
            "get",
            "--raw",
            (
                f"/apis/gateway.networking.k8s.io/v1/namespaces/"
                f"{namespace_path}/httproutes"
            ),
        ],
    )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeErrorEB("HTTPRoute raw JSON readback failed") from exc
    if not isinstance(value, dict):
        raise RuntimeErrorEB("HTTPRoute raw JSON readback is not an object")
    return value


def _database_client_identity(root: Path) -> tuple[str, str]:
    database_path = root / "secrets/database.json"
    if not database_path.is_file() or database_path.is_symlink():
        raise RuntimeErrorEB(
            "Experiment-B PostgreSQL client requires database Secret source material"
        )
    try:
        database = json.loads(database_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeErrorEB(
            "Experiment-B PostgreSQL client requires valid database Secret source material"
        ) from exc
    if (
        not isinstance(database, dict)
        or not isinstance(database.get("username"), str)
        or not database["username"]
        or not isinstance(database.get("database"), str)
        or not database["database"]
    ):
        raise RuntimeErrorEB(
            "Experiment-B PostgreSQL client database identity is invalid"
        )
    return database["username"], database["database"]


def _verified_database_client_identity(
    root: Path,
    source_commit: str,
) -> tuple[str, str]:
    expected = _expected_live_secret_values(root, source_commit)
    database = expected["database"]
    return (
        database["username"].decode("utf-8"),
        database["database"].decode("utf-8"),
    )


def _database_client_argv(
    executable: str,
    database_identity: tuple[str, str],
) -> list[str]:
    username, database = database_identity
    if not username or not database:
        raise RuntimeErrorEB(
            "Experiment-B PostgreSQL client database identity is invalid"
        )
    return [
        executable,
        "-U",
        username,
        "-d",
        database,
    ]


def _postgres_runtime_binding_identity(
    binding: dict[str, Any],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for field in (
        "container_id",
        "contract_sha256",
        "pod_contract_sha256",
        "runtime_image_ids_sha256",
    ):
        value = binding.get(field)
        if not isinstance(value, str) or not value:
            raise RuntimeErrorEB(
                f"PostgreSQL runtime binding field is invalid: {field}"
            )
        result[field] = value
    return result



def _run_bound_container_command(
    root: Path,
    source_commit: str,
    container_id: str,
    command: list[str],
    *,
    input_bytes: bytes = b"",
    timeout: int = 900,
    context: str,
) -> bytes:
    if (
        COMMIT_RE.fullmatch(source_commit) is None
        or not isinstance(container_id, str)
        or re.fullmatch(
            r"containerd://[0-9a-f]{64}",
            container_id,
        )
        is None
        or not isinstance(command, list)
        or not command
        or any(
            not isinstance(value, str) or not value
            for value in command
        )
        or not isinstance(input_bytes, bytes)
        or not isinstance(context, str)
        or not context
    ):
        raise RuntimeErrorEB(
            f"{context or 'bound container'} invocation is invalid"
        )
    raw_container_id = container_id.removeprefix(
        "containerd://"
    )
    config = _source_commit_config(source_commit)
    expected_k3s_sha256 = config.get("kubernetes", {}).get(
        "binary_sha256"
    )
    if (
        not isinstance(expected_k3s_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_k3s_sha256)
        is None
    ):
        raise RuntimeErrorEB(
            "source-commit k3s binary digest is invalid"
        )
    target = _kubernetes_target_identity(root, source_commit)
    _require_same_kubernetes_target(
        root,
        source_commit,
        target,
        f"{context} pre-exec",
    )

    runtime_argv = [
        "/proc/self/fd/9",
        "crictl",
        "exec",
        "-i",
        raw_container_id,
        *command,
    ]
    parameter_trim = "$" + "{actual%% *}"
    script = (
        "exec 9</usr/local/bin/k3s || exit 95; "
        "actual=\"$(sha256sum /proc/self/fd/9)\" || exit 95; "
        + "actual=\"" + parameter_trim + "\"; "
        + f"[ \"$actual\" = {shlex.quote(expected_k3s_sha256)} ] "
        + "|| exit 96; "
        + f"exec {shlex.join(runtime_argv)}"
    )
    remote_command = shlex.join(
        ["sudo", "/bin/sh", "-c", script]
    )
    ssh_command = [
        *ssh_argv(root, target["vm_ip"]),
        remote_command,
    ]
    with _bound_ssh_command(ssh_command) as (
        bound_ssh_command,
        ssh_pass_fds,
    ):
        result = subprocess.run(
            bound_ssh_command,
            cwd=ROOT,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            pass_fds=_bound_subprocess_pass_fds(ssh_pass_fds),
        )
    if result.returncode != 0:
        detail = result.stderr.decode(
            "utf-8", "replace"
        )[-3000:]
        raise RuntimeErrorEB(
            f"{context} failed ({result.returncode}): {detail}"
        )
    _require_same_kubernetes_target(
        root,
        source_commit,
        target,
        f"{context} post-exec",
    )
    return result.stdout


def _run_bound_postgres_client(
    root: Path,
    source_commit: str,
    binding: dict[str, Any],
    command: list[str],
    *,
    input_bytes: bytes = b"",
    timeout: int = 900,
) -> bytes:
    if (
        COMMIT_RE.fullmatch(source_commit) is None
        or not isinstance(command, list)
        or not command
        or any(
            not isinstance(value, str) or not value
            for value in command
        )
        or not isinstance(input_bytes, bytes)
    ):
        raise RuntimeErrorEB(
            "bound PostgreSQL client invocation is invalid"
        )
    expected_identity = _postgres_runtime_binding_identity(binding)
    current = _require_postgres_runtime_binding(
        root,
        source_commit,
    )
    if (
        _postgres_runtime_binding_identity(current)
        != expected_identity
    ):
        raise RuntimeErrorEB(
            "PostgreSQL runtime changed before bound client execution"
        )
    return _run_bound_container_command(
        root,
        source_commit,
        expected_identity["container_id"],
        command,
        input_bytes=input_bytes,
        timeout=timeout,
        context="bound PostgreSQL client",
    )


def _run_bound_postgres_sql(
    root: Path,
    source_commit: str,
    binding: dict[str, Any],
    database_identity: tuple[str, str],
    sql: str,
    *,
    tuples_only: bool = True,
    timeout: int = 900,
) -> str:
    command = [
        *_database_client_argv("psql", database_identity),
        "-v",
        "ON_ERROR_STOP=1",
    ]
    if tuples_only:
        command.extend(["-At"])
    raw = _run_bound_postgres_client(
        root,
        source_commit,
        binding,
        command,
        input_bytes=sql.encode("utf-8"),
        timeout=timeout,
    )
    try:
        return raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise RuntimeErrorEB(
            "bound PostgreSQL client output is not UTF-8"
        ) from exc


def _run_bound_nats_client(
    root: Path,
    source_commit: str,
    binding: dict[str, Any],
    command: list[str],
    *,
    input_bytes: bytes = b"",
    timeout: int = 900,
) -> bytes:
    if (
        COMMIT_RE.fullmatch(source_commit) is None
        or not isinstance(command, list)
        or not command
        or any(
            not isinstance(value, str) or not value
            for value in command
        )
        or not isinstance(input_bytes, bytes)
    ):
        raise RuntimeErrorEB(
            "bound NATS client invocation is invalid"
        )
    expected_identity = _nats_runtime_binding_identity(binding)
    current = _require_nats_runtime_binding(
        root,
        source_commit,
    )
    if (
        _nats_runtime_binding_identity(current)
        != expected_identity
    ):
        raise RuntimeErrorEB(
            "NATS runtime changed before bound client execution"
        )
    return _run_bound_container_command(
        root,
        source_commit,
        expected_identity["container_id"],
        command,
        input_bytes=input_bytes,
        timeout=timeout,
        context="bound NATS client",
    )

def _psql(
    root: Path,
    sql: str,
    *,
    tuples_only: bool = True,
    database_identity: tuple[str, str] | None = None,
    postgres_pod_name: str | None = None,
) -> str:
    identity = (
        _database_client_identity(root)
        if database_identity is None
        else database_identity
    )
    target = "deployment/postgres"
    if postgres_pod_name is not None:
        if (
            not isinstance(postgres_pod_name, str)
            or not postgres_pod_name
            or "/" in postgres_pod_name
        ):
            raise RuntimeErrorEB(
                "Experiment-B PostgreSQL client Pod identity is invalid"
            )
        target = postgres_pod_name
    argv = [
        "-n", DATA_NAMESPACE,
        "exec", "-i", target, "--",
        *_database_client_argv("psql", identity),
        "-v", "ON_ERROR_STOP=1",
    ]
    if tuples_only:
        argv.extend(["-At"])
    return _kubectl(root, argv, input_text=sql, timeout=600).stdout.strip()

def _run_input_file(
    argv: list[str],
    source: Path,
    *,
    env: dict[str, str] | None = None,
    timeout: int = 1200,
) -> None:
    with source.open("rb") as handle:
        result = subprocess.run(
            argv,
            cwd=ROOT,
            stdin=handle,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=timeout,
            check=False,
            pass_fds=_bound_subprocess_pass_fds(),
        )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace")[-3000:]
        raise RuntimeErrorEB(
            f"streamed command failed ({result.returncode}): {argv[0]}: {detail}"
        )


def _run_binary_to_file(
    argv: list[str],
    destination: Path,
    *,
    env: dict[str, str] | None = None,
    timeout: int = 1200,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.partial")
    temporary.unlink(missing_ok=True)
    try:
        with temporary.open("wb") as output:
            result = subprocess.run(
                argv,
                cwd=ROOT,
                stdout=output,
                stderr=subprocess.PIPE,
                env=env,
                timeout=timeout,
                check=False,
                pass_fds=_bound_subprocess_pass_fds(),
            )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", "replace")[-3000:]
            raise RuntimeErrorEB(
                f"binary capture failed ({result.returncode}): {argv[0]}: {detail}"
            )
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _write_streamed_fixture_sql(
    load_sql: Path,
    nodes_csv: Path,
    edges_csv: Path,
    destination: Path,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with load_sql.open("r", encoding="utf-8") as source, destination.open(
        "w", encoding="utf-8", newline="\n"
    ) as output:
        for line in source:
            if line.startswith("\\copy ") and "domain_nodes" in line:
                prefix, suffix = line.rstrip("\n").split(" FROM ", 1)
                _old_path, options = suffix.split(" WITH ", 1)
                output.write(f"{prefix} FROM STDIN WITH {options}\n")
                with nodes_csv.open("r", encoding="utf-8") as rows:
                    shutil.copyfileobj(rows, output)
                output.write("\\.\n")
            elif line.startswith("\\copy ") and "domain_edges" in line:
                prefix, suffix = line.rstrip("\n").split(" FROM ", 1)
                _old_path, options = suffix.split(" WITH ", 1)
                output.write(f"{prefix} FROM STDIN WITH {options}\n")
                with edges_csv.open("r", encoding="utf-8") as rows:
                    shutil.copyfileobj(rows, output)
                output.write("\\.\n")
            else:
                output.write(line)


def _t048_canonical_visibility(fixture: dict[str, Any]) -> str:
    kind = fixture.get("kind")
    if not isinstance(kind, str) or not kind:
        raise RuntimeErrorEB("T048 fixture node has no canonical kind")
    return "public" if kind == "Projekt" else "hidden"


def _t048_expected_projection_row(
    fixture: dict[str, Any],
    generation_id: str,
    dimension: int,
) -> dict[str, Any]:
    node_id = fixture.get("id")
    kind = fixture.get("kind")
    title = fixture.get("title")
    payload = fixture.get("payload")
    if (
        not isinstance(node_id, str)
        or not node_id
        or not isinstance(kind, str)
        or not kind
        or not isinstance(title, str)
        or not title
        or not isinstance(payload, dict)
        or not isinstance(dimension, int)
        or isinstance(dimension, bool)
        or dimension < 1
    ):
        raise RuntimeErrorEB("T048 fixture projection input is not canonical")
    visibility = _t048_canonical_visibility(fixture)
    is_public = visibility == "public"
    if is_public:
        raw_tags = payload.get("tags", [])
        if not isinstance(raw_tags, list) or any(
            not isinstance(tag, str) for tag in raw_tags
        ):
            raise RuntimeErrorEB("T048 public fixture tags are not canonical")
        summary = payload.get("summary")
        if summary is None:
            searchable_text = title
        elif isinstance(summary, str) and summary:
            searchable_text = summary
        else:
            raise RuntimeErrorEB("T048 public fixture summary is not canonical")
        projection_kind = kind
        projection_title = title
        tags = raw_tags
        language = "de"
        status = "active"
        visibility_scopes = ["public"]
        semantic_state = "ready"
        content_sha256 = T048_PUBLIC_CONTENT_SHA256
    else:
        projection_kind = T048_REDACTED_TEXT
        projection_title = T048_REDACTED_TEXT
        tags = []
        searchable_text = T048_REDACTED_TEXT
        language = "und"
        status = "hidden"
        visibility_scopes = []
        semantic_state = "unavailable"
        content_sha256 = T048_HIDDEN_CONTENT_SHA256
    return {
        "generation_id": generation_id,
        "node_id": node_id,
        "source_version": 1,
        "source_revision": "node-1",
        "content_sha256": content_sha256,
        "title": projection_title,
        "tags": tags,
        "searchable_text": searchable_text,
        "language": language,
        "kind": projection_kind,
        "status": status,
        "visibility_scopes": visibility_scopes,
        "semantic_state": semantic_state,
        "embedding_canonical": True,
    }


def _t048_fixture_edge_rows(
    manifest_path: Path,
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    files = manifest.get("files")
    counts = manifest.get("counts")
    if not isinstance(files, dict) or not isinstance(counts, dict):
        raise RuntimeErrorEB("T048 fixture manifest is missing canonical files/counts")
    edge_file = files.get("edges")
    edge_count = counts.get("edges")
    if (
        not isinstance(edge_file, dict)
        or not isinstance(edge_count, int)
        or isinstance(edge_count, bool)
        or edge_count < 1
    ):
        raise RuntimeErrorEB("T048 fixture manifest has no canonical edge binding")
    name = edge_file.get("name")
    expected_sha256 = edge_file.get("sha256")
    if (
        not isinstance(name, str)
        or not name
        or Path(name).name != name
        or not isinstance(expected_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
    ):
        raise RuntimeErrorEB("T048 fixture manifest edge binding is malformed")
    edge_path = manifest_path.parent / name
    if not edge_path.is_file() or sha256_file(edge_path) != expected_sha256:
        raise RuntimeErrorEB("T048 fixture edge CSV does not match its manifest digest")

    expected_header = (
        "id",
        "source_id",
        "target_id",
        "edge_kind",
        "created_at",
        "payload",
    )
    rows: list[dict[str, Any]] = []
    try:
        with edge_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != expected_header:
                raise RuntimeErrorEB("T048 fixture edge CSV header is not canonical")
            for row in reader:
                rows.append(
                    {
                        "id": row["id"],
                        "source_id": row["source_id"],
                        "target_id": row["target_id"],
                        "edge_kind": row["edge_kind"],
                        "created_at": row["created_at"],
                        "payload": json.loads(row["payload"]),
                    }
                )
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        raise RuntimeErrorEB("cannot parse canonical T048 edge fixture") from exc
    if len(rows) != edge_count:
        raise RuntimeErrorEB(
            "T048 fixture edge CSV row count does not match its manifest"
        )
    return rows


def _t048_live_fixture_binding(
    root: Path,
    manifest: Path,
    generation_id: str,
    source_commit: str,
    *,
    postgres_binding: dict[str, Any] | None = None,
    database_identity: tuple[str, str] | None = None,
) -> dict[str, Any]:
    live_binding = _source_bound_live_binding(source_commit)
    bound_postgres = (
        _require_postgres_runtime_binding(root, source_commit)
        if postgres_binding is None
        else postgres_binding
    )
    bound_database_identity = (
        _database_client_identity(root)
        if database_identity is None
        else database_identity
    )

    def bound_psql(sql: str) -> str:
        return _run_bound_postgres_sql(
            root,
            source_commit,
            bound_postgres,
            bound_database_identity,
            sql,
        )

    _manifest, fixture_rows = live_binding._manifest_and_fixture(manifest)
    db_rows = live_binding._json_lines(
        bound_psql(
            r"""
SELECT json_build_object(
  'id', id,
  'kind', kind,
  'title', title,
  'lat', lat,
  'lon', lon,
  'created_at', to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
  'updated_at', to_char(updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
  'payload', payload,
  'search_visibility', search_visibility
)::text
FROM domain_nodes
ORDER BY id;
""",
        ),
        "Experiment-B domain_nodes query",
    )
    canonical_fixture_rows = [
        {**row, "search_visibility": _t048_canonical_visibility(row)}
        for row in fixture_rows
    ]
    fixture_sha = live_binding._rows_sha256(canonical_fixture_rows)
    database_sha = live_binding._rows_sha256(db_rows)
    if len(db_rows) != len(canonical_fixture_rows) or database_sha != fixture_sha:
        raise RuntimeErrorEB(
            "live domain_nodes content does not match the deterministic T048 fixture"
        )

    fixture_edge_rows = _t048_fixture_edge_rows(manifest, _manifest)
    database_edge_rows = live_binding._json_lines(
        bound_psql(
            r"""
SELECT json_build_object(
  'id', id,
  'source_id', source_id,
  'target_id', target_id,
  'edge_kind', edge_kind,
  'created_at', to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
  'payload', payload
)::text
FROM domain_edges
ORDER BY id;
""",
        ),
        "Experiment-B domain_edges query",
    )
    fixture_edges_sha = live_binding._rows_sha256(fixture_edge_rows)
    database_edges_sha = live_binding._rows_sha256(database_edge_rows)
    if (
        len(database_edge_rows) != len(fixture_edge_rows)
        or database_edges_sha != fixture_edges_sha
    ):
        raise RuntimeErrorEB(
            "live domain_edges content does not match the deterministic T048 fixture"
        )

    version_rows = live_binding._json_lines(
        bound_psql(
            r"""
SELECT json_build_object(
  'node_id', node_id,
  'source_version', source_version,
  'source_revision', source_revision,
  'deleted', deleted_at IS NOT NULL
)::text
FROM search_node_versions
ORDER BY node_id;
""",
        ),
        "Experiment-B search_node_versions query",
    )
    expected_version_rows = [
        {
            "node_id": row["id"],
            "source_version": 1,
            "source_revision": "node-1",
            "deleted": False,
        }
        for row in fixture_rows
    ]
    fixture_versions_sha = live_binding._rows_sha256(expected_version_rows)
    database_versions_sha = live_binding._rows_sha256(version_rows)
    if (
        len(version_rows) != len(expected_version_rows)
        or database_versions_sha != fixture_versions_sha
    ):
        raise RuntimeErrorEB(
            "live search_node_versions content does not match the deterministic T048 fixture"
        )

    generation_literal = live_binding._sql_literal(generation_id)
    generation_rows = live_binding._json_lines(
        bound_psql(
            f"""
SELECT json_build_object(
  'generation_id', generation_id,
  'provider', provider,
  'model_id', model_id,
  'model_revision', model_revision,
  'runtime_identity', runtime_identity,
  'dimension', dimension,
  'document_revision', document_revision,
  'normalization_revision', normalization_revision,
  'ranking_revision', ranking_revision,
  'state', state,
  'expected_nodes', expected_nodes,
  'completed_nodes', completed_nodes
)::text
FROM search_index_generations
WHERE generation_id = {generation_literal} AND state = 'active';
""",
        ),
        "Experiment-B active search generation query",
    )
    if len(generation_rows) != 1:
        raise RuntimeErrorEB("Experiment-B requires exactly one active T048 generation")
    generation = generation_rows[0]
    semantic = _source_commit_config(source_commit)["semantic_search"]
    expected_generation_identity = {
        "generation_id": generation_id,
        "provider": semantic["provider"],
        "model_id": semantic["model_id"],
        "model_revision": semantic["model_revision"],
        "runtime_identity": semantic["runtime_identity"],
        "dimension": int(semantic["dimension"]),
        "document_revision": T048_DOCUMENT_REVISION,
        "normalization_revision": T048_NORMALIZATION_REVISION,
        "ranking_revision": T048_RANKING_REVISION,
        "state": "active",
    }
    if any(
        generation.get(key) != value
        for key, value in expected_generation_identity.items()
    ):
        raise RuntimeErrorEB(
            "Experiment-B active T048 generation identity is not canonical"
        )
    dimension = int(semantic["dimension"])

    projection_rows = live_binding._json_lines(
        bound_psql(
            f"""
SELECT json_build_object(
  'generation_id', p.generation_id,
  'node_id', p.node_id,
  'source_version', p.source_version,
  'source_revision', p.source_revision,
  'content_sha256', p.content_sha256,
  'title', p.title,
  'tags', p.tags,
  'searchable_text', p.searchable_text,
  'language', p.language,
  'kind', p.kind,
  'status', p.status,
  'visibility_scopes', p.visibility_scopes,
  'semantic_state', p.semantic_state,
  'embedding_canonical',
    CASE
      WHEN n.search_visibility = 'public'
      THEN p.embedding = array_fill(0.0::DOUBLE PRECISION, ARRAY[g.dimension])
      ELSE p.embedding IS NULL
    END
)::text
FROM search_node_projections p
JOIN domain_nodes n ON n.id = p.node_id
JOIN search_index_generations g ON g.generation_id = p.generation_id
WHERE p.generation_id = {generation_literal}
ORDER BY p.node_id;
""",
        ),
        "Experiment-B active search projection query",
    )
    expected_nodes = generation.get("expected_nodes")
    completed_nodes = generation.get("completed_nodes")
    if (
        len(projection_rows) < 1
        or expected_nodes != len(projection_rows)
        or completed_nodes != len(projection_rows)
    ):
        raise RuntimeErrorEB("Experiment-B active T048 search generation is incomplete")

    fixture_by_id = {row["id"]: row for row in fixture_rows}
    expected_projection_rows: list[dict[str, Any]] = []
    for projection in projection_rows:
        node_id = projection.get("node_id")
        fixture = fixture_by_id.get(node_id)
        if fixture is None:
            raise RuntimeErrorEB(
                f"Experiment-B search projection {node_id!r} is absent from fixture"
            )
        expected_projection_rows.append(
            _t048_expected_projection_row(
                fixture,
                generation_id,
                dimension,
            )
        )
    projection_sha = live_binding._rows_sha256(projection_rows)
    expected_projection_sha = live_binding._rows_sha256(expected_projection_rows)
    if projection_sha != expected_projection_sha:
        raise RuntimeErrorEB(
            "live search projection content does not match the deterministic T048 fixture"
        )
    return {
        "manifest_sha256": sha256_file(manifest),
        "domain_nodes_count": len(db_rows),
        "fixture_nodes_content_sha256": fixture_sha,
        "database_nodes_content_sha256": database_sha,
        "domain_edges_count": len(database_edge_rows),
        "fixture_edges_content_sha256": fixture_edges_sha,
        "database_edges_content_sha256": database_edges_sha,
        "search_node_versions_count": len(version_rows),
        "fixture_versions_content_sha256": fixture_versions_sha,
        "database_versions_content_sha256": database_versions_sha,
        "generation_id": generation_id,
        "expected_nodes": int(expected_nodes),
        "completed_nodes": int(completed_nodes),
        "active_projection_count": len(projection_rows),
        "fixture_projection_content_sha256": expected_projection_sha,
        "database_projection_content_sha256": projection_sha,
    }


def _validated_t048_fixture_receipt(
    root: Path,
    source_commit: str,
) -> dict[str, Any]:
    if not COMMIT_RE.fullmatch(source_commit):
        raise RuntimeErrorEB("T048 fixture validation source commit is not exact")
    receipt_path = root / "receipts/t048-fixture.json"
    if not receipt_path.is_file():
        raise RuntimeErrorEB("T048 load proof requires a seeded fixture receipt")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeErrorEB("T048 fixture receipt is not valid JSON") from exc
    if (
        not isinstance(receipt, dict)
        or receipt.get("status") != "loaded"
        or receipt.get("source_commit") != source_commit
    ):
        raise RuntimeErrorEB("T048 fixture receipt is not bound to the load-proof source")

    manifest = root / "performance/fixture/manifest.json"
    if receipt.get("manifest") != str(manifest) or not manifest.is_file():
        raise RuntimeErrorEB("T048 fixture receipt does not name the canonical manifest")
    if receipt.get("manifest_sha256") != sha256_file(manifest):
        raise RuntimeErrorEB("T048 fixture receipt manifest digest is stale")

    evidence, domain_scale = _performance_modules(source_commit)
    policy, _policy_sha256 = _source_bound_performance_policy(
        source_commit
    )
    contract_section = evidence.api_runtime_section(policy)
    canonical_binding = _source_bound_dataset_binding(
        source_commit,
        manifest,
        contract_section,
        domain_scale,
    )
    if (
        canonical_binding.get("manifest_sha256")
        != receipt.get("manifest_sha256")
        or canonical_binding.get("profile") != receipt.get("profile")
    ):
        raise RuntimeErrorEB(
            "T048 fixture receipt does not match source-commit generator output"
        )

    generation_id = receipt.get("generation_id")
    expected_generation = str(
        _source_commit_config(source_commit)["semantic_search"]["generation_id"]
    )
    if (
        not isinstance(generation_id, str)
        or generation_id != expected_generation
    ):
        raise RuntimeErrorEB("T048 fixture receipt generation is not current")
    current_live_binding = _t048_live_fixture_binding(
        root,
        manifest,
        generation_id,
        source_commit,
    )
    if receipt.get("live_binding") != current_live_binding:
        raise RuntimeErrorEB(
            "T048 fixture receipt does not match current live fixture contents"
        )
    return receipt


@_serialize_experiment_b_lifecycle
def seed_t048_fixture(root: Path) -> dict[str, Any]:
    _invalidate_receipts(root, FIXTURE_ATTEMPT_INVALIDATES)
    release_path = root / "receipts/release.json"
    if not release_path.is_file():
        raise RuntimeErrorEB("T048 fixture load requires an applied release receipt")
    release = json.loads(release_path.read_text(encoding="utf-8"))
    source_commit = release.get("source_commit")
    if (
        not isinstance(source_commit, str)
        or not COMMIT_RE.fullmatch(source_commit)
        or _current_protected_main_commit() != source_commit
    ):
        raise RuntimeErrorEB("T048 fixture release is not current protected main")
    config = _source_commit_config(source_commit)
    _require_kubernetes_target_binding(root, source_commit)
    fixture_target = _kubernetes_target_identity(root, source_commit)
    with (
        _bound_kube_env(root, fixture_target, source_commit),
        ExitStack() as performance_stack,
    ):
        performance_fd = _open_performance_directory(root)
        performance_stack.callback(os.close, performance_fd)
        postgres_binding = _require_postgres_runtime_binding(
            root,
            source_commit,
        )
        database_identity = _database_client_identity(root)

        def bound_psql(
            sql: str,
            *,
            tuples_only: bool = True,
            timeout: int = 900,
        ) -> str:
            return _run_bound_postgres_sql(
                root,
                source_commit,
                postgres_binding,
                database_identity,
                sql,
                tuples_only=tuples_only,
                timeout=timeout,
            )

        evidence, domain_scale = _performance_modules(source_commit)
        policy, _policy_sha256 = _source_bound_performance_policy(
            source_commit
        )
        contract_section = evidence.api_runtime_section(policy)
        proof = contract_section["dataset_proof"]
        profile = str(proof["profile"])
        canonical_manifest = root / "performance/fixture/manifest.json"
        evidence_dir = Path(f"/proc/self/fd/{performance_fd}")
        fixture = evidence_dir / "fixture"
        manifest = fixture / "manifest.json"
        if not manifest.is_file():
            if fixture.exists():
                raise RuntimeErrorEB(
                    "partial T048 fixture directory exists; refusing implicit replacement"
                )
            config_bytes = _git_blob_bytes(source_commit, DOMAIN_SCALE_CONFIG)
            with _sealed_snapshot_fd(
                config_bytes,
                "source-commit T048 domain-scale config",
            ) as config_fd:
                try:
                    domain_scale.generate_fixture(
                        Path(f"/proc/self/fd/{config_fd}"),
                        profile,
                        fixture,
                    )
                except domain_scale.DomainScaleError as exc:
                    raise RuntimeErrorEB(
                        "source-commit T048 fixture generator failed"
                    ) from exc
        binding = _source_bound_dataset_binding(
            source_commit,
            manifest,
            contract_section,
            domain_scale,
        )
        counts = binding["counts"]
        node_count = int(counts["nodes"])
        edge_count = int(counts["edges"])

        existing_nodes = int(bound_psql("SELECT count(*) FROM domain_nodes;"))
        existing_edges = int(bound_psql("SELECT count(*) FROM domain_edges;"))
        generation_id = str(config["semantic_search"]["generation_id"])
        existing_generation = int(
            bound_psql("SELECT count(*) FROM search_index_generations "
                f"WHERE generation_id = '{generation_id}';",
            )
        )
        receipt_path = root / "receipts/t048-fixture.json"

        def emit_receipt() -> dict[str, Any]:
            observed_nodes = int(bound_psql("SELECT count(*) FROM domain_nodes;"))
            observed_edges = int(bound_psql("SELECT count(*) FROM domain_edges;"))
            public_nodes = int(
                bound_psql("SELECT count(*) FROM domain_nodes WHERE search_visibility='public';",
                )
            )
            observed_projections = int(
                bound_psql("SELECT count(*) FROM search_node_projections "
                    f"WHERE generation_id = '{generation_id}';",
                )
            )
            active_generation = int(
                bound_psql("SELECT count(*) FROM search_index_generations "
                    f"WHERE generation_id = '{generation_id}' AND state = 'active';",
                )
            )
            pending_jobs = int(
                bound_psql("SELECT count(*) FROM search_projection_jobs "
                    f"WHERE generation_id = '{generation_id}' AND state <> 'done';",
                )
            )
            if (
                observed_nodes != node_count
                or observed_edges != edge_count
                or public_nodes < 1
                or observed_projections != node_count
                or active_generation != 1
                or pending_jobs != 0
            ):
                raise RuntimeErrorEB(
                    "T048 fixture/search projection counts do not match the canonical manifest"
                )
            live_binding = _t048_live_fixture_binding(
                root,
                manifest,
                generation_id,
                source_commit,
                postgres_binding=postgres_binding,
                database_identity=database_identity,
            )
            if (
                _postgres_runtime_binding_identity(
                    _require_postgres_runtime_binding(
                        root,
                        source_commit,
                    )
                )
                != _postgres_runtime_binding_identity(postgres_binding)
            ):
                raise RuntimeErrorEB(
                    "PostgreSQL runtime changed during T048 fixture load"
                )
            _require_same_kubernetes_target(
                root,
                source_commit,
                fixture_target,
                "T048 fixture success receipt",
            )
            receipt = {
                "schema_version": 1,
                "status": "loaded",
                "source_commit": source_commit,
                "kubernetes_target_sha256": _stable_json_sha256(fixture_target),
                "profile": profile,
                "manifest": str(canonical_manifest),
                "manifest_sha256": binding["manifest_sha256"],
                "nodes": observed_nodes,
                "edges": observed_edges,
                "public_semantic_nodes": public_nodes,
                "search_projections": observed_projections,
                "generation_id": generation_id,
                "generation_state": "active",
                "projection_mode": "synthetic-canonical-t048",
                "pending_projection_jobs": pending_jobs,
                "live_binding": live_binding,
                "production_data_used": False,
            }
            atomic_json(receipt_path, receipt)
            return receipt

        if (
            existing_nodes == node_count
            and existing_edges == edge_count
            and existing_generation == 1
            and not receipt_path.is_file()
        ):
            return emit_receipt()
        if existing_nodes or existing_edges:
            raise RuntimeErrorEB(
                "target database is not empty enough for a fresh T048 fixture load"
            )

        files = binding["files"]
        nodes_csv = fixture / str(files["nodes"]["name"])
        edges_csv = fixture / str(files["edges"]["name"])
        config_bytes = _git_blob_bytes(source_commit, DOMAIN_SCALE_CONFIG)
        with (
            _sealed_snapshot_fd(
                config_bytes,
                "source-commit T048 domain-scale config",
            ) as config_fd,
            _verified_snapshot_fd(
                nodes_csv,
                str(files["nodes"]["sha256"]),
                "source-commit T048 nodes fixture",
            ) as nodes_fd,
            _verified_snapshot_fd(
                edges_csv,
                str(files["edges"]["sha256"]),
                "source-commit T048 edges fixture",
            ) as edges_fd,
            tempfile.TemporaryFile() as load_output,
        ):
            try:
                domain_scale.render_load_sql(
                    manifest,
                    Path(f"/proc/self/fd/{load_output.fileno()}"),
                    Path(f"/proc/self/fd/{config_fd}"),
                )
            except domain_scale.DomainScaleError as exc:
                raise RuntimeErrorEB(
                    "source-commit T048 load SQL generation failed"
                ) from exc
            load_size = os.fstat(load_output.fileno()).st_size
            load_payload = os.pread(load_output.fileno(), load_size, 0)
            if len(load_payload) != load_size or not load_payload:
                raise RuntimeErrorEB("source-commit T048 load SQL snapshot is invalid")
            with (
                _sealed_snapshot_fd(
                    load_payload,
                    "source-commit T048 load SQL",
                ) as load_fd,
                tempfile.TemporaryFile() as streamed_output,
            ):
                _write_streamed_fixture_sql(
                    Path(f"/proc/self/fd/{load_fd}"),
                    Path(f"/proc/self/fd/{nodes_fd}"),
                    Path(f"/proc/self/fd/{edges_fd}"),
                    Path(f"/proc/self/fd/{streamed_output.fileno()}"),
                )
                streamed_size = os.fstat(streamed_output.fileno()).st_size
                streamed_payload = os.pread(
                    streamed_output.fileno(),
                    streamed_size,
                    0,
                )
                if len(streamed_payload) != streamed_size or not streamed_payload:
                    raise RuntimeErrorEB(
                        "source-commit T048 streamed SQL snapshot is invalid"
                    )
                with _sealed_snapshot_fd(
                    streamed_payload,
                    "source-commit T048 streamed SQL",
                ) as streamed_fd:
                    sealed_streamed_payload = os.pread(
                        streamed_fd,
                        streamed_size,
                        0,
                    )
                    if sealed_streamed_payload != streamed_payload:
                        raise RuntimeErrorEB(
                            "source-commit T048 sealed streamed SQL changed"
                        )
                    _run_bound_postgres_client(
                        root,
                        source_commit,
                        postgres_binding,
                        [
                            *_database_client_argv(
                                "psql",
                                database_identity,
                            ),
                            "-v",
                            "ON_ERROR_STOP=1",
                        ],
                        input_bytes=sealed_streamed_payload,
                        timeout=1800,
                    )

        semantic = config["semantic_search"]
        seed_sql = f"""
    BEGIN;
    SELECT pg_advisory_xact_lock(
        hashtextextended('weltgewebe.search.generation.activation', 0)
    );
    DO $$
    DECLARE
        generation_count BIGINT;
    BEGIN
        IF EXISTS (SELECT 1 FROM domain_nodes)
           OR EXISTS (SELECT 1 FROM domain_edges) THEN
            RAISE EXCEPTION 'Experiment-B T048 target domain changed before seed lock';
        END IF;
        IF EXISTS (SELECT 1 FROM search_node_versions)
           OR EXISTS (SELECT 1 FROM search_projection_jobs)
           OR EXISTS (SELECT 1 FROM search_node_projections) THEN
            RAISE EXCEPTION 'Experiment-B T048 search ledger is not empty before fresh seed';
        END IF;
        SELECT count(*) INTO generation_count FROM search_index_generations;
        IF generation_count > 1 THEN
            RAISE EXCEPTION 'Experiment-B T048 has unexpected pre-seed generations';
        END IF;
        IF generation_count = 1 THEN
            IF NOT EXISTS (
                SELECT 1
                  FROM search_index_generations
                 WHERE generation_id = '{semantic["generation_id"]}'
                   AND provider = '{semantic["provider"]}'
                   AND model_id = '{semantic["model_id"]}'
                   AND model_revision = '{semantic["model_revision"]}'
                   AND runtime_identity = '{semantic["runtime_identity"]}'
                   AND dimension = {int(semantic["dimension"])}
                   AND document_revision = '{T048_DOCUMENT_REVISION}'
                   AND normalization_revision = '{T048_NORMALIZATION_REVISION}'
                   AND ranking_revision = '{T048_RANKING_REVISION}'
                   AND state = 'building'
                   AND expected_nodes = 0
                   AND completed_nodes = 0
                   AND activated_at IS NULL
            ) THEN
                RAISE EXCEPTION 'Experiment-B T048 pre-seed generation is not the empty worker generation';
            END IF;
            DELETE FROM search_index_generations
             WHERE generation_id = '{semantic["generation_id"]}';
        END IF;
    END
    $$;
    INSERT INTO search_index_generations (
        generation_id, provider, model_id, model_revision, runtime_identity,
        dimension, document_revision, normalization_revision, ranking_revision,
        state, expected_nodes
    ) VALUES (
        '{semantic["generation_id"]}',
        '{semantic["provider"]}',
        '{semantic["model_id"]}',
        '{semantic["model_revision"]}',
        '{semantic["runtime_identity"]}',
        {int(semantic["dimension"])},
        '{T048_DOCUMENT_REVISION}',
        '{T048_NORMALIZATION_REVISION}',
        '{T048_RANKING_REVISION}',
        'building',
        (SELECT count(*) FROM weltgewebe_perf.domain_nodes)
    );

    INSERT INTO domain_nodes (
        id, kind, title, lat, lon, created_at, updated_at, payload, search_visibility
    )
    SELECT id, kind, title, lat, lon, created_at, updated_at, payload,
           CASE WHEN kind = 'Projekt' THEN 'public' ELSE 'hidden' END
      FROM weltgewebe_perf.domain_nodes
     ORDER BY id;

    INSERT INTO domain_edges (id, source_id, target_id, edge_kind, created_at, payload)
    SELECT id, source_id, target_id, edge_kind, created_at, payload
      FROM weltgewebe_perf.domain_edges
     ORDER BY id;

    INSERT INTO search_node_projections (
        generation_id, node_id, source_version, source_revision, content_sha256,
        title, tags, searchable_text, language, kind, status, visibility_scopes,
        semantic_state, embedding
    )
    SELECT
        g.generation_id,
        n.id,
        v.source_version,
        v.source_revision,
        CASE
            WHEN n.search_visibility = 'public' THEN '{T048_PUBLIC_CONTENT_SHA256}'
            ELSE '{T048_HIDDEN_CONTENT_SHA256}'
        END,
        CASE WHEN n.search_visibility = 'public' THEN n.title ELSE '[nicht öffentlich]' END,
        CASE
            WHEN n.search_visibility = 'public'
            THEN ARRAY(SELECT jsonb_array_elements_text(n.payload -> 'tags'))
            ELSE '{{}}'::TEXT[]
        END,
        CASE
            WHEN n.search_visibility = 'public'
            THEN coalesce(n.payload ->> 'summary', n.title)
            ELSE '[nicht öffentlich]'
        END,
        CASE WHEN n.search_visibility = 'public' THEN 'de' ELSE 'und' END,
        CASE WHEN n.search_visibility = 'public' THEN n.kind ELSE '[nicht öffentlich]' END,
        CASE WHEN n.search_visibility = 'public' THEN 'active' ELSE 'hidden' END,
        CASE
            WHEN n.search_visibility = 'public' THEN ARRAY['public']::TEXT[]
            ELSE '{{}}'::TEXT[]
        END,
        CASE WHEN n.search_visibility = 'public' THEN 'ready' ELSE 'unavailable' END,
        CASE
            WHEN n.search_visibility = 'public'
            THEN array_fill(0.0::DOUBLE PRECISION, ARRAY[g.dimension])
            ELSE NULL
        END
      FROM domain_nodes n
      JOIN search_node_versions v ON v.node_id = n.id
      CROSS JOIN search_index_generations g
     WHERE g.state = 'building'
     ORDER BY n.id;

    UPDATE search_projection_jobs
       SET state = 'done', completed_at = clock_timestamp()
     WHERE generation_id = '{semantic["generation_id"]}';

    UPDATE search_index_generations
       SET expected_nodes = (SELECT count(*) FROM domain_nodes),
           completed_nodes = (
               SELECT count(*) FROM search_node_versions WHERE deleted_at IS NULL
           )
     WHERE generation_id = '{semantic["generation_id"]}';

    DO $$
    BEGIN
        IF NOT weltgewebe_search_generation_activation_ready(
            '{semantic["generation_id"]}'
        ) THEN
            RAISE EXCEPTION 'Experiment-B T048 search generation is not activation-ready';
        END IF;
    END
    $$;
    SELECT weltgewebe_activate_search_generation('{semantic["generation_id"]}');
    COMMIT;
    """
        bound_psql(seed_sql, tuples_only=False)
        return emit_receipt()


def _k6_image_binding(source_commit: str) -> tuple[str, str]:
    workflow_bytes = _git_blob_bytes(source_commit, K6_WORKFLOW)
    try:
        text = workflow_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeErrorEB("canonical T048 workflow is not UTF-8") from exc
    match = re.search(
        r"(?m)^\s*K6_IMAGE:\s*(grafana/k6@sha256:[0-9a-f]{64})\s*$",
        text,
    )
    if match is None:
        raise RuntimeErrorEB("canonical T048 workflow has no digest-bound K6_IMAGE")
    return match.group(1), hashlib.sha256(workflow_bytes).hexdigest()


def _source_bound_performance_policy(
    source_commit: str,
) -> tuple[dict[str, Any], str]:
    policy_bytes = _git_blob_bytes(source_commit, PERFORMANCE_POLICY)
    try:
        parsed = json.loads(policy_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeErrorEB(
            "source-bound T048 performance policy is not valid UTF-8 JSON"
        ) from exc
    if (
        not isinstance(parsed, dict)
        or parsed.get("contract_id") != "weltgewebe-performance-v1"
        or not isinstance(parsed.get("measurements"), dict)
    ):
        raise RuntimeErrorEB("source-bound T048 performance policy is invalid")
    return parsed, hashlib.sha256(policy_bytes).hexdigest()


def _source_bound_k6_workload(source_commit: str) -> tuple[str, str]:
    workload_bytes = _git_blob_bytes(source_commit, K6_WORKLOAD)
    try:
        text = workload_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeErrorEB("source-bound T048 k6 workload is not UTF-8") from exc
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("import "):
            continue
        match = re.search(
            r"(?:from\s+)?[\"']([^\"']+)[\"']\s*;?\s*$",
            stripped,
        )
        if match is None:
            raise RuntimeErrorEB(
                "source-bound T048 k6 workload uses unsupported import syntax"
            )
        module = match.group(1)
        if module != "k6" and not module.startswith("k6/"):
            raise RuntimeErrorEB(
                "source-bound T048 k6 workload imports an unbound module"
            )
    if re.search(r"\b(?:open|require|import)\s*\(", text):
        raise RuntimeErrorEB(
            "source-bound T048 k6 workload performs unbound runtime loading"
        )
    return text, hashlib.sha256(workload_bytes).hexdigest()


def _reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])


def _http_read(url: str, *, timeout: int = 10) -> tuple[int, bytes, float]:
    started = time.perf_counter()
    request = urllib.request.Request(
        url, headers={"Accept": "application/json,text/html,text/plain,*/*"}
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read()
            status_code = int(response.status)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        status_code = int(exc.code)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return status_code, body, elapsed_ms


def _prime_t048_search_metric(base_url: str, search_query: str) -> None:
    if not isinstance(search_query, str) or not search_query.strip():
        raise RuntimeErrorEB("canonical T048 search query is invalid")
    query = urllib.parse.urlencode({"q": search_query, "limit": 5})
    status, _body, _elapsed = _http_read(
        f"{base_url.rstrip('/')}/search?{query}"
    )
    if status != 200:
        raise RuntimeErrorEB("T048 /search warm-up failed")



_T048_READY_COMPONENTS = ("database", "nats", "event_chain", "policy")


def _t048_readiness_diagnostics(summary: dict[str, Any]) -> dict[str, Any]:
    """Classify fixed-label readiness 503s without changing load PASS/FAIL."""
    metrics = summary.get("metrics")
    if not isinstance(metrics, dict):
        raise RuntimeErrorEB("T048 readiness diagnostics have no k6 metrics")

    def counter(name: str, *, required: bool = False) -> int:
        metric = metrics.get(name)
        if metric is None and not required:
            return 0  # k6 omits Counter series that never received a sample
        values = metric.get("values") if isinstance(metric, dict) else None
        raw = values.get("count") if isinstance(values, dict) else None
        if (
            isinstance(raw, bool)
            or not isinstance(raw, (int, float))
            or not math.isfinite(raw)
            or raw < 0
            or not float(raw).is_integer()
        ):
            raise RuntimeErrorEB(f"T048 readiness counter {name} is invalid or missing")
        return int(raw)

    samples = counter("t048_ready_samples", required=True)
    status_503 = counter("t048_ready_http_503")
    unclassified = counter("t048_ready_unclassified")
    failed = {
        name: counter(f"t048_ready_check_false_{name}")
        for name in _T048_READY_COMPONENTS
    }
    if (
        samples <= 0
        or status_503 > samples
        or unclassified > status_503
        or any(value + unclassified > status_503 for value in failed.values())
        or sum(failed.values()) + unclassified < status_503
    ):
        raise RuntimeErrorEB("T048 readiness diagnostic counters are inconsistent")
    return {
        "sample_count": samples,
        "http_503_count": status_503,
        "checks_false": failed,
        "unclassified_503_count": unclassified,
    }


def _wait_http_200(
    url: str,
    process: subprocess.Popen[Any] | None = None,
    *,
    consecutive: int = 1,
) -> None:
    if not isinstance(consecutive, int) or not 1 <= consecutive <= 3:
        raise RuntimeErrorEB("HTTP readiness streak must be between 1 and 3")
    streak = 0
    for _ in range(120 if consecutive > 1 else 60):
        if process is not None and process.poll() is not None:
            raise RuntimeErrorEB("port-forward exited before the target became ready")
        try:
            status_code, _body, _elapsed = _http_read(url, timeout=2)
        except urllib.error.URLError:
            streak = 0
        else:
            streak = streak + 1 if status_code == 200 else 0
            if streak >= consecutive:
                return
        time.sleep(1)
    raise RuntimeErrorEB(f"HTTP target did not become ready: {url}")


def _start_api_port_forward(
    root: Path,
    source_commit: str,
    expected_binding: dict[str, str],
) -> tuple[subprocess.Popen[Any], int, Any, Any]:
    pod_name = expected_binding.get("pod_name")
    if (
        COMMIT_RE.fullmatch(source_commit) is None
        or not isinstance(pod_name, str)
        or not pod_name
    ):
        raise RuntimeErrorEB(
            "API port-forward requires an exact source and verified Pod"
        )
    port = _reserve_loopback_port()
    stdout = _open_performance_text_output(root, "port-forward.stdout")
    try:
        stderr = _open_performance_text_output(root, "port-forward.stderr")
    except BaseException:
        stdout.close()
        raise
    kubectl = toolchain(root)["tools"]["kubectl"]
    try:
        process = subprocess.Popen(
            [
                kubectl,
                "-n",
                APP_NAMESPACE,
                "port-forward",
                f"pod/{pod_name}",
                f"{port}:8080",
                "--address=127.0.0.1",
            ],
            cwd=ROOT,
            stdout=stdout,
            stderr=stderr,
            env=kube_env(root),
            text=True,
            pass_fds=_bound_subprocess_pass_fds(),
        )
    except BaseException:
        stdout.close()
        stderr.close()
        raise
    try:
        _wait_http_200(
            f"http://127.0.0.1:{port}/health/live",
            process,
        )
        (
            current_pod_name,
            current_pod,
            current_binding,
        ) = _require_t048_api_runtime_binding(
            root,
            source_commit,
        )
        if (
            _t048_api_runtime_binding_identity(
                current_pod_name,
                current_pod,
                current_binding,
            )
            != expected_binding
        ):
            raise RuntimeErrorEB(
                "API runtime changed while establishing T048 port-forward"
            )
    except BaseException:
        _stop_process(process)
        stdout.close()
        stderr.close()
        raise
    return process, port, stdout, stderr

def _api_pod(root: Path) -> tuple[str, dict[str, Any]]:
    pods = _kubectl_json(
        root,
        [
            "-n", APP_NAMESPACE, "get", "pods",
            "-l", "app.kubernetes.io/name=weltgewebe-api",
        ],
    )
    items = pods.get("items")
    if not isinstance(items, list) or len(items) != 1:
        raise RuntimeErrorEB("Experiment B expects exactly one API pod")
    pod = items[0]
    name = pod.get("metadata", {}).get("name")
    if not isinstance(name, str) or not name:
        raise RuntimeErrorEB("API pod has no name")
    return name, pod


def _require_t048_api_release_binding(
    root: Path,
    source_commit: str,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    release_path = root / "receipts/release.json"
    try:
        release = json.loads(release_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeErrorEB("T048 proof requires a valid release receipt") from exc
    api_digest = release.get("api_digest") if isinstance(release, dict) else None
    if (
        not isinstance(release, dict)
        or release.get("schema_version") != 1
        or release.get("status") != "applied"
        or release.get("source_commit") != source_commit
        or not isinstance(api_digest, str)
        or not DIGEST_RE.fullmatch(api_digest)
    ):
        raise RuntimeErrorEB("T048 proof release image binding is invalid")

    pod_name, pod = _api_pod(root)
    expected_images = {
        "containers": {
            "api": f"ghcr.io/heimgewebe/commonthing-api@{api_digest}",
            "search-worker": f"ghcr.io/heimgewebe/commonthing-api@{api_digest}",
            "ollama": str(
                _source_commit_config(source_commit)[
                    "semantic_search"
                ]["ollama_image"]
            ),
        },
        "init_containers": {},
    }
    readback = _require_running_pod_image_contract(
        [pod],
        namespace=APP_NAMESPACE,
        workload="weltgewebe-api",
        expected_replicas=1,
        expected_images=expected_images,
        required_labels={"app.kubernetes.io/name": "weltgewebe-api"},
        context="T048 API Pod",
    )
    return pod_name, pod, readback


def _require_t048_api_runtime_binding(
    root: Path,
    source_commit: str,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    pod_name, pod, image_readback = _require_t048_api_release_binding(
        root, source_commit
    )
    release_path = root / "receipts/release.json"
    try:
        release = json.loads(release_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeErrorEB(
            "T048 proof requires a valid release receipt"
        ) from exc
    deployment = _kubectl_json(
        root,
        [
            "-n",
            APP_NAMESPACE,
            "get",
            "deployment",
            "weltgewebe-api",
        ],
    )
    workload = _require_live_application_workloads(
        root,
        release,
        {"weltgewebe-api": deployment},
        {"weltgewebe-api": [pod]},
        names=("weltgewebe-api",),
    ).get("weltgewebe-api")
    if (
        not isinstance(workload, dict)
        or workload.get("canonical") is not True
        or not isinstance(workload.get("contract_sha256"), str)
        or not isinstance(workload.get("pod_contract_sha256"), str)
    ):
        raise RuntimeErrorEB(
            "T048 API full runtime contract is invalid"
        )
    return (
        pod_name,
        pod,
        {
            **image_readback,
            "contract_sha256": workload["contract_sha256"],
            "pod_contract_sha256": workload["pod_contract_sha256"],
            "canonical": True,
        },
    )



def _t048_api_runtime_binding_identity(
    pod_name: str,
    pod: dict[str, Any],
    binding: dict[str, Any],
) -> dict[str, str]:
    metadata = pod.get("metadata")
    status = pod.get("status")
    pod_uid = metadata.get("uid") if isinstance(metadata, dict) else None
    statuses = (
        status.get("containerStatuses")
        if isinstance(status, dict)
        else None
    )
    api_container_id: str | None = None
    if isinstance(statuses, list):
        for item in statuses:
            if isinstance(item, dict) and item.get("name") == "api":
                value = item.get("containerID")
                if isinstance(value, str):
                    api_container_id = value
                break
    if (
        not isinstance(pod_name, str)
        or not pod_name
        or not isinstance(pod_uid, str)
        or not pod_uid
        or not isinstance(api_container_id, str)
        or re.fullmatch(
            r"containerd://[0-9a-f]{64}",
            api_container_id,
        )
        is None
    ):
        raise RuntimeErrorEB("T048 API runtime identity is incomplete")
    result = {
        "pod_name": pod_name,
        "pod_uid": pod_uid,
        "api_container_id": api_container_id,
    }
    for field in (
        "runtime_image_ids_sha256",
        "contract_sha256",
        "pod_contract_sha256",
    ):
        value = binding.get(field)
        if not isinstance(value, str) or not value:
            raise RuntimeErrorEB(
                f"T048 API runtime binding field is invalid: {field}"
            )
        result[field] = value
    return result


def _require_postgres_runtime_binding(
    root: Path,
    source_commit: str | None = None,
) -> dict[str, Any]:
    postgres_manifest = CLUSTER / "data/postgres.yaml"
    expected = (
        _versioned_data_deployment_contract(
            postgres_manifest, "postgres"
        )
        if source_commit is None
        else _source_commit_data_deployment_contract(
            source_commit,
            postgres_manifest,
            "postgres",
        )
    )
    expected_resources = (
        _versioned_data_container_resources(
            postgres_manifest, "postgres", "postgres"
        )
        if source_commit is None
        else _source_commit_data_container_resources(
            source_commit,
            postgres_manifest,
            "postgres",
            "postgres",
        )
    )
    live_data = _require_live_data_deployments(
        root,
        ("postgres",),
        source_commit=source_commit,
    )
    postgres = live_data.get("postgres")
    if (
        not isinstance(postgres, dict)
        or postgres.get("canonical") is not True
        or postgres.get("contract_sha256")
        != expected["contract_sha256"]
        or postgres.get("pod_contract_sha256")
        != expected["pod_contract_sha256"]
        or postgres.get("images_sha256")
        != _stable_json_sha256(expected["images"])
    ):
        raise RuntimeErrorEB(
            "PostgreSQL full runtime contract drifted"
        )
    pod_readback = postgres.get("pods")
    if not isinstance(pod_readback, dict):
        raise RuntimeErrorEB(
            "PostgreSQL Pod runtime contract is missing"
        )
    runtime_image_ids_sha256 = pod_readback.get(
        "runtime_image_ids_sha256"
    )
    if (
        not isinstance(runtime_image_ids_sha256, str)
        or re.fullmatch(
            r"[0-9a-f]{64}", runtime_image_ids_sha256
        )
        is None
    ):
        raise RuntimeErrorEB(
            "PostgreSQL runtime image identity is invalid"
        )
    observed_pods = pod_readback.get("pods")
    if (
        not isinstance(observed_pods, dict)
        or len(observed_pods) != 1
        or any(
            not isinstance(name, str) or not name
            for name in observed_pods
        )
    ):
        raise RuntimeErrorEB(
            "PostgreSQL runtime binding does not identify exactly one Pod"
        )
    pod_name = next(iter(observed_pods))
    container_ids = postgres.get("container_ids")
    pod_container_ids = (
        container_ids.get(pod_name)
        if isinstance(container_ids, dict)
        else None
    )
    container_id = (
        pod_container_ids.get("postgres")
        if isinstance(pod_container_ids, dict)
        else None
    )
    if (
        not isinstance(container_id, str)
        or re.fullmatch(
            r"containerd://[0-9a-f]{64}",
            container_id,
        )
        is None
    ):
        raise RuntimeErrorEB(
            "PostgreSQL runtime binding has no exact containerID"
        )

    live_pod_items = _kubectl_json(
        root,
        ["-n", DATA_NAMESPACE, "get", "pods"],
    ).get("items")
    if not isinstance(live_pod_items, list) or any(
        not isinstance(item, dict) for item in live_pod_items
    ):
        raise RuntimeErrorEB(
            "PostgreSQL runtime Pod identity inventory is invalid"
        )
    matching_live_pods = [
        item
        for item in live_pod_items
        if item.get("metadata", {}).get("name") == pod_name
    ]
    if len(matching_live_pods) != 1:
        raise RuntimeErrorEB(
            "PostgreSQL runtime Pod identity is not unique"
        )
    live_pod = matching_live_pods[0]
    live_metadata = live_pod.get("metadata")
    live_status = live_pod.get("status")
    pod_uid = (
        live_metadata.get("uid")
        if isinstance(live_metadata, dict)
        else None
    )
    pod_ip = (
        live_status.get("podIP")
        if isinstance(live_status, dict)
        else None
    )
    try:
        normalized_pod_ip = (
            str(ipaddress.ip_address(pod_ip))
            if isinstance(pod_ip, str)
            else None
        )
    except ValueError:
        normalized_pod_ip = None
    ready = (
        any(
            isinstance(condition, dict)
            and condition.get("type") == "Ready"
            and condition.get("status") == "True"
            for condition in live_status.get("conditions", [])
        )
        if isinstance(live_status, dict)
        else False
    )
    live_statuses = (
        live_status.get("containerStatuses")
        if isinstance(live_status, dict)
        else None
    )
    postgres_statuses = (
        [
            item
            for item in live_statuses
            if isinstance(item, dict)
            and item.get("name") == "postgres"
        ]
        if isinstance(live_statuses, list)
        else []
    )
    if (
        not isinstance(live_metadata, dict)
        or live_metadata.get("namespace") != DATA_NAMESPACE
        or live_metadata.get("deletionTimestamp") is not None
        or not isinstance(pod_uid, str)
        or not pod_uid
        or normalized_pod_ip is None
        or not isinstance(live_status, dict)
        or live_status.get("phase") != "Running"
        or not ready
        or len(postgres_statuses) != 1
        or postgres_statuses[0].get("containerID") != container_id
    ):
        raise RuntimeErrorEB(
            "PostgreSQL runtime Pod identity changed during binding"
        )

    return {
        "pod_name": pod_name,
        "pod_uid": pod_uid,
        "pod_ip": normalized_pod_ip,
        "container_id": container_id,
        "images_sha256": postgres["images_sha256"],
        "resources_sha256": _stable_json_sha256(
            expected_resources
        ),
        "contract_sha256": postgres["contract_sha256"],
        "pod_contract_sha256": postgres["pod_contract_sha256"],
        "runtime_image_ids_sha256": runtime_image_ids_sha256,
        "pods": pod_readback,
        "canonical": True,
    }



def _nats_runtime_binding_identity(
    binding: dict[str, Any],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for field in (
        "container_id",
        "contract_sha256",
        "pod_contract_sha256",
        "runtime_image_ids_sha256",
    ):
        value = binding.get(field)
        if not isinstance(value, str) or not value:
            raise RuntimeErrorEB(
                f"NATS runtime binding field is invalid: {field}"
            )
        result[field] = value
    return result


def _require_nats_runtime_binding(
    root: Path,
    source_commit: str,
) -> dict[str, Any]:
    if COMMIT_RE.fullmatch(source_commit) is None:
        raise RuntimeErrorEB(
            "NATS runtime binding requires exact source commit"
        )
    nats_manifest = CLUSTER / "data/nats.yaml"
    expected = _source_commit_data_deployment_contract(
        source_commit,
        nats_manifest,
        "nats",
    )
    live_data = _require_live_data_deployments(
        root,
        ("nats",),
        source_commit=source_commit,
    )
    nats = live_data.get("nats")
    if (
        not isinstance(nats, dict)
        or nats.get("canonical") is not True
        or nats.get("contract_sha256")
        != expected["contract_sha256"]
        or nats.get("pod_contract_sha256")
        != expected["pod_contract_sha256"]
        or nats.get("images_sha256")
        != _stable_json_sha256(expected["images"])
    ):
        raise RuntimeErrorEB(
            "NATS full runtime contract drifted"
        )
    pod_readback = nats.get("pods")
    if not isinstance(pod_readback, dict):
        raise RuntimeErrorEB(
            "NATS Pod runtime contract is missing"
        )
    runtime_image_ids_sha256 = pod_readback.get(
        "runtime_image_ids_sha256"
    )
    observed_pods = pod_readback.get("pods")
    if (
        not isinstance(runtime_image_ids_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", runtime_image_ids_sha256)
        is None
        or not isinstance(observed_pods, dict)
        or len(observed_pods) != 1
    ):
        raise RuntimeErrorEB(
            "NATS runtime identity is invalid"
        )
    pod_name = next(iter(observed_pods))
    container_ids = nats.get("container_ids")
    pod_container_ids = (
        container_ids.get(pod_name)
        if isinstance(container_ids, dict)
        else None
    )
    container_id = (
        pod_container_ids.get("nats")
        if isinstance(pod_container_ids, dict)
        else None
    )
    if (
        not isinstance(container_id, str)
        or re.fullmatch(
            r"containerd://[0-9a-f]{64}",
            container_id,
        )
        is None
    ):
        raise RuntimeErrorEB(
            "NATS runtime binding has no exact containerID"
        )
    return {
        "pod_name": pod_name,
        "container_id": container_id,
        "images_sha256": nats["images_sha256"],
        "contract_sha256": nats["contract_sha256"],
        "pod_contract_sha256": nats["pod_contract_sha256"],
        "runtime_image_ids_sha256": runtime_image_ids_sha256,
        "pods": pod_readback,
        "canonical": True,
    }


def _require_t048_postgres_runtime_binding(
    root: Path,
    source_commit: str | None = None,
) -> dict[str, Any]:
    return _require_postgres_runtime_binding(
        root,
        source_commit,
    )


def _require_t048_postgres_service_binding(
    root: Path,
    source_commit: str,
    postgres_binding: dict[str, Any],
) -> dict[str, Any]:
    if COMMIT_RE.fullmatch(source_commit) is None:
        raise RuntimeErrorEB(
            "PostgreSQL Service binding requires exact source commit"
        )
    if not isinstance(postgres_binding, dict):
        raise RuntimeErrorEB(
            "PostgreSQL Service binding requires runtime Pod identity"
        )
    pod_name = postgres_binding.get("pod_name")
    pod_uid = postgres_binding.get("pod_uid")
    pod_ip = postgres_binding.get("pod_ip")
    if (
        not isinstance(pod_name, str)
        or not pod_name
        or not isinstance(pod_uid, str)
        or not pod_uid
        or not isinstance(pod_ip, str)
    ):
        raise RuntimeErrorEB(
            "PostgreSQL Service binding requires complete Pod identity"
        )
    try:
        normalized_pod_ip = str(ipaddress.ip_address(pod_ip))
    except ValueError as exc:
        raise RuntimeErrorEB(
            "PostgreSQL Service binding Pod address is invalid"
        ) from exc

    expected = _source_commit_data_service_contract(
        source_commit,
        CLUSTER / "data/postgres.yaml",
        "postgres",
    )
    service = _kubectl_json(
        root,
        ["-n", DATA_NAMESPACE, "get", "service", "postgres"],
    )
    metadata = (
        service.get("metadata", {})
        if isinstance(service, dict)
        else {}
    )
    service_uid = (
        metadata.get("uid")
        if isinstance(metadata, dict)
        else None
    )
    service_resource_version = (
        metadata.get("resourceVersion")
        if isinstance(metadata, dict)
        else None
    )
    if (
        not isinstance(metadata, dict)
        or metadata.get("name") != "postgres"
        or metadata.get("namespace") != DATA_NAMESPACE
        or metadata.get("deletionTimestamp") is not None
        or not isinstance(service_uid, str)
        or not service_uid
        or not isinstance(service_resource_version, str)
        or not service_resource_version
    ):
        raise RuntimeErrorEB(
            "PostgreSQL Service identity drifted"
        )
    observed_spec = _service_spec_projection(
        service,
        "T048 PostgreSQL Service",
    )
    if observed_spec != expected["spec"]:
        raise RuntimeErrorEB(
            "PostgreSQL Service spec drifted from source commit"
        )
    service_ports = expected["spec"].get("ports")
    if (
        not isinstance(service_ports, list)
        or len(service_ports) != 1
        or not isinstance(service_ports[0], dict)
        or service_ports[0].get("name") != "postgres"
        or service_ports[0].get("protocol") != "TCP"
        or not isinstance(service_ports[0].get("port"), int)
        or isinstance(service_ports[0].get("port"), bool)
    ):
        raise RuntimeErrorEB(
            "PostgreSQL Service port contract is invalid"
        )
    expected_port = int(service_ports[0]["port"])

    slices = _endpoint_slice_collection_json(
        root,
        DATA_NAMESPACE,
        "postgres",
    )
    items = slices.get("items") if isinstance(slices, dict) else None
    list_metadata = (
        slices.get("metadata")
        if isinstance(slices, dict)
        else None
    )
    endpoint_list_resource_version = (
        list_metadata.get("resourceVersion")
        if isinstance(list_metadata, dict)
        else None
    )
    if (
        not isinstance(endpoint_list_resource_version, str)
        or not endpoint_list_resource_version
        or not isinstance(items, list)
        or len(items) != 1
        or not isinstance(items[0], dict)
    ):
        raise RuntimeErrorEB(
            "PostgreSQL Service EndpointSlice inventory is invalid"
        )
    item = items[0]
    slice_metadata = item.get("metadata")
    slice_uid = (
        slice_metadata.get("uid")
        if isinstance(slice_metadata, dict)
        else None
    )
    labels = (
        slice_metadata.get("labels")
        if isinstance(slice_metadata, dict)
        else None
    )
    ports = item.get("ports")
    endpoints = item.get("endpoints")
    if (
        not isinstance(slice_metadata, dict)
        or slice_metadata.get("namespace") != DATA_NAMESPACE
        or slice_metadata.get("deletionTimestamp") is not None
        or not isinstance(slice_uid, str)
        or not slice_uid
        or not isinstance(labels, dict)
        or labels.get("kubernetes.io/service-name") != "postgres"
        or item.get("addressType") not in {"IPv4", "IPv6"}
        or not isinstance(ports, list)
        or len(ports) != 1
        or not isinstance(ports[0], dict)
        or ports[0].get("name") != "postgres"
        or ports[0].get("protocol") != "TCP"
        or ports[0].get("port") != expected_port
        or not isinstance(endpoints, list)
        or len(endpoints) != 1
        or not isinstance(endpoints[0], dict)
    ):
        raise RuntimeErrorEB(
            "PostgreSQL Service EndpointSlice contract is invalid"
        )
    endpoint = endpoints[0]
    conditions = endpoint.get("conditions")
    target_ref = endpoint.get("targetRef")
    addresses = endpoint.get("addresses")
    if (
        not isinstance(conditions, dict)
        or conditions.get("ready") is not True
        or conditions.get("terminating") is True
        or conditions.get("serving") is False
        or not isinstance(target_ref, dict)
        or target_ref.get("kind") != "Pod"
        or target_ref.get("namespace") != DATA_NAMESPACE
        or target_ref.get("name") != pod_name
        or target_ref.get("uid") != pod_uid
        or not isinstance(addresses, list)
        or len(addresses) != 1
        or not isinstance(addresses[0], str)
    ):
        raise RuntimeErrorEB(
            "PostgreSQL Service endpoint target drifted"
        )
    try:
        observed_address = str(ipaddress.ip_address(addresses[0]))
    except ValueError as exc:
        raise RuntimeErrorEB(
            "PostgreSQL Service endpoint address is invalid"
        ) from exc
    expected_address_type = (
        "IPv4"
        if ipaddress.ip_address(normalized_pod_ip).version == 4
        else "IPv6"
    )
    if (
        item.get("addressType") != expected_address_type
        or observed_address != normalized_pod_ip
    ):
        raise RuntimeErrorEB(
            "PostgreSQL Service endpoint address drifted"
        )
    endpoint_projection = {
        "pod_name": pod_name,
        "pod_uid": pod_uid,
        "address": observed_address,
        "port": expected_port,
        "protocol": "TCP",
    }
    return {
        "service_uid": service_uid,
        "service_resource_version": service_resource_version,
        "endpoint_list_resource_version": endpoint_list_resource_version,
        "endpoint_slice_uid": slice_uid,
        "pod_name": pod_name,
        "pod_uid": pod_uid,
        "pod_ip": normalized_pod_ip,
        "service_spec_sha256": expected["spec_sha256"],
        "endpoint_sha256": _stable_json_sha256(
            endpoint_projection
        ),
    }


def _t048_postgres_service_semantic_binding(
    binding: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(binding, dict):
        raise RuntimeErrorEB(
            "PostgreSQL Service semantic binding is invalid"
        )
    normalized = json.loads(json.dumps(binding))
    normalized.pop("service_resource_version", None)
    normalized.pop("endpoint_list_resource_version", None)
    return normalized


@contextmanager
def _guard_t048_postgres_service_endpoints(
    root: Path,
    binding: dict[str, Any],
) -> Iterator[None]:
    if not isinstance(binding, dict):
        raise RuntimeErrorEB(
            "PostgreSQL serving dependency binding is invalid"
        )
    service_resource_version = binding.get(
        "service_resource_version"
    )
    endpoint_list_resource_version = binding.get(
        "endpoint_list_resource_version"
    )
    if (
        not isinstance(service_resource_version, str)
        or not service_resource_version
        or not isinstance(endpoint_list_resource_version, str)
        or not endpoint_list_resource_version
    ):
        raise RuntimeErrorEB(
            "PostgreSQL serving dependency binding has no resourceVersion"
        )

    namespace_path = urllib.parse.quote(DATA_NAMESPACE, safe="")
    dependencies = (
        (
            service_resource_version,
            "fieldSelector",
            "metadata.name=postgres",
            f"/api/v1/namespaces/{namespace_path}/services",
        ),
        (
            endpoint_list_resource_version,
            "labelSelector",
            "kubernetes.io/service-name=postgres",
            (
                f"/apis/discovery.k8s.io/v1/namespaces/"
                f"{namespace_path}/endpointslices"
            ),
        ),
    )

    yield

    kubectl = toolchain(root)["tools"]["kubectl"]
    for (
        resource_version,
        selector_name,
        selector_value,
        collection_path,
    ) in dependencies:
        watch_query = urllib.parse.urlencode(
            {
                "watch": "1",
                "resourceVersion": resource_version,
                "allowWatchBookmarks": "true",
                "timeoutSeconds": "2",
                selector_name: selector_value,
            }
        )
        result = run(
            [
                kubectl,
                "get",
                "--raw",
                f"{collection_path}?{watch_query}",
            ],
            env=kube_env(root),
            timeout=10,
        )
        for raw_event in (result.stdout or "").splitlines():
            if not raw_event.strip():
                continue
            try:
                event = json.loads(raw_event)
            except json.JSONDecodeError as exc:
                raise RuntimeErrorEB(
                    "PostgreSQL serving dependency replay is invalid"
                ) from exc
            if (
                not isinstance(event, dict)
                or not isinstance(event.get("type"), str)
                or not isinstance(event.get("object"), dict)
            ):
                raise RuntimeErrorEB(
                    "PostgreSQL serving dependency replay event is invalid"
                )
            if event["type"] != "BOOKMARK":
                raise RuntimeErrorEB(
                    "PostgreSQL serving dependency changed during T048 load"
                )


def _parse_cpu_quantity(value: str) -> float:
    if value.endswith("m"):
        return float(value[:-1]) / 1000.0
    return float(value)


def _parse_memory_quantity(value: str) -> int:
    units = {
        "Ki": 1024,
        "Mi": 1024 ** 2,
        "Gi": 1024 ** 3,
        "K": 1000,
        "M": 1000 ** 2,
        "G": 1000 ** 3,
    }
    for suffix, factor in units.items():
        if value.endswith(suffix):
            return int(float(value[: -len(suffix)]) * factor)
    return int(value)


def _require_api_resource_limits(
    pod: Any, config: dict[str, Any]
) -> tuple[float, int]:
    if not isinstance(pod, dict):
        raise RuntimeErrorEB("API pod resource contract payload is not an object")
    containers = pod.get("spec", {}).get("containers", [])
    if not isinstance(containers, list):
        raise RuntimeErrorEB("API pod container inventory is invalid")
    api_containers = [
        item for item in containers
        if isinstance(item, dict) and item.get("name") == "api"
    ]
    if len(api_containers) != 1:
        raise RuntimeErrorEB("API pod must contain exactly one api container")
    live_limits = api_containers[0].get("resources", {}).get("limits", {})
    expected_limits = config.get("runtime_binding", {}).get("api_resource_limits")
    if not isinstance(live_limits, dict) or not isinstance(expected_limits, dict):
        raise RuntimeErrorEB("Experiment-B API resource limits are missing")
    try:
        live_cpu = _parse_cpu_quantity(str(live_limits.get("cpu", "")))
        live_memory = _parse_memory_quantity(str(live_limits.get("memory", "")))
        expected_cpu = _parse_cpu_quantity(str(expected_limits.get("cpu", "")))
        expected_memory = _parse_memory_quantity(str(expected_limits.get("memory", "")))
    except (TypeError, ValueError) as exc:
        raise RuntimeErrorEB("Experiment-B API resource limits are invalid") from exc
    if live_cpu != expected_cpu or live_memory != expected_memory:
        raise RuntimeErrorEB(
            "live API resource limits drifted from the Experiment-B contract"
        )
    return expected_cpu, expected_memory


def _sample_api_cgroup(root: Path, pod_name: str) -> dict[str, Any]:
    script = (
        "cat /sys/fs/cgroup/cpu.stat; "
        "printf '\\n__CPU_MAX__\\n'; cat /sys/fs/cgroup/cpu.max; "
        "printf '\\n__MEM_CURRENT__\\n'; cat /sys/fs/cgroup/memory.current; "
        "printf '\\n__MEM_MAX__\\n'; cat /sys/fs/cgroup/memory.max"
    )
    raw = _kubectl(
        root,
        [
            "-n", APP_NAMESPACE, "exec", pod_name, "-c", "api", "--",
            "/bin/sh", "-c", script,
        ],
    ).stdout
    cpu_part, rest = raw.split("__CPU_MAX__", 1)
    cpu_max_part, rest = rest.split("__MEM_CURRENT__", 1)
    mem_current_part, mem_max_part = rest.split("__MEM_MAX__", 1)
    cpu_values = {}
    for line in cpu_part.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1].isdigit():
            cpu_values[fields[0]] = int(fields[1])
    usage_usec = cpu_values.get("usage_usec")
    if usage_usec is None:
        raise RuntimeErrorEB("API cgroup cpu.stat has no usage_usec")
    cpu_max = cpu_max_part.strip().split()
    if len(cpu_max) != 2 or cpu_max[0] == "max":
        raise RuntimeErrorEB("API cgroup does not expose a finite CPU quota")
    quota, period = (int(cpu_max[0]), int(cpu_max[1]))
    memory_current = int(mem_current_part.strip())
    memory_max_raw = mem_max_part.strip()
    if memory_max_raw == "max":
        raise RuntimeErrorEB("API cgroup does not expose a finite memory limit")
    return {
        "observed_at_unix_ms": time.time_ns() // 1_000_000,
        "usage_usec": usage_usec,
        "memory_bytes": memory_current,
        "cpu_limit_cores": quota / period,
        "memory_limit_bytes": int(memory_max_raw),
    }


def _database_connection_count(
    root: Path,
    source_commit: str,
    postgres_binding: dict[str, Any],
    database_identity: tuple[str, str],
) -> int:
    value = _run_bound_postgres_sql(
        root,
        source_commit,
        postgres_binding,
        database_identity,
        "SELECT count(*) FROM pg_stat_activity;",
    )
    try:
        count = int(value)
    except ValueError as exc:
        raise RuntimeErrorEB("PostgreSQL connection count is not an integer") from exc
    if count < 0:
        raise RuntimeErrorEB("PostgreSQL connection count is negative")
    return count


def _stop_process(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


@contextmanager
def _k6_summary_output_channel() -> Iterator[int]:
    if not sys.platform.startswith("linux"):
        raise RuntimeErrorEB("k6 summary output requires an anonymous Linux memfd")
    memfd_create = getattr(os, "memfd_create", None)
    allow_sealing = int(getattr(os, "MFD_ALLOW_SEALING", 0x0002))
    cloexec = int(getattr(os, "MFD_CLOEXEC", 0x0001))
    try:
        if callable(memfd_create):
            summary_fd = memfd_create(
                "commonthing-experiment-b-k6-summary",
                flags=allow_sealing | cloexec,
            )
        else:
            libc = ctypes.CDLL(None, use_errno=True)
            native_memfd_create = libc.memfd_create
            native_memfd_create.argtypes = [ctypes.c_char_p, ctypes.c_uint]
            native_memfd_create.restype = ctypes.c_int
            summary_fd = native_memfd_create(
                b"commonthing-experiment-b-k6-summary",
                allow_sealing | cloexec,
            )
            if summary_fd < 0:
                error_number = ctypes.get_errno()
                raise OSError(error_number, os.strerror(error_number))
    except (AttributeError, OSError) as exc:
        raise RuntimeErrorEB("k6 summary output memfd creation failed") from exc
    try:
        os.fchmod(summary_fd, 0o600)
        yield summary_fd
    finally:
        os.close(summary_fd)


def _seal_k6_summary_output(summary_fd: int) -> bytes:
    try:
        metadata = os.fstat(summary_fd)
    except OSError as exc:
        raise RuntimeErrorEB("k6 summary output descriptor is invalid") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
        or metadata.st_size > K6_SUMMARY_MAX_BYTES
    ):
        raise RuntimeErrorEB("k6 summary output size is invalid")
    f_add_seals = int(getattr(fcntl, "F_ADD_SEALS", 1033))
    f_get_seals = int(getattr(fcntl, "F_GET_SEALS", 1034))
    required_seals = (
        int(getattr(fcntl, "F_SEAL_SEAL", 0x0001))
        | int(getattr(fcntl, "F_SEAL_SHRINK", 0x0002))
        | int(getattr(fcntl, "F_SEAL_GROW", 0x0004))
        | int(getattr(fcntl, "F_SEAL_WRITE", 0x0008))
    )
    try:
        fcntl.fcntl(summary_fd, f_add_seals, required_seals)
        observed_seals = fcntl.fcntl(summary_fd, f_get_seals)
    except OSError as exc:
        raise RuntimeErrorEB("k6 summary output sealing failed") from exc
    if observed_seals & required_seals != required_seals:
        raise RuntimeErrorEB("k6 summary output sealing is incomplete")
    payload = os.pread(summary_fd, metadata.st_size, 0)
    if len(payload) != metadata.st_size:
        raise RuntimeErrorEB("k6 summary output descriptor read is incomplete")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeErrorEB("k6 summary output is not UTF-8") from exc
    if text.count(K6_SUMMARY_STDOUT_MARKER) != 1:
        raise RuntimeErrorEB("k6 summary output has no unique authority marker")
    _prefix, encoded = text.split(K6_SUMMARY_STDOUT_MARKER, 1)
    encoded = encoded.lstrip()
    try:
        summary, end = json.JSONDecoder().raw_decode(encoded)
    except json.JSONDecodeError as exc:
        raise RuntimeErrorEB("k6 summary output is invalid JSON") from exc
    if not isinstance(summary, dict) or encoded[end:].strip():
        raise RuntimeErrorEB("k6 summary output has invalid trailing content")
    return encoded[:end].encode("utf-8")

def _sample_t048_load(
    root: Path,
    source_commit: str,
    pod_name: str,
    postgres_binding: dict[str, Any],
    postgres_service_binding: dict[str, Any],
    database_identity: tuple[str, str],
    load: subprocess.Popen[Any],
    resource_samples: list[dict[str, Any]],
    db_samples: list[int],
    timeout_seconds: int,
) -> int:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or timeout_seconds <= 0
    ):
        raise RuntimeErrorEB("canonical T048 k6 workload timeout is invalid")
    if (
        not isinstance(postgres_service_binding, dict)
        or not postgres_service_binding
    ):
        raise RuntimeErrorEB(
            "canonical T048 PostgreSQL Service binding is invalid"
        )
    current_service_binding = _require_t048_postgres_service_binding(
        root,
        source_commit,
        postgres_binding,
    )
    if (
        _t048_postgres_service_semantic_binding(current_service_binding)
        != _t048_postgres_service_semantic_binding(
            postgres_service_binding
        )
    ):
        raise RuntimeErrorEB(
            "PostgreSQL Service endpoint binding changed during T048 load"
        )
    deadline = time.monotonic() + timeout_seconds
    try:
        while load.poll() is None:
            if time.monotonic() >= deadline:
                raise RuntimeErrorEB(
                    "canonical T048 k6 workload exceeded bounded runtime"
                )
            time.sleep(1)
            current_service_binding = _require_t048_postgres_service_binding(
                root,
                source_commit,
                postgres_binding,
            )
            if (
                _t048_postgres_service_semantic_binding(current_service_binding)
                != _t048_postgres_service_semantic_binding(
                    postgres_service_binding
                )
            ):
                raise RuntimeErrorEB(
                    "PostgreSQL Service endpoint binding changed during T048 load"
                )
            resource_samples.append(_sample_api_cgroup(root, pod_name))
            db_samples.append(
                _database_connection_count(
                    root,
                    source_commit,
                    postgres_binding,
                    database_identity,
                )
            )
        current_service_binding = _require_t048_postgres_service_binding(
            root,
            source_commit,
            postgres_binding,
        )
        if (
            _t048_postgres_service_semantic_binding(current_service_binding)
            != _t048_postgres_service_semantic_binding(
                postgres_service_binding
            )
        ):
            raise RuntimeErrorEB(
                "PostgreSQL Service endpoint binding changed during T048 load"
            )
        if load.returncode is None:
            raise RuntimeErrorEB("canonical T048 k6 workload has no terminal return code")
        return int(load.returncode)
    finally:
        _stop_process(load)


@_serialize_experiment_b_lifecycle
def t048_load_proof(root: Path, source_commit: str) -> dict[str, Any]:
    report_path, attempt_path, attempt_started_at_unix_ms = (
        _begin_live_check_attempt(root, "t048-load", source_commit)
    )
    if _current_protected_main_commit() != source_commit:
        raise RuntimeErrorEB("T048 proof source is not current protected main")
    (
        target_receipt_before,
        target_ip_before,
        target_server_before,
    ) = _require_kubernetes_target_binding(root, source_commit)
    target_binding_before = {
        "vm_ip": target_ip_before,
        "kubeconfig_sha256": target_receipt_before["kubeconfig_sha256"],
        "server": target_server_before,
    }
    bound_stack = ExitStack()
    postgres_service_guard = ExitStack()
    bound_stack.enter_context(_bound_kube_env(root, target_binding_before, source_commit))
    try:
        postgres_binding_before = _require_t048_postgres_runtime_binding(
            root,
            source_commit,
        )
        postgres_service_binding_before = (
            _require_t048_postgres_service_binding(
                root,
                source_commit,
                postgres_binding_before,
            )
        )
        database_identity = _database_client_identity(root)
        fixture_receipt = _validated_t048_fixture_receipt(root, source_commit)
        fixture_binding_before = {
            "manifest": fixture_receipt.get("manifest"),
            "manifest_sha256": fixture_receipt.get("manifest_sha256"),
            "generation_id": fixture_receipt.get("generation_id"),
            "live_binding": fixture_receipt.get("live_binding"),
        }
        evidence, _domain_scale = _performance_modules(source_commit)
        manifest = Path(fixture_receipt["manifest"])
        policy, policy_sha256 = _source_bound_performance_policy(
            source_commit
        )
        contract_section = evidence.api_runtime_section(policy)
        scenario = contract_section["scenario"]
        scenario_duration_seconds = scenario.get("duration_seconds")
        if (
            isinstance(scenario_duration_seconds, bool)
            or not isinstance(scenario_duration_seconds, int)
            or scenario_duration_seconds <= 0
        ):
            raise RuntimeErrorEB("canonical T048 scenario duration is invalid")
        load_timeout_seconds = scenario_duration_seconds + 120
        k6_image, k6_workflow_sha256 = _k6_image_binding(source_commit)
        k6_workload_text, k6_workload_sha256 = _source_bound_k6_workload(
            source_commit
        )
        resource_path = root / "performance/resource-receipt.json"
        db_path = root / "performance/database-connections.json"
        k6_summary_output_fd = bound_stack.enter_context(
            _k6_summary_output_channel()
        )

        pod_name, pod, api_image_binding_before = _require_t048_api_runtime_binding(
            root, source_commit
        )
        api_runtime_binding_before = _t048_api_runtime_binding_identity(
            pod_name,
            pod,
            api_image_binding_before,
        )
        declared_cpu, declared_memory = _require_api_resource_limits(
            pod,
            _source_commit_config(source_commit),
        )

        process, port, pf_stdout, pf_stderr = _start_api_port_forward(
            root,
            source_commit,
            api_runtime_binding_before,
        )
    except BaseException:
        bound_stack.close()
        raise
    base_url = f"http://127.0.0.1:{port}"
    try:
        _prime_t048_search_metric(base_url, scenario["search_query"])
        # The shared T048 k6 workload counts every readiness 503 as a
        # measured failure. Exclude initial startup by proving stable ready 200.
        _wait_http_200(f"{base_url}/health/ready", process, consecutive=3)
        before_status, before_body, _elapsed = _http_read(f"{base_url}/metrics")
        if before_status != 200:
            raise RuntimeErrorEB("API /metrics pre-snapshot failed")
        metrics_before = before_body.decode("utf-8")
        _write_performance_text(root, "metrics-before.prom", metrics_before)
        families = evidence.parse_prometheus_text(metrics_before)
        if evidence.measured_api_commit(families) != source_commit:
            raise RuntimeErrorEB("API build_info commit does not match T048 source commit")

        run_id = f"t085-{source_commit[:12]}-{int(time.time())}"
        manifest_sha = sha256_file(manifest)
        evidence_dir = root / "performance"
        stderr_path = evidence_dir / "k6.stderr"
        docker_args = [
            "docker", "run", "--rm", "--interactive", "--network", "host",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--env", f"BASE_URL={base_url}",
            "--env", f"API_RUNTIME_VUS={scenario['virtual_users']}",
            "--env", f"API_RUNTIME_DURATION_SECONDS={scenario['duration_seconds']}",
            "--env", f"API_RUNTIME_DATASET_PROFILE={scenario['dataset_profile']}",
            "--env", f"API_RUNTIME_CONCURRENCY_PROFILE={scenario['concurrency_profile']}",
            "--env", f"API_RUNTIME_DATASET_MANIFEST_SHA256={manifest_sha}",
            "--env", f"API_RUNTIME_SEARCH_QUERY={scenario['search_query']}",
            "--env", f"API_RUNTIME_RUN_ID={run_id}",
            "--env", f"API_RUNTIME_K6_IMAGE={k6_image}",
            "--env", "API_RUNTIME_SUMMARY_PATH=stdout",
            k6_image, "run", "--quiet", "-",
        ]

        initial_cgroup = _sample_api_cgroup(root, pod_name)
        resource_samples = [initial_cgroup]
        db_samples = [_database_connection_count(
            root,
            source_commit,
            postgres_binding_before,
            database_identity,
        )]
        postgres_service_guard.enter_context(
            _guard_t048_postgres_service_endpoints(
                root,
                postgres_service_binding_before,
            )
        )
        sampler_started = time.time_ns() // 1_000_000
        k6_summary_snapshot_fd: int | None = None
        with _open_performance_text_output(root, "k6.stderr") as err:
            load = subprocess.Popen(
                docker_args,
                cwd=ROOT,
                stdin=subprocess.PIPE,
                stdout=k6_summary_output_fd,
                stderr=err,
                text=True,
            )
            if load.stdin is None:
                raise RuntimeErrorEB("canonical T048 k6 workload stdin is unavailable")
            try:
                load.stdin.write(k6_workload_text)
            except BrokenPipeError:
                pass
            finally:
                load.stdin.close()
            load_returncode = _sample_t048_load(
                root,
                source_commit,
                pod_name,
                postgres_binding_before,
                postgres_service_binding_before,
                database_identity,
                load,
                resource_samples,
                db_samples,
                load_timeout_seconds,
            )
            if load_returncode == 0:
                k6_summary_payload = _seal_k6_summary_output(
                    k6_summary_output_fd
                )
                k6_summary_snapshot_fd = bound_stack.enter_context(
                    _sealed_snapshot_fd(
                        k6_summary_payload,
                        "canonical T048 k6 summary",
                    )
                )
        resource_samples.append(_sample_api_cgroup(root, pod_name))
        db_samples.append(_database_connection_count(
            root,
            source_commit,
            postgres_binding_before,
            database_identity,
        ))
        sampler_finished = time.time_ns() // 1_000_000
        (
            post_pod_name,
            post_pod,
            api_image_binding_after,
        ) = _require_t048_api_runtime_binding(root, source_commit)
        postgres_binding_after = _require_t048_postgres_runtime_binding(
            root,
            source_commit,
        )
        postgres_service_binding_after = (
            _require_t048_postgres_service_binding(
                root,
                source_commit,
                postgres_binding_after,
            )
        )
        (
            target_receipt_after,
            target_ip_after,
            target_server_after,
        ) = _require_kubernetes_target_binding(root, source_commit)
        target_binding_after = {
            "vm_ip": target_ip_after,
            "kubeconfig_sha256": target_receipt_after["kubeconfig_sha256"],
            "server": target_server_after,
        }
        api_runtime_binding_after = _t048_api_runtime_binding_identity(
            post_pod_name,
            post_pod,
            api_image_binding_after,
        )
        if api_runtime_binding_after != api_runtime_binding_before:
            raise RuntimeErrorEB(
                "API runtime contract changed during the T048 measurement"
            )
        if (
            _postgres_runtime_binding_identity(postgres_binding_after)
            != _postgres_runtime_binding_identity(postgres_binding_before)
            or postgres_binding_after["images_sha256"]
            != postgres_binding_before["images_sha256"]
            or postgres_binding_after["resources_sha256"]
            != postgres_binding_before["resources_sha256"]
        ):
            raise RuntimeErrorEB(
                "PostgreSQL runtime contract changed during the T048 measurement"
            )
        if (
            _t048_postgres_service_semantic_binding(
                postgres_service_binding_after
            )
            != _t048_postgres_service_semantic_binding(
                postgres_service_binding_before
            )
        ):
            raise RuntimeErrorEB(
                "PostgreSQL Service endpoint binding changed during the T048 measurement"
            )
        if target_binding_after != target_binding_before:
            raise RuntimeErrorEB(
                "Kubernetes target identity changed during the T048 measurement"
            )
        fixture_receipt_after = _validated_t048_fixture_receipt(
            root, source_commit
        )
        fixture_binding_after = {
            "manifest": fixture_receipt_after.get("manifest"),
            "manifest_sha256": fixture_receipt_after.get("manifest_sha256"),
            "generation_id": fixture_receipt_after.get("generation_id"),
            "live_binding": fixture_receipt_after.get("live_binding"),
        }
        if fixture_binding_after != fixture_binding_before:
            raise RuntimeErrorEB(
                "T048 fixture changed during the load measurement"
            )
        if load_returncode != 0:
            detail = stderr_path.read_text(encoding="utf-8")[-3000:]
            raise RuntimeErrorEB(f"canonical T048 k6 workload failed: {detail}")
        if k6_summary_snapshot_fd is None:
            raise RuntimeErrorEB("canonical T048 k6 summary snapshot is unavailable")

        if process.poll() is not None:
            raise RuntimeErrorEB(
                "API port-forward exited before T048 metrics post-snapshot"
            )
        after_status, after_body, _elapsed = _http_read(f"{base_url}/metrics")
        if process.poll() is not None:
            raise RuntimeErrorEB(
                "API port-forward exited during T048 metrics post-snapshot"
            )
        if after_status != 200:
            raise RuntimeErrorEB("API /metrics post-snapshot failed")
        metrics_after = after_body.decode("utf-8")
        _write_performance_text(root, "metrics-after.prom", metrics_after)

        (
            final_pod_name,
            final_pod,
            final_api_image_binding,
        ) = _require_t048_api_runtime_binding(root, source_commit)
        final_api_runtime_binding = _t048_api_runtime_binding_identity(
            final_pod_name,
            final_pod,
            final_api_image_binding,
        )
        if final_api_runtime_binding != api_runtime_binding_before:
            raise RuntimeErrorEB(
                "API runtime contract changed after T048 metrics snapshot"
            )

        postgres_service_guard.close()

        cpu_percentages: list[float] = []
        for first, second in zip(resource_samples, resource_samples[1:]):
            elapsed_us = (
                int(second["observed_at_unix_ms"]) - int(first["observed_at_unix_ms"])
            ) * 1000
            usage_delta = int(second["usage_usec"]) - int(first["usage_usec"])
            if elapsed_us > 0 and usage_delta >= 0:
                cpu_percentages.append((usage_delta / elapsed_us) * 100.0)
        if not cpu_percentages:
            raise RuntimeErrorEB("API CPU sampler produced no usable intervals")
        peak_cpu = max(cpu_percentages)
        peak_memory = max(int(item["memory_bytes"]) for item in resource_samples)
        cpu_limits = {round(float(item["cpu_limit_cores"]), 9) for item in resource_samples}
        memory_limits = {int(item["memory_limit_bytes"]) for item in resource_samples}
        if cpu_limits != {declared_cpu}:
            raise RuntimeErrorEB("live API cgroup CPU quota differs from the deployment limit")
        if memory_limits != {declared_memory}:
            raise RuntimeErrorEB("live API cgroup memory limit differs from the deployment limit")

        resource_receipt = {
            "schema_version": 3,
            "contract": "api-replica-resource-sample-v3",
            "run_id": run_id,
            "container_name": "weltgewebe-api",
            "started_at_unix_ms": sampler_started,
            "finished_at_unix_ms": sampler_finished,
            "peaks": {
                "cpu_percent": peak_cpu,
                "memory_bytes": peak_memory,
            },
            "sample_count": len(resource_samples),
        }
        database_receipt = {
            "schema_version": 2,
            "contract": "postgres-connection-sample-v2",
            "run_id": run_id,
            "database_container": "postgres",
            "started_at_unix_ms": sampler_started,
            "finished_at_unix_ms": sampler_finished,
            "max_connections": max(db_samples),
            "sample_count": len(db_samples),
            "samples": db_samples,
        }
        atomic_json(resource_path, resource_receipt)
        atomic_json(db_path, database_receipt)
        evidence.load_resource_receipt(resource_path)
        evidence.load_database_connection_receipt(db_path)

        summary = evidence.load_k6_summary(
            Path(f"/proc/self/fd/{k6_summary_snapshot_fd}")
        )
        if evidence.extract_declared_scenario(summary) != scenario:
            raise RuntimeErrorEB("k6 scenario drifted from the canonical T048 policy")
        http_metrics = evidence.extract_http_metrics(summary)
        readiness_diagnostics = _t048_readiness_diagnostics(summary)
        failures = list(
            evidence.threshold_failures(http_metrics, contract_section["thresholds"])
        )
        workload = evidence.extract_runtime_workload_bindings(summary)
        if (
            workload["started_at_unix_ms"] < sampler_started
            or workload["finished_at_unix_ms"] > sampler_finished
        ):
            failures.append("resource/database sampler window does not cover k6 load")
        if peak_cpu > declared_cpu * 100.0:
            failures.append(
                f"API peak CPU {peak_cpu:.3f}% exceeds its {declared_cpu:.3f}-core hard limit"
            )
        if peak_memory > declared_memory:
            failures.append(
                f"API peak memory {peak_memory} exceeds its {declared_memory}-byte hard limit"
            )

        before = evidence.parse_prometheus_text(metrics_before)
        after = evidence.parse_prometheus_text(metrics_after)
        if evidence.measured_api_commit(after) != source_commit:
            failures.append("API build_info commit changed during T048 load")
        http_search_before = evidence.sum_counter(
            before, evidence.HTTP_COUNTER_NAME, {"path": evidence.SEARCH_PATH_LABEL}
        )
        http_search_after = evidence.sum_counter(
            after, evidence.HTTP_COUNTER_NAME, {"path": evidence.SEARCH_PATH_LABEL}
        )
        expected_queries = int(round(http_search_after - http_search_before))
        delta_hist = evidence.histogram_delta(
            evidence.histogram_snapshot(before, evidence.QUERY_HISTOGRAM_NAME),
            evidence.histogram_snapshot(after, evidence.QUERY_HISTOGRAM_NAME),
        )
        observed_queries = int(round(delta_hist.total_count))
        if expected_queries <= 0 or expected_queries != observed_queries:
            failures.append(
                "API search request count and PostgreSQL repository query count disagree"
            )
        database_metrics = {
            "p50_ms": evidence.histogram_quantile_ms(delta_hist, 0.50),
            "p95_ms": evidence.histogram_quantile_ms(delta_hist, 0.95),
            "p99_ms": evidence.histogram_quantile_ms(delta_hist, 0.99),
            "query_count_expected": expected_queries,
            "query_count_observed": observed_queries,
        }
        report = {
            "schema_version": 1,
            "status": "fail" if failures else "pass",
            "source_commit": source_commit,
            "policy_sha256": policy_sha256,
            "k6_workflow_sha256": k6_workflow_sha256,
            "k6_workload_sha256": k6_workload_sha256,
            "k6_image": k6_image,
            "fixture_manifest_sha256": manifest_sha,
            "fixture_live_binding_sha256": _stable_json_sha256(
                fixture_binding_before
            ),
            "api_runtime_image_ids_sha256": api_image_binding_before[
                "runtime_image_ids_sha256"
            ],
            "api_contract_sha256": api_image_binding_before[
                "contract_sha256"
            ],
            "api_pod_contract_sha256": api_image_binding_before[
                "pod_contract_sha256"
            ],
            "postgres_runtime_image_ids_sha256": postgres_binding_before[
                "runtime_image_ids_sha256"
            ],
            "postgres_resources_sha256": postgres_binding_before[
                "resources_sha256"
            ],
            "postgres_contract_sha256": postgres_binding_before[
                "contract_sha256"
            ],
            "postgres_pod_contract_sha256": postgres_binding_before[
                "pod_contract_sha256"
            ],
            "postgres_service_binding_sha256": _stable_json_sha256(
                postgres_service_binding_before
            ),
            "kubernetes_target_sha256": _stable_json_sha256(
                target_binding_before
            ),
            "scenario": scenario,
            "thresholds": contract_section["thresholds"],
            "http": http_metrics,
            "readiness": readiness_diagnostics,
            "database": database_metrics,
            "resources": {
                "api_peak_cpu_percent": peak_cpu,
                "api_peak_memory_bytes": peak_memory,
                "api_cpu_limit_cores": declared_cpu,
                "api_memory_limit_bytes": declared_memory,
                "postgres_max_connections": max(db_samples),
            },
            "failures": failures,
            "limitations": [
                "This is an Experiment-B target measurement, not a production-capacity claim.",
                "The canonical T048 scenario and latency/error thresholds are read from policies/performance.v1.json.",
                "Kubernetes-specific cgroup and PostgreSQL samplers replace the Docker-only T048 sampler implementations while preserving their measured quantities.",
                "The canonical T048 web-runtime proof is fixture-driven and target-independent; Experiment-B web functionality is covered separately by functional-readback.",
            ],
        }
        atomic_json(report_path, report)
        _complete_live_check_attempt(
            attempt_path,
            report_path,
            source_commit,
            attempt_started_at_unix_ms,
            str(report["status"]),
        )
        if failures:
            raise RuntimeErrorEB("T048 Experiment-B load gate failed: " + "; ".join(failures))
        return report
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        pf_stdout.close()
        pf_stderr.close()
        postgres_service_guard.close()
        bound_stack.close()


def _gateway_base_url(root: Path, source_commit: str) -> str:
    target = _kubernetes_target_identity(root, source_commit)
    gateway = _kubectl_json(
        root,
        ["-n", APP_NAMESPACE, "get", "gateway", "commonthing-experiment-b"],
    )
    addresses = gateway.get("status", {}).get("addresses")
    if not isinstance(addresses, list) or len(addresses) != 1:
        raise RuntimeErrorEB(
            "Experiment-B Gateway must expose exactly one admitted address"
        )
    address = addresses[0]
    value = address.get("value") if isinstance(address, dict) else None
    address_type = (
        address.get("type", "IPAddress")
        if isinstance(address, dict)
        else None
    )
    if (
        address_type != "IPAddress"
        or not isinstance(value, str)
        or value != target["vm_ip"]
    ):
        raise RuntimeErrorEB(
            "Experiment-B Gateway address is not bound to the verified VM target"
        )
    return f"http://{value}"


def _application_pod_runtime_identity(
    pod: dict[str, Any],
    expected_container_names: set[str],
    context: str,
) -> dict[str, Any]:
    if (
        not isinstance(expected_container_names, set)
        or not expected_container_names
        or any(
            not isinstance(name, str) or not name
            for name in expected_container_names
        )
    ):
        raise RuntimeErrorEB(f"{context} expected container set is invalid")
    metadata = pod.get("metadata")
    status = pod.get("status")
    pod_name = metadata.get("name") if isinstance(metadata, dict) else None
    pod_uid = metadata.get("uid") if isinstance(metadata, dict) else None
    pod_ip = status.get("podIP") if isinstance(status, dict) else None
    try:
        normalized_pod_ip = (
            str(ipaddress.ip_address(pod_ip))
            if isinstance(pod_ip, str)
            else None
        )
    except ValueError:
        normalized_pod_ip = None
    statuses = (
        status.get("containerStatuses")
        if isinstance(status, dict)
        else None
    )
    if (
        not isinstance(pod_name, str)
        or not pod_name
        or not isinstance(pod_uid, str)
        or not pod_uid
        or normalized_pod_ip is None
        or not isinstance(statuses, list)
    ):
        raise RuntimeErrorEB(f"{context} Pod runtime identity is incomplete")

    status_by_name: dict[str, dict[str, Any]] = {}
    for item in statuses:
        container_name = item.get("name") if isinstance(item, dict) else None
        if (
            not isinstance(container_name, str)
            or not container_name
            or container_name in status_by_name
        ):
            raise RuntimeErrorEB(
                f"{context} container runtime identity is invalid"
            )
        status_by_name[container_name] = item
    if set(status_by_name) != expected_container_names:
        raise RuntimeErrorEB(
            f"{context} container runtime identity set drifted"
        )

    container_ids: dict[str, str] = {}
    for container_name in sorted(expected_container_names):
        container_id = status_by_name[container_name].get("containerID")
        if (
            not isinstance(container_id, str)
            or re.fullmatch(
                r"containerd://[0-9a-f]{64}",
                container_id,
            )
            is None
        ):
            raise RuntimeErrorEB(
                f"{context} container ID is invalid: {container_name}"
            )
        container_ids[container_name] = container_id
    return {
        "pod_name": pod_name,
        "pod_uid": pod_uid,
        "pod_ip": normalized_pod_ip,
        "container_ids": container_ids,
    }


def _kubernetes_object_revision(
    payload: Any,
    resource_name: str,
) -> dict[str, str]:
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    uid = metadata.get("uid") if isinstance(metadata, dict) else None
    resource_version = (
        metadata.get("resourceVersion")
        if isinstance(metadata, dict)
        else None
    )
    if (
        not isinstance(uid, str)
        or not uid
        or not isinstance(resource_version, str)
        or not resource_version
    ):
        raise RuntimeErrorEB(
            f"functional serving {resource_name} revision is invalid"
        )
    return {
        "uid": uid,
        "resource_version": resource_version,
    }


def _httproute_attaches_to_experiment_gateway(
    route: Any,
    *,
    context: str,
) -> bool:
    if not isinstance(route, dict):
        raise RuntimeErrorEB(f"{context} payload is not an object")
    metadata = route.get("metadata")
    spec = route.get("spec")
    if not isinstance(metadata, dict) or not isinstance(spec, dict):
        raise RuntimeErrorEB(f"{context} metadata/spec is invalid")
    route_name = metadata.get("name")
    if (
        not isinstance(route_name, str)
        or not route_name
        or metadata.get("namespace") != APP_NAMESPACE
    ):
        raise RuntimeErrorEB(f"{context} identity is invalid")
    parent_refs = spec.get("parentRefs")
    if not isinstance(parent_refs, list):
        raise RuntimeErrorEB(f"{context} parentRef inventory is invalid")
    for parent_ref in parent_refs:
        if not isinstance(parent_ref, dict):
            raise RuntimeErrorEB(f"{context} parentRef inventory is invalid")
        if (
            str(parent_ref.get("group") or "gateway.networking.k8s.io")
            == "gateway.networking.k8s.io"
            and str(parent_ref.get("kind") or "Gateway") == "Gateway"
            and str(parent_ref.get("namespace") or APP_NAMESPACE)
            == APP_NAMESPACE
            and parent_ref.get("name") == "commonthing-experiment-b"
        ):
            return True
    return False


def _gateway_httproute_attachment_binding(
    root: Path,
    canonical_route_uid: str,
) -> dict[str, Any]:
    if not isinstance(canonical_route_uid, str) or not canonical_route_uid:
        raise RuntimeErrorEB(
            "functional serving canonical HTTPRoute UID is invalid"
        )
    readback = _httproute_collection_json(root)
    list_metadata = (
        readback.get("metadata")
        if isinstance(readback, dict)
        else None
    )
    resource_version = (
        list_metadata.get("resourceVersion")
        if isinstance(list_metadata, dict)
        else None
    )
    items = readback.get("items") if isinstance(readback, dict) else None
    if (
        not isinstance(resource_version, str)
        or not resource_version
        or not isinstance(items, list)
        or any(not isinstance(item, dict) for item in items)
    ):
        raise RuntimeErrorEB(
            "functional serving HTTPRoute attachment inventory is invalid"
        )

    attached: dict[str, dict[str, str]] = {}
    for item in items:
        metadata = item.get("metadata")
        route_name = (
            metadata.get("name")
            if isinstance(metadata, dict)
            else None
        )
        if (
            not isinstance(metadata, dict)
            or not isinstance(route_name, str)
            or not route_name
            or metadata.get("namespace") != APP_NAMESPACE
        ):
            raise RuntimeErrorEB(
                "functional serving HTTPRoute attachment inventory is invalid"
            )
        if not _httproute_attaches_to_experiment_gateway(
            item,
            context=f"HTTPRoute inventory {route_name}",
        ):
            continue
        if metadata.get("deletionTimestamp") is not None:
            raise RuntimeErrorEB(
                "functional serving HTTPRoute attachment inventory drifted"
            )
        if route_name in attached:
            raise RuntimeErrorEB(
                "functional serving HTTPRoute attachment inventory is invalid"
            )
        revision = _kubernetes_object_revision(
            item,
            f"HTTPRoute attachment {route_name}",
        )
        attached[route_name] = revision

    canonical = attached.get("commonthing-experiment-b")
    if (
        set(attached) != {"commonthing-experiment-b"}
        or not isinstance(canonical, dict)
        or canonical.get("uid") != canonical_route_uid
    ):
        raise RuntimeErrorEB(
            "functional serving HTTPRoute attachment inventory drifted"
        )
    return {
        "resource_version": resource_version,
        "routes": dict(sorted(attached.items())),
    }


def _application_service_endpoint_binding(
    root: Path,
    service_name: str,
    pod_identities: dict[str, Any],
) -> dict[str, Any]:
    if service_name not in {"weltgewebe-api", "weltgewebe-web"}:
        raise RuntimeErrorEB(
            f"functional serving Service endpoint binding is invalid: {service_name}"
        )
    if not isinstance(pod_identities, dict) or not pod_identities:
        raise RuntimeErrorEB(
            f"functional serving Service endpoint Pod set is invalid: {service_name}"
        )

    expected: dict[str, dict[str, str]] = {}
    for pod_name, identity in pod_identities.items():
        if (
            not isinstance(pod_name, str)
            or not pod_name
            or not isinstance(identity, dict)
            or identity.get("pod_name") != pod_name
            or not isinstance(identity.get("pod_uid"), str)
            or not identity["pod_uid"]
            or not isinstance(identity.get("pod_ip"), str)
            or not identity["pod_ip"]
        ):
            raise RuntimeErrorEB(
                f"functional serving Service endpoint Pod identity is invalid: {service_name}"
            )
        try:
            address = str(ipaddress.ip_address(identity["pod_ip"]))
        except ValueError as exc:
            raise RuntimeErrorEB(
                f"functional serving Service endpoint Pod address is invalid: {service_name}"
            ) from exc
        expected[pod_name] = {
            "pod_uid": identity["pod_uid"],
            "address": address,
        }

    readback = _endpoint_slice_collection_json(
        root,
        APP_NAMESPACE,
        service_name,
    )
    items = readback.get("items") if isinstance(readback, dict) else None
    list_metadata = (
        readback.get("metadata")
        if isinstance(readback, dict)
        else None
    )
    resource_version = (
        list_metadata.get("resourceVersion")
        if isinstance(list_metadata, dict)
        else None
    )
    if (
        not isinstance(resource_version, str)
        or not resource_version
        or not isinstance(items, list)
        or not items
        or any(not isinstance(item, dict) for item in items)
    ):
        raise RuntimeErrorEB(
            f"functional serving Service EndpointSlice inventory is invalid: {service_name}"
        )

    observed: dict[str, dict[str, str]] = {}
    object_revisions: dict[str, dict[str, str]] = {}
    for item in items:
        metadata = item.get("metadata")
        item_name = metadata.get("name") if isinstance(metadata, dict) else None
        labels = metadata.get("labels") if isinstance(metadata, dict) else None
        endpoints = item.get("endpoints")
        address_type = item.get("addressType")
        if (
            not isinstance(metadata, dict)
            or not isinstance(item_name, str)
            or not item_name
            or item_name in object_revisions
            or metadata.get("namespace") != APP_NAMESPACE
            or metadata.get("deletionTimestamp") is not None
            or not isinstance(labels, dict)
            or labels.get("kubernetes.io/service-name") != service_name
            or address_type not in {"IPv4", "IPv6"}
            or not isinstance(endpoints, list)
        ):
            raise RuntimeErrorEB(
                f"functional serving Service EndpointSlice contract is invalid: {service_name}"
            )
        object_revisions[item_name] = _kubernetes_object_revision(
            item,
            f"EndpointSlice {service_name}/{item_name}",
        )
        for endpoint in endpoints:
            conditions = (
                endpoint.get("conditions")
                if isinstance(endpoint, dict)
                else None
            )
            target_ref = (
                endpoint.get("targetRef")
                if isinstance(endpoint, dict)
                else None
            )
            addresses = (
                endpoint.get("addresses")
                if isinstance(endpoint, dict)
                else None
            )
            pod_name = (
                target_ref.get("name")
                if isinstance(target_ref, dict)
                else None
            )
            expected_endpoint = expected.get(pod_name)
            if (
                not isinstance(conditions, dict)
                or conditions.get("ready") is not True
                or conditions.get("terminating") is True
                or conditions.get("serving") is False
                or not isinstance(target_ref, dict)
                or target_ref.get("kind") != "Pod"
                or target_ref.get("namespace") != APP_NAMESPACE
                or not isinstance(pod_name, str)
                or expected_endpoint is None
                or target_ref.get("uid") != expected_endpoint["pod_uid"]
                or pod_name in observed
                or not isinstance(addresses, list)
                or len(addresses) != 1
                or not isinstance(addresses[0], str)
            ):
                raise RuntimeErrorEB(
                    f"functional serving Service endpoint target is invalid: {service_name}"
                )
            try:
                address = str(ipaddress.ip_address(addresses[0]))
            except ValueError as exc:
                raise RuntimeErrorEB(
                    f"functional serving Service endpoint address is invalid: {service_name}"
                ) from exc
            expected_address_type = (
                "IPv4"
                if ipaddress.ip_address(address).version == 4
                else "IPv6"
            )
            if (
                address_type != expected_address_type
                or address != expected_endpoint["address"]
            ):
                raise RuntimeErrorEB(
                    f"functional serving Service endpoint address drifted: {service_name}"
                )
            observed[pod_name] = {
                "pod_uid": expected_endpoint["pod_uid"],
                "address": address,
            }

    if observed != expected:
        raise RuntimeErrorEB(
            f"functional serving Service endpoint set drifted: {service_name}"
        )
    normalized = dict(sorted(observed.items()))
    return {
        "pods": normalized,
        "sha256": _stable_json_sha256(normalized),
        "resource_version": resource_version,
        "object_revisions": dict(sorted(object_revisions.items())),
    }


def _functional_serving_runtime_semantic_binding(
    binding: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(binding, dict):
        raise RuntimeErrorEB(
            "functional serving runtime semantic binding is invalid"
        )
    normalized = json.loads(json.dumps(binding))
    services = normalized.get("services")
    if isinstance(services, dict):
        for service in services.values():
            endpoints = (
                service.get("endpoints")
                if isinstance(service, dict)
                else None
            )
            if isinstance(endpoints, dict):
                endpoints.pop("resource_version", None)
                revisions = endpoints.get("object_revisions")
                if isinstance(revisions, dict):
                    for revision in revisions.values():
                        if isinstance(revision, dict):
                            revision.pop("resource_version", None)
    for resource_name in ("gateway", "httproute"):
        resource = normalized.get(resource_name)
        if isinstance(resource, dict):
            resource.pop("resource_version", None)
    httproute_inventory = normalized.get("httproute_inventory")
    if isinstance(httproute_inventory, dict):
        httproute_inventory.pop("resource_version", None)
        routes = httproute_inventory.get("routes")
        if isinstance(routes, dict):
            for route in routes.values():
                if isinstance(route, dict):
                    route.pop("resource_version", None)
    return normalized


@contextmanager
def _guard_functional_service_endpoints(
    root: Path,
    serving_runtime: dict[str, Any],
) -> Iterator[None]:
    services = (
        serving_runtime.get("services")
        if isinstance(serving_runtime, dict)
        else None
    )
    if not isinstance(services, dict):
        raise RuntimeErrorEB(
            "functional serving dependency replay has no Service bindings"
        )

    dependencies: list[
        tuple[str, str, str | None, str | None, str]
    ] = []
    endpoint_path = (
        f"/apis/discovery.k8s.io/v1/namespaces/"
        f"{urllib.parse.quote(APP_NAMESPACE, safe='')}/endpointslices"
    )
    for service_name in ("weltgewebe-api", "weltgewebe-web"):
        service = services.get(service_name)
        endpoints = (
            service.get("endpoints")
            if isinstance(service, dict)
            else None
        )
        resource_version = (
            endpoints.get("resource_version")
            if isinstance(endpoints, dict)
            else None
        )
        if not isinstance(resource_version, str) or not resource_version:
            raise RuntimeErrorEB(
                f"functional serving dependency replay has no resourceVersion: "
                f"{service_name}"
            )
        dependencies.append(
            (
                service_name,
                resource_version,
                "labelSelector",
                f"kubernetes.io/service-name={service_name}",
                endpoint_path,
            )
        )

    namespace_path = urllib.parse.quote(APP_NAMESPACE, safe="")
    gateway = serving_runtime.get("gateway")
    if not isinstance(gateway, dict):
        raise RuntimeErrorEB(
            "functional serving dependency replay has no Gateway binding"
        )
    gateway_collection = _gateway_collection_json(root)
    gateway_collection_metadata = gateway_collection.get("metadata")
    gateway_items = gateway_collection.get("items")
    gateway_resource_version = (
        gateway_collection_metadata.get("resourceVersion")
        if isinstance(gateway_collection_metadata, dict)
        else None
    )
    if (
        not isinstance(gateway_resource_version, str)
        or not gateway_resource_version
        or not isinstance(gateway_items, list)
        or len(gateway_items) != 1
        or not isinstance(gateway_items[0], dict)
    ):
        raise RuntimeErrorEB(
            "functional serving dependency replay Gateway snapshot is invalid"
        )
    gateway_snapshot = gateway_items[0]
    gateway_snapshot_revision = _kubernetes_object_revision(
        gateway_snapshot,
        "Gateway collection snapshot",
    )
    gateway_snapshot_semantic = _require_gateway_ready(gateway_snapshot)
    expected_gateway_semantic = {
        field: gateway.get(field)
        for field in (
            "generation",
            "gateway_class",
            "listener",
            "programmed",
        )
    }
    if (
        gateway_snapshot_revision["uid"] != gateway.get("uid")
        or gateway_snapshot_revision["resource_version"]
        != gateway.get("resource_version")
        or gateway_snapshot_semantic != expected_gateway_semantic
    ):
        raise RuntimeErrorEB(
            "functional serving dependency replay Gateway snapshot drifted"
        )
    dependencies.append(
        (
            "Gateway",
            gateway_resource_version,
            "fieldSelector",
            "metadata.name=commonthing-experiment-b",
            (
                f"/apis/gateway.networking.k8s.io/v1/namespaces/"
                f"{namespace_path}/gateways"
            ),
        )
    )

    httproute_inventory = serving_runtime.get("httproute_inventory")
    httproute_resource_version = (
        httproute_inventory.get("resource_version")
        if isinstance(httproute_inventory, dict)
        else None
    )
    if (
        not isinstance(httproute_resource_version, str)
        or not httproute_resource_version
    ):
        raise RuntimeErrorEB(
            "functional serving dependency replay has no resourceVersion: "
            "HTTPRoute inventory"
        )
    dependencies.append(
        (
            "HTTPRoute",
            httproute_resource_version,
            None,
            None,
            (
                f"/apis/gateway.networking.k8s.io/v1/namespaces/"
                f"{namespace_path}/httproutes"
            ),
        )
    )

    yield

    kubectl = toolchain(root)["tools"]["kubectl"]
    for (
        subject,
        resource_version,
        selector_name,
        selector_value,
        collection_path,
    ) in dependencies:
        watch_parameters = {
            "watch": "1",
            "resourceVersion": resource_version,
            "allowWatchBookmarks": "true",
            "timeoutSeconds": "2",
        }
        if selector_name is not None:
            if selector_value is None:
                raise RuntimeErrorEB(
                    f"functional serving dependency replay selector is invalid: "
                    f"{subject}"
                )
            watch_parameters[selector_name] = selector_value
        watch_query = urllib.parse.urlencode(watch_parameters)
        result = run(
            [
                kubectl,
                "get",
                "--raw",
                f"{collection_path}?{watch_query}",
            ],
            env=kube_env(root),
            timeout=10,
        )
        for raw_event in (result.stdout or "").splitlines():
            if not raw_event.strip():
                continue
            try:
                event = json.loads(raw_event)
            except json.JSONDecodeError as exc:
                raise RuntimeErrorEB(
                    f"functional serving dependency replay is invalid: {subject}"
                ) from exc
            if (
                not isinstance(event, dict)
                or not isinstance(event.get("type"), str)
                or not isinstance(event.get("object"), dict)
            ):
                raise RuntimeErrorEB(
                    f"functional serving dependency replay event is invalid: "
                    f"{subject}"
                )
            if event["type"] == "BOOKMARK":
                continue
            if subject == "HTTPRoute":
                route = event["object"]
                metadata = route.get("metadata")
                route_name = (
                    metadata.get("name")
                    if isinstance(metadata, dict)
                    else None
                )
                if (
                    route_name == "commonthing-experiment-b"
                    or _httproute_attaches_to_experiment_gateway(
                        route,
                        context="HTTPRoute watch event",
                    )
                ):
                    raise RuntimeErrorEB(
                        "functional serving dependency changed during Gateway "
                        "probes: HTTPRoute"
                    )
                continue
            raise RuntimeErrorEB(
                f"functional serving dependency changed during Gateway probes: "
                f"{subject}"
            )


def _functional_serving_runtime_binding(
    root: Path,
    source_commit: str,
) -> dict[str, Any]:
    if not COMMIT_RE.fullmatch(source_commit):
        raise RuntimeErrorEB(
            "functional serving runtime source commit is not exact"
        )
    release_path = root / "receipts/release.json"
    try:
        release = json.loads(release_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeErrorEB(
            "functional serving runtime requires a valid release receipt"
        ) from exc
    api_digest = release.get("api_digest") if isinstance(release, dict) else None
    web_digest = release.get("web_digest") if isinstance(release, dict) else None
    if (
        not isinstance(release, dict)
        or release.get("schema_version") != 1
        or release.get("status") != "applied"
        or release.get("source_commit") != source_commit
        or not isinstance(api_digest, str)
        or not DIGEST_RE.fullmatch(api_digest)
        or not isinstance(web_digest, str)
        or not DIGEST_RE.fullmatch(web_digest)
    ):
        raise RuntimeErrorEB(
            "functional serving runtime release binding is invalid"
        )

    config = _source_commit_config(source_commit)
    api_replicas = int(config["semantic_search"]["api_replicas"])
    web_replicas = int(config["runtime_binding"]["web_replicas"])
    api = _kubectl_json(
        root,
        ["-n", APP_NAMESPACE, "get", "deployment", "weltgewebe-api"],
    )
    web = _kubectl_json(
        root,
        ["-n", APP_NAMESPACE, "get", "deployment", "weltgewebe-web"],
    )
    pod_items = _kubectl_json(
        root,
        ["-n", APP_NAMESPACE, "get", "pods"],
    ).get("items")
    if not isinstance(pod_items, list) or any(
        not isinstance(item, dict) for item in pod_items
    ):
        raise RuntimeErrorEB(
            "functional serving runtime Pod inventory is invalid"
        )
    pods_by_workload = {
        "weltgewebe-api": _pods_matching_labels(
            pod_items,
            {"app.kubernetes.io/name": "weltgewebe-api"},
        ),
        "weltgewebe-web": _pods_matching_labels(
            pod_items,
            {"app.kubernetes.io/name": "weltgewebe-web"},
        ),
    }
    workloads = _require_live_application_workloads(
        root,
        release,
        {
            "weltgewebe-api": api,
            "weltgewebe-web": web,
        },
        pods_by_workload,
    )

    semantic = config["semantic_search"]
    expected_images = {
        "weltgewebe-api": {
            "api": f"ghcr.io/heimgewebe/commonthing-api@{api_digest}",
            "search-worker": (
                f"ghcr.io/heimgewebe/commonthing-api@{api_digest}"
            ),
            "ollama": str(semantic["ollama_image"]),
        },
        "weltgewebe-web": {
            "web": f"ghcr.io/heimgewebe/commonthing-web@{web_digest}",
        },
    }
    pod_readback = {
        "weltgewebe-api": _require_running_pod_images(
            pods_by_workload["weltgewebe-api"],
            "weltgewebe-api",
            api_replicas,
            expected_images["weltgewebe-api"],
        ),
        "weltgewebe-web": _require_running_pod_images(
            pods_by_workload["weltgewebe-web"],
            "weltgewebe-web",
            web_replicas,
            expected_images["weltgewebe-web"],
        ),
    }

    workload_binding: dict[str, Any] = {}
    for workload_name in ("weltgewebe-api", "weltgewebe-web"):
        workload = workloads.get(workload_name)
        image_binding = pod_readback.get(workload_name)
        if (
            not isinstance(workload, dict)
            or workload.get("canonical") is not True
            or not isinstance(workload.get("contract_sha256"), str)
            or not workload["contract_sha256"]
            or not isinstance(workload.get("pod_contract_sha256"), str)
            or not workload["pod_contract_sha256"]
            or not isinstance(image_binding, dict)
            or image_binding.get("images_canonical") is not True
            or not isinstance(
                image_binding.get("requested_images_sha256"), str
            )
            or not image_binding["requested_images_sha256"]
            or not isinstance(image_binding.get("pods"), dict)
        ):
            raise RuntimeErrorEB(
                f"functional serving runtime binding is invalid: "
                f"{workload_name}"
            )

        live_by_name: dict[str, dict[str, Any]] = {}
        for pod in pods_by_workload[workload_name]:
            metadata = pod.get("metadata")
            name = (
                metadata.get("name")
                if isinstance(metadata, dict)
                else None
            )
            if (
                not isinstance(name, str)
                or not name
                or name in live_by_name
            ):
                raise RuntimeErrorEB(
                    "functional serving runtime Pod identity is invalid"
                )
            live_by_name[name] = pod
        if set(live_by_name) != set(image_binding["pods"]):
            raise RuntimeErrorEB(
                f"functional serving runtime Pod set drifted: "
                f"{workload_name}"
            )

        pod_identities: dict[str, Any] = {}
        for pod_name in sorted(live_by_name):
            identity = _application_pod_runtime_identity(
                live_by_name[pod_name],
                set(expected_images[workload_name]),
                f"functional serving {workload_name}/{pod_name}",
            )
            image_identity = image_binding["pods"].get(pod_name)
            runtime_image_ids = (
                image_identity.get("runtime_image_ids")
                if isinstance(image_identity, dict)
                else None
            )
            if (
                not isinstance(runtime_image_ids, dict)
                or set(runtime_image_ids)
                != set(expected_images[workload_name])
                or any(
                    not isinstance(value, str) or not value
                    for value in runtime_image_ids.values()
                )
            ):
                raise RuntimeErrorEB(
                    f"functional serving runtime image identity is invalid: "
                    f"{workload_name}/{pod_name}"
                )
            identity["runtime_image_ids"] = dict(
                sorted(runtime_image_ids.items())
            )
            pod_identities[pod_name] = identity

        workload_binding[workload_name] = {
            "contract_sha256": workload["contract_sha256"],
            "pod_contract_sha256": workload["pod_contract_sha256"],
            "requested_images_sha256": image_binding[
                "requested_images_sha256"
            ],
            "pods": pod_identities,
        }

    services = _require_live_application_services(root, release)
    service_binding: dict[str, Any] = {}
    for name in ("weltgewebe-api", "weltgewebe-web"):
        value = services.get(name)
        spec_sha256 = (
            value.get("spec_sha256")
            if isinstance(value, dict)
            else None
        )
        if (
            not isinstance(value, dict)
            or value.get("canonical") is not True
            or not isinstance(spec_sha256, str)
            or not spec_sha256
        ):
            raise RuntimeErrorEB(
                f"functional serving Service binding is invalid: {name}"
            )
        endpoint_binding = _application_service_endpoint_binding(
            root,
            name,
            workload_binding[name]["pods"],
        )
        service_binding[name] = {
            "spec_sha256": spec_sha256,
            "endpoints": endpoint_binding,
        }

    gateway_object = _kubectl_json(
        root,
        [
            "-n",
            APP_NAMESPACE,
            "get",
            "gateway",
            "commonthing-experiment-b",
        ],
    )
    gateway_revision = _kubernetes_object_revision(
        gateway_object,
        "Gateway",
    )
    gateway_readback = {
        **_require_gateway_ready(gateway_object),
        "uid": gateway_revision["uid"],
        "resource_version": gateway_revision["resource_version"],
    }
    httproute_object = _kubectl_json(
        root,
        [
            "-n",
            APP_NAMESPACE,
            "get",
            "httproute",
            "commonthing-experiment-b",
        ],
    )
    httproute_revision = _kubernetes_object_revision(
        httproute_object,
        "HTTPRoute",
    )
    httproute_readback = {
        **_require_httproute_ready(httproute_object),
        "uid": httproute_revision["uid"],
        "resource_version": httproute_revision["resource_version"],
    }
    httproute_inventory = _gateway_httproute_attachment_binding(
        root,
        httproute_revision["uid"],
    )
    return {
        "workloads": workload_binding,
        "services": service_binding,
        "gateway": gateway_readback,
        "httproute": httproute_readback,
        "httproute_inventory": httproute_inventory,
        "gateway_base_url": _gateway_base_url(root, source_commit),
    }


def _gateway_data_plane_readback(
    root: Path, source_commit: str
) -> dict[str, Any]:
    if not COMMIT_RE.fullmatch(source_commit):
        raise RuntimeErrorEB("Gateway data-plane source commit is not exact")
    base = _gateway_base_url(root, source_commit)
    checks: dict[str, Any] = {}
    status_code, body, elapsed = _http_read(base + "/")
    checks["web_root"] = {"status": status_code, "elapsed_ms": elapsed}
    if status_code != 200 or not body:
        raise RuntimeErrorEB("Experiment-B web root is not readable through Gateway")

    status_code, body, elapsed = _http_read(base + "/_app/version.json")
    version = json.loads(body) if status_code == 200 else {}
    checks["web_revision"] = {
        "status": status_code,
        "elapsed_ms": elapsed,
        "commit": version.get("commit") if isinstance(version, dict) else None,
    }
    if status_code != 200 or version.get("commit") != source_commit:
        raise RuntimeErrorEB("Experiment-B Web revision does not match source commit")

    status_code, body, elapsed = _http_read(base + "/health/ready")
    health = json.loads(body) if status_code == 200 else {}
    checks["api_ready"] = {"status": status_code, "elapsed_ms": elapsed, "body": health}
    if status_code != 200 or health.get("status") != "ok":
        raise RuntimeErrorEB("Experiment-B API readiness failed through Gateway")

    status_code, body, elapsed = _http_read(
        base + "/api/nodes?pagination=cursor&limit=1"
    )
    try:
        nodes = (
            json.loads(
                body,
                parse_constant=_reject_nonstandard_json_constant,
            )
            if status_code == 200
            else {}
        )
    except (TypeError, ValueError):
        nodes = {}
    items = nodes.get("items") if isinstance(nodes, dict) else None
    page = nodes.get("page") if isinstance(nodes, dict) else None
    first_node = (
        items[0]
        if isinstance(items, list) and len(items) == 1
        else None
    )
    location = (
        first_node.get("location")
        if isinstance(first_node, dict)
        else None
    )
    has_more = page.get("has_more") if isinstance(page, dict) else None
    next_cursor = (
        page.get("next_cursor")
        if isinstance(page, dict)
        else None
    )
    longitude = location.get("lon") if isinstance(location, dict) else None
    latitude = location.get("lat") if isinstance(location, dict) else None
    page_limit = page.get("limit") if isinstance(page, dict) else None
    coordinates_valid = (
        isinstance(longitude, (int, float))
        and not isinstance(longitude, bool)
        and math.isfinite(float(longitude))
        and -180.0 <= float(longitude) <= 180.0
        and isinstance(latitude, (int, float))
        and not isinstance(latitude, bool)
        and math.isfinite(float(latitude))
        and -90.0 <= float(latitude) <= 90.0
    )
    node_contract_valid = (
        isinstance(first_node, dict)
        and all(
            isinstance(first_node.get(field), str)
            and bool(first_node[field])
            for field in (
                "id",
                "kind",
                "title",
                "created_at",
                "updated_at",
            )
        )
        and isinstance(location, dict)
        and coordinates_valid
    )
    page_contract_valid = (
        isinstance(page, dict)
        and type(page_limit) is int
        and page_limit == 1
        and isinstance(has_more, bool)
        and (
            (has_more and isinstance(next_cursor, str) and bool(next_cursor))
            or (not has_more and next_cursor is None)
        )
    )
    checks["domain_nodes"] = {
        "status": status_code,
        "elapsed_ms": elapsed,
        "items": len(items) if isinstance(items, list) else None,
        "first_node_id": (
            first_node.get("id")
            if isinstance(first_node, dict)
            else None
        ),
        "has_more": has_more,
    }
    if (
        status_code != 200
        or not isinstance(items, list)
        or len(items) != 1
        or not node_contract_valid
        or not page_contract_valid
    ):
        raise RuntimeErrorEB("Experiment-B domain read failed through Gateway")

    source_config = _source_commit_config(source_commit)
    semantic_search = (
        source_config.get("semantic_search")
        if isinstance(source_config, dict)
        else None
    )
    expected_generation_id = (
        semantic_search.get("generation_id")
        if isinstance(semantic_search, dict)
        else None
    )
    if (
        not isinstance(expected_generation_id, str)
        or not expected_generation_id
    ):
        raise RuntimeErrorEB(
            "Experiment-B search generation source binding is invalid"
        )

    query = urllib.parse.urlencode({"q": "scale", "limit": 5})
    status_code, body, elapsed = _http_read(base + "/api/search?" + query)
    try:
        search = (
            json.loads(
                body,
                parse_constant=_reject_nonstandard_json_constant,
            )
            if status_code == 200
            else {}
        )
    except (TypeError, ValueError):
        search = {}
    items = search.get("items") if isinstance(search, dict) else None
    search_mode = search.get("mode") if isinstance(search, dict) else None
    generation_id = (
        search.get("generation_id")
        if isinstance(search, dict)
        else None
    )
    search_offset = (
        search.get("offset")
        if isinstance(search, dict)
        else None
    )

    def search_item_contract_valid(item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        location = item.get("location")
        if not isinstance(location, dict):
            return False
        longitude = location.get("lon")
        latitude = location.get("lat")
        if (
            not isinstance(longitude, (int, float))
            or isinstance(longitude, bool)
            or not math.isfinite(float(longitude))
            or not -180.0 <= float(longitude) <= 180.0
            or not isinstance(latitude, (int, float))
            or isinstance(latitude, bool)
            or not math.isfinite(float(latitude))
            or not -90.0 <= float(latitude) <= 90.0
        ):
            return False
        if item.get("search_visibility") != "public":
            return False
        tags = item.get("tags")
        if tags is not None and (
            not isinstance(tags, list)
            or not all(isinstance(tag, str) for tag in tags)
        ):
            return False
        for optional_field in (
            "created_by_account_id",
            "summary",
            "info",
            "address",
        ):
            optional_value = item.get(optional_field)
            if optional_value is not None and not isinstance(
                optional_value,
                str,
            ):
                return False
        return all(
            isinstance(item.get(field), str) and bool(item[field])
            for field in (
                "id",
                "kind",
                "title",
                "created_at",
                "updated_at",
            )
        )

    fallback_reason = (
        search.get("fallback_reason")
        if isinstance(search, dict)
        else None
    )
    fallback_contract_valid = (
        (search_mode == "hybrid" and fallback_reason is None)
        or (
            search_mode == "lexical_fallback"
            and isinstance(fallback_reason, str)
            and bool(fallback_reason)
        )
    )
    search_contract_valid = (
        isinstance(search, dict)
        and isinstance(items, list)
        and 0 < len(items) <= 5
        and search_mode in {"hybrid", "lexical_fallback"}
        and fallback_contract_valid
        and generation_id == expected_generation_id
        and type(search_offset) is int
        and search_offset == 0
        and all(search_item_contract_valid(item) for item in items)
    )
    checks["search"] = {
        "status": status_code,
        "elapsed_ms": elapsed,
        "generation_id": generation_id,
        "mode": search_mode,
        "offset": search_offset,
        "items": len(items) if isinstance(items, list) else None,
    }
    if status_code != 200 or not search_contract_valid:
        raise RuntimeErrorEB("Experiment-B search failed through Gateway")

    status_code, body, elapsed = _http_read(base + "/api/auth/me")
    auth = json.loads(body) if status_code == 200 else {}
    checks["anonymous_auth_boundary"] = {
        "status": status_code,
        "elapsed_ms": elapsed,
        "authenticated": auth.get("authenticated") if isinstance(auth, dict) else None,
        "role": auth.get("role") if isinstance(auth, dict) else None,
    }
    if (
        status_code != 200
        or auth.get("authenticated") is not False
        or auth.get("role") != "gast"
    ):
        raise RuntimeErrorEB("Experiment-B anonymous auth boundary is not fail-closed")

    return {"gateway": base, "checks": checks}


@_serialize_experiment_b_lifecycle
def functional_readback(root: Path, source_commit: str) -> dict[str, Any]:
    if not COMMIT_RE.fullmatch(source_commit):
        raise RuntimeErrorEB("functional readback source commit is not exact")
    receipt_path, attempt_path, attempt_started_at_unix_ms = (
        _begin_live_check_attempt(root, "functional-readback", source_commit)
    )
    if _current_protected_main_commit() != source_commit:
        raise RuntimeErrorEB("functional readback source is not current protected main")
    (
        target_receipt_before,
        target_ip_before,
        target_server_before,
    ) = _require_kubernetes_target_binding(root, source_commit)
    target_binding_before = {
        "vm_ip": target_ip_before,
        "kubeconfig_sha256": target_receipt_before["kubeconfig_sha256"],
        "server": target_server_before,
    }
    with _bound_kube_env(
        root,
        target_binding_before,
        source_commit,
    ):
        serving_runtime_before = _functional_serving_runtime_binding(
            root,
            source_commit,
        )
        serving_runtime_semantic_before = (
            _functional_serving_runtime_semantic_binding(
                serving_runtime_before
            )
        )
        with _guard_functional_service_endpoints(
            root,
            serving_runtime_before,
        ):
            serving_runtime_probe_start = (
                _functional_serving_runtime_binding(
                    root,
                    source_commit,
                )
            )
            serving_runtime_semantic_probe_start = (
                _functional_serving_runtime_semantic_binding(
                    serving_runtime_probe_start
                )
            )
            if (
                serving_runtime_semantic_probe_start
                != serving_runtime_semantic_before
            ):
                raise RuntimeErrorEB(
                    "application serving runtime changed before Gateway probes"
                )
            data_plane = _gateway_data_plane_readback(
                root,
                source_commit,
            )
            serving_runtime_after = _functional_serving_runtime_binding(
                root,
                source_commit,
            )
            serving_runtime_semantic_after = (
                _functional_serving_runtime_semantic_binding(
                    serving_runtime_after
                )
            )
            if (
                serving_runtime_semantic_after
                != serving_runtime_semantic_probe_start
            ):
                raise RuntimeErrorEB(
                    "application serving runtime changed during functional readback"
                )
        base = str(data_plane["gateway"])
        checks = data_plane["checks"]
        nats_binding_before = _require_nats_runtime_binding(
            root,
            source_commit,
        )
        jetstream = _jetstream_signature(
            root,
            source_commit=source_commit,
            nats_binding=nats_binding_before,
        )
        nats_binding_after = _require_nats_runtime_binding(
            root,
            source_commit,
        )
        if (
            _nats_runtime_binding_identity(nats_binding_after)
            != _nats_runtime_binding_identity(nats_binding_before)
        ):
            raise RuntimeErrorEB(
                "NATS runtime changed during functional readback"
            )
        if jetstream["messages"] < 1:
            raise RuntimeErrorEB(
                "Experiment-B JetStream contains no persisted test messages"
            )
        (
            target_receipt_after,
            target_ip_after,
            target_server_after,
        ) = _require_kubernetes_target_binding(root, source_commit)
        target_binding_after = {
            "vm_ip": target_ip_after,
            "kubeconfig_sha256": target_receipt_after["kubeconfig_sha256"],
            "server": target_server_after,
        }
        if target_binding_after != target_binding_before:
            raise RuntimeErrorEB(
                "Kubernetes target identity changed during functional readback"
            )
    receipt = {
        "schema_version": 1,
        "status": "pass",
        "source_commit": source_commit,
        "gateway": base,
        "checks": checks,
        "jetstream": jetstream,
        "kubernetes_target_sha256": _stable_json_sha256(
            target_binding_before
        ),
        "serving_runtime_sha256": _stable_json_sha256(
            serving_runtime_semantic_probe_start
        ),
        "nats_runtime_sha256": _stable_json_sha256(
            _nats_runtime_binding_identity(nats_binding_before)
        ),
        "production_endpoint_used": False,
    }
    atomic_json(receipt_path, receipt)
    _complete_live_check_attempt(
        attempt_path,
        receipt_path,
        source_commit,
        attempt_started_at_unix_ms,
        "pass",
    )
    return receipt





def _normalized_database_schema_dump(schema_bytes: bytes) -> bytes:
    try:
        schema_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeErrorEB(
            "database schema signature is not UTF-8"
        ) from exc

    lines = schema_bytes.split(b"\n")
    restrict_prefix = b"\\restrict "
    unrestrict_prefix = b"\\unrestrict "

    leading_restrict_index: int | None = None
    for index, line in enumerate(lines):
        if line.startswith(restrict_prefix):
            leading_restrict_index = index
            break
        if line and not line.startswith(b"--"):
            raise RuntimeErrorEB(
                "database schema signature is missing leading pg_dump restrict marker"
            )
    if leading_restrict_index is None:
        raise RuntimeErrorEB(
            "database schema signature is missing leading pg_dump restrict marker"
        )

    trailing_unrestrict_index = len(lines) - 1
    while (
        trailing_unrestrict_index >= 0
        and not lines[trailing_unrestrict_index]
    ):
        trailing_unrestrict_index -= 1
    if (
        trailing_unrestrict_index <= leading_restrict_index
        or not lines[trailing_unrestrict_index].startswith(unrestrict_prefix)
    ):
        raise RuntimeErrorEB(
            "database schema signature is missing trailing pg_dump unrestrict marker"
        )

    restrict_token = lines[leading_restrict_index][len(restrict_prefix):]
    unrestrict_token = lines[trailing_unrestrict_index][len(unrestrict_prefix):]
    ascii_whitespace = b" \t\r\n\v\f"
    if (
        not restrict_token
        or not unrestrict_token
        or not restrict_token.isascii()
        or not unrestrict_token.isascii()
        or any(byte in ascii_whitespace for byte in restrict_token)
        or any(byte in ascii_whitespace for byte in unrestrict_token)
        or restrict_token != unrestrict_token
    ):
        raise RuntimeErrorEB(
            "database schema signature has invalid pg_dump restrict markers"
        )

    lines[leading_restrict_index] = b"\\restrict <pg-dump-key>"
    lines[trailing_unrestrict_index] = b"\\unrestrict <pg-dump-key>"
    return b"\n".join(lines)


def _restore_stable_database_schema_sha256(
    root: Path,
    source_commit: str,
    postgres_binding: dict[str, Any],
    database_identity: tuple[str, str],
) -> str:
    if COMMIT_RE.fullmatch(source_commit) is None:
        raise RuntimeErrorEB(
            "restore-stable schema signature requires exact source commit"
        )
    username, database = database_identity
    if not username or not database:
        raise RuntimeErrorEB(
            "restore-stable schema signature requires database identity"
        )
    scratch_database = f"commonthing_schema_signature_{uuid.uuid4().hex}"
    if len(scratch_database) > 63 or re.fullmatch(
        r"[a-z0-9_]+",
        scratch_database,
    ) is None:
        raise RuntimeErrorEB(
            "restore-stable schema signature scratch database name is invalid"
        )

    schema_archive = _run_bound_postgres_client(
        root,
        source_commit,
        postgres_binding,
        [
            *_database_client_argv("pg_dump", database_identity),
            "-Fc",
            "--schema-only",
            "--no-owner",
            "--no-privileges",
        ],
        timeout=900,
    )
    if not schema_archive:
        raise RuntimeErrorEB(
            "restore-stable schema signature archive is empty"
        )

    scratch_identity = (username, scratch_database)
    create_sql = (
        f'CREATE DATABASE "{scratch_database}" TEMPLATE template0;\n'
    ).encode("utf-8")
    drop_sql = (
        f'DROP DATABASE IF EXISTS "{scratch_database}" WITH (FORCE);\n'
    ).encode("utf-8")
    control_command = [
        *_database_client_argv("psql", database_identity),
        "-v",
        "ON_ERROR_STOP=1",
        "-qAt",
    ]

    scratch_created = False
    try:
        _run_bound_postgres_client(
            root,
            source_commit,
            postgres_binding,
            control_command,
            input_bytes=create_sql,
            timeout=120,
        )
        scratch_created = True
        _run_bound_postgres_client(
            root,
            source_commit,
            postgres_binding,
            [
                *_database_client_argv("pg_restore", scratch_identity),
                "--schema-only",
                "--no-owner",
                "--no-privileges",
                "--exit-on-error",
            ],
            input_bytes=schema_archive,
            timeout=900,
        )
        canonical_schema = _run_bound_postgres_client(
            root,
            source_commit,
            postgres_binding,
            [
                *_database_client_argv("pg_dump", scratch_identity),
                "--schema-only",
                "--no-owner",
                "--no-privileges",
                "--quote-all-identifiers",
            ],
            timeout=900,
        )
        return hashlib.sha256(
            _normalized_database_schema_dump(canonical_schema)
        ).hexdigest()
    finally:
        if scratch_created:
            _run_bound_postgres_client(
                root,
                source_commit,
                postgres_binding,
                control_command,
                input_bytes=drop_sql,
                timeout=120,
            )


def _database_signature(
    root: Path,
    *,
    database_identity: tuple[str, str] | None = None,
    source_commit: str | None = None,
    postgres_binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sql = r"""
CREATE TEMP TABLE commonthing_signature_tables (
  schema_name text NOT NULL,
  relation_name text NOT NULL,
  row_count bigint NOT NULL,
  row_md5 text NOT NULL
);

DO $commonthing$
DECLARE
  item record;
  observed_count bigint;
  observed_md5 text;
BEGIN
  FOR item IN
    SELECT n.nspname AS schema_name, c.relname AS relation_name
      FROM pg_catalog.pg_class c
      JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     WHERE c.relkind IN ('r', 'p')
       AND n.nspname NOT IN ('pg_catalog', 'information_schema')
       AND n.nspname !~ '^pg_toast'
       AND n.nspname !~ '^pg_temp_'
     ORDER BY n.nspname, c.relname
  LOOP
    EXECUTE format(
      'SELECT count(*), md5(coalesce(string_agg(md5(to_jsonb(t)::text), '''' '
      'ORDER BY md5(to_jsonb(t)::text), to_jsonb(t)::text), '''')) '
      'FROM %I.%I AS t',
      item.schema_name,
      item.relation_name
    )
    INTO observed_count, observed_md5;

    INSERT INTO commonthing_signature_tables
      (schema_name, relation_name, row_count, row_md5)
    VALUES
      (item.schema_name, item.relation_name, observed_count, observed_md5);
  END LOOP;
END
$commonthing$;

CREATE TEMP TABLE commonthing_signature_sequences (
  schema_name text NOT NULL,
  sequence_name text NOT NULL,
  last_value text,
  is_called boolean NOT NULL,
  start_value text NOT NULL,
  increment_by text NOT NULL,
  min_value text NOT NULL,
  max_value text NOT NULL,
  cache_size text NOT NULL,
  cycle boolean NOT NULL
);

DO $commonthing$
DECLARE
  item record;
  observed_last text;
  observed_called boolean;
BEGIN
  FOR item IN
    SELECT
      n.nspname AS schema_name,
      c.relname AS sequence_name,
      s.seqstart::text AS start_value,
      s.seqincrement::text AS increment_by,
      s.seqmin::text AS min_value,
      s.seqmax::text AS max_value,
      s.seqcache::text AS cache_size,
      s.seqcycle AS cycle
    FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_catalog.pg_sequence s ON s.seqrelid = c.oid
    WHERE c.relkind = 'S'
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname !~ '^pg_toast'
      AND n.nspname !~ '^pg_temp_'
    ORDER BY n.nspname, c.relname
  LOOP
    EXECUTE format(
      'SELECT last_value::text, is_called FROM %I.%I',
      item.schema_name,
      item.sequence_name
    )
    INTO observed_last, observed_called;

    INSERT INTO commonthing_signature_sequences (
      schema_name,
      sequence_name,
      last_value,
      is_called,
      start_value,
      increment_by,
      min_value,
      max_value,
      cache_size,
      cycle
    )
    VALUES (
      item.schema_name,
      item.sequence_name,
      observed_last,
      observed_called,
      item.start_value,
      item.increment_by,
      item.min_value,
      item.max_value,
      item.cache_size,
      item.cycle
    );
  END LOOP;
END
$commonthing$;

SELECT json_build_object(
  'tables',
  COALESCE(
    (
      SELECT json_agg(
        json_build_object(
          'schema', schema_name,
          'name', relation_name,
          'rows', row_count,
          'md5', row_md5
        )
        ORDER BY schema_name, relation_name
      )
      FROM commonthing_signature_tables
    ),
    '[]'::json
  ),
  'sequences',
  COALESCE(
    (
      SELECT json_agg(
        json_build_object(
          'schema', schema_name,
          'name', sequence_name,
          'last_value', last_value,
          'is_called', is_called,
          'start_value', start_value,
          'increment_by', increment_by,
          'min_value', min_value,
          'max_value', max_value,
          'cache_size', cache_size,
          'cycle', cycle
        )
        ORDER BY schema_name, sequence_name
      )
      FROM commonthing_signature_sequences
    ),
    '[]'::json
  )
)::text;
"""
    identity = (
        _database_client_identity(root)
        if database_identity is None
        else database_identity
    )
    if postgres_binding is None:
        raw = _psql(
            root,
            sql,
            database_identity=identity,
        )
        schema_sha256: str | None = None
    else:
        if source_commit is None:
            raise RuntimeErrorEB(
                "bound database signature requires source commit"
            )
        raw_bytes = _run_bound_postgres_client(
            root,
            source_commit,
            postgres_binding,
            [
                *_database_client_argv("psql", identity),
                "-v",
                "ON_ERROR_STOP=1",
                "-qAt",
            ],
            input_bytes=sql.encode("utf-8"),
            timeout=900,
        )
        try:
            raw = raw_bytes.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise RuntimeErrorEB(
                "database continuity signature is not UTF-8"
            ) from exc
        schema_sha256 = _restore_stable_database_schema_sha256(
            root,
            source_commit,
            postgres_binding,
            identity,
        )

    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeErrorEB(
            "database continuity signature is not JSON"
        ) from exc
    tables = value.get("tables") if isinstance(value, dict) else None
    sequences = value.get("sequences") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or not isinstance(tables, list)
        or not tables
        or not isinstance(sequences, list)
    ):
        raise RuntimeErrorEB(
            "database continuity signature is incomplete"
        )
    domain_nodes = [
        item
        for item in tables
        if isinstance(item, dict)
        and item.get("schema") == "public"
        and item.get("name") == "domain_nodes"
    ]
    if (
        len(domain_nodes) != 1
        or not isinstance(domain_nodes[0].get("rows"), int)
        or domain_nodes[0]["rows"] < 1
    ):
        raise RuntimeErrorEB(
            "database continuity signature has no canonical domain state"
        )
    if schema_sha256 is not None:
        value["schema_sha256"] = schema_sha256
    return value


def _require_database_runtime_continuity(
    restored: Any,
    observed: Any,
    context: str,
) -> None:
    if not isinstance(restored, dict) or not isinstance(observed, dict):
        raise RuntimeErrorEB(f"{context} database signature is invalid")
    restored_tables = restored.get("tables")
    observed_tables = observed.get("tables")
    restored_sequences = restored.get("sequences")
    observed_sequences = observed.get("sequences")
    if (
        not isinstance(restored_tables, list)
        or not isinstance(observed_tables, list)
        or not isinstance(restored_sequences, list)
        or not isinstance(observed_sequences, list)
    ):
        raise RuntimeErrorEB(f"{context} database signature is incomplete")
    if (
        restored_tables != observed_tables
        or restored.get("schema_sha256") != observed.get("schema_sha256")
    ):
        raise RuntimeErrorEB(f"{context} persisted rows or schema drifted")

    def sequence_map(
        items: list[Any],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        result: dict[tuple[str, str], dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict):
                raise RuntimeErrorEB(f"{context} sequence signature is invalid")
            schema = item.get("schema")
            name = item.get("name")
            if not isinstance(schema, str) or not schema or not isinstance(name, str) or not name:
                raise RuntimeErrorEB(f"{context} sequence identity is invalid")
            key = (schema, name)
            if key in result:
                raise RuntimeErrorEB(f"{context} sequence identity is duplicated")
            result[key] = item
        return result

    restored_by_key = sequence_map(restored_sequences)
    observed_by_key = sequence_map(observed_sequences)
    if set(restored_by_key) != set(observed_by_key):
        raise RuntimeErrorEB(f"{context} sequence inventory drifted")

    definition_fields = (
        "start_value",
        "increment_by",
        "min_value",
        "max_value",
        "cache_size",
        "cycle",
    )
    for key in sorted(restored_by_key):
        before = restored_by_key[key]
        after = observed_by_key[key]
        if any(before.get(field) != after.get(field) for field in definition_fields):
            raise RuntimeErrorEB(f"{context} sequence definition drifted")
        try:
            increment = int(str(before["increment_by"]))
            before_last = int(str(before["last_value"]))
            after_last = int(str(after["last_value"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeErrorEB(f"{context} sequence progress is invalid") from exc
        before_called = before.get("is_called")
        after_called = after.get("is_called")
        if type(before_called) is not bool or type(after_called) is not bool:
            raise RuntimeErrorEB(f"{context} sequence call state is invalid")
        if increment == 0:
            raise RuntimeErrorEB(f"{context} sequence increment is invalid")
        if before_called and not after_called:
            raise RuntimeErrorEB(f"{context} sequence call state regressed")
        if not before_called and not after_called and after_last != before_last:
            raise RuntimeErrorEB(f"{context} unused sequence position drifted")
        if before.get("cycle") is True and after_last != before_last:
            raise RuntimeErrorEB(f"{context} cycling sequence progress is ambiguous")
        if increment > 0 and after_last < before_last:
            raise RuntimeErrorEB(f"{context} sequence position regressed")
        if increment < 0 and after_last > before_last:
            raise RuntimeErrorEB(f"{context} sequence position regressed")


def _jetstream_sequence_progress(value: Any, context: str) -> dict[str, int]:
    if not isinstance(value, dict):
        raise RuntimeErrorEB(f"{context} is missing from JetStream monitoring output")
    try:
        return {
            "consumer_seq": int(value["consumer_seq"]),
            "stream_seq": int(value["stream_seq"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeErrorEB(f"{context} has an invalid JetStream sequence") from exc


def _jetstream_signature_from_monitoring(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeErrorEB("NATS JetStream monitoring output is not an object")
    accounts = value.get("account_details")
    if not isinstance(accounts, list):
        raise RuntimeErrorEB("NATS JetStream monitoring output has no account details")

    stream_signatures: list[dict[str, Any]] = []
    durable_consumer_count = 0
    for account in accounts:
        if not isinstance(account, dict):
            raise RuntimeErrorEB("NATS JetStream account detail is invalid")
        account_name = str(account.get("name") or account.get("id") or "")
        streams = account.get("stream_detail", [])
        if not isinstance(streams, list):
            raise RuntimeErrorEB("NATS JetStream stream detail is invalid")
        for stream in streams:
            if not isinstance(stream, dict):
                raise RuntimeErrorEB("NATS JetStream stream entry is invalid")
            stream_name = str(stream.get("name") or "")
            if not stream_name:
                raise RuntimeErrorEB("NATS JetStream stream detail has no name")
            state = stream.get("state")
            if not isinstance(state, dict):
                raise RuntimeErrorEB(f"NATS JetStream stream {stream_name!r} has no state")
            try:
                stream_state = {
                    "messages": int(state["messages"]),
                    "bytes": int(state["bytes"]),
                    "first_seq": int(state["first_seq"]),
                    "last_seq": int(state["last_seq"]),
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeErrorEB(
                    f"NATS JetStream stream {stream_name!r} state is incomplete"
                ) from exc

            durable_consumers: list[dict[str, Any]] = []
            consumers = stream.get("consumer_detail", [])
            if not isinstance(consumers, list):
                raise RuntimeErrorEB(
                    f"NATS JetStream stream {stream_name!r} consumer detail is invalid"
                )
            for consumer in consumers:
                if not isinstance(consumer, dict):
                    raise RuntimeErrorEB("NATS JetStream consumer detail is invalid")
                config = consumer.get("config")
                if not isinstance(config, dict):
                    continue
                durable_name = config.get("durable_name")
                if not isinstance(durable_name, str) or not durable_name:
                    continue
                consumer_name = str(consumer.get("name") or "")
                consumer_stream = str(consumer.get("stream_name") or "")
                if not consumer_name or consumer_stream != stream_name:
                    raise RuntimeErrorEB(
                        "NATS durable consumer identity is incomplete or cross-stream"
                    )
                try:
                    num_ack_pending = int(consumer["num_ack_pending"])
                    num_redelivered = int(consumer["num_redelivered"])
                    num_pending = int(consumer["num_pending"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise RuntimeErrorEB(
                        f"NATS durable consumer {consumer_name!r} state is incomplete"
                    ) from exc
                durable_consumers.append(
                    {
                        "name": consumer_name,
                        "stream_name": consumer_stream,
                        "config": config,
                        "delivered": _jetstream_sequence_progress(
                            consumer.get("delivered"),
                            f"NATS durable consumer {consumer_name!r} delivered state",
                        ),
                        "ack_floor": _jetstream_sequence_progress(
                            consumer.get("ack_floor"),
                            f"NATS durable consumer {consumer_name!r} ack floor",
                        ),
                        "num_ack_pending": num_ack_pending,
                        "num_redelivered": num_redelivered,
                        "num_pending": num_pending,
                    }
                )
            durable_consumers.sort(
                key=lambda item: (str(item["stream_name"]), str(item["name"]))
            )
            durable_consumer_count += len(durable_consumers)
            stream_signatures.append(
                {
                    "account": account_name,
                    "name": stream_name,
                    "state": stream_state,
                    "durable_consumers": durable_consumers,
                }
            )

    stream_signatures.sort(key=lambda item: (str(item["account"]), str(item["name"])))
    try:
        result = {
            "streams": int(value["streams"]),
            "messages": int(value["messages"]),
            "bytes": int(value["bytes"]),
            "durable_consumers": durable_consumer_count,
            "stream_detail": stream_signatures,
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeErrorEB("NATS JetStream aggregate state is incomplete") from exc
    if result["streams"] < 1 or len(stream_signatures) != result["streams"]:
        raise RuntimeErrorEB("NATS JetStream stream detail does not cover all streams")
    if durable_consumer_count < 1:
        raise RuntimeErrorEB("NATS JetStream has no persisted durable consumer state")
    return result



def _jetstream_monitoring_signature(
    root: Path,
    *,
    source_commit: str | None = None,
    nats_binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    command = [
        "/bin/sh",
        "-c",
        (
            "wget -qO- "
            "'http://127.0.0.1:8222/jsz?"
            "accounts=true&streams=true&consumers=true&config=true'"
        ),
    ]
    if nats_binding is None:
        raw = _kubectl(
            root,
            [
                "-n", DATA_NAMESPACE, "exec", "deployment/nats", "--",
                *command,
            ],
        ).stdout
    else:
        if source_commit is None:
            raise RuntimeErrorEB(
                "bound NATS monitoring signature requires source commit"
            )
        raw_bytes = _run_bound_nats_client(
            root,
            source_commit,
            nats_binding,
            command,
            timeout=120,
        )
        try:
            raw = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeErrorEB(
                "NATS JetStream monitoring output is not UTF-8"
            ) from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeErrorEB(
            "NATS JetStream monitoring output is not JSON"
        ) from exc
    return _jetstream_signature_from_monitoring(value)

def _nats_message_store_sha256_from_output(output: str) -> str:
    entries: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            raise RuntimeErrorEB("NATS JetStream message-store digest output is invalid")
        digest, path = parts[0].lower(), parts[1].strip()
        if (
            re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or not path.startswith("/data/")
            or "/streams/" not in path
            or "/msgs/" not in path
            or not path.endswith(".blk")
        ):
            raise RuntimeErrorEB("NATS JetStream message-store digest output is invalid")
        relative = path.removeprefix("/data/")
        if relative in seen_paths:
            raise RuntimeErrorEB("NATS JetStream message-store path is duplicated")
        seen_paths.add(relative)
        entries.append({"path": relative, "sha256": digest})
    if not entries:
        raise RuntimeErrorEB("NATS JetStream message store has no message blocks")
    entries.sort(key=lambda item: item["path"])
    return _stable_json_sha256(entries)



def _nats_message_store_sha256(
    root: Path,
    *,
    source_commit: str | None = None,
    nats_binding: dict[str, Any] | None = None,
) -> str:
    command = [
        "/bin/sh",
        "-c",
        (
            "find /data -type f -path '*/streams/*/msgs/*.blk' "
            "-exec sha256sum '{}' ';'"
        ),
    ]
    if nats_binding is None:
        output = _kubectl(
            root,
            [
                "-n", DATA_NAMESPACE, "exec", "deployment/nats", "--",
                *command,
            ],
            timeout=120,
        ).stdout
    else:
        if source_commit is None:
            raise RuntimeErrorEB(
                "bound NATS message-store signature requires source commit"
            )
        output_bytes = _run_bound_nats_client(
            root,
            source_commit,
            nats_binding,
            command,
            timeout=120,
        )
        try:
            output = output_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeErrorEB(
                "NATS JetStream message-store output is not UTF-8"
            ) from exc
    return _nats_message_store_sha256_from_output(output)


def _jetstream_signature(
    root: Path,
    *,
    source_commit: str | None = None,
    nats_binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    before = _jetstream_monitoring_signature(
        root,
        source_commit=source_commit,
        nats_binding=nats_binding,
    )
    message_store_sha256 = _nats_message_store_sha256(
        root,
        source_commit=source_commit,
        nats_binding=nats_binding,
    )
    after = _jetstream_monitoring_signature(
        root,
        source_commit=source_commit,
        nats_binding=nats_binding,
    )
    if after != before:
        raise RuntimeErrorEB(
            "NATS JetStream state changed while hashing persisted message contents"
        )
    return {
        **before,
        "message_store_sha256": message_store_sha256,
    }

def _event_pipeline_quiescence_sample(
    root: Path,
    *,
    source_commit: str,
    database_identity: tuple[str, str],
) -> dict[str, Any]:
    postgres_binding = _require_postgres_runtime_binding(
        root,
        source_commit,
    )
    sql = f"""
SELECT json_build_object(
  'unpublished_nonquarantined',
  (
    SELECT count(*)::bigint
    FROM domain_outbox
    WHERE published_at IS NULL
      AND quarantined_at IS NULL
  ),
  'published_without_receipt',
  (
    SELECT count(*)::bigint
    FROM domain_outbox AS event
    WHERE event.published_at IS NOT NULL
      AND NOT EXISTS (
        SELECT 1
        FROM domain_event_consumptions AS receipt
        WHERE receipt.consumer_name = '{DOMAIN_EVENT_CONSUMER}'
          AND receipt.event_id = event.id
      )
  )
)::text;
"""
    raw_bytes = _run_bound_postgres_client(
        root,
        source_commit,
        postgres_binding,
        [
            *_database_client_argv("psql", database_identity),
            "-v",
            "ON_ERROR_STOP=1",
            "-qAt",
        ],
        input_bytes=sql.encode("utf-8"),
        timeout=120,
    )
    try:
        database = json.loads(raw_bytes.decode("utf-8").strip())
        unpublished = int(database["unpublished_nonquarantined"])
        missing_receipts = int(database["published_without_receipt"])
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise RuntimeErrorEB(
            "event-pipeline PostgreSQL drain state is invalid"
        ) from exc
    if unpublished < 0 or missing_receipts < 0:
        raise RuntimeErrorEB(
            "event-pipeline PostgreSQL drain counts are invalid"
        )

    nats_binding = _require_nats_runtime_binding(
        root,
        source_commit,
    )
    jetstream = _jetstream_monitoring_signature(
        root,
        source_commit=source_commit,
        nats_binding=nats_binding,
    )
    return {
        "database": {
            "unpublished_nonquarantined": unpublished,
            "published_without_receipt": missing_receipts,
        },
        "jetstream": jetstream,
    }


def _event_pipeline_snapshot_is_drained(snapshot: Any) -> bool:
    if not isinstance(snapshot, dict):
        return False
    database = snapshot.get("database")
    jetstream = snapshot.get("jetstream")
    if (
        not isinstance(database, dict)
        or database.get("unpublished_nonquarantined") != 0
        or database.get("published_without_receipt") != 0
        or not isinstance(jetstream, dict)
    ):
        return False

    expected_consumers = 0
    stream_detail = jetstream.get("stream_detail")
    if not isinstance(stream_detail, list):
        return False
    for stream in stream_detail:
        if not isinstance(stream, dict):
            return False
        consumers = stream.get("durable_consumers")
        if not isinstance(consumers, list):
            return False
        for consumer in consumers:
            if not isinstance(consumer, dict):
                return False
            if consumer.get("name") == DOMAIN_EVENT_CONSUMER:
                expected_consumers += 1
            if (
                consumer.get("num_pending") != 0
                or consumer.get("num_ack_pending") != 0
            ):
                return False
    return expected_consumers == 1


def _wait_event_pipeline_quiescent(
    root: Path,
    *,
    source_commit: str,
    database_identity: tuple[str, str],
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    previous: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        current = _event_pipeline_quiescence_sample(
            root,
            source_commit=source_commit,
            database_identity=database_identity,
        )
        if _event_pipeline_snapshot_is_drained(current):
            if current == previous:
                return current
            previous = current
        else:
            previous = None
        time.sleep(1)
    raise RuntimeErrorEB(
        "event pipeline did not reach a stable fully drained state"
    )


def _scale_deployment(root: Path, namespace: str, name: str, replicas: int) -> None:
    _kubectl(
        root,
        [
            "-n", namespace, "scale", "deployment", name,
            f"--replicas={replicas}",
        ],
    )


def _wait_deployment(
    root: Path,
    namespace: str,
    name: str,
    timeout_seconds: int = 300,
) -> None:
    if type(timeout_seconds) is not int or timeout_seconds <= 0:
        raise RuntimeErrorEB(
            "deployment rollout timeout must be positive integer seconds"
        )
    _kubectl(
        root,
        [
            "-n",
            namespace,
            "rollout",
            "status",
            f"deployment/{name}",
            f"--timeout={timeout_seconds}s",
        ],
        timeout=timeout_seconds + 60,
    )


def _flux_suspend(root: Path, name: str) -> None:
    flux = toolchain(root)["tools"]["flux"]
    run(
        [flux, "suspend", "kustomization", name, "-n", "flux-system"],
        env=kube_env(root),
    )


def _flux_resume(root: Path, name: str) -> None:
    flux = toolchain(root)["tools"]["flux"]
    run(
        [flux, "resume", "kustomization", name, "-n", "flux-system"],
        env=kube_env(root),
    )
    run(
        [flux, "reconcile", "kustomization", name, "-n", "flux-system", "--with-source"],
        env=kube_env(root),
        timeout=600,
    )


def _wait_pods_absent(
    root: Path,
    namespace: str,
    selector: str,
    *,
    timeout_seconds: int = 180,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        pods = _kubectl_json(
            root,
            ["-n", namespace, "get", "pods", "-l", selector],
        )
        items = pods.get("items")
        if not isinstance(items, list):
            raise RuntimeErrorEB("pod absence readback is not a list")
        if not items:
            return
        time.sleep(2)
    raise RuntimeErrorEB(
        f"pods did not terminate before exclusive PVC access: {namespace} {selector}"
    )


def _pvc_volume_identity(root: Path, claim_name: str) -> dict[str, str]:
    pvc = _kubectl_json(
        root, ["-n", DATA_NAMESPACE, "get", "pvc", claim_name]
    )
    metadata = pvc.get("metadata", {}) if isinstance(pvc, dict) else {}
    spec = pvc.get("spec", {}) if isinstance(pvc, dict) else {}
    pvc_uid = metadata.get("uid") if isinstance(metadata, dict) else None
    volume_name = spec.get("volumeName") if isinstance(spec, dict) else None
    if (
        metadata.get("name") != claim_name
        or metadata.get("namespace") != DATA_NAMESPACE
        or metadata.get("deletionTimestamp") is not None
        or not isinstance(pvc_uid, str)
        or not pvc_uid
        or not isinstance(volume_name, str)
        or not volume_name
    ):
        raise RuntimeErrorEB(
            f"data PVC identity is invalid or unbound: {claim_name}"
        )
    pv = _kubectl_json(root, ["get", "pv", volume_name])
    pv_metadata = pv.get("metadata", {}) if isinstance(pv, dict) else {}
    pv_spec = pv.get("spec", {}) if isinstance(pv, dict) else {}
    claim_ref = pv_spec.get("claimRef", {}) if isinstance(pv_spec, dict) else {}
    pv_uid = pv_metadata.get("uid") if isinstance(pv_metadata, dict) else None
    if (
        pv_metadata.get("name") != volume_name
        or pv_metadata.get("deletionTimestamp") is not None
        or not isinstance(pv_uid, str)
        or not pv_uid
        or pv_spec.get("storageClassName") != "local-path"
        or not isinstance(claim_ref, dict)
        or claim_ref.get("namespace") != DATA_NAMESPACE
        or claim_ref.get("name") != claim_name
        or claim_ref.get("uid") != pvc_uid
    ):
        raise RuntimeErrorEB(
            f"data PV identity is not bound to the expected claim: {claim_name}"
        )
    return {
        "pvc_uid": pvc_uid,
        "pv_name": volume_name,
        "pv_uid": pv_uid,
    }


def _wait_pv_absent(
    root: Path,
    pv_name: str,
    *,
    timeout_seconds: int = 180,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = _kubectl(
            root,
            ["get", "pv", pv_name, "--ignore-not-found=true", "-o", "name"],
            timeout=30,
        )
        if not result.stdout.strip():
            return
        time.sleep(2)
    raise RuntimeErrorEB(
        f"old persistent volume did not disappear before restore: {pv_name}"
    )


def _require_running_probe_container(
    root: Path,
    pod_name: str,
    expected_spec: dict[str, Any],
    expected_image: str,
    context: str,
) -> str:
    if (
        not isinstance(pod_name, str)
        or not pod_name
        or not isinstance(expected_spec, dict)
        or not isinstance(expected_image, str)
        or re.search(r"@sha256:[0-9a-f]{64}$", expected_image) is None
        or not isinstance(context, str)
        or not context
    ):
        raise RuntimeErrorEB(f"{context or 'probe'} runtime contract is invalid")
    pod = _kubectl_json(
        root,
        ["-n", DATA_NAMESPACE, "get", "pod", pod_name],
    )
    metadata = pod.get("metadata", {}) if isinstance(pod, dict) else {}
    live_spec = pod.get("spec", {}) if isinstance(pod, dict) else {}
    status = pod.get("status", {}) if isinstance(pod, dict) else {}
    if (
        not isinstance(metadata, dict)
        or metadata.get("name") != pod_name
        or metadata.get("namespace") != DATA_NAMESPACE
        or metadata.get("deletionTimestamp") is not None
        or not isinstance(live_spec, dict)
        or not isinstance(status, dict)
        or status.get("phase") != "Running"
        or _application_pod_spec_projection(
            live_spec,
            f"live {context}",
        )
        != _application_pod_spec_projection(
            expected_spec,
            f"expected {context}",
        )
    ):
        raise RuntimeErrorEB(f"{context} runtime contract drifted")
    ready = any(
        isinstance(condition, dict)
        and condition.get("type") == "Ready"
        and condition.get("status") == "True"
        for condition in status.get("conditions", [])
    )
    statuses = status.get("containerStatuses", [])
    if (
        not ready
        or not isinstance(statuses, list)
        or len(statuses) != 1
        or not isinstance(statuses[0], dict)
        or statuses[0].get("name") != "probe"
        or statuses[0].get("ready") is not True
        or not isinstance(statuses[0].get("state", {}).get("running"), dict)
    ):
        raise RuntimeErrorEB(f"{context} is not running and Ready")
    image_id = statuses[0].get("imageID")
    container_id = statuses[0].get("containerID")
    expected_digest = expected_image.rsplit("@", 1)[1]
    if (
        not _runtime_image_id_matches_digest(image_id, expected_digest)
        or not isinstance(container_id, str)
        or re.fullmatch(r"containerd://[0-9a-f]{64}", container_id) is None
    ):
        raise RuntimeErrorEB(f"{context} container identity drifted")
    return container_id


def _require_empty_replacement_pvc(
    root: Path,
    claim_name: str,
    old_identity: dict[str, str],
    source_commit: str,
) -> dict[str, Any]:
    workload = "postgres" if claim_name == "postgres-data" else "nats"
    manifest_path = CLUSTER / f"data/{workload}.yaml"
    manifest_bytes = _git_blob_bytes(source_commit, manifest_path)
    try:
        manifest_text = manifest_bytes.decode("utf-8")
        documents = [
            item
            for item in yaml.safe_load_all(manifest_text)
            if isinstance(item, dict)
            and item.get("kind") == "Deployment"
            and item.get("metadata", {}).get("name") == workload
            and item.get("metadata", {}).get("namespace") == DATA_NAMESPACE
        ]
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise RuntimeErrorEB(
            f"replacement PVC probe contract is invalid: {claim_name}"
        ) from exc
    if len(documents) != 1:
        raise RuntimeErrorEB(
            f"replacement PVC probe Deployment is ambiguous: {claim_name}"
        )
    pod_spec = (
        documents[0].get("spec", {}).get("template", {}).get("spec", {})
    )
    images = _pod_spec_images(
        pod_spec,
        f"source-commit replacement PVC probe Deployment {workload}",
    )
    image = images["containers"].get(workload)
    if (
        not isinstance(image, str)
        or re.search(r"@sha256:[0-9a-f]{64}$", image) is None
    ):
        raise RuntimeErrorEB(
            f"replacement PVC probe image is unavailable: {claim_name}"
        )
    security = pod_spec.get("securityContext", {})
    run_as_user = security.get("runAsUser") if isinstance(security, dict) else None
    run_as_group = security.get("runAsGroup") if isinstance(security, dict) else None
    fs_group = security.get("fsGroup") if isinstance(security, dict) else None
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in (run_as_user, run_as_group, fs_group)
    ):
        raise RuntimeErrorEB(
            f"replacement PVC probe security contract is invalid: {claim_name}"
        )

    pod_name = f"commonthing-experiment-b-{workload}-empty-probe"
    manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": pod_name, "namespace": DATA_NAMESPACE},
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": run_as_user,
                "runAsGroup": run_as_group,
                "fsGroup": fs_group,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "probe",
                    "image": image,
                    "imagePullPolicy": "IfNotPresent",
                    "command": ["/bin/sh", "-c", "sleep 3600"],
                    "resources": {},
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                        "readOnlyRootFilesystem": True,
                    },
                    "volumeMounts": [
                        {"name": "data", "mountPath": "/probe"},
                        {"name": "tmp", "mountPath": "/tmp"},
                    ],
                }
            ],
            "volumes": [
                {
                    "name": "data",
                    "persistentVolumeClaim": {"claimName": claim_name},
                },
                {"name": "tmp", "emptyDir": {}},
            ],
        },
    }
    kubectl_apply(root, json.dumps(manifest, sort_keys=True))
    try:
        _kubectl(
            root,
            [
                "-n",
                DATA_NAMESPACE,
                "wait",
                "--for=condition=Ready",
                f"pod/{pod_name}",
                "--timeout=3m",
            ],
            timeout=210,
        )
        probe_container_id = _require_running_probe_container(
            root,
            pod_name,
            manifest["spec"],
            image,
            f"replacement PVC probe {claim_name}",
        )
        new_identity = _pvc_volume_identity(root, claim_name)
        if (
            new_identity["pvc_uid"] == old_identity["pvc_uid"]
            or new_identity["pv_name"] == old_identity["pv_name"]
            or new_identity["pv_uid"] == old_identity["pv_uid"]
        ):
            raise RuntimeErrorEB(
                f"replacement PVC reused the previous storage identity: {claim_name}"
            )
        contents = _run_bound_container_command(
            root,
            source_commit,
            probe_container_id,
            [
                "find",
                "/probe",
                "-mindepth",
                "1",
                "-maxdepth",
                "1",
                "-print",
                "-quit",
            ],
            timeout=30,
            context=f"replacement PVC empty probe {claim_name}",
        )
        if contents.strip():
            raise RuntimeErrorEB(
                f"replacement PVC is not empty before restore: {claim_name}"
            )
        return {
            "old": old_identity,
            "new": new_identity,
            "empty_before_restore": True,
        }
    finally:
        _delete_pod(root, DATA_NAMESPACE, pod_name)



def _nats_transfer_pod(
    root: Path,
    name: str,
    image: str,
) -> dict[str, str]:
    if (
        not isinstance(image, str)
        or re.search(r"@sha256:[0-9a-f]{64}$", image) is None
    ):
        raise RuntimeErrorEB(
            "NATS transfer pod requires the source-commit-bound immutable image"
        )
    manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": DATA_NAMESPACE},
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 1000,
                "runAsGroup": 1000,
                "fsGroup": 1000,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "transfer",
                    "image": image,
                    "imagePullPolicy": "IfNotPresent",
                    "command": ["/bin/sh", "-c", "sleep 3600"],
                    "resources": {},
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                        "readOnlyRootFilesystem": True,
                    },
                    "volumeMounts": [
                        {"name": "data", "mountPath": "/data"},
                        {"name": "tmp", "mountPath": "/tmp"},
                    ],
                }
            ],
            "volumes": [
                {
                    "name": "data",
                    "persistentVolumeClaim": {
                        "claimName": "nats-data"
                    },
                },
                {"name": "tmp", "emptyDir": {}},
            ],
        },
    }
    kubectl_apply(root, json.dumps(manifest, sort_keys=True))
    try:
        _kubectl(
            root,
            [
                "-n", DATA_NAMESPACE, "wait", "--for=condition=Ready",
                f"pod/{name}", "--timeout=3m",
            ],
            timeout=210,
        )
        pod = _kubectl_json(
            root,
            ["-n", DATA_NAMESPACE, "get", "pod", name],
        )
        metadata = pod.get("metadata", {}) if isinstance(pod, dict) else {}
        live_spec = pod.get("spec", {}) if isinstance(pod, dict) else {}
        status = pod.get("status", {}) if isinstance(pod, dict) else {}
        if (
            not isinstance(metadata, dict)
            or metadata.get("name") != name
            or metadata.get("namespace") != DATA_NAMESPACE
            or metadata.get("deletionTimestamp") is not None
            or not isinstance(live_spec, dict)
            or not isinstance(status, dict)
            or status.get("phase") != "Running"
            or _application_pod_spec_projection(
                live_spec,
                f"live NATS transfer Pod {name}",
            )
            != _application_pod_spec_projection(
                manifest["spec"],
                f"expected NATS transfer Pod {name}",
            )
        ):
            raise RuntimeErrorEB(
                "NATS transfer Pod runtime contract drifted"
            )
        ready = any(
            isinstance(condition, dict)
            and condition.get("type") == "Ready"
            and condition.get("status") == "True"
            for condition in status.get("conditions", [])
        )
        statuses = status.get("containerStatuses", [])
        if (
            not ready
            or not isinstance(statuses, list)
            or len(statuses) != 1
            or not isinstance(statuses[0], dict)
            or statuses[0].get("name") != "transfer"
            or statuses[0].get("ready") is not True
            or not isinstance(
                statuses[0].get("state", {}).get("running"),
                dict,
            )
        ):
            raise RuntimeErrorEB(
                "NATS transfer Pod is not running and Ready"
            )
        image_id = statuses[0].get("imageID")
        container_id = statuses[0].get("containerID")
        expected_digest = image.rsplit("@", 1)[1]
        if (
            not _runtime_image_id_matches_digest(
                image_id,
                expected_digest,
            )
            or not isinstance(container_id, str)
            or re.fullmatch(
                r"containerd://[0-9a-f]{64}",
                container_id,
            )
            is None
        ):
            raise RuntimeErrorEB(
                "NATS transfer Pod runtime image identity drifted"
            )
        return {
            "container_id": container_id,
            "runtime_image_id": str(image_id),
        }
    except BaseException:
        try:
            _delete_pod(root, DATA_NAMESPACE, name)
        except BaseException as cleanup_exc:
            raise RuntimeErrorEB(
                "NATS transfer Pod validation failed and cleanup did not complete"
            ) from cleanup_exc
        raise

def _delete_pod(root: Path, namespace: str, name: str) -> None:
    _kubectl(
        root,
        [
            "-n", namespace, "delete", "pod", name,
            "--ignore-not-found=true", "--wait=true", "--timeout=2m",
        ],
        timeout=150,
    )
    readback = _kubectl(
        root,
        [
            "-n", namespace, "get", "pod", name,
            "--ignore-not-found=true", "-o", "name",
        ],
        timeout=30,
    )
    if readback.stdout.strip():
        raise RuntimeErrorEB(
            f"transfer pod still exists after deletion: {namespace}/{name}"
        )


@_serialize_experiment_b_lifecycle
def recovery_proof(root: Path) -> dict[str, Any]:
    release_path = root / "receipts/release.json"
    if not release_path.is_file():
        raise RuntimeErrorEB("recovery proof requires an applied release receipt")
    release = json.loads(release_path.read_text(encoding="utf-8"))
    source_commit = str(release.get("source_commit", ""))
    if not COMMIT_RE.fullmatch(source_commit):
        raise RuntimeErrorEB("recovery proof release binding is not exact")
    recovery_failed_receipt = root / "receipts/recovery-failed.json"
    if recovery_failed_receipt.is_file():
        raise RuntimeErrorEB(
            "recovery proof refuses a retry after a failed attempt; rebuild the "
            "Experiment-B cell to establish a fresh baseline"
        )
    _require_recovery_attempt_clear(root)
    _invalidate_receipts(root, RECOVERY_ATTEMPT_INVALIDATES)
    if _current_protected_main_commit() != source_commit:
        raise RuntimeErrorEB("recovery proof release is not current protected main")
    database_identity = _verified_database_client_identity(root, source_commit)
    database_identity_sha256 = _stable_json_sha256(
        {
            "username": database_identity[0],
            "database": database_identity[1],
        }
    )
    recovery_target = _kubernetes_target_identity(root, source_commit)
    storage_path = CLUSTER / "data/storage.yaml"
    storage_bytes = _git_blob_bytes(source_commit, storage_path)
    try:
        storage_manifest = storage_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeErrorEB(
            "recovery storage manifest is not valid UTF-8"
        ) from exc
    storage_manifest_sha256 = hashlib.sha256(storage_bytes).hexdigest()
    backup_dir = root / "recovery"
    backup_dir.mkdir(parents=True, exist_ok=True)
    db_dump = backup_dir / "postgres.dump"
    nats_tar = backup_dir / "nats.tar"
    before_db: dict[str, Any] | None = None
    before_nats: dict[str, Any] | None = None
    postgres_dump_snapshot_fd: int | None = None
    postgres_dump_sha256: str | None = None
    nats_backup_snapshot_fd: int | None = None
    nats_backup_sha256: str | None = None
    recovery_receipt, recovery_attempt, recovery_started_at = (
        _begin_live_check_attempt(root, "recovery", source_commit)
    )
    nats_source_contract = _source_commit_data_deployment_contract(
        source_commit,
        CLUSTER / "data/nats.yaml",
        "nats",
    )
    nats_transfer_image = nats_source_contract["images"]["containers"].get(
        "nats"
    )
    if (
        not isinstance(nats_transfer_image, str)
        or re.search(r"@sha256:[0-9a-f]{64}$", nats_transfer_image) is None
    ):
        raise RuntimeErrorEB(
            "source-commit NATS Deployment has no immutable transfer image"
        )

    with _bound_kube_env(root, recovery_target, source_commit):
        destructive_started = time.monotonic()
        try:
            _require_same_kubernetes_target(
                root, source_commit, recovery_target, "recovery pre-suspend"
            )
            _flux_suspend(root, "commonthing-experiment-b-app")
            _flux_suspend(root, "commonthing-experiment-b-data")
            _require_same_kubernetes_target(
                root, source_commit, recovery_target, "recovery Flux suspension"
            )
            _wait_event_pipeline_quiescent(
                root,
                source_commit=source_commit,
                database_identity=database_identity,
            )
            _scale_deployment(root, APP_NAMESPACE, "weltgewebe-api", 0)
            _scale_deployment(root, APP_NAMESPACE, "weltgewebe-web", 0)
            _wait_pods_absent(
                root,
                APP_NAMESPACE,
                "app.kubernetes.io/name=weltgewebe-api",
            )
            _wait_pods_absent(
                root,
                APP_NAMESPACE,
                "app.kubernetes.io/name=weltgewebe-web",
            )
            _require_same_kubernetes_target(
                root, source_commit, recovery_target, "recovery application quiescence"
            )
            frozen_pipeline = _event_pipeline_quiescence_sample(
                root,
                source_commit=source_commit,
                database_identity=database_identity,
            )
            if not _event_pipeline_snapshot_is_drained(frozen_pipeline):
                raise RuntimeErrorEB(
                    "event pipeline changed while application workers were stopping"
                )

            postgres_signature_before = _require_postgres_runtime_binding(
                root,
                source_commit,
            )
            before_db = _database_signature(
                root,
                database_identity=database_identity,
                source_commit=source_commit,
                postgres_binding=postgres_signature_before,
            )
            nats_signature_before_binding = (
                _require_nats_runtime_binding(
                    root,
                    source_commit,
                )
            )
            before_nats = _jetstream_signature(
                root,
                source_commit=source_commit,
                nats_binding=nats_signature_before_binding,
            )
            if before_nats["streams"] < 1 or before_nats["messages"] < 1:
                raise RuntimeErrorEB("JetStream test state is empty before recovery proof")

            _require_same_kubernetes_target(
                root, source_commit, recovery_target, "recovery PostgreSQL backup"
            )
            postgres_backup_binding = _require_postgres_runtime_binding(
                root,
                source_commit,
            )
            if (
                postgres_backup_binding["pod_name"]
                != postgres_signature_before["pod_name"]
            ):
                raise RuntimeErrorEB(
                    "PostgreSQL Pod changed between pre-recovery signature and backup"
                )
            dump_payload = _run_bound_postgres_client(
                root,
                source_commit,
                postgres_backup_binding,
                [
                    *_database_client_argv(
                        "pg_dump",
                        database_identity,
                    ),
                    "-Fc",
                ],
                timeout=900,
            )
            if not dump_payload:
                raise RuntimeErrorEB(
                    "PostgreSQL recovery dump is empty"
                )
            postgres_dump_snapshot_fd = _create_sealed_snapshot_fd(
                dump_payload,
                "PostgreSQL recovery dump",
            )
            postgres_dump_sha256 = hashlib.sha256(
                dump_payload
            ).hexdigest()
            atomic_bytes(db_dump, dump_payload)

            _require_same_kubernetes_target(
                root, source_commit, recovery_target, "recovery pre-NATS shutdown"
            )
            nats_runtime_binding = _require_nats_runtime_binding(
                root,
                source_commit,
            )
            if (
                _nats_runtime_binding_identity(
                    nats_runtime_binding
                )
                != _nats_runtime_binding_identity(
                    nats_signature_before_binding
                )
            ):
                raise RuntimeErrorEB(
                    "NATS runtime changed between continuity signature and shutdown"
                )
            _scale_deployment(root, DATA_NAMESPACE, "nats", 0)
            _wait_pods_absent(
                root,
                DATA_NAMESPACE,
                "app.kubernetes.io/name=nats",
            )
            _require_same_kubernetes_target(
                root, source_commit, recovery_target, "recovery NATS shutdown"
            )
            nats_backup_transfer = _nats_transfer_pod(
                root,
                "commonthing-experiment-b-nats-backup",
                nats_transfer_image,
            )
            try:
                nats_backup_payload = _run_bound_container_command(
                    root,
                    source_commit,
                    nats_backup_transfer["container_id"],
                    ["tar", "-C", "/data", "-cf", "-", "."],
                    timeout=900,
                    context="NATS backup transfer",
                )
                if not nats_backup_payload:
                    raise RuntimeErrorEB(
                        "NATS recovery backup is empty"
                    )
                nats_backup_snapshot_fd = _create_sealed_snapshot_fd(
                    nats_backup_payload,
                    "NATS recovery backup",
                )
                nats_backup_sha256 = hashlib.sha256(
                    nats_backup_payload
                ).hexdigest()
                atomic_bytes(nats_tar, nats_backup_payload)
            finally:
                _delete_pod(
                    root, DATA_NAMESPACE, "commonthing-experiment-b-nats-backup"
                )

            _require_same_kubernetes_target(
                root, source_commit, recovery_target, "recovery post-backup"
            )
            _scale_deployment(root, DATA_NAMESPACE, "postgres", 0)
            _wait_pods_absent(
                root,
                DATA_NAMESPACE,
                "app.kubernetes.io/name=postgres",
            )
            _require_same_kubernetes_target(
                root, source_commit, recovery_target, "recovery PostgreSQL shutdown"
            )
            old_pvc_identities = {
                name: _pvc_volume_identity(root, name)
                for name in ("postgres-data", "nats-data")
            }
            _require_same_kubernetes_target(
                root, source_commit, recovery_target, "recovery pre-PVC deletion"
            )
            _kubectl(
                root,
                [
                    "-n", DATA_NAMESPACE, "delete", "pvc",
                    "postgres-data", "nats-data", "--wait=true", "--timeout=5m",
                ],
                timeout=330,
            )
            _require_same_kubernetes_target(
                root, source_commit, recovery_target, "recovery PVC deletion"
            )
            for identity in old_pvc_identities.values():
                _wait_pv_absent(root, identity["pv_name"])
            _require_same_kubernetes_target(
                root, source_commit, recovery_target, "recovery pre-storage recreation"
            )
            kubectl_apply(root, storage_manifest)
            pvc_replacements = {
                name: _require_empty_replacement_pvc(
                    root, name, old_pvc_identities[name], source_commit
                )
                for name in ("postgres-data", "nats-data")
            }

            _require_same_kubernetes_target(
                root, source_commit, recovery_target, "recovery replacement PVC verification"
            )
            nats_restore_transfer = _nats_transfer_pod(
                root,
                "commonthing-experiment-b-nats-restore",
                nats_transfer_image,
            )
            try:
                if nats_backup_snapshot_fd is None:
                    raise RuntimeErrorEB(
                        "NATS recovery backup snapshot is missing"
                    )
                nats_backup_size = os.fstat(
                    nats_backup_snapshot_fd
                ).st_size
                nats_restore_payload = os.pread(
                    nats_backup_snapshot_fd,
                    nats_backup_size,
                    0,
                )
                if (
                    len(nats_restore_payload) != nats_backup_size
                    or hashlib.sha256(
                        nats_restore_payload
                    ).hexdigest()
                    != nats_backup_sha256
                ):
                    raise RuntimeErrorEB(
                        "NATS recovery backup snapshot drifted"
                    )
                _run_bound_container_command(
                    root,
                    source_commit,
                    nats_restore_transfer["container_id"],
                    ["tar", "-C", "/data", "-xf", "-"],
                    input_bytes=nats_restore_payload,
                    timeout=900,
                    context="NATS restore transfer",
                )
            finally:
                _delete_pod(
                    root, DATA_NAMESPACE, "commonthing-experiment-b-nats-restore"
                )

            _require_same_kubernetes_target(
                root, source_commit, recovery_target, "recovery pre-PostgreSQL restore"
            )
            _scale_deployment(root, DATA_NAMESPACE, "postgres", 1)
            _wait_deployment(root, DATA_NAMESPACE, "postgres", 300)
            _require_same_kubernetes_target(
                root, source_commit, recovery_target, "recovery PostgreSQL restore"
            )
            postgres_restore_binding = _require_postgres_runtime_binding(
                root,
                source_commit,
            )
            if postgres_dump_snapshot_fd is None:
                raise RuntimeErrorEB(
                    "PostgreSQL recovery dump snapshot is missing"
                )
            dump_size = os.fstat(
                postgres_dump_snapshot_fd
            ).st_size
            restore_payload = os.pread(
                postgres_dump_snapshot_fd,
                dump_size,
                0,
            )
            if (
                len(restore_payload) != dump_size
                or hashlib.sha256(restore_payload).hexdigest()
                != postgres_dump_sha256
            ):
                raise RuntimeErrorEB(
                    "PostgreSQL recovery dump snapshot drifted"
                )
            _run_bound_postgres_client(
                root,
                source_commit,
                postgres_restore_binding,
                [
                    *_database_client_argv(
                        "pg_restore",
                        database_identity,
                    ),
                    "--clean",
                    "--if-exists",
                    "--no-owner",
                ],
                input_bytes=restore_payload,
                timeout=1200,
            )
            _require_same_kubernetes_target(
                root, source_commit, recovery_target, "recovery post-PostgreSQL restore"
            )
            _scale_deployment(root, DATA_NAMESPACE, "nats", 1)
            _wait_deployment(root, DATA_NAMESPACE, "nats", 300)
            _require_same_kubernetes_target(
                root, source_commit, recovery_target, "recovery data restoration"
            )

            if postgres_dump_snapshot_fd is not None:
                os.close(postgres_dump_snapshot_fd)
                postgres_dump_snapshot_fd = None
            if nats_backup_snapshot_fd is not None:
                os.close(nats_backup_snapshot_fd)
                nats_backup_snapshot_fd = None
            _require_same_kubernetes_target(
                root, source_commit, recovery_target, "recovery pre-Flux resume"
            )
            # Compare the exact persisted state before application workers
            # resume. Search reconciliation intentionally retries idempotent
            # INSERT ... ON CONFLICT DO NOTHING operations; PostgreSQL can
            # advance their identity sequence even when no row is inserted.
            postgres_signature_after = _require_postgres_runtime_binding(
                root,
                source_commit,
            )
            if (
                postgres_signature_after["pod_name"]
                != postgres_restore_binding["pod_name"]
            ):
                raise RuntimeErrorEB(
                    "PostgreSQL Pod changed between restore and continuity signature"
                )
            after_db = _database_signature(
                root,
                database_identity=database_identity,
                source_commit=source_commit,
                postgres_binding=postgres_signature_after,
            )
            nats_signature_after_binding = (
                _require_nats_runtime_binding(
                    root,
                    source_commit,
                )
            )
            after_nats = _jetstream_signature(
                root,
                source_commit=source_commit,
                nats_binding=nats_signature_after_binding,
            )
            if after_db != before_db:
                raise RuntimeErrorEB(
                    "PostgreSQL/search signature changed across delete-to-prove"
                )
            if after_nats != before_nats:
                raise RuntimeErrorEB(
                    "JetStream stream/message-store/durable-consumer continuity signature changed across restore"
                )
            _require_same_kubernetes_target(
                root,
                source_commit,
                recovery_target,
                "recovery restored continuity",
            )
            _flux_resume(root, "commonthing-experiment-b-data")
            _flux_resume(root, "commonthing-experiment-b-app")
            _wait_deployment(root, APP_NAMESPACE, "weltgewebe-api", 480)
            _wait_deployment(root, APP_NAMESPACE, "weltgewebe-web", 300)
            _wait_event_pipeline_quiescent(
                root,
                source_commit=source_commit,
                database_identity=database_identity,
            )
            _require_same_kubernetes_target(
                root,
                source_commit,
                recovery_target,
                "recovery post-resume event quiescence",
            )
            postgres_post_resume_binding = _require_postgres_runtime_binding(
                root,
                source_commit,
            )
            database_post_resume = _database_signature(
                root,
                database_identity=database_identity,
                source_commit=source_commit,
                postgres_binding=postgres_post_resume_binding,
            )
            _require_database_runtime_continuity(
                after_db,
                database_post_resume,
                "recovery post-resume database",
            )
            nats_post_resume_binding = _require_nats_runtime_binding(
                root,
                source_commit,
            )
            jetstream_post_resume = _jetstream_signature(
                root,
                source_commit=source_commit,
                nats_binding=nats_post_resume_binding,
            )
            if jetstream_post_resume != after_nats:
                raise RuntimeErrorEB(
                    "JetStream state changed after restore while the event pipeline was quiescent"
                )

            _require_same_kubernetes_target(
                root, source_commit, recovery_target, "recovery completion"
            )
            rto_seconds = time.monotonic() - destructive_started
        except Exception:
            if postgres_dump_snapshot_fd is not None:
                os.close(postgres_dump_snapshot_fd)
                postgres_dump_snapshot_fd = None
            if nats_backup_snapshot_fd is not None:
                os.close(nats_backup_snapshot_fd)
                nats_backup_snapshot_fd = None
            resuspended: dict[str, bool] = {
                "commonthing-experiment-b-app": False,
                "commonthing-experiment-b-data": False,
            }
            target_safe_for_cleanup = False
            try:
                _require_same_kubernetes_target(
                    root,
                    source_commit,
                    recovery_target,
                    "recovery failure cleanup",
                )
                target_safe_for_cleanup = True
            except Exception:
                target_safe_for_cleanup = False
            if target_safe_for_cleanup:
                for name in (
                    "commonthing-experiment-b-app",
                    "commonthing-experiment-b-data",
                ):
                    try:
                        _flux_suspend(root, name)
                        resuspended[name] = True
                    except Exception:
                        resuspended[name] = False
            atomic_json(
                recovery_failed_receipt,
                {
                    "schema_version": 1,
                    "status": "failed",
                    "source_commit": source_commit,
                    "database_identity_sha256": database_identity_sha256,
                    "database_before": before_db,
                    "jetstream_before": before_nats,
                    "kubernetes_target_sha256": _stable_json_sha256(
                        recovery_target
                    ),
                    "storage_manifest_sha256": storage_manifest_sha256,
                    "nats_source_contract_sha256": nats_source_contract[
                        "contract_sha256"
                    ],
                    "nats_transfer_image_sha256": hashlib.sha256(
                        nats_transfer_image.encode("utf-8")
                    ).hexdigest(),
                    "target_safe_for_cleanup": target_safe_for_cleanup,
                    "flux_resuspended": resuspended,
                },
            )
            atomic_json(
                recovery_attempt,
                {
                    "schema_version": 1,
                    "status": "failed",
                    "source_commit": source_commit,
                    "receipt": recovery_receipt.name,
                    "started_at_unix_ms": recovery_started_at,
                    "finished_at_unix_ms": time.time_ns() // 1_000_000,
                    "failure_receipt": recovery_failed_receipt.name,
                    "failure_receipt_sha256": sha256_file(
                        recovery_failed_receipt
                    ),
                },
            )
            raise
    if before_db is None or before_nats is None:
        raise RuntimeErrorEB("recovery proof has no quiesced before-signature")
    receipt = {
        "schema_version": 1,
        "status": "pass",
        "source_commit": source_commit,
        "database_identity_sha256": database_identity_sha256,
        "kubernetes_target_sha256": _stable_json_sha256(
            recovery_target
        ),
        "storage_manifest_sha256": storage_manifest_sha256,
        "nats_source_contract_sha256": nats_source_contract[
            "contract_sha256"
        ],
        "nats_transfer_image_sha256": hashlib.sha256(
            nats_transfer_image.encode("utf-8")
        ).hexdigest(),
        "rpo_seconds": 0,
        "rto_seconds": round(rto_seconds, 3),
        "postgres_dump_sha256": postgres_dump_sha256,
        "nats_backup_sha256": nats_backup_sha256,
        "database_before": before_db,
        "database_after": after_db,
        "database_post_resume": database_post_resume,
        "jetstream_before": before_nats,
        "jetstream_after": after_nats,
        "jetstream_post_resume": jetstream_post_resume,
        "pvc_replacements": pvc_replacements,
        "pvc_delete_to_prove": True,
        "replacement_pvcs_empty_before_restore": True,
        "production_data_used": False,
    }
    atomic_json(recovery_receipt, receipt)
    _complete_live_check_attempt(
        recovery_attempt,
        recovery_receipt,
        source_commit,
        recovery_started_at,
        "pass",
    )
    return receipt


def _require_stored_pod_image_contract(
    observed: Any,
    expected_replicas: int,
    expected_images: dict[str, dict[str, str]],
    context: str,
) -> None:
    if not isinstance(observed, dict) or not isinstance(
        observed.get("pods"), dict
    ):
        raise RuntimeErrorEB(f"status does not prove the live {context} contract")
    try:
        runtime_binding_sha256 = _pod_runtime_image_ids_sha256(
            observed["pods"], expected_images
        )
    except RuntimeErrorEB as exc:
        raise RuntimeErrorEB(
            f"status does not prove the live {context} contract"
        ) from exc
    if (
        observed.get("expected_replicas") != expected_replicas
        or observed.get("observed_replicas") != expected_replicas
        or observed.get("requested_images_sha256")
        != _stable_json_sha256(expected_images)
        or observed.get("runtime_image_ids_sha256")
        != runtime_binding_sha256
        or observed.get("images_canonical") is not True
        or len(observed["pods"]) != expected_replicas
    ):
        raise RuntimeErrorEB(f"status does not prove the live {context} contract")
    for pod in observed["pods"].values():
        runtime_ids = pod.get("runtime_image_ids") if isinstance(pod, dict) else None
        if (
            not isinstance(pod, dict)
            or pod.get("ready") is not True
            or pod.get("requested_images_sha256")
            != _stable_json_sha256(expected_images)
            or not isinstance(runtime_ids, dict)
            or set(runtime_ids) != {"containers", "init_containers"}
        ):
            raise RuntimeErrorEB(f"status does not prove the live {context} contract")
        for group in ("containers", "init_containers"):
            ids = runtime_ids.get(group)
            if (
                not isinstance(ids, dict)
                or set(ids) != set(expected_images[group])
            ):
                raise RuntimeErrorEB(
                    f"status does not prove the live {context} contract"
                )
            for name, image in expected_images[group].items():
                observed_digest = _runtime_image_id_digest(ids.get(name))
                if observed_digest is None:
                    raise RuntimeErrorEB(
                        f"status does not prove the live {context} contract"
                    )
                if "@" in image:
                    expected_digest = image.rsplit("@", 1)[1]
                    if (
                        not DIGEST_RE.fullmatch(expected_digest)
                        or not secrets.compare_digest(
                            observed_digest, expected_digest
                        )
                    ):
                        raise RuntimeErrorEB(
                            f"status does not prove the live {context} contract"
                        )


@_serialize_experiment_b_lifecycle
def portability_report(root: Path) -> dict[str, Any]:
    _invalidate_receipts(root, PORTABILITY_DERIVED_RECEIPTS)
    recovery_failed_receipt = root / "receipts/recovery-failed.json"
    if recovery_failed_receipt.is_file():
        raise RuntimeErrorEB(
            "portability report is blocked by the latest failed recovery attempt"
        )
    expected_status = {
        "vm-create.json": "created",
        "k3s.json": "ready",
        "platform.json": "ready",
        "secrets.json": "ready",
        "release.json": "applied",
        "release-attempt.json": "pass",
        "t048-fixture.json": "loaded",
        "semantic-search.json": "pass",
        "semantic-search-attempt.json": "pass",
        "functional-readback.json": "pass",
        "functional-readback-attempt.json": "pass",
        "t048-load.json": "pass",
        "t048-load-attempt.json": "pass",
        "recovery.json": "pass",
        "recovery-attempt.json": "pass",
        "status.json": "observed",
        "status-attempt.json": "pass",
    }
    payloads: dict[str, dict[str, Any]] = {}
    receipts: dict[str, str] = {}
    for name, required_status in expected_status.items():
        path = root / "receipts" / name
        if not path.is_file():
            raise RuntimeErrorEB(f"portability report is missing receipt: {name}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeErrorEB(
                f"portability receipt is not valid JSON: {name}"
            ) from exc
        if not isinstance(payload, dict) or payload.get("status") != required_status:
            raise RuntimeErrorEB(
                f"portability receipt does not prove success: {name}"
            )
        payloads[name] = payload
        receipts[name] = sha256_file(path)

    source_commit = str(payloads["release.json"].get("source_commit", ""))
    if not COMMIT_RE.fullmatch(source_commit):
        raise RuntimeErrorEB("release receipt has no exact source commit")
    for name in (
        "vm-create.json",
        "k3s.json",
        "platform.json",
        "secrets.json",
        "release-attempt.json",
        "t048-fixture.json",
        "semantic-search.json",
        "semantic-search-attempt.json",
        "functional-readback.json",
        "functional-readback-attempt.json",
        "t048-load.json",
        "t048-load-attempt.json",
        "recovery.json",
        "status.json",
        "status-attempt.json",
    ):
        if payloads[name].get("source_commit") != source_commit:
            raise RuntimeErrorEB(
                f"portability receipt source binding drifted: {name}"
            )
    for receipt_stem in (
        "release",
        "semantic-search",
        "functional-readback",
        "t048-load",
        "recovery",
        "status",
    ):
        attempt_name = f"{receipt_stem}-attempt.json"
        receipt_name = f"{receipt_stem}.json"
        attempt = payloads[attempt_name]
        if (
            attempt.get("receipt") != receipt_name
            or attempt.get("receipt_sha256") != receipts[receipt_name]
        ):
            raise RuntimeErrorEB(
                f"latest {receipt_stem} attempt is not bound to its current success receipt"
            )

    fresh_functional = functional_readback(root, source_commit)
    if (
        not isinstance(fresh_functional, dict)
        or fresh_functional.get("status") != "pass"
        or fresh_functional.get("source_commit") != source_commit
    ):
        raise RuntimeErrorEB(
            "fresh functional readback did not return source-bound success evidence"
        )
    for name, required_status in (
        ("functional-readback.json", "pass"),
        ("functional-readback-attempt.json", "pass"),
    ):
        path = root / "receipts" / name
        try:
            raw = path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeErrorEB(
                f"fresh functional portability receipt is invalid: {name}"
            ) from exc
        if (
            not isinstance(payload, dict)
            or payload.get("status") != required_status
            or payload.get("source_commit") != source_commit
        ):
            raise RuntimeErrorEB(
                f"fresh functional portability receipt is invalid: {name}"
            )
        if (
            name == "functional-readback.json"
            and payload != fresh_functional
        ):
            raise RuntimeErrorEB(
                "functional readback receipt changed after fresh live validation"
            )
        payloads[name] = payload
        receipts[name] = hashlib.sha256(raw).hexdigest()
    functional_attempt = payloads["functional-readback-attempt.json"]
    if (
        functional_attempt.get("receipt") != "functional-readback.json"
        or functional_attempt.get("receipt_sha256")
        != receipts["functional-readback.json"]
    ):
        raise RuntimeErrorEB(
            "fresh functional readback attempt is not bound to its current success receipt"
        )

    fresh_t048 = t048_load_proof(root, source_commit)
    if (
        not isinstance(fresh_t048, dict)
        or fresh_t048.get("status") != "pass"
        or fresh_t048.get("source_commit") != source_commit
    ):
        raise RuntimeErrorEB(
            "fresh T048 load proof did not return source-bound success evidence"
        )
    for name, required_status in (
        ("t048-load.json", "pass"),
        ("t048-load-attempt.json", "pass"),
    ):
        path = root / "receipts" / name
        try:
            raw = path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeErrorEB(
                f"fresh T048 portability receipt is invalid: {name}"
            ) from exc
        if (
            not isinstance(payload, dict)
            or payload.get("status") != required_status
            or payload.get("source_commit") != source_commit
        ):
            raise RuntimeErrorEB(
                f"fresh T048 portability receipt is invalid: {name}"
            )
        if name == "t048-load.json" and payload != fresh_t048:
            raise RuntimeErrorEB(
                "T048 load receipt changed after fresh live validation"
            )
        payloads[name] = payload
        receipts[name] = hashlib.sha256(raw).hexdigest()
    t048_attempt = payloads["t048-load-attempt.json"]
    if (
        t048_attempt.get("receipt") != "t048-load.json"
        or t048_attempt.get("receipt_sha256")
        != receipts["t048-load.json"]
    ):
        raise RuntimeErrorEB(
            "fresh T048 load attempt is not bound to its current success receipt"
        )

    fresh_status = status(root)
    if (
        not isinstance(fresh_status, dict)
        or fresh_status.get("status") != "observed"
        or fresh_status.get("source_commit") != source_commit
    ):
        raise RuntimeErrorEB(
            "fresh status did not return source-bound live evidence"
        )
    for name, required_status in (
        ("status.json", "observed"),
        ("status-attempt.json", "pass"),
    ):
        path = root / "receipts" / name
        try:
            raw = path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeErrorEB(
                f"fresh status portability receipt is invalid: {name}"
            ) from exc
        if (
            not isinstance(payload, dict)
            or payload.get("status") != required_status
            or payload.get("source_commit") != source_commit
        ):
            raise RuntimeErrorEB(
                f"fresh status portability receipt is invalid: {name}"
            )
        if name == "status.json" and payload != fresh_status:
            raise RuntimeErrorEB(
                "status receipt changed after fresh live validation"
            )
        payloads[name] = payload
        receipts[name] = hashlib.sha256(raw).hexdigest()
    status_attempt = payloads["status-attempt.json"]
    if (
        status_attempt.get("receipt") != "status.json"
        or status_attempt.get("receipt_sha256")
        != receipts["status.json"]
    ):
        raise RuntimeErrorEB(
            "fresh status attempt is not bound to its current live receipt"
        )
    recovery_state = fresh_status.get("recovery_state")
    if (
        not isinstance(recovery_state, dict)
        or recovery_state.get("recovery_receipt_sha256")
        != receipts["recovery.json"]
        or recovery_state.get("fixture_receipt_sha256")
        != receipts["t048-fixture.json"]
    ):
        raise RuntimeErrorEB(
            "fresh status recovery evidence is not bound to retained "
            "portability receipts"
        )


    config = _source_commit_config(source_commit)
    _require_vm_create_receipt(
        payloads["vm-create.json"], source_commit, config, root
    )
    if (
        payloads["status.json"].get("vm_create_sha256") != receipts["vm-create.json"]
        or payloads["status.json"].get("vm_substrate")
        != payloads["vm-create.json"]["substrate"]
    ):
        raise RuntimeErrorEB("status is not bound to the current VM creation substrate receipt")
    cilium_status = payloads["status.json"].get("cilium")
    if (
        not isinstance(cilium_status, dict)
        or cilium_status.get("chart_version") != config["cilium"]["chart_version"]
        or cilium_status.get("gateway_api") is not True
        or cilium_status.get("kube_proxy_replacement") is not True
        or cilium_status.get("daemonset_images_canonical") is not True
        or cilium_status.get("operator_images_canonical") is not True
        or cilium_status.get("relay_images_canonical") is not True
        or not isinstance(cilium_status.get("daemonset_images"), dict)
        or not isinstance(cilium_status.get("operator_images"), dict)
        or not isinstance(cilium_status.get("relay_images"), dict)
        or not isinstance(cilium_status.get("operator"), dict)
        or cilium_status["operator"].get("available") is not True
        or cilium_status["operator"].get("desired_replicas") != 1
        or not isinstance(cilium_status.get("relay"), dict)
        or cilium_status["relay"].get("available") is not True
        or cilium_status["relay"].get("desired_replicas") != 1
        or not isinstance(cilium_status.get("daemonset_desired"), int)
        or isinstance(cilium_status.get("daemonset_desired"), bool)
        or cilium_status["daemonset_desired"] < 1
        or cilium_status.get("daemonset_ready")
        != cilium_status.get("daemonset_desired")
        or cilium_status.get("kube_proxy_present") is not False
    ):
        raise RuntimeErrorEB("status does not prove the live Cilium contract")
    _require_stored_pod_image_contract(
        cilium_status.get("daemonset_pods"),
        cilium_status["daemonset_desired"],
        cilium_status["daemonset_images"],
        "Cilium DaemonSet Pod",
    )
    _require_stored_pod_image_contract(
        cilium_status.get("operator_pods"),
        1,
        cilium_status["operator_images"],
        "Cilium operator Pod",
    )
    _require_stored_pod_image_contract(
        cilium_status.get("relay_pods"),
        1,
        cilium_status["relay_images"],
        "Hubble Relay Pod",
    )
    platform_cilium_baseline = payloads["platform.json"].get(
        "cilium_runtime_image_ids"
    )
    if (
        not isinstance(platform_cilium_baseline, dict)
        or set(platform_cilium_baseline) != {"daemonset", "operator", "relay"}
        or cilium_status.get("runtime_image_ids_baseline")
        != platform_cilium_baseline
        or cilium_status["daemonset_pods"].get("runtime_image_ids_sha256")
        != platform_cilium_baseline.get("daemonset")
        or cilium_status["operator_pods"].get("runtime_image_ids_sha256")
        != platform_cilium_baseline.get("operator")
        or cilium_status["relay_pods"].get("runtime_image_ids_sha256")
        != platform_cilium_baseline.get("relay")
    ):
        raise RuntimeErrorEB(
            "status does not prove the installed Cilium runtime image baseline"
        )

    status_payload = payloads["status.json"]
    k3s_receipt = payloads["k3s.json"]
    k3s_status = status_payload.get("k3s_runtime")
    expected_k3s_config, expected_k3s_service = _k3s_contract_paths(config)
    expected_k3s_values = {
        "binary_sha256": str(config["kubernetes"]["binary_sha256"]),
        "config_sha256": _git_blob_sha256(source_commit, expected_k3s_config),
        "service_sha256": _git_blob_sha256(source_commit, expected_k3s_service),
    }
    if (
        not isinstance(k3s_status, dict)
        or status_payload.get("vm_ip") != k3s_status.get("vm_ip")
        or k3s_receipt.get("vm_ip") != k3s_status.get("vm_ip")
        or k3s_status.get("kubeconfig_sha256")
        != k3s_receipt.get("kubeconfig_sha256")
        or not isinstance(k3s_status.get("kubeconfig_sha256"), str)
        or re.fullmatch(
            r"[0-9a-f]{64}", k3s_status["kubeconfig_sha256"]
        )
        is None
        or k3s_status.get("kubeconfig_server")
        != f"https://{k3s_status.get('vm_ip')}:6443"
        or any(
            k3s_status.get(key) != value
            or k3s_receipt.get(key) != value
            for key, value in expected_k3s_values.items()
        )
        or k3s_status.get("service_active") is not True
        or k3s_status.get("service_substate") != "running"
        or k3s_status.get("unit_file_state") != "enabled"
        or k3s_status.get("fragment_path")
        != "/etc/systemd/system/k3s.service"
        or k3s_status.get("drop_ins_absent") is not True
        or not isinstance(k3s_status.get("main_pid"), int)
        or isinstance(k3s_status.get("main_pid"), bool)
        or k3s_status["main_pid"] <= 0
        or not _k3s_process_identity_is_supported(
            k3s_status.get("process_exe"),
            k3s_status.get("process_argv"),
        )
        or (
            k3s_status.get("process_exe") != "/usr/local/bin/k3s"
            and (
                k3s_status.get("reexec_binary_sha256")
                != config["kubernetes"]["reexec_binary_sha256"]
                or k3s_status.get("reexec_current_target_verified") is not True
            )
        )
        or k3s_status.get("environment_overrides_absent") is not True
    ):
        raise RuntimeErrorEB("status does not prove the live pinned k3s runtime")

    if (
        status_payload.get("node_ready") is not True
        or status_payload.get("kubelet_version")
        != str(config["kubernetes"]["version"])
    ):
        raise RuntimeErrorEB("status does not prove a Ready pinned k3s node")

    release_bootstrap_sha256 = payloads["release.json"].get("sha256")
    expected_flux_bootstrap = _flux_bootstrap_contract(
        root,
        payloads["release.json"],
    )
    release_config_map_status = status_payload.get(
        "flux_release_config_map"
    )
    flux_controllers = status_payload.get("flux_controllers")
    flux_readback = status_payload.get("flux")
    if (
        not isinstance(release_bootstrap_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", release_bootstrap_sha256) is None
        or expected_flux_bootstrap.get("bootstrap_sha256")
        != release_bootstrap_sha256
        or status_payload.get("flux_bootstrap_sha256")
        != release_bootstrap_sha256
        or not isinstance(release_config_map_status, dict)
        or release_config_map_status.get("canonical") is not True
        or release_config_map_status.get("contract_sha256")
        != _stable_json_sha256(
            expected_flux_bootstrap["release_config_map"]
        )
        or not _flux_revision_matches_commit(
            status_payload.get("flux_source_revision"), source_commit
        )
        or not isinstance(flux_controllers, dict)
        or set(flux_controllers) != EXPECTED_FLUX_CONTROLLERS
        or any(
            not isinstance(value, dict)
            or value.get("available") is not True
            or value.get("desired_replicas") != 1
            or value.get("images_canonical") is not True
            or not isinstance(value.get("images"), dict)
            or value.get("images_sha256")
            != _stable_json_sha256(value["images"])
            for value in flux_controllers.values()
        )
        or not isinstance(flux_readback, dict)
        or set(flux_readback) != EXPECTED_FLUX_KUSTOMIZATIONS
        or any(
            not isinstance(value, dict)
            or value.get("ready") is not True
            or re.fullmatch(r"[0-9a-f]{64}", str(value.get("spec_sha256") or ""))
            is None
            for value in flux_readback.values()
        )
    ):
        raise RuntimeErrorEB("status does not prove the live Flux contract")
    expected_flux_controllers = _expected_flux_controller_contract(
        root,
        source_commit=source_commit,
    )
    flux_baseline = payloads["platform.json"].get("flux_runtime_image_ids")
    if (
        not isinstance(flux_baseline, dict)
        or set(flux_baseline) != EXPECTED_FLUX_CONTROLLERS
        or status_payload.get("flux_runtime_image_ids_baseline")
        != flux_baseline
        or set(expected_flux_controllers) != EXPECTED_FLUX_CONTROLLERS
    ):
        raise RuntimeErrorEB(
            "status does not prove the installed Flux runtime image baseline"
        )
    for name, expected in expected_flux_controllers.items():
        observed = flux_controllers.get(name)
        if (
            not isinstance(observed, dict)
            or observed.get("images") != expected["images"]
            or observed.get("images_sha256")
            != _stable_json_sha256(expected["images"])
            or not isinstance(observed.get("pods"), dict)
            or observed["pods"].get("runtime_image_ids_sha256")
            != flux_baseline.get(name)
        ):
            raise RuntimeErrorEB(
                f"status does not prove the live Flux controller contract: {name}"
            )
        _require_stored_pod_image_contract(
            observed["pods"],
            1,
            expected["images"],
            f"Flux controller Pod {name}",
        )

    namespace_status = status_payload.get("namespace_security")
    expected_namespaces = _versioned_namespace_security_contract(
        source_commit
    )
    if (
        not isinstance(namespace_status, dict)
        or set(namespace_status) != set(expected_namespaces)
    ):
        raise RuntimeErrorEB(
            "status does not prove the live Namespace security contract"
        )
    for name, expected in expected_namespaces.items():
        observed = namespace_status.get(name)
        if (
            not isinstance(observed, dict)
            or observed.get("canonical") is not True
            or observed.get("labels") != expected["labels"]
            or observed.get("labels_sha256") != expected["labels_sha256"]
        ):
            raise RuntimeErrorEB(
                f"status does not prove the live Namespace security contract: {name}"
            )

    data_deployment_status = status_payload.get("data_deployments")
    expected_data_deployments = {
        name: _source_commit_data_deployment_contract(
            source_commit,
            CLUSTER / f"data/{name}.yaml",
            name,
        )
        for name in ("postgres", "nats")
    }
    if (
        not isinstance(data_deployment_status, dict)
        or set(data_deployment_status) != set(expected_data_deployments)
    ):
        raise RuntimeErrorEB("status does not prove the live data Deployment contract")
    for name, expected in expected_data_deployments.items():
        observed = data_deployment_status.get(name)
        if (
            not isinstance(observed, dict)
            or observed.get("available") is not True
            or observed.get("desired_replicas") != expected["replicas"]
            or observed.get("images_canonical") is not True
            or observed.get("images_sha256")
            != _stable_json_sha256(expected["images"])
            or observed.get("canonical") is not True
            or observed.get("contract_sha256")
            != expected["contract_sha256"]
            or observed.get("pod_contract_sha256")
            != expected["pod_contract_sha256"]
            or not isinstance(observed.get("pod_names"), list)
            or len(observed["pod_names"]) != expected["replicas"]
        ):
            raise RuntimeErrorEB(
                f"status does not prove the live data Deployment contract: {name}"
            )
        _require_stored_pod_image_contract(
            observed.get("pods"),
            expected["replicas"],
            expected["images"],
            f"data Pod {name}",
        )

    pvc_status = status_payload.get("pvcs")
    expected_pvcs = _rendered_pvc_contract(root, source_commit)
    if (
        not isinstance(pvc_status, dict)
        or set(pvc_status) != set(expected_pvcs)
    ):
        raise RuntimeErrorEB(
            "status does not prove the complete PVC contract"
        )
    for key, expected in expected_pvcs.items():
        observed = pvc_status.get(key)
        if (
            not isinstance(observed, dict)
            or observed.get("phase") != "Bound"
            or observed.get("canonical") is not True
            or observed.get("spec_sha256") != expected["spec_sha256"]
        ):
            raise RuntimeErrorEB(
                f"status does not prove the complete PVC contract: {key}"
            )

    data_service_status = status_payload.get("data_services")
    expected_data_services = {
        name: _source_commit_data_service_contract(
            source_commit,
            CLUSTER / f"data/{name}.yaml",
            name,
        )
        for name in ("postgres", "nats")
    }
    if (
        not isinstance(data_service_status, dict)
        or set(data_service_status) != set(expected_data_services)
    ):
        raise RuntimeErrorEB("status does not prove the live data Service contract")
    for name, expected in expected_data_services.items():
        observed = data_service_status.get(name)
        if (
            not isinstance(observed, dict)
            or observed.get("canonical") is not True
            or observed.get("spec") != expected["spec"]
            or observed.get("spec_sha256") != expected["spec_sha256"]
        ):
            raise RuntimeErrorEB(
                f"status does not prove the live data Service contract: {name}"
            )


    migration_status = status_payload.get("migration")
    expected_migration = _rendered_migration_job_contract(
        root,
        str(payloads["release.json"].get("api_digest", "")),
        source_commit,
    )
    if (
        status_payload.get("migration_complete") is not True
        or not isinstance(migration_status, dict)
        or migration_status.get("canonical") is not True
        or migration_status.get("contract_sha256")
        != expected_migration["contract_sha256"]
        or migration_status.get("pod_contract_sha256")
        != expected_migration["pod_contract_sha256"]
        or migration_status.get("succeeded_pods")
        != expected_migration["contract"]["completions"]
        or not isinstance(migration_status.get("pod_names"), list)
        or len(migration_status["pod_names"])
        < expected_migration["contract"]["completions"]
    ):
        raise RuntimeErrorEB(
            "status does not prove the complete migration Job/Pod contract"
        )

    application_service_status = status_payload.get("application_services")
    expected_application_services = _rendered_application_service_contract(
        root, payloads["release.json"]
    )
    if (
        not isinstance(application_service_status, dict)
        or set(application_service_status) != set(expected_application_services)
    ):
        raise RuntimeErrorEB(
            "status does not prove the application Service contract"
        )
    for name, expected in expected_application_services.items():
        observed = application_service_status.get(name)
        if (
            not isinstance(observed, dict)
            or observed.get("canonical") is not True
            or observed.get("spec") != expected["spec"]
            or observed.get("spec_sha256") != expected["spec_sha256"]
        ):
            raise RuntimeErrorEB(
                f"status does not prove the application Service contract: {name}"
            )

    pod_status = status_payload.get("pods")
    api_digest = str(payloads["release.json"].get("api_digest", ""))
    web_digest = str(payloads["release.json"].get("web_digest", ""))
    expected_pod_contracts = {
        "weltgewebe-api": {
            "replicas": int(config["semantic_search"]["api_replicas"]),
            "images": {
                "api": f"ghcr.io/heimgewebe/commonthing-api@{api_digest}",
                "search-worker": f"ghcr.io/heimgewebe/commonthing-api@{api_digest}",
                "ollama": str(config["semantic_search"]["ollama_image"]),
            },
        },
        "weltgewebe-web": {
            "replicas": int(config["runtime_binding"]["web_replicas"]),
            "images": {
                "web": f"ghcr.io/heimgewebe/commonthing-web@{web_digest}",
            },
        },
    }
    if (
        not isinstance(pod_status, dict)
        or set(pod_status) != set(expected_pod_contracts)
    ):
        raise RuntimeErrorEB("status does not prove the live application Pod contract")
    for workload, expected in expected_pod_contracts.items():
        observed = pod_status.get(workload)
        expected_digests = {
            name: image.rsplit("@", 1)[1]
            for name, image in expected["images"].items()
        }
        if (
            not isinstance(observed, dict)
            or observed.get("expected_replicas") != expected["replicas"]
            or observed.get("observed_replicas") != expected["replicas"]
            or observed.get("requested_images_sha256")
            != _stable_json_sha256(expected["images"])
            or observed.get("images_canonical") is not True
            or not isinstance(observed.get("pods"), dict)
            or len(observed["pods"]) != expected["replicas"]
            or any(
                not isinstance(pod, dict)
                or pod.get("ready") is not True
                or pod.get("requested_images_sha256")
                != _stable_json_sha256(expected["images"])
                or not isinstance(pod.get("runtime_image_ids"), dict)
                or set(pod["runtime_image_ids"]) != set(expected_digests)
                or any(
                    not _runtime_image_id_matches_digest(
                        pod["runtime_image_ids"].get(container_name),
                        expected_digest,
                    )
                    for container_name, expected_digest in expected_digests.items()
                )
                for pod in observed["pods"].values()
            )
        ):
            raise RuntimeErrorEB(
                f"status does not prove the live application Pod contract: {workload}"
            )

    application_workload_status = status_payload.get(
        "application_workloads"
    )
    expected_application_workloads = _rendered_application_workload_contract(
        root, payloads["release.json"]
    )
    if (
        not isinstance(application_workload_status, dict)
        or set(application_workload_status)
        != set(expected_application_workloads)
    ):
        raise RuntimeErrorEB(
            "status does not prove the complete application workload contract"
        )
    for name, expected in expected_application_workloads.items():
        observed = application_workload_status.get(name)
        if (
            not isinstance(observed, dict)
            or observed.get("canonical") is not True
            or observed.get("contract_sha256")
            != expected["contract_sha256"]
            or observed.get("pod_contract_sha256")
            != expected["pod_contract_sha256"]
            or not isinstance(observed.get("pod_names"), list)
            or len(observed["pod_names"])
            != expected["contract"]["replicas"]
        ):
            raise RuntimeErrorEB(
                f"status does not prove the complete application workload contract: {name}"
            )

    expected_application_pod_inventory = sorted(
        {
            *migration_status["pod_names"],
            *(
                pod_name
                for workload in application_workload_status.values()
                for pod_name in workload["pod_names"]
            ),
        }
    )
    if (
        status_payload.get("application_pod_inventory")
        != expected_application_pod_inventory
    ):
        raise RuntimeErrorEB(
            "status does not prove the complete application Pod inventory"
        )

    service_account_status = status_payload.get(
        "application_service_accounts"
    )
    expected_service_accounts = (
        _rendered_application_service_account_contract(
            root, payloads["release.json"]
        )
    )
    if (
        not isinstance(service_account_status, dict)
        or set(service_account_status) != set(expected_service_accounts)
    ):
        raise RuntimeErrorEB(
            "status does not prove the application ServiceAccount contract"
        )
    for name, expected in expected_service_accounts.items():
        observed = service_account_status.get(name)
        if (
            not isinstance(observed, dict)
            or observed.get("canonical") is not True
            or observed.get("contract_sha256")
            != expected["contract_sha256"]
        ):
            raise RuntimeErrorEB(
                f"status does not prove the application ServiceAccount contract: {name}"
            )

    pdb_status = status_payload.get(
        "application_disruption_budgets"
    )
    expected_pdbs = _rendered_application_pdb_contract(
        root,
        payloads["release.json"],
    )
    if (
        not isinstance(pdb_status, dict)
        or set(pdb_status) != set(expected_pdbs)
    ):
        raise RuntimeErrorEB(
            "status does not prove the application PDB contract"
        )
    for name, expected in expected_pdbs.items():
        observed = pdb_status.get(name)
        if (
            not isinstance(observed, dict)
            or observed.get("canonical") is not True
            or observed.get("contract_sha256")
            != expected["contract_sha256"]
        ):
            raise RuntimeErrorEB(
                f"status does not prove the application PDB contract: {name}"
            )

    runtime_status = status_payload.get("runtime_contract")
    runtime_binding = config["runtime_binding"]
    data_network_specs = _source_commit_network_policy_specs(
        source_commit,
        CLUSTER / "data/network-policy.yaml",
        DATA_NAMESPACE,
    )
    if (
        not isinstance(runtime_status, dict)
        or runtime_status.get("config_map_data_sha256")
        != _stable_json_sha256(runtime_binding["config_map_data"])
        or runtime_status.get("network_policy_specs_sha256")
        != _stable_json_sha256(runtime_binding["network_policy_specs"])
        or runtime_status.get("network_policy_names")
        != sorted(runtime_binding["network_policy_specs"])
        or runtime_status.get("data_network_policy_specs_sha256")
        != _stable_json_sha256(data_network_specs)
        or runtime_status.get("data_network_policy_names")
        != sorted(data_network_specs)
        or runtime_status.get("cilium_network_policy_specs_sha256")
        != _stable_json_sha256(runtime_binding["cilium_network_policy_specs"])
        or runtime_status.get("cilium_network_policy_names")
        != sorted(runtime_binding["cilium_network_policy_specs"])
        or runtime_status.get("temporary_model_egress_absent") is not True
        or runtime_status.get("policy_specs_canonical") is not True
    ):
        raise RuntimeErrorEB(
            "status does not prove the live runtime configuration contract"
        )

    if _current_protected_main_commit() != source_commit:
        raise RuntimeErrorEB(
            "portability release is no longer current protected main"
        )

    result = {
        "schema_version": 1,
        "status": "pass",
        "source_commit": source_commit,
        "receipts": receipts,
        "portable_invariants": [
            "exact protected-main Git commit",
            "immutable GHCR API/Web digests",
            "external registry/database/runtime secrets",
            "Cilium Gateway API and kube-proxy replacement",
            "Flux/Kustomize reconciliation",
            "persistent PostgreSQL and JetStream state",
            "literal-loopback Ollama provider contract",
        ],
        "removed_staging_specific_mechanisms": [
            "kind runtime",
            "staging_cell.py runtime controller",
            "static kind hostPath PVs",
            "kind worker nodeSelector and nodeAffinity",
        ],
        "experiment_b_specific_mechanisms": [
            "single libvirt/KVM Ubuntu VM",
            "k3s local-path storage",
            "temporary model-download egress",
            "host-side pinned k6 load generator",
            "synthetic canonical-T048 search projections",
        ],
        "production_green_open_questions": [
            "multi-node failure-domain and HA behavior",
            "production-grade storage class and backup backend",
            "production ingress/DNS/TLS ownership",
            "production capacity beyond Experiment-B calibration",
        ],
        "does_not_establish": [
            "production Kubernetes cutover",
            "production capacity",
            "independent physical failure domains",
        ],
    }
    atomic_json(root / "receipts/portability.json", result)
    return result


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--state-root")
    sub = p.add_subparsers(dest="command", required=True)
    pre = sub.add_parser("preflight")
    pre.add_argument("--source-commit")
    sub.add_parser("prepare")
    sub.add_parser("create-vm")
    sub.add_parser("install-k3s")
    sub.add_parser("install-platform")
    sec = sub.add_parser("inject-secrets")
    sec.add_argument("--registry-config", required=True)
    rel = sub.add_parser("apply-release")
    rel.add_argument("--source-commit", required=True)
    rel.add_argument("--api-digest", required=True)
    rel.add_argument("--web-digest", required=True)
    sub.add_parser("seed-t048-fixture")
    sub.add_parser("semantic-activate")
    functional = sub.add_parser("functional-readback")
    functional.add_argument("--source-commit", required=True)
    performance = sub.add_parser("t048-load-proof")
    performance.add_argument("--source-commit", required=True)
    sub.add_parser("recovery-proof")
    sub.add_parser("portability-report")
    sub.add_parser("status")
    sub.add_parser("teardown")
    return p


def main() -> int:
    args = parser().parse_args()
    root = state_root(args.state_root)
    if args.command == "preflight":
        result = preflight(args.source_commit)
    elif args.command == "prepare":
        result = prepare(root)
    elif args.command == "create-vm":
        result = create_vm(root)
    elif args.command == "install-k3s":
        result = install_k3s(root)
    elif args.command == "install-platform":
        result = install_platform(root)
    elif args.command == "inject-secrets":
        inject_secrets(
            root, Path(args.registry_config).expanduser().resolve()
        )
        result = {
            "schema_version": 1,
            "status": "ready",
            "receipt": "receipts/secrets.json",
        }
    elif args.command == "apply-release":
        result = apply_release(
            root, args.source_commit, args.api_digest, args.web_digest
        )
    elif args.command == "seed-t048-fixture":
        result = seed_t048_fixture(root)
    elif args.command == "semantic-activate":
        result = semantic_activate(root)
    elif args.command == "functional-readback":
        result = functional_readback(root, args.source_commit)
    elif args.command == "t048-load-proof":
        result = t048_load_proof(root, args.source_commit)
    elif args.command == "recovery-proof":
        result = recovery_proof(root)
    elif args.command == "portability-report":
        result = portability_report(root)
    elif args.command == "status":
        status(root)
        result = {
            "schema_version": 1,
            "status": "observed",
            "receipt": "receipts/status.json",
        }
    elif args.command == "teardown":
        result = teardown(root)
    else:
        raise RuntimeErrorEB(f"unsupported command: {args.command}")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeErrorEB, ContractError) as exc:
        print(json.dumps({"status": "error", "error_class": type(exc).__name__}, sort_keys=True))
        raise SystemExit(2)
