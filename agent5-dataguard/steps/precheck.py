"""Pre-flight checks on both primary and standby clusters."""
from __future__ import annotations

import shlex
import time

import paramiko

from .common import StepResult, StepStatus, run_remote


def _probe(cli: paramiko.SSHClient, name: str, cmd: str, expect: str | None) -> StepResult:
    t0 = time.monotonic()
    try:
        rc, sout, serr = run_remote(cli, cmd, timeout=60)
    except Exception as e:
        return StepResult(name, StepStatus.FAIL, f"exec failed: {e!r}",
                          elapsed_ms=int((time.monotonic() - t0) * 1000))
    if rc != 0:
        return StepResult(name, StepStatus.FAIL, f"rc={rc}",
                          details={"stderr_tail": serr[-300:], "stdout_tail": sout[-300:]},
                          elapsed_ms=int((time.monotonic() - t0) * 1000))
    if expect and expect.lower() not in sout.lower():
        return StepResult(name, StepStatus.WARN, f"rc=0 but '{expect}' not found",
                          details={"stdout_tail": sout[-400:]},
                          elapsed_ms=int((time.monotonic() - t0) * 1000))
    return StepResult(name, StepStatus.PASS, "ok",
                      details={"stdout_tail": sout.strip()[-200:]},
                      elapsed_ms=int((time.monotonic() - t0) * 1000))


def precheck_primary(cli: paramiko.SSHClient, *, db_name: str) -> list[StepResult]:
    return [
        _probe(cli, "pri.gi_running",
               "sudo -iu grid bash -lc 'crsctl check cluster -all'", "ONLINE"),
        _probe(cli, "pri.db_open",
               f"sudo -iu oracle bash -lc 'srvctl status database -d {shlex.quote(db_name)}'",
               "is running"),
        _probe(cli, "pri.dbgmgrl_present",
               "sudo -iu oracle bash -lc 'command -v dgmgrl'", "/dgmgrl"),
        _probe(cli, "pri.rman_present",
               "sudo -iu oracle bash -lc 'command -v rman'", "/rman"),
        _probe(cli, "pri.broker_param",
               f"sudo -iu oracle bash -lc 'echo \"select value from v\\$parameter where name=\\\"dg_broker_start\\\";\" "
               f"| sqlplus -s -L / as sysdba'", "TRUE"),
    ]


def precheck_standby(cli: paramiko.SSHClient, *, expected_db_unique_name: str) -> list[StepResult]:
    """Standby cluster must be deployed (Agent 2 ran for both clusters), GI up, ASM mounted, no DB yet."""
    out: list[StepResult] = []
    out.append(_probe(cli, "stby.gi_running",
                      "sudo -iu grid bash -lc 'crsctl check cluster -all'", "ONLINE"))
    out.append(_probe(cli, "stby.asm_data_dg",
                      "sudo -iu grid bash -lc 'asmcmd lsdg' | awk '{print $NF}'", "DATA"))
    out.append(_probe(cli, "stby.acfs_mount",
                      "df -hT | awk '$2==\"acfs\" {print; found=1} END {exit !found}'", None))
    out.append(_probe(cli, "stby.oracle_user",
                      "id oracle && id grid", None))
    # Make sure the standby DB does NOT already exist (to avoid clobbering)
    t0 = time.monotonic()
    rc, sout, _ = run_remote(
        cli,
        f"sudo -iu oracle bash -lc 'srvctl config database -d {shlex.quote(expected_db_unique_name)} 2>&1' || true",
        timeout=60,
    )
    if "PRCD-1120" in sout or "does not exist" in sout.lower() or rc != 0:
        out.append(StepResult("stby.db_absent", StepStatus.PASS,
                              f"no existing DB {expected_db_unique_name} on standby — clean slate",
                              elapsed_ms=int((time.monotonic() - t0) * 1000)))
    else:
        out.append(StepResult("stby.db_absent", StepStatus.WARN,
                              f"DB {expected_db_unique_name} already configured on standby",
                              details={"stdout_tail": sout[-400:]},
                              elapsed_ms=int((time.monotonic() - t0) * 1000)))
    return out
