"""Adversarial tests for the measured T048 JetStream broker identity proof."""
from __future__ import annotations

import copy
import unittest

from scripts.ci.check_t048_jetstream import validate


ID = "a" * 64
IMAGE = "nats@sha256:" + "b" * 64
SERVER = "t048-ci-123-1"


class T048JetstreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.container = {
            "Id": ID,
            "Config": {"Image": IMAGE},
            "State": {"Running": True},
            "HostConfig": {"NetworkMode": "bridge"},
            "NetworkSettings": {
                "Ports": {
                    "4222/tcp": [{"HostIp": "127.0.0.1", "HostPort": "4222"}],
                    "8222/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8222"}],
                    "6222/tcp": None,
                },
            },
        }
        self.server = {"server_name": SERVER, "server_id": "NAUTHENTIC"}
        self.jetstream = {
            "server_id": "NAUTHENTIC",
            "total_streams": 1,
            "total_consumers": 1,
            "total_messages": 0,
            "account_details": [{
                "name": "$G",
                "stream_detail": [{
                    "name": "DOMAIN_EVENTS",
                    "consumer_detail": [{"name": "worker", "num_pending": 0, "num_ack_pending": 0}],
                }],
            }],
        }

    def prove(self) -> dict:
        return validate(
            self.container,
            self.server,
            self.jetstream,
            expected_container_id=ID,
            expected_image=IMAGE,
            expected_server_name=SERVER,
        )

    def test_empty_expected_broker_passes(self) -> None:
        self.assertEqual(self.prove()["consumers"], 1)

    def test_stopped_expected_container_fails(self) -> None:
        self.container["State"]["Running"] = False
        with self.assertRaisesRegex(ValueError, "liveness"):
            self.prove()

    def test_wrong_container_id_fails(self) -> None:
        self.container["Id"] = "c" * 64
        with self.assertRaisesRegex(ValueError, "identity"):
            self.prove()

    def test_wrong_image_fails(self) -> None:
        self.container["Config"]["Image"] = "nats:latest"
        with self.assertRaisesRegex(ValueError, "image"):
            self.prove()

    def test_host_network_fails(self) -> None:
        self.container["HostConfig"]["NetworkMode"] = "host"
        with self.assertRaisesRegex(ValueError, "network"):
            self.prove()

    def test_public_listener_fails(self) -> None:
        self.container["NetworkSettings"]["Ports"]["4222/tcp"][0]["HostIp"] = "0.0.0.0"
        with self.assertRaisesRegex(ValueError, "loopback"):
            self.prove()

    def test_public_monitor_fails(self) -> None:
        self.container["NetworkSettings"]["Ports"]["8222/tcp"][0]["HostIp"] = "0.0.0.0"
        with self.assertRaisesRegex(ValueError, "loopback"):
            self.prove()

    def test_extra_unexpected_mapping_fails(self) -> None:
        self.container["NetworkSettings"]["Ports"]["6222/tcp"] = [{"HostIp": "0.0.0.0", "HostPort": "6222"}]
        with self.assertRaisesRegex(ValueError, "extra"):
            self.prove()

    def test_wrong_broker_name_fails(self) -> None:
        self.server["server_name"] = "foreign-nats"
        with self.assertRaisesRegex(ValueError, "wrong broker"):
            self.prove()

    def test_monitor_and_jetstream_server_id_mismatch_fails(self) -> None:
        self.jetstream["server_id"] = "NFOREIGN"
        with self.assertRaisesRegex(ValueError, "wrong broker"):
            self.prove()

    def test_stale_stream_messages_fail(self) -> None:
        self.jetstream["total_messages"] = 1
        with self.assertRaisesRegex(ValueError, "prior messages"):
            self.prove()

    def test_consumer_pending_fails(self) -> None:
        self.jetstream["account_details"][0]["stream_detail"][0]["consumer_detail"][0]["num_pending"] = 1
        with self.assertRaisesRegex(ValueError, "pending deliveries"):
            self.prove()

    def test_ack_pending_fails(self) -> None:
        self.jetstream["account_details"][0]["stream_detail"][0]["consumer_detail"][0]["num_ack_pending"] = 1
        with self.assertRaisesRegex(ValueError, "acknowledgements"):
            self.prove()

    def test_missing_consumer_details_fail_closed(self) -> None:
        del self.jetstream["account_details"][0]["stream_detail"][0]["consumer_detail"]
        with self.assertRaisesRegex(ValueError, "do not cover all state"):
            self.prove()

    def test_missing_required_monitor_counter_fails(self) -> None:
        del self.jetstream["total_messages"]
        with self.assertRaisesRegex(ValueError, "missing"):
            self.prove()

    def test_zero_streams_and_consumers_are_valid(self) -> None:
        empty = copy.deepcopy(self.jetstream)
        empty.update({"total_streams": 0, "total_consumers": 0, "account_details": []})
        result = validate(
            self.container, self.server, empty,
            expected_container_id=ID, expected_image=IMAGE, expected_server_name=SERVER,
        )
        self.assertEqual(result["streams"], 0)


if __name__ == "__main__":
    unittest.main()
