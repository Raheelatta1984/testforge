#!/usr/bin/env bash
set -Eeuo pipefail

# TestForge OCI Always Free infrastructure provisioner.
# Run this from OCI Cloud Shell while logged in to the target tenancy.

NAME_PREFIX="${TF_OCI_NAME_PREFIX:-testforge}"
VCN_CIDR="${TF_OCI_VCN_CIDR:-10.0.0.0/16}"
SUBNET_CIDR="${TF_OCI_SUBNET_CIDR:-10.0.0.0/24}"
OCPUS="${TF_OCI_OCPUS:-1}"
MEMORY_GB="${TF_OCI_MEMORY_GB:-6}"
BOOT_GB="${TF_OCI_BOOT_GB:-100}"
SHAPE="VM.Standard.A1.Flex"

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

command -v oci >/dev/null || die "OCI CLI is unavailable. Run this script in Oracle Cloud Shell."

CONFIG_FILE="${OCI_CLI_CONFIG_FILE:-$HOME/.oci/config}"
PROFILE="${OCI_CLI_PROFILE:-DEFAULT}"
[[ -f "$CONFIG_FILE" ]] || die "OCI CLI configuration was not found at $CONFIG_FILE."

config_value() {
  local key="$1"
  awk -F= -v profile="[$PROFILE]" -v key="$key" '
    $0 == profile { active=1; next }
    /^\[/ { active=0 }
    active && $1 == key { sub(/^[^=]*=/, ""); gsub(/^[[:space:]]+|[[:space:]]+$/, ""); print; exit }
  ' "$CONFIG_FILE"
}

TENANCY_ID="${TF_OCI_COMPARTMENT_ID:-$(config_value tenancy)}"
REGION="${TF_OCI_REGION:-$(config_value region)}"
[[ -n "$TENANCY_ID" ]] || die "Could not determine the tenancy/compartment OCID."
[[ -n "$REGION" ]] || die "Could not determine the OCI region."
export OCI_CLI_REGION="$REGION"

if (( OCPUS > 2 )) || (( MEMORY_GB > 12 )); then
  die "Requested ${OCPUS} OCPU/${MEMORY_GB} GB exceeds the current 2 OCPU/12 GB Always Free A1 allowance."
fi
if (( BOOT_GB > 200 )); then
  die "Requested ${BOOT_GB} GB boot volume exceeds the 200 GB Always Free block-storage allowance."
fi

log "Using region $REGION"
HOME_REGION=$(oci iam region-subscription list \
  --tenancy-id "$TENANCY_ID" --all \
  --query 'data[?"is-home-region" == `true`]."region-name" | [0]' --raw-output)
if [[ "$HOME_REGION" != "$REGION" ]]; then
  die "Always Free resources must be created in the home region ($HOME_REGION), but Cloud Shell is using $REGION. Change region and retry."
fi

AD_NAME=$(oci iam availability-domain list --compartment-id "$TENANCY_ID" \
  --query 'data[0].name' --raw-output)
[[ -n "$AD_NAME" && "$AD_NAME" != null ]] || die "No availability domain was found."

SSH_DIR="$HOME/.ssh"
SSH_KEY="$SSH_DIR/${NAME_PREFIX}_oci"
mkdir -p "$SSH_DIR"
chmod 700 "$SSH_DIR"
if [[ ! -f "$SSH_KEY" ]]; then
  log "Generating a FIPS-compatible RSA SSH key in Cloud Shell"
  ssh-keygen -t rsa -b 4096 -N '' -C "${NAME_PREFIX}-oci" -f "$SSH_KEY"
fi
[[ -s "$SSH_KEY" && -s "${SSH_KEY}.pub" ]] || die "SSH key generation did not produce a valid key pair. Remove $SSH_KEY and retry."
SSH_PUBLIC_KEY=$(cat "${SSH_KEY}.pub")

find_id() {
  local type="$1" name="$2" query="$3"
  oci $type list --compartment-id "$TENANCY_ID" --all $query \
    --query "data[?\"display-name\" == '$name' && \"lifecycle-state\" != 'TERMINATED'] | [0].id" \
    --raw-output 2>/dev/null || true
}

VCN_NAME="${NAME_PREFIX}-vcn"
VCN_ID=$(find_id "network vcn" "$VCN_NAME" "")
if [[ -z "$VCN_ID" || "$VCN_ID" == null ]]; then
  log "Creating VCN"
  VCN_ID=$(oci network vcn create --compartment-id "$TENANCY_ID" \
    --display-name "$VCN_NAME" --dns-label testforge --cidr-block "$VCN_CIDR" \
    --wait-for-state AVAILABLE --query data.id --raw-output)
else
  log "Reusing VCN $VCN_NAME"
fi

IGW_NAME="${NAME_PREFIX}-internet-gateway"
IGW_ID=$(oci network internet-gateway list --compartment-id "$TENANCY_ID" --vcn-id "$VCN_ID" --all \
  --query "data[?\"display-name\" == '$IGW_NAME' && \"lifecycle-state\" != 'TERMINATED'] | [0].id" --raw-output)
if [[ -z "$IGW_ID" || "$IGW_ID" == null ]]; then
  log "Creating internet gateway"
  IGW_ID=$(oci network internet-gateway create --compartment-id "$TENANCY_ID" --vcn-id "$VCN_ID" \
    --display-name "$IGW_NAME" --is-enabled true --wait-for-state AVAILABLE \
    --query data.id --raw-output)
fi

ROUTE_NAME="${NAME_PREFIX}-public-routes"
ROUTE_ID=$(oci network route-table list --compartment-id "$TENANCY_ID" --vcn-id "$VCN_ID" --all \
  --query "data[?\"display-name\" == '$ROUTE_NAME' && \"lifecycle-state\" != 'TERMINATED'] | [0].id" --raw-output)
if [[ -z "$ROUTE_ID" || "$ROUTE_ID" == null ]]; then
  log "Creating public route table"
  ROUTE_RULES=$(printf '[{"destination":"0.0.0.0/0","destinationType":"CIDR_BLOCK","networkEntityId":"%s"}]' "$IGW_ID")
  ROUTE_ID=$(oci network route-table create --compartment-id "$TENANCY_ID" --vcn-id "$VCN_ID" \
    --display-name "$ROUTE_NAME" --route-rules "$ROUTE_RULES" --wait-for-state AVAILABLE \
    --query data.id --raw-output)
fi

SEC_NAME="${NAME_PREFIX}-security-list"
SEC_ID=$(oci network security-list list --compartment-id "$TENANCY_ID" --vcn-id "$VCN_ID" --all \
  --query "data[?\"display-name\" == '$SEC_NAME' && \"lifecycle-state\" != 'TERMINATED'] | [0].id" --raw-output)
if [[ -z "$SEC_ID" || "$SEC_ID" == null ]]; then
  log "Creating security rules for SSH, HTTP and HTTPS"
  INGRESS='[
    {"protocol":"6","source":"0.0.0.0/0","sourceType":"CIDR_BLOCK","tcpOptions":{"destinationPortRange":{"min":22,"max":22}}},
    {"protocol":"6","source":"0.0.0.0/0","sourceType":"CIDR_BLOCK","tcpOptions":{"destinationPortRange":{"min":80,"max":80}}},
    {"protocol":"6","source":"0.0.0.0/0","sourceType":"CIDR_BLOCK","tcpOptions":{"destinationPortRange":{"min":443,"max":443}}}
  ]'
  EGRESS='[{"protocol":"all","destination":"0.0.0.0/0","destinationType":"CIDR_BLOCK"}]'
  SEC_ID=$(oci network security-list create --compartment-id "$TENANCY_ID" --vcn-id "$VCN_ID" \
    --display-name "$SEC_NAME" --ingress-security-rules "$INGRESS" --egress-security-rules "$EGRESS" \
    --wait-for-state AVAILABLE --query data.id --raw-output)
