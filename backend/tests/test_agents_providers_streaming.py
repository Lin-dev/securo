"""Coverage for the streaming chat + embed paths in
app/agents/providers/{ollama,anthropic}.py. Both providers open
httpx.AsyncClient.stream(...) inline, so we replace AsyncClient with a
fake whose stream() yields a pre-baked SSE/JSONL response.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Iterable

import pytest

import app.agents.providers.anthropic as anthropic_module
import app.agents.providers.ollama as ollama_module
from app.agents.config import get_agent_settings
from app.agents.providers.anthropic import AnthropicProvider
from app.agents.providers.base import (
    ChatMessage,
    LLMAuthError,
    LLMNotSupportedError,
    LLMRateLimitError,
    LLMUnavailableError,
    ToolDefinition,
)
from app.agents.providers.ollama import OllamaProvider


# --------------------------------------------------------------------- httpx fake

class _FakeStreamResponse:
    """Stand-in for the response object yielded from `client.stream(...)`."""

    def __init__(self, *, status_code: int, lines: Iterable[str], body: bytes = b""):
        self.status_code = status_code
        self._lines = list(lines)
        self._body = body

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln

    async def aread(self) -> bytes:
        return self._body


class _FakeResponse:
    """Stand-in for a plain (non-streaming) response."""

    def __init__(self, *, status_code: int, json_body=None, text: str = ""):
        self.status_code = status_code
        self._body = json_body
        self.text = text

    def json(self):
        return self._body


class _FakeAsyncClient:
    queue_stream: list[_FakeStreamResponse] = []
    queue_post: list[_FakeResponse] = []
    posted: list[tuple[str, dict]] = []
    init_kwargs: dict = {}

    def __init__(self, *_, **kwargs):  # accept (timeout=...) kw
        type(self).init_kwargs = dict(kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @asynccontextmanager
    async def stream(self, method, url, *, json=None, headers=None):  # noqa: A002
        type(self).posted.append((url, json or {}))
        yield type(self).queue_stream.pop(0)

    async def post(self, url, *, json=None, headers=None):  # noqa: A002
        type(self).posted.append((url, json or {}))
        return type(self).queue_post.pop(0)


@pytest.fixture(autouse=True)
def _fake_httpx(monkeypatch):
    _FakeAsyncClient.queue_stream = []
    _FakeAsyncClient.queue_post = []
    _FakeAsyncClient.posted = []
    _FakeAsyncClient.init_kwargs = {}
    monkeypatch.setattr(ollama_module.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(anthropic_module.httpx, "AsyncClient", _FakeAsyncClient)
    yield


# --------------------------------------------------------------------- Ollama: chat_stream

@pytest.mark.asyncio
async def test_ollama_chat_stream_yields_text_and_finish():
    """Two JSONL deltas + a final done frame should produce text deltas
    plus usage + finish chunks."""
    _FakeAsyncClient.queue_stream.append(_FakeStreamResponse(status_code=200, lines=[
        json.dumps({"message": {"content": "Hel"}}),
        json.dumps({"message": {"content": "lo"}}),
        json.dumps({
            "message": {"content": ""},
            "done": True, "done_reason": "stop",
            "prompt_eval_count": 7, "eval_count": 4,
        }),
        "",  # blank line — should be skipped
    ]))

    provider = OllamaProvider(base_url="http://ollama:11434")
    chunks = []
    async for chunk in provider.chat_stream(
        [ChatMessage(role="user", content="hi")], model="llama3", temperature=0.1, max_tokens=64,
    ):
        chunks.append(chunk)

    text = "".join(c.text or "" for c in chunks if c.type == "text_delta")
    assert text == "Hello"
    assert any(c.type == "usage" and c.usage and c.usage.input_tokens == 7 for c in chunks)
    assert any(c.type == "finish" and c.finish_reason == "stop" for c in chunks)

    # Verify the request payload picked up our temperature + num_predict.
    _, payload = _FakeAsyncClient.posted[-1]
    assert payload["options"]["temperature"] == 0.1
    assert payload["options"]["num_predict"] == 64


@pytest.mark.asyncio
async def test_ollama_chat_stream_emits_tool_call_events():
    """A streaming tool_calls payload should produce start/args/end chunks."""
    _FakeAsyncClient.queue_stream.append(_FakeStreamResponse(status_code=200, lines=[
        json.dumps({
            "message": {
                "content": "",
                "tool_calls": [
                    {"function": {"name": "list_accounts", "arguments": {"limit": 5}}},
                ],
            },
        }),
        json.dumps({"message": {"content": ""}, "done": True, "done_reason": "tool_calls"}),
    ]))

    provider = OllamaProvider()
    types = []
    async for c in provider.chat_stream([ChatMessage(role="user", content="x")], model="llama3"):
        types.append(c.type)
    assert "tool_call_start" in types
    assert "tool_call_args_delta" in types
    assert "tool_call_end" in types


@pytest.mark.asyncio
async def test_ollama_chat_stream_raises_on_http_error():
    _FakeAsyncClient.queue_stream.append(_FakeStreamResponse(
        status_code=503, lines=[], body=b"unavailable",
    ))
    provider = OllamaProvider()
    with pytest.raises(LLMUnavailableError):
        async for _ in provider.chat_stream(
            [ChatMessage(role="user", content="x")], model="llama3",
        ):
            pass


@pytest.mark.asyncio
async def test_ollama_chat_stream_includes_tools_in_payload():
    """When tools are passed, they must show up in the request body."""
    _FakeAsyncClient.queue_stream.append(_FakeStreamResponse(status_code=200, lines=[
        json.dumps({"message": {"content": ""}, "done": True, "done_reason": "stop"}),
    ]))
    provider = OllamaProvider()
    tools = [ToolDefinition(name="t", description="d", parameters={"type": "object"})]
    async for _ in provider.chat_stream(
        [ChatMessage(role="user", content="x")], model="llama3", tools=tools,
    ):
        pass
    _, payload = _FakeAsyncClient.posted[-1]
    assert payload["tools"][0]["function"]["name"] == "t"


# --------------------------------------------------------------------- Ollama: local-model settings

def _done_frame() -> _FakeStreamResponse:
    return _FakeStreamResponse(status_code=200, lines=[
        json.dumps({"message": {"content": ""}, "done": True, "done_reason": "stop"}),
    ])


async def _run_ollama_once() -> dict:
    provider = OllamaProvider()
    async for _ in provider.chat_stream([ChatMessage(role="user", content="x")], model="m"):
        pass
    _, payload = _FakeAsyncClient.posted[-1]
    return payload


@pytest.mark.asyncio
async def test_ollama_chat_stream_omits_think_num_ctx_keep_alive_by_default():
    """With the settings at their defaults the payload must not carry
    `think`, `keep_alive` or `options.num_ctx` — the server decides."""
    _FakeAsyncClient.queue_stream.append(_done_frame())
    payload = await _run_ollama_once()
    assert "think" not in payload
    assert "keep_alive" not in payload
    assert "num_ctx" not in payload["options"]


@pytest.mark.asyncio
async def test_ollama_chat_stream_sends_think_level_num_ctx_and_keep_alive(monkeypatch):
    s = get_agent_settings()
    monkeypatch.setattr(s, "ollama_think", "low")
    monkeypatch.setattr(s, "ollama_num_ctx", 32768)
    monkeypatch.setattr(s, "ollama_keep_alive", "30m")
    _FakeAsyncClient.queue_stream.append(_done_frame())
    payload = await _run_ollama_once()
    assert payload["think"] == "low"
    assert payload["options"]["num_ctx"] == 32768
    assert payload["keep_alive"] == "30m"


@pytest.mark.asyncio
async def test_ollama_chat_stream_think_false_is_sent_as_bool(monkeypatch):
    """qwen3-class models need a literal JSON false to stop thinking."""
    monkeypatch.setattr(get_agent_settings(), "ollama_think", "false")
    _FakeAsyncClient.queue_stream.append(_done_frame())
    payload = await _run_ollama_once()
    assert payload["think"] is False


@pytest.mark.asyncio
async def test_ollama_chat_stream_sends_format_and_reasoning_override(monkeypatch):
    """A per-call `reasoning` wins over AGENTS_OLLAMA_THINK and a JSON schema
    goes out as Ollama's `format` (grammar-constrained output)."""
    monkeypatch.setattr(get_agent_settings(), "ollama_think", "medium")
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}
    _FakeAsyncClient.queue_stream.append(_done_frame())
    provider = OllamaProvider()
    async for _ in provider.chat_stream(
        [ChatMessage(role="user", content="x")], model="gpt-oss:20b", response_format=schema, reasoning="low"
    ):
        pass
    _, payload = _FakeAsyncClient.posted[-1]
    assert payload["think"] == "low"
    assert payload["format"] == schema
    assert "tools" not in payload


