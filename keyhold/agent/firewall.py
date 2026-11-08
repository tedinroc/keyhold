"""Keystroke firewall implementing ``keyhold.types.Firewall``.

Refuses destructive commands before they reach the console: mkfs, dd of=/dev/*, rm -rf /, shred,
wipefs, reading /etc/shadow, writing authorized_keys, changing the root password.

How it works
------------
The firewall keeps a model of the console's current input line and checks input in two places:

* **When text is typed.** Text with a forbidden tool name (mkfs, wipefs, shred, blkdiscard,
  chpasswd), the shadow password files, or a keyboard-map setting anywhere in it is refused, so
  it never reaches the screen. This also covers boot-loader lines and editors, where Enter is
  not the trigger.
* **When Enter is pressed** (``\\n`` in typed text, or ``ret``/``kp_enter``/``ctrl+m``/``ctrl+j``
  with any modifiers). The whole logical command (including continuation lines after ``\\``,
  open quotes, here-documents and ``&&``/``|`` at the end of a line) is lexed like a shell
  would: quotes and backslashes removed, ``$'...'`` decoded, known variables expanded,
  ``sudo``/``env``/``busybox``/``xargs`` and similar wrappers skipped, ``$(...)``, backticks,
  ``sh -c`` strings and here-document bodies checked recursively, and bash history expansion
  (``!!``, ``^a^b``) refused. Then the rules below run on every command in the line.

The line model only follows edits that behave the same in bash, busybox ash, dash and a plain
terminal: typed characters are appended; backspace, ctrl+u, ctrl+w, ctrl+c and ctrl+d on an
empty line are applied. Every other key (arrows, Home/End, ctrl+a/e/k/y, Escape, Alt, tab,
history keys, function keys) may move the cursor in one shell and insert raw bytes in another,
so it marks the line as unscreened instead of being guessed at.

A refused input is not sent and the firewall's model of the line does not change. If Enter is
refused, the text typed before it is still on the console's line; ``ctrl+c`` clears it.

Rules (``FirewallVerdict.rule``)
--------------------------------
mkfs, dd-device (dd of=/dev/..., redirects/tee/cp onto a disk device), rm-root (rm, find -delete
or mv on /, a top-level or critical system directory, or everything in one), recursive-root
(chmod/chown/chgrp -R on those), shred, wipefs, disk-erase (blkdiscard and similar),
partition-table (fdisk, sfdisk, parted... except listing), shadow-read (any use of /etc/shadow or
gshadow, a recursive grep, archive or copy of /etc, or a raw disk read with text tools),
authorized-keys-write (anything but reading or chmod/chown on authorized_keys, any edit of
the AuthorizedKeys* sshd options, even spelled with an expansion inside the word, and any write
into /etc/ssh whose text holds an expansion), root-password (passwd, chpasswd, usermod -p, vipw),
obfuscated (eval, variables or substitutions as the command
name, code piped or here-doc'd into a shell or interpreter, inline code for python, perl, ruby,
node, php or lua, awk programs that run commands or write files, sed scripts that execute,
starting an interpreter with no script (a REPL reads lines that are not shell), running a script
written earlier in the session, base64/xxd decoding, printf/echo -e with hex or octal escapes,
history expansion, fc), keyboard-map (stty, bind, set -o vi, loadkeys, inputrc, console keymaps),
sysrq (Magic SysRq chords), unscreened-line, non-ascii, control-char, caps-lock, unknown-key,
too-long.

Variables. The firewall remembers simple ``NAME=value`` assignments so that a refusal can see
through ``rm -rf $D``, but a remembered value never makes a line pass: every line is screened a
second time with every variable from earlier lines unknown, and refused if either screen
refuses. Starting or leaving a shell (bash, su, sudo -i, exec, exit, logout, login, a reboot)
and any key the firewall cannot follow forget the variables and the working directory.

Limits (stated plainly)
-----------------------
This is a screen on keystrokes, not a sandbox. Enter on an unscreened line is refused if
anything was typed into it, a continuation is pending, or a key that recalls shell history
(up, down, page up/down, ctrl+p/n/r/s/o) was pressed; Enter after other keys alone (Escape, a
function key) is allowed. In a boot menu the agent picks an entry with the arrows and boots it
with ``e`` then ``ctrl+x``, which needs no Enter. Scripts that already exist on the server run
unchecked when called by name, a destination assembled over several commands from file
contents cannot be followed, and a raw disk image copied to a file first and read later is not
recognised. The firewall assumes a US keyboard layout and refuses Caps Lock.
"""

from __future__ import annotations

import copy
import posixpath
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

from keyhold.agent.keys import KEY_NAMES, MAX_CHORD, chord_char, main_keys, modifiers_of
from keyhold.types import FirewallVerdict

ALLOW = FirewallVerdict(True)

MAX_LINE = 8192
MAX_DEPTH = 5

REASONS: Mapping[str, str] = {
    "mkfs": "Making a filesystem erases what is on the disk.",
    "dd-device": "Writing straight to a disk device can destroy the filesystem.",
    "rm-root": "Deleting or moving a system directory, or everything in one, would destroy the system.",
    "recursive-root": "Recursive owner or permission changes on a system directory would break the system.",
    "shred": "shred overwrites files so they cannot be recovered.",
    "wipefs": "wipefs erases filesystem and partition signatures.",
    "disk-erase": "This command erases or discards data on a disk.",
    "partition-table": "Changing the partition table can make the disk unreadable.",
    "shadow-read": "The password hashes in /etc/shadow and /etc/gshadow are off limits.",
    "authorized-keys-write": "Changing which SSH keys may log in is off limits.",
    "root-password": "Changing passwords is off limits.",
    "obfuscated": "Commands that are built at run time cannot be checked.",
    "keyboard-map": (
        "Changing the keyboard map, terminal special characters or line-editing bindings would "
        "change what keystrokes mean, so the firewall could no longer follow them."
    ),
    "sysrq": (
        "Magic SysRq chords reboot or change the kernel without going through the shell. Use the "
        "power_cycle tool to restart the server."
    ),
    "unscreened-line": (
        "The line was edited with keys the firewall cannot follow (history, tab completion, "
        "Escape or Alt sequences, editor commands), so it cannot be checked."
    ),
    "non-ascii": "Only plain ASCII text can be typed on this console.",
    "control-char": "Control characters must be sent with the key tool, not typed.",
    "caps-lock": "Caps Lock is refused; use shift for capital letters.",
    "unknown-key": "That key is not on the console keyboard.",
    "too-long": "The command is too long to check.",
}


def refuse(rule: str, detail: str = "") -> FirewallVerdict:
    reason = REASONS[rule]
    if detail:
        reason = f"{reason} {detail}"
    return FirewallVerdict(False, rule, reason)


# --------------------------------------------------------------------------------------------
# Keyword scan (applied to every typed fragment and to the modelled line)
# --------------------------------------------------------------------------------------------

_KEYWORDS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(
            r"(?<![\w-])mk(?:fs[\w.-]*|e2fs|swap|dosfs|ntfs|exfatfs|xfs|reiserfs|udffs|btrfs|jfs)(?![\w-])",
            re.I,
        ),
        "mkfs",
        "Found a mkfs-family tool name.",
    ),
    (re.compile(r"(?<![\w-])wipefs(?![\w-])", re.I), "wipefs", "Found 'wipefs'."),
    (re.compile(r"(?<![\w-])shred(?![\w-])", re.I), "shred", "Found 'shred'."),
    (re.compile(r"(?<![\w-])blkdiscard(?![\w-])", re.I), "disk-erase", "Found 'blkdiscard'."),
    (re.compile(r"(?<![\w-])chpasswd(?![\w-])", re.I), "root-password", "Found 'chpasswd'."),
    (re.compile(r"(?<![\w-])g?shadow(?!\w)", re.I), "shadow-read", "Found a shadow password file name."),
    (
        re.compile(
            r"(?<![\w-])(loadkeys|setkeycodes|setupcon|kbd_mode|inputrc|vconsole\.conf|console-setup)(?![\w-])"
            r"|/etc/default/keyboard|set-(x11-)?keymap",
            re.I,
        ),
        "keyboard-map",
        "Found a keyboard map or line-editor setting.",
    ),
)


def history_expansion(text: str) -> bool:
    """True if an interactive bash would apply history expansion to ``text`` (``!!``, ``!-2``,
    ``!rm``, ``!$``, ``^old^new``), which runs text the firewall never saw."""
    if re.search(r"(^|\n)[ \t]*\^", text):
        return True
    in_single = in_double = False
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\\" and not in_single:
            i += 2
            continue
        if c == "'" and not in_double:
            in_single = not in_single
        elif c == '"' and not in_single:
            in_double = not in_double
        elif c == "!" and not in_single:
            nxt = text[i + 1] if i + 1 < len(text) else ""
            prev = text[i - 1] if i > 0 else ""
            if nxt and nxt not in " \t\n=(\"" and prev != "$":
                return True
        i += 1
    return False

_DEQUOTE = str.maketrans("", "", "'\"\\")


def scan_keywords(text: str) -> FirewallVerdict | None:
    """Refuse text that names a forbidden tool or the shadow files anywhere, quoted or not."""
    for candidate in (text, text.translate(_DEQUOTE)):
        for pattern, rule, detail in _KEYWORDS:
            if pattern.search(candidate):
                return refuse(rule, detail)
    return None


# --------------------------------------------------------------------------------------------
# Lexer: just enough of the shell grammar to find commands, arguments and redirects
# --------------------------------------------------------------------------------------------

WILD = "\x00"  # stands for text the firewall cannot know (expansions); matches anything

