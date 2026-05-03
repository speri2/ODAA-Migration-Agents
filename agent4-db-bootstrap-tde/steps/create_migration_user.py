"""Create the migration user inside the PDB with the privileges needed by RMAN/Data Pump."""
from __future__ import annotations

import time
from pathlib import Path

import paramiko

from .common import StepResult, StepStatus, render_sql, run_sqlplus_as_oracle
from .secrets import SecretBundle

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def create_migration_user(
    cli: paramiko.SSHClient,
    *,
    db_name: str,
    pdb_name: str,
    migration_user: str,
    secrets: SecretBundle,
) -> StepResult:
    t0 = time.monotonic()
    sql = render_sql(
        SQL_DIR / "06_migration_user.sql",
        PDB_NAME=pdb_name,
        MIG_USER=migration_user,
        MIG_PASSWORD=secrets.sys_password,  # reuse sys-tier secret; rotate post-migration
    )
    rc, sout, serr = run_sqlplus_as_oracle(cli, db_name, sql, timeout=120)
    return StepResult(
        "migration_user",
        StepStatus.PASS if rc == 0 else StepStatus.FAIL,
        f"migration user '{migration_user}' provisioned" if rc == 0 else f"sqlplus rc={rc}",
        details={"stdout_tail": sout[-1200:], "stderr_tail": serr[-400:]},
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )
