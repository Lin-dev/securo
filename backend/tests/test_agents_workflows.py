"""Workflow harness: in-process tools, structured model steps, budget, proposal
emission and slash-command parsing (fork addition, qc9)."""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator

import pytest
import pytest_asyncio
from pydantic import BaseModel
from sqlalchemy import select

from app.agents.models.usage import LlmUsage
from app.agents.providers.base import ChatChunk, LLMProvider, LLMUnavailableError, Usage
from app.agents.workflows import registry
from app.agents.workflows.base import (
    WorkflowBudget,
    WorkflowBudgetExceeded,
    WorkflowContext,
    WorkflowStepError,
    build_context,
    collect,
    extract_json,
)

# asyncio_mode=auto covers the coroutine tests; the module mixes sync and async tests.


# --- fakes ------------------------------------------------------------------------


class _StructuredProvider(LLMProvider):
    """Replays canned replies (strings or exceptions) and records every call's
    kwargs so tests can assert what was requested."""

    name = "openai"

    def __init__(self, replies: list[Any]):
        super().__init__(api_key="x")
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    async def chat_stream(  # type: ignore[override]
        self, messages, *, model, tools=None, temperature=0.4, max_tokens=None,
        response_format=None, reasoning=None,
    ) -> AsyncIterator[ChatChunk]:
        self.calls.append({
            "messages": list(messages), "model": model, "tools": tools, "temperature": temperature,
            "max_tokens": max_tokens, "response_format": response_format, "reasoning": reasoning,
        })
        reply = self.replies.pop(0) if self.replies else "{}"
        if isinstance(reply, Exception):
            raise reply
        yield ChatChunk(type="text_delta", text=reply)
        yield ChatChunk(type="usage", usage=Usage(input_tokens=11, output_tokens=7))
        yield ChatChunk(type="finish", finish_reason="stop")

    async def embed(self, texts, *, model):
        return [[0.0] * 4 for _ in texts]


class _Answer(BaseModel):
    answer: str


class _EchoWorkflow:
    name = "echo"
    title = "Echo workflow"
    description = "Test workflow: emits one proposal card and a summary."
    aliases = ("say",)
    params_schema = {
        "type": "object",
        "properties": {
            "from_date": {"type": "string", "format": "date"},
            "to_date": {"type": "string", "format": "date"},
            "limit": {"type": "integer", "default": 60},
            "apply": {"type": "boolean", "default": False},
            "ratio": {"type": ["number", "null"]},
        },
        "additionalProperties": False,
    }

    def __init__(self):
        self.seen_params: list[dict[str, Any]] = []

    async def run(self, ctx: WorkflowContext, params: dict[str, Any]):
        self.seen_params.append(params)
        for ev in ctx.emit_step("load", {"limit": params.get("limit")}, {"rows": 1}):
            yield ev
        preview = {"kind": "create_payee_rule", "proposed": {"match_pattern": "ECHO", "category_id": "x"}}
        for ev in ctx.emit_proposals([
            ("securo__propose_create_payee_rule", {"match_pattern": "ECHO", "category_id": "x"}, preview),
        ]):
            yield ev
        ctx.finish("echo done", {"n": 1})


@pytest_asyncio.fixture
async def echo_workflow():
    wf = _EchoWorkflow()
    registry.register(wf)
    try:
        yield wf
    finally:
        registry.unregister(wf.name)


async def _ctx(session, test_user, test_agent, provider, **budget_kwargs) -> WorkflowContext:
    ctx = await build_context(
        session, user=test_user, agent=test_agent, conversation_id=uuid.uuid4(),
        provider=provider, model="gpt-oss:20b",
    )
    if budget_kwargs:
        ctx.budget = WorkflowBudget(**budget_kwargs)
    return ctx


# --- extract_json -----------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        '{"answer": "x"}',
        '```json\n{"answer": "x"}\n```',
        '<think>let me see</think>\n{"answer": "x"}',
        'Sure! Here it is: {"answer": "x"} hope that helps',
    ],
)
def test_extract_json_tolerates_fences_thinking_and_prose(raw):
    assert extract_json(raw) == {"answer": "x"}


