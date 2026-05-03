#!/usr/bin/env bash
# Agent 2 — orchestrator: preflight -> init -> validate -> plan -> apply -> postdeploy.
# Idempotent. Safe to re-run.
#
# Usage:
#   ./scripts/deploy.sh [tfvars_file]
#
# Env vars (optional):
#   TF_BACKEND_RG, TF_BACKEND_SA, TF_BACKEND_CONTAINER, TF_BACKEND_KEY  (remote state)
#   AUTO_APPROVE=1                                                     (skip approval)
#
set -euo pipefail
IFS=$'\n\t'

TFVARS="${1:-terraform.tfvars}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
LOG="$ROOT/agent2-run-$(date -u +%Y%m%dT%H%M%SZ).log"

log()  { printf '[agent2] %s\n' "$*" | tee -a "$LOG"; }
fail() { printf '[agent2] FATAL: %s\n' "$*" | tee -a "$LOG" >&2; exit 1; }

cd "$ROOT"

log "Run log: $LOG"
log "Step 1/6: preflight"
bash "$HERE/preflight.sh" "$TFVARS" 2>&1 | tee -a "$LOG"

log "Step 2/6: terraform init"
INIT_ARGS=()
if [[ -n "${TF_BACKEND_RG:-}" ]]; then
  INIT_ARGS+=(-backend-config="resource_group_name=${TF_BACKEND_RG}"
              -backend-config="storage_account_name=${TF_BACKEND_SA}"
              -backend-config="container_name=${TF_BACKEND_CONTAINER}"
              -backend-config="key=${TF_BACKEND_KEY:-agent2-odaa.tfstate}")
fi
terraform init -upgrade "${INIT_ARGS[@]}" 2>&1 | tee -a "$LOG" \
  || fail "terraform init failed"

log "Step 3/6: terraform validate"
terraform validate 2>&1 | tee -a "$LOG" || fail "terraform validate failed"

log "Step 4/6: terraform plan"
terraform plan -var-file="$TFVARS" -out="$ROOT/agent2.tfplan" 2>&1 | tee -a "$LOG" \
  || fail "terraform plan failed"

if [[ "${AUTO_APPROVE:-0}" != "1" ]]; then
  read -r -p "[agent2] Apply the plan above? [yes/NO] " ans
  [[ "$ans" == "yes" ]] || fail "Apply rejected by operator."
fi

log "Step 5/6: terraform apply (this can take 4-6 hours for first-time Exadata provisioning)"
terraform apply "$ROOT/agent2.tfplan" 2>&1 | tee -a "$LOG" \
  || fail "terraform apply failed — see $LOG"

log "Step 6/6: postdeploy validation + handoff"
bash "$HERE/postdeploy_validate.sh" "$ROOT/agent3_input.json" 2>&1 | tee -a "$LOG"

log "DONE. Handoff: $ROOT/agent3_input.json"
