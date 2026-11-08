"""Run the QEMU runner image with docker: one-shot tool commands and long-running VMs.

Everything Keyhold keeps for its guests lives under one directory on the host (the "VM home",
``var/vm`` by default). That directory is mounted at ``/vm`` in every container, so a path is
translated by swapping the prefix. Disk overlays use relative backing-file names, so the same
qcow2 chain opens on the host and in the container.

Ports a VM publishes are bound to the host loopback only.

The runner image's architecture decides the guest's: an arm64 runner runs 64-bit Arm guests, an
amd64 runner runs x86-64 guests (``DockerRunner.arch``). ``vm.py runner`` builds the image for the
docker host's own architecture; ``KEYHOLD_RUNNER_IMAGE`` names another image, for example one
built for the other architecture to test its guests under emulation.

A long-running guest container sees the VM home read-only, except the directories its QEMU
writes (its run directory and the folder of its disk). The probe key folder (``keys/``) and the
console password file are hidden from it, so a QEMU escape or a stray QMP client finds neither.
It runs with no Linux capabilities (KVM mode keeps only DAC_OVERRIDE, to write the files the
calling user owns), no-new-privileges, and limits on processes and memory.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from keyhold.console.base import ConsoleError

__all__ = [
    "RUNNER_IMAGE",
    "GUEST_ARCHES",
    "CONTAINER_VM_HOME",
    "LOOPBACK",
    "VM_LABEL",
    "DockerError",
    "DockerRunner",
    "anonymous_docker_env",
    "default_runner_image",
    "kvm_probe_script",
    "normalize_arch",
]

RUNNER_IMAGE = "keyhold-qemu-runner:3"  # bump when tools/vm/Dockerfile changes
GUEST_ARCHES = ("arm64", "amd64")  # Debian's names for the architectures Keyhold has guests for
# The kernel's name for each architecture (``uname -m``).
MACHINE = {"arm64": "aarch64", "amd64": "x86_64"}
_ARCH_ALIASES = {"arm64": "arm64", "aarch64": "arm64", "amd64": "amd64", "x86_64": "amd64", "x86-64": "amd64"}
CONTAINER_VM_HOME = "/vm"
LOOPBACK = "127.0.0.1"
VM_LABEL = "keyhold.vm"

RunFn = Callable[..., subprocess.CompletedProcess]

# Paths under the VM home a guest container must never see (see keyhold.console.image.VMHome).
SECRET_PATHS = ("keys", "golden/console-password")
GUEST_PIDS_LIMIT = 512
GUEST_MEMORY_OVERHEAD_MB = 1024  # QEMU itself, the TCG translation cache, socat


def normalize_arch(name: str) -> str:
    """``arm64`` or ``amd64`` for any common spelling (aarch64, x86_64, ...). Raises
    ``ValueError`` for an architecture Keyhold has no guest for."""
    arch = _ARCH_ALIASES.get(str(name).strip().lower())
    if arch is None:
        raise ValueError(f"no Keyhold guest for architecture {name!r} (only {', '.join(GUEST_ARCHES)})")
    return arch


def kvm_probe_script(arch: str) -> str:
    """The shell command of the KVM probe (``DockerRunner.kvm_available``) for a runner of
    ``arch``. It checks that the container really runs that architecture, then starts the
    runner's QEMU with ``-accel kvm`` on an empty machine (``-machine none``: no CPU, devices or
    firmware) and quits it through the monitor. Starting KVM opens /dev/kvm and creates a VM, so
    the command exits 0 only if QEMU can run a KVM guest here. ``timeout`` bounds a QEMU that
    hangs instead."""
    machine = MACHINE[arch]
    return (
        f'test -c /dev/kvm && test "$(uname -m)" = {machine} && '
        f"echo quit | timeout 30 qemu-system-{machine} -nodefaults -display none -machine none "
        "-accel kvm -monitor stdio >/dev/null"
    )


def default_runner_image() -> str:
    """``$KEYHOLD_RUNNER_IMAGE`` if set, else ``RUNNER_IMAGE``."""
    return os.environ.get("KEYHOLD_RUNNER_IMAGE", "").strip() or RUNNER_IMAGE


class DockerError(ConsoleError):
    """A docker command failed. ``stderr`` holds the tail of docker's error output."""

    def __init__(self, message: str, stderr: str = "") -> None:
        super().__init__(f"{message}: {stderr.strip()[-800:]}" if stderr.strip() else message)
        self.stderr = stderr


