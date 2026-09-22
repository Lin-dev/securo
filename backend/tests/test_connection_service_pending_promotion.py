"""Sync pipeline: settled rows that arrive under a new provider id (Pass 1b)
and rows the provider withdrew (`get_removed_transaction_ids`).

Plaid mints a new transaction_id when a pending charge posts and lists the
old id under `removed`. Without these two hooks every settled charge would
land twice (Pass 3 only catches a flip on an identical date) and dropped
authorizations would linger as pending forever.
"""

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.bank_connection import BankConnection
from app.models.transaction import Transaction
from app.providers.base import AccountData, TransactionData
from app.services.connection_service import sync_connection


async def _connection_with_account(session, user_id, workspace_id):
    conn = BankConnection(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, provider="test",
        external_id=f"ext-{uuid.uuid4().hex[:8]}", institution_name="Promo Bank",
        credentials={"token": "fake"}, status="active",
        last_sync_at=datetime.now(timezone.utc), created_at=datetime.now(timezone.utc),
    )
    session.add(conn)
    await session.flush()
    account = Account(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, connection_id=conn.id,
        external_id="acc-1", name="Checking", type="checking", balance=Decimal("0"), currency="USD",
    )
    session.add(account)
    await session.commit()
    # Return ids, not ORM objects: the sync commits and expires the session,
    # and touching an expired attribute afterwards needs a greenlet context.
    conn_id, account_id = conn.id, account.id
    return conn_id, account_id


def _synced_row(user_id, workspace_id, account_id, external_id, *, amount="10.00", status="pending",
                txn_date=None, description="PENDING COFFEE", **kw):
    fields = dict(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, account_id=account_id,
        external_id=external_id, description=description, original_description=description,
        amount=Decimal(amount), date=txn_date or date.today(), type="debit", status=status,
        source="sync", currency="USD", created_at=datetime.now(timezone.utc),
    )
    fields.update(kw)
    return Transaction(**fields)


def _provider(transactions, removed=None):
    p = AsyncMock()
    p.refresh_credentials = AsyncMock(return_value={"token": "fake"})
    p.get_accounts = AsyncMock(return_value=[
        AccountData(external_id="acc-1", name="Checking", type="checking",
                    balance=Decimal("0"), currency="USD"),
    ])
    p.get_transactions = AsyncMock(return_value=transactions)
    p.get_removed_transaction_ids = AsyncMock(return_value=removed or [])
    return p


async def _run(session, conn_id, workspace_id, user_id, provider):
    with patch("app.services.connection_service.get_provider", return_value=provider), \
         patch("app.services.connection_service.detect_transfer_pairs", new_callable=AsyncMock) as detect, \
         patch("app.services.connection_service.stamp_primary_amount", new_callable=AsyncMock) as stamp, \
         patch("app.services.connection_service.apply_rules_to_transaction", new_callable=AsyncMock):
        await sync_connection(session, conn_id, workspace_id, user_id)
    return detect, stamp


async def _rows(session, account_id):
    return (await session.execute(
        select(Transaction).where(Transaction.account_id == account_id, Transaction.source == "sync")
        .order_by(Transaction.created_at)
    )).scalars().all()


@pytest.mark.asyncio
async def test_settled_transaction_re_keys_its_pending_row(session: AsyncSession, test_user, test_workspace):
    uid, wid = test_user.id, test_workspace.id
    conn_id, account_id = await _connection_with_account(session, uid, wid)
    yesterday = date.today() - timedelta(days=2)
    pending = _synced_row(uid, wid, account_id, "pend-1", txn_date=yesterday)
    session.add(pending)
    await session.commit()

    settled = TransactionData(
        external_id="post-1", description="COFFEE SHOP", amount=Decimal("12.00"),
        date=date.today(), type="debit", status="posted", raw_data={"id": "post-1"},
        pending_external_id="pend-1",
    )
    detect, stamp = await _run(session, conn_id, wid, uid, _provider([settled]))

    rows = await _rows(session, account_id)
    assert [r.external_id for r in rows] == ["post-1"]
    row = rows[0]
    assert row.status == "posted"
    assert row.date == date.today()
    assert row.amount == Decimal("12.00")
    assert row.description == "COFFEE SHOP"
    assert row.original_description == "COFFEE SHOP"
    assert row.raw_data == {"id": "post-1"}
    stamp.assert_awaited_once()
    assert stamp.await_args.args[2].id == row.id
    # the settled row gets a second look from transfer detection
    detect.assert_awaited_once()
    assert row.id in detect.await_args.kwargs["candidate_ids"]


@pytest.mark.asyncio
async def test_user_edited_description_survives_settlement(session: AsyncSession, test_user, test_workspace):
    uid, wid = test_user.id, test_workspace.id
    conn_id, account_id = await _connection_with_account(session, uid, wid)
    pending = _synced_row(uid, wid, account_id, "pend-1")
    pending.description = "Coffee with Sam"  # edited by the user, differs from original
    session.add(pending)
    await session.commit()

    settled = TransactionData(external_id="post-1", description="COFFEE SHOP", amount=Decimal("10.00"),
                              date=date.today(), type="debit", status="posted", pending_external_id="pend-1")
    _, stamp = await _run(session, conn_id, wid, uid, _provider([settled]))

    (row,) = await _rows(session, account_id)
    assert row.external_id == "post-1"
    assert row.description == "Coffee with Sam"
    assert row.original_description == "COFFEE SHOP"
    stamp.assert_not_awaited()  # amount unchanged


