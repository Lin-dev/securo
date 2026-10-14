"""One definition per money figure (plan P1).

Every figure below reads the same shared rules before anything else: posted
rows only, `source != "opening_balance"`, split parents excluded, each row
dated by `reporting_date_col(accounting_mode)`, an explicit start/end (no
forecasts), and the workspace scope. `fire_projection`, the Money Map tool,
`advisor_facts`, the digest and the dashboard tile are all meant to read
their numbers from here rather than re-deriving them.
"""
from __future__ import annotations

import statistics
import uuid
from collections import namedtuple
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, Mapping, Optional, Sequence

from sqlalchemy import ColumnElement, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.app_clock import app_today
from app.models.account import Account
from app.models.category import Category
from app.models.category_group import CategoryGroup
from app.models.transaction import Transaction
from app.schemas.wealth import AccountAnnotation, Bucket, OneOffRow, WealthSettingsValues
from app.services import admin_service
from app.services._query_filters import (
    INVESTMENT_CONTRIBUTION_CATEGORY,
    counts_as_pnl,
    is_split_parent,
    reporting_date_col,
)
from app.services.report_service import investment_lane_rows
from app.services.wealth.buckets import INVESTMENT_BUCKETS, AccountFacts, choose_bucket
from app.services.wealth.classify import is_default_income_category
from app.services.wealth.coverage import coverage_window
from app.services.wealth.spending import month_end

# A one-off proposal needs a row at least this large, or 3x the lane's
# median monthly total, whichever is bigger (plan P1).
ONE_OFF_FLOOR = 2500.0
ONE_OFF_MEDIAN_MULTIPLE = 3.0
_INVESTING_NAME = INVESTMENT_CONTRIBUTION_CATEGORY.lower()

_NamedCat = namedtuple("_NamedCat", ["name", "group_name"])
_Lane = Literal["income", "expenses", "invested"]


@dataclass(frozen=True)
class _OneOff:
    """`OneOffRow` plus whether it's a *direct* contribution (an unpaired credit
    into an investment account). `OneOffRow` is the API contract and has no such
    field; `_period_figures_with_ctx` needs it internally, to also subtract a
    direct one-off from `direct`, not just `invested` — otherwise `direct` would
    exceed `invested` and inflate `cash_flow_after_investing` and saving."""

    transaction_id: uuid.UUID
    date: date
    description: str
    amount: float
    lane: _Lane
    proposed: bool
    selected: bool
    is_direct: bool


def _next_month(d: date) -> date:
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


def _full_months(coverage_from: date, today: date) -> list[date]:
    """Calendar months (first day) entirely inside [coverage_from, today): started on
    or after `coverage_from`, and never the current month. A coverage start mid-month
    (e.g. 2026-07-13) makes that first month partial, not full."""
    cursor = date(coverage_from.year, coverage_from.month, 1)
    if cursor < coverage_from:
        cursor = _next_month(cursor)
    current_month = date(today.year, today.month, 1)
    out = []
    while cursor < current_month:
        out.append(cursor)
        cursor = _next_month(cursor)
    return out


# --- shared building blocks ---------------------------------------------------------


def account_facts_from(account: Account) -> AccountFacts:
    """`AccountFacts` for one loaded `Account` (its `connection` must already be
    eager-loaded; `institution` is eager by default on the model)."""
    institution = account.institution.name if account.institution else None
    if institution is None and account.connection is not None:
        institution = account.connection.institution_name
    balance = account.balance_primary if account.balance_primary is not None else account.balance
    return AccountFacts(
        id=str(account.id), name=account.name, type=account.type, balance=float(balance),
        institution=institution, masked_number=account.masked_number,
    )


async def account_bucket_map(
    session: AsyncSession, workspace_id: uuid.UUID, overrides: Mapping[str, AccountAnnotation]
) -> dict[uuid.UUID, Bucket]:
    """Effective bucket per account, for every account the workspace has — a closed
    account's past trades still need the investment-bucket exclusion below. Shared
    with `wealth.settings.read_settings`, which needs the same choice for its (open-only)
    account rows."""
    accounts = (
        await session.execute(
            select(Account)
            .where(Account.workspace_id == workspace_id)
            .options(selectinload(Account.connection))
        )
    ).scalars().all()
    return {account.id: choose_bucket(account_facts_from(account), overrides).bucket for account in accounts}


