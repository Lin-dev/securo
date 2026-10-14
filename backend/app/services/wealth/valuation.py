"""Net worth over time, by bucket (plan P2 and P3).

Investment accounts whose holdings are synced from a provider are valued
from those holdings instead of the back-computed transaction ledger, so a
price move shows up in history even when nothing was bought or sold. Every
other account keeps the existing back-computed-balance behaviour the
dashboard and reports already use.

The latest point (today) is built from exactly the functions the dashboard
uses (`dashboard_service._total_balance_by_currency` plus the counted
partition of `asset_service.split_asset_values_at`), not from the
holdings-plus-cash-offset formula below, so it matches the dashboard's net
worth to the cent even though the two are mathematically equivalent at d =
today. `goal_service` and `report_service._net_worth_at` both delegate here
so the Goal, the dashboard, the Reports page and the wealth hero always
agree.
"""
from __future__ import annotations

import uuid
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.app_clock import app_today
from app.core.config import get_settings
from app.models.account import Account
from app.models.asset import Asset
from app.models.asset_value import AssetValue
from app.models.category import Category
from app.models.transaction import Transaction
from app.models.user import User
from app.schemas.wealth import AccountAnnotation, BUCKETS, Bucket, NetWorthHistory, NetWorthPoint, PointFlag
from app.services.account_service import get_account_name
from app.services.asset_service import (
    _fill_forward_at,
    _load_active_assets,
    _load_asset_native_values,
    is_account_backed,
    linked_account_id,
    split_asset_values_at,
)
from app.services.dashboard_service import _account_balance_at, _get_open_accounts, _total_balance_by_currency
from app.services.fx_rate_service import convert
from app.services.wealth.buckets import AccountFacts, BucketChoice, LIQUID_BUCKETS, asset_bucket, choose_bucket

_EMPTY_OVERRIDES: dict[str, AccountAnnotation] = {}

# A row at the asset's `purchase_date`, followed by a gap longer than this
# before its next row, is the one-time historical seed `_ensure_historical_seed`
# writes at sync time — not a real account sync (plan P2, critique C2).
_SEED_GAP_DAYS = 7


# ---------------------------------------------------------------------------
# Pure date/series helpers
# ---------------------------------------------------------------------------


def _month_end_points(today: date, months: int) -> list[date]:
    """Calendar month-end dates for the `months` completed months before today.

    The current (partial) month is deliberately excluded here — callers add
    `today` itself as the extra, final point, so the partial month is
    represented once, by its live balance, not by a stale month-end guess.
    """
    points: list[date] = []
    year, month = today.year, today.month
    for _ in range(max(months, 0)):
        month -= 1
        if month == 0:
            month, year = 12, year - 1
        if month == 12:
            next_first = date(year + 1, 1, 1)
        else:
            next_first = date(year, month + 1, 1)
        points.append(next_first - timedelta(days=1))
    points.reverse()
    return points


def _latest_leq(sorted_dates: list[date], as_of: date) -> Optional[date]:
    """The latest entry in an ascending, de-duplicated date list that is <= as_of."""
    idx = bisect_right(sorted_dates, as_of)
    return sorted_dates[idx - 1] if idx else None


def _value_at(rows: list[tuple[date, float]], as_of: date) -> Optional[tuple[date, float]]:
    """The latest (date, amount) in ascending `rows` with date <= as_of."""
    idx = bisect_right([d for d, _ in rows], as_of)
    return rows[idx - 1] if idx else None


class _PrefixSeries:
    """Cumulative sums of a sparse (date -> delta) series, for O(log n) lookups
    instead of a per-date database query. `pending` is a connected account's
    constant pending-transaction delta (dated on or before today, regardless
    of cutoff), mirroring `dashboard_service._account_balance_at`."""

    def __init__(self, deltas: dict[date, float], pending: float = 0.0):
        self._dates = sorted(deltas)
        running = 0.0
        self._prefix: list[float] = []
        for d in self._dates:
            running += deltas[d]
            self._prefix.append(running)
        self.total = running
        self.pending = pending

    def sum_leq(self, as_of: date) -> float:
        idx = bisect_right(self._dates, as_of)
        return self._prefix[idx - 1] if idx else 0.0

    def sum_gt(self, as_of: date) -> float:
        return self.total - self.sum_leq(as_of)


