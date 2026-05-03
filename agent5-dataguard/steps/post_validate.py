"""End-to-end validation: standby role, redo apply, gap, lag, and broker health."""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import paramiko

from .common import StepResult, StepStatus, render_template, run_dgmgrl, run_sqlplus_as_oracle

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def _grep_value(text: str, marker: str) -> str:
    m = re.search(rf"AGENT5_KV>>{re.escape(marker)}=(.*?)<<AGENT5_KV", text)
    return m.group(1).strip() if m else ""


def post_validate(
    pri: paramiko.SSHClient,
    stby: paramiko.SSHClient,
    *,
    db_name: str,
    primary_db_unique_name: str,
    standby_db_unique_name: str,
    primary_tns: str,
    sys_password: str,
) -> tuple[list[StepResult], dict[str, Any]]:
    out: list[StepResult] = []
    facts: dict[str, str] = {}

    # 1) Primary-side query
    t0 = time.monotonic()
    sql_pri = render_template(SQL_DIR / "03_post_validate_primary.sql",
                              STANDBY_UNIQUE_NAME=standby_db_unique_name)
    rc, sout, serr = run_sqlplus_as_oracle(pri, db_name, sql_pri, timeout=120)
    if rc != 0:
        out.append(StepResult("validate.primary_query", StepStatus.FAIL,
                              f"sqlplus on primary rc={rc}",
                              details={"stdout_tail": sout[-1000:], "stderr_tail": serr[-400:]},
                              elapsed_ms=int((time.monotonic() - t0) * 1000)))
        return out, facts
    out.append(StepResult("validate.primary_query", StepStatus.PASS, "primary validation queries ran",
                          elapsed_ms=int((time.monotonic() - t0) * 1000)))

    for k in ("PRI_ROLE", "PRI_OPEN_MODE", "PRI_PROTECTION_MODE", "PRI_PROTECTION_LEVEL",
              "PRI_DEST2_STATUS", "PRI_LAST_SEQUENCE", "PRI_GAP_COUNT"):
        facts[k.lower()] = _grep_value(sout, k)

    # 2) Standby-side query
    t0 = time.monotonic()
    sql_stby = render_template(SQL_DIR / "04_post_validate_standby.sql")
    rc, sout, serr = run_sqlplus_as_oracle(stby, db_name, sql_stby, timeout=120)
    if rc != 0:
        out.append(StepResult("validate.standby_query", StepStatus.FAIL,
                              f"sqlplus on standby rc={rc}",
                              details={"stdout_tail": sout[-1000:], "stderr_tail": serr[-400:]},
                              elapsed_ms=int((time.monotonic() - t0) * 1000)))
        return out, facts
    out.append(StepResult("validate.standby_query", StepStatus.PASS, "standby validation queries ran",
                          elapsed_ms=int((time.monotonic() - t0) * 1000)))

    for k in ("STBY_ROLE", "STBY_OPEN_MODE", "STBY_MRP_STATUS", "STBY_LAST_APPLIED_SEQ",
              "STBY_LAST_RECEIVED_SEQ", "STBY_DELAY_MINS", "STBY_WALLET_STATUS"):
        facts[k.lower()] = _grep_value(sout, k)

    # Per-fact assertions
    def assert_eq(check: str, key: str, expected: str | list[str]) -> None:
        actual = facts.get(key, "")
        ok = actual.upper() in ([e.upper() for e in expected] if isinstance(expected, list) else [expected.upper()])
        out.append(StepResult(
            f"validate.{check}",
            StepStatus.PASS if ok else StepStatus.FAIL,
            f"{key}={actual or '<missing>'} (expected {expected})",
            details={"actual": actual, "expected": expected},
        ))

    assert_eq("primary_role",   "pri_role",       "PRIMARY")
    assert_eq("standby_role",   "stby_role",      "PHYSICAL STANDBY")
    assert_eq("standby_open",   "stby_open_mode", ["MOUNTED", "READ ONLY WITH APPLY"])
    assert_eq("mrp_running",    "stby_mrp_status", ["APPLYING_LOG", "WAITING_FOR_LOG", "APPLYING_LOG_NOWAIT"])
    assert_eq("wallet_open",    "stby_wallet_status", ["OPEN", "OPEN_NO_MASTER_KEY"])

    try:
        gap = int(facts.get("pri_gap_count") or 0)
        out.append(StepResult(
            "validate.no_gap",
            StepStatus.PASS if gap == 0 else StepStatus.WARN,
            f"archive gap count = {gap}",
            details={"gap": gap},
        ))
    except ValueError:
        out.append(StepResult("validate.no_gap", StepStatus.WARN, "could not parse gap count"))

    # 3) Broker validate (catches subtle config drift)
    t0 = time.monotonic()
    rc, sout, _ = run_dgmgrl(
        pri, db_name,
        f"VALIDATE DATABASE VERBOSE '{standby_db_unique_name}';\n",
        sys_password=sys_password, connect_target=primary_tns, timeout=180,
    )
    ready_for_switch = "Ready for Switchover:  Yes" in sout or "Ready for Switchover : Yes" in sout
    out.append(StepResult(
        "validate.broker_validate",
        StepStatus.PASS if (rc == 0 and "Error" not in sout) else StepStatus.WARN,
        ("VALIDATE DATABASE clean" if (rc == 0 and "Error" not in sout)
         else f"VALIDATE DATABASE found issues (rc={rc})"),
        details={"stdout_tail": sout[-2000:], "ready_for_switchover": ready_for_switch},
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    ))
    facts["ready_for_switchover"] = "yes" if ready_for_switch else "no"

    return out, facts
