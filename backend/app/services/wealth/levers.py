"""This month's move, idle cash, the cash cushion and the top controllable
category (plan P3, decision 4) — built entirely from figures already
computed elsewhere in `wealth.*`. Pure, no I/O: every input here is a
number or an already-fetched figure; `app.api.wealth._wealth_snapshot` is
what reads the database and calls `compute` below once per request.

Re-implements the small pieces of `app.agents.services.advisor_figures.
advisor_facts` the wealth pages need (`top_controllable`, `band_next`/
`band_cut`, idle cash, "this month's move") without importing
`app.agents*` — `tests/test_wealth_isolation.py` enforces that the wealth
package never does.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from app.schemas.wealth import CashFlow, ThisMonthMove, TopControllable
from app.services.wealth import policy
from app.services.wealth.figures import TypicalFigures
from app.services.wealth.patterns import ESSENTIAL_RE, NON_CATEGORY_RE


def _money(amount: Optional[float], currency: str) -> str:
    """A minimal money formatter, local to this module on purpose: the richer
    one lives in `app.agents.services.digest_service`, which the wealth
    package must not import."""
    if amount is None:
        return "n/a"
    return f"{amount:,.2f} {currency}"


@dataclass(frozen=True)
class WealthLeverFigures:
    """Everything `/api/wealth/levers` needs, plus the pieces `/api/wealth/
    summary` (`idle_cash`, `cash_cushion_months`, `cash_flow.typical_monthly`)
    and `/api/wealth/projection` (`idle_cash`, `top_controllable.band_cut` as
    `top_cut_monthly`) read off the same computation."""

    cash: float
    buffer: float
    idle_cash: float
    cash_cushion_months: Optional[float]
    cash_flow: CashFlow
    top_controllable: Optional[TopControllable]
    move: Optional[ThisMonthMove]
    investing_exceeds_saving: bool


def _typical_savings_rate(typical: TypicalFigures) -> Optional[float]:
    income, expenses, direct = typical.income.typical, typical.expenses.typical, typical.direct.typical
    denom = income + direct
    return (income - expenses + direct) / denom if denom > 0 else None


def _band_next_and_cut(rate: Optional[float], typical: TypicalFigures) -> tuple[Optional[float], Optional[float]]:
    """The next `policy.SAVINGS_BANDS` band above the typical P1 savings
    rate, and the monthly spending cut that would reach it — the same
    identity `advisor_figures.advisor_facts` uses for `band_next`/`band_cut`,
    solved from the annualized typical income/expenses/direct instead of a
    raw period total (the two agree: both numerator and denominator scale by
    the same annualizing factor). `(None, None)` once already at the top
    band, or with no computable rate."""
    band_next = next((b for b in policy.SAVINGS_BANDS if rate is not None and rate < b), None)
    if band_next is None:
        return None, None
    income, expenses, direct = typical.income.typical, typical.expenses.typical, typical.direct.typical
    denom = income + direct
    band_cut = (band_next * denom - (income - expenses + direct)) / 12
    return band_next, round(band_cut, 2)


def _top_controllable(
    category_monthly: Mapping[str, float], band_cut: Optional[float],
) -> Optional[TopControllable]:
    """The largest monthly expense category outside `ESSENTIAL_RE`/
    `NON_CATEGORY_RE` (the plan's top-controllable rule); `None` with nothing
    controllable to report."""
    candidates = {
        name: amount for name, amount in category_monthly.items()
        if amount > 0 and not ESSENTIAL_RE.search(name or "") and not NON_CATEGORY_RE.match(name or "")
    }
    if not candidates:
        return None
    name, amount = max(candidates.items(), key=lambda kv: kv[1])
    return TopControllable(category=name, monthly=round(amount, 2), band_cut=band_cut)


def _this_month_move(
    *, median_monthly_spend: Optional[float], cash: float, savings_rate: Optional[float],
    band_next: Optional[float], band_cut: Optional[float], top: Optional[TopControllable],
    annual_contribution: float, currency: str,
) -> Optional[ThisMonthMove]:
    """`policy.next_move` on the wealth figures' facts (decision 4's Facts
    contract), with no question of its own (an empty message matches no
    `FOCUS` rule) — the single highest-priority rule that fires, not an
    answer to anything asked. For R1, holdings-based facts (crypto, stocks,
    retirement cash, card interest) are omitted by design; the rules that
    read them are simply skipped."""
    facts = {
        "monthly_spend": median_monthly_spend,
        "liquid_cash": cash,
        "savings_rate": savings_rate,
        "band_next": band_next,
        "band_cut": band_cut,
        "top_controllable": (
            {"category": top.category, "amount": top.monthly, "half": round(top.monthly / 2, 2)}
            if top is not None else None
        ),
        "annual_contribution": annual_contribution,
    }
    move = policy.next_move(facts, "", lambda v: _money(v, currency))
    if move is None:
        return None
    return ThisMonthMove(rule=move["rule"], text=move["text"], amount=move["amount"])


def compute(
    *, cash: float, median_monthly_spend: Optional[float], buffer_months: float,
    typical: TypicalFigures, category_monthly: Mapping[str, float], currency: str,
) -> WealthLeverFigures:
    """Every `WealthLevers` figure, from already-computed inputs (plan P3):
    `cash` is the latest point's `cash` bucket, `median_monthly_spend` is the
    median monthly expense over full months (one-offs excluded — see
    `figures.TypicalFigures.median_month.expenses`), `typical` is
    `figures.typical_figures`'s result, and `category_monthly` is
    `figures.typical_expense_by_category`'s result."""
    if median_monthly_spend is None:
        # No full month of spending yet: there is no buffer to measure cash
        # against, so nothing is called idle (never "invest all of it").
        buffer = 0.0
        idle_cash = 0.0
        cash_cushion_months = None
    else:
        buffer = buffer_months * median_monthly_spend
        idle_cash = max(0.0, cash - buffer)
        cash_cushion_months = cash / median_monthly_spend if median_monthly_spend > 0 else None

    median = typical.median_month
    average_monthly = (
        typical.income.all - typical.expenses.all - (typical.invested.all - typical.direct.all)
    ) / 12
    cash_flow = CashFlow(
        typical_monthly=median.cash_flow_after_investing, average_monthly=average_monthly,
        months=median.full_months,
    )
    investing_exceeds_saving = (
        median.cash_flow_after_investing is not None and median.cash_flow_after_investing < 0
    )

    rate = _typical_savings_rate(typical)
    band_next, band_cut = _band_next_and_cut(rate, typical)
    top = _top_controllable(category_monthly, band_cut)
    move = _this_month_move(
        median_monthly_spend=median_monthly_spend, cash=cash, savings_rate=rate,
        band_next=band_next, band_cut=band_cut, top=top,
        annual_contribution=typical.invested.typical, currency=currency,
    )

    return WealthLeverFigures(
        cash=cash, buffer=round(buffer, 2), idle_cash=round(idle_cash, 2),
        cash_cushion_months=cash_cushion_months, cash_flow=cash_flow,
        top_controllable=top, move=move, investing_exceeds_saving=investing_exceeds_saving,
    )
