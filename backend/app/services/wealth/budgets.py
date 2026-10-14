"""Budgets suggested from history, budget pace and a monthly spending cap
(plan R2 B5a; production has 0 budgets, so suggestions are the on-ramp —
Critique C12/R2-m2).

`suggest_budgets` reuses `budget_service.get_budget_vs_actual` per month —
the same split-share, refund-netted semantics a budget row's "actual" uses
— rather than the P1 figures, so a suggested amount and the actual it will
later be compared against are computed the same way. `apply_budget_suggestions`
is the only writer: idempotent, and it only ever creates a recurring budget,
never updates one (plan: "a second click changes nothing").
"""
from __future__ import annotations

import statistics
import uuid
from datetime import date
from decimal import ROUND_CEILING, Decimal
from typing import Optional

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.app_clock import app_today
from app.core.workspace_context import WorkspaceContext
from app.models.budget import Budget
from app.models.category import Category
from app.models.category_group import CategoryGroup
from app.models.transaction import Transaction
from app.schemas.budget import BudgetCreate
from app.schemas.wealth import (
    ApplyBudgetSuggestionsResult,
    BudgetPace,
    BudgetSuggestion,
    BudgetSuggestions,
    CapPace,
    WealthSettingsValues,
)
from app.services.budget_service import create_budget, get_budget_vs_actual
from app.services.wealth.coverage import coverage_window
from app.services.wealth.figures import detect_one_offs, effective_income_category_ids, monthly_figures, period_figures, typical_figures
from app.services.wealth.pace import compute_pace, next_month_start, recurring_debit_totals
from app.services.wealth.settings import load_settings

_FIVE = Decimal(5)


def _round_up_to_5(amount: Decimal) -> Decimal:
    return (amount / _FIVE).to_integral_value(rounding=ROUND_CEILING) * _FIVE


async def _one_off_expense_totals(
    session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID, today: date, settings: WealthSettingsValues,
) -> dict[tuple[date, uuid.UUID], Decimal]:
    """Selected one-off expenses (plan P1), summed by the month and category
    they land in — what `suggest_budgets` subtracts from a month's
    budget-actual before taking the median, so a once-off purchase doesn't
    inflate a suggestion."""
    one_offs = await detect_one_offs(session, workspace_id, user_id, today, settings=settings)
    expense_rows = [row for row in one_offs if row.selected and row.lane == "expenses"]
    if not expense_rows:
        return {}
    txn_ids = [row.transaction_id for row in expense_rows]
    cat_rows = (
        await session.execute(select(Transaction.id, Transaction.category_id).where(Transaction.id.in_(txn_ids)))
    ).all()
    cat_by_txn = {txn_id: cat_id for txn_id, cat_id in cat_rows}
    totals: dict[tuple[date, uuid.UUID], Decimal] = {}
    for row in expense_rows:
        cat_id = cat_by_txn.get(row.transaction_id)
        if cat_id is None:
            continue
        key = (date(row.date.year, row.date.month, 1), cat_id)
        totals[key] = totals.get(key, Decimal("0")) + Decimal(str(row.amount))
    return totals


async def _current_recurring_budgets(
    session: AsyncSession, workspace_id: uuid.UUID, current_month: date,
) -> dict[uuid.UUID, Decimal]:
    """The category's current recurring budget — the most recent
    `is_recurring=True` row effective at or before `current_month` — keyed by
    category id. Mirrors the recurring half of
    `budget_service._build_budget_map`; `suggest_budgets` and
    `apply_budget_suggestions` need just this, never a month-specific
    override."""
    max_month_subq = (
        select(Budget.category_id, func.max(Budget.month).label("max_month"))
        .where(
            Budget.workspace_id == workspace_id,
            Budget.is_recurring == True,  # noqa: E712
            Budget.month <= current_month,
        )
        .group_by(Budget.category_id)
        .subquery()
    )
    rows = await session.execute(
        select(Budget.category_id, Budget.amount)
        .join(
            max_month_subq,
            and_(
                Budget.category_id == max_month_subq.c.category_id,
                Budget.month == max_month_subq.c.max_month,
            ),
        )
        .where(Budget.workspace_id == workspace_id, Budget.is_recurring == True)  # noqa: E712
    )
    return {cat_id: amount for cat_id, amount in rows.all()}


