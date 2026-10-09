"""Advice rules (app/agents/runtime/advice.py) and the code-written "Next move" line.

The thresholds are policy the user approved; these tests pin each one just below and
just above its edge, the priority order, the focus fallback, and that every line Securo
appends is grounded in the figures it carries.
"""
import copy
from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.prompts import ADVISOR_VOICE, FINANCE_ANALYST_PROMPT, finance_analyst_prompt
from app.agents.runtime import advice, guided
from app.agents.runtime.grounding import ungrounded
from app.agents.runtime.guided import Prepared, RouteDecision, answer
from tests.test_agents_executor import _ScriptedProvider
from tests.test_agents_guided import _ctx, _rows, _text_turn


class _Capturing(_ScriptedProvider):
    """Records the system prompt of every call."""

    def __init__(self, turns):
        super().__init__(turns)
        self.systems: list[str] = []

    async def chat_stream(self, messages, **kwargs):
        self.systems.append(messages[0].content)
        async for chunk in super().chat_stream(messages, **kwargs):
            yield chunk


def money(v):
    return f"{v:,.2f} USD"


BASE = {
    "monthly_spend": 5000.0, "liquid_cash": 20000.0, "cards_with_interest": [], "interest_90": 0.0, "spikes": [],
    "savings_rate": 0.40, "band_next": 0.50, "band_cut": 800.0, "years_now": 15.0, "years_after_cut": 12.5,
    "retirement_cash": 0.0, "invested_assets": 500000.0, "crypto_value": 10000.0,
    "largest_stock": {"name": "Palantir", "ticker": "PLTR", "value": 30000.0}, "stock_total": 80000.0,
    "top_controllable": {"category": "Dining & delivery", "amount": 900.0, "half": 450.0}, "annual_contribution": 36000.0,
}


def facts(**overrides):
    f = copy.deepcopy(BASE)
    f.update(overrides)
    return f


def rules(f):
    return [r.rule for r in advice.recommend(f, money)]


def move_for(f, message):
    move = advice.next_move(f, message, money)
    assert move is not None
    return move


def test_base_household_fires_only_the_savings_rules_in_priority_order():
    assert rules(facts()) == ["spending_cut", "savings_band"]  # priorities 10 and 11


@pytest.mark.parametrize("overrides, rule, fires", [
    ({"cards_with_interest": [{"card": "Gold Card", "interest": 25.42, "owed": 0.0}]}, "card_carry", False),
    ({"cards_with_interest": [{"card": "Gold Card", "interest": 25.42, "owed": 120.0}]}, "card_carry", True),
    ({"liquid_cash": 15000.0}, "cash_floor", False),
    ({"liquid_cash": 14999.99}, "cash_floor", True),
    ({"liquid_cash": 32500.0}, "idle_cash", False),       # buffer = 6 x 5000 + max(1000, 2500)
    ({"liquid_cash": 32500.01}, "idle_cash", True),
    ({"spikes": None}, "spending_spike", False),
    ({"spikes": [{"category": "Travel", "amount": 900.0, "baseline": 400.0, "excess": 500.0}]}, "spending_spike", True),
    ({"retirement_cash": 25000.0}, "cash_drag", False),   # 5% of 500,000
    ({"retirement_cash": 25000.01}, "cash_drag", True),
    ({"crypto_value": 25000.0}, "crypto_cap", False),
    ({"crypto_value": 25000.01}, "crypto_cap", True),
    ({"largest_stock": {"name": "Palantir", "ticker": "PLTR", "value": 50000.0}}, "single_position", False),
    ({"largest_stock": {"name": "Palantir", "ticker": "PLTR", "value": 50000.01}}, "single_position", True),
    ({"stock_total": 100000.0}, "stock_share", False),
    ({"stock_total": 100000.01}, "stock_share", True),
    ({"savings_rate": 0.50}, "savings_band", False),      # at the band, not below it
    ({"savings_rate": 0.4999}, "savings_band", True),
])
def test_each_rule_fires_just_past_its_threshold(overrides, rule, fires):
    assert (rule in rules(facts(**overrides))) is fires


def test_savings_band_is_urgent_only_below_twenty_percent():
    low = {r.rule: r.priority for r in advice.recommend(facts(savings_rate=0.19, band_next=0.20), money)}
    high = {r.rule: r.priority for r in advice.recommend(facts(savings_rate=0.21, band_next=0.35), money)}
    assert low["savings_band"] == 6 and high["savings_band"] == 11


