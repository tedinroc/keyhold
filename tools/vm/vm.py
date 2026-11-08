"""Build and run Keyhold's local test guests.

    uv run python tools/vm/vm.py runner              build the QEMU runner image (docker)
    uv run python tools/vm/vm.py fetch               download the Debian 13 arm64 image, verify SHA512
    uv run python tools/vm/vm.py seed                build the cloud-init seed only (for inspection)
    uv run python tools/vm/vm.py golden              boot once with the seed -> golden.qcow2
    uv run python tools/vm/vm.py plant all           plant every fault -> faults/<id>.qcow2
    uv run python tools/vm/vm.py overlay <fault>     create a run overlay (no boot)
    uv run python tools/vm/vm.py up <fault|healthy>  overlay + boot + wait for the symptom
    uv run python tools/vm/vm.py shot <run-id>       save a console screenshot
    uv run python tools/vm/vm.py down <run-id>       clean shutdown (ACPI), then remove
    uv run python tools/vm/vm.py status              what is built and running
    uv run python tools/vm/vm.py clean               remove leftover containers and run overlays

Everything is kept under var/vm (or $KEYHOLD_VM_HOME). Secrets there are never printed.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from keyhold.console.base import ConsoleError
from keyhold.console.docker import DockerRunner, anonymous_docker_env, remove_tree
from keyhold.console.image import (
    DEBIAN_13_ARM64,
    GoldenConfig,
    VMHome,
    build_golden,
    build_iso,
    ensure_console_password,
    ensure_probe_key,
    fetch_base_image,
    load_golden_info,
    new_run,
    probe_spec_for,
    remove_run,
    verify_base_image,
    write_seed_dir,
)
from keyhold.console.qemu import QemuVM
from keyhold.faults import FAULTS, get_fault
from keyhold.faults.plant import plant_fault

HERE = Path(__file__).resolve().parent


def _runner(home: VMHome) -> DockerRunner:
    return DockerRunner(home.root)


def cmd_runner(args: argparse.Namespace, home: VMHome) -> int:
    runner = _runner(home)
    tmp = None
    if not args.use_my_docker_config:
        env, tmp = anonymous_docker_env()
        runner.env = env
    try:
        platform = runner.native_platform()
        print(f"building {runner.image} for {platform} (this pulls debian:trixie-slim and QEMU packages)")
        runner.build_image(HERE, platform=platform, no_cache=args.no_cache)
    finally:
        if tmp is not None:
            remove_tree(tmp)
    print(f"built {runner.image}")
    return 0


def cmd_fetch(args: argparse.Namespace, home: VMHome) -> int:
    last = [0.0]

    def progress(done: int, total: int | None) -> None:
        now = time.monotonic()
        if now - last[0] >= 5 or (total and done >= total):
            last[0] = now
            share = f"{done * 100 // total}%" if total else f"{done >> 20} MB"
            print(f"  downloaded {share}", flush=True)

    path = fetch_base_image(home, DEBIAN_13_ARM64, progress=progress)
    print(f"verified {path} (SHA512 {DEBIAN_13_ARM64.sha512[:16]}...)")
    return 0


def cmd_seed(args: argparse.Namespace, home: VMHome) -> int:
    import secrets

    runner = _runner(home)
    cfg = GoldenConfig(
        probe_public_key=ensure_probe_key(home),
        console_password=ensure_console_password(home),
        page_token=secrets.token_hex(8),
    )
    out = home.golden_dir / "seed"
    files = write_seed_dir(out, cfg)
    iso = build_iso(runner, home.golden_dir / "seed.iso", files, label="cidata", private=True)
    print(f"seed files in {out} (mode 600; console-password is secret)")
    print(f"seed image {iso}")
    return 0


def cmd_golden(args: argparse.Namespace, home: VMHome) -> int:
    runner = _runner(home)
    if not verify_base_image(home.base_image()):
        fetch_base_image(home)
    print("booting the base image with the cloud-init seed (several minutes under emulation)...")
    info = build_golden(home, runner, force=args.force, timeout=args.timeout)
    print(f"golden image {home.golden_disk}")
    print(f"page sha256 {info.page_sha256}; probe user {info.probe_user}; console user {info.console_user}")
    return 0


def cmd_plant(args: argparse.Namespace, home: VMHome) -> int:
    runner = _runner(home)
    ids = list(FAULTS) if args.fault == "all" else [args.fault]
    for fault_id in ids:
        fault = get_fault(fault_id)
        started = time.monotonic()
        print(f"planting {fault.id} ({fault.title})...", flush=True)
        path = plant_fault(home, runner, fault, force=args.force, timeout=args.timeout)
        print(f"  {path} in {time.monotonic() - started:.0f}s")
    return 0


def _fault_arg(name: str) -> str | None:
    return None if name == "healthy" else get_fault(name).id


def cmd_overlay(args: argparse.Namespace, home: VMHome) -> int:
    run_id, disk = new_run(home, _runner(home), _fault_arg(args.fault), args.run_id)
    print(f"run {run_id}: {disk}")
    return 0


def cmd_up(args: argparse.Namespace, home: VMHome) -> int:
    runner = _runner(home)
    fault_id = _fault_arg(args.fault)
    run_id, disk = new_run(home, runner, fault_id, args.run_id)
    vm = QemuVM(runner, name=f"keyhold-run-{run_id}", run_dir=home.run_dir(run_id), disk=disk)
    endpoints = vm.start()
    print(f"run {run_id} started ({vm.accel}); container {vm.name}")
    marker = get_fault(fault_id).ready_marker if fault_id else r"login:"
    started = time.monotonic()
    if vm.wait_for_serial(marker, timeout=args.timeout) is None:
        print(f"the symptom did not show within {args.timeout}s; serial log: {vm.serial_log}", file=sys.stderr)
    else:
        print(f"ready after {time.monotonic() - started:.0f}s")
    time.sleep(2)
    console = vm.console()
    shot = home.run_dir(run_id) / "screen.png"
    shot.write_bytes(console.screenshot())
    console.close()
    info = load_golden_info(home)
    spec = probe_spec_for(info, endpoints)
    print(f"screenshot {shot}")
    print(f"ssh -p {spec.ssh_port} -i {home.probe_key} {spec.ssh_user}@{spec.host}    (owner probe)")
    print(f"page {spec.url} (sha256 {info.page_sha256[:16]}...)")
    print(f"console login: {info.console_user}; password in {home.console_password_file}")
    print(f"stop with: uv run python tools/vm/vm.py down {run_id}")
    return 0


def _attached(home: VMHome, run_id: str) -> QemuVM:
    runner = _runner(home)
    vm = QemuVM(
        runner, name=f"keyhold-run-{run_id}", run_dir=home.run_dir(run_id), disk=home.run_dir(run_id) / "disk.qcow2"
    )
    vm.attach()
    return vm


def cmd_shot(args: argparse.Namespace, home: VMHome) -> int:
    vm = _attached(home, args.run_id)
    console = vm.console()
    out = Path(args.output) if args.output else home.run_dir(args.run_id) / f"screen-{int(time.time())}.png"
    out.write_bytes(console.screenshot())
    console.close()
    print(out)
    return 0


def cmd_down(args: argparse.Namespace, home: VMHome) -> int:
    vm = _attached(home, args.run_id)
    clean = vm.shutdown(timeout=args.timeout)
    print("guest powered off cleanly" if clean else "guest did not power off in time; QEMU was stopped")
    if not args.keep:
        remove_run(home, args.run_id)
    return 0


def cmd_status(args: argparse.Namespace, home: VMHome) -> int:
    runner = _runner(home)
    print(f"VM home      {home.root}")
    print(f"docker       {'ok' if runner.available() else 'not available'}")
    print(f"runner image {'present' if runner.image_present() else 'missing (vm.py runner)'}")
    base = home.base_image()
    print(f"base image   {'verified' if verify_base_image(base) else 'missing or unverified (vm.py fetch)'}")
    print(f"golden       {'built' if home.golden_disk.is_file() else 'not built (vm.py golden)'}")
    for fault_id in FAULTS:
        print(f"  fault {fault_id:22} {'built' if home.fault_disk(fault_id).is_file() else '-'}")
    runs = sorted(p.name for p in home.runs_dir.glob("*") if p.is_dir()) if home.runs_dir.is_dir() else []
    print(f"run overlays {', '.join(runs) or 'none'}")
    if runner.available():
        print(f"containers   {', '.join(runner.list_vms()) or 'none'}")
    return 0


def cmd_clean(args: argparse.Namespace, home: VMHome) -> int:
    runner = _runner(home)
    for name in runner.list_vms():
        runner.remove(name)
        print(f"removed container {name}")
    if home.runs_dir.is_dir():
        for run in sorted(home.runs_dir.iterdir()):
            if run.is_dir():
                remove_run(home, run.name)
                print(f"removed run {run.name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vm.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--home", help="VM home directory (default: $KEYHOLD_VM_HOME or var/vm)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("runner", help="build the QEMU runner image")
    p.add_argument(
        "--use-my-docker-config",
        action="store_true",
        help="use your docker credential helper instead of anonymous pulls",
    )
    p.add_argument("--no-cache", action="store_true", help="rebuild every layer instead of reusing the build cache")
    p.set_defaults(func=cmd_runner)

    sub.add_parser("fetch", help="download and verify the base image").set_defaults(func=cmd_fetch)
    sub.add_parser("seed", help="build the cloud-init seed without booting").set_defaults(func=cmd_seed)

    p = sub.add_parser("golden", help="build the golden image")
    p.add_argument("--force", action="store_true", help="rebuild (deletes fault images)")
    p.add_argument("--timeout", type=float, default=1500)
    p.set_defaults(func=cmd_golden)

    p = sub.add_parser("plant", help="plant a fault (or all)")
    p.add_argument("fault", choices=[*FAULTS, "all"])
    p.add_argument("--force", action="store_true")
    p.add_argument("--timeout", type=float, default=900)
    p.set_defaults(func=cmd_plant)

    p = sub.add_parser("overlay", help="create a run overlay")
    p.add_argument("fault", choices=[*FAULTS, "healthy"])
    p.add_argument("--run-id")
    p.set_defaults(func=cmd_overlay)

    p = sub.add_parser("up", help="boot a run and wait for the symptom")
    p.add_argument("fault", choices=[*FAULTS, "healthy"])
    p.add_argument("--run-id")
    p.add_argument("--timeout", type=float, default=600)
    p.set_defaults(func=cmd_up)

    p = sub.add_parser("shot", help="save a console screenshot of a running run")
    p.add_argument("run_id")
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_shot)

    p = sub.add_parser("down", help="shut a run down cleanly and delete its overlay")
    p.add_argument("run_id")
    p.add_argument("--timeout", type=float, default=120)
    p.add_argument("--keep", action="store_true", help="keep the run directory")
    p.set_defaults(func=cmd_down)

    sub.add_parser("status", help="show what is built and running").set_defaults(func=cmd_status)
    sub.add_parser("clean", help="remove containers and run overlays").set_defaults(func=cmd_clean)

    args = parser.parse_args(argv)
    home = VMHome(args.home) if args.home else VMHome()
    try:
        return args.func(args, home)
    except (ConsoleError, KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
