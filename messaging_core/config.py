"""Filesystem locations for the messaging core, and the native-filesystem guard.

Everything the messaging core persists lives under one data directory,
overridable with `$MESSAGING_MCP_HOME` for tests and alternate deployments.
The schema file, by contrast, is part of the repository and is located
relative to this module rather than the data directory.

`assert_native_filesystem` exists because SQLite's WAL mode assumes real
POSIX file locking. Network and translation filesystems (9p, drvfs -- the
Windows-drive passthrough WSL2 uses for /mnt/c, cifs, nfs, fuseblk, vboxsf)
either emulate locking badly or not at all, and a WAL database written there
can silently corrupt. `db_path()` calls this guard before handing back a
path, so a caller can't accidentally point the database at a mount that will
eat it.
"""

from __future__ import annotations

import os
from pathlib import Path

_ENV_VAR = "MESSAGING_MCP_HOME"

# Filesystem types known to mishandle the file locking SQLite's WAL mode
# depends on. 9p and drvfs are how WSL2 exposes non-native drives (drvfs for
# /mnt/c and friends, 9p for other passthrough mounts); cifs and nfs are
# network filesystems; fuseblk covers common FUSE-mounted block devices
# (e.g. ntfs-3g); vboxsf is VirtualBox's shared-folder filesystem.
_UNSAFE_FSTYPES = frozenset({"9p", "drvfs", "cifs", "nfs", "fuseblk", "vboxsf"})


def data_dir() -> Path:
    """Return the messaging core's data directory, creating it if needed.

    Controlled by $MESSAGING_MCP_HOME; defaults to ~/.messaging-mcp.
    """
    override = os.environ.get(_ENV_VAR)
    path = Path(override).expanduser() if override else Path.home() / ".messaging-mcp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    """Return the path to the SQLite database file, guarded against unsafe mounts."""
    path = data_dir() / "messaging.sqlite3"
    assert_native_filesystem(path)
    return path


def schema_path() -> Path:
    """Return the path to the repository's schema/schema.sql, relative to this file."""
    return Path(__file__).resolve().parent.parent / "schema" / "schema.sql"


def _find_mount(resolved: Path, mounts: list[tuple[str, str]]) -> tuple[str, str] | None:
    """Return the (mount_point, fstype) entry whose mount point is the longest
    prefix of `resolved`, or None if no entry matches."""
    best: tuple[str, str] | None = None
    best_len = -1
    resolved_str = str(resolved)
    for mount_point, fstype in mounts:
        if mount_point == "/":
            candidate = True
        else:
            candidate = resolved_str == mount_point or resolved_str.startswith(mount_point + "/")
        if candidate and len(mount_point) > best_len:
            best = (mount_point, fstype)
            best_len = len(mount_point)
    return best


def _read_mounts(proc_mounts_text: str) -> list[tuple[str, str]]:
    mounts: list[tuple[str, str]] = []
    for line in proc_mounts_text.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        _device, mount_point, fstype = fields[0], fields[1], fields[2]
        # /proc/mounts encodes spaces and other special characters as octal
        # escapes (e.g. "\040" for a space); undo that for the comparison.
        mount_point = _unescape_octal(mount_point)
        mounts.append((mount_point, fstype))
    return mounts


def _unescape_octal(s: str) -> str:
    out = []
    i = 0
    while i < len(s):
        if s[i] == "\\" and i + 3 < len(s) and s[i + 1 : i + 4].isdigit():
            out.append(chr(int(s[i + 1 : i + 4], 8)))
            i += 4
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def assert_native_filesystem(path: Path, *, mounts_text: str | None = None) -> None:
    """Raise RuntimeError if `path` resolves onto a known-unsafe filesystem type.

    Detection reads /proc/mounts (or `mounts_text`, for tests) and finds the
    longest mount point that is a prefix of the resolved path. If the mounts
    table can't be read at all, this does not raise -- an absence of proof is
    not proof of a problem.
    """
    if mounts_text is None:
        try:
            mounts_text = Path("/proc/mounts").read_text()
        except OSError:
            return

    mounts = _read_mounts(mounts_text)
    if not mounts:
        return

    # resolve(strict=False) -- the default since Python 3.6 -- makes the
    # path absolute and resolves any symlinks in the existing prefix without
    # requiring the target itself to exist, which is exactly what's needed
    # for a database file that may not have been created yet.
    resolved = path.resolve()

    match = _find_mount(resolved, mounts)
    if match is None:
        return

    mount_point, fstype = match
    if fstype in _UNSAFE_FSTYPES:
        raise RuntimeError(
            f"refusing to use {path} for the SQLite database: it resolves onto "
            f"{mount_point!r}, a {fstype!r} filesystem. WAL-mode SQLite requires "
            f"real POSIX file locking, which {fstype!r} mounts do not provide "
            f"reliably -- the database can silently corrupt. Point "
            f"MESSAGING_MCP_HOME at a native filesystem path instead."
        )
