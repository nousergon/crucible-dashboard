#!/usr/bin/env bash
#
# setup-cloudflare.sh — Automate Cloudflare setup for nousergon.ai
#
# What this script does:
#   1. Creates DNS records (A + CNAMEs for dashboard and www)
#   2. Sets SSL mode to Full (strict)
#   3. Generates a Cloudflare Origin CA certificate (saves to local files)
#   4. Creates a Cloudflare Access application for dashboard.nousergon.ai
#   5. Creates an Access policy to whitelist your email
#
# Prerequisites:
#   - nousergon.ai registered on Cloudflare
#   - Cloudflare API token with permissions:
#       Zone > DNS > Edit
#       Zone > SSL and Certificates > Edit
#       Account > Access: Apps and Policies > Edit
#   - Your Cloudflare Origin CA Key (Profile > API Tokens > Origin CA Key)
#   - curl and jq installed
#
# Usage:
#   chmod +x setup-cloudflare.sh
#   ./setup-cloudflare.sh
#
set -euo pipefail

# ── Colors ────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

info()  { echo -e "${CYAN}▶${NC} $1"; }
ok()    { echo -e "${GREEN}✓${NC} $1"; }
warn()  { echo -e "${YELLOW}!${NC} $1"; }
err()   { echo -e "${RED}✗${NC} $1"; exit 1; }

# ── Dependency check ──────────────────────────────────────────────────────
command -v curl >/dev/null 2>&1 || err "curl is required but not installed"
command -v jq   >/dev/null 2>&1 || err "jq is required (brew install jq)"

# ── Collect inputs ────────────────────────────────────────────────────────
echo ""
echo -e "${CYAN}═══════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  Cloudflare Setup for nousergon.ai${NC}"
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
else
  warn "No secrets.env found. Copy secrets.env.example → secrets.env and fill in values."
  warn "Falling back to interactive prompts."
fi

# Prompt for any missing values
[[ -z "${CF_API_TOKEN:-}" ]] && read -p "Cloudflare API Token: " CF_API_TOKEN
[[ -z "$CF_API_TOKEN" ]] && err "CF_API_TOKEN is required"

[[ -z "${CF_ORIGIN_CA_KEY:-}" ]] && read -p "Cloudflare Origin CA Key (Profile > API Tokens): " CF_ORIGIN_CA_KEY
[[ -z "$CF_ORIGIN_CA_KEY" ]] && err "CF_ORIGIN_CA_KEY is required"

[[ -z "${EC2_IP:-}" ]] && read -p "EC2 Elastic IP address: " EC2_IP
[[ -z "$EC2_IP" ]] && err "EC2_IP is required"

[[ -z "${AUTH_EMAIL:-}" ]] && read -p "Your email (for Cloudflare Access whitelist): " AUTH_EMAIL
[[ -z "$AUTH_EMAIL" ]] && err "AUTH_EMAIL is required"

DOMAIN="nousergon.ai"
CF_API="https://api.cloudflare.com/client/v4"
AUTH_HEADER="Authorization: Bearer ${CF_API_TOKEN}"

# ── Get Zone ID ───────────────────────────────────────────────────────────
info "Looking up Zone ID for ${DOMAIN}..."
ZONE_RESPONSE=$(curl -s -X GET "${CF_API}/zones?name=${DOMAIN}" \
  -H "${AUTH_HEADER}" \
  -H "Content-Type: application/json")

ZONE_ID=$(echo "$ZONE_RESPONSE" | jq -r '.result[0].id // empty')
ACCOUNT_ID=$(echo "$ZONE_RESPONSE" | jq -r '.result[0].account.id // empty')

[[ -z "$ZONE_ID" ]] && err "Could not find zone for ${DOMAIN}. Check your API token permissions."
ok "Zone ID: ${ZONE_ID}"
ok "Account ID: ${ACCOUNT_ID}"

