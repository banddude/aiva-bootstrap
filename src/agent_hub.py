from __future__ import annotations

import asyncio
import hmac
import json
import sqlite3
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

MACHINES = ("laptop", "oracle", "mac-server")


class AgentHub:
    """Oracle-hosted replacement for the old Worker AgentHub.

    The existing machine agents already speak this wire format:
      client -> {"type":"ping"}
      server -> {"type":"pong"}
      server -> {"type":"exec","id", "tool", "args"}
      client -> {"type":"result","id", "result"}

    Tool execution itself remains in agent/agent.js on each machine so shell,
    skill, file and inspection behavior stays identical to the original MCP.
    """

    def __init__(self, *, token: str, state_dir: Path) -> None:
        self.token = token
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.state_dir / "agent-jobs.sqlite3"
        self.sockets: dict[str, set[WebSocket]] = defaultdict(set)
        self.pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs(
                  id TEXT PRIMARY KEY,
                  status TEXT NOT NULL,
                  machine TEXT NOT NULL,
                  tool TEXT NOT NULL,
                  started REAL NOT NULL,
                  result TEXT
                )
                """
            )
            conn.execute("DELETE FROM jobs WHERE started < ?", (time.time() - 6 * 3600,))
            conn.commit()

    async def websocket(self, websocket: WebSocket) -> None:
        machine = (websocket.query_params.get("machine") or "").strip()
        supplied = websocket.query_params.get("token") or ""
        if machine not in MACHINES or not self.token or not hmac.compare_digest(supplied, self.token):
            await websocket.close(code=1008)
            return

        await websocket.accept()
        self.sockets[machine].add(websocket)
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if msg.get("type") == "ping":
                    await websocket.send_text('{"type":"pong"}')
                    continue
                if msg.get("type") != "result":
                    continue
                job_id = str(msg.get("id") or "")
                result = msg.get("result")
                if not isinstance(result, dict):
                    result = {"ok": False, "error": "bad result from machine agent"}

                future = self.pending.get(job_id)
                if future is not None and not future.done():
                    future.set_result(result)

                with self._connect() as conn:
                    row = conn.execute("SELECT id FROM jobs WHERE id=? AND status='running'", (job_id,)).fetchone()
                    if row is not None:
                        conn.execute(
                            "UPDATE jobs SET status='done', result=? WHERE id=?",
                            (json.dumps(result, separators=(",", ":")), job_id),
                        )
                        conn.commit()
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            self.sockets[machine].discard(websocket)

    async def _send(self, machine: str, payload: dict[str, Any]) -> int:
        sockets = list(self.sockets.get(machine) or ())
        sent = 0
        for ws in sockets:
            try:
                await ws.send_text(json.dumps(payload, separators=(",", ":")))
                sent += 1
            except Exception:
                self.sockets[machine].discard(ws)
        return sent

    async def dispatch(self, machine: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        timeout_sec = min(float(args.get("timeout_seconds") or 10) or 10, 25)
        job_id = str(uuid.uuid4())
        tool_args = dict(args)
        tool_args.pop("machine", None)
        tool_args["timeout_seconds"] = timeout_sec
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self.pending[job_id] = future
        try:
            sent = await self._send(
                machine,
                {"type": "exec", "id": job_id, "tool": tool, "args": tool_args},
            )
            if not sent:
                return {"ok": False, "error": f"machine '{machine}' not connected", "machine": machine}

            # Preserve the old Worker bound: command timeout + 3s, capped near 20s.
            wait_sec = min(timeout_sec + 3, 20)
            try:
                return await asyncio.wait_for(future, timeout=wait_sec)
            except TimeoutError:
                return {
                    "ok": False,
                    "error": (
                        f"timeout after {timeout_sec:g}s waiting for {machine} "
                        "(no reply; the agent socket may be stale and reconnecting, or the command "
                        "ran past the ~20s sync limit; retry, or use run_command_async for long jobs)"
                    ),
                    "machine": machine,
                    "stale_socket_suspected": True,
                }
        finally:
            self.pending.pop(job_id, None)

    async def dispatch_async(self, machine: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        timeout_sec = min(float(args.get("timeout_seconds") or 10) or 10, 600)
        job_id = str(uuid.uuid4())
        tool_args = dict(args)
        tool_args.pop("machine", None)
        tool_args["timeout_seconds"] = timeout_sec
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO jobs(id,status,machine,tool,started,result) VALUES(?,?,?,?,?,NULL)",
                (job_id, "running", machine, tool, time.time()),
            )
            conn.commit()

        sent = await self._send(
            machine,
            {"type": "exec", "id": job_id, "tool": tool, "args": tool_args},
        )
        if not sent:
            with self._connect() as conn:
                conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
                conn.commit()
            return {"ok": False, "error": f"machine '{machine}' not connected", "machine": machine}
        return {
            "ok": True,
            "async": True,
            "job_id": job_id,
            "status": "running",
            "note": "Job started in the background. Poll job_result with this job_id to get the output.",
        }

    def job_result(self, job_id: str) -> dict[str, Any]:
        if not job_id:
            return {"ok": False, "error": "job_id required"}
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id,status,machine,tool,started,result FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            if row is None:
                return {"ok": False, "error": f"unknown or expired job_id: {job_id}"}
            if row["status"] == "running":
                return {
                    "ok": True,
                    "status": "running",
                    "job_id": job_id,
                    "elapsed_sec": round(time.time() - float(row["started"])),
                }
            try:
                result = json.loads(str(row["result"] or "{}"))
            except Exception:
                result = {"ok": False, "error": "bad stored result"}
            conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            conn.commit()
        return {"ok": True, "status": "done", "job_id": job_id, "result": result}

    def status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "machines": {machine: {"connected": bool(self.sockets.get(machine)), "sockets": len(self.sockets.get(machine) or ())} for machine in MACHINES},
        }
