"""Account-backed holdings (issue #343).

A provider such as SimpleFIN reports a brokerage account's balance AND the
holdings inside it. Both used to be added to net worth. Sync now stamps the
owning Securo account id on each holding, and every net-worth total skips a
holding whose owning account is being summed into that same total. Portfolio
views, wallet goals and the default `get_asset_values_at` keep counting every
holding.
"""

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.asset import Asset
from app.models.asset_group import AssetGroup
from app.models.asset_value import AssetValue
from app.models.bank_connection import BankConnection
from app.models.goal import Goal
from app.providers.base import AccountData, HoldingData
from app.providers.simplefin import SimpleFinProvider
from app.services import dashboard_service
from app.services.asset_service import (
    ACCOUNT_LINK_METADATA_KEY,
    get_asset_values_at,
    is_account_backed,
    linked_account_id,
    split_asset_values_at,
)
from app.services.connection_service import sync_connection
from app.services.goal_service import _resolve_current_amount
from app.services.report_service import _net_worth_at


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _connection(session: AsyncSession, user_id, workspace_id) -> BankConnection:
    conn = BankConnection(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, provider="test",
        external_id=f"ext-{uuid.uuid4().hex[:8]}", institution_name="Brokerage",
        credentials={"token": "fake"}, status="active", settings=None,
        last_sync_at=datetime.now(timezone.utc), created_at=datetime.now(timezone.utc),
    )
    session.add(conn)
    await session.commit()
    await session.refresh(conn)
    return conn


async def _account(
    session: AsyncSession, user_id, workspace_id, *, connection_id=None, external_id=None,
    balance="0", name="Acct", acc_type="checking", currency="BRL", is_closed=False,
) -> Account:
    acc = Account(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id,
        connection_id=connection_id, external_id=external_id, name=name,
        type=acc_type, balance=Decimal(balance), currency=currency, is_closed=is_closed,
    )
    session.add(acc)
    await session.commit()
    await session.refresh(acc)
    return acc


async def _holding(
    session: AsyncSession, user_id, workspace_id, *, connection_id, account_id=None,
    value="900", name="VTI", currency="BRL", source="simplefin", group_id=None,
) -> Asset:
    metadata: dict = {"symbol": name}
    if account_id is not None:
        metadata[ACCOUNT_LINK_METADATA_KEY] = str(account_id)
    asset = Asset(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, name=name,
        type="investment", currency=currency, source=source, connection_id=connection_id,
        external_id=f"h-{uuid.uuid4().hex[:8]}", external_metadata=metadata,
        purchase_price=Decimal(value), purchase_date=date.today() - timedelta(days=30),
        valuation_method="manual", group_id=group_id,
    )
    session.add(asset)
    await session.flush()
    session.add(AssetValue(
        id=uuid.uuid4(), asset_id=asset.id, workspace_id=workspace_id,
        amount=Decimal(value), date=date.today(), source="sync",
    ))
    await session.commit()
    await session.refresh(asset)
    return asset


def _provider(accounts: list[AccountData], holdings: list[HoldingData]) -> AsyncMock:
    provider = AsyncMock()
    provider.refresh_credentials = AsyncMock(return_value={"token": "t"})
    provider.get_institution_logo = AsyncMock(return_value=None)
    provider.get_accounts = AsyncMock(return_value=accounts)
    provider.get_transactions = AsyncMock(return_value=[])
    provider.get_holdings = AsyncMock(return_value=holdings)
    return provider


async def _run_sync(session, conn, workspace_id, user_id, provider):
    with patch("app.services.connection_service.get_provider", return_value=provider), \
         patch("app.services.connection_service.detect_transfer_pairs", new_callable=AsyncMock), \
         patch("app.services.connection_service.stamp_primary_amount", new_callable=AsyncMock), \
         patch("app.services.connection_service.apply_rules_to_transaction", new_callable=AsyncMock):
        return await sync_connection(session, conn.id, workspace_id, user_id)


def _brokerage(external_id="inv-1") -> AccountData:
    return AccountData(
        external_id=external_id, name="Brokerage", type="investment",
        balance=Decimal("1000"), currency="BRL",
    )


def _vti(account_external_id="inv-1", metadata=None) -> HoldingData:
    return HoldingData(
        external_id="h-1", name="VTI", currency="BRL", current_value=Decimal("900"),
        account_external_id=account_external_id, account_name="Brokerage",
        metadata={"symbol": "VTI"} if metadata is None else metadata,
    )


async def _asset_by_external(session, external_id) -> Asset:
    return (await session.execute(select(Asset).where(Asset.external_id == external_id))).scalar_one()


