from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import platform
import secrets
import sqlite3
import subprocess
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

HOME = Path(os.environ.get("AIVA_HOME", "/opt/aiva")).resolve()
WORKSPACE = Path(os.environ.get("AIVA_WORKSPACE", HOME / "workspace")).resolve()
SKILLS = Path(os.environ.get("AIVA_SKILLS", HOME / "skills")).resolve()
STATE = Path(os.environ.get("AIVA_STATE", HOME / "state")).resolve()
MAX_OUTPUT = int(os.environ.get("AIVA_MAX_OUTPUT", "200000"))
PORT = int(os.environ.get("PORT", "8765"))
PUBLIC_ORIGIN = os.environ.get("AIVA_PUBLIC_ORIGIN", "https://mcp.officeadmin.io").rstrip("/")
OAUTH_DB = Path(os.environ.get("AIVA_OAUTH_DB", STATE / "oauth.sqlite3")).resolve()
OAUTH_IMPORT_JSON = Path(
    os.environ.get("AIVA_OAUTH_IMPORT_JSON", STATE / "oauth-d1-export-latest.json")
).resolve()
TOKEN_FILE = Path(os.environ.get("AIVA_TOKEN_FILE", STATE / "mcp-token")).resolve()

WORKSPACE.mkdir(parents=True, exist_ok=True)
SKILLS.mkdir(parents=True, exist_ok=True)
STATE.mkdir(parents=True, exist_ok=True)

TRUSTED_REDIRECT_HOSTS = {
    "claude.ai",
    "claude.com",
    "chatgpt.com",
    "chat.openai.com",
    "platform.openai.com",
    "openai.com",
}
EXACT_REDIRECTS = {
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
}
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS",
}


def _read_static_token() -> str:
    env_token = os.environ.get("MCP_TOKEN", "").strip()
    if env_token:
        return env_token
    try:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise RuntimeError(f"MCP token missing: set MCP_TOKEN or create {TOKEN_FILE}") from exc
    if not token:
        raise RuntimeError(f"MCP token file is empty: {TOKEN_FILE}")
    return token


STATIC_TOKEN = _read_static_token()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(OAUTH_DB, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mcp_oauth (
            kind TEXT NOT NULL,
            id TEXT NOT NULL,
            data TEXT NOT NULL,
            expires_at INTEGER,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (kind, id)
        )
        """
    )
    return conn


def _import_legacy_oauth_if_empty() -> None:
    if not OAUTH_IMPORT_JSON.is_file():
        return
    with _db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM mcp_oauth").fetchone()[0]
        if count:
            return
        payload = json.loads(OAUTH_IMPORT_JSON.read_text(encoding="utf-8"))
        rows = payload.get("rows", [])
        now_ms = int(time.time() * 1000)
        for row in rows:
            conn.execute(
                """
                INSERT OR REPLACE INTO mcp_oauth(kind,id,data,expires_at,created_at)
                VALUES(?,?,?,?,?)
                """,
                (
                    str(row["kind"]),
                    str(row["id"]),
                    row["data"] if isinstance(row["data"], str) else json.dumps(row["data"]),
                    row.get("expires_at"),
                    row.get("created_at") or now_ms,
                ),
            )


_import_legacy_oauth_if_empty()


def oauth_put(kind: str, item_id: str, data: dict[str, Any], expires_at: int | None = None) -> None:
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO mcp_oauth(kind,id,data,expires_at,created_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(kind,id) DO UPDATE SET
              data=excluded.data,
              expires_at=excluded.expires_at
            """,
            (kind, item_id, json.dumps(data, separators=(",", ":")), expires_at, int(time.time() * 1000)),
        )


def oauth_get(kind: str, item_id: str) -> dict[str, Any] | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT data, expires_at FROM mcp_oauth WHERE kind=? AND id=?",
            (kind, item_id),
        ).fetchone()
    if row is None:
        return None
    if row["expires_at"] is not None and int(row["expires_at"]) < int(time.time() * 1000):
        return None
    try:
        data = json.loads(row["data"])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def oauth_del(kind: str, item_id: str) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM mcp_oauth WHERE kind=? AND id=?", (kind, item_id))


