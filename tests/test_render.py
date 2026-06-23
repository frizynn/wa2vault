"""Tests for the Markdown renderer (:mod:`wa2vault.render`).

These tests pin the rendered note's structure and the note-writing behavior:

- frontmatter keys and the derived counts / date range,
- chronological day grouping (``## YYYY-MM-DD`` headings in order),
- per-kind content rendering (text, image embed + fallback, audio transcript
  blockquote + fallback, reply marker, sender labels),
- :func:`write_note` writing a slugged note into a vault directory.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from wa2vault.models import MessageRecord
from wa2vault.render import render_markdown, write_note


def _record(**overrides: object) -> MessageRecord:
    """Build a :class:`MessageRecord` with sensible defaults for these tests."""
    base: dict[str, object] = {
        "id": "MSG",
        "chat_jid": "123@g.us",
        "chat_type": "group",
        "timestamp": datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
        "from_me": False,
        "kind": "text",
    }
    base.update(overrides)
    return MessageRecord(**base)


def _sample_records() -> list[MessageRecord]:
    """A mixed set of records spanning two days, returned out of order."""
    return [
        # --- Day 2 (intentionally first to prove sorting) ---
        _record(
            id="m5",
            timestamp=datetime(2026, 6, 2, 8, 30, tzinfo=UTC),
            from_me=True,
            kind="ptt",
            transcript="Buenos días a todos",
        ),
        _record(
            id="m6",
            timestamp=datetime(2026, 6, 2, 9, 0, tzinfo=UTC),
            sender_name="Bob",
            kind="audio",
            transcript=None,
        ),
        # --- Day 1 ---
        _record(
            id="m1",
            timestamp=datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
            sender_name="Alice",
            kind="text",
            text="Hola, ¿cómo están?",
        ),
        _record(
            id="m2",
            timestamp=datetime(2026, 6, 1, 10, 5, tzinfo=UTC),
            from_me=True,
            kind="text",
            text="Todo bien",
            reply_to_id="m1",
        ),
        _record(
            id="m3",
            timestamp=datetime(2026, 6, 1, 11, 0, tzinfo=UTC),
            sender_name="Alice",
            kind="image",
            media_path=Path("/vault/Chats/media/photo.jpg"),
            text="miren esto",
        ),
        _record(
            id="m4",
            timestamp=datetime(2026, 6, 1, 11, 30, tzinfo=UTC),
            sender_name="Alice",
            kind="image",
            media_path=None,
        ),
    ]


def _render(records: list[MessageRecord]) -> str:
    return render_markdown(
        records,
        chat_name="Mi Grupo",
        chat_jid="123@g.us",
        chat_type="group",
        days=7,
        generated_at=datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
    )


def test_frontmatter_keys_and_counts() -> None:
    markdown = _render(_sample_records())

    assert markdown.startswith("---\n")
    assert "source: whatsapp" in markdown
    assert 'chat: "Mi Grupo"' in markdown
    assert 'chat_jid: "123@g.us"' in markdown
    assert 'chat_type: "group"' in markdown
    assert "days: 7" in markdown
    assert "message_count: 6" in markdown
    assert "images_count: 2" in markdown
    # Only the ptt with a transcript counts; the audio without one does not.
    assert "audios_transcribed: 1" in markdown
    assert 'date_range: "2026-06-01 → 2026-06-02"' in markdown
    assert 'generated_at: "2026-06-08T12:00:00+00:00"' in markdown
    assert "tags: [whatsapp, chat-archive]" in markdown


def test_title_and_summary() -> None:
    markdown = _render(_sample_records())
    assert "# Mi Grupo" in markdown
    assert "6 messages" in markdown
    assert "2 images" in markdown
    assert "1 audios transcribed" in markdown


def test_day_headings_appear_in_chronological_order() -> None:
    markdown = _render(_sample_records())
    first = markdown.index("## 2026-06-01")
    second = markdown.index("## 2026-06-02")
    assert first < second


def test_messages_sorted_within_and_across_days() -> None:
    markdown = _render(_sample_records())
    order = [
        markdown.index("Hola, ¿cómo están?"),
        markdown.index("Todo bien"),
        markdown.index("miren esto"),
        markdown.index("Buenos días a todos"),
    ]
    assert order == sorted(order)


def test_transcript_renders_as_blockquote() -> None:
    markdown = _render(_sample_records())
    assert "> 🎤 Buenos días a todos" in markdown


def test_audio_without_transcript_falls_back() -> None:
    markdown = _render(_sample_records())
    assert "*(audio sin transcribir)*" in markdown


def test_image_with_media_renders_embed() -> None:
    markdown = _render(_sample_records())
    assert "![[/vault/Chats/media/photo.jpg]]" in markdown
    # Caption follows the embed.
    assert "miren esto" in markdown


def test_image_without_media_falls_back() -> None:
    markdown = _render(_sample_records())
    assert "*(imagen no disponible)*" in markdown


def test_document_with_media_renders_clickable_link() -> None:
    # A document with no caption (text is None after parsing strips wacli's
    # synthetic "Sent document" placeholder) links cleanly.
    record = _record(
        id="doc1",
        kind="document",
        media_path=Path("Chats/_media/grupo/Propuesta.pdf"),
        text=None,
    )
    markdown = _render([record])
    # Documents link with [[...]] so they are clickable/openable in Obsidian.
    assert "[[Chats/_media/grupo/Propuesta.pdf]]" in markdown
    # The generic "*[documento]*" placeholder must not appear when the file is linked.
    assert "*[documento]*" not in markdown


def test_document_with_caption_renders_link_and_caption() -> None:
    record = _record(
        id="doc-cap",
        kind="document",
        media_path=Path("Chats/_media/grupo/factura.pdf"),
        text="acá va la factura",
    )
    markdown = _render([record])
    assert "[[Chats/_media/grupo/factura.pdf]]" in markdown
    assert "acá va la factura" in markdown


def test_document_without_media_falls_back_to_placeholder() -> None:
    record = _record(id="doc2", kind="document")
    markdown = _render([record])
    assert "*[documento]*" in markdown


def test_expired_media_renders_explicit_message() -> None:
    image = _record(id="img-exp", kind="image", media_path=None, media_expired=True)
    audio = _record(id="aud-exp", kind="ptt", media_path=None, media_expired=True)
    markdown = _render([image, audio])
    # Expired media reads as gone, not as a generic "unavailable"/"untranscribed".
    assert markdown.count("*(media expirada en WhatsApp, no se pudo descargar)*") == 2
    assert "*(imagen no disponible)*" not in markdown
    assert "*(audio sin transcribir)*" not in markdown


def test_audio_empty_transcript_is_distinct_from_untranscribed() -> None:
    # transcript="" means ASR ran and produced nothing transcribable; that is a
    # distinct, explicit state from "not transcribed at all" (transcript=None).
    empty = _record(id="aud-empty", kind="ptt", transcript="")
    untried = _record(id="aud-none", kind="ptt", transcript=None)
    markdown = _render([empty, untried])
    assert "*(audio sin contenido transcribible)*" in markdown
    assert "*(audio sin transcribir)*" in markdown


def test_reply_marker_and_sender_labels() -> None:
    markdown = _render(_sample_records())
    # from_me -> "Yo"; reply prefixes the header with the marker.
    assert "↳ **10:05 — Yo**" in markdown
    # sender_name is used when present and not from_me.
    assert "**10:00 — Alice**" in markdown


def test_empty_records_render_safely() -> None:
    markdown = _render([])
    assert "message_count: 0" in markdown
    assert "date_range: null" in markdown
    assert "no messages" in markdown
    assert "# Mi Grupo" in markdown


def test_render_is_deterministic() -> None:
    records = _sample_records()
    assert _render(records) == _render(records)


def test_write_note_creates_file(tmp_path: Path) -> None:
    markdown = _render(_sample_records())
    vault_dir = tmp_path / "vault"

    path = write_note(
        markdown,
        vault_dir=vault_dir,
        output_subdir="Chats",
        chat_name="Mi Grupo",
    )

    assert path == vault_dir / "Chats" / "mi-grupo.md"
    assert path.exists()
    assert path.read_text(encoding="utf-8") == markdown


def test_write_note_overwrites_and_slugifies_unsafe_names(tmp_path: Path) -> None:
    vault_dir = tmp_path / "vault"
    path = write_note(
        "first",
        vault_dir=vault_dir,
        output_subdir="Chats",
        chat_name="Família / Año 2026!",
    )
    # Path separators and accents are stripped; the name stays readable.
    assert path.name == "familia-ano-2026.md"

    path_again = write_note(
        "second",
        vault_dir=vault_dir,
        output_subdir="Chats",
        chat_name="Família / Año 2026!",
    )
    assert path_again == path
    assert path.read_text(encoding="utf-8") == "second"
