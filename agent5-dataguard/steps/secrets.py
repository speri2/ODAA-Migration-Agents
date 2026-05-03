"""Resolve SYS / TDE / wallet passwords. Reads the AKV references emitted by Agent 4."""
from __future__ import annotations

import dataclasses
import logging
import os
import time
from typing import Any

from .common import StepResult, StepStatus

log = logging.getLogger("agent5")


@dataclasses.dataclass
class SecretBundle:
    sys_password: str
    tde_password: str
    wallet_password: str
    source: str  # 'akv' | 'env'


def _from_akv(vault_name: str, names: dict[str, str]) -> SecretBundle:
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient

    cred = DefaultAzureCredential()
    cli = SecretClient(vault_url=f"https://{vault_name}.vault.azure.net", credential=cred)

    def get(key: str) -> str:
        if key not in names:
            raise KeyError(f"AKV secret reference missing: {key}")
        return cli.get_secret(names[key]).value

    return SecretBundle(
        sys_password=get("sys"),
        tde_password=get("tde"),
        wallet_password=get("wallet"),
        source="akv",
    )


def resolve_secrets(upstream_secrets: dict[str, Any]) -> tuple[StepResult, SecretBundle | None]:
    """
    Reads the secrets section emitted by Agent 4. Order:
      1. AKV (vault + secret names)
      2. Environment overrides (AGENT5_SYS_PW, AGENT5_TDE_PW, AGENT5_WALLET_PW)
    """
    t0 = time.monotonic()
    name = "secrets.resolve"

    vault = upstream_secrets.get("akv_vault_name")
    secret_names = upstream_secrets.get("akv_secret_names") or {}
    if vault and secret_names:
        try:
            bundle = _from_akv(vault, secret_names)
            return (
                StepResult(name, StepStatus.PASS, f"loaded from AKV {vault}",
                           details={"source": "akv"},
                           elapsed_ms=int((time.monotonic() - t0) * 1000)),
                bundle,
            )
        except Exception as e:
            log.warning("AKV fetch failed: %r — falling back to env vars if present", e)

    env_sys = os.environ.get("AGENT5_SYS_PW")
    env_tde = os.environ.get("AGENT5_TDE_PW")
    env_wal = os.environ.get("AGENT5_WALLET_PW")
    if env_sys and env_tde and env_wal:
        return (
            StepResult(name, StepStatus.PASS, "loaded from environment",
                       details={"source": "env"},
                       elapsed_ms=int((time.monotonic() - t0) * 1000)),
            SecretBundle(env_sys, env_tde, env_wal, source="env"),
        )

    return (
        StepResult(name, StepStatus.FAIL,
                   "no secret source available (AKV vault unset and env vars not provided)",
                   elapsed_ms=int((time.monotonic() - t0) * 1000)),
        None,
    )
