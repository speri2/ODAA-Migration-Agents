"""Make the primary DB Data-Guard ready: FORCE LOGGING, init params, standby redo logs, FRA, password file."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import paramiko

from .common import StepResult, StepStatus, render_sql, run_sqlplus_as_oracle, run_remote

log = logging.getLogger("agent4")
SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def _exec_sql(cli: paramiko.SSHClient, db_name: str, file_name: str, **bindings) -> tuple[int, str, str]:
    sql = render_sql(SQL_DIR / file_name, **bindings)
    return run_sqlplus_as_oracle(cli, db_name, sql, timeout=300)


def configure_dataguard_prep(
    cli: paramiko.SSHClient,
    *,
    db_name: str,
    db_unique_name: str,
    standby_unique_name: str,
    standby_count: int = 4,
    srl_size_mb: int = 1024,
) -> list[StepResult]:
    out: list[StepResult] = []

    stages: list[tuple[str, str, dict[str, Any]]] = [
        ("dg.force_logging",
         "01_force_logging.sql", {}),
        ("dg.archivelog_check",
         "02_archivelog_check.sql", {}),
        ("dg.standby_redo_logs",
         "03_standby_redo_logs.sql",
         {"SRL_COUNT": standby_count, "SRL_SIZE_MB": srl_size_mb}),
        ("dg.init_params",
         "04_dataguard_params.sql",
         {"DB_UNIQUE_NAME": db_unique_name,
          "STANDBY_UNIQUE_NAME": standby_unique_name,
          "DB_NAME": db_name}),
        ("dg.password_file_sync",
         "05_password_file_sync.sql", {}),
    ]

    for name, fname, bindings in stages:
        t0 = time.monotonic()
        try:
            rc, sout, serr = _exec_sql(cli, db_name, fname, **bindings)
        except Exception as e:
            out.append(StepResult(name, StepStatus.FAIL, f"exec failed: {e!r}",
                                  elapsed_ms=int((time.monotonic() - t0) * 1000)))
            continue
        # SQL*Plus exits with the result of WHENEVER SQLERROR EXIT FAILURE
        out.append(StepResult(
            name,
            StepStatus.PASS if rc == 0 else StepStatus.FAIL,
            "ok" if rc == 0 else f"sqlplus rc={rc}",
            details={"stdout_tail": sout[-1600:], "stderr_tail": serr[-400:]},
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        ))

    # Sync the password file across both nodes (RAC) using ASM-located orapwd
    t0 = time.monotonic()
    rc, sout, serr = run_remote(
        cli,
        "sudo -iu oracle bash -lc '"
        f"srvctl modify database -d {db_unique_name} -pwfile +DATA/{db_unique_name.upper()}/PASSWORD/orapw{db_name.lower()} 2>/dev/null || true; "
        f"srvctl config database -d {db_unique_name} | grep -i passwordfile'",
        timeout=60,
    )
    out.append(StepResult(
        "dg.password_file_registered",
        StepStatus.PASS if rc == 0 else StepStatus.WARN,
        "registered shared password file" if rc == 0 else "could not register pwfile (may be set already)",
        details={"stdout_tail": sout[-600:], "stderr_tail": serr[-200:]},
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    ))

    return out
