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

## Your access token

`install.sh` generates a random 32-byte token and prints it at the end. It is
stored in `/opt/aiva/.env` (mode 600, owner `aiva`). Read it again any time:

```bash
sudo grep AIVA_TOKEN /opt/aiva/.env
```

Every request to `/mcp` must send it as `Authorization: Bearer <token>`.
Without it the server answers `401`. The server will not start at all if the
token is missing, because this repository must never run open.

To rotate it (revokes the existing ChatGPT connector, which then needs the new
value pasted in):

```bash
sudo sed -i "s/^AIVA_TOKEN=.*/AIVA_TOKEN=$(openssl rand -hex 32)/" /opt/aiva/.env
sudo systemctl restart aiva-mcp
sudo grep AIVA_TOKEN /opt/aiva/.env
```

## Connect to ChatGPT

Current ChatGPT setup:

1. Use ChatGPT on the web.
2. Open **Settings → Apps → Advanced Settings** and enable **Developer mode**.
3. Open **Settings → Apps → Create**.
4. Enter the Tailscale Funnel `/mcp` endpoint.
5. For authentication, choose the **API key / custom header** option and paste
   the token from `install.sh`. If the field asks for a full header value, use
   `Bearer <token>`; if it asks only for a key, paste the token alone.
   **Do not choose "No authentication".** See below.
6. Select **Scan tools**, then create the app.
7. Test by asking ChatGPT to call `health`.

### Why not "No authentication"

Earlier versions of this README said to pick **No authentication** on the
grounds that the Funnel URL is unlisted. That was wrong, and it is the reason
this section exists.

A Tailscale Funnel hostname is not a secret. It is derived from your machine
name and tailnet name, and because Funnel serves real HTTPS, the hostname is
published in public Certificate Transparency logs the moment the certificate is
issued. Anyone can watch those logs.

This server exposes `run_command`. An unauthenticated Funnel URL is therefore
anonymous remote code execution on your box for anyone who reads a CT log feed.
This is not hypothetical: an instance was reachable that way for roughly 15
minutes on 2026-07-31 before it was caught.

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

- **Every `/mcp` request requires a bearer token.** No token, wrong token, or
  wrong scheme returns `401`. The token is compared with `secrets.compare_digest`
  so a wrong guess cannot be recovered one character at a time by timing.
- **The server refuses to start without a token.** There is no anonymous
  fallback, so a missing `AIVA_TOKEN` is a loud failure rather than a silently
  open server.
- The MCP process runs as a dedicated `aiva` Linux user.
- File tools are restricted to `/opt/aiva/workspace`.
- Skills are restricted to `/opt/aiva/skills`.
- The service has systemd hardening.
- The Oracle firewall does not need port 8000 exposed.
- HTTPS exposure is provided by Tailscale Funnel.

The `run_command` tool intentionally provides broad command execution inside the AIVA workspace. Only connect this server to a ChatGPT account you trust.

### Check that auth is actually on

```bash
# MUST be 401
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/mcp

# MUST be 200
curl -s -o /dev/null -w '%{http_code}\n' \
  -X POST http://127.0.0.1:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H "Authorization: Bearer $(sudo grep '^AIVA_TOKEN=' /opt/aiva/.env | cut -d= -f2-)" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

If the first command returns anything other than `401`, stop and do not expose
the Funnel.

## Useful commands

```bash
sudo systemctl status aiva-mcp
sudo journalctl -u aiva-mcp -f
sudo systemctl restart aiva-mcp
tailscale funnel status
```