# ---------------------------------------------------------------------------
# Holdings carry-forward (P2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HoldingsValuation:
    """An account's linked-holdings value H(d), in the account's own currency.

    `values` holds H(d) for exactly the requested dates. `synced_from`/
    `last_sync` are the account's earliest/latest real sync date (seed rows
    excluded); both are None when there is nothing to carry forward, which
    callers treat the same as "no linked holdings" (`no_holdings`).
    """

    values: dict[date, float]
    synced_from: Optional[date]
    last_sync: Optional[date]
    value_at_last_sync: float


def _exclude_seed_rows(asset: Asset, rows: list[AssetValue]) -> list[AssetValue]:
    """Drop the one-time historical seed `_ensure_historical_seed` writes at
    `purchase_date`: a `sync` row dated exactly `purchase_date`, followed by a
    gap of more than `_SEED_GAP_DAYS` days before the next row. A seed with no
    later row, or one close enough to not look like a gap, is kept — it is
    indistinguishable from a real early sync.
    """
    if not rows or asset.purchase_date is None:
        return rows
    ordered = sorted(rows, key=lambda r: (r.date, r.id))
    first = ordered[0]
    if first.date != asset.purchase_date or first.source != "sync":
        return rows
    if len(ordered) < 2:
        return rows
    gap = (ordered[1].date - first.date).days
    return ordered[1:] if gap > _SEED_GAP_DAYS else ordered


async def holdings_value_by_account(
    session: AsyncSession,
    account: Account,
    assets: list[Asset],
    rows_by_asset: dict[uuid.UUID, list[AssetValue]],
    dates: list[date],
) -> HoldingsValuation:
    """H(d) for one account's linked holdings, per plan P2.

    A holding's value is carried forward from its latest row on or before d.
    It drops to 0 only once a later sync of the SAME ACCOUNT leaves it out —
    a sync of the account is a date on which any of its holdings got a
    `sync`-sourced row (seed rows excluded). Archived holdings that still get
    rows keep counting; nothing here filters on `is_archived` or `sell_date`.

    `rows_by_asset` is pre-loaded by the caller in one bulk query across every
    linked account, so this function issues no AssetValue queries of its own.
    """
    per_asset_rows: dict[uuid.UUID, list[tuple[date, float]]] = {}
    sync_dates: set[date] = set()
    for asset in assets:
        kept = _exclude_seed_rows(asset, rows_by_asset.get(asset.id, []))
        ordered = sorted(kept, key=lambda r: (r.date, r.id))
        per_asset_rows[asset.id] = [(r.date, float(r.amount)) for r in ordered]
        for r in ordered:
            if r.source == "sync":
                sync_dates.add(r.date)

    sorted_sync_dates = sorted(sync_dates)
    if not sorted_sync_dates:
        return HoldingsValuation(values={d: 0.0 for d in dates}, synced_from=None, last_sync=None, value_at_last_sync=0.0)

    synced_from = sorted_sync_dates[0]
    last_sync = sorted_sync_dates[-1]

    async def _holding_value_native(asset: Asset, as_of: date) -> float:
        rows = per_asset_rows.get(asset.id, [])
        last_account_sync = _latest_leq(sorted_sync_dates, as_of)
        if last_account_sync is None:
            return 0.0
        found = _value_at(rows, as_of)
        if found is None:
            return 0.0
        row_date, amount = found
        if row_date < last_account_sync:
            # A later account sync happened and this holding was not in it:
            # it was sold or dropped, so it stops counting from there on.
            return 0.0
        if amount == 0.0 or asset.currency == account.currency:
            return amount
        converted, _ = await convert(session, Decimal(str(amount)), asset.currency, account.currency, as_of)
        return float(converted)

    values: dict[date, float] = {}
    for d in dates:
        values[d] = sum([await _holding_value_native(asset, d) for asset in assets])

    value_at_last_sync = sum([await _holding_value_native(asset, last_sync) for asset in assets])

    return HoldingsValuation(
        values=values, synced_from=synced_from, last_sync=last_sync, value_at_last_sync=value_at_last_sync,
    )


# ---------------------------------------------------------------------------
# Back-computed balances, bulk-loaded (the non-holdings and pre-sync path)
# ---------------------------------------------------------------------------


