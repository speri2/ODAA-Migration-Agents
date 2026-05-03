"""Verify (and finish) TDE setup. dbaascli's --enableTDE handles most of this; we assert + autologin."""
from __future__ import annotations

import logging
import shlex
import time
from typing import Any

import paramiko

from .common import StepResult, StepStatus, run_sqlplus_as_oracle, run_remote
from .secrets import SecretBundle

log = logging.getLogger("agent4")


def _query(cli: paramiko.SSHClient, db_name: str, sql: str, container: str | None = None) -> tuple[int, str, str]:
    return run_sqlplus_as_oracle(cli, db_name, sql, container=container, timeout=120)


def configure_tde(
    cli: paramiko.SSHClient,
    *,
    db_name: str,
    pdb_name: str,
    secrets: SecretBundle,
) -> tuple[list[StepResult], dict[str, Any]]:
    """
    Verifies TDE wallet is OPEN at CDB$ROOT and PDB level, ensures autologin keystore exists,
    and rotates if no master key is present in the PDB.
    """
    out: list[StepResult] = []
    info: dict[str, Any] = {}

    # 1) WALLET status @ CDB$ROOT
    t0 = time.monotonic()
    rc, sout, _ = _query(cli, db_name,
        "SELECT wrl_type, wrl_parameter, status, wallet_type FROM v$encryption_wallet;")
    out.append(StepResult(
        "tde.cdb_wallet",
        StepStatus.PASS if (rc == 0 and "OPEN" in sout.upper()) else StepStatus.FAIL,
        "wallet OPEN at CDB$ROOT" if "OPEN" in sout.upper() else "wallet NOT OPEN at CDB$ROOT",
        details={"output_tail": sout[-1500:]},
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    ))
    # Extract wallet location
    info["wallet_location"] = ""
    for line in sout.splitlines():
        if "/wallet" in line.lower() or "/tde" in line.lower():
            tokens = line.split()
            for t in tokens:
                if "/" in t:
                    info["wallet_location"] = t.strip()
                    break

    # 2) PDB master key — create one if absent
    t0 = time.monotonic()
    rc, sout, _ = _query(cli, db_name,
        f"ALTER SESSION SET CONTAINER={pdb_name};\n"
        "SELECT KEY_ID FROM V$ENCRYPTION_KEYS WHERE CON_ID = (SELECT CON_ID FROM V$PDBS "
        f"WHERE NAME='{pdb_name.upper()}') AND ACTIVATION_TIME IS NOT NULL;")
    has_key = bool(rc == 0 and any(len(line.strip()) > 20 for line in sout.splitlines()))
    if has_key:
        out.append(StepResult("tde.pdb_master_key", StepStatus.PASS, "PDB master key already present",
                              details={"output_tail": sout[-800:]},
                              elapsed_ms=int((time.monotonic() - t0) * 1000)))
    else:
        log.info("Setting TDE master key for PDB %s", pdb_name)
        sql = (
            f"ALTER SESSION SET CONTAINER={pdb_name};\n"
            f"ADMINISTER KEY MANAGEMENT SET KEY USING TAG 'agent4-{pdb_name}-init' "
            f"FORCE KEYSTORE IDENTIFIED BY \"{secrets.tde_password}\" WITH BACKUP USING 'agent4-init';"
        )
        rc2, sout2, serr2 = run_sqlplus_as_oracle(cli, db_name, sql, timeout=180)
        out.append(StepResult(
            "tde.pdb_master_key",
            StepStatus.PASS if rc2 == 0 else StepStatus.FAIL,
            "set master key on PDB" if rc2 == 0 else "failed to set PDB master key",
            details={"stdout_tail": sout2[-1200:], "stderr_tail": serr2[-400:]},
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        ))

    # 3) Autologin keystore — required so the DB opens cleanly after restart
    t0 = time.monotonic()
    if info["wallet_location"]:
        wallet_dir = shlex.quote(info["wallet_location"])
        # Check whether cwallet.sso exists (autologin marker)
        rc, sout, _ = run_remote(
            cli,
            f"sudo -iu oracle bash -lc 'ls -la {wallet_dir} 2>/dev/null; "
            f"test -f {wallet_dir}/cwallet.sso && echo PRESENT || echo MISSING'",
            timeout=30,
        )
        if "PRESENT" in sout:
            out.append(StepResult("tde.autologin", StepStatus.PASS, "autologin keystore present",
                                  details={"location": info["wallet_location"]},
                                  elapsed_ms=int((time.monotonic() - t0) * 1000)))
        else:
            sql = (
                f"ADMINISTER KEY MANAGEMENT CREATE AUTO_LOGIN KEYSTORE FROM KEYSTORE "
                f"'{info['wallet_location']}' IDENTIFIED BY \"{secrets.tde_password}\";"
            )
            rc2, sout2, serr2 = run_sqlplus_as_oracle(cli, db_name, sql, timeout=120)
            out.append(StepResult(
                "tde.autologin",
                StepStatus.PASS if rc2 == 0 else StepStatus.WARN,
                "autologin keystore created" if rc2 == 0 else "could not create autologin keystore",
                details={"stdout_tail": sout2[-1000:], "stderr_tail": serr2[-400:]},
                elapsed_ms=int((time.monotonic() - t0) * 1000),
            ))
    else:
        out.append(StepResult("tde.autologin", StepStatus.SKIP,
                              "wallet location not detected; skipping autologin step"))

    return out, info
