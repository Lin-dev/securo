"""The transactions list's `wealth_pnl_only` drill-down filter (plan B5c).

One seeded month exercises every P1 exclusion the Transactions page's click-
through has to respect: a salary (income), an expense with a refund in the
same category (netted), a card-payment transfer pair, an opening balance, a
pending row, an uncategorized trade in an investment account, and an
uncategorized debit in a checking account. `wealth_pnl_only` must return
exactly the P1 P&L rows — and because it shares `wealth.figures.
pnl_row_filter`/`aggregate_pnl_rows` with `wealth.figures.period_figures`, the
list's own income/expense summary over the same window must agree with it to
the cent.

Named `test_transactions_user_pnl_only.py` per the slice brief, but the actual
query parameter is `wealth_pnl_only` — the transactions list already has an
unrelated, pre-existing `user_pnl_only` filter (`counts_as_user_pnl()`, open
accounts only; see `test_transactions_api.py` and
`test_transaction_service_coverage.py`), used by the dashboard's own
drill-down. Reusing that name for the stricter P1 definition would have
changed its behaviour out from under those callers and their tests, so the
Wealth Cockpit's filter gets its own name instead.
"""
import csv
import io
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.category import Category
from app.models.transaction import Transaction
from app.schemas.wealth import AccountAnnotation, WealthSettingsValues
from app.services.transaction_service import get_transactions
from app.services.wealth import figures
from app.services.wealth.spending import month_end, month_start

pytestmark = pytest.mark.asyncio


async def _account(session: AsyncSession, user_id, workspace_id, name, acc_type) -> Account:
    acc = Account(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, name=name,
        type=acc_type, balance=Decimal("0"), currency="USD",
    )
    session.add(acc)
    await session.flush()
    return acc


async def _category(session: AsyncSession, user_id, workspace_id, name, transfer=False) -> Category:
    cat = Category(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, name=name,
        icon="circle-help", color="#123456", treat_as_transfer=transfer,
    )
    session.add(cat)
    await session.flush()
    return cat


def _txn(user_id, workspace_id, account, amount, typ, when, category=None, pair=None, **kw) -> Transaction:
    return Transaction(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, account_id=account.id,
        description=kw.pop("description", f"{typ} {amount}"), amount=Decimal(str(amount)),
        date=when, type=typ, source=kw.pop("source", "manual"), currency="USD",
        status=kw.pop("status", "posted"), created_at=datetime.now(timezone.utc),
        category_id=category.id if category else None, transfer_pair_id=pair, **kw,
    )


# A single month: salary 5000, Shopping expense 800 netted against a 100
# refund (-> 700), an uncategorized checking debit 40 (included), a 1000
# card-payment transfer pair, a 10000 opening balance and a 500 pending row
# (all three excluded), and a 50 uncategorized trade in the investment
# account (excluded by default; brought back in only once its account is
# re-bucketed away from an investment bucket).
INCOME = 5000.0
SHOPPING_DEBIT = 800.0
SHOPPING_REFUND = 100.0
UNCATEGORIZED_CHECKING_DEBIT = 40.0
UNCATEGORIZED_TRADE = 50.0
EXPECTED_EXPENSES = (SHOPPING_DEBIT - SHOPPING_REFUND) + UNCATEGORIZED_CHECKING_DEBIT  # 740


