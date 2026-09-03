"""Smoke tests for the wa2vault shared contracts.

These tests pin the shape of the shared contracts (models, config, transcriber
factory) that the rest of wa2vault codes against, and assert which backends are
implemented versus intentionally deferred (the Nemotron backend).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from wa2vault.config import Config
from wa2vault.models import MessageRecord
from wa2vault.transcribe import get_transcriber
from wa2vault.transcribe.base import Transcriber, TranscriptResult
from wa2vault.transcribe.faster_whisper_backend import FasterWhisperTranscriber


def test_message_record_minimal() -> None:
    record = MessageRecord(
        id="ABC123",
        chat_jid="123@s.whatsapp.net",
        chat_type="dm",
        timestamp=datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
        from_me=False,
        kind="text",
        text="hola",
    )
    assert record.transcript is None
    assert record.media_path is None
    assert record.raw == {}


def test_config_roundtrip(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    created = Config.load(cfg_path)
    assert cfg_path.exists()

    reloaded = Config.load(cfg_path)
    assert reloaded.model_dump() == created.model_dump()
    # Empty wacli_db in the TOML must round-trip to None, not Path("").
    assert reloaded.wacli_db is None


def test_config_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_path = tmp_path / "config.toml"
    monkeypatch.setenv("WA2VAULT_LANGUAGE", "en")
    monkeypatch.setenv("WA2VAULT_DEFAULT_DAYS", "7")
    config = Config.load(cfg_path)
    assert config.language == "en"
    assert config.default_days == 7


def test_config_defaults_remain_backwards_compatible() -> None:
    config = Config()

    assert config.profile == "default"
    assert config.profile_key.startswith("default--")
    assert config.wacli_account is None
    assert config.allow_git_vault is False


def test_profile_scopes_runtime_paths_without_changing_shared_roots(
    tmp_path: Path,
) -> None:
    config = Config(
        profile="Work Account",
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
        contacts_file=tmp_path / "contacts.json",
    )

    assert config.profile == "Work Account"
    assert config.profile_key.startswith("work-account--")
    assert config.profile_cache_dir == tmp_path / "cache" / "profiles" / config.profile_key
    assert config.profile_state_dir == tmp_path / "state" / "profiles" / config.profile_key
    assert config.profile_contacts_file == (
        tmp_path / "profiles" / config.profile_key / "contacts.json"
    )
    assert config.cache_dir == tmp_path / "cache"
    assert config.state_dir == tmp_path / "state"


def test_archive_state_is_scoped_by_profile_and_destination_vault(tmp_path: Path) -> None:
    common = {"state_dir": tmp_path / "state"}
    personal = Config(profile="personal", vault_dir=tmp_path / "vault-a", **common)
    work = Config(profile="work", vault_dir=tmp_path / "vault-a", **common)
    another_vault = Config(profile="personal", vault_dir=tmp_path / "vault-b", **common)

    assert (
        len(
            {
                personal.archive_state_dir,
                work.archive_state_dir,
                another_vault.archive_state_dir,
            }
        )
        == 3
    )
    assert personal.archive_state_dir.is_relative_to(personal.profile_state_dir)


@pytest.mark.parametrize("profile", ["", "   "])
def test_profile_rejects_blank_values(profile: str) -> None:
    with pytest.raises(ValidationError):
        Config(profile=profile)


def test_wacli_account_and_store_are_mutually_exclusive(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="wacli_account|wacli_db"):
        Config(wacli_account="example-account", wacli_db=tmp_path / "store")


def test_environment_account_and_store_are_mutually_exclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    Config._write_default(config_path)
    monkeypatch.setenv("WA2VAULT_WACLI_ACCOUNT", "example-account")
    monkeypatch.setenv("WA2VAULT_WACLI_DB", str(tmp_path / "store"))

    with pytest.raises(ValueError, match="mutually exclusive"):
        Config.load(config_path)


def test_named_profile_applies_account_and_path_overrides(tmp_path: Path) -> None:
    config = Config(
        wacli_db=tmp_path / "default-store",
        profiles={
            "work": {
                "wacli_account": "example-work",
                "vault_dir": tmp_path / "work-vault",
                "language": "en",
            }
        },
    )

    selected = config.select_profile("work")

    assert selected.profile == "work"
    assert selected.wacli_account == "example-work"
    assert selected.wacli_db is None
    assert selected.vault_dir == tmp_path / "work-vault"
    assert selected.language == "en"


def test_unknown_named_profile_fails_with_available_names() -> None:
    config = Config(profiles={"personal": {}})

    with pytest.raises(ValueError, match="personal"):
        config.select_profile("work")


def test_environment_profile_selects_matching_toml_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[profiles.work]\nwacli_account = "example-work"\nlanguage = "en"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("WA2VAULT_PROFILE", "work")

    selected = Config.load(config_path)

    assert selected.profile == "work"
    assert selected.wacli_account == "example-work"
    assert selected.language == "en"


def test_explicit_profile_overrides_env_without_inheriting_another_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'profile = "personal"\nlanguage = "es"\n'
        '[profiles.personal]\nlanguage = "pt"\n'
        '[profiles.work]\nwacli_account = "example-work"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("WA2VAULT_PROFILE", "personal")

    selected = Config.load(config_path, profile="work")

    assert selected.profile == "work"
    assert selected.language == "es"
    assert selected.wacli_account == "example-work"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("WA2VAULT_DEFAULT_DAYS", "0"),
        ("WA2VAULT_ASR_BACKEND", "unknown"),
        ("WA2VAULT_COMMAND_TIMEOUT", "0"),
        ("WA2VAULT_PROFILE", "   "),
    ],
)
def test_invalid_environment_overrides_are_revalidated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    config_path = tmp_path / "config.toml"
    Config._write_default(config_path)
    monkeypatch.setenv(name, value)

    with pytest.raises((ValidationError, ValueError)):
        Config.load(config_path)


def test_timeout_values_must_be_positive() -> None:
    for field in ("command_timeout", "media_timeout", "ffmpeg_timeout"):
        with pytest.raises(ValidationError):
            Config.model_validate({field: 0})


def test_transcript_result_model() -> None:
    result = TranscriptResult(text="hi", language="es", backend="faster-whisper")
    assert result.duration_s is None


def test_get_transcriber_selects_faster_whisper() -> None:
    config = Config(asr_backend="faster-whisper", asr_model="medium", language="es")
    transcriber = get_transcriber(config)
    assert transcriber.name == "faster-whisper"


def test_faster_whisper_is_concrete_transcriber() -> None:
    config = Config()
    transcriber = get_transcriber(config)
    assert isinstance(transcriber, FasterWhisperTranscriber)
    assert isinstance(transcriber, Transcriber)
    assert transcriber.name == "faster-whisper"


def test_faster_whisper_transcribe_missing_file_raises(tmp_path: Path) -> None:
    transcriber = get_transcriber(Config())
    with pytest.raises(FileNotFoundError):
        transcriber.transcribe(tmp_path / "does-not-exist.ogg")


def test_unknown_backend_raises() -> None:
    config = Config()
    config = config.model_copy(update={"asr_backend": "bogus"})
    with pytest.raises(ValueError, match="Unknown ASR backend"):
        get_transcriber(config)