@pytest.mark.asyncio
async def test_ollama_reasoning_none_sends_think_false():
    _FakeAsyncClient.queue_stream.append(_done_frame())
    provider = OllamaProvider()
    async for _ in provider.chat_stream([ChatMessage(role="user", content="x")], model="qwen3:8b", reasoning="none"):
        pass
    _, payload = _FakeAsyncClient.posted[-1]
    assert payload["think"] is False


@pytest.mark.asyncio
async def test_ollama_omits_format_when_not_requested(monkeypatch):
    monkeypatch.setattr(get_agent_settings(), "ollama_think", "medium")
    _FakeAsyncClient.queue_stream.append(_done_frame())
    payload = await _run_ollama_once()
    assert "format" not in payload
    assert payload["think"] == "medium"  # env default still applies without a per-call override


@pytest.mark.asyncio
async def test_ollama_chat_stream_drops_thinking_deltas():
    """Reasoning streamed in `message.thinking` is never surfaced as text."""
    _FakeAsyncClient.queue_stream.append(_FakeStreamResponse(status_code=200, lines=[
        json.dumps({"message": {"thinking": "let me think", "content": ""}}),
        json.dumps({"message": {"thinking": " harder", "content": ""}}),
        json.dumps({"message": {"content": "42"}}),
        json.dumps({"message": {"content": ""}, "done": True, "done_reason": "stop"}),
    ]))
    provider = OllamaProvider()
    text = ""
    async for c in provider.chat_stream([ChatMessage(role="user", content="x")], model="m"):
        if c.type == "text_delta":
            text += c.text or ""
    assert text == "42"


