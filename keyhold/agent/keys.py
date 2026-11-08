"""Key names for the console keyboard.

The console takes QEMU QKeyCode names (``ret``, ``esc``, ``ctrl``, ``a``, ``slash``...). A chord is
a list of names pressed together, for example ``["ctrl", "alt", "delete"]``. The agent writes a
chord as one string, ``"ctrl+alt+delete"``; :func:`parse_combo` turns that into the list and
rejects names the console does not know.

:func:`chord_char` says which character a chord types on a US keyboard layout, so the keystroke
firewall can follow what lands on the command line.
"""

from __future__ import annotations

import string

# Modifier key name -> modifier kind.
MODIFIERS: dict[str, str] = {
    "shift": "shift",
    "shift_r": "shift",
    "ctrl": "ctrl",
    "ctrl_r": "ctrl",
    "alt": "alt",
    "alt_r": "alt",
    "meta_l": "meta",
    "meta_r": "meta",
}

# Printable keys: name -> (plain char, shifted char).
PRINTABLE: dict[str, tuple[str, str]] = {
    **{c: (c, c.upper()) for c in string.ascii_lowercase},
    **{d: (d, s) for d, s in zip("1234567890", "!@#$%^&*()", strict=True)},
    "minus": ("-", "_"),
    "equal": ("=", "+"),
    "bracket_left": ("[", "{"),
    "bracket_right": ("]", "}"),
    "backslash": ("\\", "|"),
    "semicolon": (";", ":"),
    "apostrophe": ("'", '"'),
    "grave_accent": ("`", "~"),
    "comma": (",", "<"),
    "dot": (".", ">"),
    "slash": ("/", "?"),
    "spc": (" ", " "),
    "kp_add": ("+", "+"),
    "kp_subtract": ("-", "-"),
    "kp_multiply": ("*", "*"),
    "kp_divide": ("/", "/"),
}

# Non-printable keys the console accepts.
SPECIAL: frozenset[str] = frozenset(
    {
        "ret",
        "kp_enter",
        "esc",
        "tab",
        "backspace",
        "delete",
        "insert",
        "home",
        "end",
        "pgup",
        "pgdn",
        "up",
        "down",
        "left",
        "right",
        "sysrq",
        "caps_lock",
        "num_lock",
        "scroll_lock",
        "menu",
        "pause",
        "kp_decimal",
        *(f"f{i}" for i in range(1, 13)),
        *(f"kp_{i}" for i in range(10)),
    }
)

KEY_NAMES: frozenset[str] = frozenset(MODIFIERS) | frozenset(PRINTABLE) | SPECIAL

ALIASES: dict[str, str] = {
    "enter": "ret",
    "return": "ret",
    "escape": "esc",
    "space": "spc",
    "del": "delete",
    "pageup": "pgup",
    "page_up": "pgup",
    "pagedown": "pgdn",
    "page_down": "pgdn",
    "control": "ctrl",
    "ctl": "ctrl",
    "bksp": "backspace",
    "ins": "insert",
    "arrowup": "up",
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
    "super": "meta_l",
    "win": "meta_l",
    "meta": "meta_l",
    "option": "alt",
    "period": "dot",
    "plus": "equal+shift",
}

# Single characters the agent may write as key names, mapped to (key, needs shift).
_CHAR_TO_KEY: dict[str, tuple[str, bool]] = {}
for _name, (_plain, _shifted) in PRINTABLE.items():
    if _name.startswith("kp_"):
        continue
    _CHAR_TO_KEY.setdefault(_plain, (_name, False))
    _CHAR_TO_KEY.setdefault(_shifted, (_name, True))

MAX_CHORD = 4


def _resolve(part: str) -> list[str]:
    """Return the key names for one ``+``-separated part of a combo."""
    if len(part) == 1 and part in _CHAR_TO_KEY:
        key, shift = _CHAR_TO_KEY[part]
        return ["shift", key] if shift else [key]
    name = part.strip().lower().replace("-", "_")
    name = ALIASES.get(name, name)
    if name == "equal+shift":
        return ["shift", "equal"]
    if name not in KEY_NAMES:
        raise ValueError(f"unknown key name {part!r}")
    return [name]


def parse_combo(combo: str) -> list[str]:
    """Turn ``"ctrl+alt+delete"`` into ``["ctrl", "alt", "delete"]``.

    Accepts QKeyCode names, a few common aliases (``enter``, ``escape``, ``space``...) and single
    characters (``"a"``, ``"/"``, ``"A"`` becomes shift+a). Raises ``ValueError`` for anything else.
    """
    if not isinstance(combo, str) or not combo.strip():
        raise ValueError("combo is empty")
    text = combo.strip()
    if text == "+":
        parts = ["+"]
    elif text.endswith("++"):
        parts = [p for p in text[:-2].split("+")] + ["+"]
    else:
        parts = text.split("+")
    if any(p.strip() == "" for p in parts):
        raise ValueError(f"malformed combo {combo!r}")
    keys: list[str] = []
    for part in parts:
        for name in _resolve(part.strip() if len(part.strip()) > 1 else part):
            if name not in keys:
                keys.append(name)
    if len(keys) > MAX_CHORD:
        raise ValueError(f"combo {combo!r} presses more than {MAX_CHORD} keys")
    if all(k in MODIFIERS for k in keys):
        raise ValueError(f"combo {combo!r} has only modifier keys")
    return keys


def modifiers_of(keys: list[str]) -> set[str]:
    return {MODIFIERS[k] for k in keys if k in MODIFIERS}


def main_keys(keys: list[str]) -> list[str]:
    return [k for k in keys if k not in MODIFIERS]


def chord_char(keys: list[str]) -> str | None:
    """The character this chord types, or None if it is not a plain (or shifted) printable key."""
    mods = modifiers_of(keys)
    mains = main_keys(keys)
    if len(mains) != 1 or mods - {"shift"}:
        return None
    entry = PRINTABLE.get(mains[0])
    if entry is None:
        return None
    return entry[1] if "shift" in mods else entry[0]


def format_combo(keys: list[str]) -> str:
    return "+".join(keys)
