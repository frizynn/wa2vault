"""The ``pull`` orchestration pipeline.

This module holds the data-heavy orchestration that backs the ``pull`` CLI
command. It wires together the already-implemented building blocks:

    - :class:`~wa2vault.wacli.WacliClient` for WhatsApp data access (sync, chat
      resolution, message export, and media download).
    - :func:`~wa2vault.transcribe.get_transcriber` plus
      :class:`~wa2vault.transcribe.cache.TranscriptCache` for local voice-note
      transcription with persistent caching.
    - :func:`~wa2vault.render.render_markdown` /
      :func:`~wa2vault.render.write_note` for rendering and persisting the note.

End-to-end flow of :func:`pull_chat`:

    1. sync       -- refresh the local wacli store (best-effort; a sync failure
                     never aborts the pull, it only adds a warning).
    2. resolve    -- map a chat name/JID to a concrete chat via
                     ``WacliClient.resolve_chat``.
    3. query      -- export the last N days of the chat as ``MessageRecord``s.
    4. media      -- for each media message, materialize the local file via
                     ``ensure_media``; copy IMAGE files into the vault and point
                     ``media_path`` at the *vault-relative* path so Obsidian
                     ``![[...]]`` embeds resolve. Skipped when ``download_media``
                     is False.
    5. transcribe -- transcribe ``ptt``/``audio`` voice notes through a
                     per-message transcript cache. Skipped when ``transcribe``
                     is False; a single transcription failure only adds a
                     warning.
    6. render     -- render the records to a Markdown note and write it into the
                     vault (``config.vault_dir / config.output_subdir``).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import re

from wa2vault.config import Config
from wa2vault.contacts import ContactBook
from wa2vault.models import MessageRecord
from wa2vault.render import _slugify, render_markdown, write_note
from wa2vault.transcribe import get_transcriber
from wa2vault.transcribe.cache import TranscriptCache
from wa2vault.wacli import ChatNotFound, ChatRef, WacliClient, WacliError

#: Subdirectory (under ``output_subdir``) where copied attachments are stored.
_MEDIA_SUBDIR = "_media"

#: Message kinds that carry a transcribable voice note.
_AUDIO_KINDS = frozenset({"ptt", "audio"})

#: A sender name that is really just a phone number / bare JID (digits, an
#: optional leading "+", spaces/dashes, optionally followed by an "@server"
#: suffix). Such names are replaced by the resolved contact name for DM chats.
_BARE_NUMBER_RE = re.compile(r"^\+?[\d\s\-()]+(@\S+)?$")


@dataclass(frozen=True)
class PullResult:
    """Summary of a completed :func:`pull_chat` run.

    Attributes:
        chat_name: Human-readable name of the pulled chat.
        chat_jid: JID of the pulled chat.
        days: The ``--days`` window that was requested.
        message_count: Total number of messages written to the note.
        images_count: Number of images embedded with a usable media path.
        audios_transcribed: Number of voice notes that ended with a transcript.
        note_path: Absolute path to the written Markdown note.
        range_start: Earliest message timestamp, or None if no messages.
        range_end: Latest message timestamp, or None if no messages.
        warnings: Best-effort issues encountered during the pull (e.g. a failed
            sync or a single transcription error). The pull still succeeds.
    """

    chat_name: str
    chat_jid: str
    days: int
    message_count: int
    images_count: int
    audios_transcribed: int
    note_path: Path
    range_start: datetime | None
    range_end: datetime | None
    warnings: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        summary = (
            f"Wrote {self.message_count} messages "
            f"({self.images_count} images, {self.audios_transcribed} audios transcribed) "
            f"from {self.chat_name!r} to {self.note_path}"
        )
        if self.warnings:
            summary += f" ({len(self.warnings)} warning(s): " + "; ".join(self.warnings) + ")"
        return summary


def pull_chat(
    config: Config,
    chat: str,
    days: int,
    transcribe: bool = True,
    download_media: bool = True,
) -> PullResult:
    """Run the full pull pipeline for one chat and write a vault note.

    Args:
        config: Resolved wa2vault configuration.
        chat: Chat name or JID to pull.
        days: Number of days of history to include (window = now - ``days``).
        transcribe: When False, skip voice-note transcription.
        download_media: When False, skip locating/downloading media (images
            then render as unavailable).

    Returns:
        A :class:`PullResult` summarizing what was written.

    Raises:
        wa2vault.wacli.ChatNotFound: No chat matched ``chat``.
        wa2vault.wacli.ChatNotUnique: ``chat`` matched more than one chat.
    """
    warnings: list[str] = []
    client = WacliClient(config)

    # 1. Sync (best-effort): a sync failure OR a sync that exceeds
    # ``config.sync_timeout`` must not abort the pull; we proceed with whatever
    # is already in the local store. The timeout matters because ``sync --once``
    # only returns once the stream goes idle, which can take minutes on a stale
    # store with a large backlog -- without a bound the whole pull would hang
    # here and never reach export/transcribe/render.
    try:
        client.sync_once(timeout=config.sync_timeout)
    except WacliError as exc:
        warnings.append(f"sync incomplete, using local store as-is: {exc}")

    # 2. Resolve the chat. A local contact-book name takes precedence so the
    # note title/filename use the friendly name even when WhatsApp never synced
    # the contact. ChatNotFound / ChatNotUnique propagate to the caller.
    chatref = _resolve_chat(client, chat, config)
    chat_name = chatref.name or chatref.jid
    chat_slug = _slugify(chat_name)

    # 3. Export the requested time window.
    since = datetime.now(timezone.utc) - timedelta(days=days)
    records = client.export_messages(chatref.jid, since=since)

    # 3b. For DM chats, backfill missing/number-only sender names with the
    # resolved chat name so the rendered timeline shows the person, not a number.
    _fill_dm_sender_names(records, chatref)

    # 4. Media: materialize local files; copy images into the vault (relative
    # path), keep the local audio path for transcription. When media is
    # disabled, clear any media path carried over from the export so attachments
    # render as unavailable rather than leaking a non-vault-relative path.
    local_audio_paths: dict[str, Path] = {}
    if download_media:
        _resolve_media(
            client,
            records,
            config=config,
            chat_slug=chat_slug,
            local_audio_paths=local_audio_paths,
            warnings=warnings,
        )
    else:
        for record in records:
            record.media_path = None

    # 5. Transcription (best-effort per message).
    if transcribe:
        _transcribe_audio(
            records,
            config=config,
            local_audio_paths=local_audio_paths,
            warnings=warnings,
        )

    # 6. Render and write the note.
    generated_at = datetime.now(timezone.utc)
    markdown = render_markdown(
        records,
        chat_name=chat_name,
        chat_jid=chatref.jid,
        chat_type=chatref.chat_type,
        days=days,
        generated_at=generated_at,
    )
    note_path = write_note(
        markdown,
        vault_dir=config.vault_dir,
        output_subdir=config.output_subdir,
        chat_name=chat_name,
    )

    images_count = sum(
        1 for record in records if record.kind == "image" and record.media_path is not None
    )
    audios_transcribed = sum(
        1 for record in records if record.kind in _AUDIO_KINDS and record.transcript
    )
    range_start, range_end = _timestamp_range(records)

    return PullResult(
        chat_name=chat_name,
        chat_jid=chatref.jid,
        days=days,
        message_count=len(records),
        images_count=images_count,
        audios_transcribed=audios_transcribed,
        note_path=note_path,
        range_start=range_start,
        range_end=range_end,
        warnings=warnings,
    )


def _resolve_chat(client: WacliClient, chat: str, config: Config) -> ChatRef:
    """Resolve ``chat`` to a :class:`ChatRef`, honoring the local contact book.

    If ``chat`` matches a saved contact name (or a saved number/JID), that JID is
    resolved against wacli to confirm the chat exists and learn its type, and the
    resulting :class:`ChatRef` carries the saved friendly name. Otherwise
    resolution falls back to wacli's own name/JID/substring matching.

    Raises:
        wa2vault.wacli.ChatNotFound: The matched contact has no synced chat yet,
            or no chat matched at all.
        wa2vault.wacli.ChatNotUnique: ``chat`` matched more than one wacli chat.
    """
    book = ContactBook(config.contacts_file)
    mapped = book.find(chat)
    if mapped is None:
        return client.resolve_chat(chat)

    try:
        resolved = client.resolve_chat(mapped)
    except ChatNotFound as exc:
        raise ChatNotFound(
            f"Contact {chat!r} ({mapped}) has no chat/messages synced yet. "
            "Run `wa2vault sync` (or chat with them once) and try again."
        ) from exc

    name = book.name_for(mapped) or resolved.name
    return ChatRef(jid=resolved.jid, name=name, chat_type=resolved.chat_type)


def _fill_dm_sender_names(records: list[MessageRecord], chatref: ChatRef) -> None:
    """Backfill DM sender names that are missing or look like a bare number.

    Only DM chats are touched (groups already carry real per-sender names). For
    each incoming message (``from_me is False``) whose ``sender_name`` is missing
    or looks like a phone number / bare JID, set it to the resolved chat name so
    the rendered timeline shows the person instead of digits.
    """
    if chatref.chat_type != "dm" or not chatref.name:
        return

    for record in records:
        if record.from_me:
            continue
        if _is_missing_or_number(record.sender_name):
            record.sender_name = chatref.name


def _is_missing_or_number(sender_name: str | None) -> bool:
    """Return True if ``sender_name`` is absent or just a phone/JID, not a name."""
    if not sender_name:
        return True
    return bool(_BARE_NUMBER_RE.match(sender_name.strip()))


def _resolve_media(
    client: WacliClient,
    records: list[MessageRecord],
    *,
    config: Config,
    chat_slug: str,
    local_audio_paths: dict[str, Path],
    warnings: list[str],
) -> None:
    """Materialize media for ``records`` (in place).

    For every media-bearing record, ``ensure_media`` returns a local path (or
    None when the attachment is expired/unavailable). Image files are copied
    into the vault attachments directory and ``media_path`` is set to the path
    **relative to the vault root** so Obsidian ``![[...]]`` embeds resolve;
    other media keep ``media_path = None`` (audio is consumed via the
    transcript, not the path). Local audio paths are recorded separately for the
    transcription step.
    """
    attachments_dir = (
        config.vault_dir / config.output_subdir / _MEDIA_SUBDIR / chat_slug
    )

    for record in records:
        local = client.ensure_media(record)
        if local is None:
            # Reset any stale local path so the renderer shows the fallback.
            record.media_path = None
            continue

        if record.kind == "image":
            record.media_path = _copy_image_into_vault(
                local,
                attachments_dir=attachments_dir,
                vault_dir=config.vault_dir,
                chat_slug=chat_slug,
                warnings=warnings,
            )
        elif record.kind in _AUDIO_KINDS:
            # Keep the local audio path for transcription only; the renderer
            # uses the transcript, not the path.
            local_audio_paths[record.id] = local
            record.media_path = None
        else:
            record.media_path = None


def _copy_image_into_vault(
    local: Path,
    *,
    attachments_dir: Path,
    vault_dir: Path,
    chat_slug: str,
    warnings: list[str],
) -> Path | None:
    """Copy an image into the vault, returning its vault-relative path.

    Returns the path relative to ``vault_dir`` (e.g.
    ``Chats/_media/<slug>/<file>``) on success, or None if the copy failed (in
    which case the image renders as unavailable).
    """
    try:
        attachments_dir.mkdir(parents=True, exist_ok=True)
        destination = attachments_dir / local.name
        shutil.copyfile(local, destination)
    except OSError as exc:
        warnings.append(f"could not copy image {local.name!r} into vault: {exc}")
        return None
    return destination.relative_to(vault_dir)


def _transcribe_audio(
    records: list[MessageRecord],
    *,
    config: Config,
    local_audio_paths: dict[str, Path],
    warnings: list[str],
) -> None:
    """Transcribe voice notes for ``records`` (in place), using the cache.

    Each ``ptt``/``audio`` record with a local audio file is transcribed at most
    once: a cache hit short-circuits the model, otherwise the audio is
    transcribed and the result is cached. A single transcription failure adds a
    warning and is skipped, never aborting the whole pull.
    """
    audio_records = [
        record
        for record in records
        if record.kind in _AUDIO_KINDS and record.id in local_audio_paths
    ]
    if not audio_records:
        return

    transcriber = get_transcriber(config)
    cache = TranscriptCache(config.cache_dir)

    for record in audio_records:
        cached = cache.get(record.id)
        if cached is not None:
            record.transcript = cached
            continue

        audio_path = local_audio_paths[record.id]
        try:
            result = transcriber.transcribe(audio_path, language=config.language)
        except Exception as exc:  # noqa: BLE001 - one bad audio must not abort the pull.
            warnings.append(f"transcription failed for message {record.id}: {exc}")
            continue

        cache.set(record.id, result)
        record.transcript = result.text


def _timestamp_range(
    records: list[MessageRecord],
) -> tuple[datetime | None, datetime | None]:
    """Return the (earliest, latest) message timestamps, or (None, None)."""
    if not records:
        return None, None
    timestamps = [record.timestamp for record in records]
    return min(timestamps), max(timestamps)


__all__ = ["PullResult", "pull_chat"]
