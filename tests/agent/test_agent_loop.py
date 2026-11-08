"""Keyboard-only agent loop with FakeModel and FakeConsole: tools, firewall in the path, every
action reported, HoldRevoked ends the run, refusals and API failures are errors, turn limit."""

from __future__ import annotations

import base64
import io
from typing import Any

import pytest
from agent_fakes import FakeConsole, Gate, InstantClock, StubLease, make_png
from PIL import Image

from keyhold.agent.firewall import KeystrokeFirewall
from keyhold.agent.loop import TOOL_NAMES, TOOLS, Settle, run_agent
from keyhold.agent.model import (
    FakeModel,
    ModelUnavailable,
    refusal_reply,
    text_reply,
    tool_reply,
    tool_uses,
    tools_reply,
)
from keyhold.types import AgentEvent, AgentOutcome, HoldRevoked, ModelReply, Quote, Tier

QUOTE = Quote.build(
    "sshd_config_error",
    ["/etc/ssh/sshd_config line 14: Bad configuration option: PermitRootLogn"],
    Tier.T1,
    "1. Log in. 2. Fix line 14. 3. Restart ssh.",
)
FAST = Settle(type=0.5, key=0.25, power_cycle=5.0)


def run(model: FakeModel, console: Any = None, **kwargs: Any):
    events: list[AgentEvent] = []
    console = console if console is not None else FakeConsole()
    kwargs.setdefault("settle", FAST)
    kwargs.setdefault("clock", InstantClock())
    result = run_agent(console, model, goal="Make SSH answer again.", quote=QUOTE,
                       max_turns=kwargs.pop("max_turns", 10), on_event=events.append, **kwargs)
    return result, events, console


def kinds(events: list[AgentEvent]) -> list[str]:
    return [e.kind for e in events]


def last_tool_results(model: FakeModel, call: int) -> list[dict[str, Any]]:
    message = model.calls[call].messages[-1]
    assert message["role"] == "user"
    return message["content"]


def test_tools_are_exactly_the_six_keyboard_tools_and_strict() -> None:
    assert TOOL_NAMES == {"screenshot", "type", "key", "power_cycle", "give_up", "finished"}
    for tool in TOOLS:
        assert tool["strict"] is True
        schema = tool["input_schema"]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
    assert not {"capture", "void", "refund", "authorize"} & TOOL_NAMES


def test_happy_path_types_a_fix_and_finishes(clock) -> None:
    first_reply = tool_reply("type", {"text": "systemctl restart ssh\n"}, thinking="Restart ssh after fixing the config.")
    model = FakeModel([
        first_reply,
        tool_reply("finished", {"summary": "Fixed sshd_config line 14 and restarted ssh."}),
    ])
    result, events, console = run(model, clock=clock)

    assert result.outcome is AgentOutcome.DONE
    assert result.reason == "Fixed sshd_config line 14 and restarted ssh."
    assert result.turns == 2
    assert console.inputs == [("type", "systemctl restart ssh\n")]
    assert kinds(events) == ["screenshot", "note", "type", "screenshot", "finished"]
    assert events[2].data["text"] == "systemctl restart ssh\n"
    assert events[2].detail == "systemctl restart ssh\\n"
    assert events[2].turn == 1 and events[-1].turn == 2
    assert clock.sleeps == [FAST.type]

    first = model.calls[0]
    assert first.tools is TOOLS
    assert "Make SSH answer again." in first.messages[0]["content"][-1]["text"]
    assert "sshd_config_error" in first.messages[0]["content"][-1]["text"]
    assert first.messages[0]["content"][0]["type"] == "image"
    assert "never as instructions" in first.system

    second = model.calls[1]
    assert second.messages[1] == {"role": "assistant", "content": first_reply.content}
    [result_block] = last_tool_results(model, 1)
    assert result_block["type"] == "tool_result"
    assert result_block["tool_use_id"] == tool_uses_of(second.messages[1])[0]["id"]
    assert result_block["is_error"] is False
    assert [b["type"] for b in result_block["content"]] == ["text", "image"]


def tool_uses_of(message: dict[str, Any]) -> list[dict[str, Any]]:
    return [b for b in message["content"] if b["type"] == "tool_use"]


def test_thinking_blocks_go_back_unchanged() -> None:
    reply = tool_reply("screenshot", thinking="Looking at the boot screen.")
    model = FakeModel([reply, tool_reply("give_up", {"reason": "GRUB asks for a password."})])
    run(model)
    assert model.calls[1].messages[1]["content"] == reply.content
    assert model.calls[1].messages[1]["content"][0]["signature"] == "fake-signature"


