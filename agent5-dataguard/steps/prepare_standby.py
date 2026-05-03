"""
Prepare the standby cluster *before* RMAN DUPLICATE.

This is the load-bearing step: getting the wallet wrong here is the most common reason
RMAN duplicate fails ('ORA-19914: unable to encrypt backup' / 'ORA-28365: wallet is not open').

Steps:
  1. Copy the TDE wallet (ewallet.p12 + cwallet.sso) from primary to a staging dir on standby.
  2. Place the wallet on standby at the location WALLET_ROOT/<db_unique_name>/tde expects.
  3. Copy the orapw password file from primary to ASM on standby (orapwd file=...).
  4. Stage a minimal init<SID>.ora on standby (just enough to start NOMOUNT for duplicate).
  5. Static-register the standby DB with the local listener (so RMAN DUPLICATE can connect
     while the standby is still NOMOUNT — dynamic registration won't kick in until OPEN).
"""
from __future__ import annotations

import logging
import shlex
import time
from pathlib import Path
from typing import Any

import paramiko

from .common import StepResult, StepStatus, run_remote, render_template, upload_str

log = logging.getLogger("agent5")
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "sql"

WALLET_STAGING = "/var/tmp/agent5_wallet_stage"
INIT_STAGING = "/var/tmp/agent5_initora_stage"
PWFILE_STAGING = "/var/tmp/agent5_orapw_stage"
LISTENER_FRAG = "/var/tmp/agent5_listener_static.ora"


# ---------------- TDE wallet copy ----------------

def _copy_wallet_primary_to_standby(
    pri: paramiko.SSHClient,
    stby: paramiko.SSHClient,
    *,
    primary_wallet_path: str,
    standby_wallet_path: str,
) -> StepResult:
    t0 = time.monotonic()
    name = "stby.wallet_copy"

    # Tar wallet on primary, ship via SFTP, untar on standby.
    rc, sout, serr = run_remote(
        pri,
        f"sudo -iu oracle bash -lc 'tar -C {shlex.quote(primary_wallet_path)} -czf "
        f"{WALLET_STAGING}.tgz . && ls -la {WALLET_STAGING}.tgz'",
        timeout=180,
    )
    if rc != 0:
        return StepResult(name, StepStatus.FAIL, f"primary tar failed rc={rc}",
                          details={"stdout_tail": sout[-400:], "stderr_tail": serr[-400:]},
                          elapsed_ms=int((time.monotonic() - t0) * 1000))

    # Pull tar from primary → push to standby
    sftp_p = pri.open_sftp()
    try:
        local = Path("/tmp/agent5_wallet.tgz")
        sftp_p.get(f"{WALLET_STAGING}.tgz", str(local))
    finally:
        sftp_p.close()

    sftp_s = stby.open_sftp()
    try:
        sftp_s.put(str(local), f"{WALLET_STAGING}.tgz")
    finally:
        sftp_s.close()
    try:
        local.unlink()
    except FileNotFoundError:
        pass

    # Untar on standby into the configured wallet path, fix permissions
    rc, sout, serr = run_remote(
        stby,
        f"sudo -iu oracle bash -lc 'mkdir -p {shlex.quote(standby_wallet_path)} && "
        f"tar -C {shlex.quote(standby_wallet_path)} -xzf {WALLET_STAGING}.tgz && "
        f"chown -R oracle:oinstall {shlex.quote(standby_wallet_path)} && "
        f"chmod 700 {shlex.quote(standby_wallet_path)} && "
        f"ls -la {shlex.quote(standby_wallet_path)}'",
        timeout=120,
    )
    # Cleanup primary tar
    run_remote(pri, f"rm -f {WALLET_STAGING}.tgz", timeout=15)
    run_remote(stby, f"rm -f {WALLET_STAGING}.tgz", timeout=15)

    if rc != 0:
        return StepResult(name, StepStatus.FAIL, f"standby untar failed rc={rc}",
                          details={"stdout_tail": sout[-400:], "stderr_tail": serr[-400:]},
                          elapsed_ms=int((time.monotonic() - t0) * 1000))

    # Verify the right files landed
    files = [ln for ln in sout.splitlines() if "p12" in ln or "sso" in ln]
    has_p12 = any("ewallet.p12" in f for f in files)
    has_sso = any("cwallet.sso" in f for f in files)
    if not has_p12:
        return StepResult(name, StepStatus.FAIL, "ewallet.p12 missing on standby after copy",
                          details={"files": files},
                          elapsed_ms=int((time.monotonic() - t0) * 1000))

    return StepResult(
        name, StepStatus.PASS,
        "TDE wallet copied to standby" + ("" if has_sso else " (NOTE: cwallet.sso absent — autologin will not work)"),
        details={"path": standby_wallet_path, "p12": has_p12, "sso": has_sso},
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )


# ---------------- password file copy ----------------

def _copy_password_file(
    pri: paramiko.SSHClient,
    stby: paramiko.SSHClient,
    *,
    db_name: str,
    primary_db_unique_name: str,
    standby_db_unique_name: str,
) -> StepResult:
    t0 = time.monotonic()
    name = "stby.pwfile_copy"

    # Pull current orapw out of ASM on the primary into a local file, ship to standby, push back to ASM.
    pri_asm_path = f"+DATA/{primary_db_unique_name.upper()}/PASSWORD/orapw{db_name.lower()}"
    stby_asm_path = f"+DATA/{standby_db_unique_name.upper()}/PASSWORD/orapw{db_name.lower()}"

    fetch = (
        "sudo -iu oracle bash -lc '"
        f"asmcmd cp {shlex.quote(pri_asm_path)} {PWFILE_STAGING} && "
        f"ls -la {PWFILE_STAGING}'"
    )
    rc, sout, serr = run_remote(pri, fetch, timeout=120)
    if rc != 0:
        return StepResult(name, StepStatus.FAIL, f"asmcmd cp failed on primary rc={rc}",
                          details={"stdout_tail": sout[-400:], "stderr_tail": serr[-400:]},
                          elapsed_ms=int((time.monotonic() - t0) * 1000))

    # SFTP transfer
    sftp_p = pri.open_sftp()
    try:
        local = Path("/tmp/agent5_orapw.bin")
        sftp_p.get(PWFILE_STAGING, str(local))
    finally:
        sftp_p.close()

    sftp_s = stby.open_sftp()
    try:
        sftp_s.put(str(local), PWFILE_STAGING)
    finally:
        sftp_s.close()
    try:
        local.unlink()
    except FileNotFoundError:
        pass

    push = (
        "sudo -iu oracle bash -lc '"
        f"asmcmd mkdir +DATA/{standby_db_unique_name.upper()} 2>/dev/null || true; "
        f"asmcmd mkdir +DATA/{standby_db_unique_name.upper()}/PASSWORD 2>/dev/null || true; "
        f"asmcmd cp {PWFILE_STAGING} {shlex.quote(stby_asm_path)}'"
    )
    rc2, sout2, serr2 = run_remote(stby, push, timeout=120)

    # Cleanup
    run_remote(pri, f"rm -f {PWFILE_STAGING}", timeout=15)
    run_remote(stby, f"rm -f {PWFILE_STAGING}", timeout=15)

    if rc2 != 0:
        return StepResult(name, StepStatus.FAIL, f"asmcmd cp failed on standby rc={rc2}",
                          details={"stdout_tail": sout2[-400:], "stderr_tail": serr2[-400:]},
                          elapsed_ms=int((time.monotonic() - t0) * 1000))

    return StepResult(name, StepStatus.PASS, "password file copied into standby ASM",
                      details={"path": stby_asm_path},
                      elapsed_ms=int((time.monotonic() - t0) * 1000))


# ---------------- pfile staging + nomount start ----------------

def _stage_pfile_and_start_nomount(
    stby: paramiko.SSHClient,
    *,
    db_name: str,
    db_unique_name: str,
    primary_db_unique_name: str,
    standby_wallet_path: str,
) -> StepResult:
    t0 = time.monotonic()
    name = "stby.nomount_start"

    pfile_text = render_template(
        TEMPLATES_DIR / "01_standby_init_params.sql",
        DB_NAME=db_name,
        STANDBY_UNIQUE_NAME=db_unique_name,
        PRIMARY_UNIQUE_NAME=primary_db_unique_name,
        WALLET_ROOT=str(Path(standby_wallet_path).parent.parent),
    )
    # Strip SQL*Plus directives — RMAN nomount needs only init parameters.
    pfile_only = "\n".join(
        ln for ln in pfile_text.splitlines()
        if not ln.strip().startswith(("--", "WHENEVER", "SET", "EXIT"))
        and not ln.strip().startswith("/")
        and ln.strip()
    )
    upload_str(stby, pfile_only, f"/tmp/init{db_name.lower()}.ora", mode=0o644)

    cmd = (
        "sudo -iu oracle bash -lc '"
        f"export ORAENV_ASK=NO; export ORACLE_SID={db_name}1; "
        ". oraenv >/dev/null 2>&1; "
        f"cp /tmp/init{db_name.lower()}.ora $ORACLE_HOME/dbs/init{db_name}1.ora; "
        f"mkdir -p $ORACLE_BASE/admin/{db_unique_name}/adump; "
        "sqlplus -s -L / as sysdba <<\"AGENT5_NM_EOF\"\n"
        "WHENEVER SQLERROR EXIT FAILURE\n"
        f"STARTUP NOMOUNT PFILE='$ORACLE_HOME/dbs/init{db_name}1.ora';\n"
        "EXIT;\n"
        "AGENT5_NM_EOF"
        "'"
    )
    rc, sout, serr = run_remote(stby, cmd, timeout=300)
    if rc != 0:
        return StepResult(name, StepStatus.FAIL, f"standby NOMOUNT failed rc={rc}",
                          details={"stdout_tail": sout[-1000:], "stderr_tail": serr[-400:]},
                          elapsed_ms=int((time.monotonic() - t0) * 1000))

    return StepResult(name, StepStatus.PASS, "standby instance started NOMOUNT",
                      details={"sid": f"{db_name}1"},
                      elapsed_ms=int((time.monotonic() - t0) * 1000))


