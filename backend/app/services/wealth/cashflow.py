"""Upcoming charges, a cash forecast and the subscription audit (plan R2, B5b).

## Sources

`upcoming()` draws from four places, same as the dashboard's own forecast
(`dashboard_service._get_recurring_projections` / `_get_forecast_transactions`
/ `get_projected_transactions`), plus one new one:

  - recurring projections (debits), via `dashboard_service.get_projected_transactions`
    (transfer-like rules included — a recurring investment contribution still
    leaves the paying account, same reasoning as the dashboard's *balance*
    projections, which opt into `include_transfer_like`).
  - posted-but-future and pending real rows, via `_get_forecast_transactions`.
  - installments: a subset of the above, flagged by the Transaction's own
    installment metadata (`installment_number`/`total_installments`/
    `installment_purchase_date`, mirroring `transaction_service._is_installment`).
  - each credit card's next bill: a synced `CreditCardBill` when the provider
    exposes one (`account_service.get_credit_card_bills`), else the cycle's
    due date (`credit_card_service.get_cycle_dates`) with the card's own
    running balance (posted + pending, `dashboard_service._account_balance_at`)
    as the amount owed — the existing services, not a re-derived cycle sum.

## Dedupe rules

1. **A projection matches a real row, and is dropped**, when the real row
   (posted or pending, from `_get_forecast_transactions`) falls within
   **±3 days** of the projection's date and **±10% of the projection's
   amount**, and either (a) they share the same account *and* the real
   row's `recurring_transaction_id` equals the projection's `recurring_id`,
   or (b) they share the same merchant key (`wealth.merchant.merchant_key`).
   The real row always wins; only the projection is ever dropped.
2. **Installments on a card count once, inside that card's bill.** A real
   row that carries installment metadata and sits on a credit-card account
   is left out of `items` entirely — the card's `current_balance` (posted +
   pending) already includes it, so the bill is where it is counted. An
   installment on a non-card account (a BNPL loan debited from checking,
   say) is not folded; it has no bill to hide inside.
3. **Invoices and receivables are excluded.** Nothing here ever reads
   `invoice_forecast_service` — an invoice is a claim, not a transaction,
   and this module only ever walks real/virtual transaction rows.
4. **Recurring charges on credit-card accounts stay in `upcoming`** (the
   user wants to see the subscription itself), tagged with the card as
   their `account`, but they are card *purchases* — see "What counts as
   cash" below.

## What counts as cash

`total` (on `Upcoming`) and the daily walk in `cash_forecast` both use
`item_hits_cash(kind, bucket)`: an item hits cash when its `kind` is
`"card_bill"`, or when the account it sits on is bucketed `"cash"` (plan
P3). A card purchase — recurring, pending or an installment folded into
the bill — never hits cash on its own; only the bill does, on its due
date. An item on an investment or debt-bucketed account (other than a
card bill) is shown when it is a real/projected charge, but likewise does
not count as a cash movement.

## Cash forecast

`cash_forecast` walks `[today, today + days)` day by day: inflows are
recurring *credit* projections on cash-bucketed accounts, outflows are
recurring *debit* projections on cash-bucketed accounts plus each card's
bill on its due date. `start_cash` is today's `cash` bucket from
`valuation.latest_net_worth`; `buffer` is `settings.buffer_months *
typical_figures(...).median_month.expenses` (0 with no full month yet,
mirroring `wealth.levers.compute`). Points are emitted for today, every
date with a nonzero inflow or outflow, and the last day — the running
balance is flat between those dates, so the low point, first negative
date and first-below-buffer date are always found among the emitted
points (the balance never changes on a day silently skipped).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.app_clock import app_today
from app.core.workspace_context import WorkspaceContext
from app.models.account import Account
from app.models.recurring_transaction import RecurringTransaction
from app.models.transaction import Transaction
from app.schemas.wealth import (
    Bucket,
    CashForecast,
    CashForecastPoint,
    RecurringSuggestion,
    Subscription,
    SubscriptionAudit,
    Upcoming,
    UpcomingCharge,
    UpcomingKind,
    WealthSettingsValues,
)
from app.services import dashboard_service
from app.services._query_filters import counts_as_pnl, is_split_parent
from app.services.account_service import get_account_name, get_credit_card_bills
from app.services.credit_card_service import get_cycle_dates
from app.services.fx_rate_service import convert
from app.services.wealth.buckets import INVESTMENT_BUCKETS
from app.services.wealth.figures import account_bucket_map, typical_figures
from app.services.wealth.merchant import detect_recurring, merchant_key
from app.services.wealth.settings import load_settings
from app.services.wealth.spending import month_start
from app.services.wealth.valuation import latest_net_worth

_SUBSCRIPTION_FREQUENCIES = frozenset({"monthly"})
_MATCH_WINDOW_DAYS = 3
_MATCH_AMOUNT_TOLERANCE = 0.10


def item_hits_cash(kind: UpcomingKind, bucket: Optional[Bucket]) -> bool:
    """Whether one upcoming/forecast item moves cash on its own date.

    A card bill always does (it is the moment the card's debt leaves
    cash). Anything else only does when it sits on a cash-bucketed
    account — a card purchase (recurring, pending or installment) never
    hits cash directly; the bill does that for it (plan C11)."""
    return kind == "card_bill" or bucket == "cash"


def _is_installment(tx: Transaction) -> bool:
    """Mirrors `transaction_service._is_installment` without importing that
    module: a real row carries an installment fingerprint when all three of
    its installment fields are set."""
    return (
        tx.installment_number is not None
        and tx.total_installments is not None
        and tx.installment_purchase_date is not None
    )


def _merchant_bucket_key(description: Optional[str]) -> str:
    """The exact key `wealth.merchant.detect_recurring` groups by, so a rule's
    description and a detected merchant line up on the same key."""
    return merchant_key(description or "")[:40] or "(blank)"


@dataclass
class _RawItem:
    date: date
    description: str
    amount: float  # positive = money out
    kind: UpcomingKind
    account_id: Optional[uuid.UUID]
    account: Optional[str]
    category: Optional[str] = None
    recurring_id: Optional[uuid.UUID] = None
    is_installment: bool = False


def _matches(projection: _RawItem, real: _RawItem) -> bool:
    """Dedupe rule 1: same account + recurring id, or same merchant key,
    within ±3 days and ±10% of the projection's amount."""
    if abs((projection.date - real.date).days) > _MATCH_WINDOW_DAYS:
        return False
    proj_amount, real_amount = abs(projection.amount), abs(real.amount)
    if proj_amount <= 0 or real_amount <= 0:
        return False
    if abs(proj_amount - real_amount) > _MATCH_AMOUNT_TOLERANCE * proj_amount:
        return False
    if (
        projection.recurring_id is not None
        and projection.account_id is not None
        and projection.account_id == real.account_id
        and projection.recurring_id == real.recurring_id
    ):
        return True
    return merchant_key(projection.description) == merchant_key(real.description)


