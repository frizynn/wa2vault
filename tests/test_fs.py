"""Filesystem safety and crash-consistency tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from wa2vault import fs
from wa2vault.fs import UnsafeVaultError, atomic_copy, atomic_write_text


def test_vault_inside_git_checkout_is_refused_by_default(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)

    with pytest.raises(UnsafeVaultError, match="Git checkout"):
        fs.ensure_vault_is_safe(checkout / "private-vault")


def test_git_vault_requires_explicit_override(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)

    fs.ensure_vault_is_safe(checkout / "private-vault", allow_git_vault=True)


def test_atomic_write_failure_preserves_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "archive.md"
    target.write_text("complete old archive", encoding="utf-8")

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("simulated rename failure")

    monkeypatch.setattr(fs.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated rename failure"):
        atomic_write_text(target, "partial new archive")

    assert target.read_text(encoding="utf-8") == "complete old archive"
    assert list(tmp_path.glob(".archive.md.*")) == []


def test_atomic_copy_failure_preserves_previous_attachment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.bin"
    target = tmp_path / "attachment.bin"
    source.write_bytes(b"new")
    target.write_bytes(b"old")

    def fail_replace(_source_path: Path, _destination_path: Path) -> None:
        raise OSError("simulated rename failure")

    monkeypatch.setattr(fs.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated rename failure"):
        atomic_copy(source, target)

    assert target.read_bytes() == b"old"
    assert list(tmp_path.glob(".attachment.bin.*")) == []