# ── Helper: create DNS record (skip if exists) ───────────────────────────
create_dns_record() {
  local type="$1" name="$2" content="$3"

  # Check if record already exists
  EXISTING=$(curl -s -X GET "${CF_API}/zones/${ZONE_ID}/dns_records?type=${type}&name=${name}" \
    -H "${AUTH_HEADER}" \
    -H "Content-Type: application/json" | jq -r '.result | length')

  if [[ "$EXISTING" -gt 0 ]]; then
    warn "DNS ${type} record for ${name} already exists — skipping"
    return 0
  fi

  RESULT=$(curl -s -X POST "${CF_API}/zones/${ZONE_ID}/dns_records" \
    -H "${AUTH_HEADER}" \
    -H "Content-Type: application/json" \
    --data "{
      \"type\": \"${type}\",
      \"name\": \"${name}\",
      \"content\": \"${content}\",
      \"ttl\": 1,
      \"proxied\": true
    }")

  SUCCESS=$(echo "$RESULT" | jq -r '.success')
  if [[ "$SUCCESS" == "true" ]]; then
    ok "Created DNS ${type} record: ${name} → ${content} (proxied)"
  else
    ERRORS=$(echo "$RESULT" | jq -r '.errors[0].message // "unknown error"')
    err "Failed to create DNS ${type} record for ${name}: ${ERRORS}"
  fi
}

# ── Step 1: DNS Records ──────────────────────────────────────────────────
echo ""
info "Step 1: Creating DNS records..."

create_dns_record "A"     "${DOMAIN}"               "${EC2_IP}"
create_dns_record "CNAME" "dashboard.${DOMAIN}"     "${DOMAIN}"
create_dns_record "CNAME" "www.${DOMAIN}"           "${DOMAIN}"

# ── Step 2: SSL Mode → Full (strict) ─────────────────────────────────────
echo ""
info "Step 2: Setting SSL mode to Full (strict)..."

SSL_RESULT=$(curl -s -X PATCH "${CF_API}/zones/${ZONE_ID}/settings/ssl" \
  -H "${AUTH_HEADER}" \
  -H "Content-Type: application/json" \
  --data '{"value": "strict"}')

SSL_SUCCESS=$(echo "$SSL_RESULT" | jq -r '.success')
if [[ "$SSL_SUCCESS" == "true" ]]; then
  ok "SSL mode set to Full (strict)"
else
  # Try the "full" value as fallback — some plans require setting via the dashboard
  SSL_RESULT2=$(curl -s -X PATCH "${CF_API}/zones/${ZONE_ID}/settings/ssl" \
    -H "${AUTH_HEADER}" \
    -H "Content-Type: application/json" \
    --data '{"value": "full"}')
  SSL_SUCCESS2=$(echo "$SSL_RESULT2" | jq -r '.success')
  if [[ "$SSL_SUCCESS2" == "true" ]]; then
    ok "SSL mode set to Full (upgrade to strict manually: SSL/TLS → Overview in dashboard)"
  else
    warn "Could not set SSL mode via API."
    warn "Set it manually: Cloudflare dashboard → nousergon.ai → SSL/TLS → Overview → Full (strict)"
  fi
fi

# ── Step 3: Origin CA Certificate ─────────────────────────────────────────
echo ""
info "Step 3: Generating Origin CA certificate..."

CERT_DIR="$(cd "$(dirname "$0")" && pwd)/certs"
mkdir -p "${CERT_DIR}"

# Generate a local private key and CSR — Cloudflare API requires a real CSR
info "Generating RSA private key and CSR..."
openssl req -new -newkey rsa:2048 -nodes \
  -keyout "${CERT_DIR}/cloudflare-origin.key" \
  -out "${CERT_DIR}/cloudflare-origin.csr" \
  -subj "/CN=${DOMAIN}" 2>/dev/null

