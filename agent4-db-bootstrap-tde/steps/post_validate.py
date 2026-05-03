"""End-to-end validation queries — drives the agent5_input.json contract."""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import paramiko

from .common import StepResult, StepStatus, render_sql, run_sqlplus_as_oracle

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def _grep_value(text: str, marker: str) -> str:
    """Find 'AGENT4_KV>>key=value<<AGENT4_KV' tokens emitted by validation SQL."""
    m = re.search(rf"AGENT4_KV>>{re.escape(marker)}=(.*?)<<AGENT4_KV", text)
    return m.group(1).strip() if m else ""


def post_validate(
    cli: paramiko.SSHClient,
    *,
    db_name: str,
    pdb_name: str,
) -> tuple[list[StepResult], dict[str, Any]]:
    out: list[StepResult] = []
    facts: dict[str, Any] = {}

    t0 = time.monotonic()
    sql = render_sql(SQL_DIR / "07_post_validation.sql", PDB_NAME=pdb_name)
    rc, sout, serr = run_sqlplus_as_oracle(cli, db_name, sql, timeout=180)
    elapsed = int((time.monotonic() - t0) * 1000)

    if rc != 0:
        out.append(StepResult("post_validate.run", StepStatus.FAIL, f"sqlplus rc={rc}",
                              details={"stderr_tail": serr[-600:], "stdout_tail": sout[-1500:]},
                              elapsed_ms=elapsed))
        return out, facts

    out.append(StepResult("post_validate.run", StepStatus.PASS, "validation queries ran",
                          details={"stdout_chars": len(sout)}, elapsed_ms=elapsed))

    # Pull structured facts
    for marker in (
        "DB_NAME", "DB_UNIQUE_NAME", "DB_VERSION", "OPEN_MODE",
        "ARCHIVELOG", "FORCE_LOGGING", "FLASHBACK_ON",
        "WALLET_STATUS", "WALLET_TYPE", "WALLET_LOCATION",
        "PDB_OPEN_MODE", "PDB_RESTRICTED",
        "SRL_COUNT", "SRL_GROUP_SIZES",
        "PWFILE_LOCATION", "INSTANCE_COUNT", "ENC_TBS_COUNT",
        "MASTER_KEY_ID",
    ):
        facts[marker.lower()] = _grep_value(sout, marker)

    # Per-fact assertions (fail-loud on the things Data Guard absolutely needs)
    def assert_eq(check: str, key: str, expected: str | list[str]) -> None:
        actual = facts.get(key, "")
        ok = actual.upper() in ([e.upper() for e in expected] if isinstance(expected, list) else [expected.upper()])
        out.append(StepResult(
            f"post_validate.{check}",
            StepStatus.PASS if ok else StepStatus.FAIL,
            f"{key}={actual or '<missing>'} (expected {expected})",
            details={"actual": actual, "expected": expected},
        ))

    assert_eq("archivelog",     "archivelog",     "ARCHIVELOG")
    assert_eq("force_logging",  "force_logging",  "YES")
    assert_eq("wallet_status",  "wallet_status",  ["OPEN", "OPEN_NO_MASTER_KEY", "OPEN_UNKNOWN_MASTER_KEY_STATUS"])
    assert_eq("pdb_open",       "pdb_open_mode",  "READ WRITE")

    try:
        srl = int(facts.get("srl_count") or 0)
        out.append(StepResult(
            "post_validate.srl_count",
            StepStatus.PASS if srl >= 4 else StepStatus.FAIL,
            f"standby redo logs count={srl} (>=4 required)",
            details={"actual": srl},
        ))
    except ValueError:
        out.append(StepResult("post_validate.srl_count", StepStatus.FAIL, "could not parse SRL_COUNT"))

    return out, facts