def _current_native_balance(account: Account) -> float:
    """The account's own current balance, sign-adjusted like `_account_balance_at`."""
    bal = float(account.balance)
    return -bal if account.type == "credit_card" else bal


async def _native_balances_by_account(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    accounts: list[Account],
    today: date,
) -> dict[uuid.UUID, "_PrefixSeries"]:
    """Each account's signed daily transaction deltas, one bulk query (plus one
    for pending rows), returned as a lookup series — no per-date, per-account
    round trip. Mirrors `dashboard_service._account_balance_at`'s own math.
    """
    if not accounts:
        return {}
    account_ids = [a.id for a in accounts]
    effective = case(
        (Transaction.currency == Account.currency, Transaction.amount),
        else_=func.coalesce(Transaction.amount_primary, Transaction.amount),
    )
    signed = case((Transaction.type == "credit", effective), else_=-effective)

    posted_rows = await session.execute(
        select(Transaction.account_id, Transaction.date, func.coalesce(func.sum(signed), 0))
        .join(Account, Transaction.account_id == Account.id)
        .outerjoin(Category, Transaction.category_id == Category.id)
        .where(
            Transaction.workspace_id == workspace_id,
            Transaction.account_id.in_(account_ids),
            Transaction.date <= today,
            Transaction.status == "posted",
            Transaction.is_ignored == False,
            or_(Transaction.category_id.is_(None), Category.is_ignored == False),
        )
        .group_by(Transaction.account_id, Transaction.date)
    )
    deltas_by_account: dict[uuid.UUID, dict[date, float]] = {a.id: {} for a in accounts}
    for account_id, tx_date, amount in posted_rows.all():
        deltas_by_account.setdefault(account_id, {})[tx_date] = float(amount or 0)

    pending_rows = await session.execute(
        select(Transaction.account_id, func.coalesce(func.sum(signed), 0))
        .join(Account, Transaction.account_id == Account.id)
        .outerjoin(Category, Transaction.category_id == Category.id)
        .where(
            Transaction.workspace_id == workspace_id,
            Transaction.account_id.in_(account_ids),
            Transaction.date <= today,
            Transaction.status == "pending",
            Transaction.is_ignored == False,
            or_(Transaction.category_id.is_(None), Category.is_ignored == False),
        )
        .group_by(Transaction.account_id)
    )
    pending_by_account: dict[uuid.UUID, float] = {account_id: float(amount or 0) for account_id, amount in pending_rows.all()}

    series: dict[uuid.UUID, _PrefixSeries] = {}
    for account in accounts:
        series[account.id] = _PrefixSeries(
            deltas_by_account.get(account.id, {}), pending_by_account.get(account.id, 0.0),
        )
    return series


def _balance_at_native(account: Account, series: "_PrefixSeries", as_of: date, today: date) -> float:
    """One account's back-computed balance at `as_of`, native currency."""
    if account.connection_id:
        if as_of >= today:
            return _current_native_balance(account)
        return _current_native_balance(account) - series.sum_gt(as_of) - series.pending
    return series.sum_leq(min(as_of, today))


# ---------------------------------------------------------------------------
# Account classification: holdings / no_holdings / plain
# ---------------------------------------------------------------------------


@dataclass
class _AccountPlan:
    account: Account
    bucket_choice: BucketChoice
    kind: str  # "holdings", "no_holdings", "plain"
    synced_from: Optional[date] = None
    last_sync: Optional[date] = None
    cash_offset: float = 0.0  # C, native to account.currency
    holdings: dict[date, float] = field(default_factory=dict)  # H(d), native to account.currency


async def _linked_assets_by_account(
    session: AsyncSession, workspace_id: uuid.UUID, investment_account_ids: list[uuid.UUID],
) -> dict[uuid.UUID, list[Asset]]:
    """Every asset (any archived state) linked via `ACCOUNT_LINK_METADATA_KEY`
    to one of the given accounts. Archived holdings are included deliberately
    (plan P2): they keep counting as long as they still get sync rows.
    """
    if not investment_account_ids:
        return {}
    ids = {str(aid) for aid in investment_account_ids}
    result = await session.execute(select(Asset).where(Asset.workspace_id == workspace_id))
    by_account: dict[uuid.UUID, list[Asset]] = {}
    for asset in result.scalars().all():
        linked = linked_account_id(asset)
        if linked in ids:
            by_account.setdefault(uuid.UUID(linked), []).append(asset)
    return by_account