chmod 600 "${CERT_DIR}/cloudflare-origin.key"
CSR_CONTENT=$(cat "${CERT_DIR}/cloudflare-origin.csr")

# Escape the CSR for JSON (newlines → \n)
CSR_ESCAPED=$(echo "$CSR_CONTENT" | awk '{printf "%s\\n", $0}')

CERT_RESPONSE=$(curl -s -X POST "${CF_API}/certificates" \
  -H "X-Auth-User-Service-Key: ${CF_ORIGIN_CA_KEY}" \
  -H "Content-Type: application/json" \
  --data "{
    \"hostnames\": [\"${DOMAIN}\", \"*.${DOMAIN}\"],
    \"requested_validity\": 5475,
    \"request_type\": \"origin-rsa\",
    \"csr\": \"${CSR_ESCAPED}\"
  }")

CERT_SUCCESS=$(echo "$CERT_RESPONSE" | jq -r '.success')
if [[ "$CERT_SUCCESS" == "true" ]]; then
  echo "$CERT_RESPONSE" | jq -r '.result.certificate' > "${CERT_DIR}/cloudflare-origin.pem"
  rm -f "${CERT_DIR}/cloudflare-origin.csr"
  ok "Origin CA certificate saved to:"
  ok "  Certificate: ${CERT_DIR}/cloudflare-origin.pem"
  ok "  Private key: ${CERT_DIR}/cloudflare-origin.key"
else
  ERRORS=$(echo "$CERT_RESPONSE" | jq -r '.errors[0].message // "unknown error"')
  warn "API cert generation failed: ${ERRORS}"
  warn ""
  warn "Falling back to manual generation. Follow these steps:"
  warn "  1. Go to: https://dash.cloudflare.com → nousergon.ai → SSL/TLS → Origin Server"
  warn "  2. Click 'Create Certificate'"
  warn "  3. Key type: RSA (2048), Hostnames: nousergon.ai + *.nousergon.ai, Validity: 15 years"
  warn "  4. Click Create"
  warn "  5. Save the certificate as: ${CERT_DIR}/cloudflare-origin.pem"
  warn "  6. Save the private key as:  ${CERT_DIR}/cloudflare-origin.key"
  warn "  7. IMPORTANT: The private key is only shown once!"
  warn ""
  read -p "Press Enter once you've saved both files to continue..." _
  if [[ ! -f "${CERT_DIR}/cloudflare-origin.pem" || ! -f "${CERT_DIR}/cloudflare-origin.key" ]]; then
    err "Certificate files not found in ${CERT_DIR}/ — cannot continue"
  fi
  chmod 600 "${CERT_DIR}/cloudflare-origin.key"
  ok "Certificate files found"
fi

# ── Step 4: Cloudflare Access Application ─────────────────────────────────
echo ""
info "Step 4: Creating Cloudflare Access application for dashboard.${DOMAIN}..."

# Check if app already exists (handle null/empty .result gracefully)
APPS_RESPONSE=$(curl -s -X GET "${CF_API}/accounts/${ACCOUNT_ID}/access/apps" \
  -H "${AUTH_HEADER}" \
  -H "Content-Type: application/json")

EXISTING_APPS=$(echo "$APPS_RESPONSE" | jq -r ".result // [] | .[] | select(.domain == \"dashboard.${DOMAIN}\") | .id" 2>/dev/null || echo "")

if [[ -n "$EXISTING_APPS" ]]; then
  APP_ID="$EXISTING_APPS"
  warn "Access application for dashboard.${DOMAIN} already exists (ID: ${APP_ID}) — skipping creation"
