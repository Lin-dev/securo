"""Guided mode: intent routing, period grammar, grounded narration and the
deterministic handlers behind the common finance questions (fork, qc9).

The handlers are driven directly (the executor wires them in separately), with
real seeded rows so every pack figure can be checked against the tool that
produced it, and a scripted provider for the router and the narration.
"""
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.config import get_agent_settings
from app.agents.models.conversation import Message
from app.agents.models.usage import LlmUsage
from app.agents.providers.base import ChatChunk, LLMProvider, LLMUnavailableError, Usage
from app.agents.runtime import guided
from app.agents.runtime.grounding import extract_numbers, pack_values, ungrounded
from app.agents.runtime.guided import (
    GuidedContext,
    GuidedFallthrough,
    RouteDecision,
    answer,
    is_eligible,
    local_today,
    resolve_period,
    route,
)
from app.agents.services import conversation_service
from mcp_server.auth import CallContext
from mcp_server.registry import REGISTRY
from tests.test_agents_executor import _ScriptedProvider
from tests.test_mcp_finance_tools import _account, _asset, _category, _ensure_to_char, _seed_summary_rows, _txn  # noqa: F401

TODAY = date(2026, 9, 23)


# --- helpers ------------------------------------------------------------------


def _text_turn(text: str, usage: Usage = Usage(50, 20)) -> list[ChatChunk]:
    return [
        ChatChunk(type="text_delta", text=text),
        ChatChunk(type="usage", usage=usage),
        ChatChunk(type="finish", finish_reason="stop"),
    ]


def _route_json(**overrides) -> str:
    base = {"intent": "freeform", "period_a": None, "period_b": None, "days": None, "months": None, "category": None, "confidence": 0.0}
    base.update(overrides)
    return json.dumps(base)


class _FailingProvider(LLMProvider):
    name = "openai"

    async def chat_stream(self, messages, *, model, tools=None, temperature=0.4, max_tokens=None, **kwargs):
        raise LLMUnavailableError("down")
        yield  # pragma: no cover

    async def embed(self, texts, *, model):
        return []


async def _ctx(session, test_user, test_workspace, test_agent, provider, *, language="en", currency="BRL", today=TODAY, title="guided test"):
    conv = await conversation_service.create_conversation(
        session, workspace_id=test_workspace.id, user_id=test_user.id, agent_id=test_agent.id, channel="web", title=title
    )
    await conversation_service.append_message(session, conversation_id=conv.id, role="user", content="question")
    return GuidedContext(
        session=session, agent=test_agent, user=test_user, user_id=test_user.id, workspace_id=test_workspace.id,
        conversation_id=conv.id, provider=provider, model="gpt-4o-mini", settings=get_agent_settings(),
        today=today, tz="America/Sao_Paulo", language=language, currency=currency,
    )


async def _rows(session, conversation_id):
    return (await session.execute(select(Message).where(Message.conversation_id == conversation_id).order_by(Message.ordinal))).scalars().all()


# --- periods ------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr, expected",
    [
        ("this_month", (date(2026, 9, 1), date(2026, 9, 23), "Sep 2026")),
        ("last_month", (date(2026, 8, 1), date(2026, 8, 31), "Aug 2026")),
        ("this_year", (date(2026, 1, 1), date(2026, 9, 23), "2026 YTD")),
        ("ytd", (date(2026, 1, 1), date(2026, 9, 23), "2026 YTD")),
        ("last_year", (date(2025, 1, 1), date(2025, 12, 31), "2025")),
        ("month:2026-02", (date(2026, 2, 1), date(2026, 2, 28), "Feb 2026")),
        ("month:2026-09", (date(2026, 9, 1), date(2026, 9, 23), "Sep 2026")),
        ("year:2024", (date(2024, 1, 1), date(2024, 12, 31), "2024")),
        ("year:2026", (date(2026, 1, 1), date(2026, 9, 23), "2026")),
        ("last_n_days:30", (date(2026, 8, 25), date(2026, 9, 23), "last 30 days")),
        ("last_n_days:3", (date(2026, 9, 17), date(2026, 9, 23), "last 7 days")),
        ("last_n_months:3", (date(2026, 7, 1), date(2026, 9, 23), "last 3 months")),
        ("custom:2026-06-01..2026-06-30", (date(2026, 6, 1), date(2026, 6, 30), "2026-06-01 to 2026-06-30")),
        ("custom:2026-09-01..2026-12-31", (date(2026, 9, 1), date(2026, 9, 23), "2026-09-01 to 2026-09-23")),
    ],
)
def test_resolve_period_matrix(expr, expected):
    assert resolve_period(expr, TODAY) == expected