async def _seed(session: AsyncSession, test_user, test_workspace) -> dict:
    uid, wid = test_user.id, test_workspace.id
    day = date.today()
    month_from = month_start(day, 0)
    month_to = month_end(day)

    checking = await _account(session, uid, wid, "Checking", "checking")
    brokerage = await _account(session, uid, wid, "Brokerage", "investment")
    card = await _account(session, uid, wid, "Card", "credit_card")
    salary_cat = await _category(session, uid, wid, "Salary")
    shopping_cat = await _category(session, uid, wid, "Shopping")

    pair = uuid.uuid4()
    txns = [
        _txn(uid, wid, checking, INCOME, "credit", day, salary_cat, description="Salary"),
        _txn(uid, wid, checking, SHOPPING_DEBIT, "debit", day, shopping_cat, description="Shopping expense"),
        _txn(uid, wid, checking, SHOPPING_REFUND, "credit", day, shopping_cat, description="Shopping refund"),
        _txn(uid, wid, checking, UNCATEGORIZED_CHECKING_DEBIT, "debit", day, description="Uncategorized checking debit"),
        # Card-payment transfer pair — both legs excluded.
        _txn(uid, wid, checking, 1000, "debit", day, pair=pair, description="Card payment out"),
        _txn(uid, wid, card, 1000, "credit", day, pair=pair, description="Card payment in"),
        # Opening balance — excluded.
        _txn(uid, wid, checking, 10000, "credit", day, source="opening_balance", description="Opening balance"),
        # Pending row, same category as the real expense — excluded.
        _txn(uid, wid, checking, 500, "debit", day, shopping_cat, status="pending", description="Pending charge"),
        # Uncategorized trade in an investment account — excluded by default.
        _txn(uid, wid, brokerage, UNCATEGORIZED_TRADE, "debit", day, description="Uncategorized trade"),
    ]
    session.add_all(txns)
    await session.commit()
    return {
        "checking": checking, "brokerage": brokerage, "card": card,
        "salary_cat": salary_cat, "shopping_cat": shopping_cat,
        "month_from": month_from, "month_to": month_to, "day": day,
    }


async def test_wealth_pnl_only_returns_exactly_the_p1_rows(session, test_user, test_workspace):
    seed = await _seed(session, test_user, test_workspace)
    settings = WealthSettingsValues()

    rows, total, summary = await get_transactions(
        session, test_workspace.id, test_user.id,
        wealth_pnl_only=True, wealth_settings=settings,
        from_date=seed["month_from"], to_date=seed["month_to"], include_summary=True,
    )

    assert {tx.description for tx in rows} == {
        "Salary", "Shopping expense", "Shopping refund", "Uncategorized checking debit",
    }
    assert total == 4

    assert summary is not None
    assert summary["income"] == Decimal(str(INCOME))
    assert summary["expense"] == Decimal(str(EXPECTED_EXPENSES))


async def test_wealth_pnl_only_summary_matches_period_figures_for_the_month(session, test_user, test_workspace):
    seed = await _seed(session, test_user, test_workspace)
    settings = WealthSettingsValues()

    _, _, summary = await get_transactions(
        session, test_workspace.id, test_user.id,
        wealth_pnl_only=True, wealth_settings=settings,
        from_date=seed["month_from"], to_date=seed["month_to"], include_summary=True,
    )
    pf = await figures.period_figures(
        session, test_workspace.id, test_user.id, seed["month_from"], seed["month_to"], settings=settings,
    )

    assert summary is not None
    assert float(summary["income"]) == pytest.approx(pf.income)
    assert float(summary["expense"]) == pytest.approx(pf.expenses)


async def test_wealth_pnl_only_combines_with_category_id_and_dates_and_nets_refunds(
    session, test_user, test_workspace,
):
    seed = await _seed(session, test_user, test_workspace)
    settings = WealthSettingsValues()

    rows, total, _ = await get_transactions(
        session, test_workspace.id, test_user.id,
        wealth_pnl_only=True, wealth_settings=settings,
        category_id=seed["shopping_cat"].id,
        from_date=seed["month_from"], to_date=seed["month_to"],
    )

    # Exactly the debit and its refund — not the pending Shopping row (wrong
    # status), and not the uncategorized checking debit (no category).
    assert total == 2
    amounts = sorted((tx.type, float(tx.amount)) for tx in rows)
    assert amounts == [("credit", SHOPPING_REFUND), ("debit", SHOPPING_DEBIT)]

    debits = sum(float(tx.amount) for tx in rows if tx.type == "debit")
    refunds = sum(float(tx.amount) for tx in rows if tx.type == "credit")
    # Debits minus refunds, netted per category (plan P1) — the P1 per-category
    # expense for "Shopping" this month.
    assert debits - refunds == pytest.approx(SHOPPING_DEBIT - SHOPPING_REFUND)

    # A date window that excludes the seeded day returns nothing.
    outside_rows, outside_total, _ = await get_transactions(
        session, test_workspace.id, test_user.id,
        wealth_pnl_only=True, wealth_settings=settings,
        category_id=seed["shopping_cat"].id,
        from_date=seed["month_to"] + timedelta(days=1),
        to_date=seed["month_to"] + timedelta(days=1),
    )
    assert outside_total == 0
    assert outside_rows == []


