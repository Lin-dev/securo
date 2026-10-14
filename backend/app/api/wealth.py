"""/api/wealth — the road to the user's net-worth milestone (fork, Wealth Cockpit).

Gated on the `wealth` module. Settings landed in B1; summary, net worth
history, savings rate, projection and levers land here (B3). Shapes are
fixed in app/schemas/wealth.py.

`_wealth_snapshot` computes settings, the latest net-worth point, the
typical figures and the levers figures exactly once per request, so
`get_summary`, `get_projection` and `get_levers` can never disagree about
the same request's numbers (plan's "one computation per request"). Nothing
here is cached across requests.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.app_clock import app_today
from app.core.database import get_async_session
from app.core.module_gate import require_module, require_module_write
from app.core.workspace_context import WorkspaceContext
from app.models.goal import Goal
from app.schemas.wealth import (
    ApplyBudgetSuggestions,
    ApplyBudgetSuggestionsResult,
    BudgetPace,
    BudgetSuggestions,
    CashForecast,
    SubscriptionAudit,
    Upcoming,
    Coverage,
    GoalProgress,
    NetWorthHistory,
    Precision,
    ProjectionInputs as SchemaProjectionInputs,
    SavingsMonth,
    SavingsRateSeries,
    WealthLevers,
    WealthProjection,
    WealthSettingsRead,
    WealthSettingsUpdate,
    WealthSettingsValues,
    WealthSummary,
)
from app.services.fx_rate_service import convert
from app.services.module_service import ModuleId
from app.services.wealth import budgets as budgets_service
from app.services.wealth import levers as levers_service
from app.services.wealth.buckets import INVESTMENT_BUCKETS
from app.services.wealth.figures import TypicalFigures, monthly_figures, rolling_rate, typical_expense_by_category, typical_figures
from app.services.wealth.levers import WealthLeverFigures
from app.services.wealth.projection import (
    ProjectionInputs as EngineProjectionInputs,
    add_months,
    fi_figures,
    levers as engine_levers,
    precision_for,
    scenarios,
)
from app.services.wealth.settings import load_settings, read_settings, update_settings
from app.services.wealth.valuation import NetWorthPoint, latest_net_worth, net_worth_history

router = APIRouter(prefix="/api/wealth", tags=["wealth"])

read_ctx = require_module(ModuleId.WEALTH)
write_ctx = require_module_write(ModuleId.WEALTH)


@router.get("/settings", response_model=WealthSettingsRead)
async def get_settings(
    ctx: WorkspaceContext = Depends(read_ctx), session: AsyncSession = Depends(get_async_session)
) -> WealthSettingsRead:
    return await read_settings(session, ctx)


@router.put("/settings", response_model=WealthSettingsRead)
async def put_settings(
    body: WealthSettingsUpdate,
    ctx: WorkspaceContext = Depends(write_ctx),
    session: AsyncSession = Depends(get_async_session),
) -> WealthSettingsRead:
    return await update_settings(session, ctx, body)


# --- one computation per request -----------------------------------------------------


@dataclass
class _WealthSnapshot:
    today: date
    currency: str
    settings: WealthSettingsValues
    point: NetWorthPoint
    typical: TypicalFigures
    coverage: Coverage
    precision: Precision
    goal: Optional[Goal]
    target_total: float
    engine_inputs: EngineProjectionInputs
    levers: WealthLeverFigures


async def _resolve_milestone_goal(
    session: AsyncSession, ctx: WorkspaceContext, settings: WealthSettingsValues
) -> Optional[Goal]:
    """`settings.goal_id` if it still points at a valid net-worth goal in this
    workspace, else the workspace's net-worth goal with the largest target,
    else `None` (plan P6: the summary then reports a $1,000,000 default with
    `goal=None`)."""
    if settings.goal_id is not None:
        candidate = await session.get(Goal, settings.goal_id)
        if candidate is not None and candidate.workspace_id == ctx.id and candidate.tracking_type == "net_worth":
            return candidate
    return (
        await session.execute(
            select(Goal)
            .where(Goal.workspace_id == ctx.id, Goal.tracking_type == "net_worth")
            .order_by(Goal.target_amount.desc())
            .limit(1)
        )
    ).scalars().first()


async def _wealth_snapshot(session: AsyncSession, ctx: WorkspaceContext) -> _WealthSnapshot:
    today = app_today()
    currency = ctx.user.primary_currency
    settings = load_settings(ctx.user)

    point = await latest_net_worth(session, ctx.id, ctx.user_id, settings.accounts)
    typical = await typical_figures(session, ctx.id, ctx.user_id, settings=settings)
    category_monthly = await typical_expense_by_category(session, ctx.id, ctx.user_id, settings=settings)

    cash = point.buckets["cash"]
    median_monthly_spend = typical.median_month.expenses
    lever_figures = levers_service.compute(
        cash=cash, median_monthly_spend=median_monthly_spend, buffer_months=settings.buffer_months,
        typical=typical, category_monthly=category_monthly, currency=currency,
    )

    goal = await _resolve_milestone_goal(session, ctx, settings)
    target_total = 1_000_000.0
    if goal is not None:
        converted, _ = await convert(session, Decimal(str(goal.target_amount)), goal.currency, currency, today)
        target_total = float(converted)

    invested = sum(point.buckets[b] for b in INVESTMENT_BUCKETS)
    non_invested = point.total - invested
    engine_inputs = EngineProjectionInputs(
        invested=invested, non_invested=non_invested, target_total=target_total,
        monthly_contribution=typical.saving.typical / 12, annual_return=settings.projection.return_nominal,
    )
    precision = precision_for(typical.coverage["days"])
    coverage = Coverage(
        from_date=typical.coverage["from_date"], to_date=typical.coverage["to_date"],
        days=typical.coverage["days"], annualized=typical.coverage["annualized"],
        insufficient_history=typical.coverage["insufficient_history"],
    )

    return _WealthSnapshot(
        today=today, currency=currency, settings=settings, point=point, typical=typical,
        coverage=coverage, precision=precision, goal=goal, target_total=target_total,
        engine_inputs=engine_inputs, levers=lever_figures,
    )


async def _savings_rate_3m(session: AsyncSession, ctx: WorkspaceContext, snap: _WealthSnapshot) -> Optional[float]:
    """Σ(income - expenses + direct) / Σ(income + direct) over the last 3
    *full* calendar months (never the current, partial one) — a ratio of
    sums, not an average of monthly rates (plan P1/S2)."""
    current_month = add_months(snap.today, 0)
    start = add_months(current_month, -3)
    end = add_months(current_month, -1)
    months = await monthly_figures(session, ctx.id, ctx.user_id, start, end, settings=snap.settings)
    return rolling_rate([m.figures for m in months])


# --- endpoints -------------------------------------------------------------------------


@router.get("/summary", response_model=WealthSummary)
async def get_summary(
    ctx: WorkspaceContext = Depends(read_ctx), session: AsyncSession = Depends(get_async_session)
) -> WealthSummary:
    snap = await _wealth_snapshot(session, ctx)
    base = scenarios(snap.engine_inputs, snap.today, snap.precision)["base"]

    goal_progress = None
    if snap.goal is not None:
        pct = snap.point.total / snap.target_total if snap.target_total else 0.0
        goal_progress = GoalProgress(
            id=snap.goal.id, name=snap.goal.name, target=snap.target_total,
            target_date=snap.goal.target_date, current=snap.point.total, pct=pct,
        )

    return WealthSummary(
        as_of=snap.today, currency=snap.currency, net_worth=snap.point.total, liquid=snap.point.liquid,
        goal=goal_progress, projected_date=base.date, projected_label=base.label, precision=snap.precision,
        coverage=snap.coverage, savings_rate_3m=await _savings_rate_3m(session, ctx, snap),
        cash_flow_typical_monthly=snap.levers.cash_flow.typical_monthly,
        idle_cash=snap.levers.idle_cash, cash_cushion_months=snap.levers.cash_cushion_months,
    )


@router.get("/net-worth-history", response_model=NetWorthHistory)
async def get_net_worth_history(
    months: int = Query(12, ge=1, le=120),
    ctx: WorkspaceContext = Depends(read_ctx),
    session: AsyncSession = Depends(get_async_session),
) -> NetWorthHistory:
    settings = load_settings(ctx.user)
    return await net_worth_history(session, ctx.id, ctx.user_id, months, settings.accounts)


@router.get("/savings-rate", response_model=SavingsRateSeries)
async def get_savings_rate(
    months: int = Query(12, ge=1, le=60),
    ctx: WorkspaceContext = Depends(read_ctx),
    session: AsyncSession = Depends(get_async_session),
) -> SavingsRateSeries:
    settings = load_settings(ctx.user)
    today = app_today()
    current_month = add_months(today, 0)
    requested_start = add_months(current_month, -(months - 1))
    fetch_start = add_months(requested_start, -11)  # lookback so every requested month gets a full rolling_12m

    all_months = await monthly_figures(session, ctx.id, ctx.user_id, fetch_start, current_month, settings=settings)
    ordered = [m.month for m in all_months]
    by_month = {m.month: m for m in all_months}

    out: list[SavingsMonth] = []
    for idx, month in enumerate(ordered):
        if month < requested_start:
            continue
        mf = by_month[month]
        window3 = [by_month[ordered[i]].figures for i in range(max(0, idx - 2), idx + 1)]
        window12 = [by_month[ordered[i]].figures for i in range(max(0, idx - 11), idx + 1)]
        out.append(SavingsMonth(
            month=month, income=mf.figures.income, expenses=mf.figures.expenses, direct=mf.figures.direct,
            rate=mf.figures.savings_rate, cash_rate=mf.figures.cash_rate,
            rolling_3m=rolling_rate(window3), rolling_12m=rolling_rate(window12),
            in_coverage=mf.in_coverage, partial=mf.partial,
        ))

    return SavingsRateSeries(currency=ctx.user.primary_currency, months=out)


@router.get("/projection", response_model=WealthProjection)
async def get_projection(
    ctx: WorkspaceContext = Depends(read_ctx), session: AsyncSession = Depends(get_async_session)
) -> WealthProjection:
    snap = await _wealth_snapshot(session, ctx)
    scenario_dict = scenarios(snap.engine_inputs, snap.today, snap.precision)
    top_cut_monthly = snap.levers.top_controllable.band_cut if snap.levers.top_controllable else None
    lever_results = engine_levers(
        snap.engine_inputs, snap.today, snap.precision, snap.levers.idle_cash, top_cut_monthly,
    )
    fi = fi_figures(snap.typical.expenses.typical, snap.settings.projection.withdrawal_rate)

    inputs_model = SchemaProjectionInputs(
        invested=snap.engine_inputs.invested, non_invested=snap.engine_inputs.non_invested,
        target_total=snap.engine_inputs.target_total, target_invested=snap.engine_inputs.target,
        monthly_contribution=snap.engine_inputs.monthly_contribution,
        typical_annual_saving=snap.typical.saving.typical, nominal=True, coverage=snap.coverage,
    )

    return WealthProjection(
        currency=snap.currency, as_of=snap.today, precision=snap.precision, inputs=inputs_model,
        scenarios={"low": scenario_dict["low"], "base": scenario_dict["base"], "high": scenario_dict["high"]},
        levers=lever_results, fi=fi,
    )


@router.get("/levers", response_model=WealthLevers)
async def get_levers(
    ctx: WorkspaceContext = Depends(read_ctx), session: AsyncSession = Depends(get_async_session)
) -> WealthLevers:
    snap = await _wealth_snapshot(session, ctx)
    lf = snap.levers
    return WealthLevers(
        currency=snap.currency, as_of=snap.today, cash=lf.cash, buffer=lf.buffer, idle_cash=lf.idle_cash,
        cash_cushion_months=lf.cash_cushion_months, cash_flow=lf.cash_flow, top_controllable=lf.top_controllable,
        move=lf.move, investing_exceeds_saving=lf.investing_exceeds_saving,
    )


# --- R2: spend smarter (B5a / B5b) -------------------------------------------


def _not_yet() -> HTTPException:
    return HTTPException(status_code=501, detail="Not implemented yet")


@router.get("/budget-pace", response_model=BudgetPace)
async def get_budget_pace(
    ctx: WorkspaceContext = Depends(read_ctx), session: AsyncSession = Depends(get_async_session)
) -> BudgetPace:
    settings = load_settings(ctx.user)
    return await budgets_service.budget_pace(session, ctx, settings)


@router.get("/budget-suggestions", response_model=BudgetSuggestions)
async def get_budget_suggestions(
    ctx: WorkspaceContext = Depends(read_ctx), session: AsyncSession = Depends(get_async_session)
) -> BudgetSuggestions:
    settings = load_settings(ctx.user)
    return await budgets_service.suggest_budgets(session, ctx, settings)


@router.post("/budget-suggestions/apply", response_model=ApplyBudgetSuggestionsResult)
async def apply_budget_suggestions(
    body: ApplyBudgetSuggestions,
    ctx: WorkspaceContext = Depends(write_ctx),
    session: AsyncSession = Depends(get_async_session),
) -> ApplyBudgetSuggestionsResult:
    return await budgets_service.apply_budget_suggestions(session, ctx, body.category_ids)


@router.get("/upcoming", response_model=Upcoming)
async def get_upcoming(
    days: int = Query(30, ge=1, le=90),
    ctx: WorkspaceContext = Depends(read_ctx),
    session: AsyncSession = Depends(get_async_session),
) -> Upcoming:
    raise _not_yet()


@router.get("/cash-forecast", response_model=CashForecast)
async def get_cash_forecast(
    days: int = Query(90, ge=7, le=180),
    ctx: WorkspaceContext = Depends(read_ctx),
    session: AsyncSession = Depends(get_async_session),
) -> CashForecast:
    raise _not_yet()


@router.get("/subscriptions", response_model=SubscriptionAudit)
async def get_subscriptions(
    ctx: WorkspaceContext = Depends(read_ctx), session: AsyncSession = Depends(get_async_session)
) -> SubscriptionAudit:
    raise _not_yet()

