"""The Cloudflare-is-dead tests. These exist because of the 2026-08-15 outage.

That day, a check "passed" only because Cloudflare happened to be healthy: the
LOCAL path was broken and nothing noticed for ten hours. The law these tests
enforce: the entire local path must carry the server on its own, with the
Worker URL pointed at a address that refuses every connection. If any part of
this suite can only pass while Cloudflare is reachable, that part is lying.

AIVA_MCP_URL is set to http://127.0.0.1:9 (discard port; connection refused
instantly) by tests/conftest.py before any src import, for the whole session.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from rig import Rig

pytestmark = pytest.mark.asyncio

DEAD_WORKER_URL = "http://127.0.0.1:9"

# PR #5 replaces the notify CLI shell-out with in-process delivery; tests that
# target the CLI plumbing only make sense where it exists.
try:
    import notify_delivery  # noqa: F401

    NOTIFY_IS_INPROCESS = True
except ImportError:
    NOTIFY_IS_INPROCESS = False


async def test_the_dead_url_is_actually_dead():
    """Prove the premise: nothing listens there, and connecting is refused."""
    assert os.environ.get("AIVA_MCP_URL") == DEAD_WORKER_URL, (
        "conftest must keep AIVA_MCP_URL at the dead address; a healthy-Worker "
        "test run proves nothing"
    )
    try:
        _, writer = await asyncio.open_connection("127.0.0.1", 9)
    except (ConnectionRefusedError, OSError):
        return  # refused: exactly what we want
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    pytest.fail("something is listening on 127.0.0.1:9; the dead-Worker premise is broken")


async def test_machine_tools_run_locally_with_worker_dead():
    """With Cloudflare unreachable, a machine tool call still executes on the
    connected machine and returns that machine's own output. The pwd assertion
    proves WHERE execution happened; no cloud could have produced it."""
    async with Rig() as rig:
        await rig.connect_machine()
        parsed, is_error = await rig.call_tool(
            "run_command", {"machine": "oracle", "command": "echo local-path-proof && pwd"}
        )
        assert not is_error
        assert parsed["ok"] is True
        assert parsed["stdout"].rstrip().endswith("machine-home")


async def test_notify_aiva_delivers_via_local_spool_with_worker_dead():
    """notify target=aiva must land in the local CAO spool with the Worker
    dead. This is the exact delivery that silently broke for ten hours."""
    spool = Path(os.environ["AIVA_CAO_BRIDGE_STATE_DIR"])
    before = set((spool / "pending").glob("*.json")) if (spool / "pending").is_dir() else set()

    async with Rig() as rig:
        await rig.connect_machine()
        parsed, is_error = await rig.call_tool(
            "notify",
            {"target": "aiva", "message": "dead-worker delivery check", "from": "ci"},
        )
        assert not is_error, f"notify failed with the Worker dead: {parsed}"
        assert parsed["ok"] is True
        assert "aiva" in parsed.get("delivered", parsed.get("detail", "")) or parsed.get("status") in ("delivered", "success")

    pending = spool / "pending"
    new = [f for f in pending.glob("*.json") if f not in before]
    assert len(new) == 1, f"expected exactly one new spool record, found {len(new)}"
    body = json.loads(new[0].read_text(encoding="utf-8"))
    rendered = body["message"]
    assert "dead-worker delivery check" in rendered
    assert "from=ci" in rendered


@pytest.mark.skipif(NOTIFY_IS_INPROCESS, reason="PR #5+ notify is in-process; CLI plumbing test does not apply")
async def test_notify_cli_failure_is_reported_not_swallowed():
    """main branch: when the delivery CLI fails, the tool must say so. A
    delivery failure reported as success is how outages hide."""
    async with Rig() as rig:
        os.environ["AIVA_NOTIFY_SHIM_MODE"] = "fail"
        try:
            parsed, is_error = await rig.call_tool(
                "notify", {"target": "aiva", "message": "should fail honestly", "from": "ci"}
            )
        finally:
            os.environ.pop("AIVA_NOTIFY_SHIM_MODE", None)
        assert is_error
        assert parsed["ok"] is False
        assert "fail" in json.dumps(parsed).lower()


@pytest.mark.skipif(not NOTIFY_IS_INPROCESS, reason="in-process notify (PR #5+) is required for the no-creds path")
async def test_notify_mike_without_credentials_fails_honestly():
    """No Sendblue credentials in this process (conftest guarantees it): the
    mike channel must fail loudly, never fake success, never text anyone."""
    async with Rig() as rig:
        parsed, is_error = await rig.call_tool(
            "notify", {"target": "mike", "message": "creds absent", "from": "ci"}
        )
        assert is_error
        assert parsed["ok"] is False
        failed_channels = [c["channel"] for c in parsed.get("failed", [])]
        assert "mike" in failed_channels or "credential" in json.dumps(parsed).lower()


async def test_broken_local_path_fails_loudly_not_passingly():
    """The outage shape: local path broken, Worker dead. A tool call must
    return an honest failure FAST. If the server ever grows a silent cloud
    fallback, this test dies on the dead URL instead of quietly passing."""
    async with Rig() as rig:
        await rig.connect_machine()
        await rig.disconnect_machine()  # break the local path on purpose

        parsed, is_error = await rig.call_tool(
            "run_command", {"machine": "oracle", "command": "echo anyone?"},
        )
        assert is_error
        assert parsed["ok"] is False
        assert "not connected" in parsed["error"], (
            "with the machine gone and the Worker URL dead, the only honest "
            f"answer is not-connected; got: {parsed}"
        )


async def test_health_reports_hub_truth():
    """/health must say which machines are actually connected. During the
    outage the status surface lied by omission; this pins it to reality."""
    import httpx2

    async with Rig() as rig:
        await rig.connect_machine()
        async with httpx2.AsyncClient() as client:
            up = await client.get(f"http://127.0.0.1:{rig.server.port}/health")
        assert up.status_code == 200
        machines = up.json()["agent_hub"]["machines"]
        assert machines["oracle"]["connected"] is True
        await rig.disconnect_machine()
        async with httpx2.AsyncClient() as client:
            down = await client.get(f"http://127.0.0.1:{rig.server.port}/health")
        machines = down.json()["agent_hub"]["machines"]
        assert machines["oracle"]["connected"] is False