def redirect_uri_allowed(uri: str) -> bool:
    if uri in EXACT_REDIRECTS:
        return True
    try:
        parsed = urllib.parse.urlsplit(uri)
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.hostname in TRUSTED_REDIRECT_HOSTS


def _parse_form_body(raw: bytes) -> dict[str, str]:
    decoded = raw.decode("utf-8", errors="replace")
    pairs = urllib.parse.parse_qsl(decoded, keep_blank_values=True)
    return {k: v for k, v in pairs}


async def request_params(request: Request) -> dict[str, Any]:
    ctype = request.headers.get("content-type", "").lower()
    raw = await request.body()
    if "application/json" in ctype:
        try:
            obj = json.loads(raw or b"{}")
            return obj if isinstance(obj, dict) else {}
        except json.JSONDecodeError:
            return {}
    return _parse_form_body(raw)


def cors_json(payload: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers=CORS_HEADERS)


def auth_server_metadata(origin: str) -> JSONResponse:
    return cors_json(
        {
            "issuer": origin,
            "authorization_endpoint": f"{origin}/oauth/authorize",
            "token_endpoint": f"{origin}/oauth/token",
            "registration_endpoint": f"{origin}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": ["*"],
        }
    )


def protected_resource_metadata(origin: str) -> JSONResponse:
    return cors_json(
        {
            "resource": origin,
            "authorization_servers": [origin],
            "bearer_methods_supported": ["header"],
        }
    )


