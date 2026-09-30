from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

import server
import voice_bridge


@pytest.mark.asyncio
async def test_voice_tool_forwards_reply_to_same_agent(monkeypatch, tmp_path):
    key = tmp_path / "key"
    key.write_text("test-owner-key")
    monkeypatch.setenv("AIVA_OFFICEADMIN_KEY_FILE", str(key))
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"status": "finalized", "transcript": "wait a second", "interrupted": True})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(voice_bridge.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    result = await server.voice_speak_and_wait(sessionId="chat-1", turnId="turn-1", text="Hello", waitSeconds=5)
    assert not result.is_error
    assert '"interrupted": true' in result.content[0].text
    assert '"transcript": "wait a second"' in result.content[0].text
    assert len(seen) == 1
    assert seen[0].url.path.endswith("/speak-and-wait")
    assert seen[0].headers["authorization"] == "Bearer test-owner-key"


@pytest.mark.asyncio
async def test_transport_failure_does_not_retry_or_leak_key(monkeypatch, tmp_path):
    key = tmp_path / "key"
    key.write_text("secret-never-display")
    monkeypatch.setenv("AIVA_OFFICEADMIN_KEY_FILE", str(key))
    real_client = httpx.AsyncClient
    calls = []
    def fail(request):
        calls.append(request)
        raise httpx.ReadTimeout("secret-never-display")
    monkeypatch.setattr(voice_bridge.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(fail), **kw))
    result = await voice_bridge.speak_and_wait({"sessionId": "s", "turnId": "t", "text": "hello"})
    assert not result["ok"]
    assert "SAME" in result["hint"]
    assert "secret-never-display" not in str(result)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_missing_credential_fails_without_network(monkeypatch, tmp_path):
    monkeypatch.setenv("AIVA_OFFICEADMIN_KEY_FILE", str(tmp_path / "missing"))
    client = AsyncMock()
    monkeypatch.setattr(voice_bridge.httpx, "AsyncClient", client)
    assert not (await voice_bridge.speak_and_wait({}))["ok"]
    client.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_tool_input_cannot_ring(monkeypatch):
    call = AsyncMock()
    monkeypatch.setattr(server, "speak_and_wait", call)
    with pytest.raises(ToolError, match="at least 1 character"):
        await server.mcp.call_tool("voice_speak_and_wait", {"sessionId": "", "turnId": "t", "text": "hello"})
    call.assert_not_called()
