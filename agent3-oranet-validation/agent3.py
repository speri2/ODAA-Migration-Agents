#!/usr/bin/env python3
"""
Agent 3 — Oracle Net Connectivity Validation
=============================================

Consumes:  agent3_input.json (produced by Agent 2)
Produces:  agent4_input.json (consumed by Agent 4 — DB bootstrap + TDE)

Validation phases (executed in order):
  1. Input contract validation (JSON Schema)
  2. Azure resource state (lifecycleState == Available)
  3. DNS resolution (SCAN must yield 3 A records)
  4. TCP reachability (SCAN 1521 / 2484, node SSH/22)
  5. Oracle Net TNS handshake (real Connect packet, parses Accept/Refuse/Resend)
  6. SSH reachability (each node, opc user)
  7. GI / CRS / SCAN listener / ASM cluster health (over SSH)

Exit codes:
   0  all checks pass (status=ready)
   1  warnings only — proceed with caution (status=ready_with_warnings)
   2  one or more failures — Agent 4 MUST NOT run (status=blocked)
   3  bad CLI usage / unhandled error
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from checks import (
    CheckResult, Severity,
    validate_input, check_azure_state, check_dns,
    check_tcp_ports, check_oracle_net_handshake,
    check_ssh_reachability, check_cluster_health,
)
from checks.input_validation import load_input

LOG_FORMAT = "%(asctime)s %(levelname)-5s %(name)s %(message)s"


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=LOG_FORMAT,
        stream=sys.stderr,
    )


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def aggregate_status(results: list[CheckResult]) -> tuple[str, int]:
    failed = [r for r in results if r.severity is Severity.FAIL]
    warned = [r for r in results if r.severity is Severity.WARN]
    if failed:
        return "blocked", 2
    if warned:
        return "ready_with_warnings", 1
    return "ready", 0


def emit_console(results: list[CheckResult]) -> None:
    width = max((len(r.name) for r in results), default=10)
    for r in results:
        sym = {"pass": "✔", "warn": "!", "fail": "✘", "skip": "·"}[r.severity.value]
        print(f"  {sym} {r.name.ljust(width)}  {r.severity.value:<4}  {r.summary}", file=sys.stderr)


def build_handoff(input_doc: dict, results: list[CheckResult], status: str) -> dict:
    return {
        "schema_version": "1.0",
        "generated_at": utcnow_iso(),
        "agent": "agent3-oranet-validation",
        "next_agent": "agent4-db-bootstrap-tde",
        "status": status,
        "upstream": {
            "azure": input_doc["azure"],
            "exadata_infrastructure": input_doc["exadata_infrastructure"],
            "vm_cluster": input_doc["vm_cluster"],
            "network": input_doc["network"],
            "oracle_net": input_doc["oracle_net"],
            "ssh": {k: v for k, v in input_doc["ssh"].items() if k != "authorized_keys"},
        },
        "validation": {
            "summary": {
                "total":   len(results),
                "passed":  sum(1 for r in results if r.severity is Severity.PASS),
                "warned":  sum(1 for r in results if r.severity is Severity.WARN),
                "failed":  sum(1 for r in results if r.severity is Severity.FAIL),
                "skipped": sum(1 for r in results if r.severity is Severity.SKIP),
                "duration_ms": sum(r.elapsed_ms for r in results),
            },
            "results": [r.to_dict() for r in results],
        },
        "agent4_directives": {
            "use_scan_dns_name": input_doc["oracle_net"]["scan_dns_name"],
            "use_scan_port_tcp": input_doc["oracle_net"]["scan_listener_port_tcp"],
            "use_scan_port_tcps": input_doc["oracle_net"].get("scan_listener_port_tcp_ssl"),
            "ssh_target_node1": (
                input_doc["vm_cluster"]["hostname_prefix"] + "1"
                + ((".") + input_doc["vm_cluster"]["domain"] if input_doc["vm_cluster"].get("domain") else "")
            ),
            "ssh_user": input_doc["ssh"]["os_user"],
            "ssh_private_key_path": input_doc["ssh"].get("private_key_path"),
            "proceed": status in ("ready", "ready_with_warnings"),
        },
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Agent 3 — Oracle Net validation")
    p.add_argument("--input",  default="agent3_input.json", help="path to upstream contract")
    p.add_argument("--output", default="agent4_input.json", help="path to write downstream contract")
    p.add_argument("--report", default="agent3_report.json", help="full validation report (always written)")
    p.add_argument("--allow-warnings", action="store_true",
                   help="treat warnings as non-blocking (still exits 1 unless --strict-pass)")
    p.add_argument("--strict-pass", action="store_true",
                   help="exit 0 only when every check passes (no warnings/skips)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    setup_logging(args.verbose)

    log = logging.getLogger("agent3")
    in_path = Path(args.input)

    # Phase 1 — input contract
    contract_result = validate_input(in_path)
    results: list[CheckResult] = [contract_result]
    if contract_result.severity is Severity.FAIL:
        log.error("Input contract invalid; aborting before live checks.")
        Path(args.report).write_text(json.dumps({
            "status": "blocked",
            "validation": {"results": [r.to_dict() for r in results]},
        }, indent=2))
        emit_console(results)
        return 2

    payload = load_input(in_path)

    # Phase 2 — Azure state (sequential, fast, both resources)
    log.info("Phase 2: Azure resource state")
    results.extend(check_azure_state(payload))

    # Phase 3 — DNS
    log.info("Phase 3: DNS resolution")
    results.extend(check_dns(payload))

    # Phase 4 — TCP probes
    log.info("Phase 4: TCP reachability")
    results.extend(check_tcp_ports(payload))

    # Phase 5 — Oracle Net handshake
    log.info("Phase 5: Oracle Net TNS handshake")
    results.extend(check_oracle_net_handshake(payload))

    # Phase 6 — SSH reachability
    log.info("Phase 6: SSH reachability")
    results.extend(check_ssh_reachability(payload))

    # Phase 7 — Cluster health (only if at least one SSH PASS)
    if any(r.name.startswith("ssh.") and r.severity is Severity.PASS for r in results):
        log.info("Phase 7: GI / CRS / ASM cluster health")
        results.extend(check_cluster_health(payload))
    else:
        log.warning("Skipping cluster_health phase — no node SSH succeeded.")
        results.append(CheckResult("cluster_health", Severity.SKIP, "no SSH PASS on any node"))

    status, exit_code = aggregate_status(results)
    handoff = build_handoff(payload, results, status)

    Path(args.report).write_text(json.dumps(handoff, indent=2, default=str))
    if status != "blocked":
        Path(args.output).write_text(json.dumps(handoff, indent=2, default=str))
        log.info("Handoff for Agent 4 written: %s", args.output)
    else:
        # Don't emit a downstream contract when blocked — explicit gate
        if Path(args.output).exists():
            Path(args.output).unlink()
        log.error("Status=blocked; Agent 4 contract NOT written.")

    emit_console(results)
    print(f"\nstatus={status}  total={len(results)}  "
          f"pass={sum(1 for r in results if r.severity is Severity.PASS)}  "
          f"warn={sum(1 for r in results if r.severity is Severity.WARN)}  "
          f"fail={sum(1 for r in results if r.severity is Severity.FAIL)}  "
          f"skip={sum(1 for r in results if r.severity is Severity.SKIP)}",
          file=sys.stderr)

    if args.strict_pass and any(r.severity is not Severity.PASS for r in results):
        return 2
    if args.allow_warnings and exit_code == 1:
        return 0
    return exit_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        logging.exception("agent3 unhandled error: %s", e)
        sys.exit(3)