@dataclass
class DockerRunner:
    """Thin wrapper over the ``docker`` CLI. ``run`` is injectable so tests never start docker."""

    vm_home: Path
    image: str = field(default_factory=default_runner_image)
    docker: str = "docker"
    run: RunFn = subprocess.run
    env: Mapping[str, str] | None = None
    _kvm: bool | None = field(default=None, init=False, repr=False)
    _arch: str | None = field(default=None, init=False, repr=False)
    _daemon_arch: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.vm_home = Path(self.vm_home).resolve()

    # ------------------------------------------------------------------ plumbing

    def _docker(self, args: Sequence[str], *, timeout: float = 120, check: bool = True) -> subprocess.CompletedProcess:
        argv = [self.docker, *args]
        env = None
        if self.env is not None:
            env = {**os.environ, **self.env}
        try:
            proc = self.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
        except FileNotFoundError as exc:
            raise DockerError("docker is not installed") from exc
        except subprocess.TimeoutExpired as exc:
            raise DockerError(f"docker {args[0]} timed out after {timeout:.0f}s") from exc
        if check and proc.returncode != 0:
            raise DockerError(f"docker {args[0]} failed (exit {proc.returncode})", proc.stderr or "")
        return proc

    def container_path(self, host_path: Path | str) -> str:
        """The path of ``host_path`` inside a runner container. It must be under the VM home."""
        path = Path(host_path).resolve()
        try:
            relative = path.relative_to(self.vm_home)
        except ValueError:
            raise DockerError(f"{path} is outside the VM home {self.vm_home}") from None
        return f"{CONTAINER_VM_HOME}/{relative.as_posix()}" if relative.parts else CONTAINER_VM_HOME

    def _mount(self) -> list[str]:
        return ["-v", f"{self.vm_home}:{CONTAINER_VM_HOME}"]

    def _guest_mounts(self, writable: Sequence[Path]) -> list[str]:
        if not writable:
            args = self._mount()
        else:
            args = ["-v", f"{self.vm_home}:{CONTAINER_VM_HOME}:ro"]
            seen: set[str] = set()
            for path in writable:
                inside = self.container_path(path)
                if inside == CONTAINER_VM_HOME or inside in seen:
                    continue
                seen.add(inside)
                args += ["-v", f"{Path(path).resolve()}:{inside}"]
        for relative in SECRET_PATHS:
            secret = self.vm_home / relative
            if secret.is_dir():
                args += ["--mount", f"type=tmpfs,destination={CONTAINER_VM_HOME}/{relative},tmpfs-size=65536"]
            elif secret.exists():
                args += ["-v", f"/dev/null:{CONTAINER_VM_HOME}/{relative}:ro"]
        return args

    # ------------------------------------------------------------------ checks

    def available(self) -> bool:
        """True when the docker CLI answers and a daemon is running."""
        try:
            return self._docker(["info", "--format", "{{.ServerVersion}}"], timeout=20, check=False).returncode == 0
        except DockerError:
            return False

    def image_present(self) -> bool:
        try:
            return self._docker(["image", "inspect", self.image], timeout=20, check=False).returncode == 0
        except DockerError:
            return False

    def arch(self) -> str:
        """The runner image's architecture, ``arm64`` or ``amd64``. Guests have the same
        architecture as the runner. Cached after the first call."""
        if self._arch is None:
            inspect = ["image", "inspect", "--format", "{{.Architecture}}", self.image]
            proc = self._docker(inspect, timeout=20, check=False)
            if proc.returncode != 0:
                raise DockerError(
                    f"runner image {self.image} is missing; build it with: uv run python tools/vm/vm.py runner",
                    proc.stderr or "",
                )
            try:
                self._arch = normalize_arch(proc.stdout.strip())
            except ValueError as exc:
                raise DockerError(f"runner image {self.image}: {exc}") from None
        return self._arch

    def daemon_arch(self) -> str:
        """The docker host's own architecture, ``arm64`` or ``amd64``. Cached."""
        if self._daemon_arch is None:
            arch = self._docker(["info", "--format", "{{.Architecture}}"], timeout=20).stdout.strip()
            try:
                self._daemon_arch = normalize_arch(arch)
            except ValueError as exc:
                raise DockerError(str(exc)) from None
        return self._daemon_arch

    def kvm_available(self) -> bool:
        """True when KVM can run this runner's guests: the runner image runs natively on the
        docker host (KVM cannot run a guest of another architecture, and an emulated runner
        cannot use the host's KVM) and QEMU can use /dev/kvm in a container with the same
        privileges a KVM guest container gets. Cached after the first call.

        The probe (``kvm_probe_script``) starts the runner's QEMU with KVM on an empty machine
        and quits it at once. QEMU opens /dev/kvm and creates a VM, so a /dev/kvm that exists but
        cannot run a VM (for example while another hypervisor holds the CPU's virtualization)
        answers no. Checking the file's permissions would prove nothing: the container's root
        has DAC_OVERRIDE. The empty machine has no vCPU, so a guest can still fail to start with
        KVM; ``QemuVM.start`` then starts it under TCG and calls ``disable_kvm``."""
        if self._kvm is None:
            try:
                arch = self.arch()
                if arch != self.daemon_arch():
                    self._kvm = False
                    return False
                probe = [
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--security-opt",
                    "no-new-privileges",
                    "--cap-drop",
                    "ALL",
                    "--cap-add",
                    "DAC_OVERRIDE",
                    "--device",
                    "/dev/kvm",
                    self.image,
                    "sh",
                    "-c",
                    kvm_probe_script(arch),
                ]
                self._kvm = self._docker(probe, timeout=90, check=False).returncode == 0
            except DockerError:
                self._kvm = False
        return self._kvm

    def disable_kvm(self) -> None:
        """Run this runner's later guests under TCG: a guest did not start with KVM although the
        probe passed."""
        self._kvm = False

    # ------------------------------------------------------------------ one-shot tools

    def tool(self, argv: Sequence[str], *, timeout: float = 600) -> subprocess.CompletedProcess:
        """Run a command (qemu-img, genisoimage, cp ...) in a throwaway runner container with no
        network, the VM home mounted at /vm, as the calling user so files stay owned by them."""
        args = ["run", "--rm", "--network", "none", *self._mount(), "-w", CONTAINER_VM_HOME]
        args += self._user()
        args += [self.image, *argv]
        return self._docker(args, timeout=timeout)

    @staticmethod
    def _user() -> list[str]:
        getuid = getattr(os, "getuid", None)
        getgid = getattr(os, "getgid", None)
        if getuid is None or getgid is None:
            return []
        return ["--user", f"{getuid()}:{getgid()}"]

    # ------------------------------------------------------------------ long-running VMs

    def start(
        self,
        name: str,
        argv: Sequence[str],
        *,
        publish: Sequence[int] = (),
        kvm: bool = False,
        network: bool = True,
        labels: Mapping[str, str] | None = None,
        writable: Sequence[Path] = (),
        memory_mb: int | None = None,
    ) -> str:
        """Start ``argv`` detached in a container called ``name``. Each port in ``publish`` is
        published on a free host port on the loopback interface. Returns the container id.

        With ``writable`` the VM home is mounted read-only and only those directories (under the
        VM home) are writable; secrets in ``SECRET_PATHS`` are always hidden."""
        args = ["run", "-d", "--init", "--name", name, "--label", f"{VM_LABEL}={name}"]
        for key, value in (labels or {}).items():
            args += ["--label", f"{key}={value}"]
        args += self._guest_mounts(writable)
        args += ["--security-opt", "no-new-privileges", "--cap-drop", "ALL", "--pids-limit", str(GUEST_PIDS_LIMIT)]
        if memory_mb is not None:
            args += ["--memory", f"{int(memory_mb) + GUEST_MEMORY_OVERHEAD_MB}m"]
        if kvm:
            # /dev/kvm is owned by a host group whose id the container does not know, so a KVM
            # guest's QEMU runs as the container's root instead of the calling user.
            args += ["--device", "/dev/kvm", "--cap-add", "DAC_OVERRIDE"]
        else:
            args += self._user()
        if not network:
            args += ["--network", "none"]
        for port in publish:
            args += ["-p", f"{LOOPBACK}::{int(port)}"]
        args += [self.image, *argv]
        return self._docker(args, timeout=120).stdout.strip()

    def port(self, name: str, container_port: int) -> int:
        """The host port published for ``container_port``."""
        out = self._docker(["port", name, f"{int(container_port)}/tcp"], timeout=20).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.startswith(f"{LOOPBACK}:"):
                return int(line.rsplit(":", 1)[1])
        for line in out.splitlines():
            if ":" in line:
                return int(line.strip().rsplit(":", 1)[1])
        raise DockerError(f"no published port for {name}:{container_port}", out)

    def running(self, name: str) -> bool:
        proc = self._docker(["inspect", "--format", "{{.State.Running}}", name], timeout=20, check=False)
        return proc.returncode == 0 and proc.stdout.strip() == "true"

    def exit_code(self, name: str) -> int | None:
        """The container's exit code, or None while it runs or if it does not exist."""
        proc = self._docker(["inspect", "--format", "{{json .State}}", name], timeout=20, check=False)
        if proc.returncode != 0:
            return None
        state = json.loads(proc.stdout or "{}")
        if state.get("Running"):
            return None
        return int(state.get("ExitCode", -1))

    def logs(self, name: str, tail: int = 60) -> str:
        proc = self._docker(["logs", "--tail", str(int(tail)), name], timeout=30, check=False)
        return (proc.stdout or "") + (proc.stderr or "")

    def remove(self, name: str) -> None:
        """Force-remove the container (no error if it is already gone)."""
        self._docker(["rm", "-f", name], timeout=60, check=False)

    def list_vms(self) -> list[str]:
        out = self._docker(["ps", "-a", "--filter", f"label={VM_LABEL}", "--format", "{{.Names}}"], timeout=20).stdout
        return [line.strip() for line in out.splitlines() if line.strip()]

    # ------------------------------------------------------------------ image build

    def build_image(
        self, context_dir: Path, *, platform: str | None = None, no_cache: bool = False, timeout: float = 1800
    ) -> None:
        """Build the runner image from ``context_dir`` (holding the Dockerfile). ``no_cache``
        rebuilds every layer (and pulls the base image again) instead of reusing cached ones."""
        args = ["build", "-t", self.image]
        if platform:
            args += ["--platform", platform]
        if no_cache:
            args += ["--no-cache", "--pull"]
        args += [str(context_dir)]
        self._docker(args, timeout=timeout)
        self._arch = self._kvm = None  # the new image may be for another architecture

    def native_platform(self) -> str:
        """``linux/arm64`` or ``linux/amd64``, from the docker daemon's architecture."""
        return f"linux/{self.daemon_arch()}"


