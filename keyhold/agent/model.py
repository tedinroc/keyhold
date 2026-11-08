"""Claude wrapper implementing ``keyhold.types.ModelClient``, plus FakeModel for tests.

Uses the official ``anthropic`` SDK with claude-opus-5-5 (default) or claude-haiku-5-5
(``KEYHOLD_MODEL``), adaptive thinking, an explicit ``output_config.effort``, and, on
claude-opus-5-5, server-side refusal fallbacks. Claude Haiku 5.5 rejects the ``fallbacks``
parameter, so its requests leave the beta and the parameter out. Handles stop_reason "refusal".
See docs/SDK_NOTES.md for the exact parameter names.

Request shape (``ClaudeModel.respond``)::

    client.beta.messages.create(
        model="claude-opus-5-5", max_tokens=16000, system=..., messages=..., tools=...,
        thinking={"type": "adaptive", "display": "summarized"},
        output_config={"effort": "medium"[, "format": {"type": "json_schema", "schema": ...}]},
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",   # claude-opus-5-5 only
        cache_control={"type": "ephemeral"},
    )

``fallbacks="default"`` lets the API re-run a request that a safety classifier declined on the
model Anthropic recommends for that refusal category, inside the same call. A
``stop_reason == "refusal"`` that still comes back means the whole chain declined; callers branch
on ``ModelReply.stop_reason`` and never index ``content[0]``.

API failures (network, 4xx, 5xx after the SDK's own retries, missing credentials) raise
:class:`ModelUnavailable`, whose message never contains credentials.
"""

from __future__ import annotations

import base64
import io
import itertools
import json
import re
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from keyhold.types import ModelReply

MODEL_ID = "claude-opus-5-5"
SUPPORTED_MODELS = ("claude-opus-5-5", "claude-haiku-5-5")
# Models that accept server-side refusal fallbacks. Claude Haiku 5.5 returns 400 for ``fallbacks``.
FALLBACK_MODELS = frozenset({"claude-opus-5-5"})
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACKS = "default"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT = "medium"
DEFAULT_MAX_TOKENS = 16000
USER_AGENT = "Keyhold/0.1"
_HEADER_NAME = re.compile(r"[A-Za-z0-9-]{1,64}")
# Headers the SDK or Keyhold set themselves; a session header may not replace them.
RESERVED_HEADERS = frozenset(
    {"user-agent", "x-api-key", "authorization", "anthropic-version", "anthropic-beta", "content-type",
     "content-length", "host", "accept"}
)


def valid_session_header(name: str) -> bool:
    return bool(_HEADER_NAME.fullmatch(name)) and name.lower() not in RESERVED_HEADERS
MAX_IMAGE_SIDE = 2000  # keep screenshots at or below this many pixels per side


class ModelUnavailable(Exception):
    """The model could not be reached or rejected the request (not a refusal; see stop_reason).

    ``status_code`` is the HTTP status when there was one. ``retryable`` is True for connection
    errors, timeouts, 408/409/429 and 5xx, which the SDK has already retried.
    """

    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False,
                 request_id: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.request_id = request_id


# --------------------------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------------------------


def png_size(png: bytes) -> tuple[int, int]:
    from PIL import Image

    with Image.open(io.BytesIO(png)) as img:
        return img.size


def fit_png(data: bytes, max_side: int = MAX_IMAGE_SIDE) -> tuple[bytes, int, int]:
    """Return PNG bytes no larger than ``max_side`` on either side, with the final size.

    Anything Pillow can read is accepted; the result is always PNG. Raises ``ValueError`` for
    bytes that are not an image.
    """
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            width, height = img.size
            if img.format == "PNG" and max(width, height) <= max_side:
                return data, width, height
            out = img.copy()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("screenshot is not a readable image") from exc
    if max(width, height) > max_side:
        out.thumbnail((max_side, max_side))
    if out.mode not in ("RGB", "RGBA", "L", "LA", "P"):
        out = out.convert("RGB")
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), out.size[0], out.size[1]


def image_block(png: bytes) -> dict[str, Any]:
    """An API image content block for a console screenshot (downscaled to fit if needed)."""
    data, _, _ = fit_png(png)
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": base64.standard_b64encode(data).decode("ascii")},
    }


# --------------------------------------------------------------------------------------------
# Reading replies
# --------------------------------------------------------------------------------------------


