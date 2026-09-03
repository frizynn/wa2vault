"""Shared data contracts for wa2vault.

This module defines :class:`MessageRecord`, the canonical normalized message
that flows through the whole pipeline:

    data access (wacli JSON)  ->  transcription  ->  Markdown renderer

Every layer reads and writes ``MessageRecord`` instances. The wacli adapter,
transcriber, archive, and renderer code against this contract rather than raw
wacli JSON, keeping the upstream payload shape at one boundary.

The original wacli object is available transiently in ``MessageRecord.raw`` for
media metadata and adapter evolution. ``ArchiveStore`` deliberately excludes
it from durable persistence.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ChatType = Literal["dm", "group", "channel"]
"""Kind of conversation a message belongs to.

- ``"dm"``: one-to-one direct chat.
- ``"group"``: a multi-participant WhatsApp group.
- ``"channel"``: a WhatsApp channel (broadcast / newsletter).
"""

MessageKind = Literal[
    "text",
    "image",
    "audio",
    "ptt",
    "video",
    "document",
    "sticker",
    "location",
    "system",
    "other",
]
"""Normalized message kind.

``"ptt"`` (push-to-talk) is a WhatsApp voice note and is kept distinct from
``"audio"`` (a regular audio file) because only voice notes are transcribed by
default. ``"system"`` covers protocol/notification messages (joins, leaves,
encryption notices, etc.). Anything unrecognized maps to ``"other"``.
"""


class MessageRecord(BaseModel):
    """A single normalized WhatsApp message.

    This is the canonical unit of data in wa2vault. It is produced by the
    data-access layer from wacli JSON, optionally enriched with a
    :attr:`transcript` by the transcription layer, and consumed by the
    Markdown renderer.

    All timestamps are timezone-aware and normalized to UTC; the renderer is
    responsible for any local-time presentation.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        description="Stable message identifier as reported by wacli (WhatsApp message ID)."
    )
    chat_jid: str = Field(
        description=(
            "JID of the chat this message belongs to (e.g. '123@s.whatsapp.net' or '...@g.us')."
        )
    )
    chat_name: str | None = Field(
        default=None,
        description="Human-readable chat/group/channel name, if known.",
    )
    chat_type: ChatType = Field(
        description="Whether the chat is a direct message, group, or channel."
    )
    timestamp: datetime = Field(
        description="Message timestamp, timezone-aware and normalized to UTC."
    )
    from_me: bool = Field(
        description="True if the message was sent by the linked account (the user)."
    )
    sender_jid: str | None = Field(
        default=None,
        description="JID of the sender. None for some system messages.",
    )
    sender_name: str | None = Field(
        default=None,
        description="Display name (push name / contact name) of the sender, if known.",
    )
    kind: MessageKind = Field(
        description="Normalized message kind. Voice notes are 'ptt'; regular audio is 'audio'."
    )
    text: str | None = Field(
        default=None,
        description="Text body or media caption. None when the message carries no text.",
    )
    media_path: Path | None = Field(
        default=None,
        description="Absolute path to the downloaded media file on disk, if media was located.",
    )
    media_mime: str | None = Field(
        default=None,
        description=(
            "MIME type of the media (e.g. 'audio/ogg; codecs=opus', 'image/jpeg'), if known."
        ),
    )
    media_expired: bool = Field(
        default=False,
        description=(
            "True when this message's media could not be downloaded because it "
            "expired on WhatsApp's CDN (HTTP 404/410). Lets the renderer say the "
            "media is gone rather than show a generic 'unavailable' placeholder."
        ),
    )
    transcript: str | None = Field(
        default=None,
        description=(
            "Local ASR transcript of a voice note / audio. Populated by the transcription layer."
        ),
    )
    reply_to_id: str | None = Field(
        default=None,
        description="If this message is a reply/quote, the id of the quoted message.",
    )
    raw: dict = Field(
        default_factory=dict,
        description="Original wacli JSON object for this message, preserved verbatim.",
    )


__all__ = ["MessageRecord", "ChatType", "MessageKind"]
