"""Provider abstraction tests.

We don't talk to real LLM providers in unit tests. Instead:
  - Test the wire serializers (messages, tools) for OpenAI and Anthropic.
  - Test the registry's build_provider dispatch.
  - Test the high-level chat() helper assembles streaming chunks correctly.
"""
import json
from typing import AsyncIterator

import pytest

from app.agents.providers import build_provider, list_providers
from app.agents.providers.anthropic import (
    _serialize_messages as anthropic_serialize,
    _serialize_tools as anthropic_tools,
    _split_system,
)
from app.agents.providers.base import (
    ChatChunk,
    ChatMessage,
    ChatResponse,
    LLMProvider,
    ToolCall,
    ToolDefinition,
    Usage,
)
from app.agents.providers.openai import (
    _serialize_messages as openai_serialize,
    _serialize_tools as openai_tools,
    normalize_openai_base_url,
)


pytestmark = pytest.mark.asyncio


def test_registry_lists_all_four_providers():
    assert set(list_providers()) == {"ollama", "openai", "anthropic", "openai_compatible"}


def test_registry_unknown_raises():
    with pytest.raises(ValueError):
        build_provider("does_not_exist")


def test_registry_build_openai_compat_requires_base_url():
    # build_provider strips falsy base_url so the constructor's required
    # kwarg is missing → TypeError. If supplied as empty string directly,
    # the constructor raises ValueError. Either is acceptable as a refusal.
    with pytest.raises((ValueError, TypeError)):
        build_provider("openai_compatible", base_url="")


def test_openai_serialization_basic_message():
    out = openai_serialize([ChatMessage(role="user", content="hi")])
    assert out == [{"role": "user", "content": "hi"}]


def test_openai_serialization_assistant_with_tool_calls():
    msg = ChatMessage(
        role="assistant",
        content=None,
        tool_calls=[ToolCall(id="t1", name="list_accounts", arguments={"x": 1})],
    )
    out = openai_serialize([msg])
    assert out[0]["tool_calls"][0]["id"] == "t1"
    assert out[0]["tool_calls"][0]["function"]["name"] == "list_accounts"
    args = json.loads(out[0]["tool_calls"][0]["function"]["arguments"])
    assert args == {"x": 1}


def test_openai_serialization_tool_result():
    msg = ChatMessage(role="tool", content="42 transactions", tool_call_id="t1")
    out = openai_serialize([msg])
    assert out[0]["role"] == "tool"
    assert out[0]["tool_call_id"] == "t1"


def test_openai_tools_payload_shape():
    defs = [ToolDefinition(name="list_accounts", description="d", parameters={"type": "object"})]
    out = openai_tools(defs)
    assert out and out[0]["type"] == "function"
    assert out[0]["function"]["name"] == "list_accounts"


def test_anthropic_split_system_extracts_system_messages():
    sys, rest = _split_system([
        ChatMessage(role="system", content="primary instructions"),
        ChatMessage(role="user", content="hello"),
    ])
    assert sys == "primary instructions"
    assert len(rest) == 1
    assert rest[0].role == "user"


def test_anthropic_serializes_tool_use_blocks():
    msg = ChatMessage(
        role="assistant",
        content="thinking",
        tool_calls=[ToolCall(id="toolu_1", name="get_dashboard_snapshot", arguments={})],
    )
    out = anthropic_serialize([msg])
    blocks = out[0]["content"]
    kinds = [b["type"] for b in blocks]
    assert "text" in kinds and "tool_use" in kinds
    tu = next(b for b in blocks if b["type"] == "tool_use")
    assert tu["id"] == "toolu_1" and tu["name"] == "get_dashboard_snapshot"


def test_anthropic_tool_result_block():
    msg = ChatMessage(role="tool", content="ok", tool_call_id="toolu_1")
    out = anthropic_serialize([msg])
    blk = out[0]["content"][0]
    assert blk["type"] == "tool_result"
    assert blk["tool_use_id"] == "toolu_1"


@pytest.mark.parametrize("raw,expected", [
    ("http://192.168.1.142:1234", "http://192.168.1.142:1234/v1"),
    ("http://lmstudio:1234/", "http://lmstudio:1234/v1"),
    ("http://lmstudio:1234/v1", "http://lmstudio:1234/v1"),
    ("http://lmstudio:1234/v1/", "http://lmstudio:1234/v1"),
    ("https://api.openai.com/v1", "https://api.openai.com/v1"),
    ("https://api.groq.com/openai/v1", "https://api.groq.com/openai/v1"),
    ("https://api.example.com/v2", "https://api.example.com/v2"),
    ("https://api.example.com/beta", "https://api.example.com/beta"),
    ("https://api.example.com/", "https://api.example.com/v1"),
    ("", ""),
])
def test_normalize_openai_base_url(raw, expected):
    assert normalize_openai_base_url(raw) == expected


