"""The pace formula shared by the spending cap and every budgeted category
(plan R2 B5a; Critique C12).

`compute_pace` is pure: it takes a budget (`B`, the cap or a category's
monthly budget) and that same scope's recurring-debit totals, and says how
far along the month should be versus how far along it actually is. The
"discretionary" room — the budget left over after the month's recurring
bills — is spread evenly across the days of the month, so a month whose
only spending so far is a recurring bill reads `on_pace` on day 1, not
`over`. `recurring_debit_totals` is the one place that reads what "the
month's recurring bills" means from the database, so the cap
(`wealth.budgets.budget_pace`) and each budget row
(`budget_service.get_budget_vs_actual`) can never compute it differently.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.transaction import Transaction
from app.schemas.wealth import PaceStatus
from app.services._query_filters import counts_as_pnl
from app.services.dashboard_service import _get_recurring_projections

_UNDER = Decimal("0.9")
_OVER = Decimal("1.1")
_ZERO = Decimal("0")


@dataclass(frozen=True)
class Pace:
    """The computed half of `compute_pace`'s output — `cap`/`spent` are the
    caller's own inputs, already known before the call."""

    expected_to_date: Decimal
    projected_month_end: Decimal
    status: PaceStatus
    left_to_spend: Decimal
    left_per_day: Optional[Decimal]


def compute_pace(
    *,
    budget: Decimal,
    spent: Decimal,
    r_to_date: Decimal,
    r_month: Decimal,
    days_elapsed: int,
    days_in_month: int,
) -> Pace:
    """R2's pace formula (plan R2 B5a):

    - `expected_to_date` = recurring charges scheduled to date, plus the
      discretionary share of the budget (`budget - r_month`, floored at 0)
      spread evenly over the days elapsed.
    - `projected_month_end` = what's spent so far, plus the recurring still
      to come this month, plus today's discretionary rate carried over the
      remaining days.
    - `status`: `under` below 90% of expected, `over` above 110%, else
      `on_pace`. An all-recurring month (`expected_to_date == 0`) is
      `on_pace` at zero spend and `over` at any spend above it.
    """
    discretionary_budget = max(_ZERO, budget - r_month)
    expected_to_date = r_to_date + discretionary_budget * days_elapsed / days_in_month

    discretionary_spent = max(_ZERO, spent - r_to_date)
    discretionary_rate = discretionary_spent / days_elapsed if days_elapsed else _ZERO
    remaining_days = days_in_month - days_elapsed
    projected_month_end = spent + (r_month - r_to_date) + discretionary_rate * remaining_days

    if expected_to_date == 0:
        status: PaceStatus = "on_pace" if spent == 0 else "over"
    elif spent < _UNDER * expected_to_date:
        status = "under"
    elif spent > _OVER * expected_to_date:
        status = "over"
    else:
        status = "on_pace"

    left_to_spend = max(_ZERO, budget - spent)
    left_per_day = left_to_spend / remaining_days if remaining_days > 0 else None

    return Pace(
        expected_to_date=expected_to_date, projected_month_end=projected_month_end, status=status,
        left_to_spend=left_to_spend, left_per_day=left_per_day,
    )


def next_month_start(d: date) -> date:
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


async def recurring_debit_totals(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    month_start: date,
    today: date,
    category_id: Optional[uuid.UUID] = None,
) -> tuple[Decimal, Decimal]:
    """(R_to_date, R_month): recurring debits scheduled over `month_start`'s
    calendar month — posted rows linked by `recurring_transaction_id`, plus
    not-yet-posted projections (`dashboard_service._get_recurring_projections`,
    which already drops a projection whose occurrence has a matching real
    row, so a bill is never counted twice). `R_to_date` stops at `today`
    inclusive; `R_month` covers the whole month. Optionally scoped to one
    category; otherwise every category (plan's cap pace)."""
    month_end = next_month_start(month_start)
    filters = [
        Transaction.workspace_id == workspace_id,
        Transaction.status == "posted",
        Transaction.recurring_transaction_id.is_not(None),
        Transaction.type == "debit",
        Transaction.date >= month_start,
        Transaction.date < month_end,
        counts_as_pnl(),
    ]
    if category_id is not None:
        filters.append(Transaction.category_id == category_id)
    posted_rows = (
        await session.execute(
            select(Transaction.date, Transaction.amount_primary, Transaction.amount).where(*filters)
        )
    ).all()

    r_month = _ZERO
    r_to_date = _ZERO
    for txn_date, amount_primary, amount in posted_rows:
        value = abs(Decimal(str(amount_primary if amount_primary is not None else amount)))
        r_month += value
        if txn_date <= today:
            r_to_date += value

    projections = await _get_recurring_projections(session, workspace_id, month_start, month_end)
    for proj in projections:
        if proj["type"] != "debit":
            continue
        if category_id is not None and proj["category_id"] != category_id:
            continue
        value = abs(Decimal(str(proj["amount"])))
        r_month += value
        if proj["date"] <= today:
            r_to_date += value

    return r_to_date, r_month
