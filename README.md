This is an agentic AI Deployment of Exadata workloads of 100 TB from OnPrem Exadata Oracle servers to ODAA on Azure. 

# Agent 2 — Terraform Deployment (ODAA Exadata, 100 TB)

Production-grade Terraform module that provisions Oracle Database@Azure (ODAA)
Exadata infrastructure and a Cloud VM Cluster sized for **100 TB usable**.
Emits a JSON handoff (`agent3_input.json`) consumed by Agent 3 (Oracle Net validation).

## Layout

```
agent2-terraform/
├── versions.tf                 # provider + backend
├── variables.tf                # all inputs, with validation rules
├── network.tf                  # RG, VNet, delegated subnet, NSG
├── main.tf                     # Exadata Infra + VM Cluster + SSH
├── outputs.tf                  # outputs + agent3_input.json emission
├── terraform.tfvars.example    # copy to terraform.tfvars
├── scripts/
│   ├── deploy.sh               # orchestrator (preflight -> apply -> handoff)
│   ├── preflight.sh            # 7-step environment check
│   └── postdeploy_validate.sh  # state check + handoff JSON
└── README.md
```

## Sizing notes (100 TB)

| Knob | Default | Why |
|------|---------|-----|
| `exa_shape` | `Exadata.X9M` | Most broadly available ODAA shape |
| `compute_count` | 2 | 2-node RAC; raise for higher OLTP throughput |
| `storage_count` | 3 | X9M cell ≈ 63.6 TB usable @ NORMAL redundancy → 3 cells covers 100 TB |
| `data_storage_size_in_tbs` | 100 | Allocated to DATA disk group |
| `data_storage_percentage` | 80 | 80% DATA / 20% RECO |
| `is_local_backup_enabled` | false | RECO consumed by ZRCV / Azure Blob backup tier |

`main.tf` enforces a precondition that `data_storage_size_in_tbs <= storage_count * 63`.

## Run

```bash
cd agent2-terraform
cp terraform.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars

# (optional remote state)
export TF_BACKEND_RG=rg-tfstate
export TF_BACKEND_SA=sttfstateexam
export TF_BACKEND_CONTAINER=tfstate
export TF_BACKEND_KEY=agent2-odaa.tfstate

bash scripts/deploy.sh terraform.tfvars
```

`deploy.sh` will:

1. Preflight (CLIs, az login, RP registration, region support, marketplace entitlement, tfvars).
2. `terraform init` (with optional Azure backend).
3. `terraform validate`.
4. `terraform plan` → saved plan file.
5. Approval prompt (skip with `AUTO_APPROVE=1`).
6. `terraform apply`.
7. Postdeploy: lifecycleState check, SCAN reachability, emit `agent3_input.json`.

## Handoff to Agent 3

After a successful run, `agent3_input.json` contains everything Agent 3 needs:

```json
{
  "schema_version": "1.0",
  "agent": "agent2-terraform",
  "next_agent": "agent3-oranet-validation",
  "azure": { "subscription_id": "...", "resource_group_name": "...", "...": "..." },
  "exadata_infrastructure": { "azure_id": "...", "ocid": "...", "shape": "Exadata.X9M" },
  "vm_cluster": { "azure_id": "...", "ocid": "...", "cluster_name": "...", "...": "..." },
  "network": { "vnet_id": "...", "client_subnet_id": "...", "client_subnet_cidr": "10.50.10.0/24" },
  "oracle_net": { "scan_dns_name": "...", "scan_listener_port_tcp": 1521, "scan_listener_port_tcp_ssl": 2484 },
  "ssh": { "private_key_path": "./generated_id_rsa", "os_user": "opc" },
  "validation": { "vm_cluster_lifecycle_state": "Available", "preflight_status": "passed" },
  "status": "ready_for_agent3"
}
```

## Failure modes & remediation

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `MarketplaceNotEnabled` / 403 in preflight | ODAA Marketplace plan not accepted | Accept the Oracle Database@Azure offer in the subscription, link OCI tenancy |
| `Oracle.Database RP NotRegistered` | Fresh subscription | Preflight auto-registers; or run `az provider register --namespace Oracle.Database --wait` |
| `data_storage_size_in_tbs exceeds capacity` precondition | Too few cells | Increase `storage_count` (each X9M cell ≈ 63 TB usable) |
| VM cluster stuck in `Provisioning` | Normal — first apply takes 4–6 hours | Re-run `postdeploy_validate.sh` once it lands in `Available` |
| `Subnet must be delegated to Oracle.Database/networkAttachments` | Existing subnet missing delegation | Either set `create_network=true` or add the delegation to the existing subnet |
| Quota error on apply | Region/sub quota not increased for ODAA | File quota request for `Oracle.Database` cores/storage cells |

