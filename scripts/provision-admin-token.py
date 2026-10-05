#!/usr/bin/env python3
"""Provision the static admin token used to approve OAuth clients.

The token is intentionally stored outside the repository and is never printed.
Fresh installs create it atomically with mode 0600. Reruns preserve any existing
non-empty token instead of rotating credentials behind connected clients.
"""

from __future__ import annotations

import argparse
import os
import pwd
import secrets
import stat
import tempfile
from pathlib import Path


def _owner_ids(owner: str | None) -> tuple[int, int] | None:
    if owner is None:
        return None
    entry = pwd.getpwnam(owner)
    return entry.pw_uid, entry.pw_gid


def _secure_existing(path: Path, owner_ids: tuple[int, int] | None) -> None:
    if path.is_symlink():
        raise RuntimeError(f"refusing symlink token path: {path}")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f"refusing non-file token path: {path}")
    os.chmod(path, 0o600)
    if owner_ids is not None and os.geteuid() == 0:
        os.chown(path, *owner_ids)


def provision_admin_token(
    home: Path,
    *,
    owner: str | None = None,
    token_path: Path | None = None,
) -> Path:
    home = home.resolve()
    home.mkdir(parents=True, exist_ok=True)
    path = token_path or (home / "admin-token")
    if not path.is_absolute():
        path = home / path

    owner_ids = _owner_ids(owner)

    if path.exists() or path.is_symlink():
        _secure_existing(path, owner_ids)
        if path.read_text(encoding="utf-8").strip():
            return path

    # Create a private temporary file first, set ownership while it is still
    # empty, then write and atomically replace the final path. This avoids ever
    # creating the secret with permissive mode bits and avoids following a
    # final-path symlink during the write.
    fd, tmp_name = tempfile.mkstemp(prefix=".admin-token.", dir=home)
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        if owner_ids is not None and os.geteuid() == 0:
            os.fchown(fd, *owner_ids)
        token = secrets.token_hex(32)
        os.write(fd, (token + "\n").encode("ascii"))
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(tmp_path, path)
        _secure_existing(path, owner_ids)
        return path
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--owner")
    parser.add_argument("--token-file", type=Path)
    args = parser.parse_args()
    provision_admin_token(args.home, owner=args.owner, token_path=args.token_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
