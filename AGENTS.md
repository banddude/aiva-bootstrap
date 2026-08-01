# Agent Installation Contract

Your job is to turn the current fresh Oracle Ubuntu VM into the user's personal remote MCP server.

## Required procedure

1. Read this file and README.md completely.
2. Confirm the OS is Ubuntu 22.04 or 24.04 and that you have sudo access.
3. Run `sudo ./install.sh` from this repository.
4. Verify `systemctl is-active aiva-mcp` returns `active`.
5. Verify locally with `curl -i http://127.0.0.1:8000/mcp`. An MCP protocol response or method-related HTTP response confirms the route exists.
6. Run `sudo tailscale up`. If it prints an authentication URL, give that exact URL to the user and wait for them to approve it.
7. Run `sudo tailscale funnel --bg 8000`.
8. Run `tailscale funnel status` and extract the public HTTPS URL.
9. The final MCP endpoint is that URL plus `/mcp`.
10. Give the user exact current ChatGPT connection steps from the README.
11. In ChatGPT, have the user enable Developer Mode, create a custom app, enter the endpoint, choose no authentication, scan tools, and create it.
12. Test the `health`, `list_skills`, and `get_skill("start-here")` tools from ChatGPT.
13. Begin onboarding by interviewing the user and then update `start-here` using `save_skill`.

## Rules

- Never copy another person's credentials, memories, configuration, databases, or private skills into this installation.
- Never commit `.env`, OAuth tokens, SSH keys, databases, or generated personal skills.
- Do not expose port 8000 directly in Oracle security lists. Tailscale Funnel provides HTTPS.
- Do not stop after installing packages. The task is complete only when the public MCP endpoint is working and ChatGPT has successfully scanned the tools.
- If Tailscale requires human authentication, that is the only expected pause.