@pytest.mark.parametrize("expr", ["month:2026-10", "year:2027", "custom:2026-05-10..2026-05-01", "yesterday", "", "month:2026-13x"])
def test_resolve_period_rejects_future_and_unknown(expr):
    with pytest.raises(ValueError):
        resolve_period(expr, TODAY)


def test_previous_period_pairs_naturally():
    assert guided._previous_period("this_month", TODAY) == "last_month"
    assert guided._previous_period("last_month", TODAY) == "month:2026-07"
    assert guided._previous_period("month:2026-01", TODAY) == "month:2025-12"
    assert guided._previous_period("this_year", TODAY) == "last_year"
    assert guided._previous_period("last_n_days:30", TODAY) == "custom:2026-07-26..2026-08-24"


def test_local_today_respects_timezone_day_boundary():
    at = datetime(2026, 9, 24, 2, 30, tzinfo=timezone.utc)  # 23:30 the day before in São Paulo
    assert local_today("America/Sao_Paulo", now=at) == date(2026, 9, 23)
    assert local_today("UTC", now=at) == date(2026, 9, 24)
    assert local_today("Not/AZone", now=at) == date(2026, 9, 24)


# --- eligibility --------------------------------------------------------------


def test_is_eligible_rules(test_agent):
    s = get_agent_settings()
    assert is_eligible(agent=test_agent, settings=s, channel="web", user_message="How did August compare to July?")
    assert not is_eligible(agent=test_agent, settings=s, channel="digest", user_message="review")
    assert not is_eligible(agent=test_agent, settings=s, channel="web", user_message="/ask what about uber")
    assert not is_eligible(agent=test_agent, settings=s, channel="web", user_message="x" * 601)
    test_agent.extra = {"mode": "freeform"}
    assert not is_eligible(agent=test_agent, settings=s, channel="web", user_message="hello")
    test_agent.extra = {}


# --- router -------------------------------------------------------------------


async def test_route_parses_scripted_json_and_captures_kwargs(session: AsyncSession, test_user, test_workspace, test_agent):
    provider = _ScriptedProvider([_text_turn(_route_json(intent="compare_periods", period_a="month:2026-08", period_b="month:2026-07", confidence=0.92))])
    ctx = await _ctx(session, test_user, test_workspace, test_agent, provider)
    d = await route(ctx, user_message="How did August compare to July?", prior_user_message="hi")
    assert (d.intent, d.period_a, d.period_b, d.confidence) == ("compare_periods", "month:2026-08", "month:2026-07", 0.92)
    call = provider.calls[0]
    assert call["response_format"] is guided.ROUTER_SCHEMA
    assert call["reasoning"] == "low" and call["tools"] is None
    rows = (await session.execute(select(LlmUsage).where(LlmUsage.conversation_id == ctx.conversation_id))).scalars().all()
    assert [r.kind for r in rows] == ["router"] and rows[0].message_id is None


async def test_route_malformed_json_falls_back_to_freeform(session, test_user, test_workspace, test_agent):
    provider = _ScriptedProvider([_text_turn("<think>hmm</think> not json at all")])
    ctx = await _ctx(session, test_user, test_workspace, test_agent, provider)
    d = await route(ctx, user_message="whatever")
    assert d.intent == "freeform" and d.confidence == 0.0


async def test_route_tolerates_fences_and_invalid_slots(session, test_user, test_workspace, test_agent):
    body = _route_json(intent="money_map", days=5000, period_a="next week", months=None, confidence=1.7)
    provider = _ScriptedProvider([_text_turn("```json\n" + body + "\n```")])
    ctx = await _ctx(session, test_user, test_workspace, test_agent, provider)
    d = await route(ctx, user_message="where did my money go")
    assert d.intent == "money_map" and d.days == 730 and d.period_a is None and d.confidence == 1.0


