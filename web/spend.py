"""Model-spend cap for /try.

Every Claude reply is priced from its token counts at the list price of its model:
claude-opus-5-5 at $4 per million input tokens and $20 per million output tokens;
claude-haiku-5-5 at $0.10 and $0.50 for prompts of 100K tokens or fewer, $0.50 and $2.50 above.
A model the table does not know is priced as claude-opus-5-5. Prompt caching is on, so most input arrives
as cache writes (priced at 1.25 times the input price) or cache reads (0.1 times); the API counts
them apart from ``input_tokens`` and all three are priced here. Each reply is logged to the ledger
as a ``model_spend`` event, so the total survives a restart.

Two limits:

- a cap per rolling 24 hours for the whole deployment: /try refuses to start a run once the
  remaining budget is below one run's budget;
- a budget per run: ``MeteredModel`` refuses the next call once the run has spent it. The agent
  loop treats that like an unavailable model, so the run ends and the hold is voided.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from decimal import ROUND_UP, Decimal
from typing import Any

from keyhold.agent.model import MODEL_ID, ModelUnavailable
from keyhold.pay.ledger import Ledger
from keyhold.types import ModelReply

__all__ = ["PRICE_PER_MTOK", "RATES", "MeteredModel", "SpendMeter", "reply_cost"]

# claude-opus-5-5 list prices, US dollars per million tokens.
PRICE_PER_MTOK = {
    "input": Decimal(4),
    "output": Decimal(20),
    "cache_write": Decimal(5),  # 1.25 x input (5-minute cache)
    "cache_read": Decimal("0.4"),  # 0.1 x input
}
# Rate cards per model: (largest prompt in tokens it applies to or None, input, output), in order.
RATES: dict[str, tuple[tuple[int | None, Decimal, Decimal], ...]] = {
    "claude-opus-5-5": ((None, Decimal(4), Decimal(20)),),
    "claude-haiku-5-5": ((100_000, Decimal("0.10"), Decimal("0.50")), (None, Decimal("0.50"), Decimal("2.50"))),
}
_MILLION = Decimal(1_000_000)
_MICRO = Decimal("0.000001")
SPEND_EVENT = "model_spend"
WINDOW_S = 24 * 3600.0


def _rate_key(*names: object) -> str:
    for name in names:
        text = str(name or "")
        for key in RATES:
            if text == key or text.startswith(key + "-"):
                return key
    return MODEL_ID


def reply_cost(reply: ModelReply, model: str | None = None) -> Decimal:
    """Estimated cost of one reply in US dollars, rounded up to a millionth of a dollar.

    The rate card is the one for ``reply.model`` if the table knows it, else for ``model`` (the
    model the request asked for), else claude-opus-5-5's."""
    def tokens(value: object) -> Decimal:
        return Decimal(max(0, int(value or 0)))  # type: ignore[call-overload]

    fresh = tokens(reply.input_tokens)
    written = tokens(getattr(reply, "cache_creation_input_tokens", 0))
    read = tokens(getattr(reply, "cache_read_input_tokens", 0))
    prompt = fresh + written + read
    rate_in, rate_out = RATES[MODEL_ID][0][1:]
    for limit, r_in, r_out in RATES[_rate_key(getattr(reply, "model", ""), model)]:
        rate_in, rate_out = r_in, r_out
        if limit is None or prompt <= limit:
            break
    cost = (
        fresh * rate_in
        + tokens(reply.output_tokens) * rate_out
        + written * rate_in * Decimal("1.25")
        + read * rate_in * Decimal("0.1")
    ) / _MILLION
    return cost.quantize(_MICRO, rounding=ROUND_UP)


class SpendMeter:
    def __init__(
        self,
        ledger: Ledger,
        cap_usd: Decimal,
        *,
        clock: Callable[[], float],
        window_s: float = WINDOW_S,
    ) -> None:
        self.ledger = ledger
        self.cap = Decimal(cap_usd)
        self.window_s = window_s
        self._now = clock
        self._lock = threading.Lock()
        self._entries: list[tuple[float, Decimal]] = []
        for event in ledger.events(kind=SPEND_EVENT):
            try:
                self._entries.append((float(event.at), Decimal(str(event.data.get("usd", "0")))))
            except (ArithmeticError, ValueError):
                continue

    def spent(self) -> Decimal:
        since = self._now() - self.window_s
        with self._lock:
            self._entries = [e for e in self._entries if e[0] > since]
            return sum((usd for _, usd in self._entries), Decimal(0))

    def remaining(self) -> Decimal:
        return max(Decimal(0), self.cap - self.spent())

    def exhausted(self) -> bool:
        return self.spent() >= self.cap

    def record(self, reply: ModelReply, *, rescue_id: str | None, run_id: str, purpose: str,
               model: str | None = None) -> Decimal:
        usd = reply_cost(reply, model)
        with self._lock:
            self._entries.append((self._now(), usd))
        self.ledger.log(
            rescue_id,
            SPEND_EVENT,
            f"{purpose}: {reply.input_tokens} input, {reply.cache_creation_input_tokens} cache-write, "
            f"{reply.cache_read_input_tokens} cache-read and {reply.output_tokens} output tokens, about ${usd:.4f}",
            {
                "usd": str(usd),
                "run_id": run_id,
                "purpose": purpose,
                "model": reply.model or model,
                "input_tokens": int(reply.input_tokens),
                "cache_creation_input_tokens": int(reply.cache_creation_input_tokens),
                "cache_read_input_tokens": int(reply.cache_read_input_tokens),
                "output_tokens": int(reply.output_tokens),
            },
        )
        return usd


class MeteredModel:
    """A ``ModelClient`` that prices each reply and stops at the run budget or the daily cap."""

    def __init__(
        self,
        inner: Any,
        meter: SpendMeter,
        *,
        run_id: str,
        budget_usd: Decimal,
        rescue_id: Callable[[], str | None] = lambda: None,
    ) -> None:
        self.inner = inner
        self.meter = meter
        self.run_id = run_id
        self.budget = Decimal(budget_usd)
        self.spent = Decimal(0)
        self._rescue_id = rescue_id
        self.calls = 0

    @property
    def model(self) -> str:
        return str(getattr(self.inner, "model", MODEL_ID))

    def respond(self, **kwargs: Any) -> ModelReply:
        if self.spent >= self.budget:
            raise ModelUnavailable(f"this run used its model budget of ${self.budget}")
        if self.meter.exhausted():
            raise ModelUnavailable("the model-spend cap for the last 24 hours is reached")
        reply = self.inner.respond(**kwargs)
        self.calls += 1
        purpose = "agent" if kwargs.get("tools") else "free look"
        self.spent += self.meter.record(reply, rescue_id=self._rescue_id(), run_id=self.run_id, purpose=purpose,
                                        model=self.model)
        return reply