def consent_html(client_name: str, params: dict[str, Any], show_error: bool = False) -> str:
    fields = ("client_id", "redirect_uri", "state", "code_challenge", "code_challenge_method", "response_type")
    hidden = "".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(str(params.get(k, "")), quote=True)}">'
        for k in fields
    )
    error_style = "block" if show_error else "none"
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AIVA - Authorize</title>
<style>
body{{font-family:-apple-system,system-ui,sans-serif;background:#0a0a0a;color:#e0e0e0;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0}}
.card{{background:#1a1a1a;border:1px solid #333;border-radius:12px;padding:2rem;max-width:400px;width:90%;box-shadow:0 4px 24px rgba(0,0,0,.5)}}
h1{{font-size:1.3rem;margin:0 0 .5rem;color:#fff}}.client{{color:#7c93ee;font-weight:600}}.info{{color:#888;font-size:.85rem;margin-bottom:1.5rem}}
input[type=password]{{width:100%;padding:.7rem;border:1px solid #444;border-radius:8px;background:#111;color:#fff;font-size:1rem;box-sizing:border-box;margin-bottom:1rem}}
button{{width:100%;padding:.7rem;border:0;border-radius:8px;background:#7c93ee;color:#fff;font-size:1rem;font-weight:600;cursor:pointer}}
.error{{color:#ff6b6b;font-size:.85rem;margin-bottom:1rem;display:{error_style}}}
</style></head><body><div class="card">
<h1>Authorize <span class="client">{html.escape(client_name)}</span></h1>
<p class="info">This app wants to access your AIVA MCP server.<br>Paste your MCP token to approve.</p>
<p class="error">Invalid token. Try again.</p>
<form method="POST"><input type="password" name="token" placeholder="MCP token" autofocus required>
{hidden}<button type="submit">Approve</button></form></div></body></html>"""


def request_origin(request: Request) -> str:
    if PUBLIC_ORIGIN:
        return PUBLIC_ORIGIN
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}".rstrip("/")


mcp = FastMCP(
    "Personal AIVA Server",
    instructions=(
        "This is the user's own Oracle-hosted computer. Use tools carefully. "
        "Read start-here before personal work. Never expose secrets or credentials."
    ),
    stateless_http=True,
    json_response=True,
    host="127.0.0.1",
    port=PORT,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "127.0.0.1:*",
            "localhost:*",
            "mcp.officeadmin.io",
            "mcp.officeadmin.io:443",
        ],
        allowed_origins=[
            "http://127.0.0.1:*",
            "http://localhost:*",
            "https://mcp.officeadmin.io",
            "https://chatgpt.com",
            "https://chat.openai.com",
            "https://claude.ai",
            "https://claude.com",
        ],
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


@mcp.custom_route("/", methods=["GET"])
async def root_health(_: Request) -> Response:
    return JSONResponse({"ok": True})


@mcp.custom_route("/health", methods=["GET"])
async def http_health(_: Request) -> Response:
    return JSONResponse({"ok": True})


@mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET", "OPTIONS"])
async def oauth_protected_resource(request: Request) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=CORS_HEADERS)
    return protected_resource_metadata(request_origin(request))


@mcp.custom_route("/.well-known/oauth-protected-resource/mcp", methods=["GET", "OPTIONS"])
async def oauth_protected_resource_mcp(request: Request) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=CORS_HEADERS)
    return protected_resource_metadata(request_origin(request))


@mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET", "OPTIONS"])
async def oauth_authorization_server(request: Request) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=CORS_HEADERS)
    return auth_server_metadata(request_origin(request))


@mcp.custom_route("/.well-known/oauth-authorization-server/mcp", methods=["GET", "OPTIONS"])
async def oauth_authorization_server_mcp(request: Request) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=CORS_HEADERS)
    return auth_server_metadata(request_origin(request))


@mcp.custom_route("/oauth/register", methods=["POST", "OPTIONS"])
async def oauth_register(request: Request) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=CORS_HEADERS)
    try:
        body = await request.json()
    except Exception:
        return cors_json({"error": "invalid_request"}, 400)
    if not isinstance(body, dict):
        return cors_json({"error": "invalid_request"}, 400)
    uris = body.get("redirect_uris") or []
    if not isinstance(uris, list) or not uris or not all(isinstance(u, str) and redirect_uri_allowed(u) for u in uris):
        return cors_json(
            {
                "error": "invalid_redirect_uri",
                "error_description": "redirect_uri host not permitted (allowed: Claude, ChatGPT/OpenAI)",
            },
            403,
        )
    client_id = f"client_{uuid.uuid4().hex}"
    info = {
        "client_id": client_id,
        "redirect_uris": uris,
        "client_name": body.get("client_name") or "unknown",
        "grant_types": body.get("grant_types") or ["authorization_code"],
        "response_types": body.get("response_types") or ["code"],
        "token_endpoint_auth_method": "none",
    }
    oauth_put("client", client_id, info)
    return cors_json(info, 201)


@mcp.custom_route("/oauth/authorize", methods=["GET", "POST", "OPTIONS"])
async def oauth_authorize(request: Request) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=CORS_HEADERS)
    if request.method == "GET":
        params: dict[str, Any] = dict(request.query_params)
    else:
        params = await request_params(request)

    client_id = str(params.get("client_id", ""))
    redirect_uri = str(params.get("redirect_uri", ""))
    state = str(params.get("state", ""))
    code_challenge = str(params.get("code_challenge", ""))
    code_challenge_method = str(params.get("code_challenge_method", "S256"))
    response_type = str(params.get("response_type", "code"))

    if response_type != "code":
        return cors_json({"error": "unsupported_response_type"}, 400)
    if not redirect_uri_allowed(redirect_uri):
        return cors_json({"error": "invalid_redirect_uri"}, 400)
    if not client_id:
        return cors_json({"error": "invalid_client"}, 400)
    if code_challenge and code_challenge_method != "S256":
        return cors_json({"error": "invalid_request", "error_description": "Only PKCE S256 is supported"}, 400)

    client = oauth_get("client", client_id)
    if client is None:
        client = {
            "client_id": client_id,
            "redirect_uris": [redirect_uri],
            "client_name": "Claude",
            "token_endpoint_auth_method": "none",
        }
        oauth_put("client", client_id, client)

    if request.method == "POST":
        supplied = str(params.get("token", ""))
        if hmac.compare_digest(supplied, STATIC_TOKEN):
            code = secrets.token_urlsafe(32)
            oauth_put(
                "code",
                code,
                {
                    "client_id": client_id,
                    "redirect_uri": redirect_uri,
                    "code_challenge": code_challenge,
                    "code_challenge_method": code_challenge_method,
                },
                int(time.time() * 1000) + 300_000,
            )
            query = {"code": code}
            if state:
                query["state"] = state
            separator = "&" if "?" in redirect_uri else "?"
            location = redirect_uri + separator + urllib.parse.urlencode(query)
            return RedirectResponse(location, status_code=302)
        return HTMLResponse(consent_html(str(client.get("client_name", "unknown")), params, True))

    return HTMLResponse(consent_html(str(client.get("client_name", "unknown")), params, False))


@mcp.custom_route("/oauth/token", methods=["POST", "OPTIONS"])
async def oauth_token(request: Request) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=CORS_HEADERS)
    body = await request_params(request)
    grant = str(body.get("grant_type", ""))

    if grant == "authorization_code":
        code = str(body.get("code", ""))
        ac = oauth_get("code", code)
        if ac is None:
            return cors_json({"error": "invalid_grant", "error_description": "Invalid or expired code"}, 400)
        oauth_del("code", code)
        if ac.get("code_challenge"):
            verifier = str(body.get("code_verifier", ""))
            expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("utf-8")).digest()).decode("ascii").rstrip("=")
            if not hmac.compare_digest(expected, str(ac["code_challenge"])):
                return cors_json({"error": "invalid_grant", "error_description": "PKCE validation failed"}, 400)
        access = secrets.token_urlsafe(48)
        refresh = secrets.token_urlsafe(48)
        oauth_put("token", access, {"client_id": ac.get("client_id"), "refresh": refresh})
        oauth_put("refresh", refresh, {"client_id": ac.get("client_id"), "is_refresh": True})
        return cors_json({"access_token": access, "token_type": "bearer", "refresh_token": refresh})

    if grant == "refresh_token":
        refresh = str(body.get("refresh_token", ""))
        ri = oauth_get("refresh", refresh)
        if ri is None:
            return cors_json({"error": "invalid_grant"}, 400)
        access = secrets.token_urlsafe(48)
        oauth_put("token", access, {"client_id": ri.get("client_id"), "refresh": refresh})
        return cors_json({"access_token": access, "token_type": "bearer", "refresh_token": refresh})

    return cors_json({"error": "unsupported_grant_type"}, 400)


class AuthAndRootAlias:
    """Require the old MCP token or an imported OAuth token on the MCP transport."""

    def __init__(self, wrapped: Any):
        self.wrapped = wrapped

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.wrapped(scope, receive, send)
            return

        path = str(scope.get("path", ""))
        method = str(scope.get("method", "GET")).upper()

        if path == "/" and method == "POST":
            scope = dict(scope)
            scope["path"] = "/mcp"
            scope["raw_path"] = b"/mcp"
            path = "/mcp"

        if path == "/mcp":
            if method == "OPTIONS":
                response = Response(status_code=204, headers=CORS_HEADERS)
                await response(scope, receive, send)
                return

            headers = {
                k.decode("latin-1").lower(): v.decode("latin-1")
                for k, v in scope.get("headers", [])
            }
            auth = headers.get("authorization", "")
            valid = False
            if auth.startswith("Bearer "):
                token = auth[7:]
                valid = hmac.compare_digest(token, STATIC_TOKEN) or oauth_get("token", token) is not None
            if not valid:
                response = JSONResponse(
                    {"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": "unauthorized"}},
                    status_code=401,
                    headers={
                        "WWW-Authenticate": f'Bearer resource_metadata="{PUBLIC_ORIGIN}/.well-known/oauth-protected-resource"'
                    },
                )
                await response(scope, receive, send)
                return

        await self.wrapped(scope, receive, send)


mcp_app = mcp.streamable_http_app()
app = CORSMiddleware(
    AuthAndRootAlias(mcp_app),
    allow_origins=[
        "https://chatgpt.com",
        "https://chat.openai.com",
        "https://claude.ai",
        "https://claude.com",
    ],
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Mcp-Session-Id"],
)


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("AIVA_BIND_HOST", "127.0.0.1"),
        port=PORT,
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("AIVA_FORWARDED_ALLOW_IPS", "127.0.0.1"),
        log_level=os.environ.get("AIVA_LOG_LEVEL", "info").lower(),
    )
