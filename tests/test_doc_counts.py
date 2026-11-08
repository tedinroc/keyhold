"""The README and the judge-facing documents say how many tests `uv run pytest -q` collects, passes
and skips. When this run collects the whole suite the way that command does, the numbers written
there must be this run's numbers, so a change to the suite cannot leave them stale."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# The tests skipped by default: they boot real guests and run only with `-m slow`.
REAL_GUEST_TESTS = "test_real_guest.py"
# Each claim names the numbers it states.
CLAIMS = [
    ("README.md", r"collects (?P<collected>\d+) tests: (?P<passed>\d+) pass and (?P<skipped>\d+) are skipped"),
    ("docs/JUDGES.md", r"(?P<collected>\d+) tests, (?P<passed>\d+) pass, (?P<skipped>\d+)\s+are skipped"),
    ("docs/SUBMISSION.md", r"(?P<collected>\d+) collected, (?P<passed>\d+) passed, (?P<skipped>\d+) skipped"),
    ("docs/SUBMISSION.md", r"(?P<collected>\d+) tests: (?P<passed>\d+) pass, and (?P<skipped>\d+) boot real guests"),
]


def _whole_suite(config: pytest.Config) -> bool:
    """True when this run collects every test, as plain `uv run pytest -q` does."""
    return (
        config.args_source == pytest.Config.ArgsSource.TESTPATHS
        and not config.option.keyword
        and not config.option.markexpr
        and not config.getoption("deselect", None)
        and not config.getoption("lf", False)
    )


def test_every_claim_is_found() -> None:
    for name, pattern in CLAIMS:
        assert re.search(pattern, (ROOT / name).read_text("utf-8")), f"{name}: no test count matching {pattern!r}"


def test_documented_test_counts_match_the_suite(request: pytest.FixtureRequest) -> None:
    if not _whole_suite(request.config):
        pytest.skip("the counts are checked only when the whole suite runs (plain `uv run pytest -q`)")
    items = request.session.items
    skipped = sum(1 for item in items if item.path.name == REAL_GUEST_TESTS)
    actual = {"collected": len(items), "passed": len(items) - skipped, "skipped": skipped}
    wrong = []
    for name, pattern in CLAIMS:
        match = re.search(pattern, (ROOT / name).read_text("utf-8"))
        assert match is not None, f"{name}: no test count matching {pattern!r}"
        stated = {key: int(value) for key, value in match.groupdict().items()}
        if any(stated[key] != actual[key] for key in stated):
            wrong.append(f"{name}: says {stated}")
    assert wrong == [], f"this run: {actual}; update the counts in " + "; ".join(wrong)
