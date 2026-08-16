"""Parity gate: what this server ADVERTISES over real MCP HTTP must equal the
Cloudflare Worker's declared tool contract, name for name and schema for schema.

The reference is contract/worker-tools.json (generated from the live Worker by
scripts/parity-check --refresh; see its _provenance block). PR #5 keeps the
runtime contract in src/tool_contract.py, independently of this generated
reference. That is useful: if either the runtime contract or the generated
reference drifts, advertised != file and this test is red. The sha256 pin
(contract/worker-tools.SHA256) also catches an unreviewed hand edit of the
generated reference.

A green run here means: pin holds and the server actually advertises exactly
the names and input schemas captured from the Worker. The live Worker is
deliberately NOT contacted:
this gate runs with AIVA_MCP_URL dead (see conftest) so it can never pass by
asking Cloudflare. Freshness vs the live Worker is `parity-check --live`'s
job, run where a token exists.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from rig import Rig

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = REPO_ROOT / "contract" / "worker-tools.json"
PIN_PATH = REPO_ROOT / "contract" / "worker-tools.SHA256"


def load_contract_file() -> dict[str, dict[str, Any]]:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))["tools"]


def load_contract() -> dict[str, dict[str, Any]]:
    return {name: spec["inputSchema"] for name, spec in load_contract_file().items()}


def test_contract_file_matches_its_pin():
    """The contract file is generated, not hand-edited. If this fails, someone
    changed worker-tools.json without running scripts/parity-check --refresh."""
    tools = load_contract_file()
    digest = hashlib.sha256((json.dumps(tools, indent=2, sort_keys=True) + "\n").encode()).hexdigest()
    pinned = PIN_PATH.read_text(encoding="utf-8").strip()
    assert pinned == digest, (
        "contract/worker-tools.json no longer hashes to contract/worker-tools.SHA256. "
        "Regenerate BOTH with scripts/parity-check --refresh (never hand-edit the contract)"
    )


def schema_diff(name: str, advertised: Any, reference: Any, path: str = "") -> list[str]:
    """Human-readable first differences between two schemas."""
    diffs: list[str] = []
    if isinstance(advertised, dict) and isinstance(reference, dict):
        for key in sorted(set(advertised) | set(reference)):
            here = f"{path}.{key}" if path else key
            if key not in advertised:
                diffs.append(f"{name}{path and '.'}{here}: missing from ADVERTISED (Worker declares it)")
            elif key not in reference:
                diffs.append(f"{name}.{here}: EXTRA in ADVERTISED (Worker does not declare it)")
            else:
                diffs.extend(schema_diff(name, advertised[key], reference[key], here))
    elif advertised != reference:
        diffs.append(f"{name}{('.' + path) if path else ''}: advertised={json.dumps(advertised, sort_keys=True)} worker={json.dumps(reference, sort_keys=True)}")
    return diffs


@pytest.mark.asyncio
async def test_advertised_tools_match_worker_contract_exactly():
    contract = load_contract()
    async with Rig() as rig:
        result = await rig.session.list_tools()
        advertised = {t.name: t.input_schema for t in result.tools}

    diffs: list[str] = []
    for name in sorted(set(advertised) | set(contract)):
        if name not in advertised:
            diffs.append(f"{name}: registered on the Worker but NOT advertised by this server")
            continue
        if name not in contract:
            diffs.append(f"{name}: advertised by this server but NOT in the Worker contract")
            continue
        diffs.extend(schema_diff(name, advertised[name], contract[name]))

    assert not diffs, (
        "Oracle tool contract drifted from the Cloudflare Worker contract "
        f"({len(diffs)} difference(s)):\n  " + "\n  ".join(diffs[:40])
    )


@pytest.mark.asyncio
async def test_machine_is_required_on_every_machine_backed_tool():
    """The Worker contract requires `machine` on all 10 machine-backed tools.
    Advertised parity covers the schema; THIS test proves call-time validation
    enforces it, so a client that omits machine gets an error, not a default."""
    contract = load_contract()
    machine_backed = [n for n, s in contract.items() if "machine" in s.get("properties", {})]
    assert len(machine_backed) == 10, f"expected 10 machine-backed tools, contract lists {len(machine_backed)}"

    async with Rig() as rig:
        await rig.connect_machine()
        for tool in machine_backed:
            args: dict[str, object] = {"command": "echo hi"} if tool.startswith("run_command") else {}
            if tool == "write_file":
                args = {"path": "x.txt", "content": "x"}
            elif tool in ("read_file", "get_file"):
                args = {"path": "notes.txt"}
            elif tool == "send_file":
                args = {"path": "y.bin", "content_base64": "eA=="}
            elif tool == "get_skill":
                args = {"skill_name": "start-here"}
            _, is_error = await rig.call_tool(tool, args)  # deliberately NO machine
            assert is_error, (
                f"{tool} accepted a call without `machine`; the Worker contract "
                "requires it, so validation must reject it"
            )
