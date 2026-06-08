"""Tests for the local contact book (:mod:`wa2vault.contacts`).

Cover JID normalization across phone formats, the :class:`ContactBook`
set/lookup/find/remove operations, atomic persistence round-trips, and graceful
tolerance of a missing or corrupt store file. All tests use a temp path so the
real user-config contacts file is never touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wa2vault.contacts import ContactBook, normalize_jid, pretty_phone


# --------------------------------------------------------------------------- #
# normalize_jid
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw",
    [
        "+54 9 11 5805-7007",
        "5491158057007",
        "549 11 5805 7007",
        "(549) 11 5805 7007",
    ],
)
def test_normalize_jid_phone_formats(raw: str) -> None:
    assert normalize_jid(raw) == "5491158057007@s.whatsapp.net"


def test_normalize_jid_already_a_dm_jid() -> None:
    assert (
        normalize_jid("5491158057007@s.whatsapp.net")
        == "5491158057007@s.whatsapp.net"
    )


def test_normalize_jid_jid_is_lowercased_and_stripped() -> None:
    assert normalize_jid("  Foo@S.Whatsapp.Net  ") == "foo@s.whatsapp.net"


def test_normalize_jid_group_jid_unchanged() -> None:
    assert normalize_jid("123456789-987654321@g.us") == "123456789-987654321@g.us"


@pytest.mark.parametrize("raw", ["", "   ", "no-digits-here"])
def test_normalize_jid_invalid_raises(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_jid(raw)


# --------------------------------------------------------------------------- #
# pretty_phone
# --------------------------------------------------------------------------- #
def test_pretty_phone_dm_jid() -> None:
    assert pretty_phone("5491158057007@s.whatsapp.net") == "+5491158057007"


def test_pretty_phone_group_jid_unchanged() -> None:
    jid = "123456789-987654321@g.us"
    assert pretty_phone(jid) == jid


# --------------------------------------------------------------------------- #
# ContactBook.set / name_for
# --------------------------------------------------------------------------- #
def test_set_returns_jid_and_stores_stripped_name(tmp_path: Path) -> None:
    book = ContactBook(tmp_path / "contacts.json")
    jid = book.set("+54 9 11 5805-7007", "  Lucio  ")
    assert jid == "5491158057007@s.whatsapp.net"
    assert book.name_for(jid) == "Lucio"


def test_set_empty_name_raises(tmp_path: Path) -> None:
    book = ContactBook(tmp_path / "contacts.json")
    with pytest.raises(ValueError):
        book.set("5491158057007", "   ")


def test_name_for_unknown_jid_returns_none(tmp_path: Path) -> None:
    book = ContactBook(tmp_path / "contacts.json")
    assert book.name_for("999@s.whatsapp.net") is None


# --------------------------------------------------------------------------- #
# ContactBook.find
# --------------------------------------------------------------------------- #
def test_find_exact_name_case_insensitive(tmp_path: Path) -> None:
    book = ContactBook(tmp_path / "contacts.json")
    jid = book.set("5491158057007", "Lucio")
    assert book.find("lucio") == jid


def test_find_unique_substring(tmp_path: Path) -> None:
    book = ContactBook(tmp_path / "contacts.json")
    jid = book.set("5491158057007", "Lucio Pérez")
    assert book.find("pérez") == jid


def test_find_ambiguous_substring_returns_none(tmp_path: Path) -> None:
    book = ContactBook(tmp_path / "contacts.json")
    book.set("5491111111111", "Lucio Uno")
    book.set("5492222222222", "Lucio Dos")
    assert book.find("lucio") is None


def test_find_by_jid(tmp_path: Path) -> None:
    book = ContactBook(tmp_path / "contacts.json")
    jid = book.set("5491158057007", "Lucio")
    assert book.find("5491158057007@s.whatsapp.net") == jid


def test_find_by_number(tmp_path: Path) -> None:
    book = ContactBook(tmp_path / "contacts.json")
    jid = book.set("5491158057007", "Lucio")
    assert book.find("+54 9 11 5805-7007") == jid


def test_find_no_match_returns_none(tmp_path: Path) -> None:
    book = ContactBook(tmp_path / "contacts.json")
    book.set("5491158057007", "Lucio")
    assert book.find("nobody") is None


# --------------------------------------------------------------------------- #
# ContactBook.remove
# --------------------------------------------------------------------------- #
def test_remove_by_name_case_insensitive(tmp_path: Path) -> None:
    path = tmp_path / "contacts.json"
    book = ContactBook(path)
    jid = book.set("5491158057007", "Lucio")
    assert book.remove("LUCIO") is True
    assert book.name_for(jid) is None
    # Persisted: a fresh book sees the deletion.
    assert ContactBook(path).items() == {}


def test_remove_by_number(tmp_path: Path) -> None:
    book = ContactBook(tmp_path / "contacts.json")
    book.set("5491158057007", "Lucio")
    assert book.remove("+54 9 11 5805-7007") is True
    assert book.items() == {}


def test_remove_no_match_returns_false(tmp_path: Path) -> None:
    book = ContactBook(tmp_path / "contacts.json")
    book.set("5491158057007", "Lucio")
    assert book.remove("nobody") is False
    assert book.items() == {"5491158057007@s.whatsapp.net": "Lucio"}


# --------------------------------------------------------------------------- #
# Persistence + file tolerance
# --------------------------------------------------------------------------- #
def test_persistence_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "contacts.json"
    book = ContactBook(path)
    jid = book.set("5491158057007", "Lucio")
    assert path.exists()

    reloaded = ContactBook(path)
    assert reloaded.items() == {jid: "Lucio"}
    assert reloaded.name_for(jid) == "Lucio"


def test_missing_file_starts_empty(tmp_path: Path) -> None:
    book = ContactBook(tmp_path / "does-not-exist.json")
    assert book.items() == {}


def test_corrupt_file_starts_empty(tmp_path: Path) -> None:
    path = tmp_path / "contacts.json"
    path.write_text("{ this is not valid json", encoding="utf-8")
    book = ContactBook(path)
    assert book.items() == {}
    # The book still works and overwrites the corrupt file cleanly.
    jid = book.set("5491158057007", "Lucio")
    assert ContactBook(path).items() == {jid: "Lucio"}


def test_items_returns_a_copy(tmp_path: Path) -> None:
    book = ContactBook(tmp_path / "contacts.json")
    jid = book.set("5491158057007", "Lucio")
    snapshot = book.items()
    snapshot["bogus@s.whatsapp.net"] = "Mutated"
    assert book.items() == {jid: "Lucio"}
