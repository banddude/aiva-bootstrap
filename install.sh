#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Run as root, for example: sudo ./install.sh"
  exit 1
fi

AIVA_NAME="${AIVA_NAME:-aiva}"
AIVA_HOSTNAME="${AIVA_HOSTNAME:-${AIVA_NAME}-oracle}"
AIVA_HOME="/opt/${AIVA_NAME}"
AIVA_PORT="${AIVA_PORT:-8765}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl git jq python3 python3-venv openssl

if ! id "$AIVA_NAME" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$AIVA_HOME" --shell /bin/bash "$AIVA_NAME"
fi

install -d -o "$AIVA_NAME" -g "$AIVA_NAME" \
  "$AIVA_HOME/server" "$AIVA_HOME/workspace" "$AIVA_HOME/skills"
rsync_available=false
if command -v rsync >/dev/null 2>&1; then rsync_available=true; fi
if $rsync_available; then
  rsync -a --delete --exclude .git "$SOURCE_DIR/" "$AIVA_HOME/server/"
else
  rm -rf "$AIVA_HOME/server"/*
  cp -a "$SOURCE_DIR"/. "$AIVA_HOME/server/"
  rm -rf "$AIVA_HOME/server/.git"
fi
cp -n "$SOURCE_DIR/skills/start-here.md" "$AIVA_HOME/skills/start-here.md" || true
chown -R "$AIVA_NAME:$AIVA_NAME" "$AIVA_HOME"

python3 -m venv "$AIVA_HOME/venv"
"$AIVA_HOME/venv/bin/pip" install --upgrade pip
"$AIVA_HOME/venv/bin/pip" install -r "$AIVA_HOME/server/requirements.txt"

cat >"$AIVA_HOME/.env" <<ENV
AIVA_HOME=$AIVA_HOME
AIVA_WORKSPACE=$AIVA_HOME/workspace
AIVA_SKILLS=$AIVA_HOME/skills
PORT=$AIVA_PORT
ENV
chmod 600 "$AIVA_HOME/.env"
chown "$AIVA_NAME:$AIVA_NAME" "$AIVA_HOME/.env"

sed \
  -e "s|User=aiva|User=$AIVA_NAME|" \
  -e "s|Group=aiva|Group=$AIVA_NAME|" \
  -e "s|/opt/aiva|$AIVA_HOME|g" \
  "$SOURCE_DIR/scripts/aiva-mcp.service" >"/etc/systemd/system/${AIVA_NAME}-mcp.service"

systemctl daemon-reload
systemctl enable --now "${AIVA_NAME}-mcp.service"

for _ in $(seq 1 30); do
  if curl -sS -o /dev/null "http://127.0.0.1:${AIVA_PORT}/mcp"; then break; fi
  sleep 1
done
if ! systemctl is-active --quiet "${AIVA_NAME}-mcp.service"; then
  systemctl status "${AIVA_NAME}-mcp.service" --no-pager || true
  exit 1
fi

if ! command -v tailscale >/dev/null 2>&1; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi
systemctl enable --now tailscaled

cat >"$AIVA_HOME/finish-setup.sh" <<'FINISH'
#!/usr/bin/env bash
set -euo pipefail
AIVA_NAME="${AIVA_NAME:-aiva}"
AIVA_HOME="/opt/${AIVA_NAME}"
AIVA_PORT="${AIVA_PORT:-8765}"
AIVA_HOSTNAME="${AIVA_HOSTNAME:-${AIVA_NAME}-oracle}"

if ! tailscale status >/dev/null 2>&1; then
  echo "Tailscale needs one browser login. Open the URL printed below, approve this Oracle, then run this same command again:"
  tailscale up --hostname "$AIVA_HOSTNAME" || true
  exit 2
fi

if [[ ! -f "$AIVA_HOME/mcp-path" ]]; then
  umask 077
  printf 'mcp-%s\n' "$(openssl rand -hex 16)" >"$AIVA_HOME/mcp-path"
fi
MCP_PATH="$(cat "$AIVA_HOME/mcp-path")"

tailscale funnel --bg --yes --set-path "/${MCP_PATH}" "http://127.0.0.1:${AIVA_PORT}"
DNS_NAME="$(tailscale status --json | jq -r '.Self.DNSName' | sed 's/\.$//')"
MCP_URL="https://${DNS_NAME}/${MCP_PATH}/mcp"
printf '%s\n' "$MCP_URL" >"$AIVA_HOME/mcp-url"
chmod 600 "$AIVA_HOME/mcp-path" "$AIVA_HOME/mcp-url"

echo
echo "AIVA MCP IS READY"
echo "MCP URL: $MCP_URL"
echo
echo "Paste that URL into ChatGPT as a custom MCP app, scan tools, then test the health tool."
FINISH
chmod 700 "$AIVA_HOME/finish-setup.sh"

set +e
AIVA_NAME="$AIVA_NAME" AIVA_PORT="$AIVA_PORT" AIVA_HOSTNAME="$AIVA_HOSTNAME" "$AIVA_HOME/finish-setup.sh"
finish_status=$?
set -e

if [[ $finish_status -eq 2 ]]; then
  echo
echo "Installation is complete. After approving the Tailscale browser login, finish with:"
  echo "  sudo $AIVA_HOME/finish-setup.sh"
  exit 0
elif [[ $finish_status -ne 0 ]]; then
  exit "$finish_status"
fi
