"""Local contact book: a JSON-backed ``jid -> name`` map.

WhatsApp's app-state contact-name sync can fail on a linked device, leaving 1:1
(DM) chats showing only a phone number (e.g. ``15550100000@s.whatsapp.net``)
instead of a person's name. This module provides a small, local override the
user controls directly: save ``number -> name`` once, then list and pull chats
by that friendly name.

The store is a flat JSON object ``{jid: name}`` persisted atomically next to the
config file (see :func:`wa2vault.config.default_contacts_file`). It is purely a
display/resolution aid; it never talks to WhatsApp or wacli.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from wa2vault.fs import atomic_write_text

#: Server suffix of a WhatsApp direct-message JID.
_DM_SERVER = "s.whatsapp.net"

#: Matches a DM JID of the form ``<digits>@s.whatsapp.net``.
_DM_JID_RE = re.compile(r"^(\d+)@s\.whatsapp\.net$")


def normalize_jid(raw: str) -> str:
    """Normalize a phone number or JID into a canonical WhatsApp JID.

    Accepts a phone number in any human format ("+1 555 010-0000",
    "15550100000", "1 555 010 0000") or a full JID
    ("1555...@s.whatsapp.net", "...@g.us"). If ``raw`` already contains ``@`` it
    is treated as a JID and returned with surrounding whitespace stripped and
    lowercased. Otherwise every non-digit character is removed and the digits
    are wrapped as ``<digits>@s.whatsapp.net``.

    Args:
        raw: A phone number (any format) or a full JID.

    Returns:
        The canonical JID.

    Raises:
        ValueError: If ``raw`` is empty/whitespace, or contains no digits and is
            not a JID.
    """
    text = raw.strip()
    if not text:
        raise ValueError("Empty contact number/JID")

    if "@" in text:
        return text.lower()

    digits = re.sub(r"\D", "", text)
    if not digits:
        raise ValueError(f"No digits found in contact number {raw!r}")
    return f"{digits}@{_DM_SERVER}"


def pretty_phone(jid: str) -> str:
    """Return a readable phone string for a DM JID.

    For a DM JID ``<digits>@s.whatsapp.net`` returns ``"+<digits>"``. Any other
    JID (groups, channels, malformed) is returned unchanged. Best-effort only:
    no locale formatting, just a leading ``+`` on the digit user-part.

    Args:
        jid: A WhatsApp JID.

    Returns:
        ``"+<digits>"`` for DM JIDs, otherwise ``jid`` unchanged.
    """
    match = _DM_JID_RE.match(jid)
    if match:
        return f"+{match.group(1)}"
    return jid


class ContactBook:
    """A JSON-backed local map of ``jid -> name``.

    The book tolerates a missing or corrupt store file by starting empty, so a
    first run (or a hand-edited file gone bad) never crashes the CLI. All
    mutations persist immediately and atomically.

    Args:
        path: Path to the JSON store file. Its parent directory is created on
            the first write.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._entries: dict[str, str] = self._load()

    def _load(self) -> dict[str, str]:
        """Load the ``{jid: name}`` map, tolerating a missing/corrupt file."""
        try:
            text = self._path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return {}
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            str(jid): str(name)
            for jid, name in data.items()
            if isinstance(jid, str) and isinstance(name, str)
        }

    def _save(self) -> None:
        """Persist the map atomically (temp file + ``os.replace``)."""
        payload = json.dumps(self._entries, ensure_ascii=False, indent=2, sort_keys=True)
        atomic_write_text(self._path, payload + "\n")

    def set(self, number_or_jid: str, name: str) -> str:
        """Save ``name`` for the given number or JID and persist.

        Args:
            number_or_jid: A phone number (any format) or a full JID.
            name: The friendly name to store (surrounding whitespace stripped).

        Returns:
            The canonical JID the name was stored under.

        Raises:
            ValueError: If ``number_or_jid`` cannot be normalized, or ``name`` is
                empty after stripping.
        """
        jid = normalize_jid(number_or_jid)
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Contact name must not be empty")
        self._entries[jid] = clean_name
        self._save()
        return jid

    def remove(self, query: str) -> bool:
        """Remove an entry by JID/number or by exact (case-insensitive) name.

        Args:
            query: A phone number, a JID, or a stored name.

        Returns:
            True if an entry was removed (and the change persisted), else False.
        """
        to_delete: set[str] = set()

        try:
            jid = normalize_jid(query)
        except ValueError:
            jid = None
        if jid is not None and jid in self._entries:
            to_delete.add(jid)

        lowered = query.strip().lower()
        for stored_jid, stored_name in self._entries.items():
            if stored_name.lower() == lowered:
                to_delete.add(stored_jid)

        if not to_delete:
            return False

        for stored_jid in to_delete:
            del self._entries[stored_jid]
        self._save()
        return True

    def name_for(self, jid: str) -> str | None:
        """Return the stored name for an exact JID, or None."""
        return self._entries.get(jid)

    def find(self, query: str) -> str | None:
        """Resolve a query to a stored JID, by name or by JID/number.

        Resolution order:

        1. If ``query`` normalizes to a JID present in the book, return it.
        2. Case-insensitive *exact* name match.
        3. Case-insensitive *substring* name match, only if it is unique.

        Args:
            query: A stored name (full or partial), a phone number, or a JID.

        Returns:
            The matching JID, or None if nothing matched or a substring match was
            ambiguous (more than one).
        """
        try:
            jid = normalize_jid(query)
        except ValueError:
            jid = None
        if jid is not None and jid in self._entries:
            return jid

        lowered = query.strip().lower()
        if not lowered:
            return None

        exact = [j for j, name in self._entries.items() if name.lower() == lowered]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            return None

        partial = [j for j, name in self._entries.items() if lowered in name.lower()]
        if len(partial) == 1:
            return partial[0]
        return None

    def items(self) -> dict[str, str]:
        """Return a copy of the ``{jid: name}`` map."""
        return dict(self._entries)


__all__ = ["ContactBook", "normalize_jid", "pretty_phone"]
