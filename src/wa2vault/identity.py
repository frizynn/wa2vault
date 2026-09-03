"""Stable, privacy-preserving filesystem identities for profiles and chats."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

_FALLBACK_COMPONENT = "item"


def safe_component(value: str, *, fallback: str = _FALLBACK_COMPONENT) -> str:
    """Return a readable component that cannot escape its parent directory."""
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").lower()
    component = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")
    return component[:80] or fallback


def profile_key(profile: str) -> str:
    """Return the stable directory name for a logical WhatsApp profile."""
    readable = safe_component(profile, fallback="default")
    digest = hashlib.sha256(profile.encode("utf-8")).hexdigest()[:10]
    return f"{readable}--{digest}"


def chat_key(profile: str, chat_jid: str, chat_name: str | None = None) -> str:
    """Return a collision-safe chat name without exposing the complete JID."""
    readable = safe_component(chat_name or "chat", fallback="chat")
    identity = f"{profile}\0{chat_jid}".encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()[:12]
    return f"{readable}--{digest}"


def attachment_name(message_id: str, original_name: str | Path) -> str:
    """Return a collision-safe attachment name scoped to one message."""
    original = Path(str(original_name)).name
    stem = safe_component(Path(original).stem, fallback="attachment")
    suffix = re.sub(r"[^a-zA-Z0-9.]", "", Path(original).suffix)[:16].lower()
    digest = hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:12]
    return f"{digest}--{stem}{suffix}"


__all__ = ["attachment_name", "chat_key", "profile_key", "safe_component"]
