from __future__ import annotations

import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal
from urllib.parse import quote

import uvicorn
from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import (
    BlobResourceContents,
    CallToolResult,
    EmbeddedResource,
    TextContent,
    ToolAnnotations,
)
from pydantic import AnyHttpUrl, Field
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket


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

from agent_hub import AgentHub
from notify_delivery import make_spool_record, send_mike, summarize
from oauth_compat import (
    PUBLIC_BASE,
    STATIC_TOKEN,
    CompatTokenVerifier,
    _data,
    _row,
    _token_response,
    authorize_route,
    initialize_store,
    metadata_route,
    register_route,
    token_route,
)
from tool_contract import WORKER_TOOL_SCHEMAS

HOME = Path(os.environ.get("AIVA_HOME", "/opt/aiva"))
STATE = Path(os.environ.get("AIVA_STATE", HOME / "state")).resolve()
HOST = os.environ.get("AIVA_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8765"))
CHATGPT_TRANSFER_ROOT = PurePosixPath(
    os.environ.get("AIVA_CHATGPT_TRANSFER_ROOT", "/tmp/aiva-chatgpt-transfer")
)

STATE.mkdir(parents=True, exist_ok=True)
OAUTH_COUNTS_AT_BOOT = initialize_store()
AGENT_HUB = AgentHub(token=STATIC_TOKEN, state_dir=STATE)

READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
WRITE_DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=True,
)

Machine = Annotated[
    Literal["laptop", "oracle", "mac-server"],
    Field(description="Which machine to run on: laptop, oracle, or mac-server."),
]
SyncTimeout = Annotated[
    float | None,
    Field(description="Seconds to wait. Default 10, max 25 for a synchronous call. For longer commands use run_command_async."),
]
InspectTimeout = Annotated[
    float | None,
    Field(description="Seconds to wait. Default 10, max 25."),
]
AsyncTimeout = Annotated[
    float | None,
    Field(description="Max seconds the command may run before it's killed. Default 10, max 600."),
]

mcp = MCPServer(
    "aiva-mcp",
    version="2.0.0-oracle",
    instructions=(
        "AIVA tools for Mike Shaffer. Before performing tasks, call get_skill with "
        "skill_name=start-here to establish identity, operating rules, and routing. Then use "
        "list_skills/get_skill for relevant domain skills. Machine-backed tools require an explicit machine. "
        "Set machine=oracle, laptop, or mac-server for each machine-backed call. If a machine "
        "is offline, the call returns not connected. To read the state of a machine, use "
        "inspect_machine; run_command is a full shell. The notify tool takes no machine."
    ),
    token_verifier=CompatTokenVerifier(),
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(PUBLIC_BASE),
        resource_server_url=AnyHttpUrl(PUBLIC_BASE),
        required_scopes=["aiva"],
    ),
)


