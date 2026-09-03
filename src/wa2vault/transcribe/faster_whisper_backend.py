"""Default ASR backend: faster-whisper (CTranslate2) on CPU.

faster-whisper runs the Whisper family of models via CTranslate2, which is
efficient on CPU with quantized weights (e.g. ``compute_type="int8"``). This is
the default backend for wa2vault and targets CPU-only machines.

WhatsApp voice notes are Opus in an Ogg container; this backend decodes them to
16 kHz mono PCM WAV with ffmpeg before running the model. The Whisper model is
loaded lazily and cached on the instance, since model load is the expensive,
one-time cost.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from wa2vault.transcribe.base import Transcriber, TranscriptResult

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

#: Sample rate (Hz) Whisper models expect.
TARGET_SAMPLE_RATE = 16_000


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
        ffmpeg_timeout: float = 120.0,
    ) -> None:
        self.model = model
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.ffmpeg_timeout = ffmpeg_timeout
        self._whisper_model: WhisperModel | None = None

    def _get_model(self) -> WhisperModel:
        """Return the cached Whisper model, loading it on first use.

        Loading the model (downloading weights on first run, then initializing
        CTranslate2) is expensive, so it is done once per instance and reused
        across calls.
        """
        if self._whisper_model is None:
            from faster_whisper import WhisperModel

            self._whisper_model = WhisperModel(
                self.model,
                device=self.device,
                compute_type=self.compute_type,
            )
        return self._whisper_model

    def transcribe(self, audio_path: Path, language: str = "es") -> TranscriptResult:
        """Transcribe a single audio file to text.

        The source audio (a WhatsApp Opus voice note, or any ffmpeg-decodable
        file) is decoded to a temporary 16 kHz mono PCM WAV, transcribed with a
        lazily-loaded Whisper model, and the temporary WAV is removed.

        Args:
            audio_path: Source audio file path.
            language: Language hint (ISO-639-1 code).

        Returns:
            A :class:`TranscriptResult` with the joined segment text, the
            language used, the source duration if reported, and this backend's
            identifier.

        Raises:
            FileNotFoundError: If ``audio_path`` does not exist.
            RuntimeError: If ffmpeg fails to decode the audio.
        """
        if not audio_path.exists():
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = Path(tmp.name)
        try:
            self._decode_to_wav(audio_path, wav_path, timeout=self.ffmpeg_timeout)
            model = self._get_model()
            segments, info = model.transcribe(str(wav_path), language=language)
            text = " ".join(stripped for segment in segments if (stripped := segment.text.strip()))
        finally:
            wav_path.unlink(missing_ok=True)

        duration = getattr(info, "duration", None)
        return TranscriptResult(
            text=text,
            language=language,
            duration_s=duration,
            backend=f"{self.name}:{self.model}",
        )

    @staticmethod
    def _decode_to_wav(audio_path: Path, wav_path: Path, *, timeout: float = 120.0) -> None:
        """Decode ``audio_path`` to 16 kHz mono PCM WAV at ``wav_path``.

        Args:
            audio_path: Source audio file (any ffmpeg-decodable format).
            wav_path: Destination WAV path (overwritten).

        Raises:
            RuntimeError: If ffmpeg is missing or fails to decode the input.
        """
        command = [
            "ffmpeg",
            "-y",
            "-i",
            str(audio_path),
            "-ar",
            str(TARGET_SAMPLE_RATE),
            "-ac",
            "1",
            "-f",
            "wav",
            str(wav_path),
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "ffmpeg is required to decode audio but was not found on PATH."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"ffmpeg timed out after {timeout:g}s") from exc

        if result.returncode != 0:
            raise RuntimeError(
                f"ffmpeg failed to decode {audio_path} "
                f"(exit code {result.returncode}): {result.stderr.strip()}"
            )


__all__ = ["FasterWhisperTranscriber"]
