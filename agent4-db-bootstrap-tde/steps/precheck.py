"""Verify node1 is healthy and dbaascli is available before any DB work."""
from __future__ import annotations

import time

import paramiko

from .common import StepResult, StepStatus, run_remote


PROBES: list[tuple[str, str, str | None]] = [
    ("precheck.dbaascli_present", "command -v dbaascli || sudo -iu oracle bash -lc 'command -v dbaascli'", None),
    ("precheck.gi_running",       "sudo -iu grid bash -lc 'crsctl check cluster -all'", "ONLINE"),
    ("precheck.asm_data_dg",      "sudo -iu grid bash -lc 'asmcmd lsdg' | awk '{print $NF}'", "DATA"),
    ("precheck.acfs_mount",       "df -hT | awk '$2==\"acfs\" {print; found=1} END {exit !found}'", None),
    ("precheck.free_data_tb",     "sudo -iu grid bash -lc 'asmcmd lsdg DATA' | awk 'NR==2 {print $9}'", None),
    ("precheck.oracle_user",      "id oracle && id grid", None),
]


def precheck(cli: paramiko.SSHClient) -> list[StepResult]:
    out: list[StepResult] = []
    for name, cmd, expect in PROBES:
        t0 = time.monotonic()
        try:
            rc, sout, serr = run_remote(cli, cmd, timeout=60)
        except Exception as e:
            out.append(StepResult(name, StepStatus.FAIL, f"exec failed: {e!r}",
                                  elapsed_ms=int((time.monotonic() - t0) * 1000)))
            continue
        if rc != 0:
            out.append(StepResult(name, StepStatus.FAIL, f"rc={rc}",
                                  details={"stdout": sout[-400:], "stderr": serr[-400:]},
                                  elapsed_ms=int((time.monotonic() - t0) * 1000)))
            continue
        if expect and expect.lower() not in sout.lower():
            out.append(StepResult(name, StepStatus.WARN, f"rc=0 but '{expect}' missing",
                                  details={"stdout_tail": sout[-400:]},
                                  elapsed_ms=int((time.monotonic() - t0) * 1000)))
            continue
        out.append(StepResult(name, StepStatus.PASS, "ok",
                              details={"stdout_tail": sout.strip()[-200:]},
                              elapsed_ms=int((time.monotonic() - t0) * 1000)))
    return out