def test_extract_json_raises_without_object():
    with pytest.raises(ValueError):
        extract_json("no json here")


# --- in-process tools ----------------------------------------------------------------


async def test_ctx_tool_runs_registry_handler_in_process_with_coerced_args(
    session, test_user, test_agent, test_workspace, test_categories
):
    ctx = await _ctx(session, test_user, test_agent, _StructuredProvider([]))
    out = await ctx.tool("list_categories", bogus_argument=1)
    assert isinstance(out, dict) and isinstance(out.get("items"), list)
    assert ctx.call_ctx.external is False
    assert ctx.workspace_id == test_workspace.id


async def test_ctx_tool_unknown_name_raises(session, test_user, test_agent, test_workspace):
    ctx = await _ctx(session, test_user, test_agent, _StructuredProvider([]))
    with pytest.raises(WorkflowStepError):
        await ctx.tool("no_such_tool")


# --- structured steps ------------------------------------------------------------------


async def test_llm_structured_parses_fenced_json_and_thinking_prefix(session, test_user, test_agent, test_workspace):
    provider = _StructuredProvider(['<think>hmm</think>```json\n{"answer": "yes"}\n```'])
    ctx = await _ctx(session, test_user, test_agent, provider)
    out = await ctx.llm_structured("Answer.", "Is it?", _Answer)
    assert out.answer == "yes"
    call = provider.calls[0]
    assert call["tools"] is None
    assert call["reasoning"] == "low"
    assert call["temperature"] == 0.0
    assert call["response_format"] == _Answer.model_json_schema()
    # the schema is also spelled out for providers without grammars
    assert "JSON schema" in call["messages"][0].content and '"answer"' in call["messages"][0].content


async def test_llm_structured_reasks_once_with_validation_error_then_raises(session, test_user, test_agent, test_workspace):
    provider = _StructuredProvider(["not json at all", '{"wrong": 1}'])
    ctx = await _ctx(session, test_user, test_agent, provider)
    with pytest.raises(WorkflowStepError):
        await ctx.llm_structured("Answer.", "Is it?", _Answer, retries=1)
    assert len(provider.calls) == 2
    reask = provider.calls[1]["messages"]
    assert reask[-1].role == "user" and "not valid" in reask[-1].content
    assert reask[-2].role == "assistant" and reask[-2].content == "not json at all"
    assert ctx.budget.llm_calls == 2


async def test_llm_structured_falls_back_without_format_on_400(session, test_user, test_agent, test_workspace):
    provider = _StructuredProvider([LLMUnavailableError("json_schema unsupported", status=400), '{"answer": "ok"}'])
    ctx = await _ctx(session, test_user, test_agent, provider)
    out = await ctx.llm_structured("Answer.", "Is it?", _Answer, retries=0)
    assert out.answer == "ok"
    assert provider.calls[0]["response_format"] is not None
    assert provider.calls[1]["response_format"] is None
    assert any("structured output" in n for n in ctx.notes)


async def test_llm_structured_propagates_non_400_errors(session, test_user, test_agent, test_workspace):
    provider = _StructuredProvider([LLMUnavailableError("down", status=503)])
    ctx = await _ctx(session, test_user, test_agent, provider)
    with pytest.raises(LLMUnavailableError):
        await ctx.llm_structured("Answer.", "Is it?", _Answer)


async def test_llm_structured_records_workflow_usage(session, test_user, test_agent, test_workspace):
    provider = _StructuredProvider(['{"answer": "ok"}'])
    ctx = await _ctx(session, test_user, test_agent, provider)
    await ctx.llm_structured("Answer.", "Is it?", _Answer)
    rows = (await session.execute(select(LlmUsage).where(LlmUsage.user_id == test_user.id))).scalars().all()
    assert [r.kind for r in rows] == ["workflow"]
    assert rows[0].input_tokens == 11 and rows[0].output_tokens == 7
    assert ctx.usage_input == 11 and ctx.usage_output == 7


