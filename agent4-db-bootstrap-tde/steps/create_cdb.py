"""Create the CDB + first PDB via dbaascli, idempotently."""
from __future__ import annotations

import json
import logging
import re
import shlex
import time
from typing import Any

import paramiko

from .common import StepResult, StepStatus, run_remote, upload_str
from .secrets import SecretBundle

log = logging.getLogger("agent4")

REMOTE_PWFILE = "/var/tmp/agent4_dbaas_pw.json"  # 0600, removed at end of step


def _db_exists(cli: paramiko.SSHClient, db_name: str) -> tuple[bool, dict[str, Any]]:
    rc, out, err = run_remote(
        cli,
        f"sudo -iu oracle bash -lc 'dbaascli database getDetails --dbname {shlex.quote(db_name)} --json' 2>/dev/null",
        timeout=120,
    )
    if rc != 0:
        return False, {"rc": rc, "stderr": err.strip()[-400:]}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        # Some dbaascli versions wrap output; try to find a JSON object
        m = re.search(r"\{.*\}\s*$", out, re.DOTALL)
        if not m:
            return False, {"rc": rc, "stdout_tail": out[-400:]}
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return False, {"rc": rc, "stdout_tail": out[-400:]}
    return True, data


def _wait_for_completion(cli: paramiko.SSHClient, job_id: str, *, max_wait_s: int = 5400) -> tuple[bool, str]:
    """Poll a dbaascli job. Returns (success, last_status_text)."""
    deadline = time.monotonic() + max_wait_s
    last = ""
    while time.monotonic() < deadline:
        rc, out, _ = run_remote(
            cli,
            f"sudo -iu oracle bash -lc 'dbaascli job getStatus --jobid {shlex.quote(job_id)} --json'",
            timeout=60,
        )
        last = out.strip()
        try:
            data = json.loads(last)
            status = (data.get("status") or "").upper()
        except json.JSONDecodeError:
            status = ""
        log.info("dbaascli job %s status=%s", job_id, status or "?")
        if status in ("SUCCESS", "COMPLETED"):
            return True, last
        if status in ("FAILED", "FAILURE", "ERROR"):
            return False, last
        time.sleep(30)
    return False, f"timeout after {max_wait_s}s; last={last[-400:]}"


def create_cdb_idempotent(
    cli: paramiko.SSHClient,
    *,
    db_name: str,
    db_unique_name: str,
    pdb_name: str,
    oracle_home_version: str,
    secrets: SecretBundle,
    character_set: str = "AL32UTF8",
    national_character_set: str = "AL16UTF16",
    enable_archivelog: bool = True,
) -> tuple[StepResult, dict[str, Any]]:
    """
    If the database already exists, return its details (idempotent re-run).
    Otherwise invoke `dbaascli database create` with TDE enabled and wait for the job.
    """
    t0 = time.monotonic()
    name = "create_cdb"

    exists, details = _db_exists(cli, db_name)
    if exists:
        return (
            StepResult(name, StepStatus.PASS,
                       f"database {db_name} already exists — skipping create",
                       details={"existing": details},
                       elapsed_ms=int((time.monotonic() - t0) * 1000)),
            details,
        )

    # Stage password file (mode 0600, owned by oracle)
    pw_doc = {
        "sysPassword": secrets.sys_password,
        "tdePassword": secrets.tde_password,
        "adminPassword": secrets.sys_password,
    }
    upload_str(cli, json.dumps(pw_doc), REMOTE_PWFILE, mode=0o600)
    run_remote(cli, f"sudo chown oracle:oinstall {shlex.quote(REMOTE_PWFILE)}", timeout=30)

    # dbaascli command — async, returns a job id we then poll
    cmd = (
        "sudo -iu oracle bash -lc '"
        "dbaascli database create "
        f"--dbName {shlex.quote(db_name)} "
        f"--dbUniqueName {shlex.quote(db_unique_name)} "
        f"--pdbName {shlex.quote(pdb_name)} "
        f"--oracleHomeVersion {shlex.quote(oracle_home_version)} "
        f"--characterSet {shlex.quote(character_set)} "
        f"--nationalCharacterSet {shlex.quote(national_character_set)} "
        "--dbType RAC "
        "--enableTDE true "
        f"--enableArchiveLog {'true' if enable_archivelog else 'false'} "
        f"--passwordFile {shlex.quote(REMOTE_PWFILE)} "
        "--waitForCompletion false "
        "--json"
        "'"
    )
    rc, out, err = run_remote(cli, cmd, timeout=300, sensitive=False)
    # Always remove the password file
    run_remote(cli, f"sudo rm -f {shlex.quote(REMOTE_PWFILE)}", timeout=15)

    if rc != 0:
        return (
            StepResult(name, StepStatus.FAIL, f"dbaascli submit failed rc={rc}",
                       details={"stderr_tail": err[-800:], "stdout_tail": out[-800:]},
                       elapsed_ms=int((time.monotonic() - t0) * 1000)),
            {},
        )

    # Pull job id from stdout
    job_id = None
    try:
        submit = json.loads(out)
        job_id = submit.get("jobId") or submit.get("jobID") or submit.get("job_id")
    except json.JSONDecodeError:
        m = re.search(r'"jobI[dD]"\s*:\s*"([^"]+)"', out)
        if m:
            job_id = m.group(1)

    if not job_id:
        return (
            StepResult(name, StepStatus.FAIL, "could not parse dbaascli job id",
                       details={"stdout_tail": out[-1200:]},
                       elapsed_ms=int((time.monotonic() - t0) * 1000)),
            {},
        )

    log.info("dbaascli database create submitted: jobId=%s — polling (this can take 30-60 min)", job_id)
    ok, last = _wait_for_completion(cli, job_id, max_wait_s=5400)
    elapsed = int((time.monotonic() - t0) * 1000)
    if not ok:
        return (
            StepResult(name, StepStatus.FAIL, "dbaascli create did not finish successfully",
                       details={"job_id": job_id, "last_status": last[-1200:]}, elapsed_ms=elapsed),
            {},
        )

    # Re-fetch details
    _, details = _db_exists(cli, db_name)
    return (
        StepResult(name, StepStatus.PASS, f"database {db_name} created",
                   details={"job_id": job_id, "details": details}, elapsed_ms=elapsed),
        details,
    )
