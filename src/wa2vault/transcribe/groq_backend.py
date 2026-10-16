"""Remote ASR backend: Whisper hosted by Groq (OpenAI-compatible API).

Opt-in with ``asr_backend = "groq"``. Unlike the default local backend, the
voice note leaves this machine. A file in a format Groq accepts (WhatsApp voice
notes are Opus in Ogg) is uploaded as is, so it stays as small as it was; any
other format is first re-encoded with ffmpeg to mono 16 kHz Opus. Only the
standard library is used, so the backend adds no dependency.

The API key comes from ``GROQ_API_KEY`` or, when that is unset, from the file at
``groq_api_key_file``. It is read on the first transcription, so a missing key
becomes a per-audio warning in a pull instead of aborting it.
"""

from __future__ import annotations

import json
import mimetypes
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

#: File types Groq accepts as uploaded. Anything else is re-encoded first.
GROQ_FORMATS = frozenset(
    {".flac", ".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".ogg", ".wav", ".webm"}
)
#: Mono 16 kHz Opus: small, and all the fidelity speech recognition needs.
REENCODE_ARGS = ["-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "32k"]
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
            RuntimeError: If the key is missing or malformed, ffmpeg fails, or Groq
                answers with an error or not at all.
        """
        if not audio_path.exists():
            raise FileNotFoundError(f"Audio file not found: {audio_path}")
        key = self._key()

        # Only the extension is sent as the file name: Groq needs it to detect the
        # format, and a local name could carry quotes or private words.
        suffix = audio_path.suffix.lower()
        if suffix in GROQ_FORMATS:
            payload = self._request(key, f"audio{suffix}", audio_path.read_bytes(), language)
        else:
            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
                ogg_path = Path(tmp.name)
            try:
                run_ffmpeg(audio_path, ogg_path, REENCODE_ARGS, timeout=self.ffmpeg_timeout)
                payload = self._request(key, "audio.ogg", ogg_path.read_bytes(), language)
            finally:
                ogg_path.unlink(missing_ok=True)

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
            source = "GROQ_API_KEY"
            key = os.environ.get("GROQ_API_KEY", "").strip()
            if not key and self.api_key_file is not None and self.api_key_file.is_file():
                source = str(self.api_key_file)
                key = self.api_key_file.read_text(encoding="utf-8").strip()
            if not key:
                raise RuntimeError(
                    f"Groq needs an API key: set GROQ_API_KEY or write it to {self.api_key_file}"
                )
            # The key travels in an HTTP header, and http.client quotes the whole
            # header in its error when it holds a line break.
            if any(char.isspace() or not char.isprintable() for char in key):
                raise RuntimeError(f"The Groq API key in {source} must be one line with no spaces")
            self._api_key = key
        return self._api_key

    def _request(self, key: str, filename: str, audio: bytes, language: str) -> dict[str, Any]:
        """POST one transcription, retrying once if Groq rate-limits it briefly."""
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        body, form_type = _multipart(
            {
                "model": self.model,
                "language": language,
                "response_format": "verbose_json",
                "temperature": "0",
            },
            ("file", filename, content_type, audio),
        )
        request = urllib.request.Request(
            ENDPOINT,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": form_type,
                "User-Agent": "wa2vault",
            },
        )
        retried = False
        while True:
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                wait = _retry_wait(exc)
                if exc.code == 429 and not retried and wait is not None:
                    retried = True
                    time.sleep(wait)
                    continue
                detail = exc.read(300).decode("utf-8", "replace").strip()
                raise RuntimeError(f"Groq answered HTTP {exc.code}: {detail}") from exc
            except urllib.error.URLError as exc:
                raise RuntimeError(f"Groq is unreachable: {exc.reason}") from exc
            except TimeoutError as exc:
                raise RuntimeError(f"Groq did not answer within {self.timeout:g}s") from exc


def _retry_wait(exc: urllib.error.HTTPError) -> float | None:
    """Seconds to wait before retrying a 429, or None if Groq asks for longer than the cap."""
    try:
        wait = float(exc.headers.get("retry-after", "5"))
    except (TypeError, ValueError):
        wait = 5.0
    return max(wait, 0.0) if wait <= _MAX_RETRY_WAIT_S else None


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


__all__ = ["DEFAULT_MODEL", "ENDPOINT", "GROQ_FORMATS", "REENCODE_ARGS", "GroqTranscriber"]
