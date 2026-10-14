"""Contracts for /api/wealth (fork, Wealth Cockpit R1).

The lead owns this file: slices implement against it and do not change a
shape without agreement. Money is a float in the workspace's primary
currency (`currency` on each response), rates are fractions (0.24, not 24),
dates are calendar dates in the workspace timezone. Definitions live in the
plan's pre-registered decisions (P1 figures, P2 valuation, P3 buckets, P5
projection); the docstrings below name the decision each field follows.
"""
from __future__ import annotations

import uuid
from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field

# P3 buckets. Debts are negative amounts inside their bucket.
Bucket = Literal[
    "cash", "taxable", "retirement_pretax", "retirement_roth", "retirement_hsa",
    "crypto", "property_other", "card_debt", "other_debt",
]
BUCKETS: tuple[Bucket, ...] = (
    "cash", "taxable", "retirement_pretax", "retirement_roth", "retirement_hsa",
    "crypto", "property_other", "card_debt", "other_debt",
)
# P2 point flags: `estimated` = back-computed before the first holdings row,
# `cash_flat` = cash part of a holdings account held at its last-sync value,
# `no_holdings` = investment account without holdings (market moves missing).
PointFlag = Literal["estimated", "cash_flat", "no_holdings"]
SettingSource = Literal["default", "user"]
Precision = Literal["quarter", "month"]
LeverKey = Literal["idle_cash", "plus_500", "plus_1000", "plus_2000", "cut_top", "return_plus_1", "return_minus_1"]


class Coverage(BaseModel):
    """The span yearly figures are annualized over (`app.services.wealth.coverage`)."""

    from_date: date
    to_date: date
    days: int
    annualized: bool
    insufficient_history: bool


# --- settings (P6) ------------------------------------------------------------


class AccountAnnotation(BaseModel):
    """A user override for one account. `None` fields fall back to the default."""

    bucket: Optional[Bucket] = None
    liquid: Optional[bool] = None
    apr: Optional[float] = Field(None, ge=0, le=100, description="Card APR in percent, e.g. 24.99")
    # Institution + mask + normalized name, written by the server so an override
    # survives a reconnect that creates new account ids (C15).
    fallback_key: Optional[str] = None


class ProjectionSettings(BaseModel):
    return_nominal: float = Field(0.06, ge=-0.05, le=0.15)
    withdrawal_rate: float = Field(0.04, gt=0, le=0.10)


class WealthSettingsValues(BaseModel):
    """What is stored in User.preferences["wealth"] (versioned)."""

    schema_version: int = 1
    goal_id: Optional[uuid.UUID] = None
    projection: ProjectionSettings = ProjectionSettings()
    buffer_months: float = Field(3.0, ge=0, le=24)
    # None = the default list from `wealth.classify.is_income_like`.
    income_category_ids: Optional[list[uuid.UUID]] = None
    # transaction id -> True (treat as one-off) / False (not a one-off), overriding the detector.
    one_off_overrides: dict[str, bool] = {}
    accounts: dict[str, AccountAnnotation] = {}
    # R2: a monthly spending cap the user sets; None = no cap.
    spending_cap: Optional[float] = Field(None, gt=0)


class AccountSettingRow(BaseModel):
    account_id: Optional[uuid.UUID]  # None for an orphaned override (account gone)
    name: str
    type: Optional[str]
    institution: Optional[str]
    default_bucket: Optional[Bucket]
    bucket: Bucket
    liquid: bool
    apr: Optional[float]
    source: SettingSource
    orphaned: bool = False
    override_key: str  # the key in `accounts` this row reads from


class IncomeCategoryRow(BaseModel):
    id: uuid.UUID
    name: str
    group_name: Optional[str]
    is_default: bool
    selected: bool


class OneOffRow(BaseModel):
    transaction_id: uuid.UUID
    date: date
    description: str
    amount: float
    lane: Literal["income", "expenses", "invested"]
    proposed: bool  # the detector's suggestion (P1)
    selected: bool  # effective, after the user's override


class WealthSettingsRead(BaseModel):
    values: WealthSettingsValues  # effective values
    sources: dict[str, SettingSource]  # top-level field -> where its value came from
    accounts: list[AccountSettingRow]
    income_categories: list[IncomeCategoryRow]
    one_offs: list[OneOffRow]