async def test_route_provider_error_is_freeform(session, test_user, test_workspace, test_agent):
    ctx = await _ctx(session, test_user, test_workspace, test_agent, _FailingProvider())
    d = await route(ctx, user_message="hello")
    assert d.intent == "freeform"


def test_router_system_prompt_renders_relative_months_and_context():
    text = guided.router_system(today=TODAY, tz="America/Sao_Paulo", language="pt-BR", prior_user_message="a" * 300)
    assert "month:2026-08" in text and "month:2026-07" in text
    assert text.rstrip().endswith('"' + "a" * 200 + '"')
    assert "Today is 2026-09-23 in timezone America/Sao_Paulo" in text


# --- grounding ----------------------------------------------------------------


PACK = {"income": 5234.5, "savings_rate": 0.2748, "window": {"days": 30}, "items": [1, 2, 3, 4, 5]}


@pytest.mark.parametrize(
    "text, ok",
    [
        ("Income was R$ 5.234,50 this period.", True),
        ("Income was 5,234.50 BRL.", True),
        ("Income was 5 234,50.", True),
        ("The savings rate was 27.5%.", True),
        ("Over 30 days you saved.", True),
        ("Across 5 categories.", True),
        ("Income was −5,234.50 (negative).", True),
        ("Income was about 5.2k.", False),
        ("The savings rate was 28%.", False),
        ("Fees were 12% of income.", False),
        ("On 2026-09-01 at 07:30 you earned 5,234.50.", True),
        ("Back in 2019 things differed; income 5,234.50.", True),
        ("You spent 9,999.99.", False),
    ],
)
def test_grounding_examples(text, ok):
    assert (ungrounded(text, PACK) == []) is ok


def test_grounding_reports_each_offender_once_and_skips_small_ints():
    assert ungrounded("3 sentences, 7 items, 12 months", PACK) == []
    assert ungrounded("bad 77 and 77 again plus 88", PACK) == ["77", "88"]
    assert extract_numbers("just words") == []
    assert 30.0 in pack_values(PACK) and 27.48 in pack_values(PACK) and -5234.5 in pack_values(PACK)


# --- handlers (end to end through answer) -----------------------------------------


async def test_compare_periods_answer_persists_rows_and_grounded_narration(session: AsyncSession, test_user, test_workspace, test_agent):
    await _seed_summary_rows(session, test_user.id, test_workspace.id)  # income 1000, expense 30, invested 225, dated today
    today = date.today()
    provider = _ScriptedProvider([_text_turn("Net was 970.00 BRL this month versus 0.00 BRL last month, so income covered spending.")])
    ctx = await _ctx(session, test_user, test_workspace, test_agent, provider, today=today)
    decision = RouteDecision(intent="compare_periods", confidence=0.95, period_a="this_month", period_b="last_month")

    events = [ev async for ev in answer(ctx, decision, user_message="How did this month compare to last month?")]

    kinds = [ev.type for ev in events]
    assert kinds == ["tool_call", "tool_call", "tool_result", "tool_result", "text_delta", "text_delta"]
    assert all(ev.tool_name == "securo__get_transactions_summary" for ev in events[:4])
    head = events[4].text
    assert "1,000.00 BRL" in head and "970.00 BRL" in head and "```securo-chart" in head
    chart = json.loads(head.split("```securo-chart\n", 1)[1].split("\n```", 1)[0])
    assert chart["type"] == "bar" and len(chart["data"]) == 4 and [s["key"] for s in chart["series"]] == ["a", "b"]
    assert set(chart) <= {"type", "title", "subtitle", "currency", "x_label", "y_label", "data", "series"}
    assert events[5].text.strip().startswith("Net was 970.00 BRL")

    pack = events[2].tool_result["data"]
    tool_a = await REGISTRY["get_transactions_summary"].handler(
        session=session, ctx=CallContext(user_id=test_user.id, workspace_id=test_workspace.id),
        from_date=today.replace(day=1).isoformat(), to_date=today.isoformat(),
    )
    assert pack["a"]["income"] == tool_a["income"] == 1000.0
    assert pack["a"]["net"] == tool_a["net"] == 970.0
    assert pack["a"]["invested"] == tool_a["invested"] == 225.0
    assert pack["a"]["savings_rate_pct"] == 97.0 and pack["b"]["income"] == 0.0

    rows = await _rows(session, ctx.conversation_id)
    assert [r.role for r in rows] == ["user", "assistant", "tool", "tool", "assistant"]
    tool_ids = [tc["id"] for tc in rows[1].tool_calls]
    assert rows[1].content is None and all(i.startswith("guided_") for i in tool_ids)
    assert [r.tool_result["tool_call_id"] for r in rows[2:4]] == tool_ids
    assert rows[2].tool_result["data"]["kind"] == "compare_periods"
    assert rows[4].content.endswith("Net was 970.00 BRL this month versus 0.00 BRL last month, so income covered spending.")
    usage = (await session.execute(select(LlmUsage).where(LlmUsage.conversation_id == ctx.conversation_id))).scalars().all()
    assert [u.kind for u in usage] == ["guided"] and usage[0].message_id == rows[4].id
    assert provider.calls[0]["reasoning"] == "low" and provider.calls[0]["tools"] is None and "response_format" not in provider.calls[0]


async def test_narration_rejected_twice_uses_fallback_sentence(session, test_user, test_workspace, test_agent):
    await _seed_summary_rows(session, test_user.id, test_workspace.id)
    provider = _ScriptedProvider([_text_turn("You earned roughly 1.2k and saved 45%."), _text_turn("About 1,234.56 BRL this time.")])
    ctx = await _ctx(session, test_user, test_workspace, test_agent, provider, today=date.today())
    decision = RouteDecision(intent="compare_periods", confidence=0.9, period_a="this_month", period_b="last_month")
    events = [ev async for ev in answer(ctx, decision, user_message="compare")]
    narration = events[-1].text.strip()
    assert narration.startswith("Net was 970.00 BRL in")
    assert len(provider.calls) == 2  # one regeneration, then the templated sentence
    rows = await _rows(session, ctx.conversation_id)
    assert rows[-1].content.endswith(narration)


async def test_narration_provider_error_uses_fallback(session, test_user, test_workspace, test_agent):
    await _seed_summary_rows(session, test_user.id, test_workspace.id)
    ctx = await _ctx(session, test_user, test_workspace, test_agent, _FailingProvider(), today=date.today(), language="pt-BR")
    decision = RouteDecision(intent="compare_periods", confidence=0.9, period_a="this_month", period_b="last_month")
    events = [ev async for ev in answer(ctx, decision, user_message="compare")]
    assert events[-1].text.strip().startswith("O saldo foi 970.00 BRL em")
    usage = (await session.execute(select(LlmUsage).where(LlmUsage.conversation_id == ctx.conversation_id))).scalars().all()
    assert usage == []


async def test_handler_exception_raises_fallthrough_before_any_event(session, test_user, test_workspace, test_agent):
    provider = _ScriptedProvider([])
    ctx = await _ctx(session, test_user, test_workspace, test_agent, provider)
    decision = RouteDecision(intent="money_map", confidence=0.9, days=30)
    gen = answer(ctx, decision, user_message="where")
    with patch("app.agents.runtime.guided.call_local_tool", side_effect=RuntimeError("boom")):
        with pytest.raises(GuidedFallthrough):
            await gen.__anext__()
    assert provider.calls == []
    assert [r.role for r in await _rows(session, ctx.conversation_id)] == ["user"]


async def test_unknown_intent_and_future_period_fall_through(session, test_user, test_workspace, test_agent):
    ctx = await _ctx(session, test_user, test_workspace, test_agent, _ScriptedProvider([]))
    with pytest.raises(GuidedFallthrough):
        await answer(ctx, RouteDecision(intent="freeform", confidence=0.1), user_message="x").__anext__()
    with pytest.raises(GuidedFallthrough):
        await answer(ctx, RouteDecision(intent="spending_breakdown", confidence=0.9, period_a="month:2030-01"), user_message="x").__anext__()


