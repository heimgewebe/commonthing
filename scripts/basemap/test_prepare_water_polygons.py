#!/usr/bin/env python3
"""Offline positive and negative proofs for the pinned regional coast input."""
from __future__ import annotations

import hashlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

FILE = Path(__file__).with_name("prepare-water-polygons.py")
SPEC = importlib.util.spec_from_file_location("regional_water_source", FILE)
assert SPEC is not None and SPEC.loader is not None
water = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(water)

ZIP = bytes.fromhex("504b0304") + b"fixture-coastline-20261018"
HASH = hashlib.sha256(ZIP).hexdigest()
TARGET = water.RELATIVE_PATH


class Response(io.BytesIO):
    def __init__(self, data: bytes, *, url: str = water.URL, status: int = 200):
        super().__init__(data)
        self._url = url
        self.status = status

    def geturl(self) -> str:
        return self._url


class PinnedSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / "basemap"
        self.base.mkdir()
        self.file = self.base / TARGET

    def run_ensure(self, *, data: bytes = ZIP, url: str = water.URL,
                   digest: str = HASH, size: int = len(ZIP),
                   effective_url: str = water.URL, status: int = 200) -> str:
        def opener(request, timeout: int):
            self.assertEqual(request.full_url, url)
            self.assertEqual(timeout, 30)
            return Response(data, url=effective_url, status=status)
        return water.ensure(
            self.base, url=url, expected_sha256=digest,
            expected_size=size, opener=opener,
        )

    def test_download_atomic_and_reuse_without_network(self) -> None:
        self.assertEqual(self.run_ensure(), "downloaded")
        self.assertEqual(self.file.read_bytes(), ZIP)
        self.assertEqual(list(self.file.parent.glob("*.partial.*")), [])
        self.assertEqual(
            water.ensure(
                self.base, expected_sha256=HASH, expected_size=len(ZIP),
                opener=lambda *_args, **_kwargs: self.fail("unexpected download"),
            ),
            "reused",
        )

    def test_corrupt_existing_hash_fails_closed_and_preserves_bytes(self) -> None:
        self.file.parent.mkdir(parents=True)
        self.file.write_bytes(ZIP[:-1] + b"X")
        with self.assertRaisesRegex(water.SourceError, "SHA-256 mismatch"):
            self.run_ensure()
        self.assertEqual(self.file.read_bytes(), ZIP[:-1] + b"X")

    def test_wrong_existing_size_fails_closed(self) -> None:
        self.file.parent.mkdir(parents=True)
        self.file.write_bytes(ZIP[:-1])
        with self.assertRaisesRegex(water.SourceError, "type/size mismatch"):
            self.run_ensure()

    def test_wrong_zip_magic_fails_closed(self) -> None:
        wrong = b"BAD!" + ZIP[4:]
        self.file.parent.mkdir(parents=True)
        self.file.write_bytes(wrong)
        with self.assertRaisesRegex(water.SourceError, "invalid ZIP magic"):
            self.run_ensure(digest=hashlib.sha256(wrong).hexdigest())

    def test_symlink_target_rejected(self) -> None:
        outside = Path(self.temp.name) / "outside"
        outside.write_bytes(ZIP)
        self.file.parent.mkdir(parents=True)
        self.file.symlink_to(outside)
        with self.assertRaisesRegex(water.SourceError, "symlink"):
            self.run_ensure()
        self.assertEqual(outside.read_bytes(), ZIP)

    def test_symlink_directory_rejected(self) -> None:
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (self.base / "data").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(water.SourceError, "symlink"):
            self.run_ensure()

    def test_partial_response_never_published(self) -> None:
        with self.assertRaisesRegex(water.SourceError, "incomplete"):
            self.run_ensure(data=ZIP[:-1])
        self.assertFalse(self.file.exists())
        self.assertEqual(list(self.file.parent.iterdir()), [])

    def test_oversized_response_never_published(self) -> None:
        with self.assertRaisesRegex(water.SourceError, "exceeds pinned size"):
            self.run_ensure(data=ZIP + b"X")
        self.assertFalse(self.file.exists())

    def test_wrong_sha_never_published(self) -> None:
        with self.assertRaisesRegex(water.SourceError, "SHA-256 mismatch"):
            self.run_ensure(digest="0" * 64)
        self.assertFalse(self.file.exists())

    def test_bad_magic_download_never_published(self) -> None:
        wrong = b"BAD!" + ZIP[4:]
        with self.assertRaisesRegex(water.SourceError, "invalid ZIP magic"):
            self.run_ensure(data=wrong, digest=hashlib.sha256(wrong).hexdigest())
        self.assertFalse(self.file.exists())

    def test_insecure_redirect_rejected(self) -> None:
        with self.assertRaisesRegex(water.SourceError, "unapproved redirect"):
            self.run_ensure(effective_url="http://ftp.uni-erlangen.de/archive")
        self.assertFalse(self.file.exists())

    def test_non_success_http_status_rejected(self) -> None:
        with self.assertRaisesRegex(water.SourceError, "HTTP status"):
            self.run_ensure(status=404)
        self.assertFalse(self.file.exists())

    def test_source_identity_change_rejected(self) -> None:
        with self.assertRaisesRegex(water.SourceError, "unapproved"):
            self.run_ensure(url="https://example.net/other.zip")

    def test_concurrent_publisher_never_overwrites(self) -> None:
        with mock.patch.object(water.os, "link", side_effect=FileExistsError):
            with self.assertRaisesRegex(water.SourceError, "appeared concurrently"):
                self.run_ensure()
        self.assertFalse(self.file.exists())
        self.assertEqual(list(self.file.parent.iterdir()), [])

    def test_network_failure_cleans_temp(self) -> None:
        def failing_opener(*_args, **_kwargs):
            raise OSError("network down")
        with self.assertRaisesRegex(OSError, "network down"):
            water.ensure(self.base, expected_sha256=HASH,
                         expected_size=len(ZIP), opener=failing_opener)
        self.assertFalse(self.file.exists())
        self.assertEqual(list(self.file.parent.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
