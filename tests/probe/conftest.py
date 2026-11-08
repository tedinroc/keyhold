"""Fixtures for the owner-probe tests. Helpers live in ``probe_helpers``.

The key file only has to exist with mode 600; ssh is never run against it here.
"""

from __future__ import annotations

import os

import pytest
from probe_helpers import make_spec

from keyhold.types import ProbeSpec


@pytest.fixture
def spec() -> ProbeSpec:
    return make_spec()


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "secrets" / "probe_ed25519"
    path.parent.mkdir()
    path.write_text("not a real key\n")
    os.chmod(path, 0o600)
    return path


@pytest.fixture
def known_hosts(tmp_path):
    return tmp_path / "state" / "known_hosts" / "rescue-1"
