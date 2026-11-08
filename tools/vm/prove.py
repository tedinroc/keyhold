"""Prove the console layer on real guests: one fault at a time, through the PayPal lock.

    uv run python tools/vm/prove.py                      every fault in the catalog
    uv run python tools/vm/prove.py sshd-masked grub-password
    uv run python tools/vm/prove.py --out runs/real-vm/my-run

For each fault this boots a fresh run overlay of the fault image in a runner container and:

1. waits for the fault's marker on the serial log and saves a screenshot of the symptom;
2. runs the owner probe (SSH login with the probe key + the demo page's sha256) and expects it
   to fail;
3. runs the free look on the real screen (a scripted reply stands in for Claude) and stores the
   quote in a fresh ledger;
4. tries to type through a ``GatedConsole`` before any hold exists and expects ``HoldRevoked``
   with no key reaching the console;
5. places the hold with FakePayPal (order, owner approval, authorization);
6. runs the rescue with ``Orchestrator.run``: the fault's deterministic fix script
   (``keyhold.faults.fixes``) plays the model's part, and every key goes through the agent loop,
   the keystroke firewall, the gated console and the hold lease;
7. for a solvable fault, expects the owner probe to pass twice at least 30 seconds apart and the
   ledger to hold exactly one capture of the quote (``cap-{rescue_id}``), and replays the capture
   to show the replay moves no money; for the unsolvable fault, expects ``give_up`` and a void
   (``void-{rescue_id}``) with no capture;
8. checks that a new gated session cannot type once the rescue has ended, shuts the guest down
   and deletes the run overlay.

Results go to ``runs/real-vm/<UTC time>/``: screenshots, the serial log, the ledger, FakePayPal's
state, ``result.json`` per fault and ``summary.json``. The console password and the probe key are
never written there (the script checks every file at the end). Guests run one at a time; the
container is removed by name even when a step fails.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from keyhold.agent.model import FakeModel, json_reply, tool_reply
from keyhold.console.base import ConsoleError
from keyhold.console.docker import DockerRunner
from keyhold.console.image import (
    VMHome,
    base_image_for,
    load_golden_info,
    new_run_vm,
    probe_spec_for,
    read_console_password,
    remove_run,
    verify_base_image,
)
from keyhold.console.qemu import QemuVM
from keyhold.faults import FAULTS, Fault
from keyhold.faults.fixes import FixStep, fix_for, render_input
from keyhold.pay.client import FakePayPal
from keyhold.pay.gate import GatedConsole, HoldLease
from keyhold.pay.ledger import Ledger, Payments
from keyhold.probe.owner import HttpxFetcher, SubprocessSsh, known_hosts_path, run_probe
from keyhold.rescue import Orchestrator, RescueConfig
from keyhold.types import AgentEvent, AgentOutcome, HoldRevoked, MoneyOp, OutboxStatus, Quote, RescueState, Tier

PROJECT = Path(__file__).resolve().parents[2]
BOOT_TIMEOUT = 600.0
STABLE_GAP_S = 30.0


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------


class CountingConsole:
    """Passes everything to the real console and counts the inputs that reached it."""

    def __init__(self, console: Any) -> None:
        self._console = console
        self.inputs: list[tuple[str, float]] = []

    def screenshot(self) -> bytes:
        return self._console.screenshot()

    def type_text(self, text: str) -> None:
        self._console.type_text(text)
        self.inputs.append(("type", time.time()))

    def press(self, keys: list[str]) -> None:
        self._console.press(keys)
        self.inputs.append(("key", time.time()))

    def power_cycle(self) -> None:
        self._console.power_cycle()
        self.inputs.append(("power_cycle", time.time()))

    def close(self) -> None:
        self._console.close()


@dataclass
class Pacer:
    """Builds the scripted model: before handing out step N it waits for step N-1's serial
    marker, so the script keeps pace with the emulated guest instead of guessing."""

    vm: QemuVM
    steps: tuple[FixStep, ...]
    password: str
    mark: int = 0
    waits: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def model(self) -> FakeModel:
        return FakeModel([self._reply(i) for i in range(len(self.steps))])

    def _reply(self, index: int):
        def reply(_request: Any):
            if index > 0:
                self._wait_after(self.steps[index - 1], index - 1)
            self.mark = len(self.vm.serial_text())
            step = self.steps[index]
            return tool_reply(step.tool, render_input(step, self.password))

        return reply

    def _wait_after(self, step: FixStep, index: int) -> None:
        started = time.monotonic()
        if step.wait_for:
            match = self.vm.wait_for_serial(step.wait_for, timeout=step.timeout, start=self.mark)
            waited = time.monotonic() - started
            self.waits.append(
                {"step": index, "wait_for": step.wait_for, "seen": match is not None, "s": round(waited, 1)}
            )
            if match is None:
                self.error = f"step {index}: {step.wait_for!r} did not appear within {step.timeout:.0f}s"
                raise RuntimeError(self.error)
        if step.pause:
            time.sleep(step.pause)


class Recorder:
    """Collects orchestrator events and writes their screenshots as numbered PNG files."""

    def __init__(self, shots: Path) -> None:
        self.shots = shots
        self.events: list[dict[str, Any]] = []
        self.count = 0

    def __call__(self, rescue_id: str, event: AgentEvent) -> None:
        data = {k: v for k, v in event.data.items() if not isinstance(v, (bytes, bytearray))}
        png = event.data.get("png")
        if isinstance(png, (bytes, bytearray)):
            self.count += 1
            name = f"{self.count:02d}-turn{event.turn:02d}-{event.kind}.png"
            (self.shots / name).write_bytes(bytes(png))
            data["file"] = name
        self.events.append(
            {
                "rescue": rescue_id,
                "kind": event.kind,
                "detail": event.detail,
                "turn": event.turn,
                "at": event.at,
                "data": data,
            }
        )


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n")


def _gate_refuses(console: CountingConsole, ledger: Ledger, gateway: Any, rescue_id: str) -> dict[str, Any]:
    """Try to type and press Enter through a fresh gated session; both must be refused before
    anything reaches the console."""
    before = len(console.inputs)
    lease = HoldLease(ledger, gateway, rescue_id)
    gated = GatedConsole(console, lease)
    refused = []
    for attempt in (lambda: gated.type_text("root\n"), lambda: gated.press(["ret"]), gated.power_cycle):
        try:
            attempt()
            refused.append(None)
        except HoldRevoked as exc:
            refused.append(str(exc))
    return {
        "refused": all(r is not None for r in refused),
        "reasons": refused,
        "inputs_reached_console": len(console.inputs) - before,
        "inputs_refused": gated.inputs_refused,
    }


def _probe(spec: Any, ssh: SubprocessSsh, http: HttpxFetcher) -> dict[str, Any]:
    result = run_probe(spec, ssh=ssh, http=http)
    return {
        "ok": result.ok,
        "ssh_ok": result.ssh_ok,
        "http_ok": result.http_ok,
        "detail": result.detail,
        "at": result.at,
    }


def _quote_payload(fault: Fault) -> dict[str, Any]:
    """What the scripted free look answers: the fault's class, a symptom line as evidence."""
    tier = fault.tier.value if fault.tier is not None else "T1"
    return {
        "fault_class": fault.fault_class,
        "evidence": [fault.symptom[0]],
        "tier": tier,
        "plan": f"From the console: {fault.title.lower()}; repair it and restart what it stopped.",
    }


