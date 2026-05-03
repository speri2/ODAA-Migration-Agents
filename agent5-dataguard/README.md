# Agent 5 — Data Guard Configuration

Production-grade orchestrator that turns a freshly-bootstrapped primary (Agent 4 output) into a fully-synchronized Oracle Data Guard pair on Oracle Database @ Azure (ODAA) Exadata.

It performs **RMAN DUPLICATE TARGET DATABASE FOR STANDBY FROM ACTIVE DATABASE**, registers the standby with Clusterware, configures the Data Guard Broker, waits for redo apply to catch up, and emits a `dataguard_state.json` describing the final pair.

---

## Pipeline position

```
Agent 1 → Agent 2 → Agent 3 → Agent 4 → ┌─ Agent 5 (this) ─┐ → migration cutover
intake   terraform  net-test  bootstrap   data guard pair       (out of scope)
```

- **Input:**  `agent5_input.json`  produced by Agent 4 (`agent4_handoff.json`)
- **Config:** `agent5.config.json` (standby SSH coords, TNS aliases, RMAN tuning)
- **Output:** `dataguard_state.json` (schema `dataguard-state/1.0`)

---

## What it does (11 phases)

| # | Phase                          | Purpose                                                             |
|---|--------------------------------|---------------------------------------------------------------------|
| 1 | `validate_input`               | JSON Schema + cross-field assertions on Agent 4's handoff           |
| 2 | `config.load`                  | Load standby SSH coords, TNS aliases, RMAN tuning                   |
| 3 | `resolve_secrets`              | Pull SYS / TDE / wallet passwords from Azure Key Vault              |
| 4 | `ssh.primary` + `ssh.standby`  | Open paramiko sessions to **both** clusters' node1                  |
| 5 | `precheck_*`                   | GI up, ASM diskgroups present, primary DB open, standby slot empty  |
| 6 | `prepare_standby`              | Copy TDE wallet, copy orapw, stage init.ora, NOMOUNT, static lsnr   |
| 7 | `rman_duplicate_for_standby`   | Active duplicate, 8 primary + 4 auxiliary channels, compressed      |
| 8 | `register_standby_with_srvctl` | `srvctl add database -role PHYSICAL_STANDBY -startoption MOUNT`     |
| 9 | `configure_broker`             | CREATE CONFIGURATION → set modes → ENABLE                           |
|10 | `wait_for_apply`               | Poll `SHOW DATABASE VERBOSE` until Status=SUCCESS, lags bounded     |
|11 | `post_validate`                | Primary + standby SQL markers + `VALIDATE DATABASE VERBOSE`         |

Each phase emits one or more `StepResult(name, status, detail)` rows; the worst severity becomes the final exit code.

---

## Layout

```
agent5-dataguard/
├── agent5.py                       # 11-phase orchestrator
├── requirements.txt
├── scripts/
│   └── run.sh                      # venv launcher
├── steps/
│   ├── common.py                   # SSH primitives, sqlplus/dgmgrl/rman runners
│   ├── input_validation.py         # JSON Schema + asserts
│   ├── secrets.py                  # AKV resolver (env-var fallback)
│   ├── precheck.py                 # primary + standby readiness
│   ├── prepare_standby.py          # wallet/orapw copy, NOMOUNT, static listener
│   ├── rman_duplicate.py           # active duplicate + srvctl register
│   ├── configure_broker.py         # idempotent dgmgrl + apply wait
│   └── post_validate.py            # SQL markers + VALIDATE DATABASE
├── sql/
│   ├── 01_standby_init_params.sql  # bare-minimum NOMOUNT pfile
│   ├── 02_rman_duplicate.rman      # DUPLICATE … FOR STANDBY FROM ACTIVE …
│   ├── 03_post_validate_primary.sql
│   └── 04_post_validate_standby.sql
├── dgmgrl/
│   ├── 01_create_configuration.dgmgrl
│   └── 02_set_modes.dgmgrl
└── samples/
    ├── agent5_input.sample.json
    └── agent5.config.sample.json
```

---

## Running

```bash
cd /opt/odaa/agent5-dataguard
AGENT5_INPUT=/var/agent4/agent4_handoff.json \
AGENT5_CONFIG=./samples/agent5.config.sample.json \
AGENT5_OUTPUT=/var/agent5/dataguard_state.json \
./scripts/run.sh
```

The launcher creates a venv at `.venv/`, installs `requirements.txt`, then invokes `agent5.py` with the resolved paths.

### Exit codes

| Code | Meaning      | `dataguard_state.json` `status` |
|------|--------------|---------------------------------|
| 0    | PASS         | `synchronized`                  |
| 1    | WARN         | `synchronized_with_warnings`    |
| 2    | FAIL         | `blocked`                       |
| 3    | ERROR        | `blocked` (unhandled exception) |

---

## Output contract — `dataguard_state.json`

```jsonc
{
  "schema_version": "dataguard-state/1.0",
  "generated_at": "2026-05-02T23:11:08+00:00",
  "status": "synchronized",
  "configuration": {
    "name": "MYCDB_DG",
    "protection_mode": "MAX_PERFORMANCE",
    "transport_mode": "ASYNC",
    "apply_mode": "REAL_TIME_APPLY",
    "fast_start_failover": false
  },
  "primary": {
    "db_name": "MYCDB",
    "db_unique_name": "MYCDB_PRI",
    "role": "PRIMARY",
    "open_mode": "READ WRITE",
    "protection_level": "MAXIMUM PERFORMANCE",
    "ssh_host": "exadb-pri-node1.contoso.internal",
    "scan_name": "exadb-pri-scan.contoso.internal",
    "tns_alias": "MYCDB_PRI"
  },
  "standby": {
    "db_unique_name": "MYCDB_STBY",
    "role": "PHYSICAL STANDBY",
    "open_mode": "MOUNTED",
    "mrp_status": "APPLYING_LOG",
    "wallet_status": "OPEN",
    "ssh_host": "exadb-stby-node1.contoso.internal",
    "scan_name": "exadb-stby-scan.contoso.internal",
    "tns_alias": "MYCDB_STBY"
  },
  "redo_apply": {
    "primary_last_sequence": 4217,
    "standby_last_received_sequence": 4217,
    "standby_last_applied_sequence": 4217,
    "gap_count": 0,
    "delay_minutes": 0.2,
    "ready_for_switchover": true
  },
  "agent5_steps": [ /* full audit trail */ ]
}
```

