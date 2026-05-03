"""Validate the agent3_input.json contract emitted by Agent 2."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .common import CheckResult, Severity, timed

INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["schema_version", "azure", "exadata_infrastructure", "vm_cluster", "network", "oracle_net", "ssh"],
    "properties": {
        "schema_version": {"type": "string"},
        "azure": {
            "type": "object",
            "required": ["subscription_id", "resource_group_name", "location"],
            "properties": {
                "subscription_id": {"type": "string", "minLength": 8},
                "tenant_id": {"type": "string"},
                "resource_group_name": {"type": "string", "minLength": 1},
                "location": {"type": "string", "minLength": 2},
                "availability_zone": {"type": "string"},
            },
        },
        "exadata_infrastructure": {
            "type": "object",
            "required": ["azure_id"],
            "properties": {"azure_id": {"type": "string"}, "ocid": {"type": "string"}},
        },
        "vm_cluster": {
            "type": "object",
            "required": ["azure_id", "cluster_name", "hostname_prefix"],
            "properties": {
                "azure_id": {"type": "string"},
                "ocid": {"type": "string"},
                "cluster_name": {"type": "string"},
                "hostname_prefix": {"type": "string"},
                "domain": {"type": ["string", "null"]},
                "gi_version": {"type": "string"},
            },
        },
        "network": {
            "type": "object",
            "required": ["client_subnet_id"],
            "properties": {
                "vnet_id": {"type": "string"},
                "client_subnet_id": {"type": "string"},
                "client_subnet_cidr": {"type": "string"},
            },
        },
        "oracle_net": {
            "type": "object",
            "required": ["scan_dns_name", "scan_listener_port_tcp"],
            "properties": {
                "scan_dns_name": {"type": "string", "minLength": 1},
                "scan_listener_port_tcp": {"type": "integer", "minimum": 1, "maximum": 65535},
                "scan_listener_port_tcp_ssl": {"type": "integer", "minimum": 1, "maximum": 65535},
            },
        },
        "ssh": {
            "type": "object",
            "required": ["os_user"],
            "properties": {
                "os_user": {"type": "string"},
                "private_key_path": {"type": ["string", "null"]},
                "authorized_keys": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
}


@timed
def validate_input(input_path: Path) -> CheckResult:
    """Load and JSON-Schema validate the upstream contract."""
    name = "input_contract"
    if not input_path.exists():
        return CheckResult(name, Severity.FAIL, f"input file not found: {input_path}")
    try:
        data = json.loads(input_path.read_text())
    except json.JSONDecodeError as e:
        return CheckResult(name, Severity.FAIL, f"input is not valid JSON: {e}")

    validator = Draft202012Validator(INPUT_SCHEMA)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    if errors:
        details = {
            "violations": [
                {"path": "/".join(map(str, e.path)) or "<root>", "message": e.message}
                for e in errors[:25]
            ]
        }
        return CheckResult(name, Severity.FAIL, f"{len(errors)} schema violation(s)", details)

    return CheckResult(
        name,
        Severity.PASS,
        "input contract valid",
        details={
            "schema_version": data["schema_version"],
            "scan_dns_name": data["oracle_net"]["scan_dns_name"],
            "vm_cluster": data["vm_cluster"]["cluster_name"],
        },
    )


def load_input(input_path: Path) -> dict[str, Any]:
    return json.loads(input_path.read_text())