async def test_spending_breakdown_pack_table_and_chart(session: AsyncSession, test_user, test_workspace, test_agent):
    uid, wid = test_user.id, test_workspace.id
    checking = await _account(session, uid, wid, "Checking", "checking")
    food = await _category(session, uid, wid, "Food", transfer=False)
    rent = await _category(session, uid, wid, "Rent", transfer=False)
    transfer = await _category(session, uid, wid, "Internal transfer", transfer=True)
    session.add_all([
        _txn(uid, wid, checking, 120.5, "debit", food),
        _txn(uid, wid, checking, 79.5, "debit", food),
        _txn(uid, wid, checking, 800, "debit", rent),
        _txn(uid, wid, checking, 500, "debit", transfer),  # not P&L
        _txn(uid, wid, checking, 3000, "credit"),
    ])
    await session.commit()
    provider = _ScriptedProvider([_text_turn("Rent took 800.00 BRL of the 1,000.00 BRL you spent.")])
    ctx = await _ctx(session, test_user, test_workspace, test_agent, provider, today=date.today())
    decision = RouteDecision(intent="spending_breakdown", confidence=0.9, period_a="this_month", category="food")
    prep = await guided.HANDLERS["spending_breakdown"](ctx, decision)
    assert prep.pack["total"] == 1000.0
    assert [(i["category"], i["amount"], i["share_pct"]) for i in prep.pack["items"]] == [("Rent", 800.0, 80.0), ("Food", 200.0, 20.0)]
    assert prep.pack["focus"] == {"category": "Food", "amount": 200.0, "share_pct": 20.0}
    assert prep.chart["type"] == "pie" and prep.chart["data"] == [{"name": "Rent", "value": 800.0}, {"name": "Food", "value": 200.0}]
    assert "| Rent | 800.00 BRL | 80.0% |" in prep.table_md and "**1,000.00 BRL**" in prep.table_md
    events = [ev async for ev in answer(ctx, decision, user_message="what did I spend on")]
    assert events[-1].text.strip() == "Rent took 800.00 BRL of the 1,000.00 BRL you spent."


async def test_money_map_handler_mirrors_tool_totals(session: AsyncSession, test_user, test_workspace, test_agent):
    await _seed_summary_rows(session, test_user.id, test_workspace.id)
    ctx = await _ctx(session, test_user, test_workspace, test_agent, _ScriptedProvider([]), today=date.today())
    prep = await guided.HANDLERS["money_map"](ctx, RouteDecision(intent="money_map", confidence=0.9, days=90))
    tool = await REGISTRY["get_money_map"].handler(session=session, ctx=CallContext(user_id=test_user.id, workspace_id=test_workspace.id), days=90)
    totals = tool["totals"]
    assert prep.pack["totals"]["income"] == totals["income"] and prep.pack["totals"]["net"] == totals["net"]
    assert prep.pack["window"]["days"] == 90 and prep.pack["kind"] == "money_map"
    assert prep.chart["type"] == "bar" and len(prep.chart["data"]) == 6
    assert prep.tool_trace == [("get_money_map", {"days": 90})]
    assert "| Income |" in prep.table_md or "| Receitas |" in prep.table_md


async def test_net_worth_trend_handler(session: AsyncSession, test_user, test_workspace, test_agent):
    await _seed_summary_rows(session, test_user.id, test_workspace.id)
    ctx = await _ctx(session, test_user, test_workspace, test_agent, _ScriptedProvider([]), today=date.today())
    prep = await guided.HANDLERS["net_worth_trend"](ctx, RouteDecision(intent="net_worth_trend", confidence=0.9, months=6))
    pack = prep.pack
    assert pack["kind"] == "net_worth_trend" and pack["months"] == 6 and len(pack["points"]) >= 2
    assert pack["first"] == pack["points"][0] and pack["last"] == pack["points"][-1]
    assert pack["delta"] == round(pack["last"]["value"] - pack["first"]["value"], 2)
    assert prep.chart["type"] == "line" and prep.chart["data"][0]["x"] == pack["points"][0]["date"]
    assert prep.tool_trace == [("get_net_worth", {"months": 6, "interval": "monthly"})]