# ---------------------------------------------------------------------------------------------
# One fault
# ---------------------------------------------------------------------------------------------


def prove_fault(home: VMHome, runner: DockerRunner, fault: Fault, out: Path) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    shots = out / "shots"
    shots.mkdir(exist_ok=True)
    started = time.monotonic()
    timeline: list[tuple[float, str]] = []

    def mark(label: str) -> None:
        timeline.append((round(time.monotonic() - started, 1), label))
        print(f"  [{timeline[-1][0]:7.1f}s] {label}", flush=True)

    checks: dict[str, bool] = {}
    result: dict[str, Any] = {"fault": fault.id, "title": fault.title, "solvable": fault.solvable, "checks": checks}
    password = read_console_password(home)
    info = load_golden_info(home)
    steps = fix_for(fault.id)

    vm = new_run_vm(home, runner, fault.id)
    run_id = vm.run_dir.name
    result["arch"] = vm.arch
    mark(f"run overlay created ({vm.arch} guest)")
    console: CountingConsole | None = None
    ledger: Ledger | None = None
    recorder: Recorder | None = None
    try:
        endpoints = vm.start()
        result["accel"] = vm.accel
        if vm.kvm_fallback:
            result["kvm_fallback"] = vm.kvm_fallback
            mark("KVM failed to start the guest; started again under TCG")
        mark(f"container started ({vm.accel})")
        if vm.wait_for_serial(fault.ready_marker, timeout=BOOT_TIMEOUT) is None:
            raise RuntimeError(f"the symptom marker {fault.ready_marker!r} did not appear in {BOOT_TIMEOUT:.0f}s")
        mark("symptom marker on the serial log")
        # The screen can lag the serial log (the emergency prompt is drawn after its marker).
        time.sleep(fault.screen_settle_s)
        console = CountingConsole(vm.console())
        (out / "symptom.png").write_bytes(console.screenshot())
        mark("symptom screenshot saved")

        spec = probe_spec_for(info, endpoints)
        state_dir = out / "probe-state"
        http = HttpxFetcher()
        pre = _probe(spec, SubprocessSsh(home.probe_key, known_hosts_path(state_dir, f"pre-{run_id}")), http)
        result["probe_before"] = pre
        checks["owner probe fails before the fix"] = not pre["ok"]
        mark(f"owner probe before the fix: {'pass' if pre['ok'] else 'fail'} ({pre['detail']})")

        ledger = Ledger(out / "ledger.sqlite3")
        fake = FakePayPal(state_path=out / "fakepaypal.json")
        recorder = Recorder(shots)
        orch = Orchestrator(
            ledger, fake, config=RescueConfig(max_turns=len(steps) + 4, warranty=False), on_event=recorder
        )

        look, rec = orch.free_look(console, FakeModel([json_reply(_quote_payload(fault))]), probe=spec)
        result["free_look"] = {"fault_class": look.fault_class, "quote": look.quote is not None, "reason": look.reason}
        mark(f"free look: {look.reason}")
        if fault.solvable:
            checks["free look gives a quote"] = rec is not None
            assert rec is not None, look.reason
        else:
            checks["free look offers no hold for a locked console"] = (
                rec is None and look.fault_class == "console_locked"
            )
            # To prove give_up -> void, open a quote as if the look had read the prompt as a GRUB
            # configuration error (T2). The agent then finds the password prompt and gives up.
            quote = Quote.build(
                "grub_config_error",
                [fault.symptom[0]],
                Tier.T2,
                "Get past the boot loader prompt and boot the system.",
            )
            rec = orch.open_quote(quote, probe=spec)
            mark("quote opened by hand (T2) to exercise give_up after a hold")
        rid = rec.id
        result["rescue_id"] = rid
        result["quote_amount"] = str(rec.quote_amount)

        before_hold = _gate_refuses(console, ledger, fake, rid)
        result["gate_before_hold"] = before_hold
        checks["no key reaches the console before the hold"] = (
            before_hold["refused"] and before_hold["inputs_reached_console"] == 0
        )
        mark(
            f"gate before the hold: refused={before_hold['refused']}, keys reached console={before_hold['inputs_reached_console']}"
        )

        order = orch.place_hold(rid)
        fake.approve(order.id)  # the owner approves the order in the JS SDK
        rec = orch.confirm_hold(rid)
        checks["hold authorized (HELD)"] = rec.state is RescueState.HELD
        result["hold"] = {
            "order_id": order.id,
            "authorization_id": rec.authorization_id,
            "amount": str(rec.authorized_amount),
        }
        mark(f"hold: order {order.id}, authorization {rec.authorization_id} for ${rec.authorized_amount}")

        pacer = Pacer(vm, steps, password)
        ssh = SubprocessSsh(home.probe_key, known_hosts_path(state_dir, rid))
        agent_started = time.monotonic()
        report = orch.run(rid, console, pacer.model(), ssh=ssh, http=http, console_password=password)
        mark(f"rescue ended: {report.state.value}: {report.reason}")
        result["pacing_waits"] = pacer.waits
        if pacer.error:
            result["pacing_error"] = pacer.error
        result["report"] = {
            "state": report.state.value,
            "reason": report.reason,
            "agent_outcome": report.agent.outcome.value if report.agent else None,
            "agent_reason": report.agent.reason if report.agent else None,
            "agent_turns": report.agent.turns if report.agent else None,
            "capture_id": report.capture_id,
            "captured_amount": str(report.captured_amount) if report.captured_amount is not None else None,
            "lease_checks": report.lease_checks,
            "lease_actions": report.lease_actions,
            "seconds": round(time.monotonic() - agent_started, 1),
        }
        expected_inputs = sum(1 for s in steps if s.tool in ("type", "key", "power_cycle"))
        checks["every scripted input reached the console through the gate"] = len(console.inputs) == expected_inputs
        checks["the lease asked PayPal during the session"] = report.lease_checks >= 1
        checks["no firewall refusals"] = not any(e["kind"] == "firewall_refusal" for e in recorder.events)

        rec = ledger.get(rid)
        ops = {e.op: e for e in ledger.ops(rid)}
        captures = fake.money_calls("capture")
        voids = fake.money_calls("void")
        if fault.solvable:
            checks["agent called finished"] = report.agent is not None and report.agent.outcome is AgentOutcome.DONE
            checks["ledger state CAPTURED"] = rec.state is RescueState.CAPTURED
            checks["captured the quoted amount"] = rec.captured_amount == rec.quote_amount
            cap = ops.get(MoneyOp.CAPTURE)
            checks["one capture, request id cap-{rescue_id}"] = (
                len(captures) == 1
                and len(fake.all_captures()) == 1
                and cap is not None
                and cap.status is OutboxStatus.DONE
                and cap.request_id == f"cap-{rid}"
                and captures[0].request_id == f"cap-{rid}"
            )
            checks["no void"] = not voids and MoneyOp.VOID not in ops
            stable = report.stable
            gap = stable.passes[-1].at - stable.passes[0].at if stable and len(stable.passes) >= 2 else 0.0
            result["stable"] = {
                "passes": len(stable.passes) if stable else 0,
                "gap_s": round(gap, 1),
                "detail": stable.detail if stable else None,
            }
            checks[f"two probe passes at least {STABLE_GAP_S:.0f}s apart"] = (
                bool(stable and stable.stable) and gap >= STABLE_GAP_S
            )
            if rec.state is RescueState.CAPTURED:
                replay = Payments(ledger, fake).capture(rid)
                checks["capture replay moves no money"] = (
                    replay.id == rec.capture_id
                    and len(fake.money_calls("capture")) == 1
                    and len(fake.all_captures()) == 1
                )
            else:
                checks["capture replay moves no money"] = False  # nothing was captured to replay
        else:
            checks["agent called give_up"] = report.agent is not None and report.agent.outcome is AgentOutcome.GAVE_UP
            checks["ledger state VOIDED"] = rec.state is RescueState.VOIDED
            void = ops.get(MoneyOp.VOID)
            checks["one void, request id void-{rescue_id}"] = (
                len(voids) == 1
                and void is not None
                and void.request_id == f"void-{rid}"
                and void.status is OutboxStatus.DONE
            )
            checks["no capture"] = not captures and not fake.all_captures() and rec.capture_id is None
            auth = fake.get_authorization(rec.authorization_id) if rec.authorization_id else None
            checks["PayPal shows the authorization VOIDED"] = auth is not None and auth.status == "VOIDED"
        checks["ledger invariants hold"] = ledger.check_invariants(rid) == []
        result["ledger"] = {
            "state": rec.state.value,
            "quote": str(rec.quote_amount),
            "authorized": str(rec.authorized_amount),
            "captured": str(rec.captured_amount) if rec.captured_amount is not None else None,
            "end_reason": rec.end_reason,
            "outbox": [
                {"op": e.op.value, "request_id": e.request_id, "status": e.status.value, "attempts": e.attempts}
                for e in ledger.ops(rid)
            ],
            "paypal_money_calls": [
                {"op": c.op, "request_id": c.request_id, "outcome": c.outcome} for c in fake.money_calls()
            ],
        }

        after_end = _gate_refuses(console, ledger, fake, rid)
        result["gate_after_end"] = after_end
        checks["no key reaches the console after the rescue ends"] = (
            after_end["refused"] and after_end["inputs_reached_console"] == 0
        )
        (out / "after.png").write_bytes(console.screenshot())
        post = _probe(spec, ssh, http)
        result["probe_after"] = post
        if fault.solvable:
            checks["owner probe passes after the fix"] = post["ok"]
        else:
            checks["owner probe still fails (nothing was fixed)"] = not post["ok"]
        mark(f"owner probe after: {'pass' if post['ok'] else 'fail'} ({post['detail']})")
    except Exception as exc:  # noqa: BLE001 - any failure is recorded as a failed check; cleanup still runs
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()
        checks["completed without an exception"] = False
        mark(f"ERROR {result['error']}")
    finally:
        if recorder is not None:
            result["events"] = recorder.events
        if console is not None:
            with contextlib.suppress(ConsoleError):
                console.close()
        if ledger is not None:
            ledger.close()
        clean = False
        try:
            # A guest stuck at a boot-loader prompt ignores the ACPI power button: no clean
            # shutdown is tried for it.
            if vm.endpoints is not None and fault.acpi_power_off:
                clean = vm.shutdown(timeout=90.0)
        finally:
            vm.kill()  # removes the container by name; a no-op when shutdown already did
        result["clean_shutdown"] = clean
        mark("guest stopped and container removed" + (" (clean ACPI power-off)" if clean else ""))
        if vm.serial_log.exists():
            shutil.copyfile(vm.serial_log, out / "serial.log")
        remove_run(home, run_id)
        result["timeline"] = timeline
        result["seconds"] = round(time.monotonic() - started, 1)

    leaked = _files_containing(out, password.encode())
    checks["console password is in no saved file"] = not leaked
    if leaked:
        result["password_found_in"] = [str(p.relative_to(out)) for p in leaked]
    result["passed"] = all(checks.values())
    _write_json(out / "result.json", result)
    return result


