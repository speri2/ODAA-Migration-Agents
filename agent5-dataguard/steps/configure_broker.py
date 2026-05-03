"""Set up Data Guard Broker configuration via dgmgrl, idempotently."""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any

import paramiko

from .common import StepResult, StepStatus, render_template, run_dgmgrl

log = logging.getLogger("agent5")
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "dgmgrl"


def _show_configuration(
    pri: paramiko.SSHClient,
    *,
    db_name: str,
    primary_tns: str,
    sys_password: str,
) -> tuple[bool, str]:
    rc, sout, _ = run_dgmgrl(
        pri,
        db_name,
        "SHOW CONFIGURATION;",
        sys_password=sys_password,
        connect_target=primary_tns,
        timeout=120,
    )
    if rc != 0:
        return False, sout
    # ORA-16532 = configuration does not exist
    return ("ORA-16532" not in sout and "Configuration -" in sout), sout


def configure_broker(
    pri: paramiko.SSHClient,
    *,
    db_name: str,
    primary_db_unique_name: str,
    standby_db_unique_name: str,
    primary_tns: str,
    standby_tns: str,
    sys_password: str,
    protection_mode: str = "MAX_PERFORMANCE",
    transport_mode: str = "ASYNC",
    apply_mode: str = "REAL_TIME_APPLY",
) -> list[StepResult]:
    out: list[StepResult] = []

    exists, show_out = _show_configuration(pri, db_name=db_name, primary_tns=primary_tns, sys_password=sys_password)
    if exists:
        out.append(StepResult("broker.exists", StepStatus.PASS,
                              "Data Guard Broker configuration already present",
                              details={"show_tail": show_out[-1200:]}))
    else:
        # Create configuration + add standby
        t0 = time.monotonic()
        script = render_template(
            TEMPLATES_DIR / "01_create_configuration.dgmgrl",
            CFG_NAME=f"{db_name}_DG",
            PRIMARY_UNIQUE_NAME=primary_db_unique_name,
            STANDBY_UNIQUE_NAME=standby_db_unique_name,
            PRIMARY_TNS=primary_tns,
            STANDBY_TNS=standby_tns,
        )
        rc, sout, serr = run_dgmgrl(
            pri, db_name, script,
            sys_password=sys_password, connect_target=primary_tns, timeout=300,
        )
        if rc != 0 or "Error" in sout or "ORA-" in sout:
            out.append(StepResult("broker.create", StepStatus.FAIL,
                                  f"CREATE CONFIGURATION failed (rc={rc})",
                                  details={"stdout_tail": sout[-1500:], "stderr_tail": serr[-400:]},
                                  elapsed_ms=int((time.monotonic() - t0) * 1000)))
            return out
        out.append(StepResult("broker.create", StepStatus.PASS,
                              "Data Guard Broker configuration created",
                              details={"stdout_tail": sout[-1200:]},
                              elapsed_ms=int((time.monotonic() - t0) * 1000)))

    # Set protection / transport / apply modes
    t0 = time.monotonic()
    script = render_template(
        TEMPLATES_DIR / "02_set_modes.dgmgrl",
        STANDBY_UNIQUE_NAME=standby_db_unique_name,
        PROTECTION_MODE=protection_mode,
        TRANSPORT_MODE=transport_mode,
        APPLY_MODE=apply_mode,
    )
    rc, sout, serr = run_dgmgrl(
        pri, db_name, script,
        sys_password=sys_password, connect_target=primary_tns, timeout=180,
    )
    if rc != 0 or "Error" in sout:
        out.append(StepResult("broker.modes", StepStatus.WARN,
                              f"setting protection/transport/apply mode returned errors (rc={rc})",
                              details={"stdout_tail": sout[-1500:], "stderr_tail": serr[-400:]},
                              elapsed_ms=int((time.monotonic() - t0) * 1000)))
    else:
        out.append(StepResult("broker.modes", StepStatus.PASS,
                              f"protection={protection_mode} transport={transport_mode} apply={apply_mode}",
                              details={"stdout_tail": sout[-1000:]},
                              elapsed_ms=int((time.monotonic() - t0) * 1000)))

    # Enable
    t0 = time.monotonic()
    rc, sout, serr = run_dgmgrl(
        pri, db_name,
        "ENABLE CONFIGURATION;\n",
        sys_password=sys_password, connect_target=primary_tns, timeout=300,
    )
    enabled = rc == 0 and "Enabled" in sout and "Error" not in sout
    out.append(StepResult(
        "broker.enable",
        StepStatus.PASS if enabled else StepStatus.FAIL,
        "configuration enabled" if enabled else f"ENABLE CONFIGURATION failed (rc={rc})",
        details={"stdout_tail": sout[-1500:], "stderr_tail": serr[-400:]},
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    ))

    return out


def wait_for_apply(
    pri: paramiko.SSHClient,
    *,
    db_name: str,
    standby_db_unique_name: str,
    primary_tns: str,
    sys_password: str,
    max_wait_s: int = 1800,
) -> StepResult:
    """Poll dgmgrl SHOW DATABASE until transport_lag/apply_lag are bounded and status is SUCCESS."""
    t0 = time.monotonic()
    name = "broker.apply_lag"

    deadline = time.monotonic() + max_wait_s
    last_out = ""
    while time.monotonic() < deadline:
        rc, sout, _ = run_dgmgrl(
            pri, db_name,
            f"SHOW DATABASE VERBOSE '{standby_db_unique_name}';\n",
            sys_password=sys_password, connect_target=primary_tns, timeout=120,
        )
        last_out = sout
        if rc == 0:
            status_match = re.search(r"Database Status:\s*(\S+)", sout)
            tlag_match = re.search(r"Transport Lag:\s*([^\r\n]+)", sout)
            alag_match = re.search(r"Apply Lag:\s*([^\r\n]+)", sout)
            status = (status_match.group(1) if status_match else "").strip()
            tlag = (tlag_match.group(1) if tlag_match else "").strip()
            alag = (alag_match.group(1) if alag_match else "").strip()
            log.info("dgmgrl SHOW DATABASE: status=%s transport_lag=%s apply_lag=%s", status, tlag, alag)
            if status == "SUCCESS":
                return StepResult(
                    name, StepStatus.PASS,
                    f"standby caught up (transport_lag={tlag}, apply_lag={alag})",
                    details={"status": status, "transport_lag": tlag, "apply_lag": alag},
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                )
        time.sleep(20)

    return StepResult(
        name, StepStatus.WARN,
        f"standby did not reach SUCCESS within {max_wait_s}s",
        details={"last_show_tail": last_out[-1500:]},
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )
