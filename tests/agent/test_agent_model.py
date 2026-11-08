"""Claude wrapper: request shape (model, adaptive thinking, explicit effort, server-side fallbacks,
structured output, images), response parsing (thinking, tool use, refusal, fallback blocks),
error mapping, and the scripted FakeModel. No sockets are opened: the SDK runs on a mock
transport."""

from __future__ import annotations

import base64
import io
import json
from typing import Any

import anthropic
import httpx2
import pytest
from agent_fakes import make_png
from PIL import Image

from keyhold.agent.model import (
    FALLBACK_BETA,
    MODEL_ID,
    ClaudeModel,
    FakeModel,
    FakeModelExhausted,
    ModelUnavailable,
    fallback_used,
    fit_png,
    image_block,
    json_reply,
    refusal_reply,
    reply_text,
    text_reply,
    thinking_text,
    tool_reply,
    tool_uses,
)
from keyhold.types import ModelClient, ModelReply

TEST_KEY = "test-key-not-a-secret"


def _message(content: list[dict[str, Any]], stop_reason: str = "end_turn", **extra: Any) -> dict[str, Any]:
    body = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": MODEL_ID,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "stop_details": None,
        "usage": {"input_tokens": 120, "output_tokens": 30},
    }
    body.update(extra)
    return body


class Recorder:
    """A mock transport handler that records requests and answers from a queue."""

    def __init__(self, *responses: httpx2.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.responses.pop(0)

    def body(self, i: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[i].content)


def claude(recorder: Recorder, **kwargs: Any) -> ClaudeModel:
    client = anthropic.Anthropic(
        api_key=TEST_KEY,
        max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(recorder)),
    )
    return ClaudeModel(client, **kwargs)


def test_claude_model_is_a_model_client() -> None:
    assert isinstance(claude(Recorder()), ModelClient)
    assert isinstance(FakeModel(), ModelClient)


def test_request_shape_for_opus_5_5() -> None:
    rec = Recorder(httpx2.Response(200, json=_message([{"type": "text", "text": "{}"}])))
    model = claude(rec, effort="high")
    schema = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    tools = [{"name": "screenshot", "description": "x", "input_schema": schema, "strict": True}]
    model.respond(system="sys", messages=[{"role": "user", "content": "hi"}], tools=tools, output_schema=schema)

    req = rec.requests[0]
    assert req.url.path == "/v1/messages"
    assert FALLBACK_BETA in req.headers["anthropic-beta"]
    body = rec.body()
    assert body["model"] == "claude-opus-5-5"
    assert body["fallbacks"] == "default"
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert body["output_config"]["effort"] == "high"
    assert body["output_config"]["format"] == {"type": "json_schema", "schema": schema}
    assert body["tools"] == tools
    assert body["system"] == "sys"
    assert body["max_tokens"] >= 1024
    assert body["cache_control"] == {"type": "ephemeral"}
    assert "tool_choice" not in body  # forced tool choice is a 400 on this model
    assert "budget_tokens" not in json.dumps(body["thinking"])


def test_effort_is_always_explicit_and_validated() -> None:
    rec = Recorder(httpx2.Response(200, json=_message([{"type": "text", "text": "ok"}])))
    claude(rec).respond(system="s", messages=[{"role": "user", "content": "x"}])
    assert rec.body()["output_config"] == {"effort": "medium"}
    with pytest.raises(ValueError):
        ClaudeModel(object(), effort="extreme")
    with pytest.raises(ValueError):
        ClaudeModel(object(), max_tokens=100)


def test_tool_use_reply_keeps_api_shaped_blocks() -> None:
    content = [
        {"type": "thinking", "thinking": "The fstab line 3 looks wrong.", "signature": "sig-abc"},
        {"type": "tool_use", "id": "toolu_1", "name": "type", "input": {"text": "ls\n"}},
    ]
    rec = Recorder(httpx2.Response(200, json=_message(content, "tool_use")))
    reply = claude(rec).respond(system="s", messages=[{"role": "user", "content": "x"}])
    assert reply.stop_reason == "tool_use"
    assert reply.content == content  # appended back unchanged by the loop
    assert tool_uses(reply) == [content[1]]
    assert thinking_text(reply) == "The fstab line 3 looks wrong."
    assert reply.input_tokens == 120 and reply.output_tokens == 30
    assert reply.refusal_category is None


