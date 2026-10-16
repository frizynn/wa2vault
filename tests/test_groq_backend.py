"""Tests for the opt-in Groq ASR backend, with no network, key, or real audio."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path

import pytest
from pydantic import ValidationError

from wa2vault.config import Config
from wa2vault.transcribe import get_transcriber, groq_backend
from wa2vault.transcribe.groq_backend import (
    DEFAULT_MODEL,
    ENDPOINT,
    REENCODE_ARGS,
    GroqTranscriber,
)


class _Response(io.BytesIO):
    """The context manager ``urlopen`` returns."""

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _ok(text: str = " hola mundo ", duration: float | None = 1.5) -> _Response:
    return _Response(json.dumps({"text": text, "duration": duration}).encode())


def _http_error(
    code: int, body: bytes, headers: dict[str, str] | None = None
) -> urllib.error.HTTPError:
    message = Message()
    for name, value in (headers or {}).items():
        message[name] = value
    return urllib.error.HTTPError(ENDPOINT, code, "error", message, io.BytesIO(body))


def _answer_with(monkeypatch: pytest.MonkeyPatch, *answers: object) -> list[urllib.request.Request]:
    """Make ``urlopen`` record each request and return or raise the given answers in order."""
    seen: list[urllib.request.Request] = []
    pending = iter(answers)

    def urlopen(request: urllib.request.Request, timeout: float) -> _Response:
        seen.append(request)
        answer = next(pending)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return seen


@pytest.fixture
def voice_note(tmp_path: Path) -> Path:
    path = tmp_path / "voice note.ogg"
    path.write_bytes(b"synthetic-ogg")
    return path


@pytest.fixture
def no_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(*args: object, **kwargs: object) -> None:
        raise AssertionError("a format Groq accepts must not be re-encoded")

    monkeypatch.setattr(groq_backend, "run_ffmpeg", run)


@pytest.fixture
def key(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    return "test-key"


def test_factory_selects_groq(tmp_path: Path) -> None:
    key_file = tmp_path / "groq.key"
    config = Config(asr_backend="groq", asr_model=DEFAULT_MODEL, groq_api_key_file=key_file)

    transcriber = get_transcriber(config)

    assert isinstance(transcriber, GroqTranscriber)
    assert transcriber.api_key_file == key_file


def test_config_refuses_the_local_model_with_groq() -> None:
    with pytest.raises(ValidationError, match="not a Groq model"):
        Config(asr_backend="groq")


def test_key_file_location_follows_its_environment_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    Config._write_default(config_path)
    monkeypatch.setenv("WA2VAULT_GROQ_API_KEY_FILE", str(tmp_path / "other.key"))

    assert Config.load(config_path).groq_api_key_file == tmp_path / "other.key"


@pytest.mark.usefixtures("no_ffmpeg", "key")
def test_uploads_an_ogg_voice_note_as_it_is(
    monkeypatch: pytest.MonkeyPatch, voice_note: Path
) -> None:
    requests = _answer_with(monkeypatch, _ok())

    result = GroqTranscriber().transcribe(voice_note, language="es")

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
    assert b'filename="audio.ogg"\r\nContent-Type: audio/ogg\r\n\r\nsynthetic-ogg\r\n' in body
    assert b"voice note" not in body


@pytest.mark.usefixtures("key")
def test_reencodes_a_format_groq_does_not_take(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "memo.amr"
    source.write_bytes(b"synthetic-amr")
    outputs: list[Path] = []

    def run(audio_path: Path, out_path: Path, output_args: list[str], *, timeout: float) -> None:
        assert output_args == REENCODE_ARGS
        outputs.append(out_path)
        out_path.write_bytes(b"opus:" + audio_path.read_bytes())

    monkeypatch.setattr(groq_backend, "run_ffmpeg", run)
    requests = _answer_with(monkeypatch, _ok())

    GroqTranscriber().transcribe(source)

    assert b'filename="audio.ogg"' in requests[0].data
    assert b"opus:synthetic-amr" in requests[0].data
    assert not outputs[0].exists(), "the temporary re-encode is removed"


@pytest.mark.usefixtures("no_ffmpeg")
def test_key_file_is_used_when_the_environment_has_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, voice_note: Path
) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    key_file = tmp_path / "groq.key"
    key_file.write_text("file-key\n", encoding="utf-8")
    requests = _answer_with(monkeypatch, _ok())

    GroqTranscriber(api_key_file=key_file).transcribe(voice_note)

    assert requests[0].get_header("Authorization") == "Bearer file-key"


def test_missing_key_fails_before_any_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, voice_note: Path
) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    requests = _answer_with(monkeypatch)

    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        GroqTranscriber(api_key_file=tmp_path / "missing.key").transcribe(voice_note)
    assert requests == []


def test_a_key_file_with_more_than_one_line_is_refused_without_showing_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, voice_note: Path
) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    key_file = tmp_path / "groq.key"
    key_file.write_text("# my key\nsecret-value\n", encoding="utf-8")
    requests = _answer_with(monkeypatch)

    with pytest.raises(RuntimeError, match="one line") as raised:
        GroqTranscriber(api_key_file=key_file).transcribe(voice_note)
    assert "secret-value" not in str(raised.value)
    assert requests == []


@pytest.mark.usefixtures("no_ffmpeg", "key")
def test_http_error_reports_the_status(monkeypatch: pytest.MonkeyPatch, voice_note: Path) -> None:
    _answer_with(monkeypatch, _http_error(401, b'{"error": {"message": "Invalid API Key"}}'))

    with pytest.raises(RuntimeError, match="HTTP 401") as raised:
        GroqTranscriber().transcribe(voice_note)
    assert "test-key" not in str(raised.value)


@pytest.mark.usefixtures("no_ffmpeg", "key")
def test_a_short_rate_limit_is_retried_once(
    monkeypatch: pytest.MonkeyPatch, voice_note: Path
) -> None:
    _answer_with(monkeypatch, _http_error(429, b"slow down", {"Retry-After": "2"}), _ok("ok"))
    waits: list[float] = []
    monkeypatch.setattr(groq_backend.time, "sleep", waits.append)

    assert GroqTranscriber().transcribe(voice_note).text == "ok"
    assert waits == [2.0]


@pytest.mark.usefixtures("no_ffmpeg", "key")
def test_a_second_rate_limit_fails(monkeypatch: pytest.MonkeyPatch, voice_note: Path) -> None:
    limited = {"Retry-After": "1"}
    _answer_with(
        monkeypatch, _http_error(429, b"slow", limited), _http_error(429, b"slow", limited)
    )
    monkeypatch.setattr(groq_backend.time, "sleep", lambda seconds: None)

    with pytest.raises(RuntimeError, match="HTTP 429"):
        GroqTranscriber().transcribe(voice_note)


@pytest.mark.usefixtures("no_ffmpeg", "key")
def test_a_long_rate_limit_is_not_waited_for(
    monkeypatch: pytest.MonkeyPatch, voice_note: Path
) -> None:
    _answer_with(monkeypatch, _http_error(429, b"later", {"Retry-After": "120"}))
    waits: list[float] = []
    monkeypatch.setattr(groq_backend.time, "sleep", waits.append)

    with pytest.raises(RuntimeError, match="HTTP 429"):
        GroqTranscriber().transcribe(voice_note)
    assert waits == []


@pytest.mark.usefixtures("no_ffmpeg", "key")
def test_a_timeout_is_reported_as_such(monkeypatch: pytest.MonkeyPatch, voice_note: Path) -> None:
    _answer_with(monkeypatch, TimeoutError("read timed out"))

    with pytest.raises(RuntimeError, match="did not answer within 7s"):
        GroqTranscriber(timeout=7).transcribe(voice_note)


def test_missing_audio_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        GroqTranscriber().transcribe(tmp_path / "does-not-exist.ogg")
