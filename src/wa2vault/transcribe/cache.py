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

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from wa2vault.fs import PRIVATE_FILE_MODE, ensure_private_dir

if TYPE_CHECKING:
    from wa2vault.transcribe.base import TranscriptResult

#: Default file name for the cache inside ``Config.cache_dir``.
CACHE_FILENAME = "transcripts.sqlite3"
CACHE_KEY_SCHEMA_VERSION = 2

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
        ensure_private_dir(cache_dir)
        self.path = cache_dir / CACHE_FILENAME
        with self._connect() as conn:
            conn.execute(_SCHEMA)
        try:
            self.path.chmod(PRIVATE_FILE_MODE)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        """Open a connection to the cache database."""
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def get(self, key: str | TranscriptCacheKey) -> str | None:
        """Return the cached transcript text for ``key``, or ``None`` if absent.

        Args:
            key: WhatsApp message id used when the transcript was stored.

        Returns:
            The cached transcript text, or ``None`` if nothing is stored for
            ``key``.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT text FROM transcripts WHERE key = ?", (_key_value(key),)
            ).fetchone()
        return row[0] if row is not None else None

    def set(self, key: str | TranscriptCacheKey, result: TranscriptResult) -> None:
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
                    _key_value(key),
                    result.text,
                    result.language,
                    result.duration_s,
                    result.backend,
                ),
            )


@dataclass(frozen=True)
class TranscriptCacheKey:
    """Every input that can change a transcription result."""

    profile: str
    chat_jid: str
    message_id: str
    backend: str
    model: str
    language: str
    media_digest: str
    schema_version: int = CACHE_KEY_SCHEMA_VERSION

    @classmethod
    def from_media(
        cls,
        *,
        profile: str,
        chat_jid: str,
        message_id: str,
        backend: str,
        model: str,
        language: str,
        media_path: Path,
    ) -> TranscriptCacheKey:
        digest = hashlib.sha256()
        with media_path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return cls(
            profile=profile,
            chat_jid=chat_jid,
            message_id=message_id,
            backend=backend,
            model=model,
            language=language,
            media_digest=digest.hexdigest(),
        )

    @property
    def value(self) -> str:
        payload = json.dumps(asdict(self), separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _key_value(key: str | TranscriptCacheKey) -> str:
    return key.value if isinstance(key, TranscriptCacheKey) else key


__all__ = [
    "CACHE_FILENAME",
    "CACHE_KEY_SCHEMA_VERSION",
    "TranscriptCache",
    "TranscriptCacheKey",
]
