"""Executor integration for the deterministic paths: slash commands, `/ask`,
`workflow__*` tools inside the free-form loop, and guided-mode routing.

Reuses the scripted provider / fake MCP from `test_agents_executor` and the
echo workflow from `test_agents_workflows`; the real categorize workflow is
exercised end-to-end on an empty workspace (nothing to categorize).
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.agents.config import get_agent_settings
from app.agents.mcp.client import ToolHandle
from app.agents.models.conversation import Conversation, Message
from app.agents.providers.base import ChatChunk, Usage
from app.agents.runtime.executor import AgentExecutor
from app.agents.services import agent_service
from app.agents.workflows import registry
from tests.test_agents_executor import _FakeMCP, _ScriptedProvider, _drain, _patch_provider
from tests.test_agents_guided import _route_json, _text_turn
from tests.test_agents_workflows import _EchoWorkflow
from tests.test_mcp_finance_tools import _ensure_to_char  # noqa: F401 — autouse SQLite shim for report tools

pytestmark = pytest.mark.asyncio


# --- fixtures -------------------------------------------------------------------


@pytest_asyncio.fixture
async def echo():
    wf = _EchoWorkflow()
    registry.register(wf)
    try:
        yield wf
    finally:
        registry.unregister(wf.name)


@pytest.fixture
def guided_off(monkeypatch):
    monkeypatch.setattr(get_agent_settings(), "guided_mode", False)


@pytest.fixture
def guided_on(monkeypatch):
    monkeypatch.setattr(get_agent_settings(), "guided_mode", True)
    monkeypatch.setattr(get_agent_settings(), "guided_min_confidence", 0.7)


def _wf_tool_turn(name: str, args: dict | None = None, call_id: str = "t1"):
    return [
        ChatChunk(type="tool_call_start", tool_call_id=call_id, tool_name=name),
        ChatChunk(type="tool_call_args_delta", tool_call_id=call_id, args_delta=json.dumps(args or {})),
        ChatChunk(type="tool_call_end", tool_call_id=call_id),
        ChatChunk(type="finish", finish_reason="tool_calls"),
    ]


def _answer_turn(text: str = "Final answer."):
    return [ChatChunk(type="text_delta", text=text), ChatChunk(type="usage", usage=Usage(5, 2)), ChatChunk(type="finish", finish_reason="stop")]


async def _rows(session, conversation_id) -> list[Message]:
    return (await session.execute(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.ordinal)
    )).scalars().all()


async def _run(session, test_user, test_agent, test_conversation, provider, message, mcp=None):
    executor = AgentExecutor(mcp=mcp or _FakeMCP(tools=[]))
    with _patch_provider(provider):
        return await _drain(
            executor, session=session, agent=test_agent, user_id=test_user.id,
            conversation_id=test_conversation.id, user_message=message,
        )


# --- slash commands -------------------------------------------------------------


async def test_slash_command_runs_workflow_without_calling_provider(session, test_user, test_agent, test_conversation, echo, guided_off):
    class _NeverCalled(_ScriptedProvider):
        async def chat_stream(self, *a, **kw):  # pragma: no cover - must not run
            raise AssertionError("the model must not be called for a slash command")
            yield

    events = await _run(session, test_user, test_agent, test_conversation, _NeverCalled([]), "/echo limit=5 apply=true")

    assert echo.seen_params == [{"limit": 5, "apply": True}]
    kinds = [e.type for e in events]
    assert kinds[-2:] == ["text_delta", "done"] and events[-2].text == "echo done"
    # progress chip + one proposal card (call then result)
    assert [e.tool_name for e in events if e.type == "tool_call"] == ["workflow.load", "securo__propose_create_payee_rule"]

    rows = await _rows(session, test_conversation.id)
    assert [r.role for r in rows] == ["user", "assistant", "tool"]
    assert rows[0].content == "/echo limit=5 apply=true"
    assert rows[1].content == "echo done" and len(rows[1].tool_calls) == 1
    assert rows[1].tool_calls[0]["name"] == "securo__propose_create_payee_rule"
    assert rows[2].tool_result["tool_call_id"] == rows[1].tool_calls[0]["id"]
    assert rows[2].tool_result["data"]["kind"] == "create_payee_rule"
    # the workflow title replaces the raw command as the conversation title
    conv = await session.get(Conversation, test_conversation.id)
    assert conv.title == "Echo workflow"


async def test_slash_alias_and_unknown_command(session, test_user, test_agent, test_conversation, echo, guided_off):
    events = await _run(session, test_user, test_agent, test_conversation, _ScriptedProvider([]), "/say")
    assert events[-1].type == "done" and echo.seen_params == [{}]
    # an unknown slash command is just a message for the model
    provider = _ScriptedProvider([_answer_turn("no such command")])
    events = await _run(session, test_user, test_agent, test_conversation, provider, "/nope")
    assert "".join(e.text or "" for e in events if e.type == "text_delta") == "no such command"


async def test_ask_prefix_skips_guided_and_model_sees_the_question(session, test_user, test_agent, test_conversation, guided_on):
    seen: list[list] = []

    class _Capture(_ScriptedProvider):
        async def chat_stream(self, messages, *, model, tools=None, temperature=0.4, max_tokens=None, **kwargs):
            seen.append(list(messages))
            async for c in super().chat_stream(messages, model=model, tools=tools, temperature=temperature, max_tokens=max_tokens, **kwargs):
                yield c

    provider = _Capture([_answer_turn("about 12")])
    with patch("app.agents.runtime.guided.route", side_effect=AssertionError("router must not run for /ask")):
        events = await _run(session, test_user, test_agent, test_conversation, provider, "/ask how much did I pay uber")

    assert events[-1].type == "done"
    user_turns = [m for m in seen[0] if m.role == "user"]
    assert user_turns[-1].content == "how much did I pay uber"
    rows = await _rows(session, test_conversation.id)
    assert rows[0].content == "/ask how much did I pay uber"  # the row keeps what was typed


# --- workflow tools in the loop ---------------------------------------------------


async def test_workflow_tool_definitions_follow_the_whitelist(session, test_user, test_agent, test_conversation, echo, guided_off):
    captured: list[list[str]] = []

    class _Capture(_ScriptedProvider):
        async def chat_stream(self, messages, *, model, tools=None, temperature=0.4, max_tokens=None, **kwargs):
            captured.append([t.name for t in (tools or [])])
            async for c in super().chat_stream(messages, model=model, tools=tools, temperature=temperature, max_tokens=max_tokens, **kwargs):
                yield c

    mcp = _FakeMCP(tools=[ToolHandle(server="securo", name="list_accounts", description="", parameters={"type": "object"})])
    # no explicit rows → allow-all: MCP tools plus every workflow
    await _run(session, test_user, test_agent, test_conversation, _Capture([_answer_turn()]), "hello", mcp=mcp)
    assert "workflow__echo" in captured[-1] and "securo__list_accounts" in captured[-1]

    await agent_service.replace_tool_enablement(session, test_agent.id, [("securo", "list_accounts", True)])
    await _run(session, test_user, test_agent, test_conversation, _Capture([_answer_turn()]), "hello", mcp=mcp)
    assert captured[-1] == ["securo__list_accounts"]

    await agent_service.replace_tool_enablement(session, test_agent.id, [("securo", "list_accounts", True), ("workflow", "echo", True)])
    await _run(session, test_user, test_agent, test_conversation, _Capture([_answer_turn()]), "hello", mcp=mcp)
    assert captured[-1] == ["securo__list_accounts", "workflow__echo"]


async def test_workflow_tool_call_routes_to_harness_and_final_ends_turn(session, test_user, test_agent, test_conversation, echo, guided_off):
    provider = _ScriptedProvider([_wf_tool_turn("workflow__echo", {"limit": 3}), _answer_turn("must stay unconsumed")])
    mcp = _FakeMCP(tools=[])

    events = await _run(session, test_user, test_agent, test_conversation, provider, "please echo", mcp=mcp)

    assert len(provider.calls) == 1, "a final workflow ends the turn without another model call"
    assert mcp.calls == []
    assert echo.seen_params == [{"limit": 3}]
    assert events[-1].type == "done" and events[-1].finish_reason == "stop"
    texts = [e.text for e in events if e.type == "text_delta"]
    assert texts[-1] == "echo done"
    tool_results = [e.tool_name for e in events if e.type == "tool_result"]
    assert "workflow__echo" in tool_results and "securo__propose_create_payee_rule" in tool_results

    rows = await _rows(session, test_conversation.id)
    assert [r.role for r in rows] == ["user", "assistant", "tool", "assistant", "tool"]
    assert rows[1].tool_calls[0]["name"] == "workflow__echo"
    assert rows[2].tool_result["tool_call_id"] == "t1" and rows[2].tool_result["name"] == "workflow__echo"
    assert json.loads(rows[2].content) == {"n": 1}
    assert rows[3].content == "echo done"
    assert rows[3].tool_calls[0]["id"].startswith("wf_")
    assert rows[4].tool_result["tool_call_id"] == rows[3].tool_calls[0]["id"]


async def test_mixed_mcp_and_workflow_calls_persist_both(session, test_user, test_agent, test_conversation, echo, guided_off):
    turn = [
        ChatChunk(type="tool_call_start", tool_call_id="m1", tool_name="securo__list_transactions"),
        ChatChunk(type="tool_call_args_delta", tool_call_id="m1", args_delta="{}"),
        ChatChunk(type="tool_call_end", tool_call_id="m1"),
        ChatChunk(type="tool_call_start", tool_call_id="w1", tool_name="workflow__echo"),
        ChatChunk(type="tool_call_args_delta", tool_call_id="w1", args_delta="{}"),
        ChatChunk(type="tool_call_end", tool_call_id="w1"),
        ChatChunk(type="finish", finish_reason="tool_calls"),
    ]
    provider = _ScriptedProvider([turn, _answer_turn("unused")])
    mcp = _FakeMCP(tools=[ToolHandle(server="securo", name="list_transactions", description="", parameters={"type": "object"})])

    events = await _run(session, test_user, test_agent, test_conversation, provider, "do both", mcp=mcp)

    assert [c[0] for c in mcp.calls] == ["securo__list_transactions"]
    assert events[-1].type == "done"
    rows = await _rows(session, test_conversation.id)
    roles = [(r.role, (r.tool_result or {}).get("name")) for r in rows]
    assert roles == [
        ("user", None), ("assistant", None),
        ("tool", "securo__list_transactions"), ("tool", "workflow__echo"),
        ("assistant", None), ("tool", "securo__propose_create_payee_rule"),
    ]


async def test_duplicate_workflow_call_in_one_turn_is_skipped(session, test_user, test_agent, test_conversation, echo, guided_off):
    turn = _wf_tool_turn("workflow__echo", {}, call_id="a")[:-1] + _wf_tool_turn("workflow__echo", {}, call_id="b")
    provider = _ScriptedProvider([turn])
    events = await _run(session, test_user, test_agent, test_conversation, provider, "twice")
    assert events[-1].type == "done"
    assert len(echo.seen_params) == 1
    rows = await _rows(session, test_conversation.id)
    skipped = [r for r in rows if r.role == "tool" and (r.tool_result or {}).get("data") == {"skipped": "duplicate workflow call"}]
    assert len(skipped) == 1 and skipped[0].tool_result["tool_call_id"] == "b"


async def test_workflow_error_yields_error_event_and_readable_summary(session, test_user, test_agent, test_conversation, guided_off):
    class _Boom:
        name = "boom"
        title = "Boom workflow"
        description = "always fails"
        aliases = ()
        params_schema = {"type": "object", "properties": {}, "additionalProperties": False}

        async def run(self, ctx, params):
            for ev in ctx.emit_step("load", {}, {"rows": 0}):
                yield ev
            raise RuntimeError("database on fire")

    registry.register(_Boom())
    try:
        events = await _run(session, test_user, test_agent, test_conversation, _ScriptedProvider([]), "/boom")
    finally:
        registry.unregister("boom")

    errors = [e for e in events if e.type == "error"]
    assert errors and errors[0].error_code == "workflow" and "database on fire" in (errors[0].error_message or "")
    assert events[-1].type == "done"
    rows = await _rows(session, test_conversation.id)
    assert rows[-1].role == "assistant"
    # the pt-BR test user gets the localized failure line
    assert rows[-1].content.startswith("O fluxo Boom workflow parou antes de terminar")
    assert "database on fire" in rows[-1].content


async def test_reload_shape_matches_proposal_card_contract(session, test_user, test_agent, test_conversation, echo, guided_off):
    await _run(session, test_user, test_agent, test_conversation, _ScriptedProvider([]), "/echo")
    rows = await _rows(session, test_conversation.id)
    assistant = [r for r in rows if r.role == "assistant" and r.tool_calls][-1]
    tool_rows = {r.tool_result["tool_call_id"]: r for r in rows if r.role == "tool"}
    for tc in assistant.tool_calls:
        assert "propose_" in tc["name"]
        paired = tool_rows[tc["id"]]
        assert paired.tool_result["name"] == tc["name"]
        assert paired.tool_result["data"]["kind"] == "create_payee_rule"
        assert paired.tool_result["ok"] is True


# --- guided routing ---------------------------------------------------------------


async def test_guided_intent_answers_from_code_and_never_calls_mcp(session, test_user, test_agent, test_conversation, guided_on):
    class _StrictMCP(_FakeMCP):
        async def call(self, **kw):  # pragma: no cover - must not run
            raise AssertionError("guided answers never call MCP")

    provider = _ScriptedProvider([
        _text_turn(_route_json(intent="compare_periods", period_a="this_month", period_b="last_month", confidence=0.95)),
        _text_turn("Income covered spending in both periods."),
    ])
    events = await _run(session, test_user, test_agent, test_conversation, provider, "How did this month compare to last month?", mcp=_StrictMCP(tools=[]))

    assert len(provider.calls) == 2  # router + narration, no tool loop
    assert provider.calls[0]["response_format"] is not None and provider.calls[0]["tools"] is None
    kinds = [e.type for e in events]
    assert kinds[0] == "tool_call" and kinds[-1] == "done"
    assert [e.tool_name for e in events if e.type == "tool_call"] == ["securo__get_transactions_summary"] * 2
    rows = await _rows(session, test_conversation.id)
    assert [r.role for r in rows] == ["user", "assistant", "tool", "tool", "assistant"]
    assert rows[1].tool_calls[0]["id"].startswith("guided_")
    assert "Income covered spending" in rows[-1].content


async def test_guided_categorize_review_routes_to_workflow(session, test_user, test_agent, test_conversation, guided_on):
    provider = _ScriptedProvider([_text_turn(_route_json(intent="categorize_review", confidence=0.9))])
    events = await _run(session, test_user, test_agent, test_conversation, provider, "review my uncategorized transactions and apply the rules")

    assert len(provider.calls) == 1  # router only: nothing to categorize, so no classification call
    assert events[-1].type == "done"
    rows = await _rows(session, test_conversation.id)
    assert rows[-1].role == "assistant" and rows[-1].content
    conv = await session.get(Conversation, test_conversation.id)
    assert conv.title == "review my uncategorized transactions and apply the rules"


async def test_guided_low_confidence_falls_through_to_loop(session, test_user, test_agent, test_conversation, guided_on):
    provider = _ScriptedProvider([
        _text_turn(_route_json(intent="compare_periods", period_a="this_month", period_b="last_month", confidence=0.3)),
        _answer_turn("loop answer"),
    ])
    events = await _run(session, test_user, test_agent, test_conversation, provider, "something vague")
    assert len(provider.calls) == 2
    assert "".join(e.text or "" for e in events if e.type == "text_delta") == "loop answer"
    rows = await _rows(session, test_conversation.id)
    assert [r.role for r in rows] == ["user", "assistant"]


async def test_guided_handler_fallthrough_uses_loop(session, test_user, test_agent, test_conversation, guided_on):
    provider = _ScriptedProvider([
        _text_turn(_route_json(intent="spending_breakdown", period_a="month:2030-01", confidence=0.95)),  # future period → GuidedFallthrough
        _answer_turn("loop answer"),
    ])
    events = await _run(session, test_user, test_agent, test_conversation, provider, "what did I spend in 2030")
    assert "".join(e.text or "" for e in events if e.type == "text_delta") == "loop answer"
    assert not [e for e in events if e.type == "tool_call"]


async def test_freeform_agent_bypasses_router(session, test_user, test_agent, test_conversation, guided_on):
    test_agent.extra = {"mode": "freeform"}
    session.add(test_agent)
    await session.commit()
    provider = _ScriptedProvider([_answer_turn("direct")])
    with patch("app.agents.runtime.guided.route", side_effect=AssertionError("router must not run")):
        events = await _run(session, test_user, test_agent, test_conversation, provider, "How did this month compare to last month?")
    assert "".join(e.text or "" for e in events if e.type == "text_delta") == "direct"


async def test_digest_channel_is_never_guided(session, test_user, test_agent, test_conversation, guided_on):
    provider = _ScriptedProvider([_answer_turn("digest text")])
    executor = AgentExecutor(mcp=_FakeMCP(tools=[]))
    with _patch_provider(provider), patch("app.agents.runtime.guided.route", side_effect=AssertionError("router must not run")):
        events = await _drain(
            executor, session=session, agent=test_agent, user_id=test_user.id,
            conversation_id=test_conversation.id, user_message="Write the weekly review", channel="digest",
        )
    assert "".join(e.text or "" for e in events if e.type == "text_delta") == "digest text"
    assert uuid.UUID(str(test_conversation.id))  # sanity: same conversation object reused