def test_refusal_is_returned_not_raised_and_content_may_be_empty() -> None:
    body = _message(
        [],
        "refusal",
        stop_details={"type": "refusal", "category": "cyber", "explanation": "declined", "recommended_model": None},
    )
    rec = Recorder(httpx2.Response(200, json=body))
    reply = claude(rec).respond(system="s", messages=[{"role": "user", "content": "x"}])
    assert reply.stop_reason == "refusal"
    assert reply.refusal_category == "cyber"
    assert reply.content == []
    assert reply_text(reply) == ""


def test_refusal_without_stop_details() -> None:
    rec = Recorder(httpx2.Response(200, json=_message([], "refusal")))
    reply = claude(rec).respond(system="s", messages=[{"role": "user", "content": "x"}])
    assert reply.stop_reason == "refusal" and reply.refusal_category is None


def test_server_side_fallback_block_is_kept_and_detected() -> None:
    content = [
        {"type": "fallback", "from": {"model": MODEL_ID}, "to": {"model": "claude-opus-5"}, "trigger": {"type": "refusal"}},
        {"type": "text", "text": "{\"ok\": true}"},
    ]
    rec = Recorder(httpx2.Response(200, json=_message(content, model="claude-opus-5")))
    reply = claude(rec).respond(system="s", messages=[{"role": "user", "content": "x"}])
    assert reply.model == "claude-opus-5"
    assert reply.content[0]["type"] == "fallback"
    assert fallback_used(reply)
    assert reply_text(reply) == '{"ok": true}'
    assert not fallback_used(text_reply("x"))


@pytest.mark.parametrize(("status", "retryable"), [(400, False), (401, False), (429, True), (500, True), (529, True)])
def test_api_errors_become_model_unavailable(status: int, retryable: bool) -> None:
    error = {"type": "error", "error": {"type": "api_error", "message": "boom"}}
    rec = Recorder(httpx2.Response(status, json=error))
    with pytest.raises(ModelUnavailable) as info:
        claude(rec).respond(system="s", messages=[{"role": "user", "content": "x"}])
    assert info.value.status_code == status
    assert info.value.retryable is retryable
    assert TEST_KEY not in str(info.value)


def test_connection_error_becomes_model_unavailable() -> None:
    def broken(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route")

    client = anthropic.Anthropic(
        api_key=TEST_KEY, max_retries=0, http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(broken))
    )
    with pytest.raises(ModelUnavailable) as info:
        ClaudeModel(client).respond(system="s", messages=[{"role": "user", "content": "x"}])
    assert info.value.retryable


def test_missing_credentials_fail_before_any_request(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    rec = Recorder()
    client = anthropic.Anthropic(max_retries=0, http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(rec)))
    with pytest.raises(ModelUnavailable, match="credentials"):
        ClaudeModel(client).respond(system="s", messages=[{"role": "user", "content": "x"}])
    assert rec.requests == []


def test_image_block_is_png_base64() -> None:
    png = make_png(size=(80, 60))
    block = image_block(png)
    assert block["type"] == "image"
    assert block["source"]["media_type"] == "image/png"
    assert base64.standard_b64decode(block["source"]["data"]) == png


def test_large_screenshots_are_downscaled_to_2000px() -> None:
    big = make_png(size=(3000, 1200))
    data, w, h = fit_png(big)
    assert max(w, h) == 2000
    with Image.open(io.BytesIO(data)) as img:
        assert img.format == "PNG" and max(img.size) <= 2000
    jpeg = io.BytesIO()
    Image.new("RGB", (40, 30)).save(jpeg, format="JPEG")
    data, w, h = fit_png(jpeg.getvalue())
    assert data.startswith(b"\x89PNG") and (w, h) == (40, 30)
    with pytest.raises(ValueError):
        fit_png(b"not an image")