@pytest.mark.asyncio
async def test_chat_timeout_comes_from_settings(monkeypatch):
    monkeypatch.setattr(get_agent_settings(), "llm_timeout_seconds", 300.0)

    _FakeAsyncClient.queue_stream.append(_done_frame())
    await _run_ollama_once()
    assert _FakeAsyncClient.init_kwargs["timeout"].read == 300.0
    assert _FakeAsyncClient.init_kwargs["timeout"].connect == 10.0

    sse = [f"data: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {}})}"]
    _FakeAsyncClient.queue_stream.append(_FakeStreamResponse(status_code=200, lines=sse))
    provider = AnthropicProvider(api_key="sk-test")
    async for _ in provider.chat_stream([ChatMessage(role="user", content="x")], model="claude-x"):
        pass
    assert _FakeAsyncClient.init_kwargs["timeout"].read == 300.0


# --------------------------------------------------------------------- Ollama: embed

@pytest.mark.asyncio
async def test_ollama_embed_returns_vectors_per_input():
    _FakeAsyncClient.queue_post.append(_FakeResponse(status_code=200, json_body={
        "embeddings": [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]],
    }))
    provider = OllamaProvider()
    out = await provider.embed(["a", "b"], model="nomic-embed-text")
    assert out == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]


@pytest.mark.asyncio
async def test_ollama_embed_handles_missing_embeddings_array():
    """If the server returns an unexpected shape, we should yield [] rather than crash."""
    _FakeAsyncClient.queue_post.append(_FakeResponse(status_code=200, json_body={"unexpected": True}))
    provider = OllamaProvider()
    out = await provider.embed(["a"], model="nomic-embed-text")
    assert out == []


@pytest.mark.asyncio
async def test_ollama_embed_raises_on_http_error():
    _FakeAsyncClient.queue_post.append(_FakeResponse(status_code=500, text="boom"))
    provider = OllamaProvider()
    with pytest.raises(LLMUnavailableError):
        await provider.embed(["a"], model="nomic-embed-text")


@pytest.mark.asyncio
async def test_ollama_embed_sends_bounded_num_ctx_by_default():
    """Without num_ctx Ollama sizes the embedder's KV cache to the server default
    (32K on a large GPU), costing gigabytes for a 0.6B model."""
    _FakeAsyncClient.queue_post.append(_FakeResponse(status_code=200, json_body={"embeddings": [[0.1]]}))
    await OllamaProvider().embed(["a"], model="qwen3-embedding:0.6b")
    url, payload = _FakeAsyncClient.posted[-1]
    assert url.endswith("/api/embed")
    assert payload["input"] == ["a"]
    assert payload["options"] == {"num_ctx": 2048}


@pytest.mark.asyncio
async def test_ollama_embed_omits_num_ctx_when_disabled(monkeypatch):
    monkeypatch.setattr(get_agent_settings(), "ollama_embed_num_ctx", 0)
    _FakeAsyncClient.queue_post.append(_FakeResponse(status_code=200, json_body={"embeddings": [[0.1]]}))
    await OllamaProvider().embed(["a"], model="qwen3-embedding:0.6b")
    _, payload = _FakeAsyncClient.posted[-1]
    assert "options" not in payload


# --------------------------------------------------------------------- Anthropic: chat_stream

@pytest.mark.asyncio
async def test_anthropic_chat_stream_parses_text_block_sequence():
    sse = [
        f"data: {json.dumps({'type': 'message_start', 'message': {'usage': {'input_tokens': 5, 'output_tokens': 0}}})}",
        f"data: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text'}})}",
        f"data: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'Hi'}})}",
        f"data: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': '!'}})}",
        f"data: {json.dumps({'type': 'content_block_stop', 'index': 0})}",
        f"data: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 2}})}",
        "",  # blank — skipped
        "non-sse line — should be ignored",  # not 'data:' prefixed
    ]
    _FakeAsyncClient.queue_stream.append(_FakeStreamResponse(status_code=200, lines=sse))

    provider = AnthropicProvider(api_key="sk-test")
    chunks = []
    async for c in provider.chat_stream([ChatMessage(role="user", content="hi")], model="claude-x"):
        chunks.append(c)

    text = "".join(c.text or "" for c in chunks if c.type == "text_delta")
    assert text == "Hi!"
    assert any(c.type == "usage" and c.usage is not None and c.usage.output_tokens == 2 for c in chunks)
    assert any(c.type == "finish" and c.finish_reason == "end_turn" for c in chunks)


@pytest.mark.asyncio
async def test_anthropic_chat_stream_parses_tool_use_block():
    # Build SSE lines via explicit json.dumps payloads — avoids backslash
    # escapes inside f-strings, which Python 3.11 doesn't allow.
    events = [
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "tool_use", "id": "toolu_1", "name": "list_accounts"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": '{"limit":'}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": "5}"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta",
         "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1}},
    ]
    sse = ["data: " + json.dumps(e) for e in events]
    _FakeAsyncClient.queue_stream.append(_FakeStreamResponse(status_code=200, lines=sse))

    provider = AnthropicProvider(api_key="sk-test")
    chunks = []
    async for c in provider.chat_stream([ChatMessage(role="user", content="x")], model="claude-x"):
        chunks.append(c)

    starts = [c for c in chunks if c.type == "tool_call_start"]
    args = [c for c in chunks if c.type == "tool_call_args_delta"]
    ends = [c for c in chunks if c.type == "tool_call_end"]
    assert starts and starts[0].tool_name == "list_accounts"
    assert "".join(c.args_delta or "" for c in args) == '{"limit":5}'
    assert ends and ends[0].tool_call_id == "toolu_1"


@pytest.mark.asyncio
async def test_anthropic_includes_system_and_tools_in_payload():
    sse = [f"data: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {}})}"]
    _FakeAsyncClient.queue_stream.append(_FakeStreamResponse(status_code=200, lines=sse))
    provider = AnthropicProvider(api_key="sk-test")
    tools = [ToolDefinition(name="t", description="d", parameters={"type": "object"})]
    msgs = [
        ChatMessage(role="system", content="part1"),
        ChatMessage(role="system", content="part2"),
        ChatMessage(role="user", content="hi"),
    ]
    async for _ in provider.chat_stream(msgs, model="claude-x", tools=tools, max_tokens=200):
        pass
    _, payload = _FakeAsyncClient.posted[-1]
    assert payload["system"] == "part1\n\npart2"
    assert payload["max_tokens"] == 200
    assert payload["tools"][0]["name"] == "t"


