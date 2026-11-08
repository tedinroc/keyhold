"""Scripted fake console for tests: returns prepared screens and records every input it receives.

A ``FakeConsole`` holds named screens. A screen is either PNG bytes or lines of text that are
rendered to PNG the way a text console looks: the screen's lines, then every line entered on it
(with any output a rule adds), then the prompt and what has been typed so far.

Rules move the console between screens. A rule fires when a line is entered (Enter pressed, or
``\\n`` in typed text) and the line matches its regular expression, when a chord is pressed, or on
``power_cycle()``. That is enough to script a login, a fix and a reboot without a VM::

    console = FakeConsole(
        {
            "login": Screen(["Debian GNU/Linux 13 demo tty1", ""], prompt="demo login: "),
            "password": Screen([], prompt="Password: ", echo=False),
            "shell": Screen([], prompt="root@demo:~# "),
        },
        rules=[
            Rule(on="login", line="root", goto="password"),
            Rule(on="password", line=".*", goto="shell"),
            Rule(on="shell", line="systemctl unmask ssh", output=["Removed ..."]),
        ],
    )

Input checks match the QEMU backend: unknown key names raise ``UnsupportedKey`` and text that a
US keyboard cannot type raises ``UnsupportedText`` before anything is recorded.
"""

from __future__ import annotations

import io
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from keyhold.console.base import ConsoleClosed, char_for_chord, normalize_chord, text_to_chords
from keyhold.types import InputCut

__all__ = ["FakeConsole", "Screen", "Rule", "InputRecord", "render_text_png"]

DEFAULT_SIZE = (1024, 768)
_MONO_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Monaco.ttf",
    "C:/Windows/Fonts/consola.ttf",
)


