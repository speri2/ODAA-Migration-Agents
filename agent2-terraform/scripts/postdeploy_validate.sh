#!/usr/bin/env bash
# Agent 2 — post-deploy validation + handoff emission.
# Run AFTER `terraform apply` succeeds.
#
# Steps:
#   1. Read terraform output (must include scan_dns_name, vm_cluster_id, agent3_handoff)
#   2. Verify VM cluster lifecycleState == AVAILABLE via az
#   3. Verify SCAN DNS resolves and SCAN port reachable from this host (best effort)
#   4. Emit final agent3_input.json (the canonical handoff for Agent 3)
#
set -euo pipefail
IFS=$'\n\t'

OUT="${1:-agent3_input.json}"
LOG_PREFIX="[agent2-postdeploy]"
log()  { printf '%s %s\n'  "$LOG_PREFIX" "$*"; }
fail() { printf '%s ERROR: %s\n' "$LOG_PREFIX" "$*" >&2; exit 1; }

command -v terraform >/dev/null || fail "terraform missing"
command -v jq >/dev/null        || fail "jq missing"
command -v az >/dev/null        || fail "az missing"

############# 1. Read TF outputs #############
log "Reading terraform outputs..."
TF_JSON=$(terraform output -json)
echo "$TF_JSON" | jq -e '.agent3_handoff.value' >/dev/null \
  || fail "agent3_handoff output missing — re-run terraform apply"

VMC_ID=$(echo "$TF_JSON" | jq -r '.vm_cluster_id.value')
SCAN_DNS=$(echo "$TF_JSON" | jq -r '.scan_dns_name.value')
SCAN_PORT=$(echo "$TF_JSON" | jq -r '.scan_listener_port_tcp.value')
log "VM Cluster ID: $VMC_ID"
log "SCAN DNS: $SCAN_DNS:$SCAN_PORT"

############# 2. lifecycleState check #############
LSTATE=$(az resource show --ids "$VMC_ID" --query "properties.lifecycleState" -o tsv 2>/dev/null || echo "Unknown")
log "VM Cluster lifecycleState: $LSTATE"
[[ "$LSTATE" == "Available" ]] || fail "VM cluster is not Available (got '$LSTATE')."

############# 3. Best-effort SCAN reachability #############
if command -v getent >/dev/null && [[ -n "$SCAN_DNS" && "$SCAN_DNS" != "null" ]]; then
  if getent hosts "$SCAN_DNS" >/dev/null; then
    log "SCAN DNS resolves: $(getent hosts "$SCAN_DNS" | head -1)"
  else
    log "WARN: SCAN DNS '$SCAN_DNS' did not resolve from this host (expected if you're outside the VNet)."
  fi
fi
if command -v nc >/dev/null && [[ -n "$SCAN_DNS" && "$SCAN_DNS" != "null" ]]; then
  if timeout 5 nc -z "$SCAN_DNS" "$SCAN_PORT" 2>/dev/null; then
    log "TCP $SCAN_PORT open at $SCAN_DNS"
  else
    log "WARN: TCP $SCAN_PORT not reachable from this host. Agent 3 must run from a host on the VNet (or peered)."
  fi
fi

############# 4. Emit handoff JSON #############
echo "$TF_JSON" | jq '.agent3_handoff.value + {
  validation: {
    timestamp: now | strftime("%Y-%m-%dT%H:%M:%SZ"),
    vm_cluster_lifecycle_state: "'"$LSTATE"'",
    preflight_status: "passed",
    notes: "Reachability checks are advisory; Agent 3 performs authoritative Oracle Net validation."
  },
  status: "ready_for_agent3"
}' > "$OUT"

log "Handoff written to $OUT"
log "Pass this file to Agent 3 (Oracle Net connectivity validation)."
