"""Unit tests for the single-writer guard (:mod:`wa2vault.lock`).

These cover store-directory resolution, tolerant ``LOCK`` file parsing, and the
:func:`~wa2vault.lock.find_store_lock` decision (no file / stale / live holder)
without needing a real wacli or a live WhatsApp link.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from wa2vault import lock
from wa2vault.config import Config


def _config_with_store(tmp_path: Path) -> Config:
    """Return a Config whose wacli store dir points inside ``tmp_path``."""
    store = tmp_path / "store"
    store.mkdir()
    return Config(wacli_db=store)


def test_store_dir_prefers_configured_path(tmp_path: Path) -> None:
    config = _config_with_store(tmp_path)
    assert lock.store_dir(config) == tmp_path / "store"


def test_store_dir_honors_env_when_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WACLI_STORE_DIR", str(tmp_path / "envstore"))
    config = Config(wacli_db=None)
    assert lock.store_dir(config) == tmp_path / "envstore"


def test_parse_lock_file_extracts_pid_and_timestamp() -> None:
    pid, acquired_at = lock._parse_lock_file(
        "pid=12345\nacquired_at=2026-06-09T12:50:12.388-03:00\n"
    )
    assert pid == 12345
    assert acquired_at == "2026-06-09T12:50:12.388-03:00"


def test_parse_lock_file_tolerates_junk_and_bad_pid() -> None:
    pid, acquired_at = lock._parse_lock_file("garbage\npid=not-a-number\n")
    assert pid is None
    assert acquired_at is None


def test_find_store_lock_returns_none_without_lock_file(tmp_path: Path) -> None:
    config = _config_with_store(tmp_path)
    assert lock.find_store_lock(config) is None


def test_find_store_lock_returns_none_for_stale_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_with_store(tmp_path)
    (tmp_path / "store" / lock.LOCK_FILENAME).write_text(
        "pid=4242\nacquired_at=2026-06-09T12:50:12-03:00\n", encoding="utf-8"
    )
    monkeypatch.setattr(lock, "_pid_alive", lambda pid: False)
    assert lock.find_store_lock(config) is None


def test_find_store_lock_returns_holder_for_live_foreign_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_with_store(tmp_path)
    (tmp_path / "store" / lock.LOCK_FILENAME).write_text(
        "pid=4242\nacquired_at=2026-06-09T12:50:12-03:00\n", encoding="utf-8"
    )
    monkeypatch.setattr(lock, "_pid_alive", lambda pid: True)

    held = lock.find_store_lock(config)
    assert held is not None
    assert held.pid == 4242
    assert held.acquired_at == "2026-06-09T12:50:12-03:00"
    assert "pid 4242" in held.describe()


def test_find_store_lock_ignores_our_own_pid(tmp_path: Path) -> None:
    """A LOCK file naming this process is not treated as a foreign holder."""
    config = _config_with_store(tmp_path)
    (tmp_path / "store" / lock.LOCK_FILENAME).write_text(
        f"pid={os.getpid()}\n", encoding="utf-8"
    )
    assert lock.find_store_lock(config) is None
