# Personal AIVA MCP Bootstrap

One command turns a fresh Oracle Cloud Ubuntu VM into a personal MCP server that ChatGPT can use to run commands, manage files, and maintain personal skills.

## One-line install

SSH into the Oracle VM and run:

```bash
curl -fsSL https://raw.githubusercontent.com/banddude/aiva-bootstrap/main/bootstrap.sh | sudo bash
```

The installer handles Python, the MCP service, Tailscale, startup services, and an unguessable MCP URL path.

Tailscale requires one browser approval. Open the login URL printed by the installer. If the script has already returned, run:

```bash
sudo /opt/aiva/finish-setup.sh
```

It prints the final MCP URL. Paste that URL into ChatGPT when creating a custom MCP app, scan the tools, and call `health`.

## Give this repository to an agent

Send the agent this repository URL:

```text
https://github.com/banddude/aiva-bootstrap
```

Tell it:

> Follow AGENTS.md exactly. Install this on my Oracle VM and do not stop until the final MCP URL is printed and ChatGPT can call the health and run_command tools.

## Tools

- `health`
- `run_command`
- `list_files`
- `read_file`
- `write_file`
- `get_file` (returns an embedded MCP resource, including binary files)
- `list_skills`
- `get_skill`
- `save_skill`

## Isolation and exposure

- Runs as a dedicated `aiva` Linux user.
- File tools stay under `/opt/aiva/workspace`.
- Personal skills stay under `/opt/aiva/skills`.
- The MCP port is not exposed through Oracle's public firewall.
- Tailscale Funnel supplies HTTPS.
- The public endpoint includes a randomly generated 128-bit path. Treat the full URL as a password and do not post it publicly.

`run_command` intentionally gives broad command access as the `aiva` Linux user. Connect it only to the owner's ChatGPT account.

## Service commands

```bash
sudo systemctl status aiva-mcp
sudo journalctl -u aiva-mcp -f
sudo systemctl restart aiva-mcp
sudo cat /opt/aiva/mcp-url
```
