from __future__ import annotations

import base64
import json
import os
import platform
import secrets
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import uvicorn
from mcp.server.fastmcp import FastMCP

HOME = Path(os.environ.get("AIVA_HOME", "/opt/aiva"))
WORKSPACE = Path(os.environ.get("AIVA_WORKSPACE", HOME / "workspace")).resolve()
SKILLS = Path(os.environ.get("AIVA_SKILLS", HOME / "skills")).resolve()
MAX_OUTPUT = int(os.environ.get("AIVA_MAX_OUTPUT", "200000"))

# The shared secret every request must present. install.sh generates it into
# /opt/aiva/.env and the systemd unit loads that file.
#
# WHY THIS EXISTS AT ALL
#
# This server exposes run_command, write_file and read_file, and the README
# tells the operator to publish it on a public Tailscale Funnel. Without a
# credential that combination is anonymous remote code execution by anyone who
# guesses or is told the URL, and a Funnel hostname is not a secret: it is
# predictable from the tailnet name and it appears in TLS certificate
# transparency logs. That is not theoretical. It was live for about 15 minutes
# on 2026-07-31.
AUTH_TOKEN = os.environ.get("AIVA_TOKEN", "").strip()

WORKSPACE.mkdir(parents=True, exist_ok=True)
SKILLS.mkdir(parents=True, exist_ok=True)

mcp = FastMCP(
    "Personal AIVA Server",
    instructions=(
        "This is the user's own Oracle-hosted computer. Use tools carefully. "
        "Read start-here before personal work. Never expose secrets or credentials."
    ),
    stateless_http=True,
    json_response=True,
    host="0.0.0.0",
    port=int(os.environ.get("PORT", "8000")),
)


def _inside(root: Path, requested: str) -> Path:
    candidate = (root / requested).resolve() if not Path(requested).is_absolute() else Path(requested).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"Path must stay inside {root}")
    return candidate


def _trim(value: str) -> str:
    if len(value) <= MAX_OUTPUT:
        return value
    return value[:MAX_OUTPUT] + f"\n[output limited to {MAX_OUTPUT} characters]"


@mcp.tool()
def health() -> dict[str, Any]:
    """Check that the personal MCP server is running."""
    return {
        "ok": True,
        "hostname": platform.node(),
        "platform": platform.platform(),
        "workspace": str(WORKSPACE),
        "skills": str(SKILLS),
    }


@mcp.tool()
def run_command(command: str, cwd: str = ".", timeout_seconds: int = 25) -> dict[str, Any]:
    """Run a shell command on the user's Oracle server. Use only for tasks the user requested."""
    directory = _inside(WORKSPACE, cwd)
    directory.mkdir(parents=True, exist_ok=True)
    timeout_seconds = max(1, min(timeout_seconds, 600))
    try:
        completed = subprocess.run(
            ["bash", "-lc", command],
            cwd=directory,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            env={**os.environ, "HOME": str(HOME)},
        )
        return {
            "exit_code": completed.returncode,
            "stdout": _trim(completed.stdout),
            "stderr": _trim(completed.stderr),
            "cwd": str(directory),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "exit_code": 124,
            "stdout": _trim(exc.stdout or ""),
            "stderr": _trim((exc.stderr or "") + f"\nTimed out after {timeout_seconds}s"),
            "cwd": str(directory),
        }


@mcp.tool()
def list_files(path: str = ".", depth: int = 2) -> list[str]:
    """List files under the user's MCP workspace."""
    root = _inside(WORKSPACE, path)
    if not root.exists():
        return []
    depth = max(0, min(depth, 8))
    results: list[str] = []
    for item in sorted(root.rglob("*")):
        try:
            relative = item.relative_to(root)
        except ValueError:
            continue
        if len(relative.parts) <= depth:
            results.append(str(relative) + ("/" if item.is_dir() else ""))
    return results[:5000]


@mcp.tool()
def read_file(path: str) -> str:
    """Read a UTF-8 text file from the user's MCP workspace."""
    target = _inside(WORKSPACE, path)
    return _trim(target.read_text(encoding="utf-8"))


@mcp.tool()
def write_file(path: str, content: str, create_parent: bool = True) -> dict[str, Any]:
    """Write a UTF-8 text file in the user's MCP workspace."""
    target = _inside(WORKSPACE, path)
    if create_parent:
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"ok": True, "path": str(target), "bytes": len(content.encode("utf-8"))}


