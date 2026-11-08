"""Key names and the text -> key chord table shared by every console backend."""

from __future__ import annotations

import string

import pytest
from hypothesis import given
from hypothesis import strategies as st

from keyhold.console.base import (
    QKEYCODES,
    UnsupportedKey,
    UnsupportedText,
    char_for_chord,
    chord_for_char,
    normalize_chord,
    normalize_key,
    text_to_chords,
)

TYPEABLE = string.ascii_letters + string.digits + string.punctuation + " \n\t"


@pytest.mark.parametrize(
    ("name", "qcode"),
    [
        ("ret", "ret"),
        ("Enter", "ret"),
        ("RETURN", "ret"),
        ("Escape", "esc"),
        ("space", "spc"),
        ("Control", "ctrl"),
        ("del", "delete"),
        ("page-down", "pgdn"),
        ("PageUp", "pgup"),
        ("F2", "f2"),
        ("A", "a"),
        ("/", "slash"),
        ("-", "minus"),
        ("7", "7"),
        ("kp_enter", "kp_enter"),
    ],
)
def test_normalize_key_accepts_qcodes_aliases_and_characters(name: str, qcode: str) -> None:
    assert normalize_key(name) == qcode
    assert qcode in QKEYCODES


@pytest.mark.parametrize("name", ["", "hyper", "!", "é", "ctrl+c", None, 5])
def test_normalize_key_rejects_unknown(name: object) -> None:
    with pytest.raises(UnsupportedKey):
        normalize_key(name)  # type: ignore[arg-type]


def test_normalize_chord() -> None:
    assert normalize_chord(["ctrl", "alt", "del"]) == ["ctrl", "alt", "delete"]
    assert normalize_chord(["Ctrl", "C"]) == ["ctrl", "c"]
    with pytest.raises(UnsupportedKey):
        normalize_chord([])
    with pytest.raises(UnsupportedKey):
        normalize_chord(["ctrl", "control"])  # the same key twice
    with pytest.raises(UnsupportedKey):
        normalize_chord("ret")  # a string is not a chord
    with pytest.raises(UnsupportedKey):
        normalize_chord(["a", "b", "c", "d", "e", "f", "g"])


def test_text_to_chords_uses_a_us_layout() -> None:
    assert text_to_chords("Ab!\n") == [["shift", "a"], ["b"], ["shift", "1"], ["ret"]]
    assert text_to_chords("a\r\nb\rc") == [["a"], ["ret"], ["b"], ["ret"], ["c"]]
    assert text_to_chords('|"~') == [["shift", "backslash"], ["shift", "apostrophe"], ["shift", "grave_accent"]]
    assert text_to_chords("") == []


@pytest.mark.parametrize("text", ["café", "naïve", "\x00", "\x1b[A", "日本"])
def test_text_to_chords_refuses_untypeable_text(text: str) -> None:
    with pytest.raises(UnsupportedText):
        text_to_chords(text)


def test_every_typeable_character_round_trips() -> None:
    for char in TYPEABLE:
        chord = chord_for_char(char)
        assert all(key in QKEYCODES for key in chord), char
        assert char_for_chord(chord) == char
    assert char_for_chord(["ctrl", "c"]) is None


@given(st.text(alphabet=TYPEABLE, max_size=200))
def test_text_round_trips_through_chords(text: str) -> None:
    chords = text_to_chords(text)
    assert len(chords) == len(text)
    assert "".join(char_for_chord(c) or "?" for c in chords) == text
