"""Smoke tests for the wa2vault shared contracts.

These tests pin the shape of the shared contracts (models, config, transcriber
factory) that the rest of wa2vault codes against, and assert which backends are
implemented versus intentionally deferred (the Nemotron backend).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from wa2vault.config import Config
from wa2vault.models import MessageRecord
from wa2vault.transcribe import get_transcriber
from wa2vault.transcribe.base import Transcriber, TranscriptResult
from wa2vault.transcribe.faster_whisper_backend import FasterWhisperTranscriber
from wa2vault.transcribe.nemotron_backend import NemotronTranscriber


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


def test_nemotron_backend_is_future_stub(tmp_path: Path) -> None:
    config = Config(asr_backend="nemotron")
    transcriber = get_transcriber(config)
    assert isinstance(transcriber, NemotronTranscriber)
    audio = tmp_path / "note.ogg"
    audio.write_bytes(b"")
    with pytest.raises(NotImplementedError):
        transcriber.transcribe(audio)


def test_unknown_backend_raises() -> None:
    config = Config()
    config = config.model_copy(update={"asr_backend": "bogus"})
    with pytest.raises(ValueError, match="Unknown ASR backend"):
        get_transcriber(config)
