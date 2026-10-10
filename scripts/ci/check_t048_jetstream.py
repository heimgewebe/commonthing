"""Fail-closed identity and empty-JetStream proof for the T048 CI broker.

The pinned NATS image runs on Docker bridge with ports published to host loopback
only. The HTTP monitoring interface is never exposed on a public host address.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Mapping
from urllib.request import urlopen


def _count(obj: Mapping, key: str) -> int:
    value = obj.get(key)
    if type(value) is not int or value < 0:
        raise ValueError(f"JetStream {key} is missing or invalid")
    return value


def _aggregate_count(obj: Mapping, current: str, legacy: str) -> int:
    """Support both documented /jsz generations, rejecting missing or conflicting counts."""
    if current not in obj and legacy not in obj:
        raise ValueError(f"JetStream {current} is missing")
    values = [_count(obj, key) for key in (current, legacy) if key in obj]
    if len(values) == 2 and values[0] != values[1]:
        raise ValueError(f"JetStream {current} counters disagree")
    return values[0]


def validate(
    container: Mapping,
    server: Mapping,
    jetstream: Mapping,
    *,
    expected_container_id: str,
    expected_image: str,
    expected_server_name: str,
) -> dict[str, str | int]:
    """Reject any foreign container, exposed port or incomplete broker state."""
    if (
        container.get("Id") != expected_container_id
        or container.get("Config", {}).get("Image") != expected_image
        or container.get("State", {}).get("Running") is not True
        or container.get("HostConfig", {}).get("NetworkMode") != "bridge"
    ):
        raise ValueError("T048 NATS container identity, image, network or liveness mismatch")

    ports = container.get("NetworkSettings", {}).get("Ports")
    if not isinstance(ports, dict):
        raise ValueError("T048 NATS Docker port mappings are missing")
    for key, port in (("4222/tcp", "4222"), ("8222/tcp", "8222")):
        if ports.get(key) != [{"HostIp": "127.0.0.1", "HostPort": port}]:
            raise ValueError(f"T048 NATS {key} is not exclusively host-loopback published")
    for mappings in ports.values():
        if mappings is not None and any(
            not isinstance(mapping, dict) or mapping.get("HostIp") != "127.0.0.1"
            for mapping in mappings
        ):
            raise ValueError("T048 NATS has an extra non-loopback published listener")

    server_id = server.get("server_id")
    if (
        server.get("server_name") != expected_server_name
        or not isinstance(server_id, str)
        or not server_id
        or jetstream.get("server_id") != server_id
    ):
        raise ValueError("T048 NATS monitoring endpoint belongs to the wrong broker")

    streams = _aggregate_count(jetstream, "streams", "total_streams")
    consumers = _aggregate_count(jetstream, "consumers", "total_consumers")
    messages = _aggregate_count(jetstream, "messages", "total_messages")
    if messages != 0:
        raise ValueError(f"T048 JetStream contains {messages} prior messages")

    accounts = jetstream.get("account_details", [])
    if not isinstance(accounts, list):
        raise ValueError("T048 JetStream account details are incomplete")
    stream_count = 0
    consumer_count = 0
    for account in accounts:
        if not isinstance(account, dict):
            raise ValueError("T048 JetStream account has unexpected shape")
        details = account.get("stream_detail", [])
        if not isinstance(details, list):
            raise ValueError("T048 JetStream stream details are incomplete")
        for stream in details:
            if not isinstance(stream, dict):
                raise ValueError("T048 JetStream stream has unexpected shape")
            stream_count += 1
            consumer_details = stream.get("consumer_detail", [])
            if not isinstance(consumer_details, list):
                raise ValueError("T048 JetStream consumer details are incomplete")
            for consumer in consumer_details:
                if not isinstance(consumer, dict):
                    raise ValueError("T048 JetStream consumer has unexpected shape")
                consumer_count += 1
                if _count(consumer, "num_pending") or _count(consumer, "num_ack_pending"):
                    raise ValueError("T048 JetStream consumer has pending deliveries or acknowledgements")
    if stream_count != streams or consumer_count != consumers:
        raise ValueError("T048 JetStream stream/consumer details do not cover all state")
    return {"server_id": server_id, "streams": streams, "consumers": consumers, "messages": messages}


def _http_json(path: str) -> dict:
    with urlopen(f"http://127.0.0.1:8222/{path}", timeout=3) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError(f"T048 NATS {path} did not return a JSON object")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True)
    parser.add_argument("--container-id", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--server-name", required=True)
    args = parser.parse_args()
    try:
        output = subprocess.run(
            ["docker", "container", "inspect", args.container],
            check=True, capture_output=True, text=True, timeout=6,
        )
        details = json.loads(output.stdout)
        if not isinstance(details, list) or len(details) != 1:
            raise ValueError("T048 expected exactly one Docker container")
        evidence = validate(
            details[0],
            _http_json("varz"),
            _http_json("jsz?accounts=true&streams=true&consumers=true"),
            expected_container_id=args.container_id,
            expected_image=args.image,
            expected_server_name=args.server_name,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"T048 isolated JetStream proof failed: {exc}", file=sys.stderr)
        return 2
    print("T048 isolated JetStream proof: PASS " + json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())