async def _account_by_external(session, external_id) -> Account:
    return (await session.execute(select(Account).where(Account.external_id == external_id))).scalar_one()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_linked_account_id_reads_only_a_non_empty_string():
    assert linked_account_id(SimpleNamespace(external_metadata=None)) is None
    assert linked_account_id(SimpleNamespace(external_metadata={"symbol": "VTI"})) is None
    assert linked_account_id(SimpleNamespace(external_metadata={ACCOUNT_LINK_METADATA_KEY: ""})) is None
    assert linked_account_id(SimpleNamespace(external_metadata={ACCOUNT_LINK_METADATA_KEY: 7})) is None
    assert linked_account_id(SimpleNamespace(external_metadata={ACCOUNT_LINK_METADATA_KEY: "x"})) == "x"


def test_is_account_backed_requires_the_account_to_be_counted():
    asset = SimpleNamespace(external_metadata={ACCOUNT_LINK_METADATA_KEY: "x"})
    assert is_account_backed(asset, {"x"}) is True
    assert is_account_backed(asset, {"y"}) is False
    assert is_account_backed(asset, set()) is False
    assert is_account_backed(SimpleNamespace(external_metadata=None), {"x"}) is False


# ---------------------------------------------------------------------------
# Sync stamps (and un-stamps) the owning account
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_links_holding_to_its_account(session: AsyncSession, test_user, test_workspace):
    conn = await _connection(session, test_user.id, test_workspace.id)

    await _run_sync(session, conn, test_workspace.id, test_user.id, _provider([_brokerage()], [_vti()]))
    session.expire_all()

    account = await _account_by_external(session, "inv-1")
    asset = await _asset_by_external(session, "h-1")
    assert asset.external_metadata == {"symbol": "VTI", ACCOUNT_LINK_METADATA_KEY: str(account.id)}
    assert linked_account_id(asset) == str(account.id)


@pytest.mark.asyncio
async def test_resync_backfills_link_on_existing_asset(session: AsyncSession, test_user, test_workspace):
    """Holdings imported before the link existed get it on their next sync."""
    conn = await _connection(session, test_user.id, test_workspace.id)
    account = await _account(
        session, test_user.id, test_workspace.id, connection_id=conn.id,
        external_id="inv-1", balance="1000",
    )
    legacy = Asset(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
        connection_id=conn.id, source="test", external_id="h-1", name="VTI",
        type="investment", currency="BRL", valuation_method="manual",
        external_metadata={"symbol": "VTI", "cost_basis": "1"},
    )
    session.add(legacy)
    await session.commit()
    legacy_id, account_id = legacy.id, str(account.id)

    await _run_sync(
        session, conn, test_workspace.id, test_user.id,
        _provider([_brokerage()], [_vti(metadata={"symbol": "VTI", "cost_basis": "1"})]),
    )
    session.expire_all()

    asset = await _asset_by_external(session, "h-1")
    assert asset.id == legacy_id
    assert asset.external_metadata == {
        "symbol": "VTI", "cost_basis": "1", ACCOUNT_LINK_METADATA_KEY: account_id,
    }


@pytest.mark.asyncio
async def test_sync_drops_link_when_holding_stops_naming_an_account(
    session: AsyncSession, test_user, test_workspace
):
    """Pluggy-shaped holdings (no owning-account hint) never carry the key,
    and a stale key from an earlier sync is removed rather than kept."""
    conn = await _connection(session, test_user.id, test_workspace.id)
    account = await _account(
        session, test_user.id, test_workspace.id, connection_id=conn.id, external_id="inv-1",
    )
    stale = Asset(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
        connection_id=conn.id, source="test", external_id="h-1", name="VTI",
        type="investment", currency="BRL", valuation_method="manual",
        external_metadata={"symbol": "VTI", ACCOUNT_LINK_METADATA_KEY: str(account.id)},
    )
    session.add(stale)
    await session.commit()

    await _run_sync(
        session, conn, test_workspace.id, test_user.id,
        _provider([_brokerage()], [_vti(account_external_id=None)]),
    )
    session.expire_all()

    asset = await _asset_by_external(session, "h-1")
    assert asset.external_metadata == {"symbol": "VTI"}


@pytest.mark.asyncio
async def test_sync_ignores_link_to_an_account_that_was_never_imported(
    session: AsyncSession, test_user, test_workspace
):
    conn = await _connection(session, test_user.id, test_workspace.id)

    await _run_sync(
        session, conn, test_workspace.id, test_user.id,
        _provider([_brokerage()], [_vti(account_external_id="ghost")]),
    )
    session.expire_all()

    asset = await _asset_by_external(session, "h-1")
    assert asset.external_metadata == {"symbol": "VTI"}


