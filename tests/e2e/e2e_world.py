"""A small pretend world for end-to-end rescue tests: a scripted console, the owner's server as
the probe sees it, scripted model replies and FakePayPal. No VM, no network, no model."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from keyhold.agent.model import FakeModel, json_reply, tool_reply
from keyhold.console.fake import FakeConsole, InputRecord, Rule, Screen
from keyhold.pay.client import FakePayPal
from keyhold.pay.ledger import Ledger, RescueRecord
from keyhold.probe.fake import FakeServer
from keyhold.rescue import Orchestrator
from keyhold.types import ProbeSpec, Tier

WEBHOOK_ID = "WH-E2E-0001"
# Test-only console password for the scripted console (not a real credential).
CONSOLE_PASSWORD = "e2e-console-pw-not-real"
PAGE_TEXT = "keyhold demo page"

SSHD_FAILED = "[FAILED] Failed to start ssh.service - OpenBSD Secure Shell server."
EMERGENCY = "You are in emergency mode. After logging in, type \"journalctl -xb\" to view"
GRUB_LOCK = "Enter username:"

SPEC = ProbeSpec(
    host="demo.test",
    ssh_port=22,
    ssh_user="probe",
    probe_public_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleOnlyNotARealKey",
    url="http://demo.test/",
    expect_substring=PAGE_TEXT,
)


class SimulatedCrash(BaseException):
    """Stands in for the process dying. A BaseException, so no ``except Exception`` swallows it."""


def crash_at(op: str, phase: str):
    fired = {"done": False}

    def hook(call_op: str, call_phase: str, _request_id: str | None) -> None:
        if not fired["done"] and call_op == op and call_phase == phase:
            fired["done"] = True
            raise SimulatedCrash(f"{op} {phase}")

    return hook


@dataclass
class SshdWorld:
    """sshd will not start because of a typo on line 3 of sshd_config. The web page still works.

    Typing ``sed -i '3d' /etc/ssh/sshd_config`` and then ``systemctl restart ssh`` in the root
    shell fixes the server (the probe's SSH half starts passing).
    """

    console: FakeConsole
    server: FakeServer
    fixed_config: bool = False
    restarts: list[str] = field(default_factory=list)

    @classmethod
    def build(cls) -> SshdWorld:
        console = FakeConsole(
            {
                "boot": Screen(
                    [SSHD_FAILED, "", "Debian GNU/Linux 13 keyhold-demo tty1", ""],
                    prompt="keyhold-demo login: ",
                ),
                "password": Screen([], prompt="Password: ", echo=False),
                "shell": Screen(["Linux keyhold-demo 6.12.0-arm64"], prompt="root@keyhold-demo:~# "),
            },
            rules=[
                Rule(on="boot", line="root", goto="password"),
                Rule(on="password", line=re.escape(CONSOLE_PASSWORD), goto="shell"),
                Rule(on="password", line=".*", goto="boot", output=["Login incorrect"]),
                Rule(
                    on="shell",
                    line=r"sshd -t",
                    output=["/etc/ssh/sshd_config line 3: Bad configuration option: PermitRootLogn"],
                ),
            ],
        )
        world = cls(console, FakeServer(ssh_up=False, http_up=True, body=f"<p>{PAGE_TEXT}</p>"))
        console.on_input(world._watch)
        return world

    def _watch(self, record: InputRecord) -> None:
        if record.kind != "type" or record.screen != "shell":
            return
        if "sed -i '3d' /etc/ssh/sshd_config" in record.value:
            self.fixed_config = True
        if "systemctl restart ssh" in record.value:
            self.restarts.append(record.value)
            if self.fixed_config:
                self.server.fix()

    def screen_text(self) -> str:
        return "\n".join(self.console.visible_lines())


def emergency_console() -> FakeConsole:
    return FakeConsole(
        {
            "emergency": Screen(
                [
                    "[ TIME ] Timed out waiting for device dev-disk-by\\x2duuid-7d1e3c55.device.",
                    "[DEPEND] Dependency failed for srv-data.mount - /srv/data.",
                    EMERGENCY,
                    "Enter root password for system maintenance",
                ],
                prompt="(or press Control-D to continue): ",
                echo=False,
            )
        }
    )


def grub_console() -> FakeConsole:
    return FakeConsole({"grub": Screen([GRUB_LOCK], prompt="")})


def quote_reply(fault_class: str, evidence: str, tier: Tier, plan: str = "Fix it from the console."):
    return json_reply({"fault_class": fault_class, "evidence": [evidence], "tier": tier.value, "plan": plan})


def sshd_quote_reply():
    return quote_reply(
        "sshd_config_error",
        SSHD_FAILED,
        Tier.T1,
        "Log in, run sshd -t, remove the bad line, restart ssh.",
    )


def fix_script(*, finish: bool = True) -> list:
    """The agent's replies for a clean repair of the sshd world."""
    steps = [
        tool_reply("type", {"text": "root\n"}),
        tool_reply("type", {"text": CONSOLE_PASSWORD + "\n"}, thinking="Logging in on the console."),
        tool_reply("type", {"text": "sshd -t\n"}),
        tool_reply("type", {"text": "cp /etc/ssh/sshd_config /etc/ssh/sshd_config.keyhold-bak\n"}),
        tool_reply("type", {"text": "sed -i '3d' /etc/ssh/sshd_config\n"}),
        tool_reply("type", {"text": "systemctl restart ssh\n"}),
    ]
    if finish:
        steps.append(tool_reply("finished", {"summary": "Removed the misspelled option on line 3 and restarted ssh."}))
    return steps


def held(orch: Orchestrator, fake: FakePayPal, look_console, model: FakeModel, *, screen_text=None) -> RescueRecord:
    """Free look, then the hold: order, owner approval, authorization."""
    look, rec = orch.free_look(look_console, model, probe=SPEC, screen_text=screen_text)
    assert rec is not None, look.reason
    order = orch.place_hold(rec.id)
    fake.approve(order.id)
    rec = orch.confirm_hold(rec.id)
    assert rec.state.value == "HELD"
    return rec


def all_event_text(ledger: Ledger) -> str:
    """Every stored event's detail and data, for leak checks."""
    return "\n".join(f"{e.kind} {e.detail} {e.data}" for e in ledger.events())


def money_calls(fake: FakePayPal, op: str) -> list:
    return [c for c in fake.calls if c.op == op and c.outcome in ("ok", "replay")]
