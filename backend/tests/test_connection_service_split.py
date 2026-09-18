"""A split parent survives the provider sync that created it.

The parent keeps its external_id, so a re-sync matches it by id and, being
ignored, leaves it frozen; the lines carry no external_id and a source the
fuzzy matcher never claims, so nothing merges into them and nothing is
inserted twice.
"""

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.bank_connection import BankConnection
from app.models.category import Category
from app.models.transaction import Transaction
from app.schemas.transaction import CategorySplitLineInput
from app.services.category_split_service import split_transaction
from app.services.connection_service import sync_connection


@pytest_asyncio.fixture
async def conn_account(session: AsyncSession, test_user, test_workspace):
    conn = BankConnection(
        id=uuid.uuid4(), user_id=test_user.id, provider="test",
        external_id=f"ext-{uuid.uuid4().hex[:8]}",
        institution_name="Sync Bank", credentials={"token": "fake"},
        status="active", last_sync_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    session.add(conn)
    account = Account(
        id=uuid.uuid4(), user_id=test_user.id, connection_id=conn.id,
        workspace_id=test_workspace.id, name="Checking", type="checking",
        external_id="acc-ext-1", balance=Decimal("0"), currency="BRL",
    )
    session.add(account)
    await session.commit()
    await session.refresh(conn)
    await session.refresh(account)
    return conn, account


def _provider(transactions, account_ext="acc-ext-1"):
    from app.providers.base import AccountData

    p = AsyncMock()
    p.refresh_credentials = AsyncMock(return_value={"token": "t"})
    p.get_accounts = AsyncMock(return_value=[
        AccountData(external_id=account_ext, name="Checking",
                    type="checking", balance=Decimal("0"), currency="BRL"),
    ])
    p.get_transactions = AsyncMock(return_value=transactions)
    return p


def _tx(**kw):
    from app.providers.base import TransactionData

    kw.setdefault("currency", "BRL")
    kw.setdefault("type", "debit")
    kw.setdefault("status", "posted")
    return TransactionData(**kw)


async def _run_sync(session, conn_id, workspace_id, user_id, provider):
    with (
        patch("app.services.connection_service.get_provider", return_value=provider),
        patch("app.services.connection_service.stamp_primary_amount", new_callable=AsyncMock),
        patch("app.services.connection_service.apply_rules_to_transaction", new_callable=AsyncMock),
    ):
        connection, merged = await sync_connection(session, conn_id, workspace_id, user_id)
    # A failed sync rolls back and reports it on the connection; the tests
    # below must not pass on the strength of a sync that did nothing.
    assert connection.status == "active"
    return connection, merged


async def _rows(session, account_id):
    result = await session.execute(
        select(Transaction).where(
            Transaction.account_id == account_id,
            Transaction.source != "opening_balance",
        ).order_by(Transaction.created_at)
    )
    return list(result.scalars().all())


async def _category(session, user_id, workspace_id, name) -> uuid.UUID:
    category_id = uuid.uuid4()
    session.add(Category(
        id=category_id, user_id=user_id, workspace_id=workspace_id,
        name=name, icon="circle-help", color="#000000",
    ))
    await session.commit()
    return category_id


NWM = dict(external_id="nwm-1", description="NORTHWESTERN MUTUAL", amount=Decimal("500.00"), date=date(2026, 9, 3))


@pytest.mark.asyncio
async def test_resync_keeps_a_split_parent_and_its_lines(
    session: AsyncSession, test_user, test_workspace, conn_account
):
    conn, account = conn_account
    # The sync expires the session's objects; hold plain ids from here on.
    conn_id, account_id, ws_id, user_id = conn.id, account.id, test_workspace.id, test_user.id
    insurance = await _category(session, user_id, ws_id, "Insurance")
    invested = await _category(session, user_id, ws_id, "Investment contribution")
    await _run_sync(session, conn_id, ws_id, user_id, _provider([_tx(**NWM)]))
    (parent,) = await _rows(session, account_id)
    parent_id = parent.id
    await split_transaction(session, ws_id, parent_id, [
        CategorySplitLineInput(category_id=insurance, amount=Decimal("250")),
        CategorySplitLineInput(category_id=invested, amount=Decimal("250")),
    ])

    await _run_sync(session, conn_id, ws_id, user_id, _provider([_tx(**NWM)]))

    session.expire_all()
    rows = await _rows(session, account_id)
    assert len(rows) == 3
    parents = [r for r in rows if r.external_id == "nwm-1"]
    assert len(parents) == 1
    assert parents[0].is_ignored is True
    lines = [r for r in rows if r.parent_transaction_id == parents[0].id]
    assert sorted(float(r.amount) for r in lines) == [250.0, 250.0]
    assert {r.source for r in lines} == {"split"}
    assert all(r.external_id is None for r in lines)


@pytest.mark.asyncio
async def test_resync_does_not_merge_a_provider_twin_into_a_line(
    session: AsyncSession, test_user, test_workspace, conn_account
):
    """A second, real 250 charge must land as its own row, not be absorbed
    by the 250 line the split created."""
    conn, account = conn_account
    conn_id, account_id, ws_id, user_id = conn.id, account.id, test_workspace.id, test_user.id
    insurance = await _category(session, user_id, ws_id, "Insurance")
    await _run_sync(session, conn_id, ws_id, user_id, _provider([_tx(**NWM)]))
    (parent,) = await _rows(session, account_id)
    parent_id = parent.id
    await split_transaction(session, ws_id, parent_id, [
        CategorySplitLineInput(category_id=insurance, amount=Decimal("250")),
        CategorySplitLineInput(category_id=None, amount=Decimal("250")),
    ])

    twin = _tx(external_id="nwm-2", description="NORTHWESTERN MUTUAL", amount=Decimal("250.00"), date=date(2026, 9, 4))
    await _run_sync(session, conn_id, ws_id, user_id, _provider([_tx(**NWM), twin]))

    session.expire_all()
    rows = await _rows(session, account_id)
    assert len(rows) == 4
    assert sum(1 for r in rows if r.external_id == "nwm-2" and r.source == "sync") == 1
    assert sum(1 for r in rows if r.parent_transaction_id == parent_id) == 2


# ---------------------------------------------------------------------------
# Rule-driven splits during sync
# ---------------------------------------------------------------------------


async def _split_rule(session, user_id, ws_id, insurance, invested):
    from app.schemas.rule import RuleAction, RuleCondition, RuleCreate
    from app.services.rule_service import create_rule

    await create_rule(session, ws_id, user_id, RuleCreate(
        name="NWM split", conditions_op="and",
        conditions=[RuleCondition(field="description", op="contains", value="NORTHWESTERN")],
        actions=[RuleAction(op="split_categories", value=[
            {"category_id": str(insurance), "amount": "250"},
            {"category_id": str(invested), "remainder": True},
        ])],
    ))


@pytest.mark.asyncio
async def test_sync_splits_a_new_row_by_rule(
    session: AsyncSession, test_user, test_workspace, conn_account
):
    conn, account = conn_account
    conn_id, account_id, ws_id, user_id = conn.id, account.id, test_workspace.id, test_user.id
    insurance = await _category(session, user_id, ws_id, "Insurance")
    invested = await _category(session, user_id, ws_id, "Investment contribution")
    await _split_rule(session, user_id, ws_id, insurance, invested)
    recent = date.today() - timedelta(days=2)
    payload = [
        _tx(external_id="nwm-1", description="NORTHWESTERN MUTUAL", amount=Decimal("500.00"), date=recent),
        _tx(external_id="uber-1", description="UBER", amount=Decimal("20.00"), date=recent),
    ]

    await _run_sync(session, conn_id, ws_id, user_id, _provider(payload))

    session.expire_all()
    rows = await _rows(session, account_id)
    parent = next(r for r in rows if r.external_id == "nwm-1")
    assert parent.is_ignored is True
    lines = sorted((r for r in rows if r.parent_transaction_id == parent.id), key=lambda r: r.created_at)
    assert [(r.category_id, r.amount) for r in lines] == [(insurance, Decimal("250.00")), (invested, Decimal("250.00"))]
    uber = next(r for r in rows if r.external_id == "uber-1")
    assert uber.is_ignored is False and uber.parent_transaction_id is None

    # A second sync of the same payload changes nothing.
    await _run_sync(session, conn_id, ws_id, user_id, _provider(payload))
    session.expire_all()
    assert len(await _rows(session, account_id)) == 4


@pytest.mark.asyncio
async def test_sync_splits_a_row_when_it_posts(
    session: AsyncSession, test_user, test_workspace, conn_account
):
    """A pending charge waits; the sync that flips it to posted splits it,
    even though that row is not among the sync's new ids."""
    conn, account = conn_account
    conn_id, account_id, ws_id, user_id = conn.id, account.id, test_workspace.id, test_user.id
    insurance = await _category(session, user_id, ws_id, "Insurance")
    invested = await _category(session, user_id, ws_id, "Investment contribution")
    await _split_rule(session, user_id, ws_id, insurance, invested)
    recent = date.today() - timedelta(days=2)
    pending_id = uuid.uuid4()
    session.add(Transaction(
        id=pending_id, user_id=user_id, workspace_id=ws_id, account_id=account_id,
        external_id="nwm-1", description="NORTHWESTERN MUTUAL", amount=Decimal("500.00"),
        currency="BRL", date=recent, type="debit", source="sync", status="pending",
    ))
    await session.commit()

    await _run_sync(session, conn_id, ws_id, user_id, _provider([
        _tx(external_id="nwm-1", description="NORTHWESTERN MUTUAL", amount=Decimal("500.00"), date=recent),
    ]))

    session.expire_all()
    rows = await _rows(session, account_id)
    parent = await session.get(Transaction, pending_id)
    assert parent.status == "posted"
    assert parent.is_ignored is True
    assert sum(1 for r in rows if r.parent_transaction_id == pending_id) == 2
    assert len(rows) == 3
