"""Advisor answers in guided mode: period summary, spending insights, invest plan,
portfolio insights, and the advisor rows on FIRE / money-map answers."""
from datetime import date, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.runtime import advice, guided
from app.agents.runtime.grounding import ungrounded
from app.agents.runtime.guided import RouteDecision
from app.agents.services.advisor_figures import category_spikes, classify_asset_class, detect_recurring
from mcp_server.registry import REGISTRY
from tests.test_agents_executor import _ScriptedProvider
from tests.test_agents_guided import _ctx, _seed_portfolio
from tests.test_mcp_finance_tools import _account, _category, _ensure_to_char, _seed_summary_rows, _txn  # noqa: F401  (autouse to_char shim)


@pytest.mark.parametrize("name, ticker, security_type, account, expected", [
    ("Vanguard S&P 500 ETF", "VOO", None, "", "broad"),
    ("Vanguard Total Stock Market Index Fund", "VTI", None, "", "broad"),
    ("Invesco QQQ Trust", "QQQ", None, "", "fund_other"),
    ("Vanguard Information Technology Index Fund", "VGT", None, "", "fund_other"),
    ("189.745 shares of PLTR", "PLTR", None, "Robinhood individual", "stock"),
    ("36.864 shares of VOO", "VOO", None, "Robinhood individual", "broad"),
    ("Bitcoin", "BTC", None, "Crypto (0990)", "crypto"),
    ("0.5 shares of SOL", "SOL", None, "Crypto", "crypto"),
    ("Fidelity Government Money Market", "SPAXX", None, "", "cash"),
    ("iShares Core U.S. Aggregate Bond ETF", "AGG", None, "", "bond"),
    ("Schwab U.S. Dividend Equity ETF", "SCHD", "etf", "", "fund_other"),
    ("NVIDIA Corp", "NVDA", "equity", "", "stock"),
    ("Collective Investment Trust 2045", None, None, "Bloomberg 401(k)", "fund_other"),
    ("Mystery holding", None, None, "", "unknown"),
])
def test_asset_classes_follow_the_plan_order(name, ticker, security_type, account, expected):
    assert classify_asset_class(name, ticker, security_type, account) == expected


def _months(values_by_month):
    return {date(2026, m, 1): v for m, v in values_by_month.items()}


def test_category_spikes_edges_and_short_history():
    base = {m: {"Travel": 400.0} for m in (3, 4, 5, 6, 7, 8)}
    assert category_spikes(_months({**base, 9: {"Travel": 596.0}}), date(2026, 9, 1)) == []          # 1.49x
    spikes = category_spikes(_months({**base, 9: {"Travel": 604.0}}), date(2026, 9, 1))               # 1.51x, +204
    assert spikes == [{"category": "Travel", "amount": 604.0, "baseline": 400.0, "excess": 204.0}]
    assert category_spikes(_months({**base, 9: {"Uncategorized": 5000.0}}), date(2026, 9, 1)) == []  # never a lever
    assert category_spikes(_months({7: {"Travel": 400.0}, 8: {"Travel": 400.0}, 9: {"Travel": 999.0}}), date(2026, 9, 1)) is None
    sparse = {**{m: {} for m in (3, 4, 5, 6)}, 7: {"Gifts": 300.0}, 8: {"Gifts": 300.0}, 9: {"Gifts": 900.0}}
    assert category_spikes(_months(sparse), date(2026, 9, 1)) == []  # fewer than 3 nonzero baseline months


def test_recurring_needs_three_of_four_months_and_a_steady_amount():
    today = date(2026, 10, 9)
    rows = [(date(2026, m, 3), "NETFLIX.COM 866-579", 15.49) for m in (6, 7, 8, 9)]
    rows += [(date(2026, m, 5), "GYM CLUB", a) for m, a in ((7, 40.0), (8, 41.0), (9, 39.0))]
    rows += [(date(2026, m, 9), "RANDOM SHOP", a) for m, a in ((8, 20.0), (9, 80.0), (7, 5.0))]
    rows += [(date(2026, 9, 1), "ONE OFF", 300.0)]
    found = {r["merchant"]: r for r in detect_recurring(rows, today)}
    assert set(found) == {"NETFLIX.COM", "GYM CLUB"}  # RANDOM SHOP's amounts swing; ONE OFF is one month
    assert found["NETFLIX.COM"]["monthly"] == 15.49 and found["GYM CLUB"]["monthly"] == 40.0


