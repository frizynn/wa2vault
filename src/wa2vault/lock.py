"""Detect whether another process already holds wacli's store lock.

``wacli`` (the WhatsApp data layer wa2vault shells out to) is a *single
writer*: while it syncs or pairs it takes an exclusive lock on its store
directory and records the holder in a ``LOCK`` file::

    pid=12345
    acquired_at=2026-06-09T12:50:12.388832732-03:00

A second writer that tries to start gets a hard "store is locked" failure from
wacli. That failure is correct (it prevents store corruption) but it surfaces
late and cryptically -- and, in ``pull``, only after wasting up to
``sync_timeout`` seconds waiting for a lock it will never get.

This module lets wa2vault detect a running instance *before* spawning a second
wacli, so concurrent runs (for example two agents invoking the CLI at once) can
back off with a clear message instead of fighting over the lock. It is
best-effort: if the store directory or ``LOCK`` file cannot be located or
parsed, :func:`find_store_lock` returns ``None`` and wacli's own exclusive lock
remains the correctness backstop.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from wa2vault.config import Config

#: Name of the lock file wacli writes inside its store directory.
LOCK_FILENAME = "LOCK"


@dataclass(frozen=True)
class StoreLock:
    """A live, foreign holder of wacli's store lock.

    Attributes:
        pid: PID of the process currently holding the store lock.
        acquired_at: When the lock was taken (verbatim from the ``LOCK`` file),
            or None if the file did not record it.
        lock_file: Path to the ``LOCK`` file that was read.
    """

    pid: int
    acquired_at: str | None
    lock_file: Path

    def describe(self) -> str:
        """Return a short human description, e.g. ``"pid 12345, since <ts>"``."""
        since = f", since {self.acquired_at}" if self.acquired_at else ""
        return f"pid {self.pid}{since}"


def store_dir(config: Config) -> Path:
    """Best-effort resolution of wacli's store directory.

    Resolution order mirrors how wacli itself locates the store:

    1. An explicit ``config.wacli_db`` (threaded to wacli as ``--store``).
    2. The ``WACLI_STORE_DIR`` environment variable.
    3. The platform state directory (``~/.local/state/wacli`` on Linux), which
       is wacli's documented default.
    """
    if config.wacli_db is not None:
        return Path(config.wacli_db)
    base = _default_wacli_base_dir()
    if config.wacli_account is not None:
        # This is wacli's default for named accounts. If the account registry
        # points elsewhere, wacli's own exclusive lock remains the backstop.
        return base / "accounts" / config.wacli_account
    env = os.environ.get("WACLI_STORE_DIR")
    if env:
        return Path(env).expanduser()
    return base


def _default_wacli_base_dir() -> Path:
    """Mirror wacli's defaults (XDG state on Linux, ``~/.wacli`` elsewhere)."""
    if sys.platform.startswith("linux"):
        xdg_state = os.environ.get("XDG_STATE_HOME")
        if xdg_state and Path(xdg_state).is_absolute():
            return Path(xdg_state) / "wacli"
        return Path.home() / ".local" / "state" / "wacli"
    return Path.home() / ".wacli"


def _pid_alive(pid: int) -> bool:
    """Return True if ``pid`` is a live process (POSIX best-effort).

    Uses the standard "signal 0" liveness probe: it sends no signal but still
    performs the permission/existence checks. A ``ProcessLookupError`` means the
    process is gone (stale lock); a ``PermissionError`` means it exists but is
    owned by another user, which still counts as alive.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _parse_lock_file(text: str) -> tuple[int | None, str | None]:
    """Parse ``pid`` and ``acquired_at`` out of a ``LOCK`` file's contents.

    The file is a few ``key=value`` lines. Parsing is tolerant: unknown lines
    and a malformed ``pid`` are ignored (the latter yields ``pid=None``).
    """
    pid: int | None = None
    acquired_at: str | None = None
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip().lower()
        value = value.strip()
        if key == "pid":
            try:
                pid = int(value)
            except ValueError:
                pid = None
        elif key == "acquired_at":
            acquired_at = value or None
    return pid, acquired_at


def find_store_lock(config: Config) -> StoreLock | None:
    """Return the live, foreign holder of wacli's store lock, or ``None``.

    ``None`` means no other writer is running: there is no ``LOCK`` file, it is
    unparsable, it names this very process, or it is stale (the recorded PID is
    gone). This is best-effort -- any I/O error degrades to ``None`` so the
    caller proceeds and relies on wacli's own exclusive lock for correctness.
    """
    lock_file = store_dir(config) / LOCK_FILENAME
    try:
        text = lock_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        # Missing file, missing store dir, permission error, etc.
        return None

    pid, acquired_at = _parse_lock_file(text)
    if pid is None or pid == os.getpid():
        return None
    if not _pid_alive(pid):
        return None
    return StoreLock(pid=pid, acquired_at=acquired_at, lock_file=lock_file)


__all__ = ["StoreLock", "find_store_lock", "store_dir", "LOCK_FILENAME"]
