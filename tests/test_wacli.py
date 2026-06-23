"""Unit tests for the wacli data-access layer.

These tests exercise the pure JSON -> :class:`MessageRecord` mapper and the
helpers around it with hand-crafted fixture dicts that mirror wacli v0.11.0's
real ``messages export`` payload (PascalCase keys from Go's ``store.Message``,
RFC3339 timestamps). They never invoke the wacli binary, so they run without a
paired WhatsApp session.

The fixtures encode the schema assumptions documented in
``src/wa2vault/wacli.py``; if a post-pairing capture shows wacli emits different
keys, update both the fixtures and the parser.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from wa2vault import wacli as wacli_module
from wa2vault.config import Config
from wa2vault.models import MessageRecord
from wa2vault.wacli import (
    ChatNotFound,
    ChatNotUnique,
    ChatRef,
    MediaResult,
    WacliClient,
    WacliError,
)


# --------------------------------------------------------------------------- #
# Fixtures mirroring wacli's real `messages export` JSON (store.Message shape).
# --------------------------------------------------------------------------- #
def _text_message() -> dict:
    return {
        "ChatJID": "5491111111111@s.whatsapp.net",
        "ChatName": "Alice",
        "MsgID": "TEXT0001",
        "SenderJID": "5491111111111@s.whatsapp.net",
        "SenderName": "Alice",
        "Timestamp": "2026-06-01T15:30:00Z",
        "FromMe": False,
        "Text": "hola, cómo va?",
        "DisplayText": "hola, cómo va?",
        "MediaType": "",
        "MimeType": "",
    }


def _image_message() -> dict:
    return {
        "ChatJID": "120363000000000000@g.us",
        "ChatName": "Familia",
        "MsgID": "IMG0002",
        "SenderJID": "5492222222222@s.whatsapp.net",
        "SenderName": "Bob",
        "Timestamp": "2026-06-02T09:00:00+00:00",
        "FromMe": True,
        "Text": "",
        "DisplayText": "",
        "MediaType": "image",
        "MediaCaption": "mirá esta foto",
        "MimeType": "image/jpeg",
        "Filename": "IMG-20260602.jpg",
    }


def _ptt_message() -> dict:
    return {
        "ChatJID": "5491111111111@s.whatsapp.net",
        "ChatName": "Alice",
        "MsgID": "PTT0003",
        "SenderJID": "5491111111111@s.whatsapp.net",
        "SenderName": "Alice",
        "Timestamp": "2026-06-03T18:45:10Z",
        "FromMe": False,
        "MediaType": "ptt",
        "MimeType": "audio/ogg; codecs=opus",
        "QuotedMsgID": "TEXT0001",
    }


def _client() -> WacliClient:
    return WacliClient(Config(wacli_db=None))


# --------------------------------------------------------------------------- #
# _map_kind
# --------------------------------------------------------------------------- #
def test_map_kind_text_when_no_media() -> None:
    assert WacliClient._map_kind("", None, "hello") == "text"


def test_map_kind_system_when_no_media_and_no_text() -> None:
    assert WacliClient._map_kind("", None, None) == "system"


def test_map_kind_image() -> None:
    assert WacliClient._map_kind("image", "image/jpeg", None) == "image"


def test_map_kind_ptt_explicit() -> None:
    assert WacliClient._map_kind("ptt", "audio/ogg; codecs=opus", None) == "ptt"


def test_map_kind_audio_with_opus_mime_is_ptt() -> None:
    # WhatsApp voice notes sometimes arrive as generic "audio" with an Opus
    # mime; they must be treated as PTT so they are transcribed.
    assert WacliClient._map_kind("audio", "audio/ogg; codecs=opus", None) == "ptt"


def test_map_kind_regular_audio_stays_audio() -> None:
    assert WacliClient._map_kind("audio", "audio/mpeg", None) == "audio"


def test_map_kind_gif_maps_to_video() -> None:
    assert WacliClient._map_kind("gif", "video/mp4", None) == "video"


def test_map_kind_unknown_media_is_other() -> None:
    assert WacliClient._map_kind("contact", "text/vcard", None) == "other"


# --------------------------------------------------------------------------- #
# _parse_message field mapping
# --------------------------------------------------------------------------- #
def test_parse_text_message_field_mapping() -> None:
    record = WacliClient._parse_message(_text_message())
    assert isinstance(record, MessageRecord)
    assert record.id == "TEXT0001"
    assert record.chat_jid == "5491111111111@s.whatsapp.net"
    assert record.chat_name == "Alice"
    assert record.chat_type == "dm"
    assert record.from_me is False
    assert record.sender_jid == "5491111111111@s.whatsapp.net"
    assert record.sender_name == "Alice"
    assert record.kind == "text"
    assert record.text == "hola, cómo va?"
    assert record.media_path is None
    assert record.media_mime is None
    assert record.reply_to_id is None


def test_parse_timestamp_is_utc_aware() -> None:
    record = WacliClient._parse_message(_text_message())
    assert record.timestamp == datetime(2026, 6, 1, 15, 30, tzinfo=UTC)
    assert record.timestamp.tzinfo is not None
    assert record.timestamp.utcoffset() == timedelta(0)


def test_parse_timestamp_with_offset_normalized_to_utc() -> None:
    row = _text_message()
    row["Timestamp"] = "2026-06-01T12:30:00-03:00"  # ART
    record = WacliClient._parse_message(row)
    assert record is not None
    assert record.timestamp == datetime(2026, 6, 1, 15, 30, tzinfo=UTC)
    assert record.timestamp.tzinfo == UTC


def test_parse_timestamp_from_unix_epoch() -> None:
    row = _text_message()
    row["Timestamp"] = 1_748_792_000  # arbitrary epoch seconds
    record = WacliClient._parse_message(row)
    assert record is not None
    assert record.timestamp == datetime.fromtimestamp(1_748_792_000, tz=UTC)


def test_parse_image_message_maps_kind_and_caption() -> None:
    record = WacliClient._parse_message(_image_message())
    assert record is not None
    assert record.kind == "image"
    assert record.chat_type == "group"
    assert record.from_me is True
    # DisplayText/Text are empty, so the caption becomes the text.
    assert record.text == "mirá esta foto"
    assert record.media_mime == "image/jpeg"


def test_parse_ptt_message_maps_kind_and_reply() -> None:
    record = WacliClient._parse_message(_ptt_message())
    assert record is not None
    assert record.kind == "ptt"
    assert record.media_mime == "audio/ogg; codecs=opus"
    assert record.reply_to_id == "TEXT0001"
    assert record.text is None


def test_parse_document_strips_placeholder_display_text() -> None:
    # wacli emits DisplayText "Sent document" as a synthetic placeholder; with no
    # real MediaCaption the parsed text must be None so it never leaks into the
    # note as a fake caption.
    row = {
        "ChatJID": "120363000000000000@g.us",
        "MsgID": "DOC0009",
        "SenderName": "Alex",
        "Timestamp": "2026-06-17T18:54:39Z",
        "FromMe": False,
        "Text": "",
        "DisplayText": "Sent document",
        "MediaType": "document",
        "MediaCaption": "",
        "Filename": "Propuesta.pdf",
        "MimeType": "application/pdf",
    }
    record = WacliClient._parse_message(row)
    assert record is not None
    assert record.kind == "document"
    assert record.text is None
    assert record.raw["Filename"] == "Propuesta.pdf"


def test_parse_document_keeps_real_caption() -> None:
    row = {
        "ChatJID": "120363000000000000@g.us",
        "MsgID": "DOC0010",
        "Timestamp": "2026-06-17T18:54:39Z",
        "FromMe": True,
        "Text": "",
        "DisplayText": "Sent document",
        "MediaType": "document",
        "MediaCaption": "acá va la factura",
        "Filename": "factura.pdf",
        "MimeType": "application/pdf",
    }
    record = WacliClient._parse_message(row)
    assert record is not None
    assert record.text == "acá va la factura"


def test_parse_message_preserves_raw_verbatim() -> None:
    row = _ptt_message()
    record = WacliClient._parse_message(row)
    assert record is not None
    assert record.raw == row
    # Keys not represented elsewhere are still recoverable from raw.
    assert record.raw["MimeType"] == "audio/ogg; codecs=opus"


def test_parse_message_skips_rows_without_id() -> None:
    row = _text_message()
    del row["MsgID"]
    assert WacliClient._parse_message(row) is None


def test_parse_message_skips_rows_without_timestamp() -> None:
    row = _text_message()
    row["Timestamp"] = "not-a-date"
    assert WacliClient._parse_message(row) is None


def test_parse_message_tolerates_missing_optional_fields() -> None:
    # A minimal row should still parse without crashing.
    row = {
        "MsgID": "MIN0001",
        "ChatJID": "5493333333333@s.whatsapp.net",
        "Timestamp": "2026-06-04T00:00:00Z",
    }
    record = WacliClient._parse_message(row)
    assert record is not None
    assert record.sender_jid is None
    assert record.sender_name is None
    assert record.kind == "system"  # no media, no text


def test_parse_message_accepts_local_path_when_file_exists(tmp_path: Path) -> None:
    media = tmp_path / "IMG-20260602.jpg"
    media.write_bytes(b"\xff\xd8\xff")  # tiny JPEG marker
    row = _image_message()
    row["LocalPath"] = str(media)
    record = WacliClient._parse_message(row)
    assert record is not None
    assert record.media_path == media


def test_parse_message_drops_local_path_when_file_missing() -> None:
    row = _image_message()
    row["LocalPath"] = "/nonexistent/path/IMG.jpg"
    record = WacliClient._parse_message(row)
    assert record is not None
    assert record.media_path is None


# --------------------------------------------------------------------------- #
# export_messages: extraction + time-window filtering (no subprocess).
# --------------------------------------------------------------------------- #
def test_export_messages_extracts_and_filters_window(monkeypatch) -> None:
    payload = {
        "fts": True,
        "messages": [_text_message(), _image_message(), _ptt_message()],
    }
    client = _client()
    monkeypatch.setattr(client, "run_json", lambda *a, **k: payload)

    records = client.export_messages(
        "5491111111111@s.whatsapp.net",
        since=datetime(2026, 6, 3, tzinfo=UTC),
    )
    # Only the PTT message (2026-06-03) is on/after the `since` bound.
    assert [r.id for r in records] == ["PTT0003"]


def test_export_messages_handles_null_messages(monkeypatch) -> None:
    client = _client()
    monkeypatch.setattr(client, "run_json", lambda *a, **k: {"messages": None, "fts": True})
    records = client.export_messages(
        "x@s.whatsapp.net", since=datetime(2020, 1, 1, tzinfo=UTC)
    )
    assert records == []


def test_export_messages_naive_since_assumed_utc(monkeypatch) -> None:
    payload = {"messages": [_text_message()], "fts": True}
    client = _client()
    monkeypatch.setattr(client, "run_json", lambda *a, **k: payload)
    # Naive `since` before the message time -> message is included.
    records = client.export_messages(
        "5491111111111@s.whatsapp.net", since=datetime(2026, 6, 1, 0, 0)
    )
    assert [r.id for r in records] == ["TEXT0001"]


def test_export_messages_until_is_exclusive(monkeypatch) -> None:
    payload = {"messages": [_text_message()], "fts": True}
    client = _client()
    monkeypatch.setattr(client, "run_json", lambda *a, **k: payload)
    records = client.export_messages(
        "5491111111111@s.whatsapp.net",
        since=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
        until=datetime(2026, 6, 1, 15, 30, tzinfo=UTC),  # == message ts
    )
    assert records == []  # exclusive upper bound drops the equal timestamp


# --------------------------------------------------------------------------- #
# resolve_chat
# --------------------------------------------------------------------------- #
_CHATS = [
    {"jid": "5491111111111@s.whatsapp.net", "kind": "dm", "name": "Alice"},
    {"jid": "120363000000000000@g.us", "kind": "group", "name": "Familia"},
    {"jid": "120363999999999999@g.us", "kind": "group", "name": "Familia Extendida"},
    {"jid": "777@newsletter", "kind": "newsletter", "name": "Noticias"},
]


def _client_with_chats(monkeypatch, chats: list[dict]) -> WacliClient:
    client = _client()
    monkeypatch.setattr(client, "list_chats", lambda *a, **k: chats)
    # Stub the group-name lookup so name-resolution tests never shell out to the
    # wacli binary; tests that exercise the group-subject backfill stub it with
    # their own mapping.
    monkeypatch.setattr(client, "group_names", lambda *a, **k: {})
    return client


def test_resolve_chat_by_jid(monkeypatch) -> None:
    client = _client_with_chats(monkeypatch, _CHATS)
    ref = client.resolve_chat("120363000000000000@g.us")
    assert ref == ChatRef(
        jid="120363000000000000@g.us", name="Familia", chat_type="group"
    )


def test_resolve_chat_exact_name_case_insensitive(monkeypatch) -> None:
    client = _client_with_chats(monkeypatch, _CHATS)
    ref = client.resolve_chat("alice")
    assert ref.jid == "5491111111111@s.whatsapp.net"
    assert ref.chat_type == "dm"


def test_resolve_chat_exact_name_wins_over_substring(monkeypatch) -> None:
    # "Familia" is an exact match AND a substring of "Familia Extendida";
    # the exact match must win and not be flagged ambiguous.
    client = _client_with_chats(monkeypatch, _CHATS)
    ref = client.resolve_chat("Familia")
    assert ref.jid == "120363000000000000@g.us"


def test_resolve_chat_unique_substring(monkeypatch) -> None:
    client = _client_with_chats(monkeypatch, _CHATS)
    ref = client.resolve_chat("Noti")
    assert ref.jid == "777@newsletter"
    assert ref.chat_type == "channel"


def test_resolve_chat_ambiguous_substring_raises(monkeypatch) -> None:
    # "Famil" is a substring of both "Familia" and "Familia Extendida" and an
    # exact match of neither, so resolution is ambiguous.
    client = _client_with_chats(monkeypatch, _CHATS)
    with pytest.raises(ChatNotUnique) as excinfo:
        client.resolve_chat("Famil")
    assert len(excinfo.value.candidates) == 2
    assert excinfo.value.query == "Famil"


def test_resolve_chat_not_found_raises(monkeypatch) -> None:
    client = _client_with_chats(monkeypatch, _CHATS)
    with pytest.raises(ChatNotFound):
        client.resolve_chat("Nonexistent")


def test_resolve_chat_empty_query_raises(monkeypatch) -> None:
    client = _client_with_chats(monkeypatch, _CHATS)
    with pytest.raises(ChatNotFound):
        client.resolve_chat("   ")


# --------------------------------------------------------------------------- #
# groups: list_groups / group_names / refresh_groups
# --------------------------------------------------------------------------- #
def test_list_groups_passthrough_list(monkeypatch) -> None:
    rows = [{"JID": "120363000000000000@g.us", "Name": "Mi Grupo"}]
    client = _client()
    monkeypatch.setattr(client, "run_json", lambda *a, **k: rows)
    assert client.list_groups() == rows


def test_list_groups_null_data_is_empty(monkeypatch) -> None:
    client = _client()
    monkeypatch.setattr(client, "run_json", lambda *a, **k: None)
    assert client.list_groups() == []


def test_list_groups_dict_with_groups_key(monkeypatch) -> None:
    client = _client()
    monkeypatch.setattr(
        client, "run_json", lambda *a, **k: {"groups": [{"JID": "1@g.us"}]}
    )
    assert client.list_groups() == [{"JID": "1@g.us"}]


def test_list_groups_bad_shape_raises(monkeypatch) -> None:
    client = _client()
    monkeypatch.setattr(client, "run_json", lambda *a, **k: 42)
    with pytest.raises(WacliError, match="groups list"):
        client.list_groups()


def test_group_names_maps_jid_to_subject_and_skips_placeholders(monkeypatch) -> None:
    groups = [
        {"JID": "120363000000000000@g.us", "Name": "Mi Grupo"},
        # Name echoes the JID (app-state sync never delivered a real subject).
        {"JID": "120363111111111111@g.us", "Name": "120363111111111111@g.us"},
        {"JID": "120363222222222222@g.us", "Name": ""},  # empty name
        {"Name": "no jid"},  # unusable (no JID)
    ]
    client = _client()
    monkeypatch.setattr(client, "list_groups", lambda *a, **k: groups)
    assert client.group_names() == {"120363000000000000@g.us": "Mi Grupo"}


def test_refresh_groups_runs_with_guard_off_and_summarizes(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_json(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"groups": 5, "store": {"groups": 5}}

    client = _client()
    monkeypatch.setattr(client, "run_json", fake_run_json)
    summary = client.refresh_groups(timeout=7.5)

    assert captured["args"] == ("groups", "refresh")
    # refresh writes the local store, so the read-only guard must be OFF.
    assert captured["kwargs"]["read_only"] is False
    assert captured["kwargs"]["timeout"] == 7.5
    assert summary["ok"] is True
    assert summary["groups"] == 5
    assert summary["store"] == {"groups": 5}


def test_is_placeholder_group_detection() -> None:
    assert wacli_module._is_placeholder_group("1@g.us", "1@g.us") is True
    assert wacli_module._is_placeholder_group("1@g.us", None) is True
    assert wacli_module._is_placeholder_group("1@g.us", "") is True
    assert wacli_module._is_placeholder_group("1@g.us", "Real Name") is False
    # Only groups (@g.us) are ever treated as placeholder here.
    assert wacli_module._is_placeholder_group("1@s.whatsapp.net", "1@s.whatsapp.net") is False


# --------------------------------------------------------------------------- #
# resolve_chat: group-name backfill from the group table
# --------------------------------------------------------------------------- #
def test_resolve_chat_backfills_group_name(monkeypatch) -> None:
    # `chats list` reports the bare JID as the name (app-state sync failed)...
    chats = [
        {
            "jid": "120363000000000000@g.us",
            "kind": "group",
            "name": "120363000000000000@g.us",
        }
    ]
    client = _client()
    monkeypatch.setattr(client, "list_chats", lambda *a, **k: chats)
    # ...but the group table has the real subject.
    monkeypatch.setattr(
        client, "group_names", lambda *a, **k: {"120363000000000000@g.us": "Mi Grupo"}
    )

    ref = client.resolve_chat("Mi Grupo")
    assert ref.jid == "120363000000000000@g.us"
    assert ref.name == "Mi Grupo"
    assert ref.chat_type == "group"


def test_resolve_chat_group_subject_overrides_participant_name(monkeypatch) -> None:
    # `chats list` names the group after a *participant* (a pushname leaking
    # into the chat-list name), not the real subject. The group table's subject
    # must win, so the group resolves by its real name.
    chats = [
        {"jid": "120363999999999999@g.us", "kind": "group", "name": "Pat Lee"},
        {"jid": "5491100000000@s.whatsapp.net", "kind": "dm", "name": "Pat Lee"},
    ]
    client = _client()
    monkeypatch.setattr(client, "list_chats", lambda *a, **k: chats)
    monkeypatch.setattr(
        client,
        "group_names",
        lambda *a, **k: {"120363999999999999@g.us": "Proyecto Alfa"},
    )

    ref = client.resolve_chat("Proyecto Alfa")
    assert ref.jid == "120363999999999999@g.us"
    assert ref.name == "Proyecto Alfa"
    assert ref.chat_type == "group"

    # And the participant DM still resolves under its own name, no longer
    # colliding with the group (whose name is now the subject).
    dm = client.resolve_chat("Pat Lee")
    assert dm.jid == "5491100000000@s.whatsapp.net"


def test_resolve_chat_skips_group_lookup_when_no_groups(monkeypatch) -> None:
    # No group rows at all, so the group lookup must not run.
    chats = [{"jid": "5491111111111@s.whatsapp.net", "kind": "dm", "name": "Alice"}]
    client = _client()
    monkeypatch.setattr(client, "list_chats", lambda *a, **k: chats)

    def boom(*a, **k):
        raise AssertionError("group_names should not be called when there are no groups")

    monkeypatch.setattr(client, "group_names", boom)
    ref = client.resolve_chat("Alice")
    assert ref.jid == "5491111111111@s.whatsapp.net"


def test_resolve_chat_backfill_is_best_effort(monkeypatch) -> None:
    chats = [
        {
            "jid": "120363000000000000@g.us",
            "kind": "group",
            "name": "120363000000000000@g.us",
        }
    ]
    client = _client()
    monkeypatch.setattr(client, "list_chats", lambda *a, **k: chats)

    def boom(*a, **k):
        raise WacliError("group lookup failed")

    monkeypatch.setattr(client, "group_names", boom)
    # The JID still resolves even though the name backfill failed.
    ref = client.resolve_chat("120363000000000000@g.us")
    assert ref.jid == "120363000000000000@g.us"


# --------------------------------------------------------------------------- #
# sync_once summary
# --------------------------------------------------------------------------- #
def test_sync_once_summarizes_counts(monkeypatch) -> None:
    payload = {
        "synced": 42,
        "new_messages": 7,
        "store": {"messages": 1000, "chats": 12},
        "nested": {"ignored": True},  # non-scalar, not a 'store' -> dropped
    }
    client = _client()
    monkeypatch.setattr(client, "run_json", lambda *a, **k: payload)
    summary = client.sync_once()
    assert summary["ok"] is True
    assert summary["synced"] == 42
    assert summary["new_messages"] == 7
    assert summary["store"] == {"messages": 1000, "chats": 12}
    assert "nested" not in summary


def test_sync_once_non_dict_payload(monkeypatch) -> None:
    client = _client()
    monkeypatch.setattr(client, "run_json", lambda *a, **k: None)
    assert client.sync_once() == {"ok": True}


def test_sync_once_forwards_timeout(monkeypatch) -> None:
    """``sync_once(timeout=...)`` threads the bound into the wacli invocation."""
    captured: dict[str, object] = {}

    def fake_run_json(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"ok": True}

    client = _client()
    monkeypatch.setattr(client, "run_json", fake_run_json)
    client.sync_once(timeout=12.5)

    assert captured["args"] == ("sync", "--once")
    assert captured["kwargs"]["timeout"] == 12.5
    # sync must run with the read-only guard OFF (it writes the local store).
    assert captured["kwargs"]["read_only"] is False


def test_run_json_timeout_raises_wacli_error(monkeypatch) -> None:
    """A subprocess timeout becomes a WacliError naming the bound."""
    client = _client()

    # ``run_json`` first calls ``ensure_available`` (``shutil.which`` +
    # ``os.path.exists``). Neutralize that check so the test exercises the
    # timeout path regardless of whether ``wacli`` is installed (it is not on
    # CI). Pointing ``which`` at a path short-circuits the ``and`` in
    # ``ensure_available``, so ``os.path.exists`` is never consulted.
    monkeypatch.setattr(wacli_module.shutil, "which", lambda _name: "/usr/bin/wacli")

    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="wacli sync --once", timeout=90)

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(WacliError, match="timed out after 90s"):
        client.run_json("sync", "--once", read_only=False, timeout=90)


# --------------------------------------------------------------------------- #
# ensure_media (no subprocess; run_json stubbed)
# --------------------------------------------------------------------------- #
def _media_record(tmp_path: Path, **overrides) -> MessageRecord:
    base = dict(
        id="IMG0002",
        chat_jid="120363000000000000@g.us",
        chat_type="group",
        timestamp=datetime(2026, 6, 2, 9, 0, tzinfo=UTC),
        from_me=False,
        kind="image",
        media_mime="image/jpeg",
        raw={"Filename": "IMG-20260602.jpg"},
    )
    base.update(overrides)
    return MessageRecord(**base)


def test_ensure_media_returns_existing_path(tmp_path: Path) -> None:
    existing = tmp_path / "already.jpg"
    existing.write_bytes(b"x")
    record = _media_record(tmp_path, media_path=existing)
    client = _client()
    assert client.ensure_media(record) == MediaResult(path=existing)


def test_ensure_media_returns_empty_for_text(tmp_path: Path) -> None:
    record = MessageRecord(
        id="T1",
        chat_jid="x@s.whatsapp.net",
        chat_type="dm",
        timestamp=datetime(2026, 6, 1, tzinfo=UTC),
        from_me=False,
        kind="text",
        text="hi",
    )
    assert _client().ensure_media(record) == MediaResult(path=None)


def test_ensure_media_downloads_and_returns_path(tmp_path: Path, monkeypatch) -> None:
    cache = tmp_path / "cache"
    client = WacliClient(Config(cache_dir=cache))
    record = _media_record(tmp_path)

    def fake_run_json(*args, **kwargs):
        # The output path is the flag right after "--output".
        out = Path(args[args.index("--output") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\xff\xd8\xff")
        return {"path": str(out), "bytes": 3, "downloaded": True, "read_only": True}

    monkeypatch.setattr(client, "run_json", fake_run_json)
    result = client.ensure_media(record)
    assert result.path is not None
    assert result.path.exists()
    assert result.path.suffix == ".jpg"
    assert result.expired is False


def test_ensure_media_downloads_document(tmp_path: Path, monkeypatch) -> None:
    """A document attachment is downloaded just like an image."""
    client = WacliClient(Config(cache_dir=tmp_path / "cache"))
    record = _media_record(
        tmp_path,
        id="DOC0001",
        kind="document",
        media_mime="application/pdf",
        raw={"Filename": "Propuesta.pdf"},
    )

    def fake_run_json(*args, **kwargs):
        out = Path(args[args.index("--output") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"%PDF-1.4")
        return {"path": str(out), "bytes": 7, "downloaded": True, "read_only": True}

    monkeypatch.setattr(client, "run_json", fake_run_json)
    result = client.ensure_media(record)
    assert result.path is not None
    assert result.path.exists()
    assert result.path.suffix == ".pdf"


def test_ensure_media_expired_is_flagged(tmp_path: Path, monkeypatch) -> None:
    client = WacliClient(Config(cache_dir=tmp_path / "cache"))
    record = _media_record(tmp_path)

    def boom(*args, **kwargs):
        raise WacliError("download failed with status code 410")

    monkeypatch.setattr(client, "run_json", boom)
    assert client.ensure_media(record) == MediaResult(path=None, expired=True)


def test_ensure_media_non_expiry_error_is_not_flagged(tmp_path: Path, monkeypatch) -> None:
    """A download error that is not a CDN-expiry (404/410) is not flagged expired."""
    client = WacliClient(Config(cache_dir=tmp_path / "cache"))
    record = _media_record(tmp_path)

    def boom(*args, **kwargs):
        raise WacliError("download failed with status code 500")

    monkeypatch.setattr(client, "run_json", boom)
    assert client.ensure_media(record) == MediaResult(path=None, expired=False)


# --------------------------------------------------------------------------- #
# Live-session test placeholder (must not require a paired wacli).
# --------------------------------------------------------------------------- #
@pytest.mark.skip(reason="requires a live, paired wacli session and real data")
def test_export_messages_against_live_wacli() -> None:  # pragma: no cover
    client = WacliClient(Config())
    client.export_messages(
        "live-chat@s.whatsapp.net",
        since=datetime.now(tz=timezone.utc) - timedelta(days=1),
    )
