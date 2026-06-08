"""The ``pull`` orchestration pipeline (Phase-2 stubs).

This module holds the data-heavy orchestration that backs the ``pull`` CLI
command. The function signatures and contracts are defined here so the CLI can
wire against them now; the bodies are implemented in Phase 2.

Intended end-to-end flow of :func:`pull_chat`:

    1. sync       -- refresh the local wacli store for the target chat
                     (``WacliClient.sync_once`` -> ``wacli sync --once``), and
                     optionally request older history
                     (``wacli history backfill``).
    2. query      -- export the last N days of the chat from wacli
                     (``wacli messages export --chat <jid> --after <date>
                     --json``) and normalize each row into a
                     :class:`~wa2vault.models.MessageRecord`
                     (``resolve_chat`` first maps a chat name -> JID via
                     ``WacliClient.list_chats``).
    3. media      -- locate/download images and voice notes for the window
                     (``wacli media download --chat <jid> --id <id>``), filling
                     ``MessageRecord.media_path`` / ``media_mime``. Skipped when
                     ``download_media`` is False.
    4. transcribe -- for ``ptt``/``audio`` records, run the configured ASR
                     backend (``get_transcriber(config).transcribe(...)``)
                     through a per-message transcript cache under
                     ``config.cache_dir`` so reruns are cheap. Skipped when
                     ``transcribe`` is False.
    5. render     -- render the records to a Markdown note and write it into the
                     vault (``config.vault_dir / config.output_subdir``).

PHASE-2 STUBS: every function below raises ``NotImplementedError``.
"""

from __future__ import annotations

from wa2vault.config import Config
from wa2vault.models import MessageRecord
from wa2vault.wacli import WacliClient


def resolve_chat(client: WacliClient, chat: str) -> dict[str, object]:
    """Resolve a user-supplied chat name or JID to a concrete chat record.

    PHASE-2 STUB.

    Accepts either an exact JID (returned as-is after lookup) or a
    human-readable chat/group/channel name, matched against
    ``client.list_chats()``. Ambiguous or missing names should raise a clear
    error for the CLI to surface.

    Args:
        client: A :class:`~wa2vault.wacli.WacliClient`.
        chat: Chat name or JID provided by the user.

    Returns:
        The matched chat dict (including its JID and display name).
    """
    raise NotImplementedError("Phase 2")


def pull_chat(
    config: Config,
    chat: str,
    days: int,
    transcribe: bool = True,
    download_media: bool = True,
) -> "PullResult":
    """Run the full pull pipeline for one chat and write a vault note.

    PHASE-2 STUB. See the module docstring for the intended flow.

    Args:
        config: Resolved wa2vault configuration.
        chat: Chat name or JID to pull.
        days: Number of days of history to include (window = now - ``days``).
        transcribe: When False, skip voice-note transcription.
        download_media: When False, skip locating/downloading media.

    Returns:
        A :class:`PullResult` summarizing what was written.
    """
    raise NotImplementedError("Phase 2")


def render_markdown(
    config: Config, records: list[MessageRecord], chat: dict[str, object]
) -> str:
    """Render normalized message records to a Markdown note body.

    PHASE-2 STUB.

    Args:
        config: Resolved wa2vault configuration.
        records: Chronologically ordered messages to render.
        chat: The resolved chat record (for the note title/frontmatter).

    Returns:
        The Markdown document as a string.
    """
    raise NotImplementedError("Phase 2")


class PullResult:
    """Summary of a completed :func:`pull_chat` run (Phase-2 shape).

    The concrete fields are finalized in Phase 2 (e.g. note path, message
    count, transcript count, cache hits). Defined here so the CLI can reference
    the return type.
    """