def tool_uses(reply: ModelReply) -> list[dict[str, Any]]:
    return [b for b in reply.content if b.get("type") == "tool_use"]


def reply_text(reply: ModelReply) -> str:
    """All text blocks joined, or "" (a refusal can have no content at all)."""
    return "".join(b.get("text", "") for b in reply.content if b.get("type") == "text")


def thinking_text(reply: ModelReply) -> str:
    return "\n".join(b.get("thinking", "") for b in reply.content if b.get("type") == "thinking" and b.get("thinking"))


def fallback_used(reply: ModelReply, requested: str = MODEL_ID) -> bool:
    return any(b.get("type") == "fallback" for b in reply.content) or bool(reply.model and reply.model != requested)


def reply_from_message(message: Any) -> ModelReply:
    """Turn an SDK ``BetaMessage`` into a ``ModelReply``; blocks keep their API shape."""
    content = [block.to_dict() for block in (message.content or [])]
    category = None
    if message.stop_reason == "refusal":
        details = getattr(message, "stop_details", None)
        category = getattr(details, "category", None) if details is not None else None
    usage = getattr(message, "usage", None)
    return ModelReply(
        stop_reason=str(message.stop_reason or ""),
        content=content,
        model=str(getattr(message, "model", "") or ""),
        refusal_category=category,
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        cache_creation_input_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
        cache_read_input_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
    )


# --------------------------------------------------------------------------------------------
# Claude
# --------------------------------------------------------------------------------------------