def test_fake_model_runs_its_script_and_records_calls() -> None:
    seen: list[int] = []

    def dynamic(request) -> ModelReply:
        seen.append(len(request.messages))
        return text_reply("dynamic")

    fake = FakeModel([tool_reply("screenshot"), ModelUnavailable("down"), dynamic, json_reply({"a": 1}), refusal_reply("bio")])
    messages: list[dict[str, Any]] = [{"role": "user", "content": "x"}]
    first = fake.respond(system="s", messages=messages)
    assert tool_uses(first)[0]["name"] == "screenshot"
    messages.append({"role": "assistant", "content": first.content})
    with pytest.raises(ModelUnavailable):
        fake.respond(system="s", messages=messages)
    assert reply_text(fake.respond(system="s", messages=messages)) == "dynamic"
    assert seen == [2]
    assert json.loads(reply_text(fake.respond(system="s", messages=messages, output_schema={}))) == {"a": 1}
    assert fake.respond(system="s", messages=messages).refusal_category == "bio"
    assert len(fake.calls) == 5
    assert len(fake.calls[0].messages) == 1  # snapshot, not the live list
    assert fake.calls[3].output_schema == {}
    with pytest.raises(FakeModelExhausted):
        fake.respond(system="s", messages=messages)


def test_fake_tool_ids_are_unique() -> None:
    ids = {tool_uses(tool_reply("screenshot"))[0]["id"] for _ in range(50)}
    assert len(ids) == 50


# -- model choice ---------------------------------------------------------------------------------


def test_haiku_requests_leave_out_the_server_side_fallback() -> None:
    from keyhold.agent.model import ClaudeModel

    haiku = ClaudeModel(client=object(), model="claude-haiku-5-5")
    params = haiku.request_params(system="s", messages=[{"role": "user", "content": "hi"}])
    assert params["model"] == "claude-haiku-5-5"
    assert "fallbacks" not in params and "betas" not in params
    assert params["thinking"]["type"] == "adaptive" and params["output_config"]["effort"] == "medium"

    opus = ClaudeModel(client=object())
    params = opus.request_params(system="s", messages=[{"role": "user", "content": "hi"}])
    assert params["fallbacks"] == "default" and params["betas"] == [FALLBACK_BETA]


def test_unknown_models_are_refused() -> None:
    from keyhold.agent.model import ClaudeModel

    with pytest.raises(ValueError, match="model must be one of"):
        ClaudeModel(client=object(), model="claude-unknown-1")


def test_session_header_carries_one_stable_id_per_model() -> None:
    from keyhold.agent.model import ClaudeModel

    model = ClaudeModel(client=object(), model="claude-haiku-5-5", session_header="x-session-id")
    first = model.request_params(system="s", messages=[{"role": "user", "content": "a"}])
    second = model.request_params(system="s", messages=[{"role": "user", "content": "b"}])
    sid = first["extra_headers"]["x-session-id"]
    assert len(sid) == 32 and second["extra_headers"]["x-session-id"] == sid
    other = ClaudeModel(client=object(), model="claude-haiku-5-5", session_header="x-session-id")
    assert other.session_id != sid
    assert "extra_headers" not in ClaudeModel(client=object()).request_params(system="s", messages=[])
    with pytest.raises(ValueError):
        ClaudeModel(client=object(), session_header="bad header\\n")


@pytest.mark.parametrize("name", ["User-Agent", "x-api-key", "Authorization", "anthropic-beta", "x-session-id\n"])
def test_session_header_may_not_replace_sdk_headers_or_carry_newlines(name: str) -> None:
    from keyhold.agent.model import ClaudeModel

    with pytest.raises(ValueError):
        ClaudeModel(client=object(), session_header=name)
