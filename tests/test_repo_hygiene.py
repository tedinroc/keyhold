"""Nothing in the repository names a local account, home folder or temporary path: the project is
published under a public identity, and a path like the author's home directory would link the two."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECKED = ("keyhold", "web", "tests", "tools", "docs")
TOP_FILES = ("README.md", ".env.example", "pyproject.toml", ".gitignore")
# Spelled in pieces so this file does not match itself.
PATTERNS = [
    re.compile("/" + "Users" + r"/[A-Za-z0-9._-]+"),
    re.compile("/" + "private" + "/tmp"),
    re.compile("/" + "home" + r"/(?!user\b|<user>|\{user\})[a-z][a-z0-9_-]*/(?:Documents|Desktop|Downloads)"),
    re.compile("claude" + r"-\d{3}"),
    # Other account names of the author, and the model gateway used for the live runs.
    re.compile("jia" + "c" + "punk|jia" + "punk", re.I),
    re.compile("open" + "code", re.I),
]


def _files():
    for name in TOP_FILES:
        path = ROOT / name
        if path.is_file():
            yield path
    for folder in CHECKED:
        for path in (ROOT / folder).rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix not in (".png", ".qcow2", ".img"):
                yield path


def test_no_local_paths_or_account_names() -> None:
    hits = []
    for path in _files():
        try:
            text = path.read_text("utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pattern in PATTERNS:
            for match in pattern.finditer(text):
                hits.append(f"{path.relative_to(ROOT)}: {match.group(0)}")
    assert hits == []