async def test_fire_progress_handler(session: AsyncSession, test_user, test_workspace, test_agent):
    await _seed_summary_rows(session, test_user.id, test_workspace.id)  # annual spend 30, contribution 225
    ctx = await _ctx(session, test_user, test_workspace, test_agent, _ScriptedProvider([]), today=date.today())
    prep = await guided.HANDLERS["fire_progress"](ctx, RouteDecision(intent="fire_progress", confidence=0.9))
    pack = prep.pack
    assert pack["kind"] == "fire_progress" and pack["inputs"]["annual_spend"] == 30.0 and pack["inputs"]["annual_contribution"] == 225.0
    assert pack["fi_number"] == 750.0 and pack["inputs"]["withdrawal_rate_pct"] == 4.0 and pack["inputs"]["real_return_pct"] == 5.0
    assert "trailing 365 days" in pack["sources"]["annual_spend"]
    assert pack["years_to_fi"] is not None and pack["progress_pct"] is not None
    assert prep.chart is None or prep.chart["type"] == "area"
    assert "**FI number**" in prep.table_md and "750.00 BRL" in prep.table_md


async def test_holdings_handler(session: AsyncSession, test_user, test_workspace, test_agent):
    uid, wid = test_user.id, test_workspace.id
    brokerage = await _account(session, uid, wid, "Brokerage", "investment")
    ira = await _account(session, uid, wid, "IRA", "investment")
    checking = await _account(session, uid, wid, "Checking", "checking")
    brokerage.balance, ira.balance, checking.balance = Decimal("1500"), Decimal("500"), Decimal("100")
    await session.commit()
    ctx = await _ctx(session, test_user, test_workspace, test_agent, _ScriptedProvider([]), today=date.today())
    prep = await guided.HANDLERS["holdings"](ctx, RouteDecision(intent="holdings", confidence=0.9))
    pack = prep.pack
    assert {a["name"] for a in pack["accounts"]} == {brokerage.name, ira.name}
    tool = await REGISTRY["get_holdings"].handler(session=session, ctx=CallContext(user_id=uid, workspace_id=wid))
    assert pack["accounts_total_primary"] == tool["investment_accounts_total_primary"]
    assert prep.chart is None or (prep.chart["type"] == "pie" and len(prep.chart["data"]) == 2)
    assert "| Brokerage |" in prep.table_md and prep.tool_trace == [("get_holdings", {})]


async def _seed_portfolio(session, test_user, test_workspace):
    """Brokerage 1500 (Apple Inc/AAPL 900 + VTI 600), Roth IRA 500 (VTI 500), an empty Old 401(k); total 2000."""
    uid, wid = test_user.id, test_workspace.id
    brokerage = await _account(session, uid, wid, "Brokerage", "investment")
    ira = await _account(session, uid, wid, "Roth IRA | Econify", "investment")
    old = await _account(session, uid, wid, "Old 401(k)", "investment")
    activity = await _category(session, uid, wid, "Brokerage activity")
    # balances are derived from transactions (not Account.balance), like the finance-tool tests do
    session.add(_txn(uid, wid, brokerage, 1500, "credit", activity))
    session.add(_txn(uid, wid, ira, 500, "credit", activity))
    await session.commit()
    apple = await _asset(session, uid, wid, "Apple Inc", 900, account_id=brokerage.id)
    apple.ticker = "AAPL"
    vti_b = await _asset(session, uid, wid, "VTI", 600, account_id=brokerage.id)
    vti_b.ticker = "VTI"
    vti_i = await _asset(session, uid, wid, "VTI", 500, account_id=ira.id)
    vti_i.ticker = "VTI"
    await session.commit()
    return brokerage, ira, old


async def test_route_parses_query_and_analysis_slots(session, test_user, test_workspace, test_agent):
    provider = _ScriptedProvider([
        _text_turn(_route_json(intent="holding_lookup", query="Apple", analysis=False, confidence=0.95)),
        _text_turn(_route_json(intent="holdings", query="x" * 120, analysis="true", confidence=0.9)),
    ])
    ctx = await _ctx(session, test_user, test_workspace, test_agent, provider)
    d1 = await route(ctx, user_message="do I own apple stocks?")
    assert (d1.intent, d1.query, d1.analysis) == ("holding_lookup", "Apple", False)
    d2 = await route(ctx, user_message="any patterns?")
    assert (d2.intent, d2.query, d2.analysis) == ("holdings", None, True)
    assert "holding_lookup" in guided.INTENTS and "query" in guided.ROUTER_SCHEMA["required"] and "analysis" in guided.ROUTER_SCHEMA["required"]


