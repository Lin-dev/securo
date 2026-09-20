"""`summary_scope` on get_transactions (fork addition).

Each figure of the transactions summary line is also a row filter built from
the same predicate, so a list restricted to one figure always sums to it. The
summary itself stays computed over the unscoped filtered set (tab strip).
"""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.category import Category
from app.models.transaction import Transaction
from app.services.transaction_service import (
    INVESTMENT_CONTRIBUTION_CATEGORY,
    SUMMARY_SCOPES,
    get_transactions,
)


async def _account(session, user_id, workspace_id, name, acc_type):
    acc = Account(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, name=name,
        type=acc_type, balance=Decimal("0"), currency="BRL",
    )
    session.add(acc)
    await session.flush()
    return acc


async def _category(session, user_id, workspace_id, name, transfer=False):
    cat = Category(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, name=name,
        icon="circle-help", color="#000000", treat_as_transfer=transfer,
    )
    session.add(cat)
    await session.flush()
    return cat


def _txn(user_id, workspace_id, account, amount, typ, category=None, pair=None, **kw):
    fields = dict(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, account_id=account.id,
        description=f"{typ} {amount}", amount=Decimal(str(amount)), date=date.today(), type=typ,
        source="manual", currency="BRL", status="posted", created_at=datetime.now(timezone.utc),
        category_id=category.id if category else None, transfer_pair_id=pair,
    )
    fields.update(kw)
    return Transaction(**fields)


def _abs_sum(rows):
    return sum((abs(r.amount_primary if r.amount_primary is not None else r.amount) for r in rows), Decimal("0"))


def _signed_sum(rows):
    total = Decimal("0")
    for r in rows:
        amt = abs(r.amount_primary if r.amount_primary is not None else r.amount)
        total += amt if r.type == "credit" else -amt
    return total


@pytest.fixture
async def ledger(session: AsyncSession, test_user, test_workspace):
    """One of every kind of row the summary has an opinion about."""
    uid, wid = test_user.id, test_workspace.id
    checking = await _account(session, uid, wid, "Checking", "checking")
    card = await _account(session, uid, wid, "Card", "credit_card")
    brokerage = await _account(session, uid, wid, "Brokerage", "investment")
    salary = await _category(session, uid, wid, "Salary")
    groceries = await _category(session, uid, wid, "Groceries")
    insurance = await _category(session, uid, wid, "Insurance")
    contribution = await _category(session, uid, wid, INVESTMENT_CONTRIBUTION_CATEGORY, transfer=True)
    internal = await _category(session, uid, wid, "Internal transfer", transfer=True)
    buys = await _category(session, uid, wid, "Brokerage activity", transfer=True)

    transfer_pair = uuid.uuid4()
    contribution_pair = uuid.uuid4()
    parent_id = uuid.uuid4()
    rows = {
        # P&L rows
        "salary": _txn(uid, wid, checking, 5000, "credit", salary),
        "groceries": _txn(uid, wid, checking, 120, "debit", groceries),
        "pending_credit": _txn(uid, wid, checking, 40, "credit", salary, status="pending"),
        "refund": _txn(uid, wid, checking, 15, "credit", groceries),
        "line_a": _txn(uid, wid, checking, 300, "debit", groceries, source="split", parent_transaction_id=parent_id),
        "line_b": _txn(uid, wid, checking, 200, "debit", insurance, source="split", parent_transaction_id=parent_id),
        # excluded rows
        "ignored": _txn(uid, wid, checking, 77, "debit", groceries, is_ignored=True),
        "no_pnl": _txn(uid, wid, checking, 66, "debit", groceries, exclude_from_pnl=True),
        "transfer_out": _txn(uid, wid, checking, 300, "debit", internal, transfer_pair),
        "transfer_in": _txn(uid, wid, card, 300, "credit", internal, transfer_pair),
        "unpaired_transfer": _txn(uid, wid, checking, 200, "debit", internal),
        "contribution_unpaired": _txn(uid, wid, checking, 400, "debit", contribution),
        "contribution_paired": _txn(uid, wid, checking, 250, "debit", contribution, contribution_pair),
        "contribution_landed": _txn(uid, wid, brokerage, 250, "credit", contribution, contribution_pair),
        "payroll_contribution": _txn(uid, wid, brokerage, 1000, "credit", contribution),
        "buy": _txn(uid, wid, brokerage, 700, "debit", buys),
        "settlement": _txn(uid, wid, checking, 33, "debit", groceries, source="settlement"),
        # in no scope at all: the lines carry its money
        "parent": _txn(uid, wid, checking, 500, "debit", groceries, id=parent_id, is_ignored=True),
    }
    session.add_all(rows.values())
    await session.commit()
    ids = {name: tx.id for name, tx in rows.items()}
    return uid, wid, ids