def test_openai_provider_normalizes_base_url_at_construction():
    from app.agents.providers.openai import OpenAIProvider

    p = OpenAIProvider(api_key="x", base_url="http://lmstudio:1234")
    assert p.base_url == "http://lmstudio:1234/v1"


def test_anthropic_tools_payload_shape():
    defs = [ToolDefinition(name="search_all", description="d", parameters={"type": "object"})]
    out = anthropic_tools(defs)
    assert out and out[0]["name"] == "search_all"
    assert out[0]["input_schema"] == {"type": "object"}


async def test_openai_parser_handles_index_keyed_tool_call_chunks():
    """Regression: OpenAI streams tool calls with `id` and `name` only in
    the FIRST chunk for an `index`; subsequent chunks have just args.
    Earlier code keyed by id (falling back to `_idx_N` when missing),
    creating phantom entries with empty names — dispatch then failed with
    `unknown tool: `."""
    from unittest.mock import patch
    from app.agents.providers.base import ChatMessage
    from app.agents.providers.openai import OpenAIProvider

    sse_chunks = [
        b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_abc","function":{"name":"propose_categorize","arguments":""}}]}}]}\n',
        b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"transaction_ids\\":"}}]}}]}\n',
        b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"[\\"abc\\"],\\"category_id\\":\\"def\\"}"}}]}}]}\n',
        b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":12,"completion_tokens":34}}\n',
        b'data: [DONE]\n',
    ]

    class _StreamResp:
        status_code = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *_a): pass
        async def aiter_lines(self):
            for line in sse_chunks:
                yield line.decode().rstrip("\n")

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *_a): pass
        def stream(self, *_a, **_kw): return _StreamResp()

    p = OpenAIProvider(api_key="x", base_url="http://lmstudio:1234")
    chunks: list[ChatChunk] = []
    with patch("httpx.AsyncClient", return_value=_Client()):
        async for c in p.chat_stream([ChatMessage(role="user", content="hi")], model="m"):
            chunks.append(c)

    starts = [c for c in chunks if c.type == "tool_call_start"]
    assert len(starts) == 1, "should emit exactly one tool_call_start per index"
    assert starts[0].tool_name == "propose_categorize"
    assert starts[0].tool_call_id == "call_abc"

    arg_deltas = [
        c.args_delta for c in chunks
        if c.type == "tool_call_args_delta"
        and c.args_delta is not None
    ]
    assert "".join(arg_deltas) == '{"transaction_ids":["abc"],"category_id":"def"}'

    ends = [c for c in chunks if c.type == "tool_call_end"]
    assert len(ends) == 1 and ends[0].tool_call_id == "call_abc"


@pytest.mark.asyncio
async def test_openai_chat_timeout_comes_from_settings(monkeypatch):
    """The OpenAI-compatible path shares AGENTS_LLM_TIMEOUT_SECONDS."""
    from unittest.mock import patch
    from app.agents.config import get_agent_settings
    from app.agents.providers.openai import OpenAIProvider

    monkeypatch.setattr(get_agent_settings(), "llm_timeout_seconds", 300.0)

    class _StreamResp:
        status_code = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *_a): pass
        async def aiter_lines(self):
            yield 'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}'
            yield "data: [DONE]"

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *_a): pass
        def stream(self, *_a, **_kw): return _StreamResp()

    seen: dict = {}

    def _factory(**kwargs):
        seen.update(kwargs)
        return _Client()

    p = OpenAIProvider(api_key="x", base_url="http://lmstudio:1234")
    with patch("httpx.AsyncClient", side_effect=_factory):
        async for _ in p.chat_stream([ChatMessage(role="user", content="hi")], model="m"):
            pass
    assert seen["timeout"].read == 300.0
    assert seen["timeout"].connect == 10.0


@pytest.mark.asyncio
async def test_openai_payload_carries_json_schema_and_reasoning_effort_for_reasoning_models():
    """`response_format` becomes OpenAI's strict json_schema mode; `reasoning`
    becomes `reasoning_effort` only for models that accept it (o*, gpt-5*)."""
    from unittest.mock import patch
    from app.agents.providers.openai import OpenAIProvider, _is_reasoning_model

    assert _is_reasoning_model("gpt-5-mini") and _is_reasoning_model("o3") and not _is_reasoning_model("gpt-4o-mini")

    class _StreamResp:
        status_code = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *_a): pass
        async def aiter_lines(self):
            yield 'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}'
            yield "data: [DONE]"

    posted: list[dict] = []

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *_a): pass
        def stream(self, _method, _url, *, json=None, headers=None):
            posted.append(json or {})
            return _StreamResp()

    schema = {"title": "route", "type": "object", "properties": {"intent": {"type": "string"}}, "required": ["intent"], "additionalProperties": False}
    p = OpenAIProvider(api_key="x", base_url="http://lmstudio:1234")
    with patch("httpx.AsyncClient", return_value=_Client()):
        async for _ in p.chat_stream([ChatMessage(role="user", content="hi")], model="gpt-4o-mini", response_format=schema, reasoning="low"):
            pass
        async for _ in p.chat_stream([ChatMessage(role="user", content="hi")], model="gpt-5-mini", reasoning="none"):
            pass
        async for _ in p.chat_stream([ChatMessage(role="user", content="hi")], model="gpt-4o-mini"):
            pass

    first, second, third = posted
    assert first["response_format"] == {"type": "json_schema", "json_schema": {"name": "route", "schema": schema, "strict": True}}
    assert "reasoning_effort" not in first  # gpt-4o-mini rejects it
    assert "response_format" not in second and second["reasoning_effort"] == "low"  # "none" maps to low on OpenAI
    assert "response_format" not in third and "reasoning_effort" not in third