class ClaudeModel:
    """``ModelClient`` backed by Claude through the official SDK.

    ``client`` defaults to ``anthropic.Anthropic()``, which reads ANTHROPIC_API_KEY (or another
    configured credential) from the environment. Pass a client to use a custom transport.
    """

    def __init__(
        self,
        client: Any | None = None,
        *,
        model: str = MODEL_ID,
        effort: str = DEFAULT_EFFORT,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout: float = 120.0,
        max_retries: int = 2,
        cache: bool = True,
        session_header: str | None = None,
    ) -> None:
        if session_header is not None and not valid_session_header(session_header):
            raise ValueError("session_header must be an HTTP header name that the SDK does not set itself")
        if model not in SUPPORTED_MODELS:
            raise ValueError(f"model must be one of {', '.join(SUPPORTED_MODELS)}")
        if effort not in EFFORT_LEVELS:
            raise ValueError(f"effort must be one of {', '.join(EFFORT_LEVELS)}")
        if max_tokens < 1024:
            raise ValueError("max_tokens must leave room for thinking (at least 1024)")
        if client is None:
            import anthropic

            client = anthropic.Anthropic(timeout=timeout, max_retries=max_retries)
        self._client = client
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.cache = cache
        # Some Anthropic-compatible gateways route by conversation and ask for one stable id per
        # conversation in a header. One ClaudeModel serves one rescue, so the id lives here.
        self.session_header = session_header
        self.session_id = uuid.uuid4().hex if session_header else None
        self.last_request_id: str | None = None

    def request_params(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The keyword arguments for ``client.beta.messages.create`` (no network)."""
        output_config: dict[str, Any] = {"effort": self.effort}
        if output_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": output_schema}
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": messages,
            "thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": output_config,
        }
        if self.model in FALLBACK_MODELS:
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = FALLBACKS
        if tools:
            params["tools"] = tools
        if self.cache:
            params["cache_control"] = {"type": "ephemeral"}
        if self.session_header and self.session_id:
            params["extra_headers"] = {self.session_header: self.session_id, "User-Agent": USER_AGENT}
        return params

    def _has_credentials(self) -> bool:
        attrs = [getattr(self._client, name, _MISSING) for name in ("api_key", "auth_token", "credentials")]
        if all(a is _MISSING for a in attrs):
            return True  # not an SDK client we can inspect; let it try
        return any(a not in (None, _MISSING) for a in attrs)

    def respond(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> ModelReply:
        import anthropic

        if not self._has_credentials():
            raise ModelUnavailable("no Anthropic credentials are configured (set ANTHROPIC_API_KEY)")
        params = self.request_params(system=system, messages=messages, tools=tools, output_schema=output_schema)
        try:
            message = self._client.beta.messages.create(**params)
        except anthropic.APIStatusError as exc:
            status = exc.status_code
            raise ModelUnavailable(
                f"Claude API returned HTTP {status}: {exc.message}",
                status_code=status,
                retryable=status in (408, 409, 429) or status >= 500,
                request_id=getattr(exc, "request_id", None),
            ) from None
        except anthropic.APITimeoutError:
            raise ModelUnavailable("Claude API timed out", retryable=True) from None
        except anthropic.APIConnectionError:
            raise ModelUnavailable("could not connect to the Claude API", retryable=True) from None
        except anthropic.APIError as exc:
            raise ModelUnavailable(f"Claude API error: {type(exc).__name__}") from None
        self.last_request_id = getattr(message, "_request_id", None)
        return reply_from_message(message)


_MISSING = object()


# --------------------------------------------------------------------------------------------
# FakeModel
# --------------------------------------------------------------------------------------------


@dataclass
class ModelRequest:
    """One call recorded by FakeModel. ``messages`` is a snapshot of the list at call time."""

    system: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None
    output_schema: dict[str, Any] | None


Step = ModelReply | BaseException | Callable[[ModelRequest], ModelReply]


class FakeModelExhausted(RuntimeError):
    """FakeModel was asked for more replies than its script holds."""


class FakeModel:
    """Scripted ``ModelClient``. Each call takes the next step: a ``ModelReply`` is returned, an
    exception is raised, a callable gets the ``ModelRequest`` and returns a reply."""

    def __init__(self, script: Iterable[Step] = (), *, model: str = MODEL_ID) -> None:
        self.script: list[Step] = list(script)
        self.calls: list[ModelRequest] = []
        self.model = model

    def push(self, *steps: Step) -> None:
        self.script.extend(steps)

    @property
    def remaining(self) -> int:
        return len(self.script)

    def respond(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> ModelReply:
        request = ModelRequest(system, list(messages), tools, output_schema)
        self.calls.append(request)
        if not self.script:
            raise FakeModelExhausted(f"FakeModel has no reply for call {len(self.calls)}")
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        if callable(step) and not isinstance(step, ModelReply):
            return step(request)
        return step


_ids = itertools.count(1)


def _tool_id() -> str:
    return f"toolu_fake_{next(_ids):05d}"


def _thinking(thinking: str | None) -> list[dict[str, Any]]:
    if thinking is None:
        return []
    return [{"type": "thinking", "thinking": thinking, "signature": "fake-signature"}]


def tool_reply(name: str, tool_input: dict[str, Any] | None = None, *, thinking: str | None = None,
               model: str = MODEL_ID) -> ModelReply:
    """A reply that calls one tool."""
    return tools_reply([(name, tool_input or {})], thinking=thinking, model=model)


def tools_reply(calls: Iterable[tuple[str, dict[str, Any]]], *, thinking: str | None = None,
                model: str = MODEL_ID) -> ModelReply:
    """A reply that calls several tools in order."""
    blocks = _thinking(thinking) + [
        {"type": "tool_use", "id": _tool_id(), "name": name, "input": dict(tool_input)} for name, tool_input in calls
    ]
    return ModelReply(stop_reason="tool_use", content=blocks, model=model, input_tokens=10, output_tokens=5)


def text_reply(text: str, *, stop_reason: str = "end_turn", thinking: str | None = None,
               model: str = MODEL_ID) -> ModelReply:
    return ModelReply(stop_reason=stop_reason, content=_thinking(thinking) + [{"type": "text", "text": text}],
                      model=model, input_tokens=10, output_tokens=5)


def json_reply(obj: Any, *, model: str = MODEL_ID) -> ModelReply:
    """A structured-output reply: one text block holding JSON."""
    return text_reply(json.dumps(obj), model=model)


def refusal_reply(category: str | None = None, *, model: str = MODEL_ID) -> ModelReply:
    """A refusal with no content, as a classifier decline before any output looks."""
    return ModelReply(stop_reason="refusal", content=[], model=model, refusal_category=category)


__all__ = [
    "ClaudeModel",
    "DEFAULT_EFFORT",
    "EFFORT_LEVELS",
    "FALLBACK_BETA",
    "FakeModel",
    "FakeModelExhausted",
    "MODEL_ID",
    "ModelRequest",
    "ModelUnavailable",
    "fallback_used",
    "fit_png",
    "image_block",
    "json_reply",
    "refusal_reply",
    "reply_from_message",
    "reply_text",
    "text_reply",
    "thinking_text",
    "tool_reply",
    "tool_uses",
    "tools_reply",
]