## Security posture

- SSH keys: BYO via `ssh_public_keys`, or auto-generated and written `0600` to `ssh_key_output_path` (omit from VCS).
- Backend: Azure Storage with state file, configured via `-backend-config` at init.
- NSG: when `nsg_allowed_source_cidrs` is non-empty, only those CIDRs may reach SCAN 1521/2484.
- Diagnostic / health / incident telemetry to Oracle: opt-in, default true (override per environment).
- Customer contacts required: hardware fault notification email list.

## What this agent does NOT do (by design)

- DB creation / TDE — handled by Agent 4.
- Data Guard configuration — handled by Agent 5.
- Oracle Net validation — handled by Agent 3.
- Migration source assessment — handled by Agent 1.

# Agent 3 — Oracle Net Connectivity Validation

Consumes the JSON contract emitted by Agent 2 (`agent3_input.json`) and proves
end-to-end Oracle Net reachability against the freshly-provisioned Exadata
VM Cluster. On success, emits `agent4_input.json` for the DB bootstrap agent.

## What it actually checks

| Phase | Check | Severity policy |
|---|---|---|
| 1 | `input_contract` — JSON Schema validation of upstream payload | FAIL ⇒ abort |
| 2 | `azure_state.exadata_infrastructure`, `azure_state.vm_cluster` — `lifecycleState == Available` | FAIL on anything other than Available/Provisioning |
| 3 | `dns.<scan>` — SCAN must yield ≥ 3 A records (RAC requirement); per-node hostname resolution | SCAN < 3 ⇒ WARN, fail to resolve ⇒ FAIL |
| 4 | `tcp.scan-tcp-1521`, `tcp.scan-tcps-2484`, `tcp.ssh@<node>:22` | refused/timeout ⇒ FAIL |
| 5 | `oracle_net.scan-tcp-1521`, `oracle_net.scan-tcps-2484` — **real TNS Connect packet**, parses listener response | Accept/Refuse/Redirect/Resend ⇒ PASS, no response ⇒ FAIL |
| 6 | `ssh.<node>` — paramiko connect + `hostname/uname/oracle-release` | unauth/timeout ⇒ FAIL |
| 7 | `crs.cluster`, `crs.scan`, `crs.listener`, `crs.asm`, `crs.diskgroups`, `crs.nodeapps`, `os.chrony` | run `crsctl/srvctl/asmcmd` over SSH on node1 |

Each `CheckResult` contains a name, severity (`pass`/`warn`/`fail`/`skip`),
summary, structured details, target (host:port or resource id), and elapsed_ms.

### Why a real TNS handshake matters

A pure TCP probe proves only that *something* is listening on 1521. The Oracle
Net handshake constructs a valid `Connect` packet with a `(DESCRIPTION=...)`
payload and parses the first 8 bytes of the listener response:

| TNS packet type | Meaning | Verdict |
|---|---|---|
| `02 Accept` | listener accepted (no DB usually means redirect first) | PASS |
| `04 Refuse` | listener answered Oracle Net but refuses (typical pre-DB) | **PASS** — proves protocol |
| `05 Redirect` | normal RAC redirect | PASS |
| `0B Resend` | listener wants resend | PASS |
| no response / timeout / non-TNS bytes | something other than Oracle Net | FAIL |

Pre-DB-creation, the listener will Refuse with `(ERR=12514)`. That still proves
the migration path will work once Agent 4 creates the database — exactly the
information Agent 3 is supposed to surface.

## Layout

```
agent3-oranet-validation/
├── agent3.py                       # orchestrator (CLI)
├── requirements.txt                # paramiko, dnspython, jsonschema
├── checks/
│   ├── __init__.py
│   ├── common.py                   # CheckResult, Severity, parallel runner
│   ├── input_validation.py         # JSON Schema for agent3_input.json
│   ├── azure_state.py              # az resource show
│   ├── dns_check.py                # SCAN + per-node resolution
│   ├── tcp_probe.py                # raw TCP reachability
│   ├── oracle_net.py               # TNS Connect packet handshake (TCP + TCPS)
│   ├── ssh_check.py                # paramiko reach + OS probe
│   └── cluster_health.py           # crsctl / srvctl / asmcmd over SSH
├── scripts/
│   └── run.sh                      # venv + install + run wrapper
├── samples/
│   └── agent3_input.sample.json
└── README.md
```

