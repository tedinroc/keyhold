"""Boot real guests in docker: the QEMU backend end to end. Slow and opt-in.

Run with:  uv run pytest -m slow tests/console/test_real_guest.py

Needs docker, the runner image (tools/vm/vm.py runner) and the verified base image
(tools/vm/vm.py fetch). Those two steps download things; this test does not. It builds the golden
image and the fault images it uses if they are missing (a few minutes under emulation), then boots
throwaway run overlays. Every connection it makes goes to the host loopback.
"""

from __future__ import annotations

import io
import re
import subprocess
import time
import urllib.request

import pytest
from PIL import Image

from keyhold.console.docker import DockerRunner
from keyhold.console.image import (
    VMHome,
    build_golden,
    load_golden_info,
    new_run_vm,
    read_console_password,
    remove_run,
    verify_base_image,
)
from keyhold.console.qemu import QemuVM, VMEndpoints
from keyhold.faults import FAULTS
from keyhold.faults.plant import plant_fault
from keyhold.types import sha256_hex

pytestmark = pytest.mark.slow

BOOT_TIMEOUT = 420


@pytest.fixture(scope="module")
def env(request):
    if "slow" not in (request.config.getoption("markexpr") or ""):
        pytest.skip("boots VMs for several minutes; run with: uv run pytest -m slow tests/console")
    home = VMHome()
    runner = DockerRunner(home.root)
    if not runner.available():
        pytest.skip("docker is not available")
    if not runner.image_present():
        pytest.skip(f"runner image {runner.image} missing; build it with: uv run python tools/vm/vm.py runner")
    if not verify_base_image(home.base_image()):
        pytest.skip("base image missing; download it with: uv run python tools/vm/vm.py fetch")
    build_golden(home, runner)
    return home, runner


def _boot(home: VMHome, runner: DockerRunner, fault_id: str | None) -> QemuVM:
    if fault_id is not None:
        plant_fault(home, runner, FAULTS[fault_id])
    vm = new_run_vm(home, runner, fault_id)
    vm.start()
    return vm


def _cleanup(home: VMHome, vm: QemuVM) -> None:
    vm.kill()
    remove_run(home, vm.run_dir.name)


def _ssh_ok(home: VMHome, user: str, endpoints: VMEndpoints) -> bool:
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "IdentityAgent=none",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "LogLevel=ERROR",
        "-i",
        str(home.probe_key),
        "-p",
        str(endpoints.ssh),
        f"{user}@{endpoints.host}",
        "true",
    ]
    return subprocess.run(argv, capture_output=True, timeout=60).returncode == 0


def _page_sha256(endpoints: VMEndpoints) -> str | None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via a proxy
    try:
        with opener.open(f"http://{endpoints.host}:{endpoints.http}/", timeout=10) as response:
            return sha256_hex(response.read())
    except OSError:
        return None


def _login(console, password: str) -> None:
    time.sleep(3)  # the serial getty can be up a moment before the one on the screen
    console.type_text("root\n")
    time.sleep(1)
    console.type_text(password + "\n")
    time.sleep(3)  # login, motd and the shell prompt


def _wait(vm: QemuVM, pattern: str, start: int = 0, timeout: float = BOOT_TIMEOUT) -> re.Match[str]:
    match = vm.wait_for_serial(pattern, timeout=timeout, start=start)
    assert match is not None, f"{pattern!r} never appeared; serial tail:\n{vm.serial_text()[-1500:]}"
    return match


def test_healthy_guest_screen_keyboard_reset_and_clean_shutdown(env) -> None:
    home, runner = env
    info = load_golden_info(home)
    vm = _boot(home, runner, None)
    try:
        _wait(vm, r"login:")
        assert _page_sha256(vm.endpoints) == info.page_sha256
        assert _ssh_ok(home, info.probe_user, vm.endpoints)

        console = vm.console()
        with Image.open(io.BytesIO(console.screenshot())) as screen:
            assert screen.format == "PNG" and screen.size[0] >= 640
            low, high = screen.convert("L").getextrema()
            assert high - low > 100  # text on the screen, not a blank frame

        _login(console, read_console_password(home))
        mark = len(vm.serial_text())
        console.type_text("echo KH-TYPED-$((6*7)) > /dev/ttyAMA0\n")
        _wait(vm, r"KH-TYPED-42", start=mark, timeout=60)

        mark = len(vm.serial_text())
        console.power_cycle()
        _wait(vm, r"login:", start=mark)
        console.close()

        assert vm.shutdown(timeout=120) is True
    finally:
        _cleanup(home, vm)


def test_masked_sshd_is_fixed_from_the_keyboard(env) -> None:
    home, runner = env
    info = load_golden_info(home)
    vm = _boot(home, runner, "sshd-masked")
    try:
        _wait(vm, FAULTS["sshd-masked"].ready_marker)
        assert not _ssh_ok(home, info.probe_user, vm.endpoints)
        assert _page_sha256(vm.endpoints) == info.page_sha256

        console = vm.console()
        _login(console, read_console_password(home))
        mark = len(vm.serial_text())
        console.type_text("systemctl unmask ssh.service ssh.socket && systemctl enable --now ssh.service\n")
        _wait(vm, r"Started ssh\.service", start=mark, timeout=120)
        console.close()
        assert _ssh_ok(home, info.probe_user, vm.endpoints)
    finally:
        _cleanup(home, vm)


def test_grub_password_stops_the_boot(env) -> None:
    home, runner = env
    vm = _boot(home, runner, "grub-password")
    try:
        _wait(vm, FAULTS["grub-password"].ready_marker, timeout=180)
        console = vm.console()
        assert console.screenshot().startswith(b"\x89PNG")
        mark = len(vm.serial_text())
        console.power_cycle()
        _wait(vm, r"Enter username", start=mark, timeout=180)  # a reset does not get past it
        console.close()
        assert not _ssh_ok(home, "probe", vm.endpoints)
    finally:
        _cleanup(home, vm)
