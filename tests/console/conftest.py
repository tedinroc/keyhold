"""Fixtures for the console tests (the fakes themselves are in console_fakes.py)."""

from __future__ import annotations

import pytest
from console_fakes import FakeDocker, FakeQMPServer

from keyhold.console.docker import RUNNER_IMAGE, DockerRunner


@pytest.fixture
def qmp_server():
    server = FakeQMPServer()
    yield server
    server.close()


@pytest.fixture
def fake_docker(tmp_path):
    home = tmp_path / "vm"
    home.mkdir()
    docker = FakeDocker(home)
    runner = DockerRunner(home, image=RUNNER_IMAGE, run=docker)
    return docker, runner
