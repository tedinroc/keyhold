"""The Console protocol: screenshot() -> PNG bytes, type_text(text), press(keys), power_cycle(), close().

``Console`` is defined in ``keyhold.types`` and re-exported here, together with what every backend
shares: the QEMU key names (QKeyCode), friendly aliases for them, and the US-layout table that turns
text into key chords.

A chord is a list of key names pressed together and released in reverse order, for example
``["shift", "a"]`` for ``A`` or ``["ctrl", "alt", "delete"]``.
"""

from __future__ import annotations

from collections.abc import Iterable

from keyhold.types import Console

__all__ = [
    "Console",
    "ConsoleError",
    "ConsoleClosed",
    "UnsupportedKey",
    "UnsupportedText",
    "QKEYCODES",
    "KEY_ALIASES",
    "normalize_key",
    "normalize_chord",
    "chord_for_char",
    "char_for_chord",
    "text_to_chords",
]


class ConsoleError(RuntimeError):
    """The console backend failed (for example QEMU went away or a command was rejected)."""


class ConsoleClosed(ConsoleError):
    """The console was closed; no further screenshots or input."""


class UnsupportedKey(ValueError):
    """A key name that is not a QEMU QKeyCode and not a known alias."""


class UnsupportedText(ValueError):
    """Text holding a character that has no key on a US keyboard (for example non-ASCII)."""


# QEMU QKeyCode names (qapi/ui.json) that a server console can need. Media and language keys are
# left out on purpose.
QKEYCODES: frozenset[str] = frozenset(
    """
    shift shift_r alt alt_r ctrl ctrl_r meta_l meta_r menu
    esc tab backspace ret spc caps_lock num_lock scroll_lock
    minus equal bracket_left bracket_right semicolon apostrophe grave_accent backslash
    comma dot slash less
    home end pgup pgdn insert delete up down left right
    sysrq pause print
    kp_divide kp_multiply kp_subtract kp_add kp_enter kp_decimal
    """.split()
    + [chr(c) for c in range(ord("a"), ord("z") + 1)]
    + [str(d) for d in range(10)]
    + [f"kp_{d}" for d in range(10)]
    + [f"f{n}" for n in range(1, 13)]
)

# Names people and models tend to use, mapped to QKeyCode names.
KEY_ALIASES: dict[str, str] = {
    "enter": "ret",
    "return": "ret",
    "escape": "esc",
    "space": "spc",
    "control": "ctrl",
    "ctl": "ctrl",
    "lctrl": "ctrl",
    "rctrl": "ctrl_r",
    "lshift": "shift",
    "rshift": "shift_r",
    "option": "alt",
    "lalt": "alt",
    "ralt": "alt_r",
    "altgr": "alt_r",
    "super": "meta_l",
    "win": "meta_l",
    "meta": "meta_l",
    "cmd": "meta_l",
    "del": "delete",
    "ins": "insert",
    "bksp": "backspace",
    "back_space": "backspace",
    "pageup": "pgup",
    "page_up": "pgup",
    "pagedown": "pgdn",
    "page_down": "pgdn",
    "arrowup": "up",
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
    "capslock": "caps_lock",
    "printscreen": "print",
    "prtsc": "print",
    "period": "dot",
}

# US layout: character -> chord. Built below from the unshifted and shifted rows.
_UNSHIFTED = {
    " ": "spc",
    "\n": "ret",
    "\t": "tab",
    "-": "minus",
    "=": "equal",
    "[": "bracket_left",
    "]": "bracket_right",
    ";": "semicolon",
    "'": "apostrophe",
    "`": "grave_accent",
    "\\": "backslash",
    ",": "comma",
    ".": "dot",
    "/": "slash",
}
_SHIFTED = {
    "_": "minus",
    "+": "equal",
    "{": "bracket_left",
    "}": "bracket_right",
    ":": "semicolon",
    '"': "apostrophe",
    "~": "grave_accent",
    "|": "backslash",
    "<": "comma",
    ">": "dot",
    "?": "slash",
    "!": "1",
    "@": "2",
    "#": "3",
    "$": "4",
    "%": "5",
    "^": "6",
    "&": "7",
    "*": "8",
    "(": "9",
    ")": "0",
}

_CHAR_CHORDS: dict[str, tuple[str, ...]] = {}
for _c in "abcdefghijklmnopqrstuvwxyz0123456789":
    _CHAR_CHORDS[_c] = (_c,)
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    _CHAR_CHORDS[_c] = ("shift", _c.lower())
for _c, _k in _UNSHIFTED.items():
    _CHAR_CHORDS[_c] = (_k,)
for _c, _k in _SHIFTED.items():
    _CHAR_CHORDS[_c] = ("shift", _k)

# Single characters accepted as key names in press(), for example press(["ctrl", "/"]).
_CHAR_KEYS: dict[str, str] = {c: chord[-1] for c, chord in _CHAR_CHORDS.items() if len(chord) == 1}

# Chord -> character, for backends that echo typed text (the fake console).
_CHORD_CHARS: dict[tuple[str, ...], str] = {chord: c for c, chord in _CHAR_CHORDS.items()}


def normalize_key(name: str) -> str:
    """Return the QKeyCode for ``name``: a QKeyCode, an alias such as ``enter``, or one unshifted
    character such as ``/``. Upper-case letters are folded to lower case (press ``shift`` for
    capitals). Raises ``UnsupportedKey`` for anything else."""
    if not isinstance(name, str) or not name:
        raise UnsupportedKey(f"key name must be a non-empty string, got {name!r}")
    if len(name) == 1:
        lowered = name.lower()
        if lowered in _CHAR_KEYS:
            return _CHAR_KEYS[lowered]
        raise UnsupportedKey(f"no single key types {name!r}; use type_text for shifted characters")
    key = name.strip().lower().replace("-", "_")
    if key in QKEYCODES:
        return key
    if key in KEY_ALIASES:
        return KEY_ALIASES[key]
    raise UnsupportedKey(f"unknown key name {name!r}")


def normalize_chord(keys: Iterable[str]) -> list[str]:
    """Normalize every key in a chord. A chord has 1 to 6 keys and no key twice."""
    if isinstance(keys, str):
        raise UnsupportedKey("press() takes a list of key names, not a string")
    chord = [normalize_key(k) for k in keys]
    if not chord:
        raise UnsupportedKey("a chord needs at least one key")
    if len(chord) > 6:
        raise UnsupportedKey("a chord has at most 6 keys")
    if len(set(chord)) != len(chord):
        raise UnsupportedKey(f"key repeated in chord {chord!r}")
    return chord


def chord_for_char(char: str) -> list[str]:
    """The chord that types ``char`` on a US keyboard. ``\\r`` is not accepted; send ``\\n``."""
    try:
        return list(_CHAR_CHORDS[char])
    except KeyError:
        raise UnsupportedText(f"character {char!r} cannot be typed on a US keyboard") from None


def char_for_chord(chord: Iterable[str]) -> str | None:
    """The character a chord types on a US keyboard, or None (for example for ``ctrl`` + ``c``)."""
    return _CHORD_CHARS.get(tuple(chord))


def text_to_chords(text: str) -> list[list[str]]:
    """Turn text into one chord per character. ``\\r\\n`` and ``\\r`` count as one Enter.

    The whole text is checked before anything is returned, so a caller can refuse it without
    having sent half of it.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return [chord_for_char(c) for c in normalized]
