"""Print the one-click judge link for a deployment.

    uv run --env-file .env python tools/judge_link.py https://keyhold.example

The link is ``<base>/j/<token>``. The token is derived from KEYHOLD_SECRET_KEY and
KEYHOLD_TRY_PASSCODE (see ``web.security.judge_token``), so the passcode never appears in a URL.
Opening the link unlocks /try in that browser. Changing either value retires every link given out.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from web.security import judge_token  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1].startswith(("http://", "https://")):
        print("usage: tools/judge_link.py <base URL, e.g. https://keyhold.example>", file=sys.stderr)
        return 2
    secret, passcode = os.environ.get("KEYHOLD_SECRET_KEY", ""), os.environ.get("KEYHOLD_TRY_PASSCODE", "")
    if len(secret) < 16 or not passcode:
        print("set KEYHOLD_SECRET_KEY (16+ characters) and KEYHOLD_TRY_PASSCODE first", file=sys.stderr)
        return 2
    print(f"{argv[1].rstrip('/')}/j/{judge_token(secret, passcode)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