async def _build_account_plans(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    accounts: list[Account],
    overrides: dict[str, AccountAnnotation],
    dates: list[date],
    today: date,
) -> list[_AccountPlan]:
    """Classify every open account once: which valuation mode it uses, its
    bucket, and (for holdings accounts) H(d) over the requested dates plus
    the constant cash offset C = balance today - H(last sync).
    """
    investment_ids = [a.id for a in accounts if a.type == "investment"]
    linked_by_account = await _linked_assets_by_account(session, workspace_id, investment_ids)

    all_linked_assets = [asset for assets in linked_by_account.values() for asset in assets]
    rows_by_asset: dict[uuid.UUID, list[AssetValue]] = {}
    if all_linked_assets:
        rows_result = await session.execute(
            select(AssetValue).where(AssetValue.asset_id.in_([a.id for a in all_linked_assets]))
        )
        for row in rows_result.scalars().all():
            rows_by_asset.setdefault(row.asset_id, []).append(row)

    plans: list[_AccountPlan] = []
    for account in accounts:
        facts = AccountFacts(
            id=str(account.id),
            name=get_account_name(account),
            type=account.type,
            balance=_current_native_balance(account),
            institution=account.institution.name if account.institution else None,
            masked_number=account.masked_number,
        )
        bucket_choice = choose_bucket(facts, overrides)

        linked_assets = linked_by_account.get(account.id, [])
        if account.type == "investment" and linked_assets:
            # `last_sync` may fall outside the requested `dates` (e.g. a stale
            # connection); always include it so H(last_sync) is available for C.
            needed_dates = sorted(set(dates))
            valuation = await holdings_value_by_account(session, account, linked_assets, rows_by_asset, needed_dates)
            if valuation.synced_from is not None:
                cash_offset = _current_native_balance(account) - valuation.value_at_last_sync
                plans.append(_AccountPlan(
                    account=account, bucket_choice=bucket_choice, kind="holdings",
                    synced_from=valuation.synced_from, last_sync=valuation.last_sync,
                    cash_offset=cash_offset, holdings=valuation.values,
                ))
                continue
        if account.type == "investment":
            plans.append(_AccountPlan(account=account, bucket_choice=bucket_choice, kind="no_holdings"))
        else:
            plans.append(_AccountPlan(account=account, bucket_choice=bucket_choice, kind="plain"))
    return plans


def _plan_native_value(
    plan: _AccountPlan, as_of: date, series_by_account: dict[uuid.UUID, _PrefixSeries], today: date,
) -> float:
    """One account's value at one date, native currency: H(d) + C inside a
    holdings account's synced period, else the back-computed balance."""
    if plan.kind == "holdings" and plan.synced_from is not None and as_of >= plan.synced_from:
        return plan.holdings.get(as_of, 0.0) + plan.cash_offset
    series = series_by_account.get(plan.account.id)
    if series is None:
        return 0.0
    return _balance_at_native(plan.account, series, as_of, today)


def _collapse_shared_groups(
    accounts: list[Account], native_at_date: dict[uuid.UUID, float], shared_today: dict[uuid.UUID, float],
) -> dict[uuid.UUID, float]:
    """Mirror `dashboard_service._total_balance_by_currency`'s shared-balance
    dedupe: every account in a `shared_balance_group` carries the same debt,
    so only the group's first account counts; the rest contribute 0, toward
    both the total and its bucket. The dedupe magnitude is always *today's*
    shared balance, matching the dashboard's own convention exactly (even
    when valuing a past date) so a historical point never drifts from what
    the dashboard would show for that same date.
    """
    grouped: dict[str, list[Account]] = {}
    for account in accounts:
        key = account.shared_balance_group or str(account.id)
        grouped.setdefault(key, []).append(account)

    collapsed: dict[uuid.UUID, float] = {}
    for group in grouped.values():
        if len(group) > 1 and group[0].shared_balance_group:
            keeper = group[0]
            total = sum(native_at_date.get(a.id, 0.0) for a in group)
            total -= shared_today.get(keeper.id, 0.0) * (len(group) - 1)
            collapsed[keeper.id] = total
            for other in group[1:]:
                collapsed[other.id] = 0.0
        else:
            for a in group:
                collapsed[a.id] = native_at_date.get(a.id, 0.0)
    return collapsed


