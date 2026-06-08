"""Optional future ASR backend: NVIDIA Nemotron streaming ASR on CPU.

This is a placeholder for the optional
``nvidia/nemotron-3.5-asr-streaming-0.6b`` CPU backend planned for a later
release. It is not part of Phase 1 or Phase 2; selecting it currently raises
``NotImplementedError``. See the README for status.

PHASE-2 STUB (future): the entire backend is deferred.
"""

from __future__ import annotations

from pathlib import Path

from wa2vault.transcribe.base import Transcriber, TranscriptResult


class NemotronTranscriber(Transcriber):
    """Transcribe audio with NVIDIA Nemotron streaming ASR on the CPU.

    Optional future backend (``nvidia/nemotron-3.5-asr-streaming-0.6b``). Not
    yet implemented; see the README for status and rationale.

    Args:
        model: Model identifier or local path.
        language: Default language hint (ISO-639-1 code).
    """

    name = "nemotron"

    def __init__(
        self,
        model: str = "nvidia/nemotron-3.5-asr-streaming-0.6b",
        language: str = "es",
    ) -> None:
        self.model = model
        self.language = language

    def transcribe(self, audio_path: Path, language: str = "es") -> TranscriptResult:
        """Transcribe a single audio file to text.

        FUTURE STUB: the Nemotron CPU backend is not implemented yet.

        Args:
            audio_path: Source audio file path.
            language: Language hint (ISO-639-1 code).

        Returns:
            A :class:`TranscriptResult`.
        """
        raise NotImplementedError("Nemotron CPU backend — future; see README")


__all__ = ["NemotronTranscriber"]
