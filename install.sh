#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Run with sudo: sudo ./install.sh"
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl git jq python3 python3-venv openssl ufw

if ! id aiva >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /opt/aiva --shell /bin/bash aiva
fi

install -d -o aiva -g aiva /opt/aiva/server /opt/aiva/workspace /opt/aiva/skills
cp -a . /opt/aiva/server/
cp -n skills/start-here.md /opt/aiva/skills/start-here.md || true
chown -R aiva:aiva /opt/aiva

python3 -m venv /opt/aiva/venv
/opt/aiva/venv/bin/pip install --upgrade pip
/opt/aiva/venv/bin/pip install -r /opt/aiva/server/requirements.txt

cat >/opt/aiva/.env <<EOF
AIVA_HOME=/opt/aiva
AIVA_WORKSPACE=/opt/aiva/workspace
AIVA_SKILLS=/opt/aiva/skills
PORT=8000
EOF
chmod 600 /opt/aiva/.env
chown aiva:aiva /opt/aiva/.env

cp scripts/aiva-mcp.service /etc/systemd/system/aiva-mcp.service
systemctl daemon-reload
systemctl enable --now aiva-mcp

if ! command -v tailscale >/dev/null 2>&1; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi

echo
echo "AIVA MCP is installed locally."
echo "1. Run: sudo tailscale up"
echo "2. Open the login link it prints."
echo "3. Run: sudo tailscale funnel --bg 8000"
echo "4. Run: tailscale funnel status"
echo
echo "Your ChatGPT MCP endpoint will be the HTTPS Funnel URL with /mcp appended."
echo "Example: https://your-machine.your-tailnet.ts.net/mcp"