def _empty_buckets() -> dict[Bucket, float]:
    return {b: 0.0 for b in BUCKETS}


def _point_flags(plans: list[_AccountPlan], as_of: date) -> list[PointFlag]:
    flags: set[PointFlag] = set()
    for plan in plans:
        if plan.kind == "no_holdings":
            flags.add("no_holdings")
        elif plan.kind == "holdings":
            if plan.synced_from is not None and as_of >= plan.synced_from:
                flags.add("cash_flat")
            else:
                flags.add("estimated")
    order: dict[PointFlag, int] = {"estimated": 0, "cash_flat": 1, "no_holdings": 2}
    return sorted(flags, key=lambda f: order[f])


# ---------------------------------------------------------------------------
# Manual / unlinked assets
# ---------------------------------------------------------------------------


async def _not_backed_assets(session: AsyncSession, workspace_id: uuid.UUID, open_account_ids: list[uuid.UUID]) -> list[Asset]:
    """Active assets whose value is not already inside one of these (open)
    account balances: manual assets, holdings with no account hint, and
    holdings linked to a closed/unknown account."""
    assets = await _load_active_assets(session, workspace_id, by_workspace=True, group_ids=None)
    counted = {str(aid) for aid in open_account_ids}
    return [a for a in assets if not is_account_backed(a, counted)]


async def _asset_value_primary(session: AsyncSession, asset: Asset, values_map: dict[str, list[tuple[date, float]]], as_of: date, primary_currency: str) -> float:
    amount = _fill_forward_at(asset, values_map.get(str(asset.id), []), as_of)
    if not amount:
        return 0.0
    converted, _ = await convert(session, Decimal(str(amount)), asset.currency, primary_currency, as_of)
    return float(converted)


# ---------------------------------------------------------------------------
# A loaded valuation context — bulk-loaded once, evaluated at any number of
# dates. `net_worth_history` and `net_worth_breakdowns_at` both build one of
# these exactly once per call, regardless of how many dates they value.
# ---------------------------------------------------------------------------


@dataclass
class _ValuationContext:
    today: date
    accounts: list[Account]
    plans: list[_AccountPlan]
    series_by_account: dict[uuid.UUID, _PrefixSeries]
    shared_today: dict[uuid.UUID, float]
    not_backed: list[Asset]
    values_map: dict[str, list[tuple[date, float]]]


async def _load_context(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    dates: list[date],
    overrides: dict[str, AccountAnnotation],
) -> _ValuationContext:
    """Everything needed to value any of `dates`: open accounts, their bucket
    classification and (for holdings accounts) H(d) over `dates`, a
    back-computed balance series per account, today's shared-balance-group
    magnitudes, and the not-account-backed assets' value series. Exactly one
    `AssetValue` query for every linked holding, one transaction-delta query
    and one pending-delta query across every account — never one per date.
    """
    today = app_today()
    accounts = await _get_open_accounts(session, workspace_id)
    account_ids = [a.id for a in accounts]
    plans = await _build_account_plans(session, workspace_id, accounts, overrides, dates, today)
    series_by_account = await _native_balances_by_account(session, workspace_id, accounts, today)
    shared_today = {
        plan.account.id: (
            await _account_balance_at(session, plan.account, today) if plan.account.shared_balance_group else 0.0
        )
        for plan in plans
    }
    not_backed = await _not_backed_assets(session, workspace_id, account_ids)
    values_map = await _load_asset_native_values(session, not_backed) if not_backed else {}
    return _ValuationContext(
        today=today, accounts=accounts, plans=plans, series_by_account=series_by_account,
        shared_today=shared_today, not_backed=not_backed, values_map=values_map,
    )


# ---------------------------------------------------------------------------
# One point, evaluated from an already-loaded context
# ---------------------------------------------------------------------------


