#!/usr/bin/env python3
"""Regenerate contract/worker-tools.json and its sha256 pin from a real server boot.

Since the Cloudflare-removal transition the reference surface is THIS repo's
server (the ChatGPT connector at mcp.officeadmin.io), not the cloud Worker:
the Worker no longer tracks local-only tools (notify_dev, read_image,
voice_speak_and_wait). This boots the real server in-process with the same
environment tests/conftest.py uses (dead worker URL, tmp state, no secrets,
no real machine), lists tools over real MCP HTTP, and writes the contract
file and its pin. The only supported way to change the contract.

  .venv/bin/python scripts/regen-contract.py

Commit contract/worker-tools.json and contract/worker-tools.SHA256 together.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "src"))

CONTRACT_PATH = ROOT / "contract" / "worker-tools.json"
PIN_PATH = ROOT / "contract" / "worker-tools.SHA256"


def _bootstrap_env() -> None:
    """Same laws as tests/conftest.py: dead worker URL, tmp state, no secrets."""
    tmp = Path(tempfile.mkdtemp(prefix="aiva-regen-"))
    os.environ["AIVA_MCP_URL"] = "http://127.0.0.1:9"
    os.environ["MCP_TOKEN"] = "aiva-contract-regen-token"
    os.environ["AIVA_HOME"] = str(tmp / "home")
    os.environ["AIVA_STATE"] = str(tmp / "state")
    os.environ["AIVA_OAUTH_DB"] = str(tmp / "oauth.sqlite3")
    os.environ["AIVA_OAUTH_IMPORT"] = str(tmp / "no-import.json")
    os.environ["AIVA_PUBLIC_BASE"] = "http://127.0.0.1:9"
    os.environ["AIVA_HOST"] = "127.0.0.1"
    os.environ["AIVA_CAO_BRIDGE_STATE_DIR"] = str(tmp / "cao-spool")
    os.environ["AIVA_CAO_DEV_BRIDGE_STATE_DIR"] = str(tmp / "cao-dev-spool")
    os.environ["AIVA_NOTIFY_BIN"] = str(ROOT / "tests" / "fixtures" / "notify_shim.py")
    for key in ("SENDBLUE_API_KEY", "SENDBLUE_API_SECRET"):
        os.environ.pop(key, None)
    os.environ["AIVA_SENDBLUE_CREDS"] = str(tmp / "sendblue-creds-absent.env")


async def main() -> int:
    _bootstrap_env()
    from rig import Rig

    async with Rig() as rig:
        result = await rig.session.list_tools()
        tools = {
            t.name: {"description": t.description or "", "inputSchema": t.input_schema}
            for t in sorted(result.tools, key=lambda t: t.name)
        }

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    provenance = {
        "what": "Reference tool contract: the AIVA MCP tools as advertised by this repo's local Oracle MCP server (the ChatGPT connector surface, mcp.officeadmin.io).",
        "source": "local Oracle MCP server boot (tests rig, in-process, no real machine)",
        "regenerated": f"{today} by scripts/regen-contract.py",
        "transition_note": "During the Cloudflare-removal transition the cloud Worker lags this surface; notify_dev, read_image and voice_speak_and_wait are local-only. scripts/parity-check --refresh (Worker) is retired as the contract source; this script replaces it.",
        "rule": "This file is GENERATED. Never edit it by hand. Regenerate with scripts/regen-contract.py after any tool-surface change and commit the file and its pin together.",
        "gate_scope": "scripts/parity-check and tests/test_parity.py gate on name + inputSchema exactly, and on the sha256 pin in contract/worker-tools.SHA256.",
    }
    CONTRACT_PATH.write_text(
        json.dumps({"_provenance": provenance, "tools": tools}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(
        (json.dumps(tools, indent=2, sort_keys=True) + "\n").encode()
    ).hexdigest()
    PIN_PATH.write_text(digest + "\n", encoding="utf-8")
    print(f"wrote {CONTRACT_PATH} ({len(tools)} tools) and {PIN_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