_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(\[[^\]]*\])?\+?=")
_OPERATORS = (";;&", ";;", ";&", ";", "&&", "&", "||", "|&", "|", "(", ")")
_REDIRECTS = ("<<<", "<<-", "<<", "<>", "<&", ">>", ">&", ">|", "<", ">")
_PIPES = ("|", "|&")
_CONTINUATION_OPS = ("|", "|&", "&&", "||")


@dataclass
class Word:
    text: str
    quoted: bool = False

    @property
    def wild(self) -> bool:
        return WILD in self.text

    @property
    def bare_wild(self) -> bool:
        """True when the word is nothing but unknown expansions."""
        return bool(self.text) and self.text.strip(WILD) == ""


@dataclass
class Redirect:
    op: str
    target: Word | None
    fd: str | None = None


@dataclass
class Command:
    words: list[Word] = field(default_factory=list)
    redirects: list[Redirect] = field(default_factory=list)
    piped_in: bool = False


@dataclass
class Parsed:
    commands: list[Command]
    subs: list[str]  # bodies of $(...), `...`, <(...), >(...) and ${...:-$(...)}
    heredocs: list[str]
    incomplete: bool


class _Lexer:
    def __init__(self, text: str, env: Mapping[str, str]) -> None:
        self.s = text
        self.n = len(text)
        self.i = 0
        self.env = env
        self.tokens: list[tuple] = []
        self.subs: list[str] = []
        self.heredocs: list[str] = []
        self.incomplete = False
        self.pending_heredocs: list[tuple[str, bool]] = []  # (delimiter, strip tabs)
        self.expect_delim: str | None = None
        self._reset()

    # word assembly ---------------------------------------------------------------------------

    def _reset(self) -> None:
        self.buf: list[str] = []
        self.in_word = False
        self.quoted = False

    def _lit(self, text: str, *, quoted: bool = False) -> None:
        self.in_word = True
        self.buf.append(text)
        if quoted:
            self.quoted = True

    def _wild(self) -> None:
        self.in_word = True
        self.buf.append(WILD)

    def _end_word(self) -> None:
        if not self.in_word:
            return
        word = Word("".join(self.buf), self.quoted)
        if self.expect_delim is not None:
            self.pending_heredocs.append((word.text.replace(WILD, ""), self.expect_delim == "<<-"))
            self.expect_delim = None
        self.tokens.append(("word", word))
        self._reset()

    # scanning helpers ------------------------------------------------------------------------

    def _skip_quoted(self, j: int) -> int:
        """``s[j]`` is a quote; return the index after its closing quote, or -1."""
        q = self.s[j]
        j += 1
        while j < self.n:
            ch = self.s[j]
            if ch == "\\" and q == '"':
                j += 2
                continue
            if ch == q:
                return j + 1
            j += 1
        return -1

    def _match(self, start: int, open_ch: str, close_ch: str) -> int:
        """``s[start] == open_ch``; return the index of the matching ``close_ch`` or -1."""
        depth = 0
        j = start
        while j < self.n:
            ch = self.s[j]
            if ch == "\\":
                j += 2
                continue
            if ch in "'\"":
                j = self._skip_quoted(j)
                if j < 0:
                    return -1
                continue
            if ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    return j
            j += 1
        return -1

    def _sub_until(self, body_start: int, close: int) -> None:
        if close < 0:
            self.incomplete = True
            self.subs.append(self.s[body_start:])
            self.i = self.n
        else:
            self.subs.append(self.s[body_start:close])
            self.i = close + 1
        self._wild()

    # main loop -------------------------------------------------------------------------------

    def run(self) -> list[tuple]:
        s = self.s
        while self.i < self.n:
            c = s[self.i]
            if c == "\\":
                if self.i + 1 >= self.n:
                    self.incomplete = True  # trailing backslash: the line continues
                    self.i += 1
                    continue
                nxt = s[self.i + 1]
                self.i += 2
                if nxt != "\n":
                    self._lit(nxt, quoted=True)
                continue
            if c == "'":
                end = s.find("'", self.i + 1)
                if end < 0:
                    self.incomplete = True
                    self._lit(s[self.i + 1 :], quoted=True)
                    self.i = self.n
                else:
                    self._lit(s[self.i + 1 : end], quoted=True)
                    self.i = end + 1
                continue
            if c == '"':
                self._double_quoted()
                continue
            if c == "$":
                self._dollar(in_dq=False)
                continue
            if c == "`":
                self._backtick()
                continue
            if c in " \t":
                self._end_word()
                self.i += 1
                continue
            if c == "\n":
                self._end_word()
                self.tokens.append(("op", "\n"))
                self.i += 1
                self._read_heredocs()
                continue
            if c == "#" and not self.in_word:
                end = s.find("\n", self.i)
                self.i = self.n if end < 0 else end
                continue
            if c in "<>" and not self.in_word and s.startswith("(", self.i + 1):
                start = self.i + 1
                self._sub_until(start + 1, self._match(start, "(", ")"))
                continue
            if c in "<>" or (c == "&" and s.startswith(">", self.i + 1)):
                self._redirect()
                continue
            if c in ";&|()":
                self._end_word()
                for op in _OPERATORS:
                    if s.startswith(op, self.i):
                        self.tokens.append(("op", op))
                        self.i += len(op)
                        break
                continue
            if c == "{" and self._brace_expansion():
                continue
            self._lit(c)
            self.i += 1
        self._end_word()
        if self.pending_heredocs or self.expect_delim is not None:
            self.incomplete = True
        return self.tokens

    def _double_quoted(self) -> None:
        s = self.s
        self.i += 1
        self._lit("", quoted=True)
        while True:
            if self.i >= self.n:
                self.incomplete = True
                return
            c = s[self.i]
            if c == '"':
                self.i += 1
                return
            if c == "\\" and self.i + 1 < self.n:
                nxt = s[self.i + 1]
                if nxt == "\n":
                    pass
                elif nxt in '$`"\\':
                    self._lit(nxt, quoted=True)
                else:
                    self._lit("\\" + nxt, quoted=True)
                self.i += 2
                continue
            if c == "$":
                self._dollar(in_dq=True)
                continue
            if c == "`":
                self._backtick()
                continue
            self._lit(c, quoted=True)
            self.i += 1

    def _dollar(self, *, in_dq: bool) -> None:
        s = self.s
        nxt = s[self.i + 1] if self.i + 1 < self.n else ""
        if nxt == "'" and not in_dq:
            self._ansi_c()
            return
        if nxt == '"' and not in_dq:
            self.i += 1
            self._double_quoted()
            return
        if nxt == "(":
            start = self.i + 1
            self._sub_until(start + 1, self._match(start, "(", ")"))
            return
        if nxt == "{":
            start = self.i + 1
            close = self._match(start, "{", "}")
            if close < 0:
                self.incomplete = True
                self.i = self.n
                self._wild()
                return
            inner = s[start + 1 : close]
            self.i = close + 1
            if _NAME_RE.fullmatch(inner) and inner in self.env:
                self._lit(self.env[inner], quoted=True)
            else:
                if "$(" in inner or "`" in inner:
                    self.subs.append(inner)
                self._wild()
            return
        if nxt and (nxt.isalpha() or nxt == "_"):
            m = _NAME_RE.match(s, self.i + 1)
            assert m is not None
            name = m.group(0)
            self.i = m.end()
            if name in self.env:
                self._lit(self.env[name], quoted=True)
            else:
                self._wild()
            return
        if nxt and nxt in "0123456789@*#?$!-":
            self.i += 2
            self._wild()
            return
        self._lit("$")
        self.i += 1

    def _ansi_c(self) -> None:
        s = self.s
        j = self.i + 2
        out: list[str] = []
        simple = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b",
                  "f": "\f", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?"}
        while j < self.n:
            ch = s[j]
            if ch == "'":
                self._lit("".join(out), quoted=True)
                self.i = j + 1
                return
            if ch == "\\" and j + 1 < self.n:
                e = s[j + 1]
                if e in simple:
                    out.append(simple[e])
                    j += 2
                    continue
                m = re.match(r"x([0-9a-fA-F]{1,2})", s[j + 1 :])
                if m:
                    out.append(chr(int(m.group(1), 16)))
                    j += 1 + m.end()
                    continue
                m = re.match(r"[uU]([0-9a-fA-F]{1,8})", s[j + 1 :])
                if m:
                    try:
                        out.append(chr(int(m.group(1), 16)))
                    except (ValueError, OverflowError):
                        out.append(WILD)
                    j += 1 + m.end()
                    continue
                m = re.match(r"([0-7]{1,3})", s[j + 1 :])
                if m:
                    out.append(chr(int(m.group(1), 8) & 0xFF))
                    j += 1 + m.end()
                    continue
                m = re.match(r"c(.)", s[j + 1 :])
                if m:
                    out.append(chr(ord(m.group(1)) & 0x1F))
                    j += 1 + m.end()
                    continue
                out.append("\\" + e)
                j += 2
                continue
            out.append(ch)
            j += 1
        self.incomplete = True
        self._lit("".join(out), quoted=True)
        self.i = self.n

    def _backtick(self) -> None:
        s = self.s
        j = self.i + 1
        body: list[str] = []
        while j < self.n:
            ch = s[j]
            if ch == "\\" and j + 1 < self.n and s[j + 1] in "`$\\":
                body.append(s[j + 1])
                j += 2
                continue
            if ch == "`":
                self.subs.append("".join(body))
                self.i = j + 1
                self._wild()
                return
            body.append(ch)
            j += 1
        self.incomplete = True
        self.subs.append("".join(body))
        self.i = self.n
        self._wild()

    def _redirect(self) -> None:
        s = self.s
        fd = None
        if self.in_word and self.buf and not self.quoted and "".join(self.buf).isdigit():
            fd = "".join(self.buf)
            self._reset()
        else:
            self._end_word()
        if s.startswith("&>>", self.i):
            op = "&>>"
        elif s.startswith("&>", self.i):
            op = "&>"
        else:
            op = next(o for o in _REDIRECTS if s.startswith(o, self.i))
        self.i += len(op)
        self.tokens.append(("redir", op, fd))
        if op in ("<<", "<<-"):
            self.expect_delim = op

    def _brace_expansion(self) -> bool:
        """``{a,b}`` or ``{1..9}`` inside a word: an expansion the firewall treats as unknown."""
        s = self.s
        j = self.i + 1
        depth = 1
        while j < self.n:
            ch = s[j]
            if ch in " \t\n;|&<>()":
                return False
            if ch == "\\":
                j += 2
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if depth != 0:
            return False
        inner = s[self.i + 1 : j]
        if "," not in inner and ".." not in inner:
            return False
        self._wild()
        self.i = j + 1
        return True

    def _read_heredocs(self) -> None:
        s = self.s
        while self.pending_heredocs:
            delim, strip = self.pending_heredocs.pop(0)
            body: list[str] = []
            found = False
            while self.i < self.n:
                end = s.find("\n", self.i)
                line = s[self.i :] if end < 0 else s[self.i : end]
                self.i = self.n if end < 0 else end + 1
                if (line.lstrip("\t") if strip else line) == delim:
                    found = True
                    break
                body.append(line)
            self.heredocs.append("\n".join(body))
            if not found:
                self.incomplete = True
                self.pending_heredocs.clear()
                return


