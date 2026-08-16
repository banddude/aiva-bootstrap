"""Test-environment bootstrap. Runs before ANY src import.

Two laws here, both born from the 2026-08-15 outage:

1. AIVA_MCP_URL points at a DEAD address (http://127.0.0.1:9, discard port,
   connection refused instantly). Nothing in this suite may reach Cloudflare.
   If a code path silently falls back to the "MCP URL", it dies loudly here
   instead of passing because Cloudflare happened to be healthy. A test that
   only passes while the Worker is up proves nothing.
2. The server under test runs IN THIS PROCESS on an ephemeral port with its
   state under a tmp dir. It never touches /opt/aiva, ~/.aiva, port 8765, or
   any real machine. The only substituted component is the machine itself:
   a stub agent (tests/rig.py) speaking the real agent wire protocol over a
   real WebSocket, actually executing the tools against a sandbox directory.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

DEAD_WORKER_URL = "http://127.0.0.1:9"

# Session tmp root, created before src.server could ever be imported.
_SESSION_TMP = Path(tempfile.mkdtemp(prefix="aiva-bootstrap-test-"))

# --- environment, set once, before any src import ---------------------------------

os.environ["AIVA_MCP_URL"] = DEAD_WORKER_URL  # the law (see module docstring)
os.environ["MCP_TOKEN"] = "aiva-ci-test-token"  # test-only value, not a secret
os.environ["AIVA_HOME"] = str(_SESSION_TMP / "home")
os.environ["AIVA_STATE"] = str(_SESSION_TMP / "state")
os.environ["AIVA_OAUTH_DB"] = str(_SESSION_TMP / "oauth.sqlite3")
os.environ["AIVA_OAUTH_IMPORT"] = str(_SESSION_TMP / "no-import.json")
os.environ["AIVA_PUBLIC_BASE"] = "http://127.0.0.1:9"
os.environ["AIVA_HOST"] = "127.0.0.1"
os.environ["AIVA_CAO_BRIDGE_STATE_DIR"] = str(_SESSION_TMP / "cao-spool")
os.environ["AIVA_CAO_DEV_BRIDGE_STATE_DIR"] = str(_SESSION_TMP / "cao-dev-spool")
# main branch shells out to /usr/local/bin/notify; point that seam (added in
# this repo, default unchanged) at a faithful local-spool shim so notify is
# testable on a fresh CI runner. PR #5's in-process notify ignores this.
os.environ["AIVA_NOTIFY_BIN"] = str(REPO_ROOT / "tests" / "fixtures" / "notify_shim.py")

# Safety: even if some future code path reached the real Sendblue API, there
# must be no credentials for it to find in this process.
os.environ.pop("SENDBLUE_API_KEY", None)
os.environ.pop("SENDBLUE_API_SECRET", None)
os.environ["AIVA_SENDBLUE_CREDS"] = str(_SESSION_TMP / "sendblue-creds-absent.env")

sys.path.insert(0, str(REPO_ROOT / "src"))

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def session_tmp() -> Path:
    return _SESSION_TMP


@pytest.fixture(scope="session")
def cao_spool_dir() -> Path:
    return Path(os.environ["AIVA_CAO_BRIDGE_STATE_DIR"])


@pytest.fixture(scope="session")
def token() -> str:
    return os.environ["MCP_TOKEN"]
