from __future__ import annotations

import base64
import json
import os
import platform
import shlex
import subprocess
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

HOME = Path(os.environ.get("AIVA_HOME", "/opt/aiva"))
WORKSPACE = Path(os.environ.get("AIVA_WORKSPACE", HOME / "workspace")).resolve()
SKILLS = Path(os.environ.get("AIVA_SKILLS", HOME / "skills")).resolve()
MAX_OUTPUT = int(os.environ.get("AIVA_MAX_OUTPUT", "200000"))

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


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