_RESERVED_PREFIX = frozenset({"!", "if", "then", "else", "elif", "do", "while", "until", "time", "coproc"})
_RESERVED_SKIP = frozenset({"for", "select", "case", "function", "fi", "done", "esac", "in", "}", "{"})


_SETS_VARS_RE = re.compile(r"(?<![\w-])(for|select|read|mapfile|readarray|getopts|let|printf)(?![\w-])")


def _line_env(text: str, env: Mapping[str, str]) -> dict[str, str]:
    """The variables to expand in ``text``: the session's, overridden by literal NAME=value
    assignments in ``text``. A name assigned more than once, assigned a value that is not
    literal, or possibly set by for/read/printf -v in this line is treated as unknown."""
    merged = dict(env)
    found: dict[str, set[str]] = {}
    for tok in _Lexer(text, {}).run():
        if tok[0] != "word":
            continue
        w: Word = tok[1]
        m = _ASSIGN_RE.match(w.text)
        if not m:
            continue
        value = w.text[m.end() :] if not m.group(2) else WILD
        found.setdefault(m.group(1), set()).add(value)
    for name, values in found.items():
        value = next(iter(values))
        if len(values) == 1 and WILD not in value:
            merged[name] = value
        else:
            merged.pop(name, None)
    if _SETS_VARS_RE.search(text):
        for name in list(merged):
            if re.search(rf"(?<![\w$]){re.escape(name)}(?!\w)", text):
                merged.pop(name, None)
    return merged


def parse(text: str, env: Mapping[str, str] | None = None) -> Parsed:
    """Lex ``text`` into simple commands. Variables in ``env`` (and literal assignments made in
    ``text`` itself) are expanded; everything else that expands becomes ``WILD``."""
    merged = _line_env(text, env or {})
    lexer = _Lexer(text, merged)
    tokens = lexer.run()

    commands: list[Command] = []
    cur = Command()
    piped = False
    expect: tuple[str, str | None] | None = None
    last_op: str | None = None
    opens = 0

    def flush() -> None:
        nonlocal cur
        if cur.words or cur.redirects:
            commands.append(cur)
        cur = Command()

    for tok in tokens:
        kind = tok[0]
        if kind == "word":
            word: Word = tok[1]
            last_op = None
            if expect is not None:
                cur.redirects.append(Redirect(expect[0], word, expect[1]))
                expect = None
                continue
            if not word.quoted and word.text in ("{", "}"):
                opens += 1 if word.text == "{" else -1
                flush()
                cur.piped_in = False
                continue
            if not cur.words and not cur.redirects:
                cur.piped_in = piped
            cur.words.append(word)
        elif kind == "redir":
            last_op = None
            if not cur.words and not cur.redirects:
                cur.piped_in = piped
            expect = (tok[1], tok[2])
        else:
            op = tok[1]
            if op == "(":
                opens += 1
            elif op == ")":
                opens -= 1
            if op == "\n" and not cur.words and not cur.redirects and last_op in _CONTINUATION_OPS:
                continue  # newline after a pipe or && keeps the command going
            flush()
            piped = op in _PIPES
            last_op = op
    if expect is not None:
        lexer.incomplete = True
    flush()

    depth = 0
    for cmd in commands:
        first = next((w.text for w in cmd.words if not w.quoted), None) if cmd.words else None
        if first in ("if", "case"):
            depth += 1
        elif first in ("fi", "esac"):
            depth -= 1
        for w in cmd.words[:2]:
            if not w.quoted and w.text == "do":
                depth += 1
            elif not w.quoted and w.text == "done":
                depth -= 1
    incomplete = (
        lexer.incomplete or last_op in _CONTINUATION_OPS or opens > 0 or depth > 0
    )
    return Parsed(commands, lexer.subs, lexer.heredocs, incomplete)


# --------------------------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------------------------

_TOP_DIRS = (
    "bin boot dev etc home lib lib32 lib64 libx32 media mnt opt proc root run sbin snap srv sys "
    "tmp usr var sysroot target"
).split()
_CRITICAL_SUBDIRS = (
    "/usr/bin /usr/sbin /usr/lib /usr/lib32 /usr/lib64 /usr/libexec /usr/local /usr/share "
    "/usr/include /usr/src /var/lib /etc/ssh /etc/systemd /etc/default /etc/network /etc/pam.d "
    "/etc/security /boot/grub /boot/efi /root/.ssh"
).split()
PROTECTED_DIRS: tuple[str, ...] = ("/", *(f"/{d}" for d in _TOP_DIRS), *_CRITICAL_SUBDIRS)
_CONTENTS_OK = frozenset({"/tmp", "/var/tmp"})
_MOUNT_PREFIXES = ("/mnt", "/sysroot", "/target", "/a", "/media/root")
_UNKNOWN_CWD_BASES = ("/", "/root", "/usr", "/var", "/etc", "/boot")

_SAFE_DEV = frozenset(
    {"/dev/null", "/dev/zero", "/dev/full", "/dev/random", "/dev/urandom", "/dev/stdout", "/dev/stderr",
     "/dev/stdin", "/dev/tty", "/dev/console", "/dev/kmsg", "/dev/ptmx", "/dev/fd/1", "/dev/fd/2"}
)
_SAFE_DEV_RE = re.compile(r"^/dev/(tty\d*|pts/\d+|ttyS\d+)$")
_PURE_GLOB_RE = re.compile(r"^(\*|\?|\[[^\]]*\])+$")
_SHADOW_NAMES = ("shadow", "gshadow", "shadow-", "gshadow-")
_SHADOW_RE = re.compile(r"^g?shadow([-~]|\.[\w.-]+)?$")
_KEYS_RE = re.compile(r"^authorized_keys[\w.~-]*$")
_KEYS_NAMES = ("authorized_keys", "authorized_keys2")


def _glob(text: str) -> str:
    return text.replace(WILD, "*")


def _norm(path: str) -> str:
    path = re.sub(r"^/proc/[^/]+/root(?=/|$)", "", path) or "/"
    norm = posixpath.normpath(path)
    if norm.startswith("//"):
        norm = "/" + norm.lstrip("/")
    return norm


def resolve(text: str, cwd: str | None) -> list[str]:
    """Absolute, normalized glob patterns that ``text`` may name. Unknown text becomes ``*``.

    With an unknown working directory a relative path is tried against several likely bases.
    """
    t = _glob(text)
    if t.startswith("~"):
        user, _, rest = t[1:].partition("/")
        home = "/root" if user == "" else f"/home/{user}"
        t = home + ("/" + rest if rest else "")
    if t.startswith("/"):
        return [_norm(t)]
    if cwd is not None:
        return [_norm(posixpath.join(cwd, t))]
    return [_norm(posixpath.join(base, t)) for base in _UNKNOWN_CWD_BASES]


def _literal_chars(component: str) -> int:
    return len(re.sub(r"\*|\?|\[[^\]]*\]", "", component))


def _value_parts(word: Word) -> list[str]:
    """The word and, for ``key=value`` or ``--opt=value`` words, the value part."""
    parts = [word.text]
    if "=" in word.text:
        parts.append(word.text.split("=", 1)[1])
    return [p for p in parts if p]


def _mentions(word: Word, cwd: str | None, name_re: re.Pattern[str], names: Sequence[str],
              min_literal: int, parent_name: str) -> bool:
    """Does ``word`` name one of ``names``? Literal paths match by file name in any directory
    (so /mnt/etc/shadow counts). A glob matches when its last part has enough literal text to
    point at one of the names, or when it is spelled inside ``parent_name`` (``/etc/*``)."""
    for part in _value_parts(word):
        if part.strip(WILD) == "":
            continue  # nothing but unknown expansions: no evidence either way
        for pattern in resolve(part, cwd):
            parent, last = posixpath.split(pattern)
            if name_re.match(last):
                return True
            if not any(ch in last for ch in "*?["):
                continue
            if _literal_chars(last) >= min_literal and any(fnmatchcase(n, last) for n in names):
                return True
            spelled_dir = "/" in _glob(part) or cwd is not None
            if spelled_dir and fnmatchcase(parent_name, posixpath.basename(parent)) and any(
                fnmatchcase(n, last) for n in names
            ):
                return True
    return False


