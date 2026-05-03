"""Resolve passwords for SYS / TDE / wallet from Azure Key Vault (preferred) or local config."""
from __future__ import annotations

import dataclasses
import logging
import os
import secrets as pysecrets
import string
import time
from typing import Any

from .common import StepResult, StepStatus

log = logging.getLogger("agent4")


@dataclasses.dataclass
class SecretBundle:
    sys_password: str
    tde_password: str
    wallet_password: str
    source: str               # 'akv' or 'generated' or 'env'
    akv_vault_name: str | None = None
    akv_secret_names: dict[str, str] = dataclasses.field(default_factory=dict)


def _strong_password(n: int = 22) -> str:
    """Strong password meeting Oracle 19c+ verifier (alpha, digit, special, no @ to avoid sqlplus parser issues)."""
    alphabet = string.ascii_letters + string.digits + "_-#%&+=:"
    while True:
        pw = "".join(pysecrets.choice(alphabet) for _ in range(n))
        if (any(c.isupper() for c in pw)
                and any(c.islower() for c in pw)
                and any(c.isdigit() for c in pw)
                and any(c in "_-#%&+=:" for c in pw)):
            return pw


def _from_akv(vault_name: str, names: dict[str, str]) -> SecretBundle:
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient

    cred = DefaultAzureCredential()
    cli = SecretClient(vault_url=f"https://{vault_name}.vault.azure.net", credential=cred)

    def get(key: str) -> str:
        return cli.get_secret(names[key]).value

    return SecretBundle(
        sys_password=get("sys"),
        tde_password=get("tde"),
        wallet_password=get("wallet"),
        source="akv",
        akv_vault_name=vault_name,
        akv_secret_names=dict(names),
    )


def resolve_secrets(config: dict[str, Any]) -> tuple[StepResult, SecretBundle | None]:
    """
    Resolve passwords. Order of preference:
      1. config['akv'] = {vault, secret_names: {sys, tde, wallet}}  -> Azure Key Vault
      2. environment vars: AGENT4_SYS_PW / AGENT4_TDE_PW / AGENT4_WALLET_PW
      3. generated (development only — emits a WARN)
    """
    t0 = time.monotonic()
    name = "secrets.resolve"

    akv_cfg = config.get("akv") or {}
    if akv_cfg.get("vault_name") and akv_cfg.get("secret_names"):
        try:
            bundle = _from_akv(akv_cfg["vault_name"], akv_cfg["secret_names"])
            return (
                StepResult(name, StepStatus.PASS, f"loaded from AKV {akv_cfg['vault_name']}",
                           details={"source": "akv"}, elapsed_ms=int((time.monotonic() - t0) * 1000)),
                bundle,
            )
        except Exception as e:
            return (
                StepResult(name, StepStatus.FAIL, f"AKV fetch failed: {e!r}",
                           elapsed_ms=int((time.monotonic() - t0) * 1000)),
                None,
            )

    env_sys = os.environ.get("AGENT4_SYS_PW")
    env_tde = os.environ.get("AGENT4_TDE_PW")
    env_wal = os.environ.get("AGENT4_WALLET_PW")
    if env_sys and env_tde and env_wal:
        return (
            StepResult(name, StepStatus.PASS, "loaded from environment",
                       details={"source": "env"}, elapsed_ms=int((time.monotonic() - t0) * 1000)),
            SecretBundle(sys_password=env_sys, tde_password=env_tde, wallet_password=env_wal, source="env"),
        )

    if config.get("allow_generated_passwords"):
        bundle = SecretBundle(
            sys_password=_strong_password(),
            tde_password=_strong_password(),
            wallet_password=_strong_password(),
            source="generated",
        )
        return (
            StepResult(name, StepStatus.WARN,
                       "generated passwords (DEV ONLY) — caller must persist them",
                       details={"source": "generated"},
                       elapsed_ms=int((time.monotonic() - t0) * 1000)),
            bundle,
        )

    return (
        StepResult(name, StepStatus.FAIL,
                   "no password source configured (provide AKV, env vars, or set allow_generated_passwords=true for dev)",
                   elapsed_ms=int((time.monotonic() - t0) * 1000)),
        None,
    )