async def _scoped(session, uid, wid, scope, **kw):
    rows, total, summary = await get_transactions(
        session, wid, uid, summary_scope=scope, include_summary=True, skip_pagination=True, **kw
    )
    return rows, total, summary


@pytest.mark.asyncio
async def test_each_scope_lists_exactly_the_rows_behind_its_figure(session: AsyncSession, ledger):
    uid, wid, ids = ledger
    all_rows, all_total, summary = await _scoped(session, uid, wid, None)
    assert all_total == len(all_rows) == len(ids)
    assert summary["income"] == Decimal("5055")     # 5000 + 40 pending + 15 refund
    assert summary["expense"] == Decimal("620")     # 120 + the two split lines
    assert summary["net"] == Decimal("4435")
    assert summary["excluded"] == Decimal("3576")
    assert summary["invested"] == Decimal("1650")   # 400 + 250 (debit leg once) + 1000

    by_scope = {}
    for scope in SUMMARY_SCOPES:
        rows, total, scoped_summary = await _scoped(session, uid, wid, scope)
        assert total == len(rows), scope
        assert scoped_summary == summary, f"{scope}: the summary must stay unscoped"
        if scope == "net":
            assert _signed_sum(rows) == summary["net"]
        else:
            assert _abs_sum(rows) == summary[scope], scope
        by_scope[scope] = {r.id for r in rows}

    assert by_scope["income"] == {ids["salary"], ids["pending_credit"], ids["refund"]}
    assert by_scope["expense"] == {ids["groceries"], ids["line_a"], ids["line_b"]}
    assert by_scope["net"] == by_scope["income"] | by_scope["expense"]
    assert not by_scope["income"] & by_scope["expense"]
    assert by_scope["invested"] == {ids["contribution_unpaired"], ids["contribution_paired"], ids["payroll_contribution"]}
    assert by_scope["invested"] <= by_scope["excluded"]
    assert ids["contribution_landed"] not in by_scope["invested"]  # paired credit leg: counted via its debit
    assert ids["buy"] in by_scope["excluded"] and ids["buy"] not in by_scope["invested"]
    assert ids["parent"] not in by_scope["excluded"] and ids["parent"] not in by_scope["net"]
    assert by_scope["net"] | by_scope["excluded"] | {ids["parent"]} == {r.id for r in all_rows}


@pytest.mark.asyncio
async def test_scope_composes_with_other_filters(session: AsyncSession, ledger):
    uid, wid, ids = ledger
    rows, total, summary = await _scoped(session, uid, wid, "excluded", exclude_ignored=True)
    assert total == len(rows)
    assert _abs_sum(rows) == summary["excluded"] == Decimal("3499")  # 3576 minus the ignored 77
    assert ids["ignored"] not in {r.id for r in rows}

    rows, total, summary = await _scoped(session, uid, wid, "income", txn_type="credit", status="posted")
    assert {r.id for r in rows} == {ids["salary"], ids["refund"]}
    assert _abs_sum(rows) == summary["income"] == Decimal("5015")


@pytest.mark.asyncio
async def test_unknown_scope_is_rejected_before_any_query(session: AsyncSession, test_user, test_workspace):
    with pytest.raises(ValueError):
        await get_transactions(session, test_workspace.id, test_user.id, summary_scope="bogus")
