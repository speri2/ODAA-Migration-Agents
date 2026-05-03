"""Validate the agent4_input.json contract emitted by Agent 3."""
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
    "required": ["schema_version", "status", "upstream", "agent4_directives"],
    "properties": {
        "schema_version": {"type": "string"},
        "status": {"enum": ["ready", "ready_with_warnings", "blocked"]},
        "upstream": {
            "type": "object",
            "required": ["azure", "vm_cluster", "oracle_net", "ssh"],
            "properties": {
                "azure": {"type": "object"},
                "vm_cluster": {"type": "object"},
                "oracle_net": {"type": "object"},
                "ssh": {"type": "object"},
            },
        },
        "agent4_directives": {
            "type": "object",
            "required": ["proceed", "ssh_target_node1", "ssh_user", "use_scan_dns_name"],
            "properties": {
                "proceed": {"type": "boolean"},
                "ssh_target_node1": {"type": "string", "minLength": 1},
                "ssh_user": {"type": "string", "minLength": 1},
                "ssh_private_key_path": {"type": ["string", "null"]},
                "use_scan_dns_name": {"type": "string"},
                "use_scan_port_tcp": {"type": "integer"},
                "use_scan_port_tcps": {"type": ["integer", "null"]},
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

    if not data["agent4_directives"].get("proceed"):
        return StepResult(
            name, StepStatus.FAIL,
            f"upstream gate closed: status={data['status']}, proceed=false",
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
