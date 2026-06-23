"""Markdown renderer for wa2vault.

This module is the final stage of the pipeline: it turns a list of normalized
:class:`~wa2vault.models.MessageRecord` instances into a single Obsidian note
and writes that note into the vault.

The output is optimized for an AI agent reading the note (not a human skimming
chat bubbles): a YAML frontmatter block carries machine-readable metadata
(counts, date range, JIDs), and the body is a flat, chronological transcript
grouped by day. The rendering is fully deterministic for a given input, so a
note can be safely re-rendered and overwritten without spurious diffs.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from pathlib import Path

from wa2vault.models import MessageRecord

# Spanish UI strings rendered into the note. They are centralized here and not
# yet localized; switching the output language would mean translating this block.
#
# Concise placeholders for non-text / non-media message kinds. Voice notes
# ("ptt"/"audio") and images are handled separately because they can carry a
# transcript / embed.
_KIND_PLACEHOLDERS: dict[str, str] = {
    "video": "*[video]*",
    "document": "*[documento]*",
    "sticker": "*[sticker]*",
    "location": "*[ubicación]*",
    "system": "*[sistema]*",
    "other": "*[no soportado]*",
}

#: Media kinds rendered as a clickable vault link (the file is copied into the
#: vault but not embedded inline). Images are handled separately as ``![[…]]``
#: embeds; audio is consumed via its transcript.
_LINKED_MEDIA_KINDS = ("document", "video", "sticker")

_IMAGE_UNAVAILABLE = "*(imagen no disponible)*"
_AUDIO_UNTRANSCRIBED = "*(audio sin transcribir)*"
#: Shown when an audio note was downloaded and transcribed but came back empty
#: (silence / unintelligible), so the empty result is not mistaken for a bug.
_AUDIO_EMPTY_TRANSCRIPT = "*(audio sin contenido transcribible)*"
#: Shown when media (audio, document, …) is gone from WhatsApp's CDN and can no
#: longer be downloaded - an explicit state, not a generic "unavailable".
_MEDIA_EXPIRED = "*(media expirada en WhatsApp, no se pudo descargar)*"
#: Sender label for messages sent by the linked account (Spanish: "Me").
_SELF_SENDER_LABEL = "Yo"
#: Fallback sender label when neither a name nor a JID is known.
_UNKNOWN_SENDER_LABEL = "?"

_REPLY_MARKER = "↳ "
_FALLBACK_SLUG = "chat"


def render_markdown(
    records: list[MessageRecord],
    *,
    chat_name: str,
    chat_jid: str,
    chat_type: str,
    days: int,
    generated_at: datetime,
) -> str:
    """Render normalized messages to an Obsidian-friendly Markdown note.

    Messages are emitted in chronological order, grouped under ``## YYYY-MM-DD``
    day headings. The note opens with a YAML frontmatter block carrying
    machine-readable metadata (counts, date range, JIDs) for agent consumption.

    Args:
        records: Messages to render. They are sorted by timestamp internally, so
            the caller need not pre-sort them.
        chat_name: Human-readable chat/group/channel name for the title.
        chat_jid: JID of the chat (recorded in frontmatter).
        chat_type: Chat kind (``dm`` / ``group`` / ``channel``).
        days: The ``--days`` window the export was requested for.
        generated_at: When this note was generated (serialized as ISO 8601).

    Returns:
        The complete Markdown document, including the trailing newline.
    """
    ordered = sorted(records, key=lambda record: record.timestamp)

    message_count = len(ordered)
    images_count = sum(1 for record in ordered if record.kind == "image")
    audios_transcribed = sum(
        1
        for record in ordered
        if record.kind in ("ptt", "audio") and record.transcript
    )
    date_range = _date_range(ordered)

    frontmatter = _render_frontmatter(
        chat_name=chat_name,
        chat_jid=chat_jid,
        chat_type=chat_type,
        days=days,
        message_count=message_count,
        images_count=images_count,
        audios_transcribed=audios_transcribed,
        date_range=date_range,
        generated_at=generated_at,
    )

    summary = _render_summary(
        date_range=date_range,
        message_count=message_count,
        images_count=images_count,
        audios_transcribed=audios_transcribed,
    )

    body = _render_body(ordered)

    parts = [frontmatter, f"# {chat_name}", summary]
    if body:
        parts.append(body)

    return "\n\n".join(parts) + "\n"


def write_note(
    markdown: str,
    *,
    vault_dir: Path,
    output_subdir: str,
    chat_name: str,
) -> Path:
    """Write a rendered note into the vault, returning its path.

    The note is written to ``{vault_dir}/{output_subdir}/{slug}.md`` where
    ``slug`` is a filesystem-safe, readable slug of ``chat_name``. Parent
    directories are created as needed and an existing note at the target path is
    overwritten.

    Args:
        markdown: The rendered note body (as returned by :func:`render_markdown`).
        vault_dir: Obsidian vault root.
        output_subdir: Subdirectory inside the vault for chat notes.
        chat_name: Chat name used to derive the note filename.

    Returns:
        The absolute path the note was written to.
    """
    target_dir = vault_dir / output_subdir
    target_dir.mkdir(parents=True, exist_ok=True)

    note_path = target_dir / f"{slugify(chat_name)}.md"
    note_path.write_text(markdown, encoding="utf-8")
    return note_path


# --------------------------------------------------------------------------- #
# Frontmatter + summary
# --------------------------------------------------------------------------- #
def _render_frontmatter(
    *,
    chat_name: str,
    chat_jid: str,
    chat_type: str,
    days: int,
    message_count: int,
    images_count: int,
    audios_transcribed: int,
    date_range: tuple[str, str] | None,
    generated_at: datetime,
) -> str:
    """Build the YAML frontmatter block (including the ``---`` fences)."""
    date_range_value = (
        f"{date_range[0]} → {date_range[1]}" if date_range is not None else "null"
    )
    lines = [
        "---",
        "source: whatsapp",
        f"chat: {_yaml_scalar(chat_name)}",
        f"chat_jid: {_yaml_scalar(chat_jid)}",
        f"chat_type: {_yaml_scalar(chat_type)}",
        f"days: {days}",
        f"message_count: {message_count}",
        f"images_count: {images_count}",
        f"audios_transcribed: {audios_transcribed}",
        f"date_range: {_yaml_scalar(date_range_value)}"
        if date_range is not None
        else "date_range: null",
        f"generated_at: {_yaml_scalar(generated_at.isoformat())}",
        "tags: [whatsapp, chat-archive]",
        "---",
    ]
    return "\n".join(lines)


def _render_summary(
    *,
    date_range: tuple[str, str] | None,
    message_count: int,
    images_count: int,
    audios_transcribed: int,
) -> str:
    """Build the one-line human/agent summary under the title."""
    if date_range is None:
        span = "no messages"
    elif date_range[0] == date_range[1]:
        span = date_range[0]
    else:
        span = f"{date_range[0]} → {date_range[1]}"

    return (
        f"_{span} · {message_count} messages · {images_count} images · "
        f"{audios_transcribed} audios transcribed_"
    )


# --------------------------------------------------------------------------- #
# Body
# --------------------------------------------------------------------------- #
def _render_body(records: list[MessageRecord]) -> str:
    """Render the chronological, day-grouped message body."""
    blocks: list[str] = []
    current_day: str | None = None

    for record in records:
        day = record.timestamp.strftime("%Y-%m-%d")
        if day != current_day:
            blocks.append(f"## {day}")
            current_day = day
        blocks.append(_render_message(record))

    return "\n\n".join(blocks)


def _render_message(record: MessageRecord) -> str:
    """Render a single message: a header line plus its content."""
    time = record.timestamp.strftime("%H:%M")
    header = f"**{time} — {_sender_label(record)}**"
    if record.reply_to_id:
        header = f"{_REPLY_MARKER}{header}"

    content = _render_content(record)
    return f"{header}\n{content}"


def _render_content(record: MessageRecord) -> str:
    """Render the content lines for a message based on its kind."""
    if record.kind == "image":
        return _render_image(record)
    if record.kind in ("ptt", "audio"):
        return _render_audio(record)
    if record.kind == "text":
        return _clean_text(record.text) or _KIND_PLACEHOLDERS["other"]
    if record.kind in _LINKED_MEDIA_KINDS:
        return _render_attachment(record)

    placeholder = _KIND_PLACEHOLDERS.get(record.kind, _KIND_PLACEHOLDERS["other"])
    caption = _clean_text(record.text)
    if caption:
        return f"{placeholder}\n{caption}"
    return placeholder


def _render_image(record: MessageRecord) -> str:
    """Render an image message as an Obsidian embed plus optional caption."""
    if record.media_path is not None:
        body = f"![[{_embed_target(record.media_path)}]]"
    elif record.media_expired:
        body = _MEDIA_EXPIRED
    else:
        body = _IMAGE_UNAVAILABLE

    caption = _clean_text(record.text)
    if caption:
        return f"{body}\n{caption}"
    return body


def _render_attachment(record: MessageRecord) -> str:
    """Render a document / video / sticker as a clickable vault link or fallback.

    When the file was copied into the vault, it is linked with Obsidian's
    ``[[...]]`` so it is clickable (and openable for documents). Expired media is
    called out explicitly; otherwise a per-kind placeholder is shown.
    """
    if record.media_path is not None:
        body = f"[[{_embed_target(record.media_path)}]]"
    elif record.media_expired:
        body = _MEDIA_EXPIRED
    else:
        body = _KIND_PLACEHOLDERS.get(record.kind, _KIND_PLACEHOLDERS["other"])

    caption = _clean_text(record.text)
    if caption and caption != body:
        return f"{body}\n{caption}"
    return body


def _render_audio(record: MessageRecord) -> str:
    """Render a voice note / audio as a transcript blockquote or a clear fallback.

    Three non-transcript states are distinguished so an empty line never looks
    like a bug: media expired on the CDN (cannot be transcribed), a download that
    transcribed to empty text (silence / unintelligible), and audio not yet
    transcribed.
    """
    transcript = _clean_text(record.transcript)
    if transcript:
        quoted = "\n".join(f"> {line}" for line in transcript.splitlines())
        return f"> 🎤\n{quoted}" if "\n" in transcript else f"> 🎤 {transcript}"
    if record.media_expired:
        return _MEDIA_EXPIRED
    if record.transcript is not None:
        # A transcript was produced but is empty after cleaning: the audio was
        # downloaded and run through ASR, it just had no transcribable speech.
        return _AUDIO_EMPTY_TRANSCRIPT
    return _AUDIO_UNTRANSCRIBED


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _sender_label(record: MessageRecord) -> str:
    """Resolve the display label for a message's sender."""
    if record.from_me:
        return _SELF_SENDER_LABEL
    if record.sender_name:
        return record.sender_name
    if record.sender_jid:
        return record.sender_jid
    return _UNKNOWN_SENDER_LABEL