async def effective_income_category_ids(
    session: AsyncSession, workspace_id: uuid.UUID, settings: WealthSettingsValues
) -> frozenset[uuid.UUID]:
    """`settings.income_category_ids`, or the default list (`is_default_income_category`)."""
    if settings.income_category_ids is not None:
        return frozenset(settings.income_category_ids)
    rows = (
        await session.execute(
            select(Category.id, Category.name, CategoryGroup.name)
            .outerjoin(CategoryGroup, Category.group_id == CategoryGroup.id)
            .where(Category.workspace_id == workspace_id)
        )
    ).all()
    return frozenset(
        cat_id for cat_id, name, group_name in rows if is_default_income_category(_NamedCat(name or "", group_name))
    )


@dataclass(frozen=True)
class _Context:
    """Per-request lookups shared by every period inside one call, so a 12-month
    `monthly_figures` run resolves the income-category list and account buckets once."""

    report_date: Any  # a SQLAlchemy column expression (`reporting_date_col(...)`)
    income_category_ids: frozenset[uuid.UUID]
    account_buckets: dict[uuid.UUID, Bucket]


async def _build_context(
    session: AsyncSession, workspace_id: uuid.UUID, settings: WealthSettingsValues
) -> _Context:
    mode = await admin_service.get_credit_card_accounting_mode(session)
    income_ids = await effective_income_category_ids(session, workspace_id, settings)
    buckets = await account_bucket_map(session, workspace_id, settings.accounts)
    return _Context(report_date=reporting_date_col(mode), income_category_ids=income_ids, account_buckets=buckets)


def _amount_col():
    return func.coalesce(Transaction.amount_primary, Transaction.amount)


def pnl_row_filter(
    workspace_id: uuid.UUID, account_buckets: Mapping[uuid.UUID, Bucket]
) -> list[ColumnElement]:
    """The P1 row-level filter (plan P1): posted, not an opening balance, not a
    split parent, `counts_as_pnl()`, and excluding an uncategorized row sitting in
    an account whose *effective* bucket (plan P3, under the user's overrides) is
    an investment bucket. Row-level only — no date range, since every caller
    already applies its own window (`_shared_filters` below adds one for the
    figures above; the transactions list applies its own `from`/`to` filter).

    The single source for "is this row in the P1 P&L set?": `_shared_filters`
    (and so `_pnl_totals`, `_lane_candidates` and `_pnl_totals_by_category`) folds
    this in below, and the transactions list's `wealth_pnl_only` filter
    (`transaction_service.get_transactions`) calls it directly with a freshly
    loaded `account_bucket_map`, so a drill-down's rows and the figure they drill
    from can never drift apart.
    """
    investment_account_ids = [
        account_id for account_id, bucket in account_buckets.items() if bucket in INVESTMENT_BUCKETS
    ]
    uncategorized_in_investment_account = and_(
        Transaction.category_id.is_(None), Transaction.account_id.in_(investment_account_ids)
    )
    return [
        Transaction.workspace_id == workspace_id,
        Transaction.status == "posted",
        Transaction.source != "opening_balance",
        ~is_split_parent(),
        counts_as_pnl(),
        ~uncategorized_in_investment_account,
    ]


def _shared_filters(ctx: _Context, workspace_id: uuid.UUID, start: date, end: date) -> list:
    """`pnl_row_filter` plus this period's explicit window."""
    return [
        *pnl_row_filter(workspace_id, ctx.account_buckets),
        ctx.report_date >= start,
        ctx.report_date <= end,
    ]


# --- period figures --------------------------------------------------------------


@dataclass(frozen=True)
class PeriodFigures:
    """Every P1 figure over one explicit [start, end] window."""

    start: date
    end: date
    income: float
    expenses: float
    unclassified_credits: float
    invested: float
    direct: float
    cash_flow_after_investing: float
    savings_rate: Optional[float]
    cash_rate: Optional[float]
    one_offs_excluded: bool


