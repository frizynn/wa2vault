"""Pluggable ASR (speech-to-text) backends for wa2vault.

The public contract lives in :mod:`wa2vault.transcribe.base`:

- :class:`~wa2vault.transcribe.base.TranscriptResult` -- the result model.
- :class:`~wa2vault.transcribe.base.Transcriber` -- the abstract backend.
- :func:`~wa2vault.transcribe.base.get_transcriber` -- the factory that selects
  a concrete backend from a :class:`~wa2vault.config.Config`.

Concrete backends:

- :class:`~wa2vault.transcribe.faster_whisper_backend.FasterWhisperTranscriber`
  -- the default CPU backend (faster-whisper / CTranslate2). Audio stays local.
- :class:`~wa2vault.transcribe.groq_backend.GroqTranscriber` -- opt-in Whisper
  hosted by Groq. Audio is uploaded to Groq.

"""

from wa2vault.transcribe.base import (
    Transcriber,
    TranscriptResult,
    get_transcriber,
)

__all__ = ["Transcriber", "TranscriptResult", "get_transcriber"]
