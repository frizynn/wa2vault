"""Tests for the opt-in Groq ASR backend, with no network, key, or real audio."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path

import pytest

from wa2vault.config import Config
from wa2vault.transcribe import get_transcriber, groq_backend
from wa2vault.transcribe.groq_backend import DEFAULT_MODEL, ENDPOINT, GroqTranscriber


class _Response(io.BytesIO):
    """The context manager ``urlopen`` returns."""

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _http_error(
    code: int, body: bytes, headers: dict[str, str] | None = None
) -> urllib.error.HTTPError:
    message = Message()
    for name, value in (headers or {}).items():
        message[name] = value
    return urllib.error.HTTPError(ENDPOINT, code, "error", message, io.BytesIO(body))


@pytest.fixture
def audio(tmp_path: Path) -> Path:
    path = tmp_path / "voice-note.ogg"
    path.write_bytes(b"synthetic-ogg")
    return path


@pytest.fixture(autouse=True)
def fake_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the re-encode with a byte copy so the upload content is predictable."""

    def run(audio_path: Path, out_path: Path, output_args: list[str], *, timeout: float) -> None:
        assert output_args == ["-ar", "16000", "-ac", "1", "-c:a", "flac"]
        out_path.write_bytes(b"flac:" + audio_path.read_bytes())

    monkeypatch.setattr(groq_backend, "run_ffmpeg", run)


@pytest.fixture
def requests(monkeypatch: pytest.MonkeyPatch) -> list[urllib.request.Request]:
    """Record each request and answer with a fixed transcript."""
    seen: list[urllib.request.Request] = []

    def urlopen(request: urllib.request.Request, timeout: float) -> _Response:
        seen.append(request)
        return _Response(json.dumps({"text": " hola mundo ", "duration": 1.5}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return seen


def test_factory_selects_groq(tmp_path: Path) -> None:
    key_file = tmp_path / "groq.key"
    config = Config(asr_backend="groq", asr_model=DEFAULT_MODEL, groq_api_key_file=key_file)

    transcriber = get_transcriber(config)

    assert isinstance(transcriber, GroqTranscriber)
    assert transcriber.name == "groq"
    assert transcriber.api_key_file == key_file


def test_uploads_flac_and_returns_the_transcript(
    monkeypatch: pytest.MonkeyPatch, audio: Path, requests: list[urllib.request.Request]
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-key")

    result = GroqTranscriber().transcribe(audio, language="es")

    assert result.text == "hola mundo"
    assert result.language == "es"
    assert result.duration_s == 1.5
    assert result.backend == f"groq:{DEFAULT_MODEL}"
    (request,) = requests
    assert request.full_url == ENDPOINT
    assert request.get_header("Authorization") == "Bearer test-key"
    body = request.data
    assert f'name="model"\r\n\r\n{DEFAULT_MODEL}\r\n'.encode() in body
    assert b'name="language"\r\n\r\nes\r\n' in body
    assert b"flac:synthetic-ogg" in body


def test_key_file_is_used_when_the_environment_has_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    audio: Path,
    requests: list[urllib.request.Request],
) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    key_file = tmp_path / "groq.key"
    key_file.write_text("file-key\n", encoding="utf-8")

    GroqTranscriber(api_key_file=key_file).transcribe(audio)

    assert requests[0].get_header("Authorization") == "Bearer file-key"


def test_missing_key_fails_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    audio: Path,
    requests: list[urllib.request.Request],
) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        GroqTranscriber(api_key_file=tmp_path / "missing.key").transcribe(audio)
    assert requests == []


def test_http_error_reports_the_status_without_the_key(
    monkeypatch: pytest.MonkeyPatch, audio: Path
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-key")

    def urlopen(request: urllib.request.Request, timeout: float) -> _Response:
        raise _http_error(401, b'{"error": {"message": "Invalid API Key"}}')

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    with pytest.raises(RuntimeError, match="HTTP 401") as raised:
        GroqTranscriber().transcribe(audio)
    assert "test-key" not in str(raised.value)


def test_rate_limit_is_retried_once(monkeypatch: pytest.MonkeyPatch, audio: Path) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    answers = iter(
        [
            _http_error(429, b"slow down", {"Retry-After": "2"}),
            _Response(json.dumps({"text": "ok"}).encode()),
        ]
    )
    waits: list[float] = []

    def urlopen(request: urllib.request.Request, timeout: float) -> _Response:
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(groq_backend.time, "sleep", waits.append)

    assert GroqTranscriber().transcribe(audio).text == "ok"
    assert waits == [2.0]


def test_a_faster_whisper_size_is_rejected(audio: Path) -> None:
    with pytest.raises(ValueError, match="faster-whisper size"):
        GroqTranscriber(model="medium").transcribe(audio)


def test_missing_audio_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        GroqTranscriber().transcribe(tmp_path / "does-not-exist.ogg")