async def test_bucket_override_brings_investment_account_uncategorized_rows_back_in(
    session, test_user, test_workspace,
):
    seed = await _seed(session, test_user, test_workspace)
    override_settings = WealthSettingsValues(
        accounts={str(seed["brokerage"].id): AccountAnnotation(bucket="cash")}
    )

    rows, total, summary = await get_transactions(
        session, test_workspace.id, test_user.id,
        wealth_pnl_only=True, wealth_settings=override_settings,
        from_date=seed["month_from"], to_date=seed["month_to"], include_summary=True,
    )

    assert total == 5
    assert "Uncategorized trade" in {tx.description for tx in rows}
    assert summary is not None
    assert summary["expense"] == Decimal(str(EXPECTED_EXPENSES + UNCATEGORIZED_TRADE))


@pytest.mark.asyncio
async def test_api_wealth_pnl_only_query_param(client, auth_headers, session, test_user, test_workspace):
    """The actual `?wealth_pnl_only=true` query param, end to end: the API loads
    the caller's wealth settings (bucket overrides) from their own preferences."""
    seed = await _seed(session, test_user, test_workspace)

    response = await client.get(
        "/api/transactions",
        params={
            "wealth_pnl_only": "true",
            "from": seed["month_from"].isoformat(),
            "to": seed["month_to"].isoformat(),
        },
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 4
    assert {item["description"] for item in data["items"]} == {
        "Salary", "Shopping expense", "Shopping refund", "Uncategorized checking debit",
    }
    assert Decimal(str(data["summary"]["income"])) == Decimal(str(INCOME))
    assert Decimal(str(data["summary"]["expense"])) == Decimal(str(EXPECTED_EXPENSES))

    # The user's own bucket override (set the same way `PUT /api/wealth/settings`
    # would store it) brings the investment account's uncategorized row back in.
    test_user.preferences = {
        **(test_user.preferences or {}),
        "wealth": {"accounts": {str(seed["brokerage"].id): {"bucket": "cash"}}},
    }
    session.add(test_user)
    await session.commit()

    overridden = await client.get(
        "/api/transactions",
        params={
            "wealth_pnl_only": "true",
            "from": seed["month_from"].isoformat(),
            "to": seed["month_to"].isoformat(),
        },
        headers=auth_headers,
    )
    assert overridden.status_code == 200
    overridden_data = overridden.json()
    assert overridden_data["total"] == 5
    assert "Uncategorized trade" in {item["description"] for item in overridden_data["items"]}


@pytest.mark.asyncio
async def test_export_transactions_wealth_pnl_only(client, auth_headers, session, test_user, test_workspace):
    """The CSV export honours `wealth_pnl_only` the same way the list does."""
    seed = await _seed(session, test_user, test_workspace)

    response = await client.get(
        "/api/transactions/export",
        params={
            "wealth_pnl_only": "true",
            "from": seed["month_from"].isoformat(),
            "to": seed["month_to"].isoformat(),
        },
        headers=auth_headers,
    )
    assert response.status_code == 200
    rows = [r for r in csv.reader(io.StringIO(response.text.lstrip("﻿"))) if r]
    assert len(rows) - 1 == 4  # header + the 4 P1 rows
    descriptions = {row[1] for row in rows[1:]}
    assert descriptions == {"Salary", "Shopping expense", "Shopping refund", "Uncategorized checking debit"}