def test_keep_going_needs_the_top_band_and_nothing_serious():
    top = facts(savings_rate=0.70, band_next=None, band_cut=None)
    assert rules(top) == ["keep_going"]  # spending_cut is gated off at 65%+
    assert rules(facts(savings_rate=0.70, band_next=None, band_cut=None, stock_total=150000.0)) == ["stock_share"]


def test_missing_inputs_skip_rules_instead_of_guessing():
    f = facts()
    for key in ("monthly_spend", "savings_rate", "invested_assets"):
        f.pop(key)
    assert rules(f) == []


def test_next_move_prefers_the_questions_own_rule():
    move = move_for(facts(stock_total=150000.0), "Is too much of my portfolio in one stock?")
    assert move["rule"] == "stock_share" and move["text"].startswith("Individual stocks are 30.0% of your investments")


def test_next_move_states_a_verdict_then_the_biggest_lever():
    move = move_for(facts(), "Am I sitting on too much cash?")
    assert move["rule"] == "spending_cut"
    assert move["text"].startswith("Your cash, 20,000.00 USD, covers 4.0 months of spending: inside the 3 to 6 month range. Biggest lever: Dining & delivery runs 900.00 USD a month.")
    card = move_for(facts(interest_90=25.42), "Am I carrying credit card debt?")
    assert card["text"].startswith("You paid 25.42 USD in card interest in the last 90 days, but no card that charged it carries a balance now.")


def test_focus_matches_word_stems():
    assert advice.focus_rules("Where am I wasting money?") == {"spending_spike", "spending_cut"}
    assert advice.focus_rules("How much more should I invest each month to reach FI sooner?") == {"savings_band"}
    assert advice.focus_rules("What's the weather?") == set()


@pytest.mark.parametrize("overrides, message", [
    ({}, "Where am I wasting money?"),
    ({}, "Am I on track to retire early?"),
    ({}, "Am I sitting on too much cash?"),
    ({"cards_with_interest": [{"card": "Platinum Card® (4004)", "interest": 31.07, "owed": 2999.17}]}, "Am I carrying credit card debt?"),
    ({"liquid_cash": 9000.0}, "Do I have enough cash?"),
    ({"liquid_cash": 90000.0}, "Is my cash idle?"),
    ({"spikes": [{"category": "Travel", "amount": 912.34, "baseline": 401.2, "excess": 511.14}]}, "Did my spending jump?"),
    ({"retirement_cash": 40000.0}, "How am I investing?"),
    ({"crypto_value": 41234.56}, "How much crypto do I have?"),
    ({"largest_stock": {"name": "Palantir", "ticker": "PLTR", "value": 61234.5}, "stock_total": 61234.5}, "Am I diversified?"),
    ({"stock_total": 152030.45}, "Is too much in stocks?"),
    ({"savings_rate": 0.7012, "band_next": None, "band_cut": None}, "Is my savings rate high enough?"),
    ({"savings_rate": 0.1643, "band_next": 0.20, "band_cut": 554.33, "years_now": 21.75, "years_after_cut": 20.07}, "How much more should I invest?"),
])
def test_every_next_move_is_grounded_in_its_own_figures(overrides, message):
    move = move_for(facts(**overrides), message)
    assert move["text"]
    assert ungrounded(move["text"], advice.grounding_pack(move)) == []


def test_grounding_pack_never_contains_the_text_itself():
    move = move_for(facts(), "Where am I wasting money?")
    pack = advice.grounding_pack(move)
    assert "text" not in pack and move["text"] not in str(pack)
    tampered = move["text"].replace("450.00", "475.00")
    assert ungrounded(tampered, pack) == ["475.00"]


@pytest.mark.parametrize("line", [
    "Next move: buy AAPL", "**Next move:** sell PLTR", "- **Next move**: invest 500", "Próximo passo: corte 200", "* Próximo paso: ahorra 100",
])
def test_model_written_next_move_lines_are_removed(line):
    assert advice.strip_model_next_move(f"Your rate is 16.4%.\n\n{line}") == "Your rate is 16.4%."


