from __future__ import annotations

import base64
import os
import platform
import subprocess
from pathlib import Path
from typing import Any

import uvicorn
from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp


def _bootstrap_static_token() -> None:
    if os.environ.get("MCP_TOKEN") or os.environ.get("AIVA_TOKEN"):
        return
    token_file = os.environ.get("AIVA_TOKEN_FILE", "")
    if not token_file:
        return
    try:
        token = Path(token_file).read_text(encoding="utf-8").strip()
    except OSError:
        return
    if token:
        os.environ["MCP_TOKEN"] = token


_bootstrap_static_token()

from oauth_compat import (  # noqa: E402
    PUBLIC_BASE,
    CompatTokenVerifier,
    _data,
    _row,
    _token_response,
    authorize_route,
    initialize_store,
    metadata_route,
    oauth_counts,
    register_route,
    token_route,
)

HOME = Path(os.environ.get("AIVA_HOME", "/opt/aiva"))
WORKSPACE = Path(os.environ.get("AIVA_WORKSPACE", HOME / "workspace")).resolve()
SKILLS = Path(os.environ.get("AIVA_SKILLS", HOME / "skills")).resolve()
MAX_OUTPUT = int(os.environ.get("AIVA_MAX_OUTPUT", "200000"))
HOST = os.environ.get("AIVA_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8765"))

WORKSPACE.mkdir(parents=True, exist_ok=True)
SKILLS.mkdir(parents=True, exist_ok=True)
OAUTH_COUNTS_AT_BOOT = initialize_store()

mcp = MCPServer(
    "Personal AIVA Server",
    version="2.0.0-oracle",
    instructions=(
        "This is the user's own Oracle-hosted computer. Use tools carefully. "
        "Read start-here before personal work. Never expose secrets or credentials."
    ),
    token_verifier=CompatTokenVerifier(),
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(PUBLIC_BASE),
        resource_server_url=AnyHttpUrl(PUBLIC_BASE),
        required_scopes=["aiva"],
    ),
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


@mcp.custom_route("/healthz", methods=["GET"])
@mcp.custom_route("/health", methods=["GET"])
async def healthz(_: Request) -> Response:
    return JSONResponse(
        {
            "ok": True,
            "server": "Personal AIVA Server",
            "architecture": "MCP Python SDK v2",
            "protocol": "2026-07-28 dual-era",
        }
    )


@mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
async def protected_resource_legacy(_: Request) -> Response:
    return JSONResponse(
        {
            "resource": PUBLIC_BASE,
            "authorization_servers": [PUBLIC_BASE],
            "scopes_supported": ["aiva"],
            "bearer_methods_supported": ["header"],
        },
        headers={"Cache-Control": "public, max-age=300"},
    )


@mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
async def oauth_metadata_root(request: Request) -> Response:
    return await metadata_route(request)


@mcp.custom_route("/oauth/register", methods=["POST"])
async def oauth_register(request: Request) -> Response:
    return await register_route(request)


@mcp.custom_route("/oauth/authorize", methods=["GET", "POST"])
async def oauth_authorize(request: Request) -> Response:
    return await authorize_route(request)


@mcp.custom_route("/oauth/token", methods=["POST"])
async def oauth_token(request: Request) -> Response:
    form = await request.form()
    if str(form.get("grant_type") or "") == "refresh_token":
        refresh = str(form.get("refresh_token") or "")
        row = _row("refresh", refresh)
        data = _data(row)
        client_id = str(form.get("client_id") or data.get("client_id") or "")
        if not data or not client_id or str(data.get("client_id") or "") != client_id:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        return JSONResponse(
            _token_response(
                client_id,
                refresh_token=refresh,
                scope=str(data.get("scope") or "aiva"),
                resource=PUBLIC_BASE,
            )
        )
    return await token_route(request)


@mcp.tool()
def health() -> dict[str, Any]:
    """Check that the personal MCP server is running and report its MCP architecture."""
    return {
        "ok": True,
        "hostname": platform.node(),
        "platform": platform.platform(),
        "workspace": str(WORKSPACE),
        "skills": str(SKILLS),
        "mcp_sdk": "2.x",
        "protocol": "2026-07-28 with legacy compatibility",
        "oauth_rows": oauth_counts(),
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
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return {
            "exit_code": 124,
            "stdout": _trim(stdout),
            "stderr": _trim(stderr + f"\nTimed out after {timeout_seconds}s"),
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
    return {"filename": target.name, "base64": base64.b64encode(target.read_bytes()).decode("ascii")}


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


def _transport_security() -> TransportSecuritySettings:
    configured = [h.strip() for h in os.environ.get("AIVA_ALLOWED_HOSTS", "").split(",") if h.strip()]
    hosts = ["127.0.0.1:*", "localhost:*", "mcp.officeadmin.io"] + configured
    origins = [
        "http://127.0.0.1:*",
        "http://localhost:*",
        "https://mcp.officeadmin.io",
        "https://chatgpt.com",
        "https://chat.openai.com",
        "https://platform.openai.com",
        "https://claude.ai",
        "https://claude.com",
    ]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


def build_app() -> ASGIApp:
    return mcp.streamable_http_app(
        host=HOST,
        streamable_http_path="/",
        json_response=True,
        stateless_http=True,
        transport_security=_transport_security(),
    )


if __name__ == "__main__":
    uvicorn.run(build_app(), host=HOST, port=PORT, log_level="info")
