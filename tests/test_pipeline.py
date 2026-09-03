"""Integration tests for the ``pull`` pipeline (:mod:`wa2vault.pipeline`).

These tests exercise :func:`wa2vault.pipeline.pull_chat` end-to-end against a
fake :class:`~wa2vault.wacli.WacliClient` stand-in, so they require neither a
live WhatsApp link nor a heavy ASR model. Transcription is disabled
(``transcribe=False``) to keep the tests offline and fast; the media-copy and
rendering paths are fully exercised.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from wa2vault import pipeline
from wa2vault.config import Config
from wa2vault.lock import StoreLock
from wa2vault.models import MessageRecord
from wa2vault.wacli import ChatRef, MediaResult, WacliError


class FakeWacliClient:
    """Stand-in for :class:`~wa2vault.wacli.WacliClient`.

    Returns hand-built data instead of shelling out to ``wacli``. The image
    record's local file is looked up in ``media_files`` (keyed by message id),
    mirroring :meth:`WacliClient.ensure_media`.
    """

    def __init__(
        self,
        *,
        chatref: ChatRef,
        records: list[MessageRecord],
        media_files: dict[str, Path],
        expired_media: set[str] | None = None,
    ) -> None:
        self._chatref = chatref
        self._records = records
        self._media_files = media_files
        self._expired_media = expired_media or set()
        self.sync_calls = 0
        self.sync_timeout: float | None = None
        self.sync_error: Exception | None = None

    def __call__(self, config: Config) -> "FakeWacliClient":
        # ``pipeline.WacliClient(config)`` is monkeypatched to this instance;
        # being callable lets it act as the constructor too.
        return self

    def sync_once(self, *, full: bool = False, timeout: float | None = None) -> dict[str, object]:
        self.sync_calls += 1
        self.sync_timeout = timeout
        if self.sync_error is not None:
            raise self.sync_error
        return {"ok": True}

    def resolve_chat(self, query: str) -> ChatRef:
        return self._chatref

    def export_messages(
        self,
        chat_jid: str,
        *,
        since: datetime,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[MessageRecord]:
        return self._records

    def ensure_media(self, record: MessageRecord) -> MediaResult:
        path = self._media_files.get(record.id)
        if path is not None:
            return MediaResult(path=path)
        return MediaResult(path=None, expired=record.id in self._expired_media)


def _build_records(image_local: Path) -> list[MessageRecord]:
    """Build a small conversation: two texts, one image, one voice note."""
    base = datetime(2026, 6, 8, 9, 0, tzinfo=UTC)
    common = {
        "chat_jid": "123@s.whatsapp.net",
        "chat_name": "Family",
        "chat_type": "dm",
    }
    return [
        MessageRecord(
            id="m1",
            timestamp=base,
            from_me=False,
            sender_name="Alice",
            kind="text",
            text="hola",
            **common,
        ),
        MessageRecord(
            id="m2",
            timestamp=base + timedelta(minutes=1),
            from_me=True,
            kind="text",
            text="que tal",
            **common,
        ),
        MessageRecord(
            id="m3",
            timestamp=base + timedelta(minutes=2),
            from_me=False,
            sender_name="Alice",
            kind="image",
            text="mirá esta foto",
            media_mime="image/jpeg",
            media_path=image_local,
            **common,
        ),
        MessageRecord(
            id="m4",
            timestamp=base + timedelta(minutes=3),
            from_me=False,
            sender_name="Alice",
            kind="ptt",
            media_mime="audio/ogg; codecs=opus",
            **common,
        ),
    ]


@pytest.fixture
def fake_setup(tmp_path: Path) -> tuple[Config, ChatRef, list[MessageRecord], dict[str, Path]]:
    """Build a config pointing at a tmp vault plus fake records and media files."""
    vault_dir = tmp_path / "vault"
    cache_dir = tmp_path / "cache"
    config = Config(
        vault_dir=vault_dir,
        output_subdir="Chats",
        cache_dir=cache_dir,
        state_dir=tmp_path / "state",
        language="es",
    )

    source_dir = tmp_path / "wacli_media"
    source_dir.mkdir()
    image_local = source_dir / "photo.jpg"
    image_local.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    audio_local = source_dir / "note.ogg"
    audio_local.write_bytes(b"OggS-fake-audio")

    chatref = ChatRef(jid="123@s.whatsapp.net", name="Family", chat_type="dm")
    records = _build_records(image_local)
    media_files = {"m3": image_local, "m4": audio_local}
    return config, chatref, records, media_files


def _install_fake_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    chatref: ChatRef,
    records: list[MessageRecord],
    media_files: dict[str, Path],
) -> FakeWacliClient:
    fake = FakeWacliClient(chatref=chatref, records=records, media_files=media_files)
    monkeypatch.setattr(pipeline, "WacliClient", fake)
    # Default to "no other writer running" so the pull performs its own sync;
    # the lock-aware skip path is covered explicitly in its own test.
    monkeypatch.setattr(pipeline, "find_store_lock", lambda config: None)
    return fake


def test_pull_chat_writes_note_and_copies_image(
    monkeypatch: pytest.MonkeyPatch,
    fake_setup: tuple[Config, ChatRef, list[MessageRecord], dict[str, Path]],
) -> None:
    config, chatref, records, media_files = fake_setup
    fake = _install_fake_client(
        monkeypatch, chatref=chatref, records=records, media_files=media_files
    )

    result = pipeline.pull_chat(
        config=config,
        chat="Family",
        days=30,
        transcribe=False,
        download_media=True,
    )

    # Sync ran once (best-effort) before resolving the chat.
    assert fake.sync_calls == 1

    # The note exists under vault_dir/output_subdir/.
    note_dir = config.vault_dir / config.output_subdir
    assert result.note_path.parent == note_dir / config.profile_key
    assert result.note_path.name.startswith("family--")
    assert result.note_path.exists()

    content = result.note_path.read_text(encoding="utf-8")
    assert "# Family" in content
    assert "## 2026-06-08" in content  # day heading
    assert "hola" in content

    # The image was copied into _media/<slug>/ and embedded with a
    # VAULT-RELATIVE path (not the absolute source path).
    copied_files = list(note_dir.rglob("*.jpg"))
    assert len(copied_files) == 1
    copied = copied_files[0]
    assert copied.exists()
    assert f"![[{copied.relative_to(config.vault_dir)}]]" in content
    assert str(config.vault_dir) not in content  # no absolute path leaked

    # PullResult counts.
    assert result.message_count == 4
    assert result.images_count == 1
    assert result.audios_transcribed == 0  # transcribe=False
    assert result.range_start == records[0].timestamp
    assert result.range_end == records[-1].timestamp
    assert result.warnings == []

    rendered = str(result)
    assert "Wrote 4 messages" in rendered
    assert "1 images" in rendered
    assert "'Family'" in rendered


def test_pull_chat_passes_configured_sync_timeout(
    monkeypatch: pytest.MonkeyPatch,
    fake_setup: tuple[Config, ChatRef, list[MessageRecord], dict[str, Path]],
) -> None:
    """The pull forwards ``config.sync_timeout`` to the bounded sync."""
    config, chatref, records, media_files = fake_setup
    config = config.model_copy(update={"sync_timeout": 42.0})
    fake = _install_fake_client(
        monkeypatch, chatref=chatref, records=records, media_files=media_files
    )

    pipeline.pull_chat(config=config, chat="Family", days=30, transcribe=False)

    assert fake.sync_timeout == 42.0


def test_pull_chat_proceeds_when_sync_times_out(
    monkeypatch: pytest.MonkeyPatch,
    fake_setup: tuple[Config, ChatRef, list[MessageRecord], dict[str, Path]],
) -> None:
    """A sync that times out (or otherwise fails) never aborts the pull.

    This is the whole point of the bounded sync: a slow/stale-store sync must
    degrade to "use the local store as-is" so export/render still run and the
    note is written, with the failure recorded only as a warning.
    """
    config, chatref, records, media_files = fake_setup
    fake = _install_fake_client(
        monkeypatch, chatref=chatref, records=records, media_files=media_files
    )
    fake.sync_error = WacliError("wacli sync --once timed out after 90s")

    result = pipeline.pull_chat(
        config=config, chat="Family", days=30, transcribe=False, download_media=True
    )

    # The note was still written from the local store.
    assert result.note_path.exists()
    assert result.message_count == 4
    # The timeout surfaced as a single best-effort warning, not an exception.
    assert len(result.warnings) == 1
    assert "timed out" in result.warnings[0]
    assert "local store as-is" in result.warnings[0]


def test_pull_chat_skips_sync_when_another_instance_holds_the_lock(
    monkeypatch: pytest.MonkeyPatch,
    fake_setup: tuple[Config, ChatRef, list[MessageRecord], dict[str, Path]],
) -> None:
    """When another writer holds the store lock, the pull skips its own sync.

    wacli is single-writer, so starting a second sync would only race for a lock
    it cannot get. The pull must instead skip the sync, export from the current
    local store (reads are safe alongside the running writer), and record the
    skip as a single warning -- never starting a competing wacli.
    """
    config, chatref, records, media_files = fake_setup
    fake = _install_fake_client(
        monkeypatch, chatref=chatref, records=records, media_files=media_files
    )
    monkeypatch.setattr(
        pipeline,
        "find_store_lock",
        lambda config: StoreLock(
            pid=4242,
            acquired_at="2026-06-09T12:50:12-03:00",
            lock_file=Path("/tmp/wacli/LOCK"),
        ),
    )

    result = pipeline.pull_chat(
        config=config, chat="Family", days=30, transcribe=False, download_media=True
    )

    # No competing sync was started.
    assert fake.sync_calls == 0
    # The note was still written from the local store.
    assert result.note_path.exists()
    assert result.message_count == 4
    # The skip surfaced as a single best-effort warning naming the holder.
    assert len(result.warnings) == 1
    assert "another wa2vault/wacli instance is syncing" in result.warnings[0]
    assert "pid 4242" in result.warnings[0]


def test_pull_chat_copies_document_and_links_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A document attachment is copied into the vault and linked from the note."""
    vault_dir = tmp_path / "vault"
    config = Config(
        vault_dir=vault_dir,
        output_subdir="Chats",
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
    )

    source = tmp_path / "wacli_media"
    source.mkdir()
    doc_local = source / "3B3B46BA21CB15C1E866.pdf"
    doc_local.write_bytes(b"%PDF-1.4 fake")

    base = datetime(2026, 6, 17, 18, 0, tzinfo=UTC)
    chatref = ChatRef(jid="120363000000000000@g.us", name="Mi Grupo", chat_type="group")
    records = [
        MessageRecord(
            id="doc1",
            chat_jid=chatref.jid,
            chat_type="group",
            timestamp=base,
            from_me=False,
            sender_name="Alex",
            kind="document",
            text=None,
            media_mime="application/pdf",
            raw={"Filename": "Propuesta Piloto.pdf"},
        ),
    ]
    _install_fake_client(
        monkeypatch, chatref=chatref, records=records, media_files={"doc1": doc_local}
    )

    result = pipeline.pull_chat(
        config=config, chat="Mi Grupo", days=30, transcribe=False, download_media=True
    )

    # The PDF was copied under its readable original filename and linked.
    copied_files = list((vault_dir / "Chats").rglob("*.pdf"))
    assert len(copied_files) == 1
    copied = copied_files[0]
    assert copied.exists()
    content = result.note_path.read_text(encoding="utf-8")
    assert f"[[{copied.relative_to(vault_dir)}]]" in content
    assert "*[documento]*" not in content


