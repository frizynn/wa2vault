"""ffmpeg helper shared by the ASR backends.

Each backend wants the voice note in a different shape (16 kHz mono WAV for the
local model, 16 kHz mono FLAC for an upload), but the way ffmpeg is invoked and
its failures are reported is the same.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def run_ffmpeg(
    audio_path: Path, out_path: Path, output_args: list[str], *, timeout: float = 120.0
) -> None:
    """Re-encode ``audio_path`` into ``out_path`` with the given ffmpeg output arguments.

    Args:
        audio_path: Source audio file (any ffmpeg-decodable format).
        out_path: Destination path (overwritten).
        output_args: ffmpeg arguments placed between the input and the output path.
        timeout: Maximum seconds for one ffmpeg run.

    Raises:
        RuntimeError: If ffmpeg is missing, times out, or fails to decode the input.
    """
    command = ["ffmpeg", "-y", "-i", str(audio_path), *output_args, str(out_path)]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg is required to decode audio but was not found on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ffmpeg timed out after {timeout:g}s") from exc

    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed to decode {audio_path} "
            f"(exit code {result.returncode}): {result.stderr.strip()}"
        )


__all__ = ["run_ffmpeg"]
