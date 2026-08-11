from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

from mcp.server.auth.provider import AccessToken, TokenVerifier
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

PUBLIC_BASE = os.environ.get("AIVA_PUBLIC_BASE", "https://mcp.officeadmin.io").rstrip("/")
PUBLIC_MCP = os.environ.get("AIVA_PUBLIC_MCP", PUBLIC_BASE + "/mcp")
DB_PATH = Path(os.environ.get("AIVA_OAUTH_DB", "/opt/aiva/state/oauth.sqlite3"))
IMPORT_PATH = Path(os.environ.get("AIVA_OAUTH_IMPORT", "/opt/aiva/state/legacy-oauth.json"))
STATIC_TOKEN = os.environ.get("MCP_TOKEN") or os.environ.get("AIVA_TOKEN") or ""

_DEFAULT_REDIRECT_HOSTS = {
    "claude.ai",
    "claude.com",
    "chatgpt.com",
    "chat.openai.com",
    "platform.openai.com",
    "openai.com",
}
_EXTRA_REDIRECT_HOSTS = {
    h.strip().lower()
    for h in os.environ.get("AIVA_OAUTH_REDIRECT_HOSTS", "").split(",")
    if h.strip()
}
REDIRECT_HOSTS = _DEFAULT_REDIRECT_HOSTS | _EXTRA_REDIRECT_HOSTS


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mcp_oauth(
          kind TEXT NOT NULL,
          id TEXT NOT NULL,
          data TEXT NOT NULL,
          expires_at INTEGER,
          created_at INTEGER NOT NULL DEFAULT (unixepoch()),
          PRIMARY KEY(kind,id)
        )
        """
    )
    return conn


def initialize_store() -> dict[str, int]:
    with _connect() as conn:
        count = int(conn.execute("SELECT COUNT(*) FROM mcp_oauth").fetchone()[0])
        if count == 0 and IMPORT_PATH.is_file():
            payload = json.loads(IMPORT_PATH.read_text(encoding="utf-8"))
            rows = payload.get("rows", []) if isinstance(payload, dict) else []
            for row in rows:
                conn.execute(
                    "INSERT OR REPLACE INTO mcp_oauth(kind,id,data,expires_at,created_at) VALUES(?,?,?,?,?)",
                    (
                        str(row.get("kind", "")),
                        str(row.get("id", "")),
                        str(row.get("data") or "{}"),
                        row.get("expires_at"),
                        int(row.get("created_at") or time.time()),
                    ),
                )
            conn.commit()
        result: dict[str, int] = {}
        for row in conn.execute("SELECT kind,COUNT(*) AS n FROM mcp_oauth GROUP BY kind"):
            result[str(row["kind"])] = int(row["n"])
        return result


def _row(kind: str, ident: str) -> sqlite3.Row | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT kind,id,data,expires_at,created_at FROM mcp_oauth WHERE kind=? AND id=?",
            (kind, ident),
        ).fetchone()
    if row is None:
        return None
    expires = row["expires_at"]
    if expires is not None and int(expires) < int(time.time()):
        _delete(kind, ident)
        return None
    return row


def _put(kind: str, ident: str, data: dict[str, Any], expires_at: int | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO mcp_oauth(kind,id,data,expires_at,created_at) VALUES(?,?,?,?,?)",
            (kind, ident, json.dumps(data, separators=(",", ":")), expires_at, int(time.time())),
        )
        conn.commit()


def _delete(kind: str, ident: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM mcp_oauth WHERE kind=? AND id=?", (kind, ident))
        conn.commit()


def _data(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    try:
        parsed = json.loads(str(row["data"]))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _scopes(data: dict[str, Any]) -> list[str]:
    value = data.get("scope") or data.get("scopes") or "aiva"
    if isinstance(value, str):
        items = [s for s in value.split() if s]
    elif isinstance(value, list):
        items = [str(s) for s in value if str(s)]
    else:
        items = []
    return items or ["aiva"]


class CompatTokenVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        if STATIC_TOKEN and hmac.compare_digest(token, STATIC_TOKEN):
            return AccessToken(token=token, client_id="aiva-static-admin", scopes=["aiva"], resource=PUBLIC_MCP)
        row = _row("token", token)
        if row is None:
            return None
        data = _data(row)
        return AccessToken(
            token=token,
            client_id=str(data.get("client_id") or "legacy-oauth-client"),
            scopes=_scopes(data),
            expires_at=row["expires_at"],
            resource=str(data.get("resource") or PUBLIC_MCP),
        )


def _redirect_allowed(uri: str) -> bool:
    try:
        parsed = urlparse(uri)
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "https" and host in REDIRECT_HOSTS:
        return True
    if parsed.scheme == "http" and host in {"127.0.0.1", "localhost", "::1"}:
        return True
    return False


def oauth_metadata() -> dict[str, Any]:
    return {
        "issuer": PUBLIC_BASE,
        "authorization_endpoint": PUBLIC_BASE + "/oauth/authorize",
        "token_endpoint": PUBLIC_BASE + "/oauth/token",
        "registration_endpoint": PUBLIC_BASE + "/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": ["aiva"],
    }


async def metadata_route(_: Request) -> Response:
    return JSONResponse(oauth_metadata(), headers={"Cache-Control": "public, max-age=300"})


async def register_route(request: Request) -> Response:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)
    redirects = body.get("redirect_uris") or []
    if not isinstance(redirects, list) or not redirects or not all(isinstance(x, str) and _redirect_allowed(x) for x in redirects):
        return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
    client_id = secrets.token_urlsafe(32)
    record = {
        "client_id": client_id,
        "client_name": str(body.get("client_name") or "MCP client")[:200],
        "redirect_uris": redirects,
        "grant_types": body.get("grant_types") or ["authorization_code", "refresh_token"],
        "response_types": body.get("response_types") or ["code"],
        "token_endpoint_auth_method": "none",
    }
    _put("client", client_id, record)
    response = dict(body)
    response.update(record)
    response["client_id_issued_at"] = int(time.time())
    return JSONResponse(response, status_code=201)


def _authorization_request(query: dict[str, str]) -> tuple[dict[str, Any] | None, str | None]:
    if query.get("response_type") != "code":
        return None, "unsupported_response_type"
    client_id = query.get("client_id", "")
    client = _data(_row("client", client_id))
    if not client:
        return None, "unauthorized_client"
    redirect_uri = query.get("redirect_uri", "")
    if redirect_uri not in (client.get("redirect_uris") or []) or not _redirect_allowed(redirect_uri):
        return None, "invalid_request"
    challenge = query.get("code_challenge", "")
    method = query.get("code_challenge_method", "S256")
    if not challenge or method != "S256":
        return None, "invalid_request"
    return {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": query.get("state"),
        "code_challenge": challenge,
        "code_challenge_method": method,
        "scope": query.get("scope") or "aiva",
        "resource": query.get("resource") or PUBLIC_MCP,
    }, None


async def authorize_route(request: Request) -> Response:
    if request.method == "GET":
        query = {k: v for k, v in request.query_params.items()}
        pending, error = _authorization_request(query)
        if error:
            return JSONResponse({"error": error}, status_code=400)
        assert pending is not None
        request_id = secrets.token_urlsafe(32)
        _put("pending", request_id, pending, int(time.time()) + 600)
        client = _data(_row("client", str(pending["client_id"])))
        client_name = html.escape(str(client.get("client_name") or "MCP client"))
        body = f"""<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Authorize AIVA MCP</title></head><body style='font-family:-apple-system,sans-serif;max-width:560px;margin:48px auto;padding:0 20px'>
