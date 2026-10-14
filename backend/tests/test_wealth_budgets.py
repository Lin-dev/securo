"""Budgets suggested from history, budget pace and a monthly spending cap
(fork, plan R2 B5a; Critique C12/R2-m2).

Suggestion tests build their own small seed per scenario rather than one
shared household, because the one-off detector's threshold (plan P1) is
itself derived from the monthly totals being tested — reusing one big
scenario across assertions made some of them accidentally vacuous.
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.workspace_context import WorkspaceContext
from app.models.account import Account
from app.models.budget import Budget
from app.models.category import Category
from app.models.recurring_transaction import RecurringTransaction
from app.models.transaction import Transaction
from app.models.workspace import WorkspaceMember
from app.schemas.budget import BudgetCreate
from app.schemas.wealth import WealthSettingsUpdate, WealthSettingsValues
from app.services.budget_service import create_budget, get_budget_vs_actual
from app.services.wealth import budgets as budgets_service
from app.services.wealth import settings as settings_service
from app.services.wealth.pace import compute_pace
from app.services.wealth.spending import month_start as month_back


def _fixed_datetime(year: int, month: int, day: int) -> type:
    """A `datetime` subclass whose `.now()` is pinned to noon UTC on the given
    date — the same `patch("app.core.app_clock.datetime", ...)` pattern
    `tests/test_app_timezone.py` uses to control `app_today()`."""
    fixed = datetime(year, month, day, 12, 0, tzinfo=timezone.utc)

    class _Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed

    return _Fixed


async def _ctx(session: AsyncSession, test_user, test_workspace) -> WorkspaceContext:
    member = (
        await session.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == test_workspace.id, WorkspaceMember.user_id == test_user.id,
            )
        )
    ).scalar_one()
    return WorkspaceContext(workspace=test_workspace, member=member, user=test_user)


async def _account(session: AsyncSession, user_id, workspace_id, name="Checking", acc_type="checking") -> Account:
    acc = Account(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, name=name,
        type=acc_type, balance=Decimal("0"), currency="USD",
    )
    session.add(acc)
    await session.flush()
    return acc


async def _category(session: AsyncSession, user_id, workspace_id, name, *, transfer=False) -> Category:
    cat = Category(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, name=name,
        icon="circle-help", color="#123456", treat_as_transfer=transfer,
    )
    session.add(cat)
    await session.flush()
    return cat


def _txn(user_id, workspace_id, account, amount, typ, category=None, **kw) -> Transaction:
    when = kw.pop("when")
    return Transaction(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, account_id=account.id,
        description=kw.pop("description", f"{typ} {amount}"), amount=Decimal(str(amount)),
        date=when, type=typ, source=kw.pop("source", "manual"), currency="USD",
        status=kw.pop("status", "posted"), created_at=datetime.now(timezone.utc),
        category_id=category.id if category else None, **kw,
    )


# ---------------------------------------------------------------------------
# suggest_budgets: which months, medians, one-offs, refunds, existing budget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suggest_budgets_full_months_only_and_existing_budget(session, test_user, test_workspace):
    uid, wid = test_user.id, test_workspace.id
    ctx = await _ctx(session, test_user, test_workspace)
    checking = await _account(session, uid, wid)
    rent = await _category(session, uid, wid, "Rent")

    today = date.today()
    m1 = month_back(today, 1)
    m2 = month_back(today, 2)
    m3_mid = month_back(today, 3) + timedelta(days=14)  # mid-month: m3 becomes the partial coverage-start month

    session.add_all([
        _txn(uid, wid, checking, 99999, "debit", rent, when=m3_mid),  # partial month — must never count
        _txn(uid, wid, checking, 400, "debit", rent, when=m2),
        _txn(uid, wid, checking, 800, "debit", rent, when=m1),
    ])
    await create_budget(
        session, wid, uid, BudgetCreate(category_id=rent.id, amount=Decimal("950"), month=m1, is_recurring=True),
    )
    await session.commit()

    result = await budgets_service.suggest_budgets(session, ctx, WealthSettingsValues())

    assert result.months == [m2, m1]  # never m3 (partial) or m0 (current)
    by_cat = {s.category_id: s for s in result.suggestions}
    assert by_cat[rent.id].monthly_median == pytest.approx(600.0)  # (400 + 800) / 2, m3's 99999 excluded
    assert by_cat[rent.id].months_used == 2
    assert by_cat[rent.id].existing_budget == pytest.approx(950.0)
    assert by_cat[rent.id].category == "Rent"
    assert result.total_suggested == pytest.approx(600.0)
    assert result.typical_monthly_spending is not None


@pytest.mark.asyncio
async def test_suggest_budgets_excludes_one_offs(session, test_user, test_workspace):
    uid, wid = test_user.id, test_workspace.id
    ctx = await _ctx(session, test_user, test_workspace)
    checking = await _account(session, uid, wid)
    misc = await _category(session, uid, wid, "Misc")

    today = date.today()
    m1 = month_back(today, 1)
    m2 = month_back(today, 2)
    m3 = month_back(today, 3)  # exactly day 1 -> a full month, not partial

    session.add_all([
        _txn(uid, wid, checking, 200, "debit", misc, when=m3),
        _txn(uid, wid, checking, 400, "debit", misc, when=m2),
        _txn(uid, wid, checking, 300, "debit", misc, when=m1),
        _txn(uid, wid, checking, 5000, "debit", misc, when=m1 + timedelta(days=10)),  # the one-off
    ])
    await session.commit()

    result = await budgets_service.suggest_budgets(session, ctx, WealthSettingsValues())

    assert result.months == [m3, m2, m1]
    by_cat = {s.category_id: s for s in result.suggestions}
    # Without the one-off excluded, m1's raw actual is 5300 and the median would be 400.
    assert by_cat[misc.id].monthly_median == pytest.approx(300.0)


@pytest.mark.asyncio
async def test_suggest_budgets_nets_refunds(session, test_user, test_workspace):
    uid, wid = test_user.id, test_workspace.id
    ctx = await _ctx(session, test_user, test_workspace)
    checking = await _account(session, uid, wid)
    dining = await _category(session, uid, wid, "Dining")

    today = date.today()
    m1 = month_back(today, 1)
    m2 = month_back(today, 2)  # exactly day 1 -> a full month

    session.add_all([
        _txn(uid, wid, checking, 500, "debit", dining, when=m2),
        _txn(uid, wid, checking, 100, "credit", dining, when=m2),  # refund, nets to 400
        _txn(uid, wid, checking, 500, "debit", dining, when=m1),
        _txn(uid, wid, checking, 100, "credit", dining, when=m1),
    ])
    await session.commit()

    result = await budgets_service.suggest_budgets(session, ctx, WealthSettingsValues())

    by_cat = {s.category_id: s for s in result.suggestions}
    assert by_cat[dining.id].monthly_median == pytest.approx(400.0)  # not 500


@pytest.mark.asyncio
async def test_suggest_budgets_skips_income_and_transfer_categories(session, test_user, test_workspace):
    uid, wid = test_user.id, test_workspace.id
    ctx = await _ctx(session, test_user, test_workspace)
    checking = await _account(session, uid, wid)
    salary = await _category(session, uid, wid, "Salary")
    contrib = await _category(session, uid, wid, "Investment contribution", transfer=True)

    today = date.today()
    m1 = month_back(today, 1)
    m2 = month_back(today, 2)  # exactly day 1 -> a full month

    session.add_all([
        # A debit against the income category gives it a positive budget-actual,
        # so skipping it actually exercises the income-category filter rather
        # than relying on it being incidentally zero.
        _txn(uid, wid, checking, 50, "debit", salary, when=m2),
        _txn(uid, wid, checking, 50, "debit", salary, when=m1),
        _txn(uid, wid, checking, 70, "debit", contrib, when=m2),
        _txn(uid, wid, checking, 70, "debit", contrib, when=m1),
    ])
    await session.commit()

    settings = WealthSettingsValues(income_category_ids=[salary.id])
    result = await budgets_service.suggest_budgets(session, ctx, settings)

    by_cat = {s.category_id: s for s in result.suggestions}
    assert salary.id not in by_cat
    assert contrib.id not in by_cat
    assert result.suggestions == []


# ---------------------------------------------------------------------------
# apply_budget_suggestions: rounding, idempotence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_budget_suggestions_creates_rounded_up_recurring_budgets(session, test_user, test_workspace):
    uid, wid = test_user.id, test_workspace.id
    ctx = await _ctx(session, test_user, test_workspace)
    checking = await _account(session, uid, wid)
    utilities = await _category(session, uid, wid, "Utilities")

    today = date.today()
    m1 = month_back(today, 1)
    m2 = month_back(today, 2)  # exactly day 1 -> a full month

    session.add_all([
        _txn(uid, wid, checking, 100, "debit", utilities, when=m2),
        _txn(uid, wid, checking, 133, "debit", utilities, when=m1),
    ])
    await session.commit()
    # median = 116.5 -> rounded up to the nearest 5 = 120
    suggestions = await budgets_service.suggest_budgets(session, ctx, WealthSettingsValues())
    assert suggestions.suggestions[0].monthly_median == pytest.approx(116.5)

    result = await budgets_service.apply_budget_suggestions(session, ctx, [utilities.id])
    assert result.created == 1
    assert result.skipped == 0

    current_month = date(today.year, today.month, 1)
    budget_row = (
        await session.execute(
            select(Budget).where(Budget.workspace_id == wid, Budget.category_id == utilities.id)
        )
    ).scalar_one()
    assert budget_row.is_recurring is True
    assert budget_row.month == current_month
    assert budget_row.amount == Decimal("120")


@pytest.mark.asyncio
async def test_apply_budget_suggestions_second_call_is_idempotent(session, test_user, test_workspace):
    uid, wid = test_user.id, test_workspace.id
    ctx = await _ctx(session, test_user, test_workspace)
    checking = await _account(session, uid, wid)
    utilities = await _category(session, uid, wid, "Utilities")

    today = date.today()
    m1 = month_back(today, 1)
    m2 = month_back(today, 2)

    session.add_all([
        _txn(uid, wid, checking, 100, "debit", utilities, when=m2),
        _txn(uid, wid, checking, 140, "debit", utilities, when=m1),
    ])
    await session.commit()

    first = await budgets_service.apply_budget_suggestions(session, ctx, [utilities.id])
    assert first.created == 1
    assert first.skipped == 0

    second = await budgets_service.apply_budget_suggestions(session, ctx, [utilities.id])
    assert second.created == 0
    assert second.skipped == 1

    count = (
        await session.execute(
            select(Budget).where(Budget.workspace_id == wid, Budget.category_id == utilities.id)
        )
    ).scalars().all()
    assert len(count) == 1  # the second call never updated or duplicated it


@pytest.mark.asyncio
async def test_apply_budget_suggestions_skips_a_category_with_no_suggestion(session, test_user, test_workspace):
    uid, wid = test_user.id, test_workspace.id
    ctx = await _ctx(session, test_user, test_workspace)
    await _account(session, uid, wid)
    empty_cat = await _category(session, uid, wid, "Never Spent")
    await session.commit()

    result = await budgets_service.apply_budget_suggestions(session, ctx, [empty_cat.id])
    assert result.created == 0
    assert result.skipped == 1


# ---------------------------------------------------------------------------
# the pace formula: day 1, mid-month, the last day (pure function)
# ---------------------------------------------------------------------------


def test_pace_day_one_recurring_rent_only_is_not_over():
    pace = compute_pace(
        budget=Decimal("2000"), spent=Decimal("1500"), r_to_date=Decimal("1500"), r_month=Decimal("1500"),
        days_elapsed=1, days_in_month=30,
    )
    assert pace.status != "over"
    assert pace.status == "on_pace"


def test_pace_mid_month_overspending_is_over():
    pace = compute_pace(
        budget=Decimal("1000"), spent=Decimal("700"), r_to_date=Decimal("0"), r_month=Decimal("0"),
        days_elapsed=15, days_in_month=30,
    )
    assert pace.status == "over"


def test_pace_last_day_has_no_left_per_day():
    pace = compute_pace(
        budget=Decimal("1000"), spent=Decimal("900"), r_to_date=Decimal("0"), r_month=Decimal("0"),
        days_elapsed=30, days_in_month=30,
    )
    assert pace.left_per_day is None
    assert pace.left_to_spend == Decimal("100")


def test_pace_all_recurring_month_is_on_pace_at_zero_and_over_above_it():
    # The whole budget is recurring (r_month == budget) and none of it has come
    # due yet (r_to_date == 0), so `expected_to_date` is 0: zero spent so far
    # reads `on_pace`, anything above it reads `over` (plan's zero-expected rule).
    on_pace = compute_pace(
        budget=Decimal("500"), spent=Decimal("0"), r_to_date=Decimal("0"), r_month=Decimal("500"),
        days_elapsed=5, days_in_month=30,
    )
    assert on_pace.expected_to_date == Decimal("0")
    assert on_pace.status == "on_pace"
    over = compute_pace(
        budget=Decimal("500"), spent=Decimal("50"), r_to_date=Decimal("0"), r_month=Decimal("500"),
        days_elapsed=5, days_in_month=30,
    )
    assert over.status == "over"


# ---------------------------------------------------------------------------
# budget_pace: an end-to-end check that a recurring rent charge isn't "over"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_pace_day_one_recurring_rent_only_integration(session, test_user, test_workspace, monkeypatch):
    uid, wid = test_user.id, test_workspace.id
    ctx = await _ctx(session, test_user, test_workspace)
    checking = await _account(session, uid, wid)
    rent = await _category(session, uid, wid, "Rent")

    day_one = _fixed_datetime(2026, 11, 1)
    rent_date = date(2026, 11, 1)

    recurring = RecurringTransaction(
        id=uuid.uuid4(), user_id=uid, workspace_id=wid, account_id=checking.id, category_id=rent.id,
        description="Rent", amount=Decimal("1500.00"), currency="USD", type="debit", frequency="monthly",
        day_of_month=1, start_date=date(2026, 1, 1), next_occurrence=date(2026, 12, 1), is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    session.add(recurring)
    await session.flush()
    session.add(_txn(
        uid, wid, checking, 1500, "debit", rent, when=rent_date, recurring_transaction_id=recurring.id,
    ))
    await session.commit()

    monkeypatch.setenv("TZ", "UTC")
    settings = WealthSettingsValues(spending_cap=2000.0)
    with patch("app.core.app_clock.datetime", day_one):
        result = await budgets_service.budget_pace(session, ctx, settings)

    assert result.month == date(2026, 11, 1)
    assert result.days_elapsed == 1
    assert result.days_in_month == 30
    assert result.spent == pytest.approx(1500.0)
    assert result.cap is not None
    assert result.cap.status != "over"


# ---------------------------------------------------------------------------
# get_budget_vs_actual: pace fields, current month only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_vs_actual_fills_pace_for_current_month_and_leaves_past_months_none(
    session, test_user, test_workspace, monkeypatch,
):
    uid, wid = test_user.id, test_workspace.id
    checking = await _account(session, uid, wid)
    internet = await _category(session, uid, wid, "Internet")

    mid_month = _fixed_datetime(2026, 11, 10)
    current_month = date(2026, 11, 1)
    past_month = date(2026, 10, 1)

    await create_budget(
        session, wid, uid,
        BudgetCreate(category_id=internet.id, amount=Decimal("100"), month=current_month, is_recurring=True),
    )
    session.add_all([
        _txn(uid, wid, checking, 40, "debit", internet, when=date(2026, 11, 5)),
        _txn(uid, wid, checking, 90, "debit", internet, when=date(2026, 10, 5)),
    ])
    await create_budget(
        session, wid, uid,
        BudgetCreate(category_id=internet.id, amount=Decimal("100"), month=past_month, is_recurring=False),
    )
    await session.commit()

    monkeypatch.setenv("TZ", "UTC")
    with patch("app.core.app_clock.datetime", mid_month):
        current_rows = await get_budget_vs_actual(session, wid, uid, month=None)
        past_rows = await get_budget_vs_actual(session, wid, uid, month=past_month)

    current_row = next(r for r in current_rows if r.category_id == internet.id)
    past_row = next(r for r in past_rows if r.category_id == internet.id)

    expected = compute_pace(
        budget=Decimal("100"), spent=current_row.actual_amount, r_to_date=Decimal("0"), r_month=Decimal("0"),
        days_elapsed=10, days_in_month=30,
    )
    assert current_row.days_elapsed == 10
    assert current_row.days_in_month == 30
    assert current_row.expected_to_date == expected.expected_to_date
    assert current_row.projected_month_end == expected.projected_month_end
    assert current_row.pace_status == expected.status
    assert current_row.left_to_spend == expected.left_to_spend
    assert current_row.left_per_day == expected.left_per_day

    assert past_row.expected_to_date is None
    assert past_row.projected_month_end is None
    assert past_row.pace_status is None
    assert past_row.left_to_spend is None
    assert past_row.left_per_day is None
    assert past_row.days_elapsed is None
    assert past_row.days_in_month is None


# ---------------------------------------------------------------------------
# spending_cap in settings: set, clear, report in sources, and its pace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spending_cap_set_clear_and_its_pace(session, test_user, test_workspace):
    ctx = await _ctx(session, test_user, test_workspace)

    read_before = await settings_service.read_settings(session, ctx)
    assert read_before.values.spending_cap is None
    assert read_before.sources["spending_cap"] == "default"

    updated = await settings_service.update_settings(
        session, ctx, WealthSettingsUpdate(spending_cap=800.0),
    )
    assert updated.values.spending_cap == pytest.approx(800.0)
    assert updated.sources["spending_cap"] == "user"

    pace = await budgets_service.budget_pace(session, ctx, updated.values)
    assert pace.cap is not None
    assert pace.cap.cap == pytest.approx(800.0)
    assert pace.cap.left_to_spend == pytest.approx(800.0 - pace.spent)

    cleared = await settings_service.update_settings(
        session, ctx, WealthSettingsUpdate(clear_spending_cap=True),
    )
    assert cleared.values.spending_cap is None
    # Matches the `goal_id`/`clear_goal` convention: an explicit clear still
    # counts as "user" in `sources` (the key is present, just set to null) —
    # "default" means never touched, not "equals the default value".
    assert cleared.sources["spending_cap"] == "user"

    pace_after_clear = await budgets_service.budget_pace(session, ctx, cleared.values)
    assert pace_after_clear.cap is None


# ---------------------------------------------------------------------------
# API round trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_pace_and_suggestions_round_trip_through_the_api(client, auth_headers):
    put_resp = await client.put(
        "/api/wealth/settings", json={"spending_cap": 900.0}, headers=auth_headers,
    )
    assert put_resp.status_code == 200
    put_data = put_resp.json()
    assert put_data["values"]["spending_cap"] == pytest.approx(900.0)
    assert put_data["sources"]["spending_cap"] == "user"

    pace_resp = await client.get("/api/wealth/budget-pace", headers=auth_headers)
    assert pace_resp.status_code == 200
    pace_data = pace_resp.json()
    assert pace_data["cap"] is not None
    assert pace_data["cap"]["cap"] == pytest.approx(900.0)

    suggestions_resp = await client.get("/api/wealth/budget-suggestions", headers=auth_headers)
    assert suggestions_resp.status_code == 200
    suggestions_data = suggestions_resp.json()
    assert "suggestions" in suggestions_data
    assert "total_suggested" in suggestions_data

    apply_resp = await client.post(
        "/api/wealth/budget-suggestions/apply", json={"category_ids": []}, headers=auth_headers,
    )
    assert apply_resp.status_code == 200
    assert apply_resp.json() == {"created": 0, "skipped": 0}

    clear_resp = await client.put(
        "/api/wealth/settings", json={"clear_spending_cap": True}, headers=auth_headers,
    )
    assert clear_resp.status_code == 200
    assert clear_resp.json()["values"]["spending_cap"] is None

    pace_after_clear = await client.get("/api/wealth/budget-pace", headers=auth_headers)
    assert pace_after_clear.json()["cap"] is None
