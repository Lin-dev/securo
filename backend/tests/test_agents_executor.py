"""End-to-end-style executor tests with a scripted LLM and a fake MCP.

These exercise the full agent runtime — turn loop, tool dispatch, message
persistence, usage logging, error classification — without ever hitting a
real LLM or MCP server.

Pattern:
  - `_ScriptedProvider` yields a predetermined sequence of ChatChunks.
  - `_FakeMCP` exposes one synthetic tool and records calls.
  - We patch `_provider_for` and pass our fake MCP into AgentExecutor.
"""
import tempfile
import uuid
from typing import AsyncIterator
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.agents.mcp.client import MCPRegistry, ToolHandle
from app.agents.models.agent import Agent
from app.agents.models.conversation import Conversation, Message
from app.agents.models.usage import LlmUsage
from app.agents.providers.base import (
    ChatChunk,
    ChatMessage,
    LLMAuthError,
    LLMProvider,
    ToolCall,
    Usage,
)
from app.agents.runtime.executor import AgentExecutor, ExecutorEvent, _format_pinned_context


pytestmark = pytest.mark.asyncio


# --- Fakes -----------------------------------------------------------------


class _ScriptedProvider(LLMProvider):
    """Yields chunks from a list of "turns". Each turn is a list of chunks
    that simulate one model round. The provider iterates turns across
    successive chat_stream() calls."""

    name = "openai"  # match cost-table key for usage tests

    def __init__(self, turns: list[list[ChatChunk]]):
        super().__init__(api_key="x")
        self._turns = list(turns)
        # One entry per chat_stream() call: the tool list plus any structured
        # output / reasoning kwargs, so tests can assert what was requested.
        self.calls: list[dict] = []

    async def chat_stream(  # type: ignore[override]
        self, messages, *, model, tools=None, temperature=0.4, max_tokens=None, **kwargs
    ) -> AsyncIterator[ChatChunk]:
        self.calls.append({"model": model, "tools": tools, "temperature": temperature, "max_tokens": max_tokens, **kwargs})
        if not self._turns:
            # No more scripted turns — emit a generic finish.
            yield ChatChunk(type="finish", finish_reason="stop")
            return
        turn = self._turns.pop(0)
        for c in turn:
            yield c

    async def embed(self, texts, *, model):
        return [[0.0] * 4 for _ in texts]


class _FakeMCP(MCPRegistry):
    """In-process MCPRegistry that doesn't open any sockets. Discovers a
    single tool and records every call() invocation."""

    def __init__(self, *, tools: list[ToolHandle], canned_result: dict | None = None):
        # Skip parent __init__ so we don't try to construct real clients.
        self._tools = tools
        self._canned = canned_result or {"ok": True, "data": {"items": [{"hello": "world"}]}, "text": "ok"}
        self.calls: list[tuple[str, dict]] = []

    def server_names(self):
        return ["securo"]

    async def discover(self, *, workspace_id=None, user_id, conversation_id=None, agent_id=None):
        return list(self._tools)

    async def call(self, *, wire_name, arguments, workspace_id=None, user_id, conversation_id=None, agent_id=None):
        self.calls.append((wire_name, arguments))
        return self._canned


# --- Helpers --------------------------------------------------------------


def _patch_provider(p: LLMProvider):
    return patch("app.agents.runtime.executor._provider_for", return_value=p)


@pytest.fixture(autouse=True)
def _freeform_loop_only(monkeypatch):
    """These tests script the model's turns for the free-form loop. Guided
    mode (on by default) would spend the first scripted turn on the intent
    router, so it is switched off here; `test_agents_executor_workflows.py`
    covers the routing paths explicitly."""
    from app.agents.config import get_agent_settings

    monkeypatch.setattr(get_agent_settings(), "guided_mode", False)


async def _drain(executor: AgentExecutor, **kwargs) -> list[ExecutorEvent]:
    return [ev async for ev in executor.run(**kwargs)]


# --- Tests ----------------------------------------------------------------


async def test_simple_text_response_no_tools(session, test_user, test_agent: Agent, test_conversation: Conversation):
    """LLM returns a plain answer; no tool calls."""
    provider = _ScriptedProvider([[
        ChatChunk(type="text_delta", text="Hello "),
        ChatChunk(type="text_delta", text="world!"),
        ChatChunk(type="usage", usage=Usage(input_tokens=10, output_tokens=4)),
        ChatChunk(type="finish", finish_reason="stop"),
    ]])

    executor = AgentExecutor(mcp=_FakeMCP(tools=[]))
    with _patch_provider(provider):
        events = await _drain(
            executor,
            session=session,
            agent=test_agent,
            user_id=test_user.id,
            conversation_id=test_conversation.id,
            user_message="hi",
        )

    text_events = [e for e in events if e.type == "text_delta"]
    assert "".join(e.text or "" for e in text_events) == "Hello world!"
    assert events[-1].type == "done"
    assert events[-1].finish_reason == "stop"

    # Persisted: user msg + assistant msg.
    msgs = (await session.execute(
        select(Message).where(Message.conversation_id == test_conversation.id).order_by(Message.ordinal)
    )).scalars().all()
    roles = [m.role for m in msgs]
    assert roles == ["user", "assistant"]
    assert msgs[1].content == "Hello world!"
    assert msgs[1].input_tokens == 10
    assert msgs[1].output_tokens == 4


