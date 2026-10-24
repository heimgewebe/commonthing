#!/usr/bin/env python3
"""Opt-in external transport for durable Commonthing watcher transitions.

The source of truth is the existing fsynced events.jsonl. This worker does
not infer a human acknowledgment or guarantee exactly-once network delivery.
No network call is possible without an owner-only topic and explicit enrollment.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
from urllib.request import Request, urlopen

STATE_DIR = Path.home() / ".local/commonthing-watch-state"
TOPIC_FILE = Path.home() / ".config/commonthing-watch/ntfy-topic"
SERVER = "https://ntfy.sh"
CURSOR_KIND = "weltgewebe.watch-transport-cursor.v1"
MAX_JOURNAL_BYTES = 32 * 1024 * 1024
MAX_LINE_BYTES = 65536
MAX_CURSOR_BYTES = 8192
MAX_BATCH = 8
TOPIC_RE = re.compile(r"[A-Za-z0-9]{32,128}\Z")
EVENTS = frozenset({"ALARM", "RECOVERY", "MONITOR_DATA_FAILURE"})
TRANSPORT_NON_CLAIMS = [
    "external_device_delivery",
    "human_acknowledgment",
    "exactly_once_network_effect",
    "production_recovery",
]


class TransportBlocked(ValueError):
    """Invalid or missing local authority; do not send anything."""


@contextmanager
def _exclusive_lock(directory: Path, name: str, *, nonblocking: bool = False):
    # No added lockfile may resolve through a symlink or an alien inode.
    fd = os.open(directory / name, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o022):
            raise TransportBlocked("transport lock identity invalid")
        fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        yield
    finally:
        os.close(fd)


def _checked_bytes(path: Path, limit: int, *, private: bool) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != os.getuid() or before.st_size > limit
                or (stat.S_IMODE(before.st_mode) & (0o077 if private else 0o022))):
            raise TransportBlocked("file identity, ownership, permissions or budget invalid")
        data = bytearray()
        while len(data) < before.st_size:
            block = os.read(fd, min(65536, before.st_size - len(data)))
            if not block:
                raise TransportBlocked("file shortened during read")
            data.extend(block)
        after = os.fstat(fd)
        attrs = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, k) != getattr(after, k) for k in attrs):
            raise TransportBlocked("file changed during read")
        return bytes(data), before
    finally:
        os.close(fd)


def _private_dir(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise TransportBlocked("state or topic directory must be owner-only")


def read_topic(topic_file: Path) -> str:
    _private_dir(topic_file.parent)
    content, _ = _checked_bytes(topic_file, 256, private=True)
    try:
        topic = content.decode("ascii").strip()
    except UnicodeError as exc:
        raise TransportBlocked("topic must be ASCII") from exc
    if TOPIC_RE.fullmatch(topic) is None:
        raise TransportBlocked("topic must be an opaque 32-128 character identifier")
    return topic


def _journal_snapshot(directory: Path) -> tuple[bytes, os.stat_result, list[tuple[int, bytes, dict]]]:
    # Snapshot while sharing the writer's existing journal/state lock; release
    # it before network I/O so a slow external service cannot stall the watcher.
    with _exclusive_lock(directory, "lock"):
        raw, identity = _checked_bytes(directory / "events.jsonl", MAX_JOURNAL_BYTES,
                                       private=False)
    rows: list[tuple[int, bytes, dict]] = []
    end = 0
    if raw and not raw.endswith(b"\n"):
        raise TransportBlocked("incomplete journal tail")
    for line in raw.splitlines(keepends=True):
        end += len(line)
        if not 2 <= len(line) <= MAX_LINE_BYTES:
            raise TransportBlocked("journal event exceeds bounded line size")
        try:
            row = json.loads(line)
        except (UnicodeError, ValueError) as exc:
            raise TransportBlocked("invalid journal JSON") from exc
        if (not isinstance(row, dict) or row.get("event") not in EVENTS
                or not isinstance(row.get("checked_at_utc"), str)
                or not isinstance(row.get("issues"), list)):
            raise TransportBlocked("invalid journal event contract")
        rows.append((end, line, row))
    return raw, identity, rows


def _cursor_path(directory: Path) -> Path:
    return directory / "transport-cursor.json"


def _write_cursor(directory: Path, value: dict) -> None:
    path = _cursor_path(directory)
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(raw) > MAX_CURSOR_BYTES:
        raise TransportBlocked("transport cursor exceeds size budget")
    temporary = directory / (".transport-cursor." + secrets.token_hex(12) + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        # Failure before replace: remove only our own create-only temp path.
        # No recursive cleanup or operations on the event journal.
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_cursor(directory: Path, raw: bytes, identity: os.stat_result,
                 topic: str) -> dict:
    data, _ = _checked_bytes(_cursor_path(directory), MAX_CURSOR_BYTES, private=True)
    try:
        value = json.loads(data)
    except (UnicodeError, ValueError) as exc:
        raise TransportBlocked("transport cursor unreadable") from exc
    required = {
        "kind", "journal_device", "journal_inode", "offset", "prefix_sha256",
        "topic_sha256", "accepted", "seen_ids", "last_acceptance",
        "previous_history_not_delivered",
    }
    if (not isinstance(value, dict) or set(value) != required
            or value["kind"] != CURSOR_KIND
            or type(value["journal_device"]) is not int
            or type(value["journal_inode"]) is not int
            or (value["journal_device"], value["journal_inode"])
               != (identity.st_dev, identity.st_ino)
            or type(value["offset"]) is not int
            or not 0 <= value["offset"] <= len(raw)
            or type(value["accepted"]) is not int or value["accepted"] < 0
            or value["previous_history_not_delivered"] is not True
            or not isinstance(value["seen_ids"], list)
            or len(value["seen_ids"]) > 32
            or not all(isinstance(x, str) and re.fullmatch(r"[0-9a-f]{64}", x)
                       for x in value["seen_ids"])
            or value["last_acceptance"] is not None
               and not isinstance(value["last_acceptance"], dict)):
        raise TransportBlocked("invalid or rebound transport cursor")
    if value["topic_sha256"] != hashlib.sha256(topic.encode("ascii")).hexdigest():
        raise TransportBlocked("transport recipient changed without explicit enrollment")
    if value["prefix_sha256"] != hashlib.sha256(raw[:value["offset"]]).hexdigest():
        raise TransportBlocked("journal prefix changed since transport acknowledgment")
    if value["offset"] and raw[value["offset"] - 1:value["offset"]] != b"\n":
        raise TransportBlocked("cursor does not end on a complete journal record")
    return value


def enroll(directory: Path, topic_file: Path, *, acknowledge_prior_history: bool) -> dict:
    if not acknowledge_prior_history:
        raise TransportBlocked("enrollment must acknowledge old events are not delivered")
    _private_dir(directory)
    topic = read_topic(topic_file)
    with _exclusive_lock(directory, "transport.lock", nonblocking=True):
        if _cursor_path(directory).exists() or _cursor_path(directory).is_symlink():
            raise TransportBlocked("existing transport enrollment cannot be silently replaced")
        raw, identity, _ = _journal_snapshot(directory)
        value = {
            "kind": CURSOR_KIND,
            "journal_device": identity.st_dev,
            "journal_inode": identity.st_ino,
            "offset": len(raw),
            "prefix_sha256": hashlib.sha256(raw).hexdigest(),
            "topic_sha256": hashlib.sha256(topic.encode("ascii")).hexdigest(),
            "accepted": 0,
            "seen_ids": [],
            "last_acceptance": None,
            "previous_history_not_delivered": True,
        }
        _write_cursor(directory, value)
        return {"status": "enrolled_no_delivery", "offset": len(raw),
                "previous_history_not_delivered": True}


def _post_ntfy(topic: str, event: dict, event_id: str) -> int:
    codes = sorted({
        x["code"] for x in event["issues"]
        if isinstance(x, dict) and isinstance(x.get("code"), str)
        and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", x["code"])
    })
    status = event["event"]
    message = (f"Commonthing production {status}: "
               + (", ".join(codes[:12]) if codes else "incident transition")
               + f" (event {event_id[:16]})")
    request = Request(
        f"{SERVER}/{topic}", data=message.encode("utf-8"), method="POST",
        headers={
            "Title": f"Commonthing: {status}",
            "Priority": "5" if status == "ALARM" else "3",
            "Tags": "warning" if status == "ALARM" else "white_check_mark",
            "X-Commonthing-Event-Id": event_id,
            "User-Agent": "commonthing-independent-watch-transport/1",
        },
    )
    with urlopen(request, timeout=6) as response:
        code = int(response.status)
        response.read(1024)
    return code


def dispatch(directory: Path, topic_file: Path, *, publisher=_post_ntfy) -> dict:
    _private_dir(directory)
    topic = read_topic(topic_file)
    with _exclusive_lock(directory, "transport.lock", nonblocking=True):
        raw, identity, rows = _journal_snapshot(directory)
        cursor = _read_cursor(directory, raw, identity, topic)
        processed = 0
        newly_accepted = 0
        for end, line, event in rows:
            if end <= cursor["offset"]:
                continue
            if processed >= MAX_BATCH:
                break
            event_id = hashlib.sha256(line).hexdigest()
            if event["event"] in {"ALARM", "RECOVERY"} and event_id not in cursor["seen_ids"]:
                http_code = publisher(topic, event, event_id)
                if type(http_code) is not int or http_code not in {200, 201, 202}:
                    raise TransportBlocked("remote transport was not accepted")
                cursor["accepted"] += 1
                newly_accepted += 1
                cursor["last_acceptance"] = {
                    "event_id": event_id,
                    "event": event["event"],
                    "http_status": http_code,
                    "accepted_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "does_not_establish": TRANSPORT_NON_CLAIMS,
                }
                cursor["seen_ids"] = (cursor["seen_ids"] + [event_id])[-32:]
            cursor["offset"] = end
            cursor["prefix_sha256"] = hashlib.sha256(raw[:end]).hexdigest()
            _write_cursor(directory, cursor)
            processed += 1
        return {
            "status": "transport_http_accepted" if newly_accepted else "no_new_alarm_delivery",
            "processed": processed, "accepted": newly_accepted,
            "more_pending": len(raw) > cursor["offset"],
            "does_not_establish": TRANSPORT_NON_CLAIMS,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR)
    parser.add_argument("--enroll", action="store_true")
    parser.add_argument("--acknowledge-prior-events-not-delivered", action="store_true")
    args = parser.parse_args()
    try:
        if args.enroll:
            result = enroll(args.state_dir, TOPIC_FILE,
                            acknowledge_prior_history=args.acknowledge_prior_events_not_delivered)
        elif args.acknowledge_prior_events_not_delivered:
            parser.error("enrollment acknowledgment is valid only with --enroll")
        else:
            result = dispatch(args.state_dir, TOPIC_FILE)
    except (OSError, ValueError, BlockingIOError, TimeoutError) as exc:
        # Never print exception detail: urllib errors can contain the private
        # ntfy topic URL, and topic/recipient bytes are not loggable.
        print(f"watch transport blocked: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
