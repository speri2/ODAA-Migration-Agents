"""Validate the agent5_input.json contract emitted by Agent 4."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .common import StepResult, StepStatus

INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "schema_version", "status", "primary_database", "standby_target",
        "ssh", "scan", "secrets", "dataguard_directives",
    ],
    "properties": {
        "schema_version": {"type": "string"},
        "status": {"enum": ["ready", "ready_with_warnings", "blocked"]},
        "primary_database": {
            "type": "object",
            "required": ["db_name", "db_unique_name", "pdb_name", "is_rac", "is_cdb",
                         "archivelog", "force_logging", "tde", "standby_redo_logs"],
            "properties": {
                "db_name": {"type": "string", "minLength": 1},
                "db_unique_name": {"type": "string", "minLength": 1},
                "pdb_name": {"type": "string", "minLength": 1},
                "version": {"type": "string"},
                "is_rac": {"type": "boolean"},
                "is_cdb": {"type": "boolean"},
                "instance_count": {"type": "integer", "minimum": 1},
                "archivelog": {"type": "string"},
                "force_logging": {"type": "string"},
                "tde": {
                    "type": "object",
                    "required": ["wallet_status", "wallet_location"],
                    "properties": {
                        "wallet_status": {"type": "string"},
                        "wallet_location": {"type": "string"},
                    },
                },
                "standby_redo_logs": {
                    "type": "object",
                    "required": ["count"],
                    "properties": {"count": {"type": "integer", "minimum": 4}},
                },
            },
        },
        "standby_target": {
            "type": "object",
            "required": ["db_unique_name"],
            "properties": {"db_unique_name": {"type": "string", "minLength": 1}},
        },
        "ssh": {
            "type": "object",
            "required": ["node1_host", "user"],
            "properties": {
                "node1_host": {"type": "string", "minLength": 1},
                "user": {"type": "string", "minLength": 1},
                "private_key_path": {"type": ["string", "null"]},
            },
        },
        "scan": {
            "type": "object",
            "required": ["name", "tcp_port"],
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "tcp_port": {"type": "integer"},
                "tcps_port": {"type": ["integer", "null"]},
            },
        },
        "secrets": {
            "type": "object",
            "properties": {
                "source": {"type": ["string", "null"]},
                "akv_vault_name": {"type": ["string", "null"]},
                "akv_secret_names": {"type": "object"},
            },
        },
        "dataguard_directives": {
            "type": "object",
            "required": ["proceed"],
            "properties": {
                "proceed": {"type": "boolean"},
                "broker_required": {"type": "boolean"},
                "protection_mode": {"enum": ["MAX_PERFORMANCE", "MAX_AVAILABILITY", "MAX_PROTECTION"]},
                "transport_mode": {"enum": ["ASYNC", "SYNC", "FASTSYNC"]},
                "apply_mode": {"enum": ["REAL_TIME_APPLY", "ARCHIVED_LOG_APPLY"]},
                "fast_start_failover": {"type": "boolean"},
            },
        },
    },
}


def validate_input(input_path: Path) -> StepResult:
    t0 = time.monotonic()
    name = "input_contract"
    if not input_path.exists():
        return StepResult(name, StepStatus.FAIL, f"input not found: {input_path}")
    try:
        data = json.loads(input_path.read_text())
    except json.JSONDecodeError as e:
        return StepResult(name, StepStatus.FAIL, f"input is not valid JSON: {e}")

    errs = sorted(Draft202012Validator(INPUT_SCHEMA).iter_errors(data), key=lambda e: list(e.path))
    if errs:
        return StepResult(
            name, StepStatus.FAIL, f"{len(errs)} schema violation(s)",
            details={"violations": [{"path": "/".join(map(str, e.path)) or "<root>", "message": e.message}
                                    for e in errs[:20]]},
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )

    if not data["dataguard_directives"].get("proceed"):
        return StepResult(
            name, StepStatus.FAIL,
            f"upstream gate closed: status={data['status']}, proceed=false",
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )

    # Cross-field assertions: things that would silently break Data Guard later.
    pri = data["primary_database"]
    crossfield_errs: list[str] = []
    if pri["archivelog"].upper() != "ARCHIVELOG":
        crossfield_errs.append(f"primary not in ARCHIVELOG: {pri['archivelog']}")
    if pri["force_logging"].upper() != "YES":
        crossfield_errs.append(f"primary FORCE_LOGGING is not YES: {pri['force_logging']}")
    if (pri["tde"].get("wallet_status") or "").upper() not in ("OPEN", "OPEN_NO_MASTER_KEY"):
        crossfield_errs.append(f"primary wallet not OPEN: {pri['tde'].get('wallet_status')}")
    if pri["standby_redo_logs"]["count"] < 4:
        crossfield_errs.append(f"primary has too few SRLs: {pri['standby_redo_logs']['count']} (need >=4)")

    if crossfield_errs:
        return StepResult(
            name, StepStatus.FAIL, f"{len(crossfield_errs)} cross-field violation(s)",
            details={"errors": crossfield_errs},
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )

    return StepResult(
        name, StepStatus.PASS,
        f"input contract valid (upstream status={data['status']})",
        details={"upstream_status": data["status"]},
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )


def load_input(input_path: Path) -> dict[str, Any]:
    return json.loads(input_path.read_text())
