"""Regression tests for the durable, profile-isolated message archive."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from wa2vault.archive import ArchiveStore
from wa2vault.models import MessageRecord

_CHAT_JID = "15550100001@s.whatsapp.net"


def _message(
    message_id: str,
    *,
    minute: int = 0,
    text: str | None = None,
    raw: dict[str, object] | None = None,
) -> MessageRecord:
    return MessageRecord(
        id=message_id,
        chat_jid=_CHAT_JID,
        chat_name="Example contact",
        chat_type="dm",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=minute),
        from_me=False,
        sender_name="Example contact",
        kind="text",
        text=text or message_id,
        raw=raw or {},
    )


def test_archive_isolates_same_chat_and_message_ids_by_profile(tmp_path: Path) -> None:
    store = ArchiveStore(tmp_path / "state")

    store.upsert("personal", _CHAT_JID, [_message("same-id", text="personal")])
    store.upsert("work", _CHAT_JID, [_message("same-id", text="work")])

    assert [item.text for item in store.list("personal", _CHAT_JID)] == ["personal"]
    assert [item.text for item in store.list("work", _CHAT_JID)] == ["work"]


def test_archive_upserts_are_idempotent_and_never_shrink(tmp_path: Path) -> None:
    store = ArchiveStore(tmp_path / "state")
    initial = [_message("m1", minute=1), _message("m2", minute=2), _message("m3", minute=3)]

    assert store.upsert("default", _CHAT_JID, initial) == 3
    assert store.upsert("default", _CHAT_JID, [_message("m3", minute=3)]) == 0
    assert [item.id for item in store.list("default", _CHAT_JID)] == ["m1", "m2", "m3"]

    assert (
        store.upsert(
            "default",
            _CHAT_JID,
            [_message("m3", minute=3), _message("m4", minute=4)],
        )
        == 1
    )
    assert [item.id for item in store.list("default", _CHAT_JID)] == [
        "m1",
        "m2",
        "m3",
        "m4",
    ]


def test_archive_does_not_persist_raw_adapter_payload(tmp_path: Path) -> None:
    store = ArchiveStore(tmp_path / "state")
    marker = "raw-adapter-secret-must-not-be-stored"
    store.upsert(
        "default",
        _CHAT_JID,
        [_message("m1", raw={"AuthToken": marker, "UnusedField": "private"})],
    )

    assert marker.encode() not in store.path.read_bytes()
    restored = store.list("default", _CHAT_JID)
    assert restored[0].raw == {}


def test_archive_preserves_enrichment_when_a_later_window_is_sparse(
    tmp_path: Path,
) -> None:
    store = ArchiveStore(tmp_path / "state")
    enriched = _message("voice", minute=1).model_copy(
        update={"kind": "ptt", "transcript": "cached words"}
    )
    sparse = enriched.model_copy(update={"transcript": None})

    store.upsert("default", _CHAT_JID, [enriched])
    store.upsert("default", _CHAT_JID, [sparse])

    assert store.list("default", _CHAT_JID)[0].transcript == "cached words"
