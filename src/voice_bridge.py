from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

VOICE_DESCRIPTION = (
    "Call Mike through the OfficeAdmin iPhone app, speak one line, and wait for his spoken reply. "
    "Use only when Mike requests a call or authorizes a voice conversation; capability questions do not authorize ringing. "
    "The invoking agent remains the conversation brain. Returns transcript and interrupted when finalized. "
    "Keep sessionId stable; use a fresh turnId per new line. On timedOut or a transport error, "
    "resume with the SAME sessionId and turnId so the phone does not speak twice. "
    "Hangup ends this conversation only, not other work. end=true ends the call after the turn. "
    "No machine argument is needed: Oracle bridges directly to OfficeAdmin."
)

VOICE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sessionId": {"type": "string", "minLength": 1, "maxLength": 128, "description": "Stable identity of this agent conversation."},
        "turnId": {"type": "string", "minLength": 1, "maxLength": 128, "description": "Fresh turn identity; reuse the same value to resume a wait."},
        "text": {"type": "string", "minLength": 1, "maxLength": 8000, "description": "Plain text to speak aloud."},
        "purpose": {"type": "string", "minLength": 1, "maxLength": 400, "description": "Visible purpose on the initial call screen."},
        "waitSeconds": {"type": "integer", "minimum": 1, "maximum": 25, "description": "Bounded wait, default 20 seconds."},
        "end": {"type": "boolean", "description": "End the call after this turn, without cancelling other work."},
    },
    "required": ["sessionId", "turnId", "text"],
}


async def speak_and_wait(payload: dict[str, Any]) -> dict[str, Any]:
    key_file = Path(os.environ.get(
        "AIVA_OFFICEADMIN_KEY_FILE",
        "/home/ubuntu/.aiva/state/officeadmin/oracle-direct-key",
    ))
    try:
        key = key_file.read_text(encoding="utf-8").strip()
    except OSError:
        return {"ok": False, "error": "OfficeAdmin voice bridge credential is unavailable"}
    if not key:
        return {"ok": False, "error": "OfficeAdmin voice bridge credential is unavailable"}
    base = os.environ.get("AIVA_OFFICEADMIN_LOCAL_URL", "http://127.0.0.1:3200/api/v1").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=35, follow_redirects=False) as client:
            response = await client.post(
                base + "/aiva/voice-conversation/speak-and-wait",
                headers={"Authorization": "Bearer " + key},
                json=payload,
            )
    except httpx.HTTPError:
        return {
            "ok": False,
            "error": "OfficeAdmin voice transport failed; the turn may still be live",
            "hint": "Resume with the SAME sessionId and turnId; do not create a replacement turn.",
        }
    if response.status_code != 200:
        return {"ok": False, "error": "OfficeAdmin voice request failed", "status": response.status_code}
    try:
        data = response.json()
    except ValueError:
        return {"ok": False, "error": "OfficeAdmin returned an invalid voice result"}
    if not isinstance(data, dict):
        return {"ok": False, "error": "OfficeAdmin returned an invalid voice result"}
    return data