def _date_range(records: list[MessageRecord]) -> tuple[str, str] | None:
    """Return the (first, last) message dates as ``YYYY-MM-DD``, or None."""
    if not records:
        return None
    first = records[0].timestamp.strftime("%Y-%m-%d")
    last = records[-1].timestamp.strftime("%Y-%m-%d")
    return first, last


def _embed_target(media_path: Path) -> str:
    """Return the Obsidian embed target for a media file.

    The path is normalized for embedding as an absolute string. Absolute paths
    embed reliably in Obsidian regardless of where the vault root sits, and the
    pipeline already records absolute media paths on each record.
    """
    return media_path.as_posix()


def _clean_text(value: str | None) -> str:
    """Return ``value`` stripped, or an empty string when None/blank."""
    if value is None:
        return ""
    return value.strip()


def _yaml_scalar(value: str) -> str:
    """Quote a string as a double-quoted YAML scalar.

    Double quoting is always applied so values containing ``:``, ``#``, leading
    spaces, or other YAML-significant characters serialize unambiguously.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def slugify(name: str) -> str:
    """Derive a filesystem-safe, readable slug from a chat name.

    Unicode is transliterated to ASCII where possible, path separators and
    other unsafe characters are dropped, and runs of whitespace/punctuation
    collapse to single hyphens. An empty result falls back to ``chat``.
    """
    normalized = unicodedata.normalize("NFKD", name)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_name = ascii_name.lower()
    ascii_name = re.sub(r"[^a-z0-9]+", "-", ascii_name)
    ascii_name = ascii_name.strip("-")
    return ascii_name or _FALLBACK_SLUG


__all__ = ["render_markdown", "slugify", "write_note"]