@pytest.mark.asyncio
async def test_chat_helper_does_not_forward_unset_structured_kwargs():
    """A provider written against the old signature (no response_format /
    reasoning kwargs) still works through the aggregating chat() helper."""

    class _Legacy(LLMProvider):
        name = "legacy"

        def __init__(self):
            super().__init__()
            self.seen: list[dict] = []

        async def chat_stream(  # type: ignore[override]
            self, messages, *, model, tools=None, temperature=0.4, max_tokens=None
        ) -> AsyncIterator[ChatChunk]:
            self.seen.append({"tools": tools, "max_tokens": max_tokens})
            yield ChatChunk(type="text_delta", text="ok")
            yield ChatChunk(type="finish", finish_reason="stop")

        async def embed(self, texts, *, model):
            return []

    p = _Legacy()
    res = await p.chat([ChatMessage(role="user", content="hi")], model="m", max_tokens=5)
    assert res.content == "ok" and p.seen == [{"tools": None, "max_tokens": 5}]

    class _Modern(_Legacy):
        async def chat_stream(  # type: ignore[override]
            self, messages, *, model, tools=None, temperature=0.4, max_tokens=None, response_format=None, reasoning=None
        ) -> AsyncIterator[ChatChunk]:
            self.seen.append({"response_format": response_format, "reasoning": reasoning})
            yield ChatChunk(type="finish", finish_reason="stop")

    m = _Modern()
    await m.chat([ChatMessage(role="user", content="hi")], model="m", response_format={"type": "object"}, reasoning="low")
    assert m.seen == [{"response_format": {"type": "object"}, "reasoning": "low"}]


@pytest.mark.asyncio
async def test_anthropic_accepts_and_ignores_response_format():
    """Anthropic has no JSON-schema mode in this client: the kwargs are
    accepted for parity and leave the payload untouched."""
    from unittest.mock import patch
    from app.agents.providers.anthropic import AnthropicProvider

    class _StreamResp:
        status_code = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *_a): pass
        async def aiter_lines(self):
            yield 'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{}}'

    posted: list[dict] = []

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *_a): pass
        def stream(self, _method, _url, *, json=None, headers=None):
            posted.append(json or {})
            return _StreamResp()

    p = AnthropicProvider(api_key="sk-test")
    with patch("httpx.AsyncClient", return_value=_Client()):
        async for _ in p.chat_stream(
            [ChatMessage(role="user", content="hi")], model="claude-x",
            response_format={"type": "object"}, reasoning="high",
        ):
            pass
    assert len(posted) == 1
    assert "response_format" not in posted[0] and "format" not in posted[0] and "reasoning" not in posted[0]
    assert "reasoning_effort" not in posted[0]


# --- chat() helper assembly -------------------------------------------------

class _ScriptedProvider(LLMProvider):
    name = "scripted"

    def __init__(self, chunks: list[ChatChunk]):
        super().__init__()
        self._chunks = chunks

    async def chat_stream(  # type: ignore[override]
        self, messages, *, model, tools=None, temperature=0.4, max_tokens=None
    ) -> AsyncIterator[ChatChunk]:
        for c in self._chunks:
            yield c

    async def embed(self, texts, *, model):
        return [[0.1] * 4 for _ in texts]


async def test_chat_helper_aggregates_text_and_tool_calls():
    p = _ScriptedProvider([
        ChatChunk(type="text_delta", text="Hello "),
        ChatChunk(type="text_delta", text="world"),
        ChatChunk(type="tool_call_start", tool_call_id="t1", tool_name="list_accounts"),
        ChatChunk(type="tool_call_args_delta", tool_call_id="t1", args_delta='{"x": 1}'),
        ChatChunk(type="tool_call_end", tool_call_id="t1"),
        ChatChunk(type="usage", usage=Usage(input_tokens=12, output_tokens=4)),
        ChatChunk(type="finish", finish_reason="tool_calls"),
    ])
    res = await p.chat([ChatMessage(role="user", content="hi")], model="m")
    assert isinstance(res, ChatResponse)
    assert res.content == "Hello world"
    assert res.finish_reason == "tool_calls"
    assert res.usage.input_tokens == 12
    assert len(res.tool_calls) == 1
    assert res.tool_calls[0].name == "list_accounts"
    assert res.tool_calls[0].arguments == {"x": 1}
