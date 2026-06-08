"""Persistent transcript cache backed by SQLite.

Transcribing voice notes on CPU is the slowest step in a wa2vault ``pull``.
The cache lets reruns skip work that was already done: each transcript is keyed
by the stable WhatsApp message id (see :class:`~wa2vault.models.MessageRecord`),
so a message is transcribed at most once across runs.

The store is a single SQLite file under :attr:`~wa2vault.config.Config.cache_dir`.
SQLite is part of the standard library, handles concurrent readers, and keeps
the whole cache in one self-contained, easy-to-inspect file.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wa2vault.transcribe.base import TranscriptResult

#: Default file name for the cache inside ``Config.cache_dir``.
CACHE_FILENAME = "transcripts.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transcripts (
    key       TEXT PRIMARY KEY,
    text      TEXT NOT NULL,
    language  TEXT NOT NULL,
    duration_s REAL,
    backend   TEXT NOT NULL
)
"""


class TranscriptCache:
    """A simple, robust SQLite-backed cache of transcripts.

    Keys are caller-supplied WhatsApp message ids; values are the transcribed
    text (plus a little metadata for inspection). Use :meth:`get` to look up a
    previously cached transcript and :meth:`set` to store one.

    The backing directory is created if missing. Instances are cheap to create;
    each operation opens and closes its own short-lived connection so the cache
    is safe to use from simple scripts without managing connection lifetime.
    """

    def __init__(self, cache_dir: Path) -> None:
        """Open (creating if needed) the cache under ``cache_dir``.

        Args:
            cache_dir: Directory that holds the cache file. Created with parents
                if it does not exist.
        """
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.path = cache_dir / CACHE_FILENAME
        with self._connect() as conn:
            conn.execute(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        """Open a connection to the cache database."""
        return sqlite3.connect(self.path)

    def get(self, key: str) -> str | None:
        """Return the cached transcript text for ``key``, or ``None`` if absent.

        Args:
            key: WhatsApp message id used when the transcript was stored.

        Returns:
            The cached transcript text, or ``None`` if nothing is stored for
            ``key``.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT text FROM transcripts WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row is not None else None

    def set(self, key: str, result: TranscriptResult) -> None:
        """Store ``result`` under ``key``, replacing any existing entry.

        Args:
            key: WhatsApp message id to key the transcript by.
            result: The transcription result to cache.
        """
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO transcripts "
                "(key, text, language, duration_s, backend) VALUES (?, ?, ?, ?, ?)",
                (
                    key,
                    result.text,
                    result.language,
                    result.duration_s,
                    result.backend,
                ),
            )


__all__ = ["TranscriptCache", "CACHE_FILENAME"]