def test_router_system_prompt_has_lookup_and_analysis_examples():
    text = guided.router_system(today=TODAY, tz="UTC", language="en", prior_user_message=None)
    assert '"Do I own Apple stocks?" -> {"intent":"holding_lookup"' in text and '"query":"Apple"' in text
    assert '"How much NVDA do I have and where?" -> {"intent":"holding_lookup"' in text
    assert 'do you see any patterns emerge?" -> {"intent":"holdings"' in text and '"analysis":true' in text
    assert '"What stands out in my spending this year?" -> {"intent":"spending_breakdown","period_a":"ytd"' in text
    assert '"How much did I pay Uber in June?" -> {"intent":"freeform"' in text


async def test_holdings_pack_positions_concentration_and_split(session: AsyncSession, test_user, test_workspace, test_agent):
    await _seed_portfolio(session, test_user, test_workspace)
    ctx = await _ctx(session, test_user, test_workspace, test_agent, _ScriptedProvider([]), today=date.today())
    prep = await guided.HANDLERS["holdings"](ctx, RouteDecision(intent="holdings", confidence=0.9))
    pack = prep.pack
    assert pack["accounts_total_primary"] == 2000.0
    assert {a["name"]: a["share_pct"] for a in pack["accounts"]} == {"Brokerage": 75.0, "Roth IRA | Econify": 25.0, "Old 401(k)": 0.0}
    assert [(p["ticker"], p["account"], p["value"], p["share_pct"]) for p in pack["positions"]] == [
        ("AAPL", "Brokerage", 900.0, 45.0), ("VTI", "Brokerage", 600.0, 30.0), ("VTI", "Roth IRA | Econify", 500.0, 25.0),
    ]
    assert pack["positions_total_count"] == 3 and pack["accounts_with_positions"] == 2
    assert pack["zero_balance_accounts"] == ["Old 401(k)"] and pack["zero_balance_count"] == 1
    conc = pack["concentration"]
    assert conc["largest_position"]["ticker"] == "AAPL" and conc["largest_position"]["share_pct"] == 45.0
    assert conc["top5_share_pct"] == 100.0 and conc["largest_account"] == {"name": "Brokerage", "share_pct": 75.0}
    assert pack["split"] == {"retirement_pct": 25.0, "taxable_pct": 75.0, "crypto_pct": 0.0}
    assert "**Top positions**" in prep.table_md
    assert "| Apple Inc (AAPL) | Brokerage | 900.00 BRL | 45.0% |" in prep.table_md
    assert "| Roth IRA \\| Econify |" in prep.table_md  # pipes in account names are escaped
    assert "largest is Brokerage (75.0%)" in prep.fallback_sentence and "Apple Inc (AAPL) (45.0%)" in prep.fallback_sentence


async def test_holding_lookup_matches_by_ticker_name_and_alias(session: AsyncSession, test_user, test_workspace, test_agent):
    await _seed_portfolio(session, test_user, test_workspace)
    ctx = await _ctx(session, test_user, test_workspace, test_agent, _ScriptedProvider([]), today=date.today())
    for query in ("aapl", "Apple", "the apple stock"):
        prep = await guided.HANDLERS["holding_lookup"](ctx, RouteDecision(intent="holding_lookup", confidence=0.95, query=query))
        assert [m["ticker"] for m in prep.pack["matches"]] == ["AAPL"], query
        assert prep.pack["matches_total_value"] == 900.0 and prep.pack["matches_share_pct"] == 45.0
        assert prep.narrate is True and prep.chart is None
        assert prep.fallback_sentence.startswith("Yes — you hold AAPL in 1 account(s): Brokerage 900.00 BRL; worth 900.00 BRL (45.0% of your investment accounts).")
        assert "| Apple Inc (AAPL) | Brokerage | " in prep.table_md
    prep = await guided.HANDLERS["holding_lookup"](ctx, RouteDecision(intent="holding_lookup", confidence=0.95, query="VTI"))
    assert [m["account"] for m in prep.pack["matches"]] == ["Brokerage", "Roth IRA | Econify"]
    assert "in 2 account(s)" in prep.fallback_sentence and prep.pack["matches_total_value"] == 1100.0