async def _card_bill_items(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    accounts: list[Account],
    primary_currency: str,
    today: date,
    days: int,
) -> list[_RawItem]:
    """Each credit-card account's next bill: a synced `CreditCardBill` when the
    provider exposes one, else the cycle's due date with the card's own
    running balance (posted + pending) as the amount owed. Skips a card with
    no cycle configured, nothing owed, or whose next bill falls outside
    `[today, today + days)`."""
    window_end = today + timedelta(days=days)
    items: list[_RawItem] = []
    for account in accounts:
        if account.type != "credit_card":
            continue

        due_date: Optional[date] = None
        amount_native = 0.0
        currency = account.currency

        synced = await get_credit_card_bills(session, account.id, workspace_id, limit=5)
        upcoming_synced = [b for b in (synced or []) if b.due_date >= today]
        if upcoming_synced:
            bill = min(upcoming_synced, key=lambda b: b.due_date)
            due_date = bill.due_date
            amount_native = float(bill.total_amount)
            currency = bill.currency
        else:
            cycle = get_cycle_dates(account.statement_close_day, account.payment_due_day, today)
            due_date = cycle["next_due_date"]
            if due_date is not None:
                balance = await dashboard_service._account_balance_at(
                    session, account, today, include_pending=True,
                )
                amount_native = -balance if balance < 0 else 0.0

        if due_date is None or amount_native <= 0 or not (today <= due_date < window_end):
            continue

        converted, _ = await convert(
            session, Decimal(str(amount_native)), currency, primary_currency, today,
        )
        items.append(_RawItem(
            date=due_date, description=f"{get_account_name(account)} bill", amount=round(float(converted), 2),
            kind="card_bill", account_id=account.id, account=get_account_name(account),
        ))
    return items