fi

SUBNET_NAME="${NAME_PREFIX}-public-subnet"
SUBNET_ID=$(oci network subnet list --compartment-id "$TENANCY_ID" --vcn-id "$VCN_ID" --all \
  --query "data[?\"display-name\" == '$SUBNET_NAME' && \"lifecycle-state\" != 'TERMINATED'] | [0].id" --raw-output)
if [[ -z "$SUBNET_ID" || "$SUBNET_ID" == null ]]; then
  log "Creating public subnet"
  SUBNET_ID=$(oci network subnet create --compartment-id "$TENANCY_ID" --vcn-id "$VCN_ID" \
    --display-name "$SUBNET_NAME" --dns-label public --cidr-block "$SUBNET_CIDR" \
    --route-table-id "$ROUTE_ID" --security-list-ids "[\"$SEC_ID\"]" \
    --prohibit-public-ip-on-vnic false --wait-for-state AVAILABLE \
    --query data.id --raw-output)
fi

log "Finding the latest Ubuntu 24.04 ARM image"
IMAGE_ID=$(oci compute image list --compartment-id "$TENANCY_ID" --all \
  --operating-system "Canonical Ubuntu" --operating-system-version "24.04" \
  --shape "$SHAPE" --sort-by TIMECREATED --sort-order DESC \
  --query 'data[0].id' --raw-output)