def mentions_shadow(word: Word, cwd: str | None) -> bool:
    return _mentions(word, cwd, _SHADOW_RE, _SHADOW_NAMES, 3, "etc")


def could_spell(text: str, target: str, min_overlap: int = 3) -> bool:
    """Could ``text``, once its unknown parts (``WILD``) expand, contain ``target``? Checked by
    the literal pieces around each unknown part: one that ends with the start of ``target``,
    starts with its end, or sits inside it is enough (``Authorized$(echo K)eysCommand``)."""
    low = text.lower()
    if target in low.replace(WILD, ""):
        return True
    if WILD not in low:
        return False
    pieces = low.split(WILD)
    for i, piece in enumerate(pieces):
        piece = piece.strip(" \t\"'")
        if len(piece) < min_overlap:
            continue
        before_wild = i < len(pieces) - 1
        after_wild = i > 0
        for n in range(min(len(piece), len(target)), min_overlap - 1, -1):
            if before_wild and piece.endswith(target[:n]):
                return True
            if after_wild and piece.startswith(target[-n:]):
                return True
        if piece in target:
            return True
    return False


def mentions_keys(word: Word, cwd: str | None) -> bool:
    if could_spell(word.text, "authorizedkeys"):
        return True
    return _mentions(word, cwd, _KEYS_RE, _KEYS_NAMES, 4, ".ssh")


def is_unsafe_device(word: Word, cwd: str | None) -> bool:
    """True if writing to ``word`` could hit a disk device (or the target cannot be known)."""
    if word.bare_wild:
        return True
    for pattern in resolve(word.text, cwd):
        if not pattern.startswith("/dev/") and not fnmatchcase("/dev/x", pattern):
            continue
        if pattern in _SAFE_DEV or _SAFE_DEV_RE.match(pattern):
            continue
        return True
    return False


def system_dir_hit(text: str, cwd: str | None, *, contents_too: bool = True) -> str | None:
    """The protected directory that ``text`` names (or empties, with a glob), else None."""
    for pattern in resolve(text, cwd):
        candidates = [pattern]
        for prefix in _MOUNT_PREFIXES:
            if pattern.startswith(prefix + "/"):
                candidates.append(pattern[len(prefix) :] or "/")
        for p in candidates:
            for d in PROTECTED_DIRS:
                if fnmatchcase(d, p):
                    return d
            comps = [c for c in p.split("/") if c]
            if len(comps) == 2 and fnmatchcase("home", comps[0]):
                return "/home/" + comps[1].replace("*", "<user>")
            if not contents_too:
                continue
            parent, last = posixpath.split(p)
            if last and _PURE_GLOB_RE.match(last):
                for d in PROTECTED_DIRS:
                    if d not in _CONTENTS_OK and fnmatchcase(d, parent):
                        return d + "/*"
                pcomps = [c for c in parent.split("/") if c]
                if len(pcomps) == 2 and fnmatchcase("home", pcomps[0]):
                    return parent + "/*"
    return None


# --------------------------------------------------------------------------------------------
# Command rules
# --------------------------------------------------------------------------------------------

# wrapper name -> (options that take a separate value, positional arguments before the command)
_WRAPPERS: Mapping[str, tuple[frozenset[str], int]] = {
    "sudo": (frozenset("-u -g -C -D -h -p -r -t -U -T --user --group --close-from --chdir --host "
                       "--prompt --role --type --other-user --command-timeout".split()), 0),
    "doas": (frozenset({"-u", "-C"}), 0),
    "run0": (frozenset("-u --user -g --group -D --chdir --unit --property --description --slice "
                       "--nice --machine --setenv".split()), 0),
    "env": (frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}), 0),
    "command": (frozenset(), 0),
    "builtin": (frozenset(), 0),
    "exec": (frozenset({"-a"}), 0),
    "nohup": (frozenset(), 0),
    "setsid": (frozenset(), 0),
    "time": (frozenset({"-f", "--format", "-o", "--output"}), 0),
    "nice": (frozenset({"-n", "--adjustment"}), 0),
    "ionice": (frozenset({"-c", "--class", "-n", "--classdata", "-p", "--pid", "-P", "--pgid", "-u", "--uid"}), 0),
    "stdbuf": (frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}), 0),
    "timeout": (frozenset({"-s", "--signal", "-k", "--kill-after"}), 1),
    "chroot": (frozenset({"--userspec", "--groups"}), 1),
    "unshare": (frozenset(), 0),
    "nsenter": (frozenset({"-t", "--target", "-S", "--setuid", "-G", "--setgid"}), 0),
    "flock": (frozenset({"-w", "--timeout", "-E", "--conflict-exit-code"}), 1),
    "xargs": (frozenset("-I -n -P -d -a -L -s -E --max-args --max-procs --delimiter --arg-file "
                        "--max-lines --max-chars --eof --replace".split()), 0),
    "strace": (frozenset("-o -e -p -s -u -E -a -b -I -O -S -X -P".split()), 0),
    "ltrace": (frozenset("-o -e -p -s -u -n -a -l".split()), 0),
    "systemd-run": (frozenset("-p --property -u --unit -E --setenv --description --slice -M --machine "
                              "--uid --gid --nice --working-directory -H --host".split()), 0),
    "busybox": (frozenset(), 0),
    "toybox": (frozenset(), 0),
    "fakeroot": (frozenset(), 0),
    "unbuffer": (frozenset(), 0),
    "taskset": (frozenset(), 1),
    "chrt": (frozenset(), 1),
    "watch": (frozenset({"-n", "--interval"}), 0),
}

_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "mksh", "ash", "csh", "tcsh", "fish", "rbash"})
_DASH_C = _SHELLS | frozenset({"su", "runuser", "sg", "flock"})
_INTERPRETERS = frozenset(
    {"perl", "ruby", "node", "nodejs", "php", "lua", "luajit", "tclsh", "wish", "expect", "irb", "pwsh", "R", "Rscript"}
)
_AWKS = frozenset({"awk", "gawk", "mawk", "nawk", "busybox-awk"})
_INLINE_FLAGS = {"perl": ("-e", "-E"), "ruby": ("-e",), "node": ("-e", "-p", "--eval", "--print"),
                 "nodejs": ("-e", "-p", "--eval", "--print"), "php": ("-r",), "lua": ("-e",), "luajit": ("-e",),
                 "Rscript": ("-e",), "pwsh": ("-c", "-Command")}
# awk features that run commands or touch files: system(), getline (from a file or a command),
# pipes, and print/printf redirected to a file.
_AWK_DANGER = re.compile(r"\bsystem\s*\(|\bgetline\b|\||\bprintf?\b[^;{}]*>|\bENVIRON\b|\bclose\s*\(|\bfflush\b")
# Commands that start or leave a shell session: afterwards the firewall knows neither the
# variables nor the working directory of the shell the keys go to.
_SESSION_CHANGERS = _SHELLS | frozenset(
    {"su", "login", "exit", "logout", "reboot", "poweroff", "halt", "shutdown", "init", "telinit", "newgrp",
     "sg", "runuser", "chroot", "nsenter", "machinectl", "systemd-nspawn", "screen", "tmux", "ssh"}
)
_SYSTEMCTL_SESSION = frozenset(
    {"reboot", "poweroff", "halt", "kexec", "emergency", "rescue", "isolate", "default", "soft-reboot"}
)
_STDIN_NAMES = frozenset({"-", "/dev/stdin", "/dev/fd/0", "/proc/self/fd/0"})
_MKFS = frozenset({"mke2fs", "mkswap", "mkdosfs", "mkntfs", "mkexfatfs", "mkxfs", "mkreiserfs", "mkudffs", "mkbtrfs"})
_KEYS_SAFE = frozenset(
    "cat less more head tail grep egrep fgrep rg wc ls stat file md5sum sha1sum sha256sum sha512sum "
    "b2sum diff cmp chmod chown chgrp lsattr getfacl namei test [ [[ realpath readlink basename "
    "dirname true false".split()
)
_GREP = frozenset({"grep", "egrep", "fgrep", "rg", "ag", "ack", "zgrep"})
_ARCHIVERS = frozenset({"tar", "cpio", "zip", "7z", "7za", "rsync", "pax", "star", "bsdtar"})
_RAW_READERS = frozenset(
    "cat strings grep egrep fgrep rg xxd od hexdump hd head tail less more base64 base32 sed awk tac".split()
)
_BLOCK_DEV_RE = re.compile(r"^/dev/(sd|vd|xvd|hd|nvme|mmcblk|dm-|md|loop|mapper/|disk/|root$|\*)")


def _holds_etc(pattern: str) -> bool:
    """Could this path pattern be /etc, /, or a mounted root's /etc (or the mount point itself)?"""
    if fnmatchcase("/etc", pattern) or fnmatchcase("/", pattern):
        return True
    return any(fnmatchcase(p + "/etc", pattern) or fnmatchcase(p, pattern) for p in _MOUNT_PREFIXES)
_WRITE_DEST_LAST = frozenset({"cp", "mv", "install", "rsync", "scp", "ln"})
_DISK_COPIERS = frozenset({"ddrescue", "dcfldd", "dc3dd"})


def _is_python(name: str) -> bool:
    return re.fullmatch(r"python[\d.]*", name) is not None


@dataclass
class _Effective:
    name: str
    word: Word
    args: list[Word]
    via_xargs: bool = False


def _basename(text: str) -> str:
    stripped = text.rstrip("/")
    return stripped.rsplit("/", 1)[-1] if stripped else text