def test_pull_chat_flags_expired_audio_explicitly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Audio whose media expired on the CDN renders as 'expired', not a bare bug."""
    vault_dir = tmp_path / "vault"
    config = Config(
        vault_dir=vault_dir,
        output_subdir="Chats",
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
    )

    chatref = ChatRef(jid="123@s.whatsapp.net", name="Family", chat_type="dm")
    records = [
        MessageRecord(
            id="aud-exp",
            chat_jid=chatref.jid,
            chat_type="dm",
            timestamp=datetime(2026, 6, 2, 17, 25, tzinfo=UTC),
            from_me=False,
            sender_name="Alice",
            kind="ptt",
            media_mime="audio/ogg; codecs=opus",
        ),
    ]
    fake = FakeWacliClient(
        chatref=chatref,
        records=records,
        media_files={},  # no downloadable file
        expired_media={"aud-exp"},  # ...because it expired on the CDN
    )
    monkeypatch.setattr(pipeline, "WacliClient", fake)
    monkeypatch.setattr(pipeline, "find_store_lock", lambda config: None)

    result = pipeline.pull_chat(
        config=config, chat="Family", days=30, transcribe=True, download_media=True
    )

    content = result.note_path.read_text(encoding="utf-8")
    assert "*(media expirada en WhatsApp, no se pudo descargar)*" in content
    assert "*(audio sin transcribir)*" not in content
    assert result.audios_transcribed == 0


def test_pull_chat_without_media_renders_image_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    fake_setup: tuple[Config, ChatRef, list[MessageRecord], dict[str, Path]],
) -> None:
    config, chatref, records, media_files = fake_setup
    _install_fake_client(monkeypatch, chatref=chatref, records=records, media_files=media_files)

    result = pipeline.pull_chat(
        config=config,
        chat="Family",
        days=30,
        transcribe=False,
        download_media=False,
    )

    content = result.note_path.read_text(encoding="utf-8")
    # With media disabled, the image renders as the unavailable fallback and is
    # not embedded or copied.
    assert "*(imagen no disponible)*" in content
    assert "![[" not in content
    assert not any(
        path.name == "_media" for path in (config.vault_dir / config.output_subdir).rglob("_media")
    )

    assert result.images_count == 0
    assert result.message_count == 4


def test_pull_is_monotonic_across_overlapping_and_shrinking_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A later short export must not replace a previously complete archive."""
    config = Config(
        vault_dir=tmp_path / "vault",
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
    )
    chatref = ChatRef(
        jid="15550100001@s.whatsapp.net",
        name="Example contact",
        chat_type="dm",
    )
    initial = [
        MessageRecord(
            id=f"m{index}",
            chat_jid=chatref.jid,
            chat_type="dm",
            timestamp=datetime(2026, 1, 1, index, tzinfo=UTC),
            from_me=False,
            kind="text",
            text=f"message {index}",
        )
        for index in (1, 2, 3)
    ]
    fake = _install_fake_client(monkeypatch, chatref=chatref, records=initial, media_files={})

    first = pipeline.pull_chat(config=config, chat=chatref.jid, days=30, transcribe=False)
    fake._records = [initial[-1]]
    second = pipeline.pull_chat(config=config, chat=chatref.jid, days=1, transcribe=False)

    assert first.note_path == second.note_path
    assert second.message_count == 3
    content = second.note_path.read_text(encoding="utf-8")
    assert all(f"message {index}" in content for index in (1, 2, 3))