@pytest.mark.asyncio
async def test_ignored_pending_row_is_only_re_keyed(session: AsyncSession, test_user, test_workspace):
    uid, wid = test_user.id, test_workspace.id
    conn_id, account_id = await _connection_with_account(session, uid, wid)
    session.add(_synced_row(uid, wid, account_id, "pend-1", is_ignored=True))
    await session.commit()

    settled = TransactionData(external_id="post-1", description="COFFEE SHOP", amount=Decimal("15.00"),
                              date=date.today(), type="debit", status="posted", pending_external_id="pend-1")
    await _run(session, conn_id, wid, uid, _provider([settled]))

    (row,) = await _rows(session, account_id)
    assert row.external_id == "post-1"      # re-keyed so no twin can appear
    assert row.status == "pending"          # but frozen otherwise
    assert row.amount == Decimal("10.00")
    assert row.is_ignored is True


@pytest.mark.asyncio
async def test_unknown_pending_id_falls_through_to_a_normal_insert(session: AsyncSession, test_user, test_workspace):
    uid, wid = test_user.id, test_workspace.id
    conn_id, account_id = await _connection_with_account(session, uid, wid)
    settled = TransactionData(external_id="post-9", description="LUNCH", amount=Decimal("20.00"),
                              date=date.today(), type="debit", status="posted", pending_external_id="never-seen")
    await _run(session, conn_id, wid, uid, _provider([settled]))
    rows = await _rows(session, account_id)
    assert [r.external_id for r in rows] == ["post-9"]
    assert rows[0].status == "posted"


@pytest.mark.asyncio
async def test_removed_ids_delete_only_unpaired_pending_rows(session: AsyncSession, test_user, test_workspace):
    uid, wid = test_user.id, test_workspace.id
    conn_id, account_id = await _connection_with_account(session, uid, wid)
    # Distinct amounts and descriptions: the pipeline's phantom-duplicate
    # cleanup would otherwise treat identical rows next to a paired one as
    # duplicates, which is not what this test is about.
    session.add_all([
        _synced_row(uid, wid, account_id, "p-gone", amount="11.00", description="AUTH ONE"),
        _synced_row(uid, wid, account_id, "posted-gone", amount="12.00", status="posted", description="SETTLED TWO"),
        _synced_row(uid, wid, account_id, "p-paired", amount="13.00", description="TRANSFER THREE",
                    transfer_pair_id=uuid.uuid4()),
        _synced_row(uid, wid, account_id, "p-ignored", amount="14.00", description="HIDDEN FOUR", is_ignored=True),
        _synced_row(uid, wid, account_id, "p-stays", amount="15.00", description="KEEP FIVE"),
    ])
    await session.commit()

    await _run(session, conn_id, wid, uid,
               _provider([], removed=["p-gone", "posted-gone", "p-paired", "p-ignored", "not-here"]))

    left = sorted(r.external_id for r in await _rows(session, account_id))
    assert left == ["p-ignored", "p-paired", "p-stays", "posted-gone"]


@pytest.mark.asyncio
async def test_promoted_and_removed_in_the_same_run_keeps_the_row(session: AsyncSession, test_user, test_workspace):
    uid, wid = test_user.id, test_workspace.id
    conn_id, account_id = await _connection_with_account(session, uid, wid)
    session.add(_synced_row(uid, wid, account_id, "pend-2"))
    await session.commit()

    settled = TransactionData(external_id="post-2", description="COFFEE SHOP", amount=Decimal("10.00"),
                              date=date.today(), type="debit", status="posted", pending_external_id="pend-2")
    await _run(session, conn_id, wid, uid, _provider([settled], removed=["pend-2"]))

    rows = await _rows(session, account_id)
    assert [r.external_id for r in rows] == ["post-2"]
    assert rows[0].status == "posted"


@pytest.mark.asyncio
async def test_providers_without_the_hook_or_with_odd_returns_are_tolerated(session: AsyncSession, test_user, test_workspace):
    uid, wid = test_user.id, test_workspace.id
    conn_id, account_id = await _connection_with_account(session, uid, wid)
    session.add(_synced_row(uid, wid, account_id, "p-1"))
    await session.commit()

    bare = AsyncMock()  # get_removed_transaction_ids resolves to a MagicMock, not a list
    bare.refresh_credentials = AsyncMock(return_value={"token": "fake"})
    bare.get_accounts = AsyncMock(return_value=[
        AccountData(external_id="acc-1", name="Checking", type="checking", balance=Decimal("0"), currency="USD")
    ])
    bare.get_transactions = AsyncMock(return_value=[])
    await _run(session, conn_id, wid, uid, bare)
    assert [r.external_id for r in await _rows(session, account_id)] == ["p-1"]
