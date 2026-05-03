# Agent 4 — DB Bootstrap + TDE

Production-grade orchestrator that takes a freshly-deployed ODAA Exadata VM Cluster (validated by Agent 3) and brings up a Container Database with an initial PDB, TDE master keys, and all the Data Guard pre-requisites needed by Agent 5.

## Pipeline position

```
Agent 1 (intake)  →  Agent 2 (Terraform)  →  Agent 3 (Oracle Net)  →  [Agent 4]  →  Agent 5 (Data Guard)
                                                                       ↑
                                                          agent4_input.json (this agent's input)
                                                                       ↓
                                                          agent5_input.json (this agent's output)
```

## What it does

| Phase | Step | Notes |
|------:|------|-------|
| 1 | `input_contract`   | Validates `agent4_input.json` against a JSON Schema; refuses to run if `agent4_directives.proceed = false`. |
| 2 | `config.load`      | Loads agent config (db_name, pdb_name, AKV refs, SRL sizing). |
| 3 | `secrets.resolve`  | Pulls SYS / TDE / wallet passwords from Azure Key Vault, then env vars, then (DEV only) generates strong passwords. |
| 4 | `ssh.connect`      | SSH to RAC node1 using the directives from Agent 3. |
| 5 | `precheck.*`       | dbaascli present, GI/CRS online, ASM DATA disk group, ACFS mount, oracle/grid users. |
| 6 | `create_cdb`       | Idempotent: `dbaascli database getDetails` → if missing, `dbaascli database create --enableTDE true --enableArchiveLog true` and poll the job. |
| 7 | `tde.*`            | Confirms wallet OPEN at CDB$ROOT, sets PDB master key with `WITH BACKUP`, ensures autologin keystore (`cwallet.sso`). |
| 8 | `dg.*`             | FORCE LOGGING, ARCHIVELOG check, standby redo logs (≥ 4 per thread, sized ≥ ORL), Data Guard init params, `remote_login_passwordfile=EXCLUSIVE`, `srvctl modify database -pwfile`. |
| 9 | `migration_user`   | Creates the migration user inside the PDB with the privileges Data Pump and RMAN need. |
| 10 | `post_validate.*` | Runs the validation SQL, parses `AGENT4_KV>>KEY=value<<AGENT4_KV` markers into structured facts, asserts the load-bearing ones (archivelog, force_logging, wallet OPEN, PDB READ WRITE, SRL count). |

## Input — `agent4_input.json`

Produced by Agent 3. Required keys:

* `schema_version` — must be set
* `status` — `ready` or `ready_with_warnings`
* `upstream.azure`, `upstream.vm_cluster`, `upstream.oracle_net`, `upstream.ssh`
* `agent4_directives.proceed` — must be `true`
* `agent4_directives.ssh_target_node1`, `ssh_user`
* `agent4_directives.use_scan_dns_name`, `use_scan_port_tcp`

Sample at `samples/agent4_input.sample.json`.

## Config — `agent4.config.json`

Static knobs the orchestrator can't infer from upstream JSON:

* `db_name`, `db_unique_name`, `standby_unique_name`, `pdb_name`
* `oracle_home_version` (e.g., `19.22.0.0`)
* `character_set` (default `AL32UTF8`), `national_character_set` (default `AL16UTF16`)
* `migration_user` (default `MIGADM`)
* `standby_redo_log_count` (per-thread; default 4), `standby_redo_log_size_mb` (default 1024)
* `akv.vault_name` and `akv.secret_names.{sys,tde,wallet}` *(strongly preferred)*
* `allow_generated_passwords` — `true` only for development environments

Sample at `samples/agent4.config.sample.json`.

## Output — `agent5_input.json`

Hand-off contract for Agent 5 (Data Guard). Top-level structure:

```json
{
  "schema_version": "agent5-input/1.0",
  "status": "ready" | "ready_with_warnings" | "blocked",
  "primary_database": {
    "db_name": "MYCDB",
    "db_unique_name": "MYCDB_PRI",
    "pdb_name": "MYPDB",
    "version": "19.0.0.0.0",
    "open_mode": "READ WRITE",
    "is_rac": true,
    "is_cdb": true,
    "instance_count": 2,
    "archivelog": "ARCHIVELOG",
    "force_logging": "YES",
    "flashback_on": "YES",
    "tde": { "wallet_status": "OPEN", "wallet_type": "AUTOLOGIN", "wallet_location": "...", "master_key_id": "...", "encrypted_tablespaces": 1 },
    "standby_redo_logs": { "count": 8, "groups": "11:1024M,12:1024M,..." },
    "password_file": { "mode": "EXCLUSIVE" }
  },
  "standby_target": { "db_unique_name": "MYCDB_STBY", "service_hint": "MYCDB_STBY.client.oraclevcn.com" },
  "ssh": { "node1_host": "...", "user": "opc", "private_key_path": "..." },
  "scan": { "name": "...", "tcp_port": 1521, "tcps_port": 2484 },
  "secrets": { "source": "akv", "akv_vault_name": "...", "akv_secret_names": { "sys": "...", "tde": "...", "wallet": "..." } },
  "migration_user": { "username": "MIGADM", "container": "MYPDB" },
  "dataguard_directives": { "proceed": true, "broker_required": true, "protection_mode": "MAX_PERFORMANCE", "transport_mode": "ASYNC", "apply_mode": "REAL_TIME_APPLY", "fast_start_failover": false },
  "agent4_steps": [ /* every StepResult for audit */ ]
}
```