async def suggest_budgets(
    session: AsyncSession, ctx: WorkspaceContext, settings: WealthSettingsValues,
) -> BudgetSuggestions:
    """Per expense category, the median of its full covered months' budget-
    actuals (refunds netted, split shares applied, one-offs excluded),
    skipping income and transfer categories and a median at or below 0.
    Full months only: never the partial coverage-start month, never the
    current month, at most the 3 most recent (plan R2 B5a)."""
    today = app_today()
    currency = ctx.user.primary_currency
    typical = await typical_figures(session, ctx.id, ctx.user_id, settings=settings)
    cov = await coverage_window(session, ctx.id, today)

    all_months = await monthly_figures(session, ctx.id, ctx.user_id, cov["from_date"], today, settings=settings)
    used_months = [m.month for m in all_months if m.in_coverage][-3:]

    if not used_months:
        return BudgetSuggestions(
            currency=currency, months=[], suggestions=[], total_suggested=0.0,
            typical_monthly_spending=typical.median_month.expenses,
        )

    income_ids = await effective_income_category_ids(session, ctx.id, settings)
    all_categories = (
        await session.execute(
            select(Category, CategoryGroup)
            .outerjoin(CategoryGroup, Category.group_id == CategoryGroup.id)
            .where(Category.workspace_id == ctx.id)
        )
    ).all()
    candidates = [
        (cat, group) for cat, group in all_categories
        if cat.id not in income_ids and not cat.treat_as_transfer
    ]

    one_off_by_month_cat = await _one_off_expense_totals(session, ctx.id, ctx.user_id, today, settings)
    actual_by_month: dict[date, dict[uuid.UUID, Decimal]] = {}
    for month in used_months:
        rows = await get_budget_vs_actual(session, ctx.id, ctx.user_id, month)
        actual_by_month[month] = {row.category_id: row.actual_amount for row in rows}

    current_month = date(today.year, today.month, 1)
    existing_recurring = await _current_recurring_budgets(session, ctx.id, current_month)

    suggestions: list[BudgetSuggestion] = []
    total_suggested = 0.0
    for cat, group in candidates:
        values = [
            max(
                Decimal("0"),
                actual_by_month[month].get(cat.id, Decimal("0")) - one_off_by_month_cat.get((month, cat.id), Decimal("0")),
            )
            for month in used_months
        ]
        median = round(float(statistics.median(values)), 2)
        if median <= 0:
            continue
        existing = existing_recurring.get(cat.id)
        suggestions.append(BudgetSuggestion(
            category_id=cat.id, category=cat.name, group_name=group.name if group else None,
            monthly_median=median, months_used=len(used_months),
            existing_budget=float(existing) if existing is not None else None,
        ))
        total_suggested += median

    suggestions.sort(key=lambda s: (-s.monthly_median, s.category))
    return BudgetSuggestions(
        currency=currency, months=used_months, suggestions=suggestions,
        total_suggested=round(total_suggested, 2), typical_monthly_spending=typical.median_month.expenses,
    )


async def apply_budget_suggestions(
    session: AsyncSession, ctx: WorkspaceContext, category_ids: list[uuid.UUID],
) -> ApplyBudgetSuggestionsResult:
    """Create a recurring budget (current month, median rounded up to the
    nearest 5) for each requested category that doesn't already have one.
    Idempotent: a category with a recurring budget — including one this same
    call just created — is skipped, never updated (plan: "a second click
    changes nothing")."""
    today = app_today()
    current_month = date(today.year, today.month, 1)
    settings = load_settings(ctx.user)
    suggestions = await suggest_budgets(session, ctx, settings)
    suggestion_by_cat = {s.category_id: s for s in suggestions.suggestions}
    existing = await _current_recurring_budgets(session, ctx.id, current_month)

    created = 0
    skipped = 0
    for category_id in category_ids:
        suggestion = suggestion_by_cat.get(category_id)
        if suggestion is None or category_id in existing:
            skipped += 1
            continue
        amount = _round_up_to_5(Decimal(str(suggestion.monthly_median)))
        await create_budget(
            session, ctx.id, ctx.user_id,
            BudgetCreate(category_id=category_id, amount=amount, month=current_month, is_recurring=True),
        )
        existing[category_id] = amount
        created += 1
    return ApplyBudgetSuggestionsResult(created=created, skipped=skipped)


async def budget_pace(
    session: AsyncSession, ctx: WorkspaceContext, settings: WealthSettingsValues,
) -> BudgetPace:
    """This month's pace: P1 expenses to date, plus — when a spending cap is
    set — the cap's pace under the formula in `wealth.pace` (plan R2 B5a)."""
    today = app_today()
    month_start = date(today.year, today.month, 1)
    month_end = next_month_start(month_start)
    days_in_month = (month_end - month_start).days
    days_elapsed = today.day

    figures = await period_figures(session, ctx.id, ctx.user_id, month_start, today, settings=settings)
    spent = Decimal(str(figures.expenses))

    cap: Optional[CapPace] = None
    if settings.spending_cap is not None:
        r_to_date, r_month = await recurring_debit_totals(session, ctx.id, month_start, today)
        budget = Decimal(str(settings.spending_cap))
        pace = compute_pace(
            budget=budget, spent=spent, r_to_date=r_to_date, r_month=r_month,
            days_elapsed=days_elapsed, days_in_month=days_in_month,
        )
        cap = CapPace(
            cap=float(budget), spent=float(spent), expected_to_date=float(pace.expected_to_date),
            projected_month_end=float(pace.projected_month_end), status=pace.status,
            left_to_spend=float(pace.left_to_spend),
            left_per_day=float(pace.left_per_day) if pace.left_per_day is not None else None,
        )

    return BudgetPace(
        currency=ctx.user.primary_currency, month=month_start, days_elapsed=days_elapsed,
        days_in_month=days_in_month, cap=cap, spent=float(spent),
    )