def effective_command(words: Sequence[Word]) -> _Effective | None:
    """Skip assignments, reserved words and wrappers like sudo/env/xargs to find the real command."""
    i = 0
    n = len(words)
    via_xargs = False
    while i < n:
        w = words[i]
        t = w.text
        if not w.quoted and _ASSIGN_RE.match(t):
            i += 1
            continue
        if not w.quoted and t in _RESERVED_PREFIX:
            i += 1
            continue
        if not w.quoted and t in _RESERVED_SKIP:
            return None
        name = _basename(t)
        if name in _WRAPPERS and not w.wild:
            if name == "xargs":
                via_xargs = True
            values, positional = _WRAPPERS[name]
            j = i + 1
            while j < n:
                arg = words[j].text
                if arg == "--":
                    j += 1
                    break
                if name == "env" and _ASSIGN_RE.match(arg):
                    j += 1
                    continue
                if arg.startswith("-") and len(arg) > 1:
                    j += 2 if arg in values else 1
                    continue
                if positional > 0:
                    positional -= 1
                    j += 1
                    continue
                break
            i = j
            continue
        return _Effective(name, w, list(words[i + 1 :]), via_xargs)
    return None


def _options_and_operands(args: Sequence[Word]) -> tuple[list[str], list[Word]]:
    opts: list[str] = []
    operands: list[Word] = []
    done = False
    for a in args:
        if not done and a.text == "--":
            done = True
            continue
        if not done and a.text.startswith("-") and len(a.text) > 1 and not a.wild:
            opts.append(a.text)
        else:
            operands.append(a)
    return opts, operands


def _short_has(opts: Sequence[str], letters: str) -> bool:
    return any(o.startswith("-") and not o.startswith("--") and any(ch in o[1:] for ch in letters) for o in opts)


def _nested_strings(eff: _Effective | None, cmd: Command) -> list[tuple[str, bool]]:
    """Strings inside the command that may run as shell code, each with ``strict``.

    Strict (certainly code): the argument after -c for shells, su and flock, python -c, perl -e
    and similar, env -S, su --command. Not strict (maybe code, maybe a sed script or a message):
    any other argument with whitespace or shell operators, and the value of name='...' words
    (alias). Non-strict strings are still checked by every rule except the ones that refuse
    text merely for being built at run time.
    """
    out: list[tuple[str, bool]] = []
    for w in cmd.words:
        if any(ch in w.text for ch in " \t\n;|&"):
            out.append((w.text, False))
            if "=" in w.text:  # alias name='...', --opt='...'
                out.append((w.text.split("=", 1)[1], False))
    if eff is None:
        return out
    texts = [a.text for a in eff.args]
    for idx, t in enumerate(texts):
        if t.startswith("--command="):
            out.append((t.split("=", 1)[1], True))
        nxt = texts[idx + 1] if idx + 1 < len(texts) else None
        if nxt is None:
            continue
        if eff.name in _DASH_C and t.startswith("-") and not t.startswith("--") and "c" in t[1:]:
            out.append((nxt, True))
        if t == "--command":
            out.append((nxt, True))
        if _is_python(eff.name) and t == "-c":
            out.append((nxt, True))
        if t in _INLINE_FLAGS.get(eff.name, ()):
            out.append((nxt, True))
    words = [w.text for w in cmd.words]
    for idx, t in enumerate(words):
        if "env" not in [_basename(x) for x in words[:idx]]:
            continue
        if t in ("-S", "--split-string") and idx + 1 < len(words):
            out.append((words[idx + 1], True))
        elif t.startswith("--split-string="):
            out.append((t.split("=", 1)[1], True))
    return out


def _check_rm(eff: _Effective, cwd: str | None) -> FirewallVerdict | None:
    opts, targets = _options_and_operands(eff.args)
    if "--no-preserve-root" in opts:
        return refuse("rm-root", "--no-preserve-root is never needed.")
    recursive = "--recursive" in opts or _short_has(opts, "rR")
    if eff.via_xargs:
        return refuse("rm-root", "rm through xargs takes its targets from input, which cannot be checked.")
    for t in targets:
        hit = system_dir_hit(t.text, cwd, contents_too=True)
        if hit and (recursive or hit.endswith("/*")):
            return refuse("rm-root", f"Target {t.text.replace(WILD, '$?')!r} reaches {hit}.")
    return None


def _check_find(eff: _Effective, cwd: str | None) -> FirewallVerdict | None:
    texts = [a.text for a in eff.args]
    starts: list[Word] = []
    i = 0
    while i < len(texts) and texts[i] in ("-H", "-L", "-P"):
        i += 1
    while i < len(eff.args):
        t = texts[i]
        if t.startswith("-") or t in ("(", "!", ","):
            break
        starts.append(eff.args[i])
        i += 1
    if not starts:
        starts = [Word(".")]
    deleting = "-delete" in texts
    for idx, t in enumerate(texts):
        if t in ("-exec", "-execdir", "-ok", "-okdir") and idx + 1 < len(texts):
            if _basename(texts[idx + 1]) in ("rm", "shred", "unlink", "rmdir", "truncate"):
                deleting = True
    if deleting:
        for s in starts:
            hit = system_dir_hit(s.text, cwd, contents_too=False)
            if hit:
                return refuse("rm-root", f"find would delete inside {hit}.")
    for idx, t in enumerate(texts):
        if t in ("-exec", "-execdir", "-ok", "-okdir"):
            seg: list[Word] = []
            for a in eff.args[idx + 1 :]:
                if a.text in (";", "+"):
                    break
                start = starts[0].text if starts else "."
                seg.append(Word(a.text.replace("{}", posixpath.join(_glob(start), "x"))))
            sub = effective_command(seg)
            if sub is not None:
                v = _check_effective(sub, Command(words=seg), cwd, strict=True)
                if v:
                    return v
    return None


def _check_dd(eff: _Effective, cwd: str | None) -> FirewallVerdict | None:
    for a in eff.args:
        if a.text.startswith("of="):
            target = Word(a.text[3:], a.quoted)
            if is_unsafe_device(target, cwd):
                return refuse("dd-device", f"dd output {target.text.replace(WILD, '$?')!r} is a device or unknown.")
        elif a.wild and "=" not in a.text.replace(WILD, ""):
            return refuse("dd-device", "A dd argument is built at run time and could name a device.")
    if eff.via_xargs:
        return refuse("dd-device", "dd through xargs takes arguments from input, which cannot be checked.")
    return None


def _check_partition(eff: _Effective) -> FirewallVerdict | None:
    texts = [a.text for a in eff.args]
    name = eff.name
    listing = {
        "fdisk": {"-l", "--list"},
        "gdisk": {"-l"},
        "cfdisk": set(),
        "sfdisk": {"-l", "--list", "-d", "--dump", "-s", "--show-size", "-J", "--json", "-V", "--verify"},
        "sgdisk": {"-p", "--print", "-v", "--verify", "-i", "--info"},
        "parted": {"-l", "--list"},
    }[name]
    if any(t in listing or t.split("=", 1)[0] in listing for t in texts):
        if name == "sgdisk" and any(t.startswith(("-Z", "-z", "--zap", "-o", "--clear", "-d", "--delete", "-n", "--new")) for t in texts):
            return refuse("partition-table", f"{name} with a write option.")
        return None
    if name == "parted":
        operands = [t for t in texts if not t.startswith("-")]
        if operands[1:] and all(t in ("print", "unit", "s", "B", "MB", "MiB", "GB", "GiB", "%", "chs", "cyl", "compact")
                                for t in operands[1:]) and "print" in operands[1:]:
            return None
    return refuse("partition-table", f"{name} can rewrite the partition table; only listing is allowed.")