# ---------------- static listener registration ----------------

def _register_static_listener(
    stby: paramiko.SSHClient,
    *,
    db_name: str,
    db_unique_name: str,
    listener_host: str,
) -> StepResult:
    t0 = time.monotonic()
    name = "stby.static_listener"

    # Append a static SID_LIST entry so RMAN DUPLICATE can connect to nomount instance.
    fragment = (
        "SID_LIST_LISTENER=\n"
        "  (SID_LIST=\n"
        f"    (SID_DESC=\n"
        f"      (GLOBAL_DBNAME={db_unique_name})\n"
        f"      (ORACLE_HOME=$ORACLE_HOME)\n"
        f"      (SID_NAME={db_name}1)\n"
        f"    )\n"
        f"    (SID_DESC=\n"
        f"      (GLOBAL_DBNAME={db_unique_name}_DGMGRL)\n"
        f"      (ORACLE_HOME=$ORACLE_HOME)\n"
        f"      (SID_NAME={db_name}1)\n"
        f"    )\n"
        "  )\n"
    )
    upload_str(stby, fragment, LISTENER_FRAG, mode=0o644)

    cmd = (
        "sudo -iu grid bash -lc '"
        "set -e; "
        "GRID_HOME=$(crsctl query crs activeversion -f >/dev/null 2>&1; echo ${ORACLE_HOME}); "
        ": ${GRID_HOME:=/u01/app/19.0.0.0/grid}; "
        "LFILE=$GRID_HOME/network/admin/listener.ora; "
        f"if ! grep -q \"GLOBAL_DBNAME={db_unique_name}\" \"$LFILE\" 2>/dev/null; then "
        f"  cat {LISTENER_FRAG} >> \"$LFILE\"; "
        "fi; "
        "lsnrctl reload LISTENER'"
    )
    rc, sout, serr = run_remote(stby, cmd, timeout=120)
    run_remote(stby, f"rm -f {LISTENER_FRAG}", timeout=15)
    if rc != 0:
        return StepResult(name, StepStatus.WARN,
                          f"static listener reload rc={rc} — dynamic registration may still work",
                          details={"stdout_tail": sout[-400:], "stderr_tail": serr[-400:]},
                          elapsed_ms=int((time.monotonic() - t0) * 1000))
    return StepResult(name, StepStatus.PASS,
                      f"static GLOBAL_DBNAME={db_unique_name} registered with listener on {listener_host}",
                      elapsed_ms=int((time.monotonic() - t0) * 1000))


# ---------------- public entry point ----------------

def prepare_standby(
    pri: paramiko.SSHClient,
    stby: paramiko.SSHClient,
    *,
    db_name: str,
    primary_db_unique_name: str,
    standby_db_unique_name: str,
    primary_wallet_path: str,
    standby_wallet_path: str,
    standby_listener_host: str,
) -> list[StepResult]:
    out: list[StepResult] = []
    out.append(_copy_wallet_primary_to_standby(
        pri, stby,
        primary_wallet_path=primary_wallet_path,
        standby_wallet_path=standby_wallet_path,
    ))
    if out[-1].status == StepStatus.FAIL:
        return out

    out.append(_copy_password_file(
        pri, stby,
        db_name=db_name,
        primary_db_unique_name=primary_db_unique_name,
        standby_db_unique_name=standby_db_unique_name,
    ))
    if out[-1].status == StepStatus.FAIL:
        return out

    out.append(_stage_pfile_and_start_nomount(
        stby,
        db_name=db_name,
        db_unique_name=standby_db_unique_name,
        primary_db_unique_name=primary_db_unique_name,
        standby_wallet_path=standby_wallet_path,
    ))
    if out[-1].status == StepStatus.FAIL:
        return out

    out.append(_register_static_listener(
        stby,
        db_name=db_name,
        db_unique_name=standby_db_unique_name,
        listener_host=standby_listener_host,
    ))
    return out
