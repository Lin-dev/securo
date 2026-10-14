"""Net worth over time, valued from holdings instead of the back-computed
ledger, by bucket (fork, plan P2/P3).

Helpers mirror `tests/test_account_backed_holdings.py`: a connected account's
`balance` is authoritative once it has a `connection_id`, so tests that need a
specific balance (without a pile of transactions) give every account a fake
connection id.
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.asset import Asset
from app.models.asset_value import AssetValue
from app.models.bank_connection import BankConnection
from app.models.goal import Goal
from app.models.transaction import Transaction
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember
from app.schemas.wealth import BUCKETS, AccountAnnotation
from app.services import dashboard_service, report_service
from app.services.asset_service import ACCOUNT_LINK_METADATA_KEY
from app.services.goal_service import _resolve_current_amount
from app.services.wealth.valuation import (
    _balance_at_native,
    _month_end_points,
    _native_balances_by_account,
    holdings_value_by_account,
    latest_net_worth,
    net_worth_breakdown_at,
    net_worth_breakdowns_at,
    net_worth_history,
)
from tests.conftest import engine as _sqlite_engine

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _connection(session: AsyncSession, user_id, workspace_id) -> BankConnection:
    conn = BankConnection(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, provider="test",
        external_id=f"ext-{uuid.uuid4().hex[:8]}", institution_name="Bank",
        credentials={"token": "fake"}, status="active", settings=None,
    )
    session.add(conn)
    await session.flush()
    return conn


async def _account(
    session: AsyncSession, user_id, workspace_id, *, name="Acct", acc_type="checking",
    balance="0", currency="BRL", connection_id=None, external_id=None, is_closed=False,
    shared_balance_group=None,
) -> Account:
    acc = Account(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, name=name, type=acc_type,
        balance=Decimal(balance), currency=currency, connection_id=connection_id,
        external_id=external_id, is_closed=is_closed, shared_balance_group=shared_balance_group,
    )
    session.add(acc)
    await session.flush()
    return acc


async def _asset(
    session: AsyncSession, user_id, workspace_id, *, connection_id, account_id=None,
    name="VTI", currency="BRL", purchase_date, purchase_price="0", external_id=None,
) -> Asset:
    metadata: dict = {"symbol": name}
    if account_id is not None:
        metadata[ACCOUNT_LINK_METADATA_KEY] = str(account_id)
    asset = Asset(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, name=name, type="investment",
        currency=currency, source="simplefin", connection_id=connection_id,
        external_id=external_id or f"h-{uuid.uuid4().hex[:8]}", external_metadata=metadata,
        purchase_price=Decimal(purchase_price), purchase_date=purchase_date, valuation_method="manual",
    )
    session.add(asset)
    await session.flush()
    return asset


async def _value(session: AsyncSession, asset: Asset, *, d, amount, source="sync") -> AssetValue:
    val = AssetValue(
        id=uuid.uuid4(), asset_id=asset.id, workspace_id=asset.workspace_id,
        amount=Decimal(str(amount)), date=d, source=source,
    )
    session.add(val)
    await session.flush()
    return val


def _txn(user_id, workspace_id, account, amount, typ, **kw):
    when = kw.pop("when", date.today())
    return Transaction(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, account_id=account.id,
        description=kw.pop("description", f"{typ} {amount}"), amount=Decimal(str(amount)),
        date=when, type=typ, source="manual", currency=kw.pop("currency", "BRL"), status="posted",
        **kw,
    )


def _in_memory_asset(workspace_id, *, purchase_date, purchase_price="0", currency="BRL", name="VTI") -> Asset:
    """An unsaved Asset — enough for `holdings_value_by_account`, which never
    queries assets/values from the database itself."""
    return Asset(
        id=uuid.uuid4(), user_id=uuid.uuid4(), workspace_id=workspace_id, name=name,
        type="investment", currency=currency, purchase_date=purchase_date,
        purchase_price=Decimal(str(purchase_price)), valuation_method="manual",
    )


def _row(asset: Asset, *, d, amount, source="sync") -> AssetValue:
    return AssetValue(
        id=uuid.uuid4(), asset_id=asset.id, workspace_id=asset.workspace_id,
        amount=Decimal(str(amount)), date=d, source=source,
    )


def _in_memory_account(workspace_id, *, currency="BRL") -> Account:
    """An unsaved, connected Account — `holdings_value_by_account` only reads
    `.currency` and `.connection_id` (truthy), never queries the database."""
    return Account(
        id=uuid.uuid4(), user_id=uuid.uuid4(), workspace_id=workspace_id, name="Brokerage",
        type="investment", currency=currency, balance=Decimal("0"), connection_id=uuid.uuid4(),
    )


# ---------------------------------------------------------------------------
# Latest point == dashboard == Goal (critique C3, R2-m1)
# ---------------------------------------------------------------------------


async def test_latest_point_matches_dashboard_summary_and_goal(session, test_user, test_workspace):
    uid, wid = test_user.id, test_workspace.id
    conn = await _connection(session, uid, wid)

    await _account(session, uid, wid, name="Checking", acc_type="checking", balance="1000", connection_id=conn.id, external_id="chk-1")
    await _account(session, uid, wid, name="Card A", acc_type="credit_card", balance="200", connection_id=conn.id, external_id="card-a", shared_balance_group="shared-1")
    await _account(session, uid, wid, name="Card B", acc_type="credit_card", balance="200", connection_id=conn.id, external_id="card-b", shared_balance_group="shared-1")

    brokerage = await _account(session, uid, wid, name="Brokerage", acc_type="investment", balance="5000", connection_id=conn.id, external_id="inv-1")
    holding = await _asset(session, uid, wid, connection_id=conn.id, account_id=brokerage.id, purchase_date=date.today() - timedelta(days=60), purchase_price="4500")
    await _value(session, holding, d=date.today(), amount="4800")

    session.add(Asset(
        id=uuid.uuid4(), user_id=uid, workspace_id=wid, name="Gold", type="valuable", currency="BRL",
        purchase_price=Decimal("300"), purchase_date=date.today() - timedelta(days=10),
    ))
    goal = Goal(
        id=uuid.uuid4(), user_id=uid, workspace_id=wid, name="NW", target_amount=Decimal("100000"),
        current_amount=Decimal("0"), initial_amount=Decimal("0"), currency="BRL",
        tracking_type="net_worth", status="active",
    )
    session.add(goal)
    await session.commit()
    await session.refresh(goal)

    point = await latest_net_worth(session, wid, uid)
    summary = await dashboard_service.get_summary(session, wid, uid)

    assert point.total == pytest.approx(summary.total_balance_primary, abs=0.01)

    current = await _resolve_current_amount(session, goal, uid)
    assert float(current) == pytest.approx(point.total, abs=0.01)

    assert sum(point.buckets.values()) == pytest.approx(point.total, abs=0.01)


# ---------------------------------------------------------------------------
# Holdings carry-forward (P2) — tested directly against `holdings_value_by_account`
# ---------------------------------------------------------------------------


async def test_holding_value_rises_with_synced_price_while_back_computation_is_flat(session, test_workspace):
    wid = test_workspace.id
    account = _in_memory_account(wid)
    d0, d1, d2 = date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)
    asset = _in_memory_asset(wid, purchase_date=d0, purchase_price="1000")
    rows = {asset.id: [
        _row(asset, d=d0, amount="1000"),
        _row(asset, d=d1, amount="1050"),
        _row(asset, d=d2, amount="1100"),
    ]}

    valuation = await holdings_value_by_account(session, account, [asset], rows, [d0, d1, d2])
    assert [valuation.values[d] for d in (d0, d1, d2)] == [1000.0, 1050.0, 1100.0]

    # The back-computed ledger sees no transactions for this account at all,
    # so it would show the exact same (current) balance on every date — flat.
    account.balance = Decimal("1200")
    series = await _native_balances_by_account(session, wid, [account], d2)
    flat = [_balance_at_native(account, series[account.id], d, d2) for d in (d0, d1, d2)]
    assert flat == [1200.0, 1200.0, 1200.0]


async def test_holding_drops_only_after_a_later_account_sync_excludes_it(session, test_workspace):
    wid = test_workspace.id
    account = _in_memory_account(wid)
    d0, d1, d2 = date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)
    sold = _in_memory_asset(wid, name="Sold Co", purchase_date=d0, purchase_price="500")
    kept = _in_memory_asset(wid, name="Kept Co", purchase_date=d0, purchase_price="100")
    rows = {
        sold.id: [_row(sold, d=d0, amount="500"), _row(sold, d=d1, amount="500")],  # no row at d2: sold between d1 and d2
        kept.id: [_row(kept, d=d0, amount="100"), _row(kept, d=d1, amount="100"), _row(kept, d=d2, amount="100")],
    }

    valuation = await holdings_value_by_account(session, account, [sold, kept], rows, [d0, d1, d2])
    assert valuation.values[d0] == pytest.approx(600.0)
    assert valuation.values[d1] == pytest.approx(600.0)
    assert valuation.values[d2] == pytest.approx(100.0)  # the sold holding drops only once a later sync leaves it out


async def test_carries_forward_before_todays_sync_and_on_a_failed_sync_day(session, test_workspace):
    wid = test_workspace.id
    account = _in_memory_account(wid)
    d0, d1, d2, d3 = date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)
    asset = _in_memory_asset(wid, purchase_date=d0, purchase_price="1000")
    # d2 has no row at all for this asset or any other in the account: either
    # the day's sync hasn't run yet (queried as "today" before the cron) or
    # it ran and failed. Either way nothing drops to 0.
    rows = {asset.id: [_row(asset, d=d0, amount="1000"), _row(asset, d=d1, amount="1100"), _row(asset, d=d3, amount="1200")]}

    valuation = await holdings_value_by_account(session, account, [asset], rows, [d0, d1, d2, d3])
    assert valuation.values[d2] == pytest.approx(valuation.values[d1]) == pytest.approx(1100.0)
    assert valuation.values[d3] == pytest.approx(1200.0)


async def test_seed_row_is_excluded_when_followed_by_a_long_gap(session, test_workspace):
    wid = test_workspace.id
    account = _in_memory_account(wid)
    purchase = date(2020, 1, 1)
    first_real_sync = date(2026, 9, 13)
    asset = _in_memory_asset(wid, purchase_date=purchase, purchase_price="500")
    rows = {asset.id: [_row(asset, d=purchase, amount="500"), _row(asset, d=first_real_sync, amount="900")]}

    valuation = await holdings_value_by_account(session, account, [asset], rows, [purchase, first_real_sync])
    assert valuation.synced_from == first_real_sync
    assert valuation.values[purchase] == 0.0
    assert valuation.values[first_real_sync] == pytest.approx(900.0)


async def test_seed_row_is_kept_when_the_next_row_is_close_enough(session, test_workspace):
    wid = test_workspace.id
    account = _in_memory_account(wid)
    purchase = date(2026, 9, 1)
    next_row = date(2026, 9, 5)  # a 4-day gap: close enough to be a real early sync
    asset = _in_memory_asset(wid, purchase_date=purchase, purchase_price="500")
    rows = {asset.id: [_row(asset, d=purchase, amount="500"), _row(asset, d=next_row, amount="520")]}

    valuation = await holdings_value_by_account(session, account, [asset], rows, [purchase, next_row])
    assert valuation.synced_from == purchase
    assert valuation.values[purchase] == pytest.approx(500.0)


async def test_holding_id_churn_does_not_double_count(session, test_workspace):
    wid = test_workspace.id
    account = _in_memory_account(wid)
    d0, d1 = date(2026, 1, 5), date(2026, 1, 6)
    old = _in_memory_asset(wid, name="VTI", purchase_date=d0, purchase_price="1000")
    new = _in_memory_asset(wid, name="VTI", purchase_date=d1, purchase_price="1000")
    rows = {
        old.id: [_row(old, d=d0, amount="1000")],  # archived the day the replacement id appears
        new.id: [_row(new, d=d1, amount="1000")],
    }

    valuation = await holdings_value_by_account(session, account, [old, new], rows, [d0, d1])
    assert valuation.values[d0] == pytest.approx(1000.0)
    assert valuation.values[d1] == pytest.approx(1000.0)


# ---------------------------------------------------------------------------
# Trades don't move the synced-period value
# ---------------------------------------------------------------------------


async def test_trade_inside_the_synced_period_leaves_value_unchanged(session, test_user, test_workspace):
    uid, wid = test_user.id, test_workspace.id
    conn = await _connection(session, uid, wid)
    account = await _account(session, uid, wid, name="Brokerage", acc_type="investment", balance="1200", connection_id=conn.id, external_id="inv-1")
    d0 = date.today() - timedelta(days=5)
    asset = await _asset(session, uid, wid, connection_id=conn.id, account_id=account.id, purchase_date=d0 - timedelta(days=1), purchase_price="1000")
    await _value(session, asset, d=d0, amount="1000")
    await _value(session, asset, d=date.today(), amount="1200")
    await session.commit()

    before, _, _ = await net_worth_breakdown_at(session, wid, d0, "BRL")

    session.add(_txn(uid, wid, account, 50, "debit", when=d0))
    await session.commit()

    after, _, _ = await net_worth_breakdown_at(session, wid, d0, "BRL")
    assert after.total == pytest.approx(before.total, abs=0.01)


# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------


async def test_flags_estimated_cash_flat_and_no_holdings(session, test_user, test_workspace):
    uid, wid = test_user.id, test_workspace.id
    conn = await _connection(session, uid, wid)

    synced_acc = await _account(session, uid, wid, name="Synced", acc_type="investment", balance="1000", connection_id=conn.id, external_id="inv-1")
    first_sync = date.today() - timedelta(days=5)
    asset = await _asset(session, uid, wid, connection_id=conn.id, account_id=synced_acc.id, purchase_date=first_sync, purchase_price="900")
    await _value(session, asset, d=first_sync, amount="900")
    await _value(session, asset, d=date.today(), amount="1000")

    await _account(session, uid, wid, name="NoHoldings", acc_type="investment", balance="300", connection_id=conn.id, external_id="inv-2")
    await session.commit()

    before_point, _, _ = await net_worth_breakdown_at(session, wid, first_sync - timedelta(days=10), "BRL")
    assert "estimated" in before_point.flags
    assert "no_holdings" in before_point.flags

    inside_point, _, _ = await net_worth_breakdown_at(session, wid, first_sync + timedelta(days=1), "BRL")
    assert "cash_flat" in inside_point.flags
    assert "no_holdings" in inside_point.flags


# ---------------------------------------------------------------------------
# Buckets
# ---------------------------------------------------------------------------


async def test_buckets_are_complete_and_an_override_moves_an_account(session, test_user, test_workspace):
    uid, wid = test_user.id, test_workspace.id
    conn = await _connection(session, uid, wid)
    await _account(session, uid, wid, name="Checking", acc_type="checking", balance="1000", connection_id=conn.id, external_id="chk-1")
    await _account(session, uid, wid, name="Card", acc_type="credit_card", balance="200", connection_id=conn.id, external_id="card-1")
    brokerage = await _account(session, uid, wid, name="Robinhood", acc_type="investment", balance="500", connection_id=conn.id, external_id="inv-1")
    await session.commit()

    point = await latest_net_worth(session, wid, uid)
    assert set(point.buckets.keys()) == set(BUCKETS)
    assert sum(point.buckets.values()) == pytest.approx(point.total, abs=0.01)

    expected_liquid = point.buckets["cash"] + point.buckets["taxable"] + point.buckets["crypto"] + point.buckets["card_debt"]
    assert point.liquid == pytest.approx(expected_liquid, abs=0.01)
    assert point.buckets["taxable"] == pytest.approx(500.0, abs=0.01)

    overrides = {str(brokerage.id): AccountAnnotation(bucket="crypto")}
    moved = await latest_net_worth(session, wid, uid, overrides)
    assert moved.buckets["crypto"] == pytest.approx(500.0, abs=0.01)
    assert moved.buckets["taxable"] == pytest.approx(0.0, abs=0.01)
    assert moved.total == pytest.approx(point.total, abs=0.01)  # the override only moves the bucket, not the total


# ---------------------------------------------------------------------------
# Closed accounts
# ---------------------------------------------------------------------------


async def test_closed_account_is_not_counted(session, test_user, test_workspace):
    uid, wid = test_user.id, test_workspace.id
    conn = await _connection(session, uid, wid)
    await _account(session, uid, wid, name="Open", acc_type="checking", balance="1000", connection_id=conn.id, external_id="a1")
    await _account(session, uid, wid, name="Closed", acc_type="checking", balance="5000", connection_id=conn.id, external_id="a2", is_closed=True)
    await session.commit()

    # Mirrors `dashboard_service`/`report_service`: a closed account drops out
    # of net worth entirely, not just from today's point.
    point = await latest_net_worth(session, wid, uid)
    assert point.total == pytest.approx(1000.0, abs=0.01)

    history_point, _, _ = await net_worth_breakdown_at(session, wid, date.today() - timedelta(days=10), "BRL")
    assert history_point.total == pytest.approx(1000.0, abs=0.01)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


async def test_net_worth_report_latest_value_matches_the_valuation(session, test_user, test_workspace):
    uid, wid = test_user.id, test_workspace.id
    conn = await _connection(session, uid, wid)
    await _account(session, uid, wid, name="Checking", acc_type="checking", balance="1000", connection_id=conn.id, external_id="a1")
    brokerage = await _account(session, uid, wid, name="Brokerage", acc_type="investment", balance="2000", connection_id=conn.id, external_id="inv-1")
    asset = await _asset(session, uid, wid, connection_id=conn.id, account_id=brokerage.id, purchase_date=date.today() - timedelta(days=30), purchase_price="1800")
    await _value(session, asset, d=date.today(), amount="2000")
    await session.commit()

    point = await latest_net_worth(session, wid, uid)
    report = await report_service.get_net_worth_report(session, wid, uid, months=1)

    assert report.summary.primary_value == pytest.approx(point.total, abs=0.01)


# ---------------------------------------------------------------------------
# Bulk loading: the Reports page issues a bounded number of SQL statements,
# regardless of how many points its date range asks for.
# ---------------------------------------------------------------------------


class _StatementCounter:
    """Counts statements the test engine issues while this is active."""

    def __init__(self):
        self.count = 0

    def _before_cursor_execute(self, conn, cursor, statement, parameters, context, executemany):
        self.count += 1

    def __enter__(self):
        event.listen(_sqlite_engine.sync_engine, "before_cursor_execute", self._before_cursor_execute)
        return self

    def __exit__(self, *exc):
        event.remove(_sqlite_engine.sync_engine, "before_cursor_execute", self._before_cursor_execute)


async def test_net_worth_report_batches_its_bulk_loads_regardless_of_point_count(
    session, test_user, test_workspace,
):
    uid, wid = test_user.id, test_workspace.id
    conn = await _connection(session, uid, wid)
    await _account(session, uid, wid, name="Checking", acc_type="checking", balance="1000", connection_id=conn.id, external_id="chk-1")
    brokerage = await _account(session, uid, wid, name="Brokerage", acc_type="investment", balance="2000", connection_id=conn.id, external_id="inv-1")
    asset = await _asset(session, uid, wid, connection_id=conn.id, account_id=brokerage.id, purchase_date=date.today() - timedelta(days=60), purchase_price="1800")
    await _value(session, asset, d=date.today() - timedelta(days=60), amount="1800")
    await _value(session, asset, d=date.today() - timedelta(days=55), amount="1850")
    await _value(session, asset, d=date.today(), amount="2000")
    await session.commit()

    with _StatementCounter() as daily_counter:
        daily_report = await report_service.get_net_worth_report(
            session, wid, uid, interval="daily",
            start_date=date.today() - timedelta(days=29), end_date=date.today(),
        )
    with _StatementCounter() as monthly_counter:
        monthly_report = await report_service.get_net_worth_report(session, wid, uid, months=3, interval="monthly")

    assert len(daily_report.trend) == 30
    assert len(monthly_report.trend) < len(daily_report.trend)

    # The statement count does not grow with the number of points: a 30-point
    # daily report and a short monthly report both stay under the same small,
    # fixed bound (one bulk load per request, not one per point). A per-point
    # reload would issue dozens of statements for the daily report alone.
    bound = 10
    assert daily_counter.count <= bound, daily_counter.count
    assert monthly_counter.count <= bound, monthly_counter.count

    # Both reports' per-point values agree with calling the single-date
    # valuation function directly for the same dates.
    for point in daily_report.trend:
        as_of = date.fromisoformat(point.date)
        expected, _, _ = await net_worth_breakdown_at(session, wid, as_of, "BRL")
        assert point.value == pytest.approx(expected.total, abs=0.01)


async def test_net_worth_breakdowns_at_matches_per_date_calls(session, test_user, test_workspace):
    uid, wid = test_user.id, test_workspace.id
    conn = await _connection(session, uid, wid)
    await _account(session, uid, wid, name="Checking", acc_type="checking", balance="1000", connection_id=conn.id, external_id="chk-1")
    brokerage = await _account(session, uid, wid, name="Brokerage", acc_type="investment", balance="2000", connection_id=conn.id, external_id="inv-1")
    asset = await _asset(session, uid, wid, connection_id=conn.id, account_id=brokerage.id, purchase_date=date.today() - timedelta(days=20), purchase_price="1800")
    await _value(session, asset, d=date.today() - timedelta(days=20), amount="1800")
    await _value(session, asset, d=date.today() - timedelta(days=15), amount="1900")
    await _value(session, asset, d=date.today(), amount="2000")
    await session.commit()

    dates = [date.today() - timedelta(days=d) for d in (18, 10, 1, 0)]
    batched = await net_worth_breakdowns_at(session, wid, dates, "BRL")

    for as_of, (point, account_rows, asset_rows) in zip(dates, batched):
        single_point, single_accounts, single_assets = await net_worth_breakdown_at(session, wid, as_of, "BRL")
        assert point == single_point
        assert {a.id: v for a, v in account_rows} == {a.id: v for a, v in single_accounts}
        assert {a.id: v for a, v in asset_rows} == {a.id: v for a, v in single_assets}


# ---------------------------------------------------------------------------
# History assembly
# ---------------------------------------------------------------------------


async def test_net_worth_history_returns_calendar_month_ends_plus_today(session, test_user, test_workspace):
    uid, wid = test_user.id, test_workspace.id
    conn = await _connection(session, uid, wid)
    await _account(session, uid, wid, name="Checking", acc_type="checking", balance="1000", connection_id=conn.id, external_id="a1")
    await session.commit()

    history = await net_worth_history(session, wid, uid, months=3)
    today = date.today()

    assert len(history.points) == 4
    assert history.points[-1].date == today
    assert [p.date for p in history.points[:-1]] == _month_end_points(today, 3)

    latest = await latest_net_worth(session, wid, uid)
    assert history.points[-1] == latest


async def test_history_synced_from_is_the_earliest_first_sync_across_accounts(session, test_user, test_workspace):
    uid, wid = test_user.id, test_workspace.id
    conn = await _connection(session, uid, wid)
    acc1 = await _account(session, uid, wid, name="Acc1", acc_type="investment", balance="1000", connection_id=conn.id, external_id="inv-1")
    acc2 = await _account(session, uid, wid, name="Acc2", acc_type="investment", balance="2000", connection_id=conn.id, external_id="inv-2")

    early = date.today() - timedelta(days=200)
    late = date.today() - timedelta(days=30)
    a1 = await _asset(session, uid, wid, connection_id=conn.id, account_id=acc1.id, purchase_date=early, purchase_price="900")
    await _value(session, a1, d=early, amount="900")
    # A row soon after `early` keeps it from looking like a seed (a seed is
    # only excluded when followed by a gap of more than 7 days).
    await _value(session, a1, d=early + timedelta(days=1), amount="905")
    await _value(session, a1, d=date.today(), amount="1000")
    a2 = await _asset(session, uid, wid, connection_id=conn.id, account_id=acc2.id, purchase_date=late, purchase_price="1900")
    await _value(session, a2, d=late, amount="1900")
    await _value(session, a2, d=late + timedelta(days=1), amount="1905")
    await _value(session, a2, d=date.today(), amount="2000")
    await session.commit()

    history = await net_worth_history(session, wid, uid, months=12)
    assert history.synced_from == early


# ---------------------------------------------------------------------------
# PostgreSQL: date grouping and FX, against a real backend
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_session(postgres_sessions):
    async with postgres_sessions() as pg:
        yield pg
        await pg.rollback()


@pytest_asyncio.fixture
async def pg_user_workspace(pg_session: AsyncSession):
    import bcrypt

    hashed = bcrypt.hashpw(b"testpass123", bcrypt.gensalt()).decode()
    user = User(
        id=uuid.uuid4(), email=f"pgwealth-{uuid.uuid4().hex[:8]}@example.com", hashed_password=hashed,
        is_active=True, is_superuser=False, is_verified=True, preferences={"currency_display": "USD"},
    )
    pg_session.add(user)
    await pg_session.flush()
    workspace = Workspace(id=uuid.uuid4(), name="PG Wealth", kind="personal", created_by_user_id=user.id, default_currency="USD")
    pg_session.add(workspace)
    await pg_session.flush()
    pg_session.add(WorkspaceMember(id=uuid.uuid4(), workspace_id=workspace.id, user_id=user.id, role="owner"))
    await pg_session.commit()
    await pg_session.refresh(user)
    await pg_session.refresh(workspace)
    return user, workspace


async def test_net_worth_history_against_postgres(pg_session: AsyncSession, pg_user_workspace):
    user, workspace = pg_user_workspace
    conn = await _connection(pg_session, user.id, workspace.id)

    brokerage = await _account(
        pg_session, user.id, workspace.id, name="Brokerage", acc_type="investment", balance="1500",
        connection_id=conn.id, external_id="inv-1", currency="USD",
    )
    d0 = date.today() - timedelta(days=10)
    asset = await _asset(
        pg_session, user.id, workspace.id, connection_id=conn.id, account_id=brokerage.id,
        purchase_date=d0, purchase_price="1000", currency="USD",
    )
    await _value(pg_session, asset, d=d0, amount="1000")
    await _value(pg_session, asset, d=d0 + timedelta(days=1), amount="1010")
    await _value(pg_session, asset, d=date.today(), amount="1500")

    # A second, foreign-currency account exercises the FX leg of the
    # valuation; its currency differs from the workspace's primary (USD).
    await _account(
        pg_session, user.id, workspace.id, name="Checking", acc_type="checking", balance="800",
        connection_id=conn.id, external_id="chk-1", currency="EUR",
    )
    await pg_session.commit()

    history = await net_worth_history(pg_session, workspace.id, user.id, months=2)

    assert len(history.points) == 3
    assert history.synced_from == d0
    assert history.points[-1].total > 0
    # The earlier month-end (before the EUR account's balance existed as a
    # transaction and before the holding's first sync) still returns a
    # well-formed point with every bucket present.
    assert set(history.points[0].buckets.keys()) == set(BUCKETS)
