#!/usr/bin/env bash
#
# setup-aws.sh — Configure AWS (Elastic IP + Security Group) for nousergon.ai
#
# What this script does:
#   1. Checks for an existing Elastic IP on your EC2 instance
#   2. Allocates and associates one if missing
#   3. Configures security group: opens ports 80 + 443, removes direct 8501/8502
#
# Prerequisites:
#   - AWS CLI installed and configured (aws sts get-caller-identity works)
#   - Your EC2 instance ID
#
# Usage:
#   chmod +x setup-aws.sh
#   ./setup-aws.sh
#
set -euo pipefail

export PATH="/opt/homebrew/bin:$PATH"

# ── Colors ────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}▶${NC} $1"; }
ok()    { echo -e "${GREEN}✓${NC} $1"; }
warn()  { echo -e "${YELLOW}!${NC} $1"; }
err()   { echo -e "${RED}✗${NC} $1"; exit 1; }

# ── Dependency check ──────────────────────────────────────────────────────
command -v aws >/dev/null 2>&1 || err "AWS CLI is required but not found. Is /opt/homebrew/bin in PATH?"
command -v jq  >/dev/null 2>&1 || err "jq is required (brew install jq)"

# ── Verify AWS credentials ───────────────────────────────────────────────
info "Verifying AWS credentials..."
CALLER=$(aws sts get-caller-identity 2>/dev/null) || err "AWS credentials not configured. Run: aws configure"
ACCOUNT=$(echo "$CALLER" | jq -r '.Account')
ok "AWS Account: ${ACCOUNT}"

# ── Collect inputs ────────────────────────────────────────────────────────
echo ""
echo -e "${CYAN}═══════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  AWS Setup for nousergon.ai${NC}"
echo -e "${CYAN}═══════════════════════════════════════════════════${NC}"
echo ""

# ── Load secrets file if it exists ────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SECRETS_FILE="${SCRIPT_DIR}/secrets.env"

if [[ -f "$SECRETS_FILE" ]]; then
  info "Loading secrets from ${SECRETS_FILE}"
  set -a
  source "$SECRETS_FILE"
  set +a
fi

# Use EC2_INSTANCE_ID from secrets, prompt if missing
INSTANCE_ID="${EC2_INSTANCE_ID:-}"
[[ -z "$INSTANCE_ID" ]] && read -p "EC2 Instance ID (e.g., i-0abc123def456): " INSTANCE_ID
[[ -z "$INSTANCE_ID" ]] && err "EC2_INSTANCE_ID is required"

# Get the region from the instance or default config
REGION=$(aws configure get region 2>/dev/null || echo "us-east-1")
info "Using AWS region: ${REGION}"

# ── Step 1: Elastic IP ───────────────────────────────────────────────────
echo ""
info "Step 1: Checking Elastic IP..."

# Check if instance already has an Elastic IP
EXISTING_EIP=$(aws ec2 describe-addresses \
  --filters "Name=instance-id,Values=${INSTANCE_ID}" \
  --region "${REGION}" \
  --query 'Addresses[0].PublicIp' \
  --output text 2>/dev/null)

if [[ "$EXISTING_EIP" != "None" && -n "$EXISTING_EIP" ]]; then
  ok "Instance already has Elastic IP: ${EXISTING_EIP}"
  EIP="$EXISTING_EIP"
else
  info "No Elastic IP found. Allocating one..."

  ALLOC_RESULT=$(aws ec2 allocate-address \
    --domain vpc \
    --region "${REGION}" \
    --output json)

  ALLOC_ID=$(echo "$ALLOC_RESULT" | jq -r '.AllocationId')
  EIP=$(echo "$ALLOC_RESULT" | jq -r '.PublicIp')
  ok "Allocated Elastic IP: ${EIP} (${ALLOC_ID})"

  info "Associating with instance ${INSTANCE_ID}..."
  aws ec2 associate-address \
    --instance-id "${INSTANCE_ID}" \
    --allocation-id "${ALLOC_ID}" \
    --region "${REGION}" >/dev/null

  ok "Elastic IP ${EIP} associated with ${INSTANCE_ID}"
fi

# ── Step 2: Security Group ───────────────────────────────────────────────
echo ""
info "Step 2: Configuring security group..."

# Get the security group(s) for this instance
SG_IDS=$(aws ec2 describe-instances \
  --instance-ids "${INSTANCE_ID}" \
  --region "${REGION}" \
  --query 'Reservations[0].Instances[0].SecurityGroups[*].GroupId' \
  --output text)

[[ -z "$SG_IDS" ]] && err "Could not find security groups for instance ${INSTANCE_ID}"

# Use the first security group
SG_ID=$(echo "$SG_IDS" | awk '{print $1}')
ok "Security group: ${SG_ID}"

# Helper: add ingress rule if not exists
add_rule() {
  local port="$1" protocol="$2" cidr="$3" desc="$4"

  # Check if rule already exists
  EXISTING=$(aws ec2 describe-security-group-rules \
    --filters "Name=group-id,Values=${SG_ID}" \
    --region "${REGION}" \
    --query "SecurityGroupRules[?FromPort==\`${port}\` && ToPort==\`${port}\` && CidrIpv4==\`${cidr}\`] | length(@)" \
    --output text 2>/dev/null)

  if [[ "$EXISTING" -gt 0 ]]; then
    ok "Port ${port} from ${cidr} already open — skipping"
    return 0
  fi

  aws ec2 authorize-security-group-ingress \
    --group-id "${SG_ID}" \
    --protocol "${protocol}" \
    --port "${port}" \
    --cidr "${cidr}" \
    --region "${REGION}" >/dev/null 2>&1 && \
    ok "Opened port ${port} from ${cidr} (${desc})" || \
    warn "Port ${port} rule may already exist or failed to add"
}

# Helper: revoke ingress rule if exists
revoke_rule() {
  local port="$1" cidr="$2"

  EXISTING=$(aws ec2 describe-security-group-rules \
    --filters "Name=group-id,Values=${SG_ID}" \
    --region "${REGION}" \
    --query "SecurityGroupRules[?FromPort==\`${port}\` && ToPort==\`${port}\` && CidrIpv4==\`${cidr}\`] | length(@)" \
    --output text 2>/dev/null)

  if [[ "$EXISTING" -gt 0 ]]; then
    aws ec2 revoke-security-group-ingress \
      --group-id "${SG_ID}" \
      --protocol tcp \
      --port "${port}" \
      --cidr "${cidr}" \
      --region "${REGION}" >/dev/null 2>&1 && \
      ok "Removed port ${port} from ${cidr}" || \
      warn "Could not remove port ${port} rule"
  fi
}

# Open 80 and 443 for Cloudflare
add_rule 443 tcp "0.0.0.0/0" "HTTPS (Cloudflare)"
add_rule 80  tcp "0.0.0.0/0" "HTTP redirect"

# Remove direct Streamlit port exposure if present
revoke_rule 8501 "0.0.0.0/0"
revoke_rule 8502 "0.0.0.0/0"

# ── Summary ───────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  AWS setup complete!${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
echo ""
echo "  Elastic IP:     ${EIP}"
echo "  Security Group: ${SG_ID}"
echo "  Port 443:       open (HTTPS)"
echo "  Port 80:        open (HTTP redirect)"
echo "  Port 8501:      closed (Nginx only)"
echo "  Port 8502:      closed (Nginx only)"
echo ""
echo "  Use this IP in the Cloudflare setup: ${EIP}"
echo ""