async def test_llm_structured_accepts_explicit_json_schema(session, test_user, test_agent, test_workspace):
    provider = _StructuredProvider(['{"answer": "b"}'])
    ctx = await _ctx(session, test_user, test_agent, provider)
    patched = {"type": "object", "properties": {"answer": {"type": "string", "enum": ["a", "b"]}}, "required": ["answer"]}
    out = await ctx.llm_structured("Pick.", "?", _Answer, json_schema=patched)
    assert out.answer == "b"
    assert provider.calls[0]["response_format"]["properties"]["answer"]["enum"] == ["a", "b"]


# --- budget ------------------------------------------------------------------------------


async def test_budget_blocks_llm_calls_past_max(session, test_user, test_agent, test_workspace):
    provider = _StructuredProvider(['{"answer": "1"}', '{"answer": "2"}'])
    ctx = await _ctx(session, test_user, test_agent, provider, max_llm_calls=1)
    await ctx.llm_structured("A", "q", _Answer)
    with pytest.raises(WorkflowBudgetExceeded):
        await ctx.llm_structured("A", "q", _Answer)
    assert len(provider.calls) == 1
    assert ctx.budget.remaining_llm_calls == 0


async def test_budget_time_exceeded_raises(session, test_user, test_agent, test_workspace):
    provider = _StructuredProvider(['{"answer": "1"}'])
    ctx = await _ctx(session, test_user, test_agent, provider, max_seconds=1.0)
    ctx.budget.started = time.monotonic() - 1000
    with pytest.raises(WorkflowBudgetExceeded):
        await ctx.llm_structured("A", "q", _Answer)
    with pytest.raises(WorkflowBudgetExceeded):
        await ctx.tool("list_categories")


# --- events and proposals ------------------------------------------------------------------


async def test_emit_proposals_returns_calls_then_results_in_order_with_shared_ids(
    session, test_user, test_agent, test_workspace
):
    ctx = await _ctx(session, test_user, test_agent, _StructuredProvider([]))
    items = [
        ("securo__propose_create_payee_rule", {"match_pattern": "A"}, {"kind": "create_payee_rule", "proposed": {"match_pattern": "A"}}),
        ("securo__propose_create_payee_rule", {"match_pattern": "B"}, {"kind": "create_payee_rule", "proposed": {"match_pattern": "B"}}),
    ]
    events = ctx.emit_proposals(items)
    assert [e.type for e in events] == ["tool_call", "tool_call", "tool_result", "tool_result"]
    assert [e.tool_args["match_pattern"] for e in events[:2]] == ["A", "B"]
    assert [e.tool_result["data"]["proposed"]["match_pattern"] for e in events[2:]] == ["A", "B"]
    assert all(e.tool_name == "securo__propose_create_payee_rule" for e in events)
    assert len(ctx.pending_proposals) == 2
    ids = [p.tool_call_id for p in ctx.pending_proposals]
    assert len(set(ids)) == 2 and all(i.startswith("wf_") and len(i) == 11 for i in ids)
    assert ctx.pending_proposals[0].result["kind"] == "create_payee_rule"


async def test_emit_step_is_a_tool_chip_pair(session, test_user, test_agent, test_workspace):
    ctx = await _ctx(session, test_user, test_agent, _StructuredProvider([]))
    call, result = ctx.emit_step("load", {"limit": 5}, {"rows": 3})
    assert call.type == "tool_call" and call.tool_name == "workflow.load" and call.tool_args == {"limit": 5}
    assert result.type == "tool_result" and result.tool_result["ok"] is True and result.tool_result["data"] == {"rows": 3}
    assert ctx.pending_proposals == []


async def test_collect_runs_workflow_and_returns_events_and_result(
    session, test_user, test_agent, test_workspace, echo_workflow
):
    ctx = await _ctx(session, test_user, test_agent, _StructuredProvider([]))
    events, result = await collect(echo_workflow, ctx, {"limit": 3})
    assert [e.type for e in events] == ["tool_call", "tool_result", "tool_call", "tool_result"]
    assert result.summary == "echo done" and result.data == {"n": 1} and result.final is True and result.ok is True
    assert echo_workflow.seen_params == [{"limit": 3}]
    assert len(ctx.pending_proposals) == 1