class WealthSettingsUpdate(BaseModel):
    """Partial update; omitted fields are kept. `accounts` merges per key, and a
    `null` value removes that override. Merged server-side under a row lock."""

    goal_id: Optional[uuid.UUID] = None
    clear_goal: bool = False
    projection: Optional[ProjectionSettings] = None
    buffer_months: Optional[float] = Field(None, ge=0, le=24)
    income_category_ids: Optional[list[uuid.UUID]] = None
    reset_income_categories: bool = False
    one_off_overrides: Optional[dict[str, Optional[bool]]] = None
    accounts: Optional[dict[str, Optional[AccountAnnotation]]] = None
    spending_cap: Optional[float] = Field(None, gt=0)
    clear_spending_cap: bool = False


# --- summary (hero) -----------------------------------------------------------


class GoalProgress(BaseModel):
    id: uuid.UUID
    name: str
    target: float
    target_date: Optional[date]
    current: float  # equals WealthSummary.net_worth (P6)
    pct: float  # 0..1+, current / target


class WealthSummary(BaseModel):
    as_of: date
    currency: str
    net_worth: float  # the dashboard figure, to the cent (P2)
    liquid: float  # P3
    goal: Optional[GoalProgress]
    projected_date: Optional[date]  # base scenario (P5); None beyond the horizon
    projected_label: Optional[str]  # "Q1 2029" or "Mar 2029" per `precision`
    precision: Precision
    coverage: Coverage
    savings_rate_3m: Optional[float]  # P1 definition, last 3 full months
    cash_flow_typical_monthly: Optional[float]  # P1, median month, one-offs excluded
    idle_cash: float
    cash_cushion_months: Optional[float]


# --- net worth history (P2/P3) -------------------------------------------------


class NetWorthPoint(BaseModel):
    date: date
    total: float
    liquid: float
    buckets: dict[Bucket, float]  # every bucket present, 0.0 when empty
    flags: list[PointFlag] = []


class NetWorthHistory(BaseModel):
    currency: str
    synced_from: Optional[date]  # first daily holdings row; charts default to this
    points: list[NetWorthPoint]  # month ends, then today; the last equals the dashboard


# --- savings rate (P1) ----------------------------------------------------------


class SavingsMonth(BaseModel):
    month: date  # first day of the month
    income: float
    expenses: float
    direct: float  # direct contributions
    rate: Optional[float]  # (income - expenses + direct) / (income + direct)
    cash_rate: Optional[float]  # (income - expenses) / income
    rolling_3m: Optional[float]  # ratio of sums over the last 3 months
    rolling_12m: Optional[float]
    in_coverage: bool
    partial: bool  # the current month


class SavingsRateSeries(BaseModel):
    currency: str
    months: list[SavingsMonth]


# --- projection (P5) ------------------------------------------------------------


class ProjectionPathPoint(BaseModel):
    month: date
    balance: float  # invested balance; add `non_invested` for net worth


class ProjectionScenario(BaseModel):
    annual_return: float
    months_to_target: Optional[int]  # None beyond the 360-month horizon
    date: Optional[date]
    label: Optional[str]
    path: list[ProjectionPathPoint]


class ProjectionInputs(BaseModel):
    invested: float
    non_invested: float
    target_total: float  # the goal's target
    target_invested: float  # target_total - non_invested
    monthly_contribution: float  # typical annual saving / 12; may be negative
    typical_annual_saving: float
    nominal: bool = True
    coverage: Coverage


class LeverResult(BaseModel):
    key: LeverKey
    amount: Optional[float]  # lump for idle_cash, monthly for plus_* and cut_top, None for return levers
    months_to_target: Optional[int]
    date: Optional[date]
    label: Optional[str]
    shift_months: Optional[int]  # negative = earlier than the base scenario


class FiFigures(BaseModel):
    fi_number_real: Optional[float]  # typical annual expenses / withdrawal rate, today's dollars
    annual_expenses_typical: Optional[float]
    withdrawal_rate: float


class WealthProjection(BaseModel):
    currency: str
    as_of: date
    precision: Precision
    inputs: ProjectionInputs
    scenarios: dict[Literal["low", "base", "high"], ProjectionScenario]
    levers: list[LeverResult]
    fi: FiFigures


# --- levers (decision-changing figures) ----------------------------------------


class CashFlow(BaseModel):
    typical_monthly: Optional[float]  # median full month, one-offs excluded
    average_monthly: Optional[float]  # annualized average / 12, all rows
    months: int  # full months the median is taken over


