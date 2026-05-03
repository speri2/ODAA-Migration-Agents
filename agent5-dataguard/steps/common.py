"""SSH primitives, sqlplus / dgmgrl / rman helpers, step result types."""
from __future__ import annotations

import dataclasses
import enum
import io
import logging
import os
import shlex
from pathlib import Path
from typing import Any

import paramiko

log = logging.getLogger("agent5")


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


# ---------------- SSH ----------------

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


def push_local_file(cli: paramiko.SSHClient, local_path: Path, remote_path: str, mode: int = 0o600) -> None:
    sftp = cli.open_sftp()
    try:
        sftp.put(str(local_path), remote_path)
        sftp.chmod(remote_path, mode)
    finally:
        sftp.close()


# ---------------- sqlplus / dgmgrl / rman ----------------

def run_sqlplus_as_oracle(
    cli: paramiko.SSHClient,
    db_name: str,
    sql_text: str,
    *,
    container: str | None = None,
    timeout: float = 300.0,
    connect_string: str | None = None,
    sys_password: str | None = None,
) -> tuple[int, str, str]:
    """
    Run a SQL block as the oracle user. By default connects bequeath ('/ as sysdba').
    If connect_string is provided (e.g., 'sys@TNSALIAS as sysdba'), an explicit password is required.
    """
    container_cmd = f"ALTER SESSION SET CONTAINER={container};\n" if container else ""
    full_sql = (
        "WHENEVER SQLERROR EXIT FAILURE\n"
        "WHENEVER OSERROR EXIT FAILURE\n"
        "SET ECHO ON FEEDBACK ON LINESIZE 200 PAGESIZE 1000 SERVEROUTPUT ON\n"
        f"{container_cmd}{sql_text}\nEXIT;\n"
    )
    if connect_string:
        if not sys_password:
            raise ValueError("sys_password required when connect_string is set")
        # Use environment variable to avoid leaking password into ps -ef
        connect = f'sys/$ORA_SYS_PW@{connect_string}'
        login = f"export ORA_SYS_PW={shlex.quote(sys_password)}; "
        sqlplus_cmd = f"sqlplus -s -L {connect}"
    else:
        login = ""
        sqlplus_cmd = "sqlplus -s -L / as sysdba"

    heredoc = (
        "sudo -iu oracle bash -lc '"
        f"export ORAENV_ASK=NO; export ORACLE_SID={db_name}1; "
        ". oraenv >/dev/null 2>&1; "
        f"{login}{sqlplus_cmd} <<\"AGENT5_SQL_EOF\"\n"
        f"{full_sql}\n"
        "AGENT5_SQL_EOF"
        "'"
    )
    return run_remote(cli, heredoc, timeout=timeout, sensitive=bool(connect_string))


def run_dgmgrl(
    cli: paramiko.SSHClient,
    db_name: str,
    commands: str,
    *,
    sys_password: str,
    connect_target: str = "localhost:1521/+ASM",  # overridden by caller for DB connect
    timeout: float = 600.0,
) -> tuple[int, str, str]:
    """
    Run dgmgrl commands. Connects via 'sys@<connect_target>' so it works against either the
    primary or standby cluster. The caller passes db_unique_name as connect_target alias.
    """
    script = (
        "set echo on\n"
        "set time on\n"
        f"{commands}\n"
        "exit\n"
    )
    # Use a heredoc, password from env to avoid ps -ef leakage
    heredoc = (
        "sudo -iu oracle bash -lc '"
        f"export ORAENV_ASK=NO; export ORACLE_SID={db_name}1; "
        ". oraenv >/dev/null 2>&1; "
        f"export ORA_SYS_PW={shlex.quote(sys_password)}; "
        f"dgmgrl -silent sys/$ORA_SYS_PW@{shlex.quote(connect_target)} <<\"AGENT5_DGMGRL_EOF\"\n"
        f"{script}\n"
        "AGENT5_DGMGRL_EOF"
        "'"
    )
    return run_remote(cli, heredoc, timeout=timeout, sensitive=True)


def run_rman(
    cli: paramiko.SSHClient,
    db_name: str,
    rman_script: str,
    *,
    target_connect: str,
    auxiliary_connect: str,
    sys_password: str,
    timeout: float = 14400.0,  # default 4 hours for full duplicate
) -> tuple[int, str, str]:
    """
    Run an RMAN script as oracle. target_connect / auxiliary_connect take the form
    'sys@TNSALIAS' — the password is supplied via the ORA_SYS_PW env var.
    """
    heredoc = (
        "sudo -iu oracle bash -lc '"
        f"export ORAENV_ASK=NO; export ORACLE_SID={db_name}1; "
        ". oraenv >/dev/null 2>&1; "
        f"export ORA_SYS_PW={shlex.quote(sys_password)}; "
        f"rman target sys/$ORA_SYS_PW@{shlex.quote(target_connect.split('@',1)[1])} "
        f"auxiliary sys/$ORA_SYS_PW@{shlex.quote(auxiliary_connect.split('@',1)[1])} "
        "<<\"AGENT5_RMAN_EOF\"\n"
        f"{rman_script}\n"
        "AGENT5_RMAN_EOF"
        "'"
    )
    return run_remote(cli, heredoc, timeout=timeout, sensitive=True)


def render_template(template_path: Path, **bindings: Any) -> str:
    """Trivial templating: replaces ${KEY} tokens with bindings[KEY]."""
    text = template_path.read_text()
    for k, v in bindings.items():
        text = text.replace(f"${{{k}}}", str(v))
    return text
