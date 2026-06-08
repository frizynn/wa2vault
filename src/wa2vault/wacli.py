"""Thin client around the external ``wacli`` binary.

wa2vault never talks to WhatsApp directly; it shells out to ``wacli``
(github.com/openclaw/wacli), which mirrors a linked WhatsApp Web device into a
local SQLite store and exposes JSON output on every command.

This module centralizes process invocation so the rest of wa2vault deals with
parsed JSON, not subprocess plumbing. Every invocation runs with wacli's
read-only guard enabled (``WACLI_READONLY=1``), enforcing wa2vault's
never-send posture: wacli will reject any command that would write to WhatsApp
or mutate the local store.

wacli store location
--------------------
wacli stores its SQLite DB and downloaded media in a single "store directory".
By default this is the platform state dir; on Linux that is the XDG state dir,
``~/.local/state/wacli`` (confirmed via ``wacli doctor --json`` ->
``data.store_dir``). It can be overridden with the ``--store`` flag or the
``WACLI_STORE_DIR`` environment variable; wa2vault threads ``Config.wacli_db``
through as ``--store`` when set.

Discovered wacli interface (v0.11.0)
------------------------------------
The data layer below was reverse-engineered from ``wacli --help`` /
``<cmd> --help``, the project docs (``docs/messages.md``, ``docs/media.md``),
and the ``openclaw/wacli`` Go source (``internal/store/types.go`` for the JSON
shapes), because the machine is not paired yet (no live data). All assumptions
are coded defensively and should be re-validated after pairing.

Commands and flags used (each appended after ``--read-only --json [--store …]``):

* ``sync --once`` -- one-shot sync: keeps syncing until idle (~30s) and exits.
  ``--follow`` defaults to true, so ``--once`` is required to make it terminate.
  wacli's ``--read-only`` guard permits ``sync`` (it only blocks WhatsApp
  writes / message mutations, not store mirroring). Returns an
  implementation-defined summary object; counts are surfaced when present.
* ``chats list --limit N`` -- ``data`` is a JSON array of chat objects (or
  ``null`` when empty). Backs :meth:`list_chats` / :meth:`resolve_chat`.
* ``messages export --chat JID [--after T] [--before T] [--limit N]`` -- ``data``
  is ``{"messages": [<message>] | null, "fts": bool}``, ordered oldest-first.
  ``--after`` / ``--before`` accept RFC3339 or ``YYYY-MM-DD``. We pass the time
  window through *and* re-filter in Python so the returned window is exact.
* ``media download --chat JID --id MSG_ID --output PATH`` -- with the global
  ``--read-only`` guard the ``--output`` flag is mandatory (wacli refuses to
  write into the store DB in read-only mode). Returns
  ``{chat, id, path, bytes, media_type, mime_type, downloaded, read_only,
  recorded}``. Expired/unavailable media (404 on WhatsApp's CDN) makes wacli
  exit non-zero, surfaced here as :class:`WacliError` and handled gracefully.

Message JSON shape (``store.Message``; Go serializes most fields by their
PascalCase Go name, so keys are PascalCase unless an explicit ``json:`` tag
exists). Fields we read (all optional / accessed via ``.get``):

* ``MsgID`` -- WhatsApp message id (NOT ``id``).
* ``ChatJID`` / ``ChatName``.
* ``SenderJID`` / ``SenderName``.
* ``Timestamp`` -- RFC3339 string (Go ``time.Time``); may carry an offset.
* ``FromMe`` -- bool.
* ``Text`` -- raw body; ``DisplayText`` -- render-ready body (preferred).
* ``MediaCaption`` -- caption attached to media.
* ``MediaType`` -- ``"image"``, ``"video"``, ``"audio"``, ``"ptt"``,
  ``"document"``, ``"sticker"``, ``"gif"``, … (empty for plain text).
* ``MimeType`` / ``Filename`` / ``LocalPath`` -- media metadata; ``LocalPath`` is
  only populated by a prior *writable* download.
* ``QuotedMsgID`` (json tag ``quoted_msg_id``) -- id of the quoted message.

Chat JSON shape (``store.Chat``; explicit snake_case json tags):
``{jid, kind, name, last_message_ts, archived, pinned, muted_until, unread,
unread_count}`` where ``kind`` is one of ``dm``/``group``/``newsletter``/
``broadcast``/``unknown``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wa2vault.config import Config
from wa2vault.models import ChatType, MessageKind, MessageRecord


class WacliError(RuntimeError):
    """Raised when a wacli invocation fails or its output cannot be parsed."""


class ChatNotFound(WacliError):
    """Raised when no chat matches a :meth:`WacliClient.resolve_chat` query."""


class ChatNotUnique(WacliError):
    """Raised when a chat query matches more than one chat.

    Args:
        query: The original query string.
        candidates: The chats that matched, so the caller (or user) can refine
            the query.
    """

    def __init__(self, query: str, candidates: list[ChatRef]) -> None:
        self.query = query
        self.candidates = candidates
        listed = ", ".join(f"{c.name or '(unnamed)'} <{c.jid}>" for c in candidates)
        super().__init__(
            f"Chat query {query!r} is ambiguous; {len(candidates)} matches: {listed}"
        )


@dataclass(frozen=True)
class ChatRef:
    """A resolved reference to a single WhatsApp chat.

    Attributes:
        jid: The chat's JID (e.g. ``123@s.whatsapp.net`` or ``…@g.us``).
        name: Human-readable chat name, or None if wacli has none.
        chat_type: Normalized :data:`~wa2vault.models.ChatType`.
    """

    jid: str
    name: str | None
    chat_type: ChatType


# wacli's chat ``kind`` -> our normalized ChatType. Anything else falls back to
# JID-suffix detection (see :func:`_chat_type_from_jid`).
_CHAT_KIND_MAP: dict[str, ChatType] = {
    "dm": "dm",
    "group": "group",
    "newsletter": "channel",
}

# wacli's message/media type -> our normalized MessageKind. Voice notes ("ptt",
# push-to-talk) are kept distinct from regular "audio" so only they are
# transcribed by default. "gif" is a short looping video on WhatsApp.
_MEDIA_KIND_MAP: dict[str, MessageKind] = {
    "image": "image",
    "video": "video",
    "gif": "video",
    "audio": "audio",
    "ptt": "ptt",
    "voice": "ptt",
    "document": "document",
    "sticker": "sticker",
    "location": "location",
}


class WacliClient:
    """Run ``wacli`` subcommands and parse their JSON output.

    Args:
        config: Resolved wa2vault configuration. Provides the wacli binary
            name/path and, optionally, a custom store directory.
    """

    def __init__(self, config: Config) -> None:
        self.config = config

    # ------------------------------------------------------------------ #
    # Process plumbing
    # ------------------------------------------------------------------ #
    def _base_args(self) -> list[str]:
        """Build the leading argv shared by every invocation (binary + globals)."""
        args = [self.config.wacli_bin, "--read-only", "--json"]
        if self.config.wacli_db is not None:
            args += ["--store", str(self.config.wacli_db)]
        return args

    def _env(self) -> dict[str, str]:
        """Environment for wacli, forcing the read-only guard on."""
        env = dict(os.environ)
        env["WACLI_READONLY"] = "1"
        return env

    def ensure_available(self) -> None:
        """Raise :class:`WacliError` if the wacli binary cannot be found."""
        if shutil.which(self.config.wacli_bin) is None and not os.path.exists(
            self.config.wacli_bin
        ):
            raise WacliError(
                f"wacli binary {self.config.wacli_bin!r} not found on PATH. "
                "Install it (see the wa2vault README) or set 'wacli_bin' in the config."
            )

    def run_json(self, *args: str, timeout: float | None = None) -> Any:
        """Run a wacli subcommand and return its parsed JSON payload.

        wacli wraps successful JSON output as
        ``{"success": true, "data": ..., "error": null}``; this method returns
        the ``data`` field. On a non-zero exit or an error envelope it raises
        :class:`WacliError`.

        Args:
            *args: Subcommand and flags to append after the base argv, e.g.
                ``"chats", "list", "--limit", "500"``.
            timeout: Optional timeout in seconds.

        Returns:
            The decoded ``data`` field of the wacli JSON envelope.
        """
        self.ensure_available()
        argv = self._base_args() + list(args)
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                env=self._env(),
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise WacliError(f"Failed to run wacli: {exc}") from exc

        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip()
            raise WacliError(
                f"wacli {' '.join(args)} exited with {proc.returncode}: {detail}"
            )

        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise WacliError(
                f"Could not parse wacli JSON output for {' '.join(args)}: {exc}"
            ) from exc

        if isinstance(payload, dict) and "success" in payload:
            if not payload.get("success", False):
                raise WacliError(f"wacli reported an error: {payload.get('error')!r}")
            return payload.get("data")
        return payload

    # ------------------------------------------------------------------ #
    # Implemented commands
    # ------------------------------------------------------------------ #
    def list_chats(self, limit: int = 500) -> list[dict[str, Any]]:
        """Return the chat list via ``wacli chats list --json``.

        Args:
            limit: Maximum number of chats to return.

        Returns:
            A list of chat dicts as emitted by wacli (shape passed through
            verbatim; the CLI layer selects the fields it displays).
        """
        data = self.run_json("chats", "list", "--limit", str(limit))
        if data is None:
            # wacli returns a null `data` field when there are no chats
            # (e.g. before authentication / sync).
            return []
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("chats"), list):
            return data["chats"]
        raise WacliError(
            f"Unexpected 'chats list' payload shape: {type(data).__name__}"
        )

    def sync_once(self, *, full: bool = False) -> dict[str, Any]:
        """Run a one-shot sync of the local store via ``wacli sync --once``.

        ``wacli sync`` follows the stream indefinitely by default; ``--once``
        makes it sync until idle and exit, which is what an archiver wants.

        Args:
            full: When True, also download media in the background during the
                sync (``--download-media``). When False, only message metadata
                is mirrored and media is fetched lazily by :meth:`ensure_media`.

        Returns:
            A concise summary dict. wacli's exact ``sync`` payload is not pinned
            by the docs, so any reported counts are surfaced under their
            original keys and a normalized ``store`` snapshot is included when
            wacli provides one.
        """
        args = ["sync", "--once"]
        if full:
            args.append("--download-media")
        data = self.run_json(*args)
        return self._summarize_sync(data)

    @staticmethod
    def _summarize_sync(data: Any) -> dict[str, Any]:
        """Reduce wacli's ``sync`` payload to a concise summary dict."""
        if not isinstance(data, dict):
            return {"ok": True}
        summary: dict[str, Any] = {"ok": True}
        # Surface any scalar counts wacli reports (e.g. synced/new message
        # totals) without depending on a specific key name.
        for key, value in data.items():
            if isinstance(value, (int, float, bool, str)):
                summary[key] = value
        store = data.get("store")
        if isinstance(store, dict):
            summary["store"] = store
        return summary

    def resolve_chat(self, query: str) -> ChatRef:
        """Resolve a chat by display name or JID using :meth:`list_chats`.

        Resolution order:

        1. If ``query`` is itself a JID present in the chat list, use it.
        2. Case-insensitive *exact* name match (if exactly one).
        3. Case-insensitive *substring* name match (if exactly one).

        Args:
            query: A chat display name (full or partial) or a chat JID.

        Returns:
            The matching :class:`ChatRef`.

        Raises:
            ChatNotFound: No chat matched.
            ChatNotUnique: More than one chat matched at the resolution level
                that first produced candidates.
        """
        needle = query.strip()
        if not needle:
            raise ChatNotFound("Empty chat query")

        chats = [self._chat_ref(c) for c in self.list_chats()]
        chats = [c for c in chats if c is not None]

        # 1. Exact JID match.
        jid_matches = [c for c in chats if c.jid.lower() == needle.lower()]
        if jid_matches:
            return jid_matches[0]

        lowered = needle.lower()

        # 2. Case-insensitive exact name match.
        exact = [c for c in chats if (c.name or "").strip().lower() == lowered]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise ChatNotUnique(query, exact)

        # 3. Case-insensitive substring name match.
        partial = [c for c in chats if lowered in (c.name or "").lower()]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            raise ChatNotUnique(query, partial)

        raise ChatNotFound(f"No chat matches query {query!r}")

    def export_messages(
        self,
        chat_jid: str,
        *,
        since: datetime,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[MessageRecord]:
        """Export a chat's messages in a time window as :class:`MessageRecord`.

        Runs ``wacli messages export`` filtered to ``chat_jid`` and the time
        window, then parses each row into a normalized record. The window is
        passed to wacli *and* re-applied in Python so the returned range is
        exact regardless of wacli's boundary semantics.

        Args:
            chat_jid: JID of the chat to export.
            since: Inclusive lower bound. Naive datetimes are assumed UTC.
            until: Exclusive upper bound, or None for "up to now".
            limit: Maximum number of messages to request from wacli. None uses
                wacli's own default.

        Returns:
            Records ordered oldest-first (wacli exports ascending), filtered to
            ``[since, until)``.
        """
        since_utc = _to_utc(since)
        until_utc = _to_utc(until) if until is not None else None

        args = ["messages", "export", "--chat", chat_jid, "--after", _wacli_time(since_utc)]
        if until_utc is not None:
            args += ["--before", _wacli_time(until_utc)]
        if limit is not None:
            args += ["--limit", str(limit)]

        data = self.run_json(*args)
        rows = self._extract_message_rows(data)

        records: list[MessageRecord] = []
        for row in rows:
            record = self._parse_message(row)
            if record is None:
                continue
            if record.timestamp < since_utc:
                continue
            if until_utc is not None and record.timestamp >= until_utc:
                continue
            records.append(record)
        return records

    def ensure_media(self, record: MessageRecord) -> Path | None:
        """Ensure a message's media file is present locally, returning its path.

        If ``record.media_path`` already points at an existing file, it is
        returned unchanged. Otherwise wacli downloads the media into the cache
        directory. Because wa2vault runs wacli read-only, the download requires
        an explicit ``--output`` path and is not recorded in wacli's store.

        Old media can expire on WhatsApp's CDN (HTTP 404); in that case wacli
        exits non-zero, which is caught here and reported as ``None`` rather
        than raised, so a single expired attachment never aborts a pull.

        Args:
            record: The message whose media to materialize. Records with no
                media (``kind == "text"`` / ``"system"`` and no media metadata)
                return None.

        Returns:
            The local :class:`~pathlib.Path` to the media file, or None if the
            message has no media or the media is unavailable/expired.
        """
        if record.media_path is not None and record.media_path.exists():
            return record.media_path

        if not _has_media(record):
            return None

        target = self._media_target(record)
        try:
            data = self.run_json(
                "media",
                "download",
                "--chat",
                record.chat_jid,
                "--id",
                record.id,
                "--output",
                str(target),
            )
        except WacliError:
            # Expired/unavailable media (e.g. 404 on WhatsApp's CDN) or a
            # message wacli has no downloadable metadata for: degrade to None.
            return None

        path = _download_path(data) or target
        return path if path.exists() else None

    def _media_target(self, record: MessageRecord) -> Path:
        """Compute the local output path for a message's downloaded media."""
        media_dir = self.config.cache_dir / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        suffix = _media_suffix(record)
        safe_id = record.id.replace("/", "_").replace("\\", "_")
        return media_dir / f"{safe_id}{suffix}"

    # ------------------------------------------------------------------ #
    # Parsing helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_message_rows(data: Any) -> list[dict[str, Any]]:
        """Pull the message list out of wacli's ``messages export`` payload."""
        if data is None:
            return []
        if isinstance(data, dict):
            messages = data.get("messages")
            if messages is None:
                return []
            if isinstance(messages, list):
                return [m for m in messages if isinstance(m, dict)]
            raise WacliError(
                f"Unexpected 'messages' field type: {type(messages).__name__}"
            )
        if isinstance(data, list):
            return [m for m in data if isinstance(m, dict)]
        raise WacliError(
            f"Unexpected 'messages export' payload shape: {type(data).__name__}"
        )

    @classmethod
    def _chat_ref(cls, raw: dict[str, Any]) -> ChatRef | None:
        """Build a :class:`ChatRef` from a wacli chat dict, or None if unusable."""
        jid = _first_str(raw, "jid", "JID", "chat_jid", "ChatJID")
        if not jid:
            return None
        name = _first_str(raw, "name", "Name", "chat_name", "ChatName")
        kind = _first_str(raw, "kind", "Kind") or ""
        chat_type = _CHAT_KIND_MAP.get(kind.lower()) or _chat_type_from_jid(jid)
        return ChatRef(jid=jid, name=name or None, chat_type=chat_type)

    @classmethod
    def _parse_message(cls, raw: dict[str, Any]) -> MessageRecord | None:
        """Map one wacli message dict to a :class:`MessageRecord`.

        Returns None for rows that are unusable (missing id or timestamp), so a
        single malformed row never aborts an export.
        """
        msg_id = _first_str(raw, "MsgID", "msg_id", "id", "ID")
        chat_jid = _first_str(raw, "ChatJID", "chat_jid", "jid")
        timestamp = _parse_timestamp(
            _first_value(raw, "Timestamp", "timestamp", "ts", "Ts")
        )
        if not msg_id or not chat_jid or timestamp is None:
            return None

        chat_name = _first_str(raw, "ChatName", "chat_name")
        chat_type = _chat_type_from_jid(chat_jid)
        from_me = bool(_first_value(raw, "FromMe", "from_me") or False)
        sender_jid = _first_str(raw, "SenderJID", "sender_jid")
        sender_name = _first_str(raw, "SenderName", "sender_name")
        media_type = _first_str(raw, "MediaType", "media_type")
        media_mime = _first_str(raw, "MimeType", "mime_type")
        reply_to = _first_str(raw, "QuotedMsgID", "quoted_msg_id")

        # Prefer the render-ready display text, then raw text, then caption.
        text = (
            _first_str(raw, "DisplayText", "display_text")
            or _first_str(raw, "Text", "text")
            or _first_str(raw, "MediaCaption", "media_caption")
        )

        local_path = _first_str(raw, "LocalPath", "local_path")
        media_path = Path(local_path) if local_path else None
        if media_path is not None and not media_path.exists():
            media_path = None

        return MessageRecord(
            id=msg_id,
            chat_jid=chat_jid,
            chat_name=chat_name or None,
            chat_type=chat_type,
            timestamp=timestamp,
            from_me=from_me,
            sender_jid=sender_jid or None,
            sender_name=sender_name or None,
            kind=cls._map_kind(media_type, media_mime, text),
            text=text or None,
            media_path=media_path,
            media_mime=media_mime or None,
            reply_to_id=reply_to or None,
            raw=raw,
        )

    @staticmethod
    def _map_kind(
        media_type: str | None,
        media_mime: str | None,
        text: str | None,
    ) -> MessageKind:
        """Map wacli's media/message type to a normalized :data:`MessageKind`.

        Args:
            media_type: wacli's ``MediaType`` (empty/None for plain text).
            media_mime: wacli's ``MimeType``, used to disambiguate voice notes
                that arrive labelled ``"audio"`` (Opus voice notes are PTT).
            text: The message text, used to distinguish plain text from system
                notices when no media is present.

        Returns:
            The normalized message kind.
        """
        mt = (media_type or "").strip().lower()
        if not mt:
            return "text" if text else "system"

        kind = _MEDIA_KIND_MAP.get(mt)
        if kind is None:
            return "other"

        # WhatsApp voice notes are sometimes surfaced as generic "audio" with an
        # Opus mime type; treat those as PTT so they are transcribed.
        if kind == "audio" and media_mime and "opus" in media_mime.lower():
            return "ptt"
        return kind


def _to_utc(value: datetime) -> datetime:
    """Return ``value`` as a timezone-aware UTC datetime (naive -> assumed UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _wacli_time(value: datetime) -> str:
    """Format a UTC datetime as an RFC3339 string wacli accepts for time filters."""
    return value.astimezone(UTC).isoformat()


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse a wacli timestamp into an aware UTC datetime, or None if invalid.

    Accepts RFC3339 strings (wacli's ``time.Time`` JSON) as well as integer or
    float Unix epoch seconds, tolerating a trailing ``Z`` for UTC.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        return _to_utc(parsed)
    return None


def _chat_type_from_jid(jid: str) -> ChatType:
    """Derive a normalized chat type from a JID's server suffix.

    Falls back to ``"dm"`` for unknown/broadcast servers, since wa2vault treats
    one-to-one-ish conversations as direct messages.
    """
    server = jid.rsplit("@", 1)[-1].lower() if "@" in jid else ""
    if server == "g.us":
        return "group"
    if server == "newsletter":
        return "channel"
    return "dm"


def _has_media(record: MessageRecord) -> bool:
    """Return True if a record represents a downloadable media message."""
    return record.kind in {
        "image",
        "audio",
        "ptt",
        "video",
        "document",
        "sticker",
    }


def _media_suffix(record: MessageRecord) -> str:
    """Pick a sensible file suffix for a downloaded media file.

    Prefers the original filename's extension, then a mime-derived guess, then
    a kind-based default. Voice notes default to ``.ogg`` (Opus).
    """
    filename = _first_str(record.raw, "Filename", "filename")
    if filename:
        suffix = Path(filename).suffix
        if suffix:
            return suffix

    mime = (record.media_mime or "").split(";", 1)[0].strip().lower()
    mime_suffixes = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "audio/ogg": ".ogg",
        "audio/opus": ".ogg",
        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a",
        "video/mp4": ".mp4",
        "application/pdf": ".pdf",
    }
    if mime in mime_suffixes:
        return mime_suffixes[mime]

    kind_suffixes = {
        "image": ".jpg",
        "audio": ".ogg",
        "ptt": ".ogg",
        "video": ".mp4",
        "sticker": ".webp",
        "document": ".bin",
    }
    return kind_suffixes.get(record.kind, ".bin")


def _download_path(data: Any) -> Path | None:
    """Extract the downloaded file path from wacli's ``media download`` payload."""
    if isinstance(data, dict):
        path = _first_str(data, "path", "Path", "local_path", "LocalPath")
        if path:
            return Path(path)
    return None


def _first_value(raw: dict[str, Any], *keys: str) -> Any:
    """Return the first present, non-None value among ``keys``."""
    for key in keys:
        if key in raw and raw[key] is not None:
            return raw[key]
    return None


def _first_str(raw: dict[str, Any], *keys: str) -> str | None:
    """Return the first present value among ``keys`` coerced to a non-empty str."""
    value = _first_value(raw, *keys)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "WacliClient",
    "WacliError",
    "ChatRef",
    "ChatNotFound",
    "ChatNotUnique",
]
