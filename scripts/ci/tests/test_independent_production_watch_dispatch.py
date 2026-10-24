#!/usr/bin/env python3
"""Offline tests for the opt-in independent watcher notification transport."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[2] / "ops/independent_production_watch_dispatch.py"
SPEC = importlib.util.spec_from_file_location("watch_dispatch", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
dispatch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dispatch)

STAMP = "2026-10-24T12:00:00Z"
TOPIC = "A" * 40


class WatchDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="watch-delivery-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.topic_dir = self.root / "recipient"
        self.topic_dir.mkdir(mode=0o700)
        self.topic_file = self.topic_dir / "ntfy-topic"
        self.topic_file.write_text(TOPIC + "\n", encoding="ascii")
        self.topic_file.chmod(0o600)
        self.journal = self.state / "events.jsonl"
        self.journal.touch(mode=0o600)
        self.calls = []

    def event(self, name: str, code: str, **extra) -> dict:
        return {
            "event": name,
            "status": "ALARM" if name == "ALARM" else "HEALTHY",
            "checked_at_utc": STAMP,
            "issues": [{"code": code, "actual": "only local", "severity": "P1"}],
            **extra,
        }

    def append(self, item: dict) -> bytes:
        line = (json.dumps(item, sort_keys=True) + "\n").encode("utf-8")
        with self.journal.open("ab") as handle:
            handle.write(line)
        return line

    def sender(self, topic, event, event_id):
        self.assertEqual(topic, TOPIC)
        self.assertIn(event["event"], {"ALARM", "RECOVERY"})
        self.calls.append((event["event"], event_id))
        return 200

    def enroll(self):
        return dispatch.enroll(
            self.state, self.topic_file, acknowledge_prior_history=True,
        )

    def run_transport(self, publisher=None):
        return dispatch.dispatch(
            self.state, self.topic_file, publisher=publisher or self.sender,
        )

    def cursor(self):
        return json.loads((self.state / "transport-cursor.json").read_text())

    def test_enrollment_explicitly_skips_existing_history_without_delivery(self):
        initial = self.append(self.event("ALARM", "old"))
        with self.assertRaises(dispatch.TransportBlocked):
            dispatch.enroll(
                self.state, self.topic_file, acknowledge_prior_history=False,
            )
        self.assertFalse((self.state / "transport-cursor.json").exists())
        result = self.enroll()
        self.assertEqual(result["status"], "enrolled_no_delivery")
        self.assertEqual(result["offset"], len(initial))
        self.assertEqual(self.run_transport()["accepted"], 0)
        self.assertEqual(self.calls, [])
        with self.assertRaises(dispatch.TransportBlocked):
            self.enroll()

    def test_alarm_recovery_alarm_fifo_and_no_duplicate_on_next_poll(self):
        self.enroll()
        lines = [
            self.append(self.event("ALARM", "first")),
            self.append(self.event("RECOVERY", "first")),
            self.append(self.event("ALARM", "second")),
        ]
        result = self.run_transport()
        self.assertEqual(result["accepted"], 3)
        self.assertFalse(result["more_pending"])
        self.assertEqual(
            self.calls, [
                ("ALARM", hashlib.sha256(lines[0]).hexdigest()),
                ("RECOVERY", hashlib.sha256(lines[1]).hexdigest()),
                ("ALARM", hashlib.sha256(lines[2]).hexdigest()),
            ],
        )
        self.assertEqual(self.run_transport()["accepted"], 0)
        self.assertEqual(len(self.calls), 3)
        receipt = self.cursor()["last_acceptance"]
        self.assertEqual(receipt["http_status"], 200)
        self.assertIn("human_acknowledgment", receipt["does_not_establish"])
        self.assertEqual(self.cursor()["accepted"], 3)

    def test_transient_send_failure_preserves_exact_unacknowledged_event(self):
        self.enroll()
        first = self.append(self.event("ALARM", "one"))
        second = self.append(self.event("RECOVERY", "one"))
        self.append(self.event("ALARM", "two"))

        def unstable(topic, event, identifier):
            if event["event"] == "RECOVERY":
                raise TimeoutError("simulated transport")
            return self.sender(topic, event, identifier)

        with self.assertRaises(TimeoutError):
            self.run_transport(publisher=unstable)
        self.assertEqual(self.calls, [("ALARM", hashlib.sha256(first).hexdigest())])
        self.assertEqual(self.cursor()["offset"], len(first))
        result = self.run_transport()
        self.assertEqual(result["accepted"], 2)
        self.assertEqual(self.calls[1][1], hashlib.sha256(second).hexdigest())

    def test_bad_http_status_does_not_advance_cursor(self):
        self.enroll()
        self.append(self.event("ALARM", "down"))
        with self.assertRaises(dispatch.TransportBlocked):
            self.run_transport(publisher=lambda *_: 500)
        self.assertEqual(self.cursor()["accepted"], 0)
        self.assertEqual(self.cursor()["offset"], 0)
        self.assertEqual(self.run_transport()["accepted"], 1)

    def test_duplicate_durable_payload_is_transport_deduplicated(self):
        self.enroll()
        item = self.event("ALARM", "same")
        self.append(item)
        self.append(item)
        self.assertEqual(self.run_transport()["accepted"], 1)
        self.assertEqual(self.cursor()["offset"], self.journal.stat().st_size)
        self.assertEqual(self.cursor()["accepted"], 1)

    def test_bounded_batch_preserves_backlog(self):
        self.enroll()
        for index in range(dispatch.MAX_BATCH + 2):
            self.append(self.event("ALARM", f"alarm_{index}"))
        first = self.run_transport()
        self.assertEqual(first["processed"], dispatch.MAX_BATCH)
        self.assertTrue(first["more_pending"])
        second = self.run_transport()
        self.assertEqual(second["accepted"], 2)
        self.assertEqual(len(self.calls), dispatch.MAX_BATCH + 2)

    def test_observer_uncertainty_is_not_falsely_sent_as_p1_alarm(self):
        self.enroll()
        self.append(self.event("MONITOR_DATA_FAILURE", "github_main"))
        self.append(self.event("ALARM", "real_p1"))
        result = self.run_transport()
        self.assertEqual(result["processed"], 2)
        self.assertEqual(result["accepted"], 1)
        self.assertEqual([item[0] for item in self.calls], ["ALARM"])

    def test_journal_prefix_edit_is_not_treated_as_delivered(self):
        self.enroll()
        self.append(self.event("ALARM", "first"))
        self.run_transport()
        saved = self.journal.read_bytes()
        self.journal.write_bytes(saved.replace(b"first", b"other"))
        with self.assertRaises(dispatch.TransportBlocked):
            self.run_transport()
        self.assertEqual(len(self.calls), 1)

    def test_new_journal_inode_is_not_automatically_reenrolled(self):
        self.enroll()
        replacement = self.state / "replacement"
        replacement.touch(mode=0o600)
        os.replace(replacement, self.journal)
        with self.assertRaises(dispatch.TransportBlocked):
            self.run_transport()

    def test_partial_and_malformed_suffix_block_before_any_send(self):
        self.enroll()
        self.append(self.event("ALARM", "good"))
        with self.journal.open("ab") as out:
            out.write(b'{"event":"ALARM"')
        with self.assertRaises(dispatch.TransportBlocked):
            self.run_transport()
        self.assertEqual(self.calls, [])
        self.journal.write_bytes(
            json.dumps(self.event("ALARM", "good")).encode() + b"\n" + b"bad\n"
        )
        with self.assertRaises(dispatch.TransportBlocked):
            self.run_transport()
        self.assertEqual(self.calls, [])

    def test_missing_enrollment_or_wrong_topic_is_fail_closed(self):
        self.append(self.event("ALARM", "down"))
        with self.assertRaises(FileNotFoundError):
            self.run_transport()
        self.enroll()
        self.topic_file.write_text("B" * 40 + "\n")
        self.topic_file.chmod(0o600)
        with self.assertRaises(dispatch.TransportBlocked):
            self.run_transport()
        self.assertEqual(self.calls, [])

    def test_replaced_dispatch_or_journal_lock_blocks_before_network(self):
        self.enroll()
        self.append(self.event("ALARM", "down"))
        path = self.state / "transport.lock"
        path.unlink()
        path.symlink_to(self.topic_file)
        with self.assertRaises(OSError):
            self.run_transport()
        self.assertEqual(self.calls, [])
        path.unlink()
        self.assertEqual(self.run_transport()["accepted"], 1)
        # The journal/state serialization lock is also an authority boundary.
        shared = self.state / "lock"
        shared.unlink()
        shared.symlink_to(self.topic_file)
        self.append(self.event("RECOVERY", "down"))
        with self.assertRaises(OSError):
            self.run_transport()
        self.assertEqual(len(self.calls), 1)

    def test_secret_file_is_strictly_owner_only_and_not_symlinked(self):
        self.topic_file.chmod(0o644)
        with self.assertRaises(dispatch.TransportBlocked):
            self.enroll()
        self.topic_file.chmod(0o600)
        self.topic_file.unlink()
        self.topic_file.symlink_to(self.root / "other")
        with self.assertRaises(OSError):
            self.enroll()

    def test_post_before_durable_cursor_ack_cannot_claim_exactly_once(self):
        self.enroll()
        self.append(self.event("ALARM", "p1"))
        original = dispatch._write_cursor
        with mock.patch.object(dispatch, "_write_cursor", side_effect=OSError("fsync failed")):
            with self.assertRaises(OSError):
                self.run_transport()
        self.assertEqual(self.cursor()["accepted"], 0)
        self.assertEqual(len(self.calls), 1)
        # A provider-accepted POST followed by a failed local fsync is
        # fundamentally ambiguous; a later retry may send it again.
        with mock.patch.object(dispatch, "_write_cursor", wraps=original):
            self.assertEqual(self.run_transport()["accepted"], 1)
        self.assertEqual(len(self.calls), 2)

    def test_cron_keeps_poll_and_opt_in_transport_in_one_five_minute_schedule(self):
        crontab = (SCRIPT.parent / "independent_production_watch.crontab").read_text()
        self.assertIn("*/5 * * * *", crontab)
        self.assertIn("/home/alex/.local/commonthing-production-watch.py", crontab)
        self.assertIn("/home/alex/.local/commonthing-production-watch-dispatch.py", crontab)
        self.assertIn(" >/dev/null 2>&1; ", crontab)
        self.assertEqual(crontab.count("*/5 * * * *"), 1)


if __name__ == "__main__":
    unittest.main()
