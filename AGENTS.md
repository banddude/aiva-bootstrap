# Agent Installation Contract

Install a personal remote MCP server on the current fresh Oracle Ubuntu VM.

## Required procedure

1. Confirm Ubuntu 22.04 or 24.04 and working sudo access.
2. Run exactly:
   `curl -fsSL https://raw.githubusercontent.com/banddude/aiva-bootstrap/main/bootstrap.sh | sudo bash`
3. If Tailscale prints a login URL, give that exact URL to the user and wait for approval.
4. Run `sudo /opt/aiva/finish-setup.sh` after approval.
5. Record the exact final MCP URL it prints. Do not shorten or alter it.
6. Verify `systemctl is-active aiva-mcp` returns `active`.
7. Add the final URL to ChatGPT as a custom MCP app. When the OAuth approval page opens, have the owner run `sudo cat /opt/aiva/admin-token` locally on Oracle and paste that value only into the approval form. Never ask them to paste the token into chat or logs.
8. Scan tools after authorization.
9. From ChatGPT, test `health`, `run_command("whoami && hostname && pwd")`, and `get_skill("start-here")`.
10. Start onboarding and update `start-here` only with information confirmed by this user.

## Rules

- Do not copy Mike Shaffer's data, secrets, memories, services, or private skills.
- Do not expose Oracle port 8765 publicly.
- Treat `/opt/aiva/mcp-url` as a secret.
- Do not stop merely because packages installed. Completion means the final URL is printed and ChatGPT successfully scans and calls tools.
- The expected human pause is the Tailscale browser approval.
