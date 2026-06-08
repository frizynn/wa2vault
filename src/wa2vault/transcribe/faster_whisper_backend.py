"""Default ASR backend: faster-whisper (CTranslate2) on CPU.

faster-whisper runs the Whisper family of models via CTranslate2, which is
efficient on CPU with quantized weights (e.g. ``compute_type="int8"``). This is
the default backend for wa2vault and targets CPU-only machines.

PHASE-2 STUB: the :meth:`FasterWhisperTranscriber.transcribe` body is not yet
implemented. The constructor and contract are final; only the transcription
logic is deferred to Phase 2.
"""

from __future__ import annotations

from pathlib import Path

from wa2vault.transcribe.base import Transcriber, TranscriptResult


class FasterWhisperTranscriber(Transcriber):
    """Transcribe audio with faster-whisper on the CPU.

    Args:
        model: faster-whisper model name/size (e.g. ``"medium"``). May also be a
            local model directory path.
        device: Compute device. Always ``"cpu"`` for wa2vault's target machines.
        compute_type: CTranslate2 compute type. ``"int8"`` gives a good
            speed/quality/memory trade-off on CPU.
        language: Default language hint (ISO-639-1, e.g. ``"es"``).
    """

    name = "faster-whisper"

    def __init__(
        self,
        model: str = "medium",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str = "es",
    ) -> None:
        self.model = model
        self.device = device
        self.compute_type = compute_type
        self.language = language

    def transcribe(self, audio_path: Path, language: str = "es") -> TranscriptResult:
        """Transcribe a single audio file to text.

        PHASE-2 STUB: not yet implemented.

        Intended Phase-2 flow:

        1. Validate that ``audio_path`` exists (raise ``FileNotFoundError``).
        2. Decode/normalize the audio with ffmpeg: WhatsApp voice notes are
           Opus in an Ogg container; convert to 16 kHz mono PCM WAV
           (``ffmpeg -i <in> -ar 16000 -ac 1 -f wav <tmp.wav>``) in a temp file
           under the configured cache/work dir. ffmpeg is a hard runtime
           requirement and is present on the target machine.
        3. Lazily construct and cache a ``faster_whisper.WhisperModel`` with
           ``self.model``, ``device=self.device``,
           ``compute_type=self.compute_type`` (build it once per process, not
           per call).
        4. Run ``model.transcribe(wav_path, language=language)`` and join the
           returned segments into a single text string; capture the detected
           language and audio duration from the returned ``info``.
        5. Clean up the temporary WAV.
        6. Return a :class:`TranscriptResult` with ``backend=self.name``.

        Args:
            audio_path: Source audio file path.
            language: Language hint (ISO-639-1 code).

        Returns:
            A :class:`TranscriptResult`.
        """
        raise NotImplementedError("implemented in Phase 2")


__all__ = ["FasterWhisperTranscriber"]