<h2>Authorize AIVA MCP</h2><p><strong>{client_name}</strong> is requesting access to your Oracle-hosted AIVA tools.</p>
<p>Enter your existing AIVA admin token to approve this connection.</p>
<form method='post'><input type='hidden' name='request_id' value='{html.escape(request_id)}'>
<input type='password' name='approval_token' autocomplete='off' style='width:100%;padding:10px' required>
<button type='submit' style='margin-top:12px;padding:10px 16px'>Authorize</button></form></body></html>"""
        return HTMLResponse(body)

    form = await request.form()
    request_id = str(form.get("request_id") or "")
    approval = str(form.get("approval_token") or "")
    row = _row("pending", request_id)
    pending = _data(row)
    if not pending:
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    if not STATIC_TOKEN or not hmac.compare_digest(approval, STATIC_TOKEN):
        return HTMLResponse("Authorization denied.", status_code=403)
    _delete("pending", request_id)
    code = secrets.token_urlsafe(48)
    _put(
        "code",
        code,
        {
            "client_id": pending["client_id"],
            "redirect_uri": pending["redirect_uri"],
            "code_challenge": pending["code_challenge"],
            "code_challenge_method": "S256",
            "scope": pending.get("scope") or "aiva",
            "resource": pending.get("resource") or PUBLIC_MCP,
        },
        int(time.time()) + 300,
    )
    params = {"code": code, "iss": PUBLIC_BASE}
    if pending.get("state"):
        params["state"] = str(pending["state"])
    separator = "&" if "?" in str(pending["redirect_uri"]) else "?"
    return RedirectResponse(str(pending["redirect_uri"]) + separator + urlencode(params), status_code=302)


def _pkce_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _token_response(client_id: str, refresh_token: str | None = None, scope: str = "aiva", resource: str = PUBLIC_MCP) -> dict[str, Any]:
    access = secrets.token_urlsafe(48)
    refresh = refresh_token or secrets.token_urlsafe(48)
    _put("token", access, {"client_id": client_id, "refresh": refresh, "scope": scope, "resource": resource})
    _put("refresh", refresh, {"client_id": client_id, "is_refresh": True, "scope": scope, "resource": resource})
    return {
        "access_token": access,
        "token_type": "Bearer",
        "refresh_token": refresh,
        "scope": scope,
    }


async def token_route(request: Request) -> Response:
    form = await request.form()
    grant = str(form.get("grant_type") or "")
    client_id = str(form.get("client_id") or "")
    if not client_id or _row("client", client_id) is None:
        return JSONResponse({"error": "invalid_client"}, status_code=401)

    if grant == "authorization_code":
        code = str(form.get("code") or "")
        row = _row("code", code)
        data = _data(row)
        if not data or str(data.get("client_id")) != client_id:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        redirect_uri = str(form.get("redirect_uri") or "")
        verifier = str(form.get("code_verifier") or "")
        if redirect_uri != str(data.get("redirect_uri") or "") or not verifier:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        try:
            valid_pkce = hmac.compare_digest(_pkce_s256(verifier), str(data.get("code_challenge") or ""))
        except Exception:
            valid_pkce = False
        if not valid_pkce:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        _delete("code", code)
        return JSONResponse(
            _token_response(
                client_id,
                scope=str(data.get("scope") or "aiva"),
                resource=str(data.get("resource") or PUBLIC_MCP),
            )
        )

    if grant == "refresh_token":
        refresh = str(form.get("refresh_token") or "")
        row = _row("refresh", refresh)
        data = _data(row)
        if not data or str(data.get("client_id")) != client_id:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        return JSONResponse(
            _token_response(
                client_id,
                refresh_token=refresh,
                scope=str(data.get("scope") or "aiva"),
                resource=str(data.get("resource") or PUBLIC_MCP),
            )
        )

    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


def oauth_counts() -> dict[str, int]:
    with _connect() as conn:
        return {str(r["kind"]): int(r["n"]) for r in conn.execute("SELECT kind,COUNT(*) n FROM mcp_oauth GROUP BY kind")}