def _check_effective(eff: _Effective, cmd: Command, cwd: str | None, strict: bool = True) -> FirewallVerdict | None:
    name = eff.name
    texts = [a.text for a in eff.args]
    opts, operands = _options_and_operands(eff.args)

    if eff.word.wild:
        if not strict:
            return None
        return refuse("obfuscated", "The command name comes from a variable, substitution or brace expansion.")
    if name == "eval":
        return refuse("obfuscated", "eval runs text the firewall cannot see.")
    if name == "fc" and not ({"-l"} & set(opts)):
        return refuse("obfuscated", "fc re-runs commands from history.")
    if name == "stty" and not set(texts) <= {"-a", "--all", "-g", "--save", "size", "sane", "speed"}:
        return refuse("keyboard-map", "Only stty -a, stty size and stty sane are allowed.")
    if name == "bind" and not set(texts) <= {"-p", "-P", "-v", "-V", "-s", "-S", "-l"}:
        return refuse("keyboard-map", "Only listing bindings (bind -p, bind -P) is allowed.")
    if name == "set" and ({"vi", "emacs"} & set(texts)):
        return refuse("keyboard-map", "Switching the line editor mode is refused.")
    if name.startswith("mkfs") or name in _MKFS:
        return refuse("mkfs")
    if name == "wipefs":
        return refuse("wipefs")
    if name == "shred":
        return refuse("shred")
    if name == "blkdiscard":
        return refuse("disk-erase")
    if name == "cryptsetup" and any(t in ("luksFormat", "erase", "luksErase", "reencrypt") for t in texts):
        return refuse("disk-erase", "cryptsetup would destroy the existing data.")
    if name == "nvme" and any(t in ("format", "sanitize") for t in texts):
        return refuse("disk-erase")
    if name == "hdparm" and any(t.startswith(("--security-erase", "--sanitize", "--trim-sector", "--write-sector")) for t in texts):
        return refuse("disk-erase")
    if name in ("lvremove", "vgremove", "pvremove") or (name == "mdadm" and "--zero-superblock" in texts):
        return refuse("disk-erase")
    if name in ("zpool", "zfs") and "destroy" in texts:
        return refuse("disk-erase")
    if name in ("fdisk", "gdisk", "cfdisk", "sfdisk", "sgdisk", "parted"):
        return _check_partition(eff)
    if name in ("dd", "dcfldd"):
        v = _check_dd(eff, cwd)
        if v:
            return v
    if name in _DISK_COPIERS and any(is_unsafe_device(o, cwd) for o in operands if o.text.startswith("/dev") or o.wild):
        return refuse("dd-device", f"{name} with a device argument.")
    if name == "rm":
        v = _check_rm(eff, cwd)
        if v:
            return v
    if name == "find":
        v = _check_find(eff, cwd)
        if v:
            return v
    if name == "mv":
        for src in operands[:-1]:
            hit = system_dir_hit(src.text, cwd)
            if hit:
                return refuse("rm-root", f"mv would move {hit} away.")
    if name in ("chmod", "chown", "chgrp", "setfacl") and ("--recursive" in opts or _short_has(opts, "R")):
        for t in operands:
            hit = system_dir_hit(t.text, cwd, contents_too=True)
            if hit:
                return refuse("recursive-root", f"Target reaches {hit}.")
    if name in ("chmod", "chown", "chgrp") and any(r == "/" for o in operands for r in resolve(o.text, cwd) if not o.wild):
        return refuse("recursive-root", "Changing / itself is refused.")

    if name == "passwd" and not ({"-S", "--status"} & set(opts)):
        return refuse("root-password")
    if name in ("chpasswd", "vipw", "newusers"):
        return refuse("root-password")
    if name == "usermod" and any(
        o in ("-p", "--password", "-L", "--lock", "-U", "--unlock") or o.startswith("--password=")
        or (o.startswith("-p") and not o.startswith("--"))
        for o in opts
    ):
        return refuse("root-password")
    if name == "ssh-copy-id":
        return refuse("authorized-keys-write")

    recursive_reader = (
        (name in _GREP and (name in ("rg", "ag", "ack") or "--recursive" in opts
                            or "--dereference-recursive" in opts or _short_has(opts, "rR")))
        or name in _ARCHIVERS
        or (name in ("cp", "scp") and ("--recursive" in opts or "--archive" in opts or _short_has(opts, "rRa")))
    )
    if recursive_reader:
        for t in operands or [Word(".")]:
            if any(_holds_etc(p) for p in resolve(t.text, cwd)):
                return refuse("shadow-read", f"{name} over a directory that holds /etc/shadow would read it.")
    if name in _RAW_READERS and any(_BLOCK_DEV_RE.match(p) for o in operands for p in resolve(o.text, cwd)):
        return refuse("shadow-read", "Reading a raw disk can show the password hashes in /etc/shadow.")

    # General-purpose interpreters: their inline code can build any string at run time
    # ('sha' + 'dow'), so it is never run; a script file must already exist on the server.
    if _is_python(name) or name in _INTERPRETERS:
        inline = [t for t in texts if t in _INLINE_FLAGS.get(name, ())] or (
            [t for t in texts if t.startswith("-") and not t.startswith("--") and "c" in t[1:]] if _is_python(name) else []
        )
        if inline:
            return refuse("obfuscated", f"Inline {name} code is not run; use sed, grep or an editor for the repair.")
        if _first_script_operand(eff.args) is None and not any(t in ("-V", "--version", "-h", "--help") for t in texts):
            return refuse(
                "obfuscated",
                f"{name} with no script reads code from the keyboard or stdin, which the firewall cannot check.",
            )
    if name in _AWKS:
        program = _awk_program(eff.args)
        if program is None and not any(a.text.startswith("-f") or a.text == "--file" for a in eff.args):
            if not any(t in ("-V", "--version", "-h", "--help") for t in texts):
                return refuse("obfuscated", f"{name} with no program cannot be checked.")
        if program is not None and (program.wild or _AWK_DANGER.search(program.text)):
            return refuse("obfuscated", f"This {name} program runs commands or writes files; use it only to read text.")
    if name == "sed" and any(_sed_executes(t) for t in _sed_scripts(eff.args)):
        return refuse("obfuscated", "This sed script runs commands (the e command or the e flag).")

    # Shells and interpreters fed code they cannot be checked on.
    if name in _SHELLS or _is_python(name) or name in _INTERPRETERS:
        has_inline = any(
            (t.startswith("-") and not t.startswith("--") and "c" in t[1:]) for t in texts
        ) or any(t in _INLINE_FLAGS.get(name, ()) for t in texts)
        stdin_code = cmd.piped_in or any(r.op in ("<<", "<<-", "<<<") for r in cmd.redirects) or any(
            r.op == "<" and r.target is not None and (r.target.bare_wild or r.target.text in _STDIN_NAMES)
            for r in cmd.redirects
        )
        script = _first_script_operand(eff.args)
        if script is not None and (script.bare_wild or script.text in _STDIN_NAMES):
            return refuse("obfuscated", f"{name} would run code from a pipe or process substitution.")
        if script is None and not has_inline and stdin_code:
            return refuse("obfuscated", f"Code piped or here-doc'd into {name} cannot be checked.")
    if name in ("source", ".") and operands and (operands[0].bare_wild or operands[0].text in _STDIN_NAMES):
        return refuse("obfuscated", "Sourcing from a pipe or process substitution cannot be checked.")

    # Decoding text into commands.
    if name in ("base64", "base32", "basenc") and ("--decode" in opts or _short_has(opts, "dD")):
        return refuse("obfuscated", f"{name} decoding is not needed for a repair.")
    if name == "xxd" and ("-r" in opts or "-revert" in opts or _short_has(opts, "r")):
        return refuse("obfuscated", "xxd -r decoding is not needed for a repair.")
    if name == "uudecode":
        return refuse("obfuscated")
    if name == "openssl" and any(t in ("base64", "enc") for t in texts) and "-d" in texts:
        return refuse("obfuscated", "openssl decoding is not needed for a repair.")
    escapes = re.compile(r"\\(x[0-9a-fA-F]|u[0-9a-fA-F]|U[0-9a-fA-F]|[0-7])")
    if name == "printf" and any(escapes.search(t) for t in texts):
        return refuse("obfuscated", "printf with hex or octal escapes; type the characters instead.")
    if name == "echo" and _short_has(opts, "e") and any(escapes.search(t) for t in texts):
        return refuse("obfuscated", "echo -e with hex or octal escapes; type the characters instead.")

    # Copying onto devices.
    if name == "tee" and any(is_unsafe_device(o, cwd) for o in operands):
        return refuse("dd-device", "tee onto a device.")
    if name == "truncate" and any(is_unsafe_device(o, cwd) for o in operands if o.text.startswith("/dev")):
        return refuse("dd-device", "truncate on a device.")
    if strict and name in ("tee", "truncate") and any(o.bare_wild for o in operands):
        return refuse("obfuscated", f"The file {name} writes to is built at run time and cannot be checked.")
    if name in _WRITE_DEST_LAST and operands:
        dest = operands[-1]
        target_dir = next((t.split("=", 1)[1] for t in texts if t.startswith("--target-directory=")), None)
        if "-t" in texts and texts.index("-t") + 1 < len(texts):
            target_dir = texts[texts.index("-t") + 1]
        candidate = dest if target_dir is None else Word(target_dir)
        if len(operands) >= 2 or target_dir is not None:
            if candidate.bare_wild and strict:
                return refuse("obfuscated", f"The destination of {name} is built at run time and cannot be checked.")
            if (candidate.text.startswith("/dev") or (cwd == "/dev" and not candidate.text.startswith("/"))) and is_unsafe_device(candidate, cwd):
                return refuse("dd-device", f"{name} onto a device.")
    return None


def _first_script_operand(args: Sequence[Word]) -> Word | None:
    i = 0
    while i < len(args):
        t = args[i].text
        if t in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
            i += 2
            continue
        if (t.startswith("-") or t.startswith("+")) and len(t) > 1 and not args[i].wild:
            i += 1
            continue
        if t == "-":
            return args[i]
        return args[i]
    return None


def _awk_program(args: Sequence[Word]) -> Word | None:
    """The program text of an awk command line (the first operand when no -f is given)."""
    i = 0
    while i < len(args):
        t = args[i].text
        if t in ("-f", "--file") or t.startswith("-f"):
            return None
        if t in ("-v", "-F", "--assign", "--field-separator"):
            i += 2
            continue
        if t.startswith("-") and len(t) > 1 and not args[i].wild:
            i += 1
            continue
        if t == "--":
            return args[i + 1] if i + 1 < len(args) else None
        return args[i]
    return None


def _sed_scripts(args: Sequence[Word]) -> list[str]:
    scripts: list[str] = []
    explicit = False
    i = 0
    while i < len(args):
        t = args[i].text
        if t in ("-e", "--expression") and i + 1 < len(args):
            scripts.append(args[i + 1].text)
            explicit = True
            i += 2
            continue
        if t.startswith("--expression="):
            scripts.append(t.split("=", 1)[1])
            explicit = True
        elif not t.startswith("-") and not explicit and not scripts:
            scripts.append(t)
        i += 1
    return scripts


_SED_SUBST = re.compile(r"s([^\w\s\\])(?:\\.|(?!\1).)*\1(?:\\.|(?!\1).)*\1([a-zA-Z0-9]*)")
_SED_ADDRESS = re.compile(r"\\?([/|,#:@%])(?:\\.|(?!\1).)*\1")


