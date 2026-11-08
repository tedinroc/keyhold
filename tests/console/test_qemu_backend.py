"""QemuConsole over a fake QMP server, qemu_argv, and QemuVM with a fake docker CLI."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from console_fakes import QMPCommandError
from PIL import Image

from keyhold.console.base import ConsoleClosed, ConsoleError, UnsupportedKey, UnsupportedText
from keyhold.console.docker import RUNNER_IMAGE, DockerError, DockerRunner
from keyhold.console.qemu import (
    ENTRYPOINT,
    GuestSpec,
    QemuConsole,
    QemuVM,
    qemu_argv,
)
from keyhold.console.qmp import QMPClient
from keyhold.types import Console


def _png(size=(800, 600)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (0, 0, 0)).save(out, format="PNG")
    return out.getvalue()


def _ppm(size=(8, 4)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(out, format="PPM")
    return out.getvalue()


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []
        self.t = 0.0

    def sleep(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.t += seconds

    def clock(self) -> float:
        return self.t


@pytest.fixture
def console_setup(qmp_server, tmp_path):
    """A QemuConsole whose screendump writes into tmp_path (the same path for QEMU and us)."""
    shots = tmp_path / "shots"
    sleeps = Sleeps()
    status = {"status": "running", "running": True}

    def screendump(args):
        path = Path(args["filename"])
        if args.get("format") == "png":
            path.write_bytes(_png())
        else:
            path.write_bytes(_ppm())
        return {}

    qmp_server.handlers.update(
        {
            "screendump": screendump,
            "send-key": lambda a: {},
            "query-status": lambda a: dict(status),
            "system_reset": lambda a: {},
            "cont": lambda a: {},
        }
    )
    client = QMPClient.connect("127.0.0.1", qmp_server.port, wait=5)
    console = QemuConsole(
        client, shot_dir_qemu=str(shots), shot_dir_local=shots, sleep=sleeps.sleep, clock=sleeps.clock, hold_ms=20
    )
    return console, qmp_server, sleeps, status, shots


def _sent_keys(server) -> list[list[str]]:
    return [[k["data"] for k in args["keys"]] for _, name, args in server.log if name == "send-key"]


def test_is_a_console(console_setup) -> None:
    console, *_ = console_setup
    assert isinstance(console, Console)


def test_screenshot_reads_and_removes_the_png(console_setup) -> None:
    console, server, _, _, shots = console_setup
    data = console.screenshot()
    assert data.startswith(b"\x89PNG")
    assert list(shots.iterdir()) == []
    _, name, args = server.log[-1]
    assert name == "screendump" and args["format"] == "png" and args["filename"].startswith(str(shots))
    second = console.screenshot()
    assert second == data and server.log[-1][2]["filename"] != args["filename"]  # a new file each time


def test_screenshot_falls_back_to_ppm(console_setup) -> None:
    console, server, _, _, _ = console_setup
    original = server.handlers["screendump"]

    def no_png(args):
        if args.get("format") == "png":
            raise QMPCommandError("GenericError", "format 'png' is not supported by this build")
        return original(args)

    server.handlers["screendump"] = no_png
    data = console.screenshot()
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "PNG" and image.size == (8, 4)
    console.screenshot()
    formats = [a.get("format") for _, n, a in server.log if n == "screendump"]
    assert formats == ["png", None, None]  # PNG tried once, then PPM only


def test_screenshot_errors(console_setup) -> None:
    console, server, _, _, _ = console_setup
    server.handlers["screendump"] = lambda a: (_ for _ in ()).throw(QMPCommandError("GenericError", "no surface"))
    with pytest.raises(ConsoleError):
        console.screenshot()
    server.handlers["screendump"] = lambda a: {}  # QEMU says yes but no file appears
    with pytest.raises(ConsoleError, match="did not appear"):
        console.screenshot()


def test_type_text_sends_one_chord_per_character(console_setup) -> None:
    console, server, sleeps, _, _ = console_setup
    console.type_text("Ls -l\n")
    assert _sent_keys(server) == [["shift", "l"], ["s"], ["spc"], ["minus"], ["l"], ["ret"]]
    holds = {args["hold-time"] for _, name, args in server.log if name == "send-key"}
    assert holds == {20}
    # 7 key events pressed and released, 20 ms after each, then the settle time.
    assert sleeps.calls[0] == pytest.approx(2 * 7 * 0.020)
    assert sleeps.calls[-1] == pytest.approx(0.15)


def test_type_text_paces_long_text_in_chunks(console_setup) -> None:
    console, server, sleeps, _, _ = console_setup
    console.type_text("a" * 100)
    assert len(_sent_keys(server)) == 100
    assert len(sleeps.calls) == 4  # chunks of 40, 40, 20 and the settle


def test_type_text_refuses_bad_text_before_sending(console_setup) -> None:
    console, server, *_ = console_setup
    with pytest.raises(UnsupportedText):
        console.type_text("echo café")
    with pytest.raises(TypeError):
        console.type_text(None)  # type: ignore[arg-type]
    assert _sent_keys(server) == []


def test_press_normalizes_and_rejects(console_setup) -> None:
    console, server, *_ = console_setup
    console.press(["Ctrl", "Alt", "Del"])
    console.press(["F2"])
    assert _sent_keys(server) == [["ctrl", "alt", "delete"], ["f2"]]
    with pytest.raises(UnsupportedKey):
        console.press(["hyper"])
    assert len(_sent_keys(server)) == 2


def test_send_key_error_becomes_console_error(console_setup) -> None:
    console, server, *_ = console_setup
    server.handlers["send-key"] = lambda a: (_ for _ in ()).throw(QMPCommandError("GenericError", "bad key"))
    with pytest.raises(ConsoleError):
        console.press(["a"])


def test_power_cycle(console_setup) -> None:
    console, server, _, status, _ = console_setup
    console.power_cycle()
    assert server.commands()[-2:] == ["query-status", "system_reset"]
    status.update(status="shutdown", running=False)  # the guest powered itself off
    console.power_cycle()
    assert server.commands()[-3:] == ["query-status", "system_reset", "cont"]


def test_close_ends_the_session_only(console_setup) -> None:
    console, server, *_ = console_setup
    console.close()
    console.close()  # idempotent
    for call in (console.screenshot, lambda: console.type_text("a"), lambda: console.press(["a"]), console.power_cycle):
        with pytest.raises(ConsoleClosed):
            call()
    assert "quit" not in server.commands() and "system_powerdown" not in server.commands()


def test_hold_time_bounds(qmp_server, tmp_path) -> None:
    client = QMPClient.connect("127.0.0.1", qmp_server.port, wait=5)
    with pytest.raises(ValueError):
        QemuConsole(client, shot_dir_qemu="/vm/x", shot_dir_local=tmp_path, hold_ms=1)


# ------------------------------------------------------------------------------------- argv


def test_qemu_argv_tcg_guest() -> None:
    spec = GuestSpec(
        disk="/vm/runs/r1/disk.qcow2",
        vars="/vm/runs/r1/efivars.fd",
        extra_drives=("/vm/seed.iso",),
        serial_log="/vm/runs/r1/serial.log",
        cpus=4,
    )
    argv = qemu_argv(spec)
    text = " ".join(argv)
    assert argv[0] == "qemu-system-aarch64"
    assert "-accel tcg,thread=multi,tb-size=256" in text and "max,pauth-impdef=on" in text
    assert "file=/vm/runs/r1/disk.qcow2,format=qcow2" in text
    assert "file=/vm/seed.iso,format=raw,readonly=on" in text
    # The firmware build without a UEFI shell (no way around a GRUB password from the firmware).
    assert "if=pflash,format=raw,readonly=on,file=/usr/share/AAVMF/AAVMF_CODE.secboot.fd" in text
    netdev = argv[argv.index("-netdev") + 1]
    assert "restrict=on" in netdev  # the guest cannot open connections of its own
    assert "hostfwd=tcp:127.0.0.1:12222-:22" in netdev and "hostfwd=tcp:127.0.0.1:18080-:80" in netdev
    for device in ("ramfb", "usb-kbd,bus=xhci.0", "virtio-keyboard-pci"):
        assert device in argv
    assert (
        "-qmp" in argv
        and "tcp:0.0.0.0:4444,server=on,wait=off" in argv
        and "tcp:0.0.0.0:4445,server=on,wait=off" in argv
    )
    assert "file,id=serial0,path=/vm/runs/r1/serial.log" in argv
    assert argv[-1] == "-no-shutdown"
    assert "-display" in argv and argv[argv.index("-display") + 1] == "none"


def test_qemu_argv_kvm_and_prepare_boot() -> None:
    argv = qemu_argv(GuestSpec(disk="/vm/d.qcow2", vars="/vm/v.fd", accel="kvm", stay_on_poweroff=False))
    assert ["-accel", "kvm"] == argv[argv.index("-accel") : argv.index("-accel") + 2]
    assert "host" in argv and "-no-shutdown" not in argv
    with pytest.raises(ValueError):
        GuestSpec(disk="d", vars="v", accel="hvf")
    with pytest.raises(ValueError):
        GuestSpec(disk="d", vars="v", memory_mb=64)


# ------------------------------------------------------------------------------------- QemuVM


def _vm(runner: DockerRunner, **kwargs) -> QemuVM:
    home = runner.vm_home
    sleeps = Sleeps()
    kwargs.setdefault("sleep", sleeps.sleep)
    kwargs.setdefault("clock", sleeps.clock)
    return QemuVM(
        runner, name="keyhold-run-t1", run_dir=home / "runs" / "t1", disk=home / "runs" / "t1" / "disk.qcow2", **kwargs
    )


def test_vm_start_console_and_clean_shutdown(fake_docker, qmp_server) -> None:
    docker, runner = fake_docker
    docker.ports = {4444: qmp_server.port, 4445: qmp_server.port}
    qmp_server.handlers.update(
        {
            "query-status": lambda a: {"status": "running", "running": True},
            "system_powerdown": lambda a: qmp_server.emit("SHUTDOWN", {"guest": True}) or {},
            "quit": lambda a: "CLOSE",
        }
    )
    vm = _vm(runner)
    endpoints = vm.start()
    assert vm.accel == "tcg"  # the fake docker host has no /dev/kvm
    assert endpoints.host == "127.0.0.1" and endpoints.qmp_control == qmp_server.port
    assert endpoints.ssh == 40000 + 2222 and endpoints.http == 40000 + 8080
    assert (vm.run_dir / "efivars.fd").exists()  # copied from the firmware's template

    run = docker.docker_calls("run")[-1]
    assert run[:4] == ["docker", "run", "-d", "--init"]
    assert "--device" not in run
    for port in (4444, 4445, 2222, 8080):
        assert f"127.0.0.1::{port}" in run  # published on the loopback only
    command = run[run.index(RUNNER_IMAGE) + 1 :]
    assert command[:6] == [ENTRYPOINT, "--relay", "2222:12222", "--relay", "8080:18080", "--"]
    assert command[6] == "qemu-system-aarch64"
    assert "file=/vm/runs/t1/disk.qcow2,format=qcow2,discard=unmap" in " ".join(command)

    assert vm.status() == "running"
    console = vm.console()
    assert isinstance(console, Console)
    console.close()

    docker.running["keyhold-run-t1"] = False
    assert vm.shutdown(timeout=30) is True
    assert "system_powerdown" in qmp_server.commands() and "quit" in qmp_server.commands()
    assert "keyhold-run-t1" not in docker.running  # container removed
    assert vm.endpoints is None


def test_vm_shutdown_when_the_guest_ignores_the_button(fake_docker, qmp_server) -> None:
    docker, runner = fake_docker
    docker.ports = {4444: qmp_server.port, 4445: qmp_server.port}
    qmp_server.handlers.update(
        {
            "query-status": lambda a: {"status": "running", "running": True},
            "system_powerdown": lambda a: {},
            "quit": lambda a: "CLOSE",
        }
    )
    vm = _vm(runner)
    vm.start()
    assert vm.shutdown(timeout=5) is False
    assert "quit" in qmp_server.commands()
    assert "keyhold-run-t1" not in docker.running


def test_vm_uses_kvm_when_the_host_has_it(fake_docker, qmp_server) -> None:
    docker, runner = fake_docker
    docker.kvm = True
    docker.ports = {4444: qmp_server.port, 4445: qmp_server.port}
    vm = _vm(runner)
    vm.start()
    run = docker.docker_calls("run")[-1]
    assert vm.accel == "kvm" and "/dev/kvm" in run and "--user" not in run
    assert "-accel" in run and run[run.index("-accel") + 1] == "kvm"
    vm.kill()


def test_vm_start_failure_cleans_up(fake_docker) -> None:
    docker, runner = fake_docker
    docker.fail["port"] = (1, "Error: No such container")
    vm = _vm(runner)
    with pytest.raises(ConsoleError, match="QEMU did not start"):
        vm.start()
    assert docker.docker_calls("rm")[-1][-1] == "keyhold-run-t1"
    assert vm.endpoints is None


def test_run_to_poweroff_returns_the_serial_log(fake_docker, qmp_server) -> None:
    docker, runner = fake_docker
    docker.ports = {4445: qmp_server.port}
    docker.exit_after_polls["keyhold-run-t1"] = 2
    vm = _vm(runner, stay_on_poweroff=False)

    real_start = vm.start

    def start_and_log(**kwargs):
        endpoints = real_start(**kwargs)
        vm.serial_log.write_text("booting\nKEYHOLD-PLANT-OK demo\n")
        return endpoints

    vm.start = start_and_log  # type: ignore[method-assign]
    assert "KEYHOLD-PLANT-OK demo" in vm.run_to_poweroff(timeout=60)
    run = docker.docker_calls("run")[-1]
    assert "-no-shutdown" not in run
    assert "keyhold-run-t1" not in docker.running


def test_run_to_poweroff_times_out(fake_docker, qmp_server) -> None:
    docker, runner = fake_docker
    docker.ports = {4445: qmp_server.port}
    vm = _vm(runner, stay_on_poweroff=False)
    with pytest.raises(ConsoleError, match="did not power off"):
        vm.run_to_poweroff(timeout=10)
    assert "keyhold-run-t1" not in docker.running
    with pytest.raises(ConsoleError):
        _vm(runner).run_to_poweroff(timeout=1)  # needs stay_on_poweroff=False


def test_wait_for_serial(fake_docker, qmp_server) -> None:
    docker, runner = fake_docker
    vm = _vm(runner)
    vm.run_dir.mkdir(parents=True)
    vm.serial_log.write_text("...\nkeyhold-demo login: ")
    assert vm.wait_for_serial(r"login:", timeout=1) is not None
    assert vm.wait_for_serial(r"Enter username", timeout=3) is None


def test_vm_name_and_accel_checks(fake_docker) -> None:
    _, runner = fake_docker
    with pytest.raises(ValueError):
        QemuVM(runner, name="bad name", run_dir=runner.vm_home, disk=runner.vm_home / "d")
    with pytest.raises(ValueError):
        QemuVM(runner, name="ok", run_dir=runner.vm_home, disk=runner.vm_home / "d", accel="hvf")


def test_docker_runner_paths_and_errors(fake_docker, tmp_path) -> None:
    docker, runner = fake_docker
    assert runner.container_path(runner.vm_home / "runs" / "a" / "disk.qcow2") == "/vm/runs/a/disk.qcow2"
    assert runner.container_path(runner.vm_home) == "/vm"
    with pytest.raises(DockerError):
        runner.container_path(tmp_path / "elsewhere")
    docker.fail["rm"] = (1, "no such container")
    runner.remove("gone")  # removing a missing container is not an error
    docker.fail["build"] = (1, "network down")
    with pytest.raises(DockerError, match="network down"):
        runner.build_image(tmp_path)
    assert runner.native_platform() == "linux/arm64"
    assert runner.available() and runner.image_present()


def test_docker_missing(tmp_path) -> None:
    def no_docker(*args, **kwargs):
        raise FileNotFoundError("docker")

    runner = DockerRunner(tmp_path, run=no_docker)
    assert runner.available() is False
    assert runner.kvm_available() is False
    with pytest.raises(DockerError, match="not installed"):
        runner.tool(["true"])


def test_guest_containers_cannot_see_the_probe_key_or_write_the_golden_image(fake_docker, qmp_server) -> None:
    """Regression: the guest container mounted the whole VM home read-write, including the probe
    private key, the console password and the golden image, with every default capability."""
    docker, runner = fake_docker
    docker.ports = {4444: qmp_server.port, 4445: qmp_server.port}
    home = runner.vm_home
    (home / "keys").mkdir(parents=True, exist_ok=True)
    (home / "keys" / "probe_ed25519").write_text("not a real key")
    (home / "golden").mkdir(parents=True, exist_ok=True)
    (home / "golden" / "console-password").write_text("not a real password")
    vm = _vm(runner)
    vm.start()
    run = docker.docker_calls("run")[-1]
    mounts = [run[i + 1] for i, a in enumerate(run) if a in ("-v", "--mount")]
    assert f"{home}:/vm:ro" in mounts  # the VM home is read-only
    assert f"{home / 'runs' / 't1'}:/vm/runs/t1" in mounts  # only the run directory is writable
    assert "type=tmpfs,destination=/vm/keys,tmpfs-size=65536" in mounts
    assert "/dev/null:/vm/golden/console-password:ro" in mounts
    joined = " ".join(run)
    assert "--cap-drop ALL" in joined and "no-new-privileges" in joined and "--pids-limit" in joined
    assert "--memory" in joined and "DAC_OVERRIDE" not in joined  # TCG runs as the calling user
    vm.kill()
