"""Run the QEMU runner image with docker: one-shot tool commands and long-running VMs.

Everything Keyhold keeps for its guests lives under one directory on the host (the "VM home",
``var/vm`` by default). That directory is mounted at ``/vm`` in every container, so a path is
translated by swapping the prefix. Disk overlays use relative backing-file names, so the same
qcow2 chain opens on the host and in the container.

Ports a VM publishes are bound to the host loopback only.

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
    "CONTAINER_VM_HOME",
    "LOOPBACK",
    "VM_LABEL",
    "DockerError",
    "DockerRunner",
    "anonymous_docker_env",
]

RUNNER_IMAGE = "keyhold-qemu-runner:2"  # bump when tools/vm/Dockerfile changes
CONTAINER_VM_HOME = "/vm"
LOOPBACK = "127.0.0.1"
VM_LABEL = "keyhold.vm"

RunFn = Callable[..., subprocess.CompletedProcess]

# Paths under the VM home a guest container must never see (see keyhold.console.image.VMHome).
SECRET_PATHS = ("keys", "golden/console-password")
GUEST_PIDS_LIMIT = 512
GUEST_MEMORY_OVERHEAD_MB = 1024  # QEMU itself, the TCG translation cache, socat


class DockerError(ConsoleError):
    """A docker command failed. ``stderr`` holds the tail of docker's error output."""

    def __init__(self, message: str, stderr: str = "") -> None:
        super().__init__(f"{message}: {stderr.strip()[-800:]}" if stderr.strip() else message)
        self.stderr = stderr


@dataclass
class DockerRunner:
    """Thin wrapper over the ``docker`` CLI. ``run`` is injectable so tests never start docker."""

    vm_home: Path
    image: str = RUNNER_IMAGE
    docker: str = "docker"
    run: RunFn = subprocess.run
    env: Mapping[str, str] | None = None
    _kvm: bool | None = field(default=None, init=False, repr=False)

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

    def kvm_available(self) -> bool:
        """True when the docker host has /dev/kvm and the runner is aarch64 (KVM cannot run a
        guest of another architecture). Cached after the first call."""
        if self._kvm is None:
            probe = [
                "run",
                "--rm",
                "--device",
                "/dev/kvm",
                self.image,
                "sh",
                "-c",
                'test -c /dev/kvm && test "$(uname -m)" = aarch64',
            ]
            try:
                self._kvm = self._docker(probe, timeout=60, check=False).returncode == 0
            except DockerError:
                self._kvm = False
        return self._kvm

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

    def native_platform(self) -> str:
        """``linux/arm64`` or ``linux/amd64``, from the docker daemon's architecture."""
        arch = self._docker(["info", "--format", "{{.Architecture}}"], timeout=20).stdout.strip()
        return {"aarch64": "linux/arm64", "arm64": "linux/arm64", "x86_64": "linux/amd64", "amd64": "linux/amd64"}.get(
            arch, f"linux/{arch}"
        )


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
