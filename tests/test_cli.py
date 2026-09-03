"""Unit tests for the CLI's display helpers.

These exercise the pure name/table rendering helpers directly (mirroring how
``test_wacli.py`` tests the parsing helpers), so they never spawn the wacli
binary or require a paired session. The focus is group-name resolution: groups
whose ``chats list`` name is missing or merely echoes the JID -- a common result
of WhatsApp's app-state sync failing after pairing -- must be backfilled from the
group table, falling back to a clear ``(unnamed group)`` marker.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from wa2vault import cli
from wa2vault.cli import _display_name, _print_chats_table
from wa2vault.config import Config
from wa2vault.contacts import ContactBook, pretty_phone
from wa2vault.wacli import WacliTimeoutError


def _book(tmp_path: Path) -> ContactBook:
    return ContactBook(tmp_path / "contacts.json")


# --------------------------------------------------------------------------- #
# _display_name
# --------------------------------------------------------------------------- #
def test_display_name_group_with_real_name(tmp_path: Path) -> None:
    assert _display_name({}, "1@g.us", "Mi Grupo", _book(tmp_path), {}) == "Mi Grupo"


def test_display_name_group_backfilled_from_map(tmp_path: Path) -> None:
    # chats list echoed the JID as the name; the group map carries the real one.
    names = {"1@g.us": "Backfilled Group"}
    assert _display_name({}, "1@g.us", "1@g.us", _book(tmp_path), names) == "Backfilled Group"


def test_display_name_group_subject_overrides_participant_name(tmp_path: Path) -> None:
    # chats list named the group after a participant; the group subject wins.
    names = {"1@g.us": "Proyecto Alfa"}
    assert _display_name({}, "1@g.us", "Pat Lee", _book(tmp_path), names) == "Proyecto Alfa"


def test_display_name_group_unnamed_fallback(tmp_path: Path) -> None:
    assert _display_name({}, "1@g.us", "1@g.us", _book(tmp_path), {}) == "(unnamed group)"


def test_display_name_group_empty_name_unnamed(tmp_path: Path) -> None:
    assert _display_name({}, "1@g.us", "", _book(tmp_path), {}) == "(unnamed group)"


def test_display_name_dm_placeholder_falls_back_to_phone(tmp_path: Path) -> None:
    jid = "15550100001@s.whatsapp.net"
    assert _display_name({}, jid, jid, _book(tmp_path), {}) == pretty_phone(jid)


def test_display_name_dm_uses_saved_contact(tmp_path: Path) -> None:
    book = _book(tmp_path)
    jid = "15550100001@s.whatsapp.net"
    book.set(jid, "Alice")
    assert _display_name({}, jid, jid, book, {}) == "Alice"


def test_display_name_dm_real_name_preserved(tmp_path: Path) -> None:
    jid = "15550100001@s.whatsapp.net"
    assert _display_name({}, jid, "Alice", _book(tmp_path), {}) == "Alice"


def test_display_name_channel_keeps_name(tmp_path: Path) -> None:
    assert _display_name({}, "777@newsletter", "Noticias", _book(tmp_path), {}) == "Noticias"


# --------------------------------------------------------------------------- #
# _print_chats_table
# --------------------------------------------------------------------------- #
def test_print_chats_table_renders_backfilled_and_real_names(tmp_path: Path, capsys) -> None:
    rows = [
        {"jid": "1@g.us", "name": "1@g.us"},  # placeholder -> backfilled
        {"jid": "2@g.us", "name": "Real Group"},  # real name kept
    ]
    _print_chats_table(rows, _book(tmp_path), {"1@g.us": "Backfilled Group"})
    out = capsys.readouterr().out
    assert "Backfilled Group" in out
    assert "Real Group" in out


def test_global_profile_option_selects_isolated_config(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('[profiles.work]\nwacli_account = "example-account"\n', encoding="utf-8")
    captured: dict[str, Config] = {}

    class FakeClient:
        def __init__(self, config: Config) -> None:
            captured["config"] = config

        def list_chats(self, limit: int = 100) -> list[dict]:
            return []

        def group_names(self) -> dict[str, str]:
            return {}

    monkeypatch.setattr(cli, "WacliClient", FakeClient)

    result = CliRunner().invoke(
        cli.app,
        ["--config", str(config_path), "--profile", "work", "chats"],
    )

    assert result.exit_code == 0, result.output
    assert captured["config"].profile == "work"


def test_sync_timeout_preserves_cli_exit_code_124(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('[profiles.work]\nwacli_account = "example-account"\n')

    class FakeClient:
        def __init__(self, config: Config) -> None:
            self.config = config

        def run_passthrough(self, *args, **kwargs) -> int:
            raise WacliTimeoutError("sync --once", 9)

    monkeypatch.setattr(cli, "WacliClient", FakeClient)
    monkeypatch.setattr(cli, "find_store_lock", lambda _config: None)

    result = CliRunner().invoke(
        cli.app,
        ["--config", str(config_path), "--profile", "work", "sync"],
    )

    assert result.exit_code == 124
    assert "timed out after 9s" in result.output
