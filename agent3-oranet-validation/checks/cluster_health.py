"""GI / CRS / SCAN listener / ASM health, executed over SSH on node1."""
from __future__ import annotations

import socket
from typing import Any

import paramiko

from .common import CheckResult, Severity, timed
from .ssh_check import ssh_connect, ssh_run


# Commands to run as the grid user on node1.
# We use 'sudo -iu grid' so they pick up GRID_HOME / PATH.
GI_PROBES: list[tuple[str, str, str]] = [
    # name, command, expected substring (case-insensitive) — substring=None means rc==0 is enough
    ("crs.cluster",  "sudo -iu grid bash -lc 'crsctl check cluster -all'",        "online"),
    ("crs.scan",     "sudo -iu grid bash -lc 'srvctl status scan'",                "is running"),
    ("crs.listener", "sudo -iu grid bash -lc 'srvctl status scan_listener'",       "is running"),
    ("crs.asm",      "sudo -iu grid bash -lc 'srvctl status asm -detail'",         "is running"),
    ("crs.diskgroups", "sudo -iu grid bash -lc 'asmcmd lsdg'",                     "data"),
    ("crs.nodeapps", "sudo -iu grid bash -lc 'srvctl status nodeapps'",            "is running"),
    ("os.chrony",    "chronyc tracking || timedatectl show -p NTPSynchronized",    None),
]


def _eval(rc: int, out: str, err: str, expect: str | None) -> tuple[Severity, str]:
    if rc != 0:
        return Severity.FAIL, f"rc={rc}: {(err or out).strip().splitlines()[0:1]}"
    if expect is None:
        return Severity.PASS, "rc=0"
    if expect.lower() in out.lower():
        return Severity.PASS, f"matched '{expect}'"
    return Severity.WARN, f"rc=0 but no '{expect}' in output"


@timed
def _run_probe(cli: paramiko.SSHClient, name: str, cmd: str, expect: str | None) -> CheckResult:
    try:
        rc, out, err = ssh_run(cli, cmd, timeout=60)
    except (paramiko.SSHException, socket.timeout, OSError) as e:
        return CheckResult(name, Severity.FAIL, f"exec failed: {type(e).__name__}: {e}")
    sev, summary = _eval(rc, out, err, expect)
    return CheckResult(
        name, sev, summary,
        details={
            "cmd": cmd,
            "rc": rc,
            "stdout_tail": "\n".join(out.splitlines()[-12:]),
            "stderr_tail": "\n".join(err.splitlines()[-6:]),
        },
    )


def check_cluster_health(payload: dict[str, Any]) -> list[CheckResult]:
    user = payload["ssh"]["os_user"]
    key = payload["ssh"].get("private_key_path")
    domain = (payload["vm_cluster"].get("domain") or "").strip(".")
    prefix = payload["vm_cluster"]["hostname_prefix"]
    node1 = f"{prefix}1" + (f".{domain}" if domain else "")
    try:
        socket.getaddrinfo(node1, None)
    except socket.gaierror:
        return [CheckResult("cluster_health", Severity.SKIP, f"node1 ({node1}) did not resolve")]

    try:
        cli = ssh_connect(node1, user, key, timeout=15)
    except (paramiko.SSHException, OSError) as e:
        return [CheckResult("cluster_health", Severity.FAIL, f"ssh to node1 failed: {e}", target=node1)]

    results: list[CheckResult] = []
    try:
        for name, cmd, expect in GI_PROBES:
            results.append(_run_probe(cli, name, cmd, expect))
    finally:
        cli.close()
    for r in results:
        r.target = node1
    return results