def aggregate_pnl_rows(
    rows: Sequence[Any], income_category_ids: frozenset[uuid.UUID]
) -> tuple[float, float, float]:
    """(income, expenses, unclassified_credits) from `(category_id, type, amount)`
    rows already filtered by `pnl_row_filter` (so every uncategorized row here is
    already known *not* to sit in an investment-bucketed account).

    Income: P&L credits in an income category. Expenses: P&L debits minus refunds
    (every other categorized P&L credit), netted per category and never pushed
    below zero. Uncategorized credits are neither income nor refunds
    (`unclassified_credits`).

    Shared by `_pnl_totals`'s own [start, end] window query and by the
    transactions list's `wealth_pnl_only` summary (`transaction_service.
    get_transactions`), which runs this over whatever extra filters — category,
    account, dates — the caller also applied, so a drill-down's income/expense
    total and the figure it drills from can never drift apart.
    """
    income = 0.0
    unclassified_credits = 0.0
    debit_by_category: dict[Optional[uuid.UUID], float] = {}
    refund_by_category: dict[uuid.UUID, float] = {}

    for category_id, txn_type, amount_raw in rows:
        amount = abs(float(amount_raw or 0))
        if amount <= 0:
            continue
        if category_id is None:
            if txn_type == "credit":
                unclassified_credits += amount
            else:
                debit_by_category[None] = debit_by_category.get(None, 0.0) + amount
            continue
        if txn_type == "credit":
            if category_id in income_category_ids:
                income += amount
            else:
                refund_by_category[category_id] = refund_by_category.get(category_id, 0.0) + amount
        else:
            debit_by_category[category_id] = debit_by_category.get(category_id, 0.0) + amount

    netted = sum(
        max(0.0, debit_total - refund_by_category.get(cat_id, 0.0))
        for cat_id, debit_total in debit_by_category.items()
    )
    # Refunds beyond their own category's spending (a dedicated "Refunds & credits"
    # category has no purchases to net against) reduce total spending instead of
    # disappearing; the total never goes below zero.
    leftover = sum(
        max(0.0, refund_total - debit_by_category.get(cat_id, 0.0))
        for cat_id, refund_total in refund_by_category.items()
    )
    expenses = max(0.0, netted - leftover)
    return income, expenses, unclassified_credits


async def _pnl_totals(
    session: AsyncSession, workspace_id: uuid.UUID, ctx: _Context, start: date, end: date
) -> tuple[float, float, float]:
    """(income, expenses, unclassified_credits) — P&L rows only; `invested`/`direct`
    are not P&L and come from `investment_lane_rows` instead. See
    `aggregate_pnl_rows` for the income/refund-netting rules; `_shared_filters`
    (so `pnl_row_filter`) already drops uncategorized rows inside an
    investment-bucketed account before the rows reach it.
    """
    rows = (
        await session.execute(
            select(Transaction.category_id, Transaction.type, _amount_col())
            .where(*_shared_filters(ctx, workspace_id, start, end))
        )
    ).all()
    return aggregate_pnl_rows(rows, ctx.income_category_ids)


async def _invested_and_direct(
    session: AsyncSession, workspace_id: uuid.UUID, ctx: _Context, start: date, end: date
) -> tuple[float, float]:
    """(invested, direct): the money map's investment lane, read from
    `report_service.investment_lane_rows` so the two figures can never drift apart.

    `invested` = every contribution-lane movement: a debit leaving a non-investment
    account in the contribution category (paired or not), plus an unpaired credit
    landing in an investment account in that category. `direct` = the latter only
    (an unpaired investment-account credit with no sending leg in Securo — payroll
    401(k) deductions, employer match, an unconnected funding account).
    """
    rows = await investment_lane_rows(session, workspace_id, ctx.report_date, start, end)
    invested = 0.0
    direct = 0.0
    for _cat_id, cat_name, _color, _acct_id, _acct_name, acct_type, txn_type, unpaired, total_amount in rows:
        amount = abs(float(total_amount or 0))
        if amount <= 0 or (cat_name or "").lower() != _INVESTING_NAME:
            continue
        if acct_type == "investment":
            if txn_type == "credit" and unpaired:
                invested += amount
                direct += amount
        elif txn_type == "debit":
            invested += amount
    return invested, direct