def test_profiles_with_identical_remote_ids_write_disjoint_archives(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    common = dict(
        vault_dir=vault,
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
    )
    chatref = ChatRef(
        jid="15550100001@s.whatsapp.net",
        name="Same title",
        chat_type="dm",
    )
    personal = MessageRecord(
        id="same-message-id",
        chat_jid=chatref.jid,
        chat_type="dm",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        from_me=False,
        kind="text",
        text="personal-only marker",
    )
    work = personal.model_copy(update={"text": "work-only marker"})
    fake = _install_fake_client(monkeypatch, chatref=chatref, records=[personal], media_files={})

    personal_result = pipeline.pull_chat(
        config=Config(profile="personal", **common),
        chat=chatref.jid,
        days=30,
        transcribe=False,
    )
    fake._records = [work]
    work_result = pipeline.pull_chat(
        config=Config(profile="work", **common),
        chat=chatref.jid,
        days=30,
        transcribe=False,
    )

    assert personal_result.note_path != work_result.note_path
    assert "personal-only marker" in personal_result.note_path.read_text(encoding="utf-8")
    assert "work-only marker" not in personal_result.note_path.read_text(encoding="utf-8")
    assert "work-only marker" in work_result.note_path.read_text(encoding="utf-8")


def test_attachment_names_are_stable_and_message_collision_resistant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    config = Config(
        profile="work",
        vault_dir=vault,
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
    )
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    first_source = source_dir / "one" / "report.pdf"
    second_source = source_dir / "two" / "report.pdf"
    first_source.parent.mkdir()
    second_source.parent.mkdir()
    first_source.write_bytes(b"first")
    second_source.write_bytes(b"second")
    chatref = ChatRef(jid="15550100002@s.whatsapp.net", name="Reports", chat_type="dm")
    records = [
        MessageRecord(
            id=message_id,
            chat_jid=chatref.jid,
            chat_type="dm",
            timestamp=datetime(2026, 1, 1, index, tzinfo=UTC),
            from_me=False,
            kind="document",
            media_mime="application/pdf",
            raw={"Filename": "report.pdf"},
        )
        for index, message_id in enumerate(("doc-one", "doc-two"), start=1)
    ]
    _install_fake_client(
        monkeypatch,
        chatref=chatref,
        records=records,
        media_files={"doc-one": first_source, "doc-two": second_source},
    )

    first = pipeline.pull_chat(config=config, chat=chatref.jid, days=30, transcribe=False)
    copied = sorted((vault / "Chats").rglob("*.pdf"))
    second = pipeline.pull_chat(config=config, chat=chatref.jid, days=30, transcribe=False)

    assert len(copied) == 2
    assert copied[0].name != copied[1].name
    assert {path.read_bytes() for path in copied} == {b"first", b"second"}
    assert first.note_path == second.note_path
    assert sorted((vault / "Chats").rglob("*.pdf")) == copied
