from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("MCP_TOKEN", "test-token")
os.environ.setdefault("AIVA_HOME", "/tmp/aiva-bootstrap-test-home")
os.environ.setdefault("AIVA_STATE", "/tmp/aiva-bootstrap-test-state")
os.environ.setdefault("AIVA_PUBLIC_BASE", "http://127.0.0.1:18765")
os.environ.setdefault("AIVA_OAUTH_DB", "/tmp/aiva-bootstrap-test-oauth.sqlite3")
os.environ.setdefault("AIVA_OAUTH_IMPORT", "/tmp/aiva-bootstrap-no-import.json")

import notify_delivery
import server
from tool_contract import WORKER_TOOL_SCHEMAS


class ContractTests(unittest.TestCase):
    def test_declared_schemas_match_worker_contract(self) -> None:
        async def declared():
            return {tool.name: tool.input_schema for tool in await server.mcp.list_tools()}

        self.assertEqual(asyncio.run(declared()), WORKER_TOOL_SCHEMAS)


class GetFileTests(unittest.TestCase):
    def test_get_file_returns_embedded_blob_resource(self) -> None:
        async def dispatch(machine, tool, args):
            self.assertEqual((machine, tool, args), ("oracle", "get_file", {"path": "/tmp/aiva-chatgpt-transfer/report.pdf"}))
            return {
                "ok": True,
                "path": "/tmp/aiva-chatgpt-transfer/report.pdf",
                "bytes": 4,
                "content_base64": "JVBERg==",
            }

        with patch.object(server.AGENT_HUB, "dispatch", side_effect=dispatch):
            result = asyncio.run(server.get_file(machine="oracle", path="report.pdf"))

        self.assertFalse(result.is_error)
        self.assertEqual(len(result.content), 1)
        embedded = result.content[0]
        self.assertEqual(embedded.type, "resource")
        self.assertEqual(embedded.resource.uri, "aiva-file://oracle/tmp/aiva-chatgpt-transfer/report.pdf")
        self.assertEqual(embedded.resource.mime_type, "application/pdf")
        self.assertEqual(embedded.resource.blob, "JVBERg==")
        self.assertEqual(embedded.resource.meta["filename"], "report.pdf")
        self.assertEqual(embedded.resource.meta["size"], 4)

    def test_get_file_preserves_error_as_text(self) -> None:
        async def dispatch(machine, tool, args):
            return {"ok": False, "error": "missing"}

        with patch.object(server.AGENT_HUB, "dispatch", side_effect=dispatch):
            result = asyncio.run(server.get_file(machine="oracle", path="missing.pdf"))

        self.assertTrue(result.is_error)
        self.assertEqual(result.content[0].type, "text")
        self.assertIn("missing", result.content[0].text)

    def test_get_file_rejects_unstaged_absolute_path_without_dispatch(self) -> None:
        with patch.object(server.AGENT_HUB, "dispatch") as dispatch:
            result = asyncio.run(server.get_file(machine="oracle", path="/etc/passwd"))

        self.assertTrue(result.is_error)
        self.assertIn("only reads files staged under", result.content[0].text)
        dispatch.assert_not_called()

    def test_get_file_rejects_path_traversal_without_dispatch(self) -> None:
        with patch.object(server.AGENT_HUB, "dispatch") as dispatch:
            result = asyncio.run(server.get_file(machine="oracle", path="../secret.txt"))

        self.assertTrue(result.is_error)
        self.assertIn("path traversal", result.content[0].text)
        dispatch.assert_not_called()


class NotifyTests(unittest.TestCase):
    def test_aiva_writes_local_bridge_spool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {"AIVA_CAO_BRIDGE_STATE_DIR": str(root)}, clear=False):
                result = notify_delivery.dispatch_notify("aiva", "hello", "test-agent")
            self.assertTrue(result["ok"])
            files = list((root / "pending").glob("*.json"))
            self.assertEqual(len(files), 1)
            body = json.loads(files[0].read_text())
            self.assertEqual(body["source"], "aiva-inbox")
            self.assertIn("from=test-agent", body["message"])
            self.assertTrue(body["message"].endswith("\nhello"))

    def test_dev_compatibility_target_uses_dev_spool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {"AIVA_CAO_DEV_BRIDGE_STATE_DIR": str(root)}, clear=False):
                result = notify_delivery.dispatch_notify("dev", "hello dev", "test-agent")
            self.assertTrue(result["ok"])
            self.assertEqual(len(list((root / "pending").glob("*.json"))), 1)

    def test_mike_uses_direct_sendblue(self) -> None:
        fake = {"channel": "mike", "ok": True, "detail": "accepted"}
        with patch.object(notify_delivery, "send_mike", return_value=fake) as send:
            result = notify_delivery.dispatch_notify("mike", "test", "agent")
        self.assertTrue(result["ok"])
        send.assert_called_once_with("test", "agent")


if __name__ == "__main__":
    unittest.main()