# ---------------------------------------------------------------------------
# SimpleFIN types accounts that carry holdings as investment (create only)
# ---------------------------------------------------------------------------


def test_simplefin_types_accounts_with_holdings_as_investment():
    payload = {
        "accounts": [
            {"id": "a1", "name": "401k", "currency": "USD", "balance": "10",
             "holdings": [{"id": "h", "description": "VTI", "market_value": "10"}]},
            {"id": "a2", "name": "Checking", "currency": "USD", "balance": "5", "holdings": []},
            {"id": "a3", "name": "Savings", "currency": "USD", "balance": "5"},
        ]
    }
    _, accounts = SimpleFinProvider._parse_accounts(payload)
    by_id = {a.external_id: a for a in accounts}
    assert by_id["a1"].type == "investment"
    assert by_id["a2"].type == "checking"
    assert by_id["a3"].type == "checking"


# ---------------------------------------------------------------------------
# Net worth report
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_net_worth_skips_holding_backed_by_a_counted_account(
    session: AsyncSession, test_user, test_workspace
):
    conn = await _connection(session, test_user.id, test_workspace.id)
    acc = await _account(
        session, test_user.id, test_workspace.id, connection_id=conn.id,
        external_id="inv-1", balance="1000", acc_type="investment",
    )
    asset = await _holding(
        session, test_user.id, test_workspace.id, connection_id=conn.id, account_id=acc.id,
    )

    dp = await _net_worth_at(session, test_workspace.id, date.today(), "BRL")

    assert dp.breakdowns == {"accounts": 1000.0, "assets": 0.0, "liabilities": 0.0}
    assert dp.value == 1000.0
    keys = {item.key for item in dp.composition}
    assert str(acc.id) in keys
    assert str(asset.id) not in keys


@pytest.mark.asyncio
async def test_net_worth_counts_holding_when_its_account_is_closed(
    session: AsyncSession, test_user, test_workspace
):
    conn = await _connection(session, test_user.id, test_workspace.id)
    acc = await _account(
        session, test_user.id, test_workspace.id, connection_id=conn.id,
        external_id="inv-1", balance="1000", is_closed=True,
    )
    await _holding(session, test_user.id, test_workspace.id, connection_id=conn.id, account_id=acc.id)

    dp = await _net_worth_at(session, test_workspace.id, date.today(), "BRL")

    assert dp.breakdowns["accounts"] == 0.0
    assert dp.breakdowns["assets"] == 900.0
    assert dp.value == 900.0


@pytest.mark.asyncio
async def test_net_worth_still_counts_manual_and_unlinked_assets(
    session: AsyncSession, test_user, test_workspace
):
    conn = await _connection(session, test_user.id, test_workspace.id)
    acc = await _account(
        session, test_user.id, test_workspace.id, connection_id=conn.id,
        external_id="inv-1", balance="1000",
    )
    await _holding(session, test_user.id, test_workspace.id, connection_id=conn.id, account_id=acc.id)
    # Synced, but nothing says which account holds it → still counted.
    await _holding(
        session, test_user.id, test_workspace.id, connection_id=conn.id, value="50", name="BND",
    )
    session.add(Asset(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
        name="Apartment", type="real_estate", currency="BRL",
        purchase_price=Decimal("200000"), purchase_date=date.today() - timedelta(days=30),
    ))
    await session.commit()

    dp = await _net_worth_at(session, test_workspace.id, date.today(), "BRL")

    assert dp.breakdowns["accounts"] == 1000.0
    assert dp.breakdowns["assets"] == 200050.0
    assert dp.value == 201050.0


@pytest.mark.asyncio
async def test_net_worth_collection_without_the_account_counts_the_holding(
    session: AsyncSession, test_user, test_workspace
):
    """The skip is relative to the accounts in the same total: a collection
    that holds the wallet but not the account must not lose the holding."""
    conn = await _connection(session, test_user.id, test_workspace.id)
    acc = await _account(
        session, test_user.id, test_workspace.id, connection_id=conn.id,
        external_id="inv-1", balance="1000",
    )
    other = await _account(session, test_user.id, test_workspace.id, name="Other")
    wallet = AssetGroup(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id, name="Brokerage",
    )
    session.add(wallet)
    await session.commit()
    await _holding(
        session, test_user.id, test_workspace.id, connection_id=conn.id,
        account_id=acc.id, group_id=wallet.id,
    )

    both = await _net_worth_at(
        session, test_workspace.id, date.today(), "BRL",
        account_ids=[acc.id, other.id], asset_group_ids=[wallet.id],
    )
    wallet_only = await _net_worth_at(
        session, test_workspace.id, date.today(), "BRL",
        account_ids=[other.id], asset_group_ids=[wallet.id],
    )

    assert both.breakdowns["accounts"] == 1000.0
    assert both.breakdowns["assets"] == 0.0
    assert wallet_only.breakdowns["accounts"] == 0.0
    assert wallet_only.breakdowns["assets"] == 900.0