async def _point_at(
    session: AsyncSession,
    plans: list[_AccountPlan],
    series_by_account: dict[uuid.UUID, _PrefixSeries],
    shared_today: dict[uuid.UUID, float],
    accounts: list[Account],
    not_backed: list[Asset],
    values_map: dict[str, list[tuple[date, float]]],
    as_of: date,
    today: date,
    primary_currency: str,
) -> tuple[NetWorthPoint, dict[uuid.UUID, float], dict[uuid.UUID, float]]:
    """One `NetWorthPoint` at `as_of`, plus the per-account and per-asset
    primary-currency values behind it (shared-group collapsed for accounts).
    """
    native_at_date = {plan.account.id: _plan_native_value(plan, as_of, series_by_account, today) for plan in plans}
    collapsed = _collapse_shared_groups(accounts, native_at_date, shared_today)

    buckets = _empty_buckets()
    account_values: dict[uuid.UUID, float] = {}
    for plan in plans:
        value_native = collapsed.get(plan.account.id, 0.0)
        value_primary = 0.0
        if value_native:
            converted, _ = await convert(session, Decimal(str(value_native)), plan.account.currency, primary_currency, as_of)
            value_primary = float(converted)
        account_values[plan.account.id] = value_primary
        buckets[plan.bucket_choice.bucket] += value_primary

    asset_values: dict[uuid.UUID, float] = {}
    for asset in not_backed:
        value_primary = await _asset_value_primary(session, asset, values_map, as_of, primary_currency)
        asset_values[asset.id] = value_primary
        if value_primary:
            buckets[asset_bucket(asset.type)] += value_primary

    total = sum(buckets.values())
    liquid = sum(buckets[b] for b in LIQUID_BUCKETS)
    point = NetWorthPoint(
        date=as_of,
        total=round(total, 2),
        liquid=round(liquid, 2),
        buckets={b: round(v, 2) for b, v in buckets.items()},
        flags=_point_flags(plans, as_of),
    )
    return point, account_values, asset_values


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def latest_net_worth(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    overrides: dict[str, AccountAnnotation] = _EMPTY_OVERRIDES,
) -> NetWorthPoint:
    """Today's net worth, built from exactly the dashboard's own functions so
    it matches `dashboard_service.get_summary` to the cent (plan P2, critique
    C3): `_total_balance_by_currency` plus the counted partition of
    `split_asset_values_at`. Buckets are derived per account/asset from the
    same balances, so they sum to that total in every realistic
    single-currency workspace; `overrides` only changes which bucket a
    balance lands in, never the total itself.
    """
    today = app_today()
    user = await session.get(User, user_id)
    primary_currency = user.primary_currency if user else get_settings().default_currency

    accounts = await _get_open_accounts(session, workspace_id)
    account_ids = [a.id for a in accounts]

    total_balance = await _total_balance_by_currency(session, workspace_id, today)
    (assets_value, _assets_primary), (_backed_value, _backed_primary) = await split_asset_values_at(
        session, workspace_id, as_of_date=today, primary_currency=primary_currency,
        by_workspace=True, counted_account_ids=account_ids,
    )
    combined: dict[str, float] = dict(total_balance)
    for currency, amount in assets_value.items():
        combined[currency] = combined.get(currency, 0.0) + amount
    net_worth = 0.0
    for currency, amount in combined.items():
        converted, _ = await convert(session, Decimal(str(amount)), currency, primary_currency, today)
        net_worth += float(converted)

    plans = await _build_account_plans(session, workspace_id, accounts, overrides, [today], today)

    native_today: dict[uuid.UUID, float] = {
        plan.account.id: await _account_balance_at(session, plan.account, today) for plan in plans
    }
    shared_today = {plan.account.id: native_today[plan.account.id] for plan in plans}
    collapsed = _collapse_shared_groups(accounts, native_today, shared_today)

    buckets = _empty_buckets()
    for plan in plans:
        value_primary = 0.0
        value_native = collapsed.get(plan.account.id, 0.0)
        if value_native:
            converted, _ = await convert(session, Decimal(str(value_native)), plan.account.currency, primary_currency, today)
            value_primary = float(converted)
        buckets[plan.bucket_choice.bucket] += value_primary

    not_backed = await _not_backed_assets(session, workspace_id, account_ids)
    values_map = await _load_asset_native_values(session, not_backed, up_to_date=today) if not_backed else {}
    for asset in not_backed:
        value_primary = await _asset_value_primary(session, asset, values_map, today, primary_currency)
        if value_primary:
            buckets[asset_bucket(asset.type)] += value_primary

    liquid = sum(buckets[b] for b in LIQUID_BUCKETS)
    flags = _point_flags(plans, today)

    return NetWorthPoint(
        date=today,
        total=round(net_worth, 2),
        liquid=round(liquid, 2),
        buckets={b: round(v, 2) for b, v in buckets.items()},
        flags=flags,
    )


