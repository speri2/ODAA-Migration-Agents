"""
Run RMAN DUPLICATE FROM ACTIVE DATABASE FOR STANDBY.

Connects to the *primary* node1 (since RMAN target = primary), uses the standby's TNS alias
as the auxiliary connection. Both connections use sys/<password>@<service>; the standby static
listener entry from prepare_standby.py is what makes the auxiliary connect work while the
standby is in NOMOUNT.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import paramiko

from .common import StepResult, StepStatus, render_template, run_rman, run_remote

log = logging.getLogger("agent5")
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "sql"


def rman_duplicate_for_standby(
    pri: paramiko.SSHClient,
    *,
    db_name: str,
    primary_db_unique_name: str,
    standby_db_unique_name: str,
    primary_tns: str,
    standby_tns: str,
    sys_password: str,
    parallelism: int = 8,
    timeout_s: int = 14400,
) -> StepResult:
    t0 = time.monotonic()
    name = "rman.duplicate"

    rman_script = render_template(
        TEMPLATES_DIR.parent / "sql" / "02_rman_duplicate.rman",
        DB_NAME=db_name,
        PRIMARY_UNIQUE_NAME=primary_db_unique_name,
        STANDBY_UNIQUE_NAME=standby_db_unique_name,
        PARALLELISM=parallelism,
    )

    log.info("Starting RMAN DUPLICATE — this can run for hours on large databases (timeout=%ds)", timeout_s)
    rc, sout, serr = run_rman(
        pri,
        db_name,
        rman_script,
        target_connect=f"sys@{primary_tns}",
        auxiliary_connect=f"sys@{standby_tns}",
        sys_password=sys_password,
        timeout=timeout_s,
    )
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    # RMAN exits 0 even on some recoverable issues — check for explicit error markers.
    error_markers = ("RMAN-", "ORA-", "ERROR")
    has_error = any(
        line.lstrip().startswith(error_markers)
        and "RMAN-08" not in line  # informational
        for line in sout.splitlines()
    )
    if rc != 0 or has_error:
        # Capture more of the tail for diagnostics
        return StepResult(
            name, StepStatus.FAIL, f"RMAN DUPLICATE failed (rc={rc}, errors_in_output={has_error})",
            details={"stdout_tail": sout[-3000:], "stderr_tail": serr[-800:]},
            elapsed_ms=elapsed_ms,
        )

    return StepResult(
        name, StepStatus.PASS,
        f"RMAN DUPLICATE FROM ACTIVE DATABASE completed for {standby_db_unique_name}",
        details={"stdout_tail": sout[-1500:], "parallelism": parallelism},
        elapsed_ms=elapsed_ms,
    )


def register_standby_with_srvctl(
    stby: paramiko.SSHClient,
    *,
    db_name: str,
    standby_db_unique_name: str,
    oracle_home_version: str,
    instance_count: int = 2,
) -> StepResult:
    """
    After RMAN duplicate the standby exists at the OS level — register with Clusterware so it survives reboots
    and so srvctl/dgmgrl can drive it.
    """
    t0 = time.monotonic()
    name = "stby.srvctl_register"

    cmd = (
        "sudo -iu oracle bash -lc '"
        f"export ORAENV_ASK=NO; export ORACLE_SID={db_name}1; "
        ". oraenv >/dev/null 2>&1; "
        f"if srvctl config database -d {standby_db_unique_name} >/dev/null 2>&1; then "
        "  echo \"already registered\"; "
        "else "
        f"  srvctl add database -d {standby_db_unique_name} "
        f"    -dbname {db_name} "
        f"    -oraclehome $ORACLE_HOME "
        f"    -spfile +DATA/{standby_db_unique_name.upper()}/PARAMETERFILE/spfile{db_name.lower()}.ora "
        f"    -pwfile +DATA/{standby_db_unique_name.upper()}/PASSWORD/orapw{db_name.lower()} "
        f"    -role PHYSICAL_STANDBY "
        f"    -startoption MOUNT "
        f"    -dbtype RAC; "
        f"  for i in $(seq 1 {instance_count}); do "
        f"    srvctl add instance -d {standby_db_unique_name} -i {db_name}$i -node $(srvctl config nodeapps | awk \"/^Node $i/ {{print \\$3}}\") || true; "
        f"  done; "
        "fi; "
        f"srvctl config database -d {standby_db_unique_name}'"
    )
    rc, sout, serr = run_remote(stby, cmd, timeout=180)
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    if rc != 0:
        return StepResult(name, StepStatus.WARN,
                          "srvctl add/config returned non-zero — review and complete manually",
                          details={"stdout_tail": sout[-800:], "stderr_tail": serr[-400:]},
                          elapsed_ms=elapsed_ms)
    return StepResult(name, StepStatus.PASS,
                      f"standby {standby_db_unique_name} registered with Clusterware",
                      details={"stdout_tail": sout[-600:]}, elapsed_ms=elapsed_ms)
