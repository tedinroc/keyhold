"""The deterministic console fix scripts: every fault has one, each passes the keystroke firewall
in order, and the console password appears only as a placeholder."""

from __future__ import annotations

import re

import pytest

from keyhold.agent.firewall import KeystrokeFirewall
from keyhold.agent.keys import parse_combo
from keyhold.faults import FAULTS
from keyhold.faults.fixes import FIXES, PASSWORD, FixStep, fix_for, render_input

# A made-up password with the characters a generated one can hold (letters, digits, - and _).
FAKE_PASSWORD = "Fake-console_pw-0123456789xyz"


def test_every_fault_has_a_fix_script() -> None:
    assert set(FIXES) == set(FAULTS)
    with pytest.raises(KeyError):
        fix_for("no-such-fault")


@pytest.mark.parametrize("fault_id", list(FIXES))
def test_script_shape(fault_id: str) -> None:
    steps = fix_for(fault_id)
    fault = FAULTS[fault_id]
    assert steps[-1].tool == ("finished" if fault.solvable else "give_up")
    assert all(not s.final for s in steps[:-1])
    for step in steps:
        if step.wait_for:
            re.compile(step.wait_for)
        if step.secret:
            assert step.tool == "type" and step.text == PASSWORD + "\n"
        else:
            assert "console_password" not in step.text


@pytest.mark.parametrize("fault_id", list(FIXES))
def test_script_passes_the_keystroke_firewall(fault_id: str) -> None:
    firewall = KeystrokeFirewall()
    for step in fix_for(fault_id):
        tool_input = render_input(step, FAKE_PASSWORD)
        if step.tool == "type":
            verdict = firewall.inspect_type(tool_input["text"])
            assert verdict.allowed, (step.text, verdict)
        elif step.tool == "key":
            verdict = firewall.inspect_keys(parse_combo(tool_input["combo"]))
            assert verdict.allowed, (step.text, verdict)
        elif step.tool == "power_cycle":
            firewall.reset()


def test_render_input_fills_the_password_only_where_asked() -> None:
    login = FIXES["sshd-masked"][1]
    assert login.secret
    assert render_input(login, FAKE_PASSWORD) == {"text": FAKE_PASSWORD + "\n"}
    assert render_input(FixStep("key", "ctrl+c"), FAKE_PASSWORD) == {"combo": "ctrl+c"}
    assert render_input(FixStep("give_up", "locked"), FAKE_PASSWORD) == {"reason": "locked"}
    assert render_input(FixStep("finished", "done"), FAKE_PASSWORD) == {"summary": "done"}
    assert render_input(FixStep("power_cycle"), FAKE_PASSWORD) == {}


def test_fixstep_rejects_bad_steps() -> None:
    with pytest.raises(ValueError):
        FixStep("rm", "x")
    with pytest.raises(ValueError):
        FixStep("type", "")
    with pytest.raises(ValueError):
        FixStep("type", "x", timeout=0)


def test_the_firewall_would_refuse_a_destructive_shortcut() -> None:
    """Sanity check that the test above is meaningful: the same firewall refuses a wiping fix."""
    assert not KeystrokeFirewall().inspect_type("mkfs.ext4 /dev/vda1\n").allowed
