"""Money Map lanes in the income/expenses report (fork change).

Transfer-style categories are kept out of income and expenses. The Money Map
draws them as real movements of money only: investing debits from cash
accounts and unpaired contribution credits inside investment accounts are
"investments" (the latter also an inflow, "direct_contributions"); other
unpaired cash-account legs are "transfers_out" / "transfers_in"; buys and
exchanges inside investment accounts are not cash flow and stay out.
"""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.category import Category
from app.models.transaction import Transaction
from app.services._query_filters import INVESTMENT_CONTRIBUTION_CATEGORY
from app.services.report_service import get_income_expenses_report


def _py_to_char(value, fmt):
    """SQLite stand-in for the Postgres to_char the report buckets periods with."""
    if value is None:
        return None
    y, m, d = str(value)[:10].split("-")
    return {"YYYY-MM-DD": f"{y}-{m}-{d}", "YYYY-MM": f"{y}-{m}", "YYYY": y}.get(fmt, str(value)[:10])


@pytest.fixture(autouse=True)
async def _ensure_to_char(session: AsyncSession):
    raw = await session.connection()

    def _do(sync_conn):
        sync_conn.connection.dbapi_connection.create_function("to_char", 2, _py_to_char)

    await raw.run_sync(_do)
    yield


async def _account(session, user_id, workspace_id, name, acc_type, **kw):
    acc = Account(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, name=name,
        type=acc_type, balance=Decimal("0"), currency="BRL", **kw,
    )
    session.add(acc)
    await session.flush()
    return acc


async def _category(session, user_id, workspace_id, name, transfer=True):
    cat = Category(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, name=name,
        icon="circle-help", color="#123456", treat_as_transfer=transfer,
    )
    session.add(cat)
    await session.flush()
    return cat


def _txn(user_id, workspace_id, account, amount, typ, category=None, pair=None, **kw):
    return Transaction(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, account_id=account.id,
        description=f"{typ} {amount}", amount=Decimal(str(amount)), date=date.today(), type=typ,
        source="manual", currency="BRL", status="posted", created_at=datetime.now(timezone.utc),
        category_id=category.id if category else None, transfer_pair_id=pair, **kw,
    )


def _by_group(composition, group):
    return {c.key: c for c in composition if c.group == group}


@pytest.mark.asyncio
async def test_money_map_lanes_follow_the_cash_boundary(session: AsyncSession, test_user, test_workspace):
    uid, wid = test_user.id, test_workspace.id
    checking = await _account(session, uid, wid, "Checking", "checking")
    card = await _account(session, uid, wid, "Card", "credit_card")
    brokerage = await _account(session, uid, wid, "Brokerage", "investment")
    plan = await _account(session, uid, wid, "Employer 401(k)", "investment")
    contribution = await _category(session, uid, wid, INVESTMENT_CONTRIBUTION_CATEGORY)
    card_payment = await _category(session, uid, wid, "Card payment")
    internal = await _category(session, uid, wid, "Internal transfer")
    brokerage_activity = await _category(session, uid, wid, "Brokerage activity")
    salary = await _category(session, uid, wid, "Salary", transfer=False)
    groceries = await _category(session, uid, wid, "Groceries", transfer=False)

    paired_contribution = uuid.uuid4()
    paired_card_payment = uuid.uuid4()
    session.add_all([
        _txn(uid, wid, checking, 5000, "credit", salary),
        _txn(uid, wid, checking, 120, "debit", groceries),
        # investing debits from cash: unpaired and paired both count once
        _txn(uid, wid, checking, 400, "debit", contribution),
        _txn(uid, wid, checking, 300, "debit", contribution, paired_contribution),
        _txn(uid, wid, brokerage, 300, "credit", contribution, paired_contribution),
        # payroll deduction landing straight in the plan: investing AND an inflow
        _txn(uid, wid, plan, 1000, "credit", contribution),
        _txn(uid, wid, plan, 250, "credit", contribution),
        # buys and exchanges inside investment accounts are not cash flow
        _txn(uid, wid, brokerage, 700, "debit", brokerage_activity),
        _txn(uid, wid, plan, 1250, "debit", brokerage_activity),
        _txn(uid, wid, brokerage, 80, "credit", brokerage_activity),
        # card payment that did not pair: money left, destination unknown
        _txn(uid, wid, checking, 200, "debit", card_payment),
        # card payment that did pair: neither leg shows
        _txn(uid, wid, checking, 500, "debit", card_payment, paired_card_payment),
        _txn(uid, wid, card, 500, "credit", card_payment, paired_card_payment),
        # money arriving in cash from an account Securo cannot see
        _txn(uid, wid, checking, 150, "credit", internal),
        # ignored and excluded rows never count
        _txn(uid, wid, checking, 999, "debit", contribution, is_ignored=True),
        _txn(uid, wid, checking, 998, "debit", internal, exclude_from_pnl=True),
    ])
    await session.commit()

    report = await get_income_expenses_report(session, wid, uid, months=1, interval="monthly")
    comp = report.composition

    income = _by_group(comp, "income")
    expenses = _by_group(comp, "expenses")
    assert {c.label: c.value for c in income.values()} == {"Salary": 5000.0}
    assert {c.label: c.value for c in expenses.values()} == {"Groceries": 120.0}

    investments = _by_group(comp, "investments")
    assert investments[str(contribution.id)].value == 700.0  # 400 unpaired + 300 paired, once
    assert investments[f"account:{plan.id}"].label == "Employer 401(k)"
    assert investments[f"account:{plan.id}"].value == 1250.0
    assert f"account:{brokerage.id}" not in investments  # its credit leg was paired
    assert len(investments) == 2

    direct = _by_group(comp, "direct_contributions")
    assert list(direct) == ["direct_contributions"]
    assert direct["direct_contributions"].value == 1250.0

    assert {c.label: c.value for c in _by_group(comp, "transfers_out").values()} == {"Card payment": 200.0}
    assert {c.label: c.value for c in _by_group(comp, "transfers_in").values()} == {"Internal transfer": 150.0}

    # nothing from inside the investment accounts leaked into any lane
    assert all("Brokerage activity" != c.label for c in comp)


@pytest.mark.asyncio
async def test_money_map_ignores_closed_accounts_and_hidden_categories(session: AsyncSession, test_user, test_workspace):
    uid, wid = test_user.id, test_workspace.id
    open_checking = await _account(session, uid, wid, "Open", "checking")
    closed_checking = await _account(session, uid, wid, "Closed", "checking", is_closed=True)
    contribution = await _category(session, uid, wid, INVESTMENT_CONTRIBUTION_CATEGORY)
    hidden = await _category(session, uid, wid, "Old transfers")
    hidden.is_ignored = True
    session.add_all([
        _txn(uid, wid, closed_checking, 400, "debit", contribution),
        _txn(uid, wid, open_checking, 60, "debit", hidden),
        _txn(uid, wid, open_checking, 90, "debit", contribution),
    ])
    await session.commit()

    report = await get_income_expenses_report(session, wid, uid, months=1, interval="monthly")
    lanes = [c for c in report.composition if c.group in ("investments", "transfers_out", "transfers_in", "direct_contributions")]
    assert [(c.group, c.value) for c in lanes] == [("investments", 90.0)]