_ANON_HELPER = """#!/bin/sh
# No stored credentials: every lookup answers "not found", so registry pulls are anonymous.
case "$1" in
  get) cat >/dev/null; echo "credentials not found in native keychain"; exit 1 ;;
  list) echo "{}" ;;
  *) cat >/dev/null ;;
esac
"""


def anonymous_docker_env(docker: str = "docker", run: RunFn = subprocess.run) -> tuple[dict[str, str], Path]:
    """Environment for docker commands that pull public images without any stored credentials.

    Some desktop setups configure a credential helper that can block waiting for a keychain
    prompt. The runner image needs only public images, so this points the docker CLI at a
    temporary config with a helper that never returns credentials. The user's own docker config
    is not changed. Returns the variables and the temporary directory (delete it afterwards).
    """
    tmp = Path(tempfile.mkdtemp(prefix="keyhold-docker-"))
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    helper = bin_dir / "docker-credential-keyholdanon"
    helper.write_text(_ANON_HELPER)
    helper.chmod(0o755)
    config: dict[str, object] = {"credsStore": "keyholdanon"}
    plugin_dirs = [str(Path.home() / ".docker" / "cli-plugins")]
    config["cliPluginsExtraDirs"] = [d for d in plugin_dirs if Path(d).is_dir()]
    (tmp / "config.json").write_text(json.dumps(config))
    env = {"DOCKER_CONFIG": str(tmp), "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
    # Keep the active context's endpoint, which lives in the user's config directory.
    proc = run(
        [docker, "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    host = (proc.stdout or "").strip()
    if proc.returncode == 0 and host and "DOCKER_HOST" not in os.environ:
        env["DOCKER_HOST"] = host
    return env, tmp


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
