"""All twelve MCP tools exercised FOR REAL over real MCP HTTP.

Real server (in-process, ephemeral port), real WebSocket machine agent
speaking the production wire protocol, real execution: commands run in bash,
files are written and read back from disk, base64 round-trips real bytes.
Nothing in src/ is mocked. The only substituted component is the machine
itself (see tests/rig.py for why that is the right boundary).

Machine is passed explicitly on every machine-backed call because the Worker
contract (and this server, matching it) requires it.
"""

from __future__ import annotations

import asyncio
import base64

import pytest
from rig import Rig

pytestmark = pytest.mark.asyncio


async def test_run_command_executes_and_reports_exit_code():
    async with Rig() as rig:
        await rig.connect_machine()
        parsed, is_error = await rig.call_tool(
            "run_command", {"machine": "oracle", "command": "echo ci-marker-7f3 && pwd"}
        )
        assert not is_error
        assert parsed["ok"] is True
        assert parsed["exit_code"] == 0
        assert "ci-marker-7f3" in parsed["stdout"]
        assert parsed["stdout"].rstrip().endswith("machine-home")  # ran in the sandbox


async def test_run_command_reports_failure_without_mcp_error():
    """A failing command is a successful tool call reporting exit_code, not a
    transport error. ok:false + isError mirrors the Worker's behavior."""
    async with Rig() as rig:
        await rig.connect_machine()
        parsed, is_error = await rig.call_tool(
            "run_command", {"machine": "oracle", "command": "echo oops >&2; exit 7"}
        )
        assert is_error  # ok:false surfaces as isError, matching the Worker
        assert parsed["ok"] is False
        assert parsed["exit_code"] == 7
        assert "oops" in parsed["stderr"]


async def test_run_command_timeout_is_honest_and_teaches_extension():
    async with Rig() as rig:
        await rig.connect_machine()
        parsed, is_error = await rig.call_tool(
            "run_command", {"machine": "oracle", "command": "sleep 30", "timeout_seconds": 1}
        )
        assert is_error
        assert parsed["ok"] is False
        assert parsed["timed_out"] is True
        assert "how_to_extend" in parsed, "timeout result must tell the caller how to proceed"


async def test_inspect_machine_accepts_allowlisted_command():
    async with Rig() as rig:
        await rig.connect_machine()
        parsed, is_error = await rig.call_tool(
            "inspect_machine", {"machine": "oracle", "command": "pwd"}
        )
        assert not is_error
        assert parsed["ok"] is True
        assert parsed["refused"] is False
        assert parsed["stdout"].strip().endswith("machine-home")


async def test_inspect_machine_refuses_non_allowlisted_command():
    async with Rig() as rig:
        await rig.connect_machine()
        parsed, is_error = await rig.call_tool(
            "inspect_machine", {"machine": "oracle", "command": "rm -rf /"}
        )
        assert is_error
        assert parsed["ok"] is False
        assert parsed["refused"] is True, "a refused inspection must say refused, having run nothing"
        assert parsed["exit_code"] is None
        assert parsed.get("stdout") is None, "a refused inspection ran nothing, so there is no stdout"


async def test_inspect_machine_argv_form_passes_spaces_literally():
    async with Rig() as rig:
        await rig.connect_machine()
        parsed, is_error = await rig.call_tool(
            "inspect_machine", {"machine": "oracle", "argv": ["echo", "two words", "with spaces"]}
        )
        assert not is_error
        assert "two words with spaces" in parsed["stdout"]


async def test_run_command_async_and_job_result_round_trip():
    async with Rig() as rig:
        await rig.connect_machine()
        parsed, is_error = await rig.call_tool(
            "run_command_async", {"machine": "oracle", "command": "sleep 0.3; echo async-done-marker"}
        )
        assert not is_error
        assert parsed["ok"] is True
        job_id = parsed["job_id"]
        assert job_id

        final = None
        for _ in range(60):  # poll like a real client
            parsed, is_error = await rig.call_tool("job_result", {"job_id": job_id})
            assert not is_error
            if parsed["status"] == "done":
                final = parsed
                break
            assert parsed["status"] == "running"
            await asyncio.sleep(0.25)
        assert final is not None, "job never completed"
        assert final["result"]["ok"] is True
        assert "async-done-marker" in final["result"]["stdout"]


async def test_job_result_consumes_once_and_rejects_unknown_ids():
    async with Rig() as rig:
        await rig.connect_machine()
        parsed, _ = await rig.call_tool(
            "run_command_async", {"machine": "oracle", "command": "echo consumed"}
        )
        job_id = parsed["job_id"]
        for _ in range(60):
            parsed, _ = await rig.call_tool("job_result", {"job_id": job_id})
            if parsed["status"] == "done":
                break
            await asyncio.sleep(0.25)
        # second read: consumed once, as the tool description promises
        parsed, is_error = await rig.call_tool("job_result", {"job_id": job_id})
        assert is_error
        assert parsed["ok"] is False
        # unknown id
        parsed, is_error = await rig.call_tool("job_result", {"job_id": "no-such-job"})
        assert is_error
        assert "unknown or expired" in parsed["error"]


