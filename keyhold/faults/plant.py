"""Plant a fault into a disk image with a prepare boot.

The golden image carries a small hook (``keyhold-plant.service``). At boot it looks for a disk
labelled ``KHPLANT``; if there is none it does nothing. A prepare boot attaches such a disk with
the fault's ``plant.sh``: the hook runs it as root, removes itself, wipes the journal so the
planting leaves no trace, prints ``KEYHOLD-PLANT-OK <fault>`` on the serial port and powers off.

The result is ``faults/<fault>.qcow2``, an overlay of the golden image that run overlays are
created from (see ``keyhold.console.image.new_run``).
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path

from keyhold.console.docker import DockerRunner
from keyhold.console.image import (
    PLANT_LABEL,
    ImageError,
    VMHome,
    build_iso,
    build_lock,
    check_arch,
    create_overlay,
)
from keyhold.console.qemu import QemuVM
from keyhold.faults import Fault

__all__ = ["plant_script", "plant_marker", "write_plant_files", "plant_fault"]


def plant_script(fault: Fault) -> str:
    """The script the hook runs. It is run with ``bash -euo pipefail``."""
    return f"#!/bin/bash\n# Plant fault {fault.id}: {fault.title}\n{fault.plant}"


def plant_marker(fault: Fault, ok: bool = True) -> str:
    return f"KEYHOLD-PLANT-{'OK' if ok else 'FAILED'} {fault.id}"


def write_plant_files(directory: Path, fault: Fault) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "plant.sh"
    script.write_text(plant_script(fault))
    fault_id = directory / "fault-id"
    fault_id.write_text(fault.id + "\n")
    return [script, fault_id]


def plant_fault(
    home: VMHome,
    runner: DockerRunner,
    fault: Fault,
    *,
    timeout: float = 900.0,
    force: bool = False,
    vm_factory: Callable[..., QemuVM] = QemuVM,
) -> Path:
    """Build ``faults/<fault>.qcow2`` from the golden image. Returns its path.

    Skips the work if the image already exists, unless ``force``.
    """
    with build_lock(home):
        return _plant_locked(home, runner, fault, timeout=timeout, force=force, vm_factory=vm_factory)


def _plant_locked(
    home: VMHome,
    runner: DockerRunner,
    fault: Fault,
    *,
    timeout: float,
    force: bool,
    vm_factory: Callable[..., QemuVM],
) -> Path:
    target = home.fault_disk(fault.id)
    if target.exists() and not force:
        return target
    if not home.golden_disk.is_file():
        raise ImageError("no golden image yet; run tools/vm/vm.py golden first")
    arch = check_arch(home, runner)

    work = home.faults_dir / "build" / fault.id
    shutil.rmtree(work, ignore_errors=True)
    files = write_plant_files(work / "disk", fault)
    iso = build_iso(runner, work / "plant.iso", files, label=PLANT_LABEL)
    building = create_overlay(runner, home.golden_disk, home.faults_dir / f"{fault.id}.building.qcow2")
    vm = vm_factory(
        runner,
        name=f"keyhold-plant-{fault.id}",
        run_dir=work / "boot",
        disk=building,
        extra_drives=[iso],
        stay_on_poweroff=False,
        arch=arch,
    )
    try:
        serial = vm.run_to_poweroff(timeout)
        if plant_marker(fault, ok=True) not in serial:
            failed = plant_marker(fault, ok=False) in serial
            detail = "the plant script failed" if failed else "the plant hook did not report"
            plant_lines = "\n".join(re.findall(r"^plant: .*$", serial, flags=re.MULTILINE)[-30:])
            raise ImageError(f"planting {fault.id}: {detail}.\n{plant_lines or serial[-2000:]}")
        if target.exists():
            os.chmod(target, 0o644)
        os.replace(building, target)
        os.chmod(target, 0o444)
    finally:
        building.unlink(missing_ok=True)
    # Only on success: after a failure the serial log in work/boot is kept for inspection.
    shutil.rmtree(work, ignore_errors=True)
    return target
