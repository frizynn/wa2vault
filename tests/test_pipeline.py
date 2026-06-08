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
from wa2vault.models import MessageRecord
from wa2vault.wacli import ChatRef, WacliError


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
    ) -> None:
        self._chatref = chatref
        self._records = records
        self._media_files = media_files
        self.sync_calls = 0
        self.sync_timeout: float | None = None
        self.sync_error: Exception | None = None

    def __call__(self, config: Config) -> "FakeWacliClient":
        # ``pipeline.WacliClient(config)`` is monkeypatched to this instance;
        # being callable lets it act as the constructor too.
        return self

    def sync_once(
        self, *, full: bool = False, timeout: float | None = None
    ) -> dict[str, object]:
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

    def ensure_media(self, record: MessageRecord) -> Path | None:
        return self._media_files.get(record.id)


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
    assert result.note_path == note_dir / "family.md"
    assert result.note_path.exists()

    content = result.note_path.read_text(encoding="utf-8")
    assert "# Family" in content
    assert "## 2026-06-08" in content  # day heading
    assert "hola" in content

    # The image was copied into _media/<slug>/ and embedded with a
    # VAULT-RELATIVE path (not the absolute source path).
    copied = note_dir / "_media" / "family" / "photo.jpg"
    assert copied.exists()
    assert "![[Chats/_media/family/photo.jpg]]" in content
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


def test_pull_chat_without_media_renders_image_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    fake_setup: tuple[Config, ChatRef, list[MessageRecord], dict[str, Path]],
) -> None:
    config, chatref, records, media_files = fake_setup
    _install_fake_client(
        monkeypatch, chatref=chatref, records=records, media_files=media_files
    )

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
    assert not (config.vault_dir / config.output_subdir / "_media").exists()

    assert result.images_count == 0
    assert result.message_count == 4
