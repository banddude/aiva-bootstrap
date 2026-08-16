"""Rig smoke test: server boots, machine connects, MCP initialize works.

If this fails, every other test's failure is meaningless, so it runs first
and fails with the underlying cause visible.
"""

from __future__ import annotations

import pytest
from rig import Rig

pytestmark = pytest.mark.asyncio


async def test_rig_boots_and_lists_tools():
    async with Rig() as rig:
        result = await rig.session.list_tools()
        names = sorted(t.name for t in result.tools)
        assert names == [
            "get_file", "get_skill", "inspect_machine", "job_result",
            "list_directory", "list_skills", "notify", "read_file",
            "run_command", "run_command_async", "send_file", "write_file",
        ], f"advertised tool set drifted: {names}"


async def test_rig_stub_machine_connects_and_executes():
    async with Rig() as rig:
        await rig.connect_machine()
        parsed, is_error = await rig.call_tool(
            "run_command", {"machine": "oracle", "command": "echo rig-alive && pwd"}
        )
        assert not is_error
        assert parsed["ok"] is True
        assert "rig-alive" in parsed["stdout"]
        # the stub runs in the sandbox: pwd proves WHERE it executed
        assert parsed["stdout"].strip().endswith("machine-home")