@mcp.custom_route("/healthz", methods=["GET"])
@mcp.custom_route("/health", methods=["GET"])
async def healthz(_: Request) -> Response:
    return JSONResponse(
        {
            "ok": True,
            "server": "aiva-mcp",
            "architecture": "MCP Python SDK v2",
            "protocol": "2026-07-28 dual-era",
            "agent_hub": AGENT_HUB.status(),
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


def _result(result: Any) -> CallToolResult:
    text = result if isinstance(result, str) else json.dumps(result, indent=2)
    is_error = bool(isinstance(result, dict) and result.get("ok") is False)
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=is_error)


def _clean_args(**kwargs: Any) -> dict[str, Any]:
    return {key: value for key, value in kwargs.items() if value is not None}


def _staged_transfer_path(path: str) -> str:
    value = path.strip()
    if not value:
        raise ValueError("path must name a staged file")

    raw = PurePosixPath(value)
    candidate = raw if raw.is_absolute() else CHATGPT_TRANSFER_ROOT / raw
    if ".." in candidate.parts:
        raise ValueError("path traversal is not allowed")

    try:
        relative = candidate.relative_to(CHATGPT_TRANSFER_ROOT)
    except ValueError as exc:
        raise ValueError(
            f"get_file only reads files staged under {CHATGPT_TRANSFER_ROOT}"
        ) from exc
    if not relative.parts:
        raise ValueError("path must name a file inside the staged transfer directory")
    return str(candidate)


async def _dispatch_sync(machine: str, tool: str, args: dict[str, Any]) -> CallToolResult:
    result = await AGENT_HUB.dispatch(machine, tool, args)
    if isinstance(result, dict) and (
        result.get("timed_out")
        or (isinstance(result.get("error"), str) and str(result["error"]).startswith("timeout after"))
    ):
        waited = args.get("timeout_seconds") or 10
        if tool == "inspect_machine":
            result["how_to_extend"] = (
                f"Synchronous call timed out after {waited:g}s. Re-run with a larger timeout_seconds "
                "(max 25). Inspection has no background twin, so for a reading that genuinely needs "
                "longer than 25s, write it as a shell command and call run_command_async (that is the "
                "general shell, not this read-only tool), then poll job_result with the returned job_id "
                "(async timeout_seconds max 600)."
            )
        else:
            result["how_to_extend"] = (
                f"Synchronous call timed out after {waited:g}s. Re-run with a larger timeout_seconds "
                "(max 25 for a synchronous call). For commands that may run longer than 25s, call "
                "run_command_async with the same arguments. It returns a job_id immediately, then poll "
                "job_result with that job_id (async timeout_seconds max 600)."
            )
    return _result(result)


@mcp.tool(
    description="Run a shell command on the chosen machine (synchronous: waits for the result). Returns stdout, stderr, exit code. Times out at timeout_seconds (default 10, max 25); the timeout response tells you how to extend or go async.",
    annotations=WRITE_DESTRUCTIVE,
    structured_output=False,
)
async def run_command(*, machine: Machine, command: str, timeout_seconds: SyncTimeout = None) -> CallToolResult:
    return await _dispatch_sync(machine, "run_command", _clean_args(command=command, timeout_seconds=timeout_seconds))


@mcp.tool(
    description="Read-only inspection of a machine: where am I, what is here, what is in it, what is this host running. Runs ONE allowlisted reporting command (pwd, ls, cat, head, grep, stat, df, ps, uname, git status, git log, gh auth status, gh pr list, wrangler whoami, systemctl status, launchctl list, tailscale status, and similar). Cannot change state: the command is checked against the allowlist before it starts, and its tokens never reach a shell, so pipes, redirects, substitutions and globs are refused rather than interpreted. Anything outside the set returns refused: true having run nothing; use run_command for it. Times out at timeout_seconds (default 10, max 25).",
    annotations=READ_ONLY,
    structured_output=False,
)
async def inspect_machine(
    *,
    machine: Machine,
    command: Annotated[str | None, Field(description="The command as one string, e.g. 'git status --short'. Quotes group and a backslash escapes, but nothing is expanded or substituted.")] = None,
    argv: Annotated[list[str] | None, Field(description='The same command pre-split into tokens, e.g. ["grep", "-n", "two words", "file.txt"]. Use this instead of command when an argument contains spaces or characters that the string form refuses.')] = None,
    cwd: Annotated[str | None, Field(description="Directory to run in. Defaults to the home directory.")] = None,
    timeout_seconds: InspectTimeout = None,
) -> CallToolResult:
    return await _dispatch_sync(
        machine,
        "inspect_machine",
        _clean_args(command=command, argv=argv, cwd=cwd, timeout_seconds=timeout_seconds),
    )


@mcp.tool(
    description="Run a long shell command on the chosen machine in the BACKGROUND. Returns a job_id immediately (does not wait). Then poll job_result with that job_id to get the output. Use this for anything that may run longer than 25 seconds (builds, installs, long scripts).",
    annotations=WRITE_DESTRUCTIVE,
    structured_output=False,
)
async def run_command_async(*, machine: Machine, command: str, timeout_seconds: AsyncTimeout = None) -> CallToolResult:
    result = await AGENT_HUB.dispatch_async(
        machine,
        "run_command",
        _clean_args(command=command, timeout_seconds=timeout_seconds),
    )
    return _result(result)


@mcp.tool(
    description="Fetch the result of a run_command_async job by its job_id. Returns {status:'running', elapsed_sec} if not finished yet (poll again), or {status:'done', result} with the output. The result is consumed once delivered.",
    annotations=READ_ONLY,
    structured_output=False,
)
def job_result(job_id: str) -> CallToolResult:
    return _result(AGENT_HUB.job_result(job_id))


@mcp.tool(
    description="Read a UTF-8 text file from the chosen machine. Optional line offset/limit.",
    annotations=READ_ONLY,
    structured_output=False,
)
async def read_file(*, machine: Machine, path: str, offset: float | None = None, limit: float | None = None) -> CallToolResult:
    return await _dispatch_sync(machine, "read_file", _clean_args(path=path, offset=offset, limit=limit))


@mcp.tool(
    description="Write a UTF-8 text file on the chosen machine. mode: rewrite (default) or append.",
    annotations=WRITE_DESTRUCTIVE,
    structured_output=False,
)
async def write_file(
    *,
    machine: Machine,
    path: str,
    content: str,
    mode: Literal["rewrite", "append"] | None = None,
) -> CallToolResult:
    return await _dispatch_sync(machine, "write_file", _clean_args(path=path, content=content, mode=mode))


@mcp.tool(
    description="List a directory tree on the chosen machine, to an optional depth.",
    annotations=READ_ONLY,
    structured_output=False,
)
async def list_directory(
    *,
    machine: Machine,
    path: Annotated[str | None, Field(description="Defaults to the home directory.")] = None,
    depth: Annotated[float | None, Field(description="Default 2.")] = None,
) -> CallToolResult:
    return await _dispatch_sync(machine, "list_directory", _clean_args(path=path, depth=depth))


@mcp.tool(
    description="List the available AIVA skills on the chosen machine.",
    annotations=READ_ONLY,
    structured_output=False,
)
async def list_skills(*, machine: Machine) -> CallToolResult:
    return await _dispatch_sync(machine, "list_skills", {})


@mcp.tool(
    description="Get the full content of a named AIVA skill from the chosen machine.",
    annotations=READ_ONLY,
    structured_output=False,
)
async def get_skill(*, machine: Machine, skill_name: str) -> CallToolResult:
    return await _dispatch_sync(machine, "get_skill", {"skill_name": skill_name})


@mcp.tool(
    description=(
        "Transfer a file that was explicitly staged for ChatGPT under "
        "/tmp/aiva-chatgpt-transfer on the chosen machine. Returns the file as an embedded MCP "
        "resource with its MIME type preserved. This read-only action cannot access arbitrary "
        "filesystem paths; stage the file in the handoff directory first."
    ),
    annotations=READ_ONLY,
    structured_output=False,
)
async def get_file(*, machine: Machine, path: str) -> CallToolResult:
    try:
        staged_path = _staged_transfer_path(path)
    except ValueError as exc:
        return _result({"ok": False, "error": str(exc)})

    result = await AGENT_HUB.dispatch(machine, "get_file", {"path": staged_path})
    if not isinstance(result, dict) or result.get("ok") is False:
        return _result(result)

    blob = result.get("content_base64")
    if not isinstance(blob, str):
        return _result({"ok": False, "error": "get_file response did not contain content_base64"})

    returned_path = str(result.get("path") or staged_path)
    mime_type = mimetypes.guess_type(returned_path)[0] or "application/octet-stream"
    resource_uri = f"aiva-file://{machine}{quote(returned_path, safe='/')}"
    resource = BlobResourceContents(
        uri=resource_uri,
        mime_type=mime_type,
        blob=blob,
        _meta={
            "filename": Path(returned_path).name,
            "size": result.get("bytes"),
            "machine": machine,
        },
    )
    return CallToolResult(content=[EmbeddedResource(type="resource", resource=resource)])


@mcp.tool(
    description="Write a file to the chosen machine from base64 content. Set overwrite to replace an existing file.",
    annotations=WRITE_DESTRUCTIVE,
    structured_output=False,
)
async def send_file(
    *,
    machine: Machine,
    path: str,
    content_base64: str,
    overwrite: bool | None = None,
) -> CallToolResult:
    return await _dispatch_sync(
        machine,
        "send_file",
        _clean_args(path=path, content_base64=content_base64, overwrite=overwrite),
    )


@mcp.tool(
    description="Send a notification to AIVA or to Mike. Takes no machine: target 'aiva' and the internal 'dev' compatibility target use the local CAO spool, while target 'mike' calls api.sendblue.co directly. target 'all' sends to AIVA and Mike. The result names exactly which channels delivered and which failed, and a partial delivery is reported as partial, not as success.",
    annotations=WRITE_DESTRUCTIVE,
    structured_output=False,
)
async def notify(
    *,
    target: str,
    message: str,
    from_: Annotated[str | None, Field(validation_alias="from")] = None,
) -> CallToolResult:
    normalized = target.strip().lower()
    source = (from_ or "mcp-agent").strip() or "mcp-agent"
    if normalized not in {"aiva", "dev", "mike", "all"}:
        return _result({"ok": False, "status": "failed", "error": "'target' is required and must be one of: aiva, dev, mike, all"})
    if not message.strip():
        return _result({"ok": False, "status": "failed", "error": "'message' is required and cannot be empty"})

    wanted = ["aiva", "mike"] if normalized == "all" else [normalized]
    channels: list[dict[str, Any]] = []
    for channel in wanted:
        if channel == "mike":
            channels.append(send_mike(message, source))
            continue
        path, content, inbox_target = make_spool_record(channel, message, source)
        result = await AGENT_HUB.dispatch(
            "oracle",
            "write_file",
            {"path": str(path), "content": content, "mode": "rewrite", "timeout_seconds": 10},
        )
        if result.get("ok"):
            channels.append({"channel": channel, "ok": True, "detail": f"queued for {inbox_target} on local CAO spool"})
        else:
            channels.append({"channel": channel, "ok": False, "error": str(result.get("error") or "Oracle spool write failed")})
    return _result(summarize(normalized, source, channels))


def _apply_worker_tool_schemas() -> None:
    registered = mcp._tool_manager._tools
    if set(registered) != set(WORKER_TOOL_SCHEMAS):
        missing = sorted(set(WORKER_TOOL_SCHEMAS) - set(registered))
        extra = sorted(set(registered) - set(WORKER_TOOL_SCHEMAS))
        raise RuntimeError(f"MCP tool contract mismatch at startup: missing={missing} extra={extra}")
    for name, schema in WORKER_TOOL_SCHEMAS.items():
        registered[name].parameters = schema


_apply_worker_tool_schemas()


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


class AivaASGI:
    """Add the original machine-agent WebSocket beside the root MCP transport."""

    def __init__(self, inner: ASGIApp) -> None:
        self.inner = inner

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket" and scope.get("path") == "/agent/socket":
            await AGENT_HUB.websocket(WebSocket(scope, receive=receive, send=send))
            return
        await self.inner(scope, receive, send)


def build_app() -> ASGIApp:
    inner = mcp.streamable_http_app(
        host=HOST,
        streamable_http_path="/",
        json_response=True,
        stateless_http=True,
        transport_security=_transport_security(),
    )
    return AivaASGI(inner)


if __name__ == "__main__":
    uvicorn.run(build_app(), host=HOST, port=PORT, log_level="info")