@mcp.tool()
def get_file_base64(path: str) -> dict[str, str]:
    """Fetch a binary file from the user's MCP workspace as base64."""
    target = _inside(WORKSPACE, path)
    return {
        "filename": target.name,
        "base64": base64.b64encode(target.read_bytes()).decode("ascii"),
    }


@mcp.tool()
def list_skills() -> list[str]:
    """List personal instruction skills available on this server."""
    return sorted(p.stem for p in SKILLS.glob("*.md"))


@mcp.tool()
def get_skill(name: str) -> str:
    """Read a personal instruction skill. Read start-here first."""
    safe_name = "".join(c for c in name if c.isalnum() or c in "-_ ").strip().replace(" ", "-")
    target = _inside(SKILLS, safe_name + ".md")
    return target.read_text(encoding="utf-8")


@mcp.tool()
def save_skill(name: str, content: str) -> dict[str, Any]:
    """Create or update a personal skill after the user asks for it."""
    safe_name = "".join(c for c in name if c.isalnum() or c in "-_ ").strip().replace(" ", "-")
    if not safe_name:
        raise ValueError("Skill name is required")
    target = _inside(SKILLS, safe_name + ".md")
    target.write_text(content, encoding="utf-8")
    return {"ok": True, "name": safe_name, "path": str(target)}


class BearerAuthMiddleware:
    """Require `Authorization: Bearer <AIVA_TOKEN>` on every HTTP request.

    Written as raw ASGI rather than a Starlette BaseHTTPMiddleware for one
    specific reason: the MCP streamable-http transport streams responses and
    owns its own lifespan, and BaseHTTPMiddleware buffers the body and can
    interfere with both. This touches only `http` scopes and passes `lifespan`
    and everything else straight through untouched.

    There is deliberately no unauthenticated health route. Anything reachable
    without the token is another thing an anonymous caller can probe, and the
    `health` MCP tool already answers that question for a caller who holds the
    token.
    """

    def __init__(self, app, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if not self._authorized(scope):
            await self._unauthorized(send)
            return

        await self.app(scope, receive, send)

    def _authorized(self, scope) -> bool:
        for raw_name, raw_value in scope.get("headers", []):
            if raw_name.lower() != b"authorization":
                continue
            try:
                value = raw_value.decode("latin-1")
            except UnicodeDecodeError:
                return False
            prefix, _, presented = value.partition(" ")
            if prefix.lower() != "bearer":
                return False
            # compare_digest, not ==, so a wrong token cannot be recovered one
            # character at a time by timing the reply.
            return secrets.compare_digest(presented.strip(), self.token)
        return False

    async def _unauthorized(self, send) -> None:
        body = json.dumps(
            {"error": "unauthorized", "detail": "Missing or invalid bearer token."}
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    # Tells a well-behaved client this is an auth problem, not a
                    # broken endpoint, which is what the operator sees if they
                    # paste the URL without the token.
                    (b"www-authenticate", b'Bearer realm="aiva-mcp"'),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def build_app():
    """The MCP app with authentication wrapped around it.

    Refuses to build without a token. FAILING CLOSED IS THE POINT: if this ever
    fell back to serving anonymously when AIVA_TOKEN was missing, then an
    upgrade over an older /opt/aiva/.env, a typo'd variable name, or a systemd
    unit that forgot EnvironmentFile would silently restore anonymous remote
    code execution, and nothing in the logs would look wrong. A server that
    will not start is loud. One that quietly drops its lock is not.
    """
    if not AUTH_TOKEN:
        raise SystemExit(
            "AIVA_TOKEN is not set, refusing to start.\n"
            "This server exposes run_command and file access, so it must never "
            "run unauthenticated.\n"
            "Generate one and add it to /opt/aiva/.env:\n"
            '  echo "AIVA_TOKEN=$(openssl rand -hex 32)" | sudo tee -a /opt/aiva/.env\n'
            "  sudo systemctl restart aiva-mcp"
        )
    return BearerAuthMiddleware(mcp.streamable_http_app(), AUTH_TOKEN)


if __name__ == "__main__":
    uvicorn.run(
        build_app(),
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        log_level=os.environ.get("AIVA_LOG_LEVEL", "info"),
    )
