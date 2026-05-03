"""SSH reachability + lightweight OS probe per node."""
from __future__ import annotations

import io
import os
import socket
from pathlib import Path
from typing import Any

import paramiko

from .common import CheckResult, Severity, timed


def _load_pkey(path: str | None) -> paramiko.PKey | None:
    if not path or not os.path.exists(path):
        return None
    text = Path(path).read_text()
    for cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            return cls.from_private_key(io.StringIO(text))
        except paramiko.SSHException:
            continue
    return None


def ssh_connect(host: str, user: str, key_path: str | None, timeout: float = 10.0) -> paramiko.SSHClient:
    pkey = _load_pkey(key_path)
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(
        hostname=host,
        username=user,
        pkey=pkey,
        timeout=timeout,
        banner_timeout=timeout,
        auth_timeout=timeout,
        look_for_keys=pkey is None,
        allow_agent=pkey is None,
    )
    return cli


def ssh_run(cli: paramiko.SSHClient, cmd: str, timeout: float = 30.0) -> tuple[int, str, str]:
    stdin, stdout, stderr = cli.exec_command(cmd, timeout=timeout)
    rc = stdout.channel.recv_exit_status()
    return rc, stdout.read().decode("utf-8", "replace"), stderr.read().decode("utf-8", "replace")


@timed
def _check_node(host: str, user: str, key_path: str | None) -> CheckResult:
    name = f"ssh.{host}"
    try:
        cli = ssh_connect(host, user, key_path, timeout=10)
    except (paramiko.SSHException, socket.error, OSError) as e:
        return CheckResult(name, Severity.FAIL, f"ssh connect failed: {type(e).__name__}: {e}", target=host)
    try:
        rc, out, err = ssh_run(cli, "hostname; uname -r; cat /etc/oracle-release 2>/dev/null || true", timeout=10)
        if rc != 0:
            return CheckResult(name, Severity.WARN, f"ssh ok but probe rc={rc}", {"stderr": err.strip()[:240]}, target=host)
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        return CheckResult(
            name, Severity.PASS, f"ssh ok ({lines[0] if lines else host})",
            details={"hostname": lines[0] if lines else "", "kernel": lines[1] if len(lines) > 1 else "", "release": lines[2] if len(lines) > 2 else ""},
            target=host,
        )
    finally:
        cli.close()


def check_ssh_reachability(payload: dict[str, Any]) -> list[CheckResult]:
    user = payload["ssh"]["os_user"]
    key = payload["ssh"].get("private_key_path")
    domain = (payload["vm_cluster"].get("domain") or "").strip(".")
    prefix = payload["vm_cluster"]["hostname_prefix"]
    node_count = payload.get("vm_cluster", {}).get("node_count") or 2
    out: list[CheckResult] = []
    for i in range(1, node_count + 1):
        host = f"{prefix}{i}" + (f".{domain}" if domain else "")
        try:
            socket.getaddrinfo(host, None)
        except socket.gaierror:
            out.append(CheckResult(f"ssh.{host}", Severity.SKIP, "host did not resolve", target=host))
            continue
        out.append(_check_node(host, user, key))
    return out