# --- upcoming ---------------------------------------------------------------


async def upcoming(session: AsyncSession, ctx: WorkspaceContext, days: int) -> Upcoming:
    """Charges due in `[today, today + days)` — see the module docstring for
    sources, the dedupe rules and what counts toward `total`."""
    today = app_today()
    workspace_id, user_id = ctx.id, ctx.user_id
    primary_currency = ctx.user.primary_currency
    settings = load_settings(ctx.user)
    range_end = today + timedelta(days=days)

    accounts = await dashboard_service._get_open_accounts(session, workspace_id)
    account_name = {a.id: get_account_name(a) for a in accounts}
    buckets = await account_bucket_map(session, workspace_id, settings.accounts)

    projected = await dashboard_service.get_projected_transactions(
        session, workspace_id, user_id, from_date=today, to_date=range_end - timedelta(days=1),
    )
    recurring_items: list[_RawItem] = []
    for p in projected:
        if p.type != "debit":
            continue
        acct_id = uuid.UUID(p.account_id) if p.account_id else None
        recurring_items.append(_RawItem(
            date=date.fromisoformat(p.date), description=p.description,
            amount=float(p.amount_primary if p.amount_primary is not None else p.amount),
            kind="recurring", account_id=acct_id, account=account_name.get(acct_id) if acct_id else None,
            category=p.category_name, recurring_id=uuid.UUID(p.recurring_id) if p.recurring_id else None,
        ))

    forecast_rows = await dashboard_service._get_forecast_transactions(session, workspace_id, today, range_end)
    real_items: list[_RawItem] = []
    for tx in forecast_rows:
        if tx.type != "debit":
            continue
        real_items.append(_RawItem(
            date=tx.date, description=tx.description,
            amount=float(tx.amount_primary if tx.amount_primary is not None else tx.amount),
            kind="installment" if _is_installment(tx) else "pending",
            account_id=tx.account_id, account=account_name.get(tx.account_id),
            category=tx.category.name if tx.category else None,
            recurring_id=tx.recurring_transaction_id, is_installment=_is_installment(tx),
        ))

    # Rule 1: drop a projection matched by a real row.
    deduped_recurring = [r for r in recurring_items if not any(_matches(r, x) for x in real_items)]

    # Rule 2: fold a card's installment rows into its bill, not listed on their own.
    visible_real = [
        x for x in real_items
        if not (x.is_installment and buckets.get(x.account_id) == "card_debt")
    ]

    card_bills = await _card_bill_items(session, workspace_id, accounts, primary_currency, today, days)

    all_items = sorted(deduped_recurring + visible_real + card_bills, key=lambda it: it.date)
    total = round(sum(it.amount for it in all_items if item_hits_cash(it.kind, buckets.get(it.account_id))), 2)

    return Upcoming(
        currency=primary_currency, days=days, total=total,
        items=[
            UpcomingCharge(
                date=it.date, description=it.description, amount=round(it.amount, 2), kind=it.kind,
                account=it.account, category=it.category, recurring_id=it.recurring_id,
            )
            for it in all_items
        ],
    )


