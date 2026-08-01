# Personal AIVA MCP Bootstrap

A clean, self-hosted MCP server for a fresh Oracle Cloud Ubuntu VM. It gives ChatGPT controlled access to the user's own server through tools for shell commands, files, and personal skills.

## Give this repository to an agent

Tell the agent:

> Clone this repository on my Oracle Ubuntu server and follow AGENTS.md exactly. Do not stop until ChatGPT has scanned the MCP tools successfully.

## Manual install

```bash
git clone https://github.com/banddude/aiva-bootstrap.git
cd aiva-bootstrap
sudo ./install.sh
sudo tailscale up
sudo tailscale funnel --bg 8000
tailscale funnel status
```

Take the HTTPS Funnel address and append `/mcp`.

Example:

```text
https://oracle-name.example-tailnet.ts.net/mcp
```

## Connect to ChatGPT

Current ChatGPT setup:

1. Use ChatGPT on the web.
2. Open **Settings → Apps → Advanced Settings** and enable **Developer mode**.
3. Open **Settings → Apps → Create**.
4. Enter the Tailscale Funnel `/mcp` endpoint.
5. Select **No authentication**. The URL is an unlisted, user-controlled endpoint; do not share it.
6. Select **Scan tools**, then create the app.
7. Test by asking ChatGPT to call `health`.

Full write-capable MCP support may depend on the user's ChatGPT plan and workspace permissions. Pro accounts can have more limited custom-app capabilities than Business or Enterprise/Edu accounts.

## Installed tools

- `health`
- `run_command`
- `list_files`
- `read_file`
- `write_file`
- `get_file_base64`
- `list_skills`
- `get_skill`
- `save_skill`

## Security model

- The MCP process runs as a dedicated `aiva` Linux user.
- File tools are restricted to `/opt/aiva/workspace`.
- Skills are restricted to `/opt/aiva/skills`.
- The service has systemd hardening.
- The Oracle firewall does not need port 8000 exposed.
- HTTPS exposure is provided by Tailscale Funnel.

The `run_command` tool intentionally provides broad command execution inside the AIVA workspace. Only connect this server to a ChatGPT account you trust.

## Useful commands

```bash
sudo systemctl status aiva-mcp
sudo journalctl -u aiva-mcp -f
sudo systemctl restart aiva-mcp
tailscale funnel status
```
