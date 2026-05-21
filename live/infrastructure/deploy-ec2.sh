#!/usr/bin/env bash
#
# deploy-ec2.sh — Deploy Nous Ergon live console + Nginx to EC2
#
# Run this ON the EC2 instance after:
#   1. setup-aws.sh (local) — Elastic IP + security group
#   2. setup-cloudflare.sh (local) — DNS + Origin CA cert + Access
#   3. Cert files copied to EC2 (scp)
#
# What this script does:
#   1. Pulls latest dashboard code
#   2. Installs dependencies
#   3. Installs Origin CA certificate
#   4. Configures Nginx
#   5. Sets up systemd service for the live console
#   6. Starts all services
#   7. Runs smoke tests
#
# Usage:
#   chmod +x deploy-ec2.sh
#   ./deploy-ec2.sh
#
set -euo pipefail

# ── Colors ────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}▶${NC} $1"; }
ok()    { echo -e "${GREEN}✓${NC} $1"; }
warn()  { echo -e "${YELLOW}!${NC} $1"; }
fail()  { echo -e "${RED}✗${NC} $1"; }

DASHBOARD_DIR="/home/ec2-user/alpha-engine-dashboard"
LIVE_DIR="${DASHBOARD_DIR}/live"
CERT_STAGING="/tmp"

echo ""
echo -e "${CYAN}═══════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  Deploying Nous Ergon to EC2${NC}"
echo -e "${CYAN}═══════════════════════════════════════════════════${NC}"
echo ""

# ── Step 1: Pull latest code ─────────────────────────────────────────────
info "Step 1: Pulling latest dashboard code..."
cd "${DASHBOARD_DIR}"
git pull
ok "Code updated"

# ── Step 2: Install dependencies ─────────────────────────────────────────
info "Step 2: Installing Python dependencies..."
source "${DASHBOARD_DIR}/.venv/bin/activate"
pip install -q -r requirements.txt
deactivate
ok "Dependencies installed"

# ── Step 3: Origin CA certificate ─────────────────────────────────────────
info "Step 3: Installing Origin CA certificate..."

if [[ -f "${CERT_STAGING}/cloudflare-origin.pem" && -f "${CERT_STAGING}/cloudflare-origin.key" ]]; then
  sudo mkdir -p /etc/ssl/certs /etc/ssl/private
  sudo cp "${CERT_STAGING}/cloudflare-origin.pem" /etc/ssl/certs/cloudflare-origin.pem
  sudo cp "${CERT_STAGING}/cloudflare-origin.key" /etc/ssl/private/cloudflare-origin.key
  sudo chmod 644 /etc/ssl/certs/cloudflare-origin.pem
  sudo chmod 600 /etc/ssl/private/cloudflare-origin.key
  rm -f "${CERT_STAGING}/cloudflare-origin.pem" "${CERT_STAGING}/cloudflare-origin.key"
  ok "Certificate installed"
elif [[ -f "/etc/ssl/certs/cloudflare-origin.pem" ]]; then
  ok "Certificate already installed — skipping"
else
  fail "Certificate files not found!"
  echo ""
  echo "  Copy them from your local machine first:"
  echo "    scp certs/cloudflare-origin.pem  ec2-user@<ec2-host>:/tmp/"
  echo "    scp certs/cloudflare-origin.key  ec2-user@<ec2-host>:/tmp/"
  echo ""
  echo "  Then re-run this script."
  exit 1
fi

# ── Step 4: Nginx ─────────────────────────────────────────────────────────
info "Step 4: Configuring Nginx..."

# Install Nginx if not present
if ! command -v nginx >/dev/null 2>&1; then
  info "Installing Nginx..."
  sudo yum install -y nginx >/dev/null 2>&1 || sudo apt install -y nginx >/dev/null 2>&1
  ok "Nginx installed"
else
  ok "Nginx already installed"
fi

