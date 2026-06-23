"""Thin client around the external ``wacli`` binary.

wa2vault never talks to WhatsApp directly; it shells out to ``wacli``
(github.com/openclaw/wacli), which mirrors a linked WhatsApp Web device into a
local SQLite store and exposes JSON output on every command.

This module centralizes process invocation so the rest of wa2vault deals with
parsed JSON, not subprocess plumbing. Read/query commands run with wacli's
read-only guard (``--read-only`` / ``WACLI_READONLY=1``) as defense-in-depth.
That guard rejects ANY command that writes WhatsApp *or the local store*, so
``sync_once`` -- which must mirror messages into the local store -- runs with
the guard OFF. wa2vault's never-send guarantee does not depend on the guard:
this client exposes no send/presence command (only sync, read, and media
download), so it cannot message anyone.

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

Commands and flags used (read commands run after ``--read-only --json
[--store …]``; ``sync`` omits ``--read-only``):

* ``sync --once`` -- one-shot sync: keeps syncing until idle (~30s) and exits.
  ``--follow`` defaults to true, so ``--once`` is required to make it terminate.
  Run WITHOUT the read-only guard: ``sync`` writes mirrored messages into the
  local store, which ``--read-only`` rejects. Returns an implementation-defined
  summary object; counts are surfaced when present.
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
import re
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
class MediaResult:
    """Outcome of materializing a message's media via :meth:`WacliClient.ensure_media`.

    Attributes:
        path: Local path to the media file, or None when no file is available.
        expired: True when the media could not be fetched because WhatsApp's CDN
            no longer serves it (HTTP 404/410 - the media expired and cannot be
            re-downloaded). This lets the renderer say so explicitly instead of
            showing a bare "unavailable" placeholder that looks like a bug.
    """

    path: Path | None
    expired: bool = False


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
# Message kinds whose text body is real user text (not a media caption). For
# every other kind, wacli's DisplayText is a synthetic placeholder, so only the
# explicit MediaCaption is treated as the message's text.
_TEXT_KINDS: frozenset[MessageKind] = frozenset({"text", "system"})

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
    def _base_args(self, *, read_only: bool = True) -> list[str]:
        """Build the leading argv shared by every invocation (binary + globals).

        ``read_only`` adds wacli's ``--read-only`` guard. It must be False for
        commands that legitimately write the local store (e.g. ``sync``), which
        the guard would otherwise reject.
        """
        args = [self.config.wacli_bin, "--json"]
        if read_only:
            args.append("--read-only")
        if self.config.wacli_db is not None:
            args += ["--store", str(self.config.wacli_db)]
        return args

    def _env(self, *, read_only: bool = True) -> dict[str, str]:
        """Environment for wacli; enables the read-only guard unless ``read_only`` is False."""
        env = dict(os.environ)
        if read_only:
            env["WACLI_READONLY"] = "1"
        else:
            env.pop("WACLI_READONLY", None)
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

    def run_json(
        self, *args: str, read_only: bool = True, timeout: float | None = None
    ) -> Any:
        """Run a wacli subcommand and return its parsed JSON payload.

        wacli wraps successful JSON output as
        ``{"success": true, "data": ..., "error": null}``; this method returns
        the ``data`` field. On a non-zero exit or an error envelope it raises
        :class:`WacliError`.

        Args:
            *args: Subcommand and flags to append after the base argv, e.g.
                ``"chats", "list", "--limit", "500"``.
            read_only: Run under wacli's read-only guard (default). Pass False
                for commands that must write the local store (e.g. ``sync``).
            timeout: Optional timeout in seconds.

        Returns:
            The decoded ``data`` field of the wacli JSON envelope.
        """
        self.ensure_available()
        argv = self._base_args(read_only=read_only) + list(args)
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                env=self._env(read_only=read_only),
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise WacliError(
                f"wacli {' '.join(args)} timed out after {timeout:g}s"
            ) from exc
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

    def list_groups(self, limit: int = 500) -> list[dict[str, Any]]:
        """Return joined groups via ``wacli groups list --json``.

        wacli keeps a dedicated group table whose ``Name`` is the group
        *subject*. That subject is frequently present here even when
        ``chats list`` reports the bare JID as the chat name -- e.g. right after
        pairing, when WhatsApp's "app state" sync (where chat-level names live)
        fails with an LTHash mismatch. :meth:`group_names` uses this to backfill
        those missing names.

        Group rows (``store.Group``) use PascalCase keys; the fields we read are
        ``JID`` and ``Name`` (both accessed defensively).

        Args:
            limit: Maximum number of groups to return.

        Returns:
            A list of group dicts as emitted by wacli (shape passed through
            verbatim). Empty when there are no groups.
        """
        data = self.run_json("groups", "list", "--limit", str(limit))
        if data is None:
            # wacli returns a null `data` field when there are no groups.
            return []
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("groups"), list):
            return data["groups"]
        raise WacliError(
            f"Unexpected 'groups list' payload shape: {type(data).__name__}"
        )

    def group_names(self, limit: int = 500) -> dict[str, str]:
        """Map group JID -> subject for groups that have a real (non-placeholder) name.

        Built from :meth:`list_groups`. Groups whose name is missing or merely
        echoes the JID are skipped, so callers can ``.get(jid)`` to backfill a
        display name only when a real one exists.
        """
        mapping: dict[str, str] = {}
        for raw in self.list_groups(limit=limit):
            jid = _first_str(raw, "jid", "JID", "chat_jid", "ChatJID", "id")
            name = _first_str(raw, "name", "Name", "subject", "Subject")
            if jid and name and name != jid:
                mapping[jid] = name
        return mapping

    def refresh_groups(self, *, timeout: float | None = None) -> dict[str, Any]:
        """Fetch joined groups live and update the local store (``wacli groups refresh``).

        Unlike :meth:`list_groups` (which reads the local DB), this queries
        WhatsApp for the current set of joined groups and their subjects and
        writes them into wacli's store. Use it when the local DB has no subject
        for a group at all -- a freshly paired device whose app-state never
        synced. Because it writes the local store, it runs with the read-only
        guard OFF; it still only *reads* from WhatsApp, so wa2vault's
        never-send guarantee is unaffected (this client exposes no send command).

        Args:
            timeout: Max seconds to wait for the refresh. None waits indefinitely.

        Returns:
            A concise summary dict (counts surfaced verbatim when wacli reports
            them).
        """
        data = self.run_json("groups", "refresh", read_only=False, timeout=timeout)
        return self._summarize_sync(data)

    def sync_once(
        self, *, full: bool = False, timeout: float | None = None
    ) -> dict[str, Any]:
        """Run a one-shot sync of the local store via ``wacli sync --once``.

        ``wacli sync`` follows the stream indefinitely by default; ``--once``
        makes it sync until idle and exit, which is what an archiver wants.

        ``--once`` only exits once the stream goes idle, so on a stale store
        with a large backlog it can run for minutes. ``timeout`` bounds that:
        when it elapses, wacli is killed and a :class:`WacliError` is raised, so
        a caller that treats sync as best-effort (e.g. :func:`pull_chat`) can
        proceed with whatever already synced instead of hanging.

        Args:
            full: When True, also download media in the background during the
                sync (``--download-media``). When False, only message metadata
                is mirrored and media is fetched lazily by :meth:`ensure_media`.
            timeout: Max seconds to wait for the sync. None waits indefinitely.

        Returns:
            A concise summary dict. wacli's exact ``sync`` payload is not pinned
            by the docs, so any reported counts are surfaced under their
            original keys and a normalized ``store`` snapshot is included when
            wacli provides one.
        """
        args = ["sync", "--once"]
        if full:
            args.append("--download-media")
        # sync writes mirrored messages into the local store, so the read-only
        # guard must be off (it rejects any local-store write).
        data = self.run_json(*args, read_only=False, timeout=timeout)
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
        chats = self._backfill_group_names(chats)

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

    def _backfill_group_names(self, chats: list[ChatRef]) -> list[ChatRef]:
        """Name group chats by their subject from the group table.

        wacli's ``chats list`` ``name`` for a group is unreliable: it is missing
        or just echoes the JID when WhatsApp's app-state sync failed, and it can
        even carry a *participant's* name instead of the group subject. wacli's
        dedicated group table (:meth:`group_names`), by contrast, holds the real
        group subject. So for every group chat we prefer that subject when one
        exists, overriding the chat-list name. This lets a user resolve a group
        by its real subject and gives the note the right title. Best-effort: if
        the group lookup fails, the chats are returned unchanged.
        """
        if not any(c.chat_type == "group" for c in chats):
            return chats
        try:
            names = self.group_names()
        except WacliError:
            return chats
        if not names:
            return chats
        patched: list[ChatRef] = []
        for c in chats:
            if c.chat_type == "group":
                subject = names.get(c.jid)
                if subject and subject != c.name:
                    c = ChatRef(jid=c.jid, name=subject, chat_type=c.chat_type)
            patched.append(c)
        return patched

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

    def ensure_media(self, record: MessageRecord) -> MediaResult:
        """Ensure a message's media file is present locally.

        If ``record.media_path`` already points at an existing file, it is
        returned unchanged. Otherwise wacli downloads the media into the cache
        directory. Because wa2vault runs wacli read-only, the download requires
        an explicit ``--output`` path and is not recorded in wacli's store.

        Old media can expire on WhatsApp's CDN (HTTP 404/410); in that case wacli
        exits non-zero, which is caught here and reported as an ``expired``
        result rather than raised, so a single expired attachment never aborts a
        pull and the renderer can say so explicitly.

        Args:
            record: The message whose media to materialize. Records with no
                media (``kind == "text"`` / ``"system"`` and no media metadata)
                return an empty result.

        Returns:
            A :class:`MediaResult`: ``path`` is the local media file when it
            could be materialized, otherwise None; ``expired`` is True when the
            media was lost on WhatsApp's CDN.
        """
        if record.media_path is not None and record.media_path.exists():
            return MediaResult(path=record.media_path)

        if not _has_media(record):
            return MediaResult(path=None)

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
        except WacliError as exc:
            # Media lost on WhatsApp's CDN (404/410) cannot be re-downloaded;
            # flag it as expired so the renderer can distinguish it from a
            # message wacli simply has no downloadable metadata for.
            return MediaResult(path=None, expired=_is_expired_media_error(exc))

        path = _download_path(data) or target
        return MediaResult(path=path if path.exists() else None)

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

        display_text = _first_str(raw, "DisplayText", "display_text") or _first_str(
            raw, "Text", "text"
        )
        caption = _first_str(raw, "MediaCaption", "media_caption")
        kind = cls._map_kind(media_type, media_mime, display_text)

        # For media messages, the only meaningful text is the user's caption:
        # wacli's DisplayText is a synthetic placeholder ("Sent document", "Sent
        # audio", "[Audio]", ...) that must not leak into the note. For plain
        # text/system messages, use the display/raw text.
        text = caption if kind not in _TEXT_KINDS else display_text

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
            kind=kind,
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


def _is_placeholder_group(jid: str, name: str | None) -> bool:
    """Return True for a group JID whose name is missing or merely echoes the JID.

    wacli's ``chats list`` reports the bare JID as the name for groups whose
    subject never synced (a WhatsApp app-state LTHash failure), so such a name
    is not a real one and should be backfilled from the group table.
    """
    return jid.endswith("@g.us") and (not name or name == jid)


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


#: Matches the HTTP status wacli reports when WhatsApp's CDN no longer serves a
#: media file: 404 (Not Found) or 410 (Gone). Anchored to a "status code" phrase
#: so a stray "404" elsewhere in the message (e.g. inside a filename or path)
#: cannot be mistaken for an expiry.
_EXPIRED_MEDIA_STATUS_RE = re.compile(r"status\s*code\s*(?:404|410)\b", re.IGNORECASE)


def _is_expired_media_error(exc: WacliError) -> bool:
    """Return True if a media-download error means the media expired on the CDN.

    WhatsApp serves attachment bytes from a CDN that drops old media; once gone,
    a download returns HTTP 404 (Not Found) or 410 (Gone) and the bytes cannot
    be recovered. wacli surfaces that status in its error message, which is the
    only signal available here.
    """
    return _EXPIRED_MEDIA_STATUS_RE.search(str(exc)) is not None


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
    "MediaResult",
    "ChatNotFound",
    "ChatNotUnique",
]