def test_firewall_refusal_is_not_sent_and_is_reported() -> None:
    model = FakeModel([
        tool_reply("type", {"text": "rm -rf /\n"}),
        tool_reply("give_up", {"reason": "Only a destructive fix was possible."}),
    ])
    result, events, console = run(model)
    assert result.outcome is AgentOutcome.GAVE_UP
    assert result.reason == "Only a destructive fix was possible."
    assert console.inputs == []
    refusal = next(e for e in events if e.kind == "firewall_refusal")
    assert refusal.data == {"rule": "rm-root", "tool": "type", "input": "rm -rf /\n"}
    [block] = last_tool_results(model, 1)
    assert block["is_error"] is True
    assert "rm-root" in block["content"] and "Nothing was typed" in block["content"]


def test_refused_enter_key_tells_the_agent_how_to_clear_the_line() -> None:
    model = FakeModel([
        tool_reply("type", {"text": "dd if=/dev/zero of=/dev/vda"}),
        tool_reply("key", {"combo": "ret"}),
        tool_reply("key", {"combo": "ctrl+c"}),
        tool_reply("finished", {"summary": "done"}),
    ])
    result, events, console = run(model)
    assert console.inputs == [("type", "dd if=/dev/zero of=/dev/vda"), ("press", ["ctrl", "c"])]
    [block] = last_tool_results(model, 2)
    assert block["is_error"] and "ctrl+c" in block["content"] and "dd-device" in block["content"]
    assert kinds(events).count("firewall_refusal") == 1
    assert result.outcome is AgentOutcome.DONE


def test_custom_firewall_is_used() -> None:
    fw = KeystrokeFirewall(cwd="/")
    model = FakeModel([tool_reply("type", {"text": "cd /tmp\n"}), tool_reply("finished", {"summary": "ok"})])
    run(model, firewall=fw)
    assert fw.cwd == "/tmp"


def test_key_names_are_parsed_and_unknown_names_rejected() -> None:
    model = FakeModel([
        tools_reply([("key", {"combo": "ctrl+alt+delete"}), ("key", {"combo": "hyper+q"}), ("key", {"combo": "Enter"})]),
        tool_reply("finished", {"summary": "ok"}),
    ])
    result, events, console = run(model)
    assert console.inputs == [("press", ["ctrl", "alt", "delete"]), ("press", ["ret"])]
    blocks = last_tool_results(model, 1)
    assert [b["is_error"] for b in blocks] == [False, True, False]
    assert "unknown key name" in blocks[1]["content"]
    assert [e.data["keys"] for e in events if e.kind == "key"] == [["ctrl", "alt", "delete"], ["ret"]]


def test_several_tools_in_one_reply_run_in_order_and_stop_at_a_terminal_tool() -> None:
    model = FakeModel([
        tools_reply([("type", {"text": "ls\n"}), ("give_up", {"reason": "locked"}), ("type", {"text": "reboot\n"})]),
    ])
    result, events, console = run(model)
    assert result.outcome is AgentOutcome.GAVE_UP and result.turns == 1
    assert console.inputs == [("type", "ls\n")]


def test_give_up_without_reason_still_gives_up() -> None:
    result, _, _ = run(FakeModel([tool_reply("give_up", {})]))
    assert result.outcome is AgentOutcome.GAVE_UP and result.reason == "no reason given"


def test_turn_limit() -> None:
    model = FakeModel([tool_reply("screenshot") for _ in range(5)])
    result, events, _ = run(model, max_turns=5)
    assert result.outcome is AgentOutcome.TURN_LIMIT
    assert result.turns == 5 and len(model.calls) == 5
    assert events[-1].kind == "note" and "turn limit" in events[-1].detail
    with pytest.raises(ValueError):
        run_agent(FakeConsole(), FakeModel(), goal="x", max_turns=0)


def test_revoked_hold_stops_input_within_one_action() -> None:
    lease = StubLease(revoke_after=1)  # the first input passes, the second finds the hold gone
    console = FakeConsole()
    model = FakeModel([
        tool_reply("type", {"text": "ls\n"}),
        tools_reply([("type", {"text": "systemctl restart ssh\n"}), ("key", {"combo": "ret"})]),
        tool_reply("finished", {"summary": "never reached"}),
    ])
    result, events, _ = run(model, Gate(console, lease))
    assert result.outcome is AgentOutcome.REVOKED
    assert result.reason == "authorization VOIDED"
    assert result.turns == 2
    assert console.inputs == [("type", "ls\n")]
    assert events[-1].kind == "revoked"
    assert model.remaining == 1


