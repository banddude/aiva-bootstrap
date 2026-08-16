from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SENDBLUE_ACCEPTED = {"QUEUED", "SENT", "DELIVERED", "ACCEPTED", "PENDING"}
DEFAULT_AIVA_SPOOL = Path("/home/ubuntu/.aiva/state/cao-channel-bridge")
DEFAULT_DEV_SPOOL = Path("/home/ubuntu/.aiva/state/cao-channel-bridge-aiva-dev")
DEFAULT_CREDS = Path("/home/ubuntu/.aiva/state/sendblue-channel/creds.env")
DEFAULT_MIKE_NUMBER = "+13105969154"
DEFAULT_SENDBLUE_NUMBER = "+17862139361"
SENDBLUE_URL = "https://api.sendblue.co/api/send-message"


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("export "):
            line = line[7:].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _spool_root(target: str) -> Path:
    if target == "dev":
        return Path(os.environ.get("AIVA_CAO_DEV_BRIDGE_STATE_DIR", str(DEFAULT_DEV_SPOOL)))
    return Path(os.environ.get("AIVA_CAO_BRIDGE_STATE_DIR", str(DEFAULT_AIVA_SPOOL)))


def make_spool_record(target: str, message: str, source: str) -> tuple[Path, str, str]:
    root = _spool_root(target)
    inbox_target = "aiva-dev" if target == "dev" else "aiva"
    rendered = f"[AIVA inbound: aiva-inbox | from={source} | origin=notify]\n{message}"
    created = time.time_ns()
    body = {"source": "aiva-inbox", "message": rendered, "created_ns": created}
    stem = f"{created:020d}-notify-{os.getpid()}"
    final = root / "pending" / f"{stem}.json"
    return final, json.dumps(body, ensure_ascii=False), inbox_target


def _spool(target: str, message: str, source: str) -> dict[str, Any]:
    final, content, inbox_target = make_spool_record(target, message, source)
    pending = final.parent
    pending.mkdir(parents=True, exist_ok=True, mode=0o700)
    stem = final.stem
    fd, tmp_name = tempfile.mkstemp(prefix=f".{stem}.", suffix=".tmp", dir=pending)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, final)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return {"channel": target, "ok": True, "detail": f"queued for {inbox_target} on local CAO spool"}


def send_mike(message: str, source: str) -> dict[str, Any]:
    creds_path = Path(os.environ.get("AIVA_SENDBLUE_CREDS", str(DEFAULT_CREDS)))
    values = _read_env_file(creds_path)
    key = os.environ.get("SENDBLUE_API_KEY") or values.get("SENDBLUE_API_KEY") or values.get("SENDBLUE_API_API_KEY")
    secret = os.environ.get("SENDBLUE_API_SECRET") or values.get("SENDBLUE_API_SECRET") or values.get("SENDBLUE_API_API_SECRET")
    if not key or not secret:
        return {"channel": "mike", "ok": False, "error": "Sendblue credentials are unavailable"}
    content = f"[{source}] {message}" if source else message
    payload = json.dumps(
        {
            "number": os.environ.get("AIVA_NOTIFY_MIKE_NUMBER", DEFAULT_MIKE_NUMBER),
            "from_number": os.environ.get("AIVA_NOTIFY_SENDBLUE_NUMBER", DEFAULT_SENDBLUE_NUMBER),
            "content": content,
        }
    ).encode("utf-8")
    request = urllib.request.Request(SENDBLUE_URL, data=payload, method="POST")
    request.add_header("sb-api-key-id", key)
    request.add_header("sb-api-secret-key", secret)
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        return {"channel": "mike", "ok": False, "error": f"Sendblue HTTP {exc.code}"}
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return {"channel": "mike", "ok": False, "error": f"Sendblue send failed: {type(exc).__name__}"}
    status = str(data.get("status") or "").upper()
    if status not in SENDBLUE_ACCEPTED:
        return {"channel": "mike", "ok": False, "error": f"Sendblue did not accept the message (status {status or 'missing'})"}
    return {"channel": "mike", "ok": True, "detail": f"iMessage accepted by Sendblue for Mike (status {status})"}


def summarize(target: str, source: str, channels: list[dict[str, Any]]) -> dict[str, Any]:
    delivered = [str(channel["channel"]) for channel in channels if channel.get("ok")]
    failed = [
        {"channel": str(channel["channel"]), "error": str(channel.get("error") or "unknown error")}
        for channel in channels
        if not channel.get("ok")
    ]
    status = "delivered" if not failed else "failed" if not delivered else "partial"
    if status == "delivered":
        summary = f"Delivered to {' and '.join(delivered)}."
    elif status == "failed":
        summary = f"NOT delivered. Every requested channel failed: {', '.join(item['channel'] for item in failed)}."
    else:
        summary = (
            f"PARTIAL delivery. Delivered to {' and '.join(delivered)}, failed on "
            f"{', '.join(item['channel'] for item in failed)}. The failed channel was not rerouted anywhere, so treat it as unsent."
        )
    return {
        "ok": status == "delivered",
        "status": status,
        "target": target,
        "from": source,
        "delivered": delivered,
        "failed": failed,
        "channels": channels,
        "summary": summary,
    }


def dispatch_notify(target: str, message: str, source: str | None = None) -> dict[str, Any]:
    normalized = target.strip().lower()
    sender = (source or "mcp-agent").strip() or "mcp-agent"
    if normalized not in {"aiva", "dev", "mike", "all"}:
        return {"ok": False, "status": "failed", "error": "'target' is required and must be one of: aiva, dev, mike, all"}
    if not message.strip():
        return {"ok": False, "status": "failed", "error": "'message' is required and cannot be empty"}
    wanted = ["aiva", "mike"] if normalized == "all" else [normalized]
    channels: list[dict[str, Any]] = []
    for channel in wanted:
        try:
            channels.append(send_mike(message, sender) if channel == "mike" else _spool(channel, message, sender))
        except (OSError, ValueError) as exc:
            channels.append({"channel": channel, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    return summarize(normalized, sender, channels)