def _sed_executes(script: str) -> bool:
    """True for a sed script with the GNU ``e`` command or an ``s///e`` substitution."""
    if WILD in script:
        return "e" in script.replace(WILD, "")  # cannot tell what the unknown part adds
    for match in _SED_SUBST.finditer(script):
        if "e" in match.group(2):
            return True
    stripped = _SED_ADDRESS.sub("", _SED_SUBST.sub("s", script))
    return re.search(r"(^|[;\n{}!])\s*[0-9$,~+\s]*e(\s|;|$|\})", stripped) is not None


def _check_redirects(cmd: Command, cwd: str | None, strict: bool = True) -> FirewallVerdict | None:
    for r in cmd.redirects:
        if r.target is None:
            continue
        if r.op in (">&", "<&") and (r.target.text.isdigit() or r.target.text == "-"):
            continue
        if r.op in (">", ">>", ">|", "&>", "&>>", "<>", ">&"):
            if r.target.bare_wild and strict:
                return refuse("dd-device", "The redirect target is built at run time and cannot be checked.")
            if is_unsafe_device(r.target, cwd):
                return refuse("dd-device", f"Redirect onto {r.target.text!r}.")
    return None


def _writes_file(cmd: Command, cwd: str | None) -> bool:
    for r in cmd.redirects:
        if r.target is None or r.op not in (">", ">>", ">|", "&>", "&>>", "<>", ">&"):
            continue
        if r.op == ">&" and (r.target.text.isdigit() or r.target.text == "-"):
            continue
        if r.target.text in ("/dev/null", "/dev/stdout", "/dev/stderr"):
            continue
        return True
    return False


_IN_PLACE = frozenset({"sed", "perl"})


def _ssh_config_targets(eff: _Effective | None, cmd: Command, cwd: str | None) -> list[Word]:
    """Words naming files under /etc/ssh that this command writes."""
    targets = [
        r.target for r in cmd.redirects
        if r.target is not None and r.op in (">", ">>", ">|", "&>", "&>>", "<>")
    ]
    if eff is not None:
        _, operands = _options_and_operands(eff.args)
        if eff.name == "tee":
            targets += operands
        elif eff.name in _WRITE_DEST_LAST and len(operands) >= 2:
            targets.append(operands[-1])
        elif eff.name in _IN_PLACE and any(a.text.startswith("-i") or a.text.startswith("-pi") for a in eff.args):
            targets += operands
    return [t for t in targets if any(p == "/etc/ssh" or p.startswith("/etc/ssh/") or fnmatchcase("/etc/ssh/x", p)
                                      for p in resolve(t.text, cwd))]


def _keys_safe(eff: _Effective | None) -> bool:
    if eff is None:
        return True
    if eff.name == "ssh-keygen":
        return any(a.text.startswith("-") and not a.text.startswith("--") and "l" in a.text for a in eff.args)
    return eff.name in _KEYS_SAFE


def _apply_cd(eff: _Effective | None, cwd: str | None) -> str | None:
    if eff is None:
        return cwd
    if eff.name == "popd":
        return None
    if eff.name not in ("cd", "pushd"):
        return cwd
    _, operands = _options_and_operands(eff.args)
    if not operands:
        return "/root"
    target = operands[0]
    if target.wild or target.text == "-":
        return None
    t = target.text
    if t.startswith("~"):
        user, _, rest = t[1:].partition("/")
        t = ("/root" if not user else f"/home/{user}") + ("/" + rest if rest else "")
    if t.startswith("/"):
        return _norm(t)
    if cwd is None:
        return None
    return _norm(posixpath.join(cwd, t))


@dataclass
class _Ctx:
    cwd: str | None
    env: dict[str, str]
    depth: int = 0
    strict: bool = True  # False for text that may not be shell code at all (see _nested_strings)
    written: frozenset[str] = frozenset()  # files written earlier in the session (absolute paths)


_SCRIPT_RUNNERS = _SHELLS | _INTERPRETERS | _AWKS | frozenset({"source", ".", "python", "python3"})


def _runs_written_script(eff: _Effective, cwd: str | None, written: frozenset[str]) -> str | None:
    """The written file this command would run as code, if any."""
    candidates: list[Word] = []
    if "/" in eff.word.text:
        candidates.append(eff.word)  # ./fix.sh, /tmp/x
    if eff.name in _SCRIPT_RUNNERS or _is_python(eff.name):
        candidates += [a for a in eff.args if not a.text.startswith("-")]
    for word in candidates:
        for path in resolve(word.text, cwd):
            if any(fnmatchcase(w, path) for w in written):
                return word.text.replace(WILD, "$?")
    return None


def written_files(parsed: Parsed, cwd: str | None) -> set[str]:
    """Literal files that an accepted line writes: redirect targets, tee operands, copies' and
    editors' destinations. Running one of them later as a script is refused."""
    out: set[str] = set()
    for cmd in parsed.commands:
        eff = effective_command(cmd.words)
        words = [r.target for r in cmd.redirects if r.target is not None and r.op in (">", ">>", ">|", "&>", "&>>", "<>")]
        if eff is not None:
            _, operands = _options_and_operands(eff.args)
            if eff.name == "tee" or eff.name in _EDITORS:
                words += operands
            elif eff.name in _WRITE_DEST_LAST and len(operands) >= 2:
                words.append(operands[-1])
        for w in words:
            if w.text and not w.bare_wild and w.text not in ("/dev/null", "/dev/stdout", "/dev/stderr"):
                out.update(resolve(w.text, cwd))
        cwd = _apply_cd(eff, cwd)
    return out


_EDITORS = frozenset({"nano", "vi", "vim", "nvim", "ed", "ex", "emacs", "joe", "mcedit", "pico", "jed", "micro"})


def changes_session(parsed: Parsed) -> bool:
    """True if the line starts or leaves a shell session (see ``_SESSION_CHANGERS``)."""
    for cmd in parsed.commands:
        words = [w for w in cmd.words if not (not w.quoted and _ASSIGN_RE.match(w.text))]
        if not words:
            continue
        first = _basename(words[0].text)
        if first in ("sudo", "doas", "run0") and any(
            w.text in ("-i", "-s", "--login", "--shell", "-l") or (
                w.text.startswith("-") and not w.text.startswith("--") and set(w.text[1:]) & {"i", "s"}
            )
            for w in words[1:]
        ):
            return True
        if first == "exec":
            return True
        eff = effective_command(cmd.words)
        if eff is None:
            continue
        if eff.name in _SESSION_CHANGERS:
            return True
        if eff.name == "systemctl" and any(a.text in _SYSTEMCTL_SESSION for a in eff.args):
            return True
    return False


def _screen(text: str, ctx: _Ctx) -> FirewallVerdict | None:
    if ctx.depth > MAX_DEPTH:
        return refuse("obfuscated", "Commands are nested too deeply to check.")
    if len(text) > MAX_LINE:
        return refuse("too-long")
    v = scan_keywords(text.replace(WILD, ""))
    if v:
        return v
    if ctx.depth == 0 and history_expansion(text):
        return refuse(
            "obfuscated",
            "History expansion (!!, !-1, !$, ^old^new) would run text the firewall cannot see. "
            "Type the full command; use single quotes around a literal '!'.",
        )
    parsed = parse(text, ctx.env)
    for body in parsed.subs:
        v = _screen(body, _Ctx(ctx.cwd, dict(ctx.env), ctx.depth + 1, True, ctx.written))
        if v:
            return v
    for body in parsed.heredocs:  # data unless fed to a shell, which is refused on its own
        v = _screen(body, _Ctx(ctx.cwd, dict(ctx.env), ctx.depth + 1, False, ctx.written))
        if v:
            return v

    cwd = ctx.cwd
    keys_mentioned = False
    keys_unsafe = False
    for cmd in parsed.commands:
        eff = effective_command(cmd.words)
        ssh_targets = _ssh_config_targets(eff, cmd, cwd)
        built = any(w.wild for w in cmd.words if all(w is not t for t in ssh_targets)) or (
            cmd.piped_in and any(w.wild for c in parsed.commands for w in c.words)
        )
        if ssh_targets and built:
            return refuse(
                "authorized-keys-write",
                "Text built at run time cannot be written into /etc/ssh; type the setting out in full.",
            )
        if ctx.written and eff is not None:
            ran = _runs_written_script(eff, cwd, ctx.written)
            if ran is not None:
                return refuse("obfuscated", f"{ran} was written in this session; running it would skip the checks.")
        words = list(cmd.words) + [r.target for r in cmd.redirects if r.target is not None]
        for w in words:
            if mentions_shadow(w, cwd):
                return refuse("shadow-read", f"{w.text.replace(WILD, '$?')!r} names a shadow password file.")
            if mentions_keys(w, cwd):
                keys_mentioned = True
        for nested, strict in _nested_strings(eff, cmd):
            if nested.strip() and nested != text:
                v = _screen(nested, _Ctx(cwd, dict(ctx.env), ctx.depth + 1, strict, ctx.written))
                if v:
                    return v
        v = _check_redirects(cmd, cwd, ctx.strict)
        if v:
            return v
        if eff is not None:
            v = _check_effective(eff, cmd, cwd, ctx.strict)
            if v:
                return v
        if not _keys_safe(eff) or _writes_file(cmd, cwd):
            keys_unsafe = True
        cwd = _apply_cd(eff, cwd)
    if keys_mentioned and keys_unsafe:
        return refuse(
            "authorized-keys-write",
            "Only reading authorized_keys or fixing its owner and mode (chmod, chown) is allowed.",
        )
    return None


