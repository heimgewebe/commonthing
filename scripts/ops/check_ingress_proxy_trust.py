#!/usr/bin/env python3
"""Refuse a VPS deploy whose proxy trust does not name exactly Caddy.

The VPS stack gives Caddy one pinned address on the `ingress` network and
trusts only that address as a forwarding proxy, in the API
(`AUTH_TRUSTED_PROXIES`) and in Schaubild (`--trusted-proxy-source-cidr`).
Three values must therefore agree. If they drift apart, the API discards
Caddy's `X-Forwarded-For` and every client falls into one per-IP rate-limit
bucket, which is a site-wide login outage.

Input: the JSON from `docker compose ... config --format json` on stdin.

`--running-caddy-networks FILE` adds a second check for bounded deploys,
which recreate the API but not Caddy. FILE holds
`docker inspect -f '{{json .NetworkSettings.Networks}}'` of the running
Caddy container. If that Caddy does not hold the pinned ingress address yet,
a new API would trust a peer that does not exist. Run a full deploy first.

On success it prints the pinned Caddy address and exits 0. On a violation it
prints one `ERROR:` line per problem to stderr and exits 2.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from typing import Any

INGRESS = "ingress"
CIDR_FLAG = "--trusted-proxy-source-cidr"


def _network_names(service: dict[str, Any]) -> set[str]:
    # Compose renders a mapping; a list is accepted for hand-written input.
    return set(service.get("networks") or {})


def _single_ipam_config(
    cfg: dict[str, Any], errors: list[str]
) -> dict[str, Any] | None:
    network = (cfg.get("networks") or {}).get(INGRESS)
    if not isinstance(network, dict):
        errors.append("networks.ingress is missing")
        return None
    configs = (network.get("ipam") or {}).get("config") or []
    if len(configs) != 1 or not isinstance(configs[0], dict):
        errors.append("networks.ingress must declare exactly one IPAM config")
        return None
    return configs[0]


def check_rendered(cfg: dict[str, Any]) -> tuple[str | None, list[str]]:
    errors: list[str] = []
    services = cfg.get("services") or {}

    caddy = services.get("caddy")
    if not isinstance(caddy, dict):
        return None, ["services.caddy is missing"]
    if _network_names(caddy) != {INGRESS}:
        errors.append(
            "services.caddy must be attached to the ingress network only; "
            "on any shared network `api` may "
            "resolve to an address the API does not trust"
        )
    caddy_networks = caddy.get("networks")
    caddy_network = (
        caddy_networks.get(INGRESS) if isinstance(caddy_networks, dict) else None
    )
    raw_ip = (caddy_network or {}).get("ipv4_address")
    if not raw_ip:
        errors.append(
            "services.caddy needs a pinned ipv4_address on the ingress network"
        )
        return None, errors
    try:
        caddy_ip = ipaddress.IPv4Address(raw_ip)
    except ValueError:
        return None, errors + [
            "services.caddy ipv4_address is not a valid IPv4 address"
        ]

    ipam = _single_ipam_config(cfg, errors)
    if ipam is not None:
        try:
            subnet = ipaddress.IPv4Network(ipam.get("subnet", ""), strict=True)
            dynamic = ipaddress.IPv4Network(ipam.get("ip_range", ""), strict=True)
            gateway = ipaddress.IPv4Address(ipam.get("gateway", ""))
        except ValueError:
            errors.append("networks.ingress IPAM subnet/ip_range/gateway is invalid")
        else:
            if caddy_ip not in subnet or caddy_ip in (
                subnet.network_address,
                subnet.broadcast_address,
            ):
                errors.append(
                    "Caddy address must be a usable host in the ingress subnet"
                )
            if not dynamic.subnet_of(subnet):
                errors.append("ingress ip_range must be inside the ingress subnet")
            if caddy_ip in dynamic:
                errors.append(
                    "Caddy address lies inside the dynamic ip_range; "
                    "another container could take it first"
                )
            if caddy_ip == gateway:
                errors.append("Caddy address equals the ingress gateway")

    expected = f"{caddy_ip}/32"

    api = services.get("api")
    if not isinstance(api, dict):
        errors.append("services.api is missing")
    else:
        if not {INGRESS, "default"} <= _network_names(api):
            errors.append("services.api must be attached to both default and ingress")
        trusted = str((api.get("environment") or {}).get("AUTH_TRUSTED_PROXIES", ""))
        if trusted != expected:
            errors.append(
                "api AUTH_TRUSTED_PROXIES must equal pinned Caddy ingress IPv4 /32 "
                "(an override in the env file or shell must follow WELTGEWEBE_INGRESS_CADDY_IPV4)"
            )

    schaubild = services.get("schaubild")
    if isinstance(schaubild, dict):
        if _network_names(schaubild) != {INGRESS}:
            errors.append(
                "services.schaubild must be attached to the ingress network only"
            )
        command = [str(item) for item in schaubild.get("command") or []]
        try:
            cidr = command[command.index(CIDR_FLAG) + 1]
        except (ValueError, IndexError):
            errors.append(f"services.schaubild command lacks {CIDR_FLAG}")
        else:
            if cidr != expected:
                errors.append(
                    f"schaubild {CIDR_FLAG} must equal pinned Caddy ingress IPv4 /32 "
                    "(SCHAUWERK_SCHAUBILD_TRUSTED_PROXY_CIDR must follow WELTGEWEBE_INGRESS_CADDY_IPV4)"
                )

    return str(caddy_ip), errors


def check_running_caddy(
    cfg: dict[str, Any], caddy_ip: str, running_networks: dict[str, Any]
) -> list[str]:
    network_name = ((cfg.get("networks") or {}).get(INGRESS) or {}).get("name", INGRESS)
    attachment = running_networks.get(network_name)
    actual = (
        (attachment or {}).get("IPAddress") if isinstance(attachment, dict) else None
    )
    if actual != caddy_ip:
        return [
            "running Caddy lacks the pinned ingress address; "
            "a bounded deploy would start an API that trusts no live proxy. Run a full deploy first."
        ]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--running-caddy-networks", metavar="FILE")
    args = parser.parse_args(argv)

    try:
        cfg = json.load(sys.stdin)
    except json.JSONDecodeError:
        print("ERROR: rendered compose config is not valid JSON", file=sys.stderr)
        return 2

    caddy_ip, errors = check_rendered(cfg)
    if caddy_ip is not None and not errors and args.running_caddy_networks:
        try:
            with open(args.running_caddy_networks, encoding="utf-8") as handle:
                running = json.load(handle)
        except (OSError, json.JSONDecodeError):
            errors.append("cannot read running Caddy network attachment data")
        else:
            errors.extend(
                check_running_caddy(
                    cfg, caddy_ip, running if isinstance(running, dict) else {}
                )
            )

    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    if errors or caddy_ip is None:
        return 2
    print(caddy_ip)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
