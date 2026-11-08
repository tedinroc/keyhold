"""Check the whole git history before the repository is made public.

    uv run python tools/prepublish_check.py              every ref in this repository
    uv run python tools/prepublish_check.py <commit>     only what pushing <commit> would publish

``tests/test_repo_hygiene.py`` checks the working tree. Publishing a branch also publishes every
earlier version of every file and every commit message, so this script reads all of them: every
blob reachable from any ref, and every commit's author, committer and message. It looks for the
hygiene test's patterns (local paths, other account names, the model gateway's name) and for
co-author trailers naming Claude, which the author does not put in public commits.

Prints ``object path: pattern`` for each hit (never the matching text) and exits 1 if there is
any. A history with hits is published as a fresh squashed commit instead.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from test_repo_hygiene import PATTERNS  # noqa: E402

TRAILER = re.compile(r"Co-Authored-By:.*" + "Cla" + "ude", re.I)
SKIP_SUFFIXES = (".png", ".qcow2", ".img", ".lock")


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout


def main(argv: list[str]) -> int:
    refs = argv[1:] or ["--all"]
    hits: list[str] = []
    for line in git("rev-list", *refs, "--objects").splitlines():
        sha, _, path = line.partition(" ")
        if not path or path.endswith(SKIP_SUFFIXES) or git("cat-file", "-t", sha).strip() != "blob":
            continue
        text = subprocess.run(["git", "cat-file", "-p", sha], cwd=ROOT, capture_output=True).stdout
        body = text.decode("utf-8", "replace")
        hits += [f"{sha[:10]} {path}: {p.pattern}" for p in PATTERNS if p.search(body)]
    for commit in git("rev-list", *refs).split():
        meta = git("show", "-s", "--format=%an <%ae>%n%cn <%ce>%n%B", commit)
        hits += [f"{commit[:10]} (commit): {p.pattern}" for p in [*PATTERNS, TRAILER] if p.search(meta)]
    for hit in sorted(set(hits)):
        print(hit)
    print(f"{len(set(hits))} hit(s) in the history" if hits else "history is clean")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