# --------------------------------------------------------------------- Anthropic: HTTP errors → typed exceptions

@pytest.mark.asyncio
@pytest.mark.parametrize("status,exc_type", [
    (401, LLMAuthError),
    (403, LLMAuthError),
    (429, LLMRateLimitError),
    (500, LLMUnavailableError),
    (502, LLMUnavailableError),
    (400, LLMUnavailableError),
])
async def test_anthropic_http_errors_map_to_typed_exceptions(status, exc_type):
    _FakeAsyncClient.queue_stream.append(_FakeStreamResponse(
        status_code=status, lines=[], body=b'{"error":{"message":"x"}}',
    ))
    provider = AnthropicProvider(api_key="sk-test")
    with pytest.raises(exc_type):
        async for _ in provider.chat_stream([ChatMessage(role="user", content="x")], model="claude-x"):
            pass


@pytest.mark.asyncio
async def test_anthropic_embed_raises_not_supported():
    provider = AnthropicProvider(api_key="sk-test")
    with pytest.raises(LLMNotSupportedError):
        await provider.embed(["a"], model="x")


# --------------------------------------------------------------------- Ollama: tool-call parse retry

class _RawStream:
    """Minimal stand-in: a status, a body for aread(), and JSONL lines to stream."""

    def __init__(self, status_code: int, *, body: bytes = b"", lines=None):
        self.status_code = status_code
        self._body = body
        self._lines = list(lines or [])

    async def aread(self) -> bytes:
        return self._body

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln


@pytest.mark.asyncio
async def test_ollama_retries_once_when_a_tool_call_cannot_be_parsed(monkeypatch):
    """gpt-oss sometimes prefixes the tool-call JSON with a stray word; Ollama answers
    500 'error parsing tool call' before any token. One retry at reasoning=medium."""
    monkeypatch.setattr(get_agent_settings(), "ollama_think", "low")
    err = b'{"error":"error parsing tool call: raw=\'Need August.{\\"from_date\\":\\"2026-08-01\\"}\', err=invalid character"}'
    ok = [json.dumps({"message": {"role": "assistant", "content": "done"}, "done": True, "done_reason": "stop",
                      "prompt_eval_count": 10, "eval_count": 2})]
    _FakeAsyncClient.queue_stream.append(_RawStream(500, body=err))
    _FakeAsyncClient.queue_stream.append(_RawStream(200, lines=ok))
    chunks = [c async for c in OllamaProvider().chat_stream([ChatMessage(role="user", content="x")], model="gpt-oss:20b")]
    assert [c.type for c in chunks][0] == "text_delta" and chunks[-1].type == "finish"
    assert len(_FakeAsyncClient.posted) == 2
    first, second = _FakeAsyncClient.posted[0][1], _FakeAsyncClient.posted[1][1]
    assert first["think"] == "low" and second["think"] == "medium"


@pytest.mark.asyncio
async def test_ollama_does_not_retry_twice_or_on_other_errors():
    err = b'{"error":"error parsing tool call: raw=..."}'
    _FakeAsyncClient.queue_stream.append(_RawStream(500, body=err))
    _FakeAsyncClient.queue_stream.append(_RawStream(500, body=err))
    with pytest.raises(LLMUnavailableError):
        async for _ in OllamaProvider().chat_stream([ChatMessage(role="user", content="x")], model="m"):
            pass
    assert len(_FakeAsyncClient.posted) == 2  # exactly one retry
    _FakeAsyncClient.posted.clear()
    _FakeAsyncClient.queue_stream.append(_RawStream(500, body=b'{"error":"model not found"}'))
    with pytest.raises(LLMUnavailableError):
        async for _ in OllamaProvider().chat_stream([ChatMessage(role="user", content="x")], model="m"):
            pass
    assert len(_FakeAsyncClient.posted) == 1  # no retry for unrelated errors
