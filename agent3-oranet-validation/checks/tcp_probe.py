"""TCP-level reachability probes (independent of Oracle Net)."""
from __future__ import annotations

import socket
from typing import Any

from .common import CheckResult, Severity, timed


@timed
def _probe(host: str, port: int, timeout: float = 5.0, label: str | None = None) -> CheckResult:
    name = f"tcp.{label or port}@{host}"
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return CheckResult(
            name, Severity.PASS, f"open {host}:{port}",
            target=f"{host}:{port}",
            details={"local": s.getsockname(), "peer": s.getpeername()},
        )
    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        return CheckResult(
            name, Severity.FAIL, f"unreachable {host}:{port} ({type(e).__name__}: {e})",
            target=f"{host}:{port}",
        )
    finally:
        try:
            s.close()
        except Exception:
            pass


def check_tcp_ports(payload: dict[str, Any]) -> list[CheckResult]:
    scan = payload["oracle_net"]["scan_dns_name"]
    tcp_port = int(payload["oracle_net"]["scan_listener_port_tcp"])
    tcps_port = int(payload["oracle_net"].get("scan_listener_port_tcp_ssl") or 2484)
    results = [
        _probe(scan, tcp_port, label=f"scan-tcp-{tcp_port}"),
        _probe(scan, tcps_port, label=f"scan-tcps-{tcps_port}"),
    ]

    # Per-node SSH probe — requires DNS to publish nodes; skip silently if it doesn't
    domain = (payload["vm_cluster"].get("domain") or "").strip(".")
    prefix = payload["vm_cluster"]["hostname_prefix"]
    node_count = payload.get("vm_cluster", {}).get("node_count") or 2
    for i in range(1, node_count + 1):
        host = f"{prefix}{i}" + (f".{domain}" if domain else "")
        try:
            socket.getaddrinfo(host, None)
        except socket.gaierror:
            continue
        results.append(_probe(host, 22, label="ssh"))
    return results