async def net_worth_history(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    months: int,
    overrides: dict[str, AccountAnnotation] = _EMPTY_OVERRIDES,
) -> NetWorthHistory:
    """Net worth over time, by bucket (plan P2/P3).

    Points are calendar month-ends for the last `months` completed months,
    plus today. The last point is `latest_net_worth`, verbatim, so it equals
    the dashboard to the cent; every earlier point values each investment
    account with linked holdings from H(d) + a flat cash offset once it has
    started syncing (`cash_flat`), falling back to the existing
    back-computed balance before that (`estimated`) or always, for an
    investment account with no linked holdings at all (`no_holdings`).
    """
    today = app_today()
    user = await session.get(User, user_id)
    primary_currency = user.primary_currency if user else get_settings().default_currency

    month_ends = _month_end_points(today, months)
    ctx = await _load_context(session, workspace_id, month_ends, overrides)

    points: list[NetWorthPoint] = []
    for as_of in month_ends:
        point, _account_values, _asset_values = await _point_at(
            session, ctx.plans, ctx.series_by_account, ctx.shared_today, ctx.accounts,
            ctx.not_backed, ctx.values_map, as_of, ctx.today, primary_currency,
        )
        points.append(point)

    points.append(await latest_net_worth(session, workspace_id, user_id, overrides))

    holdings_starts = [p.synced_from for p in ctx.plans if p.kind == "holdings" and p.synced_from is not None]
    synced_from = min(holdings_starts) if holdings_starts else None

    return NetWorthHistory(currency=primary_currency, synced_from=synced_from, points=points)


async def net_worth_breakdowns_at(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    dates: list[date],
    primary_currency: str,
    overrides: dict[str, AccountAnnotation] = _EMPTY_OVERRIDES,
) -> list[tuple[NetWorthPoint, list[tuple[Account, float]], list[tuple[Asset, float]]]]:
    """Net worth at every one of `dates`, by bucket, plus the per-account and
    per-asset primary-currency values behind each point — one `_load_context`
    bulk load for the whole batch, then one `_point_at` pass per date.

    This is what `report_service.get_net_worth_report`'s unfiltered path
    calls once for its entire trend plus its baseline, instead of calling
    `_net_worth_at` (and so `net_worth_breakdown_at`) once per point: on a
    daily or weekly interval that used to mean dozens to hundreds of full
    reloads (every linked holding's `AssetValue` history, every account's
    transaction deltas, every unlinked asset) per request.
    """
    ctx = await _load_context(session, workspace_id, dates, overrides)
    accounts_by_id = {plan.account.id: plan.account for plan in ctx.plans}
    assets_by_id = {a.id: a for a in ctx.not_backed}

    results: list[tuple[NetWorthPoint, list[tuple[Account, float]], list[tuple[Asset, float]]]] = []
    for as_of in dates:
        point, account_values, asset_values = await _point_at(
            session, ctx.plans, ctx.series_by_account, ctx.shared_today, ctx.accounts,
            ctx.not_backed, ctx.values_map, as_of, ctx.today, primary_currency,
        )
        account_rows = [(accounts_by_id[aid], val) for aid, val in account_values.items()]
        asset_rows = [(assets_by_id[aid], val) for aid, val in asset_values.items()]
        results.append((point, account_rows, asset_rows))
    return results


async def net_worth_breakdown_at(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    as_of: date,
    primary_currency: str,
    overrides: dict[str, AccountAnnotation] = _EMPTY_OVERRIDES,
) -> tuple[NetWorthPoint, list[tuple[Account, float]], list[tuple[Asset, float]]]:
    """Net worth at one arbitrary date — `net_worth_breakdowns_at` for a
    single date, for a caller (like a single `report_service._net_worth_at`
    call) that only ever needs one point.

    It does not need a `user_id` because its caller already knows the
    workspace's `primary_currency`. At `as_of == today` the per-account math
    here is algebraically identical to `latest_net_worth` (H(s) + C - H(s) ==
    the account's current balance), so Reports and the dashboard agree on
    today's figure too, in addition to the historical fix.
    """
    [result] = await net_worth_breakdowns_at(session, workspace_id, [as_of], primary_currency, overrides)
    return result
