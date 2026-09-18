"""`invested` in the transactions summary (fork addition).

Money moved into an investment account is reported once per movement:
the debit leg leaving a non-investment account, or an unpaired
contribution credit inside an investment account whose funding account
is not in Securo. Other transfer-like movement and brokerage buys are not
"invested".
"""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.category import Category
from app.models.transaction import Transaction
from app.services.transaction_service import INVESTMENT_CONTRIBUTION_CATEGORY, get_transactions


async def _account(session, user_id, workspace_id, name, acc_type):
    acc = Account(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, name=name,
        type=acc_type, balance=Decimal("0"), currency="BRL",
    )
    session.add(acc)
    await session.flush()
    return acc


async def _category(session, user_id, workspace_id, name):
    cat = Category(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, name=name,
        icon="circle-help", color="#000000", treat_as_transfer=True,
    )
    session.add(cat)
    await session.flush()
    return cat


def _txn(user_id, workspace_id, account, amount, typ, category=None, pair=None):
    return Transaction(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, account_id=account.id,
        description=f"{typ} {amount}", amount=Decimal(str(amount)), date=date.today(), type=typ,
        source="manual", currency="BRL", status="posted", created_at=datetime.now(timezone.utc),
        category_id=category.id if category else None, transfer_pair_id=pair,
    )


@pytest.mark.asyncio
async def test_summary_invested_counts_each_contribution_once(
    session: AsyncSession, test_user, test_workspace
):
    uid, wid = test_user.id, test_workspace.id
    checking = await _account(session, uid, wid, "Checking", "checking")
    brokerage = await _account(session, uid, wid, "Brokerage", "investment")
    contribution = await _category(session, uid, wid, INVESTMENT_CONTRIBUTION_CATEGORY)
    internal = await _category(session, uid, wid, "Internal transfer")

    pair = uuid.uuid4()
    session.add_all([
        # paired contribution: debit leaves checking, credit lands in brokerage → 100, once
        _txn(uid, wid, checking, 100, "debit", contribution, pair),
        _txn(uid, wid, brokerage, 100, "credit", contribution, pair),
        # unpaired credit inside the brokerage (funding account not in Securo) → 50
        _txn(uid, wid, brokerage, 50, "credit", contribution),
        # unpaired debit to an investment provider that is not connected → 75
        _txn(uid, wid, checking, 75, "debit", contribution),
        # other transfer-like movement is not "invested"
        _txn(uid, wid, checking, 500, "debit", internal),
        # an ordinary expense
        _txn(uid, wid, checking, 30, "debit"),
    ])
    await session.commit()

    _, total, summary = await get_transactions(session, wid, uid, include_summary=True)

    assert total == 6
    assert summary is not None
    assert summary["invested"] == Decimal("225")
    assert summary["income"] == Decimal("0")
    assert summary["expense"] == Decimal("30")
    assert summary["excluded"] == Decimal("825")


@pytest.mark.asyncio
async def test_summary_invested_ignores_buys_inside_the_brokerage(
    session: AsyncSession, test_user, test_workspace
):
    uid, wid = test_user.id, test_workspace.id
    brokerage = await _account(session, uid, wid, "Brokerage", "investment")
    activity = await _category(session, uid, wid, "Brokerage activity")
    session.add(_txn(uid, wid, brokerage, 250, "debit", activity))
    await session.commit()

    _, _, summary = await get_transactions(session, wid, uid, include_summary=True)

    assert summary["invested"] == Decimal("0")
    assert summary["excluded"] == Decimal("250")