async def test_period_summary_answers_one_period_without_the_model(session: AsyncSession, test_user, test_workspace, test_agent):
    await _seed_summary_rows(session, test_user.id, test_workspace.id)  # income 1000, expense 30, invested 225 today
    ctx = await _ctx(session, test_user, test_workspace, test_agent, _ScriptedProvider([]), today=date.today())
    prep = await guided.HANDLERS["period_summary"](ctx, RouteDecision(intent="period_summary", confidence=0.9, period_a="ytd"))
    pack = prep.pack
    assert (pack["income"], pack["expense"], pack["net"], pack["invested"]) == (1000.0, 30.0, 970.0, 225.0)
    assert prep.narrate is False and "next_move" not in pack
    assert prep.fallback_sentence.startswith(f"In {date.today().year} YTD: income 1,000.00 BRL, expenses 30.00 BRL, net 970.00 BRL, invested 225.00 BRL")


async def test_invest_plan_agrees_with_fire_projection_and_grounds_its_next_move(session: AsyncSession, test_user, test_workspace, test_agent):
    await _seed_summary_rows(session, test_user.id, test_workspace.id)
    await _seed_portfolio(session, test_user, test_workspace)
    ctx = await _ctx(session, test_user, test_workspace, test_agent, _ScriptedProvider([]), today=date.today())
    ctx.user_message = "How much should I be putting away each month?"
    prep = await guided.HANDLERS["invest_plan"](ctx, RouteDecision(intent="invest_plan", confidence=0.9))
    fire = await REGISTRY["fire_projection"].handler(session=session, ctx=guided._call_ctx(ctx))
    pack = prep.pack
    assert pack["fi_number"] == fire["fi_number"] and pack["invested_assets"] == fire["inputs"]["invested_assets"]
    assert pack["years_to_fi"] == (fire["years_to_fi"] if fire["years_to_fi"] is not None and fire["years_to_fi"] <= 60 else None)
    assert [r["extra"] for r in pack["ladder"]] == [500.0, 1000.0, 2000.0]
    move = pack["next_move"]
    assert move["text"] and ungrounded(move["text"], advice.grounding_pack(move)) == []
    assert "If you invest more each month" in prep.table_md and prep.chart is not None


async def test_invest_plan_reports_invested_in_a_named_period(session: AsyncSession, test_user, test_workspace, test_agent):
    await _seed_summary_rows(session, test_user.id, test_workspace.id)
    ctx = await _ctx(session, test_user, test_workspace, test_agent, _ScriptedProvider([]), today=date.today())
    prep = await guided.HANDLERS["invest_plan"](ctx, RouteDecision(intent="invest_plan", confidence=0.9, period_a="ytd"))
    assert prep.pack["period"]["invested"] == 225.0 and "Invested in" in prep.table_md


async def test_portfolio_insights_mix_accounts_and_the_concentration_move(session: AsyncSession, test_user, test_workspace, test_agent):
    await _seed_portfolio(session, test_user, test_workspace)  # Brokerage: Apple Inc/AAPL 900 + VTI 600; Roth IRA: VTI 500
    ctx = await _ctx(session, test_user, test_workspace, test_agent, _ScriptedProvider([]), today=date.today())
    ctx.user_message = "Is my portfolio diversified?"
    prep = await guided.HANDLERS["portfolio_insights"](ctx, RouteDecision(intent="portfolio_insights", confidence=0.9, analysis=True))
    pack = prep.pack
    mix = {m["label"]: m["share_pct"] for m in pack["mix"]}
    assert mix == {"Broad index funds": 55.0, "Single stocks": 45.0}
    assert pack["largest_account"]["name"] == "Brokerage" and pack["stock_share_pct"] == 45.0
    assert pack["next_move"]["rule"] == "single_position"  # "diversified" focuses the concentration rules
    assert "Brokerage" in prep.table_md and prep.chart is not None