def _derived(
    income: float, expenses: float, invested: float, direct: float
) -> tuple[float, Optional[float], Optional[float]]:
    cash_flow_after_investing = income - expenses - (invested - direct)
    denom = income + direct
    savings_rate = (income - expenses + direct) / denom if denom > 0 else None
    cash_rate = (income - expenses) / income if income > 0 else None
    return cash_flow_after_investing, savings_rate, cash_rate


async def _period_figures_with_ctx(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    ctx: _Context,
    start: date,
    end: date,
    one_offs: Sequence[_OneOff] = (),
) -> PeriodFigures:
    """`period_figures`'s body, taking an already-built `_Context` and an already
    -detected one-off list so a multi-month caller resolves each only once."""
    income, expenses, unclassified_credits = await _pnl_totals(session, workspace_id, ctx, start, end)
    invested, direct = await _invested_and_direct(session, workspace_id, ctx, start, end)

    for row in one_offs:
        if not row.selected or not (start <= row.date <= end):
            continue
        if row.lane == "income":
            income -= row.amount
        elif row.lane == "expenses":
            expenses -= row.amount
        elif row.lane == "invested":
            invested -= row.amount
            if row.is_direct:
                direct -= row.amount  # a direct one-off's amount is part of both sums

    # A one-off whose category was later refunded (or any other overshoot) must
    # not drive a lane negative; `direct` is never clamped — it stays <= invested
    # automatically, since the same amount was subtracted from both above.
    income = max(0.0, income)
    expenses = max(0.0, expenses)
    invested = max(0.0, invested)

    cash_flow_after_investing, savings_rate, cash_rate = _derived(income, expenses, invested, direct)
    return PeriodFigures(
        start=start, end=end, income=income, expenses=expenses, unclassified_credits=unclassified_credits,
        invested=invested, direct=direct, cash_flow_after_investing=cash_flow_after_investing,
        savings_rate=savings_rate, cash_rate=cash_rate, one_offs_excluded=bool(one_offs),
    )


async def period_figures(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    start: date,
    end: date,
    *,
    settings: WealthSettingsValues,
    exclude_one_offs: bool = False,
) -> PeriodFigures:
    """Every P1 figure over one explicit [start, end] window (`user_id` keeps the
    signature uniform across `wealth.*`; the figures themselves are workspace-wide,
    a household's books rather than one member's share)."""
    ctx = await _build_context(session, workspace_id, settings)
    one_offs = (
        await _detect_one_offs_with_ctx(session, workspace_id, ctx, app_today(), settings)
        if exclude_one_offs else ()
    )
    return await _period_figures_with_ctx(session, workspace_id, ctx, start, end, one_offs)


# --- monthly split -----------------------------------------------------------------


@dataclass(frozen=True)
class MonthFigures:
    month: date  # first day
    in_coverage: bool
    partial: bool  # the current month
    figures: PeriodFigures


async def monthly_figures(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    start_month: date,
    end_month: date,
    *,
    settings: WealthSettingsValues,
    exclude_one_offs: bool = False,
) -> list[MonthFigures]:
    """One `period_figures`-equivalent call per calendar month in
    [start_month, end_month] (inclusive, both truncated to the first of their month)."""
    ctx = await _build_context(session, workspace_id, settings)
    today = app_today()
    cov_from = (await coverage_window(session, workspace_id, today))["from_date"]
    full_months = frozenset(_full_months(cov_from, today))
    current_month = date(today.year, today.month, 1)
    one_offs = (
        await _detect_one_offs_with_ctx(session, workspace_id, ctx, today, settings)
        if exclude_one_offs else ()
    )

    out: list[MonthFigures] = []
    cursor = date(start_month.year, start_month.month, 1)
    last = date(end_month.year, end_month.month, 1)
    while cursor <= last:
        figures = await _period_figures_with_ctx(session, workspace_id, ctx, cursor, month_end(cursor), one_offs)
        out.append(MonthFigures(
            month=cursor, in_coverage=cursor in full_months, partial=cursor == current_month, figures=figures,
        ))
        cursor = _next_month(cursor)
    return out


# --- typical (annualized) figures --------------------------------------------------


