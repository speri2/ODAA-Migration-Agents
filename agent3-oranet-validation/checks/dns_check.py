"""Resolve SCAN DNS and per-node hostnames; SCAN must yield 3 A records."""
from __future__ import annotations

import socket
from typing import Any

import dns.resolver
import dns.exception

from .common import CheckResult, Severity, timed


@timed
def _resolve(host: str, expect_min: int = 1) -> CheckResult:
    name = f"dns.{host}"
    try:
        # Use system resolver first; fall back to dnspython for richer errors
        ips = sorted({a[4][0] for a in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)})
    except socket.gaierror:
        try:
            answer = dns.resolver.resolve(host, "A", lifetime=5)
            ips = sorted({r.address for r in answer})
        except dns.exception.DNSException as e:
            return CheckResult(name, Severity.FAIL, f"resolution failed: {e}", target=host)
    if len(ips) < expect_min:
        return CheckResult(
            name, Severity.WARN,
            f"resolved {len(ips)} IP(s), expected >= {expect_min}",
            details={"ips": ips}, target=host,
        )
    return CheckResult(name, Severity.PASS, f"resolved {len(ips)} IP(s)", {"ips": ips}, target=host)


def check_dns(payload: dict[str, Any]) -> list[CheckResult]:
    results: list[CheckResult] = []
    scan = payload["oracle_net"]["scan_dns_name"]
    # SCAN must resolve to >= 3 IPs in a healthy RAC
    results.append(_resolve(scan, expect_min=3))

    # Per-node hostnames (best effort — depends on DNS publishing)
    domain = (payload["vm_cluster"].get("domain") or "").strip(".")
    prefix = payload["vm_cluster"]["hostname_prefix"]
    node_count = payload.get("vm_cluster", {}).get("node_count") or 2
    for i in range(1, node_count + 1):
        host = f"{prefix}{i}"
        if domain:
            host = f"{host}.{domain}"
        results.append(_resolve(host, expect_min=1))
    return results
