#!/usr/bin/env python3
"""Fail-closed, dated water-polygon input for the regional PMTiles builders.

This input is deliberately different from the historical Germany input.  Its
identity is the dated upstream archive *and* the verified exact byte digest.
No fallback to live, unpinned data is permitted.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
import signal
from pathlib import Path
import stat
import sys
import tempfile
import time
from typing import Callable
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

URL = (
    "https://ftp.uni-erlangen.de/pub/openstreetmapdata.com-archive/"
    "2026-10-18/water-polygons-split-3857.zip"
)
SHA256 = "e2b908a677d4f027c179cac568993b02b310a89f5e72ac7ff76301568b5d0e65"
SIZE = 931_543_967
NAME = "water-polygons-split-3857.zip"
RELATIVE_PATH = Path("data/sources") / NAME
CHUNK = 1024 * 1024
MAX_DOWNLOAD_SECONDS = 900


class SourceError(RuntimeError):
    """The input source cannot be trusted; do not build or publish PMTiles."""


class _RejectHTTPRedirects(HTTPRedirectHandler):
    """Reject 30x *before* urllib contacts an arbitrary redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SourceError("unapproved HTTP redirect")


# The real CLI always uses this opener; only isolated tests inject a substitute.
_PINNED_OPENER = build_opener(_RejectHTTPRedirects())


def _pinned_open(request: Request, timeout: float):
    return _PINNED_OPENER.open(request, timeout=timeout)


@contextmanager
def _hard_download_deadline(seconds: float):
    """Interrupt a stalled HTTP read, not merely check time after it returns."""
    if not hasattr(signal, "setitimer"):
        raise SourceError("hard download deadline unavailable on this host")
    alarm = signal.ITIMER_REAL
    if signal.getitimer(alarm)[0] > 0:
        raise SourceError("download deadline alarm is already in use")

    def expired(_signum, _frame):
        raise SourceError("water-polygon download deadline exceeded")

    previous_handler = signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(alarm, seconds)
        yield
    finally:
        signal.setitimer(alarm, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def _directory(path: Path) -> None:
    if path.is_symlink():
        raise SourceError(f"source directory is a symlink: {path}")
    path.mkdir(mode=0o755, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise SourceError(f"source directory is not a real directory: {path}")


def _verify(path: Path, expected_sha256: str, expected_size: int) -> None:
    if path.is_symlink():
        raise SourceError(f"water-polygon cache is a symlink: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise SourceError(f"cannot open water-polygon cache: {exc}") from exc
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != expected_size:
            raise SourceError("water-polygon cache type/size mismatch")
        digest = hashlib.sha256()
        prefix = source.read(4)
        if prefix != bytes.fromhex("504b0304"):
            raise SourceError("water-polygon cache has invalid ZIP magic")
        digest.update(prefix)
        while block := source.read(CHUNK):
            digest.update(block)
        if digest.hexdigest() != expected_sha256:
            raise SourceError("water-polygon cache SHA-256 mismatch")


def ensure(
    base: Path,
    *,
    url: str = URL,
    expected_sha256: str = SHA256,
    expected_size: int = SIZE,
    opener: Callable = _pinned_open,
) -> str:
    """Verify existing bytes or atomically add a fully verified pinned source.

    opener is injectable solely for offline negative tests. The CLI below
    always uses the static URL, checksum and size declared above.
    """
    if (
        urlsplit(url).scheme != "https"
        or urlsplit(url).netloc != "ftp.uni-erlangen.de"
        or urlsplit(url).username is not None
        or not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or any(c not in "0123456789abcdef" for c in expected_sha256)
        or not isinstance(expected_size, int)
        or expected_size < 4
        or expected_size > SIZE
    ):
        raise SourceError("unapproved water-polygon source identity")

    if not base.is_dir() or base.is_symlink():
        raise SourceError("base directory must be a real existing directory")
    parent = base / "data"
    _directory(parent)
    target_dir = parent / "sources"
    _directory(target_dir)
    target = target_dir / NAME
    if target.exists() or target.is_symlink():
        _verify(target, expected_sha256, expected_size)
        return "reused"

    fd, temporary = tempfile.mkstemp(prefix=f".{NAME}.partial.", dir=target_dir)
    try:
        digest = hashlib.sha256()
        size = 0
        started = time.monotonic()
        with _hard_download_deadline(MAX_DOWNLOAD_SECONDS):
            request = Request(url, headers={"User-Agent": "commonThing-regional-pmtiles/1"})
            with os.fdopen(fd, "wb") as output:
                with opener(request, timeout=30) as response:
                    effective = urlsplit(response.geturl())
                    if (
                        effective.scheme != "https"
                        or effective.netloc != "ftp.uni-erlangen.de"
                        or getattr(response, "status", None) != 200
                    ):
                        raise SourceError("unapproved redirect or HTTP status")
                    while block := response.read(CHUNK):
                        size += len(block)
                        if size > expected_size:
                            raise SourceError("water-polygon download exceeds pinned size")
                        if time.monotonic() - started > MAX_DOWNLOAD_SECONDS:
                            raise SourceError("water-polygon download deadline exceeded")
                        digest.update(block)
                        output.write(block)
                output.flush()
                os.fsync(output.fileno())
        if size != expected_size:
            raise SourceError("water-polygon download is incomplete")
        if digest.hexdigest() != expected_sha256:
            raise SourceError("water-polygon download SHA-256 mismatch")
        with open(temporary, "rb") as source:
            if source.read(4) != bytes.fromhex("504b0304"):
                raise SourceError("water-polygon download has invalid ZIP magic")
        try:
            # Atomic no-clobber publication: never overwrite an existing cache.
            os.link(temporary, target)
        except FileExistsError as exc:
            raise SourceError("water-polygon cache appeared concurrently") from exc
        _verify(target, expected_sha256, expected_size)
        return "downloaded"
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: prepare-water-polygons.py BASEMAP_DIR", file=sys.stderr)
        return 2
    try:
        result = ensure(Path(sys.argv[1]))
    except (OSError, SourceError) as exc:
        print(f"Error: verified water-polygon input unavailable: {exc}", file=sys.stderr)
        return 1
    print(f"Regional water polygons {result}: {RELATIVE_PATH} SHA256={SHA256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())