def _files_containing(root: Path, needle: bytes) -> list[Path]:
    hits = []
    for path in root.rglob("*"):
        if path.is_file() and needle in path.read_bytes():
            hits.append(path)
    return hits


# ---------------------------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="prove.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "faults", nargs="*", metavar="fault", help=f"faults to prove (default: all of {', '.join(FAULTS)})"
    )
    parser.add_argument("--out", help="output directory (default: runs/real-vm/<UTC time>)")
    parser.add_argument("--home", help="VM home directory (default: $KEYHOLD_VM_HOME or var/vm)")
    args = parser.parse_args(argv)

    home = VMHome(args.home) if args.home else VMHome()
    runner = DockerRunner(home.root)
    if not runner.available() or not runner.image_present():
        print("docker or the runner image is missing; run: uv run python tools/vm/vm.py runner", file=sys.stderr)
        return 1
    image = base_image_for(runner.arch())
    if not verify_base_image(home.base_image(image), image):
        print("base image missing or unverified; run: uv run python tools/vm/vm.py fetch", file=sys.stderr)
        return 1
    unknown = [f for f in args.faults if f not in FAULTS]
    if unknown:
        parser.error(f"unknown fault(s): {', '.join(unknown)}")
    ids = args.faults or list(FAULTS)
    missing = [f for f in ids if not home.fault_disk(f).is_file()]
    if missing:
        print(
            f"fault images missing ({', '.join(missing)}); run: uv run python tools/vm/vm.py plant all", file=sys.stderr
        )
        return 1

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out) if args.out else PROJECT / "runs" / "real-vm" / stamp
    out.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {"started": stamp, "arch": runner.arch(), "faults": {}}
    for fault_id in ids:
        fault = FAULTS[fault_id]
        print(f"{fault.id}: {fault.title}", flush=True)
        result = prove_fault(home, runner, fault, out / fault.id)
        failed = [name for name, ok in result["checks"].items() if not ok]
        summary["faults"][fault.id] = {
            "passed": result["passed"],
            "seconds": result["seconds"],
            "state": result.get("ledger", {}).get("state"),
            "failed_checks": failed,
            "timeline": result["timeline"],
        }
        print(
            f"  {'PASS' if result['passed'] else 'FAIL'} in {result['seconds']:.0f}s"
            + (f"; failed: {', '.join(failed)}" if failed else ""),
            flush=True,
        )
        _write_json(out / "summary.json", summary)
    passed = all(f["passed"] for f in summary["faults"].values())
    print(f"{'all passed' if passed else 'some faults failed'}; results in {out}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