async def test_holding_lookup_no_match_answers_without_the_model(session: AsyncSession, test_user, test_workspace, test_agent):
    await _seed_portfolio(session, test_user, test_workspace)
    provider = _ScriptedProvider([_text_turn("should not be used")])
    ctx = await _ctx(session, test_user, test_workspace, test_agent, provider, today=date.today())
    decision = RouteDecision(intent="holding_lookup", confidence=0.95, query="Tesla")
    events = [ev async for ev in answer(ctx, decision, user_message="do I own tesla?")]
    assert provider.calls == []
    text = events[-1].text.strip()
    assert text.startswith("No position matching 'Tesla' in any account that exposes holdings (Brokerage, Roth IRA | Econify); 1 account(s) do not expose positions (Old 401(k)).")
    assert events[-1].type == "text_delta" and events[-2].text.startswith("| Accounts that expose positions |")
    rows = await _rows(session, ctx.conversation_id)
    assert rows[-1].content.endswith(text) and rows[2].tool_result["data"]["kind"] == "holding_lookup"
    usage = (await session.execute(select(LlmUsage).where(LlmUsage.conversation_id == ctx.conversation_id))).scalars().all()
    assert usage == []


async def test_holding_lookup_without_query_behaves_like_holdings(session: AsyncSession, test_user, test_workspace, test_agent):
    await _seed_portfolio(session, test_user, test_workspace)
    ctx = await _ctx(session, test_user, test_workspace, test_agent, _ScriptedProvider([]), today=date.today())
    prep = await guided.HANDLERS["holding_lookup"](ctx, RouteDecision(intent="holding_lookup", confidence=0.9, query=None))
    assert prep.pack["kind"] == "holdings" and prep.chart is not None


async def test_analysis_mode_uses_medium_reasoning_and_keeps_grounding(session: AsyncSession, test_user, test_workspace, test_agent):
    await _seed_portfolio(session, test_user, test_workspace)
    provider = _ScriptedProvider([_text_turn("Brokerage carries 75.0% of the total and Apple alone is 45.0%, so the taxable side is concentrated in one name; the Roth IRA (25.0%) is your only retirement exposure.")])
    ctx = await _ctx(session, test_user, test_workspace, test_agent, provider, today=date.today())
    decision = RouteDecision(intent="holdings", confidence=0.92, analysis=True)
    events = [ev async for ev in answer(ctx, decision, user_message="looking at my holdings do you see any patterns?")]
    call = provider.calls[0]
    assert call["reasoning"] == "medium" and call["max_tokens"] == 520 and call["tools"] is None
    assert events[-1].text.strip().startswith("Brokerage carries 75.0%")

    bad = _ScriptedProvider([_text_turn("You are 62% in one stock."), _text_turn("Roughly 3.4k sits idle.")])
    ctx2 = await _ctx(session, test_user, test_workspace, test_agent, bad, today=date.today())
    events2 = [ev async for ev in answer(ctx2, decision, user_message="patterns?")]
    assert len(bad.calls) == 2 and all(c["reasoning"] == "medium" for c in bad.calls)
    assert events2[-1].text.strip().startswith("Your investment accounts total 2,000.00 BRL across 3 accounts; the largest is Brokerage (75.0%)")


def test_md_table_escapes_pipes_and_newlines():
    table = guided._md_table(["A", "B"], [["x | y", "line1\nline2"]])
    assert "| x \\| y | line1 line2 |" in table
    assert guided._cell(None) == ""


def test_render_pack_lines_formats_money_percent_and_counts():
    # human labels: the narrator must never see machine paths like `a.income` to parrot back
    lines = guided._render_pack_lines({
        "kind": "compare_periods", "currency": "BRL",
        "a": {"label": "Sep 2026", "income": 1000.0, "savings_rate_pct": 97.0},
        "delta": {"savings_rate_pts": -3.5}, "window": {"days": 30}, "points": [{"date": "2026-01", "value": 1.0}] * 15,
    })
    assert "- Income (Sep 2026): 1,000.00 BRL" in lines
    assert "- Savings rate (Sep 2026): 97.0%" in lines
    assert "- Savings rate (pts) change: -3.5 pts" in lines
    assert "- Days: 30" in lines
    assert "- Points: 15 entries (first 12 listed)" in lines
