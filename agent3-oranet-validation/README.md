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