def screen_command(text: str, *, cwd: str | None = None, env: Mapping[str, str] | None = None) -> FirewallVerdict:
    """Check one complete command line (it may contain several commands and newlines).

    Stateless; ``cwd`` None means the working directory is unknown, which makes relative paths
    count as possibly inside a system directory.
    """
    for ch in text:
        if ch in "\n\t":
            continue
        if ch == "\x7f" or ord(ch) < 32:
            return refuse("control-char")
        if ord(ch) > 126:
            return refuse("non-ascii")
    return screen_twice(text, cwd, env or {}) or ALLOW


def screen_twice(
    text: str, cwd: str | None, env: Mapping[str, str], written: frozenset[str] = frozenset()
) -> FirewallVerdict | None:
    """Screen with the remembered variables, then with all of them unknown. A remembered value
    can make a line look worse, never better: the shell the keys reach may not have it at all
    (a new shell, a login prompt, a reboot)."""
    v = _screen(text, _Ctx(cwd, dict(env), written=written))
    if v is None and env:
        v = _screen(text, _Ctx(cwd, {}, written=written))
    return v


def track(parsed: Parsed, cwd: str | None, env: Mapping[str, str]) -> tuple[str | None, dict[str, str]]:
    """Working directory and simple variables after an accepted, complete command line runs."""
    env = dict(env)
    for cmd in parsed.commands:
        eff = effective_command(cmd.words)
        assigns: list[str] = []
        if eff is None:
            assigns = [w.text for w in cmd.words if _ASSIGN_RE.match(w.text)]
        elif eff.name in ("export", "declare", "typeset", "local", "readonly"):
            assigns = [w.text for w in eff.args if _ASSIGN_RE.match(w.text)]
        elif eff.name == "unset":
            for w in eff.args:
                env.pop(w.text, None)
        for a in assigns:
            m = _ASSIGN_RE.match(a)
            assert m is not None
            name, value = m.group(1), a[m.end() :]
            if WILD in value or m.group(2) or "+=" in a[: m.end()]:
                env.pop(name, None)
            else:
                env[name] = value
        cwd = _apply_cd(eff, cwd)
    return cwd, env


# --------------------------------------------------------------------------------------------
# The stateful firewall
# --------------------------------------------------------------------------------------------

_ENTER_KEYS = frozenset({"ret", "kp_enter"})
_RECALL_KEYS = frozenset({"up", "down", "pgup", "pgdn"})
_RECALL_CTRL = frozenset({"p", "n", "r", "s", "o"})
MAX_PENDING = 16384


@dataclass
class _Line:
    """The firewall's model of the console's current input line.

    Only edits that behave the same in bash/readline, busybox ash, dash and a plain terminal
    are followed: appending typed characters, backspace, ctrl+u (kill line), ctrl+w (erase
    word), ctrl+c, and ctrl+d on an empty line. Cursor keys, Home/End, ctrl+a/e/k/y, Escape,
    Alt, tab and history keys move the cursor or insert raw bytes depending on the shell, so
    they mark the line as tainted instead of being guessed at.
    """

    text: str = ""
    tainted: bool = False
    recall: bool = False
    typed: bool = False


@dataclass
class _State:
    line: _Line = field(default_factory=_Line)
    pending: str = ""
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    history: list[str] = field(default_factory=list)
    written: set[str] = field(default_factory=set)

    def forget_session(self) -> None:
        """The keys may now reach another shell: its variables and directory are unknown."""
        self.env = {}
        self.cwd = None

    # line editing ----------------------------------------------------------------------------

    def insert(self, text: str) -> None:
        self.line.text += text
        self.line.typed = True

    def backspace(self) -> None:
        self.line.text = self.line.text[:-1]

    def taint(self, *, recall: bool = False) -> None:
        self.line.tainted = True
        self.forget_session()
        if recall:
            self.line.recall = True

    def fresh_line(self) -> None:
        self.line = _Line()

    def ctrl(self, key: str) -> FirewallVerdict | None:
        L = self.line
        if key in ("m", "j"):
            return self.enter()
        if key == "c":
            self.fresh_line()
            self.pending = ""
        elif key == "d" and L.text == "" and not L.tainted:
            self.pending = ""
            self.fresh_line()
            self.forget_session()  # ctrl+d on an empty line ends the shell (or a login)
        elif key == "u" and not L.tainted:
            L.text = ""
        elif key == "w" and not L.tainted:
            stripped = L.text.rstrip()
            cut = max(stripped.rfind(" "), stripped.rfind("\t"))
            L.text = stripped[: cut + 1]
        else:
            self.taint(recall=key in _RECALL_CTRL)
        return None

    def enter(self) -> FirewallVerdict | None:
        L = self.line
        if L.tainted:
            if L.typed or self.pending or L.recall:
                return refuse(
                    "unscreened-line",
                    "Press ctrl+c to start a fresh line, then type the whole command in one type call.",
                )
            self.fresh_line()
            return None
        logical = f"{self.pending}\n{L.text}" if self.pending else L.text
        if len(logical) > MAX_PENDING:
            return refuse("too-long", "Press ctrl+c to abandon the unfinished command.")
        verdict = screen_twice(logical, self.cwd, self.env, frozenset(self.written))
        if verdict:
            return verdict
        parsed = parse(logical, self.env)
        if parsed.incomplete:
            self.pending = logical
        else:
            self.pending = ""
            self.written |= written_files(parsed, self.cwd)
            if changes_session(parsed):
                self.forget_session()
            else:
                self.cwd, self.env = track(parsed, self.cwd, self.env)
            if logical.strip():
                self.history.append(logical)
        self.fresh_line()
        return None

    def press(self, keys: list[str]) -> FirewallVerdict | None:
        mods = modifiers_of(keys)
        mains = main_keys(keys)
        if "sysrq" in keys:
            return refuse("sysrq")  # alt+sysrq+b reboots without the shell (and without reset())
        if not mains:
            return None
        if any(k in _ENTER_KEYS for k in mains):
            return self.enter()  # Enter submits the line whatever modifiers are held
        if len(mains) > 1:
            self.taint()
            return None
        key = mains[0]
        letter = chord_char([key])
        if "ctrl" in mods and letter in ("m", "j"):
            return self.enter()  # ctrl+m and ctrl+j are carriage return and line feed
        if "alt" in mods or "meta" in mods:
            self.taint()
            return None
        if "ctrl" in mods:
            if letter is not None and letter.isalpha():
                return self.ctrl(letter)
            self.taint()
            return None
        char = chord_char(keys)
        if char is not None:
            self.insert(char)
        elif key == "backspace" and not mods:
            self.backspace()
        else:
            self.taint(recall=key in _RECALL_KEYS)
        return None

    def scan(self) -> FirewallVerdict | None:
        return scan_keywords(f"{self.pending}\n{self.line.text}")


class KeystrokeFirewall:
    """Implements ``keyhold.types.Firewall``. One instance per console session.

    ``inspect_type`` and ``inspect_keys`` check the input and, when it is allowed, update the
    firewall's model of the console line. Call them right before sending; a refused input must
    not be sent. Call :meth:`reset` after a power cycle.
    """

    def __init__(self, *, cwd: str | None = None) -> None:
        self._initial_cwd = cwd
        self._state = _State(cwd=cwd)
        self.refusals: list[FirewallVerdict] = []

    # read-only views, for the status page and tests
    @property
    def line(self) -> str:
        return self._state.line.text

    @property
    def pending(self) -> str:
        return self._state.pending

    @property
    def tainted(self) -> bool:
        return self._state.line.tainted

    @property
    def cwd(self) -> str | None:
        return self._state.cwd

    @property
    def history(self) -> list[str]:
        return list(self._state.history)

    def reset(self) -> None:
        """Forget the line, pending continuation, directory and variables (after a reboot).
        Files written in the session are still remembered: they are on the disk."""
        self._state = _State(cwd=self._initial_cwd, history=self._state.history, written=self._state.written)

    def _refused(self, verdict: FirewallVerdict) -> FirewallVerdict:
        self.refusals.append(verdict)
        return verdict

    def inspect_type(self, text: str) -> FirewallVerdict:
        if not isinstance(text, str):
            return self._refused(refuse("control-char", "Text must be a string."))
        for ch in text:
            if ch in "\n\r\t":
                continue
            if ord(ch) < 32 or ch == "\x7f":
                return self._refused(refuse("control-char", f"Character {ch!r}; use the key tool (backspace, ctrl+c...)."))
            if ord(ch) > 126:
                return self._refused(refuse("non-ascii", f"Character {ch!r} is not ASCII."))
        v = scan_keywords(text)
        if v:
            return self._refused(v)
        sim = copy.deepcopy(self._state)
        for ch in text:
            if ch in "\r\n":
                v = sim.enter()
                if v:
                    return self._refused(v)
            elif ch == "\t":
                sim.taint()
            else:
                sim.insert(ch)
        v = sim.scan()
        if v:
            return self._refused(v)
        self._state = sim
        return ALLOW

    def inspect_keys(self, keys: list[str]) -> FirewallVerdict:
        if not keys or not isinstance(keys, list) or len(keys) > MAX_CHORD:
            return self._refused(refuse("unknown-key", "Send one key or one chord of up to four keys."))
        unknown = [k for k in keys if k not in KEY_NAMES]
        if unknown:
            return self._refused(refuse("unknown-key", f"Unknown key name(s): {', '.join(map(str, unknown))}."))
        if "caps_lock" in keys:
            return self._refused(refuse("caps-lock"))
        sim = copy.deepcopy(self._state)
        v = sim.press(list(keys))
        if v:
            return self._refused(v)
        v = sim.scan()
        if v:
            return self._refused(v)
        self._state = sim
        return ALLOW


__all__ = [
    "ALLOW",
    "KeystrokeFirewall",
    "PROTECTED_DIRS",
    "REASONS",
    "parse",
    "refuse",
    "scan_keywords",
    "screen_command",
]
