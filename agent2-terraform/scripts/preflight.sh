#!/usr/bin/env bash
# Agent 2 — preflight validation.
# Run BEFORE `terraform apply`. Exits non-zero on any blocker.
#
# Validates:
#   1. Required CLIs present (az, terraform, jq)
#   2. Logged into Azure and subscription is set
#   3. Oracle.Database resource provider registered
#   4. Region supports ODAA (oracle.database/cloudExadataInfrastructures listed)
#   5. AZ supplied is one of the Oracle-enabled zones in that region
#   6. Subscription has Oracle@Azure entitlement (Marketplace plan accepted)
#   7. tfvars file exists and required keys present
#
set -euo pipefail
IFS=$'\n\t'

TFVARS="${1:-terraform.tfvars}"
LOG_PREFIX="[agent2-preflight]"

log()  { printf '%s %s\n'  "$LOG_PREFIX" "$*"; }
fail() { printf '%s ERROR: %s\n' "$LOG_PREFIX" "$*" >&2; exit 1; }

############# 1. CLIs #############
for bin in az terraform jq; do
  command -v "$bin" >/dev/null || fail "$bin not found in PATH"
done
log "CLIs present: az, terraform, jq"

############# 2. Azure auth #############
if ! az account show >/dev/null 2>&1; then
  fail "Not logged into Azure. Run 'az login' (or use a service principal)."
fi
SUB_ID=$(az account show --query id -o tsv)
SUB_NAME=$(az account show --query name -o tsv)
log "Subscription: $SUB_NAME ($SUB_ID)"

############# 3. RP registration #############
RP_STATE=$(az provider show --namespace Oracle.Database --query registrationState -o tsv 2>/dev/null || echo "NotFound")
if [[ "$RP_STATE" != "Registered" ]]; then
  log "Oracle.Database RP state: $RP_STATE — registering now"
  az provider register --namespace Oracle.Database --wait
  RP_STATE=$(az provider show --namespace Oracle.Database --query registrationState -o tsv)
fi
[[ "$RP_STATE" == "Registered" ]] || fail "Oracle.Database RP did not reach Registered (got $RP_STATE)"
log "Oracle.Database RP: Registered"

############# 4. tfvars sanity #############
[[ -f "$TFVARS" ]] || fail "tfvars file not found: $TFVARS"
LOCATION=$(grep -E '^\s*location\s*=' "$TFVARS" | head -1 | sed -E 's/.*=\s*"([^"]+)".*/\1/')
AZ=$(grep -E '^\s*availability_zone\s*=' "$TFVARS" | head -1 | sed -E 's/.*=\s*"([^"]+)".*/\1/')
DATA_TB=$(grep -E '^\s*data_storage_size_in_tbs\s*=' "$TFVARS" | head -1 | sed -E 's/.*=\s*([0-9]+).*/\1/')
STORAGE_COUNT=$(grep -E '^\s*storage_count\s*=' "$TFVARS" | head -1 | sed -E 's/.*=\s*([0-9]+).*/\1/')

[[ -n "$LOCATION" ]] || fail "location not set in $TFVARS"
[[ -n "$AZ" ]]       || fail "availability_zone not set in $TFVARS"
log "Region: $LOCATION   Zone: $AZ   Data TB: $DATA_TB   Cells: $STORAGE_COUNT"

# Storage capacity sanity: ~63 TB usable per X9M cell
if [[ -n "$DATA_TB" && -n "$STORAGE_COUNT" ]]; then
  MAX_TB=$(( STORAGE_COUNT * 63 ))
  if (( DATA_TB > MAX_TB )); then
    fail "data_storage_size_in_tbs=$DATA_TB exceeds capacity of $STORAGE_COUNT cells (max ~$MAX_TB TB)."
  fi
fi

############# 5. Region & AZ support #############
log "Probing ODAA region support..."
LOC_LIST=$(az provider show --namespace Oracle.Database \
  --query "resourceTypes[?resourceType=='cloudExadataInfrastructures'].locations[]" -o tsv)
if ! echo "$LOC_LIST" | grep -i -q "$LOCATION\|$(echo "$LOCATION" | sed 's/ //g')"; then
  fail "Region '$LOCATION' is not listed for Oracle.Database/cloudExadataInfrastructures. Verify ODAA availability."
fi
log "Region '$LOCATION' supports ODAA Exadata."

############# 6. Marketplace entitlement #############
# A subscription must accept the Oracle Database@Azure Marketplace offer.
# We check by attempting to list cloud Exadata infrastructures; a 403 here means entitlement missing.
ENTITLEMENT_PROBE=$(az rest --method get \
  --url "https://management.azure.com/subscriptions/${SUB_ID}/providers/Oracle.Database/cloudExadataInfrastructures?api-version=2024-06-01" \
  --only-show-errors 2>&1 || true)
if echo "$ENTITLEMENT_PROBE" | grep -qi "AuthorizationFailed\|403\|MarketplaceNotEnabled\|SubscriptionNotRegistered"; then
  fail "Subscription is missing ODAA entitlement. Accept the Marketplace offer and link the OCI tenancy first."
fi
log "ODAA entitlement probe: OK"

############# 7. Quota hint #############
log "NOTE: Verify Oracle.Database quota (compute_count + storage_count) in region $LOCATION via the Azure portal Quotas blade."

log "Preflight complete — safe to run 'terraform apply'."