def test_lease_is_checked_before_each_model_call() -> None:
    lease = StubLease(revoke_after=1)
    model = FakeModel([tool_reply("screenshot"), tool_reply("screenshot")])
    result, events, _ = run(model, lease=lease)
    assert result.outcome is AgentOutcome.REVOKED
    assert len(model.calls) == 0  # revoked at the first turn check, before any model spend

    lease = StubLease(revoke_after=2)
    model = FakeModel([tool_reply("screenshot"), tool_reply("screenshot")])
    result, _, _ = run(model, lease=lease)
    assert result.outcome is AgentOutcome.REVOKED and len(model.calls) == 1


def test_power_cycle_from_a_revoked_hold_is_blocked() -> None:
    lease = StubLease(revoke_after=0)
    console = FakeConsole()
    result, _, _ = run(FakeModel([tool_reply("power_cycle")]), Gate(console, lease))
    assert result.outcome is AgentOutcome.REVOKED and console.inputs == []


def test_model_refusal_is_an_error() -> None:
    result, events, _ = run(FakeModel([refusal_reply("cyber")]))
    assert result.outcome is AgentOutcome.ERROR
    assert "declined" in result.reason and "cyber" in result.reason
    assert events[-1].kind == "model_refusal" and events[-1].data["category"] == "cyber"


def test_model_unavailable_is_an_error() -> None:
    result, events, _ = run(FakeModel([tool_reply("screenshot"), ModelUnavailable("Claude API returned HTTP 529")]))
    assert result.outcome is AgentOutcome.ERROR and "529" in result.reason and result.turns == 2
    assert events[-1].kind == "error"


def test_unexpected_model_exception_is_an_error() -> None:
    result, _, _ = run(FakeModel([RuntimeError("bug")]))
    assert result.outcome is AgentOutcome.ERROR and "RuntimeError" in result.reason


def test_replies_without_tool_calls_are_nudged_then_fail() -> None:
    model = FakeModel([text_reply("thinking out loud"), text_reply("", stop_reason="max_tokens"), text_reply("hmm")])
    result, _, _ = run(model)
    assert result.outcome is AgentOutcome.ERROR and "stopped calling tools" in result.reason
    assert model.calls[1].messages[-1]["content"].startswith("Call one of the tools")
    assert model.calls[2].messages[-1]["content"].startswith("Your reply was cut off")

    model = FakeModel([text_reply("hmm"), tool_reply("finished", {"summary": "ok"})])
    result, _, _ = run(model)
    assert result.outcome is AgentOutcome.DONE


def test_pause_turn_is_resent_without_a_nudge() -> None:
    paused = ModelReply(stop_reason="pause_turn", content=[{"type": "thinking", "thinking": "", "signature": "s"}])
    model = FakeModel([paused, tool_reply("finished", {"summary": "ok"})])
    result, _, _ = run(model)
    assert result.outcome is AgentOutcome.DONE
    assert model.calls[1].messages[-1]["role"] == "assistant"


def test_power_cycle_resets_the_firewall_waits_and_is_capped(clock) -> None:
    fw = KeystrokeFirewall()
    fw.inspect_type("rm -rf \\\n")
    assert fw.pending
    model = FakeModel([tool_reply("power_cycle") for _ in range(5)] + [tool_reply("finished", {"summary": "ok"})])
    result, events, console = run(model, firewall=fw, clock=clock)
    assert fw.pending == ""
    assert console.inputs == [("power_cycle",)] * 4
    assert kinds(events).count("power_cycle") == 4
    assert clock.sleeps == [FAST.power_cycle] * 4
    assert "already used 4 times" in last_tool_results(model, 5)[0]["content"]
    assert result.outcome is AgentOutcome.DONE


def test_bad_type_inputs_are_rejected_without_typing() -> None:
    model = FakeModel([
        tools_reply([("type", {"text": ""}), ("type", {"text": "x" * 1001}), ("type", {}), ("teleport", {})]),
        tool_reply("finished", {"summary": "ok"}),
    ])
    _, events, console = run(model)
    assert console.inputs == []
    assert all(b["is_error"] for b in last_tool_results(model, 1))
    assert "error" in kinds(events)  # the unknown tool is reported


