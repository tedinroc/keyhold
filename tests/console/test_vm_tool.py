"""tools/vm/vm.py picks the guest architecture without docker doing anything real: a fake docker
CLI answers. No VM, no network."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from keyhold.console.docker import RUNNER_IMAGE, DockerRunner
from keyhold.console.image import VMHome

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def vm_tool():
    spec = importlib.util.spec_from_file_location("keyhold_tools_vm", ROOT / "tools" / "vm" / "vm.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def test_guest_arch_follows_the_runner_image(vm_tool, fake_docker) -> None:
    docker, runner = fake_docker
    docker.arch, docker.daemon_arch = "amd64", "aarch64"  # an amd64 runner, emulated
    assert vm_tool._guest_arch(runner) == "amd64"
    assert vm_tool._guest_arch(runner, "arm64") == "arm64"  # --arch wins


def test_guest_arch_without_a_runner_image(vm_tool, fake_docker, monkeypatch) -> None:
    docker, _ = fake_docker
    docker.fail["image"] = (1, "No such image")
    docker.daemon_arch = "x86_64"
    runner = DockerRunner(docker.vm_home, image=RUNNER_IMAGE, run=docker)
    assert vm_tool._guest_arch(runner) == "amd64"  # the docker host's

    docker.fail["info"] = (1, "Cannot connect to the Docker daemon")
    monkeypatch.setattr(vm_tool.platform, "machine", lambda: "arm64")
    runner = DockerRunner(docker.vm_home, image=RUNNER_IMAGE, run=docker)
    assert vm_tool._guest_arch(runner) == "arm64"  # this machine's


def test_status_names_the_arch(vm_tool, fake_docker, capsys, monkeypatch) -> None:
    docker, runner = fake_docker
    docker.arch, docker.daemon_arch = "amd64", "x86_64"
    monkeypatch.setattr(vm_tool, "_runner", lambda home: runner)
    assert vm_tool.cmd_status(None, VMHome(docker.vm_home)) == 0
    out = capsys.readouterr().out
    assert "present, amd64 guests" in out
    assert "debian-13-genericcloud-amd64-" in out and "missing or unverified" in out


def test_runner_command_takes_a_platform(vm_tool) -> None:
    with pytest.raises(SystemExit):
        vm_tool.main(["runner", "--platform", "linux/riscv64"])
    with pytest.raises(SystemExit):
        vm_tool.main(["fetch", "--arch", "i386"])