[[ -n "$IMAGE_ID" && "$IMAGE_ID" != null ]] || die "No Ubuntu 24.04 image compatible with $SHAPE was found."

INSTANCE_NAME="${NAME_PREFIX}-production"
INSTANCE_ID=$(oci compute instance list --compartment-id "$TENANCY_ID" --all \
  --display-name "$INSTANCE_NAME" \
  --query 'data[?"lifecycle-state" != `TERMINATED`] | [0].id' --raw-output)
if [[ -z "$INSTANCE_ID" || "$INSTANCE_ID" == null ]]; then
  log "Creating Always Free ARM instance (${OCPUS} OCPU, ${MEMORY_GB} GB RAM)"
  SHAPE_CONFIG=$(printf '{"ocpus":%s,"memoryInGBs":%s}' "$OCPUS" "$MEMORY_GB")
  METADATA=$(python3 -c 'import json,sys; print(json.dumps({"ssh_authorized_keys": sys.argv[1]}))' "$SSH_PUBLIC_KEY")
  LAUNCH_OUT=$(mktemp)
  LAUNCH_ERR=$(mktemp)
  trap 'rm -f "$LAUNCH_OUT" "$LAUNCH_ERR"' EXIT
  INSTANCE_ID=""
  attempt=0
  MAX_LAUNCH_ATTEMPTS="${TF_OCI_MAX_LAUNCH_ATTEMPTS:-0}"
  CAPACITY_RETRY_SECONDS="${TF_OCI_CAPACITY_RETRY_SECONDS:-1800}"
  while [[ -z "$INSTANCE_ID" ]]; do
    attempt=$((attempt + 1))
    : >"$LAUNCH_OUT"
    : >"$LAUNCH_ERR"
    if oci compute instance launch \
      --compartment-id "$TENANCY_ID" --availability-domain "$AD_NAME" \
      --display-name "$INSTANCE_NAME" --shape "$SHAPE" --shape-config "$SHAPE_CONFIG" \
      --image-id "$IMAGE_ID" --subnet-id "$SUBNET_ID" --assign-public-ip true \
      --metadata "$METADATA" --boot-volume-size-in-gbs "$BOOT_GB" \
      --wait-for-state RUNNING --max-wait-seconds 1200 \
      --query data.id --raw-output >"$LAUNCH_OUT" 2>"$LAUNCH_ERR"; then
      INSTANCE_ID=$(cat "$LAUNCH_OUT")
      break
    fi

    if [[ "$MAX_LAUNCH_ATTEMPTS" != "0" && "$attempt" -ge "$MAX_LAUNCH_ATTEMPTS" ]]; then
      cat "$LAUNCH_ERR" >&2
      die "OCI did not create the instance after ${attempt} attempts."
    fi

    if grep -Eqi 'Out of host capacity|OutOfHostCapacity' "$LAUNCH_ERR"; then
      log "No A1 host capacity is available in $REGION. Retrying in $((CAPACITY_RETRY_SECONDS / 60)) minutes (attempt $attempt)."
      sleep "$CAPACITY_RETRY_SECONDS"
    elif grep -Eqi 'Too many requests|status[^0-9]*429|TooManyRequests' "$LAUNCH_ERR"; then
      log "OCI is rate limiting the launch request. Retrying in 2 minutes (attempt $attempt)."
      sleep 120
    else
      cat "$LAUNCH_ERR" >&2
      die "OCI could not create the instance."
    fi
  done
  rm -f "$LAUNCH_OUT" "$LAUNCH_ERR"
  trap - EXIT
else
  log "Reusing instance $INSTANCE_NAME"
fi

PUBLIC_IP=$(oci compute instance list-vnics --instance-id "$INSTANCE_ID" \
  --query 'data[0]."public-ip"' --raw-output)

cat <<EOF

============================================================
TestForge Oracle infrastructure is ready.

Region:      $REGION
Instance:    $INSTANCE_NAME
Public IP:   $PUBLIC_IP
SSH key:     $SSH_KEY (stored securely in Cloud Shell)

Connect from Cloud Shell with:
  ssh -i "$SSH_KEY" ubuntu@$PUBLIC_IP

The next step is to install the TestForge production stack.
============================================================
EOF
