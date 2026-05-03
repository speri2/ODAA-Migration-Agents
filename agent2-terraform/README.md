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