---

## Security notes

- **SYS / TDE / wallet passwords** are resolved at runtime from Azure Key Vault using `DefaultAzureCredential` (managed identity → CLI → service principal). Never logged, never written to disk.
- Secrets are passed to `sqlplus` / `rman` / `dgmgrl` via the `ORA_SYS_PW` environment variable inside the remote `sudo -iu oracle bash -lc` heredoc, so they never appear in `ps -ef`.
- All paramiko SSH sessions use key-based auth (no password auth, host key check via `~/.ssh/known_hosts`).
- The TDE wallet is copied to the standby via `tar | sftp`; the staging tarball is removed from `/tmp` on both nodes after extraction.
- The orapw file is round-tripped through ASM (`asmcmd cp`); the staging file in `/tmp` is removed.

---

## Idempotency

Every phase is safe to re-run.

- `precheck_standby` accepts both "DB exists in cluster registry but in MOUNT state" and "DB does not exist yet" as valid starting states.
- `configure_broker` checks `SHOW CONFIGURATION` for `ORA-16532` and skips `CREATE CONFIGURATION` when the broker config already exists.
- `prepare_standby` skips wallet/orapw copy if the target files already match (size + mtime check).
- RMAN `DUPLICATE` uses `NOFILENAMECHECK` and will not crash if datafiles already exist at the standby paths — but you should clean `+DATA/MYCDB_STBY` before re-running for safety.

---

## Failure modes & recovery

| Symptom                                          | Likely cause                                       | Action                                                |
|--------------------------------------------------|----------------------------------------------------|-------------------------------------------------------|
| `precheck_primary.dg_broker` FAIL                | Agent 4 didn't set `dg_broker_start=TRUE`          | Re-run Agent 4 phase 4 (`04_dataguard_params.sql`)    |
| `prepare_standby.wallet` FAIL                    | `ewallet.p12` missing on primary or AKV unreachable| Open primary wallet, or fix AKV access                |
| `rman_duplicate` FAIL with `ORA-19914`           | TDE keys not present on standby                    | Re-run `prepare_standby.wallet`, retry duplicate      |
| `configure_broker.enable` FAIL with `ORA-16607`  | Standby not reachable from primary by TNS          | Re-run Agent 3 connectivity validator                 |
| `wait_for_apply` WARN (lag bounded but non-zero) | Network latency or under-sized SRLs                | Inspect `delay_minutes`; raise SRL count if persistent|
| `post_validate.standby_role` FAIL                | Duplicate succeeded but DB opened READ WRITE       | `srvctl stop database` + restart in MOUNT             |

---

## Bindings used by the SQL / DGMGRL templates

`steps/common.py::render_template()` substitutes the following placeholders:

| Token                     | Source                                              |
|---------------------------|-----------------------------------------------------|
| `${DB_NAME}`              | `upstream.primary_database.db_name`                 |
| `${PRIMARY_UNIQUE_NAME}`  | `upstream.primary_database.db_unique_name`          |
| `${STANDBY_UNIQUE_NAME}`  | `upstream.standby_target.db_unique_name`            |
| `${PRIMARY_TNS}`          | `config.primary_tns`                                |
| `${STANDBY_TNS}`          | `config.standby_tns`                                |
| `${CFG_NAME}`             | `${DB_NAME}_DG`                                     |
| `${PROTECTION_MODE}`      | `upstream.dataguard_directives.protection_mode`     |
| `${TRANSPORT_MODE}`       | `upstream.dataguard_directives.transport_mode`      |
| `${APPLY_MODE}`           | `upstream.dataguard_directives.apply_mode`          |
| `${WALLET_ROOT}`          | `config.standby.wallet_root`                        |
| `${RMAN_PARALLELISM}`     | `config.rman_parallelism`                           |

---

## Minimum required Agent 4 output for Agent 5 to start

| Field                                              | Required value          |
|----------------------------------------------------|-------------------------|
| `primary_database.archivelog`                      | `"ARCHIVELOG"`          |
| `primary_database.force_logging`                   | `"YES"`                 |
| `primary_database.tde.wallet_status`               | `"OPEN"` or `"OPEN_NO_MASTER_KEY"` |
| `primary_database.standby_redo_logs.count`         | `>= 4`                  |
| `primary_database.tde.wallet_location`             | absolute path           |
| `ssh.node1_host` / `ssh.user`                      | populated               |

The input schema validator will FAIL fast with a clear message if any of these are missing or wrong.

---

## What this does **not** do

- Switchover / failover (`SWITCHOVER TO …`) — kept manual on purpose.
- Application service relocation (`srvctl modify service`).
- Active Data Guard (read-only standby OPEN). The standby remains MOUNTED for redo apply only.
- Standby cleanup if duplicate fails halfway. Re-run `prepare_standby` after manually clearing `+DATA/MYCDB_STBY` and `+RECO/MYCDB_STBY`.
