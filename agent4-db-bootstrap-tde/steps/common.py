"""SSH primitives, step result types, sqlplus helpers."""
from __future__ import annotations

import dataclasses
import enum
import io
import logging
import os
import shlex
import time
from pathlib import Path
from typing import Any

import paramiko

log = logging.getLogger("agent4")


class StepStatus(str, enum.Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclasses.dataclass
class StepResult:
    name: str
    status: StepStatus
    summary: str
    details: dict[str, Any] = dataclasses.field(default_factory=dict)
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["status"] = self.status.value
        return d


# ---------------- SSH helpers ----------------

def _load_pkey(path: str | None) -> paramiko.PKey | None:
    if not path or not os.path.exists(path):
        return None
    text = Path(path).read_text()
    for cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            return cls.from_private_key(io.StringIO(text))
        except paramiko.SSHException:
            continue
    return None


def open_ssh(host: str, user: str, key_path: str | None, timeout: float = 20.0) -> paramiko.SSHClient:
    pkey = _load_pkey(key_path)
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(
        hostname=host, username=user, pkey=pkey,
        timeout=timeout, banner_timeout=timeout, auth_timeout=timeout,
        look_for_keys=pkey is None, allow_agent=pkey is None,
    )
    cli.get_transport().set_keepalive(30)
    return cli


def run_remote(
    cli: paramiko.SSHClient,
    cmd: str,
    *,
    timeout: float = 600.0,
    sensitive: bool = False,
    stdin_data: str | None = None,
) -> tuple[int, str, str]:
    """Execute a remote command. If sensitive=True, the command body is not logged."""
    log.debug("REMOTE: %s", "<redacted sensitive cmd>" if sensitive else cmd)
    stdin, stdout, stderr = cli.exec_command(cmd, timeout=timeout, get_pty=False)
    if stdin_data is not None:
        stdin.write(stdin_data)
        stdin.flush()
        stdin.channel.shutdown_write()
    rc = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    return rc, out, err


def upload_file(cli: paramiko.SSHClient, local_path: Path, remote_path: str, mode: int = 0o600) -> None:
    sftp = cli.open_sftp()
    try:
        sftp.put(str(local_path), remote_path)
        sftp.chmod(remote_path, mode)
    finally:
        sftp.close()


def upload_str(cli: paramiko.SSHClient, content: str, remote_path: str, mode: int = 0o600) -> None:
    sftp = cli.open_sftp()
    try:
        with sftp.open(remote_path, "w") as f:
            f.write(content)
        sftp.chmod(remote_path, mode)
    finally:
        sftp.close()


def fetch_remote_file(cli: paramiko.SSHClient, remote_path: str, local_path: Path) -> None:
    sftp = cli.open_sftp()
    try:
        sftp.get(remote_path, str(local_path))
    finally:
        sftp.close()


# ---------------- sqlplus helpers ----------------

def sqlplus_oraenv_prefix(db_name: str) -> str:
    """Source oraenv and return the shell prefix that puts ORACLE_HOME/SID into env."""
    return (
        "set -e; "
        f"export ORACLE_SID=$(srvctl status database -d {shlex.quote(db_name)} 2>/dev/null "
        "| awk '/is running on node/ {print $2; exit}'); "
        f"export ORAENV_ASK=NO; export ORACLE_SID=${{ORACLE_SID:-{db_name}1}}; "
        ". oraenv > /dev/null; "
    )


def run_sqlplus_as_oracle(
    cli: paramiko.SSHClient,
    db_name: str,
    sql_text: str,
    *,
    container: str | None = None,
    timeout: float = 300.0,
) -> tuple[int, str, str]:
    """
    Run a SQL block as the oracle user via 'sudo -iu oracle bash -lc'.
    Connects 'as sysdba' via bequeath (no password on the wire).
    """
    container_cmd = f"ALTER SESSION SET CONTAINER={container};\n" if container else ""
    full_sql = (
        "WHENEVER SQLERROR EXIT FAILURE\n"
        "WHENEVER OSERROR EXIT FAILURE\n"
        "SET ECHO ON FEEDBACK ON LINESIZE 200 PAGESIZE 1000 SERVEROUTPUT ON\n"
        f"{container_cmd}{sql_text}\nEXIT;\n"
    )
    heredoc = (
        "sudo -iu oracle bash -lc '"
        f"export ORAENV_ASK=NO; export ORACLE_SID={db_name}1; "
        ". oraenv >/dev/null 2>&1; "
        "sqlplus -s -L / as sysdba <<\"AGENT4_SQL_EOF\"\n"
        f"{full_sql}\n"
        "AGENT4_SQL_EOF"
        "'"
    )
    return run_remote(cli, heredoc, timeout=timeout)


def render_sql(template_path: Path, **bindings: Any) -> str:
    """Trivial templating: replaces ${KEY} tokens with bindings[KEY]."""
    text = template_path.read_text()
    for k, v in bindings.items():
        text = text.replace(f"${{{k}}}", str(v))
    return text
