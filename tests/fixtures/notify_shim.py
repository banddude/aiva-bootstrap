#!/usr/bin/env python3
"""Test double for /usr/local/bin/notify (the real CLI lives on hosts, not in CI).

It implements the SAME local-spool contract the real CLI implements for the
aiva / aiva-dev targets: one durable JSON record under
$AIVA_CAO_BRIDGE_STATE_DIR (or the dev dir) /pending/. The `mike` target never
sends anything real: dry-run output only, or an honest failure.

AIVA_NOTIFY_SHIM_MODE=fail makes it exit 3 with a stderr message, to prove the
server reports delivery failures instead of claiming success.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path


def main(argv: list[str]) -> int:
    if os.environ.get("AIVA_NOTIFY_SHIM_MODE") == "fail":
        print("notify-shim: simulated delivery failure", file=sys.stderr)
        return 3

    if len(argv) < 2:
        print("usage: notify_shim <target> <message> --from <source>", file=sys.stderr)
        return 1
    target, message = argv[0], argv[1]
    source = "mcp-agent"
    if "--from" in argv:
        i = argv.index("--from")
        if i + 1 < len(argv):
            source = argv[i + 1]

    if target == "mike":
        print("[notify-shim DRY-RUN] would text Mike; nothing sent")
        return 0

    if target in ("aiva", "all", "aiva-dev", "dev"):
        dev = target in ("aiva-dev", "dev")
        root = Path(
            os.environ.get("AIVA_CAO_DEV_BRIDGE_STATE_DIR" if dev else "AIVA_CAO_BRIDGE_STATE_DIR", "/tmp/notify-shim")
        )
        pending = root / "pending"
        pending.mkdir(parents=True, exist_ok=True, mode=0o700)
        rendered = f"[AIVA inbound: aiva-inbox | from={source} | origin=notify]\n{message}"
        created = time.time_ns()
        body = {"source": "aiva-inbox", "message": rendered, "created_ns": created}
        stem = f"{created:020d}-notify-{os.getpid()}"
        fd, tmp_name = tempfile.mkstemp(prefix=f".{stem}.", suffix=".tmp", dir=pending)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(body, fh, ensure_ascii=False)
        final = pending / f"{stem}.json"
        os.replace(tmp_name, final)
        print(str(final))
        if target == "all":
            print("[notify-shim DRY-RUN] would also text Mike; nothing sent")
        return 0

    print(f"notify-shim: unknown target {target}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
