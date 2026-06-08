"""Plumbing tests for the transcription layer.

These tests exercise wiring, not transcription accuracy:

- :class:`FasterWhisperTranscriber` decodes audio with ffmpeg and returns a
  well-formed :class:`TranscriptResult` (empty text is acceptable for synthetic
  silence).
- :class:`TranscriptCache` round-trips transcripts through SQLite.

The model test uses the ``tiny`` model (~75 MB) for speed and downloads it on
first run; if the download is not possible (offline), it skips with a clear
reason rather than failing.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from wa2vault.transcribe.base import TranscriptResult
from wa2vault.transcribe.cache import TranscriptCache
from wa2vault.transcribe.faster_whisper_backend import (
    TARGET_SAMPLE_RATE,
    FasterWhisperTranscriber,
)


def _make_wav(path: Path, seconds: float = 1.0) -> None:
    """Generate a ~``seconds`` long 16 kHz mono WAV at ``path`` via ffmpeg."""
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r={TARGET_SAMPLE_RATE}:cl=mono",
            "-t",
            str(seconds),
            str(path),
        ],
        capture_output=True,
        check=True,
    )


@pytest.fixture
def sample_wav(tmp_path: Path) -> Path:
    """A ~1 second 16 kHz mono WAV usable as a fake voice note."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not available on PATH")
    wav = tmp_path / "sample.wav"
    _make_wav(wav)
    return wav


def test_transcribe_returns_transcript_result(sample_wav: Path) -> None:
    transcriber = FasterWhisperTranscriber(model="tiny")
    try:
        result = transcriber.transcribe(sample_wav)
    except Exception as exc:  # noqa: BLE001 - surface only model-load failures
        if _is_model_load_failure(exc):
            pytest.skip(f"tiny model unavailable offline: {exc}")
        raise

    assert isinstance(result, TranscriptResult)
    assert isinstance(result.text, str)
    assert result.backend.startswith("faster-whisper")
    assert result.language == "es"


def test_transcribe_missing_file_raises(tmp_path: Path) -> None:
    transcriber = FasterWhisperTranscriber(model="tiny")
    with pytest.raises(FileNotFoundError):
        transcriber.transcribe(tmp_path / "does-not-exist.ogg")


def test_transcribe_undecodable_audio_raises(tmp_path: Path) -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not available on PATH")
    garbage = tmp_path / "garbage.ogg"
    garbage.write_bytes(b"this is not audio")
    transcriber = FasterWhisperTranscriber(model="tiny")
    with pytest.raises(RuntimeError, match="ffmpeg failed"):
        transcriber.transcribe(garbage)


def test_cache_roundtrip(tmp_path: Path) -> None:
    cache = TranscriptCache(tmp_path / "cache")
    result = TranscriptResult(
        text="hola mundo",
        language="es",
        duration_s=1.5,
        backend="faster-whisper:tiny",
    )

    assert cache.get("MSG_1") is None
    cache.set("MSG_1", result)
    assert cache.get("MSG_1") == "hola mundo"


def test_cache_missing_key_returns_none(tmp_path: Path) -> None:
    cache = TranscriptCache(tmp_path / "cache")
    assert cache.get("never-stored") is None


def test_cache_set_replaces_existing(tmp_path: Path) -> None:
    cache = TranscriptCache(tmp_path / "cache")
    first = TranscriptResult(text="first", language="es", backend="faster-whisper:tiny")
    second = TranscriptResult(
        text="second", language="es", backend="faster-whisper:tiny"
    )
    cache.set("MSG_1", first)
    cache.set("MSG_1", second)
    assert cache.get("MSG_1") == "second"


def test_cache_creates_directory(tmp_path: Path) -> None:
    cache_dir = tmp_path / "nested" / "cache"
    assert not cache_dir.exists()
    TranscriptCache(cache_dir)
    assert cache_dir.exists()


def _is_model_load_failure(exc: Exception) -> bool:
    """Heuristically detect a model download/load failure (vs a real bug).

    Used only to convert an offline ``tiny`` model download failure into a skip
    instead of an error; decoding/transcription bugs still surface.
    """
    message = str(exc).lower()
    needles = (
        "connection",
        "network",
        "offline",
        "download",
        "resolve",
        "timed out",
        "huggingface",
        "could not",
        "unable to",
        "no such file",
    )
    return any(needle in message for needle in needles)
