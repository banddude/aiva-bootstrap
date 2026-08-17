"""The live test rig: a real server, a real WebSocket machine agent, a real MCP client.

Nothing in here mocks code under test. The server (src/server.py) runs for real
on an ephemeral port inside the test process. The "machine" is a stub agent
that speaks the exact wire protocol the real agent.js speaks
(client {"type":"ping"} -> {"type":"pong"}; server {"type":"exec",...} ->
client {"type":"result",...}) and really executes each tool against a sandbox
directory, returning the same result shapes agent.js returns. The client is
the official MCP SDK client speaking JSON-RPC over a real HTTP socket.

Substituting the MACHINE is the correct boundary: CI has no laptop and must
never touch the live oracle agent. Everything above the socket is production
code under test.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
from pathlib import Path
from typing import Any

import uvicorn
import websockets

REPO_ROOT = Path(__file__).resolve().parents[1]

# Mirror of agent.js INSPECT_POLICY, small subset: enough to prove the accepted
# AND refused paths cross the server honestly. The full policy is agent.js's
# concern, not the server's.
STUB_INSPECT_ALLOWLIST = {
    "pwd", "ls", "cat", "echo", "uname", "hostname", "whoami", "date",
    "true", "wc", "head", "stat",
}
def _sandboxed(sandbox: Path, path: str | None, extra_roots: tuple[Path, ...] = ()) -> Path:
    """Resolve a tool path the way agent.js's expand() does, then jail it.

    The jail is the rig's own safety property (production agent.js has none).
    Besides the sandbox home, the server's configured CAO spool dirs are
    approved roots: notify delivery legitimately writes there via a machine
    write_file dispatch, and those dirs are rig-controlled tmp paths.
    """
    p = Path(path or ".")
    if not p.is_absolute():
        p = sandbox / p
    resolved = p.resolve()
    roots = [sandbox.resolve()] + [r.resolve() for r in extra_roots]
    for root in roots:
        if resolved == root or root in resolved.parents:
            return resolved
    raise PermissionError(f"path escapes the test sandbox: {path}")


class StubMachineAgent:
    """A real WebSocket machine agent (protocol-faithful, sandbox-executing)."""

    def __init__(self, uri: str, sandbox: Path, extra_roots: tuple[Path, ...] = ()) -> None:
        self.uri = uri
        self.sandbox = sandbox
        self.extra_roots = extra_roots
        self.ws: Any = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._reader: asyncio.Task[None] | None = None
        self.executed: list[dict[str, Any]] = []  # every exec the server sent

    async def start(self) -> None:
        self.ws = await websockets.connect(self.uri, open_timeout=10)
        self._reader = asyncio.create_task(self._read_loop())

    async def stop(self) -> None:
        if self._reader:
            self._reader.cancel()
        for t in list(self._tasks):
            t.cancel()
        if self.ws:
            await self.ws.close()

    async def _read_loop(self) -> None:
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if msg.get("type") == "ping":
                    await self.ws.send(json.dumps({"type": "pong"}))
                    continue
                if msg.get("type") != "exec":
                    continue
                self.executed.append(msg)
                task = asyncio.create_task(self._execute(msg))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        except Exception:
            pass  # socket closed; stop() drives teardown

    async def _execute(self, msg: dict[str, Any]) -> None:
        try:
            handler = getattr(self, f"_tool_{msg['tool']}", None)
            result = await handler(msg.get("args") or {}) if handler else {
                "ok": False, "error": f"unknown tool: {msg['tool']}",
            }
        except Exception as exc:  # mirror agent.js: never raise across the wire
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        try:
            await self.ws.send(json.dumps({"type": "result", "id": msg["id"], "result": result}))
        except Exception:
            pass

    # -- tool handlers: same shapes as agent.js tools{...} -------------------------

    async def _tool_run_command(self, a: dict[str, Any]) -> dict[str, Any]:
        timeout = min(float(a.get("timeout_seconds") or 10), 600)
        env = {"HOME": str(self.sandbox), "PATH": "/usr/bin:/bin"}
        proc = await asyncio.create_subprocess_exec(
            "/bin/bash", "-c", a.get("command") or "",
            cwd=str(_sandboxed(self.sandbox, a.get("cwd"), self.extra_roots)),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            timed_out = False
        except TimeoutError:
            proc.kill()
            out, err = await proc.communicate()
            timed_out = True
        code = proc.returncode
        return {
            "ok": not timed_out and code == 0,
            "timed_out": timed_out,
            "exit_code": None if timed_out else code,
            "signal": None,
            "killed": timed_out,
            "stdout": out.decode(errors="replace"),
            "stderr": err.decode(errors="replace"),
            "shell_mode": "login+interactive",
        }

    async def _tool_inspect_machine(self, a: dict[str, Any]) -> dict[str, Any]:
        argv = list(a.get("argv") or [])
        command = a.get("command")
        if not argv and command:
            if any(c in command for c in ";|&<>()`$\n\r"):
                return {
                    "ok": False, "refused": True, "exit_code": None, "argv": None,
                    "error": "shell metacharacters are not interpreted here; quote them to pass them as text, or run the pipeline through run_command. Use run_command for anything outside the read-only inspection set.",
                }
            argv = command.split()
        if not argv or argv[0] not in STUB_INSPECT_ALLOWLIST:
            return {
                "ok": False, "refused": True, "exit_code": None, "argv": argv or None,
                "error": f"'{argv[0] if argv else ''}' is not in the read-only inspection set. Use run_command for anything outside the read-only inspection set.",
            }
        env = {"HOME": str(self.sandbox), "PATH": "/usr/bin:/bin"}
        proc = await asyncio.create_subprocess_exec(
            "/bin/bash", "-c", 'exec "$@"', "aiva-inspect", *argv,
            cwd=str(_sandboxed(self.sandbox, a.get("cwd"), self.extra_roots)),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=15)
        code = proc.returncode
        return {
            "ok": code == 0, "timed_out": False, "exit_code": code, "signal": None,
            "killed": False, "stdout": out.decode(errors="replace"),
            "stderr": err.decode(errors="replace"), "shell_mode": "login+interactive",
            "refused": False, "argv": argv,
        }

    async def _tool_read_file(self, a: dict[str, Any]) -> dict[str, Any]:
        p = _sandboxed(self.sandbox, a.get("path"), self.extra_roots)
        content = p.read_text(encoding="utf-8")
        if a.get("offset") or a.get("limit"):
            lines = content.split("\n")
            start = int(a.get("offset") or 0)
            content = "\n".join(lines[start : start + int(a["limit"])] if a.get("limit") else lines[start:])
        return {"ok": True, "path": str(p), "content": content}

    async def _tool_write_file(self, a: dict[str, Any]) -> dict[str, Any]:
        p = _sandboxed(self.sandbox, a.get("path"), self.extra_roots)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = a.get("content") or ""
        if (a.get("mode") or "rewrite") == "append":
            with p.open("a", encoding="utf-8") as fh:
                fh.write(data)
        else:
            p.write_text(data, encoding="utf-8")
        return {"ok": True, "path": str(p), "bytes": len(data.encode("utf-8"))}

    async def _tool_list_directory(self, a: dict[str, Any]) -> dict[str, Any]:
        root = _sandboxed(self.sandbox, a.get("path") or ".", self.extra_roots)
        depth = 2 if a.get("depth") is None else int(a["depth"])
        out: list[str] = []

        async def walk(directory: Path, d: int, prefix: str) -> None:
            try:
                entries = sorted(directory.iterdir(), key=lambda x: x.name)
            except OSError:
                return
            for e in entries:
                if e.name.startswith("."):
                    continue
                rel = f"{prefix}/{e.name}" if prefix else e.name
                is_dir = e.is_dir()
                out.append(rel + "/" if is_dir else rel)
                if is_dir and d > 1:
                    await walk(e, d - 1, rel)

        await walk(root, depth, "")
        return {"ok": True, "path": str(root), "count": len(out), "entries": out}

    async def _tool_list_skills(self, a: dict[str, Any]) -> dict[str, Any]:
        skills_dir = self.sandbox / "skills"
        skills = []
        if skills_dir.is_dir():
            for e in sorted(skills_dir.iterdir(), key=lambda x: x.name):
                md = e / "SKILL.md"
                if e.is_dir() and md.exists():
                    fm = _parse_frontmatter(md.read_text(encoding="utf-8"))
                    skills.append({"name": e.name, "description": fm.get("description", "")})
        if skills:
            return {"ok": True, "dir": str(skills_dir), "count": len(skills), "skills": skills}
        return {"ok": True, "skills": [], "note": "no skills directory found"}

    async def _tool_get_skill(self, a: dict[str, Any]) -> dict[str, Any]:
        name = a.get("skill_name") or ""
        md = self.sandbox / "skills" / name / "SKILL.md"
        if md.exists():
            return {"ok": True, "name": name, "dir": str(md.parent), "content": md.read_text(encoding="utf-8")}
        return {"ok": False, "error": f"skill '{name}' not found"}

    async def _tool_get_file(self, a: dict[str, Any]) -> dict[str, Any]:
        p = _sandboxed(self.sandbox, a.get("path"), self.extra_roots)
        data = p.read_bytes()
        return {"ok": True, "path": str(p), "bytes": len(data), "content_base64": base64.b64encode(data).decode()}

    async def _tool_send_file(self, a: dict[str, Any]) -> dict[str, Any]:
        p = _sandboxed(self.sandbox, a.get("path"), self.extra_roots)
        if p.exists() and not a.get("overwrite"):
            return {"ok": False, "error": "file exists; set overwrite:true to replace"}
        p.parent.mkdir(parents=True, exist_ok=True)
        data = base64.b64decode(a.get("content_base64") or "")
        p.write_bytes(data)
        return {"ok": True, "path": str(p), "bytes": len(data)}


def _parse_frontmatter(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if text.startswith("---\n"):
        for line in text.split("\n")[1:]:
            if line.strip() == "---":
                break
            if ":" in line:
                k, v = line.split(":", 1)
                out[k.strip()] = v.strip()
    return out


def make_sandbox(root: Path) -> Path:
    """A machine home: files, a nested tree, and one installable skill."""
    sandbox = root / "machine-home"
    sandbox.mkdir(parents=True, exist_ok=True)
    (sandbox / "notes.txt").write_text("line one\nline two\nline three\n", encoding="utf-8")
    (sandbox / "projects" / "alpha").mkdir(parents=True, exist_ok=True)
    (sandbox / "projects" / "alpha" / "a.txt").write_text("alpha file\n", encoding="utf-8")
    (sandbox / "projects" / "beta.md").write_text("beta\n", encoding="utf-8")
    skill = sandbox / "skills" / "start-here" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(
        "---\nname: start-here\ndescription: Establish identity, operating rules, and routing.\n---\nYou are on a test machine.\n",
        encoding="utf-8",
    )
    return sandbox


class LiveServer:
    """Boot src/server.py's real ASGI app on an ephemeral localhost port."""

    def __init__(self, tmp_root: Path) -> None:
        import server  # deferred: conftest must set env first

        self.server_module = server
        config = uvicorn.Config(
            server.build_app(), host="127.0.0.1", port=0,
            log_level="warning", lifespan="on", access_log=False,
        )
        self.uvicorn = uvicorn.Server(config)
        self._task: asyncio.Task[None] | None = None
        self.port: int | None = None
        self.tmp_root = tmp_root

    async def start(self) -> None:
        self._task = asyncio.create_task(self.uvicorn.serve())
        for _ in range(200):
            if self.uvicorn.started and self.uvicorn.servers:
                break
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError("uvicorn failed to start")
        self.port = self.uvicorn.servers[0].sockets[0].getsockname()[1]

    @property
    def url(self) -> str:
        if self.port is None:
            raise RuntimeError("server not started")
        return f"http://127.0.0.1:{self.port}/"

    def agent_uri(self, token: str) -> str:
        return f"ws://127.0.0.1:{self.port}/agent/socket?machine=oracle&token={token}"

    async def stop(self) -> None:
        self.uvicorn.should_exit = True
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except TimeoutError:
                self._task.cancel()


