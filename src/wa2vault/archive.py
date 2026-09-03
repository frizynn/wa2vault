"""Durable cumulative message archive backed by SQLite."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from wa2vault.fs import PRIVATE_FILE_MODE, ensure_private_dir
from wa2vault.models import MessageRecord

ARCHIVE_FILENAME = "archive.sqlite3"
ARCHIVE_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    profile TEXT NOT NULL,
    chat_jid TEXT NOT NULL,
    message_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (profile, chat_jid, message_id)
);
CREATE INDEX IF NOT EXISTS messages_timeline
    ON messages(profile, chat_jid, timestamp, message_id);
"""


class ArchiveStore:
    """Idempotently retain every normalized message observed across pull windows."""

    def __init__(self, state_dir: Path) -> None:
        ensure_private_dir(state_dir)
        self.path = state_dir / ARCHIVE_FILENAME
        with self._connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > ARCHIVE_SCHEMA_VERSION:
                raise RuntimeError(
                    f"Archive schema {version} is newer than supported "
                    f"schema {ARCHIVE_SCHEMA_VERSION}"
                )
            connection.executescript(_SCHEMA)
            connection.execute(f"PRAGMA user_version = {ARCHIVE_SCHEMA_VERSION}")
        try:
            self.path.chmod(PRIVATE_FILE_MODE)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def upsert(self, profile: str, chat_jid: str, records: list[MessageRecord]) -> int:
        """Merge records transactionally and return how many identities were new."""
        if not records:
            return 0
        inserted = 0
        with self._connect() as connection:
            existing = {
                message_id: self._decode(payload)
                for message_id, payload in connection.execute(
                    "SELECT message_id, payload_json FROM messages "
                    "WHERE profile = ? AND chat_jid = ?",
                    (profile, chat_jid),
                )
            }
            for record in records:
                previous = existing.get(record.id)
                merged = self._merge(previous, record)
                payload = json.dumps(
                    merged.model_dump(mode="json", exclude={"raw"}),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                connection.execute(
                    "INSERT INTO messages "
                    "(profile, chat_jid, message_id, timestamp, payload_json) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(profile, chat_jid, message_id) DO UPDATE SET "
                    "timestamp = excluded.timestamp, payload_json = excluded.payload_json",
                    (profile, chat_jid, record.id, merged.timestamp.isoformat(), payload),
                )
                inserted += previous is None
        return inserted

    def list(self, profile: str, chat_jid: str) -> list[MessageRecord]:
        """Return the complete stored chat timeline in chronological order."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM messages "
                "WHERE profile = ? AND chat_jid = ? "
                "ORDER BY timestamp, message_id",
                (profile, chat_jid),
            ).fetchall()
        return [self._decode(row[0]) for row in rows]

    @staticmethod
    def _decode(payload: str) -> MessageRecord:
        return MessageRecord.model_validate(json.loads(payload))

    @staticmethod
    def _merge(previous: MessageRecord | None, incoming: MessageRecord) -> MessageRecord:
        if previous is None:
            return incoming.model_copy(update={"raw": {}})
        update = incoming.model_dump(exclude={"raw"})
        for field in ("transcript", "media_path"):
            if update[field] is None and getattr(previous, field) is not None:
                update[field] = getattr(previous, field)
        if update["sender_name"] is None and previous.sender_name is not None:
            update["sender_name"] = previous.sender_name
        if update["chat_name"] is None and previous.chat_name is not None:
            update["chat_name"] = previous.chat_name
        if update["media_path"] is not None:
            update["media_expired"] = False
        return previous.model_copy(update={**update, "raw": {}})


__all__ = ["ARCHIVE_FILENAME", "ARCHIVE_SCHEMA_VERSION", "ArchiveStore"]
