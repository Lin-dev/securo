"""Upcoming charges, the 90-day cash forecast and the subscription audit
(fork, plan R2, B5b) — `app/services/wealth/cashflow.py` and its three
`/api/wealth` endpoints.

Each test seeds its own household directly through `session`; the dedupe,
forecast and subscription tests call the service functions directly (a
`WorkspaceContext` built the same way `tests/test_wealth_settings.py`
does), and the API tests round-trip through `client` + `auth_headers`.
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.workspace_context import WorkspaceContext
from app.models.account import Account
from app.models.category import Category
from app.models.invoice import Invoice
from app.models.recurring_transaction import RecurringTransaction
from app.models.transaction import Transaction
from app.models.workspace import WorkspaceMember
from app.schemas.recurring_transaction import RecurringTransactionCreate
from app.schemas.wealth import WealthSettingsValues
from app.services.wealth import cashflow
from app.services.wealth.merchant import merchant_key
from app.services.wealth.spending import month_start

pytestmark = pytest.mark.asyncio

CURRENCY = "BRL"  # matches `test_workspace.default_currency` — no FX in play
TODAY = date.today()


# --- seeding helpers ---------------------------------------------------------


async def _ctx(session: AsyncSession, test_user, test_workspace) -> WorkspaceContext:
    member = (
        await session.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == test_workspace.id,
                WorkspaceMember.user_id == test_user.id,
            )
        )
    ).scalar_one()
    return WorkspaceContext(workspace=test_workspace, member=member, user=test_user)


async def _account(session: AsyncSession, user_id, workspace_id, *, name, acc_type, **kw) -> Account:
    acc = Account(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, name=name, type=acc_type,
        balance=Decimal("0"), currency=CURRENCY, **kw,
    )
    session.add(acc)
    await session.flush()
    return acc


async def _category(session: AsyncSession, user_id, workspace_id, name: str) -> Category:
    cat = Category(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, name=name,
        icon="circle-help", color="#123456",
    )
    session.add(cat)
    await session.flush()
    return cat


def _txn(user_id, workspace_id, account, amount, typ, category=None, **kw) -> Transaction:
    when = kw.pop("when", TODAY)
    return Transaction(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, account_id=account.id,
        description=kw.pop("description", f"{typ} {amount}"), amount=Decimal(str(amount)),
        date=when, type=typ, source=kw.pop("source", "manual"), currency=CURRENCY,
        status=kw.pop("status", "posted"), category_id=category.id if category else None, **kw,
    )


def _recurring(user_id, workspace_id, account, amount, typ, *, description, next_occurrence,
               frequency="monthly", start_date=None, category=None, is_active=True) -> RecurringTransaction:
    return RecurringTransaction(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, account_id=account.id,
        category_id=category.id if category else None, description=description,
        amount=Decimal(str(amount)), currency=CURRENCY, type=typ, frequency=frequency,
        start_date=start_date or next_occurrence, next_occurrence=next_occurrence, is_active=is_active,
    )


async def _two_full_months(session, uid, wid, checking: Account, salary: Category, rent: Category) -> None:
    """Two full prior calendar months, each with a $2,000 Rent debit and one
    $12,000 Salary credit two months back — `coverage_window` then sees >=60
    days of history (safely over `MIN_COVERAGE_DAYS`) and `typical_figures`
    sees two full months, each with the same $2,000 expense (a stable
    median). Net effect on the checking account today: 12,000 - 2,000 -
    2,000 = 8,000 (`start_cash` in the forecast tests)."""
    m2, m1 = month_start(TODAY, 2), month_start(TODAY, 1)
    session.add_all([
        _txn(uid, wid, checking, 12_000, "credit", salary, when=m2),
        _txn(uid, wid, checking, 2_000, "debit", rent, when=m2 + timedelta(days=10)),
        _txn(uid, wid, checking, 2_000, "debit", rent, when=m1 + timedelta(days=10)),
    ])
    await session.commit()


# --- dedupe: upcoming() ------------------------------------------------------


async def test_card_subscription_is_listed_but_only_the_bill_counts_toward_cash(
    session: AsyncSession, test_user, test_workspace,
):
    uid, wid = test_user.id, test_workspace.id
    ctx = await _ctx(session, test_user, test_workspace)
    card = await _account(
        session, uid, wid, name="Visa", acc_type="credit_card",
        # The due date lands on today itself (nearest upcoming occurrence of
        # today's own day-of-month), so it is deterministic regardless of
        # when the suite runs, and well inside a short window.
        statement_close_day=TODAY.day, payment_due_day=TODAY.day,
    )
    # A card purchase today: the card's running debt the bill fallback reads.
    session.add(_txn(uid, wid, card, 50, "debit", description="Groceries", when=TODAY))
    # A subscription billed to the same card, with no matching real row. A 10-day
    # window keeps this to its single occurrence (the next one lands ~1 month out).
    session.add(_recurring(
        uid, wid, card, 12, "debit", description="Streaming", next_occurrence=TODAY + timedelta(days=5),
    ))
    await session.commit()

    result = await cashflow.upcoming(session, ctx, days=10)

    streaming = [i for i in result.items if i.description == "Streaming"]
    assert len(streaming) == 1
    assert streaming[0].kind == "recurring"
    assert streaming[0].account == "Visa"

    bills = [i for i in result.items if i.kind == "card_bill"]
    assert len(bills) == 1
    assert bills[0].account == "Visa"
    assert bills[0].amount == pytest.approx(50.0)

    # Only the bill hits cash; the subscription (a card purchase) does not.
    assert result.total == pytest.approx(50.0)


async def test_charge_posted_one_day_late_does_not_duplicate_its_projection(
    session: AsyncSession, test_user, test_workspace,
):
    uid, wid = test_user.id, test_workspace.id
    ctx = await _ctx(session, test_user, test_workspace)
    checking = await _account(session, uid, wid, name="Checking", acc_type="checking")

    projected_date = TODAY + timedelta(days=10)
    session.add(_recurring(
        uid, wid, checking, 1_000, "debit", description="Rent", next_occurrence=projected_date,
    ))
    # The real charge posts a day late, same amount, same merchant — not linked to the rule.
    session.add(_txn(
        uid, wid, checking, 1_000, "debit", description="Rent", when=projected_date + timedelta(days=1),
    ))
    await session.commit()

    result = await cashflow.upcoming(session, ctx, days=30)

    rent_items = [i for i in result.items if i.description == "Rent"]
    assert len(rent_items) == 1
    assert rent_items[0].date == projected_date + timedelta(days=1)
    assert rent_items[0].kind == "pending"  # the real, future-dated posted row


async def test_installments_on_a_card_count_once_inside_its_bill(
    session: AsyncSession, test_user, test_workspace,
):
    uid, wid = test_user.id, test_workspace.id
    ctx = await _ctx(session, test_user, test_workspace)
    card = await _account(
        session, uid, wid, name="Mastercard", acc_type="credit_card",
        statement_close_day=5, payment_due_day=15,
    )
    session.add(Transaction(
        id=uuid.uuid4(), user_id=uid, workspace_id=wid, account_id=card.id,
        description="Sofa 2/3", amount=Decimal("40.00"), date=TODAY, type="debit",
        source="manual", currency=CURRENCY, status="pending",
        installment_number=2, total_installments=3, installment_total_amount=Decimal("120.00"),
        installment_purchase_date=TODAY - timedelta(days=30),
    ))
    await session.commit()

    result = await cashflow.upcoming(session, ctx, days=45)

    assert [i for i in result.items if i.kind == "installment"] == []
    bills = [i for i in result.items if i.kind == "card_bill"]
    assert len(bills) == 1
    assert bills[0].amount == pytest.approx(40.0)


async def test_an_invoice_is_excluded_from_upcoming(session: AsyncSession, test_user, test_workspace):
    uid, wid = test_user.id, test_workspace.id
    ctx = await _ctx(session, test_user, test_workspace)
    await _account(session, uid, wid, name="Checking", acc_type="checking")
    session.add(Invoice(
        id=uuid.uuid4(), workspace_id=wid, user_id=uid, status="open", number=1001,
        issue_date=TODAY, due_date=TODAY + timedelta(days=10), currency=CURRENCY,
        total=Decimal("500.00"),
    ))
    await session.commit()

    result = await cashflow.upcoming(session, ctx, days=30)
    assert result.items == []
    assert result.total == 0.0


# --- cash forecast ------------------------------------------------------------


async def test_cash_forecast_start_cash_equals_the_cash_bucket(
    session: AsyncSession, test_user, test_workspace,
):
    uid, wid = test_user.id, test_workspace.id
    ctx = await _ctx(session, test_user, test_workspace)
    checking = await _account(session, uid, wid, name="Checking", acc_type="checking")
    salary, rent = await _category(session, uid, wid, "Salary"), await _category(session, uid, wid, "Rent")
    await _two_full_months(session, uid, wid, checking, salary, rent)

    from app.services.wealth.valuation import latest_net_worth
    point = await latest_net_worth(session, wid, uid, {})

    result = await cashflow.cash_forecast(session, ctx, WealthSettingsValues(), days=30)
    assert result.start_cash == pytest.approx(point.buckets["cash"])
    assert result.start_cash == pytest.approx(8_000.0)
    assert result.points[0].date == TODAY


async def test_an_income_rule_raises_the_balance_on_its_date(
    session: AsyncSession, test_user, test_workspace,
):
    uid, wid = test_user.id, test_workspace.id
    ctx = await _ctx(session, test_user, test_workspace)
    checking = await _account(session, uid, wid, name="Checking", acc_type="checking")
    salary, rent = await _category(session, uid, wid, "Salary"), await _category(session, uid, wid, "Rent")
    await _two_full_months(session, uid, wid, checking, salary, rent)

    income_date = TODAY + timedelta(days=5)
    session.add(_recurring(
        uid, wid, checking, 500, "credit", description="Paycheck", next_occurrence=income_date,
    ))
    await session.commit()

    result = await cashflow.cash_forecast(session, ctx, WealthSettingsValues(), days=30)
    income_point = next(p for p in result.points if p.date == income_date)
    assert income_point.inflow == pytest.approx(500.0)
    assert income_point.balance == pytest.approx(result.start_cash + 500.0)


async def test_a_large_bill_creates_the_low_point_and_first_below_buffer_date(
    session: AsyncSession, test_user, test_workspace,
):
    uid, wid = test_user.id, test_workspace.id
    ctx = await _ctx(session, test_user, test_workspace)
    checking = await _account(session, uid, wid, name="Checking", acc_type="checking")
    salary, rent = await _category(session, uid, wid, "Salary"), await _category(session, uid, wid, "Rent")
    await _two_full_months(session, uid, wid, checking, salary, rent)

    bill_date = TODAY + timedelta(days=10)
    session.add(_recurring(
        uid, wid, checking, 5_000, "debit", description="Tuition", next_occurrence=bill_date,
    ))
    await session.commit()

    result = await cashflow.cash_forecast(session, ctx, WealthSettingsValues(), days=30)
    assert result.buffer == pytest.approx(6_000.0)  # 3 * median(2,000, 2,000)
    assert result.low_point is not None
    assert result.low_point.balance == pytest.approx(3_000.0)  # 8,000 - 5,000
    assert result.first_below_buffer_date == bill_date
    assert result.first_negative_date is None


async def test_a_negative_case_sets_first_negative_date(
    session: AsyncSession, test_user, test_workspace,
):
    uid, wid = test_user.id, test_workspace.id
    ctx = await _ctx(session, test_user, test_workspace)
    checking = await _account(session, uid, wid, name="Checking", acc_type="checking")
    salary, rent = await _category(session, uid, wid, "Salary"), await _category(session, uid, wid, "Rent")
    await _two_full_months(session, uid, wid, checking, salary, rent)

    bill_date = TODAY + timedelta(days=10)
    session.add(_recurring(
        uid, wid, checking, 11_000, "debit", description="Surgery", next_occurrence=bill_date,
    ))
    await session.commit()

    result = await cashflow.cash_forecast(session, ctx, WealthSettingsValues(), days=30)
    assert result.first_negative_date == bill_date
    assert result.low_point is not None
    assert result.low_point.balance == pytest.approx(-3_000.0)  # 8,000 - 11,000


# --- subscription audit -------------------------------------------------------


async def test_a_tracked_rule_appears_with_its_recurring_id(
    session: AsyncSession, test_user, test_workspace,
):
    uid, wid = test_user.id, test_workspace.id
    ctx = await _ctx(session, test_user, test_workspace)
    checking = await _account(session, uid, wid, name="Checking", acc_type="checking")
    rec = _recurring(
        uid, wid, checking, Decimal("15.99"), "debit", description="Netflix",
        next_occurrence=TODAY + timedelta(days=3),
    )
    session.add(rec)
    await session.commit()

    result = await cashflow.subscription_audit(session, ctx)
    netflix = [s for s in result.subscriptions if s.recurring_id == rec.id]
    assert len(netflix) == 1
    assert netflix[0].merchant == "Netflix"
    assert netflix[0].monthly == pytest.approx(15.99)
    assert netflix[0].annual == pytest.approx(15.99 * 12)


async def _seed_hulu_charges(session, uid, wid, checking: Account, category: Category) -> None:
    for k in (1, 2, 3):
        session.add(_txn(
            uid, wid, checking, 10, "debit", category, description="HULU #4521",
            when=month_start(TODAY, k) + timedelta(days=5),
        ))
    await session.commit()


async def test_a_detected_untracked_merchant_appears_in_suggestions_with_a_valid_create_payload(
    session: AsyncSession, test_user, test_workspace,
):
    uid, wid = test_user.id, test_workspace.id
    ctx = await _ctx(session, test_user, test_workspace)
    checking = await _account(session, uid, wid, name="Checking", acc_type="checking")
    streaming = await _category(session, uid, wid, "Streaming")
    await _seed_hulu_charges(session, uid, wid, checking, streaming)

    result = await cashflow.subscription_audit(session, ctx)

    suggestions = [s for s in result.suggestions if merchant_key(s.merchant) == merchant_key("Hulu")]
    assert len(suggestions) == 1
    suggestion = suggestions[0]
    assert suggestion.monthly == pytest.approx(10.0)
    assert suggestion.months == 3

    validated = RecurringTransactionCreate(**suggestion.create_payload)
    assert validated.account_id == checking.id
    assert validated.category_id == streaming.id
    assert validated.type == "debit"
    assert validated.frequency == "monthly"


async def test_a_tracked_merchant_is_not_suggested(session: AsyncSession, test_user, test_workspace):
    uid, wid = test_user.id, test_workspace.id
    ctx = await _ctx(session, test_user, test_workspace)
    checking = await _account(session, uid, wid, name="Checking", acc_type="checking")
    streaming = await _category(session, uid, wid, "Streaming")
    await _seed_hulu_charges(session, uid, wid, checking, streaming)

    rec = _recurring(
        uid, wid, checking, 10, "debit", description="Hulu", next_occurrence=TODAY + timedelta(days=3),
    )
    session.add(rec)
    await session.commit()

    result = await cashflow.subscription_audit(session, ctx)

    assert [s for s in result.suggestions if merchant_key(s.merchant) == merchant_key("Hulu")] == []
    tracked = [s for s in result.subscriptions if s.recurring_id == rec.id]
    assert len(tracked) == 1
    assert tracked[0].months_seen == 3


# --- API round trips -----------------------------------------------------------


async def test_upcoming_endpoint_round_trips(
    session: AsyncSession, test_user, test_workspace, client: AsyncClient, auth_headers,
):
    uid, wid = test_user.id, test_workspace.id
    checking = await _account(session, uid, wid, name="Checking", acc_type="checking")
    session.add(_recurring(
        uid, wid, checking, 100, "debit", description="Gym", next_occurrence=TODAY + timedelta(days=4),
    ))
    await session.commit()

    resp = await client.get("/api/wealth/upcoming", headers=auth_headers, params={"days": 30})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["currency"] == CURRENCY
    assert body["days"] == 30
    assert any(i["description"] == "Gym" for i in body["items"])
    assert body["total"] == pytest.approx(100.0)


async def test_cash_forecast_endpoint_round_trips(
    session: AsyncSession, test_user, test_workspace, client: AsyncClient, auth_headers,
):
    uid, wid = test_user.id, test_workspace.id
    checking = await _account(session, uid, wid, name="Checking", acc_type="checking")
    session.add(_txn(uid, wid, checking, 1_000, "credit", description="Opening", when=TODAY))
    await session.commit()

    resp = await client.get("/api/wealth/cash-forecast", headers=auth_headers, params={"days": 90})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["currency"] == CURRENCY
    assert body["days"] == 90
    assert body["start_cash"] == pytest.approx(1_000.0)
    assert len(body["points"]) >= 2
    assert body["points"][0]["date"] == TODAY.isoformat()


async def test_subscriptions_endpoint_round_trips(
    session: AsyncSession, test_user, test_workspace, client: AsyncClient, auth_headers,
):
    uid, wid = test_user.id, test_workspace.id
    checking = await _account(session, uid, wid, name="Checking", acc_type="checking")
    session.add(_recurring(
        uid, wid, checking, 9.99, "debit", description="Spotify", next_occurrence=TODAY + timedelta(days=2),
    ))
    await session.commit()

    resp = await client.get("/api/wealth/subscriptions", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["currency"] == CURRENCY
    assert any(s["merchant"] == "Spotify" for s in body["subscriptions"])
    assert body["monthly_total"] == pytest.approx(9.99)
    assert "suggestions" in body