async def test_usage_row_recorded(session, test_user, test_agent, test_conversation):
    provider = _ScriptedProvider([[
        ChatChunk(type="text_delta", text="ok"),
        ChatChunk(type="usage", usage=Usage(input_tokens=100, output_tokens=20)),
        ChatChunk(type="finish", finish_reason="stop"),
    ]])
    executor = AgentExecutor(mcp=_FakeMCP(tools=[]))
    with _patch_provider(provider):
        await _drain(
            executor,
            session=session,
            agent=test_agent,
            user_id=test_user.id,
            conversation_id=test_conversation.id,
            user_message="hi",
        )
    rows = (await session.execute(
        select(LlmUsage).where(LlmUsage.conversation_id == test_conversation.id)
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].input_tokens == 100
    assert rows[0].output_tokens == 20
    assert rows[0].provider == "openai"
    # gpt-4o-mini rates: (0.15, 0.60) per 1M.
    assert rows[0].cost_usd is not None
    assert float(rows[0].cost_usd) > 0


async def test_tool_call_dispatch_and_result_persisted(session, test_user, test_agent, test_conversation):
    """LLM emits a tool call; executor runs it via fake MCP and feeds the
    result back; second turn returns a plain answer."""
    tools = [ToolHandle(server="securo", name="list_accounts", description="d", parameters={"type": "object"})]
    fake_mcp = _FakeMCP(tools=tools, canned_result={
        "ok": True, "data": {"items": [{"id": "a1", "name": "Checking"}]}, "text": "1 account",
    })

    provider = _ScriptedProvider([
        # Turn 1: emit a tool call.
        [
            ChatChunk(type="tool_call_start", tool_call_id="t1", tool_name="securo__list_accounts"),
            ChatChunk(type="tool_call_args_delta", tool_call_id="t1", args_delta="{}"),
            ChatChunk(type="tool_call_end", tool_call_id="t1"),
            ChatChunk(type="usage", usage=Usage(input_tokens=20, output_tokens=5)),
            ChatChunk(type="finish", finish_reason="tool_calls"),
        ],
        # Turn 2: respond with the result.
        [
            ChatChunk(type="text_delta", text="You have 1 account."),
            ChatChunk(type="usage", usage=Usage(input_tokens=30, output_tokens=8)),
            ChatChunk(type="finish", finish_reason="stop"),
        ],
    ])

    executor = AgentExecutor(mcp=fake_mcp)
    with _patch_provider(provider):
        events = await _drain(
            executor,
            session=session,
            agent=test_agent,
            user_id=test_user.id,
            conversation_id=test_conversation.id,
            user_message="how many accounts?",
        )

    # Event stream: tool_call → tool_result → text_delta → done.
    types = [e.type for e in events]
    assert "tool_call" in types
    assert "tool_result" in types
    assert types[-1] == "done"

    # MCP got called with the right wire name.
    assert fake_mcp.calls == [("securo__list_accounts", {})]

    # DB state: user, assistant(turn1, with tool_calls), tool, assistant(turn2, with text).
    msgs = (await session.execute(
        select(Message).where(Message.conversation_id == test_conversation.id).order_by(Message.ordinal)
    )).scalars().all()
    roles = [m.role for m in msgs]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert msgs[1].tool_calls and msgs[1].tool_calls[0]["name"] == "securo__list_accounts"
    assert msgs[3].content == "You have 1 account."

    # Two usage rows, one per provider call.
    usage_rows = (await session.execute(
        select(LlmUsage).where(LlmUsage.conversation_id == test_conversation.id)
    )).scalars().all()
    assert len(usage_rows) == 2


async def test_provider_auth_error_surfaced_as_friendly_message(
    session, test_user, test_agent, test_conversation
):
    """An LLMAuthError becomes an error event with code=auth, not a 500."""

    class _BoomProvider(LLMProvider):
        name = "openai"

        async def chat_stream(self, *args, **kwargs):  # type: ignore[override]
            raise LLMAuthError("missing key")
            yield  # unreachable, makes this a generator

        async def embed(self, texts, *, model):
            return []

    executor = AgentExecutor(mcp=_FakeMCP(tools=[]))
    with _patch_provider(_BoomProvider()):
        events = await _drain(
            executor,
            session=session,
            agent=test_agent,
            user_id=test_user.id,
            conversation_id=test_conversation.id,
            user_message="hi",
        )

    err = next((e for e in events if e.type == "error"), None)
    assert err is not None
    assert err.error_code == "auth"
    done = next((e for e in events if e.type == "done"), None)
    assert done is not None and done.finish_reason == "error"


async def test_no_model_configured_yields_config_error(session, test_user, test_conversation):
    """An agent without model and without AGENTS_DEFAULT_MODEL should bail
    early with a config error rather than calling the provider."""
    bare_agent = Agent(
        id=uuid.uuid4(),
        user_id=test_user.id,
        name="Bare",
        provider="openai",
        model=None,  # the missing piece
    )
    session.add(bare_agent)
    await session.commit()

    # Reuse the existing conversation but switch its agent to the bare one.
    test_conversation.agent_id = bare_agent.id
    await session.commit()

    executor = AgentExecutor(mcp=_FakeMCP(tools=[]))
    # Provider doesn't matter here — should never be called.
    provider = _ScriptedProvider([])
    with _patch_provider(provider), patch.dict("os.environ", {"AGENTS_DEFAULT_MODEL": ""}, clear=False):
        events = await _drain(
            executor,
            session=session,
            agent=bare_agent,
            user_id=test_user.id,
            conversation_id=test_conversation.id,
            user_message="hi",
        )
    err = next((e for e in events if e.type == "error"), None)
    assert err is not None and err.error_code == "config"


async def test_per_agent_tool_whitelist_filters_discovery(
    session, test_user, test_agent, test_conversation
):
    """If the agent has explicit tool whitelist rows, the executor only
    sends the enabled ones to the LLM."""
    from app.agents.services import agent_service

    # Discover 2 tools, but only enable one.
    tools = [
        ToolHandle(server="securo", name="list_accounts", description="", parameters={"type": "object"}),
        ToolHandle(server="securo", name="list_categories", description="", parameters={"type": "object"}),
    ]
    fake_mcp = _FakeMCP(tools=tools)
    await agent_service.replace_tool_enablement(
        session, test_agent.id,
        [("securo", "list_accounts", True), ("securo", "list_categories", False)],
    )

    captured_tool_names: list[str] = []

    class _Capture(_ScriptedProvider):
        async def chat_stream(self, messages, *, model, tools=None, temperature=0.4, max_tokens=None):
            captured_tool_names.extend([t.name for t in (tools or [])])
            async for c in super().chat_stream(messages, model=model, tools=tools, temperature=temperature, max_tokens=max_tokens):
                yield c

    provider = _Capture([[
        ChatChunk(type="text_delta", text="ok"),
        ChatChunk(type="finish", finish_reason="stop"),
    ]])

    executor = AgentExecutor(mcp=fake_mcp)
    with _patch_provider(provider):
        await _drain(
            executor,
            session=session,
            agent=test_agent,
            user_id=test_user.id,
            conversation_id=test_conversation.id,
            user_message="hi",
        )

    assert captured_tool_names == ["securo__list_accounts"]


async def test_tool_result_passed_to_llm_in_full(session, test_user, test_agent, test_conversation):
    """Regression: the executor used to truncate the tool result text to
    1500 chars before feeding it back to the model, causing the LLM to
    report "truncated" rows for any list response over a few items.
    The LLM must see the full structured payload."""
    big_items = [{"id": f"id-{i}", "description": f"Transaction number {i}", "amount": i * 10} for i in range(20)]
    fake_mcp = _FakeMCP(
        tools=[ToolHandle(server="securo", name="list_transactions", description="d", parameters={"type": "object"})],
        canned_result={
            "ok": True,
            "data": {"items": big_items, "total": 20},
            "text": "long unused text",
        },
    )

    captured_tool_messages: list[str] = []

    class _Capture(_ScriptedProvider):
        async def chat_stream(self, messages, *, model, tools=None, temperature=0.4, max_tokens=None):
            captured_tool_messages.extend([m.content for m in messages if m.role == "tool"])
            async for c in super().chat_stream(messages, model=model, tools=tools, temperature=temperature, max_tokens=max_tokens):
                yield c

    provider = _Capture([
        # Turn 1: emit a tool call.
        [
            ChatChunk(type="tool_call_start", tool_call_id="t1", tool_name="securo__list_transactions"),
            ChatChunk(type="tool_call_args_delta", tool_call_id="t1", args_delta="{}"),
            ChatChunk(type="tool_call_end", tool_call_id="t1"),
            ChatChunk(type="finish", finish_reason="tool_calls"),
        ],
        # Turn 2: text reply.
        [
            ChatChunk(type="text_delta", text="ok"),
            ChatChunk(type="finish", finish_reason="stop"),
        ],
    ])

    executor = AgentExecutor(mcp=fake_mcp)
    with _patch_provider(provider):
        await _drain(
            executor,
            session=session,
            agent=test_agent,
            user_id=test_user.id,
            conversation_id=test_conversation.id,
            user_message="list me everything",
        )

    assert captured_tool_messages, "executor should have fed a tool message into turn 2"
    last = captured_tool_messages[-1]
    # Every item must appear in the LLM-facing content — no silent truncation.
    for i in range(20):
        assert f"Transaction number {i}" in last, (
            f"item {i} missing from tool message of length {len(last)} — likely truncation regression"
        )


class _CaptureAll(_ScriptedProvider):
    """Scripted provider that records every message list it was given."""

    def __init__(self, scripts):
        super().__init__(scripts)
        self.seen: list[list] = []

    async def chat_stream(self, messages, *, model, tools=None, temperature=0.4, max_tokens=None):
        self.seen.append(list(messages))
        async for c in super().chat_stream(messages, model=model, tools=tools, temperature=temperature, max_tokens=max_tokens):
            yield c


def _tool_turn(name="securo__list_transactions"):
    return [
        ChatChunk(type="tool_call_start", tool_call_id="t1", tool_name=name),
        ChatChunk(type="tool_call_args_delta", tool_call_id="t1", args_delta="{}"),
        ChatChunk(type="tool_call_end", tool_call_id="t1"),
        ChatChunk(type="finish", finish_reason="tool_calls"),
    ]


async def test_tool_result_capped_keeps_json_and_marks_truncation(session, test_user, test_agent, test_conversation):
    """A 300-item result is cut to the longest list prefix that fits 4000
    chars, stays valid JSON, and says how much was left out — while the
    persisted tool_result keeps every item for the UI."""
    import json

    big_items = [{"id": f"id-{i}", "description": f"Transaction number {i}", "amount": i * 10} for i in range(300)]
    fake_mcp = _FakeMCP(
        tools=[ToolHandle(server="securo", name="list_transactions", description="d", parameters={"type": "object"})],
        canned_result={"ok": True, "data": {"items": big_items, "total": 300}, "text": "unused"},
    )
    provider = _CaptureAll([_tool_turn(), [ChatChunk(type="text_delta", text="ok"), ChatChunk(type="finish", finish_reason="stop")]])
    executor = AgentExecutor(mcp=fake_mcp)
    with _patch_provider(provider):
        await _drain(executor, session=session, agent=test_agent, user_id=test_user.id,
                     conversation_id=test_conversation.id, user_message="list everything")

    tool_msgs = [m.content for m in provider.seen[-1] if m.role == "tool"]
    assert len(tool_msgs) == 1
    body = json.loads(tool_msgs[0])
    assert len(tool_msgs[0]) <= 4000
    assert body["truncated"] is True
    assert body["total"] == 300
    assert body["omitted_items"] == 300 - len(body["items"])
    assert 1 <= len(body["items"]) < 300
    assert "hint" in body

    row = (await session.execute(
        select(Message).where(Message.conversation_id == test_conversation.id, Message.role == "tool")
    )).scalars().one()
    assert len(row.tool_result["data"]["items"]) == 300  # full payload persisted for the UI
    assert row.content == tool_msgs[0]


async def test_replay_recaps_full_tool_data(session, test_user, test_agent, test_conversation):
    """History rows written before the cap (or with a different cap) carry
    huge `content`; replay must re-cap from `tool_result.data`."""
    from app.agents.services import conversation_service

    items = [{"id": i, "note": "x" * 90} for i in range(300)]  # ~30K chars
    full = __import__("json").dumps({"items": items, "total": 300})
    await conversation_service.append_message(session, conversation_id=test_conversation.id, role="user", content="earlier")
    await conversation_service.append_message(
        session, conversation_id=test_conversation.id, role="assistant", content=None,
        tool_calls=[{"id": "old1", "name": "securo__list_transactions", "arguments": {}}],
    )
    await conversation_service.append_message(
        session, conversation_id=test_conversation.id, role="tool", content=full,
        tool_result={"tool_call_id": "old1", "name": "securo__list_transactions", "data": {"items": items, "total": 300}, "ok": True},
    )
    await conversation_service.append_message(session, conversation_id=test_conversation.id, role="assistant", content="done")

    provider = _CaptureAll([[ChatChunk(type="text_delta", text="ok"), ChatChunk(type="finish", finish_reason="stop")]])
    executor = AgentExecutor(mcp=_FakeMCP(tools=[]))
    with _patch_provider(provider):
        await _drain(executor, session=session, agent=test_agent, user_id=test_user.id,
                     conversation_id=test_conversation.id, user_message="and now?")

    tool_msgs = [m for m in provider.seen[0] if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert len(tool_msgs[0].content) <= 4000
    assert __import__("json").loads(tool_msgs[0].content)["truncated"] is True
    assert tool_msgs[0].tool_call_id == "old1"


def test_cap_tool_content_behaviour():
    import json
    from app.agents.runtime.executor import _cap_tool_content

    small = {"items": [{"a": 1}], "total": 1}
    assert _cap_tool_content(small, max_chars=4000) == json.dumps(small)
    assert _cap_tool_content(small, max_chars=0) == json.dumps(small)  # 0 = uncapped
    long_text = "y" * 5000
    capped = _cap_tool_content(long_text, max_chars=1000)
    assert len(capped) <= 1000 + 120 and "truncated" in capped  # string cut with marker
    nested = {"lanes": {"income": {"items": [{"v": i} for i in range(500)]}}}  # no top-level list → string cut
    out = _cap_tool_content(nested, max_chars=500)
    assert len(out) <= 620 and "truncated" in out


def test_trim_to_budget_drops_whole_old_turns_never_the_last_user():
    from app.agents.runtime.executor import _estimate_tokens, _trim_to_budget

    def msgs(pairs):
        out = [ChatMessage(role="system", content="guardrail " * 20), ChatMessage(role="system", content="persona")]
        for i in range(pairs):
            out.append(ChatMessage(role="user", content=f"q{i} " + "u" * 2000))
            out.append(ChatMessage(role="assistant", content=None, tool_calls=[ToolCall(id=f"c{i}", name="securo__x", arguments={})]))
            out.append(ChatMessage(role="tool", content="r" * 2000, tool_call_id=f"c{i}", name="securo__x"))
            out.append(ChatMessage(role="assistant", content=f"a{i} " + "a" * 500))
        out.append(ChatMessage(role="user", content="final question"))
        return out

    original = msgs(12)
    trimmed, dropped = _trim_to_budget(original, budget_tokens=3000)
    assert dropped > 0
    assert trimmed[0].role == "system" and trimmed[1].role == "system"
    assert trimmed[-1].content == "final question"
    users = [m for m in trimmed if m.role == "user"]
    assert 1 <= len(users) < 13
    # whole turns only: every remaining tool row still follows its assistant call
    for i, m in enumerate(trimmed):
        if m.role == "tool":
            assert trimmed[i - 1].role == "assistant" and trimmed[i - 1].tool_calls
    assert sum(map(_estimate_tokens, trimmed)) <= 3000 or len(users) == 1

    # An oversized final turn alone is never dropped.
    huge = [ChatMessage(role="system", content="s"), ChatMessage(role="user", content="x" * 40000)]
    kept, dropped = _trim_to_budget(huge, budget_tokens=100)
    assert kept == huge and dropped == 0
    # Budget off → untouched.
    assert _trim_to_budget(original, budget_tokens=0) == (original, 0)


async def test_history_trimmed_to_prompt_budget(session, test_user, test_agent, test_conversation, monkeypatch):
    from app.agents.config import get_agent_settings
    from app.agents.services import conversation_service

    monkeypatch.setattr(get_agent_settings(), "prompt_budget_tokens", 3000)
    for i in range(12):
        await conversation_service.append_message(session, conversation_id=test_conversation.id, role="user", content=f"q{i} " + "u" * 2000)
        await conversation_service.append_message(session, conversation_id=test_conversation.id, role="assistant", content=f"a{i} " + "a" * 2000)

    provider = _CaptureAll([[ChatChunk(type="text_delta", text="ok"), ChatChunk(type="finish", finish_reason="stop")]])
    executor = AgentExecutor(mcp=_FakeMCP(tools=[]))
    with _patch_provider(provider):
        await _drain(executor, session=session, agent=test_agent, user_id=test_user.id,
                     conversation_id=test_conversation.id, user_message="latest question")

    seen = provider.seen[0]
    assert seen[0].role == "system"
    assert seen[-1].role == "user" and seen[-1].content == "latest question"
    assert len([m for m in seen if m.role == "user"]) < 13
    assert all(m.role == "system" for m in seen[:2])


def _commentary_script():
    return [
        [
            ChatChunk(type="text_delta", text="Need August. "),
            ChatChunk(type="text_delta", text="Let's call the tool."),
            ChatChunk(type="tool_call_start", tool_call_id="t1", tool_name="securo__list_transactions"),
            ChatChunk(type="tool_call_args_delta", tool_call_id="t1", args_delta="{}"),
            ChatChunk(type="tool_call_end", tool_call_id="t1"),
            ChatChunk(type="finish", finish_reason="tool_calls"),
        ],
        [ChatChunk(type="text_delta", text="Here is the answer."), ChatChunk(type="finish", finish_reason="stop")],
    ]


async def test_commentary_discarded_when_turn_has_tool_calls(session, test_user, test_agent, test_conversation):
    """Text streamed in a turn that ends with tool calls is planning
    commentary: the UI is told to discard it, the persisted turn keeps only a
    short trace, and the model never sees it again."""
    fake_mcp = _FakeMCP(tools=[ToolHandle(server="securo", name="list_transactions", description="", parameters={"type": "object"})])
    provider = _CaptureAll(_commentary_script())
    executor = AgentExecutor(mcp=fake_mcp)
    with _patch_provider(provider):
        events = await _drain(executor, session=session, agent=test_agent, user_id=test_user.id,
                              conversation_id=test_conversation.id, user_message="compare months")

    kinds = [e.type for e in events]
    assert "text_discard" in kinds
    # The discard arrives after the commentary deltas and before the tool_call events.
    assert kinds.index("text_discard") > kinds.index("text_delta")
    assert kinds.index("text_discard") < kinds.index("tool_call")
    # The final answer is still streamed normally after the tool round.
    assert [e.text for e in events if e.type == "text_delta"][-1] == "Here is the answer."

    rows = (await session.execute(
        select(Message).where(Message.conversation_id == test_conversation.id).order_by(Message.ordinal)
    )).scalars().all()
    turn1 = next(r for r in rows if r.role == "assistant" and r.tool_calls)
    assert turn1.content is None
    assert turn1.tool_calls[0]["commentary"] == "Need August. Let's call the tool."
    assert turn1.tool_calls[0]["name"] == "securo__list_transactions"
    final = rows[-1]
    assert final.role == "assistant" and final.content == "Here is the answer."

    # Turn 2's message list carries the tool-calling assistant row with no text.
    turn2_assistant = [m for m in provider.seen[1] if m.role == "assistant" and m.tool_calls]
    assert turn2_assistant and turn2_assistant[-1].content is None


async def test_commentary_kept_when_setting_is_on(session, test_user, test_agent, test_conversation, monkeypatch):
    from app.agents.config import get_agent_settings

    monkeypatch.setattr(get_agent_settings(), "show_tool_commentary", True)
    fake_mcp = _FakeMCP(tools=[ToolHandle(server="securo", name="list_transactions", description="", parameters={"type": "object"})])
    provider = _CaptureAll(_commentary_script())
    executor = AgentExecutor(mcp=fake_mcp)
    with _patch_provider(provider):
        events = await _drain(executor, session=session, agent=test_agent, user_id=test_user.id,
                              conversation_id=test_conversation.id, user_message="compare months")

    assert "text_discard" not in [e.type for e in events]
    rows = (await session.execute(
        select(Message).where(Message.conversation_id == test_conversation.id).order_by(Message.ordinal)
    )).scalars().all()
    turn1 = next(r for r in rows if r.role == "assistant" and r.tool_calls)
    assert turn1.content == "Need August. Let's call the tool."
    assert "commentary" not in turn1.tool_calls[0]
    turn2_assistant = [m for m in provider.seen[1] if m.role == "assistant" and m.tool_calls]
    assert turn2_assistant[-1].content == "Need August. Let's call the tool."


async def test_replay_hides_commentary_on_old_rows(session, test_user, test_agent, test_conversation):
    """Rows persisted before the discard behaviour still carry commentary in
    `content`; replay strips it for the model."""
    from app.agents.services import conversation_service

    await conversation_service.append_message(session, conversation_id=test_conversation.id, role="user", content="earlier")
    await conversation_service.append_message(
        session, conversation_id=test_conversation.id, role="assistant", content="We have 47 categories. Let's call list_rules.",
        tool_calls=[{"id": "old1", "name": "securo__list_rules", "arguments": {}}],
    )
    await conversation_service.append_message(
        session, conversation_id=test_conversation.id, role="tool", content="{}",
        tool_result={"tool_call_id": "old1", "name": "securo__list_rules", "data": {"items": []}, "ok": True},
    )
    provider = _CaptureAll([[ChatChunk(type="text_delta", text="ok"), ChatChunk(type="finish", finish_reason="stop")]])
    executor = AgentExecutor(mcp=_FakeMCP(tools=[]))
    with _patch_provider(provider):
        await _drain(executor, session=session, agent=test_agent, user_id=test_user.id,
                     conversation_id=test_conversation.id, user_message="next")
    replayed = [m for m in provider.seen[0] if m.role == "assistant" and m.tool_calls]
    assert replayed and replayed[0].content is None


async def test_auto_context_primer_prepended_when_enabled(session, test_user, test_agent, test_conversation, test_account):
    """Captures the system messages the provider sees and verifies the
    primer is the first one when auto_context=True (default), with the
    agent's own system_prompt right after."""
    captured: dict[str, list] = {"system": []}

    class _Capture(_ScriptedProvider):
        async def chat_stream(self, messages, *, model, tools=None, temperature=0.4, max_tokens=None):
            captured["system"] = [m.content for m in messages if m.role == "system"]
            async for c in super().chat_stream(messages, model=model, tools=tools, temperature=temperature, max_tokens=max_tokens):
                yield c

    provider = _Capture([[
        ChatChunk(type="text_delta", text="ok"),
        ChatChunk(type="finish", finish_reason="stop"),
    ]])
    executor = AgentExecutor(mcp=_FakeMCP(tools=[]))
    test_agent.auto_context = True
    test_agent.system_prompt = "You are helpful."
    await session.commit()

    with _patch_provider(provider):
        await _drain(
            executor,
            session=session,
            agent=test_agent,
            user_id=test_user.id,
            conversation_id=test_conversation.id,
            user_message="hi",
        )
    sys_msgs = captured["system"]
    # Stack: guardrail [0] + identity primer [1] + agent system_prompt [2] + auto-context [3].
    assert len(sys_msgs) == 4, f"expected guardrail + identity + agent prompt + auto-context, got {len(sys_msgs)}"
    assert "Runtime rules" in sys_msgs[0]
    assert "propose_" in sys_msgs[0]
    assert "Securo" in sys_msgs[1]            # identity primer mentions the product
    assert sys_msgs[2] == "You are helpful."
    assert "Context for this conversation" in sys_msgs[3]
    assert "test@example.com" in sys_msgs[3]  # uses test_user fixture's email
    assert "Conta Corrente" in sys_msgs[3]    # account name from fixture


async def test_auto_context_primer_skipped_when_disabled(session, test_user, test_agent, test_conversation):
    captured: dict[str, list] = {"system": []}

    class _Capture(_ScriptedProvider):
        async def chat_stream(self, messages, *, model, tools=None, temperature=0.4, max_tokens=None):
            captured["system"] = [m.content for m in messages if m.role == "system"]
            async for c in super().chat_stream(messages, model=model, tools=tools, temperature=temperature, max_tokens=max_tokens):
                yield c

    provider = _Capture([[
        ChatChunk(type="text_delta", text="ok"),
        ChatChunk(type="finish", finish_reason="stop"),
    ]])
    executor = AgentExecutor(mcp=_FakeMCP(tools=[]))
    test_agent.auto_context = False
    test_agent.system_prompt = "Just the agent prompt."
    await session.commit()

    with _patch_provider(provider):
        await _drain(
            executor,
            session=session,
            agent=test_agent,
            user_id=test_user.id,
            conversation_id=test_conversation.id,
            user_message="hi",
        )
    sys_msgs = captured["system"]
    # Guardrail [0] + identity [1] + agent prompt [2]; no auto-context primer.
    assert len(sys_msgs) == 3
    assert "Runtime rules" in sys_msgs[0]
    assert "Securo" in sys_msgs[1]
    assert sys_msgs[2] == "Just the agent prompt."


async def test_runtime_guardrail_always_present_even_with_no_agent_prompt(session, test_user, test_agent, test_conversation):
    """Even when the user gave their agent no system_prompt and disabled
    auto_context, the runtime guardrail must still be there."""
    captured: dict[str, list] = {"system": []}

    class _Capture(_ScriptedProvider):
        async def chat_stream(self, messages, *, model, tools=None, temperature=0.4, max_tokens=None):
            captured["system"] = [m.content for m in messages if m.role == "system"]
            async for c in super().chat_stream(messages, model=model, tools=tools, temperature=temperature, max_tokens=max_tokens):
                yield c

    provider = _Capture([[ChatChunk(type="text_delta", text="ok"), ChatChunk(type="finish", finish_reason="stop")]])
    executor = AgentExecutor(mcp=_FakeMCP(tools=[]))
    test_agent.auto_context = False
    test_agent.system_prompt = ""
    await session.commit()

    with _patch_provider(provider):
        await _drain(
            executor, session=session, agent=test_agent,
            user_id=test_user.id, conversation_id=test_conversation.id,
            user_message="hi",
        )
    sys_msgs = captured["system"]
    # Guardrail [0] + identity primer [1] are the irreducible minimum —
    # both are always present even when the user gave no system_prompt
    # and turned auto-context off.
    assert len(sys_msgs) == 2
    assert "Runtime rules" in sys_msgs[0]
    assert "propose_" in sys_msgs[0]
    assert "Securo" in sys_msgs[1]


async def test_max_iterations_terminates_runaway_agent(session, test_user, test_agent, test_conversation):
    """If the LLM keeps emitting tool calls forever, executor stops at
    MAX_ITERS and emits a max_iterations error."""
    tools = [ToolHandle(server="securo", name="loop_tool", description="", parameters={"type": "object"})]
    fake_mcp = _FakeMCP(tools=tools)

    # Same tool-call turn repeated indefinitely.
    def _looping_turn():
        return [
            ChatChunk(type="tool_call_start", tool_call_id=f"t{uuid.uuid4().hex[:6]}", tool_name="securo__loop_tool"),
            ChatChunk(type="tool_call_args_delta", tool_call_id="t1", args_delta="{}"),
            ChatChunk(type="tool_call_end", tool_call_id="t1"),
            ChatChunk(type="finish", finish_reason="tool_calls"),
        ]

    provider = _ScriptedProvider([_looping_turn() for _ in range(20)])
    executor = AgentExecutor(mcp=fake_mcp)
    with _patch_provider(provider):
        events = await _drain(
            executor,
            session=session,
            agent=test_agent,
            user_id=test_user.id,
            conversation_id=test_conversation.id,
            user_message="loop forever",
        )

    err = next((e for e in events if e.type == "error"), None)
    done = next((e for e in events if e.type == "done"), None)
    assert err is not None and err.error_code == "max_iterations"
    assert done is not None and done.finish_reason == "max_iterations"

    # The stop message is localized (the test user prefers pt-BR) and names
    # the instance default cap; it is streamed and persisted, never Portuguese
    # by accident for everyone else.
    from app.agents.runtime.executor import _stop_message

    expected = _stop_message("pt-BR", 10)
    assert "Parei depois de 10 chamadas" in expected
    assert [e.text for e in events if e.type == "text_delta"][-1] == expected
    rows = (await session.execute(
        select(Message).where(Message.conversation_id == test_conversation.id).order_by(Message.ordinal)
    )).scalars().all()
    assert rows[-1].role == "assistant" and rows[-1].content == expected
    # 20 scripted turns, 10 consumed.
    assert len(provider._turns) == 10


async def test_iteration_cap_from_agent_extra(session, test_user, test_agent, test_conversation):
    """`agents.extra["max_tool_iterations"]` overrides the instance default."""
    from app.agents.runtime.executor import _iteration_cap, _stop_message
    from app.agents.config import get_agent_settings

    test_agent.extra = {"max_tool_iterations": 2}
    await session.commit()
    assert _iteration_cap(test_agent, get_agent_settings()) == 2

    tools = [ToolHandle(server="securo", name="loop_tool", description="", parameters={"type": "object"})]
    fake_mcp = _FakeMCP(tools=tools)
    turn = [
        ChatChunk(type="tool_call_start", tool_call_id="t1", tool_name="securo__loop_tool"),
        ChatChunk(type="tool_call_args_delta", tool_call_id="t1", args_delta="{}"),
        ChatChunk(type="tool_call_end", tool_call_id="t1"),
        ChatChunk(type="finish", finish_reason="tool_calls"),
    ]
    provider = _ScriptedProvider([list(turn) for _ in range(5)])
    executor = AgentExecutor(mcp=fake_mcp)
    with _patch_provider(provider):
        events = await _drain(
            executor, session=session, agent=test_agent, user_id=test_user.id,
            conversation_id=test_conversation.id, user_message="loop",
        )
    assert len(provider._turns) == 3  # exactly two provider calls
    assert len(fake_mcp.calls) == 2
    assert [e.text for e in events if e.type == "text_delta"][-1] == _stop_message("pt-BR", 2)


def test_stop_message_language_fallbacks():
    from app.agents.runtime.executor import _stop_message

    assert _stop_message("en", 3).startswith("I stopped after 3 tool calls")
    assert _stop_message("pt", 3).startswith("Parei depois de 3")
    assert _stop_message("es-MX", 3).startswith("Me detuve después de 3")
    assert _stop_message("de", 3).startswith("I stopped after 3")
    assert _stop_message(None, 3).startswith("I stopped after 3")


def test_iteration_cap_clamps_and_tolerates_bad_values():
    from types import SimpleNamespace
    from app.agents.runtime.executor import _iteration_cap

    settings = SimpleNamespace(max_tool_iterations=10)
    assert _iteration_cap(SimpleNamespace(extra=None), settings) == 10
    assert _iteration_cap(SimpleNamespace(extra={"max_tool_iterations": 0}), settings) == 1
    assert _iteration_cap(SimpleNamespace(extra={"max_tool_iterations": 999}), settings) == 50
    assert _iteration_cap(SimpleNamespace(extra={"max_tool_iterations": "abc"}), settings) == 10
    assert _iteration_cap(SimpleNamespace(extra={"max_tool_iterations": "4"}), settings) == 4


# --- pinned knowledge in the system context ---------------------------------

async def _seed_doc_with_chunks(session, test_agent, test_user, monkeypatch, texts, *, pinned=True):
    """Upload a doc through the service and give it ready chunks directly
    (no embedding provider involved)."""
    from app.agents.config import get_agent_settings
    from app.agents.services import knowledge_service

    monkeypatch.setattr(get_agent_settings(), "knowledge_storage_path", tempfile.mkdtemp(prefix="kb-exec-"))
    doc = await knowledge_service.upload_doc(
        session,
        agent_id=test_agent.id,
        user_id=test_user.id,
        filename="conventions.md",
        mime="text/markdown",
        payload=b"x",
        pinned=pinned,
    )
    await knowledge_service.replace_chunks(
        session,
        doc_id=doc.id,
        agent_id=test_agent.id,
        chunks=[(t, [0.0] * 1536) for t in texts],
        embedding_model="fake",
    )
    await knowledge_service.mark_status(session, doc.id, status="ready", chunk_count=len(texts))
    return doc


class _CaptureSystem(_ScriptedProvider):
    """Scripted provider that records the system messages it was given."""

    def __init__(self, scripts):
        super().__init__(scripts)
        self.system: list[str] = []

    async def chat_stream(self, messages, *, model, tools=None, temperature=0.4, max_tokens=None):
        self.system = [m.content for m in messages if m.role == "system"]
        async for c in super().chat_stream(messages, model=model, tools=tools, temperature=temperature, max_tokens=max_tokens):
            yield c


def _ok_script():
    return [[ChatChunk(type="text_delta", text="ok"), ChatChunk(type="finish", finish_reason="stop")]]


def test_format_pinned_context_returns_none_when_nothing_to_inject():
    assert _format_pinned_context([], max_chars=6000) is None
    assert _format_pinned_context([{"content": "   "}], max_chars=6000) is None
    assert _format_pinned_context([{"content": "rule"}], max_chars=0) is None


async def test_pinned_chunks_injected_after_agent_prompt_before_auto_context(
    session, test_user, test_agent, test_conversation, test_account, monkeypatch
):
    await _seed_doc_with_chunks(
        session, test_agent, test_user, monkeypatch,
        ["Internal transfer is never income.", "Card payment is a transfer, not an expense."],
    )
    provider = _CaptureSystem(_ok_script())
    executor = AgentExecutor(mcp=_FakeMCP(tools=[]))
    test_agent.auto_context = True
    test_agent.system_prompt = "You are helpful."
    await session.commit()

    with _patch_provider(provider):
        await _drain(
            executor,
            session=session,
            agent=test_agent,
            user_id=test_user.id,
            conversation_id=test_conversation.id,
            user_message="hi",
        )
    sys_msgs = provider.system
    # guardrail [0] + identity [1] + agent prompt [2] + PINNED [3] + auto-context [4]
    assert len(sys_msgs) == 5, f"expected 5 system messages, got {len(sys_msgs)}"
    assert sys_msgs[2] == "You are helpful."
    assert "Pinned reference material" in sys_msgs[3]
    assert "Internal transfer is never income." in sys_msgs[3]
    assert "Card payment is a transfer, not an expense." in sys_msgs[3]
    assert "Context for this conversation" in sys_msgs[4]


async def test_pinned_chunks_respect_max_chars(
    session, test_user, test_agent, test_conversation, monkeypatch
):
    from app.agents.config import get_agent_settings

    monkeypatch.setattr(get_agent_settings(), "pinned_context_max_chars", 300)
    await _seed_doc_with_chunks(session, test_agent, test_user, monkeypatch, ["A" * 250, "B" * 250])
    provider = _CaptureSystem(_ok_script())
    executor = AgentExecutor(mcp=_FakeMCP(tools=[]))
    test_agent.auto_context = False
    test_agent.system_prompt = "You are helpful."
    await session.commit()

    with _patch_provider(provider):
        await _drain(
            executor,
            session=session,
            agent=test_agent,
            user_id=test_user.id,
            conversation_id=test_conversation.id,
            user_message="hi",
        )
    pinned = [m for m in provider.system if "Pinned reference material" in m]
    assert len(pinned) == 1
    assert "A" * 250 in pinned[0]
    assert "B" not in pinned[0]
    assert "pinned material truncated" in pinned[0]


async def test_unpinned_docs_are_not_injected(
    session, test_user, test_agent, test_conversation, monkeypatch
):
    await _seed_doc_with_chunks(session, test_agent, test_user, monkeypatch, ["not pinned"], pinned=False)
    provider = _CaptureSystem(_ok_script())
    executor = AgentExecutor(mcp=_FakeMCP(tools=[]))
    test_agent.auto_context = False
    test_agent.system_prompt = "You are helpful."
    await session.commit()

    with _patch_provider(provider):
        await _drain(
            executor,
            session=session,
            agent=test_agent,
            user_id=test_user.id,
            conversation_id=test_conversation.id,
            user_message="hi",
        )
    assert not any("Pinned reference material" in m for m in provider.system)
    assert len(provider.system) == 3  # guardrail + identity + agent prompt