# --- cash forecast -----------------------------------------------------------


async def cash_forecast(
    session: AsyncSession, ctx: WorkspaceContext, settings: WealthSettingsValues, days: int,
) -> CashForecast:
    """Liquid cash over `[today, today + days)` — see the module docstring."""
    today = app_today()
    workspace_id, user_id = ctx.id, ctx.user_id
    primary_currency = ctx.user.primary_currency
    range_end = today + timedelta(days=days)
    last_day = range_end - timedelta(days=1)

    point = await latest_net_worth(session, workspace_id, user_id, settings.accounts)
    start_cash = point.buckets.get("cash", 0.0)

    typical = await typical_figures(session, workspace_id, user_id, settings=settings)
    median_expenses = typical.median_month.expenses
    buffer = round(settings.buffer_months * median_expenses, 2) if median_expenses is not None else 0.0

    accounts = await dashboard_service._get_open_accounts(session, workspace_id)
    buckets = await account_bucket_map(session, workspace_id, settings.accounts)

    projected = await dashboard_service.get_projected_transactions(
        session, workspace_id, user_id, from_date=today, to_date=last_day,
    )
    daily_in: dict[date, float] = {}
    daily_out: dict[date, float] = {}
    for p in projected:
        acct_id = uuid.UUID(p.account_id) if p.account_id else None
        if not item_hits_cash("recurring", buckets.get(acct_id)):
            continue
        amt = float(p.amount_primary if p.amount_primary is not None else p.amount)
        d = date.fromisoformat(p.date)
        if p.type == "credit":
            daily_in[d] = daily_in.get(d, 0.0) + amt
        elif p.type == "debit":
            daily_out[d] = daily_out.get(d, 0.0) + amt

    for bill in await _card_bill_items(session, workspace_id, accounts, primary_currency, today, days):
        daily_out[bill.date] = daily_out.get(bill.date, 0.0) + bill.amount

    required_dates = sorted({today, last_day} | set(daily_in) | set(daily_out))

    running = start_cash
    points: list[CashForecastPoint] = []
    low_point: Optional[CashForecastPoint] = None
    first_negative_date: Optional[date] = None
    first_below_buffer_date: Optional[date] = None
    for d in required_dates:
        inflow = daily_in.get(d, 0.0)
        outflow = daily_out.get(d, 0.0)
        running = round(running + inflow - outflow, 2)
        pt = CashForecastPoint(date=d, balance=running, inflow=round(inflow, 2), outflow=round(outflow, 2))
        points.append(pt)
        if low_point is None or pt.balance < low_point.balance:
            low_point = pt
        if first_negative_date is None and pt.balance < 0:
            first_negative_date = d
        if first_below_buffer_date is None and pt.balance < buffer:
            first_below_buffer_date = d

    return CashForecast(
        currency=primary_currency, days=days, start_cash=round(start_cash, 2), buffer=buffer,
        points=points, low_point=low_point, first_negative_date=first_negative_date,
        first_below_buffer_date=first_below_buffer_date,
    )


# --- subscription audit ------------------------------------------------------


@dataclass
class _LastCharge:
    date: date
    account_id: uuid.UUID
    category_id: Optional[uuid.UUID]
    description: str
    native_amount: float
    native_currency: str


