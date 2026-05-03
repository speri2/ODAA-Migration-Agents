"""Verify the Exadata Infrastructure and VM Cluster are 'Available' in Azure."""
from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

from .common import CheckResult, Severity, timed


def _az(args: list[str], timeout: int = 30) -> tuple[int, str, str]:
    cmd = [shutil.which("az") or "az", *args]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def _resource_state(resource_id: str) -> tuple[Severity, str, dict[str, Any]]:
    rc, out, err = _az(
        ["resource", "show", "--ids", resource_id, "--query", "properties", "-o", "json"]
    )
    if rc != 0:
        return Severity.FAIL, f"az resource show failed: {err.strip()[:240]}", {}
    try:
        props = json.loads(out)
    except json.JSONDecodeError as e:
        return Severity.FAIL, f"unparseable az output: {e}", {}
    state = props.get("lifecycleState") or props.get("provisioningState") or "Unknown"
    if state == "Available":
        return Severity.PASS, f"state=Available", {"state": state}
    if state in ("Provisioning", "Updating"):
        return Severity.WARN, f"state={state} (still in flight)", {"state": state}
    return Severity.FAIL, f"state={state}", {"state": state, "lifecycleDetails": props.get("lifecycleDetails")}


@timed
def _check_resource(name: str, resource_id: str) -> CheckResult:
    if not shutil.which("az"):
        return CheckResult(name, Severity.SKIP, "az CLI not present", {"target": resource_id})
    sev, summary, details = _resource_state(resource_id)
    return CheckResult(name=name, severity=sev, summary=summary, details=details, target=resource_id)


def check_azure_state(payload: dict[str, Any]) -> list[CheckResult]:
    return [
        _check_resource("azure_state.exadata_infrastructure", payload["exadata_infrastructure"]["azure_id"]),
        _check_resource("azure_state.vm_cluster", payload["vm_cluster"]["azure_id"]),
    ]