async def mcp_client(url: str, token: str, exit_stack: Any):
    """The official MCP SDK client over real HTTP with static bearer auth.

    Must be entered via an AsyncExitStack owned by the SAME task that exits it,
    or anyio's cancel scopes raise on teardown.
    """
    from contextlib import AsyncExitStack

    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    assert isinstance(exit_stack, AsyncExitStack)
    http_client = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    transport_ctx = streamable_http_client(url, http_client=http_client)
    streams = await exit_stack.enter_async_context(transport_ctx)
    read_stream, write_stream = streams[0], streams[1]  # SDK 2.0 yields a 2-tuple
    session = await exit_stack.enter_async_context(ClientSession(read_stream, write_stream))
    await session.initialize()
    return session


class Rig:
    """Everything a test needs: server + stub machine + MCP client + sandbox."""

    def __init__(self) -> None:
        import tempfile
        from contextlib import AsyncExitStack
        from pathlib import Path as P

        self.tmp = P(tempfile.mkdtemp(prefix="aiva-rig-"))
        self.sandbox = make_sandbox(P(self.tmp))
        self.transfer_root = self.sandbox / "chatgpt-transfer"
        self.transfer_root.mkdir(parents=True, exist_ok=True)
        self.server = LiveServer(P(self.tmp))
        self.server.server_module.CHATGPT_TRANSFER_ROOT = self.server.server_module.PurePosixPath(
            str(self.transfer_root)
        )
        self.agent: StubMachineAgent | None = None
        self.session = None
        self._stack = AsyncExitStack()

    async def __aenter__(self) -> Rig:
        await self.server.start()
        self.session = await mcp_client(self.server.url, os.environ["MCP_TOKEN"], self._stack)
        return self

    async def connect_machine(self) -> StubMachineAgent:
        assert self.agent is None
        self.agent = StubMachineAgent(
            self.server.agent_uri(os.environ["MCP_TOKEN"]),
            self.sandbox,
            extra_roots=(
                Path(os.environ["AIVA_CAO_BRIDGE_STATE_DIR"]),
                Path(os.environ["AIVA_CAO_DEV_BRIDGE_STATE_DIR"]),
                self.transfer_root,
            ),
        )
        await self.agent.start()
        # the hub registers the socket on accept; give the event loop a beat
        for _ in range(50):
            if self.server.server_module.AGENT_HUB.status()["machines"]["oracle"]["connected"]:
                break
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError("stub machine never connected")
        return self.agent

    async def disconnect_machine(self) -> None:
        if self.agent:
            await self.agent.stop()
            self.agent = None
            for _ in range(50):
                if not self.server.server_module.AGENT_HUB.status()["machines"]["oracle"]["connected"]:
                    break
                await asyncio.sleep(0.05)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None):
        """Call a tool over real MCP HTTP and return (parsed_content, is_error)."""
        result = await self.session.call_tool(name, arguments or {})
        if not result.content:
            parsed: Any = ""
        elif result.content[0].type == "text":
            text = result.content[0].text
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = text
        else:
            parsed = result.content[0].model_dump(mode="json", by_alias=True)
        return parsed, bool(result.is_error)

    async def __aexit__(self, *exc: object) -> None:
        await self._stack.aclose()
        if self.agent:
            await self.agent.stop()
        await self.server.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