else
  APP_RESPONSE=$(curl -s -X POST "${CF_API}/accounts/${ACCOUNT_ID}/access/apps" \
    -H "${AUTH_HEADER}" \
    -H "Content-Type: application/json" \
    --data "{
      \"name\": \"Alpha Engine Dashboard\",
      \"domain\": \"dashboard.${DOMAIN}\",
      \"type\": \"self_hosted\",
      \"session_duration\": \"720h\",
      \"auto_redirect_to_identity\": false,
      \"app_launcher_visible\": true
    }")

  APP_SUCCESS=$(echo "$APP_RESPONSE" | jq -r '.success')
  if [[ "$APP_SUCCESS" == "true" ]]; then
    APP_ID=$(echo "$APP_RESPONSE" | jq -r '.result.id')
    ok "Access application created (ID: ${APP_ID})"
  else
    ERRORS=$(echo "$APP_RESPONSE" | jq -r '.errors[0].message // "unknown error"')
    warn "Failed to create Access application via API: ${ERRORS}"
    warn ""
    warn "This likely means your API token is missing the Access permission."
    warn "Set it up manually:"
    warn "  1. Go to: https://one.dash.cloudflare.com → Access → Applications"
    warn "  2. Add application → Self-hosted"
    warn "  3. Name: Alpha Engine Dashboard"
    warn "  4. Domain: dashboard.${DOMAIN}"
    warn "  5. Session duration: 30 days"
    warn "  6. Add policy: Allow → Include → Emails → ${AUTH_EMAIL}"
    warn "  7. Save"
    echo ""
    ok "DNS and certificate setup complete. Configure Access manually and you're done."
    exit 0
  fi
fi

# ── Step 5: Access Policy (email whitelist) ───────────────────────────────
echo ""
info "Step 5: Creating Access policy (allow ${AUTH_EMAIL})..."

# Check if policy already exists (handle null .result)
EXISTING_POLICIES=$(curl -s -X GET "${CF_API}/accounts/${ACCOUNT_ID}/access/apps/${APP_ID}/policies" \
  -H "${AUTH_HEADER}" \
  -H "Content-Type: application/json" | jq -r '.result // [] | length')

if [[ "$EXISTING_POLICIES" -gt 0 ]]; then
  warn "Access policy already exists — skipping (manage at: https://one.dash.cloudflare.com)"
else
  POLICY_RESPONSE=$(curl -s -X POST "${CF_API}/accounts/${ACCOUNT_ID}/access/apps/${APP_ID}/policies" \
    -H "${AUTH_HEADER}" \
    -H "Content-Type: application/json" \
    --data "{
      \"name\": \"Email whitelist\",
      \"decision\": \"allow\",
      \"precedence\": 1,
      \"include\": [
        {\"email\": {\"email\": \"${AUTH_EMAIL}\"}}
      ],
      \"exclude\": [],
      \"require\": []
    }")

  POLICY_SUCCESS=$(echo "$POLICY_RESPONSE" | jq -r '.success')
  if [[ "$POLICY_SUCCESS" == "true" ]]; then
    ok "Access policy created: allow ${AUTH_EMAIL}"
  else
    ERRORS=$(echo "$POLICY_RESPONSE" | jq -r '.errors[0].message // "unknown error"')
    warn "Failed to create Access policy: ${ERRORS}"
    warn "You may need to create the policy manually at: https://one.dash.cloudflare.com"
  fi
fi

# ── Summary ───────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Cloudflare setup complete!${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
echo ""
echo "  DNS:     ${DOMAIN} → ${EC2_IP} (proxied)"
echo "  DNS:     dashboard.${DOMAIN} → ${DOMAIN} (proxied)"
echo "  DNS:     www.${DOMAIN} → ${DOMAIN} (proxied)"
echo "  SSL:     Full (strict) with Origin CA cert"
echo "  Access:  dashboard.${DOMAIN} protected (${AUTH_EMAIL} whitelisted)"
echo ""
echo "  Certificate files (copy these to EC2 in the next step):"
echo "    ${CERT_DIR}/cloudflare-origin.pem"
echo "    ${CERT_DIR}/cloudflare-origin.key"
echo ""
echo "  Next: run setup-aws.sh to configure EC2"
echo ""