async def subscription_audit(session: AsyncSession, ctx: WorkspaceContext) -> SubscriptionAudit:
    """Active monthly debit recurring rules, plus merchants `detect_recurring`
    finds over the last 4 full months of posted P&L debits (merged onto a
    rule by merchant key against the rule's description). A merchant with no
    matching rule becomes a suggestion, with a `create_payload` valid against
    `POST /api/recurring-transactions`."""
    today = app_today()
    workspace_id = ctx.id
    primary_currency = ctx.user.primary_currency
    settings = load_settings(ctx.user)
    buckets = await account_bucket_map(session, workspace_id, settings.accounts)

    window_start = month_start(today, 4)
    rows = (
        await session.execute(
            select(
                Transaction.date, Transaction.description, Transaction.amount, Transaction.amount_primary,
                Transaction.currency, Transaction.account_id, Transaction.category_id,
            )
            .where(
                Transaction.workspace_id == workspace_id,
                Transaction.status == "posted",
                Transaction.source != "opening_balance",
                Transaction.type == "debit",
                Transaction.date >= window_start,
                Transaction.date <= today,
                ~is_split_parent(),
                counts_as_pnl(),
            )
        )
    ).all()

    detect_rows: list[tuple[date, str, float]] = []
    last_seen: dict[str, _LastCharge] = {}
    for when, description, amount, amount_primary, currency, account_id, category_id in rows:
        if category_id is None and buckets.get(account_id) in INVESTMENT_BUCKETS:
            continue  # an uncategorized trade/dividend/fee, never a subscription (P1)
        primary_amount = float(amount_primary if amount_primary is not None else amount)
        detect_rows.append((when, description or "", primary_amount))
        key = _merchant_bucket_key(description)
        prev = last_seen.get(key)
        if prev is None or when > prev.date:
            last_seen[key] = _LastCharge(
                date=when, account_id=account_id, category_id=category_id, description=description or key,
                native_amount=float(amount), native_currency=currency,
            )

    detected_by_key = {d["merchant"]: d for d in detect_recurring(detect_rows, today)}

    rules = (
        await session.execute(
            select(RecurringTransaction).where(
                RecurringTransaction.workspace_id == workspace_id,
                RecurringTransaction.is_active == True,
                RecurringTransaction.type == "debit",
                RecurringTransaction.frequency.in_(_SUBSCRIPTION_FREQUENCIES),
            )
        )
    ).scalars().all()
    rule_by_key: dict[str, RecurringTransaction] = {
        _merchant_bucket_key(rec.description): rec for rec in rules
    }

    subscriptions: list[Subscription] = []
    for key, rec in rule_by_key.items():
        detected = detected_by_key.get(key)
        seen = last_seen.get(key)
        rec_amount = float(rec.amount_primary if rec.amount_primary is not None else rec.amount)
        subscriptions.append(Subscription(
            merchant=rec.description, monthly=round(rec_amount, 2), annual=round(rec_amount * 12, 2),
            months_seen=detected["months"] if detected else 0,
            last_charge=seen.date if seen else None, recurring_id=rec.id,
        ))

    suggestions: list[RecurringSuggestion] = []
    for key, detected in detected_by_key.items():
        if key in rule_by_key:
            continue  # already tracked by a rule
        seen = last_seen.get(key)
        if seen is None:
            continue
        monthly = round(detected["monthly"], 2)
        subscriptions.append(Subscription(
            merchant=seen.description, monthly=monthly, annual=round(monthly * 12, 2),
            months_seen=detected["months"], last_charge=seen.date, recurring_id=None,
        ))
        create_payload = {
            "description": seen.description, "amount": seen.native_amount, "currency": seen.native_currency,
            "type": "debit", "frequency": "monthly", "day_of_month": seen.date.day,
            "start_date": seen.date.isoformat(), "account_id": str(seen.account_id),
            "category_id": str(seen.category_id) if seen.category_id else None,
        }
        suggestions.append(RecurringSuggestion(
            merchant=seen.description, monthly=monthly, months=detected["months"], create_payload=create_payload,
        ))

    return SubscriptionAudit(
        currency=primary_currency, subscriptions=subscriptions,
        monthly_total=round(sum(s.monthly for s in subscriptions), 2),
        annual_total=round(sum(s.annual for s in subscriptions), 2),
        suggestions=suggestions,
    )