async def test_spending_insights_says_when_history_is_too_short(session: AsyncSession, test_user, test_workspace, test_agent):
    uid, wid = test_user.id, test_workspace.id
    checking = await _account(session, uid, wid, "Checking", "checking")
    dining = await _category(session, uid, wid, "Dining & delivery", transfer=False)
    rent = await _category(session, uid, wid, "Rent", transfer=False)
    today = date.today()
    session.add_all([
        _txn(uid, wid, checking, 3000, "credit", when=today - timedelta(days=50)),
        _txn(uid, wid, checking, 600, "debit", dining, when=today - timedelta(days=40)),
        _txn(uid, wid, checking, 1200, "debit", rent, when=today - timedelta(days=40)),
        _txn(uid, wid, checking, 400, "debit", dining, when=today - timedelta(days=5)),
    ])
    await session.commit()
    ctx = await _ctx(session, test_user, test_workspace, test_agent, _ScriptedProvider([]), today=today)
    ctx.user_message = "Where am I overspending?"
    prep = await guided.HANDLERS["spending_insights"](ctx, RouteDecision(intent="spending_insights", confidence=0.9, analysis=True))
    pack = prep.pack
    assert "spikes" not in pack and "Not enough complete months" in prep.table_md
    assert pack["top_controllable"]["category"] == "Dining & delivery"  # rent is essential
    assert pack["next_move"]["rule"] in {"spending_cut", "cash_floor"}
    assert ungrounded(pack["next_move"]["text"], advice.grounding_pack(pack["next_move"])) == []


async def test_fire_progress_gets_the_advisor_rows_and_switch_turns_them_off(session: AsyncSession, test_user, test_workspace, test_agent):
    await _seed_summary_rows(session, test_user.id, test_workspace.id)
    ctx = await _ctx(session, test_user, test_workspace, test_agent, _ScriptedProvider([]), today=date.today())
    ctx.user_message = "How far am I from FI?"
    prep = await guided.HANDLERS["fire_progress"](ctx, RouteDecision(intent="fire_progress", confidence=0.9))
    assert "Advisor figures" in prep.table_md and "years_with_1000_more" in prep.pack["advisor"]
    ctx.settings = ctx.settings.model_copy(update={"advisor_voice": False})
    plain = await guided.HANDLERS["fire_progress"](ctx, RouteDecision(intent="fire_progress", confidence=0.9))
    assert "Advisor figures" not in plain.table_md and "next_move" not in plain.pack and "advisor" not in plain.pack


async def test_route_parses_the_advisor_intents(session, test_user, test_workspace, test_agent):
    from tests.test_agents_guided import _route_json, _text_turn

    intents = ["period_summary", "spending_insights", "invest_plan", "portfolio_insights"]
    provider = _ScriptedProvider([_text_turn(_route_json(intent=i, period_a="ytd" if i == "period_summary" else None, analysis=i != "period_summary", confidence=0.9)) for i in intents])
    ctx = await _ctx(session, test_user, test_workspace, test_agent, provider)
    for intent in intents:
        decision = await guided.route(ctx, user_message="advisor question")
        assert decision.intent == intent and decision.confidence == 0.9


def test_router_prompt_describes_the_advisor_intents():
    text = guided.router_system(today=date(2026, 10, 9), tz="UTC", language="en", prior_user_message=None)
    for intent in ("period_summary", "spending_insights", "invest_plan", "portfolio_insights"):
        assert f"- {intent}:" in text and f'{{"intent":"{intent}"' in text
    assert "Not the current portfolio value — that is holdings." in text
