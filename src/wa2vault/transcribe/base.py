"""Transcription contract: result model, abstract backend, and factory.

This module defines the stable interface that the transcription layer exposes
to the rest of wa2vault. The ``pull`` pipeline and the one-off ``transcribe``
CLI command both depend only on this contract, never on a concrete backend.

Phase-2 backends implement :meth:`Transcriber.transcribe`. The factory
:func:`get_transcriber` maps a :class:`~wa2vault.config.Config` to a concrete
:class:`Transcriber` instance.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from wa2vault.config import Config


class TranscriptResult(BaseModel):
    """Result of transcribing a single audio file."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(description="The transcribed text.")
    language: str = Field(
        description="Detected or assumed language (ISO-639-1 code, e.g. 'es')."
    )
    duration_s: float | None = Field(
        default=None,
        description="Duration of the source audio in seconds, if known.",
    )
    backend: str = Field(
        description="Identifier of the backend that produced this result (e.g. 'faster-whisper')."
    )


class Transcriber(ABC):
    """Abstract local ASR backend.

    Concrete subclasses transcribe a single audio file to text on the CPU.
    Implementations are responsible for any audio decoding/resampling they
    require (WhatsApp voice notes are Opus in an Ogg container).
    """

    #: Stable, human-readable backend identifier (e.g. ``"faster-whisper"``).
    name: str

    @abstractmethod
    def transcribe(self, audio_path: Path, language: str = "es") -> TranscriptResult:
        """Transcribe ``audio_path`` to text.

        Args:
            audio_path: Path to the source audio file (e.g. a ``.ogg``/``.opus``
                WhatsApp voice note, or any ffmpeg-decodable audio).
            language: Language hint as an ISO-639-1 code. Backends may use it as
                a constraint or as a starting point for language detection.

        Returns:
            A :class:`TranscriptResult` with the transcribed text.

        Raises:
            FileNotFoundError: If ``audio_path`` does not exist.
        """
        raise NotImplementedError


def get_transcriber(config: Config) -> Transcriber:
    """Construct the configured :class:`Transcriber`.

    The concrete backend is selected by ``config.asr_backend``. Backends are
    imported lazily so that importing this module (and the CLI) does not pull in
    heavy ASR dependencies until a transcriber is actually requested.

    Args:
        config: Resolved wa2vault configuration.

    Returns:
        A concrete :class:`Transcriber` instance.

    Raises:
        NotImplementedError: If ``config.asr_backend`` is a recognized but
            not-yet-implemented backend (``nemotron``).
        ValueError: If ``config.asr_backend`` is not a known backend.
    """
    backend = config.asr_backend
    if backend == "faster-whisper":
        from wa2vault.transcribe.faster_whisper_backend import (
            FasterWhisperTranscriber,
        )

        return FasterWhisperTranscriber(
            model=config.asr_model,
            language=config.language,
        )
    if backend == "nemotron":
        raise NotImplementedError(
            "The 'nemotron' ASR backend is not implemented yet; use 'faster-whisper'."
        )
    raise ValueError(f"Unknown ASR backend: {backend!r}")


__all__ = ["TranscriptResult", "Transcriber", "get_transcriber"]