def test_voice_is_brace_free_and_in_every_advisor_prompt():
    assert "{" not in ADVISOR_VOICE and "}" not in ADVISOR_VOICE
    assert ADVISOR_VOICE in FINANCE_ANALYST_PROMPT and ADVISOR_VOICE not in finance_analyst_prompt(voice=False)
    for template in (guided.NARRATION_SYSTEM_ADVISOR, guided.ANALYSIS_SYSTEM_ADVISOR):
        assert ADVISOR_VOICE in template and "Next move" in template
        assert template.format(agent_name="A", language="en", figures="- x: 1")
    assert ADVISOR_VOICE not in guided.NARRATION_SYSTEM and ADVISOR_VOICE not in guided.ANALYSIS_SYSTEM
    assert "the way an analyst would sum up a table" not in guided.NARRATION_SYSTEM_ADVISOR
    assert "what to do about it" in guided.ANALYSIS_SYSTEM_ADVISOR


def test_loop_prompt_asks_for_a_next_move_only_on_advice_questions():
    from app.agents.tasks.digest import DIGEST_INSTRUCTION

    assert FINANCE_ANALYST_PROMPT.count("Next move") == 1 and "When the user asks for advice" in FINANCE_ANALYST_PROMPT
    assert "Next move" not in DIGEST_INSTRUCTION


# --- answer(): the line Securo appends ------------------------------------------------


def _prep(narrate=True, text=None):
    move = move_for(facts(), "Where am I wasting money?")
    if text is not None:
        move["text"] = text
    pack = {"kind": "money_map", "currency": "USD", "rate_pct": 40.0, "next_move": move}
    return Prepared(pack, "| Figure | Value |\n|---|---|\n| Savings rate | 40.0% |", None, "Your savings rate is 40.0%.", [("get_money_map", {})], narrate=narrate)


async def _answer(session, test_user, test_workspace, test_agent, monkeypatch, prep, provider, *, voice=True):
    async def handler(ctx, decision):
        return prep

    monkeypatch.setitem(guided.HANDLERS, "money_map", handler)
    ctx = await _ctx(session, test_user, test_workspace, test_agent, provider, today=date.today())
    ctx.settings = ctx.settings.model_copy(update={"advisor_voice": voice})
    events = [ev async for ev in answer(ctx, RouteDecision(intent="money_map", confidence=0.9), user_message="where am I wasting money?")]
    rows = await _rows(session, ctx.conversation_id)
    return "".join(ev.text or "" for ev in events if ev.type == "text_delta"), rows[-1].content


async def test_narrated_answer_gets_exactly_one_code_written_next_move(session: AsyncSession, test_user, test_workspace, test_agent, monkeypatch):
    provider = _Capturing([_text_turn("Your savings rate is 40.0%: solid, not great.\n\nNext move: buy AAPL")])
    streamed, saved = await _answer(session, test_user, test_workspace, test_agent, monkeypatch, _prep(), provider)
    assert streamed.count("Next move") == 1 and "buy AAPL" not in streamed
    assert "**Next move:** Dining & delivery runs 900.00 USD a month. Cut it in half and invest the 450.00 USD." in streamed
    assert saved.strip() == streamed.strip()
    assert ADVISOR_VOICE in provider.systems[0]


async def test_unnarrated_and_fallback_answers_also_end_with_the_next_move(session: AsyncSession, test_user, test_workspace, test_agent, monkeypatch):
    streamed, _ = await _answer(session, test_user, test_workspace, test_agent, monkeypatch, _prep(narrate=False), _ScriptedProvider([]))
    assert streamed.startswith("Your savings rate is 40.0%.") and streamed.rstrip().endswith("invest the 450.00 USD.")
    bad = _ScriptedProvider([_text_turn("It is 99.9%."), _text_turn("Still 99.9%.")])  # rejected twice -> fallback sentence
    streamed, _ = await _answer(session, test_user, test_workspace, test_agent, monkeypatch, _prep(), bad)
    assert streamed.count("**Next move:**") == 1


async def test_kill_switch_and_ungrounded_lines_leave_no_next_move(session: AsyncSession, test_user, test_workspace, test_agent, monkeypatch):
    off = _Capturing([_text_turn("Your savings rate is 40.0%.")])
    streamed, _ = await _answer(session, test_user, test_workspace, test_agent, monkeypatch, _prep(), off, voice=False)
    assert "Next move" not in streamed and ADVISOR_VOICE not in off.systems[0]
    streamed, _ = await _answer(session, test_user, test_workspace, test_agent, monkeypatch,
                                _prep(narrate=False, text="Invest 12,345.00 USD today."), _ScriptedProvider([]))
    assert "Next move" not in streamed