@lru_cache(maxsize=4)
def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    for path in _MONO_FONTS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def render_text_png(lines: Sequence[str], size: tuple[int, int] = DEFAULT_SIZE, *, font_size: int = 16) -> bytes:
    """Render lines of text as a console screen (light grey on black) and return PNG bytes.

    Only the last lines that fit are drawn, like a scrolling text console.
    """
    width, height = size
    font = _font(font_size)
    line_height = font_size + 4
    rows = max(1, (height - 16) // line_height)
    image = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    y = 8
    for line in list(lines)[-rows:]:
        draw.text((8, y), line, fill=(200, 200, 200), font=font)
        y += line_height
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


@dataclass(frozen=True)
class Screen:
    """One screen. ``lines`` is text to render; ``png`` (if set) is returned as it is instead.

    ``prompt`` is shown before the typed text. With ``echo=False`` typed text is not shown (a
    password prompt).
    """

    lines: Sequence[str] = ()
    prompt: str = ""
    echo: bool = True
    png: bytes | None = None

    def __post_init__(self) -> None:
        if isinstance(self.lines, str):
            object.__setattr__(self, "lines", tuple(self.lines.splitlines()))
        else:
            object.__setattr__(self, "lines", tuple(self.lines))


@dataclass(frozen=True)
class Rule:
    """Move to ``goto`` (or stay, if None) and add ``output`` lines when the trigger matches.

    Exactly one trigger: ``line`` (a regular expression that must match the whole entered line,
    leading and trailing spaces ignored), ``keys`` (a chord, compared after key-name
    normalization) or ``power_cycle=True``. ``on`` limits the rule to one screen.
    ``once=True`` lets a rule fire only the first time it matches.
    """

    goto: str | None = None
    on: str | None = None
    line: str | None = None
    keys: Sequence[str] | None = None
    power_cycle: bool = False
    output: Sequence[str] = ()
    once: bool = False

    def __post_init__(self) -> None:
        triggers = sum([self.line is not None, self.keys is not None, bool(self.power_cycle)])
        if triggers != 1:
            raise ValueError("a Rule needs exactly one trigger: line, keys or power_cycle")
        if self.keys is not None:
            object.__setattr__(self, "keys", tuple(normalize_chord(self.keys)))
        object.__setattr__(self, "output", tuple(self.output))


@dataclass(frozen=True)
class InputRecord:
    """One input the console received. ``kind`` is ``type``, ``press`` or ``power_cycle``."""

    kind: str
    value: Any = None
    screen: str = ""


ScreenSource = Screen | Sequence[str] | bytes | str


def _as_screen(value: ScreenSource) -> Screen:
    if isinstance(value, Screen):
        return value
    if isinstance(value, (bytes, bytearray)):
        return Screen(png=bytes(value))
    if isinstance(value, str):
        return Screen(tuple(value.splitlines()))
    return Screen(tuple(value))


@dataclass
class _Transcript:
    lines: list[str] = field(default_factory=list)
    buffer: str = ""


class FakeConsole:
    """A ``keyhold.types.Console`` that plays a script instead of driving a VM.

    Attributes tests usually read: ``inputs`` (every accepted input, in order), ``entered``
    (every line entered with Enter), ``screen`` (current screen name), ``screenshots`` (how many
    were taken), ``power_cycles`` and ``closed``.
    """

    def __init__(
        self,
        screens: Mapping[str, ScreenSource] | Sequence[str] | bytes | None = None,
        *,
        start: str | None = None,
        rules: Sequence[Rule] = (),
        on_power_cycle: str | None = None,
        size: tuple[int, int] = DEFAULT_SIZE,
    ) -> None:
        if screens is None:
            screens = {"screen": Screen(["login:"])}
        elif not isinstance(screens, Mapping):
            screens = {"screen": screens}
        if not screens:
            raise ValueError("FakeConsole needs at least one screen")
        self.screens: dict[str, Screen] = {name: _as_screen(s) for name, s in screens.items()}
        self.start = start if start is not None else next(iter(self.screens))
        self.rules = list(rules)
        self.on_power_cycle = on_power_cycle
        self.size = size
        for name in [self.start, on_power_cycle, *(r.goto for r in self.rules), *(r.on for r in self.rules)]:
            if name is not None and name not in self.screens:
                raise ValueError(f"unknown screen {name!r}")

        self.screen = self.start
        self.inputs: list[InputRecord] = []
        self.entered: list[str] = []
        self.screenshots = 0
        self.power_cycles = 0
        self.closed = False
        self._transcript = _Transcript()
        self._fired: set[int] = set()
        self._errors: dict[str, BaseException] = {}
        self._listeners: list[Callable[[InputRecord], None]] = []

    # ------------------------------------------------------------------ Console protocol

    def screenshot(self) -> bytes:
        self._check("screenshot")
        self.screenshots += 1
        screen = self.screens[self.screen]
        if screen.png is not None:
            return screen.png
        return render_text_png(self.visible_lines(), self.size)

    def type_text(self, text: str, abort: Callable[[], bool] | None = None) -> None:
        """Type ``text``. With ``abort``, ask it before every key, like the QEMU console asks it
        before every batch and before every Enter; on yes the rest is not typed and ``InputCut``
        is raised (an Enter that was not reached never runs the line)."""
        self._check("type_text")
        if not isinstance(text, str):
            raise TypeError("type_text takes a string")
        chords = text_to_chords(text)  # raises UnsupportedText before anything is recorded
        for i, chord in enumerate(chords):
            if abort is not None and abort():
                if i:  # the text was recorded whole before its first key; note where it was cut
                    self.inputs.append(InputRecord("cut", i, self.screen))
                raise InputCut("the hold ended while typing", sent=i, total=len(chords), enter_withheld=["ret"] in chords[i:], enter_sent=["ret"] in chords[:i])
            if i == 0:
                self._record(InputRecord("type", text, self.screen))
            if chord == ["ret"]:
                self._enter()
            else:
                self._transcript.buffer += char_for_chord(chord) or ""

    def press(self, keys: list[str]) -> None:
        self._check("press")
        chord = normalize_chord(keys)  # raises UnsupportedKey before anything is recorded
        self._record(InputRecord("press", list(chord), self.screen))
        if chord in (["ret"], ["kp_enter"]):
            self._enter()
            return
        if chord == ["backspace"]:
            self._transcript.buffer = self._transcript.buffer[:-1]
        elif chord in (["ctrl", "c"], ["ctrl_r", "c"]):
            self._commit_line(self._shown_input() + "^C")
        elif chord in (["ctrl", "u"], ["ctrl_r", "u"]):
            self._transcript.buffer = ""
        for index, rule in enumerate(self.rules):
            if rule.keys is not None and tuple(chord) == rule.keys and self._applies(index, rule):
                self._fire(index, rule)
                break

    def power_cycle(self) -> None:
        self._check("power_cycle")
        self.power_cycles += 1
        self._record(InputRecord("power_cycle", None, self.screen))
        target = self.on_power_cycle or self.start
        for index, rule in enumerate(self.rules):
            if rule.power_cycle and self._applies(index, rule):
                self._fired.add(index)
                if rule.goto is not None:
                    target = rule.goto
                break
        self._goto(target)

    def close(self) -> None:
        self.closed = True

    # ------------------------------------------------------------------ test helpers

    def visible_lines(self) -> list[str]:
        """The text the current screen shows (empty for PNG screens)."""
        screen = self.screens[self.screen]
        if screen.png is not None:
            return []
        return [*screen.lines, *self._transcript.lines, screen.prompt + self._shown_input() + "_"]

    @property
    def buffer(self) -> str:
        """What has been typed on the current line and not yet entered."""
        return self._transcript.buffer

    def typed_text(self) -> str:
        """Every ``type_text`` argument joined, in order."""
        return "".join(r.value for r in self.inputs if r.kind == "type")

    def fail_next(self, method: str, error: BaseException) -> None:
        """Make the next call to ``method`` (screenshot, type_text, press, power_cycle) raise ``error``."""
        if method not in ("screenshot", "type_text", "press", "power_cycle"):
            raise ValueError(f"unknown method {method!r}")
        self._errors[method] = error

    def on_input(self, listener: Callable[[InputRecord], None]) -> None:
        """Call ``listener`` with each accepted input, after it is recorded."""
        self._listeners.append(listener)

    def goto(self, screen: str) -> None:
        """Switch screens directly (as if the server changed state on its own)."""
        if screen not in self.screens:
            raise ValueError(f"unknown screen {screen!r}")
        self._goto(screen)

    # ------------------------------------------------------------------ internals

    def _check(self, method: str) -> None:
        if self.closed:
            raise ConsoleClosed("console is closed")
        error = self._errors.pop(method, None)
        if error is not None:
            raise error

    def _record(self, record: InputRecord) -> None:
        self.inputs.append(record)
        for listener in self._listeners:
            listener(record)

    def _shown_input(self) -> str:
        return self._transcript.buffer if self.screens[self.screen].echo else ""

    def _commit_line(self, shown: str) -> None:
        self._transcript.lines.append(self.screens[self.screen].prompt + shown)
        self._transcript.buffer = ""

    def _enter(self) -> None:
        line = self._transcript.buffer
        self.entered.append(line)
        self._commit_line(self._shown_input())
        for index, rule in enumerate(self.rules):
            if rule.line is None or not self._applies(index, rule):
                continue
            if re.fullmatch(rule.line, line.strip()):
                self._fire(index, rule)
                return

    def _applies(self, index: int, rule: Rule) -> bool:
        if rule.on is not None and rule.on != self.screen:
            return False
        return not (rule.once and index in self._fired)

    def _fire(self, index: int, rule: Rule) -> None:
        self._fired.add(index)
        if rule.goto is not None and rule.goto != self.screen:
            self._goto(rule.goto)
        self._transcript.lines.extend(rule.output)

    def _goto(self, screen: str) -> None:
        self.screen = screen
        self._transcript = _Transcript()
