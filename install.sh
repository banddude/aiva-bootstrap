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

# The bearer token every MCP request must present.
#
# Reuse an existing one when re-running the installer. Rotating it here would
# silently break the ChatGPT connector the operator already set up, and they
# would have no idea why their app stopped working. Rotation is a deliberate
# act, documented in the README.
if [[ -f /opt/aiva/.env ]] && grep -q '^AIVA_TOKEN=.\+' /opt/aiva/.env; then
  AIVA_TOKEN="$(grep '^AIVA_TOKEN=' /opt/aiva/.env | head -n1 | cut -d= -f2-)"
  TOKEN_IS_NEW=0
else
  # 32 bytes from the kernel CSPRNG, hex encoded. Not a passphrase, not a uuid.
  AIVA_TOKEN="$(openssl rand -hex 32)"
  TOKEN_IS_NEW=1
fi

# Written before the file is readable by anyone else: create it empty with the
# right mode FIRST, then fill it, so the token is never briefly world readable.
install -m 600 -o aiva -g aiva /dev/null /opt/aiva/.env
cat >/opt/aiva/.env <<EOF
AIVA_HOME=/opt/aiva
AIVA_WORKSPACE=/opt/aiva/workspace
AIVA_SKILLS=/opt/aiva/skills
PORT=8000
AIVA_TOKEN=${AIVA_TOKEN}
EOF
chmod 600 /opt/aiva/.env
chown aiva:aiva /opt/aiva/.env

# The repo copy under /opt/aiva/server is world readable. Make sure a stray
# .env never rides along in it from the operator's working directory.
rm -f /opt/aiva/server/.env

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
echo
echo "================================================================"
if [[ "${TOKEN_IS_NEW}" -eq 1 ]]; then
  echo "  YOUR ACCESS TOKEN (new, copy it now)"
else
  echo "  YOUR ACCESS TOKEN (existing, reused from /opt/aiva/.env)"
fi
echo
echo "    ${AIVA_TOKEN}"
echo
echo "  In ChatGPT, when you create the app, choose Authentication:"
echo "  Custom / API key and paste this token. Do NOT choose"
echo "  No authentication. This server can run shell commands, so an"
echo "  unauthenticated Funnel URL is remote code execution for anyone"
echo "  who finds it. A Funnel hostname is not a secret."
echo
echo "  Stored at /opt/aiva/.env (mode 600, owner aiva). Read it again with:"
echo "    sudo grep AIVA_TOKEN /opt/aiva/.env"
echo "================================================================"
echo
echo "Verify auth is on. This MUST return 401:"
# printf, not echo: the curl format string contains a literal backslash-n that
# the operator needs to copy verbatim, and echo's handling of escapes is
# shell dependent (shellcheck SC2028).
printf "  curl -s -o /dev/null -w '%%{http_code}\\\\n' http://127.0.0.1:8000/mcp\n"