# Deploy config
sudo cp "${LIVE_DIR}/infrastructure/nginx.conf" /etc/nginx/conf.d/nousergon.conf

# Remove conflicting default configs
sudo rm -f /etc/nginx/conf.d/default.conf
# Remove old self-signed dashboard config if it exists
for f in /etc/nginx/conf.d/dashboard.conf /etc/nginx/conf.d/streamlit.conf; do
  [[ -f "$f" ]] && sudo rm -f "$f" && warn "Removed old config: $f"
done

# Test config
if sudo nginx -t 2>&1 | grep -q "successful"; then
  ok "Nginx config valid"
else
  fail "Nginx config test failed:"
  sudo nginx -t
  exit 1
fi

sudo systemctl enable nginx >/dev/null 2>&1
sudo systemctl restart nginx
ok "Nginx restarted"

# ── Step 5: Public site systemd service ───────────────────────────────────
info "Step 5: Setting up live console service..."

sudo cp "${LIVE_DIR}/infrastructure/nous-ergon-live.service" /etc/systemd/system/nous-ergon-live.service
sudo systemctl daemon-reload
sudo systemctl enable nous-ergon-live >/dev/null 2>&1
sudo systemctl restart nous-ergon-live
ok "Public site service started"

# ── Step 6: Ensure private dashboard is running ──────────────────────────
info "Step 6: Checking private dashboard..."

if sudo systemctl is-active --quiet dashboard; then
  ok "Private dashboard running on :8501"
else
  warn "Private dashboard not running — starting it..."
  sudo systemctl start dashboard
  if sudo systemctl is-active --quiet dashboard; then
    ok "Private dashboard started"
  else
    fail "Could not start private dashboard — check: sudo journalctl -u dashboard"
  fi
fi

# ── Step 7: Smoke tests ──────────────────────────────────────────────────
echo ""
info "Step 7: Running smoke tests..."

sleep 3  # Give Streamlit a moment to start

PASS=true

# Test live console app
if curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8502 | grep -q "200"; then
  ok "Live console responding on :8502"
else
  fail "Live console NOT responding on :8502"
  PASS=false
fi

# Test private dashboard
if curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8501 | grep -q "200"; then
  ok "Private dashboard responding on :8501"
else
  fail "Private dashboard NOT responding on :8501"
  PASS=false
fi

# Test Nginx
if curl -s -o /dev/null -w "%{http_code}" -k https://127.0.0.1 | grep -q "200\|301\|302"; then
  ok "Nginx responding on :443"
else
  fail "Nginx NOT responding on :443"
  PASS=false
fi

# ── Summary ───────────────────────────────────────────────────────────────
echo ""
if [[ "$PASS" == true ]]; then
  echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
  echo -e "${GREEN}  Deployment complete!${NC}"
  echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
  echo ""
  echo "  Public site:     https://nousergon.ai"
  echo "  Dashboard:       https://dashboard.nousergon.ai"
  echo ""
  echo "  Services:"
  echo "    nous-ergon-live  → port 8502 (live console)"
  echo "    dashboard          → port 8501 (private)"
  echo "    nginx              → port 443  (reverse proxy)"
  echo ""
  echo "  Logs:"
  echo "    sudo journalctl -u nous-ergon-live -f"
  echo "    sudo journalctl -u dashboard -f"
  echo "    sudo tail -f /var/log/nginx/error.log"
else
  echo -e "${YELLOW}═══════════════════════════════════════════════════${NC}"
  echo -e "${YELLOW}  Deployment finished with warnings${NC}"
  echo -e "${YELLOW}═══════════════════════════════════════════════════${NC}"
  echo ""
  echo "  Some smoke tests failed. Check the logs:"
  echo "    sudo journalctl -u nous-ergon-live --no-pager -n 30"
  echo "    sudo journalctl -u dashboard --no-pager -n 30"
  echo "    sudo tail -20 /var/log/nginx/error.log"
fi
echo ""
