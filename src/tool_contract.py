from __future__ import annotations

from copy import deepcopy
from typing import Any

MACHINES = ["laptop", "oracle", "mac-server"]
MACHINE_PROP = {
    "type": "string",
    "enum": MACHINES,
    "description": "Which machine to run on: laptop, oracle, or mac-server.",
}


def _obj(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


WORKER_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "run_command": _obj(
        {
            "machine": MACHINE_PROP,
            "command": {"type": "string"},
            "timeout_seconds": {
                "type": "number",
                "description": "Seconds to wait. Default 10, max 25 for a synchronous call. For longer commands use run_command_async.",
            },
        },
        ["machine", "command"],
    ),
    "inspect_machine": _obj(
        {
            "machine": MACHINE_PROP,
            "command": {
                "type": "string",
                "description": "The command as one string, e.g. 'git status --short'. Quotes group and a backslash escapes, but nothing is expanded or substituted.",
            },
            "argv": {
                "type": "array",
                "items": {"type": "string"},
                "description": 'The same command pre-split into tokens, e.g. ["grep", "-n", "two words", "file.txt"]. Use this instead of command when an argument contains spaces or characters that the string form refuses.',
            },
            "cwd": {"type": "string", "description": "Directory to run in. Defaults to the home directory."},
            "timeout_seconds": {"type": "number", "description": "Seconds to wait. Default 10, max 25."},
        },
        ["machine"],
    ),
    "run_command_async": _obj(
        {
            "machine": MACHINE_PROP,
            "command": {"type": "string"},
            "timeout_seconds": {"type": "number", "description": "Max seconds the command may run before it's killed. Default 10, max 600."},
        },
        ["machine", "command"],
    ),
    "job_result": _obj({"job_id": {"type": "string"}}, ["job_id"]),
    "read_file": _obj(
        {"machine": MACHINE_PROP, "path": {"type": "string"}, "offset": {"type": "number"}, "limit": {"type": "number"}},
        ["machine", "path"],
    ),
    "write_file": _obj(
        {
            "machine": MACHINE_PROP,
            "path": {"type": "string"},
            "content": {"type": "string"},
            "mode": {"type": "string", "enum": ["rewrite", "append"]},
        },
        ["machine", "path", "content"],
    ),
    "list_directory": _obj(
        {
            "machine": MACHINE_PROP,
            "path": {"type": "string", "description": "Defaults to the home directory."},
            "depth": {"type": "number", "description": "Default 2."},
        },
        ["machine"],
    ),
    "list_skills": _obj({"machine": MACHINE_PROP}, ["machine"]),
    "get_skill": _obj({"machine": MACHINE_PROP, "skill_name": {"type": "string"}}, ["machine", "skill_name"]),
    "get_file": _obj({"machine": MACHINE_PROP, "path": {"type": "string"}}, ["machine", "path"]),
    "send_file": _obj(
        {
            "machine": MACHINE_PROP,
            "path": {"type": "string"},
            "content_base64": {"type": "string"},
            "overwrite": {"type": "boolean"},
        },
        ["machine", "path", "content_base64"],
    ),
    "notify": _obj(
        {
            "target": {
                "type": "string",
                "enum": ["aiva", "mike", "all"],
                "description": "Who to notify. 'aiva' is the AIVA session inbox and is the right choice for anything routine. 'mike' texts a real person's phone, so reserve it for something he needs to see right now. 'all' sends to both.",
            },
            "message": {
                "type": "string",
                "description": "The notification text. Write it so it stands alone, because the reader has none of your context.",
            },
            "from": {
                "type": "string",
                "description": "Optional source label shown on the notification, for example the agent or job name. Defaults to mcp-agent.",
            },
        },
        ["target", "message"],
    ),
}


def worker_tool_schemas() -> dict[str, dict[str, Any]]:
    return deepcopy(WORKER_TOOL_SCHEMAS)