@dataclass(frozen=True)
class AnnualFigure:
    all: float
    typical: float  # one-offs excluded


@dataclass(frozen=True)
class MedianMonth:
    cash_flow_after_investing: Optional[float]
    saving: Optional[float]  # income - expenses + direct, for the median full month
    expenses: Optional[float]  # median monthly spending, one-offs excluded (plan P3's buffer/cushion input)
    full_months: int


@dataclass(frozen=True)
class TypicalFigures:
    coverage: dict
    income: AnnualFigure
    expenses: AnnualFigure
    direct: AnnualFigure
    invested: AnnualFigure
    saving: AnnualFigure
    median_month: MedianMonth


def _saving(f: PeriodFigures) -> float:
    return f.income - f.expenses + f.direct


async def typical_figures(
    session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID, *, settings: WealthSettingsValues
) -> TypicalFigures:
    """Both annualized versions ("all" and "typical", one-offs excluded) of income,
    expenses, direct, invested and saving, over `coverage_window`; plus the median
    full month's cash flow after investing and saving."""
    ctx = await _build_context(session, workspace_id, settings)
    today = app_today()
    cov = await coverage_window(session, workspace_id, today)
    scale = cov["scale"]
    one_offs = await _detect_one_offs_with_ctx(session, workspace_id, ctx, today, settings)

    all_figures = await _period_figures_with_ctx(session, workspace_id, ctx, cov["from_date"], cov["to_date"])
    typical = await _period_figures_with_ctx(
        session, workspace_id, ctx, cov["from_date"], cov["to_date"], one_offs
    )

    full_months = _full_months(cov["from_date"], today)
    cash_flows: list[float] = []
    savings: list[float] = []
    expenses_list: list[float] = []
    for month in full_months:
        month_figures = await _period_figures_with_ctx(
            session, workspace_id, ctx, month, month_end(month), one_offs
        )
        cash_flows.append(month_figures.cash_flow_after_investing)
        savings.append(_saving(month_figures))
        expenses_list.append(month_figures.expenses)

    return TypicalFigures(
        coverage=cov,
        income=AnnualFigure(all=all_figures.income * scale, typical=typical.income * scale),
        expenses=AnnualFigure(all=all_figures.expenses * scale, typical=typical.expenses * scale),
        direct=AnnualFigure(all=all_figures.direct * scale, typical=typical.direct * scale),
        invested=AnnualFigure(all=all_figures.invested * scale, typical=typical.invested * scale),
        saving=AnnualFigure(all=_saving(all_figures) * scale, typical=_saving(typical) * scale),
        median_month=MedianMonth(
            cash_flow_after_investing=statistics.median(cash_flows) if cash_flows else None,
            saving=statistics.median(savings) if savings else None,
            expenses=statistics.median(expenses_list) if expenses_list else None,
            full_months=len(full_months),
        ),
    )


# --- one-offs -----------------------------------------------------------------------


