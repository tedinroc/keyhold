"""The story of one run in plain words: what the pages show first.

Every screen tells one sentence of the same five-step money story (Quote, Hold, Keyboard, Probe,
then Charged or Voided). This module turns the ledger row, its events and PayPal's live read
into plain dicts that Jinja renders and that the status page re-renders from SSE with
``textContent`` only. Nothing here moves money, and every technical fact stays reachable on the
page in the "Audit details" fold (``web.views`` builds those).

- ``progress_view``: the progress strip, the one status sentence, the lock plate and the money box.
- ``paypal_table``: PayPal's own status words with a "What it means" column, the amounts, and two
  ticks per money call: PayPal's API answer and the verified webhook.
- ``KeyMoments``: the filtered timeline beside the console (lock, PayPal, firewall, probe,
  warranty, Stop, and the last few agent actions).
- ``tale_view``: "How your $9.00 moved" on the end page.

Times are UTC.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from markupsafe import Markup, escape

from keyhold.types import NO_MONEY_CAPTURE, RescueState

__all__ = [
    "KeyMoments",
    "code_paths",
    "evidence_markup",
    "visible_moments",
    "STATE_WORDS",
    "duration",
    "hms",
    "paypal_table",
    "progress_view",
    "short_id",
    "state_words",
    "tale_view",
]

STABLE_PASSES = 2  # the owner probe must pass this many times, far enough apart (both policies)
LIVE = frozenset({RescueState.HELD, RescueState.ACTIVE, RescueState.PROBING})
PRE = frozenset({RescueState.QUOTED, RescueState.HOLD_PENDING})

# Plain words for each state code (status line, outcome pill, full-log STATE rows).
STATE_WORDS = {
    "QUOTED": "Quote ready",
    "HOLD_PENDING": "Waiting for your PayPal approval",
    "HELD": "Hold approved, lending the keyboard",
    "ACTIVE": "Agent at the keyboard",
    "PROBING": "Checking your server",
    "CAPTURED": "Fixed and charged",
    "WARRANTY_OK": "Warranty passed",
    "REFUNDED": "Refunded",
    "VOIDED": "Hold released, $0 charged",
}

_STEP_LABELS = {"quote": "Quote", "hold": "Hold", "keyboard": "Keyboard", "probe": "Probe"}
_STEP_ICONS = {"quote": "i-quote", "hold": "i-hold", "keyboard": "i-key", "probe": "i-probe"}
_UPCOMING = {
    "quote": "fixed before any key",
    "hold": "you approve in PayPal",
    "keyboard": "lent only under the hold",
    "probe": "your check, twice",
}
_OUTCOME_ICONS = {
    "pending": "i-receipt",
    "charged": "i-charged",
    "capture_pending": "i-hold",
    "refunded": "i-refund",
    "refunding": "i-refund",
    "voided": "i-voided",
    "nohold": "i-nohold",
    "expired": "i-nohold",
    "cancelled": "i-nohold",
    "declined": "i-nohold",
    "failed": "i-nohold",
}


# --------------------------------------------------------------------------------------------
# Small formatters
# --------------------------------------------------------------------------------------------


def hms(epoch: float | None) -> str:
    if epoch is None:
        return ""
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%H:%M:%S")


def _iso_hms(iso: str | None) -> str:
    return str(iso)[11:19] if iso else ""


def _day(iso: str | None) -> str:
    """'2026-12-07T11:53:45Z' -> '7 Dec'."""
    if not iso:
        return ""
    try:
        when = datetime.strptime(str(iso)[:10], "%Y-%m-%d")
    except ValueError:
        return ""
    return f"{when.day} {when.strftime('%b')}"


def _amount(value: Decimal | str | None) -> str | None:
    return None if value is None else f"{Decimal(str(value)):.2f}"


def _whole(value: Decimal | str | None) -> str:
    """'$9' for 9.00, '$9.50' otherwise."""
    if value is None:
        return "$0"
    amount = Decimal(str(value))
    return f"${amount:.0f}" if amount == amount.to_integral_value() else f"${amount:.2f}"


def duration(seconds: float | None) -> str:
    """'45 s', '1 min 45 s', '2 min', '1 h 5 min'."""
    if seconds is None:
        return ""
    total = max(0, int(round(float(seconds))))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    if minutes:
        return f"{minutes} min {secs} s" if secs else f"{minutes} min"
    return f"{secs} s"


def short_id(value: str | None) -> str:
    """First six and last four characters: '9DX392…063W'. The full id goes in a title."""
    if not value:
        return ""
    text = str(value)
    return text if len(text) <= 12 else f"{text[:6]}…{text[-4:]}"


def state_words(code: str) -> str:
    return STATE_WORDS.get(code, code)


# File names and paths in a diagnosis ("Typo in sshd_config", "fstab lists a disk..."): a mono chip.
_PATHLIKE = re.compile(r"(?<![\w/])(/[\w.\-]+(?:/[\w.\-]+)*|[a-z][\w.\-]*_[\w.\-]+|fstab)(?![\w/])")


def code_paths(text: str | None) -> Markup:
    """The text, escaped, with file names and paths wrapped in <code>. Only characters from
    [\\w./-] are ever wrapped, so nothing the text says can become markup."""
    escaped = str(escape(text or ""))
    return Markup(_PATHLIKE.sub(lambda m: f"<code>{m.group(1)}</code>", escaped))


def evidence_markup(line: str | None) -> Markup:
    """One evidence line from the screen, escaped, with "[FAILED]" marked as a failure (red)."""
    return Markup(str(escape(line or "")).replace("[FAILED]", '<span class="ev-fail">[FAILED]</span>'))


def _sentence(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else text + "."


# --------------------------------------------------------------------------------------------
# Facts read from the event log
# --------------------------------------------------------------------------------------------


def stop_note(f: "Facts") -> str:
    """One sentence on what the keyboard did around Stop, from the ledger."""
    if f.cut_input and not f.typed_after_stop:
        if f.cut_input.get("enter_withheld"):
            return "Stop cut the line being typed: its Enter key was never sent, so it did not run."
        return "Stop cut the text being typed; the rest of its keys were never sent."
    if f.typed_after_stop:
        return "One input that was already being sent when Stop arrived finished after it; nothing was sent after that."
    return "Nothing was typed after Stop."


@dataclass
class Facts:
    """What the event log says happened, counted once for every view below."""

    quoted_at: float | None = None
    state_at: dict[str, float] = field(default_factory=dict)  # first time each state was reached
    gate_checks: int = 0
    gate_first_at: float | None = None
    gate_last_at: float | None = None
    gate_last_status: str | None = None
    gate_last_granted: bool = True
    revoked_at: float | None = None
    revoked_reason: str = ""
    stop_at: float | None = None
    typed_after_stop: bool = False  # an input finished after the keyboard was taken back
    cut_input: dict[str, Any] | None = None  # the input Stop cut short (keys sent, Enter withheld)

    @property
    def stop_pressed_at(self) -> float | None:
        """When Stop took the keyboard: the revoke it caused, or the stop event when the keyboard had
        already been locked for another reason (the agent finished, then Stop came while probing)."""
        if self.revoked_at is not None and "pressed Stop" in self.revoked_reason:
            return self.revoked_at
        return self.stop_at
    agent_turns: int | None = None
    agent_outcome: str | None = None
    agent_reason: str = ""
    refusals: list[str] = field(default_factory=list)
    probe_runs: int = 0
    probe_passes: list[float] = field(default_factory=list)  # passes since the last failure
    probe_last_ok: bool | None = None
    probe_last_detail: str = ""
    stable_passes: list[float] = field(default_factory=list)
    stable_at: float | None = None
    warranty: dict[str, Any] | None = None  # the plan stored when the warranty started
    warranty_runs: int = 0
    warranty_passes: int = 0
    warranty_last_ok: bool | None = None
    warranty_last_at: float | None = None
    refund_due: str | None = None
    void_at: float | None = None
    capture_at: float | None = None
    refund_at: float | None = None

    @classmethod
    def read(cls, events: Iterable[Any]) -> Facts:
        f = cls()
        for ev in events:
            kind, data, at = ev.kind, ev.data or {}, ev.at
            if kind == "quoted" and f.quoted_at is None:
                f.quoted_at = at
            elif kind == "state":
                to = str(data.get("to") or "")
                f.state_at.setdefault(to, at)
            elif kind == "gate_check":
                f.gate_checks += 1
                f.gate_first_at = f.gate_first_at if f.gate_first_at is not None else at
                f.gate_last_at = at
                f.gate_last_status = data.get("status")
                f.gate_last_granted = bool(data.get("granted", True))
            elif kind == "gate_revoked" and f.revoked_at is None:
                f.revoked_at = at
                f.revoked_reason = ev.detail
            elif kind == "stop" and f.stop_at is None:
                f.stop_at = at
            elif kind in ("agent_type", "agent_key"):
                # measured from the moment the lease was revoked (Stop pressed), not from the
                # later "stop" event, which is written once an input in flight has ended
                taken = f.stop_pressed_at
                if taken is not None and at > taken:
                    f.typed_after_stop = True
            elif kind == "agent_type_cut" and f.cut_input is None:
                if data.get("sent") != 0:  # a cut before the first key typed nothing at all
                    f.cut_input = {"sent": data.get("sent"), "total": data.get("total"),
                                   "enter_withheld": bool(data.get("enter_withheld"))}
            elif kind == "agent_result":
                f.agent_turns = data.get("turns")
                f.agent_outcome = data.get("outcome")
                f.agent_reason = ev.detail.split(": ", 1)[-1] if ": " in ev.detail else ev.detail
            elif kind == "agent_firewall_refusal":
                f.refusals.append(str(data.get("input", "")).strip())
            elif kind == "probe":
                f.probe_runs += 1
                ok = bool(data.get("ok"))
                f.probe_last_ok = ok
                f.probe_last_detail = ev.detail
                if ok:
                    f.probe_passes.append(float(data.get("at", at)))
                else:
                    f.probe_passes = []
            elif kind == "probe_stable":
                f.stable_at = at
                f.stable_passes = [float(x) for x in data.get("passes", [])]
            elif kind == "warranty_started" and f.warranty is None:
                f.warranty = dict(data)
            elif kind == "warranty_check":
                f.warranty_runs += 1
                f.warranty_last_ok = bool(data.get("ok"))
                f.warranty_last_at = at
                if data.get("ok"):
                    f.warranty_passes += 1
            elif kind == "refund_due" and f.refund_due is None:
                f.refund_due = ev.detail
            elif kind == "paypal_ok":
                if ev.detail == "authorization voided":
                    f.void_at = f.void_at or at
                elif ev.detail.startswith("capture "):
                    f.capture_at = f.capture_at or at
                elif ev.detail.startswith("refund "):
                    f.refund_at = f.refund_at or at
        return f

    def warranty_total(self, plan: Any = None) -> int | None:
        if self.warranty and self.warranty.get("checks"):
            return int(self.warranty["checks"])
        return int(plan.checks) if plan is not None else None

    def probe_count(self) -> int:
        if self.stable_at is not None:
            return STABLE_PASSES
        return min(len(self.probe_passes), STABLE_PASSES - 1)


def _who(owner: bool = True) -> dict[str, str]:
    """Words for the person who started the run: "you" on their own pages, "the owner" for anyone
    else (rescue pages are public: the ledger links to them)."""
    if owner:
        return {"you": "you", "You": "You", "your": "your", "Your": "Your"}
    return {"you": "the owner", "You": "The owner", "your": "the owner's", "Your": "The owner's"}


def _plural(n: int, word: str) -> str:
    """'1 check', '2 checks'."""
    return f"{n} {word}{'' if n == 1 else 's'}"


def _lock_reason(detail: str, owner: bool = True) -> str:
    """'the agent session ended (done)' -> 'agent finished'."""
    text = (detail or "").strip()
    if text.startswith(("the agent session ended", "the rescue session ended")):
        outcome = text.rsplit("(", 1)[-1].rstrip(")") if "(" in text else ""
        return {
            "done": "agent finished",
            "gave_up": "agent gave up",
            "turn_limit": "turn limit reached",
            "revoked": "PayPal check failed",
            "error": "agent stopped on an error",
            "timeout": "time limit reached",
        }.get(outcome, "agent session ended")
    if text == "the owner pressed Stop":
        return f"{_who(owner)['you']} pressed Stop"
    return text or "rescue over"


def _void_reason(end_reason: str | None, owner: bool = True) -> str:
    """The reason a hold was released, as a short phrase for notes."""
    text = (end_reason or "").strip()
    lower = text.lower()
    if "pressed stop" in lower:
        return f"{_who(owner)['you']} pressed Stop"
    if "gave up" in lower or "give_up" in lower or "give up" in lower:
        return "agent gave up"
    if "never passed" in lower or "probe" in lower:
        return f"{_who(owner)['your']} check never passed twice"
    if "turn limit" in lower or "time limit" in lower or "timed out" in lower:
        return "time limit reached"
    if "budget" in lower:
        return "model budget reached"
    return text or "hold released"


# --------------------------------------------------------------------------------------------
# The progress strip, status sentence, lock plate and money box
# --------------------------------------------------------------------------------------------


def _step(key: str, state: str = "upcoming", note: str | None = None, tone: str = "") -> dict[str, str]:
    return {
        "key": key,
        "label": _STEP_LABELS[key],
        "icon": _STEP_ICONS[key],
        "state": state,
        "tone": tone,
        "note": _UPCOMING[key] if note is None else note,
    }


def _outcome(state: str, label: str, note: str) -> dict[str, str]:
    return {"state": state, "label": label, "note": note, "icon": _OUTCOME_ICONS.get(state, "i-receipt")}


def _status(head: str, tail: str = "", *, sub: str = "", xl: bool = False) -> dict[str, Any]:
    return {"head": head, "tail": tail, "sub": sub, "xl": xl, "text": f"{head} · {tail}" if tail else head}


def status_title(status: Mapping[str, Any] | None) -> str:
    """The status sentence for the page title (static/common.js does the same when it changes):
    "Fixed. Charged $9.00." -> "Fixed, charged $9.00"; a long head keeps its first sentence
    ("Quote ready"). A screen reader hears the new state when a page reloads itself."""
    head = str((status or {}).get("head") or "").strip().rstrip(".")
    if ". " in head:
        first, rest = head.split(". ", 1)
        head = f"{first}, {rest[:1].lower()}{rest[1:]}" if len(head) <= 42 else first
    return head


def _line(steps: list[dict[str, str]], outcome: dict[str, str], *, final: bool = True) -> dict[str, str]:
    """The phone version of the strip: 'Step 3 of 4 · Keyboard: agent typing'. "Done" only once
    nothing can change: a charge whose warranty still runs reads 'Charged · warranty 2 of 6'."""
    for n, s in enumerate(steps, start=1):
        if s["state"] == "current":
            return {"lead": f"Step {n} of {len(steps)}", "text": f"{s['label']}: {s['note']}"}
    if outcome["state"] == "pending":
        return {"lead": "Waiting", "text": outcome["note"]}
    if outcome["state"] in ("nohold",):
        return {"lead": "Stopped at the quote", "text": "no hold offered"}
    if not final:
        lead = outcome["label"].split(" $")[0].split(" ·")[0]
        return {"lead": lead, "text": outcome["note"] or "still running"}
    return {"lead": "Done", "text": f"{outcome['label']}, {outcome['note']}" if outcome["note"] else outcome["label"]}


def _lock(state: str, title: str, evidence: str = "") -> dict[str, str]:
    return {"state": state, "title": title, "evidence": evidence}


def _money(tone: str, label: str, amount: str | None, unit: str, sentence: str = "", *, warning: str | None = None,
           stats: list[dict[str, str]] | None = None, icon: str | None = None) -> dict[str, Any]:
    """``icon``: by default from the tone; a released hold passes ``i-voided`` (grey like "no
    hold", but it was a hold)."""
    return {
        "tone": tone,
        "label": label,
        "amount": amount or "0.00",
        "unit": unit,
        "sentence": sentence,
        "warning": warning,
        "stats": stats or [],
        "icon": icon or {"held": "i-hold", "charged": "i-charged", "refunded": "i-refund"}.get(tone, "i-nohold"),
    }


def _pack(phase: str, state: str, steps: list[dict[str, str]], outcome: dict[str, str], status: dict[str, Any],
          lock: dict[str, str], money: dict[str, Any] | None, warranty: dict[str, Any] | None = None,
          *, final: bool = True) -> dict[str, Any]:
    return {
        "phase": phase,
        "state": state,
        "steps": steps,
        "outcome": outcome,
        "status": status,
        "line": _line(steps, outcome, final=final),
        "lock": lock,
        "money": money,
        "warranty": warranty,
    }


def run_progress(stage: str, *, no_quote_reason: str | None = None, honest_stop: bool = False,
                 cancelling: bool = False, look_failed: bool = False) -> dict[str, Any]:
    """Progress for a run that has no rescue row (yet, or ever): boot, free look, no quote, a
    failed start, or a Stop before the free look ended. ``cancelling``: the owner pressed Cancel
    during the boot or the free look, which still hold the server until they end."""
    if stage in ("booting", "looking") and cancelling:
        steps = [_step("quote", "skipped", "cancelling"), _step("hold", "skipped", "not offered"),
                 _step("keyboard", "skipped", "never lent"), _step("probe", "skipped", "not needed")]
        return _pack(
            "pre", stage, steps, _outcome("cancelled", "Cancelling · $0", "nothing held"),
            _status("Cancelling.", sub="The server is released when its boot ends. Nothing is held and nothing "
                                       "is charged."),
            _lock("locked", "Keyboard locked: cancelling", "the keyboard was never lent"),
            None,
        )
    if stage in ("booting", "looking"):
        note = "starting the server" if stage == "booting" else "free look, screenshots only"
        steps = [_step("quote", "current", note), _step("hold"), _step("keyboard"), _step("probe")]
        head = "Starting a fresh copy of the broken server." if stage == "booting" else "Looking at the screen"
        return _pack(
            "pre", stage, steps, _outcome("pending", "Charged / Voided", "your check decides"),
            _status(head, "keyboard locked" if stage == "looking" else ""),
            _lock("locked", "Keyboard locked: screenshots only", "the free look cannot press a key"),
            None,
        )
    if stage == "no_quote":
        steps = [
            _step("quote", "done", "no hold"),
            _step("hold", "skipped", "not offered"),
            _step("keyboard", "skipped", "never lent"),
            _step("probe", "skipped", "not needed"),
        ]
        sub = _sentence(no_quote_reason or "")
        if honest_stop:
            sub = ("The fix needs a password the console does not give, so Keyhold takes no hold it cannot earn. "
                   "$0, no PayPal call.")
        elif sub:
            sub += " $0, no PayPal call."
        return _pack(
            "decided", stage, steps, _outcome("nohold", "No hold · $0", "nothing held, nothing charged"),
            _status("No hold offered.", sub=sub or "Nothing was held and nothing was charged.", xl=True),
            _lock("locked", "Keyboard locked: no hold offered", "screenshots only"),
            _money("voided", "What this cost you", "0.00", "no hold, no charge"),
        )
    if stage == "failed" and look_failed:
        steps = [
            _step("quote", "failed", "free look failed"),
            _step("hold", "skipped", "not offered"),
            _step("keyboard", "skipped", "never lent"),
            _step("probe", "skipped", "not needed"),
        ]
        return _pack(
            "decided", stage, steps, _outcome("failed", "No quote · $0", "nothing held"),
            _status("The free look did not finish.", xl=True,
                    sub="The server started, but the look at its screen failed, so there is no quote. Nothing was "
                        "held and nothing was charged."),
            _lock("locked", "Keyboard locked: the free look stopped", "no key was pressed"),
            _money("voided", "What this cost you", "0.00", "nothing held"),
        )
    if stage == "failed":
        steps = [
            _step("quote", "failed", "could not start"),
            _step("hold", "skipped", "not offered"),
            _step("keyboard", "skipped", "never lent"),
            _step("probe", "skipped", "not needed"),
        ]
        return _pack(
            "decided", stage, steps, _outcome("failed", "Could not start · $0", "nothing held"),
            _status("The run could not start.", sub="Nothing was held and nothing was charged.", xl=True),
            _lock("locked", "Keyboard locked: the run could not start", "nothing was held"),
            _money("voided", "What this cost you", "0.00", "nothing held"),
        )
    # expired or finished without a quote: a Stop before the free look ended
    steps = [
        _step("quote", "skipped", "cancelled"),
        _step("hold", "skipped", "not offered"),
        _step("keyboard", "skipped", "never lent"),
        _step("probe", "skipped", "not needed"),
    ]
    return _pack(
        "decided", stage, steps, _outcome("cancelled", "Cancelled · $0", "nothing held"),
        _status("Cancelled. Nothing was held.", sub="The run stopped before a quote. $0, no PayPal call.", xl=True),
        _lock("locked", "Keyboard locked: nothing was held", "the keyboard was never lent"),
        _money("voided", "What this cost you", "0.00", "nothing held"),
    )


def progress_view(
    rec: Any,
    events: Iterable[Any],
    *,
    live: Mapping[str, Any] | None = None,
    plan: Any = None,
    agent: str = "The agent",
    owner: bool = True,
) -> dict[str, Any]:
    """The strip, status, lock and money box for a rescue (``rec`` is a ``RescueRecord``).

    ``plan`` is the warranty plan in force (used until the rescue's own warranty starts and
    stores its plan); ``agent`` names who typed ("Claude" or "The recorded agent"); ``owner``
    says whether the viewer started the run ("you") or not ("the owner")."""
    f = Facts.read(events)
    w = _who(owner)
    state = rec.state
    code = state.value
    quote = _amount(rec.quote_amount) or "0.00"
    held = _amount(rec.authorized_amount) or quote
    auth_live = (live or {}).get("authorization") or {}
    paypal_says = auth_live.get("status") or f.gate_last_status or rec.auth_status or "CREATED"
    checks = _plural(f.gate_checks, "PayPal check")
    quote_step = _step("quote", "done", f"${quote} fixed")
    pending = _outcome("pending", "Charged / Voided", f"{w['your']} check decides")

    warning = None
    if auth_live and state in LIVE and auth_live.get("status") not in (None, "CREATED"):
        warning = (f"PayPal now shows the hold as {auth_live['status']}. The keyboard is locked at the next action "
                   "and the hold is released.")

    # -- before the hold --------------------------------------------------------------------
    if state is RescueState.QUOTED:
        steps = [quote_step, _step("hold", "current", f"waiting for {w['you']}"), _step("keyboard"), _step("probe")]
        return _pack(
            "pre", code, steps, pending, _status("Quote ready. Approve the hold to lend the keyboard."),
            _lock("locked", "Keyboard locked: no hold yet", "screenshots only so far"),
            _money("neutral", "Quote", quote, "fixed price, nothing held yet",
                   "Nothing is held until the hold is approved in PayPal."),
        )
    if state is RescueState.HOLD_PENDING:
        steps = [quote_step, _step("hold", "current", "waiting for PayPal"), _step("keyboard"), _step("probe")]
        return _pack(
            "pre", code, steps, pending, _status(f"Waiting for {w['your']} PayPal approval."),
            _lock("locked", "Keyboard locked: no hold yet", "the keyboard is lent only once PayPal says CREATED"),
            _money("neutral", "Awaiting approval", quote, "not held yet",
                   "A PayPal order exists. Nothing is held until it is approved."),
        )

    hold_done = _step("hold", "done", f"{_whole(held)} held", tone="held")
    lent_evidence = f"PayPal says {paypal_says} · last checked {hms(f.gate_last_at)} UTC · {checks}"

    # -- the keyboard is lent -----------------------------------------------------------------
    if state in (RescueState.HELD, RescueState.ACTIVE):
        typing = state is RescueState.ACTIVE
        steps = [quote_step, hold_done, _step("keyboard", "current", "agent typing" if typing else "lending"),
                 _step("probe")]
        if warning:
            lock = _lock("lent", f"Keyboard lent, locking: PayPal now says {auth_live['status']}",
                         "the next action is refused")
        elif f.gate_checks:
            lock = _lock("lent", "Keyboard lent: hold live", lent_evidence)
        else:
            lock = _lock("lent", "Keyboard lent: hold live", f"PayPal says {paypal_says}")
        status = (_status("Agent at the keyboard", "hold live") if typing
                  else _status("Hold approved.", "lending the keyboard"))
        sentence = ("The agent is at the keyboard under the hold." if typing
                    else "The keyboard is lent only while PayPal says CREATED.")
        return _pack("live", code, steps, pending, status, lock,
                     _money("held", "Held", held, "on hold, not charged", sentence, warning=warning))
    if state is RescueState.PROBING:
        k = f.probe_count()
        if f.probe_last_ok is False:
            note = "last check failed, trying again"
        elif k == 0:
            note = "first check running"
        else:
            note = f"pass {k} of {STABLE_PASSES}"
        steps = [quote_step, hold_done, _step("keyboard", "done", "locked: agent finished"),
                 _step("probe", "current", note)]
        return _pack(
            "live", code, steps, pending,
            _status("Agent finished, keyboard locked", f"checking {w['your']} server, {note}"),
            _lock("locked", "Keyboard locked: agent finished",
                  f"at {hms(f.revoked_at)} UTC · {checks} while lent" if f.revoked_at else "the agent finished"),
            _money("held", "Held", held, "on hold, not charged",
                   f"{w['Your']} check decides: a charge only if the server answers twice, far enough apart.",
                   warning=warning),
        )

    # -- the money decision is made ----------------------------------------------------------
    keyboard_done = _step("keyboard", "done",
                          f"locked: {_lock_reason(f.revoked_reason, owner)}" if f.revoked_at else "locked")
    held_at = f.state_at.get("HELD")
    approved = _step("hold", "done", f"approved {hms(held_at)}" if held_at else "approved")
    done_lock = _lock(
        "locked", "Keyboard locked: rescue done",
        f"agent finished at {hms(f.revoked_at)} UTC · {checks}" if f.revoked_at else "agent finished",
    )
    passes = f.stable_passes or f.probe_passes[-STABLE_PASSES:]
    pass_times = " and ".join(hms(p) for p in passes[:STABLE_PASSES])
    probe_done = _step("probe", "done", f"{STABLE_PASSES} of {STABLE_PASSES} passed")
    captured = _amount(rec.captured_amount) or held
    total = f.warranty_total(plan)
    warranty = {"done": f.warranty_runs, "passed": f.warranty_passes, "total": total,
                "label": _warranty_short(f.warranty, plan),
                "last_at": hms(f.warranty_last_at or f.capture_at or f.state_at.get("CAPTURED"))}
    hold_to_charge = None
    if "HELD" in f.state_at and "CAPTURED" in f.state_at:
        hold_to_charge = f.state_at["CAPTURED"] - f.state_at["HELD"]

    if rec.capture_status in NO_MONEY_CAPTURE:
        steps = [quote_step, _step("hold", "done", "released"), keyboard_done, probe_done]
        return _pack(
            "decided", code, steps, _outcome("declined", "Capture declined · $0", "nothing charged"),
            _status("PayPal declined the charge. $0.", xl=True,
                    sub=f"{w['Your']} check passed, but PayPal answered the capture with {rec.capture_status}. "
                        "Nothing was charged."),
            done_lock, _money("voided", "Capture declined", "0.00", "charged", "Nothing was charged.", icon="i-voided"),
            warranty,
        )
    if state is RescueState.CAPTURED and rec.capture_status == "PENDING":
        steps = [quote_step, hold_done, keyboard_done, probe_done]
        return _pack(
            "decided", code, steps, _outcome("capture_pending", "Capture pending", "money not moved yet"),
            _status(f"{w['Your']} check passed. The charge is pending.", xl=True,
                    sub="PayPal accepted the capture but shows it as PENDING: the money has not moved yet."),
            done_lock,
            _money("held", "Capture pending", captured, "accepted, not moved yet",
                   "The warranty keeps re-checking the server.", warning=warning),
            warranty, final=False,
        )

    answered = f"{w['your']} server answered SSH and the web check twice"
    charged_sub = (f"Charged only after {answered}, at {pass_times} UTC." if pass_times
                   else f"Charged only after {answered}.")
    charged_short = (f"Charged after {answered} ({pass_times.replace(' and ', ', ')} UTC)." if pass_times
                     else charged_sub)
    stats = []
    if hold_to_charge is not None:
        stats.append({"value": duration(hold_to_charge), "label": "from hold to charge"})
    if f.agent_turns:
        stats.append({"value": str(f.agent_turns), "label": "agent turn" if f.agent_turns == 1 else "agent turns"})

    if state is RescueState.CAPTURED and f.refund_due:
        steps = [quote_step, approved, keyboard_done, probe_done]
        return _pack(
            "decided", code, steps, _outcome("refunding", "Refund pending", "server fell over in warranty"),
            _status(f"Refunding {_whole(captured)}.", xl=True,
                    sub=f"The server stopped answering during the warranty ({f.refund_due}). The full refund is "
                        "sent as soon as PayPal accepts it."),
            done_lock, _money("refunded", "Refund pending", captured, "refund on its way"), warranty, final=False,
        )
    if state is RescueState.CAPTURED:
        n = f.warranty_runs
        progress_note = f"warranty {n} of {total}" if total else "warranty running"
        leave = "; you can leave" if owner else ""
        if n == 0:
            wsub = f"The warranty checks start now{leave}."
        else:
            wsub = f"Warranty check {n} of {total} {'passed' if f.warranty_last_ok else 'failed'}{leave}."
        steps = [quote_step, approved, keyboard_done, probe_done]
        stats.append({"value": f"{f.warranty_passes} of {total}" if total else str(f.warranty_passes),
                      "label": "warranty checks passed"})
        # Not final yet: two failed warranty checks in a row still refund it.
        return _pack(
            "decided", code, steps, _outcome("charged", f"Charged ${captured}", progress_note),
            _status(f"Fixed. Charged ${captured}.", xl=True, sub=f"{charged_short} {wsub}"),
            done_lock, _money("charged", "Charged", captured, "captured · warranty running", stats=stats,
                              warning=warning),
            warranty, final=False,
        )
    if state is RescueState.WARRANTY_OK:
        steps = [quote_step, approved, keyboard_done, probe_done]
        if total:
            stats.append({"value": f"{f.warranty_passes} of {total}", "label": "warranty checks passed"})
        return _pack(
            "decided", code, steps, _outcome("charged", f"Charged ${captured}", "warranty passed"),
            _status(f"Fixed. Charged ${captured}.", xl=True, sub=charged_sub),
            done_lock, _money("charged", "Charged", captured, "captured, final", stats=stats), warranty,
        )
    if state is RescueState.REFUNDED:
        refunded = _amount(rec.refunded_amount) or captured
        steps = [quote_step, approved, keyboard_done, probe_done]
        return _pack(
            "decided", code, steps, _outcome("refunded", f"Refunded ${refunded}", "server fell over in warranty"),
            _status(f"Refunded ${refunded}.", xl=True,
                    sub=f"Charged ${captured} after {w['your']} check passed twice, then refunded in full because the "
                        "server stopped answering during the warranty."),
            done_lock, _money("refunded", "Refunded", refunded, "refunded in full"), warranty,
        )

    # -- VOIDED -------------------------------------------------------------------------------
    reason = (rec.end_reason or "").strip()
    never = [_step("keyboard", "skipped", "never lent"), _step("probe", "skipped", "not needed")]
    nothing_lock = _lock("locked", "Keyboard locked: nothing was held", "the keyboard was never lent")
    zero = _money("voided", "What this cost you", "0.00", "nothing held")
    if rec.authorization_id is None:
        if "not paid within" in reason:
            steps = [quote_step, _step("hold", "skipped", "not approved"), *never]
            return _pack(
                "decided", code, steps, _outcome("expired", "Expired · $0", "not approved in time"),
                _status("Quote expired. Nothing was held.", xl=True,
                        sub=f"{_sentence(reason)} The server was released and nothing was charged."),
                nothing_lock, zero,
            )
        if "pressed stop" in reason.lower():
            steps = [quote_step, _step("hold", "skipped", "cancelled"), *never]
            return _pack(
                "decided", code, steps, _outcome("cancelled", "Cancelled · $0", "nothing held"),
                _status("Cancelled before the hold. $0.", xl=True,
                        sub="The run was cancelled before the hold was approved. Nothing was held and nothing was "
                            "charged."),
                nothing_lock, zero,
            )
        steps = [quote_step, _step("hold", "failed", "not approved"), *never]
        return _pack(
            "decided", code, steps, _outcome("declined", "Not held · $0", "nothing held"),
            _status("No hold was made. $0.", xl=True,
                    sub=f"{_sentence(reason) or 'PayPal did not confirm the hold.'} Nothing was charged."),
            nothing_lock, zero,
        )

    released = _step("hold", "done", "released")
    lent_for = None
    if f.gate_first_at is not None and f.revoked_at is not None:
        lent_for = f.revoked_at - f.gate_first_at
    vstats = []
    if lent_for is not None:
        vstats.append({"value": duration(lent_for), "label": "keyboard lent"})
    if f.gate_checks:
        vstats.append({"value": str(f.gate_checks), "label": "PayPal check" if f.gate_checks == 1 else "PayPal checks"})
    if f.stop_at is not None:
        pressed = f"{w['you']} pressed Stop"
        steps = [quote_step, released, _step("keyboard", "done", pressed), _step("probe", "skipped", "after Stop")]
        sub = f"Keyboard locked at {hms(f.revoked_at or f.stop_at)} UTC, hold voided."
        sub += " " + stop_note(f)
        if not f.typed_after_stop and not f.cut_input:
            vstats.append({"value": "0", "label": "keys after Stop"})
        elif not f.typed_after_stop and f.cut_input and f.cut_input.get("enter_withheld"):
            vstats.append({"value": "0", "label": "commands run after Stop"})
        return _pack(
            "decided", code, steps, _outcome("voided", "Voided · $0", pressed),
            _status("Stopped. $0 charged.", xl=True, sub=sub),
            _lock("locked", f"Keyboard locked: {pressed}",
                  f"at {hms(f.revoked_at or f.stop_at)} UTC, before the void was sent"),
            _money("voided", "Voided", "0.00", "charged", "PayPal released the hold.", stats=vstats, icon="i-voided"),
        )
    why = _void_reason(reason, owner)
    if "PROBING" in f.state_at:
        steps = [quote_step, released, _step("keyboard", "done", "locked: agent finished"),
                 _step("probe", "failed", "never passed twice")]
    elif f.gate_first_at is not None:
        steps = [quote_step, released, _step("keyboard", "done", f"locked: {why}"),
                 _step("probe", "skipped", "not run")]
    else:
        steps = [quote_step, released, _step("keyboard", "skipped", "never lent"), _step("probe", "skipped", "not run")]
    return _pack(
        "decided", code, steps, _outcome("voided", "Voided · $0", why),
        _status("Voided. $0 charged.", xl=True, sub=f"Nothing was charged: {reason or 'the hold was released'}."),
        _lock("locked", f"Keyboard locked: {_lock_reason(f.revoked_reason, owner) if f.revoked_at else why}",
              f"at {hms(f.revoked_at)} UTC" if f.revoked_at else "the hold was released"),
        _money("voided", "Voided", "0.00", "charged", "PayPal released the hold.", stats=vstats, icon="i-voided"),
    )


def _warranty_short(started: Mapping[str, Any] | None, plan: Any) -> str:
    if started:
        from keyhold.probe.owner import WarrantyPlan

        try:
            return WarrantyPlan(
                interval_s=float(started["interval_s"]),
                checks=int(started["checks"]),
                failures_to_refund=int(started.get("failures_to_refund", 2)),
                judge_mode=bool(started.get("judge_mode", False)),
            ).short_label
        except (KeyError, TypeError, ValueError):
            pass
    return plan.short_label if plan is not None else ""


# --------------------------------------------------------------------------------------------
# PayPal, read live: one row per object, PayPal's word, what it means, two ticks
# --------------------------------------------------------------------------------------------

_PILL = {
    ("authorization", "CREATED"): "held",
    ("authorization", "PENDING"): "held",
    ("authorization", "CAPTURED"): "charged",
    ("authorization", "PARTIALLY_CAPTURED"): "charged",
    ("capture", "COMPLETED"): "charged",
    ("capture", "PENDING"): "held",
    ("capture", "REFUNDED"): "refunded",
    ("capture", "PARTIALLY_REFUNDED"): "refunded",
    ("refund", "COMPLETED"): "refunded",
    ("refund", "PENDING"): "refunded",
}


def _meaning(key: str, status: str | None, rec: Any, obj: Mapping[str, Any] | None, owner: bool = True) -> str:
    w = _who(owner)
    amount = _whole((obj or {}).get("amount") or rec.quote_amount)
    final = rec.state.terminal
    if key == "order":
        if status in ("COMPLETED", "APPROVED") or rec.authorization_id:
            return f"{w['You']} approved the hold in PayPal."
        if status == "VOIDED":
            return "Closed without a payment."
        if final:
            return "Never approved; Keyhold released it. Nothing was held."
        return f"Waiting for {w['your']} approval in PayPal."
    if key == "authorization":
        expires = _day((obj or {}).get("expiration_time") or rec.auth_expiration_time)
        return {
            "CREATED": f"{amount} on hold, not charged." + (f" Expires {expires}." if expires else ""),
            "PENDING": "PayPal is still confirming the hold. The keyboard stays locked.",
            "CAPTURED": "The hold became the charge.",
            "PARTIALLY_CAPTURED": "Part of the hold became the charge.",
            "VOIDED": "Hold released. Nothing charged.",
            "EXPIRED": "The hold expired. Nothing charged.",
            "DENIED": "PayPal refused the hold.",
        }.get(status or "", "Never held." if final else "Not held yet.")
    if key == "capture":
        if status is None:
            if rec.state in LIVE or rec.state in PRE:
                return f"Not charged. Waits for {w['your']} check to pass twice."
            if final and rec.authorization_id:
                return "Never charged: a voided hold cannot be captured."
            if final:
                return "Never charged: nothing was held."
            return "Not charged."
        return {
            "COMPLETED": f"Charged {amount}, after {w['your']} check passed twice.",
            "PENDING": "Accepted, but the money has not moved yet.",
            "DECLINED": "PayPal declined the charge. $0.",
            "FAILED": "The charge failed. $0.",
            "REFUNDED": f"Charged {amount}, then refunded in full.",
            "PARTIALLY_REFUNDED": f"Charged {amount}, then partly refunded.",
        }.get(status, status.title())
    return {
        "COMPLETED": f"{amount} returned to {w['you']}.",
        "PENDING": "Refund sent, not settled yet.",
        "FAILED": "The refund failed; it is sent again.",
        "CANCELLED": "The refund was cancelled; it is sent again.",
    }.get(status or "", "Refund on its way.")


def _tick(at: str | None, label: str = "") -> dict[str, str]:
    return {"at": _iso_hms(at), "label": label}


def paypal_table(rec: Any, live: Mapping[str, Any] | None, ticks: Iterable[Mapping[str, Any]],
                 *, source: str, owner: bool = True, webhooks_verifiable: bool = True) -> dict[str, Any]:
    """Rows for the PayPal card. ``ticks`` is ``views.ticks_view`` (one entry per money call).

    A row whose object does not exist says so: "not yet" while it still can, "none" once the
    rescue is final (a voided hold is never captured). Without a webhook id the second tick can
    never come, so the Webhook column says that instead of "not yet"."""
    live = live or {}
    by_op: dict[str, Mapping[str, Any]] = {t["op"]: t for t in ticks}
    final = rec.state.terminal

    def api(*ops: str, own: str = "") -> tuple[list[dict[str, str]], list[str]]:
        """Ticks for ``ops``; a call that is not the row's own (``own``) carries its name, so the
        authorization's row reads "✓ 14:29:34" then "✓ 14:30:14 capture" (or "void")."""
        done, waiting = [], []
        for op in ops:
            t = by_op.get(op)
            if t is None:
                continue
            label = "" if op == own else op.lower()
            if t.get("api_at"):
                done.append(_tick(t["api_at"], label))
            else:
                state = str(t.get("status", "")).lower()
                waiting.append(f"{label + ' ' if label else ''}{state}{': ' + str(t['error']) if t.get('error') else ''}")
        return done, waiting

    def hooks(*ops: str) -> tuple[list[dict[str, str]], str]:
        got, expected = [], False
        for op in ops:
            t = by_op.get(op)
            if t is None or not t.get("webhook"):
                continue
            expected = True
            if t.get("webhook_at"):
                got.append(_tick(t["webhook_at"], "void" if op == "Void" else ""))
        if expected:
            empty = "not yet" if webhooks_verifiable else "cannot verify (no webhook id)"
        else:
            empty = "none for this call" if ops == ("Order",) else "—"
        return got, empty

    rows = []
    # (key, name, id, stored status, API calls shown, webhooks shown). The authorization's status
    # changes with the capture and the void, so its API column shows when those answered too.
    specs = [
        ("order", "Order", rec.order_id, rec.order_status, ("Order",), ("Order",)),
        ("authorization", "Authorization", rec.authorization_id, rec.auth_status,
         ("Authorization", "Capture", "Void"), ("Authorization", "Void")),
        ("capture", "Capture", rec.capture_id, rec.capture_status, ("Capture",), ("Capture",)),
    ]
    if rec.refund_id or rec.state is RescueState.REFUNDED:
        specs.append(("refund", "Refund", rec.refund_id, rec.refund_status, ("Refund",), ("Refund",)))
    for key, name, ident, stored_status, ops, hook_ops in specs:
        obj = live.get(key) or None
        status = (obj or {}).get("status") or stored_status
        amount = (obj or {}).get("amount")
        if amount is None and ident:
            amount = _amount({"authorization": rec.authorized_amount, "capture": rec.captured_amount,
                              "refund": rec.refunded_amount, "order": rec.quote_amount}[key])
        api_ticks, api_waiting = api(*ops, own=ops[0])
        hook_ticks, hook_empty = hooks(*hook_ops)
        if status:
            pill = "neutral" if key == "order" else _PILL.get((key, status), "voided")
        else:
            pill = "none"
        rows.append({
            "key": key,
            "object": name,
            "id": ident or "",
            "short_id": short_id(ident),
            "absent": not ident,
            "status": status or "",
            "pill": pill,
            "status_text": status or ("none" if final and not ident else "not yet" if not ident else "—"),
            "means": _meaning(key, status, rec, obj, owner),
            "amount": f"${amount}" if (amount and ident) else "—",
            "final_capture": bool(key == "capture" and (obj or {}).get("final_capture")),
            "api": api_ticks,
            "api_waiting": api_waiting,
            "webhook": hook_ticks,
            "webhook_empty": hook_empty if ident else "—",
        })
    checked = live.get("checked_at")
    return {
        "rows": rows,
        "empty": rec.order_id is None,
        "source": source,
        "checked": f"Read from {source} at {_iso_hms(checked)} UTC" if checked else f"Read from {source}",
        "errors": list(live.get("errors", []) or []),
        "webhooks_verifiable": webhooks_verifiable,
    }


# --------------------------------------------------------------------------------------------
# Key moments: the filtered timeline beside the console
# --------------------------------------------------------------------------------------------


# A verified webhook, in plain words. The event code is kept in the full log and the row's title.
_WEBHOOK_WORDS = {
    "CHECKOUT.ORDER.APPROVED": "PayPal confirmed the approval",
    "CHECKOUT.ORDER.COMPLETED": "PayPal confirmed the order",
    "PAYMENT.AUTHORIZATION.CREATED": "PayPal confirmed the hold",
    "PAYMENT.AUTHORIZATION.VOIDED": "PayPal confirmed the release",
    "PAYMENT.CAPTURE.COMPLETED": "PayPal confirmed the charge",
    "PAYMENT.CAPTURE.PENDING": "PayPal says the charge is pending",
    "PAYMENT.CAPTURE.DENIED": "PayPal denied the charge",
    "PAYMENT.CAPTURE.DECLINED": "PayPal declined the charge",
    "PAYMENT.CAPTURE.REFUNDED": "PayPal confirmed the refund",
    "PAYMENT.CAPTURE.REVERSED": "PayPal reversed the charge",
    "PAYMENT.REFUND.PENDING": "PayPal says the refund is pending",
    "PAYMENT.REFUND.FAILED": "PayPal says the refund failed",
}


class KeyMoments:
    """Turns timeline rows into key moments, keeping the counts a moment's words depend on
    (first lock check, probe pass k of 2, warranty check n of N). Feed it every row in order,
    from the snapshot and then from the live stream, so both say the same thing."""

    # Rows that repeat keep only their latest few on screen; the full log has every one.
    KEEP = {"agent": 3, "recheck": 1, "warranty": 1}

    def __init__(self, *, warranty_total: int | None = None, amount: str | None = None, owner: bool = True) -> None:
        self.gate = 0
        self.probe_passes = 0
        self.warranty = 0
        self.warranty_total = warranty_total
        self.amount = amount
        self.stopped = False
        self.who = _who(owner)

    def add(self, entry: Mapping[str, Any], data: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
        """The moment for one timeline row (``views.timeline_entry`` output), or None."""
        kind = entry.get("kind")
        text = str(entry.get("text", ""))
        data = data or {}
        moment: dict[str, Any] | None = None

        def m(icon: str, strong: str, rest: str = "", tone: str = "", code: str = "", group: str = "",
              title: str = "") -> dict[str, Any]:
            return {"icon": icon, "strong": strong, "text": rest, "tone": tone, "code": code, "group": group,
                    "title": title}

        if kind == "gate_check":
            self.gate += 1
            if not data.get("granted", "keyboard refused" not in text):
                moment = m("i-lock", "Keyboard refused.", f"PayPal says {data.get('status', 'not CREATED')}.", "danger")
            elif self.gate == 1:
                amount = f"${data.get('amount')}" if data.get("amount") else "the"
                moment = m("i-unlock", "Keyboard lent.", f"PayPal says the {amount} hold is live (CREATED).", "now")
            else:
                moment = m("i-refresh", "PayPal re-checked:", f"hold still live. Check {self.gate}.", group="recheck")
        elif kind == "gate_revoked":
            if "pressed Stop" in text:
                return None  # the Stop row says it
            moment = m("i-lock", "Keyboard locked:",
                       _lock_reason(text.replace("keyboard locked: ", ""), self.who["you"] == "you") + ".")
        elif kind == "stop":
            self.stopped = True
            moment = m("i-stop", f"{self.who['You']} pressed Stop.", "Keyboard locked, void sent.", "danger")
        elif kind == "agent_firewall_refusal":
            command = str(data.get("input", "")).strip()
            moment = m("i-shield", "Refused:", "never sent to the console.", "danger", code=command)
        elif kind == "agent_type":
            moment = m("i-type", "Typed", "", code=text.removeprefix("typed: ").replace(" ↵", "").strip(), group="agent")
        elif kind == "agent_key":
            moment = m("i-type", "Pressed", "", code=text.removeprefix("pressed ").strip(), group="agent")
        elif kind == "agent_note":
            moment = m("i-note", "Agent:", f"“{text}”", group="agent")
        elif kind == "agent_result":
            outcome, _, why = text.partition(": ")
            words = {"done": "Agent finished:", "gave_up": "Agent gave up:", "turn_limit": "Turn limit reached:"}
            moment = m("i-note", words.get(outcome, "Agent stopped:"), why or text,
                       "" if outcome == "done" else "danger")
        elif kind == "probe":
            if text.startswith("pass"):
                if self.probe_passes:
                    return None  # passes too close together say nothing new; the stable row follows
                self.probe_passes = 1
                moment = m("i-probe", f"{self.who['Your']} check passed", f"(1 of {STABLE_PASSES}).")
            else:
                self.probe_passes = 0
                moment = m("i-probe", f"{self.who['Your']} check failed:", text.partition(". ")[2] or text, "danger")
        elif kind == "probe_stable":
            moment = m("i-probe", f"{self.who['Your']} check passed twice",
                       f"({STABLE_PASSES} of {STABLE_PASSES}), far enough apart.")
        elif kind == "paypal_ok":
            if text.startswith("capture "):
                moment = m("i-charged", "Charged:", f"PayPal captured {_amount_in(text)}.", "charged")
            elif text == "authorization voided":
                moment = m("i-voided", "Hold released:", "PayPal voided it. $0 charged.")
            elif text.startswith("refund "):
                moment = m("i-refund", "Refunded:", f"PayPal returned {_amount_in(text)}.", "refunded")
        elif kind == "paypal_error":
            moment = m("i-info", "PayPal answered an error:", text, "danger")
        elif kind == "warranty_started":
            moment = m("i-shield", "Warranty started.", "")
        elif kind == "warranty_check":
            self.warranty += 1
            total = f" of {self.warranty_total}" if self.warranty_total else ""
            if text.startswith("pass"):
                moment = m("i-shield", f"Warranty check {self.warranty}{total} passed.", "", group="warranty")
            else:
                moment = m("i-shield", f"Warranty check {self.warranty}{total} failed.", text.partition(". ")[2],
                           "danger")
        elif kind == "webhook":
            if entry.get("outcome") == "duplicate":
                return None
            # PayPal's event code stays in the full log and in the row's title.
            code = str(entry.get("event_type") or "")
            if entry.get("verified"):
                moment = m("i-webhook", _WEBHOOK_WORDS.get(code, "PayPal sent an update"), "(signature verified).",
                           title=code or "unknown event")
            else:
                moment = m("i-webhook", _WEBHOOK_WORDS.get(code, "PayPal sent an update"),
                           "(signature not verified, so it was not applied).", "danger", title=code or "unknown event")
        elif entry.get("tone") == "warn" and kind not in ("state",):
            moment = m("i-info", "", text, "danger")
        if moment is None:
            return None
        moment["at"] = entry.get("at")
        moment["id"] = str(entry.get("id"))
        return moment


def _amount_in(text: str) -> str:
    for word in text.split():
        if word.startswith("$"):
            return word
    return "the amount"


def visible_moments(moments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The moments shown before "Show all": every one-off row, plus the latest few of each
    repeating group (agent actions, lock re-checks, warranty checks)."""
    seen: dict[str, int] = {}
    keep = []
    for moment in reversed(moments):
        group = moment.get("group") or ""
        limit = KeyMoments.KEEP.get(group)
        if limit is not None:
            seen[group] = seen.get(group, 0) + 1
            if seen[group] > limit:
                continue
        keep.append(moment)
    return list(reversed(keep))


def key_moments(entries: Iterable[Mapping[str, Any]], events_by_id: Mapping[str, Any], *,
                warranty_total: int | None = None, owner: bool = True) -> tuple[list[dict[str, Any]], KeyMoments]:
    """Moments for a snapshot's timeline, and the counter to continue with on the stream."""
    keeper = KeyMoments(warranty_total=warranty_total, owner=owner)
    out = []
    for entry in entries:
        data = events_by_id.get(str(entry.get("id")))
        moment = keeper.add(entry, data)
        if moment is not None:
            out.append(moment)
    return out, keeper


# --------------------------------------------------------------------------------------------
# The tale: how the money moved (end page)
# --------------------------------------------------------------------------------------------


def tale_view(rec: Any, events: Iterable[Any], *, title: str, agent: str = "The agent",
              plan: Any = None, owner: bool = True) -> list[dict[str, Any]]:
    """The end page's money story, oldest first. Each item: time, icon, node tone, whether the
    connector below it is in the held span, a heading, one sentence and an optional proof line.
    A run that ended before any hold gets a grey closing row (cancelled, expired, not held)."""
    events = list(events)
    f = Facts.read(events)
    w = _who(owner)
    quote = _amount(rec.quote_amount) or "0.00"
    held = _amount(rec.authorized_amount) or quote
    items: list[dict[str, Any]] = []

    def add(at: float | None, icon: str, head: str, text: str = "", *, tone: str = "", held_span: bool = False,
            proof: str = "", proof_title: str = "") -> None:
        items.append({"at": hms(at), "icon": icon, "tone": tone, "held_span": held_span, "title": head,
                      "text": text, "proof": proof, "proof_title": proof_title})

    add(f.quoted_at or rec.created_at, "i-quote", f"Quote: ${quote}, fixed before any key",
        f"A free look at the screen found: {title[0].lower() + title[1:] if title else 'the fault'}. Screenshots only.")
    reason = (rec.end_reason or "").strip()
    if rec.authorization_id is None:
        if rec.state is RescueState.VOIDED:
            ended = f.state_at.get("VOIDED") or f.stop_at
            if "pressed stop" in reason.lower():
                add(ended, "i-nohold", "Cancelled: server released, nothing held",
                    "The run was cancelled before the hold was approved. No PayPal hold, nothing charged.",
                    tone="voided")
            elif "not paid within" in reason:
                add(ended, "i-nohold", "Expired: not approved in time",
                    f"{_sentence(reason)} The server was released. Nothing was held or charged.", tone="voided")
            else:
                add(ended, "i-nohold", "No hold: PayPal did not confirm it",
                    f"{_sentence(reason) or 'PayPal did not confirm the hold.'} Nothing was charged.", tone="voided")
        return items
    still_held = rec.state in LIVE
    add(f.state_at.get("HELD"), "i-hold", f"Hold: {w['you']} approved, PayPal held ${held}", "Nothing charged yet.",
        tone="held", held_span=True,
        proof=f"Authorization {short_id(rec.authorization_id)} · CREATED ${held}", proof_title=rec.authorization_id)
    turns = f"{f.agent_turns} turn{'' if f.agent_turns == 1 else 's'}" if f.agent_turns else "a while"
    if f.stop_at is not None:
        add(f.revoked_at or f.stop_at, "i-key", "Keyboard: lent, then taken back",
            f"{w['You']} pressed Stop at {hms(f.stop_pressed_at)}. The keyboard locked before the void was sent. "
            + stop_note(f), held_span=True)
    elif f.revoked_at is not None:
        add(f.revoked_at, "i-key", "Keyboard: lent, then locked",
            f"{agent} typed for {turns} while PayPal said the hold was live. "
            f"Then the keyboard locked: {_lock_reason(f.revoked_reason, owner)}.", held_span=True)
    if f.stable_at is not None:
        passes = f.stable_passes or f.probe_passes
        add(f.stable_at, "i-probe", f"Probe: {w['your']} own check passed twice",
            f"{' and '.join(hms(p) for p in passes[:STABLE_PASSES])}, no failure between.",
            held_span=True, proof=f.probe_last_detail)
    elif f.probe_runs and rec.state is RescueState.VOIDED:
        add(f.void_at, "i-probe", f"Probe: {w['your']} check did not pass twice", _sentence(f.probe_last_detail),
            held_span=True)
    if rec.capture_id and rec.capture_status not in NO_MONEY_CAPTURE:
        captured = _amount(rec.captured_amount) or held
        after = ""
        if "HELD" in f.state_at and "CAPTURED" in f.state_at:
            after = f"{duration(f.state_at['CAPTURED'] - f.state_at['HELD'])} after the hold."
        add(f.capture_at or f.state_at.get("CAPTURED"), "i-charged", f"Charged: PayPal captured ${captured}", after,
            tone="charged", proof=f"Capture {short_id(rec.capture_id)} · {rec.capture_status or 'COMPLETED'} ${captured}",
            proof_title=rec.capture_id)
        total = f.warranty_total(plan)
        label = _warranty_short(f.warranty, plan)
        if rec.state is RescueState.WARRANTY_OK:
            add(f.state_at.get("WARRANTY_OK") or f.warranty_last_at, "i-shield",
                f"Warranty: {f.warranty_passes} of {total} checks passed", _sentence(label))
        elif rec.state is RescueState.REFUNDED:
            refunded = _amount(rec.refunded_amount) or captured
            add(f.refund_at or f.state_at.get("REFUNDED"), "i-refund", f"Refunded: PayPal returned ${refunded}",
                f"Two warranty checks in a row failed ({f.refund_due or 'the server stopped answering'}).",
                tone="refunded", proof=f"Refund {short_id(rec.refund_id)} · {rec.refund_status or ''}".strip(" ·"),
                proof_title=rec.refund_id or "")
        else:
            leave = " You can leave: the result lands on the ledger." if owner else " The result lands on the ledger."
            add(f.warranty_last_at or f.capture_at or f.state_at.get("CAPTURED"), "i-shield",
                f"Warranty: {f.warranty_runs} of {total} checks so far",
                _sentence(label) + leave)
    elif rec.state is RescueState.VOIDED:
        why = f"{w['You']} pressed Stop." if "pressed stop" in reason.lower() else _sentence(reason)
        add(f.void_at or f.state_at.get("VOIDED"), "i-voided", "Released: PayPal voided the hold",
            f"$0 charged. {why}".strip(), tone="voided",
            proof=f"Authorization {short_id(rec.authorization_id)} · VOIDED", proof_title=rec.authorization_id)
    elif still_held:
        add(None, "i-hold", "Still on hold", "The rescue is running.", tone="held")
    # The held span ends at the money decision.
    for item in items:
        item["held_span"] = item["held_span"] and item["tone"] not in ("charged", "voided", "refunded")
    if items:
        items[-1]["held_span"] = False
    return items