## Run

```bash
cd agent3-oranet-validation

# Place the JSON Agent 2 produced
cp /path/from/agent2/agent3_input.json .

# Default run
bash scripts/run.sh

# Or directly
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python3 agent3.py --input agent3_input.json --output agent4_input.json -v
```

### Exit codes

| Code | Meaning | Downstream behavior |
|------|---------|---------------------|
| 0 | all PASS | `agent4_input.json` written, status = `ready` |
| 1 | warnings only | `agent4_input.json` written, status = `ready_with_warnings` |
| 2 | one or more FAIL | downstream JSON **not written**, status = `blocked` |
| 3 | unhandled error / bad CLI | nothing written |

`--strict-pass` upgrades any non-PASS result to exit 2. `--allow-warnings`
downgrades warnings-only runs to exit 0 (use in CI when you want warnings to be
informational, not gating).

## Output: `agent4_input.json`

```json
{
  "schema_version": "1.0",
  "agent": "agent3-oranet-validation",
  "next_agent": "agent4-db-bootstrap-tde",
  "status": "ready" | "ready_with_warnings" | "blocked",
  "upstream": { "azure": {}, "vm_cluster": {}, "network": {}, "oracle_net": {}, "ssh": {} },
  "validation": {
    "summary": { "total": 14, "passed": 12, "warned": 2, "failed": 0, "skipped": 0, "duration_ms": 8421 },
    "results": [ { "name": "...", "severity": "pass", "summary": "...", "details": {...}, "target": "...", "elapsed_ms": 12 } ]
  },
  "agent4_directives": {
    "use_scan_dns_name": "exampprd-scan.odaa.example.internal",
    "use_scan_port_tcp": 1521,
    "use_scan_port_tcps": 2484,
    "ssh_target_node1": "exampprd1.odaa.example.internal",
    "ssh_user": "opc",
    "ssh_private_key_path": "./generated_id_rsa",
    "proceed": true
  }
}
```

`agent4_directives.proceed = false` when status is `blocked`. Agent 4 must
honor this gate.

## Where Agent 3 must run

This validation **must execute from a host inside the VNet** (or a peered VNet
with route + DNS) — SCAN is RFC1918 and not internet-reachable. A typical
deployment runs Agent 3 from a small Azure VM in the same VNet as the client
subnet, often the same jumphost used for migration tooling.

If you run it from outside the VNet, expect Phase 4–7 to FAIL even when the
cluster is healthy.

## Failure modes & remediation

| Symptom | Likely cause | Fix |
|---|---|---|
| `dns.<scan>` resolved 0 IPs | DNS not configured / private DNS zone not linked | Link Azure Private DNS zone to the VNet, or add SCAN entries to your corporate DNS |
| `dns.<scan>` resolved 1 IP | RAC SCAN not fully published yet | Wait — Oracle publishes 3 SCAN VIPs after vmcluster is Available |
| `tcp.scan-tcp-1521` FAIL but `azure_state.vm_cluster` PASS | NSG blocks the runner host | Add the runner CIDR to `nsg_allowed_source_cidrs` in Agent 2 tfvars |
| `oracle_net.scan-tcp-1521` FAIL with `non-TNS bytes` | Something other than the Oracle listener on that port | Check NSG / private endpoint misrouting |
| `oracle_net.scan-tcp-1521` PASS with type `Refuse` | Expected before Agent 4 has created the DB | Proceed — Agent 4 will create the service |
| `ssh.<node>` FAIL `Authentication failed` | Wrong key | Re-run Agent 2 with correct `ssh_public_keys` or use the auto-generated key |
| `crs.cluster` rc!=0 | grid sudo not configured | Confirm `opc` can `sudo -iu grid` (default on ODAA images) |

## Security notes

- TCPS handshake uses `CERT_NONE` because Oracle issues self-signed listener
  certs for fresh clusters. This validates the protocol, not the certificate
  chain. Agent 4 wallets / certificate trust is configured later.
- The handoff JSON omits `authorized_keys` — only the path to the private key
  is propagated, never key material.
- `run.sh` provisions an isolated venv; nothing is installed system-wide.

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








 