class TopControllable(BaseModel):
    category: str
    monthly: float
    band_cut: Optional[float]  # the monthly cut that reaches the next savings band


class ThisMonthMove(BaseModel):
    rule: str  # a `wealth.policy` rule id
    text: str  # code-written, grounded in the figures
    amount: Optional[float]


class WealthLevers(BaseModel):
    currency: str
    as_of: date
    cash: float
    buffer: float
    idle_cash: float
    cash_cushion_months: Optional[float]
    cash_flow: CashFlow
    top_controllable: Optional[TopControllable]
    move: Optional[ThisMonthMove]
    investing_exceeds_saving: bool  # on the median month only


# --- R2: spend smarter --------------------------------------------------------

PaceStatus = Literal["under", "on_pace", "over", "no_budget"]
UpcomingKind = Literal["recurring", "card_bill", "installment", "pending"]


class BudgetSuggestion(BaseModel):
    """One category's suggested monthly budget: the median of its full covered months
    (refunds netted, one-offs excluded, the same split-share semantics as budget actuals)."""

    category_id: uuid.UUID
    category: str
    group_name: Optional[str]
    monthly_median: float
    months_used: int
    existing_budget: Optional[float]  # the category's current monthly budget, if any


class BudgetSuggestions(BaseModel):
    currency: str
    months: list[date]  # the full months the medians come from (first day of each)
    suggestions: list[BudgetSuggestion]
    total_suggested: float  # sum of the medians; not the typical total
    typical_monthly_spending: Optional[float]  # P1 median month, for comparison


class ApplyBudgetSuggestions(BaseModel):
    category_ids: list[uuid.UUID]


class ApplyBudgetSuggestionsResult(BaseModel):
    created: int
    skipped: int  # already had a recurring budget


class CapPace(BaseModel):
    cap: float
    spent: float
    expected_to_date: float  # recurring scheduled to date + discretionary share of the rest
    projected_month_end: float
    status: PaceStatus
    left_to_spend: float
    left_per_day: Optional[float]


class BudgetPace(BaseModel):
    """The month's pace for the spending cap (budget rows carry their own pace on
    /api/budgets/comparison)."""

    currency: str
    month: date
    days_elapsed: int  # workspace timezone, today included
    days_in_month: int
    cap: Optional[CapPace]
    spent: float  # P1 expenses month to date


class UpcomingCharge(BaseModel):
    date: date
    description: str
    amount: float  # positive = money out
    kind: UpcomingKind
    account: Optional[str]
    category: Optional[str]
    recurring_id: Optional[uuid.UUID]
    # False for a card purchase: it reaches cash only through the card's bill.
    hits_cash: bool = True
    # True when not scheduled by a rule or a provider: an estimated card bill, or a
    # recurring charge or income detected from history that no rule tracks yet.
    estimated: bool = False


class Upcoming(BaseModel):
    currency: str
    days: int
    items: list[UpcomingCharge]
    total: float


class CashForecastPoint(BaseModel):
    date: date
    balance: float
    inflow: float
    outflow: float


class CashForecast(BaseModel):
    """Liquid cash over the next `days`: today's cash bucket, plus scheduled inflows
    (recurring income), minus scheduled outflows (bills, card bills; card purchases reach
    cash only through the bill)."""

    currency: str
    days: int
    start_cash: float
    buffer: float
    points: list[CashForecastPoint]
    low_point: Optional[CashForecastPoint]
    first_negative_date: Optional[date]
    first_below_buffer_date: Optional[date]
    includes_estimates: bool = False  # detected recurring, estimated bills or typical spending are in it
    # What the user can set up to replace estimates with scheduled amounts.
    setup_hints: list[Literal["card_due_days", "recurring_income", "recurring_bills"]] = []


class Subscription(BaseModel):
    merchant: str
    monthly: float
    annual: float
    months_seen: int
    last_charge: Optional[date]
    recurring_id: Optional[uuid.UUID]  # set when a recurring rule already tracks it


class RecurringSuggestion(BaseModel):
    merchant: str
    monthly: float
    months: int
    create_payload: dict  # body for POST /api/recurring-transactions


class SubscriptionAudit(BaseModel):
    currency: str
    subscriptions: list[Subscription]
    monthly_total: float
    annual_total: float
    suggestions: list[RecurringSuggestion]  # detected but not yet tracked