# ---------------------------------------------------------------------------
# Dashboard summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_summary_reports_account_backed_holdings_separately(
    session: AsyncSession, test_user, test_workspace
):
    conn = await _connection(session, test_user.id, test_workspace.id)
    acc = await _account(
        session, test_user.id, test_workspace.id, connection_id=conn.id,
        external_id="inv-1", balance="1000",
    )
    other = await _account(session, test_user.id, test_workspace.id, name="Other")
    wallet = AssetGroup(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id, name="Brokerage",
    )
    session.add(wallet)
    await session.commit()
    await _holding(
        session, test_user.id, test_workspace.id, connection_id=conn.id,
        account_id=acc.id, group_id=wallet.id,
    )

    summ = await dashboard_service.get_summary(session, test_workspace.id, test_user.id)

    assert summ.total_balance.get("BRL") == 1000.0
    assert summ.assets_value.get("BRL") is None
    assert summ.account_backed_assets_value.get("BRL") == 900.0
    if summ.primary_currency == "BRL":
        assert summ.total_balance_primary == pytest.approx(1000.0)
        assert summ.assets_value_primary == pytest.approx(0.0)
        assert summ.account_backed_assets_value_primary == pytest.approx(900.0)

    filtered = await dashboard_service.get_summary(
        session, test_workspace.id, test_user.id,
        account_ids=[other.id], asset_group_ids=[wallet.id],
    )

    assert filtered.total_balance.get("BRL") == 900.0
    assert filtered.assets_value.get("BRL") == 900.0
    assert filtered.account_backed_assets_value == {}


# ---------------------------------------------------------------------------
# Goals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_net_worth_goal_skips_account_backed_holdings(
    session: AsyncSession, test_user, test_workspace
):
    conn = await _connection(session, test_user.id, test_workspace.id)
    acc = await _account(
        session, test_user.id, test_workspace.id, connection_id=conn.id,
        external_id="inv-1", balance="1000",
    )
    await _holding(session, test_user.id, test_workspace.id, connection_id=conn.id, account_id=acc.id)
    goal = Goal(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id, name="NW",
        target_amount=Decimal("5000"), current_amount=Decimal("0"), initial_amount=Decimal("0"),
        currency="BRL", tracking_type="net_worth", status="active",
    )
    session.add(goal)
    await session.commit()
    await session.refresh(goal)

    value = await _resolve_current_amount(session, goal, test_user.id)

    assert float(value) == pytest.approx(1000.0, abs=1.0)


# ---------------------------------------------------------------------------
# asset_service primitives
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_asset_values_at_default_still_counts_linked_holdings(
    session: AsyncSession, test_user, test_workspace
):
    conn = await _connection(session, test_user.id, test_workspace.id)
    acc = await _account(
        session, test_user.id, test_workspace.id, connection_id=conn.id,
        external_id="inv-1", balance="1000",
    )
    await _holding(session, test_user.id, test_workspace.id, connection_id=conn.id, account_id=acc.id)

    everything, _ = await get_asset_values_at(session, test_workspace.id, by_workspace=True)
    counted, _ = await get_asset_values_at(
        session, test_workspace.id, by_workspace=True, counted_account_ids=[acc.id],
    )

    assert everything == {"BRL": 900.0}
    assert counted == {}


@pytest.mark.asyncio
async def test_split_asset_values_at_partitions_and_short_circuits(
    session: AsyncSession, test_user, test_workspace
):
    conn = await _connection(session, test_user.id, test_workspace.id)
    acc = await _account(
        session, test_user.id, test_workspace.id, connection_id=conn.id,
        external_id="inv-1", balance="1000",
    )
    await _holding(session, test_user.id, test_workspace.id, connection_id=conn.id, account_id=acc.id)
    await _holding(
        session, test_user.id, test_workspace.id, connection_id=conn.id, value="50", name="BND",
    )

    (counted, counted_primary), (backed, backed_primary) = await split_asset_values_at(
        session, test_workspace.id, primary_currency="BRL", by_workspace=True,
        counted_account_ids=[acc.id],
    )
    empty = await split_asset_values_at(
        session, test_workspace.id, by_workspace=True, group_ids=[], counted_account_ids=[acc.id],
    )

    assert counted == {"BRL": 50.0}
    assert counted_primary == pytest.approx(50.0)
    assert backed == {"BRL": 900.0}
    assert backed_primary == pytest.approx(900.0)
    assert empty == (({}, 0.0), ({}, 0.0))
