#!/usr/bin/env python3
"""
Agent 4 — DB Bootstrap + TDE.

Reads agent4_input.json (produced by Agent 3), then:

  1. Validates input contract.
  2. Resolves SYS / TDE / wallet secrets from Azure Key Vault, env, or generates them (DEV).
  3. SSHes to node1 of the ODAA RAC.
  4. Pre-checks: dbaascli present, GI online, ASM/ACFS, oracle/grid users.
  5. Creates the CDB + first PDB via dbaascli (idempotent).
  6. Configures Data Guard pre-reqs: FORCE LOGGING, ARCHIVELOG, init params, SRLs, password file.
  7. Verifies / completes TDE: master key on PDB, autologin keystore.
  8. Provisions migration user inside the PDB.
  9. Runs end-to-end validation queries.
 10. Emits agent5_input.json (Data Guard handoff contract).

Exit codes:
  0  PASS — Agent 5 may proceed
  1  WARN — Agent 5 may proceed, review warnings
  2  FAIL — blocked, do not run Agent 5
  3  ERROR — orchestration error / unhandled exception
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
    precheck,
    create_cdb_idempotent,
    configure_tde,
    configure_dataguard_prep,
    create_migration_user,
    post_validate,
)
from steps.common import open_ssh

LOG_FMT = "%(asctime)s %(levelname)-7s %(name)s :: %(message)s"
SCHEMA_VERSION = "agent5-input/1.0"


# ---------------- aggregation helpers ----------------

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
        StepStatus.PASS: "ready",
        StepStatus.SKIP: "ready",
        StepStatus.WARN: "ready_with_warnings",
        StepStatus.FAIL: "blocked",
    }[status]


def _build_handoff(
    *,
    upstream: dict[str, Any],
    db_name: str,
    db_unique_name: str,
    pdb_name: str,
    standby_unique_name: str,
    migration_user: str,
    facts: dict[str, str],
    secrets_meta: dict[str, Any],
    overall: StepStatus,
    results: list[StepResult],
) -> dict[str, Any]:
    """Build the agent5_input.json document consumed by Agent 5 (Data Guard)."""
    upstream_directives = upstream["agent4_directives"]
    proceed = overall in (StepStatus.PASS, StepStatus.WARN)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "status": _summary_status(overall),
        "primary_database": {
            "db_name": db_name,
            "db_unique_name": db_unique_name or facts.get("db_unique_name", ""),
            "pdb_name": pdb_name,
            "version": facts.get("db_version", ""),
            "open_mode": facts.get("open_mode", ""),
            "is_rac": True,
            "is_cdb": True,
            "instance_count": int(facts.get("instance_count") or 0),
            "archivelog": facts.get("archivelog", ""),
            "force_logging": facts.get("force_logging", ""),
            "flashback_on": facts.get("flashback_on", ""),
            "tde": {
                "wallet_status": facts.get("wallet_status", ""),
                "wallet_type": facts.get("wallet_type", ""),
                "wallet_location": facts.get("wallet_location", ""),
                "master_key_id": facts.get("master_key_id", ""),
                "encrypted_tablespaces": int(facts.get("enc_tbs_count") or 0),
            },
            "standby_redo_logs": {
                "count": int(facts.get("srl_count") or 0),
                "groups": facts.get("srl_group_sizes", ""),
            },
            "password_file": {
                "mode": facts.get("pwfile_location", ""),
            },
        },
        "standby_target": {
            "db_unique_name": standby_unique_name,
            "service_hint": f"{standby_unique_name}.{upstream['upstream'].get('oracle_net', {}).get('service_domain', '')}".strip("."),
        },
        "ssh": {
            "node1_host": upstream_directives["ssh_target_node1"],
            "user": upstream_directives["ssh_user"],
            "private_key_path": upstream_directives.get("ssh_private_key_path"),
        },
        "scan": {
            "name": upstream_directives["use_scan_dns_name"],
            "tcp_port": upstream_directives.get("use_scan_port_tcp", 1521),
            "tcps_port": upstream_directives.get("use_scan_port_tcps"),
        },
        "secrets": {
            "source": secrets_meta.get("source"),
            "akv_vault_name": secrets_meta.get("akv_vault_name"),
            "akv_secret_names": secrets_meta.get("akv_secret_names") or {},
        },
        "migration_user": {
            "username": migration_user,
            "container": pdb_name,
        },
        "dataguard_directives": {
            "proceed": proceed,
            "broker_required": True,
            "protection_mode": "MAX_PERFORMANCE",
            "transport_mode": "ASYNC",
            "apply_mode": "REAL_TIME_APPLY",
            "fast_start_failover": False,
        },
        "agent4_steps": [r.to_dict() for r in results],
    }


# ---------------- main ----------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Agent 4 — DB Bootstrap + TDE")
    parser.add_argument("--input",  required=True, help="Path to agent4_input.json (from Agent 3)")
    parser.add_argument("--output", required=True, help="Path to write agent5_input.json")
    parser.add_argument("--config", required=True, help="Path to agent4 config (db/pdb names, AKV refs, etc.)")
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
    log = logging.getLogger("agent4")

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
            return _emit(output_path, all_results, facts, config={}, upstream={}, overall=StepStatus.FAIL,
                         exit_code=2)
        upstream = load_input(input_path)

        # --- Phase 2: load config ---
        if not config_path.exists():
            all_results.append(StepResult("config.load", StepStatus.FAIL, f"config not found: {config_path}"))
            return _emit(output_path, all_results, facts, config={}, upstream=upstream,
                         overall=StepStatus.FAIL, exit_code=2)
        config = json.loads(config_path.read_text())
        all_results.append(StepResult("config.load", StepStatus.PASS, f"config loaded ({len(config)} keys)"))

        db_name             = config["db_name"]
        db_unique_name      = config["db_unique_name"]
        standby_unique_name = config["standby_unique_name"]
        pdb_name            = config["pdb_name"]
        oracle_home_version = config["oracle_home_version"]
        migration_user      = config.get("migration_user", "MIGADM")
        srl_count           = int(config.get("standby_redo_log_count", 4))
        srl_size_mb         = int(config.get("standby_redo_log_size_mb", 1024))
        character_set          = config.get("character_set", "AL32UTF8")
        national_character_set = config.get("national_character_set", "AL16UTF16")

        # --- Phase 3: secrets ---
        r, secrets = resolve_secrets(config)
        all_results.append(r)
        if secrets is None:
            return _emit(output_path, all_results, facts, config=config, upstream=upstream,
                         overall=StepStatus.FAIL, exit_code=2)
        secrets_meta = {
            "source": secrets.source,
            "akv_vault_name": secrets.akv_vault_name,
            "akv_secret_names": secrets.akv_secret_names,
        }

        # --- Phase 4: SSH to node1 ---
        directives = upstream["agent4_directives"]
        log.info("Opening SSH to %s as %s", directives["ssh_target_node1"], directives["ssh_user"])
        try:
            cli = open_ssh(
                host=directives["ssh_target_node1"],
                user=directives["ssh_user"],
                key_path=directives.get("ssh_private_key_path"),
            )
        except Exception as e:
            all_results.append(StepResult("ssh.connect", StepStatus.FAIL, f"SSH failed: {e!r}"))
            return _emit(output_path, all_results, facts, config=config, upstream=upstream,
                         overall=StepStatus.FAIL, exit_code=2)
        all_results.append(StepResult("ssh.connect", StepStatus.PASS,
                                      f"connected to {directives['ssh_target_node1']}"))

        try:
            # --- Phase 5: prechecks ---
            pc = precheck(cli)
            all_results.extend(pc)
            if any(r.status == StepStatus.FAIL for r in pc):
                return _emit(output_path, all_results, facts, config=config, upstream=upstream,
                             overall=StepStatus.FAIL, exit_code=2,
                             extras={"db_name": db_name, "db_unique_name": db_unique_name,
                                     "pdb_name": pdb_name, "standby_unique_name": standby_unique_name,
                                     "migration_user": migration_user, "secrets_meta": secrets_meta})

            # --- Phase 6: create CDB (idempotent) ---
            r, _details = create_cdb_idempotent(
                cli,
                db_name=db_name,
                db_unique_name=db_unique_name,
                pdb_name=pdb_name,
                oracle_home_version=oracle_home_version,
                secrets=secrets,
                character_set=character_set,
                national_character_set=national_character_set,
                enable_archivelog=True,
            )
            all_results.append(r)
            if r.status == StepStatus.FAIL:
                return _emit(output_path, all_results, facts, config=config, upstream=upstream,
                             overall=StepStatus.FAIL, exit_code=2,
                             extras={"db_name": db_name, "db_unique_name": db_unique_name,
                                     "pdb_name": pdb_name, "standby_unique_name": standby_unique_name,
                                     "migration_user": migration_user, "secrets_meta": secrets_meta})

            # --- Phase 7: TDE ---
            tde_results, _wallet_info = configure_tde(
                cli, db_name=db_name, pdb_name=pdb_name, secrets=secrets,
            )
            all_results.extend(tde_results)

            # --- Phase 8: Data Guard prep ---
            dg_results = configure_dataguard_prep(
                cli,
                db_name=db_name,
                db_unique_name=db_unique_name,
                standby_unique_name=standby_unique_name,
                standby_count=srl_count,
                srl_size_mb=srl_size_mb,
            )
            all_results.extend(dg_results)

            # --- Phase 9: Migration user ---
            mig_result = create_migration_user(
                cli, db_name=db_name, pdb_name=pdb_name,
                migration_user=migration_user, secrets=secrets,
            )
            all_results.append(mig_result)

            # --- Phase 10: Validation ---
            val_results, facts = post_validate(cli, db_name=db_name, pdb_name=pdb_name)
            all_results.extend(val_results)

        finally:
            try:
                cli.close()
            except Exception:
                pass

        overall = _worst(all_results)
        return _emit(output_path, all_results, facts, config=config, upstream=upstream,
                     overall=overall, exit_code=_exit_code(overall),
                     extras={"db_name": db_name, "db_unique_name": db_unique_name,
                             "pdb_name": pdb_name, "standby_unique_name": standby_unique_name,
                             "migration_user": migration_user, "secrets_meta": secrets_meta})

    except Exception as e:
        log.error("Unhandled exception: %s\n%s", e, traceback.format_exc())
        all_results.append(StepResult("agent4.unhandled", StepStatus.FAIL, f"unhandled exception: {e!r}"))
        try:
            _emit(output_path, all_results, facts, config={}, upstream={},
                  overall=StepStatus.FAIL, exit_code=3)
        except Exception:
            pass
        return 3


def _emit(
    output_path: Path,
    results: list[StepResult],
    facts: dict[str, str],
    *,
    config: dict[str, Any],
    upstream: dict[str, Any],
    overall: StepStatus,
    exit_code: int,
    extras: dict[str, Any] | None = None,
) -> int:
    extras = extras or {}
    if upstream and config:
        doc = _build_handoff(
            upstream=upstream,
            db_name=extras.get("db_name", config.get("db_name", "")),
            db_unique_name=extras.get("db_unique_name", config.get("db_unique_name", "")),
            pdb_name=extras.get("pdb_name", config.get("pdb_name", "")),
            standby_unique_name=extras.get("standby_unique_name", config.get("standby_unique_name", "")),
            migration_user=extras.get("migration_user", config.get("migration_user", "MIGADM")),
            facts=facts,
            secrets_meta=extras.get("secrets_meta") or {},
            overall=overall,
            results=results,
        )
    else:
        doc = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "status": _summary_status(overall),
            "agent4_steps": [r.to_dict() for r in results],
            "dataguard_directives": {"proceed": False},
        }

    output_path.write_text(json.dumps(doc, indent=2))
    print(f"\n[agent4] wrote {output_path}  status={doc['status']}  steps={len(results)}", file=sys.stderr)
    n_fail = sum(1 for r in results if r.status == StepStatus.FAIL)
    n_warn = sum(1 for r in results if r.status == StepStatus.WARN)
    n_pass = sum(1 for r in results if r.status == StepStatus.PASS)
    print(f"[agent4] PASS={n_pass} WARN={n_warn} FAIL={n_fail}  exit={exit_code}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