async def test_pinned_text_is_bounded_and_empty_without_pins(session, test_user, test_agent, test_workspace):
    ctx = await _ctx(session, test_user, test_agent, _StructuredProvider([]))
    assert await ctx.pinned_text() == ""
    assert await ctx.pinned_text(max_chars=0) == ""


async def test_build_context_language_prefers_agent_extra_then_user_prefs(session, test_user, test_agent, test_workspace):
    ctx = await _ctx(session, test_user, test_agent, _StructuredProvider([]))
    assert ctx.language == (test_user.preferences or {}).get("language", "en")
    test_agent.extra = {"language": "es"}
    ctx2 = await _ctx(session, test_user, test_agent, _StructuredProvider([]))
    assert ctx2.language == "es"


# --- registry ---------------------------------------------------------------------------------


def test_registry_get_resolves_names_and_aliases(echo_workflow):
    assert registry.get("echo") is echo_workflow
    assert registry.get("say") is echo_workflow
    assert registry.get("ECHO") is echo_workflow
    assert registry.get("nope") is None
    assert registry.is_workflow_tool("workflow__echo") is True
    assert registry.is_workflow_tool("workflow__nope") is False
    assert registry.is_workflow_tool("securo__echo") is False
    assert registry.workflow_for_tool("workflow__say") is echo_workflow


def test_register_refuses_duplicates_and_ask(echo_workflow):
    with pytest.raises(ValueError):
        registry.register(_EchoWorkflow())

    class _Ask(_EchoWorkflow):
        name = "ask"
        aliases = ()

    with pytest.raises(ValueError):
        registry.register(_Ask())


def test_as_tool_definitions_respects_allowlist(echo_workflow):
    all_defs = registry.as_tool_definitions(None)
    echo_def = next(d for d in all_defs if d.name == "workflow__echo")
    assert echo_def.parameters == echo_workflow.params_schema
    assert registry.as_tool_definitions({("securo", "list_accounts")}) == []
    assert [d.name for d in registry.as_tool_definitions({("workflow", "echo")})] == ["workflow__echo"]
    handle = next(h for h in registry.as_tool_handles() if h.name == "echo")
    assert handle.server == "workflow" and handle.is_proposal is False


def test_parse_slash_command_coerces_params_and_resolves_alias(echo_workflow):
    wf, params = registry.parse_slash_command("/say limit=20 apply=true from=2026-08-01 to=2026-08-31 ratio=0.5 junk=1")
    assert wf is echo_workflow
    assert params == {"limit": 20, "apply": True, "from_date": "2026-08-01", "to_date": "2026-08-31", "ratio": 0.5}
    wf2, params2 = registry.parse_slash_command("  /echo  ")
    assert wf2 is echo_workflow and params2 == {}
    assert registry.parse_slash_command("/nope x=1") is None
    assert registry.parse_slash_command("hello /echo") is None
    assert registry.parse_slash_command("/ask what is my net worth") is None


def test_parse_slash_command_ignores_bad_values(echo_workflow):
    _, params = registry.parse_slash_command("/echo limit=abc apply=maybe")
    assert params == {}


def test_strip_ask_prefix():
    assert registry.strip_ask_prefix("/ask how did August compare?") == "how did August compare?"
    assert registry.strip_ask_prefix("/ASK   spaced") == "spaced"
    assert registry.strip_ask_prefix("/echo x") is None
    assert registry.strip_ask_prefix("plain question") is None


def test_load_builtin_is_idempotent():
    registry.load_builtin()
    registry.load_builtin()
    assert isinstance(registry.WORKFLOWS, dict)


def test_json_roundtrip_of_tool_definition_schema(echo_workflow):
    d = next(x for x in registry.as_tool_definitions(None) if x.name == "workflow__echo")
    assert json.loads(json.dumps(d.parameters)) == echo_workflow.params_schema
