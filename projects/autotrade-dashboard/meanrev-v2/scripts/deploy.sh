#!/bin/bash
# MeanRev AutoTrader v2 — VPS Deploy Script
# Usage: chmod +x scripts/deploy.sh && ./scripts/deploy.sh
set -e
G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; NC='\033[0m'
echo -e "${G}╔══════════════════════════════════════════╗"
echo    "║  MeanRev AutoTrader v2 — VPS Deploy      ║"
echo -e "╚══════════════════════════════════════════╝${NC}"

echo -e "${Y}[1/7] System update...${NC}"
sudo apt-get update -qq && sudo apt-get upgrade -y -qq

echo -e "${Y}[2/7] Installing Node.js 20...${NC}"
if ! command -v node &>/dev/null; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
  sudo apt-get install -y nodejs -qq
fi
echo "  Node $(node -v)  npm $(npm -v)"

echo -e "${Y}[3/7] Installing PM2 + Nginx...${NC}"
sudo npm install -g pm2 -q
sudo apt-get install -y nginx -qq

echo -e "${Y}[4/7] Installing dependencies...${NC}"
cd "$(dirname "$0")/.."
npm install --production

echo -e "${Y}[5/7] Environment setup...${NC}"
[ ! -f .env ] && cp .env.example .env

echo -e "${Y}[6/7] Creating logs dir...${NC}"
mkdir -p logs

echo -e "${Y}[7/7] Starting with PM2...${NC}"
pm2 start ecosystem.config.js
pm2 save
pm2 startup | grep "sudo" | bash || true

echo ""
echo -e "${G}✓ Deployed!${NC}"
VPS_IP=$(curl -s ifconfig.me 2>/dev/null || echo "YOUR_IP")
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  App:       http://${VPS_IP}:3000"
echo "  Health:    http://${VPS_IP}:3000/health"
echo "  PM2 logs:  pm2 logs meanrev-v2"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo -e "${Y}Setup Nginx on port 80:${NC}"
echo "  sudo cp config/nginx.conf /etc/nginx/sites-available/meanrev"
echo "  # Edit server_name in the config"
echo "  sudo ln -s /etc/nginx/sites-available/meanrev /etc/nginx/sites-enabled/"
echo "  sudo nginx -t && sudo systemctl reload nginx"
echo ""
echo -e "${Y}Optional — Free SSL:${NC}"
echo "  sudo apt install certbot python3-certbot-nginx -y"
echo "  sudo certbot --nginx -d your-domain.com"