async def _lane_candidates(
    session: AsyncSession, workspace_id: uuid.UUID, ctx: _Context, start: date, end: date
) -> dict[_Lane, list[tuple[uuid.UUID, date, str, float, bool]]]:
    """Individual transactions behind each lane's total, over [start, end] — the row
    granularity `_pnl_totals`/`_invested_and_direct` sum away. A row appears here iff
    it is one the matching total sums: income and expenses mirror `_pnl_totals`;
    invested mirrors `_invested_and_direct` exactly, including its `Account.type ==
    "investment"` test (not the effective bucket — a re-bucketed account must not
    make the two disagree on what counts). The last element is `is_direct`: always
    False for income/expenses, and for invested, True for an unpaired credit into
    an investment-typed account (a direct contribution)."""
    candidates: dict[_Lane, list[tuple[uuid.UUID, date, str, float, bool]]] = {
        "income": [], "expenses": [], "invested": [],
    }

    pnl_rows = (
        await session.execute(
            select(
                Transaction.id, ctx.report_date, Transaction.description,
                Transaction.category_id, Transaction.type, _amount_col(),
            )
            .where(*_shared_filters(ctx, workspace_id, start, end))
        )
    ).all()
    for txn_id, txn_date, description, category_id, txn_type, amount_raw in pnl_rows:
        amount = abs(float(amount_raw or 0))
        if amount <= 0:
            continue
        if txn_type == "credit":
            if category_id is not None and category_id in ctx.income_category_ids:
                candidates["income"].append((txn_id, txn_date, description, amount, False))
        else:
            # An uncategorized trade/fee in an investment-bucketed account is
            # already dropped by `pnl_row_filter` above — never a one-off.
            candidates["expenses"].append((txn_id, txn_date, description, amount, False))

    invested_rows = (
        await session.execute(
            select(
                Transaction.id, ctx.report_date, Transaction.description, Account.type,
                Transaction.type, Transaction.transfer_pair_id, _amount_col(), Category.name,
            )
            .select_from(Transaction)
            .join(Account, Transaction.account_id == Account.id)
            .join(Category, Transaction.category_id == Category.id)
            .where(
                Transaction.workspace_id == workspace_id,
                Account.is_closed == False,
                ctx.report_date >= start,
                ctx.report_date <= end,
                Transaction.source != "opening_balance",
                Transaction.status == "posted",
                Transaction.is_ignored.is_(False),
                Transaction.exclude_from_pnl.is_(False),
                Category.treat_as_transfer.is_(True),
                Category.is_ignored.is_(False),
            )
        )
    ).all()
    for txn_id, txn_date, description, acct_type, txn_type, transfer_pair_id, amount_raw, cat_name in invested_rows:
        amount = abs(float(amount_raw or 0))
        if amount <= 0 or (cat_name or "").lower() != _INVESTING_NAME:
            continue
        unpaired = transfer_pair_id is None
        if acct_type == "investment":
            if txn_type == "credit" and unpaired:
                candidates["invested"].append((txn_id, txn_date, description, amount, True))
        elif txn_type == "debit":
            candidates["invested"].append((txn_id, txn_date, description, amount, False))
    return candidates


async def detect_one_offs(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    today: date,
    *,
    settings: WealthSettingsValues,
) -> list[OneOffRow]:
    """Rows larger than max($2,500, 3x the lane's median monthly total), over full
    calendar months in the coverage window. `settings.one_off_overrides` then force
    specific rows in or out: `True` keeps a row even if small, `False` drops a
    proposal even if large. A row appears in the result when it is proposed, or
    forced in by an override.
    """
    ctx = await _build_context(session, workspace_id, settings)
    rows = await _detect_one_offs_with_ctx(session, workspace_id, ctx, today, settings)
    return [
        OneOffRow(
            transaction_id=row.transaction_id, date=row.date, description=row.description,
            amount=row.amount, lane=row.lane, proposed=row.proposed, selected=row.selected,
        )
        for row in rows
    ]


async def _detect_one_offs_with_ctx(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    ctx: _Context,
    today: date,
    settings: WealthSettingsValues,
) -> list[_OneOff]:
    cov = await coverage_window(session, workspace_id, today)
    full_months = _full_months(cov["from_date"], today)
    if not full_months:
        return []
    window_start, window_end = full_months[0], month_end(full_months[-1])

    monthly_totals: dict[_Lane, list[float]] = {"income": [], "expenses": [], "invested": []}
    for month in full_months:
        figures = await _period_figures_with_ctx(session, workspace_id, ctx, month, month_end(month))
        monthly_totals["income"].append(figures.income)
        monthly_totals["expenses"].append(figures.expenses)
        monthly_totals["invested"].append(figures.invested)

    thresholds = {
        lane: max(ONE_OFF_FLOOR, ONE_OFF_MEDIAN_MULTIPLE * statistics.median(totals))
        for lane, totals in monthly_totals.items()
    }

    candidates = await _lane_candidates(session, workspace_id, ctx, window_start, window_end)
    rows: list[_OneOff] = []
    for lane, lane_rows in candidates.items():
        threshold = thresholds[lane]
        for txn_id, txn_date, description, amount, is_direct in lane_rows:
            proposed = amount > threshold
            override = settings.one_off_overrides.get(str(txn_id))
            selected = override if override is not None else proposed
            if not (proposed or override is True):
                continue
            rows.append(_OneOff(
                transaction_id=txn_id, date=txn_date, description=description, amount=amount,
                lane=lane, proposed=proposed, selected=selected, is_direct=is_direct,
            ))
    return rows


