#!/usr/bin/env python3
"""
Agent 5 — Data Guard.

Reads agent5_input.json (produced by Agent 4), then:

  1.  Validates input contract (schema + cross-field assertions).
  2.  Resolves SYS / TDE / wallet secrets from AKV (or env override).
  3.  SSHes to primary node1 AND standby node1.
  4.  Pre-checks both clusters.
  5.  Prepares the standby:
        a. Copies TDE wallet (ewallet.p12 + cwallet.sso) to standby.
        b. Copies orapw password file from primary ASM to standby ASM.
        c. Stages init.ora and starts the standby instance NOMOUNT.
        d. Adds a static SID_LIST entry to the standby listener.
  6.  Runs RMAN DUPLICATE TARGET DATABASE FOR STANDBY FROM ACTIVE DATABASE.
  7.  Registers the standby with Clusterware (srvctl).
  8.  Configures the Data Guard Broker (CREATE CONFIGURATION + EDIT modes + ENABLE).
  9.  Waits for redo apply to catch up (Transport/Apply Lag bounded, Status=SUCCESS).
 10.  Runs end-to-end validation (primary + standby SQL, dgmgrl VALIDATE DATABASE).
 11.  Emits dataguard_state.json — the final pipeline output.

Exit codes:
  0  PASS — Data Guard fully configured and synchronized.
  1  WARN — DG configured, but with non-blocking warnings (e.g., apply lag).
  2  FAIL — blocked.
  3  ERROR — unhandled orchestration exception.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
import traceback
from pathlib import Path
from typing import Any

from steps import (
    StepResult, StepStatus,
    validate_input, load_input,
    resolve_secrets,
    precheck_primary, precheck_standby,
    prepare_standby,
    rman_duplicate_for_standby, register_standby_with_srvctl,
    configure_broker, wait_for_apply,
    post_validate,
    open_ssh,
)

LOG_FMT = "%(asctime)s %(levelname)-7s %(name)s :: %(message)s"
SCHEMA_VERSION = "dataguard-state/1.0"


# ---------------- aggregation ----------------

def _worst(results: list[StepResult]) -> StepStatus:
    order = {StepStatus.PASS: 0, StepStatus.SKIP: 0, StepStatus.WARN: 1, StepStatus.FAIL: 2}
    worst = StepStatus.PASS
    for r in results:
        if order[r.status] > order[worst]:
            worst = r.status
    return worst


def _exit_code(status: StepStatus) -> int:
    return {StepStatus.PASS: 0, StepStatus.SKIP: 0, StepStatus.WARN: 1, StepStatus.FAIL: 2}[status]


def _summary_status(status: StepStatus) -> str:
    return {
        StepStatus.PASS: "synchronized",
        StepStatus.SKIP: "synchronized",
        StepStatus.WARN: "synchronized_with_warnings",
        StepStatus.FAIL: "blocked",
    }[status]


def _build_state(
    *,
    upstream: dict[str, Any],
    config: dict[str, Any],
    overall: StepStatus,
    facts: dict[str, str],
    results: list[StepResult],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "status": _summary_status(overall),
        "configuration": {
            "name": f"{upstream['primary_database']['db_name']}_DG",
            "protection_mode": upstream["dataguard_directives"].get("protection_mode", "MAX_PERFORMANCE"),
            "transport_mode": upstream["dataguard_directives"].get("transport_mode", "ASYNC"),
            "apply_mode": upstream["dataguard_directives"].get("apply_mode", "REAL_TIME_APPLY"),
            "fast_start_failover": upstream["dataguard_directives"].get("fast_start_failover", False),
        },
        "primary": {
            "db_name": upstream["primary_database"]["db_name"],
            "db_unique_name": upstream["primary_database"]["db_unique_name"],
            "role": facts.get("pri_role", ""),
            "open_mode": facts.get("pri_open_mode", ""),
            "protection_level": facts.get("pri_protection_level", ""),
            "ssh_host": upstream["ssh"]["node1_host"],
            "scan_name": upstream["scan"]["name"],
            "tns_alias": config["primary_tns"],
        },
        "standby": {
            "db_unique_name": upstream["standby_target"]["db_unique_name"],
            "role": facts.get("stby_role", ""),
            "open_mode": facts.get("stby_open_mode", ""),
            "mrp_status": facts.get("stby_mrp_status", ""),
            "wallet_status": facts.get("stby_wallet_status", ""),
            "ssh_host": config["standby"]["ssh_node1_host"],
            "scan_name": config["standby"]["scan_name"],
            "tns_alias": config["standby_tns"],
        },
        "redo_apply": {
            "primary_last_sequence": int(facts.get("pri_last_sequence") or 0),
            "standby_last_received_sequence": int(facts.get("stby_last_received_seq") or 0),
            "standby_last_applied_sequence": int(facts.get("stby_last_applied_seq") or 0),
            "gap_count": int(facts.get("pri_gap_count") or 0),
            "delay_minutes": float(facts.get("stby_delay_mins") or 0),
            "ready_for_switchover": facts.get("ready_for_switchover", "no") == "yes",
        },
        "agent5_steps": [r.to_dict() for r in results],
    }


# ---------------- main ----------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Agent 5 — Data Guard")
    parser.add_argument("--input",  required=True, help="Path to agent5_input.json (from Agent 4)")
    parser.add_argument("--output", required=True, help="Path to write dataguard_state.json")
    parser.add_argument("--config", required=True, help="Path to agent5 config (standby SSH, TNS aliases, sizing)")
    parser.add_argument("--log-file", default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format=LOG_FMT,
        handlers=[h for h in [
            logging.StreamHandler(sys.stderr),
            logging.FileHandler(args.log_file) if args.log_file else None,
        ] if h is not None],
    )
    log = logging.getLogger("agent5")

    input_path = Path(args.input)
    output_path = Path(args.output)
    config_path = Path(args.config)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    all_results: list[StepResult] = []
    facts: dict[str, str] = {}

    try:
        # --- Phase 1: input contract ---
        r = validate_input(input_path)
        all_results.append(r)
        if r.status == StepStatus.FAIL:
            return _emit(output_path, all_results, facts, upstream={}, config={},
                         overall=StepStatus.FAIL, exit_code=2)
        upstream = load_input(input_path)

        # --- Phase 2: load config ---
        if not config_path.exists():
            all_results.append(StepResult("config.load", StepStatus.FAIL, f"config not found: {config_path}"))
            return _emit(output_path, all_results, facts, upstream=upstream, config={},
                         overall=StepStatus.FAIL, exit_code=2)
        config = json.loads(config_path.read_text())
        all_results.append(StepResult("config.load", StepStatus.PASS, f"config loaded ({len(config)} keys)"))

        db_name              = upstream["primary_database"]["db_name"]
        primary_db_unique    = upstream["primary_database"]["db_unique_name"]
        standby_db_unique    = upstream["standby_target"]["db_unique_name"]
        instance_count       = upstream["primary_database"].get("instance_count", 2)
        primary_wallet_path  = upstream["primary_database"]["tde"]["wallet_location"]
        oracle_home_version  = config.get("oracle_home_version", "19.22.0.0")
        primary_tns          = config["primary_tns"]
        standby_tns          = config["standby_tns"]
        standby_wallet_path  = config["standby"]["wallet_location"]
        rman_parallelism     = int(config.get("rman_parallelism", 8))
        rman_timeout_s       = int(config.get("rman_timeout_seconds", 14400))

        # --- Phase 3: secrets ---
        r, secrets = resolve_secrets(upstream.get("secrets") or {})
        all_results.append(r)
        if secrets is None:
            return _emit(output_path, all_results, facts, upstream=upstream, config=config,
                         overall=StepStatus.FAIL, exit_code=2)

        # --- Phase 4: SSH to both clusters ---
        try:
            log.info("Opening SSH to primary %s", upstream["ssh"]["node1_host"])
            pri = open_ssh(
                host=upstream["ssh"]["node1_host"],
                user=upstream["ssh"]["user"],
                key_path=upstream["ssh"].get("private_key_path"),
            )
            all_results.append(StepResult("ssh.primary", StepStatus.PASS,
                                          f"connected to primary {upstream['ssh']['node1_host']}"))
        except Exception as e:
            all_results.append(StepResult("ssh.primary", StepStatus.FAIL, f"SSH failed: {e!r}"))
            return _emit(output_path, all_results, facts, upstream=upstream, config=config,
                         overall=StepStatus.FAIL, exit_code=2)

        try:
            log.info("Opening SSH to standby %s", config["standby"]["ssh_node1_host"])
            stby = open_ssh(
                host=config["standby"]["ssh_node1_host"],
                user=config["standby"]["ssh_user"],
                key_path=config["standby"].get("ssh_private_key_path"),
            )
            all_results.append(StepResult("ssh.standby", StepStatus.PASS,
                                          f"connected to standby {config['standby']['ssh_node1_host']}"))
        except Exception as e:
            all_results.append(StepResult("ssh.standby", StepStatus.FAIL, f"SSH failed: {e!r}"))
            try:
                pri.close()
            except Exception:
                pass
            return _emit(output_path, all_results, facts, upstream=upstream, config=config,
                         overall=StepStatus.FAIL, exit_code=2)

        try:
            # --- Phase 5: prechecks (parallel-ish) ---
            pc_pri = precheck_primary(pri, db_name=db_name)
            pc_stby = precheck_standby(stby, expected_db_unique_name=standby_db_unique)
            all_results.extend(pc_pri)
            all_results.extend(pc_stby)
            if any(r.status == StepStatus.FAIL for r in pc_pri + pc_stby):
                return _emit(output_path, all_results, facts, upstream=upstream, config=config,
                             overall=StepStatus.FAIL, exit_code=2)

            # --- Phase 6: prepare standby ---
            prep = prepare_standby(
                pri, stby,
                db_name=db_name,
                primary_db_unique_name=primary_db_unique,
                standby_db_unique_name=standby_db_unique,
                primary_wallet_path=primary_wallet_path,
                standby_wallet_path=standby_wallet_path,
                standby_listener_host=config["standby"]["ssh_node1_host"],
            )
            all_results.extend(prep)
            if any(r.status == StepStatus.FAIL for r in prep):
                return _emit(output_path, all_results, facts, upstream=upstream, config=config,
                             overall=StepStatus.FAIL, exit_code=2)

            # --- Phase 7: RMAN DUPLICATE ---
            r = rman_duplicate_for_standby(
                pri,
                db_name=db_name,
                primary_db_unique_name=primary_db_unique,
                standby_db_unique_name=standby_db_unique,
                primary_tns=primary_tns,
                standby_tns=standby_tns,
                sys_password=secrets.sys_password,
                parallelism=rman_parallelism,
                timeout_s=rman_timeout_s,
            )
            all_results.append(r)
            if r.status == StepStatus.FAIL:
                return _emit(output_path, all_results, facts, upstream=upstream, config=config,
                             overall=StepStatus.FAIL, exit_code=2)

            # --- Phase 8: srvctl register ---
            r = register_standby_with_srvctl(
                stby,
                db_name=db_name,
                standby_db_unique_name=standby_db_unique,
                oracle_home_version=oracle_home_version,
                instance_count=instance_count,
            )
            all_results.append(r)

            # --- Phase 9: Broker ---
            br = configure_broker(
                pri,
                db_name=db_name,
                primary_db_unique_name=primary_db_unique,
                standby_db_unique_name=standby_db_unique,
                primary_tns=primary_tns,
                standby_tns=standby_tns,
                sys_password=secrets.sys_password,
                protection_mode=upstream["dataguard_directives"].get("protection_mode", "MAX_PERFORMANCE"),
                transport_mode=upstream["dataguard_directives"].get("transport_mode", "ASYNC"),
                apply_mode=upstream["dataguard_directives"].get("apply_mode", "REAL_TIME_APPLY"),
            )
            all_results.extend(br)
            if any(r.status == StepStatus.FAIL for r in br):
                return _emit(output_path, all_results, facts, upstream=upstream, config=config,
                             overall=StepStatus.FAIL, exit_code=2)

            # --- Phase 10: wait for apply ---
            r = wait_for_apply(
                pri,
                db_name=db_name,
                standby_db_unique_name=standby_db_unique,
                primary_tns=primary_tns,
                sys_password=secrets.sys_password,
                max_wait_s=int(config.get("apply_wait_seconds", 1800)),
            )
            all_results.append(r)

            # --- Phase 11: validation ---
            val_results, facts = post_validate(
                pri, stby,
                db_name=db_name,
                primary_db_unique_name=primary_db_unique,
                standby_db_unique_name=standby_db_unique,
                primary_tns=primary_tns,
                sys_password=secrets.sys_password,
            )
            all_results.extend(val_results)

        finally:
            for c in (pri, stby):
                try:
                    c.close()
                except Exception:
                    pass

        overall = _worst(all_results)
        return _emit(output_path, all_results, facts, upstream=upstream, config=config,
                     overall=overall, exit_code=_exit_code(overall))

    except Exception as e:
        log.error("Unhandled exception: %s\n%s", e, traceback.format_exc())
        all_results.append(StepResult("agent5.unhandled", StepStatus.FAIL, f"unhandled exception: {e!r}"))
        try:
            _emit(output_path, all_results, facts, upstream={}, config={},
                  overall=StepStatus.FAIL, exit_code=3)
        except Exception:
            pass
        return 3


def _emit(
    output_path: Path,
    results: list[StepResult],
    facts: dict[str, str],
    *,
    upstream: dict[str, Any],
    config: dict[str, Any],
    overall: StepStatus,
    exit_code: int,
) -> int:
    if upstream and config:
        doc = _build_state(upstream=upstream, config=config, overall=overall, facts=facts, results=results)
    else:
        doc = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "status": _summary_status(overall),
            "agent5_steps": [r.to_dict() for r in results],
        }

    output_path.write_text(json.dumps(doc, indent=2))
    n_fail = sum(1 for r in results if r.status == StepStatus.FAIL)
    n_warn = sum(1 for r in results if r.status == StepStatus.WARN)
    n_pass = sum(1 for r in results if r.status == StepStatus.PASS)
    print(f"\n[agent5] wrote {output_path}  status={doc['status']}  steps={len(results)}", file=sys.stderr)
    print(f"[agent5] PASS={n_pass} WARN={n_warn} FAIL={n_fail}  exit={exit_code}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
