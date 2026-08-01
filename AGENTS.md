# Agent Installation Contract

Your job is to turn the current fresh Oracle Ubuntu VM into the user's personal remote MCP server.

## Required procedure

1. Read this file and README.md completely.
2. Confirm the OS is Ubuntu 22.04 or 24.04 and that you have sudo access.
3. Run `sudo ./install.sh` from this repository. It prints an access token at
   the end. Capture it and give it to the user; they need it in step 11.
4. Verify `systemctl is-active aiva-mcp` returns `active`.
5. Verify authentication is ON before exposing anything:
   - `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/mcp` MUST
     print `401`. Anything else means the server is running unauthenticated.
     **Stop and fix it. Do not run `tailscale funnel`.**
   - Then confirm the token works:
     ```bash
     curl -s -o /dev/null -w '%{http_code}\n' \
       -X POST http://127.0.0.1:8000/mcp \
       -H 'Content-Type: application/json' \
       -H 'Accept: application/json, text/event-stream' \
       -H "Authorization: Bearer $(sudo grep '^AIVA_TOKEN=' /opt/aiva/.env | cut -d= -f2-)" \
       -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
     ```
     This MUST print `200`.
6. Run `sudo tailscale up`. If it prints an authentication URL, give that exact URL to the user and wait for them to approve it.
7. Run `sudo tailscale funnel --bg 8000`.
8. Run `tailscale funnel status` and extract the public HTTPS URL.
9. The final MCP endpoint is that URL plus `/mcp`.
10. Give the user exact current ChatGPT connection steps from the README.
11. In ChatGPT, have the user enable Developer Mode, create a custom app, enter
    the endpoint, and set authentication to the **API key / custom header**
    option, pasting the token from step 3 (`Bearer <token>` if the field wants a
    full header value). Then scan tools and create it. **Never tell the user to
    choose "No authentication".** This server exposes `run_command`, and a
    Funnel hostname is published in public Certificate Transparency logs, so an
    unauthenticated endpoint is anonymous remote code execution on their machine.
12. Test the `health`, `list_skills`, and `get_skill("start-here")` tools from ChatGPT.
13. Begin onboarding by interviewing the user and then update `start-here` using `save_skill`.

## Rules

- Never copy another person's credentials, memories, configuration, databases, or private skills into this installation.
- Never commit `.env`, OAuth tokens, SSH keys, databases, or generated personal skills. That includes `AIVA_TOKEN`: it is a live credential for shell access on the user's machine. Show it to the user, never paste it into a commit, an issue, or a chat log.
- Never expose the Funnel until step 5 has printed `401`. An unauthenticated MCP endpoint with `run_command` on it is remote code execution.
- Do not expose port 8000 directly in Oracle security lists. Tailscale Funnel provides HTTPS.
- Do not stop after installing packages. The task is complete only when the public MCP endpoint is working and ChatGPT has successfully scanned the tools.
- If Tailscale requires human authentication, that is the only expected pause.