# --- rolling ratio-of-sums (P1/S2) --------------------------------------------------


def rolling_rate(window: Sequence[PeriodFigures]) -> Optional[float]:
    """The savings rate as a ratio of sums over `window`: Σ(income - expenses +
    direct) / Σ(income + direct). Shared by the summary's `savings_rate_3m`
    (the last 3 full months) and the savings-rate series' `rolling_3m`/
    `rolling_12m` (every month's own trailing window) — a month with no data
    contributes 0 to both sums rather than being skipped (plan P1/S2), which
    falls out for free as long as the caller's `window` already covers it."""
    income = sum(f.income for f in window)
    expenses = sum(f.expenses for f in window)
    direct = sum(f.direct for f in window)
    denom = income + direct
    return (income - expenses + direct) / denom if denom > 0 else None


# --- per-category breakdown (top controllable) --------------------------------------


async def _pnl_totals_by_category(
    session: AsyncSession, workspace_id: uuid.UUID, ctx: _Context, start: date, end: date
) -> dict[uuid.UUID, float]:
    """`_pnl_totals`'s categorized debit side, kept per category instead of
    summed into one `expenses` total. Mirrors `_pnl_totals` exactly (same
    filters, same refund netting, same investment-bucket drop); uncategorized
    debits are dropped since there is no category to key them by."""
    rows = (
        await session.execute(
            select(Transaction.category_id, Transaction.type, _amount_col())
            .where(*_shared_filters(ctx, workspace_id, start, end))
        )
    ).all()
    debit_by_category: dict[uuid.UUID, float] = {}
    refund_by_category: dict[uuid.UUID, float] = {}
    for category_id, txn_type, amount_raw in rows:
        amount = abs(float(amount_raw or 0))
        if amount <= 0 or category_id is None:
            continue  # uncategorized rows (including the investment-bucket drop) have no category to key by
        if txn_type == "credit":
            if category_id not in ctx.income_category_ids:
                refund_by_category[category_id] = refund_by_category.get(category_id, 0.0) + amount
        else:
            debit_by_category[category_id] = debit_by_category.get(category_id, 0.0) + amount
    return {
        cat_id: max(0.0, total - refund_by_category.get(cat_id, 0.0))
        for cat_id, total in debit_by_category.items()
    }


async def typical_expense_by_category(
    session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID, *, settings: WealthSettingsValues
) -> dict[str, float]:
    """Monthly expense per category, typical (one-offs excluded) over the
    coverage window — what `wealth.levers.top_controllable` reads the biggest
    controllable category from. Keyed by category name; a category with
    nothing left after refunds and one-offs, or no name to report, is
    dropped."""
    ctx = await _build_context(session, workspace_id, settings)
    today = app_today()
    cov = await coverage_window(session, workspace_id, today)
    scale = cov["scale"]
    by_cat = await _pnl_totals_by_category(session, workspace_id, ctx, cov["from_date"], cov["to_date"])

    one_offs = await _detect_one_offs_with_ctx(session, workspace_id, ctx, today, settings)
    one_off_amounts = {row.transaction_id: row.amount for row in one_offs}
    one_off_expense_ids = [
        row.transaction_id for row in one_offs
        if row.selected and row.lane == "expenses" and cov["from_date"] <= row.date <= cov["to_date"]
    ]
    if one_off_expense_ids:
        cat_rows = (
            await session.execute(
                select(Transaction.id, Transaction.category_id).where(Transaction.id.in_(one_off_expense_ids))
            )
        ).all()
        for txn_id, cat_id in cat_rows:
            if cat_id is not None and cat_id in by_cat:
                by_cat[cat_id] = max(0.0, by_cat[cat_id] - one_off_amounts[txn_id])

    if not by_cat:
        return {}
    names = (
        await session.execute(select(Category.id, Category.name).where(Category.id.in_(by_cat.keys())))
    ).all()
    name_by_id = {cat_id: name for cat_id, name in names}
    return {
        name_by_id[cat_id]: round(amount * scale / 12, 2)
        for cat_id, amount in by_cat.items() if cat_id in name_by_id and amount > 0
    }
