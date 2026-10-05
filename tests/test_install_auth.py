from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROVISION = REPO_ROOT / "scripts" / "provision-admin-token.py"


def run_provision(home: Path, token_file: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(PROVISION),
            "--home",
            str(home),
            "--owner",
            os.environ.get("USER", ""),
            "--token-file",
            str(token_file),
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def test_fresh_admin_token_is_private_and_rerun_preserves_it(tmp_path: Path) -> None:
    home = tmp_path / "aiva"
    token_file = home / "admin-token"

    first = run_provision(home, token_file)
    assert first.returncode == 0, first.stderr
    token = token_file.read_text(encoding="utf-8").strip()
    assert len(token) == 64
    assert all(ch in "0123456789abcdef" for ch in token)
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600

    second = run_provision(home, token_file)
    assert second.returncode == 0, second.stderr
    assert token_file.read_text(encoding="utf-8").strip() == token
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


def test_admin_token_provisioning_refuses_symlink(tmp_path: Path) -> None:
    home = tmp_path / "aiva"
    home.mkdir()
    victim = tmp_path / "victim"
    victim.write_text("do-not-touch\n", encoding="utf-8")
    token_file = home / "admin-token"
    token_file.symlink_to(victim)

    result = run_provision(home, token_file)
    assert result.returncode != 0
    assert "symlink" in result.stderr.lower()
    assert victim.read_text(encoding="utf-8") == "do-not-touch\n"


def test_installer_wires_generated_token_file_into_service_env() -> None:
    install = (REPO_ROOT / "install.sh").read_text(encoding="utf-8")
    assert 'TOKEN_FILE="$AIVA_HOME/admin-token"' in install
    assert 'scripts/provision-admin-token.py' in install
    assert "AIVA_TOKEN_FILE=$TOKEN_FILE" in install


def test_generated_token_drives_mcp_auth_and_oauth_approval(tmp_path: Path) -> None:
    home = tmp_path / "aiva"
    token_file = home / "admin-token"
    provision = run_provision(home, token_file)
    assert provision.returncode == 0, provision.stderr

    env = os.environ.copy()
    env.pop("MCP_TOKEN", None)
    env.pop("AIVA_TOKEN", None)
    env.update(
        {
            "AIVA_TOKEN_FILE": str(token_file),
            "AIVA_HOME": str(home),
            "AIVA_STATE": str(home / "state"),
            "AIVA_OAUTH_DB": str(home / "state" / "oauth.sqlite3"),
            "AIVA_OAUTH_IMPORT": str(home / "state" / "no-import.json"),
            "AIVA_PUBLIC_BASE": "http://127.0.0.1",
            "AIVA_HOST": "127.0.0.1",
            "AIVA_TEST_SRC": str(REPO_ROOT / "src"),
        }
    )

    code = r'''
import asyncio
import base64
import hashlib
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.environ["AIVA_TEST_SRC"])

import server
from starlette.testclient import TestClient

token = Path(os.environ["AIVA_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
assert token
assert server.STATIC_TOKEN == token
verifier = server.CompatTokenVerifier()
assert asyncio.run(verifier.verify_token(token)) is not None
assert asyncio.run(verifier.verify_token("wrong-token")) is None
assert asyncio.run(verifier.verify_token("")) is None

initialize = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "bootstrap-auth-test", "version": "1"},
    },
}

with TestClient(server.build_app(), base_url="http://127.0.0.1") as client:
    unauth = client.post(
        "/",
        json=initialize,
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert unauth.status_code in {401, 403}, (unauth.status_code, unauth.text)

    redirect_uri = "http://127.0.0.1/callback"
    register = client.post(
        "/oauth/register",
        json={
            "client_name": "bootstrap-auth-test",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    assert register.status_code == 201, register.text
    client_id = register.json()["client_id"]

    verifier_text = "bootstrap-verifier"
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier_text.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    authorize = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "aiva",
            "state": "test-state",
        },
    )
    assert authorize.status_code == 200, authorize.text
    match = re.search(r"name='request_id' value='([^']+)'", authorize.text)
    assert match, authorize.text
    request_id = match.group(1)

    wrong = client.post(
        "/oauth/authorize",
        data={"request_id": request_id, "approval_token": "wrong-token"},
        follow_redirects=False,
    )
    assert wrong.status_code == 403, wrong.text

    empty = client.post(
        "/oauth/authorize",
        data={"request_id": request_id, "approval_token": ""},
        follow_redirects=False,
    )
    assert empty.status_code == 403, empty.text

    approved = client.post(
        "/oauth/authorize",
        data={"request_id": request_id, "approval_token": token},
        follow_redirects=False,
    )
    assert approved.status_code == 302, approved.text
    assert approved.headers["location"].startswith(redirect_uri + "?")
    assert "code=" in approved.headers["location"]
'''
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
