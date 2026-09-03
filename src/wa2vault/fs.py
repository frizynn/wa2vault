"""Crash-safe local filesystem writes and vault-boundary checks."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


class UnsafeVaultError(RuntimeError):
    """Raised when generated private data would be written into a Git checkout."""


def ensure_private_dir(path: Path) -> None:
    """Create ``path`` and restrict it to the current user where supported."""
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    try:
        path.chmod(PRIVATE_DIR_MODE)
    except OSError:
        pass


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically replace a UTF-8 file after flushing its bytes to disk."""
    ensure_private_dir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.chmod(PRIVATE_FILE_MODE)
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def atomic_copy(source: Path, destination: Path) -> None:
    """Copy ``source`` to ``destination`` using a same-directory atomic replace."""
    ensure_private_dir(destination.parent)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary_path = Path(temporary)
    try:
        with source.open("rb") as source_handle, os.fdopen(fd, "wb") as target_handle:
            shutil.copyfileobj(source_handle, target_handle)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        temporary_path.chmod(PRIVATE_FILE_MODE)
        os.replace(temporary_path, destination)
        _fsync_directory(destination.parent)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def ensure_vault_is_safe(vault_dir: Path, *, allow_git_vault: bool = False) -> None:
    """Refuse private archive output anywhere inside a Git worktree by default."""
    if allow_git_vault:
        return
    candidate = vault_dir.expanduser().resolve(strict=False)
    for directory in (candidate, *candidate.parents):
        if (directory / ".git").exists():
            raise UnsafeVaultError(
                f"Refusing to write private chat data under Git checkout {directory}. "
                "Choose a vault outside Git or set allow_git_vault=true explicitly."
            )


def _fsync_directory(directory: Path) -> None:
    """Best-effort durability barrier for a completed rename."""
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


__all__ = [
    "UnsafeVaultError",
    "atomic_copy",
    "atomic_write_text",
    "ensure_private_dir",
    "ensure_vault_is_safe",
]
