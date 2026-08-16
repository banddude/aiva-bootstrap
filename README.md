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
- `get_file_base64`
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

## Development, tests, and the tool-contract parity gate

CI (`.github/workflows/ci.yml`) runs on every pull request and push to main:
lint (ruff), type check (mypy), a real test suite, and a tool-contract parity
gate. To run everything locally:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest tests -v
.venv/bin/python scripts/parity-check
```

The test suite is real, not mocked: it boots this repository's server on an
ephemeral port, connects a protocol-faithful machine agent over a real
WebSocket, and calls all twelve tools over real MCP HTTP. `AIVA_MCP_URL` is
pinned to a dead address (`http://127.0.0.1:9`) for the whole run, so no test
can pass because Cloudflare happened to be healthy. That exact mistake is how
a 2026-08-15 outage hid for ten hours, which is why the pin exists.

`contract/worker-tools.json` is the reference tool contract generated from the
Cloudflare Worker this server replaces. The server loads it at startup (a
drift is a boot failure, not a silent difference) and the parity gate verifies
the advertised names and input schemas against it exactly. The file is pinned
by `contract/worker-tools.SHA256`; the gate fails if the file was changed
without re-pinning, because a hand edit would otherwise move the server and
the gate together and pass silently. Never edit the contract by hand. If the
Worker contract really changed, regenerate both files where a token exists:

```bash
python scripts/parity-check --refresh   # regenerate contract + pin from the live Worker
python scripts/parity-check --live      # verify the file is not stale vs the live Worker
```

