"""Remote ASR backend: Whisper hosted by Groq (OpenAI-compatible API).

Opt-in with ``asr_backend = "groq"``. Unlike the default local backend, the
voice note leaves this machine: each file is re-encoded with ffmpeg to 16 kHz
mono FLAC (Groq's recommended input, and the smallest upload) and sent to Groq
over HTTPS. Only the standard library is used, so the backend adds no
dependency.

The API key comes from ``GROQ_API_KEY`` or, when that is unset, from the file at
``groq_api_key_file``. It is read on the first transcription, so a missing key
becomes a per-audio warning in a pull instead of aborting it.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from wa2vault.transcribe.audio import run_ffmpeg
from wa2vault.transcribe.base import Transcriber, TranscriptResult

ENDPOINT = "https://api.groq.com/openai/v1/audio/transcriptions"
DEFAULT_MODEL = "whisper-large-v3-turbo"

#: faster-whisper model sizes. Seeing one here means the config still names the
#: local backend's model, which Groq would reject with a less helpful error.
_LOCAL_MODEL_NAMES = frozenset(
    {"tiny", "base", "small", "medium", "large", "large-v1", "large-v2", "large-v3", "turbo"}
)
_MAX_RETRY_WAIT_S = 30.0


class GroqTranscriber(Transcriber):
    """Transcribe audio with a Whisper model served by Groq.

    Args:
        model: Groq model id (e.g. ``"whisper-large-v3-turbo"``).
        api_key_file: File holding the API key, used when ``GROQ_API_KEY`` is unset.
        language: Default language hint (ISO-639-1, e.g. ``"es"``).
        timeout: Maximum seconds for one API request.
        ffmpeg_timeout: Maximum seconds for re-encoding one audio file.
    """

    name = "groq"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        api_key_file: Path | None = None,
        language: str = "es",
        timeout: float = 120.0,
        ffmpeg_timeout: float = 120.0,
    ) -> None:
        self.model = model
        self.api_key_file = api_key_file
        self.language = language
        self.timeout = timeout
        self.ffmpeg_timeout = ffmpeg_timeout
        self._api_key: str | None = None

    def transcribe(self, audio_path: Path, language: str = "es") -> TranscriptResult:
        """Upload ``audio_path`` to Groq and return its transcript.

        Raises:
            FileNotFoundError: If ``audio_path`` does not exist.
            ValueError: If ``model`` is a faster-whisper size instead of a Groq model.
            RuntimeError: If the key is missing, ffmpeg fails, or Groq answers with an error.
        """
        if not audio_path.exists():
            raise FileNotFoundError(f"Audio file not found: {audio_path}")
        if self.model in _LOCAL_MODEL_NAMES:
            raise ValueError(
                f"asr_model {self.model!r} is a faster-whisper size; with asr_backend = "
                f"'groq' use a Groq model such as {DEFAULT_MODEL!r}"
            )

        with tempfile.NamedTemporaryFile(suffix=".flac", delete=False) as tmp:
            flac_path = Path(tmp.name)
        try:
            run_ffmpeg(
                audio_path,
                flac_path,
                ["-ar", "16000", "-ac", "1", "-c:a", "flac"],
                timeout=self.ffmpeg_timeout,
            )
            payload = self._request(flac_path.read_bytes(), language)
        finally:
            flac_path.unlink(missing_ok=True)

        duration = payload.get("duration")
        return TranscriptResult(
            text=str(payload.get("text", "")).strip(),
            language=language,
            duration_s=float(duration) if duration is not None else None,
            backend=f"{self.name}:{self.model}",
        )

    def _key(self) -> str:
        """Return the API key: ``GROQ_API_KEY`` first, then ``api_key_file``."""
        if self._api_key is None:
            key = os.environ.get("GROQ_API_KEY", "").strip()
            if not key and self.api_key_file is not None and self.api_key_file.is_file():
                key = self.api_key_file.read_text(encoding="utf-8").strip()
            if not key:
                raise RuntimeError(
                    f"Groq needs an API key: set GROQ_API_KEY or write it to {self.api_key_file}"
                )
            self._api_key = key
        return self._api_key

    def _request(self, audio: bytes, language: str) -> dict[str, Any]:
        """POST one transcription request, retrying once if Groq rate-limits it."""
        body, content_type = _multipart(
            {
                "model": self.model,
                "language": language,
                "response_format": "verbose_json",
                "temperature": "0",
            },
            ("file", "audio.flac", "audio/flac", audio),
        )
        request = urllib.request.Request(
            ENDPOINT,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._key()}",
                "Content-Type": content_type,
                "User-Agent": "wa2vault",
            },
        )
        for attempt in (1, 2):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt == 1:
                    time.sleep(_retry_after(exc))
                    continue
                detail = exc.read(300).decode("utf-8", "replace").strip()
                raise RuntimeError(f"Groq answered HTTP {exc.code}: {detail}") from exc
            except urllib.error.URLError as exc:
                raise RuntimeError(f"Groq is unreachable: {exc.reason}") from exc
        raise RuntimeError("Groq kept rate-limiting the request")


def _retry_after(exc: urllib.error.HTTPError) -> float:
    """Seconds to wait before retrying a 429, from ``Retry-After`` (capped)."""
    try:
        wait = float(exc.headers.get("retry-after", "5"))
    except (TypeError, ValueError):
        wait = 5.0
    return min(max(wait, 0.0), _MAX_RETRY_WAIT_S)


def _multipart(fields: dict[str, str], file: tuple[str, str, str, bytes]) -> tuple[bytes, str]:
    """Encode form ``fields`` and one ``(name, filename, content_type, data)`` file."""
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode()
        )
    name, filename, content_type, data = file
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
        f'filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'.encode()
        + data
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


__all__ = ["DEFAULT_MODEL", "ENDPOINT", "GroqTranscriber"]