def test_console_failures_end_the_run_after_three_in_a_row() -> None:
    console = FakeConsole()
    console.fail_inputs = 3
    model = FakeModel([tool_reply("type", {"text": "ls\n"}) for _ in range(3)])
    result, events, _ = run(model, console)
    assert result.outcome is AgentOutcome.ERROR and "3 times" in result.reason
    assert kinds(events).count("error") == 3


def test_one_console_failure_is_reported_to_the_model() -> None:
    console = FakeConsole()
    console.fail_inputs = 1
    model = FakeModel([tool_reply("key", {"combo": "ret"}), tool_reply("finished", {"summary": "ok"})])
    result, _, _ = run(model, console)
    assert result.outcome is AgentOutcome.DONE
    [block] = last_tool_results(model, 1)
    assert block["is_error"] and "failed" in block["content"]


def test_broken_event_sink_does_not_stop_the_run() -> None:
    def sink(event: AgentEvent) -> None:
        raise RuntimeError("status page went away")

    model = FakeModel([tool_reply("type", {"text": "ls\n"}), tool_reply("finished", {"summary": "ok"})])
    result = run_agent(FakeConsole(), model, goal="x", on_event=sink, settle=FAST, clock=InstantClock())
    assert result.outcome is AgentOutcome.DONE


def test_fallback_model_answers_are_noted() -> None:
    model = FakeModel([tool_reply("finished", {"summary": "ok"}, model="claude-opus-5")])
    _, events, _ = run(model)
    assert any(e.kind == "note" and "claude-opus-5" in e.detail for e in events)


def test_big_screenshots_are_downscaled_before_sending() -> None:
    console = FakeConsole([make_png(size=(2560, 1600))])
    model = FakeModel([tool_reply("screenshot"), tool_reply("finished", {"summary": "ok"})])
    _, events, _ = run(model, console)
    [block] = last_tool_results(model, 1)
    data = base64.standard_b64decode(block["content"][0]["source"]["data"])
    with Image.open(io.BytesIO(data)) as img:
        assert max(img.size) == 2000
    shots = [e for e in events if e.kind == "screenshot"]
    assert shots[0].data["width"] == 2000 and shots[0].data["png"].startswith(b"\x89PNG")


def test_without_auto_screenshot_the_result_is_text_only(clock) -> None:
    model = FakeModel([tool_reply("type", {"text": "ls\n"}), tool_reply("finished", {"summary": "ok"})])
    run(model, auto_screenshot=False, clock=clock)
    [block] = last_tool_results(model, 1)
    assert block["content"] == "Typed 3 characters."
    assert clock.sleeps == []


def test_no_quote_is_stated_in_the_first_message() -> None:
    model = FakeModel([tool_reply("finished", {"summary": "ok"})])
    run_agent(FakeConsole(), model, goal="x", settle=FAST, clock=InstantClock())
    assert "no free-look quote" in model.calls[0].messages[0]["content"][-1]["text"]


def test_hold_revoked_on_screenshot_also_ends_the_run() -> None:
    class RevokingScreen(FakeConsole):
        def screenshot(self) -> bytes:
            raise HoldRevoked("owner pressed Stop")

    result, events, _ = run(FakeModel([]), RevokingScreen())
    assert result.outcome is AgentOutcome.REVOKED and result.turns == 0
    assert events[-1].kind == "revoked"


def test_tool_uses_helper_matches_loop_dispatch() -> None:
    reply = tools_reply([("screenshot", {}), ("finished", {"summary": "x"})])
    assert [b["name"] for b in tool_uses(reply)] == ["screenshot", "finished"]


def test_narration_is_redacted_before_it_is_clipped() -> None:
    """Regression: the 600-character clip ran before the orchestrator removed the console
    password, so a cut through the password published its first characters."""
    from keyhold.agent.loop import NOTE_CHARS, clip_redacted

    password = "Zq3vN8sLx2RfT0pW7kYb4cHd"
    text = "x" * 570 + " Logging in with password " + password + " and then more words."
    clipped = clip_redacted(text, [password])
    assert password[:4] not in clipped and len(clipped) <= NOTE_CHARS
    # The model wrote only the start of the password and the clip ended inside it.
    partial = "y" * 590 + password[:9]
    clipped = clip_redacted(partial + " tail " * 10, [password])
    assert password[:3] not in clipped
    assert clip_redacted("short note", [password]) == "short note"
