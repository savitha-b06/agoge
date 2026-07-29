#!/usr/bin/env bash
# One-shot setup on a fresh Ubuntu 24.04 VPS.
#   ssh root@YOUR_IP 'bash -s' < deploy/setup_vps.sh
set -euo pipefail

APP_USER="agoge"
APP_DIR="/home/${APP_USER}/agoge"

echo "==> packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git ufw

echo "==> user"
id -u "$APP_USER" &>/dev/null || useradd -m -s /bin/bash "$APP_USER"

echo "==> firewall (ssh only — nothing here needs to be reachable)"
ufw allow OpenSSH
ufw --force enable

echo "==> app"
sudo -u "$APP_USER" bash <<'INNER'
set -euo pipefail
cd "$HOME"
[ -d agoge ] || git clone https://github.com/YOURNAME/agoge.git agoge
cd agoge
python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -e .
[ -f .env ] || cp .env.example .env
[ -f athlete.yaml ] || cp athlete.example.yaml athlete.yaml
mkdir -p data && chmod 700 data
INNER

echo
echo "Done. Now, as the ${APP_USER} user:"
echo "  1. edit ~/agoge/.env         (ANTHROPIC_API_KEY)"
echo "  2. edit ~/agoge/athlete.yaml (zones, injuries, blocks)"
echo "  3. run  ~/agoge/.venv/bin/agoge coros tools   to authorise COROS"
echo "  4. crontab -e  and paste deploy/crontab.example"