Note: passwords never leave Key Vault. Only **secret references** flow through `agent5_input.json`. Agent 5 fetches the same passwords by reference using its own AKV identity.

## Layout

```
agent4-db-bootstrap-tde/
├── agent4.py                       # main orchestrator
├── requirements.txt                # paramiko, jsonschema, azure-identity, azure-keyvault-secrets
├── README.md                       # this file
├── scripts/
│   └── run.sh                      # venv launcher
├── samples/
│   ├── agent4_input.sample.json    # input contract example
│   └── agent4.config.sample.json   # config example (with AKV)
├── steps/                          # one module per phase
│   ├── __init__.py
│   ├── common.py                   # SSH primitives, sqlplus heredoc helper, render_sql
│   ├── input_validation.py         # JSON Schema validation
│   ├── secrets.py                  # AKV → env → generated
│   ├── precheck.py                 # node1 health probes
│   ├── create_cdb.py               # idempotent dbaascli create + job poller
│   ├── configure_tde.py            # wallet + master key + autologin
│   ├── configure_dg_prep.py        # FORCE LOGGING, init params, SRLs, password file
│   ├── create_migration_user.py    # PDB-scoped migration user
│   └── post_validate.py            # parses AGENT4_KV>>KEY=value<< markers
└── sql/
    ├── 01_force_logging.sql
    ├── 02_archivelog_check.sql
    ├── 03_standby_redo_logs.sql    # ${SRL_COUNT}, ${SRL_SIZE_MB}
    ├── 04_dataguard_params.sql     # ${DB_NAME}, ${DB_UNIQUE_NAME}, ${STANDBY_UNIQUE_NAME}
    ├── 05_password_file_sync.sql
    ├── 06_migration_user.sql       # ${PDB_NAME}, ${MIG_USER}, ${MIG_PASSWORD}
    └── 07_post_validation.sql      # ${PDB_NAME} — emits AGENT4_KV markers
```

## Running it

```bash
# Production (AKV-backed secrets)
export AGENT4_INPUT=/path/to/agent4_input.json
export AGENT4_OUTPUT=/path/to/agent5_input.json
export AGENT4_CONFIG=/path/to/agent4.config.json
./scripts/run.sh

# Or directly
python agent4.py \
    --input  agent4_input.json \
    --output agent5_input.json \
    --config agent4.config.json \
    --log-file agent4.log \
    --verbose
```

The runner is **idempotent**:
* Re-running after a successful CDB create skips the create (it sees `dbaascli database getDetails` returning data).
* Re-running after partial DG prep is safe — every SQL block is `IF NOT YES THEN ALTER`.
* Re-running after the migration user exists rotates the password and re-grants privileges.

## Exit codes

| Code | Meaning | Implication for Agent 5 |
|-----:|---------|-------------------------|
| 0 | All steps PASS | Proceed |
| 1 | At least one WARN, no FAIL | Proceed; review `agent5_input.json/agent4_steps` |
| 2 | At least one FAIL | **Stop.** `dataguard_directives.proceed = false`. |
| 3 | Unhandled orchestration exception | Stop. Investigate logs. |

## Security notes

* **No plaintext passwords in JSON.** AKV references only.
* **Password file on disk only briefly.** `/var/tmp/agent4_dbaas_pw.json` is written 0600, owned by oracle, and removed immediately after `dbaascli database create` submits.
* **No password on the wire.** sqlplus connects via bequeath (`/ as sysdba`) — passwords are passed only as DDL parameters inside heredocs that are pipe-fed to sqlplus.
* **Sensitive command logging.** `run_remote(sensitive=True)` redacts the command body in debug logs.
* **TDE.** Master key created `WITH BACKUP USING 'agent4-init'`. Autologin keystore (`cwallet.sso`) created so the DB opens cleanly across restarts.

## Pre-requisites

* Network reachability to node1 from wherever the agent runs (port 22).
* SSH private key registered with the Exadata VM cluster (`opc` user).
* Azure managed identity (or service principal env vars) with **Get** permission on the AKV secrets, when AKV is used.
* `dbaascli` ≥ 25.x on the cluster (standard ODAA images).

## Hand-off to Agent 5

When this agent exits 0 or 1, `agent5_input.json` contains everything Agent 5 needs:
the primary's `db_unique_name`, the standby's planned `db_unique_name`, RAC topology, TDE wallet location/type/master-key-id, SRL inventory, password file mode, the SCAN/SSH coordinates, and the AKV references for SYS/TDE/wallet passwords. Agent 5 then duplicates the database to the standby, configures the Broker, and enables redo apply.