async def test_write_read_file_round_trip():
    async with Rig() as rig:
        await rig.connect_machine()
        parsed, is_error = await rig.call_tool(
            "write_file", {"machine": "oracle", "path": "out/roundtrip.txt", "content": "l1\nl2\nl3\n"}
        )
        assert not is_error
        assert parsed["ok"] is True
        assert parsed["bytes"] == 9  # "l1\nl2\nl3\n"

        parsed, _ = await rig.call_tool(
            "read_file", {"machine": "oracle", "path": "out/roundtrip.txt"}
        )
        assert parsed["content"] == "l1\nl2\nl3\n"

        parsed, _ = await rig.call_tool(
            "read_file", {"machine": "oracle", "path": "out/roundtrip.txt", "offset": 1, "limit": 1}
        )
        assert parsed["content"] == "l2", f"offset/limit slicing wrong: {parsed['content']!r}"

        parsed, _ = await rig.call_tool(
            "write_file", {"machine": "oracle", "path": "out/roundtrip.txt", "content": "l4\n", "mode": "append"}
        )
        parsed, _ = await rig.call_tool(
            "read_file", {"machine": "oracle", "path": "out/roundtrip.txt"}
        )
        assert parsed["content"] == "l1\nl2\nl3\nl4\n"


async def test_list_directory_walks_the_tree():
    async with Rig() as rig:
        await rig.connect_machine()
        parsed, is_error = await rig.call_tool(
            "list_directory", {"machine": "oracle", "path": "projects", "depth": 2}
        )
        assert not is_error
        assert parsed["ok"] is True
        entries = parsed["entries"]
        assert "alpha/" in entries
        assert "alpha/a.txt" in entries
        assert "beta.md" in entries


async def test_list_skills_and_get_skill():
    async with Rig() as rig:
        await rig.connect_machine()
        parsed, is_error = await rig.call_tool("list_skills", {"machine": "oracle"})
        assert not is_error
        names = [s["name"] for s in parsed["skills"]]
        assert "start-here" in names
        descriptions = {s["name"]: s["description"] for s in parsed["skills"]}
        assert "identity" in descriptions["start-here"].lower()

        parsed, _ = await rig.call_tool(
            "get_skill", {"machine": "oracle", "skill_name": "start-here"}
        )
        assert parsed["ok"] is True
        assert "test machine" in parsed["content"]

        parsed, is_error = await rig.call_tool(
            "get_skill", {"machine": "oracle", "skill_name": "does-not-exist"}
        )
        assert is_error
        assert "not found" in parsed["error"]


async def test_send_get_file_binary_round_trip_and_overwrite_guard():
    payload = bytes(range(256)) * 4  # 1 KiB of binary, not valid UTF-8
    async with Rig() as rig:
        await rig.connect_machine()
        parsed, is_error = await rig.call_tool(
            "send_file",
            {
                "machine": "oracle",
                "path": "bin/blob.bin",
                "content_base64": base64.b64encode(payload).decode(),
            },
        )
        assert not is_error
        assert parsed["bytes"] == len(payload)

        # without overwrite, an existing file must be refused
        parsed, is_error = await rig.call_tool(
            "send_file",
            {
                "machine": "oracle",
                "path": "bin/blob.bin",
                "content_base64": base64.b64encode(b"x").decode(),
            },
        )
        assert is_error
        assert "overwrite" in parsed["error"]

        parsed, _ = await rig.call_tool(
            "send_file",
            {
                "machine": "oracle",
                "path": "bin/blob.bin",
                "content_base64": base64.b64encode(b"x").decode(),
                "overwrite": True,
            },
        )
        assert parsed["ok"] is True

        parsed, _ = await rig.call_tool("get_file", {"machine": "oracle", "path": "bin/blob.bin"})
        assert parsed["type"] == "resource"
        assert parsed["resource"]["mimeType"] == "application/octet-stream"
        assert base64.b64decode(parsed["resource"]["blob"]) == b"x"


async def test_unknown_machine_is_rejected_by_validation():
    async with Rig() as rig:
        await rig.connect_machine()
        parsed, is_error = await rig.call_tool(
            "run_command", {"machine": "toaster", "command": "echo hi"}
        )
        assert is_error, "machine must be one of laptop, oracle, mac-server"


async def test_offline_machine_is_reported_not_connected():
    """No machine connected: honest, fast failure. The Worker contract says
    this exact thing happens; it must not hang or invent a result."""
    async with Rig() as rig:  # deliberately no connect_machine()
        parsed, is_error = await rig.call_tool(
            "run_command", {"machine": "oracle", "command": "echo hi"}
        )
        assert is_error
        assert parsed["ok"] is False
        assert "not connected" in parsed["error"]


async def test_notify_rejects_empty_message():
    async with Rig() as rig:
        parsed, is_error = await rig.call_tool("notify", {"target": "aiva", "message": "   "})
        assert is_error
        assert parsed["ok"] is False
        assert "message" in parsed["error"].lower()
